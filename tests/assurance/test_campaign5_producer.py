"""In-repo A1-P v2 producer for the four Campaign 5 policy types."""

from __future__ import annotations

import unittest

from oran.contract.jcs import jcs_sha256
from oran.campaign5.families import CAMPAIGN5_FAMILIES, campaign5_capability_manifest
from oran.campaign5.producer import (
    A1Conflict,
    A1NotFound,
    A1ValidationError,
    Campaign5PolicyProducer,
)

VALIDITY = {"notBefore": "2026-09-02T00:00:00Z", "notAfter": "2026-09-03T00:00:00Z"}


def policy(config, *, revision=1, fence=1):
    return {"config": dict(config), "validity": dict(VALIDITY),
            "trace": {"traceId": "tx-1", "revision": revision, "fencingToken": fence}}


CAP = "AIC_UeDlPrbCap_1.0.0"
CAP_CONFIG = {"cellId": "cell-1", "ueId": "ue-1", "maxDlPrbs": 12}


class Discovery(unittest.TestCase):
    def test_advertises_all_four_types_and_serves_schemas_and_digests(self):
        p = Campaign5PolicyProducer()
        self.assertEqual(set(p.get_policytypes()),
                         {f.policy_type_id for f in CAMPAIGN5_FAMILIES.values()})
        meta = p.get_policytype(CAP)
        self.assertEqual(set(meta), {"policySchema", "statusSchema", "ranFunctionDefinition"})
        self.assertEqual(p.schema_digests(CAP), {
            "policySchemaJcsSha256": jcs_sha256(meta["policySchema"]),
            "statusSchemaJcsSha256": jcs_sha256(meta["statusSchema"]),
        })

    def test_producer_manifest_matches_the_pinned_local_manifest(self):
        p = Campaign5PolicyProducer()
        local = campaign5_capability_manifest()
        self.assertEqual(p.capability_manifest()["schemaDigests"], local["schemaDigests"])

    def test_capability_gate_hides_a_type_whose_definition_is_not_discovered(self):
        defs = dict(campaign5_capability_manifest()["ranFunctionDefinitions"])
        del defs["AIC_CellDlTxPower_1.0.0"]
        p = Campaign5PolicyProducer(discovered_definitions=defs)
        self.assertNotIn("AIC_CellDlTxPower_1.0.0", p.get_policytypes())
        with self.assertRaisesRegex(Exception, "not advertised"):
            p.put_policy("AIC_CellDlTxPower_1.0.0", "x",
                         policy({"cellId": "c", "gnbId": "g", "txAttenuationDb": 3}))

    def test_capability_gate_rejects_a_wrong_definition_shape(self):
        defs = dict(campaign5_capability_manifest()["ranFunctionDefinitions"])
        defs["AIC_UeDlPrbCap_1.0.0"] = {**defs["AIC_UeDlPrbCap_1.0.0"], "ricControlActionId": 7}
        p = Campaign5PolicyProducer(discovered_definitions=defs)
        self.assertNotIn(CAP, p.get_policytypes())


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.p = Campaign5PolicyProducer()

    def test_put_get_status_round_trip_starts_pending_not_enforced(self):
        self.assertEqual(self.p.put_policy(CAP, "p1", policy(CAP_CONFIG)).http_status, 201)
        self.assertEqual(self.p.get_policy(CAP, "p1")["config"], CAP_CONFIG)
        status = self.p.get_status(CAP, "p1")
        self.assertEqual(status["enforceStatus"], "NOT_ENFORCED")

    def test_identical_put_is_idempotent_but_scope_move_is_a_conflict(self):
        self.assertEqual(self.p.put_policy(CAP, "p1", policy(CAP_CONFIG)).http_status, 201)
        self.assertEqual(self.p.put_policy(CAP, "p1", policy(CAP_CONFIG)).http_status, 200)
        moved = policy({"cellId": "cell-2", "ueId": "ue-1", "maxDlPrbs": 12}, revision=2, fence=2)
        with self.assertRaisesRegex(A1Conflict, "scope"):
            self.p.put_policy(CAP, "p1", moved)

    def test_one_active_owner_per_scope(self):
        self.p.put_policy(CAP, "p1", policy(CAP_CONFIG))
        with self.assertRaisesRegex(A1Conflict, "already owned"):
            self.p.put_policy(CAP, "p2", policy(CAP_CONFIG))

    def test_update_requires_newer_revision_and_fence(self):
        self.p.put_policy(CAP, "p1", policy(CAP_CONFIG))
        with self.assertRaisesRegex(A1Conflict, "newer revision"):
            self.p.put_policy(CAP, "p1", policy({**CAP_CONFIG, "maxDlPrbs": 8}))
        ok = self.p.put_policy(CAP, "p1", policy({**CAP_CONFIG, "maxDlPrbs": 8}, revision=2, fence=2))
        self.assertEqual(ok.http_status, 200)

    def test_zero_first_fence_is_valid_but_stays_strictly_monotonic(self):
        self.assertEqual(
            self.p.put_policy(
                CAP, "p1", policy(CAP_CONFIG, revision=1, fence=0)
            ).http_status,
            201,
        )
        self.assertEqual(
            self.p.put_policy(
                CAP, "p1",
                policy({**CAP_CONFIG, "maxDlPrbs": 8}, revision=2, fence=1),
            ).http_status,
            200,
        )
        with self.assertRaisesRegex(A1Conflict, "newer revision"):
            self.p.put_policy(
                CAP, "p1",
                policy({**CAP_CONFIG, "maxDlPrbs": 6}, revision=3, fence=0),
            )

    def test_ack_alone_is_not_effect_evidence(self):
        self.p.put_policy(CAP, "p1", policy(CAP_CONFIG))
        # gNB ACKed the control but no corroborated configuration readback exists.
        self.p.record_applied(CAP, "p1", control_ack=True, observed_config=None)
        status = self.p.get_status(CAP, "p1")
        self.assertEqual(status["enforceStatus"], "NOT_ENFORCED")
        self.assertEqual(status["aicStatus"]["episodeState"], "APPLIED_UNVERIFIED")
        self.assertEqual(status["aicStatus"]["readback"]["result"], "NOT_AVAILABLE")
        self.assertFalse(status["aicStatus"]["control"]["resultIsEffectEvidence"])

    def test_corroborated_readback_promotes_to_applied_verified(self):
        self.p.put_policy(CAP, "p1", policy(CAP_CONFIG))
        self.p.record_applied(CAP, "p1", control_ack=True, observed_config=CAP_CONFIG)
        status = self.p.get_status(CAP, "p1")
        self.assertEqual(status["enforceStatus"], "ENFORCED")
        self.assertEqual(status["aicStatus"]["episodeState"], "APPLIED_VERIFIED")
        self.assertEqual(status["aicStatus"]["readback"]["result"], "VERIFIED")

    def test_control_failure_is_apply_failed(self):
        self.p.put_policy(CAP, "p1", policy(CAP_CONFIG))
        self.p.record_applied(CAP, "p1", control_ack=False)
        status = self.p.get_status(CAP, "p1")
        self.assertEqual(status["aicStatus"]["episodeState"], "APPLY_FAILED")
        self.assertEqual(status["enforceStatus"], "NOT_ENFORCED")

    def test_schema_rejects_a_bad_config_and_ratio_reversal(self):
        with self.assertRaises(A1ValidationError):
            self.p.put_policy(CAP, "p1", policy({"cellId": "c", "ueId": "u", "maxDlPrbs": 999}))
        with self.assertRaisesRegex(A1ValidationError, "minDlMcs"):
            self.p.put_policy("AIC_DlMcsBounds_1.0.0", "m1",
                              policy({"cellId": "c", "minDlMcs": 20, "maxDlMcs": 4}))

    def test_a1_p_v2_routes_expose_discovery_put_status(self):
        self.assertEqual(self.p.handle("GET", "/A1-P/v2/policytypes").status, 200)
        base = f"/A1-P/v2/policytypes/{CAP}/policies/p1"
        self.assertEqual(self.p.handle("PUT", base, policy(CAP_CONFIG)).status, 201)
        self.assertEqual(self.p.handle("GET", base + "/status").status, 200)
        # Delete needs a bound rollback worker, like the slice producer.
        self.assertEqual(self.p.handle("DELETE", base).status, 409)


if __name__ == "__main__":
    unittest.main()
