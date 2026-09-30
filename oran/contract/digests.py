"""Fail-closed integrity checks for selectable O-RAN contract authorities."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .jcs import canonicalize_bytes

CONTRACT_VERSION = "1.0.0"
DEFAULT_CONTRACT_AUTHORITY_VERSION = "1.0.1"
SUPPORTED_CONTRACT_AUTHORITY_VERSIONS = frozenset({"1.0.0", "1.0.1"})
MACOS_METADATA_NAMES = frozenset({".DS_Store"})
MACOS_METADATA_PREFIX = "._"
REGULAR_FILE_COUNT_EXCLUDING_MANIFEST = 27
PINNED_JCS_DIGESTS = {
    "AIC_UECellSteering_1.0.0.policy.schema.json": "3c48abaefd1c213ef78e3cad1e3483f512e9675bcb5936dadaada47782c02c90",
    "AIC_UECellSteering_1.0.0.status.schema.json": "9ec81ea297806d2816611b634b3d0163de830574dde9d91227ca6f309aca115a",
    "oran-aic-o1-pa-file.1.0.0.json": "8e20a04899d4486695d38b4b6edbe52bedd78d768d68be370c3cb88012569ef9",
    "o1-netconf-yang-profile.1.0.0.json": "1828395178acc2c0515921d67831e6cbfc49dcf6536c20fb006e44d976d39bbf",
    "aic.policy-evidence.1.0.0.schema.json": "a707fabbbdbbb944a15d1e1fab2bb73042db689ae2b6ee50b3ad97ea5dce6f98",
    "aic.policy-evidence-filter.1.0.0.schema.json": "beb9956f241b1a4ca00f0d528f5984153c07e23f0322d90ba330f6277e16499a",
    "aic.ran-capability.1.0.0.schema.json": "b356f1f6a183cf2438cbbfb89b5c7de08276cef0067588effa2aa344649eacf3",
    "backend-release-manifest.1.0.0.schema.json": "2b02ad831fad2cac57e5a2e9d9ddccb02748809d05f7e772299f3538bddc04cd",
    "e2-capability-inventory.1.0.0.schema.json": "b203a4b131346f149a3f48b887fb89e8748566c2175664b11823c85910af5e56",
    "integration-values.1.0.0.schema.json": "ef2422cc5ccab9f4165d95bbc04b9915523e4eb1d8d771b09709e4f77dbb0e30",
}

# Package stage pins the issued bundle-manifest raw-byte digest here.  The
# pinned manifest transitively pins every manifest-listed bundle member and a
# mismatch never selects or falls back to 1.0.0.
PINNED_AUTHORITY_BUNDLE_MANIFEST_SHA256: dict[str, str] = {
    "1.0.1": "c01dfb46518af0e6f2687e073158ae3e408f199ecd09a1405b1f162c98d7c0b1",
}


class ContractIntegrityError(RuntimeError):
    """A pinned handoff file is absent or differs from its required bytes."""


def contract_root(version: str = CONTRACT_VERSION) -> Path:
    """Return a vendored authority tree; the default is the historical baseline."""
    return Path(__file__).resolve().parents[2] / "contracts" / "oran-aic" / version


def selected_contract_root(path: str | Path | None = None) -> Path:
    """Resolve the explicit runtime authority, defaulting new runs to 1.0.1."""
    selected = (Path(path) if path is not None else
                Path(os.environ["ORAN_CONTRACT_AUTHORITY"])
                if os.environ.get("ORAN_CONTRACT_AUTHORITY") else
                contract_root(DEFAULT_CONTRACT_AUTHORITY_VERSION))
    return selected.parent if selected.name == "shared-contract-bundle" else selected


def contract_authority_version(root: str | Path) -> str:
    """Identify exactly one supported authority without trying another version."""
    authority = selected_contract_root(root)
    if authority.name in SUPPORTED_CONTRACT_AUTHORITY_VERSIONS:
        return authority.name
    bundle = authority / "shared-contract-bundle"
    candidates = {
        version for version in SUPPORTED_CONTRACT_AUTHORITY_VERSIONS
        if (bundle / f"scenario-runner-contract.{version}.json").is_file()
        or (bundle / f"scenario-catalog.{version}.json").is_file()
    }
    if len(candidates) != 1:
        raise ContractIntegrityError(
            f"contract authority must identify exactly one supported version: {authority}")
    return candidates.pop()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractIntegrityError(f"cannot parse contract JSON {path}: {exc}") from exc


def _json_pointer(value: Any, fragment: str, label: str) -> Any:
    if fragment in ("", "#"):
        return value
    text = fragment[1:] if fragment.startswith("#") else fragment
    if not text.startswith("/"):
        raise ContractIntegrityError(f"invalid JSON pointer in {label}: {fragment}")
    current = value
    for raw in text[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        try:
            current = current[int(token)] if isinstance(current, list) else current[token]
        except (KeyError, ValueError, IndexError, TypeError) as exc:
            raise ContractIntegrityError(
                f"unresolved JSON pointer in {label}: {fragment}") from exc
    return current


def _load_bundle_reference(bundle: Path, reference: str, label: str) -> Any:
    path_text, separator, fragment = reference.partition("#")
    if not path_text or Path(path_text).is_absolute() or ".." in Path(path_text).parts:
        raise ContractIntegrityError(f"unsafe contract reference in {label}: {reference}")
    target = bundle / path_text
    if not target.is_file() or _is_macos_metadata(target):
        raise ContractIntegrityError(f"missing contract reference in {label}: {path_text}")
    if separator:
        if target.suffix.lower() != ".json":
            raise ContractIntegrityError(
                f"JSON pointer targets non-JSON contract member in {label}: {reference}")
        return _json_pointer(_load_json(target), "#" + fragment, label)
    return _load_json(target) if target.suffix.lower() == ".json" else target.read_bytes()


def _resolve_fixture_reference(bundle: Path, catalog: dict[str, Any],
                               reference: str) -> Any:
    name, separator, suffix = reference[len("fixture://"):].partition("#")
    registered = catalog.get("fixtureRegistry", {}).get(name)
    if not isinstance(registered, str):
        raise ContractIntegrityError(f"unknown fixture reference: {reference}")
    selected = _load_bundle_reference(bundle, registered, f"fixture {name}")
    if separator:
        return _json_pointer(selected, "#" + suffix, reference)
    return selected


def _walk_values(value: Any):
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_values(child)
    else:
        yield value


def _walk_named_values(value: Any):
    if isinstance(value, dict):
        for name, child in value.items():
            yield name, child
            yield from _walk_named_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_named_values(child)


def _verify_pinned_bundle_manifest(bundle: Path, version: str,
                                   expected_manifest_sha256: str) -> None:
    if (not isinstance(expected_manifest_sha256, str)
            or len(expected_manifest_sha256) != 64
            or any(char not in "0123456789abcdef"
                   for char in expected_manifest_sha256)):
        raise ContractIntegrityError(
            f"invalid pinned bundle manifest SHA-256 for {version}")
    manifest_path = bundle / f"bundle-manifest.{version}.json"
    if not manifest_path.is_file():
        raise ContractIntegrityError(
            f"missing pinned bundle manifest: {manifest_path.name}")
    actual_manifest_sha256 = _sha256(manifest_path)
    if actual_manifest_sha256 != expected_manifest_sha256:
        raise ContractIntegrityError(
            f"pinned bundle manifest byte SHA-256 mismatch: "
            f"expected {expected_manifest_sha256}, got {actual_manifest_sha256}")
    manifest = _load_json(manifest_path)
    entries = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        raise ContractIntegrityError("bundle manifest requires a files array")
    listed: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ContractIntegrityError("bundle manifest file entry must be an object")
        relative = entry.get("path")
        if (not isinstance(relative, str) or not relative
                or Path(relative).is_absolute() or ".." in Path(relative).parts
                or Path(relative).as_posix() != relative
                or relative in listed):
            raise ContractIntegrityError(
                f"unsafe or duplicate bundle manifest path: {relative!r}")
        listed.add(relative)
        target = bundle / relative
        expected_count = entry.get("byteCount")
        expected_digest = entry.get("byteSha256")
        if not target.is_file():
            raise ContractIntegrityError(f"missing manifest-listed bundle file: {relative}")
        if not isinstance(expected_count, int) or expected_count < 0:
            raise ContractIntegrityError(f"invalid byteCount for bundle file: {relative}")
        if (not isinstance(expected_digest, str) or len(expected_digest) != 64
                or any(char not in "0123456789abcdef" for char in expected_digest)):
            raise ContractIntegrityError(f"invalid byteSha256 for bundle file: {relative}")
        if target.stat().st_size != expected_count or _sha256(target) != expected_digest:
            raise ContractIntegrityError(f"manifest-listed bundle file mismatch: {relative}")
    actual = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() and path != manifest_path and not _is_macos_metadata(path)
    }
    if actual != listed:
        raise ContractIntegrityError(
            "bundle manifest file set mismatch: missing=%s unexpected=%s" % (
                sorted(listed - actual), sorted(actual - listed)))


def _verify_corrected_authority(authority: Path, version: str) -> None:
    bundle = authority / "shared-contract-bundle"
    if not bundle.is_dir():
        raise ContractIntegrityError(
            f"contract bundle directory does not exist: {bundle}")
    runner_name = f"scenario-runner-contract.{version}.json"
    catalog_name = f"scenario-catalog.{version}.json"
    runner_path, catalog_path = bundle / runner_name, bundle / catalog_name
    if not runner_path.is_file():
        raise ContractIntegrityError(f"missing required authority artifact: {runner_name}")
    if not catalog_path.is_file():
        raise ContractIntegrityError(f"missing required authority artifact: {catalog_name}")

    documents: dict[Path, Any] = {}
    for path in sorted(bundle.rglob("*.json")):
        if not _is_macos_metadata(path):
            documents[path] = _load_json(path)
    runner, catalog = documents[runner_path], documents[catalog_path]
    if not isinstance(runner, dict) or runner.get("contractVersion") != (
            f"oran-aic-scenario-runner/{version}"):
        raise ContractIntegrityError(f"authority runner version mismatch: {runner_name}")
    if not isinstance(catalog, dict) or catalog.get("catalogVersion") != (
            f"oran-aic-scenario-catalog/{version}"):
        raise ContractIntegrityError(f"authority catalog version mismatch: {catalog_name}")
    if runner.get("catalogRef") != catalog_name:
        raise ContractIntegrityError("runner catalogRef does not select its authority catalog")
    if catalog.get("runnerContractRef") != runner_name:
        raise ContractIntegrityError("catalog runnerContractRef does not select its authority runner")

    for path, document in documents.items():
        if not path.name.endswith("schema.json"):
            continue
        if not isinstance(document, dict) or "$schema" not in document:
            raise ContractIntegrityError(f"contract schema is not a schema object: {path.name}")
        try:
            Draft202012Validator.check_schema(document)
        except SchemaError as exc:
            raise ContractIntegrityError(
                f"invalid contract schema {path.name}: {exc.message}") from exc

    for key in ("catalogRef", "deploymentVectorSchemaRef",
                "e2CapabilityInventorySchemaRef", "o1NetconfYangProfileRef"):
        reference = runner.get(key)
        if not isinstance(reference, str):
            raise ContractIntegrityError(f"runner {key} is absent")
        _load_bundle_reference(bundle, reference, f"runner.{key}")
    for key in ("runnerContractRef", "deploymentVectorSchemaRef"):
        reference = catalog.get(key)
        if not isinstance(reference, str):
            raise ContractIntegrityError(f"catalog {key} is absent")
        _load_bundle_reference(bundle, reference, f"catalog.{key}")
    for document_name, document in ((runner_name, runner), (catalog_name, catalog)):
        for key, reference in _walk_named_values(document):
            if not key.endswith("Ref") or not isinstance(reference, str):
                continue
            if reference.startswith("fixture://"):
                _resolve_fixture_reference(bundle, catalog, reference)
            elif reference.startswith("#/"):
                # Endpoint references in both runner operation contracts and
                # catalog steps address the catalog endpointTemplates object.
                _json_pointer(catalog, reference, f"{document_name}.{key}")
            elif ".json" in reference.partition("#")[0]:
                _load_bundle_reference(bundle, reference,
                                       f"{document_name}.{key}")
    registry = catalog.get("fixtureRegistry")
    if not isinstance(registry, dict) or not registry:
        raise ContractIntegrityError("catalog fixtureRegistry is absent")
    for name, reference in registry.items():
        if not isinstance(reference, str):
            raise ContractIntegrityError(f"fixture registry value is not a string: {name}")
        _load_bundle_reference(bundle, reference, f"fixtureRegistry.{name}")
    for value in _walk_values(catalog.get("scenarios", [])):
        if isinstance(value, str) and value.startswith("fixture://"):
            _resolve_fixture_reference(bundle, catalog, value)

    assignment = bundle / f"execution-profile-assignment.{version}.json"
    assignment_schema = bundle / f"execution-profile-assignment.{version}.schema.json"
    if assignment.exists() != assignment_schema.exists():
        raise ContractIntegrityError(
            "execution profile assignment and schema must appear together")
    if assignment.is_file():
        try:
            Draft202012Validator(documents[assignment_schema]).validate(
                documents[assignment])
        except ValidationError as exc:
            raise ContractIntegrityError(
                f"execution profile assignment schema violation: {exc.message}") from exc

    if version not in PINNED_AUTHORITY_BUNDLE_MANIFEST_SHA256:
        raise ContractIntegrityError(f"authority digest configuration is absent: {version}")
    manifest_pin = PINNED_AUTHORITY_BUNDLE_MANIFEST_SHA256[version]
    _verify_pinned_bundle_manifest(bundle, version, manifest_pin)


def _assert_file(path: Path, byte_count: int, digest: str, label: str) -> None:
    if not path.is_file():
        raise ContractIntegrityError(f"missing required {label}: {path}")
    if path.stat().st_size != byte_count:
        raise ContractIntegrityError(
            f"{label} byte count mismatch: expected {byte_count}, got {path.stat().st_size}"
        )
    actual = _sha256(path)
    if actual != digest:
        raise ContractIntegrityError(f"{label} SHA-256 mismatch: expected {digest}, got {actual}")


def _is_macos_metadata(path: Path) -> bool:
    # AppleDouble and Finder metadata are not regular bundle members; §0's
    # 27-file count and this verifier intentionally exclude them everywhere.
    return path.name in MACOS_METADATA_NAMES or path.name.startswith(MACOS_METADATA_PREFIX)


def _verify_source_identity(vendored: Path, source_root: Path) -> None:
    source_bundle = source_root / "shared-contract-bundle"
    if not source_bundle.is_dir():
        return
    candidates = [source_root / "handoff-manifest.1.0.0.json", source_root / "02-rapp-xapp-backend-mandatory-contract.md"]
    for source_file in candidates:
        if source_file.is_file():
            copy = vendored / source_file.name
            if not copy.is_file() or source_file.read_bytes() != copy.read_bytes():
                raise ContractIntegrityError(f"vendored copy differs from source handoff: {source_file.name}")
    for source_file in source_bundle.rglob("*"):
        if source_file.is_file() and not _is_macos_metadata(source_file):
            relative = source_file.relative_to(source_bundle)
            copy = vendored / "shared-contract-bundle" / relative
            if not copy.is_file() or source_file.read_bytes() != copy.read_bytes():
                raise ContractIntegrityError(f"vendored bundle differs from source: {relative}")


def _discover_oranc_source() -> Path | None:
    """Find the repository handoff when this is an Orca child worktree."""
    workspace = contract_root().parents[3]
    for ancestor in workspace.parents:
        if ancestor.name != "workspaces":
            continue
        try:
            repository_slug = workspace.relative_to(ancestor).parts[0]
        except (ValueError, IndexError):
            continue
        candidate = ancestor.parent.parent / repository_slug / "OranC" / "ORAN-refactor-handoff"
        if candidate.is_dir():
            return candidate
    return None


def verify_contract_integrity(root: Path | None = None, source_root: Path | None = None) -> None:
    """Verify every pinned handoff byte and JCS digest, or raise before startup.

    ``source_root`` is optional so a release remains self-contained.  If an
    ``OranC`` source handoff is present (or supplied by
    ``ORAN_AIC_HANDOFF_SOURCE``), it is also compared byte-for-byte.
    """
    vendored = Path(root) if root else contract_root()
    root_manifest_path = vendored / "handoff-manifest.1.0.0.json"
    root_manifest = _load_json(root_manifest_path)
    bundle_info = root_manifest.get("bundle", {})
    for document in root_manifest.get("documents", []):
        path = vendored / document["path"]
        _assert_file(path, document["byteCount"], document["byteSha256"], f"document {document['path']}")

    bundle_manifest_path = vendored / bundle_info["manifestPath"]
    _assert_file(bundle_manifest_path, bundle_info["manifestByteCount"], bundle_info["manifestByteSha256"], "bundle manifest")
    bundle_manifest = _load_json(bundle_manifest_path)
    actual_manifest_jcs = hashlib.sha256(canonicalize_bytes(bundle_manifest)).hexdigest()
    if actual_manifest_jcs != bundle_info["manifestJcsSha256"]:
        raise ContractIntegrityError("bundle manifest JCS SHA-256 mismatch")

    bundle_root = vendored / bundle_info["directory"]
    listed = {entry["path"] for entry in bundle_manifest.get("files", [])}
    for entry in bundle_manifest.get("files", []):
        _assert_file(bundle_root / entry["path"], entry["byteCount"], entry["byteSha256"], f"bundle file {entry['path']}")
    actual_regular = {
        str(path.relative_to(bundle_root)).replace(os.sep, "/")
        for path in bundle_root.rglob("*") if path.is_file() and not _is_macos_metadata(path)
    }
    if actual_regular - {"bundle-manifest.1.0.0.json"} != listed:
        raise ContractIntegrityError("bundle regular files do not exactly match bundle manifest")
    if len(listed) != REGULAR_FILE_COUNT_EXCLUDING_MANIFEST or bundle_info.get("regularFileCountExcludingManifest") != REGULAR_FILE_COUNT_EXCLUDING_MANIFEST:
        raise ContractIntegrityError("bundle regular file count is not the pinned 27")

    for relative, expected in PINNED_JCS_DIGESTS.items():
        actual = hashlib.sha256(canonicalize_bytes(_load_json(bundle_root / relative))).hexdigest()
        if actual != expected:
            raise ContractIntegrityError(f"pinned JCS SHA-256 mismatch for {relative}: expected {expected}, got {actual}")

    if source_root is None:
        configured = os.environ.get("ORAN_AIC_HANDOFF_SOURCE")
        repo_source = Path(__file__).resolve().parents[2] / "OranC" / "ORAN-refactor-handoff"
        source_root = Path(configured) if configured else (repo_source if repo_source.is_dir() else _discover_oranc_source())
    if source_root is not None:
        _verify_source_identity(vendored, source_root)


def verify_contract_authority(root: str | Path | None = None) -> Path:
    """Verify only the selected authority and return its normalized tree path.

    The historical 1.0.0 authority retains its complete manifest and byte
    verification.  Corrected 1.0.1 verifies its issued, byte-pinned bundle
    manifest before any contract member is consumed.
    No branch in this function tries a different authority after failure.
    """
    authority = selected_contract_root(root)
    version = contract_authority_version(authority)
    if version == "1.0.0":
        verify_contract_integrity(authority)
    else:
        _verify_corrected_authority(authority, version)
    return authority
