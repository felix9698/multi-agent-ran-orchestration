"""SliceSLATarget hardware-free shared matrix and contract coherence."""
import dataclasses
import unittest
from assurance.objectives.slice_sla_target import SliceSLATargetFamily
from assurance.objectives.registry import EvidenceLevel, RegistryError, SupportState, record_for, validate_record
from tests.assurance.objective_harness import ObjectiveMatrixMixin
from tests.assurance.test_obj1_matrix import _case

class SliceSLAMatrixTests(ObjectiveMatrixMixin, unittest.TestCase):
    def make_case(self): return _case(SliceSLATargetFamily(), "obj3-slice-sla")

class SliceSLAContractTests(unittest.TestCase):
    def setUp(self): self.bundle = _case(SliceSLATargetFamily(), "obj3-slice-contract").bundle
    def test_style2_action6_has_distinct_policy_type_and_axis(self):
        actuator = self.bundle.actuators[0]
        self.assertEqual(actuator.policy_type_id, "AIC_SliceSLATarget_1.0.0")
        self.assertEqual((actuator.service_model["style"], actuator.service_model["action"]), ("2", "6"))
        self.assertEqual(self.bundle.configuration_axes(), ("slicePrbQuota",))
    def test_all_measurements_are_snssai_scoped(self):
        for counter in self.bundle.counters:
            self.assertIn("sst", counter.scope_keys)
            self.assertIn("sd", counter.scope_keys)
        self.assertEqual({p.predicate_id for p in self.bundle.target.predicates}, {"ran-slice-prb-headroom", "core-slice-throughput-floor"})
    def test_registry_is_hardware_free_with_live_only_blockers(self):
        record = record_for("SliceSLATarget")
        self.assertIs(record.support_state, SupportState.HARDWARE_FREE_VERIFIED)
        self.assertIs(record.evidence_level, EvidenceLevel.HARDWARE_FREE_ROUND_TRIP)
        self.assertTrue(record.unmet_premises())
        self.assertTrue(all(p.live_only for p in record.unmet_premises()))
        validate_record(record)
    def test_ota_promotion_remains_blocked(self):
        promoted = dataclasses.replace(record_for("SliceSLATarget"), support_state=SupportState.OTA_VERIFICATION_PENDING)
        with self.assertRaises(RegistryError): validate_record(promoted)

if __name__ == "__main__": unittest.main()
