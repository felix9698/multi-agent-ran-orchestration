"""Digest-pinned capability import and E2 READY semantic validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ._contract import byte_sha256, canonicalize, jcs_sha256, validate


class CapabilityError(RuntimeError):
    """Fail-closed artifact, discovery or inventory mismatch."""


@dataclass(frozen=True)
class E2InventoryResult:
    ready: bool
    error_code: str | None
    canonical_global_e2_node_ids: tuple[str, ...]
    validation_errors: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityArtifacts:
    capability_manifest: dict[str, Any]
    release_manifest: dict[str, Any]
    e2_inventory: dict[str, Any]
    capability_sha256: str
    release_sha256: str
    inventory_sha256: str
    inventory_result: E2InventoryResult

    @property
    def near_rt_ric_id(self) -> str:
        return self.capability_manifest["nearRtRicId"]

    @property
    def a1_ready(self) -> bool:
        return self.inventory_result.ready

    @classmethod
    def load(
        cls,
        values: Mapping[str, Any],
        *,
        bundle_dir: str | Path,
        base_dir: str | Path = ".",
        a1_discovery: Mapping[str, Any] | None = None,
    ) -> "CapabilityArtifacts":
        base = Path(base_dir)
        capability_path = base / values["backend.capabilityManifestPath"]
        release_path = base / values["backend.releaseManifestPath"]
        inventory_path = base / values["backend.e2CapabilityInventoryPath"]
        capability = _load_pinned(
            capability_path,
            values["backend.capabilityManifestSha256"],
            "urn:oran-aic:schema:ran-capability:1.0.0",
            bundle_dir,
        )
        release = _load_pinned(
            release_path,
            values["backend.releaseManifestSha256"],
            "urn:oran-aic:schema:backend-release-manifest:1.0.0",
            bundle_dir,
        )
        inventory = _load_pinned(
            inventory_path,
            values["backend.e2CapabilityInventorySha256"],
            "urn:oran-aic:schema:e2-capability-inventory:1.0.0",
            bundle_dir,
        )
        release_digest = values["backend.releaseManifestSha256"]
        if release["deployment"]["nearRtRicId"] != capability["nearRtRicId"]:
            raise CapabilityError("release and capability nearRtRicId mismatch")
        if inventory["releaseManifestSha256"] != release_digest:
            raise CapabilityError("inventory releaseManifestSha256 mismatch")
        _assert_release_matches_capability(release, capability)
        result = validate_e2_inventory(
            inventory,
            capability,
            bundle_dir=bundle_dir,
            release_manifest=release,
        )
        artifacts = cls(
            capability,
            release,
            inventory,
            values["backend.capabilityManifestSha256"],
            release_digest,
            values["backend.e2CapabilityInventorySha256"],
            result,
        )
        if a1_discovery is None:
            raise CapabilityError("authoritative A1 discovery result is required")
        artifacts.assert_a1_discovery(a1_discovery)
        return artifacts

    #: The ``PolicyTypeObject`` members the frozen contract defines, and the
    #: capability-manifest digest each one is pinned by.  §7.1 of
    #: ``02-rapp-xapp-backend-mandatory-contract`` (sha256
    #: ``0793b994…07ba``, the ``frozenLowerStandard.mandatoryContractSha256``
    #: the Lower recipient's ``CONTRACT-DIGESTS.json`` pins) says a Near-RT RIC
    #: returns a ``PolicyTypeObject`` containing ``policySchema`` and
    #: ``statusSchema``.  That is the whole of the resource; nothing else may be
    #: required of it.
    A1_POLICY_TYPE_SCHEMA_MEMBERS: tuple = (
        ("policySchema", "policy"),
        ("statusSchema", "status"),
    )

    def assert_a1_discovery(self, discovery: Mapping[str, Any]) -> None:
        """Bind the discovered A1 policy type to this pinned capability release.

        The identity question this answers is "am I talking to the Near-RT RIC
        that serves the release I loaded artifacts for", and it is answered from
        the evidence the A1-P v2 resource actually carries.  The frozen contract
        defines exactly two members on the ``PolicyTypeObject``
        (:data:`A1_POLICY_TYPE_SCHEMA_MEMBERS`) and no RIC identity anywhere on
        that resource, so an earlier ``nearRtRicId`` requirement here refused
        every conformant producer and admitted only this repository's own mock.
        ``nearRtRicId`` is a *backend deployment* identifier
        (``oran/contract/ids.py``); it is cross-checked where the contract does
        carry it - capability manifest against release manifest deployment, in
        :meth:`load` - and over R1, whose policy query and create body the
        contract does define it on.

        The re-anchor is strictly stronger than the string comparison it
        replaces: both discovered schemas must canonicalise (RFC 8785) to the
        digests ``schemaDigests`` pins, so a producer serving another release's
        schemas is refused, and - unlike a self-asserted digest field, which is
        also not a contract member - a producer cannot satisfy it by claiming a
        digest it does not serve.
        """
        policy_type_id = discovery.get("policyTypeId")
        if policy_type_id not in self.capability_manifest["policyTypes"]:
            raise CapabilityError("A1 discovery policyTypeId mismatch")
        digests = self.capability_manifest["schemaDigests"]
        for member, pinned in self.A1_POLICY_TYPE_SCHEMA_MEMBERS:
            schema = discovery.get(member)
            if not isinstance(schema, Mapping):
                raise CapabilityError(
                    f"A1 discovery omitted the contract member {member}")
            if jcs_sha256(schema) != digests[pinned]:
                raise CapabilityError(
                    f"A1 discovery {pinned} schema digest mismatch")


def load_capability_manifest(
    path: str | Path,
    expected_sha256: str,
    *,
    bundle_dir: str | Path,
) -> dict[str, Any]:
    return _load_pinned(
        Path(path),
        expected_sha256,
        "urn:oran-aic:schema:ran-capability:1.0.0",
        bundle_dir,
    )


def _load_pinned(
    path: Path,
    expected_sha256: str,
    schema_id: str,
    bundle_dir: str | Path,
) -> dict[str, Any]:
    actual = byte_sha256(path)
    if actual != expected_sha256:
        raise CapabilityError(f"artifact digest mismatch: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        validate(document, schema_id, bundle_dir)
    except Exception as exc:
        raise CapabilityError(f"artifact schema validation failed: {path}: {exc}") from exc
    return document


def validate_e2_inventory(
    inventory: Mapping[str, Any],
    capability: Mapping[str, Any],
    *,
    bundle_dir: str | Path | None = None,
    release_manifest: Mapping[str, Any] | None = None,
) -> E2InventoryResult:
    errors: list[str] = []
    try:
        validate(
            inventory,
            "urn:oran-aic:schema:e2-capability-inventory:1.0.0",
            bundle_dir,
        )
    except Exception as exc:
        errors.append(f"schema: {exc}")

    connections = list(inventory.get("connections", []))
    identities: list[str] = []
    for index, connection in enumerate(connections):
        node_id = connection.get("globalE2NodeId", {})
        identity = canonicalize(node_id).decode("utf-8")
        identities.append(identity)
        errors.extend(_validate_node_encoding(node_id, index))

    active_by_identity: dict[str, int] = {}
    for identity, connection in zip(identities, connections):
        if connection.get("active"):
            active_by_identity[identity] = active_by_identity.get(identity, 0) + 1
    if any(count > 1 for count in active_by_identity.values()):
        errors.append("ONE_ACTIVE_EPOCH_PER_NODE")

    if inventory.get("status") != "READY":
        errors.append("inventory status is NOT_READY")
    if len(connections) != 2 or len(set(identities)) != 2:
        errors.append("READY_DISTINCT_ACTIVE_NODE_IDS")
    if any(not connection.get("active") for connection in connections):
        errors.append("READY requires active connections")

    static_nodes = {
        canonicalize(node["globalE2NodeId"]).decode("utf-8"): node
        for node in capability.get("e2Deployment", {}).get("nodes", [])
    }
    if set(identities) != set(static_nodes):
        errors.append("READY_STATIC_CAPABILITY_EXACT_MATCH: node identities")

    for identity, connection in zip(identities, connections):
        functions = connection.get("ranFunctions", [])
        by_id: dict[int, list[Mapping[str, Any]]] = {}
        for function in functions:
            by_id.setdefault(function.get("ranFunctionId"), []).append(function)
        if set(by_id) != {2, 3} or any(len(items) != 1 for items in by_id.values()):
            errors.append("READY requires exactly KPM function 2 and RC function 3")
            continue
        if any(not items[0].get("active") for items in by_id.values()):
            errors.append("READY requires active required RAN functions")
        static = static_nodes.get(identity)
        if static is None:
            continue
        required = {item["ranFunctionId"]: item for item in static["requiredRanFunctions"]}
        for function_id in (2, 3):
            observed = by_id[function_id][0]
            expected = required.get(function_id)
            if expected is None or not _function_matches(observed, expected):
                errors.append(
                    f"READY_STATIC_CAPABILITY_EXACT_MATCH: RAN function {function_id}"
                )
        if connection.get("e2apVersion") != capability["e2Deployment"]["e2apVersion"]:
            errors.append("READY_STATIC_CAPABILITY_EXACT_MATCH: E2AP version")
        if connection.get("transferSyntax") != capability["serviceModels"]["e2ap"]["encoding"]:
            errors.append("READY_STATIC_CAPABILITY_EXACT_MATCH: transfer syntax")
        if release_manifest is not None:
            module_sets = {
                item["model"]: item["aggregateSha256"]
                for item in release_manifest["asn1ModuleSets"]
            }
            if connection.get("decoderModuleSetSha256") != module_sets.get("E2AP"):
                errors.append("READY_STATIC_CAPABILITY_EXACT_MATCH: E2AP module set")
            for function_id, model in ((2, "E2SM-KPM"), (3, "E2SM-RC")):
                observed = by_id[function_id][0]
                if observed.get("moduleSetSha256") != module_sets.get(model):
                    errors.append(
                        f"READY_STATIC_CAPABILITY_EXACT_MATCH: {model} module set"
                    )

    errors = list(dict.fromkeys(errors))
    ready = not errors
    return E2InventoryResult(
        ready,
        None if ready else "AIC_E2_INVENTORY_NOT_READY",
        tuple(sorted(set(identities))),
        tuple(errors),
    )


def _validate_node_encoding(node: Mapping[str, Any], index: int) -> list[str]:
    errors: list[str] = []
    plmn = node.get("plmn", {})
    if len(str(plmn.get("mnc", ""))) != plmn.get("mncDigitLength"):
        errors.append(f"CANONICAL_GLOBAL_E2_NODE_ID_ENCODING[{index}]: MNC length")
    bit_string = node.get("nodeId", {})
    encoded = bit_string.get("hex", "")
    bit_length = bit_string.get("bitLength")
    if not isinstance(bit_length, int) or bit_length < 1 or not encoded.startswith("0x"):
        errors.append(f"CANONICAL_GLOBAL_E2_NODE_ID_ENCODING[{index}]")
        return errors
    digits = encoded[2:]
    expected_digits = (bit_length + 3) // 4
    try:
        value = int(digits, 16)
    except ValueError:
        value = -1
    if (
        digits != digits.lower()
        or len(digits) != expected_digits
        or value < 0
        or value >= (1 << bit_length)
    ):
        errors.append(f"CANONICAL_GLOBAL_E2_NODE_ID_ENCODING[{index}]")
    return errors


def _function_matches(observed: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return all(
        (
            observed.get("ranFunctionOid") == expected.get("ranFunctionOid"),
            observed.get("ranFunctionRevision")
            == expected.get("observedRanFunctionRevision"),
            observed.get("rawDefinition", {}).get("sha256")
            == expected.get("rawDefinitionSha256"),
            observed.get("canonicalDefinition", {}).get("sha256")
            == expected.get("canonicalDecodedDefinitionSha256"),
        )
    )


def _assert_release_matches_capability(
    release: Mapping[str, Any], capability: Mapping[str, Any]
) -> None:
    provenance = capability["softwareProvenance"]
    pairs = (
        (release["flexric"]["commit"], provenance["flexRicCommitSha1"], "FlexRIC commit"),
        (release["oai"]["release"], provenance["oaiBaseTag"], "OAI release"),
        (release["oai"]["baseCommit"], provenance["oaiBaseCommitSha1"], "OAI base commit"),
        (release["oai"]["postPatchTree"], provenance["postPatchSourceTreeSha1"], "OAI post-patch tree"),
        (
            release["oai"]["buildArtifact"]["artifactManifestSha256"],
            provenance["buildArtifactManifestSha256"],
            "OAI build artifact manifest",
        ),
    )
    for observed, expected, label in pairs:
        if observed != expected:
            raise CapabilityError(f"release/capability mismatch: {label}")
    release_patches = [
        (patch["path"], patch["sha256"]) for patch in release["oai"]["patches"]
    ]
    capability_patches = [
        (patch["path"], patch["byteSha256"])
        for patch in provenance["orderedPatchSet"]
    ]
    if release_patches != capability_patches:
        raise CapabilityError("release/capability ordered OAI patch sequence mismatch")
    profiles = {item["name"]: item["sha256"] for item in release["profiles"]}
    profile_pairs = (
        ("E2SM-RC-STYLE3-ACTION1", provenance["rcProfileArtifact"]["byteSha256"]),
        ("E2-CONTROL-APER-VECTORS", provenance["aperVectorManifest"]["byteSha256"]),
        ("OTA-READBACK-EVIDENCE", provenance["otaEvidenceManifest"]["byteSha256"]),
    )
    for name, expected in profile_pairs:
        if profiles.get(name) != expected:
            raise CapabilityError(f"release/capability profile mismatch: {name}")

    release_nodes = {
        node["role"]: {
            "nodeType": node["nodeType"],
            "plmn": node["plmn"],
            "nodeId": node["nodeId"],
            **({"cuDuComponentId": node["cuDuComponentId"]} if "cuDuComponentId" in node else {}),
        }
        for node in release["topology"]["nodes"]
    }
    capability_nodes = {
        node["role"]: node["globalE2NodeId"]
        for node in capability["e2Deployment"]["nodes"]
    }
    if {
        role: canonicalize(identity) for role, identity in release_nodes.items()
    } != {
        role: canonicalize(identity) for role, identity in capability_nodes.items()
    }:
        raise CapabilityError("release/capability topology node identity mismatch")
