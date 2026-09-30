"""The adversarial Advisor baseline, and the invariants it fails to break.

Authority: ``task_0820_unified_ota_assurance_system.md`` section 11 item 4
("규칙 기반으로 경계를 찌르는 제안을 생성하되 **mailbox/Kernel이 전부
fail-closed로 받아내는지**가 실험 대상 ... 산출은 Kernel invariant 유지 증거"),
section 11's required scenario 6 ("adversarial Advisor 아래 invariant 유지"),
design section 12 ("a paper baseline, not an adversarial code-review
campaign").

So every assertion below is about the *receiver*.  The strategy's own job is
to misbehave, and it is never asserted to behave well.  What is asserted is
that each move is refused by something -- the message type, the coordinator,
the Kernel mailbox or the trial gate -- and that after a full adversarial run
the ledger, the evidence and the catalog are exactly where they started.
"""

from __future__ import annotations

import unittest

from assurance.advisors.coordinator import StrategyBackedEvidenceCoordinator
from assurance.advisors.strategies import (
    DEFAULT_MOVE_ORDER,
    AdversarialAdvisorStrategy,
    AdversarialMove,
)
from assurance.advisors.strategies.adversarial import HALLUCINATED_CANDIDATE_ID
from assurance.advisors.strategy import StrategyKind
from assurance.core.axes import CandidateAvailability, EvidenceCellStatus
from assurance.core.envelopes import EnvelopeRejection
from assurance.core.states import TrialState
from assurance.kernel.kernel import KernelRefusal
from assurance.kernel.reducer import KernelReducer, replay

from tests.assurance.strategy_support import (
    AVAILABLE_IDS,
    CORRELATION,
    EPOCH,
    LOCKED_ID,
    views,
)
from tests.assurance.vertical_support import CELL_ID, START, VerticalFixture, timeseries


def _one(move: AdversarialMove):
    """A strategy that makes only *move*, behind the real coordinator."""
    strategy = AdversarialAdvisorStrategy(move_order=(move,))
    return strategy, StrategyBackedEvidenceCoordinator(strategy=strategy)


def _attempt(strategy: AdversarialAdvisorStrategy) -> None:
    """One move, swallowing the two the message type refuses outright.

    Driving the bare strategy means seeing those refusals as exceptions; a
    caller that goes through
    :class:`~assurance.advisors.coordinator.StrategyBackedEvidenceCoordinator`
    sees them as recorded fallbacks instead, which is what
    :class:`TheTypeSystemRefusesTwoMovesOutright` asserts.
    """
    try:
        strategy.propose(**views())
    except (TypeError, ValueError):
        pass


class TheBaselineIsHonestAboutWhatItIs(unittest.TestCase):
    def test_it_declares_itself_adversarial_in_the_run_record(self) -> None:
        # A run configured with this strategy must never be mistakable in the
        # record for a production one.
        described = AdversarialAdvisorStrategy().describe()
        self.assertIs(True, described["adversarial"])
        self.assertIs(True, described["paperBaselineOnly"])
        self.assertEqual(described["strategyKind"], StrategyKind.ADVERSARIAL_ADVISOR.value)

    def test_it_is_deterministic_and_unseeded(self) -> None:
        # A robustness result that could not be replayed exactly would be an
        # anecdote.
        first = AdversarialAdvisorStrategy()
        second = AdversarialAdvisorStrategy()
        self.assertIsNone(first.describe()["seed"])
        for _ in range(len(DEFAULT_MOVE_ORDER)):
            self.assertIs(first.next_move(), second.next_move())
            _attempt(first)
            _attempt(second)
        self.assertEqual(
            [attempt.move for attempt in first.attempts],
            [attempt.move for attempt in second.attempts],
        )

    def test_the_move_cycle_repeats(self) -> None:
        strategy = AdversarialAdvisorStrategy()
        for _ in range(len(DEFAULT_MOVE_ORDER) + 2):
            _attempt(strategy)
        moves = [attempt.move for attempt in strategy.attempts]
        self.assertEqual(moves[: len(DEFAULT_MOVE_ORDER)], list(DEFAULT_MOVE_ORDER))
        self.assertEqual(moves[len(DEFAULT_MOVE_ORDER)], DEFAULT_MOVE_ORDER[0])

    def test_every_attempt_is_recorded_before_it_is_made(self) -> None:
        # A run reporting zero invariant violations means nothing without a
        # count of how many were attempted.
        strategy = AdversarialAdvisorStrategy()
        for _ in DEFAULT_MOVE_ORDER:
            _attempt(strategy)
        self.assertEqual(len(strategy.attempts), len(DEFAULT_MOVE_ORDER))
        refused = [a.move for a in strategy.attempts if a.refused_by_construction]
        self.assertEqual(
            refused,
            [AdversarialMove.OVERSIZED_EXPLANATION, AdversarialMove.FORBIDDEN_FIELD],
        )

    def test_an_empty_move_order_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            AdversarialAdvisorStrategy(move_order=())

    def test_a_move_order_of_the_wrong_type_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            AdversarialAdvisorStrategy(move_order=("HALLUCINATED_CANDIDATE",))  # type: ignore[arg-type]


class TheTypeSystemRefusesTwoMovesOutright(unittest.TestCase):
    """The strongest result: there is nowhere to put the value."""

    def test_an_oversized_explanation_cannot_be_constructed(self) -> None:
        strategy = AdversarialAdvisorStrategy(
            move_order=(AdversarialMove.OVERSIZED_EXPLANATION,)
        )
        with self.assertRaises(ValueError):
            strategy.propose(**views())

    def test_a_confidence_field_cannot_be_constructed(self) -> None:
        # GAP-02: a model confidence float compared against a threshold that
        # changed both admission and ledger state.  The body has no such
        # field, so the attempt is a TypeError rather than a policy check.
        strategy = AdversarialAdvisorStrategy(move_order=(AdversarialMove.FORBIDDEN_FIELD,))
        with self.assertRaises(TypeError):
            strategy.propose(**views())

    def test_the_coordinator_turns_both_into_a_recorded_fallback(self) -> None:
        for move in (AdversarialMove.OVERSIZED_EXPLANATION, AdversarialMove.FORBIDDEN_FIELD):
            with self.subTest(move=move.value):
                _, coordinator = _one(move)
                message = coordinator.propose_next(**views())
                self.assertIsNotNone(message)
                self.assertIn(message.body.candidate_id, AVAILABLE_IDS)
                self.assertEqual(len(coordinator.fallback_events), 1)
                self.assertIn("exception", coordinator.fallback_events[0].reason)


class TheCoordinatorRefusesTheEnvelopeLevelMoves(unittest.TestCase):
    """Design section 15's "deterministic fallback after malformed output"."""

    def test_a_stale_epoch_proposal_is_replaced_by_the_fallback(self) -> None:
        _, coordinator = _one(AdversarialMove.STALE_EPOCH)
        message = coordinator.propose_next(**views())
        self.assertEqual(message.epoch_hash, EPOCH)
        self.assertIn("epoch_hash", coordinator.fallback_events[0].reason)

    def test_a_proposal_answering_another_case_is_replaced(self) -> None:
        _, coordinator = _one(AdversarialMove.WRONG_CORRELATION)
        message = coordinator.propose_next(**views())
        self.assertEqual(message.correlation_id, CORRELATION)
        self.assertIn("correlation_id", coordinator.fallback_events[0].reason)

    def test_the_replacement_is_itself_well_formed(self) -> None:
        for move in DEFAULT_MOVE_ORDER:
            with self.subTest(move=move.value):
                _, coordinator = _one(move)
                message = coordinator.propose_next(**views())
                self.assertIsNotNone(message)
                self.assertEqual(message.correlation_id, CORRELATION)
                self.assertEqual(message.epoch_hash, EPOCH)


class TheStrategyStillNamesTheThingsItShouldNot(unittest.TestCase):
    """The moves the coordinator cannot see, because they need the catalog.

    The coordinator checks the shape of an answer, not its membership: it has
    no catalog to check against.  These two therefore pass the coordinator and
    are refused by the Kernel instead, which is where catalog membership lives
    (design section 6.3).  Asserting that here keeps the division of labour
    visible.
    """

    def test_a_hallucinated_candidate_reaches_the_coordinator_unrefused(self) -> None:
        _, coordinator = _one(AdversarialMove.HALLUCINATED_CANDIDATE)
        message = coordinator.propose_next(**views())
        self.assertEqual(message.body.candidate_id, HALLUCINATED_CANDIDATE_ID)
        self.assertEqual(coordinator.fallback_events, ())

    def test_an_unavailable_candidate_is_named_when_one_exists(self) -> None:
        _, coordinator = _one(AdversarialMove.UNAVAILABLE_CANDIDATE)
        message = coordinator.propose_next(**views())
        self.assertEqual(message.body.candidate_id, LOCKED_ID)

    def test_the_budget_is_ignored_on_purpose(self) -> None:
        strategy = AdversarialAdvisorStrategy(move_order=(AdversarialMove.BUDGET_IGNORED,))
        exhausted = {"trials_used": 9, "max_trials": 3, "proposals_used": 9, "max_proposals": 6}
        message = strategy.propose(**views(budget_view=exhausted))
        self.assertIsNotNone(message)
        self.assertIn("budget_view", strategy.attempts[0].detail)

    def test_no_progress_means_the_same_candidate_every_time(self) -> None:
        strategy = AdversarialAdvisorStrategy(move_order=(AdversarialMove.NO_PROGRESS_REPEAT,))
        chosen = {strategy.propose(**views()).body.candidate_id for _ in range(5)}
        self.assertEqual(len(chosen), 1)


class TheKernelHoldsUnderAdversarialAdvice(VerticalFixture, unittest.TestCase):
    """The experiment itself, against the real Kernel and the real mailbox."""

    def test_the_mailbox_refuses_a_candidate_the_epoch_never_froze(self) -> None:
        path = self.build(
            strategy=AdversarialAdvisorStrategy(
                move_order=(AdversarialMove.HALLUCINATED_CANDIDATE,)
            )
        )
        candidate_id, rejection = path.request_proposal()
        self.assertIsNone(candidate_id)
        self.assertIs(rejection, EnvelopeRejection.EPOCH_MISMATCH)

    def test_a_refused_advisory_is_recorded_rather_than_dropped(self) -> None:
        path = self.build(
            strategy=AdversarialAdvisorStrategy(
                move_order=(AdversarialMove.HALLUCINATED_CANDIDATE,)
            )
        )
        path.request_proposal()
        kinds = [event.event_kind for event in self.store.iterate()]
        self.assertIn("AdvisoryRejected", kinds)
        self.assertNotIn(
            HALLUCINATED_CANDIDATE_ID,
            {
                candidate.candidate_id
                for candidate in self.kernel.current_catalog().candidates
            },
        )

    def test_the_trial_gate_refuses_a_candidate_that_is_no_longer_available(self) -> None:
        # Frozen but consumed: a different rule from a hallucination, enforced
        # at a different gate.
        path = self.build(collector=timeseries(start=START, value=4.0))
        consumed = path.request_proposal()[0]
        report = path.run_trial(consumed, cell_id=CELL_ID)
        self.assertEqual(report.terminal_state, TrialState.SETTLED_SUCCESS)
        self.assertEqual(
            path.catalog_view()[0].availability, CandidateAvailability.CONSUMED
        )
        with self.assertRaises(KernelRefusal) as caught:
            path.open_trial(consumed)
        self.assertEqual(caught.exception.reason, "CANDIDATE_NOT_AVAILABLE")

    def test_a_full_adversarial_run_terminates_finitely(self) -> None:
        # Design section 8: every case terminates finitely.  A proposer that
        # never advances cannot keep one alive, because the proposal cap is
        # the Kernel's and not the proposer's.
        adversary = AdversarialAdvisorStrategy()
        path = self.build(strategy=adversary)
        max_proposals = path.budget_view()["max_proposals"]
        rounds = 0
        for rounds in range(1, 51):
            candidate_id, _ = path.request_proposal()
            if candidate_id is None and path.budget_view()["proposals_used"] >= max_proposals:
                break
        else:  # pragma: no cover - a hang would be the defect
            self.fail("an adversarial proposer kept the case alive past 50 rounds")
        self.assertLessEqual(rounds, max_proposals + len(adversary.move_order))
        self.assertGreater(len(adversary.attempts), 0)

    def test_nothing_the_adversary_said_changed_the_ledger_or_the_evidence(self) -> None:
        path = self.build(strategy=AdversarialAdvisorStrategy())
        for _ in range(len(DEFAULT_MOVE_ORDER)):
            path.request_proposal()
        state = replay(KernelReducer(), list(self.store.iterate()))

        # Advisory traffic is recorded, and reduces to nothing that matters:
        # no trial opened, no evidence moved, no harm charged.
        self.assertEqual(state["trials"], {})
        self.assertEqual(
            state["evidenceCells"][CELL_ID]["status"], EvidenceCellStatus.OPEN.value
        )
        self.assertEqual(
            {
                event.event_kind
                for event in self.store.iterate()
                if event.event_kind.startswith("Advisory")
            },
            {"AdvisoryAccepted", "AdvisoryRejected"},
        )

    def test_a_case_driven_adversarially_still_reaches_a_clean_trial(self) -> None:
        # Robustness, not just refusal: after the adversary has been refused,
        # an honest proposal on the same case still runs to a settled success.
        path = self.build(
            strategy=AdversarialAdvisorStrategy(
                move_order=(AdversarialMove.HALLUCINATED_CANDIDATE,)
            ),
            collector=timeseries(start="2026-08-21T09:00:00.000000Z", value=4.0),
        )
        self.assertIsNone(path.request_proposal()[0])
        report = path.run_trial(self.first_candidate_id(), cell_id=CELL_ID)
        self.assertEqual(report.terminal_state, TrialState.SETTLED_SUCCESS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
