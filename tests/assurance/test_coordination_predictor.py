"""The central joint-effect predictor: estimates, and where they come from.

The predictor is what every method is given as ``effect_evidence``, so what has
to be true of it is not that it is right -- it is an estimate -- but that it is
**self-consistent and separate**.  Self-consistent: capping one UE lowers that
UE and raises the one it shares a cell with, moving a UE to an empty cell
raises it, and calibration moves the model toward what was actually observed.
Separate: nothing here imports the emulator, so an agent ranking candidates
with this model is not reading the ground truth it is about to be measured
against.

Hermetic: no clock, no file system, no network.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from assurance.coordination.predictor import (
    BASELINE_MCS_BOUNDS, BASELINE_SLICE_QUOTA, DEFAULT_UNCERTAINTY,
    JointEffectPredictor, NetworkState,
)
from assurance.coordination.tc import UNKNOWN

HOME, AWAY, EMPTY = "12345678", "87654321", "11112222"


def shared_cell_state(**overrides):
    """Two UEs contending on one cell, a third cell standing empty."""
    state = dict(cells={HOME: 5.0, AWAY: 5.0, EMPTY: 5.0},
                 ues={"131": HOME, "132": HOME},
                 offered_load_mbps={"131": 4.0, "132": 4.0})
    state.update(overrides)
    return NetworkState(**state)


class TheModelIsSeparateCode(unittest.TestCase):
    """A predictor that can read the ground truth is not a predictor."""

    def test_it_does_not_import_the_emulator(self):
        source = Path("assurance/coordination/predictor.py").read_text(encoding="utf-8")
        self.assertNotIn("hfconsole", source)
        self.assertNotIn("agent_env", source)


class TheCapacityShare(unittest.TestCase):
    """One cell divided by PF weight times efficiency, clipped by load and cap."""

    def setUp(self):
        self.state = shared_cell_state()
        self.predictor = JointEffectPredictor(self.state)
        self.baseline = self.state.applied_configuration()

    def test_two_equal_ues_split_the_cell(self):
        goodput = self.predictor.goodput(self.baseline)
        self.assertAlmostEqual(goodput["131"], 2.5)
        self.assertAlmostEqual(goodput["132"], 2.5)

    def test_a_cap_lowers_the_capped_ue_and_raises_the_one_it_shares_with(self):
        capped = dict(self.baseline, **{"dlPrbCap@132": "6"})
        before = self.predictor.goodput(self.baseline)
        after = self.predictor.goodput(capped)
        self.assertLess(after["132"], before["132"])
        self.assertGreater(after["131"], before["131"])

    def test_a_heavier_pf_weight_raises_that_ue_at_the_others_expense(self):
        weighted = dict(self.baseline, **{"pfWeight@131": "24"})
        before = self.predictor.goodput(self.baseline)
        after = self.predictor.goodput(weighted)
        self.assertGreater(after["131"], before["131"])
        self.assertLess(after["132"], before["132"])

    def test_a_steer_to_an_empty_cell_raises_both_ues(self):
        moved = dict(self.baseline, **{"servingCell@131": EMPTY})
        before = self.predictor.goodput(self.baseline)
        after = self.predictor.goodput(moved)
        self.assertGreater(after["131"], before["131"])
        self.assertGreater(after["132"], before["132"])

    def test_nobody_exceeds_their_offered_load(self):
        moved = dict(self.baseline, **{"servingCell@131": EMPTY})
        for value in self.predictor.goodput(moved).values():
            self.assertLessEqual(value, 4.0 + 1e-9)

    def test_a_cell_change_costs_the_transient_out_of_the_window(self):
        state = shared_cell_state(window_ms=4000.0)
        predictor = JointEffectPredictor(state)
        moved = dict(state.applied_configuration(), **{"servingCell@131": EMPTY})
        settled = JointEffectPredictor(shared_cell_state()).goodput(
            dict(state.applied_configuration(), **{"servingCell@131": EMPTY}))
        self.assertLess(predictor.goodput(moved)["131"], settled["131"])

    def test_an_uncapped_cap_is_written_as_zero_by_the_joint_contracts(self):
        uncapped = dict(self.baseline, **{"dlPrbCap@132": "0"})
        self.assertEqual(self.predictor.goodput(uncapped),
                         self.predictor.goodput(self.baseline))


class ThePrediction(unittest.TestCase):
    """``predict`` answers KPI keys with a value and an uncertainty."""

    def setUp(self):
        self.state = shared_cell_state()
        self.predictor = JointEffectPredictor(self.state)

    def test_every_ue_gets_a_goodput_and_a_serving_cell(self):
        predicted = self.predictor.predict(self.state.applied_configuration())
        self.assertIn("dlGoodputMbps@131", predicted)
        self.assertEqual(predicted["servingCell@131"], (HOME, 0.0))
        value, uncertainty = predicted["dlGoodputMbps@131"]
        self.assertAlmostEqual(uncertainty, value * DEFAULT_UNCERTAINTY, places=6)

    def test_a_cell_with_no_known_capacity_is_unknown_not_a_guess(self):
        moved = dict(self.state.applied_configuration(),
                     **{"servingCell@131": "99998888"})
        predicted = self.predictor.predict(moved)
        self.assertEqual(predicted["dlGoodputMbps@131"], (UNKNOWN, UNKNOWN))


class ThePredictionTable(unittest.TestCase):
    """What the executor hands over as ``effect_evidence.predictions``."""

    def setUp(self):
        self.state = shared_cell_state()
        self.predictor = JointEffectPredictor(self.state)
        self.baseline = self.state.applied_configuration()

    def test_a_small_action_space_is_covered_exhaustively(self):
        space = {"servingCell@131": [HOME, AWAY], "dlPrbCap@132": ["24", "12", "6"]}
        table = self.predictor.prediction_table(space, self.baseline, limit=64)
        self.assertEqual(len(table), 6)

    def test_a_large_action_space_is_capped_at_the_limit(self):
        space = {f"pfWeight@{ue}": ["4", "8", "12", "16"]
                 for ue in ("131", "132", "133", "134")}
        table = self.predictor.prediction_table(space, {}, limit=12)
        self.assertEqual(len(table), 12)

    def test_a_capped_table_starts_at_the_baseline_and_the_single_moves(self):
        space = {f"pfWeight@{ue}": ["4", "8", "12", "16"]
                 for ue in ("131", "132", "133", "134")}
        table = self.predictor.prediction_table(
            space, {axis: "8" for axis in space}, limit=12)
        self.assertEqual(table[0]["configuration"],
                         {axis: "8" for axis in space})
        self.assertEqual(sum(1 for row in table[1:]
                             if sum(1 for axis, value in row["configuration"].items()
                                    if value != "8") == 1), len(table) - 1)

    def test_the_evidence_carries_the_model_description_and_the_observations(self):
        evidence = self.predictor.effect_evidence(
            {"dlPrbCap@132": ["24", "12"]}, self.baseline,
            observations=[{"controlId": "C1", "kpis": {"dlGoodputMbps@131": 2.4}}])
        self.assertIn("form", evidence["predictorDescription"])
        self.assertEqual(evidence["predictorDescription"]["calibration"]["state"],
                         "uncalibrated")
        self.assertEqual(len(evidence["observations"]), 1)


class TheCalibration(unittest.TestCase):
    """Live, the same arithmetic is moved toward what was actually measured."""

    def setUp(self):
        self.state = shared_cell_state()
        self.predictor = JointEffectPredictor(self.state)
        self.baseline = self.state.applied_configuration()

    def test_it_moves_the_cell_capacity_toward_the_observation(self):
        # the cell really delivers 3 Mbps, not the 5 the model assumed
        before = self.predictor.goodput(self.baseline)
        self.predictor.calibrate([
            {"configuration": self.baseline,
             "kpis": {"dlGoodputMbps@131": 1.5, "dlGoodputMbps@132": 1.5},
             "valid": True}])
        after = self.predictor.goodput(self.baseline)
        self.assertLess(after["131"], before["131"])
        self.assertGreater(after["131"], 1.5 - 1e-6)
        self.assertLess(self.predictor.capacity_scale[HOME], 1.0)

    def test_repeated_calibration_converges_toward_the_observation(self):
        observation = [{"configuration": self.baseline,
                        "kpis": {"dlGoodputMbps@131": 1.5,
                                 "dlGoodputMbps@132": 1.5}, "valid": True}]
        errors = []
        for _round in range(4):
            self.predictor.calibrate(observation)
            errors.append(abs(self.predictor.goodput(self.baseline)["131"] - 1.5))
        self.assertEqual(errors, sorted(errors, reverse=True))
        self.assertLess(errors[-1], 0.2)

    def test_an_invalid_window_is_not_calibrated_on(self):
        self.predictor.calibrate([
            {"configuration": self.baseline,
             "kpis": {"dlGoodputMbps@131": 0.0, "dlGoodputMbps@132": 0.0},
             "valid": False}])
        self.assertEqual(self.predictor.calibration_samples, 0)
        self.assertEqual(self.predictor.capacity_scale, {})

    def test_calibrating_narrows_the_uncertainty_but_never_to_zero(self):
        loose = self.predictor.predict(self.baseline)["dlGoodputMbps@131"][1]
        self.predictor.calibrate([
            {"configuration": self.baseline,
             "kpis": {"dlGoodputMbps@131": 2.4, "dlGoodputMbps@132": 2.4},
             "valid": True}])
        tight = self.predictor.predict(self.baseline)["dlGoodputMbps@131"][1]
        self.assertLess(tight, loose)
        self.assertGreater(tight, 0.0)
        self.assertEqual(self.predictor.describe()["calibration"]["state"],
                         "calibrated")

    def test_no_usable_observation_leaves_the_model_alone(self):
        self.predictor.calibrate([])
        self.predictor.calibrate([{"configuration": self.baseline, "kpis": {}}])
        self.assertEqual(self.predictor.calibration_samples, 0)


class TheNetworkStateRecord(unittest.TestCase):
    """The executor's ``input.network_state`` reads straight into the model."""

    def test_it_reads_the_record_the_executor_builds(self):
        state = NetworkState.from_record({
            "cells": {HOME: {"capacityMbps": 5.0}},
            "ues": {"131": {"servingCell": HOME, "offeredLoadMbps": 4.0,
                            "pfWeight": 8, "dlPrbCap": 24}},
            "prbTotal": 24, "unselectedFunctionRule": "keep-current"})
        self.assertEqual(state.cells, {HOME: 5.0})
        self.assertEqual(state.ues, {"131": HOME})
        self.assertEqual(state.unselected_function_rule, "keep-current")
        self.assertEqual(state.applied_configuration()["dlPrbCap@131"], "24")

    def test_the_applied_configuration_writes_whole_numbers_as_names(self):
        state = shared_cell_state()
        self.assertEqual(state.applied_configuration()["pfWeight@131"], "8")
        self.assertEqual(state.applied_configuration()["dlPrbCap@131"], "24")


if __name__ == "__main__":
    unittest.main()


class TheCellAndSliceScopedEffects(unittest.TestCase):
    """Contract v3 section 5: what the three new axes do to the estimate.

    Each is coarse on purpose, so what is asserted is not a number but the
    three things a searcher ranks candidates on: the **direction** (a lower
    MCS ceiling, more attenuation and a tighter quota never raise the UE they
    constrain), the **scope** (a cell's knob moves every UE on that cell and
    nobody on another; a slice's knob moves that slice's UEs and frees the
    rest), and the **baseline** (the value the axis rests at changes nothing).
    """

    def setUp(self):
        self.state = shared_cell_state(
            cells={HOME: 10.0, AWAY: 10.0},
            ues={"131": HOME, "132": HOME, "133": AWAY},
            offered_load_mbps={"131": 8.0, "132": 8.0, "133": 8.0},
            ue_slices={"132": "1"})
        self.predictor = JointEffectPredictor(self.state)
        self.baseline = self.state.applied_configuration()

    def moved(self, **axes):
        return self.predictor.goodput(dict(self.baseline, **axes))

    def test_the_baseline_configuration_names_every_new_axis(self):
        self.assertEqual(BASELINE_MCS_BOUNDS, self.baseline[f"dlMcsBounds@{HOME}"])
        self.assertEqual("0.0", self.baseline[f"txAttenuationDb@{HOME}"])
        self.assertEqual(BASELINE_SLICE_QUOTA, self.baseline["slicePrbQuota@1"])
        self.assertEqual({"131": 5.0, "132": 5.0, "133": 8.0},
                         self.predictor.goodput(self.baseline))

    def test_an_mcs_ceiling_lowers_every_ue_on_that_cell_and_nobody_else(self):
        capped = self.moved(**{f"dlMcsBounds@{HOME}": "0..16"})
        self.assertLess(capped["131"], 5.0)
        self.assertAlmostEqual(capped["131"], capped["132"])
        self.assertEqual(8.0, capped["133"])

    def test_a_lower_mcs_ceiling_never_gives_more_than_a_higher_one(self):
        rates = [self.moved(**{f"dlMcsBounds@{HOME}": bounds})["131"]
                 for bounds in ("0..10", "0..16", "0..22", "0..28")]
        self.assertEqual(rates, sorted(rates))
        self.assertEqual(5.0, rates[-1])

    def test_the_mcs_floor_is_not_charged_as_rate(self):
        """A raised floor costs reliability, which this model does not claim
        to estimate -- so it must not be smuggled in as a rate change."""
        self.assertEqual(self.moved(**{f"dlMcsBounds@{HOME}": "10..28"}),
                         self.predictor.goodput(self.baseline))

    def test_attenuation_lowers_the_cell_monotonically(self):
        rates = [self.moved(**{f"txAttenuationDb@{HOME}": db})["131"]
                 for db in ("0.0", "6.0", "12.0", "24.0")]
        self.assertEqual(rates, sorted(rates, reverse=True))
        self.assertEqual(5.0, rates[0])
        self.assertLess(rates[-1], rates[1])
        self.assertGreater(rates[-1], 0.0)
        # and the other cell is untouched
        self.assertEqual(8.0, self.moved(
            **{f"txAttenuationDb@{HOME}": "24.0"})["133"])

    def test_a_slice_quota_caps_that_slice_and_frees_the_rest_of_the_cell(self):
        quota = self.moved(**{"slicePrbQuota@1": "0:1:30"})
        self.assertAlmostEqual(3.0, quota["132"])   # 30 % of a 10 Mbps cell
        self.assertGreater(quota["131"], 5.0)       # the contender gets it back
        self.assertEqual(8.0, quota["133"])

    def test_a_quota_that_does_not_bind_changes_nothing(self):
        self.assertEqual(self.moved(**{"slicePrbQuota@1": "0:1:100"}),
                         self.predictor.goodput(self.baseline))
        self.assertEqual(self.moved(**{"slicePrbQuota@1": "0:1:60"}),
                         self.predictor.goodput(self.baseline))

    def test_a_ue_in_no_slice_is_never_touched_by_a_quota(self):
        state = shared_cell_state(cells={HOME: 10.0},
                                  ues={"131": HOME, "132": HOME},
                                  offered_load_mbps={"131": 8.0, "132": 8.0})
        predictor = JointEffectPredictor(state)
        baseline = state.applied_configuration()
        self.assertNotIn("slicePrbQuota@1", baseline)
        self.assertEqual(predictor.goodput(baseline),
                         predictor.goodput(dict(baseline,
                                                **{"slicePrbQuota@1": "0:1:10"})))

    def test_an_unreadable_value_falls_back_to_the_baseline_effect(self):
        """The predictor never guesses at a value it cannot parse."""
        for axis, junk in ((f"dlMcsBounds@{HOME}", "wide-open"),
                           (f"txAttenuationDb@{HOME}", "quiet"),
                           ("slicePrbQuota@1", "generous")):
            self.assertEqual(self.predictor.goodput(self.baseline),
                             self.moved(**{axis: junk}), axis)

    def test_the_state_round_trips_the_new_axes_through_its_record(self):
        state = NetworkState(
            cells={HOME: 10.0}, ues={"131": HOME},
            mcs_bounds={HOME: "0..16"}, tx_attenuation_db={HOME: 6.0},
            ue_slices={"131": "1"}, slice_quota={"1": "0:1:30"})
        restored = NetworkState.from_record(state.to_record())
        self.assertEqual({HOME: "0..16"}, restored.mcs_bounds)
        self.assertEqual({HOME: 6.0}, restored.tx_attenuation_db)
        self.assertEqual({"131": "1"}, restored.ue_slices)
        self.assertEqual({"1": "0:1:30"}, restored.slice_quota)
        self.assertEqual(state.applied_configuration(),
                         restored.applied_configuration())

    def test_the_description_states_what_each_new_effect_does_and_omits(self):
        described = self.predictor.describe()["cellEffects"]
        self.assertIn("dlMcsBounds", described)
        self.assertIn("txAttenuationDb", described)
        self.assertIn("slicePrbQuota", described)
        self.assertIn("reliability", described)

    def test_the_prediction_table_covers_a_cell_scoped_axis(self):
        table = self.predictor.prediction_table(
            {f"dlMcsBounds@{HOME}": ("0..28", "0..16"),
             "slicePrbQuota@1": ("0:1:100", "0:1:30")},
            baselines={f"dlMcsBounds@{HOME}": "0..28",
                       "slicePrbQuota@1": "0:1:100"})
        self.assertEqual(4, len(table))
        rows = {(row["configuration"][f"dlMcsBounds@{HOME}"],
                 row["configuration"]["slicePrbQuota@1"]): row for row in table}
        self.assertLess(rows[("0..16", "0:1:100")]["predicted"]["dlGoodputMbps@131"],
                        rows[("0..28", "0:1:100")]["predicted"]["dlGoodputMbps@131"])


class TheDeadlineSuccessRatioEstimate(unittest.TestCase):
    """A coarse ratio from the predicted share: more share, more completions.

    It is an estimate and it says so.  What has to hold is the direction --
    a control that gives the UE more of the cell must not be estimated to
    complete fewer requests -- and that nothing is predicted for a UE the
    sitting never declared an echo flow on.
    """

    def state(self, **overrides):
        return shared_cell_state(echo_deadline_ms={"131": 20.0},
                                 window_ms=30000, **overrides)

    def test_no_declared_flow_means_no_predicted_ratio(self):
        predictor = JointEffectPredictor(shared_cell_state())
        predicted = predictor.predict(shared_cell_state().applied_configuration())
        self.assertNotIn("deadlineSuccessRatio@131", predicted)

    def test_only_the_ues_with_a_flow_get_a_ratio(self):
        state = self.state()
        predicted = JointEffectPredictor(state).predict(state.applied_configuration())
        self.assertIn("deadlineSuccessRatio@131", predicted)
        self.assertNotIn("deadlineSuccessRatio@132", predicted)

    def test_more_share_is_never_fewer_completions(self):
        state = self.state()
        predictor = JointEffectPredictor(state)
        baseline = state.applied_configuration()
        alone = dict(baseline, **{f"servingCell@132": EMPTY})
        capped = dict(baseline, **{"dlPrbCap@132": "6"})
        shared_ratio = predictor.predict(baseline)["deadlineSuccessRatio@131"][0]
        for better in (alone, capped):
            self.assertGreater(predictor.predict(better)["deadlineSuccessRatio@131"][0],
                               shared_ratio)

    def test_it_is_a_fraction_and_it_carries_the_usual_uncertainty(self):
        state = self.state()
        value, uncertainty = JointEffectPredictor(state).predict(
            state.applied_configuration())["deadlineSuccessRatio@131"]
        self.assertTrue(0.0 <= value <= 1.0)
        self.assertAlmostEqual(value * DEFAULT_UNCERTAINTY, uncertainty, places=5)

    def test_a_cell_with_no_capacity_is_unknown_rather_than_zero(self):
        state = self.state(cells={HOME: 0.0, AWAY: 5.0, EMPTY: 5.0})
        predicted = JointEffectPredictor(state).predict(state.applied_configuration())
        self.assertEqual(UNKNOWN, predicted["deadlineSuccessRatio@131"][0])

    def test_the_flow_and_its_deadline_survive_the_state_record(self):
        state = self.state()
        self.assertEqual({"131": 20.0},
                         NetworkState.from_record(state.to_record()).echo_deadline_ms)

    def test_the_description_states_what_the_ratio_does_not_model(self):
        described = JointEffectPredictor(self.state()).describe()
        self.assertIn("deadlineSuccessRatio", described["deadlineSuccess"])
        self.assertIn("not modelled", described["deadlineSuccess"])
        self.assertEqual({"131": 20.0}, described["echoDeadlineMs"])
