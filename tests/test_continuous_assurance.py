"""Gate A [C2]: continuous intent assurance and drift re-entry.

Verification-report counter-examples: a SATISFIED intent used to leave the
monitored set entirely (get_active returned ACTIVE only), so later drift
never re-entered coordination; and an ACTIVE intent's violation vanished
into `else: pass` with no status transition or trigger. Both are Critical
against the paper's execution-time-coordination / continuous-assurance
claims.
"""

import unittest

from coordinator.intent_coordinator import IntentCoordinator, IntentManager
from decision.intent_model import (
    ConstraintType, FeasibilityPrediction, Intent, IntentStatus, IntentTarget,
    IntentType, NetworkState,
)


class _Metric:
    def __init__(self, tput, attached=True, latency=None):
        self.throughput_mbps = tput
        self.attached = attached
        self.latency_ms = latency

    def to_dict(self):
        return {"throughput_mbps": self.throughput_mbps,
                "attached": self.attached, "latency_ms": self.latency_ms}


def _skeleton():
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.gui = None
    c.on_state_change = None
    c.on_negotiation_needed = None
    c.on_intent_violated = None
    c.current_state = "S0"
    c.history_reservoir = []
    c.max_history = 100
    c.tau_trial_s = 0.0
    c.intent_manager = IntentManager()
    return c


def _tput_intent(target=8.0, status=IntentStatus.ACTIVE) -> Intent:
    i = Intent(type=IntentType.THROUGHPUT_GOAL,
               target=IntentTarget(kpi_name="throughput",
                                   constraint_type=ConstraintType.MIN,
                                   target_value=target, unit="Mbps"))
    i.status = status
    return i


class MonitoredSetTest(unittest.TestCase):

    def test_statuses_monitored_and_withdrawn_excluded(self):
        # distinct scopes so same-(type, scope) replacement does not kick in
        mgr = IntentManager()
        for i, status in enumerate((IntentStatus.ACTIVE,
                                    IntentStatus.SATISFIED,
                                    IntentStatus.VIOLATED,
                                    IntentStatus.WITHDRAWN)):
            intent = _tput_intent(status=status)
            intent.scope.ue_ids = [f"ue{i}"]
            mgr.add(intent)
        self.assertEqual(len(mgr.get_monitored()), 3)
        self.assertEqual(len(mgr.get_active()), 1)   # unchanged semantics

    def test_add_replaces_same_type_and_scope(self):
        # C7: a committed episode (e.g. a negotiated relaxation) UPDATES
        # the operator's intent - no UUID-distinct duplicates accumulate
        # in the monitored set across a trial.
        mgr = IntentManager()
        original = _tput_intent(target=8.0, status=IntentStatus.ACTIVE)
        relaxed = _tput_intent(target=6.0, status=IntentStatus.ACTIVE)
        mgr.add(original)
        mgr.add(relaxed)
        monitored = mgr.get_monitored()
        self.assertEqual(len(monitored), 1)
        self.assertAlmostEqual(monitored[0].target.target_value, 6.0)
        self.assertIn(original, mgr.history)
        # a DIFFERENT scope is a different intent - both kept
        other_scope = _tput_intent(target=8.0, status=IntentStatus.ACTIVE)
        other_scope.scope.ue_ids = ["ue9"]
        mgr.add(other_scope)
        self.assertEqual(len(mgr.get_monitored()), 2)
        # scope identity is ORDER-insensitive and covers cells/area
        reordered = _tput_intent(target=5.0, status=IntentStatus.ACTIVE)
        reordered.scope.ue_ids = ["ue9"]
        mgr.add(reordered)                       # replaces other_scope
        self.assertEqual(len(mgr.get_monitored()), 2)
        multi_a = _tput_intent(target=7.0, status=IntentStatus.ACTIVE)
        multi_a.scope.ue_ids = ["ue1", "ue2"]
        multi_b = _tput_intent(target=6.5, status=IntentStatus.ACTIVE)
        multi_b.scope.ue_ids = ["ue2", "ue1"]    # same set, reordered
        mgr.add(multi_a)
        mgr.add(multi_b)
        targets = sorted(i.target.target_value
                         for i in mgr.get_monitored())
        self.assertEqual(len(mgr.get_monitored()), 3)
        self.assertIn(6.5, targets)              # multi_b replaced multi_a
        self.assertNotIn(7.0, targets)
        # a different cell list is a DIFFERENT intent
        celled = _tput_intent(target=4.0, status=IntentStatus.ACTIVE)
        celled.scope.ue_ids = ["ue2", "ue1"]
        celled.scope.cell_ids = [7]
        mgr.add(celled)
        self.assertEqual(len(mgr.get_monitored()), 4)


class DriftDetectionTest(unittest.TestCase):

    def test_satisfied_intent_drifts_to_violated_and_fires_hook(self):
        # Counter-example: SATISFIED -> KPI collapses -> VIOLATED + hook.
        c = _skeleton()
        intent = _tput_intent(status=IntentStatus.SATISFIED)
        c.intent_manager.add(intent)
        fired = []
        c.on_intent_violated = fired.append
        c._check_intent_satisfaction({"ue1": _Metric(3.0)})   # drifted below 8
        self.assertEqual(intent.status, IntentStatus.VIOLATED)
        self.assertEqual(fired, [intent])

    def test_active_intent_violation_is_not_swallowed(self):
        # Counter-example (Codex): ACTIVE violation used to hit `else: pass`.
        c = _skeleton()
        intent = _tput_intent(status=IntentStatus.ACTIVE)
        c.intent_manager.add(intent)
        c._check_intent_satisfaction({"ue1": _Metric(3.0)})
        self.assertEqual(intent.status, IntentStatus.VIOLATED)

    def test_hook_fires_once_per_transition(self):
        c = _skeleton()
        intent = _tput_intent(status=IntentStatus.SATISFIED)
        c.intent_manager.add(intent)
        fired = []
        c.on_intent_violated = fired.append
        c._check_intent_satisfaction({"ue1": _Metric(3.0)})
        c._check_intent_satisfaction({"ue1": _Metric(3.0)})   # still violated
        self.assertEqual(len(fired), 1)
        # recovery and a NEW drift fire again
        c._check_intent_satisfaction({"ue1": _Metric(9.0)})
        self.assertEqual(intent.status, IntentStatus.SATISFIED)
        c._check_intent_satisfaction({"ue1": _Metric(3.0)})
        self.assertEqual(len(fired), 2)

    def test_telemetry_gap_keeps_status(self):
        # unknown (no usable measurement) must not flap the status
        c = _skeleton()
        satisfied = _tput_intent(status=IntentStatus.SATISFIED)
        violated = _tput_intent(status=IntentStatus.VIOLATED)
        c.intent_manager.add(satisfied)
        c.intent_manager.add(violated)
        c._check_intent_satisfaction({"ue1": _Metric(None)})   # probe gap
        self.assertEqual(satisfied.status, IntentStatus.SATISFIED)
        self.assertEqual(violated.status, IntentStatus.VIOLATED)

    def test_detached_ue_is_violation_not_gap(self):
        # a detached UE is not served: that is a violation, not telemetry
        c = _skeleton()
        intent = _tput_intent(status=IntentStatus.SATISFIED)
        c.intent_manager.add(intent)
        c._check_intent_satisfaction({"ue1": _Metric(None, attached=False)})
        self.assertEqual(intent.status, IntentStatus.VIOLATED)

    def test_power_violation_outranks_non_finite_offset(self):
        # a NaN offset on one gNB must not mask a CONFIRMED out-of-bound
        # offset on another (violated outranks unknown)
        c = _skeleton()

        class _Ex:
            def get_all_offsets(self):
                return {"gnb1": float("nan"), "gnb2": 7.0}

        c.executor = _Ex()
        power = Intent(type=IntentType.POWER_CONSTRAINT,
                       target=IntentTarget(kpi_name="tx_power",
                                           constraint_type=ConstraintType.MAX,
                                           target_value=3.0))
        self.assertEqual(c._evaluate_intent_tristate(power, {}), "violated")
        self.assertFalse(c._evaluate_intent(power, {}))

    def test_non_finite_target_raises(self):
        # a NaN/Inf TARGET is a malformed intent: NaN comparisons are all
        # False and silently fail-opened (a NaN floor was never "violated")
        c = _skeleton()

        class _Ex:
            def get_all_offsets(self):
                return {"gnb1": 0.0}

        c.executor = _Ex()
        for value in (float("nan"), float("inf")):
            power = Intent(type=IntentType.POWER_CONSTRAINT,
                           target=IntentTarget(
                               kpi_name="tx_power",
                               constraint_type=ConstraintType.MAX,
                               target_value=value))
            with self.assertRaisesRegex(ValueError, "non-finite target"):
                c._evaluate_intent_tristate(power, {})
            tput = _tput_intent(target=value)
            with self.assertRaisesRegex(ValueError, "non-finite target"):
                c._evaluate_intent_tristate(tput, {"ue1": _Metric(9.0)})

    def test_violated_recovers_to_satisfied(self):
        c = _skeleton()
        intent = _tput_intent(status=IntentStatus.VIOLATED)
        c.intent_manager.add(intent)
        c._check_intent_satisfaction({"ue1": _Metric(9.0)})
        self.assertEqual(intent.status, IntentStatus.SATISFIED)

    def test_hook_exception_does_not_break_monitor(self):
        c = _skeleton()
        intent = _tput_intent(status=IntentStatus.SATISFIED)
        other = _tput_intent(status=IntentStatus.SATISFIED)
        other.scope.ue_ids = ["ue1"]   # distinct scope: no add-replacement
        c.intent_manager.add(intent)
        c.intent_manager.add(other)

        def _bad_hook(i):
            raise RuntimeError("hook crashed")

        c.on_intent_violated = _bad_hook
        c._check_intent_satisfaction({"ue1": _Metric(3.0)})   # must not raise
        self.assertEqual(intent.status, IntentStatus.VIOLATED)
        self.assertEqual(other.status, IntentStatus.VIOLATED)


class CoordinationContextTest(unittest.TestCase):
    """The monitored set - satisfied intents included - is what S1/S2/S4
    coordinate against (counter-example: satisfied intents used to vanish
    from the S4 validation set)."""

    def test_satisfied_intent_included_in_coordination_context(self):
        c = _skeleton()
        satisfied = _tput_intent(target=6.0, status=IntentStatus.SATISFIED)
        # a DIFFERENT intent (distinct scope): the same-(type, scope) twin
        # of the new request is excluded by design (C1 x C7 - the request
        # replaces its own lineage), other satisfied intents must stay
        satisfied.scope.ue_ids = ["ue2"]
        c.intent_manager.add(satisfied)
        new_intent = _tput_intent(target=8.0)

        class _Collector:
            simulation_mode = False

            def collect_all(self):
                return {"ue1": _Metric(5.0)}

        class _Cal:
            def get_theta_star(self, phase=None):
                return 0.5

            def can_negotiate_more(self, r, phase=None):
                return False

            def record_episode(self, m):
                pass

        seen_context = []
        c.ue_collector = _Collector()
        c.calibrator = _Cal()
        c.negotiation_policy = lambda alt: "reject"
        c.generate_alternatives_fn = lambda i, r: []
        c._parse_intent = lambda text: {"intent": new_intent, "raw": {}}
        c._check_conflicts = lambda n, a: True
        c._get_network_state = lambda: NetworkState(ue_states={})

        def _capture(intent, active, state):
            seen_context.append(list(active))
            return FeasibilityPrediction(feasible=False, confidence=0.0,
                                         reasoning="")

        c._analyze_feasibility = _capture
        c._record_episode_result = lambda r, f: None
        c.process_intent("throughput >= 8")
        self.assertEqual(seen_context, [[satisfied]])

    def test_validation_covers_satisfied_intents(self):
        # S4 evaluates the monitored set: a trial that satisfies the NEW
        # intent but breaks a SATISFIED one must not validate
        c = _skeleton()
        guarded = _tput_intent(target=6.0, status=IntentStatus.SATISFIED)
        new_intent = _tput_intent(target=8.0)
        v = c._build_validation(
            {"ue1": _Metric(9.0), "ue2": _Metric(4.0)},   # ue2 broke 6.0
            new_intent, [guarded], samples=[9.0], trajectory=[],
            hard_failure=False, reconnection_time=0.0)
        self.assertFalse(v["all_satisfied"])
        self.assertFalse(v["intent_results"][guarded.id])


if __name__ == "__main__":
    unittest.main()
