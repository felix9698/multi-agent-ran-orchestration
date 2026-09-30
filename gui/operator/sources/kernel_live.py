"""The Cockpit's Kernel-backed submission path (Gate 3).

This module is the console's *new* execution wiring.  ``live.py`` submits an
intent to the old Coordinator and reads back an Eq.12 terminal; this one hands
the intent to a deterministic Intent Agent, shows the Operator the frozen
contract instance the Kernel would act on, takes one confirmation over that
instance's content hash, and then drives the Kernel's own trial loop to a
terminal state while publishing what the Kernel says about it.

Four properties are the whole point, and each has a test:

**The GUI proposes nothing and decides nothing.**  Everything reported here is
read out of :meth:`AssuranceKernel.reduced_state` or returned by
:class:`~assurance.vertical.VerticalPath`.  There is no field an operator can
edit that becomes a verdict, an evidence closure, a harm charge or a rollback
result (task section 9.9), and no path from this module to an actuator: every
effect is a Kernel permit the vertical boundary carries.

**An agent proposal and a Kernel decision never share a badge.**  The Intent
Agent's reading is a :class:`ContractPreview` carrying DRAFT quantities and
``origin`` :data:`ORIGIN_AGENT`; what the Kernel decided is an
:class:`AxisView` / :class:`SettlementView` carrying :data:`ORIGIN_KERNEL`
(task section 9.6).

**DRAFT becomes NORMATIVE in exactly one place.**  :func:`promote_to_normative`
is the only constructor of :class:`NormativeContractInstance`, it requires a
:class:`~assurance.core.confirmation.ConfirmationRecord` that is still valid
for the preview's content hash, and design section 5's invalidation rule means
an edited preview needs a new click.

**Observation is finite polling, never a subscription.**  The Kernel already
owns the trial loop; the Cockpit polls its reduced state on a cadence derived
from the epoch's own measurement contracts and stops at the trial's terminal
state or at a contract-derived deadline, whichever comes first.  Nothing here
registers a callback with the Kernel, and the only thing the poll does with
what it reads is publish it on the state bus.

The session is constructed with a vertical path, not with a gateway or a
deployment: which endpoint is behind it, and whether that endpoint is a live
deployment or the hardware-free mock adapter, is a composition fact this module
is told (:data:`KERNEL_SESSION_MODES`) and never decides.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field, replace as dc_replace
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from assurance.advisors.deterministic_agents import DeterministicIntentAgent
from assurance.advisors.grammar import IntentGrammarEntry, IntentParseError
from assurance.core.addressing import content_hash
from assurance.core.axes import (
    CandidateAvailability,
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
    TrialOutcome,
)
from assurance.core.confirmation import ConfirmationAction, ConfirmationRecord
from assurance.core.states import TERMINAL_TRIAL_STATES, TrialState

from gui.operator import status as st

# --------------------------------------------------------------------------- #
# Mode: which data this session is showing (task section 9.7)
# --------------------------------------------------------------------------- #

#: A real deployment behind the Write Gateway.
MODE_LIVE: str = "LIVE"
#: A recorded event stream re-reduced; nothing was actuated in this run.
MODE_REPLAY: str = "REPLAY"
#: The hardware-free mock actuation adapter.  A complete Kernel path with a
#: simulated deployment: every Kernel decision is real, the equipment is not.
MODE_MOCK: str = "MOCK"

KERNEL_SESSION_MODES: Tuple[str, ...] = (MODE_LIVE, MODE_REPLAY, MODE_MOCK)

#: Mode badge colouring.  ``LIVE`` is the only mode that resolves to ``OK``;
#: every other mode carries a distinct glyph *and* a stated reason, which is
#: the console's standing rule that a non-live reading must never be able to
#: look like a live one.
MODE_STATUS: Mapping[str, str] = {
    MODE_LIVE: st.OK,
    MODE_REPLAY: st.NOT_APPLICABLE,
    MODE_MOCK: st.LAB_HARDWARE,
}

MODE_REASON: Mapping[str, str] = {
    MODE_LIVE: "live deployment behind the Write Gateway",
    MODE_REPLAY: "recorded event stream; nothing was actuated in this run",
    MODE_MOCK: "hardware-free mock actuation adapter; no O-RAN endpoint was "
               "contacted",
}


def mode_badge(mode: str) -> st.Resolved:
    """Resolve one session mode onto the shared status vocabulary.

    An unrecognised mode resolves to ``UNKNOWN`` with the vocabulary's own
    unmapped reason rather than to anything reassuring.
    """
    status = MODE_STATUS.get(mode)
    if status is None:
        return st.resolve(str(mode))
    return st.resolve(status, reason=MODE_REASON.get(mode))


# --------------------------------------------------------------------------- #
# Origin: who said it (task section 9.6)
# --------------------------------------------------------------------------- #

#: An advisory agent's reading.  Never authority for anything.
ORIGIN_AGENT: str = "AGENT_PROPOSAL"
#: The Operator's confirmation of a content hash.
ORIGIN_OPERATOR: str = "OPERATOR_CONFIRMATION"
#: The Assurance Kernel's own decision, read out of its reduced state.
ORIGIN_KERNEL: str = "KERNEL_DECISION"

ORIGIN_LABELS: Mapping[str, str] = {
    ORIGIN_AGENT: "Agent proposal",
    ORIGIN_OPERATOR: "Operator confirmation",
    ORIGIN_KERNEL: "Kernel decision",
}

#: Document status of a quantity, in the contract package's spelling.
DRAFT: str = "DRAFT"
NORMATIVE: str = "NORMATIVE"


# --------------------------------------------------------------------------- #
# Axis projection: four axes, four fields, never one
# --------------------------------------------------------------------------- #
# Design section 8 forbids collapsing these, and the collapse is exactly what a
# GUI is tempted to do because one badge is easier to draw than four.  Each axis
# gets its own table so that a value missing from one cannot borrow another's.

EXECUTION_VALIDITY_STATUS: Mapping[str, str] = {
    ExecutionValidity.NOT_EVALUATED.value: st.UNKNOWN,
    ExecutionValidity.VALID.value: st.OK,
    ExecutionValidity.INVALID.value: st.BLOCKED,
    ExecutionValidity.EXEC_ERROR.value: st.ERROR,
}

MEASUREMENT_SUFFICIENCY_STATUS: Mapping[str, str] = {
    MeasurementSufficiency.NOT_EVALUATED.value: st.UNKNOWN,
    MeasurementSufficiency.SUFFICIENT.value: st.OK,
    MeasurementSufficiency.INSUFFICIENT_COVERAGE.value: st.DEGRADED,
    MeasurementSufficiency.STALE.value: st.STALE,
    MeasurementSufficiency.MISSING_INTERVAL.value: st.DEGRADED,
    MeasurementSufficiency.CLOCK_UNHEALTHY.value: st.DEGRADED,
    MeasurementSufficiency.SCOPE_MISMATCH.value: st.BLOCKED,
}

PREDICATE_VERDICT_STATUS: Mapping[str, str] = {
    PredicateVerdict.NOT_EVALUATED.value: st.UNKNOWN,
    PredicateVerdict.PASS.value: st.OK,
    PredicateVerdict.FAIL.value: st.ERROR,
    # Never promoted to FAIL and never to OK: nothing was proven either way.
    PredicateVerdict.INDETERMINATE.value: st.UNKNOWN,
}

TRIAL_OUTCOME_STATUS: Mapping[str, str] = {
    TrialOutcome.NOT_SETTLED.value: st.UNKNOWN,
    TrialOutcome.SUCCESS.value: st.OK,
    TrialOutcome.FAIL.value: st.ERROR,
    TrialOutcome.INDETERMINATE.value: st.UNKNOWN,
    TrialOutcome.INVALID.value: st.BLOCKED,
    TrialOutcome.EXEC_ERROR.value: st.ERROR,
    TrialOutcome.OPERATOR_ABORTED.value: st.DEGRADED,
    TrialOutcome.SAFETY_STOPPED.value: st.BLOCKED,
    TrialOutcome.RECOVERY_FAILED.value: st.ERROR,
}

#: axis field name -> its table.  Named as data so a test can assert the four
#: are disjoint projections rather than one status wearing four labels.
AXIS_TABLES: Mapping[str, Mapping[str, str]] = {
    "execution_validity": EXECUTION_VALIDITY_STATUS,
    "measurement_sufficiency": MEASUREMENT_SUFFICIENCY_STATUS,
    "predicate_verdict": PREDICATE_VERDICT_STATUS,
    "trial_outcome": TRIAL_OUTCOME_STATUS,
}


def axis_status(axis: str, value: Any) -> st.Resolved:
    """Resolve one axis value.  Unknown axis or value fails to ``UNKNOWN``."""
    table = AXIS_TABLES.get(axis)
    if table is None:
        return st.resolve(st.UNKNOWN, reason=st.UNMAPPED_REASON)
    status = table.get(str(value))
    if status is None:
        return st.resolve(st.UNKNOWN, reason=st.UNMAPPED_REASON)
    return st.resolve(status)


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


class SubmissionRefused(RuntimeError):
    """The Cockpit will not submit this, and says why.

    Carries a stable ``reason`` token so the console renders a reason rather
    than a stack trace, and so a test asserts the refusal rather than its
    prose.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class ConfirmationRequired(SubmissionRefused):
    """No valid confirmation covers the content that would be acted on."""

    def __init__(self, detail: str = "") -> None:
        super().__init__("CONFIRMATION_REQUIRED", detail)


# --------------------------------------------------------------------------- #
# What the Operator reviews
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ContractPreview:
    """The Intent Agent's reading, beside the frozen instance it maps onto.

    Two halves that must not be confused.  ``objective_family``,
    ``scope_selector``, ``draft_constraints``, ``unsupported_requests`` and
    ``explanation`` are what the *agent* proposed -- every quantity in them is
    DRAFT and none of it is admissible.  ``candidate_id``, ``parameters``,
    ``target_ref``, ``semantic_hash`` and ``epoch_hash`` are what the *epoch
    already froze*: the one catalog candidate this objective resolves to.

    :meth:`content_hash` covers both halves, which is what makes design
    section 5's invalidation rule bite -- re-reading the same sentence into a
    different candidate, or the same candidate under a different epoch,
    produces a different hash and therefore needs a new click.

    ``supplementary`` names the SUPPLEMENTARY controls this contract carries
    beside its PRIMARY action -- which UE each one acts on, which value, and
    which registered client writes it.  The cap *value* is already in
    ``parameters`` because the epoch froze it as a candidate parameter; what
    only this field can say is *whose* configuration is about to move.  An
    operator who confirmed a 12-PRB cap on one UE has not confirmed one on
    another, so it is covered by the content hash like everything else here.
    It is omitted from that hash when empty, so a submission carrying no
    supplementary action hashes exactly as it did before the field existed.

    ``preconditions`` are the named things that must happen to the deployment
    *before* this contract can be applied, stated in the operator's terms --
    "withdraw policy X (APPLIED_VERIFIED) on UE Y".  They are covered by the
    same hash for the same reason as everything else here: an operator who
    confirmed a contract whose precondition was withdrawing one policy has not
    confirmed one that withdraws a different policy, and the confirmation has
    to stop covering it.  What they *are* is a deployment fact, so they are
    supplied by composition rather than derived here.
    """

    intent_text: str
    objective_family: str
    scope_selector: Mapping[str, str]
    draft_constraints: Tuple[str, ...]
    unsupported_requests: Tuple[str, ...]
    explanation: str
    epoch_hash: str
    candidate_id: str
    target_ref: str
    option_ref: str
    parameters: Mapping[str, str]
    semantic_hash: str
    availability: str
    preconditions: Tuple[str, ...] = ()
    supplementary: Tuple[Mapping[str, str], ...] = ()
    origin: str = ORIGIN_AGENT
    document_status: str = DRAFT

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "scope_selector",
            {str(k): str(v) for k, v in dict(self.scope_selector).items()})
        object.__setattr__(
            self, "parameters",
            {str(k): str(v) for k, v in dict(self.parameters).items()})
        object.__setattr__(self, "draft_constraints",
                           tuple(str(item) for item in self.draft_constraints))
        object.__setattr__(self, "unsupported_requests",
                           tuple(str(item) for item in self.unsupported_requests))
        object.__setattr__(self, "preconditions",
                           tuple(str(item) for item in self.preconditions))
        object.__setattr__(
            self, "supplementary",
            tuple({str(key): str(value) for key, value in dict(item).items()}
                  for item in self.supplementary))

    def to_canonical_dict(self) -> Dict[str, Any]:
        """The exact content the Operator is asked to confirm."""
        return {
            "intentText": self.intent_text,
            "objectiveFamily": self.objective_family,
            "scopeSelector": dict(self.scope_selector),
            "draftConstraints": list(self.draft_constraints),
            "unsupportedRequests": list(self.unsupported_requests),
            "preconditions": list(self.preconditions),
            **({"supplementary": [dict(item) for item in self.supplementary]}
               if self.supplementary else {}),
            "epochHash": self.epoch_hash,
            "candidateId": self.candidate_id,
            "targetRef": self.target_ref,
            "optionRef": self.option_ref,
            "parameters": dict(self.parameters),
            "semanticHash": self.semantic_hash,
        }

    def content_hash(self) -> str:
        return content_hash(self.to_canonical_dict())

    @property
    def is_servable(self) -> bool:
        """True when the epoch offers this objective an available candidate."""
        return self.availability == CandidateAvailability.AVAILABLE.value


@dataclass(frozen=True)
class NormativeContractInstance:
    """A confirmed preview.  The single DRAFT -> NORMATIVE promotion.

    Constructed only by :func:`promote_to_normative`.  The invariant is checked
    here rather than there so that even a direct construction cannot produce an
    instance whose confirmation does not cover its content: design section 5's
    rule is content equality, and this dataclass is where equality is enforced.
    """

    preview: ContractPreview
    confirmation: ConfirmationRecord
    origin: str = ORIGIN_OPERATOR
    document_status: str = NORMATIVE

    def __post_init__(self) -> None:
        if not isinstance(self.confirmation, ConfirmationRecord):
            raise ConfirmationRequired("no confirmation record was supplied")
        if self.confirmation.action not in CONFIRMING_ACTIONS:
            raise ConfirmationRequired(
                f"{self.confirmation.action.value} is not a confirmation")
        if not self.confirmation.is_valid_for(self.preview.content_hash()):
            raise ConfirmationRequired(
                "the confirmed content is not the content being started")

    @property
    def candidate_id(self) -> str:
        return self.preview.candidate_id

    @property
    def content_hash(self) -> str:
        return self.preview.content_hash()


#: The two controls that confirm content.  ``ABORT`` and ``EMERGENCY_STOP`` are
#: Operator controls too, but confirming with one of them would be recording a
#: stop as an approval.
CONFIRMING_ACTIONS = frozenset({
    ConfirmationAction.REVIEW_AND_CONFIRM,
    ConfirmationAction.CONFIRM_AND_START,
})


def promote_to_normative(preview: ContractPreview, *,
                         confirmation: Optional[ConfirmationRecord]
                         ) -> NormativeContractInstance:
    """The single entry point from a DRAFT preview to a NORMATIVE instance.

    There is deliberately no other way to obtain a
    :class:`NormativeContractInstance`, and nothing downstream of this function
    accepts a :class:`ContractPreview`: an unconfirmed reading cannot be
    started, and a confirmation taken over different content cannot be reused
    for this one.
    """
    if confirmation is None:
        raise ConfirmationRequired("this content has not been confirmed")
    return NormativeContractInstance(preview=preview, confirmation=confirmation)


# --------------------------------------------------------------------------- #
# What the Kernel said
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AxisView:
    """The Kernel's separated decision axes, one field each.

    Four fields, never three and never one.  ``predicate_verdicts`` stays a
    per-predicate mapping because a mandatory pair whose halves disagree --
    which is exactly how PIN_TO_CELL catches a UE that left and came back --
    has no single verdict to show.
    """

    execution_validity: str = ExecutionValidity.NOT_EVALUATED.value
    measurement_sufficiency: str = MeasurementSufficiency.NOT_EVALUATED.value
    predicate_verdicts: Tuple[Tuple[str, str], ...] = ()
    trial_outcome: str = TrialOutcome.NOT_SETTLED.value
    hold_complete: Optional[bool] = None
    origin: str = ORIGIN_KERNEL

    @property
    def predicate_verdict(self) -> str:
        """The weakest verdict among the mandatory predicates, for the badge.

        A summary *of the fourth axis only*.  It never absorbs execution
        validity or measurement sufficiency: a trial can be ``PASS`` here and
        still be an ``INVALID`` execution, and the two are drawn separately.
        """
        values = [value for _name, value in self.predicate_verdicts]
        if not values:
            return PredicateVerdict.NOT_EVALUATED.value
        for weakest in (PredicateVerdict.FAIL.value,
                        PredicateVerdict.INDETERMINATE.value,
                        PredicateVerdict.NOT_EVALUATED.value):
            if weakest in values:
                return weakest
        return PredicateVerdict.PASS.value

    def resolved(self) -> Dict[str, st.Resolved]:
        """Each axis resolved on its own table."""
        return {
            "execution_validity": axis_status("execution_validity",
                                              self.execution_validity),
            "measurement_sufficiency": axis_status("measurement_sufficiency",
                                                   self.measurement_sufficiency),
            "predicate_verdict": axis_status("predicate_verdict",
                                             self.predicate_verdict),
            "trial_outcome": axis_status("trial_outcome", self.trial_outcome),
        }


@dataclass(frozen=True)
class SettlementView:
    """The terminal facts, as the Kernel settled them."""

    trial_state: str = ""
    outcome: str = TrialOutcome.NOT_SETTLED.value
    stop_reason: Optional[str] = None
    evidence_status: Optional[str] = None
    case_termination: Optional[str] = None
    harm_charges: Tuple[str, ...] = ()
    gateway_operations: Tuple[Tuple[str, str], ...] = ()
    detail: str = ""
    origin: str = ORIGIN_KERNEL

    @property
    def is_terminal(self) -> bool:
        return bool(self.trial_state) and self.trial_state in {
            state.value for state in TERMINAL_TRIAL_STATES}


# --------------------------------------------------------------------------- #
# The polling contract (blocking item 7: polling, never a subscription)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PollingPlan:
    """A finite poll schedule derived from the epoch's own contracts.

    ``cadence_ms`` is the fastest measurement cadence the frozen epoch names --
    polling faster than the deployment reports would only re-read the same
    state.  ``deadline_ms`` is the contracted horizon after which a trial that
    has not reached a terminal state is not going to: the hold, plus one window
    for the last window to complete, plus the freshness bound after which the
    Kernel's own staleness guard would fire.  Both are read off contracts, so a
    campaign that changes its measurement cadence changes this with it and
    nobody edits a constant.
    """

    cadence_ms: int
    hold_ms: int
    deadline_ms: int

    def __post_init__(self) -> None:
        if self.cadence_ms <= 0:
            raise SubmissionRefused("POLL_CADENCE_NOT_CONTRACTED",
                                    f"cadence {self.cadence_ms} ms")
        if self.deadline_ms < self.hold_ms:
            raise SubmissionRefused(
                "POLL_DEADLINE_SHORTER_THAN_HOLD",
                f"deadline {self.deadline_ms} ms < hold {self.hold_ms} ms")

    @property
    def observation_polls(self) -> int:
        """Polls needed for the contracted hold to complete."""
        return int(math.ceil(self.hold_ms / self.cadence_ms)) + 1

    @property
    def max_polls(self) -> int:
        """Hard bound on the loop.  Reaching it is a stated refusal."""
        return int(math.ceil(self.deadline_ms / self.cadence_ms)) + 1


def polling_plan(kernel: Any) -> PollingPlan:
    """Derive the poll schedule from the Kernel's frozen measurement contracts.

    Fails closed: an epoch with no measurement contract has no cadence to
    derive, and guessing one would be inventing an observation schedule for a
    deployment that never promised it.
    """
    contracts = (kernel.reduced_state().get("contracts") or {}).values()
    bodies = [record["body"] for record in contracts
              if record.get("family") == "MeasurementContract"]
    if not bodies:
        raise SubmissionRefused("NO_MEASUREMENT_CONTRACT_IN_EPOCH")
    cadence = min(int(body["cadenceMs"]) for body in bodies)
    hold = max(int(body["holdMs"]) for body in bodies)
    window = max(int(body["windowWidthMs"]) for body in bodies)
    freshness = max(int(body["freshnessBoundMs"]) for body in bodies)
    return PollingPlan(cadence_ms=cadence, hold_ms=hold,
                       deadline_ms=hold + window + freshness)


# --------------------------------------------------------------------------- #
# The published view
# --------------------------------------------------------------------------- #

STAGE_IDLE: str = "IDLE"
STAGE_DRAFTED: str = "DRAFTED"
STAGE_CONFIRMED: str = "CONFIRMED"
STAGE_APPLYING: str = "APPLYING"
STAGE_OBSERVING: str = "OBSERVING"
STAGE_TERMINAL: str = "TERMINAL"

STAGE_ORDER: Tuple[str, ...] = (
    STAGE_IDLE, STAGE_DRAFTED, STAGE_CONFIRMED, STAGE_APPLYING,
    STAGE_OBSERVING, STAGE_TERMINAL,
)


@dataclass(frozen=True)
class SupplementaryActionView:
    """What the Cockpit shows about one SUPPLEMENTARY action in a submission.

    Frozen and derived, like every other view model here.  Nothing on it is a
    Kernel decision and nothing here can become one: the console *reads* the
    cap the sentence asked for, the controlled UE it names, and the three
    states the Write Gateway and the producer already decided -- the durable
    binding, the corroborated readback and the rollback.  It assigns none of
    them.

    ``binding_state``/``readback_state``/``rollback_state`` are plain strings
    whose only source is the composition root's read-only observer.  An empty
    string means "the composition root did not report one", never "it is fine".
    """

    action_id: str
    adapter: str
    axis: str
    policy_type_id: str
    controlled_ue_id: str
    max_dl_prbs: int
    binding_state: str = ""
    policy_id: str = ""
    readback_state: str = ""
    rollback_state: str = ""
    e2_write_count: Optional[int] = None
    detail: str = ""

    @property
    def summary(self) -> str:
        """One display line for the Cockpit and the headless printout."""
        return (
            f"{self.action_id} {self.max_dl_prbs} PRB on {self.controlled_ue_id} "
            f"via {self.adapter} ({self.policy_type_id}); binding="
            f"{self.binding_state or '-'} readback={self.readback_state or '-'} "
            f"rollback={self.rollback_state or '-'} e2Writes="
            f"{'-' if self.e2_write_count is None else self.e2_write_count}"
        )


@dataclass(frozen=True)
class KernelSessionView:
    """One immutable snapshot of a Kernel submission, for the render layer.

    Frozen, like every other view model in this console: the render layer reads
    it and cannot write back, which is the structural half of "the GUI cannot
    modify a verdict".
    """

    mode: str
    case_id: str
    stage: str = STAGE_IDLE
    epoch_hash: str = ""
    trial_id: Optional[str] = None
    preview: Optional[ContractPreview] = None
    confirmed: Optional[NormativeContractInstance] = None
    axes: AxisView = field(default_factory=AxisView)
    settlement: Optional[SettlementView] = None
    poll_count: int = 0
    max_polls: int = 0
    cadence_ms: int = 0
    #: Confirmations the content moved out from under.  Kept rather than
    #: dropped: the stream should show that a confirmation existed and was
    #: invalidated, which is design section 5's own wording.
    invalidated_confirmations: Tuple[ConfirmationRecord, ...] = ()
    refusal: Optional[str] = None
    refusal_detail: str = ""
    #: SUPPLEMENTARY actions this submission carries, in apply order.  Empty for
    #: every submission that carries none, which keeps an existing view
    #: identical to what it was.
    supplementary: Tuple[SupplementaryActionView, ...] = ()

    @property
    def mode_status(self) -> st.Resolved:
        return mode_badge(self.mode)

    @property
    def is_live(self) -> bool:
        return self.mode == MODE_LIVE

    @property
    def polls_remaining(self) -> int:
        return max(0, self.max_polls - self.poll_count)

    @property
    def content_hash(self) -> str:
        return self.preview.content_hash() if self.preview is not None else ""

    @property
    def confirmation_valid(self) -> bool:
        """Whether the standing confirmation still covers the shown content.

        False as soon as a new draft replaces the confirmed one, which is
        design section 5's "the GUI requires a new click" expressed as
        something the render layer can grey a button on.
        """
        if self.confirmed is None or self.preview is None:
            return False
        return self.confirmed.confirmation.is_valid_for(
            self.preview.content_hash())


# --------------------------------------------------------------------------- #
# The session
# --------------------------------------------------------------------------- #


class KernelSubmissionSession:
    """One Cockpit submission, from a sentence to a settled trial.

    Constructed with an already-wired
    :class:`~assurance.vertical.VerticalPath` -- the runtime boundary that owns
    the Kernel, the Write Gateway and the collector.  This class adds the
    Operator's half of the flow and nothing else: read the sentence, show the
    frozen instance, take the confirmation, drive the Kernel's loop, poll, and
    offer an Emergency Stop while it runs.

    Parameters
    ----------
    path:
        The vertical runtime boundary.
    cell_id:
        The evidence cell this case's trial contributes to, or ``None``.
    objective_registry:
        The deterministic Intent Agent's grammar: which objective families this
        deployment recognises.  A deployment fact, supplied by composition.
    preconditions:
        Called on every draft; returns the named things that must happen to the
        deployment before this contract can be applied.  Also a deployment
        fact, and read through an injected callable for the same reason the
        registry is passed in: answering it means looking at the deployment,
        and this module may not.
    mode:
        One of :data:`KERNEL_SESSION_MODES`.  Declared by whoever built the
        path, because only they know what is behind the gateway.
    agent:
        Anything with ``draft_contract``; defaults to the deterministic
        (LLM-free) Intent Agent.  There is no model call anywhere in this file.
    supplementary:
        Plain data describing the SUPPLEMENTARY controls this submission
        carries, plus one read-only ``observe`` callable.  The console never
        reaches into the gateway itself: it calls ``observe()`` and freezes
        whatever mapping comes back, so nothing it renders can reach an
        actuator and nothing it renders is a Kernel decision.
    """

    def __init__(self, *, path: Any, cell_id: Optional[str] = None,
                 objective_registry: Mapping[str, IntentGrammarEntry],
                 mode: str = MODE_MOCK,
                 agent: Any = None,
                 publish: Optional[Callable[[str, Any], None]] = None,
                 settle_ms: int = 1000,
                 preconditions: Callable[[], Sequence[str]] = tuple,
                 supplementary: Optional[Mapping[str, Any]] = None) -> None:
        if mode not in KERNEL_SESSION_MODES:
            raise SubmissionRefused("UNKNOWN_SESSION_MODE", str(mode))
        self.path = path
        self.cell_id = cell_id
        self.objective_registry = dict(objective_registry)
        self.mode = mode
        self.agent = agent or DeterministicIntentAgent()
        self.settle_ms = int(settle_ms)
        self.preconditions = preconditions
        self._publish = publish
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._preview: Optional[ContractPreview] = None
        self._confirmed: Optional[NormativeContractInstance] = None
        self._trial_id: Optional[str] = None
        self._stage: str = STAGE_IDLE
        self._poll_count: int = 0
        self._plan: Optional[PollingPlan] = None
        self._settlement: Optional[SettlementView] = None
        self._confirmations: int = 0
        self._invalidated: Tuple[ConfirmationRecord, ...] = ()
        # Plain data plus one read-only observer, handed over by the
        # composition root.  The console never reaches into the gateway itself;
        # it calls ``observe()`` and freezes whatever mapping comes back.
        self._supplementary: Dict[str, Any] = dict(supplementary or {})

    # -- wiring ---------------------------------------------------------------

    @property
    def publisher(self) -> Optional[Callable[[str, Any], None]]:
        """Where this session currently emits, or ``None``."""
        return self._publish

    def set_publisher(self, publish: Optional[Callable[[str, Any], None]]
                      ) -> None:
        """Redirect where this session emits.  Composition-time only.

        Exists so the console can adopt a session it did not build and still
        see every poll: without it the console would have to reach into the
        session's internals, or re-read the Kernel on the render thread, and
        both are worse than one narrow setter.  It changes nothing about what
        is emitted -- only who receives it, and a caller that is *adding* a
        receiver reads :attr:`publisher` first and chains rather than
        replacing, so adopting a session cannot silently unhook whoever wired
        it.
        """
        self._publish = publish

    # -- reads --------------------------------------------------------------

    @property
    def case_id(self) -> str:
        return str(getattr(self.path, "case_id", ""))

    @property
    def trial_id(self) -> Optional[str]:
        return self._trial_id

    @property
    def preview(self) -> Optional[ContractPreview]:
        return self._preview

    @property
    def confirmed(self) -> Optional[NormativeContractInstance]:
        return self._confirmed

    def _state(self) -> Mapping[str, Any]:
        return self.path.kernel.reduced_state()

    def _trial(self) -> Mapping[str, Any]:
        if self._trial_id is None:
            return {}
        return self._state()["trials"].get(self._trial_id, {})

    # -- step 1: the agent reads the sentence -------------------------------

    def draft(self, utterance: str) -> ContractPreview:
        """Read one natural-language intent into a frozen catalog candidate.

        The agent selects an objective family from the registry; the *epoch*
        decides whether that family has a candidate, and which one.  An
        objective the deployment cannot serve is refused by name here rather
        than being drafted into a contract nobody can act on.
        """
        text = (utterance or "").strip()
        if not text:
            raise SubmissionRefused("NO_INTENT_TEXT")
        with self._lock:
            try:
                message = self.agent.draft_contract(
                    utterance=text,
                    objective_registry=self.objective_registry,
                    capability_view=self._capability_view(),
                    correlation_id=self.case_id,
                    epoch_hash=self.path.epoch_hash(),
                    now=self.path.clock())
            except IntentParseError as exc:
                raise SubmissionRefused("INTENT_NOT_RECOGNISED", str(exc))

            # The draft goes to the Kernel through the mailbox, like any other
            # advisory.  A rejection is the Kernel's answer and is shown as
            # one; the console does not retry it into acceptance.
            rejection = self.path.submit(message)
            if rejection is not None:
                raise SubmissionRefused("ADVISORY_REJECTED", rejection.value)

            body = message.body
            entry = self._catalog_entry_for(body.objective_family)
            candidate = entry.candidate
            preview = ContractPreview(
                intent_text=text,
                objective_family=body.objective_family,
                scope_selector=dict(body.scope_selector),
                draft_constraints=tuple(
                    _format_constraint(item) for item in body.proposed_constraints),
                unsupported_requests=tuple(body.unsupported_requests),
                explanation=body.explanation,
                epoch_hash=self.path.epoch_hash(),
                candidate_id=candidate.candidate_id,
                target_ref=candidate.target_ref,
                option_ref=candidate.option_ref,
                parameters=dict(candidate.parameters),
                semantic_hash=candidate.semantic_hash,
                availability=entry.availability.value,
                preconditions=tuple(self.preconditions()),
                supplementary=self._supplementary_preview())
            self._preview = preview
            self._stage = STAGE_DRAFTED
            # A new reading replaces the old one.  If it is not the content
            # that was confirmed, the standing confirmation stops covering
            # anything and the console needs a new click (design section 5).
            # The record is marked and kept rather than dropped, so what
            # happened is visible instead of merely absent.
            standing = self._confirmed
            if standing is not None and not standing.confirmation.is_valid_for(
                    preview.content_hash()):
                self._invalidated += (standing.confirmation.invalidated(),)
                self._confirmed = None
            self._emit()
            return preview

    def _capability_view(self) -> Tuple[Any, ...]:
        """Capability manifests as the epoch froze them, read-only."""
        contracts = (self._state().get("contracts") or {}).values()
        return tuple(record["body"] for record in contracts
                     if record.get("family") == "CapabilityManifest")

    def _catalog_entry_for(self, objective_family: str) -> Any:
        """The one frozen candidate this objective family resolves to.

        Fail closed on both halves: an objective with no candidate is refused,
        and so is a candidate the Kernel has marked unavailable -- the Cockpit
        does not get to start a trial the Kernel would refuse.
        """
        state = self._state()
        targets = {
            record["body"]["contractId"]: record["body"]
            for record in (state.get("contracts") or {}).values()
            if record.get("family") == "TargetContract"}
        for entry in self.path.catalog_view():
            target = targets.get(entry.candidate.target_ref) or {}
            if target.get("objectiveFamily") != objective_family:
                continue
            if entry.availability is not CandidateAvailability.AVAILABLE:
                raise SubmissionRefused(
                    "CANDIDATE_NOT_AVAILABLE",
                    f"{entry.candidate.candidate_id} is "
                    f"{entry.availability.value}")
            return entry
        raise SubmissionRefused("OBJECTIVE_NOT_IN_EPOCH", objective_family)

    # -- step 2: the Operator confirms the content --------------------------

    def confirm(self, preview: Optional[ContractPreview] = None, *,
                action: ConfirmationAction = ConfirmationAction.REVIEW_AND_CONFIRM
                ) -> NormativeContractInstance:
        """Record one confirmation over the shown content and promote it.

        The record carries the content hash, the instant and an event id, and
        nothing about who pressed the button -- design section 5 removed the
        signer model, and :data:`FORBIDDEN_CONFIRMATION_FIELDS` keeps it out.
        """
        with self._lock:
            subject = preview if preview is not None else self._preview
            if subject is None:
                raise SubmissionRefused("NOTHING_TO_CONFIRM")
            if not subject.is_servable:
                raise SubmissionRefused("CANDIDATE_NOT_AVAILABLE",
                                        subject.availability)
            self._confirmations += 1
            record = ConfirmationRecord(
                confirmed_object_type=type(subject).__name__,
                confirmed_content_hash=subject.content_hash(),
                event_id=f"confirm/{self.case_id}:{self._confirmations}",
                timestamp=self.path.clock(),
                action=action)
            instance = promote_to_normative(subject, confirmation=record)
            self._confirmed = instance
            self._preview = subject
            self._stage = STAGE_CONFIRMED
            self._emit()
            return instance

    # -- step 3: drive the Kernel's loop, polling ---------------------------

    def start(self, instance: Optional[NormativeContractInstance] = None
              ) -> KernelSessionView:
        """Run one confirmed instance to a terminal state.  **Worker thread.**

        The loop is the polling regime blocking item 7 settled on: a finite
        number of reads of the Kernel's reduced state at the contracted
        cadence, publishing each one, until the trial reports a terminal state
        or the contracted deadline passes.  Nothing is subscribed and no
        callback is registered with the Kernel; the only thing this loop pushes
        anywhere is a view onto the state bus.
        """
        with self._lock:
            confirmed = instance if instance is not None else self._confirmed
            if confirmed is None:
                raise ConfirmationRequired("nothing has been confirmed")
            if self._preview is not None and not confirmed.confirmation.is_valid_for(
                    self._preview.content_hash()):
                raise ConfirmationRequired(
                    "the preview changed after it was confirmed")
            if self._trial_id is not None:
                raise SubmissionRefused("TRIAL_ALREADY_RUN", self._trial_id)
            if self._stop.is_set():
                # Nothing has been applied, so there is nothing to stop and
                # nothing to roll back.  Refusing is the honest answer.
                raise SubmissionRefused("EMERGENCY_STOP_REQUESTED")
            plan = polling_plan(self.path.kernel)
            self._plan = plan
            self._stage = STAGE_APPLYING
            self._emit()

            trial_id, report = self.path.begin_trial(confirmed.candidate_id)
            self._trial_id = trial_id
            self._stage = STAGE_OBSERVING
            observed = 0
            concluded = False

            while report is None and self._poll_count < plan.max_polls:
                self._poll_count += 1
                self._emit()
                if self._stop.is_set():
                    report = self.path.emergency_stop(trial_id,
                                                      cell_id=self.cell_id)
                elif self._is_terminal(trial_id):
                    break
                elif observed < plan.observation_polls:
                    self.path.observe(ticks=1, tick_ms=plan.cadence_ms)
                    observed += 1
                elif not concluded:
                    concluded = True
                    report = self.path.conclude_trial(
                        trial_id, cell_id=self.cell_id,
                        settle_ms=self.settle_ms)
                # Otherwise the hold is over, the Kernel has been asked to
                # decide, and no terminal state has appeared: keep polling to
                # the contracted deadline rather than inventing an answer.

            if report is None and not self._is_terminal(trial_id):
                # The contracted horizon passed without a terminal state.  The
                # Cockpit stops polling and says so; it does not decide what
                # the trial was.
                self._stage = STAGE_OBSERVING
                view = self._view(refusal="POLL_DEADLINE_EXCEEDED",
                                  detail=f"{plan.max_polls} polls at "
                                         f"{plan.cadence_ms} ms")
                self._emit(view)
                return view

            self._settlement = self._settlement_view(report)
            self._stage = STAGE_TERMINAL
            view = self._view()
            self._emit(view)
            return view

    def _is_terminal(self, trial_id: str) -> bool:
        trial = self._state()["trials"].get(trial_id) or {}
        state = trial.get("state")
        if state is None:
            return False
        return TrialState(state) in TERMINAL_TRIAL_STATES

    # -- Operator controls --------------------------------------------------

    def request_emergency_stop(self) -> None:
        """Raise the Emergency Stop.  Honoured at the next poll boundary.

        Task section 9.10's chain -- Kernel ``OPERATOR_ABORT``, Write Gateway
        stop, rollback, recovery -- is one control on the vertical boundary
        (:meth:`~assurance.vertical.VerticalPath.emergency_stop`), and this
        raises it.  The Cockpit does not drive the gateway and does not decide
        the terminal: what the deployment is observed to be in afterwards does.
        """
        self._stop.set()

    @property
    def emergency_stop_requested(self) -> bool:
        return self._stop.is_set()

    def emergency_stop(self) -> KernelSessionView:
        """Raise the stop and act on it now if this thread can.

        Never blocks.  When a poll loop already holds the session -- which is
        the normal case, because the button is pressed while a trial is running
        -- the raised flag is honoured at that loop's next poll boundary, and
        this returns the current view.  Waiting for the loop instead would make
        the one control that must always respond the one control that hangs.
        """
        self.request_emergency_stop()
        if not self._lock.acquire(blocking=False):
            return self._view()
        try:
            trial_id = self._trial_id
            if trial_id is None or self._is_terminal(trial_id):
                self._emit()
                return self._view()
            report = self.path.emergency_stop(trial_id, cell_id=self.cell_id)
            self._settlement = self._settlement_view(report)
            self._stage = STAGE_TERMINAL
            view = self._view()
            self._emit(view)
            return view
        finally:
            self._lock.release()

    def abort(self) -> Any:
        """Terminate the case at a safe Kernel boundary.

        Refuses while a trial is in flight and names the control that is for
        that: Abort stops the case from starting more work, and stopping an
        applied change is Emergency Stop's job, not this one's.
        """
        with self._lock:
            trial_id = self._trial_id
            if trial_id is not None and not self._is_terminal(trial_id):
                raise SubmissionRefused(
                    "TRIAL_IN_FLIGHT",
                    "use Emergency Stop to stop an applied change")
            termination = self.path.terminate()
            self._stage = STAGE_TERMINAL
            self._settlement = dc_replace(self._settlement or SettlementView(),
                                          case_termination=termination.value)
            self._emit()
            return termination

    # -- projections --------------------------------------------------------

    def axes(self) -> AxisView:
        """Read the Kernel's four axes for the current trial."""
        evaluation = (self._trial() or {}).get("evaluation") or {}
        verdicts = evaluation.get("predicateVerdicts") or {}
        mandatory = tuple(evaluation.get("mandatoryPredicateIds") or ())
        names = mandatory or tuple(sorted(verdicts))
        return AxisView(
            execution_validity=str(evaluation.get(
                "executionValidity", ExecutionValidity.NOT_EVALUATED.value)),
            measurement_sufficiency=str(evaluation.get(
                "measurementSufficiency",
                MeasurementSufficiency.NOT_EVALUATED.value)),
            predicate_verdicts=tuple(
                (name, str(verdicts.get(
                    name, PredicateVerdict.NOT_EVALUATED.value)))
                for name in names),
            trial_outcome=str((self._trial() or {}).get(
                "outcome", TrialOutcome.NOT_SETTLED.value)),
            hold_complete=evaluation.get("holdComplete"))

    def _settlement_view(self, report: Any) -> SettlementView:
        trial = self._trial() or {}
        charges = tuple(
            _format_movement(entry)
            for entry in (self._state().get("harmLedger") or ())
            if entry.get("trialId") == self._trial_id)
        stop_reason = getattr(report, "stop_reason", None)
        return SettlementView(
            trial_state=str(trial.get("state", "")),
            outcome=str(trial.get("outcome", TrialOutcome.NOT_SETTLED.value)),
            stop_reason=getattr(stop_reason, "value", stop_reason),
            evidence_status=getattr(report, "evidence_status", None),
            harm_charges=charges,
            gateway_operations=tuple(
                (kind, outcome.value)
                for kind, outcome in getattr(report, "gateway_results", ())),
            detail=str(getattr(report, "detail", "")))

    def _view(self, *, refusal: Optional[str] = None,
              detail: str = "") -> KernelSessionView:
        plan = self._plan
        return KernelSessionView(
            mode=self.mode,
            case_id=self.case_id,
            stage=self._stage,
            epoch_hash=self.path.epoch_hash(),
            trial_id=self._trial_id,
            preview=self._preview,
            confirmed=self._confirmed,
            axes=self.axes(),
            settlement=self._settlement,
            poll_count=self._poll_count,
            max_polls=plan.max_polls if plan else 0,
            cadence_ms=plan.cadence_ms if plan else 0,
            invalidated_confirmations=self._invalidated,
            refusal=refusal,
            refusal_detail=detail,
            supplementary=self._supplementary_views())

    def _supplementary_preview(self) -> Tuple[Mapping[str, str], ...]:
        """The declared half of each supplementary action, for Review & Confirm.

        Declared data only -- no observation.  What the operator is confirming
        is *what this contract would do*, and an observation taken before it
        runs would be neither.
        """
        if not self._supplementary:
            return ()
        declared = self._supplementary
        return ({
            "actionId": str(declared.get("actionId", "")),
            "adapter": str(declared.get("adapter", "")),
            "axis": str(declared.get("axis", "")),
            "policyTypeId": str(declared.get("policyTypeId", "")),
            "controlledUeId": str(declared.get("controlledUeId", "")),
            "maxDlPrbs": str(declared.get("maxDlPrbs", "")),
        },)

    def _supplementary_views(self) -> Tuple[SupplementaryActionView, ...]:
        """Project the composition root's supplementary declaration, read-only."""
        if not self._supplementary:
            return ()
        declared = self._supplementary
        observed: Mapping[str, Any] = {}
        observer = declared.get("observe")
        if callable(observer):
            try:
                answer = observer()
            except Exception:  # a console never fails a run by rendering it
                answer = None
            if isinstance(answer, Mapping):
                observed = answer
        return (SupplementaryActionView(
            action_id=str(declared.get("actionId", "")),
            adapter=str(declared.get("adapter", "")),
            axis=str(declared.get("axis", "")),
            policy_type_id=str(declared.get("policyTypeId", "")),
            controlled_ue_id=str(declared.get("controlledUeId", "")),
            max_dl_prbs=int(declared.get("maxDlPrbs", 0)),
            binding_state=str(observed.get("bindingState", "")),
            policy_id=str(observed.get("policyId", "")),
            readback_state=str(observed.get("readbackState", "")),
            rollback_state=str(observed.get("rollbackState", "")),
            e2_write_count=(
                int(observed["e2WriteCount"])
                if isinstance(observed.get("e2WriteCount"), int) else None),
            detail=str(observed.get("detail", "")),
        ),)

    def view(self) -> KernelSessionView:
        """The current snapshot.  Read-only; publishing it changes nothing.

        Deliberately lock-free: the Emergency Stop path must be able to read
        the session while a poll loop holds it, and a projection that only
        reads cannot corrupt anything by observing a step boundary early.
        """
        return self._view()

    def _emit(self, view: Optional[KernelSessionView] = None) -> None:
        if self._publish is None:
            return
        try:
            self._publish("decision", view if view is not None else self._view())
        except Exception:                                  # pragma: no cover
            # Presentation is downstream and powerless: a bus that refuses a
            # record must never be able to stop a Kernel trial.
            pass


def _format_movement(entry: Mapping[str, Any]) -> str:
    """One harm-ledger movement as display text.

    The movement kind is kept beside the amount because a reserve and a charge
    are not the same fact, and a row that showed only the number would let a
    returned reserve read as a charge against the case's budget.
    """
    amount = entry.get("amount") or {}
    return (f"{entry.get('movementKind', '?')} "
            f"{entry.get('harmContractRef', '?')}: "
            f"{amount.get('value', st.PRE_MEASUREMENT)} "
            f"{amount.get('unit', '')}").strip()


def _format_constraint(constraint: Any) -> str:
    """One draft constraint as display text, with its document status shown.

    The status is part of the text on purpose.  A bound rendered without it
    reads like a threshold the system agreed to, and the whole point of a
    DRAFT quantity is that nothing has agreed to it yet.
    """
    bound = getattr(constraint, "bound", None)
    operator = getattr(getattr(constraint, "operator", None), "value", "?")
    status = getattr(getattr(bound, "document_status", None), "value", DRAFT)
    return (f"{getattr(constraint, 'measurement_ref', '?')} {operator} "
            f"{getattr(bound, 'value', '?')} {getattr(bound, 'unit', '')}"
            f" [{status}]").replace("  ", " ")


__all__ = [
    "AXIS_TABLES", "AxisView", "CONFIRMING_ACTIONS", "ConfirmationRequired",
    "ContractPreview", "DRAFT", "KERNEL_SESSION_MODES", "KernelSessionView",
    "KernelSubmissionSession", "MODE_LIVE", "MODE_MOCK", "MODE_REASON",
    "MODE_REPLAY", "MODE_STATUS", "NORMATIVE", "NormativeContractInstance",
    "ORIGIN_AGENT", "ORIGIN_KERNEL", "ORIGIN_LABELS", "ORIGIN_OPERATOR",
    "PollingPlan", "STAGE_APPLYING", "STAGE_CONFIRMED", "STAGE_DRAFTED",
    "STAGE_IDLE", "STAGE_OBSERVING", "STAGE_ORDER", "STAGE_TERMINAL",
    "SettlementView", "SubmissionRefused", "axis_status", "mode_badge",
    "polling_plan", "promote_to_normative",
]
