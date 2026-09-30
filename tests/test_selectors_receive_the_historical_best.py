"""모든 선택기가 **직전 역사 최고와 이번 시행의 최고**를 받는다 (2026-09-23 결정 §2, O-2).

결정 §2 가 선택기에 주라고 명시한 네 가지 중 하나가 "The previous and updated historical
best, with supporting trial references" 였고, `input.*` 14개 키 어디에도 없었다.
`observed_best` 는 갱신된 역사 최고(유효 관측 전체, P1 순)이므로, 더하는 것은 마지막
시행 **이전까지의** 최고와 마지막 시행 **하나만의** 최고다 -- 유지 판정이 비교하는 바로
그 두 값.  해석(개선했나?)은 얹지 않는다; 선택기가 두 값을 나란히 읽는다.
"""
import unittest
from types import SimpleNamespace

from assurance.coordination import (
    Authorization, OMEGA_ONLY_PREFIX, expand_targets, shown_target_ids,
)
from assurance.coordination.tc import Observation, intent_from_sentence
from tools.liveconsole.agent import AgentSitting


def _sitting(observations):
    intents = [intent_from_sentence(s) for s in (
        "I1: UE ueId=131 needs at least 3.0 Mbps downlink, relaxable to 2.0 in 2 steps",
        "I2: UE ueId=132 needs at least 3.0 Mbps downlink, relaxable to 2.0 in 2 steps")]
    omega = expand_targets(Authorization.from_intents(intents))
    sitting = AgentSitting.__new__(AgentSitting)
    sitting.contract = omega
    sitting.evaluation_contract = omega
    sitting.valid_observations = lambda: tuple(observations)
    return sitting


def _obs(index, ue131, ue132):
    return Observation(control_id=f"C{index}", trial_index=index,
                       kpis={"dlGoodputMbps@131": ue131, "dlGoodputMbps@132": ue132},
                       window_end=f"2026-09-23T00:00:{index:02d}Z", valid=True)


class EverySelectorSeesThePreviousAndTheLatestBest(unittest.TestCase):
    def test_an_improving_trial_shows_both_points(self):
        # 시행 1: 둘 다 2.0 만 (가장 느슨한 목표만), 시행 2: 둘 다 3.0 (원 요구 T0).
        sitting = _sitting([_obs(1, 2.0, 2.0), _obs(2, 3.0, 3.0)])
        history = sitting._history(with_ids=False)
        self.assertEqual(2, history["latestTrialIndex"])
        self.assertEqual(1, history["previousBest"]["trialIndex"])
        self.assertEqual(2, history["latestTrialBest"]["trialIndex"])
        # 이 픽스처는 v4.5 동결 워크로드가 아니라 P1 순위 인코딩이 없다(`rank: None` --
        # 검증 안 된 워크로드엔 숫자를 지어내지 않는다).  그래서 레벨로 읽는다:
        # 이번 시행은 원 요구(레벨 0)를, 직전은 양보한 목표를 뒷받침했다.
        self.assertEqual({"I1.r1": 0, "I2.r1": 0}, history["latestTrialBest"]["levels"])
        self.assertTrue(any(history["previousBest"]["levels"].values()),
                        "직전 최고는 양보한 목표였다 -- 선택기가 두 점을 나란히 읽는다")

    def test_the_first_trial_has_no_previous_best(self):
        history = _sitting([_obs(1, 3.0, 3.0)])._history(with_ids=False)
        self.assertIsNone(history["previousBest"])
        self.assertIsNotNone(history["latestTrialBest"])

    def test_no_valid_observation_gives_no_history(self):
        self.assertEqual({}, _sitting([])._history(with_ids=False))

    def test_the_arm_without_T_gets_levels_and_no_target_id(self):
        """BM 은 우리 T 를 안 본다 -- 자리번호 대신 레벨."""
        history = _sitting([_obs(1, 2.0, 3.0)])._history(with_ids=False)
        latest = history["latestTrialBest"]
        self.assertNotIn("targetId", latest)
        self.assertEqual({"I1.r1", "I2.r1"}, set(latest["levels"]))

    def test_the_arms_with_T_see_the_translated_name(self):
        sitting = _sitting([_obs(1, 3.0, 3.0)])
        sitting._shown_ids_cache = shown_target_ids(sitting.evaluation_contract,
                                                    sitting.contract)
        latest = sitting._history(with_ids=True)["latestTrialBest"]
        self.assertEqual("T0", latest["targetId"])
        self.assertNotIn("levels", latest)


if __name__ == "__main__":
    unittest.main()
