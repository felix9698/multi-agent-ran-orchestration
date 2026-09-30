"""The PIN_TO_CELL regression case, judged by the new Kernel.

Gate 3 (task section 13) runs the known physical steering case through the new
architecture.  Before it can, the case has to exist in the new contract
families and the Kernel has to judge it the way the frozen
``AIC_UECellSteering_1.0.0`` contract does.  This file is that check, still
hardware-free: the same contracts Gate 3 will freeze, the same Kernel, the
same gateway, and a scripted readback series instead of a UE.

The claim worth stating twice is the identity predicate.  ``MIN(nCI) ==
pinned AND MAX(nCI) == pinned`` is not a roundabout way of writing one
equality -- it is a statement about *every sample in the hold*, and the second
class below shows it catching a UE that the old end-of-episode readback would
have called a success.
"""

from __future__ import annotations

import unittest

from assurance.core.axes import (
    CaseTermination,
    EvidenceCellStatus,
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
    TrialOutcome,
)
from assurance.core.states import StopReason, TrialState
from assurance.contracts import contract_content_hash, validate_family_set
from assurance.gateway.plan import config_hash

from tests.assurance.pin_to_cell_support import (
    BASELINE_CONFIG,
    CELL_ID,
    HOME_NCI,
    POLICY_TYPE_ID,
    TARGET_NCI,
    PinToCellFixture,
    contract_set,
)


class ContractExpressionTests(unittest.TestCase):
    """The case says the same thing the frozen policy contract says."""

    def setUp(self) -> None:
        self.data = contract_set()

    def test_the_contract_family_is_cross_consistent(self) -> None:
        validate_family_set(
            [
                self.data[key]
                for key in (
                    "counter",
                    "measurement_min",
                    "measurement_max",
                    "target",
                    "vector",
                    "release",
                    "case_policy",
                    "watchdog",
                    "harm",
                    "deployment",
                    "actuator",
                    "capability",
                    "composition",
                )
            ]
        )

    def test_pin_to_cell_admits_exactly_one_cell(self) -> None:
        """The frozen schema refuses a PIN_TO_CELL with more than one cell.

        Expressed here as a one-point parameter space, so the epoch's whole
        candidate universe for this target has cardinality 1 -- there is
        nothing else for an agent to name.
        """
        option = self.data["target"].options[0]

        self.assertEqual(
            dict(option.parameter_space), {"servingCell": (str(TARGET_NCI),)}
        )
        fixture = PinToCellFixture()
        fixture.build()
        self.assertEqual(fixture.epoch.candidate_universe_cardinality, 1)

    def test_the_actuation_stays_on_the_official_path(self) -> None:
        actuator = self.data["actuator"]

        self.assertEqual(actuator.path.value, "OFFICIAL_ORAN_DYNAMIC")
        self.assertEqual(actuator.policy_type_id, POLICY_TYPE_ID)
        self.assertEqual(
            actuator.readback_measurement_ref, "measurement/serving-cell-min"
        )

    def test_the_identity_predicate_is_a_pair_over_one_counter(self) -> None:
        floor, ceiling = self.data["target"].predicates

        self.assertEqual(
            self.data["measurement_min"].counter_id,
            self.data["measurement_max"].counter_id,
        )
        self.assertEqual(self.data["measurement_min"].aggregation.value, "MIN")
        self.assertEqual(self.data["measurement_max"].aggregation.value, "MAX")
        for predicate in (floor, ceiling):
            self.assertTrue(predicate.mandatory)
            self.assertEqual(predicate.constraint.operator.value, "EQUAL")
            self.assertEqual(predicate.constraint.bound.value, float(TARGET_NCI))
            self.assertEqual(predicate.constraint.bound.unit, "nci")

    def test_an_identity_carries_no_uncertainty_margin(self) -> None:
        """A non-zero margin would let ``EQUAL`` accept a neighbouring cell."""
        for key in ("measurement_min", "measurement_max"):
            with self.subTest(measurement=key):
                self.assertEqual(
                    self.data[key].uncertainty_rule.parameter.value, 0.0
                )
                self.assertEqual(
                    self.data[key].uncertainty_rule.parameter.unit, "nci"
                )

    def test_the_contract_set_is_content_addressed_and_stable(self) -> None:
        again = contract_set()

        for key, contract in self.data.items():
            with self.subTest(contract=key):
                self.assertEqual(
                    contract_content_hash(contract),
                    contract_content_hash(again[key]),
                )


class PinnedTrialTests(PinToCellFixture, unittest.TestCase):
    """A UE that stays where it was put."""

    def test_a_pinned_ue_runs_to_a_finalized_success(self) -> None:
        path = self.build(observed=(TARGET_NCI,) * 5)

        report = path.run_trial(self.pinned_candidate_id(), cell_id=CELL_ID)

        self.assertEqual(report.terminal_state, TrialState.SETTLED_SUCCESS)
        self.assertEqual(report.outcome, TrialOutcome.SUCCESS)
        self.assertEqual(report.evidence_status, EvidenceCellStatus.CLOSED_PASS.value)
        self.assertEqual(self.adapter.snapshot(), {"servingCell": str(TARGET_NCI)})
        self.assertEqual(path.terminate(), CaseTermination.SUCCESS)

    def test_both_identity_predicates_pass_on_the_same_trace(self) -> None:
        path = self.build(observed=(TARGET_NCI,) * 5)
        report = path.run_trial(self.pinned_candidate_id(), cell_id=CELL_ID)

        evaluation = self.kernel.reduced_state()["trials"][report.trial_id][
            "evaluation"
        ]
        self.assertEqual(
            evaluation["predicateVerdicts"],
            {
                "serving-cell-is-pinned-throughout": PredicateVerdict.PASS.value,
                "serving-cell-is-pinned-only": PredicateVerdict.PASS.value,
            },
        )
        self.assertEqual(
            evaluation["executionValidity"], ExecutionValidity.VALID.value
        )
        self.assertEqual(
            evaluation["measurementSufficiency"],
            MeasurementSufficiency.SUFFICIENT.value,
        )
        self.assertTrue(evaluation["holdComplete"])

    def test_the_kernel_armed_the_watchdog_the_a1_path_cannot_host(self) -> None:
        path = self.build()
        path.run_trial(self.pinned_candidate_id(), cell_id=CELL_ID)

        readiness = [
            envelope.payload
            for envelope in self.store.iterate()
            if envelope.event_kind == "TrialCommitReadinessRecorded"
        ]
        self.assertEqual(self.adapter.armed_watchdogs, [])
        self.assertEqual(
            readiness[0]["kernelHostedGuards"],
            ["measurement-staleness", "token-lease-deadline"],
        )
        self.assertEqual(readiness[0]["kernelHostedWatchdogIds"], ["wd/serving-cell"])


class DriftingTrialTests(PinToCellFixture, unittest.TestCase):
    """A UE that does not stay where it was put."""

    def test_a_ue_that_leaves_and_returns_inside_the_hold_fails(self) -> None:
        """The case the old one-shot readback could not see.

        The series ends on the pinned cell, so a readback taken at the end of
        the episode reports success.  ``MIN`` saw the excursion, so the
        mandatory pair does not pass and the trial is a ``FAIL`` that rolls
        back -- which is the honest answer about a pin that did not hold.
        """
        path = self.build(
            observed=(TARGET_NCI, TARGET_NCI, HOME_NCI, TARGET_NCI, TARGET_NCI)
        )

        report = path.run_trial(self.pinned_candidate_id(), cell_id=CELL_ID)

        evaluation = self.kernel.reduced_state()["trials"][report.trial_id][
            "evaluation"
        ]
        self.assertEqual(
            evaluation["predicateVerdicts"],
            {
                "serving-cell-is-pinned-throughout": PredicateVerdict.FAIL.value,
                "serving-cell-is-pinned-only": PredicateVerdict.PASS.value,
            },
        )
        # The trace was valid and sufficient: this is a failed pin, not an
        # undecidable one.
        self.assertEqual(
            evaluation["executionValidity"], ExecutionValidity.VALID.value
        )
        self.assertEqual(
            evaluation["measurementSufficiency"],
            MeasurementSufficiency.SUFFICIENT.value,
        )
        self.assertEqual(report.outcome, TrialOutcome.FAIL)
        self.assertEqual(report.stop_reason, StopReason.SEMANTIC_NON_SUCCESS)
        self.assertEqual(report.terminal_state, TrialState.SETTLED_NON_SUCCESS)
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        self.assertEqual(self.adapter.live_hash(), config_hash(BASELINE_CONFIG))

    def test_a_ue_that_never_moved_fails_both_ends(self) -> None:
        path = self.build(observed=(HOME_NCI,) * 5)

        report = path.run_trial(self.pinned_candidate_id(), cell_id=CELL_ID)

        evaluation = self.kernel.reduced_state()["trials"][report.trial_id][
            "evaluation"
        ]
        self.assertEqual(
            set(evaluation["predicateVerdicts"].values()),
            {PredicateVerdict.FAIL.value},
        )
        self.assertEqual(report.outcome, TrialOutcome.FAIL)
        self.assertEqual(report.evidence_status, EvidenceCellStatus.CLOSED_FAIL.value)

    def test_a_ue_that_moves_higher_fails_the_ceiling(self) -> None:
        path = self.build(
            observed=(TARGET_NCI, TARGET_NCI, TARGET_NCI + 1, TARGET_NCI, TARGET_NCI)
        )

        report = path.run_trial(self.pinned_candidate_id(), cell_id=CELL_ID)

        evaluation = self.kernel.reduced_state()["trials"][report.trial_id][
            "evaluation"
        ]
        self.assertEqual(
            evaluation["predicateVerdicts"],
            {
                "serving-cell-is-pinned-throughout": PredicateVerdict.PASS.value,
                "serving-cell-is-pinned-only": PredicateVerdict.FAIL.value,
            },
        )
        self.assertEqual(report.outcome, TrialOutcome.FAIL)


class EmergencyStopOnThisDeploymentTests(PinToCellFixture, unittest.TestCase):
    def test_emergency_stop_settles_because_the_safe_cell_is_the_home_cell(
        self,
    ) -> None:
        """Judgement 1, on the deployment Gate 3 actually runs.

        For a steering objective the contracted safe state is the resting
        configuration, so an Emergency Stop lands exactly on the trial's
        baseline and settles as the Operator control it was.
        """
        path = self.build()
        trial_id = path.open_trial(self.pinned_candidate_id())
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)
        path.ready(trial_id)
        path.commit_decision(trial_id)
        path.commit(trial_id)
        path.enter_observation(trial_id)

        report = path.emergency_stop(trial_id)

        self.assertEqual(self.adapter.snapshot(), dict(BASELINE_CONFIG))
        self.assertEqual(report.outcome, TrialOutcome.OPERATOR_ABORTED)
        self.assertEqual(report.terminal_state, TrialState.SETTLED_NON_SUCCESS)
        self.assertEqual(self.kernel.reduced_state()["resourceLocks"], {})
        self.assertEqual(path.terminate(), CaseTermination.OPERATOR_ABORT)


if __name__ == "__main__":
    unittest.main()
