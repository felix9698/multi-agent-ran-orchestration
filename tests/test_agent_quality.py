"""결정 §5.3 의 비교 지표 (2026-09-23 답신).

픽스처는 실제 v4.5 판의 **고정 인가**다 (Omega 144 목표, P1 순위 0~71, T0=0,
T1..T3 = 1, T72 = 36).  시행은 합성한다: `success` 는 판이 쓰는 그대로 Omega id 키.
"""
import json
import unittest
from pathlib import Path

from experiments.agent_quality import (
    INITIAL_NONE, INITIAL_RELAXED, INITIAL_T0, INITIAL_UNKNOWN,
    episode_quality, quality_summary,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" /
                      "v45_pinned_authorization.json").read_text(encoding="utf-8"))
B = FIXTURE["budget"]["deadlineBMs"]            # 480000


def _trial(index, elapsed, attained=(), *, valid=True, counted=True):
    return {"trialIndex": index, "elapsedMs": elapsed, "counted": counted,
            "observationValidity": {"valid": valid},
            "success": {target: True for target in attained}}


def _episode(*trials, method="three-agent", retained=None):
    return {**json.loads(json.dumps(FIXTURE)), "method": method, "episodeId": method,
            "trials": list(trials), "retained": retained or {}, "termination": {}}


class TheInitialStateHasFourClasses(unittest.TestCase):
    def test_t0_relaxed_none(self):
        self.assertEqual(INITIAL_T0, episode_quality(_episode(_trial(0, 30000, ["T0"])))["initialState"])
        self.assertEqual(INITIAL_RELAXED, episode_quality(_episode(_trial(0, 30000, ["T72"])))["initialState"])
        self.assertEqual(INITIAL_NONE, episode_quality(_episode(_trial(0, 30000)))["initialState"])

    def test_an_invalid_reference_is_unknown_not_none_met(self):
        """§5.1: "Its initial satisfaction class is `unknown`, not `none_met`."""
        row = episode_quality(_episode(_trial(0, 30000, ["T0"], valid=False)))
        self.assertEqual(INITIAL_UNKNOWN, row["initialState"])

    def test_a_missing_reference_is_unknown(self):
        self.assertEqual(INITIAL_UNKNOWN, episode_quality(_episode(_trial(1, 60000)))["initialState"])


class TqIsTheFirstValidEvaluationWithinB(unittest.TestCase):
    def test_each_threshold_takes_its_own_first_time(self):
        row = episode_quality(_episode(
            _trial(0, 30000, ["T72"]),          # rank 36: meets q=71 only
            _trial(1, 90000, ["T1"]),           # rank 1: meets q=1 and q=71
            _trial(2, 150000, ["T0"])))         # rank 0: meets every q
        self.assertEqual({"0": 150000.0, "1": 90000.0, "71": 30000.0}, row["tQMs"])
        self.assertEqual({"0": 3, "1": 2, "71": 1}, row["dispatchesToQ"])

    def test_an_invalid_trial_gives_no_credit(self):
        row = episode_quality(_episode(_trial(0, 30000), _trial(1, 90000, ["T0"], valid=False)))
        self.assertIsNone(row["tQMs"]["0"])

    def test_a_result_after_B_is_outside_the_budget(self):
        row = episode_quality(_episode(_trial(0, 30000), _trial(1, B + 1, ["T0"])))
        self.assertIsNone(row["tQMs"]["0"])
        self.assertIsNone(row["bestRankByB"])


class TheEfficiencyCountsSeparateTheReference(unittest.TestCase):
    def test_search_dispatches_exclude_the_counted_reference(self):
        row = episode_quality(_episode(_trial(0, 30000), _trial(1, 90000, valid=False),
                                       _trial(2, 150000)))
        self.assertEqual(3, row["formalDispatches"])
        self.assertEqual(2, row["searchDispatches"])
        self.assertEqual(1, row["invalidDispatches"])

    def test_an_uncounted_reference_is_not_subtracted(self):
        row = episode_quality(_episode(_trial(0, 0, counted=False), _trial(1, 90000)))
        self.assertEqual(1, row["formalDispatches"])
        self.assertEqual(1, row["searchDispatches"])


class TheRestrictedMeanKeepsFailures(unittest.TestCase):
    def test_nonattainment_enters_as_B_and_is_counted(self):
        summary = quality_summary([
            _episode(_trial(0, 30000, ["T0"])),             # T_0 = 30 s
            _episode(_trial(0, 30000)),                     # never: counts as B
        ])
        q0 = summary["tQ"]["0"]["all"]
        self.assertEqual((1, 2), (q0["attained"], q0["total"]))
        self.assertEqual((30000.0 + B) / 2, q0["restrictedMeanMs"])
        self.assertEqual(30000.0, q0["conditionalMeanMs"])

    def test_the_unmet_at_reference_subgroup_leaves_out_walkovers(self):
        summary = quality_summary([
            _episode(_trial(0, 30000, ["T0"])),                          # already met
            _episode(_trial(0, 30000), _trial(1, 90000, ["T0"])),        # resolved by search
            _episode(_trial(0, 30000, ["T0"], valid=False)),             # unknown: in neither
        ])
        unmet = summary["tQ"]["0"]["unmetAtReference"]
        self.assertEqual((1, 1), (unmet["attained"], unmet["total"]))
        self.assertEqual(90000.0, unmet["conditionalMeanMs"])
        self.assertEqual({INITIAL_T0: 1, INITIAL_RELAXED: 0, INITIAL_NONE: 1, INITIAL_UNKNOWN: 1},
                         summary["initialState"])


class TheFinalPreferenceIsSeparateFromRetention(unittest.TestCase):
    def test_best_by_B_with_its_owner_vector(self):
        row = episode_quality(_episode(_trial(0, 30000, ["T72"]), _trial(1, 90000, ["T1"]),
                                       retained={"controlId": "C3", "qualified": False,
                                                 "detail": "recovery-outstanding"}))
        self.assertEqual(1, row["bestRankByB"])
        self.assertEqual(4, len(row["bestOwnerVectorByB"]))
        self.assertFalse(row["finalConfiguration"]["retentionQualified"],
                         "역사적 달성과 성공한 유지는 따로 보고한다 (§3.4)")

    def test_unresolved_fraction_over_all_episodes(self):
        summary = quality_summary([_episode(_trial(0, 30000, ["T0"])), _episode(_trial(0, 30000))])
        self.assertEqual(0.5, summary["finalPreference"]["unresolvedFraction"])


if __name__ == "__main__":
    unittest.main()


class TheOperatorsInitialSatisfactionIsReportedApart(unittest.TestCase):
    """§1: "report how often the operator requirement and the complete T0 are
    initially satisfied" -- two numbers, not one."""

    def test_per_owner_from_the_reference_T0_verdicts(self):
        verdicts = {"T0": {"I0c.r1": "PASS", "I1d.r1": "FAIL", "I1g.r1": "PASS",
                           "I2d.r1": "PASS", "I2g.r1": "PASS",
                           "I3d.r1": "PASS", "I3g.r1": "PASS"}}
        reference = dict(_trial(0, 30000), verdicts=verdicts)
        row = episode_quality(_episode(reference))
        met = row["initialOwnerOriginalMet"]
        self.assertTrue(met["operator-gnb2"], "사업자 요구는 원래 수준에서 이미 만족")
        self.assertFalse(met["ue1-video"], "ue1 은 d 요구 하나가 FAIL")
        self.assertEqual(INITIAL_NONE, row["initialState"], "전체 T0 는 아니다")

    def test_an_invalid_reference_makes_every_owner_unknown(self):
        row = episode_quality(_episode(_trial(0, 30000, valid=False)))
        self.assertTrue(all(v is None for v in row["initialOwnerOriginalMet"].values()))

    def test_the_summary_counts_met_not_met_and_unknown(self):
        verdicts = {"T0": {r: "PASS" for r in ("I0c.r1", "I1d.r1", "I1g.r1", "I2d.r1",
                                               "I2g.r1", "I3d.r1", "I3g.r1")}}
        summary = quality_summary([
            _episode(dict(_trial(0, 30000, ["T0"]), verdicts=verdicts)),
            _episode(_trial(0, 30000, valid=False))])
        self.assertEqual({"met": 1, "notMet": 0, "unknown": 1},
                         summary["initialOwnerOriginalMet"]["operator-gnb2"])
