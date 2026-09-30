"""The optimization-based strategy: exhaustive search over the frozen catalog.

Owner lane: **KAGT**.  New file (Gate 6, task section 11 item 4).

Design section 12 names an "optimization-based strategy" without fixing the
objective, so this module fixes one and says why.

The objective is **remaining evidence obligation served**.  A case exists to
close evidence cells (design section 6.4); a proposal that could contribute to
three open cells is worth more than one that could contribute to none, and
that is a quantity the Kernel already publishes in ``evidence_view``.  Nothing
in the score comes from a model, a prediction or a measurement -- an expected
effect would be exactly the "predicted effect" the constraint list forbids
using for a decision.

The search is **exhaustive**, which is legitimate precisely because design
section 6.3 freezes the candidate universe with a known cardinality: there is
no space to sample.  Scoring every candidate every time also means the
strategy has no memory and no state, so its answer depends only on the views
it was handed.

It is **deterministic without a seed**.  Every tie is broken by the
lexicographically-smallest ``candidate_id`` -- a total order over the id
string, reproducible regardless of how the caller assembled the sequence,
which is the same rule
:func:`~assurance.advisors.strategy.deterministic_fallback` uses.  No clock is
read and no randomness is drawn, so ``describe()`` honestly reports
``seed: None``: there is nothing to reproduce because nothing varies.

Two design section 8 rules survive intact.  Sealed cells score zero -- a
coordinator that planned against dormant evidence would be planning against
information the experiment has not released.  And a zero total score still
produces a proposal rather than ``None``: falling silent because nothing looks
promising would be readable as exhaustion, and exhaustion is the Kernel's
decision from evidence cells, never a proposer running out of ideas.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from assurance.advisors.messages import AdvisoryMessage
from assurance.advisors.proposal_support import (
    available_candidates,
    budget_exhausted,
    build_next_candidate_proposal,
)
from assurance.advisors.strategies.description import strategy_description
from assurance.advisors.strategy import StrategyKind
from assurance.core.axes import EvidenceCellStatus

__all__ = ["OBLIGATION_WEIGHTS", "CandidateScore", "OptimizationStrategy"]

#: How much each open evidence status is worth to the objective.
#:
#: ``OPEN`` outranks ``PARTIAL`` because a cell with no usable contribution
#: yet has further to go.  ``TEMP_BLOCKED`` is worth something but least: the
#: obligation is real and the blocker may clear, yet a trial aimed at it right
#: now probably cannot land.  ``BUDGET_LOCKED`` is worth nothing -- the cell is
#: blocked by an exhausted reserve or cap, and no proposal can unlock it, so
#: scoring it would steer trials at an obligation only the Kernel can release.
#:
#: Every status not listed scores zero: closed, dormant, provisional and
#: post-closure cells are not outstanding obligations.
OBLIGATION_WEIGHTS: Mapping[str, int] = {
    EvidenceCellStatus.OPEN.value: 3,
    EvidenceCellStatus.PARTIAL.value: 2,
    EvidenceCellStatus.TEMP_BLOCKED.value: 1,
    EvidenceCellStatus.BUDGET_LOCKED.value: 0,
}


@dataclass(frozen=True)
class CandidateScore:
    """One candidate's objective value, and the cells that produced it.

    Exposed so a test -- and the paper's per-strategy trace -- can read the
    search rather than infer it from the winner.
    """

    candidate_id: str
    score: int
    served_cells: Tuple[str, ...]

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "candidateId": self.candidate_id,
            "score": self.score,
            "servedCells": list(self.served_cells),
        }


def _score_candidate(candidate: Any, evidence_view: Mapping[str, Any]) -> CandidateScore:
    score = 0
    served: List[str] = []
    target_ref = getattr(candidate, "target_ref", None)
    for cell_id in sorted(evidence_view):
        cell = evidence_view[cell_id]
        if not isinstance(cell, Mapping):
            continue
        if cell.get("sealed"):
            continue
        if str(cell.get("targetRef", "")) != str(target_ref):
            continue
        weight = OBLIGATION_WEIGHTS.get(str(cell.get("status", "")), 0)
        if weight <= 0:
            continue
        score += weight
        served.append(cell_id)
    return CandidateScore(
        candidate_id=candidate.candidate_id, score=score, served_cells=tuple(served)
    )


class OptimizationStrategy:
    """Design section 12's optimization-based strategy, LLM-free.

    Constructed with nothing.  No seed, no model, no state carried between
    calls: two instances handed the same views agree, and one instance handed
    the same views twice agrees with itself.
    """

    def __init__(self, *, strategy_id: str = "strategy-optimization") -> None:
        self.strategy_id = str(strategy_id)
        self.strategy_kind = StrategyKind.OPTIMIZATION

    def score_catalog(
        self, catalog_view: Sequence[Any], evidence_view: Mapping[str, Any]
    ) -> Tuple[CandidateScore, ...]:
        """Every available candidate's objective value, best first.

        The full search, exposed.  Ordered by descending score then ascending
        candidate id, so the winner is always element zero and the ordering is
        the tie-break rule rather than a separate one.
        """
        scores = [
            _score_candidate(candidate, evidence_view)
            for candidate in available_candidates(catalog_view)
        ]
        scores.sort(key=lambda item: (-item.score, item.candidate_id))
        return tuple(scores)

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
        scores = self.score_catalog(catalog_view, evidence_view)
        if not scores:
            return None
        best = scores[0]
        message = build_next_candidate_proposal(
            message_id=f"{self.strategy_id}/{correlation_id}/{best.candidate_id}",
            candidate_id=best.candidate_id,
            rationale=(
                f"optimization: obligation weight {best.score} over "
                f"{len(scores)} available candidate(s), ties broken by candidate id"
            ),
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
            now=now,
        )
        if not best.served_cells:
            return message
        return replace(
            message, body=replace(message.body, evidence_cell_refs=best.served_cells)
        )

    def describe(self) -> Mapping[str, Any]:
        return strategy_description(
            strategy_id=self.strategy_id,
            strategy_kind=self.strategy_kind,
            model_identity=None,
            prompt_version=None,
            token_budget=0,
            tool_call_budget=0,
            latency_budget_ms=0.0,
            seed=None,
            temperature=None,
            extra={
                "objective": "remaining-evidence-obligation-weight",
                "search": "exhaustive",
                "obligationWeights": dict(OBLIGATION_WEIGHTS),
                "tieBreak": "lexicographic-candidate-id",
            },
        )
