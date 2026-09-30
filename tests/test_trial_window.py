"""P4: polled S4 validation window, hard-failure detection, measured cost
fields flowing coordinator -> result -> runner -> metrics, and the degenerate
cost-component detector.
"""

import tempfile
import unittest

from coordinator.intent_coordinator import IntentCoordinator
from decision.intent_model import (
    ConstraintType, FeasibilityPrediction, Intent, IntentTarget, IntentType,
    NetworkState,
)
from experiments.metrics import (
    EpisodeRecord, IntentConfig, estimate_costs,
)
from experiments.runner import ExperimentRunner


class _Metric:
    def __init__(self, attached=True, tput=5.0):
        self.attached = attached
        self.throughput_mbps = tput

    def to_dict(self):
        return {"attached": self.attached,
                "throughput_mbps": self.throughput_mbps}


class _SeqCollector:
    """Batch F: get_throughput_all() is the AUTHORITATIVE live throughput probe
    walking the scripted sequence; collect_all() supplies ATTACHMENT for the
    same entry (its throughput plays no role in the S4 KPI). Per S4 iteration the
    probe reads the current entry and the following collect_all advances - so
    they stay in lockstep. The last entry repeats. (S4 throughput comes from the
    live probe, never the collect_all cache.)"""
    simulation_mode = False

    def __init__(self, seq):
        self.seq = list(seq)
        self.i = 0
        self.tp_calls = 0

    def _cur(self):
        return self.seq[min(self.i, len(self.seq) - 1)]

    def get_throughput_all(self, duration=2.0):
        import math as _m
        self.tp_calls += 1
        out = {}
        for ue, spec in self._cur().items():
            attached, tput = spec[0], spec[1]
            if attached and tput is not None:
                try:
                    f = float(tput)
                    if _m.isfinite(f):
                        out[ue] = f
                except (TypeError, ValueError):
                    pass
        return out

    def collect_all(self):
        entry = self._cur()
        self.i += 1
        return {ue: _Metric(*spec) for ue, spec in entry.items()}


def _intent(target=4.0) -> Intent:
    return Intent(type=IntentType.THROUGHPUT_GOAL,
                  target=IntentTarget(kpi_name="throughput",
                                      constraint_type=ConstraintType.MIN,
                                      target_value=target, unit="Mbps"))


def _make_coordinator(collector, tau=0.3):
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.gui = None
    c.on_state_change = None
    c.on_negotiation_needed = None
    c.current_state = "S0"
    c.history_reservoir = []
    c.max_history = 100
    c.tau_trial_s = tau
    c.hard_failure_cap_s = 0.15
    c._pre_trial_attached = set()
    c._last_throughput = {}
    c._last_tp_time = 0.0
    c.ue_collector = collector
    return c


class ValidateTrialPollingTest(unittest.TestCase):

    def test_trajectory_min_and_after(self):
        # samples walk 5.0 -> 3.0 -> 4.0 (then 4.0 repeats): the estimator
        # must see the in-window dip, not just the final sample.
        seq = [{"ue1": (True, 5.0)}, {"ue1": (True, 3.0)}, {"ue1": (True, 4.0)}]
        col = _SeqCollector(seq)
        c = _make_coordinator(col, tau=0.3)
        c._pre_trial_attached = {"ue1"}
        v = c._validate_trial(_intent(target=4.0), [])
        self.assertAlmostEqual(v["tput_min"], 3.0)
        self.assertAlmostEqual(v["tput_after"], 4.0)
        self.assertGreaterEqual(len(v["trajectory"]), 3)
        self.assertFalse(v["hard_failure"])
        self.assertEqual(v["reconnection_time"], 0.0)
        # Batch F: ONE authoritative live probe per window sample (not a single
        # pre-window refresh) - each sample is an independent measurement.
        self.assertEqual(col.tp_calls, len(v["trajectory"]))

    def test_hard_failure_two_consecutive_bad_samples_capped(self):
        # ue1 attached at S3 entry, then detached for the rest of the window
        # and never reattaches -> hard failure, reconnection capped.
        seq = [{"ue1": (True, 5.0)}, {"ue1": (False, None)}]
        col = _SeqCollector(seq)
        c = _make_coordinator(col, tau=0.3)
        c._pre_trial_attached = {"ue1"}
        # LEGACY direct-call contract: measure reconnection in-window. The
        # default-safe P0-3 path (stop_on_hard_failure=True) instead stops and
        # rolls back first; post-restore recovery is covered in
        # test_hard_failure_recovery.py.
        v = c._validate_trial(_intent(target=1.0), [], stop_on_hard_failure=False)
        self.assertTrue(v["hard_failure"])
        self.assertFalse(v["all_satisfied"])   # hard failure never validates
        self.assertAlmostEqual(v["reconnection_time"],
                               c.hard_failure_cap_s, places=6)

    def test_single_bad_sample_is_not_hard_failure(self):
        # one bad sample (streak < 2) must not trigger a hard failure
        seq = [{"ue1": (True, 5.0)}, {"ue1": (False, None)},
               {"ue1": (True, 5.0)}]
        col = _SeqCollector(seq)
        c = _make_coordinator(col, tau=0.3)
        c._pre_trial_attached = {"ue1"}
        v = c._validate_trial(_intent(target=4.0), [])
        self.assertFalse(v["hard_failure"])

    def test_tau_zero_still_samples_once(self):
        col = _SeqCollector([{"ue1": (True, 5.0)}])
        c = _make_coordinator(col, tau=0.0)
        c._pre_trial_attached = {"ue1"}
        v = c._validate_trial(_intent(target=4.0), [])
        self.assertEqual(len(v["trajectory"]), 1)
        self.assertAlmostEqual(v["tput_min"], 5.0)
        self.assertAlmostEqual(v["tput_after"], 5.0)

    def test_blocking_pre_measurement_does_not_consume_window(self):
        # get_throughput_all blocks 0.1s; the window clock must start AFTER
        # it, so a tau=0.2 window still gets multiple polls.
        class _SlowTP(_SeqCollector):
            def get_throughput_all(self, duration=2.0):
                import time as _t
                _t.sleep(0.1)
                return super().get_throughput_all(duration)

        col = _SlowTP([{"ue1": (True, 5.0)}, {"ue1": (True, 3.0)},
                       {"ue1": (True, 4.0)}])
        c = _make_coordinator(col, tau=0.2)
        c._pre_trial_attached = {"ue1"}
        v = c._validate_trial(_intent(target=4.0), [])
        self.assertGreaterEqual(len(v["trajectory"]), 3)
        self.assertAlmostEqual(v["tput_min"], 3.0)

    def test_attached_but_no_throughput_is_measurement_invalid(self):
        # Gate A [C5]: an ATTACHED UE with no usable throughput is a
        # TELEMETRY failure (iperf/SSH/parser), not a session loss. The
        # trial still fails closed (all_satisfied False -> rollback), but it
        # must NOT be classified hard_failure / start a reconnection timer -
        # that would pollute C_hard with non-RRC events.
        seq = [{"ue1": (True, 5.0)}, {"ue1": (True, None)}]
        col = _SeqCollector(seq)
        c = _make_coordinator(col, tau=0.3)
        c._pre_trial_attached = {"ue1"}
        v = c._validate_trial(_intent(target=1.0), [])
        self.assertFalse(v["hard_failure"])
        self.assertTrue(v["measurement_invalid"])
        self.assertFalse(v["all_satisfied"])   # fail-closed: still rolls back
        self.assertEqual(v["reconnection_time"], 0.0)

    def test_nan_throughput_is_measurement_invalid(self):
        seq = [{"ue1": (True, 5.0)}, {"ue1": (True, float("nan"))}]
        col = _SeqCollector(seq)
        c = _make_coordinator(col, tau=0.3)
        c._pre_trial_attached = {"ue1"}
        v = c._validate_trial(_intent(target=1.0), [])
        self.assertFalse(v["hard_failure"])
        self.assertTrue(v["measurement_invalid"])
        self.assertFalse(v["all_satisfied"])

    def test_transient_single_unknown_fails_closed(self):
        # Batch F (review A): a SINGLE transient authoritative UNKNOWN IN_WINDOW
        # sample (OK, UNKNOWN, OK) must fail the window CLOSED - it can never be
        # smoothed into a "satisfied throughout window" claim. It is a TELEMETRY
        # UNKNOWN (the UE stays attached), NOT a session-loss hard_failure.
        seq = [{"ue1": (True, 5.0)}, {"ue1": (True, None)},
               {"ue1": (True, 5.0)}]
        col = _SeqCollector(seq)
        c = _make_coordinator(col, tau=0.3)
        c._pre_trial_attached = {"ue1"}
        v = c._validate_trial(_intent(target=4.0), [])
        self.assertTrue(v["measurement_invalid"])   # any UNKNOWN -> invalid
        self.assertFalse(v["all_satisfied"])         # fail closed
        self.assertFalse(v["hard_failure"])          # attached -> not a detach

    def test_detach_is_hard_failure_not_invalid(self):
        seq = [{"ue1": (True, 5.0)}, {"ue1": (False, None)}]
        col = _SeqCollector(seq)
        c = _make_coordinator(col, tau=0.3)
        c._pre_trial_attached = {"ue1"}
        v = c._validate_trial(_intent(target=1.0), [])
        self.assertTrue(v["hard_failure"])
        self.assertFalse(v["measurement_invalid"])

    def test_reattach_in_window_measures_reconnection(self):
        seq = [{"ue1": (True, 5.0)}, {"ue1": (False, None)},
               {"ue1": (False, None)}, {"ue1": (True, 5.0)}]
        col = _SeqCollector(seq)
        c = _make_coordinator(col, tau=0.4)
        c.hard_failure_cap_s = 5.0
        c._pre_trial_attached = {"ue1"}
        # legacy in-window reconnection contract (P0-3 default stops instead)
        v = c._validate_trial(_intent(target=4.0), [], stop_on_hard_failure=False)
        self.assertTrue(v["hard_failure"])
        self.assertGreater(v["reconnection_time"], 0.0)
        self.assertLess(v["reconnection_time"], 5.0)   # not capped

    def test_staggered_second_failure_reopens_outage(self):
        # ue1 fails and recovers (reattach latched), then ue2 fails and
        # stays down: the latched reattach must be reset - the outage is
        # only over when EVERY failed UE is back (else reconnection_time
        # would reflect just the first UE's recovery).
        seq = [
            {"ue1": (True, 5.0), "ue2": (True, 5.0)},
            {"ue1": (False, None), "ue2": (True, 5.0)},
            {"ue1": (False, None), "ue2": (True, 5.0)},   # ue1 hard-fails
            {"ue1": (True, 5.0), "ue2": (True, 5.0)},     # ue1 recovers
            {"ue1": (True, 5.0), "ue2": (False, None)},
            {"ue1": (True, 5.0), "ue2": (False, None)},   # ue2 hard-fails
        ]
        col = _SeqCollector(seq)
        c = _make_coordinator(col, tau=0.4)
        c.hard_failure_cap_s = 0.15
        c._pre_trial_attached = {"ue1", "ue2"}
        # legacy in-window reconnection contract (P0-3 default stops instead)
        v = c._validate_trial(_intent(target=4.0), [], stop_on_hard_failure=False)
        self.assertTrue(v["hard_failure"])
        # ue2 never reattaches -> capped, NOT the small ue1-recovery delta
        self.assertAlmostEqual(v["reconnection_time"],
                               c.hard_failure_cap_s, places=6)

    def test_reattach_without_telemetry_stops_reconnection_clock(self):
        # detach x2 (hard failure), then ATTACHED again but the throughput
        # probe is still broken: the reconnection clock stops at
        # reattachment - a post-recovery telemetry failure must not inflate
        # C_hard (it surfaces as measurement_invalid instead).
        seq = [{"ue1": (True, 5.0)}, {"ue1": (False, None)},
               {"ue1": (False, None)}, {"ue1": (True, None)}]
        col = _SeqCollector(seq)
        c = _make_coordinator(col, tau=0.4)
        c.hard_failure_cap_s = 5.0
        c._pre_trial_attached = {"ue1"}
        # legacy in-window reconnection contract (P0-3 default stops instead)
        v = c._validate_trial(_intent(target=4.0), [], stop_on_hard_failure=False)
        self.assertTrue(v["hard_failure"])
        self.assertLess(v["reconnection_time"], 1.0)   # measured, not capped
        self.assertTrue(v["measurement_invalid"])      # probe loss surfaced


class TrialStatsInResultTest(unittest.TestCase):

    def _wire(self, c):
        intent = _intent(target=4.0)
        c.intent_manager = type("IM", (), {"get_active": lambda s: [],
                                           "get_monitored": lambda s: [],
                                           "add": lambda s, i: None})()
        c.calibrator = type("Cal", (), {
            "get_theta_star": lambda s: 0.5,
            "can_negotiate_more": lambda s, r, phase=None: r < 2,
            "record_episode": lambda s, m: None})()
        c.negotiation_policy = None
        c.generate_alternatives_fn = lambda i, r: []
        c._parse_intent = lambda text: {"intent": intent, "raw": {}}
        c._check_conflicts = lambda new, active: True
        c._get_network_state = lambda: NetworkState(ue_states={})
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=0.9, reasoning="", alternatives=[])
        return c

    def test_result_carries_trial_stats_and_hard_failure(self):
        col = _SeqCollector([{"ue1": (True, 5.5)}])
        c = self._wire(_make_coordinator(col, tau=0.1))
        c._execute_trial = lambda feas: {"success": True, "snapshot": {},
                                         "clipped": [], "applied": []}
        c._validate_trial = lambda ni, ai: {
            "all_satisfied": False, "metrics": {}, "trajectory": [],
            "tput_min": 1.6, "tput_after": 5.5,
            "hard_failure": True, "reconnection_time": 12.5}
        c._rollback = lambda snapshot: None
        result = c.process_intent("throughput >= 4 Mbps")
        self.assertAlmostEqual(result["trial_stats"]["tput_before"], 5.5)
        self.assertAlmostEqual(result["trial_stats"]["tput_min"], 1.6)
        self.assertAlmostEqual(result["trial_stats"]["tput_after"], 5.5)
        self.assertAlmostEqual(result["trial_stats"]["tau"], 0.1)
        self.assertTrue(result["hard_failure"])
        self.assertAlmostEqual(result["reconnection_time"], 12.5)

    def test_apply_failure_has_zero_duration_stats(self):
        col = _SeqCollector([{"ue1": (True, 5.5)}])
        c = self._wire(_make_coordinator(col, tau=0.1))
        c._execute_trial = lambda feas: {"success": False, "snapshot": {},
                                         "clipped": [], "applied": []}
        result = c.process_intent("throughput >= 4 Mbps")
        self.assertAlmostEqual(result["trial_stats"]["tau"], 0.0)
        self.assertAlmostEqual(result["trial_stats"]["tput_min"], 5.5)

    def test_result_carries_measurement_invalid(self):
        # Gate A [C5]: the flag flows validation -> result (-> calibrator)
        col = _SeqCollector([{"ue1": (True, 5.5)}])
        c = self._wire(_make_coordinator(col, tau=0.1))
        c._execute_trial = lambda feas: {"success": True, "snapshot": {},
                                         "clipped": [], "applied": []}
        c._validate_trial = lambda ni, ai: {
            "all_satisfied": False, "metrics": {}, "trajectory": [],
            "tput_min": 0.0, "tput_after": 0.0,
            "hard_failure": False, "reconnection_time": 0.0,
            "measurement_invalid": True}
        c._rollback = lambda snapshot: None
        result = c.process_intent("throughput >= 4 Mbps")
        self.assertTrue(result["measurement_invalid"])
        self.assertFalse(result["hard_failure"])

    def test_episode_total_latency_measured_at_public_boundary(self):
        # P1-3 (blocker 2): the episode total is a MEASURED monotonic float
        # stamped at the public single-flight boundary, never the int-0 sentinel.
        import math
        col = _SeqCollector([{"ue1": (True, 5.5)}])
        c = self._wire(_make_coordinator(col, tau=0.1))
        c._execute_trial = lambda feas: {"success": True, "snapshot": {},
                                         "clipped": [], "applied": []}
        c._validate_trial = lambda ni, ai: {
            "all_satisfied": True, "metrics": {}, "trajectory": [],
            "tput_min": 5.5, "tput_after": 5.5}
        c._rollback = lambda snapshot: None
        result = c.process_intent("throughput >= 4 Mbps")
        lat = result["latency_ms"]
        self.assertIsInstance(lat, float)             # measured, not int 0
        self.assertTrue(math.isfinite(lat))
        self.assertGreaterEqual(lat, 0.0)

    def test_reentry_rejection_latency_is_measured_not_zero_int(self):
        # P1-3 (blocker 2): a rejected re-entry gets an ACTUAL measured float
        # (stamped after finalization), never the hard-coded int 0.
        import math
        col = _SeqCollector([{"ue1": (True, 5.5)}])
        c = self._wire(_make_coordinator(col, tau=0.1))
        c._episode_in_flight = True                   # force the reject path
        result = c.process_intent_text("throughput >= 4 Mbps")
        self.assertEqual(result["resolution"], "reject")
        lat = result["latency_ms"]
        self.assertIsInstance(lat, float)             # NOT the int-0 sentinel
        self.assertTrue(math.isfinite(lat) and lat >= 0.0)

class InferenceExceptionTimerP13Test(unittest.TestCase):
    """P1-3 (blocker 1/3): a REAL production-method counterexample - a raising
    bounded model call still records the inference timer, and the honest
    proposal-stage stamp preserves it with proposal_generated=False / id None
    while keeping the real episode/cycle ids."""

    def test_inference_timer_survives_bounded_call_exception_then_honest_stamp(self):
        import math
        import types
        c = IntentCoordinator.__new__(IntentCoordinator)
        # minimal production wiring (NOT the _wire feasibility stub):
        c._history_for_prompt = lambda ni: []            # zero-history prompt
        c.llm_manager = types.SimpleNamespace(
            build_feasibility_prompt_hash=lambda *a, **k: "a" * 64)  # fixed hash

        def _boom(fn, timeout_s, label=None):
            raise RuntimeError("bounded-call boom")       # model timeout/exception
        c._bounded_call = _boom
        # the REAL _analyze_feasibility must raise, but its finally records the
        # measured inference latency.
        with self.assertRaises(RuntimeError):
            c._analyze_feasibility(_intent(target=4.0), [],
                                   NetworkState(ue_states={}))
        self.assertIsInstance(c._cur_inference_ms, float)
        self.assertTrue(math.isfinite(c._cur_inference_ms))
        self.assertGreaterEqual(c._cur_inference_ms, 0.0)
        # the REAL proposal-stage stamp on a cycle carrying real ids: no proposal
        # was generated -> inference_ms preserved, proposal_generated False,
        # proposal_id None, and the existing episode/cycle ids are untouched.
        c._cur_proposal_generated = False
        c._cur_schema_valid = None
        c._cur_schema_reason = None
        cycle = {"episode_id": "ep-real", "cycle_id": "cyc-real"}
        c._stamp_proposal_stage(cycle)
        self.assertEqual(cycle["inference_ms"], c._cur_inference_ms)   # preserved
        self.assertFalse(cycle["proposal_generated"])
        self.assertIsNone(cycle["proposal_id"])
        self.assertEqual(cycle["episode_id"], "ep-real")   # existing ids kept
        self.assertEqual(cycle["cycle_id"], "cyc-real")


class ThroughputCacheValidityTest(unittest.TestCase):
    """Gate A [C5]: a stale iperf3 cache must not mask a fresh measurement
    failure - merge only within the validity window (2x tp_interval_s)."""

    def _cached_coordinator(self):
        c = _make_coordinator(_SeqCollector([]), tau=0.1)
        c.tp_interval_s = 15.0
        c._update_throughput_cache({"ue1": 7.5})
        return c

    def test_fresh_cache_merges(self):
        c = self._cached_coordinator()
        m = _Metric(attached=True, tput=None)
        c._merge_throughput({"ue1": m})
        self.assertEqual(m.throughput_mbps, 7.5)

    def test_stale_cache_does_not_merge(self):
        c = self._cached_coordinator()
        c._last_tput_ts["ue1"] -= 31.0     # older than 2 x 15 s
        m = _Metric(attached=True, tput=None)
        c._merge_throughput({"ue1": m})
        self.assertIsNone(m.throughput_mbps)

    def test_measured_value_never_overwritten(self):
        c = self._cached_coordinator()
        m = _Metric(attached=True, tput=9.9)
        c._merge_throughput({"ue1": m})
        self.assertEqual(m.throughput_mbps, 9.9)

    def test_uncached_ue_untouched(self):
        c = self._cached_coordinator()
        m = _Metric(attached=True, tput=None)
        c._merge_throughput({"ue2": m})
        self.assertIsNone(m.throughput_mbps)


class MeasurementInvalidCostExclusionTest(unittest.TestCase):
    """Gate A [C5]: measurement-invalid trials must not contaminate the
    cost estimates their untrusted trajectories would feed."""

    def _ep(self, **kw):
        base = dict(trial_id=0, method="m", phase="P1")
        base.update(kw)
        return EpisodeRecord(**base)

    def test_invalid_trial_excluded_from_c_cont(self):
        eps = [
            self._ep(trial_executed=True, trial_success=False,
                     rolled_back=True, throughput_before=5.0,
                     throughput_trial_min=1.0, tau_trial=10.0),
            self._ep(trial_executed=True, trial_success=False,
                     rolled_back=True, measurement_invalid=True,
                     throughput_before=5.0, throughput_trial_min=0.0,
                     tau_trial=10.0),
        ]
        cost = estimate_costs(eps, IntentConfig())
        self.assertEqual(cost["samples"]["n_failed"], 1)
        self.assertEqual(cost["samples"]["n_invalid"], 1)
        self.assertEqual(cost["samples"]["n_trials"], 2)
        # only the trusted trial contributes: (5.0 - 1.0) * 10.0
        self.assertAlmostEqual(cost["c_cont"], 40.0)

    def test_invalid_trial_excluded_from_r_success(self):
        eps = [
            self._ep(trial_executed=True, trial_success=True,
                     throughput_before=5.0, throughput_after=9.0,
                     tau_trial=10.0),
            self._ep(trial_executed=True, trial_success=True,
                     measurement_invalid=True, throughput_before=5.0,
                     throughput_after=90.0, tau_trial=10.0),
        ]
        cost = estimate_costs(eps, IntentConfig())
        self.assertEqual(cost["samples"]["n_success"], 1)
        self.assertAlmostEqual(cost["r_success"], 40.0)   # (9-5)*10 only

    def test_hard_failure_sample_kept_when_also_invalid(self):
        # detach + reconnection_time is a REAL observation even when the
        # throughput probe also failed: C_hard keeps it, C_cont drops it.
        eps = [self._ep(trial_executed=True, trial_success=False,
                        rolled_back=True, hard_failure=True,
                        measurement_invalid=True, throughput_before=5.0,
                        reconnection_time=10.0, tau_trial=10.0)]
        cost = estimate_costs(eps, IntentConfig())
        self.assertEqual(cost["samples"]["n_hard"], 1)
        self.assertEqual(cost["samples"]["n_failed"], 0)
        self.assertAlmostEqual(cost["c_hard"], 50.0)      # 5.0 * 10.0

    def test_cost_metrics_bridges_measurement_invalid(self):
        from calibration.adaptive_calibrator import CostMetrics
        rec = CostMetrics(trial_executed=True,
                          measurement_invalid=True).to_episode_record()
        self.assertTrue(rec.measurement_invalid)
        rec2 = CostMetrics(trial_executed=True).to_episode_record()
        self.assertFalse(rec2.measurement_invalid)


class RunnerEpisodeMappingTest(unittest.TestCase):

    def test_episode_filled_from_trial_and_nego_stats(self):
        trace = {"routed_to": "trial", "trial_executed": True,
                 "rolled_back": True, "entered_negotiation": True,
                 "sequence": ["S3", "S4", "S5", "S6"]}
        result = {
            "success": True, "trial_success": False,
            "feasibility": {"confidence": 0.8, "feasible": True},
            "trial_stats": {"tput_before": 5.5, "tput_min": 1.6,
                            "tput_after": 5.0, "tau": 15.0},
            "nego_stats": {"rounds": 2, "duration_s": 10.0,
                           "tput_during_nego": 5.5,
                           "terminated_by": "accept"},
            "hard_failure": True, "reconnection_time": 3.0,
            "measurement_invalid": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExperimentRunner(coordinator=None, output_dir=tmp)
            [ep] = runner._episode_from_result("m", 0, "P1", result, trace,
                                               5.5)
        self.assertEqual(ep.negotiation_rounds, 2)
        self.assertAlmostEqual(ep.nego_duration, 10.0)
        self.assertAlmostEqual(ep.throughput_during_nego, 5.5)
        self.assertAlmostEqual(ep.throughput_after, 5.0)
        self.assertAlmostEqual(ep.throughput_trial_min, 1.6)
        self.assertAlmostEqual(ep.tau_trial, 15.0)
        self.assertTrue(ep.hard_failure)
        self.assertTrue(ep.measurement_invalid)
        self.assertAlmostEqual(ep.reconnection_time, 3.0)
        self.assertIs(ep.trial_success, False)

    def test_per_cycle_records_from_cycles_list(self):
        # C1: one EpisodeRecord per coordination cycle, each with its own
        # confidence/outcome; `success` carries the episode resolution.
        # P1-3: inference_ms is the REAL measured proposal-inference latency of
        # each cycle (threaded verbatim from cyc["inference_ms"]), NOT a total-
        # episode latency divided across cycles; total_latency_ms is the ACTUAL
        # measured episode total, attributed once (cycle 0).
        result = {
            "success": True, "latency_ms": 200.0,
            "cycles": [
                {"cycle": 0, "confidence": 0.9, "feasible": True,
                 "theta_star": 0.5, "routed_to": "trial",
                 "trial_success": False, "rolled_back": True,
                 "restore_verified": True, "inference_ms": 120.0,
                 "trial_stats": {"tput_before": 5.5, "tput_min": 1.6,
                                 "tput_after": 5.0, "tau": 15.0},
                 "nego_stats": {"rounds": 1, "duration_s": 2.0,
                                "tput_during_nego": 5.5,
                                "terminated_by": "accept"}},
                {"cycle": 1, "confidence": 0.8, "feasible": True,
                 "theta_star": 0.5, "routed_to": "trial",
                 "trial_success": True, "inference_ms": 80.0,
                 "trial_stats": {"tput_before": 5.5, "tput_min": 5.4,
                                 "tput_after": 7.0, "tau": 15.0}},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExperimentRunner(coordinator=None, output_dir=tmp)
            eps = runner._episode_from_result("m", 0, "P1", result, {}, 5.5)
        self.assertEqual(len(eps), 2)
        first, second = eps
        self.assertEqual(first.cycle_idx, 0)
        self.assertIs(first.trial_success, False)
        self.assertTrue(first.rolled_back)
        self.assertTrue(first.entered_negotiation)
        self.assertIs(first.restore_verified, True)
        self.assertTrue(first.success)   # episode resolution on every record
        self.assertEqual(second.cycle_idx, 1)
        self.assertIs(second.trial_success, True)
        self.assertFalse(second.entered_negotiation)
        self.assertAlmostEqual(second.throughput_after, 7.0)
        self.assertAlmostEqual(first.confidence, 0.9)
        self.assertAlmostEqual(second.confidence, 0.8)
        # REAL per-cycle inference latency threaded verbatim (never a total split)
        self.assertAlmostEqual(first.inference_ms, 120.0)
        self.assertAlmostEqual(second.inference_ms, 80.0)
        # the ACTUAL measured episode total is attributed once (cycle 0), None else
        self.assertAlmostEqual(first.total_latency_ms, 200.0)
        self.assertIsNone(second.total_latency_ms)
        self.assertAlmostEqual(first.total_ms, 200.0)

    def test_coordinator_s3_baseline_takes_precedence(self):
        trace = {"routed_to": "trial", "trial_executed": True,
                 "rolled_back": False, "entered_negotiation": False,
                 "sequence": ["S3", "S4", "S6"]}
        result = {"success": True, "trial_success": True,
                  "feasibility": {},
                  "trial_stats": {"tput_before": 6.6, "tput_min": 6.0,
                                  "tput_after": 9.0, "tau": 15.0}}
        with tempfile.TemporaryDirectory() as tmp:
            runner = ExperimentRunner(coordinator=None, output_dir=tmp)
            [ep] = runner._episode_from_result("m", 0, "P1", result, trace,
                                               5.5)
            self.assertAlmostEqual(ep.throughput_before, 6.6)
            # absent trial_stats falls back to the runner's measurement
            [ep2] = runner._episode_from_result("m", 0, "P1",
                                                {"success": True,
                                                 "feasibility": {}},
                                                trace, 5.5)
            self.assertAlmostEqual(ep2.throughput_before, 5.5)
            # a MEASURED 0.0 baseline is valid and must NOT fall back
            result_zero = {"success": True, "feasibility": {},
                           "trial_stats": {"tput_before": 0.0,
                                           "tput_min": 0.0,
                                           "tput_after": 0.0, "tau": 15.0}}
            [ep3] = runner._episode_from_result("m", 0, "P1", result_zero,
                                                trace, 5.5)
            self.assertAlmostEqual(ep3.throughput_before, 0.0)
            # unmeasured (None) falls back
            result_none = {"success": True, "feasibility": {},
                           "trial_stats": {"tput_before": None,
                                           "tput_min": 0.0,
                                           "tput_after": 0.0, "tau": 0.0}}
            [ep4] = runner._episode_from_result("m", 0, "P1", result_none,
                                                trace, 5.5)
            self.assertAlmostEqual(ep4.throughput_before, 5.5)


class DegenerateCostDetectionTest(unittest.TestCase):

    def _ep(self, **kw):
        base = dict(trial_id=0, method="m", phase="P1")
        base.update(kw)
        return EpisodeRecord(**base)

    def test_all_zero_samples_flagged_degenerate(self):
        # trials exist but every trajectory field is a MEASURED 0 (the unfilled-
        # pipeline failure mode): samples are present AND all zero. The throughput
        # fields are Optional (None == UNKNOWN, excluded), so a measured-zero
        # degenerate sample must be stated EXPLICITLY as 0.0, not left defaulted.
        eps = [self._ep(trial_executed=True, trial_success=True,
                        throughput_before=0.0, throughput_after=0.0,
                        throughput_trial_min=0.0),
               self._ep(trial_executed=True, trial_success=False,
                        rolled_back=True, throughput_before=0.0,
                        throughput_after=0.0, throughput_trial_min=0.0)]
        cost = estimate_costs(eps, IntentConfig())
        self.assertTrue(cost["degenerate"]["r_success"])
        self.assertTrue(cost["degenerate"]["c_cont"])
        self.assertTrue(cost["derived"]["r_success"])   # honest but flagged

    def test_measured_samples_not_flagged(self):
        eps = [self._ep(trial_executed=True, trial_success=True,
                        throughput_before=5.0, throughput_after=9.0,
                        tau_trial=10.0),
               self._ep(trial_executed=True, trial_success=False,
                        rolled_back=True, throughput_before=5.0,
                        throughput_trial_min=2.0, tau_trial=10.0)]
        cost = estimate_costs(eps, IntentConfig())
        self.assertFalse(cost["degenerate"]["r_success"])
        self.assertFalse(cost["degenerate"]["c_cont"])

    def test_no_samples_not_flagged_degenerate(self):
        # empty sample sets fall back to priors (derived=False) - that is a
        # DIFFERENT honesty flag, not degeneracy
        cost = estimate_costs([], IntentConfig())
        self.assertFalse(any(cost["degenerate"].values()))
        self.assertFalse(any(cost["derived"].values()))


if __name__ == "__main__":
    unittest.main()
