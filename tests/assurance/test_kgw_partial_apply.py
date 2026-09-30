"""Partial apply, reverse rollback, safe state and the watchdog.

Design section 7 fixes the rule these tests pin down: every post-commit
non-success path runs through stop, reverse rollback and recovery verification
before another trial may start, and partial apply outranks any semantic
verdict.  Design section 4.4 requires the detection itself: a multi-axis change
that lands halfway must be *observed*, not inferred from acknowledgements.

The fixture deployment writes two axes, so "halfway" is a real state.
"""

from __future__ import annotations

import unittest

from assurance.gateway.commands import GatewayOperation
from assurance.gateway.journal import NextSafeAction, TransactionPhase
from assurance.gateway.mock_adapter import FaultInjection
from assurance.gateway.write_gateway import GatewayOutcome
from tests.assurance.kgw_support import (
    AFTER_LEASE,
    APPLIED,
    APPLIED_HASH,
    BASELINE,
    BASELINE_HASH,
    PARTIAL,
    PARTIAL_HASH,
    SAFE_HASH,
    SAFE_STATE,
    GatewayFixture,
)


class AHalfAppliedChangeIsObserved(GatewayFixture, unittest.TestCase):
    """One axis live and one not is a positively observed inconsistency."""

    def setUp(self):
        self.build(faults=FaultInjection(fail_axes={"queuePriority"}))
        self.results = self.commit_applied()

    def test_the_commit_reports_partial_apply_and_not_a_rejection(self):
        result = self.results["commit"]
        self.assertIs(result.outcome, GatewayOutcome.PARTIAL_APPLY)
        self.assertEqual(result.observed_config_hash, PARTIAL_HASH)
        self.assertEqual(self.adapter.snapshot(), PARTIAL)

    def test_the_durable_record_names_what_is_live_and_what_to_do_next(self):
        record = self.gateway.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.PARTIALLY_APPLIED)
        self.assertIs(record.next_safe_action, NextSafeAction.REVERSE_ROLLBACK)
        self.assertEqual(record.applied_axes, ("servingCell",))
        self.assertTrue(record.is_uncertain)
        self.assertEqual(self.gateway.uncertain_transactions(), ("tx-1",))

    def test_a_partial_apply_cannot_be_finalized(self):
        self.assertIs(self.do_finalize().outcome, GatewayOutcome.REJECTED)

    def test_a_permit_naming_another_state_of_this_plan_still_reverses_to_baseline(self):
        """2026-09-23: the permit names APPLIED, the radio is at PARTIAL -- both are
        states of **this** plan (each axis at its baseline or its planned value).

        A1 effects land out of order and between the Kernel's reread and the
        reversal, so the two can differ without anything foreign being live.
        Reversing the whole plan to its baseline is well defined from any such
        state.  A value outside the plan is still refused
        (``test_kgw_rollback_withdraws_when_unread``).
        """
        result = self.do_rollback(expected=APPLIED_HASH)
        self.assertIs(result.outcome, GatewayOutcome.ACKED, result.detail)
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE))

    def test_rollback_refuses_a_permit_when_something_foreign_is_live(self):
        self.adapter.apply_drift({"prbCap": 99})
        result = self.do_rollback(expected=APPLIED_HASH)
        self.assertIs(result.outcome, GatewayOutcome.REJECTED_CONFIG_MISMATCH)
        self.assertEqual(self.adapter.snapshot()["servingCell"], "cell-2")

    def test_reread_then_rollback_restores_the_baseline(self):
        reread = self.do_reread(expected=APPLIED_HASH)
        self.assertIs(reread.outcome, GatewayOutcome.ACKED)
        self.assertEqual(reread.observed_config_hash, PARTIAL_HASH)
        self.assertIn("differs", reread.detail)

        rolled = self.do_rollback(expected=PARTIAL_HASH, sequence=6)
        self.assertIs(rolled.outcome, GatewayOutcome.ACKED)
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE))
        record = self.gateway.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.ROLLED_BACK)
        self.assertIs(record.next_safe_action, NextSafeAction.SETTLE)
        self.assertEqual(record.applied_axes, ())
        self.assertEqual(self.gateway.uncertain_transactions(), ())


class ALostAcknowledgementStillShowsUpInTheReadback(GatewayFixture, unittest.TestCase):
    """The write landed, the ack did not, and the reread is what decides."""

    def test_a_lost_ack_on_the_first_axis_is_reported_as_partial_apply(self):
        self.build(faults=FaultInjection(drop_ack_axes={"servingCell"}))
        result = self.commit_applied()["commit"]
        self.assertIs(result.outcome, GatewayOutcome.PARTIAL_APPLY)
        self.assertEqual(result.observed_config_hash, PARTIAL_HASH)
        self.assertEqual(
            self.gateway.transaction_record("tx-1").applied_axes, ("servingCell",)
        )


class ReverseRollbackRunsBackwards(GatewayFixture, unittest.TestCase):
    """Undoing a multi-step change forwards can pass through an invalid state."""

    def test_the_reversal_order_is_the_opposite_of_the_apply_order(self):
        self.build()
        self.commit_applied()
        applied = [
            command["axis"]
            for command in self.adapter.commands
            if command["operation"] == GatewayOperation.APPLY.value
        ]
        self.do_rollback()
        undone = [
            command["axis"]
            for command in self.adapter.commands
            if command["operation"] == GatewayOperation.UNDO.value
        ]
        self.assertEqual(applied, ["servingCell", "queuePriority"])
        self.assertEqual(undone, list(reversed(applied)))
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE))

    def test_a_reversal_that_does_not_restore_the_baseline_says_so(self):
        self.build()
        self.commit_applied()
        self.adapter.set_faults(FaultInjection(fail_undo_axes={"servingCell"}))
        result = self.do_rollback()
        self.assertIs(result.outcome, GatewayOutcome.PARTIAL_APPLY)
        record = self.gateway.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.ROLLBACK_INCOMPLETE)
        self.assertIs(record.next_safe_action, NextSafeAction.EMERGENCY_SAFE_STATE)
        self.assertNotEqual(self.adapter.live_hash(), BASELINE_HASH)
        # The sentence alone could not be acted on.  Live on 2026-09-16/17 this
        # branch fired twice while the event it produced carried *equal*
        # expected and observed hashes -- the event records the permit's
        # ``expected_config_hash`` and this branch compares the transaction's
        # ``baseline_config_hash``, two different numbers.  So the detail names
        # all three and a reader can tell which one moved.
        self.assertIn("observed", result.detail)
        self.assertIn("transaction baseline", result.detail)
        self.assertIn("permit expected", result.detail)
        # 그리고 그 수가 **무엇을 세는지**도 말해야 한다.  "reversed 8 of 9" 는
        # 여덟 번이 되돌아갔다는 뜻으로 읽히지만, 어댑터는 **철회할 것이 없을 때도**
        # ACK 한다.  2026-09-17 에 그 오독이 안전 검사를 약화시킬 뻔했다.
        self.assertIn("acknowledged", result.detail)
        self.assertIn("an acknowledgement is not a restoration", result.detail)
        self.assertIn(self.adapter.live_hash()[:12], result.detail)

    def test_observed_equal_to_the_permit_means_the_reversal_did_nothing(self):
        """`observed == permit expected` 는 성공이 아니라 **아무것도 안 움직였다** 이다.

        2026-09-17 에 라이브 상세를 이렇게 읽고 "되돌림은 성공했는데 비교 대상이
        틀렸다" 고 판단해 게이트웨이를 고칠 뻔했다:

            observed 1ac708e1d178 · transaction baseline 13f05ca9aad7
            · permit expected **1ac708e1d178**

        `expected_config_hash` 는 커널이 "행동 **전에** 관측해야 할 것" 으로 정의한
        값이다(`kernel.py`, `observedConfigHash` 또는 `planBaselineHash`).  되돌림
        허가증에서 그것은 **되돌리기 직전의 부분적용 상태**다.  그러니 관측이 그것과
        같다는 건 **undo 가 하나도 듣지 않았다**는 뜻이고, 그것을 성공으로 받으면
        안전 검사를 통째로 무력화한다.

        이 시험은 그 유혹을 못 박는다 -- 허가증 해시를 받아들이도록 고치면 빨개진다.
        """
        self.build()
        self.commit_applied()
        self.adapter.set_faults(FaultInjection(fail_undo_axes={"servingCell", "dlPrbCap"}))
        result = self.do_rollback()
        self.assertIs(result.outcome, GatewayOutcome.PARTIAL_APPLY, result.detail)
        self.assertIn("reversal did not restore", result.detail)
        self.assertNotEqual(self.adapter.live_hash(), BASELINE_HASH,
                            "듣지 않은 undo 가 있으면 기준선에 닿지 못한다")
        # 그리고 게이트웨이는 **기준선 하나만** 본다.  허가증 해시를 받아들이도록
        # 고치면 이 판정이 뒤집히고 안전 검사가 무력해진다.
        self.assertNotIn("permit expected\" matched", result.detail)

    def test_rollback_is_not_offered_before_anything_was_applied(self):
        self.build()
        self.do_prepare()
        self.do_ready()
        result = self.do_rollback(expected=BASELINE_HASH)
        self.assertIs(result.outcome, GatewayOutcome.REJECTED)
        self.assertIn("nothing applied", result.detail)


class StopIsASafetyActionNotAVerdict(GatewayFixture, unittest.TestCase):
    """Design section 7: a stop must not wait, and must work from any state."""

    def setUp(self):
        self.build()
        self.commit_applied()

    def test_stop_does_not_require_the_configuration_to_look_as_expected(self):
        self.adapter.apply_drift({"prbCap": 51})
        result = self.do_stop()
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        record = self.gateway.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.STOPPED)
        self.assertIs(record.next_safe_action, NextSafeAction.REVERSE_ROLLBACK)

    def test_stop_reports_no_observation_because_it_does_not_read(self):
        result = self.do_stop()
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertIsNone(result.observed_config_hash)

    def test_stop_is_safe_to_repeat_from_a_stopping_state(self):
        self.do_stop(sequence=3)
        again = self.do_stop(sequence=4)
        self.assertIs(again.outcome, GatewayOutcome.ACKED)
        self.assertIs(
            self.gateway.transaction_record("tx-1").phase, TransactionPhase.STOPPED
        )

    def test_a_stopped_transaction_can_still_be_reversed(self):
        self.do_stop()
        rolled = self.do_rollback()
        self.assertIs(rolled.outcome, GatewayOutcome.ACKED)
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE))


class EmergencySafeStateWorksFromAnyState(GatewayFixture, unittest.TestCase):
    """Design section 11: Emergency Stop ends at the contracted safe state."""

    def test_it_reaches_the_safe_state_even_when_the_reversal_path_is_broken(self):
        self.build()
        self.commit_applied()
        self.adapter.set_faults(FaultInjection(fail_undo_axes={"servingCell"}))
        self.do_rollback()
        result = self.do_emergency()
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(self.adapter.snapshot(), dict(SAFE_STATE))
        self.assertEqual(result.observed_config_hash, SAFE_HASH)
        record = self.gateway.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.SAFE_STATE)
        self.assertIs(record.next_safe_action, NextSafeAction.SETTLE)

    def test_it_works_when_prepare_never_completed(self):
        self.build()
        result = self.do_emergency(sequence=0)
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(self.adapter.snapshot(), dict(SAFE_STATE))

    def test_an_unconfirmable_safe_state_is_not_reported_as_reached(self):
        self.build()
        self.commit_applied()
        self.adapter.set_faults(FaultInjection(unreadable=True))
        result = self.do_emergency()
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertIs(
            self.gateway.transaction_record("tx-1").next_safe_action,
            NextSafeAction.RECOVERY_CONFIRM,
        )


class TheWatchdogActsWhenTheLeaseRunsOut(GatewayFixture, unittest.TestCase):
    """Task section 6.5: watchdogs stay armed from apply to recovery or finalize.

    The watchdog holds no fresh permit, so it is confined to what the expired
    one already contracted: halt, and the deployment's contracted safe state.
    """

    def test_nothing_fires_while_the_lease_is_alive(self):
        self.build()
        self.commit_applied()
        self.assertEqual(self.gateway.watchdog_check(), ())
        self.assertEqual(self.adapter.snapshot(), APPLIED)

    def test_an_outstanding_change_is_driven_to_safe_state_after_expiry(self):
        self.build()
        self.commit_applied()
        self.clock.now = AFTER_LEASE
        fired = self.gateway.watchdog_check()
        self.assertEqual(len(fired), 1)
        self.assertIs(fired[0].outcome, GatewayOutcome.ACKED)
        self.assertIn("watchdog", fired[0].detail)
        self.assertEqual(self.adapter.snapshot(), dict(SAFE_STATE))
        self.assertIs(
            self.gateway.transaction_record("tx-1").phase, TransactionPhase.SAFE_STATE
        )

    def test_a_finalized_transaction_is_left_alone(self):
        self.build()
        self.commit_applied()
        self.do_finalize()
        self.clock.now = AFTER_LEASE
        self.assertEqual(self.gateway.watchdog_check(), ())
        self.assertEqual(self.adapter.snapshot(), APPLIED)

    def test_the_watchdog_halts_before_it_writes(self):
        self.build()
        self.commit_applied()
        self.adapter.commands.clear()
        self.clock.now = AFTER_LEASE
        self.gateway.watchdog_check()
        operations = [command["operation"] for command in self.adapter.commands]
        self.assertEqual(operations[0], GatewayOperation.HALT.value)


if __name__ == "__main__":
    unittest.main()


class ACountInTheRecordNamesWhatItCounts(GatewayFixture, unittest.TestCase):
    """증거 문장의 수는 **무엇을 세었는지** 말해야 한다.

    2026-09-17 에 `reversed 8 of 9 writes` 를 "여덟 축이 되돌아갔다" 로 읽었다.
    실제로는 ACK 수였고 -- 어댑터는 철회할 것이 없을 때도 ACK 한다 -- 그 오독이
    안전 검사를 약화시킬 뻔했다.  같은 모양이 `N/M axes are live` 에도 있었다:
    그 수는 `max(되읽기로 확인된 수, ACK 수)` 인데 문장은 "live" 라고 단언했다.
    """

    def test_the_applied_count_says_whether_a_readback_confirmed_it(self):
        from assurance.gateway.gateway import _applied_basis
        self.assertIn("confirmed by readback", _applied_basis(3, 3))
        self.assertIn("confirmed by readback", _applied_basis(5, 2))
        # ACK 이 되읽기보다 앞서면 "live" 라고 말하지 않는다
        self.assertNotIn("are live", _applied_basis(1, 4))
        self.assertIn("acknowledged", _applied_basis(1, 4))
        self.assertIn("confirmed only 1", _applied_basis(1, 4))
        self.assertIn("confirmed none", _applied_basis(0, 4))
        self.assertIn("confirmed none", _applied_basis(None, 4))
