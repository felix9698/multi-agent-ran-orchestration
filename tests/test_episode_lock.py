"""Gate A [H1]: episode-level single-flight lock.

Verification-report counter-example: the executor had a per-command lock,
but nothing serialized the S0-S6 transaction - two overlapping
process_intent calls could interleave their snapshots, trials, rollbacks
and calibration records.
"""

import threading
import time
import unittest

from coordinator.intent_coordinator import IntentCoordinator, IntentManager
from decision.intent_model import (
    ConstraintType, FeasibilityPrediction, Intent, IntentTarget, IntentType,
    NetworkState,
)


class _FakeCalibrator:
    def get_theta_star(self, phase=None):
        return 0.5

    def can_negotiate_more(self, rounds, phase=None):
        return False

    def record_episode(self, metrics):
        pass


def _intent():
    return Intent(type=IntentType.THROUGHPUT_GOAL,
                  target=IntentTarget(kpi_name="throughput",
                                      constraint_type=ConstraintType.MIN,
                                      target_value=8.0, unit="Mbps"))


def _skeleton(events):
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.gui = None
    c.on_negotiation_needed = None
    c.on_intent_violated = None
    c.on_state_change = (
        lambda old, new: events.append((threading.get_ident(), new)))
    c.current_state = "S0"
    c.history_reservoir = []
    c.max_history = 100
    c.tau_trial_s = 0.0
    c.intent_manager = IntentManager()
    c.calibrator = _FakeCalibrator()
    c.negotiation_policy = lambda alt: "reject"
    c.generate_alternatives_fn = lambda i, r: []
    c._parse_intent = lambda text: {"intent": _intent(), "raw": {}}
    c._check_conflicts = lambda new, active: True

    def _slow_feasibility(i, a, s):
        time.sleep(0.05)   # widen the race window mid-episode
        return FeasibilityPrediction(feasible=False, confidence=0.0,
                                     reasoning="")

    c._get_network_state = lambda: NetworkState(ue_states={})
    c._analyze_feasibility = _slow_feasibility
    return c


class EpisodeLockTest(unittest.TestCase):

    def test_concurrent_episodes_are_serialized(self):
        events = []
        c = _skeleton(events)
        threads = [threading.Thread(target=c.process_intent,
                                    args=("throughput >= 8 Mbps",))
                   for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertTrue(all(not t.is_alive() for t in threads))
        # each episode's state transitions form ONE contiguous block: the
        # observed thread ids switch exactly once (2 runs), never interleave
        tids = [tid for tid, _state in events]
        runs = 1 + sum(1 for a, b in zip(tids, tids[1:]) if a != b)
        self.assertEqual(runs, 2, f"interleaved transitions: {events}")
        # both episodes completed a full S0 ... S0 pass
        per_thread = {}
        for tid, state in events:
            per_thread.setdefault(tid, []).append(state)
        for seq in per_thread.values():
            self.assertEqual(seq[0], "S0")
            self.assertEqual(seq[-1], "S0")
            self.assertIn("S5", seq)

    def test_lock_is_reentrant_for_lock_holders(self):
        # holding the lock externally (same thread, no episode in flight)
        # must not deadlock
        events = []
        c = _skeleton(events)
        c._episode_lock = threading.RLock()
        with c._episode_lock:
            result = c.process_intent("throughput >= 8 Mbps")
        self.assertIn("resolution", result)

    def test_recursive_episode_rejected_not_nested(self):
        # an in-episode callback calling BACK into process_intent must not
        # nest a full S0-S6 between the outer snapshot and rollback: the
        # recursive call is rejected explicitly (no deadlock, no nesting)
        events = []
        c = _skeleton(events)
        inner_results = []
        outer_calls = {"n": 0}

        def _reentrant_state_hook(old, new):
            events.append((threading.get_ident(), new))
            if new == "S5" and outer_calls["n"] == 0:
                outer_calls["n"] += 1
                inner_results.append(
                    c.process_intent("throughput >= 9 Mbps"))

        c.on_state_change = _reentrant_state_hook
        outer = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(len(inner_results), 1)
        self.assertFalse(inner_results[0]["success"])
        self.assertIn("re-entrant", inner_results[0]["error"])
        # the OUTER episode completed normally despite the rejected nest
        self.assertIn("resolution", outer)
        self.assertNotIn("re-entrant", outer.get("error", ""))


if __name__ == "__main__":
    unittest.main()
