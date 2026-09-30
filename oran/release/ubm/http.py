"""Capture-instrumented TLS front-ends for the four upper listeners.

Capture must see the *wire* bytes, so the instrumentation sits on the handler's
own streams: the request and response file objects are teed, the existing
component handler runs untouched, and the exact bytes it consumed and produced
are digested afterwards.  Nothing here re-implements an O-RAN route.

The release control plane (``/ubm/v1/*``) and the Integration Control Surface
(``/harness/*``) are intercepted before the component handler and are not
O-RAN exchanges: the control plane is not captured at all and the ICS is
audited into ``harnessOperations`` with argument *key names* only.
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from .capture import (
    register_path_identifiers,
    CaptureError, http_date_from_instant, json_pointer_jcs_digest,
    message_digest, redact_headers,
)
from .ics import IntegrationControlSurface
from .plan import UNDECLARED_STEP_ID, PlanMatcher

CONTROL_PREFIX = "/ubm/v1/"
HARNESS_PREFIX = "/harness/"


class ListenerError(RuntimeError):
    """A listener could not be bound; there is never a port fallback."""

    exit_code = 78


class CaptureContext:
    """Per-listener capture wiring, rebound at every scenario reset."""

    def __init__(self, *, component: str, base_uri: str, authority: str,
                 timeout_ms: int, templates: Any = None,
                 suppressed_capture_paths: tuple[str, ...] = ()):
        self.component = component
        self.base_uri = base_uri.rstrip("/")
        self.authority = authority
        self.timeout_ms = int(timeout_ms)
        self.templates = templates
        self.suppressed_capture_paths = tuple(suppressed_capture_paths)
        self.recorder: Any | None = None
        self.matcher: PlanMatcher | None = None
        #: Set by the service; used only to tell an upper-originated request
        #: apart from a counterpart-originated one.
        self.egress: Any | None = None
        self.ics: IntegrationControlSurface | None = None
        self.control: Callable[[str, str, Mapping[str, Any] | None],
                               tuple[int, dict[str, Any]]] | None = None
        self._lock = threading.RLock()

    def bind(self, *, recorder: Any, matcher: PlanMatcher | None) -> None:
        with self._lock:
            self.recorder = recorder
            self.matcher = matcher

    def resolved_uri(self, path: str) -> str:
        return self.base_uri + path

    def peer_for(self, client_address: Any) -> str:
        """Who sent this request.

        The upper's own components talk to each other over the same loopback
        listeners the counterpart uses, so the authority alone cannot tell them
        apart.  The egress guard observes every connection this process opens,
        and attribution requires a LIVE one whose local endpoint is this
        request's client address and whose remote endpoint is this very
        listener.  A source port recycled by an unrelated process therefore
        does not match, and an endpoint the guard cannot resolve is reported as
        ``UNATTRIBUTED`` rather than optimistically claimed as the upper's.  No
        wire marker is invented to carry the distinction.
        """
        guard = self.egress
        if guard is None:
            return "LOWER_RUNNER"
        verdict = guard.attribute_peer(client_address, self.authority)
        if verdict == "UPPER_SELF":
            return "UPPER_SELF"
        if verdict == "UNATTRIBUTED":
            return "UNATTRIBUTED"
        return "LOWER_RUNNER"

    def record(self, *, method: str, path: str, request_bytes: bytes,
               response_bytes: bytes, sequence: int | None = None,
               client_address: Any = None) -> None:
        recorder = self.recorder
        if recorder is None or path in self.suppressed_capture_paths:
            return
        request = _parse_request(request_bytes)
        response = _parse_response(response_bytes)
        if request is None or response is None:
            return
        planned = None
        with self._lock:
            if self.matcher is not None:
                planned = self.matcher.match(method, path)
        _, redacted_request = redact_headers(request["headers"])
        _, redacted_response = redact_headers(response["headers"])
        recorder.note_redacted_headers(redacted_request + redacted_response)
        pointer_digests = {}
        digest = json_pointer_jcs_digest(request["body"], "/policyObject")
        if digest is not None:
            pointer_digests["/policyObject"] = digest
        response_pointer_digests = {}
        digest = json_pointer_jcs_digest(response["body"], "/policyObject")
        if digest is not None:
            response_pointer_digests["/policyObject"] = digest
        endpoint_ref = planned.endpoint_ref if planned else (
            self.templates.resolve(path) if self.templates is not None else None)
        # Register before digesting: the creating response is the first place an
        # assigned identifier exists, and its own body/headers already carry it.
        register_path_identifiers(recorder, endpoint_ref, path)
        captured = _captured_outputs(path, response["headers"], endpoint_ref,
                                     request["body"])
        for member, value in captured.items():
            if member in {"policyId", "dataJobId", "registrationId",
                          "subscriptionId", "deliveryBindingId"}:
                recorder.register_assigned_identifier(value, member)
        identifiers = recorder.assigned_identifiers()
        record: dict[str, Any] = {
            "sequence": (recorder.next_sequence() if sequence is None
                         else int(sequence)),
            "stepId": planned.step_id if planned else UNDECLARED_STEP_ID,
            "stepIndex": planned.step_index if planned else 0,
            "logicalTimeMs": (planned.at_ms if planned
                              else recorder.clock.now_ms()),
            "direction": "UPPER_INBOUND",
            "role": "SERVER",
            "peer": self.peer_for(client_address),
            "transport": {
                "scheme": "https",
                "peerAuthority": self.authority,
                "tlsProtocol": None,
                "peerAuthenticated": False,
            },
            "method": method.upper(),
            "resolvedUri": self.resolved_uri(path),
            "endpointRef": endpoint_ref,
            "bindings": dict(planned.bindings) if planned else {},
            "request": message_digest(
                request["headers"], request["body"],
                pointer_digests=pointer_digests or None,
                identifiers=identifiers),
            "response": {
                **message_digest(response["headers"], response["body"],
                                 pointer_digests=response_pointer_digests or None,
                                 identifiers=identifiers),
                "status": response["status"],
                "locationHeader": _header(response["headers"], "location"),
                "locationLastPathSegment": _last_segment(
                    _header(response["headers"], "location")),
            },
            "attemptCount": 1,
            "retryPolicy": "NO_RETRY_DECLARED",
            "timeoutMs": self.timeout_ms,
            "timedOut": False,
            "correlationId": _json_pointer_string(request["body"],
                                                  "/trace/correlationId"),
            "idempotencyKey": _json_pointer_string(request["body"],
                                                   "/trace/idempotencyKey"),
            "capturedOutputs": captured,
            "declaredExpectedHttpStatus": planned.expected_status if planned else None,
        }
        if planned is not None:
            recorder.clock.set_now_ms(planned.at_ms)
        recorder.emit(record)
        recorder.note_attempted_authority(self.authority)


class _TeeReader:
    """Records every byte the handler actually consumed from the socket."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.buffer = bytearray()

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        chunk = self._stream.read(*args, **kwargs)
        self.buffer.extend(chunk)
        return chunk

    def readline(self, *args: Any, **kwargs: Any) -> bytes:
        chunk = self._stream.readline(*args, **kwargs)
        self.buffer.extend(chunk)
        return chunk

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _TeeWriter:
    """Records every byte the handler actually produced."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.buffer = bytearray()

    def write(self, payload: Any) -> Any:
        self.buffer.extend(bytes(payload))
        return self._stream.write(payload)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def capturing_handler(base: type, context: CaptureContext) -> type:
    """Wrap a component's handler class with capture and the two control planes."""

    class Captured(base):  # type: ignore[misc,valid-type]
        capture_context = context

        def log_message(self, _format: str, *args: Any) -> None:
            return

        def send_response(self, code: int, message: str | None = None) -> None:
            # ``BaseHTTPRequestHandler.send_response`` appends ``Server`` and a
            # wall-clock ``Date``.  Both are suppressed here: ``Date`` would be
            # a wall-clock read on the request path (G-DET-1) and would make the
            # response header-block digest differ between two runs (G-DET-3).
            # No catalog step declares either header.
            self.log_request(code)
            self.send_response_only(code, message)

        def version_string(self) -> str:
            # Constant, in case a base handler emits Server explicitly.
            return "oran-aic-upper-bilateral-mock/1.0.0"

        def date_time_string(self, timestamp: Any = None) -> str:
            # Logical time, never the wall clock, if a base handler asks.
            recorder = self.capture_context.recorder
            if recorder is None:
                return http_date_from_instant("1970-01-01T00:00:00Z")
            return http_date_from_instant(recorder.clock.at(recorder.clock.now_ms()))

        def parse_request(self) -> bool:
            # The capture sequence is reserved at request *entry* so a nested
            # upper-originated call can never be ordered before the inbound
            # request that triggered it.
            self._ubm_sequence = None
            accepted = super().parse_request()
            if accepted:
                path = urlsplit(self.path).path
                recorder = self.capture_context.recorder
                if recorder is not None and not path.startswith(CONTROL_PREFIX) \
                        and not path.startswith(HARNESS_PREFIX) \
                        and path not in self.capture_context.suppressed_capture_paths:
                    self._ubm_sequence = recorder.next_sequence()
            return accepted

        def handle_one_request(self) -> None:
            reader, writer = _TeeReader(self.rfile), _TeeWriter(self.wfile)
            self.rfile, self.wfile = reader, writer
            try:
                super().handle_one_request()
            finally:
                self.rfile, self.wfile = reader._stream, writer._stream
                if reader.buffer and writer.buffer:
                    parsed = _parse_request(bytes(reader.buffer))
                    if parsed is not None and not parsed["path"].startswith(
                            CONTROL_PREFIX) and not parsed["path"].startswith(
                            HARNESS_PREFIX):
                        self.capture_context.record(
                            method=parsed["method"], path=parsed["path"],
                            request_bytes=bytes(reader.buffer),
                            response_bytes=bytes(writer.buffer),
                            sequence=getattr(self, "_ubm_sequence", None),
                            client_address=getattr(self, "client_address", None))

        # -- control planes ------------------------------------------------
        def _intercept(self) -> bool:
            path = urlsplit(self.path).path
            if path.startswith(CONTROL_PREFIX):
                status, payload = self._control(path)
                self._emit_json(status, payload)
                return True
            if path.startswith(HARNESS_PREFIX):
                status, payload = self._harness_surface(path)
                self._emit_json(status, payload)
                return True
            return False

        def _control(self, path: str) -> tuple[int, dict[str, Any]]:
            handler = self.capture_context.control
            if handler is None:
                return 404, {"code": "AIC_RESOURCE_NOT_FOUND"}
            try:
                return handler(self.command, path, self._body())
            except Exception as exc:  # fail closed with a machine-readable code
                return 500, {"code": "AIC_INTERNAL_ERROR",
                             "detail": "%s: %s" % (type(exc).__name__, exc)}

        def _harness_surface(self, path: str) -> tuple[int, dict[str, Any]]:
            surface = self.capture_context.ics
            if surface is None:
                return 404, {"code": "AIC_RESOURCE_NOT_FOUND"}
            try:
                return surface.handle(self.command, path, self._body())
            except CaptureError:
                raise
            except Exception as exc:  # fail closed without leaking internals
                return 400, {"code": "AIC_SCHEMA_INVALID",
                             "detail": type(exc).__name__}

        def _body(self) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if not length:
                return None
            raw = self.rfile.read(length)
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return decoded if isinstance(decoded, dict) else None

        def _emit_json(self, status: int, payload: Any) -> None:
            raw = b"" if payload is None else json.dumps(
                payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            if raw:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self._intercept():
                return
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            if self._intercept():
                return
            super().do_POST()

    Captured.__name__ = base.__name__ + "Captured"
    return Captured


class DispatchServer(ThreadingHTTPServer):
    """Thin HTTP shell around a component's own ``dispatch`` method.

    ``oran.profiles.local_mock`` has an equivalent shell, but importing it would
    pull that module's schema-rewrite shadow into the bilateral import graph,
    which gate G-TLS-1 forbids.  (The shadow's function name is deliberately not
    written out anywhere under this package: G-TLS-1 is enforced as a byte grep
    over the packaged members, so naming it here would trip the gate it states.)
    """

    daemon_threads = True

    def __init__(self, address: tuple[str, int], dispatch: Callable[..., Any]):
        self.component_dispatch = dispatch
        super().__init__(address, DispatchHandler)


class DispatchHandler(BaseHTTPRequestHandler):
    server: DispatchServer
    server_version = "oran-aic-upper-bilateral-mock/1.0.0"

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def _run(self) -> None:
        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        body: Mapping[str, Any] | None = None
        if raw:
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                self.send_error(400)
                return
            if not isinstance(decoded, dict):
                self.send_error(400)
                return
            body = decoded
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        try:
            result = self.server.component_dispatch(
                self.command, parsed.path, body, query=query)
        except TypeError:
            result = self.server.component_dispatch(self.command, parsed.path, body)
        status = int(getattr(result, "status", 0) or 503)
        payload = b"" if getattr(result, "body", None) is None else json.dumps(
            result.body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        for name, value in (getattr(result, "headers", None) or {}).items():
            self.send_header(str(name), str(value))
        if payload:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    do_GET = _run
    do_POST = _run
    do_PUT = _run
    do_DELETE = _run


def bind_tls(server: ThreadingHTTPServer, context: ssl.SSLContext) -> None:
    server.socket = context.wrap_socket(server.socket, server_side=True)


def assert_port_free(host: str, port: int) -> None:
    """G-DET-5: a bound port is a hard failure, never an ephemeral fallback."""
    probe = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET,
                          socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
    except OSError as exc:
        raise ListenerError(
            "listen authority %s:%d is already in use; the bilateral profile "
            "never falls back to another port" % (host, port)) from exc
    finally:
        probe.close()


# -- wire parsing ---------------------------------------------------------

def _parse_request(raw: bytes) -> dict[str, Any] | None:
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        return None
    lines = head.split(b"\r\n")
    try:
        method, target, _ = lines[0].decode("latin-1").split(" ", 2)
    except ValueError:
        return None
    return {
        "method": method.upper(),
        "path": urlsplit(target).path,
        "target": target,
        "headers": _parse_headers(lines[1:]),
        "body": body,
    }


def _parse_response(raw: bytes) -> dict[str, Any] | None:
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        return None
    lines = head.split(b"\r\n")
    parts = lines[0].decode("latin-1").split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return {
        "status": int(parts[1]),
        "headers": _parse_headers(lines[1:]),
        "body": body,
    }


def _parse_headers(lines: list[bytes]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in lines:
        if not line:
            continue
        name, separator, value = line.decode("latin-1").partition(":")
        if not separator:
            continue
        headers[name.strip()] = value.strip()
    return headers


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def _last_segment(location: str | None) -> str | None:
    if not location:
        return None
    return urlsplit(location).path.rstrip("/").rsplit("/", 1)[-1] or None


def _json_pointer_string(raw: bytes, pointer: str) -> str | None:
    try:
        document = json.loads(bytes(raw or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    current: Any = document
    for token in pointer.split("/")[1:]:
        if not isinstance(current, Mapping) or token not in current:
            return None
        current = current[token]
    return str(current) if isinstance(current, str) else None


def _captured_outputs(path: str, headers: Mapping[str, str],
                      reference: str | None,
                      request_body: bytes = b"") -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    segment = _last_segment(_header(headers, "location"))
    if segment:
        if reference == "#/endpointTemplates/r1Policies":
            outputs["policyId"] = segment
        elif reference == "#/endpointTemplates/r1DmeDataJobs":
            outputs["dataJobId"] = segment
        elif reference == "#/endpointTemplates/r1DmeProductionCapabilities":
            outputs["registrationId"] = segment
        elif reference == "#/endpointTemplates/r1PolicySubscriptions":
            outputs["subscriptionId"] = segment
    if reference == "#/endpointTemplates/r1DmePushDestination":
        binding = path.rstrip("/").rsplit("/", 1)[-1]
        if binding:
            outputs["deliveryBindingId"] = binding
    if reference == "#/endpointTemplates/r1DmeDataJobs":
        # The binding a data job pushes to is minted by whoever creates the job
        # and appears only inside the request body's dataPushUri.  Capturing it
        # here is what lets the canonical digests neutralise it; without that
        # the create request's own digest can never be stable across runs.
        push = _json_pointer_string(
            request_body, "/pushDeliveryDetailsHttp/dataPushUri")
        segment = _last_segment(push)
        if segment:
            outputs["deliveryBindingId"] = segment
    return outputs
