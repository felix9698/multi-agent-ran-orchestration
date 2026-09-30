"""Digest-pinned reader for a composed release's contract documents.

What this module is for
-----------------------
The deployment binds ``oran-aic-lower-integration/1.0.0`` by *contract*.  That
published release provides a small set of contract documents - a
capability manifest, a release manifest, a contract-digest record, an E2
inventory, an endpoint descriptor, a status source and an integration-inputs
fragment - and those documents, under their published SHA-256 digests, are the
only thing this deployment binding is allowed to read.

What it deliberately does not do
--------------------------------
* It never reads, extracts or imports composed-release production source.  The release
  ships ``artifacts/*.tar.gz``; :data:`CONTRACT_FILES` is an allowlist, so those
  archives are unreachable through this module by construction rather than by
  convention, and :func:`DeploymentBindingContracts.load` refuses a root whose
  contract set is incomplete rather than falling back to anything inside them.
* It resolves no endpoint and invents no value.  Every endpoint in the composed
  descriptor is ``UNRESOLVED`` because endpoints are the deployment's to state;
  a reader that filled one in would be manufacturing a deployment fact.

Trust chain
-----------
Four digests are pinned out of band in the integration task and repeated in the
receipt marker: the handoff archive, the release manifest, the capability
manifest, the E2 inventory release manifest and the standalone verifier.  From
those:

* ``RELEASE-MANIFEST.json`` pins ``CONTRACT-DIGESTS.json`` and re-states the
  capability and E2-inventory digests, so those three cross-check.
* ``LOWER-INTEGRATION-INPUTS.fragment.json`` pins ``STATUS-SOURCE.json``.
* ``SHA256SUMS`` covers every remaining contract file, and the standalone
  verifier - itself pinned out of band - is what checked that membership when
  the receipt recorded ``44/44 PASS``.

Every one of those links is recomputed here from the bytes on disk.  A mismatch
anywhere fails closed with the artifact named.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

#: Contract documents this module may read, and the whole of what it may read.
#: ``artifacts/`` and ``records/`` are absent on purpose: the first holds release
#: production source and evidence archives, the second holds run evidence that
#: belongs to the composed release's own claim record, not to this binding.
CONTRACT_FILES: Tuple[str, ...] = (
    "RECEIPT-OK",
    "SHA256SUMS",
    "RELEASE-MANIFEST.json",
    "CAPABILITY-MANIFEST.json",
    "CONTRACT-DIGESTS.json",
    "E2-INVENTORY-RELEASE-MANIFEST.json",
    "ENDPOINT-DESCRIPTOR.json",
    "STATUS-SOURCE.json",
    "LOWER-INTEGRATION-INPUTS.fragment.json",
    "DEPLOYMENT-TEST-VECTOR-LOWER.fragment.json",
)

#: Paths this module refuses to touch even if a caller names them.  Listed so
#: the refusal is testable rather than merely absent from the allowlist.
FORBIDDEN_PREFIXES: Tuple[str, ...] = ("artifacts/", "records/", "src/")


class DeploymentBindingError(ValueError):
    """The composed-release contracts are absent, incomplete or not as pinned."""


@dataclass(frozen=True)
class DeploymentBindingIdentity:
    """The exact published components a deployment is allowed to bind."""

    release: str
    tag: str
    commit: str
    tree: str
    handoff_archive_sha256: str
    source_archive_sha256: str
    release_manifest_sha256: str
    capability_manifest_sha256: str
    e2_inventory_release_manifest_sha256: str
    standalone_verifier_sha256: str
    composed_component_release: str
    provider_release: str
    provider_oci_manifest_digest: str
    corrected_handoff_version: str
    wire_policy_profile: str
    official_objectives: Tuple[str, ...]

    @property
    def upper_release(self) -> str:
        """Compatibility accessor for callers using the retired field name."""
        return self.composed_component_release

    def as_dict(self) -> Dict[str, Any]:
        return {
            "release": self.release, "tag": self.tag, "commit": self.commit,
            "tree": self.tree,
            "handoffArchiveSha256": self.handoff_archive_sha256,
            "sourceArchiveSha256": self.source_archive_sha256,
            "releaseManifestSha256": self.release_manifest_sha256,
            "capabilityManifestSha256": self.capability_manifest_sha256,
            "e2InventoryReleaseManifestSha256":
                self.e2_inventory_release_manifest_sha256,
            "standaloneVerifierSha256": self.standalone_verifier_sha256,
            "composedComponentRelease": self.composed_component_release,
            "upperRelease": self.composed_component_release,
            "providerRelease": self.provider_release,
            "providerOciManifestDigest": self.provider_oci_manifest_digest,
            "correctedHandoffVersion": self.corrected_handoff_version,
            "wirePolicyProfile": self.wire_policy_profile,
            "officialObjectives": list(self.official_objectives),
        }


#: The one deployment binding this tree accepts.  Every value is an
#: out-of-band identity or digest from the integration authority; none of them
#: is read from the release being verified, which is the point - a release that
#: rewrote its own manifest could otherwise attest to itself.
FROZEN_DEPLOYMENT_BINDING = DeploymentBindingIdentity(
    release="oran-aic-lower-integration/1.0.0",
    tag="oran-aic-lower-integration-1.0.0",
    commit="9f607c336a6f8b55c0421d0e0e4cabb498180b5a",
    tree="ff60c53ea4025e78f583269d98299ebf743b1262",
    handoff_archive_sha256=(
        "a45180c5974697678faf8056f71a028954c284de8acb56393813e4122874dbb6"),
    source_archive_sha256=(
        "b68d4b0ad027b79c9af73e2a13a94eaa186c0e28f3557fee11531392da47604f"),
    release_manifest_sha256=(
        "28759bb9efe1af0868e231691b1558b4a26e355fcca44b9ee48c9cfdb943b7a3"),
    capability_manifest_sha256=(
        "1b37b22f5e817cafa8e49998c9f426daffba14c6355ceb580ccaade490e6f9e1"),
    e2_inventory_release_manifest_sha256=(
        "9c1977c3710785ac7e1f927310eb9267c6dfd0821ac4680210a333f8658eb6aa"),
    standalone_verifier_sha256=(
        "ebbc7bcf799918ea4c602a1756f4c5d5d6c0b667dfb223699758baa6bdbcacda"),
    composed_component_release="upper-live-o1-harness/1.0.9",
    provider_release="oran-aic-o1-provider/1.0.4",
    provider_oci_manifest_digest=(
        "sha256:7a8cba9f004e7a64f0c76aa90ebd2fd5aa22e252c4e3062383d6e43391e6f639"),
    corrected_handoff_version="1.0.1",
    wire_policy_profile="oran-aic/1.0.0",
    official_objectives=("PIN_TO_CELL",),
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(root: Path, name: str) -> bytes:
    if name not in CONTRACT_FILES and name != "verify_lower_integration_handoff.py":
        raise DeploymentBindingError(
            f"{name} is not a composed-release contract document; this binding reads "
            "contracts only")
    path = root / name
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DeploymentBindingError(
            f"composed-release contract {name} is not readable at {root}: {exc}") from exc


def _json(root: Path, name: str) -> Dict[str, Any]:
    raw = _read(root, name)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentBindingError(
            f"composed-release contract {name} is not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise DeploymentBindingError(f"composed-release contract {name} is not an object")
    return value


def _receipt(raw: bytes) -> Dict[str, str]:
    """Parse the fresh-recipient marker's ``key: value`` lines."""
    fields: Dict[str, str] = {}
    for line in raw.decode("utf-8").splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip("'")
    return fields


def _checksums(raw: bytes) -> Dict[str, str]:
    sums: Dict[str, str] = {}
    for line in raw.decode("utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1]] = parts[0]
    return sums


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DeploymentBindingError(message)


@dataclass(frozen=True)
class DeploymentBindingContracts:
    """One verified composed release, reduced to binding-consumed facts."""

    root: str
    identity: DeploymentBindingIdentity
    receipt: Mapping[str, str]
    release_manifest: Mapping[str, Any]
    capability: Mapping[str, Any]
    contract_digests: Mapping[str, Any]
    e2_inventory: Mapping[str, Any]
    endpoint_descriptor: Mapping[str, Any]
    status_source: Mapping[str, Any]
    integration_inputs: Mapping[str, Any]
    deployment_fragment: Mapping[str, Any]

    # -- construction ------------------------------------------------------- #

    @classmethod
    def load(cls, root: Any, *,
             identity: DeploymentBindingIdentity = FROZEN_DEPLOYMENT_BINDING
             ) -> "DeploymentBindingContracts":
        """Verify and load one extracted composed-release root.

        The root must be a completed fresh-recipient extraction: the receipt
        marker is what says the archive digest was checked against the
        out-of-band value before anything was unpacked.  Without it this is an
        unverified directory that merely looks like a release, so it is refused
        rather than read.
        """
        base = Path(root).expanduser()
        if not base.is_dir():
            raise DeploymentBindingError(
                f"composed release root {base} does not exist; the release "
                "is unavailable to this deployment binding")
        marker = base / "RECEIPT-OK"
        if not marker.is_file():
            raise DeploymentBindingError(
                f"{base} carries no RECEIPT-OK marker, so no fresh-recipient "
                "verification of the handoff archive has been recorded for it")

        receipt = _receipt(_read(base, "RECEIPT-OK"))
        _require(receipt.get("receiptStatus") == "PASS",
                 "the composed-release receipt marker does not record receiptStatus PASS")
        for field, expected, label in (
                ("release", identity.release, "release"),
                ("releaseCommit", identity.commit, "source commit"),
                ("releaseTree", identity.tree, "source tree"),
                ("handoffArchiveSha256", identity.handoff_archive_sha256,
                 "handoff archive digest"),
                ("sourceArchiveSha256", identity.source_archive_sha256,
                 "source archive digest"),
                ("releaseManifestSha256", identity.release_manifest_sha256,
                 "release manifest digest"),
                ("capabilityManifestSha256", identity.capability_manifest_sha256,
                 "capability manifest digest"),
                ("e2InventoryReleaseManifestSha256",
                 identity.e2_inventory_release_manifest_sha256,
                 "E2 inventory release manifest digest"),
                ("standaloneVerifierSha256", identity.standalone_verifier_sha256,
                 "standalone verifier digest")):
            _require(receipt.get(field) == expected,
                     f"the composed-release receipt {label} is {receipt.get(field)!r}, not "
                     f"the pinned {expected!r}")

        # Bytes on disk, not the receipt's word for them.
        digests = {name: _sha256(_read(base, name))
                   for name in CONTRACT_FILES if (base / name).is_file()}
        for name, expected in (
                ("RELEASE-MANIFEST.json", identity.release_manifest_sha256),
                ("CAPABILITY-MANIFEST.json", identity.capability_manifest_sha256),
                ("E2-INVENTORY-RELEASE-MANIFEST.json",
                 identity.e2_inventory_release_manifest_sha256)):
            _require(digests.get(name) == expected,
                     f"{name} hashes to {digests.get(name)} but the pinned "
                     f"digest is {expected}")
        verifier = base / "verify_lower_integration_handoff.py"
        _require(verifier.is_file(),
                 "the composed release ships no standalone verifier")
        _require(_sha256(verifier.read_bytes()) ==
                 identity.standalone_verifier_sha256,
                 "the composed-release standalone verifier does not hash to its pinned "
                 "digest, so its membership result cannot be relied on")

        checksums = _checksums(_read(base, "SHA256SUMS"))
        for name, actual in digests.items():
            if name in ("RECEIPT-OK", "SHA256SUMS"):
                continue  # neither is a member of the release's own checksum list
            _require(checksums.get(name) == actual,
                     f"{name} is not the file SHA256SUMS names "
                     f"({actual} vs {checksums.get(name)})")
        missing = [name for name in CONTRACT_FILES if name not in digests]
        _require(not missing,
                 "the composed release root is missing contract documents: "
                 + ", ".join(missing))

        release_manifest = _json(base, "RELEASE-MANIFEST.json")
        capability = _json(base, "CAPABILITY-MANIFEST.json")
        contract_digests = _json(base, "CONTRACT-DIGESTS.json")
        e2_inventory = _json(base, "E2-INVENTORY-RELEASE-MANIFEST.json")
        endpoint_descriptor = _json(base, "ENDPOINT-DESCRIPTOR.json")
        status_source = _json(base, "STATUS-SOURCE.json")
        integration_inputs = _json(base, "LOWER-INTEGRATION-INPUTS.fragment.json")
        deployment_fragment = _json(
            base, "DEPLOYMENT-TEST-VECTOR-LOWER.fragment.json")

        manifests = release_manifest.get("manifests", {})
        _require(
            manifests.get("contract", {}).get("sha256") ==
            digests["CONTRACT-DIGESTS.json"],
            "the release manifest's contract-digest entry does not match the "
            "CONTRACT-DIGESTS.json bytes")
        _require(
            manifests.get("capability", {}).get("sha256") ==
            identity.capability_manifest_sha256,
            "the release manifest's capability entry does not match the pinned "
            "capability manifest digest")
        _require(release_manifest.get("release") == identity.release,
                 "the release manifest names a different release")
        _require(release_manifest.get("source", {}).get("commit") == identity.commit
                 and release_manifest.get("source", {}).get("tree") == identity.tree,
                 "the release manifest names a different source commit/tree")
        _require(release_manifest.get("standardDiffFileCount") == 0,
                 "the composed release reports a modified frozen standard/ tree")
        _require(release_manifest.get("upper", {}).get("release") ==
                 identity.composed_component_release,
                 "the release manifest was not built against the pinned composed "
                 "component release")
        provider = release_manifest.get("provider", {})
        _require(provider.get("release") == identity.provider_release and
                 provider.get("ociManifestDigest") ==
                 identity.provider_oci_manifest_digest,
                 "the release manifest names a different O1 Provider binding")
        _require(tuple(release_manifest.get("officialObjectives") or ()) ==
                 identity.official_objectives,
                 "the release manifest advertises official objectives other "
                 f"than {list(identity.official_objectives)}")

        _require(contract_digests.get("correctedHandoffVersion") ==
                 identity.corrected_handoff_version,
                 "the composed-release contract digests name a different corrected handoff")
        _require(contract_digests.get("wirePolicyProfile") ==
                 identity.wire_policy_profile,
                 "the composed-release contract digests name a different wire policy profile")
        _require(contract_digests.get("frozenLowerStandard", {})
                 .get("diffFileCountAtSourceCommit") == 0,
                 "the composed-release contract digests report a modified frozen standard/")

        _require(capability.get("release") == identity.release and
                 capability.get("sourceCommit") == identity.commit,
                 "the composed-release capability manifest belongs to a different release")

        status = _json_pointer(integration_inputs, status_source_digest_pointer())
        _require(status == digests["STATUS-SOURCE.json"],
                 "the integration-inputs fragment pins a different "
                 "STATUS-SOURCE.json digest")

        return cls(
            root=str(base), identity=identity, receipt=receipt,
            release_manifest=release_manifest, capability=capability,
            contract_digests=contract_digests, e2_inventory=e2_inventory,
            endpoint_descriptor=endpoint_descriptor, status_source=status_source,
            integration_inputs=integration_inputs,
            deployment_fragment=deployment_fragment)

    # -- what the composition consumes -------------------------------------- #

    @property
    def official_a1_policy(self) -> Mapping[str, Any]:
        value = self.capability.get("officialA1Policy")
        if not isinstance(value, Mapping):
            raise DeploymentBindingError(
                "the composed-release capability manifest declares no officialA1Policy")
        return value

    @property
    def official_objectives(self) -> Tuple[str, ...]:
        return tuple(self.official_a1_policy.get("objectives") or ())

    @property
    def control_path(self) -> Tuple[str, ...]:
        return tuple(self.official_a1_policy.get("path") or ())

    @property
    def policy_type_id(self) -> str:
        return str(self.official_a1_policy.get("policyTypeId") or "")

    @property
    def experimental_extensions(self) -> Mapping[str, Any]:
        value = self.capability.get("experimentalExtensions")
        return value if isinstance(value, Mapping) else {}

    @property
    def claim_boundaries(self) -> Mapping[str, Any]:
        value = self.capability.get("claimBoundaries")
        return value if isinstance(value, Mapping) else {}

    def observable(self) -> Mapping[str, Any]:
        """The joint KPI/status-field declaration, verbatim.

        ``observableKpis`` keeps its interface prefix (``E2:`` / ``O1:``) because
        per-UE ``RRU.PrbTotDl`` over E2 and cell-scope ``RRU.PrbDl`` over O1 are
        different measurements; summing, renaming or aliasing one into the other
        is exactly the substitution the contract forbids.
        """
        for entry in self.integration_inputs.get("inputs", []):
            if isinstance(entry, Mapping) and entry.get("id") == "LOWER-INPUT-07":
                values = entry.get("fieldValues")
                if isinstance(values, Mapping):
                    return values
        raise DeploymentBindingError(
            "the integration-inputs fragment declares no observable KPI input")

    def unresolved_inputs(self) -> Tuple[str, ...]:
        """Input ids the composed release leaves to the deployment, in order."""
        pending = []
        for entry in self.integration_inputs.get("inputs", []):
            if not isinstance(entry, Mapping):
                continue
            status = str(entry.get("verificationStatus") or "")
            if status != "LOWER_PROVIDED":
                pending.append(str(entry.get("id")))
        return tuple(pending)

    def binding(self) -> Dict[str, Any]:
        """The exact identity/digest record to carry into status and export."""
        record = self.identity.as_dict()
        record.update({
            "composedReleaseRoot": self.root,
            "lowerReleaseRoot": self.root,
            "receiptStatus": self.receipt.get("receiptStatus"),
            "receivedAt": self.receipt.get("receivedAt"),
            "membershipDigestVerification":
                self.receipt.get("membershipDigestVerification"),
            "contractDigests": {
                "correctedBundleManifestSha256":
                    self.contract_digests.get("correctedBundleManifestSha256"),
                "correctedHandoffManifestSha256":
                    self.contract_digests.get("correctedHandoffManifestSha256"),
                "scenarioCatalogSha256":
                    self.contract_digests.get("scenarioCatalogSha256"),
                "runnerContractSha256":
                    self.contract_digests.get("runnerContractSha256"),
                "profileAssignmentSha256":
                    self.contract_digests.get("profileAssignmentSha256"),
            },
            "controlPath": list(self.control_path),
            "policyTypeId": self.policy_type_id,
            "effectEvidence": self.official_a1_policy.get("effectEvidence"),
            "ackIsEffectEvidence": self.official_a1_policy.get(
                "ackIsEffectEvidence"),
            "claimBoundaries": dict(self.claim_boundaries),
            "unresolvedIntegrationInputs": list(self.unresolved_inputs()),
        })
        return record


#: A deployment declares its composed-release binding in a file of this name,
#: beside its integration-values document.  That is the same place the
#: deployment already states its capability manifest and deployment vector, so
#: no new configuration channel is introduced and no environment variable can
#: redirect a composition.  The file names a root only; identity and digests are
#: still checked against :data:`FROZEN_DEPLOYMENT_BINDING`, so a binding file
#: cannot point a validated composition at a different release.
BINDING_FILENAME = "deployment-binding.json"
LEGACY_BINDING_FILENAME = "lower-release-binding.json"


def resolve_deployment_binding(
        integration_path: Any, *, override: Any = None,
        identity: DeploymentBindingIdentity = FROZEN_DEPLOYMENT_BINDING
        ) -> "DeploymentBindingContracts | None":
    """Return the verified composed release this deployment is bound to, if any.

    ``override`` wins: it is the more specific statement, and it goes through
    exactly the same verification.  Otherwise the deployment's own binding file
    is used when it exists.  A deployment that declares no binding is not bound
    to a composed release, which is a fact about the deployment - it is reported as
    ``None`` rather than guessed at.

    A binding file that exists but cannot be honoured is an error, never a
    silent fall back to unbound: a deployment that meant to be Live must not
    quietly become a development composition.
    """
    if override is not None:
        return DeploymentBindingContracts.load(override, identity=identity)
    declaration = Path(integration_path).expanduser().resolve().parent / BINDING_FILENAME
    if not declaration.is_file():
        legacy_declaration = declaration.with_name(LEGACY_BINDING_FILENAME)
        if not legacy_declaration.is_file():
            return None
        declaration = legacy_declaration
    try:
        document = json.loads(declaration.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentBindingError(
            f"{declaration} is not a readable deployment binding: {exc}") from exc
    if not isinstance(document, dict):
        raise DeploymentBindingError(f"{declaration} is not a binding object")
    root = document.get("composedReleaseRoot", document.get("lowerReleaseRoot"))
    if not isinstance(root, str) or not root:
        raise DeploymentBindingError(
            f"{declaration} names no composed-release root")
    resolved = Path(root).expanduser()
    if not resolved.is_absolute():
        resolved = declaration.parent / resolved
    declared = document.get("release")
    if declared is not None and declared != identity.release:
        raise DeploymentBindingError(
            f"{declaration} declares release {declared!r}, not the pinned "
            f"{identity.release!r}")
    return DeploymentBindingContracts.load(resolved, identity=identity)


def status_source_digest_pointer() -> Tuple[str, ...]:
    """Where the integration-inputs fragment states the status-source digest."""
    return ("inputs", "LOWER-INPUT-03", "fieldValues", "sourceDigestSha256")


def _json_pointer(document: Mapping[str, Any], route: Tuple[str, ...]) -> Any:
    """Resolve ``("inputs", "<id>", ...)`` against the inputs fragment."""
    current: Any = document
    for index, part in enumerate(route):
        if index == 1 and isinstance(current, list):
            match = next((item for item in current
                          if isinstance(item, Mapping) and item.get("id") == part),
                         None)
            if match is None:
                raise DeploymentBindingError(
                    f"the integration-inputs fragment declares no {part}")
            current = match
            continue
        if not isinstance(current, Mapping) or part not in current:
            raise DeploymentBindingError(
                "the integration-inputs fragment is missing "
                + "/".join(route[:index + 1]))
        current = current[part]
    return current


__all__ = ["BINDING_FILENAME", "CONTRACT_FILES", "FORBIDDEN_PREFIXES",
           "LEGACY_BINDING_FILENAME",
           "FROZEN_DEPLOYMENT_BINDING", "DeploymentBindingContracts",
           "DeploymentBindingError", "DeploymentBindingIdentity",
           "resolve_deployment_binding"]
