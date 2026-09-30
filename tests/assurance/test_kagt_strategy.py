"""KAGT lane: the DETERMINISTIC and RANDOM strategies of design section 12.

Authority: docs/architecture/SEAMS-GATE2.md; design section 12 ("All
strategies use the same Kernel, contracts, catalog, harm limits, hardware
conditions, and evidence rules") -- verified here as "produce the same
well-formed proposal contract regardless of which strategy is behind the
interface".
"""

from __future__ import annotations

import unittest

from assurance.advisors.catalog_view import CatalogEntry
from assurance.advisors.messages import AdvisoryKind, AdvisoryMessage
from assurance.advisors.strategies import DeterministicStrategy, RandomStrategy
from assurance.advisors.strategy import AdvisoryStrategy, StrategyKind, deterministic_fallback
from assurance.contracts.catalog import Candidate
from assurance.core.addressing import content_hash
from assurance.core.axes import CandidateAvailability
from assurance.core.components import ComponentId

STAMP = "2026-08-21T09:00:00.000000Z"
EPOCH = content_hash({"epoch": 1})


def _entry(candidate_id: str, availability=CandidateAvailability.AVAILABLE) -> CatalogEntry:
    candidate = Candidate(
        candidate_id=candidate_id, target_ref="target/PIN_TO_CELL", option_ref="opt/pin",
        parameters={}, semantic_hash=content_hash({"c": candidate_id}), capability_ref="cap/1",
    )
    return CatalogEntry(candidate=candidate, availability=availability)


CATALOG = [_entry("cand-b"), _entry("cand-a"), _entry("cand-c", CandidateAvailability.LOCKED)]


def _assert_well_formed_proposal(test: unittest.TestCase, message, *, correlation_id: str) -> None:
    test.assertIsInstance(message, AdvisoryMessage)
    test.assertIs(message.kind, AdvisoryKind.NEXT_CANDIDATE_PROPOSAL)
    test.assertIs(message.issued_by, ComponentId.EVIDENCE_COORDINATOR)
    test.assertEqual(message.correlation_id, correlation_id)
    test.assertEqual(message.epoch_hash, EPOCH)
    test.assertIn(message.body.candidate_id, {"cand-a", "cand-b"})  # cand-c is LOCKED


class DeterministicStrategyBehaviour(unittest.TestCase):
    def test_is_a_runtime_checkable_advisory_strategy(self):
        strategy = DeterministicStrategy()
        self.assertIsInstance(strategy, AdvisoryStrategy)
        self.assertIs(strategy.strategy_kind, StrategyKind.DETERMINISTIC)

    def test_ignores_locked_candidates_and_repeats_the_same_answer(self):
        strategy = DeterministicStrategy()
        first = strategy.propose(
            catalog_view=CATALOG, evidence_view={}, budget_view={},
            correlation_id="c1", epoch_hash=EPOCH, now=STAMP,
        )
        second = strategy.propose(
            catalog_view=CATALOG, evidence_view={}, budget_view={},
            correlation_id="c1", epoch_hash=EPOCH, now=STAMP,
        )
        _assert_well_formed_proposal(self, first, correlation_id="c1")
        self.assertEqual(first.body.candidate_id, second.body.candidate_id)

    def test_a_fresh_instance_with_the_same_catalog_agrees(self):
        first = DeterministicStrategy().propose(
            catalog_view=CATALOG, evidence_view={}, budget_view={},
            correlation_id="c1", epoch_hash=EPOCH, now=STAMP,
        )
        second = DeterministicStrategy().propose(
            catalog_view=CATALOG, evidence_view={}, budget_view={},
            correlation_id="c1", epoch_hash=EPOCH, now=STAMP,
        )
        self.assertEqual(first.body.candidate_id, second.body.candidate_id)

    def test_no_available_candidate_returns_none(self):
        strategy = DeterministicStrategy()
        locked_only = [_entry("cand-x", CandidateAvailability.LOCKED)]
        self.assertIsNone(
            strategy.propose(
                catalog_view=locked_only, evidence_view={}, budget_view={},
                correlation_id="c1", epoch_hash=EPOCH, now=STAMP,
            )
        )

    def test_exhausted_budget_returns_none(self):
        strategy = DeterministicStrategy()
        self.assertIsNone(
            strategy.propose(
                catalog_view=CATALOG, evidence_view={},
                budget_view={"trials_used": 5, "max_trials": 5},
                correlation_id="c1", epoch_hash=EPOCH, now=STAMP,
            )
        )

    def test_describe_carries_no_credential_and_names_the_kind(self):
        described = DeterministicStrategy().describe()
        self.assertEqual(described["strategyKind"], "DETERMINISTIC")
        self.assertNotIn("apiKey", described)
        self.assertNotIn("secret", described)


class RandomStrategyBehaviour(unittest.TestCase):
    def test_is_a_runtime_checkable_advisory_strategy(self):
        strategy = RandomStrategy(seed=1)
        self.assertIsInstance(strategy, AdvisoryStrategy)
        self.assertIs(strategy.strategy_kind, StrategyKind.RANDOM)

    def test_a_fresh_strategy_with_the_same_seed_makes_the_same_first_draw(self):
        first_draws = [
            RandomStrategy(seed=42).propose(
                catalog_view=CATALOG, evidence_view={}, budget_view={},
                correlation_id="c1", epoch_hash=EPOCH, now=STAMP,
            ).body.candidate_id
            for _ in range(3)
        ]
        self.assertEqual(len(set(first_draws)), 1)

    def test_a_full_sequence_is_reproduced_by_an_identically_seeded_strategy(self):
        def _draw_sequence(strategy: RandomStrategy):
            return [
                strategy.propose(
                    catalog_view=CATALOG, evidence_view={}, budget_view={},
                    correlation_id="c1", epoch_hash=EPOCH, now=STAMP,
                ).body.candidate_id
                for _ in range(6)
            ]

        sequence_a = _draw_sequence(RandomStrategy(seed=7))
        sequence_repeat = _draw_sequence(RandomStrategy(seed=7))
        self.assertEqual(sequence_a, sequence_repeat)

    def test_only_picks_from_available_candidates(self):
        strategy = RandomStrategy(seed=3)
        for _ in range(20):
            message = strategy.propose(
                catalog_view=CATALOG, evidence_view={}, budget_view={},
                correlation_id="c1", epoch_hash=EPOCH, now=STAMP,
            )
            self.assertIn(message.body.candidate_id, {"cand-a", "cand-b"})

    def test_exhausted_budget_returns_none(self):
        strategy = RandomStrategy(seed=1)
        self.assertIsNone(
            strategy.propose(
                catalog_view=CATALOG, evidence_view={},
                budget_view={"proposals_used": 20, "max_proposals": 20},
                correlation_id="c1", epoch_hash=EPOCH, now=STAMP,
            )
        )


class StrategyEquivalence(unittest.TestCase):
    """Design section 12: swapping the strategy changes the proposal and
    nothing about the contract it answers."""

    def test_every_strategy_and_the_fallback_answer_the_same_contract(self):
        producers = [
            DeterministicStrategy().propose,
            RandomStrategy(seed=99).propose,
            lambda **kwargs: deterministic_fallback(failure_reason="equivalence-check", **kwargs),
        ]
        for produce in producers:
            with self.subTest(producer=produce):
                message = produce(
                    catalog_view=CATALOG, evidence_view={}, budget_view={},
                    correlation_id="corr-equiv", epoch_hash=EPOCH, now=STAMP,
                )
                _assert_well_formed_proposal(self, message, correlation_id="corr-equiv")


if __name__ == "__main__":
    unittest.main()
