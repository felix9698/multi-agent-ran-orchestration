"""Deployment-vector loading and explicit local development-vector construction."""
from __future__ import annotations

from copy import deepcopy
import ipaddress
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from oran.contract.integration_values import load_integration_values as load_contract_integration_values

from .contracts import ContractBundle, ContractError, validate_json_schema


def load_deployment_vector(path: str | Path, bundle: ContractBundle) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("cannot load deployment test vector") from exc
    validate_json_schema(value, bundle.schema("deployment-test-vector"), bundle.path)
    return value


def local_development_vector(values: Mapping[str, Any], *, insecure_dev_loopback: bool) -> dict[str, Any]:
    """Build a vector from caller-supplied values, never from process defaults.

    The caller supplies every deployment identity, endpoint and secret reference;
    this helper only refuses a non-loopback insecure-development request.
    """
    vector = deepcopy(dict(values))
    if not insecure_dev_loopback:
        raise ContractError("local development vector requires explicit insecure development flag")
    endpoints = {
        "r1ApiRoot": vector.get("r1", {}).get("apiRoot"),
        "a1ApiRoot": vector.get("a1", {}).get("apiRoot"),
        "a1StatusCallbackRoot": vector.get("a1", {}).get("statusCallbackRoot"),
        "rAppCallbackRoot": vector.get("r1", {}).get("callbackApi", {}).get("rootUri"),
        "MnSRoot": vector.get("o1", {}).get("fileDataReporting", {}).get("mnsRoot"),
        "o1ConsumerRoot": vector.get("o1", {}).get("fileDataReporting", {}).get("consumerReference"),
        "configured.r1.dme.policyEvidencePushBaseUri": vector.get("r1", {}).get("dme", {}).get("policyEvidencePushBaseUri"),
    }
    # MnSVersion is the eighth root mapping. It is not a URI, but absence must
    # still fail rather than silently falling back to a process default.
    mns_version = vector.get("o1", {}).get("fileDataReporting", {}).get("mnsVersion")
    if not isinstance(mns_version, str) or not mns_version:
        raise ContractError("insecure development MnSVersion root mapping is absent")
    for name, endpoint in endpoints.items():
        try:
            hostname = urlparse(endpoint).hostname if isinstance(endpoint, str) else None
            loopback = hostname == "localhost" or (hostname is not None and ipaddress.ip_address(hostname).is_loopback)
        except ValueError:
            loopback = False
        if not loopback:
            raise ContractError("insecure development endpoint must be loopback: %s" % name)
    return vector


def load_integration_values(path: str | Path) -> dict[str, Any]:
    """Compatibility name backed exclusively by the contract kernel."""
    return load_contract_integration_values(path)
