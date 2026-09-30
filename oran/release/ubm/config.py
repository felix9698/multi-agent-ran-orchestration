"""Startup configuration for the upper-bilateral-mock runtime.

The startup document is the only place a deployment binds the runtime.  Its
shape is frozen in DESIGN.md 9; ``load_startup_config`` never invents a default
endpoint, never falls back to another port and never approves itself.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from oran.contract.harness import INTEGRATION_CONTROL_SURFACE_APPROVAL_ENV

PROFILE = "bilateral-mock"
APPROVAL_FLAG_VALUE = "approved"


class StartupConfigurationError(RuntimeError):
    """The startup document is incomplete or self-inconsistent (exit 78)."""

    exit_code = 78


@dataclass(frozen=True)
class UbmStartupConfig:
    profile: str
    vector_path: Path
    vector_sha256: str
    integration_values_path: Path
    contract_authority: Path
    state_dir: Path
    secret_map_path: Path
    integration_control_approved: bool
    release_manifest_path: Path
    run_id: str

    def validate(self) -> None:
        if self.profile != PROFILE:
            raise StartupConfigurationError(
                "the bilateral runtime only runs the %s profile" % PROFILE)
        for label, path in (
                ("deploymentVector", self.vector_path),
                ("integrationValues", self.integration_values_path),
                ("secretMap", self.secret_map_path),
                ("releaseManifest", self.release_manifest_path)):
            if not Path(path).is_file():
                raise StartupConfigurationError(
                    "startup %s does not address a regular file" % label)
        if not Path(self.contract_authority).is_dir():
            raise StartupConfigurationError(
                "startup contractAuthority does not address a directory")
        if len(self.vector_sha256) != 64 or any(
                character not in "0123456789abcdef"
                for character in self.vector_sha256):
            raise StartupConfigurationError("vectorSha256 must be a SHA-256 hex digest")
        observed = hashlib.sha256(Path(self.vector_path).read_bytes()).hexdigest()
        if observed != self.vector_sha256:
            raise StartupConfigurationError(
                "deployment vector byte digest does not match the pinned value")
        if not self.run_id:
            raise StartupConfigurationError("runId is required")

    @property
    def release_root(self) -> Path:
        return Path(self.release_manifest_path).resolve().parent

    @property
    def dependency_lock_path(self) -> Path:
        return self.release_root / "deps" / "requirements.lock"

    @property
    def spec_dir(self) -> Path:
        return self.release_root / "spec"


_REQUIRED = (
    "profile", "vectorPath", "vectorSha256", "integrationValuesPath",
    "contractAuthority", "stateDir", "secretMapPath", "releaseManifestPath",
    "runId",
)


def load_startup_config(path: Path,
                        *, argv_overrides: Mapping[str, str] | None = None,
                        ) -> UbmStartupConfig:
    """Read, merge and validate the startup document.

    ``argv_overrides`` carries the two integration-control keys and any
    operator-supplied path override.  The environment key is read here; neither
    key alone enables the control surface.
    """
    target = Path(path)
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StartupConfigurationError("cannot read the startup document") from exc
    if not isinstance(document, dict):
        raise StartupConfigurationError("startup document must be one JSON object")
    merged: dict[str, Any] = dict(document)
    merged.update({key: value for key, value in dict(argv_overrides or {}).items()
                   if value is not None})
    missing = [name for name in _REQUIRED if not merged.get(name)]
    if missing:
        raise StartupConfigurationError(
            "startup document is missing %s" % ", ".join(sorted(missing)))

    base = target.resolve().parent
    approved_flag = str(merged.get("integrationControlSurface", "")) == APPROVAL_FLAG_VALUE
    approved_env = os.environ.get(INTEGRATION_CONTROL_SURFACE_APPROVAL_ENV) == "1"
    if approved_flag != approved_env:
        raise StartupConfigurationError(
            "the integration control surface needs both the CLI flag and "
            "%s=1; neither key alone is sufficient"
            % INTEGRATION_CONTROL_SURFACE_APPROVAL_ENV)

    config = UbmStartupConfig(
        profile=str(merged["profile"]),
        vector_path=_resolve(base, merged["vectorPath"]),
        vector_sha256=str(merged["vectorSha256"]),
        integration_values_path=_resolve(base, merged["integrationValuesPath"]),
        contract_authority=_resolve(base, merged["contractAuthority"]),
        state_dir=_resolve(base, merged["stateDir"]),
        secret_map_path=_resolve(base, merged["secretMapPath"]),
        integration_control_approved=approved_flag and approved_env,
        release_manifest_path=_resolve(base, merged["releaseManifestPath"]),
        run_id=str(merged["runId"]),
    )
    config.validate()
    return config


def _resolve(base: Path, value: Any) -> Path:
    candidate = Path(str(value))
    return candidate if candidate.is_absolute() else (base / candidate).resolve()
