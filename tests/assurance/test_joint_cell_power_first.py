"""2026-09-26: four of six v5 boards locked down on a ue1 steer (717/719/720/723).  gnb2 power
was written and restored after ue1 had been steered off gnb2, and the producer cannot address
a cell with no UE on it.  Cell power now leads the plan; steering and UE-scoped axes follow."""
import unittest

from assurance.live.joint_runtime import cell_power_first


class CellPowerFirst(unittest.TestCase):
    def test_power_leads_steering_and_ue_axes_keep_their_order(self):
        declared = [{"axis": "servingCell@ue1"}, {"axis": "dlPrbCap@ue2"},
                    {"axis": "txAttenuationDb@87654321"}, {"axis": "pfWeight@ue1"}]
        self.assertEqual(["txAttenuationDb@87654321", "servingCell@ue1", "dlPrbCap@ue2", "pfWeight@ue1"],
                         [d["axis"] for d in cell_power_first(declared)])

    def test_without_power_the_order_is_unchanged(self):
        declared = [{"axis": "servingCell@ue1"}, {"axis": "dlPrbCap@ue2"}]
        self.assertEqual(declared, cell_power_first(declared))

    def test_the_runtime_uses_it(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "assurance/live/joint_runtime.py").read_text()
        self.assertIn("declarations = cell_power_first(declarations)", src)


if __name__ == "__main__":
    unittest.main()
