"""The joint intent-set bundle: any number of intents, any number of axes."""

from __future__ import annotations

import unittest

from assurance.contracts.capability import DeploymentBinding, TransportSecurity
from assurance.contracts.catalog import generate_catalog
from assurance.contracts.validation import validate_family_set
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION
from assurance.contracts.target import ComparisonOperator
from assurance.objectives.joint import (
    DEFAULT_ATTENUATION_LADDER, DEFAULT_CAP_LADDER, DEFAULT_MCS_LADDER,
    DEFAULT_PF_LADDER, DEFAULT_SLICE_QUOTA_LADDER, INTENT_KIND_KPI,
    INTENT_KIND_SERVING_CELL, CapAxisSpec, CatalogTooLargeError, IntentSpec,
    JointCompositionError, McsBoundsAxisSpec, PriorityAxisSpec,
    SlicePrbQuotaAxisSpec, SteeringAxisSpec, TxAttenuationAxisSpec,
    compose_joint,
)


def deployment() -> DeploymentBinding:
    return DeploymentBinding(
        contract_id="deployment/test", version="1.0.0",
        schema_version=ASSURANCE_SCHEMA_VERSION, document_status="NORMATIVE",
        standard_mapping={"a1p": "2"},
        endpoint_id="test", base_url="https://r1.test/r1",
        transport_security=TransportSecurity.MTLS, secret_refs={"ca": "file:/ca"})


HOME, TARGET = 12345678, 87654321


def two_intents():
    return (
        IntentSpec("I1", "UELevelTarget", "22", HOME, TARGET, "hold UE 22 on the target"),
        IntentSpec("I2", "QoSTarget", "24", HOME, TARGET, "meet the QoS target for UE 24"),
    )


class ComposesAnyNumberOfIntents(unittest.TestCase):

    def compose(self, intents, **kwargs):
        ues = tuple(dict.fromkeys(item.ue_id for item in intents))
        kwargs.setdefault("steering_axes", tuple(
            SteeringAxisSpec(ue, HOME, (HOME, TARGET)) for ue in ues))
        kwargs.setdefault("budget_trials", 5)
        return compose_joint(intents=intents, deployment_binding=deployment(), **kwargs)

    def test_two_intents_on_two_ues_freeze_one_catalog_over_both_axes(self) -> None:
        joint = self.compose(two_intents())
        validate_family_set(joint.bundle.all_contracts() + (joint.bundle.deployment,))
        catalog = generate_catalog(
            targets=(joint.bundle.target,), capabilities=joint.bundle.capabilities,
            composition=joint.bundle.composition, generator_version="test",
            epoch_ref="epoch/test", identity={
                "contract_id": "catalog/test", "version": "1.0.0",
                "schema_version": ASSURANCE_SCHEMA_VERSION, "document_status": "NORMATIVE",
                "standard_mapping": {}})
        self.assertEqual(4, catalog.cardinality)  # 2 cells x 2 cells
        self.assertEqual({"servingCell@22", "servingCell@24"},
                         set(catalog.candidates[0].parameters))
        self.assertEqual(("I1", "I2"), joint.intent_ids)
        self.assertEqual(("steer@22", "steer@24"), joint.action_ids)

    def test_predicates_are_keyed_by_intent_and_all_mandatory(self) -> None:
        joint = self.compose(two_intents())
        ids = {p.predicate_id for p in joint.bundle.target.predicates}
        self.assertTrue(all(p.mandatory for p in joint.bundle.target.predicates))
        for intent_id, predicates in joint.intent_predicates.items():
            self.assertTrue(predicates)
            self.assertTrue(all(p.startswith(f"{intent_id}/") for p in predicates))
            self.assertTrue(set(predicates) <= ids)
        self.assertEqual(joint.intent_predicates, dict(joint.bundle.component_predicates))

    def test_measurements_are_per_intent_and_counters_per_ue(self) -> None:
        joint = self.compose(two_intents())
        measurement_ids = [m.contract_id for m in joint.bundle.measurements]
        self.assertEqual(len(measurement_ids), len(set(measurement_ids)))
        self.assertTrue(any(m.endswith("@I1") for m in measurement_ids))
        self.assertTrue(any(m.endswith("@I2") for m in measurement_ids))
        counter_ids = [c.counter_id for c in joint.bundle.counters]
        self.assertEqual(len(counter_ids), len(set(counter_ids)))
        self.assertTrue(any(c.endswith("@22") for c in counter_ids))
        self.assertTrue(any(c.endswith("@24") for c in counter_ids))
        self.assertEqual(max(int(m.hold_ms) for m in joint.bundle.measurements),
                         joint.bundle.target.hold_ms)

    def test_five_intents_on_two_ues_fold_without_collision(self) -> None:
        intents = tuple(
            IntentSpec(f"I{k}", family, ue, HOME, TARGET)
            for k, (family, ue) in enumerate((
                ("UELevelTarget", "22"), ("QoSTarget", "24"),
                ("TrafficSteeringPreference", "22"), ("QoSandTSP", "24"),
                ("UELevelTarget", "24")), start=1))
        joint = self.compose(intents)
        validate_family_set(joint.bundle.all_contracts() + (joint.bundle.deployment,))
        self.assertEqual(5, len(joint.intent_ids))
        self.assertEqual(5, len(joint.intent_predicates))
        self.assertEqual(2, len(joint.action_ids))

    def test_cap_and_priority_axes_widen_the_product_and_add_their_readback(self) -> None:
        joint = self.compose(
            two_intents(),
            cap_axes=(CapAxisSpec("24", HOME, (6, 12), 1500.0, "calibration/test"),),
            priority_axes=(PriorityAxisSpec("22", HOME, (0.5, 2.0)),))
        validate_family_set(joint.bundle.all_contracts() + (joint.bundle.deployment,))
        space = joint.bundle.target.options[0].parameter_space
        self.assertEqual(("0", "6", "12"), space["dlPrbCap@24"])
        self.assertEqual(("1.0", "0.5", "2.0"), space["pfWeight@22"])
        catalog = generate_catalog(
            targets=(joint.bundle.target,), capabilities=joint.bundle.capabilities,
            composition=joint.bundle.composition, generator_version="test",
            epoch_ref="epoch/test", identity={
                "contract_id": "catalog/test", "version": "1.0.0",
                "schema_version": ASSURANCE_SCHEMA_VERSION, "document_status": "NORMATIVE",
                "standard_mapping": {}})
        self.assertEqual(2 * 2 * 3 * 3, catalog.cardinality)
        self.assertEqual({"0": "dlPrbCap@24", "1.0": "pfWeight@22"},
                         {joint.axis_baselines[a]: a for a in ("dlPrbCap@24", "pfWeight@22")})
        self.assertIn("cap@24", joint.action_ids)
        self.assertIn("priority@22", joint.action_ids)
        self.assertEqual("AIC_UeDlPrbCap_1.0.0", joint.actuators_by_axis["dlPrbCap@24"].policy_type_id)
        self.assertEqual({"cellId": str(HOME), "ueId": "24"}, joint.bundle.scope["controlledUe@24"])

    def test_the_budget_is_the_frozen_trial_cap(self) -> None:
        joint = self.compose(two_intents(), budget_trials=7)
        self.assertEqual(7, joint.bundle.case_policy.max_trials)
        self.assertGreater(joint.bundle.case_policy.max_proposals, 7)

    def test_refusals_name_their_reason(self) -> None:
        with self.assertRaises(JointCompositionError):
            self.compose(())
        with self.assertRaises(JointCompositionError):
            self.compose((IntentSpec("I1", "UELevelTarget", "22", HOME, TARGET),
                          IntentSpec("I1", "QoSTarget", "24", HOME, TARGET)))
        with self.assertRaises(JointCompositionError):
            self.compose((IntentSpec("I1", "UELevelTarget", "22", HOME, TARGET),),
                         steering_axes=(SteeringAxisSpec("24", HOME, (HOME, TARGET)),))
        with self.assertRaises(JointCompositionError):
            IntentSpec("I/1", "UELevelTarget", "22", HOME, TARGET)
        with self.assertRaises(JointCompositionError):
            IntentSpec("I1", "SliceSLATarget", "22", HOME, TARGET)


class AKpiIntentNamesNoCell(unittest.TestCase):
    """An intent whose condition is a KPI folds as membership, not identity.

    The bug this closes: a goodput intent has no cell to pin, so folding it as
    ``servingCell == home`` made the Kernel judge the very control that met the
    KPI a non-success and roll it back -- the best configuration a sitting
    found could never stay applied.
    """

    def compose(self, intents, **kwargs):
        ues = tuple(dict.fromkeys(item.ue_id for item in intents))
        kwargs.setdefault("steering_axes", tuple(
            SteeringAxisSpec(ue, HOME, (HOME, TARGET)) for ue in ues))
        kwargs.setdefault("budget_trials", 5)
        return compose_joint(intents=intents, deployment_binding=deployment(), **kwargs)

    def kpi_intent(self, **kwargs):
        return IntentSpec("I1", "UELevelTarget", "22", HOME, HOME,
                          kind=INTENT_KIND_KPI, **kwargs)

    def test_the_default_kind_pins_the_named_cell_exactly_as_before(self) -> None:
        joint = self.compose((IntentSpec("I1", "UELevelTarget", "22", HOME, TARGET),))
        for predicate in joint.bundle.target.predicates:
            self.assertEqual(ComparisonOperator.EQUAL, predicate.constraint.operator)
            self.assertEqual((), predicate.constraint.allowed_values)
            self.assertEqual(float(TARGET), predicate.constraint.bound.value)

    def test_a_kpi_intent_folds_the_cell_predicate_as_membership(self) -> None:
        joint = self.compose((self.kpi_intent(served_cells=(HOME, TARGET)),))
        self.assertEqual(2, len(joint.bundle.target.predicates))
        for predicate in joint.bundle.target.predicates:
            self.assertEqual(ComparisonOperator.MEMBER_OF, predicate.constraint.operator)
            # The Kernel compares str(observed) and observed is the float
            # aggregate of the NCI counter, so both spellings are admitted.
            self.assertEqual(
                {str(HOME), str(float(HOME)), str(TARGET), str(float(TARGET))},
                set(predicate.constraint.allowed_values))
            self.assertIn("served by one of", predicate.description)
        # everything else about the fold is untouched
        self.assertTrue(all(p.mandatory for p in joint.bundle.target.predicates))
        self.assertEqual(("I1",), joint.intent_ids)
        self.assertIn("22", joint.steering_measurements)
        self.assertTrue(joint.bundle.harm.watchdogs)
        self.assertTrue(joint.bundle.harm.bounds)

    def test_the_membership_set_is_never_empty(self) -> None:
        # An empty served_cells falls back to the cells the intent itself
        # names; the Kernel refuses a MEMBER_OF constraint with no values.
        spec = self.kpi_intent()
        self.assertEqual((HOME,), spec.served_cells)
        self.assertEqual((str(HOME), str(float(HOME))), spec.membership_values)
        joint = self.compose((spec,))
        for predicate in joint.bundle.target.predicates:
            self.assertTrue(predicate.constraint.allowed_values)

    def test_a_kpi_intent_beside_a_serving_cell_intent_keeps_both_readings(self) -> None:
        joint = self.compose((
            IntentSpec("I1", "UELevelTarget", "22", HOME, TARGET),
            IntentSpec("I2", "UELevelTarget", "24", HOME, HOME,
                       kind=INTENT_KIND_KPI, served_cells=(HOME, TARGET))))
        operators = {predicate.predicate_id.split("/", 1)[0]: predicate.constraint.operator
                     for predicate in joint.bundle.target.predicates}
        self.assertEqual(ComparisonOperator.EQUAL, operators["I1"])
        self.assertEqual(ComparisonOperator.MEMBER_OF, operators["I2"])

    def test_an_unknown_kind_is_refused_by_name(self) -> None:
        with self.assertRaises(JointCompositionError) as caught:
            IntentSpec("I1", "UELevelTarget", "22", HOME, TARGET, kind="whatever")
        self.assertIn("whatever", str(caught.exception))
        self.assertEqual(INTENT_KIND_SERVING_CELL,
                         IntentSpec("I1", "UELevelTarget", "22", HOME, TARGET).kind)


if __name__ == "__main__":
    unittest.main()


class CellAndSliceScopedAxes(unittest.TestCase):
    """An axis that is not a UE's still freezes, reads back and rolls back.

    Contract v3 sections 2-3.  The point of the three new axes is that they
    widen the *product*, so what has to hold is that they enter the frozen
    parameter space exactly like the UE-scoped ones while being charged to
    their own scope: a cell's MCS ceiling is not one UE's configuration, and a
    readback that said it was would be wrong by every other UE on that cell.
    """

    def compose(self, **kwargs):
        intents = two_intents()
        kwargs.setdefault("steering_axes", tuple(
            SteeringAxisSpec(item.ue_id, HOME, (HOME, TARGET)) for item in intents))
        kwargs.setdefault("budget_trials", 5)
        return compose_joint(intents=intents, deployment_binding=deployment(),
                             **kwargs)

    def test_each_new_axis_widens_the_frozen_catalog(self):
        joint = self.compose(
            mcs_axes=(McsBoundsAxisSpec(HOME),),
            attenuation_axes=(TxAttenuationAxisSpec(HOME),),
            slice_quota_axes=(SlicePrbQuotaAxisSpec(1, "000001"),))
        validate_family_set(joint.bundle.all_contracts() + (joint.bundle.deployment,))
        space = joint.bundle.target.options[0].parameter_space
        self.assertEqual(DEFAULT_MCS_LADDER, space[f"dlMcsBounds@{HOME}"])
        self.assertEqual(DEFAULT_ATTENUATION_LADDER,
                         space[f"txAttenuationDb@{HOME}"])
        self.assertEqual(DEFAULT_SLICE_QUOTA_LADDER, space["slicePrbQuota@1"])
        # 2 cells x 2 cells x 3 x 3 x 3
        self.assertEqual(4 * 27, joint.catalog_cardinality)
        catalog = generate_catalog(
            targets=(joint.bundle.target,), capabilities=joint.bundle.capabilities,
            composition=joint.bundle.composition, generator_version="test",
            epoch_ref="epoch/test", identity={
                "contract_id": "catalog/test", "version": "1.0.0",
                "schema_version": ASSURANCE_SCHEMA_VERSION,
                "document_status": "NORMATIVE", "standard_mapping": {}})
        self.assertEqual(4 * 27, catalog.cardinality)

    def test_the_baseline_and_the_safe_state_carry_every_new_axis(self):
        joint = self.compose(
            mcs_axes=(McsBoundsAxisSpec(HOME),),
            attenuation_axes=(TxAttenuationAxisSpec(HOME),),
            slice_quota_axes=(SlicePrbQuotaAxisSpec(1),))
        for surface in (joint.bundle.baseline_config, joint.bundle.safe_state):
            self.assertEqual("0..28", dict(surface)[f"dlMcsBounds@{HOME}"])
            self.assertEqual("0.0", dict(surface)[f"txAttenuationDb@{HOME}"])
            self.assertEqual("0:1:100", dict(surface)["slicePrbQuota@1"])

    def test_a_cell_axis_is_read_back_at_the_cell_not_at_a_ue(self):
        joint = self.compose(mcs_axes=(McsBoundsAxisSpec(HOME),))
        readback = next(m for m in joint.bundle.measurements
                        if m.contract_id.endswith("/dl-mcs-bounds"))
        self.assertEqual({"cellId": str(HOME)}, dict(readback.scope_selector))
        counter = next(c for c in joint.bundle.counters
                       if c.counter_id == readback.counter_id)
        self.assertEqual(("cellId",), tuple(counter.scope_keys))
        self.assertEqual("RAN.Cell.DlMcsBounds", counter.deployment_counter_name)

    def test_a_slice_axis_is_read_back_at_the_snssai(self):
        joint = self.compose(
            slice_quota_axes=(SlicePrbQuotaAxisSpec(1, "000001"),))
        readback = next(m for m in joint.bundle.measurements
                        if m.contract_id.endswith("/slice-prb-quota"))
        self.assertEqual({"sNssai": "1-000001"}, dict(readback.scope_selector))
        self.assertEqual({"sst": "1", "sd": "000001", "sNssai": "1-000001"},
                         dict(joint.bundle.scope)["slice@1"])

    def test_each_new_axis_names_the_actuator_that_carries_it(self):
        joint = self.compose(
            mcs_axes=(McsBoundsAxisSpec(HOME),),
            attenuation_axes=(TxAttenuationAxisSpec(HOME),),
            slice_quota_axes=(SlicePrbQuotaAxisSpec(1),))
        expected = {f"dlMcsBounds@{HOME}": ("AIC_DlMcsBounds_1.0.0", "101"),
                    f"txAttenuationDb@{HOME}": ("AIC_CellDlTxPower_1.0.0", "104"),
                    "slicePrbQuota@1": ("AIC_SliceSLATarget_1.0.0", "6")}
        for axis, (policy_type, action) in expected.items():
            actuator = joint.actuators_by_axis[axis]
            self.assertEqual(policy_type, actuator.policy_type_id)
            self.assertEqual(action, actuator.service_model["action"])
            # every actuator reads back the measurement of its own axis
            self.assertTrue(any(m.contract_id == actuator.readback_measurement_ref
                                for m in joint.bundle.measurements))

    def test_the_axis_scope_map_says_where_each_axis_is_written(self):
        joint = self.compose(
            cap_axes=(CapAxisSpec("24", HOME, DEFAULT_CAP_LADDER, 1500.0, "cal/t"),),
            mcs_axes=(McsBoundsAxisSpec(HOME),),
            slice_quota_axes=(SlicePrbQuotaAxisSpec(1),))
        self.assertEqual("ue@24", joint.axis_scopes["dlPrbCap@24"])
        self.assertEqual(f"cell@{HOME}", joint.axis_scopes[f"dlMcsBounds@{HOME}"])
        self.assertEqual("slice@1", joint.axis_scopes["slicePrbQuota@1"])

    def test_a_ladder_rung_the_declaration_refuses_names_itself(self):
        for build, message in (
                (lambda: McsBoundsAxisSpec(HOME, ("20..10",)), "exceeds maxDlMcs"),
                (lambda: TxAttenuationAxisSpec(HOME, ("6.05",)),
                 "cannot round trip"),
                (lambda: SlicePrbQuotaAxisSpec(1, quotas=("0:70:60",)),
                 "dedicated <= min <= max")):
            with self.assertRaisesRegex(JointCompositionError, message):
                build()

    def test_an_snssai_that_is_not_one_is_refused(self):
        with self.assertRaisesRegex(JointCompositionError, "SST is 1..255"):
            SlicePrbQuotaAxisSpec(0)
        with self.assertRaisesRegex(JointCompositionError, "six hexadecimal"):
            SlicePrbQuotaAxisSpec(1, "zz")

    def test_a_ladder_of_nothing_but_the_baseline_is_not_an_axis(self):
        with self.assertRaisesRegex(JointCompositionError, "at least one rung"):
            McsBoundsAxisSpec(HOME, ("0..28",))


class TheCatalogCeiling(unittest.TestCase):
    """The catalog is the product of the axes, so widening them multiplies."""

    def every_axis(self):
        intents = two_intents()
        ues = tuple(item.ue_id for item in intents)
        return dict(
            intents=intents,
            steering_axes=tuple(SteeringAxisSpec(ue, HOME, (HOME, TARGET))
                                for ue in ues),
            cap_axes=tuple(CapAxisSpec(ue, HOME, DEFAULT_CAP_LADDER, 1500.0,
                                       "cal/t") for ue in ues),
            priority_axes=tuple(PriorityAxisSpec(ue, HOME, DEFAULT_PF_LADDER)
                                for ue in ues),
            mcs_axes=tuple(McsBoundsAxisSpec(cell) for cell in (HOME, TARGET)),
            attenuation_axes=tuple(TxAttenuationAxisSpec(cell)
                                   for cell in (HOME, TARGET)),
            slice_quota_axes=(SlicePrbQuotaAxisSpec(1),),
            deployment_binding=deployment(), budget_trials=5)

    def test_every_axis_on_two_ues_and_two_cells_is_refused_by_name(self):
        with self.assertRaises(CatalogTooLargeError) as caught:
            compose_joint(**self.every_axis(), max_catalog_cardinality=4096)
        message = str(caught.exception)
        self.assertIn("248832 combinations", message)
        self.assertIn("steer 4 x cap 16 x pf 16 x mcs 9 x atten 9 x quota 3",
                      message)
        self.assertIn("the ceiling is 4096", message)
        self.assertIn("--axes", message)

    def test_dropping_an_axis_kind_brings_the_same_sitting_under_the_ceiling(self):
        narrowed = self.every_axis()
        narrowed["priority_axes"] = ()
        narrowed["attenuation_axes"] = ()
        joint = compose_joint(**narrowed)
        self.assertEqual(4 * 16 * 9 * 3, joint.catalog_cardinality)
        self.assertLessEqual(joint.catalog_cardinality, 4096)

    def test_the_ceiling_is_the_operator_s_to_raise(self):
        joint = compose_joint(**self.every_axis(),
                              max_catalog_cardinality=250_000)
        self.assertEqual(248_832, joint.catalog_cardinality)

    def test_refusing_builds_nothing(self):
        """The refusal is a statement about the axes, not a half-built case."""
        with self.assertRaises(CatalogTooLargeError):
            compose_joint(**self.every_axis(), max_catalog_cardinality=10)
        # a narrower sitting composed afterwards is unaffected
        narrowed = self.every_axis()
        narrowed["cap_axes"] = ()
        narrowed["priority_axes"] = ()
        narrowed["mcs_axes"] = ()
        narrowed["attenuation_axes"] = ()
        narrowed["slice_quota_axes"] = ()
        self.assertEqual(4, compose_joint(**narrowed).catalog_cardinality)

    def test_the_default_cap_ladder_keeps_the_uncapped_sentinel_as_a_baseline(self):
        spec = CapAxisSpec("24", HOME, DEFAULT_CAP_LADDER, 1500.0, "cal/t")
        self.assertEqual(("0", "6", "12", "18"), spec.values)
        self.assertEqual("0", spec.baseline)
        weight = PriorityAxisSpec("24", HOME, DEFAULT_PF_LADDER)
        self.assertEqual(("1.0", "0.5", "2.0", "4.0"), weight.values)
