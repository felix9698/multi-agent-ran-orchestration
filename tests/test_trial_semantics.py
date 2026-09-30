"""P2: trial outcome vs episode resolution semantics.

Regression: an apply-failed trial (S3 -> S5, no S4 pass) whose negotiation was
auto-accepted produced result["success"]=True, so the failed trial was counted
as a SUCCESS by ECE and estimate_costs (polluting R_success / C_cont).
"""

import tempfile
import unittest

from coordinator.intent_coordinator import IntentCoordinator, IntentManager
from decision.intent_model import (
    Alternative, ConstraintType, FeasibilityPrediction, Intent, IntentScope,
    IntentStatus, IntentTarget, IntentType, NetworkState,
)
from experiments.metrics import (
    EpisodeRecord, IntentConfig, estimate_costs, expected_calibration_error,
)
from experiments.runner import ExperimentRunner


class _FakeIntentManager:
    def __init__(self):
        self.added = []

    def get_active(self):
        return []

    def get_monitored(self):
        return []

    def add(self, intent):
        self.added.append(intent)


class _FakeCalibrator:
    def __init__(self, theta=0.5, n_max=2):
        self.theta = theta
        self.n_max = n_max
        self.recorded = []

    def get_theta_star(self):
        return self.theta

    def can_negotiate_more(self, rounds, phase=None):
        return rounds < self.n_max

    def record_episode(self, metrics):
        self.recorded.append(metrics)


def _make_intent() -> Intent:
    return Intent(type=IntentType.THROUGHPUT_GOAL,
                  target=IntentTarget(kpi_name="throughput",
                                      constraint_type=ConstraintType.MIN,
                                      target_value=8.0, unit="Mbps"))


def _make_coordinator(states):
    """Bare IntentCoordinator wired with only what process_intent touches.

    __new__ skips __init__ so no executor/collector/LLM hardware is built;
    the S0-S6 control flow, _negotiate and result assembly stay REAL.
    """
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.gui = None
    c.on_negotiation_needed = None
    c.on_state_change = lambda old, new: states.append(new)
    c.current_state = "S0"
    c.history_reservoir = []
    c.max_history = 100
    c.tau_trial_s = 0.0
    c.intent_manager = _FakeIntentManager()
    c.calibrator = _FakeCalibrator()
    c.negotiation_policy = None            # default auto-accept resolution
    c.generate_alternatives_fn = lambda intent, rejected: []

    intent = _make_intent()
    alt = Alternative(id="a1", description="relax I2 to 6 Mbps")
    c._parse_intent = lambda text: {"intent": intent, "raw": {}}
    c._check_conflicts = lambda new, active: True   # skip S1 shortcut
    c._get_network_state = lambda: NetworkState(ue_states={})
    c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
        feasible=True, confidence=0.9, reasoning="", alternatives=[alt])
    return c


def _ep(**kw) -> EpisodeRecord:
    base = dict(trial_id=0, method="m", phase="P1")
    base.update(kw)
    return EpisodeRecord(**base)


class ProcessIntentTrialSuccessTest(unittest.TestCase):
    """Drive the real process_intent through S3/S4/S5 and check the C1
    semantics: agreement alone never resolves an episode - only an
    S4-validated (re-)execution does."""

    def test_apply_failure_then_bare_agreement_rejects(self):
        # S3 apply fails (no S4) -> S5 auto-accepts a BARE alternative (no
        # modified intent): the agreement cannot be executed and
        # re-verified, so the episode is REJECTED (pre-C1 it resolved
        # success=True on the agreement alone).
        states = []
        c = _make_coordinator(states)
        c._execute_trial = lambda feas: {"success": False, "snapshot": {},
                                         "clipped": [], "applied": []}
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertIs(result["trial_success"], False)
        self.assertFalse(result["success"])
        self.assertEqual(result["resolution"], "reject")
        self.assertTrue(result["agreement"]["accepted"])   # agreement != success
        self.assertIn("S3", states)
        self.assertNotIn("S4", states)   # apply failure skips validation
        self.assertIn("S5", states)
        self.assertEqual(result["nego_stats"]["terminated_by"],
                         "unmaterializable")

    def test_validation_failure_then_bare_agreement_rejects(self):
        # S4 validation fails -> rollback -> S5 auto-accept of a bare
        # alternative -> reject (no re-executable modified intent).
        states = []
        c = _make_coordinator(states)
        c._execute_trial = lambda feas: {"success": True, "snapshot": {},
                                         "clipped": [], "applied": []}
        c._validate_trial = lambda ni, ai: {"all_satisfied": False,
                                            "metrics": {}}
        c._rollback = lambda snapshot: None
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertIs(result["trial_success"], False)
        self.assertFalse(result["success"])
        self.assertEqual(result["resolution"], "reject")
        self.assertTrue(result["rolled_back"])
        self.assertEqual(states.count("S4"), 1)
        self.assertEqual(result["nego_stats"]["terminated_by"],
                         "unmaterializable")
        self.assertEqual(result["nego_stats"]["rounds"], 1)

    def test_validation_pass_commits_trial_success_true(self):
        states = []
        c = _make_coordinator(states)
        c._execute_trial = lambda feas: {"success": True, "snapshot": {},
                                         "clipped": [], "applied": []}
        c._validate_trial = lambda ni, ai: {
            "all_satisfied": True, "metrics": {},
            "monitor_verdicts": {getattr(i, "id", None): "satisfied"
                                 for i in list(ai) + [ni]}}
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertIs(result["trial_success"], True)
        self.assertTrue(result["success"])
        self.assertEqual(result["resolution"], "accept")
        self.assertEqual(len(result["cycles"]), 1)

    def test_accepted_alternative_is_reexecuted_and_validated(self):
        # C1 counter-example #1/#2: an accepted MATERIALIZABLE alternative
        # triggers a SECOND actuation + S4 validation; only when that
        # passes does the episode resolve (accept_modified + success).
        states = []
        c = _make_coordinator(states)
        relaxed = Intent(type=IntentType.THROUGHPUT_GOAL,
                         target=IntentTarget(kpi_name="throughput",
                                             constraint_type=ConstraintType.MIN,
                                             target_value=6.0, unit="Mbps"))
        alt = Alternative(id="a6", description="relax to 6",
                          modified_intent=relaxed)
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=0.9, reasoning="", alternatives=[alt])
        executions = []
        c._execute_trial = lambda feas: executions.append(1) or {
            "success": True, "snapshot": {}, "clipped": [], "applied": []}
        validations = []

        def _validate(ni, ai):
            validations.append(ni.target.target_value)
            # first (8.0) fails, the relaxed re-execution (6.0) passes
            ok = len(validations) > 1
            return {"all_satisfied": ok, "metrics": {},
                    "monitor_verdicts": ({getattr(i, "id", None): "satisfied"
                                          for i in list(ai) + [ni]} if ok
                                         else {})}

        c._validate_trial = _validate
        c._rollback = lambda snapshot: None
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertTrue(result["success"])
        self.assertEqual(result["resolution"], "accept_modified")
        self.assertEqual(len(executions), 2)          # second actuation ran
        self.assertEqual(validations, [8.0, 6.0])     # S4 on BOTH intents
        self.assertEqual(len(result["cycles"]), 2)
        self.assertIs(result["cycles"][0]["trial_success"], False)
        self.assertIs(result["cycles"][1]["trial_success"], True)
        self.assertIs(result["trial_success"], True)  # last-cycle mirror
        # the RELAXED intent is what got committed
        self.assertEqual(c.intent_manager.added[-1].target.target_value, 6.0)
        # exactly one calibration record per cycle (A8)
        self.assertEqual(len(c.calibrator.recorded), 2)
        self.assertFalse(c.calibrator.recorded[0].episode_success)
        self.assertTrue(c.calibrator.recorded[1].episode_success)

    def test_agreement_never_resolves_without_passing_trial(self):
        # Verification-report counter-example #1: an accepted alternative
        # must NEVER produce success without a PASSING second trial + S4.
        # Here every (re-)execution fails validation, so agreements keep
        # forming but the episode ends REJECTED once N_max cycles are spent.
        states = []
        c = _make_coordinator(states)
        c.calibrator = _FakeCalibrator(n_max=1)
        relaxed = Intent(type=IntentType.THROUGHPUT_GOAL,
                         target=IntentTarget(kpi_name="throughput",
                                             constraint_type=ConstraintType.MIN,
                                             target_value=6.0, unit="Mbps"))
        alt = Alternative(id="a6", description="relax to 6",
                          modified_intent=relaxed)
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=0.9, reasoning="", alternatives=[alt])
        executions = []
        c._execute_trial = lambda feas: executions.append(1) or {
            "success": True, "snapshot": {}, "clipped": [], "applied": []}
        c._validate_trial = lambda ni, ai: {"all_satisfied": False,
                                            "metrics": {}}
        c._rollback = lambda snapshot: None
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertTrue(result["agreement"]["accepted"])
        self.assertFalse(result["success"])
        self.assertEqual(result["resolution"], "reject")
        # cycle 0 + one re-execution (N_max=1), both failed validation
        self.assertEqual(len(executions), 2)
        self.assertEqual(len(result["cycles"]), 2)
        self.assertEqual(result["nego_stats"]["terminated_by"], "n_max")
        self.assertTrue(all(cyc["trial_success"] is False
                            for cyc in result["cycles"]))
        # N_max=0 shuts negotiation entirely: no agreement can even form
        c0 = _make_coordinator([])
        c0.calibrator = _FakeCalibrator(n_max=0)
        c0._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=0.9, reasoning="", alternatives=[alt])
        c0._execute_trial = lambda feas: {"success": True, "snapshot": {},
                                          "clipped": [], "applied": []}
        c0._validate_trial = lambda ni, ai: {"all_satisfied": False,
                                             "metrics": {}}
        c0._rollback = lambda snapshot: None
        r0 = c0.process_intent("throughput >= 8 Mbps")
        self.assertFalse(r0["agreement"]["accepted"])
        self.assertFalse(r0["success"])
        self.assertEqual(len(r0["cycles"]), 1)


class CycleCompositionTest(unittest.TestCase):
    """C1 x C7 composition holes found by the holistic review."""

    def test_relaxed_reexecution_excludes_registered_twin(self):
        # The runner registers I2 (8.0). A relaxed re-execution (6.0) must
        # NOT be validated against the original 8.0 twin it is replacing -
        # otherwise a relaxation could never validate below the original
        # target. Other registered intents (I1) stay in the context.
        states = []
        c = _make_coordinator(states)
        c.intent_manager = IntentManager()
        registered_i2 = _make_intent()                 # 8.0 Mbps twin
        registered_i2.status = IntentStatus.ACTIVE
        i1 = Intent(type=IntentType.POWER_CONSTRAINT,
                    target=IntentTarget(kpi_name="tx_power",
                                        constraint_type=ConstraintType.MAX,
                                        target_value=3.0, unit="dB"),
                    scope=IntentScope(bs_ids=["bs2"]))
        i1.status = IntentStatus.ACTIVE
        c.intent_manager.add(registered_i2)
        c.intent_manager.add(i1)

        relaxed = Intent(type=IntentType.THROUGHPUT_GOAL,
                         target=IntentTarget(kpi_name="throughput",
                                             constraint_type=ConstraintType.MIN,
                                             target_value=6.0, unit="Mbps"))
        alt = Alternative(id="a6", description="relax to 6",
                          modified_intent=relaxed)
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=0.9, reasoning="", alternatives=[alt])
        c._execute_trial = lambda feas: {"success": True, "snapshot": {},
                                         "clipped": [], "applied": []}
        seen_active = []

        def _validate(ni, ai):
            seen_active.append(
                (ni.target.target_value,
                 sorted(i.target.target_value for i in ai)))
            ok = len(seen_active) > 1
            return {"all_satisfied": ok, "metrics": {},
                    "monitor_verdicts": ({getattr(i, "id", None): "satisfied"
                                          for i in list(ai) + [ni]} if ok
                                         else {})}

        c._validate_trial = _validate
        c._rollback = lambda snapshot: None
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertTrue(result["success"])
        # both cycles validated against I1 (3.0) but NEVER the 8.0 twin
        self.assertEqual(seen_active, [(8.0, [3.0]), (6.0, [3.0])])

    def test_total_consultations_bounded_by_frozen_n_max(self):
        # Codex acceptance #3 counter-example: the previous double use of
        # N_max (outer cycle budget + independent per-negotiation round
        # budget) allowed O(N_max^2) total policy consultations. Now every
        # cycle's negotiation draws from ONE frozen episode budget: total
        # consultations across the whole episode <= N_max.
        states = []
        c = _make_coordinator(states)
        c.calibrator = _FakeCalibrator(n_max=3)
        relaxed = Intent(type=IntentType.THROUGHPUT_GOAL,
                         target=IntentTarget(kpi_name="throughput",
                                             constraint_type=ConstraintType.MIN,
                                             target_value=6.0, unit="Mbps"))
        alt = Alternative(id="a6", description="relax to 6",
                          modified_intent=relaxed)
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=0.9, reasoning="", alternatives=[alt])
        c.generate_alternatives_fn = lambda i, r: [alt]
        consultations = {"n": 0}

        def _policy(a):
            consultations["n"] += 1
            # reject, accept, reject, accept, ... - each accept re-enters
            return "accept" if consultations["n"] % 2 == 0 else "reject"

        c.negotiation_policy = _policy
        c._execute_trial = lambda feas: {"success": True, "snapshot": {},
                                         "clipped": [], "applied": []}
        c._validate_trial = lambda ni, ai: {"all_satisfied": False,
                                            "metrics": {}}
        c._rollback = lambda snapshot: None
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertFalse(result["success"])
        # cycle0 consumed 2 rounds (reject+accept), cycle1 consumed the
        # last one (reject) -> exactly N_max consultations, never 3 * 3
        self.assertEqual(consultations["n"], 3)
        self.assertEqual(
            sum(cyc["nego_stats"]["rounds"] for cyc in result["cycles"]
                if cyc.get("nego_stats")), 3)
        self.assertEqual(result["nego_stats"]["terminated_by"], "n_max")

    def test_cycle_budget_frozen_at_episode_start(self):
        # Per-cycle recalibration must not inflate N_max mid-episode: the
        # cycle budget is a per-episode quantity (Eq. 9) frozen at S0 -
        # without the freeze, emulated episodes ran past 100 cycles.
        class _InflatingCalibrator(_FakeCalibrator):
            def __init__(self):
                super().__init__(n_max=1)

            def record_episode(self, metrics):
                super().record_episode(metrics)
                self.n_max += 10      # adaptive recalibration inflates

        states = []
        c = _make_coordinator(states)
        c.calibrator = _InflatingCalibrator()
        relaxed = Intent(type=IntentType.THROUGHPUT_GOAL,
                         target=IntentTarget(kpi_name="throughput",
                                             constraint_type=ConstraintType.MIN,
                                             target_value=6.0, unit="Mbps"))
        alt = Alternative(id="a6", description="relax to 6",
                          modified_intent=relaxed)
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=0.9, reasoning="", alternatives=[alt])
        c._execute_trial = lambda feas: {"success": True, "snapshot": {},
                                         "clipped": [], "applied": []}
        c._validate_trial = lambda ni, ai: {"all_satisfied": False,
                                            "metrics": {}}
        c._rollback = lambda snapshot: None
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertFalse(result["success"])
        # frozen budget 1: cycle 0 + exactly one re-entry, despite the
        # calibrator advertising n_max=11, 21, ... mid-episode
        self.assertEqual(len(result["cycles"]), 2)
        self.assertEqual(result["nego_stats"]["terminated_by"], "n_max")
        self.assertGreater(c.calibrator.n_max, 1)   # it DID inflate


class TrialOutcomeSemanticsTest(unittest.TestCase):

    def test_failed_trial_with_auto_accept_nego_is_ece_failure(self):
        # Apply failure path: trial executed, negotiation auto-accepted the
        # first alternative => episode resolved (success=True) but the TRIAL
        # failed (trial_success=False). ECE must count it as a failure.
        e = _ep(confidence=0.9, trial_executed=True, entered_negotiation=True,
                success=True, trial_success=False)
        res = expected_calibration_error([e], n_bins=10)
        self.assertEqual(res["n"], 1)
        bin_ = next(b for b in res["bins"] if b["count"] == 1)
        self.assertEqual(bin_["accuracy"], 0.0)
        self.assertAlmostEqual(res["ece"], 0.9, places=6)

    def test_estimate_costs_classifies_by_trial_outcome(self):
        # trial_success=False must land in the C_cont (failed) bucket even
        # though the episode resolved successfully via negotiation.
        failed = _ep(trial_executed=True, success=True, trial_success=False,
                     throughput_before=5.0, throughput_trial_min=2.0,
                     tau_trial=10.0)
        succ = _ep(trial_executed=True, success=True, trial_success=True,
                   throughput_before=5.0, throughput_after=9.0, tau_trial=10.0)
        cost = estimate_costs([failed, succ], IntentConfig())
        self.assertEqual(cost["samples"]["n_failed"], 1)
        self.assertEqual(cost["samples"]["n_success"], 1)
        # C_cont sample = (5.0 - 2.0) * 10.0; R_success = (9.0 - 5.0) * 10.0
        self.assertAlmostEqual(cost["c_cont"], 30.0)
        self.assertAlmostEqual(cost["r_success"], 40.0)

    def test_trial_outcome_legacy_fallback_when_none(self):
        # Pre-P2 records (trial_success=None) keep the old classification.
        self.assertTrue(_ep(trial_executed=True, success=True).trial_outcome())
        self.assertFalse(_ep(trial_executed=True, success=True,
                             rolled_back=True).trial_outcome())
        self.assertFalse(_ep(trial_executed=True, success=False).trial_outcome())

    def test_runner_passes_trial_success_through(self):
        trace = {"routed_to": "trial", "trial_executed": True,
                 "rolled_back": False, "entered_negotiation": True,
                 "sequence": ["S3", "S5", "S6"]}
        result = {"success": True, "trial_success": False,
                  "feasibility": {"confidence": 0.9, "feasible": True}}
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExperimentRunner(coordinator=None, output_dir=tmp)
            [ep] = runner._episode_from_result("m", 0, "P1", result, trace, 5.0)
        self.assertIs(ep.trial_success, False)
        self.assertTrue(ep.success)          # episode resolution preserved
        self.assertFalse(ep.trial_outcome())  # trial outcome is the S4 result

    def test_runner_leaves_trial_success_none_when_absent(self):
        trace = {"routed_to": "negotiation", "trial_executed": False,
                 "rolled_back": False, "entered_negotiation": True,
                 "sequence": ["S5", "S6"]}
        result = {"success": True, "feasibility": {}}
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExperimentRunner(coordinator=None, output_dir=tmp)
            [ep] = runner._episode_from_result("m", 0, "P1", result, trace, 5.0)
        self.assertIsNone(ep.trial_success)


if __name__ == "__main__":
    unittest.main()
