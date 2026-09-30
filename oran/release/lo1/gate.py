"""The pre-start identity and authority gate (S3 seam, G-ID-01 … G-ID-10).

Every check runs to completion **before** the process opens any socket to any
external target, in the order ``identity-authority-gate.1.0.0.json`` declares.
One ``REFUSED`` or ``UNRESOLVED`` check aborts with exit 78 and zero external
calls.  There is no per-check override, no ``--force`` and no environment
variable that converts a refusal into a pass: an operator who needs a different
answer must change the deployment vector, the authority record or the Provider
release, all of which are digest-bound.

A refusal is *evidence*: :class:`GateRefused` carries a fully formed
:class:`GateResult`, so the caller can still write a capture whose
``run.disposition`` is ``ABORTED_GATE`` and whose ``authority.gateResult`` is
``REFUSED`` (G-GATE-2).  Checks that were never reached are recorded as
``UNRESOLVED`` rather than omitted, because an omitted check is indistinguishable
from a check that passed.

Documents this gate consumes
----------------------------

* ``RELEASE-MANIFEST.json`` -- the upper release identity and contract lock.
* the **Provider acceptance contract** (schema frozen in
  ``provider-acceptance-contract.1.0.0.schema.json``), which carries the Provider
  identity, the authority token and scope, the adapter-action assignment, the
  deterministic-stub routing and the execution window.
* the **execution authority record**, which carries the run envelope the
  Provider document cannot: the approved ``runId``, the deployment-vector digest
  the authority signed, the signer identity, the network allowlist and the two
  bilateral acceptance records.  Its member list is documented in
  :data:`AUTHORITY_RECORD_MEMBERS`; an unknown or missing member is a refusal
  rather than a default.
* the **deployment vector**, validated against the FROZEN
  ``deployment-test-vector.1.0.0.schema.json`` bytes with no shadow rewrite.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker

from oran.conformance.contracts import ContractBundle, ContractError

from .config import Lo1StartupConfig, PROFILE_SELF_TEST
from .o1_readiness import PROVIDER_INITIAL_STATE
from .plan import ASSIGNMENT_POINTER, CATALOG_POINTER, LIVE_O1_SCENARIO
from .preflight import (
    PreflightError, live_capture_lower_bound_ms, load_vector, pointer,
    validate_vector_against_frozen_schema,
)
from .tls import ANY_REFERENCE, SecretResolver, TlsConfigurationError
from .security import (
    SecurityAuthorityError, load_security_authority,
    security_network_authorities, security_secret_references,
)

GATE_SPEC_FILE_NAME = "identity-authority-gate.1.0.0.json"
PROVIDER_CONTRACT_SCHEMA_FILE_NAME = "provider-acceptance-contract.1.0.0.schema.json"
EXIT_CONFIG = 78

RELEASE_ID = "upper-live-o1-harness"
RELEASE_VERSION = "1.0.3"

SELF_TEST_LABEL = "UPPER_LIVE_O1_HARNESS_SELF_TEST"
PROVIDER_KIND_LIVE = "LIVE_O1_PROVIDER_UNDER_SEPARATE_AUTHORITY"
PROVIDER_KIND_EMULATOR = "CONTRACT_FAITHFUL_EMULATOR"
COUNTERPART_LIVE = "LIVE_O1_AUTHORITY_APPROVED_HARNESS"
COUNTERPART_SELF_TEST = "UPPER_ARTIFACT_SELF_TEST_HARNESS"

CHECK_IDS = tuple("G-ID-%02d" % index for index in range(1, 11))

UNRESOLVED = "UNRESOLVED"

#: The delivery archive digest is the one release digest that cannot be a
#: packaged member: writing it into a file inside the archive changes the bytes
#: being digested, so the fixed point never converges.  The manifest therefore
#: names *where* the value is published and the gate reads it from that record,
#: which is supplied out of band and lives outside the packaged member set.
ARCHIVE_DIGEST_PUBLICATION_MEMBER = "upperDeliveryArchiveSha256Publication"
#: A literal value under this name can only be stale or invented, for the same
#: reason.  Its presence is a refusal, not a convenience.
ARCHIVE_DIGEST_LITERAL_MEMBER = "upperDeliveryArchiveSha256"

#: The six permissions the scope must force false (G-ID-09), each paired with the
#: prohibited target kind it answers in the frozen assignment.
SCOPE_PERMISSIONS = {
    "liveE2Control": "LIVE_E2_CONTROL",
    "liveRanWrite": "LIVE_RAN_WRITE",
    "otaTransmission": "OTA_TRANSMISSION",
    "usrpRadio": "USRP_RADIO",
    "upperProductionDeployment": "UPPER_PRODUCTION_DEPLOYMENT",
    "peerInternalSourceImport": "PEER_INTERNAL_SOURCE_IMPORT",
}

ADAPTER_ACTIONS = ("LOAD_O1_PROFILE", "INSTALL_O1_SUBSCRIPTION", "SET_PERF_METRIC_JOB")

#: A10: the notification boundary header the receiver binds a notification to
#: an active subscription with.  The listener deliberately does not mint it --
#: a receiver that invents its own correlation is self-fulfilling -- so the
#: Provider's sender owes it, and G-ID-03 refuses a Provider that does not
#: declare it.  Restated here from ``o1_consumer`` because the gate must not
#: import the O1 boundary it runs before.
SUBSCRIPTION_ID_HEADER = "x-lo1-internal-subscription-id"
SUBSCRIPTION_ID_VALUE_SOURCE = "SUBSCRIPTION_ID_RETURNED_AT_SUBSCRIPTION_CREATE"

STUB_ROUTINGS = ("UPPER_DETERMINISTIC_STUB", "LOWER_IMPLEMENTATION_UNDER_TEST")

#: Members the execution authority record must carry.  Documented rather than
#: inferred, so a missing one is a refusal instead of a silent default.
AUTHORITY_RECORD_MEMBERS = (
    "specVersion", "runId", "signerIdentity", "deploymentVectorSha256",
    "approvedNotBefore", "approvedNotAfter", "networkAllowlist",
    "releaseAuthorities", "acceptance",
    # SC-084's three mandatory O1 readiness adapter actions are assigned by the
    # execution authority itself.  A record that does not make the assignment
    # cannot be the record a Provider acceptance contract binds itself to.
    "adapterActionAssignment",
)
AUTHORITY_RECORD_SPEC_VERSION = (
    "oran-aic-upper-live-o1-harness-execution-authority/1.0.0")

#: DESIGN.md 5.1: the upper binds FOUR listeners from five upper-owned origins,
#: so exactly these three origins are co-hosted.  Any other authority collision
#: is an endpoint ambiguity (G-ID-06).
CO_HOSTED_UPPER_ORIGINS = frozenset({
    "rAppCallbackRoot",
    "configured.r1.dme.policyEvidencePushBaseUri",
    "a1StatusCallbackRoot",
})

UPPER_ORIGINS = {
    "r1ApiRoot": ("r1", "apiRoot"),
    "rAppCallbackRoot": ("r1", "callbackApi", "rootUri"),
    "configured.r1.dme.policyEvidencePushBaseUri":
        ("r1", "dme", "policyEvidencePushBaseUri"),
    "a1StatusCallbackRoot": ("a1", "statusCallbackRoot"),
    "o1ConsumerRoot": ("o1", "fileDataReporting", "consumerReference"),
}
LOWER_PROVIDER_ORIGINS = {
    "MnSRoot": ("o1", "fileDataReporting", "mnsRoot"),
}
LOWER_UNDER_TEST_ORIGINS = {
    "a1ApiRoot": ("a1", "apiRoot"),
}

#: Path markers that would mean a Provider artefact was packaged (G-ID-03).
PROVIDER_ARTEFACT_SUFFIXES = (".yang", ".yin")
PROVIDER_ARTEFACT_DIRECTORIES = frozenset({
    "yang", "yang-modules", "yang_modules", "provider-source", "provider_src",
    "image-layers", "blobs",
})

_HEX64 = "0123456789abcdef"


class GateRefused(RuntimeError):
    """The gate refused to admit the run.  Exit 78, zero external calls."""

    exit_code: int = EXIT_CONFIG

    def __init__(self, message: str, *, result: "GateResult | None" = None) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class CheckResult:
    id: str
    outcome: str
    reason_code: str | None
    observed_digest: str | None

    def as_capture(self) -> dict[str, Any]:
        record: dict[str, Any] = {"id": self.id, "outcome": self.outcome}
        if self.reason_code:
            record["reasonCode"] = self.reason_code
        if self.observed_digest:
            record["observedDigest"] = self.observed_digest
        return record


@dataclass(frozen=True)
class UnresolvedEntry:
    field: str
    owed_by: str
    blocks_check: str
    meaning: str
    permitted_unresolved: bool

    def as_ledger_entry(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "owedBy": self.owed_by,
            "blocksCheck": self.blocks_check,
            "meaning": self.meaning,
            "permittedUnresolved": self.permitted_unresolved,
        }


@dataclass(frozen=True)
class GateResult:
    admitted: bool
    provider_kind: str
    counterpart_kind: str
    self_test_label: str | None
    checks: tuple[CheckResult, ...]
    unresolved: tuple[UnresolvedEntry, ...]
    scope: Mapping[str, Any]
    allowlist: tuple[str, ...]
    netconf_adapter_actions: frozenset[str]
    deterministic_stub_routing: str
    run_id: str
    capture_root: Path
    execution_window: tuple[str, str]
    #: Non-frozen extras the composition root needs; they are observations of
    #: what the gate already admitted, never a second decision.
    observations: Mapping[str, Any] = None  # type: ignore[assignment]

    def require_admitted(self) -> None:
        if not self.admitted:
            raise GateRefused(
                "the identity/authority gate refused this run", result=self)

    def as_capture_authority(self) -> dict[str, Any]:
        extras = dict(self.observations or {})
        return {
            "gateSpecSha256": str(extras.get("gateSpecSha256", "0" * 64)),
            "gateResult": "ADMITTED" if self.admitted else "REFUSED",
            "checks": [check.as_capture() for check in self.checks],
            "scope": dict(self.scope),
            "deploymentVectorDigest": str(extras.get("deploymentVectorDigest", "0" * 64)),
            "authorityRecordDigest": str(extras.get("authorityRecordDigest", "0" * 64)),
            # The harness holds no signing key and has no code path that can
            # produce an authority record, so the alternative is unrepresentable.
            "selfIssued": False,
        }

    def as_capture_profile(self) -> dict[str, Any]:
        return {
            "executionProfile": "live-O1",
            "counterpartKind": self.counterpart_kind,
            "providerKind": self.provider_kind,
            "emulatorInUse": self.provider_kind == PROVIDER_KIND_EMULATOR,
            "selfTestLabel": self.self_test_label,
        }

    def unresolved_ledger(self) -> dict[str, Any]:
        return {
            "specVersion": "oran-aic-upper-live-o1-harness-unresolved-ledger/1.0.0",
            "generatedForRunId": self.run_id,
            "entries": [entry.as_ledger_entry() for entry in self.unresolved],
        }


class _Refusal(Exception):
    """Internal: one check refused, with the reason code it recorded."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


def run_identity_authority_gate(*, startup: Lo1StartupConfig,
                                spec_path: Path) -> GateResult:
    """Run G-ID-01 … G-ID-10 in order and decide admission exactly once."""
    return _GateRun(startup=startup, spec_path=Path(spec_path)).run()


class _GateRun:
    def __init__(self, *, startup: Lo1StartupConfig, spec_path: Path) -> None:
        self.startup = startup
        self.spec_path = Path(spec_path)
        self.checks: list[CheckResult] = []
        self.unresolved: list[UnresolvedEntry] = []
        self.observations: dict[str, Any] = {}
        self.scope: dict[str, Any] = _default_scope()
        self.allowlist: tuple[str, ...] = ()
        self.adapter_actions: frozenset[str] = frozenset()
        self.stub_routing = UNRESOLVED
        self.execution_window: tuple[str, str] = ("", "")
        # Documents, loaded lazily by the first check that needs them.
        self.manifest: dict[str, Any] = {}
        self.provider: dict[str, Any] = {}
        self.authority: dict[str, Any] = {}
        self.vector: dict[str, Any] = {}
        self.vector_sha256 = ""
        self.bundle: ContractBundle | None = None
        self.catalog: Mapping[str, Any] = {}
        self.assignment: Mapping[str, Any] = {}
        self.upper_listeners: tuple[str, ...] = ()
        self.security_authority: dict[str, Any] = {}

    # -- driver -----------------------------------------------------------
    def run(self) -> GateResult:
        steps = (
            self._check_01, self._check_02, self._check_03, self._check_04,
            self._check_05, self._check_06, self._check_07, self._check_08,
            self._check_09, self._check_10,
        )
        for index, step in enumerate(steps):
            check_id = CHECK_IDS[index]
            try:
                digest = step()
            except _Refusal as refusal:
                self.checks.append(CheckResult(
                    check_id, "REFUSED", refusal.reason_code, None))
                self._fill_unreached(index + 1)
                result = self._result(admitted=False)
                raise GateRefused(
                    "%s refused: %s" % (check_id, refusal.detail),
                    result=result) from None
            except (ContractError, PreflightError, TlsConfigurationError,
                    SecurityAuthorityError,
                    OSError, ValueError, KeyError, TypeError) as exc:
                self.checks.append(CheckResult(
                    check_id, "REFUSED", "CHECK_INPUT_UNREADABLE", None))
                self._fill_unreached(index + 1)
                result = self._result(admitted=False)
                raise GateRefused(
                    "%s refused: %s" % (check_id, exc), result=result) from None
            self.checks.append(CheckResult(check_id, "PASS", None, digest))
        return self._result(admitted=True)

    def _fill_unreached(self, start: int) -> None:
        for check_id in CHECK_IDS[start:]:
            self.checks.append(CheckResult(
                check_id, "UNRESOLVED", "NOT_REACHED_AFTER_REFUSAL", None))

    def _result(self, *, admitted: bool) -> GateResult:
        self_test = self.startup.profile == PROFILE_SELF_TEST
        provider_kind = (PROVIDER_KIND_EMULATOR if self_test else PROVIDER_KIND_LIVE)
        return GateResult(
            admitted=admitted,
            provider_kind=provider_kind,
            counterpart_kind=(COUNTERPART_SELF_TEST if self_test
                              else COUNTERPART_LIVE),
            self_test_label=SELF_TEST_LABEL if self_test else None,
            checks=tuple(self.checks),
            unresolved=tuple(self.unresolved),
            scope=dict(self.scope),
            allowlist=self.allowlist,
            netconf_adapter_actions=self.adapter_actions,
            deterministic_stub_routing=self.stub_routing,
            run_id=self.startup.run_id,
            capture_root=Path(self.startup.capture_root),
            execution_window=self.execution_window,
            observations=dict(self.observations),
        )

    # -- G-ID-01 ----------------------------------------------------------
    def _check_01(self) -> str:
        """CONTRACT_LOCK_AND_ASSIGNMENT -- packaged bytes only."""
        self.observations["gateSpecSha256"] = _digest_file(self.spec_path)
        self.manifest = _load_json(self.startup.release_manifest_path,
                                   "RELEASE-MANIFEST.json")
        lock = self.manifest.get("contractLock")
        if not isinstance(lock, Mapping) or not isinstance(
                lock.get("files"), Mapping):
            raise _Refusal("CONTRACT_LOCK_ABSENT",
                           "the release manifest carries no contract lock")
        authority_root = Path(self.startup.contract_authority)
        differing = 0
        for relative, declared in sorted(lock["files"].items()):
            target = authority_root / str(relative)
            if not target.is_file():
                raise _Refusal("CONTRACT_FILE_MISSING",
                               "locked contract file %s is absent" % relative)
            if _digest_file(target) != str(declared):
                differing += 1
        if differing:
            raise _Refusal(
                "CONTRACT_BYTE_DIFF",
                "%d packaged contract files differ from the manifest digest"
                % differing)
        self.observations["frozenBundleDiffFileCount"] = differing
        self.bundle = ContractBundle(authority_root)
        self.catalog = self.bundle.catalog
        index = int(str(lock.get("catalogPointer", CATALOG_POINTER)).rsplit("/", 1)[-1])
        scenario = self.catalog["scenarios"][index]
        if str(scenario.get("id")) != LIVE_O1_SCENARIO:
            raise _Refusal("SCENARIO_POINTER_MISMATCH",
                           "the recorded catalog pointer does not address %s"
                           % LIVE_O1_SCENARIO)
        assignment_document = self.bundle.execution_profile_assignment or {}
        assignments = list(assignment_document.get("assignments", []))
        selected = [item for item in assignments
                    if str(item.get("scenarioId")) == LIVE_O1_SCENARIO]
        if len(selected) != 1:
            raise _Refusal("ASSIGNMENT_NOT_UNIQUE",
                           "the frozen assignment does not select exactly one "
                           "%s row" % LIVE_O1_SCENARIO)
        self.assignment = selected[0]
        self.observations["assignmentIndex"] = assignments.index(selected[0])
        if str(self.assignment.get("catalogPointer")) != CATALOG_POINTER:
            raise _Refusal("ASSIGNMENT_POINTER_MISMATCH",
                           "the assignment does not point at %s" % CATALOG_POINTER)
        if str(self.assignment.get("executionProfile")) != "live-O1":
            raise _Refusal("EXECUTION_PROFILE_MISMATCH",
                           "the assignment does not select the live-O1 profile")
        if str(self.assignment.get("fixtureMode")) != str(scenario.get("fixtureMode")):
            raise _Refusal("FIXTURE_MODE_MISMATCH",
                           "assignment and catalog disagree about fixtureMode")
        self.observations["assignmentPointer"] = ASSIGNMENT_POINTER
        self.observations["contractDigests"] = _contract_digests(self.bundle)
        return _digest_file(
            Path(self.bundle.path) / ("scenario-catalog.%s.json" % self.bundle.version))

    # -- G-ID-02 ----------------------------------------------------------
    def _check_02(self) -> str:
        """UPPER_RELEASE_IDENTITY -- four digests, four distinct meanings."""
        revisions = self.manifest.get("revisions")
        if not isinstance(revisions, Mapping):
            raise _Refusal("RELEASE_REVISIONS_ABSENT",
                           "the release manifest declares no revisions")
        for name in ("upperSourceCommit", "upperSourceTree", "testedCodeCommit"):
            if not _is_hex(str(revisions.get(name, "")), 40):
                raise _Refusal("REVISION_MALFORMED",
                               "%s is not a 40-hex git object id" % name)
        members = self.manifest.get("files")
        if not isinstance(members, Mapping) or not members:
            raise _Refusal("MEMBER_DIGESTS_ABSENT",
                           "the release manifest lists no packaged members")
        recomputed = content_set_digest(members)
        declared_content = str(revisions.get("upperReleaseContentSha256", ""))
        if not _is_hex(declared_content, 64):
            raise _Refusal("CONTENT_DIGEST_MALFORMED",
                           "upperReleaseContentSha256 is not a SHA-256 digest")
        if recomputed != declared_content:
            raise _Refusal("CONTENT_DIGEST_MISMATCH",
                           "the recomputed content-set digest differs from the "
                           "declared value")
        # The content-set digest is only self-consistent: it proves the manifest
        # agrees with itself, not that the extracted tree is the tree the
        # manifest describes.  Bind the two, or a member could be swapped after
        # the manifest was written and every digest above would still agree.
        release_root = Path(self.startup.release_root)
        for relative, declared in sorted(members.items()):
            target = release_root / str(relative)
            if not target.is_file():
                raise _Refusal(
                    "MEMBER_FILE_MISSING",
                    "the manifest lists %s, which is not in the extracted "
                    "release tree" % relative)
            if _digest_file(target) != str(declared):
                raise _Refusal(
                    "MEMBER_BYTE_DIFF",
                    "packaged member %s differs from its manifest digest"
                    % relative)
        self.observations["releaseMemberCount"] = len(members)
        archive = self._archive_digest(revisions, members)
        manifest_raw = _digest_file(self.startup.release_manifest_path)
        image = str(revisions.get("upperOciImageManifestDigest", ""))
        if not _is_oci_digest(image):
            raise _Refusal(
                "UPPER_IMAGE_DIGEST_MALFORMED",
                "upperOciImageManifestDigest must be an OCI image manifest "
                "digest computed from the packaged image layout")
        # A6: the field the capture reports is the packaged layout's own
        # manifest digest, so it is read from provenance rather than declared
        # twice.  Two declarations that can disagree are one declaration too
        # many, and the legacy unresolved-registry token is refused outright.
        provenance = self.manifest.get("provenance")
        oci = provenance.get("oci") if isinstance(provenance, Mapping) else None
        if not isinstance(oci, Mapping) or not _is_oci_digest(
                str(oci.get("imageManifestDigest", ""))):
            raise _Refusal(
                "UPPER_IMAGE_PROVENANCE_ABSENT",
                "the manifest declares no packaged OCI image layout to read "
                "the upper image manifest digest from")
        if str(oci["imageManifestDigest"]) != image:
            raise _Refusal(
                "UPPER_IMAGE_DIGEST_MISMATCH",
                "the declared upper image digest is not the manifest digest of "
                "the OCI layout this release packages")
        distinct = {declared_content, archive, manifest_raw,
                    str(revisions["upperSourceTree"])}
        if len(distinct) != 4:
            raise _Refusal(
                "DIGEST_MEANING_CONFLATION",
                "two of the four upper digests are equal; their meanings are "
                "not interchangeable")
        self.observations["revisions"] = {
            "upperReleaseId": str(self.manifest.get("releaseId", RELEASE_ID)),
            "upperReleaseVersion": str(
                self.manifest.get("releaseVersion", RELEASE_VERSION)),
            "upperReleaseContentSha256": declared_content,
            "upperReleaseManifestSha256": manifest_raw,
            "upperDeliveryArchiveSha256": archive,
            "upperSourceCommit": str(revisions["upperSourceCommit"]),
            "upperSourceTree": str(revisions["upperSourceTree"]),
            "testedCodeCommit": str(revisions["testedCodeCommit"]),
            "upperOciImageManifestDigest": image,
        }
        return manifest_raw

    def _archive_digest(self, revisions: Mapping[str, Any],
                        members: Mapping[str, Any]) -> str:
        """A9: bind the delivery archive digest to a record from outside it.

        A container cannot carry the digest of itself, so the packaged manifest
        names the publication location instead of a value.  The gate resolves
        that name against the release root, reads the record supplied out of
        band, and refuses a missing, malformed, self-referential or untruthful
        one.  Nothing here invents a digest: with no record, there is no
        admission.
        """
        if ARCHIVE_DIGEST_LITERAL_MEMBER in revisions:
            raise _Refusal(
                "ARCHIVE_DIGEST_FABRICATED",
                "the manifest carries a literal %s; a container cannot contain "
                "the digest of itself, so that value is stale or invented"
                % ARCHIVE_DIGEST_LITERAL_MEMBER)
        publication = str(revisions.get(ARCHIVE_DIGEST_PUBLICATION_MEMBER, ""))
        name, separator, record = publication.partition("#")
        if not separator or not name or not record:
            raise _Refusal(
                "ARCHIVE_DIGEST_PUBLICATION_MALFORMED",
                "%s must name <file>#<record>" % ARCHIVE_DIGEST_PUBLICATION_MEMBER)
        parts = Path(name).parts
        if Path(name).is_absolute() or ".." in parts:
            raise _Refusal(
                "ARCHIVE_DIGEST_PUBLICATION_MALFORMED",
                "the publication location must be a relative name under the "
                "release root")
        if name in members:
            raise _Refusal(
                "ARCHIVE_DIGEST_PUBLICATION_SELF_REFERENTIAL",
                "the publication location %s is a packaged member, so its "
                "bytes are part of the archive it claims to digest" % name)
        record_path = Path(self.startup.release_root) / name
        if not record_path.is_file():
            raise _Refusal(
                "ARCHIVE_DIGEST_RECORD_ABSENT",
                "the out-of-band digest record %s was not supplied" % name)
        digest, target = _digest_record(record_path, record)
        if not _is_hex(digest, 64):
            raise _Refusal(
                "ARCHIVE_DIGEST_MALFORMED",
                "%s carries no %s record that is a SHA-256 digest"
                % (name, record))
        measured = False
        if target:
            archive_path = record_path.parent / target
            if archive_path.is_file():
                if _digest_file(archive_path) != digest:
                    raise _Refusal(
                        "ARCHIVE_DIGEST_RECORD_UNTRUTHFUL",
                        "the published %s does not bind the bytes of %s it "
                        "names" % (record, target))
                measured = True
        self.observations["archiveDigestPublication"] = {
            "location": publication,
            "recordIsPackagedMember": False,
            "measuredAgainstNamedArchive": measured,
        }
        return digest

    # -- G-ID-03 ----------------------------------------------------------
    def _check_03(self) -> str:
        """PROVIDER_RELEASE_IDENTITY -- identity only, immutably pinned."""
        self.provider = _load_json(self.startup.provider_acceptance_path,
                                   "provider acceptance contract")
        schema_path = _spec_file(self.startup, PROVIDER_CONTRACT_SCHEMA_FILE_NAME)
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        errors = sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
                self.provider),
            key=lambda error: list(error.absolute_path))
        if errors:
            raise _Refusal(
                "PROVIDER_CONTRACT_INVALID",
                "the Provider acceptance contract fails its frozen schema at /%s"
                % "/".join(str(part) for part in errors[0].absolute_path))
        image = self.provider["image"]
        digest = str(image.get("ociImageManifestDigest", ""))
        if not _is_oci_digest(digest):
            raise _Refusal(
                "PROVIDER_IMAGE_NOT_IMMUTABLE",
                "the Provider image reference is a mutable tag, a local image "
                "ID or the unresolved token; only a manifest digest is accepted")
        if image.get("localImageIdIsNotAcceptable") is not True:
            raise _Refusal("PROVIDER_LOCAL_IMAGE_ID_ADMITTED",
                           "the Provider contract does not refuse a local image ID")
        # Only the Provider IDENTITY sections are G-ID-03's business.  The
        # authority, window and acceptance sections are owed by the integration
        # authority and are answered by G-ID-05, G-ID-08 and G-ID-10, each of
        # which names the value it is still waiting for.
        identity = {name: self.provider.get(name) for name in (
            "providerRelease", "sourceRevision", "image", "netconfYang",
            "endpoints", "notificationBoundary", "trust", "ownership")}
        remaining = sorted(_unresolved_pointers(identity))
        if remaining:
            raise _Refusal(
                "PROVIDER_MEMBER_UNRESOLVED",
                "the Provider acceptance contract still carries %d unresolved "
                "identity members, beginning at %s"
                % (len(remaining), remaining[0]))
        yang = self.provider["netconfYang"]
        closure = str(yang.get("closureAggregateSha256", ""))
        capability = str(yang.get("capabilityDigest", ""))
        if not (_is_hex(closure, 64) and _is_hex(capability, 64)):
            raise _Refusal("YANG_DIGEST_MALFORMED",
                           "the YANG closure and capability digests must both "
                           "be SHA-256 digests")
        if closure == capability:
            raise _Refusal(
                "YANG_DIGEST_CONFLATION",
                "the vendored-closure digest and the advertised-capability "
                "digest have distinct meanings and may not be equal")
        # A10: the notification boundary header is the one wire value the
        # Provider owes that is not a deployment-vector leaf.  The upper never
        # mints it, so a Provider that does not declare it will have every
        # notification refused; that is settled here rather than at the first
        # notification.
        boundary = self.provider["notificationBoundary"]
        if str(boundary.get("subscriptionIdHeader")) != SUBSCRIPTION_ID_HEADER:
            raise _Refusal(
                "NOTIFICATION_BOUNDARY_HEADER_UNKNOWN",
                "the Provider contract names a different notification boundary "
                "header than the receiver binds")
        if boundary.get(
                "senderCarriesHeaderOnEveryFileReadyNotification") is not True:
            raise _Refusal(
                "NOTIFICATION_BOUNDARY_HEADER_UNDECLARED",
                "the Provider does not declare that its notification sender "
                "carries %s, so no notification it posts could be bound to a "
                "subscription" % SUBSCRIPTION_ID_HEADER)
        if str(boundary.get("headerValueSource")) != SUBSCRIPTION_ID_VALUE_SOURCE:
            raise _Refusal(
                "NOTIFICATION_BOUNDARY_VALUE_SOURCE",
                "the boundary header's value must be the subscription "
                "identifier returned at subscription create; any other source "
                "lets the sender choose its own correlation")
        offender = _packaged_provider_artefact(self.startup.release_root)
        if offender is not None:
            raise _Refusal("PROVIDER_ARTEFACT_PACKAGED",
                           "a Provider artefact was found inside the packaged "
                           "release at %s" % offender)
        self.observations["provider"] = {
            "releaseId": str(self.provider["providerRelease"]["releaseId"]),
            "releaseVersion": str(self.provider["providerRelease"]["releaseVersion"]),
            "sourceCommit": str(self.provider["sourceRevision"]["commit"]),
            "sourceTree": str(self.provider["sourceRevision"]["tree"]),
            "releaseManifestSha256": str(
                self.provider["providerRelease"]["releaseManifestSha256"]),
            "ociImageManifestDigest": digest,
            "yangClosureDigest": closure,
            "yangCapabilityDigest": capability,
            "acceptanceRecordDigest": _digest_file(
                self.startup.provider_acceptance_path),
            "runtimeOwner": str(self.provider["ownership"]["runtimeOwner"]),
            "teardownOwner": str(self.provider["ownership"]["teardownOwner"]),
        }
        return digest.split(":", 1)[1]

    # -- G-ID-04 ----------------------------------------------------------
    def _check_04(self) -> str:
        """BILATERAL_ACCEPTANCE_RECORDS -- each side names the other's digests."""
        self.authority = _load_json(self.startup.authority_record_path,
                                    "execution authority record")
        missing = [name for name in AUTHORITY_RECORD_MEMBERS
                   if name not in self.authority]
        if missing:
            raise _Refusal("AUTHORITY_RECORD_INCOMPLETE",
                           "the authority record is missing %s"
                           % ", ".join(sorted(missing)))
        if str(self.authority.get("specVersion")) != AUTHORITY_RECORD_SPEC_VERSION:
            raise _Refusal("AUTHORITY_RECORD_SPEC_VERSION",
                           "the authority record declares an unknown specVersion")
        acceptance = self.authority["acceptance"]
        upper_record = acceptance.get("upperRecord") if isinstance(
            acceptance, Mapping) else None
        lower_record = acceptance.get("lowerRecord") if isinstance(
            acceptance, Mapping) else None
        if not isinstance(upper_record, Mapping) or not isinstance(
                lower_record, Mapping):
            raise _Refusal("ACCEPTANCE_RECORD_ABSENT",
                           "both acceptance records must be present")
        provider_digest = self.observations["provider"]["ociImageManifestDigest"]
        if str(upper_record.get("providerReleaseDigest")) != provider_digest:
            raise _Refusal(
                "ACCEPTANCE_PROVIDER_DIGEST_MISMATCH",
                "the upper acceptance record names a different Provider image "
                "digest than the one G-ID-03 admitted")
        content = self.observations["revisions"]["upperReleaseContentSha256"]
        if str(lower_record.get("upperReleaseContentSha256")) != content:
            raise _Refusal(
                "ACCEPTANCE_UPPER_DIGEST_MISMATCH",
                "the lower acceptance record names a different upper content "
                "digest than the one G-ID-02 admitted")
        upper_signer = str(upper_record.get("signerIdentity", ""))
        lower_signer = str(lower_record.get("signerIdentity", ""))
        if not upper_signer or not lower_signer:
            raise _Refusal("ACCEPTANCE_UNSIGNED",
                           "an acceptance record carries no signer identity")
        if upper_signer == lower_signer:
            raise _Refusal("ACCEPTANCE_SINGLE_AUTHORITY",
                           "one authority signed both acceptance records")
        if RELEASE_ID in {upper_signer, lower_signer}:
            raise _Refusal("ACCEPTANCE_SELF_SIGNED",
                           "an acceptance record is signed by the harness itself")
        self.observations["acceptanceSigners"] = sorted({upper_signer, lower_signer})
        return _digest_file(self.startup.authority_record_path)

    # -- G-ID-05 ----------------------------------------------------------
    def _check_05(self) -> str:
        """EXECUTION_AUTHORITY_AND_DEPLOYMENT_VECTOR -- separate and digest-bound."""
        assert self.bundle is not None
        self.vector, self.vector_sha256, _raw = load_vector(self.startup.vector_path)
        try:
            schema_digest = validate_vector_against_frozen_schema(
                self.vector, self.bundle)
        except PreflightError as exc:
            raise _Refusal("VECTOR_SCHEMA_INVALID", str(exc)) from None
        self.observations["deploymentVectorSchemaSha256"] = schema_digest
        self.observations["deploymentVectorDigest"] = self.vector_sha256
        self.observations["authorityRecordDigest"] = _digest_file(
            self.startup.authority_record_path)
        profiles = (self.bundle.execution_profile_assignment or {}).get("profiles", {})
        expected_token = str(profiles.get("live-O1", {}).get("authorityToken", ""))
        declared_token = str(self.provider["authority"].get("authorityToken", ""))
        if not expected_token or declared_token != expected_token:
            raise _Refusal(
                "AUTHORITY_TOKEN_MISMATCH",
                "the authority token does not equal the one the frozen "
                "assignment declares for the live-O1 profile")
        if str(self.authority.get("deploymentVectorSha256")) != self.vector_sha256:
            raise _Refusal(
                "AUTHORITY_VECTOR_DIGEST_MISMATCH",
                "the execution authority names a different deployment vector "
                "digest than the bytes this run holds")
        signer = str(self.authority.get("signerIdentity", ""))
        if not signer:
            raise _Refusal("AUTHORITY_UNSIGNED",
                           "the execution authority record is unsigned")
        authorities = self.authority.get("releaseAuthorities")
        if not isinstance(authorities, Mapping) or not authorities.get("upper") \
                or not authorities.get("provider"):
            raise _Refusal("RELEASE_AUTHORITIES_ABSENT",
                           "the authority record does not name both release "
                           "authorities")
        if signer in {str(authorities["upper"]), str(authorities["provider"]),
                      RELEASE_ID}:
            raise _Refusal(
                "SELF_ISSUED_AUTHORITY",
                "the execution authority signer must differ from both release "
                "authorities and from the harness identity")
        # The Provider acceptance contract's ``authority`` section describes the
        # SAME execution authority, so the two documents must NAME the same
        # signer; a disagreement means one of them was signed by someone else.
        if str(self.provider["authority"].get("signerIdentity")) != signer:
            raise _Refusal(
                "AUTHORITY_SIGNER_DISAGREEMENT",
                "the Provider acceptance contract and the execution authority "
                "record name different signers")
        return self.vector_sha256

    # -- G-ID-06 ----------------------------------------------------------
    def _check_06(self) -> str:
        """ENDPOINT_BINDING_EXACTNESS -- one authority per origin, no defaulting."""
        resolved: dict[str, str] = {}
        ownership: dict[str, str] = {}
        for name, path in UPPER_ORIGINS.items():
            resolved[name] = self._origin_authority(name, path)
            ownership[name] = "UPPER_HARNESS"
        for name, path in LOWER_PROVIDER_ORIGINS.items():
            resolved[name] = self._origin_authority(name, path)
            ownership[name] = "LOWER_LIVE_O1_PROVIDER"
        for name, path in LOWER_UNDER_TEST_ORIGINS.items():
            resolved[name] = self._origin_authority(name, path)
            ownership[name] = "LOWER_IMPLEMENTATION_UNDER_TEST"

        netconf = str(pointer(self.vector, ("o1", "netconf", "endpoint")))
        netconf_authority = self._ssh_authority(netconf, "/o1/netconf/endpoint")
        ownership["/o1/netconf/endpoint"] = "LOWER_LIVE_O1_PROVIDER"
        sftp_authorities = [
            self._exact_authority(str(item), "/o1/sftp/allowedAuthorities")
            for item in pointer(self.vector, ("o1", "sftp", "allowedAuthorities"))]
        if not sftp_authorities:
            raise _Refusal("SFTP_AUTHORITIES_EMPTY",
                           "o1.sftp.allowedAuthorities declares no authority")
        ownership["/o1/sftp/allowedAuthorities"] = "LOWER_LIVE_O1_PROVIDER"

        # Collisions: only the three co-hosted upper origins may share one
        # authority, because DESIGN.md 5.1 binds four listeners, not five.
        seen: dict[str, list[str]] = {}
        for name, authority in resolved.items():
            seen.setdefault(authority, []).append(name)
        for authority, names in sorted(seen.items()):
            if len(names) == 1:
                continue
            if not set(names) <= CO_HOSTED_UPPER_ORIGINS:
                raise _Refusal(
                    "ORIGIN_AUTHORITY_COLLISION",
                    "origins %s share the authority %s without a co-hosting "
                    "declaration" % (", ".join(sorted(names)), authority))
        lower_authorities = {resolved[name] for name in LOWER_PROVIDER_ORIGINS} \
            | {resolved[name] for name in LOWER_UNDER_TEST_ORIGINS} \
            | {netconf_authority} | set(sftp_authorities)
        upper_authorities = {resolved[name] for name in UPPER_ORIGINS}
        overlap = sorted(lower_authorities & upper_authorities)
        if overlap:
            raise _Refusal(
                "UPPER_LOWER_AUTHORITY_COLLISION",
                "authority %s is claimed by both an upper listener and a lower "
                "origin" % overlap[0])
        self.upper_listeners = tuple(sorted(upper_authorities))
        self.allowlist = tuple(sorted(upper_authorities | lower_authorities))
        self.observations["resolvedRoots"] = {
            "r1ApiRoot": str(pointer(self.vector, UPPER_ORIGINS["r1ApiRoot"])),
            "rAppCallbackRoot": str(pointer(
                self.vector, UPPER_ORIGINS["rAppCallbackRoot"])),
            "a1ApiRoot": str(pointer(self.vector, LOWER_UNDER_TEST_ORIGINS["a1ApiRoot"])),
            "a1StatusCallbackRoot": str(pointer(
                self.vector, UPPER_ORIGINS["a1StatusCallbackRoot"])),
            "policyEvidencePushBaseUri": str(pointer(
                self.vector,
                UPPER_ORIGINS["configured.r1.dme.policyEvidencePushBaseUri"])),
            "mnsRoot": str(pointer(self.vector, LOWER_PROVIDER_ORIGINS["MnSRoot"])),
            "o1ConsumerRoot": str(pointer(
                self.vector, UPPER_ORIGINS["o1ConsumerRoot"])),
            "netconfEndpoint": netconf,
            "sftpAuthorities": sorted(set(sftp_authorities)),
        }
        self.observations["originOwnership"] = ownership
        self.observations["upperListeners"] = list(self.upper_listeners)
        return hashlib.sha256(
            "\n".join(self.allowlist).encode("utf-8")).hexdigest()

    def _origin_authority(self, name: str, path: Sequence[str]) -> str:
        value = pointer(self.vector, tuple(path))
        text = str(value)
        parsed = urlsplit(text)
        if parsed.scheme != "https":
            raise _Refusal("ORIGIN_SCHEME",
                           "origin %s must be an https URI" % name)
        return self._exact_authority(
            "%s:%s" % (parsed.hostname, parsed.port) if parsed.port else str(
                parsed.hostname), "/" + "/".join(path))

    def _ssh_authority(self, uri: str, label: str) -> str:
        parsed = urlsplit(str(uri))
        if parsed.scheme != "ssh":
            raise _Refusal("ORIGIN_SCHEME", "%s must be an ssh:// endpoint" % label)
        return self._exact_authority(
            "%s:%s" % (parsed.hostname, parsed.port) if parsed.port else str(
                parsed.hostname), label)

    def _exact_authority(self, candidate: str, label: str) -> str:
        text = str(candidate).strip()
        if not text or "${" in text or "PLACEHOLDER" in text:
            raise _Refusal("ORIGIN_PLACEHOLDER",
                           "%s still carries a template placeholder" % label)
        if "/" in text:
            raise _Refusal("ORIGIN_IS_A_RANGE",
                           "%s is a CIDR or a path, not one authority" % label)
        if text.startswith("["):
            host, _, port = text.partition("]")
            host, port = host[1:], port.lstrip(":")
        else:
            host, _, port = text.rpartition(":")
            if not host:
                raise _Refusal("ORIGIN_WITHOUT_PORT",
                               "%s must declare an explicit port" % label)
        if not port or "-" in port or not port.isdigit():
            raise _Refusal("ORIGIN_PORT_RANGE",
                           "%s must declare exactly one numeric port" % label)
        if "*" in host or not host or _is_unspecified(host):
            raise _Refusal("ORIGIN_WILDCARD",
                           "%s resolves to a wildcard address" % label)
        addresses = _resolve_addresses(host)
        if len(addresses) > 1:
            raise _Refusal(
                "ORIGIN_AMBIGUOUS",
                "%s resolves to %d addresses; exactly one is required"
                % (label, len(addresses)))
        if not addresses:
            raise _Refusal("ORIGIN_UNRESOLVABLE",
                           "%s does not resolve to an address" % label)
        return "[%s]:%s" % (host, port) if ":" in host else "%s:%s" % (host, port)

    # -- G-ID-07 ----------------------------------------------------------
    def _check_07(self) -> str:
        """TRUST_AND_SECRET_RESOLUTION -- every reference resolves, or refuse."""
        self.security_authority = load_security_authority(
            self.startup.security_authority_path,
            expected_sha256=self.startup.security_authority_sha256,
            schema_path=self.startup.spec_dir / "o1-security-authority.1.0.0.schema.json")
        security = self.security_authority
        if str(security.get("runId")) != self.startup.run_id:
            raise _Refusal("SECURITY_AUTHORITY_RUN_MISMATCH",
                           "the O1 security authority names another run")
        if str(security.get("signerIdentity")) != str(
                self.authority.get("signerIdentity", "")):
            raise _Refusal("SECURITY_AUTHORITY_SIGNER_MISMATCH",
                           "the O1 security authority and execution authority differ")
        if str(security.get("deploymentVectorSha256")) != self.vector_sha256:
            raise _Refusal("SECURITY_AUTHORITY_VECTOR_MISMATCH",
                           "the O1 security authority names another deployment vector")
        if str(security.get("providerAcceptanceSha256")) != _digest_file(
                self.startup.provider_acceptance_path):
            raise _Refusal("SECURITY_AUTHORITY_PROVIDER_MISMATCH",
                           "the O1 security authority names another Provider acceptance")
        revisions = self.manifest.get("revisions", {})
        if str(security.get("upperReleaseContentSha256")) != str(
                revisions.get("upperReleaseContentSha256", "")):
            raise _Refusal("SECURITY_AUTHORITY_RELEASE_MISMATCH",
                           "the O1 security authority names another upper release")
        mns_host = urlsplit(str(pointer(
            self.vector, ("o1", "fileDataReporting", "mnsRoot")))).hostname
        if str(security["tls"]["mnsClient"]["serverName"]) != str(mns_host):
            raise _Refusal("MNS_TLS_SERVER_NAME_MISMATCH",
                           "the mTLS serverName does not equal the MnS URI hostname")
        provider_trust = self.provider.get("trust", {})
        mns_tls = security["tls"]["mnsClient"]
        if str(provider_trust.get("mnsTruststoreRef", "")) != str(
                mns_tls["truststoreRef"]):
            raise _Refusal("MNS_TRUST_AUTHORITY_DISAGREEMENT",
                           "Provider acceptance and O1 security authority name "
                           "different MnS truststores")
        declared_client_refs = {
            str(item) for item in provider_trust.get("clientCredentialRefs", [])}
        required_client_refs = {
            str(mns_tls["clientCertificateRef"]),
            str(mns_tls["clientPrivateKeyRef"]),
            str(security["oauth"]["mnsClient"]["credentialRef"]),
        }
        if not required_client_refs <= declared_client_refs:
            raise _Refusal("MNS_CLIENT_CREDENTIAL_AUTHORITY_DISAGREEMENT",
                           "Provider acceptance does not declare every mTLS/OAuth "
                           "credential reference used by the MnS client")
        security_authorities = security_network_authorities(security)
        self.allowlist = tuple(sorted(set(self.allowlist) | set(security_authorities)))
        self.observations["o1SecurityAuthorityDigest"] = \
            self.startup.security_authority_sha256
        self.observations["o1SecurityNetworkAuthorities"] = list(
            security_authorities)
        required = [
            ("/security/truststoreRef", ("security", "truststoreRef")),
            ("/security/r1ClientCredentialRef",
             ("security", "r1ClientCredentialRef")),
            ("/security/o1NotificationCredentialRef",
             ("security", "o1NotificationCredentialRef")),
            ("/o1/sftp/knownHostsRef", ("o1", "sftp", "knownHostsRef")),
            ("/o1/sftp/credentialRef", ("o1", "sftp", "credentialRef")),
            # Unconditional: the NETCONF readiness lifecycle is mandatory for
            # SC-084, so a run without resolvable NETCONF trust could never
            # reach JOB_ACTIVE and must not be admitted in the first place.
            ("/o1/netconf/knownHostsRef", ("o1", "netconf", "knownHostsRef")),
            ("/o1/netconf/credentialRef", ("o1", "netconf", "credentialRef")),
        ]
        required.extend(("/o1SecurityAuthority/secretRefs", (reference,))
                        for reference in security_secret_references(security))
        resolver = SecretResolver.from_file(Path(self.startup.secret_map_path))
        resolved: list[str] = []
        unresolved: list[str] = []
        for label, path in required:
            reference = (str(path[0]) if label == "/o1SecurityAuthority/secretRefs"
                         else str(pointer(self.vector, path)))
            if not ANY_REFERENCE.match(reference):
                raise _Refusal(
                    "LITERAL_CREDENTIAL",
                    "%s must be a secretRef URI, never a literal credential"
                    % label)
            if not resolver.has(reference):
                unresolved.append(reference)
                continue
            if resolver.resolves_inside(reference, self.startup.release_root):
                raise _Refusal(
                    "SECRET_INSIDE_RELEASE",
                    "%s resolves to material inside the release tree" % label)
            try:
                resolver.resolve_path(reference)
            except TlsConfigurationError:
                unresolved.append(reference)
                continue
            resolved.append(reference)
        if unresolved:
            self.observations["unresolvedSecretRefs"] = sorted(set(unresolved))
            for reference in sorted(set(unresolved)):
                self.unresolved.append(UnresolvedEntry(
                    field=reference, owed_by="OPERATOR", blocks_check="G-ID-07",
                    meaning="the secret map resolves no material for this "
                            "reference; an unresolvable reference is a refusal, "
                            "never a downgrade to an unauthenticated connection",
                    permitted_unresolved=False))
            raise _Refusal("SECRET_REF_UNRESOLVED",
                           "%d required secret references do not resolve"
                           % len(set(unresolved)))
        self.observations["secretRefsResolved"] = sorted(set(resolved))
        self.observations["unresolvedSecretRefs"] = []
        self.observations["resolvedSecuritySecretRefsDigest"] = hashlib.sha256(
            "\n".join(sorted(set(resolved))).encode("utf-8")).hexdigest()
        return self.startup.security_authority_sha256

    def _adapter_assignment(self) -> frozenset[str]:
        """The three O1 readiness adapter actions -- all of them, digest-bound.

        The withdrawn 1.0.1 admitted an absent, empty or partial assignment and
        called that "a complete and valid configuration".  It is not:
        ``scenario-catalog.1.0.1.json#/scenarios/83/materialization/initialState``
        makes ``INSTALL_O1_PROFILE``, ``INSTALL_ACTIVE_O1_SUBSCRIPTION`` and
        ``SET_PERF_METRIC_JOB_UNLOCKED`` mandatory for SC-084, so a run that
        cannot establish all three cannot run SC-084 at all.  Each way of
        failing to assign them gets its own reason code, and the assignment is
        admitted only when the Provider acceptance contract that carries it is
        bound by digest to the exact execution authority record this run holds
        and that record declares the same three actions.
        """
        assignment = self.provider["authority"].get("adapterActionAssignment")
        if not isinstance(assignment, Mapping):
            raise _Refusal(
                "ADAPTER_ASSIGNMENT_ABSENT",
                "the Provider acceptance contract declares no "
                "authority.adapterActionAssignment; SC-084's three mandatory O1 "
                "initial states have no owner")
        declared = assignment.get("assignedToUpper")
        if not isinstance(declared, Sequence) or isinstance(declared, (str, bytes)):
            raise _Refusal(
                "ADAPTER_ASSIGNMENT_MALFORMED",
                "authority.adapterActionAssignment.assignedToUpper is not a list")
        assigned = {str(item) for item in declared}
        unknown = sorted(assigned - set(ADAPTER_ACTIONS))
        if unknown:
            raise _Refusal("ADAPTER_ACTION_UNKNOWN",
                           "the authority assigned unknown adapter action %s"
                           % unknown[0])
        if not assigned:
            raise _Refusal(
                "ADAPTER_ASSIGNMENT_EMPTY",
                "the execution authority assigned none of the three mandatory "
                "O1 readiness adapter actions to the upper; SC-084 declares "
                "them as initial states, so an empty assignment cannot run it")
        missing = sorted(set(ADAPTER_ACTIONS) - assigned)
        if missing:
            raise _Refusal(
                "ADAPTER_ASSIGNMENT_PARTIAL",
                "the execution authority assigned %d of the 3 mandatory O1 "
                "readiness adapter actions; %s is unassigned and the readiness "
                "lifecycle SUBSCRIBED -> JOB_ACTIVE -> ASSURANCE_READY cannot "
                "be completed without it" % (len(assigned), missing[0]))
        measured = _digest_file(self.startup.authority_record_path)
        bound = str(self.provider["authority"].get("authorityRecordSha256", ""))
        if bound != measured:
            raise _Refusal(
                "ADAPTER_ASSIGNMENT_NOT_DIGEST_BOUND",
                "the Provider acceptance contract carrying the adapter "
                "assignment names authority record %s, but this run holds %s; "
                "an assignment that is not digest-bound to the record that "
                "approved it is not an assignment"
                % (bound[:16] or "(absent)", measured[:16]))
        record_assignment = self.authority.get("adapterActionAssignment")
        if not isinstance(record_assignment, Mapping):
            raise _Refusal(
                "ADAPTER_ASSIGNMENT_UNDECLARED_BY_AUTHORITY",
                "the execution authority record itself declares no "
                "adapterActionAssignment; the digest binding would point at a "
                "record that never made the assignment")
        record_declared = record_assignment.get("assignedToUpper")
        if not isinstance(record_declared, Sequence) or isinstance(
                record_declared, (str, bytes)) or \
                {str(item) for item in record_declared} != assigned:
            raise _Refusal(
                "ADAPTER_ASSIGNMENT_AUTHORITY_DISAGREEMENT",
                "the execution authority record and the Provider acceptance "
                "contract declare different adapter action assignments")
        self._require_provider_initial_state_acceptance()
        return frozenset(assigned)

    def _require_provider_initial_state_acceptance(self) -> None:
        """``CONFIGURE_LIVE_O1_OK_RECORD_FOR_EACH_POLICY_CELL`` stays Lower's.

        Frozen ownership is unchanged -- the upper consumes the live PM source
        and must never synthesise it -- but it is still one of SC-084's ten
        mandatory initial states, so readiness cannot complete until the
        Provider has accepted it in writing.  The evidence is consumed here, at
        admission, so a run that could never reach ASSURANCE_READY never starts.
        """
        acceptance = self.provider.get("acceptance")
        entries = acceptance.get("providerEstablishedInitialStates") \
            if isinstance(acceptance, Mapping) else None
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            raise _Refusal(
                "PROVIDER_INITIAL_STATE_EVIDENCE_ABSENT",
                "the Provider acceptance contract records no "
                "acceptance.providerEstablishedInitialStates, so the "
                "Provider-owned live PM initial state is unaccepted")
        found = None
        for entry in entries:
            if isinstance(entry, Mapping) and \
                    str(entry.get("initialState")) == PROVIDER_INITIAL_STATE:
                found = entry
                break
        if found is None:
            raise _Refusal(
                "PROVIDER_INITIAL_STATE_UNACCEPTED",
                "the Provider acceptance contract does not accept %s, which "
                "SC-084 declares and only the Provider can establish"
                % PROVIDER_INITIAL_STATE)
        if found.get("established") is not True:
            raise _Refusal(
                "PROVIDER_INITIAL_STATE_NOT_ESTABLISHED",
                "the Provider acceptance contract carries %s but does not "
                "declare it established" % PROVIDER_INITIAL_STATE)
        runner = dict(self.bundle.runner) if self.bundle is not None else {}
        expected = str((runner.get("initialStates") or {})
                       .get(PROVIDER_INITIAL_STATE, {})
                       .get("postcondition", ""))
        declared = str(found.get("postcondition", ""))
        if not expected or declared != expected:
            raise _Refusal(
                "PROVIDER_INITIAL_STATE_POSTCONDITION_MISMATCH",
                "the Provider's acceptance of %s does not quote the frozen "
                "runner-contract postcondition verbatim" % PROVIDER_INITIAL_STATE)
        self.observations["providerEstablishedInitialStates"] = [
            PROVIDER_INITIAL_STATE]

    # -- G-ID-08 ----------------------------------------------------------
    def _check_08(self) -> str:
        """RUN_ENVELOPE -- approved id, window, timeouts, capture root, allowlist."""
        approved_run_id = str(self.authority.get("runId", ""))
        if approved_run_id != self.startup.run_id:
            raise _Refusal(
                "RUN_ID_NOT_APPROVED",
                "the run id this process holds is not the one the authority "
                "approved")
        not_before = str(self.authority.get("approvedNotBefore", ""))
        not_after = str(self.authority.get("approvedNotAfter", ""))
        window = self.provider.get("executionWindow", {})
        if str(window.get("notBefore")) != not_before or \
                str(window.get("notAfter")) != not_after:
            raise _Refusal(
                "EXECUTION_WINDOW_DISAGREEMENT",
                "the Provider acceptance contract and the authority record "
                "declare different execution windows")
        now = datetime.now(timezone.utc)
        try:
            start = _instant(not_before)
            end = _instant(not_after)
        except ValueError as exc:
            raise _Refusal("EXECUTION_WINDOW_MALFORMED", str(exc)) from None
        if start >= end:
            raise _Refusal("EXECUTION_WINDOW_MALFORMED",
                           "approvedNotBefore is not before approvedNotAfter")
        if not (start <= now <= end):
            raise _Refusal("OUTSIDE_EXECUTION_WINDOW",
                           "the current instant is outside the approved window")
        maximum = window.get("maximumRunDurationMs")
        if (isinstance(maximum, bool) or not isinstance(maximum, int)
                or maximum <= 0):
            raise _Refusal(
                "MAXIMUM_RUN_DURATION_UNRESOLVED",
                "executionWindow.maximumRunDurationMs must be a positive "
                "integer supplied by the Provider release authority")
        self.execution_window = (not_before, not_after)
        self.observations["maximumRunDurationMs"] = maximum

        timeouts = pointer(self.vector, ("timeouts",))
        bound = live_capture_lower_bound_ms(self.catalog, CATALOG_POINTER)
        live_capture = int(timeouts.get("liveCaptureMs", 0))
        if live_capture < bound:
            raise _Refusal(
                "LIVE_CAPTURE_TIMEOUT_TOO_SHORT",
                "timeouts.liveCaptureMs is shorter than the catalog-derived "
                "collection window")
        if int(timeouts.get("defaultStepMs", 0)) <= 0:
            raise _Refusal("STEP_TIMEOUT_ABSENT",
                           "timeouts.defaultStepMs must be positive")
        self.observations["liveCaptureLowerBoundMs"] = bound

        capture_root = Path(self.startup.capture_root)
        if not capture_root.is_dir():
            raise _Refusal("CAPTURE_ROOT_ABSENT",
                           "the capture root does not exist")
        if any(capture_root.iterdir()):
            raise _Refusal(
                "STALE_CAPTURE_ROOT",
                "the capture root is not empty; a previous run's evidence must "
                "never be conflated with this one")
        probe = capture_root / ".lo1-writable"
        try:
            probe.write_bytes(b"")
            probe.unlink()
        except OSError:
            raise _Refusal("CAPTURE_ROOT_NOT_WRITABLE",
                           "the capture root is not writable") from None
        state_dir = Path(self.startup.state_dir)
        if state_dir.exists() and any(state_dir.iterdir()):
            raise _Refusal(
                "STALE_DURABLE_STATE",
                "a durable state directory from a previous run is present; the "
                "harness never silently reuses or truncates it")

        declared_allowlist = [str(item) for item in
                              self.authority.get("networkAllowlist", [])]
        if not declared_allowlist:
            raise _Refusal("ALLOWLIST_EMPTY",
                           "the authority record declares no network allowlist")
        for entry in declared_allowlist:
            if "/" in entry or "*" in entry or entry.count(":") == 0:
                raise _Refusal(
                    "ALLOWLIST_NOT_EXACT",
                    "allowlist entry %s is a range or a wildcard; only exact "
                    "authorities are admitted" % entry)
        missing = sorted(set(self.allowlist) - set(declared_allowlist))
        if missing:
            raise _Refusal(
                "ALLOWLIST_INCOMPLETE",
                "the authority allowlist does not cover the vector-derived "
                "authority %s" % missing[0])
        extra = sorted(set(declared_allowlist) - set(self.allowlist))
        if extra:
            raise _Refusal(
                "ALLOWLIST_OVERBROAD",
                "the authority allowlist admits %s, which no vector origin "
                "names" % extra[0])
        return hashlib.sha256(
            (approved_run_id + "|" + not_before + "|" + not_after).encode(
                "utf-8")).hexdigest()

    # -- G-ID-09 ----------------------------------------------------------
    def _check_09(self) -> str:
        """SCOPE_RESTRICTION -- exactly SC-084 and O1_ONLY, everything else false."""
        declared = self.authority.get("scope")
        provider_scope = self.provider["authority"].get("scope", {})
        if not isinstance(declared, Mapping):
            raise _Refusal("SCOPE_ABSENT",
                           "the authority record declares no scope")
        if str(declared.get("scenarioScope")) != LIVE_O1_SCENARIO or \
                str(provider_scope.get("scenarioScope")) != LIVE_O1_SCENARIO:
            raise _Refusal("SCENARIO_SCOPE",
                           "the admitted scenario scope is not %s" % LIVE_O1_SCENARIO)
        source_map = declared.get("scenarioSourceMap")
        if not isinstance(source_map, Mapping) or len(source_map) != 1 or \
                str(source_map.get(LIVE_O1_SCENARIO)) != CATALOG_POINTER:
            raise _Refusal(
                "SCENARIO_SET_NOT_SINGULAR",
                "the scenario source map must contain exactly the one %s entry"
                % LIVE_O1_SCENARIO)
        if declared.get("o1Only") is not True or \
                provider_scope.get("o1Only") is not True:
            raise _Refusal("SCOPE_NOT_O1_ONLY", "the admitted scope is not O1_ONLY")
        for name in SCOPE_PERMISSIONS:
            if declared.get(name) is not False:
                raise _Refusal(
                    "PERMISSION_NOT_FALSE",
                    "%s must be present and false on the live-O1 profile" % name)
        assignment_document = self.bundle.execution_profile_assignment or {}
        frozen_scope = set(assignment_document.get("profiles", {}).get(
            "live-O1", {}).get("liveScope", []))
        live_scope = {str(item) for item in declared.get("liveScope", [])}
        if not live_scope or not live_scope <= frozen_scope:
            raise _Refusal(
                "LIVE_SCOPE_EXCEEDED",
                "the declared live scope is not a subset of the frozen "
                "live-O1 liveScope")
        prohibited = set(self.assignment.get("prohibitedTargetKinds", []))
        answered = {SCOPE_PERMISSIONS[name] for name in SCOPE_PERMISSIONS}
        unanswered = sorted(prohibited - answered)
        if unanswered:
            raise _Refusal(
                "PROHIBITED_KIND_UNANSWERED",
                "prohibited target kind %s is answered by neither a false "
                "permission nor an armed guard rule" % unanswered[0])
        hardware = assignment_document.get("hardwareAuthorization", {})
        for name in ("usrp", "ota", "liveE2Control", "liveRanWrite"):
            if str(hardware.get(name)) != "NOT_AUTHORIZED":
                raise _Refusal(
                    "HARDWARE_AUTHORIZATION",
                    "the frozen assignment does not mark %s NOT_AUTHORIZED"
                    % name)
        self.scope = {
            "scenarioScope": LIVE_O1_SCENARIO,
            "o1Only": True,
            "liveE2Control": False,
            "liveRanWrite": False,
            "otaTransmission": False,
            "usrpRadio": False,
            "upperProductionDeployment": False,
            "peerInternalSourceImport": False,
        }
        self.observations["liveScope"] = sorted(live_scope)
        self.adapter_actions = self._adapter_assignment()
        # No unresolved ledger entry is written here any more.  An unassigned
        # or partly assigned NETCONF module used to be recorded as a permitted
        # unresolved value; G-ID-07 now refuses it outright, with one reason
        # code per way of failing, so reaching this line means the three
        # mandatory O1 readiness actions are assigned and digest-bound.
        self.observations["netconfAssignment"] = {
            "adapterActionsAssignedToUpper": sorted(self.adapter_actions),
            "authorityRecordDigest": self.observations["authorityRecordDigest"],
            "roleTablePointer": "/initialStates",
        }
        return hashlib.sha256(
            json.dumps(self.scope, sort_keys=True).encode("utf-8")).hexdigest()

    # -- G-ID-10 ----------------------------------------------------------
    def _check_10(self) -> str:
        """PROHIBITIONS, PROVIDER KIND and the UNRESOLVED LEDGER, together."""
        # PROHIBITION: mutable tag / local image ID (re-check of G-ID-03).
        digest = str(self.provider["image"]["ociImageManifestDigest"])
        if not _is_oci_digest(digest):
            raise _Refusal("MUTABLE_TAG",
                           "a non-digest image reference reached admission")
        if self.provider["image"].get("localImageIdIsNotAcceptable") is not True:
            raise _Refusal("LOCAL_IMAGE_ID",
                           "a local image ID would be acceptable to the "
                           "Provider contract")
        # PROHIBITION: unpinned endpoint (re-check of G-ID-06).
        if not self.allowlist:
            raise _Refusal("UNPINNED_ENDPOINT",
                           "no origin resolved to an exact authority")
        # PROHIBITION: a real secret inside the package, at START time.
        #
        # The frozen contract bundle is excluded, and the exclusion is earned
        # rather than assumed.  Those bytes legitimately contain
        # credential-shaped substrings -- the frozen scenario catalog drives its
        # 401/403 negative paths with literal authorization-header values -- and
        # they are outside this release's authority to edit, so a gate that
        # rejected its own frozen input could only pass by editing bytes it must
        # not touch.  What binds them is stronger than a scan: G-ID-01 has
        # already compared every file in the bundle, one at a time, against the
        # per-file digests in the contract lock, and refused on any difference.
        offender, excluded = _packaged_secret(
            self.startup.release_root,
            frozen_root=Path(self.startup.contract_authority))
        if offender is not None:
            raise _Refusal("SECRET_IN_PACKAGE",
                           "the extracted release tree carries credential "
                           "material at %s" % offender)
        self.observations["frozenAuthorityMembersExcludedFromByteScan"] = excluded
        # PROHIBITION: self-issued authority (re-check of G-ID-05).
        signer = str(self.authority.get("signerIdentity", ""))
        authorities = self.authority.get("releaseAuthorities", {})
        if signer in {RELEASE_ID, str(authorities.get("upper")),
                      str(authorities.get("provider"))}:
            raise _Refusal("SELF_ISSUED_AUTHORITY",
                           "the execution authority is self-issued")

        # The deterministic KPM / E2-control / readback boundary: the upper
        # ships it either way, but which side SERVES it is an integration
        # authority assignment the gate refuses to guess.
        routing = str(self.provider["authority"].get(
            "deterministicStubRouting", UNRESOLVED))
        if routing not in STUB_ROUTINGS:
            self.unresolved.append(UnresolvedEntry(
                field="authority.deterministicStubRouting",
                owed_by="LIVE_O1_INTEGRATION_AUTHORITY",
                blocks_check="G-ID-10",
                meaning="which side serves the deterministic KPM/E2-control/"
                        "readback boundary; the upper ships the stub either "
                        "way but never guesses the routing",
                permitted_unresolved=False))
            raise _Refusal("STUB_ROUTING_UNRESOLVED",
                           "the deterministic stub routing is unresolved")
        self.stub_routing = routing

        # Emulator / production split, decided here once and for all.
        self_test = self.startup.profile == PROFILE_SELF_TEST
        declared_kind = str(self.authority.get(
            "providerKind", PROVIDER_KIND_LIVE if not self_test
            else PROVIDER_KIND_EMULATOR))
        if self_test:
            if declared_kind != PROVIDER_KIND_EMULATOR:
                raise _Refusal(
                    "EMULATOR_AS_PRODUCTION",
                    "a live Provider authority is present while the startup "
                    "profile is the self-test profile")
        else:
            if declared_kind != PROVIDER_KIND_LIVE:
                raise _Refusal(
                    "EMULATOR_AS_PRODUCTION",
                    "the live profile admits only a Provider under separate "
                    "authority")
            loopback = sorted(
                authority for authority in self.allowlist
                if _is_loopback(authority)
                and authority not in self.observations.get("upperListeners", []))
            if loopback:
                raise _Refusal(
                    "EMULATOR_REACHABLE_UNDER_LIVE_PROFILE",
                    "the lower origin %s is a loopback authority, which under "
                    "the live profile can only be an emulator" % loopback[0])
        # Every remaining UNRESOLVED token in an admitted input is a refusal
        # when the admitted provider kind requires the value.
        blocking = [entry for entry in self.unresolved
                    if not entry.permitted_unresolved]
        if blocking:
            raise _Refusal("UNRESOLVED_REQUIRED_FIELD",
                           "the admitted provider kind requires %s"
                           % blocking[0].field)
        self.observations["providerKind"] = declared_kind
        return hashlib.sha256(
            (digest + "|" + declared_kind + "|" + routing).encode("utf-8")).hexdigest()


# -- helpers ---------------------------------------------------------------

def content_set_digest(members: Mapping[str, Any]) -> str:
    """CONTENT_SET digest: over sorted member path plus per-member digest.

    Never equal to the RAW_ARCHIVE digest of the ``.tar.gz`` container, which is
    exactly why the two carry their meaning in their names.
    """
    lines = ["%s  %s" % (str(digest), str(path))
             for path, digest in sorted(dict(members).items())]
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


def _default_scope() -> dict[str, Any]:
    return {
        "scenarioScope": LIVE_O1_SCENARIO,
        "o1Only": True,
        "liveE2Control": False,
        "liveRanWrite": False,
        "otaTransmission": False,
        "usrpRadio": False,
        "upperProductionDeployment": False,
        "peerInternalSourceImport": False,
    }


def _spec_file(startup: Lo1StartupConfig, name: str) -> Path:
    for root in (startup.spec_dir,
                 Path(__file__).resolve().parents[3] / "docs" / "upper-live-o1-harness"):
        candidate = Path(root) / name
        if candidate.is_file():
            return candidate
    raise _Refusal("SPEC_FILE_ABSENT", "the frozen %s was not found" % name)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _Refusal("DOCUMENT_UNREADABLE", "cannot read the %s" % label) from exc
    if not isinstance(document, dict):
        raise _Refusal("DOCUMENT_SHAPE", "the %s must be one JSON object" % label)
    return document


def _digest_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _digest_record(path: Path, record: str) -> tuple[str, str]:
    """Read ``<record>  <digest>  <name>`` out of an out-of-band digest file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "", ""
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[0] == record:
            return fields[1], fields[2]
    return "", ""


def _contract_digests(bundle: ContractBundle) -> dict[str, str]:
    root = Path(bundle.path)
    version = bundle.version
    names = {
        "handoffManifestSha256": Path(bundle.authority_path)
        / ("handoff-manifest.%s.json" % version),
        "bundleManifestSha256": root / ("bundle-manifest.%s.json" % version),
        "catalogSha256": root / ("scenario-catalog.%s.json" % version),
        "runnerContractSha256": root / ("scenario-runner-contract.%s.json" % version),
        "profileAssignmentSha256": root
        / ("execution-profile-assignment.%s.json" % version),
        "deploymentVectorSchemaSha256": root
        / "deployment-test-vector.1.0.0.schema.json",
        "o1NetconfYangProfileSha256": root / "o1-netconf-yang-profile.1.0.0.json",
        "o1PaFileProfileSha256": root / "oran-aic-o1-pa-file.1.0.0.json",
        "policyEvidenceSchemaSha256": root / "aic.policy-evidence.1.0.0.schema.json",
    }
    return {key: _digest_file(path) for key, path in names.items()}


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(character in _HEX64 for character in value)


def _is_oci_digest(value: str) -> bool:
    return value.startswith("sha256:") and _is_hex(value[7:], 64)


def _unresolved_pointers(document: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(document, Mapping):
        for key, value in document.items():
            if str(key) == "_comment":
                continue
            found.extend(_unresolved_pointers(value, prefix + "/" + str(key)))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            found.extend(_unresolved_pointers(value, prefix + "/" + str(index)))
    elif isinstance(document, str) and document == UNRESOLVED:
        found.append(prefix)
    return found


def _own_oci_blob_paths(release_root: Path) -> frozenset[Path]:
    """The release's OWN content-addressed image blobs, if it really has some.

    ``blobs`` is a Provider-artefact directory name because a vendored Provider
    image would land there.  The upper release packages its own deterministic
    OCI image layout, whose blob directory is the same word, so a bare name ban
    refuses the very artefact this release is required to ship.  The rule is
    therefore made to say what it means: a ``blobs`` directory is admitted only
    when it is *this* release's image layout -- ``oci/oci-layout`` and
    ``oci/index.json`` beside it, and every blob hashing to its own file name.
    A forged blob, or a ``blobs`` directory anywhere else, is still refused,
    which is strictly more than the name ban caught.
    """
    root = Path(release_root) / "oci"
    if not (root / "oci-layout").is_file() or not (root / "index.json").is_file():
        return frozenset()
    blobs = root / "blobs"
    if not blobs.is_dir():
        return frozenset()
    permitted = {blobs}
    for path in sorted(blobs.rglob("*")):
        if path.is_dir():
            permitted.add(path)
            continue
        if not path.is_file() or _digest_file(path) != path.name:
            return frozenset()
        permitted.add(path)
    return frozenset(permitted)


def _packaged_provider_artefact(release_root: Path) -> str | None:
    root = Path(release_root)
    if not root.is_dir():
        return None
    own_blobs = _own_oci_blob_paths(root)
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            if path.name in PROVIDER_ARTEFACT_DIRECTORIES and path not in own_blobs:
                return str(path.relative_to(root))
            continue
        if path.suffix.lower() in PROVIDER_ARTEFACT_SUFFIXES:
            return str(path.relative_to(root))
    return None


#: Key or certificate stores refused by what the file IS.  A member-name rule is
#: a stronger and far more precise statement than searching source text for the
#: words ``known_hosts`` or ``.pem``, which is what the earlier detector did.
_KEY_MATERIAL_BASENAMES = frozenset((
    "authorized_keys", "known_hosts", "known-hosts", "id_rsa", "id_dsa",
    "id_ecdsa", "id_ed25519", "ssh_host_rsa_key", "ssh_host_ed25519_key",
))
_KEY_MATERIAL_SUFFIXES = frozenset((
    ".pem", ".crt", ".cer", ".der", ".key", ".p12", ".pfx", ".jks",
    ".keystore", ".pub",
))

#: Credential and key MATERIAL, not vocabulary.  The runtime, the capture
#: recorder and the independent verifier each define the marker table they scan
#: for, so the words ``-----BEGIN``, ``Bearer`` and ``password=`` legitimately
#: appear in this release as *definitions*.  A substring rule reports every one
#: of them and therefore refuses the release it is meant to protect.  These
#: expressions restate the packaging gate's corrected predicates
#: (``scripts/lo1_release_common.py``); the runtime may not import the packaging
#: layer -- it is not on the launcher's path -- so the two are compared by test
#: rather than trusted to stay aligned.
_PEM_BLOCK = re.compile(
    rb"-----BEGIN [A-Z0-9 ]{1,48}-----[\r\n]+[A-Za-z0-9+/=\r\n]{32,}?"
    rb"-----END [A-Z0-9 ]{1,48}-----")
_PUTTY_KEY = re.compile(rb"PuTTY-User-Key-File-[0-9]+\s*:")
_SSH_KEY_LINE = re.compile(
    rb"(?:ssh-rsa|ssh-dss|ssh-ed25519|sk-ssh-ed25519@openssh\.com|"
    rb"ecdsa-sha2-nistp(?:256|384|521))\s+AAAA[A-Za-z0-9+/]{32,}={0,3}")
_BEARER_VALUE = re.compile(
    rb"(?<![A-Za-z0-9_])[Bb]earer[ \t]+([A-Za-z0-9._~+/=-]{8,})")
_ASSIGNED_SECRET = re.compile(
    rb"(?<![A-Za-z0-9_])(?:password|passwd|secret|api[_-]?key|apikey|"
    rb"client[_-]?secret)"
    rb"(?![A-Za-z0-9_])[\"']?[ \t]*[=:][ \t]*[\"']?(?!//)([A-Za-z0-9._~+/=-]{6,})")
_PLACEHOLDER_VALUES = re.compile(
    rb"^(?:REDACTED|redacted|PLACEHOLDER|placeholder|EXAMPLE|example|CHANGEME|"
    rb"changeme|UNRESOLVED[A-Z_]*|TODO|None|null|true|false|[Xx]{6,}|[0-9]+)$")


def secret_material_findings(content: bytes) -> list[str]:
    """Hazard classes present in ``content`` as material rather than vocabulary."""
    found: list[str] = []
    if _PEM_BLOCK.search(content) or _PUTTY_KEY.search(content):
        found.append("pem-private-key-block")
    if _SSH_KEY_LINE.search(content):
        found.append("ssh-key-line")
    if _BEARER_VALUE.search(content):
        found.append("bearer-token-value")
    for match in _ASSIGNED_SECRET.finditer(content):
        if not _PLACEHOLDER_VALUES.match(match.group(1)):
            found.append("assigned-secret-value")
            break
    return found


def _packaged_secret(release_root: Path,
                     *, frozen_root: Path | None = None) -> tuple[str | None, int]:
    """Return the first member carrying credential material, and the frozen count.

    ``frozen_root`` names the packaged contract authority, whose bytes this
    release copies unchanged and may not edit.  Those members are excluded from
    the byte scan and counted, so the exclusion is measured rather than assumed.
    They are not unbound by the exclusion: G-ID-01 compares every one of them
    against the contract lock's per-file digest before this check runs.
    """
    root = Path(release_root)
    if not root.is_dir():
        return None, 0
    own_blobs = _own_oci_blob_paths(root)
    frozen = Path(frozen_root).resolve() if frozen_root is not None else None
    excluded = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if frozen is not None and frozen in path.resolve().parents:
            excluded += 1
            continue
        if (path.name in _KEY_MATERIAL_BASENAMES
                or path.suffix.lower() in _KEY_MATERIAL_SUFFIXES):
            return str(path.relative_to(root)), excluded
        if path in own_blobs:
            # A self-naming blob is a derivation of members already scanned
            # individually; scanning the container would re-report the frozen
            # contract's negative-path fixtures through an opaque wrapper.
            continue
        try:
            payload = path.read_bytes()
        except OSError:  # pragma: no cover - defensive
            continue
        if secret_material_findings(payload):
            return str(path.relative_to(root)), excluded
    return None, excluded


def _resolve_addresses(host: str) -> tuple[str, ...]:
    try:
        ipaddress.ip_address(host)
        return (host,)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return ()
    return tuple(sorted({info[4][0] for info in infos}))


def _is_unspecified(host: str) -> bool:
    """The all-interfaces address, recognised without writing it out."""
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def _is_loopback(authority: str) -> bool:
    text = authority
    host = text[1:text.index("]")] if text.startswith("[") else text.rsplit(":", 1)[0]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _instant(value: str) -> datetime:
    text = str(value)
    if not text.endswith("Z"):
        raise ValueError("only UTC instants are accepted: %s" % text)
    return datetime.fromisoformat(text[:-1] + "+00:00")
