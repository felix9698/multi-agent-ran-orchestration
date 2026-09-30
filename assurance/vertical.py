"""The single hardware-free vertical runtime boundary (Gate 2).

Added at Gate 2; recorded in ``docs/architecture/SEAMS-GATE2.md``.

Task section 3.1 requires "다음 단일 통합 runtime 경계를 완성한다" over Operator,
Intent Agent, xApp Agent(s), Evidence Coordinator, Assurance Kernel, the single
logical Write Gateway and the Measurement Collector.  Each of those four lanes
built and tested its own half against fixtures; this module is the wiring that
makes them one path, and nothing else.  Specifically it is **not** a second
decision authority:

* it never decides a target, a verdict, a closure, a charge, a release or a
  terminal state -- every one of those is a call into
  :class:`~assurance.kernel.kernel.AssuranceKernel`;
* it never touches the equipment -- every effect is a
  :class:`~assurance.gateway.token.KernelToken` the Kernel issued, handed to
  the :class:`~assurance.gateway.write_gateway.WriteGateway`;
* it never summarises a measurement -- the collector's samples go to the
  Kernel's ingest and nowhere else (design section 4.5);
* it never lets an advisory reach anything but
  :meth:`~assurance.kernel.kernel.AssuranceKernel.submit_advisory`.

What it *does* own is the order of the steps, which is design section 7's
canonical execution flow read top to bottom, and the two things that sit
between two components and belong to neither: the
:class:`~assurance.gateway.plan.ActuationPlan` derived from a frozen candidate,
and the deployment description the plan is built against.

Determinism.  There is no clock read, no randomness and no I/O here.  ``now``
comes from an injected :class:`SteppingClock` (or any callable returning
canonical UTC), which is what lets the same script produce the same event
stream, and therefore the same terminal state hash, on every run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from assurance.advisors.catalog_view import CatalogEntry
from assurance.advisors.mailbox import seal_advisory_message
from assurance.advisors.messages import AdvisoryMessage
from assurance.contracts.catalog import Candidate
from assurance.contracts.ledgers import EvidenceContribution
from assurance.core.axes import (
    CandidateAvailability,
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
    TrialOutcome,
)
from assurance.core.envelopes import EnvelopeRejection
from assurance.core.states import PRE_COMMIT_STATES, StopReason, TrialState
from assurance.core.timebase import format_utc, parse_utc
from assurance.gateway.plan import config_hash
from assurance.gateway.token import TokenKind
from assurance.gateway.write_gateway import GatewayOutcome, GatewayResult

__all__ = [
    "SteppingClock",
    "TrialReport",
    "VerticalDeployment",
    "VerticalPath",
]

#: Gateway outcomes that mean "the operation happened as asked".
_OK = frozenset({GatewayOutcome.ACKED, GatewayOutcome.ALREADY_APPLIED})


class SteppingClock:
    """A deterministic injected clock.

    Nothing in the assurance package reads the wall clock; a run's timestamps
    come from here, so a replay of the same script is the same stream.  The
    same instance is handed to the gateway and to this module, because two
    clocks would let a lease expire in one component and not in the other.
    """

    def __init__(self, start: str) -> None:
        self._now = format_utc(parse_utc(start))

    def __call__(self) -> str:
        return self._now

    def advance(self, milliseconds: int) -> str:
        """Move the clock forward and return the new instant."""
        if milliseconds < 0:
            raise ValueError("a deterministic clock does not run backwards")
        from datetime import timedelta

        self._now = format_utc(parse_utc(self._now) + timedelta(milliseconds=milliseconds))
        return self._now


@dataclass(frozen=True)
class VerticalDeployment:
    """What the runtime knows about the deployment it acts on.

    The configuration surface and the adapter name are deployment facts, not
    Kernel decisions: the Kernel authorises a *plan over* this surface and the
    gateway verifies the surface is really live before anything is written.

    ``baseline_config`` is the configuration the runtime last durably
    established -- the starting configuration for the first trial, and the
    finalized one after a success.  It is a belief, and it is checked: a
    prepare whose baseline is not what the equipment reads back is refused
    (``REJECTED_CONFIG_MISMATCH``), which is exactly the drift detection the
    permit's expected configuration hash exists for.
    """

    adapter_name: str
    scope: Mapping[str, Any]
    baseline_config: Mapping[str, Any]
    #: Frozen declarations for the SUPPLEMENTARY axes a composition may carry,
    #: in apply order after the PRIMARY step.  Each entry is plain data:
    #:
    #: ``candidateParameter``
    #:     The name the epoch-frozen candidate carries the value under.  It is
    #:     deliberately not the axis name: a candidate parameter space holds
    #:     sortable scalars, while a configuration axis holds whatever object
    #:     the deployment's readback returns.
    #: ``axis``
    #:     The configuration-surface axis the step writes.
    #: ``adapter``
    #:     The registered client that carries it -- ``r1-cap`` for the UE DL PRB
    #:     cap.  Immutable once staged; nothing in an advisory can change it.
    #:
    #: Empty is the ordinary single-participant case, and every axis then maps
    #: one-to-one onto a candidate parameter as it always did.
    supplementary_axes: Tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", dict(self.scope))
        object.__setattr__(self, "baseline_config", dict(self.baseline_config))
        declared = tuple(dict(entry) for entry in self.supplementary_axes)
        for entry in declared:
            missing = [key for key in ("candidateParameter", "axis", "adapter")
                       if not str(entry.get(key) or "")]
            if missing:
                raise ValueError(
                    f"a supplementary axis declaration needs {missing}: {entry!r}")
        object.__setattr__(self, "supplementary_axes", declared)

    def supplementary_for(self, candidate_parameter: str) -> Optional[Mapping[str, Any]]:
        """The declaration for one candidate parameter, or ``None``."""
        for entry in self.supplementary_axes:
            if entry["candidateParameter"] == candidate_parameter:
                return entry
        return None


@dataclass(frozen=True)
class TrialReport:
    """What one trial did, as observed from the runtime boundary."""

    trial_id: str
    terminal_state: TrialState
    outcome: TrialOutcome
    stop_reason: Optional[StopReason] = None
    gateway_results: Tuple[Tuple[str, GatewayOutcome], ...] = ()
    evidence_status: Optional[str] = None
    detail: str = ""

    @property
    def is_success(self) -> bool:
        return self.outcome is TrialOutcome.SUCCESS


@dataclass
class _Mailbox:
    """Sequencing state for one case's advisory mailbox."""

    object_id: str
    sequence: int = 0
    messages: int = 0
    rejections: List[EnvelopeRejection] = field(default_factory=list)


class VerticalPath:
    """One coordination case, wired end to end, hardware-free.

    Parameters
    ----------
    kernel:
        The real :class:`~assurance.kernel.kernel.AssuranceKernel`.
    gateway:
        The real :class:`~assurance.gateway.gateway.TokenBoundWriteGateway`
        over whichever adapter the run registered.
    deployment:
        :class:`VerticalDeployment`.
    case_id:
        The case already opened on *kernel*.
    clock:
        Callable returning canonical UTC; :class:`SteppingClock` in tests.
    coordinator:
        Anything implementing
        :class:`~assurance.advisors.roles.EvidenceCoordinator`.  Optional: a
        run with no coordinator is exactly the "agent removed" replay design
        section 4.3 requires to reach the same terminal state.
    collector:
        Anything implementing
        :class:`~assurance.collector.collector.MeasurementCollector`.  Bound to
        the Kernel's ingest here and to nothing else.
    """

    def __init__(
        self,
        *,
        kernel: Any,
        gateway: Any,
        deployment: VerticalDeployment,
        case_id: str,
        clock: Callable[[], str],
        coordinator: Any = None,
        collector: Any = None,
    ) -> None:
        self.kernel = kernel
        self.gateway = gateway
        self.deployment = deployment
        self.case_id = case_id
        self.clock = clock
        self.coordinator = coordinator
        self.collector = collector
        self._live_baseline: Dict[str, Any] = dict(deployment.baseline_config)
        self._mailbox = _Mailbox(object_id=f"mailbox:{case_id}")
        self._gateway_log: List[Tuple[str, GatewayOutcome]] = []
        if collector is not None:
            collector.bind_sink(self._ingest)

    # ------------------------------------------------------------------ #
    # collector -> Kernel, and nowhere else (design section 4.5)
    # ------------------------------------------------------------------ #

    def _ingest(self, sample: Any) -> None:
        self.kernel.ingest_raw_sample(sample, now=self.clock())

    def collect(self) -> Sequence[Any]:
        """Poll the collector once at the current instant."""
        if self.collector is None:
            return ()
        return self.collector.poll(now=self.clock())

    # ------------------------------------------------------------------ #
    # read-only views handed to an advisory agent
    # ------------------------------------------------------------------ #

    def _state(self) -> Mapping[str, Any]:
        return self.kernel.reduced_state()

    def _trial(self, trial_id: str) -> Mapping[str, Any]:
        return self._state()["trials"][trial_id]

    def epoch_hash(self) -> str:
        state = self._state()
        return state["epochs"][state["activeEpoch"]]["epochHash"]

    def catalog_size(self) -> int:
        """The frozen universe's cardinality -- the product, never enumerated."""
        return int((self._state().get("catalog") or {}).get("cardinality", 0))

    def candidate_entry(self, candidate_id: str) -> Optional[CatalogEntry]:
        """One frozen point with its availability, in time independent of the size."""
        from assurance.kernel.kernel import candidate_availability, frozen_candidate
        state = self._state()
        item = frozen_candidate(state.get("catalog") or {}, candidate_id)
        if item is None:
            return None
        return CatalogEntry(
            candidate=Candidate(candidate_id=item["candidateId"], target_ref=item["targetRef"],
                                option_ref=item["optionRef"], parameters=dict(item["parameters"]),
                                semantic_hash=item["semanticHash"],
                                capability_ref=item["capabilityRef"]),
            availability=CandidateAvailability(
                candidate_availability(state, candidate_id)
                or CandidateAvailability.AVAILABLE.value))

    def find_candidate(self, parameters: Mapping[str, Any]) -> Optional[CatalogEntry]:
        """The frozen point whose parameters are exactly ``parameters`` (membership)."""
        catalog = (self._state().get("catalog") or {})
        if catalog.get("domain"):
            from assurance.contracts.catalog import domain_lookup
            found = domain_lookup(catalog["domain"], parameters)
            return None if found is None else self.candidate_entry(found.candidate_id)
        wanted = {str(k): str(v) for k, v in dict(parameters).items()}
        for item in catalog.get("candidates", []):
            if {str(k): str(v) for k, v in dict(item.get("parameters") or {}).items()} == wanted:
                return self.candidate_entry(item["candidateId"])
        return None

    def spent_ids(self) -> set:
        """Points no longer AVAILABLE (tried, in flight or locked)."""
        return {str(key) for key, value in self._state()["candidateAvailability"].items()
                if value != CandidateAvailability.AVAILABLE.value}

    def spent_count(self) -> int:
        return len(self.spent_ids())

    def catalog_view(self) -> Tuple[CatalogEntry, ...]:
        """The frozen catalog with Kernel-owned availability attached.

        Linear in the cardinality: a large domain is read through
        :meth:`candidate_entry` / :meth:`find_candidate` instead.
        """
        state = self._state()
        catalog = state.get("catalog") or {}
        if catalog.get("domain"):
            return tuple(self.candidate_entry(f"candidate/{index:06d}")
                         for index in range(int(catalog.get("cardinality", 0))))
        entries = []
        for item in catalog.get("candidates", []):
            entries.append(
                CatalogEntry(
                    candidate=Candidate(
                        candidate_id=item["candidateId"],
                        target_ref=item["targetRef"],
                        option_ref=item["optionRef"],
                        parameters=dict(item["parameters"]),
                        semantic_hash=item["semanticHash"],
                        capability_ref=item["capabilityRef"],
                    ),
                    availability=CandidateAvailability(
                        state["candidateAvailability"].get(
                            item["candidateId"], CandidateAvailability.AVAILABLE.value
                        )
                    ),
                )
            )
        return tuple(entries)

    def evidence_view(self) -> Mapping[str, Any]:
        """Cell id to status, with dormant cells reported as sealed.

        A coordinator sees *that* a cell is sealed and never what is in it --
        design section 8 keeps dormant evidence sealed until its target vector
        is active, and a view that leaked the contributions would let a
        strategy plan against evidence the experiment has not released.
        """
        state = self._state()
        view: Dict[str, Any] = {}
        for cell_id, cell in sorted(state["evidenceCells"].items()):
            if cell.get("caseId") != self.case_id:
                continue
            view[cell_id] = {
                "status": cell["status"],
                "targetRef": cell["targetRef"],
                "sealed": cell.get("sealedUntilVectorRef") is not None,
            }
        return view

    def budget_view(self) -> Mapping[str, Any]:
        """The Kernel's finite limits, read-only (design section 8)."""
        state = self._state()
        case = state["cases"][self.case_id]
        trials = [
            trial for trial in state["trials"].values() if trial["caseId"] == self.case_id
        ]
        proposals = sum(
            1
            for envelope in self.kernel._event_store.iterate()  # noqa: SLF001 - read-only
            if envelope.event_kind in {"AdvisoryAccepted", "AdvisoryRejected"}
            and envelope.payload.get("correlationId") == self.case_id
        )
        return {
            "trials_used": len(trials),
            "max_trials": int(case["maxTrials"]),
            "proposals_used": proposals,
            "max_proposals": int(case["maxProposals"]),
            "deadline_at": case["deadlineAt"],
            "active_vector": case["activeVector"],
        }

    # ------------------------------------------------------------------ #
    # advisory mailbox
    # ------------------------------------------------------------------ #

    def submit(self, message: AdvisoryMessage) -> Optional[EnvelopeRejection]:
        """Seal one advisory and submit it through the Kernel mailbox.

        The only route from an agent into the Kernel.  A rejection does not
        advance the mailbox sequence -- the Kernel's admission state is built
        from accepted messages only -- but it does consume the proposal cap,
        so a malformed-output loop is finite (design section 8).
        """
        self._mailbox.messages += 1
        envelope = seal_advisory_message(
            message,
            object_id=self._mailbox.object_id,
            event_id=f"{self._mailbox.object_id}:{self._mailbox.messages}",
            sequence=self._mailbox.sequence,
            idempotency_key=f"{self._mailbox.object_id}:idem:{self._mailbox.messages}",
            timestamp=self.clock(),
        )
        rejection = self.kernel.submit_advisory(envelope, now=self.clock())
        if rejection is None:
            self._mailbox.sequence += 1
        else:
            self._mailbox.rejections.append(rejection)
        return rejection

    def request_proposal(self) -> Tuple[Optional[str], Optional[EnvelopeRejection]]:
        """Ask the coordinator for a candidate and submit its answer.

        Returns ``(candidate_id, rejection)``.  ``(None, None)`` means the
        coordinator proposed nothing, which is *not* exhaustion: whether the
        vector is exhausted is the Kernel's call
        (:meth:`~assurance.kernel.kernel.AssuranceKernel.exhaustion_certificate`).
        """
        if self.coordinator is None:
            return None, None
        message = self.coordinator.propose_next(
            catalog_view=self.catalog_view(),
            evidence_view=self.evidence_view(),
            budget_view=self.budget_view(),
            correlation_id=self.case_id,
            epoch_hash=self.epoch_hash(),
            now=self.clock(),
        )
        if message is None:
            return None, None
        rejection = self.submit(message)
        if rejection is not None:
            return None, rejection
        return message.body.candidate_id, None

    # ------------------------------------------------------------------ #
    # the actuation plan derived from a frozen candidate
    # ------------------------------------------------------------------ #

    def plan_for(self, trial_id: str) -> Dict[str, Any]:
        """Build the plan that realises this trial's frozen candidate.

        Derived, never authored: the axes and values come from the candidate
        the epoch froze, the surface from the deployment, and the watchdog set
        from the Kernel's own epoch-frozen harm contracts.  There is no field
        an advisory message could influence.
        """
        state = self._state()
        trial = state["trials"][trial_id]
        from assurance.kernel.kernel import frozen_candidate
        candidate = frozen_candidate(state["catalog"], trial["candidateId"])
        watchdogs = self.kernel._expected_watchdog_ids(  # noqa: SLF001 - derivation
            trial, state=state
        )
        # PRIMARY steps first, in the sorted order they have always had, then
        # the SUPPLEMENTARY steps in the order the deployment declared them.
        # Order is the contract: an RNTI is cell-local, so a cap written before
        # the steering readback is a cap on an identity that may not survive
        # the move, and reverse rollback unwinds this list backwards.
        order = {
            entry["candidateParameter"]: position
            for position, entry in enumerate(self.deployment.supplementary_axes)
        }
        primary_steps = []
        supplementary: List[Tuple[int, Dict[str, Any]]] = []
        for parameter, value in sorted(candidate["parameters"].items()):
            declared = self.deployment.supplementary_for(parameter)
            if declared is None:
                primary_steps.append({"axis": parameter, "value": value})
                continue
            supplementary.append((order[parameter], {
                # The value is the candidate's own, unencoded: the Kernel
                # refuses a plan whose steps do not literally realise the frozen
                # candidate, and this declaration adds only the client that
                # carries the write.
                "axis": declared["axis"],
                "value": value,
                "adapter": declared["adapter"],
            }))
        supplementary_steps = [step for _, step in sorted(supplementary, key=lambda item: item[0])]
        return {
            "adapter": self.deployment.adapter_name,
            "scope": dict(self.deployment.scope),
            "baselineConfig": dict(self._live_baseline),
            "steps": primary_steps + supplementary_steps,
            "watchdogs": list(watchdogs),
        }

    def applied_config(self, trial_id: str) -> Dict[str, Any]:
        """The configuration the staged plan produces when fully applied."""
        merged = dict(self._live_baseline)
        for step in self.plan_for(trial_id)["steps"]:
            merged[step["axis"]] = step["value"]
        return merged

    # ------------------------------------------------------------------ #
    # one gateway operation: Kernel permit in, Kernel record out
    # ------------------------------------------------------------------ #

    def _operate(self, trial_id: str, kind: TokenKind, **kwargs: Any) -> GatewayResult:
        now = self.clock()
        token = self.kernel.issue_token(trial_id, token_kind=kind, now=now)
        operation = {
            TokenKind.PREPARE: self.gateway.prepare,
            TokenKind.READY: self.gateway.ready,
            TokenKind.COMMIT: self.gateway.commit,
            TokenKind.STOP: self.gateway.stop,
            TokenKind.REVERSE_ROLLBACK: self.gateway.reverse_rollback,
            TokenKind.CONFIGURATION_REREAD: self.gateway.reread_configuration,
            TokenKind.RECOVERY_CONFIRM: self.gateway.confirm_recovery,
            TokenKind.EMERGENCY_SAFE_STATE: self.gateway.emergency_safe_state,
            TokenKind.FINALIZE_LIVE: self.gateway.finalize_live,
        }[kind]
        result = operation(token=token, **kwargs)
        self._gateway_log.append((kind.value, result.outcome))
        resource_id = self._trial(trial_id)["resourceId"]
        self.kernel.record_gateway_result(
            token, result, resource_id=resource_id, now=self.clock()
        )
        return result

    @property
    def gateway_log(self) -> Tuple[Tuple[str, GatewayOutcome], ...]:
        """Every gateway operation this run performed, in order."""
        return tuple(self._gateway_log)

    # ------------------------------------------------------------------ #
    # the canonical flow (design section 7)
    # ------------------------------------------------------------------ #

    def open_trial(self, candidate_id: str) -> str:
        return self.kernel.open_trial(
            candidate_id=candidate_id, case_id=self.case_id, now=self.clock()
        )

    def reserve_and_stage(self, trial_id: str) -> None:
        """Validate, reserve harm and locks, and bind the actuation plan."""
        now = self.clock()
        self.kernel.advance_trial(trial_id, TrialState.VALIDATING, now=now)
        self.kernel.reserve(trial_id, now=now)
        self.kernel.advance_trial(trial_id, TrialState.RESERVED, now=now)
        self.kernel.stage_actuation_plan(
            trial_id, plan=self.plan_for(trial_id), now=now
        )
        self.kernel.advance_trial(trial_id, TrialState.PREPARING, now=now)

    def prepare(self, trial_id: str) -> GatewayResult:
        return self._operate(trial_id, TokenKind.PREPARE, plan=self.plan_for(trial_id))

    def ready(self, trial_id: str) -> GatewayResult:
        return self._operate(trial_id, TokenKind.READY)

    def commit_decision(self, trial_id: str) -> None:
        """Freeze the observed baseline and record the durable commit decision."""
        now = self.clock()
        self.kernel.advance_trial(trial_id, TrialState.READY, now=now)
        self.kernel.record_commit_readiness(
            trial_id,
            watchdogs_armed=True,
            baseline_hash=self._trial(trial_id)["readyBaselineHash"],
            now=now,
        )
        self.kernel.advance_trial(trial_id, TrialState.COMMIT_DECIDED, now=now)

    def commit(self, trial_id: str) -> GatewayResult:
        return self._operate(trial_id, TokenKind.COMMIT)

    def enter_observation(self, trial_id: str) -> None:
        """Walk the post-apply states up to the start of the hold window."""
        for target in (
            TrialState.APPLYING,
            TrialState.APPLIED_PENDING_RESULT,
            TrialState.SETTLING,
            TrialState.OBSERVING,
        ):
            self.kernel.advance_trial(trial_id, target, now=self.clock())

    def decide(self, trial_id: str, *, stop_reasons: Sequence[StopReason] = ()) -> TrialState:
        """Evaluate raw traces and apply safety precedence."""
        now = self.clock()
        self.kernel.advance_trial(trial_id, TrialState.DECISION_HOLD, now=now)
        self.kernel.evaluate_trial(trial_id, now=now)
        return self.kernel.decide_trial(trial_id, stop_reasons=stop_reasons, now=now)

    # -- terminal branches -------------------------------------------------

    def abort_pre_commit(
        self,
        trial_id: str,
        *,
        outcome: TrialOutcome,
        detail: str = "",
        cell_id: Optional[str] = None,
    ) -> TrialReport:
        """Exit before the commit line: nothing applied, reserves returned."""
        self.kernel.advance_trial(
            trial_id, TrialState.PRE_COMMIT_ABORT, now=self.clock()
        )
        status = self.settle(trial_id, outcome=outcome, cell_id=cell_id)
        return self._report(trial_id, evidence_status=status, detail=detail)

    #: Why the last :meth:`stop_and_rollback` gave up, for the lockdown that reports it.
    _rollback_failure: str = ""

    def stop_and_rollback(self, trial_id: str) -> bool:
        """Stop, reverse the change, reread and confirm a known safe state.

        Returns ``True`` when recovery verified; ``False`` when the
        transaction was placed in incident lockdown, which is a terminal state
        for the trial and blocks the case from starting another (design
        section 8).
        """
        transaction_id = self._trial(trial_id)["transactionId"]
        self._operate(trial_id, TokenKind.STOP)
        # A halt deliberately reports no observation (assurance/gateway: "a
        # digest from before the halt is not what the gateway read back"), and
        # an adapter whose halt *is* a withdrawal -- the R1 policy path -- puts
        # the baseline back while the Kernel still believes the applied
        # configuration is live.  Naming that stale digest on the reverse
        # permit is refused as REJECTED_CONFIG_MISMATCH ("the permit names a
        # configuration that is not live; reread first") and a recovered
        # deployment is filed as an incident.  So learn the live configuration
        # first; the Kernel allows the reread from STOPPING for exactly this.
        self._operate(trial_id, TokenKind.CONFIGURATION_REREAD)
        self.kernel.advance_trial(
            trial_id, TrialState.REVERSE_ROLLBACK, now=self.clock()
        )
        rollback = self._operate(trial_id, TokenKind.REVERSE_ROLLBACK)
        if rollback.outcome not in _OK:
            self._rollback_failure = (
                f"the reverse rollback answered {rollback.outcome.value}"
                + (f": {rollback.detail}" if rollback.detail else "")
            )
            self.kernel.resolve_recovery(
                transaction_id, resolution="INCIDENT_LOCKDOWN", now=self.clock()
            )
            return False
        self.kernel.advance_trial(
            trial_id, TrialState.RECOVERY_VERIFYING, now=self.clock()
        )
        return self.verify_recovery(trial_id)

    def verify_recovery(self, trial_id: str) -> bool:
        """Reread and confirm from ``RECOVERY_VERIFYING``."""
        transaction_id = self._trial(trial_id)["transactionId"]
        reread = self._operate(trial_id, TokenKind.CONFIGURATION_REREAD)
        if not self._trial(trial_id).get("recoveryConfigurationReread"):
            self._rollback_failure = (
                "the rollback was taken but the reread that has to confirm it did not "
                f"land ({reread.outcome.value}"
                + (f": {reread.detail}" if reread.detail else "") + ")"
            )
            self.kernel.resolve_recovery(
                transaction_id, resolution="INCIDENT_LOCKDOWN", now=self.clock()
            )
            return False
        confirm = self._operate(trial_id, TokenKind.RECOVERY_CONFIRM)
        if not self._trial(trial_id).get("recoveryVerified"):
            self._rollback_failure = (
                "the rollback was read back but the Kernel did not accept it as recovery "
                f"({confirm.outcome.value}"
                + (f": {confirm.detail}" if confirm.detail else "") + ")"
            )
            self.kernel.resolve_recovery(
                transaction_id, resolution="INCIDENT_LOCKDOWN", now=self.clock()
            )
            return False
        self.kernel.resolve_recovery(
            transaction_id, resolution="ROLLED_BACK", now=self.clock()
        )
        return True

    def finalize_live(self, trial_id: str) -> bool:
        """Reread the live configuration and take the finalize acknowledgement.

        Design section 7.9 and task section 6.9: an acknowledgement alone is
        not a success.  Returns ``False`` when either half is missing, in
        which case the caller owes the rollback path.
        """
        self._operate(trial_id, TokenKind.CONFIGURATION_REREAD)
        if not self._trial(trial_id).get("configurationReread"):
            return False
        self._operate(trial_id, TokenKind.FINALIZE_LIVE)
        return bool(self._trial(trial_id).get("finalizeAcknowledged"))

    def emergency_safe_state(self, trial_id: str) -> GatewayResult:
        """Drive the deployment to its contracted safe configuration."""
        return self._operate(trial_id, TokenKind.EMERGENCY_SAFE_STATE)

    def emergency_stop(
        self, trial_id: str, *, cell_id: Optional[str] = None
    ) -> TrialReport:
        """The Operator's Emergency Stop, as one control (task section 9.10).

        ``Kernel OPERATOR_ABORT -> Write Gateway stop -> rollback -> recovery``
        in that order, ending in a terminal state either way.

        The terminal is decided by what the deployment is *observed* to be in
        afterwards, not by the fact that the safe state was requested:

        * the contracted safe state is the trial's baseline -- nothing of the
          change survives -- so recovery verifies and the trial settles
          ``OPERATOR_ABORTED``;
        * it is not the baseline, so recovery cannot establish the known state
          the next trial would start from, and the transaction goes to
          ``INCIDENT_LOCKDOWN``.  Design section 8 calls that recovery failure,
          and it is one of the six finite case terminals.

        Before the commit line there is no configuration change to reverse
        (``PRE_COMMIT_STATES``), so the control is a pre-commit abort and the
        gateway is not driven at all -- driving a deployment to a safe state it
        is already in would be a write nobody needed.
        """
        current = TrialState(self._trial(trial_id)["state"])
        if current in PRE_COMMIT_STATES:
            return self.abort_pre_commit(
                trial_id,
                outcome=TrialOutcome.OPERATOR_ABORTED,
                detail="emergency stop before the commit line; nothing applied",
                cell_id=cell_id,
            )

        transaction_id = self._trial(trial_id)["transactionId"]
        self.emergency_safe_state(trial_id)

        current = TrialState(self._trial(trial_id)["state"])
        if current not in {
            TrialState.STOPPING,
            TrialState.REVERSE_ROLLBACK,
            TrialState.RECOVERY_VERIFYING,
        }:
            self.kernel.advance_trial(
                trial_id,
                TrialState.STOPPING,
                reason=StopReason.OPERATOR_ABORT,
                now=self.clock(),
            )
            current = TrialState.STOPPING
        if current is TrialState.STOPPING:
            self.kernel.advance_trial(
                trial_id, TrialState.REVERSE_ROLLBACK, now=self.clock()
            )
            current = TrialState.REVERSE_ROLLBACK
        if current is TrialState.REVERSE_ROLLBACK:
            self.kernel.advance_trial(
                trial_id, TrialState.RECOVERY_VERIFYING, now=self.clock()
            )

        if not self.verify_recovery(trial_id):
            return self._report(
                trial_id,
                detail="emergency safe state is not the baseline; incident lockdown",
            )
        status = self.settle(
            trial_id, outcome=TrialOutcome.OPERATOR_ABORTED, cell_id=cell_id
        )
        return self._report(
            trial_id,
            evidence_status=status,
            detail=f"emergency stop settled from {transaction_id}",
        )

    def record_evidence(self, trial_id: str, *, cell_id: str) -> Optional[str]:
        """Record this trial's contribution to one evidence obligation.

        The contribution restates the Kernel's own evaluation; the Kernel
        refuses a contribution that does not match it
        (``EVIDENCE_EVALUATION_MISMATCH``), so this cannot become a second
        opinion about what the trace showed.
        """
        trial = self._trial(trial_id)
        evaluation = trial["evaluation"]
        mandatory = tuple(evaluation.get("mandatoryPredicateIds", []))
        verdicts = [
            evaluation.get("predicateVerdicts", {}).get(predicate_id)
            for predicate_id in mandatory
        ]
        if verdicts and all(item == PredicateVerdict.PASS.value for item in verdicts):
            verdict = PredicateVerdict.PASS
        elif any(item == PredicateVerdict.FAIL.value for item in verdicts):
            verdict = PredicateVerdict.FAIL
        else:
            verdict = PredicateVerdict.INDETERMINATE
        contribution = EvidenceContribution(
            contribution_id=f"contribution/{trial_id}",
            trial_ref=trial_id,
            candidate_semantic_hash=trial["candidateSemanticHash"],
            execution_validity=ExecutionValidity(evaluation["executionValidity"]),
            measurement_sufficiency=MeasurementSufficiency(
                evaluation["measurementSufficiency"]
            ),
            predicate_verdict=verdict,
            trace_refs=tuple(evaluation.get("traceRefs", [])),
            dependency_group=f"group/{trial_id}",
        )
        if not contribution.trace_refs:
            return None
        status = self.kernel.close_evidence(
            cell_id=cell_id, contribution=contribution, now=self.clock()
        )
        return status.value

    def settle(
        self, trial_id: str, *, outcome: TrialOutcome, cell_id: Optional[str] = None
    ) -> Optional[str]:
        """The one idempotent settlement event (design section 7 step 10).

        The terminal trial state comes from the settlement itself -- the
        reducer moves the trial when the event lands -- so there is no second
        transition to make here, and no window in which a settled trial is
        still in ``SETTLEMENT``.

        The evidence contribution is recorded from inside ``SETTLEMENT`` and
        before the settlement event, because that one event carries the
        evidence update with the outcome, the charge and the released locks
        (task section 6.11).  Recording it afterwards would be a second write
        with its own crash window.
        """
        now = self.clock()
        self.kernel.advance_trial(trial_id, TrialState.SETTLEMENT, now=now)
        status = (
            self.record_evidence(trial_id, cell_id=cell_id)
            if cell_id is not None
            else None
        )
        self.kernel.settle_trial(trial_id, outcome=outcome, now=now)
        return status

    # ------------------------------------------------------------------ #
    # the whole flow
    # ------------------------------------------------------------------ #

    def begin_trial(self, candidate_id: str) -> Tuple[str, Optional[TrialReport]]:
        """Open one trial and drive it up to the start of the hold window.

        Returns ``(trial_id, None)`` when the change is applied and the trial
        is observing, and ``(trial_id, report)`` when a pre-commit refusal or
        an unacknowledged commit already took it to a terminal state.

        Split out of :meth:`run_trial` at Gate 3 so a caller that has to stay
        responsive between Kernel steps -- the Cockpit's submission path, which
        polls and offers an Emergency Stop -- drives *this* order rather than
        writing its own.  The step sequence and the events it produces are
        unchanged; ``run_trial`` is now this method plus :meth:`observe` plus
        :meth:`conclude_trial`, in that order.
        """
        trial_id = self.open_trial(candidate_id)
        self.reserve_and_stage(trial_id)

        prepared = self.prepare(trial_id)
        if prepared.outcome not in _OK:
            return trial_id, self.abort_pre_commit(
                trial_id,
                outcome=TrialOutcome.EXEC_ERROR,
                detail=f"prepare: {prepared.outcome.value} {prepared.detail}",
            )
        ready = self.ready(trial_id)
        if ready.outcome not in _OK:
            return trial_id, self.abort_pre_commit(
                trial_id,
                outcome=TrialOutcome.EXEC_ERROR,
                detail=f"ready: {ready.outcome.value} {ready.detail}",
            )
        self.commit_decision(trial_id)

        committed = self.commit(trial_id)
        if not self._trial(trial_id).get("commitAcknowledged"):
            return trial_id, self._post_commit_recovery(
                trial_id, detail=f"commit: {committed.outcome.value} {committed.detail}"
            )

        self.enter_observation(trial_id)
        return trial_id, None

    def observe(self, *, ticks: int = 1, tick_ms: int = 1000) -> None:
        """Advance the clock and poll the collector, ``ticks`` times."""
        for _ in range(ticks):
            self.clock.advance(tick_ms)
            self.collect()

    def conclude_trial(
        self,
        trial_id: str,
        *,
        cell_id: Optional[str] = None,
        settle_ms: int = 1000,
        stop_reasons: Sequence[StopReason] = (),
    ) -> TrialReport:
        """Settle the hold, evaluate, and take whichever terminal branch holds.

        The branch is the Kernel's: :meth:`decide` is what says whether this
        trial finalizes live or stops, and every outcome below is read back out
        of the Kernel's own axes.  Nothing here is a second opinion.
        """
        self.clock.advance(settle_ms)

        decided = self.decide(trial_id, stop_reasons=stop_reasons)
        if decided is TrialState.FINALIZING_LIVE:
            applied = self.applied_config(trial_id)
            if self.finalize_live(trial_id):
                self._live_baseline = applied
                status = self.settle(
                    trial_id, outcome=TrialOutcome.SUCCESS, cell_id=cell_id
                )
                return self._report(
                    trial_id, evidence_status=status, detail="finalized live"
                )
            # A finalize the gateway could not acknowledge already drives the
            # trial to STOPPING through the recorded gateway result; only an
            # unacknowledged-but-not-stopped finalize needs the transition.
            if TrialState(self._trial(trial_id)["state"]) is TrialState.FINALIZING_LIVE:
                self.kernel.advance_trial(
                    trial_id,
                    TrialState.STOPPING,
                    reason=StopReason.EXECUTION_ERROR,
                    now=self.clock(),
                )
        return self._settle_non_success(trial_id, cell_id=cell_id)

    def run_trial(
        self,
        candidate_id: str,
        *,
        cell_id: Optional[str] = None,
        observation_ticks: int = 4,
        tick_ms: int = 1000,
        settle_ms: int = 1000,
    ) -> TrialReport:
        """Admission to settlement for one candidate.

        Every branch ends in a terminal trial state: success finalization, a
        pre-commit abort, a rollback settlement, or incident lockdown.  There
        is no path that leaves the trial in flight.
        """
        trial_id, report = self.begin_trial(candidate_id)
        if report is not None:
            return report
        self.observe(ticks=observation_ticks, tick_ms=tick_ms)
        return self.conclude_trial(trial_id, cell_id=cell_id, settle_ms=settle_ms)

    def _post_commit_recovery(self, trial_id: str, *, detail: str) -> TrialReport:
        """Resolve a commit that did not land cleanly, through the Kernel.

        The same restart sweep design section 8 requires -- query the
        transaction, then abort, finalize, roll back or lock down -- because
        "the commit result did not arrive" and "the process died before it
        arrived" are the same uncertainty and deserve the same resolution.
        """
        unresolved = self.kernel.recover(now=self.clock())
        state = self._trial(trial_id)
        if TrialState(state["state"]) is TrialState.INCIDENT_LOCKDOWN or unresolved:
            return self._report(trial_id, detail=detail)
        self.settle(trial_id, outcome=TrialOutcome.SAFETY_STOPPED)
        return self._report(trial_id, detail=detail)

    def _settle_non_success(
        self, trial_id: str, *, cell_id: Optional[str] = None
    ) -> TrialReport:
        """Stop, reverse, verify, and settle a post-commit non-success."""
        stop_reason = self._trial(trial_id).get("stopReason")
        self._rollback_failure = ""
        if not self.stop_and_rollback(trial_id):
            # 13 of the 18 incident lockdowns on record say exactly "rollback failed;
            # incident lockdown" and nothing else, and the three ways to get here are
            # different faults with different remedies: the reverse write was refused,
            # the reread after it did not land, or the Kernel would not accept the
            # reread as recovery.  Each of those already has a detail; carry it.
            return self._report(
                trial_id,
                detail="rollback failed; incident lockdown"
                + (f" ({self._rollback_failure})" if self._rollback_failure else ""),
            )
        outcome = self._non_success_outcome(trial_id, stop_reason)
        status = self.settle(trial_id, outcome=outcome, cell_id=cell_id)
        return self._report(trial_id, evidence_status=status,
                            detail=self._non_success_detail(trial_id, outcome, stop_reason))

    def _non_success_detail(
        self, trial_id: str, outcome: TrialOutcome, stop_reason: Optional[str]
    ) -> str:
        """Which axis decided this non-success, for the record to carry.

        :meth:`_non_success_outcome` reads three separate axes and a predicate
        table to reach its answer and then threw the reasoning away: attempt 150
        of 2026-09-16 settled ``SETTLED_NON_SUCCESS`` with an empty detail on a
        trial whose KPIs had passed **every** target T0-T7, so the record said a
        control attained the original requirement and was rolled back anyway,
        without saying by what.  The outcome is not the reason; this is.
        """
        evaluation = self._trial(trial_id)["evaluation"]
        parts = [str(getattr(outcome, "value", outcome))]
        if stop_reason:
            parts.append(f"stopped on {stop_reason}")
        validity = str(evaluation.get("executionValidity") or "")
        sufficiency = str(evaluation.get("measurementSufficiency") or "")
        if validity and validity != ExecutionValidity.VALID.value:
            parts.append(f"executionValidity={validity}")
        if sufficiency and sufficiency != MeasurementSufficiency.SUFFICIENT.value:
            parts.append(f"measurementSufficiency={sufficiency}")
        verdicts = evaluation.get("predicateVerdicts", {}) or {}
        mandatory = list(evaluation.get("mandatoryPredicateIds", []) or [])
        undecided = [p for p in mandatory
                     if verdicts.get(p) == PredicateVerdict.INDETERMINATE.value]
        failed = [p for p in mandatory
                  if verdicts.get(p) == PredicateVerdict.FAIL.value]
        if undecided:
            parts.append("undecided mandatory predicates: " + ", ".join(sorted(undecided)))
        if failed:
            parts.append("failed mandatory predicates: " + ", ".join(sorted(failed)))
        if len(parts) == 1:
            # Every axis is clean and no mandatory predicate objected, so the
            # settlement came from the optional predicates or the target itself.
            parts.append("every axis valid and no mandatory predicate objected")
        return "; ".join(parts)[:400]

    def _non_success_outcome(
        self, trial_id: str, stop_reason: Optional[str]
    ) -> TrialOutcome:
        """Map the Kernel's own axes onto the settled outcome.

        The three axes stay separate (design section 8): an invalid execution
        is not a failed predicate, and an undecidable trace is neither.
        """
        evaluation = self._trial(trial_id)["evaluation"]
        validity = ExecutionValidity(evaluation["executionValidity"])
        sufficiency = MeasurementSufficiency(evaluation["measurementSufficiency"])
        if stop_reason == StopReason.OPERATOR_ABORT.value:
            return TrialOutcome.OPERATOR_ABORTED
        if stop_reason in {
            StopReason.HARM_LIMIT_BREACH.value,
            StopReason.HARD_SAFETY_GUARD.value,
            StopReason.PARTIAL_APPLY.value,
            StopReason.LEASE_EXPIRY.value,
            StopReason.TELEMETRY_STALE.value,
        }:
            return TrialOutcome.SAFETY_STOPPED
        if validity is ExecutionValidity.EXEC_ERROR:
            return TrialOutcome.EXEC_ERROR
        if validity is not ExecutionValidity.VALID:
            return TrialOutcome.INVALID
        if sufficiency is not MeasurementSufficiency.SUFFICIENT:
            return TrialOutcome.INDETERMINATE
        verdicts = evaluation.get("predicateVerdicts", {})
        if any(
            verdicts.get(predicate_id) == PredicateVerdict.INDETERMINATE.value
            for predicate_id in evaluation.get("mandatoryPredicateIds", [])
        ):
            return TrialOutcome.INDETERMINATE
        mandatory = list(evaluation.get("mandatoryPredicateIds", []))
        if (stop_reason == StopReason.BASELINE_RESET.value and verdicts and mandatory
                and all(verdicts.get(p) == PredicateVerdict.PASS.value for p in mandatory)):
            return TrialOutcome.PASS_RESET
        return TrialOutcome.FAIL

    def _report(
        self,
        trial_id: str,
        *,
        evidence_status: Optional[str] = None,
        detail: str = "",
    ) -> TrialReport:
        trial = self._trial(trial_id)
        reason = trial.get("stopReason")
        return TrialReport(
            trial_id=trial_id,
            terminal_state=TrialState(trial["state"]),
            outcome=TrialOutcome(trial["outcome"]),
            stop_reason=StopReason(reason) if reason is not None else None,
            gateway_results=self.gateway_log,
            evidence_status=evidence_status,
            detail=detail,
        )

    # ------------------------------------------------------------------ #
    # case termination
    # ------------------------------------------------------------------ #

    def terminate(self) -> Any:
        return self.kernel.terminate_case(case_id=self.case_id, now=self.clock())

    def terminal_state_hash(self) -> str:
        return self.kernel.terminal_state_hash()

    def live_baseline_hash(self) -> str:
        """Digest of the configuration the runtime believes is live."""
        return config_hash(self._live_baseline)
