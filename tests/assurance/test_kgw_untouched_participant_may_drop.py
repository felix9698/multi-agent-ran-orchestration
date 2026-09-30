"""무관한 UE 가 떨어져도 그 부분은 빼고 진행한다 (2026-09-23 오너 지시).

시도 452 시행 3: ue1 의 cap 만 움직였다.  되돌리기는 나갔고 ue1 정책은 철회됐는데, 확인 읽기가
세 UE 공동 읽기라 **건드리지 않은 ue2** 가 그 순간 이탈해 `r1-cap@ue2 answered UNKNOWN` →
전체 UNKNOWN → 잠금 → 판 `RECOVERY_FAILURE`.

규칙: 이 거래가 **건드리지 않는 축만** 가진 참여자가 못 읽히면 빼고 계획의 기준값으로 채우되,
증거 참조에 제외 사실을 남긴다.  건드린 축의 참여자는 여전히 읽혀야 한다.
"""
from __future__ import annotations

import unittest

from assurance.gateway.gateway import TokenBoundWriteGateway
from assurance.gateway.journal import InMemoryTransactionJournal
from assurance.gateway.mock_adapter import FaultInjection, MockActuationAdapter
from assurance.gateway.plan import config_hash
from assurance.gateway.token import TokenKind
from assurance.gateway.write_gateway import GatewayOutcome

from tests.assurance.kgw_support import TestClock, token

BASELINE = {"cap@ue1": 0, "cap@ue2": 0}
SAFE = {"cap@ue1": 0, "cap@ue2": 0}


class _Owned(MockActuationAdapter):
    """자기 축만 이름 붙여 읽는 참여자 -- 여러 참여자가 표면을 나눠 가진 모양."""

    def __init__(self, axes, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.axes = tuple(axes)
        self.dropped = False

    def _read(self, reference, detail):
        result = super()._read(reference, detail)
        if self.dropped:
            from assurance.gateway.write_gateway import GatewayResult
            return GatewayResult(outcome=GatewayOutcome.UNKNOWN, evidence_refs=(reference,),
                                 detail="the contracted readback did not produce an observation")
        if result.observed_config is None:
            return result
        from assurance.gateway.write_gateway import GatewayResult
        own = {a: v for a, v in result.observed_config.items() if a in self.axes}
        return GatewayResult(outcome=result.outcome, observed_config_hash=config_hash(own),
                             observed_config=own, evidence_refs=result.evidence_refs,
                             detail=result.detail)


class AnUntouchedParticipantThatDropsIsExcluded(unittest.TestCase):
    def setUp(self):
        self.ue1 = _Owned(["cap@ue1"], config=dict(BASELINE))
        self.ue2 = _Owned(["cap@ue2"], config=dict(BASELINE))
        self.gateway = TokenBoundWriteGateway(
            adapters={"ue1": self.ue1, "ue2": self.ue2}, safe_state=SAFE,
            journal=InMemoryTransactionJournal(), clock=TestClock(),
            axis_adapters={"cap@ue1": "ue1", "cap@ue2": "ue2"})
        self.plan = {"adapter": "ue1", "scope": {"ueId": "ue-1"},
                     "baselineConfig": dict(BASELINE),
                     "steps": [{"axis": "cap@ue1", "value": 12},
                               {"axis": "cap@ue2", "value": 0}]}     # ue2 는 제자리
        base = config_hash(BASELINE)
        self.gateway.prepare(token=token(TokenKind.PREPARE, sequence=0, expected=base), plan=self.plan)
        self.gateway.ready(token=token(TokenKind.READY, sequence=1, expected=base))
        self.gateway.commit(token=token(TokenKind.COMMIT, sequence=2, expected=base))
        self.applied = config_hash({"cap@ue1": 12, "cap@ue2": 0})

    def test_the_untouched_ue_dropping_does_not_block_the_rollback(self):
        self.ue2.dropped = True
        rolled = self.gateway.reverse_rollback(
            token=token(TokenKind.REVERSE_ROLLBACK, sequence=3, expected=self.applied))
        self.assertIs(GatewayOutcome.ACKED, rolled.outcome, rolled.detail)
        self.assertEqual(0, self.ue1._config["cap@ue1"], "우리 쓰기는 되돌려졌다")
        self.assertTrue(any("ue2:excluded:unread-untouched" in ref for ref in rolled.evidence_refs),
                        "제외 사실이 증거에 남아야 한다")

    def test_a_participant_this_transaction_wrote_must_still_be_read(self):
        self.ue1.dropped = True           # 건드린 쪽이 못 읽히면 예외가 아니다
        rolled = self.gateway.reverse_rollback(
            token=token(TokenKind.REVERSE_ROLLBACK, sequence=3, expected=self.applied))
        self.assertIsNot(GatewayOutcome.ACKED, rolled.outcome)


if __name__ == "__main__":
    unittest.main()


class TheFirstParticipantIsNotSpecial(AnUntouchedParticipantThatDropsIsExcluded):
    """시도 456: 공동 계획의 첫 참여자(`r1-steer@ue1`)는 순서상 첫째일 뿐이다."""

    def setUp(self):
        self.ue1 = _Owned(["cap@ue1"], config=dict(BASELINE))
        self.ue2 = _Owned(["cap@ue2"], config=dict(BASELINE))
        self.gateway = TokenBoundWriteGateway(
            adapters={"ue1": self.ue1, "ue2": self.ue2}, safe_state=SAFE,
            journal=InMemoryTransactionJournal(), clock=TestClock(),
            axis_adapters={"cap@ue1": "ue1", "cap@ue2": "ue2"})
        # 거래 어댑터는 ue1 이지만 **움직이는 건 ue2** 다.
        self.plan = {"adapter": "ue1", "scope": {"ueId": "ue-1"},
                     "baselineConfig": dict(BASELINE),
                     "steps": [{"axis": "cap@ue1", "value": 0},
                               {"axis": "cap@ue2", "value": 12}]}
        base = config_hash(BASELINE)
        self.gateway.prepare(token=token(TokenKind.PREPARE, sequence=0, expected=base), plan=self.plan)
        self.gateway.ready(token=token(TokenKind.READY, sequence=1, expected=base))
        self.gateway.commit(token=token(TokenKind.COMMIT, sequence=2, expected=base))
        self.applied = config_hash({"cap@ue1": 0, "cap@ue2": 12})

    def test_the_untouched_ue_dropping_does_not_block_the_rollback(self):
        self.ue1.dropped = True              # 첫 참여자지만 건드리지 않았다
        rolled = self.gateway.reverse_rollback(
            token=token(TokenKind.REVERSE_ROLLBACK, sequence=3, expected=self.applied))
        self.assertIs(GatewayOutcome.ACKED, rolled.outcome, rolled.detail)
        self.assertEqual(0, self.ue2._config["cap@ue2"])

    def test_a_participant_this_transaction_wrote_must_still_be_read(self):
        self.ue2.dropped = True
        rolled = self.gateway.reverse_rollback(
            token=token(TokenKind.REVERSE_ROLLBACK, sequence=3, expected=self.applied))
        self.assertIsNot(GatewayOutcome.ACKED, rolled.outcome)

    def test_nothing_readable_is_never_called_the_baseline(self):
        self.ue1.dropped = self.ue2.dropped = True
        rolled = self.gateway.reverse_rollback(
            token=token(TokenKind.REVERSE_ROLLBACK, sequence=3, expected=self.applied))
        self.assertIsNot(GatewayOutcome.ACKED, rolled.outcome)
