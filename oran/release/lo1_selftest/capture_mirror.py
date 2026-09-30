"""Structural mirror of the capture document, written by the self-test.

``work-split.1.0.0.json#/owners/W3-SELFTEST/importRule`` forbids importing the
runtime, so the self-test carries its own assembler for
``capture-schema.2.0.0.json`` rather than calling W2's ``CaptureRecorder``.  The
schema is the shared seam **S1**; this module targets those bytes exactly and
validates against them before writing.

**What this module is not.**  It is not the release runtime and never claims to
be.  The O1 sub-graph it records -- notifications, retrievals, normalization,
raw artefacts, the external-target ledger, cleanup and the redaction scan --
comes from a real conversation with the Provider emulator over real sockets.
The core-runtime sub-graph -- ``exchanges``, ``coordinator``,
``deterministicStubs`` and ``state`` -- has no producer until W1/W2 land, so its
origin is declared explicitly in ``core_sections_origin`` and repeated in the
bundle's sidecar report.  A bundle whose core sections are
``SYNTHETIC_FALSIFIER_SUBSTRATE`` exists to feed the falsifiers and the
independent verifier; it is never evidence that a run happened.
"""

from __future__ import annotations

import datetime as _datetime
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from oran.contract.jcs import jcs_sha256

from . import SELF_TEST_STATE_LABEL
from .frozen import FrozenBundle, sha256_bytes

CAPTURE_SCHEMA_VERSION = "oran-aic-upper-live-o1-harness-capture/2.0.0"
REDACTION_POLICY = "oran-aic-upper-live-o1-harness-redaction/1.0.0"

CORE_FROM_RUNTIME = "RUNTIME_UNDER_TEST"
CORE_SYNTHETIC = "SYNTHETIC_FALSIFIER_SUBSTRATE"

PACKAGED_RELEASE_IDENTITY = "PACKAGED_RELEASE"
SOURCE_TREE_IDENTITY = "SOURCE_TREE_DEVELOPMENT"
RELEASE_ID = "upper-live-o1-harness"
DEVELOPMENT_RELEASE_ID = "upper-live-o1-harness-development-self-test"
RELEASE_VERSION = "1.0.9"

CREDENTIAL_MARKERS = (
    b"-----BEGIN", b"Bearer ", b"password=", b"secret=", b"api-key=", b"apikey=",
)
#: A private-key BLOCK, not merely the words that name one.  The scanners in
#: this package quote these patterns, so a marker that matched its own source
#: would make every scan report a false hit and hide a real one.
PRIVATE_KEY_BEGIN = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
PRIVATE_KEY_END = re.compile(rb"-----END [A-Z0-9 ]*PRIVATE KEY-----")


def contains_private_key_block(blob: bytes) -> bool:
    """True only for a complete PEM/OpenSSH private-key block."""
    return bool(PRIVATE_KEY_BEGIN.search(blob)) and bool(PRIVATE_KEY_END.search(blob))


def count_private_key_blocks(blob: bytes) -> int:
    return len(PRIVATE_KEY_BEGIN.findall(blob)) if contains_private_key_block(blob) else 0


class CaptureMirrorError(RuntimeError):
    """The capture could not be assembled against the frozen schema."""


def instant(moment: _datetime.datetime | None = None) -> str:
    moment = moment or _datetime.datetime.now(_datetime.timezone.utc)
    return moment.astimezone(_datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_revisions(repo_root: Path) -> dict[str, str]:
    """Read packaged provenance in release mode; use git only for source development."""
    provenance_path = os.environ.get("LO1_ARTIFACT_PROVENANCE")
    if provenance_path:
        path = Path(provenance_path).resolve()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CaptureMirrorError(f"packaged artifact provenance unreadable: {exc}") from exc
        commit = str(document.get("sourceCommit", ""))
        tree = str(document.get("sourceTree", ""))
        if not re.fullmatch(r"[a-f0-9]{40}", commit) or not re.fullmatch(r"[a-f0-9]{40}", tree):
            raise CaptureMirrorError("packaged artifact provenance has malformed git object ids")
        return {"commit": commit, "tree": tree}

    def _run(*args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True, text=True, check=False)
        value = completed.stdout.strip()
        if completed.returncode != 0 or not re.fullmatch(r"[a-f0-9]{40}", value):
            raise CaptureMirrorError(
                "the self-test needs real git object ids for /revisions and refuses "
                f"to fabricate one: git {' '.join(args)} -> {completed.stderr.strip()!r}")
        return value

    return {"commit": _run("rev-parse", "HEAD"), "tree": _run("rev-parse", "HEAD^{tree}")}


def _git_tree_dirty(repo_root: Path) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain",
         "--untracked-files=all"],
        capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise CaptureMirrorError(
            "the development self-test could not determine whether its source "
            f"tree is dirty: {completed.stderr.strip()!r}")
    return bool(completed.stdout)


def _json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureMirrorError(f"{label} is not valid JSON") from exc
    if not isinstance(document, dict):
        raise CaptureMirrorError(f"{label} is not a JSON object")
    return document


def _sha256_value(value: Any, *, label: str, prefixed: bool = False) -> str:
    text = str(value)
    pattern = r"sha256:[a-f0-9]{64}" if prefixed else r"[a-f0-9]{64}"
    if re.fullmatch(pattern, text) is None:
        raise CaptureMirrorError(f"{label} is not a {pattern} digest")
    return text


def _git_oid(value: Any, *, label: str) -> str:
    text = str(value)
    if re.fullmatch(r"[a-f0-9]{40}", text) is None:
        raise CaptureMirrorError(f"{label} is not a git object id")
    return text


@dataclass(frozen=True)
class UpperIdentity:
    """Identity facts used in a capture, plus the raw bytes that prove them."""

    revisions: Mapping[str, Any]
    artifact_provenance_raw: bytes | None = None
    release_manifest_raw: bytes | None = None

    def capture_record(self, raw_store: "RawStoreMirror") -> dict[str, Any]:
        result = dict(self.revisions)
        if self.revisions["identityMode"] == PACKAGED_RELEASE_IDENTITY:
            if self.artifact_provenance_raw is None or self.release_manifest_raw is None:
                raise CaptureMirrorError(
                    "packaged release identity has no raw provenance/manifest bytes")
            result["artifactProvenanceRaw"] = raw_store.put(
                self.artifact_provenance_raw, kind="release",
                name="ARTIFACT-PROVENANCE.json", media_type="application/json")
            result["releaseManifestRaw"] = raw_store.put(
                self.release_manifest_raw, kind="release",
                name="RELEASE-MANIFEST.json", media_type="application/json")
        else:
            result["artifactProvenanceRaw"] = None
            result["releaseManifestRaw"] = None
        return result


def resolve_upper_identity(
        repo_root: Path, *, release_archive_sha256: str | None = None,
        environ: Mapping[str, str] | None = None) -> UpperIdentity:
    """Resolve a real packaged identity or an explicit development identity.

    Release mode never consults git and never manufactures a digest.  It reads
    the exact packaged ``ARTIFACT-PROVENANCE.json`` and
    ``RELEASE-MANIFEST.json`` bytes, verifies their cross-bindings, and requires
    the raw archive digest to arrive out of band.  Source-tree mode does the
    opposite: it records the git base and dirtiness, but every release-only
    field is ``null`` so a development run cannot masquerade as a release.
    """
    environment = dict(os.environ if environ is None else environ)
    root_value = environment.get("LO1_RELEASE_ROOT")
    provenance_value = environment.get("LO1_ARTIFACT_PROVENANCE")
    apparent_root = Path(root_value).resolve() if root_value else Path(repo_root).resolve()
    packaged = bool(root_value or provenance_value)

    if not packaged:
        revisions = git_revisions(Path(repo_root))
        return UpperIdentity(revisions={
            "identityMode": SOURCE_TREE_IDENTITY,
            "upperReleaseId": DEVELOPMENT_RELEASE_ID,
            "upperReleaseVersion": None,
            "upperReleaseContentSha256": None,
            "upperReleaseManifestSha256": None,
            "upperDeliveryArchiveSha256": None,
            "upperSourceCommit": revisions["commit"],
            "upperSourceTree": revisions["tree"],
            "testedCodeCommit": None,
            "upperOciImageManifestDigest": None,
            "sourceTreeDirty": _git_tree_dirty(Path(repo_root)),
        })

    provenance_path = (
        Path(provenance_value).resolve() if provenance_value
        else apparent_root / "ARTIFACT-PROVENANCE.json")
    manifest_path = apparent_root / "RELEASE-MANIFEST.json"
    if provenance_path != apparent_root / "ARTIFACT-PROVENANCE.json":
        raise CaptureMirrorError(
            "LO1_ARTIFACT_PROVENANCE must name ARTIFACT-PROVENANCE.json in "
            "LO1_RELEASE_ROOT")
    for path, label in ((provenance_path, "ARTIFACT-PROVENANCE.json"),
                        (manifest_path, "RELEASE-MANIFEST.json")):
        if path.is_symlink() or not path.is_file():
            raise CaptureMirrorError(f"packaged {label} is absent or not regular")

    provenance_raw = provenance_path.read_bytes()
    manifest_raw = manifest_path.read_bytes()
    provenance = _json_object(provenance_raw, label="ARTIFACT-PROVENANCE.json")
    manifest = _json_object(manifest_raw, label="RELEASE-MANIFEST.json")
    if (provenance.get("releaseId") != RELEASE_ID
            or manifest.get("releaseId") != RELEASE_ID
            or provenance.get("releaseVersion") != RELEASE_VERSION
            or manifest.get("releaseVersion") != RELEASE_VERSION):
        raise CaptureMirrorError(
            "packaged self-test accepts only upper-live-o1-harness/1.0.9")
    if (provenance.get("sourceIntegrity") != "FULLY_INTEGRATED"
            or manifest.get("sourceIntegrity") != "FULLY_INTEGRATED"
            or provenance.get("payloadBoundToSourceTree") is not True
            or (manifest.get("provenance") or {}).get(
                "payloadBoundToSourceTree") is not True):
        raise CaptureMirrorError(
            "packaged self-test refuses a development or source-unbound release")

    manifest_provenance = manifest.get("provenance") or {}
    manifest_revisions = manifest.get("revisions") or {}
    source_commit = _git_oid(
        provenance.get("sourceCommit"), label="artifact provenance sourceCommit")
    source_tree = _git_oid(
        provenance.get("sourceTree"), label="artifact provenance sourceTree")
    if (manifest_provenance.get("sourceCommit") != source_commit
            or manifest_provenance.get("sourceTree") != source_tree
            or manifest_revisions.get("upperSourceCommit") != source_commit
            or manifest_revisions.get("upperSourceTree") != source_tree
            or manifest_revisions.get("testedCodeCommit") != source_commit):
        raise CaptureMirrorError(
            "artifact provenance and release manifest source identity disagree")

    files = manifest.get("files")
    if not isinstance(files, dict) or files.get("ARTIFACT-PROVENANCE.json") != \
            sha256_bytes(provenance_raw):
        raise CaptureMirrorError(
            "release manifest does not bind the artifact provenance bytes")
    measured_files: dict[str, str] = {}
    for name, declared_digest in sorted(files.items()):
        member = PurePosixPath(str(name))
        if (member.is_absolute() or member.as_posix() != name
                or any(part in {"", ".", ".."} for part in member.parts)
                or re.fullmatch(r"[a-f0-9]{64}", str(declared_digest)) is None):
            raise CaptureMirrorError(f"unsafe or malformed manifest member: {name!r}")
        path = apparent_root.joinpath(*member.parts)
        if path.is_symlink() or not path.is_file():
            raise CaptureMirrorError(f"manifest member is absent or not regular: {name}")
        actual_digest = sha256_bytes(path.read_bytes())
        if actual_digest != declared_digest:
            raise CaptureMirrorError(f"manifest member digest mismatch: {name}")
        measured_files[name] = actual_digest
    measured_content = sha256_bytes("".join(
        f"{digest}  {name}\n" for name, digest in sorted(measured_files.items())
    ).encode("utf-8"))
    content_digest = _sha256_value(
        manifest.get("releaseContentSha256"), label="release content")
    if (manifest_revisions.get("upperReleaseContentSha256") != content_digest
            or measured_content != content_digest):
        raise CaptureMirrorError(
            "release manifest content digest fields disagree")
    oci = manifest_provenance.get("oci") or {}
    oci_digest = _sha256_value(
        oci.get("imageManifestDigest"), label="OCI image manifest", prefixed=True)
    if manifest_revisions.get("upperOciImageManifestDigest") != oci_digest:
        raise CaptureMirrorError("release manifest OCI digest fields disagree")
    oci_blob = apparent_root / "oci" / "blobs" / "sha256" / oci_digest[7:]
    if (oci_blob.is_symlink() or not oci_blob.is_file()
            or sha256_bytes(oci_blob.read_bytes()) != oci_digest[7:]):
        raise CaptureMirrorError(
            "the OCI manifest digest is not bound to its packaged blob bytes")
    archive_digest = _sha256_value(
        release_archive_sha256
        or environment.get("LO1_RELEASE_ARCHIVE_SHA256"),
        label="out-of-band release archive")

    return UpperIdentity(
        revisions={
            "identityMode": PACKAGED_RELEASE_IDENTITY,
            "upperReleaseId": RELEASE_ID,
            "upperReleaseVersion": RELEASE_VERSION,
            "upperReleaseContentSha256": content_digest,
            "upperReleaseManifestSha256": sha256_bytes(manifest_raw),
            "upperDeliveryArchiveSha256": archive_digest,
            "upperSourceCommit": source_commit,
            "upperSourceTree": source_tree,
            "testedCodeCommit": source_commit,
            "upperOciImageManifestDigest": oci_digest,
            "sourceTreeDirty": False,
        },
        artifact_provenance_raw=provenance_raw,
        release_manifest_raw=manifest_raw,
    )


class RawStoreMirror:
    """Writes wire bytes verbatim under the capture root BEFORE anything parses them."""

    def __init__(self, capture_root: Path) -> None:
        self.capture_root = Path(capture_root)
        self.written: list[dict[str, Any]] = []

    def put(self, raw: bytes, *, kind: str, name: str,
            media_type: str | None = None) -> dict[str, Any]:
        if kind not in ("o1", "netconf", "notify", "release"):
            raise CaptureMirrorError(f"raw artefact kind {kind!r} is not in the schema")
        safe = re.sub(r"[^A-Za-z0-9._-]", "-", name)
        directory = self.capture_root / "raw" / kind
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / safe
        target.write_bytes(raw)
        record = {
            "byteCount": len(raw),
            "sha256": sha256_bytes(raw),
            "artifactPath": f"raw/{kind}/{safe}",
            "mediaType": media_type,
            "truncated": False,
        }
        self.written.append(record)
        return record

    def scan(self) -> dict[str, int]:
        files = 0
        credential_hits = 0
        key_hits = 0
        root = self.capture_root / "raw"
        for path in sorted(root.rglob("*")) if root.is_dir() else []:
            if not path.is_file():
                continue
            files += 1
            blob = path.read_bytes()
            credential_hits += sum(1 for marker in CREDENTIAL_MARKERS if marker in blob)
            key_hits += count_private_key_blocks(blob)
        return {
            "filesScanned": files,
            "credentialMarkerHits": credential_hits,
            "privateKeyBlockHits": key_hits,
        }


@dataclass
class CaptureMirror:
    """Assembles one capture document against the frozen schema."""

    bundle: FrozenBundle
    repo_root: Path
    capture_root: Path
    run_id: str
    scenario_id: str
    core_sections_origin: str = CORE_SYNTHETIC
    raw_store: RawStoreMirror | None = None

    started_at: str = field(default_factory=instant)
    monotonic_origin_ns: int = field(default_factory=time.monotonic_ns)
    sequence: int = 0

    notifications: list[dict[str, Any]] = field(default_factory=list)
    retrievals: list[dict[str, Any]] = field(default_factory=list)
    normalization_records: list[dict[str, Any]] = field(default_factory=list)
    harness_operations: list[dict[str, Any]] = field(default_factory=list)
    netconf_rpcs: list[dict[str, Any]] = field(default_factory=list)
    netconf_session: dict[str, Any] | None = None
    netconf_measured_segment: dict[str, Any] | None = None
    netconf_readiness: dict[str, Any] | None = None
    netconf_perf_metric_job: dict[str, Any] | None = None
    cleanup_actions: list[dict[str, Any]] = field(default_factory=list)
    cleanup_residual: list[str] = field(default_factory=list)
    cleanup_path: str = "NORMAL"
    retry_attempts: list[dict[str, Any]] = field(default_factory=list)
    target_ledger: dict[str, dict[str, Any]] = field(default_factory=dict)
    guard_methods: tuple[str, ...] = (
        "SOCKET_CONNECT", "SUBPROCESS_SPAWN", "SSH_TRANSPORT_OPEN", "SFTP_CLIENT_OPEN")
    guard_violations: list[dict[str, Any]] = field(default_factory=list)
    hardware_calls: int = 0
    secret_refs_resolved: list[str] = field(default_factory=list)
    unresolved_secret_refs: list[str] = field(default_factory=list)
    parser_invocations: int = 0
    hidden_sleep_count: int = 0
    disposition: str = "COMPLETED"
    gate_result: str = "ADMITTED"
    gate_checks: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.raw_store is None:
            self.raw_store = RawStoreMirror(self.capture_root)

    def next_sequence(self) -> int:
        value = self.sequence
        self.sequence += 1
        return value

    def monotonic_offset_ms(self) -> int:
        return max(0, (time.monotonic_ns() - self.monotonic_origin_ns) // 1_000_000)

    # --------------------------------------------------------------- emitters

    def emit_harness_operation(self, *, component: str, op: str, allowed: bool,
                               argument_keys: Sequence[str], http_status: int,
                               event: str, output_keys: Sequence[str] = ()) -> int:
        sequence = self.next_sequence()
        self.harness_operations.append({
            "sequence": sequence,
            "observedAt": instant(),
            "component": component,
            "op": op,
            "allowed": allowed,
            "argumentKeys": list(argument_keys),
            "httpStatus": http_status,
            "event": event,
            "outputKeys": list(output_keys),
        })
        return sequence

    def note_target(self, authority: str, *, protocol: str, role: str,
                    allowed: bool) -> None:
        entry = self.target_ledger.get(authority)
        now = instant()
        if entry is None:
            self.target_ledger[authority] = {
                "authority": authority, "protocol": protocol, "role": role,
                "allowed": allowed, "callCount": 1, "firstAt": now, "lastAt": now,
            }
        else:
            entry["callCount"] += 1
            entry["lastAt"] = now

    # -------------------------------------------------------------- assembly

    def _oracle_source(self) -> dict[str, Any]:
        return {
            "origin": "SCENARIO_CATALOG_1_0_1",
            "expectedPointer": self.bundle.expected_pointer(self.scenario_id),
            "rulesPointer": self.bundle.rules_pointer(self.scenario_id),
            "assignmentPointer": self.bundle.assignment_pointer(self.scenario_id),
            "valuesCopiedIntoThisDocument": 0,
        }

    def _contract(self) -> dict[str, Any]:
        digests = self.bundle.contract_digests()
        handoff = self.repo_root / "contracts" / "oran-aic" / "1.0.1" / \
            "handoff-manifest.1.0.1.json"
        repository = self.repo_root / "contracts" / "oran-aic" / "1.0.1" / \
            "shared-contract-bundle"
        differing = 0
        for path in sorted(repository.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(repository)
            packaged = self.bundle.root / relative
            if not packaged.is_file() or packaged.read_bytes() != path.read_bytes():
                differing += 1
        return {
            "contractProfile": "oran-aic/1.0.1",
            "correctedHandoff": "1.0.1",
            "handoffManifestSha256": sha256_bytes(handoff.read_bytes()),
            "frozenBundleDiffFileCount": differing,
            **digests,
        }

    def _scenario(self) -> dict[str, Any]:
        scenario = self.bundle.scenario(self.scenario_id)
        assignment = self.bundle.scenario_assignment(self.scenario_id)
        return {
            "scenarioId": self.scenario_id,
            "catalogPointer": self.bundle.catalog_pointer(self.scenario_id),
            "expectedPointer": self.bundle.expected_pointer(self.scenario_id),
            "rulesPointer": self.bundle.rules_pointer(self.scenario_id),
            "fixtureMode": scenario["fixtureMode"],
            "executionProfile": assignment["executionProfile"],
            "counterpartProvisioning": assignment["counterpartProvisioning"],
            "declaredFaultCount": len(scenario["materialization"].get("faults", [])),
            "declaredStepCount": len(self.bundle.steps(self.scenario_id)),
            "declaredRuleCount": len(self.bundle.declared_rules(self.scenario_id)),
        }

    def _applied_rules(self) -> list[dict[str, Any]]:
        declared = self.bundle.declared_rules(self.scenario_id)
        records = []
        for rule_id in declared:
            records.append({
                "ruleId": rule_id,
                "declaredInCatalog": True,
                "inputs": self._rule_inputs(rule_id),
                "observations": self._rule_observations(rule_id),
            })
        return records

    def _rule_inputs(self, rule_id: str) -> list[str]:
        mapping = {
            "RULE-R1-CORRELATION": ["/exchanges/*/correlationId", "/exchanges/*/idempotencyKey"],
            "RULE-R1-DME-PUSH-BINDING": ["/exchanges/*/bindings", "/state/after/acceptedPushPayloadCounts"],
            "RULE-O1-NOTIFY-AND-RETRIEVAL": ["/notifications/*", "/retrievals/*/sourceOfLocation"],
            "RULE-O1-LIVE-VALUE-INVARIANTS": [
                "/normalization/records/*/measurementName", "/normalization/records/*/value",
                "/normalization/records/*/unit", "/normalization/records/*/aggregation"],
            "RULE-O1-DN-BIJECTION": [
                "/normalization/records/*/measuredObjectDn",
                "/normalization/records/*/resolvedCellId",
                "/normalization/records/*/dnBijective"],
            "RULE-O1-TIME-RELATIONS": [
                "/normalization/records/*/window", "/normalization/records/*/observedAt",
                "/normalization/records/*/readyAt", "/normalization/records/*/retrievedAt"],
            "RULE-O1-FILE-TEMPORAL-ORDER": [
                "/notifications/*/eventTime", "/notifications/*/fileInfoList",
                "/retrievals/*/completedAt"],
            "RULE-O1-QUALITY-PRECEDENCE": [
                "/normalization/records/*/quality",
                "/normalization/records/*/ambiguityReason",
                "/normalization/records/*/commitEligible"],
            "RULE-O1-RAW-DIGEST-SELF-CONSISTENT": [
                "/retrievals/*/byteSha256", "/normalization/records/*/sourceFileSha256"],
        }
        return mapping.get(rule_id, [])

    def _rule_observations(self, rule_id: str) -> dict[str, Any]:
        if rule_id == "RULE-O1-LIVE-VALUE-INVARIANTS":
            return {
                "measurementNames": [r["measurementName"] for r in self.normalization_records],
                "values": [r["value"] for r in self.normalization_records],
                "units": [r["unit"] for r in self.normalization_records],
                "aggregations": [r["aggregation"] for r in self.normalization_records],
            }
        if rule_id == "RULE-O1-DN-BIJECTION":
            return {
                "measuredObjectDns": [r["measuredObjectDn"] for r in self.normalization_records],
                "resolvedCellIds": [r["resolvedCellId"] for r in self.normalization_records],
                "bijective": [r["dnBijective"] for r in self.normalization_records],
            }
        if rule_id == "RULE-O1-TIME-RELATIONS":
            return {
                "windowStarts": [r["window"]["start"] for r in self.normalization_records],
                "windowEnds": [r["window"]["end"] for r in self.normalization_records],
                "measurementAgeMs": [r["measurementAgeMs"] for r in self.normalization_records],
                "ingestLatencyMs": [r["ingestLatencyMs"] for r in self.normalization_records],
            }
        if rule_id == "RULE-O1-FILE-TEMPORAL-ORDER":
            return {
                "eventTimes": [n.get("eventTime") for n in self.notifications],
                "fileReadyTimes": [
                    info["fileReadyTime"] for n in self.notifications
                    for info in n.get("fileInfoList", [])],
                "fileExpirationTimes": [
                    info["fileExpirationTime"] for n in self.notifications
                    for info in n.get("fileInfoList", [])],
                "retrievalCompletedAt": [r["completedAt"] for r in self.retrievals],
            }
        if rule_id == "RULE-O1-QUALITY-PRECEDENCE":
            return {
                "qualities": [r["quality"] for r in self.normalization_records],
                "ambiguityReasons": [r.get("ambiguityReason") for r in self.normalization_records],
                "commitEligible": [r["commitEligible"] for r in self.normalization_records],
            }
        if rule_id == "RULE-O1-RAW-DIGEST-SELF-CONSISTENT":
            return {
                "retrievalDigests": [r["byteSha256"] for r in self.retrievals],
                "recordSourceDigests": [
                    r["sourceFileSha256"] for r in self.normalization_records],
            }
        if rule_id == "RULE-O1-NOTIFY-AND-RETRIEVAL":
            return {
                "responseStatuses": [n["responseStatus"] for n in self.notifications],
                "accepted": [n["accepted"] for n in self.notifications],
                "subscriptionIdMatched": [n["subscriptionIdMatched"] for n in self.notifications],
                "sourceOfLocation": [
                    r.get("sourceOfLocation") for r in self.retrievals],
            }
        return {"recordCount": len(self.normalization_records)}

    def _sequence_high_water_mark(
            self, core_sections: Mapping[str, Any]) -> int:
        """Exact maximum over every sequence-bearing capture collection."""
        collections: list[Sequence[Mapping[str, Any]]] = [
            list(core_sections.get("exchanges", [])),
            self.harness_operations,
            self.netconf_rpcs,
            self.notifications,
            self.retrievals,
        ]
        stubs = core_sections.get("deterministicStubs", {})
        if isinstance(stubs, Mapping):
            for name in ("kpmSnapshots", "controlResults", "readbacks",
                         "writeLedger"):
                value = stubs.get(name, [])
                if isinstance(value, list):
                    collections.append(value)
        sequences = [
            int(record["sequence"])
            for collection in collections for record in collection
            if isinstance(record, Mapping) and "sequence" in record
        ]
        return max(sequences, default=0)

    # ------------------------------------------------------------------ build

    def build(
        self,
        *,
        vector: Mapping[str, Any],
        vector_sha256: str,
        provider_identity: Mapping[str, Any],
        authority_record_digest: str,
        execution_window: tuple[str, str],
        core_sections: Mapping[str, Any],
        origin_ownership: Mapping[str, str],
        allowlist: Sequence[str],
        upper_identity: UpperIdentity,
    ) -> dict[str, Any]:
        if self.raw_store is None:  # pragma: no cover - __post_init__ invariant
            raise CaptureMirrorError("capture raw store was not initialized")
        revision_record = upper_identity.capture_record(self.raw_store)
        raw_store_scan = self.raw_store.scan()
        document: dict[str, Any] = {
            "schemaVersion": CAPTURE_SCHEMA_VERSION,
            "captureId": str(uuid.uuid4()),
            "notAVerdict": True,
            "oracleOwnership": self.bundle.scenario_assignment(
                self.scenario_id)["evidenceProducer"],
            "oracleSource": self._oracle_source(),
            "profile": {
                "executionProfile": self.bundle.execution_profile(self.scenario_id),
                "counterpartKind": "UPPER_ARTIFACT_SELF_TEST_HARNESS",
                "providerKind": "CONTRACT_FAITHFUL_EMULATOR",
                "emulatorInUse": True,
                "selfTestLabel": SELF_TEST_STATE_LABEL,
            },
            "run": {
                "runId": self.run_id,
                "scenarioId": self.scenario_id,
                "startedAt": self.started_at,
                "endedAt": instant(),
                "sequenceHighWaterMark": self._sequence_high_water_mark(
                    core_sections),
                "orderingRule": "MONOTONIC_OBSERVED_THEN_ARRAY_ORDER",
                "stepIndexBase": 0,
                "executionWindow": {
                    "approvedNotBefore": execution_window[0],
                    "approvedNotAfter": execution_window[1],
                    "withinWindow": True,
                },
                "disposition": self.disposition,
            },
            "revisions": revision_record,
            "contract": self._contract(),
            "provider": dict(provider_identity),
            "authority": {
                "gateSpecSha256": sha256_bytes((
                    (self.repo_root / "spec") if (self.repo_root / "spec").is_dir()
                    else (self.repo_root / "docs" / "upper-live-o1-harness")
                ).joinpath("identity-authority-gate.1.0.0.json").read_bytes()),
                "gateResult": self.gate_result,
                "checks": self.gate_checks,
                "scope": {
                    "scenarioScope": self.scenario_id,
                    "o1Only": True,
                    "liveE2Control": False,
                    "liveRanWrite": False,
                    "otaTransmission": False,
                    "usrpRadio": False,
                    "upperProductionDeployment": False,
                    "peerInternalSourceImport": False,
                },
                "deploymentVectorDigest": vector_sha256,
                "authorityRecordDigest": authority_record_digest,
                "selfIssued": False,
            },
            "deployment": {
                "vectorVersion": vector["vectorVersion"],
                "vectorSha256": vector_sha256,
                "bindingDocSha256": sha256_bytes(
                    json.dumps(dict(origin_ownership), sort_keys=True).encode()),
                "placeholderFree": True,
                "resolvedRoots": {
                    "r1ApiRoot": vector["r1"]["apiRoot"],
                    "rAppCallbackRoot": vector["r1"]["callbackApi"]["rootUri"],
                    "a1ApiRoot": vector["a1"]["apiRoot"],
                    "a1StatusCallbackRoot": vector["a1"]["statusCallbackRoot"],
                    "policyEvidencePushBaseUri":
                        vector["r1"]["dme"]["policyEvidencePushBaseUri"],
                    "mnsRoot": vector["o1"]["fileDataReporting"]["mnsRoot"],
                    "o1ConsumerRoot":
                        vector["o1"]["fileDataReporting"]["consumerReference"],
                    "netconfEndpoint": vector["o1"]["netconf"]["endpoint"],
                    "sftpAuthorities": list(vector["o1"]["sftp"]["allowedAuthorities"]),
                },
                "originOwnership": dict(origin_ownership),
                "secretRefsResolved": sorted(set(self.secret_refs_resolved)),
                "unresolvedSecretRefs": sorted(set(self.unresolved_secret_refs)),
            },
            "scenario": self._scenario(),
            "clock": {
                "mode": "LIVE_OBSERVED",
                "source": "SYSTEM_UTC",
                "monotonicOriginNs": self.monotonic_origin_ns,
                "observedStart": self.started_at,
                "observedEnd": instant(),
                "skewProbes": [],
                "orderingViolations": [],
                "hiddenSleepCount": self.hidden_sleep_count,
            },
            "exchanges": list(core_sections.get("exchanges", [])),
            "harnessOperations": self.harness_operations,
            "netconf": {
                "enabled": bool(self.netconf_rpcs),
                "assignment": {
                    "adapterActionsAssignedToUpper": [
                        "LOAD_O1_PROFILE",
                        "INSTALL_O1_SUBSCRIPTION",
                        "SET_PERF_METRIC_JOB",
                    ],
                    "authorityRecordDigest": authority_record_digest,
                    "roleTablePointer": "/initialStates",
                },
                "session": self.netconf_session,
                "rpcs": self.netconf_rpcs,
                "measuredSegment": self.netconf_measured_segment,
                "perfMetricJob": self.netconf_perf_metric_job,
                "readiness": self.netconf_readiness,
            },
            "notifications": self.notifications,
            "retrievals": self.retrievals,
            "normalization": {
                "invocations": 1 if self.normalization_records else 0,
                "parserInvocations": self.parser_invocations,
                "commitEligibleCount": sum(
                    1 for record in self.normalization_records if record["commitEligible"]),
                "rejectedCount": sum(
                    1 for record in self.normalization_records
                    if not record["commitEligible"]),
                "duplicateCount": 0,
                "records": self.normalization_records,
            },
            "state": core_sections["state"],
            "coordinator": core_sections["coordinator"],
            "deterministicStubs": core_sections["deterministicStubs"],
            "cleanup": {
                "path": self.cleanup_path,
                "complete": not self.cleanup_residual,
                "actions": self.cleanup_actions,
                "residual": list(self.cleanup_residual),
            },
            "retries": {
                "policy": "no-retry-on-the-live-capture-path",
                "attempts": self.retry_attempts,
                "idempotencyKeysUsed": [],
                "duplicateSuppressions": 0,
            },
            "externalCalls": {
                "targetLedger": list(self.target_ledger.values()),
                "hardwareCalls": self.hardware_calls,
                # Counted from the recorded violations rather than declared, so
                # the mirror cannot report a number its own ledger contradicts.
                # HARDWARE_CONTROL and EXTERNAL_LIVE_TARGET partition the
                # refused attempts, which is why these are two counters.
                "externalLiveTargetCalls": sum(
                    1 for item in self.guard_violations
                    if str(item.get("classification")) == "EXTERNAL_LIVE_TARGET"),
                "forbiddenEgressAttempts": len(self.guard_violations),
                "attemptedAuthorities": sorted(self.target_ledger),
                "authorityAllowlist": list(allowlist),
                "guardInstalled": True,
                "guardArmedNow": True,
                "guardMethods": list(self.guard_methods),
                "scope": "scenario",
                "hardwareDefinitionSource":
                    "docs/upper-live-o1-harness/release-gates.1.0.0.json"
                    "#/hardwareControlSurfaces",
                "connectionAttempts": sum(
                    entry["callCount"] for entry in self.target_ledger.values()),
                "approvedConnectionAttempts": sum(
                    entry["callCount"] for entry in self.target_ledger.values()
                    if entry["allowed"]),
                "violations": self.guard_violations,
                "peerAttributionAmbiguities": [],
            },
            "appliedRules": self._applied_rules(),
            "redaction": {
                "policy": REDACTION_POLICY,
                "redactedHeaderNames": [],
                "redactedBodyPointers": [],
                "secretReferencesObserved": sorted(set(self.secret_refs_resolved)),
                "secretValuesCaptured": False,
                "credentialMaterialCaptured": False,
                "rawArtifactScan": raw_store_scan,
            },
        }
        return document


def validate_capture(document: Mapping[str, Any], *, schema_path: Path) -> None:
    """Validate a capture against the FROZEN schema bytes.  No local shadow."""
    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    if errors:
        rendered = "; ".join(
            f"/{'/'.join(str(part) for part in error.path)}: {error.message}"
            for error in errors[:6])
        raise CaptureMirrorError(f"capture is schema-invalid: {rendered}")


def record_jcs(record: Mapping[str, Any]) -> str:
    return jcs_sha256(dict(record))
