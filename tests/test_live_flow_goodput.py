"""Hermetic per-flow source/observer tests: injected sockets, clocks and tun reads."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from assurance.coordination.tc import intent_from_sentence
from tools.liveconsole import flow_goodput as fg
from tools.liveconsole.kpi_observer import (
    CompositeKpiObserver, KpiObserverError, LiveFlowGoodputObserver,
    LiveTaggedEchoObserver, TunRateObserver, build_profile_observer, live_flow_sources,
)

IDENTITY = {'name': 'oaitun_ue1', 'ip': '12.1.1.2', 'ifindex': 7, 'up': True}
COMMON = {'schemaVersion': fg.LOG_SCHEMA, 'sessionId': 'session-a',
          'flowId': 'ue1-data', 'clockId': 'boot-a'}
KEY = 'dlGoodputMbps@131'
SOURCE = {'sourcePath': '/tmp/source path/flow_goodput.py', 'logPath': '/tmp/flow log.jsonl',
          'sessionId': 'session-a', 'flowId': 'ue1-data', 'maxAgeMs': 1500}
PROFILE = {'liveConsole': {'ueHosts': {'131': 'ue1', '132': 'ue2'},
                            'flowGoodput': {'131': dict(SOURCE)}}}


def record(event, at, **fields):
    return dict(COMMON, event=event, atMs=at, **fields)


def start():
    return record('start', 0, interfaceName=fg.INTERFACE, bindIp=IDENTITY['ip'], port=5203,
                  allowedSourceIp='192.168.70.135', durationSeconds=10, maxRateMbps=30,
                  measurementDefinition=fg.MEASUREMENT)


def connected(at=100):
    return record('connected', at, peerIp='192.168.70.135', peerPort=40000,
                  handshake=json.loads(fg.make_handshake('session-a', 'ue1-data', 5, 2)))


def heartbeat(at=1000, payload=100, rx=1000, **changes):
    row = record('heartbeat', at, status='running', interface=dict(IDENTITY),
                 payloadBytes=payload, tunRxBytes=rx)
    row.update(changes)
    return row


def snapshot_row(at=1000, payload=100, rx=1000, **changes):
    row = dict(COMMON, schemaVersion=fg.SNAPSHOT_SCHEMA, status='running',
               measurementDefinition=fg.MEASUREMENT, interface=dict(IDENTITY),
               connection={'peerIp': '192.168.70.135', 'peerPort': 40000, 'connectedAtMs': 100},
               payloadBytes=payload, tunRxBytes=rx, observedAtMs=at, remoteNowMs=at + 100,
               sourceLog='/tmp/flow log.jsonl')
    row.update(changes)
    return row


class Listener:
    def __init__(self, network):
        self.network = network
        self.options = []
        self.bound = None
        self.closed = False
        self.accept_count = 0

    def setsockopt(self, *args):
        self.options.append(args)

    def bind(self, address):
        self.bound = address

    def listen(self, backlog):
        self.backlog = backlog

    def setblocking(self, value):
        self.blocking = value

    def accept(self):
        self.accept_count += 1
        self.network.accepted = True
        return self.network.channel, self.network.peer

    def close(self):
        self.closed = True


class Channel:
    def __init__(self, network):
        self.network = network
        self.closed = False

    def setblocking(self, value):
        self.blocking = value

    def recv(self, size):
        at, data = self.network.queue.pop(0)
        if len(data) > size:
            self.network.queue.insert(0, (at, data[size:]))
        return data[:size]

    def close(self):
        self.closed = True


class ReceiverNetwork:
    def __init__(self, queue=None):
        header = fg.make_handshake('session-a', 'ue1-data', .8, 2)
        self.queue = list(queue if queue is not None else [(.11, header[:10]),
                           (.12, header[10:] + b'payload'), (.3, b'more'), (.9, b'')])
        self.now = 0.0
        self.accepted = False
        self.peer = ('192.168.70.135', 40000)
        self.listener = Listener(self)
        self.channel = Channel(self)
        self.calls = 0
        self.on_wait = None

    def factory(self, family, kind):
        assert (family, kind) == (fg.socket.AF_INET, fg.socket.SOCK_STREAM)
        return self.listener

    def clock(self):
        return self.now

    def wait(self, readers, writers, errors, timeout):
        self.calls += 1
        if self.calls > 100:
            raise AssertionError('busy loop')
        if self.on_wait:
            self.on_wait()
        if not readers:  # a pure sleep: nothing can become readable
            self.now += timeout
            return [], [], []
        due = .1 if not self.accepted else (self.queue[0][0] if self.queue else float('inf'))
        if due <= self.now + timeout:
            self.now = max(self.now, due)
            return readers, [], []
        self.now += timeout
        return [], [], []


class SenderNetwork:
    def __init__(self):
        self.now = 0.0
        self.sent = []
        self.header = None
        self.closed = False
        self.block_once = 0.0
        self.partial = None
        self.timeouts = []

    def factory(self, family, kind):
        assert (family, kind) == (fg.socket.AF_INET, fg.socket.SOCK_STREAM)
        return self

    def clock(self):
        return self.now

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self, peer):
        self.peer = peer
        self.now += .01

    def sendall(self, data):
        self.header = data

    def setblocking(self, value):
        self.blocking = value

    def wait(self, readers, writers, errors, timeout):
        if writers:
            self.now += min(self.block_once, timeout)
            self.block_once = 0
            return [], writers, []
        self.now += timeout
        return [], [], []

    def send(self, data):
        data = data[:self.partial] if self.partial else data
        self.sent.append((self.now, data))
        return len(data)

    def close(self):
        self.closed = True


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'flow.jsonl'
        guard = patch.object(fg.socket, 'socket', side_effect=AssertionError('real socket forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    def rows(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def write_rows(self, rows, suffix=b''):
        self.path.write_bytes(b''.join(json.dumps(row).encode() + b'\n' for row in rows) + suffix)

    def take(self, **changes):
        options = dict(log_path=self.path, session_id='session-a', flow_id='ue1-data',
                       max_age_ms=1500, clock_id='boot-a', monotonic=lambda: 1.1)
        options.update(changes)
        return fg.snapshot(**options)

    def receive(self, network, **changes):
        options = dict(bind_ip=IDENTITY['ip'], port=5203, session_id='session-a', flow_id='ue1-data',
                       log_path=self.path, clock_id='boot-a', duration=1.2,
                       socket_factory=network.factory, wait=network.wait, monotonic=network.clock,
                       interface_reader=lambda name: dict(IDENTITY),
                       counter_reader=lambda name: 100000 + int(network.now * 100000))
        options.update(changes)
        return fg.run_receiver(**options)

    def test_receiver_counts_consumed_payload_excluding_split_handshake_and_other_tun_traffic(self):
        net = ReceiverNetwork()
        snapshots = []
        def inspect():
            # Until the sender's EOF at .9: after it the sink waits for a reconnect (rebind).
            if .5 <= net.now < .9:
                snapshots.append(self.take(monotonic=net.clock))
        net.on_wait = inspect
        self.assertEqual(self.receive(net), 11)
        self.assertTrue(snapshots)
        self.assertEqual(snapshots[-1]['payloadBytes'], 11)
        self.assertGreater(snapshots[-1]['tunRxBytes'], 100000)
        self.assertEqual(net.listener.bound, (IDENTITY['ip'], 5203))
        self.assertIn((fg.socket.SOL_SOCKET, fg.socket.SO_BINDTODEVICE, b'oaitun_ue1\0'),
                      net.listener.options)
        self.assertEqual(net.listener.accept_count, 1)
        self.assertTrue(net.listener.closed and net.channel.closed)
        self.assertEqual(self.rows()[-1]['payloadBytes'], 11)
        with self.assertRaisesRegex(fg.FlowGoodputError, 'ended'):
            self.take(monotonic=net.clock)

    def test_source_log_is_exclusive_append_only_and_private(self):
        self.receive(ReceiverNetwork())
        original = self.path.read_bytes()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            self.receive(ReceiverNetwork())
        self.assertEqual(self.path.read_bytes(), original)

    def test_source_reuses_sudo_ownership_without_world_readability(self):
        with patch.object(fg.common.os, 'geteuid', return_value=0), \
                patch.dict(os.environ, {'SUDO_UID': '1001', 'SUDO_GID': '1002'}), \
                patch.object(fg.common.os, 'fchown') as chown:
            self.receive(ReceiverNetwork())
        self.assertEqual(chown.call_args.args[1:], (1001, 1002))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_wrong_source_ipv4_cannot_create_a_running_session(self):
        net = ReceiverNetwork()
        net.peer = ('192.168.70.134', 40000)
        with self.assertRaisesRegex(fg.FlowGoodputError, 'not the permitted'):
            self.receive(net)
        self.assertFalse(any(row['event'] == 'connected' for row in self.rows()))
        self.assertEqual(self.rows()[-1]['status'], 'source-failure')

    def test_foreign_oversized_and_incomplete_handshakes_fail_closed(self):
        for payload in (fg.make_handshake('other', 'ue1-data', .8, 2),
                        b'x' * fg.MAX_HEADER_BYTES, b'{}\n'):
            with self.subTest(payload=payload[:30]):
                path = self.path.with_name(str(len(payload)) + '.jsonl')
                with self.assertRaises(fg.FlowGoodputError):
                    self.receive(ReceiverNetwork([(.11, payload), (.2, b'')]), log_path=path)
        with self.assertRaisesRegex(fg.FlowGoodputError, 'before its validated handshake'):
            self.receive(ReceiverNetwork([(.11, b'')]))

    def test_interface_change_rebinds_and_only_an_unannounced_counter_reset_fails(self):
        # docs/design/ue-identity-continuity.md: a re-registered UE's tun is followed.
        header = fg.make_handshake('session-a', 'ue1-data', .8, 2)
        for field in ('ip', 'ifindex', 'up', 'counter'):
            with self.subTest(field=field):
                net = ReceiverNetwork([(.11, header + b'payload'), (.3, b'more'),
                                       (.7, header + b'again'), (1.15, b'')])
                changed = {'ip': '12.1.1.3', 'ifindex': 8, 'up': False}
                def iface(name):
                    row = dict(IDENTITY)
                    if net.now >= .5 and field != 'counter':
                        row[field] = changed[field]
                    return row
                def counter(name):
                    return 0 if net.now >= .5 and field == 'counter' else 1000
                path = self.path.with_name(field + '.jsonl')
                options = dict(log_path=path, interface_reader=iface, counter_reader=counter)
                if field == 'counter':
                    with self.assertRaisesRegex(fg.FlowGoodputError, 'counter reset'):
                        self.receive(net, **options)
                    continue
                payload = self.receive(net, **options)
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                events = [row['event'] for row in rows]
                self.assertEqual(rows[-1]['status'], 'finished')
                self.assertEqual(rows[-1]['reason'], 'receiver-deadline')
                # the tun change, and (for a returned sink) the sender's own EOF at 1.15
                self.assertEqual(events.count('rebind-wait'), 1 if field == 'up' else 2)
                after = rows[events.index('rebind-wait'):]
                if field == 'up':
                    self.assertEqual(payload, 11)
                    self.assertNotIn('rebind', events)
                    self.assertFalse([row for row in after if row['event'] == 'heartbeat'])
                else:
                    self.assertEqual(payload, 16)
                    rebind = rows[events.index('rebind')]
                    self.assertEqual(rebind['bindEpoch'], 1)
                    self.assertEqual(rebind['bindIp'], changed['ip'] if field == 'ip' else IDENTITY['ip'])
                    self.assertEqual(net.listener.accept_count, 2)
                    self.assertEqual(events.count('connected'), 2)
                    self.assertEqual(net.listener.bound[0], changed['ip'] if field == 'ip' else IDENTITY['ip'])
                    # The log the receiver wrote reads back as one running source across the rebind.
                    running = path.with_name(field + '-running.jsonl')
                    lines = path.read_text().splitlines()
                    last = [row for row in rows if row['event'] == 'heartbeat' and row['status'] == 'running'][-1]
                    running.write_text(''.join(line + '\n' for line in lines[:rows.index(last) + 1]))
                    snap = self.take(log_path=running, monotonic=lambda: last['atMs'] / 1000)
                    self.assertEqual((snap['bindEpoch'], snap['payloadBytes']), (1, last['payloadBytes']))

    def test_wrong_bind_ip_is_refused_before_socket_creation(self):
        net = ReceiverNetwork()
        with self.assertRaisesRegex(fg.FlowGoodputError, 'differs'):
            self.receive(net, bind_ip='12.1.1.9')
        self.assertIsNone(net.listener.bound)

    def test_no_connection_is_bounded_and_not_a_zero_goodput_measurement(self):
        net = ReceiverNetwork()
        def wait(readers, writers, errors, timeout):
            net.now += timeout
            return [], [], []
        with self.assertRaisesRegex(fg.FlowGoodputError, 'no validated flow'):
            self.receive(net, wait=wait)
        self.assertAlmostEqual(net.now, 1.2)
        self.assertFalse(any(row.get('status') == 'running' for row in self.rows()))

    def test_sender_rate_and_runtime_are_bounded_and_no_catchup_after_backpressure(self):
        net = SenderNetwork()
        net.block_once = .05
        net.partial = 7
        result = fg.run_sender(receiver_ip=IDENTITY['ip'], port=5203, session_id='session-a',
                               flow_id='ue1-data', duration=.2, rate_mbps=.01,
                               socket_factory=net.factory, wait=net.wait, monotonic=net.clock)
        self.assertTrue(net.closed)
        self.assertEqual(json.loads(net.header)['flowId'], 'ue1-data')
        self.assertEqual(result['payloadSentBytes'], sum(len(data) for _, data in net.sent))
        self.assertLessEqual(result['payloadSentBytes'], .2 * .01 * 1e6 / 8)
        for (a, data), (b, _) in zip(net.sent, net.sent[1:]):
            self.assertGreaterEqual(b - a + 1e-12, len(data) / (.01 * 1e6 / 8))
        self.assertLessEqual(result['elapsedSeconds'], .2 + 1e-9)
        self.assertGreaterEqual(net.sent[0][0], .06)

    def test_invalid_bounds_or_wildcard_ips_never_open_sockets(self):
        base = dict(receiver_ip=IDENTITY['ip'], port=5203, session_id='s', flow_id='f')
        for change in ({'duration': 3601}, {'duration': 0}, {'rate_mbps': 30.01},
                       {'rate_mbps': float('nan')}, {'rate_mbps': True},
                       {'receiver_ip': '0.0.0.0'}, {'receiver_ip': 'host.invalid'}, {'port': 0}):
            with self.subTest(change=change), self.assertRaises(fg.FlowGoodputError):
                fg.run_sender(**dict(base, **change))


class SnapshotTest(SourceTest):
    def evidence(self):
        return [start(), heartbeat(0, 0, 500, status='waiting'), connected(),
                heartbeat(500, 10, 1000), heartbeat(1000, 20, 3000)]

    def test_reader_uses_last_complete_heartbeat_on_same_boot(self):
        self.write_rows(self.evidence(), suffix=b'{"event":"heartbeat"')
        result = self.take()
        self.assertEqual(result['payloadBytes'], 20)
        self.assertEqual(result['tunRxBytes'], 3000)
        self.assertEqual(result['observedAtMs'], 1000)
        self.assertEqual(result['remoteNowMs'], 1100)
        self.assertEqual(result['connection']['connectedAtMs'], 100)
        self.assertEqual(result['measurementDefinition'], fg.MEASUREMENT)
        self.assertEqual(self.take(monotonic=lambda: 2.0)['payloadBytes'], 20)

    def test_waiting_terminal_foreign_stale_and_malformed_sources_are_unknown(self):
        cases = [self.evidence() + [record('end', 1100, status='finished')],
                 self.evidence() + [record('failure', 1100, error='lost source')],
                 [start(), heartbeat(0, 0, 500, status='waiting')],
                 self.evidence() + [record('unknown', 1100)],
                 [connected(), heartbeat()],
                 self.evidence() + [dict(heartbeat(1200), flowId='another')]]
        for rows in cases:
            with self.subTest(rows=rows):
                self.write_rows(rows)
                with self.assertRaises(fg.FlowGoodputError):
                    self.take()
        self.write_rows(self.evidence())
        for changes in ({'clock_id': 'another-boot'}, {'monotonic': lambda: 4},
                        {'monotonic': lambda: .5}):
            with self.subTest(changes=changes), self.assertRaises(fg.FlowGoodputError):
                self.take(**changes)
        self.write_rows(self.evidence(), suffix=b'not-json\n')
        with self.assertRaises(fg.FlowGoodputError):
            self.take()

    def test_reset_changed_identity_reordered_or_repeated_heartbeats_are_refused(self):
        for bad in (heartbeat(1500, 1, 4000), heartbeat(1500, 30, 1),
                    heartbeat(1000, 30, 4000), heartbeat(900, 30, 4000),
                    heartbeat(1500, True, 4000), heartbeat(1500, 30, 4000,
                        interface=dict(IDENTITY, ifindex=9)),
                    heartbeat(1500, 30, 4000, status='waiting'), connected(1500)):
            with self.subTest(bad=bad):
                self.write_rows(self.evidence() + [bad])
                with self.assertRaises(fg.FlowGoodputError):
                    self.take(monotonic=lambda: 2)


class Runner:
    def __init__(self, rows):
        self.rows = list(rows)
        self.commands = []

    def run(self, argv, **kwargs):
        self.commands.append(argv)
        row = self.rows.pop(0)
        if row is None:
            return SimpleNamespace(returncode=1, stdout='', stderr='source missing')
        return SimpleNamespace(returncode=0, stdout=json.dumps(row), stderr='')


def observer(rows):
    return LiveFlowGoodputObserver(hosts={'131': 'ue1'}, source_path=SOURCE['sourcePath'],
                                   log_path=SOURCE['logPath'], session_id='session-a',
                                   flow_id='ue1-data', runner=Runner(rows),
                                   monotonic_ms=lambda: 9e12)


class ObserverTest(unittest.TestCase):
    def test_payload_rate_uses_source_clock_and_excludes_other_tun_bytes(self):
        source = observer([snapshot_row(), snapshot_row(2000, 750100, 3001000)])
        self.assertEqual(source.sample(), {})
        self.assertEqual(source.sample()[KEY], 6.0)
        description = source.describe()
        self.assertEqual(description['measurementDefinition'], fg.MEASUREMENT)
        self.assertEqual(description['sourceSamples'][-1]['tunRxBytes'], 3001000)
        argv = shlex.split(source.runner.commands[0][-1])
        self.assertEqual(argv[:3], ['python3', SOURCE['sourcePath'], 'snapshot'])
        self.assertEqual(argv[argv.index('--log') + 1], SOURCE['logPath'])

    def test_preflight_establishes_fresh_source_not_a_fabricated_rate(self):
        source = observer([snapshot_row(), snapshot_row(2000, 250100, 500100)])
        self.assertEqual(source.preflight()['flowGoodputSource@131']['payloadBytes'], 100)
        self.assertEqual(source.sample()[KEY], 2.0)

    def test_missing_sample_clears_delta_baseline_but_retains_session_pin(self):
        source = observer([snapshot_row(), None, snapshot_row(3000, 500100, 600100),
                           snapshot_row(4000, 750100, 850100)])
        self.assertEqual(source.sample(), {})
        self.assertEqual(source.sample(), {})
        self.assertEqual(source.sample(), {})
        self.assertEqual(source.sample()[KEY], 2.0)
        self.assertTrue(source.failures)

    def test_each_rate_carries_the_source_interval_it_spans_and_a_gap_has_none(self):
        source = observer([snapshot_row(), snapshot_row(2000, 250100, 500100), None])
        source.sample()
        self.assertEqual(source.intervals, {})
        source.sample()
        from tools.liveconsole.kpi_observer import LOW_DELIVERY_RUN_KPI
        span = {'startMs': 1000, 'endMs': 2000, 'clockId': 'boot-a'}
        # The continuity key spans the same source interval as the goodput it
        # is read from; it is the same measurement asked a different question.
        self.assertEqual(source.intervals,
                         {KEY: span,
                          f"{LOW_DELIVERY_RUN_KPI}@{KEY.split('@')[1]}": span})
        source.sample()
        # A missed read leaves no stale interval behind for the next row.
        self.assertEqual(source.intervals, {})
        composite = CompositeKpiObserver(observers=(source,))
        source.intervals = {KEY: {'startMs': 1, 'endMs': 2, 'clockId': 'boot-a'}}
        self.assertEqual(composite.intervals, source.intervals)

    def test_bad_source_never_contributes_rate_or_bridges_the_interval(self):
        changes = [{'clockId': 'another'}, {'sessionId': 'old'}, {'flowId': 'echo'},
                   {'status': 'finished'}, {'remoteNowMs': 9000}, {'remoteNowMs': float('nan')},
                   {'interface': dict(IDENTITY, up=False)}, {'interface': dict(IDENTITY, ifindex=8)},
                   {'payloadBytes': 0}, {'tunRxBytes': 0}, {'observedAtMs': 1000},
                   {'measurementDefinition': 'tun-aggregate-rx-bytes'},
                   {'connection': {'peerIp': '192.168.70.135', 'peerPort': 40001, 'connectedAtMs': 100}},
                   {'payloadBytes': True}]
        for change in changes:
            with self.subTest(change=change):
                source = observer([snapshot_row(), snapshot_row(2000, 200, 2000, **change),
                                   snapshot_row(3000, 300, 3000)])
                self.assertEqual(source.sample(), {})
                self.assertEqual(source.sample(), {})
                self.assertEqual(source.sample(), {})
                self.assertTrue(source.failures)

    @staticmethod
    def _goodput_requirements(*ues):
        return [intent_from_sentence(
            f'I{i}: UE ueId={ue} needs at least 1 Mbps downlink, relaxable in 0 steps').requirement
                for i, ue in enumerate(ues, 1)]

    def test_configuration_selects_app_source_without_duplicate_tun_kpi(self):
        """모든 UE 에 flow source 를 주면 tun 은 goodput 을 하나도 안 싣는다.

        2026-09-22 감사 이후 **섞인 구성은 거절된다**(아래 시험).  그래서 이 시험의
        주제 -- "응용 흐름이 tun 집계를 덮어쓰지 못한다" -- 는 이제 전원 구성으로만
        표현된다.  tun 관측자는 남지만 hosts 가 비어 goodput 키를 내지 않는다.
        """
        profile = deepcopy(PROFILE)
        profile['liveConsole']['flowGoodput']['132'] = dict(SOURCE, flowId='ue2-data')
        source = build_profile_observer(self._goodput_requirements(131, 132),
                                        profile_document=profile,
                                        hosts={'131': 'ue1', '132': 'ue2'})
        self.assertIsInstance(source, CompositeKpiObserver)
        self.assertEqual(source.observers[0].hosts, {})
        flows = {tuple(o.hosts.items())[0]: o for o in source.observers[1:]}
        self.assertEqual(sorted(k[0] for k in flows), ['131', '132'])
        for observer in flows.values():
            self.assertIsInstance(observer, LiveFlowGoodputObserver)
        # Execute the actual composite; the tun cannot overwrite either payload KPI.
        with patch.object(source.observers[0], 'sample', return_value={}), \
                patch.object(flows[('131', 'ue1')], 'sample', return_value={KEY: 2}), \
                patch.object(flows[('132', 'ue2')], 'sample',
                             return_value={'dlGoodputMbps@132': 1}):
            self.assertEqual(source.sample(), {KEY: 2, 'dlGoodputMbps@132': 1})
        definitions = [d['measurementDefinition'] for d in source.describe()['sources']]
        self.assertEqual(definitions[0], 'tun-aggregate-rx-bytes')
        self.assertEqual(set(definitions[1:]), {fg.MEASUREMENT})

    def test_a_mixed_configuration_is_refused_not_silently_split(self):
        """UE 마다 정의가 다르면 같은 `dlGoodputMbps@UE` 이름이 두 가지를 뜻한다.

        PROFILE 은 131 에만 flow source 를 둔다.  132 를 tun 집계로 조용히 떨어뜨리면
        한 목표 안에서 '우리가 보낸 바이트' 와 '그 UE 가 받은 모든 트래픽' 을 견주게
        된다 -- 2026-09-22 감사가 잡은 것이 이것이다.
        """
        with self.assertRaises(KpiObserverError) as raised:
            build_profile_observer(self._goodput_requirements(131, 132),
                                   profile_document=PROFILE,
                                   hosts={'131': 'ue1', '132': 'ue2'})
        self.assertIn("Configure every UE or none", str(raised.exception))

    def test_echo_is_still_independent_and_legacy_unconfigured_profile_uses_tun(self):
        goodput = intent_from_sentence('I1: UE ueId=131 needs at least 1 Mbps downlink, relaxable in 0 steps').requirement
        echo = intent_from_sentence('I4: at least 95% of tagged echo requests within 50 ms for '
                                    'ueId=131, relaxable in 0 steps').requirement
        profile = deepcopy(PROFILE)
        profile['liveConsole']['taggedEcho'] = {'131': dict(SOURCE, flowId='ue1-command')}
        source = build_profile_observer([goodput, echo], profile_document=profile, hosts={'131': 'ue1'})
        self.assertEqual(source.observers[0].hosts, {})
        self.assertIsInstance(source.observers[1], LiveTaggedEchoObserver)
        self.assertIsInstance(source.observers[2], LiveFlowGoodputObserver)
        plain = build_profile_observer([goodput], profile_document={}, hosts={'131': 'ue1'})
        self.assertIsInstance(plain, TunRateObserver)
        self.assertTrue(plain.verify_interface)

    def test_actual_live_root_refuses_malformed_flow_before_policy_or_scope_work(self):
        from tools.hfconsole.agent_env import HermeticDeployment
        from tools.liveconsole.agent import AgentRequest, build_agent_sitting
        from tools.liveconsole.build import LiveConsoleError
        sentence = 'I1: UE ueId=131 needs at least 1 Mbps downlink, relaxable in 0 steps'
        with tempfile.TemporaryDirectory() as tmp:
            profile = HermeticDeployment.write(
                tmp, ues={'131': '12345678'}, cells={'12345678': 5, '87654321': 5})
            document = json.loads(profile.read_text())
            document['liveConsole']['ueHosts'] = {'131': 'ue1'}
            document['liveConsole']['flowGoodput'] = {'131': dict(SOURCE, sessionId='')}
            profile.write_text(json.dumps(document))
            with patch('tools.liveconsole.agent.build_r1_policy_port',
                       side_effect=AssertionError('must refuse before policy work')) as policy, \
                    patch('tools.liveconsole.agent.clear_ue_scope',
                          side_effect=AssertionError('must refuse before scope writes')) as scope:
                with self.assertRaisesRegex(LiveConsoleError, 'flowGoodput'):
                    build_agent_sitting(profile, AgentRequest(sentences=(sentence,)))
                policy.assert_not_called()
                scope.assert_not_called()

    def test_explicit_profile_source_must_be_complete_and_attributed(self):
        for changes in ({'sessionId': ''}, {'flowId': None}, {'sourcePath': 4},
                        {'maxAgeMs': 0}, {'maxAgeMs': float('inf')}, {'maxAgeMs': 10001},
                        {'maxAgeMs': True}):
            profile = deepcopy(PROFILE)
            profile['liveConsole']['flowGoodput']['131'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(KpiObserverError):
                live_flow_sources([KEY], profile_document=profile, hosts={'131': 'ue1'})
        with self.assertRaises(KpiObserverError):
            live_flow_sources([KEY], profile_document=PROFILE, hosts={'131': 'other-host'})
        self.assertEqual(live_flow_sources([KEY], profile_document={}, hosts={'131': 'ue1'}), {})
        for bad in ([], False, None, {'131': None}, {'131': 'not-an-object'}):
            profile = deepcopy(PROFILE)
            profile['liveConsole']['flowGoodput'] = bad
            with self.subTest(bad=bad), self.assertRaises(KpiObserverError):
                live_flow_sources([KEY], profile_document=profile, hosts={'131': 'ue1'})


if __name__ == '__main__':
    unittest.main()


class SlowHostRunner:
    """Every host answers after ``delay_s``, like one SSH round trip on the radio."""

    def __init__(self, rows_by_host, delay_s):
        import threading
        self.rows_by_host = {host: list(rows) for host, rows in rows_by_host.items()}
        self.delay_s = delay_s
        self.lock = threading.Lock()

    def run(self, argv, **kwargs):
        import time
        time.sleep(self.delay_s)
        host = next(host for host in self.rows_by_host if host in argv)
        with self.lock:
            row = self.rows_by_host[host].pop(0)
        return SimpleNamespace(returncode=0, stdout=json.dumps(row), stderr='')


class HostsAreReadTogether(unittest.TestCase):
    """2026-09-15 attempt 32: serial SSH per UE made a poll 1.5-2 s against a 1 s cadence,
    so goodput was judged at coverage 0.667 and I1g/I2g/I3g stayed UNKNOWN."""

    def test_three_slow_hosts_cost_one_round_trip_and_give_the_same_rates(self):
        import time
        hosts = {'131': 'ue1', '132': 'ue2', '133': 'ue3'}
        rows = {alias: [snapshot_row(), snapshot_row(2000, 750100, 3001000)] for alias in hosts.values()}
        source = LiveFlowGoodputObserver(hosts=hosts, source_path=SOURCE['sourcePath'],
                                         log_path=SOURCE['logPath'], session_id='session-a',
                                         flow_id='ue1-data', runner=SlowHostRunner(rows, 0.3),
                                         monotonic_ms=lambda: 9e12)
        source.sample()
        started = time.monotonic()
        rates = source.sample()
        self.assertLess(time.monotonic() - started, 0.6)
        # Both keys per UE: the observer publishes the same rate a second time
        # under the continuity KPI so the lowRun statistic can ask a different
        # question of it (v4, OTA_SCENARIO_REDESIGN_20260916 section 3).  One
        # read, two questions -- no extra ssh and no second source.
        from tools.liveconsole.kpi_observer import LOW_DELIVERY_RUN_KPI
        expected = {f"{name}@{ue}": 6.0 for ue in hosts
                    for name in (KEY.split('@')[0], LOW_DELIVERY_RUN_KPI)}
        self.assertEqual(expected, rates)
