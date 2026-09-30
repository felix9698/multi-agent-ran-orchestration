"""The shared hardware-free scenario matrix every objective family runs.

Not a test module (``discover`` collects ``test*.py`` only): it is the harness
those tests drive.  Task section 8, chain item 11 requires every one of the
seven families to complete a hardware-free positive, negative, malformed,
conflict, stale, missing, timeout, partial-effect, duplicate and fault
scenario, and Gate 4's acceptance requires the round trip to actually pass.
Written once here, parameterised by family, so a family lane supplies contract
instances and expected outcomes and the matrix runs itself.

What a lane supplies is deliberately small -- an
:class:`~assurance.objectives.family.ObjectiveContractBundle`, a collector per
verdict-bearing scenario, and an
:class:`~assurance.objectives.family.ExpectedOutcome` per verdict-bearing
scenario.  What it cannot supply is what "refused" means: malformed, conflict,
timeout and duplicate are architecture-wide refusals asserted identically for
every family, because a family able to define its own refusal could define one
it happens to satisfy.

Nothing is stubbed but the deployment.  The contracts go through the Kernel's
real admission and epoch freeze, the effects through the real
:class:`~assurance.gateway.gateway.TokenBoundWriteGateway` over
:class:`~assurance.gateway.mock_adapter.MockActuationAdapter`, and the samples
through :class:`~assurance.collector.mock_source.MockMeasurementSource`.  Every
run happens with ``socket``, ``subprocess`` and ``os.system`` replaced by a
raising stub, so "hardware-free" is a property of the run rather than a claim
about it (design section 15).

**Mock and replay results are never OTA evidence.**  Every
:class:`ScenarioResult` carries :attr:`ScenarioResult.evidence_grade` fixed at
``HARDWARE_FREE``; the registry's ``OTA_LIVE_VERIFIED`` state refuses to be
entered without retained OTA evidence references, and nothing here can produce
one.
"""

from __future__ import annotations

import os
import socket
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

from assurance.advisors.coordinator import StrategyBackedEvidenceCoordinator
from assurance.advisors.proposal_support import build_next_candidate_proposal
from assurance.advisors.strategies import DeterministicStrategy
from assurance.collector.mock_source import MockMeasurementSource
from assurance.contracts.ledgers import EvidenceCell
from assurance.contracts.validation import contract_content_hash, validate_family_set
from assurance.core.axes import EvidenceCellStatus, TrialOutcome
from assurance.core.confirmation import ConfirmationAction, ConfirmationRecord
from assurance.core.envelopes import EnvelopeRejection
from assurance.core.provenance import TypedQuantity
from assurance.core.timebase import parse_utc
from assurance.core.states import StopReason, TrialState
from assurance.gateway.gateway import TokenBoundWriteGateway
from assurance.gateway.journal import InMemoryTransactionJournal
from assurance.gateway.mock_adapter import FaultInjection, MockActuationAdapter
from assurance.gateway.token import KernelToken, TokenKind
from assurance.gateway.write_gateway import GatewayOutcome
from assurance.kernel.event_store import MemoryEventStore
from assurance.kernel.kernel import AssuranceKernel, KernelRefusal
from assurance.kernel.reducer import KernelReducer
from assurance.objectives.family import (
    VERDICT_SCENARIOS,
    ExpectedConfiguration,
    ExpectedOutcome,
    ObjectiveContractBundle,
    ScenarioName,
)
from assurance.vertical import SteppingClock, VerticalDeployment, VerticalPath

__all__ = [
    "FamilyCase",
    "HarnessInvariantError",
    "NoLiveCall",
    "ObjectiveHarness",
    "ObjectiveMatrixMixin",
    "ScenarioResult",
    "mismatches",
    "no_live_calls",
]

#: Every terminal trial state.  A scenario that leaves a trial anywhere else
#: has left the deployment in flight, which is the one outcome no branch of
#: design section 7 permits.
TERMINAL_STATES = frozenset(
    {
        TrialState.PRE_COMMIT_ABORT,
        TrialState.SETTLED_SUCCESS,
        TrialState.SETTLED_NON_SUCCESS,
        TrialState.INCIDENT_LOCKDOWN,
    }
)


class NoLiveCall(AssertionError):
    """Raised the moment a hardware-free run reaches for the outside world."""


class HarnessInvariantError(AssertionError):
    """A run broke an invariant that holds for every family and every scenario."""


@contextmanager
def no_live_calls() -> Iterator[None]:
    """Run the block with every outward door replaced by a raising stub."""

    def refuse(*args: Any, **kwargs: Any):
        raise NoLiveCall("a hardware-free scenario attempted a live call")

    patches = [
        (socket, "socket"),
        (socket, "create_connection"),
        (subprocess, "Popen"),
        (subprocess, "run"),
        (os, "system"),
        (os, "popen"),
    ]
    originals = [(module, name, getattr(module, name)) for module, name in patches]
    for module, name in patches:
        setattr(module, name, refuse)
    try:
        yield
    finally:
        for module, name, original in originals:
            setattr(module, name, original)


@dataclass(frozen=True)
class FamilyCase:
    """Everything one objective family gives the matrix.

    ``collector_for`` receives the scenario and the instant the observation
    window starts, and returns the scripted measurement source for it.  It is a
    callable rather than a mapping because the start instant is only known once
    the case has been opened.
    """

    bundle: ObjectiveContractBundle
    expectations: Mapping[ScenarioName, ExpectedOutcome]
    collector_for: Callable[[ScenarioName, str], MockMeasurementSource]
    case_id: str
    evidence_cell_id: str
    #: Optional per-family fault recipe.  Returning ``None`` for a scenario
    #: falls back to the harness's own recipe.  A family may choose *how* its
    #: deployment fails; it may not choose what counts as a refusal, which is
    #: why this changes the injected fault and never the assertion.
    faults_for: Optional[Callable[[ScenarioName], Optional[FaultInjection]]] = None
    #: The instant every run starts from.  Fixed, so two runs of the same
    #: scenario produce the same event stream and the same terminal hash.
    start: str = "2026-08-21T09:00:00.000000Z"
    #: Observation geometry, in the collector's own cadence.
    observation_ticks: int = 4
    tick_ms: int = 1000
    settle_ms: int = 1000
    #: How far *past* the permit's own lease the timeout scenario advances the
    #: clock.  A margin, not the lease: the lease itself is sized by the
    #: Kernel from the frozen contracts
    #: (``assurance.kernel.kernel.contracted_lease_ms``), so a long-hold
    #: family gets a long permit and a constant here would silently stop
    #: overrunning it.
    lease_overrun_ms: int = 30_000
    #: Fraction of the harm reserve one trial may charge.
    reserve_fraction: float = 0.2

    def family(self) -> str:
        return self.bundle.family


@dataclass
class ScenarioResult:
    """What one scenario did, as observed from outside the runtime."""

    scenario: ScenarioName
    family: str
    #: Fixed. A mock or replay run is never OTA evidence (design section 15).
    evidence_grade: str = "HARDWARE_FREE"
    trial_id: Optional[str] = None
    terminal_state: Optional[TrialState] = None
    outcome: Optional[TrialOutcome] = None
    stop_reason: Optional[StopReason] = None
    evidence_status: Optional[str] = None
    configuration: Mapping[str, Any] = field(default_factory=dict)
    #: The configuration a successful finalization would have left behind.
    #: Recorded so an expectation can name it without recomputing the merge.
    applied_config: Mapping[str, Any] = field(default_factory=dict)
    gateway_results: Tuple[Tuple[str, GatewayOutcome], ...] = ()
    adapter_writes: int = 0
    #: Refusal reasons the Kernel recorded, in order.
    refusals: Tuple[str, ...] = ()
    detail: str = ""

    @property
    def is_ota_evidence(self) -> bool:
        """Always ``False``. Kept as a property so a caller has to ask."""
        return False


def mismatches(
    result: ScenarioResult, expected: ExpectedOutcome, bundle: ObjectiveContractBundle
) -> Tuple[str, ...]:
    """Every way ``result`` differs from what the family said it expected."""
    found: List[str] = []
    if result.terminal_state is not expected.terminal_state:
        found.append(
            f"terminal state: expected {expected.terminal_state.value}, "
            f"observed {result.terminal_state.value if result.terminal_state else None}"
        )
    if result.outcome is not expected.outcome:
        found.append(
            f"outcome: expected {expected.outcome.value}, "
            f"observed {result.outcome.value if result.outcome else None}"
        )
    if result.stop_reason is not expected.stop_reason:
        observed = result.stop_reason.value if result.stop_reason else None
        wanted = expected.stop_reason.value if expected.stop_reason else None
        found.append(f"stop reason: expected {wanted}, observed {observed}")
    if expected.evidence_status is not None and result.evidence_status != expected.evidence_status:
        found.append(
            f"evidence status: expected {expected.evidence_status}, "
            f"observed {result.evidence_status}"
        )
    wanted_config = _expected_configuration(expected.configuration, bundle, result)
    if wanted_config is not None and dict(result.configuration) != dict(wanted_config):
        found.append(
            f"configuration: expected {dict(wanted_config)}, observed {dict(result.configuration)}"
        )
    return tuple(found)


def _expected_configuration(
    kind: ExpectedConfiguration,
    bundle: ObjectiveContractBundle,
    result: ScenarioResult,
) -> Optional[Mapping[str, Any]]:
    if kind is ExpectedConfiguration.UNCONSTRAINED:
        return None
    if kind is ExpectedConfiguration.BASELINE:
        return bundle.baseline_config
    if kind is ExpectedConfiguration.SAFE_STATE:
        return bundle.safe_state
    return result.applied_config


class ObjectiveHarness:
    """Runs one family's contract set through one scenario, and checks itself.

    A new instance per scenario: the whole point of a deterministic clock and
    an in-memory event store is that a run starts from nothing, so sharing one
    across scenarios would make the second run depend on the first.
    """

    def __init__(self, case: FamilyCase) -> None:
        self.case = case
        self.bundle = case.bundle
        self.clock = SteppingClock(case.start)

    # -- construction ------------------------------------------------------

    def _build(
        self,
        *,
        scenario: ScenarioName,
        faults: Optional[FaultInjection] = None,
    ) -> VerticalPath:
        bundle = self.bundle
        validate_family_set(list(bundle.all_contracts()) + [bundle.deployment])
        self.adapter = MockActuationAdapter(
            config=dict(bundle.baseline_config), faults=faults, name=bundle.adapter_name
        )
        self.journal = InMemoryTransactionJournal()
        self.gateway = TokenBoundWriteGateway(
            adapters={bundle.adapter_name: self.adapter},
            safe_state=dict(bundle.safe_state),
            journal=self.journal,
            clock=self.clock,
        )
        self.store = MemoryEventStore()
        self.kernel = AssuranceKernel(
            event_store=self.store,
            reducer=KernelReducer(),
            write_gateway=self.gateway,
            measurement_collector=None,
        )
        self._admit_and_freeze()
        self._open_case()
        self.collector = self.case.collector_for(scenario, self.clock())
        self.path = VerticalPath(
            kernel=self.kernel,
            gateway=self.gateway,
            deployment=VerticalDeployment(
                adapter_name=bundle.adapter_name,
                scope=dict(bundle.scope),
                baseline_config=dict(bundle.baseline_config),
            ),
            case_id=self.case.case_id,
            clock=self.clock,
            coordinator=StrategyBackedEvidenceCoordinator(strategy=DeterministicStrategy()),
            collector=self.collector,
        )
        return self.path

    def _confirmation(self, contract: Any, *, event_id: str) -> ConfirmationRecord:
        return ConfirmationRecord(
            confirmed_object_type=type(contract).__name__,
            confirmed_content_hash=contract_content_hash(contract),
            event_id=event_id,
            timestamp=self.clock(),
            action=ConfirmationAction.CONFIRM_AND_START,
        )

    def _admit_and_freeze(self) -> None:
        bundle = self.bundle
        now = self.clock()
        for contract in bundle.unconfirmed_contracts():
            self.kernel.admit_contract(contract, confirmation=None, now=now)
        self.vector_confirmation = self._confirmation(
            bundle.vector, event_id="confirm/vector"
        )
        self.kernel.admit_contract(
            bundle.vector, confirmation=self.vector_confirmation, now=now
        )
        self.kernel.admit_contract(
            bundle.case_policy,
            confirmation=self._confirmation(bundle.case_policy, event_id="confirm/policy"),
            now=now,
        )
        self.kernel.admit_deployment(bundle.deployment, now=now)
        self.epoch = self.kernel.freeze_epoch(
            confirmation=self.vector_confirmation, now=now
        )

    def _open_case(self) -> None:
        bundle = self.bundle
        reserve = bundle.harm.reserve
        per_trial = TypedQuantity(
            reserve.value * self.case.reserve_fraction,
            reserve.unit,
            reserve.provenance,
            reserve.source_record,
        )
        catalog = self.kernel.current_catalog()
        self.kernel.open_case(
            case_id=self.case.case_id,
            policy=bundle.case_policy,
            active_vector=bundle.vector.ordered_target_refs[0],
            usable_reserve={bundle.harm.contract_id: reserve.to_canonical_dict()},
            reserve_per_trial={bundle.harm.contract_id: per_trial.to_canonical_dict()},
            evidence_cells=(
                EvidenceCell(
                    cell_id=self.case.evidence_cell_id,
                    target_ref=bundle.target.contract_id,
                    candidate_semantic_hash=catalog.candidates[0].semantic_hash,
                    status=EvidenceCellStatus.OPEN,
                    required_independent_contributions=1,
                ),
            ),
            now=self.clock(),
        )

    # -- inspection --------------------------------------------------------

    def first_candidate_id(self) -> str:
        return self.kernel.current_catalog().candidates[0].candidate_id

    def applied_configuration(self) -> Dict[str, Any]:
        merged = dict(self.bundle.baseline_config)
        merged.update(dict(self.kernel.current_catalog().candidates[0].parameters))
        return merged

    def _kernel_refusals(self) -> Tuple[str, ...]:
        return tuple(
            envelope.payload["reason"]
            for envelope in self.store.iterate()
            if envelope.event_kind
            in {"AdvisoryRejected", "GatewayResultRejected", "TrialRefused"}
        )

    def _issued_tokens(self) -> List[KernelToken]:
        return [
            KernelToken.from_canonical_dict(
                {key: value for key, value in envelope.payload.items() if key != "resourceId"}
            )
            for envelope in self.store.iterate()
            if envelope.event_kind == "TokenIssued"
        ]

    def _trial_record(self, trial_id: str) -> Mapping[str, Any]:
        return self.kernel.reduced_state()["trials"][trial_id]

    # -- the matrix --------------------------------------------------------

    def run(self, scenario: ScenarioName) -> ScenarioResult:
        """Execute one scenario, assert the universal invariants, report."""
        runner = {
            ScenarioName.POSITIVE: self._run_verdict,
            ScenarioName.NEGATIVE: self._run_verdict,
            ScenarioName.STALE: self._run_verdict,
            ScenarioName.MISSING: self._run_verdict,
            ScenarioName.PARTIAL_EFFECT: self._run_verdict,
            ScenarioName.FAULT: self._run_verdict,
            ScenarioName.MALFORMED: self._run_malformed,
            ScenarioName.CONFLICT: self._run_conflict,
            ScenarioName.TIMEOUT: self._run_timeout,
            ScenarioName.DUPLICATE: self._run_duplicate,
        }[scenario]
        with no_live_calls():
            result = runner(scenario)
        self._assert_universal(result, scenario)
        return result

    def _faults_for(self, scenario: ScenarioName) -> Optional[FaultInjection]:
        if self.case.faults_for is not None:
            supplied = self.case.faults_for(scenario)
            if supplied is not None:
                return supplied
        if scenario is ScenarioName.FAULT:
            # A PREPARE that fails: the cheapest fault to reason about, because
            # nothing has been written when it happens, so "no side effect" is
            # an assertion about the equipment and not about a reversal.
            return FaultInjection(fail_prepare=True)
        if scenario is ScenarioName.PARTIAL_EFFECT:
            axes = self.bundle.configuration_axes()
            if len(axes) > 1:
                # The last axis is refused outright: the first lands, the plan
                # does not complete, and the gateway must observe the gap
                # rather than the acknowledgement it did not get.
                return FaultInjection(fail_axes=frozenset({axes[-1]}))
            # A one-axis plan cannot land partially, and the substitutes are
            # different scenarios wearing this one's name: a lost
            # acknowledgement the readback resolves is a success, and a
            # readback that fails is the fault case.  So the harness refuses to
            # invent one and asks the lane for the partial effect its own
            # deployment actually produces -- an A1 policy created but not
            # enforced, a control acknowledged by one node of two.
            raise HarnessInvariantError(
                f"{self.bundle.family}: the target's options change "
                f"{list(axes)}, fewer than the two configuration axes a plan "
                "needs in order to land partially. Supply FamilyCase.faults_for "
                "for the partial-effect scenario rather than reporting a "
                "substitute under its name."
            )
        return None

    def _run_verdict(self, scenario: ScenarioName) -> ScenarioResult:
        path = self._build(scenario=scenario, faults=self._faults_for(scenario))
        candidate_id, rejection = path.request_proposal()
        if rejection is not None or candidate_id is None:
            raise HarnessInvariantError(
                f"{self.bundle.family}/{scenario.value}: the coordinator could not "
                f"propose a frozen candidate ({rejection})"
            )
        report = path.run_trial(
            candidate_id,
            cell_id=self.case.evidence_cell_id,
            observation_ticks=self.case.observation_ticks,
            tick_ms=self.case.tick_ms,
            settle_ms=self.case.settle_ms,
        )
        return ScenarioResult(
            scenario=scenario,
            family=self.bundle.family,
            applied_config=self.applied_configuration(),
            trial_id=report.trial_id,
            terminal_state=report.terminal_state,
            outcome=report.outcome,
            stop_reason=report.stop_reason,
            evidence_status=report.evidence_status,
            configuration=self.adapter.snapshot(),
            gateway_results=report.gateway_results,
            adapter_writes=len(self.adapter.writes),
            refusals=self._kernel_refusals(),
            detail=report.detail,
        )

    def _run_malformed(self, scenario: ScenarioName) -> ScenarioResult:
        """A sealed advisory body outside the typed schema changes nothing.

        Malformed input is refused at the mailbox, before it can reach a
        decision: no trial exists afterwards, and the equipment has not been
        touched.
        """
        path = self._build(scenario=ScenarioName.POSITIVE)
        message = build_next_candidate_proposal(
            message_id="probe/malformed",
            candidate_id=self.first_candidate_id(),
            rationale="probe",
            correlation_id=self.case.case_id,
            epoch_hash=path.epoch_hash(),
            now=self.clock(),
        )
        rejection = path.submit(_tampered(message, {"verdict": "PASS"}))
        if rejection is not EnvelopeRejection.SOURCE_NOT_PERMITTED:
            raise HarnessInvariantError(
                f"{self.bundle.family}: a malformed advisory body was not refused "
                f"({rejection})"
            )
        state = self.kernel.reduced_state()
        if state["trials"]:
            raise HarnessInvariantError(
                f"{self.bundle.family}: a refused advisory opened a trial"
            )
        if self.adapter.writes:
            raise HarnessInvariantError(
                f"{self.bundle.family}: a refused advisory reached the equipment"
            )
        return ScenarioResult(
            scenario=scenario,
            family=self.bundle.family,
            configuration=self.adapter.snapshot(),
            adapter_writes=0,
            refusals=self._kernel_refusals(),
            detail=f"refused: {rejection.value}",
        )

    def _run_conflict(self, scenario: ScenarioName) -> ScenarioResult:
        """Two trials cannot act on one case at once, and the first still ends.

        The conflict a coordination case actually has is concurrency over the
        same scope, and the Kernel serialises it.  The refusal has two shapes
        and the scenario checks both, because they guard different windows: a
        second trial attempted while the first is merely open is refused
        ``ACTIVE_TRIAL_EXISTS``, and one attempted while a change is
        outstanding is refused ``RECOVERY_BLOCKED`` -- the stronger guard,
        which fires before the Kernel even looks at how many trials are live.

        Neither refusal may cost the trial that was already running, so the
        scenario also drives that one to its terminal state.
        """
        path = self._build(scenario=ScenarioName.POSITIVE)
        candidate_id, _ = path.request_proposal()
        trial_id = path.open_trial(candidate_id)
        path.reserve_and_stage(trial_id)
        before_commit = self._refused_second_trial(candidate_id)
        if before_commit != "ACTIVE_TRIAL_EXISTS":
            raise HarnessInvariantError(
                f"{self.bundle.family}: a second trial opened beside a live one was "
                f"refused {before_commit!r}, not ACTIVE_TRIAL_EXISTS"
            )
        for step, call in (("prepare", path.prepare), ("ready", path.ready)):
            outcome = call(trial_id).outcome
            if outcome is not GatewayOutcome.ACKED:
                raise HarnessInvariantError(
                    f"{self.bundle.family}: {step} did not acknowledge ({outcome.value})"
                )
        path.commit_decision(trial_id)
        path.commit(trial_id)
        path.enter_observation(trial_id)
        after_commit = self._refused_second_trial(candidate_id)
        if after_commit not in {"RECOVERY_BLOCKED", "ACTIVE_TRIAL_EXISTS"}:
            raise HarnessInvariantError(
                f"{self.bundle.family}: a second trial attempted over an outstanding "
                f"change was refused {after_commit!r}"
            )
        path.observe(ticks=self.case.observation_ticks, tick_ms=self.case.tick_ms)
        report = path.conclude_trial(
            trial_id,
            cell_id=self.case.evidence_cell_id,
            settle_ms=self.case.settle_ms,
        )
        return ScenarioResult(
            scenario=scenario,
            family=self.bundle.family,
            applied_config=self.applied_configuration(),
            trial_id=report.trial_id,
            terminal_state=report.terminal_state,
            outcome=report.outcome,
            stop_reason=report.stop_reason,
            evidence_status=report.evidence_status,
            configuration=self.adapter.snapshot(),
            gateway_results=report.gateway_results,
            adapter_writes=len(self.adapter.writes),
            refusals=self._kernel_refusals(),
            detail=f"second trial refused: {before_commit} then {after_commit}",
        )

    def _refused_second_trial(self, candidate_id: str) -> str:
        """Attempt a second trial on the same case and return the refusal reason."""
        try:
            self.path.open_trial(candidate_id)
        except KernelRefusal as refusal:
            return refusal.reason
        raise HarnessInvariantError(
            f"{self.bundle.family}: a second concurrent trial was admitted"
        )

    def _ms_until_lease_expiry(self) -> int:
        """How long the outstanding permits still have to run.

        Read off the Kernel's own record rather than assumed, because the
        lease is sized from the frozen contracts: a 3 s hold and a 120 s hold
        do not get the same permit, and a scenario that advanced by a
        constant would stop testing expiry the moment a family's hold grew.
        """
        transactions = self.kernel.reduced_state().get("transactions", {})
        expiries = [
            parse_utc(record["leaseExpiry"])
            for record in transactions.values()
            if record.get("leaseExpiry")
        ]
        if not expiries:
            return 0
        remaining = (max(expiries) - parse_utc(self.clock())).total_seconds() * 1000
        return max(0, int(remaining))

    def _run_timeout(self, scenario: ScenarioName) -> ScenarioResult:
        """An expired permit lease drives the deployment to its safe state.

        The Kernel going away mid-trial is the case a lease exists for.  The
        gateway's watchdog may halt and drive to the contracted safe state; it
        may not finalize, and it may not settle -- neither of those is the
        gateway's to decide.
        """
        path = self._build(scenario=ScenarioName.POSITIVE)
        candidate_id, _ = path.request_proposal()
        trial_id, early = path.begin_trial(candidate_id)
        if early is not None:
            raise HarnessInvariantError(
                f"{self.bundle.family}: the trial ended before its lease could expire"
            )
        self.clock.advance(self._ms_until_lease_expiry() + self.case.lease_overrun_ms)
        fired = self.gateway.watchdog_check()
        if not fired or any(result.outcome is not GatewayOutcome.ACKED for result in fired):
            raise HarnessInvariantError(
                f"{self.bundle.family}: an expired lease did not drive the safe state "
                f"({[result.outcome.value for result in fired]})"
            )
        live = self.adapter.snapshot()
        if live != dict(self.bundle.safe_state):
            raise HarnessInvariantError(
                f"{self.bundle.family}: after lease expiry the deployment is at "
                f"{live}, not the contracted safe state {dict(self.bundle.safe_state)}"
            )
        if self._trial_record(trial_id).get("finalizeAcknowledged"):
            raise HarnessInvariantError(
                f"{self.bundle.family}: a watchdog finalized a trial"
            )
        return ScenarioResult(
            scenario=scenario,
            family=self.bundle.family,
            trial_id=trial_id,
            applied_config=self.applied_configuration(),
            terminal_state=TrialState(self._trial_record(trial_id)["state"]),
            outcome=TrialOutcome(self._trial_record(trial_id)["outcome"]),
            configuration=live,
            adapter_writes=len(self.adapter.writes),
            refusals=self._kernel_refusals(),
            detail=f"safe state after {self.case.lease_overrun_ms} ms lease overrun",
        )

    def _run_duplicate(self, scenario: ScenarioName) -> ScenarioResult:
        """A retransmitted commit produces no second effect.

        Task section 8, chain item 6 asks for A1 idempotency; the same property
        has to hold one layer down, or a retried policy create would apply
        twice.  The permit's derived idempotency key is what makes the replay a
        no-op, and the adapter's write count is what proves it.
        """
        path = self._build(scenario=ScenarioName.POSITIVE)
        candidate_id, _ = path.request_proposal()
        trial_id, early = path.begin_trial(candidate_id)
        if early is not None:
            raise HarnessInvariantError(
                f"{self.bundle.family}: the trial ended before the commit could be "
                "retransmitted"
            )
        commits = [
            token for token in self._issued_tokens() if token.token_kind is TokenKind.COMMIT
        ]
        if not commits:
            raise HarnessInvariantError(
                f"{self.bundle.family}: no COMMIT permit was issued"
            )
        writes_before = len(self.adapter.writes)
        replay = self.gateway.commit(token=commits[-1])
        if replay.outcome is not GatewayOutcome.ALREADY_APPLIED:
            raise HarnessInvariantError(
                f"{self.bundle.family}: a retransmitted commit returned "
                f"{replay.outcome.value}, not ALREADY_APPLIED"
            )
        if len(self.adapter.writes) != writes_before:
            raise HarnessInvariantError(
                f"{self.bundle.family}: a retransmitted commit wrote again"
            )
        path.observe(ticks=self.case.observation_ticks, tick_ms=self.case.tick_ms)
        report = path.conclude_trial(
            trial_id,
            cell_id=self.case.evidence_cell_id,
            settle_ms=self.case.settle_ms,
        )
        return ScenarioResult(
            scenario=scenario,
            family=self.bundle.family,
            applied_config=self.applied_configuration(),
            trial_id=report.trial_id,
            terminal_state=report.terminal_state,
            outcome=report.outcome,
            stop_reason=report.stop_reason,
            evidence_status=report.evidence_status,
            configuration=self.adapter.snapshot(),
            gateway_results=report.gateway_results,
            adapter_writes=len(self.adapter.writes),
            refusals=self._kernel_refusals(),
            detail="retransmitted commit reported ALREADY_APPLIED",
        )

    # -- invariants every family and every scenario must hold ---------------

    def _assert_universal(self, result: ScenarioResult, scenario: ScenarioName) -> None:
        family = self.bundle.family
        if result.evidence_grade != "HARDWARE_FREE" or result.is_ota_evidence:
            raise HarnessInvariantError(
                f"{family}/{scenario.value}: a hardware-free run reported OTA evidence"
            )
        if result.trial_id is not None and scenario is not ScenarioName.TIMEOUT:
            if result.terminal_state not in TERMINAL_STATES:
                raise HarnessInvariantError(
                    f"{family}/{scenario.value}: trial left in "
                    f"{result.terminal_state.value if result.terminal_state else None}, "
                    "which is not a terminal state"
                )
            if result.outcome is TrialOutcome.NOT_SETTLED:
                raise HarnessInvariantError(
                    f"{family}/{scenario.value}: trial ended unsettled"
                )
        issued = {
            envelope.payload["idempotencyKey"]
            for envelope in self.store.iterate()
            if envelope.event_kind == "TokenIssued"
        }
        for key in self.adapter.idempotency_keys:
            if key.split("#")[0] not in issued:
                raise HarnessInvariantError(
                    f"{family}/{scenario.value}: the equipment saw a command with no "
                    f"Kernel-issued permit ({key})"
                )
        if not isinstance(self.adapter, MockActuationAdapter):
            raise HarnessInvariantError(
                f"{family}/{scenario.value}: the matrix ran against a live adapter"
            )

    # -- joint verification for composite families -------------------------

    def joint_predicate_report(self, result: ScenarioResult) -> Mapping[str, Any]:
        """Which component predicates were judged, and in how many trials.

        Task section 8 forbids composing a combined objective from separate
        passes.  The structural reading of that rule is: every component's
        mandatory predicate ids appear in the evaluation of *one* trial.  This
        returns what is needed to assert it; the caller decides.
        """
        state = self.kernel.reduced_state()
        trial = state["trials"][result.trial_id]
        evaluation = trial["evaluation"]
        judged = set(evaluation.get("predicateVerdicts", {}))
        mandatory = set(evaluation.get("mandatoryPredicateIds", []))
        return {
            "trialCount": len(state["trials"]),
            "trialId": result.trial_id,
            "judgedPredicateIds": judged,
            "mandatoryPredicateIds": mandatory,
            "components": {
                component: set(predicate_ids)
                for component, predicate_ids in self.bundle.component_predicates.items()
            },
        }


def _tampered(message: Any, extra_body: Mapping[str, Any]) -> Any:
    """A sender that seals a body the typed message type would not build.

    The Kernel's mailbox validates the sealed canonical form, not the Python
    object, so a malformed advisory has to be produced this way: a well-formed
    message whose serialised body carries a field the schema does not have.
    """
    canonical = message.to_canonical_dict()
    canonical["body"].update(dict(extra_body))

    class Tampered:
        kind = message.kind
        issued_by = message.issued_by
        correlation_id = message.correlation_id
        epoch_hash = message.epoch_hash
        created_at = message.created_at

        def to_canonical_dict(self) -> dict:
            return canonical

    return Tampered()


class ObjectiveMatrixMixin:
    """The ten scenario tests, written once, for any family.

    A family lane's test file is::

        class TrafficSteeringMatrixTests(ObjectiveMatrixMixin, unittest.TestCase):
            def make_case(self) -> FamilyCase:
                return traffic_steering_case()

    and it inherits the whole matrix.  The six verdict-bearing scenarios are
    compared against the family's own
    :meth:`~assurance.objectives.family.ObjectiveFamilyModule.hardware_free_expectations`;
    the four refusal scenarios are asserted by the harness identically for
    every family.
    """

    def make_case(self) -> FamilyCase:
        raise NotImplementedError(
            "a family matrix test supplies its case through make_case()"
        )

    def _run(self, scenario: ScenarioName) -> ScenarioResult:
        case = self.make_case()
        harness = ObjectiveHarness(case)
        result = harness.run(scenario)
        self.harness = harness
        if scenario in VERDICT_SCENARIOS:
            expected = case.expectations.get(scenario)
            self.assertIsNotNone(
                expected,
                f"{case.family()} declares no expectation for {scenario.value}",
            )
            self.assertEqual(
                mismatches(result, expected, case.bundle),
                (),
                f"{case.family()}/{scenario.value}: {expected.rationale}",
            )
        return result

    def test_positive(self) -> None:
        self._run(ScenarioName.POSITIVE)

    def test_negative(self) -> None:
        self._run(ScenarioName.NEGATIVE)

    def test_malformed(self) -> None:
        self._run(ScenarioName.MALFORMED)

    def test_conflict(self) -> None:
        self._run(ScenarioName.CONFLICT)

    def test_stale(self) -> None:
        self._run(ScenarioName.STALE)

    def test_missing(self) -> None:
        self._run(ScenarioName.MISSING)

    def test_timeout(self) -> None:
        self._run(ScenarioName.TIMEOUT)

    def test_partial_effect(self) -> None:
        self._run(ScenarioName.PARTIAL_EFFECT)

    def test_duplicate(self) -> None:
        self._run(ScenarioName.DUPLICATE)

    def test_fault(self) -> None:
        self._run(ScenarioName.FAULT)

    def test_components_are_judged_in_one_trial(self) -> None:
        """A combined objective is never assembled out of separate passes.

        Vacuous for a single-objective family, which declares no components;
        for a composite it is the structural form of task section 8's rule.
        """
        case = self.make_case()
        if not case.bundle.component_predicates:
            self.skipTest(f"{case.family()} is not a composite objective")
        harness = ObjectiveHarness(case)
        result = harness.run(ScenarioName.POSITIVE)
        report = harness.joint_predicate_report(result)

        self.assertEqual(report["trialCount"], 1)
        for component, predicate_ids in report["components"].items():
            self.assertTrue(
                predicate_ids,
                f"component {component} names no mandatory predicate",
            )
            self.assertLessEqual(
                predicate_ids,
                report["mandatoryPredicateIds"],
                f"component {component} is not mandatory in the combined trial",
            )
            self.assertLessEqual(
                predicate_ids,
                report["judgedPredicateIds"],
                f"component {component} was not judged in trial {report['trialId']}",
            )


# --------------------------------------------------------------------------- #
# the harness's own reference case
# --------------------------------------------------------------------------- #
#
# A harness nobody has run is a claim, not a tool.  The case below drives the
# matrix over the Gate 2 vertical contract set -- the one
# ``tests/assurance/vertical_support.py`` already builds, unchanged -- so
# ``tests/assurance/test_oseam_harness.py`` proves the ten scenarios really do
# reach the terminal states they say they do, before any family lane depends
# on them.
#
# It is explicitly **not** a family record.  The bundle it wraps is the Gate 2
# fixture, not ``TrafficSteeringPreference``'s own contract set, so running it
# says nothing about that family's support state; the registry still reads
# IMPLEMENTATION_IN_PROGRESS until OBJ1 builds and runs its own.


def gate2_reference_bundle() -> ObjectiveContractBundle:
    """The Gate 2 vertical contract set, expressed as a family bundle."""
    from tests.assurance import vertical_support

    contracts = vertical_support.contract_set()
    return ObjectiveContractBundle(
        family="reference/gate2-vertical",
        counters=(contracts["counter"],),
        measurements=(contracts["measurement"],),
        target=contracts["target"],
        vector=contracts["vector"],
        release=contracts["release"],
        case_policy=contracts["case_policy"],
        watchdogs=(contracts["watchdog"],),
        harm=contracts["harm"],
        deployment=contracts["deployment"],
        actuators=(contracts["actuator"],),
        capabilities=(contracts["capability"],),
        composition=contracts["composition"],
        baseline_config=vertical_support.BASELINE_CONFIG,
        safe_state=vertical_support.SAFE_STATE,
        scope=vertical_support.SCOPE,
        sample_scope=vertical_support.SAMPLE_SCOPE,
        adapter_name=vertical_support.ADAPTER,
    )


def gate2_reference_case() -> FamilyCase:
    """The reference case: bundle, collectors and expected terminal states."""
    from tests.assurance import vertical_support

    def collector_for(scenario: ScenarioName, start: str) -> MockMeasurementSource:
        if scenario is ScenarioName.NEGATIVE:
            # Below the contracted floor, cleanly measured: a real semantic
            # failure rather than an undecidable trace.
            return vertical_support.timeseries(start=start, value=1.0)
        if scenario is ScenarioName.STALE:
            return vertical_support.failing_collector("stale", start=start)
        if scenario is ScenarioName.MISSING:
            return vertical_support.failing_collector("silent", start=start)
        return vertical_support.timeseries(start=start, value=4.0)

    expectations = {
        ScenarioName.POSITIVE: ExpectedOutcome(
            terminal_state=TrialState.SETTLED_SUCCESS,
            outcome=TrialOutcome.SUCCESS,
            evidence_status=EvidenceCellStatus.CLOSED_PASS.value,
            configuration=ExpectedConfiguration.APPLIED,
            rationale=(
                "every mandatory predicate passes over the hold, so the change is "
                "finalized live and the evidence cell closes"
            ),
        ),
        ScenarioName.NEGATIVE: ExpectedOutcome(
            terminal_state=TrialState.SETTLED_NON_SUCCESS,
            outcome=TrialOutcome.FAIL,
            stop_reason=StopReason.SEMANTIC_NON_SUCCESS,
            evidence_status=EvidenceCellStatus.CLOSED_FAIL.value,
            rationale=(
                "a cleanly measured miss is a semantic failure: reversed to the "
                "baseline, and it closes the cell as a fail rather than leaving it open"
            ),
        ),
        ScenarioName.STALE: ExpectedOutcome(
            terminal_state=TrialState.SETTLED_NON_SUCCESS,
            outcome=TrialOutcome.SAFETY_STOPPED,
            stop_reason=StopReason.TELEMETRY_STALE,
            evidence_status=EvidenceCellStatus.OPEN.value,
            rationale=(
                "stale telemetry is a safety stop, not a verdict; the obligation "
                "stays open because nothing was decided"
            ),
        ),
        ScenarioName.MISSING: ExpectedOutcome(
            terminal_state=TrialState.SETTLED_NON_SUCCESS,
            outcome=TrialOutcome.INDETERMINATE,
            stop_reason=StopReason.SEMANTIC_NON_SUCCESS,
            rationale=(
                "no observation at all is insufficient coverage, which decides "
                "nothing and fills no closure quota"
            ),
        ),
        ScenarioName.PARTIAL_EFFECT: ExpectedOutcome(
            terminal_state=TrialState.SETTLED_NON_SUCCESS,
            outcome=TrialOutcome.SAFETY_STOPPED,
            stop_reason=StopReason.PARTIAL_APPLY,
            rationale=(
                "a plan that did not land completely is reversed on every axis and "
                "settles at the baseline"
            ),
        ),
        ScenarioName.FAULT: ExpectedOutcome(
            terminal_state=TrialState.SETTLED_NON_SUCCESS,
            outcome=TrialOutcome.EXEC_ERROR,
            rationale=(
                "a PREPARE that fails aborts before the commit line, so the "
                "baseline was never left"
            ),
        ),
    }
    return FamilyCase(
        bundle=gate2_reference_bundle(),
        expectations=expectations,
        collector_for=collector_for,
        case_id=vertical_support.CASE_ID,
        evidence_cell_id=vertical_support.CELL_ID,
        start=vertical_support.START,
    )
