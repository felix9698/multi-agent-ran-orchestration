"""P0-9: the authoritative joint intent-set evaluation + scoped throughput
fairness.

Covers:
 * evaluate_joint_intent_set returns typed per-intent verdicts + a joint flag
   that is SATISFIED only when EVERY member is SATISFIED (a single VIOLATED or
   UNKNOWN blocks it), and deduplicates by intent identity (duplicate ids /
   the pending intent already in the monitored set count once; two distinct
   semantic aliases are both evaluated).
 * the no-conflict/already-satisfied shortcut routes through the joint
   evaluation over ONE fresh observation bundle and never admits when a
   monitored/pending intent is VIOLATED/UNKNOWN.
 * THROUGHPUT_FAIRNESS honours an explicit scope.ue_ids: >= 2 scoped members,
   every scoped member present + attached + finite; unrelated UEs are ignored;
   a detached scoped member (even with a stale cached throughput) is not
   satisfied.
"""

import unittest

from coordinator.intent_coordinator import IntentCoordinator, IntentManager
from coordinator.episode_types import (
    MonitorVerdict, SafetyState, TerminalOutcome, TerminalReason,
)
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


def _tput_intent(target=8.0, status=IntentStatus.ACTIVE, ue_ids=None) -> Intent:
    i = Intent(type=IntentType.THROUGHPUT_GOAL,
               target=IntentTarget(kpi_name="throughput",
                                   constraint_type=ConstraintType.MIN,
                                   target_value=target, unit="Mbps"))
    i.status = status
    if ue_ids is not None:
        i.scope.ue_ids = list(ue_ids)
    return i


def _fairness_intent(variance=1.0, ue_ids=None,
                     status=IntentStatus.ACTIVE) -> Intent:
    i = Intent(type=IntentType.THROUGHPUT_FAIRNESS,
               target=IntentTarget(kpi_name="throughput_variance",
                                   constraint_type=ConstraintType.MAX,
                                   target_value=variance, unit="mbps2"))
    i.status = status
    if ue_ids is not None:
        i.scope.ue_ids = list(ue_ids)
    return i


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


# ---------------------------------------------------------------------------
# evaluate_joint_intent_set
# ---------------------------------------------------------------------------
class JointEvaluationTest(unittest.TestCase):

    def test_all_satisfied_is_jointly_satisfied(self):
        c = _skeleton()
        a = _tput_intent(target=8.0, ue_ids=["ue1"])
        b = _tput_intent(target=5.0, ue_ids=["ue2"])
        metrics = {"ue1": _Metric(9.0), "ue2": _Metric(6.0)}
        out = c.evaluate_joint_intent_set(a, [b], metrics)
        self.assertTrue(out["joint_satisfied"])
        self.assertEqual(out["verdicts"][a.id], "satisfied")
        self.assertEqual(out["verdicts"][b.id], "satisfied")

    def test_one_violated_blocks_joint(self):
        c = _skeleton()
        pend = _tput_intent(target=8.0, ue_ids=["ue1"])
        mon = _tput_intent(target=8.0, ue_ids=["ue2"])
        metrics = {"ue1": _Metric(9.0), "ue2": _Metric(3.0)}   # ue2 violated
        out = c.evaluate_joint_intent_set(pend, [mon], metrics)
        self.assertFalse(out["joint_satisfied"])
        self.assertTrue(out["any_violated"])
        self.assertEqual(out["verdicts"][mon.id], "violated")

    def test_one_unknown_blocks_joint(self):
        c = _skeleton()
        pend = _tput_intent(target=8.0, ue_ids=["ue1"])
        mon = _tput_intent(target=8.0, ue_ids=["ue2"])
        metrics = {"ue1": _Metric(9.0), "ue2": _Metric(None)}  # ue2 gap
        out = c.evaluate_joint_intent_set(pend, [mon], metrics)
        self.assertFalse(out["joint_satisfied"])
        self.assertTrue(out["any_unknown"])
        self.assertEqual(out["verdicts"][mon.id], "unknown")

    def test_empty_set_is_not_jointly_satisfied(self):
        c = _skeleton()
        self.assertFalse(
            c.evaluate_joint_intent_set(None, [], {})["joint_satisfied"])

    def test_duplicate_id_and_pending_in_monitored_counts_once(self):
        # adversarial: the pending intent is ALSO in the monitored set (same
        # object, same id) and the id is repeated - it must be evaluated once.
        c = _skeleton()
        pend = _tput_intent(target=8.0, ue_ids=["ue1"])
        metrics = {"ue1": _Metric(9.0)}
        out = c.evaluate_joint_intent_set(pend, [pend, pend], metrics)
        self.assertEqual(out["required_intent_ids"], (pend.id,))
        self.assertTrue(out["joint_satisfied"])

    def test_same_id_different_content_is_conflict_unknown(self):
        # review #1: the SAME id with DIFFERENT full semantic content is a
        # CONFLICT - explicit UNKNOWN + joint forced false; the differing
        # (monitored-first) pending intent is never silently dropped.
        c = _skeleton()
        mon = _tput_intent(target=8.0, ue_ids=["ue1"])
        pend = _tput_intent(target=99.0, ue_ids=["ue1"])   # same content shape
        pend.id = mon.id                                    # SAME id, differs
        metrics = {"ue1": _Metric(50.0)}                    # >=8 but <99
        out = c.evaluate_joint_intent_set(pend, [mon], metrics)
        self.assertIn(mon.id, out["conflict_ids"])
        self.assertEqual(out["verdicts"][mon.id], "unknown")
        self.assertFalse(out["joint_satisfied"])
        self.assertTrue(out["any_unknown"])

    def test_same_id_identical_clone_dedups_quietly(self):
        # an exact semantic CLONE with the same id is a true duplicate: one
        # entry, no conflict.
        c = _skeleton()
        mon = _tput_intent(target=8.0, ue_ids=["ue1"])
        clone = _tput_intent(target=8.0, ue_ids=["ue1"])
        clone.id = mon.id
        out = c.evaluate_joint_intent_set(clone, [mon], {"ue1": _Metric(9.0)})
        self.assertEqual(out["conflict_ids"], ())
        self.assertEqual(out["required_intent_ids"], (mon.id,))
        self.assertTrue(out["joint_satisfied"])

    def test_conflicting_pending_not_dropped_even_if_kept_entry_satisfied(self):
        # monitored-first: the kept (monitored) entry is satisfied, but the
        # differing same-id pending must still force a non-satisfied joint.
        c = _skeleton()
        mon = _tput_intent(target=5.0, ue_ids=["ue1"])     # satisfied at 9
        pend = _tput_intent(target=50.0, ue_ids=["ue1"])   # would be violated
        pend.id = mon.id
        out = c.evaluate_joint_intent_set(pend, [mon], {"ue1": _Metric(9.0)})
        self.assertFalse(out["joint_satisfied"])
        self.assertIn(mon.id, out["conflict_ids"])

    def test_semantic_alias_with_distinct_id_is_evaluated(self):
        # adversarial: two DISTINCT intents that are semantic aliases (same
        # content, different id) are BOTH evaluated; one violated blocks joint.
        c = _skeleton()
        pend = _tput_intent(target=8.0, ue_ids=["ue1"])
        alias = _tput_intent(target=8.0, ue_ids=["ue1"])   # different id
        self.assertNotEqual(pend.id, alias.id)
        metrics = {"ue1": _Metric(9.0)}
        out = c.evaluate_joint_intent_set(pend, [alias], metrics)
        self.assertEqual(len(out["required_intent_ids"]), 2)
        self.assertTrue(out["joint_satisfied"])
        # now make the shared UE violate: BOTH aliases go violated -> not joint
        out2 = c.evaluate_joint_intent_set(pend, [alias], {"ue1": _Metric(3.0)})
        self.assertFalse(out2["joint_satisfied"])
        self.assertEqual(out2["verdicts"][pend.id], "violated")
        self.assertEqual(out2["verdicts"][alias.id], "violated")


# ---------------------------------------------------------------------------
# scoped THROUGHPUT_FAIRNESS
# ---------------------------------------------------------------------------
class ScopedFairnessTest(unittest.TestCase):

    def _verdict(self, intent, metrics):
        return _skeleton()._evaluate_intent_verdict(intent, metrics)

    def test_scoped_fairness_ignores_unrelated_ue(self):
        # scope {ue1, ue2} both equal -> SATISFIED even though an UNRELATED ue3
        # has a wildly different throughput (it must not enter the fairness set).
        intent = _fairness_intent(variance=1.0, ue_ids=["ue1", "ue2"])
        metrics = {"ue1": _Metric(10.0), "ue2": _Metric(10.0),
                   "ue3": _Metric(0.0)}
        self.assertIs(self._verdict(intent, metrics), MonitorVerdict.SATISFIED)
        # sanity: WITHOUT a scope, ue3 widens the population and it violates
        unscoped = _fairness_intent(variance=1.0)
        self.assertIs(self._verdict(unscoped, metrics), MonitorVerdict.VIOLATED)

    def test_scoped_fairness_requires_all_members_attached(self):
        # a detached scoped member is not served -> VIOLATED (not satisfied)
        intent = _fairness_intent(variance=1.0, ue_ids=["ue1", "ue2"])
        metrics = {"ue1": _Metric(10.0),
                   "ue2": _Metric(10.0, attached=False)}
        self.assertIs(self._verdict(intent, metrics), MonitorVerdict.VIOLATED)

    def test_scoped_fairness_missing_member_is_unknown(self):
        # a scoped member entirely absent from the bundle -> UNKNOWN, never
        # positively satisfied on the remaining members.
        intent = _fairness_intent(variance=1.0, ue_ids=["ue1", "ue2"])
        self.assertIs(self._verdict(intent, {"ue1": _Metric(10.0)}),
                      MonitorVerdict.UNKNOWN)

    def test_scoped_fairness_needs_two_members(self):
        intent = _fairness_intent(variance=1.0, ue_ids=["ue1"])
        self.assertIs(self._verdict(intent, {"ue1": _Metric(10.0)}),
                      MonitorVerdict.UNKNOWN)

    def test_cached_detached_ue_cannot_satisfy_intent(self):
        # a detached scoped UE that still reports a (stale, merged) throughput
        # must NOT satisfy the fairness intent - attachment is checked first.
        intent = _fairness_intent(variance=1.0, ue_ids=["ue1", "ue2"])
        metrics = {"ue1": _Metric(10.0),
                   "ue2": _Metric(10.0, attached=False)}   # cached tput, detached
        self.assertIsNot(self._verdict(intent, metrics), MonitorVerdict.SATISFIED)
        # the same holds for a per-UE throughput goal (detached -> violated)
        goal = _tput_intent(target=5.0, ue_ids=["ue2"])
        self.assertIs(self._verdict(goal, metrics), MonitorVerdict.VIOLATED)

    def test_scoped_fairness_nonfinite_member_is_unknown(self):
        intent = _fairness_intent(variance=1.0, ue_ids=["ue1", "ue2"])
        metrics = {"ue1": _Metric(10.0), "ue2": _Metric(float("nan"))}
        self.assertIs(self._verdict(intent, metrics), MonitorVerdict.UNKNOWN)

    def test_repeated_scope_member_does_not_count_twice(self):
        # review #2: [ue1, ue1] is ONE UE, not two - it must not fake a >= 2
        # member fairness population (UNKNOWN, never satisfied on one UE).
        intent = _fairness_intent(variance=1.0, ue_ids=["ue1", "ue1"])
        self.assertIs(self._verdict(intent, {"ue1": _Metric(10.0)}),
                      MonitorVerdict.UNKNOWN)
        # even with a fully finite single UE it stays UNKNOWN (one unique member)
        self.assertIs(self._verdict(intent, {"ue1": _Metric(10.0),
                                             "ue2": _Metric(0.0)}),
                      MonitorVerdict.UNKNOWN)

    def test_deduped_scope_with_two_unique_members_evaluates(self):
        # [ue1, ue1, ue2] -> two UNIQUE members -> a normal fairness evaluation
        intent = _fairness_intent(variance=1.0, ue_ids=["ue1", "ue1", "ue2"])
        self.assertIs(self._verdict(intent, {"ue1": _Metric(10.0),
                                             "ue2": _Metric(10.0)}),
                      MonitorVerdict.SATISFIED)


# ---------------------------------------------------------------------------
# already-satisfied shortcut routes through joint evaluation
# ---------------------------------------------------------------------------
def _shortcut_coord(metrics, monitored=None):
    """A coordinator wired so the no-conflict path reaches the joint-evaluated
    already-satisfied shortcut, and (on fall-through) a clean infeasible
    terminal, with ONE observation bundle."""
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.gui = None
    c.on_state_change = None
    c.on_negotiation_needed = None
    c.on_intent_violated = None
    c.current_state = "S0"
    c.history_reservoir = []
    c.max_history = 100
    c.tau_trial_s = 0.0
    c.episode_budget_s = 30.0
    c.safety_state = SafetyState.READY
    c._safety_latch = None
    c.llm_manager = None
    c.intent_manager = IntentManager()
    for m in (monitored or []):
        c.intent_manager.add(m)

    class _Collector:
        simulation_mode = False

        def __init__(self):
            self.calls = 0

        def collect_all(self):
            self.calls += 1
            return {k: v for k, v in metrics.items()}

    c.ue_collector = _Collector()

    class _Cal:
        def get_theta_star(self, phase=None):
            return 0.5

        def can_negotiate_more(self, r, phase=None):
            return False

        def get_n_max(self):
            return 0

        def record_episode(self, m):
            pass
    c.calibrator = _Cal()
    c.negotiation_policy = lambda alt: "reject"
    c.generate_alternatives_fn = lambda i, r: []
    c._check_conflicts = lambda n, a: False        # no conflict -> shortcut
    c._get_network_state = lambda: NetworkState(ue_states={})
    c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
        feasible=False, confidence=0.0, reasoning="")
    c._record_episode_result = lambda r, f: None
    return c


class AlreadySatisfiedShortcutTest(unittest.TestCase):

    def test_joint_satisfied_shortcut_records_typed_evidence(self):
        pend = _tput_intent(target=8.0, ue_ids=["ue1"])
        c = _shortcut_coord({"ue1": _Metric(9.0)})
        result = c.resolve_pending_intent(pend)
        self.assertFalse(result["success"])
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.ALREADY_SATISFIED_UNVERIFIED.value)
        # exactly ONE observation collection for the shortcut
        self.assertEqual(c.ue_collector.calls, 1)
        # the typed joint verdicts are recorded as evidence
        self.assertTrue(result["joint_evaluation"]["joint_satisfied"])
        self.assertEqual(result["evidence"]["monitor_verdicts"][pend.id],
                         "satisfied")

    def test_shortcut_evidence_has_exactly_one_observation_no_fake_action(self):
        # review item A: the no-cycle shortcut records EXACTLY the one collect_all
        # bundle as one Observation whose deep-copied per-UE details equal the
        # collected data and whose timestamps bound the collection, with all
        # typed verdicts and NO fabricated action / read-back.
        pend = _tput_intent(target=8.0, ue_ids=["ue1"])
        c = _shortcut_coord({"ue1": _Metric(9.0),
                             "ue2": _Metric(4.0, attached=False, latency=12.0)})
        result = c.resolve_pending_intent(pend)
        self.assertEqual(c.ue_collector.calls, 1)             # ONE collector call
        ev = result["evidence"]
        self.assertEqual(len(ev["observations"]), 1)          # exactly one
        obs = ev["observations"][0]
        self.assertEqual(obs["source"], "joint_shortcut")
        # the bundle equals the EXACT collected per-UE data (attachment + KPIs)
        self.assertEqual(obs["details"], {
            "ue1": {"throughput_mbps": 9.0, "attached": True,
                    "latency_ms": None},
            "ue2": {"throughput_mbps": 4.0, "attached": False,
                    "latency_ms": 12.0}})
        # timestamps bound the collection call
        self.assertLessEqual(obs["collection_start"], obs["collection_end"])
        self.assertLessEqual(obs["collection_start"], obs["sample_time"])
        self.assertLessEqual(obs["sample_time"], obs["collection_end"])
        # all typed verdicts recorded
        self.assertEqual(ev["monitor_verdicts"][pend.id], "satisfied")
        # NO fabricated action / read-back / trial
        self.assertEqual(ev["canonical_action"], {})
        self.assertEqual(ev["clipped_action"], {})
        self.assertIsNone(ev["final_readback"])
        self.assertIsNone(ev["actuation_trial_id"])

    def test_shortcut_observation_bundle_is_deep_copied(self):
        # mutating the live metrics after collection must NOT change the recorded
        # evidence bundle (deep copy).
        pend = _tput_intent(target=8.0, ue_ids=["ue1"])
        live = {"ue1": _Metric(9.0)}

        class _Coll:
            simulation_mode = False

            def __init__(self):
                self.calls = 0

            def collect_all(self_):
                self_.calls += 1
                return live                      # returns the LIVE objects
        c = _shortcut_coord({"ue1": _Metric(9.0)})
        c.ue_collector = _Coll()
        result = c.resolve_pending_intent(pend)
        live["ue1"].throughput_mbps = -999.0     # mutate AFTER collection
        obs = result["evidence"]["observations"][0]
        self.assertEqual(obs["details"]["ue1"]["throughput_mbps"], 9.0)

    def test_caller_nested_ctx_mutation_does_not_corrupt_provenance(self):
        # review gap 1: the trigger context is DEEP-copied at episode entry, so a
        # caller mutating a NESTED object mid-episode (here during S1 conflict
        # screening, before the shortcut) cannot corrupt the recorded provenance.
        ctx = {"reentry": True, "reentry_reason": "assurance_drift",
               "meta": {"tags": ["a"]}}
        pend = _tput_intent(target=8.0, ue_ids=["ue1"])
        c = _shortcut_coord({"ue1": _Metric(9.0)})

        def _cc(new, active):
            ctx["meta"]["tags"].append("MUTATED")   # caller-owned nested mutation
            ctx["reentry_reason"] = "MUTATED"
            return False                              # no conflict -> shortcut
        c._check_conflicts = _cc
        result = c.resolve_pending_intent(pend, trigger_context=ctx)
        self.assertEqual(result["trigger_context"]["meta"]["tags"], ["a"])
        self.assertEqual(result["trigger_context"]["reentry_reason"],
                         "assurance_drift")
        self.assertEqual(result["evidence"]["trigger_context"]["meta"]["tags"],
                         ["a"])
        # coordinator-owned state is its own independent deep copy too
        self.assertEqual(c._active_trigger_context["meta"]["tags"], ["a"])

    def test_already_satisfied_requires_joint_monitored_satisfaction(self):
        # the pending intent is satisfied, but a MONITORED intent is VIOLATED
        # under the same bundle -> the shortcut must NOT admit "already
        # satisfied"; it falls through (and this infeasible episode ends
        # non-admitted with a DIFFERENT reason).
        pend = _tput_intent(target=8.0, ue_ids=["ue1"])
        guarded = _tput_intent(target=8.0, status=IntentStatus.SATISFIED,
                               ue_ids=["ue2"])
        c = _shortcut_coord({"ue1": _Metric(9.0), "ue2": _Metric(3.0)},
                            monitored=[guarded])
        result = c.resolve_pending_intent(pend)
        self.assertNotEqual(result["terminal_reason"],
                            TerminalReason.ALREADY_SATISFIED_UNVERIFIED.value)
        self.assertFalse(result["success"])
        # nothing was admitted to the monitored set for the pending intent
        self.assertNotIn(pend.id, c.intent_manager.intents)

    def test_shortcut_unknown_monitored_blocks_admission(self):
        pend = _tput_intent(target=8.0, ue_ids=["ue1"])
        guarded = _tput_intent(target=8.0, status=IntentStatus.SATISFIED,
                               ue_ids=["ue2"])
        c = _shortcut_coord({"ue1": _Metric(9.0), "ue2": _Metric(None)},
                            monitored=[guarded])
        result = c.resolve_pending_intent(pend)
        self.assertNotEqual(result["terminal_reason"],
                            TerminalReason.ALREADY_SATISFIED_UNVERIFIED.value)
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
