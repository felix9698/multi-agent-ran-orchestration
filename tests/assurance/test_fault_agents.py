"""Task section 14: agent timeout, malformed output, hallucinated id, stale
context and repeated no-progress proposals.

The claim being tested is not "the agent behaves"; it is that **nothing
downstream depends on the agent behaving**.  Design section 15 requires a
"deterministic fallback after agent timeout or malformed output", and design
section 8 requires every case to terminate finitely regardless of what an
agent does or does not say.  So each fault here is followed through to a
Kernel-decided outcome: a real trial, a recorded rejection, or a finite case
terminal -- never a stalled case and never a change on the equipment that no
frozen candidate authorised.
"""

from __future__ import annotations

import threading
import unittest
from typing import Any, Mapping, Optional, Sequence

from assurance.advisors.coordinator import StrategyBackedEvidenceCoordinator
from assurance.advisors.messages import AdvisoryMessage
from assurance.advisors.proposal_support import build_next_candidate_proposal
from assurance.advisors.strategy import StrategyKind
from assurance.core.axes import CaseTermination, TrialOutcome
from assurance.core.envelopes import EnvelopeRejection
from assurance.core.states import TrialState
from assurance.kernel.kernel import KernelRefusal

from tests.assurance.fault_preservation import (
    PreservationSnapshot,
    assert_finite_terminal,
    snapshot,
)
from tests.assurance.vertical_support import (
    CELL_ID,
    START,
    VerticalFixture,
    timeseries,
)


SUCCESS_SNAPSHOT = PreservationSnapshot(
    actual_outcome="SUCCESS",
    harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
    reserve_outstanding=0.0,
    evidence=((CELL_ID, "CLOSED_PASS", (("VALID", "SUFFICIENT", "PASS", False),)),),
    configuration=(("queuePriority", "7"), ("servingCell", "cell-2")),
    locks=(),
    recovery=(),
    trial_terminal="SETTLED_SUCCESS",
    case_terminal="SUCCESS",
)


class _Strategy:
    """A strategy whose failure mode is chosen by the test."""

    def __init__(self, mode: str, *, candidate_id: str = "") -> None:
        self.mode = mode
        self.candidate_id = candidate_id
        self.calls = 0
        self.released = threading.Event()

    strategy_kind = StrategyKind.DETERMINISTIC
    strategy_id = "strategy-under-test"

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
        self.calls += 1
        if self.mode == "hang":
            # Blocks until the test releases it, so the coordinator's timeout
            # is what ends the call rather than a real sleep.
            self.released.wait(timeout=5)
            return None
        if self.mode == "raise":
            raise RuntimeError("injected strategy failure")
        if self.mode == "malformed":
            return {"candidateId": self.candidate_id}
        if self.mode == "wrong-correlation":
            return build_next_candidate_proposal(
                message_id=f"{self.strategy_id}/{self.calls}",
                candidate_id=self.candidate_id,
                rationale="answering a different question",
                correlation_id="case/somebody-else",
                epoch_hash=epoch_hash,
                now=now,
            )
        return build_next_candidate_proposal(
            message_id=f"{self.strategy_id}/{self.calls}",
            candidate_id=self.candidate_id,
            rationale=self.mode,
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
            now=now,
        )

    def describe(self) -> Mapping[str, Any]:
        return {"strategyId": self.strategy_id, "strategyKind": self.strategy_kind.value}


class AgentFailureFixture(VerticalFixture):
    def with_strategy(self, mode: str, *, candidate_id: str = "", timeout: float = 2.0, **kwargs):
        path = self.build(**kwargs)
        self.strategy = _Strategy(mode, candidate_id=candidate_id or self.first_candidate_id())
        path.coordinator = StrategyBackedEvidenceCoordinator(
            strategy=self.strategy, timeout_seconds=timeout
        )
        self.coordinator = path.coordinator
        return path

    def rejections(self):
        return [
            envelope.payload["reason"]
            for envelope in self.store.iterate()
            if envelope.event_kind == "AdvisoryRejected"
        ]


class DeterministicFallbackTests(AgentFailureFixture, unittest.TestCase):
    def test_a_timed_out_agent_falls_back_and_the_trial_still_runs(self) -> None:
        path = self.with_strategy("hang", timeout=0.05)
        try:
            candidate_id, rejection = path.request_proposal()
        finally:
            self.strategy.released.set()

        self.assertIsNone(rejection)
        self.assertEqual(candidate_id, self.first_candidate_id())
        self.assertEqual(len(self.coordinator.fallback_events), 1)
        self.assertIn("timeout", self.coordinator.fallback_events[0].reason)

        report = path.run_trial(candidate_id, cell_id=CELL_ID)
        self.assertEqual(report.outcome, TrialOutcome.SUCCESS)
        assert_finite_terminal(self, path, CaseTermination.SUCCESS)
        self.assertEqual(snapshot(self, path, trial_id=report.trial_id), SUCCESS_SNAPSHOT)

    def test_a_raising_agent_falls_back(self) -> None:
        path = self.with_strategy("raise")

        candidate_id, rejection = path.request_proposal()

        self.assertIsNone(rejection)
        self.assertEqual(candidate_id, self.first_candidate_id())
        self.assertIn("exception", self.coordinator.fallback_events[0].reason)

    def test_malformed_output_falls_back(self) -> None:
        path = self.with_strategy("malformed")

        candidate_id, rejection = path.request_proposal()

        self.assertIsNone(rejection)
        self.assertEqual(candidate_id, self.first_candidate_id())
        self.assertIn("malformed", self.coordinator.fallback_events[0].reason)
        report = path.run_trial(candidate_id, cell_id=CELL_ID)
        assert_finite_terminal(self, path, CaseTermination.SUCCESS)
        self.assertEqual(snapshot(self, path, trial_id=report.trial_id), SUCCESS_SNAPSHOT)

    def test_an_answer_to_a_different_question_falls_back(self) -> None:
        """Stale context: a well-formed message about another case."""
        path = self.with_strategy("wrong-correlation")

        candidate_id, rejection = path.request_proposal()

        self.assertIsNone(rejection)
        self.assertEqual(candidate_id, self.first_candidate_id())
        self.assertIn(
            "correlation_id", self.coordinator.fallback_events[0].reason
        )
        report = path.run_trial(candidate_id, cell_id=CELL_ID)
        assert_finite_terminal(self, path, CaseTermination.SUCCESS)
        self.assertEqual(snapshot(self, path, trial_id=report.trial_id), SUCCESS_SNAPSHOT)

    def test_the_fallback_proposal_is_the_same_on_every_run(self) -> None:
        first = self.with_strategy("raise").request_proposal()[0]
        second = self.with_strategy("raise").request_proposal()[0]

        self.assertEqual(first, second)


class HallucinationTests(AgentFailureFixture, unittest.TestCase):
    def test_a_hallucinated_candidate_is_refused_at_the_mailbox(self) -> None:
        path = self.with_strategy("ok", candidate_id="candidate/does-not-exist")

        candidate_id, rejection = path.request_proposal()

        self.assertIsNone(candidate_id)
        self.assertIs(rejection, EnvelopeRejection.EPOCH_MISMATCH)
        self.assertEqual(self.rejections(), ["CANDIDATE_NOT_FROZEN"])
        # The refusal is recorded, and nothing else happened: no trial, no
        # permit, no write.
        self.assertEqual(self.kernel.reduced_state()["trials"], {})
        self.assertEqual(self.adapter.commands, [])
        for _ in range(5):
            candidate_id, rejection = path.request_proposal()
            self.assertIsNone(candidate_id)
            self.assertIs(rejection, EnvelopeRejection.EPOCH_MISMATCH)
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=None),
            PreservationSnapshot(
                actual_outcome=None,
                harm_settlement=(),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=(),
                trial_terminal=None,
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )

    def test_a_message_formed_against_a_stale_epoch_is_refused(self) -> None:
        path = self.build()
        message = build_next_candidate_proposal(
            message_id="stale/1",
            candidate_id=self.first_candidate_id(),
            rationale="formed against a superseded epoch",
            correlation_id=path.case_id,
            epoch_hash="0" * 64,
            now=self.clock(),
        )

        rejection = path.submit(message)

        self.assertIs(rejection, EnvelopeRejection.EPOCH_MISMATCH)
        self.assertEqual(self.kernel.reduced_state()["trials"], {})

    def test_a_replayed_message_is_refused_as_a_duplicate(self) -> None:
        path = self.build()
        message = build_next_candidate_proposal(
            message_id="replay/1",
            candidate_id=self.first_candidate_id(),
            rationale="first submission",
            correlation_id=path.case_id,
            epoch_hash=path.epoch_hash(),
            now=self.clock(),
        )
        self.assertIsNone(path.submit(message))

        # Same sequence again: the mailbox has already accepted that position.
        path._mailbox.sequence = 0
        rejection = path.submit(message)

        self.assertIs(rejection, EnvelopeRejection.STALE_SEQUENCE)
        for _ in range(4):
            self.assertIs(path.submit(message), EnvelopeRejection.STALE_SEQUENCE)
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=None),
            PreservationSnapshot(
                actual_outcome=None,
                harm_settlement=(),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=(),
                trial_terminal=None,
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )

    def test_a_reordered_message_is_refused(self) -> None:
        path = self.build()
        path._mailbox.sequence = 4
        message = build_next_candidate_proposal(
            message_id="reordered/1",
            candidate_id=self.first_candidate_id(),
            rationale="arrived out of order",
            correlation_id=path.case_id,
            epoch_hash=path.epoch_hash(),
            now=self.clock(),
        )

        rejection = path.submit(message)

        self.assertIs(rejection, EnvelopeRejection.REORDERED)
        for _ in range(5):
            self.assertIs(path.submit(message), EnvelopeRejection.REORDERED)
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=None),
            PreservationSnapshot(
                actual_outcome=None,
                harm_settlement=(),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=(),
                trial_terminal=None,
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )


class NoProgressTests(AgentFailureFixture, unittest.TestCase):
    def test_a_repeated_proposal_for_a_consumed_candidate_terminates_finitely(
        self,
    ) -> None:
        """An agent that never changes its mind cannot keep the case alive."""
        path = self.with_strategy(
            "ok", collector=timeseries(start=START, value=1.5, count=40)
        )
        first_candidate = self.first_candidate_id()
        report = path.run_trial(first_candidate, cell_id=CELL_ID)
        self.assertEqual(report.terminal_state, TrialState.SETTLED_NON_SUCCESS)

        refusals = []
        for _ in range(10):
            candidate_id, rejection = path.request_proposal()
            if candidate_id is None:
                refusals.append(rejection)
                continue
            try:
                path.open_trial(candidate_id)
            except KernelRefusal as refusal:
                refusals.append(refusal.reason)
            else:  # pragma: no cover - would mean the candidate was reusable
                self.fail("a consumed candidate must not open a second trial")
            if "PROPOSAL_CAP_REACHED" in refusals:
                break

        self.assertIn("CANDIDATE_NOT_AVAILABLE", refusals)
        self.assertIn("PROPOSAL_CAP_REACHED", refusals)
        # Finite either way: here the one evidence obligation did close (on a
        # fail), so the vector is genuinely exhausted.  ``EVIDENCE_INCOMPLETE``
        # is the terminal when an obligation is still open --
        # ``test_fault_lifecycle.py`` covers that one.
        assert_finite_terminal(self, path, CaseTermination.VECTORS_EXHAUSTED)
        self.assertEqual(
            snapshot(self, path, trial_id=report.trial_id),
            PreservationSnapshot(
                actual_outcome="FAIL",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "CLOSED_FAIL", (("VALID", "SUFFICIENT", "FAIL", False),)),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=("ROLLED_BACK",),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="VECTORS_EXHAUSTED",
            ),
        )

    def test_a_silent_agent_is_not_an_exhaustion_certificate(self) -> None:
        """``None`` from a coordinator says nothing about the vector."""
        path = self.with_strategy("silent")
        path.coordinator = StrategyBackedEvidenceCoordinator(
            strategy=_SilentStrategy(), timeout_seconds=1.0
        )

        candidate_id, rejection = path.request_proposal()

        self.assertIsNone(candidate_id)
        self.assertIsNone(rejection)
        aggregate, _ = self.kernel.exhaustion_certificate(vector_ref="target/steer")
        self.assertEqual(aggregate.value, "EVIDENCE_INCOMPLETE")


class _SilentStrategy:
    strategy_kind = StrategyKind.DETERMINISTIC
    strategy_id = "strategy-silent"

    def propose(self, **kwargs: Any) -> None:
        return None

    def describe(self) -> Mapping[str, Any]:
        return {"strategyId": self.strategy_id}


if __name__ == "__main__":
    unittest.main()
