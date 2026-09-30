"""The board: whole-intent state ↔ whole-action state, one pair per trial.

The owner's picture is a table whose rows are combinations of *every* intent's
state and whose columns are combinations of *every* action's state.  Both
sides use the same two-valued vocabulary: **prioritised now** or **deferred
now** (the prime).  ``I2'`` says intent I2 is not being held this round;
``A2'`` says action A2 is not being executed this round -- its priority is
low *right now*, under the current conditions.  A prime is a priority
judgement about this moment, never a lowered target, a removed action or a
switched-off xApp.

The table is never enumerated -- with ``n`` intents and ``m`` actions it has
``2^n x 2^m`` cells -- it is *filled*: each applied candidate produces one
observed pair, and an advisory may contribute predicted pairs that are kept
apart from observed ones and never overwrite them.

Two things stay structurally separate from the pair:

* the **outcome kind** -- "not evaluated", "indeterminate", "execution error"
  and "evaluated" are recorded beside the pair, so a search can never read a
  transport failure as a fact about the combination;
* the **technical verification** of each executed action (did its write read
  back?) is kept in ``action_verification``, beside the pair, not in it.  An
  action that was executed and verified and one that was executed and not
  verified are both ``A`` on the board -- both were *prioritised* -- and the
  difference is a matter for the trial record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "ActionState",
    "ActionVerification",
    "Board",
    "IntentState",
    "OutcomeKind",
    "PairProvenance",
    "StatePair",
    "action_states_from_candidate",
    "derive_state_pair",
    "intent_states_from_verdicts",
]


class IntentState(str, Enum):
    """One intent's state this round."""

    #: Held this round: every one of its predicates passed.
    PRIORITIZED = "PRIORITIZED"
    #: The prime: not held this round.
    DEFERRED = "DEFERRED"
    #: No verdict either way -- not evaluated, or indeterminate.
    UNKNOWN = "UNKNOWN"


class ActionState(str, Enum):
    """One action's state this round."""

    #: Executed this round: the candidate moved its axis off the baseline.
    PRIORITIZED = "PRIORITIZED"
    #: The prime: not executed this round; its axis stayed at the baseline.
    DEFERRED = "DEFERRED"


class ActionVerification(str, Enum):
    """Technical record for an executed action, beside the board."""

    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    REFUSED = "REFUSED"
    NOT_EXECUTED = "NOT_EXECUTED"


class PairProvenance(str, Enum):
    OBSERVED = "OBSERVED"
    PREDICTED = "PREDICTED"


class OutcomeKind(str, Enum):
    """What kind of answer the trial gave, kept apart from *what* it said."""

    #: The Kernel evaluated the predicates; the intent states are verdicts.
    EVALUATED = "EVALUATED"
    #: The trial never reached evaluation.
    NOT_EVALUATED = "NOT_EVALUATED"
    #: Evaluated, but the measurements could not decide.
    INDETERMINATE = "INDETERMINATE"
    #: A transport or execution failure; says nothing about the combination.
    EXEC_ERROR = "EXEC_ERROR"
    #: Stopped by a safety mechanism before or during the hold.
    SAFETY_STOPPED = "SAFETY_STOPPED"
    #: Stopped by the operator.
    ABORTED = "ABORTED"


#: Outcome kinds that carry no information about the combination itself.
UNINFORMATIVE_OUTCOMES = frozenset({
    OutcomeKind.NOT_EVALUATED, OutcomeKind.EXEC_ERROR, OutcomeKind.ABORTED,
})


def _prime(name: str, primed: bool) -> str:
    return f"{name}'" if primed else name


@dataclass(frozen=True)
class StatePair:
    """One cell of the board: a whole-intent state beside a whole-action state.

    ``intent_states`` and ``action_states`` are keyed by intent id and action
    id; their length is whatever the case admitted.  ``conditions`` records
    the circumstances the pair was observed under -- epoch, UE identities,
    cells -- because the same candidate under other conditions is a different
    cell of the table, not a contradiction.
    """

    candidate_id: str
    parameters: Mapping[str, str]
    intent_states: Mapping[str, IntentState]
    action_states: Mapping[str, ActionState]
    provenance: PairProvenance
    outcome_kind: OutcomeKind
    conditions: Mapping[str, Any] = field(default_factory=dict)
    trial_id: Optional[str] = None
    recorded_at: str = ""
    detail: str = ""
    #: Beside the pair, never in it: whether each executed action's write was
    #: read back.  Deferred actions are ``NOT_EXECUTED``.
    action_verification: Mapping[str, ActionVerification] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters",
                           {str(k): str(v) for k, v in dict(self.parameters).items()})
        object.__setattr__(self, "intent_states", {
            str(k): IntentState(v) for k, v in dict(self.intent_states).items()})
        object.__setattr__(self, "action_states", {
            str(k): ActionState(v) for k, v in dict(self.action_states).items()})
        object.__setattr__(self, "conditions", dict(self.conditions))
        object.__setattr__(self, "action_verification", {
            str(k): ActionVerification(v)
            for k, v in dict(self.action_verification).items()})

    # -- readings -------------------------------------------------------------

    @property
    def informative(self) -> bool:
        """True when the intent states are verdicts rather than absences."""
        return self.outcome_kind not in UNINFORMATIVE_OUTCOMES

    @property
    def all_intents_prioritized(self) -> bool:
        return bool(self.intent_states) and all(
            state is IntentState.PRIORITIZED for state in self.intent_states.values())

    @property
    def executed_actions_verified(self) -> bool:
        """Every executed action read back; a technical fact, not a board state."""
        return all(
            self.action_verification.get(name, ActionVerification.NOT_EXECUTED)
            in (ActionVerification.VERIFIED, ActionVerification.NOT_EXECUTED)
            for name, state in self.action_states.items()
            if state is ActionState.PRIORITIZED)

    def deferred_intents(self) -> Tuple[str, ...]:
        return tuple(name for name, state in self.intent_states.items()
                     if state is not IntentState.PRIORITIZED)

    def deferred_actions(self) -> Tuple[str, ...]:
        return tuple(name for name, state in self.action_states.items()
                     if state is ActionState.DEFERRED)

    def intent_tuple(self, order: Sequence[str]) -> Tuple[str, ...]:
        """``("I1'", "I2", "I3")`` in the given order; unknown reads as ``?``."""
        out = []
        for name in order:
            state = self.intent_states.get(name, IntentState.UNKNOWN)
            out.append(f"{name}?" if state is IntentState.UNKNOWN
                       else _prime(name, state is IntentState.DEFERRED))
        return tuple(out)

    def action_tuple(self, order: Sequence[str]) -> Tuple[str, ...]:
        return tuple(
            _prime(name, self.action_states.get(name, ActionState.DEFERRED)
                   is ActionState.DEFERRED)
            for name in order)

    def label(self, intent_order: Sequence[str], action_order: Sequence[str]) -> str:
        return ("(" + ", ".join(self.intent_tuple(intent_order)) + ") <-> ("
                + ", ".join(self.action_tuple(action_order)) + ")")

    def to_record(self) -> Dict[str, Any]:
        return {
            "candidateId": self.candidate_id,
            "parameters": dict(self.parameters),
            "intentStates": {k: v.value for k, v in self.intent_states.items()},
            "actionStates": {k: v.value for k, v in self.action_states.items()},
            "provenance": self.provenance.value,
            "outcomeKind": self.outcome_kind.value,
            "conditions": dict(self.conditions),
            "trialId": self.trial_id,
            "recordedAt": self.recorded_at,
            "detail": self.detail,
            "actionVerification": {k: v.value for k, v in self.action_verification.items()},
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "StatePair":
        return cls(
            candidate_id=str(record["candidateId"]),
            parameters=dict(record.get("parameters", {})),
            intent_states=dict(record.get("intentStates", {})),
            action_states=dict(record.get("actionStates", {})),
            provenance=PairProvenance(record.get("provenance", "OBSERVED")),
            outcome_kind=OutcomeKind(record.get("outcomeKind", "NOT_EVALUATED")),
            conditions=dict(record.get("conditions", {})),
            trial_id=record.get("trialId"),
            recorded_at=str(record.get("recordedAt", "")),
            detail=str(record.get("detail", "")),
            action_verification=dict(record.get("actionVerification", {})),
        )


class Board:
    """An append-only record of pairs.  Observed and predicted are kept apart."""

    def __init__(self, pairs: Iterable[StatePair] = ()) -> None:
        self._pairs: List[StatePair] = list(pairs)

    def record(self, pair: StatePair) -> StatePair:
        self._pairs.append(pair)
        return pair

    def __len__(self) -> int:
        return len(self._pairs)

    def pairs(self) -> Tuple[StatePair, ...]:
        return tuple(self._pairs)

    def observed(self) -> Tuple[StatePair, ...]:
        return tuple(p for p in self._pairs if p.provenance is PairProvenance.OBSERVED)

    def predicted(self) -> Tuple[StatePair, ...]:
        return tuple(p for p in self._pairs if p.provenance is PairProvenance.PREDICTED)

    def tried(self) -> Tuple[str, ...]:
        """Candidates that were actually applied, in order of first trial."""
        seen: List[str] = []
        for pair in self.observed():
            if pair.candidate_id not in seen:
                seen.append(pair.candidate_id)
        return tuple(seen)

    def for_candidate(self, candidate_id: str) -> Tuple[StatePair, ...]:
        return tuple(p for p in self._pairs if p.candidate_id == candidate_id)

    def informative_observed(self) -> Tuple[StatePair, ...]:
        return tuple(p for p in self.observed() if p.informative)

    def best_observed(self, comparator: Any) -> Optional[StatePair]:
        """The informative observed pair the comparator ranks highest."""
        candidates = self.informative_observed()
        if not candidates:
            return None
        return max(candidates, key=comparator.key)

    def to_records(self) -> List[Dict[str, Any]]:
        return [pair.to_record() for pair in self._pairs]

    @classmethod
    def from_records(cls, records: Iterable[Mapping[str, Any]]) -> "Board":
        return cls(StatePair.from_record(item) for item in records)


# --------------------------------------------------------------------------- #
# derivation from the Kernel's and the gateway's own records
# --------------------------------------------------------------------------- #


def intent_states_from_verdicts(
    intent_predicates: Mapping[str, Sequence[str]],
    verdicts: Mapping[str, str],
) -> Dict[str, IntentState]:
    """Per-intent state from per-predicate verdicts, never the other way round.

    An intent was held this round only when every one of its predicates
    passed; one ``FAIL`` defers it; anything else -- ``INDETERMINATE``,
    ``NOT_EVALUATED``, a predicate the evaluation never mentioned -- is
    ``UNKNOWN``.
    """
    states: Dict[str, IntentState] = {}
    for intent_id, predicate_ids in intent_predicates.items():
        values = [str(verdicts.get(predicate_id, "NOT_EVALUATED"))
                  for predicate_id in predicate_ids]
        if values and all(value == "PASS" for value in values):
            states[intent_id] = IntentState.PRIORITIZED
        elif any(value == "FAIL" for value in values):
            states[intent_id] = IntentState.DEFERRED
        else:
            states[intent_id] = IntentState.UNKNOWN
    return states


def action_states_from_candidate(
    parameters: Mapping[str, Any],
    action_axes: Mapping[str, str],
    baselines: Mapping[str, Any],
) -> Dict[str, ActionState]:
    """Which actions a candidate executes: an axis moved off its baseline."""
    return {
        action_id: (ActionState.DEFERRED
                    if str(parameters.get(axis)) == str(baselines.get(axis))
                    else ActionState.PRIORITIZED)
        for action_id, axis in action_axes.items()
    }


_OUTCOME_KINDS = {
    "SUCCESS": OutcomeKind.EVALUATED,
    "FAIL": OutcomeKind.EVALUATED,
    "INDETERMINATE": OutcomeKind.INDETERMINATE,
    "INVALID": OutcomeKind.EXEC_ERROR,
    "EXEC_ERROR": OutcomeKind.EXEC_ERROR,
    "SAFETY_STOPPED": OutcomeKind.SAFETY_STOPPED,
    "OPERATOR_ABORTED": OutcomeKind.ABORTED,
    "RECOVERY_FAILED": OutcomeKind.SAFETY_STOPPED,
    "NOT_SETTLED": OutcomeKind.NOT_EVALUATED,
}


def derive_state_pair(
    *,
    candidate_id: str,
    parameters: Mapping[str, Any],
    intent_predicates: Mapping[str, Sequence[str]],
    evaluation: Mapping[str, Any],
    trial_outcome: str,
    action_axes: Mapping[str, str],
    baselines: Mapping[str, Any],
    axis_verification: Optional[Mapping[str, ActionVerification]] = None,
    conditions: Optional[Mapping[str, Any]] = None,
    trial_id: Optional[str] = None,
    recorded_at: str = "",
    detail: str = "",
) -> StatePair:
    """Derive one observed pair from a settled trial.

    ``evaluation`` is the trial record's own ``evaluation`` mapping
    (``predicateVerdicts``), ``trial_outcome`` its settled outcome,
    ``baselines`` the configuration the trial started from, and
    ``axis_verification`` what the gateway observed per executed axis.
    """
    verdicts = dict(evaluation.get("predicateVerdicts") or {})
    outcome_kind = _OUTCOME_KINDS.get(str(trial_outcome), OutcomeKind.NOT_EVALUATED)
    if outcome_kind in UNINFORMATIVE_OUTCOMES or str(evaluation.get(
            "executionValidity", "NOT_EVALUATED")) == "NOT_EVALUATED":
        intent_states = {name: IntentState.UNKNOWN for name in intent_predicates}
    else:
        intent_states = intent_states_from_verdicts(intent_predicates, verdicts)
    action_states = action_states_from_candidate(parameters, action_axes, baselines)
    verification = dict(axis_verification or {})
    action_verification = {
        action_id: (ActionVerification.NOT_EXECUTED
                    if action_states[action_id] is ActionState.DEFERRED
                    else verification.get(axis, ActionVerification.UNVERIFIED))
        for action_id, axis in action_axes.items()
    }
    return StatePair(
        candidate_id=candidate_id,
        parameters=parameters,
        intent_states=intent_states,
        action_states=action_states,
        provenance=PairProvenance.OBSERVED,
        outcome_kind=outcome_kind,
        conditions=dict(conditions or {}),
        trial_id=trial_id,
        recorded_at=recorded_at,
        detail=detail,
        action_verification=action_verification,
    )
