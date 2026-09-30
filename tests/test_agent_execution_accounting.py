"""Amendment v3.1-select10-existing3 sections 5 and 6, on a hardware-free sitting.

The trailing issued-cohort collection runs while the judged configuration is
still applied; a trial is started only when B holds its observation; and the
execution, recovery, completion, episode and service rows carry the fields the
accounting needs.  No network, no radio, no model.
"""
from __future__ import annotations

from dataclasses import replace
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from assurance.coordination.intake import _parse_time
from assurance.core.axes import TrialOutcome
from assurance.core.states import TrialState
from gui.operator.sources.kernel_live import polling_plan
from tests.test_agent_sitting import (
    AgentSittingFixture, C_ANSWER, METHOD_DETERMINISTIC, PAIR, T_ANSWER, TARGET_NCI,
    slow,
)
from tools.liveconsole import agent as agent_module
from tools.liveconsole.agent import (
    EXPERIMENT_VERSION, TIMING_COLD_START, _FormationSampler, _service_row,
)

PREPARATION_MS = 5000.0


class Fixture(AgentSittingFixture):
    def sitting(self, **kwargs):
        kwargs.setdefault("method", METHOD_DETERMINISTIC)
        kwargs.setdefault("axes", ("servingCell",))
        sitting = self.build(**kwargs)
        sitting.confirm()
        sitting.started_ms = sitting.clock.monotonic_ms()
        return sitting

    def steer(self, sitting):
        return next(c for c in sitting.controls.candidates
                    if c.configuration.get("servingCell@131") == TARGET_NCI
                    and c.configuration.get("servingCell@132") != TARGET_NCI)

    def trial(self, sitting, order=None, waited=None):
        candidate = self.steer(sitting)
        plan = polling_plan(sitting.runtime.kernel)
        order = [] if order is None else order
        waited = [] if waited is None else waited
        aggregate = sitting._aggregate_window
        conclude = sitting.runtime.path.conclude_trial
        stop = sitting.runtime.path.emergency_stop

        def aggregating(*args, **kwargs):
            order.append(("aggregate", kwargs.get("held", True)))
            waited.append(kwargs.get("waited", False))
            return aggregate(*args, **kwargs)

        def concluding(*args, **kwargs):
            order.append("conclude")
            return conclude(*args, **kwargs)

        def stopping(*args, **kwargs):
            order.append("emergency-stop")
            return stop(*args, **kwargs)

        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch.object(sitting, "_aggregate_window", side_effect=aggregating), \
                patch.object(sitting.runtime.path, "conclude_trial", side_effect=concluding), \
                patch.object(sitting.runtime.path, "emergency_stop", side_effect=stopping):
            return sitting._run_trial("T0", candidate.control_id,
                                      sitting.catalog_of_control[candidate.control_id], {},
                                      plan.observation_polls, plan)


class TheTrailingCollectionIsHeld(Fixture):
    def test_the_cohort_is_collected_before_finalize_or_rollback(self):
        order = []
        trial = self.trial(self.sitting(), order)
        self.assertEqual([("aggregate", True), "conclude"], order)
        # No tagged-echo source here, so no trailing collection ran to stamp.
        self.assertIsNone(trial.window["trailingCollectionEnd"])

    def test_an_emergency_stop_reads_no_cohort_under_the_restored_baseline(self):
        sitting = self.sitting()
        sitting.stopped = True
        order = []
        trial = self.trial(sitting, order)
        self.assertEqual(["emergency-stop", ("aggregate", False)], order)
        recovery = trial.kernel["recovery"]
        self.assertTrue(recovery["requested"])
        self.assertIsNotNone(recovery["requestedAt"])

    def test_the_runtime_reports_its_phases_in_order(self):
        sitting = self.sitting()
        candidate = self.steer(sitting)
        plan = polling_plan(sitting.runtime.kernel)
        phases = []
        sitting.runtime.run_candidate(
            sitting.catalog_of_control[candidate.control_id],
            observation_polls=2, cadence_ms=plan.cadence_ms, settle_ms=0,
            on_phase=phases.append)
        self.assertEqual(["apply-start", "applied", "hold-end", "trailing-end",
                          "conclude-start", "concluded"], phases)

    def test_the_runtime_keeps_polling_through_the_trailing_collection(self):
        sitting = self.sitting()
        candidate = self.steer(sitting)
        plan = polling_plan(sitting.runtime.kernel)
        events = []
        sitting.runtime.run_candidate(
            sitting.catalog_of_control[candidate.control_id],
            observation_polls=2, cadence_ms=1000, settle_ms=0, trailing_ms=2500,
            on_poll=lambda index: events.append(index), on_phase=events.append)
        # Three more collector ticks (2500 ms at a 1 s cadence) keep the Kernel's
        # grid fresh while the configuration stays applied.
        self.assertEqual([1, 2, "hold-end", 3, 4, 5, "trailing-end"],
                         [item for item in events
                          if item not in ("apply-start", "applied", "conclude-start",
                                          "concluded")])

    def test_synthetic_trailing_collection_is_outside_the_judged_window(self):
        """Synthetic 2 s trailing deadline on a hardware-free sitting."""
        sitting = self.sitting()
        order, waited = [], []
        with patch.object(sitting, "_trailing_ms", return_value=2000.0):
            trial = self.trial(sitting, order, waited)
        plan = polling_plan(sitting.runtime.kernel)
        self.assertEqual([("aggregate", True), "conclude"], order)
        self.assertEqual([True], waited)          # the runtime waited; no second sleep
        self.assertEqual(plan.observation_polls, trial.window["samples"])
        self.assertEqual(trial.window["end"], sitting.service_trace[-3]["t"])


class TheExecutionAndRecoveryRecords(Fixture):
    def test_a_finalized_trial_stamps_each_execution_step_in_order(self):
        # The finalize-live path itself; the default resets every trial (2026-09-20).
        trial = self.trial(self.sitting(reset_each_trial=False))
        execution = trial.kernel["execution"]
        stamps = [_parse_time(execution[name]) for name in (
            "applicationStartedAt", "appliedAt", "holdEndedAt", "concludeStartedAt",
            "settledAt")]
        self.assertEqual(sorted(stamps), stamps)
        self.assertEqual(trial.applied_at, execution["applicationStartedAt"])
        self.assertEqual(trial.kernel["trialId"], execution["dispatchId"])
        self.assertIs(False, execution["partialApply"])
        self.assertEqual({"requested": False, "requestedAt": None, "restored": None,
                          "resolvedAt": None, "unresolved": False},
                         {k: v for k, v in trial.kernel["recovery"].items()
                          if k != "resolution"})

    def test_a_rolled_back_trial_records_a_confirmed_restoration(self):
        sitting = self.sitting(budget_trials=1)
        sitting.stopped = True
        trial = self.trial(sitting)
        recovery = trial.kernel["recovery"]
        self.assertTrue(trial.rolled_back)
        self.assertEqual((True, True, False),
                         (recovery["requested"], recovery["restored"],
                          recovery["unresolved"]))
        self.assertLessEqual(_parse_time(recovery["requestedAt"]),
                             _parse_time(recovery["resolvedAt"]))

    def test_an_unconfirmed_recovery_is_not_a_restoration(self):
        """A recovery that timed out without the Kernel's confirmation stays unknown."""
        sitting = self.sitting(budget_trials=1)
        sitting.stopped = True
        with patch.object(sitting.runtime, "trial_recovery", return_value={
                "applied": True, "resolution": None, "recoveryVerified": False}):
            recovery = self.trial(sitting).kernel["recovery"]
        self.assertEqual((True, None, None, True),
                         (recovery["requested"], recovery["restored"],
                          recovery["resolvedAt"], recovery["unresolved"]))

    def test_a_lockdown_is_a_failed_restoration_and_stays_unresolved(self):
        sitting = self.sitting(budget_trials=3)
        run = sitting.runtime.run_candidate

        def incident(*args, **kwargs):
            trial_id, report = run(*args, **kwargs)
            return trial_id, replace(report, terminal_state=TrialState.INCIDENT_LOCKDOWN,
                                     outcome=TrialOutcome.NOT_SETTLED)

        with patch.object(sitting.runtime, "run_candidate", side_effect=incident):
            sitting.run()
        record = sitting.episode_record()
        recovery = record["trials"][-1]["kernel"]["recovery"]
        self.assertEqual((True, False, True),
                         (recovery["requested"], recovery["restored"],
                          recovery["unresolved"]))
        self.assertEqual("INCIDENT_LOCKDOWN",
                         record["completion"]["unresolved"][0]["terminalState"])

    def test_a_partial_apply_is_flagged_on_the_execution_row(self):
        from assurance.core.states import StopReason
        sitting = self.sitting(budget_trials=1)
        run = sitting.runtime.run_candidate

        def partial(*args, **kwargs):
            trial_id, report = run(*args, **kwargs)
            return trial_id, replace(report, terminal_state=TrialState.SETTLED_NON_SUCCESS,
                                     outcome=TrialOutcome.SAFETY_STOPPED,
                                     stop_reason=StopReason.PARTIAL_APPLY)

        with patch.object(sitting.runtime, "run_candidate", side_effect=partial):
            sitting.run()
        self.assertTrue(sitting.grid.trials[-1].kernel["execution"]["partialApply"])


class TheEpisodeRow(Fixture):
    def test_version_code_request_load_and_final_observation_are_on_the_record(self):
        sitting = self.sitting(budget_trials=1, condition={"offeredLoadMbps": 8.0})
        sitting.run()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AIC_EXPERIMENT_VERSION", None)
            record = sitting.episode_record()
        self.assertEqual(EXPERIMENT_VERSION, record["experimentVersion"])
        self.assertEqual("v3.1-select10-existing3", record["experimentVersion"])
        self.assertIn("codeRevision", record)
        self.assertEqual(sitting.request.to_record(), record["request"])
        self.assertEqual(8.0, record["offeredLoadMbps"])
        self.assertEqual(sitting.service_trace[-1]["t"],
                         record["completion"]["finalObservationAt"])
        self.assertEqual([], record["completion"]["unresolved"])
        with patch.dict(os.environ, {"AIC_EXPERIMENT_VERSION": "v3.0-other"}):
            self.assertEqual("v3.0-other", sitting.episode_record()["experimentVersion"])

    def test_code_revision_is_none_when_git_is_unavailable(self):
        agent_module._code_revision.cache_clear()
        self.addCleanup(agent_module._code_revision.cache_clear)
        with patch.object(agent_module.Path, "read_text", side_effect=OSError("no .git")):
            self.assertIsNone(agent_module._code_revision())
        agent_module._code_revision.cache_clear()
        with patch("subprocess.run", side_effect=AssertionError("subprocess forbidden")):
            revision = agent_module._code_revision()
        self.assertTrue(revision is None or len(revision) == 40)


class TheObservationReserve(Fixture):
    def test_no_trial_starts_when_b_cannot_hold_its_observation(self):
        sitting = self.build(method=METHOD_DETERMINISTIC, axes=("servingCell",),
                             budget_trials=2, deadline_ms=1500)
        sitting.confirm()
        summary = sitting.run()
        self.assertEqual("DEADLINE", summary["termination"])
        self.assertIn("cannot hold another observation", sitting.termination_detail)
        self.assertEqual([], sitting.runtime.trials)


class TheServiceTrace(unittest.TestCase):
    def test_gaps_are_timestamped_and_a_failed_poll_is_an_invalid_row(self):
        observer = SimpleNamespace(failures=[{"ueId": "131", "error": "old"}],
                                   intervals={"dlGoodputMbps@132": {"startMs": 1, "endMs": 2}})

        def sample():
            observer.failures.append({"ueId": "131", "error": "ssh exit 255"})
            return {"dlGoodputMbps@132": 3.0}

        observer.sample = sample
        errors = []
        row = _service_row(observer, lambda: "T1", errors)
        self.assertEqual((True, {"dlGoodputMbps@132": 3.0}), (row["valid"], row["kpis"]))
        self.assertEqual([{"ueId": "131", "error": "ssh exit 255", "at": "T1"}], row["gaps"])
        self.assertNotIn("at", observer.failures[0])
        self.assertEqual({"startMs": 1, "endMs": 2}, row["intervals"]["dlGoodputMbps@132"])
        observer.sample = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        row = _service_row(observer, lambda: "T2", errors)
        self.assertEqual((False, {}, {}), (row["valid"], row["kpis"], row["intervals"]))
        self.assertEqual(["RuntimeError: down"], errors)


class TheColdStartServiceTrace(Fixture):
    def test_service_is_sampled_from_input_release_through_formation(self):
        resolver, models = self.scripted({
            "target": [slow(T_ANSWER, PREPARATION_MS)],
            "control": [C_ANSWER], "trajectory": [PAIR]})
        sitting = self.build(resolver=resolver, role_models=models, budget_trials=1,
                             timing_mode=TIMING_COLD_START)
        t0 = _parse_time(sitting.timing["t0"])
        formed = [_parse_time(row["t"]) for row in sitting.service_trace]
        self.assertTrue(formed)
        self.assertLessEqual(formed[0] - t0, 1000.0)
        # Rows at least once per cadence up to the end of preparation.
        self.assertGreaterEqual(formed[-1], _parse_time(sitting.timing["prepEnd"]) - 1000.0)
        self.assertTrue(all(b - a <= 1000.0 for a, b in zip(formed, formed[1:])))
        sitting.confirm()
        sitting.run()
        stamps = [_parse_time(row["t"]) for row in sitting.episode_record()["serviceTrace"]]
        self.assertEqual(formed, stamps[:len(formed)])
        self.assertEqual(sorted(stamps), stamps)

    def test_the_background_sampler_stops_on_request_and_at_its_horizon(self):
        """Wall clock, fake observer: nothing is polled after stop or past until_ms."""
        clock = SimpleNamespace(now=lambda: "now",
                                monotonic_ms=lambda: time.monotonic() * 1000.0)
        observer = SimpleNamespace(sample=lambda: {}, failures=[])
        sampler = _FormationSampler(observer, clock, 5)
        sampler.start()
        deadline = time.monotonic() + 5
        while len(sampler.rows) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        sampler.stop()
        self.assertFalse(sampler._thread.is_alive())
        count = len(sampler.rows)
        time.sleep(0.03)
        self.assertEqual(count, len(sampler.rows))
        bounded = _FormationSampler(observer, clock, 5, until_ms=clock.monotonic_ms() + 20)
        bounded.start()
        bounded._thread.join(timeout=5)
        self.assertFalse(bounded._thread.is_alive())
        self.assertLessEqual(len(bounded.rows), 5)


if __name__ == "__main__":
    unittest.main()
