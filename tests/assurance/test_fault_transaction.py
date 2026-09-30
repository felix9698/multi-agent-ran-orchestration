"""Task section 14: transaction faults, from PREPARE failure to lost ACK.

Each test injects one failure and then asserts what design section 14 requires
to be preserved through it: the actual outcome, the harm settlement, the
reserve return or charge, whether the trial's evidence counted, the live
configuration, the resource lock, the recovery resolution, and the case
terminal.  A fault that ends in a *plausible* state but loses one of those is
a failure of this file, not a passing test with a caveat.

The injected failures come from
:class:`assurance.gateway.mock_adapter.FaultInjection` -- the real adapter the
KGW lane wrote, not a test double invented here.
"""

from __future__ import annotations

import unittest

from assurance.core.axes import CaseTermination, TrialOutcome
from assurance.core.states import StopReason, TrialState
from assurance.gateway.mock_adapter import FaultInjection
from assurance.gateway.plan import config_hash
from assurance.gateway.token import KernelToken, TokenKind
from assurance.gateway.write_gateway import GatewayOutcome
from assurance.kernel.kernel import AssuranceKernel, KernelRefusal
from assurance.kernel.reducer import KernelReducer, replay, terminal_state_hash

from tests.assurance.fault_preservation import (
    PreservationSnapshot,
    assert_finite_terminal,
    snapshot,
)
from tests.assurance.vertical_support import (
    BASELINE_CONFIG,
    contract_set,
    CELL_ID,
    SAFE_STATE,
    VerticalFixture,
)

#: The plan writes axes in sorted order, so ``queuePriority`` lands first and
#: ``servingCell`` second.  Failing the second is a partial apply; failing the
#: first is a refusal with the baseline still live.
FIRST_AXIS = "queuePriority"
SECOND_AXIS = "servingCell"


class TransactionFaultFixture(VerticalFixture):
    def ledger(self):
        return [
            (entry["movementKind"], float(entry["amount"]["value"]))
            for entry in self.kernel.reduced_state()["harmLedger"]
        ]

    def resolutions(self):
        return [
            envelope.payload["resolution"]
            for envelope in self.store.iterate()
            if envelope.event_kind == "TransactionResolved"
        ]

    def evidence_status(self) -> str:
        return self.kernel.reduced_state()["evidenceCells"][CELL_ID]["status"]


class PrepareFailureTests(TransactionFaultFixture, unittest.TestCase):
    def test_prepare_failure_aborts_with_no_side_effect(self) -> None:
        path = self.build(faults=FaultInjection(fail_prepare=True), max_trials=1)

        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        self.assertEqual(report.terminal_state, TrialState.SETTLED_NON_SUCCESS)
        self.assertEqual(report.outcome, TrialOutcome.EXEC_ERROR)
        self.assertEqual(report.gateway_results, (("PREPARE", GatewayOutcome.REJECTED),))
        # Nothing was written, so there is nothing to reverse and no recovery
        # to verify -- the trial exits through PRE_COMMIT_ABORT.
        self.assertEqual(self.adapter.writes, [])
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        self.assertEqual(self.resolutions(), [])
        self.assertEqual(self.ledger(), [("RESERVE", 20.0), ("RETURN", 20.0)])
        self.assertEqual(self.kernel.reduced_state()["resourceLocks"], {})
        self.assertEqual(self.evidence_status(), "OPEN")
        self.assertEqual(self.store.uncertain_transactions(), ())
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=report.trial_id),
            PreservationSnapshot(
                actual_outcome="EXEC_ERROR",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=(),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )

    def test_configuration_drift_before_prepare_is_refused_not_overwritten(self) -> None:
        path = self.build(faults=FaultInjection(drift={SECOND_AXIS: "cell-9"}))

        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        self.assertEqual(
            report.gateway_results,
            (("PREPARE", GatewayOutcome.REJECTED_CONFIG_MISMATCH),),
        )
        self.assertEqual(self.adapter.writes, [])
        self.assertEqual(self.adapter.snapshot()[SECOND_AXIS], "cell-9")
        self.assertEqual(report.outcome, TrialOutcome.EXEC_ERROR)

    def test_a_watchdog_that_will_not_arm_blocks_the_commit_decision(self) -> None:
        armed = contract_set()["harm"].watchdogs[0].watchdog_id
        path = self.build(faults=FaultInjection(fail_arm_watchdogs={armed}))

        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        self.assertEqual(
            [outcome for kind, outcome in report.gateway_results if kind == "READY"],
            [GatewayOutcome.REJECTED],
        )
        self.assertEqual(self.adapter.armed_watchdogs, [])
        self.assertEqual(self.adapter.writes, [])
        self.assertEqual(report.terminal_state, TrialState.SETTLED_NON_SUCCESS)
        self.assertEqual(self.ledger(), [("RESERVE", 20.0), ("RETURN", 20.0)])


class PartialApplyTests(TransactionFaultFixture, unittest.TestCase):
    def test_partial_apply_is_observed_and_reversed(self) -> None:
        path = self.build(
            faults=FaultInjection(fail_axes={SECOND_AXIS}), max_trials=1
        )

        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        kinds = dict(report.gateway_results)
        self.assertEqual(kinds["COMMIT"], GatewayOutcome.PARTIAL_APPLY)
        self.assertEqual(report.stop_reason, StopReason.PARTIAL_APPLY)
        self.assertEqual(report.outcome, TrialOutcome.SAFETY_STOPPED)
        self.assertEqual(report.terminal_state, TrialState.SETTLED_NON_SUCCESS)
        # Reversed all the way back, and recorded as rolled back rather than
        # assumed.
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        self.assertEqual(self.resolutions(), ["ROLLED_BACK"])
        self.assertEqual(self.ledger(), [("RESERVE", 20.0), ("RETURN", 20.0)])
        self.assertEqual(self.kernel.reduced_state()["resourceLocks"], {})
        # An invalid execution contributes nothing to a closure quota.
        self.assertEqual(self.evidence_status(), "OPEN")
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=report.trial_id),
            PreservationSnapshot(
                actual_outcome="SAFETY_STOPPED",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=("ROLLED_BACK",),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )

    def test_a_refused_first_axis_leaves_the_baseline_confirmed_live(self) -> None:
        path = self.build(faults=FaultInjection(fail_axes={FIRST_AXIS}))

        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        self.assertEqual(dict(report.gateway_results)["COMMIT"], GatewayOutcome.REJECTED)
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        self.assertEqual(report.terminal_state, TrialState.SETTLED_NON_SUCCESS)
        self.assertEqual(self.store.uncertain_transactions(), ())

    def test_rollback_failure_ends_in_incident_lockdown(self) -> None:
        path = self.build(
            faults=FaultInjection(
                fail_axes={SECOND_AXIS}, fail_undo_axes={FIRST_AXIS}
            )
        )

        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        self.assertEqual(report.terminal_state, TrialState.INCIDENT_LOCKDOWN)
        self.assertEqual(self.resolutions(), ["INCIDENT_LOCKDOWN"])
        # Not settled: an unrecovered change keeps its reserve charged and its
        # lock held, because releasing either would say the deployment is
        # back to a known state when it is not.
        self.assertEqual(report.outcome, TrialOutcome.NOT_SETTLED)
        self.assertEqual(self.ledger(), [("RESERVE", 20.0)])
        self.assertEqual(
            self.kernel.reduced_state()["resourceLocks"],
            {"capability/steer": report.trial_id},
        )
        self.assertNotEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        assert_finite_terminal(self, path, CaseTermination.RECOVERY_FAILURE)
        self.assertEqual(
            snapshot(self, path, trial_id=report.trial_id),
            PreservationSnapshot(
                actual_outcome="NOT_SETTLED",
                harm_settlement=(("RESERVE", 20.0, False),),
                reserve_outstanding=20.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", "7"), ("servingCell", "cell-1")),
                locks=(("capability/steer", report.trial_id),),
                recovery=("INCIDENT_LOCKDOWN",),
                trial_terminal="INCIDENT_LOCKDOWN",
                case_terminal="RECOVERY_FAILURE",
            ),
        )

    def test_incident_lockdown_blocks_the_next_trial(self) -> None:
        path = self.build(
            faults=FaultInjection(
                fail_axes={SECOND_AXIS}, fail_undo_axes={FIRST_AXIS}
            )
        )
        path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        with self.assertRaises(KernelRefusal) as refusal:
            path.open_trial(self.kernel.current_catalog().candidates[1].candidate_id)

        self.assertEqual(refusal.exception.reason, "INCIDENT_LOCKDOWN")


class LostAcknowledgementTests(TransactionFaultFixture, unittest.TestCase):
    def test_a_lost_apply_ack_is_resolved_by_the_readback(self) -> None:
        """Design section 9: the readback decides, not the acknowledgement."""
        path = self.build(faults=FaultInjection(drop_ack_axes={SECOND_AXIS}))

        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        self.assertEqual(dict(report.gateway_results)["COMMIT"], GatewayOutcome.ACKED)
        commit_effects = [
            effect["detail"]
            for effect in self.gateway.transaction_record(
                f"tx:{report.trial_id}"
            ).effects.values()
            if effect["kind"] == "COMMIT"
        ]
        self.assertEqual(len(commit_effects), 1)
        self.assertIn("an acknowledgement was lost", commit_effects[0])
        self.assertEqual(report.outcome, TrialOutcome.SUCCESS)
        self.assertEqual(self.adapter.snapshot(), self.applied_config_for(0))
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

    def test_a_lost_ack_with_no_readback_stays_uncertain_until_it_is_resolved(
        self,
    ) -> None:
        """The one case that cannot be decided from the readback.

        The write lands, the acknowledgement is lost, and the reread that
        would have resolved it fails too.  ``UNKNOWN`` is the only honest
        answer; the transaction stays uncertain, and the resolution comes
        from the recovery sweep once the deployment can be read again.
        """
        path = self.build(max_trials=1)
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)
        path.ready(trial_id)
        path.commit_decision(trial_id)
        # Installed only now, so prepare and ready are unaffected.  One more
        # read succeeds -- the pre-apply configuration check -- and the
        # reconciling readback afterwards does not.
        self.adapter.set_faults(
            FaultInjection(drop_ack_axes={FIRST_AXIS}, unreadable_after_reads=1)
        )

        committed = path.commit(trial_id)

        self.assertIs(committed.outcome, GatewayOutcome.UNKNOWN)
        transaction_id = f"tx:{trial_id}"
        self.assertEqual(
            self.gateway.transaction_record(transaction_id).phase.value, "UNCERTAIN"
        )
        self.assertEqual(self.store.uncertain_transactions(), (transaction_id,))

        # The deployment becomes readable again; the sweep resolves it.
        self.adapter.set_faults(FaultInjection())
        self.assertEqual(self.kernel.recover(now=self.clock()), ())
        self.assertEqual(self.resolutions(), ["ROLLED_BACK"])
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        path.settle(trial_id, outcome=TrialOutcome.SAFETY_STOPPED)
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=trial_id),
            PreservationSnapshot(
                actual_outcome="SAFETY_STOPPED",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=("ROLLED_BACK",),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )

    def test_a_lost_finalize_ack_is_not_a_success(self) -> None:
        path = self.build(
            faults=FaultInjection(drop_finalize_ack=True), max_trials=1
        )

        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        kinds = dict(report.gateway_results)
        self.assertEqual(kinds["FINALIZE_LIVE"], GatewayOutcome.UNKNOWN)
        self.assertEqual(report.terminal_state, TrialState.SETTLED_NON_SUCCESS)
        # An unacknowledged finalize is filed conservatively: the Kernel does
        # not know what state the deployment settled in, and PARTIAL_APPLY is
        # the safety reason that says so.  It outranks the passing predicates,
        # which is the point.
        self.assertEqual(report.stop_reason, StopReason.PARTIAL_APPLY)
        self.assertEqual(report.outcome, TrialOutcome.SAFETY_STOPPED)
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        self.assertEqual(self.resolutions(), ["ROLLED_BACK"])
        self.assertFalse(
            self.kernel.reduced_state()["cases"][path.case_id].get("deployedSuccess")
        )
        assert_finite_terminal(self, path, CaseTermination.VECTORS_EXHAUSTED)
        self.assertEqual(
            snapshot(self, path, trial_id=report.trial_id),
            PreservationSnapshot(
                actual_outcome="SAFETY_STOPPED",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "CLOSED_PASS", (("VALID", "SUFFICIENT", "PASS", False),)),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=("ROLLED_BACK",),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="VECTORS_EXHAUSTED",
            ),
        )


class CrashRecoveryTests(TransactionFaultFixture, unittest.TestCase):
    def test_a_kernel_restart_after_the_commit_journal_resolves_the_transaction(
        self,
    ) -> None:
        path = self.build(
            faults=FaultInjection(crash_axes={SECOND_AXIS}), max_trials=1
        )
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)
        path.ready(trial_id)
        path.commit_decision(trial_id)

        # The process dies inside the apply: the gateway does not catch
        # BaseException, so nothing reconciles the transaction.
        with self.assertRaises(KeyboardInterrupt):
            path.commit(trial_id)

        transaction_id = f"tx:{trial_id}"
        self.assertEqual(
            self.gateway.transaction_record(transaction_id).phase.value, "APPLYING"
        )
        self.assertEqual(self.store.uncertain_transactions(), (transaction_id,))

        # Restart: a new Kernel over the same durable stream and the same
        # gateway journal.  Nothing is carried in process memory.
        restarted = AssuranceKernel(
            event_store=self.store,
            reducer=KernelReducer(),
            write_gateway=self.gateway,
            measurement_collector=None,
        )
        self.assertEqual(restarted.recover(now=self.clock()), ())

        self.assertEqual(self.resolutions(), ["ROLLED_BACK"])
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        self.assertEqual(
            restarted.reduced_state()["trials"][trial_id]["state"],
            TrialState.RECOVERY_VERIFYING.value,
        )
        restarted.advance_trial(
            trial_id, TrialState.SETTLEMENT, now=self.clock()
        )
        restarted.settle_trial(
            trial_id, outcome=TrialOutcome.SAFETY_STOPPED, now=self.clock()
        )
        self.assertIs(
            restarted.terminate_case(case_id=path.case_id, now=self.clock()),
            CaseTermination.EVIDENCE_INCOMPLETE,
        )

        events = list(self.store.iterate())
        agent_free_events = [
            event
            for event in events
            if event.event_kind not in {"AdvisoryAccepted", "AdvisoryRejected"}
        ]
        version = KernelReducer.reducer_version
        hashes = {
            self.kernel.terminal_state_hash(),
            restarted.terminal_state_hash(),
            terminal_state_hash(
                replay(KernelReducer(), events), reducer_version=version
            ),
            terminal_state_hash(
                replay(KernelReducer(), agent_free_events), reducer_version=version
            ),
        }
        self.assertEqual(len(hashes), 1)
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=trial_id),
            PreservationSnapshot(
                actual_outcome="SAFETY_STOPPED",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=("ROLLED_BACK",),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )

    def test_a_new_trial_is_blocked_until_recovery_completes(self) -> None:
        path = self.build(faults=FaultInjection(crash_axes={SECOND_AXIS}))
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)
        path.ready(trial_id)
        path.commit_decision(trial_id)
        with self.assertRaises(KeyboardInterrupt):
            path.commit(trial_id)

        with self.assertRaises(KernelRefusal) as refusal:
            path.open_trial(self.kernel.current_catalog().candidates[1].candidate_id)

        self.assertEqual(refusal.exception.reason, "RECOVERY_BLOCKED")


class HaltThatRestoresTheBaselineTests(TransactionFaultFixture, unittest.TestCase):
    """A halt that already put the baseline back must still recover.

    Live OTA 2026-09-12: the R1 policy adapter's HALT withdraws the policy, so
    the baseline is live again the moment the stop is acknowledged.  The gateway
    deliberately reports no observation for a halt, so the Kernel still believed
    the applied configuration was live and named it on the reverse-rollback
    permit; the gateway read the baseline instead and refused with
    ``REJECTED_CONFIG_MISMATCH`` -- "the permit names a configuration that is
    not live; reread first" -- and a fully recovered deployment was filed as
    ``INCIDENT_LOCKDOWN`` / ``RECOVERY_FAILURE``.  The mock adapter's HALT
    leaves the configuration alone, which is why no existing test saw it.
    """

    def setUp(self) -> None:
        self.path = self.build()
        self.trial_id = self.path.open_trial(self.path.request_proposal()[0])
        self.path.reserve_and_stage(self.trial_id)
        self.path.prepare(self.trial_id)
        self.path.ready(self.trial_id)
        self.path.commit_decision(self.trial_id)

    def test_a_halt_that_restored_the_baseline_still_verifies_recovery(self) -> None:
        self.path.commit(self.trial_id)
        self.path.enter_observation(self.trial_id)
        self.kernel.advance_trial(
            self.trial_id,
            TrialState.STOPPING,
            reason=StopReason.OPERATOR_ABORT,
            now=self.clock(),
        )
        # What the real adapter does on HALT, stated as the out-of-band change
        # it is: the deployment is back at the baseline before any reverse
        # rollback is issued.
        self.adapter.apply_drift(BASELINE_CONFIG)

        self.assertTrue(self.path.stop_and_rollback(self.trial_id))

        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        self.assertEqual(self.resolutions(), ["ROLLED_BACK"])


class FencingAndIdempotencyTests(TransactionFaultFixture, unittest.TestCase):
    def setUp(self) -> None:
        self.path = self.build()
        self.trial_id = self.path.open_trial(self.path.request_proposal()[0])
        self.path.reserve_and_stage(self.trial_id)
        self.path.prepare(self.trial_id)
        self.path.ready(self.trial_id)
        self.path.commit_decision(self.trial_id)

    def issued_tokens(self):
        return [
            KernelToken.from_canonical_dict(
                {
                    key: value
                    for key, value in envelope.payload.items()
                    if key != "resourceId"
                }
            )
            for envelope in self.store.iterate()
            if envelope.event_kind == "TokenIssued"
        ]

    def test_a_retransmitted_commit_produces_no_second_effect(self) -> None:
        first = self.path.commit(self.trial_id)
        self.assertIs(first.outcome, GatewayOutcome.ACKED)
        commit_token = self.issued_tokens()[-1]
        writes_before = len(self.adapter.writes)

        second = self.gateway.commit(token=commit_token)

        self.assertIs(second.outcome, GatewayOutcome.ALREADY_APPLIED)
        self.assertEqual(len(self.adapter.writes), writes_before)
        self.assertEqual(self.adapter.snapshot(), self.applied_config_for(0))
        self.path.enter_observation(self.trial_id)
        self.path.observe(ticks=4)
        report = self.path.conclude_trial(self.trial_id, cell_id=CELL_ID)
        assert_finite_terminal(self, self.path, CaseTermination.SUCCESS)
        self.assertEqual(
            snapshot(self, self.path, trial_id=self.trial_id),
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
        self.assertEqual(report.outcome, TrialOutcome.SUCCESS)

    def test_a_late_old_fence_commit_after_rollback_is_refused(self) -> None:
        self.path.commit(self.trial_id)
        stale = self.issued_tokens()[-1]
        self.path.enter_observation(self.trial_id)
        self.kernel.advance_trial(
            self.trial_id,
            TrialState.STOPPING,
            reason=StopReason.OPERATOR_ABORT,
            now=self.clock(),
        )
        self.assertTrue(self.path.stop_and_rollback(self.trial_id))
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        writes_before = len(self.adapter.writes)

        replayed = self.gateway.commit(token=stale)

        self.assertIs(replayed.outcome, GatewayOutcome.REJECTED_FENCE)
        self.assertEqual(len(self.adapter.writes), writes_before)
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        self.assertEqual(len(self.gateway.refusals()), 1)
        self.path.settle(self.trial_id, outcome=TrialOutcome.OPERATOR_ABORTED)
        assert_finite_terminal(self, self.path, CaseTermination.OPERATOR_ABORT)
        self.assertEqual(
            snapshot(self, self.path, trial_id=self.trial_id),
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

    def test_a_late_old_fence_finalize_after_rollback_is_refused(self) -> None:
        self.path.commit(self.trial_id)
        self.path.enter_observation(self.trial_id)
        self.path.observe(ticks=4)
        self.assertIs(
            self.path.decide(self.trial_id), TrialState.FINALIZING_LIVE
        )
        stale = self.kernel.issue_token(
            self.trial_id, token_kind=TokenKind.FINALIZE_LIVE, now=self.clock()
        )
        self.kernel.advance_trial(
            self.trial_id,
            TrialState.STOPPING,
            reason=StopReason.OPERATOR_ABORT,
            now=self.clock(),
        )
        self.assertTrue(self.path.stop_and_rollback(self.trial_id))
        writes_before = len(self.adapter.writes)

        replayed = self.gateway.finalize_live(token=stale)

        self.assertIs(replayed.outcome, GatewayOutcome.REJECTED_FENCE)
        self.assertEqual(len(self.adapter.writes), writes_before)
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        self.path.settle(self.trial_id, outcome=TrialOutcome.OPERATOR_ABORTED)
        assert_finite_terminal(self, self.path, CaseTermination.OPERATOR_ABORT)
        self.assertEqual(
            snapshot(self, self.path, trial_id=self.trial_id),
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

    def test_an_idempotency_key_reused_for_different_content_is_refused(self) -> None:
        self.path.commit(self.trial_id)
        commit_token = self.issued_tokens()[-1]
        from dataclasses import replace

        colliding = replace(
            commit_token,
            token_kind=TokenKind.STOP,
            command_sequence=commit_token.command_sequence + 1,
        )
        writes_before = len(self.adapter.writes)

        result = self.gateway.stop(token=colliding)

        self.assertIs(result.outcome, GatewayOutcome.REJECTED_IDEMPOTENCY_COLLISION)
        self.assertEqual(len(self.adapter.writes), writes_before)
        self.path.enter_observation(self.trial_id)
        self.path.observe(ticks=4)
        report = self.path.conclude_trial(self.trial_id, cell_id=CELL_ID)
        assert_finite_terminal(self, self.path, CaseTermination.SUCCESS)
        self.assertEqual(
            snapshot(self, self.path, trial_id=self.trial_id),
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
        self.assertEqual(report.outcome, TrialOutcome.SUCCESS)

    def test_the_kernel_refuses_a_result_carrying_an_old_fence(self) -> None:
        self.path.commit(self.trial_id)
        stale = self.issued_tokens()[-2]
        from assurance.gateway.write_gateway import GatewayResult

        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.record_gateway_result(
                stale,
                GatewayResult(GatewayOutcome.ACKED),
                resource_id="capability/steer",
                now=self.clock(),
            )

        self.assertEqual(refusal.exception.reason, "OLD_FENCE")
        rejected = [
            envelope.payload["reason"]
            for envelope in self.store.iterate()
            if envelope.event_kind == "GatewayResultRejected"
        ]
        self.assertEqual(rejected, ["OLD_FENCE"])

    def test_the_kernel_refuses_a_result_for_a_permit_it_never_issued(self) -> None:
        from dataclasses import replace

        from assurance.gateway.write_gateway import GatewayResult

        forged = replace(self.issued_tokens()[-1], fencing_token=99)

        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.record_gateway_result(
                forged,
                GatewayResult(GatewayOutcome.ACKED),
                resource_id="capability/steer",
                now=self.clock(),
            )

        self.assertEqual(refusal.exception.reason, "TOKEN_NOT_ISSUED")


class LeaseExpiryTests(TransactionFaultFixture, unittest.TestCase):
    def test_an_expired_lease_drives_the_deployment_to_its_safe_state(self) -> None:
        path = self.build()
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)
        path.ready(trial_id)
        path.commit_decision(trial_id)
        path.commit(trial_id)
        path.enter_observation(trial_id)

        # The Kernel goes away; the permit's lease runs out with the change
        # outstanding.  The watchdog may only halt and drive to the contracted
        # safe state -- it cannot finalize and it cannot settle.
        self.clock.advance(600_000)  # past any priced COMMIT lease (apply read-back included, 2026-09-24)
        fired = self.gateway.watchdog_check()

        self.assertEqual([result.outcome for result in fired], [GatewayOutcome.ACKED])
        self.assertEqual(self.adapter.snapshot(), dict(SAFE_STATE))
        self.assertEqual(self.adapter.live_hash(), config_hash(SAFE_STATE))
        self.assertEqual(
            self.gateway.transaction_record(f"tx:{trial_id}").phase.value, "SAFE_STATE"
        )
        self.assertFalse(
            self.kernel.reduced_state()["trials"][trial_id].get("finalizeAcknowledged")
        )

    def test_a_result_recorded_after_the_lease_stops_the_trial(self) -> None:
        path = self.build()
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)
        path.ready(trial_id)
        path.commit_decision(trial_id)
        token = self.kernel.issue_token(
            trial_id, token_kind=TokenKind.COMMIT, now=self.clock()
        )
        result = self.gateway.commit(token=token)
        self.clock.advance(600_000)  # past any priced COMMIT lease

        from assurance.kernel.kernel import KernelRefusal as Refusal

        with self.assertRaises(Refusal) as refusal:
            self.kernel.record_gateway_result(
                token, result, resource_id="capability/steer", now=self.clock()
            )

        self.assertEqual(refusal.exception.reason, "LEASE_EXPIRED")
        trial = self.kernel.reduced_state()["trials"][trial_id]
        self.assertEqual(trial["state"], TrialState.STOPPING.value)
        self.assertEqual(trial["stopReason"], StopReason.LEASE_EXPIRY.value)


class WatchdogHostingTests(TransactionFaultFixture, unittest.TestCase):
    """Judgement 2: who arms the contract watchdog, and on what evidence.

    ``docs/architecture/SEAMS-GATE2.md`` section 8.3.  An adapter declares
    whether the deployment behind it can host a contract watchdog.  Where it
    cannot -- the A1 policy path, and therefore Gate 3's PIN_TO_CELL run --
    the Kernel arms the watchdog on the two mechanisms it owns end to end
    instead, after checking that both are actually available.
    """

    def readiness(self):
        return [
            envelope.payload
            for envelope in self.store.iterate()
            if envelope.event_kind == "TrialCommitReadinessRecorded"
        ]

    def test_a_non_hosting_adapter_is_armed_by_the_kernel(self) -> None:
        path = self.build(adapter_hosts_watchdogs=False)

        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        # The deployment was never asked to arm anything, and the trial still
        # reached a guarded commit and a finalized success.
        self.assertEqual(self.adapter.armed_watchdogs, [])
        self.assertEqual(report.terminal_state, TrialState.SETTLED_SUCCESS)
        recorded = self.readiness()
        self.assertEqual(len(recorded), 1)
        self.assertEqual(
            recorded[0]["kernelHostedGuards"],
            ["measurement-staleness", "token-lease-deadline"],
        )
        self.assertEqual(
            recorded[0]["kernelHostedWatchdogIds"],
            [contract_set()["harm"].watchdogs[0].watchdog_id],
        )

    def test_a_hosting_adapter_arms_at_the_deployment(self) -> None:
        path = self.build()

        path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        armed = contract_set()["harm"].watchdogs[0].watchdog_id
        self.assertEqual(self.adapter.armed_watchdogs, [armed])
        recorded = self.readiness()
        self.assertEqual(recorded[0]["kernelHostedGuards"], [])
        self.assertEqual(recorded[0]["kernelHostedWatchdogIds"], [])

    def test_the_gateway_states_where_the_watchdogs_are_hosted(self) -> None:
        for hosting, expected in ((True, "adapter"), (False, "kernel")):
            with self.subTest(hosting=hosting):
                path = self.build(adapter_hosts_watchdogs=hosting)
                trial_id = path.open_trial(path.request_proposal()[0])
                path.reserve_and_stage(trial_id)
                path.prepare(trial_id)

                result = path.ready(trial_id)

                self.assertIn(
                    f"watchdog-hosting:mock:{expected}", result.evidence_refs
                )

    def test_silence_is_not_a_declaration(self) -> None:
        """Missing arming with no hosting statement is refused.

        The Kernel arms its own guards only where the gateway said the adapter
        cannot host.  A READY that simply carries no arming evidence -- a
        gateway that forgot, a fabricated result -- is not that statement.
        """
        path = self.build(adapter_hosts_watchdogs=False)
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)
        token = self.kernel.issue_token(
            trial_id, token_kind=TokenKind.READY, now=self.clock()
        )
        from assurance.gateway.write_gateway import GatewayResult

        silent = GatewayResult(
            GatewayOutcome.ACKED,
            observed_config_hash=self.adapter.live_hash(),
            evidence_refs=(),
        )
        self.kernel.record_gateway_result(
            token, silent, resource_id="capability/steer", now=self.clock()
        )
        self.kernel.advance_trial(trial_id, TrialState.READY, now=self.clock())

        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.record_commit_readiness(
                trial_id,
                watchdogs_armed=True,
                baseline_hash=self.adapter.live_hash(),
                now=self.clock(),
            )

        self.assertEqual(refusal.exception.reason, "WATCHDOG_ARMING_NOT_OBSERVED")
        self.assertEqual(self.adapter.writes, [])

    def test_the_kernel_refuses_to_arm_without_a_live_lease(self) -> None:
        """The lease deadline is a guard only while the lease is ahead of now."""
        path = self.build(adapter_hosts_watchdogs=False)
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)
        path.ready(trial_id)
        self.kernel.advance_trial(trial_id, TrialState.READY, now=self.clock())
        baseline = self.kernel.reduced_state()["trials"][trial_id][
            "readyBaselineHash"
        ]
        # Past any READY lease: since 2026-09-24 it is priced per participant
        # read (at most (steps + 1) x 20.5 s), no longer a flat 30 s.
        self.clock.advance(600_000)

        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.record_commit_readiness(
                trial_id,
                watchdogs_armed=True,
                baseline_hash=baseline,
                now=self.clock(),
            )

        self.assertEqual(refusal.exception.reason, "KERNEL_WATCHDOG_NO_LIVE_LEASE")

    def test_the_kernel_hosted_guards_are_the_ones_that_actually_fire(self) -> None:
        """Both named mechanisms stop a trial on a non-hosting deployment.

        ``measurement-staleness`` through the evaluator's sufficiency axis, and
        ``token-lease-deadline`` through the permit lease.  Naming them in the
        arming record would be decoration if they did not.
        """
        from tests.assurance.vertical_support import failing_collector

        stale = self.build(
            adapter_hosts_watchdogs=False,
            collector=failing_collector(
                "clock-drift", start="2026-08-21T09:00:00.000000Z"
            ),
        )
        report = stale.run_trial(stale.request_proposal()[0], cell_id=CELL_ID)
        self.assertEqual(report.outcome, TrialOutcome.INDETERMINATE)
        self.assertNotEqual(report.terminal_state, TrialState.SETTLED_SUCCESS)

        lease = self.build(adapter_hosts_watchdogs=False)
        trial_id = lease.open_trial(lease.request_proposal()[0])
        lease.reserve_and_stage(trial_id)
        lease.prepare(trial_id)
        lease.ready(trial_id)
        lease.commit_decision(trial_id)
        token = self.kernel.issue_token(
            trial_id, token_kind=TokenKind.COMMIT, now=self.clock()
        )
        result = self.gateway.commit(token=token)
        self.clock.advance(600_000)  # past any priced COMMIT lease
        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.record_gateway_result(
                token, result, resource_id="capability/steer", now=self.clock()
            )
        self.assertEqual(refusal.exception.reason, "LEASE_EXPIRED")
        self.assertEqual(
            self.kernel.reduced_state()["trials"][trial_id]["stopReason"],
            StopReason.LEASE_EXPIRY.value,
        )


if __name__ == "__main__":
    unittest.main()
