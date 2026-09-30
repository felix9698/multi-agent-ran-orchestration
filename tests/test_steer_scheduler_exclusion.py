"""A steer and a per-UE scheduler setting on one UE cannot be expressed together.

2026-09-19 board 20260919T145410: pfWeight@ue1 8.0 settled on 87654321; a candidate
steered ue1 to 12345678 keeping 8.0; the cell-scoped policy stayed behind, COMMIT
read PARTIAL_APPLY and the rollback could not restore it.
"""
import unittest

from tools.liveconsole import agent


class SteerAndSchedulerAreExclusiveOnOneUe(unittest.TestCase):
    RULES = agent._compatibility_rules(agent.FunctionCatalog(()), ("ue1", "ue2"), {})
    BASE = {"servingCell@ue1": "87654321", "pfWeight@ue1": "1.0", "dlPrbCap@ue1": "0",
            "pfWeight@ue2": "1.0"}

    def test_the_live_rules_carry_the_pairs(self):
        rules = agent._compatibility_rules(agent.FunctionCatalog(()), ("ue1",), {})
        self.assertIn(("servingCell", "pfWeight"), rules.exclusive_axes)
        self.assertIn(("servingCell", "dlPrbCap"), rules.exclusive_axes)

    def test_steer_with_a_held_weight_is_refused(self):
        self.assertTrue(self.RULES.configuration_refusals(
            {**self.BASE, "servingCell@ue1": "12345678", "pfWeight@ue1": "8.0"}, self.BASE))

    def test_steer_with_a_weight_on_another_ue_is_allowed(self):
        self.assertEqual([], self.RULES.configuration_refusals(
            {**self.BASE, "servingCell@ue1": "12345678", "pfWeight@ue2": "4.0"}, self.BASE))

    def test_the_notes_say_why(self):
        notes = agent._compatibility(("ue1",), {"servingCell@ue1": (), "pfWeight@ue1": ()})
        self.assertTrue(any("servingCell@ue1 and pfWeight@ue1" in note for note in notes))


if __name__ == "__main__":
    unittest.main()
