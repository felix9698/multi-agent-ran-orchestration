"""PIN_TO_CELL over the real testbed: contracts, collector, readback, runtime.

Gate 3 (task section 13) runs the known physical steering case through the new
architecture.  ``tests/assurance/pin_to_cell_support.py`` said what that case
*is* in the new contract families and proved the Kernel judges it the way the
frozen ``AIC_UECellSteering_1.0.0`` contract does, against a scripted readback.
This module is the same case with the script removed: the counter is fed by the
deployment's own KPM indication stream, and the configuration readback is the
policy status the A1-P producer publishes, corroborated against that stream.

Three things are worth stating before the code.

**An acknowledgement is still not an effect.**  :class:`CorroboratedServingCellReadback`
answers ``None`` -- which the gateway turns into ``UNKNOWN`` -- unless *both*
the frozen status object carries ``readback.result == "VERIFIED"`` and the live
UE attribution stream independently shows the UE on that cell.  Two
independent observations of the same fact, and a disagreement is a refusal
rather than a tie broken in favour of the optimistic one.

**The UE identity is observed, never configured.**  The AMF hands out a new
``amfUeNgapId`` on every registration, and the previous integration failed
repeatedly on a pinned one that had gone stale.  Here the identity -- the
``amfUeNgapId`` *and* the ``guAmI`` it sits under -- is read out of the KPM
format-3 UE attribution record that the deployment is publishing right now, so
a re-attach changes the policy scope by itself.

**The reporting grid is the trial's, and gaps are gaps.**  The Kernel's
evaluator windows samples from the instant the trial started observing.  A
collector that reported on its own phase would never fill a window, so
:class:`ServingCellCollector` reports on the cadence grid anchored at that
instant -- the same thing a TS 32.435 PM job does with its granularity period.
A grid instant with no indication behind it within two cadences is emitted as a
:class:`~assurance.collector.samples.MissingInterval`, never as the previous
value carried forward.

Nothing here opens a transport, reads a clock or imports a model client: every
live port arrives as an injected callable, which is what keeps this module
inside the ``assurance/**`` seam while it drives real radios.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from assurance.advisors.grammar import IntentGrammarEntry
from assurance.collector.collector import SampleSink
from assurance.collector.samples import ClockHealth, MissingInterval, RawSample
from assurance.contracts import (
    ActuatorBinding,
    ActuatorPath,
    Aggregation,
    CapabilityManifest,
    CertifiedHarmBound,
    ClockRequirement,
    ComparisonOperator,
    CompositionManifest,
    CoordinationCasePolicy,
    CounterBinding,
    DeploymentBinding,
    Estimator,
    EvidenceCell,
    GapPolicy,
    HarmContract,
    HarmKind,
    MeasurementContract,
    MeasurementSource,
    OverlapPolicy,
    TargetContract,
    TargetOption,
    TargetPredicate,
    TargetReleasePolicy,
    TargetVector,
    TypedConstraint,
    UncertaintyRule,
    WatchdogAction,
    WatchdogContract,
)
from assurance.contracts.validation import contract_content_hash
from assurance.core.addressing import content_hash
from assurance.core.axes import EvidenceCellStatus
from assurance.core.confirmation import ConfirmationAction, ConfirmationRecord
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.core.timebase import format_utc, parse_utc
from assurance.gateway.gateway import TokenBoundWriteGateway
from assurance.gateway.journal import InMemoryTransactionJournal
from assurance.gateway.live import R1StatusPoller, build_live_r1_adapter
from assurance.kernel.event_store import MemoryEventStore
from assurance.kernel.kernel import AssuranceKernel
from assurance.kernel.reducer import KernelReducer
from assurance.vertical import VerticalDeployment, VerticalPath

__all__ = [
    "COUNTER_ID",
    "NCI_UNIT",
    "PIN_TO_CELL_GRAMMAR",
    "PIN_TO_CELL_OBJECTIVE",
    "POLICY_TYPE_ID",
    "CorroboratedServingCellReadback",
    "InjectedClock",
    "KpmUeAttributionReader",
    "LiveCellTopology",
    "LiveDriverError",
    "LivePinToCellDeployment",
    "LivePinToCellRuntime",
    "LiveTiming",
    "LiveUeObservation",
    "ServingCellCollector",
    "build_live_pin_to_cell_runtime",
    "live_contract_set",
    "pin_utterance",
    "serving_cell_projection",
]

#: The frozen policy type this case actuates through.
POLICY_TYPE_ID = "AIC_UECellSteering_1.0.0"

#: The objective family name the epoch's target contract carries.  Identical to
#: the hardware-free case: Gate 3 runs *the same objective* on real equipment,
#: and renaming it would make the two runs incomparable.
PIN_TO_CELL_OBJECTIVE = "UeCellSteeringPinToCell"

#: ``CId.ncI`` is a count of nothing; the unit token says what the number is an
#: identity of rather than pretending it is dimensionless.
NCI_UNIT = "nci"

COUNTER_ID = "counter/serving-cell"

#: The grammar a deployment offering UE cell steering registers with the
#: deterministic Intent Agent.  Keywords only -- the agent selects an objective
#: from the registry and never invents one.
PIN_TO_CELL_GRAMMAR: Mapping[str, IntentGrammarEntry] = {
    PIN_TO_CELL_OBJECTIVE: IntentGrammarEntry(
        objective_family=PIN_TO_CELL_OBJECTIVE,
        keywords=("pin", "serving cell"),
        measurement_ref="measurement/serving-cell-min",
        default_operator=ComparisonOperator.EQUAL,
        default_unit=NCI_UNIT,
    ),
}

_IDENTITY = {
    "version": "1.0.0",
    "schema_version": ASSURANCE_SCHEMA_VERSION,
    "document_status": "NORMATIVE",
    "standard_mapping": {"a1p": "1.0", "e2sm-rc": "1.0", "o1": "1.0"},
}


class LiveDriverError(RuntimeError):
    """A live port did not supply something the run cannot proceed without."""


def _identity(contract_id: str) -> Dict[str, Any]:
    return {**_IDENTITY, "contract_id": contract_id}


def _quantity(value: float, unit: str, source: str) -> TypedQuantity:
    return TypedQuantity(value, unit, Provenance.EXPERIMENT_CONFIG, source)


def pin_utterance(target_nci: int, ue_scope_id: str) -> str:
    """The sentence an Operator types to pin the UE to *target_nci*.

    The cell identity precedes the scope token on purpose: the deterministic
    grammar takes the *first* number in the sentence as the drafted bound, and
    a UE label that contains a digit would otherwise be read as the cell.
    """
    return f"Pin the UE to serving cell {target_nci} nci for ueId={ue_scope_id}"


# --------------------------------------------------------------------------- #
# What the deployment is
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LiveTiming:
    """The observation geometry this deployment can actually deliver.

    Every number is a claim about the equipment, not a preference:

    ``cadence_ms``
        The KPM indication cadence the E2 subscription reports at.  The
        collector stamps it on every sample and the Kernel refuses a window
        whose samples disagree with it.
    ``window_width_ms`` / ``hold_ms``
        How long the pin has to hold before a verdict is admissible.
    ``freshness_bound_ms``
        How old the newest observation in a window may be when the Kernel
        evaluates it.  It has to cover the settle step *plus* the distance
        between the end of the last completed window and the evaluation
        instant, or a run that observed perfectly would still be reported
        ``STALE``.
    ``enforced_timeout_ms``
        The runtime timeout that makes the harm bound true.  It becomes the
        policy's ``actionDeadlineMs`` and ``rollbackTimeoutMs``
        (``assurance/contracts/actuation_request.py``).
    ``case_deadline_ms``
        The coordination case's own horizon.
    """

    cadence_ms: int = 1000
    window_width_ms: int = 3000
    hold_ms: int = 3000
    freshness_bound_ms: int = 4000
    enforced_timeout_ms: int = 10_000
    case_deadline_ms: int = 600_000

    def __post_init__(self) -> None:
        for name in (
            "cadence_ms",
            "window_width_ms",
            "hold_ms",
            "freshness_bound_ms",
            "enforced_timeout_ms",
            "case_deadline_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise LiveDriverError(f"{name} must be a positive integer")
        if self.window_width_ms % self.cadence_ms:
            raise LiveDriverError(
                "the window width must be a whole number of reporting cadences; "
                "a partial cadence at the end of a window is a gap nobody declared"
            )


@dataclass(frozen=True)
class LiveCellTopology:
    """How a live E2 node identity maps onto the cell a policy names.

    ``nb_id_to_nci`` comes from the deployment's own capability manifest
    (``topology.cells[].globalE2NodeId`` beside ``cellId.cId.ncI``), so the
    mapping is the deployment's, not this module's.  ``expected_epochs`` is the
    connection epoch each node was admitted at: an indication carrying a
    different epoch is from a *different* association and is discarded rather
    than read as the same node, which is the same fail-closed rule
    ``assurance/collector/o1col.py`` applies.
    """

    plmn: Mapping[str, str]
    nb_id_to_nci: Mapping[int, int]
    expected_epochs: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "plmn", dict(self.plmn))
        object.__setattr__(
            self, "nb_id_to_nci", {int(k): int(v) for k, v in self.nb_id_to_nci.items()}
        )
        object.__setattr__(
            self,
            "expected_epochs",
            {str(k): int(v) for k, v in self.expected_epochs.items()},
        )
        if set(self.plmn) != {"mcc", "mnc"}:
            raise LiveDriverError("plmn must carry exactly mcc and mnc")
        if not self.nb_id_to_nci:
            raise LiveDriverError("the topology names no cell")

    def cell_object(self, nci: int) -> Dict[str, Any]:
        """The frozen ``CellId`` shape for *nci* under this deployment's PLMN."""
        return {"plmnId": dict(self.plmn), "cId": {"ncI": int(nci)}}


@dataclass(frozen=True)
class LivePinToCellDeployment:
    """One live PIN_TO_CELL run's subject: which UE, from which cell to which."""

    home_nci: int
    target_nci: int
    ue_scope_id: str
    topology: LiveCellTopology
    r1_deployment: DeploymentBinding
    adapter_name: str = "r1"
    policy_type_id: str = POLICY_TYPE_ID

    def __post_init__(self) -> None:
        if self.home_nci == self.target_nci:
            raise LiveDriverError(
                "the source and target cells are the same; there is no steering "
                "effect for a readback to observe"
            )
        for nci in (self.home_nci, self.target_nci):
            if nci not in set(self.topology.nb_id_to_nci.values()):
                raise LiveDriverError(f"cell {nci} is outside the live topology")
        if not self.ue_scope_id:
            raise LiveDriverError("the UE scope label must be observed, not empty")

    @property
    def scope(self) -> Dict[str, str]:
        return {"ueId": str(self.ue_scope_id)}

    @property
    def baseline_config(self) -> Dict[str, str]:
        return {"servingCell": str(self.home_nci)}

    @property
    def applied_config(self) -> Dict[str, str]:
        return {"servingCell": str(self.target_nci)}


# --------------------------------------------------------------------------- #
# The contract family the epoch freezes
# --------------------------------------------------------------------------- #


def _serving_cell_measurement(
    contract_id: str,
    *,
    aggregation: Aggregation,
    deployment: LivePinToCellDeployment,
    timing: LiveTiming,
) -> MeasurementContract:
    """One end of the identity predicate pair.

    Both ends read the same counter and differ only in how they collapse the
    window.  The uncertainty parameter is exactly zero: an identity has no
    measurement error, and a non-zero margin would let ``EQUAL`` accept a
    neighbouring ``nCI``.
    """
    return MeasurementContract(
        **_identity(contract_id),
        counter_id=COUNTER_ID,
        scope_selector=deployment.scope,
        membership_snapshot=(deployment.ue_scope_id,),
        cadence_ms=timing.cadence_ms,
        window_width_ms=timing.window_width_ms,
        window_stride_ms=timing.window_width_ms,
        overlap=OverlapPolicy.DISJOINT,
        aggregation=aggregation,
        estimator=Estimator.EMPIRICAL_QUANTILE,
        minimum_entity_count=1,
        hold_ms=timing.hold_ms,
        gap_policy=GapPolicy.CONSERVATIVE_CHARGE,
        missing_interval_charge=_quantity(5.0, "ms", "plan/pin-missing-charge"),
        freshness_bound_ms=timing.freshness_bound_ms,
        clock_requirement=ClockRequirement.SYNCHRONISED_REQUIRED,
        uncertainty_rule=UncertaintyRule(
            "exact_identity", _quantity(0.0, NCI_UNIT, "calibration/serving-cell")
        ),
    )


def live_contract_set(
    deployment: LivePinToCellDeployment, timing: LiveTiming
) -> Dict[str, Any]:
    """The live PIN_TO_CELL case as a complete, cross-consistent family.

    Deliberately its own instance rather than a parameter of the hardware-free
    fixture: the two describe *different deployments* -- different cells,
    different UE scope, a different R1 endpoint and a different observation
    geometry -- and a single builder serving both would have to be told which
    it was on every line.  What they share is the shape, and that is the point
    of the comparison: the identity predicate pair, the one-point parameter
    space the frozen policy schema forces, and the official dynamic actuator
    path are the same on the testbed as on the fixture.
    """
    counter = CounterBinding(
        counter_id=COUNTER_ID,
        deployment_counter_name="observedServingCell",
        # The contracted effect readback itself, not a performance count.
        source=MeasurementSource.CONFIGURATION_READBACK,
        scope_keys=("ueId",),
        unit=NCI_UNIT,
        native_cadence_ms=timing.cadence_ms,
        deployment_binding_ref=deployment.r1_deployment.contract_id,
    )
    floor = _serving_cell_measurement(
        "measurement/serving-cell-min",
        aggregation=Aggregation.MIN,
        deployment=deployment,
        timing=timing,
    )
    ceiling = _serving_cell_measurement(
        "measurement/serving-cell-max",
        aggregation=Aggregation.MAX,
        deployment=deployment,
        timing=timing,
    )
    pinned = _quantity(float(deployment.target_nci), NCI_UNIT, "plan/pinned-cell")
    predicates = (
        TargetPredicate(
            "serving-cell-is-pinned-throughout",
            TypedConstraint(
                "measurement/serving-cell-min", ComparisonOperator.EQUAL, pinned
            ),
            description="no sample in the hold observed a lower cell identity",
        ),
        TargetPredicate(
            "serving-cell-is-pinned-only",
            TypedConstraint(
                "measurement/serving-cell-max", ComparisonOperator.EQUAL, pinned
            ),
            description="no sample in the hold observed a higher cell identity",
        ),
    )
    option = TargetOption(
        **_identity("option/pin-to-cell"),
        capability_ref="capability/ue-cell-steering",
        parameter_space={"servingCell": (str(deployment.target_nci),)},
    )
    target = TargetContract(
        **_identity("target/pin-to-cell"),
        objective_family=PIN_TO_CELL_OBJECTIVE,
        scope_selector=deployment.scope,
        predicates=predicates,
        options=(option,),
        hold_ms=timing.hold_ms,
    )
    vector = TargetVector(
        **_identity("vector/pin-to-cell"), ordered_target_refs=("target/pin-to-cell",)
    )
    release = TargetReleasePolicy(**_identity("release/pin-to-cell"))
    case_policy = CoordinationCasePolicy(
        **_identity("case-policy/pin-to-cell"),
        deadline_ms=timing.case_deadline_ms,
        max_trials=2,
        max_proposals=4,
        target_release_policy_ref="release/pin-to-cell",
        harm_contract_refs=("harm/pin-to-cell",),
    )
    watchdog = WatchdogContract(
        **_identity("watchdog/serving-cell"),
        watchdog_id="wd/serving-cell",
        trigger=TypedConstraint(
            "measurement/serving-cell-min", ComparisonOperator.EQUAL, pinned
        ),
        action=WatchdogAction.STOP_AND_ROLLBACK,
    )
    bound = CertifiedHarmBound(
        "bound/pin-to-cell",
        _quantity(20.0, "ms", "calibration/pin-bound"),
        _quantity(2.0, "ms", "calibration/pin-uncertainty"),
        _quantity(22.0, "ms", "calibration/pin-conservative"),
        "measurement/serving-cell-min#uncertainty",
        deployment.scope,
        timing.enforced_timeout_ms,
        ("calibration/pin-1",),
        "proof/pin-to-cell-v1",
    )
    harm = HarmContract(
        **_identity("harm/pin-to-cell"),
        harm_kind=HarmKind.TRIAL_INDUCED,
        scope_selector=deployment.scope,
        reserve=_quantity(100.0, "ms", "plan/pin-reserve"),
        bounds=(bound,),
        watchdogs=(watchdog,),
        missing_interval_charge=_quantity(5.0, "ms", "plan/pin-missing-charge"),
    )
    actuator = ActuatorBinding(
        **_identity("actuator/ue-cell-steering"),
        capability_ref="capability/ue-cell-steering",
        path=ActuatorPath.OFFICIAL_ORAN_DYNAMIC,
        policy_type_id=deployment.policy_type_id,
        service_model={"serviceModel": "E2SM-RC", "style": "3"},
        readback_measurement_ref="measurement/serving-cell-min",
        deployment_binding_ref=deployment.r1_deployment.contract_id,
    )
    capability = CapabilityManifest(
        **_identity("capability/ue-cell-steering"),
        capability_id="capability/ue-cell-steering",
        supported_objectives=(PIN_TO_CELL_OBJECTIVE,),
        constraints=(predicates[0].constraint,),
        actuator_refs=("actuator/ue-cell-steering",),
        measurement_refs=(
            "measurement/serving-cell-min",
            "measurement/serving-cell-max",
        ),
        interface_versions={"a1p": "1.0", "e2smRc": "1.0"},
    )
    composition = CompositionManifest(
        **_identity("composition/pin-to-cell"),
        composition_id="composition/pin-to-cell",
        capability_refs=("capability/ue-cell-steering",),
    )
    return {
        "counter": counter,
        "measurement_min": floor,
        "measurement_max": ceiling,
        "target": target,
        "vector": vector,
        "release": release,
        "case_policy": case_policy,
        "watchdog": watchdog,
        "harm": harm,
        "deployment": deployment.r1_deployment,
        "actuator": actuator,
        "capability": capability,
        "composition": composition,
    }


# --------------------------------------------------------------------------- #
# The live UE attribution stream
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LiveUeObservation:
    """One KPM format-3 UE attribution record, read as an identity and a cell.

    This is the whole live-identity story in one object: which UE the AMF is
    currently calling this, which AMF said so, which E2 node is reporting it,
    and therefore which cell it is on.
    """

    amf_ue_ngap_id: int
    gu_ami: Mapping[str, Any]
    serving_nci: int
    e2_node: str
    connection_epoch: int
    observed_at: str
    trace_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "gu_ami", json.loads(json.dumps(self.gu_ami)))

    def ue_id(self) -> Dict[str, Any]:
        """The frozen ``UeId`` this observation authorises a policy to name."""
        return {
            "guAmfUeNgapId": {
                "guAmI": json.loads(json.dumps(self.gu_ami)),
                "amfUeNgapId": int(self.amf_ue_ngap_id),
            }
        }


def _gu_ami(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Project a KPM ``guami`` onto the frozen ``GuAmI`` string encoding.

    The indication reports the four fields as integers; the frozen policy
    schema types them as fixed-width hexadecimal strings
    (``amfRegionId`` two digits, ``amfSetId`` three, ``amfPointer`` two).  The
    conversion is total and lossless, and a value that does not fit the frozen
    width is a refusal rather than a truncation.
    """
    try:
        mcc = int(raw["mcc"])
        mnc = int(raw["mnc"])
        digits = int(raw.get("mnc_digit_len", 2))
        region = int(raw["amf_region_id"])
        set_id = int(raw["amf_set_id"])
        pointer = int(raw["amf_pointer"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LiveDriverError("KPM guami is not a complete AMF identity") from exc
    if not (0 <= region <= 0xFF and 0 <= set_id <= 0x3FF and 0 <= pointer <= 0x3F):
        raise LiveDriverError("KPM guami does not fit the frozen GuAmI widths")
    return {
        "plmnId": {"mcc": f"{mcc:03d}", "mnc": f"{mnc:0{digits}d}"},
        "amfRegionId": f"{region:02X}".lower(),
        "amfSetId": f"{set_id:03X}".lower(),
        "amfPointer": f"{pointer:02X}".lower(),
    }


def kpm_node_nb_id(node: str) -> Optional[int]:
    """Read the decimal NB component of a deployment-bound KPM node key.

    Epoch membership still has to be checked by the caller. A missing,
    malformed or repeated NB component is not a usable node identity.
    """
    if not isinstance(node, str):
        return None
    fields = [part[3:] for part in node.split(";") if part.startswith("nb=")]
    if len(fields) != 1:
        return None
    value, separator, suffix = fields[0].partition("/")
    if (not separator or not value.isascii() or not value.isdecimal()
            or not suffix.isascii() or not suffix.isdecimal()):
        return None
    try:
        return int(value)
    except ValueError:
        return None


class KpmUeAttributionReader:
    """Read live UE-to-node attribution out of the KPM indication stream.

    The stream is supplied as a callable returning whatever JSONL lines have
    appeared since the previous call -- a file tail, a socket drain, a test
    list.  This class never opens it.

    Records are refused, not repaired: an indication whose connection epoch is
    not the one the deployment was admitted at belongs to a different E2
    association, an indication from a node outside the topology names a cell
    this run cannot reason about, and both are dropped with a count kept so a
    silent stream can be told from a rejected one.
    """

    def __init__(
        self,
        *,
        read_new_lines: Callable[[], Sequence[str]],
        topology: LiveCellTopology,
        history: int = 900,
    ) -> None:
        if history <= 0:
            raise LiveDriverError("the observation history must be positive")
        self._read_new_lines = read_new_lines
        self._topology = topology
        self._history = int(history)
        self._observations: List[LiveUeObservation] = []
        self._rejected = 0
        #: 왜 버렸는지.  2026-09-21: 여섯 가지 서로 다른 사유가 한 카운터에 합산돼,
        #: 이 수가 올라가도 **gNB 재기동으로 epoch 이 낡은 것인지 노드 이름이 안 맞는
        #: 것인지 구분할 수 없었다** -- 조치가 완전히 다른데도.  총계는 그대로 두고
        #: (읽는 쪽을 깨지 않으려고) 내역을 옆에 둔다.
        self._rejected_by_reason: "Counter[str]" = Counter()
        self._lines = 0

    # -- inspection --------------------------------------------------------

    @property
    def rejected_records(self) -> int:
        """Indications discarded for a stale epoch or an unknown node."""
        return self._rejected

    @property
    def rejected_by_reason(self) -> Mapping[str, int]:
        """버린 사유별 내역.  합은 `rejected_records` 와 같다.

        `epoch` 이 오르면 gNB 가 재기동해 연결 epoch 이 바뀐 것이고(재핀이 필요하다),
        `nbId` 는 노드 문자열과 nb_id 가 어긋난 것이며, `topology` 는 그 nb_id 가 이 판의
        토폴로지에 없다는 뜻이다.  `json`/`fields`/`ueEntry`/`ueFields` 는 입력이 깨진
        것이라 무해하다.
        """
        return dict(self._rejected_by_reason)

    @property
    def lines_read(self) -> int:
        return self._lines

    def observations(self) -> Tuple[LiveUeObservation, ...]:
        return tuple(self._observations)

    # -- the stream --------------------------------------------------------

    def refresh(self, *, amf_ue_ngap_id: Optional[int] = None) -> Tuple[LiveUeObservation, ...]:
        """Drain the stream once; return what it added, oldest first."""
        added: List[LiveUeObservation] = []
        for line in self._read_new_lines():
            self._lines += 1
            for observation in self._parse(line):
                if amf_ue_ngap_id is not None and observation.amf_ue_ngap_id != amf_ue_ngap_id:
                    continue
                added.append(observation)
        if added:
            self._observations.extend(added)
            self._observations.sort(key=lambda item: item.observed_at)
            if len(self._observations) > self._history:
                del self._observations[: len(self._observations) - self._history]
        return tuple(added)

    def latest(self, *, amf_ue_ngap_id: Optional[int] = None) -> Optional[LiveUeObservation]:
        for observation in reversed(self._observations):
            if amf_ue_ngap_id is None or observation.amf_ue_ngap_id == amf_ue_ngap_id:
                return observation
        return None

    def at_or_before(
        self, instant: str, *, lookback_ms: int, amf_ue_ngap_id: Optional[int] = None
    ) -> Optional[LiveUeObservation]:
        """The newest observation no later than *instant* and not older than
        *lookback_ms* before it.  ``None`` is a gap, and the caller reports it
        as one rather than carrying the previous value forward."""
        edge = parse_utc(instant)
        floor = edge - timedelta(milliseconds=int(lookback_ms))
        for observation in reversed(self._observations):
            if amf_ue_ngap_id is not None and observation.amf_ue_ngap_id != amf_ue_ngap_id:
                continue
            observed = parse_utc(observation.observed_at)
            if observed > edge:
                continue
            if observed < floor:
                return None
            return observation
        return None

    # -- internals ---------------------------------------------------------

    def _parse(self, line: str) -> Tuple[LiveUeObservation, ...]:
        text = line.strip()
        if not text:
            return ()
        try:
            record = json.loads(text)
        except (TypeError, ValueError):
            self._rejected += 1
            self._rejected_by_reason['json'] += 1
            return ()
        if not isinstance(record, Mapping) or record.get("event") != "kpm_indication":
            return ()
        entries = record.get("ues")
        if not isinstance(entries, list) or not entries:
            return ()
        node = record.get("e2_node")
        epoch = record.get("connection_epoch")
        nb_id = record.get("nb_id")
        received_us = record.get("recv_unix_us")
        if (
            not isinstance(node, str)
            or isinstance(epoch, bool)
            or not isinstance(epoch, int)
            or isinstance(nb_id, bool)
            or not isinstance(nb_id, int)
            or isinstance(received_us, bool)
            or not isinstance(received_us, int)
        ):
            self._rejected += 1
            self._rejected_by_reason['fields'] += 1
            return ()
        if self._topology.expected_epochs.get(node) != epoch:
            # gNB 가 재기동해 연결 epoch 이 올랐다.  재핀 전까지 이 노드의 지시는
            # **한 줄도** 통과하지 못하고, Kernel 에는 MissingInterval 조차 가지 않는다.
            self._rejected += 1
            self._rejected_by_reason['epoch'] += 1
            return ()
        if kpm_node_nb_id(node) != nb_id:
            self._rejected += 1
            self._rejected_by_reason['nbId'] += 1
            return ()
        nci = self._topology.nb_id_to_nci.get(int(nb_id))
        if nci is None:
            self._rejected += 1
            self._rejected_by_reason['topology'] += 1
            return ()
        observed_at = format_utc(
            datetime.fromtimestamp(received_us / 1_000_000, tz=timezone.utc)
        )
        trace_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        parsed: List[LiveUeObservation] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                self._rejected += 1
                self._rejected_by_reason['ueEntry'] += 1
                continue
            identifier = entry.get("amf_ue_ngap_id")
            guami = entry.get("guami")
            if (
                isinstance(identifier, bool)
                or not isinstance(identifier, int)
                or not isinstance(guami, Mapping)
            ):
                self._rejected += 1
                self._rejected_by_reason['ueFields'] += 1
                continue
            parsed.append(
                LiveUeObservation(
                    amf_ue_ngap_id=int(identifier),
                    gu_ami=_gu_ami(guami),
                    serving_nci=int(nci),
                    e2_node=node,
                    connection_epoch=int(epoch),
                    observed_at=observed_at,
                    trace_hash=trace_hash,
                )
            )
        return tuple(parsed)


# --------------------------------------------------------------------------- #
# The Measurement Collector
# --------------------------------------------------------------------------- #


class ServingCellCollector:
    """Report the UE's serving cell on the trial's own cadence grid.

    The Kernel windows samples from the instant the trial started observing, and
    a completed window needs observations spanning its full width at no more
    than the contracted cadence apart.  A source reporting on its own phase can
    never satisfy that, so this collector takes the anchor from the Kernel --
    through an injected reader of its reduced state -- and reports at
    ``anchor + k * cadence``.  Which is what a TS 32.435 PM job does: the
    granularity period's end instant is the timestamp, not the moment the
    counter happened to be sampled.

    A grid instant with no indication behind it within two cadences is emitted
    with a :class:`~assurance.collector.samples.MissingInterval` covering the
    interval it reports on.  Design section 8 forbids zero or last-value
    substitution for a gap; the value field carries zero because the envelope
    is numeric, and the missing interval is the authoritative marker the
    evaluator reads.
    """

    def __init__(
        self,
        *,
        reader: KpmUeAttributionReader,
        deployment: LivePinToCellDeployment,
        timing: LiveTiming,
        anchor: Callable[[], Optional[str]],
        amf_ue_ngap_id: int,
        clock_health: Callable[[Optional[LiveUeObservation], str], ClockHealth],
        source_id: str = "live-serving-cell",
        counter_id: Optional[str] = None,
        scope_snapshot: Optional[Mapping[str, str]] = None,
        cadence_ms: Optional[int] = None,
    ) -> None:
        self._reader = reader
        self._deployment = deployment
        self._timing = timing
        # An objective family names its own counter, its own sample scope and
        # its own cadence in the contracts the epoch freezes.  Defaulting to
        # the PIN_TO_CELL case keeps the Gate 3 call sites unchanged; supplying
        # them is how a second family reuses this collector without the
        # collector knowing which family it is serving.
        self._counter_id = counter_id or COUNTER_ID
        self._scope = dict(scope_snapshot) if scope_snapshot is not None else None
        self._cadence_ms = int(cadence_ms) if cadence_ms else int(timing.cadence_ms)
        self._anchor_of = anchor
        # int 이면 조립 시점에 얼고, callable 이면 매 poll 마다 다시 묻는다 --
        # `CorroboratedServingCellReadback` 이 쓰는 것과 **같은 계약**이다.
        # 2026-09-17: 되읽기는 그렇게 고쳐 놓고 **이 수집기만 int 로 남아 있었다**
        # (joint_runtime.py:707 이 `int(participant.identity.amf_ue_ngap_id)` 를 준다).
        # ue1 이 판 도중 재등록하면 수집기는 옛 번호를 계속 묻고, 그 구간의 칸이 비어
        # servingCell coverage 가 1.0 미만이 되며, 그러면 그 KPI 는 통째로 게시되지
        # 않는다 -- 실측 44% 의 시행이 그랬다.
        # [[a-momentary-gap-must-not-erase-a-usable-id]]: 한 경로를 고쳤으면 같은
        # 원리를 쓰는 다른 경로를 전부 찾아라.
        self._amf_of = amf_ue_ngap_id if callable(amf_ue_ngap_id) else None
        self._amf_ue_ngap_id = None if self._amf_of else int(amf_ue_ngap_id)
        self._clock_health_of = clock_health
        self._source_id = source_id
        self._sink: Optional[SampleSink] = None
        self._anchor: Optional[str] = None
        self._next_slot = 0
        self._sequence = 0
        self._clock_health = ClockHealth.UNKNOWN
        self._emitted: List[RawSample] = []

    # -- the frozen collector surface --------------------------------------

    def bind_sink(self, sink: SampleSink) -> None:
        if self._sink is not None:
            raise RuntimeError("a collector delivers to exactly one Kernel sink")
        self._sink = sink

    def _current_id(self) -> Optional[int]:
        """지금 이 역할이 답하는 번호, 없으면 마지막으로 통했던 번호.

        해석기가 순간적으로 답하지 못하는 것은 **재등록이 아니라 공백**이므로,
        읽던 번호를 지우지 않는다 (되읽기 쪽 `_current_id` 와 같은 규칙).
        """
        if self._amf_of is None:
            return self._amf_ue_ngap_id
        resolved = self._amf_of()
        if resolved is not None:
            self._amf_ue_ngap_id = int(resolved)
        return self._amf_ue_ngap_id

    def poll(self, *, now: str) -> Sequence[RawSample]:
        self._reader.refresh(amf_ue_ngap_id=self._current_id())
        if self._anchor is None:
            self._anchor = self._anchor_of()
            if self._anchor is None:
                # Nothing is observing yet.  Draining the stream was still the
                # right thing to do: the history it built is what lets the very
                # first grid instant be answered from an indication that
                # arrived before the trial started.
                return ()
        anchor = parse_utc(self._anchor)
        edge = parse_utc(now)
        produced: List[RawSample] = []
        while True:
            instant = anchor + timedelta(
                milliseconds=self._next_slot * self._cadence_ms
            )
            if instant > edge:
                break
            produced.append(self._sample(format_utc(instant)))
            self._next_slot += 1
        for sample in produced:
            self._clock_health = sample.clock_health
            if self._sink is not None:
                self._sink(sample)
        self._emitted.extend(produced)
        return tuple(produced)

    def clock_health(self) -> ClockHealth:
        return self._clock_health

    def restart_grid(self) -> None:
        """Take the next trial's anchor afresh.

        A case that runs several trials starts a new observation window each
        time; a grid carried over from the previous trial would put its
        instants on the old anchor and leave the new trial's first window
        short of a sample.
        """
        self._anchor = None
        self._next_slot = 0

    def scope_snapshot(self) -> Mapping[str, str]:
        return self._sample_scope()

    def _sample_scope(self) -> Dict[str, str]:
        base = self._scope if self._scope is not None else self._deployment.scope
        return {str(k): str(v) for k, v in base.items()}

    def describe_source(self) -> Mapping[str, Any]:
        return {
            "sourceId": self._source_id,
            "kind": "kpm-ue-attribution",
            "networkAccess": False,
            "counterId": self._counter_id,
        }

    # -- inspection --------------------------------------------------------

    def emitted(self) -> Tuple[RawSample, ...]:
        return tuple(self._emitted)

    @property
    def anchor(self) -> Optional[str]:
        return self._anchor

    # -- internals ---------------------------------------------------------

    def _sample(self, instant: str) -> RawSample:
        lookback = 2 * self._cadence_ms
        observation = self._reader.at_or_before(
            instant, lookback_ms=lookback, amf_ue_ngap_id=self._current_id()
        )
        sequence = self._sequence
        self._sequence += 1
        missing: Tuple[MissingInterval, ...] = ()
        if observation is None:
            value = 0
            trace_hash = content_hash(
                {"counterId": self._counter_id, "observedAt": instant, "gap": True}
            )
            start = format_utc(
                parse_utc(instant) - timedelta(milliseconds=self._cadence_ms)
            )
            missing = (
                MissingInterval(
                    start,
                    instant,
                    "no KPM UE attribution indication within two cadences",
                ),
            )
        else:
            value = float(observation.serving_nci)
            trace_hash = observation.trace_hash
        sample_id = content_hash(
            {
                "counterId": self._counter_id,
                "observedAt": instant,
                "sequence": sequence,
                "sourceId": self._source_id,
                "traceHash": trace_hash,
            }
        )
        return RawSample(
            sample_id=sample_id,
            counter_id=self._counter_id,
            value=TypedQuantity(value, NCI_UNIT, Provenance.MEASURED, sample_id),
            scope_snapshot=self._sample_scope(),
            observed_at=instant,
            cadence_ms=self._cadence_ms,
            clock_health=self._clock_health_of(observation, instant),
            trace_hash=trace_hash,
            sequence=sequence,
            missing_intervals=missing,
        )


def same_host_clock_health(
    observation: Optional[LiveUeObservation],
    instant: str,
    *,
    tolerance_ms: int,
) -> ClockHealth:
    """Clock health as an observation, not an assertion.

    The indication timestamps are stamped by a process on the host this
    collector runs on, from the same system clock, so "synchronised" here means
    the stream is live on *this* clock -- which is checkable, and is checked:
    an indication further from the reporting instant than the tolerance is
    reported ``UNKNOWN``, and the Kernel then answers ``CLOCK_UNHEALTHY``
    rather than judging a window on timestamps nobody stood behind.
    """
    if observation is None:
        return ClockHealth.UNKNOWN
    skew_ms = abs(
        (parse_utc(instant) - parse_utc(observation.observed_at)).total_seconds() * 1000
    )
    return (
        ClockHealth.SYNCHRONISED if skew_ms <= tolerance_ms else ClockHealth.UNKNOWN
    )


# --------------------------------------------------------------------------- #
# The contracted effect readback
# --------------------------------------------------------------------------- #


def serving_cell_projection(status: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """Project a policy status onto this deployment's configuration surface.

    ``assurance.gateway.r1_adapter.project_verified_readback`` reports the
    frozen ``observedServingCell`` object as it stands; the configuration
    surface a PIN_TO_CELL plan moves is a one-point axis whose values are the
    cell identities the frozen ``TargetOption`` parameter space names, and those
    are ``CId.ncI`` decimal strings.  This is that projection, and it keeps the
    ``VERIFIED``-only rule of the original: an acknowledgement carries no
    configuration digest.
    """
    aic = status.get("aicStatus") if isinstance(status, Mapping) else None
    if not isinstance(aic, Mapping):
        return None
    readback = aic.get("readback")
    if not isinstance(readback, Mapping) or readback.get("result") != "VERIFIED":
        return None
    observed = readback.get("observedServingCell")
    if not isinstance(observed, Mapping):
        return None
    cell = observed.get("cId")
    if not isinstance(cell, Mapping):
        return None
    nci = cell.get("ncI")
    if isinstance(nci, bool) or not isinstance(nci, int):
        return None
    return {"servingCell": str(nci)}


class CorroboratedServingCellReadback:
    """The contracted effect readback, from two independent observations.

    Before a policy exists there is nothing to poll, and the question is simply
    *where is this UE now*: the answer comes from the live UE attribution
    stream.  Once a policy is bound the question is *did this policy's episode
    put the UE where it said*, and the answer needs both halves:

    * the frozen status object reports ``readback.result == "VERIFIED"`` with an
      ``observedServingCell``, which is the producer's own contracted readback;
    * the KPM attribution stream, which no policy writes to, independently
      reports the UE on that same cell within the freshness bound.

    Either alone is refused.  A status without the stream is the acceptance the
    whole design refuses to call an effect; the stream without the status would
    be this component deciding an episode succeeded, which is the producer's
    call and not ours.  Disagreement is ``None`` -- the gateway's ``UNKNOWN`` --
    for as long as the contracted deadline allows, and then it stays ``None``.
    """

    def __init__(
        self,
        *,
        reader: KpmUeAttributionReader,
        status_poller: R1StatusPoller,
        amf_ue_ngap_id: int,
        now: Callable[[], str],
        monotonic_ms: Callable[[], int],
        sleep_ms: Callable[[int], None],
        cadence_ms: int,
        deadline_ms: int,
        freshness_bound_ms: int,
        corroboration_deadline_ms: int,
    ) -> None:
        self._reader = reader
        self._status_poller = status_poller
        # An int freezes the id at composition; a callable is asked again at every
        # read.  2026-09-16 attempts 130 and 144 died on the frozen form: a UE that
        # re-registers mid-sitting keeps answering to a new id, KPM carries it, and
        # every PREPARE still asks for the old one and is told "the contracted
        # readback did not produce an observation" -- two of those end the episode.
        self._amf_of = amf_ue_ngap_id if callable(amf_ue_ngap_id) else None
        self._amf_ue_ngap_id = None if self._amf_of else int(amf_ue_ngap_id)
        self._now = now
        self._monotonic_ms = monotonic_ms
        self._sleep_ms = sleep_ms
        self._cadence_ms = int(cadence_ms)
        self._deadline_ms = int(deadline_ms)
        self._freshness_bound_ms = int(freshness_bound_ms)
        # The stream reports once per measurement cadence, so corroboration is
        # a question of a few indications, not of the producer's whole action
        # deadline.  Keeping it separate is what stops the two halves summing
        # past the permit's own lease and turning a good handover into a
        # ``LEASE_EXPIRED`` stop.
        self._corroboration_deadline_ms = int(corroboration_deadline_ms)
        self._reads: List[Dict[str, Any]] = []

    @property
    def reads(self) -> Tuple[Mapping[str, Any], ...]:
        """Every readback this run took, for the evidence record."""
        return tuple(dict(entry) for entry in self._reads)

    def __call__(
        self,
        *,
        scope: Mapping[str, Any],
        transaction_id: str,
        policy_id: Optional[str],
    ) -> Optional[Mapping[str, Any]]:
        if policy_id is None:
            observed = self._observed_now()
            if observed is None:
                # The baseline read waited for nothing, and a UE that keeper
                # revives mid-sitting (``UE_ZOMBIE_RESTART_DURING_EPISODE``)
                # leaves no observation at all for a few seconds -- under any
                # id, so following the role does not help.  Measured over 84
                # such revivals (ops/overnight/keeper.out, 2026-09-18): median
                # 10.0 s, p90 12.0 s, p99 21.0 s.  One look into that gap read
                # UNKNOWN and killed the trial; attempt 032251 lost two that
                # way while the radio was fine.  The corroboration path below
                # already waits -- only this one did not.  ``deadline_ms`` is
                # the R1 polling deadline the binding already contracts
                # (20 s), which covers 83 of those 84 gaps.
                # Counted in polls, not milliseconds: an injected clock that
                # does not advance would make an elapsed-time loop spin forever,
                # and a hermetic test drives exactly such a clock.
                polls = int(self._deadline_ms) // max(1, int(self._cadence_ms))
                for _ in range(polls):
                    if observed is not None:
                        break
                    self._sleep_ms(self._cadence_ms)
                    observed = self._observed_now()
            return self._record(
                transaction_id, policy_id, observed, "stream-only"
            )
        # The producer's half first, on its own contracted deadline.  It stops
        # by itself at a terminal episode without a verified readback, so a
        # quarantined episode costs one GET rather than a full window of them.
        status_cell = self._status_poller.readback(
            scope=scope, transaction_id=transaction_id, policy_id=policy_id
        )
        if status_cell is None:
            return self._record(
                transaction_id,
                policy_id,
                None,
                "no verified readback within the contracted deadline",
            )
        # Then the independent half.  The stream may lag the producer's readback
        # by an indication or two, so it is given the same deadline before the
        # disagreement is reported as one.
        started = self._monotonic_ms()
        while True:
            if self._observed_now() == status_cell:
                return self._record(
                    transaction_id, policy_id, status_cell, "status-and-stream"
                )
            remaining = self._corroboration_deadline_ms - (
                self._monotonic_ms() - started
            )
            if remaining <= 0:
                return self._record(
                    transaction_id,
                    policy_id,
                    None,
                    "status verified but the stream did not corroborate",
                )
            self._sleep_ms(min(self._cadence_ms, remaining))

    # -- internals ---------------------------------------------------------

    def _current_id(self) -> Optional[int]:
        """The id this role answers to now, or the last one that worked.

        A resolver that cannot answer (the role is briefly unaddressable) keeps the
        previous id rather than reading nothing at all: a gap is not a re-registration
        and the caller's own UNKNOWN already covers it.
        """
        if self._amf_of is None:
            return self._amf_ue_ngap_id
        resolved = self._amf_of()
        if resolved is not None:
            self._amf_ue_ngap_id = int(resolved)
        return self._amf_ue_ngap_id

    def _observed_now(self) -> Optional[Mapping[str, Any]]:
        current = self._current_id()
        if current is None:
            return None
        self._reader.refresh(amf_ue_ngap_id=current)
        observation = self._reader.at_or_before(
            self._now(),
            lookback_ms=self._freshness_bound_ms,
            amf_ue_ngap_id=current,
        )
        if observation is None:
            return None
        return {"servingCell": str(observation.serving_nci)}

    def _record(
        self,
        transaction_id: str,
        policy_id: Optional[str],
        observed: Optional[Mapping[str, Any]],
        detail: str,
    ) -> Optional[Mapping[str, Any]]:
        # 2026-09-18: 읽기가 **왜** 빈손인지 남긴다.  `KpmUeAttributionReader` 는
        # `nb_id` 가 토폴로지·epoch 과 안 맞는 지시를 조용히 버리고 `rejected_records`
        # 로만 세는데, 그 수가 어디에도 안 실려서 "the contracted readback did not
        # produce an observation" 이 **스트림이 비었는지 버려졌는지** 구분되지 않았다.
        # 2026-09-17 밤에 조종(셀 이동) 시행의 되읽기 실패율이 31% 대 7% (p=0.0003)
        # 인 것까지 왔는데 기전 확정이 여기서 막혔다.  침묵을 성공으로도 실패로도
        # 읽지 않으려면 센 것을 실어야 한다.
        reader = self._reader
        self._reads.append(
            {
                "at": self._now(),
                "transactionId": transaction_id,
                "policyId": policy_id,
                "observed": dict(observed) if observed is not None else None,
                "detail": detail,
                "readerLinesRead": getattr(reader, "lines_read", None),
                "readerRejectedRecords": getattr(reader, "rejected_records", None),
                # 총계만 남기면 `epoch`(재핀 필요)인지 `json`(무해)인지 사후에 알 수 없다.
                "readerRejectedByReason": dict(
                    getattr(reader, "rejected_by_reason", {}) or {}),
            }
        )
        return observed


# --------------------------------------------------------------------------- #
# The clock
# --------------------------------------------------------------------------- #


class InjectedClock:
    """Wall-clock time for a live run, still supplied from outside.

    :class:`~assurance.vertical.SteppingClock` is the deterministic clock a
    replay needs; this is its live counterpart, and it is the same shape --
    callable for *now*, ``advance`` for the next observation instant -- so the
    runtime boundary does not know which one it is holding.  It reads no clock
    itself: ``now`` and ``sleep_ms`` are injected, which is what keeps this
    module inside the seam and lets a test drive a whole live-shaped run
    without waiting for it.

    ``advance`` is drift-compensated against the injected monotonic source: a
    reporting grid that slipped by the cost of each sleep would drift out of
    the cadence the measurement contract declares.
    """

    def __init__(
        self,
        *,
        now: Callable[[], str],
        monotonic_ms: Callable[[], int],
        sleep_ms: Callable[[int], None],
    ) -> None:
        self._now = now
        self._monotonic_ms = monotonic_ms
        self._sleep_ms = sleep_ms
        self._target_ms = monotonic_ms()

    def __call__(self) -> str:
        return self._now()

    def advance(self, milliseconds: int) -> str:
        if milliseconds < 0:
            raise ValueError("a clock does not run backwards")
        self._target_ms += int(milliseconds)
        remaining = self._target_ms - self._monotonic_ms()
        if remaining > 0:
            self._sleep_ms(remaining)
        else:
            # The caller already spent longer than the tick asked for; carry the
            # grid forward from now rather than firing a burst of catch-up
            # polls at a stream that reports once per cadence.
            self._target_ms = self._monotonic_ms()
        return self._now()


# --------------------------------------------------------------------------- #
# The assembled runtime
# --------------------------------------------------------------------------- #


@dataclass
class LivePinToCellRuntime:
    """Everything one live PIN_TO_CELL case is made of, already wired."""

    deployment: LivePinToCellDeployment
    timing: LiveTiming
    contracts: Mapping[str, Any]
    kernel: AssuranceKernel
    gateway: TokenBoundWriteGateway
    adapter: Any
    collector: ServingCellCollector
    readback: CorroboratedServingCellReadback
    reader: KpmUeAttributionReader
    identity: LiveUeObservation
    clock: InjectedClock
    path: VerticalPath
    case_id: str
    cell_id: str
    epoch: Any
    event_store: MemoryEventStore = field(repr=False, default=None)

    def candidate_id(self) -> str:
        return self.kernel.current_catalog().candidates[0].candidate_id

    def utterance(self) -> str:
        return pin_utterance(self.deployment.target_nci, self.deployment.ue_scope_id)


def _confirmation_for(contract: Any, *, event_id: str, at: str) -> ConfirmationRecord:
    return ConfirmationRecord(
        confirmed_object_type=type(contract).__name__,
        confirmed_content_hash=contract_content_hash(contract),
        event_id=event_id,
        timestamp=at,
        action=ConfirmationAction.CONFIRM_AND_START,
    )


def build_live_pin_to_cell_runtime(
    *,
    deployment: LivePinToCellDeployment,
    timing: LiveTiming,
    binding: Any,
    policy_port: Any,
    policy_builder_factory: Callable[
        [AssuranceKernel, Mapping[str, Any]], Callable[[Mapping[str, Any]], Dict[str, Any]]
    ],
    reader: KpmUeAttributionReader,
    identity: LiveUeObservation,
    now: Callable[[], str],
    monotonic_ms: Callable[[], int],
    sleep_ms: Callable[[int], None],
    case_id: str,
    cell_id: str = "cell/pin-to-cell",
) -> LivePinToCellRuntime:
    """Wire one live case: contracts frozen, ports bound, nothing started.

    Every live thing is a parameter.  ``policy_port`` is the real R1 consumer,
    ``policy_builder_factory`` returns the real schema-validating translator,
    and ``reader`` is already draining the real indication stream -- all three
    are constructed by the composition root outside this package, because that
    is the only place allowed to import a transport.

    ``identity`` is the UE observation the composition root took *from that same
    reader* moments ago, which is why the reader is passed in rather than
    created here: the history that answered "which UE, on which cell" is the
    same history the first instant of the observation window is answered from.

    The builder arrives as a *factory* because the values it puts in a policy
    body are the frozen epoch's, so it cannot exist until the epoch does, and
    the epoch cannot be frozen until the Kernel is built on a gateway that
    already has the adapter the builder belongs to.  Binding it late resolves
    that honestly: the indirection below refuses rather than building a body
    from contracts nothing has frozen yet.
    """
    if str(identity.amf_ue_ngap_id) != deployment.ue_scope_id:
        raise LiveDriverError(
            "the observed UE is not the one this deployment is scoped to"
        )
    amf_ue_ngap_id = int(identity.amf_ue_ngap_id)
    clock = InjectedClock(now=now, monotonic_ms=monotonic_ms, sleep_ms=sleep_ms)
    contracts = live_contract_set(deployment, timing)
    # Keep the history draining from here on: the first grid instant of the
    # observation window is answered from an indication that arrived *before*
    # the trial started observing.
    reader.refresh(amf_ue_ngap_id=amf_ue_ngap_id)

    status_poller = R1StatusPoller(
        policy_port,
        cadence_ms=binding.r1.cadence_ms,
        deadline_ms=binding.r1.deadline_ms,
        monotonic_ms=monotonic_ms,
        sleep_ms=sleep_ms,
        # The same projection the adapter uses.  Two projections would mean two
        # answers to "which configuration is live", and the corroboration below
        # would compare a cell object with a cell identity and never agree.
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
        freshness_bound_ms=timing.freshness_bound_ms,
        corroboration_deadline_ms=timing.freshness_bound_ms,
    )
    bound: Dict[str, Callable[[Mapping[str, Any]], Dict[str, Any]]] = {}

    def policy_builder(command: Mapping[str, Any]) -> Dict[str, Any]:
        if "build" not in bound:
            raise LiveDriverError(
                "no epoch is frozen yet; there is nothing for a policy body to "
                "derive its behaviour-bearing values from"
            )
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
    # 2026-09-23 audit: the 09-19 hand-back (pin the baseline cell, see the UE
    # arrive, then DELETE) was set on the joint route alone, so this route's
    # rollback still DELETEd and left the UE on the target cell (boards 093728 /
    # 110539).  The readback above answers ``policy_id=None`` from the KPM stream,
    # which the independent-counter reads need.  Producer refusals (400/404/409)
    # are classified by the adapter from the status ``R1Refusal`` carries.
    adapter.restore_by_handover = True
    gateway = TokenBoundWriteGateway(
        adapters={deployment.adapter_name: adapter},
        # For a steering objective the safe thing to do with a UE is leave it
        # where it was, so the contracted safe state *is* the baseline.
        safe_state=deployment.baseline_config,
        journal=InMemoryTransactionJournal(),
        clock=clock,
    )
    event_store = MemoryEventStore()
    kernel = AssuranceKernel(
        event_store=event_store,
        reducer=KernelReducer(),
        write_gateway=gateway,
        measurement_collector=None,
    )
    epoch = _admit_and_freeze(kernel, contracts, clock)
    _open_case(kernel, contracts, clock, case_id=case_id, cell_id=cell_id)
    bound["build"] = policy_builder_factory(kernel, contracts)

    collector = ServingCellCollector(
        reader=reader,
        deployment=deployment,
        timing=timing,
        anchor=lambda: _observation_anchor(kernel, case_id),
        amf_ue_ngap_id=amf_ue_ngap_id,
        clock_health=lambda observation, instant: same_host_clock_health(
            observation, instant, tolerance_ms=2 * timing.cadence_ms
        ),
    )
    path = VerticalPath(
        kernel=kernel,
        gateway=gateway,
        deployment=VerticalDeployment(
            adapter_name=deployment.adapter_name,
            scope=deployment.scope,
            baseline_config=deployment.baseline_config,
        ),
        case_id=case_id,
        clock=clock,
        coordinator=None,
        collector=collector,
    )
    return LivePinToCellRuntime(
        deployment=deployment,
        timing=timing,
        contracts=contracts,
        kernel=kernel,
        gateway=gateway,
        adapter=adapter,
        collector=collector,
        readback=readback,
        reader=reader,
        identity=identity,
        clock=clock,
        path=path,
        case_id=case_id,
        cell_id=cell_id,
        epoch=epoch,
        event_store=event_store,
    )


def _observation_anchor(kernel: AssuranceKernel, case_id: str) -> Optional[str]:
    """The instant this case's trial started observing, read from the Kernel.

    Read rather than remembered: the observation window belongs to the Kernel's
    own trial record, and a collector that anchored on its own idea of when the
    window opened would be reporting against a grid nobody evaluated.
    """
    anchors = [
        trial.get("observationStartedAt")
        for trial in kernel.reduced_state()["trials"].values()
        if trial.get("caseId") == case_id and trial.get("observationStartedAt")
    ]
    return max(anchors) if anchors else None


def _admit_and_freeze(
    kernel: AssuranceKernel, contracts: Mapping[str, Any], clock: InjectedClock
) -> Any:
    now = clock()
    vector_confirmation = _confirmation_for(
        contracts["vector"], event_id="confirm/live-pin-vector", at=now
    )
    policy_confirmation = _confirmation_for(
        contracts["case_policy"], event_id="confirm/live-pin-policy", at=now
    )
    for key in (
        "counter",
        "measurement_min",
        "measurement_max",
        "target",
        "release",
        "watchdog",
        "harm",
        "actuator",
        "capability",
        "composition",
    ):
        kernel.admit_contract(contracts[key], confirmation=None, now=clock())
    kernel.admit_contract(
        contracts["vector"], confirmation=vector_confirmation, now=clock()
    )
    kernel.admit_contract(
        contracts["case_policy"], confirmation=policy_confirmation, now=clock()
    )
    kernel.admit_deployment(contracts["deployment"], now=clock())
    return kernel.freeze_epoch(confirmation=vector_confirmation, now=clock())


def _open_case(
    kernel: AssuranceKernel,
    contracts: Mapping[str, Any],
    clock: InjectedClock,
    *,
    case_id: str,
    cell_id: str,
) -> None:
    catalog = kernel.current_catalog()
    kernel.open_case(
        case_id=case_id,
        policy=contracts["case_policy"],
        active_vector="target/pin-to-cell",
        usable_reserve={
            "harm/pin-to-cell": _quantity(
                100.0, "ms", "plan/pin-reserve"
            ).to_canonical_dict()
        },
        reserve_per_trial={
            "harm/pin-to-cell": _quantity(
                20.0, "ms", "plan/pin-per-trial"
            ).to_canonical_dict()
        },
        evidence_cells=(
            EvidenceCell(
                cell_id=cell_id,
                target_ref="target/pin-to-cell",
                candidate_semantic_hash=catalog.candidates[0].semantic_hash,
                status=EvidenceCellStatus.OPEN,
                required_independent_contributions=1,
            ),
        ),
        now=clock(),
    )
