"""Gate 2 acceptance: one hardware-free candidate, admission to terminal.

Task section 13's first Gate 2 criterion -- "hardware-free vertical candidate가
complete 또는 안전 rollback terminal까지 완주" -- is two claims, not one, and
this file makes both against the real components wired together: the KCON
contract family, the KERN Kernel, the KGW gateway over the mock adapter, and
the KAGT agents and collector.  Nothing is stubbed except the deployment
itself.

The remaining Gate 2 criteria live beside this file: determinism and replay in
``test_vertical_replay.py``, the fail-closed boundary in
``test_vertical_boundary.py``, and section 14's fault list in
``test_fault_*.py``.
"""

from __future__ import annotations

import unittest

from assurance.core.axes import (
    CandidateAvailability,
    CaseTermination,
    EvidenceCellStatus,
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
    TrialOutcome,
)
from assurance.core.states import StopReason, TrialState
from assurance.gateway.plan import config_hash
from assurance.gateway.write_gateway import GatewayOutcome
from assurance.kernel.kernel import KernelRefusal

from tests.assurance.vertical_support import (
    BASELINE_CONFIG,
    CELL_ID,
    START,
    VerticalFixture,
    timeseries,
)


class VerticalSuccessPathTests(VerticalFixture, unittest.TestCase):
    """The complete path: proposal to finalized, evidence-backed success."""

    def test_candidate_runs_from_admission_to_finalized_settlement(self) -> None:
        path = self.build()

        candidate_id, rejection = path.request_proposal()
        self.assertIsNone(rejection)
        report = path.run_trial(candidate_id, cell_id=CELL_ID)

        self.assertEqual(report.terminal_state, TrialState.SETTLED_SUCCESS)
        self.assertEqual(report.outcome, TrialOutcome.SUCCESS)
        self.assertIsNone(report.stop_reason)
        # Every gateway step of design section 7 ran, in order, and the two
        # that make a success a success -- the configuration reread and the
        # finalize acknowledgement -- are both present.
        self.assertEqual(
            [kind for kind, _ in report.gateway_results],
            [
                "PREPARE",
                "READY",
                "COMMIT",
                "CONFIGURATION_REREAD",
                "FINALIZE_LIVE",
            ],
        )
        self.assertTrue(
            all(outcome is GatewayOutcome.ACKED for _, outcome in report.gateway_results)
        )
        self.assertEqual(self.adapter.snapshot(), self.applied_config_for(0))
        self.assertEqual(report.evidence_status, EvidenceCellStatus.CLOSED_PASS.value)

    def test_success_needs_the_arming_the_gateway_actually_reported(self) -> None:
        path = self.build()
        path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        armed = self.contracts["harm"].watchdogs[0].watchdog_id
        self.assertEqual(self.adapter.armed_watchdogs, [armed])
        readiness = [
            envelope
            for envelope in self.store.iterate()
            if envelope.event_kind == "TrialCommitReadinessRecorded"
        ]
        self.assertEqual(len(readiness), 1)
        self.assertEqual(
            readiness[0].payload["armedWatchdogEvidenceRefs"],
            [f"watchdog:{armed}:armed"],
        )

    def test_settlement_returns_the_reserve_and_releases_the_lock(self) -> None:
        path = self.build()
        path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        state = self.kernel.reduced_state()
        movements = [
            (entry["movementKind"], float(entry["amount"]["value"]))
            for entry in state["harmLedger"]
        ]
        self.assertEqual(movements, [("RESERVE", 20.0), ("RETURN", 20.0)])
        self.assertEqual(state["resourceLocks"], {})
        self.assertEqual(self.store.uncertain_transactions(), ())

    def test_case_terminates_finitely_on_deployed_success(self) -> None:
        path = self.build()
        path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        self.assertEqual(path.terminate(), CaseTermination.SUCCESS)
        state = self.kernel.reduced_state()
        self.assertEqual(
            state["candidateAvailability"][self.first_candidate_id()],
            CandidateAvailability.CONSUMED.value,
        )

    def test_three_axes_stay_separate_on_the_settled_trial(self) -> None:
        path = self.build()
        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        evaluation = self.kernel.reduced_state()["trials"][report.trial_id]["evaluation"]
        self.assertEqual(
            evaluation["executionValidity"], ExecutionValidity.VALID.value
        )
        self.assertEqual(
            evaluation["measurementSufficiency"], MeasurementSufficiency.SUFFICIENT.value
        )
        self.assertEqual(
            evaluation["predicateVerdicts"],
            {"dl-throughput-floor": PredicateVerdict.PASS.value},
        )
        self.assertTrue(evaluation["holdComplete"])
        self.assertTrue(evaluation["traceRefs"])


class VerticalRollbackPathTests(VerticalFixture, unittest.TestCase):
    """The other terminal: a non-success reversed to a known safe state."""

    def test_semantic_non_success_rolls_back_and_settles(self) -> None:
        path = self.build(collector=timeseries(start=START, value=1.5))

        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        self.assertEqual(report.terminal_state, TrialState.SETTLED_NON_SUCCESS)
        self.assertEqual(report.outcome, TrialOutcome.FAIL)
        self.assertEqual(report.stop_reason, StopReason.SEMANTIC_NON_SUCCESS)
        # The reread between STOP and REVERSE_ROLLBACK is a read, not a write:
        # a halt reports no observation, so without it the reverse permit names
        # whatever was live before the halt, which an adapter whose halt is a
        # withdrawal refuses as REJECTED_CONFIG_MISMATCH (see
        # HaltThatRestoresTheBaselineTests).  The recovery guards are unchanged
        # -- the post-rollback reread and RECOVERY_CONFIRM both still run.
        self.assertEqual(
            [kind for kind, _ in report.gateway_results],
            [
                "PREPARE",
                "READY",
                "COMMIT",
                "STOP",
                "CONFIGURATION_REREAD",
                "REVERSE_ROLLBACK",
                "CONFIGURATION_REREAD",
                "RECOVERY_CONFIRM",
            ],
        )

    def test_rollback_restores_the_baseline_configuration(self) -> None:
        path = self.build(collector=timeseries(start=START, value=1.5))
        path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        self.assertEqual(self.adapter.live_hash(), config_hash(BASELINE_CONFIG))
        record = self.gateway.transaction_record("tx:case/steer-1:trial:1")
        self.assertEqual(record.phase.value, "ROLLED_BACK")
        self.assertEqual(record.applied_axes, ())

    def test_non_success_settles_the_ledger_and_the_evidence(self) -> None:
        path = self.build(collector=timeseries(start=START, value=1.5))
        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        self.assertEqual(report.evidence_status, EvidenceCellStatus.CLOSED_FAIL.value)
        state = self.kernel.reduced_state()
        self.assertEqual(state["resourceLocks"], {})
        self.assertEqual(
            [entry["movementKind"] for entry in state["harmLedger"]],
            ["RESERVE", "RETURN"],
        )
        self.assertEqual(self.store.uncertain_transactions(), ())

    def test_recovery_is_verified_before_the_next_trial_may_open(self) -> None:
        path = self.build(collector=timeseries(start=START, value=1.5, count=12))
        first = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        resolutions = [
            envelope.payload["resolution"]
            for envelope in self.store.iterate()
            if envelope.event_kind == "TransactionResolved"
        ]
        self.assertEqual(resolutions, ["ROLLED_BACK"])
        self.assertTrue(
            self.kernel.reduced_state()["trials"][first.trial_id]["recoveryVerified"]
        )
        # And the case is genuinely able to continue: the second candidate is
        # still available and a second trial opens against the restored
        # baseline.
        second_id = self.kernel.current_catalog().candidates[1].candidate_id
        second = path.open_trial(second_id)
        self.assertEqual(
            self.kernel.reduced_state()["trials"][second]["state"],
            TrialState.PROPOSED.value,
        )


class PlanBindingTests(VerticalFixture, unittest.TestCase):
    """An advisory names a candidate; the candidate fixes the change."""

    def test_plan_must_realise_the_frozen_candidate_exactly(self) -> None:
        path = self.build()
        trial_id = path.open_trial(path.request_proposal()[0])
        self.kernel.advance_trial(trial_id, TrialState.VALIDATING, now=self.clock())
        self.kernel.reserve(trial_id, now=self.clock())
        self.kernel.advance_trial(trial_id, TrialState.RESERVED, now=self.clock())

        tampered = path.plan_for(trial_id)
        tampered["steps"] = [{"axis": "servingCell", "value": "cell-9"}]
        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.stage_actuation_plan(
                trial_id, plan=tampered, now=self.clock()
            )

        self.assertEqual(
            refusal.exception.reason, "PLAN_DOES_NOT_REALISE_CANDIDATE"
        )
        self.assertEqual(self.adapter.writes, [])

    def test_plan_must_arm_the_epoch_frozen_watchdog_set(self) -> None:
        path = self.build()
        trial_id = path.open_trial(path.request_proposal()[0])
        self.kernel.advance_trial(trial_id, TrialState.VALIDATING, now=self.clock())
        self.kernel.reserve(trial_id, now=self.clock())
        self.kernel.advance_trial(trial_id, TrialState.RESERVED, now=self.clock())

        unarmed = path.plan_for(trial_id)
        unarmed["watchdogs"] = []
        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.stage_actuation_plan(trial_id, plan=unarmed, now=self.clock())

        self.assertEqual(refusal.exception.reason, "PLAN_WATCHDOG_SET_MISMATCH")

    def test_no_permit_may_be_issued_before_a_plan_is_staged(self) -> None:
        path = self.build()
        trial_id = path.open_trial(path.request_proposal()[0])
        self.kernel.advance_trial(trial_id, TrialState.VALIDATING, now=self.clock())
        self.kernel.reserve(trial_id, now=self.clock())
        self.kernel.advance_trial(trial_id, TrialState.RESERVED, now=self.clock())
        self.kernel.advance_trial(trial_id, TrialState.PREPARING, now=self.clock())

        from assurance.gateway.token import TokenKind

        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.issue_token(
                trial_id, token_kind=TokenKind.PREPARE, now=self.clock()
            )

        self.assertEqual(refusal.exception.reason, "ACTUATION_PLAN_NOT_STAGED")

    def test_a_staged_plan_cannot_be_replaced_under_a_live_permit(self) -> None:
        path = self.build()
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)

        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.stage_actuation_plan(
                trial_id, plan=path.plan_for(trial_id), now=self.clock()
            )

        self.assertEqual(refusal.exception.reason, "PLAN_STAGED_UNDER_LIVE_PERMIT")

    def test_the_permit_names_the_configuration_the_gateway_must_observe(self) -> None:
        """The permit's expected hash is a configuration digest, not a meaning.

        Before any readback it is the staged plan's baseline; afterwards it is
        the last configuration the gateway actually read.
        """
        path = self.build()
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)
        trial = self.kernel.reduced_state()["trials"][trial_id]
        self.assertEqual(trial["planBaselineHash"], config_hash(BASELINE_CONFIG))
        self.assertNotEqual(trial["planBaselineHash"], trial["candidateSemanticHash"])

        path.prepare(trial_id)
        path.ready(trial_id)
        path.commit_decision(trial_id)
        path.commit(trial_id)

        after = self.kernel.reduced_state()["trials"][trial_id]
        self.assertEqual(after["observedConfigHash"], after["planAppliedHash"])
        self.assertEqual(after["observedConfigHash"], self.adapter.live_hash())
        self.assertTrue(after["commitAcknowledged"])


if __name__ == "__main__":
    unittest.main()


class ALockdownSaysWhichPartOfTheRollbackFailed(unittest.TestCase):
    """13 of the 18 incident lockdowns on record say exactly "rollback failed; incident
    lockdown" and nothing more.  Three different faults end there -- the reverse write
    was refused, the reread that has to confirm it did not land, or the Kernel would not
    accept that reread as recovery -- and they have different remedies, so collapsing
    them into one sentence makes the biggest bucket of lost trials undiagnosable.
    """

    def setUp(self) -> None:
        from tests.assurance.vertical_support import VerticalFixture
        self.fixture = VerticalFixture()

    def _lockdown(self, **faults):
        from assurance.gateway.mock_adapter import FaultInjection
        path = self.fixture.build(collector=timeseries(start=START, value=1.5),
                                  faults=FaultInjection(**faults))
        return path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

    def test_a_refused_reverse_write_is_named(self) -> None:
        report = self._lockdown(fail_undo_axes=frozenset({"servingCell"}))
        self.assertEqual(report.terminal_state, TrialState.INCIDENT_LOCKDOWN)
        self.assertIn("rollback failed", report.detail)
        self.assertIn("reverse rollback", report.detail)
        # The gateway's own verdict survives, not just the outcome word.
        self.assertIn("PARTIAL_APPLY", report.detail)
        self.assertIn("did not restore the baseline", report.detail)

    def test_the_bare_sentence_is_no_longer_the_whole_story(self) -> None:
        report = self._lockdown(fail_undo_axes=frozenset({"servingCell"}))
        self.assertNotEqual(report.detail, "rollback failed; incident lockdown")


class ANonSuccessSaysWhichAxisDecidedIt(unittest.TestCase):
    """A settled non-success carried its outcome and threw its reasoning away.

    ``_non_success_outcome`` reads three separate axes and a mandatory-predicate
    table to reach its answer, and the record then kept only the answer.
    Attempt 150 of 2026-09-16 settled ``SETTLED_NON_SUCCESS`` with an empty
    detail on a trial whose KPIs had passed **every** target T0-T7: the record
    said a control attained the original requirement and was rolled back
    anyway, without saying by what.
    """

    def detail(self, evaluation, outcome, stop_reason=None):
        from assurance.core.axes import TrialOutcome
        from assurance.vertical import VerticalPath
        path = VerticalPath.__new__(VerticalPath)
        path._trial = lambda _id: {"evaluation": evaluation}
        return VerticalPath._non_success_detail(path, "t1", outcome, stop_reason)

    def clean(self, **overrides):
        evaluation = {"executionValidity": "VALID",
                      "measurementSufficiency": "SUFFICIENT",
                      "predicateVerdicts": {}, "mandatoryPredicateIds": []}
        evaluation.update(overrides)
        return evaluation

    def test_the_outcome_alone_is_never_the_whole_detail(self):
        from assurance.core.axes import TrialOutcome
        message = self.detail(self.clean(), TrialOutcome.FAIL)
        self.assertIn("FAIL", message)
        self.assertIn("no mandatory predicate objected", message)

    def test_an_unmeasured_trial_names_the_sufficiency_axis(self):
        from assurance.core.axes import TrialOutcome
        message = self.detail(
            self.clean(measurementSufficiency="MISSING_INTERVAL"),
            TrialOutcome.INDETERMINATE)
        self.assertIn("measurementSufficiency=MISSING_INTERVAL", message)
        self.assertNotIn("executionValidity", message)

    def test_an_undecided_mandatory_predicate_is_named(self):
        from assurance.core.axes import TrialOutcome
        message = self.detail(
            self.clean(predicateVerdicts={"p-serving": "INDETERMINATE", "p-other": "PASS"},
                       mandatoryPredicateIds=["p-serving", "p-other"]),
            TrialOutcome.INDETERMINATE)
        self.assertIn("undecided mandatory predicates: p-serving", message)
        self.assertNotIn("p-other", message.split("undecided")[1])

    def test_a_failed_mandatory_predicate_is_named_separately(self):
        from assurance.core.axes import TrialOutcome
        message = self.detail(
            self.clean(predicateVerdicts={"p-serving": "FAIL"},
                       mandatoryPredicateIds=["p-serving"]),
            TrialOutcome.FAIL)
        self.assertIn("failed mandatory predicates: p-serving", message)

    def test_a_stop_reason_is_carried_with_the_outcome(self):
        from assurance.core.axes import TrialOutcome
        message = self.detail(self.clean(), TrialOutcome.SAFETY_STOPPED,
                              stop_reason="TELEMETRY_STALE")
        self.assertIn("SAFETY_STOPPED", message)
        self.assertIn("stopped on TELEMETRY_STALE", message)
