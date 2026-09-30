"""Smoke tests for the --compare-llms CLI path (P1).

Regression: `--compare-llms --mode live` constructed MultiLLMComparison
without a coordinator, so _run_live raised RuntimeError immediately.
"""

import sys
import tempfile
import unittest
from unittest import mock

from experiments import runner as runner_mod


class _FakeCoordinator:
    """Minimal coordinator stand-in for the compare path."""

    def __init__(self):
        self.calls = []

    def set_llm_backend(self, model):
        self.calls.append(("set_llm_backend", model))
        return True

    def start(self):
        self.calls.append(("start", None))

    def stop(self):
        self.calls.append(("stop", None))


class RunnerFailClosedTest(unittest.TestCase):
    """Gate A [C4]: runner-side record generation must be fail-closed too."""

    def _runner(self, offsets):
        class _Ex:
            def get_all_offsets(self):
                return offsets

        class _Coord:
            executor = _Ex()

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return runner_mod.ExperimentRunner(coordinator=_Coord(),
                                           output_dir=tmp.name)

    def test_missing_gnb_offset_omitted_not_zeroed(self):
        # A gNB with no observable offset must be OMITTED from the record
        # (i1_satisfied then fails closed), never defaulted to 0.0 dB.
        r = self._runner({"gnb1": 1.0})
        self.assertEqual(r._read_action_offsets(), {"bs1": 1.0})
        r2 = self._runner({})
        self.assertEqual(r2._read_action_offsets(), {})
        r3 = self._runner({"gnb1": 1.0, "gnb2": 0.0})
        self.assertEqual(r3._read_action_offsets(), {"bs1": 1.0, "bs2": 0.0})

    def test_clip_events_record_their_actual_axis(self):
        # Gate A [H2] counter-example: every clip used to be labeled
        # '<gnb>_power' - PRB/MCS/scheduling clips must carry their axis.
        label = runner_mod.ExperimentRunner._clip_axis_label
        self.assertEqual(label({"gnb_id": "gnb1", "axis": "power_offset"}),
                         "gnb1_power_offset")
        self.assertEqual(label({"gnb_id": "gnb2", "axis": "prb"}),
                         "gnb2_prb")
        self.assertEqual(label({"gnb_id": "gnb1", "axis": "mcs_offset"}),
                         "gnb1_mcs_offset")
        self.assertEqual(label({"gnb_id": "gnb1", "ue_id": "ue1",
                                "axis": "sched_priority"}),
                         "ue1_sched_priority")

    def test_i2_violated_uses_metric_predicate(self):
        # NaN / missing observations must count as violations (they are
        # "not satisfied" in the metric layer - one source of truth).
        r = self._runner({})
        self.assertTrue(r._i2_violated(
            {"ue1": {"throughput_mbps": float("nan")}}))
        self.assertTrue(r._i2_violated({"ue1": {}}))
        self.assertTrue(r._i2_violated({"ue1": {"throughput_mbps": 5.0}}))
        self.assertFalse(r._i2_violated({"ue1": {"throughput_mbps": 9.0}}))
        # missing configured UE is a violation
        r.cfg = runner_mod.IntentConfig(throughput_ue_ids=("ue1", "ue2"))
        self.assertTrue(r._i2_violated({"ue1": {"throughput_mbps": 9.0}}))


class UnwiredBaselineDefaultTest(unittest.TestCase):
    """Gate B [A1]: unwired baselines (rule_based / score_heuristic /
    rl_controller) are SKIPPED by default in live/emulated runs - their
    records would reflect the coordinator's default behavior, not the named
    baseline; --include-unwired is the explicit opt-in."""

    def _run_main(self, argv_extra):
        coord = _FakeCoordinator()
        captured = {}
        fake_res = {"metrics": {}, "figures": {}, "report": "",
                    "records_base": "", "metrics_path": "",
                    "n_steps": 0, "n_episodes": 0}

        def fake_run(self_runner, **kw):
            captured["skip_unwired"] = self_runner.skip_unwired
            return fake_res

        with tempfile.TemporaryDirectory() as tmp:
            argv = ["runner", "--trials", "1", "--no-figures",
                    "--output", tmp] + argv_extra
            with mock.patch.object(runner_mod.ExperimentRunner, "run",
                                   autospec=True, side_effect=fake_run), \
                 mock.patch.object(runner_mod, "_make_live_coordinator",
                                   return_value=coord), \
                 mock.patch("experiments.emulation."
                            "build_emulated_coordinator",
                            return_value=(coord, None)), \
                 mock.patch.object(sys, "argv", argv):
                runner_mod.main()
        return captured["skip_unwired"]

    def test_emulated_skips_unwired_by_default(self):
        self.assertTrue(self._run_main(["--mode", "emulated"]))

    def test_live_skips_unwired_by_default(self):
        self.assertTrue(self._run_main(["--mode", "live"]))

    def test_synthetic_keeps_all_methods(self):
        self.assertFalse(self._run_main(["--mode", "synthetic"]))

    def test_include_unwired_opts_back_in(self):
        self.assertFalse(self._run_main(["--mode", "emulated",
                                         "--include-unwired"]))


class PaperIntentRegistrationTest(unittest.TestCase):
    """Gate A [C7]: the live/emulated driver registers BOTH paper intents
    as active intents, so the coordinator actually coordinates the pair
    (counter-example: I1 used to exist only as an offline metric)."""

    def _runner_with_manager(self):
        from coordinator.intent_coordinator import IntentManager

        class _Coord:
            intent_manager = IntentManager()

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        r = runner_mod.ExperimentRunner(coordinator=_Coord(),
                                        output_dir=tmp.name)
        return r, _Coord()

    def test_both_intents_registered_and_monitored(self):
        from decision.intent_model import IntentType
        r, coord = self._runner_with_manager()
        r._register_paper_intents(coord)
        monitored = coord.intent_manager.get_monitored()
        types = {i.type for i in monitored}
        self.assertEqual(types, {IntentType.POWER_CONSTRAINT,
                                 IntentType.THROUGHPUT_GOAL})
        i1 = next(i for i in monitored
                  if i.type == IntentType.POWER_CONSTRAINT)
        self.assertEqual(i1.scope.bs_ids, [r.cfg.power_bs])
        self.assertAlmostEqual(i1.target.target_value, r.cfg.power_bound_db)
        i2 = next(i for i in monitored
                  if i.type == IntentType.THROUGHPUT_GOAL)
        self.assertAlmostEqual(i2.target.target_value,
                               r.cfg.throughput_target_mbps)

    def test_reregistration_clears_previous_trial(self):
        r, coord = self._runner_with_manager()
        r._register_paper_intents(coord)
        r._register_paper_intents(coord)   # next (method, trial)
        self.assertEqual(len(coord.intent_manager.get_monitored()), 2)

    def test_scoped_power_constraint_checks_only_that_gnb(self):
        # I1 scoped to bs2: a gnb1 offset outside the bound (e.g. the
        # serving-cell action) must NOT violate it; a gnb2 offset must.
        from coordinator.intent_coordinator import IntentCoordinator
        from decision.intent_model import (
            ConstraintType, Intent, IntentScope, IntentTarget, IntentType)

        c = IntentCoordinator.__new__(IntentCoordinator)

        class _Ex:
            def __init__(self, offsets):
                self._o = offsets

            def get_all_offsets(self):
                return self._o

        i1 = Intent(type=IntentType.POWER_CONSTRAINT,
                    target=IntentTarget(kpi_name="tx_power",
                                        constraint_type=ConstraintType.MAX,
                                        target_value=3.0),
                    scope=IntentScope(bs_ids=["bs2"]))
        c.executor = _Ex({"gnb1": 5.0, "gnb2": 0.0})
        self.assertEqual(c._evaluate_intent_tristate(i1, {}), "satisfied")
        c.executor = _Ex({"gnb1": 0.0, "gnb2": 5.0})
        self.assertEqual(c._evaluate_intent_tristate(i1, {}), "violated")
        # a scoped gNB with no observable offset cannot be verified
        c.executor = _Ex({"gnb1": 0.0})
        self.assertEqual(c._evaluate_intent_tristate(i1, {}), "unknown")


class CompareLLMsLivePathTest(unittest.TestCase):

    def test_run_live_uses_injected_coordinator(self):
        coord = _FakeCoordinator()
        with tempfile.TemporaryDirectory() as tmp:
            cmp = runner_mod.MultiLLMComparison(coordinator=coord, output_dir=tmp)
            with mock.patch.object(cmp.runner, "run_live",
                                   return_value=([], [])) as run_live:
                res = cmp.run(models=[runner_mod.HEADLINE_BACKEND], trials=1,
                              mode="live", make_figures=False)
        self.assertIn(("set_llm_backend", runner_mod.HEADLINE_BACKEND),
                      coord.calls)
        run_live.assert_called_once_with([runner_mod.HEADLINE_BACKEND], 1)
        self.assertIn("table", res)

    def test_main_compare_live_injects_and_stops_coordinator(self):
        coord = _FakeCoordinator()
        fake_res = {"table": [], "metrics": {}, "figures": {},
                    "report": "", "path": ""}
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["runner", "--compare-llms", "--mode", "live",
                    "--trials", "1", "--no-figures", "--output", tmp]
            with mock.patch.object(runner_mod, "_make_live_coordinator",
                                   return_value=coord) as mk, \
                 mock.patch.object(runner_mod.MultiLLMComparison, "run",
                                   autospec=True, return_value=fake_res) as run_p, \
                 mock.patch.object(sys, "argv", argv):
                runner_mod.main()
        mk.assert_called_once_with(runner_mod.HEADLINE_BACKEND, "adaptive")
        cmp_instance = run_p.call_args[0][0]
        self.assertIs(cmp_instance.coordinator, coord)
        self.assertIn(("stop", None), coord.calls)

    def test_main_compare_live_stops_coordinator_on_error(self):
        coord = _FakeCoordinator()
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["runner", "--compare-llms", "--mode", "live",
                    "--trials", "1", "--no-figures", "--output", tmp]
            with mock.patch.object(runner_mod, "_make_live_coordinator",
                                   return_value=coord), \
                 mock.patch.object(runner_mod.MultiLLMComparison, "run",
                                   side_effect=RuntimeError("boom")), \
                 mock.patch.object(sys, "argv", argv):
                with self.assertRaises(RuntimeError):
                    runner_mod.main()
        self.assertIn(("stop", None), coord.calls)


if __name__ == "__main__":
    unittest.main()
