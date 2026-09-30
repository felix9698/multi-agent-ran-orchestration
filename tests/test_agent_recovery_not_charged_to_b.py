# SPDX-License-Identifier: MIT
"""A trial's roll-back is excised from B (owner 2026-09-24).

"복구 시간은 우리가 안치기로 했는데 전체 제한에서" -- the first v46r10 board
(20260923T151803, three-agent) spent about 280 s rolling back one trial and ended
``DEADLINE`` with four trials run.  The roll-back (STOP, REVERSE_ROLLBACK, the
recovery reread and any hand-back) is now relieved from B like a hardware wait:
one capped at ``HARDWARE_WAIT_CAP_MS``, all of them sharing ``EXCISED_TOTAL_CAP_MS``.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from datetime import datetime, timedelta, timezone

from experiments.agent_metrics import _service_elapsed
from tools.liveconsole.agent import AgentSitting, HARDWARE_WAIT_CAP_MS

_T0 = datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc)


def _row(seconds, excised=None):
    row = {"t": (_T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")}
    if excised:
        row["excised"] = list(excised)
    return row


def _sitting(elapsed_ms, deadline_ms=480_000.0):
    sitting = AgentSitting.__new__(AgentSitting)
    sitting.request = SimpleNamespace(deadline_ms=deadline_ms)
    sitting._elapsed_ms = lambda: float(elapsed_ms)
    sitting.composition_wait_ms = 0.0
    sitting.composition_wait_log = []
    sitting.recovery_wait_log = []
    sitting.hardware_disconnects = []
    sitting.service_trace = []
    sitting.ended = []
    sitting._end = lambda reason, detail: sitting.ended.append((reason, detail))
    return sitting


class RecoveryIsNotChargedToB(unittest.TestCase):

    def test_board_151803_shape_keeps_searching(self):
        sitting = _sitting(elapsed_ms=480_000)
        self.assertTrue(_sitting(elapsed_ms=480_000)._past_deadline())
        sitting._charge_recovery("2026-09-23T15:20:00Z", "2026-09-23T15:24:40Z")  # 280 s
        self.assertEqual(280_000.0, sitting._hardware_wait_ms())
        self.assertFalse(sitting._past_deadline())
        self.assertEqual([], sitting.ended)

    def test_one_recovery_is_relieved_at_most_one_cap(self):
        sitting = _sitting(elapsed_ms=0)
        sitting._charge_recovery("2026-09-23T15:20:00Z", "2026-09-23T15:30:00Z")  # 600 s
        self.assertEqual(HARDWARE_WAIT_CAP_MS, sitting._hardware_wait_ms())
        self.assertEqual("2026-09-23T15:25:00", sitting.recovery_wait_log[0]["end"][:19])

    def test_a_dropped_ue_waited_for_inside_the_rollback_is_relieved_once(self):
        # Spans, not sums: 15:20-15:23 with a wait 15:21-15:22 inside is 180 s.
        sitting = _sitting(elapsed_ms=0)
        sitting.hardware_disconnects = [{"waitedMs": 60_000.0, "waits": [
            {"start": "2026-09-23T15:21:00Z", "end": "2026-09-23T15:22:00Z", "waitedMs": 60_000.0}]}]
        sitting._charge_recovery("2026-09-23T15:20:00Z", "2026-09-23T15:23:00Z")  # 180 s
        self.assertEqual(180_000.0, sitting._hardware_wait_ms())

    def test_the_rollback_is_an_excision_interval(self):
        sitting = _sitting(elapsed_ms=0)
        sitting._charge_recovery("2026-09-23T15:20:00Z", "2026-09-23T15:21:00Z")
        reasons = [item["reasons"] for item in sitting._excision_record()["intervals"]]
        self.assertEqual([["recovery"]], reasons)


class ATraceStretchIsItsWallClockNotItsSampleCount(unittest.TestCase):
    """v46r10's first boards: the trace counted samples x cadence, so a stretch the
    bed filled with few samples was charged to B almost whole."""

    def test_board_153808_shape(self):
        # One flagged row after a 66 s silence, then 150 flagged rows over 260 s.
        rows = [_row(0), _row(66, ["observer-gap"]), _row(80)]
        rows += [_row(84 + i * 260 / 149, ["observer-gap"]) for i in range(150)]
        rows.append(_row(345))
        sitting = _sitting(elapsed_ms=0)
        sitting.service_trace = rows
        self.assertAlmostEqual(66_000 + 264_000, sitting._hardware_wait_ms(), delta=1.0)

    def test_board_151803_shape_is_capped_at_one_wait(self):
        # 76 flagged rows spread over 419 s: relieved up to the one-wait cap.
        rows = [_row(0)] + [_row(1 + i * 418 / 75, ["handover-overlong@ue2"])
                            for i in range(76)] + [_row(430)]
        sitting = _sitting(elapsed_ms=549_000)
        sitting.service_trace = rows
        self.assertEqual(HARDWARE_WAIT_CAP_MS, sitting._hardware_wait_ms())
        self.assertFalse(sitting._past_deadline())

    def test_a_rollback_inside_a_flagged_stretch_is_not_relieved_twice(self):
        rows = [_row(0), _row(60, ["observer-gap"]), _row(61)]
        sitting = _sitting(elapsed_ms=0)
        sitting.service_trace = rows
        sitting._charge_recovery(rows[0]["t"], rows[1]["t"])
        self.assertAlmostEqual(60_000, sitting._hardware_wait_ms(), delta=1.0)


class MetricsRemoveOverlappingIntervalsOnce(unittest.TestCase):

    def test_a_rollback_overlapping_an_observer_gap(self):
        episode = {"timing": {"t0": "2026-09-23T15:00:00Z"}, "excision": {"intervals": [
            {"start": "2026-09-23T15:01:00Z", "end": "2026-09-23T15:03:00Z", "reasons": ["recovery"]},
            {"start": "2026-09-23T15:02:00Z", "end": "2026-09-23T15:04:00Z", "reasons": ["observer-gap"]}]}}
        point = {"t": "2026-09-23T15:05:00Z"}
        # 300 s elapsed, 180 s excised (15:01-15:04), not 240 s.
        self.assertEqual(120_000.0, _service_elapsed(episode, point))

    def test_a_run_longer_than_one_wait_is_relieved_one_cap_as_b_was(self):
        episode = {"timing": {"t0": "2026-09-23T15:00:00Z"}, "excision": {
            "runCapMs": HARDWARE_WAIT_CAP_MS, "intervals": [
                {"start": "2026-09-23T15:01:00Z", "end": "2026-09-23T15:08:00Z"}]}}
        # 600 s elapsed, a 420 s run relieved 300 s.
        self.assertEqual(300_000.0, _service_elapsed(episode, {"t": "2026-09-23T15:10:00Z"}))


if __name__ == "__main__":
    unittest.main()
