"""읽을 수 없다는 이유로 우리가 쓴 것을 안 지우면 안 된다 (2026-09-23 라이브 판).

판 `formal38guarded-20260923T060007`: 방식이 ue2 하나를 조종했다.  A1 정책은 **생성**됐고
(policyId 45792da9…) 커밋 되읽기는 기준선을 보였다.  커널이 REVERSE_ROLLBACK 을 냈고,
Gateway 의 첫 되읽기가 값을 못 받았다.  Gateway 는 **UNDO 를 하나도 보내지 않고**
`UNKNOWN: the configuration could not be read` 로 거절했다 -- 그래서 우리가 만든 정책이
RIC 에 그대로 남은 채 커널이 잠갔다 (`INCIDENT_LOCKDOWN` → `RECOVERY_FAILURE`).

같은 함수 안의 주석이 이미 반대 방향을 정해 두었다: 불확정이면 "reverse the whole plan.
Restoring the baseline value of an axis that was never written costs one command and is the
safe direction to be wrong in."  **읽을 수 없음**은 불확정이다.  그런데 선행 읽기가
실패하면 그 원칙에 닿기도 전에 거절하고 있었다.

지키는 것 두 가지 (약화하지 않는다):
1. 선행 읽기가 **성공했는데** 허가증과 다른 설정을 말하면 여전히 `REJECTED_CONFIG_MISMATCH`
   -- 모르는 설정 위에서 되돌리지 않는다는 펜싱 규칙은 그대로다.
2. 되돌린 뒤 **확인 읽기가 기준선을 보이지 않으면** 여전히 ACKED 가 아니다.
"""
from __future__ import annotations

import unittest

from assurance.gateway.mock_adapter import FaultInjection, MockActuationAdapter
from assurance.gateway.write_gateway import GatewayOutcome

from tests.assurance.kgw_support import (
    APPLIED_HASH, BASELINE, BASELINE_HASH, GatewayFixture, PARTIAL_HASH,
)


class _FailTheNextRead(MockActuationAdapter):
    """정확히 **다음 N번** 읽기만 실패한다 -- 쓰기는 정상이다.

    라이브에서 본 것: 정책을 만든 직후 그 UE 의 되읽기가 잠깐 비고(producer 가
    `RECOVERY_PENDING` 로 보고), 정책을 지우면 다시 읽힌다.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_next = 0

    def _read(self, reference, detail):
        if self.fail_next > 0:
            self.fail_next -= 1
            from assurance.gateway.write_gateway import GatewayResult
            return GatewayResult(outcome=GatewayOutcome.ERROR,
                                 evidence_refs=(reference,),
                                 detail="injected: readback unavailable right after the write")
        return super()._read(reference, detail)


class TheRollbackWithdrawsWhatWeWroteEvenWhenItCannotReadFirst(GatewayFixture, unittest.TestCase):
    def _partially_applied(self):
        """servingCell 은 적용, queuePriority 는 거절 -- 실측 판과 같은 PARTIAL_APPLY."""
        radio = _FailTheNextRead(
            config=dict(BASELINE), faults=FaultInjection(fail_axes={"queuePriority"}))
        self.build(adapters={"mock": radio})
        # `build()` 은 늘 새 목을 `self.adapter` 에 만든다; 게이트웨이가 쓰는 것은 radio 다.
        self.adapter = radio
        results = self.commit_applied()
        self.assertIs(GatewayOutcome.PARTIAL_APPLY, results["commit"].outcome)
        self.assertEqual("cell-2", self.adapter._config["servingCell"])
        return results

    def test_an_unreadable_pre_read_still_sends_the_reversal(self):
        self._partially_applied()
        self.adapter.fail_next = 1             # 선행 읽기만 실패
        result = self.do_rollback(expected=PARTIAL_HASH)
        self.assertEqual("cell-1", self.adapter._config["servingCell"],
                         "쓴 것을 지우지 않았다 -- 우리 정책이 RIC 에 남는다")
        self.assertIs(GatewayOutcome.ACKED, result.outcome, result.detail)
        self.assertEqual(BASELINE_HASH, result.observed_config_hash)

    def test_withdrawing_without_a_confirming_read_is_not_a_success(self):
        """되돌림은 보냈지만 확인 못 하면 여전히 UNKNOWN 이다 -- 성공으로 세지 않는다."""
        self._partially_applied()
        from assurance.gateway.gateway import REVERSAL_CONFIRM_READS
        self.adapter.fail_next = 1 + REVERSAL_CONFIRM_READS   # 선행 읽기와 모든 확인 읽기 실패
        result = self.do_rollback(expected=PARTIAL_HASH)
        self.assertEqual("cell-1", self.adapter._config["servingCell"],
                         "확인을 못 해도 되돌림은 나가야 한다")
        self.assertIs(GatewayOutcome.UNKNOWN, result.outcome)
        self.assertIn("could not be read", result.detail)

    def test_a_confirming_read_that_misses_once_is_read_again(self):
        """2026-09-28 판 860: 되돌림 직후 한 번 못 읽은 것으로 잠그지 않는다 -- 허가증 안에서 다시 읽는다."""
        self._partially_applied()
        self.adapter.fail_next = 2             # 선행 읽기 + 첫 확인 읽기 실패, 두 번째 확인 읽기 성공
        result = self.do_rollback(expected=PARTIAL_HASH)
        self.assertIs(GatewayOutcome.ACKED, result.outcome, result.detail)
        self.assertEqual(BASELINE_HASH, result.observed_config_hash)

    def test_a_read_that_names_another_configuration_is_still_refused(self):
        """펜싱 규칙은 그대로다: 읽혔는데 다른 설정이면 되돌리지 않는다."""
        self._partially_applied()
        self.adapter.apply_drift({"prbCap": 99})   # 우리가 모르는 설정
        result = self.do_rollback(expected=PARTIAL_HASH)
        self.assertIs(GatewayOutcome.REJECTED_CONFIG_MISMATCH, result.outcome)
        self.assertEqual("cell-2", self.adapter._config["servingCell"],
                         "모르는 설정 위에서 되돌리면 안 된다")


if __name__ == "__main__":
    unittest.main()


class _AcceptedButNotYetOnAir(MockActuationAdapter):
    """A1 정책처럼: 쓰기를 **접수**하지만 무선 설정은 아직 안 바뀐다."""

    def _write(self, command, reference):
        from assurance.gateway.commands import GatewayOperation
        from assurance.gateway.write_gateway import GatewayResult
        if GatewayOperation(command["operation"]) is GatewayOperation.APPLY:
            return GatewayResult(outcome=GatewayOutcome.ACKED, evidence_refs=(reference,),
                                 detail="policy created; effect not yet observed")
        return super()._write(command, reference)


class ANoOpFirstAxisDoesNotMakeTheDetailLie(GatewayFixture, unittest.TestCase):
    """계획 첫 축이 제자리면 `matched` 가 1 로 나와 "confirmed only 1" 이라 적혔다.

    라이브 판의 상세는 "9/9 axes were acknowledged (the readback confirmed only 1)" 였다.
    실제로는 **아무 축도** 무선에 안 보였다.  라우팅(되돌림 의무)은 옳으니 그대로 두고
    문장만 사실대로 쓴다.
    """

    def test_the_detail_says_the_baseline_is_still_live(self):
        radio = _AcceptedButNotYetOnAir(config=dict(BASELINE))
        self.build(adapters={"mock": radio})
        plan = {"adapter": "mock", "scope": {"guAmfUeNgapId": "ue-1"},
                "baselineConfig": dict(BASELINE),
                "steps": [{"axis": "prbCap", "value": 24},          # 제자리 축이 먼저
                          {"axis": "servingCell", "value": "cell-2"}]}
        self.do_prepare(plan=plan)
        self.do_ready()
        result = self.do_commit()
        self.assertIs(GatewayOutcome.PARTIAL_APPLY, result.outcome,
                      "되돌림 의무는 그대로여야 한다 -- 정책은 만들어졌다")
        self.assertEqual(BASELINE_HASH, result.observed_config_hash)
        self.assertIn("readback still shows the baseline", result.detail)
        self.assertIn("owed a reversal", result.detail)
        self.assertNotIn("confirmed only", result.detail)


class AnOwnPartialApplyOutOfOrderIsStillOurs(GatewayFixture, unittest.TestCase):
    """A1 효과는 순서 없이 착지한다 (2026-09-23 라이브 판 20260923T083611 시행 3).

    계획 [servingCell, queuePriority] 에서 **두 번째만** 들어간 상태는 접두가 아니다.
    예전엔 '모르는 설정' 으로 거절·잠금되어 우리 정책이 RIC 에 남았다.
    """

    def _second_step_only(self):
        """커밋이 전부 들어간 뒤, 첫 축(조종)의 효과가 아직/다시 안 보이는 상태."""
        radio = _FailTheNextRead(config=dict(BASELINE))
        self.build(adapters={"mock": radio})
        self.adapter = radio
        self.commit_applied()
        self.adapter.apply_drift({"servingCell": "cell-1"})
        self.assertEqual({"servingCell": "cell-1", "prbCap": 24, "queuePriority": 7},
                         self.adapter.snapshot(), "두 번째 축만 들어간 상태")

    def test_the_subset_digests_include_every_combination(self):
        from assurance.gateway.plan import ActuationPlan, config_hash
        from tests.assurance.kgw_support import PLAN
        subsets = ActuationPlan.from_mapping(PLAN).subset_hashes()
        self.assertEqual(4, len(subsets))
        self.assertIn(config_hash({**BASELINE, "queuePriority": 7}), subsets,
                      "접두가 아닌 부분 적용")

    def test_a_readable_own_state_other_than_the_permit_is_reversed(self):
        self._second_step_only()
        # 허가증은 커밋 때 본 전체 적용을 적었지만, 지금은 두 번째 축만 살아 있다.
        result = self.do_rollback(expected=APPLIED_HASH)
        self.assertIs(GatewayOutcome.ACKED, result.outcome, result.detail)
        self.assertEqual(dict(BASELINE), self.adapter.snapshot())

    def test_a_value_outside_the_plan_is_still_refused(self):
        self._second_step_only()
        self.adapter.apply_drift({"prbCap": 99})      # 계획에 없는 값
        result = self.do_rollback(expected=APPLIED_HASH)
        self.assertIs(GatewayOutcome.REJECTED_CONFIG_MISMATCH, result.outcome)


class AnAcceptedWriteIsNeverCalledRefusedBecauseOfAxisOrder(GatewayFixture, unittest.TestCase):
    """2026-09-23 시도 455 시행 1: 조종 정책은 **생성됐는데**(CREATE ISSUED) 되읽기가 아직
    기준선이라 커밋이 `REJECTED refused downstream with the baseline confirmed live` 가 됐고
    적용 축 `()` -- 철회 의무가 사라졌다.  같은 상황에서 조종이 계획의 **두 번째** 축이었던
    판 450 은 `PARTIAL_APPLY`(의무 유지)였다.  판정이 축 순서에 달려 있었다.
    과거 잠금 43건 중 'R1 거절' 13건이 이 부류.
    """

    def _commit(self, steps):
        radio = _AcceptedButNotYetOnAir(config=dict(BASELINE))
        self.build(adapters={"mock": radio})
        plan = {"adapter": "mock", "scope": {"guAmfUeNgapId": "ue-1"},
                "baselineConfig": dict(BASELINE), "steps": steps}
        self.do_prepare(plan=plan)
        self.do_ready()
        return self.do_commit()

    def test_a_moving_first_step_accepted_but_unseen_owes_a_reversal(self):
        result = self._commit([{"axis": "servingCell", "value": "cell-2"},     # 움직이는 축이 먼저
                               {"axis": "prbCap", "value": 24}])
        self.assertIs(GatewayOutcome.PARTIAL_APPLY, result.outcome, result.detail)
        self.assertIn("still shows the baseline", result.detail)
        self.assertNotIn("refused downstream", result.detail)

    def test_the_verdict_does_not_depend_on_axis_order(self):
        first = self._commit([{"axis": "servingCell", "value": "cell-2"},
                              {"axis": "prbCap", "value": 24}]).outcome
        second = self._commit([{"axis": "prbCap", "value": 24},
                               {"axis": "servingCell", "value": "cell-2"}]).outcome
        self.assertIs(first, second)
