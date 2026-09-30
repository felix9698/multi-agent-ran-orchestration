"""Specialist xApp executors: assignments, permits, staleness, rollback."""
import unittest

from assurance.gateway.token import TokenKind
from assurance.xapps import (
    CellPowerXApp, HardwareFreeConfigStore, PermitRequiredError,
    TrafficSteeringXApp, UeSchedulerXApp, XAppExecutionStatus,
    assignment_for_step, default_capability_manifests,
)
from assurance.xapps.assignment import (
    FORBIDDEN_REPORT_FIELDS, XAppExecutionAssignment, XAppExecutionReport,
)

from tests.assurance.xapp_support import (
    NOW, SOURCE_CELL, TARGET_CELL, TARGET_UE, attribution_snapshot,
    coordination_runtime, deployment, permit, priority_action, steer_action,
)


def _manifests():
    return {m.xapp_id: m for m in default_capability_manifests(deployment())}


def _assignment(action_id, xapp_id, parameters, selector, snapshot,
                *, the_permit):
    return XAppExecutionAssignment(
        assignment_id=f"assignment/test/{action_id}",
        plan_id="xapp-plan/test", step_id=f"step/test/{action_id}",
        xapp_id=xapp_id, action_id=action_id, parameters=parameters,
        target_selector=selector, snapshot_id=snapshot.snapshot_id,
        snapshot_hash=snapshot.content_hash(), preconditions=(),
        deadline="2026-08-31T10:05:00.000000Z",
        permit_ref=the_permit.content_hash())


class PermitBoundaryTests(unittest.TestCase):
    """No permit, no write: the single-writer invariant at this layer."""

    def setUp(self):
        manifests = _manifests()
        self.store = HardwareFreeConfigStore()
        self.executor = UeSchedulerXApp(
            manifest=manifests["xapp/ue-scheduler"], store=self.store)
        self.snapshot = attribution_snapshot()
        self.permit = permit()
        self.assignment = _assignment(
            "scheduler-priority", "xapp/ue-scheduler",
            {"rnti": 0x2222, "pfWeight": 2.0},
            {"objectiveUeId": TARGET_UE, "servingCellId": SOURCE_CELL},
            self.snapshot, the_permit=self.permit)

    def test_a_write_without_a_permit_is_blocked_before_any_state_change(self):
        before = self.store.as_dict()
        with self.assertRaisesRegex(PermitRequiredError, "blocked"):
            self.executor.execute(self.assignment, permit=None,
                                  snapshot=self.snapshot, now=NOW)
        self.assertEqual(self.store.as_dict(), before)

    def test_a_wrong_kind_permit_is_refused(self):
        with self.assertRaisesRegex(PermitRequiredError, "PREPARE"):
            self.executor.execute(
                self.assignment, permit=permit(kind=TokenKind.PREPARE),
                snapshot=self.snapshot, now=NOW)

    def test_an_expired_permit_is_refused(self):
        expired = permit(lease_expiry="2026-08-31T10:00:00.500000Z")
        stale_assignment = _assignment(
            "scheduler-priority", "xapp/ue-scheduler",
            {"rnti": 0x2222, "pfWeight": 2.0},
            {"objectiveUeId": TARGET_UE, "servingCellId": SOURCE_CELL},
            self.snapshot, the_permit=expired)
        with self.assertRaisesRegex(PermitRequiredError, "expired"):
            self.executor.execute(stale_assignment, permit=expired,
                                  snapshot=self.snapshot, now=NOW)

    def test_a_permit_for_a_different_assignment_is_refused(self):
        other = permit(sequence=7)
        with self.assertRaisesRegex(PermitRequiredError, "not the one"):
            self.executor.execute(self.assignment, permit=other,
                                  snapshot=self.snapshot, now=NOW)


class CapabilityBoundaryTests(unittest.TestCase):
    def setUp(self):
        manifests = _manifests()
        self.snapshot = attribution_snapshot()
        self.permit = permit()
        self.executors = {
            "xapp/traffic-steering": TrafficSteeringXApp(
                manifest=manifests["xapp/traffic-steering"],
                store=HardwareFreeConfigStore()),
            "xapp/ue-scheduler": UeSchedulerXApp(
                manifest=manifests["xapp/ue-scheduler"],
                store=HardwareFreeConfigStore()),
            "xapp/cell-power": CellPowerXApp(
                manifest=manifests["xapp/cell-power"],
                store=HardwareFreeConfigStore()),
        }

    def _foreign(self, xapp_id, action_id):
        assignment = _assignment(action_id, xapp_id, {"any": 1}, {},
                                 self.snapshot, the_permit=self.permit)
        return self.executors[xapp_id].execute(
            assignment, permit=self.permit, snapshot=self.snapshot, now=NOW)

    def test_traffic_steering_rejects_everything_but_steer(self):
        for foreign in ("ue-dl-prb-cap", "scheduler-priority",
                        "dl-rf-attenuation"):
            with self.subTest(action=foreign):
                report = self._foreign("xapp/traffic-steering", foreign)
                self.assertIs(report.status,
                              XAppExecutionStatus.REJECTED_CAPABILITY)

    def test_ue_scheduler_rejects_steer_and_rfatt(self):
        for foreign in ("cell-steering", "dl-rf-attenuation"):
            with self.subTest(action=foreign):
                report = self._foreign("xapp/ue-scheduler", foreign)
                self.assertIs(report.status,
                              XAppExecutionStatus.REJECTED_CAPABILITY)

    def test_cell_power_rejects_steer_cap_and_priority(self):
        for foreign in ("cell-steering", "ue-dl-prb-cap",
                        "scheduler-priority"):
            with self.subTest(action=foreign):
                report = self._foreign("xapp/cell-power", foreign)
                self.assertIs(report.status,
                              XAppExecutionStatus.REJECTED_CAPABILITY)


class UeSchedulerStalenessTests(unittest.TestCase):
    def setUp(self):
        manifests = _manifests()
        self.store = HardwareFreeConfigStore()
        self.executor = UeSchedulerXApp(
            manifest=manifests["xapp/ue-scheduler"], store=self.store)
        self.permit = permit()

    def _execute(self, snapshot, *, selector_cell=SOURCE_CELL, rnti=0x2222):
        assignment = _assignment(
            "scheduler-priority", "xapp/ue-scheduler",
            {"rnti": rnti, "pfWeight": 2.0},
            {"objectiveUeId": TARGET_UE, "servingCellId": selector_cell},
            snapshot, the_permit=self.permit)
        return self.executor.execute(assignment, permit=self.permit,
                                     snapshot=snapshot, now=NOW)

    def test_success_when_cell_and_rnti_still_match(self):
        report = self._execute(attribution_snapshot())
        self.assertIs(report.status, XAppExecutionStatus.SUCCEEDED)
        self.assertEqual(self.store.read("ue/0x2222/pfWeight"), 2.0)

    def test_rnti_change_after_handover_is_stale_state(self):
        moved = attribution_snapshot(
            snapshot_id="snap/moved",
            target_cell_of_target_ue=TARGET_CELL, target_rnti=0x3333)
        report = self._execute(moved, selector_cell=SOURCE_CELL)
        self.assertIs(report.status, XAppExecutionStatus.REJECTED_STALE_STATE)
        self.assertIn("no longer served", report.detail)
        report_rnti = self._execute(
            attribution_snapshot(snapshot_id="snap/rnti",
                                 target_rnti=0x3333))
        self.assertIs(report_rnti.status,
                      XAppExecutionStatus.REJECTED_STALE_STATE)
        self.assertIn("RNTI changed", report_rnti.detail)

    def test_a_snapshot_other_than_the_planned_one_is_stale(self):
        planned = attribution_snapshot()
        other = attribution_snapshot(snapshot_id="snap/other")
        assignment = _assignment(
            "scheduler-priority", "xapp/ue-scheduler",
            {"rnti": 0x2222, "pfWeight": 2.0},
            {"objectiveUeId": TARGET_UE, "servingCellId": SOURCE_CELL},
            planned, the_permit=self.permit)
        report = self.executor.execute(assignment, permit=self.permit,
                                       snapshot=other, now=NOW)
        self.assertIs(report.status, XAppExecutionStatus.REJECTED_STALE_STATE)


class CellPowerTests(unittest.TestCase):
    def setUp(self):
        manifests = _manifests()
        self.store = HardwareFreeConfigStore(
            {f"cell/{TARGET_CELL}/txAttenuationDb": 3.0})
        self.executor = CellPowerXApp(
            manifest=manifests["xapp/cell-power"], store=self.store)
        self.permit = permit()
        self.snapshot = attribution_snapshot(
            target_cell_of_target_ue=TARGET_CELL)

    def test_success_carries_baseline_direction_and_affected_ues(self):
        assignment = _assignment(
            "dl-rf-attenuation", "xapp/cell-power",
            {"txAttenuationDb": 9.0}, {"cellId": TARGET_CELL},
            self.snapshot, the_permit=self.permit)
        report = self.executor.execute(assignment, permit=self.permit,
                                       snapshot=self.snapshot, now=NOW)
        self.assertIs(report.status, XAppExecutionStatus.SUCCEEDED)
        self.assertEqual(
            report.readback["rollbackBaseline"],
            {f"cell/{TARGET_CELL}/txAttenuationDb": 3.0})
        self.assertEqual(report.readback["affectedActiveUeIds"], [TARGET_UE])
        self.assertIn("lowers DL transmit power",
                      report.readback["powerDirectionNote"])

    def test_rollback_restores_the_baseline(self):
        assignment = _assignment(
            "dl-rf-attenuation", "xapp/cell-power",
            {"txAttenuationDb": 9.0}, {"cellId": TARGET_CELL},
            self.snapshot, the_permit=self.permit)
        self.executor.execute(assignment, permit=self.permit,
                              snapshot=self.snapshot, now=NOW)
        rollback_permit = permit(kind=TokenKind.REVERSE_ROLLBACK)
        report = self.executor.rollback(assignment, permit=rollback_permit,
                                        now=NOW)
        self.assertIs(report.status, XAppExecutionStatus.ROLLBACK_SUCCEEDED)
        self.assertEqual(
            self.store.read(f"cell/{TARGET_CELL}/txAttenuationDb"), 3.0)


class PartialFailureReverseRollbackTests(unittest.TestCase):
    """Step 2 fails after step 1 applied: the applied steps are reversed in
    the plan's reverse order and the store returns to its baseline."""

    class _DroppingStore(HardwareFreeConfigStore):
        def __init__(self, *, drop_axis):
            super().__init__()
            self._drop_axis = drop_axis

        def apply(self, axis, value):
            if axis == self._drop_axis and self.read(axis) != value \
                    and not getattr(self, "_rolling_back", False):
                return  # the write is silently lost; readback will disagree
            super().apply(axis, value)

    def test_reverse_rollback_after_a_failed_second_step(self):
        manifests = _manifests()
        runtime = coordination_runtime()
        snapshot = attribution_snapshot()
        candidate_set = runtime.composition_coordinator.recommend(
            objective_family="QoSTarget",
            proposals=[steer_action(), priority_action(serving_cell=None)],
            snapshot=snapshot, now=NOW)
        plan = runtime.execution_coordinator.plan([candidate_set], now=NOW).plan

        drop_axis = "ue/0x2222/pfWeight"
        store = self._DroppingStore(drop_axis=drop_axis)
        executors = {
            "xapp/traffic-steering": TrafficSteeringXApp(
                manifest=manifests["xapp/traffic-steering"], store=store),
            "xapp/ue-scheduler": UeSchedulerXApp(
                manifest=manifests["xapp/ue-scheduler"], store=store),
        }
        the_permit = permit()
        baseline = store.as_dict()
        reports = []
        executed = []
        for step in plan.steps:
            assignment = assignment_for_step(
                plan, step, permit=the_permit,
                snapshot_hash=snapshot.content_hash())
            report = executors[step.xapp_id].execute(
                assignment, permit=the_permit, snapshot=snapshot, now=NOW)
            reports.append(report)
            if report.status is XAppExecutionStatus.SUCCEEDED:
                executed.append((step, assignment))
            else:
                break
        self.assertIs(reports[0].status, XAppExecutionStatus.SUCCEEDED)
        self.assertIs(reports[1].status, XAppExecutionStatus.FAILED)

        store._rolling_back = True
        rollback_permit = permit(kind=TokenKind.REVERSE_ROLLBACK)
        rollback_order = {step_id: index for index, step_id
                          in enumerate(plan.rollback_order)}
        executed.sort(key=lambda pair: rollback_order[pair[0].step_id])
        for step, assignment in executed:
            report = executors[step.xapp_id].rollback(
                assignment, permit=rollback_permit, now=NOW)
            self.assertIs(report.status,
                          XAppExecutionStatus.ROLLBACK_SUCCEEDED)
        self.assertEqual(store.as_dict()[f"ue/{TARGET_UE}/servingCell"],
                         baseline.get(f"ue/{TARGET_UE}/servingCell"))


class ReportSurfaceTests(unittest.TestCase):
    def test_a_report_carries_no_proposal_surface(self):
        from dataclasses import fields
        names = {f.name for f in fields(XAppExecutionReport)}
        self.assertFalse(FORBIDDEN_REPORT_FIELDS & names)
        self.assertIn("status", names)
        self.assertIn("readback", names)

    def test_the_executor_module_opens_no_transport(self):
        import pathlib
        import assurance.xapps.executors as module
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("telnetlib", "socket", "requests", "urllib",
                          "subprocess", "http.client"):
            self.assertNotIn(f"import {forbidden}", source)
            self.assertNotIn(f"from {forbidden}", source)


if __name__ == "__main__":
    unittest.main()
