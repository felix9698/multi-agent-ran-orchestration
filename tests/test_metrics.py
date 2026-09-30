#!/usr/bin/env python3
"""
Offline unit tests for the experiment-harness metric pipeline.

Run from the repo root with either:
    python3 -m unittest discover -s tests -v
    python3 tests/test_metrics.py

NO live RAN, LLM, or network is touched - every metric is checked against
HAND-COMPUTED expected values on tiny fixtures, plus a determinism/sanity pass
over the synthetic generator. This is the Phase-1 acceptance gate: Phase 2 only
swaps real records in for the synthetic ones; the numbers below are computed by
the identical code path.
"""

import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from experiments.metrics import (
    IntentConfig, StepRecord, EpisodeRecord, ClipEvent, CostPriors,
    dual_intent_satisfaction, per_ue_kpi_statistics, rollback_rate,
    negotiation_rate, consecutive_rollback_distribution,
    expected_calibration_error, executor_enforcement_statistics,
    hard_failure_count, estimate_costs, derive_theta_star, derive_n_max,
    confidence_interval, compute_all_metrics, compute_multi_method,
    trajectory_cost, strict_joint_satisfaction,
    component_latency_breakdown, latency_breakdown, latency_linkage_violations,
    regret_decomposition, REFERENCE_UTILITY_MBPS,
)


def step(trial, phase, idx, ue_tputs, bs2=0.0):
    """Helper: build a StepRecord with the given per-UE throughputs."""
    return StepRecord(
        trial_id=trial, method="m", phase=phase, step_idx=idx,
        ue_kpis={ue: {"throughput_mbps": t, "rsrp": -88.0, "sinr": 10.0}
                 for ue, t in ue_tputs.items()},
        action_offsets={"bs1": 0.0, "bs2": bs2},
    )


class TestIntentEvaluation(unittest.TestCase):
    def test_i1_power_bound(self):
        cfg = IntentConfig()  # bs2 +/-3 dB
        self.assertTrue(cfg.i1_satisfied({"bs2": 3.0}))
        self.assertTrue(cfg.i1_satisfied({"bs2": -3.0}))
        self.assertFalse(cfg.i1_satisfied({"bs2": 3.5}))
        self.assertFalse(cfg.i1_satisfied({"bs2": -5.0}))

    def test_i2_throughput_goal(self):
        cfg = IntentConfig()  # >= 8 Mbps
        self.assertTrue(cfg.i2_satisfied_ue(8.0))
        self.assertTrue(cfg.i2_satisfied_ue(12.0))
        self.assertFalse(cfg.i2_satisfied_ue(7.99))
        self.assertFalse(cfg.i2_satisfied_ue(None))   # missing => not satisfied

    def test_step_evaluate_dual(self):
        cfg = IntentConfig()
        ev = step(1, "Nominal", 0, {"ue1": 10, "ue2": 6}, bs2=0.0).evaluate(cfg)
        self.assertTrue(ev["i1"])
        self.assertFalse(ev["i2_all"])               # ue2 below 8
        self.assertTrue(ev["per_ue"]["ue1"]["dual"])
        self.assertFalse(ev["per_ue"]["ue2"]["dual"])


class TestMissingDataFailClosed(unittest.TestCase):
    """Gate A [C4] counter-examples (verification report): a missing
    configured UE or a missing constrained-BS action must never pass dual
    satisfaction - telemetry loss is not satisfaction."""

    def test_report_reproduction_missing_ue_and_bs(self):
        # Report scenario: configured scope (ue1, ue2); only ue1 present at
        # 9 Mbps; bs2 action missing. Pre-fix: dual_all=True. Now: False.
        cfg = IntentConfig(throughput_ue_ids=("ue1", "ue2"))
        rec = StepRecord(
            trial_id=1, method="m", phase="Nominal", step_idx=0,
            ue_kpis={"ue1": {"throughput_mbps": 9.0}},
            action_offsets={})                        # bs2 action missing
        ev = rec.evaluate(cfg)
        self.assertFalse(ev["i1"])                    # missing bs2 -> fail
        self.assertFalse(ev["i2_all"])                # ue2 missing -> fail
        self.assertFalse(ev["dual_all"])
        self.assertTrue(ev["per_ue"]["ue2"]["missing"])
        self.assertFalse(ev["per_ue"]["ue2"]["dual"])

    def test_missing_configured_ue_alone_fails_i2(self):
        cfg = IntentConfig(throughput_ue_ids=("ue1", "ue2"))
        rec = StepRecord(
            trial_id=1, method="m", phase="Nominal", step_idx=0,
            ue_kpis={"ue1": {"throughput_mbps": 9.0}},
            action_offsets={"bs1": 0.0, "bs2": 0.0})
        ev = rec.evaluate(cfg)
        self.assertTrue(ev["i1"])                     # bs2 recorded at 0.0
        self.assertFalse(ev["dual_all"])              # ue2 unverifiable
        self.assertTrue(ev["per_ue"]["ue1"]["dual"])  # present UE unaffected

    def test_missing_bs_action_fails_i1(self):
        cfg = IntentConfig()
        self.assertFalse(cfg.i1_satisfied({}))                 # no record
        self.assertFalse(cfg.i1_satisfied({"bs1": 0.0}))       # wrong BS only
        self.assertTrue(cfg.i1_satisfied({"bs2": 0.0}))        # explicit zero
        self.assertFalse(cfg.i1_satisfied({"bs2": float("nan")}))
        self.assertFalse(cfg.i1_satisfied({"bs2": float("inf")}))

    def test_non_finite_throughput_fails_i2(self):
        # a NaN/Inf "measurement" is not a measurement (fail-closed)
        cfg = IntentConfig()
        self.assertFalse(cfg.i2_satisfied_ue(float("nan")))
        self.assertFalse(cfg.i2_satisfied_ue(float("inf")))

    def test_configured_scope_not_filtered(self):
        cfg = IntentConfig(throughput_ue_ids=("ue1", "ue2"))
        self.assertEqual(cfg.i2_ue_scope(["ue1"]), ["ue1", "ue2"])
        # default (unconfigured) scope still follows the present UEs
        self.assertEqual(IntentConfig().i2_ue_scope(["ue1"]), ["ue1"])

    def test_by_ue_counts_missing_steps_as_failures(self):
        cfg = IntentConfig(throughput_ue_ids=("ue1", "ue2"))
        steps = [
            StepRecord(trial_id=1, method="m", phase="P", step_idx=0,
                       ue_kpis={"ue1": {"throughput_mbps": 9.0},
                                "ue2": {"throughput_mbps": 9.0}},
                       action_offsets={"bs1": 0.0, "bs2": 0.0}),
            StepRecord(trial_id=1, method="m", phase="P", step_idx=1,
                       ue_kpis={"ue1": {"throughput_mbps": 9.0}},   # ue2 lost
                       action_offsets={"bs1": 0.0, "bs2": 0.0}),
        ]
        r = dual_intent_satisfaction(steps, cfg)
        self.assertAlmostEqual(r["by_ue"]["ue1"]["dual_satisfaction_rate"], 1.0)
        # ue2: step0 satisfied, step1 missing -> 1/2, counted over 2 steps
        self.assertAlmostEqual(r["by_ue"]["ue2"]["dual_satisfaction_rate"], 0.5)
        self.assertEqual(r["by_ue"]["ue2"]["n"], 2)
        # exact aggregate cross-check on the same fixture:
        # pairs: step0 2/2 + step1 (ue1 ok, ue2 missing) 1/2 -> 3/4
        self.assertAlmostEqual(
            r["overall"]["dual_satisfaction_rate"], 0.75)
        self.assertEqual(r["overall"]["n_pairs"], 4)
        # strict: step0 True, step1 False -> 1/2 over BOTH steps
        self.assertAlmostEqual(
            r["overall"]["dual_satisfaction_strict"], 0.5)
        self.assertEqual(r["overall"]["n_steps"], 2)

    def test_all_configured_ues_absent_zero_satisfaction(self):
        cfg = IntentConfig(throughput_ue_ids=("ue1", "ue2"))
        steps = [StepRecord(trial_id=1, method="m", phase="P", step_idx=0,
                            ue_kpis={},
                            action_offsets={"bs1": 0.0, "bs2": 0.0})]
        r = dual_intent_satisfaction(steps, cfg)
        self.assertAlmostEqual(r["overall"]["dual_satisfaction_rate"], 0.0)
        self.assertEqual(r["overall"]["n_pairs"], 2)   # both still in scope
        self.assertAlmostEqual(r["overall"]["dual_satisfaction_strict"], 0.0)

    def test_out_of_scope_ue_excluded_everywhere(self):
        # An observed UE outside the configured scope must not appear in any
        # aggregation - by_ue included (it used to emit ue3 with n=0).
        cfg = IntentConfig(throughput_ue_ids=("ue1",))
        steps = [StepRecord(trial_id=1, method="m", phase="P", step_idx=0,
                            ue_kpis={"ue1": {"throughput_mbps": 9.0},
                                     "ue3": {"throughput_mbps": 1.0}},
                            action_offsets={"bs1": 0.0, "bs2": 0.0})]
        r = dual_intent_satisfaction(steps, cfg)
        self.assertNotIn("ue3", r["by_ue"])
        self.assertEqual(r["overall"]["n_pairs"], 1)   # ue1 only
        self.assertAlmostEqual(r["overall"]["dual_satisfaction_rate"], 1.0)

    def test_empty_unconfigured_step_counts_in_strict(self):
        # Unconfigured scope: [satisfied step, telemetry-empty step] must
        # report strict 0.5, not 1.0 - the empty step stays in the
        # denominator (fail-closed).
        cfg = IntentConfig()
        steps = [
            step(1, "P", 0, {"ue1": 9.0}, bs2=0.0),
            StepRecord(trial_id=1, method="m", phase="P", step_idx=1,
                       ue_kpis={}, action_offsets={"bs1": 0.0, "bs2": 0.0}),
        ]
        r = dual_intent_satisfaction(steps, cfg)
        self.assertAlmostEqual(r["overall"]["dual_satisfaction_strict"], 0.5)
        self.assertEqual(r["overall"]["n_steps"], 2)


class TestDualIntentSatisfaction(unittest.TestCase):
    def setUp(self):
        cfg = IntentConfig()
        self.cfg = cfg
        # 1 trial, 3 steps, 2 UEs
        self.steps = [
            step(1, "Nominal", 0, {"ue1": 10, "ue2": 10}, bs2=0.0),   # both dual
            step(1, "Degradation", 1, {"ue1": 5, "ue2": 10}, bs2=0.0),  # ue1 fail
            step(1, "Impairment", 2, {"ue1": 10, "ue2": 10}, bs2=5.0),  # I1 fail
        ]

    def test_overall_rates(self):
        r = dual_intent_satisfaction(self.steps, self.cfg)
        # pairs: step1 2/2, step2 1/2, step3 0/2 => 3/6 = 0.5
        self.assertAlmostEqual(r["overall"]["dual_satisfaction_rate"], 0.5)
        # strict all-UE: step1 T, step2 F, step3 F => 1/3
        self.assertAlmostEqual(r["overall"]["dual_satisfaction_strict"], 1/3)
        # I1: step1 T, step2 T, step3 F => 2/3
        self.assertAlmostEqual(r["overall"]["i1_satisfaction_rate"], 2/3)

    def test_by_ue(self):
        r = dual_intent_satisfaction(self.steps, self.cfg)
        self.assertAlmostEqual(r["by_ue"]["ue1"]["dual_satisfaction_rate"], 1/3)
        self.assertAlmostEqual(r["by_ue"]["ue2"]["dual_satisfaction_rate"], 2/3)

    def test_by_phase(self):
        r = dual_intent_satisfaction(self.steps, self.cfg)
        self.assertAlmostEqual(r["by_phase"]["Nominal"]["dual_satisfaction_rate"], 1.0)
        self.assertAlmostEqual(r["by_phase"]["Impairment"]["dual_satisfaction_rate"], 0.0)


def _s(trial, idx, tput=10.0, bs2=0.0, t_s=0.0, ue="ue1", ue2=None):
    """A single StepRecord (method 'm') with controllable t_s / step_idx."""
    kpis = {ue: {"throughput_mbps": tput}}
    if ue2 is not None:
        kpis["ue2"] = {"throughput_mbps": ue2}
    return StepRecord(trial_id=trial, method="m", phase="P", step_idx=idx,
                      t_s=t_s, ue_kpis=kpis, action_offsets={"bs2": bs2})


class TestStrictJointSatisfactionPrimary(unittest.TestCase):
    """P1-2: strict joint satisfaction is the PRIMARY metric; the per-UE/step
    average is diagnostic only. Hand-computed counterexamples for every blocker
    (incl. the 4 mid-review fixes: bool reject, malformed-idx fail-closed,
    separated X*step area units, rolled_back-IS-True denominator)."""

    def test_strict_joint_primary_fails_on_one_ue_one_step(self):
        cfg = IntentConfig()          # scope = UEs present at the step
        steps = [                     # one (m,1) window, contiguous idx 0..2
            _s(1, 0, tput=10.0, ue2=10.0),        # both UEs satisfied
            _s(1, 1, tput=10.0, ue2=5.0),         # ue2 below 8 at ONE step
            _s(1, 2, tput=10.0, ue2=10.0),        # satisfied again
        ]
        r = strict_joint_satisfaction(steps, [], cfg)
        # PRIMARY: the window fails because one UE missed at one step, even
        # though the per-(UE,step) AVERAGE is a high 5/6.
        self.assertEqual(r["strict_joint_rate"], 0.0)
        self.assertEqual(r["n_windows"], 1)
        self.assertEqual(r["n_unverifiable_windows"], 0)
        davg = dual_intent_satisfaction(steps, cfg)["overall"][
            "dual_satisfaction_rate"]
        self.assertAlmostEqual(davg, 5 / 6)       # the averaged metric hides it

    def test_primary_consumers_use_strict_not_per_ue_average(self):
        from experiments.runner import ExperimentRunner, MultiLLMComparison
        cfg = IntentConfig()
        steps = [_s(1, 0, tput=10.0, ue2=10.0),
                 _s(1, 1, tput=10.0, ue2=5.0),     # strict fails, dual avg high
                 _s(1, 2, tput=10.0, ue2=10.0)]
        res = compute_all_metrics(steps, [], cfg)
        self.assertEqual(res["strict_joint_satisfaction"]["strict_joint_rate"],
                         0.0)
        metrics = {"m": res}
        with tempfile.TemporaryDirectory() as tmp:
            report = ExperimentRunner(coordinator=None,
                                      output_dir=tmp).generate_report(metrics)
            row = MultiLLMComparison(output_dir=tmp)._comparison_table(metrics)[0]
        # report headline is STRICT (0.0%), dual is explicitly demoted
        self.assertIn("strict-sat", report)
        self.assertIn("DIAGNOSTIC SECONDARY", report)
        self.assertIn("0.0%", report)
        # the strict subresult's OWN area / recovery / rollback-verified are
        # actually printed together (not replaced by the legacy rollback count).
        self.assertIn("strict detail:", report)
        self.assertIn("viol-area", report)
        self.assertIn("Mbps*step", report)
        self.assertIn("dB*step", report)
        self.assertIn("rollback-verified", report)
        # comparison table carries strict primary + legacy dual diagnostic
        self.assertEqual(row["strict_joint_pct"], 0.0)
        self.assertGreater(row["dual_satisfaction_pct"], 80.0)

    def test_evaluator_rejects_bool_and_nonfinite(self):
        cfg = IntentConfig()
        # I1: a bool / NaN / inf offset must NOT satisfy (True must not read as
        # 1.0 dB and pass); a real in-bound offset does.
        self.assertFalse(cfg.i1_satisfied({"bs2": True}))
        self.assertFalse(cfg.i1_satisfied({"bs2": float("nan")}))
        self.assertFalse(cfg.i1_satisfied({"bs2": float("inf")}))
        self.assertTrue(cfg.i1_satisfied({"bs2": 0.0}))
        self.assertTrue(cfg.i1_satisfied({"bs2": 2.9}))
        self.assertFalse(cfg.i1_satisfied({"bs2": 3.5}))
        # I2: bool / NaN / None throughput must NOT satisfy; a real value does.
        self.assertFalse(cfg.i2_satisfied_ue(True))
        self.assertFalse(cfg.i2_satisfied_ue(float("nan")))
        self.assertFalse(cfg.i2_satisfied_ue(None))
        self.assertTrue(cfg.i2_satisfied_ue(10.0))
        # step-level: a bool offset fails the whole strict window
        st = StepRecord(trial_id=1, method="m", phase="P", step_idx=0,
                        ue_kpis={"ue1": {"throughput_mbps": 10.0}},
                        action_offsets={"bs2": True})
        self.assertFalse(st.evaluate(cfg)["dual_all"])

    def test_malformed_step_idx_window_is_failclosed_not_inflated(self):
        cfg = IntentConfig()
        steps = [
            _s(1, 0), _s(1, 0),          # (m,1) DUPLICATE idx -> unverifiable
            _s(2, 0), _s(2, 2),          # (m,2) GAP (0,2) -> unverifiable
            _s(3, -1),                   # (m,3) NEGATIVE idx -> unverifiable
            _s(4, True),                 # (m,4) BOOL idx -> unverifiable
            _s(6, 3), _s(6, 4),          # (m,6) LEADING-ABSENT (starts at 3)
            _s(5, 0),                    # (m,5) clean, satisfied -> 1.0
        ]
        r = strict_joint_satisfaction(steps, [], cfg)
        # every step is individually "satisfied": if malformed windows were
        # EXCLUDED they would inflate the rate to 1.0. Instead they are
        # fail-closed 0 and only the clean window scores -> 1/6.
        self.assertEqual(r["n_windows"], 6)
        self.assertEqual(r["n_verifiable_windows"], 1)
        self.assertEqual(r["n_unverifiable_windows"], 5)
        self.assertEqual(r["windows_status"], "partial_invalid")
        self.assertAlmostEqual(r["strict_joint_rate"], 1 / 6)
        # the excluded malformed windows make the violation area a LOWER BOUND
        # (completeness reflects n_unverifiable, not only per-cell unknowns).
        vt = r["violation_area"]["throughput"]
        self.assertEqual(vt["n_unverifiable_windows_excluded"], 5)
        self.assertTrue(vt["is_lower_bound"])

    def test_strict_joint_unknowns_and_no_samples_are_finite_json(self):
        cfg = IntentConfig(throughput_ue_ids=("ue1", "ue2"))
        # unknowns: a bool + a NaN throughput and a MISSING offset are all
        # counted as unknown (never zero), and never satisfy.
        steps = [
            StepRecord(trial_id=1, method="m", phase="P", step_idx=0,
                       ue_kpis={"ue1": {"throughput_mbps": True},
                                "ue2": {"throughput_mbps": float("nan")}},
                       action_offsets={"bs2": 0.0}),
            StepRecord(trial_id=1, method="m", phase="P", step_idx=1,
                       ue_kpis={"ue1": {"throughput_mbps": 10.0},
                                "ue2": {"throughput_mbps": 10.0}},
                       action_offsets={}),          # bs2 offset MISSING
        ]
        r = strict_joint_satisfaction(steps, [], cfg)
        self.assertEqual(r["strict_joint_rate"], 0.0)   # not inflated by unknowns
        va = r["violation_area"]
        self.assertEqual(va["throughput"]["unit"], "Mbps*step")
        self.assertEqual(va["power_i1"]["unit"], "dB*step")
        self.assertEqual(va["throughput"]["n_unknown_cells"], 2)
        self.assertTrue(va["throughput"]["is_lower_bound"])
        self.assertEqual(va["power_i1"]["n_unknown_steps"], 1)
        self.assertTrue(va["power_i1"]["is_lower_bound"])
        json.dumps(r, allow_nan=False)          # finite JSON, no NaN/Infinity
        # no-sample: every rate is explicit None + status, never NaN
        empty = strict_joint_satisfaction([], [], cfg)
        self.assertIsNone(empty["strict_joint_rate"])
        self.assertEqual(empty["rate_status"], "no_windows")
        self.assertIsNone(empty["ci95"])
        self.assertIsNone(empty["recovery"]["mean_time_to_recovery_s"])
        self.assertIsNone(empty["rollback_verified"]["rate"])
        json.dumps(empty, allow_nan=False)

    def test_recovery_reports_censored_and_elapsed_time(self):
        cfg = IntentConfig(throughput_ue_ids=("ue1",))
        steps = [
            # window A: violate at t=15, recover at t=30 -> elapsed 15 s
            _s(1, 0, tput=10.0, t_s=0.0), _s(1, 1, tput=5.0, t_s=15.0),
            _s(1, 2, tput=10.0, t_s=30.0),
            # window B: violate and NEVER recover -> censored
            _s(2, 0, tput=10.0, t_s=0.0), _s(2, 1, tput=5.0, t_s=15.0),
            # window C: violation start t_s is NaN -> unknown-timing recovery
            _s(3, 0, tput=10.0, t_s=0.0), _s(3, 1, tput=5.0, t_s=float("nan")),
            _s(3, 2, tput=10.0, t_s=45.0),
        ]
        r = strict_joint_satisfaction(steps, [], cfg)
        rec = r["recovery"]
        self.assertEqual(rec["mean_time_to_recovery_s"], 15.0)   # actual t_s
        self.assertEqual(rec["n_recovered"], 1)
        self.assertEqual(rec["n_recovered_unknown_timing"], 1)
        self.assertEqual(rec["n_censored_unrecovered"], 1)
        # a LATER recovery never retroactively makes the full window succeed
        self.assertEqual(r["strict_joint_rate"], 0.0)

    def test_rollback_verified_rate_requires_explicit_true(self):
        cfg = IntentConfig()
        eps = [
            EpisodeRecord(1, "m", "P", trial_executed=True, rolled_back=True,
                          restore_verified=True),      # verified
            EpisodeRecord(2, "m", "P", trial_executed=True, rolled_back=True,
                          restore_verified=False),     # explicit false
            EpisodeRecord(3, "m", "P", trial_executed=True, rolled_back=True,
                          restore_verified=None),      # unknown
            EpisodeRecord(4, "m", "P", trial_executed=False, rolled_back=True,
                          restore_verified=True),      # PARTIAL write, no trial
            EpisodeRecord(5, "m", "P", trial_executed=True, rolled_back=False,
                          restore_verified=True),      # NOT a rollback attempt
        ]
        rb = strict_joint_satisfaction([], eps, cfg)["rollback_verified"]
        # denominator = every rolled_back IS True (incl. the partial write), NOT
        # filtered by trial_executed: 4 attempts, 2 verified -> 0.5.
        self.assertEqual(rb["n_rollbacks"], 4)
        self.assertEqual(rb["n_verified_true"], 2)
        self.assertEqual(rb["n_verified_false"], 1)
        self.assertEqual(rb["n_verified_unknown"], 1)
        self.assertEqual(rb["rate"], 0.5)
        self.assertEqual(rb["status"], "measured")
        # no rollbacks -> explicit None + status, never a fake 0.0
        none_rb = strict_joint_satisfaction([], [eps[4]], cfg)["rollback_verified"]
        self.assertIsNone(none_rb["rate"])
        self.assertEqual(none_rb["status"], "no_rollbacks")


class TestFigureAggregationP15(unittest.TestCase):
    """P1-5: pure figure-aggregation helpers - no I2 violation averaged away,
    cycle-level calibration+threshold history, distinct repeated phases, trial CI."""

    def _srec(self, trial, idx, ues, phase="Nominal", phase_idx=0, method="m"):
        return StepRecord(
            trial_id=trial, method=method, phase=phase, step_idx=idx,
            phase_idx=phase_idx, phase_label=f"p{phase_idx}:{phase}",
            ue_kpis={u: {"throughput_mbps": v} for u, v in ues.items()},
            action_offsets={"bs2": 0.0})

    def test_ue1_high_ue2_low_mean_above_target_still_violates(self):
        from experiments.figures import scoped_throughput_metadata
        cfg = IntentConfig(throughput_target_mbps=8.0,
                           throughput_ue_ids=("ue1", "ue2"))
        # mean (12+5)/2 = 8.5 > target, but ue2=5 < 8 -> STRICT violation visible
        meta = scoped_throughput_metadata(
            [self._srec(1, 0, {"ue1": 12.0, "ue2": 5.0})], cfg)
        self.assertIn(0, meta["violation_steps"]["m"])
        self.assertTrue(meta["any_violation"])

    def test_one_failing_trial_not_hidden_by_another(self):
        from experiments.figures import scoped_throughput_metadata
        cfg = IntentConfig(throughput_target_mbps=8.0, throughput_ue_ids=("ue1",))
        # trial 1 passes (10), trial 2 fails (5) at the SAME step -> violation
        meta = scoped_throughput_metadata(
            [self._srec(1, 0, {"ue1": 10.0}), self._srec(2, 0, {"ue1": 5.0})], cfg)
        self.assertIn(0, meta["violation_steps"]["m"])

    def test_missing_scoped_ue_is_unknown_failclosed_not_zero(self):
        from experiments.figures import scoped_throughput_metadata
        cfg = IntentConfig(throughput_target_mbps=8.0,
                           throughput_ue_ids=("ue1", "ue2"))
        meta = scoped_throughput_metadata(
            [self._srec(1, 0, {"ue1": 10.0})], cfg)     # ue2 missing
        self.assertIn(0, meta["violation_steps"]["m"])   # unknown -> violation
        self.assertGreaterEqual(meta["n_unknown_cells"], 1)
        ue2_vals = [v for (m, pk, t, ue), pts in meta["series"].items()
                    if ue == "ue2" for (_i, v) in pts]
        self.assertTrue(ue2_vals and all(v is None for v in ue2_vals))  # None, not 0

    def test_invalid_target_fails_closed_with_per_method_coverage(self):
        from experiments.figures import scoped_throughput_metadata
        # a NaN target cannot decide satisfaction -> every scoped step violates
        cfg = IntentConfig(throughput_target_mbps=float("nan"),
                           throughput_ue_ids=("ue1",))
        meta = scoped_throughput_metadata(
            [self._srec(1, 0, {"ue1": 100.0}, method="a"),
             self._srec(1, 1, {"ue1": 100.0}, method="a")], cfg)
        self.assertFalse(meta["target_valid"])
        self.assertEqual(meta["violation_steps"]["a"], [0, 1])   # fail-closed
        # a negative target is likewise invalid
        cfg2 = IntentConfig(throughput_target_mbps=-1.0, throughput_ue_ids=("ue1",))
        m2 = scoped_throughput_metadata([self._srec(1, 0, {"ue1": 5.0})], cfg2)
        self.assertFalse(m2["target_valid"])
        self.assertIn(0, m2["violation_steps"]["m"])
        # per-method coverage is exposed (unknown + violation counts)
        cfg3 = IntentConfig(throughput_target_mbps=8.0,
                            throughput_ue_ids=("ue1", "ue2"))
        m3 = scoped_throughput_metadata([self._srec(1, 0, {"ue1": 10.0})], cfg3)
        self.assertEqual(m3["coverage_by_method"]["m"]["n_unknown_cells"], 1)
        self.assertEqual(m3["coverage_by_method"]["m"]["n_violation_steps"], 1)

    def test_two_nominals_are_distinct(self):
        from experiments.figures import phase_group_metadata
        steps = [self._srec(1, 0, {"ue1": 9.0}, phase="Nominal", phase_idx=0),
                 self._srec(1, 4, {"ue1": 9.0}, phase="Nominal", phase_idx=4)]
        pg = phase_group_metadata(steps)
        self.assertEqual(pg["n_phases"], 2)
        self.assertEqual(set(pg["phase_keys"]), {"p0:Nominal", "p4:Nominal"})
        self.assertIn("Nominal", pg["repeated_names_kept_distinct"])
        # the real consumer (per_ue_kpi_statistics) also keeps them distinct
        from experiments.metrics import per_ue_kpi_statistics
        byp = per_ue_kpi_statistics(steps)["ue1"]["by_phase"]
        self.assertIn("p0:Nominal", byp)
        self.assertIn("p4:Nominal", byp)

    def test_phase_key_malformed_and_mismatch(self):
        # a label disagreeing with canonical p{idx}:{name} is NOT trusted
        bad = StepRecord(1, "m", "Nominal", 0, phase_idx=0, phase_label="Nominal#0")
        self.assertTrue(bad.phase_key().startswith("__phase_mismatch__"))
        # malformed phase_idx (bool / negative) -> invalid identity
        self.assertTrue(StepRecord(2, "m", "Nominal", 0, phase_idx=True)
                        .phase_key().startswith("__invalid_phase_idx__"))
        self.assertTrue(StepRecord(3, "m", "Nominal", 0, phase_idx=-1)
                        .phase_key().startswith("__invalid_phase_idx__"))
        # canonical label matches -> canonical key; legacy (neither) -> bare name
        self.assertEqual(StepRecord(4, "m", "Nominal", 0, phase_idx=2,
                                    phase_label="p2:Nominal").phase_key(),
                         "p2:Nominal")
        self.assertEqual(StepRecord(5, "m", "Nominal", 0).phase_key(), "Nominal")

    def test_reliability_uses_cycle_calibrated_and_threshold_history(self):
        from experiments.figures import reliability_metadata
        eps = [EpisodeRecord(1, "m", "Nominal", cycle_idx=0,
                             calibrated_probability=0.6, threshold=0.3,
                             threshold_applied_to="calibrated_probability",
                             trial_executed=True, trial_success=True),
               EpisodeRecord(1, "m", "Nominal", cycle_idx=1,
                             calibrated_probability=0.4, threshold=0.7,
                             threshold_applied_to="calibrated_probability",
                             trial_executed=True, trial_success=False)]
        bm = reliability_metadata(eps)["by_method"]["m"]
        self.assertEqual(bm["distinct_thresholds"], [0.3, 0.7])   # BOTH cycles
        self.assertEqual([c["calibrated_probability"]
                          for c in bm["cycle_history"]], [0.6, 0.4])

    def test_reliability_rejects_raw_only_malformed_and_misapplied_threshold(self):
        from experiments.figures import reliability_metadata
        raw_only = EpisodeRecord(1, "m", "P", calibrated_probability=None,
                                 raw_confidence=0.9, threshold=0.5,
                                 threshold_applied_to="calibrated_probability",
                                 trial_executed=True, trial_success=True)
        mal = EpisodeRecord(2, "m", "P", calibrated_probability=True, threshold=0.5,
                            trial_executed=True, trial_success=True)
        oor = EpisodeRecord(3, "m", "P", calibrated_probability=1.5, threshold=0.5,
                            trial_executed=True, trial_success=True)
        # a valid p but the threshold applies to RAW confidence -> threshold unknown
        misapplied = EpisodeRecord(4, "m", "P", calibrated_probability=0.6,
                                   threshold=0.5,
                                   threshold_applied_to="raw_confidence",
                                   trial_executed=True, trial_success=True)
        bm = reliability_metadata([raw_only, mal, oor, misapplied])["by_method"]["m"]
        self.assertEqual(bm["calibration"], [(0.6, True)])   # raw NEVER substituted
        self.assertEqual(bm["n_unknown_calibrated"], 3)
        # the 3 calibrated-applied thresholds count; the raw-applied one is unknown
        self.assertEqual(bm["distinct_thresholds"], [0.5])
        self.assertEqual(bm["n_unknown_threshold"], 1)       # misapplied excluded

    def test_reliability_exact_outcome_and_stable_ordinal(self):
        from experiments.figures import reliability_metadata
        # a valid p but NO genuine executed outcome -> not a calibration sample
        no_out = EpisodeRecord(1, "m", "P", calibrated_probability=0.6,
                               trial_executed=True, trial_success=None,
                               success=None)
        bm = reliability_metadata([no_out])["by_method"]["m"]
        self.assertEqual(bm["calibration"], [])
        self.assertEqual(bm["n_invalid_outcome"], 1)
        # cross-phase same-trial cycle_idx=0 tie is ordered by phase_idx (not input
        # order): phase_idx 4 then 0 in input must sort to [0, 4].
        e_late = EpisodeRecord(1, "m", "Nominal", phase_idx=4, cycle_idx=0,
                               calibrated_probability=0.9,
                               trial_executed=True, trial_success=True)
        e_early = EpisodeRecord(1, "m", "Nominal", phase_idx=0, cycle_idx=0,
                                calibrated_probability=0.1,
                                trial_executed=True, trial_success=True)
        hist = reliability_metadata([e_late, e_early])["by_method"]["m"]["cycle_history"]
        self.assertEqual([c["calibrated_probability"] for c in hist], [0.1, 0.9])

    def test_reliability_is_per_method_partitioned(self):
        from experiments.figures import reliability_metadata
        eps = [EpisodeRecord(1, "a", "P", calibrated_probability=0.6, threshold=0.3,
                             trial_executed=True, trial_success=True),
               EpisodeRecord(1, "b", "P", calibrated_probability=0.4, threshold=0.7,
                             trial_executed=True, trial_success=False)]
        bm = reliability_metadata(eps)["by_method"]
        self.assertEqual(set(bm), {"a", "b"})            # methods never mixed
        self.assertEqual(bm["a"]["distinct_thresholds"], [0.3])
        self.assertEqual(bm["b"]["distinct_thresholds"], [0.7])

    def test_reliability_synthetic_legacy_outcome_yields_samples(self):
        from experiments.figures import reliability_metadata
        from experiments import synthetic
        _steps, eps = synthetic.generate_experiment(
            list(synthetic.METHOD_PROFILES.keys()))
        bm = reliability_metadata(eps)["by_method"]
        # synthetic uses trial_success=None + success/rolled_back: the EXACT legacy
        # fallback must still yield calibration samples (n>0) for every method.
        self.assertTrue(bm)
        self.assertTrue(all(len(v["calibration"]) > 0 for v in bm.values()))

    def test_trial_ci_is_per_method_not_mixed(self):
        from experiments.figures import trial_satisfaction_ci
        cfg = IntentConfig(throughput_target_mbps=8.0, throughput_ue_ids=("ue1",))
        # two methods REUSE trial_id 1 - must stay SEPARATE per-method trials
        steps = [self._srec(1, 0, {"ue1": 10.0}, method="a"),
                 self._srec(1, 0, {"ue1": 5.0}, method="b")]
        r = trial_satisfaction_ci(steps, cfg)["by_method"]
        self.assertEqual(r["a"]["n_trials"], 1)
        self.assertEqual(r["b"]["n_trials"], 1)
        self.assertEqual(r["a"]["per_trial_rates"], [1.0])   # a passed
        self.assertEqual(r["b"]["per_trial_rates"], [0.0])   # b failed (not merged)

    def test_trial_ci_present_and_across_trials(self):
        from experiments.figures import trial_satisfaction_ci
        cfg = IntentConfig(throughput_target_mbps=8.0, throughput_ue_ids=("ue1",))
        steps = [self._srec(1, 0, {"ue1": 10.0}), self._srec(2, 0, {"ue1": 10.0}),
                 self._srec(3, 0, {"ue1": 5.0})]
        bm = trial_satisfaction_ci(steps, cfg)["by_method"]["m"]
        self.assertEqual(bm["n_trials"], 3)         # trials, not UE×step replicates
        self.assertIsNotNone(bm["ci95"])
        self.assertEqual(bm["ci95"]["n"], 3)        # CI computed across 3 trials

    def test_scoped_finite_violation_and_unknown_are_separate(self):
        from experiments.figures import scoped_throughput_metadata
        cfg = IntentConfig(throughput_target_mbps=8.0,
                           throughput_ue_ids=("ue1", "ue2"))
        # step 0: ue1=5 finite violation; step 1: ue2 missing -> unknown only
        steps = [self._srec(1, 0, {"ue1": 5.0, "ue2": 10.0}),
                 self._srec(1, 1, {"ue1": 10.0})]
        meta = scoped_throughput_metadata(steps, cfg)
        self.assertEqual(meta["finite_violation_steps"]["m"], [0])
        self.assertEqual(meta["unknown_steps"]["m"], [1])   # NOT a measured diamond
        self.assertEqual(meta["violation_steps"]["m"], [0, 1])  # strict union

    def test_phase_label_without_idx_is_invalid(self):
        from experiments.figures import phase_group_metadata
        s = StepRecord(1, "m", "Nominal", 0, phase_label="p0:Nominal")  # no idx
        self.assertTrue(s.phase_key().startswith("__phase_label_without_idx__"))
        pg = phase_group_metadata([s])
        self.assertEqual(len(pg["invalid_phase_keys"]), 1)

    def test_invalid_target_draws_no_numeric_baseline(self):
        import matplotlib
        matplotlib.use("Agg")
        import tempfile
        from experiments.figures import plot_throughput_timeline
        cfg = IntentConfig(throughput_target_mbps=float("nan"),
                           throughput_ue_ids=("ue1",))
        steps = [self._srec(1, 0, {"ue1": 5.0})]
        with tempfile.TemporaryDirectory() as t:
            paths = plot_throughput_timeline(steps, cfg=cfg, output_dir=t)
        self.assertTrue(paths)                 # renders (no crash) with a bad target

    def test_reliability_not_executed_vs_invalid_outcome(self):
        from experiments.figures import reliability_metadata
        # valid p but the trial was NOT executed -> not_executed (NOT invalid)
        not_exec = EpisodeRecord(1, "m", "P", calibrated_probability=0.6,
                                 trial_executed=False)
        bm = reliability_metadata([not_exec])["by_method"]["m"]
        self.assertEqual(bm["n_not_executed"], 1)
        self.assertEqual(bm["n_invalid_outcome"], 0)
        self.assertEqual(bm["calibration"], [])
        # executed=True with NO exact outcome -> invalid (separate bucket)
        bad = EpisodeRecord(2, "m", "P", calibrated_probability=0.6,
                            trial_executed=True, trial_success=None, success=None)
        bm2 = reliability_metadata([bad])["by_method"]["m"]
        self.assertEqual(bm2["n_invalid_outcome"], 1)
        self.assertEqual(bm2["n_not_executed"], 0)

    def test_cycle_history_exposes_ordinal_and_phase_idx(self):
        from experiments.figures import reliability_metadata
        e_late = EpisodeRecord(1, "m", "Nominal", phase_idx=4, cycle_idx=0,
                               calibrated_probability=0.9, trial_executed=True,
                               trial_success=True)
        e_early = EpisodeRecord(1, "m", "Nominal", phase_idx=0, cycle_idx=0,
                                calibrated_probability=0.1, trial_executed=True,
                                trial_success=True)
        hist = reliability_metadata([e_late, e_early])["by_method"]["m"]["cycle_history"]
        # ordered by phase_idx; ordinal + phase_idx exposed for plot verification
        self.assertEqual([c["ordinal"] for c in hist], [0, 1])
        self.assertEqual([c["phase_idx"] for c in hist], [0, 4])


class TestRegretDecompositionP14(unittest.TestCase):
    """P1-4: regret vs a DECLARED FINITE reference (never a global optimum), four
    SEPARATE components summing exactly to total, honest unknown handling."""

    CFG = IntentConfig(throughput_target_mbps=8.0)

    def test_finite_reference_membership_and_boundary(self):
        r = regret_decomposition([], self.CFG)
        ref = r["reference_set"]
        self.assertEqual(ref["utility_mbps_by_phase"]["Nominal"], 10.5)
        self.assertIn("Mbps", ref["unit"])
        self.assertFalse(ref["is_global_optimum"])
        self.assertIn("NOT an unknown global optimum", ref["provenance"])
        # boundary: achieved == reference -> ZERO decision regret, still COUNTED
        at_ref = EpisodeRecord(1, "m", "Nominal", trial_executed=True,
                               trial_success=True, throughput_after=10.5,
                               throughput_before=8.0, throughput_trial_min=8.0,
                               tau_trial=15.0)
        d = regret_decomposition([at_ref], self.CFG)["decision_regret"]
        self.assertEqual(d["value"], 0.0)
        self.assertEqual(d["n_counted"], 1)
        self.assertEqual(d["status"], "measured")
        # achieved ABOVE reference -> clamped to 0 (never negative regret)
        above = EpisodeRecord(2, "m", "Nominal", trial_executed=True,
                              trial_success=True, throughput_after=11.0,
                              throughput_before=8.0, throughput_trial_min=8.0,
                              tau_trial=15.0)
        self.assertEqual(
            regret_decomposition([above], self.CFG)["decision_regret"]["value"],
            0.0)

    def test_negotiation_no_trial_decision_is_counted_without_double_count(self):
        # a NO-TRIAL negotiation decision contributes DECISION regret (req 2) over
        # the DECISION HORIZON using the baseline it left (throughput_before),
        # while NEGOTIATION cost uses the ACTUAL negotiation window + intent
        # target - DISTINCT operand+window, so no double count of one shortfall.
        ep = EpisodeRecord(1, "m", "Nominal", trial_executed=False,
                           entered_negotiation=True, routed_to="negotiation",
                           throughput_before=7.0, tau_trial=15.0,
                           throughput_during_nego=6.0, nego_duration=10.0)
        r = regret_decomposition([ep], self.CFG)
        # decision: (10.5 - 7.0[baseline]) * 15[horizon] = 52.5, counted, NO trial
        self.assertAlmostEqual(r["decision_regret"]["value"], 52.5)
        self.assertEqual(r["decision_regret"]["n_counted"], 1)
        # negotiation cost vs the intent TARGET over the nego window:
        # (8.0 - 6.0) * 10 = 20.0 - a different window (10 != 15) and reference
        self.assertAlmostEqual(r["negotiation_cost"]["value"], 20.0)
        self.assertAlmostEqual(r["total_regret"]["value"], 72.5)

    def test_failed_trial_counts_decision_and_execution_separately(self):
        # a FAILED executed trial contributes BOTH decision regret (opportunity)
        # AND execution cost (the in-window dip) - kept separate.
        ep = EpisodeRecord(1, "m", "Nominal", trial_executed=True,
                           trial_success=False, throughput_after=8.0,
                           throughput_before=8.0, throughput_trial_min=6.0,
                           tau_trial=15.0)
        r = regret_decomposition([ep], self.CFG)
        self.assertAlmostEqual(r["decision_regret"]["value"], 37.5)  # (10.5-8)*15
        self.assertAlmostEqual(r["execution_cost"]["value"], 30.0)   # (8-6)*15
        self.assertAlmostEqual(r["total_regret"]["value"], 67.5)

    def test_each_component_isolated_and_exact_total_sum(self):
        eps = [
            EpisodeRecord(1, "m", "Nominal", trial_executed=True,     # decision
                          trial_success=True, throughput_after=9.0,
                          throughput_before=8.0, throughput_trial_min=8.0,
                          tau_trial=15.0),
            EpisodeRecord(2, "m", "Nominal", trial_executed=True,     # +execution
                          trial_success=False, throughput_after=8.0,
                          throughput_before=8.0, throughput_trial_min=6.0,
                          tau_trial=15.0),
            EpisodeRecord(3, "m", "Nominal", trial_executed=False,    # +negotiation
                          entered_negotiation=True, routed_to="negotiation",
                          throughput_before=7.0, tau_trial=15.0,
                          throughput_during_nego=6.0, nego_duration=10.0),
            EpisodeRecord(4, "m", "Nominal", hard_failure=True,       # +hard
                          throughput_before=8.0, reconnection_time=2.0),
        ]
        r = regret_decomposition(eps, self.CFG)
        d = r["decision_regret"]["value"]
        e = r["execution_cost"]["value"]
        n = r["negotiation_cost"]["value"]
        h = r["hard_failure_cost"]["value"]
        # total is EXACTLY the sum of the four separate components
        self.assertEqual(r["total_regret"]["value"], d + e + n + h)
        self.assertAlmostEqual(h, 16.0)          # 8.0 * 2.0, isolated
        self.assertEqual(r["hard_failure_cost"]["n_counted"], 1)

    def test_unknown_nonfinite_negative_excluded_and_finite_json(self):
        eps = [
            EpisodeRecord(1, "m", "Nominal", trial_executed=True,     # NaN -> unk
                          trial_success=True, throughput_after=float("nan"),
                          throughput_before=8.0, throughput_trial_min=8.0,
                          tau_trial=15.0),
            EpisodeRecord(2, "m", "Nominal", trial_executed=True,     # neg -> unk
                          trial_success=True, throughput_after=-5.0,
                          throughput_before=8.0, throughput_trial_min=8.0,
                          tau_trial=15.0),
            EpisodeRecord(3, "m", "Nominal", trial_executed=True,     # None -> unk
                          trial_success=True, throughput_after=None,
                          throughput_unknown=True, tau_trial=15.0),
            EpisodeRecord(4, "m", "Nominal", trial_executed=True,     # 0.0 valid
                          trial_success=True, throughput_after=0.0,
                          throughput_before=0.0, throughput_trial_min=0.0,
                          tau_trial=15.0),
        ]
        r = regret_decomposition(eps, self.CFG)
        d = r["decision_regret"]
        self.assertEqual(d["n_applicable"], 4)        # 4 trial decisions
        self.assertEqual(d["n_counted"], 1)           # only the measured 0.0
        self.assertEqual(d["n_unknown"], 3)           # NaN/neg/None all excluded
        self.assertTrue(d["is_lower_bound"])          # excluded unknowns -> LB
        self.assertEqual(d["status"], "partial")      # some counted, some unknown
        self.assertAlmostEqual(d["value"], 157.5)     # (10.5 - 0.0) * 15
        # a component with NO applicable events is a TRUE zero (not a lower bound)
        self.assertEqual(r["hard_failure_cost"]["status"], "not_applicable")
        self.assertFalse(r["hard_failure_cost"]["is_lower_bound"])
        self.assertEqual(r["hard_failure_cost"]["value"], 0.0)
        # total exposes lower-bound + completeness
        self.assertTrue(r["total_regret"]["is_lower_bound"])
        self.assertFalse(r["total_regret"]["completeness"]["all_components_complete"])
        json.dumps(r, allow_nan=False)                # finite JSON, no NaN/Inf

    def test_unknown_only_component_is_lower_bound_not_true_zero(self):
        # an APPLICABLE hard failure whose operand is unknown -> value 0 but
        # status unknown_only + is_lower_bound (NOT a fabricated real zero).
        ep = EpisodeRecord(1, "m", "Nominal", hard_failure=True,
                           throughput_before=None, reconnection_time=None)
        h = regret_decomposition([ep], self.CFG)["hard_failure_cost"]
        self.assertEqual(h["n_applicable"], 1)
        self.assertEqual(h["n_counted"], 0)
        self.assertEqual(h["status"], "unknown_only")
        self.assertTrue(h["is_lower_bound"])
        self.assertEqual(h["value"], 0.0)

    def test_event_flags_and_operands_fail_closed(self):
        # a non-True (truthy) trial_executed flag must NOT be coerced into an
        # executed trial - it is EXPOSED as n_invalid, not silently dropped.
        weird = EpisodeRecord(1, "m", "Nominal", throughput_after=5.0,
                              tau_trial=15.0)
        weird.trial_executed = 1        # truthy int, NOT the bool True
        r = regret_decomposition([weird], self.CFG)
        self.assertEqual(r["decision_regret"]["n_applicable"], 0)   # not coerced
        self.assertEqual(r["decision_regret"]["n_invalid"], 1)      # exposed
        self.assertEqual(r["decision_regret"]["status"], "unknown_only")
        self.assertTrue(r["decision_regret"]["is_lower_bound"])
        self.assertEqual(r["execution_cost"]["n_invalid"], 1)
        # a negative negotiation duration fails closed (excluded, not counted)
        negdur = EpisodeRecord(2, "m", "Nominal", entered_negotiation=True,
                               throughput_during_nego=6.0, nego_duration=-3.0)
        rn = regret_decomposition([negdur], self.CFG)
        self.assertEqual(rn["negotiation_cost"]["n_applicable"], 1)
        self.assertEqual(rn["negotiation_cost"]["n_counted"], 0)
        self.assertEqual(rn["negotiation_cost"]["status"], "unknown_only")

    def test_malformed_trial_outcome_is_invalid_not_coerced(self):
        # trial_success is a MALFORMED non-bool: the execution outcome must NOT be
        # bool()-coerced (as trial_outcome() would) - it is EXPOSED as n_invalid.
        ep = EpisodeRecord(1, "m", "Nominal", trial_executed=True,
                           throughput_after=8.0, throughput_before=8.0,
                           throughput_trial_min=6.0, tau_trial=15.0)
        ep.trial_success = "yes"        # malformed, not a genuine bool
        r = regret_decomposition([ep], self.CFG)
        exe = r["execution_cost"]
        self.assertEqual(exe["n_invalid"], 1)     # outcome not classifiable
        self.assertEqual(exe["n_applicable"], 0)
        self.assertEqual(exe["status"], "unknown_only")
        self.assertTrue(exe["is_lower_bound"])
        # decision does not depend on the outcome flag -> still counted honestly
        self.assertEqual(r["decision_regret"]["n_counted"], 1)
        self.assertEqual(r["total_regret"]["completeness"]["n_invalid_total"], 1)

    def test_malformed_unknown_provenance_flag_is_invalid_not_measured(self):
        # throughput_unknown='false' (STRING) is malformed provenance: it must NOT
        # be read as "known" via `is not True` - it is n_invalid, NOT measured.
        bad = EpisodeRecord(1, "m", "Nominal", trial_executed=True,
                            trial_success=True, throughput_after=8.0,
                            throughput_before=8.0, throughput_trial_min=8.0,
                            tau_trial=15.0)
        bad.throughput_unknown = "false"
        d = regret_decomposition([bad], self.CFG)["decision_regret"]
        self.assertEqual(d["n_counted"], 0)
        self.assertEqual(d["n_invalid"], 1)
        self.assertEqual(d["status"], "unknown_only")
        self.assertTrue(d["is_lower_bound"])
        # int 0 (not a genuine bool) is ALSO malformed -> invalid, not measured
        bad_int = EpisodeRecord(2, "m", "Nominal", trial_executed=True,
                                trial_success=True, throughput_after=8.0,
                                tau_trial=15.0)
        bad_int.throughput_unknown = 0
        self.assertEqual(regret_decomposition([bad_int], self.CFG)
                         ["decision_regret"]["n_invalid"], 1)
        # execution path: a FAILED trial with a malformed flag -> exe n_invalid
        exbad = EpisodeRecord(3, "m", "Nominal", trial_executed=True,
                              trial_success=False, throughput_before=8.0,
                              throughput_trial_min=6.0, tau_trial=15.0)
        exbad.throughput_unknown = "false"
        ex = regret_decomposition([exbad], self.CFG)["execution_cost"]
        self.assertEqual(ex["n_invalid"], 1)
        self.assertEqual(ex["n_counted"], 0)
        # negotiation path: nego_throughput_unknown='false' -> neg n_invalid
        negbad = EpisodeRecord(4, "m", "Nominal", entered_negotiation=True,
                               throughput_during_nego=6.0, nego_duration=10.0)
        negbad.nego_throughput_unknown = "false"
        ng = regret_decomposition([negbad], self.CFG)["negotiation_cost"]
        self.assertEqual(ng["n_invalid"], 1)
        self.assertEqual(ng["n_counted"], 0)
        # genuine False + measured 0.0 stays VALID (counted, not excluded)
        ok0 = EpisodeRecord(5, "m", "Nominal", trial_executed=True,
                            trial_success=True, throughput_after=0.0,
                            throughput_before=0.0, throughput_trial_min=0.0,
                            tau_trial=15.0, throughput_unknown=False)
        d0 = regret_decomposition([ok0], self.CFG)["decision_regret"]
        self.assertEqual(d0["n_counted"], 1)
        self.assertAlmostEqual(d0["value"], 157.5)      # (10.5 - 0.0) * 15
        # genuine True -> honest unknown exclusion (n_unknown, NOT n_invalid)
        tru = EpisodeRecord(6, "m", "Nominal", trial_executed=True,
                            trial_success=True, throughput_after=8.0,
                            tau_trial=15.0, throughput_unknown=True)
        dt = regret_decomposition([tru], self.CFG)["decision_regret"]
        self.assertEqual(dt["n_unknown"], 1)
        self.assertEqual(dt["n_invalid"], 0)

    def test_unknown_phase_has_no_fallback_reference(self):
        # a phase OUTSIDE the declared finite reference set has NO fallback: its
        # decision sample is EXCLUDED (n_unknown), never computed against 10.5,
        # so the membership boundary stays meaningful.
        self.assertTrue(regret_decomposition([], self.CFG)["reference_set"]
                        ["no_fallback"])
        ep = EpisodeRecord(1, "m", "UnlistedPhase", trial_executed=True,
                           trial_success=True, throughput_after=1.0,
                           throughput_before=1.0, throughput_trial_min=1.0,
                           tau_trial=15.0)
        d = regret_decomposition([ep], self.CFG)["decision_regret"]
        self.assertEqual(d["n_applicable"], 1)
        self.assertEqual(d["n_counted"], 0)       # no reference -> excluded
        self.assertEqual(d["n_unknown"], 1)
        self.assertEqual(d["value"], 0.0)
        self.assertTrue(d["is_lower_bound"])       # NOT a real zero

    def test_no_global_optimum_and_no_physical_bound_claims(self):
        r = regret_decomposition([], self.CFG)
        self.assertTrue(r["no_global_optimum"])
        self.assertFalse(r["reference_set"]["is_global_optimum"])
        self.assertIn("does NOT imply a physical-loss upper bound",
                      r["reserve_accounting_note"])

    def test_reference_is_single_config_source(self):
        import config
        from experiments import synthetic
        # ONE source: both the metrics reference AND synthetic's ceiling derive
        # from config.REFERENCE_CEILING_MBPS (no separate literal to drift).
        for v in REFERENCE_UTILITY_MBPS.values():
            self.assertIs(v, config.REFERENCE_CEILING_MBPS)
        self.assertIs(synthetic.RECOVER_CEILING_MBPS, config.REFERENCE_CEILING_MBPS)


class TestLatencyInstrumentationP13(unittest.TestCase):
    """P1-3: real per-segment latency, None=unknown (never fake 0 or a total
    split across cycles), finite JSON, honest evidence-id linkage, n/a rendering."""

    def test_total_latency_is_not_split_into_inference(self):
        from experiments.runner import ExperimentRunner
        r = ExperimentRunner(coordinator=None)
        # a 90 ms, 3-cycle episode: the total must NOT become three 30 ms
        # inference samples. Each cycle carries its OWN inference_ms (here None).
        result = {"success": True, "latency_ms": 90.0, "cycles": [
            {"cycle": 0, "trial_success": True, "inference_ms": None,
             "evidence_episode_id": "e"},
            {"cycle": 1, "trial_success": True, "inference_ms": None,
             "evidence_episode_id": "e"},
            {"cycle": 2, "trial_success": True, "inference_ms": None,
             "evidence_episode_id": "e"}]}
        eps = r._episode_from_result("m", 1, "P", result, {}, 8.0)
        self.assertEqual(len(eps), 3)
        self.assertNotIn(30.0, [e.inference_ms for e in eps])   # NO split
        self.assertTrue(all(e.inference_ms is None for e in eps))
        # the real total is attributed ONCE (to cycle 0), None on the rest
        self.assertEqual(eps[0].total_latency_ms, 90.0)
        self.assertIsNone(eps[1].total_latency_ms)
        self.assertIsNone(eps[2].total_latency_ms)

    def test_latency_missing_segment_is_none_not_zero(self):
        eps = [EpisodeRecord(1, "m", "P", inference_ms=12.0)]
        cb = component_latency_breakdown(eps)
        self.assertEqual(cb["inference_ms"]["mean_ms"], 12.0)
        self.assertEqual(cb["inference_ms"]["status"], "measured")
        # a segment that never ran is None + not_instrumented, NEVER a fake 0
        self.assertIsNone(cb["rollback_ms"]["mean_ms"])
        self.assertEqual(cb["rollback_ms"]["status"], "not_instrumented")
        self.assertEqual(cb["rollback_ms"]["n_missing"], 1)

    def test_latency_breakdown_rejects_nonfinite_bool_negative(self):
        eps = [
            EpisodeRecord(1, "m", "P", inference_ms=10.0),        # valid
            EpisodeRecord(2, "m", "P", inference_ms=0.0),         # measured 0 -> valid
            EpisodeRecord(3, "m", "P", inference_ms=float("nan")),   # unknown
            EpisodeRecord(4, "m", "P", inference_ms=float("inf")),   # unknown
            EpisodeRecord(5, "m", "P", inference_ms=-5.0),        # unknown (neg)
            EpisodeRecord(6, "m", "P", inference_ms=True),        # unknown (bool)
        ]
        cb = component_latency_breakdown(eps)
        seg = cb["inference_ms"]
        self.assertEqual(seg["n_measured"], 2)          # 10.0 and 0.0 only
        self.assertEqual(seg["n_unknown"], 4)           # nan/inf/neg/bool
        self.assertAlmostEqual(seg["mean_ms"], 5.0)     # mean(10,0), no bad values
        json.dumps(cb, allow_nan=False)                 # finite JSON, no NaN/Inf
        lb = latency_breakdown(eps)                     # Table I scalar shape
        self.assertAlmostEqual(lb["inference_ms"], 5.0)
        json.dumps(lb, allow_nan=False)

    def test_latency_linkage_uses_real_stage_ids(self):
        # (a) any populated cycle segment WITHOUT the real episode/cycle ids
        # is a violation - even with actuation/proposal ids present.
        no_ids = EpisodeRecord(1, "m", "P", readback_ms=1.0,
                               evidence_actuation_trial_id="a")
        viol = latency_linkage_violations(no_ids)
        self.assertIn("cycle-segment latency without evidence_episode_id", viol)
        self.assertIn("cycle-segment latency without evidence_cycle_id", viol)
        # (c) post-actuation latency WITHOUT the actuation id -> violation
        bad_rb = EpisodeRecord(2, "m", "P", readback_ms=1.0,
                               evidence_episode_id="e", evidence_cycle_id="c",
                               evidence_actuation_trial_id=None)
        self.assertIn("readback_ms populated without evidence_actuation_trial_id",
                      latency_linkage_violations(bad_rb))
        # (b) a generated proposal's inference latency WITHOUT the proposal id
        bad_prop = EpisodeRecord(3, "m", "P", inference_ms=5.0,
                                 proposal_generated=True,
                                 evidence_episode_id="e", evidence_cycle_id="c",
                                 evidence_proposal_id=None)
        self.assertIn("inference_ms populated without evidence_proposal_id",
                      latency_linkage_violations(bad_prop))
        # TIMEOUT/EXCEPTION honesty: inference timing exists but the proposal was
        # NOT generated -> proposal_id None is CORRECT, NOT a violation.
        timeout = EpisodeRecord(4, "m", "P", inference_ms=5.0,
                                proposal_generated=False,
                                evidence_episode_id="e", evidence_cycle_id="c",
                                evidence_proposal_id=None)
        self.assertEqual(latency_linkage_violations(timeout), [])
        # (d) pre-snapshot write FAILURE: executor timer with no applied write
        # may honestly have actuation id None -> NOT a violation.
        pre_snap = EpisodeRecord(5, "m", "P", executor_write_ms=2.0,
                                 applied_action=None,
                                 evidence_episode_id="e", evidence_cycle_id="c",
                                 evidence_actuation_trial_id=None)
        self.assertEqual(latency_linkage_violations(pre_snap), [])
        # a COMPLETED write (applied_action present) WITHOUT actuation id -> viol
        applied_no_id = EpisodeRecord(6, "m", "P", executor_write_ms=2.0,
                                      applied_action={"bs2": {"power_offset": 1.0}},
                                      evidence_episode_id="e",
                                      evidence_cycle_id="c",
                                      evidence_actuation_trial_id=None)
        self.assertIn("executor_write_ms (applied write) without "
                      "evidence_actuation_trial_id",
                      latency_linkage_violations(applied_no_id))
        # fully-linked record -> no violation
        ok = EpisodeRecord(7, "m", "P", inference_ms=5.0, readback_ms=1.0,
                           validation_ms=2.0, executor_write_ms=2.0,
                           proposal_generated=True,
                           applied_action={"bs2": {"power_offset": 1.0}},
                           evidence_episode_id="e", evidence_cycle_id="c",
                           evidence_proposal_id="p",
                           evidence_actuation_trial_id="a")
        self.assertEqual(latency_linkage_violations(ok), [])
        # pre-parse stage: no latency at all -> honest, no violation
        self.assertEqual(latency_linkage_violations(EpisodeRecord(8, "m", "P")), [])
        # a NEGATIVE / NaN latency is UNKNOWN, NOT a populated segment -> it
        # imposes NO id requirement (no false violation for a missing id).
        self.assertEqual(
            latency_linkage_violations(EpisodeRecord(9, "m", "P",
                                                     readback_ms=-5.0)), [])
        self.assertEqual(
            latency_linkage_violations(EpisodeRecord(10, "m", "P",
                                                     readback_ms=float("nan"))), [])
        # the aggregation surfaces linkage coverage over REAL episodes
        cb = component_latency_breakdown([bad_rb, ok])
        self.assertEqual(cb["linkage"]["n_records_with_violations"], 1)
        self.assertEqual(cb["linkage"]["status"], "violations")

    def test_latency_consumers_render_uninstrumented_as_na(self):
        from experiments.runner import MultiLLMComparison
        base = {"model": "m", "strict_joint_pct": 50.0,
                "dual_satisfaction_pct": 80.0, "rollback_pct": 1.0,
                "negotiation_pct": 2.0, "ece": 0.1,
                "theta_star": -0.2, "n_max": 3, "assumption_ok": True,
                "is_headline": False}
        with tempfile.TemporaryDirectory() as tmp:
            cmp = MultiLLMComparison(output_dir=tmp)
            # uninstrumented / NaN / negative -> n/a (never crash, never 0 ms)
            for bad in ("not_instrumented", float("nan"), float("inf"), -3.0):
                txt = cmp._render_table([dict(base, avg_inference_ms=bad)])
                self.assertIn("n/a", txt)
            # a real finite value renders as the actual number (finite path)
            txt_ok = cmp._render_table([dict(base, avg_inference_ms=123.4)])
            self.assertIn("123", txt_ok)
            self.assertNotIn("n/a", txt_ok)

    def test_latency_report_shows_units_and_sample_counts(self):
        from experiments.runner import ExperimentRunner
        eps = [EpisodeRecord(1, "m", "P", inference_ms=12.0,
                             evidence_episode_id="e", evidence_cycle_id="c",
                             evidence_proposal_id="p", proposal_generated=True)]
        res = compute_all_metrics([], eps, IntentConfig())
        with tempfile.TemporaryDirectory() as tmp:
            report = ExperimentRunner(coordinator=None,
                                      output_dir=tmp).generate_report({"m": res})
        # the report surfaces the component breakdown WITH units + sample counts
        self.assertIn("Component latency", report)
        self.assertIn("unit=ms", report)
        self.assertIn("measured=", report)
        self.assertIn("inference_ms", report)


class TestPerUeKpi(unittest.TestCase):
    def test_mean_std(self):
        steps = [
            step(1, "Nominal", 0, {"ue1": 6.0}),
            step(1, "Nominal", 1, {"ue1": 10.0}),
        ]
        r = per_ue_kpi_statistics(steps)
        t = r["ue1"]["overall"]["throughput_mbps"]
        self.assertAlmostEqual(t["mean"], 8.0)
        self.assertAlmostEqual(t["std"], math.sqrt(8.0))  # stdev([6,10]) = 2.828


class TestRollbackNegotiation(unittest.TestCase):
    def test_rollback_rate(self):
        eps = [
            EpisodeRecord(1, "m", "P", trial_executed=True, rolled_back=True),
            EpisodeRecord(1, "m", "P", trial_executed=True, rolled_back=False),
            EpisodeRecord(1, "m", "P", trial_executed=True, rolled_back=True),
            EpisodeRecord(1, "m", "P", trial_executed=False, routed_to="negotiation"),
        ]
        r = rollback_rate(eps)
        self.assertEqual(r["n_trials"], 3)
        self.assertAlmostEqual(r["rate"], 2/3)

    def test_negotiation_rate_counts_only_s2_routing(self):
        eps = [
            EpisodeRecord(1, "m", "P", routed_to="trial", trial_executed=True,
                          rolled_back=True, entered_negotiation=True),  # NOT nego route
            EpisodeRecord(1, "m", "P", routed_to="negotiation",
                          entered_negotiation=True),
            EpisodeRecord(1, "m", "P", routed_to="trial", trial_executed=True),
            EpisodeRecord(1, "m", "P", routed_to="negotiation"),
        ]
        r = negotiation_rate(eps)
        # only routed_to == negotiation counts (2 of 4), NOT the rolled-back one
        self.assertEqual(r["n_negotiations"], 2)
        self.assertAlmostEqual(r["rate"], 0.5)


class TestConsecutiveRollback(unittest.TestCase):
    def test_run_lengths(self):
        pattern = [True, True, False, True, False, False, True, True, True]
        eps = [EpisodeRecord(1, "m", "P", trial_executed=True, rolled_back=rb)
               for rb in pattern]
        r = consecutive_rollback_distribution(eps)
        # runs of consecutive rollbacks: len2, len1, len3
        self.assertEqual(r["histogram"], {1: 1, 2: 1, 3: 1})
        self.assertEqual(r["max_consecutive"], 3)

    def test_nonexecuted_breaks_run(self):
        # a non-trial episode between two rollbacks must break the run
        eps = [
            EpisodeRecord(1, "m", "P", trial_executed=True, rolled_back=True),
            EpisodeRecord(1, "m", "P", trial_executed=False, routed_to="negotiation"),
            EpisodeRecord(1, "m", "P", trial_executed=True, rolled_back=True),
        ]
        r = consecutive_rollback_distribution(eps)
        self.assertEqual(r["histogram"], {1: 2})
        self.assertEqual(r["max_consecutive"], 1)


class TestECE(unittest.TestCase):
    def test_hand_computed_ece(self):
        eps = [
            EpisodeRecord(1, "m", "P", trial_executed=True, confidence=0.9, success=True),
            EpisodeRecord(1, "m", "P", trial_executed=True, confidence=0.9, success=True),
            EpisodeRecord(1, "m", "P", trial_executed=True, confidence=0.9, success=False),
            EpisodeRecord(1, "m", "P", trial_executed=True, confidence=0.3, success=False),
        ]
        r = expected_calibration_error(eps, n_bins=10)
        # bin 9 (0.9): conf=0.9 acc=2/3 gap=0.2333 w=3/4 => 0.175
        # bin 3 (0.3): conf=0.3 acc=0.0 gap=0.3    w=1/4 => 0.075
        self.assertAlmostEqual(r["ece"], 0.25, places=4)

    def test_only_executed_trials_counted(self):
        eps = [
            EpisodeRecord(1, "m", "P", trial_executed=True, confidence=0.9, success=True),
            EpisodeRecord(1, "m", "P", trial_executed=False, confidence=0.1,
                          routed_to="negotiation"),
        ]
        r = expected_calibration_error(eps)
        self.assertEqual(r["n"], 1)


class TestEnforcementStats(unittest.TestCase):
    def test_schema_clip_stats(self):
        eps = [
            EpisodeRecord(1, "m", "P", schema_valid=True, trial_executed=True,
                          clips=[ClipEvent("bs2_power", 5.0, 8.0)]),      # mag 3
            EpisodeRecord(1, "m", "P", schema_valid=True, trial_executed=True,
                          clips=[ClipEvent("bs1_power", 10.0, 8.0),       # mag 2
                                 ClipEvent("bs2_power", 12.0, 8.0)]),     # mag 4
            EpisodeRecord(1, "m", "P", schema_valid=True, trial_executed=True,
                          clips=[]),                                       # applied, no clip
            EpisodeRecord(1, "m", "P", schema_valid=True,
                          trial_executed=False),   # schema-valid but NO applied proposal
            EpisodeRecord(1, "m", "P", schema_valid=False),              # schema reject
        ]
        r = executor_enforcement_statistics(eps)
        self.assertAlmostEqual(r["schema_reject_fraction"], 0.2)
        # P1-1: denominator is ACTUAL APPLIED PROPOSALS (excludes the schema-valid
        # no-proposal episode), not every schema-valid cycle.
        self.assertEqual(r["n_applied_proposals"], 3)
        self.assertAlmostEqual(r["clip_fraction"], 2/3)   # 2 of 3 applied clipped
        self.assertEqual(r["n_clip_events"], 3)
        # P1-1: the unit-mixed fields are REMOVED; per-axis native + normalized.
        self.assertNotIn("mean_clip_magnitude_db", r)
        self.assertNotIn("max_clip_magnitude_db", r)
        self.assertAlmostEqual(
            r["per_axis_native_clip"]["power_offset"]["mean"], 3.0)  # mean(3,2,4)
        self.assertAlmostEqual(
            r["per_axis_native_clip"]["power_offset"]["max"], 4.0)
        self.assertEqual(r["per_axis_native_clip"]["power_offset"]["unit"], "dB")


class TestHardFailure(unittest.TestCase):
    def test_count(self):
        eps = [
            EpisodeRecord(1, "m", "P", trial_executed=True, hard_failure=True),
            EpisodeRecord(1, "m", "P", trial_executed=True, hard_failure=False),
            EpisodeRecord(1, "m", "P", trial_executed=True, hard_failure=False),
            EpisodeRecord(1, "m", "P", trial_executed=True, hard_failure=False),
        ]
        r = hard_failure_count(eps)
        self.assertEqual(r["count"], 1)
        self.assertAlmostEqual(r["p_hard"], 0.25)


class TestCostDerivation(unittest.TestCase):
    """Metric 9 + verify_math: theta* (Eq.8) / N_max (Eq.9) from measured costs."""

    def test_eq8_eq9_closed_form(self):
        # Blind re-derivation check against the paper's closed forms.
        # theta* = (C_worst - C_nego)/(R_success + C_worst)
        self.assertAlmostEqual(derive_theta_star(125.0, 50.0, 64.5),
                               (125.0 - 50.0) / (64.5 + 125.0))
        # N_max (corrected P0-10) = max(0, floor((C_episode - C_trial_ub)
        #                                        / (C_trial_ub + C_nego_ub)))
        self.assertEqual(
            derive_n_max(500.0, 125.0, 50.0),
            max(0, math.floor((500.0 - 125.0) / (125.0 + 50.0))))
        self.assertEqual(derive_n_max(500.0, 125.0, 50.0), 2)
        # C_episode < C_trial_ub -> not even the initial trial is affordable
        self.assertEqual(derive_n_max(100.0, 125.0, 50.0), 0)

    def test_matches_config_formula(self):
        # cross-check the harness derivation equals config.CalibrationConfig.
        # P0-10/P0-11: compute_n_max now uses the DETERMINISTIC caps (corrected
        # Eq. 9), so cross-check derive_n_max over the SAME caps, not the
        # empirical c_worst/c_nego means.
        from config import CalibrationConfig
        c = CalibrationConfig()
        self.assertAlmostEqual(
            derive_theta_star(c.c_worst, c.c_nego, c.r_success),
            c.compute_theta_star())
        self.assertEqual(
            derive_n_max(c.c_episode, c.c_trial_ub(), c.c_nego_ub()),
            c.compute_n_max())
        # the empirical closed form (soft guard) still uses the corrected shape
        self.assertEqual(derive_n_max(500.0, 125.0, 50.0), 2)

    def test_estimate_from_measured_episodes(self):
        cfg = IntentConfig()  # I2 target 8 Mbps
        eps = [
            # 2 successful trials -> R_success = mean((9-5)*15, (8-6)*15) = 45
            EpisodeRecord(1, "m", "P", trial_executed=True, success=True,
                          throughput_before=5.0, throughput_after=9.0, tau_trial=15),
            EpisodeRecord(1, "m", "P", trial_executed=True, success=True,
                          throughput_before=6.0, throughput_after=8.0, tau_trial=15),
            # 2 failed trials -> C_cont = mean((5-1)*15, (4-2)*15) = 45
            EpisodeRecord(1, "m", "P", trial_executed=True, rolled_back=True,
                          throughput_before=5.0, throughput_trial_min=1.0, tau_trial=15),
            EpisodeRecord(1, "m", "P", trial_executed=True, rolled_back=True,
                          throughput_before=4.0, throughput_trial_min=2.0, tau_trial=15,
                          hard_failure=True, reconnection_time=2.0),  # C_hard=4*2=8
            # 2 pure negotiation episodes -> C_nego = mean((8-5)*10, (8-6)*10) = 25
            EpisodeRecord(1, "m", "P", routed_to="negotiation", entered_negotiation=True,
                          throughput_during_nego=5.0, nego_duration=10),
            EpisodeRecord(1, "m", "P", routed_to="negotiation", entered_negotiation=True,
                          throughput_during_nego=6.0, nego_duration=10),
        ]
        c = estimate_costs(eps, cfg, c_episode=500.0)
        self.assertAlmostEqual(c["r_success"], 45.0)
        self.assertAlmostEqual(c["c_cont"], 45.0)
        self.assertAlmostEqual(c["c_hard"], 8.0)
        self.assertAlmostEqual(c["p_hard"], 0.25)      # 1 hard of 4 trials
        self.assertAlmostEqual(c["c_worst"], 45.0 + 0.25 * 8.0)  # 47.0
        self.assertAlmostEqual(c["c_nego"], 25.0)
        # theta* = (47 - 25)/(45 + 47) = 22/92
        self.assertAlmostEqual(c["theta_star"], 22.0 / 92.0)
        # N_max = floor(500/(47+25)) = floor(6.944) = 6
        self.assertEqual(c["n_max"], 6)
        self.assertTrue(c["assumption_ok"])            # C_worst > C_nego
        self.assertTrue(all(c["derived"].values()))    # nothing fell back to a prior

    def test_falls_back_to_priors_when_no_samples(self):
        cfg = IntentConfig()
        c = estimate_costs([], cfg, c_episode=500.0, priors=CostPriors())
        # with no episodes, everything uses priors and is flagged derived=False
        self.assertFalse(c["derived"]["r_success"])
        self.assertAlmostEqual(c["r_success"], CostPriors().r_success)


class TestUnknownBaselineCostSamples(unittest.TestCase):
    """None=unknown regression (reviewer edge case): with throughput_before/after
    now Optional (default None), an UNMEASURED baseline must yield NO cost sample
    and fall back to the prior - never a fabricated 0.0 reference and never a
    None-arithmetic TypeError, EVEN when a trial_trajectory IS present (the
    trajectory-integration path receives deficit_ref=None and must return None
    BEFORE the v-deficit_ref arithmetic)."""

    TRAJ = [(0.0, 5.0), (1.0, 1.0), (2.0, 1.0)]      # >=2 timestamped points

    def test_trajectory_cost_none_reference_returns_none(self):
        # a present, well-formed trajectory with NO reference baseline: honest
        # None (no sample), NOT a TypeError from `v - None`, for BOTH the deficit
        # (c_cont) and gain (r_success) senses.
        self.assertIsNone(trajectory_cost(self.TRAJ, deficit_ref=None))
        self.assertIsNone(trajectory_cost(self.TRAJ, deficit_ref=None, gain=True))
        # a real reference still integrates normally (guard is None-only).
        self.assertIsNotNone(trajectory_cost(self.TRAJ, deficit_ref=10.0))

    def test_success_trajectory_missing_before_falls_back_to_prior(self):
        # R_success operand: gain vs throughput_before. Trajectory present but
        # baseline UNKNOWN -> no sample -> prior; the success is still COUNTED.
        cfg, priors = IntentConfig(), CostPriors()
        ep = EpisodeRecord(0, "m", "P", trial_executed=True, success=True,
                           throughput_before=None, throughput_after=9.0,
                           tau_trial=15, trial_trajectory=self.TRAJ)
        c = estimate_costs([ep], cfg, c_episode=500.0, priors=priors)  # no raise
        self.assertFalse(c["derived"]["r_success"])
        self.assertAlmostEqual(c["r_success"], priors.r_success)
        self.assertEqual(c["samples"]["n_success"], 1)

    def test_success_scalar_missing_after_falls_back_to_prior(self):
        # R_success metric-specific operand: throughput_after. Legacy scalar path
        # (no trajectory) with a KNOWN before but UNKNOWN after -> no sample.
        cfg, priors = IntentConfig(), CostPriors()
        ep = EpisodeRecord(0, "m", "P", trial_executed=True, success=True,
                           throughput_before=5.0, throughput_after=None,
                           tau_trial=15)
        c = estimate_costs([ep], cfg, c_episode=500.0, priors=priors)  # no raise
        self.assertFalse(c["derived"]["r_success"])
        self.assertAlmostEqual(c["r_success"], priors.r_success)

    def test_failed_trajectory_missing_before_falls_back_to_prior(self):
        # C_cont operand: deficit vs throughput_before. Trajectory present but
        # baseline UNKNOWN -> no sample -> prior (C_cont prior is C_worst).
        cfg, priors = IntentConfig(), CostPriors()
        ep = EpisodeRecord(0, "m", "P", trial_executed=True, rolled_back=True,
                           throughput_before=None, throughput_trial_min=1.0,
                           tau_trial=15, trial_trajectory=self.TRAJ)
        c = estimate_costs([ep], cfg, c_episode=500.0, priors=priors)  # no raise
        self.assertFalse(c["derived"]["c_cont"])
        self.assertAlmostEqual(c["c_cont"], priors.c_worst)
        self.assertEqual(c["samples"]["n_failed"], 1)

    def test_failed_scalar_missing_trial_min_falls_back_to_prior(self):
        # C_cont metric-specific operand: throughput_trial_min. Legacy scalar path
        # with a KNOWN before but UNKNOWN trial_min -> no sample.
        cfg, priors = IntentConfig(), CostPriors()
        ep = EpisodeRecord(0, "m", "P", trial_executed=True, rolled_back=True,
                           throughput_before=5.0, throughput_trial_min=None,
                           tau_trial=15)
        c = estimate_costs([ep], cfg, c_episode=500.0, priors=priors)  # no raise
        self.assertFalse(c["derived"]["c_cont"])
        self.assertAlmostEqual(c["c_cont"], priors.c_worst)

    def test_hard_failure_missing_before_excluded_from_c_hard(self):
        # C_hard operand: throughput_before * reconnection_time. A hard failure
        # with a MEASURED reconnection time but UNKNOWN baseline contributes NO
        # C_hard sample (prior fallback) - the hard EVENT is still counted in
        # p_hard and n_hard.
        cfg, priors = IntentConfig(), CostPriors()
        ep = EpisodeRecord(0, "m", "P", trial_executed=True, rolled_back=True,
                           throughput_before=None, throughput_trial_min=None,
                           tau_trial=15, hard_failure=True, reconnection_time=2.0)
        c = estimate_costs([ep], cfg, c_episode=500.0, priors=priors)  # no raise
        self.assertFalse(c["derived"]["c_hard"])
        self.assertAlmostEqual(c["c_hard"], priors.c_hard)
        self.assertEqual(c["samples"]["n_hard"], 1)             # event counted
        self.assertEqual(c["samples"]["n_hard_measured"], 0)    # no cost sample
        self.assertGreater(c["p_hard"], 0.0)                    # p_hard still fires


class TestConfidenceInterval(unittest.TestCase):
    def test_t_interval(self):
        r = confidence_interval([0.8, 0.9, 1.0])
        self.assertAlmostEqual(r["mean"], 0.9)
        self.assertAlmostEqual(r["std"], 0.1)
        # t95(df=2)=4.303; hw = 4.303 * 0.1 / sqrt(3)
        self.assertAlmostEqual(r["half_width"], 4.303 * 0.1 / math.sqrt(3), places=4)

    def test_single_sample(self):
        r = confidence_interval([0.7])
        self.assertEqual(r["n"], 1)
        self.assertAlmostEqual(r["half_width"], 0.0)

    def test_empty(self):
        r = confidence_interval([])
        self.assertEqual(r["n"], 0)


class TestSyntheticPipeline(unittest.TestCase):
    """End-to-end offline: generator -> metrics, determinism + sanity ordering."""

    def _run(self):
        from experiments.synthetic import generate_experiment, METHOD_PROFILES
        methods = list(METHOD_PROFILES.keys())
        # Fixed 2-UE set passed EXPLICITLY for a stable pipeline fixture. (The
        # synthetic default is now the config-derived PROVISIONED set, also 2-UE
        # today, but pass ue_ids explicitly so this fixture is count-independent.)
        steps, eps = generate_experiment(methods, trials=5, seed=42,
                                         ue_ids=("ue1", "ue2"))
        return methods, compute_multi_method(steps, eps)

    def test_primary_default_enumerates_provisioned_ues(self):
        # UE count is variable: the synthetic PRIMARY default (no explicit ue_ids)
        # enumerates the config-derived PROVISIONED UE set, which today is ue1/ue2
        # (the unprovisioned logical ue3 is EXCLUDED and would propagate once
        # provisioned). Callers wanting a 3-UE what-if pass ue_ids explicitly.
        from experiments.synthetic import generate_experiment
        steps, _ = generate_experiment(["llm_with_history"], trials=1, seed=1)
        seen = set()
        for s in steps:
            seen.update(s.ue_kpis.keys())
        self.assertEqual(seen, {"ue1", "ue2"})

    def test_full_metric_set_present(self):
        _methods, m = self._run()
        res = m["llm_with_history"]
        for key in ("dual_intent_satisfaction", "per_ue_kpi", "rollback",
                    "negotiation", "consecutive_rollback", "ece", "enforcement",
                    "hard_failures", "cost_estimate", "latency"):
            self.assertIn(key, res)

    def test_determinism(self):
        _m1, a = self._run()
        _m2, b = self._run()
        ra = a["llm_with_history"]["dual_intent_satisfaction"]["overall"]["dual_satisfaction_rate"]
        rb = b["llm_with_history"]["dual_intent_satisfaction"]["overall"]["dual_satisfaction_rate"]
        self.assertEqual(ra, rb)  # same seed => identical

    def test_sanity_ordering(self):
        _methods, m = self._run()
        best = m["llm_with_history"]["dual_intent_satisfaction"]["overall"]["dual_satisfaction_rate"]
        worst = m["rule_based"]["dual_intent_satisfaction"]["overall"]["dual_satisfaction_rate"]
        self.assertGreater(best, worst)  # proposed beats rule-based baseline

    def test_theta_star_in_unit_interval_for_headline(self):
        # with a full 5-trial synthetic run the headline method's derived theta*
        # should satisfy the paper's Prop. 2 range (0,1)
        _methods, m = self._run()
        cost = m["llm_with_history"]["cost_estimate"]
        self.assertGreater(cost["theta_star"], 0.0)
        self.assertLess(cost["theta_star"], 1.0)


class TestFiguresSmoke(unittest.TestCase):
    """Figures must render headlessly to files (Agg backend)."""

    def test_generate_all_figures(self):
        from experiments.synthetic import generate_experiment, METHOD_PROFILES
        from experiments.figures import generate_all_figures
        steps, eps = generate_experiment(list(METHOD_PROFILES.keys()), trials=3,
                                         ue_ids=("ue1", "ue2"))  # legacy 2-UE
        m = compute_multi_method(steps, eps)
        with tempfile.TemporaryDirectory() as d:
            figs = generate_all_figures(steps, eps, m, output_dir=d,
                                        headline_method="llm_with_history")
            for _name, paths in figs.items():
                for p in paths:
                    self.assertTrue(os.path.exists(p), f"missing {p}")


class TestRunnerOffline(unittest.TestCase):
    """The runner's synthetic path produces records + metrics + report offline."""

    def test_run_synthetic(self):
        from experiments.runner import ExperimentRunner
        with tempfile.TemporaryDirectory() as d:
            r = ExperimentRunner(output_dir=d)
            out = r.run(mode="synthetic", trials=3, make_figures=False)
            self.assertGreater(out["n_steps"], 0)
            self.assertGreater(out["n_episodes"], 0)
            # P0-20: the default method set is the canonical six (single source
            # experiments.runner.DEFAULT_METHODS == paired_runner.REQUIRED_METHODS,
            # headline 'adaptive'; llm_with_history is a KNOWN integration method,
            # NOT a default). The metrics key set is EXACTLY the canonical six.
            from experiments.runner import DEFAULT_METHODS
            from experiments.paired_runner import REQUIRED_METHODS
            self.assertEqual(set(DEFAULT_METHODS), set(REQUIRED_METHODS))
            self.assertEqual(set(out["metrics"]), set(REQUIRED_METHODS))
            self.assertIn("adaptive", out["metrics"])
            self.assertNotIn("llm_with_history", out["metrics"])
            self.assertFalse(out["paper_ready"])   # non-paired run is not paper
            self.assertIn("EXPERIMENT REPORT", out["report"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
