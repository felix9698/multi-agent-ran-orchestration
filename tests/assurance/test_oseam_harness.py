"""The shared scenario matrix, run against a real contract set.

Gate 4 asks for a hardware-free positive, negative and fault round trip for
every objective family (task section 13).  A harness that has never executed
would make that promise cheap, so this file runs the whole ten-scenario matrix
over the Gate 2 vertical contract set -- unchanged, through the real Kernel,
Gateway and Collector -- and then checks the harness's own guarantees: that a
wrong expectation is caught, that a composite family's components are proved to
have been judged in one trial, that the run is deterministic, and that nothing
it produces can be mistaken for OTA evidence.

Running the reference case says nothing about any family's support state.  The
bundle is the Gate 2 fixture, not ``TrafficSteeringPreference``'s own contract
set; the registry reads ``IMPLEMENTATION_IN_PROGRESS`` for all seven families
until each lane builds and runs its own.
"""

from __future__ import annotations

import socket
import unittest
from dataclasses import replace

from assurance.contracts.target import TargetPredicate
from assurance.core.axes import TrialOutcome
from assurance.core.states import TrialState
from assurance.gateway.mock_adapter import FaultInjection
from assurance.objectives.family import (
    VERDICT_SCENARIOS,
    ExpectedConfiguration,
    ExpectedOutcome,
    ScenarioName,
)

from tests.assurance.objective_harness import (
    HarnessInvariantError,
    NoLiveCall,
    ObjectiveHarness,
    ObjectiveMatrixMixin,
    ScenarioResult,
    gate2_reference_bundle,
    gate2_reference_case,
    mismatches,
    no_live_calls,
)


def composite_reference_case():
    """The reference case with two mandatory predicates, in two components.

    A synthetic composite, built by adding a second mandatory predicate over
    the same measurement, so the joint-verification rule can be exercised
    before any real composite family exists.  ``QoSandTSP`` and ``QoEandTSP``
    will supply two genuinely different predicate sets; what is proved here is
    that the harness notices whether both were judged inside one trial.
    """
    case = gate2_reference_case()
    bundle = case.bundle
    first = bundle.target.predicates[0]
    guard = TargetPredicate(
        "dl-throughput-guard",
        replace(
            first.constraint,
            bound=replace(first.constraint.bound, value=1.0),
        ),
        description="the second component's mandatory condition",
    )
    target = replace(bundle.target, predicates=(first, guard))
    return replace(
        case,
        bundle=replace(
            bundle,
            target=target,
            component_predicates={
                "reference/component-a": (first.predicate_id,),
                "reference/component-b": (guard.predicate_id,),
            },
        ),
    )


class ReferenceMatrixTests(ObjectiveMatrixMixin, unittest.TestCase):
    """All ten scenarios, over the Gate 2 contract set."""

    def make_case(self):
        return gate2_reference_case()


class CompositeMatrixTests(ObjectiveMatrixMixin, unittest.TestCase):
    """All ten scenarios plus joint verification, over a two-component target."""

    def make_case(self):
        return composite_reference_case()


class MatrixCoverageTests(unittest.TestCase):
    """The matrix cannot shrink without the shrinking being visible."""

    def test_every_scenario_of_task_section_8_has_a_test_method(self) -> None:
        expected = {
            f"test_{name.value.replace('-', '_')}" for name in ScenarioName
        }
        self.assertTrue(expected <= set(dir(ObjectiveMatrixMixin)))
        self.assertEqual(
            [name.value for name in ScenarioName],
            [
                "positive", "negative", "malformed", "conflict", "stale",
                "missing", "timeout", "partial-effect", "duplicate", "fault",
            ],
        )

    def test_verdict_scenarios_are_the_ones_a_family_must_declare(self) -> None:
        case = gate2_reference_case()
        self.assertEqual(set(case.expectations), set(VERDICT_SCENARIOS))
        refusals = set(ScenarioName) - set(VERDICT_SCENARIOS)
        self.assertEqual(
            {name.value for name in refusals},
            {"malformed", "conflict", "timeout", "duplicate"},
        )


class HarnessGuaranteeTests(unittest.TestCase):
    """What the harness promises the family lanes, checked rather than stated."""

    def test_a_wrong_expectation_is_reported_not_absorbed(self) -> None:
        case = gate2_reference_case()
        result = ObjectiveHarness(case).run(ScenarioName.NEGATIVE)

        wrong = ExpectedOutcome(
            terminal_state=TrialState.SETTLED_SUCCESS,
            outcome=TrialOutcome.SUCCESS,
            configuration=ExpectedConfiguration.APPLIED,
        )
        problems = mismatches(result, wrong, case.bundle)

        self.assertTrue(any("terminal state" in item for item in problems))
        self.assertTrue(any("outcome" in item for item in problems))
        self.assertTrue(any("configuration" in item for item in problems))

    def test_the_declared_expectation_matches_exactly(self) -> None:
        case = gate2_reference_case()
        for scenario in VERDICT_SCENARIOS:
            with self.subTest(scenario=scenario.value):
                result = ObjectiveHarness(case).run(scenario)
                self.assertEqual(
                    mismatches(result, case.expectations[scenario], case.bundle), ()
                )

    def test_no_scenario_can_report_ota_evidence(self) -> None:
        case = gate2_reference_case()
        for scenario in ScenarioName:
            with self.subTest(scenario=scenario.value):
                result = ObjectiveHarness(case).run(scenario)
                self.assertEqual(result.evidence_grade, "HARDWARE_FREE")
                self.assertFalse(result.is_ota_evidence)

    def test_the_evidence_grade_is_not_a_settable_opinion(self) -> None:
        """``is_ota_evidence`` is derived, so relabelling a result cannot lie."""
        result = ScenarioResult(scenario=ScenarioName.POSITIVE, family="probe")
        result.evidence_grade = "OTA"
        self.assertFalse(result.is_ota_evidence)

    def test_a_run_opens_no_socket_and_starts_no_process(self) -> None:
        with self.assertRaises(NoLiveCall):
            with no_live_calls():
                socket.socket()

    def test_the_matrix_is_deterministic(self) -> None:
        case = gate2_reference_case()
        first = ObjectiveHarness(case)
        second = ObjectiveHarness(case)
        first.run(ScenarioName.POSITIVE)
        second.run(ScenarioName.POSITIVE)

        self.assertEqual(
            first.kernel.terminal_state_hash(), second.kernel.terminal_state_hash()
        )
        self.assertEqual(first.epoch.catalog_hash, second.epoch.catalog_hash)
        self.assertEqual(first.path.epoch_hash(), second.path.epoch_hash())

    def test_every_effect_carries_a_kernel_issued_permit(self) -> None:
        """The universal invariant, restated as its own claim.

        ``ObjectiveHarness.run`` raises when it does not hold; this asserts the
        positive form so a reader can see what the invariant is without
        reading the harness.
        """
        harness = ObjectiveHarness(gate2_reference_case())
        harness.run(ScenarioName.POSITIVE)

        issued = {
            envelope.payload["idempotencyKey"]
            for envelope in harness.store.iterate()
            if envelope.event_kind == "TokenIssued"
        }
        self.assertTrue(harness.adapter.idempotency_keys)
        for key in harness.adapter.idempotency_keys:
            self.assertIn(key.split("#")[0], issued)


class JointVerificationTests(unittest.TestCase):
    """Task section 8: a combined objective is judged in one trial, not two."""

    def test_both_components_are_judged_inside_one_trial(self) -> None:
        case = composite_reference_case()
        harness = ObjectiveHarness(case)
        result = harness.run(ScenarioName.POSITIVE)
        report = harness.joint_predicate_report(result)

        self.assertEqual(report["trialCount"], 1)
        self.assertEqual(
            report["mandatoryPredicateIds"],
            {"dl-throughput-floor", "dl-throughput-guard"},
        )
        for predicate_ids in report["components"].values():
            self.assertLessEqual(predicate_ids, report["judgedPredicateIds"])

    def test_a_component_absent_from_the_trial_is_visible(self) -> None:
        """The check is a subset test, so a missing component cannot pass.

        Declaring a component predicate the target does not carry is the
        shape of "we verified the other half in a different run"; the report
        has to make that fail rather than silently match on the half present.
        """
        case = composite_reference_case()
        bundle = replace(
            case.bundle,
            component_predicates={
                **case.bundle.component_predicates,
                "reference/component-c": ("judged-somewhere-else",),
            },
        )
        harness = ObjectiveHarness(replace(case, bundle=bundle))
        result = harness.run(ScenarioName.POSITIVE)
        report = harness.joint_predicate_report(result)

        self.assertFalse(
            report["components"]["reference/component-c"]
            <= report["judgedPredicateIds"]
        )


class ScenarioMeaningTests(unittest.TestCase):
    """The four refusal scenarios, in the form a reader can check."""

    def test_malformed_input_touches_nothing(self) -> None:
        harness = ObjectiveHarness(gate2_reference_case())
        result = harness.run(ScenarioName.MALFORMED)

        self.assertEqual(result.adapter_writes, 0)
        self.assertIn("MALFORMED_TYPED_ADVISORY", result.refusals)
        self.assertEqual(harness.kernel.reduced_state()["trials"], {})

    def test_a_concurrent_trial_is_refused_at_both_windows(self) -> None:
        result = ObjectiveHarness(gate2_reference_case()).run(ScenarioName.CONFLICT)

        self.assertIn("ACTIVE_TRIAL_EXISTS", result.detail)
        self.assertIn("RECOVERY_BLOCKED", result.detail)
        self.assertEqual(result.terminal_state, TrialState.SETTLED_SUCCESS)

    def test_an_expired_lease_halts_but_never_finalizes(self) -> None:
        case = gate2_reference_case()
        harness = ObjectiveHarness(case)
        result = harness.run(ScenarioName.TIMEOUT)

        self.assertEqual(dict(result.configuration), dict(case.bundle.safe_state))
        # The Kernel decided nothing, so the trial is not settled: the gateway
        # may halt a run it cannot reach the Kernel about, and may not close it.
        self.assertEqual(result.outcome, TrialOutcome.NOT_SETTLED)
        trial = harness.kernel.reduced_state()["trials"][result.trial_id]
        self.assertFalse(trial.get("finalizeAcknowledged"))

    def test_a_retransmitted_commit_writes_once(self) -> None:
        result = ObjectiveHarness(gate2_reference_case()).run(ScenarioName.DUPLICATE)

        self.assertIn("ALREADY_APPLIED", result.detail)
        self.assertEqual(result.terminal_state, TrialState.SETTLED_SUCCESS)


class BundleShapeTests(unittest.TestCase):
    """The bundle is what a family lane fills; its order is load-bearing."""

    def test_admission_order_puts_references_before_the_things_using_them(
        self,
    ) -> None:
        bundle = gate2_reference_bundle()
        order = [type(contract).__name__ for contract in bundle.unconfirmed_contracts()]

        self.assertLess(order.index("CounterBinding"), order.index("MeasurementContract"))
        self.assertLess(order.index("MeasurementContract"), order.index("TargetContract"))
        self.assertLess(order.index("ActuatorBinding"), order.index("CapabilityManifest"))
        self.assertLess(
            order.index("CapabilityManifest"), order.index("CompositionManifest")
        )

    def test_the_operator_confirms_the_vector_and_the_policy(self) -> None:
        bundle = gate2_reference_bundle()
        self.assertEqual(
            [type(contract).__name__ for contract in bundle.confirmed_contracts()],
            ["TargetVector", "CoordinationCasePolicy"],
        )

    def test_configuration_axes_come_from_the_option_parameter_space(self) -> None:
        bundle = gate2_reference_bundle()
        self.assertEqual(
            set(bundle.configuration_axes()), {"queuePriority", "servingCell"}
        )

    def test_a_one_axis_family_must_supply_its_own_partial_effect(self) -> None:
        """A single-axis plan cannot land partially, and the harness says so.

        The substitutes are other scenarios wearing this one's name: a lost
        acknowledgement the readback resolves is a success, and a readback that
        fails is the fault case.  Refusing to invent one is what keeps a
        reported partial-effect pass meaning what it says.
        """
        case = gate2_reference_case()
        bundle = case.bundle
        option = replace(
            bundle.target.options[0], parameter_space={"servingCell": ("cell-2",)}
        )
        one_axis = replace(
            case,
            bundle=replace(bundle, target=replace(bundle.target, options=(option,))),
        )

        with self.assertRaises(HarnessInvariantError) as raised:
            ObjectiveHarness(one_axis).run(ScenarioName.PARTIAL_EFFECT)
        self.assertIn("faults_for", str(raised.exception))

    def test_a_supplied_fault_recipe_is_used(self) -> None:
        """The escape hatch changes the injected fault, never the assertion."""
        case = gate2_reference_case()
        bundle = case.bundle
        option = replace(
            bundle.target.options[0], parameter_space={"servingCell": ("cell-2",)}
        )
        one_axis = replace(
            case,
            bundle=replace(bundle, target=replace(bundle.target, options=(option,))),
            expectations={
                **case.expectations,
                ScenarioName.PARTIAL_EFFECT: ExpectedOutcome(
                    terminal_state=TrialState.SETTLED_NON_SUCCESS,
                    outcome=TrialOutcome.SAFETY_STOPPED,
                    rationale=(
                        "the supplied recipe refuses the only axis, so no axis of "
                        "the plan is live; the apply is reversed and the trial "
                        "settles as a safety stop"
                    ),
                ),
            },
            faults_for=lambda scenario: (
                FaultInjection(fail_axes=frozenset({"servingCell"}))
                if scenario is ScenarioName.PARTIAL_EFFECT
                else None
            ),
        )

        result = ObjectiveHarness(one_axis).run(ScenarioName.PARTIAL_EFFECT)

        self.assertEqual(
            mismatches(
                result, one_axis.expectations[ScenarioName.PARTIAL_EFFECT], one_axis.bundle
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
