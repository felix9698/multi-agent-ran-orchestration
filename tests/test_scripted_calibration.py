"""v4.7 plan step 1: the calibration-only scripted answer for the basic-monolith decision."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import os
from unittest import mock

from tools.liveconsole.agent import _calibrating, scripted_calibration_decision

BASE = {"servingCell@ue3": "12345678", "pfWeight@ue2": "1.0", "dlPrbCap@ue3": "0"}


def _inputs(tried=()):
    return SimpleNamespace(
        baselines=dict(BASE), tried_configurations=tuple(dict(t) for t in tried),
        intents=[], authorization=SimpleNamespace(originals=lambda: {"I2g.r1": 8.0}))


class ScriptedCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.script = Path(tempfile.mkdtemp()) / "script.json"
        self.script.write_text(json.dumps([
            {},                                   # C0
            {"pfWeight@ue2": "4.0"},
            {"servingCell@ue3": "87654321"},
        ]))

    def test_refuses_outside_a_calibration_campaign(self):
        with self.assertRaises(RuntimeError):
            scripted_calibration_decision(_inputs(), str(self.script), "blocks18-v47")

    def test_answers_the_next_untried_configuration(self):
        decision, record = scripted_calibration_decision(
            _inputs(tried=[BASE]), str(self.script), "blocks18-v47cal")
        self.assertEqual(dict(BASE, **{"pfWeight@ue2": "4.0"}), decision.configuration)
        self.assertEqual("scripted-calibration", record.model)
        self.assertEqual({"I2g.r1": 8.0}, decision.requirements)

    def test_an_exhausted_script_stops(self):
        tried = [BASE, dict(BASE, **{"pfWeight@ue2": "4.0"}),
                 dict(BASE, **{"servingCell@ue3": "87654321"})]
        decision, record = scripted_calibration_decision(
            _inputs(tried=tried), str(self.script), "blocks18-v47cal")
        self.assertIsNone(decision)
        self.assertIn("exhausted", record.rationale)


class CalibrationDoesNotStopOnAMetTarget(unittest.TestCase):
    """The review of 2026-09-25: a met T0 must not cut the calibration list short.
    The three early exits (initial T0, trial T0, relaxed stop) are gated on this."""

    def test_the_gate_follows_the_script_variable(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AIC_CALIBRATION_SCRIPT", None)
            self.assertFalse(_calibrating())
        with mock.patch.dict(os.environ, {"AIC_CALIBRATION_SCRIPT": "/x.json",
                                          "AIC_CAMPAIGN": "ho-soak-cal"}):
            self.assertTrue(_calibrating())
        # A leftover script variable in a v4.7 campaign must not switch the exits off.
        with mock.patch.dict(os.environ, {"AIC_CALIBRATION_SCRIPT": "/x.json",
                                          "AIC_CAMPAIGN": "blocks18-v47"}):
            self.assertFalse(_calibrating())

    def test_every_early_exit_is_gated(self):
        src = Path(__file__).resolve().parents[1].joinpath(
            "tools", "liveconsole", "agent.py").read_text(encoding="utf-8")
        self.assertEqual(2, src.count(
            "trial.success.get(self.contract.t0.target_id) and not _calibrating()"))
        self.assertIn("and not _calibrating()):", src)


if __name__ == "__main__":
    unittest.main()
