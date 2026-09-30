"""2026-09-27 v5.2: the basic monolith restated C0 settings ("explicitly repeat settings you intend
to preserve"); rows equal to C0 are no-ops and must not count toward the changed-entry limit."""
import unittest

from assurance.coordination.agents import _without_baseline_rows
from assurance.coordination.tc import FunctionSelection
from tests.assurance.test_coordination_tc import APPLIED, CATALOG


class BaselineRowsAreDropped(unittest.TestCase):
    def test_c0_valued_rows_go_and_a_real_change_stays(self):
        base = CATALOG.baselines(APPLIED)
        axis, value = next((a, v) for a, v in base.items() if a.startswith("dlPrbCap@"))
        scope = "ue@" + axis.split("@", 1)[1]
        rows = [FunctionSelection("ue-dl-prb-cap", scope, {"maxDlPrbs": value}),
                FunctionSelection("ue-dl-prb-cap", scope, {"maxDlPrbs": "12"})]
        self.assertEqual([rows[1]], _without_baseline_rows(rows, CATALOG, base, "baseline"))

    def test_all_at_c0_keeps_them_for_the_later_rules(self):
        base = CATALOG.baselines(APPLIED)
        axis, value = next((a, v) for a, v in base.items() if a.startswith("dlPrbCap@"))
        rows = [FunctionSelection("ue-dl-prb-cap", "ue@" + axis.split("@", 1)[1], {"maxDlPrbs": value})]
        self.assertEqual(rows, _without_baseline_rows(rows, CATALOG, base, "baseline"))


    def test_keep_current_and_a_wrong_field_drop_nothing(self):
        base = CATALOG.baselines(APPLIED)
        axis, value = next((a, v) for a, v in base.items() if a.startswith("dlPrbCap@"))
        scope = "ue@" + axis.split("@", 1)[1]
        rows = [FunctionSelection("ue-dl-prb-cap", scope, {"maxDlPrbs": value}),
                FunctionSelection("ue-dl-prb-cap", scope, {"maxDlPrbs": "12"})]
        self.assertEqual(rows, _without_baseline_rows(rows, CATALOG, base, "keep-current"))
        typo = [FunctionSelection("ue-dl-prb-cap", scope, {"typo": value}), rows[1]]
        self.assertEqual(typo, _without_baseline_rows(typo, CATALOG, base, "baseline"))

if __name__ == "__main__":
    unittest.main()
