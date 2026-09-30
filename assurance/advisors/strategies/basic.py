"""Two of design section 12's six comparable strategies: DETERMINISTIC and
RANDOM.

Owner lane: **KAGT**.  Originally ``assurance/advisors/strategies.py``; moved
into :mod:`assurance.advisors.strategies` unchanged when Gate 6 added the
remaining four strategies beside it, so ``from assurance.advisors.strategies
import DeterministicStrategy`` still resolves to exactly this class.

Both implement :class:`~assurance.advisors.strategy.AdvisoryStrategy`
exactly, so a caller cannot tell which is behind the interface without
reading :attr:`~assurance.advisors.strategy.AdvisoryStrategy.strategy_kind`.
These two are the LLM-free, hardware-free floor of the comparison: whatever
the other four do, a run configured with one of these makes no model call at
all.
"""

from __future__ import annotations

import random
from typing import Any, Mapping, Optional, Sequence

from assurance.advisors.messages import AdvisoryMessage
from assurance.advisors.proposal_support import (
    available_candidates,
    budget_exhausted,
    build_next_candidate_proposal,
)
from assurance.advisors.strategies.description import strategy_description
from assurance.advisors.strategy import StrategyKind

__all__ = ["DeterministicStrategy", "RandomStrategy", "available_candidates", "budget_exhausted"]


class DeterministicStrategy:
    """The first available candidate in catalog order, every time.

    No seed, no clock, no I/O: the same catalog, evidence and budget view
    produce the same proposal on every host and every run -- the property
    :func:`~assurance.advisors.strategy.deterministic_fallback` also needs,
    which is why both pick the same way.
    """

    def __init__(self, *, strategy_id: str = "strategy-deterministic") -> None:
        self.strategy_id = strategy_id
        self.strategy_kind = StrategyKind.DETERMINISTIC

    def propose(
        self,
        *,
        catalog_view: Sequence[Any],
        evidence_view: Mapping[str, Any],
        budget_view: Mapping[str, Any],
        correlation_id: str,
        epoch_hash: str,
        now: str,
    ) -> Optional[AdvisoryMessage]:
        if budget_exhausted(budget_view):
            return None
        available = available_candidates(catalog_view)
        if not available:
            return None
        chosen = available[0]
        return build_next_candidate_proposal(
            message_id=f"{self.strategy_id}/{correlation_id}/{chosen.candidate_id}",
            candidate_id=chosen.candidate_id,
            rationale="deterministic: first available candidate in catalog order",
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
            now=now,
        )

    def describe(self) -> Mapping[str, Any]:
        return strategy_description(
            strategy_id=self.strategy_id,
            strategy_kind=self.strategy_kind,
            seed=None,
        )


class RandomStrategy:
    """Uniform choice among available candidates, seeded at construction.

    Design section 13: "the batch plan fixes the seed and the run records
    it" -- the seed is a constructor argument, never a per-call one, so a
    strategy that drew randomness per call could not be reproduced from the
    plan.
    """

    def __init__(self, *, seed: int, strategy_id: str = "strategy-random") -> None:
        self.strategy_id = strategy_id
        self.strategy_kind = StrategyKind.RANDOM
        self._seed = seed
        self._rng = random.Random(seed)
        self._draws = 0

    def propose(
        self,
        *,
        catalog_view: Sequence[Any],
        evidence_view: Mapping[str, Any],
        budget_view: Mapping[str, Any],
        correlation_id: str,
        epoch_hash: str,
        now: str,
    ) -> Optional[AdvisoryMessage]:
        if budget_exhausted(budget_view):
            return None
        available = available_candidates(catalog_view)
        if not available:
            return None
        chosen = self._rng.choice(available)
        self._draws += 1
        return build_next_candidate_proposal(
            message_id=f"{self.strategy_id}/{correlation_id}/{self._draws}",
            candidate_id=chosen.candidate_id,
            rationale=f"random: seed={self._seed}, draw={self._draws}",
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
            now=now,
        )

    def describe(self) -> Mapping[str, Any]:
        return strategy_description(
            strategy_id=self.strategy_id,
            strategy_kind=self.strategy_kind,
            seed=self._seed,
        )
