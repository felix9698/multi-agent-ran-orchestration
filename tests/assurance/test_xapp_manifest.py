"""XApp Capability Manifest: ownership boundary, honesty, round trip."""
import unittest

from assurance.contracts.capability import ActuatorDeploymentState
from assurance.objectives import record_for
from assurance.xapps import (
    ACTION_FAMILY_BY_ID, XAPP_KIND_OWNED_ACTIONS, XAppCapabilityManifest,
    XAppExecutionPathState, XAppKind, XAppManifestError,
    default_capability_manifests, export_manifest_json, parse_manifest_json,
    validate_xapp_manifest,
)
from assurance.xapps.manifest import XAppActionBinding

from tests.assurance.xapp_support import deployment


def _binding(action_id: str) -> XAppActionBinding:
    return XAppActionBinding(
        action_id=action_id,
        action_family=ACTION_FAMILY_BY_ID[action_id],
        service_model={"serviceModel": "E2SM-RC", "version": "1.03",
                       "style": "2", "action": "definition-dependent",
                       "operation": "test"},
        parameters=({"name": "p", "type": "integer", "scope": "UE",
                     "unit": "1"},),
        readback_measurement_ref=f"measurement/action-space/{action_id}/readback",
        rollback_supported=True,
        deployment_state=ActuatorDeploymentState.HARDWARE_FREE_VERIFIED,
    )


def _manifest(kind: XAppKind, action_ids, **overrides) -> XAppCapabilityManifest:
    values = dict(
        manifest_id="xapp-manifest/test", xapp_id="xapp/test",
        xapp_version="1.0.0", kind=kind,
        action_bindings=tuple(_binding(a) for a in action_ids),
        target_scopes=("UE",), required_identifiers=("objectiveUeId",),
        required_kpis=(),
        execution_path_state=XAppExecutionPathState.HARDWARE_FREE_ONLY,
        rollback_supported=True, cooldown_ms=1000,
        max_concurrent_assignments=1)
    values.update(overrides)
    return XAppCapabilityManifest(**values)


class OwnershipBoundaryTests(unittest.TestCase):
    def test_traffic_steering_owns_steer_and_nothing_else(self):
        self.assertEqual(XAPP_KIND_OWNED_ACTIONS[XAppKind.TRAFFIC_STEERING],
                         ("cell-steering",))
        for foreign in ("ue-dl-prb-cap", "scheduler-priority",
                        "dl-rf-attenuation"):
            with self.subTest(action=foreign):
                with self.assertRaises(XAppManifestError):
                    _manifest(XAppKind.TRAFFIC_STEERING,
                              ("cell-steering", foreign))

    def test_ue_scheduler_owns_cap_and_priority_and_nothing_else(self):
        self.assertEqual(set(XAPP_KIND_OWNED_ACTIONS[XAppKind.UE_SCHEDULER]),
                         {"ue-dl-prb-cap", "scheduler-priority"})
        for foreign in ("cell-steering", "dl-rf-attenuation"):
            with self.subTest(action=foreign):
                with self.assertRaises(XAppManifestError):
                    _manifest(XAppKind.UE_SCHEDULER, ("ue-dl-prb-cap", foreign))

    def test_cell_power_owns_rfatt_and_nothing_else(self):
        self.assertEqual(XAPP_KIND_OWNED_ACTIONS[XAppKind.CELL_POWER],
                         ("dl-rf-attenuation",))
        for foreign in ("cell-steering", "ue-dl-prb-cap",
                        "scheduler-priority"):
            with self.subTest(action=foreign):
                with self.assertRaises(XAppManifestError):
                    _manifest(XAppKind.CELL_POWER,
                              ("dl-rf-attenuation", foreign))

    def test_tier_b_actions_are_not_ownable(self):
        with self.assertRaisesRegex(XAppManifestError, "not an ownable"):
            XAppActionBinding(
                action_id="drb-qos", action_family="qos",
                service_model={"serviceModel": "E2SM-RC"},
                parameters=(),
                readback_measurement_ref="measurement/action-space/drb-qos/readback",
                rollback_supported=True,
                deployment_state=ActuatorDeploymentState.HARDWARE_FREE_VERIFIED)

    def test_a_manifest_owns_at_least_one_action(self):
        with self.assertRaises(XAppManifestError):
            _manifest(XAppKind.TRAFFIC_STEERING, ())


class HonestyTests(unittest.TestCase):
    def test_ota_claim_with_blockers_is_refused(self):
        with self.assertRaisesRegex(XAppManifestError, "OTA_LIVE_VERIFIED"):
            _manifest(XAppKind.CELL_POWER, ("dl-rf-attenuation",),
                      execution_path_state=XAppExecutionPathState.OTA_LIVE_VERIFIED,
                      production_blockers=("no E2 encoder",))

    def test_external_source_claim_needs_a_reference(self):
        with self.assertRaisesRegex(XAppManifestError, "external_source_ref"):
            _manifest(XAppKind.TRAFFIC_STEERING, ("cell-steering",),
                      external_source_required=True)


class DefaultManifestTests(unittest.TestCase):
    def setUp(self):
        self.deployment = deployment()
        self.manifests = {m.xapp_id: m
                          for m in default_capability_manifests(self.deployment)}

    def test_default_manifests_pass_catalog_cross_check(self):
        for manifest in self.manifests.values():
            with self.subTest(xapp=manifest.xapp_id):
                validate_xapp_manifest(manifest, deployment=self.deployment)

    def test_telnet_backed_xapps_do_not_claim_a_live_e2_path(self):
        """The legacy telnet knob is never presented as the production E2 path."""
        for xapp_id in ("xapp/ue-scheduler", "xapp/cell-power",
                        "xapp/link-adaptation"):
            manifest = self.manifests[xapp_id]
            with self.subTest(xapp=xapp_id):
                self.assertIs(manifest.execution_path_state,
                              XAppExecutionPathState.HARDWARE_FREE_ONLY)
                self.assertTrue(manifest.production_blockers)
                self.assertTrue(any("telnet" in blocker
                                    for blocker in manifest.production_blockers))
                self.assertTrue(any("not an E2 path" in blocker
                                    or "not the official A1/E2 path" in blocker
                                    for blocker in manifest.production_blockers))

    def test_traffic_steering_records_the_external_source_boundary(self):
        manifest = self.manifests["xapp/traffic-steering"]
        self.assertTrue(manifest.external_source_required)
        self.assertIn("oran-aic-lower-integration/1.0.0",
                      manifest.external_source_ref)
        self.assertIs(manifest.execution_path_state,
                      XAppExecutionPathState.EXTERNAL_SOURCE_REQUIRED)

    def test_slice_resource_carries_the_objective_registry_blockers(self):
        manifest = self.manifests["xapp/slice-resource"]
        record = record_for("SliceSLATarget")
        self.assertFalse(record.deployment_capability.submittable)
        self.assertEqual(manifest.production_blockers,
                         record.deployment_capability.blocking_reasons)
        self.assertIs(manifest.execution_path_state,
                      XAppExecutionPathState.HARDWARE_FREE_ONLY)

    def test_cell_power_documents_the_attenuation_direction(self):
        manifest = self.manifests["xapp/cell-power"]
        self.assertIn("REDUCES DL transmit power", manifest.provenance_note)


class RoundTripTests(unittest.TestCase):
    def test_every_default_manifest_round_trips_and_keeps_its_hash(self):
        for manifest in default_capability_manifests(deployment()):
            with self.subTest(xapp=manifest.xapp_id):
                text = export_manifest_json(manifest)
                rebuilt = parse_manifest_json(text)
                self.assertEqual(rebuilt, manifest)
                self.assertEqual(rebuilt.content_hash(), manifest.content_hash())
                self.assertEqual(export_manifest_json(rebuilt), text)

    def test_parse_refuses_malformed_and_boundary_violating_json(self):
        with self.assertRaises(XAppManifestError):
            parse_manifest_json("not json")
        with self.assertRaises(XAppManifestError):
            parse_manifest_json("[]")
        manifest = default_capability_manifests(deployment())[0]
        record = manifest.to_canonical_dict()
        record["actionBindings"] = [
            dict(binding, actionId="dl-rf-attenuation",
                 actionFamily="rfatt")
            for binding in record["actionBindings"]
        ]
        import json
        with self.assertRaises(XAppManifestError):
            parse_manifest_json(json.dumps(record))


if __name__ == "__main__":
    unittest.main()
