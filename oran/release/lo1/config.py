"""Startup configuration for the upper-live-o1-harness runtime (W2).

The startup document is the only place a deployment binds the runtime.  Nothing
here invents a default endpoint, falls back to another port, or approves itself:
every concrete value arrives from operator-supplied files whose digests the
identity/authority gate re-measures.

The shape is frozen in ``work-split.1.0.0.json#/frozenInterfaces``; the field
list of :class:`Lo1StartupConfig` is part of that freeze.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

PROFILE_LIVE = "live-O1"
PROFILE_SELF_TEST = "self-test"

PROFILES = (PROFILE_LIVE, PROFILE_SELF_TEST)

EXIT_CONFIG = 78


class StartupConfigurationError(RuntimeError):
    """The startup document is incomplete or self-inconsistent (exit 78)."""

    exit_code = EXIT_CONFIG


@dataclass(frozen=True)
class Lo1StartupConfig:
    profile: str
    vector_path: Path
    vector_sha256: str
    provider_acceptance_path: Path
    authority_record_path: Path
    security_authority_path: Path
    security_authority_sha256: str
    contract_authority: Path
    state_dir: Path
    secret_map_path: Path
    capture_root: Path
    release_manifest_path: Path
    run_id: str

    def validate(self) -> None:
        if self.profile not in PROFILES:
            raise StartupConfigurationError(
                "profile must be one of %s" % ", ".join(PROFILES))
        for label, path in (
                ("deploymentVector", self.vector_path),
                ("providerAcceptance", self.provider_acceptance_path),
                ("authorityRecord", self.authority_record_path),
                ("securityAuthority", self.security_authority_path),
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
            raise StartupConfigurationError(
                "vectorSha256 must be a lowercase SHA-256 hex digest")
        if len(self.security_authority_sha256) != 64 or any(
                character not in "0123456789abcdef"
                for character in self.security_authority_sha256):
            raise StartupConfigurationError(
                "securityAuthoritySha256 must be a lowercase SHA-256 hex digest")
        observed = hashlib.sha256(Path(self.vector_path).read_bytes()).hexdigest()
        if observed != self.vector_sha256:
            raise StartupConfigurationError(
                "deployment vector byte digest does not match the pinned value")
        security_observed = hashlib.sha256(
            Path(self.security_authority_path).read_bytes()).hexdigest()
        if security_observed != self.security_authority_sha256:
            raise StartupConfigurationError(
                "security authority byte digest does not match the pinned value")
        if not str(self.run_id):
            raise StartupConfigurationError("runId is required")

    # -- derived locations -------------------------------------------------
    @property
    def release_root(self) -> Path:
        return Path(self.release_manifest_path).resolve().parent

    @property
    def spec_dir(self) -> Path:
        return self.release_root / "spec"

    @property
    def dependency_lock_path(self) -> Path:
        return self.release_root / "deps" / "requirements.lock"

    @property
    def is_self_test(self) -> bool:
        return self.profile == PROFILE_SELF_TEST


_REQUIRED = (
    "profile", "vectorPath", "vectorSha256", "providerAcceptancePath",
    "authorityRecordPath", "securityAuthorityPath", "securityAuthoritySha256",
    "contractAuthority", "stateDir", "secretMapPath",
    "captureRoot", "releaseManifestPath", "runId",
)


def load_startup_config(path: Path,
                        *, argv_overrides: Mapping[str, str] | None = None,
                        ) -> Lo1StartupConfig:
    """Read, merge and validate the startup document.

    ``argv_overrides`` carries operator-supplied path overrides only.  There is
    no override that can relax a value the gate later checks: the vector digest
    is re-measured here and the gate re-measures it again from the bytes.
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
    config = Lo1StartupConfig(
        profile=str(merged["profile"]),
        vector_path=_resolve(base, merged["vectorPath"]),
        vector_sha256=str(merged["vectorSha256"]),
        provider_acceptance_path=_resolve(base, merged["providerAcceptancePath"]),
        authority_record_path=_resolve(base, merged["authorityRecordPath"]),
        security_authority_path=_resolve(base, merged["securityAuthorityPath"]),
        security_authority_sha256=str(merged["securityAuthoritySha256"]),
        contract_authority=_resolve(base, merged["contractAuthority"]),
        state_dir=_resolve(base, merged["stateDir"]),
        secret_map_path=_resolve(base, merged["secretMapPath"]),
        capture_root=_resolve(base, merged["captureRoot"]),
        release_manifest_path=_resolve(base, merged["releaseManifestPath"]),
        run_id=str(merged["runId"]),
    )
    config.validate()
    return config


def _resolve(base: Path, value: Any) -> Path:
    candidate = Path(str(value))
    return candidate if candidate.is_absolute() else (base / candidate).resolve()
