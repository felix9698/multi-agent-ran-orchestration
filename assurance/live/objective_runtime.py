"""One live runtime for any objective family, built from its frozen bundle.

Gate 3 wired a single case -- PIN_TO_CELL -- with its contract family written
out longhand in :mod:`assurance.live.pin_to_cell_driver`.  Gate 4 gave every
family a builder that produces the same shape,
:class:`~assurance.objectives.family.ObjectiveContractBundle`, so Gate 5 does
not need a second driver per objective: it needs the one driver to stop naming
the objective.

That is all this module is.  It takes a bundle, the live binding and the same
injected ports the Gate 3 runtime takes, and produces the same
:class:`~assurance.vertical.VerticalPath`.  Everything family-specific -- which
counter carries the evidence, which scope the samples are stamped with, which
cadence the windows are cut on, what the baseline and the contracted safe state
are -- is read off the bundle rather than passed in beside it, because a caller
that could pass a cadence different from the one the epoch froze could produce
a run whose samples the evaluator would refuse for a reason no reader could see.

The seam is unchanged: no transport, no clock, no model client.  A family whose
``policy_lifecycle().policy_type_id`` is ``None`` is refused here rather than
wired to an actuator the deployment never advertised.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from functools import reduce
from math import gcd
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from assurance.collector.live import build_live_collectors
from assurance.collector.samples import ClockHealth, MissingInterval, RawSample
from assurance.contracts.ledgers import EvidenceCell
from assurance.contracts.measurement import MeasurementSource
from assurance.core.axes import EvidenceCellStatus
from assurance.core.confirmation import ConfirmationAction, ConfirmationRecord
from assurance.contracts.validation import contract_content_hash
from assurance.gateway.gateway import TokenBoundWriteGateway
from assurance.gateway.journal import InMemoryTransactionJournal
from assurance.gateway.live import R1StatusPoller, build_live_r1_adapter
from assurance.kernel.event_store import MemoryEventStore
from assurance.kernel.kernel import AssuranceKernel
from assurance.kernel.reducer import KernelReducer
from assurance.core.addressing import content_hash
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.core.timebase import format_utc, parse_utc
from assurance.live.pin_to_cell_driver import (
    CorroboratedServingCellReadback,
    InjectedClock,
    KpmUeAttributionReader,
    LiveDriverError,
    LiveUeObservation,
    ServingCellCollector,
    same_host_clock_health,
    serving_cell_projection,
)
from assurance.vertical import VerticalDeployment, VerticalPath

__all__ = [
    "LiveObjectiveRuntime",
    "ObjectiveNotActuable",
    "bundle_geometry",
    "build_live_objective_runtime",
]


#: The one configuration counter this runtime reads through its own UE
#: attribution collector rather than through a counter-grid loader.
SERVING_CELL_COUNTER = "UE.ServingCell"


class ObjectiveNotActuable(LiveDriverError):
    """The family has no policy type on this deployment, so it cannot be submitted."""


@dataclass(frozen=True)
class CounterGeometry:
    """One counter and the measurement geometry that reads it."""

    counter_id: str
    deployment_counter_name: str
    source: MeasurementSource
    scope: Mapping[str, str]
    measurement_refs: Tuple[str, ...]
    cadence_ms: int
    window_width_ms: int
    hold_ms: int
    freshness_bound_ms: int


@dataclass(frozen=True)
class BundleGeometry:
    """The complete observation geometry frozen by a bundle."""

    counters: Tuple[CounterGeometry, ...]
    cadence_ms: int
    window_width_ms: int
    hold_ms: int
    freshness_bound_ms: int

    @property
    def counter_id(self) -> str:
        """Compatibility surface for the unchanged one-counter runtime."""
        if len(self.counters) != 1:
            raise LiveDriverError("a multi-counter geometry has no single counter_id")
        return self.counters[0].counter_id


def bundle_geometry(bundle: Any) -> BundleGeometry:
    """Read the window geometry off the bundle, refusing an inconsistent set.

    The collector has to report on the cadence the epoch froze and stamp the
    counter the predicates read.  Both are already stated by the measurement
    contracts, so they are derived here instead of being handed in: a mismatch
    between what the collector emits and what the evaluator windows is a
    failure with no visible cause, and this is the one place it can be refused
    with a name.
    """
    measurements = tuple(bundle.measurements)
    if not measurements:
        raise LiveDriverError("the bundle states no measurement contract")
    cadences = {int(m.cadence_ms) for m in measurements}
    bindings = {counter.counter_id: counter for counter in bundle.counters}
    unknown = sorted({m.counter_id for m in measurements} - set(bindings))
    if unknown:
        raise LiveDriverError("measurements name unbound counters: " + ", ".join(unknown))
    counter_geometries = []
    for counter_id in sorted({m.counter_id for m in measurements}):
        relevant = tuple(m for m in measurements if m.counter_id == counter_id)
        counter = bindings[counter_id]
        counter_cadences = {int(m.cadence_ms) for m in relevant}
        if len(counter_cadences) != 1:
            raise LiveDriverError(f"measurements for {counter_id} disagree on cadence")
        scopes = {
            tuple(sorted((str(k), str(v)) for k, v in m.scope_selector.items()))
            for m in relevant
        }
        if len(scopes) != 1:
            raise LiveDriverError(f"measurements for {counter_id} disagree on scope")
        counter_geometries.append(CounterGeometry(
            counter_id=counter_id,
            deployment_counter_name=counter.deployment_counter_name,
            source=counter.source,
            scope=dict(scopes.pop()),
            measurement_refs=tuple(sorted(m.contract_id for m in relevant)),
            cadence_ms=counter_cadences.pop(),
            window_width_ms=max(int(m.window_width_ms) for m in relevant),
            hold_ms=max(int(m.hold_ms) for m in relevant),
            freshness_bound_ms=min(int(m.freshness_bound_ms) for m in relevant),
        ))
    return BundleGeometry(
        counters=tuple(counter_geometries),
        # Poll on the common grid while each counter collector emits only at
        # its own cadence.  With 1 s serving-cell and 60 s QoS counters this
        # preserves one shared anchor without delaying readback confirmation.
        cadence_ms=reduce(gcd, cadences),
        window_width_ms=max(int(m.window_width_ms) for m in measurements),
        hold_ms=max(int(m.hold_ms) for m in measurements),
        freshness_bound_ms=min(int(m.freshness_bound_ms) for m in measurements),
    )


@dataclass
class LiveObjectiveRuntime:
    """One live objective case, wired end to end."""

    family: str
    bundle: Any
    geometry: BundleGeometry
    kernel: AssuranceKernel
    gateway: TokenBoundWriteGateway
    adapter: Any
    collector: Any
    readback: Any
    reader: KpmUeAttributionReader
    identity: LiveUeObservation
    clock: InjectedClock
    path: VerticalPath
    case_id: str
    cell_id: str
    epoch: Any
    event_store: MemoryEventStore = field(repr=False, default=None)
    #: Adapter key -> adapter for every SUPPLEMENTARY participant this case
    #: registered.  Empty for a single-participant run, which is every case
    #: before WP-D's cap.
    supplementary_adapters: Mapping[str, Any] = field(default_factory=dict)

    def candidate_id(self) -> str:
        return self.kernel.current_catalog().candidates[0].candidate_id


def _confirmation(contract: Any, *, event_id: str, at: str) -> ConfirmationRecord:
    return ConfirmationRecord(
        confirmed_object_type=type(contract).__name__,
        confirmed_content_hash=contract_content_hash(contract),
        event_id=event_id,
        timestamp=at,
        action=ConfirmationAction.CONFIRM_AND_START,
    )


def _admit_and_freeze(kernel: AssuranceKernel, bundle: Any, clock: InjectedClock) -> Any:
    """Admit the bundle in the Kernel's required order and freeze the epoch."""
    now = clock()
    vector_confirmation = _confirmation(
        bundle.vector, event_id="confirm/live-objective-vector", at=now
    )
    policy_confirmation = _confirmation(
        bundle.case_policy, event_id="confirm/live-objective-policy", at=now
    )
    unconfirmed = (
        *bundle.counters, *bundle.measurements, bundle.target, bundle.release,
        *bundle.watchdogs, bundle.harm, *bundle.actuators, *bundle.capabilities,
        bundle.composition,
    )
    for contract in unconfirmed:
        kernel.admit_contract(contract, confirmation=None, now=clock())
    kernel.admit_contract(bundle.vector, confirmation=vector_confirmation, now=clock())
    kernel.admit_contract(
        bundle.case_policy, confirmation=policy_confirmation, now=clock()
    )
    kernel.admit_deployment(bundle.deployment, now=clock())
    return kernel.freeze_epoch(confirmation=vector_confirmation, now=clock())


class _KpmFileTail:
    """Read complete, newly appended JSONL records from one bound file."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._offset = 0

    def __call__(self) -> Tuple[str, ...]:
        if not self._path.is_file():
            return ()
        with self._path.open("rb") as stream:
            stream.seek(self._offset)
            raw = stream.read()
        complete, separator, _remainder = raw.rpartition(b"\n")
        if not separator:
            return ()
        self._offset += len(complete) + 1
        return tuple(
            line.decode("utf-8", "replace")
            for line in complete.split(b"\n")
            if line
        )


class _CounterGridCollector:
    """Project one real source counter onto its contract's shared grid.

    Each source sample can feed at most one grid interval.  An empty interval
    produces an explicit gap; values are never filled from the preceding
    interval.  The source trace hash and clock health survive the projection.
    """

    def __init__(
        self,
        *,
        geometry: CounterGeometry,
        anchor: Callable[[], Optional[str]],
        load_samples: Callable[[], Sequence[RawSample]],
        membership: Sequence[str],
        ue_aliases: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.geometry = geometry
        #: UE label -> the AMF UE NGAP id the stream carries for it in this case
        #: (docs/design/ue-identity-continuity.md); a numeric label is its own id.
        #: 2026-09-22: 값은 **매 읽기 재해석되는 해석기**일 수 있다.  조립 시점 번호로
        #: 얼려 두면 UE 가 판 도중 재등록했을 때 기대값만 옛 번호에 남아 `_scope_matches`
        #: 가 그 UE 의 기록을 전부 거절하고, coverage 가 미달해 KPI 가 통째로 게시되지
        #: 않는다 -- 바로 옆 `ServingCellCollector` 가 2026-09-17 에 같은 이유로
        #: 고쳐졌는데 이 형제만 얼어 있었다.
        self._ue_aliases = {str(k): v for k, v in dict(ue_aliases or {}).items()}
        self._anchor_of = anchor
        self._load_samples = load_samples
        self._membership = tuple(str(item) for item in membership)
        self._sink: Optional[Callable[[RawSample], None]] = None
        self._anchor: Optional[str] = None
        self._next_slot = 0
        self._sequence = 0
        self._pending: List[RawSample] = []
        self._emitted: List[RawSample] = []
        self._clock_health = ClockHealth.UNKNOWN

    def bind_sink(self, sink: Callable[[RawSample], None]) -> None:
        if self._sink is not None:
            raise RuntimeError("a collector delivers to exactly one Kernel sink")
        self._sink = sink

    def poll(self, *, now: str) -> Sequence[RawSample]:
        self._pending.extend(
            sample for sample in self._load_samples()
            if sample.counter_id == self.geometry.deployment_counter_name
            and self._scope_matches(sample.scope_snapshot)
        )
        if self._anchor is None:
            self._anchor = self._anchor_of()
            if self._anchor is None:
                return ()
        anchor = parse_utc(self._anchor)
        edge = parse_utc(now)
        # A sample at or before the next slot's lower edge can never feed a
        # slot again, and one for another UE never could.  Keeping them made
        # every poll sort and rescan the whole KPM history (the tail starts at
        # offset 0): ~340 ms per counter on 2026-09-15 attempt 44, 3 s per
        # poll, TELEMETRY_STALE.  Assumes a restarted grid's anchor never moves
        # back before slots already emitted.
        floor = anchor + timedelta(
            milliseconds=(self._next_slot - 1) * self.geometry.cadence_ms)
        self._pending = [
            sample for sample in self._pending
            if parse_utc(sample.observed_at) > floor
        ]
        self._pending.sort(key=lambda sample: (sample.observed_at, sample.sample_id))
        produced = []
        while True:
            instant = anchor + timedelta(
                milliseconds=self._next_slot * self.geometry.cadence_ms
            )
            if instant > edge:
                break
            previous = instant - timedelta(milliseconds=self.geometry.cadence_ms)
            eligible = [
                sample for sample in self._pending
                if previous < parse_utc(sample.observed_at) <= instant
            ]
            source = eligible[-1] if eligible else None
            if source is not None:
                self._pending.remove(source)
            produced.append(self._project(source, format_utc(instant)))
            self._next_slot += 1
        for sample in produced:
            self._clock_health = sample.clock_health
            if self._sink is not None:
                self._sink(sample)
        self._emitted.extend(produced)
        return tuple(produced)

    def _scope_matches(self, scope: Mapping[str, str]) -> bool:
        if self.geometry.source is MeasurementSource.O1_PM:
            expected = self.geometry.scope.get("cellId", "")
            delivered = str(scope.get("nrCellDu", ""))
            return not expected or delivered == expected.rsplit("-", 1)[-1]
        if self.geometry.source in (
                MeasurementSource.E2_KPM, MeasurementSource.CONFIGURATION_READBACK):
            # A per-UE counter is attributed to the UE its contract names,
            # whether that UE is the objective one (``ueId``) or a controlled
            # one (``controlledUeId``).  Two UEs publish the same counter name
            # on one stream, so a collector that took the newest record of
            # either would report one UE's configuration as the other's.
            expected = (self.geometry.scope.get("ueId", "")
                        or self.geometry.scope.get("controlledUeId", ""))
            alias = self._ue_aliases.get(str(expected), expected)
            expected = str(alias() if callable(alias) else alias)
            delivered = str(scope.get("amf_ue_ngap_id", ""))
            return not expected or delivered == expected
        return True

    def _project(self, source: Optional[RawSample], instant: str) -> RawSample:
        sequence = self._sequence
        self._sequence += 1
        scope = dict(self.geometry.scope)
        if self._membership:
            scope["ueId"] = self._membership[0]
        missing: Tuple[MissingInterval, ...] = ()
        if source is None:
            value = 0.0
            unit = "1"
            clock_health = ClockHealth.UNKNOWN
            trace_hash = content_hash({
                "counterId": self.geometry.counter_id,
                "observedAt": instant,
                "gap": True,
            })
            missing = (MissingInterval(
                format_utc(parse_utc(instant) - timedelta(
                    milliseconds=self.geometry.cadence_ms)),
                instant,
                f"no {self.geometry.source.value} sample in the grid interval",
            ),)
        else:
            value = float(source.value.value)
            unit = source.value.unit
            clock_health = source.clock_health
            trace_hash = source.trace_hash
            missing = tuple(source.missing_intervals)
        sample_id = content_hash({
            "counterId": self.geometry.counter_id,
            "observedAt": instant,
            "sequence": sequence,
            "traceHash": trace_hash,
        })
        return RawSample(
            sample_id=sample_id,
            counter_id=self.geometry.counter_id,
            value=TypedQuantity(value, unit, Provenance.MEASURED, sample_id),
            scope_snapshot=scope,
            observed_at=instant,
            cadence_ms=self.geometry.cadence_ms,
            clock_health=clock_health,
            trace_hash=trace_hash,
            sequence=sequence,
            missing_intervals=missing,
        )

    def clock_health(self) -> ClockHealth:
        return self._clock_health

    def restart_grid(self) -> None:
        """Take the next trial's anchor afresh (see ``ServingCellCollector``)."""
        self._anchor = None
        self._next_slot = 0

    def scope_snapshot(self) -> Mapping[str, str]:
        return dict(self.geometry.scope)

    def describe_source(self) -> Mapping[str, Any]:
        return {
            "kind": self.geometry.source.value,
            "counterId": self.geometry.counter_id,
            "deploymentCounterName": self.geometry.deployment_counter_name,
            "networkAccess": False,
        }

    def emitted(self) -> Tuple[RawSample, ...]:
        return tuple(self._emitted)


class _MultiCounterCollector:
    """One MeasurementCollector surface over counter-specific collectors."""

    def __init__(self, collectors: Sequence[Any]) -> None:
        self.collectors = tuple(collectors)

    def bind_sink(self, sink: Callable[[RawSample], None]) -> None:
        for collector in self.collectors:
            collector.bind_sink(sink)

    def poll(self, *, now: str) -> Sequence[RawSample]:
        return tuple(
            sample
            for collector in self.collectors
            for sample in collector.poll(now=now)
        )

    def restart_grid(self) -> None:
        for collector in self.collectors:
            restart = getattr(collector, "restart_grid", None)
            if callable(restart):
                restart()

    def clock_health(self) -> ClockHealth:
        health = {collector.clock_health() for collector in self.collectors}
        return (ClockHealth.SYNCHRONISED
                if health == {ClockHealth.SYNCHRONISED}
                else ClockHealth.UNKNOWN)

    def scope_snapshot(self) -> Mapping[str, str]:
        merged: Dict[str, str] = {}
        for collector in self.collectors:
            merged.update(collector.scope_snapshot())
        return merged

    def describe_source(self) -> Mapping[str, Any]:
        return {"kind": "multi-counter", "sources": tuple(
            collector.describe_source() for collector in self.collectors
        )}

    def emitted(self) -> Tuple[RawSample, ...]:
        return tuple(
            sample for collector in self.collectors for sample in collector.emitted()
        )


def build_live_objective_runtime(
    *,
    family_module: Any,
    scope: Mapping[str, Any],
    binding: Any,
    policy_port: Any,
    policy_builder_factory: Callable[
        [AssuranceKernel, Any], Callable[[Mapping[str, Any]], Dict[str, Any]]
    ],
    reader: KpmUeAttributionReader,
    identity: LiveUeObservation,
    now: Callable[[], str],
    monotonic_ms: Callable[[], int],
    sleep_ms: Callable[[int], None],
    case_id: str,
    cell_id: str = "cell/live-objective",
    adapter_name: str = "r1",
    adapter_override: Optional[Any] = None,
    counter_sample_loaders: Optional[
        Mapping[str, Callable[[], Sequence[RawSample]]]
    ] = None,
    arrival: Optional[Callable[[], str]] = None,
    supplementary_adapters: Optional[Mapping[str, Any]] = None,
    axis_adapters: Optional[Mapping[str, str]] = None,
    supplementary_axes: Tuple[Mapping[str, Any], ...] = (),
    bundle_transform: Optional[Callable[[Any], Any]] = None,
) -> LiveObjectiveRuntime:
    """Wire one live objective case from its frozen bundle.

    ``supplementary_adapters``, ``axis_adapters`` and ``supplementary_axes``
    are the three halves of a multi-participant composition, and they are
    separate because they answer different questions.  The first registers the
    extra clients (``r1-cap``) the gateway may dispatch through; the second
    tells the gateway which *configuration axis* each client owns, so a merged
    readback covers the whole surface instead of half of it; the third tells the
    runtime how a frozen candidate parameter becomes one ordered plan step on
    one of those clients.  All three default to empty, and a run with them empty
    is byte-for-byte the single-participant run it always was.

    A SUPPLEMENTARY participant is never a substitute for the PRIMARY one.  The
    plan writes the PRIMARY steering step first and the cap after it, and
    reverse rollback unwinds them the other way round, because an RNTI is
    cell-local and a cap resolved before the steering readback is a cap on an
    identity the move may have ended.

    ``adapter_override`` exists for the hardware-free dry run: it lets the same
    wiring be driven over
    :class:`~assurance.gateway.mock_adapter.MockActuationAdapter` so the
    contracts, the admission order and the terminal branches are exercised
    without a transport.  It is not a way to make a mock run count: the
    Kernel's evidence state and the registry's OTA level both remain what they
    were, and nothing in this module writes either.
    """
    lifecycle = family_module.policy_lifecycle()
    if adapter_override is None and lifecycle.policy_type_id is None:
        raise ObjectiveNotActuable(
            f"{family_module.family} has no policy type on this deployment; "
            "there is nothing to submit and inventing one would be the forced "
            "mapping the gate refuses"
        )
    if adapter_override is None:
        # A family whose policy type exists in the module but is not advertised
        # by the released composition (e.g. SliceSLATarget) must be refused here
        # -- before any reader/adapter use -- surfacing the registry record's own
        # blocking reasons rather than crashing on a reader the caller left None.
        from assurance.objectives import RegistryError, record_for

        capability = record_for(family_module.family).deployment_capability
        if not capability.submittable:
            raise RegistryError(
                f"{family_module.family} is not submittable on this deployment: "
                "its policy type is not advertised by the released composition, "
                "so there is nothing to wire live. "
                + "; ".join(capability.blocking_reasons)
            )

    bundle = family_module.contract_bundle(
        scope=scope, deployment_binding=binding.r1.deployment
    )
    if bundle_transform is not None:
        # The one place a composition may extend a family's frozen contract set
        # before admission -- ``assurance.objectives.action102_support`` adds the
        # SUPPLEMENTARY cap's counters, predicate, watchdogs, harm bound and
        # configuration axis here.  It runs before ``_admit_and_freeze``, so
        # everything it adds is admitted and frozen by the same epoch as the
        # family's own contracts rather than bolted on afterwards.
        bundle = bundle_transform(bundle)
    geometry = bundle_geometry(bundle)
    amf_ue_ngap_id = int(identity.amf_ue_ngap_id)
    clock = InjectedClock(now=now, monotonic_ms=monotonic_ms, sleep_ms=sleep_ms)
    reader.refresh(amf_ue_ngap_id=amf_ue_ngap_id)

    if adapter_override is not None:
        adapter = adapter_override
        readback: Any = None
    else:
        status_poller = R1StatusPoller(
            policy_port,
            cadence_ms=binding.r1.cadence_ms,
            deadline_ms=binding.r1.deadline_ms,
            monotonic_ms=monotonic_ms,
            sleep_ms=sleep_ms,
            projection=serving_cell_projection,
        )
        readback = CorroboratedServingCellReadback(
            reader=reader,
            status_poller=status_poller,
            amf_ue_ngap_id=amf_ue_ngap_id,
            now=now,
            monotonic_ms=monotonic_ms,
            sleep_ms=sleep_ms,
            cadence_ms=binding.r1.cadence_ms,
            deadline_ms=binding.r1.deadline_ms,
            freshness_bound_ms=geometry.freshness_bound_ms,
            corroboration_deadline_ms=geometry.freshness_bound_ms,
        )
        bound: Dict[str, Callable[[Mapping[str, Any]], Dict[str, Any]]] = {}

        def policy_builder(command: Mapping[str, Any]) -> Dict[str, Any]:
            if "build" not in bound:
                raise LiveDriverError("no epoch is frozen yet")
            return bound["build"](command)

        adapter = build_live_r1_adapter(
            binding,
            policy_port=policy_port,
            policy_builder=policy_builder,
            monotonic_ms=monotonic_ms,
            sleep_ms=sleep_ms,
            status_projection=serving_cell_projection,
            readback_port=readback,
        )
        # Same hand-back as the joint route (2026-09-23 audit); an
        # ``adapter_override`` keeps whatever behaviour its caller gave it.
        adapter.restore_by_handover = True

    registered: Dict[str, Any] = {adapter_name: adapter}
    for key, participant in dict(supplementary_adapters or {}).items():
        if key == adapter_name:
            raise LiveDriverError(
                f"supplementary adapter {key!r} collides with the primary adapter"
            )
        registered[key] = participant
    declared_axes = tuple(dict(entry) for entry in supplementary_axes)
    for entry in declared_axes:
        if entry["adapter"] not in registered:
            raise LiveDriverError(
                f"supplementary axis {entry['axis']!r} names unregistered adapter "
                f"{entry['adapter']!r}"
            )
        if entry["axis"] not in dict(bundle.safe_state):
            raise LiveDriverError(
                f"supplementary axis {entry['axis']!r} is not on the bundle's "
                "contracted configuration surface"
            )
    gateway = TokenBoundWriteGateway(
        adapters=registered,
        safe_state=dict(bundle.safe_state),
        journal=InMemoryTransactionJournal(),
        axis_adapters=dict(axis_adapters or {}),
        clock=clock,
    )
    event_store = MemoryEventStore()
    kernel = AssuranceKernel(
        event_store=event_store,
        reducer=KernelReducer(),
        write_gateway=gateway,
        measurement_collector=None,
    )
    epoch = _admit_and_freeze(kernel, bundle, clock)

    catalog = kernel.current_catalog()
    kernel.open_case(
        case_id=case_id,
        policy=bundle.case_policy,
        active_vector=bundle.vector.ordered_target_refs[0],
        usable_reserve={bundle.harm.contract_id: bundle.harm.reserve.to_canonical_dict()},
        reserve_per_trial={
            bundle.harm.contract_id: bundle.harm.reserve.to_canonical_dict()
        },
        evidence_cells=(
            EvidenceCell(
                cell_id=cell_id,
                target_ref=bundle.target.contract_id,
                candidate_semantic_hash=catalog.candidates[0].semantic_hash,
                status=EvidenceCellStatus.OPEN,
                required_independent_contributions=1,
            ),
        ),
        now=clock(),
    )
    if adapter_override is None:
        bound["build"] = policy_builder_factory(kernel, bundle)

    anchor = lambda: _observation_anchor(kernel, case_id)
    serving_geometries = tuple(
        item for item in geometry.counters
        if item.deployment_counter_name == SERVING_CELL_COUNTER
    )
    if len(serving_geometries) != 1:
        raise LiveDriverError(
            "the live actuator requires exactly one UE.ServingCell counter"
        )
    serving_geometry = serving_geometries[0]
    serving_collector = ServingCellCollector(
        reader=reader,
        deployment=None,
        timing=None,
        anchor=anchor,
        amf_ue_ngap_id=amf_ue_ngap_id,
        clock_health=lambda observation, instant: same_host_clock_health(
            observation, instant, tolerance_ms=2 * serving_geometry.cadence_ms
        ),
        counter_id=serving_geometry.counter_id,
        scope_snapshot=bundle.sample_scope,
        cadence_ms=serving_geometry.cadence_ms,
    )
    if len(geometry.counters) == 1:
        # Preserve the historical object and polling path exactly.
        collector: Any = serving_collector
    else:
        loaders = dict(
            _live_counter_loaders(binding, geometry, arrival)
            if counter_sample_loaders is None
            else counter_sample_loaders
        )
        counter_collectors: List[Any] = [serving_collector]
        for item in geometry.counters:
            if item is serving_geometry:
                continue
            loader = loaders.get(item.counter_id)
            if loader is None:
                raise LiveDriverError(
                    f"no live {item.source.value} loader for {item.counter_id}"
                )
            relevant = tuple(
                measurement for measurement in bundle.measurements
                if measurement.counter_id == item.counter_id
            )
            membership = relevant[0].membership_snapshot if relevant else ()
            counter_collectors.append(_CounterGridCollector(
                geometry=item,
                anchor=anchor,
                load_samples=loader,
                membership=membership,
            ))
        collector = _MultiCounterCollector(counter_collectors)
    path = VerticalPath(
        kernel=kernel,
        gateway=gateway,
        deployment=VerticalDeployment(
            adapter_name=adapter_name,
            scope=dict(bundle.scope),
            baseline_config=dict(bundle.baseline_config),
            supplementary_axes=declared_axes,
        ),
        case_id=case_id,
        clock=clock,
        coordinator=None,
        collector=collector,
    )
    return LiveObjectiveRuntime(
        family=family_module.family, bundle=bundle, geometry=geometry, kernel=kernel,
        gateway=gateway, adapter=adapter, collector=collector, readback=readback,
        reader=reader, identity=identity, clock=clock, path=path, case_id=case_id,
        cell_id=cell_id, epoch=epoch, event_store=event_store,
        supplementary_adapters=dict(supplementary_adapters or {}),
    )


def _observation_anchor(kernel: AssuranceKernel, case_id: str) -> Optional[str]:
    anchors = [
        trial.get("observationStartedAt")
        for trial in kernel.reduced_state()["trials"].values()
        if trial.get("caseId") == case_id and trial.get("observationStartedAt")
    ]
    return max(anchors) if anchors else None


def _receiver_arrival() -> str:
    """The instant this host first observed a new PM file, on this host's clock.

    Deliberately the collector's own first sight rather than the file's
    modification time.  ``mtime`` looks like an arrival record and is not one:
    anything that re-copies the file rewrites it, and in this deployment the
    PM mirror does exactly that -- a batch of files an hour apart in content
    was observed carrying one identical ``mtime``, the instant of the last
    copy.  Corroborating a producer's clock against a stamp some third process
    can rewrite would be corroboration against nothing.  First sight belongs to
    the receiver, and no other component can move it.
    """
    return format_utc(datetime.now(timezone.utc))


def _corroborated_by_arrival(
    samples: Sequence[RawSample], received_at: Optional[str]
) -> Tuple[RawSample, ...]:
    """Promote a producer-claimed timestamp only when our clock agrees with it.

    Freshness alone establishes nothing: a producer whose clock is wrong emits
    timestamps that look recent and are not on our timebase, and calling that
    ``SYNCHRONISED`` would let a skewed source satisfy a contract that demands
    synchronisation.  What is evidence is *agreement between two clocks* -- the
    instant the producer claims the reporting period ended, and the instant
    this host saw the file that reports it.  They may differ by at most one
    reporting period, since a file cannot honestly describe a period that has
    not ended and does not wait a whole period to be written.

    Anything already carrying a verdict (notably
    ``DRIFTING_OUT_OF_BOUND`` from an O1 ``suspect`` flag) is left alone: this
    promotes the unknown, it never overrules a source that reported trouble.
    """
    if received_at is None:
        return tuple(samples)
    arrival = parse_utc(received_at)
    observed: list[RawSample] = []
    for sample in samples:
        if sample.clock_health is not ClockHealth.UNKNOWN:
            observed.append(sample)
            continue
        disagreement_ms = abs(
            (arrival - parse_utc(sample.observed_at)).total_seconds() * 1000
        )
        observed.append(
            replace(sample, clock_health=ClockHealth.SYNCHRONISED)
            if disagreement_ms <= sample.cadence_ms
            else sample
        )
    return tuple(observed)


def _receiver_generated(samples: Sequence[RawSample]) -> Tuple[RawSample, ...]:
    """Promote timestamps this host generated itself.

    The KPM collector does not copy a timestamp out of the indication; it
    stamps ``recv_unix_us`` -- the arrival instant read from this host's clock
    -- and derives ``observed_at`` from it
    (``assurance/collector/o1col.py``).  There is no second clock to be out of
    step with, so the sample is on the Kernel's own timebase by construction.
    That is a same-host basis, not an assumption about the RAN's clock, and it
    is the only reason this promotion is sound.
    """
    return tuple(
        replace(sample, clock_health=ClockHealth.SYNCHRONISED)
        if sample.clock_health is ClockHealth.UNKNOWN
        else sample
        for sample in samples
    )


def _live_counter_loaders(
    binding: Any,
    geometry: BundleGeometry,
    arrival: Optional[Callable[[], str]] = None,
) -> Mapping[str, Callable[[], Sequence[RawSample]]]:
    """Bind the two delivered QoS wire counters to their read-only sources.

    ``arrival`` is the receiver's own clock, injectable for the same reason
    every other clock here is: it is *this host's* first sight of a PM file, so
    a caller driving the runtime on an injected timebase has to be able to move
    it or its samples can never corroborate.  The default is the real wall
    clock, which is what a live run must use -- a receiver whose arrival stamp
    could be set by the thing it is corroborating would corroborate nothing.
    """
    arrival = arrival or _receiver_arrival
    o1_pm, kpm = build_live_collectors(binding)
    seen_pm_paths: set[str] = set()
    kpm_tail = _KpmFileTail(binding.kpm_jsonl_path)

    def load_o1() -> Sequence[RawSample]:
        """O1 PM: a producer-claimed instant, corroborated against our own.

        ``observed_at`` here is the granularity period's ``endTime`` as the PM
        producer wrote it (``assurance/collector/o1col.py``), so it is a claim
        about the producer's clock and not an observation of ours.  Each poll
        therefore stamps the instant this host first saw the new files, and
        the two clocks are compared.
        """
        directory = Path(binding.pm_directory)
        paths = tuple(
            str(path) for path in sorted(directory.glob("*"))
            if path.is_file() and str(path) not in seen_pm_paths
        ) if directory.is_dir() else ()
        arrived_at = arrival()
        seen_pm_paths.update(paths)
        samples: list[RawSample] = []
        for path in paths:
            samples.extend(
                _corroborated_by_arrival(o1_pm.collect((path,)), arrived_at)
            )
        return tuple(samples)

    def load_kpm() -> Sequence[RawSample]:
        """KPM: an instant this host generated, so it is already our timebase."""
        return _receiver_generated(kpm.parse_lines(kpm_tail()).samples)

    loaders: Dict[str, Callable[[], Sequence[RawSample]]] = {}
    for item in geometry.counters:
        if item.source is MeasurementSource.O1_PM:
            loaders[item.counter_id] = load_o1
        elif item.source is MeasurementSource.E2_KPM:
            loaders[item.counter_id] = load_kpm
        elif item.source is MeasurementSource.CONFIGURATION_READBACK:
            # ``CONFIGURATION_READBACK`` says what the number *is* -- the
            # scheduler's current setting rather than a rate -- not where it
            # arrives from.  A deployment that publishes it as a KPM counter
            # (``RAN.UE.DlPrbCap``) delivers it on the same indication stream,
            # so it is read there.  ``UE.ServingCell`` is the exception this
            # runtime already handles: it has its own attribution collector,
            # and giving it a second reader of the same tail would consume the
            # lines that one needs.
            if item.deployment_counter_name != SERVING_CELL_COUNTER:
                loaders[item.counter_id] = load_kpm
        else:
            raise LiveDriverError(
                f"no live collector adapter for {item.source.value}"
            )
    return loaders
