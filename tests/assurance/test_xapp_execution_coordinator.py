"""XApp Execution Coordinator: mapping, conflicts, ordering, refusals."""
import unittest

from assurance.xapps import (
    CoordinationOutcome, RecentExecution, XAppRuntimeStatus,
)
from assurance.xapps.execution import PreconditionKind

from tests.assurance.xapp_support import (
    NOW, SOURCE_CELL, TAKEN, TARGET_CELL, TARGET_UE, attribution_snapshot,
    cap_action, coordination_runtime, live_status, power_action,
    priority_action, steer_action,
)


def _steer_priority_set(runtime, snapshot, *, serving_cell=SOURCE_CELL):
    return runtime.composition_coordinator.recommend(
        objective_family="QoSTarget",
        proposals=[steer_action(), priority_action(serving_cell=serving_cell)],
        snapshot=snapshot, now=NOW)


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.runtime = coordination_runtime()
        self.snapshot = attribution_snapshot()

    def test_actions_map_to_their_owning_xapps(self):
        candidate_set = _steer_priority_set(self.runtime, self.snapshot)
        result = self.runtime.execution_coordinator.plan([candidate_set],
                                                         now=NOW)
        self.assertIs(result.outcome, CoordinationOutcome.PLANNED)
        by_action = {step.action_id: step.xapp_id
                     for step in result.plan.steps}
        self.assertEqual(by_action["cell-steering"], "xapp/traffic-steering")
        self.assertEqual(by_action["scheduler-priority"], "xapp/ue-scheduler")

    def test_parameters_pass_through_unchanged(self):
        candidate_set = _steer_priority_set(self.runtime, self.snapshot)
        result = self.runtime.execution_coordinator.plan([candidate_set],
                                                         now=NOW)
        for step in result.plan.steps:
            original = candidate_set.action_for(step.action_id)
            self.assertEqual(dict(step.parameters), dict(original.parameters))
            self.assertEqual(dict(step.target_selector),
                             dict(original.target_selector))

    def test_the_coordinator_has_no_parameter_mutation_surface(self):
        import inspect
        from assurance.xapps.execution import XAppExecutionCoordinator
        for name, _ in inspect.getmembers(XAppExecutionCoordinator,
                                          inspect.isfunction):
            self.assertNotIn("mutate", name)
            self.assertNotIn("rewrite", name)
            self.assertNotIn("adjust", name)


class UnavailableXAppTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = attribution_snapshot()

    def test_not_wired_when_the_owner_has_no_e2_wire(self):
        runtime = coordination_runtime(wire_live=("xapp/traffic-steering",))
        runtime.registry.update_runtime_status(
            live_status("xapp/ue-scheduler", e2_connected=False))
        candidate_set = _steer_priority_set(runtime, self.snapshot)
        result = runtime.execution_coordinator.plan([candidate_set], now=NOW)
        self.assertIs(result.outcome, CoordinationOutcome.NOT_WIRED)
        self.assertIsNone(result.plan)
        self.assertTrue(result.replan_required)
        refused = {r.action_id for r in result.refusals}
        self.assertIn("scheduler-priority", refused)

    def test_unsupported_capability_when_the_owner_is_outside_the_live_set(self):
        runtime = coordination_runtime()
        power_set = runtime.composition_coordinator \
            .declare_standalone_action_set(
                action=power_action(cell="99999999"),
                basis="lab power management", snapshot=self.snapshot, now=NOW)
        # Rebuild a runtime whose live set excludes Cell Power.
        from assurance.xapps import XAppCapabilityRegistry, XAppKind, \
            default_capability_manifests
        from assurance.xapps.execution import XAppExecutionCoordinator
        from tests.assurance.xapp_support import deployment
        registry = XAppCapabilityRegistry(
            deployment=deployment(),
            coordinated_live_set=(XAppKind.TRAFFIC_STEERING,))
        for manifest in default_capability_manifests(deployment()):
            registry.register(manifest)
        coordinator = XAppExecutionCoordinator(registry=registry)
        result = coordinator.plan([power_set], now=NOW)
        self.assertIs(result.outcome,
                      CoordinationOutcome.UNSUPPORTED_CAPABILITY)
        self.assertIsNone(result.plan)


class HandoverSchedulerConflictTests(unittest.TestCase):
    """The mandated conflict: steer UE1 off Cell 1 + raise its PF weight on
    Cell 1.  The two xApps must not run simultaneously; the handover runs
    first and the source-cell scheduler step is invalidated."""

    def setUp(self):
        self.runtime = coordination_runtime()
        self.snapshot = attribution_snapshot()
        self.candidate_set = _steer_priority_set(self.runtime, self.snapshot)
        self.result = self.runtime.execution_coordinator.plan(
            [self.candidate_set], now=NOW)
        self.plan = self.result.plan

    def test_the_handover_runs_first_and_the_steps_never_share_a_priority(self):
        steer = self.plan.step_for("cell-steering")
        scheduler = self.plan.step_for("scheduler-priority")
        self.assertLess(steer.execution_priority,
                        scheduler.execution_priority)

    def test_the_handover_step_requires_readback_and_fresh_attribution(self):
        steer = self.plan.step_for("cell-steering")
        self.assertIn("UE.ServingCell", steer.post_readback_measurements)
        self.assertIn("new serving cell and RNTI", steer.proceed_condition)
        self.assertIn("new common KPI snapshot".replace("KPI ", "KPI "),
                      steer.proceed_condition)

    def test_the_scheduler_step_re_verifies_cell_rnti_and_snapshot(self):
        scheduler = self.plan.step_for("scheduler-priority")
        kinds = {p.kind for p in scheduler.preconditions}
        self.assertIn(PreconditionKind.PRIMARY_READBACK_CONFIRMED, kinds)
        self.assertIn(PreconditionKind.SERVING_CELL_REVERIFIED, kinds)
        self.assertIn(PreconditionKind.RNTI_REVERIFIED, kinds)
        self.assertIn(PreconditionKind.FRESH_SNAPSHOT_REQUIRED, kinds)

    def test_the_source_cell_step_is_invalidated_toward_replan(self):
        steer = self.plan.step_for("cell-steering")
        scheduler = self.plan.step_for("scheduler-priority")
        self.assertEqual(scheduler.revalidation, "REPLAN_REQUIRED")
        self.assertIn(scheduler.step_id, steer.invalidates_step_ids)
        self.assertTrue(self.result.replan_required)
        refused = {(r.action_id, r.outcome) for r in self.result.refusals}
        self.assertIn(("scheduler-priority", CoordinationOutcome.REPLAN_REQUIRED),
                      refused)

    def test_a_cell_agnostic_scheduler_step_revalidates_instead(self):
        candidate_set = _steer_priority_set(self.runtime, self.snapshot,
                                            serving_cell=None)
        result = self.runtime.execution_coordinator.plan([candidate_set],
                                                         now=NOW)
        scheduler = result.plan.step_for("scheduler-priority")
        self.assertEqual(scheduler.revalidation,
                         "REVALIDATE_AGAINST_TARGET_CELL")
        self.assertFalse(result.replan_required)


class HandoverPowerConflictTests(unittest.TestCase):
    """The mandated conflict: steer UE1 to Cell 2 + raise Cell 2's TX
    attenuation.  The power change can invalidate the state the handover was
    judged on, so it is held behind readback and fresh KPI."""

    def setUp(self):
        self.runtime = coordination_runtime()
        self.snapshot = attribution_snapshot()
        self.steer_set = self.runtime.composition_coordinator.recommend(
            objective_family="TrafficSteeringPreference",
            proposals=[steer_action()], snapshot=self.snapshot, now=NOW)
        self.power_set = self.runtime.composition_coordinator \
            .declare_standalone_action_set(
                action=power_action(cell=TARGET_CELL),
                basis="lab power management", snapshot=self.snapshot, now=NOW)
        self.result = self.runtime.execution_coordinator.plan(
            [self.steer_set, self.power_set], now=NOW)

    def test_the_power_step_is_held_behind_the_handover(self):
        steer = self.result.plan.step_for("cell-steering")
        power = self.result.plan.step_for("dl-rf-attenuation")
        self.assertLess(steer.execution_priority, power.execution_priority)
        self.assertTrue(power.held)
        kinds = {p.kind for p in power.preconditions}
        self.assertIn(PreconditionKind.PRIMARY_READBACK_CONFIRMED, kinds)
        self.assertIn(PreconditionKind.FRESH_SNAPSHOT_REQUIRED, kinds)

    def test_the_hold_is_recorded_as_a_safety_block(self):
        refused = {(r.action_id, r.outcome) for r in self.result.refusals}
        self.assertIn(("dl-rf-attenuation", CoordinationOutcome.BLOCKED_BY_SAFETY),
                      refused)

    def test_a_power_step_on_an_uninvolved_cell_is_not_held(self):
        other = self.runtime.composition_coordinator \
            .declare_standalone_action_set(
                action=power_action(cell="99999999"),
                basis="lab power management", snapshot=self.snapshot, now=NOW)
        result = self.runtime.execution_coordinator.plan(
            [self.steer_set, other], now=NOW)
        power = result.plan.step_for("dl-rf-attenuation")
        self.assertFalse(power.held)

    def test_the_power_plan_demands_the_affected_ue_range_when_unmeasured(self):
        power = self.result.plan.step_for("dl-rf-attenuation")
        self.assertEqual(power.affected_scope, ())
        fresh_snapshot = {(p.kind, p.subject) for p in power.preconditions}
        self.assertTrue(any(kind is PreconditionKind.FRESH_SNAPSHOT_REQUIRED
                            for kind, _ in fresh_snapshot))


class NoFixedXAppPriorityTests(unittest.TestCase):
    """Priority attaches to the (xApp, concrete Action) step, never to the
    xApp.  Every specialist runs at priority 1 when its Action has no
    dependency -- so no global rank like 'Traffic Steering always first'
    exists."""

    def setUp(self):
        self.runtime = coordination_runtime()
        self.snapshot = attribution_snapshot()

    def test_each_xapp_runs_first_when_its_action_is_independent(self):
        steering_only = self.runtime.composition_coordinator.recommend(
            objective_family="TrafficSteeringPreference",
            proposals=[steer_action()], snapshot=self.snapshot, now=NOW)
        power_only = self.runtime.composition_coordinator \
            .declare_standalone_action_set(
                action=power_action(cell="99999999"),
                basis="lab power management", snapshot=self.snapshot, now=NOW)
        for candidate_set, xapp_id in ((steering_only, "xapp/traffic-steering"),
                                       (power_only, "xapp/cell-power")):
            with self.subTest(xapp=xapp_id):
                result = self.runtime.execution_coordinator.plan(
                    [candidate_set], now=NOW)
                step = result.plan.steps[0]
                self.assertEqual(step.xapp_id, xapp_id)
                self.assertEqual(step.execution_priority, 1)

    def test_no_module_constant_ranks_xapps(self):
        import pathlib
        import assurance.xapps.execution as module
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("XAPP_PRIORITY", source)
        self.assertNotIn("FIXED_PRIORITY", source)


class StaleAndPolicyRefusalTests(unittest.TestCase):
    def setUp(self):
        self.runtime = coordination_runtime()
        self.snapshot = attribution_snapshot()
        self.candidate_set = _steer_priority_set(self.runtime, self.snapshot)

    def test_stale_snapshot_returns_stale_snapshot_not_a_plan(self):
        result = self.runtime.execution_coordinator.plan(
            [self.candidate_set], now="2026-08-31T10:05:00.000000Z")
        self.assertIs(result.outcome, CoordinationOutcome.STALE_SNAPSHOT)
        self.assertIsNone(result.plan)
        self.assertTrue(result.replan_required)

    def test_snapshot_older_than_an_approved_write_is_stale(self):
        recent = RecentExecution(
            action_id="scheduler-priority",
            target_key=f"objectiveUeId={TARGET_UE};rnti=8738",
            applied_parameters={"rnti": 0x2222, "pfWeight": 1.5},
            previous_parameters={"rnti": 0x2222, "pfWeight": 1.0},
            completed_at="2026-08-31T10:00:00.500000Z")
        result = self.runtime.execution_coordinator.plan(
            [self.candidate_set], now=NOW, recent_executions=[recent])
        self.assertIs(result.outcome, CoordinationOutcome.STALE_SNAPSHOT)
        self.assertIn("revert", result.refusals[0].reason)

    def test_an_opposite_action_inside_cooldown_is_blocked_by_policy(self):
        fresh_snapshot = attribution_snapshot(
            snapshot_id="snap/2", taken_at="2026-08-31T10:00:00.900000Z")
        candidate_set = self.runtime.composition_coordinator.recommend(
            objective_family="QoSTarget",
            proposals=[steer_action(),
                       priority_action(pf_weight=0.5)],
            snapshot=fresh_snapshot, now=NOW)
        step = candidate_set.action_for("scheduler-priority")
        from assurance.xapps.execution import XAppExecutionCoordinator
        target_key = XAppExecutionCoordinator._target_key(step)
        recent = RecentExecution(
            action_id="scheduler-priority", target_key=target_key,
            applied_parameters={"rnti": 0x2222, "pfWeight": 2.0},
            previous_parameters={"rnti": 0x2222, "pfWeight": 1.0},
            completed_at="2026-08-31T10:00:00.800000Z")
        result = self.runtime.execution_coordinator.plan(
            [candidate_set], now=NOW, recent_executions=[recent])
        self.assertIs(result.outcome, CoordinationOutcome.BLOCKED_BY_POLICY)
        self.assertIn("cooldown", result.refusals[0].reason)

    def test_cross_set_contradiction_fails_closed_to_replan(self):
        set_a = self.runtime.composition_coordinator \
            .declare_standalone_action_set(
                action=power_action(cell="99999999", attenuation_db=3.0),
                basis="lab a", snapshot=self.snapshot, now=NOW)
        set_b = self.runtime.composition_coordinator \
            .declare_standalone_action_set(
                action=power_action(cell="99999999", attenuation_db=9.0),
                basis="lab b", snapshot=self.snapshot, now=NOW)
        result = self.runtime.execution_coordinator.plan([set_a, set_b],
                                                         now=NOW)
        self.assertIs(result.outcome, CoordinationOutcome.REPLAN_REQUIRED)
        self.assertIsNone(result.plan)
        self.assertIn("fail closed", result.refusals[0].reason)


class PlanShapeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = coordination_runtime()
        self.snapshot = attribution_snapshot()
        self.candidate_set = _steer_priority_set(self.runtime, self.snapshot)
        self.plan = self.runtime.execution_coordinator.plan(
            [self.candidate_set], now=NOW).plan

    def test_rollback_order_is_the_reverse_of_the_steps(self):
        self.assertEqual(self.plan.rollback_order,
                         tuple(step.step_id
                               for step in reversed(self.plan.steps)))

    def test_the_plan_carries_harm_watchdog_permit_and_admission_facts(self):
        self.assertTrue(self.plan.combined_harm_refs)
        self.assertTrue(self.plan.watchdog_refs)
        self.assertTrue(self.plan.abort_conditions)
        self.assertIn("Kernel admission", self.plan.admission_note)
        for step in self.plan.steps:
            self.assertEqual(step.required_permit_kinds,
                             ("PREPARE", "COMMIT"))
        self.assertGreater(self.plan.deadline, self.plan.created_at)
        self.assertEqual(self.plan.snapshot_ids,
                         (self.snapshot.snapshot_id,))


if __name__ == "__main__":
    unittest.main()
