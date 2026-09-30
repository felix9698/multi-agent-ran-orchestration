"""Start-time, fail-closed preflight for the bilateral profile (D-6 V-3).

Everything here runs *before* a socket is bound.  Any failure exits 78
(``EX_CONFIG``); there is no "resolve at first use", no port fallback and no
schema shadow: the deployment vector is validated against the frozen
``deployment-test-vector.1.0.0.schema.json`` bytes exactly as they are.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from oran.conformance.contracts import ContractBundle
from oran.contract.jcs import canonicalize_bytes
from oran.contract.validator import ContractValidator

EXIT_CONFIG = 78

PLACEHOLDER = re.compile(
    r"\$\{|<[A-Z_]+>|PLACEHOLDER|TODO|CHANGEME|example\.(com|test|invalid)")

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: The seven vector fields the frozen schema constrains to ``^https://``.
HTTPS_VECTOR_FIELDS = (
    ("r1", "apiRoot"),
    ("r1", "callbackApi", "rootUri"),
    ("r1", "dme", "policyEvidencePushBaseUri"),
    ("a1", "apiRoot"),
    ("a1", "statusCallbackRoot"),
    ("o1", "fileDataReporting", "mnsRoot"),
    ("o1", "fileDataReporting", "consumerReference"),
)


class PreflightError(RuntimeError):
    """A preflight assertion failed; the process must exit 78."""

    exit_code = EXIT_CONFIG


def pointer(document: Mapping[str, Any], path: Iterable[str]) -> Any:
    current: Any = document
    for token in path:
        if not isinstance(current, Mapping) or token not in current:
            raise PreflightError("deployment vector is missing /%s" % "/".join(path))
        current = current[token]
    return current


def authority_of(uri: str, label: str) -> tuple[str, int]:
    parsed = urlsplit(uri)
    if parsed.scheme != "https":
        raise PreflightError("%s must use https (the bilateral profile "
                             "forbids the local-mock scheme relaxation)" % label)
    if parsed.hostname not in LOOPBACK_HOSTS:
        raise PreflightError("%s must address a loopback host" % label)
    try:
        port = parsed.port
    except ValueError as exc:
        raise PreflightError("%s has an invalid port" % label) from exc
    if port is None:
        raise PreflightError("%s must declare its listen port" % label)
    return parsed.hostname, int(port)


def base_path_of(uri: str) -> str:
    return urlsplit(uri).path.rstrip("/")


def load_vector(path: Path) -> tuple[dict[str, Any], str, bytes]:
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError("deployment vector is not valid JSON") from exc
    if not isinstance(document, dict):
        raise PreflightError("deployment vector must be one JSON object")
    return document, digest, raw


def validate_vector_against_frozen_schema(vector: Mapping[str, Any],
                                          bundle: ContractBundle) -> str:
    """Validate the vector with NO shadow rewrite (gate G-TLS-1).

    The schema-rewrite shadow in ``oran.profiles.local_mock`` is deliberately
    not imported here; the bilateral import graph must not contain it.  Its
    function name is never written out under this package, because G-TLS-1 is
    enforced as a byte grep over the packaged members.
    """
    schema_path = bundle.path / "deployment-test-vector.1.0.0.schema.json"
    schema_bytes = schema_path.read_bytes()
    validator = ContractValidator(bundle.path)
    errors = validator.errors(schema_path.name, dict(vector))
    if errors:
        first = errors[0]
        raise PreflightError("deployment vector fails the frozen schema at /%s: %s" % (
            "/".join(str(part) for part in first.absolute_path), first.message))
    return hashlib.sha256(schema_bytes).hexdigest()


def assert_placeholder_free(vector: Mapping[str, Any]) -> None:
    for path, value in _walk(vector):
        if isinstance(value, str) and PLACEHOLDER.search(value):
            raise PreflightError(
                "deployment vector still carries a placeholder at /%s" % "/".join(path))


def assert_schema_external_invariants(vector: Mapping[str, Any],
                                      bundle: ContractBundle) -> None:
    """The six invariants the frozen schema delegates to the runner."""
    window = pointer(vector, ("o1", "live", "expectedMeasurementWindow"))
    if str(window["start"]) >= str(window["end"]):
        raise PreflightError("expectedMeasurementWindow start is not before end")

    topology = pointer(vector, ("topology",))
    serving, target = topology["servingCell"], topology["targetCell"]
    if _jcs(serving["cellId"]) == _jcs(target["cellId"]):
        raise PreflightError("servingCell and targetCell must differ")

    capability = bundle.fixture("fixture://capabilityManifest")
    neighbours = {
        (_jcs(item["sourceCell"]), _jcs(item["targetCell"]))
        for item in capability.get("topology", {}).get("neighbours", [])
    }
    if (_jcs(serving["cellId"]), _jcs(target["cellId"])) not in neighbours:
        raise PreflightError(
            "servingCell -> targetCell is not a declared directed neighbour")

    mappings = topology["cellMappings"]
    cells = [_jcs(item["cellId"]) for item in mappings]
    names = [str(item["managedObjectDn"]) for item in mappings]
    if len(set(cells)) != len(cells) or len(set(names)) != len(names):
        raise PreflightError("cellId <-> managedObjectDn mapping is not bijective")

    for path, info in _file_infos(vector):
        if str(info["fileReadyTime"]) >= str(info["fileExpirationTime"]):
            raise PreflightError(
                "FileInfo at /%s has fileReadyTime >= fileExpirationTime" % path)

    schemas = pointer(vector, ("schemas",))
    record_digest = hashlib.sha256(
        canonicalize_bytes(schemas["policyEvidenceRecordSchema"])).hexdigest()
    if record_digest != schemas["policyEvidenceRecordSchemaJcsSha256"]:
        raise PreflightError("policyEvidenceRecordSchemaJcsSha256 mismatch")
    canonical = canonicalize_bytes(
        schemas["policyEvidenceRecordSchema"]).decode("utf-8")
    if canonical != schemas["policyEvidenceRecordSchemaCanonicalJson"]:
        raise PreflightError("policyEvidenceRecordSchemaCanonicalJson is not RFC 8785")
    expected_filter = hashlib.sha256(canonicalize_bytes(json.loads(
        (bundle.path / "aic.policy-evidence-filter.1.0.0.schema.json").read_text(
            encoding="utf-8")))).hexdigest()
    if expected_filter != schemas["policyEvidenceFilterSchemaJcsSha256"]:
        raise PreflightError("policyEvidenceFilterSchemaJcsSha256 mismatch")


def assert_https_endpoints(vector: Mapping[str, Any]) -> None:
    for path in HTTPS_VECTOR_FIELDS:
        authority_of(pointer(vector, path), "/" + "/".join(path))


#: ``deps/requirements.lock`` pins the interpreter as well as the four
#: distributions.  ``importlib.metadata`` cannot resolve an interpreter, so this
#: one entry is compared against ``platform.python_version()`` instead of being
#: skipped -- a skipped entry would silently weaken G-DEP-1.
INTERPRETER_LOCK_NAME = "python"


def check_dependency_lock(lock_path: Path) -> dict[str, str]:
    """G-DEP-1: installed distributions must equal the locked versions."""
    import importlib.metadata as metadata
    import platform

    target = Path(lock_path)
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PreflightError(
            "dependency lock is required before any socket is bound") from exc
    observed: dict[str, str] = {}
    for line in lines:
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        name, separator, expected = entry.partition("==")
        if not separator:
            raise PreflightError("dependency lock entries must pin name==version")
        name, expected = name.strip(), expected.strip()
        if name == INTERPRETER_LOCK_NAME:
            installed = platform.python_version()
            if installed != expected:
                raise PreflightError(
                    "locked interpreter %s expects %s but %s is running"
                    % (name, expected, installed))
            observed[name] = installed
            continue
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError as exc:
            raise PreflightError(
                "locked dependency %s is not installed" % name) from exc
        if installed != expected:
            raise PreflightError(
                "locked dependency %s expects %s but %s is installed"
                % (name, expected, installed))
        observed[name] = installed
    if not observed:
        raise PreflightError("dependency lock declares no entries")
    return observed


def _walk(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, path + (str(index),))
    else:
        yield path, value


def _file_infos(vector: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    for path, node in _walk_objects(vector):
        if isinstance(node, Mapping) and "fileReadyTime" in node \
                and "fileExpirationTime" in node:
            yield "/".join(path), node


def _walk_objects(value: Any, path: tuple[str, ...] = ()
                  ) -> Iterable[tuple[tuple[str, ...], Any]]:
    if isinstance(value, Mapping):
        yield path, value
        for key, child in value.items():
            yield from _walk_objects(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_objects(child, path + (str(index),))


def _jcs(value: Any) -> str:
    return canonicalize_bytes(value).decode("utf-8")
