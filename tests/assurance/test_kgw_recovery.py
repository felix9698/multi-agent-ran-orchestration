"""Restart, lost acknowledgements and the resolution of uncertain transactions.

Design section 8: "The Kernel and Write Gateway retain durable transaction
phase, fencing token, configuration snapshots, reservations, participant state,
and next safe action. Restart recovery blocks new trials until every uncertain
transaction is queried and safely aborted, finalized, rolled back, or placed in
incident lockdown."

The claim being tested is narrow and load-bearing: the gateway must be able to
say *I do not know*, must keep saying it until it observes something, and must
survive the process that wrote the record.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from assurance.gateway.gateway import TokenBoundWriteGateway
from assurance.gateway.journal import (
    JsonFileTransactionJournal,
    NextSafeAction,
    TransactionPhase,
)
from assurance.gateway.mock_adapter import FaultInjection, MockActuationAdapter
from assurance.gateway.token import TokenKind
from assurance.gateway.write_gateway import GatewayOutcome
from tests.assurance.kgw_support import (
    APPLIED,
    APPLIED_HASH,
    BASELINE,
    BASELINE_HASH,
    PARTIAL,
    PARTIAL_HASH,
    PLAN,
    SAFE_STATE,
    GatewayFixture,
    TestClock,
    token,
)


class ALostAcknowledgementIsNeverAnAbsentChange(GatewayFixture, unittest.TestCase):
    """``UNKNOWN`` and ``REJECTED`` are different answers and stay different."""

    def test_a_confirmed_readback_resolves_a_lost_ack_without_pretending(self):
        self.build(faults=FaultInjection(drop_ack_axes={"queuePriority"}))
        result = self.commit_applied()["commit"]
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertIn("acknowledgement was lost", result.detail)
        self.assertEqual(self.adapter.snapshot(), APPLIED)

    def test_a_lost_ack_with_no_readback_stays_unknown(self):
        self.build()
        self.do_prepare()
        self.do_ready()
        self.adapter.set_faults(
            FaultInjection(drop_ack_axes={"queuePriority"}, unreadable_after_reads=1)
        )
        result = self.do_commit()
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertIsNone(result.observed_config_hash)
        record = self.gateway.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.UNCERTAIN)
        self.assertIs(record.next_safe_action, NextSafeAction.RECOVERY_CONFIRM)
        self.assertEqual(self.gateway.uncertain_transactions(), ("tx-1",))

    def test_an_unknown_commit_is_not_finalizable_and_blocks_on_query(self):
        self.build()
        self.do_prepare()
        self.do_ready()
        self.adapter.set_faults(FaultInjection(unreadable_after_reads=1))
        self.do_commit()
        query = self.gateway.query_transaction("tx-1")
        self.assertIs(query.outcome, GatewayOutcome.UNKNOWN)
        self.assertIn("UNCERTAIN", query.detail)
        self.assertIs(self.do_finalize().outcome, GatewayOutcome.REJECTED)


class RecoveryConfirmObservesRatherThanAssumes(GatewayFixture, unittest.TestCase):
    """Design section 8: an unfounded confirmation would release the block."""

    def uncertain(self, faults=None):
        self.build()
        self.do_prepare()
        self.do_ready()
        self.adapter.set_faults(
            faults or FaultInjection(unreadable_after_reads=1)
        )
        self.do_commit()

    def test_a_live_applied_change_resolves_to_applied(self):
        self.uncertain()
        self.adapter.set_faults(FaultInjection())
        result = self.do_confirm()
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(result.observed_config_hash, APPLIED_HASH)
        record = self.gateway.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.APPLIED)
        self.assertIs(record.next_safe_action, NextSafeAction.REVERSE_ROLLBACK)
        self.assertEqual(self.gateway.uncertain_transactions(), ())

    def test_a_live_baseline_resolves_to_never_applied(self):
        self.uncertain(FaultInjection(fail_axes={"servingCell", "queuePriority"},
                                      unreadable_after_reads=1))
        self.adapter.set_faults(FaultInjection())
        result = self.do_confirm()
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(result.observed_config_hash, BASELINE_HASH)
        record = self.gateway.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.ROLLED_BACK)
        self.assertIs(record.next_safe_action, NextSafeAction.SETTLE)

    def test_a_configuration_that_is_neither_stays_a_partial_apply(self):
        self.uncertain()
        self.adapter.set_faults(FaultInjection())
        self.adapter.apply_drift({"queuePriority": 3})
        result = self.do_confirm()
        self.assertIs(result.outcome, GatewayOutcome.PARTIAL_APPLY)
        record = self.gateway.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.PARTIALLY_APPLIED)
        self.assertIs(record.next_safe_action, NextSafeAction.REVERSE_ROLLBACK)
        self.assertEqual(self.gateway.uncertain_transactions(), ("tx-1",))

    def test_an_unreadable_configuration_is_never_confirmed(self):
        self.uncertain()
        result = self.do_confirm()
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertIsNot(result.outcome, GatewayOutcome.ACKED)
        record = self.gateway.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.UNCERTAIN)
        self.assertEqual(self.gateway.uncertain_transactions(), ("tx-1",))


class ARecordSurvivesTheProcessThatWroteIt(unittest.TestCase):
    """"Durable" means a restart finds it, not that it was in memory once."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "gateway-journal.json"
        self.clock = TestClock()
        self.adapter = MockActuationAdapter(config=dict(BASELINE))

    def gateway(self) -> TokenBoundWriteGateway:
        """A gateway process over the durable journal on disk."""
        return TokenBoundWriteGateway(
            adapters={"mock": self.adapter},
            safe_state=SAFE_STATE,
            journal=JsonFileTransactionJournal(self.path),
            clock=self.clock,
        )

    def test_a_durable_ready_survives_a_restart_and_can_still_commit(self):
        first = self.gateway()
        first.prepare(token=token(TokenKind.PREPARE, sequence=0), plan=dict(PLAN))
        first.ready(token=token(TokenKind.READY, sequence=1))

        second = self.gateway()
        self.assertIs(
            second.transaction_record("tx-1").phase, TransactionPhase.READY
        )
        result = second.commit(token=token(TokenKind.COMMIT, sequence=2))
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(self.adapter.snapshot(), APPLIED)

    def test_a_crash_mid_apply_is_found_as_uncertain_and_then_resolved(self):
        first = self.gateway()
        first.prepare(token=token(TokenKind.PREPARE, sequence=0), plan=dict(PLAN))
        first.ready(token=token(TokenKind.READY, sequence=1))
        self.adapter.set_faults(FaultInjection(crash_axes={"queuePriority"}))
        with self.assertRaises(KeyboardInterrupt):
            first.commit(token=token(TokenKind.COMMIT, sequence=2))

        # A new process over the same journal and the same equipment.
        self.adapter.set_faults(FaultInjection())
        second = self.gateway()
        record = second.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.APPLYING)
        self.assertTrue(record.is_uncertain)
        self.assertEqual(second.uncertain_transactions(), ("tx-1",))
        self.assertIs(
            second.query_transaction("tx-1").outcome, GatewayOutcome.UNKNOWN
        )

        confirmed = second.confirm_recovery(
            token=token(TokenKind.RECOVERY_CONFIRM, fence=2, sequence=0)
        )
        self.assertIs(confirmed.outcome, GatewayOutcome.PARTIAL_APPLY)
        self.assertEqual(confirmed.observed_config_hash, PARTIAL_HASH)
        self.assertEqual(self.adapter.snapshot(), PARTIAL)

        rolled = second.reverse_rollback(
            token=token(
                TokenKind.REVERSE_ROLLBACK, fence=2, sequence=1, expected=PARTIAL_HASH
            )
        )
        self.assertIs(rolled.outcome, GatewayOutcome.ACKED)
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE))
        self.assertEqual(second.uncertain_transactions(), ())

    def test_a_crash_before_any_acknowledgement_reverses_the_whole_plan(self):
        """Nothing was recorded as applied, so nothing may be assumed unapplied."""
        first = self.gateway()
        first.prepare(token=token(TokenKind.PREPARE, sequence=0), plan=dict(PLAN))
        first.ready(token=token(TokenKind.READY, sequence=1))
        self.adapter.set_faults(FaultInjection(crash_axes={"servingCell"}))
        with self.assertRaises(KeyboardInterrupt):
            first.commit(token=token(TokenKind.COMMIT, sequence=2))
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE))

        self.adapter.set_faults(FaultInjection())
        second = self.gateway()
        record = second.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.APPLYING)
        self.assertEqual(record.applied_axes, ())

        rolled = second.reverse_rollback(
            token=token(
                TokenKind.REVERSE_ROLLBACK, fence=2, sequence=0, expected=BASELINE_HASH
            )
        )
        self.assertIs(rolled.outcome, GatewayOutcome.ACKED)
        undone = [
            command["axis"]
            for command in self.adapter.commands
            if command["operation"] == "UNDO"
        ]
        self.assertEqual(undone, ["queuePriority", "servingCell"])
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE))

    def test_the_idempotency_ledger_survives_a_restart(self):
        first = self.gateway()
        first.prepare(token=token(TokenKind.PREPARE, sequence=0), plan=dict(PLAN))
        first.ready(token=token(TokenKind.READY, sequence=1))
        first.commit(token=token(TokenKind.COMMIT, sequence=2))
        writes = list(self.adapter.writes)

        second = self.gateway()
        replay = second.commit(token=token(TokenKind.COMMIT, sequence=2))
        self.assertIs(replay.outcome, GatewayOutcome.ALREADY_APPLIED)
        self.assertEqual(self.adapter.writes, writes)


class QueryingIsHonestAboutWhatIsNotKnown(GatewayFixture, unittest.TestCase):

    def test_an_unseen_transaction_is_unknown_rather_than_an_error(self):
        self.build()
        result = self.gateway.query_transaction("tx-never")
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertIn("no durable record", result.detail)

    def test_a_settled_transaction_reports_its_phase_and_next_safe_action(self):
        self.build()
        self.commit_applied()
        self.do_finalize()
        result = self.gateway.query_transaction("tx-1")
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertIn(TransactionPhase.FINALIZED.value, result.detail)
        self.assertIn(NextSafeAction.SETTLE.value, result.detail)
        self.assertEqual(result.observed_config_hash, APPLIED_HASH)


if __name__ == "__main__":
    unittest.main()
