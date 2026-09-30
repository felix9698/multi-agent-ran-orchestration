"""Hardware-free QoETarget positive/negative/fault round-trip."""
import dataclasses
import unittest
from assurance.objectives.qoe_target import QoETargetFamily
from assurance.objectives.registry import EvidenceLevel, RegistryError, SupportState, record_for, validate_record
from tests.assurance.objective_harness import ObjectiveMatrixMixin
from tests.assurance.test_obj1_matrix import _case

class QoETargetMatrixTests(ObjectiveMatrixMixin, unittest.TestCase):
    def make_case(self):
        return _case(QoETargetFamily(), "obj2-qoe")

class QoETargetContractTests(unittest.TestCase):
    def test_hardware_free_state_keeps_live_blockers(self):
        record = record_for("QoETarget")
        self.assertIs(record.support_state, SupportState.HARDWARE_FREE_VERIFIED)
        self.assertIs(record.evidence_level, EvidenceLevel.HARDWARE_FREE_ROUND_TRIP)
        self.assertTrue(all(p.live_only for p in record.unmet_premises()))
        validate_record(record)

    def test_ota_promotion_is_refused_until_live_source_exists(self):
        record = dataclasses.replace(record_for("QoETarget"), support_state=SupportState.OTA_VERIFICATION_PENDING)
        with self.assertRaises(RegistryError):
            validate_record(record)

    def test_measurement_and_actuator_are_distinct(self):
        case = _case(QoETargetFamily(), "obj2-qoe-contract")
        bundle = case.bundle
        self.assertEqual({p.predicate_id for p in bundle.target.predicates}, {"application-experience-floor"})
        self.assertEqual(bundle.actuators[0].policy_type_id, "AIC_UECellSteering_1.0.0")
        self.assertEqual(bundle.actuators[0].service_model["style"], "3")
        self.assertIn("APP.QoEScore", {c.deployment_counter_name for c in bundle.counters})

if __name__ == "__main__": unittest.main()
