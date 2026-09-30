"""Draft 2020-12 validation with format assertion for vendored schemas.

``jsonschema==4.23.0`` is pinned because it implements Draft 2020-12 and its
``referencing`` registry keeps every schema lookup offline.  Supplying its
format checker turns UUID/URI/date-time from annotations into assertions.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from .digests import selected_contract_root


class ContractSchemaError(ValueError):
    """A vendored schema is unsafe or malformed."""


def _walk_refs(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        if isinstance(value.get("$ref"), str):
            yield value["$ref"]
        for child in value.values():
            yield from _walk_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_refs(child)


def _assert_no_network_refs(schema: dict[str, Any], name: str) -> None:
    for reference in _walk_refs(schema):
        if reference.startswith(("http:", "https:", "//")):
            raise ContractSchemaError(f"network $ref is forbidden in {name}: {reference}")


class ContractValidator:
    """Offline `$id` registry and strict validators for the contract bundle."""

    def __init__(self, bundle_root: Path | None = None) -> None:
        self.bundle_root = bundle_root or selected_contract_root() / "shared-contract-bundle"
        self.schemas_by_name: dict[str, dict[str, Any]] = {}
        self.schema_names_by_alias: dict[str, str] = {}
        registry = Registry()
        for path in sorted(self.bundle_root.glob("*schema.json")):
            if path.name.startswith("._"):
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "$schema" not in data:
                continue
            _assert_no_network_refs(data, path.name)
            try:
                Draft202012Validator.check_schema(data)
            except SchemaError as exc:
                raise ContractSchemaError(f"invalid vendored schema {path.name}: {exc.message}") from exc
            self.schemas_by_name[path.name] = data
            self.schema_names_by_alias[path.name] = path.name
            resource = Resource.from_contents(data, default_specification=DRAFT202012)
            registry = registry.with_resource(path.name, resource)
            if isinstance(data.get("$id"), str):
                self.schema_names_by_alias[data["$id"]] = path.name
                registry = registry.with_resource(data["$id"], resource)
        # Component branches historically used these contract-level identifiers.
        # They remain accepted aliases, but always resolve to the immutable kernel
        # schema and never select a component-local validator.
        compatibility_aliases = {
            "AIC_UECellSteering_1.0.0": "AIC_UECellSteering_1.0.0.policy.schema.json",
            "AIC_UECellSteering_1.0.0.policy": "AIC_UECellSteering_1.0.0.policy.schema.json",
            "AIC_UECellSteering_1.0.0.status": "AIC_UECellSteering_1.0.0.status.schema.json",
            "aic:policy-evidence:1.0.0": "aic.policy-evidence.1.0.0.schema.json",
            "aic:ran-capability:1.0.0": "aic.ran-capability.1.0.0.schema.json",
            "oran-aic-integration-values/1.0.0": "integration-values.1.0.0.schema.json",
            "deployment-test-vector": "deployment-test-vector.1.0.0.schema.json",
            "integration-values": "integration-values.1.0.0.schema.json",
        }
        for alias, name in compatibility_aliases.items():
            if name in self.schemas_by_name:
                self.schema_names_by_alias[alias] = name
        self.registry = registry
        self.format_checker: FormatChecker = Draft202012Validator.FORMAT_CHECKER

    def resolve_name(self, name_or_id: str) -> str:
        """Resolve a vendored filename, schema ``$id``, or frozen alias."""
        if not isinstance(name_or_id, str):
            raise ContractSchemaError("schema name or id must be a string")
        try:
            return self.schema_names_by_alias[name_or_id]
        except KeyError as exc:
            raise ContractSchemaError(f"unknown vendored schema: {name_or_id}") from exc

    def schema(self, name_or_id: str) -> dict[str, Any]:
        return self.schemas_by_name[self.resolve_name(name_or_id)]

    def validator(self, name: str) -> Draft202012Validator:
        return Draft202012Validator(self.schema(name), registry=self.registry, format_checker=self.format_checker)

    def validate(self, name: str, instance: Any) -> None:
        """Raise ``ValidationError`` if ``instance`` violates the named schema."""
        self.validator(name).validate(instance)

    def errors(self, name: str, instance: Any) -> list[ValidationError]:
        return sorted(self.validator(name).iter_errors(instance), key=lambda error: list(error.path))


@lru_cache(maxsize=None)
def _validator_for(bundle_root: Path) -> ContractValidator:
    return ContractValidator(bundle_root)


def default_validator() -> ContractValidator:
    """The selected bundle's validator, built once per bundle root.

    Building one re-reads and meta-checks every vendored schema (~0.9 s on the
    OTA host); doing that per R1 request stretched a live trial past its A1
    policy window (2026-09-15 attempt 40).  The bundle is frozen, so the root
    is the whole cache key; callers must not mutate returned schemas.
    """
    return _validator_for(selected_contract_root() / "shared-contract-bundle")


def validate(name: str, instance: Any) -> None:
    """Convenience strict validation against the default vendored bundle."""
    default_validator().validate(name, instance)
