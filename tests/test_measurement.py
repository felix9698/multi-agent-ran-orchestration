#!/usr/bin/env python3
"""Batch F (P0-13 / P0-14): live-measurement contract, scheduler, KPI semantics.

No real subprocess/network: deterministic blocking/failing probe fakes + an
injectable epoch clock. Covers every named counterexample.
"""

import hashlib
import threading
import time
import unittest

from coordinator.measurement import (
    MeasurementSample, MeasurementPurpose, MeasurementStatus, MeasurementError,
    MeasurementScheduler, MeasurementBusy, ProbeConfig, ProbeMode,
    capacity_offered_load_ok, new_probe_id,
)
from coordinator.intent_coordinator import IntentCoordinator
from collectors.multi_ue_collector import UEMetrics
from config import ExperimentConfig


# ---------------------------------------------------------------------------
# MeasurementSample / ProbeConfig contract
# ---------------------------------------------------------------------------
class SampleContractTest(unittest.TestCase):

    def test_ok_sample_is_finite_and_jsonsafe(self):
        import json
        s = MeasurementSample.ok(metric="ue_throughput_mbps", value=8.5,
                                 purpose="in_window", source="iperf3_udp_dl",
                                 sample_time=100.0, probe_id="p1")
        json.dumps(s.to_dict())
        self.assertFalse(s.is_unknown())
        self.assertEqual(s.status, MeasurementStatus.OK)

    def test_unknown_is_explicit_never_zero(self):
        s = MeasurementSample.unknown(metric="ue_throughput_mbps",
                                      purpose="in_window", source="iperf3",
                                      sample_time=1.0, probe_id="p2",
                                      error="probe failed")
        self.assertTrue(s.is_unknown())
        self.assertIsNone(s.value)                 # None, NOT 0
        self.assertEqual(s.to_dict()["value"], None)

    def test_nan_inf_bool_value_rejected(self):
        for bad in (float("nan"), float("inf"), True):
            with self.assertRaises(MeasurementError):
                MeasurementSample.ok(metric="m", value=bad, purpose="in_window",
                                     source="s", sample_time=1.0, probe_id="p")

    def test_unknown_with_value_rejected(self):
        with self.assertRaises(MeasurementError):
            MeasurementSample(metric="m", value=3.0,
                              status=MeasurementStatus.UNKNOWN,
                              purpose="in_window", source="s",
                              sample_time=1.0, probe_id="p")

    def test_post_action_and_admissibility(self):
        pre = MeasurementSample.ok(metric="m", value=5.0, purpose="pre_action",
                                   source="s", sample_time=200.0, probe_id="p")
        win = MeasurementSample.ok(metric="m", value=5.0, purpose="in_window",
                                   source="s", sample_time=200.0, probe_id="p",
                                   freshness="fresh", cache_status="live")
        # a pre-action sample is NEVER post-action (even if later in time)
        self.assertFalse(pre.is_post_action(100.0))
        self.assertFalse(pre.admissible_for_commit(100.0))
        # an in-window fresh live sample at/after apply time IS admissible
        self.assertTrue(win.is_post_action(100.0))
        self.assertTrue(win.admissible_for_commit(100.0))
        # a pre-action-time (before apply) in-window sample is NOT post-action
        stale = MeasurementSample.ok(metric="m", value=5.0, purpose="in_window",
                                     source="s", sample_time=50.0, probe_id="p",
                                     freshness="fresh", cache_status="live")
        self.assertFalse(stale.admissible_for_commit(100.0))
        # a cached sample is not admissible (and cached can never be 'fresh')
        cached = MeasurementSample.ok(metric="m", value=5.0, purpose="in_window",
                                      source="s", sample_time=200.0,
                                      probe_id="p", freshness="stale",
                                      cache_status="cached")
        self.assertFalse(cached.admissible_for_commit(100.0))
        # a NEGOTIATION sample is NEVER admissible for commit (purpose gate)
        nego = MeasurementSample.ok(metric="m", value=5.0, purpose="negotiation",
                                    source="s", sample_time=200.0, probe_id="p",
                                    freshness="fresh", cache_status="live")
        self.assertFalse(nego.admissible_for_commit(100.0))

    def test_probe_config_validation_rejects_bad_numbers(self):
        for bad in (0, -1, float("nan"), float("inf"), True):
            with self.assertRaises(MeasurementError):
                ProbeConfig(offered_load_mbps=bad)
        with self.assertRaises(MeasurementError):
            ProbeConfig(offered_load_mbps=12.0, direction="sideways")


class CapacityModeTest(unittest.TestCase):

    def test_capacity_metric_rejects_non_saturating_probe(self):
        # a 12 Mbps goodput probe is fine at its offered load...
        good = ProbeConfig(offered_load_mbps=12.0, mode=ProbeMode.GOODPUT)
        self.assertTrue(capacity_offered_load_ok(good, 100.0))
        # ...but a CAPACITY claim at 12 Mbps offered load (< 100 floor) is invalid
        cap = ProbeConfig(offered_load_mbps=12.0, mode=ProbeMode.CAPACITY)
        self.assertFalse(capacity_offered_load_ok(cap, 100.0))
        # a saturating capacity probe is accepted
        sat = ProbeConfig(offered_load_mbps=150.0, mode=ProbeMode.CAPACITY)
        self.assertTrue(capacity_offered_load_ok(sat, 100.0))


# ---------------------------------------------------------------------------
# Shared scheduler
# ---------------------------------------------------------------------------
class SchedulerTest(unittest.TestCase):

    def test_measurement_scheduler_prevents_overlapping_iperf(self):
        sched = MeasurementScheduler(default_timeout_s=0.2)
        entered = threading.Event()
        release = threading.Event()
        overlaps = []

        def _bg():
            with sched.probe("background"):
                entered.set()
                overlaps.append(sched.peak_concurrency)
                release.wait(timeout=3.0)

        t = threading.Thread(target=_bg)
        t.start()
        self.assertTrue(entered.wait(timeout=3.0))
        # while the background probe holds it, an S4 probe CANNOT acquire within
        # the bound -> fail-closed MeasurementBusy (never overlaps).
        with self.assertRaises(MeasurementBusy):
            with sched.probe("s4", timeout_s=0.2):
                pass
        release.set()
        t.join(timeout=3.0)
        self.assertEqual(sched.peak_concurrency, 1)     # never 2

    def test_scheduler_serializes_without_deadlock(self):
        sched = MeasurementScheduler(default_timeout_s=2.0)
        order = []

        def _probe(name, hold):
            with sched.probe(name):
                order.append(("enter", name))
                time.sleep(hold)
                order.append(("exit", name))

        a = threading.Thread(target=_probe, args=("a", 0.1))
        b = threading.Thread(target=_probe, args=("b", 0.1))
        a.start(); b.start(); a.join(timeout=5); b.join(timeout=5)
        self.assertFalse(a.is_alive() or b.is_alive())   # no deadlock
        self.assertEqual(sched.peak_concurrency, 1)      # never concurrent


# ---------------------------------------------------------------------------
# Coordinator measurement helpers (injectable probe + clock; no network)
# ---------------------------------------------------------------------------
def _meas_coord(now=None, probe_fn=None, cfg=None, floor=100.0):
    c = IntentCoordinator.__new__(IntentCoordinator)
    c._measurement_scheduler = MeasurementScheduler(default_timeout_s=0.2)
    c._probe_config = cfg or ProbeConfig()
    c._now = now or (lambda: 1000.0)
    c._throughput_probe_fn = probe_fn
    c.capacity_saturation_floor_mbps = floor
    c.probe_lock_timeout_s = 0.2

    class _Col:
        probe_config = None

        def get_throughput_all(self, duration=2.0):
            return {}

        def set_probe_config(self, cfg):
            self.probe_config = cfg
    c.ue_collector = _Col()
    return c


class CoordinatorProbeTest(unittest.TestCase):

    def test_probe_failure_returns_unknown_not_cached_success(self):
        def _boom(duration):
            raise RuntimeError("iperf died")
        c = _meas_coord(probe_fn=_boom)
        s = c._measure_throughput(MeasurementPurpose.IN_WINDOW,
                                  action_apply_time=1.0)
        self.assertTrue(s.is_unknown())
        self.assertIsNone(s.value)                 # UNKNOWN, not a cached number
        self.assertIn("probe failed", s.error)

    def test_empty_probe_is_unknown_not_zero(self):
        c = _meas_coord(probe_fn=lambda d: {})     # no usable per-UE data
        s = c._measure_throughput(MeasurementPurpose.IN_WINDOW,
                                  action_apply_time=1.0)
        self.assertTrue(s.is_unknown())
        self.assertIsNone(s.value)

    def test_lock_timeout_is_explicit_unknown(self):
        # the shared scheduler is held by another thread; the probe must fail
        # closed to an explicit UNKNOWN within the bound (not block / not 0).
        c = _meas_coord(probe_fn=lambda d: {"ue1": 9.0})
        held = threading.Event(); release = threading.Event()

        def _hold():
            with c._measurement_scheduler.probe("bg"):
                held.set(); release.wait(timeout=3.0)
        t = threading.Thread(target=_hold); t.start()
        self.assertTrue(held.wait(timeout=3.0))
        s = c._measure_throughput(MeasurementPurpose.IN_WINDOW,
                                  action_apply_time=1.0)
        release.set(); t.join(timeout=3.0)
        self.assertTrue(s.is_unknown())
        self.assertIn("busy", s.error.lower())

    def test_capacity_metric_rejects_non_saturating_probe_in_coord(self):
        cfg = ProbeConfig(offered_load_mbps=12.0, mode=ProbeMode.CAPACITY)
        c = _meas_coord(probe_fn=lambda d: {"ue1": 12.0}, cfg=cfg, floor=100.0)
        s = c._measure_throughput(MeasurementPurpose.IN_WINDOW,
                                  action_apply_time=1.0)
        self.assertTrue(s.is_unknown())
        self.assertIn("non-saturating", s.error)

    def test_offered_load_is_configurable_and_recorded(self):
        cfg = ProbeConfig(offered_load_mbps=50.0, protocol="udp",
                          direction="downlink", duration_s=3.0)
        c = _meas_coord(probe_fn=lambda d: {"ue1": 40.0}, cfg=cfg)
        s = c._measure_throughput(MeasurementPurpose.IN_WINDOW,
                                  action_apply_time=1.0)
        self.assertFalse(s.is_unknown())
        pc = s.to_dict()["probe_config"]
        self.assertEqual(pc["offered_load_mbps"], 50.0)
        self.assertEqual(pc["protocol"], "udp")
        self.assertEqual(pc["direction"], "downlink")
        self.assertEqual(pc["duration_s"], 3.0)

    def test_repeated_samples_have_distinct_probe_ids_and_times(self):
        times = iter([1001.0, 1002.0, 1003.0, 1004.0])
        c = _meas_coord(now=lambda: next(times),
                        probe_fn=lambda d: {"ue1": 9.0})
        s1 = c._measure_throughput(MeasurementPurpose.IN_WINDOW,
                                   action_apply_time=1.0)
        s2 = c._measure_throughput(MeasurementPurpose.IN_WINDOW,
                                   action_apply_time=1.0)
        self.assertNotEqual(s1.probe_id, s2.probe_id)   # distinct ids
        self.assertNotEqual(s1.sample_time, s2.sample_time)  # distinct times
        self.assertIsNot(s1, s2)                        # not aliases

    def test_missing_pretrial_throughput_is_not_zero(self):
        c = _meas_coord()
        # a missing baseline -> UNKNOWN PRE_ACTION sample (value None, never 0)
        s = c._baseline_sample(None)
        self.assertEqual(s.purpose, MeasurementPurpose.PRE_ACTION)
        self.assertTrue(s.is_unknown())
        self.assertIsNone(s.value)

        class _Tx:
            pass
        tx = _Tx(); tx.pre_action_baseline = s.to_dict()
        self.assertIsNone(c.baseline_value_or_none(tx))   # excluded, not 0
        # a real baseline is an OK sample and its value is usable
        ok = c._baseline_sample(7.5)
        self.assertFalse(ok.is_unknown())
        tx.pre_action_baseline = ok.to_dict()
        self.assertEqual(c.baseline_value_or_none(tx), 7.5)


# ---------------------------------------------------------------------------
# Radio KPI honest naming (P0-14)
# ---------------------------------------------------------------------------
class BackgroundVsS4SchedulerTest(unittest.TestCase):
    """The background probe and the S4 probe both go through _measure_throughput
    (the SAME shared scheduler), so they can never overlap - exercised via the
    ACTUAL production probe path, not a manual lock hold."""

    def test_background_and_s4_share_scheduler(self):
        entered = threading.Event()
        release = threading.Event()

        def _slow_probe(duration):
            entered.set()
            release.wait(timeout=3.0)
            return {"ue1": 10.0}
        c = _meas_coord(probe_fn=_slow_probe)
        c.probe_lock_timeout_s = 0.1

        bg_result = {}

        def _bg():
            # the REAL background path: _measure_throughput(BACKGROUND)
            bg_result["s"] = c._measure_throughput(
                MeasurementPurpose.BACKGROUND, label="background")
        t = threading.Thread(target=_bg)
        t.start()
        self.assertTrue(entered.wait(timeout=3.0))       # background probe is in
        # an S4 probe now CANNOT acquire the shared scheduler -> UNKNOWN (busy)
        s4 = c._measure_throughput(MeasurementPurpose.IN_WINDOW,
                                   action_apply_time=1.0)
        release.set()
        t.join(timeout=3.0)
        self.assertTrue(s4.is_unknown())                 # blocked by background
        self.assertIn("busy", s4.error.lower())
        self.assertEqual(c._measurement_scheduler.peak_concurrency, 1)
        self.assertFalse(bg_result["s"].is_unknown())    # background succeeded


class NegotiationProbeTest(unittest.TestCase):

    def test_negotiation_probe_failure_is_unknown(self):
        def _boom(duration):
            raise RuntimeError("nego iperf died")
        c = _meas_coord(probe_fn=_boom)
        s = c._measure_throughput(MeasurementPurpose.NEGOTIATION,
                                  label="nego_round_0")
        self.assertEqual(s.purpose, MeasurementPurpose.NEGOTIATION)
        self.assertTrue(s.is_unknown())
        self.assertIsNone(s.value)                       # UNKNOWN, not a 0/cache
        self.assertIn("probe failed", s.error)


class S3BaselineTest(unittest.TestCase):

    def test_s3_baseline_ignores_high_cache_on_probe_failure(self):
        # a HIGH cached collect_all value must NOT become the baseline when the
        # fresh PRE_ACTION probe FAILS: the baseline stays UNKNOWN (None) and
        # contributes no 0/high value to calibration/cost.
        from experiments.emulation import build_emulated_coordinator
        from experiments.topology import two_ue_topology   # legacy 2-UE (explicit)
        c, _ = build_emulated_coordinator(
            seed=7, tau_trial_s=0.1, topology=two_ue_topology())

        def _boom(duration):
            raise RuntimeError("iperf died")
        c._throughput_probe_fn = _boom
        # collect_all cache reports a healthy throughput
        cache = c.ue_collector.collect_all()
        try:
            cached_vals = [m.throughput_mbps for m in cache.values()
                           if m.throughput_mbps]
            self.assertTrue(cached_vals and max(cached_vals) > 5.0)
            # the REAL S3 baseline probe fails -> UNKNOWN, value None
            baseline = c._measure_throughput(MeasurementPurpose.PRE_ACTION,
                                             label="s3_baseline")
            self.assertEqual(baseline.purpose, MeasurementPurpose.PRE_ACTION)
            self.assertTrue(baseline.is_unknown())
            self.assertIsNone(baseline.value)

            class _Tx:
                pass
            tx = _Tx()
            tx.pre_action_baseline = baseline.to_dict()
            self.assertIsNone(c.baseline_value_or_none(tx))   # excluded, not 0/high
        finally:
            c.stop()


class ProbeCommandHonestyTest(unittest.TestCase):
    """#6: only the implemented (udp+downlink) combo is accepted; the actual
    probe applies the offered load / direction; capacity floor is validated and
    a 12M probe can never be a capacity measurement."""

    def test_probe_config_rejects_unsupported_protocol_direction(self):
        with self.assertRaises(MeasurementError):
            ProbeConfig(offered_load_mbps=12.0, protocol="tcp")
        with self.assertRaises(MeasurementError):
            ProbeConfig(offered_load_mbps=12.0, direction="uplink")

    def test_collector_probe_applies_offered_load_and_direction(self):
        from collectors.multi_ue_collector import MultiUECollector

        seen = {}

        class _Spy:
            def get_throughput(self, duration=2.0, direction="downlink",
                               udp_rate="12M"):
                seen["duration"] = duration
                seen["direction"] = direction
                seen["udp_rate"] = udp_rate
                return 40.0
        from concurrent.futures import ThreadPoolExecutor
        col = MultiUECollector.__new__(MultiUECollector)
        col.collectors = {"ue1": _Spy()}
        col._executor = ThreadPoolExecutor(max_workers=1)
        col.probe_config = ProbeConfig(offered_load_mbps=50.0,
                                       direction="downlink", duration_s=3.0)
        col.scheduler = None
        res = col.get_throughput_all()
        self.assertEqual(res["ue1"], 40.0)
        self.assertEqual(seen["udp_rate"], "50M")        # actual offered load
        self.assertEqual(seen["direction"], "downlink")
        self.assertEqual(seen["duration"], 3.0)

    def test_capacity_floor_validation_and_12M_rejected(self):
        from coordinator.measurement import _finite_positive
        for bad in (0, -1, float("nan"), float("inf"), True):
            with self.assertRaises(MeasurementError):
                _finite_positive("capacity_floor_mbps", bad)
        # a self-declared low floor (10) can NOT let a 12M probe claim capacity
        cap12 = ProbeConfig(offered_load_mbps=12.0, mode=ProbeMode.CAPACITY)
        self.assertFalse(capacity_offered_load_ok(cap12, 10.0))
        self.assertFalse(capacity_offered_load_ok(cap12, 100.0))
        # only a genuinely saturating load passes
        cap150 = ProbeConfig(offered_load_mbps=150.0, mode=ProbeMode.CAPACITY)
        self.assertTrue(capacity_offered_load_ok(cap150, 100.0))


class _Executor:
    """Minimal synchronous stand-in for ThreadPoolExecutor.submit."""
    def submit(self, fn, *a, **k):
        class _F:
            def __init__(self, v):
                self._v = v

            def result(self):
                return self._v
        return _F(fn(*a, **k))


class EvidenceTxBindingTest(unittest.TestCase):
    """#7: commit evidence provenance comes from the TRUSTED tx-bound tuple, not
    a mutable self._probe_cfg()/cycle a callback could swap after S4."""

    def test_commit_probe_config_from_tx_not_mutated_self(self):
        from tests.test_measurement import _meas_coord   # reuse helper
        # a coordinator whose tx carries a bound probe_config; mutating
        # self._probe_config afterwards must NOT change what a tx-derived commit
        # evidence would report.
        c = _meas_coord()
        import copy as _c

        class _Tx:
            pass
        tx = _Tx()
        tx.probe_config = _c.deepcopy(c._probe_cfg().to_dict())
        bound = tx.probe_config["offered_load_mbps"]
        # a callback swaps the live probe config to a different offered load
        c.set_probe_config(ProbeConfig(offered_load_mbps=99.0))
        # the tx-bound value is unchanged (deep-frozen copy)
        self.assertEqual(tx.probe_config["offered_load_mbps"], bound)
        self.assertNotEqual(c._probe_cfg().offered_load_mbps, bound)


class NegotiationPropagationTest(unittest.TestCase):
    """#7: an end-to-end negotiation whose probe FAILS records UNKNOWN into the
    stats (ledger) and carries the exact NEGOTIATION sample provenance."""

    def _nego_coord(self, probe_fn):
        from decision.intent_model import (
            Alternative, ConstraintType, Intent, IntentTarget, IntentType)
        from calibration.adaptive_calibrator import AdaptiveCalibrator
        c = IntentCoordinator.__new__(IntentCoordinator)
        c._measurement_scheduler = MeasurementScheduler(default_timeout_s=0.2)
        c._probe_config = ProbeConfig()
        c._now = lambda: 1000.0
        c._throughput_probe_fn = probe_fn
        c.capacity_saturation_floor_mbps = 100.0
        c.probe_lock_timeout_s = 0.2
        c.tau_trial_s = 0.0
        c.gui = None
        c.on_negotiation_needed = None
        c.negotiation_policy = lambda alt: "reject"
        c.generate_alternatives_fn = lambda i, r: []

        class _Cal:
            def get_n_max(self):
                return 2
        c.calibrator = _Cal()

        class _Col:
            def get_throughput_all(self, duration=2.0):
                return {}
        c.ue_collector = _Col()
        return c

    def test_negotiation_probe_failure_records_unknown_in_stats(self):
        from decision.intent_model import (
            ConstraintType, Intent, IntentTarget, IntentType, Alternative)

        def _boom(duration):
            raise RuntimeError("nego iperf died")
        c = self._nego_coord(_boom)
        intent = Intent(type=IntentType.THROUGHPUT_GOAL,
                        target=IntentTarget(kpi_name="throughput",
                                            constraint_type=ConstraintType.MIN,
                                            target_value=8.0, unit="Mbps"))
        alt = Alternative(id="a1", description="relax",
                          modified_intent=intent)
        res = c._negotiate(intent, [alt])
        stats = res["stats"]
        # UNKNOWN throughput -> None (never a silent 0) + flagged for the ledger
        self.assertIsNone(stats["tput_during_nego"])
        self.assertTrue(stats["tput_during_nego_unknown"])
        # the exact per-round NEGOTIATION sample provenance (UNKNOWN + error)
        self.assertTrue(stats["measurement_samples"])
        s0 = stats["measurement_samples"][0]
        self.assertEqual(s0["purpose"], "negotiation")
        self.assertEqual(s0["status"], "unknown")
        self.assertIsNone(s0["value"])
        self.assertIn("probe failed", s0["error"])


class HonestExportTest(unittest.TestCase):
    """#8: no ambiguous rsrp/sinr keys leak into the public UEMetrics export and
    a gNB-UL value is never placed in a UE-DL field."""

    def test_uemetrics_export_has_no_ambiguous_top_level_keys(self):
        from collectors.multi_ue_collector import UEMetrics
        m = UEMetrics(ue_id="ue1", attached=True, rsrp=-90.0, sinr=15.0,
                      radio_source="gnb_ul_mac", radio_direction="uplink")
        d = m.to_dict()
        self.assertNotIn("rsrp", d)                # ambiguous -> not top-level
        self.assertNotIn("sinr", d)
        self.assertNotIn("cqi", d)                 # gNB-reported -> honest name
        self.assertNotIn("mcs", d)
        self.assertNotIn("dl_bler", d)
        self.assertIn("gnb_reported_dl_cqi_index", d)
        self.assertIn("gnb_dl_mcs_index", d)
        self.assertIn("gnb_dl_bler_ratio", d)
        self.assertNotIn("legacy_radio", d)        # NOT auto-included (opt-in)
        self.assertEqual(d["gnb_ul_snr_db"], 15.0)  # honest name
        self.assertEqual(d["gnb_ul_avg_rsrp_dbm"], -90.0)
        self.assertEqual(d["radio_source"], "gnb_ul_mac")
        # a gNB-UL value NEVER lands in a UE-DL field
        self.assertIsNone(d["ue_dl_sinr_db"])
        self.assertIsNone(d["ue_dl_ss_rsrp_dbm"])
        # the legacy names survive ONLY via the EXPLICIT opt-in adapter
        legacy = m.legacy_radio_adapter()
        self.assertEqual(legacy["sinr"], 15.0)
        self.assertEqual(legacy["source"], "gnb_ul_mac")


class ImmutableJsonSafeTest(unittest.TestCase):
    """msg_19be: probe_config is deeply immutable + JSON-finite; the scheduler
    validates timeouts fail-closed."""

    def test_probe_config_is_deeply_immutable(self):
        s = MeasurementSample.ok(metric="m", value=5.0, purpose="in_window",
                                 source="s", sample_time=1.0, probe_id="p",
                                 probe_config={"offered_load_mbps": 12.0})
        with self.assertRaises((TypeError, AttributeError)):
            s.probe_config["offered_load_mbps"] = 999    # frozen -> raises
        # caller mutation of the ORIGINAL dict cannot reach in
        src = {"offered_load_mbps": 12.0}
        s2 = MeasurementSample.ok(metric="m", value=5.0, purpose="in_window",
                                  source="s", sample_time=1.0, probe_id="p",
                                  probe_config=src)
        src["offered_load_mbps"] = 999
        self.assertEqual(s2.to_dict()["probe_config"]["offered_load_mbps"], 12.0)

    def test_nested_nan_probe_config_rejected_at_construction(self):
        with self.assertRaises(MeasurementError):
            MeasurementSample.ok(metric="m", value=5.0, purpose="in_window",
                                 source="s", sample_time=1.0, probe_id="p",
                                 probe_config={"offered_load_mbps":
                                               float("nan")})

    def test_to_dict_is_allow_nan_false_serializable(self):
        import json
        s = MeasurementSample.ok(metric="m", value=5.0, purpose="in_window",
                                 source="s", sample_time=1.0, probe_id="p",
                                 probe_config=ProbeConfig().to_dict())
        json.dumps(s.to_dict(), allow_nan=False)         # must not raise

    def test_scheduler_rejects_bad_timeouts(self):
        for bad in (float("nan"), float("inf"), -1.0):
            with self.assertRaises(MeasurementError):
                MeasurementScheduler(default_timeout_s=bad)
        sched = MeasurementScheduler(default_timeout_s=0.1)
        for bad in (float("nan"), float("inf"), -1.0):
            with self.assertRaises(MeasurementError):
                with sched.probe("x", timeout_s=bad):
                    pass


class CostMetricsUnknownTest(unittest.TestCase):
    """msg_f0f31 B: a missing baseline flows through _record_cycle -> the
    calibrator's CostMetrics as throughput_unknown=True (excluded from cost),
    never a fake throughput_before=0.0 that enters adaptive history."""

    def test_record_cycle_sets_throughput_unknown(self):
        from calibration.adaptive_calibrator import CostMetrics
        recorded = {}
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.current_phase = "P"
        c._episode_context = ("modelA", "P")

        class _Cal:
            def record_episode(self, m):
                recorded["m"] = m
        c.calibrator = _Cal()
        c._resolve_calibration = lambda: {}
        c._reserve_ledger = None
        c.tau_trial_s = 15.0
        # a committed trial whose S3 baseline was UNKNOWN (None)
        cycle = {"trial_success": True, "trial_stats": {
            "tput_before": None, "tput_min": None, "tput_after": None,
            "tau": 15.0, "baseline_unknown": True}}
        c._record_cycle(cycle, episode_success=True)
        m = recorded["m"]
        self.assertTrue(m.throughput_unknown)            # flagged UNKNOWN
        # to_dict/from_dict round-trip preserves the flag
        self.assertTrue(CostMetrics.from_dict(m.to_dict()).throughput_unknown)
        # the derived EpisodeRecord carries it (estimator honors it)
        self.assertTrue(m.to_episode_record().throughput_unknown)


class ProbeInstallFailClosedTest(unittest.TestCase):
    """msg_6379: set_probe_config must be fail-closed + atomic - a collector
    failure/mismatch must NOT leave self._probe_config claiming a config the
    collector never applied."""

    def test_non_probeconfig_rejected(self):
        c = _meas_coord()
        with self.assertRaises(MeasurementError):
            c.set_probe_config({"offered_load_mbps": 12.0})   # not a ProbeConfig

    def test_missing_collector_setter_fails_closed(self):
        c = _meas_coord()

        class _NoSetter:
            def get_throughput_all(self, d=2.0):
                return {}
        c.ue_collector = _NoSetter()
        prev = c._probe_cfg().offered_load_mbps
        with self.assertRaises(RuntimeError):
            c.set_probe_config(ProbeConfig(offered_load_mbps=77.0))
        # coordinator config PRESERVED (no partial/mismatch)
        self.assertEqual(c._probe_cfg().offered_load_mbps, prev)

    def test_throwing_collector_setter_preserves_state(self):
        c = _meas_coord()

        class _Boom:
            probe_config = "PREV"

            def get_throughput_all(self, d=2.0):
                return {}

            def set_probe_config(self, cfg):
                raise RuntimeError("collector down")
        col = _Boom()
        c.ue_collector = col
        prev = c._probe_cfg().offered_load_mbps
        with self.assertRaises(RuntimeError):
            c.set_probe_config(ProbeConfig(offered_load_mbps=77.0))
        # self._probe_config unchanged; collector restore attempted (no partial)
        self.assertEqual(c._probe_cfg().offered_load_mbps, prev)

    def test_success_installs_on_both(self):
        c = _meas_coord()
        cfg = ProbeConfig(offered_load_mbps=44.0)
        c.set_probe_config(cfg)
        self.assertEqual(c._probe_cfg().offered_load_mbps, 44.0)
        self.assertIs(c.ue_collector.probe_config, cfg)

    def test_construction_fails_closed_on_collector_reject(self):
        # __init__ must NOT swallow a collector set_scheduler/set_probe_config
        # failure - a coordinator can never be built with an unconfigured probe
        # while self._probe_config claims a config.
        from unittest import mock
        from collectors.multi_ue_collector import MultiUECollector
        with mock.patch.object(MultiUECollector, "set_probe_config",
                               side_effect=RuntimeError("collector down")):
            with self.assertRaises(Exception):
                IntentCoordinator()
        with mock.patch.object(MultiUECollector, "set_scheduler",
                               side_effect=RuntimeError("no scheduler")):
            with self.assertRaises(Exception):
                IntentCoordinator()

    def test_runner_propagates_install_failure(self):
        import types
        from experiments.runner import ExperimentRunner

        class _BadCoord:
            def set_probe_config(self, cfg):
                raise RuntimeError("coordinator refuses")
        r = ExperimentRunner(coordinator=_BadCoord(),
                             output_dir="experiment_results")
        # this test targets the probe-install failure, not the P0-19 environment
        # preflight: inject an emulated ChannelModel so preflight passes and the
        # probe-install failure is reached.
        r.channel_model = types.SimpleNamespace(
            set_environment=lambda *a, **k: None)
        r._run_live_trial = lambda m, t: ([], [])
        with self.assertRaises(RuntimeError):
            r.run_live(["llm_with_history"], trials=1)


class ApiConsistencyTest(unittest.TestCase):
    """msg_87d28: admissibility purpose gate, stale-window fail-closed, measured
    ue_scope, and cross-field / duration validation."""

    def test_negotiation_sample_not_admissible(self):
        nego = MeasurementSample.ok(metric="m", value=5.0, purpose="negotiation",
                                    source="s", sample_time=9.0, probe_id="p",
                                    freshness="fresh", cache_status="live")
        self.assertFalse(nego.admissible_for_commit(1.0))
        win = MeasurementSample.ok(metric="m", value=5.0, purpose="in_window",
                                   source="s", sample_time=9.0, probe_id="p",
                                   freshness="fresh", cache_status="live")
        self.assertTrue(win.admissible_for_commit(1.0))

    def test_stale_window_sample_fails_s4(self):
        # an OK IN_WINDOW sample whose epoch REGRESSED before action_apply_time
        # (injected clock) is marked stale -> the window fails closed.
        times = iter([50.0, 50.0])       # sample epoch BEFORE apply (100)
        c = _meas_coord(now=lambda: next(times),
                        probe_fn=lambda d: {"ue1": 12.0})
        s, _ = c._measure_throughput_detailed(
            MeasurementPurpose.IN_WINDOW, action_apply_time=100.0)
        self.assertFalse(s.is_unknown())              # value is real...
        self.assertEqual(s.freshness, "stale")        # ...but STALE
        self.assertFalse(s.admissible_for_commit(100.0))

    def test_measured_ue_scope_is_populated(self):
        c = _meas_coord(probe_fn=lambda d: {"ue1": 9.0, "ue2": 8.0})
        s, _ = c._measure_throughput_detailed(
            MeasurementPurpose.IN_WINDOW, action_apply_time=1.0)
        self.assertEqual(set(s.ue_scope), {"ue1", "ue2"})   # measured keys

    def test_partial_probe_scope_is_measured_not_requested(self):
        # requested ue1+ue2 but only ue1 produced data: the OK sample's scope is
        # the MEASURED {ue1}, never the requested {ue1, ue2}.
        c = _meas_coord(probe_fn=lambda d: {"ue1": 9.0})
        s, _ = c._measure_throughput_detailed(
            MeasurementPurpose.IN_WINDOW, action_apply_time=1.0,
            ue_scope=["ue1", "ue2"])
        self.assertEqual(set(s.ue_scope), {"ue1"})          # measured only

    def test_unknown_retains_requested_scope(self):
        c = _meas_coord(probe_fn=lambda d: {})              # nothing measured
        s, _ = c._measure_throughput_detailed(
            MeasurementPurpose.IN_WINDOW, action_apply_time=1.0,
            ue_scope=["ue1", "ue2"])
        self.assertTrue(s.is_unknown())
        self.assertEqual(set(s.ue_scope), {"ue1", "ue2"})   # requested retained

    def test_negative_duration_and_cross_field_contradictions_rejected(self):
        cfg = ProbeConfig(offered_load_mbps=12.0, direction="downlink",
                          duration_s=2.0).to_dict()
        with self.assertRaises(MeasurementError):
            MeasurementSample.ok(metric="m", value=5.0, purpose="in_window",
                                 source="s", sample_time=1.0, probe_id="p",
                                 duration_s=-1.0)
        # sample direction contradicts probe_config direction
        with self.assertRaises(MeasurementError):
            MeasurementSample.ok(metric="m", value=5.0, purpose="in_window",
                                 source="s", sample_time=1.0, probe_id="p",
                                 direction="uplink", probe_config=cfg)
        # sample duration contradicts probe_config duration
        with self.assertRaises(MeasurementError):
            MeasurementSample.ok(metric="m", value=5.0, purpose="in_window",
                                 source="s", sample_time=1.0, probe_id="p",
                                 duration_s=999.0, probe_config=cfg)
        # a live sample naming a cache source is rejected
        with self.assertRaises(MeasurementError):
            MeasurementSample.ok(metric="m", value=5.0, purpose="in_window",
                                 source="cache_replay", sample_time=1.0,
                                 probe_id="p", cache_status="live")


class ProbeConfigCanonicalTest(unittest.TestCase):
    """msg_bdc9 #4: a sample's probe_config must be a CANONICAL ProbeConfig -
    arbitrary/unknown keys or a non-dict are rejected at construction."""

    def test_arbitrary_probe_config_rejected(self):
        with self.assertRaises(MeasurementError):
            MeasurementSample.ok(metric="m", value=5.0, purpose="in_window",
                                 source="s", sample_time=1.0, probe_id="p",
                                 probe_config={"foo": "bar"})
        with self.assertRaises(MeasurementError):
            MeasurementSample.ok(metric="m", value=5.0, purpose="in_window",
                                 source="s", sample_time=1.0, probe_id="p",
                                 probe_config="load=12M")   # not a dict
        # unknown extra key alongside valid ones is rejected
        with self.assertRaises(MeasurementError):
            MeasurementSample.ok(metric="m", value=5.0, purpose="in_window",
                                 source="s", sample_time=1.0, probe_id="p",
                                 probe_config={"offered_load_mbps": 12.0,
                                               "surprise": 1})
        # a valid canonical config is accepted + normalized
        s = MeasurementSample.ok(metric="m", value=5.0, purpose="in_window",
                                 source="s", sample_time=1.0, probe_id="p",
                                 probe_config={"offered_load_mbps": 12.0})
        self.assertEqual(s.to_dict()["probe_config"]["mode"], "goodput")


class NegotiationPartialUnknownTest(unittest.TestCase):
    """msg_bdc9 #3: an OK + UNKNOWN negotiation must NOT be settled with a
    partial mean - the aggregate cost is UNMEASURED and the ledger settles
    UNKNOWN (per-round samples preserved)."""

    def test_ok_plus_unknown_round_is_unmeasured(self):
        from tests.test_measurement import NegotiationPropagationTest as _NP
        from decision.intent_model import (
            ConstraintType, Intent, IntentTarget, IntentType, Alternative)
        calls = {"n": 0}

        def _probe(duration):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"ue1": 9.0}          # round 0 OK
            raise RuntimeError("round-1 iperf died")   # round 1 UNKNOWN
        c = _NP()._nego_coord(_probe)
        # allow 2 engaged rounds: reject policy + a generator that keeps
        # supplying a valid relaxation
        intent = Intent(type=IntentType.THROUGHPUT_GOAL,
                        target=IntentTarget(kpi_name="throughput",
                                            constraint_type=ConstraintType.MIN,
                                            target_value=8.0, unit="Mbps"))
        c.generate_alternatives_fn = lambda i, r: [
            Alternative(id=f"g{len(r)}", description="relax",
                        modified_intent=intent)]
        res = c._negotiate(intent, [Alternative(id="a0", description="relax",
                                                modified_intent=intent)])
        stats = res["stats"]
        self.assertTrue(stats["tput_during_nego_unknown"])   # any UNKNOWN round
        self.assertIsNone(stats["tput_during_nego"])         # no partial mean
        # both per-round samples preserved (OK + UNKNOWN)
        statuses = {s["status"] for s in stats["measurement_samples"]}
        self.assertIn("ok", statuses)
        self.assertIn("unknown", statuses)


class ProductionTrajectoryTest(unittest.TestCase):
    """msg_bdc9: prove the wired trajectory reaches CostMetrics via the
    PRODUCTION _record_cycle (not just a pure estimator construction)."""

    def test_record_cycle_wires_trajectory_into_cost_metrics(self):
        recorded = {}
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.current_phase = "P"
        c._episode_context = ("modelA", "P")

        class _Cal:
            def record_episode(self, m):
                recorded["m"] = m
        c.calibrator = _Cal()
        c._resolve_calibration = lambda: {}
        c._reserve_ledger = None
        c.tau_trial_s = 1.0
        cycle = {"trial_success": True, "trial_stats": {
            "tput_before": 5.0, "tput_min": 6.0, "tput_after": 9.0, "tau": 1.0,
            "trajectory": [(0.0, 9.0), (0.5, 8.0), (1.0, 9.0)],
            "throughput_unknown": False}}
        c._record_cycle(cycle, episode_success=True)
        m = recorded["m"]
        self.assertEqual(len(m.trial_trajectory), 3)         # wired through
        self.assertFalse(m.throughput_unknown)
        # the derived EpisodeRecord + estimator use the trajectory (not legacy)
        from experiments.metrics import (
            estimate_costs, IntentConfig, CostPriors)
        res = estimate_costs([m.to_episode_record()], IntentConfig(),
                             priors=CostPriors())
        self.assertGreaterEqual(res["trajectory"]["integrated"]["r_success"], 1)

    def test_record_cycle_transient_unknown_excludes_from_cost(self):
        # a committed trial with REAL tput_min/after but a transient window
        # UNKNOWN (throughput_unknown flag) must still be flagged UNKNOWN so the
        # estimator excludes it.
        recorded = {}
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.current_phase = "P"
        c._episode_context = ("modelA", "P")

        class _Cal:
            def record_episode(self, m):
                recorded["m"] = m
        c.calibrator = _Cal()
        c._resolve_calibration = lambda: {}
        c._reserve_ledger = None
        c.tau_trial_s = 1.0
        cycle = {"trial_success": True, "trial_stats": {
            "tput_before": 5.0, "tput_min": 6.0, "tput_after": 9.0, "tau": 1.0,
            "throughput_unknown": True}}      # transient UNKNOWN despite real vals
        c._record_cycle(cycle, episode_success=True)
        self.assertTrue(recorded["m"].throughput_unknown)


class TrajectoryUsedTest(unittest.TestCase):
    """msg_d8b9: when authoritative OK samples exist, cost integration uses the
    timestamped trajectory (trajectory_used), not the legacy min*tau fallback."""

    def test_costs_report_trajectory_used(self):
        from experiments.metrics import (
            EpisodeRecord, IntentConfig, estimate_costs, CostPriors)
        e = EpisodeRecord(
            trial_id=1, method="llm_with_history", phase="P",
            trial_executed=True, trial_success=True, success=True,
            throughput_before=5.0, throughput_after=9.0, throughput_trial_min=6.0,
            tau_trial=1.0,
            trial_trajectory=[(0.0, 9.0), (0.5, 8.0), (1.0, 9.0)])
        res = estimate_costs([e], IntentConfig(), priors=CostPriors())
        # the trajectory path was taken (not the legacy scalar shortcut)
        self.assertGreaterEqual(res["trajectory"]["integrated"]["r_success"], 1)
        self.assertEqual(res["trajectory"]["legacy"]["r_success"], 0)


class ProvenanceEndToEndTest(unittest.TestCase):
    """msg_726e: the exact measurement provenance survives result -> cycle ->
    EvidenceRecord -> raw *_episodes.json, and a callback mutating the cycle
    after tx binding cannot forge the committed evidence."""

    def _commit_coord(self):
        import time
        from tests.test_commit_invariant import (
            _commit_coord, _real_write_trial, _full_obs, _ok_verdicts)
        c = _commit_coord(readback=2.0)
        # set the coordinator probe config directly (this fixture's collector has
        # no set_probe_config setter; the fail-closed install is tested elsewhere)
        c._probe_config = ProbeConfig(offered_load_mbps=33.0)
        c.capacity_saturation_floor_mbps = 100.0

        def _feas(i, a, s):
            c._last_intent_signature = c._intent_content_hash(i)
            from decision.intent_model import FeasibilityPrediction
            # honest double: stamp the REAL prompt hash + proposal-generated
            # state so the pre-write S3 invariant (P1-6) sees a bound prompt hash.
            c._cur_proposal_generated = True
            c._cur_schema_valid = True   # generated-proposal schema verdict (real bool)
            c._cur_prompt_hash = hashlib.sha256(b"measurement-provenance").hexdigest()
            return FeasibilityPrediction(feasible=True, confidence=0.9,
                                         reasoning="",
                                         proposed_config={"bs1_power_offset": 2.0})
        c._analyze_feasibility = _feas
        c._execute_trial = _real_write_trial(c)

        def _v(ni, ai):
            # a CALLBACK that swaps the live probe config + mutates the public
            # cycle AFTER the tx binding - must NOT reach the committed evidence.
            c._probe_config = ProbeConfig(offered_load_mbps=999.0)
            cyc = (c._active_result.get("cycles") or [{}])[-1]
            if cyc.get("probe_config"):
                cyc["probe_config"]["offered_load_mbps"] = 999.0
            apply_t = getattr(c._active_txn, "action_apply_time",
                              None) or time.time()
            return {"all_satisfied": True, "metrics": {},
                    "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(apply_t)]}
        c._validate_trial = _v
        return c

    def test_commit_evidence_provenance_from_tx_not_mutated_cycle(self):
        from coordinator.episode_types import TerminalOutcome
        c = self._commit_coord()
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        ev = result["evidence"]
        # the committed evidence's probe_config is the TRUSTED tx-bound value
        # (33), NOT the callback-swapped 999 - and the cycle's public probe_config
        # was forged to 999 yet the evidence is unaffected.
        self.assertEqual(ev["probe_config"]["offered_load_mbps"], 33.0)
        self.assertEqual(result["cycles"][-1]["probe_config"][
            "offered_load_mbps"], 999.0)                  # cycle WAS mutated
        # the pre-action baseline provenance is present in the committed evidence
        self.assertIsNotNone(ev["pre_action_baseline"])

    def test_save_records_preserves_measurement_provenance(self):
        import tempfile, json
        from experiments.runner import ExperimentRunner
        from experiments.metrics import EpisodeRecord
        ep = EpisodeRecord(
            trial_id=1, method="llm_with_history", phase="P",
            pre_action_baseline={"status": "unknown", "value": None,
                                 "probe_id": "pb", "purpose": "pre_action"},
            probe_config={"offered_load_mbps": 12.0, "mode": "goodput"},
            measurement_samples=[{"probe_id": "pw", "purpose": "in_window",
                                  "status": "ok", "value": 8.0,
                                  "sample_time": 2.0}],
            trial_trajectory=[(2.0, 8.0)])
        with tempfile.TemporaryDirectory() as tmp:
            r = ExperimentRunner(coordinator=None, output_dir=tmp)
            base = r.save_records([], [ep], label="chk")
            d = json.load(open(base + "_episodes.json"))[0]
        self.assertEqual(d["pre_action_baseline"]["probe_id"], "pb")
        self.assertEqual(d["pre_action_baseline"]["status"], "unknown")
        self.assertEqual(d["probe_config"]["offered_load_mbps"], 12.0)
        self.assertEqual(d["measurement_samples"][0]["probe_id"], "pw")
        self.assertEqual(d["trial_trajectory"][0][1], 8.0)


class RadioKpiTest(unittest.TestCase):

    def test_radio_kpi_source_matches_field_name(self):
        m = UEMetrics(ue_id="ue1", attached=True, rsrp=-90.0, sinr=15.0,
                      radio_source="gnb_ul_mac", radio_direction="uplink")
        k = m.honest_radio_kpis()
        # the gNB-UL SNR is named gnb_ul_snr_db (NOT ue_dl_sinr_db)
        self.assertEqual(k["gnb_ul_snr_db"], 15.0)
        self.assertEqual(k["radio_source"], "gnb_ul_mac")
        self.assertEqual(k["radio_direction"], "uplink")
        # the paper's UE-DL metrics are NOT fabricated from gNB-UL -> UNKNOWN
        self.assertIsNone(k["ue_dl_sinr_db"])
        self.assertIsNone(k["ue_dl_ss_rsrp_dbm"])
        # to_dict carries the honest names + provenance
        d = m.to_dict()
        self.assertEqual(d["gnb_ul_snr_db"], 15.0)
        self.assertEqual(d["radio_source"], "gnb_ul_mac")

    def test_ue_dl_source_uses_ue_dl_names(self):
        m = UEMetrics(ue_id="ue1", attached=True, rsrp=-95.0, sinr=12.0,
                      radio_source="ue_dl_meas_report", radio_direction="downlink")
        k = m.honest_radio_kpis()
        self.assertEqual(k["ue_dl_sinr_db"], 12.0)
        self.assertEqual(k["ue_dl_ss_rsrp_dbm"], -95.0)
        self.assertNotIn("gnb_ul_snr_db", k)


# ---------------------------------------------------------------------------
# ExperimentConfig numeric validation (P0-14)
# ---------------------------------------------------------------------------
class ConfigValidationTest(unittest.TestCase):

    def test_config_rejects_bad_numeric_fields(self):
        for kw in ("offered_load_mbps", "probe_duration_s",
                   "capacity_saturation_floor_mbps", "kpi_interval_s"):
            for bad in (0, -1, float("nan"), float("inf"), True):
                with self.assertRaises(Exception):
                    ExperimentConfig(**{kw: bad})
        with self.assertRaises(Exception):
            ExperimentConfig(num_trials=0)
        with self.assertRaises(Exception):
            ExperimentConfig(probe_mode="turbo")
        with self.assertRaises(Exception):
            ExperimentConfig(probe_direction="sideways")

    def test_config_probe_config_roundtrips(self):
        cfg = ExperimentConfig(offered_load_mbps=25.0, probe_mode="capacity")
        pc = cfg.probe_config()
        self.assertEqual(pc.offered_load_mbps, 25.0)
        self.assertEqual(pc.mode, ProbeMode.CAPACITY)


# ---------------------------------------------------------------------------
# Provenance survives result -> cycle -> EvidenceRecord -> raw export
# ---------------------------------------------------------------------------
class ProvenanceSurvivalTest(unittest.TestCase):

    def test_measurement_provenance_in_evidence_export(self):
        import json
        from coordinator.episode_types import EvidenceRecord
        base = MeasurementSample.ok(metric="ue_throughput_mbps", value=6.0,
                                    purpose="pre_action", source="iperf3_udp_dl",
                                    sample_time=1.0, probe_id="pb").to_dict()
        win = MeasurementSample.ok(metric="ue_throughput_mbps", value=8.0,
                                   purpose="in_window", source="iperf3_udp_dl",
                                   sample_time=2.0, probe_id="pw",
                                   freshness="fresh").to_dict()
        ev = EvidenceRecord(
            experiment_run_id="r", episode_id="e", fsm_step_id="f",
            evidence_record_id="evid", proposer_id="p", model_version="m",
            intent_set_version="v", pending_intent_hash="h",
            pre_action_baseline=base, probe_config=ProbeConfig().to_dict(),
            measurement_samples=(win,))
        d = ev.to_dict()
        json.dumps(d)                                  # JSON-safe export
        self.assertEqual(d["pre_action_baseline"]["probe_id"], "pb")
        self.assertEqual(d["pre_action_baseline"]["source"], "iperf3_udp_dl")
        self.assertEqual(d["probe_config"]["offered_load_mbps"], 12.0)
        self.assertEqual(d["measurement_samples"][0]["probe_id"], "pw")
        self.assertEqual(d["measurement_samples"][0]["purpose"], "in_window")


class EmulatedS4IntegrationTest(unittest.TestCase):
    """Batch F through the REAL emulated S4 measurement path (no network): the
    authoritative live probe drives validation, a cache can't substitute, and
    the SHARED scheduler truly blocks an overlapping S4 probe."""

    def _coord(self, probe_fn):
        from experiments.emulation import build_emulated_coordinator
        from experiments.topology import two_ue_topology   # legacy 2-UE (explicit)
        c, _ = build_emulated_coordinator(
            seed=7, tau_trial_s=0.1, topology=two_ue_topology())
        c._throughput_probe_fn = probe_fn

        class _Tx:
            pass
        tx = _Tx()
        tx.action_apply_time = c._epoch_now() - 0.001   # action already applied
        c._active_txn = tx
        c._pre_trial_attached = {"ue1", "ue2"}
        return c

    @staticmethod
    def _intent():
        from decision.intent_model import (
            ConstraintType, Intent, IntentScope, IntentTarget, IntentType)
        return Intent(type=IntentType.THROUGHPUT_GOAL,
                      target=IntentTarget(kpi_name="throughput",
                                          constraint_type=ConstraintType.MIN,
                                          target_value=8.0, unit="Mbps"),
                      scope=IntentScope(ue_ids=["ue1", "ue2"]))

    def test_every_s4_sample_is_post_action(self):
        c = self._coord(lambda d: {"ue1": 12.0, "ue2": 12.0})
        out = c._validate_trial(self._intent(), [])
        samples = out["measurement_samples"]
        self.assertTrue(samples)                       # real live samples
        apply_t = c._active_txn.action_apply_time
        pids = set()
        for s in samples:
            self.assertEqual(s["purpose"], "in_window")
            self.assertEqual(s["status"], "ok")
            self.assertGreaterEqual(s["sample_time"], apply_t)   # POST-action
            pids.add((s["probe_id"], s["sample_time"]))
        self.assertEqual(len(pids), len(samples))      # distinct ids + times
        self.assertTrue(out["all_satisfied"])          # 12 >= 8 validates

    def test_probe_failure_with_high_cache_rollback(self):
        # the emulated collect_all cache reports a healthy throughput, but the
        # AUTHORITATIVE live probe FAILS -> UNKNOWN -> measurement_invalid, so
        # the trial can NOT validate (the cache can never substitute).
        def _boom(duration):
            raise RuntimeError("iperf died")
        c = self._coord(_boom)
        out = c._validate_trial(self._intent(), [])
        self.assertTrue(out["measurement_invalid"])
        self.assertFalse(out["all_satisfied"])         # cache did NOT validate
        self.assertIsNone(out["tput_min"])             # None, never 0
        self.assertIsNone(out["tput_after"])
        self.assertTrue(all(s["status"] == "unknown"
                            for s in out["measurement_samples"]))

    def test_empty_probe_cannot_validate_trial(self):
        c = self._coord(lambda d: {})                  # no usable per-UE data
        out = c._validate_trial(self._intent(), [])
        self.assertTrue(out["measurement_invalid"])
        self.assertFalse(out["all_satisfied"])
        self.assertIsNone(out["tput_min"])

    def test_stale_background_sample_not_admissible_for_commit(self):
        # a healthy trial produces admissible in_window samples; a background /
        # pre-action / stale sample is dropped by the commit-evidence filter.
        c = self._coord(lambda d: {"ue1": 12.0, "ue2": 12.0})
        out = c._validate_trial(self._intent(), [])
        apply_t = c._active_txn.action_apply_time
        # real in_window samples ARE admissible
        self.assertTrue(any(c._sample_admissible_for_commit(s, apply_t)
                            for s in out["measurement_samples"]))
        # a background sample (even fresh + live) is NOT admissible
        bg = MeasurementSample.ok(metric="ue_throughput_mbps", value=99.0,
                                  purpose="background", source="s",
                                  sample_time=apply_t + 1, probe_id="bg",
                                  freshness="fresh", cache_status="live")
        self.assertFalse(
            c._sample_admissible_for_commit(bg.to_dict(), apply_t))
        # a pre-action sample is NOT admissible
        pa = MeasurementSample.ok(metric="ue_throughput_mbps", value=99.0,
                                  purpose="pre_action", source="s",
                                  sample_time=apply_t + 1, probe_id="pa",
                                  freshness="fresh", cache_status="live")
        self.assertFalse(
            c._sample_admissible_for_commit(pa.to_dict(), apply_t))

    def test_background_probe_blocks_s4_via_shared_scheduler(self):
        # hold the coordinator's SHARED scheduler from a background thread while
        # S4 runs: every S4 probe fails closed to UNKNOWN (busy), the trial can't
        # validate, and peak concurrency never exceeds 1 (no overlapping iperf).
        c = self._coord(lambda d: {"ue1": 12.0, "ue2": 12.0})
        c.probe_lock_timeout_s = 0.1
        held = threading.Event()
        release = threading.Event()

        def _bg():
            with c._measurement_scheduler.probe("background"):
                held.set()
                release.wait(timeout=5.0)
        t = threading.Thread(target=_bg)
        t.start()
        self.assertTrue(held.wait(timeout=3.0))
        try:
            out = c._validate_trial(self._intent(), [])
        finally:
            release.set()
            t.join(timeout=5.0)
        self.assertTrue(out["measurement_invalid"])    # blocked -> UNKNOWN
        self.assertFalse(out["all_satisfied"])
        self.assertEqual(c._measurement_scheduler.peak_concurrency, 1)


if __name__ == "__main__":
    unittest.main()
