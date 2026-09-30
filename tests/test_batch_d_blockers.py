"""Gate-D blocker corrections for Batch D: calibration fail-closed, whole-state
partitioning + persistence, coverage, ledger settlement/actuals, EvidenceRecord
validation, and provenance forgery resistance.
"""

import json
import time
import unittest

from config import CalibrationConfig
from calibration.adaptive_calibrator import AdaptiveCalibrator, CostMetrics
from coordinator.episode_types import (
    EvidenceRecord, ReserveLedger, TerminalOutcome, TerminalReason,
)
from experiments.metrics import trajectory_cost
from decision.intent_model import Alternative, FeasibilityPrediction


# ---------------------------------------------------------------------------
# Blocker 1: calibration fail-closed
# ---------------------------------------------------------------------------
class CalibrationFailClosedTest(unittest.TestCase):

    def _coord(self):
        from tests.test_safety_transaction import _make_coordinator, _trial_ok
        c, _ = _make_coordinator()
        c.tau_trial_s = 0.0
        calls = {"n": 0}
        c._execute_trial = lambda feas: (
            calls.__setitem__("n", calls["n"] + 1) or _trial_ok(feas))
        c._exec_calls = calls
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=0.95, reasoning="",
            alternatives=[Alternative(id="a1", description="x")])
        return c

    def _assert_no_trial_json_safe(self, result, c):
        last = result["cycles"][0]
        self.assertEqual(last["routed_to"], "negotiation")
        self.assertEqual(c._exec_calls["n"], 0)                 # no write
        self.assertEqual(last["calibrated_probability"], 0.0)   # fail-closed
        json.dumps(result["evidence"])                          # JSON-safe

    def test_calibration_inf_fails_closed(self):
        c = self._coord()
        c.calibrator.calibrated_probability = lambda *a, **k: float("inf")
        self._assert_no_trial_json_safe(
            c.process_intent("throughput >= 8 Mbps"), c)

    def test_calibration_nan_fails_closed(self):
        c = self._coord()
        c.calibrator.calibrated_probability = lambda *a, **k: float("nan")
        self._assert_no_trial_json_safe(
            c.process_intent("throughput >= 8 Mbps"), c)

    def test_calibration_out_of_range_fails_closed(self):
        c = self._coord()
        c.calibrator.calibrated_probability = lambda *a, **k: 1.5
        self._assert_no_trial_json_safe(
            c.process_intent("throughput >= 8 Mbps"), c)

    def test_calibration_exception_fails_closed_not_raw(self):
        c = self._coord()

        def _boom(*a, **k):
            raise RuntimeError("calibration exploded")
        c.calibrator.calibrated_probability = _boom
        self._assert_no_trial_json_safe(
            c.process_intent("throughput >= 8 Mbps"), c)

    def test_calibrator_validates_output(self):
        cal = AdaptiveCalibrator(initial_theta=0.4, initial_n_max=2)
        self.assertEqual(cal.calibrated_probability(float("inf")), 0.0)
        self.assertEqual(cal.calibrated_probability(1.5), 0.0)
        self.assertEqual(cal.calibrated_probability(-0.1), 0.0)
        p, reason = cal.calibrate(0.8, "m", "r")
        self.assertAlmostEqual(p, 0.8)
        self.assertEqual(reason, "cold_start_identity")


# ---------------------------------------------------------------------------
# Blocker 2: whole-state partitioning + persistence
# ---------------------------------------------------------------------------
class WholeStatePartitionTest(unittest.TestCase):

    def _metric(self, model, tput_min, tput_before):
        return CostMetrics(
            phase="P", model_id=model, operating_regime_id="R",
            trial_executed=True, trial_success=False, rolled_back=True,
            throughput_before=tput_before, throughput_trial_min=tput_min,
            trial_duration=15.0, raw_confidence=0.5)

    def test_record_episode_switch_no_cross_contamination(self):
        cal = AdaptiveCalibrator(mode="online", window_size=20)
        cold_theta, cold_n = cal.theta_star, cal.n_max
        for _ in range(6):
            cal.record_episode(self._metric("modelA", 0.5, 9.0))
        a_theta, a_est = cal.theta_star, cal.c_worst_estimate
        self.assertNotAlmostEqual(a_theta, cold_theta)          # A moved
        # an UNSEEN modelB returns the COLD start, not A's state
        cal.set_operating_context("modelB", "R")
        self.assertAlmostEqual(cal.theta_star, cold_theta)
        self.assertEqual(cal.n_max, cold_n)
        self.assertNotAlmostEqual(cal.c_worst_estimate, a_est)
        # a modelB episode updates ONLY B; switching back RESTORES A exactly
        cal.record_episode(self._metric("modelB", 1.0, 5.0))
        cal.set_operating_context("modelA", "R")
        self.assertAlmostEqual(cal.theta_star, a_theta)
        self.assertAlmostEqual(cal.c_worst_estimate, a_est)

    def test_persistence_round_trip_all_partitions(self):
        cal = AdaptiveCalibrator(mode="online", window_size=20)
        for _ in range(6):
            cal.record_episode(self._metric("modelA", 0.5, 9.0))
        for _ in range(6):
            cal.record_calibration_outcome(0.85, True, "modelA", "R")
        cal.record_episode(self._metric("modelB", 1.0, 5.0))
        blob = json.loads(json.dumps(cal.to_persistable()))
        cal2 = AdaptiveCalibrator(mode="online", window_size=20)
        self.assertTrue(cal2.load_persistable(blob))
        cal2.set_operating_context("modelA", "R")
        cal.set_operating_context("modelA", "R")
        self.assertAlmostEqual(cal2.theta_star, cal.theta_star)
        self.assertEqual(len(cal2._calibration.get(("modelA", "R"), [])), 6)

    def test_malformed_persistence_rejected_fail_closed(self):
        cal = AdaptiveCalibrator(mode="online", window_size=20)
        base = cal.theta_star
        self.assertFalse(cal.load_persistable({"version": 999}))
        self.assertFalse(cal.load_persistable({"garbage": 1}))
        self.assertAlmostEqual(cal.theta_star, base)            # untouched

    def test_reset_clears_all_partitions(self):
        cal = AdaptiveCalibrator(mode="online", window_size=20)
        for _ in range(6):
            cal.record_episode(self._metric("modelA", 0.5, 9.0))
        cal.set_operating_context("modelB", "R")
        cal.record_calibration_outcome(0.8, True, "modelB", "R")
        cal.reset()
        self.assertEqual(cal._partitions, {})
        self.assertEqual(cal._calibration, {})


# ---------------------------------------------------------------------------
# Blocker 4: coverage = executed / eligible
# ---------------------------------------------------------------------------
class CoverageSelectionTest(unittest.TestCase):

    def test_coverage_is_executed_over_eligible(self):
        cal = AdaptiveCalibrator(initial_theta=0.4, initial_n_max=2,
                                 calibration_min_samples=3)
        for _ in range(10):
            cal.record_proposal(0.85, "m", "R")
        for _ in range(4):
            cal.record_calibration_outcome(0.85, True, "m", "R")
        met = cal.calibration_metrics("m", "R")
        self.assertEqual(met["eligible_proposals"], 10)
        self.assertEqual(met["executed_proposals"], 4)
        self.assertAlmostEqual(met["coverage"], 0.4)
        self.assertTrue(met["sufficient_samples"])   # a bin has >= 3 usable


# ---------------------------------------------------------------------------
# Blocker 5: ledger settlement / measured actuals
# ---------------------------------------------------------------------------
class LedgerSettlementTest(unittest.TestCase):

    def _coord(self):
        from tests.test_safety_transaction import _make_coordinator
        c, _ = _make_coordinator()
        return c

    def test_negotiation_actual_is_measured_14(self):
        c = self._coord()
        led = ReserveLedger(1e9, 0, 0, 50)
        led.reserve_negotiation()
        rid = led.last_reservation_id
        c._settle_negotiation_actual(
            led, rid, {"duration_s": 2, "tput_during_nego": 1}, 8.0)
        s = led.settlements[rid]
        self.assertEqual(s["status"], ReserveLedger.SETTLED)
        self.assertAlmostEqual(s["actual"], 14.0)              # (8-1)*2
        self.assertEqual(s["method"], "duration_deficit")

    def test_negotiation_missing_measurement_is_unknown(self):
        c = self._coord()
        led = ReserveLedger(1e9, 0, 0, 50)
        led.reserve_negotiation()
        rid = led.last_reservation_id
        c._settle_negotiation_actual(led, rid, {"duration_s": None}, None)
        s = led.settlements[rid]
        self.assertEqual(s["status"], ReserveLedger.UNKNOWN)   # not SETTLED 0.0
        self.assertIsNone(s["actual"])
        self.assertEqual(s["method"], "measurement_missing")

    def test_trajectory_actual_differs_from_min_tau(self):
        area = trajectory_cost([(0.0, 10.0), (1.0, 10.0), (2.0, 0.0)],
                               deficit_ref=10.0)
        self.assertAlmostEqual(area, 5.0)
        self.assertNotAlmostEqual(area, (10.0 - 0.0) * 2.0)    # not min*tau (20)

    def test_hard_failure_unmeasured_is_partial_unknown(self):
        led = ReserveLedger(1e9, 100, 300, 50)
        led.reserve_trial()
        rid = led.last_reservation_id
        led.settle(rid, ReserveLedger.PARTIAL_UNKNOWN, actual=None,
                   actual_lower_bound=30.0,
                   components={"continuous": 30.0, "hard_failure": None})
        s = led.settlements[rid]
        self.assertEqual(s["status"], ReserveLedger.PARTIAL_UNKNOWN)
        self.assertIsNone(s["actual"])
        self.assertAlmostEqual(s["actual_lower_bound"], 30.0)
        self.assertAlmostEqual(led.actual, 0.0)   # incomplete not counted

    def test_reservation_id_and_settlement_linkage(self):
        led = ReserveLedger(1e9, 0, 0, 50)
        led.reserve_trial("initial_trial", cycle_index=0, cycle_id="cy-0")
        rid = led.last_reservation_id
        e = led.entries[-1]
        self.assertTrue(e["reservation_id"])
        self.assertIsNotNone(e["timestamp"])
        self.assertEqual(e["cycle_index"], 0)
        self.assertEqual(e["cycle_id"], "cy-0")
        led.settle(rid, ReserveLedger.SETTLED, actual=1.0, method="trajectory")
        self.assertEqual(led.settlements[rid]["reservation_id"], rid)
        self.assertTrue(e["settled"])
        self.assertEqual(led.unsettled_accepted_ids(), [])


# ---------------------------------------------------------------------------
# Blocker 8: EvidenceRecord validation + provenance forgery resistance
# ---------------------------------------------------------------------------
class EvidenceValidationTest(unittest.TestCase):

    def _base(self, **over):
        kw = dict(experiment_run_id="r", episode_id="e", fsm_step_id="f",
                  evidence_record_id="evid", proposer_id="p", model_version="m",
                  intent_set_version="i", pending_intent_hash="pi")
        kw.update(over)
        return kw

    def test_out_of_range_raw_rejected(self):
        with self.assertRaises(ValueError):
            EvidenceRecord(**self._base(raw_confidence=1.5))
        with self.assertRaises(ValueError):
            EvidenceRecord(**self._base(calibrated_probability=float("inf")))

    def test_bad_threshold_applied_to_rejected(self):
        with self.assertRaises(ValueError):
            EvidenceRecord(**self._base(threshold_applied_to="raw_confidence"))

    def test_valid_calibration_accepted(self):
        ev = EvidenceRecord(**self._base(
            raw_confidence=0.9, calibrated_probability=0.3, threshold=0.4,
            threshold_applied_to="calibrated_probability"))
        self.assertEqual(ev.to_dict()["threshold_applied_to"],
                         "calibrated_probability")


class ProvenanceForgeryTest(unittest.TestCase):

    def _committing(self):
        from tests.test_commit_invariant import (
            _commit_coord, _real_write_trial, _full_obs, _ok_verdicts)
        c = _commit_coord(readback=2.0)
        c.on_intent_violated = None
        c._execute_trial = _real_write_trial(c)

        def _v(ni, ai):
            at = getattr(c._active_txn, "action_apply_time", None) or time.time()
            return {"all_satisfied": True, "metrics": {},
                    "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(at)]}
        c._validate_trial = _v
        return c

    def test_forged_calibration_does_not_survive_commit(self):
        c = self._committing()
        real_final = c._finalize_episode
        n = {"n": 0}

        def _fake(result, outcome, reason, *, pending_intent=None,
                  committed_revision=None):
            r = real_final(result, outcome, reason, pending_intent=pending_intent,
                           committed_revision=committed_revision)
            n["n"] += 1
            if n["n"] >= 2:
                for cyc in (result.get("cycles") or []):
                    cyc["raw_confidence"] = 999.0
                    cyc["calibrated_probability"] = 999.0
                    cyc["threshold"] = 999.0
                    cyc["calibration_context"] = {"model_id": "FORGED"}
            return r
        c._finalize_episode = _fake
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        ev = result["evidence"]
        self.assertNotEqual(ev["raw_confidence"], 999.0)
        self.assertNotEqual(ev["calibrated_probability"], 999.0)
        self.assertLessEqual(ev["calibrated_probability"], 1.0)
        self.assertNotIn("FORGED",
                         json.dumps(ev.get("calibration_context") or {}))
        self.assertNotEqual(result["cycles"][-1]["raw_confidence"], 999.0)


class ContextSelectionFailClosedTest(unittest.TestCase):
    """Review: a failed set_operating_context must fail CLOSED (no trial)."""

    def test_context_selection_failure_forces_no_trial(self):
        from tests.test_safety_transaction import _make_coordinator, _trial_ok
        c, _ = _make_coordinator()
        c.tau_trial_s = 0.0
        calls = {"n": 0}
        c._execute_trial = lambda feas: (
            calls.__setitem__("n", calls["n"] + 1) or _trial_ok(feas))
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=0.95, reasoning="",
            alternatives=[Alternative(id="a1", description="x")])

        def _boom(*a, **k):
            raise RuntimeError("cannot select partition")
        c.calibrator.set_operating_context = _boom
        result = c.process_intent("throughput >= 8 Mbps")
        last = result["cycles"][0]
        self.assertEqual(last["routed_to"], "negotiation")   # no trial
        self.assertEqual(calls["n"], 0)                      # no actuation
        self.assertEqual(last["calibrated_probability"], 0.0)
        self.assertEqual(last["calibration_reason"],
                         "fail_closed_context_selection")


class EceCalibratedTest(unittest.TestCase):
    """Item (a): ECE uses the calibrated probability primary + source flag."""

    def test_ece_uses_calibrated_probability(self):
        from experiments.metrics import EpisodeRecord, expected_calibration_error
        eps = [EpisodeRecord(1, "m", "P", trial_executed=True, rolled_back=True,
                             confidence=0.95, raw_confidence=0.95,
                             calibrated_probability=0.2)
               for _ in range(5)]
        out = expected_calibration_error(eps)
        self.assertEqual(out["confidence_source"], "calibrated")
        self.assertEqual(out["probability_field"], "calibrated_probability")
        self.assertAlmostEqual(out["ece"], 0.2, places=6)   # on p_cal, not 0.95

    def test_ece_flags_raw_legacy_source(self):
        from experiments.metrics import EpisodeRecord, expected_calibration_error
        eps = [EpisodeRecord(1, "m", "P", trial_executed=True, rolled_back=True,
                             confidence=0.9)]
        out = expected_calibration_error(eps)
        self.assertEqual(out["confidence_source"], "raw_legacy")
        # HONEST probability_field: raw_legacy -> raw_confidence, NOT calibrated
        self.assertEqual(out["probability_field"], "raw_confidence")

    def test_ece_none_source_when_no_executed(self):
        from experiments.metrics import EpisodeRecord, expected_calibration_error
        out = expected_calibration_error(
            [EpisodeRecord(1, "m", "P", trial_executed=False)])
        self.assertEqual(out["confidence_source"], "none")
        self.assertEqual(out["probability_field"], "none")


class ExpectedCiTest(unittest.TestCase):
    """Item (d): expected_ci includes a p_hard CI (proportion)."""

    def test_expected_ci_has_p_hard(self):
        from experiments.metrics import (
            EpisodeRecord, IntentConfig, estimate_costs)
        eps = [EpisodeRecord(1, "m", "P", trial_executed=True, rolled_back=True,
                             throughput_before=5.0, throughput_trial_min=1.0,
                             tau_trial=15, hard_failure=(i == 0),
                             reconnection_time=2.0)
               for i in range(4)]
        out = estimate_costs(eps, IntentConfig(), c_episode=500.0)
        self.assertIn("p_hard", out["expected_ci"])
        self.assertIn("mean", out["expected_ci"]["p_hard"])


class CostMetricsRoundTripTest(unittest.TestCase):
    """Item (b): EXACT round-trip of every CostMetrics field."""

    def test_exact_round_trip_all_fields(self):
        m = CostMetrics(
            phase="Deg", trial_executed=True, trial_success=True,
            rolled_back=True, episode_success=True, throughput_before=5.0,
            throughput_after=7.0, throughput_trial_min=1.0,
            throughput_loss=3.3, trial_duration=12.0, hard_failure=True,
            reconnection_time=2.5, measurement_invalid=True,
            negotiation_entered=True, negotiation_rounds=2,
            negotiation_duration=4.0, throughput_during_nego=1.5,
            raw_confidence=0.9, calibrated_probability=0.3, threshold=0.4,
            model_id="gpt", operating_regime_id="Deg", proposal_eligible=True,
            routed_to="trial", proposal_executed=True)
        m2 = CostMetrics.from_dict(m.to_dict())
        for f in ("phase", "trial_executed", "trial_success", "rolled_back",
                  "episode_success", "throughput_before", "throughput_after",
                  "throughput_trial_min", "throughput_loss", "trial_duration",
                  "hard_failure", "reconnection_time", "measurement_invalid",
                  "negotiation_entered", "negotiation_rounds",
                  "negotiation_duration", "throughput_during_nego",
                  "raw_confidence", "calibrated_probability", "threshold",
                  "threshold_applied_to", "model_id", "operating_regime_id",
                  "proposal_eligible", "routed_to", "proposal_executed"):
            self.assertEqual(getattr(m, f), getattr(m2, f), f)
        self.assertEqual(m.timestamp.isoformat(), m2.timestamp.isoformat())


class AtomicPersistenceTest(unittest.TestCase):
    """Item (c): a malformed field leaves live state DEEP-UNCHANGED."""

    def _loaded(self):
        cal = AdaptiveCalibrator(mode="online", window_size=20)
        for _ in range(6):
            cal.record_episode(CostMetrics(
                model_id="modelA", operating_regime_id="R", trial_executed=True,
                rolled_back=True, throughput_before=9.0,
                throughput_trial_min=0.5, raw_confidence=0.5))
        cal.record_calibration_outcome(0.85, True, "modelA", "R")
        return cal

    def _snapshot(self, cal):
        return json.dumps(cal.to_persistable(), sort_keys=True, default=str)

    def _assert_unchanged(self, cal, before, mutate):
        blob = json.loads(json.dumps(cal.to_persistable(), default=str))
        mutate(blob)
        self.assertFalse(cal.load_persistable(blob))       # rejected
        self.assertEqual(self._snapshot(cal), before)      # DEEP unchanged

    def test_bad_version_leaves_state_unchanged(self):
        cal = self._loaded()
        before = self._snapshot(cal)
        self._assert_unchanged(cal, before,
                               lambda b: b.__setitem__("version", 999))

    def test_nonfinite_routing_scalar_unchanged(self):
        cal = self._loaded()
        before = self._snapshot(cal)

        def _m(b):
            k = next(iter(b["routing_partitions"]))
            b["routing_partitions"][k]["theta_star"] = "nan_str"
        self._assert_unchanged(cal, before, _m)

    def test_bad_reliability_entry_unchanged(self):
        cal = self._loaded()
        before = self._snapshot(cal)

        def _m(b):
            k = next(iter(b["reliability"]))
            b["reliability"][k] = [[1.5, True]]     # raw out of [0,1]
        self._assert_unchanged(cal, before, _m)

    def test_negative_counter_unchanged(self):
        cal = self._loaded()
        before = self._snapshot(cal)
        self._assert_unchanged(
            cal, before,
            lambda b: b["counters"].__setitem__("total_trials", -1))

    def test_missing_active_key_unchanged(self):
        cal = self._loaded()
        before = self._snapshot(cal)
        self._assert_unchanged(
            cal, before,
            lambda b: b.__setitem__("active_key", "ghost␟Z"))

    def _first_part(self, b):
        # a partition key that actually HAS history to mutate
        for k, r in b["routing_partitions"].items():
            if r.get("history"):
                return k
        return next(iter(b["routing_partitions"]))

    def test_out_of_range_history_prob_unchanged(self):
        cal = self._loaded()
        before = self._snapshot(cal)

        def _m(b):
            r = b["routing_partitions"][self._first_part(b)]
            r["history"][0]["calibrated_probability"] = 9.0   # finite but >1
        self._assert_unchanged(cal, before, _m)

    def test_missing_partition_counter_unchanged(self):
        cal = self._loaded()
        before = self._snapshot(cal)

        def _m(b):
            del b["routing_partitions"][self._first_part(b)]["p_trials"]
        self._assert_unchanged(cal, before, _m)

    def test_nonint_phase_n_max_unchanged(self):
        cal = self._loaded()
        before = self._snapshot(cal)

        def _m(b):
            b["routing_partitions"][self._first_part(b)]["phase_n_max"] = \
                {"P": 1.5}
        self._assert_unchanged(cal, before, _m)

        cal2 = self._loaded()
        before2 = self._snapshot(cal2)

        def _m2(b):
            b["routing_partitions"][self._first_part(b)]["phase_n_max"] = \
                {"P": -1}
        self._assert_unchanged(cal2, before2, _m2)

    def test_string_bool_history_field_unchanged(self):
        cal = self._loaded()
        before = self._snapshot(cal)

        def _m(b):
            r = b["routing_partitions"][self._first_part(b)]
            r["history"][0]["trial_success"] = "false"   # str, must not coerce
        self._assert_unchanged(cal, before, _m)

    def test_nonint_negotiation_rounds_unchanged(self):
        cal = self._loaded()
        before = self._snapshot(cal)

        def _m(b):
            r = b["routing_partitions"][self._first_part(b)]
            r["history"][0]["negotiation_rounds"] = 1.5   # must not coerce to 1
        self._assert_unchanged(cal, before, _m)


class LedgerHardeningTest(unittest.TestCase):
    """Ledger follow-up: settle accepts only an existing ACCEPTED reservation
    exactly once, with strict status/value invariants and no clamping."""

    def _led(self):
        led = ReserveLedger(500, 100, 30, 50)
        led.reserve_trial()
        return led, led.last_reservation_id

    def test_double_settle_raises(self):
        led, rid = self._led()
        led.settle(rid, ReserveLedger.SETTLED, actual=7.0)
        with self.assertRaises(ValueError):
            led.settle(rid, ReserveLedger.SETTLED, actual=7.0)
        self.assertAlmostEqual(led.actual, 7.0)          # counted ONCE

    def test_unknown_id_settle_raises(self):
        led, _ = self._led()
        with self.assertRaises(ValueError):
            led.settle("rsv-does-not-exist", ReserveLedger.SETTLED, actual=9.0)
        self.assertAlmostEqual(led.actual, 0.0)

    def test_rejected_reservation_settle_raises(self):
        led = ReserveLedger(10, 100, 30, 50)   # trial 130 > cap -> rejected
        self.assertFalse(led.reserve_trial())
        rid = led.last_reservation_id
        with self.assertRaises(ValueError):
            led.settle(rid, ReserveLedger.SETTLED, actual=3.0)
        self.assertAlmostEqual(led.actual, 0.0)

    def test_invalid_status_raises(self):
        led, rid = self._led()
        with self.assertRaises(ValueError):
            led.settle(rid, "bogus_status", actual=1.0)

    def test_settled_requires_finite_actual(self):
        led, rid = self._led()
        with self.assertRaises(ValueError):
            led.settle(rid, ReserveLedger.SETTLED, actual=None)
        with self.assertRaises(ValueError):
            led.settle(rid, ReserveLedger.SETTLED, actual=float("inf"))

    def test_status_value_invariants(self):
        led, rid = self._led()
        with self.assertRaises(ValueError):     # UNKNOWN must have actual=None
            led.settle(rid, ReserveLedger.UNKNOWN, actual=1.0)
        led2 = ReserveLedger(500, 100, 30, 50)
        led2.reserve_trial()
        with self.assertRaises(ValueError):     # PARTIAL needs a lower bound
            led2.settle(led2.last_reservation_id,
                        ReserveLedger.PARTIAL_UNKNOWN, actual=None)

    def test_nonfinite_amount_rejected_not_clamped(self):
        with self.assertRaises(ValueError):
            ReserveLedger(float("nan"), 100, 30, 50)
        with self.assertRaises(ValueError):
            ReserveLedger(500, -5, 30, 50)

    def test_actual_exceeds_reservation_flagged(self):
        led, rid = self._led()             # trial UB = 130
        rec = led.settle(rid, ReserveLedger.SETTLED, actual=999.0)
        self.assertTrue(rec["actual_exceeds_reservation"])
        self.assertTrue(led.any_cap_breach)
        self.assertAlmostEqual(led.actual, 999.0)   # preserved, not clamped


class NonCommitForgeryTest(unittest.TestCase):
    """Blocker 8: a forged cycle calibration on a NON-COMMIT terminal cannot
    survive into the evidence (normalized from the trusted source)."""

    def test_forged_calibration_does_not_survive_noncommit(self):
        from tests.test_safety_transaction import _make_coordinator, _trial_ok
        c, _ = _make_coordinator()
        c.tau_trial_s = 0.0
        # low confidence -> routes to negotiation -> PendingNotAdmitted (no commit)
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=0.9, reasoning="",
            alternatives=[Alternative(id="a1", description="x")])
        real_final = c._finalize_episode

        def _fake(result, outcome, reason, *, pending_intent=None,
                  committed_revision=None):
            # forge the live cycle calibration BEFORE finalization normalizes it
            for cyc in (result.get("cycles") or []):
                cyc["raw_confidence"] = 999.0
                cyc["calibrated_probability"] = 999.0
                cyc["threshold"] = 999.0
            return real_final(result, outcome, reason,
                              pending_intent=pending_intent,
                              committed_revision=committed_revision)
        c._finalize_episode = _fake
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertFalse(result["success"])                  # non-commit
        ev = result["evidence"]
        # evidence calibration is from the TRUSTED source, not the forged cycle
        if ev.get("raw_confidence") is not None:
            self.assertNotEqual(ev["raw_confidence"], 999.0)
            self.assertLessEqual(ev["calibrated_probability"], 1.0)
        # last cycle normalized too
        self.assertNotEqual(result["cycles"][-1]["raw_confidence"], 999.0)


class EceInvalidCountTest(unittest.TestCase):
    """Item (a): ECE counts invalid probabilities separately."""

    def test_invalid_probabilities_counted_and_excluded(self):
        from experiments.metrics import EpisodeRecord, expected_calibration_error
        eps = [EpisodeRecord(1, "m", "P", trial_executed=True,
                             calibrated_probability=0.5),
               EpisodeRecord(2, "m", "P", trial_executed=True,
                             calibrated_probability=1.5),   # invalid
               EpisodeRecord(3, "m", "P", trial_executed=True,
                             calibrated_probability=float("nan"))]  # invalid
        out = expected_calibration_error(eps)
        self.assertEqual(out["n_invalid"], 2)
        self.assertEqual(out["n"], 1)


class RecordCycleHardFailureTest(unittest.TestCase):
    """Exact probe: _record_cycle with hard_failure and Optional reconnection
    time must not crash and must settle the trial reservation correctly."""

    def _coord(self):
        from coordinator.intent_coordinator import IntentCoordinator
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.calibrator = AdaptiveCalibrator(initial_theta=0.4, initial_n_max=2)
        c.tau_trial_s = 2.0
        c.current_phase = "P"
        c._active_txn = None
        c._active_calibration = {}
        c._episode_context = ("m", "P")
        c._calibration_context_ok = True
        led = ReserveLedger(1e9, 100, 300, 50)
        led.reserve_trial()
        c._reserve_ledger = led
        return c, led

    def _cycle(self, rc):
        return {"trial_success": False, "hard_failure": True,
                "reconnection_time": rc,
                "trial_reservation_id": None,   # filled below
                "trial_stats": {"tput_before": 10.0, "tput_min": 8.0, "tau": 2.0}}

    def test_unmeasured_reconnection_is_partial_unknown(self):
        c, led = self._coord()
        cyc = self._cycle(None)
        cyc["trial_reservation_id"] = led.last_reservation_id
        c._record_cycle(cyc, episode_success=False)   # must NOT raise
        s = led.settlements[led.last_reservation_id]
        self.assertEqual(s["status"], ReserveLedger.PARTIAL_UNKNOWN)
        self.assertIsNone(s["actual"])
        self.assertAlmostEqual(s["actual_lower_bound"], 4.0)   # continuous only
        self.assertEqual(led.unsettled_accepted_ids(), [])
        # no zero-cost C_hard sample: the recorded metric keeps None
        self.assertIsNone(c.calibrator.history[-1].reconnection_time)

    def test_measured_reconnection_is_settled_34(self):
        c, led = self._coord()
        cyc = self._cycle(3.0)
        cyc["trial_reservation_id"] = led.last_reservation_id
        c._record_cycle(cyc, episode_success=False)
        s = led.settlements[led.last_reservation_id]
        self.assertEqual(s["status"], ReserveLedger.SETTLED)
        self.assertAlmostEqual(s["actual"], 34.0)   # continuous 4 + hard 10*3=30
        self.assertEqual(led.unsettled_accepted_ids(), [])


class CoverageConsistencyTest(unittest.TestCase):
    """Coverage blocker: the exported proposal_eligible must EXACTLY match the
    calibrator's eligible counter (bind the actual record_proposal bool)."""

    def _coord(self, confidence):
        from tests.test_safety_transaction import _make_coordinator
        c, _ = _make_coordinator()
        c.tau_trial_s = 0.0
        c.calibrator = AdaptiveCalibrator(initial_theta=0.4, initial_n_max=1)
        # an UNMATERIALIZABLE alt -> a single negotiation cycle, no re-entry
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=confidence, reasoning="",
            alternatives=[Alternative(id="a1", description="x")])
        return c

    def test_invalid_raw_not_eligible_consistent(self):
        c = self._coord(1.5)                      # invalid raw -> not eligible
        c.process_intent("throughput >= 8 Mbps")
        total_eligible = sum(p["eligible"]
                             for p in c.calibrator._proposals.values())
        self.assertEqual(total_eligible, 0)       # calibrator counted 0
        rec = c.calibrator.history[-1]
        self.assertFalse(rec.proposal_eligible)   # exported matches
        self.assertFalse(rec.proposal_executed)

    def test_valid_negotiation_proposal_eligible_not_executed(self):
        c = self._coord(0.3)                       # valid but < theta -> nego
        c.process_intent("throughput >= 8 Mbps")
        total_eligible = sum(p["eligible"]
                             for p in c.calibrator._proposals.values())
        total_executed = sum(p["executed"]
                             for p in c.calibrator._proposals.values())
        self.assertEqual(total_eligible, 1)        # counted eligible
        self.assertEqual(total_executed, 0)        # not executed (no trial)
        rec = c.calibrator.history[-1]
        self.assertTrue(rec.proposal_eligible)
        self.assertFalse(rec.proposal_executed)


if __name__ == "__main__":
    unittest.main()
