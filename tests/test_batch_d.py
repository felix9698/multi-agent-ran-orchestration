"""Batch D: runtime reserve ledger (P0-10), expected-vs-deterministic-bound
separation (P0-11), one cold-start config source (P0-12), and partitioned
confidence calibration (P0-17).

The end-to-end budget tests reuse the Batch-B skeleton coordinator (real
S0-S6 flow, no hardware) and drive it to the S3 trial / S5 negotiation
transition; the ledger is exercised at those exact points.
"""

import math
import random
import unittest

from config import CalibrationConfig
from calibration.adaptive_calibrator import AdaptiveCalibrator
from coordinator.episode_types import (
    ReserveLedger, TerminalOutcome, TerminalReason,
)
from experiments.metrics import (
    CostPriors, EpisodeRecord, IntentConfig, derive_n_max, estimate_costs,
    trajectory_cost,
)

from tests.test_safety_transaction import (
    _make_coordinator, _trial_ok, _make_intent,
)
from decision.intent_model import (
    Alternative, FeasibilityPrediction, NetworkState,
)


# ===========================================================================
# P0-10: reserve ledger
# ===========================================================================
class ReserveLedgerUnitTest(unittest.TestCase):

    def test_budget_reserves_initial_trial(self):
        led = ReserveLedger(c_episode=500, c_trial_cont_ub=150, c_hard_ub=30,
                            c_nego_ub=50)
        self.assertTrue(led.reserve_trial("initial_trial"))
        self.assertAlmostEqual(led.reserved, 180)          # 150 + 30 (no double)
        e = led.entries[-1]
        self.assertTrue(e["accepted"])
        self.assertEqual(e["kind"], "initial_trial")
        # explicit component breakdown: continuous + hard failure
        self.assertAlmostEqual(e["components"]["continuous"], 150)
        self.assertAlmostEqual(e["components"]["hard_failure"], 30)
        self.assertAlmostEqual(e["cumulative_reserved"], 180)

    def test_budget_blocks_initial_trial_when_insufficient(self):
        # C_episode < C_trial_ub -> the initial trial cannot even be reserved.
        led = ReserveLedger(c_episode=100, c_trial_cont_ub=150, c_hard_ub=30,
                            c_nego_ub=50)
        self.assertFalse(led.can_reserve_trial())
        self.assertFalse(led.reserve_trial())
        self.assertAlmostEqual(led.reserved, 0.0)          # nothing reserved
        self.assertFalse(led.entries[-1]["accepted"])      # rejected recorded

    def test_budget_covers_n_plus_one_trials(self):
        # corrected Eq. 9: N negotiations imply (N+1) trials + N negotiations.
        cfg = CalibrationConfig()
        n = cfg.compute_n_max()          # deterministic, corrected formula
        led = ReserveLedger(cfg.c_episode, cfg.kpi_loss_cap_mbps * cfg.tau_trial,
                            cfg.c_hard_ub(), cfg.c_nego_ub())
        # (N+1) trials + N negotiations must ALL fit
        self.assertTrue(led.reserve_trial("initial_trial"))
        for _ in range(n):
            self.assertTrue(led.reserve_negotiation())
            self.assertTrue(led.reserve_trial("revised_trial"))
        self.assertLessEqual(led.reserved, cfg.c_episode + 1e-9)
        # ... and one more negotiation+trial would NOT fit (N is maximal).
        # (Under the honest caps N may be 0, in which case even the first
        # revised trial after a negotiation must not fit.)
        extra = led.reserve_negotiation() and led.reserve_trial("revised_trial")
        self.assertFalse(extra)

    def test_budget_never_exceeds_episode_cap_property(self):
        # property: NO sequence of reservations ever pushes reserved past cap.
        rng = random.Random(1234)
        for _ in range(200):
            led = ReserveLedger(rng.uniform(0, 600), rng.uniform(0, 120),
                                rng.uniform(0, 60), rng.uniform(0, 60))
            for _ in range(rng.randint(0, 20)):
                if rng.random() < 0.5:
                    led.reserve_trial()
                else:
                    led.reserve_negotiation()
                self.assertLessEqual(led.reserved, led.c_episode + 1e-9)

    def test_nmax_zero_still_requires_initial_trial_reservation(self):
        # adversarial: N_max=0 does NOT imply a free initial trial.
        # c_episode just under one trial+nego but >= one trial: N_max=0, initial
        # trial still reserved (and a subsequent nego would not fit).
        led = ReserveLedger(c_episode=200, c_trial_cont_ub=150, c_hard_ub=30,
                            c_nego_ub=50)   # trial_ub=180, +nego 230 > 200
        self.assertEqual(derive_n_max(200, 180, 50), 0)     # N_max = 0
        self.assertTrue(led.reserve_trial())                # trial still reserved
        self.assertFalse(led.reserve_negotiation())         # nego would exceed

    def test_hard_failure_component_included_without_double_count(self):
        led = ReserveLedger(c_episode=500, c_trial_cont_ub=100, c_hard_ub=30,
                            c_nego_ub=50)
        self.assertAlmostEqual(led.c_trial_ub, 130)         # 100+30 once
        led.reserve_trial()
        self.assertAlmostEqual(led.reserved, 130)

    def test_actual_cost_recorded_separately_from_reserved(self):
        led = ReserveLedger(500, 150, 30, 50)
        led.reserve_trial()
        rid = led.last_reservation_id
        led.settle(rid, ReserveLedger.SETTLED, actual=12.5, method="trajectory")
        self.assertAlmostEqual(led.reserved, 180)           # worst-case reserve
        self.assertAlmostEqual(led.actual, 12.5)            # measured actual
        self.assertNotEqual(led.reserved, led.actual)


class BudgetEndToEndTest(unittest.TestCase):
    """The ledger at the real S3 / S5 transition points."""

    def _trial_coord(self):
        c, _ = _make_coordinator()
        c.tau_trial_s = 0.0            # zero-window: trial cont cap -> 0
        calls = {"exec": 0}
        real = _trial_ok

        def _exec(feas):
            calls["exec"] += 1
            return real(feas)
        c._execute_trial = _exec
        c._validate_trial = lambda ni, ai: {"all_satisfied": True, "metrics": {}}
        c._exec_calls = calls
        return c

    def test_budget_reserves_initial_trial(self):
        c = self._trial_coord()
        result = c.process_intent("throughput >= 8 Mbps")
        led = result["reserve_ledger"]
        trial_entries = [e for e in led["entries"] if e["accepted"]
                         and "trial" in e["kind"]]
        self.assertTrue(trial_entries)                       # a trial was reserved
        self.assertIn("hard_failure", trial_entries[0]["components"])
        # and it is reflected on the evidence record too
        self.assertIsNotNone(result["evidence"]["reserve_ledger"])

    def test_budget_blocks_initial_trial_when_insufficient(self):
        c = self._trial_coord()
        # a ledger that cannot afford ANY trial (cap below the trial UB)
        c._new_reserve_ledger = lambda: ReserveLedger(
            c_episode=10, c_trial_cont_ub=100, c_hard_ub=30, c_nego_ub=50)
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.INSUFFICIENT_TRIAL_BUDGET.value)
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)
        self.assertEqual(c._exec_calls["exec"], 0)           # NO write/actuation
        self.assertFalse(result["success"])

    def test_budget_reserves_negotiation_before_callback(self):
        # route to negotiation (low confidence); a ledger that cannot afford the
        # negotiation must stop BEFORE the callback.
        relaxed = _make_intent(6.0)
        alt = Alternative(id="a1", description="relax", modified_intent=relaxed)
        c, _ = _make_coordinator(alt=alt)
        c.tau_trial_s = 0.0
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=0.1, reasoning="", alternatives=[alt])
        consulted = {"n": 0}
        c.generate_alternatives_fn = lambda intent, rej: (
            consulted.__setitem__("n", consulted["n"] + 1) or [alt])
        c._new_reserve_ledger = lambda: ReserveLedger(
            c_episode=10, c_trial_cont_ub=0, c_hard_ub=0, c_nego_ub=50)
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.INSUFFICIENT_NEGOTIATION_BUDGET.value)
        self.assertEqual(consulted["n"], 0)                  # NO callback invoked

    def test_rejected_reservation_leaves_ledger_consistent_on_exception(self):
        # adversarial: even if _execute_trial raises AFTER a reservation, the
        # reservation was recorded before the risky call, so the ledger stays
        # consistent (reserved <= cap) and the episode fails closed.
        c = self._trial_coord()

        def _boom(feas):
            raise RuntimeError("trial exploded")
        c._execute_trial = _boom
        result = c.process_intent("throughput >= 8 Mbps")
        led = result.get("reserve_ledger") or \
            result["evidence"].get("reserve_ledger")
        self.assertIsNotNone(led)
        self.assertLessEqual(led["reserved"], led["c_episode"] + 1e-9)
        # a trial reservation is on record even though the write failed
        self.assertTrue(any("trial" in e["kind"] for e in led["entries"]))


# ===========================================================================
# P0-11: expected estimates vs deterministic bounds
# ===========================================================================
class ExpectedVsUpperBoundTest(unittest.TestCase):

    def test_risk_upper_bound_does_not_use_sample_mean(self):
        # feed episodes with a TINY empirical continuous cost; the deterministic
        # c_trial_ub must NOT drop toward the sample mean.
        cfg = IntentConfig()
        eps = [EpisodeRecord(1, "m", "P", trial_executed=True, rolled_back=True,
                             throughput_before=1.0, throughput_trial_min=0.9,
                             tau_trial=15) for _ in range(5)]
        priors = CostPriors()
        out = estimate_costs(eps, cfg, c_episode=500.0, priors=priors)
        # empirical c_cont is tiny (~1.5), but the deterministic UB is unmoved
        self.assertLess(out["c_cont"], 5.0)
        self.assertAlmostEqual(out["upper_bounds"]["c_trial_ub"],
                               priors.c_trial_ub())
        self.assertGreater(out["upper_bounds"]["c_trial_ub"], out["c_cont"])

    def test_expected_and_upper_bound_costs_are_separate(self):
        cfg = IntentConfig()
        out = estimate_costs([], cfg, c_episode=500.0, priors=CostPriors())
        self.assertIn("expected", out)
        self.assertIn("upper_bounds", out)
        self.assertEqual(out["expected"]["p_hard_definition"],
                         "per_trial_unconditional")
        self.assertEqual(out["upper_bounds"]["source"], "deterministic_caps")
        # they are different NAMES carrying different values
        self.assertNotEqual(out["expected"]["c_worst"],
                            out["upper_bounds"]["c_trial_ub"])

    def test_trajectory_cost_uses_sample_timestamps(self):
        # deficit trajectory 0,0,10 over t=0,1,2 (ref 10) integrates to 5.0,
        # NOT the tput_min*tau shortcut (min=0 -> (10-0)*2 = 20).
        samples = [(0.0, 10.0), (1.0, 10.0), (2.0, 0.0)]
        area = trajectory_cost(samples, deficit_ref=10.0)
        self.assertAlmostEqual(area, 5.0)
        self.assertNotAlmostEqual(area, (10.0 - 0.0) * 2.0)  # not the shortcut
        # < 2 timestamped samples -> None (caller falls back, flagged)
        self.assertIsNone(trajectory_cost([(0.0, 5.0)], deficit_ref=10.0))

    def test_estimator_reports_trajectory_vs_legacy_usage(self):
        cfg = IntentConfig()
        traj = [(0.0, 5.0), (1.0, 1.0), (2.0, 1.0)]
        eps = [EpisodeRecord(1, "m", "P", trial_executed=True, rolled_back=True,
                             throughput_before=5.0, throughput_trial_min=1.0,
                             tau_trial=15, trial_trajectory=traj),
               EpisodeRecord(2, "m", "P", trial_executed=True, rolled_back=True,
                             throughput_before=5.0, throughput_trial_min=1.0,
                             tau_trial=15)]   # no trajectory -> legacy
        out = estimate_costs(eps, cfg, c_episode=500.0)
        self.assertEqual(out["trajectory"]["integrated"]["c_cont"], 1)
        self.assertEqual(out["trajectory"]["legacy"]["c_cont"], 1)


# ===========================================================================
# P0-12: one cold-start configuration source
# ===========================================================================
class ColdStartSourceTest(unittest.TestCase):

    def test_runtime_initial_theta_matches_config(self):
        cfg = CalibrationConfig()
        cal = AdaptiveCalibrator(
            initial_theta=cfg.compute_initial_theta(),
            initial_n_max=cfg.compute_initial_n_max(cfg.tau_trial),
            c_episode=cfg.c_episode)
        self.assertAlmostEqual(cal.theta_star, cfg.compute_initial_theta())
        self.assertAlmostEqual(cal.get_theta_star(), cfg.compute_initial_theta())

    def test_runtime_initial_nmax_matches_config(self):
        cfg = CalibrationConfig()
        cal = AdaptiveCalibrator(
            initial_theta=cfg.compute_initial_theta(),
            initial_n_max=cfg.compute_initial_n_max(cfg.tau_trial),
            c_episode=cfg.c_episode)
        self.assertEqual(cal.get_n_max(), cfg.compute_initial_n_max(cfg.tau_trial))
        # explicit override is honored
        cfg2 = CalibrationConfig(initial_n_max=7)
        self.assertEqual(cfg2.compute_initial_n_max(), 7)

    def test_all_modes_share_calibration_source(self):
        cfg = CalibrationConfig()
        theta = cfg.compute_initial_theta()
        # synthetic mode derives its θ* from the SAME config source
        import experiments.synthetic as synth
        self.assertAlmostEqual(synth.SYNTHETIC_COLD_START_THETA, theta)
        # coordinator/emulated runtime: the emulated coordinator (which the
        # runner drives) starts from the config-derived values, not 0.7/3.
        from experiments.emulation import build_emulated_coordinator
        from experiments.topology import two_ue_topology   # legacy 2-UE (explicit)
        coordinator, _ = build_emulated_coordinator(
            seed=1, tau_trial_s=15.0, topology=two_ue_topology())
        try:
            self.assertAlmostEqual(coordinator.calibrator._initial_theta, theta)
            self.assertEqual(coordinator.calibrator._initial_n_max,
                             cfg.compute_initial_n_max(cfg.tau_trial))
            self.assertNotEqual(coordinator.calibrator._initial_theta, 0.7)
        finally:
            coordinator.stop()

    def test_cold_start_source_recorded_in_stats(self):
        cfg = CalibrationConfig()
        cal = AdaptiveCalibrator(
            initial_theta=cfg.compute_initial_theta(),
            initial_n_max=cfg.compute_initial_n_max(),
            c_episode=cfg.c_episode,
            cold_start_source=cfg.cold_start_source())
        st = cal.get_stats()
        self.assertEqual(st["cold_start_source"]["policy"], "cost_derived")
        self.assertAlmostEqual(st["cold_start_source"]["initial_theta"],
                               cfg.compute_initial_theta())


# ===========================================================================
# P0-17: partitioned reliability calibration
# ===========================================================================
class CalibrationTest(unittest.TestCase):

    def _cal(self):
        return AdaptiveCalibrator(initial_theta=0.4, initial_n_max=2,
                                  calibration_min_samples=5, calibration_bins=10)

    def test_calibration_partitioned_by_model_and_regime(self):
        cal = self._cal()
        # model A / regime R1: raw ~0.85 always FAILS -> calibrated low
        for _ in range(10):
            cal.record_calibration_outcome(0.85, False, "modelA", "R1")
        # model B / regime R1: raw ~0.85 always SUCCEEDS -> calibrated high
        for _ in range(10):
            cal.record_calibration_outcome(0.85, True, "modelB", "R1")
        pa = cal.calibrated_probability(0.85, "modelA", "R1")
        pb = cal.calibrated_probability(0.85, "modelB", "R1")
        self.assertLess(pa, 0.2)
        self.assertGreater(pb, 0.8)
        self.assertEqual(len(cal.calibration_partitions()), 2)

    def test_model_regime_cross_contamination_blocked(self):
        cal = self._cal()
        for _ in range(10):
            cal.record_calibration_outcome(0.85, False, "modelA", "R1")
        # a DIFFERENT regime for the same model must NOT reuse R1's map
        p_other = cal.calibrated_probability(0.85, "modelA", "R2")
        self.assertAlmostEqual(p_other, 0.85)   # cold-start identity, not 0.0
        # a different model likewise
        p_modelb = cal.calibrated_probability(0.85, "modelB", "R1")
        self.assertAlmostEqual(p_modelb, 0.85)

    def test_raw_and_calibrated_confidence_are_distinct(self):
        cal = self._cal()
        for _ in range(10):
            cal.record_calibration_outcome(0.9, False, "m", "R")
        raw = 0.9
        calibrated = cal.calibrated_probability(raw, "m", "R")
        self.assertNotAlmostEqual(raw, calibrated)   # high raw, low calibrated
        self.assertLess(calibrated, raw)

    def test_raw_high_but_calibrated_below_threshold(self):
        cal = self._cal()
        for _ in range(10):
            cal.record_calibration_outcome(0.9, False, "m", "R")
        theta = 0.4
        raw = 0.9
        calibrated = cal.calibrated_probability(raw, "m", "R")
        self.assertGreaterEqual(raw, theta)          # raw would route to trial
        self.assertLess(calibrated, theta)           # calibrated routes to nego

    def test_non_finite_confidence_fails_closed(self):
        cal = self._cal()
        for bad in (float("nan"), float("inf"), None, "x"):
            self.assertEqual(cal.calibrated_probability(bad, "m", "R"), 0.0)
            # a non-finite outcome record is dropped, never stored
            self.assertFalse(cal.record_calibration_outcome(bad, True, "m", "R"))

    def test_reset_clears_partitions(self):
        cal = self._cal()
        for _ in range(10):
            cal.record_calibration_outcome(0.85, True, "m", "R")
        self.assertEqual(len(cal.calibration_partitions()), 1)
        cal.reset()
        self.assertEqual(len(cal.calibration_partitions()), 0)
        self.assertAlmostEqual(cal.calibrated_probability(0.85, "m", "R"), 0.85)

    def test_calibration_metrics_include_coverage(self):
        cal = self._cal()
        for i in range(12):
            cal.record_calibration_outcome(0.85, i % 2 == 0, "m", "R")
        met = cal.calibration_metrics("m", "R")
        for key in ("ece", "brier", "log_loss", "calibration_slope",
                    "coverage", "ci95", "sample_count", "sufficient_samples"):
            self.assertIn(key, met)
        self.assertEqual(met["sample_count"], 12)
        self.assertTrue(met["sufficient_samples"])
        self.assertGreaterEqual(met["coverage"], 0.0)
        self.assertLessEqual(met["coverage"], 1.0)

    def test_insufficient_samples_not_claimed_superior(self):
        cal = self._cal()
        cal.record_calibration_outcome(0.85, True, "m", "R")  # only 1 sample
        met = cal.calibration_metrics("m", "R")
        self.assertFalse(met["sufficient_samples"])
        # calibrated falls back to raw (no superiority claimed)
        self.assertAlmostEqual(cal.calibrated_probability(0.85, "m", "R"), 0.85)


class RoutingUsesCalibratedTest(unittest.TestCase):

    def test_routing_uses_calibrated_probability(self):
        # a real calibrator whose partition maps a high raw score to a LOW
        # calibrated probability must route the coordinator to NEGOTIATION even
        # though the raw score exceeds theta.
        c, _ = _make_coordinator()
        c.tau_trial_s = 0.0
        cal = AdaptiveCalibrator(initial_theta=0.4, initial_n_max=2,
                                 calibration_min_samples=5, calibration_bins=10)
        for _ in range(10):
            cal.record_calibration_outcome(0.95, False, "fake-model", "default")
        c.calibrator = cal
        exec_calls = {"n": 0}
        c._execute_trial = lambda feas: (
            exec_calls.__setitem__("n", exec_calls["n"] + 1)
            or _trial_ok(feas))
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=0.95, reasoning="",
            alternatives=[Alternative(id="a1", description="x")])
        result = c.process_intent("throughput >= 8 Mbps")
        last = result["cycles"][0]
        self.assertEqual(last["threshold_applied_to"], "calibrated_probability")
        self.assertAlmostEqual(last["raw_confidence"], 0.95)
        self.assertLess(last["calibrated_probability"], 0.4)   # calibrated low
        self.assertEqual(last["routed_to"], "negotiation")     # NOT a trial
        self.assertEqual(exec_calls["n"], 0)                   # no trial ran


if __name__ == "__main__":
    unittest.main()
