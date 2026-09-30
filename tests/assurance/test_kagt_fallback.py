"""KAGT lane: the Evidence Coordinator's forced deterministic fallback.

Authority: docs/architecture/SEAMS-GATE2.md; design section 15 ("deterministic
fallback after agent timeout or malformed output"); design section 8 ("every
case terminates finitely").
"""

from __future__ import annotations

import time
import unittest

from assurance.advisors.catalog_view import CatalogEntry
from assurance.advisors.coordinator import StrategyBackedEvidenceCoordinator
from assurance.advisors.messages import AdvisoryKind, AdvisoryMessage
from assurance.advisors.strategy import StrategyKind, deterministic_fallback
from assurance.contracts.catalog import Candidate
from assurance.core.addressing import content_hash
from assurance.core.axes import CandidateAvailability
from assurance.core.components import ComponentId

STAMP = "2026-08-21T09:00:00.000000Z"
EPOCH = content_hash({"epoch": 1})


def _entry(candidate_id: str) -> CatalogEntry:
    candidate = Candidate(
        candidate_id=candidate_id, target_ref="target/PIN_TO_CELL", option_ref="opt/pin",
        parameters={}, semantic_hash=content_hash({"c": candidate_id}), capability_ref="cap/1",
    )
    return CatalogEntry(candidate=candidate, availability=CandidateAvailability.AVAILABLE)


CATALOG = [_entry("cand-b"), _entry("cand-a")]


class _HangingStrategy:
    strategy_id = "hang"
    strategy_kind = StrategyKind.DETERMINISTIC

    def __init__(self, sleep_seconds: float) -> None:
        self._sleep_seconds = sleep_seconds

    def propose(self, **kwargs):
        time.sleep(self._sleep_seconds)
        return None

    def describe(self):
        return {"strategyId": self.strategy_id, "strategyKind": self.strategy_kind.value}


class _RaisingStrategy:
    strategy_id = "raise"
    strategy_kind = StrategyKind.DETERMINISTIC

    def propose(self, **kwargs):
        raise RuntimeError("simulated agent failure")

    def describe(self):
        return {"strategyId": self.strategy_id, "strategyKind": self.strategy_kind.value}


class _MalformedStrategy:
    """Returns something that is not a well-formed proposal at all."""

    strategy_id = "malformed"
    strategy_kind = StrategyKind.DETERMINISTIC

    def __init__(self, payload=None) -> None:
        self._payload = payload if payload is not None else "not-a-message"

    def propose(self, **kwargs):
        return self._payload

    def describe(self):
        return {"strategyId": self.strategy_id, "strategyKind": self.strategy_kind.value}


class _WrongEchoStrategy:
    """Returns a well-typed message that answers a *different* request."""

    strategy_id = "wrong-echo"
    strategy_kind = StrategyKind.DETERMINISTIC

    def propose(self, **kwargs):
        from assurance.advisors.messages import NextCandidateProposal

        return AdvisoryMessage(
            message_id="wrong-echo-1", kind=AdvisoryKind.NEXT_CANDIDATE_PROPOSAL,
            issued_by=ComponentId.EVIDENCE_COORDINATOR, correlation_id="a-different-correlation",
            epoch_hash=kwargs["epoch_hash"], created_at=kwargs["now"],
            body=NextCandidateProposal(candidate_id="cand-a"),
        )

    def describe(self):
        return {"strategyId": self.strategy_id, "strategyKind": self.strategy_kind.value}


class _WellBehavedStrategy:
    strategy_id = "well-behaved"
    strategy_kind = StrategyKind.DETERMINISTIC

    def propose(self, **kwargs):
        from assurance.advisors.messages import NextCandidateProposal

        return AdvisoryMessage(
            message_id="ok-1", kind=AdvisoryKind.NEXT_CANDIDATE_PROPOSAL,
            issued_by=ComponentId.EVIDENCE_COORDINATOR, correlation_id=kwargs["correlation_id"],
            epoch_hash=kwargs["epoch_hash"], created_at=kwargs["now"],
            body=NextCandidateProposal(candidate_id="cand-b"),
        )

    def describe(self):
        return {"strategyId": self.strategy_id, "strategyKind": self.strategy_kind.value}


def _propose(coordinator: StrategyBackedEvidenceCoordinator, correlation_id: str = "corr-1"):
    return coordinator.propose_next(
        catalog_view=CATALOG, evidence_view={}, budget_view={},
        correlation_id=correlation_id, epoch_hash=EPOCH, now=STAMP,
    )


class HappyPathIsUnaffected(unittest.TestCase):
    def test_a_well_behaved_strategy_is_returned_unchanged(self):
        coordinator = StrategyBackedEvidenceCoordinator(strategy=_WellBehavedStrategy())
        message = _propose(coordinator)
        self.assertEqual(message.message_id, "ok-1")
        self.assertEqual(coordinator.fallback_events, ())


class ForcedFallback(unittest.TestCase):
    """Every non-happy path forces exactly the deterministic fallback."""

    def _direct_fallback(self, correlation_id: str, failure_reason_substring: str):
        return deterministic_fallback(
            catalog_view=CATALOG, evidence_view={}, budget_view={},
            correlation_id=correlation_id, epoch_hash=EPOCH, now=STAMP,
            failure_reason=failure_reason_substring,
        )

    def test_timeout_forces_the_fallback_and_returns_promptly(self):
        coordinator = StrategyBackedEvidenceCoordinator(
            strategy=_HangingStrategy(sleep_seconds=0.3), timeout_seconds=0.02,
        )
        started = time.monotonic()
        message = _propose(coordinator)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.25, "the coordinator must not block for the hung strategy")
        self.assertEqual(len(coordinator.fallback_events), 1)
        self.assertIn("timeout", coordinator.fallback_events[0].reason)
        self.assertEqual(message.body.candidate_id, self._direct_fallback("corr-1", "x").body.candidate_id)

    def test_an_exception_forces_the_fallback(self):
        coordinator = StrategyBackedEvidenceCoordinator(strategy=_RaisingStrategy())
        message = _propose(coordinator)
        self.assertEqual(len(coordinator.fallback_events), 1)
        self.assertIn("exception", coordinator.fallback_events[0].reason)
        self.assertEqual(message.body.candidate_id, self._direct_fallback("corr-1", "x").body.candidate_id)

    def test_a_non_message_return_value_forces_the_fallback(self):
        coordinator = StrategyBackedEvidenceCoordinator(strategy=_MalformedStrategy("not-a-message"))
        message = _propose(coordinator)
        self.assertEqual(len(coordinator.fallback_events), 1)
        self.assertIn("malformed", coordinator.fallback_events[0].reason)
        self.assertEqual(message.body.candidate_id, self._direct_fallback("corr-1", "x").body.candidate_id)

    def test_a_wrong_kind_or_issuer_forces_the_fallback(self):
        from assurance.advisors.messages import IntentDraft

        wrong_kind = AdvisoryMessage(
            message_id="wk-1", kind=AdvisoryKind.INTENT_DRAFT, issued_by=ComponentId.INTENT_AGENT,
            correlation_id="corr-1", epoch_hash=EPOCH, created_at=STAMP,
            body=IntentDraft(objective_family="PIN_TO_CELL", scope_selector={}),
        )
        coordinator = StrategyBackedEvidenceCoordinator(strategy=_MalformedStrategy(wrong_kind))
        message = _propose(coordinator)
        self.assertEqual(len(coordinator.fallback_events), 1)
        self.assertIn("malformed", coordinator.fallback_events[0].reason)

    def test_a_proposal_answering_a_different_request_forces_the_fallback(self):
        coordinator = StrategyBackedEvidenceCoordinator(strategy=_WrongEchoStrategy())
        message = _propose(coordinator)
        self.assertEqual(len(coordinator.fallback_events), 1)
        self.assertIn("does not match", coordinator.fallback_events[0].reason)
        self.assertEqual(message.correlation_id, "corr-1")

    def test_a_strategy_declining_to_propose_is_not_treated_as_malformed(self):
        class _Declines:
            strategy_id = "declines"
            strategy_kind = StrategyKind.DETERMINISTIC

            def propose(self, **kwargs):
                return None

            def describe(self):
                return {}

        coordinator = StrategyBackedEvidenceCoordinator(strategy=_Declines())
        message = _propose(coordinator)
        self.assertEqual(coordinator.fallback_events, ())
        self.assertIsNone(message)


class FiniteTermination(unittest.TestCase):
    """A run of failures never hangs, however many times it happens."""

    def test_repeated_timeouts_each_terminate_within_a_bounded_time(self):
        coordinator = StrategyBackedEvidenceCoordinator(
            strategy=_HangingStrategy(sleep_seconds=0.05), timeout_seconds=0.01,
        )
        started = time.monotonic()
        for i in range(10):
            _propose(coordinator, correlation_id=f"corr-{i}")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, "ten forced fallbacks must not accumulate unbounded delay")
        self.assertEqual(len(coordinator.fallback_events), 10)

    def test_a_positive_timeout_is_required(self):
        with self.assertRaises(ValueError):
            StrategyBackedEvidenceCoordinator(strategy=_WellBehavedStrategy(), timeout_seconds=0)


if __name__ == "__main__":
    unittest.main()
