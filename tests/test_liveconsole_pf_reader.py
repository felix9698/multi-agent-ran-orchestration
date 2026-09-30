"""Y2 injected KPM evidence only; no radio, socket, or model calls."""
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from assurance.collector.o1col import KpmJsonlAdapter
from assurance.objectives.action102_support import (
    CAP_ACTION_ID, PRIORITY_ACTION_ID, supplementary_action,
)
from oran.campaign5.families import CAMPAIGN5_FAMILIES
from oran.campaign5.readback import CorroboratedConfigReadback
from tools.hfconsole.agent_env import (
    EmulatedClock, EmulatedKpiObserver, EmulatedKpmStream, EmulatedPolicyPort,
    EmulatedRan, HermeticDeployment, POLICY_TYPE_ID,
)
from tools.liveconsole.agent import AgentRequest, build_agent_sitting
from tools.liveconsole.build import (
    KpmCapConfigReader, controlled_scope_builder, joint_serving_attribution,
)
from tools.liveconsole.profile import LiveConsoleError
from tests.test_liveconsole_action102 import (
    EXPECTED, E2_NODE, EPOCH, OTHER_E2_NODE, OTHER_EPOCH, CONTROLLED_UE,
    OBJECTIVE_UE, _Tail, _indication,
)

PF = CAMPAIGN5_FAMILIES['priority']


def indication(value=0.5, **kwargs):
    record = json.loads(_indication(value=0, **kwargs))
    record['ues'][0]['measurements'] = [
        {'name': PF.readback_counter, 'type': 'real', 'value': value}]
    return json.dumps(record)


class PfReaderTests(unittest.TestCase):
    def test_joint_handover_uses_target_counter_and_target_policy_scope(self):
        target = type(EXPECTED)(EXPECTED.amf_ue_ngap_id, 87654321,
                                OTHER_E2_NODE, OTHER_EPOCH)
        reader = self.reader([indication(0.5), indication(2.0, node=OTHER_E2_NODE,
                                                       epoch=OTHER_EPOCH)])
        reader._attribution_provider = lambda: target
        self.assertEqual({'pfWeight': 2.0}, reader.read(PF.readback_counter, self.scope()))
        self.assertEqual(target.to_record(), reader.reads[-1]['expected'])
        build = controlled_scope_builder(
            PF, supplementary_action(PRIORITY_ACTION_ID),
            controlled_scope_key='controlledUe@132', attribution_provider=lambda: target,
            validity_provider=lambda _: {'notBefore': '2026-09-04T00:00:00Z',
                                         'notAfter': '2026-09-05T00:00:00Z'})
        body = build({'operation': 'APPLY', 'transactionId': 'tx', 'trialId': 'trial',
                      'fencingToken': 2, 'commandSequence': 1, 'commandIndex': 1,
                      'idempotencyKey': 'tx:APPLY:2:1', 'axis': 'pfWeight@132',
                      'value': '2.0', 'scope': self.scope()})
        self.assertEqual('87654321', body['config']['cellId'])
        self.assertEqual('132', body['config']['ueId'])

    def test_joint_handover_never_accepts_source_counter_or_wrong_target_epoch(self):
        target = type(EXPECTED)(EXPECTED.amf_ue_ngap_id, 87654321,
                                OTHER_E2_NODE, OTHER_EPOCH)
        for lines in ([indication()], [indication(node=OTHER_E2_NODE, epoch=OTHER_EPOCH + 1)]):
            reader = self.reader(lines)
            reader._attribution_provider = lambda: target
            self.assertIsNone(reader.read(PF.readback_counter, self.scope()))
        for expected in (None, type(EXPECTED)(999, 87654321, OTHER_E2_NODE, OTHER_EPOCH)):
            reader = self.reader([indication()])
            reader._attribution_provider = lambda: expected
            self.assertIsNone(reader.read(PF.readback_counter, self.scope()))

    def test_joint_resolver_refuses_missing_or_outside_catalog_attribution(self):
        observed = SimpleNamespace(amf_ue_ngap_id=132, serving_nci=87654321,
                                   e2_node=OTHER_E2_NODE, connection_epoch=OTHER_EPOCH)
        source = SimpleNamespace(refresh=lambda: None, at_or_before=lambda *a, **k: observed)
        resolve = joint_serving_attribution(reader=source, ue_id='132',
                    allowed_cells=(12345678, 87654321), now=lambda: 'now', freshness_bound_ms=5000)
        self.assertEqual(OTHER_EPOCH, resolve().connection_epoch)
        observed.serving_nci = 999
        self.assertIsNone(resolve())
        observed = None
        self.assertIsNone(resolve())

    def reader(self, lines, now='2025-09-04T15:33:20.000000Z', expected=EXPECTED):
        return KpmCapConfigReader(
            _Tail(lines), KpmJsonlAdapter(expected_epochs={
                E2_NODE: EPOCH, OTHER_E2_NODE: OTHER_EPOCH}),
            counter_name=PF.readback_counter, readback_leaf='pfWeight',
            now=lambda: now, freshness_bound_ms=5000, expected=expected,
            controlled_scope_key='controlledUe@132')

    def scope(self):
        return {**OBJECTIVE_UE, 'controlledUe@132': dict(CONTROLLED_UE)}

    def test_fractional_weight_is_not_truncated_and_counter_is_exact(self):
        reader = self.reader([indication()])
        self.assertIsNone(reader.read('RAN.UE.DlPrbCap', self.scope()))
        self.assertEqual({'pfWeight': 0.5}, reader.read(PF.readback_counter, self.scope()))
        self.assertEqual(0.5, reader.reads[-1]['value'])

    def test_entitlement_absence_and_freshness_match_cap(self):
        cases = [
            ([], {}, 'COUNTER_ABSENT'),
            ([_indication(value=12)], {}, 'COUNTER_ABSENT'),
            ([indication(ue=999)], {}, 'COUNTER_ABSENT'),
            ([indication(node=OTHER_E2_NODE, epoch=OTHER_EPOCH)], {}, 'ATTRIBUTION_MISMATCH'),
            ([indication(node=OTHER_E2_NODE, epoch=OTHER_EPOCH)],
             {'expected': type(EXPECTED)(EXPECTED.amf_ue_ngap_id, EXPECTED.serving_nci,
                                        OTHER_E2_NODE, EPOCH)}, 'ATTRIBUTION_MISMATCH'),
            ([indication()], {'now': '2025-09-04T15:33:25.001000Z'}, 'STALE'),
            ([indication()], {'now': '2025-09-04T15:33:14.999000Z'}, 'STALE'),
        ]
        for lines, kwargs, outcome in cases:
            with self.subTest(outcome=outcome, kwargs=kwargs):
                reader = self.reader(lines, **kwargs)
                self.assertIsNone(reader.read(PF.readback_counter, self.scope()))
                self.assertEqual(outcome, reader.reads[-1]['outcome'])
        reader = self.reader([indication()])
        self.assertIsNone(reader.read(PF.readback_counter, {'controlledUe@132': OBJECTIVE_UE}))
        self.assertEqual('SCOPE_MISMATCH', reader.reads[-1]['outcome'])

    def test_wrong_identity_cannot_displace_fresh_matching_weight(self):
        reader = self.reader([indication(), indication(value=2.0, node=OTHER_E2_NODE,
                                                      epoch=OTHER_EPOCH, at=1757000001)],
                             now='2025-09-04T15:33:25.000000Z')
        self.assertEqual({'pfWeight': 0.5}, reader.read(PF.readback_counter, self.scope()))

    def test_producer_status_must_agree_with_independent_fractional_counter(self):
        for claimed in (0.5, 1.0):
            with self.subTest(claimed=claimed):
                status = {'aicStatus': {'readback': {'result': 'VERIFIED',
                            PF.observed_key: {'pfWeight': claimed}}}}
                # 시계는 흘러야 한다.  되읽기는 2026-09-18 부터 '커밋 뒤 다른 값' 을
                # 즉시 불일치로 읽지 않고 마감까지 다시 읽는다(옛 값을 실은 KPM 지시와
                # 진짜 불일치를 가르기 위해서다).  멈춘 시계에서는 마감이 오지 않아
                # 이 테스트가 영원히 돈다 -- 실제로 묶음 검증을 50분 넘게 잡아먹었다.
                clock = {'ms': 0}

                def monotonic():
                    clock['ms'] += 1000
                    return clock['ms']

                readback = CorroboratedConfigReadback(
                    PF, status_port=SimpleNamespace(get_policy_status=lambda _: status),
                    kpm_reader=self.reader([indication()]), monotonic_ms=monotonic,
                    sleep_ms=lambda _: None, cadence_ms=1000, deadline_ms=20000)
                result = readback(scope=self.scope(), transaction_id='tx', policy_id='policy')
                self.assertEqual({PF.axis: {'pfWeight': 0.5}} if claimed == 0.5 else None, result)

    def test_joint_scoped_pf_policy_can_name_the_objective_ue(self):
        build = controlled_scope_builder(
            PF, supplementary_action(PRIORITY_ACTION_ID),
            controlled_scope_key='controlledUe@132',
            validity_provider=lambda _: {'notBefore': '2026-09-04T00:00:00Z',
                                         'notAfter': '2026-09-05T00:00:00Z'})
        body = build({'operation': 'APPLY', 'transactionId': 'tx', 'trialId': 'trial',
                      'fencingToken': 2, 'commandSequence': 1, 'commandIndex': 1,
                      'idempotencyKey': 'tx:APPLY:2:1', 'axis': 'pfWeight@132', 'value': '0.5',
                      'scope': {**CONTROLLED_UE, 'controlledUe@132': CONTROLLED_UE}})
        self.assertEqual(0.5, body['config']['pfWeight'])
        self.assertEqual('132', body['config']['ueId'])


class PfLiveCompositionTests(unittest.TestCase):
    def compose(self, directory, publish, *, action_id=PRIORITY_ACTION_ID):
        ran = EmulatedRan(ues={'131': '12345678'},
                          cells={'12345678': 5.0, '87654321': 5.0}, noise_sigma=0.0)
        clock = EmulatedClock(ran)
        stream = EmulatedKpmStream(clock, ran)
        profile = HermeticDeployment.write(directory, ues=ran.ues, cells=ran.cells)
        family = PF if action_id == PRIORITY_ACTION_ID else CAMPAIGN5_FAMILIES['cap']
        kind = 'pfWeight' if action_id == PRIORITY_ACTION_ID else 'dlPrbCap'
        self.ran, self.clock, self.stream = ran, clock, stream

        def lines():
            records = [json.loads(line) for line in stream()]
            if publish:
                for record in records:
                    record['ues'][0]['measurements'] = [
                        {'name': family.readback_counter,
                         'type': 'real' if kind == 'pfWeight' else 'int',
                         'value': 0.5 if kind == 'pfWeight' else ran.caps['131']}]
            return [json.dumps(record) for record in records]

        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            return build_agent_sitting(
                profile, AgentRequest(
                    sentences=('I1: UE ueId=131 needs at least 3.0 Mbps downlink, relaxable to 2.0',),
                    method='deterministic', axes=('servingCell', kind),
                    pf_weights={'131': (0.5,)}, caps={'131': (6, 12)},
                    restrict_catalog_to_candidates=False),
                read_new_lines=lines, policy_port=EmulatedPolicyPort(POLICY_TYPE_ID),
                action_policy_ports={action_id: object()}, ports=clock,
                scope_clearer=lambda *args, **kwargs: (),
                kpi_observer=EmulatedKpiObserver(ran), stamp='20260908T120000Z')

    def test_pf_composes_with_real_participant_and_fractional_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            sitting = self.compose(directory, True)
            participant, = sitting.supplementary
            self.assertEqual('pfWeight@131', participant.axis)
            self.assertEqual(PF.policy_type_id, participant.policy_type_id)
            self.assertEqual(PRIORITY_ACTION_ID, participant.action_id)
            self.assertEqual({'pfWeight': 0.5}, participant.counter_reader.read(
                PF.readback_counter, {'controlledUe@131': {'ueId': '131'}}))
            self.assertIsNone(participant.request)
            participant.close()

    def test_cap_joint_handover_updates_counter_and_policy_attribution(self):
        family = CAMPAIGN5_FAMILIES['cap']
        with tempfile.TemporaryDirectory() as directory:
            sitting = self.compose(directory, True, action_id=CAP_ACTION_ID)
            participant, = sitting.supplementary
            try:
                reader = participant.counter_reader
                self.assertIsNotNone(reader._attribution_provider)
                self.ran.ues['131'] = '87654321'
                self.ran.caps['131'] = 6
                self.clock.sleep_ms(100)
                scope = {'controlledUe@131': {'ueId': '131', 'cellId': '12345678'}}
                self.assertEqual({'maxDlPrbs': 6}, reader.read(family.readback_counter, scope))
                self.assertEqual(87654321, reader.expected.serving_nci)
                body = participant.adapter._build({
                    'operation': 'APPLY', 'transactionId': 'tx', 'trialId': 'trial',
                    'fencingToken': 2, 'commandSequence': 1, 'commandIndex': 1,
                    'idempotencyKey': 'tx:APPLY:2:1', 'axis': 'dlPrbCap@131',
                    'value': '6', 'scope': scope})
                self.assertEqual('87654321', body['config']['cellId'])
                self.assertEqual('131', body['config']['ueId'])
                self.assertEqual(6, body['config']['maxDlPrbs'])
            finally:
                participant.close()

    def test_absent_pf_counter_refuses_axis_and_counter_by_name(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LiveConsoleError,
                    r'pfWeight@131.*RAN.UE.PfWeight: COUNTER_ABSENT'):
                self.compose(directory, False)
