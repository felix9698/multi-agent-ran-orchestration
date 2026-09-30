"""Hardware-free contract and deterministic rollback matrix for every action."""
import unittest

from assurance.actions import action_catalog, action_catalog_view, validate_action_parameters
from assurance.advisors.action_space import AdvisoryAction, resolve_composition
from assurance.contracts.capability import (
    ActuatorDeploymentState, DeploymentBinding, TransportSecurity,
)
from assurance.contracts.validation import ContractAdmissionError, validate_family_set


def _deployment():
    return DeploymentBinding(
        contract_id="deployment/action-space-hf", version="1.0.0",
        schema_version="1.0.0", document_status="NORMATIVE",
        standard_mapping={"test": "hardware-free"}, endpoint_id="mock-write-gateway",
        base_url="https://127.0.0.1:9443", transport_security=TransportSecurity.MTLS,
        secret_refs={"clientCert": "env:HF_CLIENT_CERT"})


class _HardwareFreeActuator:
    def __init__(self):
        self.value = "baseline"
        self.baseline = "baseline"

    def round_trip(self, scenario):
        if scenario == "positive":
            self.value = "candidate"
            return "APPLY_READBACK_COMMIT", self.value
        if scenario == "negative":
            return "PARAMETER_REFUSED_NO_EFFECT", self.value
        self.value = "partial"
        self.value = self.baseline
        return "STOP_ROLLBACK_READBACK_BASELINE", self.value


class ActionCatalogContractTests(unittest.TestCase):
    def setUp(self):
        self.deployment = _deployment()
        self.catalog = action_catalog(self.deployment)

    def test_complete_diverse_ordered_catalog(self):
        self.assertEqual(len(self.catalog), 11)
        self.assertEqual([a.tier for a in self.catalog[:6]], ["A"] * 6)
        self.assertEqual([a.binding.service_model["style"] for a in self.catalog],
                         ["3", "2", "2", "2", "CUSTOM", "2", "1", "4", "5", "6", "7"])
        self.assertEqual(sum(a.binding.live_capable for a in self.catalog), 6)

    def test_every_entry_is_a_cross_resolved_contract_family(self):
        for item in self.catalog:
            with self.subTest(action=item.action_id):
                validate_family_set((self.deployment, item.counter, item.readback,
                                     item.watchdog, item.harm, item.binding,
                                     item.capability))
                self.assertTrue(item.binding.parameters)
                self.assertTrue(item.binding.rollback_supported)
                self.assertEqual(item.binding.deployment_state,
                                 ActuatorDeploymentState.HARDWARE_FREE_VERIFIED)

    def test_contract_only_entries_fail_closed_for_live(self):
        for item in self.catalog[6:]:
            with self.subTest(action=item.action_id):
                self.assertFalse(item.binding.live_capable)
                self.assertIn("NOT_ACTUATED_BY_DEPLOYMENT",
                              item.binding.live_blocking_premise)

    def test_positive_negative_and_fault_round_trip_for_every_action(self):
        for item in self.catalog:
            for scenario in ("positive", "negative", "fault"):
                with self.subTest(action=item.action_id, scenario=scenario):
                    harness = _HardwareFreeActuator()
                    result, final = harness.round_trip(scenario)
                    self.assertEqual(result, item.hardware_free_expectations[scenario])
                    if scenario in ("negative", "fault"):
                        self.assertEqual(final, harness.baseline)

    def test_agentic_view_exposes_only_proposals_not_command_payloads(self):
        view = action_catalog_view(self.deployment)
        self.assertEqual(len(view), len(self.catalog))
        for row in view:
            self.assertIn("parameters", row)
            self.assertIn("readbackMeasurementRef", row)
            self.assertIn("watchdogRef", row)
            self.assertNotIn("command", row)
            self.assertNotEqual(row["hardwareFreeState"], "OTA_LIVE_VERIFIED")

    def test_invalid_enriched_live_binding_is_refused(self):
        item = self.catalog[0]
        broken = item.binding.__class__(**{
            **item.binding.__dict__, "live_backend": None,
        })
        with self.assertRaisesRegex(ContractAdmissionError, "real backend"):
            validate_family_set((self.deployment, item.counter, item.readback,
                                 item.watchdog, item.harm, broken, item.capability))

    def test_adviser_can_compose_multiple_catalog_actions_but_not_actuate(self):
        proposals = (
            AdvisoryAction("ue-dl-prb-cap", {"rnti": 0x1234, "maxDlPrbs": 12},
                           {"objectiveUeId": "ue-target", "controlledUeRole": "NON_TARGET_HEAVY_UE",
                            "rntiBinding": "RESOLVE_AFTER_PRIMARY_READBACK"}),
            AdvisoryAction("scheduler-priority", {"rnti": 0x5678, "pfWeight": 2.0},
                           {"objectiveUeId": "ue-target", "controlledUeRole": "TARGET_UE",
                            "rntiBinding": "RESOLVE_AFTER_PRIMARY_READBACK"}),
        )
        proposals = (AdvisoryAction("cell-steering", {"targetPrimaryCellId": 87654321},
                                    {"objectiveUeId": "ue-target"}),) + proposals
        resolved = resolve_composition(proposals, self.deployment, "QoSTarget")
        self.assertEqual(resolved.actions, proposals)

    def test_out_of_range_and_cross_parameter_proposals_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "above maximum"):
            resolve_composition((
                AdvisoryAction("cell-steering", {"targetPrimaryCellId": 87654321},
                               {"objectiveUeId": "ue-target"}),
                AdvisoryAction("ue-dl-prb-cap", {"rnti": 0x1234, "maxDlPrbs": 276},
                               {"objectiveUeId": "ue-target",
                                "rntiBinding": "RESOLVE_AFTER_PRIMARY_READBACK"}),
            ), self.deployment, "UELevelTarget")
        with self.assertRaisesRegex(ValueError, "minDlMcs exceeds"):
            action = next(item for item in self.catalog if item.action_id == "dl-mcs-bounds")
            validate_action_parameters(action, {"maxDlMcs": 10, "minDlMcs": 11})


if __name__ == "__main__":
    unittest.main()
