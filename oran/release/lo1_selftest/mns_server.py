"""The Provider's FileDataReportingMnS HTTPS surface, and the notification hop.

Every route this server answers is derived from
``oran-aic-o1-pa-file.1.0.0.json#/delivery/subscription`` -- the collection
resource, the item resource, the files resource, the HTTP methods and the
success statuses are all read from those bytes.  A request that does not match
a derived route is answered 404 and counted as an unknown route, which is what
``G-EMU-1`` measures.

The notification hop is the Provider calling the *consumer*: the emulator POSTs
a ``notifyFileReady`` document, shaped from ``golden/o1/notify-file-ready.json``
but carrying this run's own href, event time and file locations, to the
consumer reference the deployment vector names.  The consumer's 204 is
recorded, never assumed.
"""

from __future__ import annotations

import json
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping

from .tls import LOOPBACK, LoopbackTls

#: The notification boundary header the Provider owes on every file-ready
#: notification (A10).  The receiver binds a notification to an active
#: subscription through it and never mints it for itself.  The self-test may
#: not import the runtime (G-SPLIT-1), so the name is restated here and
#: compared with ``oran.release.lo1.o1_consumer`` by test.
SUBSCRIPTION_ID_HEADER = "x-lo1-internal-subscription-id"


class MnsError(RuntimeError):
    """The MnS surface could not be derived from the frozen profile bytes."""


@dataclass
class MnsRoutes:
    """Route paths derived from the frozen PM file profile."""

    subscriptions: str
    subscription_item_prefix: str
    files: str
    create_method: str
    create_status: int
    delete_method: str
    delete_status: int
    delete_absent_status: int
    list_method: str
    list_status: int
    list_required_query: tuple[str, ...]
    notification_success_status: int

    @classmethod
    def derive(cls, pa_profile: Mapping[str, Any], *, mns_version: str) -> "MnsRoutes":
        subscription = pa_profile["delivery"]["subscription"]

        def path_of(template: str) -> str:
            rendered = template.replace("{MnSRoot}", "").replace("{MnSVersion}", mns_version)
            if not rendered.startswith("/"):
                raise MnsError(f"derived MnS route is not absolute: {template!r}")
            return rendered

        item = path_of(subscription["itemResource"])
        prefix = item.split("{subscriptionId}")[0]
        return cls(
            subscriptions=path_of(subscription["collectionResource"]),
            subscription_item_prefix=prefix,
            files=path_of(subscription["filesResource"]),
            create_method=str(subscription["createHttpMethod"]),
            create_status=int(subscription["createSuccessStatus"]),
            delete_method=str(subscription["deleteHttpMethod"]),
            delete_status=int(subscription["deleteSuccessStatus"]),
            delete_absent_status=int(subscription["deleteAlreadyAbsentStatus"]),
            list_method=str(subscription["listFilesHttpMethod"]),
            list_status=int(subscription["listFilesSuccessStatus"]),
            list_required_query=tuple(subscription["listFilesRequiredQuery"]),
            notification_success_status=int(subscription["notificationSuccessStatus"]),
        )


@dataclass
class MnsObservations:
    requests: list[dict[str, Any]] = field(default_factory=list)
    unknown_routes: int = 0
    subscriptions: dict[str, dict[str, Any]] = field(default_factory=dict)
    notifications_sent: list[dict[str, Any]] = field(default_factory=list)


class _Handler(BaseHTTPRequestHandler):
    server_version = "lo1-selftest-provider-emulator"
    sys_version = ""

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt, *args):  # noqa: A003 - silence stderr chatter
        return

    @property
    def _emulator(self) -> "MnsServer":
        return self.server.emulator  # type: ignore[attr-defined]

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _respond(self, status: int, payload: Any = None, *, location: str | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
        else:
            self.send_header("Content-Length", "0")
        if location:
            self.send_header("Location", location)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _record(self, method: str, path: str, status: int, *, known: bool) -> None:
        observations = self._emulator.observations
        with self._emulator.lock:
            observations.requests.append(
                {"method": method, "path": path, "status": status, "knownRoute": known})
            if not known:
                observations.unknown_routes += 1

    # -- verbs -------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol
        routes = self._emulator.routes
        parsed = urllib.parse.urlsplit(self.path)
        body = self._read_body()
        if parsed.path == routes.subscriptions and self.command == routes.create_method:
            document = json.loads(body.decode("utf-8") or "{}")
            identifier = self._emulator.mint_subscription(document)
            location = f"{routes.subscription_item_prefix}{identifier}"
            self._record("POST", parsed.path, routes.create_status, known=True)
            # Match the immutable Provider 1.0.3 wire contract: allocation is
            # authoritative only in Location.  The representation remains a
            # complete echo of the created resource but does not duplicate the
            # server-assigned identifier in a non-contractual body field.
            self._respond(routes.create_status, document, location=location)
            return
        self._record("POST", parsed.path, 404, known=False)
        self._respond(404, {"title": "no such MnS resource"})

    def do_DELETE(self) -> None:  # noqa: N802
        routes = self._emulator.routes
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.startswith(routes.subscription_item_prefix):
            identifier = parsed.path[len(routes.subscription_item_prefix):]
            with self._emulator.lock:
                present = self._emulator.observations.subscriptions.pop(identifier, None)
            status = routes.delete_status if present else routes.delete_absent_status
            self._record("DELETE", parsed.path, status, known=True)
            self._respond(status)
            return
        self._record("DELETE", parsed.path, 404, known=False)
        self._respond(404, {"title": "no such MnS resource"})

    def do_GET(self) -> None:  # noqa: N802
        routes = self._emulator.routes
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == routes.files and self.command == routes.list_method:
            query = urllib.parse.parse_qs(parsed.query)
            missing = [name for name in routes.list_required_query if name not in query]
            if missing:
                self._record("GET", parsed.path, 400, known=True)
                self._respond(400, {"title": "missing required query", "missing": missing})
                return
            self._record("GET", parsed.path, routes.list_status, known=True)
            self._respond(routes.list_status, self._emulator.files_listing())
            return
        self._record("GET", parsed.path, 404, known=False)
        self._respond(404, {"title": "no such MnS resource"})


class MnsServer:
    """HTTPS FileDataReportingMnS surface plus the notification client."""

    def __init__(
        self,
        *,
        routes: MnsRoutes,
        tls: LoopbackTls,
        files_provider: Callable[[], list[dict[str, Any]]],
    ) -> None:
        self.routes = routes
        self.tls = tls
        self.observations = MnsObservations()
        self.lock = threading.Lock()
        self._files_provider = files_provider
        self._subscription_seq = 0
        self._server = ThreadingHTTPServer((LOOPBACK, 0), _Handler)
        self._server.emulator = self  # type: ignore[attr-defined]
        self._server.socket = tls.server_context().wrap_socket(
            self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._consumer_context: ssl.SSLContext | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    @property
    def authority(self) -> str:
        host, port = self._server.server_address[:2]
        return f"{host}:{port}"

    @property
    def root(self) -> str:
        return f"https://{self.authority}"

    def trust_consumer(self, context: ssl.SSLContext) -> None:
        """Bind the trust anchor the notification hop will verify against."""
        self._consumer_context = context

    # -- state -------------------------------------------------------------

    def mint_subscription(self, document: Mapping[str, Any]) -> str:
        with self.lock:
            self._subscription_seq += 1
            identifier = f"emu-subscription-{self._subscription_seq:04d}"
            self.observations.subscriptions[identifier] = dict(document)
        return identifier

    def active_subscription_id(self) -> str | None:
        """The identifier this server returned at subscription create, if any."""
        with self.lock:
            identifiers = sorted(self.observations.subscriptions)
        return identifiers[-1] if identifiers else None

    def files_listing(self) -> list[dict[str, Any]]:
        return self._files_provider()

    # -- notification hop ---------------------------------------------------

    def send_notification(self, uri: str, document: Mapping[str, Any],
                          *, timeout: float = 10.0,
                          carry_subscription_header: bool = True) -> int:
        """POST ``notifyFileReady``, carrying the boundary header the receiver binds.

        The upper's receiver correlates a notification with an active
        subscription through ``x-lo1-internal-subscription-id`` and deliberately
        does not mint it, so the *sender* owes it -- here, the identifier this
        server minted at subscription create.  ``carry_subscription_header`` is
        false only for the negative demonstration that a notification without it
        is refused.
        """
        if self._consumer_context is None:
            raise MnsError(
                "the notification hop has no trust anchor; the emulator refuses to "
                "POST notifyFileReady over an unverified TLS connection")
        payload = json.dumps(document).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if carry_subscription_header:
            identifier = self.active_subscription_id()
            if identifier is None:
                raise MnsError(
                    "no subscription has been created, so there is no "
                    "identifier to bind this notification to; the emulator "
                    "does not invent one")
            headers[SUBSCRIPTION_ID_HEADER] = identifier
        request = urllib.request.Request(
            uri, data=payload, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(
                    request, timeout=timeout, context=self._consumer_context) as response:
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
        with self.lock:
            self.observations.notifications_sent.append(
                {"uri": uri, "status": status,
                 "notificationId": document.get("notificationId")})
        return status
