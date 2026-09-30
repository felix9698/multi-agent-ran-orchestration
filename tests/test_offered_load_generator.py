#!/usr/bin/env python3
"""
Tests for the LIVE offered-load background traffic generator (§1-4 P0-19).

Covers the last missing OTA piece: IntentCoordinator.set_offered_load(mbps) and
the coordinator.offered_load.BackgroundLoadGenerator behind it. Everything is
mock/monkeypatched - NO real subprocess, NO real iperf3, NO hardware touched
(project rule: testbed/LLM/network absent; all validation mock/emulation).

Verified properties:
  * every burst is wrapped in the IN-CONTAINER `timeout` (zombie safety, the
    same pattern collectors.multi_ue_collector.get_throughput uses);
  * a burst is a UDP iperf3 at the requested rate to each serving UE's tun IP;
  * rate change cleans up the old generator and restarts at the new rate;
  * rate 0 stops the load; stop() guarantees teardown;
  * bursts run UNDER the shared MeasurementScheduler lock (KPI-probe coordination
    - peak concurrency with a probe stays 1);
  * the load_setter injection path (runner -> coordinator.set_offered_load ->
    generator) is wired end to end.
"""

import threading
import types
import unittest

import coordinator.offered_load as ol
from coordinator.offered_load import (
    BackgroundLoadGenerator, _load_burst_cmd, _fmt_rate, _nonneg_rate)
from coordinator.measurement import MeasurementScheduler, MeasurementBusy


def _rate_of(cmd):
    """Extract the '-b <rate>M' value from an iperf3 burst argv."""
    return cmd[cmd.index("-b") + 1]


def _target_of(cmd):
    """Extract the '-c <ip>' target from an iperf3 burst argv."""
    return cmd[cmd.index("-c") + 1]


class RecordingRunner:
    """A stand-in for collectors.multi_ue_collector._run: records argv + the
    host timeout, returns a benign (ok, stdout)."""

    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, cmd, timeout):
        with self._lock:
            self.calls.append((list(cmd), timeout))
        return True, ""

    @property
    def cmds(self):
        with self._lock:
            return [c for c, _ in self.calls]


# --------------------------------------------------------------------------- #
# 1. Burst command construction (zombie-safe container timeout wrapping)      #
# --------------------------------------------------------------------------- #

class BurstCommandTest(unittest.TestCase):
    def test_burst_is_container_timeout_wrapped_udp(self):
        cmd = _load_burst_cmd("oai-ext-dn", "12.1.1.2", 12.0, burst_s=4,
                              hard_s=10)
        # zombie safety: docker exec ... timeout <hard> iperf3 ... (the host
        # never has to kill it; the in-container timeout reaps it)
        self.assertEqual(cmd[:6],
                         ["docker", "exec", "oai-ext-dn", "timeout", "10",
                          "iperf3"])
        self.assertEqual(_target_of(cmd), "12.1.1.2")
        self.assertIn("-u", cmd)                       # UDP background load
        self.assertEqual(_rate_of(cmd), "12M")         # offered rate
        self.assertEqual(cmd[cmd.index("-t") + 1], "4")  # burst length

    def test_hard_timeout_exceeds_burst(self):
        # a normally-completing burst is bounded by -t; a wedged one by timeout,
        # so hard must be strictly greater than the burst length.
        cmd = _load_burst_cmd("c", "ip", 5.0, burst_s=4, hard_s=10)
        self.assertGreater(int(cmd[cmd.index("timeout") + 1]),
                           int(cmd[cmd.index("-t") + 1]))

    def test_fmt_rate_formats_int_and_float(self):
        self.assertEqual(_fmt_rate(12.0), "12M")
        self.assertEqual(_fmt_rate(2.5), "2.5M")
        self.assertEqual(_fmt_rate(18), "18M")


# --------------------------------------------------------------------------- #
# 2. _run_once: one burst per serving UE, rate/target correct                 #
# --------------------------------------------------------------------------- #

class RunOnceTest(unittest.TestCase):
    def test_loads_each_serving_ue_at_rate(self):
        runner = RecordingRunner()
        gen = BackgroundLoadGenerator(
            lambda: {"ue1": "12.1.1.2", "ue2": "12.1.1.3"},
            scheduler=None, runner=runner)
        n = gen._run_once(rate=9.0)
        self.assertEqual(n, 2)
        self.assertEqual(sorted(_target_of(c) for c in runner.cmds),
                         ["12.1.1.2", "12.1.1.3"])
        self.assertTrue(all(_rate_of(c) == "9M" for c in runner.cmds))
        # each is container-timeout wrapped (zombie safety on the real path)
        self.assertTrue(all(c[:4] == ["docker", "exec", "oai-ext-dn", "timeout"]
                            for c in runner.cmds))

    def test_no_targets_is_a_noop(self):
        runner = RecordingRunner()
        gen = BackgroundLoadGenerator(lambda: {}, scheduler=None, runner=runner)
        self.assertEqual(gen._run_once(rate=9.0), 0)
        self.assertEqual(runner.cmds, [])

    def test_zero_rate_launches_nothing(self):
        runner = RecordingRunner()
        gen = BackgroundLoadGenerator(
            lambda: {"ue1": "12.1.1.2"}, scheduler=None, runner=runner)
        self.assertEqual(gen._run_once(rate=0.0), 0)
        self.assertEqual(runner.cmds, [])

    def test_skips_ue_without_ip(self):
        runner = RecordingRunner()
        gen = BackgroundLoadGenerator(
            lambda: {"ue1": "12.1.1.2", "ue2": None}, scheduler=None,
            runner=runner)
        self.assertEqual(gen._run_once(rate=7.0), 1)
        self.assertEqual([_target_of(c) for c in runner.cmds], ["12.1.1.2"])


# --------------------------------------------------------------------------- #
# 3. Shared-scheduler coordination with the KPI probe                         #
# --------------------------------------------------------------------------- #

class SchedulerCoordinationTest(unittest.TestCase):
    def test_burst_runs_under_the_shared_lock(self):
        sched = MeasurementScheduler(default_timeout_s=5.0)
        seen = {}

        def runner(cmd, timeout):
            # while a burst runs the shared lock is held under our label
            seen["active"] = sched.active_probe
            seen["peak"] = sched.peak_concurrency
            return True, ""

        gen = BackgroundLoadGenerator(
            lambda: {"ue1": "12.1.1.2"}, scheduler=sched, runner=runner)
        gen._run_once(rate=9.0)
        self.assertEqual(seen["active"], "offered-load")
        self.assertEqual(seen["peak"], 1)          # never overlapped a probe
        self.assertIsNone(sched.active_probe)       # released after the burst

    def test_burst_yields_when_probe_holds_the_lock(self):
        # a KPI/trial probe holding the lock -> the burst is SKIPPED (the read
        # wins), never run concurrently, never blocking forever.
        sched = MeasurementScheduler(default_timeout_s=0.2)
        runner = RecordingRunner()
        gen = BackgroundLoadGenerator(
            lambda: {"ue1": "12.1.1.2"}, scheduler=sched, runner=runner,
            lock_timeout_s=0.2)
        with sched.probe(label="kpi", timeout_s=0.2):   # probe owns the lock
            launched = gen._run_once(rate=9.0)
        self.assertEqual(launched, 0)                   # yielded, nothing run
        self.assertEqual(runner.cmds, [])


# --------------------------------------------------------------------------- #
# 4. Lifecycle: rate change, stop, cleanup                                    #
# --------------------------------------------------------------------------- #

class LifecycleTest(unittest.TestCase):
    def _gen(self, runner, **kw):
        # tiny gap so the loop cycles fast; targets fixed for determinism.
        return BackgroundLoadGenerator(
            lambda: {"ue1": "12.1.1.2"}, scheduler=None, runner=runner,
            burst_s=0, gap_s=0.01, hard_margin_s=1, **kw)

    def _wait_burst(self, gen):
        self.assertTrue(gen._burst_event.wait(timeout=3.0),
                        "generator never launched a burst")
        gen._burst_event.clear()

    def test_set_rate_starts_a_running_thread_that_loads(self):
        runner = RecordingRunner()
        gen = self._gen(runner)
        self.addCleanup(gen.stop)
        gen.set_rate(10.0)
        self.assertTrue(gen.running)
        self._wait_burst(gen)
        self.assertTrue(any(_rate_of(c) == "10M" for c in runner.cmds))

    def test_rate_change_cleans_up_and_restarts_at_new_rate(self):
        runner = RecordingRunner()
        gen = self._gen(runner)
        self.addCleanup(gen.stop)
        gen.set_rate(10.0)
        self._wait_burst(gen)
        first_thread = gen._thread
        gen.set_rate(20.0)
        # old thread was joined (cleaned up), a new one runs
        self.assertFalse(first_thread.is_alive())
        self.assertIsNot(gen._thread, first_thread)
        self.assertTrue(gen.running)
        self.assertEqual(gen.rate_mbps, 20.0)
        self._wait_burst(gen)
        # a burst at the NEW rate appears after the change
        self.assertTrue(any(_rate_of(c) == "20M" for c in runner.cmds))

    def test_rate_zero_stops_the_load(self):
        runner = RecordingRunner()
        gen = self._gen(runner)
        gen.set_rate(10.0)
        self._wait_burst(gen)
        gen.set_rate(0.0)
        self.assertFalse(gen.running)
        self.assertEqual(gen.rate_mbps, 0.0)
        self.assertIsNone(gen._thread)

    def test_stop_tears_down_the_thread(self):
        runner = RecordingRunner()
        gen = self._gen(runner)
        gen.set_rate(10.0)
        self._wait_burst(gen)
        gen.stop()
        self.assertFalse(gen.running)
        self.assertIsNone(gen._thread)

    def test_set_rate_zero_with_no_generator_is_a_noop(self):
        runner = RecordingRunner()
        gen = self._gen(runner)
        gen.set_rate(0.0)                # nothing was running
        self.assertFalse(gen.running)
        self.assertEqual(runner.cmds, [])


# --------------------------------------------------------------------------- #
# 5. Input validation (fail closed before any iperf)                          #
# --------------------------------------------------------------------------- #

class ValidationTest(unittest.TestCase):
    def test_rejects_negative_nan_inf_and_bool(self):
        for bad in (-1.0, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                _nonneg_rate(bad)

    def test_rejects_noncallable_target_provider(self):
        with self.assertRaises(ValueError):
            BackgroundLoadGenerator(None)

    def test_rejects_noncallable_runner(self):
        with self.assertRaises(ValueError):
            BackgroundLoadGenerator(lambda: {}, runner="nope")


# --------------------------------------------------------------------------- #
# 6. Coordinator hook: lazy build, forward, targets, teardown, injection      #
# --------------------------------------------------------------------------- #

class _FakeGen:
    """Records what IntentCoordinator.set_offered_load hands the generator."""
    instances = []

    def __init__(self, provider, scheduler=None):
        self.provider = provider
        self.scheduler = scheduler
        self.rates = []
        self.stopped = False
        _FakeGen.instances.append(self)

    def set_rate(self, mbps):
        self.rates.append(mbps)

    def stop(self):
        self.stopped = True


class CoordinatorHookTest(unittest.TestCase):
    def setUp(self):
        from coordinator.intent_coordinator import IntentCoordinator
        _FakeGen.instances = []
        self._orig = ol.BackgroundLoadGenerator
        ol.BackgroundLoadGenerator = _FakeGen
        # a __new__ skeleton: exercise the hook WITHOUT building the full live
        # coordinator (no collectors/executor/LLM), matching the project's
        # "no hardware" test rule.
        c = IntentCoordinator.__new__(IntentCoordinator)
        c._offered_load_gen = None
        c._measurement_scheduler = "SCHED"
        c.ue_collector = types.SimpleNamespace(collectors={})
        self.c = c

    def tearDown(self):
        ol.BackgroundLoadGenerator = self._orig

    def test_zero_without_generator_builds_nothing(self):
        self.c.set_offered_load(0)
        self.assertIsNone(self.c._offered_load_gen)
        self.assertEqual(_FakeGen.instances, [])

    def test_nonzero_lazily_builds_and_forwards_rate(self):
        self.c.set_offered_load(12.0)
        gen = self.c._offered_load_gen
        self.assertIsInstance(gen, _FakeGen)
        self.assertEqual(gen.rates, [12.0])
        self.assertIs(gen.scheduler, "SCHED")   # shares the KPI scheduler lock
        # only ONE generator is ever built (reused across calls)
        self.c.set_offered_load(24.0)
        self.assertIs(self.c._offered_load_gen, gen)
        self.assertEqual(gen.rates, [12.0, 24.0])
        self.assertEqual(len(_FakeGen.instances), 1)

    def test_provider_resolves_serving_attached_ues(self):
        # wire two collectors: one attached-with-IP, one detached -> only the
        # attached one is a load target.
        self.c.ue_collector = types.SimpleNamespace(collectors={
            "ue1": types.SimpleNamespace(get_last_metrics=lambda: types.
                SimpleNamespace(attached=True, ip_address="12.1.1.2")),
            "ue2": types.SimpleNamespace(get_last_metrics=lambda: types.
                SimpleNamespace(attached=False, ip_address=None)),
        })
        self.c.set_offered_load(10.0)
        provider = self.c._offered_load_gen.provider
        self.assertEqual(provider(), {"ue1": "12.1.1.2"})

    def test_invalid_rate_raises_before_building(self):
        for bad in (-5.0, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.c.set_offered_load(bad)
        self.assertIsNone(self.c._offered_load_gen)

    def test_stop_offered_load_tears_down(self):
        self.c.set_offered_load(10.0)
        gen = self.c._offered_load_gen
        self.c._stop_offered_load()
        self.assertTrue(gen.stopped)

    def test_load_setter_injection_path_end_to_end(self):
        # runner._resolve_offered_load_setter finds the hook, the approved
        # OfferedLoadEnvironmentDriver applies through it, reaching the generator.
        from experiments.runner import _resolve_offered_load_setter
        from experiments.environment import OfferedLoadEnvironmentDriver
        setter = _resolve_offered_load_setter(self.c)
        self.assertTrue(callable(setter))
        driver = OfferedLoadEnvironmentDriver(load_setter=setter, approved=True)
        driver.apply({"name": "Congestion", "serving_gain": 0,
                      "neighbor_gain": 0, "offered_load_mbps": 21.0},
                     0, "p0:Congestion")
        self.assertEqual(self.c._offered_load_gen.rates, [21.0])


if __name__ == "__main__":
    unittest.main()
