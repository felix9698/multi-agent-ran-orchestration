"""P3: round-based negotiation loop - N_max enforcement, monotonic relaxation,
per-round KPI sampling and nego_stats reporting.
"""

import json
import unittest

from coordinator.intent_coordinator import (
    IntentCoordinator, IntentManager, auto_accept_first, make_scripted_policy,
)
from coordinator.fsm import UnknownSystemEvent
from decision.intent_model import (
    Alternative, ConstraintType, FeasibilityPrediction, Intent, IntentScope,
    IntentStatus, IntentTarget, IntentType, NetworkState,
)


class _FakeIntentManager:
    def get_active(self):
        return []

    def get_monitored(self):
        return []

    def add(self, intent):
        pass


class _FakeCalibrator:
    def __init__(self, n_max=2, theta=0.5):
        self.n_max = n_max
        self.theta = theta
        self.recorded = []

    def get_theta_star(self):
        return self.theta

    def can_negotiate_more(self, rounds, phase=None):
        return rounds < self.n_max

    def record_episode(self, metrics):
        self.recorded.append(metrics)


class _Metric:
    def __init__(self, tput):
        self.throughput_mbps = tput
        self.attached = True


class _FakeCollector:
    def __init__(self, tput=5.5):
        self.tput = tput
        self.calls = 0        # counts the KPI probe (get_throughput_all)

    def collect_all(self):
        return {"ue1": _Metric(self.tput)}

    def get_throughput_all(self, duration=2.0):
        # Batch F: the negotiation KPI now comes from the LIVE probe, not
        # collect_all - this is the per-round fresh structured measurement.
        self.calls += 1
        return {"ue1": self.tput}


def _intent(target=8.0) -> Intent:
    return Intent(type=IntentType.THROUGHPUT_GOAL,
                  target=IntentTarget(kpi_name="throughput",
                                      constraint_type=ConstraintType.MIN,
                                      target_value=target, unit="Mbps"))


def _alt(aid, target) -> Alternative:
    return Alternative(id=aid, description=f"relax to {target}",
                       modified_intent=_intent(target))


def _make_coordinator(n_max=2):
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.gui = None
    c.on_state_change = None
    c.on_negotiation_needed = None
    c.current_state = "S0"
    c.history_reservoir = []
    c.max_history = 100
    c.tau_trial_s = 0.0
    c.intent_manager = _FakeIntentManager()
    c.calibrator = _FakeCalibrator(n_max=n_max)
    c.ue_collector = _FakeCollector()
    c.negotiation_policy = auto_accept_first
    c.generate_alternatives_fn = None
    return c


class NegotiationLoopTest(unittest.TestCase):

    def test_n_max_enforced_with_always_reject_policy(self):
        # Acceptance (a): N_max=2 + always-reject => exactly 2 rounds,
        # terminated_by == "n_max", no candidate accepted.
        c = _make_coordinator(n_max=2)
        c.negotiation_policy = lambda alt: "reject"
        gen_targets = iter([6.0, 5.0, 4.0])
        c.generate_alternatives_fn = (
            lambda intent, rejected: [_alt(f"g{len(rejected)}",
                                           next(gen_targets))])
        res = c._negotiate(_intent(), [_alt("a1", 7.0)])
        self.assertIsNone(res["accepted"])
        self.assertEqual(res["stats"]["rounds"], 2)
        self.assertEqual(res["stats"]["terminated_by"], "n_max")

    def test_monotonic_violation_discarded_then_regenerated(self):
        # Acceptance (b): round-2 candidate relaxes LESS than round-1
        # (7 > 6 on a MIN constraint) => discarded + one regeneration.
        c = _make_coordinator(n_max=5)
        c.negotiation_policy = make_scripted_policy(["reject", "accept"])
        calls = []
        supply = [[_alt("bad", 7.0)], [_alt("good", 5.0)]]

        def gen(intent, rejected):
            calls.append([a.id for a in rejected])
            return supply.pop(0) if supply else []

        c.generate_alternatives_fn = gen
        res = c._negotiate(_intent(), [_alt("a1", 6.0)])
        self.assertEqual(res["accepted"].id, "good")
        self.assertEqual(res["stats"]["terminated_by"], "accept")
        self.assertEqual(res["stats"]["rounds"], 2)
        self.assertEqual(len(calls), 2)   # round-2 gen + in-round regeneration
        self.assertIn("bad", calls[1])    # discarded alt fed back to generator

    def test_monotonic_reviolation_terminates_reject(self):
        c = _make_coordinator(n_max=5)
        c.negotiation_policy = lambda alt: "reject"
        supply = [[_alt("bad1", 7.0)], [_alt("bad2", 9.0)]]
        c.generate_alternatives_fn = (
            lambda i, r: supply.pop(0) if supply else [])
        res = c._negotiate(_intent(), [_alt("a1", 6.0)])
        self.assertIsNone(res["accepted"])
        self.assertEqual(res["stats"]["terminated_by"], "reject")
        # rounds = policy consultations only (the aborted round is excluded),
        # but the aborted round still contributed a KPI sample
        self.assertEqual(res["stats"]["rounds"], 1)
        self.assertEqual(c.ue_collector.calls, 2)
        self.assertAlmostEqual(res["stats"]["tput_during_nego"], 5.5)

    def test_targetless_candidate_fails_guard_once_anchored(self):
        # Safety property: after a numeric anchor exists, an alternative
        # WITHOUT target_value is unverifiable and must be treated as a
        # violation (discard + regenerate; second violation terminates).
        c = _make_coordinator(n_max=5)
        c.negotiation_policy = lambda alt: "reject"
        bare1 = Alternative(id="bare1", description="different means")
        bare2 = Alternative(id="bare2", description="different means 2")
        supply = [[bare1], [bare2]]
        c.generate_alternatives_fn = (
            lambda i, r: supply.pop(0) if supply else [])
        res = c._negotiate(_intent(), [_alt("a1", 6.0)])
        self.assertIsNone(res["accepted"])
        self.assertEqual(res["stats"]["terminated_by"], "reject")

    def test_targetless_candidate_passes_without_anchor(self):
        # Round 1 has no anchor: a "different means" alternative with no
        # numeric target is still negotiable AT THE SELECTION LEVEL (the
        # coordinator later fail-closes it as unmaterializable - C1).
        c = _make_coordinator(n_max=3)
        bare = Alternative(id="bare", description="reallocate PRB instead")
        res = c._negotiate(_intent(), [bare])
        self.assertEqual(res["accepted"].id, "bare")
        self.assertEqual(res["stats"]["terminated_by"], "accept")

    def test_empty_first_round_falls_through_to_generator(self):
        # Spec pseudocode: alts = (round 1: feasibility.alternatives) OR
        # generate(...) - an empty initial list is generated over, not an
        # immediate reject.
        c = _make_coordinator(n_max=3)
        gen_calls = []

        def gen(intent, rejected):
            gen_calls.append(list(rejected))
            return [_alt("g1", 6.0)]

        c.generate_alternatives_fn = gen
        res = c._negotiate(_intent(), [])
        self.assertEqual(res["accepted"].id, "g1")
        self.assertEqual(gen_calls, [[]])   # called in round 1, empty history

    def test_no_alternative_terminates(self):
        c = _make_coordinator(n_max=5)
        c.negotiation_policy = lambda alt: "reject"
        c.generate_alternatives_fn = lambda i, r: []
        res = c._negotiate(_intent(), [_alt("a1", 6.0)])
        self.assertEqual(res["stats"]["rounds"], 1)
        self.assertEqual(res["stats"]["terminated_by"], "no_alternative")

    def test_auto_accept_first_selects_first_alternative(self):
        # C1 contract: acceptance is a SELECTION (agreement), not episode
        # success - the coordinator must re-execute the modified intent.
        c = _make_coordinator(n_max=3)
        res = c._negotiate(_intent(), [_alt("a1", 6.0)])
        self.assertEqual(res["accepted"].id, "a1")
        self.assertNotIn("success", res)   # selection carries no resolution
        self.assertEqual(res["stats"]["rounds"], 1)
        self.assertEqual(res["stats"]["terminated_by"], "accept")
        self.assertAlmostEqual(res["stats"]["tput_during_nego"], 5.5)

    def test_callback_takes_precedence_over_policy(self):
        c = _make_coordinator(n_max=3)
        c.negotiation_policy = lambda alt: "accept"   # would end at round 1
        seen = []

        def cb(alts):
            seen.append(alts[0].id)
            # Batch B fail-closed callback contract: only an explicit "accept"
            # accepts; the second round returns a RECOGNIZED accept value.
            return "reject" if len(seen) == 1 else "accept"

        c.on_negotiation_needed = cb
        c.generate_alternatives_fn = lambda i, r: [_alt("g2", 5.0)]
        res = c._negotiate(_intent(), [_alt("a1", 6.0)])
        self.assertEqual(seen, ["a1", "g2"])
        self.assertEqual(res["accepted"].id, "g2")
        self.assertEqual(res["stats"]["rounds"], 2)

    def test_unknown_callback_response_is_unknown_system_event(self):
        # Batch C (coordinator review): an UNRECOGNIZED callback response ("ok")
        # is a broken system contract - it raises UnknownSystemEvent (the
        # episode finalizes TechnicalFailsafe with no actuation), NOT a silent
        # downgrade to reject.
        c = _make_coordinator(n_max=1)
        seen = []

        def cb(alts):
            seen.append(alts[0].id)
            return "ok"                       # unknown -> UnknownSystemEvent

        c.on_negotiation_needed = cb
        with self.assertRaises(UnknownSystemEvent):
            c._negotiate(_intent(), [_alt("a1", 6.0)])
        self.assertEqual(seen, ["a1"])        # the callback WAS consulted

    def test_none_callback_response_is_unknown_system_event(self):
        c = _make_coordinator(n_max=1)
        c.on_negotiation_needed = lambda alts: None
        with self.assertRaises(UnknownSystemEvent):
            c._negotiate(_intent(), [_alt("a1", 6.0)])

    def test_empty_alternatives_without_generator_rejects(self):
        # No alternatives and the LLM-backed generator can't run (no
        # llm_manager wired) -> graceful no_alternative reject.
        c = _make_coordinator(n_max=3)
        res = c._negotiate(_intent(), [])
        self.assertIsNone(res["accepted"])
        self.assertEqual(res["stats"]["terminated_by"], "no_alternative")

    def test_scripted_policy_exhaustion_rejects(self):
        pol = make_scripted_policy(["accept"])
        self.assertEqual(pol(None), "accept")
        self.assertEqual(pol(None), "reject")
        self.assertEqual(pol(None), "reject")


class AlternativesFromJsonTest(unittest.TestCase):
    """The real LLM paths must produce COMPARABLE alternatives (with
    modified_intent) or the monotonic rule passes vacuously."""

    def test_target_value_becomes_modified_intent(self):
        base = _intent(8.0)
        alts = IntentCoordinator._alternatives_from_json(
            {"alternatives": [
                {"id": "a1", "description": "relax", "target_value": 6.0},
                {"id": "a2", "description": "other means"}]}, base)
        self.assertEqual(alts[0].modified_intent.target.target_value, 6.0)
        self.assertEqual(base.target.target_value, 8.0)   # base untouched
        self.assertIsNone(alts[1].modified_intent)

    def test_generate_alternatives_excludes_twin_lineage(self):
        # C1 x C7: the S5 REGENERATION rounds must use the same
        # twin-excluded context as the cycle - reloading all monitored
        # intents would hand the LLM the original target as a constraint
        # to preserve, making multi-round relaxation futile.
        c = _make_coordinator(n_max=3)
        c.intent_manager = IntentManager()
        twin = _intent(8.0)
        twin.status = IntentStatus.ACTIVE
        i1 = Intent(type=IntentType.POWER_CONSTRAINT,
                    target=IntentTarget(kpi_name="tx_power",
                                        constraint_type=ConstraintType.MAX,
                                        target_value=3.0, unit="dB"),
                    scope=IntentScope(bs_ids=["bs2"]))
        i1.status = IntentStatus.ACTIVE
        c.intent_manager.add(twin)
        c.intent_manager.add(i1)
        c._get_network_state = lambda: NetworkState(ue_states={})

        class _Resp:
            success = True
            parsed_json = {"alternatives": [
                {"id": "g1", "description": "relax", "target_value": 6.0}]}

        class _LLM:
            def generate_alternatives(self, active, failed, state):
                self.active = active
                return _Resp()

        c.llm_manager = _LLM()
        c.generate_alternatives_fn = None
        c._generate_alternatives(_intent(8.0), [])
        targets = [a["target"]["target_value"] for a in c.llm_manager.active]
        self.assertIn(3.0, targets)        # I1 preserved in the context
        self.assertNotIn(8.0, targets)     # the twin lineage excluded

    def test_llm_generate_path_produces_comparable_alternatives(self):
        c = _make_coordinator(n_max=3)
        c._get_network_state = lambda: NetworkState(ue_states={})

        # complete alternative envelope (id/description/target_value/confidence)
        # required by the strict S5 dynamic-alternatives schema.
        _obj = {"alternatives": [{"id": "g1", "description": "relax",
                                  "target_value": 6.5, "confidence": 0.8}]}

        class _Resp:
            success = True
            content = json.dumps(_obj)
            parsed_json = _obj

        class _LLM:
            def generate_alternatives(self, active, failed, state):
                self.failed = failed
                return _Resp()

        c.llm_manager = _LLM()
        alts = c._generate_alternatives(_intent(8.0), [_alt("r1", 7.0)])
        self.assertEqual(alts[0].modified_intent.target.target_value, 6.5)
        # rejected history (with targets) is surfaced to the LLM
        rejected = c.llm_manager.failed["rejected_alternatives"]
        self.assertEqual(rejected[0]["id"], "r1")
        self.assertEqual(rejected[0]["target_value"], 7.0)


class NegoStatsInProcessIntentTest(unittest.TestCase):

    def test_low_confidence_route_cycles_until_n_max(self):
        # C1: a low-confidence route auto-accepts a materializable
        # alternative -> the coordinator RE-ENTERS S2 with the relaxed
        # intent (a new coordination cycle) instead of declaring success.
        # With confidence stuck below theta*, the cycle budget (N_max=3)
        # exhausts and the episode is REJECTED - agreement without
        # execution success never resolves an episode.
        states = []
        c = _make_coordinator(n_max=3)
        c.on_state_change = lambda old, new: states.append(new)
        intent = _intent()
        alt = _alt("a1", 6.0)
        c._parse_intent = lambda text: {"intent": intent, "raw": {}}
        c._check_conflicts = lambda new, active: True
        c._get_network_state = lambda: NetworkState(ue_states={})
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=0.3, reasoning="", alternatives=[alt])
        result = c.process_intent("throughput >= 8 Mbps")
        # agreement recorded, but no execution success
        self.assertTrue(result["agreement"]["accepted"])
        self.assertEqual(result["agreement"]["alternative_id"], "a1")
        self.assertAlmostEqual(result["agreement"]["target_value"], 6.0)
        self.assertFalse(result["success"])
        self.assertEqual(result["resolution"], "reject")
        # cycle 0 + N_max re-entries; the last cycle's negotiation gets the
        # exhausted budget (0 rounds) and terminates by "n_max"
        self.assertEqual(len(result["cycles"]), 4)
        self.assertIn("nego_stats", result)
        self.assertEqual(result["nego_stats"]["terminated_by"], "n_max")
        self.assertEqual(result["nego_stats"]["rounds"], 0)
        # total consultations across the episode == N_max (frozen budget),
        # and every CONSULTED round sampled the 5.5 Mbps KPI
        consulted = [cyc["nego_stats"] for cyc in result["cycles"]
                     if cyc["nego_stats"]["rounds"] > 0]
        self.assertEqual(sum(n["rounds"] for n in consulted), 3)
        for n in consulted:
            self.assertAlmostEqual(n["tput_during_nego"], 5.5)
        self.assertIn("S5", states)
        self.assertNotIn("S3", states)
        # exactly one calibration record per cycle (A8)
        self.assertEqual(len(c.calibrator.recorded), 4)
        self.assertTrue(all(m.negotiation_entered
                            for m in c.calibrator.recorded))
        self.assertTrue(all(not m.episode_success
                            for m in c.calibrator.recorded))


if __name__ == "__main__":
    unittest.main()
