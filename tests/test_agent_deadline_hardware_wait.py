# SPDX-License-Identifier: MIT
"""B charges the search, not the radio.

The owner's 2026-09-16 instruction -- the breakage is the hardware's fault, so
wait -- was first met by removing the total deadline B altogether
(``AIC_DEADLINE_S=off``).  That kept episodes alive but left the manuscript's
480 s attainment curves with no clock to be drawn against.  The owner's
decision of the same day is to keep B at 480 s and relieve it of the hardware
wait, the way the 240 s formation allowance was already relieved.

The relief is ``_hardware_wait_ms``, read off the disconnect records, so the
seconds forgiven are the same seconds ``hardwareWaitMs`` reports.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from datetime import datetime, timedelta, timezone

from tools.liveconsole.agent import AgentSitting

_EPOCH = datetime(2026, 9, 23, 11, 21, tzinfo=timezone.utc)


def _iso(ms):
    return (_EPOCH + timedelta(milliseconds=ms)).isoformat().replace("+00:00", "Z")


def _span(start_ms, length_ms):
    return {"start": _iso(start_ms), "end": _iso(start_ms + length_ms), "waitedMs": length_ms}


class DeadlineBIsRelievedOfTheHardwareWait(unittest.TestCase):

    def sitting(self, elapsed_ms, wait_ms, deadline_ms=480000.0):
        sitting = AgentSitting.__new__(AgentSitting)
        sitting.request = SimpleNamespace(deadline_ms=deadline_ms)
        sitting._elapsed_ms = lambda: float(elapsed_ms)
        sitting.composition_wait_ms = float(wait_ms)
        # 2026-09-24: the relief is the union of time spans, so a wait is its span.
        sitting.composition_wait_log = [_span(0.0, wait_ms)] if wait_ms else []
        sitting.hardware_disconnects = []
        sitting.ended = []
        sitting._end = lambda reason, detail: sitting.ended.append((reason, detail))
        return sitting

    def test_a_search_that_really_spent_b_still_ends(self):
        sitting = self.sitting(elapsed_ms=480_000, wait_ms=0)
        self.assertTrue(sitting._past_deadline())
        self.assertEqual(1, len(sitting.ended))

    def test_the_same_elapsed_time_does_not_end_it_when_the_radio_took_it(self):
        # Attempt 154's shape: 776.8 s elapsed of which 776.8 s was hardware.
        sitting = self.sitting(elapsed_ms=480_000, wait_ms=200_000)
        self.assertFalse(sitting._past_deadline())
        self.assertEqual([], sitting.ended)

    def test_the_reserve_check_is_relieved_too(self):
        # 470 s elapsed, 100 s of it waiting: 370 s spent, so a 60 s reserve fits.
        sitting = self.sitting(elapsed_ms=470_000, wait_ms=100_000)
        self.assertFalse(sitting._past_deadline(reserve_ms=60_000))
        # Without the relief the same numbers would refuse the observation.
        bare = self.sitting(elapsed_ms=470_000, wait_ms=0)
        self.assertTrue(bare._past_deadline(reserve_ms=60_000))
        self.assertIn("cannot hold", bare.ended[0][1])

    def test_the_relieved_amount_is_what_the_evidence_reports(self):
        sitting = self.sitting(elapsed_ms=0, wait_ms=152_169)
        sitting.hardware_disconnects = [{"waitedMs": 204_204.0,
                                         "waits": [_span(200_000.0, 204_204.0)]}]
        self.assertEqual(356_373.0, sitting._hardware_wait_ms())

    def test_no_deadline_declared_is_not_a_deadline_of_zero(self):
        sitting = self.sitting(elapsed_ms=10**9, wait_ms=0, deadline_ms=None)
        self.assertFalse(sitting._past_deadline())


class TheWaitIsRelievedWhileItLasts(unittest.TestCase):
    """Board 462 (2026-09-23): ``waitedMs`` was written only when the wait ended,
    so during it B kept being charged; the sitting gave up after 33 s and the
    steered UE came back 78 s later."""

    def sitting(self, *, back_after_ms):
        clock = SimpleNamespace(t=0.0)
        clock.monotonic_ms = lambda: clock.t
        clock.now = lambda: _iso(clock.t)

        def sleep_ms(ms):
            clock.t += ms
        clock.sleep_ms = sleep_ms
        sitting = AgentSitting.__new__(AgentSitting)
        sitting.clock = clock
        sitting.started_ms = -400_000.0          # 400 s of search already spent
        sitting.request = SimpleNamespace(deadline_ms=480_000.0)
        sitting.composition_wait_ms = 0.0
        sitting.stopped = False
        sitting.ended = []
        sitting._end = lambda reason, detail: sitting.ended.append((reason, detail))
        sitting.hardware_disconnects = [{"ues": {"ue2": {"previousAmfUeNgapId": 640}},
                                         "reregistered": None, "trials": []}]
        sitting.amf_of = lambda ue: 647 if clock.t >= back_after_ms else None
        return sitting

    def test_a_ue_back_after_78_s_is_still_awaited(self):
        sitting = self.sitting(back_after_ms=78_000)
        self.assertTrue(sitting._await_reregistration(["ue2"], reserve_ms=60_000))
        record = sitting.hardware_disconnects[0]
        self.assertTrue(record["reregistered"])
        self.assertGreaterEqual(record["waitedMs"], 78_000)
        self.assertEqual([], sitting.ended)

    def test_the_search_time_already_spent_still_counts(self):
        sitting = self.sitting(back_after_ms=10**9)
        sitting.started_ms = -470_000.0          # 470 s spent: a 60 s reserve never fits
        self.assertFalse(sitting._await_reregistration(["ue2"], reserve_ms=60_000))


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
