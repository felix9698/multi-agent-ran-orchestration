"""Start-time, fail-closed preflight for the live-O1 profile.

Everything here runs *before* a socket is bound.  Any failure exits 78
(``EX_CONFIG``); there is no "resolve at first use", no port fallback and no
schema shadow: the deployment vector is validated against the frozen
``deployment-test-vector.1.0.0.schema.json`` bytes exactly as they are.

Two values this release must never type are read out of the frozen schema
instead: ``o1.live.expectedPolicyCellCount`` (a ``const``) and the pinned
cardinality of ``o1.live.recoveryFiles.ambiguousCandidates``
(``minItems == maxItems``).  One value is read out of the frozen *catalog*: the
lower bound on ``timeouts.liveCaptureMs``, which is the SC-084 ``o1-notify``
step's ``collectionDurationMs`` plus its ``afterActionDelayMs``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from oran.conformance.contracts import ContractBundle
from oran.contract.validator import ContractValidator

EXIT_CONFIG = 78

#: ``${LO1:OWNER:/pointer}`` is this release's own template grammar; the rest are
#: the generic markers an operator copy might leave behind.
PLACEHOLDER = re.compile(
    r"\$\{|<[A-Z_]+>|PLACEHOLDER|TODO|CHANGEME|example\.(com|test|invalid)")

VECTOR_SCHEMA_NAME = "deployment-test-vector.1.0.0.schema.json"

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

NOTIFY_STEP_ID = "o1-notify"


class PreflightError(RuntimeError):
    """A preflight assertion failed; the process must exit 78."""

    exit_code = EXIT_CONFIG


def pointer(document: Mapping[str, Any], path: Iterable[str]) -> Any:
    steps = tuple(path)
    current: Any = document
    for token in steps:
        if not isinstance(current, Mapping) or token not in current:
            raise PreflightError("deployment vector is missing /%s" % "/".join(steps))
        current = current[token]
    return current


def authority_of(uri: str, label: str) -> tuple[str, int]:
    """Split an ``https://`` origin into an exact host and port.

    Unlike the bilateral profile this does NOT require loopback: the lower live
    Provider and the lower implementation under test are remote by definition.
    Exactness is checked by the gate (G-ID-06), not relaxed here.
    """
    parsed = urlsplit(str(uri))
    if parsed.scheme != "https":
        raise PreflightError("%s must use https" % label)
    if not parsed.hostname:
        raise PreflightError("%s has no host" % label)
    try:
        port = parsed.port
    except ValueError as exc:
        raise PreflightError("%s has an invalid port" % label) from exc
    if port is None:
        raise PreflightError("%s must declare its port explicitly" % label)
    return parsed.hostname, int(port)


def base_path_of(uri: str) -> str:
    return urlsplit(str(uri)).path.rstrip("/")


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

    The schema-rewrite shadow in ``oran.profiles`` is deliberately absent from
    this import graph; G-TLS-1 is enforced as a byte grep over the packaged
    members, so its function name is never written out under this package.
    """
    schema_path = Path(bundle.path) / VECTOR_SCHEMA_NAME
    schema_bytes = schema_path.read_bytes()
    validator = ContractValidator(Path(bundle.path))
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


def assert_https_endpoints(vector: Mapping[str, Any]) -> None:
    for path in HTTPS_VECTOR_FIELDS:
        authority_of(pointer(vector, path), "/" + "/".join(path))


# -- values READ from the frozen schema, never typed -----------------------

def expected_policy_cell_count(bundle: ContractBundle) -> int:
    schema = _vector_schema(bundle)
    try:
        return int(schema["properties"]["o1"]["properties"]["live"][
            "properties"]["expectedPolicyCellCount"]["const"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PreflightError(
            "the frozen vector schema does not pin expectedPolicyCellCount") from exc


def ambiguous_candidate_cardinality(bundle: ContractBundle) -> int:
    schema = _vector_schema(bundle)
    try:
        node = schema["properties"]["o1"]["properties"]["live"]["properties"][
            "recoveryFiles"]["properties"]["ambiguousCandidates"]
        minimum, maximum = int(node["minItems"]), int(node["maxItems"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PreflightError(
            "the frozen vector schema does not pin the ambiguousCandidates "
            "cardinality") from exc
    if minimum != maximum:
        raise PreflightError(
            "ambiguousCandidates cardinality is not pinned by the frozen schema")
    return minimum


def assert_recovery_files(vector: Mapping[str, Any], bundle: ContractBundle) -> None:
    """LO1-ST-V02: the recovery-file set must be exactly what the schema pins.

    ``uniqueCandidate`` must be genuinely unique -- it may not reappear among the
    ambiguous candidates -- and the ambiguous set must hold exactly the pinned
    number of *distinct* entries.  The count is read, never typed.
    """
    recovery = pointer(vector, ("o1", "live", "recoveryFiles"))
    unique = recovery.get("uniqueCandidate")
    ambiguous = recovery.get("ambiguousCandidates")
    if not isinstance(unique, Mapping) or not isinstance(ambiguous, list):
        raise PreflightError("o1.live.recoveryFiles is malformed")
    expected = ambiguous_candidate_cardinality(bundle)
    if len(ambiguous) != expected:
        raise PreflightError(
            "o1.live.recoveryFiles.ambiguousCandidates must hold exactly the "
            "%d entries the frozen schema pins" % expected)
    locations = [str(item.get("fileLocation")) for item in ambiguous]
    if len(set(locations)) != len(locations):
        raise PreflightError(
            "ambiguousCandidates are not distinct, so they cannot be ambiguous "
            "with respect to one another")
    if str(unique.get("fileLocation")) in set(locations):
        raise PreflightError(
            "recoveryFiles.uniqueCandidate is not unique: it also appears among "
            "the ambiguous candidates")


def live_capture_lower_bound_ms(catalog: Mapping[str, Any],
                                catalog_pointer: str = "/scenarios/83") -> int:
    """The SC-084 ``o1-notify`` collection window, read from the catalog.

    The release stores neither ``collectionDurationMs`` nor
    ``afterActionDelayMs``; G-ID-08 computes the bound from these bytes and
    refuses a shorter ``timeouts.liveCaptureMs``.
    """
    index = int(str(catalog_pointer).rsplit("/", 1)[-1])
    try:
        scenario = catalog["scenarios"][index]
        steps = scenario["materialization"]["steps"]
    except (KeyError, IndexError, TypeError) as exc:
        raise PreflightError("catalog pointer %s does not resolve to a scenario"
                             % catalog_pointer) from exc
    for step in steps:
        if str(step.get("id")) == NOTIFY_STEP_ID:
            return (int(step.get("collectionDurationMs", 0))
                    + int(step.get("afterActionDelayMs", 0)))
    raise PreflightError(
        "the scenario at %s declares no %s step" % (catalog_pointer, NOTIFY_STEP_ID))


def _vector_schema(bundle: ContractBundle) -> Mapping[str, Any]:
    path = Path(bundle.path) / VECTOR_SCHEMA_NAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError("the frozen vector schema is unreadable") from exc


# -- dependency lock -------------------------------------------------------

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


def _walk(value: Any, path: tuple[str, ...] = ()
          ) -> Iterable[tuple[tuple[str, ...], Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, path + (str(index),))
    else:
        yield path, value
