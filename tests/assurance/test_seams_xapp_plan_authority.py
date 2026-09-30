"""Whose order governs a plan: the owner's answer, pinned from our side.

Ownership authority: ``docs/architecture/SEAMS-GATE2.md`` section 13, verdict
X-06, and ``docs/architecture/XAPP-COORDINATION.md`` section 2.

For everything xApp-related the owner's implementation takes priority over
this repository's pre-existing layers.  Concretely, for order:

* the **XApp Execution Plan** is the authority for execution order, and
  ``plan.rollback_order`` -- the reverse of the plan's own steps -- is the
  authority for rollback;
* the composition policy's ``apply_order`` / ``rollback_order``
  (``assurance/actions/composition_policy.py`` via
  ``assurance/advisors/action_space.py::resolve_composition``) is the
  **composition-level declaration**, a pre-plan default, and does not bind a
  plan;
* when the two differ, the plan wins, and consumers downstream of the plan --
  Kernel admission included -- read the plan.

This file is **ours**, not the owner's.  It exists because our side is the
side that has to yield: a test of ours that asserted the declared order
outranked a plan would be this repository quietly overruling the layer's
designer.  ``assurance/xapps/**`` and ``tests/assurance/test_xapp_*.py`` are
the owner's and are not edited to satisfy anything here.

Hermetic: no hardware, no network, no model client -- it drives the same
in-memory fixtures the owner's tests use.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

from assurance.xapps import CoordinationOutcome, assignment_for_step
from assurance.xapps import execution as execution_module

from tests.assurance.xapp_support import (
    NOW, attribution_snapshot, cap_action, coordination_runtime, permit,
    priority_action, steer_action,
)


def _divergent_case():
    """The composition whose declared order and planned order disagree.

    ``QoSTarget``'s three-action combination is declared
    ``(cell-steering, ue-dl-prb-cap, scheduler-priority)``.  Once the actions
    are paired with their owning xApps, the two scheduler actions both fall
    behind the handover, and the plan orders them by the coordinator's own
    rule -- which puts ``scheduler-priority`` first.  Nothing is wrong with
    either sequence; they answer different questions, and this is the case
    that shows which one a plan follows.
    """
    runtime = coordination_runtime()
    snapshot = attribution_snapshot()
    candidate_set = runtime.composition_coordinator.recommend(
        objective_family="QoSTarget",
        proposals=[steer_action(), cap_action(),
                   priority_action(serving_cell=None)],
        snapshot=snapshot, now=NOW)
    result = runtime.execution_coordinator.plan([candidate_set], now=NOW)
    return runtime, snapshot, candidate_set, result


class TheDeclaredOrderDoesNotOverrideAPlan(unittest.TestCase):
    """A declaration that differs from the plan is not an error to refuse."""

    def setUp(self) -> None:
        _, _, self.candidate_set, self.result = _divergent_case()

    def test_the_two_orders_really_do_diverge_here(self) -> None:
        """Guards the rest of the file: without divergence it proves nothing."""
        self.assertEqual(
            self.candidate_set.apply_order,
            ("cell-steering", "ue-dl-prb-cap", "scheduler-priority"))
        self.assertNotEqual(
            tuple(step.action_id for step in self.result.plan.steps),
            self.candidate_set.apply_order)

    def test_a_divergent_declaration_still_plans(self) -> None:
        self.assertIs(self.result.outcome, CoordinationOutcome.PLANNED)
        self.assertIsNotNone(self.result.plan)
        self.assertFalse(self.result.replan_required)
        self.assertEqual(self.result.refusals, ())

    def test_the_declaration_is_carried_unchanged_beside_the_plan(self) -> None:
        """Yielding is not deleting: the composition keeps its own record."""
        self.assertEqual(self.candidate_set.apply_order,
                         self.candidate_set.resolved.apply_order)
        self.assertEqual(self.candidate_set.rollback_order,
                         tuple(reversed(self.candidate_set.apply_order)))

    def test_the_coordinator_never_reads_the_declared_order(self) -> None:
        """Structural, not incidental: no code path consults it.

        A future edit that made the plan defer to ``apply_order`` would have
        to read it, so this is the cheapest place to notice.
        """
        source = pathlib.Path(execution_module.__file__).read_text(
            encoding="utf-8")
        tree = ast.parse(source, filename=execution_module.__file__)
        reads = sorted({
            node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and node.attr in {"apply_order", "rollback_order"}
            and not (isinstance(node.value, ast.Name)
                     and node.value.id == "self")
        })
        self.assertEqual(
            reads, [],
            f"the execution coordinator reads the composition-level "
            f"declaration {reads}; per SEAMS-GATE2 section 13 X-06 the plan's "
            "own order governs")


class ThePlanIsTheOrderAuthority(unittest.TestCase):
    """Execution order comes from concrete-Action dependencies."""

    def setUp(self) -> None:
        _, _, self.candidate_set, self.result = _divergent_case()
        self.plan = self.result.plan

    def test_the_step_order_is_the_dependency_derived_one(self) -> None:
        self.assertEqual(
            tuple(step.action_id for step in self.plan.steps),
            ("cell-steering", "scheduler-priority", "ue-dl-prb-cap"))

    def test_the_dependency_is_what_produced_that_order(self) -> None:
        """The handover leads because the other two depend on its readback."""
        by_action = {step.action_id: step for step in self.plan.steps}
        self.assertEqual(by_action["cell-steering"].execution_priority, 1)
        for dependent in ("scheduler-priority", "ue-dl-prb-cap"):
            with self.subTest(action=dependent):
                self.assertEqual(by_action[dependent].execution_priority, 2)
                kinds = {p.kind for p in by_action[dependent].preconditions}
                self.assertIn(
                    execution_module.PreconditionKind.PRIMARY_READBACK_CONFIRMED,
                    kinds)

    def test_no_xapp_is_ranked_anywhere(self) -> None:
        """Order still attaches to Actions, never to an xApp."""
        source = pathlib.Path(execution_module.__file__).read_text(
            encoding="utf-8")
        for banned in ("XAPP_PRIORITY", "FIXED_PRIORITY", "XAPP_RANK"):
            self.assertNotIn(banned, source)


class ThePlanIsTheRollbackAuthority(unittest.TestCase):
    """Rollback undoes what was applied, in the reverse of applying it."""

    def setUp(self) -> None:
        _, _, self.candidate_set, self.result = _divergent_case()
        self.plan = self.result.plan

    def test_rollback_is_the_reverse_of_the_plan_s_own_steps(self) -> None:
        self.assertEqual(
            self.plan.rollback_order,
            tuple(step.step_id for step in reversed(self.plan.steps)))

    def test_rollback_is_not_the_declared_rollback_order(self) -> None:
        step_id_of = {step.action_id: step.step_id for step in self.plan.steps}
        declared = tuple(step_id_of[action_id]
                         for action_id in self.candidate_set.rollback_order)
        self.assertNotEqual(self.plan.rollback_order, declared)

    def test_rollback_reverses_every_applied_step_exactly_once(self) -> None:
        """The property that actually protects the equipment."""
        self.assertEqual(sorted(self.plan.rollback_order),
                         sorted(step.step_id for step in self.plan.steps))
        self.assertEqual(len(set(self.plan.rollback_order)),
                         len(self.plan.steps))


class AdmissionConsumersReadThePlan(unittest.TestCase):
    """What a downstream consumer can see is the plan's order, and only it."""

    def setUp(self) -> None:
        _, self.snapshot, self.candidate_set, self.result = _divergent_case()
        self.plan = self.result.plan

    def test_the_canonical_plan_record_carries_the_plan_s_order(self) -> None:
        record = self.plan.to_canonical_dict()
        self.assertEqual([step["actionId"] for step in record["steps"]],
                         [step.action_id for step in self.plan.steps])
        self.assertEqual(tuple(record["rollbackOrder"]),
                         self.plan.rollback_order)

    def test_the_canonical_plan_record_carries_no_declared_order(self) -> None:
        """A consumer cannot reach the declaration through the plan at all."""
        record = self.plan.to_canonical_dict()
        self.assertNotIn("applyOrder", record)
        for key in ("applyOrder", "rollbackOrder"):
            for step in record["steps"]:
                self.assertNotIn(key, step)

    def test_the_plan_still_says_admission_and_a_permit_come_first(self) -> None:
        """Order authority is not write authority; nothing here grants a write."""
        self.assertIn("Kernel admission", self.plan.admission_note)
        for step in self.plan.steps:
            self.assertEqual(step.required_permit_kinds, ("PREPARE", "COMMIT"))

    def test_assignments_are_built_from_the_plan_s_steps(self) -> None:
        the_permit = permit()
        assignments = [
            assignment_for_step(self.plan, step, permit=the_permit,
                                snapshot_hash=self.snapshot.content_hash())
            for step in self.plan.steps
        ]
        self.assertEqual([a.action_id for a in assignments],
                         [step.action_id for step in self.plan.steps])
        self.assertEqual([a.step_id for a in assignments],
                         [step.step_id for step in self.plan.steps])


if __name__ == "__main__":
    unittest.main()
