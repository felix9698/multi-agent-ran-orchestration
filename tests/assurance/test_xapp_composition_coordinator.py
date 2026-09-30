"""Action Composition Coordinator: recommend, refuse, replan."""
import unittest

from assurance.actions.composition_policy import OBJECTIVE_ACTION_POLICIES
from assurance.advisors.action_space import (
    AdvisoryAction, ForbiddenCombinationError,
)
from assurance.core.provenance import (
    DocumentStatus, Provenance, TypedQuantity,
)
from assurance.objectives import record_for
from assurance.xapps import (
    CommonKpiSnapshot, ContradictoryActionError, PROPOSAL_VALUE_BOUNDARY,
    StaleSnapshotError, snapshot_from_samples,
)
from assurance.xapps.composition import (
    ActionCompositionCoordinator, CompositionCoordinatorError,
)

from tests.assurance.xapp_support import (
    NOW, TAKEN, attribution_snapshot, cap_action, deployment, power_action,
    priority_action, raw_sample, steer_action,
)


def _coordinator(**overrides):
    values = dict(deployment=deployment())
    values.update(overrides)
    return ActionCompositionCoordinator(**values)


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = _coordinator()
        self.snapshot = attribution_snapshot()

    def test_every_allowed_combination_of_a_submittable_family_recommends(self):
        fixtures = {
            "cell-steering": steer_action(),
            "scheduler-priority": priority_action(serving_cell=None),
            "ue-dl-prb-cap": cap_action(),
        }
        for family in ("QoSTarget", "UELevelTarget", "QoSandTSP"):
            policy = OBJECTIVE_ACTION_POLICIES[family]
            for combination in policy.allowed_combinations:
                with self.subTest(family=family, combination=combination):
                    proposals = [fixtures[a] for a in combination]
                    candidate_set = self.coordinator.recommend(
                        objective_family=family, proposals=proposals,
                        snapshot=self.snapshot, now=NOW,
                        for_live=record_for(family)
                        .deployment_capability.submittable)
                    self.assertEqual(candidate_set.apply_order, combination)
                    self.assertEqual(candidate_set.rollback_order,
                                     tuple(reversed(combination)))
                    self.assertEqual(candidate_set.snapshot_id,
                                     self.snapshot.snapshot_id)

    def test_the_set_records_snapshot_expiry_and_the_value_boundary(self):
        candidate_set = self.coordinator.recommend(
            objective_family="TrafficSteeringPreference",
            proposals=[steer_action()], snapshot=self.snapshot, now=NOW)
        self.assertEqual(candidate_set.snapshot_hash,
                         self.snapshot.content_hash())
        self.assertEqual(candidate_set.snapshot_taken_at, TAKEN)
        self.assertGreater(candidate_set.expires_at, candidate_set.created_at)
        self.assertEqual(candidate_set.provenance_note, PROPOSAL_VALUE_BOUNDARY)
        self.assertEqual(candidate_set.primary_action_id, "cell-steering")

    def test_family_policy_violations_still_refuse_through_reuse(self):
        """The frozen composition policy is reused, not reimplemented."""
        with self.assertRaises(ForbiddenCombinationError):
            self.coordinator.recommend(
                objective_family="QoSTarget",
                proposals=[power_action()], snapshot=self.snapshot, now=NOW)

    def test_missing_current_value_becomes_a_reread_precondition(self):
        candidate_set = self.coordinator.recommend(
            objective_family="TrafficSteeringPreference",
            proposals=[steer_action()], snapshot=self.snapshot, now=NOW)
        action = candidate_set.action_for("cell-steering")
        self.assertEqual(dict(action.current_values), {})
        self.assertTrue(any("CONFIGURATION_REREAD" in p
                            for p in action.preconditions))

    def test_snapshot_current_value_is_bound_when_delivered(self):
        readback = raw_sample(
            "s-cap", "counter/action-space/ue-dl-prb-cap/readback", 12, "PRB",
            {"scope": "UE", "rnti": "0x1111"})
        snapshot = attribution_snapshot(extra_samples=(readback,))
        candidate_set = self.coordinator.recommend(
            objective_family="QoSTarget",
            proposals=[steer_action(), cap_action()], snapshot=snapshot,
            now=NOW)
        action = candidate_set.action_for("ue-dl-prb-cap")
        current = action.current_values[
            "counter/action-space/ue-dl-prb-cap/readback"]
        self.assertEqual(current["value"], 12)


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = _coordinator()
        self.snapshot = attribution_snapshot()

    def test_stale_snapshot_is_refused(self):
        with self.assertRaisesRegex(StaleSnapshotError, "stale"):
            self.coordinator.recommend(
                objective_family="TrafficSteeringPreference",
                proposals=[steer_action()], snapshot=self.snapshot,
                now="2026-08-31T10:01:00.000000Z")

    def test_future_dated_snapshot_is_refused(self):
        with self.assertRaises(StaleSnapshotError):
            self.coordinator.recommend(
                objective_family="TrafficSteeringPreference",
                proposals=[steer_action()], snapshot=self.snapshot,
                now="2026-08-31T09:59:00.000000Z")

    def test_contradictory_proposals_are_detected_at_composition_stage(self):
        with self.assertRaisesRegex(ContradictoryActionError, "different values"):
            self.coordinator.recommend(
                objective_family="QoSTarget",
                proposals=[steer_action(),
                           priority_action(pf_weight=2.0),
                           priority_action(pf_weight=0.5)],
                snapshot=self.snapshot, now=NOW)

    def test_expected_impacts_must_be_draft(self):
        admissible = TypedQuantity(1.5, "Mbps", Provenance.EXPERIMENT_CONFIG,
                                   "test/impact")
        with self.assertRaisesRegex(CompositionCoordinatorError, "DRAFT"):
            self.coordinator.recommend(
                objective_family="TrafficSteeringPreference",
                proposals=[steer_action()], snapshot=self.snapshot, now=NOW,
                expected_impacts={"cell-steering": (admissible,)})
        draft = TypedQuantity(1.5, "Mbps", Provenance.EXPERIMENT_CONFIG,
                              "test/impact",
                              document_status=DocumentStatus.DRAFT)
        candidate_set = self.coordinator.recommend(
            objective_family="TrafficSteeringPreference",
            proposals=[steer_action()], snapshot=self.snapshot, now=NOW,
            expected_impacts={"cell-steering": (draft,)})
        impact = candidate_set.action_for("cell-steering").expected_kpi_impact
        self.assertEqual(impact, (draft,))

    def test_snapshot_must_come_from_collector_samples(self):
        with self.assertRaises(CompositionCoordinatorError):
            self.coordinator.recommend(
                objective_family="TrafficSteeringPreference",
                proposals=[steer_action()], snapshot={"fake": "kpi"}, now=NOW)


class StandaloneDeclarationTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = _coordinator()
        self.snapshot = attribution_snapshot()

    def test_power_action_requires_an_explicit_declared_basis(self):
        with self.assertRaisesRegex(CompositionCoordinatorError, "basis"):
            self.coordinator.declare_standalone_action_set(
                action=power_action(), basis="  ", snapshot=self.snapshot,
                now=NOW)

    def test_declared_power_set_is_labelled_standalone_not_policy(self):
        candidate_set = self.coordinator.declare_standalone_action_set(
            action=power_action(), basis="lab power management",
            snapshot=self.snapshot, now=NOW)
        self.assertEqual(candidate_set.objective_family,
                         "StandaloneDeclaredAction")
        self.assertTrue(candidate_set.policy_ref.startswith("standalone:"))
        self.assertEqual(candidate_set.apply_order, ("dl-rf-attenuation",))

    def test_tier_b_actions_cannot_be_declared_standalone(self):
        tier_b = AdvisoryAction("drb-qos",
                                {"qfi": 1, "drbId": 1, "fiveQi": 9})
        with self.assertRaisesRegex(CompositionCoordinatorError, "Tier B"):
            self.coordinator.declare_standalone_action_set(
                action=tier_b, basis="test", snapshot=self.snapshot, now=NOW)


class ReplanTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = _coordinator()
        self.snapshot = attribution_snapshot()

    def test_replan_records_lineage_and_needs_a_fresh_snapshot(self):
        original = self.coordinator.recommend(
            objective_family="QoSTarget",
            proposals=[steer_action(), priority_action()],
            snapshot=self.snapshot, now=NOW)
        with self.assertRaisesRegex(StaleSnapshotError, "fresh"):
            self.coordinator.replan(
                previous=original, reason="REPLAN_REQUIRED",
                objective_family="QoSTarget",
                proposals=[steer_action(), priority_action()],
                snapshot=self.snapshot, now=NOW)
        post_handover = attribution_snapshot(
            snapshot_id="snap/post-handover",
            taken_at="2026-08-31T10:00:02.000000Z",
            target_cell_of_target_ue="87654321", target_rnti=0x3333)
        replanned = self.coordinator.replan(
            previous=original,
            reason="REPLAN_REQUIRED: source-cell step invalidated by handover",
            objective_family="QoSTarget",
            proposals=[steer_action(),
                       priority_action(serving_cell="87654321", rnti=0x3333)],
            snapshot=post_handover, now="2026-08-31T10:00:03.000000Z")
        self.assertEqual(replanned.replanned_from, original.set_id)
        self.assertIn("REPLAN_REQUIRED", replanned.replan_reason)
        self.assertEqual(replanned.snapshot_id, "snap/post-handover")


class NoWriteAuthorityTests(unittest.TestCase):
    def test_the_composition_module_never_touches_the_gateway(self):
        import pathlib
        import assurance.xapps.composition as module
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("assurance.gateway", "telnetlib", "socket",
                          "requests", "urllib"):
            self.assertNotIn(f"import {forbidden}", source)
            self.assertNotIn(f"from {forbidden}", source)


if __name__ == "__main__":
    unittest.main()
