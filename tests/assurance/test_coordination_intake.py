"""Intake, observation rules and generation options -- the deterministic frame.

Three things the executor does around the model calls, all asserted directly:

* the **checklist** finds what the operator did not say *before* any call, and
  a sentence with no relaxation phrase is missing information, not a refusal to
  relax -- the whole point of contract v2 section 2.2;
* an **observation** is a statistic over a window with a minimum coverage, and
  a thin window is ``UNKNOWN`` rather than a quietly averaged number; every
  observation carries the moment it stops being usable;
* a **generation budget** is what the model is asked to spend, chosen from the
  latency calibration when one exists so that an answer arrives while its
  observations are still valid -- and never as a cutoff.

Hermetic: no clock beyond the numbers in the fixtures, no network, no model.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from assurance.coordination import (
    DEFAULT_OBSERVATION_RULES, KPI_DEADLINE_RATIO, SITTING, STATISTIC_RATIO,
    UNKNOWN, GenerationOptions,
    IntakeChecklist, KpiObservationRule, ObservationRules, Requirement,
    choose_from_calibration, intent_from_sentence, load_latency_calibration,
    merge_answers,
)
from assurance.coordination.tc import Authorization, Intent, expand_targets

SETTINGS = {"trialsK": 8, "deadlineMs": None, "horizonMs": None,
            "stopAfterRelaxedSuccess": False, "retention": "bestAttained",
            "observation": {"dlGoodputMbps": DEFAULT_OBSERVATION_RULES[
                "dlGoodputMbps"].to_record()}}


def complete_intent():
    return intent_from_sentence(
        "I1: UE ueId=131 needs at least 3.0 Mbps downlink, relaxable in 2 steps "
        "to 2.0, owner ue1-video priority 1")


class TheChecklist(unittest.TestCase):
    """What the operator has not said, asked in the agent's own shape."""

    def test_a_complete_intent_and_a_complete_sitting_ask_nothing(self):
        self.assertEqual(IntakeChecklist().check([complete_intent()], None, SETTINGS),
                         [])

    def test_a_sentence_with_no_relaxation_phrase_asks_for_steps_and_bound(self):
        intent = intent_from_sentence(
            "I2: UE ueId=132 needs at least 1.5 Mbps downlink, owner ue2-map")
        missing = IntakeChecklist().check([intent], None, SETTINGS)
        self.assertEqual([item["field"] for item in missing], ["steps"])
        self.assertIn("steps", missing[0]["question"])
        self.assertEqual(missing[0]["intentId"], "I2")

    def test_steps_without_a_bound_asks_for_the_bound(self):
        requirement = Requirement(req_id="I2.r1", kpi="dlGoodputMbps",
                                  scope="ue@132", op=">=", value=1.5, unit="Mbps",
                                  steps=2)
        intent = Intent(intent_id="I2", owner="ue2-map", ue_id="132",
                        requirement=requirement)
        missing = IntakeChecklist().check([intent], None, SETTINGS)
        self.assertEqual([item["field"] for item in missing], ["bound"])
        self.assertIn("bound", missing[0]["question"])

    def test_a_missing_unit_is_asked_for(self):
        requirement = Requirement(req_id="I2.r1", kpi="dlGoodputMbps",
                                  scope="ue@132", op=">=", value=1.5, unit="",
                                  steps=2, bound=1.0)
        intent = Intent(intent_id="I2", owner="ue2-map", ue_id="132",
                        requirement=requirement)
        missing = IntakeChecklist().check([intent], None, SETTINGS)
        self.assertEqual([item["field"] for item in missing], ["unit"])

    def test_an_explicit_refusal_to_relax_asks_nothing(self):
        intent = intent_from_sentence(
            "I3: UE ueId=133 needs at least 1.0 Mbps downlink, non-relaxable, "
            "owner ue3-incumbent")
        self.assertEqual(IntakeChecklist().check([intent], None, SETTINGS), [])

    def test_every_sitting_setting_is_asked_for_when_absent(self):
        missing = IntakeChecklist().check([complete_intent()], None, {})
        fields = [item["field"] for item in missing]
        self.assertIn("trialsK", fields)
        self.assertIn("deadlineMs", fields)
        self.assertIn("retention", fields)
        self.assertIn("observation.dlGoodputMbps", fields)
        for item in missing:
            self.assertEqual(item["intentId"], SITTING)

    def test_an_explicit_null_deadline_is_an_answer_not_a_gap(self):
        settings = dict(SETTINGS)
        missing = IntakeChecklist().check([complete_intent()], None, settings)
        self.assertEqual(missing, [])


class MergingAnswers(unittest.TestCase):
    """The operator's answers fold back into the intents and the settings."""

    def test_a_steps_and_bound_answer_completes_the_requirement(self):
        intent = intent_from_sentence(
            "I2: UE ueId=132 needs at least 1.5 Mbps downlink, owner ue2-map")
        intents, authorization, settings = merge_answers(
            [intent], None, {"I2": {"steps": 2, "bound": 1.0}}, SETTINGS)
        self.assertEqual(intents[0].requirement.levels, (1.5, 1.25, 1.0))
        self.assertEqual(authorization.get("I2.r1").steps, 2)
        self.assertEqual(IntakeChecklist().check(intents, authorization, settings), [])

    def test_a_sitting_answer_completes_the_settings(self):
        _intents, _authorization, settings = merge_answers(
            [complete_intent()], None, {SITTING: {"trialsK": 6}}, {})
        self.assertEqual(settings["trialsK"], 6)

    def test_an_answer_for_an_unknown_intent_is_ignored_not_refused(self):
        intents, _authorization, _settings = merge_answers(
            [complete_intent()], None, {"I9": {"steps": 4}}, SETTINGS)
        self.assertEqual(intents[0].requirement.steps, 2)

    def test_an_owner_answer_reaches_the_authorization(self):
        intent = intent_from_sentence(
            "I2: UE ueId=132 needs at least 1.5 Mbps downlink, relaxable in 1 "
            "steps to 1.0")
        _intents, authorization, _settings = merge_answers(
            [intent], None, {"I2": {"owner": "ue2-map"}}, SETTINGS)
        self.assertEqual(authorization.get("I2.r1").owner, "ue2-map")


class TheObservationRules(unittest.TestCase):
    """Settle, window, statistic, coverage and validity, per KPI kind."""

    def setUp(self):
        self.rules = ObservationRules.defaults(("dlGoodputMbps", "servingCell"))

    def samples(self, count, value=3.0, step=1000, start=0):
        return [{"t": start + index * step, "kpis": {"dlGoodputMbps@131": value}}
                for index in range(count)]

    def test_the_case_hold_is_the_longest_settle_plus_window(self):
        self.assertEqual(self.rules.hold_ms(), 35000)

    def test_a_full_window_is_the_mean_of_what_is_inside_it(self):
        samples = self.samples(31, value=3.0)
        result = self.rules.aggregate(samples, hold_end=30000, interval_ms=1000)
        self.assertAlmostEqual(result["dlGoodputMbps@131"], 3.0)

    def test_only_the_samples_inside_the_window_count(self):
        samples = ([{"t": index * 1000, "kpis": {"dlGoodputMbps@131": 9.0}}
                    for index in range(5)]
                   + [{"t": 5000 + index * 1000, "kpis": {"dlGoodputMbps@131": 3.0}}
                      for index in range(31)])
        result = self.rules.aggregate(samples, hold_end=35000, interval_ms=1000)
        self.assertAlmostEqual(result["dlGoodputMbps@131"], 3.0)

    def test_a_thin_window_is_unknown_rather_than_an_average(self):
        samples = self.samples(5, value=3.0, start=25000)
        result = self.rules.aggregate(samples, hold_end=30000, interval_ms=1000)
        self.assertEqual(result["dlGoodputMbps@131"], UNKNOWN)

    def test_the_serving_cell_takes_the_last_sample_not_a_mean(self):
        samples = [{"t": 1000, "kpis": {"servingCell@131": "12345678"}},
                   {"t": 2000, "kpis": {"servingCell@131": "87654321"}},
                   {"t": 3000, "kpis": {"servingCell@131": "87654321"}}]
        result = self.rules.aggregate(samples, hold_end=3000, interval_ms=1000)
        self.assertEqual(result["servingCell@131"], "87654321")

    def test_every_kpi_is_judged_at_the_same_hold_end(self):
        samples = [{"t": index * 1000,
                    "kpis": {"dlGoodputMbps@131": 3.0,
                             "servingCell@131": "12345678"}}
                   for index in range(36)]
        coverage = self.rules.coverage(samples, hold_end=35000, interval_ms=1000)
        self.assertGreaterEqual(coverage["dlGoodputMbps@131"], 0.8)
        self.assertGreaterEqual(coverage["servingCell@131"], 1.0)

    def test_valid_until_is_the_window_end_plus_the_validity(self):
        self.assertEqual(self.rules.valid_until("dlGoodputMbps@131", 30000), 90000.0)
        self.assertEqual(
            self.rules.valid_until("servingCell@131", "2026-09-07T10:00:30.000000Z"),
            "2026-09-07T10:00:40.000000Z")

    def test_an_answer_after_the_earliest_validity_is_stale(self):
        observations = [{"validUntil": "2026-09-07T10:01:30.000000Z"},
                        {"validUntil": "2026-09-07T10:00:40.000000Z"}]
        self.assertTrue(self.rules.is_stale(observations,
                                            "2026-09-07T10:00:41.000000Z"))
        self.assertFalse(self.rules.is_stale(observations,
                                             "2026-09-07T10:00:39.000000Z"))

    def test_the_shortest_validity_is_what_a_budget_has_to_fit_inside(self):
        self.assertEqual(self.rules.min_validity_ms(), 10000)

    def test_the_rules_round_trip_as_the_episode_records_them(self):
        record = self.rules.to_record()
        self.assertEqual(record["dlGoodputMbps"]["validityMs"], 60000)
        again = ObservationRules.from_record(record)
        self.assertEqual(again.hold_ms(), self.rules.hold_ms())

    def test_a_kpi_kind_with_no_rule_falls_back_to_a_stated_default(self):
        rules = ObservationRules({})
        self.assertEqual(rules.rule_for("dlGoodputMbps@131").window_ms, 30000)
        self.assertIsInstance(rules.rule_for("somethingElse@131"),
                              KpiObservationRule)


class TheGenerationOptions(unittest.TestCase):
    """What the model is asked to spend -- never a cutoff."""

    def test_a_formation_call_is_given_more_room_than_a_selection_call(self):
        formation = GenerationOptions.for_role("target")
        selection = GenerationOptions.for_role("trajectory")
        self.assertGreater(formation.thinking_budget_tokens,
                           selection.thinking_budget_tokens)
        self.assertTrue(formation.json_mode)

    def test_an_unknown_role_falls_back_to_the_stated_default(self):
        options = GenerationOptions.for_role("something-else")
        self.assertEqual(options.max_tokens, 1500)
        self.assertEqual(options.thinking_budget_tokens, 4000)

    def calibration(self):
        return {"schemaVersion": "agent-latency-calibration/1.0.0",
                "models": {"claude-sonnet": {"trajectory": [
                    {"thinkingBudgetTokens": 2000, "p50LatencyMs": 2000,
                     "p95LatencyMs": 4000},
                    {"thinkingBudgetTokens": 8000, "p50LatencyMs": 20000,
                     "p95LatencyMs": 40000}]}}}

    def test_the_largest_budget_that_fits_the_validity_is_chosen(self):
        options = choose_from_calibration(self.calibration(), "trajectory", 60000,
                                          "claude-sonnet")
        self.assertEqual(options.thinking_budget_tokens, 8000)
        self.assertEqual(options.source, "calibration")

    def test_a_short_validity_picks_the_smaller_budget(self):
        options = choose_from_calibration(self.calibration(), "trajectory", 10000,
                                          "claude-sonnet")
        self.assertEqual(options.thinking_budget_tokens, 2000)

    def test_nothing_fitting_keeps_the_default_rather_than_truncating(self):
        options = choose_from_calibration(self.calibration(), "trajectory", 6000,
                                          "claude-sonnet")
        self.assertEqual(options.source, "default")
        self.assertEqual(options.thinking_budget_tokens,
                         GenerationOptions.for_role("trajectory").thinking_budget_tokens)

    def test_no_calibration_at_all_keeps_the_default(self):
        self.assertEqual(choose_from_calibration({}, "target", 60000).source, "default")
        self.assertEqual(choose_from_calibration(None, "target", 60000).source,
                         "default")

    def v2_calibration(self):
        """The shape ``tools/campaign5/calibrate_agent_latency.py`` writes:
        the model at the top level, and the budgets keyed by their token count."""
        return {"mock:agent": {
            "trajectory": {"500": {"p50": 10.1, "p95": 12.0, "n": 3, "accepted": 1.0},
                           "8000": {"p50": 160.0, "p95": 200.0, "n": 3,
                                    "accepted": 1.0}}}}

    def test_the_calibration_tools_own_shape_is_read_too(self):
        options = choose_from_calibration(self.v2_calibration(), "trajectory", 60000,
                                          "mock:agent")
        self.assertEqual(8000, options.thinking_budget_tokens)
        self.assertEqual("calibration", options.source)

    def test_that_shape_still_picks_the_budget_that_fits(self):
        options = choose_from_calibration(self.v2_calibration(), "trajectory", 5100,
                                          "mock:agent")
        self.assertEqual(500, options.thinking_budget_tokens)

    def test_the_only_model_is_used_when_the_caller_names_none(self):
        options = choose_from_calibration(self.v2_calibration(), "trajectory", 60000)
        self.assertEqual(8000, options.thinking_budget_tokens)

    def test_a_role_keyed_document_is_not_mistaken_for_a_model_keyed_one(self):
        options = choose_from_calibration(
            {"trajectory": [{"thinkingBudgetTokens": 4321, "p95LatencyMs": 100}]},
            "trajectory", 60000)
        self.assertEqual(4321, options.thinking_budget_tokens)

    def test_a_missing_or_broken_calibration_file_is_an_empty_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent-latency-calibration.json"
            self.assertEqual(load_latency_calibration(path), {})
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_latency_calibration(path), {})
            path.write_text(json.dumps(self.calibration()), encoding="utf-8")
            self.assertIn("models", load_latency_calibration(path))

    def test_the_chosen_options_are_recorded_with_their_source(self):
        record = GenerationOptions.for_role("control").to_record()
        self.assertEqual(record["source"], "default")
        self.assertIn("thinkingBudgetTokens", record)
        self.assertNotIn("source", GenerationOptions.for_role("control").as_options())


class TheDeadlineSuccessRatioRule(unittest.TestCase):
    """A ratio is not a mean, and a window with no request is not a success.

    ``exp_metrics.md`` section 4, restated in ``exp_3UEscenario.md``: the
    denominator is every eligible issued request, the ones that never answered
    included.  The observer reports the two counters and the rule differences
    them over the window, so a window can be asked for at any deadline the
    target in force names.
    """

    def echo_intent(self):
        return intent_from_sentence(
            "I4: at least 95% of tagged echo requests within 50 ms for ueId=131 "
            "owner ue1-command, relaxable in 2 steps to 0.9")

    def samples(self, rows):
        return [{"t": stamp, "kpis": {"deadlineSuccessRatio@131": counters}}
                for stamp, counters in rows]

    def rules(self):
        return ObservationRules.defaults((KPI_DEADLINE_RATIO,))

    def test_the_stated_default_is_the_one_the_scenario_names(self):
        rule = DEFAULT_OBSERVATION_RULES[KPI_DEADLINE_RATIO]
        self.assertEqual((5000, 30000, STATISTIC_RATIO, 0.8, 60000),
                         (rule.settle_ms, rule.window_ms, rule.statistic,
                          rule.min_coverage, rule.validity_ms))

    def test_the_window_value_is_completed_over_eligible_not_a_mean(self):
        rows = [(0, {"eligible": 0, "completed": 0}),
                (10000, {"eligible": 100, "completed": 100}),
                (20000, {"eligible": 200, "completed": 150}),
                (30000, {"eligible": 300, "completed": 210})]
        aggregated = self.rules().aggregate(self.samples(rows), 30000)
        # 210 of 300 over the window; the mean of the three per-sample ratios
        # (1.00, 0.50, 0.60) would have been 0.70
        self.assertEqual(0.7, aggregated["deadlineSuccessRatio@131"])

    def test_a_request_that_never_answered_stays_in_the_denominator(self):
        rows = [(0, {"eligible": 0, "completed": 0}),
                (10000, {"eligible": 40, "completed": 40}),
                (20000, {"eligible": 80, "completed": 80}),
                (30000, {"eligible": 120, "completed": 80})]
        self.assertEqual(
            round(80 / 120, 6),
            self.rules().aggregate(self.samples(rows), 30000)["deadlineSuccessRatio@131"])

    def test_a_window_with_no_issued_request_is_unknown_not_one(self):
        rows = [(stamp, {"eligible": 5, "completed": 5})
                for stamp in (0, 10000, 20000, 30000)]
        self.assertEqual(
            UNKNOWN,
            self.rules().aggregate(self.samples(rows), 30000)["deadlineSuccessRatio@131"])

    def test_only_the_window_counts_and_the_baseline_is_read_from_before_it(self):
        rows = [(-30000, {"eligible": 500, "completed": 0}),
                (0, {"eligible": 1000, "completed": 0}),
                (15000, {"eligible": 1050, "completed": 25}),
                (30000, {"eligible": 1100, "completed": 50})]
        # the window is [0, 30000]: 100 eligible, 50 completed, and the 500
        # earlier misses are not charged to it
        self.assertEqual(
            0.5,
            self.rules().aggregate(self.samples(rows), 30000)["deadlineSuccessRatio@131"])

    def test_a_thin_window_is_unknown_rather_than_a_ratio(self):
        rows = [(0, {"eligible": 0, "completed": 0}),
                (1000, {"eligible": 10, "completed": 10})]
        rule = DEFAULT_OBSERVATION_RULES[KPI_DEADLINE_RATIO]
        value, coverage = rule.aggregate([(stamp, counters) for stamp, counters in rows],
                                         30000, interval_ms=1000)
        self.assertEqual(UNKNOWN, value)
        self.assertLess(coverage, 0.8)

    def test_a_sample_that_is_not_a_counter_pair_establishes_nothing(self):
        rows = [{"t": stamp, "kpis": {"deadlineSuccessRatio@131": 0.99}}
                for stamp in (0, 10000, 20000, 30000)]
        self.assertEqual(UNKNOWN,
                         self.rules().aggregate(rows, 30000)["deadlineSuccessRatio@131"])

    def test_a_counter_reset_inside_the_window_falls_back_to_its_own_totals(self):
        rows = [(0, {"eligible": 900, "completed": 800}),
                (15000, {"eligible": 20, "completed": 18}),
                (30000, {"eligible": 60, "completed": 30})]
        self.assertEqual(
            0.5,
            self.rules().aggregate(self.samples(rows), 30000)["deadlineSuccessRatio@131"])

    def test_a_missing_deadline_is_asked_for_like_any_other_field(self):
        intent = intent_from_sentence(
            "I4: at least 95% of tagged echo requests for ueId=131 owner "
            "ue1-command, relaxable in 2 steps to 0.9")
        missing = IntakeChecklist().check([intent], None, self.settings())
        self.assertEqual(["deadlineMs"], [item["field"] for item in missing])
        self.assertEqual("I4", missing[0]["intentId"])

    def test_a_deadline_that_is_there_asks_nothing(self):
        self.assertEqual(
            [], IntakeChecklist().check([self.echo_intent()], None, self.settings()))

    def test_deadline_steps_with_no_bound_asks_for_the_bound(self):
        intent = self.echo_intent()
        intent = intent.__class__(
            intent_id=intent.intent_id, owner=intent.owner, ue_id=intent.ue_id,
            requirement=Requirement.from_record(
                dict(intent.requirement.to_record(), deadlineSteps=2,
                     deadlineBound=None)),
            priority=intent.priority, sentence=intent.sentence)
        missing = IntakeChecklist().check([intent], None, self.settings())
        self.assertEqual(["deadlineBound"], [item["field"] for item in missing])

    def test_the_operators_answer_completes_the_deadline(self):
        intent = intent_from_sentence(
            "I4: at least 95% of tagged echo requests for ueId=131 owner "
            "ue1-command, relaxable in 2 steps to 0.9")
        rows, authorization, _settings = merge_answers(
            [intent], None, {"I4": {"deadlineMs": 50, "deadlineSteps": 1,
                                    "deadlineBound": 80}})
        self.assertEqual(50.0, rows[0].requirement.deadline_ms)
        self.assertEqual((50.0, 80.0), rows[0].requirement.deadline_levels)
        self.assertEqual((50.0, 80.0),
                         authorization.get("I4.r1").deadline_levels)
        self.assertEqual([], IntakeChecklist().check(rows, None, self.settings()))

    def settings(self):
        return dict(SETTINGS, observation=dict(
            SETTINGS["observation"],
            **{KPI_DEADLINE_RATIO: DEFAULT_OBSERVATION_RULES[
                KPI_DEADLINE_RATIO].to_record()}))


class ModeConstraintsReachTheAuthorization(unittest.TestCase):
    """The sitting's cross-axis and cross-owner shaping survives intake.

    ``merge_answers`` *rebuilds* the authorization, so whatever it does not
    carry across is silently gone by the time the domain is expanded -- and a
    constraint that is recorded, signed and has no effect is exactly the class
    of bug this path exists to prevent.  The sizes below are derived one
    restriction at a time, so a change that breaks one shows up there and not
    only as a wrong total.
    """

    #: Two owners, each with a relaxable goodput (``g = 0,1,2``) and an
    #: extendable response (``d = 0,1``): ``(3 * 2) ** 2`` unshaped.
    FREE = 6 * 6

    #: UE2 signed for one concession or the other, never both: 4 of its 6 modes.
    MODE_SET = {"owner": "ue2", "axes": ["I2.g", "I2.d#deadline"],
                "allow": [[0, 0], [1, 0], [2, 0], [0, 1]]}
    #: The cross-owner rule no single owner could state.
    QUOTA = {"axes": ["I1.d#deadline", "I2.d#deadline"], "atMost": 1, "level": 1,
             "note": "at most one owner may use the extended deadline"}

    def intents(self):
        rows = []
        for index in (1, 2):
            ue = str(130 + index)
            rows.append(Intent(
                intent_id=f"I{index}", owner=f"ue{index}", priority=index, ue_id=ue,
                requirement=Requirement(
                    req_id=f"I{index}.g", kpi="dlGoodputMbps", op=">=",
                    value=9.0, unit="Mbps", scope=f"ue@{ue}",
                    bound=7.0, steps=2, relaxable=True)))
            rows.append(Intent(
                intent_id=f"I{index}d", owner=f"ue{index}", priority=index, ue_id=ue,
                requirement=Requirement(
                    req_id=f"I{index}.d", kpi=KPI_DEADLINE_RATIO, op=">=",
                    value=0.90, unit="fraction", scope=f"ue@{ue}",
                    steps=0, relaxable=False, deadline_ms=1000.0,
                    deadline_steps=1, deadline_bound=1500.0)))
        return rows

    def merge(self, sitting=None, authorization=None):
        answers = {} if sitting is None else {SITTING: dict(sitting)}
        return merge_answers(self.intents(), authorization, answers, SETTINGS)

    def size(self, authorization):
        return len(expand_targets(authorization).targets)

    # -- what the sitting says now reaches the domain ------------------------ #

    def test_a_sitting_that_says_nothing_expands_to_the_free_product(self):
        _rows, authorization, _settings = self.merge()
        self.assertEqual((), authorization.mode_constraints)
        self.assertEqual(self.FREE, self.size(authorization))

    def test_the_mode_set_alone_carves_it_to_the_modes_ue2_signed(self):
        _rows, authorization, _settings = self.merge(
            {"modeConstraints": [self.MODE_SET]})
        self.assertEqual(1, len(authorization.mode_constraints))
        # UE1 keeps all 6 of its (g,d) modes; UE2 keeps 4 of its 6.
        self.assertEqual(6 * 4, self.size(authorization))

    def test_the_cross_owner_quota_lands_on_the_shaped_domain(self):
        _rows, authorization, _settings = self.merge(
            {"modeConstraints": [self.MODE_SET, self.QUOTA]})
        self.assertEqual(2, len(authorization.mode_constraints))
        # minus the ones where both owners extend: UE1's 3 extended modes
        # against the single extended mode UE2 kept.
        self.assertEqual(6 * 4 - 3 * 1, self.size(authorization))

    def test_ue2_never_relaxes_both_at_once_in_the_expanded_domain(self):
        _rows, authorization, _settings = self.merge(
            {"modeConstraints": [self.MODE_SET, self.QUOTA]})
        for target in expand_targets(authorization).targets:
            self.assertFalse(target.levels.get("I2.g", 0)
                             and target.deadline_levels.get("I2.d", 0),
                             "UE2 signed for one concession or the other")

    def test_constraints_already_signed_survive_an_answer_about_something_else(self):
        """The regression proper: the authorization arrives carrying them and
        the operator answers about a bound, so the sitting says nothing about
        shaping.  They must not be dropped on the way through."""
        signed = Authorization.from_intents(
            self.intents(), mode_constraints=[self.MODE_SET, self.QUOTA])
        rows, authorization, _settings = merge_answers(
            self.intents(), signed, {"I1": {"bound": 6.0}}, SETTINGS)
        self.assertEqual(6.0, rows[0].requirement.bound)
        self.assertEqual(signed.mode_constraints, authorization.mode_constraints)
        self.assertEqual(6 * 4 - 3 * 1, self.size(authorization))

    # -- an authorization nobody shaped is what it always was ---------------- #

    def test_a_sitting_without_them_is_bit_identical_to_an_unshaped_one(self):
        _rows, plain, _settings = self.merge({"trialsK": 8})
        reference = Authorization.from_intents(self.intents())
        self.assertEqual(reference.to_full_record(), plain.to_full_record())
        self.assertEqual([], plain.to_full_record()["modeConstraints"])

    # -- malformed shaping is refused, never quietly dropped ----------------- #

    def test_a_constraint_that_is_neither_kind_is_refused_and_named(self):
        with self.assertRaises(ValueError) as caught:
            self.merge({"modeConstraints": [self.MODE_SET, {"axes": ["I1.g"]}]})
        self.assertIn("I1.g", str(caught.exception))

    def test_a_sentence_is_refused_rather_than_read_as_no_constraint(self):
        with self.assertRaises(ValueError) as caught:
            self.merge({"modeConstraints": ["ue2 may not relax both"]})
        self.assertIn("ue2 may not relax both", str(caught.exception))

    def test_an_axis_no_requirement_carries_is_refused_through_intake(self):
        with self.assertRaises(ValueError) as caught:
            self.merge({"modeConstraints": [
                {"owner": "ue2", "axes": ["I2.g", "I2.typo"], "allow": [[0, 0]]}]})
        self.assertIn("I2.typo", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
