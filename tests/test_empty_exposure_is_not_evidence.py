"""계수된 시행이 없는 판은 아무것도 확인하지 않는다 (2026-09-21).

`trial_exposure()` 의 `complete = all(r['complete'] for r in rows)` 는 `rows` 가 비면
True 였다. 그래서 시행이 하나도 계수되지 않은 판이 `exposureMs 0` · `completionConfirmed
True` · "한계 성립" 으로 보고됐다 — 실측 16판. 공허참이 안전 주장의 근거 수를 채우고
방식별 평균 라디오 노출 시간을 0 쪽으로 끌어내린다.
"""
import unittest

from experiments.agent_metrics import trial_exposure


class AnEpisodeWithNoCountedTrials(unittest.TestCase):
    def setUp(self):
        self.result = trial_exposure({"episodeId": "e", "trials": []})

    def test_confirms_nothing(self):
        self.assertFalse(self.result["completionConfirmed"])

    def test_claims_no_exposure_figure(self):
        """0 ms 는 '노출이 없었다' 가 아니라 '세지 않았다' 다."""
        self.assertIsNone(self.result["exposureMs"])

    def test_does_not_claim_the_shortage_bound_holds(self):
        self.assertIn("not established", self.result["trialCountShortageBound"])


if __name__ == "__main__":
    unittest.main()
