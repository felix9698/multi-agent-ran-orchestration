"""Configuration boundary for the Non-RT component.

Deployment values are always loaded through the shared integration-values API.
The local loader is only a merge-time compatibility shim with the same
``load(path) -> values mapping`` signature.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

def load(path: str | Path, *, bundle_dir: str | Path | None = None) -> Mapping[str, Any]:
    from oran.contract.integration_values import load_integration_values
    return load_integration_values(path)["values"]


@dataclass(frozen=True)
class SecurityProfile:
    """Transport policy; insecure mode is test-only and loopback-only."""

    insecure_dev_mode: bool = False
    listen_host: str = ""
    auth_hook: Any = None
    # Loopback-only, two-key approved integration control surface.  TLS is
    # mandatory on the bilateral path, so ``insecure_dev_mode`` cannot be the
    # predicate that exposes the contract-declared harness operations.
    integration_control_approved: bool = False

    def validate(self) -> None:
        if not self.insecure_dev_mode:
            if self.auth_hook is None:
                raise ValueError("production profile requires an mTLS/OAuth auth hook")
            if self.integration_control_approved:
                self._require_loopback()
            return
        self._require_loopback()

    def _require_loopback(self) -> None:
        host = self.listen_host
        if not host:
            raise ValueError("insecure development mode requires an explicit loopback host")
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback:
            raise ValueError("insecure development mode is restricted to loopback")

    def authenticated_rapp_id(self, headers: Mapping[str, str]) -> str:
        if self.auth_hook is not None:
            identity = self.auth_hook(headers)
        elif self.insecure_dev_mode:
            identity = headers.get("x-authenticated-rapp-id", "")
        else:  # pragma: no cover - validate() prevents this configuration
            identity = ""
        if not identity:
            raise PermissionError("authenticated rApp identity is required")
        return str(identity)


def require_https_or_loopback(uri: str, insecure_dev_mode: bool) -> None:
    parsed = urlsplit(uri)
    if parsed.scheme == "https":
        return
    if insecure_dev_mode and parsed.scheme == "http" and parsed.hostname:
        try:
            if ipaddress.ip_address(parsed.hostname).is_loopback:
                return
        except ValueError:
            if parsed.hostname == "localhost":
                return
    raise ValueError("transport URI must use HTTPS (HTTP is loopback dev-only)")
