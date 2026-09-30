"""Gate B [A4]: the Coordinator console's CLI options must reach the runtime.

Codex finding A4: --calibration-mode was parsed but never applied (the
calibrator silently stayed "online"), and --experiment printed the
pre-P-series metric keys (total_samples / overall_dual_satisfaction /
by_method), showing zeros/empty summaries for completed runs.

These options moved with the runtime they configure.  The B-01 cutover took
them out of ``main.py`` -- which now composes the Cockpit over the Assurance
Kernel and constructs no coordinator at all -- and into
``tools.legacy.coordinator_console``, behind the operator gate that was already
on the legacy window.  The A4 property is unchanged and is asserted there, on
the same options, so the finding cannot regress by being relocated.
"""

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from tools.legacy import coordinator_console as main_mod


class CalibrationModeWiringTest(unittest.TestCase):

    def _run(self, argv):
        fake_coord = mock.Mock()
        with mock.patch.object(main_mod, "IntentCoordinator",
                               return_value=fake_coord), \
             mock.patch.object(main_mod, "run_cli") as run_cli, \
             mock.patch.dict(os.environ,
                             {main_mod.LEGACY_CONSOLE_ENV: "1"}), \
             mock.patch.object(sys, "argv", argv):
            main_mod.main()
        run_cli.assert_called_once()
        return fake_coord

    def test_the_gate_refuses_before_anything_is_constructed(self):
        """No approval, no coordinator: exit 2 and nothing built."""
        with mock.patch.object(main_mod, "IntentCoordinator") as ctor, \
             mock.patch.dict(os.environ, {main_mod.LEGACY_CONSOLE_ENV: ""}), \
             mock.patch.object(sys, "argv", ["console", "--no-gui"]), \
             contextlib.redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(2, main_mod.main())
        ctor.assert_not_called()
        self.assertIn(main_mod.LEGACY_CONSOLE_ENV, buf.getvalue())

    def test_calibration_mode_applied_to_runtime_calibrator(self):
        coord = self._run(["console", "--no-gui",
                           "--calibration-mode", "fixed"])
        self.assertEqual(coord.calibrator.mode, "fixed")

    def test_default_calibration_mode_is_online(self):
        coord = self._run(["console", "--no-gui"])
        self.assertEqual(coord.calibrator.mode, "online")


class ExperimentSummaryTest(unittest.TestCase):

    def test_experiment_prints_current_report(self):
        fake_runner = mock.Mock()
        fake_runner.run.return_value = {"report": "REPORT_BODY",
                                        "n_steps": 3, "n_episodes": 2,
                                        "metrics_path": "m.json"}
        buf = io.StringIO()
        with mock.patch("experiments.runner.ExperimentRunner",
                        return_value=fake_runner), \
             contextlib.redirect_stdout(buf):
            main_mod.run_experiment(None, trials=2)
        out = buf.getvalue()
        self.assertIn("REPORT_BODY", out)
        self.assertIn("3 step records", out)
        self.assertNotIn("total_samples", out)   # old keys are gone
        fake_runner.run.assert_called_once_with(mode="synthetic", trials=2,
                                                make_figures=False)


if __name__ == "__main__":
    unittest.main()
