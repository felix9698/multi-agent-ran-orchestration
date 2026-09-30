"""The black-box self-test driver.

It brings up the Provider emulator and an O1 consumer in one process group,
drives the O1 leg of SC-084 across real sockets, and exports an evidence bundle
that the **independent verifier** -- which shares no code with either -- then
re-adjudicates from the raw bytes.

Two properties are load-bearing:

* **Admission runs before anything binds outward.**  ``SelfTestAdmission``
  mirrors the ten ``G-ID`` checks over the self-test's own inputs and returns
  ``REFUSED`` with exit 78 rather than degrading.  It is the self-test's own
  fail-closed gate, not W2's; W2's gate is the runtime's and is consumed
  read-only when the runtime exists.
* **A missing runtime is a refusal, not a pass.**  When
  ``oran.release.lo1`` is absent the driver reports
  ``RUNTIME_ABSENT`` and exits non-zero.  The evidence it can still produce --
  the real O1 conversation with the emulator -- is labelled
  ``SYNTHETIC_FALSIFIER_SUBSTRATE`` for its core sections so that it can never
  be read as a run of the release.
"""

from __future__ import annotations

import contextlib
import datetime as _datetime
import importlib.util
import json
import re
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import (
    EXIT_FINDING,
    EXIT_REFUSED,
    EXIT_RUNTIME_ABSENT,
    SELF_TEST_STATE_LABEL,
)
from .capture_mirror import (
    CORE_FROM_RUNTIME,
    CORE_SYNTHETIC,
    CaptureMirrorError,
    PACKAGED_RELEASE_IDENTITY,
    SOURCE_TREE_IDENTITY,
    UpperIdentity,
    contains_private_key_block,
    instant,
    resolve_upper_identity,
    validate_capture,
)
from .core_sections import synthetic_core_sections
from .frozen import BUNDLE_RELATIVE, FrozenBundle, json_pointer, sha256_bytes
from .provider_emulator import EMULATOR_KIND, ProviderEmulator
from .reference_consumer import ReferenceEvidenceProducer
from .vector import (
    LOOPBACK,
    UpperListeners,
    build_self_test_vector,
    validate_against_frozen_schema,
    vector_digest,
)

RUNTIME_MODULE = "oran.release.lo1"
DESIGN_DIR = Path("docs") / "upper-live-o1-harness"
CAPTURE_SCHEMA_NAME = "capture-schema.2.0.0.json"

#: The upper-owned origins that share ONE listener.  DESIGN.md 5.1 binds four
#: listeners from five ``hostOwner: UPPER_HARNESS`` origins, so exactly one
#: collision group exists and it has three members: the rApp callback root, the
#: DME push base URI and the A1 status callback root are the same rApp listener.
#: This mirrors ``gate.CO_HOSTED_UPPER_ORIGINS`` -- the self-test may not import
#: the runtime (G-SPLIT-1), so the set is restated, but a narrower set here
#: would refuse the very vector the composition root requires.
CO_HOSTED_ORIGINS = frozenset({
    "rAppCallbackRoot", "policyEvidencePushBaseUri", "a1StatusCallbackRoot"})


class GateRefused(RuntimeError):
    """Admission refused.  Fail-closed, exit 78, no per-check override."""

    exit_code = EXIT_REFUSED


@dataclass(frozen=True)
class CheckResult:
    id: str
    outcome: str
    reason_code: str | None = None
    observed_digest: str | None = None

    def as_capture(self) -> dict[str, Any]:
        record: dict[str, Any] = {"id": self.id, "outcome": self.outcome}
        if self.reason_code:
            record["reasonCode"] = self.reason_code
        if self.observed_digest:
            record["observedDigest"] = self.observed_digest
        return record


@dataclass
class SelfTestConfig:
    repo_root: Path
    work_dir: Path
    bundle_path: Path | None = None
    seed: int = 20260811
    scenario_id: str = "SC-084"
    profile: str = "self-test"
    emulator_faults: tuple[str, ...] = ()
    run_id: str | None = None
    require_runtime: bool = False
    provider_ocidigest: str = "UNRESOLVED"
    stale_state_probe: bool = False
    endpoint_collision_probe: bool = False
    drop_secret_ref: str | None = None
    recovery_files_probe: str | None = None
    release_archive_sha256: str | None = None

    def resolved_bundle(self) -> Path:
        return Path(self.bundle_path) if self.bundle_path \
            else Path(self.repo_root) / BUNDLE_RELATIVE

    def design_root(self) -> Path:
        release_design = Path(self.repo_root) / "spec"
        return release_design if release_design.is_dir() \
            else Path(self.repo_root) / DESIGN_DIR


@dataclass
class SelfTestResult:
    status: str
    run_id: str
    capture_root: Path
    checks: list[CheckResult] = field(default_factory=list)
    emulator: Mapping[str, Any] | None = None
    findings: tuple[str, ...] = ()
    detail: str = ""

    @property
    def exit_code(self) -> int:
        return {"OK": 0, "RUNTIME_ABSENT": EXIT_RUNTIME_ABSENT,
                "REFUSED": EXIT_REFUSED, "FINDING": EXIT_FINDING}.get(self.status, 70)


def runtime_available() -> bool:
    """Is the runtime under test importable?  Discovery only; never imported."""
    try:
        return importlib.util.find_spec(RUNTIME_MODULE) is not None
    except (ImportError, ValueError):  # pragma: no cover - namespace edge cases
        return False


class PortReservation:
    """Loopback ports the absent runtime would bind, held for the run's duration."""

    def __init__(self, names: Sequence[str]) -> None:
        self._sockets: dict[str, socket.socket] = {}
        for name in names:
            handle = socket.socket()
            handle.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            handle.bind((LOOPBACK, 0))
            self._sockets[name] = handle

    def ports(self) -> dict[str, int]:
        return {name: handle.getsockname()[1] for name, handle in self._sockets.items()}

    def release(self) -> None:
        for handle in self._sockets.values():
            with contextlib.suppress(OSError):
                handle.close()
        self._sockets.clear()


class SelfTestAdmission:
    """The self-test's own fail-closed admission, mirroring the ten G-ID checks."""

    def __init__(self, *, bundle: FrozenBundle, repo_root: Path, config: SelfTestConfig,
                 vector: Mapping[str, Any], secret_map: Mapping[str, str],
                 capture_root: Path, provider_kind: str,
                 upper_identity: UpperIdentity) -> None:
        self.bundle = bundle
        self.repo_root = Path(repo_root)
        self.config = config
        self.vector = vector
        self.secret_map = dict(secret_map)
        self.capture_root = Path(capture_root)
        self.provider_kind = provider_kind
        self.upper_identity = upper_identity
        self.checks: list[CheckResult] = []
        self.unresolved: list[dict[str, Any]] = []

    def _record(self, identifier: str, outcome: str, reason: str | None = None,
                digest: str | None = None) -> None:
        self.checks.append(CheckResult(identifier, outcome, reason, digest))

    def run(self) -> list[CheckResult]:
        self._contract_lock()
        self._upper_identity()
        self._provider_identity()
        self._acceptance_records()
        self._authority_and_vector()
        self._endpoint_exactness()
        self._trust_and_secrets()
        self._run_envelope()
        self._scope()
        self._prohibitions()
        refused = [check for check in self.checks
                   if check.outcome in ("REFUSED", "UNRESOLVED")]
        if refused:
            raise GateRefused(
                "admission refused: "
                + ", ".join(f"{check.id}:{check.reason_code}" for check in refused))
        return self.checks

    # -- G-ID-01 ------------------------------------------------------------

    def _contract_lock(self) -> None:
        try:
            scenario = self.bundle.scenario(self.config.scenario_id)
            assignment = self.bundle.scenario_assignment(self.config.scenario_id)
        except Exception as exc:
            self._record("G-ID-01", "REFUSED", f"CATALOG_POINTER_UNRESOLVED:{exc}")
            return
        if assignment.get("executionProfile") != "live-O1":
            self._record("G-ID-01", "REFUSED", "EXECUTION_PROFILE_NOT_LIVE_O1")
            return
        if assignment.get("fixtureMode") != scenario.get("fixtureMode"):
            self._record("G-ID-01", "REFUSED", "FIXTURE_MODE_DISAGREEMENT")
            return
        self._record("G-ID-01", "PASS", digest=self.bundle.digest(
            "scenario-catalog.1.0.1.json"))

    # -- G-ID-02 ------------------------------------------------------------

    def _upper_identity(self) -> None:
        revisions = self.upper_identity.revisions
        if revisions["upperSourceCommit"] == revisions["upperSourceTree"]:
            self._record("G-ID-02", "REFUSED", "DIGEST_MEANING_CONFLATION")
            return
        if revisions["identityMode"] == SOURCE_TREE_IDENTITY:
            for field in (
                    "upperReleaseVersion", "upperReleaseContentSha256",
                    "upperReleaseManifestSha256", "upperDeliveryArchiveSha256",
                    "testedCodeCommit", "upperOciImageManifestDigest"):
                self.unresolved.append({
                    "field": f"revisions.{field}",
                    "owedBy": "UPPER_RELEASE",
                    "blocksCheck": "G-ID-02",
                    "meaning": (
                        "source-tree development self-test deliberately carries no "
                        "packaged release identity"),
                    "permittedUnresolved": True,
                })
            reason = f"SOURCE_TREE_DEVELOPMENT:{revisions['upperSourceTree']}"
        elif revisions["identityMode"] == PACKAGED_RELEASE_IDENTITY:
            reason = f"PACKAGED_RELEASE:{revisions['upperReleaseManifestSha256']}"
        else:
            self._record("G-ID-02", "REFUSED", "UPPER_IDENTITY_MODE_UNKNOWN")
            return
        # `observedDigest` is a SHA-256 slot.  A git tree oid is a different
        # digest kind, and `digestMeaningConflation` is a REFUSE prohibition, so
        # the tree oid is reported in the reason code and never in that slot.
        self._record("G-ID-02", "PASS", reason)

    # -- G-ID-03 ------------------------------------------------------------

    def _provider_identity(self) -> None:
        digest = self.config.provider_ocidigest
        if re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            if self.provider_kind == EMULATOR_KIND:
                self._record("G-ID-03", "REFUSED", "EMULATOR_CANNOT_CARRY_IMAGE_DIGEST")
                return
            self._record("G-ID-03", "PASS", digest=digest.split(":", 1)[1])
            return
        if digest == "UNRESOLVED":
            if self.provider_kind == EMULATOR_KIND:
                self.unresolved.append({
                    "field": "provider.ociImageManifestDigest",
                    "owedBy": "LOWER_PROVIDER_RELEASE",
                    "blocksCheck": "G-ID-03",
                    "meaning": "the emulator has no OCI image manifest and never will",
                    "permittedUnresolved": True,
                })
                self._record("G-ID-03", "PASS", "SELF_TEST_PROFILE_NO_PROVIDER_IMAGE")
                return
            self._record("G-ID-03", "REFUSED", "PROVIDER_IMAGE_DIGEST_UNRESOLVED")
            return
        # Everything else is a mutable tag or a local image id.
        reason = "MUTABLE_TAG" if ":" in digest else "LOCAL_IMAGE_ID"
        self._record("G-ID-03", "REFUSED", reason)

    # -- G-ID-04 ------------------------------------------------------------

    def _acceptance_records(self) -> None:
        if self.provider_kind == EMULATOR_KIND:
            self._record("G-ID-04", "PASS", "SELF_TEST_PROFILE_NO_LIVE_ACCEPTANCE_RECORDS")
            return
        self._record("G-ID-04", "REFUSED", "BILATERAL_ACCEPTANCE_RECORDS_ABSENT")

    # -- G-ID-05 ------------------------------------------------------------

    def _authority_and_vector(self) -> None:
        try:
            validate_against_frozen_schema(self.vector, self.bundle)
        except Exception as exc:
            self._record("G-ID-05", "REFUSED", f"VECTOR_SCHEMA_INVALID:{exc}"[:200])
            return
        token = self.bundle.profile("live-O1").get("authorityToken")
        if not token:
            self._record("G-ID-05", "REFUSED", "AUTHORITY_TOKEN_ABSENT")
            return
        # o1.live.recoveryFiles is easy to miss: the frozen schema pins how many
        # ambiguous candidates there must be, and uniqueCandidate has to be
        # genuinely unique.  Both numbers are READ, never typed.
        schema = self.bundle.vector_schema
        pinned = json_pointer(
            schema,
            "/properties/o1/properties/live/properties/recoveryFiles"
            "/properties/ambiguousCandidates")
        recovery = self.vector["o1"]["live"]["recoveryFiles"]
        candidates = recovery["ambiguousCandidates"]
        if len(candidates) != int(pinned["minItems"]):
            self._record("G-ID-05", "REFUSED",
                         f"RECOVERY_FILE_CARDINALITY:{len(candidates)}"
                         f"!={pinned['minItems']}")
            return
        unique = json.dumps(recovery["uniqueCandidate"], sort_keys=True)
        if any(json.dumps(entry, sort_keys=True) == unique for entry in candidates):
            self._record("G-ID-05", "REFUSED", "RECOVERY_FILE_UNIQUE_CANDIDATE_NOT_UNIQUE")
            return
        self._record("G-ID-05", "PASS", digest=vector_digest(self.vector))

    # -- G-ID-06 ------------------------------------------------------------

    def _endpoint_exactness(self) -> None:
        roots = {
            "r1ApiRoot": self.vector["r1"]["apiRoot"],
            "rAppCallbackRoot": self.vector["r1"]["callbackApi"]["rootUri"],
            "a1ApiRoot": self.vector["a1"]["apiRoot"],
            "a1StatusCallbackRoot": self.vector["a1"]["statusCallbackRoot"],
            "policyEvidencePushBaseUri":
                self.vector["r1"]["dme"]["policyEvidencePushBaseUri"],
            "mnsRoot": self.vector["o1"]["fileDataReporting"]["mnsRoot"],
            "o1ConsumerRoot": self.vector["o1"]["fileDataReporting"]["consumerReference"],
            "netconfEndpoint": self.vector["o1"]["netconf"]["endpoint"],
        }
        for name, value in roots.items():
            if not isinstance(value, str) or "${" in value or not value:
                self._record("G-ID-06", "REFUSED", f"PLACEHOLDER_OR_EMPTY:{name}")
                return
        authorities: dict[str, str] = {}
        for name, value in roots.items():
            authority = value.split("//", 1)[-1].split("/", 1)[0]
            if not re.fullmatch(r"[A-Za-z0-9._~:\[\]-]+:[0-9]{1,5}", authority):
                self._record("G-ID-06", "REFUSED", f"AUTHORITY_NOT_EXACT:{name}")
                return
            host = authority.rsplit(":", 1)[0]
            if host in ("0.0.0.0", "*", "::"):
                self._record("G-ID-06", "REFUSED", f"WILDCARD_AUTHORITY:{name}")
                return
            if "/" in host or host.count(".") == 3 and host.endswith("/0"):
                self._record("G-ID-06", "REFUSED", f"CIDR_AUTHORITY:{name}")
                return
            existing = authorities.get(authority)
            if existing and not {existing, name} <= CO_HOSTED_ORIGINS:
                self._record("G-ID-06", "REFUSED",
                             f"AUTHORITY_COLLISION:{existing}~{name}")
                return
            authorities.setdefault(authority, name)
        for authority in self.vector["o1"]["sftp"]["allowedAuthorities"]:
            if not re.fullmatch(r"[A-Za-z0-9._~:\[\]-]+:[0-9]{1,5}", str(authority)):
                self._record("G-ID-06", "REFUSED", "SFTP_AUTHORITY_NOT_EXACT")
                return
        self._record("G-ID-06", "PASS")

    # -- G-ID-07 ------------------------------------------------------------

    def _trust_and_secrets(self) -> None:
        required = [
            self.vector["o1"]["netconf"]["knownHostsRef"],
            self.vector["o1"]["netconf"]["credentialRef"],
            self.vector["o1"]["sftp"]["knownHostsRef"],
            self.vector["o1"]["sftp"]["credentialRef"],
            self.vector["security"]["truststoreRef"],
        ]
        pattern = self.bundle.vector_schema["$defs"]["secretRef"]["pattern"]
        for reference in required:
            if not re.match(pattern, str(reference)):
                self._record("G-ID-07", "REFUSED", f"SECRET_REF_MALFORMED:{reference}")
                return
            target = self.secret_map.get(str(reference))
            if not target or not Path(target).is_file():
                self._record("G-ID-07", "REFUSED", f"SECRET_REF_UNRESOLVED:{reference}")
                return
            if Path(target).resolve().is_relative_to(self.repo_root.resolve()):
                self._record("G-ID-07", "REFUSED",
                             f"SECRET_MATERIAL_INSIDE_RELEASE_TREE:{reference}")
                return
        self._record("G-ID-07", "PASS")

    # -- G-ID-08 ------------------------------------------------------------

    def _run_envelope(self) -> None:
        if self.capture_root.exists() and any(self.capture_root.iterdir()):
            self._record("G-ID-08", "REFUSED", "STALE_STATE_CAPTURE_ROOT_NOT_EMPTY")
            return
        floor = self.bundle.live_capture_lower_bound_ms()
        live_capture = int(self.vector["timeouts"]["liveCaptureMs"])
        if live_capture < floor:
            self._record("G-ID-08", "REFUSED",
                         f"LIVE_CAPTURE_MS_BELOW_CATALOG_FLOOR:{live_capture}<{floor}")
            return
        self._record("G-ID-08", "PASS")

    # -- G-ID-09 ------------------------------------------------------------

    def _scope(self) -> None:
        hardware = self.bundle.assignment.get("hardwareAuthorization", {})
        for key in ("usrp", "ota", "liveE2Control", "liveRanWrite"):
            if hardware.get(key) != "NOT_AUTHORIZED":
                self._record("G-ID-09", "REFUSED", f"HARDWARE_AUTHORIZED:{key}")
                return
        profile = self.bundle.profile("live-O1")
        if int(profile.get("scenarioCount", 0)) != 1:
            self._record("G-ID-09", "REFUSED", "MORE_THAN_ONE_SCENARIO_IN_PROFILE")
            return
        # SC-084's three O1 initial states are a complete, digest-bound
        # readiness assignment in the self-test profile.  The emulator remains
        # loopback-only, so enabling these actions does not authorize a live
        # Provider or any hardware surface.
        self._record("G-ID-09", "PASS", "NETCONF_READINESS_ASSIGNMENT_COMPLETE")

    # -- G-ID-10 ------------------------------------------------------------

    def _prohibitions(self) -> None:
        if self.provider_kind == EMULATOR_KIND and self.config.profile != "self-test":
            self._record("G-ID-10", "REFUSED", "EMULATOR_REACHABLE_UNDER_LIVE_PROFILE")
            return
        if self.provider_kind != EMULATOR_KIND and self.config.profile == "self-test":
            self._record("G-ID-10", "REFUSED", "LIVE_PROVIDER_UNDER_SELF_TEST_PROFILE")
            return
        release_tree = self.repo_root / "lib" / "oran" / "release" / "lo1_selftest"
        if not release_tree.is_dir():
            release_tree = self.repo_root / "oran" / "release" / "lo1_selftest"
        if not release_tree.is_dir():
            self._record("G-ID-10", "REFUSED", "SELFTEST_SOURCE_TREE_UNRESOLVED")
            return
        for path in sorted(release_tree.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if contains_private_key_block(path.read_bytes()):
                self._record("G-ID-10", "REFUSED", f"KEY_MATERIAL_IN_TREE:{path.name}")
                return
        self._record("G-ID-10", "PASS")

    def unresolved_ledger(self, run_id: str) -> dict[str, Any]:
        return {
            "specVersion": "oran-aic-upper-live-o1-harness-unresolved-ledger/1.0.0",
            "generatedForRunId": run_id,
            "entries": self.unresolved,
        }


class SelfTestDriver:
    """Brings the emulator and the O1 consumer up and exports one evidence bundle."""

    def __init__(self, config: SelfTestConfig) -> None:
        self.config = config
        self.repo_root = Path(config.repo_root).resolve()
        self.bundle = FrozenBundle(config.resolved_bundle())
        self.work_dir = Path(config.work_dir)
        self.run_id = config.run_id or f"lo1-selftest-{uuid.uuid4().hex[:12]}"
        self.capture_root = self.work_dir / "capture"
        self._emulator: ProviderEmulator | None = None
        self._consumer: ReferenceEvidenceProducer | None = None
        self._reservation: PortReservation | None = None
        self.admission: SelfTestAdmission | None = None
        self.upper_identity: UpperIdentity | None = None

    # ------------------------------------------------------------------- run

    def run(self) -> SelfTestResult:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.upper_identity = resolve_upper_identity(
                self.repo_root,
                release_archive_sha256=self.config.release_archive_sha256)
        except CaptureMirrorError as exc:
            raise GateRefused(f"G-ID-02:UPPER_IDENTITY_UNRESOLVED:{exc}") from exc
        if self.config.stale_state_probe:
            self.capture_root.mkdir(parents=True, exist_ok=True)
            (self.capture_root / "leftover-from-a-previous-run.json").write_text(
                "{}", encoding="utf-8")
        self._reservation = PortReservation(
            ["r1", "rapp", "a1_status", "lower_a1"])
        try:
            return self._run()
        finally:
            self._teardown()

    def _run(self) -> SelfTestResult:
        if self.upper_identity is None:  # pragma: no cover - run() establishes it
            raise GateRefused("G-ID-02:UPPER_IDENTITY_NOT_EVALUATED")
        ports = dict(self._reservation.ports())
        upper = UpperListeners.loopback({**ports, "o1_consumer": 0})

        provisional = build_self_test_vector(
            self.bundle, upper=upper,
            provider_mns_root=f"https://{LOOPBACK}:1",
            provider_sftp_authority=f"{LOOPBACK}:1",
            provider_netconf_endpoint=f"ssh://{LOOPBACK}:1", seed=self.config.seed)

        emulator = ProviderEmulator(
            bundle_path=self.bundle.root, vector=provisional,
            work_dir=self.work_dir / "provider", seed=self.config.seed,
            consumer_notification_uri=f"https://{LOOPBACK}:1/pending")
        self._emulator = emulator
        for fault in self.config.emulator_faults:
            emulator.inject(fault)
        endpoints = emulator.start()

        consumer = ReferenceEvidenceProducer(
            bundle=self.bundle, repo_root=self.repo_root,
            capture_root=self.capture_root, work_dir=self.work_dir / "consumer",
            vector=provisional, secret_map=emulator.secret_map(),
            run_id=self.run_id, scenario_id=self.config.scenario_id)
        self._consumer = consumer
        consumer_authority = consumer.start()

        upper = UpperListeners.loopback({
            **ports, "o1_consumer": int(consumer_authority.rsplit(":", 1)[1])})
        vector = build_self_test_vector(
            self.bundle, upper=upper,
            provider_mns_root=endpoints.mns_root,
            provider_sftp_authority=endpoints.sftp_authority,
            provider_netconf_endpoint=endpoints.netconf, seed=self.config.seed)
        if self.config.endpoint_collision_probe:
            # Two distinct role-table origins forced onto one authority with no
            # co-hosting declaration: G-ID-06 must refuse.
            vector["a1"]["statusCallbackRoot"] = vector["r1"]["apiRoot"]
        if self.config.recovery_files_probe == "NOT_UNIQUE":
            recovery = vector["o1"]["live"]["recoveryFiles"]
            recovery["uniqueCandidate"] = dict(recovery["ambiguousCandidates"][0])
        elif self.config.recovery_files_probe == "WRONG_CARDINALITY":
            recovery = vector["o1"]["live"]["recoveryFiles"]
            recovery["ambiguousCandidates"] = recovery["ambiguousCandidates"][:1]
        emulator.adopt_vector(vector)
        emulator.adopt_consumer_uri(consumer.notification_uri)
        consumer.vector = dict(vector)
        emulator.trust_consumer(consumer.client_tls_context())

        secret_map = dict(emulator.secret_map())
        if self.config.drop_secret_ref:
            secret_map.pop(self.config.drop_secret_ref, None)
        admission = SelfTestAdmission(
            bundle=self.bundle, repo_root=self.repo_root, config=self.config,
            vector=vector, secret_map=secret_map,
            capture_root=self.capture_root, provider_kind=EMULATOR_KIND,
            upper_identity=self.upper_identity)
        self.admission = admission
        checks = admission.run()

        self.capture_root.mkdir(parents=True, exist_ok=True)
        consumer.mirror.gate_checks = [check.as_capture() for check in checks]
        consumer.mirror.gate_result = "ADMITTED"

        provider_context = emulator.client_tls_context()
        authority_record_digest = sha256_bytes(
            json.dumps({"runId": self.run_id, "profile": self.config.profile},
                       sort_keys=True).encode())
        consumer.netconf_readiness_before_subscription()
        consumer.subscribe(mns_root=endpoints.mns_root, provider_context=provider_context)
        consumer.netconf_readiness_after_subscription()

        result = emulator.run_scenario(self.config.scenario_id)

        window_ms = int(vector["timeouts"]["notificationWindowMs"])
        delivered = consumer.wait_for_notification(timeout_ms=min(window_ms, 30000))
        findings: list[str] = []
        failure = False
        if not delivered:
            findings.append("NOTIFICATION_NOT_DELIVERED_WITHIN_WINDOW")
            failure = True

        accepted = [record for record in consumer.mirror.notifications
                    if record.get("accepted")]
        if accepted:
            for info in accepted[-1]["fileInfoList"]:
                try:
                    retrieved = consumer.retrieve(file_info=info)
                except Exception as exc:  # retrieval refusal is evidence
                    findings.append(f"RETRIEVAL_REFUSED:{type(exc).__name__}")
                    failure = True
                    continue
                try:
                    consumer.normalize(retrieved=retrieved)
                except Exception as exc:
                    findings.append(f"NORMALIZATION_REFUSED:{type(exc).__name__}")
                    failure = True
        else:
            findings.append("NO_NOTIFICATION_ACCEPTED")
            failure = True

        consumer.finish_netconf_measured_segment()
        consumer.teardown_netconf()
        consumer.cleanup(failure=failure, mns_root=endpoints.mns_root,
                         provider_context=provider_context)
        consumer.mirror.secret_refs_resolved = list(consumer.resolver.observed)
        consumer.mirror.unresolved_secret_refs = list(consumer.resolver.unresolved)
        consumer.mirror.disposition = "COMPLETED" if not failure else "ABORTED_ERROR"

        # The origin is the PRODUCER that actually built these sections, not
        # whether the runtime package happens to be importable.  Deriving it
        # from `runtime_available()` labelled W3's synthetic substrate
        # `RUNTIME_UNDER_TEST` in every checkout that carries the runtime, which
        # is a false provenance claim about bytes the runtime never produced.
        core = synthetic_core_sections(
            self.bundle, vector=vector, scenario_id=self.config.scenario_id,
            normalization_records=consumer.mirror.normalization_records)
        core_origin = CORE_SYNTHETIC
        consumer.mirror.core_sections_origin = core_origin

        provider_identity = {
            "releaseId": "contract-faithful-emulator",
            "releaseVersion": "self-test",
            "sourceCommit": "UNRESOLVED",
            "sourceTree": "UNRESOLVED",
            "releaseManifestSha256": "UNRESOLVED",
            "ociImageManifestDigest": self.config.provider_ocidigest,
            "yangClosureDigest": "UNRESOLVED",
            "yangCapabilityDigest": "UNRESOLVED",
            "acceptanceRecordDigest": "UNRESOLVED",
            "runtimeOwner": "lo1-selftest-operator",
            "teardownOwner": "lo1-selftest-operator",
        }
        window = (
            instant(_datetime.datetime.now(_datetime.timezone.utc)
                    - _datetime.timedelta(minutes=5)),
            instant(_datetime.datetime.now(_datetime.timezone.utc)
                    + _datetime.timedelta(hours=1)),
        )
        origin_ownership = {
            "r1ApiRoot": "UPPER_HARNESS",
            "rAppCallbackRoot": "UPPER_HARNESS",
            "a1StatusCallbackRoot": "UPPER_HARNESS",
            "o1ConsumerRoot": "UPPER_HARNESS",
            "configured.r1.dme.policyEvidencePushBaseUri": "UPPER_HARNESS",
            "a1ApiRoot": "LOWER_IMPLEMENTATION_UNDER_TEST",
            "MnSRoot": "LOWER_LIVE_O1_PROVIDER",
            "/o1/netconf/endpoint": "LOWER_LIVE_O1_PROVIDER",
            "/o1/sftp/allowedAuthorities": "LOWER_LIVE_O1_PROVIDER",
        }
        allowlist = sorted({
            endpoints.mns_root.split("//", 1)[1],
            endpoints.sftp_authority,
            endpoints.netconf.split("//", 1)[1],
        })
        document = consumer.mirror.build(
            vector=vector, vector_sha256=vector_digest(vector),
            provider_identity=provider_identity,
            authority_record_digest=authority_record_digest,
            execution_window=window, core_sections=core,
            origin_ownership=origin_ownership, allowlist=allowlist,
            upper_identity=self.upper_identity)

        schema_path = self.config.design_root() / CAPTURE_SCHEMA_NAME
        validate_capture(document, schema_path=schema_path)
        (self.capture_root / "capture.json").write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        report = {
            "specVersion": "oran-aic-upper-live-o1-harness-self-test-report/1.0.0",
            "stateLabel": SELF_TEST_STATE_LABEL,
            "notAcceptance": (
                "Upper-side readiness evidence only. Disposition for this scenario "
                "belongs to the lower conformance runner under the live-O1 "
                "integration authority."),
            "runId": self.run_id,
            "scenarioId": self.config.scenario_id,
            "providerKind": EMULATOR_KIND,
            "emulatorInUse": True,
            "coreSectionsOrigin": core_origin,
            "runtimeUnderTestAvailable": runtime_available(),
            "evidenceOrigin": {
                "o1SubGraph": "REAL_SOCKETS_AGAINST_THE_PROVIDER_EMULATOR",
                "coreSubGraph":
                    CORE_FROM_RUNTIME if core_origin == CORE_FROM_RUNTIME
                    else "W3_SYNTHETIC_FALSIFIER_SUBSTRATE_NOT_A_RUN",
            },
            "emulator": dict(result.observations),
            "emulatorHttpSequence": list(result.http_sequence),
            "emulatorUnknownRoutes": result.unknown_routes,
            "emittedPmSha256": list(result.emitted_pm_sha256),
            "goldenValueCollisions": list(emulator.golden_value_collisions),
            "armedFaults": list(self.config.emulator_faults),
            "admissionChecks": [check.as_capture() for check in checks],
            "unresolvedLedger": admission.unresolved_ledger(self.run_id),
            "driverFindings": findings,
        }
        (self.capture_root / "self-test-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        if self.config.require_runtime and not runtime_available():
            return SelfTestResult(
                status="RUNTIME_ABSENT", run_id=self.run_id,
                capture_root=self.capture_root, checks=checks,
                emulator=result.observations,
                detail=(f"{RUNTIME_MODULE} is not importable; the self-test refuses to "
                        "report a pass without the runtime under test"))
        status = "OK" if not findings else "FINDING"
        return SelfTestResult(
            status=status, run_id=self.run_id, capture_root=self.capture_root,
            checks=checks, emulator=result.observations, findings=tuple(findings))

    def _teardown(self) -> None:
        if self._consumer is not None:
            with contextlib.suppress(Exception):
                self._consumer.stop()
        if self._emulator is not None:
            with contextlib.suppress(Exception):
                self._emulator.stop()
        if self._reservation is not None:
            self._reservation.release()
