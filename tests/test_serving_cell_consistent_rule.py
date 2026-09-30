"""Serving-cell membership under AIC_SERVINGCELL_CONSISTENT=1 (owner 2026-09-25, v4.7 block 4):
one missed poll keeps the membership, a cell change inside the window voids it; off = old rule."""
import importlib
import os
import unittest

from assurance.coordination import intake


def _rule(on):
    old = os.environ.pop("AIC_SERVINGCELL_CONSISTENT", None)
    try:
        if on:
            os.environ["AIC_SERVINGCELL_CONSISTENT"] = "1"
        module = importlib.reload(intake)
        return module, module.DEFAULT_OBSERVATION_RULES[module.KPI_SERVING_CELL]
    finally:
        os.environ.pop("AIC_SERVINGCELL_CONSISTENT", None)
        if old is not None:
            os.environ["AIC_SERVINGCELL_CONSISTENT"] = old


class ServingCellConsistent(unittest.TestCase):
    def tearDown(self):
        importlib.reload(intake)

    def test_one_missed_poll_keeps_membership(self):
        module, rule = _rule(True)
        rule_ms = module.KpiObservationRule(kpi=rule.kpi, settle_ms=0, window_ms=4000, statistic=rule.statistic,
                                            min_coverage=rule.min_coverage, validity_ms=rule.validity_ms)
        samples = [(6000.0 + i * 1000.0, v) for i, v in enumerate(["c1", None, "c1", "c1", "c1"])]
        value, coverage = rule_ms.aggregate(samples, 10000.0, 1000.0)
        self.assertEqual(value, "c1")
        self.assertGreaterEqual(coverage, 0.8)

    def test_a_change_inside_the_window_is_unknown(self):
        module, rule = _rule(True)
        rule_ms = module.KpiObservationRule(kpi=rule.kpi, settle_ms=0, window_ms=4000, statistic=rule.statistic,
                                            min_coverage=rule.min_coverage, validity_ms=rule.validity_ms)
        samples = [(6000.0 + i * 1000.0, v) for i, v in enumerate(["c1", "c1", "c2", "c2", "c2"])]
        value, _coverage = rule_ms.aggregate(samples, 10000.0, 1000.0)
        self.assertEqual(value, module.UNKNOWN)

    def test_off_keeps_the_old_rule(self):
        module, rule = _rule(False)
        self.assertEqual((rule.statistic, rule.min_coverage), ("last", 1.0))


if __name__ == "__main__":
    unittest.main()
