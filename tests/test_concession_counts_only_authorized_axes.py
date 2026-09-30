"""완화가 인가되지 않은 요구는 D 에 들어가면 안 된다 (2026-09-21).

그런 요구의 q_r 은 **정의상 언제나 0** 이다 -- 원 목표에서 움직일 수 없으니 양보할
것이 없다.  그것을 owner 평균에 섞으면 그 owner 가 실제로 양보한 만큼이 기계적으로
깎인다.  런타임(`assurance/coordination/tc.py`)은 `if entry.relaxable:` 로 처음부터
걸러 왔는데 논문 집계(`experiments/agent_metrics.py`)는 안 걸렀다.

전수 실측(현행 액션공간 이후, D 가 있는 64판): **33판(52%)** 에서 두 구현의 수가 달랐고,
`ue2-map` 은 완화 불가 요구 하나 때문에 언제나 정확히 절반이 됐다.  어떤 판은 `D_max`
가 1.0 대 0.5 로 **순서까지** 뒤집혔다.  수정 뒤 64판 전부 일치한다.

그리고 양보할 축이 하나도 없으면 D 는 **0 이 아니라 없다**.  0 은 "아무것도 양보하지
않았다"(가장 좋은 결과)로 읽히는데, 실제로는 잴 차원이 없었다는 뜻이다.
"""

from __future__ import annotations

import unittest

from experiments.agent_metrics import concession_of


def episode(*, relaxable: bool):
    """UE 하나는 완화 가능(9.0 -> 5.6), 하나는 인가 여부를 시험이 정한다."""
    return {
        "intents": [
            {"intentId": "I1", "owner": "ue1-video",
             "requirement": {"reqId": "I1.r1", "kpi": "dlGoodputMbps", "scope": "ue@ue1",
                             "op": ">=", "value": 9.0, "unit": "Mbps",
                             "bound": 5.6, "relaxable": True}},
            {"intentId": "I2", "owner": "ue2-map",
             "requirement": {"reqId": "I2.r1", "kpi": "deadlineSuccessRatio",
                             "scope": "ue@ue2", "op": ">=", "value": 0.9, "unit": "ratio",
                             "bound": 0.9 if not relaxable else 0.5,
                             "relaxable": relaxable}},
        ],
        "T": {"authorization": {}},
    }


def target(i1_value: float, i2_value: float):
    return {"targetId": "T", "requirements": {"I1.r1": i1_value, "I2.r1": i2_value}}


class AnUnrelaxableRequirementCarriesNoConcession(unittest.TestCase):
    def test_it_does_not_dilute_its_owner(self):
        quality = concession_of(episode(relaxable=False), target(5.6, 0.9))
        # ue1 은 끝까지 양보했고, ue2 는 양보할 축이 없다.
        self.assertEqual(1.0, quality["perOwner"]["ue1-video"])
        self.assertNotIn("ue2-map", quality["perOwner"], "잴 축이 없는 owner 가 0 으로 셌다")
        self.assertEqual(1.0, quality["max"])
        self.assertEqual(1.0, quality["mean"])
        # 요구 단위 기록에는 그대로 남는다 -- 빠지는 것은 owner 평균에서다.
        self.assertEqual(0.0, quality["perRequirement"]["I2.r1"])

    def test_a_relaxable_sibling_is_averaged_normally(self):
        quality = concession_of(episode(relaxable=True), target(9.0, 0.7))
        self.assertEqual(0.0, quality["perOwner"]["ue1-video"])
        self.assertAlmostEqual(0.5, quality["perOwner"]["ue2-map"])
        self.assertAlmostEqual(0.5, quality["max"])

    def test_with_nothing_relaxable_at_all_there_is_no_d_not_a_zero(self):
        document = episode(relaxable=False)
        document["intents"][0]["requirement"].update(relaxable=False, bound=9.0)
        quality = concession_of(document, target(9.0, 0.9))
        self.assertEqual({}, quality["perOwner"])
        self.assertIsNone(quality["max"], "양보할 축이 없는데 D=0 으로 적었다")
        self.assertIsNone(quality["mean"])


if __name__ == "__main__":
    unittest.main()
