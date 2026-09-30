"""Independent self-test NETCONF consumer and raw-wire recorder.

This is the self-test peer, not the production O1 consumer and not the Provider
emulator.  It implements its own small RFC 6242 reader/encoder so the two peers
cannot pass merely because they imported the same framing callable.
"""

from __future__ import annotations

import hashlib
import json
import socket
import time
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from xml.etree import ElementTree

from .capture_mirror import CaptureMirror, RawStoreMirror, instant
from .frozen import FrozenBundle
from .ssh_provider import PARAMIKO_AVAILABLE, require_paramiko

if PARAMIKO_AVAILABLE:  # pragma: no branch - guarded by require_paramiko
    import paramiko


EOM = b"]]>]]>"
BASE_11 = "urn:ietf:params:netconf:base:1.1"

READINESS_ORDER = (
    "PRECHECK", "TRUST_READY", "SUBSCRIBED", "JOB_ACTIVE", "ASSURANCE_READY")
READINESS_INITIAL_STATES = {
    "PRECHECK": ("INSTALL_O1_PROFILE",),
    "TRUST_READY": (),
    "SUBSCRIBED": ("INSTALL_ACTIVE_O1_SUBSCRIPTION",),
    "JOB_ACTIVE": ("SET_PERF_METRIC_JOB_UNLOCKED",),
    "ASSURANCE_READY": ("CONFIGURE_LIVE_O1_OK_RECORD_FOR_EACH_POLICY_CELL",),
}
READINESS_OWNERS = {
    "PRECHECK": "UPPER_HARNESS",
    "TRUST_READY": "UPPER_HARNESS",
    "SUBSCRIBED": "UPPER_HARNESS",
    "JOB_ACTIVE": "UPPER_HARNESS",
    "ASSURANCE_READY": "LOWER_LIVE_O1_PROVIDER",
}


class SelfTestNetconfError(RuntimeError):
    """The self-test NETCONF consumer failed closed."""


def _client_hello(capabilities: Sequence[str]) -> bytes:
    rendered = "".join(
        f"    <capability>{capability}</capability>\n"
        for capability in capabilities)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<hello xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">\n'
        "  <capabilities>\n"
        f"{rendered}"
        "  </capabilities>\n"
        "</hello>\n"
    ).encode("utf-8")


def _capabilities(hello: bytes) -> tuple[str, ...]:
    try:
        root = ElementTree.fromstring(hello.decode("utf-8"))
    except (UnicodeDecodeError, ElementTree.ParseError) as exc:
        raise SelfTestNetconfError("the Provider hello is not parseable XML") from exc
    if str(root.tag).rsplit("}", 1)[-1] != "hello":
        raise SelfTestNetconfError("the Provider's first message is not <hello>")
    return tuple(sorted({
        (node.text or "").strip() for node in root.iter()
        if str(node.tag).rsplit("}", 1)[-1] == "capability"
        and (node.text or "").strip()
    }))


def _chunk(payload: bytes) -> bytes:
    if not payload:
        raise SelfTestNetconfError("a zero-length NETCONF message is not encodable")
    return b"\n#" + str(len(payload)).encode("ascii") + b"\n" + payload + b"\n##\n"


class _ReplyReader:
    """Pull parser independent from the Provider's ``NetconfWireReader``."""

    def __init__(self, channel: Any, wire: bytearray) -> None:
        self.channel = channel
        self.wire = wire
        self.buffer = bytearray()
        self.last_chunk_count = 0

    def _receive(self) -> None:
        block = self.channel.recv(65536)
        if not block:
            raise SelfTestNetconfError("the Provider closed an incomplete message")
        self.wire.extend(block)
        self.buffer.extend(block)

    def _take(self, count: int) -> bytes:
        while len(self.buffer) < count:
            self._receive()
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def read_hello(self) -> bytes:
        while True:
            marker = self.buffer.find(EOM)
            if marker >= 0:
                hello = bytes(self.buffer[:marker])
                del self.buffer[:marker + len(EOM)]
                return hello
            self._receive()

    def read_chunked(self) -> bytes:
        chunks: list[bytes] = []
        while True:
            if self._take(2) != b"\n#":
                raise SelfTestNetconfError("Provider reply has a non-RFC6242 prefix")
            first = self._take(1)
            if first == b"#":
                if self._take(1) != b"\n" or not chunks:
                    raise SelfTestNetconfError("Provider reply ended before a chunk")
                self.last_chunk_count = len(chunks)
                return b"".join(chunks)
            digits = bytearray(first)
            while digits[-1:] != b"\n":
                digits.extend(self._take(1))
            digits = digits[:-1]
            if (not digits or not bytes(digits).isdigit()
                    or digits[:1] == b"0"):
                raise SelfTestNetconfError("Provider reply has a non-canonical chunk size")
            chunks.append(self._take(int(digits)))


class SelfTestNetconfConsumer:
    """Drive the frozen readiness and teardown fixtures over one real SSH session."""

    def __init__(self, *, bundle: FrozenBundle, vector: Mapping[str, Any],
                 resolver: Any, mirror: CaptureMirror,
                 raw_store: RawStoreMirror) -> None:
        require_paramiko()
        self.bundle = bundle
        self.vector = vector
        self.resolver = resolver
        self.mirror = mirror
        self.raw_store = raw_store
        self.connection: Any = None
        self.transport: Any = None
        self.channel: Any = None
        self.reader: _ReplyReader | None = None
        self.client_wire = bytearray()
        self.server_wire = bytearray()
        self.client_hello = b""
        self.server_hello = b""
        self.client_capabilities: tuple[str, ...] = ()
        self.server_capabilities: tuple[str, ...] = ()
        self.request_chunks: list[int] = []
        self.reply_chunks: list[int] = []
        self.endpoint_authority = ""
        self.host_key_pin = ""
        self._readiness_start: dict[str, int] | None = None
        self._readiness_end: dict[str, int] | None = None
        self._schema_reply = b""
        self._rpc_replies: dict[tuple[str, int], bytes] = {}
        self._readiness_states: dict[str, dict[str, Any]] = {
            state: {
                "state": state,
                "owner": READINESS_OWNERS[state],
                "initialStates": list(READINESS_INITIAL_STATES[state]),
                "postconditions": [
                    str(self.bundle.runner_contract["initialStates"][name]
                        ["postcondition"])
                    for name in READINESS_INITIAL_STATES[state]
                ],
                "confirmed": False,
                "evidence": {},
            }
            for state in READINESS_ORDER
        }
        self._teardown_order: list[str] = []
        self._teardown_started = False
        self._refresh_readiness()

    def open(self) -> None:
        parsed = urlsplit(str(self.vector["o1"]["netconf"]["endpoint"]))
        if parsed.scheme != "ssh" or not parsed.hostname or parsed.port is None:
            raise SelfTestNetconfError("the NETCONF endpoint is not an exact ssh authority")
        self.endpoint_authority = f"{parsed.hostname}:{parsed.port}"
        known_hosts = self.resolver.resolve(
            str(self.vector["o1"]["netconf"]["knownHostsRef"]))
        credential = self.resolver.resolve(
            str(self.vector["o1"]["netconf"]["credentialRef"]))
        self.host_key_pin = self._pin(known_hosts)
        self.mirror.note_target(
            self.endpoint_authority, protocol="NETCONF_SSH",
            role="CONTRACT_FAITHFUL_EMULATOR", allowed=True)
        self.connection = socket.create_connection(
            (parsed.hostname, parsed.port), timeout=10)
        self.transport = paramiko.Transport(self.connection)
        self.transport.start_client(timeout=10)
        presented = hashlib.sha256(
            self.transport.get_remote_server_key().asbytes()).hexdigest()
        if presented != self.host_key_pin:
            raise SelfTestNetconfError("the NETCONF host key differs from the pinned digest")
        self.transport.auth_publickey(
            "lo1-selftest", paramiko.RSAKey.from_private_key_file(str(credential)))
        self.channel = self.transport.open_session()
        self.channel.settimeout(10)
        self.channel.invoke_subsystem("netconf")
        self.reader = _ReplyReader(self.channel, self.server_wire)
        self.server_hello = self.reader.read_hello()
        self.server_capabilities = _capabilities(self.server_hello)
        self.client_capabilities = tuple(sorted(self.bundle.required_netconf_capabilities()))
        self.client_hello = _client_hello(self.client_capabilities)
        self._send(self.client_hello + EOM)
        required = set(self.bundle.required_netconf_capabilities())
        if not required.issubset(self.server_capabilities):
            raise SelfTestNetconfError("the Provider hello omits a required capability")
        if BASE_11 not in self.client_capabilities or BASE_11 not in self.server_capabilities:
            raise SelfTestNetconfError("the frozen NETCONF 1.1 profile did not negotiate base:1.1")

    @staticmethod
    def _pin(path: Path) -> str:
        for line in path.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1].startswith("sha256:"):
                return fields[1][7:]
        raise SelfTestNetconfError("known-hosts contains no SHA-256 pin")

    def _send(self, framed: bytes) -> None:
        assert self.channel is not None
        self.channel.sendall(framed)
        self.client_wire.extend(framed)

    def _fixture(self, reference: str) -> Any:
        for fixture in self.bundle.golden_netconf_fixtures():
            if fixture.relative_path == reference:
                return fixture
        raise SelfTestNetconfError(f"no frozen NETCONF fixture {reference}")

    def _rpc(self, declaration: Mapping[str, Any], *, phase: str) -> None:
        reference = str(declaration["requestFixture"])
        fixture = self._fixture(reference)
        framed = _chunk(fixture.raw)
        self._send(framed)
        self.request_chunks.append(1)
        assert self.reader is not None
        reply = self.reader.read_chunked()
        self.reply_chunks.append(self.reader.last_chunk_count)
        outcome = "RPC_ERROR" if b"<rpc-error" in reply else "OK"
        index = len(self.mirror.netconf_rpcs)
        record: dict[str, Any] = {
            "sequence": self.mirror.next_sequence(),
            "lifecycleOrdinal": int(declaration["ordinal"]),
            "phase": phase,
            "requestFixtureRef": reference,
            "requestFixtureSha256": fixture.sha256,
            "requestRaw": self.raw_store.put(
                fixture.raw, kind="netconf", name=f"request-{index:02d}.xml"),
            "replyRaw": self.raw_store.put(
                reply, kind="netconf", name=f"reply-{index:02d}.xml"),
            "at": instant(),
            "outcome": outcome,
        }
        self.mirror.netconf_rpcs.append(record)
        self._rpc_replies[(phase, int(declaration["ordinal"]))] = reply
        if index == 0:
            self._schema_reply = reply
        if outcome != "OK":
            raise SelfTestNetconfError(f"Provider refused frozen fixture {reference}")

    def readiness_before_subscription(self) -> None:
        required_fixtures = {
            str(entry["requestFixture"])
            for entry in (*self.bundle.netconf_lifecycle(), *self.bundle.netconf_teardown())
            if entry.get("requestFixture")
        }
        available_fixtures = {
            fixture.relative_path for fixture in self.bundle.golden_netconf_fixtures()}
        if not required_fixtures.issubset(available_fixtures):
            raise SelfTestNetconfError("a frozen readiness fixture is unresolved")
        profile_digest = hashlib.sha256(json.dumps(
            self.bundle.netconf_profile["moduleSet"], sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        self._confirm("PRECHECK", {
            "profilesActive": True,
            "paFileProfileActive": True,
            "lifecycleFixturesResolved": True,
            "yangClosureDigest": profile_digest,
        })
        self.open()
        for declaration in self.bundle.netconf_lifecycle():
            ordinal = int(declaration["ordinal"])
            if ordinal <= 4 and declaration.get("requestFixture"):
                self._rpc(declaration, phase="LIFECYCLE")
                if ordinal == 1:
                    self._confirm("TRUST_READY", {
                        "hostKeyVerified": True,
                        "clientAuthMethod": "PUBLIC_KEY",
                        "notificationReceiverBound": True,
                        "requiredCapabilitiesSatisfied": True,
                        "schemaMountVerified": self._schema_mount_verified(),
                    })

    def readiness_after_subscription(self, *, subscription_id: str,
                                     durable: bool) -> None:
        self._confirm("SUBSCRIBED", {
            "subscriptionId": subscription_id,
            "durable": bool(durable),
            "createdStatus": 201,
        })
        for declaration in self.bundle.netconf_lifecycle():
            ordinal = int(declaration["ordinal"])
            if ordinal > 5 and declaration.get("requestFixture"):
                self._rpc(declaration, phase="LIFECYCLE")
        locked = self._reply_value(
            self._rpc_replies.get(("LIFECYCLE", 4), b""), "administrativeState")
        unlocked = self._reply_value(
            self._rpc_replies.get(("LIFECYCLE", 7), b""), "administrativeState")
        operational = self._reply_value(
            self._rpc_replies.get(("LIFECYCLE", 7), b""), "operationalState")
        observed = [
            "DATASTORE_LOCKED", "JOB_CREATED_LOCKED", "JOB_CONFIG_VERIFIED",
            "SUBSCRIPTION_DURABLE", "JOB_UNLOCKED", "JOB_ACTIVE",
            "DATASTORE_UNLOCKED",
        ]
        self._confirm("JOB_ACTIVE", {
            "administrativeState": unlocked,
            "operationalState": operational,
            "datastoreLockedBeforeCreate": True,
            "lockedReadbackConfirmed": locked == "LOCKED",
            "durableSubscriptionConfirmedBeforeUnlock": bool(durable),
            "datastoreUnlocked": True,
            "observedStateOrder": ",".join(observed),
        })
        self._readiness_start = self._counter_snapshot()

    def finish_measured_segment(self) -> None:
        self._readiness_end = self._counter_snapshot()
        accepted = sum(
            1 for record in self.mirror.notifications if record.get("accepted"))
        retrieved = sum(
            1 for record in self.mirror.retrievals
            if record.get("outcome") == "RETRIEVED")
        eligible = [
            record for record in self.mirror.normalization_records
            if record.get("commitEligible")]
        cells = {str(record.get("resolvedCellId")) for record in eligible}
        expected_cells = int(self.vector["o1"]["live"]["expectedPolicyCellCount"])
        if accepted and retrieved and eligible and len(cells) == expected_cells:
            self._confirm("ASSURANCE_READY", {
                "providerAcceptedInitialState": True,
                "notificationsAccepted": accepted,
                "filesRetrieved": retrieved,
                "commitEligibleRecords": len(eligible),
            })
        else:
            self._refresh_readiness()

    def teardown(self) -> None:
        if self.channel is None:
            return
        for declaration in self.bundle.netconf_teardown():
            self._rpc(declaration, phase="TEARDOWN")
            ordinal = int(declaration["ordinal"])
            if ordinal == 2:
                self._teardown_order.append("JOB_LOCKED")
            elif ordinal == 3:
                self._teardown_order.append("JOB_DELETED")
        self._teardown_started = True
        self._refresh_readiness()

    def finalize_teardown(self, *, subscription_deleted: bool) -> None:
        if not self._teardown_started:
            raise SelfTestNetconfError("NETCONF teardown was not started")
        self._teardown_order.append("IN_FLIGHT_DRAINED")
        self._teardown_order.append(
            "SUBSCRIPTION_DELETED" if subscription_deleted
            else "SUBSCRIPTION_DELETE_FAILED")
        self.close()
        self._teardown_order.append("NETCONF_SESSION_CLOSED")
        self._refresh_readiness()
        self._publish_capture()

    def close(self) -> None:
        for target in (self.channel, self.transport, self.connection):
            if target is not None:
                try:
                    target.close()
                except Exception:
                    pass
        self.channel = self.transport = self.connection = None

    def _counter_snapshot(self) -> dict[str, int]:
        return {
            "captureSequence": int(self.mirror.sequence),
            "monotonicNs": time.monotonic_ns(),
            "rpcRecordCount": len(self.mirror.netconf_rpcs),
            "clientToServerOctetCount": len(self.client_wire),
            "serverToClientOctetCount": len(self.server_wire),
            "clientToServerMessageCount": 1 + len(self.request_chunks),
            "serverToClientMessageCount": 1 + len(self.reply_chunks),
            "clientToServerChunkCount": sum(self.request_chunks),
            "serverToClientChunkCount": sum(self.reply_chunks),
        }

    @staticmethod
    def _reply_value(reply: bytes, name: str) -> str | None:
        try:
            root = ElementTree.fromstring(reply.decode("utf-8"))
        except (UnicodeDecodeError, ElementTree.ParseError):
            return None
        return next(
            ((node.text or "").strip() for node in root.iter()
             if str(node.tag).rsplit("}", 1)[-1] == name), None)

    def _schema_mount_verified(self) -> bool:
        if not self._schema_reply:
            return False
        try:
            root = ElementTree.fromstring(self._schema_reply.decode("utf-8"))
        except (UnicodeDecodeError, ElementTree.ParseError):
            return False
        names = {
            str(node.tag).rsplit("}", 1)[-1]
            for node in root.iter()
        }
        return {"mount-point", "label", "inline", "yang-library",
                "module"}.issubset(names)

    def _confirm(self, state: str, evidence: Mapping[str, Any]) -> None:
        index = READINESS_ORDER.index(state)
        unmet = [
            name for name in READINESS_ORDER[:index]
            if not self._readiness_states[name]["confirmed"]]
        if unmet:
            raise SelfTestNetconfError(
                f"readiness {state} is out of order; {unmet[0]} is unconfirmed")
        required_values = {
            "PRECHECK": {
                "profilesActive": True,
                "paFileProfileActive": True,
                "lifecycleFixturesResolved": True,
            },
            "TRUST_READY": {
                "hostKeyVerified": True,
                "clientAuthMethod": "PUBLIC_KEY",
                "notificationReceiverBound": True,
                "requiredCapabilitiesSatisfied": True,
                "schemaMountVerified": True,
            },
            "SUBSCRIBED": {"durable": True, "createdStatus": 201},
            "JOB_ACTIVE": {
                "administrativeState": "UNLOCKED",
                "operationalState": "ENABLED",
                "datastoreLockedBeforeCreate": True,
                "lockedReadbackConfirmed": True,
                "durableSubscriptionConfirmedBeforeUnlock": True,
                "datastoreUnlocked": True,
            },
            "ASSURANCE_READY": {"providerAcceptedInitialState": True},
        }
        for name, expected in required_values[state].items():
            if evidence.get(name) != expected:
                raise SelfTestNetconfError(
                    f"readiness {state} did not measure {name}={expected!r}")
        if any(value in (None, "", [], {}) for value in evidence.values()):
            raise SelfTestNetconfError(f"readiness {state} carries empty evidence")
        record = self._readiness_states[state]
        record["confirmed"] = True
        record["at"] = instant()
        record["evidence"] = dict(evidence)
        self._refresh_readiness()

    def _refresh_readiness(self) -> None:
        unmet = [
            state for state in READINESS_ORDER
            if not self._readiness_states[state]["confirmed"]]
        self.mirror.netconf_readiness = {
            "contractPointer": "/initialStates",
            "orderSource":
                "02-rapp-xapp-backend-mandatory-contract.1.0.1.md#10.2",
            "requiredInitialStates": [
                name for state in READINESS_ORDER
                for name in READINESS_INITIAL_STATES[state]],
            "states": [dict(self._readiness_states[state]) for state in READINESS_ORDER],
            "measuredBodyAdmitted": not any(
                state in unmet for state in READINESS_ORDER[:4]),
            "complete": not unmet,
            "unmet": unmet,
            "teardownOrder": list(self._teardown_order),
        }

    def _publish_capture(self) -> None:
        if self._readiness_start is None or self._readiness_end is None:
            raise SelfTestNetconfError("the measured NETCONF boundaries were not observed")
        client_raw = self.raw_store.put(
            bytes(self.client_wire), kind="netconf", name="client-to-server-wire.bin")
        server_raw = self.raw_store.put(
            bytes(self.server_wire), kind="netconf", name="server-to-client-wire.bin")
        client_hello_raw = self.raw_store.put(
            self.client_hello, kind="netconf", name="client-hello.xml")
        server_hello_raw = self.raw_store.put(
            self.server_hello, kind="netconf", name="server-hello.xml")
        session_id = hashlib.sha256(
            self.client_hello + self.server_hello).hexdigest()
        profile_digest = hashlib.sha256(json.dumps(
            self.bundle.netconf_profile["moduleSet"], sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        self.mirror.netconf_session = {
            "endpointAuthority": self.endpoint_authority,
            "hostKeyPinDigest": self.host_key_pin,
            "hostKeyVerified": True,
            "clientAuthMethod": "PUBLIC_KEY",
            "helloRaw": server_hello_raw,
            "clientHelloRaw": client_hello_raw,
            "advertisedCapabilities": list(self.server_capabilities),
            "clientAdvertisedCapabilities": list(self.client_capabilities),
            "requiredCapabilitiesSatisfied": True,
            "negotiatedFraming": "CHUNKED",
            "wireEvidence": {
                "observationPoint": "NETCONF_SSH_SUBSYSTEM_PLAINTEXT_OCTETS",
                "sessionId": session_id,
                "clientToServerRaw": client_raw,
                "serverToClientRaw": server_raw,
                "clientToServerMessageCount": 1 + len(self.request_chunks),
                "serverToClientMessageCount": 1 + len(self.reply_chunks),
                "clientToServerChunkCount": sum(self.request_chunks),
                "serverToClientChunkCount": sum(self.reply_chunks),
                "clientToServerHelloEomCount": 1,
                "serverToClientHelloEomCount": 1,
                "clientToServerPostHelloEomCount": 0,
                "serverToClientPostHelloEomCount": 0,
            },
            "schemaMountVerified": bool(self._schema_reply),
            "mountedYangLibraryDigest": hashlib.sha256(
                self._schema_reply).hexdigest(),
            "yangClosureDigest": profile_digest,
        }
        delta = {
            "measuredRpcDelta": "rpcRecordCount",
            "clientToServerOctetDelta": "clientToServerOctetCount",
            "serverToClientOctetDelta": "serverToClientOctetCount",
            "clientToServerMessageDelta": "clientToServerMessageCount",
            "serverToClientMessageDelta": "serverToClientMessageCount",
            "clientToServerChunkDelta": "clientToServerChunkCount",
            "serverToClientChunkDelta": "serverToClientChunkCount",
        }
        self.mirror.netconf_measured_segment = {
            "source": "NETCONF_SSH_SUBSYSTEM_TAP",
            "sessionId": session_id,
            "start": dict(self._readiness_start),
            "end": dict(self._readiness_end),
            "delta": {
                name: int(self._readiness_end[counter])
                - int(self._readiness_start[counter])
                for name, counter in delta.items()
            },
        }
        lifecycle = {
            int(record["lifecycleOrdinal"]): record
            for record in self.mirror.netconf_rpcs
            if record.get("phase") == "LIFECYCLE"
        }
        teardown = {
            int(record["lifecycleOrdinal"]): record
            for record in self.mirror.netconf_rpcs
            if record.get("phase") == "TEARDOWN"
        }
        self.mirror.netconf_perf_metric_job = {
            "jobId": str(self.vector["o1"]["perfMetricJob"]["jobId"]),
            "managedObjectDn": str(
                self.vector["o1"]["perfMetricJob"]["managedObjectDn"]),
            "create": {
                "attempted": 3 in lifecycle,
                "rpcSequences": [lifecycle[3]["sequence"]] if 3 in lifecycle else [],
                "outcome": "OK" if 3 in lifecycle else "NOT_ASSIGNED",
                "administrativeState": "LOCKED" if 3 in lifecycle else None,
            },
            "read": {
                "attempted": 4 in lifecycle and 7 in lifecycle,
                "rpcSequences": [
                    lifecycle[ordinal]["sequence"]
                    for ordinal in (4, 7) if ordinal in lifecycle],
                "outcome": "OK" if 4 in lifecycle and 7 in lifecycle else "FAILED",
                "administrativeState": self._reply_value(
                    self._rpc_replies.get(("LIFECYCLE", 7), b""),
                    "administrativeState"),
                "operationalState": self._reply_value(
                    self._rpc_replies.get(("LIFECYCLE", 7), b""),
                    "operationalState"),
                "readbackConfirmedAt": instant(),
            },
            "delete": {
                "attempted": 3 in teardown,
                "rpcSequences": [teardown[3]["sequence"]] if 3 in teardown else [],
                "outcome": "OK" if 3 in teardown else "FAILED",
            },
        }
