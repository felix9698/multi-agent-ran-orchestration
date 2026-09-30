"""The optimization-based strategy: exhaustive, deterministic, seedless.

Authority: ``task_0820_unified_ota_assurance_system.md`` section 11 item 3
("LLM 없이 카탈로그 위 결정론 최적화 ... 카탈로그 유한이므로 전수 탐색 허용.
seed 없는 결정론"), design section 12.

The claims worth testing here are the ones a scoring function can quietly stop
satisfying: that it looks at *every* candidate, that its objective is the
remaining evidence obligation and nothing predicted, that a tie is broken by a
total order rather than by input order, and that it never treats "nothing
looks promising" as exhaustion.
"""

from __future__ import annotations

import unittest

from assurance.advisors.strategies import OBLIGATION_WEIGHTS, OptimizationStrategy
from assurance.advisors.strategy import StrategyKind
from assurance.core.axes import CandidateAvailability, EvidenceCellStatus

from tests.assurance.strategy_support import (
    AVAILABLE_IDS,
    CATALOG,
    EVIDENCE,
    EXHAUSTED_BUDGET,
    LOCKED_ID,
    OTHER_TARGET,
    TARGET,
    entry,
    views,
)


def _cells(**statuses: str):
    return {
        cell_id: {"status": status, "targetRef": TARGET, "sealed": False}
        for cell_id, status in statuses.items()
    }


class TheSearchIsExhaustive(unittest.TestCase):
    def test_every_available_candidate_is_scored(self) -> None:
        scores = OptimizationStrategy().score_catalog(CATALOG, EVIDENCE)
        self.assertEqual(
            sorted(score.candidate_id for score in scores), sorted(AVAILABLE_IDS)
        )

    def test_an_unavailable_candidate_is_not_scored_at_all(self) -> None:
        scores = OptimizationStrategy().score_catalog(CATALOG, EVIDENCE)
        self.assertNotIn(LOCKED_ID, {score.candidate_id for score in scores})

    def test_the_scores_come_back_best_first(self) -> None:
        scores = OptimizationStrategy().score_catalog(CATALOG, EVIDENCE)
        ordered = sorted(scores, key=lambda item: (-item.score, item.candidate_id))
        self.assertEqual(list(scores), ordered)


class TheObjectiveIsRemainingObligation(unittest.TestCase):
    def test_an_open_cell_outranks_a_partial_one(self) -> None:
        self.assertGreater(
            OBLIGATION_WEIGHTS[EvidenceCellStatus.OPEN.value],
            OBLIGATION_WEIGHTS[EvidenceCellStatus.PARTIAL.value],
        )

    def test_a_budget_locked_cell_is_worth_nothing(self) -> None:
        # No proposal can unlock it; scoring it would steer trials at an
        # obligation only the Kernel can release.
        self.assertEqual(OBLIGATION_WEIGHTS[EvidenceCellStatus.BUDGET_LOCKED.value], 0)

    def test_a_closed_cell_is_not_an_obligation(self) -> None:
        self.assertNotIn(EvidenceCellStatus.CLOSED_PASS.value, OBLIGATION_WEIGHTS)
        self.assertNotIn(EvidenceCellStatus.CLOSED_FAIL.value, OBLIGATION_WEIGHTS)
        self.assertNotIn(EvidenceCellStatus.POST_CLOSURE_WITNESS.value, OBLIGATION_WEIGHTS)

    def test_the_score_is_the_sum_over_the_candidates_own_target(self) -> None:
        strategy = OptimizationStrategy()
        scores = {
            score.candidate_id: score for score in strategy.score_catalog(CATALOG, EVIDENCE)
        }
        # OPEN(3) + PARTIAL(2) on target/steer; the CLOSED_PASS cell adds
        # nothing and the sealed cell is on the other target.
        self.assertEqual(scores["candidate/000000"].score, 5)
        self.assertEqual(scores["candidate/000000"].served_cells, ("cell/open-a", "cell/partial-b"))
        # candidate/000003 is bound to the other target, whose only cell is
        # sealed, so it serves nothing.
        self.assertEqual(scores["candidate/000003"].score, 0)
        self.assertEqual(scores["candidate/000003"].served_cells, ())

    def test_a_sealed_cell_never_contributes(self) -> None:
        # Design section 8: dormant evidence stays sealed, so planning
        # against it would be planning against unreleased information.
        sealed_only = {
            "cell/sealed": {"status": "OPEN", "targetRef": TARGET, "sealed": True}
        }
        scores = OptimizationStrategy().score_catalog(CATALOG, sealed_only)
        self.assertEqual({score.score for score in scores}, {0})

    def test_the_proposal_names_the_cells_it_would_serve(self) -> None:
        message = OptimizationStrategy().propose(**views())
        self.assertEqual(message.body.evidence_cell_refs, ("cell/open-a", "cell/partial-b"))

    def test_nothing_predicted_enters_the_score(self) -> None:
        # The constraint list forbids a predicted effect being used for a
        # decision; the proposal carries no expected gain at all.
        self.assertIsNone(OptimizationStrategy().propose(**views()).body.expected_information_gain)


class ItIsDeterministicWithoutASeed(unittest.TestCase):
    def test_describe_reports_no_seed_and_no_model(self) -> None:
        described = OptimizationStrategy().describe()
        self.assertIsNone(described["seed"])
        self.assertIsNone(described["modelIdentity"])
        self.assertEqual(described["tokenBudget"], 0)
        self.assertEqual(described["search"], "exhaustive")

    def test_two_fresh_instances_agree(self) -> None:
        first = OptimizationStrategy().propose(**views())
        second = OptimizationStrategy().propose(**views())
        self.assertEqual(first.body.candidate_id, second.body.candidate_id)
        self.assertEqual(first.body.rationale, second.body.rationale)

    def test_one_instance_repeats_itself(self) -> None:
        strategy = OptimizationStrategy()
        self.assertEqual(
            strategy.propose(**views()).body.candidate_id,
            strategy.propose(**views()).body.candidate_id,
        )

    def test_a_tie_is_broken_by_candidate_id_not_by_input_order(self) -> None:
        # CATALOG is deliberately out of id order, and both steering
        # candidates score identically.
        forward = OptimizationStrategy().propose(**views())
        reversed_view = OptimizationStrategy().propose(**views(catalog_view=tuple(reversed(CATALOG))))
        self.assertEqual(forward.body.candidate_id, "candidate/000000")
        self.assertEqual(reversed_view.body.candidate_id, "candidate/000000")

    def test_a_higher_score_beats_the_lexicographic_tie_break(self) -> None:
        # Otherwise the "optimization" would just be the deterministic
        # strategy with extra arithmetic.
        catalog = (
            entry("candidate/000000", target_ref=OTHER_TARGET),
            entry("candidate/000001", target_ref=TARGET),
        )
        message = OptimizationStrategy().propose(**views(catalog_view=catalog))
        self.assertEqual(message.body.candidate_id, "candidate/000001")

    def test_it_reads_no_clock(self) -> None:
        # The ``now`` it stamps is the caller's, so two runs at different
        # instants still produce the same choice.
        first = OptimizationStrategy().propose(**views())
        second = OptimizationStrategy().propose(**views(now="2027-01-01T00:00:00.000000Z"))
        self.assertEqual(first.body.candidate_id, second.body.candidate_id)
        self.assertNotEqual(first.created_at, second.created_at)


class SilenceIsNeverExhaustion(unittest.TestCase):
    def test_a_zero_score_still_produces_a_proposal(self) -> None:
        # Falling silent because nothing looks promising would read as
        # exhaustion, and exhaustion is the Kernel's decision from evidence
        # cells (design section 6.4).
        message = OptimizationStrategy().propose(**views(evidence_view={}))
        self.assertIsNotNone(message)
        self.assertIn(message.body.candidate_id, AVAILABLE_IDS)

    def test_no_available_candidate_returns_none(self) -> None:
        locked = (entry("candidate/000009", availability=CandidateAvailability.LOCKED),)
        self.assertIsNone(OptimizationStrategy().propose(**views(catalog_view=locked)))

    def test_an_exhausted_kernel_budget_returns_none(self) -> None:
        self.assertIsNone(
            OptimizationStrategy().propose(**views(budget_view=EXHAUSTED_BUDGET))
        )

    def test_it_is_the_optimization_kind(self) -> None:
        self.assertIs(OptimizationStrategy().strategy_kind, StrategyKind.OPTIMIZATION)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
