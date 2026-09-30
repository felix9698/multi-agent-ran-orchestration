"""Task section 14: evidence, exhaustion, Operator Abort and Emergency Stop.

The evidence rules of design section 8 -- dormant sealing, cross-epoch
compatibility, the post-closure witness, and "evidence incomplete" rather than
relaxation -- plus the two Operator controls that end a case from outside it.

What every test here is really checking is that a case ends, honestly, through
one of the six terminals in design section 8, and that nothing along the way
promoted an unproven thing into a proven one.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from assurance.contracts.harm import HarmKind
from assurance.contracts.ledgers import (
    CompatibilityCheck,
    CompatibilityRecord,
    EvidenceCell,
    EvidenceContribution,
)
from assurance.core.axes import (
    AggregateState,
    CaseTermination,
    EvidenceCellStatus,
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
    TrialOutcome,
)
from assurance.core.states import StopReason, TrialState
from assurance.gateway.mock_adapter import FaultInjection
from assurance.gateway.plan import config_hash
from assurance.kernel.kernel import KernelRefusal

from tests.assurance.fault_preservation import (
    PreservationSnapshot,
    assert_finite_terminal,
    snapshot,
)
from tests.assurance.vertical_support import (
    ACTIVE_VECTOR,
    BASELINE_CONFIG,
    CELL_ID,
    SAFE_STATE,
    SAFE_STATE_IS_BASELINE,
    START,
    VerticalFixture,
    measured,
    timeseries,
)

DORMANT_CELL = "cell/dormant"
PRIOR_EPOCH = "epoch/prior"


class LifecycleFixture(VerticalFixture):
    def cell(self, cell_id: str = CELL_ID):
        return self.kernel.reduced_state()["evidenceCells"][cell_id]

    def contribution(
        self,
        *,
        verdict: PredicateVerdict,
        suffix: str,
        reused_from_epoch: str | None = None,
    ) -> EvidenceContribution:
        return EvidenceContribution(
            contribution_id=f"contribution/{suffix}",
            trial_ref=f"trial/{suffix}",
            candidate_semantic_hash=self.kernel.current_catalog()
            .candidates[0]
            .semantic_hash,
            execution_validity=ExecutionValidity.VALID,
            measurement_sufficiency=MeasurementSufficiency.SUFFICIENT,
            predicate_verdict=verdict,
            trace_refs=(f"trace/{suffix}",),
            dependency_group=f"group/{suffix}",
            reused_from_epoch=reused_from_epoch,
        )

    def compatibility(self, *, admitted: bool, failing: str = "") -> CompatibilityRecord:
        results = {check.value: True for check in CompatibilityCheck}
        if failing:
            results[failing] = False
        return CompatibilityRecord(
            record_id=f"compatibility/{'ok' if admitted else failing or 'refused'}",
            source_epoch_ref=PRIOR_EPOCH,
            target_epoch_ref=self.epoch.epoch_id,
            candidate_semantic_hash=self.kernel.current_catalog()
            .candidates[0]
            .semantic_hash,
            results=results,
            admitted=admitted,
            refusal_reason="" if admitted else f"{failing} did not hold",
        )

    def replacement_epoch(self, suffix: str):
        """A content-addressed replacement with identical frozen semantics."""

        current = self.kernel.current_catalog()
        catalog = replace(
            current,
            contract_id=f"catalog/{suffix}",
            epoch_ref=f"epoch/{suffix}",
        )
        epoch = replace(
            self.epoch,
            contract_id=f"epoch-record/{suffix}",
            epoch_id=f"epoch/{suffix}",
            supersedes_epoch_ref=self.kernel.reduced_state()["activeEpoch"],
        )
        return epoch, catalog


class DormantEvidenceTests(LifecycleFixture, unittest.TestCase):
    def dormant_cells(self):
        return (
            EvidenceCell(
                cell_id=CELL_ID,
                target_ref="target/steer",
                candidate_semantic_hash="a" * 64,
                status=EvidenceCellStatus.OPEN,
                required_independent_contributions=1,
            ),
            EvidenceCell(
                cell_id=DORMANT_CELL,
                target_ref="target/steer",
                candidate_semantic_hash="b" * 64,
                status=EvidenceCellStatus.DORMANT_SEALED,
                required_independent_contributions=1,
                sealed_until_vector_ref="target/not-yet-active",
            ),
        )

    def test_a_dormant_cell_stays_sealed_when_written_to_early(self) -> None:
        path = self.build(evidence_cells=self.dormant_cells())
        # Compatible in every one of the nine checks: the *only* thing keeping
        # this contribution out is the seal.
        self.kernel.record_compatibility(
            CompatibilityRecord(
                record_id="compatibility/dormant",
                source_epoch_ref=PRIOR_EPOCH,
                target_epoch_ref=self.epoch.epoch_id,
                candidate_semantic_hash="b" * 64,
                results={check.value: True for check in CompatibilityCheck},
                admitted=True,
            ),
            now=self.clock(),
        )

        status = self.kernel.close_evidence(
            cell_id=DORMANT_CELL,
            contribution=EvidenceContribution(
                contribution_id="contribution/early",
                trial_ref="trial/early",
                candidate_semantic_hash="b" * 64,
                execution_validity=ExecutionValidity.VALID,
                measurement_sufficiency=MeasurementSufficiency.SUFFICIENT,
                predicate_verdict=PredicateVerdict.PASS,
                trace_refs=("trace/early",),
                reused_from_epoch=PRIOR_EPOCH,
            ),
            now=self.clock(),
        )

        self.assertIs(status, EvidenceCellStatus.DORMANT_SEALED)
        self.assertEqual(self.cell(DORMANT_CELL)["status"], "DORMANT_SEALED")
        self.clock.advance(600_000)
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=None),
            PreservationSnapshot(
                actual_outcome=None,
                harm_settlement=(),
                reserve_outstanding=0.0,
                evidence=(
                    (DORMANT_CELL, "DORMANT_SEALED", (("VALID", "SUFFICIENT", "PASS", False),)),
                    (CELL_ID, "OPEN", ()),
                ),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=(),
                trial_terminal=None,
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )

    def test_an_agent_sees_that_a_cell_is_sealed_and_not_what_is_in_it(self) -> None:
        path = self.build(evidence_cells=self.dormant_cells())

        view = path.evidence_view()

        self.assertTrue(view[DORMANT_CELL]["sealed"])
        self.assertFalse(view[CELL_ID]["sealed"])
        self.assertNotIn("contributions", view[DORMANT_CELL])

    def test_a_sealed_obligation_blocks_the_exhaustion_certificate(self) -> None:
        path = self.build(evidence_cells=self.dormant_cells())

        aggregate, certificate = self.kernel.exhaustion_certificate(
            vector_ref=ACTIVE_VECTOR
        )

        self.assertIs(aggregate, AggregateState.EVIDENCE_INCOMPLETE)
        self.assertTrue(certificate)


class CrossEpochReuseTests(LifecycleFixture, unittest.TestCase):
    def test_reuse_without_an_admitted_compatibility_record_is_refused(self) -> None:
        path = self.build()

        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.close_evidence(
                cell_id=CELL_ID,
                contribution=self.contribution(
                    verdict=PredicateVerdict.PASS,
                    suffix="reuse-unchecked",
                    reused_from_epoch=PRIOR_EPOCH,
                ),
                now=self.clock(),
            )

        self.assertEqual(refusal.exception.reason, "INCOMPATIBLE_EVIDENCE_REUSE")
        self.assertEqual(self.cell()["status"], "OPEN")
        self.clock.advance(600_000)
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=None),
            PreservationSnapshot(
                actual_outcome=None,
                harm_settlement=(),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=(),
                trial_terminal=None,
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )

    def test_reuse_with_one_failing_check_is_refused(self) -> None:
        path = self.build()
        self.kernel.record_compatibility(
            self.compatibility(admitted=False, failing=CompatibilityCheck.DRIFT.value),
            now=self.clock(),
        )

        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.close_evidence(
                cell_id=CELL_ID,
                contribution=self.contribution(
                    verdict=PredicateVerdict.PASS,
                    suffix="reuse-drifted",
                    reused_from_epoch=PRIOR_EPOCH,
                ),
                now=self.clock(),
            )

        self.assertEqual(refusal.exception.reason, "INCOMPATIBLE_EVIDENCE_REUSE")

    def test_a_compatible_reuse_is_provisional_not_a_closure(self) -> None:
        path = self.build()
        self.kernel.record_compatibility(
            self.compatibility(admitted=True), now=self.clock()
        )

        status = self.kernel.close_evidence(
            cell_id=CELL_ID,
            contribution=self.contribution(
                verdict=PredicateVerdict.PASS,
                suffix="reuse-ok",
                reused_from_epoch=PRIOR_EPOCH,
            ),
            now=self.clock(),
        )

        self.assertIs(status, EvidenceCellStatus.PROVISIONAL_HISTORICAL)
        aggregate, _ = self.kernel.exhaustion_certificate(vector_ref=ACTIVE_VECTOR)
        self.assertIs(aggregate, AggregateState.EVIDENCE_INCOMPLETE)

    def test_a_compatible_pass_after_a_closed_fail_is_a_witness(self) -> None:
        """It is appended; it does not rewrite history (design section 8)."""
        path = self.build(
            collector=timeseries(start=START, value=1.5), max_trials=1
        )
        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)
        self.assertEqual(report.evidence_status, EvidenceCellStatus.CLOSED_FAIL.value)
        self.kernel.record_compatibility(
            self.compatibility(admitted=True), now=self.clock()
        )

        status = self.kernel.close_evidence(
            cell_id=CELL_ID,
            contribution=self.contribution(
                verdict=PredicateVerdict.PASS,
                suffix="witness",
                reused_from_epoch=PRIOR_EPOCH,
            ),
            now=self.clock(),
        )

        self.assertIs(status, EvidenceCellStatus.CLOSED_FAIL)
        contributions = self.cell()["contributions"]
        self.assertEqual(len(contributions), 2)
        self.assertTrue(contributions[-1]["isPostClosureWitness"])
        self.assertEqual(self.cell()["status"], "CLOSED_FAIL")
        assert_finite_terminal(self, path, CaseTermination.VECTORS_EXHAUSTED)
        self.assertEqual(
            snapshot(self, path, trial_id=report.trial_id),
            PreservationSnapshot(
                actual_outcome="FAIL",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=(
                    (
                        CELL_ID,
                        "CLOSED_FAIL",
                        (
                            ("VALID", "SUFFICIENT", "FAIL", False),
                            ("VALID", "SUFFICIENT", "PASS", True),
                        ),
                    ),
                ),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=("ROLLED_BACK",),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="VECTORS_EXHAUSTED",
            ),
        )


class ExhaustionTests(LifecycleFixture, unittest.TestCase):
    def test_a_partial_obligation_terminates_the_case_evidence_incomplete(self) -> None:
        # A non-success trial, so nothing here is a deployed success and the
        # only question left is whether the obligation was met.
        path = self.build(max_trials=1, collector=timeseries(start=START, value=1.5))
        # One trial may run and the obligation needs two independent
        # contributions, so the case runs out of trials with the cell PARTIAL.
        self.kernel.register_evidence_cell(
            EvidenceCell(
                cell_id="cell/needs-two",
                target_ref="target/steer",
                candidate_semantic_hash=self.kernel.current_catalog()
                .candidates[0]
                .semantic_hash,
                status=EvidenceCellStatus.OPEN,
                required_independent_contributions=2,
            ),
            case_id=path.case_id,
            now=self.clock(),
        )

        report = path.run_trial(
            path.request_proposal()[0], cell_id="cell/needs-two"
        )

        self.assertEqual(report.evidence_status, EvidenceCellStatus.PARTIAL.value)
        aggregate, _ = self.kernel.exhaustion_certificate(vector_ref=ACTIVE_VECTOR)
        self.assertIs(aggregate, AggregateState.EVIDENCE_INCOMPLETE)
        with self.assertRaises(KernelRefusal) as refusal:
            path.open_trial(self.kernel.current_catalog().candidates[1].candidate_id)
        self.assertEqual(refusal.exception.reason, "TRIAL_CAP_REACHED")
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=report.trial_id),
            PreservationSnapshot(
                actual_outcome="FAIL",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=(
                    ("cell/needs-two", "PARTIAL", (("VALID", "SUFFICIENT", "FAIL", False),)),
                    (CELL_ID, "OPEN", ()),
                ),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=("ROLLED_BACK",),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )

    def test_proposal_exhaustion_is_evidence_incomplete(self) -> None:
        path = self.build()
        for _ in range(6):
            candidate_id, rejection = path.request_proposal()
            self.assertEqual(candidate_id, self.first_candidate_id())
            self.assertIsNone(rejection)

        with self.assertRaises(KernelRefusal) as refusal:
            path.open_trial(self.first_candidate_id())

        self.assertEqual(refusal.exception.reason, "PROPOSAL_CAP_REACHED")
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=None),
            PreservationSnapshot(
                actual_outcome=None,
                harm_settlement=(),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=(),
                trial_terminal=None,
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )

    def test_deadline_exhaustion_is_evidence_incomplete(self) -> None:
        path = self.build()
        self.clock.advance(600_000)

        with self.assertRaises(KernelRefusal) as refusal:
            path.open_trial(self.first_candidate_id())

        self.assertEqual(refusal.exception.reason, "CASE_DEADLINE_REACHED")
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=None),
            PreservationSnapshot(
                actual_outcome=None,
                harm_settlement=(),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=(),
                trial_terminal=None,
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )

    def test_usable_reserve_exhaustion_is_evidence_incomplete(self) -> None:
        path = self.build(max_trials=6)
        for index in range(5):
            if index and index % 2 == 0:
                epoch, catalog = self.replacement_epoch(f"reserve-{index // 2 + 1}")
                self.kernel.activate_frozen_epoch(
                    epoch, catalog, now=self.clock(), safety_relevant=False
                )
            candidate_id = self.kernel.current_catalog().candidates[index % 2].candidate_id
            trial_id = path.open_trial(candidate_id)
            path.reserve_and_stage(trial_id)
            path.prepare(trial_id)
            path.ready(trial_id)
            path.commit_decision(trial_id)
            path.commit(trial_id)
            path.enter_observation(trial_id)
            self.kernel.charge_harm(
                trial_id,
                amount=measured(20.0, "ms", f"fault14/reserve-{index}"),
                harm_kind=HarmKind.TRIAL_INDUCED,
                for_missing_interval=False,
                now=self.clock(),
            )
            self.kernel.advance_trial(
                trial_id,
                TrialState.STOPPING,
                reason=StopReason.EXECUTION_ERROR,
                now=self.clock(),
            )
            self.assertTrue(path.stop_and_rollback(trial_id))
            path.settle(trial_id, outcome=TrialOutcome.EXEC_ERROR)

        available = self.kernel.current_catalog().candidates[1].candidate_id
        with self.assertRaises(KernelRefusal) as refusal:
            path.open_trial(available)

        self.assertEqual(refusal.exception.reason, "USABLE_RESERVE_EXHAUSTED")
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        state = self.kernel.reduced_state()
        self.assertEqual(
            [trial["outcome"] for trial in state["trials"].values()],
            [TrialOutcome.EXEC_ERROR.value] * 5,
        )
        self.assertEqual(
            snapshot(self, path, trial_id=None),
            PreservationSnapshot(
                actual_outcome=None,
                harm_settlement=(
                    ("RESERVE", 20.0, False),
                    ("CHARGE", 20.0, False),
                    ("RESERVE", 20.0, False),
                    ("CHARGE", 20.0, False),
                    ("RESERVE", 20.0, False),
                    ("CHARGE", 20.0, False),
                    ("RESERVE", 20.0, False),
                    ("CHARGE", 20.0, False),
                    ("RESERVE", 20.0, False),
                    ("CHARGE", 20.0, False),
                ),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=(
                    "ROLLED_BACK",
                    "ROLLED_BACK",
                    "ROLLED_BACK",
                    "ROLLED_BACK",
                    "ROLLED_BACK",
                ),
                trial_terminal=None,
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )

    def test_a_target_vector_cannot_be_released_over_an_open_obligation(self) -> None:
        path = self.build()

        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.release_target_vector(
                vector_ref="target/does-not-exist", now=self.clock()
            )

        self.assertEqual(refusal.exception.reason, "TARGET_VECTOR_ORDER_VIOLATION")


class EpochDrainTests(LifecycleFixture, unittest.TestCase):
    def test_safety_epoch_waits_for_an_active_transaction_to_settle(self) -> None:
        path = self.build()
        trial_id, report = path.begin_trial(path.request_proposal()[0])
        self.assertIsNone(report)
        replacement_epoch, replacement_catalog = self.replacement_epoch("active")
        active_epoch = self.kernel.reduced_state()["activeEpoch"]
        event_position = self.store.last_position()

        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.activate_frozen_epoch(
                replacement_epoch, replacement_catalog, now=self.clock()
            )

        self.assertEqual(refusal.exception.reason, "EPOCH_DRAIN_REQUIRED")
        self.assertEqual(self.store.last_position(), event_position)
        self.assertEqual(self.kernel.reduced_state()["activeEpoch"], active_epoch)
        path.observe(ticks=4)
        settled = path.conclude_trial(trial_id, cell_id=CELL_ID)
        self.assertEqual(settled.outcome, TrialOutcome.SUCCESS)
        assert_finite_terminal(self, path, CaseTermination.SUCCESS)
        self.assertEqual(
            snapshot(self, path, trial_id=trial_id),
            PreservationSnapshot(
                actual_outcome="SUCCESS",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "CLOSED_PASS", (("VALID", "SUFFICIENT", "PASS", False),)),),
                configuration=(("queuePriority", "7"), ("servingCell", "cell-2")),
                locks=(),
                recovery=(),
                trial_terminal="SETTLED_SUCCESS",
                case_terminal="SUCCESS",
            ),
        )

    def test_safety_epoch_waits_for_a_live_deployment_to_drain(self) -> None:
        path = self.build()
        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)
        replacement_epoch, replacement_catalog = self.replacement_epoch("live")
        active_epoch = self.kernel.reduced_state()["activeEpoch"]
        event_position = self.store.last_position()

        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.activate_frozen_epoch(
                replacement_epoch, replacement_catalog, now=self.clock()
            )

        self.assertEqual(refusal.exception.reason, "EPOCH_DRAIN_REQUIRED")
        self.assertEqual(self.store.last_position(), event_position)
        self.assertEqual(self.kernel.reduced_state()["activeEpoch"], active_epoch)
        assert_finite_terminal(self, path, CaseTermination.SUCCESS)
        self.assertEqual(
            snapshot(self, path, trial_id=report.trial_id),
            PreservationSnapshot(
                actual_outcome="SUCCESS",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "CLOSED_PASS", (("VALID", "SUFFICIENT", "PASS", False),)),),
                configuration=(("queuePriority", "7"), ("servingCell", "cell-2")),
                locks=(),
                recovery=(),
                trial_terminal="SETTLED_SUCCESS",
                case_terminal="SUCCESS",
            ),
        )


class OperatorControlTests(LifecycleFixture, unittest.TestCase):
    def test_a_pre_commit_operator_abort_settles_with_nothing_applied(self) -> None:
        path = self.build()
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)

        report = path.abort_pre_commit(
            trial_id, outcome=TrialOutcome.OPERATOR_ABORTED, detail="Abort pressed"
        )

        self.assertEqual(report.terminal_state, TrialState.SETTLED_NON_SUCCESS)
        self.assertEqual(report.outcome, TrialOutcome.OPERATOR_ABORTED)
        self.assertEqual(self.adapter.writes, [])
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        self.assertEqual(self.kernel.reduced_state()["resourceLocks"], {})
        self.assertEqual(
            [
                entry["movementKind"]
                for entry in self.kernel.reduced_state()["harmLedger"]
            ],
            ["RESERVE", "RETURN"],
        )

    def test_a_post_commit_operator_abort_reverses_the_change(self) -> None:
        path = self.build()
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)
        path.ready(trial_id)
        path.commit_decision(trial_id)
        path.commit(trial_id)
        path.enter_observation(trial_id)

        self.kernel.advance_trial(
            trial_id,
            TrialState.STOPPING,
            reason=StopReason.OPERATOR_ABORT,
            now=self.clock(),
        )
        self.assertTrue(path.stop_and_rollback(trial_id))
        path.settle(trial_id, outcome=TrialOutcome.OPERATOR_ABORTED)

        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        self.assertEqual(
            self.kernel.reduced_state()["trials"][trial_id]["state"],
            TrialState.SETTLED_NON_SUCCESS.value,
        )
        assert_finite_terminal(self, path, CaseTermination.OPERATOR_ABORT)
        self.assertEqual(
            snapshot(self, path, trial_id=trial_id),
            PreservationSnapshot(
                actual_outcome="OPERATOR_ABORTED",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=("ROLLED_BACK",),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="OPERATOR_ABORT",
            ),
        )

    def _applied_trial(self, path, trial_id: str) -> None:
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)
        path.ready(trial_id)
        path.commit_decision(trial_id)
        path.commit(trial_id)
        path.enter_observation(trial_id)

    def test_emergency_stop_settles_when_the_safe_state_is_the_baseline(self) -> None:
        """The terminal is decided by what the deployment is observed to be in.

        With the contracted safe configuration equal to the trial's baseline,
        nothing of the change survives, recovery verifies it, and the trial
        settles as the Operator control it was: ``OPERATOR_ABORTED``, reserve
        returned, lock released, case terminal ``OPERATOR_ABORT``.
        """
        path = self.build(safe_state=SAFE_STATE_IS_BASELINE)
        trial_id = path.open_trial(path.request_proposal()[0])
        self._applied_trial(path, trial_id)

        report = path.emergency_stop(trial_id)

        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        self.assertEqual(report.terminal_state, TrialState.SETTLED_NON_SUCCESS)
        self.assertEqual(report.outcome, TrialOutcome.OPERATOR_ABORTED)
        self.assertEqual(report.stop_reason, StopReason.OPERATOR_ABORT)
        state = self.kernel.reduced_state()
        self.assertEqual(state["resourceLocks"], {})
        self.assertEqual(
            [entry["movementKind"] for entry in state["harmLedger"]],
            ["RESERVE", "RETURN"],
        )
        self.assertEqual(
            [
                envelope.payload["resolution"]
                for envelope in self.store.iterate()
                if envelope.event_kind == "TransactionResolved"
            ],
            ["ROLLED_BACK"],
        )
        self.assertEqual(self.store.uncertain_transactions(), ())
        assert_finite_terminal(self, path, CaseTermination.OPERATOR_ABORT)
        self.assertEqual(
            snapshot(self, path, trial_id=trial_id),
            PreservationSnapshot(
                actual_outcome="OPERATOR_ABORTED",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=("ROLLED_BACK",),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="OPERATOR_ABORT",
            ),
        )

    def test_emergency_stop_locks_down_when_the_safe_state_is_not_the_baseline(
        self,
    ) -> None:
        """A deployment that is safe but not where the trial started.

        Recovery cannot establish the known configuration the next trial would
        begin from, so the transaction is placed in incident lockdown -- design
        section 8's recovery failure, and one of the six finite terminals.  The
        reserve stays charged and the lock stays held, because releasing either
        would claim a known state the Kernel does not have.
        """
        path = self.build()
        trial_id = path.open_trial(path.request_proposal()[0])
        self._applied_trial(path, trial_id)

        report = path.emergency_stop(trial_id)

        self.assertEqual(self.adapter.snapshot(), dict(SAFE_STATE))
        self.assertEqual(self.adapter.live_hash(), config_hash(SAFE_STATE))
        self.assertEqual(report.terminal_state, TrialState.INCIDENT_LOCKDOWN)
        self.assertEqual(report.outcome, TrialOutcome.NOT_SETTLED)
        state = self.kernel.reduced_state()
        self.assertEqual(
            state["resourceLocks"], {"capability/steer": trial_id}
        )
        self.assertEqual(
            [entry["movementKind"] for entry in state["harmLedger"]], ["RESERVE"]
        )
        assert_finite_terminal(self, path, CaseTermination.RECOVERY_FAILURE)
        self.assertEqual(
            snapshot(self, path, trial_id=trial_id),
            PreservationSnapshot(
                actual_outcome="NOT_SETTLED",
                harm_settlement=(("RESERVE", 20.0, False),),
                reserve_outstanding=20.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 0), ("servingCell", "cell-1")),
                locks=(("capability/steer", trial_id),),
                recovery=("INCIDENT_LOCKDOWN",),
                trial_terminal="INCIDENT_LOCKDOWN",
                case_terminal="RECOVERY_FAILURE",
            ),
        )

    def test_emergency_stop_before_the_commit_line_writes_nothing(self) -> None:
        path = self.build(safe_state=SAFE_STATE_IS_BASELINE)
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)

        report = path.emergency_stop(trial_id)

        self.assertEqual(self.adapter.writes, [])
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        self.assertEqual(report.outcome, TrialOutcome.OPERATOR_ABORTED)
        self.assertEqual(report.terminal_state, TrialState.SETTLED_NON_SUCCESS)
        self.assertEqual(self.kernel.reduced_state()["resourceLocks"], {})

    def test_a_paused_case_admits_no_further_trial(self) -> None:
        path = self.build()
        self.kernel.pause_case(path.case_id, paused=True, now=self.clock())

        with self.assertRaises(KernelRefusal) as refusal:
            path.open_trial(self.first_candidate_id())

        self.assertEqual(refusal.exception.reason, "CASE_PAUSED")


class CaseTerminationConvergenceTests(LifecycleFixture, unittest.TestCase):
    """The corrected brief requires finite convergence to a Kernel terminal."""

    def test_fault_paths_reach_each_of_the_six_kernel_terminals_without_oscillation(
        self,
    ) -> None:
        success = self.build()
        success.run_trial(success.request_proposal()[0], cell_id=CELL_ID)
        assert_finite_terminal(self, success, CaseTermination.SUCCESS)

        exhausted = self.build(
            collector=timeseries(start=START, value=1.5), max_trials=1
        )
        exhausted.run_trial(exhausted.request_proposal()[0], cell_id=CELL_ID)
        assert_finite_terminal(
            self, exhausted, CaseTermination.VECTORS_EXHAUSTED
        )

        incomplete = self.build()
        self.clock.advance(600_000)
        assert_finite_terminal(
            self, incomplete, CaseTermination.EVIDENCE_INCOMPLETE
        )

        operator = self.build()
        operator_trial, early = operator.begin_trial(operator.request_proposal()[0])
        self.assertIsNone(early)
        self.kernel.advance_trial(
            operator_trial,
            TrialState.STOPPING,
            reason=StopReason.OPERATOR_ABORT,
            now=self.clock(),
        )
        self.assertTrue(operator.stop_and_rollback(operator_trial))
        operator.settle(operator_trial, outcome=TrialOutcome.OPERATOR_ABORTED)
        assert_finite_terminal(self, operator, CaseTermination.OPERATOR_ABORT)

        safety = self.build()
        safety_trial, early = safety.begin_trial(safety.request_proposal()[0])
        self.assertIsNone(early)
        self.kernel.advance_trial(
            safety_trial,
            TrialState.STOPPING,
            reason=StopReason.HARD_SAFETY_GUARD,
            now=self.clock(),
        )
        self.assertTrue(safety.stop_and_rollback(safety_trial))
        safety.settle(safety_trial, outcome=TrialOutcome.SAFETY_STOPPED)
        assert_finite_terminal(self, safety, CaseTermination.SAFETY_INCIDENT)

        recovery = self.build(
            faults=FaultInjection(
                fail_axes={"servingCell"}, fail_undo_axes={"queuePriority"}
            )
        )
        recovery.run_trial(recovery.request_proposal()[0], cell_id=CELL_ID)
        assert_finite_terminal(self, recovery, CaseTermination.RECOVERY_FAILURE)

        self.assertEqual(
            {terminal.value for terminal in CaseTermination},
            {
                "SUCCESS",
                "VECTORS_EXHAUSTED",
                "EVIDENCE_INCOMPLETE",
                "OPERATOR_ABORT",
                "SAFETY_INCIDENT",
                "RECOVERY_FAILURE",
            },
        )


if __name__ == "__main__":
    unittest.main()
