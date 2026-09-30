"""A historical deployed success cannot conceal an unrecovered later write."""
import unittest
from unittest.mock import patch

from assurance.core.axes import CaseTermination
from assurance.gateway.mock_adapter import FaultInjection
from tests.assurance.vertical_support import CELL_ID, VerticalFixture


class IncidentPrecedence(VerticalFixture, unittest.TestCase):
    def test_prior_success_does_not_override_recovery_failure(self):
        path = self.build(faults=FaultInjection(
            fail_axes={"servingCell"}, fail_undo_axes={"queuePriority"}))
        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)
        self.assertEqual("INCIDENT_LOCKDOWN", report.terminal_state.value)
        state = self.kernel.reduced_state()
        state["cases"][path.case_id]["deployedSuccess"] = True
        with patch.object(self.kernel, "_state", return_value=state):
            result = self.kernel.terminate_case(case_id=path.case_id, now=self.clock())
        self.assertEqual(CaseTermination.RECOVERY_FAILURE, result)
