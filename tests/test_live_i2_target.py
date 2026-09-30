#!/usr/bin/env python3
"""LIVE-only I2 throughput target (config.LIVE_I2_TARGET_MBPS = 3.5 Mbps).

WHY this exists. On the real testbed (gNB1, 24 PRB, att_tx 18 dB, DL MCS cap
[0..11], RPi5 + B206mini UEs) the measured per-UE DL ceiling is
4.823 +/- 0.013 Mbps solo (n = 6) and ~3.0 Mbps per UE under 2-UE contention
(docs/ue_stability_and_capacity.md §4). The offline default
IntentConfig.throughput_target_mbps = 8.0 is therefore ABOVE what any UE can
ever deliver: strict satisfaction would collapse to 0 and theta*'s premise
C_worst > C_nego would break. The operator chose 3.5 Mbps/UE - met when the peer
UE is idle (+38 %), violated when the peer saturates the cell (-14 %).

WHAT MUST NOT MOVE. 8.0 is calibrated for the SYNTHETIC/EMULATED paper model
(ceiling config.REFERENCE_CEILING_MBPS = 10.5, shared with
experiments.synthetic's frozen profiles). Changing the dataclass default breaks
that model. So 3.5 must reach the LIVE path ONLY. These tests pin both
directions of that separation, plus the CLI override and the figure baseline.

Offline: no hardware / network / OTA contact (mock only).
"""

import argparse
import shutil
import tempfile
import unittest

import config
from experiments import figures
from experiments.figures import plot_per_ue_kpi, generate_all_figures
from experiments.metrics import (
    IntentConfig, live_intent_config, validated_i2_target,
)
from experiments.runner import (
    ExperimentRunner, MultiLLMComparison, build_arg_parser, _resolve_intent_cfg,
)


_TMP = None                      # scratch output dir (no artifact is inspected)


def setUpModule():
    global _TMP
    _TMP = tempfile.mkdtemp(prefix="aic_i2_target_")


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def _args(**kw):
    """A Namespace with only the fields _resolve_intent_cfg reads."""
    d = dict(mode="synthetic", i2_target_mbps=None)
    d.update(kw)
    return argparse.Namespace(**d)


# --------------------------------------------------------------------------- #
# 1. The constant itself                                                       #
# --------------------------------------------------------------------------- #

class LiveI2ConstantTest(unittest.TestCase):
    def test_live_constant_is_the_measured_operating_point(self):
        self.assertEqual(config.LIVE_I2_TARGET_MBPS, 3.5)

    def test_live_constant_is_strictly_inside_the_measured_envelope(self):
        # It MUST be reachable solo (4.82) and MUST be violated under 2-UE
        # contention (3.0); otherwise the intent is either never satisfiable or
        # never violable and the experiment degenerates.
        solo_ceiling, contended = 4.823, 3.0
        self.assertLess(config.LIVE_I2_TARGET_MBPS, solo_ceiling)
        self.assertGreater(config.LIVE_I2_TARGET_MBPS, contended)
        # and it must avoid the 4.0-4.8 band (margin inside the MCS outer
        # loop's own wander) - see docs/ue_stability_and_capacity.md §5.
        self.assertLess(config.LIVE_I2_TARGET_MBPS, 4.0)

    def test_live_constant_is_independent_of_the_offline_ceiling(self):
        # the offline paper model keeps its own single source of truth
        self.assertEqual(config.REFERENCE_CEILING_MBPS, 10.5)
        self.assertNotEqual(config.LIVE_I2_TARGET_MBPS,
                            config.REFERENCE_CEILING_MBPS)


# --------------------------------------------------------------------------- #
# 2. (b) REGRESSION: the offline default must not move                        #
# --------------------------------------------------------------------------- #

class OfflineDefaultUnchangedTest(unittest.TestCase):
    """The synthetic/emulated paper model still targets 8.0 Mbps."""

    def test_intent_config_dataclass_default_is_still_8(self):
        self.assertEqual(IntentConfig().throughput_target_mbps, 8.0)

    def test_experiment_runner_default_cfg_is_still_8(self):
        self.assertEqual(ExperimentRunner(output_dir=_TMP)
                         .cfg.throughput_target_mbps, 8.0)

    def test_synthetic_mode_resolves_to_no_cfg_override(self):
        # None => ExperimentRunner's `cfg or IntentConfig()` fallback, i.e. the
        # offline path is byte-for-byte what it was before the live wiring.
        self.assertIsNone(_resolve_intent_cfg(_args(mode="synthetic")))

    def test_emulated_mode_resolves_to_no_cfg_override(self):
        self.assertIsNone(_resolve_intent_cfg(_args(mode="emulated")))

    def test_paired_runner_offline_origins_keep_8(self):
        from experiments.paired_runner import PairedBlockRunner, DataOrigin
        for origin in (DataOrigin.SYNTHETIC_HARNESS,
                       DataOrigin.EMULATED_PIPELINE):
            r = PairedBlockRunner(["llm_with_history"], n_blocks=1,
                                  master_seed=1, data_origin=origin)
            self.assertEqual(r.cfg.throughput_target_mbps, 8.0, origin)

    def test_synthetic_generator_still_uses_the_8_mbps_model(self):
        # end-to-end guard: the frozen synthetic profiles are evaluated against
        # 8.0, so a default drift would show up here as changed satisfaction.
        from experiments.synthetic import generate_experiment
        steps, _eps = generate_experiment(["rule_based"], trials=1, seed=7)
        self.assertTrue(steps)
        cfg = IntentConfig()
        self.assertEqual(cfg.throughput_target_mbps, 8.0)
        # a value between the live target and the offline target must be a
        # VIOLATION offline and a SATISFACTION live - the two regimes really
        # are different, which is exactly why they must not share a constant.
        self.assertFalse(cfg.i2_satisfied_ue(4.0))
        self.assertTrue(live_intent_config().i2_satisfied_ue(4.0))


# --------------------------------------------------------------------------- #
# 3. (a) LIVE path uses 3.5                                                    #
# --------------------------------------------------------------------------- #

class LiveIntentConfigTest(unittest.TestCase):
    def test_factory_default_is_the_live_constant(self):
        self.assertEqual(live_intent_config().throughput_target_mbps,
                         config.LIVE_I2_TARGET_MBPS)

    def test_factory_changes_nothing_else(self):
        cfg, default = live_intent_config(), IntentConfig()
        self.assertEqual(cfg.power_bs, default.power_bs)
        self.assertEqual(cfg.power_bound_db, default.power_bound_db)
        self.assertEqual(cfg.throughput_ue_ids, ())

    def test_factory_scopes_ues(self):
        cfg = live_intent_config(throughput_ue_ids=["ue1", "ue2"])
        self.assertEqual(cfg.throughput_ue_ids, ("ue1", "ue2"))

    def test_live_mode_resolves_to_the_live_target(self):
        cfg = _resolve_intent_cfg(_args(mode="live"))
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.throughput_target_mbps, 3.5)

    def test_live_target_satisfaction_matches_the_measurements(self):
        cfg = live_intent_config()
        self.assertTrue(cfg.i2_satisfied_ue(4.823))    # peer idle  -> satisfied
        self.assertFalse(cfg.i2_satisfied_ue(3.0))     # peer loads -> violated

    def test_runner_built_with_live_cfg_reports_the_live_target(self):
        r = ExperimentRunner(output_dir=_TMP,
                             cfg=_resolve_intent_cfg(_args(mode="live")))
        self.assertEqual(r.cfg.throughput_target_mbps, 3.5)
        self.assertIn("3.5", r.generate_report({}))

    def test_multi_llm_comparison_threads_the_cfg(self):
        cfg = _resolve_intent_cfg(_args(mode="live"))
        cmp = MultiLLMComparison(output_dir=_TMP, cfg=cfg)
        self.assertEqual(cmp.cfg.throughput_target_mbps, 3.5)
        self.assertEqual(cmp.runner.cfg.throughput_target_mbps, 3.5)

    def test_paired_runner_live_ota_uses_the_live_target(self):
        from experiments.paired_runner import PairedBlockRunner, DataOrigin
        r = PairedBlockRunner(["llm_with_history"], n_blocks=1, master_seed=1,
                              data_origin=DataOrigin.LIVE_OTA)
        self.assertEqual(r.cfg.throughput_target_mbps, 3.5)
        # the UE scope still comes from the topology, as before
        self.assertEqual(tuple(r.cfg.throughput_ue_ids),
                         tuple(r.topology.contending_ues()))

    def test_paired_runner_explicit_cfg_always_wins(self):
        from experiments.paired_runner import PairedBlockRunner, DataOrigin
        r = PairedBlockRunner(["llm_with_history"], n_blocks=1, master_seed=1,
                              data_origin=DataOrigin.LIVE_OTA,
                              cfg=IntentConfig(throughput_target_mbps=8.0))
        self.assertEqual(r.cfg.throughput_target_mbps, 8.0)


# --------------------------------------------------------------------------- #
# 4. (c) CLI override                                                          #
# --------------------------------------------------------------------------- #

class CliOverrideTest(unittest.TestCase):
    def test_flag_exists_and_defaults_to_none(self):
        self.assertIsNone(build_arg_parser().parse_args([]).i2_target_mbps)

    def test_flag_parses(self):
        a = build_arg_parser().parse_args(["--mode", "live",
                                           "--i2-target-mbps", "2.5"])
        self.assertEqual(a.i2_target_mbps, 2.5)

    def test_override_wins_in_live_mode(self):
        cfg = _resolve_intent_cfg(_args(mode="live", i2_target_mbps=2.5))
        self.assertEqual(cfg.throughput_target_mbps, 2.5)

    def test_override_applies_in_offline_modes_too(self):
        # explicit is explicit: a hardware-swap value must be usable anywhere.
        for mode in ("synthetic", "emulated"):
            cfg = _resolve_intent_cfg(_args(mode=mode, i2_target_mbps=4.5))
            self.assertIsNotNone(cfg, mode)
            self.assertEqual(cfg.throughput_target_mbps, 4.5, mode)

    def test_end_to_end_parse_then_resolve(self):
        args = build_arg_parser().parse_args(["--mode", "live",
                                              "--i2-target-mbps", "4.2"])
        self.assertEqual(_resolve_intent_cfg(args).throughput_target_mbps, 4.2)
        args = build_arg_parser().parse_args(["--mode", "synthetic"])
        self.assertIsNone(_resolve_intent_cfg(args))

    def test_invalid_override_is_rejected_fail_closed(self):
        for bad in (0.0, -1.0, float("nan"), float("inf"), True, "3.5", None):
            if bad is None:
                continue                     # None means "use the default"
            with self.assertRaises(ValueError, msg=repr(bad)):
                validated_i2_target(bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                _resolve_intent_cfg(_args(mode="live", i2_target_mbps=bad))
            with self.assertRaises(ValueError, msg=repr(bad)):
                _resolve_intent_cfg(_args(mode="synthetic",
                                          i2_target_mbps=bad))

    def test_validated_target_accepts_a_real_measurement(self):
        self.assertEqual(validated_i2_target(3.5), 3.5)
        self.assertEqual(validated_i2_target(4), 4.0)


# --------------------------------------------------------------------------- #
# 5. (d) figures draw the CONFIGURED target, never a hardcoded 8.0            #
# --------------------------------------------------------------------------- #

def _per_ue_metrics():
    return {"adaptive": {"per_ue_kpi": {
        "ue1": {"by_phase": {"Nominal": {
            "throughput_mbps": {"mean": 4.8, "std": 0.1}}}}}}}


class _CapturedFigure:
    """Patch figures._save so the Figure survives for inspection."""

    def __init__(self):
        self.figs = []

    def __call__(self, fig, out_dir, name):
        self.figs.append(fig)
        return []

    def hlines(self):
        """(y, label) for every axhline-style full-width line drawn."""
        out = []
        for fig in self.figs:
            for ax in fig.axes:
                for ln in ax.get_lines():
                    ys = list(ln.get_ydata())
                    if len(ys) == 2 and ys[0] == ys[1]:
                        out.append((ys[0], ln.get_label()))
        return out


class FiguresUseConfiguredTargetTest(unittest.TestCase):
    def setUp(self):
        self.cap = _CapturedFigure()
        self._orig_save = figures._save
        figures._save = self.cap

    def tearDown(self):
        figures._save = self._orig_save
        for fig in self.cap.figs:
            figures.plt.close(fig)

    def test_per_ue_kpi_draws_the_live_target(self):
        plot_per_ue_kpi(_per_ue_metrics(), "adaptive", live_intent_config(),
                        output_dir=_TMP)
        self.assertIn((3.5, "I2 target (3.5 Mbps)"), self.cap.hlines())
        self.assertNotIn(8.0, [y for y, _ in self.cap.hlines()])

    def test_per_ue_kpi_defaults_to_the_offline_target(self):
        # no cfg supplied -> the offline 8.0 default, exactly as before.
        plot_per_ue_kpi(_per_ue_metrics(), "adaptive",
                        output_dir=_TMP)
        self.assertIn((8.0, "I2 target (8 Mbps)"), self.cap.hlines())

    def test_per_ue_kpi_honours_an_arbitrary_target(self):
        plot_per_ue_kpi(_per_ue_metrics(), "adaptive",
                        IntentConfig(throughput_target_mbps=2.5),
                        output_dir=_TMP)
        self.assertIn((2.5, "I2 target (2.5 Mbps)"), self.cap.hlines())

    def test_per_ue_kpi_fails_closed_on_an_invalid_target(self):
        # an invalid target must NOT be drawn as a legitimate baseline
        plot_per_ue_kpi(_per_ue_metrics(), "adaptive",
                        IntentConfig(throughput_target_mbps=float("nan")),
                        output_dir=_TMP)
        self.assertEqual(self.cap.hlines(), [])

    def test_generate_all_figures_threads_cfg_into_per_ue_kpi(self):
        # the real call path the runner uses: the live cfg must reach the
        # per-UE figure, not just the timeline.
        from experiments.synthetic import generate_experiment
        from experiments.metrics import compute_multi_method
        cfg = live_intent_config()
        steps, eps = generate_experiment(["adaptive"], trials=1, seed=3,
                                         cfg=cfg)
        metrics = compute_multi_method(steps, eps, cfg)
        generate_all_figures(steps, eps, metrics, cfg=cfg,
                             output_dir=_TMP,
                             headline_method="adaptive")
        ys = [y for y, _ in self.cap.hlines()]
        self.assertIn(3.5, ys)
        self.assertNotIn(8.0, ys)


if __name__ == "__main__":
    unittest.main()
