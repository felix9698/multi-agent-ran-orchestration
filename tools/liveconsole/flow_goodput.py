#!/usr/bin/env python3
"""Explicit, bounded ext-DN -> UE TCP application goodput source (stdlib only).

Copy this file AND tagged_echo.py together. Nothing starts on import. The UE
receiver counts only payload consumed after one validated session handshake;
TCP/IP overhead, the handshake and the independent echo flow are not goodput.
Heartbeats also preserve tun rx_bytes for the operational cross-check. Query a
running log on its originating UE/boot, never a copied log on the controller.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import select
import socket
import sys
import time
from pathlib import Path

try:
    from . import tagged_echo as common
except ImportError:  # Two explicitly deployed, adjacent standalone programs.
    import tagged_echo as common

LOG_SCHEMA = "flow-goodput-log/1"
SNAPSHOT_SCHEMA = "flow-goodput-snapshot/1"
HANDSHAKE_SCHEMA = "flow-goodput-handshake/1"
MEASUREMENT = "tcp-application-payload-consumed"
INTERFACE = common.INTERFACE
MAX_RUNTIME_SECONDS = 3600
MAX_RATE_MBPS = 30
MAX_HEADER_BYTES = 1024
CHUNK_BYTES = 16384
HEARTBEAT_SECONDS = common.HEARTBEAT_SECONDS
FlowGoodputError = common.TaggedEchoError


def _limits(duration, rate_mbps):
    duration = common._number(duration, "duration", positive=True)
    rate_mbps = common._number(rate_mbps, "rate Mbps", positive=True)
    if duration > MAX_RUNTIME_SECONDS or rate_mbps > MAX_RATE_MBPS:
        raise FlowGoodputError("duration must be <=3600 seconds and rate <=30 Mbps")
    return duration, rate_mbps


def _counter(value, label):
    if type(value) is not int or not 0 <= value < 2**64:
        raise FlowGoodputError(f"{label} must be a nonnegative 64-bit integer")
    return value


def read_tun_rx(interface=INTERFACE):
    return int((Path('/sys/class/net') / interface / 'statistics/rx_bytes').read_text())


def make_handshake(session_id, flow_id, duration, rate_mbps):
    duration, rate_mbps = _limits(duration, rate_mbps)
    row = {"schemaVersion": HANDSHAKE_SCHEMA,
           "sessionId": common._tag(session_id, "sessionId"),
           "flowId": common._tag(flow_id, "flowId"),
           "durationSeconds": duration, "rateMbps": rate_mbps}
    encoded = json.dumps(row, separators=(",", ":"), allow_nan=False).encode('ascii') + b'\n'
    if len(encoded) > MAX_HEADER_BYTES:
        raise FlowGoodputError("handshake exceeds the bounded header")
    return encoded


def _handshake(row, session_id, flow_id, max_rate_mbps):
    if (not isinstance(row, dict)
            or set(row) != {"schemaVersion", "sessionId", "flowId", "durationSeconds", "rateMbps"}
            or row.get("schemaVersion") != HANDSHAKE_SCHEMA
            or row.get("sessionId") != session_id or row.get("flowId") != flow_id):
        raise FlowGoodputError("foreign or malformed flow handshake")
    _limits(row["durationSeconds"], row["rateMbps"])
    if row["rateMbps"] > max_rate_mbps:
        raise FlowGoodputError("sender rate exceeds the receiver's declared bound")
    return row


class _Log(common._Log):
    def __init__(self, path, session_id, flow_id, clock_id):
        super().__init__(path, session_id, flow_id, clock_id)
        self.common["schemaVersion"] = LOG_SCHEMA


def run_receiver(*, bind_ip, port, session_id, flow_id, log_path, duration=120,
                 max_rate_mbps=MAX_RATE_MBPS, allowed_source_ip="192.168.70.135",
                 clock_id=None, socket_factory=None, wait=None, monotonic=None,
                 interface_reader=None, counter_reader=None):
    """Consume one session only; a disconnect never silently starts a new flow."""
    duration, max_rate_mbps = _limits(duration, max_rate_mbps)
    bind_ip, port = common._ipv4(bind_ip), common._port(port)
    allowed_source_ip = common._ipv4(allowed_source_ip)
    common._tag(session_id, "sessionId")
    common._tag(flow_id, "flowId")
    socket_factory = socket.socket if socket_factory is None else socket_factory
    wait = select.select if wait is None else wait
    monotonic = time.monotonic if monotonic is None else monotonic
    interface_reader = common.read_interface if interface_reader is None else interface_reader
    counter_reader = read_tun_rx if counter_reader is None else counter_reader
    log = _Log(log_path, session_id, flow_id, common._clock_id(clock_id))
    listener = channel = None
    start = monotonic()
    status = "finished"
    end_reason = "receiver-deadline"
    payload_bytes = 0
    try:
        log.emit("start", start * 1000, interfaceName=INTERFACE, bindIp=bind_ip,
                 port=port, allowedSourceIp=allowed_source_ip,
                 durationSeconds=duration, maxRateMbps=max_rate_mbps,
                 measurementDefinition=MEASUREMENT)
        initial = common._interface(interface_reader(INTERFACE))
        if initial['ip'] != bind_ip:
            raise FlowGoodputError("bind IPv4 differs from the current UP tun")

        def open_listener(address):
            opened = socket_factory(socket.AF_INET, socket.SOCK_STREAM)
            try:
                opened.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                                  INTERFACE.encode('ascii') + b'\0')
                # Re-listening on the same address after the sink closed its end would otherwise
                # wait out TIME_WAIT (~60 s) before the port binds again.
                opened.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                opened.bind((address, port))
                opened.listen(1)
                opened.setblocking(False)
            except BaseException:
                opened.close()
                raise
            return opened

        def current_interface():
            try:
                return common._interface(interface_reader(INTERFACE))
            except (OSError, ValueError):
                return None

        listener = open_listener(bind_ip)
        finish, next_heartbeat = start + duration, start
        header = b''
        connected = ever_connected = False
        previous_rx = None
        peer = None
        bound, bind_epoch, waiting = initial, 0, False
        while monotonic() < finish:
            now = monotonic()
            if now >= next_heartbeat:
                current = current_interface()
                if current != bound or waiting:
                    # The UE re-registered under a new tun (docs/design/
                    # ue-identity-continuity.md).  The connection is gone with the old
                    # address; the sink re-listens on the new one and the ext-DN sender
                    # reconnects.  Payload stays cumulative, the new tun's rx counter
                    # starts afresh, and no heartbeat is written while there is no tun,
                    # so the gap reads as missing rather than as zero goodput.
                    if not waiting:
                        log.emit("rebind-wait", monotonic() * 1000)
                        for handle in (channel, listener):
                            if handle is not None:
                                handle.close()
                        channel = listener = None
                        connected, header, peer, waiting = False, b'', None, True
                    if current is not None:
                        try:
                            listener = open_listener(current['ip'])
                        except OSError:
                            listener = None  # not bindable yet; the next heartbeat retries
                        if listener is not None:
                            bound, bind_epoch, waiting, previous_rx = current, bind_epoch + 1, False, None
                            log.emit("rebind", monotonic() * 1000, interface=current,
                                     bindEpoch=bind_epoch, bindIp=current['ip'])
                if not waiting:
                    rx = _counter(counter_reader(INTERFACE), "tun rx bytes")
                    if previous_rx is not None and rx < previous_rx:
                        raise FlowGoodputError("tun rx counter reset")
                    previous_rx = rx
                    log.emit("heartbeat", monotonic() * 1000,
                             status="running" if connected else "waiting",
                             interface=bound, payloadBytes=payload_bytes, tunRxBytes=rx)
                next_heartbeat = monotonic() + HEARTBEAT_SECONDS
            active = channel if channel is not None else listener
            if active is None:
                wait([], [], [], max(0.0, min(finish, next_heartbeat) - monotonic()))
                continue
            readable, _, _ = wait([active], [], [],
                                  max(0.0, min(finish, next_heartbeat) - monotonic()))
            if not readable or monotonic() >= finish:
                continue
            try:
                if channel is None:
                    channel, peer = listener.accept()
                    channel.setblocking(False)
                    listener.close()
                    listener = None
                    if peer[0] != allowed_source_ip:
                        raise FlowGoodputError("connection source is not the permitted ext-DN IPv4")
                    continue
                data = channel.recv(CHUNK_BYTES)
            except BlockingIOError:
                continue
            except OSError as exc:
                if current_interface() == bound and not (
                        connected and isinstance(exc, (ConnectionError, TimeoutError))):
                    raise
                data = None  # the tun, or the connection with it, went away under the socket
            if not data:
                if not connected and current_interface() == bound:
                    raise FlowGoodputError("connection ended before its validated handshake")
                # A drop and return of the same tun between heartbeats leaves the address as it
                # was, so the sink cannot tell it from the sender's own end: either way it waits
                # for the (reconnecting) sender again until its window closes, and the gap is a
                # rebind -- missing, never a finished flow.
                log.emit("rebind-wait", monotonic() * 1000)
                channel.close()
                channel = None
                connected, header, peer, waiting = False, b'', None, True
                next_heartbeat = monotonic()
                continue
            if not connected:
                header += data
                split = header.find(b'\n')
                if split < 0:
                    if len(header) >= MAX_HEADER_BYTES:
                        raise FlowGoodputError("unterminated/oversized handshake")
                    continue
                if split + 1 > MAX_HEADER_BYTES:
                    raise FlowGoodputError("oversized handshake")
                settings = _handshake(common._json(header[:split]), session_id, flow_id, max_rate_mbps)
                log.emit("connected", monotonic() * 1000, peerIp=peer[0], peerPort=peer[1],
                         handshake=settings)
                connected = ever_connected = True
                data, header = header[split + 1:], b''
            payload_bytes += len(data)
            if payload_bytes > math.ceil(max_rate_mbps * 1e6 / 8 * duration) + CHUNK_BYTES:
                raise FlowGoodputError("payload exceeds the bounded workload")
        if not ever_connected:
            raise FlowGoodputError("no validated flow connected before the receiver deadline")
        return payload_bytes
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        status = "source-failure"
        end_reason = "source-failure"
        log.emit("failure", monotonic() * 1000, status=status, error=str(exc))
        raise FlowGoodputError(f"flow goodput {status}: {exc}") from exc
    finally:
        try:
            log.emit("end", monotonic() * 1000, status=status, reason=end_reason,
                     payloadBytes=payload_bytes)
        finally:
            log.close()
            if channel is not None:
                channel.close()
            if listener is not None:
                listener.close()


def run_sender(*, receiver_ip, port, session_id, flow_id, duration=90, rate_mbps=2,
               socket_factory=None, wait=None, monotonic=None):
    """Paced TCP workload: backpressure does not cause a later catch-up burst.

    The total runtime includes bounded connection/handshake time. The declared
    rate is offered pacing, not a claim about sent or delivered application rate.
    """
    duration, rate_mbps = _limits(duration, rate_mbps)
    receiver_ip, port = common._ipv4(receiver_ip), common._port(port)
    header = make_handshake(session_id, flow_id, duration, rate_mbps)
    socket_factory = socket.socket if socket_factory is None else socket_factory
    wait = select.select if wait is None else wait
    monotonic = time.monotonic if monotonic is None else monotonic
    channel = socket_factory(socket.AF_INET, socket.SOCK_STREAM)
    start = monotonic()
    finish = start + duration
    sent = 0
    connections = 0

    def connect():
        """A connected, handshaken channel; refused or timed-out connects retry within the window.

        The sink may be re-listening on a re-registered UE's tun (docs/design/
        ue-identity-continuity.md), so an attempt that fails is not the end of the source.
        """
        nonlocal channel, connections
        while True:
            channel.settimeout(max(0.1, min(5.0, finish - monotonic())))
            try:
                channel.connect((receiver_ip, port))
                remaining = finish - monotonic()
                if remaining <= 0:
                    raise FlowGoodputError("connection used the bounded sending window")
                channel.settimeout(min(5.0, remaining))
                channel.sendall(header)
                channel.setblocking(False)
                connections += 1
                return
            except OSError:
                channel.close()
                if monotonic() + 1.0 >= finish:
                    raise FlowGoodputError("the receiver never accepted within the bounded window")
                wait([], [], [], 1.0)
                channel = socket_factory(socket.AF_INET, socket.SOCK_STREAM)

    try:
        connect()
        bytes_per_second = rate_mbps * 1e6 / 8
        block = b'x' * max(1, min(CHUNK_BYTES, int(bytes_per_second * .01)))
        next_send = monotonic()
        while monotonic() < finish:
            now = monotonic()
            if now < next_send:
                wait([], [], [], min(next_send, finish) - now)
                continue
            _, writable, _ = wait([], [channel], [], finish - now)
            if not writable or monotonic() >= finish:
                continue
            remaining_bytes = max(0, int((finish - monotonic()) * bytes_per_second))
            if not remaining_bytes:
                break
            try:
                count = channel.send(block[:remaining_bytes])
            except BlockingIOError:
                continue
            except OSError:
                count = 0
            if not count:
                # The sink closed with its tun (a re-registration, or the same tun back after
                # a drop): connect again for what is left of the window, at the same pace.
                channel.close()
                channel = socket_factory(socket.AF_INET, socket.SOCK_STREAM)
                connect()
                next_send = monotonic()
                continue
            sent += count
            next_send = monotonic() + count / bytes_per_second
        return {"sessionId": session_id, "flowId": flow_id, "rateMbps": rate_mbps,
                "durationSeconds": duration, "payloadSentBytes": sent,
                "elapsedSeconds": monotonic() - start, "connections": connections,
                "status": "finished"}
    finally:
        channel.close()


def snapshot(log_path, session_id, flow_id, max_age_ms, *, clock_id=None, monotonic=None):
    """Validate the complete committed log and select its last running heartbeat."""
    common._tag(session_id, "sessionId")
    common._tag(flow_id, "flowId")
    expected_clock = common._clock_id(clock_id)
    max_age_ms = common._number(max_age_ms, "max age", positive=True)
    monotonic = time.monotonic if monotonic is None else monotonic
    with open(log_path, 'rb') as source:
        if os.fstat(source.fileno()).st_size > common.MAX_LOG_BYTES:
            raise FlowGoodputError("source log exceeds 32 MiB; not truncated")
        data = source.read(common.MAX_LOG_BYTES + 1)
    if len(data) > common.MAX_LOG_BYTES:
        raise FlowGoodputError("source log exceeds 32 MiB; not truncated")
    data = data[:data.rfind(b'\n') + 1]
    start = boundary = identity = connection = None
    previous_at = previous_heartbeat = -1.0
    previous_payload = previous_rx = 0
    bind_ip, bind_epoch, waiting = None, 0, False
    for index, line in enumerate(data.splitlines()):
        row = common._json(line)
        if (row.get('schemaVersion') != LOG_SCHEMA or row.get('sessionId') != session_id
                or row.get('flowId') != flow_id or row.get('clockId') != expected_clock):
            raise FlowGoodputError("log schema/session/flow/clock does not match this source boot")
        at = common._number(row.get('atMs'), "event time")
        if at < previous_at:
            raise FlowGoodputError("reordered event time")
        previous_at = at
        event = row.get('event')
        if event == 'start':
            if index != 0 or start is not None or row.get('interfaceName') != INTERFACE:
                raise FlowGoodputError("invalid or repeated start")
            _limits(row.get('durationSeconds'), row.get('maxRateMbps'))
            common._ipv4(row.get('bindIp'))
            common._ipv4(row.get('allowedSourceIp'))
            common._port(row.get('port'))
            if row.get('measurementDefinition') != MEASUREMENT:
                raise FlowGoodputError("wrong measurement definition")
            start = row
            bind_ip = row['bindIp']
        elif start is None:
            raise FlowGoodputError("log has no initial start")
        elif event == 'connected':
            if waiting or connection is not None or row.get('peerIp') != start['allowedSourceIp']:
                raise FlowGoodputError("repeated or foreign source connection")
            common._port(row.get('peerPort'))
            _handshake(row.get('handshake', {}), session_id, flow_id, start['maxRateMbps'])
            connection = {'peerIp': row['peerIp'], 'peerPort': row['peerPort'], 'connectedAtMs': at}
        elif event == 'rebind-wait':
            if waiting:
                raise FlowGoodputError("repeated rebind-wait")
            connection = boundary = None
            waiting = True
        elif event == 'rebind':
            current = common._interface(row.get('interface'))
            if (not waiting or row.get('bindEpoch') != bind_epoch + 1
                    or row.get('bindIp') != current['ip']):
                raise FlowGoodputError("rebind without a wait, an epoch that does not advance by "
                                       "one, or another address")
            bind_epoch, bind_ip, identity, connection, boundary = bind_epoch + 1, current['ip'], current, None, None
            waiting = False
            previous_rx = 0  # the new tun's counter starts afresh
        elif event == 'heartbeat':
            if waiting:
                raise FlowGoodputError("heartbeat while the sink waits for a rebind")
            current = common._interface(row.get('interface'))
            if current['ip'] != bind_ip or (identity is not None and current != identity):
                raise FlowGoodputError("source interface identity changed")
            payload = _counter(row.get('payloadBytes'), "payload bytes")
            rx = _counter(row.get('tunRxBytes'), "tun rx bytes")
            if payload < previous_payload or rx < previous_rx or at <= previous_heartbeat:
                raise FlowGoodputError("reset counter or nonadvancing heartbeat")
            if payload > math.ceil(start['maxRateMbps'] * 1e6 / 8 * start['durationSeconds']) + CHUNK_BYTES:
                raise FlowGoodputError("payload exceeds bounded workload")
            expected_status = 'running' if connection is not None else 'waiting'
            if row.get('status') != expected_status or (connection is None and payload and not bind_epoch):
                raise FlowGoodputError("invalid source heartbeat state")
            previous_payload, previous_rx, previous_heartbeat = payload, rx, at
            identity = current
            boundary = row if connection is not None else None
        elif event in ('end', 'failure'):
            raise FlowGoodputError("source has ended or failed")
        else:
            raise FlowGoodputError("unknown source event")
    if boundary is None:
        raise FlowGoodputError("no complete running heartbeat")
    result = {'schemaVersion': SNAPSHOT_SCHEMA, 'sessionId': session_id, 'flowId': flow_id,
              'clockId': expected_clock, 'status': 'running',
              'measurementDefinition': MEASUREMENT, 'interface': identity,
              'connection': connection, 'observedAtMs': boundary['atMs'],
              'remoteNowMs': monotonic() * 1000, 'payloadBytes': boundary['payloadBytes'],
              'tunRxBytes': boundary['tunRxBytes'], 'sourceLog': os.path.abspath(log_path),
              'bindEpoch': bind_epoch}
    validate_snapshot(result, session_id, flow_id, max_age_ms)
    return result


def validate_snapshot(row, session_id, flow_id, max_age_ms):
    """Shared validation for the local reader and the remote LIVE observer."""
    if (not isinstance(row, dict) or row.get('schemaVersion') != SNAPSHOT_SCHEMA
            or row.get('sessionId') != session_id or row.get('flowId') != flow_id
            or row.get('status') != 'running' or row.get('measurementDefinition') != MEASUREMENT):
        raise FlowGoodputError("wrong flow-goodput session/flow/source state")
    common._tag(row.get('clockId'), 'clockId')
    common._interface(row.get('interface'))
    at = common._number(row.get('observedAtMs'), 'observation time')
    now = common._number(row.get('remoteNowMs'), 'remote time')
    if not 0 <= now - at <= common._number(max_age_ms, 'max age', positive=True):
        raise FlowGoodputError("stale flow-goodput source")
    _counter(row.get('payloadBytes'), 'payload bytes')
    _counter(row.get('tunRxBytes'), 'tun rx bytes')
    connection = row.get('connection')
    if not isinstance(connection, dict):
        raise FlowGoodputError("no attributed source connection")
    common._ipv4(connection.get('peerIp'))
    common._port(connection.get('peerPort'))
    connected = common._number(connection.get('connectedAtMs'), 'connection time')
    if connected > at:
        raise FlowGoodputError("heartbeat precedes the source connection")
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    receiver = commands.add_parser('receiver', help='explicit UE-bound single-session payload sink')
    sender = commands.add_parser('sender', help='bounded paced ext-DN TCP workload')
    reader = commands.add_parser('snapshot', help='read a running log on its source UE/boot')
    for command in (receiver, sender, reader):
        command.add_argument('--session-id', required=True)
        command.add_argument('--flow-id', required=True)
    for command in (receiver, sender):
        command.add_argument('--port', required=True, type=int)
        command.add_argument('--duration-s', dest='duration', type=float, default=120 if command is receiver else 90)
    receiver.add_argument('--bind-ip', required=True)
    receiver.add_argument('--allow-source-ip', dest='allowed_source_ip', default='192.168.70.135')
    receiver.add_argument('--max-rate-mbps', type=float, default=MAX_RATE_MBPS)
    sender.add_argument('--receiver-ip', required=True)
    sender.add_argument('--rate-mbps', type=float, default=2)
    for command in (receiver, reader):
        command.add_argument('--log', dest='log_path', required=True)
        command.add_argument('--clock-id')
    reader.add_argument('--max-age-ms', required=True, type=float)
    args = vars(parser.parse_args(argv))
    command = args.pop('command')
    try:
        if command == 'receiver':
            run_receiver(**args)
        else:
            result = run_sender(**args) if command == 'sender' else snapshot(**args)
            print(json.dumps(result, separators=(',', ':'), allow_nan=False))
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        print(f'flow-goodput: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
