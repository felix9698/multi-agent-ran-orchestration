"""The epoch freezes a candidate *domain*, not an enumeration (2026-09-19).

Owner: "셀이 10개 되면 선택이 불가하냐, 말이 안 된다".  The freeze used to hash
every point (about 1 ms each, 819,200 points ~ 14 minutes); it now fixes each
axis's allowed values, and the cardinality is their product.
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests.assurance.test_joint_objective import HOME, TARGET, deployment  # noqa: E402

from assurance.contracts.catalog import DomainMembership, generate_catalog  # noqa: E402
from assurance.core.axes import AggregateState  # noqa: E402
from assurance.kernel.kernel import AssuranceKernel, candidate_availability, frozen_candidate  # noqa: E402
from assurance.kernel.reducer import KernelReducer  # noqa: E402
from assurance.kernel.event_store import MemoryEventStore  # noqa: E402
from assurance.live.objective_runtime import _admit_and_freeze  # noqa: E402
from assurance.objectives.joint import (  # noqa: E402
    CapAxisSpec, IntentSpec, PriorityAxisSpec, SteeringAxisSpec, TxAttenuationAxisSpec,
    compose_joint)
from assurance.vertical import SteppingClock  # noqa: E402

IDENTITY = {"contract_id": "catalog/d", "version": "1.0.0", "schema_version": "oran-aic/1.0.0",
            "document_status": "NORMATIVE", "standard_mapping": {}}


def joint(ues, *, atten=True):
    intents = tuple(IntentSpec(f"I{i}", "UELevelTarget", ue, HOME, TARGET, f"hold {ue}")
                    for i, ue in enumerate(ues))
    return compose_joint(
        intents=intents, deployment_binding=deployment(), budget_trials=5,
        steering_axes=tuple(SteeringAxisSpec(ue, HOME, (HOME, TARGET)) for ue in ues),
        cap_axes=tuple(CapAxisSpec(ue, HOME, (0, 6, 12, 18), 1500.0, "cal/t") for ue in ues),
        priority_axes=tuple(PriorityAxisSpec(ue, HOME, (1.0, 2.0, 4.0, 8.0)) for ue in ues),
        attenuation_axes=((TxAttenuationAxisSpec(HOME, ("0.0", "3.0", "6.0", "9.0", "12.0")),
                           TxAttenuationAxisSpec(TARGET, ("10.0", "13.0", "16.0", "19.0", "22.0")))
                          if atten else ()),
        max_catalog_cardinality=10 ** 9)


def catalog_of(composed):
    bundle = composed.bundle
    return generate_catalog(targets=(bundle.target,), capabilities=bundle.capabilities,
                            composition=bundle.composition, generator_version="d",
                            epoch_ref="epoch/d", identity=IDENTITY)


class TheFreezeDoesNotGrowWithTheProduct(unittest.TestCase):
    def test_widening_the_axes_leaves_the_freeze_fast(self):
        timings = {}
        for ues in (("22",), ("22", "24"), ("22", "24", "26")):
            composed = joint(ues)
            started = time.perf_counter()
            catalog = catalog_of(composed)
            timings[catalog.cardinality] = time.perf_counter() - started
            self.assertEqual(composed.catalog_cardinality, catalog.cardinality)
            self.assertIsInstance(catalog.candidates, DomainMembership)
        largest = max(timings)
        self.assertGreater(largest, 800_000)
        # ~1 ms per point before; a whole large domain now freezes in well under a second.
        self.assertLess(timings[largest], 0.5, timings)


class ALargeDomainIsFrozenAcceptedAndCounted(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.composed = joint(("22", "24", "26"))
        cls.kernel = AssuranceKernel(event_store=MemoryEventStore(), reducer=KernelReducer(),
                                     write_gateway=None, measurement_collector=None)
        clock = SteppingClock("2026-09-19T00:00:00Z")
        started = time.perf_counter()
        _admit_and_freeze(cls.kernel, cls.composed.bundle, clock)
        cls.freeze_s = time.perf_counter() - started
        cls.state = cls.kernel.reduced_state()

    def test_the_kernel_freezes_it_quickly_and_records_its_size(self):
        self.assertLess(self.freeze_s, 5.0)
        catalog = self.kernel.current_catalog()
        self.assertEqual(self.composed.catalog_cardinality, catalog.cardinality)
        self.assertEqual([], self.state["catalog"]["candidates"])     # nothing listed
        self.assertTrue(self.state["catalog"]["domain"])

    def test_acceptance_is_membership_and_ids_are_deterministic(self):
        catalog = self.kernel.current_catalog()
        last = catalog.candidates[catalog.cardinality - 1]
        again = catalog.candidates.find(dict(last.parameters))
        self.assertEqual(last.candidate_id, again.candidate_id)
        self.assertEqual(last.semantic_hash, again.semantic_hash)
        row = frozen_candidate(self.state["catalog"], last.candidate_id)
        self.assertEqual(dict(last.parameters), row["parameters"])
        outside = dict(last.parameters)
        key = next(iter(outside))
        outside[key] = "not-an-allowed-value"
        self.assertIsNone(catalog.candidates.find(outside))       # out of range: refused
        self.assertIsNone(frozen_candidate(self.state["catalog"], "candidate/99999999"))

    def test_an_untried_point_is_available_and_the_domain_is_not_exhausted(self):
        self.assertEqual("AVAILABLE", candidate_availability(self.state, "candidate/000123"))
        self.assertEqual({}, dict(self.state["candidateAvailability"]))
        self.kernel.open_case(
            case_id="case/d", policy=self.composed.bundle.case_policy,
            active_vector=self.composed.bundle.vector.ordered_target_refs[0],
            usable_reserve={self.composed.bundle.harm.contract_id:
                            self.composed.bundle.harm.reserve.to_canonical_dict()},
            reserve_per_trial={self.composed.bundle.harm.contract_id:
                               self.composed.bundle.harm.reserve.to_canonical_dict()},
            evidence_cells=(), lazy_evidence_cells=True, now="2026-09-19T00:01:00.000000Z")
        aggregate, _ = self.kernel._exhaustion_for_case(  # noqa: SLF001
            case_id="case/d", vector_ref=self.composed.bundle.vector.ordered_target_refs[0])
        self.assertIs(AggregateState.EVIDENCE_INCOMPLETE, aggregate)


if __name__ == "__main__":
    unittest.main()
