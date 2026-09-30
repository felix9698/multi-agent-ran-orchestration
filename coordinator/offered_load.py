#!/usr/bin/env python3
"""
Live offered-load background traffic generator (§1-4 exogenous environment).

The §1-4 exogenous environment on THIS testbed is the offered traffic load -
"cell load ... scheduler behavior" (§I), observation x_k = "traffic load ...
available RAN resources" (§II-B) - a RESOURCE-COMPETITION driver, NOT a dB
channel sweep and NOT the coordinator's `ci rfatt` action knob. This module is
the PHYSICAL actuator behind that environment axis: continuous iperf3 UDP pushed
from the oai-ext-dn container into each serving UE's PDU-session tun IP at a
driver-selected rate.

Wiring (all fail-closed at the environment gate):
    OfferedLoadEnvironmentDriver.apply()            # experiments/environment.py
      -> IntentCoordinator.set_offered_load(mbps)   # the injected load_setter
        -> BackgroundLoadGenerator.set_rate(mbps)   # this module

Zombie safety (the reason the KPI probe was already rewritten - see
collectors/multi_ue_collector.py::UECollector.get_throughput): EVERY iperf3
client runs INSIDE the container under `timeout`
(``docker exec oai-ext-dn timeout <N> iperf3 ...``). A host-side kill of a
``docker exec`` orphans the container-side iperf3, and those orphans pile up
until every later measurement wedges. The in-container ``timeout`` guarantees
each burst self-terminates, so nothing accumulates. This generator therefore
NEVER host-kills a running burst: on a rate change / stop it stops LAUNCHING new
bursts and lets the in-flight (already container-bounded) burst finish, so a new
rate can never overlap a still-running old burst.

KPI-measurement coordination: a background burst and the KPI throughput probe
must never hit the same UE's iperf3 server at once (two overlapping UDP tests to
one server corrupt the reading). Both acquire the coordinator's SHARED
MeasurementScheduler lock (Batch F P0-13, the same lock that already serializes
the background monitor vs. the S4 trial probe), so at most ONE iperf3 runs at a
time system-wide; the KPI/trial probe wins the lock in the gap between bursts,
within its bounded timeout. The documented tradeoff (the load briefly pauses for
the ~2 s KPI read, and bursts are time-sliced across UEs rather than fully
concurrent) is written up in docs/offered_load_hook.md.

Stdlib-only (subprocess/threading), consistent with the rest of the live path.
"""

from __future__ import annotations

import logging
import math
import subprocess
import threading
from types import SimpleNamespace
from typing import Callable, Dict, Optional

from coordinator.measurement import MeasurementBusy

EXT_DN_CONTAINER = "oai-ext-dn"


def _run(command: list[str], timeout: float):
    """Legacy load-generator subprocess boundary, locally injectable in tests."""
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False)
        return completed
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SimpleNamespace(returncode=-1, stdout="", stderr=str(exc))

logger = logging.getLogger("OfferedLoad")


def _nonneg_rate(mbps) -> float:
    """A finite, >= 0 offered rate in Mbit/s (rejects bool/NaN/inf/negative)."""
    if isinstance(mbps, bool):
        raise ValueError(f"offered load must be a number, got bool {mbps!r}")
    try:
        f = float(mbps)
    except (TypeError, ValueError):
        raise ValueError(f"offered load is not a number: {mbps!r}")
    if not math.isfinite(f) or f < 0:
        raise ValueError(f"offered load must be finite and >= 0, got {f!r}")
    return f


def _fmt_rate(mbps: float) -> str:
    """iperf3 ``-b`` bandwidth argument, e.g. 12.0 -> '12M', 2.5 -> '2.5M'."""
    return ("%gM" % float(mbps))


def _load_burst_cmd(container: str, ue_ip: str, rate_mbps: float,
                    burst_s: int, hard_s: int) -> list:
    """The argv for ONE container-timeout-wrapped iperf3 UDP background burst.

    ``docker exec <container> timeout <hard> iperf3 -c <ue_ip> -u -b <rate>M
    -t <burst> --connect-timeout 3000``. The IN-CONTAINER ``timeout <hard>`` is
    the zombie backstop (identical to the KPI probe); ``-t <burst>`` is the
    burst length and ``-b <rate>M`` the offered UDP rate. hard MUST exceed burst
    so a normally-completing burst is bounded by ``-t``, and a wedged one by
    ``timeout``."""
    return ["docker", "exec", container, "timeout", str(int(hard_s)),
            "iperf3", "-c", str(ue_ip), "-u", "-b", _fmt_rate(rate_mbps),
            "-t", str(int(burst_s)), "--connect-timeout", "3000"]


class BackgroundLoadGenerator:
    """A continuous, container-timeout-wrapped iperf3 UDP background load pushed
    into the serving UEs, coordinated with the KPI probe through the shared
    MeasurementScheduler (see the module docstring).

    Lifecycle: ``set_rate(mbps)`` re-arms the load at a new rate (0 stops it);
    ``stop()`` tears it down. A rate change tears the old generator down FIRST
    (stops launching + joins the in-flight burst) and only then starts the new
    one, so two rates can never run at once. All external effects go through the
    injectable ``runner`` / ``sleep_fn`` so tests never spawn a real subprocess.
    """

    def __init__(self, target_provider: Callable[[], Dict[str, str]], *,
                 scheduler=None, container: str = EXT_DN_CONTAINER,
                 burst_s: float = 4.0, gap_s: float = 1.0,
                 hard_margin_s: float = 6.0, lock_timeout_s: float = 10.0,
                 runner: Callable[[list, float], object] = _run,
                 sleep_fn: Optional[Callable[[float], object]] = None) -> None:
        if not callable(target_provider):
            raise ValueError("target_provider must be callable")
        if not callable(runner):
            raise ValueError("runner must be callable")
        self._targets = target_provider
        self._scheduler = scheduler
        self._container = container
        self._burst_s = float(burst_s)
        self._gap_s = float(gap_s)
        self._hard_margin_s = float(hard_margin_s)
        self._lock_timeout_s = float(lock_timeout_s)
        self._runner = runner
        self._sleep_fn = sleep_fn

        self._rate = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop: Optional[threading.Event] = None
        self._mutex = threading.Lock()          # guards rate/thread transitions
        # observability + test synchronisation: bumped after every burst cycle.
        self._bursts = 0
        self._burst_event = threading.Event()

    # -- lifecycle ---------------------------------------------------------- #

    @property
    def rate_mbps(self) -> float:
        return self._rate

    @property
    def running(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    def set_rate(self, mbps) -> None:
        """(Re)arm the offered load at ``mbps`` Mbit/s; 0 stops it. Idempotent
        cleanup: any existing generator is stopped and JOINED before a new one
        starts, so a rate change never overlaps the old and new rate."""
        rate = _nonneg_rate(mbps)
        with self._mutex:
            self._teardown_locked()
            self._rate = rate
            if rate <= 0.0:
                return
            stop = threading.Event()
            self._stop = stop
            self._burst_event.clear()
            self._bursts = 0
            t = threading.Thread(target=self._loop, args=(stop, rate),
                                 name="offered-load", daemon=True)
            self._thread = t
            t.start()

    def stop(self) -> None:
        """Stop the offered load and JOIN the in-flight burst (fail-safe cleanup
        for coordinator.stop())."""
        with self._mutex:
            self._teardown_locked()
            self._rate = 0.0

    def _teardown_locked(self) -> None:
        """Signal the current loop to stop and join it. The loop never acquires
        ``self._mutex``, so joining while holding it cannot deadlock. The join is
        bounded because each burst is container-``timeout`` limited."""
        t = self._thread
        if t is None:
            return
        if self._stop is not None:
            self._stop.set()
        # bound the join generously above one burst's container timeout so a
        # hung host-side subprocess.run cannot wedge cleanup forever.
        if t.is_alive():
            t.join(timeout=self._burst_s + self._hard_margin_s + 10.0)
        self._thread = None
        self._stop = None

    # -- worker ------------------------------------------------------------- #

    def _sleep(self, stop: threading.Event, seconds: float) -> None:
        if self._sleep_fn is not None:
            self._sleep_fn(seconds)
        else:
            stop.wait(seconds)                  # interruptible idle gap

    def _loop(self, stop: threading.Event, rate: float) -> None:
        while not stop.is_set():
            try:
                self._run_once(rate, stop)
            except Exception as e:              # a burst failure is best-effort
                logger.debug("offered-load burst cycle failed: %s", e)
            if stop.is_set():
                break
            # yield the shared lock between cycles so a waiting KPI/trial probe
            # wins it within its bounded timeout.
            self._sleep(stop, self._gap_s)

    def _run_once(self, rate: Optional[float] = None,
                  stop: Optional[threading.Event] = None) -> int:
        """Push ONE burst to every currently-serving UE (targets resolved fresh
        so a re-attached UE's new tun IP is picked up). Returns the number of
        bursts actually launched. Zero targets -> a no-op cycle (nothing to
        load yet)."""
        rate = self._rate if rate is None else rate
        if rate <= 0.0:
            return 0
        targets = self._targets() or {}
        hard = int(self._burst_s + self._hard_margin_s)
        launched = 0
        for ue_id, ue_ip in targets.items():
            if stop is not None and stop.is_set():
                break
            if not ue_ip:
                continue
            cmd = _load_burst_cmd(self._container, ue_ip, rate,
                                  int(self._burst_s), hard)
            if self._run_burst(cmd, hard):
                launched += 1
        self._bursts += 1
        self._burst_event.set()
        return launched

    def _run_burst(self, cmd: list, hard: int) -> bool:
        """Run one burst UNDER the shared measurement lock so it never overlaps a
        KPI/trial iperf3 probe. If the lock cannot be acquired within the bound
        (a probe is measuring), the burst is SKIPPED - the KPI read wins, and the
        load resumes next cycle (best-effort environment, never a safety path)."""
        sched = self._scheduler
        try:
            if sched is not None:
                with sched.probe(label="offered-load",
                                 timeout_s=self._lock_timeout_s):
                    self._runner(cmd, hard + 4)
            else:
                self._runner(cmd, hard + 4)
            return True
        except MeasurementBusy as e:
            logger.debug("offered-load burst yielded to a probe: %s", e)
            return False
