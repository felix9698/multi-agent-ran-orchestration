"""NETCONF-over-SSH consumer, FileDataReporting subscription, PerfMetricJob lifecycle.

The three adapter actions the role table marks ``REQUIRED_READINESS_ASSIGNED``
are the three halves of one lifecycle, and this module implements them for real.
SC-084 declares the corresponding initial states, so a run that does not carry
all three is refused at admission (``G-ID-09``) and a partial assignment is
refused when this adapter is constructed.  Nothing here runs unless the
execution authority named the action; an unnamed action still fails closed.

**The lifecycle is recorded in three separate segments**, because a measured
scenario whose HTTP/notification evidence is interleaved with configuration
traffic cannot be adjudicated:

``readiness``
    FileDataReporting subscription create and durable persist, then the frozen
    ``o1-netconf-yang-profile.1.0.0.json#/lifecycle`` ordinals 1..8 -- schema
    mount and YANG closure verification, ``lock`` running, ``PerfMetricJob``
    created ``LOCKED``, ``LOCKED`` readback, durable-subscription confirmation,
    job unlock, the eventual ``UNLOCKED``/``ENABLED`` readback, ``unlock``
    running.  Every RPC is captured with ``phase: LIFECYCLE`` and its
    ``lifecycleOrdinal``.

``measured scenario``
    notification -> SFTP retrieval -> normalization.  **Zero** NETCONF RPCs are
    issued here, which is what SC-084's zero ``PERF_METRIC_JOB`` steps mean.

``cleanup``
    ``#/teardown`` ordinals 1..4 -- lock running, lock the job, delete the job,
    unlock running -- then the in-flight drain and only then the subscription
    delete and its absence readback, which is the order §10.2 item 9 fixes.
    Captured with ``phase: TEARDOWN``.

Each segment reports into the ``o1_readiness`` ledger, which is what makes the
§10.2 order ``PRECHECK -> TRUST_READY -> SUBSCRIBED -> JOB_ACTIVE`` a
measurement rather than a comment: the measured body cannot start until every
one of those postconditions is confirmed, and the readiness RPCs stay out of the
frozen SC-084 step list so the measured segment's NETCONF RPC delta stays zero.

Fail-closed properties, all of them measured rather than asserted:

* the server host key is compared against the pin the vector's ``knownHostsRef``
  resolves to **before** authentication, and authentication is public-key only;
* the egress guard's ``SSH_TRANSPORT_OPEN`` seam is called before the transport
  opens, so a NETCONF connect to an authority the gate did not admit is refused
  rather than tallied afterwards;
* the frozen profile declares ``NETCONF_1_1_OVER_SSH``, so the hello exchange is
  end-of-message framed and **every** message after it is RFC 6242 chunked --
  see :mod:`.o1_netconf_framing`, which this module's transport backend is the
  only caller of, and which the self-test Provider emulator deliberately does
  not share;
* every request is the frozen golden fixture's bytes, sent verbatim -- the
  release neither templates nor re-serialises an RPC, so a Provider that only
  answers byte-equal requests is a real test of the release;
* every request and reply is stored verbatim under the capture root before it is
  parsed, and each ``requestFixtureSha256`` is taken over the frozen file;
* a readback that does not carry the state the frozen lifecycle declares aborts
  the lifecycle; there is no "eventually it will settle" path other than the
  declared ``eventualReadbackTimeoutMs`` window, whose waits are declared to the
  observed clock so ``hiddenSleepCount`` stays a measurement.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from .o1_netconf_framing import (
    DEFAULT_MAX_MESSAGE_OCTETS,
    DEFAULT_SEND_CHUNK_OCTETS,
    END_OF_MESSAGE_SENTINEL,
    FRAMING_CHUNKED,
    FRAMING_END_OF_MESSAGE,
    ChunkedMessageDecoder,
    EndOfMessageDecoder,
    NetconfFramingError,
    NetconfTransportError,
    chunk_count,
    encode_chunked,
    encode_end_of_message,
    negotiate_framing,
)
from .o1_sftp import ParamikoBackend, _is_loopback

ADAPTER_ACTIONS = frozenset(
    {"LOAD_O1_PROFILE", "INSTALL_O1_SUBSCRIPTION", "SET_PERF_METRIC_JOB"}
)

#: RFC 6242 §4.3.  The hello exchange is framed with it and nothing after the
#: hello exchange may be; :mod:`o1_netconf_framing` owns that distinction.
NETCONF_FRAME_TERMINATOR = END_OF_MESSAGE_SENTINEL
NETCONF_SUBSYSTEM = "netconf"
NETCONF_BASE_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"

NETCONF_PROFILE_NAME = "o1-netconf-yang-profile.1.0.0.json"
PA_FILE_PROFILE_NAME = "oran-aic-o1-pa-file.1.0.0.json"
GOLDEN_NETCONF_PREFIX = "golden/o1/netconf/"

#: Raw artefact kind for every NETCONF frame this module stores.
RAW_KIND = "netconf"

#: The one lifecycle ordinal that runs before any mutation: schema verification.
#: Selected by ordinal rather than by list position, so a reordered frozen file
#: changes what runs instead of being absorbed.
SCHEMA_VERIFIED_ORDINAL = 1

MAX_FRAME_BYTES = DEFAULT_MAX_MESSAGE_OCTETS


class NetconfAssignmentRefused(RuntimeError):
    """An assigned NETCONF action has no usable, pinned in-process transport."""


class NetconfLifecycleError(RuntimeError):
    """A lifecycle step did not observe the state the frozen profile declares."""


class SubscriptionError(RuntimeError):
    """The FileDataReporting subscription could not be created or persisted."""


# --------------------------------------------------------------- frozen bytes


def _local(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


@dataclass(frozen=True)
class GoldenRpc:
    """One registered golden request fixture, kept as bytes plus its digest."""

    relative_path: str
    raw: bytes
    sha256: str

    @property
    def name(self) -> str:
        return self.relative_path.rsplit("/", 1)[-1]


class FrozenNetconfProfile:
    """Reader over the frozen NETCONF/YANG profile and its golden fixtures.

    Every value is READ from ``profile_bytes`` -- the same frozen bundle bytes
    the rest of the O1 runtime is handed.  Nothing in this class writes a
    contract value down.
    """

    def __init__(self, profile_bytes: Mapping[str, bytes]) -> None:
        self._raw = {
            str(name).replace("\\", "/"): bytes(value)
            for name, value in dict(profile_bytes or {}).items()
        }
        document = self._json_ending_with(NETCONF_PROFILE_NAME)
        if document is None:
            raise NetconfAssignmentRefused(
                "the frozen %s is not among the bundle bytes handed to the O1 "
                "consumer; an assigned NETCONF action cannot be derived"
                % NETCONF_PROFILE_NAME)
        self.document = document
        self.fixtures = self._golden_fixtures()
        if not self.fixtures:
            raise NetconfAssignmentRefused(
                "no golden NETCONF request fixture is present; the release never "
                "templates an RPC and therefore has nothing to send")

    # -- raw access -------------------------------------------------------
    def _json_ending_with(self, suffix: str) -> dict[str, Any] | None:
        for name, raw in sorted(self._raw.items()):
            if name.endswith(suffix):
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise NetconfAssignmentRefused(
                        "frozen %s is not readable JSON" % suffix) from exc
        return None

    def _golden_fixtures(self) -> dict[str, GoldenRpc]:
        found: dict[str, GoldenRpc] = {}
        for name, raw in sorted(self._raw.items()):
            index = name.find(GOLDEN_NETCONF_PREFIX)
            if index < 0 or not name.endswith(".xml"):
                continue
            relative = name[index:]
            found[relative.rsplit("/", 1)[-1]] = GoldenRpc(
                relative_path=relative, raw=raw,
                sha256=hashlib.sha256(raw).hexdigest())
        return found

    def fixture(self, reference: str) -> GoldenRpc:
        name = str(reference).replace("\\", "/").rsplit("/", 1)[-1]
        found = self.fixtures.get(name)
        if found is None:
            raise NetconfAssignmentRefused(
                "the frozen lifecycle names %s, which is not a registered golden "
                "fixture" % reference)
        return found

    # -- declared shape ---------------------------------------------------
    @property
    def transport(self) -> Mapping[str, Any]:
        return self.document["transport"]

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        return tuple(str(item) for item in self.transport["requiredCapabilities"])

    @property
    def schema_mount(self) -> Mapping[str, Any]:
        return self.document["schemaMount"]

    @property
    def module_set(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self.document["moduleSet"])

    @property
    def instance(self) -> Mapping[str, Any]:
        return self.document["instance"]

    @property
    def lifecycle(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self.document["lifecycle"])

    @property
    def teardown(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self.document["teardown"])

    def pa_file_profile(self) -> Mapping[str, Any]:
        document = self._json_ending_with(PA_FILE_PROFILE_NAME)
        if document is None:
            raise SubscriptionError(
                "the frozen %s is absent; the FileDataReporting subscription "
                "resource cannot be derived" % PA_FILE_PROFILE_NAME)
        return document

    # -- derived identities ----------------------------------------------
    def pinned_closure(self) -> list[dict[str, Any]]:
        """The module closure the RELEASE pins, as a canonical, sorted list.

        Entries whose revision the profile deliberately leaves to the mounted
        YANG library carry ``null``: writing a revision the profile does not pin
        would make the closure digest a fabrication.
        """
        closure = []
        for entry in self.module_set:
            closure.append({
                "name": str(entry["name"]),
                "revision": (str(entry["revision"])
                             if entry.get("revision") is not None else None),
            })
        return sorted(closure, key=lambda item: item["name"])

    def closure_digest(self) -> str:
        return _canonical_digest({"pinnedModuleClosure": self.pinned_closure()})

    def required_module_features(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (str(entry["module"]), str(entry["feature"]))
            for entry in self.schema_mount.get("requiredModuleFeatures", ()))


def _canonical_digest(document: Any) -> str:
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ------------------------------------------------------------------ endpoint


@dataclass(frozen=True)
class SshEndpoint:
    host: str
    port: int
    username: str
    authority: str


def parse_netconf_endpoint(uri: str) -> SshEndpoint:
    """``ssh://[user@]host:port`` with an exact authority and no password."""
    try:
        parsed = urlsplit(str(uri))
        port = parsed.port
    except ValueError as exc:
        raise NetconfTransportError("the NETCONF endpoint has an invalid port") from exc
    if (parsed.scheme != "ssh" or not parsed.hostname or port is None
            or parsed.username == "" or parsed.password is not None):
        raise NetconfTransportError(
            "the NETCONF endpoint must be ssh://[user@]host:port with an exact "
            "authority and no password")
    host = parsed.hostname
    authority = "[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)
    username = unquote(parsed.username or "")
    if not username:
        if not _is_loopback(host):
            raise NetconfTransportError(
                "a non-loopback NETCONF endpoint must carry a username")
        username = "o1"
    return SshEndpoint(host=host, port=int(port), username=username,
                       authority=authority)


# ----------------------------------------------------------------- transport


class NetconfBackend(Protocol):
    def open_transport(self, host: str, port: int, timeout: float) -> Any: ...
    def presented_host_key(self, transport: Any) -> tuple[str, bytes]: ...
    def expected_host_keys(self, path: Path, host: str,
                           port: int) -> Mapping[str, bytes]: ...
    def authenticate(self, transport: Any, username: str,
                     credential_path: Path) -> None: ...
    def open_subsystem(self, transport: Any, name: str, timeout: float) -> Any: ...
    def send_frame(self, channel: Any, payload: bytes) -> None: ...
    def receive_frame(self, channel: Any, timeout: float) -> bytes: ...
    #: Called once, by the session, the moment the hello exchange has settled
    #: on ``base:1.1``.  A backend that cannot honour the negotiated framing
    #: has no way to implement this, which is why the session refuses when it
    #: is absent instead of carrying on in the framing the peer just rejected.
    def begin_chunked_framing(self, channel: Any) -> None: ...
    def close_channel(self, channel: Any) -> None: ...


class ParamikoNetconfBackend(ParamikoBackend):
    """Real SSH, and the only place this release turns messages into octets.

    Framing is transport business (RFC 6242 is titled *Using the NETCONF
    Protocol over SSH*), so it lives here: the hello exchange goes out and
    comes back end-of-message framed, and from the instant the session calls
    :meth:`begin_chunked_framing` every message in both directions is chunked.
    The decoder survives the switch with its buffer intact, because a peer may
    legitimately put its hello and its first chunk octets in one segment.
    """

    def __init__(self, *, max_message_octets: int = DEFAULT_MAX_MESSAGE_OCTETS,
                 send_chunk_octets: int = DEFAULT_SEND_CHUNK_OCTETS) -> None:
        self.max_message_octets = int(max_message_octets)
        self.send_chunk_octets = int(send_chunk_octets)
        self._framing = FRAMING_END_OF_MESSAGE
        self._decoder: Any = EndOfMessageDecoder(
            max_message_octets=self.max_message_octets)
        self._ready: list[bytes] = []
        self._ready_counts: list[int] = []
        #: Counted for evidence: how many chunks the last reply arrived in.
        self.last_reply_chunk_count = 0
        self._wire_lock = threading.RLock()
        self._client_to_server_octets = bytearray()
        self._server_to_client_octets = bytearray()
        self._client_messages = 0
        self._server_messages = 0
        self._client_chunks = 0
        self._server_chunks = 0
        self._client_hello_eom_count = 0
        self._server_hello_eom_count = 0
        self._client_post_hello_eom_count = 0
        self._server_post_hello_eom_count = 0

    # -- framing state ----------------------------------------------------
    @property
    def framing(self) -> str:
        return self._framing

    def begin_chunked_framing(self, channel: Any) -> None:
        if self._framing == FRAMING_CHUNKED:
            return
        # Octets the peer coalesced behind its hello belong to the chunked
        # decoder that replaces the hello one; dropping them would lose the
        # first RPC of any Provider that writes both in one segment.
        residue = self._decoder.residue
        self._framing = FRAMING_CHUNKED
        self._decoder = ChunkedMessageDecoder(
            max_message_octets=self.max_message_octets)
        if residue:
            self._ready.extend(self._decoder.feed(residue))
            self._ready_counts.extend(self._decoder.drained_frame_counts)

    # -- transport --------------------------------------------------------
    def open_subsystem(self, transport: Any, name: str, timeout: float) -> Any:
        channel = transport.open_session(timeout=timeout)
        channel.settimeout(timeout)
        channel.invoke_subsystem(name)
        self._framing = FRAMING_END_OF_MESSAGE
        self._decoder = EndOfMessageDecoder(
            max_message_octets=self.max_message_octets)
        self._ready = []
        self._ready_counts = []
        self.last_reply_chunk_count = 0
        with self._wire_lock:
            self._client_to_server_octets.clear()
            self._server_to_client_octets.clear()
            self._client_messages = 0
            self._server_messages = 0
            self._client_chunks = 0
            self._server_chunks = 0
            self._client_hello_eom_count = 0
            self._server_hello_eom_count = 0
            self._client_post_hello_eom_count = 0
            self._server_post_hello_eom_count = 0
        return channel

    def send_frame(self, channel: Any, payload: bytes) -> None:
        encoded = (encode_chunked(bytes(payload),
                                  chunk_octets=self.send_chunk_octets)
                   if self._framing == FRAMING_CHUNKED
                   else encode_end_of_message(bytes(payload)))
        channel.sendall(encoded)
        with self._wire_lock:
            self._client_to_server_octets.extend(encoded)
            if self._framing == FRAMING_CHUNKED:
                self._client_chunks += chunk_count(encoded)
            elif self._client_messages == 0:
                self._client_hello_eom_count += 1
            else:
                self._client_post_hello_eom_count += 1
            self._client_messages += 1

    def receive_frame(self, channel: Any, timeout: float) -> bytes:
        channel.settimeout(timeout)
        while not self._ready:
            octets = channel.recv(65536)
            if not octets:
                # ``close`` says which incompleteness it was, with its own
                # reason code; only a stream that ended cleanly between
                # messages falls through to the generic transport error.
                self._decoder.close()
                raise NetconfTransportError(
                    "the NETCONF peer ended the channel between messages")
            with self._wire_lock:
                self._server_to_client_octets.extend(octets)
            self._ready.extend(self._decoder.feed(octets))
            self._ready_counts.extend(self._decoder.drained_frame_counts)
        frame = self._ready.pop(0)
        self.last_reply_chunk_count = (
            self._ready_counts.pop(0) if self._ready_counts else 0)
        with self._wire_lock:
            if self._framing == FRAMING_CHUNKED:
                self._server_chunks += self.last_reply_chunk_count
            elif self._server_messages == 0:
                self._server_hello_eom_count += 1
            else:
                self._server_post_hello_eom_count += 1
            self._server_messages += 1
        return frame

    def wire_evidence(self) -> dict[str, Any]:
        """An immutable snapshot of every plaintext NETCONF octet observed."""
        with self._wire_lock:
            return {
                "clientToServerOctets": bytes(self._client_to_server_octets),
                "serverToClientOctets": bytes(self._server_to_client_octets),
                "counters": self.wire_counter_snapshot(),
            }

    def wire_counter_snapshot(self) -> dict[str, int]:
        """Monotonic counters suitable for before/after segment deltas."""
        with self._wire_lock:
            return {
                "clientToServerOctets": len(self._client_to_server_octets),
                "serverToClientOctets": len(self._server_to_client_octets),
                "clientMessages": self._client_messages,
                "serverMessages": self._server_messages,
                "clientChunks": self._client_chunks,
                "serverChunks": self._server_chunks,
                "clientHelloEomCount": self._client_hello_eom_count,
                "serverHelloEomCount": self._server_hello_eom_count,
                "clientPostHelloEomCount": self._client_post_hello_eom_count,
                "serverPostHelloEomCount": self._server_post_hello_eom_count,
            }

    def close_channel(self, channel: Any) -> None:
        channel.close()


@dataclass
class NetconfSessionFacts:
    """What the session establishment MEASURED.  Never a claim."""

    endpoint_authority: str
    host_key_pin_digest: str
    host_key_verified: bool
    hello_raw: bytes
    advertised_capabilities: tuple[str, ...]
    required_capabilities_satisfied: bool
    session_id: str = ""
    schema_mount_verified: bool = False
    mounted_yang_library_digest: str = ""
    yang_closure_digest: str = ""
    #: RFC 6242 framing the hello exchange settled on.  Recorded because it is
    #: the property 1.0.1 was withdrawn for: a capture that shows ``base:1.1``
    #: advertised on both sides and ``END_OF_MESSAGE`` here is exactly the
    #: defect, and an adjudicator should not have to re-derive that from the
    #: capability list.
    negotiated_framing: str = FRAMING_END_OF_MESSAGE

    def as_capture(self, hello_descriptor: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "endpointAuthority": self.endpoint_authority,
            "hostKeyPinDigest": self.host_key_pin_digest,
            "hostKeyVerified": bool(self.host_key_verified),
            "clientAuthMethod": "PUBLIC_KEY",
            "helloRaw": dict(hello_descriptor),
            "advertisedCapabilities": list(self.advertised_capabilities),
            "requiredCapabilitiesSatisfied": bool(
                self.required_capabilities_satisfied),
            "negotiatedFraming": self.negotiated_framing,
            "schemaMountVerified": bool(self.schema_mount_verified),
            "mountedYangLibraryDigest": self.mounted_yang_library_digest,
            "yangClosureDigest": self.yang_closure_digest,
        }


class PinnedNetconfSession:
    """One NETCONF-over-SSH session with a pinned host key and public-key auth."""

    def __init__(self, *, endpoint: SshEndpoint, known_hosts_path: Path,
                 credential_path: Path, guard: Any,
                 allowed_authorities: Sequence[str],
                 required_capabilities: Sequence[str],
                 backend: NetconfBackend | None = None,
                 timeout: float = 30.0) -> None:
        if guard is None or not hasattr(guard, "note_ssh_open"):
            raise NetconfTransportError(
                "NETCONF refuses to run without the egress guard's SSH seam")
        self.endpoint = endpoint
        self.known_hosts_path = Path(known_hosts_path)
        self.credential_path = Path(credential_path)
        self.guard = guard
        self.allowed_authorities = frozenset(str(item) for item in allowed_authorities)
        self.required_capabilities = tuple(str(item) for item in required_capabilities)
        self.backend: Any = backend or ParamikoNetconfBackend()
        self.timeout = float(timeout)
        self._transport: Any = None
        self._channel: Any = None
        self.facts: NetconfSessionFacts | None = None
        self.client_hello_raw: bytes | None = None
        self.server_hello_raw: bytes | None = None
        #: RFC 6242 framing this session settled on.  Until the hello exchange
        #: completes it is the only framing the hello exchange may use.
        self.framing = FRAMING_END_OF_MESSAGE

    # -- lifecycle --------------------------------------------------------
    def open(self) -> NetconfSessionFacts:
        if self.endpoint.authority not in self.allowed_authorities:
            raise NetconfTransportError(
                "the NETCONF authority is outside the gate-admitted allowlist")
        if not self.known_hosts_path.is_file() or not self.credential_path.is_file():
            raise NetconfTransportError(
                "the NETCONF secretRefs did not resolve to regular files")
        self.guard.note_ssh_open(self.endpoint.host, self.endpoint.port,
                                 library="paramiko.Transport")
        transport = self.backend.open_transport(
            self.endpoint.host, self.endpoint.port, self.timeout)
        self._transport = transport
        try:
            algorithm, presented = self.backend.presented_host_key(transport)
            expected = self.backend.expected_host_keys(
                self.known_hosts_path, self.endpoint.host, self.endpoint.port)
            if not _pin_matches(expected, algorithm, presented):
                raise NetconfTransportError(
                    "the presented NETCONF host key differs from the pinned "
                    "known_hosts key; refusing to authenticate")
            pin_digest = hashlib.sha256(presented).hexdigest()
            self.backend.authenticate(transport, self.endpoint.username,
                                      self.credential_path)
            self._channel = self.backend.open_subsystem(
                transport, NETCONF_SUBSYSTEM, self.timeout)
            # RFC 6242 §4.3: the hello exchange -- and only the hello exchange
            # -- is end-of-message framed, because the framing for everything
            # after it is what these two messages decide.
            hello = self.backend.receive_frame(self._channel, self.timeout)
            self.server_hello_raw = bytes(hello)
            advertised = _capabilities_of(hello)
            satisfied = set(self.required_capabilities) <= set(advertised)
            # The client half of the RFC 6241 capability exchange.  It is
            # advertised from the frozen required set, never invented.
            client_hello = _client_hello(self.required_capabilities)
            self.backend.send_frame(self._channel, client_hello)
            self.client_hello_raw = bytes(client_hello)
            if not satisfied:
                raise NetconfLifecycleError(
                    "the Provider does not advertise every required NETCONF "
                    "capability: %s" % sorted(
                        set(self.required_capabilities) - set(advertised)))
            self._settle_framing(advertised)
            self.facts = NetconfSessionFacts(
                endpoint_authority=self.endpoint.authority,
                host_key_pin_digest=pin_digest,
                host_key_verified=True,
                hello_raw=hello,
                advertised_capabilities=advertised,
                required_capabilities_satisfied=satisfied,
                session_id=_session_id_of(hello),
                negotiated_framing=self.framing)
            return self.facts
        except BaseException:
            self.close()
            raise

    def _settle_framing(self, advertised: Sequence[str]) -> None:
        """Switch to chunked framing when RFC 6242 says the session must.

        The switch is not optional and it is not a preference: once both peers
        have advertised ``base:1.1`` a conformant Provider will refuse an
        end-of-message frame, so a transport that cannot chunk has to say so
        here rather than send the first RPC in a framing the peer rejects.
        That is the defect this release was withdrawn for.
        """
        self.framing = negotiate_framing(self.required_capabilities, advertised)
        if self.framing != FRAMING_CHUNKED:
            return
        begin = getattr(self.backend, "begin_chunked_framing", None)
        if begin is None:
            raise NetconfFramingError(
                "LO1-CFRAME-008",
                "both peers advertised base:1.1, and the bound NETCONF "
                "transport backend %s offers no chunked framing"
                % type(self.backend).__name__)
        begin(self._channel)

    def exchange(self, request: bytes, *, timeout: float | None = None) -> bytes:
        if self._channel is None:
            raise NetconfTransportError("the NETCONF session is not open")
        window = self.timeout if timeout is None else float(timeout)
        self.backend.send_frame(self._channel, bytes(request))
        return self.backend.receive_frame(self._channel, window)

    def session_binding(self) -> dict[str, Any]:
        """Identity/capability facts for capture 2.0's session binding."""
        facts = self.facts
        return {
            "endpointAuthority": self.endpoint.authority,
            "clientHello": bytes(self.client_hello_raw or b""),
            "serverHello": bytes(self.server_hello_raw or b""),
            "requiredCapabilities": list(self.required_capabilities),
            "advertisedCapabilities": (
                [] if facts is None else list(facts.advertised_capabilities)),
            "negotiatedFraming": self.framing,
            "sessionId": "" if facts is None else facts.session_id,
            "hostKeyPinDigest": (
                "" if facts is None else facts.host_key_pin_digest),
            "hostKeyVerified": bool(
                False if facts is None else facts.host_key_verified),
            "clientAuthMethod": "PUBLIC_KEY",
            "requiredCapabilitiesSatisfied": bool(
                False if facts is None
                else facts.required_capabilities_satisfied),
            "schemaMountVerified": bool(
                False if facts is None else facts.schema_mount_verified),
            "mountedYangLibraryDigest": (
                "" if facts is None else facts.mounted_yang_library_digest),
            "yangClosureDigest": (
                "" if facts is None else facts.yang_closure_digest),
        }

    def wire_evidence(self) -> dict[str, Any]:
        method = getattr(self.backend, "wire_evidence", None)
        if not callable(method):
            return {"available": False,
                    "reason": "BOUND_BACKEND_HAS_NO_OCTET_CAPTURE"}
        evidence = dict(method())
        evidence["available"] = True
        return evidence

    def wire_counter_snapshot(self) -> dict[str, Any]:
        method = getattr(self.backend, "wire_counter_snapshot", None)
        if not callable(method):
            return {"available": False,
                    "reason": "BOUND_BACKEND_HAS_NO_OCTET_CAPTURE"}
        return {"available": True, **dict(method())}

    def close(self) -> None:
        channel, transport = self._channel, self._transport
        self._channel = self._transport = None
        if channel is not None:
            try:
                self.backend.close_channel(channel)
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass

    @property
    def open_now(self) -> bool:
        return self._channel is not None


def _pin_matches(expected: Mapping[str, bytes], algorithm: str,
                 presented: bytes) -> bool:
    raw = expected.get(algorithm)
    digest = expected.get("sha256")
    if raw is not None and hmac.compare_digest(
            hashlib.sha256(raw).digest(), hashlib.sha256(presented).digest()):
        return True
    return digest is not None and hmac.compare_digest(
        digest, hashlib.sha256(presented).digest())


def _client_hello(capabilities: Sequence[str]) -> bytes:
    rendered = "".join(
        "    <capability>%s</capability>\n" % item for item in capabilities)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<hello xmlns="%s">\n'
        "  <capabilities>\n"
        "%s"
        "  </capabilities>\n"
        "</hello>\n" % (NETCONF_BASE_NS, rendered)
    ).encode("utf-8")


def _capabilities_of(hello: bytes) -> tuple[str, ...]:
    try:
        root = ElementTree.fromstring(hello.decode("utf-8"))
    except (UnicodeDecodeError, ElementTree.ParseError) as exc:
        raise NetconfTransportError(
            "the Provider's NETCONF <hello> does not parse") from exc
    if _local(root.tag) != "hello":
        raise NetconfTransportError(
            "the first NETCONF frame was not a <hello>")
    return tuple(sorted({
        (node.text or "").strip() for node in root.iter()
        if _local(node.tag) == "capability" and (node.text or "").strip()}))


def _session_id_of(hello: bytes) -> str:
    """Return the server-assigned RFC 6241 session-id without reserializing."""
    try:
        root = ElementTree.fromstring(hello.decode("utf-8"))
    except (UnicodeDecodeError, ElementTree.ParseError):
        return ""
    return next(((node.text or "").strip() for node in root.iter()
                 if _local(node.tag) == "session-id"
                 and (node.text or "").strip()), "")


def _reply_error(reply: bytes) -> str | None:
    try:
        root = ElementTree.fromstring(reply.decode("utf-8"))
    except (UnicodeDecodeError, ElementTree.ParseError) as exc:
        return "reply does not parse: %s" % exc
    for node in root.iter():
        if _local(node.tag) == "rpc-error":
            message = next(
                ((child.text or "").strip() for child in node.iter()
                 if _local(child.tag) == "error-message"), "")
            return message or "rpc-error"
    return None


def _reply_root(reply: bytes) -> ElementTree.Element:
    return ElementTree.fromstring(reply.decode("utf-8"))


# ------------------------------------------------------------- subscription


class FileDataReportingSubscription:
    """Create, durably persist and delete the FileDataReporting subscription.

    ``consumerCreatesBeforeJobUnlock`` and ``consumerPersistsSubscriptionId`` are
    read from the frozen PM-file profile, so "durable before unlock" is the
    contract's ordering rather than this module's preference.
    """

    def __init__(self, *, vector: Mapping[str, Any], profile: Mapping[str, Any],
                 state_path: Path, ssl_context: ssl.SSLContext | None,
                 timeout: float = 30.0,
                 opener: Callable[..., Any] | None = None,
                 oauth_header_provider: Callable[[], str] | None = None) -> None:
        self.subscription_profile = profile["delivery"]["subscription"]
        self.vector = vector
        self.state_path = Path(state_path)
        self.ssl_context = ssl_context
        self.timeout = float(timeout)
        self._opener = opener or urllib.request.urlopen
        self._oauth_header_provider = oauth_header_provider
        file_data = vector["o1"]["fileDataReporting"]
        self.mns_root = str(file_data["mnsRoot"]).rstrip("/")
        self.mns_version = str(file_data["mnsVersion"])
        self.consumer_reference = str(file_data["consumerReference"])
        self.subscription_id: str | None = None

    # -- resources --------------------------------------------------------
    def _render(self, template: str, **extra: str) -> str:
        rendered = str(template).replace("{MnSRoot}", self.mns_root).replace(
            "{MnSVersion}", self.mns_version)
        for key, value in extra.items():
            rendered = rendered.replace("{%s}" % key, value)
        return rendered

    @property
    def collection_uri(self) -> str:
        return self._render(self.subscription_profile["collectionResource"])

    def item_uri(self, identifier: str) -> str:
        return self._render(self.subscription_profile["itemResource"],
                            subscriptionId=identifier)

    # -- operations -------------------------------------------------------
    def create(self) -> dict[str, Any]:
        body = json.dumps({
            str(self.subscription_profile["consumerReferenceWireField"]):
                self.consumer_reference,
            "timeTick": self.subscription_profile["timeTick"],
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        status, response_headers, payload = self._request(
            self.collection_uri, method=str(
                self.subscription_profile["createHttpMethod"]), body=body)
        if status != int(self.subscription_profile["createSuccessStatus"]):
            raise SubscriptionError(
                "FileDataReportingMnS subscribe answered %d, not %s"
                % (status, self.subscription_profile["createSuccessStatus"]))
        location = next(
            (str(value) for key, value in response_headers.items()
             if str(key).lower() == "location"), "")
        identifier = self._identifier_from_location(location)
        self._persist(
            identifier, location=location,
            representation=dict(payload or {}))
        self.subscription_id = identifier
        return {"subscriptionId": identifier, "status": status,
                "durable": True, "durableStatePath": str(self.state_path)}

    def confirm_durable(self) -> dict[str, Any]:
        """Read the persisted identifier back off the filesystem.

        The frozen lifecycle makes a durable subscription the precondition for
        unlocking the job, so the confirmation reads what was actually written
        rather than trusting the in-memory value.
        """
        if not self.state_path.is_file():
            raise SubscriptionError(
                "the subscription identifier is not durably persisted; the job "
                "must not be unlocked")
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SubscriptionError(
                "the persisted subscription state is unreadable") from exc
        identifier = str(state.get("subscriptionId") or "")
        if not identifier or (self.subscription_id is not None
                              and identifier != self.subscription_id):
            raise SubscriptionError(
                "the persisted subscription identifier does not match the one "
                "the Provider returned")
        self.subscription_id = identifier
        return {"subscriptionId": identifier, "durable": True}

    def delete(self) -> dict[str, Any]:
        identifier = self.subscription_id
        if identifier is None:
            return {"outcome": "ALREADY_ABSENT", "status": None}
        status, _headers, _payload = self._request(
            self.item_uri(identifier),
            method=str(self.subscription_profile["deleteHttpMethod"]), body=None)
        success = int(self.subscription_profile["deleteSuccessStatus"])
        absent = int(self.subscription_profile["deleteAlreadyAbsentStatus"])
        outcome = ("DONE" if status == success
                   else "ALREADY_ABSENT" if status == absent else "FAILED")
        if outcome == "FAILED":
            return {"outcome": outcome, "status": status}
        # Absence readback: deleting again must answer the already-absent status.
        readback, _headers, _payload = self._request(
            self.item_uri(identifier),
            method=str(self.subscription_profile["deleteHttpMethod"]), body=None)
        confirmed_absent = readback == absent
        if not confirmed_absent:
            # A 204 response is not absence evidence.  Preserve the durable
            # identifier so a later cleanup retry can target the same resource.
            return {"outcome": "FAILED", "status": status,
                    "absenceReadbackStatus": readback, "absent": False}
        self._forget()
        self.subscription_id = None
        return {"outcome": outcome, "status": status,
                "absenceReadbackStatus": readback, "absent": True}

    # -- durability -------------------------------------------------------
    @staticmethod
    def _identifier_from_location(location: str) -> str:
        """Apply the frozen ``LOCATION_LAST_PATH_SEGMENT`` transform.

        The Location may be absolute or relative.  Empty trailing separators
        are ignored, then the final non-empty path segment is percent-decoded.
        No response-body fallback exists because the Provider owns allocation.
        """
        if not str(location):
            raise SubscriptionError(
                "the subscribe response carried no Location header")
        try:
            path = urlsplit(str(location)).path
        except ValueError as exc:
            raise SubscriptionError(
                "the subscribe response Location is not a usable URI") from exc
        segments = [segment for segment in path.split("/") if segment]
        if not segments:
            raise SubscriptionError(
                "the subscribe response Location has no non-empty path segment")
        identifier = unquote(segments[-1])
        if not identifier:
            raise SubscriptionError(
                "the subscribe response Location decoded to an empty path segment")
        return identifier

    def _persist(self, identifier: str, *, location: str | None = None,
                 representation: Mapping[str, Any] | None = None) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(
            {"subscriptionId": identifier,
             "consumerReference": self.consumer_reference,
             "location": location,
             "createRepresentation": dict(representation or {})},
            sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(
            prefix=".o1-subscription-", dir=self.state_path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            directory = os.open(self.state_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _forget(self) -> None:
        try:
            self.state_path.unlink()
        except FileNotFoundError:
            pass

    def residual(self) -> list[str]:
        return [str(self.state_path.name)] if self.state_path.is_file() else []

    # -- transport --------------------------------------------------------
    def _request(self, uri: str, *, method: str,
                 body: bytes | None) -> tuple[
                     int, dict[str, str], dict[str, Any] | None]:
        if self.ssl_context is None and str(uri).startswith("https"):
            # No pinned trust anchor resolved.  Falling through to the system
            # trust store would verify against a different authority than the
            # deployment declared, which is a downgrade dressed as a success.
            raise SubscriptionError(
                "the FileDataReportingMnS trust anchor did not resolve; the "
                "subscription refuses to connect on system trust instead")
        headers = {"Content-Type": "application/json"} if body else {}
        if self._oauth_header_provider is None:
            raise SubscriptionError(
                "OAUTH_VALIDATION_AUTHORITY_UNSPECIFIED: FileDataReportingMnS "
                "client-credentials provider is absent")
        headers["Authorization"] = self._oauth_header_provider()
        request = urllib.request.Request(uri, data=body, method=method,
                                         headers=headers)
        try:
            with self._opener(request, timeout=self.timeout,
                              context=self.ssl_context) as response:
                raw = response.read()
                status = int(response.status)
                response_headers = {
                    str(key): str(value) for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            error_headers = {
                str(key): str(value)
                for key, value in (exc.headers.items() if exc.headers else ())}
            return int(exc.code), error_headers, None
        except OSError as exc:
            raise SubscriptionError(
                "the FileDataReportingMnS request could not be completed") from exc
        if not raw:
            return status, response_headers, None
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return status, response_headers, None
        return (status, response_headers,
                document if isinstance(document, dict) else None)


# ------------------------------------------------------------------ lifecycle


@dataclass
class _JobAction:
    attempted: bool = False
    rpc_sequences: list[int] = field(default_factory=list)
    outcome: str = "NOT_ASSIGNED"
    administrative_state: str | None = None
    operational_state: str | None = None
    readback_confirmed_at: str | None = None

    def as_capture(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "attempted": bool(self.attempted),
            "rpcSequences": list(self.rpc_sequences),
            "outcome": self.outcome,
        }
        if self.administrative_state is not None:
            record["administrativeState"] = self.administrative_state
        if self.operational_state is not None:
            record["operationalState"] = self.operational_state
        if self.readback_confirmed_at is not None:
            record["readbackConfirmedAt"] = self.readback_confirmed_at
        return record


class PerfMetricJobLifecycle:
    """The three recorded segments of the PerfMetricJob lifecycle."""

    def __init__(self, *, profile: FrozenNetconfProfile, vector: Mapping[str, Any],
                 resolver: Any, recorder: Any, guard: Any,
                 allowed_authorities: Sequence[str],
                 capture_root: Path,
                 backend: NetconfBackend | None = None,
                 subscription: FileDataReportingSubscription | None = None,
                 ssl_context: ssl.SSLContext | None = None,
                 timeout_ms: int = 30000,
                 readiness: Any = None,
                 drain: Callable[[], Mapping[str, Any]] | None = None,
                 oauth_header_provider: Callable[[], str] | None = None) -> None:
        self.readiness = readiness
        self.drain = drain
        self.profile = profile
        self.vector = vector
        self.resolver = resolver
        self.recorder = recorder
        self.guard = guard
        self.capture_root = Path(capture_root)
        self.timeout = max(1.0, float(timeout_ms) / 1000.0)
        self.endpoint = parse_netconf_endpoint(vector["o1"]["netconf"]["endpoint"])
        self.session = PinnedNetconfSession(
            endpoint=self.endpoint,
            known_hosts_path=resolver.resolve_path(
                str(vector["o1"]["netconf"]["knownHostsRef"])),
            credential_path=resolver.resolve_path(
                str(vector["o1"]["netconf"]["credentialRef"])),
            guard=guard, allowed_authorities=allowed_authorities,
            required_capabilities=profile.required_capabilities,
            backend=backend, timeout=self.timeout)
        self.subscription = subscription or FileDataReportingSubscription(
            vector=vector, profile=profile.pa_file_profile(),
            state_path=self.capture_root / "state" / "o1-subscription.json",
            ssl_context=ssl_context, timeout=self.timeout,
            oauth_header_provider=oauth_header_provider)
        self.job_id = str(vector["o1"]["perfMetricJob"]["jobId"])
        self.managed_object_dn = str(vector["o1"]["perfMetricJob"]["managedObjectDn"])
        self._create = _JobAction()
        self._read = _JobAction()
        self._delete = _JobAction()
        self._profile_loaded = False
        self._subscription_created = False
        self._job_active = False
        self._job_quiesced = False
        # Successful teardown RPCs are durable process state for retry.  A
        # later ordinal may fail after the job was already deleted; replaying
        # ordinals 1..3 on the next cleanup attempt would target a resource
        # that no longer exists and could never make progress.
        self._teardown_completed_ordinals: set[int] = set()
        self._torn_down = False
        self._teardown_residual: list[str] = []
        self._measured_start: dict[str, Any] | None = None
        self._measured_end: dict[str, Any] | None = None
        self.segments: dict[str, list[dict[str, Any]]] = {
            "readiness": [], "measured": [], "cleanup": []}

    # -- helpers ----------------------------------------------------------
    @property
    def _clock(self) -> Any:
        return getattr(self.recorder, "clock", None)

    def _now(self) -> str:
        clock = self._clock
        if clock is not None:
            return clock.now()
        from datetime import datetime, timezone

        moment = datetime.now(timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + (
            "%03dZ" % (moment.microsecond // 1000))

    def _raw_store(self) -> Any:
        store = getattr(self.recorder, "raw_store", None)
        if store is None:
            raise NetconfLifecycleError(
                "the capture raw store is unavailable; a NETCONF frame must be "
                "stored verbatim before it is parsed")
        return store

    def _store(self, raw: bytes, *, name: str) -> dict[str, Any]:
        descriptor = dict(self._raw_store().put(
            raw, kind=RAW_KIND, name=name, media_type="application/xml"))
        return {
            "byteCount": int(descriptor["byteCount"]),
            "sha256": str(descriptor["sha256"]),
            "artifactPath": str(descriptor["artifactPath"]),
            "mediaType": descriptor.get("mediaType", "application/xml"),
            "truncated": False,
        }

    def _note(self, segment: str, entry: Mapping[str, Any]) -> None:
        self.segments[segment].append(dict(entry))

    # -- readiness --------------------------------------------------------
    def _publish_readiness(self) -> None:
        if self.readiness is None:
            return
        publish = getattr(self.recorder, "set_o1_readiness", None)
        if callable(publish):
            publish(self.readiness.as_capture())

    def _confirm_readiness(self, state: str, **evidence: Any) -> None:
        if self.readiness is None:
            return
        self.readiness.confirm(state, evidence=evidence)
        self._publish_readiness()

    # -- one RPC ----------------------------------------------------------
    def _rpc(self, fixture_reference: str, *, ordinal: int, phase: str,
             segment: str, timeout: float | None = None) -> bytes:
        fixture = self.profile.fixture(fixture_reference)
        sequence = int(self.recorder.next_sequence())
        request_descriptor = self._store(
            fixture.raw, name="rpc-%04d-%s-request.xml" % (sequence, fixture.name[:-4]))
        outcome = "OK"
        reason: str | None = None
        try:
            reply = self.session.exchange(fixture.raw, timeout=timeout)
        except NetconfTransportError as exc:
            reply = b""
            outcome = "TIMEOUT" if "closed" in str(exc) else "REFUSED_FAIL_CLOSED"
            reason = str(exc)
        reply_descriptor = self._store(
            reply, name="rpc-%04d-%s-reply.xml" % (sequence, fixture.name[:-4]))
        if outcome == "OK":
            error = _reply_error(reply)
            if error is not None:
                outcome = "RPC_ERROR"
                reason = error
        record: dict[str, Any] = {
            "sequence": sequence,
            "lifecycleOrdinal": int(ordinal),
            "phase": phase,
            "requestFixtureRef": fixture.relative_path,
            "requestFixtureSha256": fixture.sha256,
            "requestRaw": request_descriptor,
            "replyRaw": reply_descriptor,
            "at": self._now(),
            "outcome": outcome,
        }
        if reason:
            record["failClosedReason"] = reason[:4096]
        self.recorder.emit_netconf_rpc(record)
        self._note(segment, {
            "step": fixture.name, "ordinal": int(ordinal), "phase": phase,
            "sequence": sequence, "outcome": outcome,
            "requestSha256": fixture.sha256,
            "replySha256": reply_descriptor["sha256"]})
        if outcome != "OK":
            raise NetconfLifecycleError(
                "NETCONF %s (ordinal %d) failed closed: %s"
                % (fixture.name, ordinal, reason or outcome))
        return reply

    # -- segment 1: readiness --------------------------------------------
    def load_profile(self) -> dict[str, Any]:
        """Ordinal 1: session, capabilities, schema mount, YANG closure."""
        if self._profile_loaded:
            return {"profileLoaded": True, "reused": True}
        facts = self.session.open()
        entry = next(item for item in self.profile.lifecycle
                     if int(item["ordinal"]) == SCHEMA_VERIFIED_ORDINAL)
        reply = self._rpc(str(entry["requestFixture"]),
                          ordinal=int(entry["ordinal"]), phase="LIFECYCLE",
                          segment="readiness")
        mounted = self._verify_schema_mount(reply)
        facts.schema_mount_verified = True
        facts.mounted_yang_library_digest = mounted["mountedYangLibraryDigest"]
        facts.yang_closure_digest = self.profile.closure_digest()
        hello_descriptor = self._store(facts.hello_raw, name="hello-server.xml")
        self.recorder.set_netconf_session(facts.as_capture(hello_descriptor))
        self._profile_loaded = True
        self._publish_job()
        # §10.2 item 2.  The session is open on a pinned host key with public-key
        # authentication, the profile's required capabilities were advertised and
        # the schema mount is verified -- all of it before any mutation.
        self._confirm_readiness(
            "TRUST_READY",
            hostKeyVerified=True,
            clientAuthMethod="PUBLIC_KEY",
            requiredCapabilitiesSatisfied=bool(
                facts.required_capabilities_satisfied),
            schemaMountVerified=True,
            mountedYangLibraryDigest=facts.mounted_yang_library_digest,
            endpointAuthority=self.endpoint.authority)
        return {
            "profileLoaded": True,
            "advertisedCapabilityCount": len(facts.advertised_capabilities),
            "requiredCapabilitiesSatisfied": facts.required_capabilities_satisfied,
            "schemaMountVerified": True,
            "mountedYangLibraryDigest": facts.mounted_yang_library_digest,
            "yangClosureDigest": facts.yang_closure_digest,
        }

    def _verify_schema_mount(self, reply: bytes) -> dict[str, Any]:
        declared = self.profile.schema_mount
        root = _reply_root(reply)
        wanted_label = str(declared["label"])

        def direct_values(node: ElementTree.Element, tag: str) -> list[str]:
            return [(child.text or "").strip() for child in node
                    if _local(child.tag) == tag and (child.text or "").strip()]

        mount_points = [
            node for node in root.iter()
            if _local(node.tag) == "mount-point"
            and wanted_label in direct_values(node, "label")
        ]
        if len(mount_points) != 1:
            raise NetconfLifecycleError(
                "schema mount label %r resolved to %d mount-point elements, not one"
                % (wanted_label, len(mount_points)))
        mount_point = mount_points[0]

        def expect_direct(tag: str, wanted: str, what: str) -> None:
            observed = direct_values(mount_point, tag)
            if observed != [wanted]:
                raise NetconfLifecycleError(
                    "schema mount %s is %r, not the pinned %r"
                    % (what, observed, wanted))

        expect_direct("label", wanted_label, "label")
        expect_direct("module", str(declared["parentModule"]), "parent module")
        if declared.get("configRequired"):
            expect_direct("config", "true", "mount point config")
        reference_kind = str(declared["schemaReferenceKind"])
        references = [child for child in mount_point
                      if _local(child.tag) == reference_kind]
        if len(references) != 1:
            raise NetconfLifecycleError(
                "schema mount schema reference kind is not the single pinned %r"
                % reference_kind)

        mounted_modules, features = _mounted_yang_library(root)
        if not mounted_modules:
            raise NetconfLifecycleError(
                "the schema-mount RPC returned no ietf-yang-library:yang-library "
                "for the concrete mount-point instance")
        for name_key, revision_key, what in (
                ("parentModule", "parentRevision", "parent module"),
                ("mountedRootModule", "mountedRootRevision", "mounted root module")):
            module_name = str(declared[name_key])
            observed_revision = mounted_modules.get(module_name)
            wanted_revision = str(declared[revision_key])
            if observed_revision != wanted_revision:
                raise NetconfLifecycleError(
                    "schema mount %s %s revision is %r, not the pinned %r"
                    % (what, module_name, observed_revision, wanted_revision))
        for module in self.profile.pinned_closure():
            advertised = mounted_modules.get(module["name"])
            if advertised is None:
                raise NetconfLifecycleError(
                    "the mounted YANG library does not advertise the pinned "
                    "module %s" % module["name"])
            if module["revision"] is not None and advertised != module["revision"]:
                raise NetconfLifecycleError(
                    "mounted %s revision %r differs from the pinned %r"
                    % (module["name"], advertised, module["revision"]))
        for module_name, feature in self.profile.required_module_features():
            if feature not in features.get(module_name, ()):
                raise NetconfLifecycleError(
                    "the mounted YANG library does not enable %s on %s"
                    % (feature, module_name))
        return {
            "mountedYangLibraryDigest": _canonical_digest({
                "modules": [{"name": name, "revision": mounted_modules[name]}
                            for name in sorted(mounted_modules)],
                "features": {name: sorted(features[name])
                             for name in sorted(features)},
            }),
        }

    def install_subscription(self) -> dict[str, Any]:
        """The FileDataReporting subscription, created and durably persisted."""
        if not self._profile_loaded:
            raise NetconfLifecycleError(
                "the NETCONF profile must be verified before the subscription is "
                "created; the frozen lifecycle verifies the schema before it "
                "mutates anything")
        if self._subscription_created:
            return {"subscriptionInstalled": True, "reused": True}
        created = self.subscription.create()
        self._subscription_created = True
        self._note("readiness", {
            "step": "FILE_DATA_REPORTING_SUBSCRIBE", "ordinal": 0,
            "phase": "LIFECYCLE", "outcome": "OK",
            "subscriptionId": created["subscriptionId"],
            "durable": True})
        # §10.2 item 3: SUBSCRIBED.  The postcondition the frozen runner contract
        # binds is "One durable provider subscription exists", so the evidence is
        # the 201, the identifier the Provider returned and the durable path.
        self._confirm_readiness(
            "SUBSCRIBED",
            subscriptionId=created["subscriptionId"],
            createdStatus=int(created.get("status", 0)),
            durable=bool(created.get("durable")),
            durableStatePath=str(created.get("durableStatePath", "")))
        return {"subscriptionInstalled": True,
                "subscriptionId": created["subscriptionId"],
                "durable": True}

    def set_perf_metric_job(self) -> dict[str, Any]:
        """Ordinals 2..8: lock, create LOCKED, readback, unlock, reconcile."""
        if not self._profile_loaded:
            raise NetconfLifecycleError(
                "the NETCONF profile must be verified before the datastore is "
                "mutated")
        if not self._subscription_created:
            raise NetconfLifecycleError(
                "the FileDataReporting subscription must exist before the "
                "PerfMetricJob is created; consumerCreatesBeforeJobUnlock is "
                "the frozen ordering")
        if self._job_active:
            return {"perfMetricJobPresent": True, "reused": True}
        instance = self.profile.instance["configuration"]
        locked_state = str(instance["createAdministrativeState"])
        active_state = str(instance["activeAdministrativeState"])
        # The states this call actually reached, in the order it reached them.
        # §10.2 item 4 is an ORDER, so the evidence for it has to be an order:
        # "durable subscription confirmed before the unlock" is read off this
        # list rather than asserted by whoever wrote the loop.
        observed_states: list[str] = []
        for entry in self.profile.lifecycle:
            ordinal = int(entry["ordinal"])
            if ordinal == SCHEMA_VERIFIED_ORDINAL:
                continue
            state_name = str(entry.get("state", "ORDINAL_%d" % ordinal))
            if "requestFixture" not in entry:
                # Ordinal 5 is the external precondition: the subscription must
                # be durable BEFORE the job is unlocked, and the confirmation
                # reads the persisted identifier back rather than trusting it.
                confirmed = self.subscription.confirm_durable()
                self._note("readiness", {
                    "step": str(entry["state"]), "ordinal": ordinal,
                    "phase": "LIFECYCLE", "outcome": "OK",
                    "subscriptionId": confirmed["subscriptionId"]})
                observed_states.append(state_name)
                continue
            expected_admin = entry.get("expectedAdministrativeState")
            expected_oper = entry.get("expectedOperationalState")
            window = entry.get("eventualReadbackTimeoutMs")
            if expected_admin is None:
                reply = self._rpc(str(entry["requestFixture"]), ordinal=ordinal,
                                  phase="LIFECYCLE", segment="readiness")
                if str(entry.get("expectedReply", "ok")) == "ok":
                    _require_ok(reply, entry)
                if entry["requestFixture"].endswith("create-perfmetricjob-locked.xml"):
                    self._create.attempted = True
                    if self.segments["readiness"]:
                        self._create.rpc_sequences.append(
                            self.segments["readiness"][-1]["sequence"])
                    self._create.outcome = "OK"
                    self._create.administrative_state = locked_state
                observed_states.append(state_name)
                continue
            observed = self._readback(
                entry, expected_admin=str(expected_admin),
                expected_operational=(str(expected_oper) if expected_oper else None),
                window_ms=int(window) if window else None)
            self._read.attempted = True
            self._read.rpc_sequences.append(observed["sequence"])
            self._read.outcome = "OK"
            self._read.administrative_state = observed["administrativeState"]
            self._read.operational_state = observed.get("operationalState")
            self._read.readback_confirmed_at = observed["at"]
            if str(expected_admin) == locked_state:
                self._create.readback_confirmed_at = observed["at"]
            if str(expected_admin) == active_state:
                self._job_active = True
            observed_states.append(state_name)
        self._publish_job()
        # Freeze the counter prefix while admission is still closed.  Once
        # JOB_ACTIVE is confirmed, listener threads may enter the measured
        # body immediately; taking this snapshot afterwards leaves a race in
        # which real measured traffic precedes its declared start boundary.
        self._measured_start = self._segment_counter_snapshot()
        self._confirm_readiness(
            "JOB_ACTIVE",
            administrativeState=self._read.administrative_state,
            operationalState=self._read.operational_state,
            readbackConfirmedAt=self._read.readback_confirmed_at,
            datastoreLockedBeforeCreate=_precedes(
                observed_states, "DATASTORE_LOCKED", "JOB_CREATED_LOCKED"),
            lockedReadbackConfirmed="JOB_CONFIG_VERIFIED" in observed_states,
            durableSubscriptionConfirmedBeforeUnlock=_precedes(
                observed_states, "SUBSCRIPTION_DURABLE", "JOB_UNLOCKED"),
            datastoreUnlocked="DATASTORE_UNLOCKED" in observed_states,
            observedStateOrder=",".join(observed_states))
        return {"perfMetricJobPresent": True,
                "administrativeState": self._read.administrative_state,
                "operationalState": self._read.operational_state}

    def _readback(self, entry: Mapping[str, Any], *, expected_admin: str,
                  expected_operational: str | None,
                  window_ms: int | None) -> dict[str, Any]:
        """Read the job back until it carries the declared state, or fail closed."""
        deadline_ms = int(window_ms or 0)
        waited_ms = 0
        interval_ms = 250
        last: dict[str, Any] | None = None
        while True:
            reply = self._rpc(str(entry["requestFixture"]),
                              ordinal=int(entry["ordinal"]), phase="LIFECYCLE",
                              segment="readiness")
            observed = _job_state(reply)
            observed["sequence"] = self.segments["readiness"][-1]["sequence"]
            observed["at"] = self._now()
            last = observed
            admin_ok = observed.get("administrativeState") == expected_admin
            oper_ok = (expected_operational is None
                       or observed.get("operationalState") == expected_operational)
            if admin_ok and oper_ok:
                return observed
            if waited_ms >= deadline_ms:
                break
            step = min(interval_ms, deadline_ms - waited_ms)
            self._sleep(step / 1000.0)
            waited_ms += step
            # Declared backoff, capped: a fixed 250 ms poll would put a hundred
            # readback RPCs into the evidence for one stuck job.
            interval_ms = min(interval_ms * 2, 2000)
        raise NetconfLifecycleError(
            "PerfMetricJob readback observed %r/%r, not the declared %r/%r"
            % ((last or {}).get("administrativeState"),
               (last or {}).get("operationalState"), expected_admin,
               expected_operational))

    def _sleep(self, seconds: float) -> None:
        import time

        clock = self._clock
        if clock is not None:
            # Declared: this is the frozen eventualReadbackTimeoutMs window, not
            # a hidden sleep standing in for an observable event.
            clock.note_sleep(declared=True)
        time.sleep(max(0.0, seconds))

    # -- segment 3: cleanup ----------------------------------------------
    @staticmethod
    def _teardown_semantics(entry: Mapping[str, Any]) -> tuple[str, str, str]:
        """Return the truthful target, action and residual for one RPC.

        Teardown is a four-step transaction, not four aliases for deleting the
        job.  In particular, failure of the final ``unlock-running`` after a
        successful delete leaves a datastore lock, not a residual job.
        """
        fixture = str(entry.get("requestFixture", ""))
        if fixture.endswith("unlock-running.xml"):
            return "RUNNING_DATASTORE", "UNLOCK", "RUNNING_DATASTORE_LOCK"
        if fixture.endswith("lock-running.xml"):
            return "RUNNING_DATASTORE", "LOCK", "RUNNING_DATASTORE_LOCK"
        if fixture.endswith("lock-perfmetricjob.xml"):
            return "PERF_METRIC_JOB", "LOCK", "PERF_METRIC_JOB"
        if fixture.endswith("delete-perfmetricjob.xml"):
            return "PERF_METRIC_JOB", "DELETE", "PERF_METRIC_JOB"
        return "NETCONF_TEARDOWN", "RPC", "NETCONF_TEARDOWN"

    def teardown(self) -> list[dict[str, Any]]:
        """``#/teardown`` then the subscription delete and absence readback.

        §10.2 item 9 fixes the drain order: lock and delete the job first so the
        Provider creates no new file, drain what is in flight, and only then
        delete the subscription.  The order is recorded step by step in the
        readiness ledger as it happens, on the normal path and on the failure
        path alike, so "we tore down in the right order" is readable off the
        capture instead of being taken on trust.
        """
        if self._torn_down:
            return list(self.segments["cleanup"])
        if self.readiness is not None:
            self.readiness.close_measured_admission(
                timeout_seconds=float(self.timeout))
        self.mark_measured_segment_end()
        self._teardown_residual = []
        actions: list[dict[str, Any]] = []
        if self._create.attempted:
            teardown_ordinals = {
                int(entry["ordinal"]) for entry in self.profile.teardown
                if entry.get("requestFixture")}
            already_complete = teardown_ordinals.issubset(
                self._teardown_completed_ordinals)
            delete_complete_at_entry = any(
                int(entry["ordinal"]) in self._teardown_completed_ordinals
                and str(entry.get("requestFixture", "")).endswith(
                    "delete-perfmetricjob.xml")
                for entry in self.profile.teardown)
            if not already_complete and not (
                    self._profile_loaded and self.session.open_now):
                pending = next(
                    entry for entry in self.profile.teardown
                    if int(entry["ordinal"]) not in
                    self._teardown_completed_ordinals)
                target, action, residual = self._teardown_semantics(pending)
                actions.append({
                    "target": target, "action": action,
                    "outcome": "FAILED", "at": self._now(),
                    "detail": "the NETCONF session closed before teardown completed"})
                self._teardown_residual.append(residual)
                self._publish_teardown_actions(actions)
                return actions
            for entry in self.profile.teardown:
                ordinal = int(entry["ordinal"])
                if ordinal in self._teardown_completed_ordinals:
                    continue
                fixture = str(entry["requestFixture"])
                try:
                    reply = self._rpc(
                        fixture, ordinal=ordinal,
                        phase="TEARDOWN", segment="cleanup")
                    if str(entry.get("expectedReply", "ok")) == "ok":
                        _require_ok(reply, entry)
                except NetconfLifecycleError as exc:
                    target, action, residual = self._teardown_semantics(entry)
                    if (self._delete.outcome == "OK"
                            and not delete_complete_at_entry):
                        actions.append({
                            "target": "PERF_METRIC_JOB", "action": "DELETE",
                            "outcome": "DONE", "at": self._now(),
                            "detail": "delete completed before a later teardown RPC failed"})
                    actions.append({
                        "target": target, "action": action,
                        "outcome": "FAILED", "at": self._now(),
                        "detail": str(exc)[:4096]})
                    if fixture.endswith("delete-perfmetricjob.xml"):
                        self._delete.attempted = True
                        self._delete.outcome = "FAILED"
                    self._teardown_residual.append(residual)
                    self._publish_teardown_actions(actions)
                    return actions
                self._teardown_completed_ordinals.add(ordinal)
                if fixture.endswith("lock-perfmetricjob.xml"):
                    self._note_teardown("JOB_LOCKED")
                if fixture.endswith("delete-perfmetricjob.xml"):
                    self._delete.attempted = True
                    if self.segments["cleanup"]:
                        self._delete.rpc_sequences.append(
                            self.segments["cleanup"][-1]["sequence"])
                    self._delete.outcome = "OK"
                    # The Provider has accepted the deletion even if a later
                    # datastore-unlock ordinal fails.  Preserve that fact so a
                    # retry resumes after the delete instead of replaying it.
                    self._job_quiesced = True
                    self._job_active = False
                    self._note_teardown("JOB_DELETED")
            if teardown_ordinals.issubset(self._teardown_completed_ordinals):
                actions.append({
                    "target": "PERF_METRIC_JOB", "action": "DELETE",
                    "outcome": ("ALREADY_ABSENT"
                                if already_complete or delete_complete_at_entry
                                else "DONE"),
                    "at": self._now(),
                    "detail": ("job deletion completed by an earlier cleanup attempt"
                               if delete_complete_at_entry else
                               "frozen teardown ordinals already completed"
                               if already_complete else
                               "frozen teardown ordinals 1..4 completed")})
            else:
                self._teardown_residual.append("PERF_METRIC_JOB")
                self._publish_teardown_actions(actions)
                return actions
        else:
            actions.append({
                "target": "PERF_METRIC_JOB", "action": "DELETE",
                "outcome": "ALREADY_ABSENT", "at": self._now(),
                "detail": "no PerfMetricJob was created by this run"})
        # §10.2 item 9: new file creation is blocked first, THEN in-flight
        # notification/retrieval work drains, and only after that does the
        # subscription go.  The drain runs on both paths; a drain that cannot be
        # performed is recorded as such rather than skipped silently.
        drained = self._drain_in_flight()
        actions.append({
            "target": "IN_FLIGHT_WORK", "action": "DRAIN",
            "outcome": str(drained["outcome"]), "at": self._now(),
            "detail": str(drained["detail"])})
        if str(drained["outcome"]) not in ("DONE", "ALREADY_ABSENT"):
            self._teardown_residual.append("IN_FLIGHT_WORK")
            self._publish_teardown_actions(actions)
            return actions
        self._note_teardown("IN_FLIGHT_DRAINED")
        if self._subscription_created:
            try:
                result = self.subscription.delete()
            except SubscriptionError as exc:
                result = {"outcome": "FAILED", "detail": str(exc)}
            actions.append({
                "target": "O1_SUBSCRIPTION", "action": "DELETE",
                "outcome": (str(result.get("outcome", "FAILED"))
                            if result.get("absent") else "FAILED"),
                "at": self._now(),
                "detail": "absence readback %s" % (
                    "confirmed" if result.get("absent") else "not confirmed")})
            if (str(result.get("outcome")) not in ("DONE", "ALREADY_ABSENT")
                    or result.get("absent") is not True):
                self._teardown_residual.append("O1_SUBSCRIPTION")
                self._publish_teardown_actions(actions)
                return actions
            self._subscription_created = False
            self._note_teardown("SUBSCRIPTION_DELETED")
        else:
            actions.append({
                "target": "O1_SUBSCRIPTION", "action": "DELETE",
                "outcome": "ALREADY_ABSENT", "at": self._now(),
                "detail": "no subscription was created by this run"})
            self._note_teardown("SUBSCRIPTION_ALREADY_ABSENT")
        self.session.close()
        actions.append({
            "target": "NETCONF_SESSION", "action": "CLOSE", "outcome": "DONE",
            "at": self._now(), "detail": "run-scoped NETCONF-over-SSH session"})
        self._note_teardown("NETCONF_SESSION_CLOSED")
        self._torn_down = True
        if self.readiness is not None:
            self.readiness.mark_stopped()
        self._publish_teardown_actions(actions)
        return actions

    def _publish_teardown_actions(self, actions: Sequence[Mapping[str, Any]]) -> None:
        """Persist exactly the transitions attempted by this teardown call."""
        for action in actions:
            self.recorder.emit_cleanup_action(dict(action))
            self._note("cleanup", dict(action))
        self._publish_job()
        self._publish_readiness()

    def _note_teardown(self, step: str) -> None:
        if self.readiness is not None:
            self.readiness.note_teardown_step(step)

    def _drain_in_flight(self) -> dict[str, Any]:
        """Quiesce in-flight notification/retrieval work before the unsubscribe."""
        drain = self.drain
        if drain is None:
            return {"outcome": "ALREADY_ABSENT",
                    "detail": "no in-flight work source is bound to this lifecycle"}
        try:
            observed = dict(drain())
        except Exception as exc:  # noqa: BLE001 - a failed drain is evidence
            return {"outcome": "FAILED", "detail": str(exc)[:4096]}
        pending = int(observed.get("inFlight", 0))
        return {
            "outcome": "DONE" if pending == 0 else "FAILED",
            "detail": "in-flight notification/retrieval count %d after drain"
                      % pending,
        }

    def residual(self) -> list[str]:
        return list(dict.fromkeys(
            list(self._teardown_residual) + list(self.subscription.residual())))

    # -- capture ----------------------------------------------------------
    def _publish_job(self) -> None:
        self.recorder.set_perf_metric_job({
            "jobId": self.job_id,
            "managedObjectDn": self.managed_object_dn,
            "create": self._create.as_capture(),
            "read": self._read.as_capture(),
            "delete": self._delete.as_capture(),
        })

    def lifecycle_evidence(self) -> dict[str, list[dict[str, Any]]]:
        """The three segments, kept apart on purpose."""
        return {name: [dict(item) for item in entries]
                for name, entries in self.segments.items()}

    def _segment_counter_snapshot(self) -> dict[str, Any]:
        snapshot = getattr(self.session, "wire_counter_snapshot", None)
        wire = (dict(snapshot()) if callable(snapshot)
                else {"available": False,
                      "reason": "SESSION_TEST_DOUBLE_HAS_NO_OCTET_CAPTURE"})
        return {
            # A boundary consumes a capture sequence without fabricating an RPC
            # record.  It places the lifecycle RPCs strictly before the start
            # and any teardown RPCs strictly after the end.
            "captureSequence": int(self.recorder.next_sequence()),
            "monotonicNs": int(time.monotonic_ns()),
            "rpcCount": sum(
                1 for entries in self.segments.values() for entry in entries
                if "requestSha256" in entry and "replySha256" in entry),
            "wire": dict(wire),
        }

    def mark_measured_segment_end(self) -> dict[str, Any]:
        """Freeze the pre-teardown boundary once; safe for export then cleanup."""
        if self._measured_end is None:
            self._measured_end = self._segment_counter_snapshot()
        return dict(self._measured_end)

    def measured_segment_counters(self) -> dict[str, Any]:
        """Before/after values and independently computed zero-delta fields."""
        start = None if self._measured_start is None else dict(self._measured_start)
        end = None if self._measured_end is None else dict(self._measured_end)
        result: dict[str, Any] = {"start": start, "end": end}
        if start is None or end is None:
            return result
        delta: dict[str, int] = {
            "rpcCount": int(end["rpcCount"]) - int(start["rpcCount"])}
        start_wire = start.get("wire", {})
        end_wire = end.get("wire", {})
        for name in ("clientToServerOctets", "serverToClientOctets",
                     "clientMessages", "serverMessages",
                     "clientChunks", "serverChunks"):
            if name in start_wire and name in end_wire:
                delta[name] = int(end_wire[name]) - int(start_wire[name])
        result["delta"] = delta
        return result

    def session_binding(self) -> dict[str, Any]:
        return self.session.session_binding()

    def wire_evidence(self) -> dict[str, Any]:
        return self.session.wire_evidence()

    def wire_counter_snapshot(self) -> dict[str, Any]:
        return self.session.wire_counter_snapshot()


def _precedes(observed: Sequence[str], earlier: str, later: str) -> bool:
    """True only when BOTH states ran and ``earlier`` ran first."""
    sequence = list(observed)
    if earlier not in sequence or later not in sequence:
        return False
    return sequence.index(earlier) < sequence.index(later)


def _require_ok(reply: bytes, entry: Mapping[str, Any]) -> None:
    root = _reply_root(reply)
    if not any(_local(node.tag) == "ok" for node in root.iter()):
        raise NetconfLifecycleError(
            "ordinal %s expected an <ok/> reply and did not get one"
            % entry.get("ordinal"))


def _mounted_yang_library(root: ElementTree.Element
                          ) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """Modules and enabled features as the mounted YANG library advertised them."""
    modules: dict[str, str] = {}
    features: dict[str, list[str]] = {}
    for node in root.iter():
        if _local(node.tag) != "module":
            continue
        name = next(((child.text or "").strip() for child in node
                     if _local(child.tag) == "name"), "")
        if not name:
            continue
        if name in modules:
            raise NetconfLifecycleError(
                "the mounted YANG library advertises duplicate module %s" % name)
        revision = next(((child.text or "").strip() for child in node
                         if _local(child.tag) == "revision"), "")
        modules[name] = revision
        enabled = [(child.text or "").strip() for child in node
                   if _local(child.tag) == "feature" and (child.text or "").strip()]
        if enabled:
            features.setdefault(name, []).extend(enabled)
    return modules, {name: tuple(values) for name, values in features.items()}


def _job_state(reply: bytes) -> dict[str, Any]:
    root = _reply_root(reply)
    state: dict[str, Any] = {}
    for node in root.iter():
        tag = _local(node.tag)
        value = (node.text or "").strip()
        if tag in ("administrativeState", "operationalState", "jobId") and value:
            state.setdefault(tag, value)
    return state


# --------------------------------------------------------------- the adapter


class OptionalNetconfAdapter:
    """Authority-gated entry point for the three mandatory adapter actions.

    "Optional" is a historical name.  SC-084 declares ``INSTALL_O1_PROFILE``,
    ``INSTALL_ACTIVE_O1_SUBSCRIPTION`` and ``SET_PERF_METRIC_JOB_UNLOCKED`` as
    initial states, so a run that does not carry all three adapter actions is
    refused at admission (``G-ID-07``) and never reaches this class.  What
    survives here is the by-name refusal: an action the authority did not name
    is still fail-closed, and a partial assignment is refused on construction
    rather than discovered halfway through readiness.
    """

    def __init__(
        self,
        *,
        assigned_actions: frozenset[str],
        recorder: Any,
        authority_record_digest: str | None,
        handlers: Mapping[str, Callable[[], dict[str, Any]]] | None = None,
        vector: Mapping[str, Any] | None = None,
        resolver: Any = None,
        profile_bytes: Mapping[str, bytes] | None = None,
        guard: Any = None,
        allowed_authorities: Sequence[str] = (),
        capture_root: Path | None = None,
        backend: NetconfBackend | None = None,
        ssl_context: ssl.SSLContext | None = None,
        timeout_ms: int = 30000,
        lifecycle: PerfMetricJobLifecycle | None = None,
        readiness: Any = None,
        drain: Callable[[], Mapping[str, Any]] | None = None,
        oauth_header_provider: Callable[[], str] | None = None,
    ) -> None:
        unknown = set(assigned_actions).difference(ADAPTER_ACTIONS)
        if unknown:
            raise NetconfAssignmentRefused("authority assigned unknown NETCONF adapter actions")
        missing = set(ADAPTER_ACTIONS).difference(assigned_actions)
        if missing and assigned_actions:
            raise NetconfAssignmentRefused(
                "a partial NETCONF assignment cannot establish SC-084's "
                "mandatory O1 initial states; %s is unassigned"
                % ",".join(sorted(missing)))
        self.readiness = readiness
        self.drain = drain
        self.oauth_header_provider = oauth_header_provider
        self.assigned_actions = frozenset(assigned_actions)
        self.recorder = recorder
        self.handlers = dict(handlers or {})
        self.vector = vector
        self.resolver = resolver
        self.profile_bytes = dict(profile_bytes or {})
        self.guard = guard
        self.allowed_authorities = tuple(str(item) for item in allowed_authorities)
        self.capture_root = Path(capture_root) if capture_root is not None else None
        self.backend = backend
        self.ssl_context = ssl_context
        self.timeout_ms = int(timeout_ms)
        self.lifecycle = lifecycle
        self._executed: list[str] = []
        recorder.set_netconf_assignment(
            {
                "adapterActionsAssignedToUpper": sorted(self.assigned_actions),
                "authorityRecordDigest": authority_record_digest,
                "roleTablePointer": "/initialStates",
            }
        )
        recorder.set_netconf_session(None)
        recorder.set_perf_metric_job(None)

    @property
    def enabled(self) -> bool:
        return bool(self.assigned_actions)

    # -- start-time proof --------------------------------------------------
    def ensure_startable(self) -> None:
        """Prove, before the run starts, that every assigned action can run.

        A named assignment with no usable transport is the exact failure mode
        that must not be discovered halfway through a scenario, so the proof is
        taken here and the refusal is fail-closed.
        """
        if not self.assigned_actions:
            return
        missing = self.assigned_actions.difference(self.handlers)
        if not missing:
            return
        if self.lifecycle is not None:
            return
        try:
            self.lifecycle = self._build_lifecycle()
        except NetconfAssignmentRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - every failure is fail-closed
            raise NetconfAssignmentRefused(
                "assigned NETCONF actions lack a pinned in-process transport: %s "
                "(%s)" % (",".join(sorted(missing)), exc)) from exc

    def _build_lifecycle(self) -> PerfMetricJobLifecycle:
        if self.vector is None or self.resolver is None:
            raise NetconfAssignmentRefused(
                "assigned NETCONF actions have no deployment vector or secret "
                "resolver bound")
        if self.guard is None or not hasattr(self.guard, "note_ssh_open"):
            raise NetconfAssignmentRefused(
                "assigned NETCONF actions have no egress guard SSH seam bound")
        if self.capture_root is None:
            raise NetconfAssignmentRefused(
                "assigned NETCONF actions have no capture root bound")
        profile = FrozenNetconfProfile(self.profile_bytes)
        resolved = 0
        for entry in tuple(profile.lifecycle) + tuple(profile.teardown):
            if "requestFixture" in entry:
                profile.fixture(str(entry["requestFixture"]))
                resolved += 1
        pa_profile = profile.pa_file_profile()
        lifecycle = PerfMetricJobLifecycle(
            profile=profile, vector=self.vector, resolver=self.resolver,
            recorder=self.recorder, guard=self.guard,
            allowed_authorities=self.allowed_authorities,
            capture_root=self.capture_root, backend=self.backend,
            ssl_context=self.ssl_context, timeout_ms=self.timeout_ms,
            readiness=self.readiness, drain=self.drain,
            oauth_header_provider=self.oauth_header_provider)
        # §10.2 item 1 / INSTALL_O1_PROFILE: both frozen profiles are active and
        # every lifecycle requestFixture resolved to registered immutable bytes.
        # No transport is involved, which is why it can be proved before start.
        if self.readiness is not None:
            self.readiness.confirm("PRECHECK", evidence={
                "profilesActive": True,
                "paFileProfileActive": bool(pa_profile),
                "lifecycleFixturesResolved": resolved,
                "yangClosureDigest": profile.closure_digest(),
            })
            publish = getattr(self.recorder, "set_o1_readiness", None)
            if callable(publish):
                publish(self.readiness.as_capture())
        return lifecycle

    # -- execution ---------------------------------------------------------
    def execute(self, action: str) -> dict[str, Any]:
        if action not in ADAPTER_ACTIONS or action not in self.assigned_actions:
            raise NetconfAssignmentRefused(
                "NETCONF/PerfMetricJob action is OFF without a named authority assignment"
            )
        handler = self.handlers.get(action)
        if handler is not None:
            self._executed.append(action)
            return dict(handler())
        if self.lifecycle is None:
            self.ensure_startable()
        if self.lifecycle is None:
            raise NetconfAssignmentRefused(
                "assigned NETCONF action has no pinned transport integration and fails closed"
            )
        runner = {
            "LOAD_O1_PROFILE": self.lifecycle.load_profile,
            "INSTALL_O1_SUBSCRIPTION": self.lifecycle.install_subscription,
            "SET_PERF_METRIC_JOB": self.lifecycle.set_perf_metric_job,
        }[action]
        outputs = dict(runner())
        self._executed.append(action)
        return outputs

    # -- teardown ----------------------------------------------------------
    def teardown(self) -> list[dict[str, Any]]:
        """Cleanup segment.  Safe to call when nothing was ever started."""
        if self.lifecycle is None:
            return []
        return self.lifecycle.teardown()

    def residual(self) -> list[str]:
        return [] if self.lifecycle is None else self.lifecycle.residual()

    def lifecycle_evidence(self) -> dict[str, list[dict[str, Any]]]:
        if self.lifecycle is None:
            return {"readiness": [], "measured": [], "cleanup": []}
        return self.lifecycle.lifecycle_evidence()

    def session_binding(self) -> dict[str, Any]:
        if self.lifecycle is None:
            return {}
        return self.lifecycle.session_binding()

    def wire_evidence(self) -> dict[str, Any]:
        if self.lifecycle is None:
            return {"available": False, "reason": "LIFECYCLE_NOT_BOUND"}
        return self.lifecycle.wire_evidence()

    def wire_counter_snapshot(self) -> dict[str, Any]:
        if self.lifecycle is None:
            return {"available": False, "reason": "LIFECYCLE_NOT_BOUND"}
        return self.lifecycle.wire_counter_snapshot()

    def mark_measured_segment_end(self) -> dict[str, Any]:
        if self.lifecycle is None:
            return {}
        return self.lifecycle.mark_measured_segment_end()

    def measured_segment_counters(self) -> dict[str, Any]:
        if self.lifecycle is None:
            return {"start": None, "end": None}
        return self.lifecycle.measured_segment_counters()

    def state(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "assignedActions": sorted(self.assigned_actions),
            "executedActions": list(self._executed),
        }


__all__ = [
    "ADAPTER_ACTIONS",
    "FRAMING_CHUNKED",
    "FRAMING_END_OF_MESSAGE",
    "FileDataReportingSubscription",
    "FrozenNetconfProfile",
    "GoldenRpc",
    "NetconfAssignmentRefused",
    "NetconfFramingError",
    "NetconfLifecycleError",
    "NetconfTransportError",
    "OptionalNetconfAdapter",
    "ParamikoNetconfBackend",
    "PerfMetricJobLifecycle",
    "PinnedNetconfSession",
    "SubscriptionError",
    "parse_netconf_endpoint",
]
