# SPDX-License-Identifier: MIT
"""A judged window's refill stops at the per-wait cap (board 470, 2026-09-24).

v46r10 board 20260923T160544 (basic-monolith): trial 2 steered ue2, the UE never
came back to any cell, every later sample was excised, and ``_extend_hold`` kept
refilling the window.  It checked only the board-wide cap, which one continuous
stretch can no longer reach (each merged run counts at most HARDWARE_WAIT_CAP_MS),
so the runner printed nothing for 16 min and the keeper reaped the board.  It must
end ``HARDWARE_UNAVAILABLE`` at the per-wait cap instead.
"""
from __future__ import annotations

import contextlib
import io
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tools.liveconsole.agent import AgentSitting, HARDWARE_UNAVAILABLE, HARDWARE_WAIT_CAP_MS

_T0 = datetime(2026, 9, 23, 16, 9, 30, tzinfo=timezone.utc)


class _Clock:
    def __init__(self):
        self.ms = 0.0

    def monotonic_ms(self):
        return self.ms

    def sleep_ms(self, ms):
        self.ms += float(ms)

    def now(self):
        return (_T0 + timedelta(milliseconds=self.ms)).isoformat().replace("+00:00", "Z")


def _sitting():
    sitting = AgentSitting.__new__(AgentSitting)
    sitting.clock = _Clock()
    sitting.stopped = False
    sitting.termination = None
    sitting.composition_wait_ms = 0.0
    sitting.composition_wait_log = []
    sitting.recovery_wait_log = []
    sitting.hardware_disconnects = []
    sitting.service_trace = []
    sitting.ended = []
    sitting._end = lambda reason, detail: sitting.ended.append((reason, detail))
    sitting._trace_cadence_ms = lambda: 1000.0

    def sample(**flags):
        if len(sitting.service_trace) > 2000:
            raise AssertionError("the refill never stopped")
        row = {"t": sitting.clock.now(), "excised": ["handover-overlong@ue2"], **flags}
        sitting.service_trace.append(dict(row))
        return row

    sitting._sample_service_trace = sample
    return sitting


class ARefillStopsAtTheWaitCap(unittest.TestCase):

    def test_board_470_shape_ends_hardware_unavailable(self):
        sitting = _sitting()
        samples = [{"t": sitting.clock.now(), "excised": ["handover-overlong@ue2"]}]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            added = sitting._extend_hold(samples, 15, 1000, in_trial=False)
        self.assertLessEqual(added, HARDWARE_WAIT_CAP_MS / 1000 + 1)
        self.assertEqual(HARDWARE_UNAVAILABLE, sitting.ended[0][0])
        self.assertIn("refill", sitting.ended[0][1])
        # One progress line a minute, so the keeper does not read it as a hung runner.
        self.assertGreaterEqual(out.getvalue().count("waiting            :"), 4)

    def test_a_refill_that_fills_ends_nothing(self):
        sitting = _sitting()
        sitting._sample_service_trace = lambda **flags: {"t": sitting.clock.now(), **flags}
        samples = [{"t": sitting.clock.now(), "excised": ["observer-gap"]}]
        self.assertEqual(1, sitting._extend_hold(samples, 1, 1000, in_trial=False))
        self.assertEqual([], sitting.ended)


if __name__ == "__main__":
    unittest.main()


class HorizonIsSkippedWhenTheHardwareEndedTheBoard(unittest.TestCase):
    """Board 471 (09-24): after HARDWARE_UNAVAILABLE the runner observed the horizon
    for eight more silent minutes (H plus the excised 300 s) and raced the keeper's
    idle reaper.  A board the hardware ended has no service left to score."""

    def test_no_horizon_sample_after_hardware_unavailable(self):
        sitting = _sitting()
        sitting.termination = HARDWARE_UNAVAILABLE
        sitting.request = SimpleNamespace(horizon_ms=480_000)
        sitting.preflight = {}
        sitting.started_ms = 0.0
        sitting.clock.ms = 300_000.0
        sitting._observe_to_horizon(1000)
        self.assertEqual(sitting.service_trace, [])
        self.assertEqual(sitting.preflight["horizonStoppedEarly"]["reason"], "hardware-unavailable")
