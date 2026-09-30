"""Hardware-free QoEandTSP joint-trial contract."""
import unittest
from assurance.objectives.qoe_and_tsp import QoEandTSPFamily
from assurance.objectives.registry import SupportState, record_for, validate_registry, OBJECTIVE_REGISTRY
from tests.assurance.objective_harness import ObjectiveMatrixMixin
from tests.assurance.test_obj1_matrix import _case

class QoEandTSPMatrixTests(ObjectiveMatrixMixin, unittest.TestCase):
    def make_case(self): return _case(QoEandTSPFamily(), "obj2-qoe-tsp")

class QoEandTSPContractTests(unittest.TestCase):
    def test_components_are_mandatory_in_one_target(self):
        bundle = _case(QoEandTSPFamily(), "obj2-qoe-tsp-contract").bundle
        self.assertEqual(dict(bundle.component_predicates), {
            "QoETarget": ("application-experience-floor",),
            "TrafficSteeringPreference": ("serving-cell-preferred-min", "serving-cell-preferred-max"),
        })
        self.assertEqual(len(bundle.target.predicates), 3)

    def test_registry_is_hardware_free_and_composite_valid(self):
        self.assertIs(record_for("QoEandTSP").support_state, SupportState.HARDWARE_FREE_VERIFIED)
        validate_registry(OBJECTIVE_REGISTRY)

if __name__ == "__main__": unittest.main()
