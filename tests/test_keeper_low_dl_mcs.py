"""2026-09-27: a UE that came up in a bad state (ue3 MCS 8 at RSRP -95 beside ue2 at 25) is found
from the gNB stats while loaded; idle UEs and a lone loaded UE are never judged."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "keeper_mod", Path(__file__).resolve().parents[1] / "experiment_results/ota-20260911/ops/keeper.py")
keeper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keeper)


def rows(rnti, mcs, step):
    return "\n".join(f"UE {rnti}: dlsch_rounds {1000 + i * step}/1/1/0, dlsch_errors 0, pucch0_DTX 1 "
                     f"(SNR 21 dB), BLER 0.1 MCS (0) {mcs} CCE fail 1" for i in range(5))


class LowDlMcs(unittest.TestCase):
    def test_lagging_ue_beside_a_good_peer(self):
        text = "\n".join((rows("7675", 25, 1000), rows("9833", 8, 1000), rows("4e8a", 0, 10)))
        self.assertEqual([("9833", 8)], keeper.lagging_mcs_ues(text))

    def test_only_the_newest_samples_count(self):
        ramp = rows("9833", 1, 1000) + "\n" + "\n".join(
            f"UE 9833: dlsch_rounds {6000 + i * 1000}/1/1/0, dlsch_errors 0, pucch0_DTX 1 (SNR 21 dB), "
            f"BLER 0.1 MCS (0) 25 CCE fail 1" for i in range(25))
        self.assertEqual([], keeper.lagging_mcs_ues(ramp + "\n" + rows("7675", 26, 1000)))

    def test_no_loaded_peer_no_verdict(self):
        self.assertEqual([], keeper.lagging_mcs_ues(rows("9833", 8, 1000)))
        self.assertEqual([], keeper.lagging_mcs_ues(rows("7675", 25, 1000) + "\n" + rows("9833", 20, 1000)))


if __name__ == "__main__":
    unittest.main()


class SelfHistoryLowMcs(unittest.TestCase):
    """2026-09-27: after hand-backs ue3 ran whole contexts at MCS 10-12 while its other contexts on
    gnb1 ran 18-23 (one at 28); judge a context against the median of the host's recent ones."""
    host = {"a": "ue3", "b": "ue3", "c": "ue3", "d": "ue3", "e": "ue3"}.get

    def test_a_context_far_below_the_hosts_median_is_named(self):
        best = {}
        for r, m, t in (("a", 21, 1.0), ("b", 28, 2.0), ("c", 19, 3.0)):
            self.assertEqual({}, keeper.self_lagging_mcs("gnb1", {r: m}, self.host, best, t))
        self.assertEqual({"d": 11}, keeper.self_lagging_mcs("gnb1", {"d": 11}, self.host, best, 4.0))
        self.assertEqual({}, keeper.self_lagging_mcs("gnb1", {"e": 18}, self.host, best, 5.0))
        self.assertNotIn("d", best["ue3@gnb1"])
        self.assertEqual({}, keeper.self_lagging_mcs("gnb2", {"d": 11}, self.host, best, 6.0))

    def test_one_other_context_or_old_ones_judge_nothing(self):
        best = {"ue3@gnb1": {"a": [23, 0.0], "b": [22, 0.0]}}
        later = keeper.LOW_MCS_BEST_MAX_AGE_S + 1
        self.assertEqual({}, keeper.self_lagging_mcs("gnb1", {"d": 11}, self.host, best, later))
        self.assertEqual({}, keeper.self_lagging_mcs("gnb1", {"e": 5}, self.host, {"ue3@gnb1": [23, 0.0]}, 1.0))

    def test_idle_samples_are_not_a_loaded_mcs(self):
        idle = "\n".join(f"UE 1ca0: dlsch_rounds {1000 + i * 100}/1/1/0, dlsch_errors 0, pucch0_DTX 1 "
                         f"(SNR 21 dB), BLER 0.1 MCS (0) 6 CCE fail 1, goodput 0.04 Mbps" for i in range(80))
        self.assertEqual({}, keeper._loaded_mcs(idle))

    def test_a_broken_context_under_load_is_judged_though_its_goodput_is_low(self):
        # 18:4x ue2 b014: MCS 0, BLER 0.6, 0.4 Mbps, ~780 new rounds per stats block
        broken = "\n".join(f"UE b014: dlsch_rounds {1000 + i * 780}/1/1/0, dlsch_errors 0, pucch0_DTX 1 "
                           f"(SNR 21 dB), BLER 0.6 MCS (0) 0 CCE fail 1, goodput 0.40 Mbps" for i in range(8))
        self.assertEqual({"b014": 0}, keeper._loaded_mcs(broken))


class LowMcsVerdictsOnACell(unittest.TestCase):
    def _run(self, at_baseline, loaded, peer, best):
        import json, tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "mcs-best.json"
            f.write_text(json.dumps(best))
            with mock.patch.object(keeper, "_cell_at_baseline", return_value=at_baseline), \
                    mock.patch.object(keeper, "mcs_verdicts", return_value=peer), \
                    mock.patch.object(keeper, "_loaded_mcs", return_value=loaded), \
                    mock.patch.object(keeper, "_host_of_rnti", side_effect={"3bb4": "ue3", "38b1": "ue2"}.get), \
                    mock.patch.object(keeper, "MCS_BEST_FILE", f):
                return keeper.low_mcs_verdicts_on("gnb1", "")

    def test_nothing_is_judged_off_baseline(self):
        self.assertEqual({}, self._run(False, {"3bb4": 5}, {"3bb4": (5, True)}, {}))

    def test_a_lone_healthy_context_is_judged_fine_and_a_low_one_lagging(self):
        best = {"ue3@gnb1": {"x": [23, 1e12], "y": [22, 1e12]}, "ue2@gnb1": {"x": [25, 1e12], "y": [26, 1e12]}}
        self.assertEqual({"3bb4": (22, False), "38b1": (15, True)},
                         self._run(True, {"3bb4": 22, "38b1": 15}, {}, best))
