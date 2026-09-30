"""KAGT lane: the Measurement Collector -- mock source and gap arithmetic.

Authority: docs/architecture/SEAMS-GATE2.md; design section 4.5 ("raw
counters, scope snapshots, timestamps, clock health, missing intervals, and
trace hashes ... directly to the Kernel"); design section 8 (no zero or
last-value substitution for a missing interval).
"""

from __future__ import annotations

import unittest

from assurance.collector import gap_detection, mock_source
from assurance.collector.samples import ClockHealth

STAMP = "2026-08-21T09:00:00.000000Z"
SCOPE = {"cellId": "87654321"}


class GapDetection(unittest.TestCase):
    def test_fully_covered_window_reports_no_gaps(self):
        observed = [
            "2026-08-21T09:00:00.000000Z", "2026-08-21T09:00:01.000000Z",
            "2026-08-21T09:00:02.000000Z",
        ]
        intervals = gap_detection.compute_missing_intervals(
            window_start="2026-08-21T09:00:00.000000Z", window_end="2026-08-21T09:00:02.000000Z",
            cadence_ms=1000, observed_at=observed,
        )
        self.assertEqual(intervals, ())

    def test_a_single_missed_instant_is_one_cadence_wide(self):
        observed = ["2026-08-21T09:00:00.000000Z", "2026-08-21T09:00:02.000000Z"]
        intervals = gap_detection.compute_missing_intervals(
            window_start="2026-08-21T09:00:00.000000Z", window_end="2026-08-21T09:00:02.000000Z",
            cadence_ms=1000, observed_at=observed, reason="kpm gap",
        )
        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0].start, "2026-08-21T09:00:01.000000Z")
        self.assertEqual(intervals[0].end, "2026-08-21T09:00:02.000000Z")
        self.assertEqual(intervals[0].reason, "kpm gap")

    def test_consecutive_misses_merge_into_one_interval(self):
        observed = ["2026-08-21T09:00:00.000000Z", "2026-08-21T09:00:04.000000Z"]
        intervals = gap_detection.compute_missing_intervals(
            window_start="2026-08-21T09:00:00.000000Z", window_end="2026-08-21T09:00:04.000000Z",
            cadence_ms=1000, observed_at=observed,
        )
        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0].start, "2026-08-21T09:00:01.000000Z")
        self.assertEqual(intervals[0].end, "2026-08-21T09:00:04.000000Z")

    def test_no_observations_at_all_covers_the_whole_window(self):
        intervals = gap_detection.compute_missing_intervals(
            window_start="2026-08-21T09:00:00.000000Z", window_end="2026-08-21T09:00:03.000000Z",
            cadence_ms=1000, observed_at=(),
        )
        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0].start, "2026-08-21T09:00:00.000000Z")
        self.assertEqual(intervals[0].end, "2026-08-21T09:00:04.000000Z")

    def test_never_returns_a_zero_or_negative_duration_interval(self):
        for interval in gap_detection.compute_missing_intervals(
            window_start="2026-08-21T09:00:00.000000Z", window_end="2026-08-21T09:00:00.000000Z",
            cadence_ms=1000, observed_at=(),
        ):
            self.assertGreater(interval.duration_ms(), 0)

    def test_rejects_a_non_positive_cadence(self):
        with self.assertRaises(ValueError):
            gap_detection.compute_missing_intervals(
                window_start=STAMP, window_end=STAMP, cadence_ms=0, observed_at=(),
            )

    def test_rejects_an_inverted_window(self):
        with self.assertRaises(ValueError):
            gap_detection.compute_missing_intervals(
                window_start="2026-08-21T09:00:05.000000Z", window_end=STAMP,
                cadence_ms=1000, observed_at=(),
            )


class MockMeasurementSourceProtocol(unittest.TestCase):
    def test_reports_unknown_clock_health_and_base_scope_before_any_poll(self):
        source = mock_source.normal_timeseries_source(scope=SCOPE, start=STAMP, count=2)
        self.assertEqual(source.clock_health(), ClockHealth.UNKNOWN)
        self.assertEqual(source.scope_snapshot(), SCOPE)

    def test_binding_a_second_sink_raises(self):
        source = mock_source.normal_timeseries_source(scope=SCOPE, start=STAMP, count=1)
        source.bind_sink(lambda sample: None)
        with self.assertRaises(RuntimeError):
            source.bind_sink(lambda sample: None)

    def test_poll_forwards_every_released_sample_to_the_bound_sink(self):
        source = mock_source.normal_timeseries_source(scope=SCOPE, start=STAMP, count=3)
        delivered = []
        source.bind_sink(delivered.append)
        polled = source.poll(now="2026-08-21T09:00:05.000000Z")
        self.assertEqual(len(polled), 3)
        self.assertEqual([s.sample_id for s in delivered], [s.sample_id for s in polled])

    def test_poll_only_releases_samples_due_by_now(self):
        source = mock_source.normal_timeseries_source(
            scope=SCOPE, start=STAMP, cadence_ms=1000, count=5
        )
        first = source.poll(now="2026-08-21T09:00:01.500000Z")
        self.assertEqual(len(first), 2)
        second = source.poll(now="2026-08-21T09:00:10.000000Z")
        self.assertEqual(len(second), 3)
        self.assertEqual(source.poll(now="2026-08-21T09:00:20.000000Z"), ())

    def test_describe_source_has_no_credential_like_field(self):
        source = mock_source.normal_timeseries_source(scope=SCOPE, start=STAMP, count=1)
        description = source.describe_source()
        self.assertIn("sourceId", description)
        for forbidden in ("secret", "password", "token", "apiKey", "privateKey"):
            self.assertNotIn(forbidden, description)


class NormalScenario(unittest.TestCase):
    def test_every_sample_is_synchronised_and_gap_free(self):
        source = mock_source.normal_timeseries_source(scope=SCOPE, start=STAMP, count=4)
        samples = source.poll(now="2026-08-21T09:00:10.000000Z")
        self.assertEqual(len(samples), 4)
        for sample in samples:
            self.assertEqual(sample.clock_health, ClockHealth.SYNCHRONISED)
            self.assertFalse(sample.has_gaps())


class GapScenario(unittest.TestCase):
    def test_the_dropped_index_never_appears_and_the_next_sample_carries_the_gap(self):
        source = mock_source.gap_source(scope=SCOPE, start=STAMP, count=5, gap_at=2)
        samples = source.poll(now="2026-08-21T09:00:10.000000Z")
        sample_ids = [s.sample_id for s in samples]
        self.assertEqual(len(samples), 4)
        self.assertNotIn("mock-gap-2", sample_ids)
        gapped = [s for s in samples if s.has_gaps()]
        self.assertEqual(len(gapped), 1)
        self.assertEqual(gapped[0].sample_id, "mock-gap-3")
        self.assertEqual(gapped[0].missing_ms(), 1000)
        # The gap is reported, never filled: no zero-valued stand-in sample
        # was substituted for the dropped observation.
        self.assertEqual(len(samples), 5 - 1)

    def test_rejects_an_out_of_range_gap_index(self):
        with self.assertRaises(ValueError):
            mock_source.gap_source(scope=SCOPE, start=STAMP, count=3, gap_at=3)


class ClockDriftScenario(unittest.TestCase):
    def test_clock_health_degrades_from_the_configured_index(self):
        source = mock_source.clock_drift_source(
            scope=SCOPE, start=STAMP, count=4, drift_at=2,
            drifted_health=ClockHealth.UNSYNCHRONISED,
        )
        samples = source.poll(now="2026-08-21T09:00:10.000000Z")
        healths = [s.clock_health for s in samples]
        self.assertEqual(
            healths,
            [ClockHealth.SYNCHRONISED, ClockHealth.SYNCHRONISED,
             ClockHealth.UNSYNCHRONISED, ClockHealth.UNSYNCHRONISED],
        )
        self.assertEqual(source.clock_health(), ClockHealth.UNSYNCHRONISED)


class StaleScenario(unittest.TestCase):
    def test_a_stale_sample_is_still_delivered_and_reports_itself_as_not_fresh(self):
        source = mock_source.stale_source(scope=SCOPE, observed_at=STAMP)
        samples = source.poll(now="2026-08-21T10:00:00.000000Z")
        self.assertEqual(len(samples), 1)
        self.assertFalse(samples[0].is_fresh("2026-08-21T10:00:00.000000Z", freshness_bound_ms=5000))


if __name__ == "__main__":
    unittest.main()
