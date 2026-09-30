"""Batch C (P0-5): strict schema + finite-number validation at the model
boundaries.

Covers the four fail-OPEN counterexamples the pre-Batch-C parser allowed and
the requirement that the runner's schema metric use the ACTUAL validator
verdict:

  * a string boolean ("feasible":"false") must NOT route to a trial;
  * a NaN action value must be rejected before the executor;
  * an Infinity confidence must be rejected;
  * an unknown action key rejects the whole proposal (never silently dropped);
  * a duplicate JSON key is rejected (json.loads would keep only the last);
  * schema_valid on the episode record is the validator verdict, not merely
    "no top-level error".

Offline deterministic fakes only; no network, no hardware, no real sleeps.
"""

import hashlib
import json
import math
import unittest

from coordinator import schema as S
from coordinator.schema import SchemaError
from coordinator.intent_coordinator import IntentCoordinator
from decision.intent_model import (
    NetworkState,
    ConstraintType, FeasibilityPrediction, Intent, IntentTarget, IntentType,
)
from config import ActionSpaceConfig
from experiments.runner import ExperimentRunner  # noqa: F401  (import guard)


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------

class _Resp:
    def __init__(self, content, parsed=None, success=True):
        self.success = success
        self.content = content
        self.parsed_json = parsed
        self.model = "fake"


class _FeasibilityLLM:
    """Returns a fixed feasibility response regardless of arguments."""
    def __init__(self, content, parsed=None, success=True):
        self._resp = _Resp(content, parsed, success)

    def analyze_feasibility(self, active, new, state, history=None):
        return self._resp

    def build_feasibility_prompt_hash(self, active, new, state, history=None):
        # a real backend computes a full SHA-256 of the exact prompt; the double
        # returns a valid-shaped hash so _analyze_feasibility proceeds (the
        # coordinator no longer swallows a missing/failed prompt-hash call).
        return "prompt-sha256-" + "0" * 64


class _FakeExecutor:
    class _S:
        pass

    def __init__(self, gnbs=("gnb1",)):
        self.states = {g: self._S() for g in gnbs}


def _fb_coordinator(content, parsed=None, success=True, gnbs=("gnb1",)):
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.history_reservoir = []
    c.llm_manager = _FeasibilityLLM(content, parsed, success)
    c._cur_schema_valid = True
    c._cur_schema_reason = None
    c.executor = _FakeExecutor(gnbs)
    c.ue_serving_gnb = {}
    c.action_space = ActionSpaceConfig()
    c.gui = None
    return c


def _intent():
    return Intent(type=IntentType.THROUGHPUT_GOAL,
                  target=IntentTarget(kpi_name="throughput",
                                      constraint_type=ConstraintType.MIN,
                                      target_value=8.0, unit="Mbps"))


# --------------------------------------------------------------------------
# Pure schema module unit tests
# --------------------------------------------------------------------------

class StrictParseTest(unittest.TestCase):

    def test_nan_constant_is_rejected(self):
        with self.assertRaises(SchemaError):
            S.strict_loads('{"x": NaN}')

    def test_infinity_constant_is_rejected(self):
        with self.assertRaises(SchemaError):
            S.strict_loads('{"x": Infinity}')
        with self.assertRaises(SchemaError):
            S.strict_loads('{"x": -Infinity}')

    def test_duplicate_json_key_is_rejected(self):
        # json.loads would silently keep only the last value; strict rejects.
        with self.assertRaises(SchemaError):
            S.strict_loads('{"feasible": true, "feasible": false}')

    def test_require_json_bool_rejects_string(self):
        with self.assertRaises(SchemaError):
            S.require_json_bool("false", "feasible")
        with self.assertRaises(SchemaError):
            S.require_json_bool(0, "feasible")            # int is not a JSON bool
        self.assertTrue(S.require_json_bool(True, "feasible"))

    def test_require_finite_rejects_nan_inf_and_bool(self):
        for bad in (float("nan"), float("inf"), float("-inf"), True, "3"):
            with self.assertRaises(SchemaError):
                S.require_finite_number(bad, "v")
        self.assertEqual(S.require_finite_number(2, "v"), 2.0)

    def test_unit_interval_bounds(self):
        for bad in (-0.01, 1.01, float("inf")):
            with self.assertRaises(SchemaError):
                S.require_unit_interval(bad, "confidence")
        self.assertEqual(S.require_unit_interval(0.5, "c"), 0.5)

    def test_validate_action_proposal_unknown_key(self):
        with self.assertRaises(SchemaError):
            S.validate_action_proposal({"bs9_power": 1.0},
                                       {"gnb1.cell.power_offset"})

    def test_validate_action_proposal_nan_value(self):
        with self.assertRaises(SchemaError):
            S.validate_action_proposal({"k": float("nan")}, {"k"})


class StrictAlternativesTest(unittest.TestCase):
    """Gate-review 2 #1: the dynamic-alternatives response + entries are strict
    (no fail-open acceptance of empty/null/underspecified objects)."""

    _GOOD = {"alternatives": [{"id": "a", "description": "d",
                               "target_value": 6.0, "confidence": 0.7}]}

    def test_good_response_passes(self):
        self.assertEqual(len(S.validate_alternatives(self._GOOD)), 1)

    def test_missing_alternatives_key_rejected(self):
        with self.assertRaises(SchemaError):
            S.validate_alternatives({})

    def test_unknown_top_level_key_rejected(self):
        with self.assertRaises(SchemaError):
            S.validate_alternatives({"junk": 1})
        with self.assertRaises(SchemaError):
            S.validate_alternatives(dict(self._GOOD, junk=1))

    def test_null_alternatives_rejected(self):
        with self.assertRaises(SchemaError):
            S.validate_alternatives({"alternatives": None})

    def test_entry_wrong_types_rejected(self):
        # id must be a string, description a string (not 7 / [])
        with self.assertRaises(SchemaError):
            S.validate_alternatives({"alternatives": [
                {"id": 7, "description": [], "target_value": 6.0,
                 "confidence": 0.7}]})

    def test_entry_missing_required_field_rejected(self):
        for missing in ("id", "description", "target_value", "confidence"):
            alt = {"id": "a", "description": "d", "target_value": 6.0,
                   "confidence": 0.7}
            del alt[missing]
            with self.assertRaises(SchemaError):
                S.validate_alternatives({"alternatives": [alt]})

    def test_ambiguous_target_value_and_value_rejected(self):
        with self.assertRaises(SchemaError):
            S.validate_alternatives({"alternatives": [
                {"id": "a", "description": "d", "target_value": 6.0,
                 "value": 6.0, "confidence": 0.7}]})

    def test_nonfinite_target_value_rejected(self):
        with self.assertRaises(SchemaError):
            S.validate_alternatives({"alternatives": [
                {"id": "a", "description": "d",
                 "target_value": float("inf"), "confidence": 0.7}]})


class StrictFeasibilityOptionalTest(unittest.TestCase):
    """Gate-review 2 #1: feasibility OPTIONAL fields, when present, cannot be
    null / wrong type."""

    def _base(self, **extra):
        d = {"feasible": True, "confidence": 0.9}
        d.update(extra)
        import json as _json
        return _json.dumps(d)

    def test_null_proposed_config_rejected(self):
        with self.assertRaises(SchemaError):
            S.validate_feasibility(self._base(proposed_config=None))

    def test_null_alternatives_rejected(self):
        with self.assertRaises(SchemaError):
            S.validate_feasibility(self._base(alternatives=None))

    def test_null_reasoning_rejected(self):
        with self.assertRaises(SchemaError):
            S.validate_feasibility(self._base(reasoning=None))

    def test_expected_kpi_nonfinite_value_rejected(self):
        with self.assertRaises(SchemaError):
            S.validate_feasibility(self._base(
                expected_kpi={"ue1": float("inf")}))

    def test_feasibility_alternatives_entry_strict(self):
        # a feasibility.alternatives entry missing confidence is rejected
        with self.assertRaises(SchemaError):
            S.validate_feasibility(self._base(alternatives=[
                {"id": "a", "description": "d", "target_value": 6.0}]))

    def test_good_optional_fields_pass(self):
        out = S.validate_feasibility(self._base(
            reasoning="ok", proposed_config={"bs1_power_offset": 2.0},
            expected_kpi={"ue1": 8.0}, alternatives=[
                {"id": "a", "description": "d", "target_value": 6.0,
                 "confidence": 0.7}]))
        self.assertTrue(out["feasible"])


# --------------------------------------------------------------------------
# Feasibility boundary (through _analyze_feasibility)
# --------------------------------------------------------------------------

class FeasibilityBoundaryTest(unittest.TestCase):

    def test_string_boolean_is_rejected(self):
        # "feasible":"false" is a STRING - it must not be treated as truthy and
        # route to a trial. Schema-rejected -> infeasible + schema_valid False.
        c = _fb_coordinator('{"feasible":"false","confidence":0.9}')
        pred = c._analyze_feasibility(_intent(), [], NetworkState(ue_states={}))
        self.assertFalse(pred.feasible)
        self.assertFalse(c._cur_schema_valid)
        self.assertIn("feasible", (c._cur_schema_reason or ""))

    def test_string_true_boolean_is_rejected(self):
        # even "true" as a string is rejected (JSON booleans only)
        c = _fb_coordinator('{"feasible":"true","confidence":0.9}')
        pred = c._analyze_feasibility(_intent(), [], NetworkState(ue_states={}))
        self.assertFalse(pred.feasible)
        self.assertFalse(c._cur_schema_valid)

    def test_infinity_confidence_is_rejected(self):
        c = _fb_coordinator('{"feasible":true,"confidence":Infinity}')
        pred = c._analyze_feasibility(_intent(), [], NetworkState(ue_states={}))
        self.assertFalse(pred.feasible)
        self.assertFalse(c._cur_schema_valid)

    def test_out_of_range_confidence_is_rejected(self):
        c = _fb_coordinator('{"feasible":true,"confidence":1.5}')
        pred = c._analyze_feasibility(_intent(), [], NetworkState(ue_states={}))
        self.assertFalse(pred.feasible)
        self.assertFalse(c._cur_schema_valid)

    def test_duplicate_json_key_is_rejected(self):
        c = _fb_coordinator('{"feasible":true,"feasible":false,"confidence":0.9}')
        pred = c._analyze_feasibility(_intent(), [], NetworkState(ue_states={}))
        self.assertFalse(pred.feasible)
        self.assertFalse(c._cur_schema_valid)

    def test_unknown_top_level_field_is_rejected(self):
        c = _fb_coordinator('{"feasible":true,"confidence":0.9,"evil":1}')
        pred = c._analyze_feasibility(_intent(), [], NetworkState(ue_states={}))
        self.assertFalse(pred.feasible)
        self.assertFalse(c._cur_schema_valid)

    def test_valid_feasibility_passes(self):
        c = _fb_coordinator(
            '{"feasible":true,"confidence":0.88,"proposed_config":'
            '{"bs1_power_offset":2.0}}')
        pred = c._analyze_feasibility(_intent(), [], NetworkState(ue_states={}))
        self.assertTrue(pred.feasible)
        self.assertAlmostEqual(pred.confidence, 0.88)
        self.assertTrue(c._cur_schema_valid)

    def test_model_failure_is_not_a_schema_reject(self):
        # a model FAILURE (no output) is not a schema violation: infeasible but
        # schema verdict stays valid.
        c = _fb_coordinator("", success=False)
        pred = c._analyze_feasibility(_intent(), [], NetworkState(ue_states={}))
        self.assertFalse(pred.feasible)
        self.assertTrue(c._cur_schema_valid)


# --------------------------------------------------------------------------
# Action-proposal boundary (through _parse_action_vector)
# --------------------------------------------------------------------------

class ActionProposalBoundaryTest(unittest.TestCase):

    def _coord(self, gnbs=("gnb1",)):
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.executor = _FakeExecutor(gnbs)
        c.ue_serving_gnb = {}
        c.action_space = ActionSpaceConfig()
        return c

    def test_nan_action_is_rejected(self):
        c = self._coord()
        with self.assertRaises(SchemaError):
            c._parse_action_vector({"bs1_power_offset": float("nan")})

    def test_infinity_action_is_rejected(self):
        c = self._coord()
        with self.assertRaises(SchemaError):
            c._parse_action_vector({"bs1_power_offset": float("inf")})

    def test_unknown_action_key_rejects_proposal(self):
        # bs2_* targets gnb2, which is NOT configured -> unknown action key.
        c = self._coord(gnbs=("gnb1",))
        with self.assertRaises(SchemaError):
            c._parse_action_vector({"bs2_power_offset": 1.0})

    def test_non_action_metadata_key_is_skipped(self):
        # ONLY explicitly whitelisted envelope metadata ("reasoning",
        # "expected_kpi") is skipped; the action keys are still parsed.
        c = self._coord()
        actions = c._parse_action_vector({"reasoning": "x",
                                          "expected_kpi": {"ue1": 8.0},
                                          "bs1_power_offset": 2.0})
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0][1], "power_offset")

    def test_typo_key_rejects_proposal(self):
        # a typo-like unknown key that is NEITHER an action axis NOR whitelisted
        # envelope metadata REJECTS the whole proposal (coordinator review): it
        # is never silently dropped.  Includes the exact reproduced counter-
        # examples 'bs1_power_typo' (suffix typo) and
        # 'junk_bs1_power_offset_suffix' (prefix/suffix noise).
        c = self._coord()
        for typo in ("bs1_powr", "gnb1_pwr", "throughput_hint", "frobnicate",
                     "bs1_power_typo", "junk_bs1_power_offset_suffix",
                     "bs1_power_offset_x", "xbs1_power_offset"):
            self.assertIsNone(c._parse_axis_key(typo))     # not an action key
            with self.assertRaises(SchemaError):
                c._parse_action_vector({typo: 1.0})

    def test_unknown_key_yields_zero_executor_calls(self):
        # An unknown action key must reject BEFORE any executor write - zero
        # executor calls (through the real _execute_trial choke point).
        calls = []

        class _TrackingExec(_FakeExecutor):
            def __init__(self):
                super().__init__(("gnb1",))

            def snapshot(self, from_device=True):
                return {"gnb1": {"power_offset_db": 0.0,
                                 "snapshot_source": {"power_offset": "device"}}}

            def apply_axis(self, *a, **k):
                calls.append((a, k))
                return True

        c = self._coord()
        c.executor = _TrackingExec()
        c.ue_collector = type("C", (), {"simulation_mode": False})()
        c._log_gui = lambda *a, **k: None
        pred = FeasibilityPrediction(feasible=True, confidence=0.9,
                                     reasoning="",
                                     proposed_config={"bs1_powr": 1.0})
        with self.assertRaises(SchemaError):
            c._parse_action_vector(pred.proposed_config)
        self.assertEqual(calls, [])

    def test_string_action_value_is_rejected(self):
        c = self._coord()
        with self.assertRaises(SchemaError):
            c._parse_action_vector({"bs1_power_offset": "2.0"})

    def test_canonical_axis_collision_rejected(self):
        # Item 2: distinct keys/aliases/prefix-aliases that resolve to the SAME
        # canonical (gnb1.cell.prb) reject the whole proposal.
        c = self._coord()
        for prop in ({"bs1_prb": 10, "bs1_prb_alloc": 20},   # alias collision
                     {"bs1_prb": 10, "gnb1_prb": 20},          # prefix alias
                     {"gnb1_prb": 10, "cell1_prb": 20}):
            with self.assertRaises(SchemaError):
                c._parse_action_vector(prop)

    def test_distinct_axes_do_not_collide(self):
        c = self._coord()
        actions = c._parse_action_vector({"bs1_prb": 10,
                                          "bs1_power_offset": 2.0})
        self.assertEqual(len(actions), 2)


# --------------------------------------------------------------------------
# schema_valid metric uses the ACTUAL validator verdict
# --------------------------------------------------------------------------

class SchemaMetricTest(unittest.TestCase):

    def test_schema_metric_uses_validator_verdict(self):
        # The runner must read the recorded strict-schema verdict, not "no
        # top-level error". A cycle flagged schema_valid=False yields a record
        # with schema_valid False even though the result has no 'error' key.
        from experiments.runner import ExperimentRunner
        runner = ExperimentRunner.__new__(ExperimentRunner)
        runner.coordinator = None

        result = {"success": False, "latency_ms": 10.0, "cycles": [{}]}
        cyc = {"cycle": 0, "confidence": 0.9, "theta_star": 0.5,
               "feasible": True, "schema_valid": False, "routed_to": "trial"}
        rec = runner._episode_from_cycle("m", 1, "P", result, cyc, 0.0, 10.0)
        self.assertFalse(rec.schema_valid)

        cyc_ok = dict(cyc, schema_valid=True)
        rec_ok = runner._episode_from_cycle("m", 1, "P", result, cyc_ok,
                                            0.0, 10.0)
        self.assertTrue(rec_ok.schema_valid)


# --------------------------------------------------------------------------
# End-to-end: a typo proposal through the REAL cycle route -> zero executor
# calls, schema_valid=false, non-commit (coordinator review)
# --------------------------------------------------------------------------

class _TrackingExecutor:
    """Records every device-touching call so a test can prove NONE happened."""
    class _S:
        pass

    def __init__(self, gnbs=("gnb1",)):
        self.states = {g: self._S() for g in gnbs}
        self.snapshots = 0
        self.applies = []

    def snapshot(self, from_device=True):
        self.snapshots += 1
        return {g: {"power_offset_db": 0.0,
                    "snapshot_source": {"power_offset": "device"}}
                for g in self.states}

    def apply_axis(self, *a, **k):
        self.applies.append((a, k))
        return True

    def restore(self, snap):
        return True


class _IMroute:
    def get_active(self):
        return []

    def get_monitored(self):
        return []

    def add(self, intent):
        return True


class _Calroute:
    def get_theta_star(self, phase=None):
        return 0.5

    def get_n_max(self):
        return 1

    n_max = 1

    def record_episode(self, m):
        pass

    def get_stats(self):
        return {}


class _CollectorRoute:
    simulation_mode = False

    def collect_all(self):
        return {}


class _LLMroute:
    def active_backend_name(self):
        return "fake"


class CycleRouteZeroExecutorTest(unittest.TestCase):

    def _coord(self, proposed):
        from coordinator.episode_types import SafetyState
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.gui = None
        c.on_state_change = None
        c.on_negotiation_needed = None
        c.current_state = "S0"
        c.history_reservoir = []
        c.max_history = 100
        c.tau_trial_s = 0.0
        c.intent_manager = _IMroute()
        c.calibrator = _Calroute()
        c.negotiation_policy = lambda alt: "reject"
        c.generate_alternatives_fn = lambda i, r: []
        c.ue_collector = _CollectorRoute()
        c.executor = _TrackingExecutor()
        c.llm_manager = _LLMroute()
        c.action_space = ActionSpaceConfig()
        c.ue_serving_gnb = {}
        c.safety_state = SafetyState.READY
        c._safety_latch = None
        c.episode_budget_s = 5.0
        c.model_call_timeout_s = 30.0
        c.policy_call_timeout_s = 15.0
        c.max_fsm_steps = 256
        c.commit_freshness_s = 30.0
        c._deadline = None
        c._fsm_steps = 0
        c._cur_schema_valid = True
        c._cur_schema_reason = None
        c._parse_intent = lambda text: {"intent": _intent(), "raw": {}}
        c._check_conflicts = lambda new, active: True
        c._get_network_state = lambda: NetworkState(ue_states={})
        def _fake_feasibility(i, a, s):
            # a REAL S2 proposal stamps its prompt hash + the proposal-generated
            # source fact at generation; the S3 write is fail-closed on a bound
            # prompt hash (P1-6). This bypass fixture replicates that contract so
            # a VALID proposal can reach the executor (the fail-closed invariant
            # is preserved, not relaxed).
            c._cur_prompt_hash = hashlib.sha256(
                b"cycle-route-fixture-prompt").hexdigest()   # real 64-hex digest
            c._cur_proposal_generated = True
            c._cur_schema_valid = True   # generated-proposal schema verdict (real bool)
            return FeasibilityPrediction(
                feasible=True, confidence=0.9, reasoning="",
                proposed_config=proposed)
        c._analyze_feasibility = _fake_feasibility
        return c

    def test_typo_proposal_through_process_intent_is_zero_executor(self):
        # A typo action key fed through the REAL process_intent / coordination
        # cycle must be schema-rejected BEFORE any device touch: zero snapshot,
        # zero apply/write, schema_valid=false, and a non-commit outcome.
        c = self._coord({"bs1_powr": 1.0})     # typo: not an axis, not envelope
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(c.executor.snapshots, 0)     # no pre-trial snapshot
        self.assertEqual(c.executor.applies, [])      # no write
        self.assertFalse(result["success"])           # non-commit
        self.assertNotIn(result["terminal_outcome"],
                         ("commit_original", "commit_revised"))
        # the cycle recorded a strict-schema rejection
        cyc = result["cycles"][-1]
        self.assertFalse(cyc["schema_valid"])
        self.assertIn("powr", (cyc.get("schema_reject_reason") or ""))
        self.assertFalse(bool(result.get("schema_valid", True)))

    def test_malformed_dynamic_alternatives_sets_evidence_schema_false(self):
        # A5 (coordinator review): a schema failure during S5 dynamic
        # regeneration (AFTER the S2 verdict) propagates false to the cycle,
        # result, EvidenceRecord and (implicitly) the runner metric.
        c = self._coord({})
        # route to negotiation: infeasible at S2
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=False, confidence=0.1, reasoning="")
        c.generate_alternatives_fn = None          # use the REAL _generate_...
        c._coordination_context = lambda intent: []

        class _AltLLM:
            def generate_alternatives(self, active, failed, state):
                # malformed: alternative missing the required 'confidence'
                obj = {"alternatives": [{"id": "g1", "description": "d",
                                         "target_value": 6.0}]}
                return type("R", (), {"success": True,
                                      "content": json.dumps(obj),
                                      "parsed_json": obj})()

            def active_backend_name(self):
                return "fake"
        c.llm_manager = _AltLLM()
        result = c.process_intent("tput >= 8")
        self.assertFalse(result["success"])
        self.assertFalse(result["evidence"]["schema_valid"])
        self.assertIn("alternatives",
                      (result["evidence"]["schema_reject_reason"] or ""))
        self.assertFalse(bool(result.get("schema_valid", True)))

    def test_axis_collision_through_process_intent_is_zero_executor(self):
        # Item 2 end-to-end: a canonical-axis collision rejects the proposal
        # BEFORE any write - zero snapshot/apply, schema_valid=false, non-commit,
        # no trial id.
        c = self._coord({"bs1_prb": 10, "bs1_prb_alloc": 20})
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(c.executor.snapshots, 0)
        self.assertEqual(c.executor.applies, [])
        self.assertFalse(result["success"])
        self.assertNotIn(result["terminal_outcome"],
                         ("commit_original", "commit_revised"))
        cyc = result["cycles"][-1]
        self.assertFalse(cyc["schema_valid"])
        self.assertIsNone(cyc.get("actuation_trial_id"))

    def test_valid_proposal_through_process_intent_reaches_executor(self):
        # Control: a VALID action key does reach the executor snapshot/apply, so
        # the zero-executor result above is due to the schema rejection, not a
        # broken route.
        c = self._coord({"bs1_power_offset": 2.0})
        c.process_intent("throughput >= 8 Mbps")
        self.assertGreaterEqual(c.executor.snapshots, 1)
        self.assertTrue(c.executor.applies)


# --------------------------------------------------------------------------
# Intent-envelope schema through REAL process_intent -> InputSchemaRejected
# with evidence schema_valid=false (coordinator gate correction)
# --------------------------------------------------------------------------

class _ParseLLM:
    def __init__(self, content):
        self._content = content

    def generate(self, prompt, system_prompt=""):
        return _Resp(self._content, parsed=None, success=True)

    def active_backend_name(self):
        return "fake"


class IntentEnvelopeSchemaTest(unittest.TestCase):
    from coordinator.episode_types import TerminalOutcome, TerminalReason

    def _coord(self, content):
        from coordinator.episode_types import SafetyState
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.gui = None
        c.on_state_change = None
        c.on_negotiation_needed = None
        c.current_state = "S0"
        c.history_reservoir = []
        c.max_history = 100
        c.tau_trial_s = 0.0
        c.intent_manager = _IMroute()
        c.calibrator = _Calroute()
        c.negotiation_policy = lambda alt: "reject"
        c.generate_alternatives_fn = lambda i, r: []
        c.ue_collector = _CollectorRoute()
        c.executor = _TrackingExecutor()
        c.llm_manager = _ParseLLM(content)
        c.action_space = ActionSpaceConfig()
        c.ue_serving_gnb = {}
        c.safety_state = SafetyState.READY
        c._safety_latch = None
        c.episode_budget_s = 5.0
        c.model_call_timeout_s = 30.0
        c.policy_call_timeout_s = 15.0
        c.max_fsm_steps = 256
        c.commit_freshness_s = 30.0
        c._deadline = None
        c._fsm_steps = 0
        c._cur_schema_valid = True
        c._cur_schema_reason = None
        c._check_conflicts = lambda new, active: False
        c._get_network_state = lambda: NetworkState(ue_states={})
        return c

    _COMPLETE = ('{"type":"throughput_goal","constraint":"min","value":8.0,'
                 '"unit":"Mbps","scope":{"ue_ids":[],"bs_ids":[]},'
                 '"description":"x"}')

    def _assert_input_schema_rejected(self, result):
        from coordinator.episode_types import TerminalOutcome, TerminalReason
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.INPUT_SCHEMA_REJECTED.value)
        self.assertFalse(result["success"])
        # evidence surfaces the actual schema verdict even with NO cycle
        self.assertFalse(result["evidence"]["schema_valid"])
        self.assertTrue(result["evidence"]["schema_reject_reason"])
        self.assertEqual(result.get("schema_valid"), False)

    def test_missing_scope_is_input_schema_rejected(self):
        content = ('{"type":"throughput_goal","constraint":"min","value":8.0,'
                   '"unit":"Mbps","description":"x"}')
        result = self._coord(content).process_intent("tput >= 8")
        self._assert_input_schema_rejected(result)
        self.assertIn("scope", result["evidence"]["schema_reject_reason"])

    def test_scope_missing_bs_ids_is_rejected(self):
        content = ('{"type":"throughput_goal","constraint":"min","value":8.0,'
                   '"unit":"Mbps","scope":{"ue_ids":[]},"description":"x"}')
        result = self._coord(content).process_intent("tput >= 8")
        self._assert_input_schema_rejected(result)

    def test_scope_non_string_ids_is_rejected(self):
        content = ('{"type":"throughput_goal","constraint":"min","value":8.0,'
                   '"unit":"Mbps","scope":{"ue_ids":[1,2],"bs_ids":[]},'
                   '"description":"x"}')
        result = self._coord(content).process_intent("tput >= 8")
        self._assert_input_schema_rejected(result)

    def test_missing_value_is_input_schema_rejected(self):
        content = ('{"type":"throughput_goal","constraint":"min",'
                   '"unit":"Mbps","scope":{"ue_ids":[],"bs_ids":[]},'
                   '"description":"x"}')
        result = self._coord(content).process_intent("tput >= 8")
        self._assert_input_schema_rejected(result)

    def test_string_value_is_input_schema_rejected(self):
        content = ('{"type":"throughput_goal","constraint":"min","value":"8.0",'
                   '"unit":"Mbps","scope":{"ue_ids":[],"bs_ids":[]},'
                   '"description":"x"}')
        result = self._coord(content).process_intent("tput >= 8")
        self._assert_input_schema_rejected(result)

    def test_unknown_top_level_key_is_rejected(self):
        content = ('{"type":"throughput_goal","constraint":"min","value":8.0,'
                   '"unit":"Mbps","scope":{"ue_ids":[],"bs_ids":[]},'
                   '"description":"x","evil":1}')
        result = self._coord(content).process_intent("tput >= 8")
        self._assert_input_schema_rejected(result)

    def test_complete_envelope_parses(self):
        # control: a complete valid envelope is NOT schema-rejected
        result = self._coord(self._COMPLETE).process_intent("tput >= 8")
        from coordinator.episode_types import TerminalReason
        self.assertNotEqual(result["terminal_reason"],
                            TerminalReason.INPUT_SCHEMA_REJECTED.value)


if __name__ == "__main__":
    unittest.main()
