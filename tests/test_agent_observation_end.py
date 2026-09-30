"""Finalization latency must not move the already observed KPI window."""

from unittest.mock import patch

from gui.operator.sources.kernel_live import polling_plan
from tests.test_agent_sitting import AgentSittingFixture, TARGET_NCI


class ObservationEnd(AgentSittingFixture):
    def trial_after_slow_receipt(self, *, missing=False):
        sitting = self.build(method="deterministic", axes=("servingCell",),
                             budget_trials=1, reset_each_trial=False)
        sitting.confirm()
        sitting.started_ms = sitting.clock.monotonic_ms()
        candidate = next(c for c in sitting.controls.candidates
                         if c.configuration.get("servingCell@131") == TARGET_NCI
                         and c.configuration.get("servingCell@132") != TARGET_NCI)
        plan = polling_plan(sitting.runtime.kernel)
        run_candidate = sitting.runtime.run_candidate

        def delayed_receipt(*args, **kwargs):
            result = run_candidate(*args, **kwargs)
            sitting.clock.sleep_ms(20000)
            return result

        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch.object(sitting.runtime, "run_candidate", side_effect=delayed_receipt):
            if missing:
                with patch.object(sitting.observer, "sample", return_value={}):
                    trial = sitting._run_trial("T0", candidate.control_id,
                        sitting.catalog_of_control[candidate.control_id], {},
                        plan.observation_polls, plan)
            else:
                trial = sitting._run_trial("T0", candidate.control_id,
                    sitting.catalog_of_control[candidate.control_id], {},
                    plan.observation_polls, plan)
        return sitting, trial

    def test_late_receipt_preserves_measurement_and_its_original_validity(self):
        sitting, trial = self.trial_after_slow_receipt()
        self.assertEqual("SUCCESS", trial.kernel["outcome"])
        self.assertEqual(sitting.service_trace[-1]["t"], trial.window["end"])
        self.assertLess(trial.window["end"], sitting.clock.now())
        self.assertEqual(sitting._valid_until(trial.kpis, trial.window["end"]),
                         trial.window["validUntil"])
        self.assertEqual([], trial.window["unknownKpis"])
        self.assertTrue(trial.success["T0"])

    def test_missing_observations_still_cannot_establish_success(self):
        _, trial = self.trial_after_slow_receipt(missing=True)
        self.assertEqual({}, trial.kpis)
        self.assertFalse(trial.success["T0"])
