"""The final O-RAN runtime composition, verified without hardware.

Three questions are answered here, and nothing about a radio is assumed.

1. **Binding.**  Does the Upper composition bind the frozen Lower release by
   contract - digest for digest - and refuse anything else?
2. **Advertisement.**  Does a bound composition advertise, let an operator
   select, and accept exactly ``PIN_TO_CELL``, while the Lower release's
   experimental Style 2 / Action 6 QoS work stays capability and provenance
   only?
3. **Round trip.**  Does one natural-language intent traverse the preserved
   coordinator exactly once, leave through R1 to the A1-P producer, come back as
   A1 status carrying a KPM-readback verdict, and reach an assurance judgement
   that no ACK alone could have produced?

Everything runs against ``oran.profiles.local_mock`` - the contract-faithful
loopback stack this repository already ships and the conformance catalog already
runs - or against a synthetic release built in a temporary directory.  Nothing
here contacts a Near-RT RIC, an xApp, FlexRIC, E2, a gNB, a UE or a USRP.
"""

import copy
import hashlib
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from decision.llm_backend import DeterministicMockBackend, LLMBackendManager
from oran.conformance.contracts import ContractBundle, canonicalize
from oran.integration.lower_release import (
    BINDING_FILENAME, CONTRACT_FILES, FROZEN_INTEGRATION_BINDING,
    LowerReleaseContracts, LowerReleaseError, LowerReleaseIdentity,
    resolve_binding,
)
from oran.integration.objectives import (
    EXECUTABLE_OBJECTIVES, ObjectiveNotExecutable, advertise, assert_submittable,
)
from oran.rapp.assurance import CombinedAssurance
from oran.rapp.contract_support import _bundle_dir
from oran.rapp.gui_entry import IntegrationError, LiveIntegration
from oran.rapp.headless import run_once
from oran.rapp.ports import AssuranceDecision

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = REPO_ROOT / "docs" / "phase-a" / "raw" / "mock-local"
INTEGRATION_VALUES = DEPLOYMENT / "integration-values.json"
INTENT_TEXT = "keep UE downlink throughput above 1 Mbps"

#: Where a completed fresh-recipient extraction of the Lower release lands.
#: Absent, the tests that need the *real* frozen bytes skip with a reason; the
#: rest run against a synthetic release and are unaffected.
LOWER_RECEIPTS = Path.home() / "OranC" / "lower-final-handoff"


def _dumps(value) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True) + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def real_lower_release_root():
    """The extracted Lower release root, if a receipt exists on this machine."""
    if not LOWER_RECEIPTS.is_dir():
        return None
    for receipt in sorted(LOWER_RECEIPTS.iterdir(), reverse=True):
        root = receipt / "oran-aic-lower-integration-1.0.0"
        if (root / "RECEIPT-OK").is_file():
            return root
    return None


def write_synthetic_release(root: Path, *, mutate=None,
                            mutate_release=None) -> LowerReleaseIdentity:
    """Write a contract-shaped Lower release and return the identity pinning it.

    The shapes are the released ones; the bytes are this test's, so the identity
    is computed rather than pinned.  ``mutate`` receives the mutable document map
    just before the digests are computed, which is how the negative cases build
    a release that is internally consistent but not the one Upper composes with.
    """
    root.mkdir(parents=True, exist_ok=True)
    documents = {
        "CAPABILITY-MANIFEST.json": {
            "schemaVersion": "oran-aic-lower-integration-capability/1.0.0",
            "release": "oran-aic-lower-integration/1.0.0",
            "sourceCommit": "9f607c336a6f8b55c0421d0e0e4cabb498180b5a",
            "officialA1Policy": {
                "policyTypeId": "AIC_UECellSteering_1.0.0",
                "objectives": ["PIN_TO_CELL"],
                "balancePrbLoadAdvertised": False,
                "controlAxes": ["serving_cell"],
                "ackIsEffectEvidence": False,
                "effectEvidence": "POST_SEND_E2SM_KPM_SERVING_CELL_READBACK",
                "liveA1Result": "APPLIED_VERIFIED",
                "liveA1Release": "oran-aic-phase-b-pin-to-cell/1.0.0",
                "path": ["R1", "NON_RT_RIC", "A1_P_V2", "A1P_PRODUCER",
                         "XAPP_WORKER", "FLEXRIC", "E2AP_2.03",
                         "E2SM_RC_1.03_STYLE3_ACTION1", "OAI_GNB"],
            },
            "experimentalExtensions": {
                "E2SM_RC_STYLE2_ACTION6_QOS": {
                    "a1PolicyType": "NONE",
                    "autonomousClosedLoopClaimed": False,
                    "controlAxis": "slice_prb_policy_ratio",
                    "mixedIntoFrozenCellSteeringPolicy": False,
                    "serviceModel": "E2SM-RC_1.03_STYLE2_ACTION6",
                    "state": "EXPERIMENTAL_LIVE_ACTUATOR_EVIDENCE",
                    "normalRanWrites": 3, "rollbackRanWrites": 1,
                },
            },
            "claimBoundaries": {
                "seamlessUserPlaneContinuity": "NOT_VERIFIED",
                "longDurationB206Stability": "NOT_VERIFIED",
            },
            "rollback": {"liveReverseRollbackVerified": False,
                         "officialA1RunState": "NOT_REQUESTED"},
        },
        "E2-INVENTORY-RELEASE-MANIFEST.json": {
            "schemaVersion": "oran-aic-lower-e2-inventory-release/1.0.0",
            "release": "oran-aic-lower-integration/1.0.0",
            "runtimeRule": "REQUIRE_FRESH_E2_SETUP_AND_CONNECTION_EPOCH_BEFORE_CONTROL",
            "officialInventory": {"ueIdentityFormat": "guAmfUeNgapId",
                                  "runtimeUeInventory": "UNRESOLVED"},
        },
        "CONTRACT-DIGESTS.json": {
            "schemaVersion": "oran-aic-lower-contract-digests/1.0.0",
            "correctedHandoffVersion": "1.0.1",
            "wirePolicyProfile": "oran-aic/1.0.0",
            "frozenLowerStandard": {"diffFileCountAtSourceCommit": 0},
        },
        "ENDPOINT-DESCRIPTOR.json": {
            "schemaVersion": "oran-aic-lower-endpoint-descriptor/1.0.0",
            "secretsIncluded": False, "actualEndpointValuesIncluded": False,
            "a1p": {"root": "UNRESOLVED",
                    "rootTemplate": "https://{A1P_HOST}:{A1P_PORT}/A1-P/v2",
                    "policyTypeId": "AIC_UECellSteering_1.0.0"},
        },
        "STATUS-SOURCE.json": {
            "schemaVersion": "oran-aic-lower-status-source/1.0.0",
            "observedAtField": "aicStatus.occurredAt",
            "capabilitySourceRef": "CAPABILITY-MANIFEST.json",
        },
        "DEPLOYMENT-TEST-VECTOR-LOWER.fragment.json": {
            "schemaVersion":
                "oran-aic-lower-deployment-test-vector-fragment/1.0.0",
            "policy": {"policyTypeId": "AIC_UECellSteering_1.0.0",
                       "supportedObjectives": ["PIN_TO_CELL"]},
        },
    }
    if mutate is not None:
        mutate(documents)

    verifier = root / "verify_lower_integration_handoff.py"
    verifier.write_bytes(b"# synthetic stand-in for the released verifier\n")
    raw = {name: _dumps(value) for name, value in documents.items()}
    for name, payload in raw.items():
        (root / name).write_bytes(payload)

    inputs = {
        "specVersion":
            "oran-aic-phase-b-gui-lower-integration-inputs-template/1.0.0",
        "lowerResolutionSummary": {"providedFieldCount": 13,
                                   "totalFieldCount": 29,
                                   "unresolvedFieldCount": 16},
        "inputs": [
            {"id": "LOWER-INPUT-01", "verificationStatus": "LOWER_PROVIDED",
             "fieldValues": {"releaseId": "oran-aic-lower-integration"}},
            {"id": "LOWER-INPUT-03", "verificationStatus": "LOWER_PROVIDED",
             "fieldValues": {
                 "capabilitySourceRef": "CAPABILITY-MANIFEST.json",
                 "statusSourceRef": "STATUS-SOURCE.json",
                 "observedAtField": "aicStatus.occurredAt",
                 "sourceDigestSha256": _sha256(raw["STATUS-SOURCE.json"])}},
            {"id": "LOWER-INPUT-07",
             "verificationStatus": "PARTIAL_LOWER_PROVIDED_JOINT_VALUE_PENDING",
             "fieldValues": {
                 "observableKpis": ["E2:UE.ServingCell", "E2:DRB.UEThpDl",
                                    "E2:RRU.PrbTotDl", "O1:RRU.PrbDl"],
                 "observableStatusFields": ["enforceStatus",
                                            "aicStatus.episodeState"],
                 "unsupportedFields": ["E2_CELL_SCOPE_RRU.PrbDl",
                                       "A1_OBJECTIVE_BALANCE_PRB_LOAD",
                                       "A1_POLICY_FOR_STYLE2_QOS"],
                 "upperRegistryMappingRef": "UNRESOLVED"}},
        ],
    }
    raw["LOWER-INTEGRATION-INPUTS.fragment.json"] = _dumps(inputs)
    (root / "LOWER-INTEGRATION-INPUTS.fragment.json").write_bytes(
        raw["LOWER-INTEGRATION-INPUTS.fragment.json"])

    release_manifest = {
        "schemaVersion": "oran-aic-lower-integration-release/1.0.0",
        "release": "oran-aic-lower-integration/1.0.0",
        "state": "LOWER_INTEGRATION_HANDOFF_READY",
        "standardDiffFileCount": 0,
        "officialObjectives": ["PIN_TO_CELL"],
        "source": {"commit": FROZEN_INTEGRATION_BINDING.commit,
                   "tree": FROZEN_INTEGRATION_BINDING.tree,
                   "archiveSha256": "0" * 64},
        "upper": {"release": FROZEN_INTEGRATION_BINDING.upper_release},
        "provider": {
            "release": FROZEN_INTEGRATION_BINDING.provider_release,
            "ociManifestDigest":
                FROZEN_INTEGRATION_BINDING.provider_oci_manifest_digest},
        "manifests": {
            "capability": {"path": "CAPABILITY-MANIFEST.json",
                           "sha256": _sha256(raw["CAPABILITY-MANIFEST.json"])},
            "contract": {"path": "CONTRACT-DIGESTS.json",
                         "sha256": _sha256(raw["CONTRACT-DIGESTS.json"])},
            "e2InventoryRelease": {
                "path": "E2-INVENTORY-RELEASE-MANIFEST.json",
                "sha256": _sha256(raw["E2-INVENTORY-RELEASE-MANIFEST.json"])},
        },
    }
    if mutate_release is not None:
        mutate_release(release_manifest)
    raw["RELEASE-MANIFEST.json"] = _dumps(release_manifest)
    (root / "RELEASE-MANIFEST.json").write_bytes(raw["RELEASE-MANIFEST.json"])

    checksums = "".join(f"{_sha256(payload)}  {name}\n"
                        for name, payload in sorted(raw.items()))
    checksums += f"{_sha256(verifier.read_bytes())}  {verifier.name}\n"
    (root / "SHA256SUMS").write_bytes(checksums.encode("utf-8"))

    identity = LowerReleaseIdentity(
        release="oran-aic-lower-integration/1.0.0",
        tag=FROZEN_INTEGRATION_BINDING.tag,
        commit=FROZEN_INTEGRATION_BINDING.commit,
        tree=FROZEN_INTEGRATION_BINDING.tree,
        handoff_archive_sha256="1" * 64,
        source_archive_sha256="0" * 64,
        release_manifest_sha256=_sha256(raw["RELEASE-MANIFEST.json"]),
        capability_manifest_sha256=_sha256(raw["CAPABILITY-MANIFEST.json"]),
        e2_inventory_release_manifest_sha256=_sha256(
            raw["E2-INVENTORY-RELEASE-MANIFEST.json"]),
        standalone_verifier_sha256=_sha256(verifier.read_bytes()),
        upper_release=FROZEN_INTEGRATION_BINDING.upper_release,
        provider_release=FROZEN_INTEGRATION_BINDING.provider_release,
        provider_oci_manifest_digest=(
            FROZEN_INTEGRATION_BINDING.provider_oci_manifest_digest),
        corrected_handoff_version="1.0.1",
        wire_policy_profile="oran-aic/1.0.0",
        official_objectives=("PIN_TO_CELL",),
    )
    (root / "RECEIPT-OK").write_bytes("\n".join([
        "receiptStatus: PASS",
        "receivedAt: 2026-08-18T21:31:03+09:00",
        f"release: {identity.release}",
        f"releaseCommit: {identity.commit}",
        f"releaseTree: {identity.tree}",
        f"handoffArchiveSha256: {identity.handoff_archive_sha256}",
        f"sourceArchiveSha256: {identity.source_archive_sha256}",
        f"releaseManifestSha256: {identity.release_manifest_sha256}",
        f"capabilityManifestSha256: {identity.capability_manifest_sha256}",
        "e2InventoryReleaseManifestSha256: "
        f"{identity.e2_inventory_release_manifest_sha256}",
        f"standaloneVerifierSha256: {identity.standalone_verifier_sha256}",
        "membershipDigestVerification: 44/44 PASS",
        "",
    ]).encode("utf-8"))
    return identity


class SyntheticReleaseFixture(unittest.TestCase):
    """One synthetic Lower release per test, so mutations cannot leak."""

    def build(self, *, mutate=None, mutate_release=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "oran-aic-lower-integration-1.0.0"
        identity = write_synthetic_release(root, mutate=mutate,
                                           mutate_release=mutate_release)
        return root, identity

    def load(self, *, mutate=None, mutate_release=None):
        root, identity = self.build(mutate=mutate, mutate_release=mutate_release)
        return LowerReleaseContracts.load(root, identity=identity)


class LowerReleaseBindingTests(SyntheticReleaseFixture):
    def test_a_verified_release_yields_the_composition_facts(self):
        lower = self.load()

        self.assertEqual(lower.official_objectives, ("PIN_TO_CELL",))
        self.assertEqual(lower.policy_type_id, "AIC_UECellSteering_1.0.0")
        self.assertEqual(lower.control_path[-2:],
                         ("E2SM_RC_1.03_STYLE3_ACTION1", "OAI_GNB"))
        self.assertIs(lower.official_a1_policy["ackIsEffectEvidence"], False)
        self.assertEqual(lower.official_a1_policy["effectEvidence"],
                         "POST_SEND_E2SM_KPM_SERVING_CELL_READBACK")

    def test_the_binding_record_carries_every_pinned_identity(self):
        lower = self.load()
        binding = lower.binding()

        for key in ("release", "commit", "tree", "capabilityManifestSha256",
                    "releaseManifestSha256", "upperRelease", "providerRelease",
                    "providerOciManifestDigest", "correctedHandoffVersion",
                    "wirePolicyProfile"):
            self.assertTrue(binding[key], f"{key} is missing from the binding")
        self.assertEqual(binding["controlPath"], list(lower.control_path))
        self.assertEqual(binding["claimBoundaries"]["seamlessUserPlaneContinuity"],
                         "NOT_VERIFIED")

    def test_an_absent_release_is_unavailable_not_empty(self):
        with self.assertRaises(LowerReleaseError) as raised:
            LowerReleaseContracts.load(Path(tempfile.gettempdir()) / "no-such-lower")
        self.assertIn("unavailable", str(raised.exception))

    def test_an_extraction_without_a_receipt_is_refused(self):
        root, identity = self.build()
        (root / "RECEIPT-OK").unlink()
        with self.assertRaises(LowerReleaseError) as raised:
            LowerReleaseContracts.load(root, identity=identity)
        self.assertIn("RECEIPT-OK", str(raised.exception))

    def test_a_changed_contract_byte_is_refused_by_name(self):
        root, identity = self.build()
        manifest = root / "CAPABILITY-MANIFEST.json"
        manifest.write_bytes(manifest.read_bytes().replace(b"PIN_TO_CELL",
                                                           b"PIN_TO_CELl"))
        with self.assertRaises(LowerReleaseError) as raised:
            LowerReleaseContracts.load(root, identity=identity)
        self.assertIn("CAPABILITY-MANIFEST.json", str(raised.exception))

    def test_a_different_release_identity_is_refused(self):
        root, identity = self.build()
        other = LowerReleaseIdentity(
            **{**identity.__dict__, "release": "oran-aic-lower-integration/9.9.9"})
        with self.assertRaises(LowerReleaseError) as raised:
            LowerReleaseContracts.load(root, identity=other)
        self.assertIn("release", str(raised.exception))

    def test_a_modified_frozen_standard_is_refused(self):
        def touched_standard(release_manifest):
            release_manifest["standardDiffFileCount"] = 1
        root, identity = self.build(mutate_release=touched_standard)
        with self.assertRaises(LowerReleaseError) as raised:
            LowerReleaseContracts.load(root, identity=identity)
        self.assertIn("standard", str(raised.exception))

    def test_a_release_built_against_another_upper_is_refused(self):
        def other_upper(release_manifest):
            release_manifest["upper"]["release"] = "upper-live-o1-harness/1.0.6"
        root, identity = self.build(mutate_release=other_upper)
        with self.assertRaises(LowerReleaseError) as raised:
            LowerReleaseContracts.load(root, identity=identity)
        self.assertIn("composed component", str(raised.exception))

    def test_a_release_advertising_a_second_objective_is_refused(self):
        def widened(release_manifest):
            release_manifest["officialObjectives"] = ["PIN_TO_CELL",
                                                      "BALANCE_PRB_LOAD"]
        root, identity = self.build(mutate_release=widened)
        with self.assertRaises(LowerReleaseError) as raised:
            LowerReleaseContracts.load(root, identity=identity)
        self.assertIn("PIN_TO_CELL", str(raised.exception))

    def test_lower_production_source_is_not_reachable_through_this_reader(self):
        # The allowlist is the mechanism, so assert the allowlist itself: no
        # archive, no record directory, no source path is in it.
        for name in CONTRACT_FILES:
            self.assertFalse(name.startswith(("artifacts/", "records/", "src/")),
                             f"{name} would expose Lower production material")
        lower = self.load()
        from oran.integration import lower_release as module
        with self.assertRaises(LowerReleaseError):
            module._read(Path(lower.root), "artifacts/lower-source.tar.gz")

    def test_a_deployment_declares_its_binding_beside_its_values(self):
        root, identity = self.build()
        with tempfile.TemporaryDirectory() as deployment:
            values = Path(deployment) / "integration-values.json"
            values.write_text("{}", encoding="utf-8")
            (Path(deployment) / BINDING_FILENAME).write_text(json.dumps({
                "schemaVersion": "oran-aic-upper-lower-release-binding/1.0.0",
                "lowerReleaseRoot": str(root),
                "release": identity.release}), encoding="utf-8")

            bound = resolve_binding(values, identity=identity)
            self.assertIsNotNone(bound)
            self.assertEqual(bound.official_objectives, ("PIN_TO_CELL",))

            # No declaration at all is "not bound", which is a fact, not an error.
            (Path(deployment) / BINDING_FILENAME).unlink()
            self.assertIsNone(resolve_binding(values, identity=identity))

    def test_a_declared_but_unreachable_binding_never_degrades_to_unbound(self):
        with tempfile.TemporaryDirectory() as deployment:
            values = Path(deployment) / "integration-values.json"
            values.write_text("{}", encoding="utf-8")
            (Path(deployment) / BINDING_FILENAME).write_text(json.dumps({
                "lowerReleaseRoot": str(Path(deployment) / "absent")}),
                encoding="utf-8")
            with self.assertRaises(LowerReleaseError):
                resolve_binding(values)

    def test_the_real_frozen_release_matches_the_pinned_binding(self):
        root = real_lower_release_root()
        if root is None:
            self.skipTest("no fresh-recipient extraction of the Lower release "
                          "is present on this machine")
        lower = LowerReleaseContracts.load(root)

        self.assertEqual(lower.identity, FROZEN_INTEGRATION_BINDING)
        self.assertEqual(lower.official_objectives, ("PIN_TO_CELL",))
        self.assertEqual(lower.control_path, (
            "R1", "NON_RT_RIC", "A1_P_V2", "A1P_PRODUCER", "XAPP_WORKER",
            "FLEXRIC", "E2AP_2.03", "E2SM_RC_1.03_STYLE3_ACTION1", "OAI_GNB"))
        observable = lower.observable()
        self.assertIn("A1_OBJECTIVE_BALANCE_PRB_LOAD",
                      observable["unsupportedFields"])
        self.assertIn("A1_POLICY_FOR_STYLE2_QOS", observable["unsupportedFields"])
        # per-UE E2 measurement and cell-scope O1 measurement stay distinct
        self.assertIn("E2:RRU.PrbTotDl", observable["observableKpis"])
        self.assertIn("O1:RRU.PrbDl", observable["observableKpis"])


class ObjectiveAdvertisementTests(SyntheticReleaseFixture):
    def capability(self):
        return json.loads((DEPLOYMENT / "capability.json")
                          .read_text(encoding="utf-8"))

    def test_a_bound_composition_advertises_pin_to_cell_alone(self):
        lower = self.load()
        composition = advertise(capability_manifest=self.capability(), lower=lower)

        self.assertEqual(composition.executable, ("PIN_TO_CELL",))
        self.assertEqual(EXECUTABLE_OBJECTIVES, ("PIN_TO_CELL",))
        self.assertTrue(composition.composed_release_bound)
        balance = next(item for item in composition.objectives
                       if item.kind == "BALANCE_PRB_LOAD")
        # The deployment's own capability manifest advertises it; the
        # composition still refuses it, and says why rather than hiding it.
        self.assertTrue(balance.provenance["advertisedByCapabilityManifest"])
        self.assertFalse(balance.executable)
        self.assertIn("PIN_TO_CELL", balance.reason)

    def test_the_experimental_qos_extension_is_never_an_objective(self):
        lower = self.load()
        composition = advertise(capability_manifest=self.capability(), lower=lower)

        self.assertEqual([item.name for item in composition.extensions],
                         ["E2SM_RC_STYLE2_ACTION6_QOS"])
        extension = composition.extensions[0]
        self.assertFalse(extension.executable)
        self.assertIsNone(extension.a1_policy_type)
        self.assertNotIn("E2SM_RC_STYLE2_ACTION6_QOS",
                         [item.kind for item in composition.objectives])
        self.assertNotIn("E2SM_RC_STYLE2_ACTION6_QOS", composition.executable)

    def test_submitting_the_qos_extension_is_refused_by_name(self):
        lower = self.load()
        for requested in ("E2SM_RC_STYLE2_ACTION6_QOS",
                          "E2SM-RC_1.03_STYLE2_ACTION6",
                          "slice_prb_policy_ratio"):
            with self.subTest(requested=requested):
                with self.assertRaises(ObjectiveNotExecutable) as raised:
                    assert_submittable(requested,
                                       capability_manifest=self.capability(),
                                       lower=lower)
                self.assertIn("no frozen A1 policy type", str(raised.exception))

    def test_a_release_that_bound_the_extension_to_a1_fails_the_composition(self):
        def bind_policy_type(documents):
            (documents["CAPABILITY-MANIFEST.json"]["experimentalExtensions"]
             ["E2SM_RC_STYLE2_ACTION6_QOS"]["a1PolicyType"]) = \
                "AIC_UECellSteering_1.0.0"
        lower = self.load(mutate=bind_policy_type)
        with self.assertRaises(LowerReleaseError) as raised:
            advertise(capability_manifest=self.capability(), lower=lower)
        self.assertIn("AIC_UECellSteering_1.0.0", str(raised.exception))

    def test_a_release_that_mixed_the_extension_in_fails_the_composition(self):
        def mix(documents):
            (documents["CAPABILITY-MANIFEST.json"]["experimentalExtensions"]
             ["E2SM_RC_STYLE2_ACTION6_QOS"]
             ["mixedIntoFrozenCellSteeringPolicy"]) = True
        lower = self.load(mutate=mix)
        with self.assertRaises(LowerReleaseError):
            advertise(capability_manifest=self.capability(), lower=lower)

    def test_an_objective_outside_the_frozen_policy_type_is_refused(self):
        lower = self.load()
        with self.assertRaises(ObjectiveNotExecutable):
            assert_submittable("MAXIMISE_ENERGY_EFFICIENCY",
                               capability_manifest=self.capability(), lower=lower)
        with self.assertRaises(ObjectiveNotExecutable):
            assert_submittable(None, capability_manifest=self.capability(),
                               lower=lower)

    def test_the_frozen_policy_schema_cannot_express_a_qos_objective(self):
        # Structural, not procedural: the steering objective kind is a closed
        # enumeration in the frozen schema, so no code path - LLM proposal,
        # negotiation, translation or dispatch - can produce a UE Cell Steering
        # policy carrying the Style 2 QoS axis.
        schema = json.loads(
            (_bundle_dir() / "AIC_UECellSteering_1.0.0.policy.schema.json")
            .read_text(encoding="utf-8"))
        kind = schema["$defs"]["SteeringObjective"]["properties"]["kind"]

        self.assertEqual(kind["enum"], ["BALANCE_PRB_LOAD", "PIN_TO_CELL"])
        serialised = json.dumps(schema)
        for token in ("slice_prb_policy_ratio", "STYLE2", "ACTION6", "QOS"):
            self.assertNotIn(token, serialised,
                             f"the frozen policy schema mentions {token}")



def _register_evidence_producer(profile):
    """Register the deployment's evidence production capability over R1.

    The DME type ``aic:policy-evidence:1.0.0`` is registered by the *producer*
    side, exactly as the conformance catalog's SC-089 does and from the same
    contract fixtures.  Without it the deployment has no data-access surface for
    a continuous job to bind to, which is a fact about the deployment rather
    than about the composition.
    """
    bundle = ContractBundle.discover(None)
    registration = {
        "dmeTypeDefinition": {
            "dmeTypeId": {"namespace": "aic", "name": "policy-evidence",
                          "version": "1.0.0"},
            "metadata": {"dataCategory": ["PERFORMANCE"]},
            "dataProductionSchema": bundle.fixture(
                "fixture://policyEvidenceFilterSchema"),
            "dataDeliverySchemas": [{
                "type": "JSON_SCHEMA",
                "deliverySchemaId": "aic.policy-evidence.record.schema.1.0.0",
                "schema": canonicalize(bundle.schema(
                    "aic.policy-evidence.1.0.0.schema.json")).decode("utf-8"),
            }],
            "dataDeliveryMechanisms": [{"dataDeliveryMethod": "PUSH_HTTP"}],
        },
        "dataAccessEndpoint": profile.vector["r1"]["dme"]["dataAccessEndpoint"],
        "dataDeliveryModes": ["CONTINUOUS"],
    }
    response = profile.nonrt.handle(
        "POST", "/data-registration/v2/production-capabilities",
        body=registration, headers={"Version": "2.0.0-alpha.2"})
    if response.status != 201:
        raise AssertionError(
            "the local deployment refused the evidence producer registration: "
            f"{response.status}")


def _deployment_listen_ports():
    """The loopback ports the local development deployment binds.

    Read from the deployment vector the integration-values document pins, so
    this stays correct if the profile's addresses ever move.
    """
    from urllib.parse import urlsplit

    values = json.loads(
        INTEGRATION_VALUES.read_text(encoding="utf-8"))["values"]
    vector = json.loads(
        (DEPLOYMENT / values["deployment.testVectorPath"])
        .read_text(encoding="utf-8"))
    uris = [vector["r1"]["apiRoot"], vector["a1"]["apiRoot"],
            vector["a1"]["statusCallbackRoot"],
            vector["r1"]["callbackApi"]["rootUri"],
            vector["r1"]["dme"]["policyEvidencePushBaseUri"],
            vector["o1"]["fileDataReporting"]["mnsRoot"],
            vector["o1"]["fileDataReporting"]["consumerReference"]]
    ports = set()
    for uri in uris:
        parsed = urlsplit(uri)
        if parsed.hostname and parsed.port:
            ports.add((parsed.hostname, parsed.port))
    return sorted(ports)


def _await_free_deployment_ports(timeout=30.0):
    """Block until nothing is listening on the deployment's ports.

    ``LocalMockProfile`` binds a fixed set of loopback listeners taken from the
    deployment vector, and this repository has more than one suite that drives
    it.  Constructing it while a previous instance is still shutting down fails
    half way through and leaves the listeners it *did* bind with no owner to
    close them, so the wait happens here rather than as a retry afterwards.
    """
    import socket
    import time

    deadline = time.monotonic() + timeout
    while True:
        busy = []
        for host, port in _deployment_listen_ports():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                if probe.connect_ex((host, port)) == 0:
                    busy.append(f"{host}:{port}")
        if not busy or time.monotonic() >= deadline:
            return busy
        time.sleep(0.25)


def _unload_local_profile():
    """Leave no local development profile resident for the rest of the run.

    ``tests/test_ubm_runtime_gates`` asserts, process-wide, that the bilateral
    mock runtime never has ``oran.profiles.local_mock`` loaded beside it.  That
    gate is about the *runtime's* import graph; a test-only deployment must not
    weaken it by staying in ``sys.modules`` after its own class has finished.
    The profile is therefore imported inside ``setUpClass`` and dropped here,
    the same discipline ``tests/gui/test_live_composition`` follows.
    """
    import sys

    sys.modules.pop("oran.profiles.local_mock", None)


class LiveCompositionRoundTripTests(unittest.TestCase):
    """One ``PIN_TO_CELL`` intent, all the way out and all the way back.

    Out:  natural language -> preserved coordinator -> rApp -> R1 -> Non-RT RIC
          -> A1-P v2 -> A1-P Producer -> deterministic xApp/E2 double.
    Back: E2SM-RC Style 3 / Action 1 control, KPM serving-cell readback, A1
          policy status, and an O1-derived assurance judgement.

    The stack is ``oran.profiles.local_mock``: real HTTP on loopback for R1 and
    A1, the repository's contract-faithful producer behind them.  Nothing here
    starts or addresses a radio.

    The deployment is built once for the class - binding and unbinding half a
    dozen loopback listeners per test invites port contention with the other
    suites that drive the same profile - and every test starts from a clean
    producer: no policies, a serving cell back at the source, and zeroed write
    counters.  Those counters are the test double's own bookkeeping, so
    resetting them is stating the precondition, not editing evidence.
    """

    @classmethod
    def setUpClass(cls):
        from oran.profiles.local_mock import LocalMockProfile

        cls._class_tmp = tempfile.TemporaryDirectory()
        root = Path(cls._class_tmp.name)
        cls.lower_root = root / "lower" / "oran-aic-lower-integration-1.0.0"
        cls.lower_identity = write_synthetic_release(cls.lower_root)
        cls.lower = LowerReleaseContracts.load(cls.lower_root,
                                               identity=cls.lower_identity)
        cls.capability = json.loads(
            (DEPLOYMENT / "capability.json").read_text(encoding="utf-8"))
        cls.golden = json.loads(
            (_bundle_dir() / "golden/golden-vectors.1.0.0.json")
            .read_text(encoding="utf-8"))["canonicalObjects"]
        cls.source_cell, cls.target_cell = [
            copy.deepcopy(cell["cellId"])
            for cell in cls.capability["topology"]["cells"]]
        busy = _await_free_deployment_ports()
        if busy:
            _unload_local_profile()
            cls._class_tmp.cleanup()
            raise unittest.SkipTest(
                "the local development deployment's ports are still held by "
                "another owner: " + ", ".join(busy))
        try:
            cls.profile = LocalMockProfile(INTEGRATION_VALUES, root / "profile")
            cls.profile.start()
            _register_evidence_producer(cls.profile)
            cls.profile.nearrt.harness_op({
                "op": "LOAD_CAPABILITY", "capability": cls.capability,
                "knownUeScopes": [cls.golden["policy"]["scope"]["ueId"]]})
            cls.integration = LiveIntegration.load(
                INTEGRATION_VALUES, state_dir=root / "integration",
                insecure_dev=True, endpoints_from_vector=True)
        except Exception:
            profile = getattr(cls, "profile", None)
            if profile is not None:
                profile.close()
            cls._class_tmp.cleanup()
            _unload_local_profile()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.profile.close()
        cls._class_tmp.cleanup()
        _unload_local_profile()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name)
        self._reset_deployment()
        self.addCleanup(self._reset_deployment)
        self._arm_kpm_arrival()

    def _reset_deployment(self):
        producer = self.profile.nearrt
        for policy_id in list(producer.policies):
            producer.delete_policy(policy_id)
        producer.quarantine.clear()
        producer.fences.clear()
        producer.e2.serving = copy.deepcopy(self.source_cell)
        producer.e2.normal_writes = 0
        producer.e2.rollback_writes = 0
        producer.e2.log.clear()
        producer.http_interactions.clear()

    # -- deployment behaviour, not composition behaviour --------------------- #

    def _arm_kpm_arrival(self):
        """Let one E2 KPM report reach the producer while the job is bound.

        In a deployment the Near-RT RIC receives KPM from E2 on its own; nothing
        upstream of A1 pushes it, and it does not arrive inside the A1 PUT that
        created the policy.  The rApp binds its continuous data job between
        creating the policy and reading its status, so that is where the report
        is delivered here: on the rApp's own thread, outside every request
        handler, exactly once per policy.

        The wrapper calls the real client method first and changes nothing about
        it.  What it models is the deployment, not the composition.
        """
        from unittest import mock
        from oran.rapp.r1_client import R1Client

        original = R1Client.create_continuous_job
        producer = self.profile.nearrt
        source, target = self.source_cell, self.target_cell
        delivered = set()

        def create_continuous_job(client, **kwargs):
            result = original(client, **kwargs)
            policy_id = kwargs.get("policy_id")
            if policy_id not in delivered:
                delivered.add(policy_id)
                observed = producer.e2.serving or source
                now = datetime.now(timezone.utc).replace(microsecond=0)
                producer.kpm_snapshot({
                    "fresh": False,
                    "observationWindowEnd": _z(now),
                    "servingCell": copy.deepcopy(observed),
                    "prbByCell": {str(source["cId"]["ncI"]): 80,
                                  str(target["cId"]["ncI"]): 20},
                })
            return result

        patcher = mock.patch.object(R1Client, "create_continuous_job",
                                    autospec=True,
                                    side_effect=create_continuous_job)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- request construction ------------------------------------------------ #

    def policy_context(self, *, objective="PIN_TO_CELL", intent_id=None,
                       correlation_id=None):
        policy = self.golden["policy"]
        now = datetime.now(timezone.utc).replace(microsecond=0)
        context = {
            "ueId": copy.deepcopy(policy["scope"]["ueId"]),
            "allowedCells": [copy.deepcopy(self.target_cell)],
            "forbiddenCells": [],
            "objectiveKind": objective,
            "minSecondsBetweenActuations": 30,
            "requiredKpiFreshnessMs": 60000,
            "actionDeadlineMs": 10000,
            "notBefore": _z(now - timedelta(minutes=1)),
            "expiresAt": _z(now + timedelta(hours=1)),
            "rollbackOn": list(policy["rollbackPolicy"]["on"]),
            "rollbackTimeoutMs": 10000,
            "intentRevision": 1,
            "policyRevision": 1,
            "correlationId": correlation_id or str(uuid.uuid4()),
            "producerId": policy["trace"]["producerId"],
            "intentId": intent_id or str(uuid.uuid4()),
        }
        if objective == "BALANCE_PRB_LOAD":
            context["allowedCells"] = [copy.deepcopy(self.source_cell),
                                       copy.deepcopy(self.target_cell)]
            context["improvementThresholdPrb"] = 10
        return context

    def submit(self, context, *, state_name):
        state = self.work / state_name
        state.mkdir(parents=True, exist_ok=True)
        request = {
            "statePath": str(state / "r1-state.json"),
            "evidencePath": str(state / "evidence.jsonl"),
            "intentText": INTENT_TEXT,
            "policyContext": copy.deepcopy(context),
            "evidenceRecords": [],
            "identifiers": {},
        }
        return run_once(
            integration_path=str(INTEGRATION_VALUES), request=request,
            composed_release_contracts=self.lower,
            llm_manager=LLMBackendManager.with_backend(
                DeterministicMockBackend(seed=7)),
            **self.integration.runner_kwargs())

    def round_trip(self, *, state_name="roundtrip"):
        """Run one PIN_TO_CELL episode and return its recorded trial."""
        result = self.submit(self.policy_context(), state_name=state_name)
        cycle = result["cycles"][0]
        trial = cycle.get("profile_trial")
        self.assertIsNotNone(trial, cycle.get("profile_error"))
        return result, trial

    # -- the round trip ------------------------------------------------------ #

    def test_one_intent_reaches_the_producer_and_returns_applied_verified(self):
        from unittest import mock
        from oran.rapp.coordinator_adapter import RAppCoordinatorAdapter

        original = RAppCoordinatorAdapter.process_intent
        passes = []

        def counted(adapter_self, *args, **kwargs):
            passes.append(1)
            return original(adapter_self, *args, **kwargs)

        with mock.patch.object(RAppCoordinatorAdapter, "process_intent",
                               autospec=True, side_effect=counted):
            result, trial = self.round_trip()

        # The preserved coordinator was entered exactly once for one intent.
        self.assertEqual(len(passes), 1)
        self.assertEqual(len(result["cycles"]), 1)

        # Out: a frozen-policy-type A1-P v2 policy carrying PIN_TO_CELL.
        policy = trial["policy"]
        self.assertEqual(policy["steeringObjective"]["kind"], "PIN_TO_CELL")
        self.assertEqual(policy["steeringObjective"]["actionEnvelope"]
                         ["allowedCells"], [self.target_cell])
        self.assertNotIn("improvementThresholdPrb", policy["steeringObjective"])
        self.assertTrue(trial["policyId"])

        # Back: the producer's own episode, driven by a KPM readback.
        status = trial["policy_status"]
        aic = status["aicStatus"]
        self.assertEqual(status["enforceStatus"], "ENFORCED")
        self.assertEqual(aic["episodeState"], "APPLIED_VERIFIED")
        self.assertIs(aic["episodeTerminal"], True)
        self.assertEqual(aic["readback"]["result"], "VERIFIED")
        self.assertEqual(aic["readback"]["observedServingCell"],
                         aic["selectedCell"])
        self.assertEqual(aic["selectedCell"], self.target_cell)
        self.assertEqual(aic["policyId"], trial["policyId"])
        self.assertEqual(aic["trace"]["intentId"], policy["trace"]["intentId"])

        # Exactly one normal RAN write, no rollback write.
        self.assertEqual(self.profile.nearrt.e2.normal_writes, 1)
        self.assertEqual(self.profile.nearrt.e2.rollback_writes, 0)
        control = self.profile.nearrt.e2.log[-1]
        self.assertEqual(control["kind"], "normal")
        self.assertEqual(control["target"], self.target_cell)

    def test_an_ack_alone_never_satisfies_the_assurance_gate(self):
        _, trial = self.round_trip()
        status = trial["policy_status"]
        _, window_end = self._window(trial)

        # APPLIED_VERIFIED, a delivered control and a verified readback are all
        # present - and without correlated O1 evidence the verdict is still not
        # SATISFIED.
        self.assertIs(
            CombinedAssurance(self.capability).assess(
                trial["policy"], trial["policyId"], status, [],
                now=window_end + timedelta(seconds=1)),
            AssuranceDecision.UNKNOWN)

    def test_correlated_o1_evidence_completes_the_assurance_round_trip(self):
        _, trial = self.round_trip()
        status = trial["policy_status"]
        window_start, window_end = self._window(trial)
        record = self._evidence(trial, window_start, window_end)

        self.assertIs(
            CombinedAssurance(self.capability).assess(
                trial["policy"], trial["policyId"], status, [record],
                now=window_end + timedelta(seconds=1)),
            AssuranceDecision.SATISFIED)

    def test_stale_or_uncorrelated_evidence_fails_closed(self):
        _, trial = self.round_trip()
        assurance = CombinedAssurance(self.capability)
        status = trial["policy_status"]
        window_start, window_end = self._window(trial)
        now = window_end + timedelta(seconds=1)

        def evidence():
            return self._evidence(trial, window_start, window_end)

        cases = {"stale window": (evidence(), now + timedelta(seconds=600))}
        wrong_episode = evidence()
        wrong_episode["correlation"]["episodeId"] = str(uuid.uuid4())
        cases["uncorrelated episode"] = (wrong_episode, now)
        missing_sample = evidence()
        missing_sample["samples"] = [
            sample for sample in missing_sample["samples"]
            if sample["name"] != "RRU.PrbDl"]
        cases["missing required measurement"] = (missing_sample, now)
        suspect = evidence()
        suspect["quality"] = "SUSPECT"
        cases["suspect quality"] = (suspect, now)

        for label, (record, at) in cases.items():
            with self.subTest(case=label):
                self.assertIs(
                    assurance.assess(trial["policy"], trial["policyId"], status,
                                     [record], now=at),
                    AssuranceDecision.UNKNOWN)

    def test_resubmitting_the_same_intent_creates_no_second_policy_or_write(self):
        context = self.policy_context()

        first = self.submit(context, state_name="idem-1")
        first_trial = first["cycles"][0]["profile_trial"]
        writes_after_first = self.profile.nearrt.e2.normal_writes
        puts_after_first = len(self.profile.nearrt.http_interactions)

        second = self.submit(context, state_name="idem-2")
        second_trial = second["cycles"][0]["profile_trial"]

        # Same intent identity and revision -> same idempotency key -> the same
        # policy resource, no second A1-P PUT and no second RAN write.
        self.assertEqual(first_trial["policy"], second_trial["policy"])
        self.assertEqual(first_trial["policyId"], second_trial["policyId"])
        self.assertEqual(writes_after_first, 1)
        self.assertEqual(puts_after_first, 1)
        self.assertEqual(self.profile.nearrt.e2.normal_writes,
                         writes_after_first)
        self.assertEqual(len(self.profile.nearrt.http_interactions),
                         puts_after_first)
        self.assertEqual(len(self.profile.nearrt.policies), 1)

    def test_an_unadvertised_objective_is_refused_before_any_transport(self):
        with self.assertRaises(ObjectiveNotExecutable) as raised:
            self.submit(self.policy_context(objective="BALANCE_PRB_LOAD"),
                        state_name="refused")

        self.assertIn("PIN_TO_CELL", str(raised.exception))
        self.assertEqual(self.profile.nearrt.e2.normal_writes, 0)
        self.assertEqual(self.profile.nearrt.http_interactions, [])
        self.assertEqual(self.profile.nearrt.policies, {})

    def test_the_experimental_qos_axis_cannot_be_submitted_from_the_entry(self):
        with self.assertRaises(ObjectiveNotExecutable) as raised:
            self.submit(
                self.policy_context(objective="E2SM_RC_STYLE2_ACTION6_QOS"),
                state_name="qos-refused")
        self.assertIn("no frozen A1 policy type", str(raised.exception))
        self.assertEqual(self.profile.nearrt.e2.normal_writes, 0)
        self.assertEqual(self.profile.nearrt.policies, {})

    def test_an_unavailable_lower_release_fails_the_composition_closed(self):
        state = self.work / "unavailable"
        state.mkdir(parents=True, exist_ok=True)
        request = {
            "statePath": str(state / "r1-state.json"),
            "evidencePath": str(state / "evidence.jsonl"),
            "intentText": INTENT_TEXT,
            "policyContext": self.policy_context(),
            "evidenceRecords": [], "identifiers": {},
        }
        with self.assertRaises(LowerReleaseError) as raised:
            run_once(integration_path=str(INTEGRATION_VALUES), request=request,
                     composed_release_root=str(self.work / "no-lower-here"),
                     llm_manager=LLMBackendManager.with_backend(
                         DeterministicMockBackend(seed=7)),
                     **self.integration.runner_kwargs())
        self.assertIn("unavailable", str(raised.exception))
        self.assertEqual(self.profile.nearrt.policies, {})

    # -- helpers ------------------------------------------------------------- #

    def _window(self, trial):
        """The AFTER observation window for one completed episode.

        It starts on the first whole second *after* the action was reported.
        The contract requires the action to fall outside the AFTER window, and
        the producer stamps ``occurredAt`` with millisecond precision, so a
        window that merely truncated that timestamp would place the action
        inside its own evidence window.
        """
        occurred = _parse_z(trial["policy_status"]["aicStatus"]["occurredAt"])
        start = occurred.replace(microsecond=0) + timedelta(seconds=1)
        return start, start + timedelta(seconds=60)

    def _evidence(self, trial, window_start, window_end):
        """One O1-derived evidence record correlated to *this* episode.

        Shape and provenance come from the contract's golden record; only the
        correlation and the observation window are bound to the episode that
        actually ran, because those are the two things a real DME push would
        carry from the run rather than from a fixture.
        """
        record = copy.deepcopy(self.golden["afterEvidence"])
        aic = trial["policy_status"]["aicStatus"]
        record["window"] = {"start": _z(window_start), "end": _z(window_end)}
        record["observedAt"] = _z(window_end)
        record["observationId"] = str(uuid.uuid4())
        record["policyScope"]["ueId"] = copy.deepcopy(
            trial["policy"]["scope"]["ueId"])
        record["measurementScope"]["cellId"] = copy.deepcopy(self.target_cell)
        record["measurementScope"]["managedObjectDn"] = next(
            cell["managedObjectDn"] for cell in self.capability["topology"]["cells"]
            if cell["cellId"]["cId"]["ncI"] == self.target_cell["cId"]["ncI"])
        record["source"]["profileId"] = self.capability["o1PerformanceProfileId"]
        control = aic.get("control") or {}
        record["correlation"] = {
            "policyTypeId": "AIC_UECellSteering_1.0.0",
            "policyId": trial["policyId"],
            "policyRevision": trial["policy"]["trace"]["policyRevision"],
            "episodeId": aic["episodeId"],
            "transactionId": control.get("transactionId"),
            "actionId": control.get("actionId"),
        }
        return record


def _parse_z(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")



class UnboundDevelopmentCompositionTests(unittest.TestCase):
    """A deployment with no Lower release bound must say so, not pretend."""

    def capability(self):
        return json.loads((DEPLOYMENT / "capability.json")
                          .read_text(encoding="utf-8"))

    def test_an_unbound_composition_is_labelled_and_is_not_final(self):
        composition = advertise(capability_manifest=self.capability())

        self.assertFalse(composition.composed_release_bound)
        self.assertFalse(composition.final)
        self.assertEqual(composition.basis,
                         "DEVELOPMENT_DEPLOYMENT_NOT_COMPOSED_RELEASE_BOUND")
        # It is limited by its own capability manifest, and by nothing it made
        # up: the narrowing to PIN_TO_CELL is what *binding* the Lower release
        # does, and this deployment has not done that.
        self.assertEqual(set(composition.executable),
                         set(self.capability()["objectives"]))
        self.assertEqual(composition.extensions, ())

    def test_binding_the_lower_release_is_what_narrows_the_composition(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "oran-aic-lower-integration-1.0.0"
        identity = write_synthetic_release(root)
        lower = LowerReleaseContracts.load(root, identity=identity)

        unbound = advertise(capability_manifest=self.capability())
        bound = advertise(capability_manifest=self.capability(), lower=lower)

        self.assertIn("BALANCE_PRB_LOAD", unbound.executable)
        self.assertEqual(bound.executable, ("PIN_TO_CELL",))
        self.assertTrue(bound.final)
        self.assertTrue(bound.basis.startswith("COMPOSED_RELEASE_BOUND:"))

    def test_an_objective_the_capability_omits_is_refused_either_way(self):
        capability = self.capability()
        capability["objectives"] = ["PIN_TO_CELL"]
        with self.assertRaises(ObjectiveNotExecutable) as raised:
            assert_submittable("BALANCE_PRB_LOAD", capability_manifest=capability)
        self.assertIn("capability manifest", str(raised.exception))


class CompositionRefusalTests(unittest.TestCase):
    """Refusals that must happen before a deployment is ever contacted."""

    def test_the_translator_cannot_be_asked_for_a_qos_objective(self):
        from oran.rapp.policy_translator import (AdmissionRejected,
                                                 PolicyTranslationContext,
                                                 translate_intent)
        from decision.intent_model import (ConstraintType, Intent,
                                           IntentPriority, IntentScope,
                                           IntentTarget, IntentType)

        golden = json.loads(
            (_bundle_dir() / "golden/golden-vectors.1.0.0.json")
            .read_text(encoding="utf-8"))["canonicalObjects"]
        policy = golden["policy"]
        intent = Intent(
            id=policy["trace"]["intentId"], type=IntentType.THROUGHPUT_GOAL,
            target=IntentTarget("throughput", ConstraintType.MIN, 1.0,
                                unit="Mbps"),
            scope=IntentScope(ue_ids=["ue-fixture"]),
            priority=IntentPriority.HIGH)
        context = PolicyTranslationContext(
            ue_id=policy["scope"]["ueId"],
            allowed_cells=policy["steeringObjective"]["actionEnvelope"]
            ["allowedCells"][:1],
            forbidden_cells=[], objective_kind="E2SM_RC_STYLE2_ACTION6_QOS",
            improvement_threshold_prb=None, min_seconds_between_actuations=30,
            required_kpi_freshness_ms=3000, action_deadline_ms=10000,
            not_before=policy["validity"]["notBefore"],
            expires_at=policy["validity"]["expiresAt"],
            rollback_on=policy["rollbackPolicy"]["on"],
            rollback_timeout_ms=10000, intent_revision=1, policy_revision=1,
            correlation_id=policy["trace"]["correlationId"],
            producer_id=policy["trace"]["producerId"])

        # Even bypassing the composition entry entirely, the translator has no
        # branch that could produce a policy for the experimental axis.
        with self.assertRaises(AdmissionRejected) as raised:
            translate_intent(intent, policy_type_discovery={},
                             capability_manifest=golden["capabilityManifest"],
                             context=context)
        self.assertIn("unsupported objective", str(raised.exception))

    def test_a_console_refuses_to_bind_a_deployment_whose_lower_is_missing(self):
        import shutil

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        deployment = Path(tmp.name) / "deployment"
        shutil.copytree(DEPLOYMENT, deployment,
                        ignore=shutil.ignore_patterns("state", "artifacts",
                                                      "traceability-state"))
        values = deployment / "integration-values.json"

        # Without a declaration the deployment binds and is honestly labelled
        # as not being the final composition.
        unbound = LiveIntegration.load(values, state_dir=Path(tmp.name) / "s1",
                                       insecure_dev=True,
                                       endpoints_from_vector=True)
        self.assertIsNone(unbound.lower)
        self.assertFalse(unbound.advertised_objectives().final)
        self.assertEqual(unbound.identity()["compositionBasis"],
                         "DEVELOPMENT_DEPLOYMENT_NOT_COMPOSED_RELEASE_BOUND")

        # With a declaration that cannot be honoured it refuses to come up at
        # all, rather than coming up as a development deployment.
        (deployment / BINDING_FILENAME).write_text(json.dumps({
            "schemaVersion": "oran-aic-upper-lower-release-binding/1.0.0",
            "lowerReleaseRoot": str(deployment / "absent-lower")}),
            encoding="utf-8")
        with self.assertRaises(IntegrationError) as raised:
            LiveIntegration.load(values, state_dir=Path(tmp.name) / "s2",
                                 insecure_dev=True, endpoints_from_vector=True)
        self.assertIn("Lower release", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
