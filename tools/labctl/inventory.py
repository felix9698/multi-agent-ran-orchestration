"""Strict, secret-free inventory loading for labctl."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict

from .models import CommandSpec, ComponentSpec, HostSpec, LabInventory


class InventoryError(ValueError):
    """Raised when an inventory cannot be trusted for execution."""


_FORBIDDEN_SECRET_KEYS = {
    "password",
    "passwd",
    "privatekey",
    "private_key",
    "secretvalue",
    "secret_value",
    "token",
}
_PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----", re.IGNORECASE)
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)\b(?:authorization\s*:\s*bearer|password\s*[:=]|passwd\s*[:=]|token\s*[:=])\s*\S+"
)
_READINESS_CATEGORIES = {"CORE", "GNB", "UE", "USRP", "NETWORK", "PREREQUISITE"}


def _reject_embedded_secrets(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).replace("-", "_").lower()
            if normalized in _FORBIDDEN_SECRET_KEYS:
                raise InventoryError(f"password/private key material is forbidden at {path}.{key}")
            _reject_embedded_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_embedded_secrets(child, f"{path}[{index}]")
    elif isinstance(value, str):
        if _PRIVATE_KEY_PATTERN.search(value):
            raise InventoryError(f"private key material is forbidden at {path}")
        if _SECRET_VALUE_PATTERN.search(value):
            raise InventoryError(f"secret-bearing value is forbidden at {path}")


def _required_string(body: Dict[str, Any], key: str, context: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InventoryError(f"{context}.{key} must be a non-empty string")
    return value


def _command(value: Any, context: str, *, required: bool) -> CommandSpec | None:
    if value is None and not required:
        return None
    if not isinstance(value, dict):
        raise InventoryError(f"{context} must be an object")
    argv = value.get("argv")
    if not isinstance(argv, list):
        raise InventoryError(f"{context}.argv must be an array")
    if not argv or any(
        not isinstance(item, str) or not item or "\x00" in item or "\n" in item
        for item in argv
    ):
        raise InventoryError(f"{context}.argv must be a non-empty array of safe strings")
    timeout = value.get("timeoutSeconds", 60)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        raise InventoryError(f"{context}.timeoutSeconds must be a positive integer")
    return CommandSpec(argv=tuple(argv), timeout_seconds=timeout)


def _default_readiness_category(component_id: str) -> str:
    lowered = component_id.lower()
    if lowered == "core":
        return "CORE"
    if lowered.startswith("gnb"):
        return "GNB"
    if lowered.startswith("ue"):
        return "UE"
    return "PREREQUISITE"


def load_inventory(path: str | Path, overlay_path: str | Path | None = None) -> LabInventory:
    source = Path(path)
    try:
        body = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InventoryError(f"cannot read inventory {source}: {exc}") from exc
    if not isinstance(body, dict):
        raise InventoryError("inventory root must be an object")
    _reject_embedded_secrets(body)
    if overlay_path is not None:
        overlay_source = Path(overlay_path)
        try:
            overlay = json.loads(overlay_source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InventoryError(f"cannot read overlay {overlay_source}: {exc}") from exc
        if not isinstance(overlay, dict):
            raise InventoryError("overlay root must be an object")
        _reject_embedded_secrets(overlay)
        if overlay.get("schemaVersion") != "oran-aic-labctl-overlay/1.0.0":
            raise InventoryError("unsupported overlay schemaVersion")
        body = dict(body)
        base_hosts = dict(body.get("hosts", {}))
        for host_id, host in overlay.get("hosts", {}).items():
            if host_id in base_hosts:
                raise InventoryError(f"overlay cannot replace host {host_id}")
            base_hosts[host_id] = host
        body["hosts"] = base_hosts
        body["components"] = list(body.get("components", [])) + list(
            overlay.get("components", [])
        )

    schema_version = _required_string(body, "schemaVersion", "inventory")
    if schema_version != "oran-aic-labctl-inventory/1.0.0":
        raise InventoryError(f"unsupported schemaVersion: {schema_version}")
    profile_id = _required_string(body, "profileId", "inventory")
    objective = _required_string(body, "objective", "inventory")
    state_root = Path(_required_string(body, "stateRoot", "inventory"))

    raw_hosts = body.get("hosts")
    if not isinstance(raw_hosts, dict) or not raw_hosts:
        raise InventoryError("hosts must be a non-empty object")
    hosts: Dict[str, HostSpec] = {}
    for host_id, raw_host in raw_hosts.items():
        if not isinstance(host_id, str) or not host_id or not isinstance(raw_host, dict):
            raise InventoryError("each host must have a non-empty id and object body")
        transport = _required_string(raw_host, "transport", f"hosts.{host_id}")
        if transport not in {"local", "ssh"}:
            raise InventoryError(f"hosts.{host_id}.transport is unsupported: {transport}")
        target = raw_host.get("target")
        if transport == "ssh" and (not isinstance(target, str) or not target):
            raise InventoryError(f"hosts.{host_id}.target is required for ssh")
        if target is not None and not isinstance(target, str):
            raise InventoryError(f"hosts.{host_id}.target must be a string")
        hosts[host_id] = HostSpec(id=host_id, transport=transport, target=target)

    raw_components = body.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise InventoryError("components must be a non-empty array")
    components = []
    seen = set()
    for index, raw_component in enumerate(raw_components):
        context = f"components[{index}]"
        if not isinstance(raw_component, dict):
            raise InventoryError(f"{context} must be an object")
        component_id = _required_string(raw_component, "id", context)
        if component_id in seen:
            raise InventoryError(f"duplicate component id: {component_id}")
        seen.add(component_id)
        host_id = _required_string(raw_component, "host", context)
        if host_id not in hosts:
            raise InventoryError(f"unknown host {host_id} for component {component_id}")
        stage = raw_component.get("stage")
        if not isinstance(stage, int) or isinstance(stage, bool) or stage < 0:
            raise InventoryError(f"{context}.stage must be a non-negative integer")
        dependencies = raw_component.get("dependencies", [])
        if not isinstance(dependencies, list) or any(
            not isinstance(item, str) or not item for item in dependencies
        ):
            raise InventoryError(f"{context}.dependencies must be an array of ids")
        rf = raw_component.get("rf", False)
        if not isinstance(rf, bool):
            raise InventoryError(f"{context}.rf must be boolean")
        stop_policy = raw_component.get("stopPolicy", "owned-only")
        if stop_policy not in {"never", "owned-only", "always"}:
            raise InventoryError(f"{context}.stopPolicy is unsupported: {stop_policy}")
        category = raw_component.get("readinessCategory", _default_readiness_category(component_id))
        if category not in _READINESS_CATEGORIES:
            raise InventoryError(f"{context}.readinessCategory is unsupported: {category}")
        required = raw_component.get("required", True)
        if not isinstance(required, bool):
            raise InventoryError(f"{context}.required must be boolean")
        components.append(
            ComponentSpec(
                id=component_id,
                host=host_id,
                stage=stage,
                dependencies=tuple(dependencies),
                rf=rf,
                stop_policy=stop_policy,
                status=_command(raw_component.get("status"), f"{context}.status", required=True),
                start=_command(raw_component.get("start"), f"{context}.start", required=False),
                stop=_command(raw_component.get("stop"), f"{context}.stop", required=False),
                configure=_command(raw_component.get("configure"), f"{context}.configure", required=False),
                readiness_category=category,
                required=required,
                inventory_index=index,
            )
        )

    component_ids = {component.id for component in components}
    for component in components:
        for dependency in component.dependencies:
            if dependency not in component_ids:
                raise InventoryError(
                    f"unknown dependency {dependency} for component {component.id}"
                )

    inventory = LabInventory(
        schema_version=schema_version,
        profile_id=profile_id,
        objective=objective,
        state_root=state_root,
        hosts=hosts,
        components=tuple(components),
    )
    try:
        inventory.ordered_components()
    except ValueError as exc:
        raise InventoryError("dependency cycle detected") from exc
    return inventory
