"""The T x C data model v2: steps and bounds, the cost expansion, functions.

Everything here is pure and deterministic, so everything here is asserted
directly: no backend, no clock, no file system beyond one temp directory.

What is pinned down is the arithmetic and the refusals the paper's metrics
depend on.  Relaxation is a number of **steps** toward a **bound** and the
bound is the floor, so ``T`` is the whole product of the authorized levels
ranked by ``sum_i w_i q_i^2`` -- squared, which is what stops the search from
piling every concession on one owner.  A joint condition removes the
combinations that would ride over a protected minimum.  A control candidate is
the **functions** used together, and translating them onto the action axes is
what the deployment can actually apply -- under both rules for the functions a
candidate did not select.  ``q_r`` is still exactly ``exp_metrics.md`` section
2, one trial still fills a whole column, and a value past the bound is still
flagged unauthorized rather than clipped into a success.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from assurance.coordination import (
    AXIS_SCOPE_KINDS, FUNCTION_NAMING, FunctionSpec, PolicyField, axis_kind,
    scope_for,
    FAIL, PASS, RULE_BASELINE, RULE_KEEP_CURRENT, UNKNOWN, Authorization,
    CompatibilityRules, ControlCandidate, ControlCandidates, ControlValidationError,
    EpisodeRecord, FunctionCatalog, FunctionSelection, Grid, Intent, JointCondition,
    Observation, Requirement, Target,
    TargetValidationError, Trial, concession_of, cost_of, preference_key,
    catalog_product_controls,
    deterministic_controls,
    deterministic_function_moves, deterministic_targets, deterministic_trajectory,
    expand_targets, intent_from_sentence, judge, judge_target, kpi_gaps, kpi_key,
    verdict_of,
    observed_best, translate_functions, validate_control_candidates,
    validate_target_contract,
    KPI_DEADLINE_RATIO,
)

HOME, AWAY = "12345678", "87654321"

#: The three functions this sitting exposes, exactly the shape of contract v2
#: section 3.1.  MCS, TX attenuation and slice quota stay unwired, so they are
#: not here: a model may only select what the deployment actually offers.
CATALOG = FunctionCatalog.from_record([
    {"functionId": "steer", "xapp": "traffic-steering",
     "actionId": "AIC_UECellSteering_1.0.0", "scopes": ["ue@131", "ue@132"],
     "policyFields": {"servingCell": {"values": [HOME, AWAY], "unit": "nci"}},
     "prerequisites": ["the UE is attached", "the target cell is advertised"],
     "axis": "servingCell@<ue>"},
    {"functionId": "ue-dl-prb-cap", "xapp": "our_rc_xapp", "actionId": "102",
     "scopes": ["ue@132"],
     "policyFields": {"maxDlPrbs": {"values": [24, 12, 6], "unit": "PRB",
                                    "baseline": 24}},
     "axis": "dlPrbCap@<ue>"},
    {"functionId": "ue-sched-priority", "xapp": "our_rc_xapp", "actionId": "103",
     "scopes": ["ue@131"],
     "policyFields": {"pfWeight": {"values": [4, 8, 12], "unit": "weight",
                                   "baseline": 8}},
     "axis": "pfWeight@<ue>"},
])

APPLIED = {"servingCell@131": HOME, "servingCell@132": HOME,
           "dlPrbCap@132": "24", "pfWeight@131": "8"}

#: The deployment's own co-use truth: cap and PF weight fight over the same
#: scheduler decision on one UE, and a steer is applied before a cap.
COMPATIBILITY = CompatibilityRules(
    mutually_exclusive=(("ue-dl-prb-cap", "ue-sched-priority"),),
    precedence=(("steer", "ue-dl-prb-cap"),),
    notes=("steer before cap on the same UE",))

ACTION_SPACE = {
    "servingCell@131": [HOME, AWAY],
    "dlPrbCap@132": ["24", "12", "6"],
    "pfWeight@131": ["4", "8", "12"],
}
BASELINE = {"servingCell@131": HOME, "dlPrbCap@132": "24", "pfWeight@131": "8"}


def two_intents():
    """Two owners of equal priority: two steps on one, one step on the other."""
    return [
        intent_from_sentence(
            "I1: UE ueId=131 video needs at least 3.0 Mbps downlink, relaxable in "
            "2 steps to 2.0, owner ue1-video priority 1"),
        intent_from_sentence(
            "I2: UE ueId=132 map data needs at least 1.5 Mbps downlink, relaxable "
            "in 1 steps to 1.0, owner ue2-map priority 1"),
    ]


class SentenceParsing(unittest.TestCase):
    """One operator sentence in, one structured requirement out."""

    def test_steps_and_a_bound_become_equally_spaced_levels(self):
        intent = intent_from_sentence(
            "I2: UE ueId=132 needs at least 1.5 Mbps downlink, relaxable in 2 steps "
            "to 1.0, owner ue2-map priority 2")
        requirement = intent.requirement
        self.assertEqual(requirement.steps, 2)
        self.assertEqual(requirement.bound, 1.0)
        self.assertEqual(requirement.levels, (1.5, 1.25, 1.0))
        self.assertEqual(requirement.floor, 1.0)
        self.assertEqual(requirement.value_at(1), 1.25)
        self.assertTrue(requirement.relaxation_stated)

    def test_a_bound_with_no_step_count_is_the_two_steps_v1_meant(self):
        requirement = intent_from_sentence(
            "I1: UE ueId=131 needs at least 3.0 Mbps downlink, relaxable to 2.0"
        ).requirement
        self.assertEqual(requirement.steps, 2)
        self.assertEqual(requirement.levels, (3.0, 2.5, 2.0))

    def test_a_sentence_with_no_relaxation_phrase_is_missing_not_refused(self):
        requirement = intent_from_sentence(
            "I3: UE ueId=133 needs at least 1.0 Mbps downlink, owner ue3-incumbent"
        ).requirement
        self.assertIsNone(requirement.steps)
        self.assertFalse(requirement.relaxation_stated)
        self.assertEqual(requirement.levels, (1.0,))

    def test_saying_it_out_loud_is_a_refusal_and_records_zero_steps(self):
        requirement = intent_from_sentence(
            "I3: UE ueId=133 needs at least 1.0 Mbps downlink, non-relaxable"
        ).requirement
        self.assertEqual(requirement.steps, 0)
        self.assertTrue(requirement.relaxation_stated)

    def test_a_v1_record_with_a_bare_relax_limit_still_reads(self):
        requirement = Requirement.from_record(
            {"reqId": "I1.r1", "kpi": "dlGoodputMbps", "scope": "ue@131", "op": ">=",
             "value": 3.0, "unit": "Mbps", "relaxLimit": 2.0, "relaxable": True})
        self.assertEqual(requirement.steps, 2)
        self.assertEqual(requirement.bound, 2.0)
        self.assertEqual(requirement.relax_limit, 2.0)



class AStructuredRecordNeedsNoSeparateScope(unittest.TestCase):
    """The Cockpit form and --intents-json name the UE once, on the intent."""

    @staticmethod
    def _record(**overrides):
        requirement = {"reqId": "I1.r1", "kpi": "dlGoodputMbps", "op": ">=",
                       "value": 3.0, "unit": "Mbps", "steps": 2, "bound": 2.0}
        requirement.update(overrides.pop("requirement", {}))
        record = {"intentId": "I1", "owner": "ue1-video", "ueId": "131",
                  "requirement": requirement}
        record.update(overrides)
        return record

    def test_the_requirement_scope_comes_from_the_intents_ue(self):
        intent = Intent.from_record(self._record())
        self.assertEqual("ue@131", intent.requirement.scope)
        self.assertEqual("dlGoodputMbps@131", kpi_key(intent.requirement.kpi,
                                                      intent.requirement.scope))

    def test_an_explicit_scope_is_never_overwritten(self):
        intent = Intent.from_record(self._record(requirement={"scope": "ue@999"}))
        self.assertEqual("ue@999", intent.requirement.scope)

    def test_an_intent_with_no_ue_keeps_an_empty_scope(self):
        intent = Intent.from_record(self._record(ueId=""))
        self.assertEqual("", intent.requirement.scope)

class TheWeights(unittest.TestCase):
    """``w_i`` is the operator's, or 1 + (max priority - this priority)."""

    def test_a_higher_priority_earns_a_larger_weight(self):
        intents = [
            intent_from_sentence("I1: UE ueId=131 needs at least 3.0 Mbps downlink, "
                                 "relaxable in 2 steps to 2.0, priority 1"),
            intent_from_sentence("I2: UE ueId=132 needs at least 1.5 Mbps downlink, "
                                 "relaxable in 1 steps to 1.0, priority 3"),
        ]
        weights = Authorization.from_intents(intents).weights()
        self.assertEqual(weights, {"I1.r1": 3.0, "I2.r1": 1.0})

    def test_an_explicit_weight_wins_over_the_priority_default(self):
        intent = intent_from_sentence(
            "I1: UE ueId=131 needs at least 3.0 Mbps downlink, relaxable in 2 steps "
            "to 2.0, priority 3")
        weights = Authorization.from_intents(
            [Intent(intent_id=intent.intent_id, owner=intent.owner,
                    ue_id=intent.ue_id, requirement=intent.requirement,
                    priority=intent.priority, weight=7.5)]).weights()
        self.assertEqual(weights["I1.r1"], 7.5)


class TheExpansion(unittest.TestCase):
    """``T`` is the whole product of the authorized levels, ranked by cost."""

    def setUp(self):
        self.authorization = Authorization.from_intents(two_intents())
        self.contract = expand_targets(self.authorization)

    def test_the_product_of_the_levels_is_the_number_of_targets(self):
        # (2 steps + 1) x (1 step + 1) = 6, T0 included
        self.assertEqual(len(self.contract.targets), 6)
        self.assertEqual(self.contract.t0.requirements,
                         {"I1.r1": 3.0, "I2.r1": 1.5})
        self.assertEqual(self.contract.t0.levels, {"I1.r1": 0, "I2.r1": 0})

    def test_t0_is_first_and_the_rest_increase_in_owner_concession(self):
        # Contract v2 section 3: the ranking is lexicographic over the owners'
        # *normalized* concessions, not over sum_i w_i q_i^2.  The squared cost
        # is still computed and recorded on every target -- it is just not the
        # order any more, and along this order it is visibly not monotone.
        keys = [preference_key(target, self.authorization)
                for target in self.contract.targets]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(keys[0], (0.0, 0.0, 0.0, 0.0))
        costs = [target.cost for target in self.contract.targets]
        self.assertEqual(costs[0], 0.0)
        self.assertEqual(costs, [0.0, 1.0, 1.0, 4.0, 2.0, 5.0])
        self.assertNotEqual(costs, sorted(costs))

    def test_normalizing_changes_which_concession_is_preferred(self):
        # Under sum_i w_i q_i^2 one step on each owner (2) beat two steps on one
        # (4), because an integer level counts subdivisions: I1 is cut into two
        # steps and I2 into one, so the same full concession was charged 4w on
        # one owner and w on the other.  Normalized, two steps on I1 is all of
        # what that owner signed and nothing from the other -- (D_max, D_mean)
        # of (1, 0.5) against the spread target's (1, 0.75) -- so the piled
        # target now ranks first and the squared cost no longer decides.
        spread = next(t for t in self.contract.targets
                      if t.levels == {"I1.r1": 1, "I2.r1": 1})
        piled = next(t for t in self.contract.targets
                     if t.levels == {"I1.r1": 2, "I2.r1": 0})
        self.assertLess(spread.cost, piled.cost)
        order = list(self.contract.target_ids)
        self.assertLess(order.index(piled.target_id), order.index(spread.target_id))

    def test_a_cost_tie_is_broken_by_d_max_then_by_intent_order(self):
        # both single-step targets cost 1; the one whose normalized concession
        # is smaller (I1 moves half its interval, I2 all of it) comes first
        tied = [t for t in self.contract.targets if t.cost == 1.0]
        self.assertEqual(len(tied), 2)
        first, second = tied
        self.assertEqual(first.levels, {"I1.r1": 1, "I2.r1": 0})
        self.assertEqual(second.levels, {"I1.r1": 0, "I2.r1": 1})
        self.assertLess(concession_of(first, self.authorization)["max"],
                        concession_of(second, self.authorization)["max"])

    def test_every_level_stays_inside_the_owners_own_bound(self):
        for target in self.contract.targets:
            self.assertGreaterEqual(target.requirements["I1.r1"], 2.0)
            self.assertLessEqual(target.requirements["I1.r1"], 3.0)
            self.assertGreaterEqual(target.requirements["I2.r1"], 1.0)

    def test_a_joint_condition_removes_the_combinations_it_refuses(self):
        authorization = Authorization.from_intents(
            two_intents(),
            joint_conditions=[JointCondition(kpi="dlGoodputMbps@132", op=">=",
                                             value=1.25, unit="Mbps",
                                             owner="ue3-incumbent")])
        contract = expand_targets(authorization)
        # I2's only relaxed level (1.0) is below the protected minimum, so the
        # whole column of combinations that use it is gone
        self.assertEqual(len(contract.targets), 3)
        for target in contract.targets:
            self.assertEqual(target.requirements["I2.r1"], 1.5)

    def test_a_non_relaxable_requirement_contributes_exactly_one_level(self):
        intents = two_intents() + [intent_from_sentence(
            "I3: UE ueId=133 needs at least 1.0 Mbps downlink, non-relaxable, "
            "owner ue3-incumbent")]
        contract = expand_targets(Authorization.from_intents(intents))
        self.assertEqual(len(contract.targets), 6)
        for target in contract.targets:
            self.assertEqual(target.requirements["I3.r1"], 1.0)

    def test_the_deterministic_contract_is_that_same_expansion(self):
        deterministic = deterministic_targets(two_intents())
        self.assertEqual([t.requirements for t in deterministic.targets],
                         [t.requirements for t in self.contract.targets])

    def test_an_absurd_authorization_says_so_instead_of_hanging(self):
        many = [intent_from_sentence(
            f"I{index}: UE ueId=13{index} needs at least 3.0 Mbps downlink, "
            "relaxable in 40 steps to 2.0") for index in range(1, 5)]
        with self.assertRaises(TargetValidationError) as caught:
            expand_targets(Authorization.from_intents(many))
        self.assertIn("targets", str(caught.exception))

    def test_cost_of_reads_a_target_that_carries_no_levels(self):
        bare = Target(target_id="TX", requirements={"I1.r1": 2.0, "I2.r1": 1.5})
        self.assertEqual(cost_of(bare, self.authorization), 4.0)


class TheCompactContract(unittest.TestCase):
    """What the Target agent returns, and what the executor does with it."""

    def setUp(self):
        self.authorization = Authorization.from_intents(two_intents())

    def compact(self, **overrides):
        record = {
            "t0": {"targetId": "T0", "requirements": {"I1.r1": 3.0, "I2.r1": 1.5}},
            "levels": {"I1.r1": {"steps": 2, "bound": 2.0},
                       "I2.r1": {"steps": 1, "bound": 1.0}},
            "constraints": ["I2 never below 1.0 Mbps"],
            "ranking": {"costRule": "sum_w_q2", "tieBreak": "by owner order"},
        }
        record.update(overrides)
        return record

    def test_it_expands_into_the_same_contract_the_executor_would_build(self):
        """The levels, bounds and ranking survive the round trip.

        The answer names no addition, so ``T`` is the mandatory set -- ``T0``
        and the one maximally weakened target of this two-requirement domain
        (v3.1, amendment section 3.2).  Choosing *more* is the agent's job and
        the executor does not do it for them.  What this pins is that the
        *expansion the selection draws from* is the one the executor would have
        built.
        """
        contract, notes = validate_target_contract(self.compact(), self.authorization)
        self.assertEqual(notes, ["the answer selected no additional target; T is the 2 mandatory targets",
                                 "T holds 2 mandatory and 0 model-selected targets of 6 authorized"])
        self.assertEqual(len(contract.targets), 2)
        self.assertEqual(6, len(expand_targets(contract.authorization).targets))
        self.assertEqual(contract.preference.tie_break, "by owner order")
        self.assertEqual(contract.provenance["constraints"],
                         ["I2 never below 1.0 Mbps"])

    def test_a_finer_subdivision_of_the_same_interval_is_allowed(self):
        """Four steps on the same signed interval is five levels, not a breach.

        Asserted on the expansion the adjusted authorization yields, since the
        answer itself selected nothing.
        """
        contract, notes = validate_target_contract(
            self.compact(levels={"I1.r1": {"steps": 4, "bound": 2.0},
                                 "I2.r1": {"steps": 1, "bound": 1.0}}),
            self.authorization)
        self.assertEqual(notes, ["the answer selected no additional target; T is the 2 mandatory targets",
                                 "T holds 2 mandatory and 0 model-selected targets of 10 authorized"])
        self.assertEqual(10, len(expand_targets(contract.authorization).targets))

    def test_a_bound_past_the_signed_one_is_clipped_and_reported(self):
        contract, notes = validate_target_contract(
            self.compact(levels={"I1.r1": {"steps": 2, "bound": 0.5},
                                 "I2.r1": {"steps": 1, "bound": 1.0}}),
            self.authorization)
        self.assertTrue(any("outside" in note for note in notes))
        for target in contract.targets:
            self.assertGreaterEqual(target.requirements["I1.r1"], 2.0)

    def test_a_non_relaxable_requirement_may_not_be_given_steps(self):
        intents = two_intents() + [intent_from_sentence(
            "I3: UE ueId=133 needs at least 1.0 Mbps downlink, non-relaxable")]
        authorization = Authorization.from_intents(intents)
        contract, notes = validate_target_contract(
            self.compact(t0={"targetId": "T0",
                             "requirements": {"I1.r1": 3.0, "I2.r1": 1.5,
                                              "I3.r1": 1.0}},
                         levels={"I3.r1": {"steps": 3, "bound": 0.2}}), authorization)
        self.assertTrue(any("non-relaxable" in note for note in notes))
        for target in contract.targets:
            self.assertEqual(target.requirements["I3.r1"], 1.0)

    def test_a_t0_that_moved_off_its_original_is_a_fallback(self):
        with self.assertRaises(TargetValidationError):
            validate_target_contract(
                self.compact(t0={"targetId": "T0",
                                 "requirements": {"I1.r1": 2.5, "I2.r1": 1.5}}),
                self.authorization)

    def test_an_unknown_requirement_id_is_refused(self):
        with self.assertRaises(TargetValidationError):
            validate_target_contract(
                self.compact(levels={"I9.r1": {"steps": 1, "bound": 0.5}}),
                self.authorization)

    def test_the_compact_form_round_trips_out_of_an_expanded_contract(self):
        contract = expand_targets(self.authorization)
        compact = contract.compact_record()
        self.assertEqual(compact["levels"]["I1.r1"], {"steps": 2, "bound": 2.0})
        self.assertEqual(compact["ranking"]["costRule"], "normalized-concession")


class TheConcession(unittest.TestCase):
    """``q_r``, ``D_o``, ``D_mean`` and ``D_max`` -- exp_metrics section 2."""

    def setUp(self):
        self.authorization = Authorization.from_intents(two_intents())

    def test_a_floor_relaxed_half_way_is_half_a_concession(self):
        target = Target(target_id="TA", requirements={"I1.r1": 2.5, "I2.r1": 1.5})
        quality = concession_of(target, self.authorization)
        self.assertAlmostEqual(quality["perRequirement"]["I1.r1"], 0.5)
        self.assertAlmostEqual(quality["perRequirement"]["I2.r1"], 0.0)
        self.assertAlmostEqual(quality["max"], 0.5)
        self.assertAlmostEqual(quality["mean"], 0.25)

    def test_a_value_past_the_bound_is_flagged_not_clipped(self):
        target = Target(target_id="TB", requirements={"I1.r1": 1.0, "I2.r1": 1.5})
        quality = concession_of(target, self.authorization)
        self.assertGreater(quality["perRequirement"]["I1.r1"], 1.0)
        self.assertIn("I1.r1", quality["invalid"])

    def test_a_strengthening_costs_nothing(self):
        target = Target(target_id="TC", requirements={"I1.r1": 4.0, "I2.r1": 1.5})
        quality = concession_of(target, self.authorization)
        self.assertEqual(quality["perRequirement"]["I1.r1"], 0.0)


class TheFunctionCatalog(unittest.TestCase):
    """Functions, their policies and scopes, and the axes they translate to."""

    def test_each_function_and_scope_spans_one_axis(self):
        self.assertEqual(sorted(CATALOG.axes()),
                         ["dlPrbCap@132", "pfWeight@131", "servingCell@131",
                          "servingCell@132"])
        self.assertEqual(CATALOG.axes()["dlPrbCap@132"], ("24", "12", "6"))

    def test_the_baseline_of_a_stateful_axis_comes_from_what_is_applied(self):
        baselines = CATALOG.baselines(APPLIED)
        self.assertEqual(baselines["servingCell@131"], HOME)
        self.assertEqual(baselines["dlPrbCap@132"], "24")
        self.assertEqual(baselines["pfWeight@131"], "8")

    def test_it_round_trips_through_its_record(self):
        again = FunctionCatalog.from_record(CATALOG.to_record())
        self.assertEqual(again.axes(), CATALOG.axes())
        self.assertEqual(again.function("steer").prerequisites,
                         CATALOG.function("steer").prerequisites)


class TheTranslation(unittest.TestCase):
    """Selected functions become axis values; unselected ones follow the rule."""

    def candidate(self, *selections):
        return ControlCandidate(control_id="C3", functions=selections)

    def test_baseline_deactivates_every_axis_no_function_selected(self):
        applied = dict(APPLIED, dlPrbCap132="ignored")
        applied = dict(APPLIED)
        applied["pfWeight@131"] = "12"          # something is running right now
        configuration = translate_functions(
            self.candidate(FunctionSelection("ue-dl-prb-cap", "ue@132",
                                             {"maxDlPrbs": "12"})),
            CATALOG, CATALOG.baselines(APPLIED), applied, RULE_BASELINE)
        self.assertEqual(configuration["dlPrbCap@132"], "12")
        self.assertEqual(configuration["pfWeight@131"], "8")   # back to baseline

    def test_keep_current_leaves_the_running_functions_where_they_are(self):
        applied = dict(APPLIED)
        applied["pfWeight@131"] = "12"
        configuration = translate_functions(
            self.candidate(FunctionSelection("ue-dl-prb-cap", "ue@132",
                                             {"maxDlPrbs": "12"})),
            CATALOG, CATALOG.baselines(APPLIED), applied, RULE_KEEP_CURRENT)
        self.assertEqual(configuration["dlPrbCap@132"], "12")
        self.assertEqual(configuration["pfWeight@131"], "12")  # left alone

    def test_two_functions_used_together_write_two_axes(self):
        configuration = translate_functions(
            self.candidate(FunctionSelection("steer", "ue@131", {"servingCell": AWAY}),
                           FunctionSelection("ue-dl-prb-cap", "ue@132",
                                             {"maxDlPrbs": "12"})),
            CATALOG, CATALOG.baselines(APPLIED), APPLIED)
        self.assertEqual(configuration["servingCell@131"], AWAY)
        self.assertEqual(configuration["dlPrbCap@132"], "12")

    def test_an_unknown_function_is_refused(self):
        with self.assertRaises(ControlValidationError):
            translate_functions(self.candidate(
                FunctionSelection("mcs-clamp", "ue@131", {"mcs": "9"})),
                CATALOG, CATALOG.baselines(APPLIED), APPLIED)

    def test_a_scope_the_function_does_not_serve_is_refused(self):
        with self.assertRaises(ControlValidationError):
            translate_functions(self.candidate(
                FunctionSelection("ue-dl-prb-cap", "ue@131", {"maxDlPrbs": "12"})),
                CATALOG, CATALOG.baselines(APPLIED), APPLIED)

    def test_a_bare_ue_name_resolves_to_the_one_listed_scope_it_names(self):
        # 2026-09-27 v5.2: "ue3" for "ue@ue3" dropped 8 of 10 IM candidates.
        ok = translate_functions(self.candidate(
            FunctionSelection("ue-dl-prb-cap", "132", {"maxDlPrbs": "12"})),
            CATALOG, CATALOG.baselines(APPLIED), APPLIED)
        good = translate_functions(self.candidate(
            FunctionSelection("ue-dl-prb-cap", "ue@132", {"maxDlPrbs": "12"})),
            CATALOG, CATALOG.baselines(APPLIED), APPLIED)
        self.assertEqual(good, ok)
        with self.assertRaises(ControlValidationError):
            translate_functions(self.candidate(
                FunctionSelection("ue-dl-prb-cap", "131", {"maxDlPrbs": "12"})),
                CATALOG, CATALOG.baselines(APPLIED), APPLIED)

    def test_a_policy_value_off_the_list_is_refused(self):
        with self.assertRaises(ControlValidationError):
            translate_functions(self.candidate(
                FunctionSelection("ue-dl-prb-cap", "ue@132", {"maxDlPrbs": "18"})),
                CATALOG, CATALOG.baselines(APPLIED), APPLIED)

    def test_two_functions_writing_one_axis_is_refused(self):
        with self.assertRaises(ControlValidationError):
            translate_functions(self.candidate(
                FunctionSelection("steer", "ue@131", {"servingCell": AWAY}),
                FunctionSelection("steer", "ue@131", {"servingCell": HOME})),
                CATALOG, CATALOG.baselines(APPLIED), APPLIED)


class TheCompatibilityRules(unittest.TestCase):
    """The deployment's own co-use truth, applied by the executor."""

    def test_two_mutually_exclusive_functions_on_one_ue_are_refused(self):
        refusals = COMPATIBILITY.refusals((
            FunctionSelection("ue-dl-prb-cap", "ue@132", {"maxDlPrbs": "12"}),
            FunctionSelection("ue-sched-priority", "ue@132", {"pfWeight": "12"})))
        self.assertTrue(refusals)

    def test_the_same_two_on_different_ues_are_allowed(self):
        self.assertEqual(COMPATIBILITY.refusals((
            FunctionSelection("ue-dl-prb-cap", "ue@132", {"maxDlPrbs": "12"}),
            FunctionSelection("ue-sched-priority", "ue@131", {"pfWeight": "12"}))), [])

    def test_precedence_orders_a_steer_before_a_cap_on_the_same_ue(self):
        cap = FunctionSelection("ue-dl-prb-cap", "ue@132", {"maxDlPrbs": "12"})
        steer = FunctionSelection("steer", "ue@132", {"servingCell": AWAY})
        self.assertEqual([item.function_id for item in COMPATIBILITY.ordered((cap, steer))],
                         ["steer", "ue-dl-prb-cap"])


class ThePerConfigurationCap(unittest.TestCase):
    """"Use the common limit of four changed function/scope entries per
    configuration" -- applied at ``refusals()``, the one site every arm reaches:
    the basic monolith calls it directly, the model arms reach it through
    ``validate_control_candidates``, and both enumerators ask it before they
    emit a combination.
    """

    def selections(self, count):
        return tuple(FunctionSelection("steer", f"ue@{130 + index}",
                                       {"servingCell": AWAY})
                     for index in range(count))

    def test_a_selection_over_the_cap_is_refused_naming_both_numbers(self):
        refusals = CompatibilityRules(max_changed_entries=4).refusals(
            self.selections(5))
        self.assertEqual(1, len(refusals))
        self.assertIn("5", refusals[0])
        self.assertIn("4", refusals[0])

    def test_the_per_scope_knob_cannot_express_this(self):
        # Five entries on five scopes are five groups of one, so the by-scope
        # test never fires -- which is why the cap is not that knob.
        self.assertEqual([], CompatibilityRules(
            max_functions_per_scope=4).refusals(self.selections(5)))

    def test_at_the_cap_and_under_it_nothing_is_refused(self):
        rules = CompatibilityRules(max_changed_entries=4)
        self.assertEqual([], rules.refusals(self.selections(4)))
        self.assertEqual([], rules.refusals(self.selections(1)))

    def test_the_default_is_none_so_nothing_changes(self):
        self.assertIsNone(CompatibilityRules().max_changed_entries)
        self.assertEqual([], CompatibilityRules().refusals(self.selections(9)))
        self.assertEqual([], COMPATIBILITY.refusals(self.selections(9)))

    def test_the_cap_round_trips_through_the_record(self):
        rules = CompatibilityRules(max_changed_entries=4)
        self.assertEqual(4, rules.to_record()["maxChangedEntries"])
        self.assertEqual(rules, CompatibilityRules.from_record(rules.to_record()))
        # A record written before the cap existed reads back uncapped.
        self.assertIsNone(CompatibilityRules.from_record(
            {"maxFunctionsPerScope": None}).max_changed_entries)

    def test_a_configuration_only_candidate_is_capped_too(self):
        # It carries no functions, so it never reaches ``refusals()``: the cap
        # is applied against the same quantity ``changes_from`` counts.
        candidates = ControlCandidates(candidates=(
            ControlCandidate(control_id="CA", configuration={
                "servingCell@131": AWAY, "dlPrbCap@132": "12",
                "pfWeight@131": "12"}),), action_space=ACTION_SPACE)
        cleaned, dropped = validate_control_candidates(
            candidates, ACTION_SPACE, BASELINE,
            compatibility=CompatibilityRules(max_changed_entries=2))
        self.assertEqual(cleaned.control_ids, ("C0",))
        self.assertTrue(any("3" in note and "2" in note for note in dropped))

    def test_a_configuration_only_candidate_under_the_cap_survives(self):
        candidates = ControlCandidates(candidates=(
            ControlCandidate(control_id="CA",
                             configuration={"dlPrbCap@132": "12"}),),
            action_space=ACTION_SPACE)
        cleaned, dropped = validate_control_candidates(
            candidates, ACTION_SPACE, BASELINE,
            compatibility=CompatibilityRules(max_changed_entries=2))
        self.assertEqual(cleaned.control_ids, ("C0", "CA"))
        self.assertEqual([], dropped)


class RefusedCombinationsSayWhy(unittest.TestCase):
    """Both enumerators used to drop a refused combination with no cause."""

    CAPPED = CompatibilityRules(max_changed_entries=1)

    def test_the_product_records_what_it_refused(self):
        dropped = []
        rows = catalog_product_controls(CATALOG, CATALOG.baselines(APPLIED),
                                        compatibility=self.CAPPED,
                                        dropped=dropped)
        self.assertTrue(dropped)
        self.assertTrue(all(len(row.functions) == 1 for row in rows))
        self.assertTrue(any("per configuration" in note for note in dropped))
        self.assertTrue(any(" on ue@" in note for note in dropped))

    def test_the_moves_record_the_pairs_they_refused(self):
        dropped = []
        rows = deterministic_function_moves(CATALOG, CATALOG.baselines(APPLIED),
                                            compatibility=self.CAPPED,
                                            pairs=True, dropped=dropped)
        self.assertTrue(dropped)
        self.assertTrue(all(len(row.functions) == 1 for row in rows))
        self.assertTrue(any("per configuration" in note for note in dropped))

    def test_collecting_the_reasons_does_not_change_the_enumeration(self):
        for compatibility in (COMPATIBILITY, self.CAPPED):
            self.assertEqual(
                catalog_product_controls(CATALOG, CATALOG.baselines(APPLIED),
                                         compatibility=compatibility),
                catalog_product_controls(CATALOG, CATALOG.baselines(APPLIED),
                                         compatibility=compatibility,
                                         dropped=[]))
            self.assertEqual(
                deterministic_function_moves(CATALOG, CATALOG.baselines(APPLIED),
                                             compatibility=compatibility),
                deterministic_function_moves(CATALOG, CATALOG.baselines(APPLIED),
                                             compatibility=compatibility,
                                             dropped=[]))

    def test_an_uncapped_enumeration_refuses_nothing_and_records_nothing(self):
        dropped = []
        rows = deterministic_function_moves(CATALOG, CATALOG.baselines(APPLIED),
                                            compatibility=COMPATIBILITY,
                                            pairs=True, dropped=dropped)
        self.assertEqual([], dropped)
        self.assertGreater(len(rows), 6)


class ValidatingC(unittest.TestCase):
    """``C`` keeps only what the deployment can apply, and always has ``C0``."""

    def candidates(self, *rows):
        return ControlCandidates(candidates=tuple(rows), catalog=CATALOG,
                                 construction_policy={"retain": 8})

    def validate(self, candidates, **kwargs):
        options = dict(catalog=CATALOG, compatibility=COMPATIBILITY, applied=APPLIED,
                       rule=RULE_BASELINE, retain=8)
        options.update(kwargs)
        return validate_control_candidates(
            candidates, CATALOG.axes(), CATALOG.baselines(APPLIED), **options)

    def test_the_baseline_is_always_the_first_candidate(self):
        cleaned, _dropped = self.validate(self.candidates())
        self.assertEqual(cleaned.control_ids, ("C0",))
        self.assertEqual(cleaned.candidate("C0").configuration,
                         CATALOG.baselines(APPLIED))

    def test_a_candidate_that_breaks_a_compatibility_rule_is_excluded(self):
        cleaned, dropped = self.validate(self.candidates(
            ControlCandidate(control_id="CA", functions=(
                FunctionSelection("ue-dl-prb-cap", "ue@132", {"maxDlPrbs": "12"}),
                FunctionSelection("ue-sched-priority", "ue@132", {"pfWeight": "12"}))),
            ControlCandidate(control_id="CB", functions=(
                FunctionSelection("steer", "ue@131", {"servingCell": AWAY}),))))
        self.assertEqual(cleaned.control_ids, ("C0", "CB"))
        self.assertTrue(any("CA" in note for note in dropped))

    def test_two_candidates_that_translate_the_same_way_are_merged(self):
        cleaned, dropped = self.validate(self.candidates(
            ControlCandidate(control_id="CA", functions=(
                FunctionSelection("steer", "ue@131", {"servingCell": AWAY}),)),
            ControlCandidate(control_id="CB", functions=(
                FunctionSelection("steer", "ue@131", {"servingCell": AWAY}),))))
        self.assertEqual(cleaned.control_ids, ("C0", "CA"))
        self.assertTrue(any("same configuration" in note for note in dropped))

    def test_only_the_retained_number_of_candidates_survives_in_order(self):
        rows = [ControlCandidate(control_id=f"C{index}", functions=(
            FunctionSelection("ue-dl-prb-cap", "ue@132",
                              {"maxDlPrbs": value}),))
            for index, value in enumerate(("12", "6"), start=1)]
        cleaned, dropped = self.validate(self.candidates(*rows), retain=1)
        self.assertEqual(cleaned.control_ids, ("C0", "C1"))
        self.assertTrue(any("retains" in note for note in dropped))

    def test_the_v1_configuration_path_still_validates_without_a_catalog(self):
        candidates = ControlCandidates(candidates=(
            ControlCandidate(control_id="CA", configuration={"dlPrbCap@132": "12"}),
            ControlCandidate(control_id="CB", configuration={"dlPrbCap@132": "99"})),
            action_space=ACTION_SPACE)
        cleaned, dropped = validate_control_candidates(
            candidates, ACTION_SPACE, BASELINE)
        self.assertEqual(cleaned.control_ids, ("C0", "CA"))
        self.assertTrue(any("CB" in note for note in dropped))

    def test_the_deterministic_moves_enumerate_singles_then_pairs(self):
        singles = deterministic_function_moves(CATALOG, CATALOG.baselines(APPLIED),
                                               compatibility=COMPATIBILITY,
                                               pairs=False)
        self.assertEqual(len(singles), 6)
        for candidate in singles:
            self.assertEqual(len(candidate.functions), 1)
        with_pairs = deterministic_function_moves(CATALOG, CATALOG.baselines(APPLIED),
                                                  compatibility=COMPATIBILITY,
                                                  pairs=True)
        self.assertGreater(len(with_pairs), len(singles))
        for candidate in with_pairs:
            self.assertEqual(COMPATIBILITY.refusals(candidate.functions), [])


class Judgement(unittest.TestCase):
    """Measured numbers only; a missing window is never a success."""

    def setUp(self):
        self.intents = two_intents()
        self.authorization = Authorization.from_intents(self.intents)
        self.contract = expand_targets(self.authorization)

    def test_a_vector_that_meets_every_floor_passes_t0(self):
        kpis = {"dlGoodputMbps@131": 3.1, "dlGoodputMbps@132": 1.6}
        self.assertEqual(judge_target(kpis, self.contract.t0, self.authorization),
                         {"I1.r1": PASS, "I2.r1": PASS})
        self.assertEqual(judge(kpis, self.contract.t0, self.intents),
                         {"I1.r1": PASS, "I2.r1": PASS})

    def test_a_missing_observation_is_unknown_not_a_failure(self):
        verdicts = judge_target({"dlGoodputMbps@131": 3.1}, self.contract.t0,
                                self.authorization)
        self.assertEqual(verdicts["I2.r1"], UNKNOWN)

    def test_a_broken_joint_condition_fails_every_target(self):
        authorization = Authorization.from_intents(
            two_intents(),
            joint_conditions=[JointCondition(kpi="dlGoodputMbps@133", op=">=",
                                             value=0.5, unit="Mbps")])
        contract = expand_targets(authorization)
        kpis = {"dlGoodputMbps@131": 3.1, "dlGoodputMbps@132": 1.6,
                "dlGoodputMbps@133": 0.2}
        for target in contract.targets:
            verdicts = judge_target(kpis, target, authorization)
            self.assertEqual(verdicts["joint[0]:dlGoodputMbps@133"], FAIL)


class ObservedBestAndGaps(unittest.TestCase):
    """What the executor computes for Trajectory, never the model."""

    def setUp(self):
        self.authorization = Authorization.from_intents(two_intents())
        self.contract = expand_targets(self.authorization)

    def observation(self, index, control_id, kpis, valid=True):
        return Observation(control_id=control_id, trial_index=index, kpis=kpis,
                           window_end="2026-09-07T10:00:30Z",
                           valid_until="2026-09-07T10:01:30Z", valid=valid)

    def test_the_cheapest_supported_target_is_the_observed_best(self):
        best = observed_best([
            self.observation(1, "C1", {"dlGoodputMbps@131": 2.6,
                                       "dlGoodputMbps@132": 1.6}),
            self.observation(2, "C2", {"dlGoodputMbps@131": 2.1,
                                       "dlGoodputMbps@132": 1.1}),
        ], self.contract)
        self.assertEqual(best["controlId"], "C1")
        self.assertEqual(best["trialIndex"], 1)
        self.assertEqual(best["observationRefs"], ["trial:1"])
        self.assertEqual(best["cost"], 1.0)      # one step on I1 only

    def test_a_narrowed_arm_is_still_judged_on_the_owners_whole_omega(self):
        """The arm that listed only T0 still gets credit for what it met.

        Before 2026-09-18 ``observed_best`` walked ``contract.targets``, so an
        arm whose Target step had not listed a relaxed target could never be
        seen to have reached it -- and its PASS count was compared against
        basic-monolith's, which carries all of Omega.  Different denominators
        (handoff section 4.3).
        """
        narrowed = replace(self.contract, alternatives=())
        self.assertEqual(len(narrowed.targets), 1)          # T0 only
        kpis = {"dlGoodputMbps@131": 2.6, "dlGoodputMbps@132": 1.6}
        best = observed_best([self.observation(1, "C1", kpis)], narrowed)
        full = observed_best([self.observation(1, "C1", kpis)], self.contract)
        self.assertIsNotNone(best)
        self.assertEqual(best["targetId"], full["targetId"])
        self.assertEqual(best["cost"], full["cost"])
        # ...and the narrowing survives as a diagnostic, never as a filter.
        self.assertFalse(best["inPreparedT"])
        self.assertTrue(full["inPreparedT"])

    def test_a_kpi_the_window_never_carried_is_unknown_not_a_miss(self):
        """두 판정기가 조용히 어긋나 있었다 (2026-09-18).

        ``judge`` 는 빠진 KPI 를 ``UNKNOWN`` 으로 뒀는데 그 쌍둥이인
        ``judge_target`` 은 같은 경우를 ``FAIL`` 로 접었다.  둘 다 ``PASS`` 를
        막으니 ``observed_best`` 는 안전했지만, 미달을 세는 쪽은 "그 UE 가
        바닥을 못 맞췄다" 로 읽었다 — 진실은 "아무도 그 UE 를 재지 않았다" 다.
        """
        target = self.contract.t0
        both = judge_target({"dlGoodputMbps@131": 3.5, "dlGoodputMbps@132": 1.9},
                            target, self.authorization)
        self.assertNotIn(UNKNOWN, both.values())
        one_missing = judge_target({"dlGoodputMbps@131": 3.5},
                                   target, self.authorization)
        self.assertEqual(one_missing["I2.r1"], UNKNOWN)
        self.assertEqual(verdict_of(one_missing), UNKNOWN)

    def test_an_invalid_window_can_never_establish_a_target(self):
        best = observed_best([
            self.observation(1, "C1", {"dlGoodputMbps@131": 3.5,
                                       "dlGoodputMbps@132": 1.9}, valid=False)],
            self.contract)
        self.assertIsNone(best)

    def test_no_supported_target_is_null_not_an_empty_object(self):
        self.assertIsNone(observed_best([
            self.observation(1, "C1", {"dlGoodputMbps@131": 0.5,
                                       "dlGoodputMbps@132": 0.4})], self.contract))

    def test_the_gaps_name_the_shortfall_against_t0_and_the_next_target(self):
        gaps = kpi_gaps({"dlGoodputMbps@131": 2.4, "dlGoodputMbps@132": 1.6},
                        self.contract)
        row = gaps["perRequirement"]["I1.r1"]
        self.assertEqual(row["owner"], "ue1-video")
        self.assertEqual(row["unit"], "Mbps")
        self.assertAlmostEqual(row["againstT0"]["shortfall"], 0.6)
        self.assertEqual(row["againstT0"]["verdict"], FAIL)
        self.assertIsNotNone(row["againstNext"])
        self.assertEqual(gaps["perRequirement"]["I2.r1"]["againstT0"]["shortfall"], 0.0)

    def test_no_kpi_at_all_is_null(self):
        self.assertIsNone(kpi_gaps({}, self.contract))
        self.assertIsNone(kpi_gaps(None, self.contract))


class TheGrid(unittest.TestCase):
    """One trial fills a whole column: every target judged from one vector."""

    def setUp(self):
        self.intents = two_intents()
        self.authorization = Authorization.from_intents(self.intents)
        self.contract = expand_targets(self.authorization)
        self.controls = validate_control_candidates(
            ControlCandidates(candidates=(
                ControlCandidate(control_id="C1", functions=(
                    FunctionSelection("steer", "ue@131", {"servingCell": AWAY}),)),),
                catalog=CATALOG),
            CATALOG.axes(), CATALOG.baselines(APPLIED), catalog=CATALOG,
            applied=APPLIED)[0]
        self.grid = Grid(self.contract, self.controls)

    def test_one_trial_writes_every_target_row_for_its_control(self):
        trial = Trial(trial_index=1, control_id="C1",
                      configuration=dict(APPLIED),
                      kpis={"dlGoodputMbps@131": 2.6, "dlGoodputMbps@132": 1.6},
                      window={"valid": True})
        trial.judge_against(self.contract, self.intents)
        self.grid.record(trial)
        self.assertEqual(self.grid.cell("T0", "C1"), FAIL)      # 2.6 < 3.0
        relaxed = next(t for t in self.contract.targets
                       if t.levels == {"I1.r1": 1, "I2.r1": 0})
        self.assertEqual(self.grid.cell(relaxed.target_id, "C1"), PASS)

    def test_an_invalid_window_writes_unknown_everywhere(self):
        trial = Trial(trial_index=1, control_id="C1",
                      kpis={"dlGoodputMbps@131": 3.5, "dlGoodputMbps@132": 1.9},
                      window={"valid": False, "reason": "coverage 0.3"})
        trial.judge_against(self.contract, self.intents)
        self.grid.record(trial)
        for target_id in self.contract.target_ids:
            self.assertEqual(self.grid.cell(target_id, "C1"), UNKNOWN)
        self.assertIsNone(self.grid.best_attained(self.contract))

    def test_best_attained_takes_the_lowest_cost_success(self):
        for index, kpis in enumerate((
                {"dlGoodputMbps@131": 2.1, "dlGoodputMbps@132": 1.1},
                {"dlGoodputMbps@131": 2.6, "dlGoodputMbps@132": 1.6}), start=1):
            trial = Trial(trial_index=index, control_id=f"C{index}", kpis=kpis,
                          window={"valid": True})
            trial.judge_against(self.contract, self.intents)
            self.grid.record(trial)
        best = self.grid.best_attained(self.contract)
        self.assertEqual(best.trial_index, 2)

    def test_the_deterministic_trajectory_picks_an_untried_control(self):
        decision = deterministic_trajectory(self.contract, self.controls, self.grid,
                                            APPLIED)
        self.assertIn(decision.control_id, self.controls.control_ids)


class TheEpisodeRecord(unittest.TestCase):
    """The JSON the metrics and the figures read."""

    def test_it_round_trips_and_names_its_schema(self):
        intents = two_intents()
        contract = expand_targets(Authorization.from_intents(intents))
        controls = deterministic_controls(ACTION_SPACE, BASELINE, 4)
        episode = EpisodeRecord(episode_id="E1", method="three-agent",
                                intents=tuple(intents), contract=contract,
                                controls=controls,
                                trials=[Trial(trial_index=1, control_id="C1")])
        record = episode.to_record()
        self.assertEqual(record["schemaVersion"], "agent-episode/1.1.0")
        self.assertEqual(record["T"]["weights"], {"I1.r1": 1.0, "I2.r1": 1.0})
        self.assertIn("compact", record["T"])
        self.assertIn("measurementRules", record)
        self.assertIn("intake", record)
        again = EpisodeRecord.from_record(record)
        self.assertEqual(again.contract.target_ids, contract.target_ids)
        self.assertEqual(again.intents[0].requirement.steps, 2)

    def test_the_measurement_rules_and_the_intake_survive_the_round_trip(self):
        episode = EpisodeRecord(
            episode_id="E2", method="three-agent",
            measurement_rules={"dlGoodputMbps": {"settleMs": 5000, "windowMs": 30000,
                                                 "statistic": "mean",
                                                 "minCoverage": 0.8,
                                                 "validityMs": 60000}},
            intake={"missing": [{"intentId": "I2", "field": "bound",
                                 "question": "what is the bound?"}],
                    "answers": {"I2": {"steps": 2, "bound": 1.0}}, "rounds": 1})
        again = EpisodeRecord.from_record(episode.to_record())
        self.assertEqual(again.measurement_rules["dlGoodputMbps"]["validityMs"], 60000)
        self.assertEqual(again.intake["rounds"], 1)


if __name__ == "__main__":
    unittest.main()


class TheFunctionCatalogNamesEveryScopeKind(unittest.TestCase):
    """Contract v3 section 3: three of the six axes are not a UE's.

    The axis template names the *kind* of scope a function is written at, and
    substitution is the same for all of them.  What this pins is that a
    cell-scoped or slice-scoped function resolves to its own axis rather than
    silently falling through to the un-substituted template -- which would put
    two cells' MCS bounds on one axis and freeze a catalog that cannot be
    applied.
    """

    def spec(self, kind, scope, values):
        naming = FUNCTION_NAMING[kind]
        field_name = str(naming["policyField"])
        return FunctionSpec(
            function_id=str(naming["functionId"]), xapp=str(naming["xapp"]),
            scopes=(scope,), axis=str(naming["axisTemplate"]),
            axis_field=field_name,
            policy_fields={field_name: PolicyField(
                name=field_name, values=values, unit=str(naming["unit"]),
                baseline=values[0])})

    def test_every_declared_kind_substitutes_its_own_scope_target(self):
        cases = {
            "servingCell": ("ue@131", "servingCell@131"),
            "dlPrbCap": ("ue@132", "dlPrbCap@132"),
            "pfWeight": ("ue@132", "pfWeight@132"),
            "dlMcsBounds": ("cell@12345678", "dlMcsBounds@12345678"),
            "txAttenuationDb": ("cell@87654321", "txAttenuationDb@87654321"),
            "slicePrbQuota": ("slice@1", "slicePrbQuota@1"),
        }
        for kind, (scope, axis) in cases.items():
            spec = self.spec(kind, scope, ("a", "b"))
            self.assertEqual(axis, spec.axis_for(scope), kind)
            self.assertNotIn("<", spec.axis_for(scope))

    def test_the_catalog_spans_one_axis_per_function_and_scope(self):
        catalog = FunctionCatalog((
            self.spec("dlMcsBounds", "cell@12345678", ("0..28", "0..16")),
            self.spec("slicePrbQuota", "slice@1", ("0:1:100", "0:1:30")),
        ))
        self.assertEqual({"dlMcsBounds@12345678": ("0..28", "0..16"),
                          "slicePrbQuota@1": ("0:1:100", "0:1:30")},
                         catalog.axes())
        self.assertEqual({"dlMcsBounds@12345678": "0..28",
                          "slicePrbQuota@1": "0:1:100"}, catalog.baselines())
        spec, scope = catalog.axis_owner("slicePrbQuota@1")
        self.assertEqual("slice@1", scope)
        self.assertEqual("slice-prb-quota", spec.function_id)

    def test_the_scope_of_an_axis_is_the_inverse_of_its_template(self):
        for axis, scope in (("servingCell@131", "ue@131"),
                            ("dlPrbCap@132", "ue@132"),
                            ("dlMcsBounds@12345678", "cell@12345678"),
                            ("txAttenuationDb@12345678", "cell@12345678"),
                            ("slicePrbQuota@1", "slice@1")):
            self.assertEqual(scope, scope_for(axis))
            self.assertEqual(axis.split("@", 1)[0], axis_kind(axis))

    def test_an_axis_nobody_declared_has_no_scope(self):
        with self.assertRaisesRegex(ValueError, "not one of the declared"):
            scope_for("cellBandwidth@12345678")

    def test_the_naming_table_states_a_template_and_a_scope_kind_for_each(self):
        for kind, naming in FUNCTION_NAMING.items():
            self.assertTrue(str(naming["axisTemplate"]).startswith(kind + "@<"))
            self.assertEqual(AXIS_SCOPE_KINDS[kind], naming["scopeKind"])
            self.assertTrue(naming["functionId"] and naming["xapp"])
            self.assertTrue(naming["prerequisites"])


class TheDeadlineSuccessRatio(unittest.TestCase):
    """Scenario I4: a fraction within D1, and D1 signed separately from R1.

    The two concessions are two levels of the same intent, so the expansion is
    their product, the cost charges both at the intent's own weight, and a
    deadline the owner never signed for is refused rather than clipped.
    """

    SENTENCE = ("I4: at least 95% of tagged echo requests within 50 ms for "
                "ueId=131 owner ue1-command, relaxable in 2 steps to 0.9, "
                "deadline relaxable in 1 step to 80 ms")

    def intent(self, sentence=None):
        return intent_from_sentence(sentence or self.SENTENCE)

    def test_the_sentence_reads_r1_the_deadline_and_two_authorizations(self):
        requirement = self.intent().requirement
        self.assertEqual(KPI_DEADLINE_RATIO, requirement.kpi)
        self.assertEqual("ue@131", requirement.scope)
        self.assertEqual("deadlineSuccessRatio@131", requirement.observation_key)
        self.assertEqual(">=", requirement.op)
        self.assertEqual(0.95, requirement.value)
        self.assertEqual("fraction", requirement.unit)
        self.assertEqual((0.95, 0.925, 0.9), requirement.levels)
        self.assertEqual(50.0, requirement.deadline_ms)
        self.assertEqual((50.0, 80.0), requirement.deadline_levels)

    def test_a_deadline_phrase_is_never_mistaken_for_a_ratio_bound(self):
        """"deadline relaxable in 1 step to 80 ms" is 80 ms, not a ratio of 80."""
        requirement = self.intent().requirement
        self.assertEqual(0.9, requirement.bound)
        self.assertEqual(80.0, requirement.deadline_bound)

    def test_a_percentage_and_a_bare_fraction_are_the_same_requirement(self):
        percent = self.intent(
            "I4: at least 95% of tagged echo requests within 50 ms for ueId=131")
        fraction = self.intent(
            "I4: at least 0.95 of tagged echo requests within 50 ms for ueId=131")
        self.assertEqual(percent.requirement.value, fraction.requirement.value)

    def test_a_deadline_in_seconds_is_the_same_deadline_in_milliseconds(self):
        self.assertEqual(
            50.0,
            self.intent("I4: at least 95% of tagged echo requests within 0.05 s "
                        "for ueId=131").requirement.deadline_ms)

    def test_a_sentence_with_no_deadline_leaves_it_missing_not_guessed(self):
        requirement = self.intent(
            "I4: at least 95% of tagged echo requests for ueId=131").requirement
        self.assertIsNone(requirement.deadline_ms)
        self.assertEqual((), requirement.deadline_levels)

    def test_a_fixed_ratio_stays_out_of_its_owners_denominator(self):
        """Contract v2 section 2: only what may move counts toward ``D_o``.

        This owner signed one concession -- the deadline -- and left the success
        ratio fixed.  Stretching the deadline all the way is therefore all of
        what the owner authorized, ``D_o = 1``.  Averaging the fixed ratio's
        permanent ``0`` in beside it would report ``0.5`` and halve every
        concession this owner ever makes.
        """
        intent = self.intent(
            "I4: at least 90% of tagged echo requests within 200 ms for "
            "ueId=131 owner ue1-command, deadline relaxable in 1 step to 300 ms")
        authorization = Authorization.from_intents([intent])
        entry = authorization.requirements["I4.r1"]
        self.assertFalse(entry.relaxable)            # the ratio itself is fixed
        self.assertTrue(entry.deadline_relaxable)    # only the deadline moves
        contract = expand_targets(authorization)
        stretched = contract.ranked()[-1]
        self.assertEqual({"I4.r1": 300.0}, stretched.deadlines)
        quality = concession_of(stretched, authorization)
        self.assertEqual({"ue1-command": 1.0}, quality["perOwner"])
        self.assertEqual(1.0, quality["max"])
        # the fixed ratio is still reported, just not averaged in
        self.assertEqual(0.0, quality["perRequirement"]["I4.r1"])

    def test_the_ratio_is_a_floor_and_a_ceiling_phrasing_is_refused(self):
        with self.assertRaisesRegex(ValueError, "floor"):
            self.intent("I4: at most 95% of tagged echo requests within 50 ms "
                        "for ueId=131")

    def test_the_requirement_round_trips_through_its_record(self):
        requirement = self.intent().requirement
        self.assertEqual(requirement, Requirement.from_record(requirement.to_record()))

    def test_a_requirement_with_no_deadline_writes_no_deadline_keys(self):
        goodput = intent_from_sentence(
            "I1: UE ueId=131 needs at least 3.0 Mbps downlink").requirement
        self.assertNotIn("deadlineMs", goodput.to_record())

    def test_the_expansion_is_the_product_of_both_level_axes(self):
        contract = deterministic_targets([self.intent()])
        self.assertEqual(3 * 2, len(contract.targets))
        self.assertEqual(0.95, contract.t0.requirements["I4.r1"])
        self.assertEqual(50.0, contract.t0.deadline_for("I4.r1"))
        self.assertEqual(
            {(0.95, 50.0), (0.95, 80.0), (0.925, 50.0), (0.925, 80.0),
             (0.9, 50.0), (0.9, 80.0)},
            {(target.requirements["I4.r1"], target.deadline_for("I4.r1"))
             for target in contract.targets})

    def test_stretching_the_deadline_costs_the_same_as_relaxing_the_ratio(self):
        contract = deterministic_targets([self.intent()])
        by_pair = {(target.requirements["I4.r1"], target.deadline_for("I4.r1")): target
                   for target in contract.targets}
        self.assertEqual(0.0, by_pair[(0.95, 50.0)].cost)
        # one step on either axis is one step: w * 1^2, and both together pay
        # for both rather than one riding free on the other
        self.assertEqual(by_pair[(0.925, 50.0)].cost, by_pair[(0.95, 80.0)].cost)
        self.assertEqual(by_pair[(0.925, 50.0)].cost + by_pair[(0.95, 80.0)].cost,
                         by_pair[(0.925, 80.0)].cost)

    def test_the_deadline_concession_is_reported_under_its_own_requirement(self):
        contract = deterministic_targets([self.intent()])
        authorization = contract.authorization
        stretched = next(target for target in contract.targets
                         if target.deadline_for("I4.r1") == 80.0
                         and target.requirements["I4.r1"] == 0.95)
        quality = concession_of(stretched, authorization)
        self.assertEqual(0.0, quality["perRequirement"]["I4.r1"])
        self.assertEqual(1.0, quality["perRequirement"]["I4.r1#deadline"])
        # D_o averages both levels of the owner's one intent
        self.assertEqual(0.5, quality["perOwner"]["ue1-command"])

    def test_a_deadline_past_the_signed_bound_is_refused_not_clipped(self):
        contract = deterministic_targets([self.intent()])
        unauthorized = Target(target_id="TX", requirements={"I4.r1": 0.95},
                              deadlines={"I4.r1": 500.0})
        _kept, dropped = validate_target_contract(
            replace_alternatives(contract, (unauthorized,)))
        self.assertEqual(1, len(dropped))
        self.assertIn("outside the separately authorized", dropped[0])

    def test_a_deadline_on_a_requirement_that_has_none_is_refused(self):
        contract = deterministic_targets([intent_from_sentence(
            "I1: UE ueId=131 needs at least 3.0 Mbps downlink, relaxable in 2 "
            "steps to 2.0")])
        invented = Target(target_id="TX", requirements={"I1.r1": 3.0},
                          deadlines={"I1.r1": 40.0})
        _kept, dropped = validate_target_contract(
            replace_alternatives(contract, (invented,)))
        self.assertIn("the owner authorized none", dropped[0])

    def test_a_compact_answer_may_subdivide_the_extension_but_not_exceed_it(self):
        authorization = deterministic_targets([self.intent()]).authorization
        compact = {"t0": {"requirements": {"I4.r1": 0.95}},
                   "levels": {"I4.r1": {"steps": 2, "bound": 0.9,
                                        "deadlineSteps": 3, "deadlineBound": 70.0}},
                   "constraints": [], "ranking": {}}
        contract, notes = validate_target_contract(compact, authorization)
        self.assertEqual(["the answer selected no additional target; T is the 2 mandatory targets",
                          "T holds 2 mandatory and 0 model-selected targets of 12 authorized"], notes)
        expanded = expand_targets(contract.authorization)
        self.assertEqual(3 * 4, len(expanded.targets))
        self.assertEqual(70.0, max(target.deadline_for("I4.r1")
                                   for target in expanded.targets))

        past = dict(compact)
        past["levels"] = {"I4.r1": {"steps": 2, "bound": 0.9,
                                    "deadlineSteps": 1, "deadlineBound": 500.0}}
        contract, notes = validate_target_contract(past, authorization)
        self.assertTrue(any("deadline bound 500.0 is outside" in note
                            for note in notes))
        # T is T0 alone, whose deadline is the original; what the clipping has
        # to hold is the signed ceiling on the expansion the selection draws from.
        self.assertEqual(80.0, max(target.deadline_for("I4.r1") for target
                                   in expand_targets(contract.authorization).targets))

    def test_the_ratio_is_judged_on_its_own_kpi_key(self):
        contract = deterministic_targets([self.intent()])
        self.assertEqual(
            {"I4.r1": PASS},
            judge_target({"deadlineSuccessRatio@131": 0.96}, contract.t0,
                         contract.authorization))
        self.assertEqual(
            {"I4.r1": FAIL},
            judge_target({"deadlineSuccessRatio@131": 0.5}, contract.t0,
                         contract.authorization))
        self.assertEqual(
            {"I4.r1": UNKNOWN},
            judge_target({}, contract.t0, contract.authorization))


def replace_alternatives(contract, alternatives):
    """The contract with these alternatives instead of the expanded ones."""
    from dataclasses import replace as _replace
    return _replace(contract, alternatives=tuple(alternatives))


# --------------------------------------------------------------------------- #
# the domain of the specification drop's section 3
# --------------------------------------------------------------------------- #

from dataclasses import replace as _dc_replace

#: Not re-exported by ``assurance.coordination`` yet, so taken from the module
#: that owns them.
from assurance.coordination.tc import (LevelQuota, OwnerModeSet,
                                       mode_constraint_from_record)

#: The offered load per UE the goodput levels are written against.
L_MBPS = 10.0

OWNERS = ("ue1", "ue2", "ue3")

#: Section 3's table, exactly: the ``(g, d)`` modes each owner signed for.
OWNER_MODES = {
    "ue1": ((0, 0), (1, 0), (2, 0)),                      # response protected
    "ue2": ((0, 0), (1, 0), (2, 0), (0, 1)),              # one or the other
    "ue3": ((0, 0), (1, 0), (2, 0), (0, 1), (1, 1)),      # joint, but not (2,1)
}


def three_owner_intents(extendable=(False, True, True)):
    """Three owners, each with a goodput and a response requirement.

    ``g = 0,1,2`` is "at least 0.9L / 0.8L / 0.7L" -- two steps from the
    original to the bound -- and ``d = 0,1`` is the original and the extended
    deadline, with the timely-completion ratio held at 0.90 over every issued
    request.  UE1's response is **protected**: its owner signed no extension,
    so that axis carries a single level.
    """
    rows = []
    for index, (owner, extend) in enumerate(zip(OWNERS, extendable), start=1):
        ue = str(130 + index)
        rows.append(Intent(
            intent_id=f"I{index}", owner=owner, priority=index, ue_id=ue,
            requirement=Requirement(
                req_id=f"I{index}.g", kpi="dlGoodputMbps", op=">=",
                value=0.9 * L_MBPS, unit="Mbps", scope=f"ue@{ue}",
                bound=0.7 * L_MBPS, steps=2, relaxable=True)))
        rows.append(Intent(
            intent_id=f"I{index}d", owner=owner, priority=index, ue_id=ue,
            requirement=Requirement(
                req_id=f"I{index}.d", kpi=KPI_DEADLINE_RATIO, op=">=",
                value=0.90, unit="fraction", scope=f"ue@{ue}",
                steps=0, relaxable=False, deadline_ms=1000.0,
                deadline_steps=1 if extend else 0,
                deadline_bound=1500.0 if extend else None)))
    return rows


def owner_mode_sets():
    return tuple(OwnerModeSet(owner=owner,
                              axes=(f"I{index}.g", f"I{index}.d#deadline"),
                              allow=OWNER_MODES[owner])
                 for index, owner in enumerate(OWNERS, start=1))


def extension_quota():
    return LevelQuota(axes=tuple(f"I{index}.d#deadline" for index in (1, 2, 3)),
                      at_most=1, level=1,
                      note="at most one owner may use the extended deadline")


def three_owner_authorization(extendable=(False, True, True), shaped=True):
    constraints = owner_mode_sets() + (extension_quota(),) if shaped else ()
    return Authorization.from_intents(three_owner_intents(extendable),
                                      mode_constraints=constraints)


def owner_mode(target, index):
    """The ``(g, d)`` this target puts owner ``index`` at."""
    return (target.levels.get(f"I{index}.g", 0),
            target.deadline_levels.get(f"I{index}.d", 0))


class TheDomainSection3Defines(unittest.TestCase):
    """``|Omega| = 3*4*5 - 6 = 54``, derived one restriction at a time.

    Each intermediate is asserted on its own, so a change that breaks one shows
    up there instead of only as a wrong total.
    """

    def size(self, **kwargs):
        return len(expand_targets(three_owner_authorization(**kwargs)).targets)

    def test_with_every_response_extendable_and_nothing_shaped(self):
        # per owner: 3 goodput levels x 2 deadline levels; cubed
        self.assertEqual(216, self.size(extendable=(True, True, True), shaped=False))

    def test_protecting_ue1s_response_removes_half(self):
        # UE1 signed no extension, so its response axis has one level: 3 x 6 x 6
        self.assertEqual(108, self.size(shaped=False))

    def test_the_per_owner_mode_sets_carve_it_to_sixty(self):
        # UE2 keeps 4 of its 6 (g,d) modes and UE3 keeps 5 of 6: 3 x 4 x 5
        authorization = Authorization.from_intents(
            three_owner_intents(), mode_constraints=owner_mode_sets())
        self.assertEqual(60, len(expand_targets(authorization).targets))

    def test_the_cross_owner_quota_lands_on_the_signed_domain(self):
        # minus the 6 where UE2 and UE3 both extend: 1 x 2 x 3
        self.assertEqual(3 * 4 * 5 - 6, self.size())
        self.assertEqual(54, self.size())


class WhatTheShapedDomainContains(unittest.TestCase):
    def setUp(self):
        self.authorization = three_owner_authorization()
        self.omega = expand_targets(self.authorization)

    def test_each_owner_sits_only_at_the_modes_it_signed(self):
        for index, owner in enumerate(OWNERS, start=1):
            seen = sorted({owner_mode(target, index)
                           for target in self.omega.targets})
            self.assertEqual(sorted(OWNER_MODES[owner]), seen, owner)

    def test_ue2_never_relaxes_both_at_once(self):
        for target in self.omega.targets:
            goodput, deadline = owner_mode(target, 2)
            self.assertFalse(goodput and deadline,
                             "UE2 signed for one concession or the other")

    def test_ue3_may_relax_both_but_not_at_its_lowest_goodput(self):
        modes = {owner_mode(target, 3) for target in self.omega.targets}
        self.assertIn((1, 1), modes)
        self.assertNotIn((2, 1), modes)

    def test_at_most_one_owner_uses_the_extended_deadline(self):
        for target in self.omega.targets:
            extended = [index for index in (1, 2, 3)
                        if owner_mode(target, index)[1] >= 1]
            self.assertLessEqual(len(extended), 1, target.target_id)

    def test_t0_is_still_every_original(self):
        self.assertEqual("T0", self.omega.t0.target_id)
        self.assertEqual({0}, set(self.omega.t0.levels.values()))
        self.assertEqual({0}, set(self.omega.t0.deadline_levels.values()))

    def test_a_per_kpi_threshold_predicate_could_not_have_said_this(self):
        """Why this needed a new kind at all.

        A joint condition is a predicate on one observed KPI *value*, and the
        expansion consults it against the threshold axis only.  Written the
        natural way against the response KPI it is recorded, binds, and then
        leaves the domain exactly as it found it.
        """
        recorded = Authorization.from_intents(
            three_owner_intents(),
            joint_conditions=[{"kpi": KPI_DEADLINE_RATIO, "scope": "ue@132",
                               "op": ">=", "value": 0.90, "unit": "fraction"}])
        self.assertEqual(1, len(recorded.joint_conditions))
        self.assertEqual("deadlineSuccessRatio@132", recorded.joint_conditions[0].kpi)
        self.assertEqual(108, len(expand_targets(recorded).targets))


class TheShapingChangesMembershipOnly(unittest.TestCase):
    """The ranking law is untouched: joint shaping decides who is in ``T``,
    never where in ``T`` anybody sits."""

    def setUp(self):
        self.shaped = three_owner_authorization()
        self.free = three_owner_authorization(shaped=False)

    def identity(self, target):
        return (tuple(sorted(target.levels.items())),
                tuple(sorted(target.deadline_levels.items())))

    def test_the_relative_order_of_any_two_survivors_is_unchanged(self):
        after = [self.identity(target)
                 for target in expand_targets(self.shaped).targets]
        survivors = set(after)
        before = [self.identity(target)
                  for target in expand_targets(self.free).targets
                  if self.identity(target) in survivors]
        self.assertEqual(before, after)

    def test_the_ranking_functions_do_not_read_the_constraints(self):
        for target in expand_targets(self.shaped).targets:
            self.assertEqual(cost_of(target, self.free),
                             cost_of(target, self.shaped))
            self.assertEqual(preference_key(target, self.free),
                             preference_key(target, self.shaped))
            self.assertEqual(concession_of(target, self.free),
                             concession_of(target, self.shaped))


class TheProtectedDimensionStaysOutOfTheDenominator(unittest.TestCase):
    """Section 3: a protected dimension remains a requirement but is excluded
    from the concession averaging denominator.

    Already true of a protected *response* dimension, and pinned here so it
    stays true: UE1's response is protected, so ``D1`` is ``g1/2`` and not
    ``(g1/2 + 0)/2``, which would have halved every concession beside it.
    """

    def setUp(self):
        self.authorization = three_owner_authorization()
        self.omega = expand_targets(self.authorization)

    def test_d1_is_exactly_half_ue1s_goodput_level(self):
        for target in self.omega.targets:
            quality = concession_of(target, self.authorization)
            self.assertAlmostEqual(owner_mode(target, 1)[0] / 2,
                                   quality["perOwner"]["ue1"], places=9)

    def test_the_relaxable_owners_average_both_of_their_dimensions(self):
        for target in self.omega.targets:
            quality = concession_of(target, self.authorization)
            for index in (2, 3):
                goodput, deadline = owner_mode(target, index)
                self.assertAlmostEqual((goodput / 2 + deadline) / 2,
                                       quality["perOwner"][OWNERS[index - 1]],
                                       places=9)

    def test_the_protected_dimension_is_still_reported(self):
        quality = concession_of(self.omega.t0, self.authorization)
        self.assertIn("I1.d#deadline", quality["perRequirement"])


class TheShapingRecordsRoundTrip(unittest.TestCase):
    def test_each_kind_reads_back_as_what_it_was(self):
        for item in owner_mode_sets() + (extension_quota(),):
            self.assertEqual(item, mode_constraint_from_record(item.to_record()))

    def test_the_authorization_carries_them_through_its_record(self):
        authorization = three_owner_authorization()
        record = authorization.to_full_record()
        self.assertEqual(4, len(record["modeConstraints"]))
        again = Authorization.from_record(record)
        self.assertEqual(authorization.mode_constraints, again.mode_constraints)
        self.assertEqual(54, len(expand_targets(again).targets))

    def test_a_sentence_is_not_a_constraint(self):
        self.assertIsNone(mode_constraint_from_record("ue2 may not relax both"))
        self.assertIsNone(mode_constraint_from_record({"axes": []}))

    def test_an_axis_no_requirement_carries_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            Authorization.from_intents(
                three_owner_intents(),
                mode_constraints=[OwnerModeSet(owner="ue2",
                                               axes=("I2.g", "I2.typo"),
                                               allow=((0, 0),))])
        self.assertIn("I2.typo", str(caught.exception))

    def test_a_mode_of_the_wrong_width_is_refused(self):
        with self.assertRaises(ValueError):
            OwnerModeSet(owner="ue2", axes=("I2.g", "I2.d#deadline"),
                         allow=((0, 0), (1,)))

    def test_the_compact_contract_tells_the_model_about_them(self):
        constraints = expand_targets(
            three_owner_authorization()).compact_record()["constraints"]
        self.assertTrue(any("extended deadline" in line for line in constraints),
                        constraints)


class TheValidatorRefusesAnExcludedCombination(unittest.TestCase):
    """An answer proposing a combination outside the signed modes is refused,
    not silently kept."""

    def setUp(self):
        self.shaped = three_owner_authorization()
        self.free = expand_targets(three_owner_authorization(shaped=False))

    def carrying(self, target):
        contract = _dc_replace(self.free, authorization=self.shaped)
        return replace_alternatives(contract,
                                    [_dc_replace(target, target_id="T1")])

    def test_a_mode_the_owner_did_not_sign_is_dropped_with_a_note(self):
        # UE2 relaxing goodput and deadline at once; nobody else extends, so
        # the mode set is the only thing this breaks
        excluded = next(target for target in self.free.targets
                        if owner_mode(target, 2) == (1, 1)
                        and owner_mode(target, 3)[1] == 0)
        kept, dropped = validate_target_contract(self.carrying(excluded), self.shaped)
        self.assertEqual((), kept.alternatives)
        self.assertTrue(any("authorized modes" in note for note in dropped), dropped)

    def test_two_owners_at_the_extended_deadline_is_dropped(self):
        # both sit at a mode each owner signed; only the cross-owner rule fails
        both = next(target for target in self.free.targets
                    if owner_mode(target, 2) == (0, 1)
                    and owner_mode(target, 3) == (0, 1))
        kept, dropped = validate_target_contract(self.carrying(both), self.shaped)
        self.assertEqual((), kept.alternatives)
        self.assertTrue(any("extended deadline" in note for note in dropped), dropped)

    def test_a_signed_combination_survives(self):
        allowed = next(target for target in self.free.targets
                       if owner_mode(target, 3) == (1, 1)
                       and owner_mode(target, 2) == (0, 0))
        kept, dropped = validate_target_contract(self.carrying(allowed), self.shaped)
        self.assertEqual(1, len(kept.alternatives))
        self.assertEqual([], dropped)


class AJointConditionComposesItsScope(unittest.TestCase):
    """``scope`` was read and dropped, so a condition written the natural way
    matched no observation key and bound nothing at all."""

    def test_a_scope_composes_into_the_observed_key(self):
        condition = JointCondition.from_record(
            {"kpi": "dlGoodputMbps", "scope": "ue@132", "op": ">=", "value": 1.25})
        self.assertEqual("dlGoodputMbps@132", condition.kpi)

    def test_a_record_that_already_carries_the_composed_key_is_untouched(self):
        condition = JointCondition.from_record(
            {"kpi": "dlGoodputMbps@132", "scope": "ue@999", "op": ">=", "value": 1.25})
        self.assertEqual("dlGoodputMbps@132", condition.kpi)

    def test_the_composed_condition_carves_the_same_domain(self):
        """The same refusal as the composed-key form, written the natural way."""
        authorization = Authorization.from_intents(
            two_intents(),
            joint_conditions=[{"kpi": "dlGoodputMbps", "scope": "ue@132",
                               "op": ">=", "value": 1.25, "unit": "Mbps"}])
        contract = expand_targets(authorization)
        self.assertEqual(3, len(contract.targets))
        for target in contract.targets:
            self.assertEqual(1.5, target.requirements["I2.r1"])


class ACarvingThatCannotBeReadIsRefused(unittest.TestCase):
    """Both carvings only ever *remove* combinations, so one that silently fails
    to read authorizes a wider domain than was signed -- and since target ids are
    positional, every id past the missing carving then names a different vector.
    A larger domain looks entirely plausible in the output, so the refusal has to
    happen where the authorization is built."""

    def test_an_unreadable_mode_constraint_is_refused_and_named(self):
        with self.assertRaises(ValueError) as caught:
            Authorization.from_intents(
                two_intents(), mode_constraints=[{"axes": ["I1.r1"], "allwo": [[0]]}])
        self.assertIn("wider domain than was signed", str(caught.exception))
        self.assertIn("allwo", str(caught.exception), "it must name the entry")

    def test_a_mangled_joint_condition_is_refused(self):
        """A dict that was *meant* to bind and cannot is the silent-widening case."""
        with self.assertRaises(ValueError) as caught:
            Authorization.from_intents(
                two_intents(),
                joint_conditions=[{"kpi": "dlGoodputMbps", "op": ">=", "unit": "Mbps"}])
        self.assertIn("wider domain than was signed", str(caught.exception))

    def test_an_operator_sentence_is_not_a_mangled_condition(self):
        """Prose was never going to bind, so its presence is not evidence of
        damage. This container cannot carry it -- ``to_full_record`` calls
        ``to_record`` on every entry -- so it is dropped here, not refused."""
        authorization = Authorization.from_intents(
            two_intents(), joint_conditions=["keep ue1 comfortable, please"])
        self.assertEqual((), authorization.joint_conditions)
        self.assertEqual([], authorization.to_full_record()["jointConditions"])

    def test_prose_round_trips_where_it_is_actually_kept(self):
        """``TargetContract`` is where a sentence survives: its record passes a
        non-JointCondition entry through verbatim, and a read back returns it."""
        contract = expand_targets(Authorization.from_intents(two_intents()))
        with_prose = _dc_replace(contract,
                                 joint_conditions=("keep ue1 comfortable, please",))
        record = with_prose.to_record()
        self.assertEqual(["keep ue1 comfortable, please"], record["jointConditions"])

    def test_a_readable_carving_still_carves_exactly_as_before(self):
        """The refusal must not change what a well-formed authorization expands to."""
        authorization = Authorization.from_intents(
            two_intents(),
            joint_conditions=[{"kpi": "dlGoodputMbps", "scope": "ue@132",
                               "op": ">=", "value": 1.25, "unit": "Mbps"}])
        self.assertEqual(3, len(expand_targets(authorization).targets))


class TheThreeAxesOfATrialAreSeparate(unittest.TestCase):
    """핸드오프 2026-09-18 §4.2: 실행 상태·관측 유효성·요구 달성을 따로 남긴다.

    BM 판 20260917T222430 시행 1 은 커널이 ``INDETERMINATE`` 로 닫았는데 ``success`` 에
    부분 PASS 8 개가 남아 첫 성공·유지·observed_best 로 흘러들 수 있었다.
    """

    def trial(self, outcome, stop=None, valid=True):
        from assurance.coordination.tc import Trial, Observation
        return Trial(trial_index=1, control_id="M1",
                     window={"valid": valid, "reason": "" if valid else "coverage"},
                     kpis={}, success={"T3": True, "T0": False},
                     kernel={"outcome": outcome, "stopReason": stop,
                             "terminalState": "SETTLED_NON_SUCCESS",
                             "execution": {"appliedAt": "x", "partialApply": stop == "PARTIAL_APPLY"}},
                     rolled_back=True)

    def test_an_indeterminate_window_attains_nothing_and_is_not_an_observation(self):
        from assurance.coordination.tc import Observation
        t = self.trial("INDETERMINATE")
        self.assertFalse(t.observation_valid)
        self.assertEqual("UNKNOWN", t.attainment["status"])
        self.assertFalse(Observation.from_trial(t).valid)
        record = t.to_record()
        self.assertEqual("kernel outcome INDETERMINATE", record["observationValidity"]["reason"])
        self.assertEqual("SETTLED_NON_SUCCESS", record["executionStatus"]["terminalState"])

    def test_a_partial_apply_is_not_an_observation_of_the_proposal(self):
        self.assertFalse(self.trial("FAIL", stop="PARTIAL_APPLY").observation_valid)

    def test_a_rollback_alone_does_not_void_a_valid_window(self):
        t = self.trial("FAIL", stop="SEMANTIC_NON_SUCCESS")
        self.assertTrue(t.observation_valid)
        self.assertEqual("ATTAINED", t.attainment["status"])
        self.assertTrue(t.execution_status["rolledBack"])

    def test_judging_a_void_window_passes_no_target(self):
        from assurance.coordination.tc import Trial
        t = self.trial("INDETERMINATE")
        t.success = {}
        contract = type("C", (), {"targets": ()})()
        t.judge_against(contract, ())
        self.assertEqual({}, t.success)


class AMissingRequiredKpiVoidsTheWindow(unittest.TestCase):
    """§4.2·§6.2 (3A 20260918T040406 시행 4): ue1 KPI 가 빠진 창은 UNKNOWN, 재사용 관측 아님."""

    def test_missing_kpi_is_unknown_and_certain_failures_stay(self):
        from assurance.coordination.tc import Trial, Observation
        t = Trial(trial_index=4, control_id="C3", window={"valid": True},
                  verdicts={"T0": {"I1g.r1": "UNKNOWN", "I2d.r1": "PASS", "I3g.r1": "FAIL"}},
                  success={"T0": False},
                  kernel={"outcome": "SUCCESS", "terminalState": "SETTLED_SUCCESS"})
        self.assertEqual({"valid": False, "reason": "required KPI missing: I1g.r1"},
                         t.observation_validity)
        self.assertEqual("UNKNOWN", t.attainment["status"])
        self.assertEqual(["I1g.r1"], t.attainment["missingRequirements"])
        self.assertEqual("FAIL", t.verdicts["T0"]["I3g.r1"])     # 확실한 미달은 남는다
        self.assertFalse(Observation.from_trial(t).valid)
