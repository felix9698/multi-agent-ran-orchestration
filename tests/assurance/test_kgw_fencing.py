"""Permit misuse: every way a token can be wrong, and what comes back.

Design section 4.4 lists what a Kernel token is tied to and why -- transaction,
trial, fencing token, command sequence, lease, expected configuration hash and
idempotency key -- and each of those is a specific failure it prevents.  This
file is that list as a matrix.

Two rules are asserted throughout:

* **fail-closed with a record.**  A refused command produces a returned result
  with a ``REJECTED*`` outcome and an entry in
  :meth:`TokenBoundWriteGateway.refusals`.  Nothing is silently ignored, and
  nothing reaches the equipment.
* **a retransmission is not a second effect.**  The same permit replayed
  returns ``ALREADY_APPLIED`` with the recorded observation, and the adapter
  sees no additional write.
"""

from __future__ import annotations

import unittest

from assurance.gateway.journal import TransactionPhase
from assurance.gateway.token import KernelToken, TokenKind
from assurance.gateway.write_gateway import GatewayOutcome, GatewayRefusal
from tests.assurance.kgw_support import (
    AFTER_LEASE,
    APPLIED,
    APPLIED_HASH,
    BASELINE,
    BASELINE_HASH,
    GatewayFixture,
    token,
)


class APermitAuthorisesExactlyOneOperation(GatewayFixture, unittest.TestCase):
    """A permit obtained for a side-effect-free prepare cannot commit."""

    def setUp(self):
        self.build()
        self.commit_applied()
        self.adapter.writes.clear()

    def test_every_operation_refuses_a_permit_for_another_operation(self):
        operations = {
            "ready": (self.gateway.ready, TokenKind.READY),
            "commit": (self.gateway.commit, TokenKind.COMMIT),
            "stop": (self.gateway.stop, TokenKind.STOP),
            "reverse_rollback": (self.gateway.reverse_rollback, TokenKind.REVERSE_ROLLBACK),
            "reread_configuration": (
                self.gateway.reread_configuration, TokenKind.CONFIGURATION_REREAD),
            "confirm_recovery": (self.gateway.confirm_recovery, TokenKind.RECOVERY_CONFIRM),
            "emergency_safe_state": (
                self.gateway.emergency_safe_state, TokenKind.EMERGENCY_SAFE_STATE),
            "finalize_live": (self.gateway.finalize_live, TokenKind.FINALIZE_LIVE),
        }
        for name, (operation, kind) in operations.items():
            wrong = TokenKind.PREPARE if kind is not TokenKind.PREPARE else TokenKind.COMMIT
            with self.subTest(operation=name):
                with self.assertRaises(GatewayRefusal):
                    operation(token=token(wrong, sequence=9))
        self.assertEqual(self.adapter.writes, [])
        self.assertEqual(len(self.gateway.refusals()), len(operations))

    def test_prepare_refuses_a_commit_permit(self):
        with self.assertRaises(GatewayRefusal):
            self.gateway.prepare(token=token(TokenKind.COMMIT, sequence=9), plan={})


class AnExpiredLeaseStopsBeingAPermit(GatewayFixture, unittest.TestCase):
    """Design section 4.4: the permit stops being valid on its own."""

    def test_an_expired_lease_is_refused_before_anything_is_dispatched(self):
        self.build()
        self.do_prepare()
        self.do_ready()
        self.clock.now = AFTER_LEASE
        before = len(self.adapter.commands)
        result = self.do_commit()
        self.assertIs(result.outcome, GatewayOutcome.REJECTED_LEASE_EXPIRED)
        self.assertEqual(len(self.adapter.commands), before)
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE))

    def test_even_a_safety_stop_needs_a_live_permit(self):
        """A stale halt is refused; the watchdog is what covers the gap."""
        self.build()
        self.commit_applied()
        self.clock.now = AFTER_LEASE
        self.assertIs(self.do_stop().outcome, GatewayOutcome.REJECTED_LEASE_EXPIRED)


class AnOlderFenceIsRefusedRatherThanAppliedLate(GatewayFixture, unittest.TestCase):
    """Task section 6.12's old-fence rule."""

    def setUp(self):
        self.build()
        self.commit_applied(fence=2)

    def test_a_delayed_command_from_a_superseded_attempt_is_refused(self):
        result = self.gateway.stop(token=token(TokenKind.STOP, fence=1, sequence=3))
        self.assertIs(result.outcome, GatewayOutcome.REJECTED_FENCE)
        self.assertEqual(self.adapter.snapshot(), APPLIED)

    def test_a_reordered_command_inside_one_fence_is_refused(self):
        result = self.gateway.stop(token=token(TokenKind.STOP, fence=2, sequence=1))
        self.assertIs(result.outcome, GatewayOutcome.REJECTED_FENCE)

    def test_a_new_fence_supersedes_and_is_accepted(self):
        result = self.gateway.stop(token=token(TokenKind.STOP, fence=3, sequence=0))
        self.assertIs(result.outcome, GatewayOutcome.ACKED)

    def test_the_gateway_fences_exactly_as_the_frozen_helper_does(self):
        record = self.gateway.transaction_record("tx-1")
        newest = KernelToken.from_canonical_dict(record.last_token)
        for fence, sequence, superseded in (
            (1, 9, True), (2, 1, True), (2, 2, False), (2, 3, False), (3, 0, False),
        ):
            candidate = token(TokenKind.STOP, fence=fence, sequence=sequence)
            with self.subTest(fence=fence, sequence=sequence):
                self.assertEqual(newest.fences_out(candidate), superseded)
                outcome = self.gateway.stop(token=candidate).outcome
                self.assertEqual(
                    outcome is GatewayOutcome.REJECTED_FENCE, superseded
                )


class TheObservedConfigurationMustMatchThePermit(GatewayFixture, unittest.TestCase):
    """``expected_config_hash`` is what makes a blind overwrite impossible."""

    def test_a_commit_naming_a_configuration_that_is_not_live_is_refused(self):
        self.build()
        self.do_prepare()
        self.do_ready()
        result = self.gateway.commit(
            token=token(TokenKind.COMMIT, sequence=2, expected=APPLIED_HASH)
        )
        self.assertIs(result.outcome, GatewayOutcome.REJECTED_CONFIG_MISMATCH)
        self.assertEqual(self.adapter.writes, [])
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE))

    def test_an_out_of_band_change_between_ready_and_commit_is_caught(self):
        self.build()
        self.do_prepare()
        self.do_ready()
        self.adapter.apply_drift({"prbCap": 51})
        result = self.do_commit()
        self.assertIs(result.outcome, GatewayOutcome.REJECTED_CONFIG_MISMATCH)
        self.assertEqual(self.adapter.writes, [])
        self.assertNotEqual(result.observed_config_hash, BASELINE_HASH)


class OneKeyIsOneEffect(GatewayFixture, unittest.TestCase):
    """Design section 4.4: a retried command with the same key is the same command."""

    def test_a_retransmitted_commit_produces_no_second_effect(self):
        self.build()
        self.do_prepare()
        self.do_ready()
        first = self.do_commit()
        writes = list(self.adapter.writes)
        second = self.do_commit()
        self.assertIs(first.outcome, GatewayOutcome.ACKED)
        self.assertIs(second.outcome, GatewayOutcome.ALREADY_APPLIED)
        self.assertEqual(self.adapter.writes, writes)
        self.assertEqual(second.observed_config_hash, first.observed_config_hash)
        self.assertEqual(second.evidence_refs, first.evidence_refs)
        self.assertEqual(self.adapter.snapshot(), APPLIED)

    def test_a_retransmission_is_recognised_even_though_the_configuration_moved(self):
        """The permit still names the pre-change configuration; that is the point."""
        self.build()
        self.commit_applied()
        replay = self.do_commit()
        self.assertIs(replay.outcome, GatewayOutcome.ALREADY_APPLIED)
        self.assertIsNot(replay.outcome, GatewayOutcome.REJECTED_CONFIG_MISMATCH)

    def test_the_same_key_for_a_different_command_is_a_collision(self):
        self.build()
        self.do_prepare()
        self.do_ready()
        self.do_commit()
        colliding = token(
            TokenKind.STOP, sequence=3, idempotency_key="tx-1:COMMIT:1:2"
        )
        result = self.gateway.stop(token=colliding)
        self.assertIs(result.outcome, GatewayOutcome.REJECTED_IDEMPOTENCY_COLLISION)

    def test_a_re_prepare_does_not_clear_the_effect_ledger(self):
        """A second attempt on one transaction must not free a spent key."""
        self.build()
        self.do_prepare()
        self.do_prepare(fence=2)
        colliding = token(
            TokenKind.STOP, fence=2, sequence=3, idempotency_key="tx-1:PREPARE:1:0"
        )
        self.assertIs(
            self.gateway.stop(token=colliding).outcome,
            GatewayOutcome.REJECTED_IDEMPOTENCY_COLLISION,
        )

    def test_a_key_reused_by_another_transaction_is_a_collision(self):
        self.build()
        self.commit_applied()
        result = self.gateway.prepare(
            token=token(
                TokenKind.PREPARE,
                transaction_id="tx-2",
                idempotency_key="tx-1:COMMIT:1:2",
            ),
            plan={},
        )
        self.assertIs(result.outcome, GatewayOutcome.REJECTED_IDEMPOTENCY_COLLISION)


class APermitMustMatchItsDurableTransaction(GatewayFixture, unittest.TestCase):

    def test_a_permit_for_an_unknown_transaction_reaches_nothing(self):
        self.build()
        for name, operation, kind in (
            ("ready", self.gateway.ready, TokenKind.READY),
            ("commit", self.gateway.commit, TokenKind.COMMIT),
            ("stop", self.gateway.stop, TokenKind.STOP),
            ("rollback", self.gateway.reverse_rollback, TokenKind.REVERSE_ROLLBACK),
            ("reread", self.gateway.reread_configuration, TokenKind.CONFIGURATION_REREAD),
            ("recover", self.gateway.confirm_recovery, TokenKind.RECOVERY_CONFIRM),
            ("finalize", self.gateway.finalize_live, TokenKind.FINALIZE_LIVE),
        ):
            with self.subTest(operation=name):
                result = operation(token=token(kind, transaction_id="tx-never"))
                self.assertIs(result.outcome, GatewayOutcome.REJECTED)
        self.assertEqual(self.adapter.commands, [])

    def test_a_permit_naming_another_trial_is_refused(self):
        self.build()
        self.do_prepare()
        result = self.gateway.ready(
            token=token(TokenKind.READY, sequence=1, trial_id="trial-2")
        )
        self.assertIs(result.outcome, GatewayOutcome.REJECTED)
        self.assertIn("different trial", result.detail)
        self.assertIs(
            self.gateway.transaction_record("tx-1").phase, TransactionPhase.PREPARED
        )


class EveryRefusalIsRecorded(GatewayFixture, unittest.TestCase):
    """"Fail-closed" means a returned record, never a silent drop."""

    def test_each_refusal_kind_lands_in_the_refusal_log(self):
        self.build()
        self.do_prepare()
        self.do_ready()
        self.gateway.commit(token=token(TokenKind.COMMIT, sequence=2, expected=APPLIED_HASH))
        self.do_commit()
        self.gateway.stop(token=token(TokenKind.STOP, fence=0, sequence=3))
        self.clock.now = AFTER_LEASE
        self.gateway.stop(token=token(TokenKind.STOP, fence=5, sequence=4))
        recorded = [entry["detail"].split(":")[0] for entry in self.gateway.refusals()]
        self.assertIn(GatewayOutcome.REJECTED_CONFIG_MISMATCH.value, recorded)
        self.assertIn(GatewayOutcome.REJECTED_FENCE.value, recorded)
        self.assertIn(GatewayOutcome.REJECTED_LEASE_EXPIRED.value, recorded)
        for entry in self.gateway.refusals():
            self.assertEqual(entry["transactionId"], "tx-1")
            self.assertIn("detail", entry)


if __name__ == "__main__":
    unittest.main()
