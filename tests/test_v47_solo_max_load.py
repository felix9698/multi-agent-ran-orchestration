"""v4.7 solo-max send-rate rule (owner 2026-09-25): L = mean of ue2's and ue3's solo maximum,
measured at saturation; unusable windows refuse rather than guess."""
import importlib.util
import unittest
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "experiment_results/ota-20260911/ops/reference_dl.py"
_spec = importlib.util.spec_from_file_location("reference_dl_t", _PATH)
ref = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ref)


class SoloMaxLoad(unittest.TestCase):
    def test_mean_of_the_two_solo_maxima(self):
        sat = {"B": {"ue2": [11.7, 11.8, 11.8, 11.9, 11.8]}, "C": {"ue3": [12.7, 12.8, 12.8, 12.8, 12.9]}}
        self.assertEqual(ref.solo_max_load(sat), 12.3)

    def test_incomplete_or_dead_windows_refuse(self):
        self.assertIsNone(ref.solo_max_load({"B": {"ue2": [11.8] * 4}, "C": {"ue3": [12.8] * 5}}))
        self.assertIsNone(ref.solo_max_load({"B": {"ue2": [11.8, 0, 11.8, 11.8, 11.8]}, "C": {"ue3": [12.8] * 5}}))

    def test_configs_follow_the_rate(self):
        self.assertEqual(ref.configs(12.3)["A"], {"ue1": 12.3, "ue2": 12.3, "ue3": 12.3})
        self.assertEqual(ref.configs(12.3)["B"]["ue3"], ref.KEEP)


if __name__ == "__main__":
    unittest.main()
