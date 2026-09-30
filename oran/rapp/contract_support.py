"""rApp compatibility names backed exclusively by the contract kernel."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

from jsonschema import ValidationError

from oran.contract.digests import selected_contract_root
from oran.contract.jcs import canonicalize_bytes, jcs_sha256
from oran.contract.validator import ContractSchemaError, default_validator

class ContractValidationError(ValueError):
    """A wire object is outside the frozen ``oran-aic/1.0.0`` contract."""


def canonicalize(obj: Any) -> bytes:
    return canonicalize_bytes(obj)


def _bundle_dir() -> Path:
    return selected_contract_root() / "shared-contract-bundle"


def load_schema(schema_id: str) -> Dict[str, Any]:
    try:
        return deepcopy(default_validator().schema(schema_id))
    except ContractSchemaError as exc:
        raise ContractValidationError(str(exc)) from exc


def validate(instance: Any, schema_id: str) -> None:
    """Preserve the rApp call shape while delegating to the kernel signature."""
    try:
        default_validator().validate(schema_id, instance)
    except (ContractSchemaError, ValidationError) as exc:
        raise ContractValidationError(str(exc)) from exc
