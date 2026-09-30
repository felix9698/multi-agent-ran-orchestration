"""What a family may compose, and what the epoch freezes when it does.

The cap is SUPPLEMENTARY.  It selects no candidate, it never runs standalone,
and it acts on a heavy UE the objective does *not* protect.  This file pins the
five statements that make that true rather than merely stated:

* live admission is limited to ``QoSTarget`` and ``UELevelTarget``;
* PRIMARY steering is applied first and reverse rollback unwinds backwards;
* the frozen candidate catalog is finite and inside ``[5,24] PRB``;
* the objective UE's throughput floor becomes a *mandatory* predicate and a
  ``STOP_AND_ROLLBACK`` watchdog whenever a cap is composed;
* the controlled UE's debt is charged to its own scope, and a cap on the
  objective UE is refused before anything is prepared.

Hermetic: no socket, no radio, no subprocess, no model.
"""

from __future__ import annotations

import unittest

from assurance.actions.composition_policy import (
    LIVE_CAP_FAMILIES, OBJECTIVE_ACTION_POLICIES, ActionRole, apply_order,
    live_cap_admissible, rollback_order,
)
from assurance.contracts.harm import WatchdogAction
from assurance.objectives import FAMILY_MODULES
from assurance.objectives.action102_support import (
    CAP_ACTION_ID, CAP_ADAPTER_KEY, CAP_AXIS, CONTROLLED_UE_SCOPE_KEY,
    SupplementaryCapError, SupplementaryCapRequest, cap_axis_baseline,
    with_supplementary_cap,
)

from tests.assurance.xapp_support import deployment

STEER = "cell-steering"
PRIORITY = "scheduler-priority"


def _request(controlled="132", caps=(8, 12)):
    return SupplementaryCapRequest(
        controlled_ue={"cellId": "12345678", "ueId": controlled},
        candidate_caps=caps,
        objective_throughput_floor_kbps=500.0,
        controlled_harm_reserve_kbps=1500.0,
        calibration_ref="calibration/test/ue-dl-prb-cap",
        controlled_ue_scope_id=controlled,
    )


def _bundle(family="QoSTarget", ue_id="131"):
    module = FAMILY_MODULES[family]()
    return module.contract_bundle(
        scope={"ueId": ue_id, "cellId": "NRCellDU-1",
               "homeServingCell": 12345678, "targetServingCell": 87654321},
        deployment_binding=deployment())


class AdmissionTests(unittest.TestCase):

    def test_a_live_cap_is_admitted_only_in_the_two_named_families(self):
        self.assertEqual(LIVE_CAP_FAMILIES, ("QoSTarget", "UELevelTarget"))
        for family in LIVE_CAP_FAMILIES:
            self.assertTrue(live_cap_admissible(family))
        for family in ("TrafficSteeringPreference", "SliceSLATarget",
                       "QoETarget", "QoEandTSP", "QoSandTSP"):
            with self.subTest(family=family):
                self.assertFalse(live_cap_admissible(family))

    def test_the_cap_is_supplementary_in_every_policy_that_declares_it(self):
        for policy in OBJECTIVE_ACTION_POLICIES.values():
            for binding in policy.action_bindings:
                if binding.action_id != CAP_ACTION_ID:
                    continue
                with self.subTest(family=policy.objective_family):
                    self.assertIs(binding.classification, ActionRole.SUPPLEMENTARY)
                    self.assertIsNone(binding.candidate_axis)

    def test_the_cap_never_appears_without_a_primary_in_the_same_set(self):
        for policy in OBJECTIVE_ACTION_POLICIES.values():
            primaries = {b.action_id for b in policy.primary_bindings}
            for combination in policy.allowed_combinations:
                if CAP_ACTION_ID not in combination:
                    continue
                with self.subTest(family=policy.objective_family,
                                  combination=combination):
                    self.assertTrue(primaries & set(combination))

    def test_both_live_families_require_a_non_target_controlled_ue(self):
        for family in LIVE_CAP_FAMILIES:
            constraints = {c.constraint_id
                           for c in OBJECTIVE_ACTION_POLICIES[family].constraints}
            with self.subTest(family=family):
                self.assertIn("target-cap-throughput-floor", constraints)


class OrderingTests(unittest.TestCase):

    def test_primary_steering_is_applied_before_the_cap(self):
        self.assertEqual(
            apply_order((CAP_ACTION_ID, STEER), "QoSTarget"),
            (STEER, CAP_ACTION_ID))

    def test_rollback_is_exactly_the_reverse_of_apply(self):
        actions = (STEER, CAP_ACTION_ID, PRIORITY)
        self.assertEqual(
            rollback_order(actions, "QoSTarget"),
            tuple(reversed(apply_order(actions, "QoSTarget"))))

    def test_a_steering_only_composition_keeps_its_order(self):
        self.assertEqual(apply_order((STEER,), "UELevelTarget"), (STEER,))


class BundleExtensionTests(unittest.TestCase):

    def setUp(self):
        self.bundle = _bundle()
        self.capped = with_supplementary_cap(self.bundle, _request())

    def test_the_cap_axis_joins_the_baseline_and_the_safe_state_uncapped(self):
        self.assertNotIn(CAP_AXIS, dict(self.bundle.baseline_config))
        self.assertEqual(dict(self.capped.baseline_config)[CAP_AXIS],
                         cap_axis_baseline())
        self.assertEqual(dict(self.capped.safe_state)[CAP_AXIS],
                         cap_axis_baseline())

    def test_the_candidate_catalog_is_finite_and_inside_the_apply_range(self):
        space = dict(self.capped.target.options[0].parameter_space)
        self.assertEqual(space[CAP_AXIS], ("8", "12"))
        self.assertIn("servingCell", space)

    def test_the_objective_floor_becomes_a_mandatory_predicate(self):
        predicate = next(p for p in self.capped.target.predicates
                         if p.predicate_id.endswith("objective-ue-throughput-floor"))
        self.assertTrue(predicate.mandatory)
        self.assertIn("ue-throughput-objective", predicate.constraint.measurement_ref)

    def test_the_floor_and_the_controlled_ue_both_get_stop_and_rollback_watchdogs(self):
        actions = {w.watchdog_id: w.action for w in self.capped.watchdogs}
        self.assertEqual(
            actions["wd/QoSTarget/objective-ue-throughput-floor"],
            WatchdogAction.STOP_AND_ROLLBACK)
        self.assertEqual(
            actions["wd/QoSTarget/controlled-ue-cap"],
            WatchdogAction.STOP_AND_ROLLBACK)

    def test_the_controlled_ue_debt_is_charged_to_its_own_scope(self):
        bound = next(b for b in self.capped.harm.bounds
                     if b.bound_id.endswith("controlled-ue-cap"))
        self.assertEqual(dict(bound.operating_scope), {"controlledUeId": "132"})
        self.assertTrue(bound.calibration_records)
        self.assertTrue(bound.proof_ref)
        # Not the objective UE's scope: the two debts cannot hide in each other.
        self.assertNotEqual(dict(bound.operating_scope),
                            dict(self.bundle.harm.scope_selector))

    def test_the_plan_scope_carries_the_controlled_ue_in_its_own_key(self):
        scope = dict(self.capped.scope)
        self.assertEqual(scope[CONTROLLED_UE_SCOPE_KEY]["ueId"], "132")
        self.assertEqual(scope["ueId"], "131")
        self.assertEqual(scope["controlledUeId"], "132")

    def test_the_cap_measurements_share_the_family_evidence_grid(self):
        cadence = {m.cadence_ms for m in self.bundle.measurements}
        added = [m for m in self.capped.measurements
                 if m not in self.bundle.measurements]
        self.assertEqual(len(added), 3)
        for measurement in added:
            with self.subTest(measurement=measurement.contract_id):
                self.assertEqual(measurement.cadence_ms, max(cadence))

    def test_capping_the_objective_ue_is_refused(self):
        with self.assertRaisesRegex(SupplementaryCapError, "different, heavy"):
            with_supplementary_cap(self.bundle, _request(controlled="131"))

    def test_a_family_that_may_not_carry_a_cap_is_refused(self):
        with self.assertRaisesRegex(SupplementaryCapError, "may not carry"):
            with_supplementary_cap(_bundle("TrafficSteeringPreference"), _request())

    def test_two_caps_on_one_composition_are_refused(self):
        with self.assertRaisesRegex(SupplementaryCapError, "already carries"):
            with_supplementary_cap(self.capped, _request(controlled="133"))

    def test_a_ue_level_target_can_carry_the_same_cap(self):
        capped = with_supplementary_cap(_bundle("UELevelTarget"), _request())
        self.assertEqual(dict(capped.baseline_config)[CAP_AXIS], cap_axis_baseline())
        self.assertTrue(any(p.predicate_id.endswith("objective-ue-throughput-floor")
                            for p in capped.target.predicates))


class CalibrationTests(unittest.TestCase):

    def test_a_request_without_calibration_is_refused(self):
        with self.assertRaisesRegex(SupplementaryCapError, "calibration"):
            SupplementaryCapRequest(
                controlled_ue={"cellId": "1", "ueId": "132"}, candidate_caps=(12,),
                objective_throughput_floor_kbps=500.0,
                controlled_harm_reserve_kbps=1500.0,
                calibration_ref="", controlled_ue_scope_id="132")

    def test_a_non_positive_floor_is_refused(self):
        with self.assertRaisesRegex(SupplementaryCapError, "positive calibration"):
            SupplementaryCapRequest(
                controlled_ue={"cellId": "1", "ueId": "132"}, candidate_caps=(12,),
                objective_throughput_floor_kbps=0.0,
                controlled_harm_reserve_kbps=1500.0,
                calibration_ref="c", controlled_ue_scope_id="132")

    def test_the_declared_adapter_is_the_one_the_axis_is_written_through(self):
        self.assertEqual(CAP_ADAPTER_KEY, "r1-cap")


if __name__ == "__main__":
    unittest.main()
