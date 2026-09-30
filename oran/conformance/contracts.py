"""Read-only access to the normative shared-contract bundle."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from oran.contract.digests import (
    ContractIntegrityError,
    contract_authority_version,
    selected_contract_root,
    verify_contract_authority,
)
from oran.contract.jcs import canonicalize_bytes, jcs_sha256
from oran.contract.validator import ContractSchemaError, ContractValidator


class ContractError(ValueError):
    pass


def canonicalize(value: Any) -> bytes:
    return canonicalize_bytes(value)


def pointer(value: Any, fragment: str) -> Any:
    if fragment in ("", "#"):
        return deepcopy(value)
    text = fragment[1:] if fragment.startswith("#") else fragment
    if not text.startswith("/"):
        raise ContractError("invalid JSON pointer: %s" % fragment)
    current = value
    for raw in text[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        try:
            current = current[int(token)] if isinstance(current, list) else current[token]
        except (KeyError, ValueError, IndexError, TypeError) as exc:
            raise ContractError("unresolved JSON pointer: %s" % fragment) from exc
    return deepcopy(current)


class ContractBundle:
    def __init__(self, path: str | Path):
        candidate = Path(path)
        authority = candidate.parent if candidate.name == "shared-contract-bundle" else candidate
        try:
            self.authority_path = verify_contract_authority(authority)
            self.version = contract_authority_version(self.authority_path)
        except ContractIntegrityError as exc:
            raise ContractError(str(exc)) from exc
        self.path = self.authority_path / "shared-contract-bundle"
        if not self.path.is_dir():
            raise ContractError("contract bundle directory does not exist: %s" % self.path)
        self.runner = self.load_json(
            "scenario-runner-contract.%s.json" % self.version)
        self.catalog = self.load_json("scenario-catalog.%s.json" % self.version)
        self.execution_profile_assignment = self._load_execution_profile_assignment()

    @classmethod
    def discover(cls, path: str | Path | None = None) -> "ContractBundle":
        if path is not None:
            return cls(path)
        configured = os.environ.get("ORAN_CONTRACT_AUTHORITY")
        if configured:
            return cls(configured)
        legacy_bundle = os.environ.get("ORAN_CONTRACT_BUNDLE")
        if legacy_bundle:
            return cls(legacy_bundle)
        return cls(selected_contract_root())

    def _load_execution_profile_assignment(self) -> dict[str, Any] | None:
        name = "execution-profile-assignment.%s.json" % self.version
        schema_name = "execution-profile-assignment.%s.schema.json" % self.version
        target, schema_target = self.path / name, self.path / schema_name
        if not target.exists() and not schema_target.exists():
            return None
        if not target.is_file() or not schema_target.is_file():
            raise ContractError(
                "execution profile assignment and schema must appear together")
        assignment = self.load_json(name)
        try:
            ContractValidator(self.path).validate(schema_name, assignment)
        except (ContractSchemaError, ValidationError) as exc:
            raise ContractError(
                "execution profile assignment schema validation failed: %s" % exc) from exc
        return assignment

    def load_json(self, relative: str) -> Any:
        target = self.path / relative
        if not target.is_file() or target.name.startswith("._"):
            raise ContractError("contract artifact is not a regular file: %s" % relative)
        try:
            with target.open(encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("cannot load contract artifact %s" % relative) from exc

    def fixture(self, ref: str) -> Any:
        if not ref.startswith("fixture://"):
            raise ContractError("not a fixture URI: %s" % ref)
        name, _, suffix = ref[len("fixture://"):].partition("#")
        target = self.catalog.get("fixtureRegistry", {}).get(name)
        if not isinstance(target, str):
            raise ContractError("unknown fixture: %s" % name)
        path, _, target_fragment = target.partition("#")
        target_path = self.path / path
        if not target_path.is_file() or target_path.name.startswith("._"):
            raise ContractError("fixture is not a regular file: %s" % path)
        if target_path.suffix.lower() == ".json":
            document = self.load_json(path)
        else:
            if suffix or target_fragment:
                raise ContractError("JSON pointer cannot address a non-JSON fixture: %s" % ref)
            try:
                return target_path.read_bytes()
            except OSError as exc:
                raise ContractError("cannot load fixture %s" % ref) from exc
        combined_fragment = target_fragment
        if suffix:
            combined_fragment = combined_fragment.rstrip("/") + "/" + suffix.lstrip("/") if combined_fragment else suffix
        fragment = ("#" + combined_fragment if combined_fragment else "")
        return pointer(document, fragment)

    def schema(self, schema_id: str) -> dict[str, Any]:
        try:
            return deepcopy(ContractValidator(self.path).schema(schema_id))
        except ContractSchemaError as exc:
            raise ContractError(str(exc)) from exc


def validate_json_schema(instance: Any, schema: dict[str, Any], base_uri: Path) -> None:
    """Validate through the common kernel; ``schema`` identifies a vendored member."""
    schema_id = schema.get("$id") if isinstance(schema, dict) else None
    if not isinstance(schema_id, str):
        raise ContractError("schema has no vendored $id")
    try:
        ContractValidator(base_uri).validate(schema_id, instance)
    except (ContractSchemaError, ValidationError) as exc:
        raise ContractError("schema validation failed: %s" % exc) from exc
