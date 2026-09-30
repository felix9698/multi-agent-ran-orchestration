from copy import deepcopy
import unittest
from unittest.mock import patch

from oran.rapp.gui_entry import run_gui_once


class OranGuiHeadlessEquivalenceTest(unittest.TestCase):
    def test_terminal_outcome_and_evidence_are_presentation_independent(self):
        request = {"intentText": "UE throughput 10 Mbps 이상 유지"}
        result = {
            "success": True,
            "terminal_outcome": "commit_original",
            "evidence": [{"plane": "A1", "policyState": "ACTIVE"}],
        }
        presented = []
        with patch("oran.rapp.gui_entry.run_once", return_value=deepcopy(result)) as authority:
            gui_result = run_gui_once(
                integration_path="values.json", request=request,
                present=lambda value: presented.append(value))
        authority.assert_called_once()
        self.assertEqual(gui_result["terminal_outcome"], result["terminal_outcome"])
        self.assertEqual(gui_result["evidence"], result["evidence"])
        self.assertEqual(presented, [result])

        def broken_display(_value):
            raise RuntimeError("widget failure")

        with patch("oran.rapp.gui_entry.run_once", return_value=deepcopy(result)):
            broken_result = run_gui_once(
                integration_path="values.json", request=request, present=broken_display)
        self.assertEqual(broken_result, result)
