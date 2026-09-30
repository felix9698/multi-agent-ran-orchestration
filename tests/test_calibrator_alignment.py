"""P5: calibrator aligned with the single offline estimator.

Numeric acceptance scenario (spec): 10 episodes - 8 successes
(before 5.5 -> after 9.4, min 5.3), 2 failures (rolled back, min 1.6,
after 5.5), each failure with a recorded negotiation (2 rounds, 10 s,
tput_during_nego 5.5). Expected: R_success = 58.5, C_cont = 58.5,
C_nego = 25 -> theta* = 33.5/117 = 0.286 (NOT the 0.1 clamp).
"""

import tempfile
import unittest
from unittest import mock

from calibration.adaptive_calibrator import AdaptiveCalibrator, CostMetrics
from experiments.metrics import IntentConfig, estimate_costs


def _success() -> CostMetrics:
    return CostMetrics(trial_executed=True, trial_success=True,
                       throughput_before=5.5, throughput_after=9.4,
                       throughput_trial_min=5.3, trial_duration=15.0)


def _failure_with_nego() -> CostMetrics:
    return CostMetrics(trial_executed=True, trial_success=False,
                       rolled_back=True,
                       throughput_before=5.5, throughput_after=5.5,
                       throughput_trial_min=1.6, trial_duration=15.0,
                       negotiation_entered=True, negotiation_rounds=2,
                       negotiation_duration=10.0, throughput_during_nego=5.5)


def _scenario():
    return ([_success() for _ in range(8)]
            + [_failure_with_nego() for _ in range(2)])


class CalibratorNumericAcceptanceTest(unittest.TestCase):

    def test_scenario_theta_not_clamped_and_matches_offline(self):
        cal = AdaptiveCalibrator(mode="online", window_size=20,
                                 initial_theta=0.7, c_episode=500.0)
        eps = _scenario()
        for m in eps:
            cal.record_episode(m)

        offline = estimate_costs([m.to_episode_record() for m in eps],
                                 IntentConfig(), c_episode=500.0)
        # spec numbers hold for the offline estimator
        self.assertAlmostEqual(offline["r_success"], 58.5, places=6)
        self.assertAlmostEqual(offline["c_cont"], 58.5, places=6)
        self.assertAlmostEqual(offline["c_nego"], 25.0, places=6)
        self.assertAlmostEqual(offline["theta_star"], 33.5 / 117.0, places=6)

        # runtime == offline to >=3 decimals (single-estimator principle)
        self.assertAlmostEqual(cal.theta_star, offline["theta_star"], places=3)
        self.assertAlmostEqual(cal.r_success_estimate, offline["r_success"],
                               places=3)
        self.assertAlmostEqual(cal.c_worst_estimate, offline["c_worst"],
                               places=3)
        self.assertAlmostEqual(cal.c_nego_estimate, offline["c_nego"],
                               places=3)

        # NOT collapsed to the 0.1 clamp (old all-trial C_worst dilution bug:
        # 80% success rate used to drive theta* to the floor in 5 episodes)
        self.assertGreaterEqual(cal.theta_star, 0.2)
        self.assertLessEqual(cal.theta_star, 0.4)
        self.assertTrue(cal.assumption_ok)
        self.assertEqual(cal.n_max, offline["n_max"])
        self.assertEqual(cal.n_max, 5)   # floor(500 / (58.5 + 25))

    def test_stats_expose_raw_clamped_and_assumption(self):
        cal = AdaptiveCalibrator(mode="online", window_size=20)
        for m in _scenario():
            cal.record_episode(m)
        stats = cal.get_stats()
        self.assertAlmostEqual(stats["theta_star_raw"], 33.5 / 117.0, places=6)
        self.assertAlmostEqual(stats["theta_star_clamped"], stats["theta_star"])
        self.assertTrue(stats["assumption_ok"])

    def test_n_max_fully_delegated_zero_allowed(self):
        # Eq. 9 floor can legitimately be 0 when negotiation is unaffordable
        # within the episode budget - the runtime must NOT force N_max >= 1.
        cal = AdaptiveCalibrator(mode="online", window_size=20,
                                 c_episode=50.0)   # floor(50/83.5) = 0
        for m in _scenario():
            cal.record_episode(m)
        self.assertEqual(cal.n_max, 0)
        self.assertFalse(cal.can_negotiate_more(0))

    def test_c_nego_tracks_measured_negotiations(self):
        # old defect (2): negotiation episodes were never recorded, so
        # C_nego stayed at the prior 50 forever
        cal = AdaptiveCalibrator(mode="online", window_size=20)
        for m in _scenario():
            cal.record_episode(m)
        self.assertAlmostEqual(cal.c_nego_estimate, 25.0, places=6)


class CalibratorIndependenceTest(unittest.TestCase):

    def test_no_carry_between_instances(self):
        # old defect (3): /tmp data_file default carried theta* across
        # sessions/trials/models
        cal1 = AdaptiveCalibrator(mode="online", window_size=20)
        self.assertIsNone(cal1.data_file)
        for m in _scenario():
            cal1.record_episode(m)
        self.assertLess(cal1.theta_star, 0.5)   # updated away from initial
        cal2 = AdaptiveCalibrator(mode="online", window_size=20)
        self.assertEqual(cal2.total_episodes, 0)
        # blocker 7: an OMITTED initial_theta derives from CalibrationConfig
        # (the ONE cold-start source), NOT a hidden 0.7.
        from config import CalibrationConfig
        self.assertAlmostEqual(cal2.theta_star,
                               CalibrationConfig().compute_initial_theta())

    def test_reset_restores_initial_state(self):
        cal = AdaptiveCalibrator(mode="online", window_size=20,
                                 initial_theta=0.7, initial_n_max=3)
        for m in _scenario():
            cal.record_episode(m)
        cal.reset()
        self.assertAlmostEqual(cal.theta_star, 0.7)
        self.assertAlmostEqual(cal.theta_star_raw, 0.7)
        self.assertEqual(cal.n_max, 3)
        self.assertEqual(cal.total_episodes, 0)
        self.assertEqual(len(cal.history), 0)
        self.assertTrue(cal.assumption_ok)

    def test_fixed_mode_never_updates(self):
        cal = AdaptiveCalibrator(mode="fixed", initial_theta=0.7,
                                 initial_n_max=3)
        for m in _scenario():
            cal.record_episode(m)
        self.assertAlmostEqual(cal.theta_star, 0.7)
        self.assertEqual(cal.n_max, 3)


class RunnerCalibrationWiringTest(unittest.TestCase):

    def test_fresh_calibration_resets_unless_carry(self):
        from experiments.runner import ExperimentRunner

        class _Cal:
            def __init__(self):
                self.resets = 0

            def reset(self):
                self.resets += 1

        class _Coord:
            def __init__(self):
                self.calibrator = _Cal()

        with tempfile.TemporaryDirectory() as tmp:
            r = ExperimentRunner(coordinator=_Coord(), output_dir=tmp)
            r._fresh_calibration(r.coordinator)
            self.assertEqual(r.coordinator.calibrator.resets, 1)
            r.carry_calibration = True
            r._fresh_calibration(r.coordinator)
            self.assertEqual(r.coordinator.calibrator.resets, 1)   # no reset

    def test_make_live_coordinator_fixed_theta(self):
        from experiments import runner as runner_mod

        class _Cal:
            mode = "online"

        class _Coord:
            calibrator = _Cal()

            def set_llm_backend(self, b):
                return True

            def start(self):
                pass

        stub = _Coord()
        # real get_config() never returns None; supply a config stub whose
        # live-topology check is a no-op so the P0-18 fail-closed guard in
        # _make_live_coordinator is exercised (not bypassed).
        cfg_stub = mock.Mock()
        cfg_stub.validate_live_topology.return_value = None
        with mock.patch("coordinator.intent_coordinator.IntentCoordinator",
                        return_value=stub), \
             mock.patch("config.get_config", return_value=cfg_stub):
            c = runner_mod._make_live_coordinator("claude-sonnet", "fixed")
        self.assertEqual(c.calibrator.mode, "fixed")


class CoordinatorSinglePointRecordingTest(unittest.TestCase):
    """process_intent records ALL S2-routed episodes (incl. nego-only) with
    the P3/P4 measured fields, through the real AdaptiveCalibrator."""

    def _make(self, cal):
        from coordinator.intent_coordinator import IntentCoordinator
        from decision.intent_model import (
            Alternative, ConstraintType, FeasibilityPrediction, Intent,
            IntentTarget, IntentType, NetworkState,
        )

        class _Metric:
            def __init__(self, tput=5.5):
                self.attached = True
                self.throughput_mbps = tput

        class _Collector:
            simulation_mode = False

            def collect_all(self):
                return {"ue1": _Metric()}

            def get_throughput_all(self, duration=2.0):
                # Batch F: negotiation/window KPI comes from the LIVE probe.
                return {"ue1": 5.5}

        c = IntentCoordinator.__new__(IntentCoordinator)
        c.gui = None
        c.on_state_change = None
        c.on_negotiation_needed = None
        c.current_state = "S0"
        c.current_phase = "Degradation"
        c.history_reservoir = []
        c.max_history = 100
        c.tau_trial_s = 15.0
        c.hard_failure_cap_s = 0.1
        c._pre_trial_attached = set()
        c._last_throughput = {}
        c._last_tp_time = 0.0
        c.ue_collector = _Collector()
        c.calibrator = cal
        c.negotiation_policy = None
        c.generate_alternatives_fn = lambda i, r: []
        c.intent_manager = type("IM", (), {"get_active": lambda s: [],
                                           "get_monitored": lambda s: [],
                                           "add": lambda s, i: None})()
        intent = Intent(type=IntentType.THROUGHPUT_GOAL,
                        target=IntentTarget(kpi_name="throughput",
                                            constraint_type=ConstraintType.MIN,
                                            target_value=8.0, unit="Mbps"))
        alt = Alternative(id="a1", description="relax to 6")
        c._parse_intent = lambda text: {"intent": intent, "raw": {}}
        c._check_conflicts = lambda new, active: True
        c._get_network_state = lambda: NetworkState(ue_states={})
        self._feas = lambda conf: FeasibilityPrediction(
            feasible=True, confidence=conf, reasoning="", alternatives=[alt])
        return c

    def test_nego_only_episode_is_recorded(self):
        # honest caps make cold-start N_max=0; this test EXERCISES negotiation,
        # so it sets an explicit experiment-specific N_max override (blocker 6).
        cal = AdaptiveCalibrator(mode="online", window_size=20, initial_n_max=3)
        c = self._make(cal)
        c._analyze_feasibility = lambda i, a, s: self._feas(0.3)  # < theta
        c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(cal.total_episodes, 1)
        rec = cal.history[-1]
        self.assertFalse(rec.trial_executed)
        self.assertTrue(rec.negotiation_entered)
        self.assertEqual(rec.negotiation_rounds, 1)
        self.assertAlmostEqual(rec.throughput_during_nego, 5.5)
        self.assertEqual(rec.phase, "Degradation")

    def test_rolled_back_trial_recorded_with_measured_fields(self):
        cal = AdaptiveCalibrator(mode="online", window_size=20, initial_n_max=3)
        c = self._make(cal)
        c._analyze_feasibility = lambda i, a, s: self._feas(0.9)
        c._execute_trial = lambda feas: {"success": True, "snapshot": {},
                                         "clipped": [], "applied": []}
        c._validate_trial = lambda ni, ai: {
            "all_satisfied": False, "metrics": {}, "trajectory": [],
            "tput_min": 1.6, "tput_after": 5.5,
            "hard_failure": False, "reconnection_time": 0.0}
        c._rollback = lambda snapshot: None
        c.process_intent("throughput >= 8 Mbps")
        rec = cal.history[-1]
        self.assertTrue(rec.trial_executed)
        self.assertFalse(rec.trial_success)
        self.assertTrue(rec.rolled_back)
        self.assertAlmostEqual(rec.throughput_trial_min, 1.6)
        self.assertAlmostEqual(rec.throughput_before, 5.5)
        self.assertTrue(rec.negotiation_entered)   # auto-accept nego followed


if __name__ == "__main__":
    unittest.main()
