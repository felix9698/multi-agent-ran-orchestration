"""Stdlib HTTP adapter for :class:`NonRtRicService`.

The harness routes are explicitly a development control plane, not O-RAN R1.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .service import NonRtRicService, ResponseDropped, SimulatedCrash


class NonRtHttpServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], service: NonRtRicService,
                 path_prefix: str = ""):
        if address[0] != service.security.listen_host:
            raise ValueError("HTTP listen host must match the validated security profile")
        self.service = service
        self.path_prefix = path_prefix.rstrip("/")
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: NonRtHttpServer

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def _handle(self) -> None:
        body: Any = None
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length:
            raw = self.rfile.read(content_length)
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = raw.decode("utf-8", errors="replace")
        try:
            target = self.path
            parsed_path = urlsplit(target).path
            if (self.server.path_prefix
                    and parsed_path.startswith(self.server.path_prefix + "/")):
                target = target[len(self.server.path_prefix):]
            response = self.server.service.handle(
                self.command,
                target,
                headers=dict(self.headers),
                body=body,
            )
        except ResponseDropped:
            self.close_connection = True
            return
        except SimulatedCrash:
            # The outer process supervisor performs the actual restart.  Close
            # this request without manufacturing a response.
            self.close_connection = True
            return
        payload = b"" if response.body is None else json.dumps(response.body).encode("utf-8")
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        if payload:
            self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        # Host applications provide structured/redacted logging.
        return
