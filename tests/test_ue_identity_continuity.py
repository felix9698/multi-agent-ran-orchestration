"""A UE that re-registers mid-sitting is followed, not refused (docs/design/ue-identity-continuity.md).

Hermetic: injected sockets, clocks, interface reads and fake runtimes only.
"""
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.liveconsole import flow_goodput as fg
from tools.liveconsole import tagged_echo as te
from tools.liveconsole import agent as agent_mod
from tools.liveconsole.agent import AgentSitting, role_identity_resolver
from tools.liveconsole.build import joint_serving_attribution
from tools.liveconsole.kpi_observer import LiveFlowGoodputObserver
from tests.test_liveconsole_tagged_echo import COMMON as ECHO, FakeNetwork, IDENTITY as ECHO_IDENTITY
from tests import test_live_flow_goodput as flow
from tests import test_liveconsole_tagged_echo as echo_rows

NEW_IDENTITY = {'name': 'oaitun_ue1', 'ip': '12.1.1.3', 'ifindex': 8, 'up': True}
REPO = Path(__file__).resolve().parents[1]


class TheSinkLogFollowsARebind(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'flow.jsonl'

    def take(self, rows):
        self.path.write_bytes(b''.join(json.dumps(row).encode() + b'\n' for row in rows))
        return fg.snapshot(log_path=self.path, session_id='session-a', flow_id='ue1-data',
                           max_age_ms=1500, clock_id='boot-a', monotonic=lambda: 3.05)

    def rebound_log(self, epoch=1, rx_after=40):
        return [flow.start(), flow.connected(100), flow.heartbeat(1000, payload=100, rx=1000),
                flow.record('rebind-wait', 1200),
                flow.record('rebind', 1500, interface=dict(NEW_IDENTITY), bindEpoch=epoch,
                            bindIp=NEW_IDENTITY['ip']),
                flow.record('heartbeat', 1600, status='waiting', interface=dict(NEW_IDENTITY),
                            payloadBytes=100, tunRxBytes=10),
                flow.record('connected', 1700, peerIp='192.168.70.135', peerPort=40001,
                            handshake=json.loads(fg.make_handshake('session-a', 'ue1-data', 5, 2))),
                flow.record('heartbeat', 3000, status='running', interface=dict(NEW_IDENTITY),
                            payloadBytes=300, tunRxBytes=rx_after)]

    def test_a_rebound_sink_is_a_running_source_on_the_new_tun(self):
        row = self.take(self.rebound_log())
        self.assertEqual(row['bindEpoch'], 1)
        self.assertEqual(row['interface'], NEW_IDENTITY)
        self.assertEqual((row['payloadBytes'], row['tunRxBytes']), (300, 40))
        self.assertEqual(row['connection']['peerPort'], 40001)

    def test_a_counter_reset_or_new_tun_without_a_rebind_is_still_refused(self):
        rows = [flow.start(), flow.connected(100), flow.heartbeat(1000, payload=100, rx=1000),
                flow.heartbeat(2000, payload=200, rx=10)]
        with self.assertRaises(fg.FlowGoodputError):
            self.take(rows)
        rows = [flow.start(), flow.connected(100), flow.heartbeat(1000),
                flow.record('heartbeat', 2000, status='running', interface=dict(NEW_IDENTITY),
                            payloadBytes=200, tunRxBytes=2000)]
        with self.assertRaises(fg.FlowGoodputError):
            self.take(rows)

    def test_a_rebind_must_advance_its_epoch_by_one(self):
        with self.assertRaises(fg.FlowGoodputError):
            self.take(self.rebound_log(epoch=2))


class TheFlowObserverTreatsARebindAsAGap(unittest.TestCase):
    def test_a_higher_bind_epoch_resets_the_baseline_and_is_recorded(self):
        observer = LiveFlowGoodputObserver(hosts={'ue1': 'ue1'}, source_path='/s/flow_goodput.py',
                                           log_path='/l/flow.jsonl', session_id='session-a',
                                           flow_id='ue1-data', monotonic_ms=lambda: 9e12)
        first = flow.snapshot_row(1000, payload=100, rx=1000)
        self.assertIsNotNone(observer._snapshot('ue1', 'ue1', [json.dumps(first)]))
        observer.previous['ue1'] = first
        rebound = flow.snapshot_row(3000, payload=300, rx=40, interface=dict(NEW_IDENTITY), bindEpoch=1,
                                    connection={'peerIp': '192.168.70.135', 'peerPort': 40001,
                                                'connectedAtMs': 1700})
        self.assertIsNotNone(observer._snapshot('ue1', 'ue1', [json.dumps(rebound)]))
        self.assertNotIn('ue1', observer.previous)
        self.assertEqual(observer.failures[-1]['error'], 'source-rebind')
        # the same change without a higher epoch is still a foreign source
        observer2 = LiveFlowGoodputObserver(hosts={'ue1': 'ue1'}, source_path='/s/flow_goodput.py',
                                            log_path='/l/flow.jsonl', session_id='session-a',
                                            flow_id='ue1-data', monotonic_ms=lambda: 9e12)
        observer2._snapshot('ue1', 'ue1', [json.dumps(first)])
        self.assertIsNone(observer2._snapshot('ue1', 'ue1', [json.dumps(dict(rebound, bindEpoch=0))]))


class TheEchoClientRebindsAndVoidsWhatCouldNotBeAnswered(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'echo.jsonl'
        guard = patch.object(te.socket, 'socket', side_effect=AssertionError('real socket forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_the_client_keeps_one_log_across_the_new_tun(self):
        network = FakeNetwork(self.path)
        new = dict(ECHO_IDENTITY, ip='12.1.1.3', ifindex=8)

        def interface(_name):
            if network.now < 1.0:
                return dict(ECHO_IDENTITY)
            if network.now < 2.0:
                raise OSError('tun missing')
            return dict(new)

        te.run_client(server_ip='192.0.2.10', port=5000, session_id=ECHO['sessionId'],
                      flow_id=ECHO['flowId'], log_path=self.path, clock_id=ECHO['clockId'],
                      duration=4.0, rate_hz=4, reply_drain=0.4, socket_factory=network.factory,
                      wait=network.wait, monotonic=network.clock, interface_reader=interface)
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        events = [row['event'] for row in rows]
        self.assertEqual(rows[-1]['status'], 'finished')
        waited = next(row for row in rows if row['event'] == 'rebind-wait')
        rebind = next(row for row in rows if row['event'] == 'rebind')
        self.assertEqual((rebind['bindEpoch'], rebind['interface']), (1, new))
        self.assertTrue(waited['voidedSeqs'])  # nothing replied in the fake: pending ones are voided
        self.assertFalse([row for row in rows if row['event'] == 'issued'
                          and waited['atMs'] < row['atMs'] < rebind['atMs']])
        self.assertIn(('12.1.1.3', 0), network.binds)
        self.assertIn('heartbeat', events[events.index('rebind'):])
        # A snapshot reads a running source: take the log as it stood before its end row.
        running = Path(self.temp.name) / 'running.jsonl'
        running.write_text(''.join(line + '\n' for line in self.path.read_text().splitlines()[:-1]))
        snap = te.snapshot(log_path=running, session_id=ECHO['sessionId'], flow_id=ECHO['flowId'],
                           clock_id=ECHO['clockId'], deadlines_ms=[1000], max_age_ms=1e9,
                           monotonic=lambda: rows[-2]['atMs'] / 1000)
        issued_total = sum(1 for row in rows if row['event'] == 'issued')
        self.assertEqual(snap['bindEpoch'], 1)
        self.assertEqual(snap['voidedRequests'], len(waited['voidedSeqs']))
        self.assertLessEqual(snap['countersByDeadlineMs']['1000']['issued'],
                             issued_total - len(waited['voidedSeqs']))


class ARoleLabelResolvesThroughTheRunnerFile(unittest.TestCase):
    def test_numeric_labels_are_their_own_id_and_roles_read_a_fresh_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'ue-identity.json'
            now = [1000.0]
            resolve = role_identity_resolver({'liveConsole': {'ueIdentityPath': str(path)}},
                                             clock=lambda: now[0], max_age_s=60)
            self.assertEqual(resolve('34'), 34)
            self.assertIsNone(resolve('ue3'))  # no file yet
            path.write_text(json.dumps({'roles': {'ue3': {'amfUeNgapId': 38, 'writtenAtUnix': 990.0}}}))
            self.assertEqual(resolve('ue3'), 38)
            now[0] = 1100.0
            self.assertIsNone(resolve('ue3'))  # stale
        self.assertIsNone(role_identity_resolver({})('ue1'))

    def test_the_serving_attribution_follows_the_resolved_id(self):
        reader = SimpleNamespace(refresh=lambda: None, calls=[])

        def at_or_before(now, *, lookback_ms, amf_ue_ngap_id):
            reader.calls.append(amf_ue_ngap_id)
            return SimpleNamespace(amf_ue_ngap_id=amf_ue_ngap_id, serving_nci=12345678,
                                   e2_node='node', connection_epoch=796)

        reader.at_or_before = at_or_before
        current = [37]
        resolve = joint_serving_attribution(reader=reader, ue_id='ue3', allowed_cells=[12345678],
                                            now=lambda: 't', freshness_bound_ms=4000,
                                            amf_of=lambda: current[0])
        self.assertEqual(resolve().amf_ue_ngap_id, 37)
        current[0] = 38
        self.assertEqual(resolve().amf_ue_ngap_id, 38)
        current[0] = None
        self.assertIsNone(resolve())


class Catalog:
    def __init__(self, spent=()):
        self.entries = [SimpleNamespace(candidate=SimpleNamespace(candidate_id=cid),
                                        availability=SimpleNamespace(
                                            value='CONSUMED' if cid in spent else 'AVAILABLE'))
                        for cid in ('cand-a', 'cand-b')]

    def __call__(self):
        return self.entries


class FakeRuntime:
    def __init__(self, case_id, trials=0, spent=()):
        self.case_id, self._trials, self.catalog_entries = case_id, trials, Catalog(spent)
        self.terminated = False
        self.trials = [{'candidateId': cid} for cid in spent]

    def trials_used(self):
        return self._trials

    # 2026-09-19: the sitting reads single points of the frozen domain.
    def candidate_entry(self, candidate_id):
        return next((e for e in self.catalog_entries() if e.candidate.candidate_id == candidate_id),
                    None)

    def spent_ids(self):
        return {e.candidate.candidate_id for e in self.catalog_entries()
                if e.availability.value != 'AVAILABLE'}

    def terminate(self):
        self.terminated = True
        return SimpleNamespace(value='VECTORS_EXHAUSTED')

    def live_baseline(self):
        return {'pfWeight@ue3': '4.0', 'servingCell@ue3': '12345678', 'pfWeight@ue1': '4.0'}


class TheSittingRebindsBetweenTrials(unittest.TestCase):
    def sitting(self, amf_now):
        sitting = AgentSitting.__new__(AgentSitting)
        observed = SimpleNamespace(amf_ue_ngap_id=37, observed_at='t0', serving_nci=12345678)
        sitting.identities = {'ue3': observed, 'ue1': SimpleNamespace(amf_ue_ngap_id=34, observed_at='t0',
                                                                       serving_nci=12345678)}
        sitting.amf_of = lambda ue: {'ue3': amf_now, 'ue1': 34}[ue]
        sitting.runtime = FakeRuntime('case/x', trials=2, spent=('cand-a',))
        sitting.retention_runtime = None
        sitting.retired_runtimes, sitting.identity_rebinds, sitting.hardware_disconnects = [], [], []
        sitting.baselines = {'pfWeight@ue3': '1.0', 'servingCell@ue3': '12345678', 'pfWeight@ue1': '1.0'}
        sitting.policy_builders, sitting.supplementary = [], ()
        sitting.clock = SimpleNamespace(now=lambda: 'now', t=0.0)
        sitting.clock.monotonic_ms = lambda: sitting.clock.t
        sitting.clock.sleep_ms = lambda ms: setattr(sitting.clock, 't', sitting.clock.t + ms)
        sitting.stopped, sitting.termination, sitting.started_ms = False, None, 0.0
        sitting.composition_wait_ms, sitting.service_trace = 0.0, []
        sitting.calls = []

        sitting.request = SimpleNamespace(budget_trials=8, deadline_ms=None)

        def factory(identities, changed, applied, index, remaining_trials=None):
            sitting.calls.append((sorted(changed), dict(applied), index))
            sitting.remaining = remaining_trials
            fresh = dict(identities, ue3=SimpleNamespace(amf_ue_ngap_id=38, observed_at='t1',
                                                         serving_nci=87654321))
            return {'runtime': FakeRuntime('case/x/rebind-1'), 'identities': fresh, 'freed': {'ue3': []}}

        sitting.rebind_factory = factory
        return sitting

    def test_no_change_no_rebind(self):
        sitting = self.sitting(37)
        sitting._rebind_if_reregistered()
        self.assertFalse(sitting.calls)
        self.assertEqual(sitting.case_id, 'case/x')

    def test_a_re_registered_ue_opens_a_new_case_that_keeps_the_count_and_what_was_spent(self):
        sitting = self.sitting(38)
        old = sitting.runtime
        sitting._rebind_if_reregistered()
        changed, applied, index = sitting.calls[0]
        self.assertEqual((changed, index), (['ue3'], 1))
        self.assertEqual(sitting.remaining, 6)  # the new case holds what the sitting has left
        # the re-registered UE's axes are back at baseline; the other UE's stay applied
        self.assertEqual(applied['pfWeight@ue3'], '1.0')
        self.assertEqual(applied['pfWeight@ue1'], '4.0')
        self.assertTrue(old.terminated)
        self.assertEqual(sitting.retired_runtimes, [old])
        self.assertEqual(sitting.identities['ue3'].amf_ue_ngap_id, 38)
        self.assertEqual(sitting.case_id, 'case/x')  # the episode keeps its first case id
        self.assertEqual(sitting._trials_used_total(), 2)
        self.assertFalse(sitting._available('cand-a'))
        self.assertTrue(sitting._available('cand-b'))
        record = sitting.identity_rebinds[0]
        self.assertEqual(record['outcome'], 'REBOUND')
        self.assertEqual(record['ues']['ue3']['previousAmfUeNgapId'], 37)
        self.assertEqual(record['ues']['ue3']['amfUeNgapId'], 38)

    def test_a_failed_rebind_is_recorded_and_leaves_the_case(self):
        # 2026-09-23: a failed rebind is retried as a hardware wait; if it never succeeds
        # the sitting stops (HARDWARE_UNAVAILABLE) instead of firing trials on the old
        # case at an id the network no longer gives.
        sitting = self.sitting(38)

        def broken(*_args, **_kwargs):
            raise RuntimeError('not fresh')

        sitting.rebind_factory = broken
        old = sitting.runtime
        self.assertFalse(sitting._rebind_if_reregistered())
        self.assertIs(sitting.runtime, old)
        self.assertEqual(sitting.identity_rebinds[0]['outcome'], 'NOT_REBOUND')
        self.assertEqual('HARDWARE_UNAVAILABLE', sitting.termination)


class ARoleNeverReachesAPolicyBody(unittest.TestCase):
    def test_without_a_resolver_the_case_identity_is_named_and_without_one_it_refuses(self):
        from assurance.objectives.action102_support import CAP_ACTION_ID, supplementary_action
        from oran.campaign5.families import CAMPAIGN5_FAMILIES
        from tools.liveconsole.build import (
            CapReadbackAttribution, SupplementaryCapError, controlled_scope_builder)
        family, declared = CAMPAIGN5_FAMILIES['cap'], supplementary_action(CAP_ACTION_ID)
        validity = lambda _command: {'notBefore': '2026-09-16T00:00:00Z', 'notAfter': '2026-09-16T01:00:00Z'}
        command = {'operation': 'APPLY', 'transactionId': 'tx', 'trialId': 't', 'fencingToken': 2,
                   'commandSequence': 1, 'commandIndex': 1, 'idempotencyKey': 'k',
                   'axis': 'dlPrbCap@ue1', 'value': '6',
                   'scope': {'controlledUe@ue1': {'ueId': 'ue1', 'cellId': '12345678'}}}
        expected = CapReadbackAttribution(amf_ue_ngap_id=34, serving_nci=12345678, e2_node='n',
                                          connection_epoch=1)
        body = controlled_scope_builder(family, declared, validity_provider=validity,
                                        controlled_scope_key='controlledUe@ue1',
                                        expected=expected)(command)
        self.assertEqual(body['config']['ueId'], '34')
        with self.assertRaises(SupplementaryCapError):
            controlled_scope_builder(family, declared, validity_provider=validity,
                                     controlled_scope_key='controlledUe@ue1')(command)


class DisconnectsAreCountedForTheFootnote(unittest.TestCase):
    def test_counts_wait_and_the_trial_that_preceded_it(self):
        from experiments.agent_metrics import hardware_disconnects
        episodes = [{'episodeId': 'a', 'hardwareDisconnects': [
                        {'ues': {'ue2': {}}, 'reregistered': True, 'waitedMs': 4000.0,
                         'trials': [{'terminalState': 'INCIDENT_LOCKDOWN'}]},
                        {'ues': {'ue3': {}}, 'reregistered': False, 'waitedMs': 1000, 'trials': []}]},
                    {'episodeId': 'b'}]
        summary = hardware_disconnects(episodes)
        self.assertEqual((summary['episodes'], summary['count'], summary['reregistered'],
                          summary['notReregistered'], summary['waitedMsTotal'], summary['afterTrial']),
                         (1, 2, 1, 1, 5000.0, 1))
        self.assertEqual(summary['events'][0]['trialStates'], ['INCIDENT_LOCKDOWN'])
        self.assertEqual(hardware_disconnects([{'episodeId': 'c'}])['count'], 0)


class TheRunnersRoleProfileComposesTheLiveObservers(unittest.TestCase):
    """LIVE-only path (MOCK skips it): the runner's role-keyed profile reaches every source."""

    def test_hosts_flow_and_echo_sources_are_keyed_by_role(self):
        from tools.liveconsole.agent import AgentRequest, parse_agent_intents
        from tools.liveconsole.build import live_capable_families
        from tools.liveconsole.kpi_observer import (
            CompositeKpiObserver, build_profile_observer, live_echo_sources, live_flow_sources,
            resolve_ue_hosts)
        runner = _runner()
        identities = {host: dict(name='oaitun_ue1', ip=f'12.1.1.{n}', ifindex=5, up=True,
                                 bootId='00000000-0000-0000-0000-000000000000')
                      for n, host in enumerate(runner.HOSTS, 2)}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = {'liveConsole': {'assuranceBindingPath': 'b.json', 'producerDatabasePath': 'p.sqlite'}}
            profile = runner.make_profile(base, root / 'base.json', root / 'attempt',
                                          {'34': 'ue1', '42': 'ue2', '47': 'ue3'},
                                          runner.specs_for(identities, 6301), 'session-x')
            intents = runner.pilot_intents({'1': 'ue1', '2': 'ue2', '3': 'ue3'})['intents']
        self.assertEqual({'ue1', 'ue2', 'ue3'}, {record['ueId'] for record in intents})
        rows = parse_agent_intents(AgentRequest(intents=tuple(intents)),
                                   families=live_capable_families())
        keys = [row.intent.requirement.observation_key for row in rows]
        self.assertEqual({'ue1', 'ue2', 'ue3'}, {key.partition('@')[2] for key in keys})
        hosts = resolve_ue_hosts(['ue1', 'ue2', 'ue3'], profile_document=profile, env={})
        self.assertEqual({'ue1': 'ue1', 'ue2': 'ue2', 'ue3': 'ue3'}, hosts)
        self.assertEqual({'ue1', 'ue2', 'ue3'},
                         set(live_flow_sources(keys, profile_document=profile, hosts=hosts)))
        self.assertEqual({'ue1', 'ue2', 'ue3'},
                         set(live_echo_sources(keys, profile_document=profile, hosts=hosts)))
        observer = build_profile_observer([row.intent.requirement for row in rows],
                                          profile_document=profile, hosts=hosts,
                                          serving_cells=lambda: {})
        self.assertIsInstance(observer, CompositeKpiObserver)
        self.assertTrue(observer.observers)


class TheEchoGapExcusesOnlyWhatItInterrupted(unittest.TestCase):
    """Codex review 2026-09-16: voiding every pending request erased real misses."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'echo.jsonl'

    def take(self, rows, *, at_ms, deadlines=(1000,)):
        self.path.write_bytes(b''.join(json.dumps(row).encode() + b'\n' for row in rows))
        return te.snapshot(log_path=self.path, session_id=echo_rows.COMMON['sessionId'],
                           flow_id=echo_rows.COMMON['flowId'], deadlines_ms=list(deadlines),
                           max_age_ms=1500, clock_id=echo_rows.COMMON['clockId'],
                           monotonic=lambda: at_ms / 1000)

    def gap_log(self):
        new = dict(echo_rows.IDENTITY, ip='12.1.1.3', ifindex=8)
        return [echo_rows.start(),
                echo_rows.issue(0, 100),                        # answered in time
                echo_rows.reply(0, 150, 50),
                echo_rows.issue(1, 200),                        # its 1000 ms passed before the gap: a miss
                echo_rows.issue(2, 1900),                       # open when the gap began: excused
                echo_rows.heartbeat(2000),
                echo_rows.issue(3, 2300),                       # issued after the last good heartbeat
                echo_rows.record('send-error', 2300, seq=3, error='ENODEV'),
                echo_rows.record('rebind-wait', 2500, voidedSeqs=[1, 2], lastGoodAtMs=2000),
                echo_rows.record('rebind', 4000, interface=new, bindEpoch=1),
                echo_rows.heartbeat(4000, interface=new),
                echo_rows.issue(4, 4100),
                echo_rows.reply(4, 4200, 100),
                echo_rows.heartbeat(5500, interface=new)]

    def test_misses_before_the_gap_stay_and_gap_casualties_are_excluded(self):
        counters = self.take(self.gap_log(), at_ms=5600)['countersByDeadlineMs']['1000']
        # seq 0 and 4 completed, seq 1 a miss; seq 2 (open at the gap) and 3 (inside it) excused
        self.assertEqual((counters['issued'], counters['eligible'], counters['completed']), (3, 3, 2))
        self.assertEqual(counters['gapExcluded'], 2)

    def test_a_longer_deadline_excuses_more_of_what_was_pending(self):
        counters = self.take(self.gap_log(), at_ms=5600, deadlines=(3000,))['countersByDeadlineMs']['3000']
        # with 3000 ms, seq 1 (200 + 3000 > 2000) was still open when the gap began
        self.assertEqual(counters['gapExcluded'], 3)
        # seq 0 and 4 remain; seq 4's 3000 ms has not passed at 5500, so only seq 0 is eligible
        self.assertEqual((counters['issued'], counters['eligible'], counters['completed']), (2, 1, 1))

    def test_malformed_rebind_sequences_are_refused(self):
        log = self.gap_log()
        heartbeat_in_gap = log[:9] + [echo_rows.heartbeat(3000)] + log[9:]
        rebind_without_wait = log[:8] + log[9:]
        repeated_wait = log[:9] + [echo_rows.record('rebind-wait', 2600, voidedSeqs=[], lastGoodAtMs=2000)] + log[9:]
        wait_without_last_good = log[:8] + [echo_rows.record('rebind-wait', 2500, voidedSeqs=[1, 2])] + log[9:]
        for rows in (heartbeat_in_gap, rebind_without_wait, repeated_wait, wait_without_last_good):
            with self.subTest(events=[row['event'] for row in rows]), self.assertRaises(te.TaggedEchoError):
                self.take(rows, at_ms=5600)

    def test_a_waiting_source_has_no_boundary(self):
        with self.assertRaisesRegex(te.TaggedEchoError, 'no complete running heartbeat'):
            self.take(self.gap_log()[:9], at_ms=2600)

    def test_the_client_comes_back_on_the_same_tun(self):
        network = FakeNetwork(self.path)
        with patch.object(te.socket, 'socket', side_effect=AssertionError('real socket forbidden')):
            def interface(_name):
                if 1.0 <= network.now < 2.0:
                    raise OSError('tun down')
                return dict(ECHO_IDENTITY)

            te.run_client(server_ip='192.0.2.10', port=5000, session_id=ECHO['sessionId'],
                          flow_id=ECHO['flowId'], log_path=self.path, clock_id=ECHO['clockId'],
                          duration=4.0, rate_hz=4, reply_drain=0.4, socket_factory=network.factory,
                          wait=network.wait, monotonic=network.clock, interface_reader=interface)
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        rebind = next(row for row in rows if row['event'] == 'rebind')
        self.assertEqual((rebind['bindEpoch'], rebind['interface']), (1, ECHO_IDENTITY))
        self.assertTrue(any(at >= 2.0 for at, _data, _peer in network.sent))
        self.assertEqual(rows[-1]['status'], 'finished')


class TheSenderReconnectsWithinItsWindow(unittest.TestCase):
    def test_a_broken_connection_reconnects_and_resends_the_handshake(self):
        net = flow.SenderNetwork()
        headers, connects = [], []
        net.connect = lambda peer: (connects.append(net.now), setattr(net, 'now', net.now + .01))
        net.sendall = lambda data: headers.append(data)
        original_send = net.send

        def send(data):
            if .05 <= net.now < .06 and not getattr(net, 'broke', False):
                net.broke = True
                raise ConnectionResetError('sink closed with its tun')
            return original_send(data)

        net.send = send
        result = fg.run_sender(receiver_ip=flow.IDENTITY['ip'], port=5203, session_id='session-a',
                               flow_id='ue1-data', duration=.2, rate_mbps=.05,
                               socket_factory=net.factory, wait=net.wait, monotonic=net.clock)
        self.assertEqual(result['connections'], 2)
        self.assertEqual(len(headers), 2)
        self.assertTrue(any(at > .06 for at, _data in net.sent))
        self.assertLessEqual(result['payloadSentBytes'], .2 * .05 * 1e6 / 8)


class ACohortCannotReviveAThinWindow(unittest.TestCase):
    """Codex review 2026-09-16: the issued-cohort ratio overwrote the coverage rule's UNKNOWN."""

    def window(self, samples):
        from assurance.coordination.intake import ObservationRules
        from tools.liveconsole import agent as agent_module
        sitting = AgentSitting.__new__(AgentSitting)
        sitting.rules = ObservationRules({'deadlineSuccessRatio': {
            'windowMs': 10000, 'minCoverage': 0.8, 'statistic': 'ratio'}})
        sitting._cached_cadence_ms = 1000
        sitting.observer = None
        sitting.clock = SimpleNamespace(now=lambda: '2026-09-16T00:00:10.000000Z', sleep_ms=lambda _ms: None)
        key = 'deadlineSuccessRatio@ue1'
        rows = [{'t': f'2026-09-16T00:00:{second:02d}.000000Z',
                 'kpis': {key: {'observedAtMs': second * 1000,
                                'byDeadlineMs': {'1000': {'issued': second, 'eligible': second,
                                                          'completed': second}}}}}
                for second in samples]

        def cohort(_rows, _end, _rules, _sources, sleep_ms=None, audit=None, held=True):
            audit[key] = {'valid': True}
            return {key: {'1000': 0.95}}

        with patch.object(agent_module, 'issued_cohort_ratios', cohort):
            kpis, unknown, coverage, cohorts = sitting._aggregate_window(rows, '2026-09-16T00:00:10.000000Z', waited=True)
        return key, kpis, unknown, cohorts

    def test_a_window_mostly_lost_to_a_gap_stays_unknown(self):
        key, kpis, unknown, cohorts = self.window([0, 9, 10])
        self.assertNotIn(key, kpis)
        self.assertIn(key, unknown)
        self.assertEqual(cohorts['byKpi'][key]['invalidReason'], 'sample-coverage-below-minimum')

    def test_a_covered_window_takes_the_cohort_ratio(self):
        key, kpis, unknown, _cohorts = self.window(list(range(0, 11)))
        self.assertEqual(kpis[key], {'1000': 0.95})


def _load_keeper():
    path = REPO / 'experiment_results' / 'ota-20260911' / 'ops' / 'keeper.py'
    spec = importlib.util.spec_from_file_location('keeper_identity', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CompositionWaitsForAUeInsteadOfRefusingIt(unittest.TestCase):
    """2026-09-16: attempts 120, 127 and 128 were refused with pfWeight@<ue>
    ATTRIBUTION_UNAVAILABLE after both model calls had been paid for, because the UE left the
    KPM stream between the identity pin and the axis build. A UE the radio dropped is a
    hardware fault, not a cost of the control, so composition waits for the role and re-pins
    to the id it holds now."""

    class Clock:
        def __init__(self):
            self.ms = 0.0
        def monotonic_ms(self):
            return self.ms
        def sleep_ms(self, ms):
            self.ms += float(ms)
        def now(self):
            return '2026-09-16T05:00:00Z'

    def observation(self, amf, nci=12345678):
        return SimpleNamespace(amf_ue_ngap_id=int(amf), serving_nci=int(nci),
                               e2_node='node', connection_epoch=1, observed_at='t')

    def call(self, roles, observable, previous_amf=41, cells=(12345678,)):
        """roles: successive answers of the role resolver; observable: amf -> observation."""
        answers = list(roles)
        clock = self.Clock()
        seen = []

        def amf_of(ue):
            seen.append(clock.ms)
            return answers.pop(0) if len(answers) > 1 else answers[0]

        def observe(reader, *, now, sleep_ms, freshness_ms, amf_ue_ngap_id):
            if amf_ue_ngap_id not in observable:
                raise agent_mod.LiveConsoleError('no fresh indication')
            return observable[amf_ue_ngap_id]

        with patch.object(agent_mod, 'observe_selected_ue', observe):
            return agent_mod._await_addressable(
                reader=None, amf_of=amf_of, ue='ue3', clock=clock,
                freshness_ms=4000, allowed_cells=cells,
                previous=self.observation(previous_amf)), clock

    def test_an_addressable_ue_costs_no_wait(self):
        observed, clock = self.call([41], {41: self.observation(41)})
        self.assertEqual(41, observed.amf_ue_ngap_id)
        self.assertEqual(0.0, clock.ms)

    def test_a_ue_that_comes_back_under_a_new_id_is_followed(self):
        observed, clock = self.call([None, None, 49], {49: self.observation(49)})
        self.assertEqual(49, observed.amf_ue_ngap_id)
        self.assertGreater(clock.ms, 0.0)

    def test_a_ue_on_a_cell_outside_the_admitted_surface_is_not_accepted(self):
        with self.assertRaises(agent_mod.LiveConsoleError):
            self.call([41], {41: self.observation(41, nci=99999999)})

    def test_a_ue_that_never_returns_still_refuses(self):
        with self.assertRaises(agent_mod.LiveConsoleError) as caught:
            self.call([None], {})
        self.assertIn('no current amfUeNgapId', str(caught.exception))

    def test_composition_actually_calls_it(self):
        """The five cases above exercise the helper; this one pins that composition uses it.
        Without this, deleting the call site leaves every other case green -- checked."""
        import inspect
        source = inspect.getsource(agent_mod._compose_agent_sitting)
        self.assertIn('_await_addressable(', source)
        # and it runs before the participant is built, not after it has already refused
        self.assertLess(source.index('_await_addressable('), source.index('_build_supplementary('))

    def test_the_wait_is_bounded(self):
        with self.assertRaises(agent_mod.LiveConsoleError):
            self.call([7], {})     # id resolves, indication never arrives
        # and it does not spin forever: the bound is a constant, stated
        self.assertGreaterEqual(agent_mod.ROLE_ADDRESSABLE_WAIT_MS, 60000)


class TheFormationAllowanceIsNotChargedForARadioOutage(unittest.TestCase):
    """The 240 s first-proposal allowance exists to compare methods with each other. Charging
    it for a UE the radio dropped compares the bed instead -- and with composition now waiting
    for an absent UE, a three-minute gap would spend the whole allowance before the first
    proposal was even asked for. Waiting time is excluded; everything else still counts."""

    def sitting(self, elapsed_ms, composition_wait_ms=0.0, disconnect_waits=()):
        s = AgentSitting.__new__(AgentSitting)
        s.request = SimpleNamespace(formation_deadline_ms=240000)
        s.composition_wait_ms = composition_wait_ms
        s.hardware_disconnects = [{'waitedMs': w} for w in disconnect_waits]
        s._elapsed_ms = lambda: elapsed_ms
        s._trials_used_total = lambda: 0
        s.ended = []
        s._end = lambda *a, **k: s.ended.append(a)
        s.events = []
        s.declare_non_trial_event = lambda kind, detail, **k: s.events.append(kind)
        return s

    def test_a_slow_method_is_recorded_and_waited_for(self):
        # 오너 지시(2026-09-18): 첫 제안 허용 시간을 넘겨도 판을 끝내지 않고 한 번 기록한다.
        s = self.sitting(240001)
        self.assertFalse(s._formation_overrun())
        self.assertFalse(s._formation_overrun())
        self.assertEqual([], s.ended)
        self.assertEqual(['formation-overrun'], s.events)

    def test_a_composition_wait_is_not_charged(self):
        s = self.sitting(400000, composition_wait_ms=180000)
        self.assertFalse(s._formation_overrun())
        self.assertFalse(s.ended)

    def test_an_in_sitting_disconnect_wait_is_not_charged_either(self):
        s = self.sitting(400000, disconnect_waits=(120000.0, 61000.0))
        self.assertFalse(s._formation_overrun())

    def test_the_relief_is_only_the_measured_wait(self):
        s = self.sitting(400000, composition_wait_ms=100000)   # 300 s of real work left
        s._formation_overrun()
        self.assertEqual(['formation-overrun'], s.events)

    def test_the_number_relieved_is_the_number_the_evidence_reports(self):
        s = self.sitting(0, composition_wait_ms=1000, disconnect_waits=(2000.0, 3000.0))
        self.assertEqual(6000.0, s._hardware_wait_ms())


class TheTimeTheRadioCostIsInTheEvidence(unittest.TestCase):
    """Attempt 129 waited 64 s for ue3 during composition and its episode still reported
    hardwareDisconnects 0, because the sitting did not exist yet when the wait happened. The
    footnote has to be able to state what the radio cost, so both waits are reported."""

    def test_the_episode_record_carries_both_waits(self):
        s = AgentSitting.__new__(AgentSitting)
        s.composition_wait_ms = 64000.0
        s.hardware_disconnects = [{'waitedMs': 2000.0}, {'waitedMs': 3000.0}]
        record = {'compositionMs': float(s.composition_wait_ms),
                  'disconnectMs': s._hardware_wait_ms() - float(s.composition_wait_ms),
                  'totalMs': s._hardware_wait_ms()}
        self.assertEqual({'compositionMs': 64000.0, 'disconnectMs': 5000.0,
                          'totalMs': 69000.0}, record)

    def test_the_metric_sums_composition_waits_across_episodes(self):
        from experiments.agent_metrics import hardware_disconnects
        summary = hardware_disconnects([
            {'episodeId': 'a', 'hardwareDisconnects': [{'waitedMs': 2000.0}],
             'hardwareWaitMs': {'compositionMs': 64000.0}},
            {'episodeId': 'b', 'hardwareDisconnects': [], 'hardwareWaitMs': {'compositionMs': 0.0}},
            {'episodeId': 'c'}])                      # an older episode with no such field
        self.assertEqual(64000.0, summary['compositionWaitMsTotal'])
        self.assertEqual(1, summary['episodesWithCompositionWait'])
        self.assertEqual(2000.0, summary['waitedMsTotal'])

    def test_both_writers_actually_emit_the_field(self):
        """Two places build the record; a field added to only one is invisible in the other."""
        import inspect
        source = inspect.getsource(AgentSitting)
        self.assertEqual(2, source.count('"hardwareWaitMs"'))
        self.assertEqual(source.count('"hardwareDisconnects"'), source.count('"hardwareWaitMs"'))


class ARestartNeedsEvidenceAndAnEndlessReattachIsEvidence(unittest.TestCase):
    """2026-09-16: ue2 held no tun address and completed random access about once a second
    under a new RNTI each time, and no evidence class covered it, so the readiness gate
    printed 'UE restart held by NO_UE_AUTO_RESTART: ue2' every round until the attempt
    expired.  Measured on the real logs of the same minutes: ue2 26 successes over 24
    RNTIs, ue1 and ue3 zero successes on a single RNTI each."""

    def zombie(self):
        path = REPO / 'experiment_results' / 'ota-20260911' / 'ops' / 'ue_zombie.py'
        spec = importlib.util.spec_from_file_location('ue_zombie_evidence', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    SETTLED = ('UE 0 RNTI 2e4b stats sfn: 128.8\n' * 20)
    LOOPING = ('[RAPROC] 4-Step RA procedure succeeded. CBRA\n'
               'UE 0 RNTI b2b5 stats sfn: 0.8\n'
               '[RAPROC] 4-Step RA procedure succeeded. CBRA\n'
               '[RAPROC] 4-Step RA procedure succeeded. CBRA\n'
               'UE 0 RNTI 3363 stats sfn: 256.8\n')

    def test_a_board_that_keeps_reattaching_under_new_contexts_is_evidence(self):
        self.assertTrue(self.zombie().ra_looping(self.LOOPING))

    def test_a_settled_board_is_not(self):
        self.assertFalse(self.zombie().ra_looping(self.SETTLED))

    def test_repeated_attaches_on_one_context_are_not(self):
        self.assertFalse(self.zombie().ra_looping(self.LOOPING.replace('3363', 'b2b5')))

    def test_a_single_reattach_is_not(self):
        self.assertFalse(self.zombie().ra_looping(
            '[RAPROC] 4-Step RA procedure succeeded. CBRA\n' + self.SETTLED))

    def test_it_is_reported_as_its_own_class_before_the_scanning_probe(self):
        zombie = self.zombie()
        probed = []

        def ssh(host, args, timeout=0):
            probed.append(args)
            text = "'0'" if 'grep -c' in ' '.join(args) else ''
            return SimpleNamespace(stdout='2' if 'grep -c' in ' '.join(args) else self.LOOPING,
                                   stderr='', returncode=0)

        with patch.object(zombie, 'scanning_evidence', lambda *a: self.fail('probed the cell first')), \
                patch.object(zombie, 'core_session_released', lambda *a: self.fail('read the core first')):
            self.assertEqual('ra-looping', zombie.zombie('ue2', 'gnb2', ssh, lambda: None))


class TheEpochRepinWaitsForTheSittingNotForTheGate(unittest.TestCase):
    """A stale epoch refuses every attempt, and the attempt's own lock used to keep the
    repin from running: on 2026-09-16 attempts 121-123 were each refused with
    FRESH_TWO_CELL_KPM_REQUIRED while keeper skipped the repin because the bed was busy.
    The repin is barred only while a trial is live."""

    def repin(self, in_sitting, binding):
        keeper = _load_keeper()
        logs = []
        node = 'ngran=02;plmn=208-095-2;nb=0000002816/00;cudu=none:00000000000000000000'
        with patch.object(keeper, 'episode_in_sitting', lambda: in_sitting), \
                patch.object(keeper, 'log', lambda *a, **k: logs.append((a, k))), \
                patch.object(keeper, '_live_epochs', lambda: {2816: 812, 3584: 810}), \
                patch.object(keeper, 'CELL_NB', {'gnb1': 3584, 'gnb2': 2816}), \
                patch.object(keeper, 'BINDING', binding):
            keeper.sync_binding_epochs()
        return json.loads(binding.read_text())['kpm']['expectedEpochs'][node], logs

    def document(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'binding.json'
        path.write_text(json.dumps({'kpm': {'expectedEpochs': {
            'ngran=02;plmn=208-095-2;nb=0000002816/00;cudu=none:00000000000000000000': 809,
            'ngran=02;plmn=208-095-2;nb=0000003584/00;cudu=none:00000000000000000000': 810}}}))
        return path

    def test_the_readiness_gate_does_not_block_the_repin(self):
        epoch, logs = self.repin(False, self.document())
        self.assertEqual(812, epoch)
        self.assertEqual(['BINDING_EPOCH_REPINNED'], [a[0] for a, _ in logs])

    def test_a_live_sitting_still_blocks_it(self):
        epoch, logs = self.repin(True, self.document())
        self.assertEqual(809, epoch)
        self.assertEqual([], logs)


class TheKeeperRecoversOnlyAProvablyDeadUeDuringAnEpisode(unittest.TestCase):
    def keeper(self):
        path = REPO / 'experiment_results' / 'ota-20260911' / 'ops' / 'keeper.py'
        spec = importlib.util.spec_from_file_location('keeper_identity', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_three_bad_probes_and_evidence_restart_and_nothing_else_does(self):
        keeper = self.keeper()
        restarts, logs = [], []
        evidence = {'ue1': None, 'ue3': 'softmodem-absent'}
        with patch.object(keeper, 'episode_in_sitting', lambda: True), \
                patch.object(keeper, 'log', lambda *a, **k: logs.append((a, k))), \
                patch.object(keeper, 'restart_ue', lambda host, evidence=None: restarts.append((host, evidence))), \
                patch.object(keeper, 'zombie_release_evidence', lambda host: evidence[host]):
            for _ in range(keeper.FAIL_STREAK):
                keeper.recover_during_episode('ue1', False)   # dead-looking, no evidence
                keeper.recover_during_episode('ue3', False)
            keeper.recover_during_episode('ue2', False)       # one bad probe only
            keeper.recover_during_episode('ue2', True)
        self.assertEqual(restarts, [('ue3', 'softmodem-absent')])
        self.assertEqual(keeper._busy_streak['ue2'], 0)

    def test_the_readiness_gate_keeps_recovery_while_it_holds_the_lock(self):
        keeper = self.keeper()
        restarts = []
        with patch.object(keeper, 'log', lambda *a, **k: None), \
                patch.object(keeper, 'restart_ue', lambda host, evidence=None: restarts.append(host)), \
                patch.object(keeper, 'zombie_release_evidence', lambda host: 'softmodem-absent'), \
                patch.object(keeper, 'BUSY', SimpleNamespace(read_text=lambda: '4242 manual-attempt\n')):
            for _ in range(keeper.FAIL_STREAK + 2):
                keeper.recover_during_episode('ue3', False)
            self.assertEqual(restarts, [])
            self.assertFalse(keeper.episode_in_sitting())
        with patch.object(keeper, 'BUSY', SimpleNamespace(read_text=lambda: '4242 cli\n')):
            self.assertTrue(keeper.episode_in_sitting())

    def test_an_evidence_restart_resets_usb_and_does_not_look_the_evidence_up_twice(self):
        keeper = self.keeper()
        calls = []
        with patch.object(keeper, 'log', lambda *a, **k: None), \
                patch.object(keeper, 'ue_auto_restart_held', lambda: True), \
                patch.object(keeper, 'zombie_release_evidence', side_effect=AssertionError('looked up twice')), \
                patch.object(keeper, 'budget', lambda _key: True), \
                patch.object(keeper, 'episode_running', lambda: False), \
                patch.object(keeper, '_ue_password', lambda: 'x'), \
                patch.object(keeper, 'usb_wedged', lambda host: False), \
                patch.object(keeper, 'usb_reset', lambda host: calls.append(('usb', host))), \
                patch.object(keeper, 'ssh', lambda host, argv, **k: calls.append(('ssh', host)) or SimpleNamespace(returncode=0, stderr='')), \
                patch.object(keeper.time, 'sleep', lambda _s: None):
            keeper.restart_ue('ue3', evidence='idle-after-release')
        # 2026-09-23: 기동 뒤 반송파 되읽기 ssh 가 하나 더 붙는다 -- 순서(멈춤 -> USB -> 기동)만 본다.
        self.assertEqual(calls[:3], [('ssh', 'ue3'), ('usb', 'ue3'), ('ssh', 'ue3')])


class TheSinkWaitsForTheSenderAfterABriefDrop(unittest.TestCase):
    def test_an_eof_on_the_same_tun_relistens_and_the_reconnected_sender_counts(self):
        header = fg.make_handshake('session-a', 'ue1-data', .8, 2)
        net = flow.ReceiverNetwork([(.11, header + b'payload'), (.3, b''),
                                    (.6, header + b'again'), (1.0, b'x')])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'flow.jsonl'
            with patch.object(fg.socket, 'socket', side_effect=AssertionError('real socket forbidden')):
                payload = fg.run_receiver(bind_ip=flow.IDENTITY['ip'], port=5203, session_id='session-a',
                                          flow_id='ue1-data', log_path=path, clock_id='boot-a', duration=1.2,
                                          socket_factory=net.factory, wait=net.wait, monotonic=net.clock,
                                          interface_reader=lambda name: dict(flow.IDENTITY),
                                          counter_reader=lambda name: 1000)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
        events = [row['event'] for row in rows]
        self.assertEqual(payload, 13)
        self.assertEqual((events.count('rebind-wait'), events.count('rebind'), events.count('connected')), (1, 1, 2))
        self.assertEqual(rows[events.index('rebind')]['bindEpoch'], 1)
        self.assertEqual((rows[-1]['status'], rows[-1]['reason']), ('finished', 'receiver-deadline'))
        self.assertIn((fg.socket.SOL_SOCKET, fg.socket.SO_REUSEADDR, 1), net.listener.options)


class AnEchoReleasedSessionIsRestartEvidence(unittest.TestCase):
    """2026-09-16 00:0x: the AMF 116-min echo released ue1's current PDU session; RRC stayed up."""

    def zombie_module(self):
        path = REPO / 'experiment_results' / 'ota-20260911' / 'ops' / 'ue_zombie.py'
        spec = importlib.util.spec_from_file_location('ue_zombie_identity', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_softmodem_that_never_found_its_cell_is_evidence(self):
        zombie = self.zombie_module()
        self.assertTrue(zombie.never_synced(0, 900, 600))      # fifteen minutes of scanning
        self.assertFalse(zombie.never_synced(1, 900, 600))     # it did sync once
        self.assertFalse(zombie.never_synced(0, 900, 30))      # still starting up
        self.assertFalse(zombie.never_synced(0, 5, 600))       # quiet, not scanning
        reads = []

        def ssh(host, argv, **_kwargs):
            reads.append(argv[-1])
            return SimpleNamespace(stdout='0 900 600\n')
        self.assertTrue(zombie.scanning_evidence('ue2', ssh))
        self.assertIn('ota-fixed38-ue2-', reads[0])
        self.assertFalse(zombie.scanning_evidence('ue2', lambda *a, **k: SimpleNamespace(stdout='')))

    def test_the_last_session_event_of_this_subscriber_decides(self):
        zombie = self.zombie_module()
        create = '[x] Handle a PDU Session Create SM Context Request message from AMF, SUPI imsi-{0}, SNSSAI\n'
        release = ('[x] Retrieve SMF context with SUPI imsi-{0}\n'
                   '[x] Handle itti_n4_session_deletion_response (Release SM Context Request): pdu-session-id 10\n')
        mine, other = '001010000000001', '001010000000002'
        self.assertFalse(zombie.session_released(create.format(mine), mine))
        self.assertTrue(zombie.session_released(create.format(mine) + release.format(mine), mine))
        self.assertFalse(zombie.session_released(create.format(mine) + release.format(other), mine))
        self.assertFalse(zombie.session_released(release.format(mine) + create.format(mine), mine))

    def test_the_log_window_starts_at_the_running_softmodem_and_failures_are_no_evidence(self):
        zombie = self.zombie_module()
        seen = {}

        def ssh(host, argv, **_kwargs):
            if 'basename' in argv[-1]:
                return SimpleNamespace(stdout='ota-fixed38-ue1-20260915T231150862640.log\n')
            return SimpleNamespace(stdout='imsi = "001010000000001"\n')

        def run(argv, **_kwargs):
            seen['argv'] = argv
            return SimpleNamespace(stdout='[x] Retrieve SMF context with SUPI imsi-001010000000001\n'
                                          '[x] Handle itti_n4_session_deletion_response (Release SM Context Request)\n',
                                   stderr='')
        self.assertTrue(zombie.core_session_released('ue1', ssh, run=run))
        self.assertEqual(seen['argv'][:4], ['docker', 'logs', '--since', '2026-09-15T23:11:50+09:00'])
        self.assertFalse(zombie.core_session_released('ue2', lambda *a, **k: SimpleNamespace(stdout=''), run=run))


class TheLockdownAsksWhatTheAdaptersRead(unittest.TestCase):
    """2026-09-16 attempt 109: ue3 left the stream mid-trial and the combined read failed,
    but the only axis the trial changed (ue2's cap) had already read back at its baseline."""

    def sitting(self, observed):
        sitting = AgentSitting.__new__(AgentSitting)
        refs = {f'tx:case/x:trial:6#5:READ:0:r1-{kind}': {
                    'operation': 'READ', 'observedConfig': config}
                for kind, config in observed.items()}
        sitting.runtime = SimpleNamespace(gateway=SimpleNamespace(
            evidence_references=lambda: tuple(refs), evidence=lambda ref: refs[ref]))
        return sitting

    def trial(self):
        from assurance.coordination.tc import Trial
        trial = Trial(trial_index=6, control_id='C5',
                      configuration={'dlPrbCap@ue2': '18', 'servingCell@ue3': '12345678'})
        trial.kernel = {'trialId': 'case/x:trial:6', 'terminalState': 'INCIDENT_LOCKDOWN'}
        return trial

    def test_an_axis_read_back_at_its_baseline_is_verified_even_when_the_read_failed(self):
        baseline = {'dlPrbCap@ue2': '0', 'servingCell@ue3': '12345678'}
        sitting = self.sitting({'cap@ue2': {'dlPrbCap@ue2': '0'}})
        self.assertEqual({'dlPrbCap@ue2': '0'}, sitting._reverted_axes(self.trial(), baseline))

    def test_an_axis_still_holding_the_applied_value_is_not_verified(self):
        baseline = {'dlPrbCap@ue2': '0', 'servingCell@ue3': '12345678'}
        sitting = self.sitting({'cap@ue2': {'dlPrbCap@ue2': '18'}})
        self.assertEqual({'dlPrbCap@ue2': '18'}, sitting._reverted_axes(self.trial(), baseline))

    def test_an_axis_no_adapter_read_is_unverified(self):
        baseline = {'dlPrbCap@ue2': '0', 'servingCell@ue3': '12345678'}
        sitting = self.sitting({'steer@ue1': {'servingCell@ue1': '12345678'}})
        self.assertEqual({'dlPrbCap@ue2': None}, sitting._reverted_axes(self.trial(), baseline))

    def test_only_this_trial_s_reads_count(self):
        baseline = {'dlPrbCap@ue2': '0', 'servingCell@ue3': '12345678'}
        sitting = self.sitting({'cap@ue2': {'dlPrbCap@ue2': '0'}})
        other = self.trial()
        other.kernel = {'trialId': 'case/x:trial:5'}
        self.assertEqual({'dlPrbCap@ue2': None}, sitting._reverted_axes(other, baseline))


class TheEchoClientOutlivesItsServer(unittest.TestCase):
    """2026-09-16 attempt 116: the echo server owns its own 600 s window and starts a second
    before the client, so the client's last requests draw an ICMP port-unreachable."""

    def test_a_refused_datagram_is_a_miss_not_a_source_failure(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / 'echo.jsonl'
        network = FakeNetwork(path)
        original_recv = network.recv

        def recv(size):
            if network.now >= 0.4 and not getattr(network, 'refused', False):
                network.refused = True
                raise ConnectionRefusedError(111, 'Connection refused')
            return original_recv(size)

        network.recv = recv
        good = te.make_packet(ECHO['sessionId'], ECHO['flowId'], 0, 256)
        network.queue = [(0.05, good, None), (0.45, good, None)]
        with patch.object(te.socket, 'socket', side_effect=AssertionError('real socket forbidden')):
            te.run_client(server_ip='192.0.2.10', port=5000, session_id=ECHO['sessionId'],
                          flow_id=ECHO['flowId'], log_path=path, clock_id=ECHO['clockId'],
                          duration=0.8, rate_hz=4, reply_drain=0.2, socket_factory=network.factory,
                          wait=network.wait, monotonic=network.clock,
                          interface_reader=lambda _name: dict(ECHO_IDENTITY))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(rows[-1]['status'], 'finished')
        self.assertTrue(network.refused)
        self.assertNotIn('source-failure', [row.get('status') for row in rows])


def _runner():
    path = REPO / 'experiment_results' / 'ota-20260911' / 'atomic_formal_run_guarded.py'
    spec = importlib.util.spec_from_file_location('afrg_identity', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TheRunnerFollowsTheUe(unittest.TestCase):
    def test_the_role_identity_file_names_each_role_s_current_id(self):
        runner = _runner()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'ue-identity.json'
            rows = {'ue1': {'RC_HEADER_AMF_UE_NGAP_ID': 34, 'nbId': 3584, 'connectionEpoch': 796},
                    'ue3': {'RC_HEADER_AMF_UE_NGAP_ID': 38, 'nbId': 3584, 'connectionEpoch': 796}}
            runner.write_role_identity(rows, path)
            document = json.loads(path.read_text())
            self.assertEqual(document['roles']['ue3']['amfUeNgapId'], 38)
            self.assertLessEqual(abs(document['roles']['ue1']['writtenAtUnix'] - time.time()), 5)

    def test_a_role_the_stream_stops_carrying_ages_out_instead_of_being_erased(self):
        """The file has an age bound; erasing bypasses it.

        This used to assert that a refresh which saw no UE emptied the roles
        map.  That made a five-second gap indistinguishable from a sixty-second
        absence, and 2026-09-16 attempt 159 was refused before submission with
        "UE ue2 has no current amfUeNgapId" while ue1 and ue3 sat in the same
        file 47 seconds old and healthy.  A host the refresh cannot see now
        keeps its entry AND its original timestamp, so ``role_identity_resolver``
        retires it at ``ROLE_IDENTITY_MAX_AGE_S`` the way it retires any other
        stale entry, and a UE that really re-registered overwrites it.
        """
        runner = _runner()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'ue-identity.json'
            runner.write_role_identity({'ue1': {'RC_HEADER_AMF_UE_NGAP_ID': 34, 'nbId': 3584,
                                                'connectionEpoch': 796}}, path)
            stamped = json.loads(path.read_text())['roles']['ue1']['writtenAtUnix']
            refresher = runner.ControlHeaderRefresher(object(), {'34': 'ue1'}, [], directory=Path(temp),
                                                      identity_path=path)
            refresher._stop.wait = lambda _period, _n=[0]: _n.__setitem__(0, _n[0] + 1) or _n[0] > 1
            with patch.object(runner, 'control_header_rows', return_value={}):
                refresher._run()
            kept = json.loads(path.read_text())['roles']['ue1']
            self.assertEqual(34, kept['amfUeNgapId'], 'a gap must not erase a usable id')
            self.assertEqual(stamped, kept['writtenAtUnix'],
                             'the kept entry must not be freshened; it has to age')
            resolve = role_identity_resolver({'liveConsole': {'ueIdentityPath': str(path)}})
            self.assertEqual(34, resolve('ue1'), 'still inside the age bound')
            aged = role_identity_resolver({'liveConsole': {'ueIdentityPath': str(path)}},
                                          clock=lambda: stamped + 10_000)
            self.assertIsNone(aged('ue1'), 'past the bound it is unresolved, as before')

    def test_a_new_tun_address_gets_one_ready_sender_and_a_served_address_none(self):
        runner = _runner()
        pinned = {host: {'ip': f'12.1.1.{n}', 'ifindex': 5} for n, host in enumerate(runner.HOSTS, 2)}
        specs = runner.specs_for(
            {host: dict(name='oaitun_ue1', ip=pinned[host]['ip'], ifindex=5, up=True,
                        bootId='00000000-0000-0000-0000-000000000000') for host in runner.HOSTS},
            6500, 300)
        calls, polls = [], [0]
        ue1_ready = [False]

        class Remote:
            def call(self, endpoint, action, spec=None, **_):
                calls.append((endpoint, action, spec))
                if action == 'identity':
                    if endpoint == 'ue3':  # new address, then back to the original one
                        return {'ip': '12.1.1.99', 'ifindex': 9} if polls[0] < 3 else dict(pinned['ue3'], ifindex=10)
                    if endpoint == 'ue2':  # same address on a re-created tun: its sender reconnects
                        return dict(pinned['ue2'], ifindex=6)
                    return {'ip': '12.1.1.77', 'ifindex': 7}  # ue1 moves; its first launch never runs
                if action == 'inspect':
                    if spec['slot'].startswith('ue1') and not ue1_ready[0]:
                        ue1_ready[0] = True
                        return {'ready': False}
                    return {'ready': True, 'owner': {}, 'sourceOwner': {}}
                return {'reserved': True}

        import threading
        launches, attempted, log = [], set(), []
        retargeter = runner.SenderRetargeter(Remote(), specs, pinned, launches, attempted,
                                             threading.Lock(), log, time.monotonic() + 600)

        def wait(_period):
            polls[0] += 1
            return polls[0] > 4
        retargeter._stop.wait = wait
        with patch.object(runner, 'wait_ready', side_effect=lambda remote, spec, **k: (
                remote.call(spec['endpoint'], 'inspect', spec=spec)['ready']
                or runner.require(False, 'SOURCE_OWNER_OR_LISTENER_NOT_READY:' + spec['slot']))):
            retargeter._run()
        started = [spec['slot'] for endpoint, action, spec in calls if action == 'start']
        # ue2 never (same address), ue3 once (its original address is still served by the
        # original sender), ue1 twice: the first launch never became ready, so it is retried.
        self.assertEqual(sorted(started), ['ue1-tx-r1', 'ue1-tx-r2', 'ue3-tx-r1'])
        self.assertEqual({row['host'] for row in log if 'slot' in row}, {'ue1', 'ue3'})
        self.assertTrue(any(row.get('errorType') for row in log if row['host'] == 'ue1'))
        self.assertIn('extdn', attempted)


if __name__ == '__main__':
    unittest.main()
