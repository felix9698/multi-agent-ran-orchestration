"""Hermetic guard tests. No test may inherit real credentials or run a process."""
import base64
import copy
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT/'experiment_results/ota-20260911'
FAKE_ENV = {'ANTHROPIC_BASE_URL':'http://127.0.0.1:8317',
            'AIC_CLAUDE_MODEL_ID':'test-provider-model',
            'ANTHROPIC_AUTH_TOKEN':'fake-test-token-never-a-real-credential'}
SESSION = 'formal38guarded-20260911T090000-'+'a'*32
MAPPING = {'901':'ue3','903':'ue1','902':'ue2'}
HEADER_ROWS = {host: {'RC_HEADER_RRC_UE_ID': 1, 'RC_HEADER_AMF_UE_NGAP_ID': int(ue),
                     'nbId': 3584, 'connectionEpoch': 796} for ue, host in MAPPING.items()}
BOOTS = {host: f'00000000-0000-0000-0000-00000000000{i}' for i,host in enumerate(('ue1','ue2','ue3'),1)}
IDENTITIES = {host: {'name':'oaitun_ue1','ip':f'12.1.1.{100+i}', 'ifindex':10+i,
                      'up':True,'bootId':BOOTS[host]} for i,host in enumerate(('ue1','ue2','ue3'),1)}


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, EXPERIMENT/filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guarded = load('guarded_runner_tests','atomic_formal_run_guarded.py')
helper = load('guarded_process_tests','guarded_source_process.py')


def snapshot(spec, session):
    result = {'schemaVersion':'flow-goodput-snapshot/1' if spec['role']=='receiver' else 'tagged-echo-snapshot/1',
              'sessionId':session,'flowId':spec['flowId'],'clockId':BOOTS[spec['host']],
              'status':'running','observedAtMs':900.,'remoteNowMs':1000.,
              'interface':{key: value for key,value in IDENTITIES[spec['host']].items() if key!='bootId'},
              'sourceLog':f"/tmp/aic-{session}/{spec['slot']}.jsonl"}
    if spec['role']=='receiver':
        result.update(measurementDefinition='tcp-application-payload-consumed',payloadBytes=0,tunRxBytes=0,
                      connection={'peerIp':'192.168.70.135','peerPort':12000,'connectedAtMs':800})
    else:
        result['countersByDeadlineMs'] = {d:dict(issued=5,eligible=4,completed=0) for d in ('200','300')}
    return result


def owner(pid=101, argv=None):
    return {'pid':pid,'startTicks':123,'pgid':pid,'ppid':77,'bootId':BOOTS['ue1'],'uid':1000,
            'argv':argv or ['python3','a-source','--session-id',SESSION]}


def ownership():
    return {'ready':True,'owner':owner(),'sourceOwner':owner(102),
            'listener':{'pid':102,'startTicks':123,'inode':'555','protocol':'tcp','ip':'12.1.1.101','port':6301}}


class Hermetic(unittest.TestCase):
    def setUp(self):
        # Start this FIRST. Failure diagnostics and mock call records can only
        # ever contain explicit fake values, not a copy of the session env.
        self.env = patch.dict(os.environ, FAKE_ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.block_run = patch('subprocess.run', side_effect=AssertionError('unmocked subprocess.run'))
        self.block_popen = patch('subprocess.Popen', side_effect=AssertionError('unmocked subprocess.Popen'))
        self.run_mock = self.block_run.start()
        self.block_popen.start()
        self.addCleanup(self.block_run.stop)
        self.addCleanup(self.block_popen.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.hold = patch.object(guarded,'AUTHENTICATION_HOLD',self.tmp/'fake-authentication-hold.json')
        self.hold.start();self.addCleanup(self.hold.stop)


class PolicyTests(Hermetic):
    def test_import_has_no_file_environment_or_process_side_effects(self):
        with patch.object(Path,'mkdir',side_effect=AssertionError('mkdir at import')), \
             patch.object(Path,'read_text',side_effect=AssertionError('read at import')), \
             patch.object(Path,'read_bytes',side_effect=AssertionError('read at import')), \
             patch.object(os.environ,'get',side_effect=AssertionError('env at import')):
            load('fresh_guarded','atomic_formal_run_guarded.py')
            load('fresh_helper','guarded_source_process.py')
        self.run_mock.assert_not_called()

    def test_exact_pilot_policy_changes_only_ue_ids_via_manifest(self):
        result = guarded.pilot_intents(MAPPING)
        pilot = json.loads((guarded.PILOT/'intents.json').read_text())
        old_hosts = json.loads((guarded.PILOT/'manifest.json').read_text())['ueHosts']
        for before, after in zip(pilot['intents'], result['intents']):
            expected = copy.deepcopy(before)
            # Intents are sealed on the UE host role; the sitting resolves the id at run time.
            expected['ueId'] = old_hosts[before['ueId']]
            self.assertEqual(after,expected)
        # The values are the default PILOT's own, read from the pinned directory:
        # the point of this test is that the mapping changes ueId and nothing
        # else, so the expectation has to follow whichever pilot is pinned.
        self.assertEqual([row['requirement']['value'] for row in result['intents']],
                         [row['requirement']['value'] for row in pilot['intents']])
        self.assertEqual([9.0, 0.9, 9.0, 0.9, 9.0, 0.9],
                         [row['requirement']['value'] for row in result['intents']])
        # The response half of each owner's pair: 1000 ms original, and the
        # extended 1500 ms only where that owner is authorized to reach it.
        self.assertEqual([(1000, 1000), (1000, 1500), (1000, 1500)],
                         [(row['requirement']['deadlineMs'], row['requirement']['deadlineBound'])
                          for row in result['intents']
                          if row['requirement']['kpi'] == 'deadlineSuccessRatio'])

    def test_pilot_mutation_refuses(self):
        for name in ('intents.json','manifest.json'):
            (self.tmp/name).write_bytes((guarded.PILOT/name).read_bytes())
        (self.tmp/'intents.json').write_text('{}')
        with self.assertRaisesRegex(guarded.Refused,'PILOT_INTENTS_HASH_MISMATCH'):
            guarded.pilot_intents(MAPPING,self.tmp)

    def test_mapping_is_complete_unique_and_decimal(self):
        for invalid in ({'1':'ue1'}, {'1':'ue1','2':'ue1','3':'ue3'},
                        {'x':'ue1','2':'ue2','3':'ue3'}):
            with self.subTest(mapping=invalid),self.assertRaises(guarded.Refused):
                guarded.mapping_by_host(invalid)

    def test_the_overall_deadline_can_be_removed_and_comes_back_with_a_number(self):
        """2026-09-16, the owner's call: a sitting must not be ended by the clock while it is
        waiting for a UE the radio dropped, because that is a hardware fault and not a cost of
        the control being measured. AIC_DEADLINE_S=off omits the flag, main.py then leaves
        request.deadline_ms None and agent.py::_deadline_spent skips the check entirely."""
        for value in ('off', 'none', '0', 'OFF'):
            with self.subTest(value=value), patch.dict(os.environ, {'AIC_DEADLINE_S': value}):
                self.assertNotIn('--deadline-s', guarded.command_for(self.tmp, MAPPING))
        with patch.dict(os.environ, {'AIC_DEADLINE_S': '420'}):
            argv = guarded.command_for(self.tmp, MAPPING)
            self.assertEqual('420', argv[argv.index('--deadline-s') + 1])

    def test_original_flags_and_authenticated_entry_preserved(self):
        argv = guarded.command_for(self.tmp,MAPPING)
        self.assertEqual(Path(argv[2]).name,'authenticated_live_entry.py')
        # B and H are 10/15, not the original 60/90: exp_metrics.md section 1 sets the
        # physical timing from pilot measurements. Decomposing the one episode that
        # finished, the prepared board removes 121.2 s of Target and Control and leaves
        # about 10 s of header freeze, so a UE must hold for its attach plus that plus
        # B+H. ue1's fifteen measured lives top out at 70.6 s, which 20/30 could not
        # meet. H >= B still holds and H is a whole number of the frozen 5 s bins. This
        # guard exists so the pair cannot drift silently, so it moves with them.
        flags = {'--budget':'4','--deadline-s':'10','--horizon-s':'15','--axes':'servingCell,dlPrbCap',
                 '--retain':'11','--max-catalog':'512','--cells':'12345678,87654321',
                 '--quality-thresholds':'0,0.25,0.5,1.0'}
        for flag,value in flags.items():
            self.assertEqual(argv[argv.index(flag)+1],value)
        self.assertIn('--stop-after-relaxed-success',argv)
        self.assertIn('dlGoodputMbps=1000:5000:60000',argv)
        self.assertIn('deadlineSuccessRatio=1000:5000:60000',argv)
        self.assertEqual({argv[i+1] for i,x in enumerate(argv) if x=='--cap-axis'},
                         {'ue1:6,12','ue2:6,12','ue3:6,12'})
        # The first domain-shaping flag in this argv: --preference travels by
        # environment, this one does not.  It names the pinned pilot's file,
        # never the per-attempt root/, because the shaping keys on reqIds.
        self.assertEqual(argv[argv.index('--answers')+1],
                         str(guarded.PILOT/'answers.json'))

    def test_the_answers_flag_shapes_the_domain_to_the_authorized_54(self):
        # An argv assertion alone is how the preference profiles came to be
        # "implemented" while every episode ran the bare default, so this takes
        # the value out of the argv and drives it down the path the child takes
        # -- _parse_answers -> merge_answers -> expand_targets -- and counts the
        # domain at the far end.  Hermetic: two files off disk, no radio, no
        # model, no network.
        import main
        from assurance.coordination.intake import merge_answers
        from assurance.coordination.tc import Authorization, Intent, expand_targets

        argv = guarded.command_for(self.tmp,MAPPING)
        supplied = argv[argv.index('--answers')+1]
        # Shape what the child is actually given -- the remapped document, not
        # the pristine pilot; the answers key on reqIds and owner strings, which
        # the ueId remap does not touch.
        intents = tuple(Intent.from_record(row)
                        for row in guarded.pilot_intents(MAPPING)['intents'])
        unshaped = Authorization.from_intents(intents)
        self.assertEqual(len(expand_targets(unshaped).targets), 108)
        merged, authorization, _ = merge_answers(
            intents, unshaped, main._parse_answers(supplied), {})
        self.assertEqual(len(expand_targets(authorization).targets), 54)
        # Shaped, and nothing else: the intent rows the pinned hash covers come
        # back identical, so the file's top-level _provenance key is inert.
        self.assertEqual(merged, intents)

    def test_the_candidate_budget_is_twelve_and_overridable(self):
        # The integrated reply section 4 sets Control's budget at 12
        # configurations including baseline; AIC_RETAIN lowers it for a
        # calibration probe that must not spend a full board.
        with patch.dict(os.environ,{'AIC_RETAIN':'6'}):
            argv = guarded.command_for(self.tmp,MAPPING)
        self.assertEqual(argv[argv.index('--retain')+1],'6')

    def test_the_flag_builds_twelve_configurations_including_the_baseline(self):
        # An argv assertion alone is what let three values of this one number
        # coexist -- 11 in the library, 12 in this flag, 4K per trial in the
        # executor -- because each looked right read on its own.  So take the
        # value out of the argv and drive it down the path the child takes,
        # settings -> build_agent_sitting -> validate_control_candidates, and
        # count the board at the far end.  Hermetic: emulated radio, no model,
        # no network, no process.
        from tools.liveconsole.agent import (
            METHOD_DETERMINISTIC, AgentRequest, build_hardware_free_agent_sitting)
        argv = guarded.command_for(self.tmp,MAPPING)
        retain = int(argv[argv.index('--retain')+1])
        sitting = build_hardware_free_agent_sitting(
            AgentRequest(sentences=('I1: UE ueId=131 needs at least 3.0 Mbps downlink',
                                    'I2: UE ueId=132 needs at least 1.5 Mbps downlink'),
                         method=METHOD_DETERMINISTIC, budget_trials=4,
                         # A product wide enough that the cap is what decides
                         # the count: two four-rung cap ladders and the steer.
                         caps={'131':(6,12,18,24),'132':(6,12,18,24)},
                         answers={'I1':{'steps':2,'bound':2.0},
                                  'I2':{'steps':1,'bound':1.0}},
                         settings={'retain':retain}),
            tmp_dir=str(self.tmp), stamp='20260911T000000Z')
        # retain is a ceiling: since 2026-09-19 a steer excludes a cap on the same UE,
        # and this two-UE board has fewer than 11 admissible moves.
        self.assertLessEqual(len(sitting.controls.control_ids), retain + 1)
        self.assertEqual(12, retain + 1)
        self.assertEqual('C0', sitting.controls.control_ids[0])
        # ... and the cap that produced it is readable back off the artefact,
        # not only off this argv: both places the episode record carries it.
        self.assertEqual(retain, sitting.intake['settings']['retain'])
        self.assertEqual(retain, sitting.controls.construction_policy['retain'])

    def test_up_unique_ipv4_boot_and_ifindex_are_all_pinned(self):
        self.assertEqual(guarded.check_identities(IDENTITIES),IDENTITIES)
        for key,value in [('up',False),('ip','12.1.1.102'),('ifindex',0),('bootId','')]:
            changed = copy.deepcopy(IDENTITIES); changed['ue1'][key]=value
            with self.subTest(key=key),self.assertRaises(guarded.Refused):
                guarded.check_identities(changed)
        for key,value in [('ip','12.1.1.120'),('ifindex',99),('bootId',BOOTS['ue2'])]:
            changed=copy.deepcopy(IDENTITIES); changed['ue1'][key]=value
            with self.subTest(pinned=key),self.assertRaisesRegex(guarded.Refused,'TUN_IDENTITY_CHANGED'):
                guarded.check_identities(changed,IDENTITIES)

    def test_missing_stale_or_changed_source_is_not_zero_filled(self):
        spec = guarded.specs_for(IDENTITIES,6301)[0]
        good = snapshot(spec,SESSION)
        self.assertEqual(guarded.check_snapshot(good,spec,SESSION,IDENTITIES)['payloadBytes'],0)
        for field, bad in [('remoteNowMs',5000),('clockId','another-boot'),('payloadBytes',None),
                           ('sessionId','different-session'),('status','stopped'),('interface',{})]:
            value = copy.deepcopy(good);value[field]=bad
            with self.subTest(field=field),self.assertRaises(guarded.Refused):
                guarded.check_snapshot(value,spec,SESSION,IDENTITIES)

    def test_partial_or_mock_episode_is_not_a_started_live_episode(self):
        (self.tmp/'evidence').mkdir()
        path=self.tmp/'evidence'/'AGENT-test-episode.json'
        for value in ([], {'schemaVersion':'agent-episode/1.3.0','sessionMode':'MOCK','episodeId':'case/test'},
                      {'schemaVersion':'agent-episode/1.3.0','mode':'LIVE','episodeId':'case/test'}):
            path.write_text(json.dumps(value))
            self.assertEqual(guarded.submission_status(self.tmp,True,0)[0],
                             'SUBMISSION_UNKNOWN_RECONCILIATION_REQUIRED')

    def test_quoted_refusal_text_is_not_a_framework_refusal(self):
        (self.tmp/'evidence').mkdir()
        (self.tmp/'live-sitting.stdout').write_text('model said: refused before anything was submitted:\n')
        self.assertEqual(guarded.submission_status(self.tmp,True,3)[0],
                         'SUBMISSION_UNKNOWN_RECONCILIATION_REQUIRED')

    def test_echo_deadline_counters_are_independent_and_required(self):
        spec = guarded.specs_for(IDENTITIES,6301)[-1]
        value=snapshot(spec,SESSION)
        self.assertEqual(guarded.check_snapshot(value,spec,SESSION,IDENTITIES),value)
        del value['countersByDeadlineMs']['300']
        with self.assertRaisesRegex(guarded.Refused,'ECHO_COUNTERS'):
            guarded.check_snapshot(value,spec,SESSION,IDENTITIES)

    def test_profile_relocates_binding_and_uses_unique_source_logs(self):
        base={'integrationValuesPath':'values.json','capabilityManifestPath':'cap.json',
              'liveConsole':{'assuranceBindingPath':'binding.json','producerDatabasePath':'db.sqlite',
                             'actionProducer':{}},'intentDefaults':[{'old':True}]}
        result=guarded.make_profile(base,self.tmp/'base.json',self.tmp/'attempt',MAPPING,
                                    guarded.specs_for(IDENTITIES,6301),SESSION)
        self.assertEqual(result['liveConsole']['assuranceBindingPath'],str(self.tmp/'binding.json'))
        self.assertEqual(result['intentDefaults'],[])
        self.assertEqual(result['liveConsole']['ueHosts'],{host:host for host in MAPPING.values()})
        self.assertEqual(result['liveConsole']['ueIdentityPath'],str(guarded.role_identity_path(self.tmp/'attempt')))
        self.assertIn(SESSION,result['liveConsole']['flowGoodput']['ue1']['logPath'])
        self.assertEqual(base['intentDefaults'],[{'old':True}])


class KpmTests(Hermetic):
    def fixtures(self, *, shift=0, wrong_pair=False, unknown_epoch=False, empty=False):
        from assurance.live.pin_to_cell_driver import LiveCellTopology
        nodes=['plmn=001/01;nb=0000003584/00;kind=gnb','plmn=001/01;nb=0000002816/00;kind=gnb']
        topology=LiveCellTopology(plmn={'mcc':'001','mnc':'01'},nb_id_to_nci={3584:12345678,2816:87654321},
                                  expected_epochs={nodes[0]:5,nodes[1]:7})
        at=int((datetime.now(timezone.utc)+timedelta(seconds=shift)).timestamp()*1e6)
        guami={'mcc':1,'mnc':1,'mnc_digit_len':2,'amf_region_id':0,'amf_set_id':1,'amf_pointer':0}
        rows=[{'event':'kpm_indication','e2_node':nodes[0],'nb_id':2816 if wrong_pair else 3584,
               'connection_epoch':99 if unknown_epoch else 5,'recv_unix_us':at,
               'ues':[] if empty else [{'amf_ue_ngap_id':903,'guami':guami},
                                      {'amf_ue_ngap_id':902,'guami':guami}]},
              {'event':'kpm_indication','e2_node':nodes[1],'nb_id':2816,'connection_epoch':7,
               'recv_unix_us':at,'ues':[] if empty else [{'amf_ue_ngap_id':901,'guami':guami}]}]
        return topology,[json.dumps(row) for row in rows]

    def check(self, topology, lines, mapping=MAPPING):
        deployment=SimpleNamespace(binding=object(),capability={},kpm_jsonl_path=self.tmp/'kpm.jsonl')
        with patch('tools.g3ota.composition.live_topology',return_value=topology), \
             patch('tools.g3ota.composition.KpmTail') as tail:
            tail.return_value.read_new_lines.return_value=lines
            return guarded.kpm_dependencies(deployment,mapping)

    def test_fresh_real_attribution_supports_either_cell(self):
        topology,lines=self.fixtures()
        value=self.check(topology,lines)
        self.assertEqual(value['initialAssociation'],{'903':12345678,'902':12345678,'901':87654321})
        self.assertTrue(value['observations']['903']['traceHash'])
        self.assertTrue(value['observations']['903']['observedAt'].endswith('Z'))

    def test_stale_future_unknown_epoch_or_wrong_node_fail_closed(self):
        for options in ({'shift':-20},{'shift':20},{'wrong_pair':True},{'unknown_epoch':True}):
            with self.subTest(options=options),self.assertRaisesRegex(guarded.Refused,'FRESH_TWO_CELL_KPM'):
                self.check(*self.fixtures(**options))

    def test_node_dependency_allows_no_ue_but_attribution_does_not(self):
        topology,lines=self.fixtures(empty=True)
        self.assertEqual(len(self.check(topology,lines,mapping=None)['nodes']),2)
        with self.assertRaisesRegex(guarded.Refused,'FRESH_UE_KPM'):
            self.check(topology,lines)

    def test_simultaneously_fresh_dual_attribution_is_ambiguous(self):
        topology,lines=self.fixtures()
        second=json.loads(lines[1]); guami=second['ues'][0]['guami']
        second['ues'].append({'amf_ue_ngap_id':903,'guami':guami});lines[1]=json.dumps(second)
        with self.assertRaisesRegex(guarded.Refused,'ATTRIBUTION_AMBIGUOUS'):
            self.check(topology,lines)


class ProcessTests(Hermetic):
    def setUp(self):
        super().setUp()
        self.state=patch.object(helper,'STATE_PARENT',self.tmp)
        self.state.start();self.addCleanup(self.state.stop)
        self.spec=guarded.specs_for(IDENTITIES,6301)[0]

    def test_deployment_missing_or_hash_mismatch_is_concrete_refusal(self):
        with self.assertRaisesRegex(helper.GuardError,'SOURCE_MISSING:flow_goodput.py'):
            helper.checked_sources(str(self.tmp))
        (self.tmp/'flow_goodput.py').write_text('not-the-source')
        with self.assertRaisesRegex(helper.GuardError,'SOURCE_HASH_MISMATCH:flow_goodput.py'):
            helper.checked_sources(str(self.tmp))

    def test_existing_or_unreadable_source_inventory_refuses_without_signalling(self):
        existing=owner(argv=['python3','/different-deployment/flow_goodput.py','--session-id','other'])
        for state in (existing,PermissionError('fake proc denial')):
            with self.subTest(unreadable=isinstance(state,Exception)), \
                 patch.object(helper,'checked_sources',return_value=Path('/fake')), \
                 patch.object(Path,'iterdir',return_value=[Path('/proc/101')]), \
                 patch.object(helper,'proc_identity',side_effect=state if isinstance(state,Exception) else None,
                              return_value=state),patch.object(os,'pidfd_open',return_value=77), \
                 patch.object(os,'close'),patch.object(signal,'pidfd_send_signal') as send, \
                 self.assertRaises(helper.GuardError):
                helper.handle({'action':'preflight','sourceDir':'/fake'})
            send.assert_not_called()

    def test_real_local_source_hashes_match_pinned_deployment_contract(self):
        for name,expected in helper.SOURCE_HASHES.items():
            self.assertEqual(hashlib.sha256((ROOT/'tools/liveconsole'/name).read_bytes()).hexdigest(),expected)

    def test_exclusive_owner_and_log_preexistence_never_clobber(self):
        path=self.tmp/'receipt.json'
        helper.exclusive_json(path,{'pid':1})
        with self.assertRaises(FileExistsError):helper.exclusive_json(path,{'pid':2})
        self.assertEqual(json.loads(path.read_text()),{'pid':1})
        where=helper.paths_for(SESSION,'ue1-rx',True)
        where['log'].write_text('historical')
        request={'session':SESSION,'spec':self.spec,'endpoint':'ue1','sourceDir':'/fake'}
        with patch.object(helper,'source_argv',return_value=['fake']), \
             patch.object(helper,'check_identity'),self.assertRaisesRegex(helper.GuardError,'SOURCE_LOG_EXISTS'):
            helper.start(request)
        self.assertEqual(where['log'].read_text(),'historical')

    def test_workload_arguments_and_container_only_roles(self):
        with patch.object(helper,'checked_sources',return_value=Path('/fake')):
            specs=guarded.specs_for(IDENTITIES,6301)
            for spec in specs:
                argv=helper.source_argv(SESSION,spec['endpoint'],spec,'/fake')
                if spec['role']=='sender':self.assertEqual(argv[argv.index('--rate-mbps')+1],'1')
                # The two echo rates MUST differ, and this asymmetry is the fix, not a
                # typo to tidy away.  run_server's limiter gates replies at 1/rate_hz
                # from the last admitted arrival, so a server at the client's own '5'
                # gates at 200.000 ms against a 200.281 ms cadence and silently drops
                # ~37 % of requests, capping I4's deadlineSuccessRatio at 2/3.  The
                # earlier form of this test asserted '5' for both roles and so pinned
                # that defect in place.  Server = headroom, client = offered cadence.
                if spec['role']=='echo_server':self.assertEqual(argv[argv.index('--rate-hz')+1],'20')
                if spec['role']=='echo_client':self.assertEqual(argv[argv.index('--rate-hz')+1],'5')
                if spec['role'] in ('sender','echo_server'):
                    with self.assertRaises(helper.GuardError):helper.source_argv(SESSION,'ue1',spec,'/fake')

    def test_pid_reuse_before_or_after_pidfd_open_never_signals(self):
        pinned=owner();current={**pinned,'startTicks':999}
        for sequence in ([current],[pinned,current]):
            with self.subTest(race=len(sequence)),patch.object(helper,'proc_identity',side_effect=sequence), \
                 patch.object(os,'pidfd_open',return_value=77),patch.object(os,'close'), \
                 patch.object(signal,'pidfd_send_signal') as send,self.assertRaises(helper.GuardError):
                helper.signal_exact(pinned,[pinned['argv']],signal.SIGTERM)
            send.assert_not_called()

    def test_foreign_session_argv_never_signals(self):
        pinned=owner();current={**pinned,'argv':['other-session']}
        with patch.object(helper,'proc_identity',return_value=current), \
             patch.object(signal,'pidfd_send_signal') as send,self.assertRaises(helper.GuardError):
            helper.signal_exact(pinned,[pinned['argv']],signal.SIGTERM)
        send.assert_not_called()

    def test_exact_owner_uses_pidfd_not_pid_or_group_kill(self):
        pinned=owner()
        with patch.object(helper,'proc_identity',return_value=pinned), \
             patch.object(os,'pidfd_open',return_value=77) as opened,patch.object(os,'close'), \
             patch.object(signal,'pidfd_send_signal') as send, \
             patch.object(os,'kill',side_effect=AssertionError('unfenced kill')):
            self.assertEqual(helper.signal_exact(pinned,[pinned['argv']],signal.SIGTERM),'signalled')
        opened.assert_called_once_with(pinned['pid']);send.assert_called_once_with(77,signal.SIGTERM)

    def test_foreign_listener_same_port_is_not_ready_and_no_connection_probe(self):
        process=owner(111)
        table='sl local_address rem_address st tx_queue tr retr uid timeout inode\n'
        table+='0: 6501010C:189D 00000000:0000 0A 0:0 0:0 0 1000 0 777\n'
        with patch.object(Path,'iterdir',return_value=[Path('/proc/111/fd/3')]), \
             patch.object(os,'readlink',return_value='socket:[555]'),patch.object(Path,'read_text',return_value=table), \
             patch('socket.create_connection',side_effect=AssertionError('connection probe')):
            self.assertIsNone(helper.listening_socket(process,'tcp','12.1.1.101',6301))
        table=table.replace('777','555')
        with patch.object(Path,'iterdir',return_value=[Path('/proc/111/fd/3')]), \
             patch.object(os,'readlink',return_value='socket:[555]'),patch.object(Path,'read_text',return_value=table):
            self.assertEqual(helper.listening_socket(process,'tcp','12.1.1.101',6301)['inode'],'555')

    def claim(self, session=SESSION):
        where=helper.paths_for(session,'ue1-rx',True)
        claim={'session':session,'spec':self.spec,'bootstrapArgv':['bootstrap',session],
               'sourceArgv':['source',session],'sourceBootstrapArgv':['source-bootstrap',session]}
        helper.exclusive_json(where['claim'],claim)
        return where,claim

    def test_lost_start_reply_cleanup_cancels_unpublished_child_and_other_session_is_untouched(self):
        where,_=self.claim()
        other='formal38guarded-20260911T090001-'+'b'*32
        foreign,_=self.claim(other)
        with patch.object(helper,'stop_process') as stop:
            result=helper.cleanup({'session':SESSION})
        self.assertTrue(result['complete']);stop.assert_not_called()
        self.assertTrue((where['claim'].parent/'cancelled').is_file())
        self.assertFalse((foreign['claim'].parent/'cancelled').exists())

    def test_late_child_publishes_then_obeys_cancel_without_exec(self):
        where,_=self.claim()
        helper.cleanup({'session':SESSION})
        with patch.object(helper,'proc_identity',return_value=owner()), \
             patch.object(os,'execv') as execute,self.assertRaisesRegex(helper.GuardError,'SESSION_CANCELLED'):
            helper.child(str(where['claim']),source_child=True)
        self.assertTrue(where['source_owner'].is_file());execute.assert_not_called()

    def test_cleanup_supervisor_reuse_still_attempts_exact_source_only(self):
        where,claim=self.claim()
        helper.exclusive_json(where['owner'],owner())
        helper.exclusive_json(where['source_owner'],owner(102))
        calls=[]
        def stop(pinned,argv):
            calls.append(pinned['pid'])
            if pinned['pid']==101:raise helper.GuardError('PID_REUSED_OR_UNOWNED')
            return 'stopped'
        with patch.object(helper,'stop_process',side_effect=stop):
            result=helper.cleanup({'session':SESSION})
        self.assertEqual(calls,[101,102]);self.assertFalse(result['complete'])

    def test_source_child_identity_receipt_precedes_exec(self):
        where,claim=self.claim()
        claim.update(sourceDir='/fake',endpoint='ue1')
        # This is a temporary test fixture only, not a production owner replacement.
        where['claim'].write_text(json.dumps(claim))
        def execute(program,argv):
            self.assertTrue(where['source_owner'].exists())
            raise RuntimeError('exec intercepted')
        with patch.object(helper,'proc_identity',return_value=owner()), \
             patch.object(helper,'check_identity'),patch.object(helper,'source_argv',return_value=claim['sourceArgv']), \
             patch.object(os,'execv',side_effect=execute),self.assertRaisesRegex(RuntimeError,'exec intercepted'):
            helper.child(str(where['claim']),source_child=True)


class TransportTests(Hermetic):
    def test_r1_preflight_reuses_read_only_discovery_and_never_writes_a_policy(self):
        files=[]
        for name in ('binding.json','cap.json','values.json'):
            path=self.tmp/name;path.write_text('{}');files.append(path)
        producer=Mock(api_root='https://fake.invalid')
        producer.for_action.return_value=SimpleNamespace(policy_type_id='FAKE_CAP')
        deployment=SimpleNamespace(action_producer=producer,values={},
            binding=SimpleNamespace(r1=SimpleNamespace(policy_type_id='FAKE_PRIMARY')),
            capability={},binding_path=files[0],capability_path=files[1],integration_values_path=files[2])
        # Only these discovery methods exist; any control method call fails.
        primary=Mock(spec=['bootstrap_info','discover_services','get_policy_type','discover_policy_types'])
        cap=Mock(spec=['bootstrap_info','discover_services','get_policy_type','discover_policy_types'])
        cap.discover_policy_types.return_value=['FAKE_CAP'];cap.get_policy_type.return_value={'policyTypeId':'FAKE_CAP'}
        with patch('tools.liveconsole.profile.load_live_deployment',return_value=deployment), \
             patch.object(guarded,'control_header_rows',return_value=HEADER_ROWS), \
             patch.object(guarded,'write_control_headers',return_value={}), \
             patch.object(guarded,'ControlHeaderRefresher'), \
             patch.object(guarded,'kpm_dependencies',return_value={'nodes':{}}), \
             patch('tools.g3ota.composition.build_r1_policy_port',side_effect=[primary,cap]), \
             patch('tools.g3ota.composition.build_policy_type_discovery',return_value={'policyTypeIds':['FAKE_PRIMARY']}):
            result=guarded.r1_dependencies(self.tmp/'profile.json',self.tmp/'state')
        self.assertEqual(result['r1Discovery'],{'primary':'FAKE_PRIMARY','supplementary':'FAKE_CAP'})
        self.assertEqual(len(result['deploymentHashes']),3)
        cap.get_policy_type.assert_called_once_with('FAKE_CAP')

    def test_unavailable_kpm_refuses_before_r1_discovery(self):
        with patch('tools.liveconsole.profile.load_live_deployment',return_value=object()), \
             patch.object(guarded,'control_header_rows',return_value=HEADER_ROWS), \
             patch.object(guarded,'write_control_headers',return_value={}), \
             patch.object(guarded,'ControlHeaderRefresher'), \
             patch.object(guarded,'kpm_dependencies',side_effect=guarded.Refused('FRESH_TWO_CELL_KPM_REQUIRED')), \
             patch('tools.g3ota.composition.build_r1_policy_port') as port, \
             self.assertRaisesRegex(guarded.Refused,'FRESH_TWO_CELL_KPM'):
            guarded.r1_dependencies(self.tmp/'profile.json',self.tmp/'state')
        port.assert_not_called()

    def test_only_extdn_runs_docker_and_timeout_never_leaks_raw_stderr(self):
        remote=guarded.Remote(SESSION)
        self.run_mock.side_effect=None
        self.run_mock.return_value=SimpleNamespace(returncode=0,stdout='{"ok":true,"result":{}}',stderr='')
        remote.call('extdn','preflight')
        command=self.run_mock.call_args.args[0]
        self.assertEqual(command[:5],['docker','exec','-i','oai-ext-dn','python3'])
        remote.call('ue1','identity')
        self.assertEqual(self.run_mock.call_args.args[0][0],'ssh')
        self.run_mock.side_effect=subprocess.TimeoutExpired(['fake'],1,stderr='fake-sensitive-body')
        with self.assertRaisesRegex(guarded.Refused,'REMOTE_TIMEOUT:ue1:start') as caught:
            remote.call('ue1','start',spec={'slot':'ue1-rx'})
        self.assertNotIn('fake-sensitive-body',str(caught.exception))

    def test_missing_deployment_reason_is_preserved_without_raw_stderr(self):
        self.run_mock.side_effect=None
        self.run_mock.return_value=SimpleNamespace(returncode=1,stdout=json.dumps(
            {'ok':False,'error':'SOURCE_MISSING:flow_goodput.py'}),stderr='not-archived')
        with self.assertRaisesRegex(guarded.Refused,'SOURCE_MISSING:flow_goodput.py:ue1:preflight'):
            guarded.Remote(SESSION).call('ue1','preflight')

    def test_dependency_refusal_and_timeout_are_bounded_subprocesses(self):
        self.run_mock.side_effect=None
        self.run_mock.return_value=SimpleNamespace(returncode=3,stdout='{"ok":false,"errorType":"FRESH_TWO_CELL_KPM_REQUIRED"}',
                                                  stderr='fake credential not for artifacts')
        with self.assertRaisesRegex(guarded.Refused,'FRESH_TWO_CELL_KPM_REQUIRED'):
            guarded.dependency_preflight(self.tmp/'profile.json',self.tmp)
        self.assertEqual(self.run_mock.call_args.kwargs['timeout'],35)

    def test_private_join_failure_never_repeats_subscriber_stderr(self):
        script=self.tmp/'join.py';script.write_text('# fixture only')
        self.run_mock.side_effect=None
        self.run_mock.return_value=SimpleNamespace(returncode=1,stdout='',stderr='fake subscriber data')
        with self.assertRaisesRegex(guarded.Refused,'HOST_JOIN_FAILED') as caught:
            guarded.join_hosts(self.tmp/'base.json',self.tmp/'out.json',script)
        self.assertNotIn('subscriber',str(caught.exception))


class AttemptTests(Hermetic):
    def attempt(self, *, cli_exit=0, timeout=False, dependency_error=None, remote_failure=None,
                cleanup_failure=False, episode=True, refusal_marker=False, mapping_changed=False,
                identity_changed=False, stale_source=False, archive_failure=False,
                expiring_samples=0, slow_sample_s=0.0):
        calls=[]
        instances=[]
        def remote_factory(session):
            remote=Mock()
            remote.session=session
            def call(endpoint,action,**kwargs):
                spec=kwargs.get('spec')
                calls.append((endpoint,action,spec['slot'] if spec else None))
                if remote_failure and remote_failure(endpoint,action,spec):
                    raise guarded.Refused('REMOTE_TIMEOUT:'+endpoint+':'+action)
                if action=='preflight':return {'sourceHashes':helper.SOURCE_HASHES}
                if action=='identity':
                    value=copy.deepcopy(IDENTITIES[endpoint])
                    if identity_changed and any(row[1]=='start' for row in calls):
                        value['ifindex']+=100
                    return value
                if action=='start':return {'reserved':True,'launcherPid':101}
                if action=='inspect':return ownership()
                if action=='sample':
                    if slow_sample_s:
                        import time as _time; _time.sleep(slow_sample_s)
                    value=snapshot(spec,session)
                    if slow_sample_s:
                        value['remoteNowMs']=value['observedAtMs']+400
                    if stale_source:value['remoteNowMs']=5000
                    if spec['flowId']=='ue1-data' and sum(1 for row in calls if row[1]=='sample' and row[2]==spec['slot'])<=expiring_samples:
                        value['remoteNowMs']=value['observedAtMs']+1500
                    return {'ownership':ownership(),'snapshot':value}
                if action=='cleanup':return {'complete':not cleanup_failure,'cancelled':True,'processes':[]}
                if action=='archive':return {'files':[],'errors':['fake failure'] if archive_failure else []}
                raise AssertionError('unexpected fake action')
            remote.call.side_effect=call
            instances.append(remote)
            return remote
        base={'liveConsole':{'assuranceBindingPath':'binding.json','producerDatabasePath':'state.sqlite'}}
        joins=[(MAPPING,base),(dict(MAPPING),base)]
        if mapping_changed:joins[1]=({'904':'ue1','902':'ue2','901':'ue3'},base)
        def kpm(*args):
            return {'initialAssociation':{'903':12345678,'902':12345678,'901':87654321},
                    'checkedAt':guarded.utc_now(),'freshnessBoundMs':4000,
                    'observations':{ue:{'observedAt':guarded.utc_now()} for ue in MAPPING}}
        def cli(command,**kwargs):
            # No env= of its own: the sitting inherits this process's
            # environment, which is the ONLY path AIC_PREFERENCE has into the
            # child (there is no CLI flag for the owner preference). If this
            # call ever starts passing env=, the profile silently stops
            # arriving and every case ranks under the bare default again.
            assert 'env' not in kwargs,'the sitting must inherit this environment'
            attempt_root=Path(command[command.index('--runs-root')+1]).parent
            if episode:
                guarded.write_json(attempt_root/'evidence'/'AGENT-test-episode.json',
                    {'schemaVersion':'agent-episode/1.3.0','sessionMode':'LIVE','episodeId':'case/test',
                     'termination':{'reason':'BUDGET_EXHAUSTED'}})
            if refusal_marker:kwargs['stdout'].write('refused before anything was submitted:\n')
            if timeout:raise subprocess.TimeoutExpired(['fake-cli'],420)
            return SimpleNamespace(returncode=cli_exit)
        with patch.object(guarded,'Remote',side_effect=remote_factory), \
             patch.object(guarded,'dependency_preflight',side_effect=dependency_error,
                          return_value={'deploymentHashes':{}}), \
             patch.object(guarded,'join_hosts',side_effect=joins), \
             patch('tools.liveconsole.profile.load_live_deployment',return_value=object()), \
             patch.object(guarded,'control_header_rows',return_value=HEADER_ROWS), \
             patch.object(guarded,'write_control_headers',return_value={}), \
             patch.object(guarded,'ControlHeaderRefresher'), \
             patch.object(guarded,'kpm_dependencies',side_effect=kpm), \
             patch('subprocess.run',side_effect=cli),patch('sys.stdout',new_callable=io.StringIO):
            code=guarded.run_attempt(self.tmp/'base.json',output_dir=self.tmp)
        roots=list(self.tmp.glob('formal38guarded-*'))
        root=max(roots,key=lambda path:path.stat().st_mtime_ns)
        return code,json.loads((root/'exit.json').read_text()),calls,root

    def test_cli_zero_started_episode_is_not_reported_as_ota_completion(self):
        code,report,calls,root=self.attempt()
        self.assertEqual(code,0)
        self.assertEqual(report['submissionStatus'],'STARTED_EPISODE')
        self.assertFalse(report['otaCompletionVerified'])
        self.assertEqual(set(report['cleanup']),{'ue1','ue2','ue3','extdn'})
        manifest=json.loads((root/'manifest.json').read_text())
        self.assertEqual(manifest['initialAssociation'],{'903':12345678,'902':12345678,'901':87654321})
        self.assertEqual(manifest['offeredLoad']['dlMbpsPerUe'],1)

    def test_a_sample_that_ages_past_the_bound_before_submit_is_read_again(self):
        code,report,calls,root=self.attempt(expiring_samples=1)
        self.assertEqual(report['submissionStatus'],'STARTED_EPISODE')
        slot=next(row[2] for row in calls if row[1]=='sample' and row[2].startswith('ue1'))
        self.assertEqual(sum(1 for row in calls if row[1]=='sample' and row[2]==slot),2)

    def test_a_slow_reply_does_not_age_a_fresh_sample(self):
        # 2026-09-15 attempt 36: the time before the remote stamped remoteNowMs was
        # counted on top of the sample's own age, so a 0.4 s-old sample read over a
        # slow round trip expired on every re-read.
        code,report,calls,root=self.attempt(slow_sample_s=1.2)
        self.assertEqual(report['submissionStatus'],'STARTED_EPISODE')

    def test_a_sample_that_stays_past_the_bound_is_still_refused(self):
        code,report,calls,root=self.attempt(expiring_samples=99)
        self.assertEqual(code,3)
        self.assertEqual(report['failure']['code'],'SOURCE_SAMPLE_EXPIRED_BEFORE_SUBMIT:ue1-data')
        self.assertFalse(report['cliInvoked'])

    def test_authentication_hold_precedes_credentials_dependencies_and_source_launch(self):
        import tools.liveconsole.profile  # Load mock targets under the fake-only fixture env.
        guarded.AUTHENTICATION_HOLD.write_text('{"testHold":true}')
        with patch.object(os.environ,'get',side_effect=AssertionError('must not read credentials under hold')):
            code,report,calls,_=self.attempt()
        self.assertEqual(code,3);self.assertEqual(calls,[])
        self.assertEqual(report['failure']['code'],'LIVE_MODEL_AUTHENTICATION_ON_HOLD')
        self.assertFalse(report['cliInvoked'])
        self.assertTrue(guarded.AUTHENTICATION_HOLD.is_file())

    def test_dependency_failure_precedes_any_source_or_host_launch(self):
        code,report,calls,_=self.attempt(dependency_error=guarded.Refused('FRESH_TWO_CELL_KPM_REQUIRED'))
        self.assertEqual(code,3);self.assertEqual(calls,[])
        self.assertEqual(report['submissionStatus'],'REFUSED_BEFORE_SUBMISSION')
        self.assertFalse(report['cliInvoked'])

    def test_lost_partial_start_response_is_in_finally_cleanup(self):
        code,report,calls,_=self.attempt(remote_failure=lambda endpoint,action,spec:
                                       endpoint=='extdn' and action=='start')
        self.assertNotEqual(code,0)
        # Parallel source pairs may also have launched another host; every attempted one is cleaned.
        self.assertLessEqual({'ue1','extdn'},set(report['cleanup']))
        self.assertIn(('extdn','cleanup',None),calls)
        self.assertFalse(report['cliInvoked'])

    def test_presubmit_amf_change_refuses_without_rebinding_frozen_policy(self):
        code,report,_,root=self.attempt(mapping_changed=True)
        self.assertEqual(code,3);self.assertFalse(report['cliInvoked'])
        self.assertEqual(json.loads((root/'intents.json').read_text())['intents'][0]['ueId'],'ue1')
        self.assertEqual(report['failure']['code'],'AMF_IDENTITY_CHANGED_BEFORE_SUBMIT')

    def test_tun_identity_change_between_launches_stops_this_attempt(self):
        code,report,calls,_=self.attempt(identity_changed=True)
        self.assertEqual(code,3);self.assertFalse(report['cliInvoked'])
        self.assertEqual(report['failure']['code'],'TUN_IDENTITY_CHANGED')
        # Pairs start in parallel, so either host may launch first; none may launch after the change.
        starts=[row for row in calls if row[1]=='start']
        self.assertEqual(len(starts),1)
        self.assertEqual(set(report['cleanup']),{starts[0][0]})

    def test_stale_presubmit_source_refuses_and_cleans_all_partial_work(self):
        code,report,_,_=self.attempt(stale_source=True)
        self.assertEqual(code,3);self.assertFalse(report['cliInvoked'])
        self.assertEqual(report['failure']['code'],'SOURCE_SNAPSHOT_STALE')
        self.assertEqual(len(report['cleanup']),4)

    def test_source_archive_failure_is_not_hidden_by_cli_zero(self):
        code,report,_,_=self.attempt(archive_failure=True)
        self.assertEqual(code,74);self.assertEqual(report['cliExit'],0)
        self.assertFalse(report['sourceArchive']['ue1']['complete'])

    def test_nonzero_cli_exit_propagates(self):
        code,report,_,_=self.attempt(cli_exit=7)
        self.assertEqual(code,7);self.assertEqual(report['cliExit'],7);self.assertEqual(report['outerExit'],7)

    def test_cli_timeout_records_started_episode_and_requires_reconciliation(self):
        code,report,_,_=self.attempt(timeout=True)
        self.assertEqual(code,124);self.assertTrue(report['cliTimedOut'])
        self.assertEqual(report['submissionStatus'],'STARTED_EPISODE')
        self.assertTrue(report['reconciliationRequired']);self.assertEqual(len(report['cleanup']),4)

    def test_no_episode_and_no_refusal_is_unknown_not_preflight(self):
        code,report,_,_=self.attempt(episode=False)
        self.assertEqual(code,75)
        self.assertEqual(report['submissionStatus'],'SUBMISSION_UNKNOWN_RECONCILIATION_REQUIRED')
        self.assertTrue(report['reconciliationRequired'])

    def test_framework_explicit_preflight_refusal_is_not_started(self):
        code,report,_,_=self.attempt(cli_exit=3,episode=False,refusal_marker=True)
        self.assertEqual(code,3)
        self.assertEqual(report['submissionStatus'],'FRAMEWORK_REFUSED_BEFORE_SUBMISSION')

    def test_cleanup_failure_prevents_successful_outer_exit(self):
        code,report,_,_=self.attempt(cleanup_failure=True)
        self.assertEqual(code,70);self.assertEqual(report['cliExit'],0)
        self.assertTrue(report['reconciliationRequired'])

    def test_every_attempt_uses_a_new_session_and_does_not_overwrite_prior_run(self):
        _,one,_,root1=self.attempt(dependency_error=guarded.Refused('NOT_READY'))
        contents=(root1/'exit.json').read_bytes()
        _,two,_,root2=self.attempt(dependency_error=guarded.Refused('NOT_READY'))
        self.assertNotEqual(root1,root2);self.assertNotEqual(one['sessionId'],two['sessionId'])
        self.assertEqual((root1/'exit.json').read_bytes(),contents)


class PreferenceProfileTests(Hermetic):
    """Section 3 of the integrated reply, proven on the pinned pilot's board.

    The audit's finding: ``AIC_PREFERENCE`` was READ by the sitting and SET by
    nothing in the tree, so every stored episode ranked under the bare default
    rule while six cases carried P1/P2/P3 labels. A test that a flag parses
    would not have caught that, so these build the pilot's authorization under
    each profile and compare the boards that actually reach a contract.
    """

    def board(self, name):
        """``(rule, target ids, level vectors)`` of the pilot's ranked ``T``."""
        from assurance.coordination.tc import Intent, Authorization, expand_targets
        from tools.liveconsole.agent import _preference_for
        intents = tuple(Intent.from_record(row)
                        for row in guarded.pilot_intents(MAPPING)['intents'])
        preference = _preference_for(intents, name)
        contract = expand_targets(
            Authorization.from_intents(intents, preference=preference))
        ranked = contract.ranked()
        return (preference.rule,
                tuple(target.target_id for target in ranked),
                tuple(tuple(sorted(target.levels.items())) for target in ranked))

    def first_alternative(self, name):
        """What the profile asks the owners to concede first, after ``T0``."""
        from assurance.coordination.tc import (Intent, Authorization, expand_targets,
                                               concession_of)
        from tools.liveconsole.agent import _preference_for
        intents = tuple(Intent.from_record(row)
                        for row in guarded.pilot_intents(MAPPING)['intents'])
        authorization = Authorization.from_intents(
            intents, preference=_preference_for(intents, name))
        ranked = expand_targets(authorization).ranked()
        return {owner: round(float(value), 3) for owner, value in
                concession_of(ranked[1], authorization)['perOwner'].items()}

    def test_each_profile_delivers_its_own_ranking_to_the_contract(self):
        default, p1, p2, p3 = (self.board(name)
                               for name in (None, 'P1', 'P2', 'P3'))
        # P3 is today's default ranking under its own name, so naming it may
        # not move a single target -- that is what makes it the safe label.
        self.assertEqual(default[2], p3[2])
        self.assertEqual('lexicographic(D_max, D_mean)', default[0])
        self.assertEqual('P3: lexicographic(D_max, D_mean)', p3[0])
        # P1 and P2 are genuinely different boards, and differ from each other.
        self.assertNotEqual(default[2], p1[2])
        self.assertNotEqual(default[2], p2[2])
        self.assertNotEqual(p1[2], p2[2])
        self.assertEqual('P1: lexicographic(D_ue1-video, D_ue2-map, D_ue3-incumbent)',
                         p1[0])
        self.assertEqual('P2: lexicographic(D_ue2-map, D_ue3-incumbent, D_ue1-video)',
                         p2[0])

    def test_target_ids_are_rank_labels_and_show_no_difference_at_all(self):
        # tc.py::expand_targets numbers the alternatives T1..Tn *after* sorting
        # them by the preference, so the id sequence is identical under every
        # profile. Comparing ids would "prove" the profiles are all the same
        # board; the levels at each rank are what actually moves.
        self.assertEqual(1, len({self.board(name)[1]
                                 for name in (None, 'P1', 'P2', 'P3')}))

    def test_the_profile_decides_which_owner_yields_first(self):
        # Default and P1 both lead with an owner the board may relax cheaply:
        # ue3-incumbent concedes a quarter and nobody else concedes anything.
        for name in (None, 'P1', 'P3'):
            with self.subTest(profile=name):
                self.assertEqual({'ue1-video': 0.0, 'ue2-map': 0.0,
                                  'ue3-incumbent': 0.25},
                                 self.first_alternative(name))
        # P2 ranks ue2-map first, so the owner asked to yield first is the one
        # it ranks LAST: ue1-video gives up half of its goodput before
        # ue3-incumbent is asked for anything. A campaign that labelled a case
        # P2 and ran the default charged the wrong owner for every repair.
        self.assertEqual({'ue1-video': 0.5, 'ue2-map': 0.0, 'ue3-incumbent': 0.0},
                         self.first_alternative('P2'))

    def test_absence_is_the_default_rule_and_a_profile_passes_through(self):
        self.assertIsNone(guarded.preference_profile())
        for name in guarded.PREFERENCE_PROFILES:
            with self.subTest(profile=name), patch.dict(os.environ,
                                                        {'AIC_PREFERENCE':name}):
                self.assertEqual(name, guarded.preference_profile())

    def test_an_unknown_profile_is_refused_not_ranked_by_the_default(self):
        # 'C4' is a case label, 'p1' a typo: both used to rank under the bare
        # default and record nothing about having done so.
        for name in ('P4','p1','C4','lexicographic(D_max, D_mean)'):
            with self.subTest(name=name), patch.dict(os.environ,
                                                     {'AIC_PREFERENCE':name}), \
                 self.assertRaisesRegex(guarded.Refused,
                                        'UNKNOWN_PREFERENCE_PROFILE:P1,P2,P3'):
                guarded.preference_profile()

    def test_the_cli_option_sets_the_variable_the_child_inherits(self):
        seen = {}
        def attempt(*args, **kwargs):
            seen['profile'] = os.environ.get('AIC_PREFERENCE')
            return 0
        with patch.object(guarded,'run_attempt',side_effect=attempt):
            self.assertEqual(0, guarded.main(['--preference','P2']))
        self.assertEqual('P2', seen['profile'])
        # No flag and no variable: unset, so the default rule still applies.
        with patch.dict(os.environ), patch.object(guarded,'run_attempt',
                                                  side_effect=attempt):
            os.environ.pop('AIC_PREFERENCE',None)
            self.assertEqual(0, guarded.main([]))
        self.assertIsNone(seen['profile'])
        # No flag but an exported variable: the passthrough keeps it. This is
        # why --preference may not clear what it did not set -- the six-case
        # matrix can declare the profile either way.
        with patch.dict(os.environ,{'AIC_PREFERENCE':'P1'}), \
             patch.object(guarded,'run_attempt',side_effect=attempt):
            self.assertEqual(0, guarded.main([]))
        self.assertEqual('P1', seen['profile'])

    def test_an_unknown_cli_profile_exits_naming_the_accepted_values(self):
        with patch.object(guarded,'run_attempt',
                          side_effect=AssertionError('must not run')), \
             patch('sys.stderr',new_callable=io.StringIO) as stderr, \
             self.assertRaises(SystemExit):
            guarded.main(['--preference','C4'])
        for name in guarded.PREFERENCE_PROFILES:
            self.assertIn(name, stderr.getvalue())

    def test_the_profile_is_not_an_argv_flag_of_the_child(self):
        with patch.dict(os.environ,{'AIC_PREFERENCE':'P2'}):
            argv = guarded.command_for(self.tmp,MAPPING)
        self.assertNotIn('--preference', argv)
        self.assertNotIn('P2', argv)


if __name__=='__main__':
    unittest.main()


class ACommandRefusalLeavesWhatItSaw(unittest.TestCase):
    def test_the_observed_argv_is_archived_beside_the_slot_log(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            where = {'log': Path(tmp)/'ue1-tx.jsonl'}
            helper.record_refusal(where, 'SOURCE_COMMAND_CHANGED',
                                  {'pid': os.getpid(), 'ppid': os.getppid(), 'argv': []})
            row = json.loads((Path(tmp)/'ue1-tx.refusal.jsonl').read_text())
        self.assertEqual((row['code'], row['argvLength'], row['argv']),
                         ('SOURCE_COMMAND_CHANGED', 0, []))
        self.assertIsNotNone(row['state'])


class AnExecInProgressIsNotACommandChange(unittest.TestCase):
    """2026-09-15 attempt 65: inspect read the pinned source mid-execv."""

    def _source_for(self, argv):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            where = {'source_owner': Path(tmp)/'s.source-owner.json', 'log': Path(tmp)/'s.jsonl'}
            pinned = {'pid': 7, 'startTicks': 1, 'pgid': 7, 'bootId': 'b', 'uid': 0, 'ppid': 5}
            where['source_owner'].write_text(json.dumps(pinned))
            claim = {'sourceArgv': ['python3', 'tagged_echo.py'], 'sourceBootstrapArgv': ['python3', '-c', 'x']}
            with patch.object(helper, 'proc_identity', return_value={**pinned, 'argv': argv}), \
                 patch.object(helper, 'safe_file'):
                return helper.source_for(claim, where, {'pid': 5})

    def test_an_empty_cmdline_on_the_pinned_process_is_not_ready_yet(self):
        self.assertIsNone(self._source_for([]))

    def test_the_exact_source_argv_is_still_required_to_be_ready(self):
        self.assertEqual(self._source_for(['python3', 'tagged_echo.py'])['pid'], 7)
        with self.assertRaisesRegex(helper.GuardError, 'SOURCE_COMMAND_CHANGED'):
            self._source_for(['python3', 'other.py'])


class TheSourceStartReportsTheCauseNotTheCancellation(unittest.TestCase):
    def test_a_later_pair_failure_outranks_an_earlier_cancellation(self):
        with self.assertRaisesRegex(guarded.Refused, '^SOURCE_COMMAND_CHANGED:extdn:inspect$'):
            guarded.raise_root_cause([guarded.Refused('SOURCE_START_CANCELLED'), None,
                                      guarded.Refused('SOURCE_COMMAND_CHANGED:extdn:inspect')])

    def test_no_error_raises_nothing(self):
        guarded.raise_root_cause([None, None])


class TheModelsAreToldTheRealEnvironment(unittest.TestCase):
    """The condition carries the bed's numbers, not the module defaults.

    ``tools/liveconsole/agent.py::_predictor_state`` falls back to 4.0 Mbps
    offered, 5.0 Mbps cells and 24 PRB for anything the condition omits.  Passing
    only ``name=`` therefore described a deployment no candidate could satisfy,
    and every Control answer across five episodes came back ``predictedTarget:
    "none"`` -- an artefact of the input, not a measurement.
    """

    def conditions(self, **env):
        from unittest import mock
        with mock.patch.dict(os.environ, env, clear=False):
            argv = guarded.command_for(Path(tempfile.mkdtemp()), MAPPING)
        pairs = {}
        for index, value in enumerate(argv):
            if value == '--condition':
                key, _, val = argv[index + 1].partition('=')
                pairs[key] = val
        return pairs

    def test_the_offered_load_reaching_the_model_is_the_one_driving_the_senders(self):
        self.assertEqual('10', self.conditions(AIC_OFFERED_LOAD_MBPS='10')
                         .get('offeredLoadMbps'),
                         'the model must be told the load the senders actually run')

    def test_the_prb_total_is_the_deployed_one_not_the_24_prb_default(self):
        self.assertEqual('38', self.conditions().get('prbTotal'))

    def test_the_cell_capacity_is_stated_rather_than_defaulted_to_five(self):
        got = self.conditions()
        self.assertIn('cellCapacityMbps', got)
        self.assertGreater(float(got['cellCapacityMbps']), 5.0,
                           'the measured point is 14.5; 5.0 is the module default')

    def test_every_environment_value_stays_overridable(self):
        got = self.conditions(AIC_OFFERED_LOAD_MBPS='6', AIC_CELL_CAPACITY_MBPS='12',
                              AIC_PRB_TOTAL='51')
        self.assertEqual(('6', '12', '51'),
                         (got.get('offeredLoadMbps'), got.get('cellCapacityMbps'),
                          got.get('prbTotal')))

    def test_the_condition_name_is_still_carried(self):
        self.assertIn('name', self.conditions())


class TheRevisedCaseIsWhatTheRevisionStates(unittest.TestCase):
    """``v3-select10-existing3``: the frozen snapshot and the action scope.

    The revision of 2026-09-14 changes three things about a run's definition --
    the deadline levels, the action scope and the catalogue ceiling -- and each
    one is the kind of change that has already been made by hand and forgotten.
    The steering-only scope of the five-episode report was exactly that: the
    runner's own default is ``servingCell,dlPrbCap`` and it was overridden in a
    launch script, so the narrowing left no trace in any artefact.
    """

    PILOT = (Path(__file__).resolve().parents[1] / 'experiment_results' /
             'ota-20260911' / 'pilot38-v3-existing3-20260914T2300')

    def flags(self, **env):
        from unittest import mock
        with mock.patch.dict(os.environ, env, clear=False):
            return guarded.command_for(Path(tempfile.mkdtemp()), MAPPING)

    def values_for(self, argv, flag):
        return [argv[i + 1] for i, value in enumerate(argv) if value == flag]

    def test_the_revised_pilot_states_the_two_and_three_second_deadlines(self):
        # Section 6: "Do not inherit the unexplained 1,000/1,500-ms values from
        # the latest corpus."  d=0 is 2000 ms everywhere; d=1 is 3000 ms only
        # where that owner is authorized to reach it, which is unchanged.
        rows = json.loads((self.PILOT / 'intents.json').read_text())['intents']
        self.assertEqual(
            [(2000, 2000), (2000, 3000), (2000, 3000)],
            [(row['requirement']['deadlineMs'], row['requirement']['deadlineBound'])
             for row in rows if row['requirement']['kpi'] == 'deadlineSuccessRatio'])

    def test_the_revised_pilot_keeps_the_same_owner_authority(self):
        # Only the deadline levels move.  The 54-member authority, the owner
        # tables and the goodput levels are the earlier pilot's, byte for byte
        # in answers.json and value for value here -- a revision that silently
        # also moved the authority would make the two blocks incomparable.
        old = (self.PILOT.parent / 'pilot38-v3-select10-20260914T1530')
        self.assertEqual((old / 'answers.json').read_bytes(),
                         (self.PILOT / 'answers.json').read_bytes())
        for path in (old, self.PILOT):
            document = json.loads((path / 'intents.json').read_text())
            self.assertEqual('3*4*5 - 6 = 54',
                             document['domain']['jointPermission'].split('|Omega| = ')[1])
            self.assertEqual([9.0, 0.9, 9.0, 0.9, 9.0, 0.9],
                             [row['requirement']['value'] for row in document['intents']])

    def test_all_three_existing_families_reach_the_argv(self):
        argv = self.flags(AIC_AXES='servingCell,dlPrbCap,pfWeight',
                          AIC_CAP_HOSTS='ue1:18,12,6;ue2:18,12,6;ue3:18,12,6',
                          AIC_PF_HOSTS='ue1:1,4;ue2:1,4;ue3:1,4')
        self.assertEqual(['servingCell,dlPrbCap,pfWeight'], self.values_for(argv, '--axes'))
        self.assertEqual(3, len(self.values_for(argv, '--cap-axis')))
        self.assertEqual(3, len(self.values_for(argv, '--pf-axis')))
        for spec in self.values_for(argv, '--cap-axis'):
            self.assertTrue(spec.endswith(':18,12,6'), spec)
        for spec in self.values_for(argv, '--pf-axis'):
            self.assertTrue(spec.endswith(':1,4'), spec)

    def test_an_empty_cap_host_list_still_means_no_cap_axis(self):
        # The steering-only narrowing that produced the five-episode report.
        # It stays expressible -- a calibration probe needs it -- but it is a
        # stated choice, and this pins that set-but-empty is not "the default".
        self.assertEqual([], self.values_for(self.flags(AIC_CAP_HOSTS=''), '--cap-axis'))

    def test_the_catalogue_ceiling_rises_with_the_scope(self):
        # 512 is below the count the three declared families admit, so leaving
        # it would refuse the epoch -- a ceiling silently deciding the scope.
        self.assertEqual(['512'], self.values_for(self.flags(), '--max-catalog'))
        # 4096 is the freeze's own count for the three declared families
        # ("steer 8 x cap 64 x pf 8").  It is the CATALOGUE, not the admissible
        # configuration count: the per-UE dlPrbCap/pfWeight exclusion and
        # MAX_CHANGED_ENTRIES = 4 both exist and cut that to 1,000 and then 696
        # one layer down, at candidate enumeration.  Raising the ceiling to the
        # catalogue's real size therefore narrows nothing.
        self.assertEqual(['4096'],
                         self.values_for(self.flags(AIC_MAX_CATALOG='4096'), '--max-catalog'))


class TheControlHeadersFollowThePinnedUes(unittest.TestCase):
    """The action producer fires a cap/PF control only when ``<host>-hdr.env``
    names the UE's current amfUeNgapId and ran_ue_id.  Stale headers made every
    cap/PF policy BOUND-but-never-applied on 2026-09-14.  Synthetic KPM lines;
    no radio, no producer."""

    NODE1 = 'ngran=02;plmn=208-095-2;nb=0000003584/00;cudu=none:00000000000000000000'
    NODE2 = 'ngran=02;plmn=208-095-2;nb=0000002816/00;cudu=none:00000000000000000000'
    GUAMI = {'mcc': 208, 'mnc': 95, 'mnc_digit_len': 2, 'amf_region_id': 1,
             'amf_set_id': 64, 'amf_pointer': 4}

    def line(self, node, nb, epoch, age_ms, ues):
        now_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
        return json.dumps({'event': 'kpm_indication', 'e2_node': node, 'nb_id': nb,
                           'connection_epoch': epoch, 'recv_unix_us': now_us - age_ms * 1000,
                           'ues': [{'amf_ue_ngap_id': a, 'ran_ue_id': r, 'guami': self.GUAMI}
                                   for a, r in ues]})

    def rows(self, lines, mapping):
        topology = SimpleNamespace(nb_id_to_nci={3584: 12345678, 2816: 87654321},
                                   expected_epochs={self.NODE1: 765, self.NODE2: 758})
        tail = Mock(); tail.return_value.read_new_lines.return_value = lines
        with patch('tools.g3ota.composition.live_topology', return_value=topology), \
             patch('tools.g3ota.composition.KpmTail', tail):
            return guarded.control_header_rows(
                SimpleNamespace(binding=None, capability=None, kpm_jsonl_path='x'), mapping)

    def test_each_pinned_ue_gets_its_own_current_identity(self):
        rows = self.rows([self.line(self.NODE1, 3584, 765, 100, [(2116, 2), (2123, 1)]),
                          self.line(self.NODE2, 2816, 758, 100, [(2125, 1)])],
                         {'2116': 'ue1', '2125': 'ue2', '2123': 'ue3'})
        self.assertEqual({'ue1': (2116, 2, 3584), 'ue2': (2125, 1, 2816), 'ue3': (2123, 1, 3584)},
                         {h: (r['RC_HEADER_AMF_UE_NGAP_ID'], r['RC_HEADER_RRC_UE_ID'], r['nbId'])
                          for h, r in rows.items()})

    def test_after_a_handover_the_newer_node_wins(self):
        # ue3 kept amf 2123 but moved to gnb2, where its ran_ue_id is 2.
        rows = self.rows([self.line(self.NODE2, 2816, 758, 200, [(2123, 2)]),
                          self.line(self.NODE1, 3584, 765, 900, [(2123, 1)])],
                         {'2123': 'ue3'})
        self.assertEqual((2816, 2), (rows['ue3']['nbId'], rows['ue3']['RC_HEADER_RRC_UE_ID']))

    def test_stale_or_wrong_epoch_indications_are_ignored(self):
        rows = self.rows([self.line(self.NODE1, 3584, 999, 100, [(2116, 2)]),
                          self.line(self.NODE1, 3584, 765, 60_000, [(2116, 2)])],
                         {'2116': 'ue1'})
        self.assertEqual({}, rows)

    def test_writes_are_atomic_idempotent_and_in_the_workers_format(self):
        from oran.campaign5.live_worker import _HEADER_KEYS
        rows = self.rows([self.line(self.NODE1, 3584, 765, 100, [(2116, 2)])], {'2116': 'ue1'})
        directory = Path(tempfile.mkdtemp())
        self.assertEqual(['ue1'], sorted(guarded.write_control_headers(rows, directory)))
        self.assertEqual({}, guarded.write_control_headers(rows, directory),
                         'an unchanged header is not rewritten')
        body = (directory / 'ue1-hdr.env').read_text().splitlines()
        self.assertEqual(set(_HEADER_KEYS), {line.split('=', 1)[0] for line in body})
        self.assertEqual([], [p.name for p in directory.iterdir() if p.name.startswith('.')])


class _HelperGuardError(Exception):
    """Stands in for the helper's own GuardError, whose messages are already tokens."""


class AnExceptionClassIsEvidenceAndMustSurviveTheSanitiser(unittest.TestCase):
    """Eight attempts died as bare ``REMOTE_REFUSED:<host>:sample`` (88, 92, 93, 104,
    107, 115, 125, 137) with no reason attached.  The remote helper had one: it sends
    ``str(exc)`` for its own ``GuardError`` and the exception's class name otherwise.
    The caller keeps an error only when it looks like a token, so that a remote message
    can never carry a secret into the log -- and ``OSError`` has lowercase letters, so
    the class name was discarded together with the diagnosis.

    The filter stays exactly as strict.  The helper sends a token instead.
    """

    SANITISER = re.compile(r'[A-Z][A-Z0-9_]*(?::[a-z_]+\.py)?')

    def _encode(self, exc):
        """The helper's encoder, read out of the deployed source rather than restated."""
        source = (Path(__file__).resolve().parents[1] / 'experiment_results'
                  / 'ota-20260911' / 'guarded_source_process.py').read_text(encoding='utf-8')
        line = [row.strip() for row in source.splitlines()
                if row.strip().startswith('code=str(exc) if isinstance(exc,GuardError)')]
        self.assertEqual(len(line), 1, 'the helper has one error encoder')

        scope = {'exc': exc, 'GuardError': _HelperGuardError}
        exec(line[0], scope)  # noqa: S102 - the deployed line, run as written
        return scope['code']

    def test_the_caller_still_refuses_anything_that_is_not_a_token(self):
        for unsafe in ('imsi-001010000000001 not found', 'password: hunter2',
                       'no such file: ~ue/secret.key', ''):  # not an absolute host path: the portability guard scans this file as text
            with self.subTest(unsafe=unsafe):
                self.assertFalse(self.SANITISER.fullmatch(unsafe))

    def test_an_exception_class_now_reaches_the_log_as_a_token(self):
        for exc in (OSError('detail that must not travel'), ValueError('x'), KeyError('k'),
                    TimeoutError('y')):
            with self.subTest(exc=type(exc).__name__):
                code = self._encode(exc)
                self.assertTrue(self.SANITISER.fullmatch(code),
                                f'{code} would still be replaced by REMOTE_REFUSED')
                self.assertIn(type(exc).__name__.upper(), code)
                # The message itself never travels; only the class does.
                self.assertNotIn('detail', code)
                self.assertNotIn('that must not travel', code)

    def test_a_guard_error_keeps_its_own_token_unchanged(self):
        self.assertEqual(self._encode(_HelperGuardError('SOURCE_NOT_READY_FOR_SAMPLE')),
                         'SOURCE_NOT_READY_FOR_SAMPLE')


class AMomentaryGapMustNotEraseARoleIdentity(unittest.TestCase):
    """The role identity file has an age bound; erasure bypasses it.

    ``role_identity_resolver`` treats an entry older than
    ``ROLE_IDENTITY_MAX_AGE_S`` as unresolved, which is the mechanism for
    deciding that a UE can no longer be addressed.  ``write_role_identity``
    rebuilt the whole document from the current refresh, so a host that happened
    to be between registrations when the refresh ran was deleted outright rather
    than aged out.  Attempt 159 of 2026-09-16 was refused before submission with
    "UE ue2 has no current amfUeNgapId: the runner's role identity entry is
    absent or stale" while ue1 and ue3 sat in the same file 47 seconds old and
    healthy.
    """

    def setUp(self):
        import tempfile
        self.directory = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.directory, ignore_errors=True)

    def row(self, amf, nb=3584, epoch=824):
        return {'RC_HEADER_AMF_UE_NGAP_ID': amf, 'nbId': nb, 'connectionEpoch': epoch}

    def write(self, rows):
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            'afrg_under_test',
            Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911'
            / 'atomic_formal_run_guarded.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.write_role_identity(rows, Path(self.directory) / 'ue-identity.json')

    def test_every_refreshed_role_is_written(self):
        document = self.write({'ue1': self.row(8), 'ue2': self.row(9), 'ue3': self.row(4, 2816, 822)})
        self.assertEqual({'ue1', 'ue2', 'ue3'}, set(document['roles']))
        self.assertEqual(9, document['roles']['ue2']['amfUeNgapId'])

    def test_a_role_absent_from_one_refresh_keeps_its_entry_and_its_timestamp(self):
        first = self.write({'ue1': self.row(8), 'ue2': self.row(9)})
        stamp = first['roles']['ue2']['writtenAtUnix']
        second = self.write({'ue1': self.row(8)})          # ue2 between registrations
        self.assertIn('ue2', second['roles'], 'a gap must age out, not erase')
        self.assertEqual(9, second['roles']['ue2']['amfUeNgapId'])
        self.assertEqual(stamp, second['roles']['ue2']['writtenAtUnix'],
                         'the kept entry must not be freshened; it has to age')

    def test_a_role_that_really_re_registered_is_overwritten(self):
        self.write({'ue2': self.row(9)})
        second = self.write({'ue2': self.row(11)})
        self.assertEqual(11, second['roles']['ue2']['amfUeNgapId'])

    def test_a_refreshed_role_gets_a_new_timestamp(self):
        first = self.write({'ue1': self.row(8)})
        second = self.write({'ue1': self.row(8)})
        self.assertGreaterEqual(second['roles']['ue1']['writtenAtUnix'],
                                first['roles']['ue1']['writtenAtUnix'])

    def test_an_unreadable_previous_document_is_not_fatal(self):
        from pathlib import Path
        Path(self.directory, 'ue-identity.json').write_text('not json')
        document = self.write({'ue1': self.row(8)})
        self.assertEqual({'ue1'}, set(document['roles']))
