"""Gate A [C3]: fail-closed intent parsing and evaluation.

Independent-verification counter-examples (report C3): unsupported intent
types, unknown constraints, missing/NaN/Infinity values must be rejected
outright - never coerced into a valid-looking intent (the pre-fix fail-open
path: unknown type -> THROUGHPUT_GOAL, unknown constraint -> MAX, missing
value -> 0.0) - and an intent type without an evaluator must never count as
satisfied (pre-fix: `return True`).
"""

import types
import unittest

from coordinator.intent_coordinator import (
    IntentCoordinator, IntentManager, IntentParseError, SUPPORTED_INTENT_TYPES,
)
from decision.intent_model import (
    ConstraintType, FeasibilityPrediction, Intent, IntentStatus, IntentTarget,
    IntentType, NetworkState,
)


class _Resp:
    def __init__(self, parsed_json, success=True):
        self.success = success
        self.parsed_json = parsed_json


class _FakeLLM:
    def __init__(self, parse_json):
        self.parse_json = parse_json

    def generate(self, prompt, system_prompt=""):
        return _Resp(self.parse_json)


class _Metric:
    def __init__(self, tput, latency=None):
        self.throughput_mbps = tput
        self.latency_ms = latency
        self.attached = True

    def to_dict(self):
        return {"throughput_mbps": self.throughput_mbps,
                "latency_ms": self.latency_ms,
                "attached": self.attached}


class _FakeCollector:
    def __init__(self, metrics=None):
        self.metrics = metrics or {}

    def collect_all(self):
        return dict(self.metrics)


class _FakeCalibrator:
    def __init__(self, theta=0.5, n_max=2):
        self.theta = theta
        self.n_max = n_max
        self.recorded = []

    def get_theta_star(self, phase=None):
        return self.theta

    def can_negotiate_more(self, rounds, phase=None):
        return rounds < self.n_max

    def record_episode(self, metrics):
        self.recorded.append(metrics)


def _skeleton(parse_json=None):
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.gui = None
    c.on_state_change = None
    c.on_negotiation_needed = None
    c.current_state = "S0"
    c.history_reservoir = []
    c.max_history = 100
    c.tau_trial_s = 0.0
    c.llm_manager = _FakeLLM(parse_json)
    # a minimal executor + action space so _authorize_action can derive the
    # canonical action (Batch C: authorization runs before the trial).
    from config import ActionSpaceConfig
    c.executor = types.SimpleNamespace(states={"gnb1": object(),
                                               "gnb2": object()})
    c.action_space = ActionSpaceConfig()
    c.ue_serving_gnb = {"ue1": "gnb1", "ue2": "gnb1", "ue3": "gnb2"}
    c.ue_rnti = {}
    return c


VALID = {"type": "throughput_goal", "constraint": "min", "value": 8.0,
         "unit": "Mbps", "scope": {"ue_ids": [], "bs_ids": []},
         "description": "tput"}


def _intent_of(itype) -> Intent:
    return Intent(type=itype,
                  target=IntentTarget(kpi_name="x",
                                      constraint_type=ConstraintType.MAX,
                                      target_value=1.0))


class ParseFailClosedTest(unittest.TestCase):
    """_parse_intent rejects (raises) instead of coercing."""

    def _parse(self, **overrides):
        data = dict(VALID)
        for key, val in overrides.items():
            if val is _MISSING:
                del data[key]
            else:
                data[key] = val
        return _skeleton(data)._parse_intent("text")

    def test_valid_intent_still_parses(self):
        parsed = self._parse()
        self.assertEqual(parsed["intent"].type, IntentType.THROUGHPUT_GOAL)
        self.assertEqual(parsed["intent"].target.constraint_type,
                         ConstraintType.MIN)
        self.assertAlmostEqual(parsed["intent"].target.target_value, 8.0)

    def test_unknown_type_rejected(self):
        with self.assertRaisesRegex(IntentParseError, "unsupported"):
            self._parse(type="beamforming_goal")

    def test_unsupported_known_type_rejected(self):
        # energy_efficiency IS an IntentType enum member, but there is no
        # evaluator for it -> parse must reject it too
        with self.assertRaisesRegex(IntentParseError, "unsupported"):
            self._parse(type="energy_efficiency")

    def test_missing_type_rejected(self):
        with self.assertRaises(IntentParseError):
            self._parse(type=_MISSING)

    def test_unknown_constraint_rejected(self):
        with self.assertRaisesRegex(IntentParseError, "constraint"):
            self._parse(constraint="range")

    def test_missing_constraint_rejected(self):
        with self.assertRaises(IntentParseError):
            self._parse(constraint=_MISSING)

    def test_missing_value_rejected(self):
        # 'value' is in the required key set: a missing value is a required-key
        # rejection (message mentions 'value').
        with self.assertRaisesRegex(IntentParseError, "value"):
            self._parse(value=_MISSING)

    def test_null_value_rejected(self):
        with self.assertRaises(IntentParseError):
            self._parse(value=None)

    def test_nan_value_rejected(self):
        with self.assertRaisesRegex(IntentParseError, "non-finite"):
            self._parse(value=float("nan"))

    def test_infinity_value_rejected(self):
        with self.assertRaisesRegex(IntentParseError, "non-finite"):
            self._parse(value=float("inf"))

    def test_non_numeric_value_rejected(self):
        with self.assertRaisesRegex(IntentParseError, "non-numeric"):
            self._parse(value="a lot")

    def test_llm_failure_still_returns_none(self):
        c = _skeleton(None)
        c.llm_manager = _FakeLLM(None)
        self.assertIsNone(c._parse_intent("text"))

    def test_empty_json_returns_none(self):
        # {} is "the LLM produced no content", not a malformed intent
        self.assertIsNone(_skeleton({})._parse_intent("text"))

    def test_mismatched_constraint_direction_rejected(self):
        # The evaluator implements throughput as a floor and latency/
        # fairness/power as ceilings; the opposite direction would be
        # evaluated backwards, so it must be rejected at parse.
        cases = [
            ("throughput_goal", "max"),
            ("latency_goal", "min"),
            ("power_constraint", "min"),
            ("throughput_fairness", "min"),
        ]
        for type_str, constraint in cases:
            with self.assertRaisesRegex(IntentParseError, "unsupported for",
                                        msg=f"{type_str}/{constraint}"):
                self._parse(type=type_str, constraint=constraint)

    def test_matching_constraint_directions_parse(self):
        for type_str, constraint, itype in [
                ("throughput_goal", "min", IntentType.THROUGHPUT_GOAL),
                ("latency_goal", "max", IntentType.LATENCY_GOAL),
                ("power_constraint", "max", IntentType.POWER_CONSTRAINT),
                ("throughput_fairness", "max", IntentType.THROUGHPUT_FAIRNESS)]:
            parsed = self._parse(type=type_str, constraint=constraint)
            self.assertEqual(parsed["intent"].type, itype)


_MISSING = object()


class ProcessIntentRejectTest(unittest.TestCase):
    """A malformed parse terminates the episode as an explicit rejection."""

    def test_malformed_parse_rejects_episode(self):
        # complete envelope so it reaches the intended unsupported-TYPE assertion
        # (P0-5 requires the full required intent envelope).
        c = _skeleton({"type": "coverage_goal", "constraint": "min",
                       "value": 5, "unit": "dB",
                       "scope": {"ue_ids": [], "bs_ids": []},
                       "description": "coverage"})
        result = c.process_intent("ensure coverage everywhere")
        self.assertFalse(result["success"])
        self.assertEqual(result["resolution"], "reject")
        self.assertIn("fail-closed", result["error"])
        self.assertIn("coverage_goal", result["error"])


class EvaluateFailClosedTest(unittest.TestCase):
    """_evaluate_intent raises on unsupported types; callers fail closed."""

    UNSUPPORTED = (IntentType.RSRP_CONSTRAINT, IntentType.ENERGY_EFFICIENCY,
                   IntentType.COVERAGE_GOAL)

    def test_unsupported_types_raise(self):
        c = _skeleton()
        for itype in self.UNSUPPORTED:
            with self.assertRaises(ValueError):
                c._evaluate_intent(_intent_of(itype), {})

    def test_non_enum_type_raises_value_error(self):
        # A malformed (non-enum) intent.type must still raise ValueError -
        # an AttributeError would escape every caller's fail-closed handler.
        c = _skeleton()
        bogus = Intent(type=IntentType.THROUGHPUT_GOAL,
                       target=IntentTarget(kpi_name="x",
                                           constraint_type=ConstraintType.MAX,
                                           target_value=1.0))
        bogus.type = "bogus_type"
        with self.assertRaisesRegex(ValueError, "bogus_type"):
            c._evaluate_intent(bogus, {})

    def test_supported_set_matches_parse_map(self):
        # The parser whitelist and the evaluator's supported set must be the
        # same set - a divergence reopens a fail-open path on one side.
        parse_types = {t for t, _ in
                       IntentCoordinator._PARSE_TYPE_MAP.values()}
        self.assertEqual(parse_types, set(SUPPORTED_INTENT_TYPES))
        for itype in self.UNSUPPORTED:
            self.assertNotIn(itype, SUPPORTED_INTENT_TYPES)

    def test_empty_metrics_not_satisfied(self):
        # An empty UE scope / empty metrics map can never verify a goal.
        c = _skeleton()
        tput = Intent(type=IntentType.THROUGHPUT_GOAL,
                      target=IntentTarget(kpi_name="throughput",
                                          constraint_type=ConstraintType.MIN,
                                          target_value=8.0))
        self.assertFalse(c._evaluate_intent(tput, {}))

    def test_nan_kpi_not_satisfied(self):
        c = _skeleton()
        tput = Intent(type=IntentType.THROUGHPUT_GOAL,
                      target=IntentTarget(kpi_name="throughput",
                                          constraint_type=ConstraintType.MIN,
                                          target_value=8.0))
        self.assertFalse(c._evaluate_intent(
            tput, {"ue1": _Metric(float("nan"))}))
        lat = Intent(type=IntentType.LATENCY_GOAL,
                     target=IntentTarget(kpi_name="latency",
                                         constraint_type=ConstraintType.MAX,
                                         target_value=50.0))
        self.assertFalse(c._evaluate_intent(
            lat, {"ue1": _Metric(9.0, latency=float("nan"))}))

    def test_empty_offsets_power_not_satisfied(self):
        c = _skeleton()

        class _NoGNBExecutor:
            def get_all_offsets(self):
                return {}

        c.executor = _NoGNBExecutor()
        power = Intent(type=IntentType.POWER_CONSTRAINT,
                       target=IntentTarget(kpi_name="tx_power",
                                           constraint_type=ConstraintType.MAX,
                                           target_value=3.0))
        self.assertFalse(c._evaluate_intent(power, {}))

    def test_mismatched_direction_raises_for_programmatic_intent(self):
        # A programmatic THROUGHPUT_GOAL with a MAX constraint would be
        # evaluated backwards (9 >= 8 "satisfies" a <= 8 ceiling); the
        # evaluator enforces its direction contract on every path, not just
        # the LLM parse.
        c = _skeleton()
        backwards = Intent(type=IntentType.THROUGHPUT_GOAL,
                           target=IntentTarget(
                               kpi_name="throughput",
                               constraint_type=ConstraintType.MAX,
                               target_value=8.0))
        with self.assertRaisesRegex(ValueError, "unsupported for"):
            c._evaluate_intent(backwards, {"ue1": _Metric(9.0)})

    def test_fairness_mixed_nan_not_satisfied(self):
        # [10, 10, NaN] must not pass a variance ceiling: fairness over a
        # population with an unverifiable member is unverifiable.
        c = _skeleton()
        fair = Intent(type=IntentType.THROUGHPUT_FAIRNESS,
                      target=IntentTarget(kpi_name="throughput_variance",
                                          constraint_type=ConstraintType.MAX,
                                          target_value=1.0))
        self.assertFalse(c._evaluate_intent(
            fair, {"ue1": _Metric(10.0), "ue2": _Metric(10.0),
                   "ue3": _Metric(float("nan"))}))
        self.assertFalse(c._evaluate_intent(
            fair, {"ue1": _Metric(10.0), "ue2": _Metric(None)}))
        # all-finite population still evaluates normally
        self.assertTrue(c._evaluate_intent(
            fair, {"ue1": _Metric(10.0), "ue2": _Metric(10.0)}))

    def test_s4_validation_fail_closed_on_unsupported(self):
        # An unverifiable active intent can never validate a trial.
        c = _skeleton()
        good = Intent(type=IntentType.THROUGHPUT_GOAL,
                      target=IntentTarget(kpi_name="throughput",
                                          constraint_type=ConstraintType.MIN,
                                          target_value=8.0))
        bad = _intent_of(IntentType.COVERAGE_GOAL)
        metrics = {"ue1": _Metric(9.0)}
        v = c._build_validation(metrics, good, [bad], samples=[9.0],
                                trajectory=[], hard_failure=False,
                                reconnection_time=0.0)
        self.assertFalse(v["all_satisfied"])
        self.assertFalse(v["intent_results"][bad.id])
        self.assertTrue(v["intent_results"][good.id])

    def test_monitoring_skips_unsupported_without_status_change(self):
        c = _skeleton()
        c.intent_manager = IntentManager()
        bad = _intent_of(IntentType.ENERGY_EFFICIENCY)
        bad.status = IntentStatus.ACTIVE
        c.intent_manager.add(bad)
        c._check_intent_satisfaction({})   # must not raise
        self.assertEqual(bad.status, IntentStatus.ACTIVE)

    def test_programmatic_unsupported_rejected_before_s2(self):
        # Programmatic (non-LLM-parsed) unsupported intent must be rejected
        # BEFORE S2/S3 - it can never be validated at S4, so letting it
        # through could execute a RAN action that cannot be verified.
        c = _skeleton()
        bad = _intent_of(IntentType.COVERAGE_GOAL)
        c._parse_intent = lambda text: {"intent": bad, "raw": {}}
        s2_calls = []
        c._analyze_feasibility = (
            lambda i, a, s: s2_calls.append(i) or FeasibilityPrediction(
                feasible=True, confidence=0.99, reasoning=""))
        result = c.process_intent("coverage goal")
        self.assertEqual(result["resolution"], "reject")
        self.assertFalse(result["success"])
        self.assertIn("unsupported intent type", result["error"])
        self.assertEqual(s2_calls, [])   # S2 never reached

    def test_s4_exception_still_rolls_back(self):
        # An unexpected S4 validation exception must not leave the trial
        # configuration applied: rollback happens before the error surfaces.
        c = _skeleton()
        intent = Intent(type=IntentType.THROUGHPUT_GOAL,
                        target=IntentTarget(kpi_name="throughput",
                                            constraint_type=ConstraintType.MIN,
                                            target_value=8.0))
        c._parse_intent = lambda text: {"intent": intent, "raw": {}}
        c.intent_manager = IntentManager()
        c.ue_collector = _FakeCollector({"ue1": _Metric(5.0)})
        c.calibrator = _FakeCalibrator(theta=0.5)
        c.negotiation_policy = lambda alt: "reject"
        c.generate_alternatives_fn = lambda i, r: []
        c._check_conflicts = lambda n, a: False
        c._get_network_state = lambda: NetworkState(ue_states={})
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=0.9, reasoning="",
            proposed_config={"bs1_power_offset": 2.0})
        snapshot = {"gnb1": {"power_offset_db": 0.0}}
        c._execute_trial = lambda feas: {"success": True,
                                         "snapshot": snapshot,
                                         "clipped": [], "applied": []}

        def _boom(new_intent, active_intents):
            raise RuntimeError("collector crashed mid-window")

        c._validate_trial = _boom
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        result = c.process_intent("throughput >= 8")
        self.assertEqual(rollbacks, [snapshot])
        self.assertTrue(result["rolled_back"])
        self.assertFalse(result["success"])
        self.assertIn("collector crashed", result["error"])
        # the exception cycle is still RECORDED, honestly: the trial DID
        # execute and failed, with an untrusted (invalid) window
        self.assertEqual(len(c.calibrator.recorded), 1)
        rec = c.calibrator.recorded[0]
        self.assertTrue(rec.trial_executed)
        self.assertFalse(rec.trial_success)
        self.assertTrue(rec.measurement_invalid)
        self.assertTrue(rec.rolled_back)

    def test_s4_malformed_validation_dict_still_rolls_back(self):
        # A validation dict missing "all_satisfied" (KeyError while
        # CONSUMING the validation) must also trigger the rollback - the
        # transactional guard covers consumption, not just the call.
        c = _skeleton()
        intent = Intent(type=IntentType.THROUGHPUT_GOAL,
                        target=IntentTarget(kpi_name="throughput",
                                            constraint_type=ConstraintType.MIN,
                                            target_value=8.0))
        c._parse_intent = lambda text: {"intent": intent, "raw": {}}
        c.intent_manager = IntentManager()
        c.ue_collector = _FakeCollector({"ue1": _Metric(5.0)})
        c.calibrator = _FakeCalibrator(theta=0.5)
        c.negotiation_policy = lambda alt: "reject"
        c.generate_alternatives_fn = lambda i, r: []
        c._check_conflicts = lambda n, a: False
        c._get_network_state = lambda: NetworkState(ue_states={})
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=0.9, reasoning="",
            proposed_config={"bs1_power_offset": 2.0})
        snapshot = {"gnb1": {"power_offset_db": 0.0}}
        c._execute_trial = lambda feas: {"success": True,
                                         "snapshot": snapshot,
                                         "clipped": [], "applied": []}
        c._validate_trial = lambda n, a: {}   # malformed: no all_satisfied
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        result = c.process_intent("throughput >= 8")
        self.assertEqual(rollbacks, [snapshot])
        self.assertTrue(result["rolled_back"])
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
