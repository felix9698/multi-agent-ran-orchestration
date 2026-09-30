"""Non-RT compatibility names backed exclusively by the contract kernel."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from oran.contract.jcs import canonicalize_bytes, jcs_sha256
from oran.contract.problem import problem_details
from oran.contract.validator import ContractSchemaError, ContractValidator

class ContractValidationError(ValueError):
    pass


def canonicalize(value: Any) -> bytes:
    return canonicalize_bytes(value)


def byte_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(instance: Any, schema_id: str, bundle_dir: str | Path | None = None) -> None:
    """Preserve the Non-RT call shape while delegating to the kernel signature."""
    try:
        ContractValidator(Path(bundle_dir) if bundle_dir is not None else None).validate(schema_id, instance)
    except (ContractSchemaError, ValidationError) as exc:
        raise ContractValidationError(str(exc)) from exc


def problem(code: str, status: int, detail: str, instance: str) -> dict[str, Any]:
    return problem_details(code, status, detail, instance)
