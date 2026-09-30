"""Capturing outbound transports for the three upper-originated calls.

C1/C2 keep ``oran.nonrt.a1_client.A1PClient`` as the protocol layer and only
replace its transport, so the A1-P wire contract is still the frozen one.  C3
keeps ``NonRtRicService``'s ``callback_sender`` seam.  Both variants record the
exact request and response bytes, because the capture schema needs raw-byte
digests that exist nowhere else.

Row 4 of SC-083 (``r1-status-to-rapp``) is the one exchange where both ends are
upper; it is recorded once, on the client side, with ``peer: UPPER_SELF``.
"""

from __future__ import annotations

import http.client
import json
import ssl
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

from oran.contract.jcs import canonicalize_bytes
from oran.nonrt.a1_client import TransportResponse

from .capture import (
    json_pointer_jcs_digest, message_digest, redact_headers,
    register_path_identifiers)
from .plan import UNDECLARED_STEP_ID, PlanMatcher


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

    def __init__(self, *, context: ssl.SSLContext, timeout_ms: int) -> None:
        self.context = context
        self.timeout_ms = int(timeout_ms)

    def exchange(self, *, uri: str, method: str, headers: Mapping[str, str],
                 payload: bytes | None) -> tuple[int, dict[str, str], bytes,
                                                 bytes, bytes]:
        parsed = urlsplit(uri)
        if parsed.scheme != "https":
            raise OutboundError("the bilateral profile only issues https calls")
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
        return (status, response_headers, body,
                bytes(connection.sent), bytes(connection.received))


class OutboundRecorderBinding:
    """Holds the recorder/matcher that outbound calls attach their records to."""

    def __init__(self) -> None:
        self.recorder: Any | None = None
        self.matcher: PlanMatcher | None = None
        self.templates: Any = None

    def bind(self, *, recorder: Any, matcher: PlanMatcher | None,
             templates: Any = None) -> None:
        self.recorder = recorder
        self.matcher = matcher
        if templates is not None:
            self.templates = templates

    def reserve(self) -> int | None:
        """Reserve the sequence *before* the call leaves the process."""
        recorder = self.recorder
        return recorder.next_sequence() if recorder is not None else None


def _record_outbound(binding: OutboundRecorderBinding, *, uri: str, method: str,
                     peer: str, request_bytes: bytes, response_status: int,
                     response_headers: Mapping[str, str], response_body: bytes,
                     request_headers: Mapping[str, str], request_body: bytes,
                     timeout_ms: int, sequence: int | None = None) -> None:
    recorder = binding.recorder
    if recorder is None:
        return
    parsed = urlsplit(uri)
    authority = "%s:%d" % (parsed.hostname, parsed.port)
    planned = None
    if binding.matcher is not None:
        planned = binding.matcher.match(method, parsed.path)
    _, redacted_request = redact_headers(request_headers)
    _, redacted_response = redact_headers(response_headers)
    recorder.note_redacted_headers(redacted_request + redacted_response)
    # Register what THIS exchange's own endpoint bindings name before digesting.
    # The A1 PUT is issued from inside the R1 create handler, i.e. before that
    # handler's own response exists, so waiting for the inbound path to register
    # the policy identifier would leave this exchange's digests unstable.
    if planned is not None:
        for member, value in dict(planned.bindings).items():
            if member in {"policyId", "dataJobId", "registrationId",
                          "subscriptionId", "deliveryBindingId"}:
                recorder.register_assigned_identifier(value, member)
    pointer_digests = {}
    digest = json_pointer_jcs_digest(request_body, "/policyObject")
    if digest is not None:
        pointer_digests["/policyObject"] = digest
    endpoint_ref = planned.endpoint_ref if planned else (
        binding.templates.resolve(parsed.path)
        if binding.templates is not None else None)
    register_path_identifiers(recorder, endpoint_ref, parsed.path)
    identifiers = recorder.assigned_identifiers()
    record: dict[str, Any] = {
        "sequence": (recorder.next_sequence() if sequence is None
                     else int(sequence)),
        "stepId": planned.step_id if planned else UNDECLARED_STEP_ID,
        "stepIndex": planned.step_index if planned else 0,
        "logicalTimeMs": planned.at_ms if planned else recorder.clock.now_ms(),
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
        "request": message_digest(
            request_headers, request_body,
            pointer_digests=pointer_digests or None, identifiers=identifiers),
        "response": {
            **message_digest(response_headers, response_body,
                             identifiers=identifiers),
            "status": int(response_status),
            "locationHeader": _header(response_headers, "location"),
            "locationLastPathSegment": _last_segment(
                _header(response_headers, "location")),
        },
        "attemptCount": 1,
        "retryPolicy": "NO_RETRY_DECLARED",
        "timeoutMs": int(timeout_ms),
        "timedOut": False,
        "correlationId": _pointer_string(request_body, "/trace/correlationId"),
        "idempotencyKey": _pointer_string(request_body, "/trace/idempotencyKey"),
        "capturedOutputs": {},
        "declaredExpectedHttpStatus": planned.expected_status if planned else None,
    }
    del request_bytes
    if planned is not None:
        recorder.clock.set_now_ms(planned.at_ms)
    recorder.emit(record)
    recorder.note_attempted_authority(authority)


class CapturingA1Transport:
    """``oran.nonrt.a1_client.Transport`` implementation with capture (C1/C2)."""

    def __init__(self, *, api_root: str, client: CapturingHttpsClient,
                 binding: OutboundRecorderBinding) -> None:
        if urlsplit(api_root).scheme != "https":
            raise OutboundError("a1.apiRoot must be an https authority")
        self.api_root = api_root.rstrip("/")
        self.client = client
        self.binding = binding

    def request(self, method: str, path: str, *,
                query: Mapping[str, str] | None = None,
                body: Any = None) -> TransportResponse:
        uri = self.api_root + path
        if query:
            uri += "?" + urlencode(dict(query))
        # RFC 8785 canonical bytes, so the forwarded policy object is provably
        # unmodified: bodyRawSha256 and bodyJcsSha256 coincide on the A1 PUT.
        payload = None if body is None else canonicalize_bytes(body)
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        sequence = self.binding.reserve()
        status, response_headers, raw, sent, _ = self.client.exchange(
            uri=uri, method=method, headers=headers, payload=payload)
        _record_outbound(
            self.binding, uri=uri, method=method, peer="LOWER_A1",
            request_bytes=sent, response_status=status,
            response_headers=response_headers, response_body=raw,
            request_headers=headers, request_body=payload or b"",
            timeout_ms=self.client.timeout_ms, sequence=sequence)
        parsed = json.loads(raw) if raw else None
        return TransportResponse(status, parsed, response_headers)


class CapturingCallbackSender:
    """``NonRtRicService.callback_sender`` implementation with capture (C3)."""

    def __init__(self, *, client: CapturingHttpsClient,
                 binding: OutboundRecorderBinding, version: str = "1.0.0",
                 observer: Any = None) -> None:
        self.client = client
        self.binding = binding
        self.version = version
        # AGREED_UPPER_OBSERVATION hook: this exchange has both ends upper, so
        # the lower observer needs an ICS-visible counter as well as capture.
        self.observer = observer

    def __call__(self, destination: str, body: Mapping[str, Any]) -> int:
        payload = json.dumps(dict(body), separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Declared by the catalog step r1-status-to-rapp headers block.
            "Version": self.version,
        }
        sequence = self.binding.reserve()
        status, response_headers, raw, sent, _ = self.client.exchange(
            uri=destination, method="POST", headers=headers, payload=payload)
        _record_outbound(
            self.binding, uri=destination, method="POST", peer="UPPER_SELF",
            request_bytes=sent, response_status=status,
            response_headers=response_headers, response_body=raw,
            request_headers=headers, request_body=payload,
            timeout_ms=self.client.timeout_ms, sequence=sequence)
        if self.observer is not None:
            self.observer(int(status))
        return int(status)


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


def _pointer_string(raw: bytes, pointer: str) -> str | None:
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
