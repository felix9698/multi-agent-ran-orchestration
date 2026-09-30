"""External black-box self-test driver.

The upper is always a **separate operating-system process**, reached only over
its published HTTPS endpoints.  Nothing in this module imports the upper runtime,
and no result is manufactured from in-process fixtures.

The report is deliberately narrow: it carries the label ``UPPER_ARTIFACT_SELF_TEST``,
records which upper was actually driven, and states in prose that it is not
bilateral acceptance.  The lower frozen runner owns the disposition.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import NOT_BILATERAL_ACCEPTANCE, SELF_TEST_STATE_LABEL
from . import mutations as mut
from .contract_model import FrozenContract, sha256_bytes
from .endpoints import UbmEndpoints
from .lower_double import ContractFaithfulLowerDouble
from .proxy import MutationProxy, flip_one_policy_byte, r1_create_201_to_200
from .tls_bootstrap import (
    LoopbackTlsMaterial, bootstrap_loopback_tls, openssl_available)
from .wire import SocketAuthorityGuard, authority_of, json_request

BILATERAL_SCENARIOS = ("SC-062", "SC-083", "SC-091", "SC-092")
INGRESS_PROXY_BACKEND_PORT = 19545
EGRESS_PROXY_PORT = 19546


class SelfTestError(RuntimeError):
    """The self-test could not be run at all (as distinct from a failing run)."""


@dataclass
class SelfTestConfig:
    repo_root: Path
    work_dir: Path
    upper_command: tuple[str, ...] | None = None
    upper_label: str | None = None
    scenarios: tuple[str, ...] = BILATERAL_SCENARIOS
    readiness_timeout_s: float = 20.0
    #: Root of an **extracted release archive**.  When set, the upper under test
    #: is that release's own ``bin/ubm`` launcher, its TLS material comes from
    #: the release's own ``bootstrap`` command, and every spec/contract byte is
    #: read out of the release rather than out of this repository.  This is what
    #: turns the suite from "the stand-in answered correctly" into "the shipped
    #: artifact answered correctly".
    release_root: Path | None = None
    #: How long the double waits to observe a CAPTURE_AND_RESPOND exchange.
    #: The contract default is deployment.timeouts.defaultStepMs (30 s); the
    #: self-test shortens it so a mutant that will never be observed fails
    #: promptly.  Recorded in the report so the shortening is visible.
    observation_deadline_ms: int = 3000

    @property
    def artifact_root(self) -> Path:
        return Path(self.release_root) if self.release_root else self.repo_root

    @property
    def bundle_dir(self) -> Path:
        return self.artifact_root / "contracts" / "oran-aic" / "1.0.1" / "shared-contract-bundle"

    @property
    def spec_dir(self) -> Path:
        """``docs/upper-bilateral-mock`` in the repo, ``spec/`` in a release."""
        repo_spec = self.artifact_root / "docs" / "upper-bilateral-mock"
        return repo_spec if repo_spec.is_dir() else self.artifact_root / "spec"

    @property
    def launcher_path(self) -> Path:
        return self.artifact_root / "bin" / "ubm"

    @property
    def release_manifest_path(self) -> Path:
        return self.artifact_root / "RELEASE-MANIFEST.json"

    @property
    def vector_path(self) -> Path:
        return self.spec_dir / "deployment-vector.1.0.0.template.json"

    @property
    def capture_schema_path(self) -> Path:
        return self.spec_dir / "capture-schema.1.0.0.json"

    @property
    def gates_path(self) -> Path:
        return self.spec_dir / "release-gates.1.0.0.json"

    @property
    def ics_path(self) -> Path:
        return self.spec_dir / "integration-control-surface.1.0.0.json"

    @property
    def binding_path(self) -> Path:
        return self.spec_dir / "deployment-binding.1.0.0.json"


@dataclass
class UpperProcess:
    """Owns the separate upper process for exactly one run."""

    config: SelfTestConfig
    startup_path: Path
    command: tuple[str, ...]
    label: str
    endpoints: UbmEndpoints
    client_context: Any
    process: subprocess.Popen[bytes] | None = None
    stderr_path: Path | None = None
    _stderr_handle: Any = None

    def start(self) -> None:
        environment = dict(os.environ)
        if self.config.release_root is None:
            # The in-package stand-in is imported out of this repository.
            environment["PYTHONPATH"] = str(self.config.repo_root) + os.pathsep + (
                environment.get("PYTHONPATH", ""))
            working_directory = str(self.config.repo_root)
        else:
            # A release run must prove the archive is self-sufficient: no
            # repository on the import path and no repository as the working
            # directory, so a module the release forgot to package cannot be
            # silently supplied from the source tree.
            environment.pop("PYTHONPATH", None)
            working_directory = str(self.config.work_dir)
        environment["UBM_INTEGRATION_CONTROL_APPROVED"] = "1"
        self.stderr_path = self.startup_path.with_suffix(".stderr")
        self._stderr_handle = self.stderr_path.open("wb")
        self.process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(self.command), cwd=working_directory, env=environment,
            stdout=subprocess.DEVNULL, stderr=self._stderr_handle)

    def await_ready(self) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.readiness_timeout_s
        last: Exception | None = None
        readiness: dict[str, Any] = {}
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise SelfTestError(
                    "the upper process exited with %s before readiness:\n%s"
                    % (self.process.returncode, self._stderr()))
            try:
                for origin in self.endpoints.upper_origins():
                    response = json_request(
                        origin=origin, method="GET", path="/ubm/v1/readiness",
                        ssl_context=self.client_context, timeout_ms=2000)
                    if response.status != 200:
                        raise SelfTestError("readiness on %s returned %d" % (origin, response.status))
                    readiness[origin] = response.json()
                if all(entry.get("ready") for entry in readiness.values()):
                    return readiness
            except (OSError, SelfTestError) as error:
                last = error
                readiness.clear()
            time.sleep(0.1)
        raise SelfTestError("upper never became ready: %s\n%s" % (last, self._stderr()))

    def export(self, out_dir: Path) -> dict[str, Any]:
        response = json_request(
            origin=self.endpoints.r1, method="POST", path="/ubm/v1/export",
            payload={"outDir": str(out_dir)}, ssl_context=self.client_context, timeout_ms=10000)
        if response.status != 200:
            raise SelfTestError("capture export returned %d: %s"
                                % (response.status, response.json()))
        return response.json()

    def stop(self) -> dict[str, Any]:
        """Stop the upper and report every scalar the stop produced.

        Not just the exit code: an upper that ignored POST /ubm/v1/stop and had
        to be signalled behaved differently from one that shut down on request,
        and a verdict that reads only the code cannot see that.
        """
        if self.process is None:
            return {"exitCode": 0, "stopDisposition": "NOT_STARTED",
                    "stderrBytes": 0, "signalled": False}
        try:
            json_request(origin=self.endpoints.r1, method="POST", path="/ubm/v1/stop",
                         payload={}, ssl_context=self.client_context, timeout_ms=3000)
        except OSError:
            pass
        disposition = "GRACEFUL"
        try:
            code = self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            disposition = "TERMINATED"
            self.process.terminate()
            try:
                code = self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                disposition = "KILLED"
                self.process.kill()
                code = self.process.wait(timeout=5)
        self.process = None
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None
        stderr = self.stderr_path.stat().st_size if (
            self.stderr_path is not None and self.stderr_path.exists()) else 0
        return {"exitCode": int(code), "stopDisposition": disposition,
                "stderrBytes": int(stderr), "signalled": disposition != "GRACEFUL"}

    def _stderr(self) -> str:
        if self.stderr_path is None or not self.stderr_path.exists():
            return ""
        return self.stderr_path.read_text("utf-8", errors="replace")[-4000:]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_ID_TOKEN_MARKER = "{SERVER_ASSIGNED:"

#: Identifier roles the driver knows how to recognise in a capture.  Every one
#: of them must also be declared in the spec's ``identifierSources``; a role the
#: spec does not declare, or a declared role this driver cannot find, is a hard
#: failure rather than a silent narrowing of the comparison.
_KNOWN_IDENTIFIER_ROLES = {
    "policyId": "/exchanges/*/capturedOutputs/policyId",
    "dataJobId": "/exchanges/*/capturedOutputs/dataJobId",
    "deliveryBindingId": "/exchanges/*/capturedOutputs/deliveryBindingId",
    "terminalEvidenceRef": "/coordinator/terminalEvidenceRef",
}

#: capturedOutputs members that name an identifier the upper assigns.
_ASSIGNED_MEMBERS = frozenset({"policyId", "dataJobId", "deliveryBindingId"})

#: Every capturedOutputs member whose provenance this driver has a rule for.
#: A member outside this set is reported rather than assumed (fail-closed).
_CLASSIFIED_MEMBERS = _ASSIGNED_MEMBERS | frozenset({
    "registrationId", "subscriptionId", "policyObjectJcsSha256"})


class VolatileRule:
    """The declared server-assigned-identifier derivation rule.

    Read from ``release-gates.1.0.0.json#/serverAssignedIdentifierDerivation``
    rather than hard-coded here, so the spec is the single statement of which
    slots may be excused and why.  Construction fails closed: an absent block,
    an unknown handling, an undeclared identifier role, or an exempt pointer
    this driver cannot honour all abort the self-test instead of quietly
    widening (or narrowing) the exemption.
    """

    def __init__(self, gates: Mapping[str, Any]) -> None:
        block = gates.get("serverAssignedIdentifierDerivation")
        if not isinstance(block, Mapping):
            raise SelfTestError(
                "release-gates.1.0.0.json declares no "
                "serverAssignedIdentifierDerivation block; refusing to invent one")
        self.document = json.loads(json.dumps(block))
        roles = {str(entry["roleToken"]).rsplit(":", 1)[-1].rstrip("}")
                 for entry in block["identifierSources"]}
        if roles != set(_KNOWN_IDENTIFIER_ROLES):
            raise SelfTestError(
                "declared identifier roles %s do not match the roles this driver "
                "canonicalises %s" % (sorted(roles), sorted(_KNOWN_IDENTIFIER_ROLES)))
        #: role -> declared source (binding-provenance vocabulary).  Only
        #: SERVER_ASSIGNED roles may be tokenised; everything else keeps its
        #: literal under byte comparison, and an undeclared role is reported.
        self.role_sources: dict[str, str] = {}
        self.server_assigned_roles: frozenset[str] = frozenset()
        self.canonicalise_pointers: list[str] = []
        #: Digest slots the RUNTIME re-digests with identifiers tokenised; the
        #: raw slot is dropped from the comparison but its canonical companion
        #: is compared, so detection is retained rather than surrendered.
        self.source_canonical_pointers: list[str] = []
        self.canonical_companions: dict[str, str] = {}
        self.exempt_pointers: list[str] = []
        for entry in block.get("identifierRoles", {}).get("roles", ()):
            role, source = str(entry["role"]), str(entry["source"])
            if not str(entry.get("evidence", "")).strip():
                raise SelfTestError("identifier role %s carries no evidence" % role)
            self.role_sources[role] = source
        self.server_assigned_roles = frozenset(
            role for role, source in self.role_sources.items()
            if source == "SERVER_ASSIGNED")
        if not self.server_assigned_roles:
            raise SelfTestError(
                "no identifier role is declared SERVER_ASSIGNED; refusing to "
                "canonicalise anything on a guess")
        for slot in block["derivedSlots"]:
            pointer, handling = str(slot["pointer"]), str(slot["handling"])
            if not str(slot.get("evidence", "")).strip():
                raise SelfTestError("derived slot %s carries no evidence" % pointer)
            if handling == "CANONICALISE":
                self.canonicalise_pointers.append(pointer)
            elif handling == "CANONICALISE_AT_SOURCE":
                companion = str(slot.get("canonicalCompanion", ""))
                if not companion:
                    raise SelfTestError(
                        "slot %s is CANONICALISE_AT_SOURCE but names no "
                        "canonicalCompanion" % pointer)
                self.source_canonical_pointers.append(pointer)
                self.canonical_companions[pointer] = companion
            elif handling == "EXEMPT_WHEN_REFERENCED":
                if not str(slot.get("whyNotCanonicalisable", "")).strip():
                    raise SelfTestError(
                        "slot %s stays EXEMPT_WHEN_REFERENCED without saying why "
                        "it cannot be canonicalised at source" % pointer)
                self.exempt_pointers.append(pointer)
            else:
                raise SelfTestError(
                    "derived slot %s declares unknown handling %r" % (pointer, handling))
        for pointer in self.canonicalise_pointers:
            if pointer in self.exempt_pointers:
                raise SelfTestError("slot %s is both canonicalised and exempt" % pointer)
        for pointer in self.exempt_pointers + self.source_canonical_pointers:
            if not pointer.endswith("Sha256"):
                raise SelfTestError(
                    "only a digest slot may be excused from equality, not %s" % pointer)
        # A slot may not be excused twice: the blanket allowlist and the
        # derivation table must be disjoint.
        overlap = sorted(set(gates["volatileFieldAllowlist"])
                         & set(self.canonicalise_pointers + self.exempt_pointers
                               + self.source_canonical_pointers))
        if overlap:
            raise SelfTestError(
                "these slots are excused both by the blanket allowlist and by the "
                "derivation rule: %s" % overlap)

    def summary(self) -> dict[str, Any]:
        return {
            "source": "release-gates.1.0.0.json#/serverAssignedIdentifierDerivation",
            "roleSources": dict(sorted(self.role_sources.items())),
            "serverAssignedRoles": sorted(self.server_assigned_roles),
            "canonicaliseSlots": list(self.canonicalise_pointers),
            "canonicaliseAtSourceSlots": list(self.source_canonical_pointers),
            "canonicalCompanions": dict(self.canonical_companions),
            "exemptSlots": list(self.exempt_pointers),
            "statement": self.document["statement"],
            "compensatingControls": self.document["compensatingControls"],
        }


def _pointer_segments(pointer: str) -> list[str]:
    return [token for token in pointer.split("/") if token]


def _drop_declared_pointer(document: Any, segments: Sequence[str],
                           prefix: str = "") -> list[str]:
    """Remove one declared (possibly ``*``-wildcarded) pointer; report what went."""
    if not segments:
        return []
    head, rest = segments[0], segments[1:]
    removed: list[str] = []
    if isinstance(document, Mapping):
        keys = list(document) if head == "*" else ([head] if head in document else [])
        for key in keys:
            if rest:
                removed.extend(_drop_declared_pointer(
                    document[key], rest, "%s/%s" % (prefix, key)))
            elif document.pop(key, None) is not None:
                removed.append("%s/%s" % (prefix, key))
    elif isinstance(document, list):
        indices = range(len(document)) if head == "*" else (
            [int(head)] if head.isdigit() and int(head) < len(document) else [])
        for index in indices:
            if rest:
                removed.extend(_drop_declared_pointer(
                    document[index], rest, "%s/%d" % (prefix, index)))
    return removed


def _identifier_role(member: str, exchange: Mapping[str, Any]) -> str:
    """The role an assigned identifier plays, keyed by WHO asked for it.

    Two resources of the same kind can legitimately exist in one scenario when
    they were requested by different parties: SC-083's declared ``create-dme-job``
    step is issued by the counterpart, while ``COORDINATOR_PROCESS_INTENT``
    makes the upper's own rApp create a data job of its own.  Keying the role on
    the member name alone collapses those two into one and reports a false
    inconsistency; keying it on the originator as well keeps them apart without
    weakening the check, because two values in the SAME role are still a
    finding.
    """
    peer = str(exchange.get("peer") or "LOWER_RUNNER")
    origin = "UPPER_SELF" if peer == "UPPER_SELF" else "COUNTERPART"
    return "%s@%s" % (member, origin)


def _deployment_supplied_values(vector: Any, constants: Any,
                                provenance: Any) -> frozenset[str]:
    """Identifiers the DEPLOYMENT supplies; canonicalising them hides a forgery.

    ``binding-provenance.1.0.0.json`` classifies every identifier, and only
    SERVER_ASSIGNED is minted per run.  A fixed value such as
    ``deployment.r1.dme.activeDeliveryBindingId`` must stay under byte
    comparison: replacing it with a role token would make a counterpart that
    forged it compare equal to one that did not.
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif isinstance(node, str) and len(node) >= 8:
            found.add(node)

    walk(vector)
    walk(constants)
    if isinstance(provenance, Mapping):
        for binding in provenance.get("bindings", ()):
            if isinstance(binding, Mapping) and \
                    str(binding.get("class")) != "SERVER_ASSIGNED":
                walk(binding.get("value"))
    return frozenset(found)


def _assigned_identifiers(outcome: "RunOutcome",
                          supplied: frozenset[str] = frozenset(),
                          server_assigned_roles: frozenset[str] | None = None,
                          ) -> list[tuple[str, str]]:
    """(value, role-token) pairs.

    The token is derived from the identifier's *role*, never from iteration
    order, so two runs canonicalise to the same text even though the values are
    freshly minted each time.
    """
    found: dict[str, str] = {}
    for document in outcome.captures.values():
        for exchange in document.get("exchanges", []):
            for key, value in (exchange.get("capturedOutputs") or {}).items():
                if key not in _ASSIGNED_MEMBERS or not isinstance(value, str):
                    continue
                role = _identifier_role(key, exchange)
                # Declared source decides, not the shape of the value: a role
                # the spec calls VECTOR_SUPPLIED (or does not declare at all)
                # keeps its literal, so a forged fixed identifier is detected.
                if server_assigned_roles is not None and role not in server_assigned_roles:
                    continue
                if value in supplied:
                    continue
                found[value] = _ID_TOKEN_MARKER + role + "}"
        reference = (document.get("coordinator") or {}).get("terminalEvidenceRef")
        if isinstance(reference, str) and reference not in supplied:
            found[reference] = _ID_TOKEN_MARKER + "terminalEvidenceRef}"
    return sorted(found.items(), key=lambda item: len(item[0]), reverse=True)


def unattributed_exchanges(outcome: "RunOutcome") -> list[str]:
    """Exchanges whose originator the upper could not establish.

    ``UNATTRIBUTED`` is the runtime's fail-closed answer when a request's client
    address cannot be matched to a live connection it opened.  Treating it as a
    finding is the point: an originator nobody could establish must not be
    quietly filed under either party.
    """
    findings: list[str] = []
    for scenario_id, document in sorted(outcome.captures.items()):
        for index, exchange in enumerate(document.get("exchanges", [])):
            if str(exchange.get("peer")) == "UNATTRIBUTED":
                findings.append("%s/exchanges/%d has an unattributed originator"
                                % (scenario_id, index))
        ambiguities = ((document.get("externalCalls") or {})
                       .get("peerAttributionAmbiguities") or [])
        for entry in ambiguities:
            findings.append("%s: ambiguous peer attribution for %s"
                            % (scenario_id, entry.get("clientAddress")))
    return findings


def unclassified_captured_members(outcome: "RunOutcome") -> list[str]:
    """capturedOutputs members with no declared provenance class.

    Fail-closed: an identifier whose origin nobody declared is NOT canonicalised
    (so a change to it is still detected) and is reported, rather than being
    quietly assumed to be server-assigned.
    """
    findings: list[str] = []
    for scenario_id, document in sorted(outcome.captures.items()):
        for index, exchange in enumerate(document.get("exchanges", [])):
            for key in sorted((exchange.get("capturedOutputs") or {})):
                if key not in _CLASSIFIED_MEMBERS:
                    findings.append("%s/exchanges/%d/capturedOutputs/%s has no "
                                    "declared provenance class"
                                    % (scenario_id, index, key))
    return findings


def _canonicalise_identifiers(document: Any, identifiers: Sequence[tuple[str, str]]) -> Any:
    """Replace every occurrence of a server-assigned id with a role-stable token.

    Stronger than deleting the field: the *shape* of the document still has to
    match, and an identifier used inconsistently within one run still shows up.
    """
    rendered = json.dumps(document, sort_keys=True)
    for identifier, token in identifiers:
        rendered = rendered.replace(identifier, token)
    return json.loads(rendered)


def _pointer_diff(left: Any, right: Any, prefix: str = "") -> list[str]:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        differences: list[str] = []
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                differences.append("%s/%s" % (prefix, key))
                continue
            differences.extend(_pointer_diff(left[key], right[key], "%s/%s" % (prefix, key)))
        return differences
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return ["%s (length %d vs %d)" % (prefix, len(left), len(right))]
        differences = []
        for index, (one, two) in enumerate(zip(left, right)):
            differences.extend(_pointer_diff(one, two, "%s/%d" % (prefix, index)))
        return differences
    return [] if left == right else [prefix or "/"]


def _matches_allowlist(pointer: str, allowlist: Sequence[str]) -> bool:
    clean = pointer.split(" ", 1)[0]
    actual = [token for token in clean.split("/") if token]
    for entry in allowlist:
        declared = [token for token in entry.split("/") if token]
        if len(declared) > len(actual):
            continue
        if all(want == "*" or want == have for want, have in zip(declared, actual)):
            return True
    return False


#: G-SEC-1's four secret-shaped literals.  They are assembled from fragments so
#: that this file -- which ships inside the release as ``selftest/driver.py`` --
#: does not itself contain the literals the gate requires zero occurrences of.
_FORBIDDEN_SECRET_MARKERS = (
    "-----" + "BEGIN", "Bearer" + " ", "password" + "=", "secret" + "=")


def _secret_leaks(raw: str, material: Iterable[str]) -> list[str]:
    findings = [marker for marker in _FORBIDDEN_SECRET_MARKERS if marker in raw]
    for value in material:
        if value and value in raw:
            findings.append("private-material")
    return findings


def _validate_capture(document: Mapping[str, Any], schema_path: Path) -> list[str]:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError:  # pragma: no cover - jsonschema is a declared dependency
        return ["jsonschema is unavailable; capture validation was not performed"]
    schema = json.loads(schema_path.read_text("utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return ["%s: %s" % ("/".join(str(part) for part in error.path), error.message)
            for error in sorted(validator.iter_errors(document), key=lambda item: list(item.path))]


def probe_capture_schema_satisfiability(schema_path: Path) -> dict[str, Any]:
    """Prove whether ``$defs.exchange.properties.response`` can be satisfied at all.

    The response object composes ``messageDigest`` with a status-bearing branch
    through ``allOf``.  If the member set were closed inside ``messageDigest``
    with ``additionalProperties: false`` the two branches would contradict each
    other - ``allOf`` branches are evaluated independently, so ``status`` would
    be an *additional* property for the first branch while the second requires
    it - and no capture document could ever validate.  This probe measures the
    shipped bytes rather than trusting prose, and also proves the composition is
    still *closed*: an unknown member must be rejected.
    """
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError:  # pragma: no cover
        return {"probed": False, "reason": "jsonschema unavailable"}
    schema = json.loads(schema_path.read_text("utf-8"))
    response_schema = dict(
        schema["$defs"]["exchange"]["properties"]["response"],
        **{"$schema": schema["$schema"], "$defs": schema["$defs"]})
    validator = Draft202012Validator(response_schema, format_checker=FormatChecker())
    minimal = {"headerNamesPresent": [], "headerBlockSha256": "a" * 64,
               "bodyByteCount": 0, "bodyRawSha256": "b" * 64}
    with_status = [error.message for error in validator.iter_errors(dict(minimal, status=204))]
    without_status = [error.message for error in validator.iter_errors(dict(minimal))]
    with_unknown = [error.message for error in
                    validator.iter_errors(dict(minimal, status=204, unknownMember=1))]
    return {
        "probed": True,
        "schemaPath": str(schema_path),
        "pointer": "#/$defs/exchange/properties/response",
        "withStatusErrors": with_status,
        "withoutStatusErrors": without_status,
        "withUnknownMemberErrors": with_unknown,
        "unsatisfiable": bool(with_status) and bool(without_status),
        "satisfiableAndClosed": (not with_status) and bool(without_status)
                                and bool(with_unknown),
    }


# --------------------------------------------------------------------------
# a single run
# --------------------------------------------------------------------------

@dataclass
class RunOutcome:
    label: str
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    unknown_routes: int = 0
    upper_404s: list[str] = field(default_factory=list)
    child_exit: int | None = None
    #: Every other scalar the child's stop produced.  Kept beside the exit code
    #: so a sweep of "run-level scalar signals" has one place to look.
    stop_signals: dict[str, Any] = field(default_factory=dict)
    captures: dict[str, Any] = field(default_factory=dict)
    capture_errors: dict[str, list[str]] = field(default_factory=dict)
    secret_findings: list[str] = field(default_factory=list)
    unexpected_authorities: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def dispositions(self) -> dict[str, str]:
        return {entry["scenarioId"]: entry["disposition"] for entry in self.scenarios}

    @property
    def failed(self) -> bool:
        return self.error is not None or any(
            entry["disposition"] != "PASS" for entry in self.scenarios)


class SelfTestRunner:
    def __init__(self, config: SelfTestConfig):
        if not openssl_available():
            raise SelfTestError("openssl is required; refusing to run the self-test in plaintext")
        self.config = config
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir: Path | None = None
        if config.release_root is None:
            self.tls = bootstrap_loopback_tls(self.config.work_dir / "tls")
        else:
            self.runtime_dir, self.tls = self._bootstrap_release_runtime()
        self.vector = json.loads(self.config.vector_path.read_text("utf-8"))
        self.endpoints = UbmEndpoints.from_vector(self.vector)
        self.contract = FrozenContract(self.config.bundle_dir)
        self.gates = json.loads(self.config.gates_path.read_text("utf-8"))
        provenance_path = self.config.spec_dir / "binding-provenance.1.0.0.json"
        self.supplied_values = _deployment_supplied_values(
            self.vector, self.contract.constants,
            json.loads(provenance_path.read_text("utf-8"))
            if provenance_path.is_file() else None)
        self.private_material = self.tls.private_key.read_text("utf-8")

    # -- release runtime bootstrap ---------------------------------------
    def _bootstrap_release_runtime(self) -> tuple[Path, Any]:
        """Run the release's own ``bin/ubm bootstrap`` and adopt its TLS anchor.

        Nothing here reaches into the release's Python: the launcher is executed
        as a separate process exactly the way an operator would run it, and only
        the files it writes are read back.  Using the release's own CA as the
        lower double's anchor keeps certificate verification switched on at both
        ends without minting a second, unrelated trust root.
        """
        launcher = self.config.launcher_path
        if not launcher.is_file():
            raise SelfTestError("release launcher %s is missing" % launcher)
        manifest = self.config.release_manifest_path
        if not manifest.is_file():
            raise SelfTestError("release manifest %s is missing" % manifest)
        runtime_dir = self.config.work_dir / "release-runtime"
        command = [
            str(launcher), "bootstrap",
            "--out", str(runtime_dir),
            "--vector", str(self.config.vector_path),
            "--contract-authority", str(
                self.config.artifact_root / "contracts" / "oran-aic" / "1.0.1"),
            "--run-id", "ubm-selftest-bootstrap",
            "--release-manifest", str(manifest),
            "--integration-control-surface", "approved",
        ]
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command, cwd=str(self.config.work_dir), env=environment,
            capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise SelfTestError(
                "release bootstrap failed with exit %d:\n%s"
                % (completed.returncode, completed.stderr[-4000:]))
        tls_dir = runtime_dir / "tls"
        leaf, key = tls_dir / "loopback-leaf.pem", tls_dir / "loopback-leaf.key"
        anchor = tls_dir / "loopback-ca.pem"
        for path in (leaf, key, anchor, runtime_dir / "secret-map.json",
                     runtime_dir / "integration-values.json"):
            if not path.is_file():
                raise SelfTestError("release bootstrap did not write %s" % path.name)
        material = LoopbackTlsMaterial(
            directory=tls_dir, certificate=leaf, private_key=key, trust_anchor=anchor)
        return runtime_dir, material

    # -- startup file ----------------------------------------------------
    def _startup_document(self, run_id: str) -> dict[str, Any]:
        """One document both uppers accept.

        ``oran/release/ubm/config.py::load_startup_config`` reads a fixed member
        list and ignores the rest, and the in-package stand-in reads its own
        members with ``.get``, so the union is unambiguous for either process.
        """
        references = self.tls.reference_map()
        binding_sha = sha256_bytes(self.config.binding_path.read_bytes())
        document: dict[str, Any] = {
            "profile": "bilateral-mock",
            "runId": run_id,
            "vectorPath": str(self.config.vector_path),
            "contractBundle": str(self.config.bundle_dir),
            "stateDir": str(self.config.work_dir / run_id / "state"),
            "secretMap": references,
            "tlsCertificateRef": "file://ubm-selftest/tls/certificate",
            "tlsPrivateKeyRef": "file://ubm-selftest/tls/private-key",
            "tlsTruststoreRef": "file://ubm-selftest/tls/truststore",
            "integrationControlSurface": "approved",
            "bindingDocSha256": binding_sha,
            "logicalOrigin": "2026-08-04T00:00:00Z",
        }
        if self.runtime_dir is not None:
            document.update({
                "vectorSha256": sha256_bytes(self.config.vector_path.read_bytes()),
                "integrationValuesPath": str(self.runtime_dir / "integration-values.json"),
                "contractAuthority": str(
                    self.config.artifact_root / "contracts" / "oran-aic" / "1.0.1"),
                "secretMapPath": str(self.runtime_dir / "secret-map.json"),
                "releaseManifestPath": str(self.config.release_manifest_path),
            })
        return document

    def _upper_command(self, startup_path: Path) -> tuple[tuple[str, ...], str]:
        if self.config.upper_command:
            command = tuple(
                part.replace("{startup}", str(startup_path)) for part in self.config.upper_command)
            return command, self.config.upper_label or "EXTERNAL_UPPER_COMMAND"
        if self.config.release_root is not None:
            manifest = json.loads(self.config.release_manifest_path.read_text("utf-8"))
            return (
                (str(self.config.launcher_path), "start", "--startup", str(startup_path)),
                "UPPER_BILATERAL_MOCK_RELEASE_RUNTIME %s/%s releaseContentSha256=%s"
                % (manifest["releaseId"], manifest["releaseVersion"],
                   manifest["releaseContentSha256"]),
            )
        return (
            (sys.executable, "-m", "oran.release.ubm_selftest.stub_upper",
             "--startup", str(startup_path)),
            "SELFTEST_STUB_UPPER_NOT_RELEASE_RUNTIME",
        )

    # -- one run ---------------------------------------------------------
    def run(self, *, label: str, order: Sequence[str],
            driver_mutations: Sequence[str] = (),
            proxy_mutation: str | None = None) -> RunOutcome:
        outcome = RunOutcome(label=label)
        run_id = "%s-%s" % (label, uuid.uuid4().hex[:8])
        run_dir = self.config.work_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        startup_path = run_dir / "startup.json"
        startup_path.write_text(
            json.dumps(self._startup_document(run_id), indent=2), encoding="utf-8")
        command, upper_label = self._upper_command(startup_path)
        outcome_label = upper_label

        allowlist = {authority_of(origin) for origin in self.endpoints.upper_origins()}
        allowlist.add(authority_of(self.endpoints.lower_a1))

        double_listen = authority_of(self.endpoints.lower_a1)
        connect_overrides: dict[str, str] = {}
        proxies: list[MutationProxy] = []
        if proxy_mutation == mut.M01_FLIP_A1_POLICY_BYTE:
            double_listen = "127.0.0.1:%d" % INGRESS_PROXY_BACKEND_PORT
            allowlist.add(double_listen)
            proxies.append(MutationProxy(
                host="127.0.0.1", port=int(authority_of(self.endpoints.lower_a1).rsplit(":", 1)[1]),
                upstream_origin="https://%s" % double_listen,
                server_context=self.tls.server_context(), client_context=self.tls.client_context(),
                name="m01-ingress", mutate_request=flip_one_policy_byte))
        elif proxy_mutation == mut.M04_R1_CREATE_200:
            proxy_authority = "127.0.0.1:%d" % EGRESS_PROXY_PORT
            allowlist.add(proxy_authority)
            connect_overrides[self.endpoints.r1] = proxy_authority
            proxies.append(MutationProxy(
                host="127.0.0.1", port=EGRESS_PROXY_PORT,
                upstream_origin=self.endpoints.r1,
                server_context=self.tls.server_context(), client_context=self.tls.client_context(),
                name="m04-egress", mutate_response=r1_create_201_to_200))

        upper = UpperProcess(
            config=self.config, startup_path=startup_path, command=command, label=upper_label,
            endpoints=self.endpoints, client_context=self.tls.client_context())
        double = ContractFaithfulLowerDouble(
            catalog_path=self.config.bundle_dir / "scenario-catalog.1.0.1.json",
            runner_contract_path=self.config.bundle_dir / "scenario-runner-contract.1.0.1.json",
            vector=self.vector,
            ssl_context=self.tls.server_context(),
            upper=self.endpoints,
            client_ssl_context=self.tls.client_context(),
            integration_control_surface_path=self.config.ics_path,
            connect_overrides=connect_overrides,
            listen_authority=double_listen,
            driver_mutations=driver_mutations,
            observation_deadline_ms=self.config.observation_deadline_ms)

        guard = SocketAuthorityGuard(sorted(allowlist))
        try:
            with guard:
                for proxy in proxies:
                    proxy.start()
                double.start()
                upper.start()
                upper.await_ready()
                for scenario_id in order:
                    result = double.run_scenario(scenario_id)
                    outcome.scenarios.append({
                        "scenarioId": result.scenario_id,
                        "disposition": result.observations["disposition"],
                        "httpSequence": list(result.http_sequence),
                        "expectedHttpSequence": result.observations["expectedHttpSequence"],
                        "failures": result.observations["failures"],
                        "unknownRoutes": result.unknown_routes,
                        "normalized": result.observations["normalized"],
                        "countersAtScenarioStart": result.observations["countersAtScenarioStart"],
                    })
                    outcome.unknown_routes += result.unknown_routes
                    outcome.upper_404s.extend(result.observations["upper404s"])
                    exported = upper.export(run_dir / "captures")
                    raw = Path(exported["capturePath"]).read_text("utf-8")
                    document = json.loads(raw)
                    outcome.captures[scenario_id] = document
                    errors = _validate_capture(document, self.config.capture_schema_path)
                    if errors:
                        outcome.capture_errors[scenario_id] = errors
                    outcome.secret_findings.extend(
                        _secret_leaks(raw, [self.private_material]))
        except Exception as error:  # noqa: BLE001 - the report records the failure
            outcome.error = "%s: %s" % (type(error).__name__, error)
        finally:
            double.stop()
            for proxy in proxies:
                proxy.stop()
            try:
                stopped = upper.stop()
                outcome.child_exit = stopped["exitCode"]
                outcome.stop_signals = stopped
            except Exception as error:  # noqa: BLE001
                outcome.error = outcome.error or "upper stop failed: %s" % error
            outcome.unexpected_authorities = guard.unexpected()
        outcome.label = "%s@%s" % (label, outcome_label)
        return outcome


# --------------------------------------------------------------------------
# whole suite
# --------------------------------------------------------------------------

def _normalized_scenarios(outcome: RunOutcome) -> dict[str, Any]:
    return {
        entry["scenarioId"]: {
            "disposition": entry["disposition"],
            "httpSequence": entry["httpSequence"],
            "normalized": entry["normalized"],
        }
        for entry in outcome.scenarios
    }


def run_selftest(config: SelfTestConfig) -> dict[str, Any]:
    runner = SelfTestRunner(config)
    allowlist = runner.gates["volatileFieldAllowlist"]
    rule = VolatileRule(runner.gates)
    forward = tuple(config.scenarios)
    reverse = tuple(reversed(forward))

    run_a = runner.run(label="run-a", order=forward)
    run_b = runner.run(label="run-b", order=forward)
    run_reverse = runner.run(label="run-reverse", order=reverse)

    falsifiers: list[dict[str, Any]] = []
    for mutation in mut.MUTATIONS:
        order = tuple(item for item in forward if item in mutation.scenarios)
        if not order:
            # Never silently "pass" a falsifier that had nothing to bite on.
            falsifiers.append({
                "id": mutation.identifier, "seam": mutation.seam,
                "description": mutation.description, "scenariosRun": [],
                "dispositions": {}, "firstFailure": None, "detected": False,
                "result": "FALSIFIER_NOT_EXERCISED",
            })
            continue
        driver_mutations = (mutation.identifier,) if mutation.identifier in mut.DRIVER_MUTATIONS else ()
        proxy_mutation = mutation.identifier if mutation.identifier in mut.PROXY_MUTATIONS else None
        outcome = runner.run(
            label="mutant-%s" % mutation.identifier, order=order,
            driver_mutations=driver_mutations, proxy_mutation=proxy_mutation)
        detected = outcome.failed
        falsifiers.append({
            "id": mutation.identifier,
            "seam": mutation.seam,
            "description": mutation.description,
            "scenariosRun": list(order),
            "dispositions": outcome.dispositions,
            "firstFailure": next(
                (failure for entry in outcome.scenarios for failure in entry["failures"]),
                outcome.error),
            "detected": detected,
            "result": "FALSIFIER_HELD" if detected else "FALSIFIER_BROKEN",
        })

    supplied = runner.supplied_values
    determinism = _compare_runs(run_a, run_b, allowlist, rule, supplied=supplied)
    permutation = _compare_runs(run_a, run_reverse, allowlist, rule,
                                order_insensitive=True, supplied=supplied)

    unknown_routes = run_a.unknown_routes + run_b.unknown_routes + run_reverse.unknown_routes
    upper_404s = run_a.upper_404s + run_b.upper_404s + run_reverse.upper_404s
    external = [
        {"scenarioId": scenario_id,
         "externalLiveTargetCalls": document["externalCalls"]["externalLiveTargetCalls"],
         "hardwareCalls": document["externalCalls"]["hardwareCalls"],
         "attemptedAuthorities": document["externalCalls"]["attemptedAuthorities"],
         # Zero means nothing unless something was measuring.  A release run
         # must report an installed guard; the in-package stand-in reports
         # false, and the gate below refuses to read that as a clean result.
         "guardInstalled": document["externalCalls"].get("guardInstalled", False),
         "guardMethod": document["externalCalls"].get("guardMethod", ""),
         "hardwareDefinitionSource": document["externalCalls"].get(
             "hardwareDefinitionSource", ""),
         "connectionAttempts": document["externalCalls"].get("connectionAttempts", 0),
         "violations": document["externalCalls"].get("violations", []),
         "liveOutboundConnections": document["externalCalls"].get(
             "liveOutboundConnections", 0),
         "peerAttributionAmbiguities": document["externalCalls"].get(
             "peerAttributionAmbiguities", [])}
        for scenario_id, document in sorted(run_a.captures.items())
    ]

    satisfiability = probe_capture_schema_satisfiability(config.capture_schema_path)
    # Every schema error is a genuine capture omission: the response composition
    # is satisfiable, so there is no tolerated error class left to excuse.
    #
    # All three runs are folded in, not just the forward one.  Reading only
    # run-a is the same defect class as reading `identical` without the
    # consistency findings: the suite would produce evidence from the repeat
    # and reverse runs and then decline to look at it.
    all_runs = (("forward", run_a), ("forwardRepeat", run_b), ("reverse", run_reverse))
    genuine_capture_errors: dict[str, list[str]] = {
        "%s/%s" % (label, scenario_id): errors
        for label, outcome in all_runs
        for scenario_id, errors in sorted(outcome.capture_errors.items()) if errors}
    all_secret_findings = sorted({
        "%s: %s" % (label, finding)
        for label, outcome in all_runs for finding in outcome.secret_findings})
    all_unexpected_authorities = sorted({
        "%s: %s" % (label, authority)
        for label, outcome in all_runs for authority in outcome.unexpected_authorities})
    run_errors = {label: outcome.error for label, outcome in all_runs
                  if outcome.error is not None}
    run_signals = _run_scalar_signals(all_runs)

    completion = runner.gates["selfTestCompletion"]
    passes = sum(1 for entry in run_a.scenarios if entry["disposition"] == "PASS")
    fails = sum(1 for entry in run_a.scenarios if entry["disposition"] == "FAIL")

    blockers: list[str] = []
    if not satisfiability.get("satisfiableAndClosed"):
        blockers.append(
            "capture-schema.1.0.0.json#/$defs/exchange/properties/response must be "
            "satisfiable *and* closed; the shipped bytes are not. Probe: %s"
            % json.dumps(satisfiability, sort_keys=True))

    gates: dict[str, dict[str, Any]] = {}

    def _gate(identifier: str, ok: bool, evidence: Any, blocked: bool = False) -> None:
        gates[identifier] = {
            "status": "BLOCKED" if blocked else ("PASS" if ok else "FAIL"),
            "evidence": evidence,
        }

    _gate("UBM-ST-C01", run_a.error is None and all(
        "CONTRACT_FAITHFULNESS" not in failure
        for entry in run_a.scenarios for failure in entry["failures"]),
        {"catalogSha256": runner.contract.catalog_sha256,
         "runnerContractSha256": runner.contract.runner_contract_sha256,
         "note": "the double resolves every route, op and initial state from these bytes only"})
    _gate("UBM-ST-D01", unknown_routes == 0 and not upper_404s,
          {"unknownRoutes": unknown_routes, "upper404s": upper_404s})
    for item in falsifiers:
        _gate(item["id"], item["detected"],
              {"seam": item["seam"], "dispositions": item["dispositions"],
               "firstFailure": item["firstFailure"]})
    negative_control = negative_control_comparison(run_a, allowlist, forward, rule,
                                                  supplied)
    conformance = rule_conformance(run_a, rule, supplied)
    _gate("UBM-ST-R01", determinism["identical"], determinism)
    _gate("UBM-ST-R01-NEG", bool(negative_control.get("comparatorIsFalsifiable")), negative_control)
    _gate("UBM-ST-R01-DECL", bool(conformance["conforms"]), conformance)
    leaked = [item for item in run_signals["offNominal"]
              if "nonZeroStartCounters" in item]
    _gate("UBM-ST-L01", permutation["identical"] and not leaked,
          dict(permutation, startCounterLeakageByRun=leaked))
    counters_were_measured = all(item["guardInstalled"] for item in external)
    _gate("UBM-ST-X01",
          not all_unexpected_authorities
          and all(item["externalLiveTargetCalls"] == 0 and item["hardwareCalls"] == 0
                  and not item["violations"]
                  and not item["peerAttributionAmbiguities"] for item in external)
          # Against the release artifact the counters must come from a guard
          # that was actually armed; a zero nobody measured is not evidence.
          and (counters_were_measured or config.release_root is None),
          {"unexpectedAuthorities": all_unexpected_authorities,
           "externalCalls": external,
           "countersWereMeasured": counters_were_measured,
           "upperUnderTestIsReleaseArtifact": config.release_root is not None})
    _gate("UBM-ST-S01",
          not genuine_capture_errors and not all_secret_findings
          and bool(satisfiability.get("satisfiableAndClosed")),
          {"captureOmissions": genuine_capture_errors,
           "secretFindings": all_secret_findings,
           "captureSchemaSatisfiability": satisfiability,
           "runsFolded": [label for label, _outcome in all_runs]})
    # Every run's SCALAR signals, not only the forward run's, and not only the
    # arrays.  The previous sweep folded every finding array and left the exit
    # code -- a scalar -- read from run-a alone; this closes that class.
    _gate("UBM-ST-E01", not run_errors and not run_signals["offNominal"],
          {"runErrors": run_errors,
           "signals": run_signals,
           "requirement": "for EVERY run (forward, repeat, reverse): no error, "
                          "child exit 0, a graceful stop, zero unknown routes and "
                          "zero upper 404s"})
    _gate("COMPLETION",
          run_a.error is None and passes == completion["pass"] and fails == completion["fail"]
          and sorted(run_a.dispositions) == sorted(completion["scenarioSet"])
          and not run_signals["offNominal"],
          {"pass": passes, "fail": fails, "skip": 0,
           "childExitByRun": {label: outcome.child_exit for label, outcome in all_runs},
           "scenarioSet": sorted(run_a.dispositions)})

    suite_ok = all(entry["status"] == "PASS" for entry in gates.values()) and not blockers

    return {
        "stateLabel": SELF_TEST_STATE_LABEL,
        "notBilateralAcceptance": True,
        "scope": NOT_BILATERAL_ACCEPTANCE,
        "oracleOwnership": "LOWER_FROZEN_RUNNER_51d73ca098743b25fe074d184904e695af37fd95",
        "permittedFinalState": runner.gates["permittedFinalState"],
        "upperUnderTest": run_a.label.split("@", 1)[-1],
        "upperUnderTestIsReleaseArtifact": config.release_root is not None,
        "artifactRoot": ("<release>" if config.release_root is not None else "<repository>"),
        "scenarioSet": list(forward),
        "gates": gates,
        "blockers": blockers,
        "runs": {
            "forward": _run_summary(run_a),
            "forwardRepeat": _run_summary(run_b),
            "reverse": _run_summary(run_reverse),
        },
        "falsifiers": falsifiers,
        "determinism": determinism,
        "orderPermutation": permutation,
        "unknownRouteTotal": unknown_routes,
        "upper404Total": len(upper_404s),
        "externalCalls": external,
        "comparatorNegativeControl": negative_control,
        "declaredVolatileRule": rule.summary(),
        "volatileRuleConformance": conformance,
        "captureSchemaSatisfiability": satisfiability,
        "captureOmissions": genuine_capture_errors,
        "secretFindings": all_secret_findings,
        "socketAuthorityViolations": all_unexpected_authorities,
        "runErrors": run_errors,
        "runScalarSignals": run_signals,
        "volatileFieldAllowlist": allowlist,
        "observationDeadlineMs": config.observation_deadline_ms,
        "suiteSatisfied": suite_ok,
    }


#: Per-run scalar signals and what "nominal" means for each.  A signal listed
#: here is consumed by UBM-ST-E01 for every run; adding a field to RunOutcome
#: without adding it here makes ``test_every_run_scalar_is_adjudicated`` fail.
RUN_SCALAR_SIGNALS: tuple[tuple[str, Any], ...] = (
    ("childExit", 0),
    # The raw code the stop reported, adjudicated separately from childExit so a
    # synthetic outcome cannot present a clean childExit over a dirty stop.
    ("stopExitCode", 0),
    ("childExitMatchesStopExitCode", True),
    ("stopDisposition", "GRACEFUL"),
    ("signalled", False),
    ("unknownRoutes", 0),
    ("upper404Count", 0),
    # Per-scenario counters must start at zero in EVERY run, not just the
    # forward one: that is the state-leakage check (G-DET-4), and reading it
    # from one run is the same gap the exit code had.
    ("nonZeroStartCounters", 0),
    ("error", None),
)


def _non_zero_start_counters(outcome: RunOutcome) -> int:
    """How many per-scenario counters were non-zero at scenario start."""
    total = 0
    for entry in outcome.scenarios:
        for component in (entry.get("countersAtScenarioStart") or {}).values():
            if isinstance(component, Mapping):
                total += sum(1 for value in component.values() if value)
    return total


def _run_scalars(outcome: RunOutcome) -> dict[str, Any]:
    stop = dict(outcome.stop_signals)
    stop_exit = stop.get("exitCode")
    return {
        "childExit": outcome.child_exit,
        "stopExitCode": stop_exit,
        "childExitMatchesStopExitCode": outcome.child_exit == stop_exit,
        "stopDisposition": stop.get("stopDisposition"),
        "signalled": stop.get("signalled"),
        "unknownRoutes": outcome.unknown_routes,
        "upper404Count": len(outcome.upper_404s),
        "nonZeroStartCounters": _non_zero_start_counters(outcome),
        "error": outcome.error,
        # Reported, not adjudicated: a child may legitimately write to stderr.
        "stderrBytes": stop.get("stderrBytes"),
    }


def _run_scalar_signals(all_runs: Sequence[tuple[str, RunOutcome]]) -> dict[str, Any]:
    observed = {label: _run_scalars(outcome) for label, outcome in all_runs}
    off_nominal = [
        "%s.%s=%r (nominal %r)" % (label, name, values.get(name), nominal)
        for label, values in sorted(observed.items())
        for name, nominal in RUN_SCALAR_SIGNALS
        if values.get(name) != nominal
    ]
    return {
        "adjudicated": [name for name, _nominal in RUN_SCALAR_SIGNALS],
        "runs": observed,
        "offNominal": off_nominal,
    }


def _run_summary(outcome: RunOutcome) -> dict[str, Any]:
    return {
        "label": outcome.label,
        "dispositions": outcome.dispositions,
        "childExit": outcome.child_exit,
        "stopSignals": dict(outcome.stop_signals),
        "unknownRoutes": outcome.unknown_routes,
        "upper404s": outcome.upper_404s,
        "error": outcome.error,
        "scenarios": [
            {key: value for key, value in entry.items() if key != "normalized"}
            for entry in outcome.scenarios
        ],
    }


def _drop_identifier_dependent_digests(document: Any, rule: VolatileRule | None = None,
                                       ) -> tuple[Any, list[str]]:
    """Remove exactly the digest slots the declared rule marks exempt.

    A digest cannot be canonicalised after the fact, so wherever an opaque
    per-run identifier reaches a hashed body or header block the two runs can
    never agree.  The exemption applies only to a capture that actually contains
    such an identifier: an identifier-free scenario (SC-062) keeps every digest
    under byte comparison.  The removed pointers are reported so the blast radius
    of the exemption is visible rather than implied, and the pointer list comes
    from ``release-gates.1.0.0.json`` rather than from this module.
    """
    if not isinstance(document, Mapping):
        return document, []
    result = json.loads(json.dumps(document))
    if rule is None or _ID_TOKEN_MARKER not in json.dumps(result, sort_keys=True):
        return result, []
    exempted: list[str] = []
    for pointer in rule.exempt_pointers:
        exempted.extend(_drop_declared_pointer(result, _pointer_segments(pointer)))
    # A raw digest is excused only where the runtime actually published its
    # canonical companion -- i.e. only where an identifier had been assigned by
    # the time the message was digested.  An exchange that predates any
    # assignment keeps its raw digests under full byte comparison.
    for index, exchange in enumerate(result.get("exchanges", [])):
        for side in ("request", "response"):
            message = exchange.get(side)
            if not isinstance(message, Mapping):
                continue
            for pointer in rule.source_canonical_pointers:
                segments = _pointer_segments(pointer)
                if len(segments) != 4 or segments[2] != side:
                    continue
                companion = _pointer_segments(rule.canonical_companions[pointer])[3]
                if companion in message and message.pop(segments[3], None) is not None:
                    exempted.append("/exchanges/%d/%s/%s" % (index, side, segments[3]))
    return result, exempted


def missing_canonical_companions(document: Any, rule: "VolatileRule") -> list[str]:
    """Where a raw digest was dropped, its canonical companion must exist.

    Otherwise the exemption is a hole: the slot would be excused and nothing
    would take its place.  Reported as a finding the verdict consumes.
    """
    if not isinstance(document, Mapping) or rule is None:
        return []
    missing: list[str] = []
    for index, exchange in enumerate(document.get("exchanges", [])):
        for side in ("request", "response"):
            message = exchange.get(side)
            if not isinstance(message, Mapping):
                continue
            # A companion is required exactly where the message carries an
            # identifier this run assigned.  `bodyCanonicalSha256` is the marker
            # for that: when the runtime published one, every raw digest of the
            # same message must have its companion too.
            if "bodyCanonicalSha256" not in message and \
                    "headerBlockCanonicalSha256" not in message:
                continue
            for pointer in rule.source_canonical_pointers:
                segments = _pointer_segments(pointer)
                if len(segments) != 4 or segments[2] != side:
                    continue
                if segments[3] not in message:
                    continue
                companion = _pointer_segments(rule.canonical_companions[pointer])[3]
                if companion not in message:
                    missing.append("/exchanges/%d/%s/%s (companion %s absent)"
                                   % (index, side, segments[3], companion))
    return sorted(set(missing))


def canonicalisation_footprint(document: Any,
                               identifiers: Sequence[tuple[str, str]]) -> list[str]:
    """Pointers whose value the identifier canonicalisation actually changed.

    Every one of them must be a slot the spec declares ``CANONICALISE``; an
    identifier reaching an undeclared slot means the derivation table is stale
    and ``UBM-ST-R01-DECL`` fails rather than the exemption silently widening.
    """
    if not isinstance(document, Mapping):
        return []
    return _pointer_diff(document, _canonicalise_identifiers(document, identifiers))


def compare_captures(left: Any, right: Any, allowlist: Sequence[str],
                     left_ids: Sequence[tuple[str, str]],
                     right_ids: Sequence[tuple[str, str]],
                     rule: VolatileRule | None = None,
                     ) -> tuple[list[str], list[str]]:
    """Byte-compare two capture documents outside the declared volatile slots."""
    one, exempt_left = _drop_identifier_dependent_digests(
        _canonicalise_identifiers(left, left_ids), rule)
    two, exempt_right = _drop_identifier_dependent_digests(
        _canonicalise_identifiers(right, right_ids), rule)
    differences = [pointer for pointer in _pointer_diff(one, two)
                   if not _matches_allowlist(pointer, allowlist)]
    return differences, sorted(set(exempt_left) | set(exempt_right))


def _compare_runs(left: RunOutcome, right: RunOutcome, allowlist: Sequence[str],
                  rule: VolatileRule, *, order_insensitive: bool = False,
                  supplied: frozenset[str] = frozenset()) -> dict[str, Any]:
    differences: list[str] = []
    exempted: list[str] = []
    left_view = _normalized_scenarios(left)
    right_view = _normalized_scenarios(right)
    left_ids = _assigned_identifiers(left, supplied, rule.server_assigned_roles)
    right_ids = _assigned_identifiers(right, supplied, rule.server_assigned_roles)
    if sorted(left_view) != sorted(right_view):
        differences.append("scenario sets differ")
    for scenario_id in sorted(set(left_view) & set(right_view)):
        one = _canonicalise_identifiers(left_view[scenario_id], left_ids)
        two = _canonicalise_identifiers(right_view[scenario_id], right_ids)
        for pointer in _pointer_diff(one, two):
            differences.append("normalized %s%s" % (scenario_id, pointer))
        capture_differences, capture_exempted = compare_captures(
            left.captures.get(scenario_id, {}), right.captures.get(scenario_id, {}),
            allowlist, left_ids, right_ids, rule)
        exempted.extend("%s%s" % (scenario_id, pointer) for pointer in capture_exempted)
        for pointer in capture_differences:
            differences.append("capture %s%s" % (scenario_id, pointer))
    consistency = (_identifier_consistency(left, supplied)
                   + _identifier_consistency(right, supplied)
                   + unclassified_captured_members(left)
                   + unclassified_captured_members(right)
                   + unattributed_exchanges(left)
                   + unattributed_exchanges(right)
                   + undeclared_identifier_roles(left, rule.role_sources)
                   + undeclared_identifier_roles(right, rule.role_sources))
    return {
        # fail-closed: `identical` is the ONLY thing the gate reads, so it must
        # account for every finding this comparison produces.  An earlier
        # revision reported serverAssignedIdentifierInconsistencies and then
        # ignored them here, which let a run the report itself called
        # inconsistent be judged PASS.
        "identical": not differences and not consistency,
        "findingArrays": ["differences", "serverAssignedIdentifierInconsistencies"],
        "differences": differences,
        "comparedScenarios": sorted(set(left_view) & set(right_view)),
        "orderInsensitive": order_insensitive,
        "serverAssignedIdentifierInconsistencies": consistency,
        "declaredVolatileRule": rule.summary(),
        "deploymentSuppliedValuesExcludedFromCanonicalisation": len(supplied),
        "digestSlotsExempted": sorted(set(exempted)),
    }


def rule_conformance(outcome: RunOutcome, rule: VolatileRule,
                     supplied: frozenset[str] = frozenset()) -> dict[str, Any]:
    """UBM-ST-R01-DECL: every excused slot is one the spec declares.

    Two directions are checked.  Forward: every pointer the canonicalisation
    touched matches a declared ``CANONICALISE`` slot.  Reverse: every declared
    ``EXEMPT_WHEN_REFERENCED`` slot is one this driver actually removed in at
    least one identifier-bearing scenario, so the table cannot accumulate
    exemptions that buy nothing but would excuse a future difference.
    """
    identifiers = _assigned_identifiers(outcome, supplied, rule.server_assigned_roles)
    undeclared: list[str] = []
    touched: set[str] = set()
    applied: set[str] = set()
    for scenario_id, document in sorted(outcome.captures.items()):
        for pointer in canonicalisation_footprint(document, identifiers):
            touched.add("%s%s" % (scenario_id, pointer))
            if not _matches_allowlist(pointer, rule.canonicalise_pointers):
                undeclared.append("%s%s" % (scenario_id, pointer))
        _, removed = _drop_identifier_dependent_digests(
            _canonicalise_identifiers(document, identifiers), rule)
        applied.update(removed)
    unused = [pointer for pointer in rule.exempt_pointers + rule.source_canonical_pointers
              if not any(_matches_allowlist(item, [pointer]) for item in applied)]
    missing_companions: list[str] = []
    for scenario_id, document in sorted(outcome.captures.items()):
        missing_companions.extend(
            "%s%s" % (scenario_id, pointer)
            for pointer in missing_canonical_companions(document, rule))
    return {
        "declaredRule": rule.summary(),
        "missingCanonicalCompanions": missing_companions,
        "canonicalisationTouched": sorted(touched),
        "undeclaredCanonicalisedSlots": undeclared,
        "exemptPointersApplied": sorted(applied),
        "declaredExemptSlotsNeverApplied": unused,
        "conforms": not undeclared and not unused and not missing_companions,
    }


def negative_control_comparison(outcome: RunOutcome, allowlist: Sequence[str],
                                scenario_ids: Sequence[str],
                                rule: VolatileRule,
                                supplied: frozenset[str] = frozenset()) -> dict[str, Any]:
    """UBM-ST-R01-NEG: prove the comparator is not vacuous.

    Several single-value injections are made into slots that the volatile
    allowlist and the identifier-dependent-digest exemption do **not** cover.
    If any of them survives undetected the whole two-run equality argument is
    worthless, so the gate fails.
    """
    identifiers = _assigned_identifiers(outcome, supplied, rule.server_assigned_roles)
    injections: list[dict[str, Any]] = []

    def _inject(scenario_id: str, pointer: str, mutate: Any) -> None:
        original = outcome.captures.get(scenario_id)
        if not original:
            return
        tampered = json.loads(json.dumps(original))
        try:
            mutate(tampered)
        except (KeyError, IndexError, TypeError):
            return
        detected, _ = compare_captures(
            original, tampered, allowlist, identifiers, identifiers, rule)
        injections.append({
            "scenarioId": scenario_id, "pointer": pointer,
            "detectedPointers": detected, "detected": bool(detected)})

    identifier_free = [
        scenario_id for scenario_id in scenario_ids
        if _ID_TOKEN_MARKER not in json.dumps(
            _canonicalise_identifiers(outcome.captures.get(scenario_id, {}), identifiers))
        and outcome.captures.get(scenario_id)
    ]
    any_scenario = next((item for item in scenario_ids if outcome.captures.get(item)), None)
    if any_scenario is not None:
        def _order(document: Any) -> None:
            document["run"]["orderingRule"] += "X"
        _inject(any_scenario, "/run/orderingRule", _order)

        def _status(document: Any) -> None:
            document["exchanges"][0]["response"]["status"] += 1
        _inject(any_scenario, "/exchanges/0/response/status", _status)

        def _count(document: Any) -> None:
            document["exchanges"][0]["request"]["bodyByteCount"] += 1
        _inject(any_scenario, "/exchanges/0/request/bodyByteCount", _count)

        def _writes(document: Any) -> None:
            document["simulatedWrites"]["normalRanWrites"] += 1
        _inject(any_scenario, "/simulatedWrites/normalRanWrites", _writes)
    for scenario_id in identifier_free[:1]:
        def _digest(document: Any) -> None:
            digest = document["exchanges"][0]["response"]["bodyRawSha256"]
            document["exchanges"][0]["response"]["bodyRawSha256"] = "0" + digest[1:]
        _inject(scenario_id, "/exchanges/0/response/bodyRawSha256", _digest)

    # Identifier-BEARING scenarios used to be blind to a digest-only tamper,
    # because the whole digest slot was excluded.  The runtime now publishes an
    # identifier-canonical companion, so the same injection must be caught here
    # too -- that is the whole point of canonicalising instead of excluding.
    identifier_bearing = [scenario_id for scenario_id in scenario_ids
                          if scenario_id not in identifier_free
                          and outcome.captures.get(scenario_id)]
    for scenario_id in identifier_bearing[:1]:
        def _canonical_body(document: Any) -> None:
            for exchange in document["exchanges"]:
                message = exchange.get("response") or {}
                if "bodyCanonicalSha256" in message:
                    digest = message["bodyCanonicalSha256"]
                    message["bodyCanonicalSha256"] = "0" + digest[1:]
                    return
            raise KeyError("no canonical body digest to tamper with")
        _inject(scenario_id, "/exchanges/*/response/bodyCanonicalSha256",
                _canonical_body)

        def _canonical_headers(document: Any) -> None:
            for exchange in document["exchanges"]:
                message = exchange.get("request") or {}
                if "headerBlockCanonicalSha256" in message:
                    digest = message["headerBlockCanonicalSha256"]
                    message["headerBlockCanonicalSha256"] = "0" + digest[1:]
                    return
            raise KeyError("no canonical header digest to tamper with")
        _inject(scenario_id, "/exchanges/*/request/headerBlockCanonicalSha256",
                _canonical_headers)

    return {
        "performed": bool(injections),
        "identifierFreeScenarios": identifier_free,
        "identifierBearingScenarios": identifier_bearing,
        "injections": injections,
        "comparatorIsFalsifiable": bool(injections) and all(
            item["detected"] for item in injections),
    }


def undeclared_identifier_roles(outcome: "RunOutcome",
                                declared: Mapping[str, str]) -> list[str]:
    """Roles observed in a capture that the spec does not classify.

    Fail-closed: an unclassified role is never canonicalised, and saying so is
    what keeps "we could not classify it" from looking like "there was nothing
    to classify".
    """
    findings: list[str] = []
    for scenario_id, document in sorted(outcome.captures.items()):
        seen: set[str] = set()
        for exchange in document.get("exchanges", []):
            for key in (exchange.get("capturedOutputs") or {}):
                if key in _ASSIGNED_MEMBERS:
                    seen.add(_identifier_role(key, exchange))
        for role in sorted(seen - set(declared)):
            findings.append("%s: identifier role %s has no declared source"
                            % (scenario_id, role))
    return findings


def _identifier_consistency(outcome: RunOutcome,
                            supplied: frozenset[str] = frozenset()) -> list[str]:
    """An identifier must be single-valued within its role, inside one run.

    Roles are ``<member>@<originator>`` (see :func:`_identifier_role`), so a
    resource the counterpart created and one the upper created for itself are
    not confused with each other.  Two values in the same role remain a
    finding, and the finding is now consumed by ``UBM-ST-R01`` rather than
    merely reported.
    """
    findings: list[str] = []
    for scenario_id, document in sorted(outcome.captures.items()):
        seen: dict[str, set[str]] = {}
        for exchange in document.get("exchanges", []):
            for key, value in (exchange.get("capturedOutputs") or {}).items():
                if key in _ASSIGNED_MEMBERS and isinstance(value, str) \
                        and value not in supplied:
                    seen.setdefault(_identifier_role(key, exchange), set()).add(value)
        for role, values in sorted(seen.items()):
            if len(values) > 1:
                findings.append("%s: %s appears with %d distinct values"
                                % (scenario_id, role, len(values)))
    return findings
