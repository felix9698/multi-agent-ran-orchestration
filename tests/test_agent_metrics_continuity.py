# SPDX-License-Identifier: MIT
"""Sustained delivery is a run-length question, and a gap in the record is not a run.

A mean says nothing about how a shortfall was distributed: the same mean is one
long outage or a scatter of dips, and only the second is tolerable for a video
owner.  ``delivery_continuity`` reads the 1-s stream ``service_deficit``
already slots the service trace into and reports the longest run of bins below
a floor.

Missing bins get two numbers rather than one guess, which is the rule the
redesign states: invalid collection stays unknown and is never a continuity
violation, while a genuine break is a failed requirement and not an invalid
observation.
"""
from __future__ import annotations

import unittest

from experiments.agent_metrics import delivery_continuity

KEY = 'dlGoodputMbps@ue1'
FLOOR = 4.0
MAX_RUN = 2


def episode(values, horizon_ms=10_000, valid=None):
    """One sample per second; ``None`` in ``values`` means the second is absent."""
    trace = []
    for index, value in enumerate(values):
        if value is None:
            continue
        trace.append({'t': index * 1000, 'kpis': {KEY: value},
                      'valid': True if valid is None else valid[index]})
    return {'timing': {'t0': 0}, 'budget': {'horizonHMs': horizon_ms},
            'serviceTrace': trace}


class ARunIsCountedOnlyOverObservedSeconds(unittest.TestCase):

    def test_a_delivered_horizon_passes_with_no_run(self):
        result = delivery_continuity(episode([8.0] * 10), KEY, FLOOR, MAX_RUN)
        self.assertEqual('PASS', result['verdict'])
        self.assertEqual(0, result['longestRun'])
        self.assertEqual([], result['runs'])

    def test_scattered_dips_are_not_a_continuity_failure(self):
        # Five low seconds, never two in a row: the mean is poor, delivery is not.
        result = delivery_continuity(
            episode([1.0, 8.0, 1.0, 8.0, 1.0, 8.0, 1.0, 8.0, 1.0, 8.0]),
            KEY, FLOOR, MAX_RUN)
        self.assertEqual('PASS', result['verdict'])
        self.assertEqual(1, result['longestRun'])

    def test_one_long_outage_fails_even_with_the_same_count_of_low_seconds(self):
        result = delivery_continuity(
            episode([8.0, 8.0, 1.0, 1.0, 1.0, 1.0, 1.0, 8.0, 8.0, 8.0]),
            KEY, FLOOR, MAX_RUN)
        self.assertEqual('FAIL', result['verdict'])
        self.assertEqual(5, result['longestRun'])
        self.assertEqual([{'startMs': 2000, 'bins': 5}], result['runs'])

    def test_a_run_exactly_at_the_limit_is_allowed(self):
        result = delivery_continuity(
            episode([8.0, 1.0, 1.0, 8.0, 8.0, 8.0, 8.0, 8.0, 8.0, 8.0]),
            KEY, FLOOR, MAX_RUN)
        self.assertEqual('PASS', result['verdict'])
        self.assertEqual(2, result['longestRun'])


class AMissingSecondDecidesNothingByItself(unittest.TestCase):

    def test_a_gap_that_could_bridge_two_runs_is_unknown_not_a_violation(self):
        # Low, low, missing, low, low: proven 2, permitted 5.
        result = delivery_continuity(
            episode([1.0, 1.0, None, 1.0, 1.0, 8.0, 8.0, 8.0, 8.0, 8.0]),
            KEY, FLOOR, MAX_RUN)
        self.assertEqual('UNKNOWN', result['verdict'])
        self.assertEqual(2, result['longestRun'])
        self.assertEqual(5, result['longestPossibleRun'])
        self.assertEqual([{'startMs': 2000, 'endMs': 3000}], result['missingIntervals'])

    def test_a_gap_cannot_rescue_a_break_the_record_already_proves(self):
        result = delivery_continuity(
            episode([1.0, 1.0, 1.0, 8.0, None, 8.0, 8.0, 8.0, 8.0, 8.0]),
            KEY, FLOOR, MAX_RUN)
        self.assertEqual('FAIL', result['verdict'])
        self.assertEqual(3, result['longestRun'])

    def test_a_gap_short_enough_to_stay_within_the_limit_still_passes(self):
        result = delivery_continuity(
            episode([8.0, 1.0, None, 8.0, 8.0, 8.0, 8.0, 8.0, 8.0, 8.0]),
            KEY, FLOOR, MAX_RUN)
        self.assertEqual('PASS', result['verdict'])
        self.assertEqual(2, result['longestPossibleRun'])

    def test_an_invalid_sample_is_a_gap_and_not_a_low_second(self):
        values = [8.0] * 10
        valid = [True] * 10
        valid[3] = valid[4] = valid[5] = False
        result = delivery_continuity(episode(values, valid=valid), KEY, FLOOR, MAX_RUN)
        self.assertEqual('UNKNOWN', result['verdict'])
        self.assertEqual(0, result['longestRun'])
        self.assertEqual(3, result['longestPossibleRun'])

    def test_a_record_with_nothing_collected_is_unknown(self):
        result = delivery_continuity(episode([None] * 10), KEY, FLOOR, MAX_RUN)
        self.assertEqual('UNKNOWN', result['verdict'])
        self.assertEqual(0, result['observedBins'])


class TheHorizonIsTheEpisodeSAndIsNeverInvented(unittest.TestCase):

    def test_no_declared_horizon_reports_nothing_rather_than_zero(self):
        result = delivery_continuity(
            {'timing': {'t0': 0}, 'budget': {}, 'serviceTrace': []},
            KEY, FLOOR, MAX_RUN)
        self.assertIsNone(result['longestRun'])
        self.assertEqual('UNKNOWN', result['verdict'])

    def test_a_horizon_that_is_not_whole_bins_is_refused(self):
        with self.assertRaises(ValueError):
            delivery_continuity(episode([8.0], horizon_ms=1500), KEY, FLOOR, MAX_RUN)

    def test_samples_outside_the_horizon_are_ignored(self):
        record = episode([8.0] * 10)
        record['serviceTrace'].append({'t': 20_000, 'kpis': {KEY: 0.0}, 'valid': True})
        result = delivery_continuity(record, KEY, FLOOR, MAX_RUN)
        self.assertEqual('PASS', result['verdict'])


if __name__ == '__main__':  # pragma: no cover - convenience
    unittest.main()
