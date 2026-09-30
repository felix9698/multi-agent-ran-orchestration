"""An incident ends the search and must not start a retention write."""
from dataclasses import replace
from unittest.mock import patch

from assurance.core.axes import CaseTermination, TrialOutcome
from assurance.core.states import TrialState
from tests.test_agent_sitting import AgentSittingFixture


class IncidentStop(AgentSittingFixture):
    def test_incident_stops_before_another_decision_or_retention(self):
        sitting = self.build(method="deterministic", axes=("servingCell",), budget_trials=3)
        sitting.confirm()
        run = sitting.runtime.run_candidate

        def incident(*args, **kwargs):
            trial_id, report = run(*args, **kwargs)
            return trial_id, replace(report, terminal_state=TrialState.INCIDENT_LOCKDOWN,
                                     outcome=TrialOutcome.NOT_SETTLED)

        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch.object(sitting.runtime, "run_candidate", side_effect=incident) as calls, \
                patch.object(sitting.runtime, "terminate", return_value=CaseTermination.RECOVERY_FAILURE):
            sitting.run()
        self.assertEqual(1, calls.call_count)
        self.assertEqual("EXECUTION_FAILURE", sitting.termination)
        self.assertFalse(sitting.retained["qualified"])
        self.assertIn("incident", sitting.retained["detail"])
