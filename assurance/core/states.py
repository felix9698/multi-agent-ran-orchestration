"""Trial state machine.

Complete module: pure data and pure functions, no owner.

Design section 7 fixes the canonical execution flow::

    PROPOSED -> VALIDATING -> RESERVED -> PREPARING -> READY -> COMMIT_DECIDED
    -> APPLYING -> APPLIED_PENDING_RESULT -> SETTLING -> OBSERVING
    -> DECISION_HOLD

"with dedicated branches for pre-commit abort, stop, rollback, recovery,
finalizing live, settlement, success, non-success, and incident lockdown".
Those branches are states here, not flags, because the whole point of the
replacement is that a trial's position is a single readable value that the
reducer can replay (design section 4.3) and the Cockpit can display without
inferring anything (section 11).

Two names look similar and are deliberately distinct:

``SETTLING``
    The physical settle window after apply -- the configuration is live, the
    radio has not stabilised yet, and no KPI may be read as a verdict.
``SETTLEMENT``
    The single idempotent accounting event that writes outcome, harm charge,
    reserve return, evidence update, lock and deployment disposition together
    (design section 7 step 10, task section 6.11).

The commit line is the other load-bearing boundary.  Everything up to and
including ``READY`` is side-effect-free (task section 6.2: no real
configuration change before READY), so a failure there ends in
``PRE_COMMIT_ABORT`` with nothing to roll back.  From ``COMMIT_DECIDED``
onward an apply may already be in flight, so every *non-success* exit runs
through stop, reverse rollback and recovery verification before the trial may
settle (design section 7: "All post-commit non-success paths require reverse
rollback and recovery verification before another trial").  The one exit that
does not is success, and it pays a different price: ``DECISION_HOLD`` reaches
``SETTLEMENT`` only through ``FINALIZING_LIVE``, which needs a durable success
decision, a configuration reread and a finalize acknowledgement.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Dict, FrozenSet, Iterable, Mapping, Set, Tuple

__all__ = [
    "IllegalTransitionError",
    "POST_COMMIT_STATES",
    "PRE_COMMIT_STATES",
    "SAFETY_PRECEDENCE",
    "StopReason",
    "TERMINAL_TRIAL_STATES",
    "TRIAL_TRANSITIONS",
    "TrialState",
    "assert_transition",
    "is_legal_transition",
    "is_terminal",
    "outranks_semantic_verdict",
    "reachable_from",
    "strongest_reason",
    "successors",
]


class TrialState(Enum):
    """Every explicit position a trial can hold."""

    # -- pre-commit: no equipment change has happened -----------------------
    #: The advisory proposal has been accepted into the mailbox, nothing more.
    PROPOSED = "PROPOSED"
    #: Deterministic validation against the frozen epoch and catalog.
    VALIDATING = "VALIDATING"
    #: Harm budget reserved and resource locks acquired (section 7 step 5).
    RESERVED = "RESERVED"
    #: Side-effect-free prepare in progress across all components.
    PREPARING = "PREPARING"
    #: Every component reported durable ready.  Still nothing applied.
    READY = "READY"

    # -- the commit line ----------------------------------------------------
    #: Measurement baseline frozen and the commit decision durably recorded.
    #: Past this point an apply may exist in the field even if no ack arrived.
    COMMIT_DECIDED = "COMMIT_DECIDED"

    # -- post-commit: an equipment change may exist -------------------------
    #: The Write Gateway is applying.  The trial count and harm clock start at
    #: the first real state-changing apply, exactly once (task section 6.4).
    APPLYING = "APPLYING"
    #: Applied, waiting for the authoritative result/readback correlation.
    APPLIED_PENDING_RESULT = "APPLIED_PENDING_RESULT"
    #: Physical settle window; KPIs are not yet verdict material.
    SETTLING = "SETTLING"
    #: The measurement window the predicates will be evaluated over.
    OBSERVING = "OBSERVING"
    #: Hold period completed, verdict being decided against the contract.
    DECISION_HOLD = "DECISION_HOLD"

    # -- branches -----------------------------------------------------------
    #: Pre-commit exit: validation failure, prepare failure or Operator abort
    #: before the commit line.  Nothing to roll back; reserves are returned.
    PRE_COMMIT_ABORT = "PRE_COMMIT_ABORT"
    #: Post-commit safety exit.  Entered by any :class:`StopReason`, and by a
    #: non-success verdict, because both must reverse the change.
    STOPPING = "STOPPING"
    #: The change is being reversed in the opposite order it was applied.
    REVERSE_ROLLBACK = "REVERSE_ROLLBACK"
    #: Configuration reread and recovery confirmation.  Also the landing state
    #: for a restart with an uncertain transaction (design section 8).
    RECOVERY_VERIFYING = "RECOVERY_VERIFYING"
    #: Success decided: durable decision, configuration reread, finalize ack.
    FINALIZING_LIVE = "FINALIZING_LIVE"
    #: The one idempotent settlement event (see the module docstring).
    SETTLEMENT = "SETTLEMENT"

    # -- terminal -----------------------------------------------------------
    #: Settled with a live, finalized, evidence-backed success.
    SETTLED_SUCCESS = "SETTLED_SUCCESS"
    #: Settled without success: fail, indeterminate, invalid, exec error,
    #: abort or safety stop.  The distinction lives on the outcome axis
    #: (:class:`assurance.core.axes.TrialOutcome`), not in the state name.
    SETTLED_NON_SUCCESS = "SETTLED_NON_SUCCESS"
    #: Recovery could not establish a safe known state.  New trials are
    #: blocked until the incident is resolved (design section 8).
    INCIDENT_LOCKDOWN = "INCIDENT_LOCKDOWN"


#: States in which no real configuration change can exist yet (task 6.2).
PRE_COMMIT_STATES: FrozenSet[TrialState] = frozenset(
    {
        TrialState.PROPOSED,
        TrialState.VALIDATING,
        TrialState.RESERVED,
        TrialState.PREPARING,
        TrialState.READY,
        TrialState.PRE_COMMIT_ABORT,
    }
)

#: States from the durable commit decision onward, where a change may exist in
#: the field.  Watchdog, harm and validity guards stay armed across all of
#: them until recovery verification or live finalization (task section 6.5).
POST_COMMIT_STATES: FrozenSet[TrialState] = frozenset(
    {
        TrialState.COMMIT_DECIDED,
        TrialState.APPLYING,
        TrialState.APPLIED_PENDING_RESULT,
        TrialState.SETTLING,
        TrialState.OBSERVING,
        TrialState.DECISION_HOLD,
        TrialState.STOPPING,
        TrialState.REVERSE_ROLLBACK,
        TrialState.RECOVERY_VERIFYING,
        TrialState.FINALIZING_LIVE,
    }
)

#: A trial stops here.  ``INCIDENT_LOCKDOWN`` is terminal for the trial and
#: additionally blocks the case from starting another one.
TERMINAL_TRIAL_STATES: FrozenSet[TrialState] = frozenset(
    {
        TrialState.SETTLED_SUCCESS,
        TrialState.SETTLED_NON_SUCCESS,
        TrialState.INCIDENT_LOCKDOWN,
    }
)


def _table() -> Mapping[TrialState, FrozenSet[TrialState]]:
    S = TrialState
    raw: Dict[TrialState, Tuple[TrialState, ...]] = {
        # Pre-commit.  Every step may abort; nothing needs reversing.
        S.PROPOSED: (S.VALIDATING, S.PRE_COMMIT_ABORT),
        S.VALIDATING: (S.RESERVED, S.PRE_COMMIT_ABORT),
        S.RESERVED: (S.PREPARING, S.PRE_COMMIT_ABORT),
        S.PREPARING: (S.READY, S.PRE_COMMIT_ABORT),
        S.READY: (S.COMMIT_DECIDED, S.PRE_COMMIT_ABORT),
        # The commit line.  An abort here is no longer "pre-commit": the apply
        # may already be in flight, so it exits through STOPPING, and a
        # restart with no ack lands in RECOVERY_VERIFYING.
        S.COMMIT_DECIDED: (S.APPLYING, S.STOPPING, S.RECOVERY_VERIFYING),
        # Post-commit.  A partial or unknown apply goes straight to recovery;
        # an execution error or any safety reason goes to STOPPING.
        S.APPLYING: (
            S.APPLIED_PENDING_RESULT,
            S.STOPPING,
            S.RECOVERY_VERIFYING,
        ),
        S.APPLIED_PENDING_RESULT: (S.SETTLING, S.STOPPING, S.RECOVERY_VERIFYING),
        S.SETTLING: (S.OBSERVING, S.STOPPING),
        S.OBSERVING: (S.DECISION_HOLD, S.STOPPING),
        # Success leaves through FINALIZING_LIVE.  Every non-success leaves
        # through STOPPING, because a post-commit non-success owes a reverse
        # rollback before another trial may start (design section 7).
        S.DECISION_HOLD: (S.FINALIZING_LIVE, S.STOPPING),
        S.FINALIZING_LIVE: (S.SETTLEMENT, S.STOPPING, S.RECOVERY_VERIFYING),
        S.STOPPING: (S.REVERSE_ROLLBACK, S.INCIDENT_LOCKDOWN),
        S.REVERSE_ROLLBACK: (S.RECOVERY_VERIFYING, S.INCIDENT_LOCKDOWN),
        # The four safe resolutions of an uncertain transaction (section 8):
        # aborted/finalized settle, rolled back re-runs the reversal, and an
        # unresolvable one is locked down.
        S.RECOVERY_VERIFYING: (
            S.SETTLEMENT,
            S.REVERSE_ROLLBACK,
            S.FINALIZING_LIVE,
            S.INCIDENT_LOCKDOWN,
        ),
        S.PRE_COMMIT_ABORT: (S.SETTLEMENT,),
        S.SETTLEMENT: (S.SETTLED_SUCCESS, S.SETTLED_NON_SUCCESS),
        S.SETTLED_SUCCESS: (),
        S.SETTLED_NON_SUCCESS: (),
        S.INCIDENT_LOCKDOWN: (),
    }
    return MappingProxyType({state: frozenset(nexts) for state, nexts in raw.items()})


#: The allowed-transition table.  Read-only: an illegal transition is a
#: rejected event, never a table edit at runtime (design acceptance criterion
#: 17.3 -- the Kernel "reject[s] every illegal one").
TRIAL_TRANSITIONS: Mapping[TrialState, FrozenSet[TrialState]] = _table()


class IllegalTransitionError(ValueError):
    """A trial transition is not in :data:`TRIAL_TRANSITIONS`."""

    def __init__(self, source: TrialState, target: TrialState) -> None:
        allowed = sorted(s.value for s in TRIAL_TRANSITIONS.get(source, frozenset()))
        super().__init__(
            f"{source.value} -> {target.value} is not a legal trial transition; "
            f"allowed: {allowed or ['<terminal>']}"
        )
        self.source = source
        self.target = target


def successors(state: TrialState) -> FrozenSet[TrialState]:
    """The states reachable from *state* in one step."""
    return TRIAL_TRANSITIONS[state]


def is_legal_transition(source: TrialState, target: TrialState) -> bool:
    """True when ``source -> target`` is in the table.

    Fail-closed on anything that is not a :class:`TrialState`: an unknown
    value is not a transition, it is a malformed event.
    """
    if not isinstance(source, TrialState) or not isinstance(target, TrialState):
        return False
    return target in TRIAL_TRANSITIONS[source]


def assert_transition(source: TrialState, target: TrialState) -> TrialState:
    """Return *target* if the transition is legal, else raise."""
    if not is_legal_transition(source, target):
        raise IllegalTransitionError(source, target)
    return target


def is_terminal(state: TrialState) -> bool:
    """True for the three states a trial does not leave."""
    return state in TERMINAL_TRIAL_STATES


def reachable_from(state: TrialState) -> FrozenSet[TrialState]:
    """Transitive closure of :func:`successors`, excluding *state* itself
    unless a cycle genuinely returns to it.

    Used by the seam tests to prove that every state can still reach a
    terminal state -- the structural half of "every coordination case
    terminates finitely" (design section 8).
    """
    seen: Set[TrialState] = set()
    frontier = list(TRIAL_TRANSITIONS[state])
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(TRIAL_TRANSITIONS[current])
    return frozenset(seen)


class StopReason(Enum):
    """Why a post-commit trial stops.

    Design section 7 fixes the precedence: "Telemetry staleness, harm-limit
    breach, validity exit, hard safety guard, partial apply, lease expiry,
    operator abort, and execution error outrank semantic KPI success.  They
    trigger stop and recovery without waiting for an agent or predicate
    evaluator."

    ``SEMANTIC_NON_SUCCESS`` is listed last and is the only member that does
    *not* outrank a semantic verdict -- it *is* one.  It exists so that the
    stop path has a single vocabulary whether the trial was cut short or
    simply failed its predicates.
    """

    TELEMETRY_STALE = "TELEMETRY_STALE"
    HARM_LIMIT_BREACH = "HARM_LIMIT_BREACH"
    VALIDITY_EXIT = "VALIDITY_EXIT"
    HARD_SAFETY_GUARD = "HARD_SAFETY_GUARD"
    PARTIAL_APPLY = "PARTIAL_APPLY"
    LEASE_EXPIRY = "LEASE_EXPIRY"
    OPERATOR_ABORT = "OPERATOR_ABORT"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    SEMANTIC_NON_SUCCESS = "SEMANTIC_NON_SUCCESS"
    #: Case policy, not a verdict (owner decision 2026-09-20): every trial is
    #: judged on the frozen baseline, so a judged trial is returned to it by
    #: the ordinary STOP -> REVERSE_ROLLBACK path instead of being finalized
    #: live.  The weakest reason: any other reason outranks it.
    BASELINE_RESET = "BASELINE_RESET"


#: Precedence order, strongest first.  Position is meaningful: when two
#: reasons are raised in the same evaluation the earlier one is recorded, so a
#: harm breach is never filed as a KPI failure.
SAFETY_PRECEDENCE: Tuple[StopReason, ...] = (
    StopReason.TELEMETRY_STALE,
    StopReason.HARM_LIMIT_BREACH,
    StopReason.VALIDITY_EXIT,
    StopReason.HARD_SAFETY_GUARD,
    StopReason.PARTIAL_APPLY,
    StopReason.LEASE_EXPIRY,
    StopReason.OPERATOR_ABORT,
    StopReason.EXECUTION_ERROR,
    StopReason.SEMANTIC_NON_SUCCESS,
    StopReason.BASELINE_RESET,
)


def outranks_semantic_verdict(reason: StopReason) -> bool:
    """True for the eight reasons that pre-empt predicate evaluation."""
    return reason not in (StopReason.SEMANTIC_NON_SUCCESS, StopReason.BASELINE_RESET)


def strongest_reason(reasons: Iterable[StopReason]) -> StopReason:
    """The highest-precedence reason in *reasons*.

    Raises ``ValueError`` on an empty iterable: "stopped for no reason" is not
    a recordable outcome.
    """
    ordered = [r for r in SAFETY_PRECEDENCE if r in set(reasons)]
    if not ordered:
        raise ValueError("no stop reason given")
    return ordered[0]
