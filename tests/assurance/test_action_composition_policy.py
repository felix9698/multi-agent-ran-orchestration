"""Objective-axis composition, safety, harm, and refusal-order tests."""
import inspect
import unittest

from assurance.actions import action_catalog
from assurance.actions.composition_policy import (
    HARM_AGGREGATION_POLICY, OBJECTIVE_ACTION_POLICIES, ActionRole,
    HarmAggregationRule,
)
from assurance.advisors.action_space import (
    ActionNotAllowedForFamilyError, AdvisoryAction,
    CompositionConstraintViolationError, DuplicateActionError,
    EmptyCompositionError, ForbiddenCombinationError,
    LiveCompositionNotSubmittableError, UnknownActionError,
    UnknownObjectiveFamilyError, resolve_composition,
)
from assurance.contracts.capability import DeploymentBinding, TransportSecurity
from assurance.contracts.harm import HarmKind
from assurance.objectives import FAMILY_MODULES, record_for


def _deployment():
    return DeploymentBinding(
        contract_id="deployment/action-composition-hf", version="1.0.0",
        schema_version="1.0.0", document_status="NORMATIVE",
        standard_mapping={"test": "hardware-free"}, endpoint_id="mock-write-gateway",
        base_url="https://127.0.0.1:9443", transport_security=TransportSecurity.MTLS,
        secret_refs={"clientCert": "env:HF_CLIENT_CERT"})


def _selector(**updates):
    value = {
        "objectiveUeId": "ue-target",
        "rntiBinding": "RESOLVE_AFTER_PRIMARY_READBACK",
    }
    value.update(updates)
    return value


def _action(action_id):
    if action_id == "cell-steering":
        return AdvisoryAction(action_id, {"targetPrimaryCellId": 87654321},
                              {"objectiveUeId": "ue-target"})
    if action_id == "slice-prb-quota":
        return AdvisoryAction(action_id, {
            "sst": 1, "sd": "000001", "minPrbPolicyRatio": 30,
            "maxPrbPolicyRatio": 90, "dedicatedPrbPolicyRatio": 15,
        })
    if action_id == "ue-dl-prb-cap":
        return AdvisoryAction(action_id, {"rnti": 0x1111, "maxDlPrbs": 20},
                              _selector(controlledUeRole="NON_TARGET_HEAVY_UE",
                                        sliceRelation="OUTSIDE_OBJECTIVE_SLICE"))
    if action_id == "scheduler-priority":
        return AdvisoryAction(action_id, {"rnti": 0x2222, "pfWeight": 2.0},
                              _selector(controlledUeRole="TARGET_UE"))
    raise AssertionError(f"no positive fixture for {action_id}")


class ObjectiveActionCompositionTests(unittest.TestCase):
    def setUp(self):
        self.deployment = _deployment()

    def test_every_declared_combination_has_one_primary_and_resolves(self):
        for family, policy in OBJECTIVE_ACTION_POLICIES.items():
            for combination in policy.allowed_combinations:
                with self.subTest(family=family, combination=combination):
                    resolved = resolve_composition(
                        tuple(_action(action_id) for action_id in reversed(combination)),
                        self.deployment, family,
                        for_live=record_for(family).deployment_capability.submittable)
                    self.assertEqual(resolved.apply_order, combination)
                    self.assertEqual(resolved.rollback_order, tuple(reversed(combination)))
                    self.assertIn(resolved.primary_action_id, combination)

    def test_policy_primary_axes_match_frozen_bundle_axes_both_directions(self):
        scope = {
            "ueId": "ue-target", "cellId": "NRCellDU-1", "sst": "1",
            "sd": "000001", "targetServingCell": "87654321",
            "homeServingCell": "12345678",
        }
        for family, module_type in FAMILY_MODULES.items():
            with self.subTest(family=family):
                module = module_type()
                bundle = module.contract_bundle(scope=scope,
                                                deployment_binding=self.deployment)
                policy = OBJECTIVE_ACTION_POLICIES[family]
                primary_axes = {binding.candidate_axis
                                for binding in policy.primary_bindings}
                self.assertEqual(primary_axes, set(bundle.configuration_axes()))
                self.assertEqual(primary_axes, set(module.candidate_parameters()))
                for combination in policy.allowed_combinations:
                    roles = {binding.action_id: binding.classification
                             for binding in policy.action_bindings}
                    self.assertEqual(sum(roles[action_id] is ActionRole.PRIMARY
                                         for action_id in combination), 1)

    def test_mcs_and_rf_are_equally_forbidden_for_floor_families(self):
        for family in ("QoSTarget", "QoSandTSP", "QoETarget", "QoEandTSP"):
            for action in (
                AdvisoryAction("dl-mcs-bounds", {"maxDlMcs": 2, "minDlMcs": 0}),
                AdvisoryAction("dl-rf-attenuation", {"txAttenuationDb": 12}),
            ):
                with self.subTest(family=family, action=action.action_id):
                    with self.assertRaises(ForbiddenCombinationError):
                        resolve_composition((action,), self.deployment, family)

    def test_supplementary_actions_never_resolve_standalone(self):
        for family in ("QoSTarget", "UELevelTarget", "QoSandTSP", "SliceSLATarget"):
            policy = OBJECTIVE_ACTION_POLICIES[family]
            for binding in policy.action_bindings:
                if binding.classification is not ActionRole.SUPPLEMENTARY:
                    continue
                with self.subTest(family=family, action=binding.action_id):
                    with self.assertRaises(ForbiddenCombinationError):
                        resolve_composition((_action(binding.action_id),), self.deployment,
                                            family, for_live=False)

    def test_named_resolution_errors_and_hardware_free_path(self):
        with self.assertRaises(EmptyCompositionError):
            resolve_composition((), self.deployment, "QoSTarget")
        with self.assertRaises(DuplicateActionError):
            resolve_composition((_action("cell-steering"), _action("cell-steering")),
                                self.deployment, "QoSTarget")
        with self.assertRaises(UnknownObjectiveFamilyError):
            resolve_composition((_action("cell-steering"),), self.deployment, "NoSuch")
        with self.assertRaises(UnknownActionError):
            resolve_composition((AdvisoryAction("not-an-action", {}),),
                                self.deployment, "QoSTarget")
        with self.assertRaises(ActionNotAllowedForFamilyError):
            resolve_composition((_action("ue-dl-prb-cap"),), self.deployment,
                                "TrafficSteeringPreference")
        resolved = resolve_composition((_action("slice-prb-quota"),), self.deployment,
                                       "SliceSLATarget", for_live=False)
        self.assertEqual(resolved.primary_candidate_axis, "slicePrbQuota")

    def test_live_support_state_refuses_with_real_blocking_reasons(self):
        with self.assertRaises(LiveCompositionNotSubmittableError) as caught:
            resolve_composition((_action("slice-prb-quota"),), self.deployment,
                                "SliceSLATarget", for_live=True)
        message = str(caught.exception)
        for reason in record_for("SliceSLATarget").deployment_capability.blocking_reasons:
            self.assertIn(reason, message)

    def test_tier_b_live_refusal_is_derived_for_every_catalog_tier_b_action(self):
        tier_b_ids = {item.action_id for item in action_catalog(self.deployment)
                      if item.tier == "B"}
        self.assertTrue(tier_b_ids)
        parameter_fixtures = {
            "drb-qos": {"qfi": 1, "drbId": 1, "fiveQi": 9},
            "radio-access-control": {"accessOperation": "ACCESS_BARRING"},
            "dual-connectivity": {"scgOperation": "ADD", "secondaryCellId": 2},
            "carrier-aggregation": {"sCellOperation": "ADD", "sCellId": 2},
            "idle-mode-mobility": {"cellReselectionPriority": 3, "frequency": 640000},
        }
        self.assertEqual(set(parameter_fixtures), tier_b_ids)
        for action_id in tier_b_ids:
            with self.subTest(action=action_id):
                with self.assertRaisesRegex(ForbiddenCombinationError, "contract-only"):
                    resolve_composition((AdvisoryAction(action_id,
                                        parameter_fixtures[action_id]),),
                                        self.deployment, "QoSTarget", for_live=True)

    def test_every_declared_constraint_has_a_refusal_exercise(self):
        steer = _action("cell-steering")
        refusal_cases = {
            "ue-scope-identity": (
                "TrafficSteeringPreference",
                (AdvisoryAction("cell-steering", {"targetPrimaryCellId": 87654321}),),
                True),
            "target-cap-throughput-floor": (
                "QoSTarget", (steer, AdvisoryAction("ue-dl-prb-cap",
                    {"rnti": 0x1111, "maxDlPrbs": 20},
                    _selector(controlledUeRole="TARGET_UE"))), True),
            "priority-target-role": (
                "QoSTarget", (steer, AdvisoryAction("scheduler-priority",
                    {"rnti": 0x2222, "pfWeight": 2.0},
                    _selector(controlledUeRole="NON_TARGET_HEAVY_UE"))), True),
            "qos-resource-role-separation": (
                "QoSTarget", (steer, _action("ue-dl-prb-cap"),
                    AdvisoryAction("scheduler-priority", {"rnti": 0x1111, "pfWeight": 2.0},
                                   _selector(controlledUeRole="TARGET_UE"))), True),
            "rnti-refresh-after-primary": (
                "QoSTarget", (steer, AdvisoryAction("scheduler-priority",
                    {"rnti": 0x2222, "pfWeight": 2.0},
                    _selector(controlledUeRole="TARGET_UE", rntiBinding="STATIC"))), True),
            "slice-cap-outside-objective": (
                "SliceSLATarget", (_action("slice-prb-quota"),
                    AdvisoryAction("ue-dl-prb-cap", {"rnti": 0x1111, "maxDlPrbs": 20},
                                   _selector(sliceRelation="IN_OBJECTIVE_SLICE"))), False),
        }
        declared = {constraint.constraint_id
                    for policy in OBJECTIVE_ACTION_POLICIES.values()
                    for constraint in policy.constraints}
        self.assertEqual(set(refusal_cases), declared)
        for constraint_id, (family, proposals, for_live) in refusal_cases.items():
            with self.subTest(constraint=constraint_id):
                with self.assertRaisesRegex(CompositionConstraintViolationError,
                                            constraint_id):
                    resolve_composition(proposals, self.deployment, family,
                                        for_live=for_live)

    def test_ue_scope_rejects_different_objective_ids(self):
        priority = AdvisoryAction("scheduler-priority", {"rnti": 0x2222, "pfWeight": 2.0},
                                  _selector(objectiveUeId="other-ue",
                                            controlledUeRole="TARGET_UE"))
        with self.assertRaisesRegex(CompositionConstraintViolationError,
                                    "objectiveUeId values differ"):
            resolve_composition((_action("cell-steering"), priority), self.deployment,
                                "QoSTarget")

    def test_every_required_selector_refuses_non_string_values(self):
        cases = (
            ("objectiveUeId", "TrafficSteeringPreference",
             (AdvisoryAction("cell-steering", {"targetPrimaryCellId": 87654321},
                             {"objectiveUeId": 0}),), True),
            ("controlledUeRole", "QoSTarget",
             (_action("cell-steering"), AdvisoryAction("ue-dl-prb-cap",
                 {"rnti": 0x1111, "maxDlPrbs": 20},
                 _selector(controlledUeRole=None))), True),
            ("rntiBinding", "QoSTarget",
             (_action("cell-steering"), AdvisoryAction("scheduler-priority",
                 {"rnti": 0x2222, "pfWeight": 2.0},
                 _selector(controlledUeRole="TARGET_UE", rntiBinding=0))), True),
            ("sliceRelation", "SliceSLATarget",
             (_action("slice-prb-quota"), AdvisoryAction("ue-dl-prb-cap",
                 {"rnti": 0x1111, "maxDlPrbs": 20},
                 _selector(sliceRelation=None))), False),
        )
        for selector_name, family, proposals, for_live in cases:
            with self.subTest(selector=selector_name):
                with self.assertRaisesRegex(CompositionConstraintViolationError,
                                            f"{selector_name} must be a non-empty string"):
                    resolve_composition(proposals, self.deployment, family,
                                        for_live=for_live)

    def test_internally_contradictory_selector_pair_is_refused(self):
        contradictory_cap = AdvisoryAction(
            "ue-dl-prb-cap", {"rnti": 0x1111, "maxDlPrbs": 20},
            _selector(controlledUeRole="TARGET_UE",
                      sliceRelation="OUTSIDE_OBJECTIVE_SLICE"))
        with self.assertRaisesRegex(CompositionConstraintViolationError,
                                    "selector-consistency"):
            resolve_composition((_action("slice-prb-quota"), contradictory_cap),
                                self.deployment, "SliceSLATarget", for_live=False)

    def test_rnti_binding_and_selector_claims_are_deferred_to_named_layers(self):
        resolved = resolve_composition((_action("cell-steering"),
                                       _action("scheduler-priority")),
                                       self.deployment, "QoSTarget")
        self.assertEqual(len(resolved.rnti_bindings), 1)
        binding = resolved.rnti_bindings[0]
        self.assertEqual(binding.resolution_after_action_id, "cell-steering")
        self.assertEqual(binding.resolution_semantics,
                         "RESOLVE_AFTER_PRIMARY_READBACK")
        self.assertTrue(binding.attribution_measurement_refs)
        self.assertIn("Write Gateway", binding.verifying_layer)
        self.assertTrue(resolved.selector_verifications)
        self.assertTrue(all(requirement.verifying_layer == "Kernel admission"
                            for requirement in resolved.selector_verifications))

    def test_resolved_parameters_and_selectors_are_immutable(self):
        resolved = resolve_composition((_action("cell-steering"),), self.deployment,
                                       "TrafficSteeringPreference")
        with self.assertRaises(TypeError):
            resolved.actions[0].parameters["targetPrimaryCellId"] = 1
        with self.assertRaises(TypeError):
            resolved.actions[0].target_selector["objectiveUeId"] = "other"

    def test_combined_harm_carries_watchdogs_charges_and_reverse_rollback(self):
        actions = (_action("cell-steering"), _action("ue-dl-prb-cap"),
                   _action("scheduler-priority"))
        resolved = resolve_composition(actions, self.deployment, "QoSTarget")
        combined = resolved.combined_harm.by_kind[HarmKind.TRIAL_INDUCED]
        self.assertEqual(combined.aggregation_rule, HarmAggregationRule.SUM)
        self.assertEqual(combined.admissible_bound.value, 6000)
        self.assertEqual(combined.required_reserve.value, 6000)
        self.assertEqual(len(resolved.combined_harm.required_watchdogs), 3)
        self.assertEqual(len(resolved.combined_harm.missing_interval_charges), 3)
        self.assertEqual(resolved.rollback_order,
                         ("scheduler-priority", "ue-dl-prb-cap", "cell-steering"))

    def test_max_harm_rules_are_declared_but_unreachable_in_current_catalog(self):
        kinds = {item.harm.harm_kind for item in action_catalog(self.deployment)}
        self.assertEqual(kinds, {HarmKind.TRIAL_INDUCED})
        self.assertEqual(HARM_AGGREGATION_POLICY[HarmKind.CONTRACT].rule,
                         HarmAggregationRule.MAX)
        self.assertEqual(HARM_AGGREGATION_POLICY[HarmKind.TARGET_DEBT].rule,
                         HarmAggregationRule.MAX)

    def test_policy_registry_catalog_and_constraint_handlers_are_consistent(self):
        self.assertEqual(set(OBJECTIVE_ACTION_POLICIES), set(FAMILY_MODULES))
        catalog_ids = {action.action_id for action in action_catalog(self.deployment)}
        mentioned_ids = set()
        for policy in OBJECTIVE_ACTION_POLICIES.values():
            mentioned_ids.update(policy.allowed_action_ids)
            for combination in policy.allowed_combinations:
                self.assertLessEqual(set(combination), catalog_ids)
            for forbidden in policy.forbidden:
                mentioned_ids.update(forbidden.action_ids)
        tier_b = {action.action_id for action in action_catalog(self.deployment)
                  if action.tier == "B"}
        self.assertEqual(mentioned_ids | tier_b, catalog_ids)
        signature = inspect.signature(resolve_composition)
        self.assertNotIn("objective_context", signature.parameters)


if __name__ == "__main__":
    unittest.main()
