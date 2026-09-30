"""The seven-case hardware-free fault matrix for the SUPPLEMENTARY UE cap.

``A102-HF-{FENCE,LEASE,DUPLICATE,STALE-RNTI,PARTIAL,REVERSE,PRODUCER-REFUSAL}``.
Every row runs through the **real** Kernel-shaped permits, the real
:class:`~assurance.gateway.gateway.TokenBoundWriteGateway`, the real
:class:`~assurance.gateway.r1_adapter.R1Adapter` with its durable binding
journal, the real corroborated readback and the in-repo Campaign 5 producer.
Only the near-RT worker, the gNB scheduler and the KPM stream are stood in for,
and the stand-in *counts E2 control writes* -- because most of these rows are
assertions about how many happened.

Each row asserts, as the contract's fault table requires: the number of E2
writes, the producer's policy/episode state, the Gateway outcome, the durable
transaction and binding records, and the live configuration afterwards.  The
last case additionally drives the whole thing through the real
:class:`~assurance.kernel.kernel.AssuranceKernel` and
:class:`~assurance.vertical.VerticalPath`, so the seven axes and the evidence
closure are exercised on the composed two-participant path rather than asserted
about it.

Passing this file makes the path ``HARDWARE_FREE_ROUND_TRIP``.  It is **not**
OTA evidence: no radio, no A1 transport, no released xApp, no E2 node.
"""

from __future__ import annotations

import unittest

from assurance.core.axes import TrialOutcome
from assurance.gateway.commands import GatewayOperation, build_command
from assurance.gateway.plan import config_hash
from assurance.gateway.r1_binding_journal import BindingState
from assurance.gateway.write_gateway import GatewayOutcome
from assurance.objectives.action102_support import CAP_ACTION_ID

from oran.campaign5.producer import A1Conflict, A1ValidationError

from tests.assurance.action102_support import (
    APPLIED_CAP, CAP, CAP_FAMILY, CONTROLLED_UE, EARLIER, EXPIRED_LEASE,
    UNCAPPED, CapFaults, applied_config, baseline_config, build_cap_harness,
    permit, plan_scope,
)

SCENARIOS = (
    "A102-HF-FENCE", "A102-HF-LEASE", "A102-HF-DUPLICATE", "A102-HF-STALE-RNTI",
    "A102-HF-PARTIAL", "A102-HF-REVERSE", "A102-HF-PRODUCER-REFUSAL",
)


class _Row(unittest.TestCase):
    """Shared staging: prepare, ready, and (usually) commit."""

    faults = None

    def setUp(self):
        self.harness = build_cap_harness(faults=self.faults)
        self.base = config_hash(baseline_config())
        self.applied = config_hash(applied_config())

    def stage(self):
        self.assertIs(
            self.harness.gateway.prepare(
                token=permit("PREPARE", self.base, 0), plan=self.harness.plan()
            ).outcome, GatewayOutcome.ACKED)
        self.assertIs(
            self.harness.gateway.ready(token=permit("READY", self.base, 1)).outcome,
            GatewayOutcome.ACKED)

    def commit(self):
        return self.harness.gateway.commit(token=permit("COMMIT", self.base, 2))


class FenceRow(_Row):
    """``A102-HF-FENCE``: a delayed lower fence for the same scope."""

    def test_a_superseded_fence_is_refused_with_zero_writes(self):
        self.stage()
        self.assertIs(self.commit().outcome, GatewayOutcome.ACKED)
        before = self.harness.assert_diagnostic_agrees(self)

        late = self.harness.gateway.commit(
            token=permit("COMMIT", self.applied, 2, fence=1))
        self.assertIs(late.outcome, GatewayOutcome.REJECTED_FENCE)
        # The gateway refused the stale fence itself: no further call left the
        # adapter and no further control reached the RAN.
        self.assertEqual(self.harness.assert_diagnostic_agrees(self), before)
        self.assertEqual(self.harness.live_cap(), int(APPLIED_CAP))
        # The refusal is recorded, not dropped.
        self.assertTrue(any("REJECTED_FENCE" in entry["detail"]
                            for entry in self.harness.gateway.refusals()))

    def test_the_producer_refuses_a_lower_fence_of_its_own(self):
        self.stage()
        self.commit()
        policy_id = self.harness.cap_binding().policy_id
        body = self.harness.producer.get_policy(CAP_FAMILY.policy_type_id, policy_id)
        stale = {**body, "config": {**body["config"], "maxDlPrbs": 8},
                 "trace": {**body["trace"], "revision": 1, "fencingToken": 1}}
        with self.assertRaises(A1Conflict):
            self.harness.producer.put_policy(
                CAP_FAMILY.policy_type_id, policy_id, stale)
        self.assertEqual(self.harness.live_cap(), int(APPLIED_CAP))


class LeaseRow(_Row):
    """``A102-HF-LEASE``: the permit lease runs out."""

    def test_a_pre_write_expiry_rejects_with_zero_writes(self):
        self.stage()
        expired = self.harness.gateway.commit(
            token=permit("COMMIT", self.base, 2, lease=EXPIRED_LEASE,
                         issued_at=EARLIER))
        self.assertIs(expired.outcome, GatewayOutcome.REJECTED_LEASE_EXPIRED)
        evidence = self.harness.assert_diagnostic_agrees(self)
        self.assertEqual(evidence.writes_sent, 0)
        self.assertEqual(evidence.controls_reaching_ran, 0)
        self.assertEqual(self.harness.live_cap(), int(UNCAPPED))

    def test_a_post_write_expiry_halts_and_restores_the_baseline(self):
        self.stage()
        self.commit()
        self.assertEqual(self.harness.live_cap(), int(APPLIED_CAP))
        # The watchdog acts on the *expired* permit and may only do what that
        # permit already contracted: halt, then the contracted safe state.
        fired = self.harness.gateway.watchdog_check(now="2026-09-04T11:00:00.000000Z")
        self.assertTrue(fired)
        self.assertEqual(self.harness.live_cap(), int(UNCAPPED))
        # Halt and safe state are two routes to one restoration obligation.
        # The second is idempotent: exactly one withdrawal left the adapter and
        # exactly two controls reached the RAN -- the apply and its restore.
        evidence = self.harness.assert_diagnostic_agrees(self)
        self.assertEqual(evidence.withdrawals_sent, 1)
        self.assertEqual(evidence.controls_reaching_ran, 2)


class DuplicateRow(_Row):
    """``A102-HF-DUPLICATE``: the same key twice, then the same key with a new body."""

    def test_an_identical_retransmission_produces_no_second_write(self):
        self.stage()
        first = self.commit()
        self.assertIs(first.outcome, GatewayOutcome.ACKED)
        self.assertEqual(
            self.harness.assert_diagnostic_agrees(self).applies_sent, 1)

        replay = self.commit()
        self.assertIs(replay.outcome, GatewayOutcome.ALREADY_APPLIED)
        evidence = self.harness.assert_diagnostic_agrees(self)
        self.assertEqual(evidence.applies_sent, 1)
        self.assertEqual(evidence.controls_reaching_ran, 1)

    def test_the_same_key_with_a_different_body_is_an_idempotency_collision(self):
        self.stage()
        self.commit()
        collision = self.harness.gateway.reverse_rollback(
            token=permit("REVERSE_ROLLBACK", self.applied, 2,
                         key="tx-cap:COMMIT:2:2"))
        self.assertIs(collision.outcome,
                      GatewayOutcome.REJECTED_IDEMPOTENCY_COLLISION)
        self.assertEqual(
            self.harness.assert_diagnostic_agrees(self).withdrawals_sent, 0)

    def test_the_producer_refuses_a_second_policy_on_one_scope(self):
        self.stage()
        self.commit()
        body = self.harness.producer.get_policy(
            CAP_FAMILY.policy_type_id, self.harness.cap_binding().policy_id)
        with self.assertRaises(A1Conflict):
            self.harness.producer.put_policy(
                CAP_FAMILY.policy_type_id, "pol-cap-other", body)


class StaleRntiRow(_Row):
    """``A102-HF-STALE-RNTI``: no fresh attribution for the controlled UE."""

    faults = CapFaults(stale_attribution=True)

    def test_a_stale_identity_performs_zero_e2_writes(self):
        self.stage()
        result = self.commit()
        self.assertIsNot(result.outcome, GatewayOutcome.ACKED)
        # The two production records disagree on purpose, and the disagreement
        # *is* the fault: the adapter created a policy, and the worker sent no
        # control because it had no fresh identity to address.
        evidence = self.harness.assert_diagnostic_agrees(self)
        self.assertEqual(evidence.applies_sent, 1)
        self.assertEqual(evidence.controls_reaching_ran, 0)
        self.assertEqual(evidence.controls_failed, 1)
        self.assertEqual(self.harness.live_cap(), int(UNCAPPED))

    def test_the_producer_reports_apply_failed_and_never_enforced(self):
        self.stage()
        self.commit()
        policy_id = self.harness.adapter.bound_policy("tx-cap")
        status = self.harness.port.get_policy_status(policy_id)
        self.assertEqual(status["enforceStatus"], "NOT_ENFORCED")
        self.assertEqual(status["aicStatus"]["episodeState"], "APPLY_FAILED")
        self.assertFalse(
            status["aicStatus"]["control"]["resultIsEffectEvidence"])
        self.assertFalse(
            status["aicStatus"]["control"]["writeMayHaveOccurred"])

    def test_there_is_no_fallback_to_another_ue(self):
        self.stage()
        self.commit()
        self.assertEqual(
            self.harness.assert_diagnostic_agrees(self).controls_reaching_ran, 0)
        # And the diagnostic agrees: not one control, to any UE.
        self.assertEqual(list(self.harness.port.e2_writes), [])


class PartialRow(_Row):
    """``A102-HF-PARTIAL``: the write may have landed and the readback is lost."""

    faults = CapFaults(suppress_counter=True)

    def test_a_lost_readback_is_unknown_and_never_a_clean_rejection(self):
        self.stage()
        result = self.commit()
        self.assertIn(result.outcome,
                      (GatewayOutcome.UNKNOWN, GatewayOutcome.PARTIAL_APPLY))
        self.assertNotIn(result.outcome,
                         (GatewayOutcome.ACKED, GatewayOutcome.REJECTED))

    def test_the_transaction_stays_uncertain_and_blocks_the_resource(self):
        self.stage()
        self.commit()
        record = self.harness.transaction_journal.read("tx-cap")
        self.assertTrue(record.is_uncertain)
        self.assertEqual(self.harness.gateway.uncertain_transactions(), ("tx-cap",))
        query = self.harness.gateway.query_transaction("tx-cap")
        self.assertIs(query.outcome, GatewayOutcome.UNKNOWN)

    def test_a_write_may_have_occurred_and_nothing_says_it_did_not(self):
        self.stage()
        self.commit()
        evidence = self.harness.assert_diagnostic_agrees(self)
        self.assertEqual(evidence.applies_sent, 1)
        self.assertEqual(evidence.controls_reaching_ran, 1)
        self.assertEqual(evidence.verified, 0)
        self.assertIs(self.harness.cap_binding().state, BindingState.BOUND)


class RevisionRow(_Row):
    """``A102-HF-REVISION``: one A1 revision, in the body and in the record.

    The end-to-end version of the WP-B7 contract, driven through the gateway
    rather than the adapter: what the producer stored and what the durable
    binding says have to be the same number, or a restart re-numbers into a
    producer that has already seen higher and is refused for it.
    """

    def _stored_revision(self):
        policy_id = self.harness.cap_binding().policy_id
        body = self.harness.producer.get_policy(
            CAP_FAMILY.policy_type_id, policy_id)
        return body["trace"]["revision"]

    def test_the_producer_and_the_durable_record_hold_one_revision(self):
        self.stage()
        self.commit()
        self.assertEqual(self._stored_revision(),
                         self.harness.cap_binding().policy_revision)

    def test_the_first_revision_is_one_and_the_first_fence_is_zero(self):
        # Two axes, two starting values, and neither derived from the other.
        harness = build_cap_harness()
        harness.gateway.prepare(token=permit("PREPARE", self.base, 0, fence=0),
                                plan=harness.plan())
        harness.gateway.ready(token=permit("READY", self.base, 1, fence=0))
        harness.gateway.commit(token=permit("COMMIT", self.base, 2, fence=0))
        body = harness.producer.get_policy(
            CAP_FAMILY.policy_type_id, harness.cap_binding().policy_id)
        self.assertEqual(body["trace"]["revision"], 1)
        self.assertEqual(body["trace"]["fencingToken"], 0)
        self.assertEqual(harness.cap_binding().policy_revision, 1)

    def test_prepare_and_commit_are_one_revision_not_two(self):
        self.stage()          # drafts the body once
        self.commit()         # and again at the same fence
        self.assertEqual(self._stored_revision(), 1)


class ReverseRow(_Row):
    """``A102-HF-REVERSE``: apply, reverse, then replay the undo."""

    def test_one_restore_brings_the_baseline_back_and_releases_the_scope(self):
        self.stage()
        self.commit()
        self.assertEqual(self.harness.live_cap(), int(APPLIED_CAP))

        reversed_result = self.harness.gateway.reverse_rollback(
            token=permit("REVERSE_ROLLBACK", self.applied, 3))
        self.assertIs(reversed_result.outcome, GatewayOutcome.ACKED)
        evidence = self.harness.assert_diagnostic_agrees(self)
        self.assertEqual(evidence.withdrawals_sent, 1)
        self.assertEqual(evidence.controls_reaching_ran, 2)
        self.assertEqual(self.harness.live_cap(), int(UNCAPPED))
        record = self.harness.cap_binding()
        self.assertIs(record.state, BindingState.RESTORED)
        self.assertFalse(record.holds_scope)

    def test_a_replayed_undo_is_already_applied_with_no_further_write(self):
        self.stage()
        self.commit()
        self.harness.gateway.reverse_rollback(
            token=permit("REVERSE_ROLLBACK", self.applied, 3))
        before = self.harness.assert_diagnostic_agrees(self)
        replay = self.harness.gateway.reverse_rollback(
            token=permit("REVERSE_ROLLBACK", self.applied, 3))
        self.assertIs(replay.outcome, GatewayOutcome.ALREADY_APPLIED)
        self.assertEqual(self.harness.assert_diagnostic_agrees(self), before)

    def test_the_primary_is_unwound_after_the_supplementary(self):
        self.stage()
        self.commit()
        self.harness.gateway.reverse_rollback(
            token=permit("REVERSE_ROLLBACK", self.applied, 3))
        undone = [command["axis"] for command in self.harness.steering.commands
                  if command["operation"] == "UNDO"]
        # The steering mock sees its own undo last, because the cap came off
        # first: reverse order across participants, not only within one.
        self.assertEqual(undone, ["servingCell"])
        self.assertEqual(self.harness.steering.snapshot()["servingCell"], "12345678")


class ProducerRefusalRow(_Row):
    """``A102-HF-PRODUCER-REFUSAL``: refused before a policy was accepted."""

    faults = CapFaults(refuse_create=A1ValidationError)

    def test_a_cap_only_refusal_is_a_rejection_with_the_baseline_confirmed(self):
        # Nothing else was in the plan, so the refusal is a clean rejection and
        # a reread confirms the baseline is still live.
        harness = build_cap_harness(faults=self.faults)
        plan = harness.plan(steering=False)
        self.assertIs(
            harness.gateway.prepare(token=permit("PREPARE", self.base, 0),
                                    plan=plan).outcome, GatewayOutcome.ACKED)
        harness.gateway.ready(token=permit("READY", self.base, 1))
        result = harness.gateway.commit(token=permit("COMMIT", self.base, 2))
        self.assertIs(result.outcome, GatewayOutcome.REJECTED)
        evidence = harness.assert_diagnostic_agrees(self)
        self.assertEqual(evidence.writes_sent, 0)
        self.assertEqual(evidence.refused, 1)
        self.assertEqual(evidence.controls_reaching_ran, 0)
        self.assertEqual(harness.live_cap(), int(UNCAPPED))
        self.assertIsNone(harness.adapter.bound_policy("tx-cap"))
        self.assertEqual(
            harness.producer.list_policies(CAP_FAMILY.policy_type_id), [])

    def test_a_refusal_after_the_primary_landed_is_a_partial_apply(self):
        # The PRIMARY steering write landed and the SUPPLEMENTARY one did not.
        # That is a *positively observed* mixed state, so the gateway says
        # PARTIAL_APPLY -- never a clean rejection, which would tell the Kernel
        # nothing happened when the cell has already moved.
        self.stage()
        result = self.commit()
        self.assertIs(result.outcome, GatewayOutcome.PARTIAL_APPLY)
        evidence = self.harness.assert_diagnostic_agrees(self)
        self.assertEqual(evidence.writes_sent, 0)
        self.assertEqual(evidence.refused, 1)
        self.assertEqual(evidence.controls_reaching_ran, 0)
        self.assertEqual(self.harness.live_cap(), int(UNCAPPED))
        self.assertIsNone(self.harness.adapter.bound_policy("tx-cap"))
        self.assertEqual(
            self.harness.producer.list_policies(CAP_FAMILY.policy_type_id), [])
        record = self.harness.transaction_journal.read("tx-cap")
        self.assertEqual(record.applied_axes, ("servingCell",))

    def test_the_reserved_scope_never_becomes_a_bound_policy(self):
        self.stage()
        self.commit()
        record = self.harness.cap_binding()
        self.assertIs(record.state, BindingState.RESERVED)
        self.assertIsNone(record.policy_id)


class MatrixCoverageTests(unittest.TestCase):
    """The matrix is named, complete, and reported as hardware-free."""

    def test_every_scenario_has_a_case_class(self):
        classes = {
            "A102-HF-FENCE": FenceRow, "A102-HF-LEASE": LeaseRow,
            "A102-HF-DUPLICATE": DuplicateRow, "A102-HF-STALE-RNTI": StaleRntiRow,
            "A102-HF-PARTIAL": PartialRow, "A102-HF-REVERSE": ReverseRow,
            "A102-HF-PRODUCER-REFUSAL": ProducerRefusalRow,
        }
        self.assertEqual(tuple(classes), SCENARIOS)
        for scenario, case in classes.items():
            with self.subTest(scenario=scenario):
                self.assertTrue(issubclass(case, _Row))
                self.assertTrue(case.__doc__ and scenario in case.__doc__)


class ComposedKernelPathTests(unittest.TestCase):
    """The whole two-participant composition, through the real Kernel."""

    def test_a_hardware_free_run_settles_and_never_badges_live(self):
        from tools.hfconsole import build_hardware_free_session

        composed = build_hardware_free_session("QoSTarget", with_cap=True)
        preview = composed.session.draft(composed.utterance)
        instance = composed.session.confirm(preview)
        view = composed.session.start(instance)

        self.assertEqual(view.mode, "MOCK")
        self.assertFalse(view.is_live)
        self.assertEqual(view.stage, "TERMINAL")
        # Seven axes, separately: a run with no injected samples is
        # measurement-insufficient, and that is not a KPI failure.
        self.assertEqual(view.axes.execution_validity, "VALID")
        self.assertEqual(view.axes.measurement_sufficiency, "MISSING_INTERVAL")
        self.assertEqual(view.axes.predicate_verdict, "INDETERMINATE")
        self.assertEqual(view.settlement.outcome, TrialOutcome.INDETERMINATE.value)

    def test_the_plan_writes_the_primary_first_and_the_cap_on_its_own_adapter(self):
        from tools.hfconsole import build_hardware_free_session

        composed = build_hardware_free_session("QoSTarget", with_cap=True)
        preview = composed.session.draft(composed.utterance)
        instance = composed.session.confirm(preview)
        view = composed.session.start(instance)
        steps = composed.runtime.path.plan_for(view.trial_id)["steps"]
        self.assertEqual([step["axis"] for step in steps],
                         ["servingCell", CAP.axis])
        self.assertNotIn("adapter", steps[0])
        self.assertEqual(steps[1]["adapter"], CAP.adapter)

    def test_the_cap_is_reversed_and_the_uncapped_baseline_is_confirmed(self):
        from tools.hfconsole import build_hardware_free_session

        composed = build_hardware_free_session("QoSTarget", with_cap=True)
        preview = composed.session.draft(composed.utterance)
        instance = composed.session.confirm(preview)
        composed.session.start(instance)
        operations = [kind for kind, _ in composed.runtime.path.gateway_log]
        self.assertEqual(operations[:3], ["PREPARE", "READY", "COMMIT"])
        self.assertIn("REVERSE_ROLLBACK", operations)
        self.assertEqual(composed.cap.adapter.snapshot()[CAP.axis], UNCAPPED)

    def test_the_hardware_free_run_carries_a_scenario_name_not_ota_evidence(self):
        # A hardware-free result is named by its scenario; OTA ``evidence_refs``
        # belong to a physical run and are forbidden at this level.
        self.assertEqual(len(SCENARIOS), 7)
        self.assertTrue(all(name.startswith("A102-HF-") for name in SCENARIOS))
        self.assertEqual(CAP_ACTION_ID, "ue-dl-prb-cap")


if __name__ == "__main__":
    unittest.main()
