"""``UELevelTarget`` -- the six-seat implementation and its hardware-free matrix.

Owner lane: **OBJ2** (``docs/architecture/SEAMS-GATE4.md`` section 3).

Task section 8's chain for this family, less items 1/2/12 (registry record,
Intent Agent grammar, and a Gate 5 OTA artefact -- not this module's job).
The matrix class is the four lines ``docs/architecture/SEAMS-GATE4.md``
section 3 promises; everything else here pins facts a matrix pass alone does
not check: ``candidate_parameters``/``policy_lifecycle``/``kpi_declaration``
actually describe a per-UE identity (never a cell aggregate wearing a UE
label), and ``terminal_oracle`` -- which the shared harness never calls,
because it exists for the Cockpit/Replay side of task section 8 item 10 --
reads the Kernel's own evaluation axes correctly on its own.
"""

from __future__ import annotations

import unittest

from assurance.core.axes import (
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
    TrialOutcome,
)
from assurance.objectives.family import ScenarioName, VERDICT_SCENARIOS
from assurance.objectives.registry import record_for
from assurance.objectives.ue_level_target import HOME_NCI, TARGET_NCI, UELevelTargetFamily
from tests.assurance.objective_harness import FamilyCase, ObjectiveMatrixMixin
from tests.assurance.test_obj2_support import ue_level_target_bundle, ue_level_target_case


class UELevelTargetMatrixTests(ObjectiveMatrixMixin, unittest.TestCase):
    def make_case(self) -> FamilyCase:
        return ue_level_target_case()


class UELevelTargetContractBundleTests(unittest.TestCase):
    """The bundle the frozen seat actually builds, not a hand-rolled stand-in."""

    def test_the_bundle_names_the_family_and_no_other(self):
        bundle = ue_level_target_bundle()
        self.assertEqual(bundle.family, "UELevelTarget")
        self.assertEqual(bundle.target.objective_family, "UELevelTarget")

    def test_the_predicate_is_an_identity_pair_not_a_rate(self):
        bundle = ue_level_target_bundle()
        self.assertEqual(len(bundle.target.predicates), 2)
        for predicate in bundle.target.predicates:
            constraint = predicate.constraint
            self.assertEqual(constraint.bound.unit, "nci")
            self.assertEqual(constraint.bound.value, float(TARGET_NCI))
        for measurement in bundle.measurements:
            # An identity has no measurement error; a non-zero margin would
            # let EQUAL accept a neighbouring cell identity.
            self.assertEqual(measurement.uncertainty_rule.parameter.value, 0.0)

    def test_the_candidate_surface_is_exactly_one_axis(self):
        bundle = ue_level_target_bundle()
        self.assertEqual(bundle.configuration_axes(), ("servingCell",))

    def test_the_bundle_is_not_a_composite(self):
        bundle = ue_level_target_bundle()
        self.assertEqual(bundle.component_predicates, {})


class UELevelTargetDeclarationTests(unittest.TestCase):
    """Chain items 4, 5, 6 and 8: catalog, R1/A1-P lifecycle, KPI scope."""

    def test_candidate_parameters_are_the_two_real_cells(self):
        catalog = UELevelTargetFamily().candidate_parameters()
        self.assertEqual(catalog, {"servingCell": (str(HOME_NCI), str(TARGET_NCI))})

    def test_policy_lifecycle_names_the_frozen_policy_type_and_a_real_readback(self):
        lifecycle = UELevelTargetFamily().policy_lifecycle()
        self.assertEqual(lifecycle.policy_type_id, "AIC_UECellSteering_1.0.0")
        self.assertNotIn("A1 create", lifecycle.enforcement_evidence)
        self.assertNotIn("E2 ACK", lifecycle.enforcement_evidence)
        self.assertIn("readback", lifecycle.enforcement_evidence)

    def test_kpi_declaration_is_scoped_to_the_ue_not_the_cell(self):
        declarations = UELevelTargetFamily().kpi_declaration()
        self.assertEqual(len(declarations), 2)
        for declaration in declarations:
            self.assertEqual(declaration.scope_level, "UE")
            self.assertEqual(declaration.unit, "nci")


class UELevelTargetTerminalOracleTests(unittest.TestCase):
    """The oracle reads the Kernel's own axes; it is not a second evaluator.

    Not exercised by the shared matrix (``objective_harness.py`` never calls
    a family's ``terminal_oracle``), so this is the seat's only test.
    """

    def _evaluation(self, **overrides):
        base = {
            "executionValidity": ExecutionValidity.VALID.value,
            "measurementSufficiency": MeasurementSufficiency.SUFFICIENT.value,
            "predicateVerdicts": {
                "ue-level-serving-cell-is-target-throughout": PredicateVerdict.PASS.value,
                "ue-level-serving-cell-is-target-only": PredicateVerdict.PASS.value,
            },
            "mandatoryPredicateIds": [
                "ue-level-serving-cell-is-target-throughout",
                "ue-level-serving-cell-is-target-only",
            ],
            "holdComplete": True,
            "sameCandidate": True,
            "validityRegionStable": True,
        }
        base.update(overrides)
        return base

    def test_success_requires_every_axis_true(self):
        oracle = UELevelTargetFamily().terminal_oracle
        self.assertIs(oracle(self._evaluation()), TrialOutcome.SUCCESS)

    def test_one_failed_mandatory_predicate_is_fail_not_success(self):
        oracle = UELevelTargetFamily().terminal_oracle
        evaluation = self._evaluation(
            predicateVerdicts={
                "ue-level-serving-cell-is-target-throughout": PredicateVerdict.FAIL.value,
                "ue-level-serving-cell-is-target-only": PredicateVerdict.PASS.value,
            }
        )
        self.assertIs(oracle(evaluation), TrialOutcome.FAIL)

    def test_insufficient_measurement_is_indeterminate_not_fail(self):
        oracle = UELevelTargetFamily().terminal_oracle
        evaluation = self._evaluation(
            measurementSufficiency=MeasurementSufficiency.INSUFFICIENT_COVERAGE.value,
            predicateVerdicts={
                "ue-level-serving-cell-is-target-throughout": PredicateVerdict.INDETERMINATE.value,
                "ue-level-serving-cell-is-target-only": PredicateVerdict.INDETERMINATE.value,
            },
        )
        self.assertIs(oracle(evaluation), TrialOutcome.INDETERMINATE)

    def test_invalid_execution_is_never_promoted_to_a_verdict(self):
        oracle = UELevelTargetFamily().terminal_oracle
        evaluation = self._evaluation(executionValidity=ExecutionValidity.INVALID.value)
        self.assertIs(oracle(evaluation), TrialOutcome.INVALID)

    def test_exec_error_outranks_everything_else(self):
        oracle = UELevelTargetFamily().terminal_oracle
        evaluation = self._evaluation(executionValidity=ExecutionValidity.EXEC_ERROR.value)
        self.assertIs(oracle(evaluation), TrialOutcome.EXEC_ERROR)

    def test_hold_incomplete_is_not_success_even_with_passing_predicates(self):
        oracle = UELevelTargetFamily().terminal_oracle
        evaluation = self._evaluation(holdComplete=False)
        self.assertIs(oracle(evaluation), TrialOutcome.FAIL)


class UELevelTargetExpectationsCoverageTests(unittest.TestCase):
    def test_every_verdict_scenario_has_an_expectation(self):
        expectations = UELevelTargetFamily().hardware_free_expectations()
        self.assertEqual(set(expectations), set(VERDICT_SCENARIOS))


class UELevelTargetRegistryTests(unittest.TestCase):
    """The record the matrix's own result now has to justify."""

    def test_the_record_matches_what_the_matrix_actually_passed(self):
        record = record_for("UELevelTarget")
        # The matrix itself is what this test is about, and it is unchanged:
        # every hardware-free scenario still passes and is still named.
        self.assertEqual(
            set(record.hardware_free_scenarios),
            {scenario.value for scenario in ScenarioName},
        )
        # Gate 5 stage 2 (2026-08-25) then drove this family over real radio --
        # one success reaching SETTLED_SUCCESS / CLOSED_PASS and one recorded
        # non-success with the full reverse-rollback recovery -- so the record
        # moved past the hardware-free states it sat in at Gate 4.  The OTA
        # claim is not free: the validator requires retained evidence, and the
        # references are checked to exist in
        # tests/assurance/test_oseam_registry.py.
        self.assertEqual(record.support_state.value, "OTA_LIVE_VERIFIED")
        self.assertEqual(record.evidence_level.value, "OTA_RAW_EVIDENCE")
        self.assertTrue(record.evidence_refs)
        # Submittable since the dispatcher ruling of 2026-08-25 established the
        # versioned wire-kind mapping this family's steering travels on
        # (ASSURANCE_FAMILY_TO_WIRE_KIND v1.2.0, GATE4-ACCEPTANCE section 7b).
        # Submitting is a different claim from having been verified; the
        # mapping is what made the submission possible, and the OTA run above
        # is what made the verification real.
        self.assertTrue(record.deployment_capability.submittable)
        from assurance.objectives.registry import ASSURANCE_FAMILY_TO_WIRE_KIND

        self.assertEqual(ASSURANCE_FAMILY_TO_WIRE_KIND["UELevelTarget"], "PIN_TO_CELL")

    def test_the_measurement_source_premise_is_narrowed_not_flipped(self):
        """D1: the premise wording must show what this identity-scoped
        objective actually needs, not silently claim a rate measurement now
        exists."""
        record = record_for("UELevelTarget")
        premises = {premise.kind.value: premise for premise in record.premises}
        measurement = premises["MEASUREMENT_SOURCE"]
        self.assertTrue(measurement.met)
        self.assertIn("identity", measurement.statement)

    def test_the_unsupported_rate_fact_is_preserved_not_deleted(self):
        """The fact outlived the blocking reason that used to carry it.

        ``blocking_reasons`` must be empty for a submittable record, so when the
        wire-kind ruling made this family submittable the per-UE rate fact had
        to move rather than disappear.  It moved into the standard mapping's
        notes, restated as what it actually is: not a reason the deployment
        refuses this objective, but the limit of what this objective claims --
        its predicates are the identity pair and need no rate.  This test is
        why it could not simply be dropped.
        """
        record = record_for("UELevelTarget")
        notes = record.standard_mapping.notes
        self.assertIn("RRU.PrbTotDl", notes)
        self.assertIn("no source here", notes)
        self.assertIn("scope limit rather than a submission block", notes)
        self.assertEqual(record.deployment_capability.blocking_reasons, ())


if __name__ == "__main__":
    unittest.main()
