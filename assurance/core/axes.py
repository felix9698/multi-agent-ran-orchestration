"""The seven separated decision axes.

Complete module: pure data and pure functions, no owner.

Design section 8 opens with the rule this module exists to make structurally
impossible to break:

    "Execution validity, measurement sufficiency, predicate verdict, trial
    outcome, evidence-cell status, candidate availability, and
    candidate-vector aggregate state are separate dimensions and must not be
    collapsed.  ``INVALID``, ``INDETERMINATE``, ``FAIL``, and ``EXEC_ERROR``
    remain distinct."

Seven axes, seven Python enums.  They are not folded into one status enum and
they are not modelled as strings, because the failure mode is specific and has
already happened on the legacy path: a trial whose telemetry went missing gets
recorded as a KPI failure, that failure fills a closure quota, and the quota
makes the case look explored when nothing was actually observed.  Keeping the
axes as distinct *types* means such a collapse cannot be written by accident --
``MeasurementSufficiency.MISSING_INTERVAL`` is not equal to
``PredicateVerdict.FAIL`` and no comparison between them is ever true.

Every axis carries an explicit "not yet evaluated" member rather than using
``None``.  ``Optional[Enum]`` would put the unevaluated case into a different
type and invite ``or`` defaulting, which is how "unknown" turns into "pass".
"""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet, Tuple, Type

__all__ = [
    "AXIS_ENUMS",
    "AggregateState",
    "CandidateAvailability",
    "CaseTermination",
    "EvidenceCellStatus",
    "ExecutionValidity",
    "MeasurementSufficiency",
    "OPEN_EVIDENCE_CELL_STATUSES",
    "PredicateVerdict",
    "TrialOutcome",
    "aggregate_from_cells",
    "counts_toward_closure",
    "is_non_success",
]


# --------------------------------------------------------------------------- #
# Axis 1 - execution validity: did the trial happen the way the contract says?
# --------------------------------------------------------------------------- #

class ExecutionValidity(Enum):
    """Whether the trial's execution is usable as evidence at all.

    This axis is decided from the transaction record -- apply acknowledgement,
    readback, fencing, lease, rollback -- and never from a KPI.  A trial can
    be perfectly executed and still fail its predicates, and it can satisfy
    every predicate while being ``INVALID`` because the configuration was not
    the one the candidate names.
    """

    NOT_EVALUATED = "NOT_EVALUATED"
    #: Applied as specified, held through the window, correctly reversed or
    #: finalized.  Only a VALID execution may contribute to closure.
    VALID = "VALID"
    #: The execution violated a precondition of the contract: wrong scope,
    #: partial apply, validity region exited, stale fence, drifted baseline.
    INVALID = "INVALID"
    #: The execution path itself errored -- transport failure, gateway
    #: rejection, unreachable endpoint.  Distinct from INVALID: nothing about
    #: the candidate was tested, so it is not counted against it.
    EXEC_ERROR = "EXEC_ERROR"


# --------------------------------------------------------------------------- #
# Axis 2 - measurement sufficiency: was enough actually observed?
# --------------------------------------------------------------------------- #

class MeasurementSufficiency(Enum):
    """Whether the raw observation supports a verdict.

    Design section 8: "Invalid, unprovable, or insufficient traces do not fill
    closure quotas", and missing intervals take a conservative
    contract-defined charge rather than zero or last-value substitution.  The
    members below are the distinguishable reasons a trace does not support a
    verdict, kept separate so the Cockpit and the paper can report *why*
    evidence was thin.
    """

    NOT_EVALUATED = "NOT_EVALUATED"
    #: Complete window, healthy clock, sufficient entity count, fresh.
    SUFFICIENT = "SUFFICIENT"
    #: The window is complete but too few entities/samples to estimate.
    INSUFFICIENT_COVERAGE = "INSUFFICIENT_COVERAGE"
    #: Data arrived, but older than the contract's freshness bound.
    STALE = "STALE"
    #: A gap in the observation window (task section 5.6, section 8).
    MISSING_INTERVAL = "MISSING_INTERVAL"
    #: Collector clock drift or loss of synchronisation makes correlation
    #: unsafe; timestamps cannot be trusted to align with the trial window.
    CLOCK_UNHEALTHY = "CLOCK_UNHEALTHY"
    #: The counters belong to a different scope than the trial's.
    SCOPE_MISMATCH = "SCOPE_MISMATCH"


# --------------------------------------------------------------------------- #
# Axis 3 - predicate verdict: what did the contract's predicates say?
# --------------------------------------------------------------------------- #

class PredicateVerdict(Enum):
    """The evaluator's answer for one mandatory predicate.

    Three answers, not two.  ``INDETERMINATE`` is what an insufficient or
    invalid trace produces; it is never promoted to ``FAIL`` (which would
    charge the candidate for something never tested) nor to ``PASS``.
    """

    NOT_EVALUATED = "NOT_EVALUATED"
    PASS = "PASS"
    FAIL = "FAIL"
    #: Could not be decided from the evidence available.
    INDETERMINATE = "INDETERMINATE"


# --------------------------------------------------------------------------- #
# Axis 4 - trial outcome: how did this trial settle?
# --------------------------------------------------------------------------- #

class TrialOutcome(Enum):
    """The settled result of one trial.

    Written exactly once, by the settlement event (design section 7 step 10).
    ``SUCCESS`` additionally requires the finalize path: all mandatory
    predicates passing in the same live trial and validity region through the
    full hold period, a durable success decision, a live configuration reread
    and a finalize acknowledgement.
    """

    NOT_SETTLED = "NOT_SETTLED"
    SUCCESS = "SUCCESS"
    #: Predicates were evaluated on a valid, sufficient trace and did not pass.
    FAIL = "FAIL"
    #: Execution was valid but the evidence could not decide the predicates.
    INDETERMINATE = "INDETERMINATE"
    #: The execution violated the contract; the candidate was not tested.
    INVALID = "INVALID"
    #: The execution path errored.
    EXEC_ERROR = "EXEC_ERROR"
    #: Operator abort, before or after the commit line.
    OPERATOR_ABORTED = "OPERATOR_ABORTED"
    #: A safety reason outranking the semantic verdict stopped the trial
    #: (see :class:`assurance.core.states.StopReason`).
    SAFETY_STOPPED = "SAFETY_STOPPED"
    #: Recovery could not establish a safe known state; paired with
    #: ``TrialState.INCIDENT_LOCKDOWN``.
    RECOVERY_FAILED = "RECOVERY_FAILED"
    #: Every mandatory predicate passed on a valid, sufficient trace, and the
    #: case returned the trial to the baseline (``StopReason.BASELINE_RESET``)
    #: with recovery verified.  Not a deployed success: nothing stays live.
    PASS_RESET = "PASS_RESET"


# --------------------------------------------------------------------------- #
# Axis 5 - evidence cell status: how complete is one evidence obligation?
# --------------------------------------------------------------------------- #

class EvidenceCellStatus(Enum):
    """The state of one evidence obligation ("cell") in the ledger.

    Task section 6.16 names the four statuses that block an exhaustion
    certificate: ``OPEN``, ``PARTIAL``, ``TEMP_BLOCKED`` and ``BUDGET_LOCKED``
    produce ``EVIDENCE_INCOMPLETE`` rather than automatic relaxation.  The
    remaining members carry design section 8's evidence rules: dormant
    evidence stays sealed until its target vector is active, a historical pass
    is provisional until a full confirmation trial, and a compatible pass
    after a closed fail is appended as a witness instead of rewriting history.
    """

    #: No usable contribution yet.
    OPEN = "OPEN"
    #: Some usable contributions, below the contract's closure quota.
    PARTIAL = "PARTIAL"
    #: Temporarily unobtainable -- capability absent, deployment draining,
    #: validity region unavailable.  Not a failure and not closure.
    TEMP_BLOCKED = "TEMP_BLOCKED"
    #: Blocked by an exhausted harm reserve or trial/proposal cap.
    BUDGET_LOCKED = "BUDGET_LOCKED"
    #: Closed by sufficient passing evidence.
    CLOSED_PASS = "CLOSED_PASS"
    #: Closed by sufficient failing evidence.  A later compatible pass becomes
    #: a ``POST_CLOSURE_WITNESS``; it does not reopen this cell.
    CLOSED_FAIL = "CLOSED_FAIL"
    #: Sealed until its target vector is released (design section 8).
    DORMANT_SEALED = "DORMANT_SEALED"
    #: Carried from a prior epoch or historical run; provisional until a full
    #: confirmation trial in the current epoch.
    PROVISIONAL_HISTORICAL = "PROVISIONAL_HISTORICAL"
    #: Appended after closure; recorded, never counted into the closed quota.
    POST_CLOSURE_WITNESS = "POST_CLOSURE_WITNESS"


#: The four statuses that make an exhaustion claim ``EVIDENCE_INCOMPLETE``
#: instead of ``EXHAUSTED`` (task section 6.16).
OPEN_EVIDENCE_CELL_STATUSES: FrozenSet[EvidenceCellStatus] = frozenset(
    {
        EvidenceCellStatus.OPEN,
        EvidenceCellStatus.PARTIAL,
        EvidenceCellStatus.TEMP_BLOCKED,
        EvidenceCellStatus.BUDGET_LOCKED,
    }
)


# --------------------------------------------------------------------------- #
# Axis 6 - candidate availability: may this catalog entry be tried now?
# --------------------------------------------------------------------------- #

class CandidateAvailability(Enum):
    """Whether a frozen catalog candidate can be selected for a trial.

    Availability is a Kernel-owned property of an entry in an epoch-frozen
    catalog.  It changes as locks, budgets and capabilities change; the
    catalog's membership, cardinality and hash do not (design section 6.3 --
    "Agents cannot add, remove, or mutate candidates during an epoch").
    """

    AVAILABLE = "AVAILABLE"
    #: Reserved by a proposal that has not yet settled.
    LOCKED = "LOCKED"
    #: Currently being trialled.
    IN_FLIGHT = "IN_FLIGHT"
    #: Already trialled to a settled outcome in this epoch.
    CONSUMED = "CONSUMED"
    #: Temporarily unavailable: capability missing, deployment draining,
    #: resource lock held elsewhere.
    TEMP_BLOCKED = "TEMP_BLOCKED"
    #: Outside the active target vector's scope or validity region.
    INELIGIBLE = "INELIGIBLE"
    #: No admissible way remains to try it in this epoch.
    EXHAUSTED = "EXHAUSTED"


# --------------------------------------------------------------------------- #
# Axis 7 - aggregate state of a candidate vector
# --------------------------------------------------------------------------- #

class AggregateState(Enum):
    """The rolled-up state of one target vector's candidate set.

    Deliberately *not* derivable by "any/all" over the outcome axis: design
    section 6.4 requires an exhaustion certificate, and open, partial,
    temporarily-blocked or budget-locked obligations produce
    ``EVIDENCE_INCOMPLETE`` rather than an exhaustion claim.
    """

    IN_PROGRESS = "IN_PROGRESS"
    #: A candidate reached deployed success under this vector.
    SATISFIED = "SATISFIED"
    #: Every candidate settled and every obligation closed: a valid
    #: current-vector exhaustion certificate can be issued (section 6.4).
    EXHAUSTED = "EXHAUSTED"
    #: Obligations remain open/partial/blocked/budget-locked.  Not exhaustion.
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    #: A safety incident or failed recovery blocks further work.
    BLOCKED = "BLOCKED"
    #: The case terminated before this vector resolved.
    TERMINATED = "TERMINATED"


# --------------------------------------------------------------------------- #
# Case termination - exactly six ways a coordination case ends
# --------------------------------------------------------------------------- #

class CaseTermination(Enum):
    """Design section 8: "Every coordination case terminates finitely through
    success, all confirmed vectors exhausted, evidence incomplete, operator
    abort, safety incident, or recovery failure."

    Six members, closed set.  "No agent can keep a case alive past its
    deadline, trial cap, proposal cap, or usable harm reserve" -- those four
    exhaustion conditions all land on one of these six, they are not a seventh
    open-ended "still running" value.
    """

    SUCCESS = "SUCCESS"
    VECTORS_EXHAUSTED = "VECTORS_EXHAUSTED"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    OPERATOR_ABORT = "OPERATOR_ABORT"
    SAFETY_INCIDENT = "SAFETY_INCIDENT"
    RECOVERY_FAILURE = "RECOVERY_FAILURE"


#: Every axis, in the order design section 8 lists them.  The seam test walks
#: this tuple to prove the axes stay seven distinct types with disjoint
#: members -- the mechanical form of "must not be collapsed".
AXIS_ENUMS: Tuple[Type[Enum], ...] = (
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
    TrialOutcome,
    EvidenceCellStatus,
    CandidateAvailability,
    AggregateState,
)


# --------------------------------------------------------------------------- #
# Pure cross-axis helpers
# --------------------------------------------------------------------------- #

def counts_toward_closure(
    validity: ExecutionValidity,
    sufficiency: MeasurementSufficiency,
    verdict: PredicateVerdict,
) -> bool:
    """True only when a trial may fill an evidence closure quota.

    Design section 8: "Invalid, unprovable, or insufficient traces do not fill
    closure quotas."  All three axes must be affirmative -- a valid execution
    with a gapped trace contributes nothing, and so does a sufficient trace
    from an invalid execution.
    """
    return (
        validity is ExecutionValidity.VALID
        and sufficiency is MeasurementSufficiency.SUFFICIENT
        and verdict in (PredicateVerdict.PASS, PredicateVerdict.FAIL)
    )


def is_non_success(outcome: TrialOutcome) -> bool:
    """True for every settled outcome that is not ``SUCCESS``.

    ``NOT_SETTLED`` raises: asking whether an unsettled trial is a non-success
    is a category error, and answering ``True`` would let an in-flight trial be
    filed as a failure.
    """
    if outcome is TrialOutcome.NOT_SETTLED:
        raise ValueError("NOT_SETTLED is not an outcome; the trial has not settled")
    return outcome is not TrialOutcome.SUCCESS


def aggregate_from_cells(
    cells: FrozenSet[EvidenceCellStatus],
    *,
    any_deployed_success: bool,
) -> AggregateState:
    """Roll evidence-cell statuses up to a vector aggregate state.

    The one rule this encodes is design section 6.4: an exhaustion claim is
    only available when no obligation is still open, partial, temporarily
    blocked or budget-locked.  Anything less is ``EVIDENCE_INCOMPLETE``, never
    "close enough".

    ``any_deployed_success`` is passed in rather than inferred, because
    deployed success needs the finalize path (design section 7) and an
    evidence cell alone cannot testify to it.
    """
    if any_deployed_success:
        return AggregateState.SATISFIED
    if not cells:
        return AggregateState.IN_PROGRESS
    if cells & OPEN_EVIDENCE_CELL_STATUSES:
        blocking = cells & OPEN_EVIDENCE_CELL_STATUSES
        if blocking <= {EvidenceCellStatus.OPEN, EvidenceCellStatus.PARTIAL}:
            return AggregateState.IN_PROGRESS
        return AggregateState.EVIDENCE_INCOMPLETE
    return AggregateState.EXHAUSTED
