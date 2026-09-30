"""An unacknowledged DELETE cannot be repaired by another cell's equal scalar.

The Gateway and R1 adapter are real. The radio, policy port and current-cell
readback are injected; there is no network, process launch or equipment here.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assurance.gateway.commands import GatewayOperation, build_command
from assurance.gateway.plan import config_hash
from assurance.gateway.r1_adapter import R1Adapter
from assurance.gateway.r1_binding_journal import (
    BindingState, InMemoryR1BindingJournal, JsonFileR1BindingJournal, R1BindingRecord,
)
from assurance.gateway.r1_operation_journal import InMemoryR1OperationJournal
from assurance.gateway.write_gateway import GatewayOutcome
from tests.assurance.action102_support import (
    APPLIED_CAP, CAP, CAP_FAMILY, CONTROLLED_UE, HOME_NCI, STEERING_AXIS,
    TARGET_NCI, UNCAPPED, baseline_config, build_cap_harness, permit, plan_scope,
)


class CellPolicyPort:
    def __init__(self):
        self.caps = {HOME_NCI: UNCAPPED, TARGET_NCI: UNCAPPED}
        self.policies = {}
        self.delete_calls = []
        self.delete_error = None
        self.next_policy_id = 1

    def get_policy_type(self, policy_type):
        return {}

    def create_policy(self, ric, policy_type, body):
        policy_id = 'policy-' + str(self.next_policy_id)
        self.next_policy_id += 1
        self.policies[policy_id] = dict(body)
        config = body['config']
        self.caps[config['cellId']] = str(config['cap'])
        return {'policyId': policy_id}

    def update_policy(self, policy_id, body):
        self.policies[policy_id] = dict(body)
        self.caps[body['config']['cellId']] = str(body['config']['cap'])

    def get_policy_status(self, policy_id):
        return {'observed': {CAP.axis: self.policies[policy_id]['config']['cap']},
                'enforceStatus': 'ENFORCED', 'aicStatus': {'episodeTerminal': True}}

    def delete_policy(self, policy_id):
        self.delete_calls.append(policy_id)
        if self.delete_error is not None:
            raise self.delete_error
        policy = self.policies.pop(policy_id)
        self.caps[policy['config']['cellId']] = UNCAPPED
        # The production R1 port returns None only after HTTP 204. Campaign-5
        # issues that response after the owning worker verified its baseline.


class WithdrawalObligationTests(unittest.TestCase):
    def setUp(self):
        self.harness = build_cap_harness()
        self.port = CellPolicyPort()
        self.base = config_hash(baseline_config())
        self._configure(self.harness.adapter)
        steering_dispatch = self.harness.steering.dispatch

        def withdraw_steering(*, token, command):
            result = steering_dispatch(token=token, command=command)
            if command['operation'] == GatewayOperation.HALT.value:
                # The live R1 steering HALT withdraws its policy; model that
                # association restoration, not the mock's stop-only HALT.
                self.harness.steering.apply_drift({STEERING_AXIS: HOME_NCI})
            return result

        self.harness.steering.dispatch = withdraw_steering
        self.harness.gateway.prepare(token=permit('PREPARE', self.base, 0),
                                     plan=self.harness.plan())
        self.harness.gateway.ready(token=permit('READY', self.base, 1))
        result = self.harness.gateway.commit(token=permit('COMMIT', self.base, 2))
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.policy_id = self.harness.adapter.bound_policy('tx-cap')
        self.assertEqual(TARGET_NCI, self.port.policies[self.policy_id]['config']['cellId'])
        self.assertIn('cellId=' + TARGET_NCI, self.harness.cap_binding().scope_key)
        self.assertFalse(self.harness.cap_binding().withdrawal_acknowledged)

    def _configure(self, adapter):
        def builder(command):
            return {'config': {'cellId': self.harness.steering.snapshot()[STEERING_AXIS],
                               'ueId': CONTROLLED_UE['ueId'],
                               'cap': str(command['value'])},
                    'trace': {'revision': 1}}

        def current_cell_readback(**kwargs):
            cell = self.harness.steering.snapshot()[STEERING_AXIS]
            return {CAP.axis: self.port.caps[cell]}

        adapter._port = self.port
        adapter._build = builder
        adapter._builder_takes_last_revision = False
        adapter._readback = current_cell_readback
        adapter._project = lambda status: status.get('observed')

    def _dispatch(self, operation, *, adapter=None, kind='CONFIGURATION_REREAD', sequence=4):
        adapter = adapter or self.harness.adapter
        token = permit(kind, self.base, sequence)
        kwargs = {'axis': CAP.axis, 'value': UNCAPPED} if operation is GatewayOperation.UNDO else {}
        return adapter.dispatch(token=token, command=build_command(
            token, operation, scope=plan_scope(), index=sequence, **kwargs))

    def _restart(self, snapshot=None):
        journal = InMemoryR1BindingJournal(
            self.harness.binding_journal.snapshot() if snapshot is None else snapshot)
        adapter = R1Adapter(
            policy_port=self.port, policy_builder=lambda command: {},
            near_rt_ric_id='near-rt-ric-hermetic', policy_type_id=CAP_FAMILY.policy_type_id,
            readback_port=lambda **kwargs: None,
            binding_journal=journal,
            scope_key=lambda body: '/'.join(
                f"{key}={body['config'][key]}" for key in CAP_FAMILY.scope_fields),
            retain_binding_until_restore=True)
        self._configure(adapter)
        return adapter, journal

    def test_failed_target_delete_cannot_restore_from_source_cell_scalar(self):
        self.port.delete_error = RuntimeError('injected transport failure')
        stopped = self.harness.gateway.stop(token=permit('STOP', self.base, 3))
        self.assertIs(stopped.outcome, GatewayOutcome.UNKNOWN)
        self.assertEqual(HOME_NCI, self.harness.steering.snapshot()[STEERING_AXIS])
        self.assertEqual(APPLIED_CAP, self.port.caps[TARGET_NCI])
        read = self._dispatch(GatewayOperation.READ)
        self.assertIs(read.outcome, GatewayOutcome.UNKNOWN)
        self.assertIsNone(read.observed_config_hash)
        self.assertIs(self.harness.cap_binding().state, BindingState.RESTORE_PENDING)
        self.assertEqual(self.policy_id, self.harness.adapter.bound_policy('tx-cap'))
        self.assertIn(self.policy_id, self.port.policies)

    def test_whole_gateway_cannot_ack_recovery_while_delete_is_unconfirmed(self):
        self.port.delete_error = RuntimeError('injected transport failure')
        self.harness.gateway.stop(token=permit('STOP', self.base, 3))
        reversed_result = self.harness.gateway.reverse_rollback(
            token=permit('REVERSE_ROLLBACK', self.base, 4))
        self.assertIs(reversed_result.outcome, GatewayOutcome.UNKNOWN)
        reread = self.harness.gateway.reread_configuration(
            token=permit('CONFIGURATION_REREAD', self.base, 5))
        self.assertIs(reread.outcome, GatewayOutcome.UNKNOWN)
        confirmed = self.harness.gateway.confirm_recovery(
            token=permit('RECOVERY_CONFIRM', self.base, 6))
        self.assertIsNot(confirmed.outcome, GatewayOutcome.ACKED)
        self.assertIs(self.harness.cap_binding().state, BindingState.RESTORE_PENDING)
        self.assertEqual([self.policy_id], self.port.delete_calls)

    def test_new_apply_cannot_overwrite_an_unconfirmed_delete_obligation(self):
        self.port.delete_error = RuntimeError('injected transport failure')
        self._dispatch(GatewayOperation.HALT, kind='STOP', sequence=3)
        original = self.harness.cap_binding()
        for operation in (GatewayOperation.VALIDATE, GatewayOperation.APPLY):
            with self.subTest(operation=operation):
                kind = 'PREPARE' if operation is GatewayOperation.VALIDATE else 'COMMIT'
                token = permit(kind, self.base, 8, fence=5)
                result = self.harness.adapter.dispatch(token=token, command=build_command(
                    token, operation, scope=plan_scope(), axis=CAP.axis,
                    value='9', index=8))
                self.assertIs(result.outcome, GatewayOutcome.REJECTED)
                self.assertEqual(original, self.harness.cap_binding())
                self.assertEqual(APPLIED_CAP, self.port.caps[TARGET_NCI])

    def test_repeat_undo_does_not_ack_or_blindly_retry_an_unknown_delete(self):
        self.port.delete_error = RuntimeError('injected transport failure')
        self._dispatch(GatewayOperation.HALT, kind='STOP', sequence=3)
        result = self._dispatch(GatewayOperation.UNDO, kind='REVERSE_ROLLBACK')
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertEqual([self.policy_id], self.port.delete_calls)
        self.assertIn(self.policy_id, self.port.policies)

    def test_refused_delete_remains_pending_after_restart(self):
        self.port.delete_error = ValueError('injected producer refusal')
        result = self._dispatch(GatewayOperation.HALT, kind='STOP', sequence=3)
        self.assertIs(result.outcome, GatewayOutcome.REJECTED)
        adapter, journal = self._restart()
        self.harness.steering.apply_drift({STEERING_AXIS: HOME_NCI})
        read = self._dispatch(GatewayOperation.READ, adapter=adapter)
        self.assertIs(read.outcome, GatewayOutcome.UNKNOWN)
        self.assertIs(journal.binding_for('tx-cap').state, BindingState.RESTORE_PENDING)
        self.assertEqual(self.policy_id, adapter.bound_policy('tx-cap'))

    def test_missing_ack_field_in_legacy_record_is_not_inferred_from_scalar(self):
        self._dispatch(GatewayOperation.HALT, kind='STOP', sequence=3)
        snapshot = self.harness.binding_journal.snapshot()
        snapshot['tx-cap'].pop('withdrawalAcknowledged', None)
        adapter, journal = self._restart(snapshot)
        result = self._dispatch(GatewayOperation.READ, adapter=adapter)
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertIs(journal.binding_for('tx-cap').state, BindingState.RESTORE_PENDING)

    def test_acknowledged_delete_survives_restart_without_operation_history(self):
        self._dispatch(GatewayOperation.HALT, kind='STOP', sequence=3)
        self.assertTrue(self.harness.cap_binding().withdrawal_acknowledged)
        adapter, journal = self._restart()
        adapter._operations = InMemoryR1OperationJournal()
        result = self._dispatch(GatewayOperation.READ, adapter=adapter)
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertIs(journal.binding_for('tx-cap').state, BindingState.RESTORED)
        self.assertIsNone(adapter.bound_policy('tx-cap'))
        self.assertEqual({}, self.port.policies)

    def test_crash_before_ack_is_durable_cannot_be_repaired_by_a_scalar(self):
        journal = self.harness.binding_journal
        write = journal.write

        def crash_at_ack(record):
            if record.withdrawal_acknowledged:
                raise SystemExit('injected crash before ACK persistence')
            return write(record)

        with patch.object(journal, 'write', side_effect=crash_at_ack):
            with self.assertRaises(SystemExit):
                self._dispatch(GatewayOperation.HALT, kind='STOP', sequence=3)
        self.assertEqual({}, self.port.policies)
        adapter, restarted = self._restart()
        result = self._dispatch(GatewayOperation.READ, adapter=adapter)
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertFalse(restarted.binding_for('tx-cap').withdrawal_acknowledged)

    def test_ack_is_not_enough_without_the_matching_baseline_readback(self):
        self._dispatch(GatewayOperation.HALT, kind='STOP', sequence=3)
        self.port.caps[TARGET_NCI] = APPLIED_CAP
        result = self._dispatch(GatewayOperation.READ)
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertIs(self.harness.cap_binding().state, BindingState.RESTORE_PENDING)
        self.assertEqual(self.policy_id, self.harness.adapter.bound_policy('tx-cap'))

    def test_apply_rebind_resets_prior_withdrawal_acknowledgment(self):
        self._dispatch(GatewayOperation.HALT, kind='STOP', sequence=3)
        self._dispatch(GatewayOperation.READ)
        self.assertIs(self.harness.cap_binding().state, BindingState.RESTORED)
        token = permit('COMMIT', self.base, 7, fence=5)
        create = self.port.create_policy

        def inspect_new_write(*args):
            self.assertFalse(self.harness.cap_binding().withdrawal_acknowledged)
            return create(*args)

        with patch.object(self.port, 'create_policy', side_effect=inspect_new_write):
            result = self.harness.adapter.dispatch(token=token, command=build_command(
                token, GatewayOperation.APPLY, scope=plan_scope(), axis=CAP.axis,
                value=APPLIED_CAP, index=7))
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        record = self.harness.cap_binding()
        self.assertIs(record.state, BindingState.BOUND)
        self.assertNotEqual(self.policy_id, record.policy_id)
        self.assertFalse(record.withdrawal_acknowledged)

    def test_failed_ack_file_replace_does_not_publish_ack_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bindings.json'
            journal = JsonFileR1BindingJournal(path)
            pending = self.harness.cap_binding().evolve(
                state=BindingState.RESTORE_PENDING, withdrawal_acknowledged=False)
            journal.write(pending)
            with patch('assurance.gateway.r1_binding_journal.os.replace',
                       side_effect=OSError('injected atomic replacement failure')):
                with self.assertRaises(OSError):
                    journal.write(pending.evolve(withdrawal_acknowledged=True))
            self.assertFalse(journal.binding_for('tx-cap').withdrawal_acknowledged)
            self.assertFalse(JsonFileR1BindingJournal(path).binding_for('tx-cap')
                             .withdrawal_acknowledged)

    def test_ack_round_trips_through_the_file_journal(self):
        self._dispatch(GatewayOperation.HALT, kind='STOP', sequence=3)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bindings.json'
            journal = JsonFileR1BindingJournal(path)
            journal.write(self.harness.cap_binding())
            restored = JsonFileR1BindingJournal(path).binding_for('tx-cap')
            self.assertTrue(restored.withdrawal_acknowledged)
            self.assertEqual(self.policy_id, restored.policy_id)
            self.assertEqual(self.harness.cap_binding().scope_key, restored.scope_key)
            self.assertEqual(self.harness.cap_binding().baseline_config, restored.baseline_config)

    def test_ack_serialization_requires_a_real_boolean(self):
        record = self.harness.cap_binding().to_canonical_dict()
        record['withdrawalAcknowledged'] = 'true'
        with self.assertRaises(TypeError):
            R1BindingRecord.from_canonical_dict(record)


class RetainedConstructionTests(unittest.TestCase):
    def adapter(self, port, *, retained=False):
        return R1Adapter(
            policy_port=port,
            policy_builder=lambda command: {'config': {
                'cellId': TARGET_NCI, 'cap': str(command['value'])}},
            near_rt_ric_id='hermetic-ric', policy_type_id='hermetic-cap',
            readback_port=lambda **kwargs: {CAP.axis: port.caps[TARGET_NCI]},
            retain_binding_until_restore=retained)

    def test_retained_mode_requires_a_binding_journal(self):
        with self.assertRaisesRegex(ValueError, 'binding journal'):
            self.adapter(CellPolicyPort(), retained=True)

    def test_non_retaining_journal_less_mode_remains_available(self):
        port = CellPolicyPort()
        adapter = self.adapter(port)
        token = permit('COMMIT', config_hash(baseline_config()), 1)
        applied = adapter.dispatch(token=token, command=build_command(
            token, GatewayOperation.APPLY, scope=plan_scope(), axis=CAP.axis,
            value=APPLIED_CAP, index=1))
        self.assertIs(applied.outcome, GatewayOutcome.ACKED)
        self.assertIsNotNone(adapter.bound_policy('tx-cap'))
        stop = permit('STOP', config_hash(baseline_config()), 2)
        halted = adapter.dispatch(token=stop, command=build_command(
            stop, GatewayOperation.HALT, scope=plan_scope(), index=2))
        self.assertIs(halted.outcome, GatewayOutcome.ACKED)
        self.assertIsNone(adapter.bound_policy('tx-cap'))
        self.assertEqual((), adapter.recover_bindings())
        self.assertEqual(UNCAPPED, port.caps[TARGET_NCI])
