"""Minimal stdlib TLS HTTP plumbing shared by the double, the proxy and the stub.

Deliberately tiny and dependency-free so that the lower double cannot smuggle in
upper runtime behaviour through a shared HTTP client.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit


class SocketAuthorityViolation(RuntimeError):
    """A connection was attempted to an authority outside the allowlist."""


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None


def authority_of(uri: str) -> str:
    parts = urlsplit(uri)
    if parts.port is None:
        raise ValueError("uri %r has no explicit port" % uri)
    host = "[%s]" % parts.hostname if ":" in (parts.hostname or "") else parts.hostname
    return "%s:%d" % (host, parts.port)


def origin_of(uri: str) -> str:
    parts = urlsplit(uri)
    return "%s://%s" % (parts.scheme, authority_of(uri))


class SocketAuthorityGuard:
    """Fail-closed egress allowlist (UBM-ST-X01).

    Any ``connect`` to an authority outside the allowlist raises immediately, so
    a stray live-target or hardware call aborts the run instead of being counted
    after the fact.
    """

    def __init__(self, allowlist: Sequence[str]):
        self.allowlist = set(allowlist)
        self.attempted: list[str] = []
        self._original: Callable[..., Any] | None = None
        self._lock = threading.Lock()

    def allow(self, authority: str) -> None:
        self.allowlist.add(authority)

    def __enter__(self) -> "SocketAuthorityGuard":
        guard = self
        self._original = socket.socket.connect

        def _connect(sock: socket.socket, address: Any) -> Any:  # noqa: ANN401
            if sock.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple):
                host, port = address[0], address[1]
                rendered = "[%s]:%d" % (host, port) if ":" in str(host) else "%s:%d" % (host, port)
                with guard._lock:
                    if rendered not in guard.attempted:
                        guard.attempted.append(rendered)
                if rendered not in guard.allowlist:
                    raise SocketAuthorityViolation(
                        "connection to %s is outside the self-test authority allowlist %s"
                        % (rendered, sorted(guard.allowlist)))
            return guard._original(sock, address)  # type: ignore[misc]

        socket.socket.connect = _connect  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._original is not None:
            socket.socket.connect = self._original  # type: ignore[assignment]
            self._original = None

    def unexpected(self) -> list[str]:
        return [item for item in self.attempted if item not in self.allowlist]


def request(*, origin: str, method: str, path: str,
            headers: Mapping[str, str] | None = None,
            body: bytes | None = None,
            ssl_context: ssl.SSLContext,
            connect_authority: str | None = None,
            timeout_ms: int = 30000) -> HttpResponse:
    """Issue one HTTPS request; no retry, no redirect following."""
    target = connect_authority or authority_of(origin)
    host, _, port = target.rpartition(":")
    host = host.strip("[]")
    server_hostname = urlsplit(origin).hostname
    connection = http.client.HTTPSConnection(
        host, int(port), context=ssl_context, timeout=timeout_ms / 1000.0)
    try:
        connection.request(method, path, body=body, headers=dict(headers or {}))
        raw = connection.getresponse()
        payload = raw.read()
        collected = {key.lower(): value for key, value in raw.getheaders()}
        return HttpResponse(status=raw.status, headers=collected, body=payload)
    finally:
        connection.close()


def json_request(*, origin: str, method: str, path: str, payload: Any = None,
                 headers: Mapping[str, str] | None = None, **kwargs: Any) -> HttpResponse:
    merged = {"Accept": "application/json"}
    merged.update(dict(headers or {}))
    body: bytes | None = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        merged.setdefault("Content-Type", "application/json")
    return request(origin=origin, method=method, path=path, headers=merged, body=body, **kwargs)


Handler = Callable[[str, str, Mapping[str, str], bytes], "ServerReply"]


@dataclass
class ServerReply:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, status: int, payload: Any, headers: Mapping[str, str] | None = None) -> "ServerReply":
        raw = b"" if payload is None else json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        merged = dict(headers or {})
        if raw:
            merged.setdefault("Content-Type", "application/json")
        return cls(status=status, body=raw, headers=merged)


class TlsHttpServer:
    """One TLS listener that delegates every request to a single callable."""

    def __init__(self, *, host: str, port: int, ssl_context: ssl.SSLContext,
                 handler: Handler, name: str):
        self.host = host
        self.port = port
        self.name = name
        self._handler = handler

        outer = self

        class _RequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "ubm-selftest/1.0"
            sys_version = ""

            def log_message(self, *args: Any) -> None:  # noqa: D401 - silence stderr
                return

            def _dispatch(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                incoming = {key.lower(): value for key, value in self.headers.items()}
                try:
                    reply = outer._handler(method, self.path, incoming, body)
                except Exception as error:  # fail closed, never a silent 200
                    reply = ServerReply.json(500, {"error": type(error).__name__, "detail": str(error)})
                # send_response_only: no automatic Server/Date headers.  ``Date`` is
                # a wall-clock read on the response path and would make every
                # header-block digest differ between two otherwise identical runs.
                self.send_response_only(reply.status)
                for key, value in reply.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                if reply.body:
                    self.wfile.write(reply.body)

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def do_PUT(self) -> None:
                self._dispatch("PUT")

            def do_DELETE(self) -> None:
                self._dispatch("DELETE")

        class _Server(ThreadingHTTPServer):
            daemon_threads = True
            # SO_REUSEADDR only skips TIME_WAIT; a port with a live listener still
            # fails to bind, so a genuinely occupied authority remains a hard error
            # and no fallback port is ever chosen.
            allow_reuse_address = True

        self._server = _Server((host, port), _RequestHandler)
        self._server.socket = ssl_context.wrap_socket(self._server.socket, server_side=True)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="ubm-selftest-%s" % name, daemon=True)

    @property
    def authority(self) -> str:
        return "%s:%d" % (self.host, self.port)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
