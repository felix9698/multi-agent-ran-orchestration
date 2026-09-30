"""부전승은 세어서 드러내야 한다 (2026-09-21).

판 기록에 `t0Success` 가 있는데 `experiments/agent_metrics.py` 는 그것을 한 번도 읽지
않았다. 실측: 양보값이 기록된 181판 중 90판(50%)이 `bestAttained.trialIndex == 0` -- 초기
측정이 최선이고 8분과 LLM 호출 8회가 그것을 넘지 못했다. 보이지 않으면 해석할 수 없다.

여기서 아무것도 빼지 않는다. 부전승도 결과이고 제외 여부는 실험 설계의 몫이다.
"""
import unittest

from experiments.agent_metrics import walkover_summary


class WalkoversAreCounted(unittest.TestCase):
    ROWS = [
        {"t0Success": True, "bestAttained": {"trialIndex": 0}},
        {"t0Success": False, "bestAttained": {"trialIndex": 4}},
        {"t0Success": False},                      # 양보값 없음
    ]

    def test_it_does_not_report_t0_success(self):
        """`t0Success` 는 "트라이얼 0 성공" 이 아니라 **원 목표 달성**이다.

        실측으로 `bestAttained.concession.max == 0` 과 정확히 일치하고(321판 중 6판,
        예외 0), 그것은 이미 `original_target_attainment()` 이 보고한다. 같은 것을 다른
        출처로 또 세면 두 수가 언젠가 갈린다.
        """
        self.assertNotIn("t0Success", walkover_summary(self.ROWS))

    def test_best_at_trial_zero_is_counted_only_where_concession_exists(self):
        """분모가 다르다 -- 양보값이 없는 판은 '최선이 t0' 인지 말할 수 없다."""
        r = walkover_summary(self.ROWS)["bestAtTrial0"]
        self.assertEqual((r["n"], r["N"]), (1, 2))

    def test_it_reports_how_many_episodes_have_a_concession_at_all(self):
        r = walkover_summary(self.ROWS)["concessionMeasured"]
        self.assertEqual((r["n"], r["N"]), (2, 3))

    def test_an_excluded_episode_is_left_out_but_still_reported(self):
        rows = self.ROWS + [{"bestAttained": {"trialIndex": 0},
                             "excluded": {"rule": "external-equipment-failure",
                                          "reason": "ue died", "at": "t"}}]
        summary = walkover_summary(rows)
        self.assertEqual(summary["N"], 3)
        self.assertEqual(summary["excludedCount"], 1)
        self.assertEqual(summary["bestAtTrial0"]["n"], 1, "제외된 판은 세지 않는다")

    def test_no_episodes_does_not_divide_by_zero(self):
        self.assertIsNone(walkover_summary([])["bestAtTrial0"]["rate"])


if __name__ == "__main__":
    unittest.main()
