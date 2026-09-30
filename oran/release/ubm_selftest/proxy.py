"""TLS-terminating mutation proxy used by the transport falsifiers.

Both directions are supported with one class:

* **ingress** — binds the deployment vector's ``a1.apiRoot`` authority and
  forwards to the lower double's real (internal) listener, so a mutation is
  indistinguishable from the *upper* having sent corrupted bytes (UBM-ST-M01);
* **egress** — binds a spare loopback port that the double is told to connect to
  instead of the upper's origin, and forwards to the real upper, so a mutation is
  indistinguishable from the *upper* having answered differently (UBM-ST-M04).

Because the mutation happens on the socket, neither falsifier depends on the
upper being the stand-in: it applies unchanged to the release runtime.
"""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass, field
from typing import Callable, Mapping

from .wire import HttpResponse, ServerReply, TlsHttpServer, authority_of, request

RequestMutator = Callable[[str, str, dict[str, str], bytes], tuple[dict[str, str], bytes]]
ResponseMutator = Callable[[str, str, HttpResponse], HttpResponse]

_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "host"}


@dataclass
class MutationProxy:
    """Forward every request to ``upstream_origin`` applying declared mutations."""

    host: str
    port: int
    upstream_origin: str
    server_context: ssl.SSLContext
    client_context: ssl.SSLContext
    name: str
    upstream_authority: str | None = None
    mutate_request: RequestMutator | None = None
    mutate_response: ResponseMutator | None = None
    forwarded: list[tuple[str, str, int]] = field(default_factory=list)
    _server: TlsHttpServer | None = field(default=None, repr=False)

    def start(self) -> None:
        self._server = TlsHttpServer(
            host=self.host, port=self.port, ssl_context=self.server_context,
            handler=self._handle, name=self.name)
        self._server.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None

    @property
    def authority(self) -> str:
        return "%s:%d" % (self.host, self.port)

    def _handle(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> ServerReply:
        outgoing = {key: value for key, value in headers.items() if key.lower() not in _HOP_BY_HOP}
        payload = body
        if self.mutate_request is not None:
            outgoing, payload = self.mutate_request(method, path, dict(outgoing), payload)
        response = request(
            origin=self.upstream_origin, method=method, path=path,
            headers=outgoing, body=payload or None,
            ssl_context=self.client_context,
            connect_authority=self.upstream_authority or authority_of(self.upstream_origin))
        if self.mutate_response is not None:
            response = self.mutate_response(method, path, response)
        self.forwarded.append((method, path, response.status))
        reply_headers = {
            key: value for key, value in response.headers.items()
            if key.lower() not in _HOP_BY_HOP
        }
        return ServerReply(status=response.status, body=response.body, headers=reply_headers)


def flip_one_policy_byte(method: str, path: str, headers: dict[str, str],
                         body: bytes) -> tuple[dict[str, str], bytes]:
    """UBM-ST-M01: alter exactly one byte of a JSON policy body on an A1 PUT."""
    if method != "PUT" or not body:
        return headers, body
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return headers, body
    marker = body.find(b"\"ncI\":")
    if marker == -1:
        marker = 0
    index = next(
        (position for position in range(marker, len(body)) if body[position:position + 1].isdigit()),
        None)
    if index is None:
        return headers, body
    original = body[index:index + 1]
    replacement = b"9" if original != b"9" else b"8"
    mutated = body[:index] + replacement + body[index + 1:]
    assert len(mutated) == len(body) and mutated != body
    del document
    return headers, mutated


def r1_create_201_to_200(method: str, path: str, response: HttpResponse) -> HttpResponse:
    """UBM-ST-M04: downgrade the R1 policy-create status."""
    if method == "POST" and path.endswith("/a1-policy-management/v1/policies") and response.status == 201:
        response.status = 200
    return response
