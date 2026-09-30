"""유지 판정의 '유효' 는 평가기의 '유효' 와 같은 규칙이어야 한다 (2026-09-23 라이브 실측).

v4.6 판 15개에서 유지 판정 23건이 **전부** `observation-invalid` 였다.  평가기가 유효라고
한 창까지 그랬다.  유지 쪽이 집계의 UNKNOWN 목록을 통째로 보았고, 그 안에는 요구조건이
아닌 진단 KPI `lowDeliveryRunBins@ue*` (이 베드에서 늘 UNKNOWN)가 있었다.  그래서 결정 §3
의 유지는 라이브에서 한 번도 일어날 수 없었다 -- 실패가 '복원' 쪽이라 아무 데서도 안 터졌다.
"""
import unittest
from types import SimpleNamespace

from assurance.coordination import Authorization, expand_targets
from assurance.coordination.tc import intent_from_sentence
from tools.liveconsole.agent import AgentSitting, RETENTION_NO_ATTAINMENT


def _sitting():
    intents = [intent_from_sentence(s) for s in (
        "I1: UE ueId=131 needs at least 3.0 Mbps downlink, relaxable to 2.0 in 2 steps",
        "I2: UE ueId=132 needs at least 3.0 Mbps downlink, relaxable to 2.0 in 2 steps")]
    omega = expand_targets(Authorization.from_intents(intents))
    sitting = AgentSitting.__new__(AgentSitting)
    # `intents` 는 속성이라 못 넣는다 -- 판정에 쓰는 키를 같은 규칙으로 준다.
    keys = tuple(intent.requirement.observation_key for intent in intents)
    sitting._required_observation_keys = lambda: keys
    sitting.contract = omega
    sitting.evaluation_contract = omega
    sitting.request = SimpleNamespace(retain_on_improvement=True)
    sitting.previous_best_rank = RETENTION_NO_ATTAINMENT
    sitting.retention_baseline = {}
    sitting.retention_decisions = []
    return sitting


def _aggregate(kpis, unknown):
    return ("2026-09-23T00:00:00Z", (dict(kpis), list(unknown), {}, {}))


class RetentionJudgesValidityLikeTheEvaluator(unittest.TestCase):
    REQUIRED = {"dlGoodputMbps@131": 3.0, "dlGoodputMbps@132": 3.0}

    def test_an_unknown_diagnostic_kpi_does_not_void_the_window(self):
        record = _sitting()._retention_for(
            _aggregate(self.REQUIRED, ["lowDeliveryRunBins@131", "lowDeliveryRunBins@132"]),
            {"servingCell@131": "1"})
        self.assertTrue(record["observationValid"])
        self.assertNotEqual("observation-invalid", record["reason"])

    def test_a_missing_requirement_kpi_still_voids_it(self):
        record = _sitting()._retention_for(
            _aggregate({"dlGoodputMbps@131": 3.0}, ["dlGoodputMbps@132"]), {})
        self.assertFalse(record["observationValid"])
        self.assertEqual("observation-invalid", record["reason"])
        self.assertEqual(["dlGoodputMbps@132"], record["missingObservationKeys"])


if __name__ == "__main__":
    unittest.main()
