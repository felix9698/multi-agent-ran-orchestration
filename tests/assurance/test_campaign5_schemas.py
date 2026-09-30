"""The four Campaign 5 policy types: schema shape, JCS pins, discovery gate.

The status schema is copied verbatim from ``AIC_UECellSteering_1.0.0.status``,
so these tests assert the load-bearing invariants survive the copy: a control ACK
is structurally not effect evidence, the readback result vocabulary is exact, and
the observed quantity is present only for VERIFIED/MISMATCH.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from oran.contract.jcs import jcs_sha256
from oran.campaign5.families import (
    CAMPAIGN5_FAMILIES,
    CAMPAIGN5_POLICY_TYPES,
    Campaign5Error,
    campaign5_capability_manifest,
    family_by_policy_type,
    load_campaign5_schema,
    local_schema_digest,
    verify_campaign5_discovery,
)

REPO = Path(__file__).resolve().parents[2]
DIR = REPO / "contracts" / "oran-aic" / "campaign5"


def _well_formed_discovery():
    manifest = campaign5_capability_manifest()
    discovery = {}
    for fam in CAMPAIGN5_FAMILIES.values():
        discovery[fam.policy_type_id] = {
            "policySchema": load_campaign5_schema(f"{fam.policy_type_id}.policy"),
            "statusSchema": load_campaign5_schema(f"{fam.policy_type_id}.status"),
            "ranFunctionDefinition": {
                "ricStyleType": fam.rc_style,
                "ricControlActionId": fam.rc_action_id,
                "ranParameterIds": list(fam.rc_param_ids),
            },
        }
    return manifest, discovery


class SchemasAreValidAndPinned(unittest.TestCase):
    def test_every_type_has_a_policy_and_status_schema_on_disk(self):
        for type_id in CAMPAIGN5_POLICY_TYPES:
            for kind in ("policy", "status"):
                path = DIR / f"{type_id}.{kind}.schema.json"
                with self.subTest(schema=path.name):
                    self.assertTrue(path.is_file())
                    data = json.loads(path.read_text(encoding="utf-8"))
                    Draft202012Validator.check_schema(data)

    def test_pinned_digests_equal_the_on_disk_jcs_digests(self):
        for fam in CAMPAIGN5_FAMILIES.values():
            with self.subTest(family=fam.key):
                self.assertEqual(
                    fam.policy_digest, local_schema_digest(f"{fam.policy_type_id}.policy")
                )
                self.assertEqual(
                    fam.status_digest, local_schema_digest(f"{fam.policy_type_id}.status")
                )

    def test_status_schema_states_that_a_control_ack_is_not_effect_evidence(self):
        for fam in CAMPAIGN5_FAMILIES.values():
            status = load_campaign5_schema(f"{fam.policy_type_id}.status")
            defs = status["$defs"]
            with self.subTest(family=fam.key):
                self.assertEqual(
                    defs["Control"]["properties"]["resultIsEffectEvidence"],
                    {"const": False},
                )
                self.assertEqual(
                    defs["Readback"]["properties"]["result"]["enum"],
                    ["VERIFIED", "MISMATCH", "MISSING", "STALE", "NOT_AVAILABLE"],
                )

    def test_observed_quantity_is_present_only_for_verified_or_mismatch(self):
        for fam in CAMPAIGN5_FAMILIES.values():
            status = load_campaign5_schema(f"{fam.policy_type_id}.status")
            branch = status["$defs"]["Readback"]["allOf"][0]
            with self.subTest(family=fam.key):
                self.assertEqual(branch["if"]["properties"]["result"]["enum"],
                                 ["VERIFIED", "MISMATCH"])
                self.assertEqual(branch["then"]["required"], [fam.observed_key])
                self.assertEqual(branch["else"]["not"]["required"], [fam.observed_key])
                # The observed quantity was swapped away from the steering CellId.
                self.assertNotIn("observedServingCell", json.dumps(status))
                self.assertNotIn("CellId", json.dumps(status))

    def test_policy_body_carries_exactly_the_designed_config_leaves(self):
        expected = {
            "AIC_UeDlPrbCap_1.0.0": {"cellId", "ueId", "maxDlPrbs"},
            "AIC_SchedulerPriority_1.0.0": {"cellId", "ueId", "pfWeight"},
            "AIC_DlMcsBounds_1.0.0": {"cellId", "minDlMcs", "maxDlMcs"},
            "AIC_CellDlTxPower_1.0.0": {"cellId", "gnbId", "txAttenuationDb"},
        }
        for type_id, leaves in expected.items():
            policy = load_campaign5_schema(f"{type_id}.policy")
            config = policy["$defs"]["Config"]
            with self.subTest(type_id=type_id):
                self.assertEqual(set(config["properties"]), leaves)
                self.assertEqual(set(config["required"]), leaves)
                self.assertFalse(config["additionalProperties"])

    def test_real_style2_leaves_accept_fractional_values(self):
        for type_id, leaf in (
            ("AIC_SchedulerPriority_1.0.0", "pfWeight"),
            ("AIC_CellDlTxPower_1.0.0", "txAttenuationDb"),
        ):
            policy = load_campaign5_schema(f"{type_id}.policy")
            observed = load_campaign5_schema(f"{type_id}.status")
            with self.subTest(type_id=type_id):
                self.assertEqual(policy["$defs"]["Config"]["properties"][leaf]["type"], "number")
                self.assertEqual(observed["$defs"]["ObservedValue"]["properties"][leaf]["type"], "number")


class CapabilityManifestAndDiscoveryGate(unittest.TestCase):
    def test_manifest_advertises_all_four_types_with_per_type_digests(self):
        manifest = campaign5_capability_manifest()
        self.assertEqual(set(manifest["policyTypes"]), set(CAMPAIGN5_POLICY_TYPES))
        for fam in CAMPAIGN5_FAMILIES.values():
            digests = manifest["schemaDigests"][fam.policy_type_id]
            with self.subTest(family=fam.key):
                self.assertEqual(digests["policy"], fam.policy_digest)
                self.assertEqual(digests["status"], fam.status_digest)

    def test_discovery_gate_accepts_agreeing_three_way_digests(self):
        manifest, discovery = _well_formed_discovery()
        for fam in CAMPAIGN5_FAMILIES.values():
            with self.subTest(family=fam.key):
                verify_campaign5_discovery(discovery, manifest, fam.policy_type_id)

    def test_discovery_gate_refuses_a_digest_disagreement(self):
        manifest, discovery = _well_formed_discovery()
        # Perturb the R1-advertised schema so its digest no longer matches.
        discovery["AIC_UeDlPrbCap_1.0.0"]["policySchema"]["title"] = "tampered"
        with self.assertRaises(Campaign5Error):
            verify_campaign5_discovery(discovery, manifest, "AIC_UeDlPrbCap_1.0.0")

    def test_discovery_gate_refuses_a_bare_action_number_without_definition(self):
        manifest, discovery = _well_formed_discovery()
        del discovery["AIC_DlMcsBounds_1.0.0"]["ranFunctionDefinition"]
        with self.assertRaisesRegex(Campaign5Error, "capability gate"):
            verify_campaign5_discovery(discovery, manifest, "AIC_DlMcsBounds_1.0.0")

    def test_discovery_gate_refuses_a_definition_with_the_wrong_action_id(self):
        manifest, discovery = _well_formed_discovery()
        discovery["AIC_DlMcsBounds_1.0.0"]["ranFunctionDefinition"]["ricControlActionId"] = 999
        with self.assertRaises(Campaign5Error):
            verify_campaign5_discovery(discovery, manifest, "AIC_DlMcsBounds_1.0.0")

    def test_discovery_gate_refuses_a_manifest_definition_disagreement(self):
        manifest, discovery = _well_formed_discovery()
        manifest["ranFunctionDefinitions"]["AIC_DlMcsBounds_1.0.0"]["ricControlActionId"] = 999
        with self.assertRaises(Campaign5Error):
            verify_campaign5_discovery(discovery, manifest, "AIC_DlMcsBounds_1.0.0")

    def test_family_lookup_rejects_a_foreign_type(self):
        with self.assertRaises(Campaign5Error):
            family_by_policy_type("AIC_UECellSteering_1.0.0")


if __name__ == "__main__":
    unittest.main()
