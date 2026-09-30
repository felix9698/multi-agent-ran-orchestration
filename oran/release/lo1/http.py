"""Inbound HTTPS listeners and the inbound half of the capture seam.

Four listeners are bound from the deployment vector alone: the R1 API root, the
rApp callback root (which co-hosts the DME push destination and the A1 status
callback), and the O1 consumer root.  Nothing here adds an O-RAN route; the
routes come from the components the composition root wires in.

Two fail-closed properties live here:

* **G-DET-3** -- a port that is already bound is a hard failure with exit 78.
  There is no fallback port and no ephemeral bind, because a harness that
  silently moved would be observed at an authority the gate never admitted.
* **peer attribution is never guessed** -- an inbound request is attributed to
  ``UPPER_SELF`` only when a live connection this process opened has exactly this
  client address as its local endpoint AND this listener as its remote endpoint.
  ``UNATTRIBUTED`` is the fail-closed answer, and it is a finding rather than a
  default.
"""

from __future__ import annotations

import socket
import ssl
import threading
import hashlib
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from .capture import message_digest, redact_headers

EXIT_CONFIG = 78

# Private metadata shared only by the in-process dispatcher and handler.  It
# is removed before headers are written to the wire.  A readiness refusal is
# outside SC-084's measured body and therefore must not become an exchange row.
SUPPRESS_CAPTURE_HEADER = "X-LO1-Internal-Suppress-Capture"
TRUSTED_TLS_CERT_SHA256_HEADER = "X-LO1-Trusted-TLS-Peer-Certificate-Sha256"
TRUSTED_TLS_ROLE_HEADER = "X-LO1-Trusted-TLS-Peer-Role"
TRUSTED_TLS_PROTOCOL_HEADER = "X-LO1-Trusted-TLS-Protocol"
TRUSTED_TCP_PEER_HEADER = "X-LO1-Trusted-TCP-Peer-Authority"
CALLER_SPOOFABLE_INTERNAL_HEADERS = frozenset({
    "x-lo1-internal-tls-peer-authority",
    TRUSTED_TLS_CERT_SHA256_HEADER.lower(), TRUSTED_TLS_ROLE_HEADER.lower(),
    TRUSTED_TLS_PROTOCOL_HEADER.lower(), TRUSTED_TCP_PEER_HEADER.lower(),
})

#: What the capture calls a peer, given the guard's attribution answer.
_PEERS = {
    "UPPER_SELF": "UPPER_SELF",
    "COUNTERPART": "LOWER_RUNNER",
    "UNATTRIBUTED": "UNATTRIBUTED",
}


class ListenerError(RuntimeError):
    """A listener could not be bound exactly as the vector declares it."""

    exit_code = EXIT_CONFIG


class CaptureBoundaryClosed(RuntimeError):
    """The end-of-body boundary has closed before this request began."""


class CapturePendingBarrier:
    """Mechanical request/capture drain, independent of O1 readiness.

    A request stays leased until its response bytes and capture row have both
    been emitted.  This is intentionally a separate state machine from the O1
    measured-body gate: readiness seed operations also create audit/capture
    rows, so the end boundary must wait for them without requiring readiness.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._open = True
        self._in_flight = 0
        self._local = threading.local()

    @contextmanager
    def lease(self, *, admitted_child: bool = False):
        with self._condition:
            depth = int(getattr(self._local, "depth", 0))
            # A root operation admitted before drain may synchronously issue an
            # outbound child on the same thread.  An upper-self callback can
            # arrive on a listener thread; the egress attribution proves that
            # it belongs to a currently open upper operation.  Unrelated late
            # requests remain refused.
            if not self._open and depth == 0 and not (
                    admitted_child and self._in_flight > 0):
                raise CaptureBoundaryClosed(
                    "the run capture boundary is draining")
            self._in_flight += 1
            self._local.depth = depth + 1
        try:
            yield
        finally:
            with self._condition:
                self._in_flight -= 1
                self._local.depth = max(
                    0, int(getattr(self._local, "depth", 1)) - 1)
                if self._in_flight == 0:
                    self._condition.notify_all()

    def close_and_wait(self, *, timeout_seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._condition:
            self._open = False
            while self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CaptureBoundaryClosed(
                        "%d HTTP capture operation(s) remained in flight"
                        % self._in_flight)
                self._condition.wait(timeout=remaining)

    @property
    def in_flight(self) -> int:
        with self._condition:
            return self._in_flight


def assert_port_free(host: str, port: int) -> None:
    """G-DET-3: a bound port is a hard failure, never an ephemeral fallback."""
    probe = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET,
                          socket.SOCK_STREAM)
    try:
        # The production listener uses the same option.  This permits an exact
        # endpoint to be rebound after a clean shutdown while prior accepted
        # connections remain in TIME_WAIT; it does not permit a second bind
        # while the original listener is still active (SO_REUSEPORT is never
        # enabled).
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, int(port)))
    except OSError as exc:
        raise ListenerError(
            "listen authority %s:%d is already in use; the live-O1 profile "
            "never falls back to another port" % (host, int(port))) from exc
    finally:
        probe.close()


Dispatch = Callable[[str, str, Mapping[str, str], bytes], tuple[int, dict[str, str], bytes]]
_UNPREPARED = object()


@dataclass(frozen=True)
class _PreparedCapture:
    planned: Any
    attributed_peer: str | None
    sequence: int | None
    observed_at: str | None
    monotonic_offset_ms: int | None


@dataclass
class CaptureContext:
    """Everything one listener needs to record an inbound exchange."""

    component: str
    base_uri: str
    authority: str
    timeout_ms: int
    recorder: Any = None
    matcher: Any = None
    templates: Any = None
    egress: Any = None
    suppressed_paths: Sequence[str] = field(default_factory=tuple)
    suppress_upper_self: bool = False
    completion_barrier: Callable[[bool], Any] | None = None
    trusted_tls_roles: Mapping[str, str] = field(default_factory=dict)

    def capture_barrier(self, client_address: Any):
        child = False
        if self.egress is not None:
            child = (self.egress.attribute_peer(
                client_address, self.authority) == "UPPER_SELF")
        return (self.completion_barrier(child)
                if self.completion_barrier is not None else _null_barrier())

    def suppressed(self, path: str) -> bool:
        target = str(path).rstrip("/")
        return any(target == str(item).rstrip("/") for item in self.suppressed_paths)

    def prepare_record(self, *, method: str, path: str,
                       client_address: Any) -> _PreparedCapture | None:
        """Reserve an inbound catalog sequence before nested work starts.

        R1 create synchronously emits A1 PUT, and an inbound A1 status
        synchronously relays an R1 callback.  Reserving only after dispatch
        would invert both catalog pairs in the final capture.  Suppressed
        upper-self server halves are rejected before matching so they cannot
        consume the outbound step they mirror.
        """
        if self.recorder is None or self.suppressed(path):
            return None
        attributed_peer = None
        if self.egress is not None:
            attributed_peer = self.egress.attribute_peer(
                client_address, self.authority)
            if self.suppress_upper_self and attributed_peer == "UPPER_SELF":
                return None
        planned = self.matcher.match(method, path) if self.matcher is not None else None
        sequence = None
        observed_at = None
        monotonic_offset_ms = None
        if planned is not None and str(planned.step_id) != "o1-notify":
            sequence = self.recorder.next_sequence()
            observed_at = self.recorder.clock.now()
            monotonic_offset_ms = self.recorder.clock.monotonic_offset_ms()
        return _PreparedCapture(
            planned, attributed_peer, sequence,
            observed_at, monotonic_offset_ms)

    def record(self, *, method: str, path: str, request_headers: Mapping[str, str],
               request_body: bytes, status: int,
               response_headers: Mapping[str, str], response_body: bytes,
               client_address: Any, tls_protocol: str | None = None,
               peer_authenticated: bool = False,
               prepared: _PreparedCapture | None | object = _UNPREPARED) -> None:
        recorder = self.recorder
        if prepared is _UNPREPARED:
            prepared = self.prepare_record(
                method=method, path=path, client_address=client_address)
        if recorder is None or prepared is None:
            return
        planned = prepared.planned
        attributed_peer = prepared.attributed_peer
        sequence = prepared.sequence
        observed_at = prepared.observed_at
        monotonic_offset_ms = prepared.monotonic_offset_ms
        if sequence is None:
            # Direct unit callers and undeclared exchanges have no nested
            # catalog operation to order ahead of, so allocate on emission.
            sequence = recorder.next_sequence()
            observed_at = recorder.clock.now()
            monotonic_offset_ms = recorder.clock.monotonic_offset_ms()
        if planned is not None and str(planned.step_id) == "o1-notify":
            # O1 notification has a dedicated capture vector.  Successful
            # dispatch suppresses this generic record; a direct call cannot
            # turn it into a duplicate exchange.
            if prepared is not _UNPREPARED:
                return
        _retained, redacted = redact_headers(request_headers)
        recorder.note_redacted_headers(redacted)
        identifiers = recorder.assigned_identifiers()
        location = response_headers.get("Location")
        response = message_digest(response_headers, response_body,
                                  identifiers=identifiers)
        response["status"] = int(status)
        response["locationHeader"] = location
        response["locationLastPathSegment"] = (
            str(location).rstrip("/").rsplit("/", 1)[-1] if location else None)
        peer = _PEERS.get(attributed_peer, "UNATTRIBUTED")
        record = {
            "sequence": int(sequence),
            "stepId": planned.step_id if planned else "UNDECLARED_EXCHANGE",
            "stepIndex": planned.step_index if planned else 0,
            "observedAt": str(observed_at),
            "monotonicOffsetMs": int(monotonic_offset_ms),
            "direction": "UPPER_INBOUND",
            "role": "SERVER",
            "peer": peer,
            "transport": {
                "scheme": "https",
                "peerAuthority": _client_authority(client_address),
                "tlsProtocol": tls_protocol,
                "peerAuthenticated": bool(peer_authenticated),
            },
            "method": method,
            "resolvedUri": self.base_uri.rstrip("/") + path,
            "endpointRef": (planned.endpoint_ref if planned
                            else (self.templates.resolve(path)
                                  if self.templates is not None else None)),
            "bindings": dict(planned.bindings) if planned else {},
            "request": message_digest(request_headers, request_body,
                                      identifiers=identifiers),
            "response": response,
            "attemptCount": 1,
            "timeoutMs": int(self.timeout_ms),
            "timedOut": False,
            "correlationId": request_headers.get("X-Correlation-Id"),
            "idempotencyKey": request_headers.get("Idempotency-Key"),
            "capturedOutputs": {},
            "declaredExpectedHttpStatus": planned.expected_status if planned else None,
        }
        recorder.emit(record)


class DispatchServer(ThreadingHTTPServer):
    """One TLS listener whose routing is a single dispatch callable."""

    daemon_threads = True
    # A clean stop must not strand the frozen endpoint behind TIME_WAIT.  An
    # active listener remains a hard conflict because SO_REUSEPORT is absent.
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], dispatch: Dispatch,
                 context: CaptureContext) -> None:
        self.dispatch = dispatch
        self.capture_context = context
        super().__init__(address, _handler_class())

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A handler error must never be swallowed into a 200; the connection is
        # dropped and the exception propagates to the owning thread's log.
        raise


def bind_tls(server: ThreadingHTTPServer, context: ssl.SSLContext | None) -> None:
    if context is None:
        return
    server.socket = context.wrap_socket(server.socket, server_side=True)


def _handler_class() -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "lo1"
        sys_version = ""

        def log_message(self, *_args: Any) -> None:  # noqa: D102 - silence
            return

        def _serve(self, method: str) -> None:
            server: Any = self.server
            context: CaptureContext = server.capture_context
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            target = self.path
            path = urlsplit(target).path
            wire_headers = {key: value for key, value in self.headers.items()}
            headers = {key: value for key, value in wire_headers.items()
                       if str(key).lower() not in CALLER_SPOOFABLE_INTERNAL_HEADERS}
            tls_protocol, certificate_digest = _tls_peer(self.connection)
            peer_role = context.trusted_tls_roles.get(
                str(certificate_digest), "UNATTRIBUTED")
            if certificate_digest is not None:
                headers[TRUSTED_TLS_CERT_SHA256_HEADER] = certificate_digest
                headers[TRUSTED_TLS_ROLE_HEADER] = peer_role
                headers[TRUSTED_TLS_PROTOCOL_HEADER] = str(tls_protocol or "")
                headers[TRUSTED_TCP_PEER_HEADER] = _client_authority(
                    self.client_address)
            barrier = context.capture_barrier(self.client_address)
            try:
                with barrier:
                    prepared = context.prepare_record(
                        method=method, path=path,
                        client_address=self.client_address)
                    try:
                        status, response_headers, payload = server.dispatch(
                            method, target, headers, body)
                    except Exception as exc:  # noqa: BLE001 - fail closed, never 200
                        status, response_headers, payload = 500, {
                            "Content-Type": "application/problem+json"}, str(exc).encode()
                    self.send_response(int(status))
                    outgoing = dict(response_headers or {})
                    suppress_capture = (
                        outgoing.pop(SUPPRESS_CAPTURE_HEADER, None) == "1")
                    outgoing.setdefault("Content-Length", str(len(payload)))
                    for key, value in outgoing.items():
                        self.send_header(key, value)
                    self.end_headers()
                    if payload:
                        self.wfile.write(payload)
                    if not suppress_capture:
                        context.record(
                            method=method, path=path, request_headers=wire_headers,
                            request_body=body, status=int(status),
                            response_headers=outgoing, response_body=payload,
                            client_address=self.client_address,
                            tls_protocol=tls_protocol,
                            peer_authenticated=certificate_digest is not None,
                            prepared=prepared)
            except CaptureBoundaryClosed as exc:
                payload = str(exc).encode("utf-8")
                self.send_response(409)
                self.send_header("Content-Type", "application/problem+json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            self._serve("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._serve("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._serve("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._serve("DELETE")

    return _Handler


class _null_barrier:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: Any) -> None:
        return None


def serve_in_thread(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, kwargs={
        "poll_interval": 0.05}, daemon=True)
    thread.start()
    return thread


def _client_authority(client_address: Any) -> str:
    if isinstance(client_address, tuple) and len(client_address) >= 2:
        host, port = str(client_address[0]), int(client_address[1])
        return "[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)
    # Fail-closed sentinel: an unattributable client address is a finding,
    # and it is never written as an address the deployment could match.
    return "unattributed-peer:0"


def _tls_peer(connection: Any) -> tuple[str | None, str | None]:
    """Return facts from the actual TLS socket, never from request headers."""
    if not isinstance(connection, ssl.SSLSocket):
        return None, None
    try:
        certificate = connection.getpeercert(binary_form=True)
        protocol = connection.version()
    except (ssl.SSLError, OSError):
        return None, None
    return (str(protocol) if protocol else None,
            hashlib.sha256(certificate).hexdigest() if certificate else None)
