"""Owner 2026-09-26: a board whose trial 0 already met T0 is expunged (the block reference did
not describe its bed); ledger exclusion notes take attempts out; bed drift is flagged, not dropped."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "experiment_results/ota-20260911/ops/campaign_report.py"


def _mod():
    spec = importlib.util.spec_from_file_location("campaign_report_excl_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Exclusions(unittest.TestCase):
    def test_walkover_is_trial_zero_meeting_t0(self):
        mod = _mod()
        self.assertTrue(mod.walkover({"trials": [{"trialIndex": 0, "success": {"T0": True}}]}))
        self.assertFalse(mod.walkover({"trials": [{"trialIndex": 0, "success": {"T0": False}},
                                                  {"trialIndex": 1, "success": {"T0": True}}]}))

    def test_ledger_notes_remove_attempts(self):
        mod = _mod()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.jsonl"
            rows = [{"campaign": "c", "episodeStarted": True, "attempt": 1, "method": "three-agent", "dir": "-"},
                    {"campaign": "c", "episodeStarted": True, "attempt": 2, "method": "three-agent", "dir": "-"},
                    {"campaign": "c", "note": "block-restart", "attempts": [1]}]
            ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            started, _rows, _missing = mod.load("c", str(ledger))
        self.assertEqual([2], [r["attempt"] for r in started])
        self.assertEqual([(1, "block-restart")], [(r["attempt"], why) for r, why in mod.EXCLUSIONS])


if __name__ == "__main__":
    unittest.main()
