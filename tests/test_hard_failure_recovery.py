"""Batch B / P0-3: hard-failure ordering and physical-recovery separation.

On the DEFAULT-SAFE path a confirmed hard failure (1) STOPS sampling
immediately, (2) rolls the configuration back BEFORE any reconnection/recovery
wait, and (3) checks PHYSICAL recovery UNDER the restored config, recording
config_restore_verdict and physical_recovery_verdict SEPARATELY.  UNKNOWN is
never success, and a hard failure never commits.

The legacy in-window reconnection contract (stop_on_hard_failure=False) stays
covered by tests/test_trial_window.py.
"""

import unittest

from coordinator.intent_coordinator import IntentCoordinator
from coordinator.episode_types import (
    ConfigRestoreVerdict, PhysicalRecoveryVerdict, SafetyState, TerminalOutcome,
)
from decision.intent_model import (
    ConstraintType, FeasibilityPrediction, Intent, IntentTarget, IntentType,
    NetworkState,
)


class _M:
    def __init__(self, attached=True, tput=5.0):
        self.attached = attached
        self.throughput_mbps = tput
        self.latency_ms = None

    def to_dict(self):
        return {"attached": self.attached, "throughput_mbps": self.throughput_mbps}


def _intent(target=4.0):
    return Intent(type=IntentType.THROUGHPUT_GOAL,
                  target=IntentTarget(kpi_name="throughput",
                                      constraint_type=ConstraintType.MIN,
                                      target_value=target, unit="Mbps"))


class _FakeIntentManager:
    def get_active(self):
        return []

    def get_monitored(self):
        return []

    def add(self, intent):
        pass


class _FakeCalibrator:
    def __init__(self, n_max=0):
        self.n_max = n_max

    def get_theta_star(self, phase=None):
        return 0.5

    def can_negotiate_more(self, rounds, phase=None):
        return rounds < self.n_max

    def record_episode(self, metrics):
        pass


class _FakeLLM:
    def active_backend_name(self):
        return "fake"


class _FakeExecutor:
    def __init__(self):
        self._latched = False

    def latch_failsafe(self, reason=""):
        self._latched = True

    def clear_latch(self):
        self._latched = False

    @property
    def is_latched(self):
        return self._latched


# --------------------------------------------------------------------------
# (1) STOP sampling on first confirmed hard failure (real _validate_trial)
# --------------------------------------------------------------------------

class _StopCollector:
    simulation_mode = False

    def __init__(self):
        self.collect_calls = 0

    def collect_all(self):
        self.collect_calls += 1
        attached = self.collect_calls <= 1     # attached only on the 1st sample
        return {"ue1": _M(attached, 5.0 if attached else None)}

    def get_throughput_all(self, duration=2.0):
        return {}


def _validate_coordinator(collector, tau=0.5):
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.gui = None
    c.on_state_change = None
    c.on_negotiation_needed = None
    c.current_state = "S0"
    c.tau_trial_s = tau
    c.hard_failure_cap_s = 0.1
    c._pre_trial_attached = {"ue1"}
    c._last_throughput = {}
    c._last_tp_time = 0.0
    c.ue_collector = collector
    return c


class StopSamplingTest(unittest.TestCase):

    def test_hard_failure_stops_sampling_immediately(self):
        col = _StopCollector()
        c = _validate_coordinator(col, tau=0.5)     # tau/poll -> ~10 samples
        v = c._validate_trial(_intent(1.0), [])     # default stop-safe
        self.assertTrue(v["hard_failure"])
        self.assertTrue(v["recovery_pending"])
        self.assertEqual(v["failed_ues"], ["ue1"])
        # sampling STOPPED at the confirming sample (attached, detach, detach)
        self.assertEqual(col.collect_calls, 3)      # NOT the full ~10 window
        # reconnection is NOT measured in-window on the safe path
        self.assertEqual(v["reconnection_time"], 0.0)


# --------------------------------------------------------------------------
# (2)-(5) rollback-before-recovery, verdict separation, no-commit
# --------------------------------------------------------------------------

class _RecoveryCollector:
    """Serves the S3-entry pre-measurement AND the post-rollback recovery
    phase.  ``mode`` selects the recovery observation."""
    simulation_mode = False

    def __init__(self, mode, order):
        self.mode = mode          # "recovered" | "stuck" | "blind"
        self.order = order

    def collect_all(self):
        self.order.append("collect")
        if self.mode == "blind":
            raise RuntimeError("collector unavailable")
        if self.mode == "recovered":
            return {"ue1": _M(True, 6.0)}
        return {"ue1": _M(False, None)}            # stuck: observed, detached

    def get_throughput_all(self, duration=2.0):
        return {}


def _txn_coordinator(mode, restore_ok, order):
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.gui = None
    c.on_negotiation_needed = None
    c.on_state_change = None
    c.current_state = "S0"
    c.history_reservoir = []
    c.max_history = 100
    c.tau_trial_s = 0.2
    c.hard_failure_cap_s = 0.1
    c._pre_trial_attached = {"ue1"}
    c._last_throughput = {}
    c._last_tp_time = 0.0
    c.intent_manager = _FakeIntentManager()
    c.calibrator = _FakeCalibrator(n_max=0)         # no negotiation
    c.negotiation_policy = None
    c.generate_alternatives_fn = lambda i, r: []
    c.ue_collector = _RecoveryCollector(mode, order)
    c.executor = _FakeExecutor()
    c.llm_manager = _FakeLLM()
    c.safety_state = SafetyState.READY
    c._safety_latch = None

    intent = _intent(4.0)
    c._parse_intent = lambda text: {"intent": intent, "raw": {}}
    c._check_conflicts = lambda new, active: True
    c._get_network_state = lambda: NetworkState(ue_states={})
    c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
        feasible=True, confidence=0.9, reasoning="", alternatives=[])
    c._execute_trial = lambda feas: {
        "success": True, "snapshot": {"gnb1": {"power_offset_db": 0.0}},
        "clipped": [], "applied": [{"axis": "power"}], "first_write_time": 100.0}
    # a hard failure that stopped the window early (recovery pending)
    c._validate_trial = lambda ni, ai, **kw: {
        "all_satisfied": False, "metrics": {}, "trajectory": [],
        "tput_min": 0.0, "tput_after": 0.0, "hard_failure": True,
        "reconnection_time": 0.0, "measurement_invalid": False,
        "failed_ues": ["ue1"], "recovery_pending": True}
    c._rollback = lambda snap: order.append("rollback") or restore_ok
    return c


class RollbackBeforeRecoveryTest(unittest.TestCase):

    def test_hard_failure_rolls_back_before_recovery_wait(self):
        order = []
        c = _txn_coordinator("recovered", True, order)
        c.process_intent("throughput >= 4 Mbps")
        # S3-entry collect, THEN rollback, THEN recovery collect(s)
        self.assertIn("rollback", order)
        rb = order.index("rollback")
        self.assertGreater(len([x for x in order[rb + 1:] if x == "collect"]),
                           0, "recovery must be checked AFTER rollback")

    def test_recovery_is_checked_after_restore(self):
        order = []
        c = _txn_coordinator("recovered", True, order)
        result = c.process_intent("throughput >= 4 Mbps")
        cyc = result["cycles"][-1]
        self.assertEqual(cyc["physical_recovery_verdict"],
                         PhysicalRecoveryVerdict.RECOVERED.value)

    def test_restore_and_recovery_verdicts_are_separate(self):
        # config restore VERIFIED but service NOT_RECOVERED -> distinct fields
        order = []
        c = _txn_coordinator("stuck", True, order)
        result = c.process_intent("throughput >= 4 Mbps")
        cyc = result["cycles"][-1]
        self.assertEqual(cyc["config_restore_verdict"],
                         ConfigRestoreVerdict.VERIFIED.value)
        self.assertEqual(cyc["physical_recovery_verdict"],
                         PhysicalRecoveryVerdict.NOT_RECOVERED.value)
        # verified config restore does NOT latch
        self.assertFalse(c._is_latched())
        # rollback_result carries BOTH verdicts + exposure timing
        rr = cyc["rollback_result"]
        self.assertIn("tau_exposure_to_restore", rr)
        self.assertEqual(rr["physical_recovery_verdict"],
                         PhysicalRecoveryVerdict.NOT_RECOVERED.value)

    def test_unknown_recovery_is_not_commit(self):
        # recovery never observable -> UNKNOWN, and a hard failure never commits
        order = []
        c = _txn_coordinator("blind", True, order)
        result = c.process_intent("throughput >= 4 Mbps")
        cyc = result["cycles"][-1]
        self.assertEqual(cyc["physical_recovery_verdict"],
                         PhysicalRecoveryVerdict.UNKNOWN.value)
        self.assertNotIn(result["terminal_outcome"],
                         {TerminalOutcome.COMMIT_ORIGINAL.value,
                          TerminalOutcome.COMMIT_REVISED.value})
        self.assertFalse(result["success"])

    def test_hard_failure_with_restore_failure_latches(self):
        # config restore FAILS during the hard-failure rollback -> latch
        order = []
        c = _txn_coordinator("stuck", False, order)
        result = c.process_intent("throughput >= 4 Mbps")
        self.assertTrue(c._is_latched())
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)


if __name__ == "__main__":
    unittest.main()
