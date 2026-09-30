"""P10 cleanup batch: proportion-safe CIs, theta* honesty footnote, ECE
binning, unwired-baseline skip, conflict_detector removal, figure/export
settings, metrics.json reproducibility header, synthetic bound coupling.
"""

import json
import tempfile
import unittest
from pathlib import Path

from config import ActionSpaceConfig
from experiments.metrics import (
    EpisodeRecord, IntentConfig, StepRecord, compute_all_metrics,
    compute_multi_method, confidence_interval,
)
from experiments.runner import ExperimentRunner


class Clip01ConfidenceIntervalTest(unittest.TestCase):

    def test_proportion_ci_clipped_to_unit_interval(self):
        rates = [1.0, 1.0, 0.8, 0.9, 0.6]
        raw = confidence_interval(rates)
        clipped = confidence_interval(rates, clip01=True)
        self.assertGreater(raw["high"], 1.0)          # the [.., 1.048] bug
        self.assertLessEqual(clipped["high"], 1.0)
        self.assertGreaterEqual(clipped["low"], 0.0)
        self.assertAlmostEqual(clipped["mean"], raw["mean"])

    def test_satisfaction_ci_is_clipped_in_pipeline(self):
        steps = []
        for tid in (1, 2, 3):
            steps.append(StepRecord(
                trial_id=tid, method="m", phase="Nominal", step_idx=0,
                ue_kpis={"ue1": {"throughput_mbps": 9.0}},
                action_offsets={"bs2": 0.0}))
        res = compute_all_metrics(steps, [], IntentConfig())
        ci = res["dual_intent_satisfaction"]["ci95"]
        self.assertLessEqual(ci["high"], 1.0)
        self.assertGreaterEqual(ci["low"], 0.0)


class EceBinningTest(unittest.TestCase):

    def test_pipeline_defaults_to_five_bins(self):
        ep = EpisodeRecord(trial_id=0, method="m", phase="P1",
                           confidence=0.9, trial_executed=True,
                           trial_success=True)
        res = compute_all_metrics([], [ep], IntentConfig())
        self.assertEqual(res["ece"]["n_bins"], 5)
        self.assertEqual(res["ece"]["n"], 1)
        res10 = compute_all_metrics([], [ep], IntentConfig(), ece_bins=10)
        self.assertEqual(res10["ece"]["n_bins"], 10)


class ThetaFootnoteTest(unittest.TestCase):

    def _metrics_with_assumption_violated(self):
        # C_nego >> C_worst: successes only (C_cont falls to prior 125) with
        # huge negotiation deficits -> assumption_ok False
        eps = [EpisodeRecord(trial_id=i, method="m", phase="P1",
                             trial_executed=True, trial_success=True,
                             throughput_before=5.0, throughput_after=6.0,
                             tau_trial=15.0) for i in range(3)]
        eps.append(EpisodeRecord(trial_id=9, method="m", phase="P1",
                                 entered_negotiation=True,
                                 throughput_during_nego=0.0,
                                 nego_duration=100.0))
        steps = [StepRecord(trial_id=1, method="m", phase="Nominal",
                            step_idx=0,
                            ue_kpis={"ue1": {"throughput_mbps": 9.0}},
                            action_offsets={"bs2": 0.0})]
        return compute_multi_method(steps, eps, IntentConfig())

    def test_report_marks_dagger_and_footnote(self):
        metrics = self._metrics_with_assumption_violated()
        self.assertFalse(metrics["m"]["cost_estimate"]["assumption_ok"])
        with tempfile.TemporaryDirectory() as tmp:
            r = ExperimentRunner(coordinator=None, output_dir=tmp)
            report = r.generate_report(metrics)
        self.assertIn("†", report)
        self.assertIn("premise C_worst > C_nego violated", report)
        self.assertIn("clamped", report)
        self.assertIn("ECE n=", report)


class UnwiredBaselineTest(unittest.TestCase):

    def test_configure_method_fails_closed_on_unwired_baseline(self):
        # Batch G (P0-20): a baseline whose wired proposer backend
        # ('baseline:<method>') cannot be selected is now a HARD error - never a
        # silent skip or a coordinator-default mislabel, even when skip_unwired
        # is requested. A non-baseline LLM method still configures normally.
        with tempfile.TemporaryDirectory() as tmp:
            r = ExperimentRunner(coordinator=object(), output_dir=tmp)
            with self.assertRaises(RuntimeError):
                r._configure_method("rule_based")
            r.skip_unwired = True
            with self.assertRaises(RuntimeError):
                r._configure_method("rule_based")
            self.assertTrue(r._configure_method("llm_with_history"))

    def test_run_live_fails_closed_on_unwired_methods(self):
        # Batch G (P0-20): a requested baseline missing its wired backend must
        # FAIL the run (fail closed), not be silently dropped from the
        # comparison - so run_live propagates the hard error.
        import types

        class _Coord:
            def set_probe_config(self, cfg):   # Batch F mandatory install
                pass

            def _active_model_id(self):
                return "llm-x"                  # a valid (non-baseline) entry LLM

            def set_llm_backend(self, name):
                return True                     # no-op: does NOT wire a baseline

        with tempfile.TemporaryDirectory() as tmp:
            r = ExperimentRunner(coordinator=_Coord(), output_dir=tmp)
            # emulated channel so the P0-19 environment preflight passes and the
            # run reaches the P0-20 baseline-wiring check.
            r.channel_model = types.SimpleNamespace(
                set_environment=lambda *a, **k: None)
            r.skip_unwired = True
            r._run_live_trial = lambda m, t: ([], [])
            with self.assertRaises(RuntimeError):
                r.run_live(["rule_based", "rl_controller"], trials=1)


class ConflictDetectorRemovedTest(unittest.TestCase):

    def test_module_gone_and_package_importable(self):
        import decision   # package import still works
        self.assertFalse(hasattr(decision, "ConflictDetector"))
        with self.assertRaises(ImportError):
            from decision.conflict_detector import ConflictDetector  # noqa


class FigureSettingsTest(unittest.TestCase):

    def test_fonttype_and_dpi(self):
        import matplotlib
        from experiments import figures
        self.assertEqual(matplotlib.rcParams["pdf.fonttype"], 42)
        self.assertEqual(matplotlib.rcParams["ps.fonttype"], 42)
        self.assertEqual(figures.SAVE_DPI, 300)

    def test_single_column_rescales_saved_figure(self):
        import matplotlib.pyplot as plt
        from experiments import figures
        with tempfile.TemporaryDirectory() as tmp:
            figures.set_column("single")
            try:
                fig, _ = plt.subplots(figsize=(10, 4))
                figures._save(fig, Path(tmp), "t")
                self.assertAlmostEqual(fig.get_size_inches()[0], 3.5,
                                       places=3)
                self.assertAlmostEqual(fig.get_size_inches()[1], 1.4,
                                       places=3)
            finally:
                figures.set_column("double")


class MetricsMetaTest(unittest.TestCase):

    def test_run_records_reproducibility_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = ExperimentRunner(coordinator=None, output_dir=tmp)
            r.theta_mode = "fixed"
            res = r.run(mode="synthetic", methods=["llm_with_history"],
                        trials=1, make_figures=False, seed=4242)
            data = json.loads(Path(res["metrics_path"]).read_text())
        meta = data["_meta"]
        self.assertEqual(meta["seed"], 4242)
        self.assertEqual(meta["mode"], "synthetic")
        self.assertEqual(meta["theta_mode"], "fixed")
        self.assertIn("git_hash", meta)
        self.assertEqual(meta["config"]["c_episode"], 500.0)
        self.assertIn("intent_config", meta["config"])
        self.assertIn("llm_with_history", data)   # methods still top-level


class MultiLLMComparisonParityTest(unittest.TestCase):
    """The comparison path gets the same P10 treatments as the method path."""

    def test_render_table_dagger_with_clamped_value(self):
        from experiments.runner import MultiLLMComparison
        with tempfile.TemporaryDirectory() as tmp:
            cmp = MultiLLMComparison(output_dir=tmp)
            table = [{"model": "claude-sonnet", "dual_satisfaction_pct": 80.0,
                      "rollback_pct": 10.0, "negotiation_pct": 5.0,
                      "ece": 0.1, "avg_inference_ms": 900.0,
                      "theta_star": -0.25, "n_max": 3,
                      "assumption_ok": False, "is_headline": True}]
            txt = cmp._render_table(table)
        self.assertIn("†", txt)
        self.assertIn("claude-sonnet=0.100", txt)      # clamped value shown
        self.assertIn("premise C_worst > C_nego violated", txt)

    def test_comparison_json_carries_meta_and_column(self):
        from unittest import mock
        from experiments import figures
        from experiments.runner import MultiLLMComparison
        captured = {}

        def fake_figs(*a, **kw):
            captured.update(kw)
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            cmp = MultiLLMComparison(output_dir=tmp)
            cmp.runner.figures_column = "single"
            meta = cmp.runner._run_meta("synthetic", 777)
            with mock.patch.object(figures, "generate_all_figures",
                                   side_effect=fake_figs):
                res = cmp.run(models=["claude-sonnet"], trials=1,
                              mode="synthetic", make_figures=True, meta=meta)
            data = json.loads(Path(res["path"]).read_text())
        self.assertEqual(captured.get("column"), "single")
        self.assertEqual(data["_meta"]["seed"], 777)
        self.assertEqual(data["_meta"]["mode"], "synthetic")
        self.assertIn("table", data)


class SyntheticBoundCouplingTest(unittest.TestCase):

    def test_exec_bound_references_action_space_config(self):
        from experiments import synthetic
        self.assertEqual(synthetic.EXEC_BOUND_DB,
                         ActionSpaceConfig().power_offset_max_db)


if __name__ == "__main__":
    unittest.main()
