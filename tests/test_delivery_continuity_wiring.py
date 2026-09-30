# SPDX-License-Identifier: MIT
"""Delivery continuity is a second question asked of the goodput numbers.

A mean over a window cannot tell one long outage from a scatter of dips, and
only the second is tolerable for sustained delivery, so v4 makes the run length
its own requirement.  It needs no extra collection: the observer publishes the
same per-sample rate under a second key and the measurement rule's ``lowRun``
statistic reduces that series differently.

Missing samples need no second number on the live path.  ``min_coverage``
already refuses a thin window as ``UNKNOWN``, which is exactly the rule the
redesign states -- invalid collection stays unknown and is never a continuity
violation, while a real break is a failed requirement.
"""
from __future__ import annotations

import unittest

from assurance.coordination.intake import (KpiObservationRule, STATISTIC_LOW_RUN,
                                           UNKNOWN, low_delivery_runs)
from assurance.coordination.tc import KPI_LOW_DELIVERY_RUN, SUPPORTED_KPIS

FLOOR = 4.0


def rule(min_coverage=0.0, window_ms=10000):
    return KpiObservationRule(kpi=KPI_LOW_DELIVERY_RUN, statistic=STATISTIC_LOW_RUN,
                              floor=FLOOR, window_ms=window_ms, min_coverage=min_coverage)


def series(values, step_ms=1000):
    return [(float(index * step_ms), value) for index, value in enumerate(values)]


class TheKpiIsDeclared(unittest.TestCase):

    def test_the_contract_knows_the_kpi(self):
        self.assertIn(KPI_LOW_DELIVERY_RUN, SUPPORTED_KPIS)

    def test_the_observer_publishes_it_beside_the_goodput(self):
        from tools.liveconsole.kpi_observer import GOODPUT_KPI, LOW_DELIVERY_RUN_KPI
        self.assertNotEqual(GOODPUT_KPI, LOW_DELIVERY_RUN_KPI)
        self.assertEqual(KPI_LOW_DELIVERY_RUN, LOW_DELIVERY_RUN_KPI)


class TheRunLengthIsTheStatistic(unittest.TestCase):

    def test_a_delivered_window_has_no_run(self):
        value, coverage = rule().aggregate(series([8.0] * 10), 9000.0, 1000.0)
        self.assertEqual(0, value)
        self.assertEqual(1.0, coverage)

    def test_scattered_dips_do_not_accumulate(self):
        value, _ = rule().aggregate(
            series([1.0, 8.0, 1.0, 8.0, 1.0, 8.0, 1.0, 8.0, 1.0, 8.0]), 9000.0, 1000.0)
        self.assertEqual(1, value, "five low bins, never two in a row")

    def test_one_outage_is_reported_at_its_full_length(self):
        value, _ = rule().aggregate(
            series([8.0, 8.0, 1.0, 1.0, 1.0, 1.0, 1.0, 8.0, 8.0, 8.0]), 9000.0, 1000.0)
        self.assertEqual(5, value, "the same count of low bins, one run")

    def test_a_sample_exactly_at_the_floor_is_not_low(self):
        value, _ = rule().aggregate(series([FLOOR] * 10), 9000.0, 1000.0)
        self.assertEqual(0, value, "the floor is strict: below it, not at it")

    def test_a_thin_window_is_unknown_and_not_a_violation(self):
        value, coverage = rule(min_coverage=0.8).aggregate(
            series([1.0, 1.0]), 9000.0, 1000.0)
        self.assertIs(UNKNOWN, value)
        self.assertLess(coverage, 0.8)

    def test_a_rule_without_a_floor_is_refused_rather_than_silently_empty(self):
        with self.assertRaises(ValueError):
            KpiObservationRule(kpi=KPI_LOW_DELIVERY_RUN, statistic=STATISTIC_LOW_RUN)

    def test_the_floor_survives_a_record_round_trip(self):
        record = rule().to_record()
        self.assertEqual(FLOOR, record["floor"])
        back = KpiObservationRule.from_record(KPI_LOW_DELIVERY_RUN, record)
        self.assertEqual(FLOOR, back.floor)
        self.assertEqual(STATISTIC_LOW_RUN, back.statistic)


class LiveAndOfflineUseOneImplementation(unittest.TestCase):

    def test_intake_delegates_to_the_metrics_core(self):
        from experiments.agent_metrics import low_delivery_runs as core
        self.assertEqual(core([1.0, 1.0, None, 1.0], FLOOR),
                         low_delivery_runs([1.0, 1.0, None, 1.0], FLOOR))

    def test_a_gap_breaks_the_proven_run_and_extends_the_permitted_one(self):
        proven, permitted, _ = low_delivery_runs([1.0, 1.0, None, 1.0, 1.0], FLOOR)
        self.assertEqual(2, proven)
        self.assertEqual(5, permitted)


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
