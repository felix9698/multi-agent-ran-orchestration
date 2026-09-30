"""The frozen contract of the SUPPLEMENTARY UE DL PRB cap (E2SM-RC 2/102).

What this file pins is the *client* half of the path
``Kernel permit -> Write Gateway/R1Adapter("r1-cap") -> R1 -> A1-P -> xApp ->
FlexRIC -> E2SM-RC Style 2 Action 102 -> OAI gNB``: the action catalog's exact
style/action/parameter-path constants, the ``[5,24] PRB`` APPLY range on a
24-PRB radio, the adapter and policy type permanently bound to the action, and
the two facts a policy body must never carry -- an RNTI, and the objective UE.

The policy type is the released Campaign 5 ``AIC_UeDlPrbCap_1.0.0`` this
deployment's producer already advertises.  Hermetic: no socket, no radio, no
subprocess, no model.
"""

from __future__ import annotations

import unittest

from assurance.actions import action_catalog, validate_action_parameters
from assurance.actions.catalog import (
    ActionParameterError, UE_DL_PRB_CAP_APPLY_RANGE,
    UE_DL_PRB_CAP_UNCAPPED_SENTINEL,
)
from assurance.objectives.action102_support import (
    ATTENUATION_ACTION_ID, CAP_ACTION_ID, CAP_ADAPTER_KEY, CAP_AXIS,
    CAP_POLICY_TYPE_ID, CELL_SCOPE_KEY, MCS_ACTION_ID, PRIORITY_ACTION_ID,
    SLICE_QUOTA_ACTION_ID, SLICE_SCOPE_KEY, SUPPLEMENTARY_ACTIONS,
    SupplementaryCapError, cap_candidate_values, supplementary_action,
    supplementary_axis_declarations,
)

from oran.campaign5 import validate_campaign5
from oran.campaign5.families import CAMPAIGN5_FAMILIES, Campaign5Error

from tests.assurance.action102_support import (
    APPLIED_CAP, CAP_FAMILY, CONTROLLED_UE, OBJECTIVE_UE, VALIDITY,
    controlled_scope_builder, plan_scope,
)
from tests.assurance.xapp_support import deployment


def _command(value=APPLIED_CAP, scope=None, axis=CAP_AXIS):
    return {
        "operation": "APPLY", "transactionId": "tx-cap", "trialId": "trial-cap",
        "fencingToken": 3, "commandSequence": 1, "commandIndex": 1,
        "idempotencyKey": "tx-cap:APPLY:3:1",
        "scope": plan_scope() if scope is None else scope,
        "axis": axis, "value": value,
    }


class CatalogPinsTests(unittest.TestCase):
    """The action catalog states the deployed definition, not a placeholder."""

    def setUp(self):
        self.contract = {item.action_id: item
                         for item in action_catalog(deployment())}[CAP_ACTION_ID]

    def test_the_service_model_pins_style_2_action_102_and_211_212(self):
        model = dict(self.contract.binding.service_model)
        self.assertEqual(model["serviceModel"], "E2SM-RC")
        self.assertEqual(model["style"], "2")
        self.assertEqual(model["action"], "102")
        self.assertEqual(model["ranParameterPath"], "211/212")

    def test_the_adapter_and_policy_type_are_frozen_on_the_binding(self):
        # Not selectable from proposal text: an advisory names an action, and
        # which client carries it is deployment data.
        self.assertEqual(
            dict(self.contract.binding.service_model)["adapter"], CAP_ADAPTER_KEY)
        self.assertEqual(self.contract.binding.policy_type_id, CAP_POLICY_TYPE_ID)
        self.assertEqual(CAP_POLICY_TYPE_ID, CAP_FAMILY.policy_type_id)
        self.assertEqual(CAP_ADAPTER_KEY, CAP_FAMILY.adapter_name)

    def test_the_readback_counter_is_the_configuration_counter(self):
        self.assertEqual(self.contract.counter.deployment_counter_name,
                         "RAN.UE.DlPrbCap")
        self.assertEqual(self.contract.counter.source.value,
                         "CONFIGURATION_READBACK")
        self.assertEqual(CAP_FAMILY.readback_counter, "RAN.UE.DlPrbCap")
        self.assertEqual(
            SUPPLEMENTARY_ACTIONS[CAP_ACTION_ID].readback_counter,
            self.contract.counter.deployment_counter_name)

    def test_the_rc_definition_matches_the_campaign_five_family(self):
        self.assertEqual(CAP_FAMILY.rc_style, 2)
        self.assertEqual(CAP_FAMILY.rc_action_id, 102)
        self.assertEqual(CAP_FAMILY.rc_param_ids, (211, 212))

    def test_the_apply_range_is_five_to_twenty_four_on_a_24_prb_radio(self):
        self.assertEqual(UE_DL_PRB_CAP_APPLY_RANGE, (5, 24))
        for value in (5, 12, 24):
            validate_action_parameters(
                self.contract, {"rnti": 0x4602, "maxDlPrbs": value})
        for value in (1, 4, 25, 276):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ActionParameterError, "5..24|above maximum"):
                    validate_action_parameters(
                        self.contract, {"rnti": 0x4602, "maxDlPrbs": value})

    def test_zero_is_the_uncapped_sentinel_and_never_a_candidate(self):
        # It is a legitimate observed baseline and restore value...
        validate_action_parameters(
            self.contract,
            {"rnti": 0x4602, "maxDlPrbs": UE_DL_PRB_CAP_UNCAPPED_SENTINEL})
        # ...and never a member of the frozen candidate catalog.
        with self.assertRaisesRegex(SupplementaryCapError, "outside the frozen"):
            cap_candidate_values((0,))
        self.assertEqual(
            SUPPLEMENTARY_ACTIONS[CAP_ACTION_ID].baseline,
            str(UE_DL_PRB_CAP_UNCAPPED_SENTINEL))


class CandidateCatalogTests(unittest.TestCase):

    def test_the_catalog_is_finite_sorted_and_deduplicated(self):
        self.assertEqual(cap_candidate_values((16, 8, 8, 12)), (8, 12, 16))

    def test_an_empty_catalog_is_refused(self):
        with self.assertRaisesRegex(SupplementaryCapError, "at least one value"):
            cap_candidate_values(())

    def test_the_axis_declaration_names_the_one_adapter_that_writes_it(self):
        declared = supplementary_axis_declarations()
        self.assertEqual(len(declared), 1)
        self.assertEqual(declared[0], {
            "candidateParameter": CAP_AXIS, "axis": CAP_AXIS,
            "adapter": CAP_ADAPTER_KEY})

    def test_an_undeclared_supplementary_action_has_no_adapter(self):
        # ``drb-qos`` is in the action catalog but has no A1/E2 encoder on this
        # deployment, so nothing declares an adapter, a policy type or a
        # readback counter for it and no composition may invent one.
        with self.assertRaisesRegex(SupplementaryCapError, "not a declared"):
            supplementary_action("drb-qos")


class PolicyBodyTests(unittest.TestCase):
    """A cap policy names the controlled UE, never an RNTI and never the target."""

    def setUp(self):
        self.build = controlled_scope_builder()

    def test_the_body_validates_and_names_the_controlled_ue(self):
        body = self.build(_command())
        validate_campaign5(body, f"{CAP_POLICY_TYPE_ID}.policy")
        self.assertEqual(body["config"]["ueId"], CONTROLLED_UE["ueId"])
        self.assertNotEqual(body["config"]["ueId"], OBJECTIVE_UE["ueId"])
        self.assertEqual(body["config"]["maxDlPrbs"], int(APPLIED_CAP))

    def test_the_body_carries_no_rnti(self):
        # The resolved RNTI is status and readback data.  A caller-supplied one
        # would be a path for aiming the cap at a UE nobody attributed.
        body = self.build(_command())
        self.assertNotIn("rnti", body["config"])
        self.assertNotIn("rnti", body["trace"])

    def test_the_trace_carries_the_permit_fence_and_transaction(self):
        body = self.build(_command())
        # 2026-09-20: the transaction, plus the scope -- one A1 id per scope.
        self.assertEqual(body["trace"]["traceId"], "tx-cap#cellId=12345678/ueId=132")
        self.assertEqual(body["trace"]["fencingToken"], 3)
        self.assertEqual(body["validity"], dict(VALIDITY))

    def test_a_plan_scope_with_no_controlled_ue_is_refused_before_any_write(self):
        with self.assertRaisesRegex(ValueError, "no controlled UE"):
            self.build(_command(scope=dict(OBJECTIVE_UE)))

    def test_capping_the_objective_ue_is_refused(self):
        scope = {**OBJECTIVE_UE, "controlledUe": dict(OBJECTIVE_UE)}
        with self.assertRaisesRegex(ValueError, "objective UE"):
            self.build(_command(scope=scope))

    def test_a_value_outside_this_deployments_range_is_refused_before_the_wire(self):
        # The released 1.0.0 schema was written for a 275-PRB carrier and would
        # admit 30.  This deployment is 24 PRB, so the frozen APPLY range is the
        # binding one and the refusal happens before a body exists.
        for value in ("30", "4", "276"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(SupplementaryCapError, "5..24 PRB"):
                    self.build(_command(value=value))

    def test_the_axis_is_the_one_the_declaration_names(self):
        with self.assertRaisesRegex(ValueError, "is not"):
            self.build(_command(axis="pfWeight"))


if __name__ == "__main__":
    unittest.main()


class CellAndSliceScopedDeclarations(unittest.TestCase):
    """The three actions that are not a UE's, as an epoch would freeze them.

    Contract v3 sections 1-2.  What has to be true of them is not that they are
    new -- they are not, every string here is already carried by the Campaign 5
    families and the action catalog -- but that a composition can tell them
    apart from a UE's action *by their declaration alone*: a different scope
    kind, a different readback key, and an axis value that decodes into every
    policy leaf the one write carries.
    """

    def test_each_one_is_declared_with_its_own_scope_and_readback_key(self):
        expected = {
            MCS_ACTION_ID: ("NRCellDU", CELL_SCOPE_KEY, "dlMcsBounds", "0..28"),
            ATTENUATION_ACTION_ID: ("NRCellDU", CELL_SCOPE_KEY,
                                    "txAttenuationDb", "0.0"),
            SLICE_QUOTA_ACTION_ID: ("S-NSSAI", SLICE_SCOPE_KEY,
                                    "slicePrbQuota", "0:1:100"),
        }
        for action_id, (kind, key, axis, baseline) in expected.items():
            declared = supplementary_action(action_id)
            self.assertEqual(kind, declared.scope_kind, action_id)
            self.assertEqual(key, declared.scope_key, action_id)
            self.assertEqual(axis, declared.axis, action_id)
            self.assertEqual(baseline, declared.baseline, action_id)
            self.assertTrue(declared.is_composite, action_id)

    def test_a_ue_scoped_action_is_still_keyed_by_the_controlled_ue(self):
        for action_id in (CAP_ACTION_ID, PRIORITY_ACTION_ID):
            declared = supplementary_action(action_id)
            self.assertEqual("UE", declared.scope_kind)
            self.assertEqual("controlledUeId", declared.scope_key)
            self.assertFalse(declared.is_composite)

    def test_a_composite_axis_value_decodes_into_every_policy_leaf(self):
        cases = {
            MCS_ACTION_ID: ("10..24", {"minDlMcs": 10, "maxDlMcs": 24}),
            ATTENUATION_ACTION_ID: ("6.0", {"txAttenuationDb": 6.0}),
            SLICE_QUOTA_ACTION_ID: ("0:20:60", {
                "dedicatedPrbPolicyRatio": 0, "minPrbPolicyRatio": 20,
                "maxPrbPolicyRatio": 60}),
        }
        for action_id, (axis_value, leaves) in cases.items():
            declared = supplementary_action(action_id)
            self.assertEqual(leaves, declared.policy_values(axis_value))
            # and back to the very string the catalog froze
            self.assertEqual(axis_value, declared.axis_value_text(leaves))

    def test_every_leaf_the_policy_writes_is_named_by_the_declaration(self):
        for action_id, leaves in (
                (MCS_ACTION_ID, {"minDlMcs", "maxDlMcs"}),
                (ATTENUATION_ACTION_ID, {"txAttenuationDb"}),
                (SLICE_QUOTA_ACTION_ID, {"dedicatedPrbPolicyRatio",
                                         "minPrbPolicyRatio",
                                         "maxPrbPolicyRatio"})):
            declared = supplementary_action(action_id)
            self.assertEqual(leaves, set(declared.leaves))
            self.assertEqual(
                set(declared.policy_values(declared.baseline)), leaves)

    def test_a_composite_axis_has_no_single_wire_scalar(self):
        for action_id in (MCS_ACTION_ID, SLICE_QUOTA_ACTION_ID):
            with self.assertRaisesRegex(SupplementaryCapError,
                                        "no single wire scalar"):
                supplementary_action(action_id).wire_value("0..28")

    def test_out_of_range_and_cross_field_violations_are_refused(self):
        cases = (
            (MCS_ACTION_ID, "0..31", "outside the frozen"),
            (MCS_ACTION_ID, "20..10", "exceeds maxDlMcs"),
            (MCS_ACTION_ID, "16", "'<min>..<max>'"),
            (ATTENUATION_ACTION_ID, "61.0", "outside the"),
            (ATTENUATION_ACTION_ID, "6.05", "cannot round trip"),
            (SLICE_QUOTA_ACTION_ID, "0:70:60", "dedicated <= min <= max"),
            (SLICE_QUOTA_ACTION_ID, "0:1:101", "outside"),
            (SLICE_QUOTA_ACTION_ID, "0:1", "<dedicated>:<min>:<max>"),
        )
        for action_id, axis_value, message in cases:
            with self.assertRaisesRegex(SupplementaryCapError, message):
                supplementary_action(action_id).policy_values(axis_value)

    def test_the_declaration_agrees_with_the_campaign5_family_it_names(self):
        """The strings are named as data here; nothing may drift from the
        producer that actually carries them."""
        for key, action_id in (("cap", CAP_ACTION_ID),
                               ("priority", PRIORITY_ACTION_ID),
                               ("mcs", MCS_ACTION_ID),
                               ("power", ATTENUATION_ACTION_ID)):
            family = CAMPAIGN5_FAMILIES[key]
            declared = supplementary_action(action_id)
            self.assertEqual(family.catalog_action_id, declared.action_id)
            self.assertEqual(family.policy_type_id, declared.policy_type_id)
            self.assertEqual(family.adapter_name, declared.adapter)
            self.assertEqual(family.readback_counter, declared.readback_counter)
            self.assertEqual(family.axis, declared.axis)
            self.assertEqual(family.scope_kind, declared.scope_kind)
            self.assertEqual(set(family.value_fields), set(declared.leaves))
