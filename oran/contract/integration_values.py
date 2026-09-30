"""Strict loader for the non-secret final-merge integration values document."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import ValidationError

from .digests import PINNED_JCS_DIGESTS
from .validator import ContractValidator

SCHEMA_NAME = "integration-values.1.0.0.schema.json"


class IntegrationValuesError(ValueError):
    """Integration values are incomplete or do not satisfy the pinned schema."""


def load_integration_values(path: str | Path, *, validator: ContractValidator | None = None) -> dict[str, Any]:
    """Load exactly the schema's 80 endpoint/identity/reference values.

    Secrets are references only.  The schema asserts SecretReference syntax and
    SFTP authority ports in the inclusive 1..65535 range; this loader also
    enforces the expected bundle JCS digest instead of merely accepting any
    well-formed SHA-256 string.
    """
    path = Path(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationValuesError(f"cannot load integration values {path}: {exc}") from exc
    active_validator = validator or ContractValidator()
    try:
        active_validator.validate(SCHEMA_NAME, document)
    except ValidationError as exc:
        raise IntegrationValuesError(f"integration values schema violation at {list(exc.absolute_path)}: {exc.message}") from exc
    required = set(active_validator.schema(SCHEMA_NAME)["properties"]["values"]["required"])
    values = document["values"]
    if set(values) != required or len(required) != 80:
        raise IntegrationValuesError("integration values must contain exactly the contract's 80 keys")
    expected_bundle = "6f9908ca9cee29ca5fa7b629f4daa0f502b5b8ce244519a76b9c88c6c1710ce3"
    if document["bundleManifestJcsSha256"] != expected_bundle:
        raise IntegrationValuesError("integration values reference an unpinned bundle manifest digest")
    return document
