"""Role 3: the search over the frozen candidate catalog, driven by the board.

One decision at a time: given the board so far, the catalog with the Kernel's
own availability, the two priority roles and a legality rule, name the next
frozen candidate or say why the search ends.  The searcher proposes; the
Kernel still admits, permits, judges and settles.

The choice rule is deterministic and stated, so a run's next-candidate choices
can be replayed from its board:

1. If an observed pair meets every intent with every action OK, the search has
   **found** its combination and stops.
2. If the trial budget is spent, it stops **budget exhausted** -- which is not
   a claim that no satisfying combination exists.
3. If no legal, available, untried candidate remains, it stops **catalog
   exhausted** -- over the frozen catalog only.
4. If the last ``undecidable_streak`` trials were uninformative (execution
   error, indeterminate, not evaluated), it stops **undecidable**: the
   equipment or the measurements, not the combinations, are what failed.
5. Otherwise it ranks the untried legal candidates.  With predictions (from an
   advisory, kept PREDICTED on the board) the comparator ranks them.  Without,
   it makes a local move from the best observed pair: change the axes that can
   affect the highest-priority unmet intent, one axis at a time, the least
   protected action first, then catalog order.  The first trial, with nothing
   observed, is the candidate closest to the baseline -- observe before
   moving.

Every decision carries its rationale in words, and every termination names
its reason separately from the Kernel's own case termination.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from assurance.coordination.board import (
    Board, OutcomeKind, PairProvenance, StatePair, UNINFORMATIVE_OUTCOMES,
)
from assurance.coordination.priorities import PairComparator

#: Outcomes that say the trial could not be *judged*: nothing was learned
#: about the combination, for a reason that lies in execution or measurement.
UNDECIDED_OUTCOMES = frozenset(UNINFORMATIVE_OUTCOMES | {OutcomeKind.INDETERMINATE})

__all__ = [
    "ActionAxisView",
    "BoardSearcher",
    "CandidateView",
    "IntentView",
    "SearchDecision",
    "TerminationReason",
]


class TerminationReason(str, Enum):
    FOUND = "FOUND"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    CATALOG_EXHAUSTED = "CATALOG_EXHAUSTED"
    UNDECIDABLE = "UNDECIDABLE"


@dataclass(frozen=True)
class CandidateView:
    candidate_id: str
    parameters: Mapping[str, str]
    available: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters",
                           {str(k): str(v) for k, v in dict(self.parameters).items()})


@dataclass(frozen=True)
class IntentView:
    intent_id: str
    #: The UE (or other scope) the intent is about; used to say which axes
    #: can affect it.
    scope_id: str


@dataclass(frozen=True)
class ActionAxisView:
    action_id: str
    axis: str
    scope_id: str
    baseline: str
    #: True for an action that changes a shared resource (a cell's scheduler),
    #: so it can affect intents on other scopes too.
    shared: bool = False


@dataclass(frozen=True)
class SearchDecision:
    candidate_id: Optional[str]
    termination: Optional[TerminationReason]
    rationale: str
    ranked: Tuple[str, ...] = ()
    best_so_far: Optional[StatePair] = None

    def to_record(self) -> Dict[str, Any]:
        return {
            "candidateId": self.candidate_id,
            "termination": None if self.termination is None else self.termination.value,
            "rationale": self.rationale,
            "ranked": list(self.ranked),
            "bestSoFar": None if self.best_so_far is None else self.best_so_far.to_record(),
        }


@dataclass
class BoardSearcher:
    catalog: Sequence[CandidateView]
    intents: Sequence[IntentView]
    actions: Sequence[ActionAxisView]
    comparator: PairComparator
    budget_trials: int
    legality: Callable[[Mapping[str, str]], bool] = lambda parameters: True
    undecidable_streak: int = 2
    #: Optional advisory prediction: parameters -> a PREDICTED pair.  Never
    #: authoritative; it only orders the untried candidates.
    predictor: Optional[Callable[[Mapping[str, str]], Optional[StatePair]]] = None
    #: Optional search agent (an LLM on its own model): given the untried legal
    #: candidates, the best observed pair and the board, it names ONE untried
    #: candidate id or ``None``.  Any id outside the untried set is ignored and
    #: the deterministic ranking chooses -- the agent selects among what the
    #: frozen catalog admits, never beyond it.
    chooser: Optional[Callable[[Sequence["CandidateView"], Optional[StatePair], Board],
                              Optional[str]]] = None
    decisions: List[SearchDecision] = field(default_factory=list)

    # -- helpers ---------------------------------------------------------

    def _affecting_axes(self, intent: IntentView) -> Tuple[str, ...]:
        return tuple(action.axis for action in self.actions
                     if action.scope_id == intent.scope_id or action.shared)

    def _baseline_distance(self, parameters: Mapping[str, str]) -> int:
        return sum(1 for action in self.actions
                   if str(parameters.get(action.axis)) != str(action.baseline))

    def _axis_rank(self) -> Dict[str, int]:
        """A smaller rank is more deferrable: the least protected action's axis first."""
        order = self.comparator.actions.most_deferrable_first(
            [action.action_id for action in self.actions])
        by_action = {action.action_id: action.axis for action in self.actions}
        return {by_action[action_id]: position
                for position, action_id in enumerate(order) if action_id in by_action}

    def _intent_by_id(self, intent_id: str) -> Optional[IntentView]:
        return next((item for item in self.intents if item.intent_id == intent_id), None)

    # -- the decision ----------------------------------------------------

    def next(self, board: Board, *, trials_used: int) -> SearchDecision:
        decision = self._decide(board, trials_used=trials_used)
        self.decisions.append(decision)
        return decision

    def _decide(self, board: Board, *, trials_used: int) -> SearchDecision:
        best = board.best_observed(self.comparator)
        for pair in board.informative_observed():
            if pair.all_intents_prioritized:
                return SearchDecision(
                    candidate_id=pair.candidate_id, termination=TerminationReason.FOUND,
                    rationale=(f"{pair.candidate_id} held every intent this round; the "
                               "search found its combination"),
                    best_so_far=pair)
        if trials_used >= self.budget_trials:
            return SearchDecision(
                candidate_id=None, termination=TerminationReason.BUDGET_EXHAUSTED,
                rationale=(f"{trials_used} of {self.budget_trials} trials used without a "
                           "combination that held every intent; this is a spent budget, "
                           "not a proof that none exists"),
                best_so_far=best)
        recent = board.observed()[-self.undecidable_streak:]
        if (self.undecidable_streak > 0 and len(recent) >= self.undecidable_streak
                and all(pair.outcome_kind in UNDECIDED_OUTCOMES for pair in recent)):
            return SearchDecision(
                candidate_id=None, termination=TerminationReason.UNDECIDABLE,
                rationale=(f"the last {len(recent)} trials could not be judged "
                           f"({', '.join(p.outcome_kind.value for p in recent)}); "
                           "execution or measurement failed, not the combinations"),
                best_so_far=best)
        tried = set(board.tried())
        untried = [item for item in self.catalog
                   if item.available and item.candidate_id not in tried
                   and self.legality(item.parameters)]
        if not untried:
            return SearchDecision(
                candidate_id=None, termination=TerminationReason.CATALOG_EXHAUSTED,
                rationale=("no legal, available, untried candidate remains in the frozen "
                           "catalog; exhaustion is over this catalog only"),
                best_so_far=best)
        ranked, rationale = self._rank(untried, best, board)
        if self.chooser is not None:
            try:
                pick = self.chooser(tuple(untried), best, board)
            except Exception as exc:  # an agent fault never ends the search
                pick, rationale = None, f"{rationale}; the search agent raised {type(exc).__name__}"
            untried_ids = {item.candidate_id for item in untried}
            if pick is not None and str(pick) in untried_ids:
                rule_choice = ranked[0].candidate_id
                ranked = ([item for item in untried if item.candidate_id == str(pick)]
                          + [item for item in ranked if item.candidate_id != str(pick)])
                rationale = (f"the search agent chose {pick} (the deterministic rule would "
                             f"have chosen {rule_choice}); {rationale}")
            elif pick is not None:
                rationale = (f"the search agent named {pick!r}, which is not an untried "
                             f"candidate, so the deterministic rule chose; {rationale}")
        return SearchDecision(candidate_id=ranked[0].candidate_id, termination=None,
                              rationale=rationale,
                              ranked=tuple(item.candidate_id for item in ranked),
                              best_so_far=best)

    def _rank(self, untried: Sequence[CandidateView], best: Optional[StatePair],
              board: Board) -> Tuple[List[CandidateView], str]:
        catalog_position = {item.candidate_id: index
                            for index, item in enumerate(self.catalog)}
        if self.predictor is not None:
            predicted: Dict[str, StatePair] = {}
            for item in untried:
                pair = self.predictor(item.parameters)
                if pair is not None and pair.provenance is PairProvenance.PREDICTED:
                    predicted[item.candidate_id] = pair
                    board.record(pair)
            if predicted:
                # Catalog order first, then a stable sort by the comparator's
                # key, best first; candidates without a prediction sink to the
                # end in catalog order.
                by_catalog = sorted(untried, key=lambda item: catalog_position[item.candidate_id])
                with_prediction = [item for item in by_catalog if item.candidate_id in predicted]
                without = [item for item in by_catalog if item.candidate_id not in predicted]
                with_prediction.sort(
                    key=lambda item: self.comparator.key(predicted[item.candidate_id]),
                    reverse=True)
                ranked = with_prediction + without
                return ranked, (f"{len(predicted)} predicted pairs ranked the untried "
                                "candidates under the stated priorities; predictions are "
                                "recorded PREDICTED and never counted as observed")
        if best is None:
            ranked = sorted(untried, key=lambda item: (
                self._baseline_distance(item.parameters), catalog_position[item.candidate_id]))
            return ranked, ("nothing observed yet; the candidate closest to the baseline is "
                            "tried first so the current state is a recorded pair before "
                            "anything moves")
        deferred = self.comparator.intents.deferred_in_priority_order(best)
        focus = next((self._intent_by_id(name) for name in deferred), None)
        affecting = set(self._affecting_axes(focus)) if focus is not None else set()
        axis_rank = self._axis_rank()

        def changed_axes(parameters: Mapping[str, str]) -> Tuple[str, ...]:
            return tuple(action.axis for action in self.actions
                         if str(parameters.get(action.axis))
                         != str(best.parameters.get(action.axis)))

        def key(item: CandidateView) -> Tuple[Any, ...]:
            changed = changed_axes(item.parameters)
            touches_focus = any(axis in affecting for axis in changed)
            return (
                0 if touches_focus else 1,
                len(changed),
                min((axis_rank.get(axis, len(axis_rank)) for axis in changed), default=0),
                catalog_position[item.candidate_id],
            )

        ranked = sorted(untried, key=key)
        target = focus.intent_id if focus is not None else "none"
        return ranked, (f"best observed so far is {best.candidate_id}; the highest-priority "
                        f"deferred intent is {target}, so candidates that change which actions "
                        "are executed for it are tried first, one action at a time, least "
                        "protected action first, then catalog order")

    def to_record(self) -> Dict[str, Any]:
        return {
            "budgetTrials": int(self.budget_trials),
            "undecidableStreak": int(self.undecidable_streak),
            "comparator": self.comparator.to_record(),
            "intents": [{"intentId": i.intent_id, "scopeId": i.scope_id} for i in self.intents],
            "actions": [{"actionId": a.action_id, "axis": a.axis, "scopeId": a.scope_id,
                         "baseline": a.baseline, "shared": a.shared} for a in self.actions],
            "decisions": [item.to_record() for item in self.decisions],
        }
