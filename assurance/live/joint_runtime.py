"""One live Kernel case over a joint intent set, wired for many trials.

:func:`assurance.live.objective_runtime.build_live_objective_runtime` wires one
family, one UE, one candidate.  This module wires the joint bundle
:mod:`assurance.objectives.joint` composes: one epoch whose catalog is the
product of every action axis, one Write Gateway with **one adapter per
scoped axis**, one collector per UE-scoped counter, and a runtime that can
open trial after trial on the same case until the Kernel's own trial cap ends
it.  The seam is the one every live module keeps: no transport, no clock and
no model client are created here; they are injected.

Three things make many-axis, many-trial operation possible, and each is a
small wrapper rather than a change to the Kernel or the gateway:

``_AxisReadback`` / ``_axis_projection``
    A steering adapter reads back ``{"servingCell": nci}``.  In a joint
    surface the axis is ``servingCell@<ue>``, so the adapter's readback and
    status projection are renamed onto its own axis.  The gateway then merges
    one axis per participant into the whole surface, exactly as it merges a
    steering adapter with a cap adapter today.

``BaselineAwareAdapter``
    A candidate may leave an axis at its baseline -- "do not cap this UE",
    "leave this UE on its cell".  The Kernel still requires the plan to name
    every frozen parameter, so the step exists; this wrapper answers it
    without a write, because a steering policy to the cell a UE is already on
    and a cap of zero PRB are both requests the producers refuse.  Its
    readback is the real one: the axis is verified by observation like any
    other, only nothing was sent.

``JointLiveRuntime.run_candidate``
    ``begin_trial -> observe -> conclude_trial`` for one named candidate,
    returning the Kernel's report and the per-axis verification the board
    derives an action state from.  Calling it again opens the next trial on
    the same case; the Kernel refuses when its budget is spent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from assurance.collector.samples import RawSample
from assurance.contracts.ledgers import EvidenceCell
from assurance.coordination.board import ActionVerification
from assurance.core.axes import EvidenceCellStatus
from assurance.core.states import StopReason
from assurance.gateway.gateway import TokenBoundWriteGateway
from assurance.gateway.journal import InMemoryTransactionJournal
from assurance.gateway.live import R1StatusPoller, build_live_r1_adapter
from assurance.gateway.commands import GatewayOperation
from assurance.gateway.write_gateway import GatewayOutcome, GatewayResult
from assurance.kernel.event_store import FileEventStore, MemoryEventStore
from assurance.kernel.kernel import AssuranceKernel
from assurance.kernel.reducer import KernelReducer
from assurance.live.objective_runtime import (
    _CounterGridCollector, _MultiCounterCollector, _admit_and_freeze,
    _live_counter_loaders, _observation_anchor, bundle_geometry,
)
from assurance.live.pin_to_cell_driver import (
    CorroboratedServingCellReadback, InjectedClock, KpmUeAttributionReader,
    LiveDriverError, LiveUeObservation, ServingCellCollector, same_host_clock_health,
    serving_cell_projection,
)
from assurance.objectives.joint import JointComposition, steering_axis
from assurance.vertical import TrialReport, VerticalDeployment, VerticalPath

__all__ = [
    "BaselineAwareAdapter",
    "JointLiveRuntime",
    "SteeringParticipantSpec",
    "SupplementaryParticipantSpec",
    "build_joint_live_runtime",
]

def cell_power_first(declarations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Order the plan's axes: cell TX attenuation, then everything else in declared order.

    The producer writes and restores a cell's attenuation through a UE-addressed E2 RC
    header, so it needs a UE on that cell; steering is declared next, UE-scoped axes last.
    Reverse rollback unwinds the list backwards (see build_joint_live_runtime).
    """
    return sorted(declarations, key=lambda entry: 0 if str(entry["axis"]).startswith("txAttenuationDb@") else 1)


def _builder_following_the_ue(participant: Any, kernel: Any, joint: Any) -> Any:
    """A policy builder that is rebuilt when the role's AMF id changes.

    The readback has followed a re-registration since 2026-09-16; the *body* was
    deliberately left frozen so a trial's writes all name one UE.  A rollback is
    the one place where that costs the bed: 2026-09-20 board 123728 steered ue1
    away, ue1 re-registered (431 -> 434) before the hand-back, the hand-back
    named the dead id, the radio did nothing, and the Kernel locked the trial
    down with "readback did not produce an observation".  Following the id here
    keeps every write on the UE the role actually is; a role without a resolver
    (a numeric label, a test double) behaves exactly as before.
    """
    build = participant.policy_builder_factory(kernel, joint, participant)
    if participant.amf_of is None:
        return build
    state = {"id": participant.amf_of(), "build": build}

    def build_for(command: Mapping[str, Any]) -> Dict[str, Any]:
        current = participant.amf_of()
        if current is not None and current != state["id"]:
            state["build"] = participant.policy_builder_factory(kernel, joint, participant)
            state["id"] = current
        return state["build"](command)

    return build_for


def _resolved_or_pinned(participant: Any) -> Any:
    """The steering readback's id source: the live resolver, pinned id as fallback.

    Returns the plain integer when the participant carries no resolver, so a
    composition that never opted into role resolution behaves exactly as before.
    """
    pinned = int(participant.identity.amf_ue_ngap_id)
    if participant.amf_of is None:
        return pinned

    # 2026-09-22: 공백에 **조립 시점** id 로 떨어지면 두 가지를 동시에 잃는다.  하나는
    # 쓰기와의 일치다 -- `_role_following_builder` 의 `build_for` 는 공백에 마지막으로
    # 따라간 id 를 유지하므로, 쓰기는 68, 되읽기는 61 을 가리켜 갈라진다.  다른 하나는
    # 관측 자체다: 재등록한 UE 의 옛 번호는 죽어 있어 아무 관측도 내지 않는다.  AMF 가
    # 한 case 안에서 번호를 되돌리는 일은 없으니, 마지막으로 확인된 번호가 언제나 pinned
    # 보다 참에 가깝다.  "공백이 쓸 수 있는 번호를 지우면 안 된다."
    last = {"id": pinned}

    def _now(_of=participant.amf_of, _last=last):
        resolved = _of()
        if resolved is not None:
            _last["id"] = int(resolved)
        return _last["id"]
    return _now




SERVING_CELL_COUNTER = "UE.ServingCell"


# --------------------------------------------------------------------------- #
# adapters
# --------------------------------------------------------------------------- #


def _axis_projection(axis: str) -> Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]]:
    """The steering status projection, renamed onto one scoped axis."""

    def project(status: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        observed = serving_cell_projection(status)
        if observed is None:
            return None
        return {axis: observed["servingCell"]}

    return project


class _AxisReadback:
    """A steering readback renamed onto one scoped axis."""

    def __init__(self, inner: Any, axis: str) -> None:
        self._inner = inner
        self.axis = axis

    @property
    def reads(self) -> Tuple[Mapping[str, Any], ...]:
        return getattr(self._inner, "reads", ())

    def __call__(self, *, scope: Mapping[str, Any], transaction_id: str,
                 policy_id: Optional[str]) -> Optional[Mapping[str, Any]]:
        observed = self._inner(scope=scope, transaction_id=transaction_id, policy_id=policy_id)
        if observed is None:
            return None
        if "servingCell" in observed:
            return {self.axis: observed["servingCell"]}
        return dict(observed)


class _SharedAttributionReader:
    """One attribution reader shared by every UE's collector and readback.

    :meth:`KpmUeAttributionReader.refresh` filters the lines it drains to one
    UE and discards the rest, which is right for one UE and wrong for many:
    whichever collector polled first would eat the other UEs' indications.
    This facade refreshes *unfiltered* on every call, so the history holds
    every UE, and every per-UE read then finds its own records.
    """

    def __init__(self, inner: KpmUeAttributionReader) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def refresh(self, *, amf_ue_ngap_id: Optional[int] = None) -> Tuple[LiveUeObservation, ...]:
        added = self._inner.refresh(amf_ue_ngap_id=None)
        if amf_ue_ngap_id is None:
            return added
        return tuple(item for item in added if item.amf_ue_ngap_id == int(amf_ue_ngap_id))


class _LazyCellIds(dict):
    """``candidate id -> evidence cell id`` for any point of the frozen domain."""

    def __missing__(self, candidate_id: str) -> str:
        return f"cell/joint/{candidate_id}"


class BaselineAwareAdapter:
    """Answer a step that leaves its axis at baseline without a write.

    Everything else is delegated to the wrapped adapter, attribute reads
    included, so the evidence writer sees the wrapped adapter's bindings and
    journals.
    """

    def __init__(self, inner: Any, *, axis: str,
                 baseline_of: Callable[[], Mapping[str, Any]],
                 name_baseline_on_halt: bool = False) -> None:
        self._inner = inner
        self.axis = axis
        self._baseline_of = baseline_of
        #: A steering HALT names no axis, but restoring a handover needs the
        #: cell to hand back to (``R1Adapter.restore_by_handover``).
        self._name_baseline_on_halt = name_baseline_on_halt
        self._skipped: Dict[str, bool] = {}
        self.skipped_operations: List[Dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def actuator_path(self) -> Any:
        return self._inner.actuator_path

    @property
    def hosts_watchdogs(self) -> bool:
        return bool(getattr(self._inner, "hosts_watchdogs", False))

    @property
    def inner(self) -> Any:
        return self._inner

    def _at_baseline(self, command: Mapping[str, Any]) -> bool:
        if str(command.get("axis")) != self.axis:
            return False
        baseline = self._baseline_of().get(self.axis)
        return baseline is not None and str(command.get("value")) == str(baseline)

    def dispatch(self, *, token: Any, command: Mapping[str, Any]) -> GatewayResult:
        operation = GatewayOperation(command["operation"])
        transaction_id = str(token.transaction_id)
        if operation in (GatewayOperation.VALIDATE, GatewayOperation.APPLY) and self._at_baseline(command):
            if operation is GatewayOperation.APPLY:
                self._skipped[transaction_id] = True
            self.skipped_operations.append({
                "transactionId": transaction_id, "operation": operation.value,
                "axis": self.axis, "value": str(command.get("value"))})
            return GatewayResult(
                outcome=GatewayOutcome.ACKED,
                evidence_refs=(f"{getattr(self._inner, 'name', 'adapter')}:baseline:{self.axis}",),
                detail=f"{self.axis} stays at its baseline; nothing written")
        if operation is GatewayOperation.FINALIZE and self._skipped.get(transaction_id):
            # Nothing was written, so there is no policy to finalize; the axis is
            # verified the way it always is -- by reading it back.
            read = {**dict(command), "operation": GatewayOperation.READ.value}
            return self._inner.dispatch(token=token, command=read)
        if operation in (GatewayOperation.UNDO, GatewayOperation.HALT) and self._skipped.get(transaction_id):
            return GatewayResult(
                outcome=GatewayOutcome.ACKED,
                evidence_refs=(f"{getattr(self._inner, 'name', 'adapter')}:baseline:{self.axis}",),
                detail=f"{self.axis} was never moved; nothing to withdraw")
        if operation is GatewayOperation.HALT and self._name_baseline_on_halt:
            baseline = self._baseline_of().get(self.axis)
            if baseline is not None:
                command = {**dict(command), "axis": self.axis, "value": str(baseline)}
        return self._inner.dispatch(token=token, command=command)


# --------------------------------------------------------------------------- #
# participants
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SteeringParticipantSpec:
    """One UE's steering actuator, as the composition root hands it in."""

    ue_id: str
    identity: LiveUeObservation
    policy_port: Any
    #: ``factory(kernel, joint, participant) -> policy builder`` bound after
    #: the epoch is frozen, like the single-family runtime's.
    policy_builder_factory: Callable[[AssuranceKernel, JointComposition, "SteeringParticipantSpec"], Callable[[Mapping[str, Any]], Dict[str, Any]]]
    adapter_key: str = ""
    #: A hermetic stand-in for the R1 adapter; ``None`` builds the live one.
    adapter_override: Optional[Any] = None
    #: The id this role answers to *now*, asked again at every readback.
    #: 2026-09-16 attempts 130 and 144 both ended EXECUTION_FAILURE because the
    #: steering readback held the id captured when the participant was composed:
    #: in 144 ue1 re-registered from 24 to 26 at about +180 s, KPM carried 26
    #: continuously from +210 s, and the trials at +260 s and +269 s still asked
    #: for 24 and were answered "the contracted readback did not produce an
    #: observation".  ``None`` keeps the frozen id, which is what a hermetic
    #: test and a numeric-labelled sitting both want.
    amf_of: Optional[Callable[[], Optional[int]]] = None
    #: Where this adapter records what it issued.  The supplementary axes have
    #: had one since they were built; steering never did, so on 2026-09-17 the
    #: producer's ``503 AIC_INTERNAL_ERROR`` could be counted but not *timed* --
    #: the one axis that fails is the one axis with no operation record.  Only
    #: the operation journal belongs here: a binding journal would make the
    #: steering adapter durable across cases, which the joint composition does
    #: not want.
    operation_journal: Optional[Any] = None

    @property
    def axis(self) -> str:
        return steering_axis(self.ue_id)

    @property
    def key(self) -> str:
        return self.adapter_key or f"r1-steer@{self.ue_id}"


@dataclass(frozen=True)
class SupplementaryParticipantSpec:
    """One scoped supplementary axis (cap, priority) with its ready adapter."""

    action_id: str
    axis: str
    adapter_key: str
    adapter: Any
    #: The participant's own configuration-counter reader, for the evidence.
    counter_reader: Any = None
    describe: Optional[Callable[[], Mapping[str, Any]]] = None


@dataclass
class JointLiveRuntime:
    """One joint case, wired end to end, that can run trial after trial."""

    joint: JointComposition
    kernel: AssuranceKernel
    gateway: TokenBoundWriteGateway
    path: VerticalPath
    collector: Any
    reader: KpmUeAttributionReader
    clock: InjectedClock
    case_id: str
    epoch: Any
    event_store: MemoryEventStore
    adapters: Mapping[str, Any]
    steering: Tuple[SteeringParticipantSpec, ...]
    supplementary: Tuple[SupplementaryParticipantSpec, ...]
    cell_ids: Mapping[str, str]
    geometry: Any
    trials: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def bundle(self) -> Any:
        return self.joint.bundle

    def epoch_hash(self) -> str:
        return self.path.epoch_hash()

    def catalog_entries(self) -> Tuple[Any, ...]:
        """Every point -- linear in the cardinality; prefer the three below."""
        return self.path.catalog_view()

    def candidate_entry(self, candidate_id: str) -> Any:
        return self.path.candidate_entry(candidate_id)

    def find_candidate(self, parameters: Mapping[str, Any]) -> Any:
        return self.path.find_candidate(parameters)

    def catalog_size(self) -> int:
        return self.path.catalog_size()

    def spent_count(self) -> int:
        return self.path.spent_count()

    def spent_ids(self) -> set:
        return self.path.spent_ids()

    def trials_used(self) -> int:
        state = self.kernel.reduced_state()
        return sum(1 for trial in state["trials"].values() if trial["caseId"] == self.case_id)

    def live_baseline(self) -> Mapping[str, Any]:
        return dict(self.path._live_baseline)  # noqa: SLF001 - the gateway's belief

    # -- one trial -------------------------------------------------------- #

    def _feed_to_fresh_instant(self, cadence_ms: int, step_ms: int = 50) -> None:
        """Collect up to now, then until a new grid instant arrives (at most one cadence)."""
        self.path.collect()
        waited = 0
        while waited <= cadence_ms:
            if self.path.collect():
                return
            self.clock.advance(step_ms)
            waited += step_ms

    def run_candidate(self, candidate_id: str, *, observation_polls: int,
                      cadence_ms: int, settle_ms: int,
                      stop_requested: Callable[[], bool] = lambda: False,
                      on_poll: Optional[Callable[[int], None]] = None,
                      on_phase: Optional[Callable[[str], None]] = None,
                      trailing_ms: int = 0,
                      conclude_stop_reasons: Callable[[], Sequence[StopReason]] = tuple,
                      ) -> Tuple[str, TrialReport]:
        """Open, apply, observe, judge and settle one candidate on this case.

        ``on_phase`` is told ``apply-start``, ``applied``, ``hold-end``,
        ``trailing-end``, ``conclude-start`` and ``concluded`` as they happen,
        so the caller can stamp them on its own clock.  ``trailing_ms`` keeps
        the candidate applied -- and the collector polled, so the Kernel's
        freshness bound still holds at evaluation -- for that long after the
        hold, before FINALIZE or rollback: what the caller reads at
        ``trailing-end`` (a trailing collection) observed this configuration.
        """
        phase = on_phase or (lambda _name: None)
        cell_id = self.cell_ids[candidate_id]
        if cell_id not in self.kernel.reduced_state()["evidenceCells"]:
            frozen = self.kernel.current_catalog().candidates
            candidate = (frozen.get(candidate_id) if hasattr(frozen, "get") else
                         next((c for c in frozen if c.candidate_id == candidate_id), None))
            if candidate is not None:
                self.kernel.register_evidence_cell(EvidenceCell(
                    cell_id=cell_id, target_ref=self.bundle.target.contract_id,
                    candidate_semantic_hash=candidate.semantic_hash,
                    status=EvidenceCellStatus.OPEN, required_independent_contributions=1),
                    case_id=self.case_id, now=self.clock())
        # The baseline this trial starts from, kept because a finalized success
        # moves the gateway's belief to the applied configuration before the
        # board derives the pair -- and an axis judged against the *new*
        # baseline would look untouched.
        baseline_before = dict(self.live_baseline())
        # Every trial observes on its own grid, anchored at its own start.
        restart = getattr(self.collector, "restart_grid", None)
        if callable(restart):
            restart()
        phase("apply-start")
        trial_id, report = self.path.begin_trial(candidate_id)
        if report is None:
            phase("applied")
            # The change is applied and the hold has an anchor.  Drain the
            # attribution stream once now, so the first grid instant is
            # answered from an indication published after the write rather
            # than from the last one before it -- on the radio the corroborated
            # readback has already done this; a mocked adapter has not.
            self.reader.refresh()
            trailing_polls = -(-max(0, int(trailing_ms)) // max(1, int(cadence_ms)))
            for poll in range(int(observation_polls) + trailing_polls):
                if stop_requested():
                    phase("conclude-start")
                    report = self.path.emergency_stop(trial_id, cell_id=cell_id)
                    break
                self.path.observe(ticks=1, tick_ms=int(cadence_ms))
                if on_poll is not None:
                    on_poll(poll + 1)
                if poll + 1 == int(observation_polls):
                    phase("hold-end")
            if report is None:
                # The caller's trailing-end read (on the radio, the issued echo
                # cohort over SSH, ~5 s) is time the collector does not run, so
                # it goes first and the Kernel is fed afterwards, right after a
                # fresh grid instant: the joint freshness bound equals the 1 s
                # cadence.  Collecting before that read left 2026-09-15 attempt
                # 26 judging a gap-free stream 4.8 s after its newest sample --
                # TELEMETRY_STALE on three of four trials.  The bound is
                # unchanged; a silent collector still leaves the window stale.
                phase("trailing-end")
                phase("conclude-start")
                self.clock.advance(int(settle_ms))
                self._feed_to_fresh_instant(int(cadence_ms))
                report = self.path.conclude_trial(trial_id, cell_id=cell_id, settle_ms=0,
                                                  stop_reasons=tuple(conclude_stop_reasons()))
        phase("concluded")
        record = {
            "trialId": trial_id, "candidateId": candidate_id,
            "baselineBefore": baseline_before,
            "terminalState": report.terminal_state.value, "outcome": report.outcome.value,
            "stopReason": None if report.stop_reason is None else report.stop_reason.value,
            "evidenceStatus": report.evidence_status, "detail": report.detail,
            "gatewayOperations": [list(item) for item in report.gateway_results],
        }
        self.trials.append(record)
        return trial_id, report

    # -- what the board reads --------------------------------------------- #

    def trial_evaluation(self, trial_id: str) -> Mapping[str, Any]:
        trial = self.kernel.reduced_state()["trials"].get(trial_id) or {}
        return dict(trial.get("evaluation") or {})

    def trial_outcome(self, trial_id: str) -> str:
        trial = self.kernel.reduced_state()["trials"].get(trial_id) or {}
        return str(trial.get("outcome") or "NOT_SETTLED")

    def trial_recovery(self, trial_id: str) -> Dict[str, Any]:
        """Did the trial write, and how did the Kernel resolve its transaction?"""
        state = self.kernel.reduced_state()
        trial = state["trials"].get(trial_id) or {}
        transaction = state["transactions"].get(str(trial.get("transactionId") or "")) or {}
        return {"applied": bool(trial.get("applyCounted")),
                "resolution": transaction.get("resolution"),
                "recoveryVerified": bool(trial.get("recoveryVerified"))}

    def trial_baseline(self, trial_id: str) -> Dict[str, Any]:
        """The configuration the trial started from."""
        recorded = next((item for item in self.trials if item["trialId"] == trial_id), None)
        return dict(recorded["baselineBefore"] if recorded else self.live_baseline())

    def axis_verification(self, trial_id: str, candidate_parameters: Mapping[str, str]
                          ) -> Dict[str, ActionVerification]:
        """Per-axis technical verification, from the gateway's own evidence.

        The last configuration read of the *applied* phase -- the commit's
        confirming read -- is what says whether each executed axis reached the
        value the candidate asked for.  An axis the candidate left at its
        baseline is ``NOT_EXECUTED``; one whose apply the adapter refused is
        ``REFUSED``; one the read could not confirm is ``UNVERIFIED``.
        """
        trial = self.kernel.reduced_state()["trials"].get(trial_id) or {}
        transaction_id = str(trial.get("transactionId") or "")
        baseline = self.trial_baseline(trial_id)
        apply_outcomes: Dict[str, str] = {}
        apply_sequences: set = set()
        reads: List[Tuple[int, int, Dict[str, Any]]] = []
        for reference in self.gateway.evidence_references():
            if not reference.startswith(f"{transaction_id}#"):
                continue
            entry = self.gateway.evidence(reference)
            command = entry.get("command") or {}
            operation = str(entry.get("operation"))
            sequence = int(command.get("commandSequence", 0) or 0)
            if operation == GatewayOperation.APPLY.value:
                apply_outcomes[str(command.get("axis"))] = str(entry.get("outcome"))
                apply_sequences.add(sequence)
            elif operation == GatewayOperation.READ.value and entry.get("observedConfig"):
                reads.append((sequence, int(command.get("commandIndex", 0) or 0),
                              dict(entry["observedConfig"])))
        # The confirming read is the one issued under the same permit as the
        # applies (the COMMIT token's command sequence), at the highest index.
        # The recovery reads that follow a rollback are issued under later
        # permits and would show the baseline again.
        observed: Dict[str, Any] = {}
        confirming = [item for item in reads if item[0] in apply_sequences]
        if confirming:
            top = max(item[1] for item in confirming)
            for sequence, index, config in confirming:
                if index == top:
                    observed.update(config)
        states: Dict[str, ActionVerification] = {}
        for axis, value in candidate_parameters.items():
            if str(value) == str(baseline.get(axis)):
                states[axis] = ActionVerification.NOT_EXECUTED
                continue
            outcome = apply_outcomes.get(axis)
            if outcome in (GatewayOutcome.REJECTED.value, GatewayOutcome.ERROR.value,
                           GatewayOutcome.REJECTED_CONFIG_MISMATCH.value):
                states[axis] = ActionVerification.REFUSED
                continue
            seen = observed.get(axis)
            if seen is not None and str(seen) == str(value):
                states[axis] = ActionVerification.VERIFIED
            elif outcome is None:
                states[axis] = ActionVerification.REFUSED
            else:
                states[axis] = ActionVerification.UNVERIFIED
        return states

    def conditions(self) -> Dict[str, Any]:
        return {
            "epochHash": self.epoch_hash(),
            "ues": {p.ue_id: {"amfUeNgapId": p.identity.amf_ue_ngap_id,
                              "servingCell": p.identity.serving_nci,
                              "e2Node": p.identity.e2_node,
                              "connectionEpoch": p.identity.connection_epoch}
                    for p in self.steering},
            "baseline": dict(self.live_baseline()),
        }

    def terminate(self) -> Any:
        return self.path.terminate()

    def policy_ids(self) -> Dict[str, Tuple[str, ...]]:
        answer: Dict[str, Tuple[str, ...]] = {}
        for key, adapter in self.adapters.items():
            bindings = getattr(adapter, "bindings", None)
            answer[key] = (tuple(sorted(set(bindings().values())))
                           if callable(bindings) else ())
        return answer


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #


class _FanOutSampleLoader:
    """One drain of a sample source shared by several grid collectors.

    Each collector polls on its own; a source drained by the first would be
    empty for the second.  This keeps every loaded sample until every consumer
    has taken it once.
    """

    def __init__(self, load: Callable[[], Sequence[RawSample]]) -> None:
        self._load = load
        self._buffer: List[RawSample] = []
        self._cursors: Dict[str, int] = {}

    def for_consumer(self, name: str) -> Callable[[], Sequence[RawSample]]:
        self._cursors.setdefault(name, 0)

        def load() -> Sequence[RawSample]:
            self._buffer.extend(self._load())
            start = self._cursors[name]
            taken = tuple(self._buffer[start:])
            self._cursors[name] = len(self._buffer)
            floor = min(self._cursors.values()) if self._cursors else 0
            if floor:
                del self._buffer[:floor]
                for key in self._cursors:
                    self._cursors[key] -= floor
            return taken

        return load


def build_joint_live_runtime(
    *,
    joint: JointComposition,
    binding: Any,
    steering: Sequence[SteeringParticipantSpec],
    supplementary: Sequence[SupplementaryParticipantSpec] = (),
    reader: KpmUeAttributionReader,
    now: Callable[[], str],
    monotonic_ms: Callable[[], int],
    sleep_ms: Callable[[int], None],
    case_id: str,
    counter_sample_loaders: Optional[Mapping[str, Callable[[], Sequence[RawSample]]]] = None,
    arrival: Optional[Callable[[], str]] = None,
    initial_configuration: Optional[Mapping[str, Any]] = None,
    #: 주면 이벤트를 **append 할 때마다 디스크에 fsync** 한다.  주지 않으면 메모리에만
    #: 쌓고 판이 끝날 때 한 번에 쓴다 -- 그러면 **강제 종료된 판의 증거가 통째로 사라진다**
    #: (2026-09-21: 러너 timeout·서비스 재기동으로 죽은 판이 이 경우다).  기본값을 유지해
    #: 기존 호출부(테스트·하드웨어-프리)는 그대로 둔다.
    event_store_path: Optional[Any] = None,
) -> JointLiveRuntime:
    """Wire one joint case: every axis its adapter, every UE its collector.

    ``initial_configuration`` is what the equipment holds when this case opens,
    when that is not the composition's declared baseline -- an Agent sitting's
    retention case opens after the search case's trials moved the radio, and
    the Write Gateway refuses a permit whose expected configuration is not the
    live one.  Only the frozen axes are read from it; the rest is ignored.
    """
    steering = tuple(steering)
    supplementary = tuple(supplementary)
    if not steering:
        raise LiveDriverError("a joint case needs at least one steering participant")
    bundle = joint.bundle
    geometry = bundle_geometry(bundle)
    clock = InjectedClock(now=now, monotonic_ms=monotonic_ms, sleep_ms=sleep_ms)
    reader = _SharedAttributionReader(reader)  # type: ignore[assignment]
    reader.refresh()

    adapters: Dict[str, Any] = {}
    axis_adapters: Dict[str, str] = {}
    declarations: List[Dict[str, Any]] = []
    bound_builders: Dict[str, Callable[[Mapping[str, Any]], Dict[str, Any]]] = {}
    live_baseline: Dict[str, Any] = dict(bundle.baseline_config)
    if initial_configuration is not None:
        for axis in live_baseline:
            if axis in initial_configuration:
                live_baseline[axis] = str(initial_configuration[axis])
    baseline_of = lambda: live_baseline  # noqa: E731 - replaced once the path exists

    for participant in steering:
        axis = participant.axis
        if axis not in bundle.baseline_config:
            raise LiveDriverError(f"steering participant {participant.ue_id} has no axis {axis} on the surface")
        if participant.adapter_override is not None:
            inner = participant.adapter_override
        else:
            status_poller = R1StatusPoller(
                participant.policy_port, cadence_ms=binding.r1.cadence_ms,
                deadline_ms=binding.r1.deadline_ms, monotonic_ms=monotonic_ms,
                # Corroboration compares the unscoped servingCell from both
                # sources; _AxisReadback adds the UE scope after they agree.
                sleep_ms=sleep_ms, projection=serving_cell_projection)
            readback = _AxisReadback(CorroboratedServingCellReadback(
                reader=reader, status_poller=status_poller,
                # The resolver alone was not enough.  Handing the bare callable in
                # left the readback with no id at all until the resolver first
                # answered, so a role whose entry was already older than
                # ROLE_IDENTITY_MAX_AGE_S on the first read asked KPM for nothing
                # and the trial locked down -- attempt 158 of 2026-09-16 lost
                # r1-steer@ue2 exactly that way, with the per-read resolution
                # already in place.  Wrap it so the composition-time id is the
                # fallback: the readback follows a re-registration and a stale
                # entry can never erase a usable id.
                amf_ue_ngap_id=_resolved_or_pinned(participant), now=now,
                monotonic_ms=monotonic_ms, sleep_ms=sleep_ms,
                cadence_ms=binding.r1.cadence_ms, deadline_ms=binding.r1.deadline_ms,
                freshness_bound_ms=geometry.freshness_bound_ms,
                corroboration_deadline_ms=geometry.freshness_bound_ms), axis)

            def policy_builder(command: Mapping[str, Any], *, _key: str = participant.key) -> Dict[str, Any]:
                build = bound_builders.get(_key)
                if build is None:
                    raise LiveDriverError("no epoch is frozen yet")
                return build(command)

            inner = build_live_r1_adapter(
                binding, policy_port=participant.policy_port, policy_builder=policy_builder,
                monotonic_ms=monotonic_ms, sleep_ms=sleep_ms,
                status_projection=_axis_projection(axis), readback_port=readback,
                operation_journal=participant.operation_journal,
                # 시계를 주지 않으면 어댑터가 `lambda: ""` 로 떨어져 작업 원장의 모든
                # `at` 이 **빈 문자열**이 된다 (r1_adapter.py:349).  2026-09-17 에 원장을
                # 붙이면서 이걸 빠뜨려, 조종 항목만 시각 없이 쌓였다 -- 보조 축은
                # 어댑터가 미리 만들어져 와서 시계를 갖고 있었으므로 비교로도 안 드러났다.
                # 시각이 없으면 되읽기 실패를 시간으로 대조할 수 없다.
                clock=clock,
                name=participant.key)
        # Only the live adapter this loop built; an injected override keeps
        # whatever behaviour its test gave it.
        if participant.adapter_override is None:
            inner.restore_by_handover = True
        adapters[participant.key] = BaselineAwareAdapter(
            inner, axis=axis, baseline_of=lambda: baseline_of(),
            name_baseline_on_halt=True)
        axis_adapters[axis] = participant.key
        declarations.append({"candidateParameter": axis, "axis": axis, "adapter": participant.key})
    for participant in supplementary:
        if participant.axis not in bundle.baseline_config:
            raise LiveDriverError(f"supplementary axis {participant.axis} is not on the surface")
        if participant.adapter_key in adapters:
            raise LiveDriverError(f"adapter key {participant.adapter_key} is registered twice")
        adapters[participant.adapter_key] = BaselineAwareAdapter(
            participant.adapter, axis=participant.axis, baseline_of=lambda: baseline_of())
        axis_adapters[participant.axis] = participant.adapter_key
        declarations.append({"candidateParameter": participant.axis, "axis": participant.axis,
                             "adapter": participant.adapter_key})
    # Cell power goes first, then steering, then the UE-scoped axes (2026-09-26).  The E2 RC
    # control for a cell's TX attenuation carries a UE (Format-1) header, so the producer can
    # only write or restore it while some UE is attached to that cell.  Steering ue1 -- the
    # only UE on gnb2 -- ahead of a gnb2 power write left the write and its rollback with no
    # UE to address: readback UNKNOWN, lockdown (boards 717/719/720/723, four of six).  The
    # declaration order is the apply order and reverse rollback unwinds it backwards, so this
    # writes power before the UE leaves and restores it after the UE is back.  UE-scoped axes
    # stay after steering: an RNTI is cell-local (vertical.plan_for).
    declarations = cell_power_first(declarations)
    uncovered = sorted(set(bundle.baseline_config) - set(axis_adapters))
    if uncovered:
        raise LiveDriverError(f"no adapter carries the axes {uncovered}; every frozen axis needs a writer")

    gateway = TokenBoundWriteGateway(
        adapters=adapters, safe_state=dict(bundle.safe_state),
        journal=InMemoryTransactionJournal(), axis_adapters=axis_adapters, clock=clock)
    event_store = (MemoryEventStore() if event_store_path is None
                   else FileEventStore(event_store_path))
    kernel = AssuranceKernel(event_store=event_store, reducer=KernelReducer(),
                             write_gateway=gateway, measurement_collector=None)
    epoch = _admit_and_freeze(kernel, bundle, clock)
    catalog = kernel.current_catalog()
    kernel.open_case(
        case_id=case_id, policy=bundle.case_policy,
        active_vector=bundle.vector.ordered_target_refs[0],
        usable_reserve={bundle.harm.contract_id: bundle.harm.reserve.to_canonical_dict()},
        reserve_per_trial={bundle.harm.contract_id: bundle.harm.reserve.to_canonical_dict()},
        # 2026-09-19: the catalog is a frozen domain.  One evidence cell per
        # point would be the enumeration again (819,200 cells); a cell is
        # registered when its candidate is first trialled (run_candidate), and
        # the Kernel counts the untried rest as open (_exhaustion_for_case).
        evidence_cells=(), lazy_evidence_cells=True,
        now=clock())
    for participant in steering:
        if participant.adapter_override is None:
            bound_builders[participant.key] = _builder_following_the_ue(
                participant, kernel, joint)

    anchor = lambda: _observation_anchor(kernel, case_id)  # noqa: E731
    collectors: List[Any] = []
    serving_by_ue = {p.ue_id: p for p in steering}
    # A role-labelled UE (``ue1``) is carried on the stream under the id this case addresses.
    # 되읽기(616행)·serving-cell 수집기(798행)와 **같은 해석기**를 준다.  여기만
    # 조립 시점 번호로 얼려 두면 재등록 뒤 counter grid 가 그 UE 의 기록을 전부 거절한다.
    ue_aliases = {p.ue_id: _resolved_or_pinned(p) for p in steering
                  if not str(p.ue_id).isdigit()}
    loaders: Dict[str, Callable[[], Sequence[RawSample]]] = {}
    if counter_sample_loaders is not None:
        loaders = dict(counter_sample_loaders)
    else:
        live = _live_counter_loaders(binding, geometry, arrival)
        shared: Dict[int, _FanOutSampleLoader] = {}
        for counter_id, load in live.items():
            shared.setdefault(id(load), _FanOutSampleLoader(load))
            loaders[counter_id] = shared[id(load)].for_consumer(counter_id)
    for item in geometry.counters:
        if item.deployment_counter_name == SERVING_CELL_COUNTER:
            ue_id = str(item.scope.get("ueId", ""))
            participant = serving_by_ue.get(ue_id)
            if participant is None:
                raise LiveDriverError(f"serving-cell counter {item.counter_id} names UE {ue_id}, which no participant observes")
            collectors.append(ServingCellCollector(
                reader=reader, deployment=None, timing=None, anchor=anchor,
                # 되읽기(616행)와 **같은 해석기**를 준다.  여기만 int 로 얼려 두면
                # UE 가 판 도중 재등록했을 때 수집기가 옛 번호를 계속 묻고, 그 구간
                # 칸이 비어 servingCell coverage 가 1.0 미만이 되며, 그러면 그 KPI 가
                # 통째로 게시되지 않는다 (2026-09-17 실측: 시행의 44%).
                amf_ue_ngap_id=_resolved_or_pinned(participant),
                clock_health=lambda observation, instant, _cadence=item.cadence_ms: same_host_clock_health(
                    observation, instant, tolerance_ms=2 * _cadence),
                source_id=f"live-serving-cell@{ue_id}", counter_id=item.counter_id,
                scope_snapshot={"ueId": ue_id}, cadence_ms=item.cadence_ms))
            continue
        load = loaders.get(item.counter_id)
        if load is None:
            raise LiveDriverError(f"no live {item.source.value} loader for {item.counter_id}")
        relevant = tuple(m for m in bundle.measurements if m.counter_id == item.counter_id)
        membership = relevant[0].membership_snapshot if relevant else ()
        collectors.append(_CounterGridCollector(
            geometry=item, anchor=anchor, load_samples=load, membership=membership,
            ue_aliases=ue_aliases))
    collector = _MultiCounterCollector(collectors)
    path = VerticalPath(
        kernel=kernel, gateway=gateway,
        deployment=VerticalDeployment(
            adapter_name=steering[0].key, scope=dict(bundle.scope),
            baseline_config=dict(live_baseline),
            supplementary_axes=tuple(declarations)),
        case_id=case_id, clock=clock, coordinator=None, collector=collector)
    baseline_of = lambda: path._live_baseline  # noqa: E731,SLF001 - the gateway's own belief
    return JointLiveRuntime(
        joint=joint, kernel=kernel, gateway=gateway, path=path, collector=collector,
        reader=reader, clock=clock, case_id=case_id, epoch=epoch, event_store=event_store,
        adapters=adapters, steering=steering, supplementary=supplementary,
        cell_ids=_LazyCellIds(), geometry=geometry)
