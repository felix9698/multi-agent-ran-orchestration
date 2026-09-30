"""The two priority roles, as orderings over tuples of any length.

Role 1 ranks intents: when not every intent can be held this round, which
is held first.  Role 2 ranks actions: which action's deferral is least
acceptable this round.
Both are *orderings the operator states* (or a stated default), and both are
consumed only by :class:`PairComparator`, which turns two whole-state pairs
into "this one is preferable".  Nothing here changes a verdict or a state;
a prime stays a prime whatever its priority.

Two policies exist because the owner has not settled which one the paper
uses.  ``LEXICOGRAPHIC`` protects the higher-ranked item first: a pair that
meets the top intent beats any pair that does not, whatever the rest says.
``WEIGHTED`` sums stated weights.  The policy is data on the coordinator and
is recorded with the search, so a run says which rule chose its candidates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from assurance.coordination.board import ActionState, IntentState, StatePair

__all__ = [
    "ActionPriorityCoordinator",
    "IntentPriorityCoordinator",
    "PairComparator",
    "PriorityPolicy",
]


class PriorityPolicy(str, Enum):
    LEXICOGRAPHIC = "LEXICOGRAPHIC"
    WEIGHTED = "WEIGHTED"


def _ordered(order: Sequence[str], names: Sequence[str]) -> Tuple[str, ...]:
    """The stated order first, then anything unstated in its given order."""
    stated = [str(item) for item in order if str(item) in set(map(str, names))]
    rest = [str(item) for item in names if str(item) not in stated]
    return tuple(stated + rest)


@dataclass(frozen=True)
class IntentPriorityCoordinator:
    """Role 1: which intents to protect first.

    ``order`` is highest priority first.  An intent not named in it ranks
    after every named one, in admission order.
    """

    order: Tuple[str, ...]
    policy: PriorityPolicy = PriorityPolicy.LEXICOGRAPHIC
    weights: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "order", tuple(str(item) for item in self.order))
        object.__setattr__(self, "weights",
                           {str(k): float(v) for k, v in dict(self.weights).items()})
        if self.policy is PriorityPolicy.WEIGHTED and not self.weights:
            raise ValueError("a WEIGHTED intent priority needs weights")

    def ranking(self, intent_ids: Sequence[str]) -> Tuple[str, ...]:
        return _ordered(self.order, intent_ids)

    def score(self, pair: StatePair) -> Tuple[float, ...]:
        """Higher is better.  ``UNKNOWN`` never scores as satisfied."""
        ranking = self.ranking(tuple(pair.intent_states))
        met = [1.0 if pair.intent_states.get(name) is IntentState.PRIORITIZED else 0.0
               for name in ranking]
        if self.policy is PriorityPolicy.LEXICOGRAPHIC:
            return tuple(met)
        return (sum(self.weights.get(name, 0.0) * value
                    for name, value in zip(ranking, met)),)

    def deferred_in_priority_order(self, pair: StatePair) -> Tuple[str, ...]:
        return tuple(name for name in self.ranking(tuple(pair.intent_states))
                     if pair.intent_states.get(name) is not IntentState.PRIORITIZED)

    def to_record(self) -> Dict[str, Any]:
        return {"role": "intent-priority", "order": list(self.order),
                "policy": self.policy.value, "weights": dict(self.weights)}


@dataclass(frozen=True)
class ActionPriorityCoordinator:
    """Role 2: which actions to keep executing first.

    ``order`` is the action whose deferral is *least* acceptable first.  Read
    the other way round, the last action is the one the search defers first
    when something has to give this round.
    """

    order: Tuple[str, ...]
    policy: PriorityPolicy = PriorityPolicy.LEXICOGRAPHIC
    weights: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "order", tuple(str(item) for item in self.order))
        object.__setattr__(self, "weights",
                           {str(k): float(v) for k, v in dict(self.weights).items()})
        if self.policy is PriorityPolicy.WEIGHTED and not self.weights:
            raise ValueError("a WEIGHTED action priority needs weights")

    def ranking(self, action_ids: Sequence[str]) -> Tuple[str, ...]:
        return _ordered(self.order, action_ids)

    def score(self, pair: StatePair) -> Tuple[float, ...]:
        ranking = self.ranking(tuple(pair.action_states))
        ok = [1.0 if pair.action_states.get(name) is ActionState.PRIORITIZED else 0.0
              for name in ranking]
        if self.policy is PriorityPolicy.LEXICOGRAPHIC:
            return tuple(ok)
        return (sum(self.weights.get(name, 0.0) * value
                    for name, value in zip(ranking, ok)),)

    def most_deferrable_first(self, action_ids: Sequence[str]) -> Tuple[str, ...]:
        """Actions in the order the search may defer them: least protected first."""
        return tuple(reversed(self.ranking(action_ids)))

    def to_record(self) -> Dict[str, Any]:
        return {"role": "action-priority", "order": list(self.order),
                "policy": self.policy.value, "weights": dict(self.weights)}


@dataclass(frozen=True)
class PairComparator:
    """Turn the two roles into one preference between whole-state pairs.

    Intent preference dominates: two pairs are compared on the intent role
    first and on the action role only when the intent role ties.  That is the
    owner's statement that the table exists to meet intents, and actions are
    how.  An uninformative pair (execution error, aborted) ranks below every
    informative one, whatever it claims.
    """

    intents: IntentPriorityCoordinator
    actions: ActionPriorityCoordinator

    def key(self, pair: StatePair) -> Tuple[Any, ...]:
        return (1 if pair.informative else 0,
                self.intents.score(pair), self.actions.score(pair))

    def better(self, candidate: StatePair, incumbent: Optional[StatePair]) -> bool:
        if incumbent is None:
            return True
        return self.key(candidate) > self.key(incumbent)

    def to_record(self) -> Dict[str, Any]:
        return {"intents": self.intents.to_record(),
                "actions": self.actions.to_record(),
                "rule": "intent preference dominates; action preference breaks ties"}
