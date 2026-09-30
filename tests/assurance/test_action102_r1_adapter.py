"""The ``r1-cap`` adapter's durable transaction-to-policy binding.

The steering adapter's bindings lived in a dictionary: a restart erased them,
and a policy created just before the crash had no name anybody could still
address.  A SUPPLEMENTARY control makes that worse, because it is created
*after* a PRIMARY one is already live, so an orphan on this side leaves a
composition nobody can unwind.

Four claims, and each is a failure the journal exists to prevent:

* the returned policy id is persisted **before** the adapter acknowledges;
* a restart recovers the binding and does not create a second policy;
* an A1 DELETE alone does **not** release the binding or the scope -- a DELETE
  response is not recovery;
* a stale, mismatched or absent readback is ``UNKNOWN``, never a clean
  rejection and never a success.

Hermetic: no socket, no radio, no subprocess, no model.
"""

from __future__ import annotations

import unittest

from assurance.gateway.commands import GatewayOperation, build_command
from assurance.gateway.plan import config_hash
from assurance.gateway.r1_adapter import R1Adapter
from assurance.gateway.r1_binding_journal import (
    BindingState, InMemoryR1BindingJournal,
)
from assurance.gateway.write_gateway import GatewayOutcome

from tests.assurance.action102_support import (
    APPLIED_CAP, CAP, CAP_FAMILY, CONTROLLED_UE, CapFaults, UNCAPPED,
    applied_config, baseline_config, build_cap_harness, controlled_scope_builder,
    permit, plan_scope,
)


def _command(operation, index=1, value=APPLIED_CAP, token=None):
    return build_command(
        token or permit("COMMIT", config_hash(baseline_config()), 2),
        operation, scope=plan_scope(), axis=CAP.axis, value=value, index=index)


class DurableBindingTests(unittest.TestCase):

    def setUp(self):
        self.harness = build_cap_harness()
        self.base = config_hash(baseline_config())
        self.applied = config_hash(applied_config())

    def _through_commit(self):
        self.harness.gateway.prepare(
            token=permit("PREPARE", self.base, 0), plan=self.harness.plan())
        self.harness.gateway.ready(token=permit("READY", self.base, 1))
        return self.harness.gateway.commit(token=permit("COMMIT", self.base, 2))

    def test_the_scope_is_reserved_at_prepare_with_nothing_created(self):
        result = self.harness.gateway.prepare(
            token=permit("PREPARE", self.base, 0), plan=self.harness.plan())
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        record = self.harness.cap_binding()
        self.assertIsNotNone(record)
        self.assertIs(record.state, BindingState.RESERVED)
        self.assertIsNone(record.policy_id)
        evidence = self.harness.assert_diagnostic_agrees(self)
        self.assertEqual(evidence.writes_sent, 0)
        self.assertEqual(evidence.controls_reaching_ran, 0)
        self.assertEqual(self.harness.producer.list_policies(
            CAP_FAMILY.policy_type_id), [])

    def test_the_scope_key_is_the_controlled_ue_not_the_objective_ue(self):
        self._through_commit()
        record = self.harness.cap_binding()
        self.assertIn(f"ueId={CONTROLLED_UE['ueId']}", record.scope_key)
        self.assertNotIn("ueId=131", record.scope_key)

    def test_the_policy_id_is_persisted_before_the_acknowledgement(self):
        result = self._through_commit()
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        record = self.harness.cap_binding()
        self.assertIs(record.state, BindingState.BOUND)
        self.assertEqual(record.policy_id,
                         self.harness.adapter.bound_policy("tx-cap"))
        evidence = self.harness.assert_diagnostic_agrees(self)
        self.assertEqual(evidence.applies_sent, 1)
        self.assertEqual(evidence.controls_reaching_ran, 1)

    def test_a_restart_recovers_the_binding_and_creates_no_second_policy(self):
        self._through_commit()
        snapshot = self.harness.binding_journal.snapshot()
        policy_id = self.harness.cap_binding().policy_id

        # A new process, the same durable journal.
        restarted = build_cap_harness(
            binding_journal=InMemoryR1BindingJournal(snapshot))
        self.assertEqual(restarted.adapter.bindings().get("tx-cap"), policy_id)
        self.assertEqual(restarted.adapter.recover_bindings(), ("tx-cap",))
        # The recovered process issued nothing of its own.
        recovered = restarted.assert_diagnostic_agrees(self)
        self.assertEqual(recovered.writes_sent, 0)

    def test_a_second_transaction_cannot_take_a_held_scope(self):
        self._through_commit()
        other = self.harness.adapter.dispatch(
            token=permit("COMMIT", self.base, 2, transaction_id="tx-other",
                         trial_id="trial-other"),
            command=_command(
                GatewayOperation.APPLY,
                token=permit("COMMIT", self.base, 2, transaction_id="tx-other",
                             trial_id="trial-other")))
        self.assertIs(other.outcome, GatewayOutcome.REJECTED)
        self.assertIn("still owned by transaction", other.detail)
        # The refusal is a decision by the adapter, before any port call: one
        # apply stands and the second transaction issued nothing.
        self.assertEqual(self.harness.adapter.write_counts("tx-other"),
                         {"applies": 0, "withdrawals": 0, "refused": 0, "unknown": 0})
        evidence = self.harness.assert_diagnostic_agrees(self)
        self.assertEqual(evidence.applies_sent, 1)


class WithdrawalTests(unittest.TestCase):

    def setUp(self):
        self.harness = build_cap_harness()
        self.base = config_hash(baseline_config())
        self.applied = config_hash(applied_config())
        self.harness.gateway.prepare(
            token=permit("PREPARE", self.base, 0), plan=self.harness.plan())
        self.harness.gateway.ready(token=permit("READY", self.base, 1))
        self.harness.gateway.commit(token=permit("COMMIT", self.base, 2))

    def test_a_delete_alone_does_not_release_the_binding_or_the_scope(self):
        harness = build_cap_harness(faults=CapFaults(suppress_restore=True))
        harness.gateway.prepare(token=permit("PREPARE", self.base, 0),
                                plan=harness.plan())
        harness.gateway.ready(token=permit("READY", self.base, 1))
        harness.gateway.commit(token=permit("COMMIT", self.base, 2))
        result = harness.gateway.reverse_rollback(
            token=permit("REVERSE_ROLLBACK", self.applied, 3))
        # The DELETE was acknowledged and the restore never reached the
        # scheduler: the honest answer is not "reversed".
        self.assertIsNot(result.outcome, GatewayOutcome.ACKED)
        record = harness.cap_binding()
        self.assertIs(record.state, BindingState.RESTORE_PENDING)
        self.assertTrue(record.holds_scope)
        self.assertIsNone(
            harness.binding_journal.scope_owner(
                CAP_FAMILY.policy_type_id, "no-such-scope"))
        self.assertIs(
            harness.binding_journal.scope_owner(
                CAP_FAMILY.policy_type_id, record.scope_key), record)

    def test_the_binding_is_released_only_when_the_baseline_is_read_back(self):
        result = self.harness.gateway.reverse_rollback(
            token=permit("REVERSE_ROLLBACK", self.applied, 3))
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        record = self.harness.cap_binding()
        self.assertIs(record.state, BindingState.RESTORED)
        self.assertFalse(record.holds_scope)
        self.assertEqual(self.harness.live_cap(), int(UNCAPPED))
        evidence = self.harness.assert_diagnostic_agrees(self)
        self.assertEqual(evidence.withdrawals_sent, 1)
        # Apply then restore: two controls reached the RAN, no more.
        self.assertEqual(evidence.controls_reaching_ran, 2)

    def test_the_restore_is_read_through_the_independent_counter(self):
        # While a withdrawal is outstanding there is no policy left to ask, so
        # the observation must come from the configuration counter alone.
        self.harness.gateway.reverse_rollback(
            token=permit("REVERSE_ROLLBACK", self.applied, 3))
        self.assertEqual(
            self.harness.assert_diagnostic_agrees(self).withdrawals_sent, 1)
        self.assertEqual(self.harness.cap_binding().baseline_config,
                         {CAP.axis: UNCAPPED})


class UnreadableConfigurationTests(unittest.TestCase):

    def setUp(self):
        self.base = config_hash(baseline_config())

    def test_an_absent_configuration_counter_stages_nothing(self):
        harness = build_cap_harness()
        harness.kpm._samples.clear()  # the counter is not in the stream
        result = harness.gateway.prepare(
            token=permit("PREPARE", self.base, 0), plan=harness.plan())
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertEqual(
            harness.assert_diagnostic_agrees(self).writes_sent, 0)
        self.assertIsNone(harness.transaction_journal.read("tx-cap"))

    def test_a_lost_acknowledgement_with_no_readback_is_unknown(self):
        harness = build_cap_harness(faults=CapFaults(suppress_counter=True))
        harness.gateway.prepare(token=permit("PREPARE", self.base, 0),
                                plan=harness.plan())
        harness.gateway.ready(token=permit("READY", self.base, 1))
        result = harness.gateway.commit(token=permit("COMMIT", self.base, 2))
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        # A write may have landed.  Nothing here says it did not: the adapter
        # issued one apply and the producer recorded a control that reached the
        # RAN without a corroborated readback.
        evidence = harness.assert_diagnostic_agrees(self)
        self.assertEqual(evidence.applies_sent, 1)
        self.assertEqual(evidence.controls_reaching_ran, 1)
        self.assertEqual(evidence.verified, 0)
        record = harness.transaction_journal.read("tx-cap")
        self.assertTrue(record.is_uncertain)

    def test_a_mismatched_readback_is_never_reported_as_success(self):
        harness = build_cap_harness(faults=CapFaults(applied_offset=3))
        harness.gateway.prepare(token=permit("PREPARE", self.base, 0),
                                plan=harness.plan())
        harness.gateway.ready(token=permit("READY", self.base, 1))
        result = harness.gateway.commit(token=permit("COMMIT", self.base, 2))
        self.assertNotIn(result.outcome,
                         (GatewayOutcome.ACKED, GatewayOutcome.REJECTED))
        self.assertEqual(harness.live_cap(), int(APPLIED_CAP) + 3)


class TheDurableRevisionContract(unittest.TestCase):
    """WP-B7 / E5 finding 3: one revision, in the body and in the record.

    The Campaign 5 builder numbers the A1 policy revision independently of the
    Kernel fence, and it keeps only an in-process draft cache.  The durable
    binding journal is therefore the revision authority: the adapter seeds the
    builder from it on every draft and persists back exactly the
    ``trace.revision`` the body it sent carried.

    What that buys is the property asserted below: a rebuilt process continues
    the numbering where the last one left off.  Without the seed a restart
    starts again at revision one, and a producer that has already stored three
    refuses the update as non-monotonic -- the case then fails for a reason
    that has nothing to do with the radio.
    """

    def setUp(self):
        self.harness = build_cap_harness()
        self.base = config_hash(baseline_config())
        self.applied = config_hash(applied_config())

    def _prepare(self, harness=None):
        harness = harness or self.harness
        return harness.gateway.prepare(
            token=permit("PREPARE", self.base, 0), plan=harness.plan())

    def _commit(self, harness=None, fence=2, sequence=2):
        harness = harness or self.harness
        return harness.gateway.commit(
            token=permit("COMMIT", self.base, sequence, fence=fence))

    def _sent_revision(self, harness=None):
        """The revision in the body the producer actually stored."""
        harness = harness or self.harness
        policy_id = harness.adapter.bound_policy("tx-cap")
        body = harness.producer.get_policy(CAP_FAMILY.policy_type_id, policy_id)
        return body["trace"]["revision"]

    def test_the_first_body_carries_revision_one_at_fence_zero(self):
        harness = build_cap_harness()
        harness.gateway.prepare(token=permit("PREPARE", self.base, 0, fence=0),
                                plan=harness.plan())
        harness.gateway.ready(token=permit("READY", self.base, 1, fence=0))
        result = harness.gateway.commit(
            token=permit("COMMIT", self.base, 2, fence=0))
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        # The A1 revision starts at one; the Kernel fence starts at zero, and
        # neither is derived from the other.
        self.assertEqual(self._sent_revision(harness), 1)
        self.assertEqual(harness.cap_binding().policy_revision, 1)
        policy_id = harness.adapter.bound_policy("tx-cap")
        body = harness.producer.get_policy(CAP_FAMILY.policy_type_id, policy_id)
        self.assertEqual(body["trace"]["fencingToken"], 0)

    def test_the_record_holds_exactly_the_revision_that_was_sent(self):
        self._prepare()
        self.harness.gateway.ready(token=permit("READY", self.base, 1))
        self._commit()
        self.assertEqual(self.harness.cap_binding().policy_revision,
                         self._sent_revision())

    def test_a_same_fence_replay_reuses_the_revision(self):
        # PREPARE and COMMIT draft the same policy at the same fence.  That is
        # one revision, not two: an idempotent retry must not advance a number
        # the producer fences on.
        self._prepare()
        self.harness.gateway.ready(token=permit("READY", self.base, 1))
        self._commit()
        self.assertEqual(self._sent_revision(), 1)
        self.assertEqual(self.harness.cap_binding().policy_revision, 1)

    def test_a_higher_fence_advances_the_revision_by_one(self):
        self._prepare()
        self.harness.gateway.ready(token=permit("READY", self.base, 1))
        self._commit()
        first = self.harness.cap_binding().policy_revision

        # A watchdog re-fenced the resource; the same transaction re-applies.
        higher = permit("COMMIT", self.base, 3, fence=5)
        self.harness.adapter.dispatch(
            token=higher,
            command=_command(GatewayOperation.APPLY, value="9", token=higher))
        self.assertEqual(self._sent_revision(), first + 1)
        self.assertEqual(self.harness.cap_binding().policy_revision, first + 1)

    def test_a_rebuilt_process_continues_where_the_durable_record_left_off(self):
        """The property the seed exists for."""
        self._prepare()
        self.harness.gateway.ready(token=permit("READY", self.base, 1))
        self._commit()
        higher = permit("COMMIT", self.base, 3, fence=5)
        self.harness.adapter.dispatch(
            token=higher,
            command=_command(GatewayOperation.APPLY, value="9", token=higher))
        durable = self.harness.binding_journal.snapshot()
        self.assertEqual(self.harness.cap_binding().policy_revision, 2)

        # A new process: a fresh adapter, a fresh builder with no memory at
        # all, and the journal the old one left behind.  The producer is
        # carried over because the A1-P side did not restart with us.
        restarted = build_cap_harness(
            binding_journal=InMemoryR1BindingJournal(durable),
            policy_builder=controlled_scope_builder(),
            port=self.harness.port)
        again = permit("COMMIT", self.base, 4, fence=9)
        restarted.adapter.dispatch(
            token=again,
            command=_command(GatewayOperation.APPLY, value="7", token=again))
        # Not one.  Three.
        self.assertEqual(restarted.cap_binding().policy_revision, 3)
        self.assertEqual(self._sent_revision(restarted), 3)

    def test_an_older_fence_is_refused_rather_than_renumbered(self):
        self._prepare()
        self.harness.gateway.ready(token=permit("READY", self.base, 1))
        higher = permit("COMMIT", self.base, 3, fence=5)
        self.harness.adapter.dispatch(
            token=higher,
            command=_command(GatewayOperation.APPLY, value="9", token=higher))
        revision = self.harness.cap_binding().policy_revision

        stale = permit("COMMIT", self.base, 4, fence=1)
        result = self.harness.adapter.dispatch(
            token=stale,
            command=_command(GatewayOperation.APPLY, value="8", token=stale))
        self.assertIs(result.outcome, GatewayOutcome.REJECTED)
        self.assertEqual(self.harness.cap_binding().policy_revision, revision)

    def test_a_body_without_trace_revision_is_refused_with_no_write(self):
        """A record cannot be honest about a number the body does not carry."""

        def unnumbered(command, *, last_revision=None):
            body = controlled_scope_builder()(command,
                                              last_revision=last_revision)
            body["trace"].pop("revision")
            return body

        harness = build_cap_harness(policy_builder=unnumbered)
        result = harness.gateway.prepare(
            token=permit("PREPARE", self.base, 0), plan=harness.plan())
        self.assertIs(result.outcome, GatewayOutcome.REJECTED)
        self.assertIn("trace.revision", result.detail)
        self.assertEqual(harness.adapter.write_counts("tx-cap"),
                         {"applies": 0, "withdrawals": 0,
                          "refused": 0, "unknown": 0})
        self.assertIsNone(harness.cap_binding())
        self.assertEqual(
            harness.producer.list_policies(CAP_FAMILY.policy_type_id), [])

    def test_a_journal_less_adapter_keeps_the_builders_own_numbering(self):
        """No durable record, no seed -- and no false claim of one.

        Passing zero would assert that no revision has ever been issued, and
        the builder would then refuse its own in-process history at the next
        higher fence.  A journal-less adapter is exactly as restart-safe as its
        in-memory bindings are, and says so by not seeding.
        """
        adapter = R1Adapter(
            policy_port=self.harness.port,
            policy_builder=controlled_scope_builder(),
            near_rt_ric_id="near-rt-ric-hermetic",
            policy_type_id=CAP_FAMILY.policy_type_id,
            readback_port=lambda **kwargs: None,
            name="r1-cap-no-journal",
        )
        first = permit("COMMIT", self.base, 2, transaction_id="tx-nj", fence=0)
        adapter.dispatch(token=first,
                         command=_command(GatewayOperation.APPLY, token=first))
        higher = permit("COMMIT", self.base, 3, transaction_id="tx-nj", fence=4)
        adapter.dispatch(
            token=higher,
            command=_command(GatewayOperation.APPLY, value="9", token=higher))
        body = self.harness.producer.get_policy(
            CAP_FAMILY.policy_type_id, adapter.bound_policy("tx-nj"))
        self.assertEqual(body["trace"]["revision"], 2)

    def test_the_fencing_token_is_never_read_as_the_revision(self):
        # The two are separate axes and the fence's valid first value is zero,
        # which is not a valid A1 revision.  Reading one for the other is the
        # defect this whole contract exists to prevent.
        harness = build_cap_harness()
        harness.gateway.prepare(token=permit("PREPARE", self.base, 0, fence=0),
                                plan=harness.plan())
        record = harness.cap_binding()
        self.assertEqual(record.policy_revision, 1)
        self.assertNotEqual(record.policy_revision, 0)


if __name__ == "__main__":
    unittest.main()
