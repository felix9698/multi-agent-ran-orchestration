"""The independent verifier: re-adjudication from RAW evidence alone.

``LO1-ST-I01`` is the guard against the suite believing its own output.  A test
may never accept its own result as the oracle, so this module:

* **imports nothing from the runtime and nothing from the rest of this
  package.**  Its only dependencies are the standard library and ``jsonschema``.
  It deliberately re-implements pointer resolution, digesting and the PM parse
  instead of sharing them with the emulator or the capture mirror, so a bug in
  either cannot be agreed with;
* **re-derives every digest, linkage and counter from the bytes on disk.**  The
  capture document is treated as a *claim*; the raw artefacts under the capture
  root are the evidence;
* **reads the oracle from the frozen catalog at adjudication time.**  Nothing
  here restates an expected value, so mutating a scratch copy of
  ``/scenarios/83/expected`` changes what this verifier reports -- which is
  exactly what ``G-ORACLE-2`` / ``LO1-ST-O01`` require;
* **produces findings, never a verdict about the scenario.**  Disposition for
  SC-084 belongs to the lower conformance runner.

Every finding is a stable ``CODE:detail`` string so a falsifier can assert the
*specific* defect it injected rather than merely "something failed".
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree

SELF_TEST_STATE_LABEL = "UPPER_LIVE_O1_HARNESS_SELF_TEST"
EMULATOR_KIND = "CONTRACT_FAITHFUL_EMULATOR"
LIVE_PROVIDER_KIND = "LIVE_O1_PROVIDER_UNDER_SEPARATE_AUTHORITY"

_CLAIM_TOKEN = re.compile(rb"[A-Za-z0-9_/ .-]{5,48}")
_PRIVATE_KEY_BEGIN = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PRIVATE_KEY_END = re.compile(rb"-----END [A-Z0-9 ]*PRIVATE KEY-----")
_CREDENTIAL_MARKERS = (b"Bearer ", b"password=", b"api-key=", b"apikey=")
_NETCONF_BASE_11 = "urn:ietf:params:netconf:base:1.1"
_NETCONF_EOM = b"]]>]]>"
_NETCONF_PHASES = frozenset(("LIFECYCLE", "TEARDOWN"))
_NETCONF_COUNTERS = (
    ("measuredRpcDelta", "rpcRecordCount"),
    ("clientToServerOctetDelta", "clientToServerOctetCount"),
    ("serverToClientOctetDelta", "serverToClientOctetCount"),
    ("clientToServerMessageDelta", "clientToServerMessageCount"),
    ("serverToClientMessageDelta", "serverToClientMessageCount"),
    ("clientToServerChunkDelta", "clientToServerChunkCount"),
    ("serverToClientChunkDelta", "serverToClientChunkCount"),
)
_READINESS_ORDER = (
    "PRECHECK", "TRUST_READY", "SUBSCRIBED", "JOB_ACTIVE", "ASSURANCE_READY")
_READINESS_INITIAL_STATES = {
    "PRECHECK": ("INSTALL_O1_PROFILE",),
    "TRUST_READY": (),
    "SUBSCRIBED": ("INSTALL_ACTIVE_O1_SUBSCRIPTION",),
    "JOB_ACTIVE": ("SET_PERF_METRIC_JOB_UNLOCKED",),
    "ASSURANCE_READY": ("CONFIGURE_LIVE_O1_OK_RECORD_FOR_EACH_POLICY_CELL",),
}
_READINESS_OWNERS = {
    "PRECHECK": "UPPER_HARNESS",
    "TRUST_READY": "UPPER_HARNESS",
    "SUBSCRIBED": "UPPER_HARNESS",
    "JOB_ACTIVE": "UPPER_HARNESS",
    "ASSURANCE_READY": "LOWER_LIVE_O1_PROVIDER",
}
_READINESS_OBSERVED_JOB_ORDER = (
    "DATASTORE_LOCKED", "JOB_CREATED_LOCKED", "JOB_CONFIG_VERIFIED",
    "SUBSCRIPTION_DURABLE", "JOB_UNLOCKED", "JOB_ACTIVE", "DATASTORE_UNLOCKED")
_READINESS_TEARDOWN_ORDER = (
    "JOB_LOCKED", "JOB_DELETED", "IN_FLIGHT_DRAINED",
    "SUBSCRIPTION_DELETED", "NETCONF_SESSION_CLOSED")


class VerifierError(RuntimeError):
    """The verifier could not read what it was pointed at.  It never guesses."""


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _pointer(document: Any, pointer: str) -> Any:
    current = document
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            current = current[token]
        elif isinstance(current, list):
            current = current[int(token)]
        else:
            raise VerifierError(f"pointer {pointer} does not resolve")
    return current


def _instant(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class _WireDecodeError(ValueError):
    """Raw NETCONF plaintext cannot be partitioned under RFC 6242."""


def _decode_post_hello_stream(
        blob: bytes) -> tuple[bytes, list[bytes], list[int], list[int]]:
    """Parse hello EOM then chunked messages without either peer's codec."""
    hello, marker, remainder = blob.partition(_NETCONF_EOM)
    if not marker or not hello:
        raise _WireDecodeError("missing hello or hello EOM delimiter")
    if _NETCONF_EOM in remainder:
        raise _WireDecodeError("post-hello EOM delimiter observed")

    messages: list[bytes] = []
    chunk_counts: list[int] = []
    end_offsets: list[int] = []
    cursor = 0
    while cursor < len(remainder):
        chunks: list[bytes] = []
        while True:
            if remainder[cursor:cursor + 4] == b"\n##\n":
                if not chunks:
                    raise _WireDecodeError("message ended before its first chunk")
                cursor += 4
                messages.append(b"".join(chunks))
                chunk_counts.append(len(chunks))
                end_offsets.append(cursor)
                break
            if remainder[cursor:cursor + 2] != b"\n#":
                raise _WireDecodeError(f"invalid chunk prefix at octet {cursor}")
            line_end = remainder.find(b"\n", cursor + 2)
            if line_end < 0:
                raise _WireDecodeError("unterminated chunk size")
            digits = remainder[cursor + 2:line_end]
            if (not digits or not digits.isdigit() or digits[:1] == b"0"):
                raise _WireDecodeError("non-canonical chunk size")
            size = int(digits)
            body_start = line_end + 1
            body_end = body_start + size
            if body_end > len(remainder):
                raise _WireDecodeError("chunk body ends after the captured stream")
            chunks.append(remainder[body_start:body_end])
            cursor = body_end
    return hello, messages, chunk_counts, end_offsets


def _hello_capabilities(raw: bytes) -> list[str]:
    try:
        root = ElementTree.fromstring(raw.decode("utf-8"))
    except (UnicodeDecodeError, ElementTree.ParseError) as exc:
        raise _WireDecodeError("NETCONF hello is not parseable XML") from exc
    if _local(str(root.tag)) != "hello":
        raise _WireDecodeError("first wire message is not NETCONF hello")
    return sorted({
        (node.text or "").strip() for node in root.iter()
        if _local(str(node.tag)) == "capability" and (node.text or "").strip()
    })


class IndependentVerifier:
    """Re-adjudicates one evidence bundle without trusting the producer."""

    def __init__(self, *, capture_path: Path, bundle_path: Path, gates_path: Path,
                 capture_schema_path: Path) -> None:
        capture_path = Path(capture_path)
        if capture_path.is_dir():
            self.capture_root = capture_path
            self.capture_path = capture_path / "capture.json"
        else:
            self.capture_root = capture_path.parent
            self.capture_path = capture_path
        if not self.capture_path.is_file():
            raise VerifierError(f"no capture document at {self.capture_path}")
        self.bundle_path = Path(bundle_path)
        self.gates_path = Path(gates_path)
        self.capture_schema_path = Path(capture_schema_path)
        self.document: Mapping[str, Any] = json.loads(
            self.capture_path.read_text(encoding="utf-8"))
        report_path = self.capture_root / "self-test-report.json"
        self.report: Mapping[str, Any] | None = (
            json.loads(report_path.read_text(encoding="utf-8"))
            if report_path.is_file() else None)
        self._findings: list[str] = []
        self._observations: dict[str, Any] = {}

    # ------------------------------------------------------------- utilities

    def _finding(self, code: str, detail: str = "") -> None:
        self._findings.append(f"{code}:{detail}" if detail else code)

    def _bundle_json(self, name: str) -> Any:
        path = self.bundle_path / name
        if not path.is_file():
            raise VerifierError(f"frozen input {name} is absent from {self.bundle_path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _artifact(self, relative: str) -> bytes | None:
        candidate = PurePosixPath(relative)
        if (candidate.is_absolute() or not candidate.parts
                or any(part in {"", ".", ".."} for part in candidate.parts)
                or candidate.as_posix() != relative):
            return None
        root = self.capture_root.resolve()
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return None
        if not path.is_file():
            return None
        return path.read_bytes()

    # ------------------------------------------------------------ the oracle

    def _oracle(self) -> tuple[Mapping[str, Any], Sequence[str], Mapping[str, Any]]:
        """Read the oracle, never restate it."""
        assignment = self._bundle_json("execution-profile-assignment.1.0.1.json")
        catalog = self._bundle_json("scenario-catalog.1.0.1.json")
        entry = None
        for candidate in assignment["assignments"]:
            if candidate.get("scenarioId") == self.document["run"]["scenarioId"]:
                entry = candidate
                break
        if entry is None:
            raise VerifierError("the frozen assignment carries no entry for this scenario")
        scenario = _pointer(catalog, entry["catalogPointer"])
        return scenario["expected"], scenario["rules"], scenario

    # ------------------------------------------------------------ adjudicate

    def readjudicate(self) -> dict[str, Any]:
        self._findings = []
        self._observations = {}
        self._check_schema()
        expected, rules, scenario = self._oracle()
        self._check_profile_agreement()
        self._check_upper_release_identity()
        self._check_provider_identity()
        self._check_contract_binding()
        self._check_endpoints()
        self._check_secrets()
        self._check_sequence_high_water()
        self._check_raw_linkage()
        self._check_netconf_capture_semantics()
        self._check_unreferenced_artifacts()
        self._check_notifications()
        self._check_retrievals()
        self._rederive_normalization()
        self._check_rules(rules)
        self._check_live_value_invariants()
        self._check_time_relations()
        self._check_coordinator(expected)
        self._check_stubs(expected)
        self._check_upper_exchange_coverage()
        self._check_counters(expected, scenario)
        self._check_cleanup()
        self._check_egress()
        self._check_labels_and_claims()
        self._check_key_material()
        return {
            "specVersion": "oran-aic-upper-live-o1-harness-independent-verifier/1.0.0",
            "notAVerdict": True,
            "oracleOwnership": self.document.get("oracleOwnership"),
            "captureRoot": str(self.capture_root),
            "runId": self.document["run"]["runId"],
            "scenarioId": self.document["run"]["scenarioId"],
            "coreSectionsOrigin": (self.report or {}).get("coreSectionsOrigin"),
            "observations": self._observations,
            "findings": list(self._findings),
            "findingCount": len(self._findings),
        }

    def findings(self) -> tuple[str, ...]:
        if not self._findings and not self._observations:
            self.readjudicate()
        return tuple(self._findings)

    # ------------------------------------------------------------- the checks

    def _check_schema(self) -> None:
        try:
            from jsonschema import Draft202012Validator, FormatChecker
        except ImportError:  # pragma: no cover - the standalone verifier needs it
            self._finding("SCHEMA_VALIDATOR_UNAVAILABLE")
            return
        schema = json.loads(self.capture_schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(self.document),
                        key=lambda error: list(error.path))
        for error in errors[:5]:
            self._finding(
                "CAPTURE_SCHEMA_INVALID",
                f"/{'/'.join(str(part) for part in error.path)} {error.message}"[:200])
        self._observations["schemaErrorCount"] = len(errors)

    def _check_profile_agreement(self) -> None:
        profile = self.document.get("profile", {})
        kind = profile.get("providerKind")
        label = profile.get("selfTestLabel")
        emulator_in_use = profile.get("emulatorInUse")
        counterpart = profile.get("counterpartKind")
        if kind == EMULATOR_KIND:
            if emulator_in_use is not True:
                self._finding("EMULATOR_SPLIT_DISAGREEMENT", "emulatorInUse")
            if label != SELF_TEST_STATE_LABEL:
                self._finding("EMULATOR_SPLIT_DISAGREEMENT", f"selfTestLabel={label}")
            if counterpart != "UPPER_ARTIFACT_SELF_TEST_HARNESS":
                self._finding("EMULATOR_SPLIT_DISAGREEMENT",
                              f"counterpartKind={counterpart}")
        elif kind == LIVE_PROVIDER_KIND:
            if emulator_in_use is not False:
                self._finding("EMULATOR_USED_AS_PRODUCTION", "emulatorInUse under live")
            if label is not None:
                self._finding("EMULATOR_USED_AS_PRODUCTION", f"selfTestLabel={label}")
        else:
            self._finding("PROVIDER_KIND_UNKNOWN", str(kind))
        if self.report is not None:
            reported = self.report.get("providerKind")
            if reported != kind:
                self._finding("EMULATOR_SPLIT_DISAGREEMENT",
                              f"report={reported} capture={kind}")
            if self.report.get("stateLabel") != SELF_TEST_STATE_LABEL \
                    and kind == EMULATOR_KIND:
                self._finding("SELF_TEST_LABEL_MISSING",
                              str(self.report.get("stateLabel")))
        authority = self.document.get("authority", {})
        refused = [check for check in authority.get("checks", [])
                   if check.get("outcome") in ("REFUSED", "UNRESOLVED")]
        if refused and authority.get("gateResult") == "ADMITTED":
            self._finding(
                "GATE_DECISION_DISAGREEMENT",
                ",".join(str(check.get("id")) for check in refused))
        if authority.get("gateResult") == "REFUSED" and \
                self.document["run"].get("disposition") != "ABORTED_GATE":
            self._finding("GATE_DECISION_DISAGREEMENT",
                          "REFUSED without ABORTED_GATE")
        if authority.get("selfIssued"):
            self._finding("SELF_ISSUED_AUTHORITY")
        self._observations["providerKind"] = kind
        self._observations["gateChecksRefused"] = len(refused)

    def _check_provider_identity(self) -> None:
        provider = self.document.get("provider", {})
        digest = str(provider.get("ociImageManifestDigest", ""))
        kind = self.document.get("profile", {}).get("providerKind")
        if kind == LIVE_PROVIDER_KIND:
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                self._finding("IMMUTABLE_IDENTITY_MISMATCH", digest[:80])
        elif kind == EMULATOR_KIND and digest != "UNRESOLVED":
            self._finding("EMULATOR_CLAIMS_IMAGE_DIGEST", digest[:80])
        closure = provider.get("yangClosureDigest")
        capability = provider.get("yangCapabilityDigest")
        if closure != "UNRESOLVED" and closure == capability:
            self._finding("DIGEST_MEANING_CONFLATION", "yangClosure==yangCapability")

    def _check_upper_release_identity(self) -> None:
        """Re-bind capture identity to raw release metadata independently."""
        revisions = self.document.get("revisions")
        if not isinstance(revisions, Mapping):
            self._finding("UPPER_IDENTITY_INVALID", "revisions absent")
            return
        mode = revisions.get("identityMode")
        release_only = (
            "upperReleaseVersion", "upperReleaseContentSha256",
            "upperReleaseManifestSha256", "upperDeliveryArchiveSha256",
            "testedCodeCommit", "upperOciImageManifestDigest")
        if mode == "SOURCE_TREE_DEVELOPMENT":
            if (revisions.get("upperReleaseId")
                    != "upper-live-o1-harness-development-self-test"
                    or any(revisions.get(name) is not None for name in release_only)
                    or revisions.get("artifactProvenanceRaw") is not None
                    or revisions.get("releaseManifestRaw") is not None):
                self._finding("DEVELOPMENT_IDENTITY_MASQUERADES_AS_RELEASE")
            self._observations["upperIdentityMode"] = mode
            self._observations["releaseEvidenceEligible"] = False
            return
        if mode != "PACKAGED_RELEASE":
            self._finding("UPPER_IDENTITY_INVALID", f"identityMode={mode}")
            return

        provenance_raw = self._semantic_raw(
            revisions.get("artifactProvenanceRaw"),
            "revisions artifact provenance")
        manifest_raw = self._semantic_raw(
            revisions.get("releaseManifestRaw"),
            "revisions release manifest")
        if provenance_raw is None or manifest_raw is None:
            self._finding("PACKAGED_RELEASE_IDENTITY_RAW_MISSING")
            return
        try:
            provenance = json.loads(provenance_raw)
            manifest = json.loads(manifest_raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._finding("PACKAGED_RELEASE_IDENTITY_RAW_INVALID")
            return
        if not isinstance(provenance, Mapping) or not isinstance(manifest, Mapping):
            self._finding("PACKAGED_RELEASE_IDENTITY_RAW_INVALID", "not object")
            return

        expected_id = "upper-live-o1-harness"
        expected_version = "1.0.9"
        manifest_provenance = manifest.get("provenance") or {}
        manifest_revisions = manifest.get("revisions") or {}
        oci = manifest_provenance.get("oci") or {}
        comparisons = {
            "releaseId": (
                revisions.get("upperReleaseId"), provenance.get("releaseId"),
                manifest.get("releaseId"), expected_id),
            "releaseVersion": (
                revisions.get("upperReleaseVersion"), provenance.get("releaseVersion"),
                manifest.get("releaseVersion"), expected_version),
            "sourceCommit": (
                revisions.get("upperSourceCommit"), provenance.get("sourceCommit"),
                manifest_provenance.get("sourceCommit"),
                manifest_revisions.get("upperSourceCommit"),
                manifest_revisions.get("testedCodeCommit")),
            "sourceTree": (
                revisions.get("upperSourceTree"), provenance.get("sourceTree"),
                manifest_provenance.get("sourceTree"),
                manifest_revisions.get("upperSourceTree")),
            "content": (
                revisions.get("upperReleaseContentSha256"),
                manifest.get("releaseContentSha256"),
                manifest_revisions.get("upperReleaseContentSha256")),
            "oci": (
                revisions.get("upperOciImageManifestDigest"),
                oci.get("imageManifestDigest"),
                manifest_revisions.get("upperOciImageManifestDigest")),
        }
        for name, values in comparisons.items():
            if len(set(map(str, values))) != 1:
                self._finding(
                    "PACKAGED_RELEASE_IDENTITY_MISMATCH",
                    f"{name}={','.join(map(str, values))}"[:200])
        if (revisions.get("testedCodeCommit") != revisions.get("upperSourceCommit")
                or revisions.get("sourceTreeDirty") is not False
                or provenance.get("sourceIntegrity") != "FULLY_INTEGRATED"
                or manifest.get("sourceIntegrity") != "FULLY_INTEGRATED"
                or provenance.get("payloadBoundToSourceTree") is not True
                or manifest_provenance.get("payloadBoundToSourceTree") is not True):
            self._finding("PACKAGED_RELEASE_SOURCE_BINDING_INVALID")
        if revisions.get("upperReleaseManifestSha256") != _sha256(manifest_raw):
            self._finding("PACKAGED_RELEASE_MANIFEST_DIGEST_MISMATCH")
        files = manifest.get("files")
        if (not isinstance(files, Mapping)
                or files.get("ARTIFACT-PROVENANCE.json")
                != _sha256(provenance_raw)):
            self._finding("PACKAGED_RELEASE_PROVENANCE_DIGEST_MISMATCH")
        archive_digest = revisions.get("upperDeliveryArchiveSha256")
        if not isinstance(archive_digest, str) or re.fullmatch(
                r"[a-f0-9]{64}", archive_digest) is None:
            self._finding("PACKAGED_RELEASE_ARCHIVE_DIGEST_UNRESOLVED")
        self._observations["upperIdentityMode"] = mode
        self._observations["releaseEvidenceEligible"] = not any(
            finding.startswith("PACKAGED_RELEASE_") for finding in self._findings)

    def _check_contract_binding(self) -> None:
        contract = self.document.get("contract", {})
        measured = {
            "catalogSha256": "scenario-catalog.1.0.1.json",
            "runnerContractSha256": "scenario-runner-contract.1.0.1.json",
            "profileAssignmentSha256": "execution-profile-assignment.1.0.1.json",
            "deploymentVectorSchemaSha256": "deployment-test-vector.1.0.0.schema.json",
            "o1NetconfYangProfileSha256": "o1-netconf-yang-profile.1.0.0.json",
            "o1PaFileProfileSha256": "oran-aic-o1-pa-file.1.0.0.json",
            "bundleManifestSha256": "bundle-manifest.1.0.1.json",
            "policyEvidenceSchemaSha256": "aic.policy-evidence.1.0.0.schema.json",
        }
        for member, name in measured.items():
            path = self.bundle_path / name
            if not path.is_file():
                self._finding("CONTRACT_FILE_ABSENT", name)
                continue
            actual = _sha256(path.read_bytes())
            if contract.get(member) != actual:
                self._finding("CONTRACT_DIGEST_MISMATCH", f"{member} {name}")
        if int(contract.get("frozenBundleDiffFileCount", -1)) != 0:
            self._finding("FROZEN_BUNDLE_DIFF",
                          str(contract.get("frozenBundleDiffFileCount")))

    def _check_endpoints(self) -> None:
        roots = self.document.get("deployment", {}).get("resolvedRoots", {})
        seen: dict[str, str] = {}
        for name, value in roots.items():
            if name == "sftpAuthorities":
                for authority in value:
                    if "${" in str(authority):
                        self._finding("ENDPOINT_PLACEHOLDER", name)
                continue
            text = str(value)
            if "${" in text or not text:
                self._finding("ENDPOINT_PLACEHOLDER", name)
                continue
            authority = text.split("//", 1)[-1].split("/", 1)[0]
            host = authority.rsplit(":", 1)[0]
            if host in ("0.0.0.0", "*", "::") or "/" in host:
                self._finding("ENDPOINT_AMBIGUOUS", f"{name}={authority}")
            other = seen.get(authority)
            if other and {other, name} != {"rAppCallbackRoot", "policyEvidencePushBaseUri"}:
                self._finding("ENDPOINT_AUTHORITY_COLLISION", f"{other}~{name}")
            seen.setdefault(authority, name)
        if not self.document.get("deployment", {}).get("placeholderFree"):
            self._finding("ENDPOINT_PLACEHOLDER", "placeholderFree=false")

    def _check_secrets(self) -> None:
        deployment = self.document.get("deployment", {})
        unresolved = deployment.get("unresolvedSecretRefs", [])
        admitted = self.document.get("authority", {}).get("gateResult") == "ADMITTED"
        if unresolved and admitted:
            self._finding("UNRESOLVED_SECRET_REF_ON_ADMITTED_RUN",
                          ",".join(map(str, unresolved))[:120])
        redaction = self.document.get("redaction", {})
        if redaction.get("secretValuesCaptured") or \
                redaction.get("credentialMaterialCaptured"):
            self._finding("CREDENTIAL_MATERIAL_CAPTURED")
        scan = redaction.get("rawArtifactScan", {})
        if int(scan.get("privateKeyBlockHits", 0)) > 0:
            self._finding("PRIVATE_KEY_BLOCK_IN_EVIDENCE",
                          str(scan.get("privateKeyBlockHits")))
        stored = sum(1 for path in (self.capture_root / "raw").rglob("*")
                     if path.is_file()) if (self.capture_root / "raw").is_dir() else 0
        if int(scan.get("filesScanned", -1)) != stored:
            self._finding("REDACTION_SCAN_INCOMPLETE",
                          f"declared={scan.get('filesScanned')} stored={stored}")

    def _check_sequence_high_water(self) -> None:
        """Recompute the exact maximum across every sequenced record class."""
        collections: list[tuple[str, Any]] = [
            ("exchanges", self.document.get("exchanges", [])),
            ("harnessOperations", self.document.get("harnessOperations", [])),
            ("netconf.rpcs", (self.document.get("netconf") or {}).get("rpcs", [])),
            ("notifications", self.document.get("notifications", [])),
            ("retrievals", self.document.get("retrievals", [])),
        ]
        raw_stubs = self.document.get("deterministicStubs") or {}
        stubs = raw_stubs if isinstance(raw_stubs, Mapping) else {}
        if not isinstance(raw_stubs, Mapping):
            collections.append(("deterministicStubs", raw_stubs))
        for name in ("kpmSnapshots", "controlResults", "readbacks", "writeLedger"):
            collections.append((f"deterministicStubs.{name}", stubs.get(name, [])))
        sequences: list[int] = []
        malformed: list[str] = []
        for name, records in collections:
            if not isinstance(records, list):
                malformed.append(f"{name}:not-array")
                continue
            for index, record in enumerate(records):
                value = record.get("sequence") if isinstance(record, Mapping) else None
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    malformed.append(f"{name}/{index}:{value}")
                else:
                    sequences.append(value)
        if malformed:
            self._finding(
                "SEQUENCE_HIGH_WATER_INVALID", ",".join(malformed)[:200])
        declared = (self.document.get("run") or {}).get("sequenceHighWaterMark")
        expected = max(sequences, default=0)
        if not isinstance(declared, int) or isinstance(declared, bool) \
                or declared != expected:
            self._finding(
                "SEQUENCE_HIGH_WATER_MISMATCH",
                f"declared={declared} exactMax={expected}")
        self._observations["sequenceHighWaterMark"] = {
            "declared": declared,
            "recomputedExactMax": expected,
            "recordCount": len(sequences),
        }

    def _check_raw_linkage(self) -> None:
        """Recompute every declared digest over the bytes actually on disk."""
        missing = 0
        mismatched = 0

        def check(record: Mapping[str, Any], where: str) -> None:
            nonlocal missing, mismatched
            path = record.get("artifactPath") or record.get("rawArtifactPath")
            if not path:
                self._finding("RAW_CAPTURE_OMISSION", f"{where} has no artifactPath")
                missing += 1
                return
            blob = self._artifact(str(path))
            if blob is None:
                self._finding("RAW_CAPTURE_OMISSION", f"{where} {path}")
                missing += 1
                return
            declared = record.get("sha256") or record.get("byteSha256")
            actual = _sha256(blob)
            if declared != actual:
                self._finding("RAW_DIGEST_MISMATCH", f"{where} {path}")
                mismatched += 1
            declared_count = record.get("byteCount")
            if declared_count is not None and int(declared_count) != len(blob):
                self._finding("RAW_BYTE_COUNT_MISMATCH", f"{where} {path}")

        for index, notification in enumerate(self.document.get("notifications", [])):
            check(notification.get("raw", {}), f"/notifications/{index}/raw")
        for index, retrieval in enumerate(self.document.get("retrievals", [])):
            if retrieval.get("outcome") == "RETRIEVED":
                check(retrieval, f"/retrievals/{index}")
        session = (self.document.get("netconf") or {}).get("session")
        if session:
            check(session.get("helloRaw", {}), "/netconf/session/helloRaw")
            check(session.get("clientHelloRaw", {}),
                  "/netconf/session/clientHelloRaw")
            wire = session.get("wireEvidence") or {}
            check(wire.get("clientToServerRaw", {}),
                  "/netconf/session/wireEvidence/clientToServerRaw")
            check(wire.get("serverToClientRaw", {}),
                  "/netconf/session/wireEvidence/serverToClientRaw")
        for index, rpc in enumerate((self.document.get("netconf") or {}).get("rpcs", [])):
            check(rpc.get("requestRaw", {}), f"/netconf/rpcs/{index}/requestRaw")
            check(rpc.get("replyRaw", {}), f"/netconf/rpcs/{index}/replyRaw")
        revisions = self.document.get("revisions") or {}
        for name in ("artifactProvenanceRaw", "releaseManifestRaw"):
            descriptor = revisions.get(name)
            if descriptor is not None:
                check(descriptor, f"/revisions/{name}")
        self._observations["rawArtifactsMissing"] = missing
        self._observations["rawDigestMismatches"] = mismatched

    def _check_unreferenced_artifacts(self) -> None:
        referenced: set[str] = set()
        for notification in self.document.get("notifications", []):
            path = (notification.get("raw") or {}).get("artifactPath")
            if path:
                referenced.add(str(path))
        for retrieval in self.document.get("retrievals", []):
            if retrieval.get("rawArtifactPath"):
                referenced.add(str(retrieval["rawArtifactPath"]))
        netconf = self.document.get("netconf") or {}
        session = netconf.get("session")
        if session and session.get("helloRaw"):
            referenced.add(str(session["helloRaw"]["artifactPath"]))
        if session and session.get("clientHelloRaw"):
            referenced.add(str(session["clientHelloRaw"]["artifactPath"]))
        wire = (session or {}).get("wireEvidence") if session else None
        if wire:
            for name in ("clientToServerRaw", "serverToClientRaw"):
                descriptor = wire.get(name) or {}
                if descriptor.get("artifactPath"):
                    referenced.add(str(descriptor["artifactPath"]))
        for rpc in netconf.get("rpcs", []):
            referenced.add(str(rpc["requestRaw"]["artifactPath"]))
            referenced.add(str(rpc["replyRaw"]["artifactPath"]))
        revisions = self.document.get("revisions") or {}
        for name in ("artifactProvenanceRaw", "releaseManifestRaw"):
            descriptor = revisions.get(name)
            if isinstance(descriptor, Mapping) and descriptor.get("artifactPath"):
                referenced.add(str(descriptor["artifactPath"]))
        root = self.capture_root / "raw"
        stray = []
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                relative = str(path.relative_to(self.capture_root))
                if relative not in referenced:
                    stray.append(relative)
        for relative in stray:
            self._finding("STALE_OR_UNREFERENCED_ARTIFACT", relative)
        self._observations["unreferencedArtifacts"] = len(stray)

    def _semantic_raw(self, descriptor: Any, where: str) -> bytes | None:
        if not isinstance(descriptor, Mapping):
            self._finding("NETCONF_RAW_EVIDENCE_INVALID", f"{where} descriptor")
            return None
        relative = descriptor.get("artifactPath")
        blob = self._artifact(str(relative)) if isinstance(relative, str) else None
        if blob is None:
            self._finding("NETCONF_RAW_EVIDENCE_INVALID", f"{where} missing")
            return None
        if (_sha256(blob) != descriptor.get("sha256")
                or len(blob) != descriptor.get("byteCount")):
            self._finding("NETCONF_RAW_EVIDENCE_INVALID", f"{where} digest/count")
            return None
        return blob

    def _check_netconf_capture_semantics(self) -> None:
        """Re-derive capture-v2 NETCONF relations from delivered raw bytes.

        This pass shares no framing or validation callable with the producer.
        JSON Schema remains the structural gate; this method proves the
        cross-field and raw-wire relationships the schema cannot express.
        """
        netconf = self.document.get("netconf")
        if not isinstance(netconf, Mapping):
            self._finding("NETCONF_CAPTURE_V2_MISSING")
            return
        raw_rpcs = netconf.get("rpcs")
        if not isinstance(raw_rpcs, list):
            self._finding("NETCONF_RPC_SET_INVALID", "rpcs is not an array")
            rpcs: list[Mapping[str, Any]] = []
        else:
            rpcs = [record for record in raw_rpcs if isinstance(record, Mapping)]
            if len(rpcs) != len(raw_rpcs):
                self._finding("NETCONF_RPC_SET_INVALID", "non-object RPC record")

        valid_phases: list[str] = []
        for index, record in enumerate(rpcs):
            phase = record.get("phase")
            if phase not in _NETCONF_PHASES:
                self._finding(
                    "NETCONF_RPC_PHASE_INVALID", f"/{index} phase={phase!r}")
            else:
                valid_phases.append(str(phase))
        lifecycle_count = sum(
            record.get("phase") == "LIFECYCLE" for record in rpcs)
        teardown_count = sum(
            record.get("phase") == "TEARDOWN" for record in rpcs)
        if valid_phases != (["LIFECYCLE"] * lifecycle_count
                            + ["TEARDOWN"] * teardown_count):
            self._finding("NETCONF_RPC_PHASE_ORDER_INVALID")

        self._check_netconf_readiness(netconf, rpcs)

        session = netconf.get("session")
        segment = netconf.get("measuredSegment")
        completed = (self.document.get("run") or {}).get("disposition") == "COMPLETED"
        if completed and not isinstance(segment, Mapping):
            self._finding("NETCONF_MEASURED_SEGMENT_MISSING")
        if not isinstance(session, Mapping):
            if completed or isinstance(segment, Mapping):
                self._finding("NETCONF_SESSION_BINDING_MISMATCH", "session absent")
            return
        if not isinstance(segment, Mapping):
            return

        wire = session.get("wireEvidence")
        if not isinstance(wire, Mapping):
            self._finding("NETCONF_WIRE_EVIDENCE_MISSING")
            return
        if segment.get("sessionId") != wire.get("sessionId"):
            self._finding("NETCONF_SESSION_BINDING_MISMATCH", "segment != wire")

        start = segment.get("start")
        end = segment.get("end")
        delta = segment.get("delta")
        counters_valid = all(isinstance(value, Mapping)
                             for value in (start, end, delta))
        if not counters_valid:
            self._finding("NETCONF_MEASURED_DELTA_INVALID", "counter map absent")
        else:
            assert isinstance(start, Mapping) and isinstance(end, Mapping)
            assert isinstance(delta, Mapping)
            try:
                if (int(end["captureSequence"]) < int(start["captureSequence"])
                        or int(end["monotonicNs"]) < int(start["monotonicNs"])):
                    self._finding("NETCONF_MEASURED_DELTA_INVALID", "ordering")
                for delta_name, counter_name in _NETCONF_COUNTERS:
                    observed = int(end[counter_name]) - int(start[counter_name])
                    declared = int(delta[delta_name])
                    if declared != 0 or observed != declared:
                        self._finding(
                            "NETCONF_MEASURED_DELTA_INVALID",
                            f"{delta_name} declared={declared} observed={observed}")
            except (KeyError, TypeError, ValueError):
                self._finding("NETCONF_MEASURED_DELTA_INVALID", "counter malformed")

        client_wire = self._semantic_raw(
            wire.get("clientToServerRaw"), "client-to-server wire")
        server_wire = self._semantic_raw(
            wire.get("serverToClientRaw"), "server-to-client wire")
        if client_wire is None or server_wire is None:
            return
        try:
            client_hello, requests, request_chunks, request_ends = \
                _decode_post_hello_stream(client_wire)
            server_hello, replies, reply_chunks, reply_ends = \
                _decode_post_hello_stream(server_wire)
            client_caps = _hello_capabilities(client_hello)
            server_caps = _hello_capabilities(server_hello)
        except _WireDecodeError as exc:
            self._finding("NETCONF_WIRE_FRAMING_INVALID", str(exc))
            return

        if (_NETCONF_BASE_11 in client_caps and _NETCONF_BASE_11 in server_caps
                and session.get("negotiatedFraming") != "CHUNKED"):
            self._finding("NETCONF_BASE11_NOT_CHUNKED")
        if (client_caps != sorted(map(str, session.get(
                "clientAdvertisedCapabilities", [])))
                or server_caps != sorted(map(str, session.get(
                    "advertisedCapabilities", [])))):
            self._finding("NETCONF_HELLO_BIJECTION_MISMATCH", "capabilities")

        client_hello_claim = self._semantic_raw(
            session.get("clientHelloRaw"), "client hello")
        server_hello_claim = self._semantic_raw(
            session.get("helloRaw"), "server hello")
        if (client_hello_claim != client_hello
                or server_hello_claim != server_hello):
            self._finding("NETCONF_HELLO_BIJECTION_MISMATCH", "hello bytes")
        session_id = _sha256(client_hello + server_hello)
        if wire.get("sessionId") != session_id:
            self._finding("NETCONF_SESSION_BINDING_MISMATCH", "hello digest")

        summary = {
            "clientToServerMessageCount": 1 + len(requests),
            "serverToClientMessageCount": 1 + len(replies),
            "clientToServerChunkCount": sum(request_chunks),
            "serverToClientChunkCount": sum(reply_chunks),
            "clientToServerHelloEomCount": 1,
            "serverToClientHelloEomCount": 1,
            "clientToServerPostHelloEomCount": 0,
            "serverToClientPostHelloEomCount": 0,
        }
        for name, observed in summary.items():
            if wire.get(name) != observed:
                self._finding(
                    "NETCONF_WIRE_SUMMARY_MISMATCH",
                    f"{name} declared={wire.get(name)} observed={observed}")

        if len(requests) != len(rpcs) or len(replies) != len(rpcs):
            self._finding(
                "NETCONF_RPC_BIJECTION_MISMATCH",
                f"records={len(rpcs)} requests={len(requests)} replies={len(replies)}")
        else:
            for index, record in enumerate(rpcs):
                request = self._semantic_raw(
                    record.get("requestRaw"), f"RPC {index} request")
                reply = self._semantic_raw(
                    record.get("replyRaw"), f"RPC {index} reply")
                if (request is None or request != requests[index]
                        or reply != replies[index]
                        or record.get("requestFixtureSha256") != _sha256(request)):
                    self._finding("NETCONF_RPC_BIJECTION_MISMATCH", str(index))

        if not counters_valid:
            return
        assert isinstance(start, Mapping) and isinstance(end, Mapping)
        try:
            lifecycle_sequences = [
                int(record["sequence"]) for record in rpcs
                if record.get("phase") == "LIFECYCLE"]
            teardown_sequences = [
                int(record["sequence"]) for record in rpcs
                if record.get("phase") == "TEARDOWN"]
            if (lifecycle_sequences
                    and max(lifecycle_sequences) >= int(start["captureSequence"])):
                self._finding("NETCONF_MEASURED_BOUNDARY_INVALID", "lifecycle")
            if (teardown_sequences
                    and min(teardown_sequences) < int(end["captureSequence"])):
                self._finding("NETCONF_MEASURED_BOUNDARY_INVALID", "teardown")

            def octets(hello: bytes, offsets: Sequence[int]) -> int:
                tail = offsets[lifecycle_count - 1] if lifecycle_count else 0
                return len(hello) + len(_NETCONF_EOM) + int(tail)

            expected_boundary = {
                "rpcRecordCount": lifecycle_count,
                "clientToServerOctetCount": octets(client_hello, request_ends),
                "serverToClientOctetCount": octets(server_hello, reply_ends),
                "clientToServerMessageCount": 1 + lifecycle_count,
                "serverToClientMessageCount": 1 + lifecycle_count,
                "clientToServerChunkCount": sum(request_chunks[:lifecycle_count]),
                "serverToClientChunkCount": sum(reply_chunks[:lifecycle_count]),
            }
            for snapshot_name, snapshot in (("start", start), ("end", end)):
                for name, observed in expected_boundary.items():
                    if int(snapshot[name]) != observed:
                        self._finding(
                            "NETCONF_MEASURED_BOUNDARY_INVALID",
                            f"{snapshot_name}.{name}={snapshot.get(name)} != {observed}")
        except (IndexError, KeyError, TypeError, ValueError):
            self._finding("NETCONF_MEASURED_BOUNDARY_INVALID", "counter malformed")

        self._observations["netconfSemanticRpcCount"] = len(rpcs)
        self._observations["netconfSemanticWireMessages"] = {
            "clientToServer": 1 + len(requests),
            "serverToClient": 1 + len(replies),
        }

    def _check_netconf_readiness(
            self, netconf: Mapping[str, Any],
            rpcs: Sequence[Mapping[str, Any]]) -> None:
        """Independently re-derive §10.2 assignment, order, and evidence.

        The producer's readiness helper is deliberately not imported.  The
        required initial states and their postcondition strings are read from
        the frozen scenario and runner contract, while the §10.2 state and
        teardown orders are protocol invariants of this verifier.
        """
        assignment = netconf.get("assignment")
        if not isinstance(assignment, Mapping):
            self._finding("NETCONF_READINESS_ASSIGNMENT_INVALID", "absent")
        else:
            runner = self._bundle_json("scenario-runner-contract.1.0.1.json")
            expected_actions = sorted(
                str(runner["initialStates"][initial]["adapterAction"])
                for state in _READINESS_ORDER[:4]
                for initial in _READINESS_INITIAL_STATES[state])
            assigned = sorted(map(
                str, assignment.get("adapterActionsAssignedToUpper", [])))
            if assigned != expected_actions:
                self._finding(
                    "NETCONF_READINESS_ASSIGNMENT_INVALID",
                    f"assigned={assigned} expected={expected_actions}")
            digest = assignment.get("authorityRecordDigest")
            authority_digest = (self.document.get("authority") or {}).get(
                "authorityRecordDigest")
            if (not isinstance(digest, str)
                    or re.fullmatch(r"[a-f0-9]{64}", digest) is None
                    or digest != authority_digest):
                self._finding(
                    "NETCONF_READINESS_ASSIGNMENT_INVALID",
                    "authorityRecordDigest is absent or not bound")

        readiness = netconf.get("readiness")
        if not isinstance(readiness, Mapping):
            self._finding("NETCONF_READINESS_MISSING")
            return

        scenario_id = str((self.document.get("run") or {}).get("scenarioId", ""))
        catalog = self._bundle_json("scenario-catalog.1.0.1.json")
        scenario = next(
            (entry for entry in catalog.get("scenarios", [])
             if str(entry.get("id")) == scenario_id), None)
        if not isinstance(scenario, Mapping):
            self._finding("NETCONF_READINESS_REQUIRED_SET_INVALID", "scenario absent")
            return
        scenario_initial = set(map(
            str, scenario.get("materialization", {}).get("initialState", [])))
        expected_initial = [
            initial for state in _READINESS_ORDER
            for initial in _READINESS_INITIAL_STATES[state]]
        declared_initial = list(map(
            str, readiness.get("requiredInitialStates", [])))
        if (declared_initial != expected_initial
                or not set(expected_initial).issubset(scenario_initial)):
            self._finding(
                "NETCONF_READINESS_REQUIRED_SET_INVALID",
                f"declared={declared_initial}")

        states = readiness.get("states")
        if not isinstance(states, list) or not all(
                isinstance(entry, Mapping) for entry in states):
            self._finding("NETCONF_READINESS_ORDER_INVALID", "states malformed")
            return
        observed_order = [str(entry.get("state")) for entry in states]
        if observed_order != list(_READINESS_ORDER):
            self._finding(
                "NETCONF_READINESS_ORDER_INVALID", str(observed_order))

        runner = self._bundle_json("scenario-runner-contract.1.0.1.json")
        by_state = {
            str(entry.get("state")): entry for entry in states
            if str(entry.get("state")) in _READINESS_ORDER
        }
        for state in _READINESS_ORDER:
            entry = by_state.get(state)
            if entry is None:
                continue
            initials = list(_READINESS_INITIAL_STATES[state])
            postconditions = [
                str(runner["initialStates"][name]["postcondition"])
                for name in initials]
            if (list(entry.get("initialStates", [])) != initials
                    or list(entry.get("postconditions", [])) != postconditions
                    or entry.get("owner") != _READINESS_OWNERS[state]):
                self._finding(
                    "NETCONF_READINESS_STATE_BINDING_INVALID", state)

        confirmed = [
            state for state in _READINESS_ORDER
            if (by_state.get(state) or {}).get("confirmed") is True]
        expected_unmet = [state for state in _READINESS_ORDER if state not in confirmed]
        if (readiness.get("unmet") != expected_unmet
                or readiness.get("measuredBodyAdmitted")
                is not (confirmed[:4] == list(_READINESS_ORDER[:4]))
                or readiness.get("complete") is not (not expected_unmet)):
            self._finding("NETCONF_READINESS_COMPLETENESS_INVALID")
        completed = (self.document.get("run") or {}).get("disposition") == "COMPLETED"
        if completed and (confirmed != list(_READINESS_ORDER)
                          or readiness.get("complete") is not True):
            self._finding("NETCONF_COMPLETED_WITHOUT_READINESS")

        subscribed = (by_state.get("SUBSCRIBED") or {}).get("evidence", {})
        job = (by_state.get("JOB_ACTIVE") or {}).get("evidence", {})
        observed_job_order = str(job.get("observedStateOrder", "")).split(",")
        if (subscribed.get("durable") is not True
                or subscribed.get("createdStatus") != 201
                or not subscribed.get("subscriptionId")
                or job.get("durableSubscriptionConfirmedBeforeUnlock") is not True
                or observed_job_order != list(_READINESS_OBSERVED_JOB_ORDER)
                or observed_job_order.index("SUBSCRIPTION_DURABLE")
                >= observed_job_order.index("JOB_UNLOCKED")):
            self._finding("NETCONF_DURABLE_BEFORE_UNLOCK_INVALID")
        if (job.get("administrativeState") != "UNLOCKED"
                or job.get("operationalState") != "ENABLED"
                or job.get("datastoreLockedBeforeCreate") is not True
                or job.get("lockedReadbackConfirmed") is not True
                or job.get("datastoreUnlocked") is not True):
            self._finding("NETCONF_JOB_ACTIVE_EVIDENCE_INVALID")

        teardown_order = list(map(str, readiness.get("teardownOrder", [])))
        if teardown_order != list(_READINESS_TEARDOWN_ORDER):
            self._finding(
                "NETCONF_READINESS_TEARDOWN_ORDER_INVALID",
                str(teardown_order))

        profile = self._bundle_json("o1-netconf-yang-profile.1.0.0.json")
        expected_lifecycle = [
            (int(entry["ordinal"]), str(entry["requestFixture"]))
            for entry in profile.get("lifecycle", []) if entry.get("requestFixture")]
        expected_teardown = [
            (int(entry["ordinal"]), str(entry["requestFixture"]))
            for entry in profile.get("teardown", []) if entry.get("requestFixture")]
        observed_lifecycle = [
            (int(entry.get("lifecycleOrdinal", -1)),
             str(entry.get("requestFixtureRef", "")))
            for entry in rpcs if entry.get("phase") == "LIFECYCLE"]
        observed_teardown = [
            (int(entry.get("lifecycleOrdinal", -1)),
             str(entry.get("requestFixtureRef", "")))
            for entry in rpcs if entry.get("phase") == "TEARDOWN"]
        if observed_lifecycle != expected_lifecycle:
            self._finding("NETCONF_READINESS_LIFECYCLE_INVALID")
        if observed_teardown != expected_teardown:
            self._finding("NETCONF_READINESS_TEARDOWN_RPCS_INVALID")

        lifecycle_records = [
            entry for entry in rpcs if entry.get("phase") == "LIFECYCLE"]
        before_subscription = [
            int(entry.get("sequence", -1)) for entry in lifecycle_records
            if int(entry.get("lifecycleOrdinal", -1)) <= 4]
        after_subscription = [
            int(entry.get("sequence", -1)) for entry in lifecycle_records
            if int(entry.get("lifecycleOrdinal", -1)) >= 6]
        subscription_sequences = [
            int(entry.get("sequence", -1))
            for entry in self.document.get("harnessOperations", [])
            if entry.get("op") == "O1_SUBSCRIBE"]
        if (not before_subscription or not after_subscription
                or len(subscription_sequences) != 1
                or max(before_subscription) >= subscription_sequences[0]
                or subscription_sequences[0] >= min(after_subscription)):
            self._finding("NETCONF_DURABLE_BEFORE_UNLOCK_INVALID", "sequence")

        def reply_value(ordinal: int, member: str) -> str | None:
            record = next(
                (entry for entry in lifecycle_records
                 if int(entry.get("lifecycleOrdinal", -1)) == ordinal), None)
            if record is None:
                return None
            blob = self._semantic_raw(
                record.get("replyRaw"), f"readiness ordinal {ordinal} reply")
            if blob is None:
                return None
            try:
                root = ElementTree.fromstring(blob.decode("utf-8"))
            except (UnicodeDecodeError, ElementTree.ParseError):
                return None
            return next(
                ((node.text or "").strip() for node in root.iter()
                 if _local(str(node.tag)) == member), None)

        if (reply_value(4, "administrativeState") != "LOCKED"
                or reply_value(7, "administrativeState") != "UNLOCKED"
                or reply_value(7, "operationalState") != "ENABLED"):
            self._finding("NETCONF_JOB_ACTIVE_EVIDENCE_INVALID", "raw readback")

        cleanup_targets = [
            str(entry.get("target"))
            for entry in (self.document.get("cleanup") or {}).get("actions", [])]
        try:
            if not (
                    cleanup_targets.index("PERF_METRIC_JOB")
                    < cleanup_targets.index("IN_FLIGHT_WORK")
                    < cleanup_targets.index("O1_SUBSCRIPTION")
                    < cleanup_targets.index("NETCONF_SESSION")):
                self._finding(
                    "NETCONF_READINESS_TEARDOWN_ORDER_INVALID", "cleanup actions")
        except ValueError:
            self._finding(
                "NETCONF_READINESS_TEARDOWN_ORDER_INVALID",
                "cleanup action absent")

        assurance = (by_state.get("ASSURANCE_READY") or {}).get("evidence", {})
        accepted = sum(
            1 for record in self.document.get("notifications", [])
            if record.get("accepted"))
        retrieved = sum(
            1 for record in self.document.get("retrievals", [])
            if record.get("outcome") == "RETRIEVED")
        eligible = sum(
            1 for record in self.document.get("normalization", {}).get("records", [])
            if record.get("commitEligible"))
        if completed and (
                accepted < 1 or retrieved < 1 or eligible < 1
                or assurance.get("providerAcceptedInitialState") is not True
                or assurance.get("notificationsAccepted") != accepted
                or assurance.get("filesRetrieved") != retrieved
                or assurance.get("commitEligibleRecords") != eligible):
            self._finding("NETCONF_ASSURANCE_EVIDENCE_INVALID")

        self._observations["netconfReadinessConfirmedCount"] = len(confirmed)

    def _check_notifications(self) -> None:
        notifications = self.document.get("notifications", [])
        if not notifications:
            self._finding("NO_NOTIFICATION_RECORDED")
            return
        for index, notification in enumerate(notifications):
            if notification.get("accepted") and \
                    not notification.get("durablyAcceptedBeforeResponse"):
                self._finding("ACCEPTED_BEFORE_DURABLE", f"/notifications/{index}")
            blob = self._artifact(str((notification.get("raw") or {}).get(
                "artifactPath", "")))
            if blob is None:
                continue
            try:
                document = json.loads(blob.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            event_time = document.get("eventTime")
            for info in document.get("fileInfoList", []):
                if info.get("fileReadyTime") != event_time:
                    self._finding("FILE_TEMPORAL_ORDER",
                                  f"eventTime!=fileReadyTime at /notifications/{index}")
                try:
                    ready = _instant(str(info["fileReadyTime"]))
                    expires = _instant(str(info["fileExpirationTime"]))
                except (KeyError, ValueError):
                    self._finding("FILE_TEMPORAL_ORDER", "unparseable file times")
                    continue
                if not ready < expires:
                    self._finding("FILE_TEMPORAL_ORDER",
                                  "fileReadyTime not strictly before fileExpirationTime")

    def _check_retrievals(self) -> None:
        allowed = set(map(str, self.document.get("deployment", {})
                          .get("resolvedRoots", {}).get("sftpAuthorities", [])))
        for index, retrieval in enumerate(self.document.get("retrievals", [])):
            if retrieval.get("outcome") != "RETRIEVED":
                continue
            if not retrieval.get("hostKeyVerified"):
                self._finding("TRUST_DOWNGRADE", f"/retrievals/{index} unpinned host key")
            if not retrieval.get("authorityAllowed"):
                self._finding("FORBIDDEN_EGRESS",
                              f"/retrievals/{index} authority not allowed")
            authority = str(retrieval.get("requestedAuthority"))
            if allowed and authority not in allowed:
                self._finding("FORBIDDEN_EGRESS", f"{authority} not on the allowlist")
            source = retrieval.get("sourceOfLocation")
            if source not in ("CAPTURED_FILE_LOCATION", "FILES_LIST_SELECTION"):
                self._finding("RETRIEVAL_SOURCE_UNDECLARED", str(source))
            # RULE-O1-FILE-TEMPORAL-ORDER: fileReadyTime <= retrievedAt < expiration
            for notification in self.document.get("notifications", []):
                for info in notification.get("fileInfoList", []):
                    if str(info.get("fileLocation")) != str(
                            retrieval.get("requestedLocation")):
                        continue
                    try:
                        ready = _instant(str(info["fileReadyTime"]))
                        expires = _instant(str(info["fileExpirationTime"]))
                        completed = _instant(str(retrieval["completedAt"]))
                    except (KeyError, ValueError):
                        continue
                    if not (ready <= completed < expires):
                        self._finding(
                            "FILE_TEMPORAL_ORDER",
                            f"retrievedAt outside [ready, expiration) at /retrievals/{index}")

    def _rederive_normalization(self) -> None:
        """Parse the stored PM bytes with this module's own parser."""
        profile = self._bundle_json("oran-aic-o1-pa-file.1.0.0.json")
        namespace = profile["delivery"]["xmlNamespace"]
        root_element = profile["delivery"]["xmlRoot"]
        required = [entry for entry in profile["measurements"] if entry.get("required")]
        derived: dict[tuple[str, str, str], list[str]] = {}
        for retrieval in self.document.get("retrievals", []):
            if retrieval.get("outcome") != "RETRIEVED":
                continue
            path = str(retrieval.get("rawArtifactPath", ""))
            blob = self._artifact(path)
            if blob is None:
                continue
            try:
                tree = ElementTree.fromstring(blob.decode("utf-8"))
            except (UnicodeDecodeError, ElementTree.ParseError) as exc:
                self._finding("PM_DOCUMENT_UNPARSEABLE", f"{path} {exc}"[:120])
                continue
            if tree.tag != f"{{{namespace}}}{root_element}":
                self._finding("PM_PROFILE_DRIFT",
                              f"{path} root {tree.tag} is not the declared "
                              f"{{{namespace}}}{root_element}")
                continue
            meas_info = next(
                (node for node in tree.iter() if _local(node.tag) == "measInfo"), None)
            if meas_info is None:
                self._finding("PM_DOCUMENT_UNPARSEABLE", f"{path} has no measInfo")
                continue
            positions = {
                str(node.get("p")): (node.text or "").strip()
                for node in meas_info if _local(node.tag) == "measType"}
            for block in (node for node in meas_info if _local(node.tag) == "measValue"):
                dn = str(block.get("measObjLdn"))
                values = {str(node.get("p")): (node.text or "").strip()
                          for node in block if _local(node.tag) == "r"}
                for measurement in required:
                    name = str(measurement["name"])
                    position = next(
                        (key for key, value in positions.items() if value == name), None)
                    if position is None:
                        continue
                    derived.setdefault(
                        (str(retrieval["byteSha256"]), dn, name), []
                    ).append(values.get(position, ""))
        for key, texts in derived.items():
            if len(texts) > 1:
                self._finding("DN_BIJECTION",
                              f"{key[1]}/{key[2]} appears {len(texts)} times in one "
                              "PM file")
        self._observations["independentlyDerivedSamples"] = sum(
            len(texts) for texts in derived.values())
        for index, record in enumerate(self.document.get("normalization", {})
                                       .get("records", [])):
            key = (str(record.get("sourceFileSha256")),
                   str(record.get("measuredObjectDn")),
                   str(record.get("measurementName")))
            if key not in derived:
                self._finding("NORMALIZED_RECORD_UNLINKED",
                              f"/normalization/records/{index}")
                continue
            texts = derived[key]
            text = texts[0]
            value = record.get("value")
            if value is None:
                if text and text.upper() not in ("NIL", ""):
                    self._finding("NORMALIZED_VALUE_MISMATCH",
                                  f"/normalization/records/{index} null vs {text}")
            else:
                try:
                    if all(abs(float(candidate) - float(value)) > 1e-9
                           for candidate in texts if candidate):
                        self._finding(
                            "NORMALIZED_VALUE_MISMATCH",
                            f"/normalization/records/{index} {text} vs {value}")
                except ValueError:
                    self._finding("NORMALIZED_VALUE_MISMATCH",
                                  f"/normalization/records/{index} {text!r}")

    def _check_rules(self, declared: Sequence[str]) -> None:
        applied = [entry.get("ruleId") for entry in self.document.get("appliedRules", [])]
        if sorted(applied) != sorted(declared):
            self._finding(
                "APPLIED_RULES_MISMATCH",
                f"applied={sorted(applied)} declared={sorted(declared)}"[:220])
        self._observations["appliedRuleCount"] = len(applied)

    def _golden_sample_values(self) -> set[str]:
        values: set[str] = set()
        for name in ("valid-prb.xml", "null-prb.xml", "suspect-prb.xml"):
            path = self.bundle_path / "golden" / "o1" / name
            if not path.is_file():
                continue
            tree = ElementTree.fromstring(path.read_text(encoding="utf-8"))
            for node in tree.iter():
                if _local(node.tag) == "r" and node.text is not None:
                    values.add(node.text.strip())
        return values

    def _check_live_value_invariants(self) -> None:
        profile = self._bundle_json("oran-aic-o1-pa-file.1.0.0.json")
        by_name = {entry["name"]: entry for entry in profile["measurements"]}
        golden = self._golden_sample_values()
        collisions = 0
        for index, record in enumerate(self.document.get("normalization", {})
                                       .get("records", [])):
            name = str(record.get("measurementName"))
            measurement = by_name.get(name)
            if measurement is None:
                self._finding("LIVE_VALUE_INVARIANT",
                              f"/normalization/records/{index} unknown measurement {name}")
                continue
            if record.get("unit") != measurement.get("unit"):
                self._finding("LIVE_VALUE_INVARIANT",
                              f"/normalization/records/{index} unit")
            if record.get("aggregation") != measurement.get("aggregation"):
                self._finding("LIVE_VALUE_INVARIANT",
                              f"/normalization/records/{index} aggregation")
            value = record.get("value")
            if value is None:
                continue
            minimum = measurement.get("minimum")
            maximum = measurement.get("maximum")
            if minimum is not None and value < minimum:
                self._finding("LIVE_VALUE_OUT_OF_RANGE",
                              f"/normalization/records/{index} {value}<{minimum}")
            if maximum is not None and value > maximum:
                self._finding("LIVE_VALUE_OUT_OF_RANGE",
                              f"/normalization/records/{index} {value}>{maximum}")
            rendered = str(int(value)) if float(value).is_integer() else f"{value}"
            if rendered in golden or f"{value}" in golden:
                collisions += 1
                self._finding("LIVE_VALUE_EQUALS_GOLDEN_SAMPLE",
                              f"/normalization/records/{index} {rendered}")
            if record.get("commitEligible") and record.get("quality") != "OK":
                self._finding("QUALITY_PRECEDENCE",
                              f"/normalization/records/{index} commit-eligible but "
                              f"{record.get('quality')}")
            if not record.get("dnBijective") and record.get("commitEligible"):
                self._finding("DN_BIJECTION",
                              f"/normalization/records/{index} committed a "
                              "non-bijective DN")
            if not record.get("dnBijective") and record.get("quality") != "AMBIGUOUS":
                self._finding("DN_BIJECTION",
                              f"/normalization/records/{index} non-bijective DN was not "
                              "quarantined as AMBIGUOUS")
        self._observations["goldenValueCollisions"] = collisions

    def _check_time_relations(self) -> None:
        profile = self._bundle_json("oran-aic-o1-pa-file.1.0.0.json")
        limit = int(profile["time"]["evidenceFreshnessLimitMs"])
        for index, record in enumerate(self.document.get("normalization", {})
                                       .get("records", [])):
            try:
                start = _instant(str(record["window"]["start"]))
                end = _instant(str(record["window"]["end"]))
                ready = _instant(str(record["readyAt"]))
                retrieved = _instant(str(record["retrievedAt"]))
            except (KeyError, ValueError):
                self._finding("TIME_RELATIONS", f"/normalization/records/{index} unparseable")
                continue
            if not start < end:
                self._finding("TIME_RELATIONS",
                              f"/normalization/records/{index} window.start !< window.end")
            if str(record.get("observedAt")) != str(record["window"]["end"]):
                self._finding("TIME_RELATIONS",
                              f"/normalization/records/{index} observedAt != window.end")
            if ready < end or retrieved < end:
                self._finding("TIME_RELATIONS",
                              f"/normalization/records/{index} readyAt/retrievedAt "
                              "before window.end")
            expected_ingest = int((retrieved - end).total_seconds() * 1000)
            if int(record.get("ingestLatencyMs", -1)) != expected_ingest:
                self._finding("TIME_RELATIONS",
                              f"/normalization/records/{index} ingestLatencyMs")
            if int(record.get("measurementAgeMs", 0)) > limit and \
                    record.get("quality") == "OK":
                self._finding("QUALITY_PRECEDENCE",
                              f"/normalization/records/{index} stale but OK")

    def _check_coordinator(self, expected: Mapping[str, Any]) -> None:
        coordinator = self.document.get("coordinator", {})
        if not expected.get("requiresRealCoordinatorExecution"):
            return
        if coordinator.get("executionMode") != "REAL_PROCESS_INTENT":
            self._finding("SYNTHETIC_COORDINATOR",
                          f"executionMode={coordinator.get('executionMode')}")
        if int(coordinator.get("syntheticTransitionCount", 0)) != 0:
            self._finding("SYNTHETIC_COORDINATOR",
                          f"syntheticTransitionCount="
                          f"{coordinator.get('syntheticTransitionCount')}")
        declared_calls = expected.get("processIntentCalls")
        if declared_calls is not None and \
                int(coordinator.get("processIntentCallsDelta", -1)) != int(declared_calls):
            self._finding(
                "COORDINATOR_CALL_COUNT",
                f"delta={coordinator.get('processIntentCallsDelta')} "
                f"oracle={declared_calls}")
        history = coordinator.get("fsmHistory", [])
        if not history:
            self._finding("SYNTHETIC_COORDINATOR", "empty fsmHistory")
        for entry in history:
            if entry.get("origin") != "REAL":
                self._finding("SYNTHETIC_COORDINATOR",
                              f"origin={entry.get('origin')}")
        if not coordinator.get("ledgerReferences"):
            self._finding("SYNTHETIC_COORDINATOR", "empty ledgerReferences")
        if not coordinator.get("terminalEvidenceRef"):
            self._finding("SYNTHETIC_COORDINATOR", "absent terminalEvidenceRef")
        terminal = expected.get("coordinatorState")
        if terminal and history:
            if history[-1].get("to") != terminal:
                self._finding("COORDINATOR_TERMINAL_STATE",
                              f"{history[-1].get('to')} != {terminal}")
        supplied = coordinator.get("evidenceRecordsSuppliedFromNormalization", [])
        digests = {record.get("recordJcsSha256")
                   for record in self.document.get("normalization", {}).get("records", [])}
        for digest in supplied:
            if digest not in digests:
                self._finding("COORDINATOR_EVIDENCE_SUBSTITUTED", str(digest)[:16])

    def _check_stubs(self, expected: Mapping[str, Any]) -> None:
        stubs = self.document.get("deterministicStubs", {})
        if stubs.get("routing") == "UNRESOLVED" and \
                self.document["run"].get("disposition") != "ABORTED_GATE":
            self._finding("STUB_ROUTING_UNRESOLVED", str(stubs.get("routing")))
        for entry in stubs.get("writeLedger", []):
            if entry.get("live"):
                self._finding("LIVE_RAN_WRITE", str(entry))
        declared = expected.get("normalRanWrites")
        if declared is not None and int(stubs.get("normalRanWrites", -1)) != int(declared):
            self._finding("RAN_WRITE_COUNT",
                          f"{stubs.get('normalRanWrites')} != {declared}")
        rollback = expected.get("rollbackRanWrites")
        if rollback is not None and \
                int(stubs.get("rollbackRanWrites", -1)) != int(rollback):
            self._finding("RAN_WRITE_COUNT",
                          f"rollback {stubs.get('rollbackRanWrites')} != {rollback}")

    def _check_counters(self, expected: Mapping[str, Any],
                        scenario: Mapping[str, Any]) -> None:
        normalization = self.document.get("normalization", {})
        committed = expected.get("committedEvidenceRecords")
        if committed is not None and \
                int(normalization.get("commitEligibleCount", -1)) != int(committed):
            self._finding("COMMITTED_EVIDENCE_COUNT",
                          f"{normalization.get('commitEligibleCount')} != {committed}")
        pushes = expected.get("dmePushDeliveries")
        if pushes is not None:
            observed = sum(
                1 for exchange in self.document.get("exchanges", [])
                if str(exchange.get("stepId", "")).startswith("dme-push"))
            if observed != int(pushes):
                self._finding("DME_PUSH_COUNT", f"{observed} != {pushes}")
        sequence = expected.get("httpSequence")
        if sequence:
            observed = self._observed_http_sequence(scenario)
            self._observations["observedHttpSequence"] = observed
            if observed != [int(value) for value in sequence]:
                self._finding("HTTP_SEQUENCE",
                              f"{observed} != {list(sequence)}")
        declared_steps = len(scenario["materialization"]["steps"])
        if int(self.document.get("scenario", {})
               .get("declaredStepCount", -1)) != declared_steps:
            self._finding("STEP_COUNT_MISREAD",
                          f"{self.document.get('scenario', {}).get('declaredStepCount')}"
                          f" != {declared_steps}")
        step_ids = [str(step["id"]) for step in scenario["materialization"]["steps"]]
        for exchange in self.document.get("exchanges", []):
            if str(exchange.get("stepId")) not in step_ids:
                self._finding("STEP_ID_NOT_IN_CATALOG", str(exchange.get("stepId")))
        for exchange in self.document.get("exchanges", []):
            declared_status = exchange.get("declaredExpectedHttpStatus")
            step = next((entry for entry in scenario["materialization"]["steps"]
                         if str(entry["id"]) == str(exchange.get("stepId"))), None)
            if step is None:
                continue
            catalog_status = step.get("expectedHttpStatus")
            if catalog_status is not None and declared_status is not None and \
                    int(catalog_status) != int(declared_status):
                self._finding("STEP_EXPECTED_STATUS_MISREAD",
                              f"{exchange.get('stepId')} {declared_status} != "
                              f"{catalog_status}")

    def _check_upper_exchange_coverage(self) -> None:
        """Independently derive the exact upper network/capture vector.

        The runtime's completion predicate is not imported.  This verifier
        reads the packaged role table beside its gate specification and checks
        identity, order, direction and role from those bytes.
        """
        role_path = self.gates_path.parent / "role-table.1.0.0.json"
        try:
            table = json.loads(role_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._finding("UPPER_ROLE_TABLE_UNREADABLE", str(exc)[:160])
            return
        exchange_ops = {
            "HTTP", "R1_DME_QUERY", "R1_DME_DATA_JOB", "R1_DME_PUBLISH",
            "A1_EMIT_STATUS", "O1_NOTIFY",
        }
        expected: list[tuple[int, str, str, str]] = []
        expected_notifications = 0
        for row in table.get("steps", []):
            if not isinstance(row, Mapping):
                self._finding("UPPER_ROLE_TABLE_MALFORMED")
                return
            obligation = str(row.get("upperObligation", ""))
            operation = str(row.get("op", ""))
            if obligation == "REQUIRED_SURFACE" and operation == "O1_NOTIFY":
                expected_notifications += 1
                continue
            if (obligation not in {"REQUIRED_SURFACE", "REQUIRED_CLIENT"}
                    or operation not in exchange_ops):
                continue
            step_id = str(row.get("id", ""))
            try:
                step_index = int(str(row.get("catalogPointer", "")).rsplit(
                    "/", 1)[1])
            except (IndexError, ValueError):
                self._finding("UPPER_ROLE_TABLE_MALFORMED", step_id)
                return
            self_relay = (
                step_id == "r1-status-to-rapp"
                and str(row.get("actor")) == "NON_RT_RIC_FRAMEWORK"
                and str(row.get("hostOwner")) == "UPPER_HARNESS")
            outbound = obligation == "REQUIRED_CLIENT" or self_relay
            expected.append((
                step_index, step_id,
                "UPPER_OUTBOUND" if outbound else "UPPER_INBOUND",
                "CLIENT" if outbound else "SERVER"))
        observed: list[tuple[int, str, str, str]] = []
        exchanges = self.document.get("exchanges")
        if isinstance(exchanges, list):
            for row in exchanges:
                if not isinstance(row, Mapping):
                    self._finding("UPPER_EXCHANGE_COVERAGE", "malformed row")
                    return
                try:
                    observed.append((
                        int(row.get("stepIndex")), str(row.get("stepId")),
                        str(row.get("direction")), str(row.get("role"))))
                except (TypeError, ValueError):
                    self._finding("UPPER_EXCHANGE_COVERAGE", "malformed identity")
                    return
        if observed != expected:
            self._finding(
                "UPPER_EXCHANGE_COVERAGE",
                "observed=%s expected=%s" % (observed, expected))
        accepted_notifications = [
            row for row in self.document.get("notifications", [])
            if isinstance(row, Mapping)
            and row.get("accepted") is True
            and row.get("responseStatus") == 204
            and row.get("durablyAcceptedBeforeResponse") is True
            and row.get("subscriptionIdMatched") is True]
        if len(accepted_notifications) != expected_notifications:
            self._finding(
                "UPPER_NOTIFICATION_COVERAGE",
                "%d/%d" % (len(accepted_notifications), expected_notifications))
        self._observations["upperExchangeCount"] = len(observed)

    def _observed_http_sequence(self, scenario: Mapping[str, Any]) -> list[int]:
        """Compose the observed statuses by walking the frozen step vector.

        A status can land in ``/exchanges`` (the upper made or served the call)
        or in ``/notifications`` (the Provider called the upper's O1 consumer).
        Ordering comes from the catalog, never from either array's own order.
        """
        by_step: dict[str, int] = {}
        for exchange in self.document.get("exchanges", []):
            status = (exchange.get("response") or {}).get("status")
            if status is not None:
                by_step.setdefault(str(exchange.get("stepId")), int(status))
        notifications = [record for record in self.document.get("notifications", [])
                         if record.get("accepted")]
        observed: list[int] = []
        for step in scenario["materialization"]["steps"]:
            declared = step.get("expectedHttpStatus")
            if declared is None:
                declared = step.get("callbackExpectedHttpStatus")
            step_id = str(step["id"])
            if step.get("op") == "O1_NOTIFY":
                if notifications:
                    observed.append(int(notifications[0]["responseStatus"]))
                continue
            if declared is None:
                continue
            if step_id in by_step:
                observed.append(by_step[step_id])
        return observed

    def _check_cleanup(self) -> None:
        cleanup = self.document.get("cleanup", {})
        residual = cleanup.get("residual", [])
        if residual:
            self._finding("INCOMPLETE_CLEANUP", ",".join(map(str, residual))[:160])
        if cleanup.get("complete") != (not residual):
            self._finding("INCOMPLETE_CLEANUP", "complete disagrees with residual")
        if cleanup.get("path") not in ("NORMAL", "FAILURE"):
            self._finding("INCOMPLETE_CLEANUP", f"path={cleanup.get('path')}")
        if not cleanup.get("actions"):
            self._finding("INCOMPLETE_CLEANUP", "no cleanup action recorded")

    def _check_egress(self) -> None:
        external = self.document.get("externalCalls", {})
        methods = set(external.get("guardMethods", []))
        for required in ("SOCKET_CONNECT", "SUBPROCESS_SPAWN", "SSH_TRANSPORT_OPEN"):
            if required not in methods:
                self._finding("EGRESS_GUARD_UNDER_ARMED", required)
        if external.get("violations"):
            self._finding("FORBIDDEN_EGRESS",
                          json.dumps(external["violations"])[:160])
        if int(external.get("hardwareCalls", 0)) != 0:
            self._finding("HARDWARE_CALL", str(external.get("hardwareCalls")))
        # The two counters answer different questions and are both required:
        # a capture that omits either is not adjudicable, and an omission must
        # not read as a zero.
        for name in ("hardwareCalls", "externalLiveTargetCalls"):
            if name not in external:
                self._finding("EGRESS_COUNTER_ABSENT", name)
        if int(external.get("externalLiveTargetCalls", 0)) != 0:
            self._finding("EXTERNAL_LIVE_TARGET_CALL",
                          str(external.get("externalLiveTargetCalls")))
        counted = sum(1 for item in external.get("violations", [])
                      if str(item.get("classification")) == "EXTERNAL_LIVE_TARGET")
        if "externalLiveTargetCalls" in external and \
                int(external["externalLiveTargetCalls"]) != counted:
            self._finding(
                "EXTERNAL_LIVE_TARGET_CALL",
                "the declared count %s disagrees with the %d EXTERNAL_LIVE_TARGET "
                "violations in the ledger"
                % (external["externalLiveTargetCalls"], counted))
        allowlist = set(map(str, external.get("authorityAllowlist", [])))
        for entry in external.get("targetLedger", []):
            authority = str(entry.get("authority"))
            if entry.get("allowed") and allowlist and authority not in allowlist:
                self._finding("FORBIDDEN_EGRESS", f"{authority} not on the allowlist")
            if entry.get("role") == "UNATTRIBUTED":
                self._finding("PEER_UNATTRIBUTED", authority)
        self._observations["externalTargets"] = len(external.get("targetLedger", []))

    def _prohibited_claim_digests(self) -> set[str]:
        gates = json.loads(self.gates_path.read_text(encoding="utf-8"))
        return set(gates.get("prohibitedClaimDigests", {}).get("digests", []))

    def _check_labels_and_claims(self) -> None:
        prohibited = self._prohibited_claim_digests()
        hits = 0
        scanned = 0
        for path in sorted(self.capture_root.rglob("*")):
            if not path.is_file():
                continue
            scanned += 1
            blob = path.read_bytes()
            for token in _CLAIM_TOKEN.findall(blob):
                text = token.strip()
                for length in range(5, min(len(text), 48) + 1):
                    for start in range(0, len(text) - length + 1):
                        window = text[start:start + length]
                        if _sha256(window) in prohibited:
                            hits += 1
                            self._finding("PROHIBITED_CLAIM_STRING",
                                          str(path.relative_to(self.capture_root)))
                            break
                    else:
                        continue
                    break
        self._observations["claimScanFilesScanned"] = scanned
        self._observations["prohibitedClaimHits"] = hits
        if self.report is not None and \
                self.report.get("stateLabel") != SELF_TEST_STATE_LABEL:
            self._finding("SELF_TEST_LABEL_MISSING", str(self.report.get("stateLabel")))

    def _check_key_material(self) -> None:
        hits = 0
        for path in sorted(self.capture_root.rglob("*")):
            if not path.is_file():
                continue
            blob = path.read_bytes()
            if _PRIVATE_KEY_BEGIN.search(blob) and _PRIVATE_KEY_END.search(blob):
                hits += 1
                self._finding("PRIVATE_KEY_BLOCK_IN_EVIDENCE",
                              str(path.relative_to(self.capture_root)))
            for marker in _CREDENTIAL_MARKERS:
                if marker in blob:
                    self._finding("CREDENTIAL_MARKER_IN_EVIDENCE",
                                  str(path.relative_to(self.capture_root)))
                    break
        self._observations["privateKeyBlocksInEvidence"] = hits


def verify_from_raw_evidence(capture_root: Path, *, bundle_path: Path) -> dict[str, Any]:
    """Adjudicate a bundle, locating the design artefacts beside the bundle path."""
    capture_root = Path(capture_root)
    repo_root = Path(bundle_path).resolve().parents[2].parent
    design = repo_root / "spec"
    if not design.is_dir():
        design = repo_root / "docs" / "upper-live-o1-harness"
    verifier = IndependentVerifier(
        capture_path=capture_root,
        bundle_path=Path(bundle_path),
        gates_path=design / "release-gates.1.0.0.json",
        capture_schema_path=design / "capture-schema.2.0.0.json",
    )
    return verifier.readjudicate()
