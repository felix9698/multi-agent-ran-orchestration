#!/usr/bin/env python3
"""Explicit, bounded UE -> ext-DN UDP echo source (stdlib only, copyable).

Nothing starts on import. ``server`` reflects only the configured session/flow;
``client`` must bind to oaitun_ue1 and preserves every issued request in a fresh
append-only JSONL file. RTT is measured entirely on the UE's monotonic clock.
``snapshot`` must run on that SAME UE/boot, not on a copied log on the controller.
An explicit --clock-id may replace Linux boot_id, but must identify the same boot
and be supplied consistently to client and snapshot. It is not a wall clock.

Log schemaVersion: tagged-echo-log/1. Every event has sessionId, flowId, clockId,
event and atMs. issued adds seq/issuedAtMs; reply adds seq/rttMs; send-error adds
seq/error. Heartbeats carry status and interface {name, ip, ifindex, up}. End is
terminal. A snapshot validates the whole complete log, but folds only events
BEFORE its last heartbeat, so concurrent replies cannot change an older boundary.
No raw evidence is deleted, repaired, truncated or rewritten by this tool.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import select
import socket
import struct
import sys
import time
from pathlib import Path

LOG_SCHEMA = "tagged-echo-log/1"
PACKET_SCHEMA = "tagged-echo-packet/1"
SNAPSHOT_SCHEMA = "tagged-echo-snapshot/1"
INTERFACE = "oaitun_ue1"
HEARTBEAT_SECONDS = 0.5
MAX_RUNTIME_SECONDS = 3600
MAX_RATE_HZ = 20
MAX_PAYLOAD_BYTES = 1200
MAX_DRAIN_SECONDS = 10
MAX_SEQUENCES = MAX_RUNTIME_SECONDS * MAX_RATE_HZ
MAX_LOG_BYTES = 32 * 1024 * 1024
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


class TaggedEchoError(ValueError):
    """Invalid source configuration or unavailable/untrustworthy evidence."""


def _number(value, label, *, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or (positive and value == 0)):
        raise TaggedEchoError(f"{label} must be a finite {'positive' if positive else 'nonnegative'} number")
    return float(value)


def _tag(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise TaggedEchoError(f"{label} must be a nonempty string of at most 128 characters")
    return value


def boot_clock_id():
    """Identity for time.monotonic() on this Linux host and boot."""
    return _tag(Path(BOOT_ID_PATH).read_text(encoding="ascii").strip(), "boot_id")


def _clock_id(explicit):
    return boot_clock_id() if explicit is None else _tag(explicit, "clockId")


def _ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, TypeError) as exc:
        raise TaggedEchoError("an explicit IPv4 address is required (no DNS)") from exc
    if address.is_unspecified or address.is_multicast or int(address) == 0xffffffff:
        raise TaggedEchoError("wildcard, multicast and broadcast addresses are not allowed")
    return str(address)


def _port(port):
    if type(port) is not int or not 1 <= port <= 65535:
        raise TaggedEchoError("port must be in 1..65535")
    return port


def _limits(duration, rate_hz, payload_bytes=256, reply_drain=0):
    duration = _number(duration, "duration", positive=True)
    rate_hz = _number(rate_hz, "rate", positive=True)
    reply_drain = _number(reply_drain, "reply drain")
    if duration + reply_drain > MAX_RUNTIME_SECONDS or reply_drain > MAX_DRAIN_SECONDS:
        raise TaggedEchoError("duration plus drain must be <=3600 seconds; drain must be <=10 seconds")
    if rate_hz > MAX_RATE_HZ:
        raise TaggedEchoError("rate must be <=20 Hz")
    if type(payload_bytes) is not int or not 1 <= payload_bytes <= MAX_PAYLOAD_BYTES:
        raise TaggedEchoError("UDP payload must be in 1..1200 bytes, including tags")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TaggedEchoError("duplicate JSON key")
        result[key] = value
    return result


def _bad_constant(_value):
    raise TaggedEchoError("non-finite JSON number")


def _json(data):
    try:
        result = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_bad_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise TaggedEchoError("malformed JSON record") from exc
    if not isinstance(result, dict):
        raise TaggedEchoError("JSON record must be an object")
    return result


def _sequence(value):
    if type(value) is not int or not 0 <= value < MAX_SEQUENCES:
        raise TaggedEchoError("invalid sequence number")
    return value


def make_packet(session_id, flow_id, seq, payload_bytes):
    """Fixed-size UDP payload; the server reflects these exact bytes."""
    packet = json.dumps({"schemaVersion": PACKET_SCHEMA, "sessionId": session_id,
                         "flowId": flow_id, "seq": seq}, separators=(",", ":")).encode("ascii")
    if len(packet) > payload_bytes:
        raise TaggedEchoError("payload is too small for the session/flow tags")
    return packet.ljust(payload_bytes, b" ")


def _packet_sequence(data, session_id, flow_id):
    if len(data) > MAX_PAYLOAD_BYTES:
        raise TaggedEchoError("oversized packet")
    packet = _json(data)
    if (set(packet) != {"schemaVersion", "sessionId", "flowId", "seq"}
            or packet.get("schemaVersion") != PACKET_SCHEMA
            or packet.get("sessionId") != session_id or packet.get("flowId") != flow_id):
        raise TaggedEchoError("foreign packet")
    return _sequence(packet.get("seq"))


def read_interface(name=INTERFACE):
    """Read Linux IFF_UP, current IPv4 and ifindex, never process liveness.

    A released OAI tun can retain its IPv4 while IFF_UP is cleared. Both are
    checked, and ifindex catches a deleted/recreated tun with the same address.
    """
    request = struct.pack("256s", name.encode("ascii"))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        flags = struct.unpack_from("H", fcntl.ioctl(control, 0x8913, request), 16)[0]
        index = struct.unpack_from("i", fcntl.ioctl(control, 0x8933, request), 16)[0]
        address = socket.inet_ntoa(fcntl.ioctl(control, 0x8915, request)[20:24])
    return {"name": name, "ip": address, "ifindex": index, "up": bool(flags & 1)}


def _interface(identity):
    if (not isinstance(identity, dict) or identity.get("name") != INTERFACE
            or identity.get("up") is not True or type(identity.get("ifindex")) is not int
            or identity["ifindex"] <= 0):
        raise TaggedEchoError("tun is missing/down or its interface identity is invalid")
    return {"name": INTERFACE, "ip": _ipv4(identity.get("ip")),
            "ifindex": identity["ifindex"], "up": True}


class _Log:
    def __init__(self, path, session_id, flow_id, clock_id):
        # O_EXCL refuses existing paths, including symlinks; O_APPEND forbids
        # seeking back over an earlier event. Flush issued BEFORE attempting send.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
        try:
            # Binding the tun may require sudo; the SSH observer still runs as
            # the invoking user. Keep mode 0600, but let that user read the log.
            if os.geteuid() == 0 and os.environ.get("SUDO_UID") is not None:
                os.fchown(descriptor, int(os.environ["SUDO_UID"]),
                          int(os.environ.get("SUDO_GID", -1)))
            self.file = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        self.common = {"schemaVersion": LOG_SCHEMA, "sessionId": session_id,
                       "flowId": flow_id, "clockId": clock_id}

    def emit(self, event, at_ms, **fields):
        self.file.write(json.dumps(dict(self.common, event=event, atMs=at_ms, **fields),
                                   separators=(",", ":"), allow_nan=False) + "\n")
        self.file.flush()

    def close(self):
        self.file.close()


def run_client(*, server_ip, port, session_id, flow_id, log_path, duration=60,
               rate_hz=5, payload_bytes=256, reply_drain=1, clock_id=None,
               interface=INTERFACE, socket_factory=None, wait=None,
               monotonic=None, interface_reader=None):
    """Run explicitly; injected socket/wait/clock/interface functions enable hermetic tests.

    duration is the issuing window, reply_drain is the final receive-only window.
    Their sum is <=3600 seconds. Rate is fixed; delayed sends never catch up in a
    burst. A send failure remains an issued request, not a missing denominator.
    """
    socket_factory = socket.socket if socket_factory is None else socket_factory
    wait = select.select if wait is None else wait
    monotonic = time.monotonic if monotonic is None else monotonic
    interface_reader = read_interface if interface_reader is None else interface_reader
    _limits(duration, rate_hz, payload_bytes, reply_drain)
    server_ip, port = _ipv4(server_ip), _port(port)
    _tag(session_id, "sessionId")
    _tag(flow_id, "flowId")
    if interface != INTERFACE:
        raise TaggedEchoError("client must bind explicitly to oaitun_ue1")
    # Reserve enough space even when the sequence grows during a long run.
    make_packet(session_id, flow_id, MAX_SEQUENCES - 1, payload_bytes)
    log = _Log(log_path, session_id, flow_id, _clock_id(clock_id))
    channel = None
    status = "finished"
    start = monotonic()
    try:
        log.emit("start", start * 1000, status="starting", interfaceName=interface,
                 server={"ip": server_ip, "port": port}, durationSeconds=duration,
                 rateHz=rate_hz, payloadBytes=payload_bytes, replyDrainSeconds=reply_drain)
        initial = _interface(interface_reader(interface))
        if not hasattr(socket, "SO_BINDTODEVICE"):
            raise TaggedEchoError("SO_BINDTODEVICE is unavailable; refusing an unbound socket")

        def open_channel(identity):
            opened = socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                opened.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode("ascii") + b"\0")
                opened.bind((identity["ip"], 0))
                opened.connect((server_ip, port))
                opened.setblocking(False)
            except BaseException:
                opened.close()
                raise
            return opened

        def current_interface():
            try:
                return _interface(interface_reader(interface))
            except (OSError, TaggedEchoError):
                return None

        channel = open_channel(initial)
        bound, bind_epoch = initial, 0
        issued, replied, failed, voided = {}, set(), set(), set()
        last_good_ms = start * 1000  # the last moment the bound tun was seen as bound
        next_issue = next_heartbeat = start
        issue_end, finish = start + duration, start + duration + reply_drain
        while True:
            now = monotonic()
            if now >= finish:
                break
            if now >= next_heartbeat:
                current = current_interface()
                if current != bound or channel is None:
                    # The UE re-registered or its tun went down (docs/design/
                    # ue-identity-continuity.md).  Replies to the requests still pending
                    # are no longer accepted; which of them the gap excused is decided per
                    # deadline by snapshot() from ``lastGoodAtMs``, so a request whose
                    # deadline had already passed stays a miss.  Nothing is issued until a
                    # tun is bound again -- the same one coming back included -- and no
                    # heartbeat is written meanwhile, so the gap reads as missing.
                    if channel is not None:
                        pending = sorted(seq for seq in issued
                                         if seq not in replied and seq not in failed and seq not in voided)
                        voided.update(pending)
                        log.emit("rebind-wait", monotonic() * 1000, voidedSeqs=pending,
                                 lastGoodAtMs=last_good_ms)
                        channel.close()
                        channel = None
                    if current is not None:
                        try:
                            channel = open_channel(current)
                        except OSError:
                            channel = None  # the new tun is not bindable yet; the next heartbeat retries
                        if channel is not None:
                            bound, bind_epoch = current, bind_epoch + 1
                            log.emit("rebind", monotonic() * 1000, interface=current, bindEpoch=bind_epoch)
                            next_issue = monotonic()
                if channel is not None:
                    now = monotonic()
                    log.emit("heartbeat", now * 1000, status="running", interface=bound)
                    last_good_ms = now * 1000
                next_heartbeat = monotonic() + HEARTBEAT_SECONDS
            if channel is not None and now < issue_end and now >= next_issue:
                seq = len(issued)
                if seq >= MAX_SEQUENCES:
                    raise TaggedEchoError("sequence bound exceeded")
                issued[seq] = now * 1000
                log.emit("issued", now * 1000, seq=seq, issuedAtMs=now * 1000)
                packet = make_packet(session_id, flow_id, seq, payload_bytes)
                try:
                    if channel.send(packet) != len(packet):
                        raise OSError("short UDP send")
                except OSError as exc:
                    failed.add(seq)
                    log.emit("send-error", monotonic() * 1000, seq=seq, error=str(exc))
                next_issue = monotonic() + 1.0 / rate_hz
            # Nothing is issued while unbound, so a past issue time must not spin the loop.
            wake = min(finish, next_heartbeat,
                       next_issue if channel is not None and now < issue_end
                       and next_issue < issue_end else finish)
            readable, _, _ = wait([channel] if channel is not None else [], [], [],
                                  max(0.0, wake - monotonic()))
            if monotonic() >= finish:
                break
            if not readable or channel is None:
                continue
            try:
                data = channel.recv(MAX_PAYLOAD_BYTES + 1)
            except BlockingIOError:
                continue
            except ConnectionError:
                # An ICMP port-unreachable for an earlier datagram surfaces on the next
                # read of a connected UDP socket.  The server owns its own window and
                # starts a second before this client (2026-09-16 attempt 116: ue3's echo
                # server ended at 600 s and the client, one second behind, died with
                # ECONNREFUSED after 2428 answered requests).  An unanswered request is
                # already a miss; the source is not a failure for it.
                continue
            reply_at = monotonic() * 1000
            try:
                seq = _packet_sequence(data, session_id, flow_id)
            except TaggedEchoError:
                continue  # Untrusted network packets are not source records.
            if (seq not in issued or seq in replied or seq in failed or seq in voided
                    or data != make_packet(session_id, flow_id, seq, payload_bytes)):
                continue
            rtt = _number(reply_at - issued[seq], "RTT")
            log.emit("reply", reply_at, seq=seq, rttMs=rtt)
            replied.add(seq)
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "source-failure"
        log.emit("heartbeat", monotonic() * 1000, status=status, error=str(exc))
        raise TaggedEchoError(f"tagged echo {status}: {exc}") from exc
    finally:
        try:
            log.emit("end", monotonic() * 1000, status=status)
        finally:
            log.close()
            if channel is not None:
                channel.close()


def run_server(*, bind_ip, port, session_id, flow_id, duration=60, rate_hz=20,
               allowed_subnet="12.1.1.0/24", socket_factory=None,
               wait=None, monotonic=None):
    """Finite explicit IPv4 UDP reflector, <=20 replies/s total, no amplification."""
    socket_factory = socket.socket if socket_factory is None else socket_factory
    wait = select.select if wait is None else wait
    monotonic = time.monotonic if monotonic is None else monotonic
    _limits(duration, rate_hz)
    bind_ip, port = _ipv4(bind_ip), _port(port)
    _tag(session_id, "sessionId")
    _tag(flow_id, "flowId")
    try:
        allowed = ipaddress.IPv4Network(allowed_subnet)
    except (ValueError, TypeError) as exc:
        raise TaggedEchoError("allowed subnet must be an explicit IPv4 network") from exc
    channel = socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        channel.bind((bind_ip, port))
        channel.setblocking(False)
        finish, next_reply = monotonic() + duration, monotonic()
        while monotonic() < finish:
            readable, _, _ = wait([channel], [], [], max(0.0, finish - monotonic()))
            if not readable or monotonic() >= finish:
                continue
            try:
                data, peer = channel.recvfrom(MAX_PAYLOAD_BYTES + 1)
            except BlockingIOError:
                continue
            now = monotonic()
            try:
                if ipaddress.IPv4Address(peer[0]) not in allowed:
                    continue
                _packet_sequence(data, session_id, flow_id)
            except (ValueError, TypeError):
                continue
            if now < next_reply:
                continue
            next_reply = now + 1.0 / rate_hz
            try:
                channel.sendto(data, peer)
            except OSError:
                pass  # No retry or amplified traffic after a failed reply.
    finally:
        channel.close()


def snapshot(log_path, session_id, flow_id, deadlines_ms, max_age_ms, *,
             clock_id=None, monotonic=None, window_start_ms=None, window_end_ms=None):
    """Return counters at one complete heartbeat on the source host/boot.

    All complete records (including the suffix after the boundary) are validated.
    Exact issue/reply/send-error duplicates count once; conflicts fail closed.
    Reader time is used only to establish freshness, NEVER to mature requests.

    Supplying BOTH window bounds restricts the cohort to the requests issued in
    ``[window_start_ms, window_end_ms)`` on the source clock. Each such request
    keeps its full deadline: the observation is valid only if collection reached
    the window end AND the last issue plus that deadline. An early-ended
    collection or an empty cohort is marked invalid instead of being turned into
    a ratio -- zero issued requests establish neither a zero nor a success. With
    no bounds the cohort, the counters and the returned keys are unchanged.
    """
    monotonic = time.monotonic if monotonic is None else monotonic
    if (window_start_ms is None) != (window_end_ms is None):
        raise TaggedEchoError("window start and end must be supplied together")
    window = None
    if window_start_ms is not None:
        window = (_number(window_start_ms, "window start"), _number(window_end_ms, "window end"))
        if window[1] <= window[0]:
            raise TaggedEchoError("window end must be after window start")
    _tag(session_id, "sessionId")
    _tag(flow_id, "flowId")
    expected_clock = _clock_id(clock_id)
    max_age_ms = _number(max_age_ms, "max age", positive=True)
    deadlines = {}
    for value in deadlines_ms:
        deadline = _number(value, "deadline", positive=True)
        key = format(deadline, ".12g")
        if key in deadlines and deadlines[key] != deadline:
            raise TaggedEchoError("deadline keys collide after .12g formatting")
        deadlines[key] = deadline
    if not deadlines:
        raise TaggedEchoError("at least one deadline is required")
    with open(log_path, "rb") as source:
        if os.fstat(source.fileno()).st_size > MAX_LOG_BYTES:
            raise TaggedEchoError("source log exceeds 32 MiB; raw evidence was not truncated")
        data = source.read(MAX_LOG_BYTES + 1)
    if len(data) > MAX_LOG_BYTES:
        raise TaggedEchoError("source log exceeds 32 MiB; raw evidence was not truncated")
    # Concurrent writer may have flushed only part of its last line. Everything
    # ending in a newline is committed evidence and must parse successfully.
    data = data[:data.rfind(b"\n") + 1]
    issues, replies, failures = {}, {}, {}
    voided, bind_epoch, waiting, gaps = set(), 0, False, []
    boundary = identity = None
    previous_at = -1.0
    started = False
    for index, line in enumerate(data.splitlines()):
        record = _json(line)
        if (record.get("schemaVersion") != LOG_SCHEMA or record.get("sessionId") != session_id
                or record.get("flowId") != flow_id or record.get("clockId") != expected_clock):
            raise TaggedEchoError("log schema/session/flow/clock does not match this source boot")
        event = record.get("event")
        at = _number(record.get("atMs"), "event time")
        if event in ("issued", "reply", "send-error"):
            seq = _sequence(record.get("seq"))
            table = {"issued": issues, "reply": replies, "send-error": failures}[event]
            if seq in table:
                if record != table[seq][0]:
                    raise TaggedEchoError(f"conflicting {event} for sequence {seq}")
                continue
        if at < previous_at:
            raise TaggedEchoError("reordered event time")
        previous_at = at
        if event == "start":
            if started or index != 0 or record.get("interfaceName") != INTERFACE:
                raise TaggedEchoError("invalid or repeated start")
            started = True
            continue
        if not started:
            raise TaggedEchoError("log has no initial start")
        if event == "issued":
            if _number(record.get("issuedAtMs"), "issue time") != at:
                raise TaggedEchoError("issue time differs from monotonic event time")
            issues[seq] = (record, index)
        elif event in ("reply", "send-error"):
            if seq not in issues or seq in voided:
                raise TaggedEchoError("unknown/reordered/voided reply or send-error")
            if event == "reply":
                rtt = _number(record.get("rttMs"), "RTT")
                if seq in failures or not math.isclose(at - issues[seq][0]["issuedAtMs"], rtt,
                                                       rel_tol=1e-9, abs_tol=1e-3):
                    raise TaggedEchoError("reply conflicts with issue time or send failure")
                replies[seq] = (record, index)
            else:
                if seq in replies or not isinstance(record.get("error"), str) or not record["error"]:
                    raise TaggedEchoError("invalid/conflicting send-error")
                failures[seq] = (record, index)
        elif event == "rebind-wait":
            pending = record.get("voidedSeqs")
            if waiting or not isinstance(pending, list):
                raise TaggedEchoError("repeated rebind-wait or one without its voided sequences")
            last_good = _number(record.get("lastGoodAtMs"), "last good time")
            if last_good > at:
                raise TaggedEchoError("last good time is after the wait")
            for seq in pending:
                seq = _sequence(seq)
                if seq not in issues or seq in replies or seq in failures or seq in voided:
                    raise TaggedEchoError("voided sequence was not pending")
                voided.add(seq)
            # The last pre-gap heartbeat is not a boundary for a source that is waiting.
            waiting, boundary = True, None
            gaps.append([last_good, math.inf])
        elif event == "rebind":
            current = _interface(record.get("interface"))
            if not waiting or record.get("bindEpoch") != bind_epoch + 1:
                raise TaggedEchoError("rebind without a wait, or an epoch that does not advance by one")
            bind_epoch, waiting, identity = bind_epoch + 1, False, current
            gaps[-1][1] = at
        elif event == "heartbeat":
            if waiting:
                raise TaggedEchoError("heartbeat while the source waits for a rebind")
            if record.get("status") != "running":
                raise TaggedEchoError("source heartbeat is not running")
            current = _interface(record.get("interface"))
            if identity is not None and current != identity:
                raise TaggedEchoError("source interface identity changed")
            identity = current
            boundary = (index, at)
        elif event == "end":
            raise TaggedEchoError("source has ended")
        else:
            raise TaggedEchoError("unknown source event")
    if boundary is None:
        raise TaggedEchoError("no complete running heartbeat")
    remote_now = _number(monotonic() * 1000, "remote monotonic time")
    boundary_index, observed_at = boundary
    if not 0 <= remote_now - observed_at <= max_age_ms:
        raise TaggedEchoError("source heartbeat is stale or in the future on this boot")
    def excused(issued_at, reply, deadline):
        """A request a disconnect gap excused for this deadline: not answered in time, and
        its deadline still open when the gap began (or issued inside the gap)."""
        if reply is not None and reply[1] < boundary_index and reply[0]["rttMs"] <= deadline:
            return False
        return any(issued_at < end and issued_at + deadline > begin for begin, end in gaps)

    counters = {}
    for key, deadline in deadlines.items():
        issued = eligible = completed = excluded = 0
        last_issue = None
        for seq, (record, index) in issues.items():
            if index >= boundary_index:
                continue
            issued_at = record["issuedAtMs"]
            if window is not None and not window[0] <= issued_at < window[1]:
                continue
            if excused(issued_at, replies.get(seq), deadline):
                excluded += 1
                continue
            issued += 1
            last_issue = issued_at if last_issue is None else max(last_issue, issued_at)
            if issued_at + deadline <= observed_at:
                eligible += 1
                reply = replies.get(seq)
                if reply is not None and reply[1] < boundary_index and reply[0]["rttMs"] <= deadline:
                    completed += 1
        counters[key] = {"issued": issued, "eligible": eligible, "completed": completed}
        if gaps:
            counters[key]["gapExcluded"] = excluded
        if window is None:
            continue
        # A cohort with no request, or one whose collection stopped before the
        # window end or before the last request could still answer in time, is
        # not a service result. Report it as invalid, never as 0.0 or 1.0.
        if issued == 0:
            counters[key].update(valid=False, invalidReason="no-issued-requests")
        elif observed_at < max(window[1], last_issue + deadline):
            counters[key].update(valid=False, invalidReason="collection-ended-early")
        else:
            counters[key]["valid"] = True
    result = {"schemaVersion": SNAPSHOT_SCHEMA, "sessionId": session_id, "flowId": flow_id,
              "clockId": expected_clock, "remoteNowMs": remote_now, "observedAtMs": observed_at,
              "status": "running", "countersByDeadlineMs": counters,
              "sourceLog": os.path.abspath(log_path), "interface": identity,
              "bindEpoch": bind_epoch, "voidedRequests": len(voided)}
    if window is not None:
        # Enough to audit the cohort without shipping it: which sequences were
        # issued in the window (count, ends, digest) and when the first and
        # last of them went out.  Bounded however long the window is.
        cohort = sorted((seq, record["issuedAtMs"]) for seq, (record, index) in issues.items()
                        if index < boundary_index
                        and window[0] <= record["issuedAtMs"] < window[1]
                        and not all(excused(record["issuedAtMs"], replies.get(seq), deadline)
                                    for deadline in deadlines.values()))
        stamps = [at for _seq, at in cohort]
        result["window"] = {"startMs": window[0], "endMs": window[1],
                            "valid": all(row["valid"] for row in counters.values()),
                            "issued": len(cohort),
                            "firstSeq": cohort[0][0] if cohort else None,
                            "lastSeq": cohort[-1][0] if cohort else None,
                            "firstIssuedAtMs": min(stamps, default=None),
                            "lastIssuedAtMs": max(stamps, default=None),
                            "seqSha256": hashlib.sha256(",".join(
                                str(seq) for seq, _at in cohort).encode("ascii")).hexdigest()}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("server", help="run a bounded explicit ext-DN UDP reflector")
    client = commands.add_parser("client", help="run the UE-bound source and preserve raw JSONL")
    reader = commands.add_parser("snapshot", help="read a running log on its source UE/boot")
    for command in (server, client, reader):
        command.add_argument("--session-id", required=True)
        command.add_argument("--flow-id", required=True)
    for command in (server, client):
        command.add_argument("--port", required=True, type=int)
        command.add_argument("--duration-s", dest="duration", type=float, default=60)
        command.add_argument("--rate-hz", type=float, default=20 if command is server else 5)
    server.add_argument("--bind-ip", required=True, help="explicit ext-DN IPv4; no wildcard/DNS")
    server.add_argument("--allow-subnet", dest="allowed_subnet", default="12.1.1.0/24")
    client.add_argument("--server-ip", required=True, help="explicit ext-DN IPv4; no DNS")
    client.add_argument("--interface", default=INTERFACE, choices=[INTERFACE])
    client.add_argument("--payload-bytes", type=int, default=256)
    client.add_argument("--reply-drain-s", dest="reply_drain", type=float, default=1)
    for command in (client, reader):
        command.add_argument("--log", dest="log_path", required=True)
        command.add_argument("--clock-id", help="explicit stable same-boot identity (default Linux boot_id)")
    reader.add_argument("--deadlines-ms", required=True, help="comma-separated positive milliseconds")
    reader.add_argument("--max-age-ms", required=True, type=float)
    for bound in ("start", "end"):
        reader.add_argument(f"--window-{bound}-ms", dest=f"window_{bound}_ms", type=float,
                            help=f"source-clock window {bound}; both bounds or neither")
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    try:
        if command == "snapshot":
            args["deadlines_ms"] = [float(value) for value in args["deadlines_ms"].split(",")]
            result = snapshot(**args)
            print(json.dumps(result, separators=(",", ":"), allow_nan=False))
        elif command == "client":
            run_client(**args)
        else:
            run_server(**args)
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        print(f"tagged-echo: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
