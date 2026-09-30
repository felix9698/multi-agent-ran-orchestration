"""Capturing outbound transports for the upper-originated calls.

SC-084 gives the upper exactly two outbound obligations: ``observe-a1-put``
towards the lower A1 implementation under test, and ``o1-retrieve`` towards the
Provider's SFTP surface (which W1 owns).  This module covers the HTTPS half.

The A1 protocol layer stays the frozen ``oran.nonrt.a1_client.A1PClient``; only
its transport is replaced, so the A1-P wire contract is unchanged and the
capture gains the raw request and response bytes that exist nowhere else.
"""

from __future__ import annotations

import http.client
import json
from contextlib import nullcontext
import ssl
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

from oran.contract.jcs import canonicalize_bytes
from oran.nonrt.a1_client import TransportResponse

from .capture import message_digest, redact_headers
from .plan import UNDECLARED_STEP_ID


class OutboundError(RuntimeError):
    """An upper-originated call could not be issued."""


class _TeeSocket:
    """Delegating socket proxy whose ``makefile`` records what was read."""

    def __init__(self, sock: Any, sink: bytearray) -> None:
        self._sock = sock
        self._sink = sink

    def makefile(self, *args: Any, **kwargs: Any) -> Any:
        return _TeeFile(self._sock.makefile(*args, **kwargs), self._sink)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sock, name)


class _TeeFile:
    def __init__(self, stream: Any, sink: bytearray) -> None:
        self._stream = stream
        self._sink = sink

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        chunk = self._stream.read(*args, **kwargs)
        self._sink.extend(chunk)
        return chunk

    def readline(self, *args: Any, **kwargs: Any) -> bytes:
        chunk = self._stream.readline(*args, **kwargs)
        self._sink.extend(chunk)
        return chunk

    def readinto(self, buffer: Any) -> int:
        count = self._stream.readinto(buffer)
        if count:
            self._sink.extend(bytes(memoryview(buffer)[:count]))
        return count

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _RecordingConnection(http.client.HTTPSConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sent = bytearray()
        self.received = bytearray()

    def connect(self) -> None:
        super().connect()
        self.sock = _TeeSocket(self.sock, self.received)

    def send(self, data: Any) -> None:
        if isinstance(data, (bytes, bytearray)):
            self.sent.extend(bytes(data))
        super().send(data)


class CapturingHttpsClient:
    """One-shot HTTPS exchange that records the exact wire bytes."""

    def __init__(self, *, context: ssl.SSLContext | None, timeout_ms: int) -> None:
        self.context = context
        self.timeout_ms = int(timeout_ms)

    def exchange(self, *, uri: str, method: str, headers: Mapping[str, str],
                 payload: bytes | None) -> tuple[int, dict[str, str], bytes]:
        parsed = urlsplit(uri)
        if parsed.scheme != "https":
            raise OutboundError("the live-O1 profile only issues https calls")
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        connection = _RecordingConnection(
            parsed.hostname, parsed.port, context=self.context,
            timeout=self.timeout_ms / 1000.0)
        try:
            connection.request(method, target, body=payload, headers=dict(headers))
            response = connection.getresponse()
            body = response.read()
            status = response.status
            response_headers = {str(name): str(value)
                                for name, value in response.getheaders()}
        finally:
            connection.close()
        return status, response_headers, body


class OutboundRecorderBinding:
    """Holds the recorder/matcher that outbound calls attach their records to."""

    def __init__(self) -> None:
        self.recorder: Any | None = None
        self.matcher: Any | None = None
        self.templates: Any = None
        self.completion_barrier: Any = None

    def bind(self, *, recorder: Any, matcher: Any,
             templates: Any = None, completion_barrier: Any = None) -> None:
        self.recorder = recorder
        self.matcher = matcher
        if templates is not None:
            self.templates = templates
        if completion_barrier is not None:
            self.completion_barrier = completion_barrier

    def capture_operation(self):
        return (self.completion_barrier()
                if self.completion_barrier is not None else nullcontext())

    def reserve(self) -> int | None:
        """Reserve the sequence *before* the call leaves the process."""
        recorder = self.recorder
        return recorder.next_sequence() if recorder is not None else None

    def record(self, *, uri: str, method: str, peer: str, timeout_ms: int,
               request_headers: Mapping[str, str], request_body: bytes,
               response_status: int, response_headers: Mapping[str, str],
               response_body: bytes, sequence: int | None = None) -> None:
        recorder = self.recorder
        if recorder is None:
            return
        parsed = urlsplit(uri)
        authority = "%s:%d" % (parsed.hostname, parsed.port)
        planned = self.matcher.match(method, parsed.path) if self.matcher else None
        _, redacted_request = redact_headers(request_headers)
        _, redacted_response = redact_headers(response_headers)
        recorder.note_redacted_headers(redacted_request + redacted_response)
        if planned is not None:
            for member, value in dict(planned.bindings).items():
                if member in {"policyId", "dataJobId", "registrationId",
                              "subscriptionId", "deliveryBindingId"}:
                    recorder.register_assigned_identifier(value, member)
        identifiers = recorder.assigned_identifiers()
        endpoint_ref = planned.endpoint_ref if planned else (
            self.templates.resolve(parsed.path) if self.templates else None)
        location = response_headers.get("Location")
        response = message_digest(response_headers, response_body,
                                  identifiers=identifiers)
        response["status"] = int(response_status)
        response["locationHeader"] = location
        response["locationLastPathSegment"] = (
            str(location).rstrip("/").rsplit("/", 1)[-1] if location else None)
        recorder.emit({
            "sequence": (recorder.next_sequence() if sequence is None
                         else int(sequence)),
            "stepId": planned.step_id if planned else UNDECLARED_STEP_ID,
            "stepIndex": planned.step_index if planned else 0,
            "observedAt": recorder.clock.now(),
            "monotonicOffsetMs": recorder.clock.monotonic_offset_ms(),
            "direction": "UPPER_OUTBOUND",
            "role": "CLIENT",
            "peer": peer,
            "transport": {
                "scheme": "https",
                "peerAuthority": authority,
                "tlsProtocol": None,
                "peerAuthenticated": True,
            },
            "method": method.upper(),
            "resolvedUri": uri,
            "endpointRef": endpoint_ref,
            "bindings": dict(planned.bindings) if planned else {},
            "request": message_digest(request_headers, request_body,
                                      identifiers=identifiers),
            "response": response,
            "attemptCount": 1,
            "timeoutMs": int(timeout_ms),
            "timedOut": False,
            "correlationId": dict(request_headers).get("X-Correlation-Id"),
            "idempotencyKey": dict(request_headers).get("Idempotency-Key"),
            "capturedOutputs": {},
            "declaredExpectedHttpStatus": planned.expected_status if planned else None,
        })


class CapturingA1Transport:
    """The A1-P transport seam: frozen protocol, recorded bytes."""

    def __init__(self, *, api_root: str, client: CapturingHttpsClient,
                 binding: OutboundRecorderBinding) -> None:
        self.api_root = str(api_root).rstrip("/")
        self.client = client
        self.binding = binding

    def request(self, method: str, path: str, *, query: Mapping[str, Any] | None = None,
                body: Any = None, headers: Mapping[str, str] | None = None
                ) -> TransportResponse:
        uri = self.api_root + path
        if query:
            uri += "?" + urlencode({key: str(value) for key, value in query.items()})
        payload = b""
        request_headers = {"Accept": "application/json"}
        request_headers.update(dict(headers or {}))
        if body is not None:
            payload = canonicalize_bytes(body)
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(payload))
        # A status notification is only a hint.  Non-RT RIC therefore performs
        # this authoritative A1 status GET before it persists or relays a new
        # producer epoch.  The frozen ten-step result vector counts the emitted
        # callback, not this protocol-internal confirmation query.  Execute the
        # real request (and let the egress guard measure it), but do not consume
        # a catalog exchange slot.
        authoritative_status_query = (
            method.upper() == "GET"
            and path.startswith("/A1-P/v2/policytypes/")
            and "/policies/" in path
            and path.rstrip("/").endswith("/status"))
        with self.binding.capture_operation():
            sequence = None if authoritative_status_query else self.binding.reserve()
            status, response_headers, response_body = self.client.exchange(
                uri=uri, method=method.upper(), headers=request_headers,
                payload=payload or None)
            if not authoritative_status_query:
                self.binding.record(
                    uri=uri, method=method, peer="LOWER_A1",
                    timeout_ms=self.client.timeout_ms,
                    request_headers=request_headers, request_body=payload,
                    response_status=status, response_headers=response_headers,
                    response_body=response_body, sequence=sequence)
        decoded: Any = None
        if response_body:
            try:
                decoded = json.loads(response_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = None
        return TransportResponse(status=int(status), headers=dict(response_headers),
                                 body=decoded)


class CapturingCallbackSender:
    """The R1 status relay to the rApp: an upper-to-upper exchange."""

    def __init__(self, *, client: CapturingHttpsClient,
                 binding: OutboundRecorderBinding,
                 observer: Any = None) -> None:
        self.client = client
        self.binding = binding
        self.observer = observer

    def __call__(self, uri: str, body: Mapping[str, Any]) -> int:
        return self.send(uri, body)

    def send(self, uri: str, body: Mapping[str, Any],
             headers: Mapping[str, str] | None = None) -> int:
        payload = canonicalize_bytes(body)
        request_headers = {"Content-Type": "application/json",
                           "Content-Length": str(len(payload)),
                           "Accept": "application/json",
                           "Version": "1.0.0"}
        request_headers.update(dict(headers or {}))
        with self.binding.capture_operation():
            sequence = self.binding.reserve()
            status, response_headers, response_body = self.client.exchange(
                uri=uri, method="POST", headers=request_headers, payload=payload)
            self.binding.record(
                uri=uri, method="POST", peer="UPPER_SELF",
                timeout_ms=self.client.timeout_ms,
                request_headers=request_headers, request_body=payload,
                response_status=status, response_headers=response_headers,
                response_body=response_body, sequence=sequence)
        if self.observer is not None:
            self.observer(int(status))
        return int(status)
