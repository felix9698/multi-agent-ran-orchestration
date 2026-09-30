"""Standard A1-P v2 Consumer with no Near-RT implementation dependency."""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .config import require_https_or_loopback


@dataclass(frozen=True)
class TransportResponse:
    status: int
    body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        body: Any = None,
    ) -> TransportResponse: ...


class A1Transport:
    """Small HTTPS transport; credentials/tokens are supplied by hooks."""

    def __init__(
        self,
        api_root: str,
        *,
        insecure_dev_mode: bool = False,
        ssl_context: ssl.SSLContext | None = None,
        authorization_hook: Any = None,
        timeout_seconds: float = 10.0,
    ):
        require_https_or_loopback(api_root, insecure_dev_mode)
        self.api_root = api_root.rstrip("/")
        self.ssl_context = ssl_context
        self.authorization_hook = authorization_hook
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        body: Any = None,
    ) -> TransportResponse:
        uri = f"{self.api_root}{path}"
        if query:
            uri += "?" + urlencode(query)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.authorization_hook is not None:
            headers.update(self.authorization_hook())
        request = Request(uri, data=payload, headers=headers, method=method)
        try:
            response = urlopen(  # noqa: S310 - URI policy checked in constructor
                request,
                timeout=self.timeout_seconds,
                context=self.ssl_context,
            )
            raw = response.read()
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError as exc:
                # A 2xx whose body is not JSON is a broken peer, not a refusal:
                # OSError keeps it on the uncertain (503) side (2026-09-23 audit).
                raise OSError("A1-P answered with a non-JSON body") from exc
            return TransportResponse(response.status, parsed, dict(response.headers))
        except HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                # An HTML 500/502 from a proxy must keep its status.  Raising here
                # became 400 AIC_SCHEMA_INVALID upstream -- "unknown" turned into
                # "definitively refused" (2026-09-23 audit).
                parsed = None
            return TransportResponse(exc.code, parsed, dict(exc.headers))


class A1ProtocolError(RuntimeError):
    def __init__(self, response: TransportResponse):
        # 2026-09-23: the producer says *why* it refused ({"error": ...}); dropping
        # it left "409" as the only trace of a hand-back refused for a dead UE.
        # 2026-09-29: A1-P answers problem+json, whose reason is "detail" (e.g. "UE scope is
        # already owned by policy <id>."), not "error" -- the owner id never reached the adapter.
        body = response.body if isinstance(response.body, dict) else {}
        reason = body.get("error") or body.get("detail")
        super().__init__(f"A1-P returned HTTP {response.status}"
                         + (f": {reason}" if reason else ""))
        self.response = response


class A1PClient:
    POLICY_ROOT = "/A1-P/v2/policytypes"

    def __init__(self, transport: Transport):
        self.transport = transport

    @staticmethod
    def _part(value: str) -> str:
        return quote(value, safe="")

    def discover_policy_types(self) -> list[Any]:
        response = self.transport.request("GET", self.POLICY_ROOT)
        self._expect(response, 200)
        return list(response.body)

    def get_policy_type(self, policy_type_id: str) -> Any:
        response = self.transport.request(
            "GET", f"{self.POLICY_ROOT}/{self._part(policy_type_id)}"
        )
        self._expect(response, 200)
        return response.body

    def list_policy_ids(self, policy_type_id: str) -> list[str]:
        response = self.transport.request(
            "GET", f"{self.POLICY_ROOT}/{self._part(policy_type_id)}/policies"
        )
        self._expect(response, 200)
        return list(response.body)

    def put_policy(
        self,
        policy_type_id: str,
        policy_id: str,
        policy_object: dict[str, Any],
        notification_destination: str | None,
    ) -> TransportResponse:
        query = (
            {"notificationDestination": notification_destination}
            if notification_destination is not None
            else None
        )
        response = self.transport.request(
            "PUT",
            f"{self.POLICY_ROOT}/{self._part(policy_type_id)}/policies/{self._part(policy_id)}",
            query=query,
            body=policy_object,
        )
        self._expect(response, 200, 201)
        return response

    def get_policy(self, policy_type_id: str, policy_id: str) -> dict[str, Any] | None:
        response = self.transport.request(
            "GET",
            f"{self.POLICY_ROOT}/{self._part(policy_type_id)}/policies/{self._part(policy_id)}",
        )
        if response.status == 404:
            return None
        self._expect(response, 200)
        return response.body

    def delete_policy(self, policy_type_id: str, policy_id: str) -> None:
        response = self.transport.request(
            "DELETE",
            f"{self.POLICY_ROOT}/{self._part(policy_type_id)}/policies/{self._part(policy_id)}",
        )
        self._expect(response, 204)

    def get_status(self, policy_type_id: str, policy_id: str) -> dict[str, Any]:
        response = self.transport.request(
            "GET",
            f"{self.POLICY_ROOT}/{self._part(policy_type_id)}/policies/{self._part(policy_id)}/status",
        )
        self._expect(response, 200)
        return response.body

    def find_by_idempotency_key(
        self, policy_type_id: str, idempotency_key: str
    ) -> list[tuple[str, dict[str, Any]]]:
        matches: list[tuple[str, dict[str, Any]]] = []
        for policy_id in self.list_policy_ids(policy_type_id):
            policy = self.get_policy(policy_type_id, policy_id)
            if policy is None:
                continue
            trace = policy.get("trace", {})
            if trace.get("idempotencyKey") == idempotency_key:
                matches.append((policy_id, policy))
        return matches

    @staticmethod
    def _expect(response: TransportResponse, *statuses: int) -> None:
        if response.status not in statuses:
            raise A1ProtocolError(response)
