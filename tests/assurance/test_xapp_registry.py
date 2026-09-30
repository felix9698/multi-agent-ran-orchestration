"""XApp Capability Registry: registration, lookup, runtime/static split."""
import unittest

from assurance.xapps import (
    CapabilityOwnershipError, INITIAL_COORDINATED_LIVE_SET,
    XAppCapabilityRegistry, XAppKind, XAppRegistryError, XAppRuntimeStatus,
    default_capability_manifests,
)
from assurance.xapps.registry import LiveSelectionState

from tests.assurance.xapp_support import NOW, deployment, live_status


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.deployment = deployment()
        self.registry = XAppCapabilityRegistry(deployment=self.deployment)
        self.manifests = default_capability_manifests(self.deployment)

    def test_register_and_lookup(self):
        for manifest in self.manifests:
            self.registry.register(manifest)
        self.assertEqual(len(self.registry.manifests()), 5)
        owner = self.registry.owner_of("cell-steering")
        self.assertEqual(owner.xapp_id, "xapp/traffic-steering")
        self.assertEqual(self.registry.owner_of("scheduler-priority").xapp_id,
                         "xapp/ue-scheduler")
        self.assertIsNone(self.registry.owner_of("drb-qos"))

    def test_duplicate_registration_is_refused(self):
        self.registry.register(self.manifests[0])
        with self.assertRaisesRegex(XAppRegistryError, "already registered"):
            self.registry.register(self.manifests[0])

    def test_one_action_has_one_owner(self):
        for manifest in self.manifests:
            self.registry.register(manifest)
        clone = default_capability_manifests(self.deployment)[0]
        renamed = type(clone)(**{**_kwargs(clone), "xapp_id": "xapp/second-ts",
                                 "manifest_id": "xapp-manifest/second-ts"})
        with self.assertRaisesRegex(CapabilityOwnershipError, "already owned"):
            self.registry.register(renamed)

    def test_runtime_status_needs_a_registered_xapp(self):
        with self.assertRaisesRegex(XAppRegistryError, "unregistered"):
            self.registry.update_runtime_status(live_status("xapp/nobody"))


class RuntimeStateSeparationTests(unittest.TestCase):
    def setUp(self):
        self.registry = XAppCapabilityRegistry(deployment=deployment())
        for manifest in default_capability_manifests(deployment()):
            self.registry.register(manifest)

    def test_capability_without_runtime_status_is_not_selectable(self):
        selection = self.registry.live_xapp_for("cell-steering", now=NOW)
        self.assertFalse(selection.selectable)
        self.assertIs(selection.state, LiveSelectionState.NO_RUNTIME_STATUS)

    def test_capability_without_e2_wire_is_not_selectable(self):
        self.registry.update_runtime_status(
            live_status("xapp/ue-scheduler", e2_connected=False))
        selection = self.registry.live_xapp_for("ue-dl-prb-cap", now=NOW)
        self.assertFalse(selection.selectable)
        self.assertIs(selection.state, LiveSelectionState.NOT_WIRED)

    def test_stale_heartbeat_and_pause_block_selection(self):
        self.registry.update_runtime_status(live_status(
            "xapp/traffic-steering",
            heartbeat_at="2026-08-31T09:00:00.000000Z"))
        stale = self.registry.live_xapp_for("cell-steering", now=NOW)
        self.assertIs(stale.state, LiveSelectionState.STALE_HEARTBEAT)
        self.registry.update_runtime_status(
            live_status("xapp/traffic-steering", paused=True))
        paused = self.registry.live_xapp_for("cell-steering", now=NOW)
        self.assertIs(paused.state, LiveSelectionState.PAUSED)

    def test_fully_wired_xapp_is_selectable(self):
        self.registry.update_runtime_status(live_status("xapp/traffic-steering"))
        selection = self.registry.live_xapp_for("cell-steering", now=NOW)
        self.assertTrue(selection.selectable)
        self.assertEqual(selection.manifest.xapp_id, "xapp/traffic-steering")


class CoordinatedLiveSetTests(unittest.TestCase):
    def setUp(self):
        self.registry = XAppCapabilityRegistry(deployment=deployment())
        for manifest in default_capability_manifests(deployment()):
            self.registry.register(manifest)

    def test_initial_live_set_is_ts_scheduler_power(self):
        self.assertEqual(INITIAL_COORDINATED_LIVE_SET,
                         (XAppKind.TRAFFIC_STEERING, XAppKind.UE_SCHEDULER,
                          XAppKind.CELL_POWER))

    def test_link_adaptation_is_excluded_but_its_capability_is_preserved(self):
        self.registry.update_runtime_status(live_status("xapp/link-adaptation"))
        selection = self.registry.live_xapp_for("dl-mcs-bounds", now=NOW)
        self.assertFalse(selection.selectable)
        self.assertIs(selection.state,
                      LiveSelectionState.NOT_IN_COORDINATED_LIVE_SET)
        # The capability itself is still registered and queryable.
        self.assertEqual(self.registry.owner_of("dl-mcs-bounds").xapp_id,
                         "xapp/link-adaptation")

    def test_slice_resource_is_outside_the_live_set(self):
        self.registry.update_runtime_status(live_status("xapp/slice-resource"))
        selection = self.registry.live_xapp_for("slice-prb-quota", now=NOW)
        self.assertFalse(selection.selectable)
        self.assertIs(selection.state,
                      LiveSelectionState.NOT_IN_COORDINATED_LIVE_SET)


def _kwargs(manifest):
    return dict(
        manifest_id=manifest.manifest_id, xapp_id=manifest.xapp_id,
        xapp_version=manifest.xapp_version, kind=manifest.kind,
        action_bindings=manifest.action_bindings,
        target_scopes=manifest.target_scopes,
        required_identifiers=manifest.required_identifiers,
        required_kpis=manifest.required_kpis,
        execution_path_state=manifest.execution_path_state,
        rollback_supported=manifest.rollback_supported,
        cooldown_ms=manifest.cooldown_ms,
        max_concurrent_assignments=manifest.max_concurrent_assignments,
        production_blockers=manifest.production_blockers,
        external_source_required=manifest.external_source_required,
        external_source_ref=manifest.external_source_ref,
        provenance_note=manifest.provenance_note,
        schema_version=manifest.schema_version)


if __name__ == "__main__":
    unittest.main()
