#!/usr/bin/env python3
"""
Tests for the LIVE-measurement fix: PASSIVE tun rx_bytes goodput vs the active
iperf3 KPI probe under a §1-4 background offered load.

The defect (observed live): the KPI probe injects its OWN iperf3 UDP session to
the SAME UE iperf3 server the background offered load is already using. A single
iperf3 server serves one test at a time, so the second client is rejected /
100%-dropped and get_throughput computes a spurious 0.00 - which collapsed the
whole run's strict-sat metric. The fix measures the KPI PASSIVELY (UE tun
rx_bytes counter delta) whenever a background offered load is active: it injects
nothing, touches no iperf3 server, and so cannot collide - it reports the real
delivered goodput under load.

Everything is mock/monkeypatched - NO real subprocess, NO real iperf3, NO
hardware touched (project rule: testbed/LLM/network absent; all mock/emulation).
"""

import threading
import time
import types
import unittest
from unittest import mock

from collectors.multi_ue_collector import (
    UECollector, LocalUECollector, MultiUECollector)
from coordinator.intent_coordinator import IntentCoordinator
from coordinator.measurement import (
    MeasurementScheduler, MeasurementPurpose, ProbeConfig)


# --------------------------------------------------------------------------- #
# 1. Collector: passive rx_bytes goodput (no iperf3 injection)                #
# --------------------------------------------------------------------------- #
def _ue_collector_with_reads(reads):
    """A UECollector whose tun rx_bytes counter yields `reads` in order (each a
    (ok, stdout) SSH result). No real SSH is ever issued."""
    col = UECollector("ue1", "10.0.0.1", "u1", gnb_id="gnb1")
    seq = list(reads)

    def _fake_ssh(cmd, timeout=5.0):
        return seq.pop(0) if seq else (False, "")
    col._ssh_cmd = _fake_ssh
    return col


class PassiveGoodputCollectorTest(unittest.TestCase):

    def test_rx_bytes_delta_is_mbps(self):
        # 3,000,000 bytes over 2 s = 12 Mbit/s delivered goodput.
        col = _ue_collector_with_reads([(True, "1000000"), (True, "4000000")])
        with mock.patch("collectors.multi_ue_collector.time.sleep"):
            val = col.get_goodput_passive(duration=2.0)
        self.assertAlmostEqual(val, (3_000_000 * 8) / (2.0 * 1e6))  # 12.0
        self.assertAlmostEqual(val, 12.0)

    def test_passive_probe_injects_no_iperf3(self):
        # the fix's whole point: passive measurement NEVER shells out an iperf3
        # client (that is what collided with the load). It only reads a counter.
        col = _ue_collector_with_reads([(True, "10"), (True, "2500010")])
        with mock.patch("collectors.multi_ue_collector.subprocess.run") as run, \
             mock.patch("collectors.multi_ue_collector.time.sleep"):
            val = col.get_goodput_passive(duration=2.0)
        run.assert_not_called()                       # no docker exec / iperf3
        self.assertAlmostEqual(val, (2_500_000 * 8) / (2.0 * 1e6))  # 10.0

    def test_unreadable_counter_is_none_not_zero(self):
        # first read fails -> explicit None (UNKNOWN), never a fabricated 0.
        col = _ue_collector_with_reads([(False, "")])
        with mock.patch("collectors.multi_ue_collector.time.sleep"):
            self.assertIsNone(col.get_goodput_passive(duration=2.0))
        # second read fails -> also None.
        col2 = _ue_collector_with_reads([(True, "100"), (False, "")])
        with mock.patch("collectors.multi_ue_collector.time.sleep"):
            self.assertIsNone(col2.get_goodput_passive(duration=2.0))

    def test_counter_reset_is_none(self):
        # a counter that goes backwards (device re-created) -> None, not negative.
        col = _ue_collector_with_reads([(True, "9000000"), (True, "10")])
        with mock.patch("collectors.multi_ue_collector.time.sleep"):
            self.assertIsNone(col.get_goodput_passive(duration=2.0))

    def test_nonpositive_duration_is_none(self):
        col = _ue_collector_with_reads([(True, "0"), (True, "1")])
        with mock.patch("collectors.multi_ue_collector.time.sleep"):
            self.assertIsNone(col.get_goodput_passive(duration=0.0))
            self.assertIsNone(col.get_goodput_passive(duration=-1.0))

    def test_garbage_counter_is_none(self):
        col = _ue_collector_with_reads([(True, "not-a-number"), (True, "5")])
        with mock.patch("collectors.multi_ue_collector.time.sleep"):
            self.assertIsNone(col.get_goodput_passive(duration=2.0))

    def test_zero_delivery_reads_zero_not_none(self):
        # a genuinely idle link (counter unchanged) reads exactly 0.0 - which is
        # an HONEST measurement (nothing delivered), distinct from the UNKNOWN
        # None a failed read returns.
        col = _ue_collector_with_reads([(True, "500"), (True, "500")])
        with mock.patch("collectors.multi_ue_collector.time.sleep"):
            self.assertEqual(col.get_goodput_passive(duration=2.0), 0.0)

    def test_local_collector_passive_is_none(self):
        self.assertIsNone(LocalUECollector("ue1").get_goodput_passive())


class PassiveGoodputAllTest(unittest.TestCase):

    def _multi(self, values):
        col = MultiUECollector(simulation_mode=False)
        col.collectors.clear()
        for ue, v in values.items():
            fake = types.SimpleNamespace()
            fake.get_goodput_passive = (
                lambda duration=2.0, direction="downlink", _v=v: _v)
            col.collectors[ue] = fake
        return col

    def test_aggregates_per_ue(self):
        col = self._multi({"ue1": 4.1, "ue2": 3.9})
        col.set_probe_config(ProbeConfig(offered_load_mbps=12.0, duration_s=2.0))
        out = col.get_goodput_passive_all(duration=2.0)
        self.assertEqual(set(out), {"ue1", "ue2"})
        self.assertAlmostEqual(out["ue1"], 4.1)
        self.assertAlmostEqual(out["ue2"], 3.9)

    def test_none_result_is_dropped(self):
        col = self._multi({"ue1": 4.1, "ue2": None})
        out = col.get_goodput_passive_all(duration=2.0)
        self.assertEqual(set(out), {"ue1"})          # ue2 absent, never a 0

    def test_collector_without_passive_probe_is_skipped(self):
        col = MultiUECollector(simulation_mode=False)
        col.collectors.clear()
        col.collectors["ue1"] = types.SimpleNamespace()   # no get_goodput_passive
        self.assertEqual(col.get_goodput_passive_all(duration=1.0), {})


# --------------------------------------------------------------------------- #
# 2. Coordinator: route to passive WHEN loaded, active otherwise              #
# --------------------------------------------------------------------------- #
class _FakeGen:
    def __init__(self, rate):
        self.rate_mbps = float(rate)


class _FakeCollector:
    def __init__(self, simulation_mode=False):
        self.simulation_mode = simulation_mode
        self.probe_config = None

    def set_probe_config(self, cfg):
        self.probe_config = cfg

    def get_throughput_all(self, duration=2.0):
        return {}

    def get_goodput_passive_all(self, duration=2.0):
        return {}


class _NoPassiveCollector:
    """A collector that exposes NO passive probe (older collector) - routing must
    stay on the active path rather than crash."""
    def __init__(self, simulation_mode=False):
        self.simulation_mode = simulation_mode
        self.probe_config = None

    def set_probe_config(self, cfg):
        self.probe_config = cfg

    def get_throughput_all(self, duration=2.0):
        return {}


def _load_coord(*, load_rate=0.0, sim=False, has_passive=True,
                active_fn=None, passive_fn=None, enabled=True,
                lock_timeout=0.2):
    c = IntentCoordinator.__new__(IntentCoordinator)
    c._measurement_scheduler = MeasurementScheduler(default_timeout_s=lock_timeout)
    c._probe_config = ProbeConfig(offered_load_mbps=12.0, duration_s=2.0)
    c._now = lambda: 1000.0
    c._throughput_probe_fn = active_fn
    c._goodput_probe_fn = passive_fn
    c.capacity_saturation_floor_mbps = 100.0
    c.probe_lock_timeout_s = lock_timeout
    c.passive_goodput_when_loaded = enabled
    c._offered_load_gen = _FakeGen(load_rate) if load_rate else None
    c.ue_collector = (_FakeCollector(simulation_mode=sim) if has_passive
                      else _NoPassiveCollector(simulation_mode=sim))
    return c


class PassiveRoutingTest(unittest.TestCase):

    def test_no_load_uses_active_iperf3(self):
        c = _load_coord(load_rate=0.0,
                        active_fn=lambda d: {"ue1": 4.0},
                        passive_fn=lambda d: {"ue1": 99.0})
        self.assertFalse(c._use_passive_goodput())
        s = c._measure_throughput(MeasurementPurpose.BACKGROUND)
        self.assertAlmostEqual(s.value, 4.0)              # active path
        self.assertTrue(s.source.startswith("iperf3_"))

    def test_load_active_uses_passive_goodput(self):
        # the crux: with a 12 Mbps background load, the active probe would collide
        # and read 0.0; the passive path reads the real delivered goodput instead.
        c = _load_coord(load_rate=12.0,
                        active_fn=lambda d: {"ue1": 0.0, "ue2": 0.0},  # collision
                        passive_fn=lambda d: {"ue1": 4.1, "ue2": 3.7})
        self.assertTrue(c._use_passive_goodput())
        s = c._measure_throughput(MeasurementPurpose.BACKGROUND)
        self.assertGreater(s.value, 0.0)                  # NOT the spurious 0
        self.assertAlmostEqual(s.value, (4.1 + 3.7) / 2)
        self.assertEqual(s.source, "tun_rx_bytes_goodput_downlink")

    def test_passive_does_not_hold_measurement_lock(self):
        # passive must NOT take the shared lock (the load has to keep flowing).
        # Prove it: another thread holds the lock; the passive probe still
        # succeeds within a tiny lock timeout (an active probe would fail closed).
        c = _load_coord(load_rate=12.0, lock_timeout=0.2,
                        active_fn=lambda d: {"ue1": 4.0},
                        passive_fn=lambda d: {"ue1": 4.1})
        held, release = threading.Event(), threading.Event()

        def _hold():
            with c._measurement_scheduler.probe("offered-load"):
                held.set()
                release.wait(timeout=3.0)
        t = threading.Thread(target=_hold)
        t.start()
        try:
            self.assertTrue(held.wait(timeout=3.0))
            s = c._measure_throughput(MeasurementPurpose.BACKGROUND)
            self.assertFalse(s.is_unknown())              # did not block/timeout
            self.assertAlmostEqual(s.value, 4.1)
        finally:
            release.set()
            t.join(timeout=3.0)

    def test_simulation_mode_never_passive(self):
        c = _load_coord(load_rate=12.0, sim=True,
                        active_fn=lambda d: {"ue1": 5.0},
                        passive_fn=lambda d: {"ue1": 4.1})
        self.assertFalse(c._use_passive_goodput())
        s = c._measure_throughput(MeasurementPurpose.BACKGROUND)
        self.assertAlmostEqual(s.value, 5.0)              # active (emulated) path

    def test_switch_disabled_forces_active(self):
        c = _load_coord(load_rate=12.0, enabled=False,
                        active_fn=lambda d: {"ue1": 0.0},
                        passive_fn=lambda d: {"ue1": 4.1})
        self.assertFalse(c._use_passive_goodput())

    def test_collector_without_passive_probe_stays_active(self):
        c = _load_coord(load_rate=12.0, has_passive=False,
                        active_fn=lambda d: {"ue1": 4.0},
                        passive_fn=lambda d: {"ue1": 4.1})
        self.assertFalse(c._use_passive_goodput())

    def test_passive_empty_is_unknown_not_zero(self):
        # a passive probe that returns no usable per-UE data is an explicit
        # UNKNOWN (value=None), never coerced to 0.
        c = _load_coord(load_rate=12.0,
                        active_fn=lambda d: {"ue1": 4.0},
                        passive_fn=lambda d: {})
        s = c._measure_throughput(MeasurementPurpose.BACKGROUND)
        self.assertTrue(s.is_unknown())
        self.assertIsNone(s.value)


# --------------------------------------------------------------------------- #
# 3. Phase transitions + teardown (recovery guarantee)                        #
# --------------------------------------------------------------------------- #
class PhaseTransitionTest(unittest.TestCase):
    """set_offered_load drives the generator rate, and the passive/active routing
    tracks it - so a Nominal->Congestion->Recovery sweep changes the load and the
    measurement path together, and termination tears the load down cleanly."""

    def _coord_with_recording_gen(self):
        c = IntentCoordinator.__new__(IntentCoordinator)
        c._measurement_scheduler = MeasurementScheduler(default_timeout_s=0.2)
        c._probe_config = ProbeConfig(offered_load_mbps=12.0, duration_s=2.0)
        c.passive_goodput_when_loaded = True
        c.ue_collector = _FakeCollector(simulation_mode=False)

        class _RecGen:
            def __init__(self):
                self.rate_mbps = 0.0
                self.rates = []
                self.stopped = 0

            def set_rate(self, mbps):
                self.rate_mbps = float(mbps)
                self.rates.append(float(mbps))

            def stop(self):
                self.stopped += 1
                self.rate_mbps = 0.0
        c._offered_load_gen = _RecGen()
        return c

    def test_rate_changes_toggle_passive_routing(self):
        c = self._coord_with_recording_gen()
        gen = c._offered_load_gen

        # Nominal: load on -> passive.
        c.set_offered_load(12.0)
        self.assertEqual(gen.rate_mbps, 12.0)
        self.assertTrue(c._offered_load_active())
        self.assertTrue(c._use_passive_goodput())

        # Congestion: heavier load, still passive.
        c.set_offered_load(24.0)
        self.assertEqual(gen.rate_mbps, 24.0)
        self.assertTrue(c._use_passive_goodput())

        # Recovery-to-idle: load off -> active probe restored, generator stopped
        # launching new bursts (rate 0).
        c.set_offered_load(0.0)
        self.assertEqual(gen.rate_mbps, 0.0)
        self.assertFalse(c._offered_load_active())
        self.assertFalse(c._use_passive_goodput())
        self.assertEqual(gen.rates, [12.0, 24.0, 0.0])   # every phase applied

    def test_stop_tears_down_offered_load(self):
        c = self._coord_with_recording_gen()
        gen = c._offered_load_gen
        c.set_offered_load(12.0)
        self.assertTrue(c._offered_load_active())
        c._stop_offered_load()
        self.assertEqual(gen.stopped, 1)                 # cleaned up, no zombie
        self.assertFalse(c._offered_load_active())


if __name__ == "__main__":
    unittest.main()
