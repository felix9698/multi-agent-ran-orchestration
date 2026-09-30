"""The controlled equivalent budget, and the §12 metering that proves it.

Authority: ``task_0820_unified_ota_assurance_system.md`` section 11
("monolithic LLM은 proposed method와 같은 foundation model과 통제된 동등
token/tool-call budget을 사용한다") and section 12 ("agent token/tool-call/
latency" among the automatically calculated run figures); design section 12.

Two separate claims live here and they are easy to conflate.  *Equivalence* is
about the ceiling: both LLM strategies are stopped by the same numbers, by the
same code.  *Metering* is about the record: what each actually spent is
counted per role, and is readable without re-deriving it from a log.  A system
could have either without the other, and the paper needs both.
"""

from __future__ import annotations

import json
import unittest

from assurance.advisors.strategies import (
    DEFAULT_COMPARABLE_BUDGET,
    AgentBudget,
    AgentBudgetMeter,
    BudgetExceeded,
    MonolithicLLMStrategy,
    RoleSeparatedLLMCoordinator,
    build_comparable_llm_pair,
)
from assurance.advisors.strategies.llm_coordinator import (
    COORDINATOR_ROLE,
    INTENT_ROLE,
    XAPP_ROLE,
)
from assurance.advisors.strategies.monolithic import MONOLITHIC_ROLE

from tests.assurance.strategy_support import (
    choice_reply,
    role_separated_script,
    scripted,
    views,
)

BUDGET = AgentBudget(max_total_tokens=1000, max_tool_calls=3, max_latency_ms=500.0)


def _meter(budget: AgentBudget = BUDGET) -> AgentBudgetMeter:
    meter = AgentBudgetMeter(budget, meter_id="test-meter")
    meter.begin_proposal("case/budget")
    return meter


def _charge(meter: AgentBudgetMeter, *, agent_role: str = "r", tokens: int = 100, latency: float = 10.0):
    return meter.charge(
        agent_role=agent_role,
        model_identity="mock/scripted",
        prompt_hash="prompt-hash",
        input_tokens=tokens,
        output_tokens=0,
        latency_ms=latency,
    )


class TheBudgetIsAValueNotAConvention(unittest.TestCase):
    def test_a_budget_with_a_non_positive_ceiling_is_refused(self) -> None:
        for kwargs in (
            {"max_total_tokens": 0},
            {"max_tool_calls": 0},
            {"max_latency_ms": 0.0},
        ):
            with self.subTest(**kwargs):
                fields = {"max_total_tokens": 10, "max_tool_calls": 1, "max_latency_ms": 1.0}
                fields.update(kwargs)
                with self.assertRaises(ValueError):
                    AgentBudget(**fields)  # type: ignore[arg-type]

    def test_two_budgets_with_the_same_numbers_compare_equal(self) -> None:
        # The comparison condition is checked by equality, so the value object
        # has to have value semantics rather than identity semantics.
        self.assertEqual(
            AgentBudget(max_total_tokens=10, max_tool_calls=1, max_latency_ms=1.0),
            AgentBudget(max_total_tokens=10, max_tool_calls=1, max_latency_ms=1.0),
        )

    def test_the_default_budget_fits_the_method_that_spends_most(self) -> None:
        # The role-separated method makes three calls; a ceiling below that
        # would be a handicap dressed as a control.
        self.assertGreaterEqual(DEFAULT_COMPARABLE_BUDGET.max_tool_calls, 3)


class TheMeterEnforcesEveryDimension(unittest.TestCase):
    def test_a_call_beyond_the_tool_call_ceiling_is_refused_before_it_is_made(self) -> None:
        meter = _meter()
        for _ in range(BUDGET.max_tool_calls):
            meter.ensure_room()
            _charge(meter, tokens=1, latency=1.0)
        with self.assertRaises(BudgetExceeded) as caught:
            meter.ensure_room()
        self.assertEqual(caught.exception.dimension, "toolCalls")

    def test_the_token_ceiling_stops_the_window(self) -> None:
        meter = _meter()
        with self.assertRaises(BudgetExceeded) as caught:
            _charge(meter, tokens=BUDGET.max_total_tokens + 1)
        self.assertEqual(caught.exception.dimension, "tokens")

    def test_the_latency_ceiling_is_a_budget_dimension_too(self) -> None:
        meter = _meter()
        with self.assertRaises(BudgetExceeded) as caught:
            _charge(meter, tokens=1, latency=BUDGET.max_latency_ms + 1.0)
        self.assertEqual(caught.exception.dimension, "latencyMs")

    def test_the_overspending_call_is_still_recorded(self) -> None:
        # Tokens a model already emitted cannot be un-spent, and dropping the
        # record would under-report exactly the runs the paper needs to see.
        meter = _meter()
        with self.assertRaises(BudgetExceeded):
            _charge(meter, tokens=BUDGET.max_total_tokens + 1)
        self.assertEqual(len(meter.records), 1)
        self.assertEqual(meter.cumulative_totals()["tokens"], BUDGET.max_total_tokens + 1)

    def test_a_new_proposal_window_restarts_enforcement_but_not_the_totals(self) -> None:
        meter = _meter()
        _charge(meter, tokens=400, latency=100.0)
        meter.begin_proposal("case/second")
        self.assertEqual(meter.window_totals()["tokens"], 0)
        self.assertEqual(meter.cumulative_totals()["tokens"], 400)
        self.assertEqual(meter.cumulative_totals()["proposalWindows"], 2)

    def test_remaining_is_reported_against_the_current_window(self) -> None:
        meter = _meter()
        _charge(meter, tokens=250, latency=50.0)
        remaining = meter.remaining()
        self.assertEqual(remaining["tokens"], BUDGET.max_total_tokens - 250)
        self.assertEqual(remaining["toolCalls"], BUDGET.max_tool_calls - 1)
        self.assertAlmostEqual(remaining["latencyMs"], BUDGET.max_latency_ms - 50.0)

    def test_a_sibling_meter_carries_the_identical_budget(self) -> None:
        meter = _meter()
        sibling = meter.sibling(meter_id="other")
        self.assertEqual(sibling.budget, meter.budget)
        self.assertEqual(sibling.records, ())

    def test_a_meter_needs_a_real_budget(self) -> None:
        with self.assertRaises(TypeError):
            AgentBudgetMeter({"max_total_tokens": 10})  # type: ignore[arg-type]


class TheTwoLLMArmsShareOneCeiling(unittest.TestCase):
    """Task section 11's comparison condition, checked rather than asserted."""

    def test_the_pair_is_built_over_one_transport_and_one_budget(self) -> None:
        transport = scripted(role_separated_script())
        proposed, monolithic = build_comparable_llm_pair(transport=transport, budget=BUDGET)
        self.assertEqual(proposed.meter.budget, monolithic.meter.budget)
        self.assertEqual(
            proposed.describe()["modelIdentity"], monolithic.describe()["modelIdentity"]
        )

    def test_both_report_the_same_budget_fields(self) -> None:
        proposed, monolithic = build_comparable_llm_pair(
            transport=scripted(role_separated_script()), budget=BUDGET
        )
        for field in ("tokenBudget", "toolCallBudget", "latencyBudgetMs"):
            with self.subTest(field=field):
                self.assertEqual(proposed.describe()[field], monolithic.describe()[field])

    def test_their_meters_are_separate_so_neither_starves_the_other(self) -> None:
        # Equal budgets, not a shared meter: a shared one would mean whichever
        # arm ran first spent the other's allowance.
        proposed, monolithic = build_comparable_llm_pair(
            transport=scripted(role_separated_script()), budget=BUDGET
        )
        self.assertIsNot(proposed.meter, monolithic.meter)

    def test_the_same_ceiling_stops_both_at_the_same_spend(self) -> None:
        tight = AgentBudget(max_total_tokens=100, max_tool_calls=4, max_latency_ms=500.0)
        # 140 tokens per call, so the first call of either arm overspends.
        transport_a = scripted(role_separated_script(), input_tokens=100, output_tokens=40)
        transport_b = scripted([choice_reply()], input_tokens=100, output_tokens=40)
        proposed = RoleSeparatedLLMCoordinator(transport=transport_a, budget=tight)
        monolithic = MonolithicLLMStrategy(transport=transport_b, budget=tight)

        for strategy, expected_role in ((proposed, INTENT_ROLE), (monolithic, MONOLITHIC_ROLE)):
            with self.subTest(strategy=strategy.strategy_id):
                message = strategy.propose(**views())
                self.assertIsNotNone(message)
                self.assertEqual([record.cause for record in strategy.fallbacks], ["budget"])
                self.assertIn("tokens", strategy.fallbacks[0].detail)
                # Which role was speaking when the budget ran out, not "llm".
                self.assertEqual(strategy.fallbacks[0].agent_role, expected_role)

    def test_a_meter_disagreeing_with_its_budget_is_refused(self) -> None:
        other = AgentBudget(max_total_tokens=1, max_tool_calls=1, max_latency_ms=1.0)
        with self.assertRaises(ValueError):
            RoleSeparatedLLMCoordinator(
                transport=scripted([]), budget=BUDGET, meter=AgentBudgetMeter(other)
            )


class TheRunRecordGetsItsLineItems(unittest.TestCase):
    """Task section 12: agent token use, tool calls and latency, per run."""

    def test_the_role_separated_method_records_one_call_per_role(self) -> None:
        proposed = RoleSeparatedLLMCoordinator(
            transport=scripted(role_separated_script()), budget=BUDGET
        )
        proposed.propose(**views())
        telemetry = proposed.telemetry()
        self.assertEqual(
            [call["agentRole"] for call in telemetry["calls"]],
            [INTENT_ROLE, XAPP_ROLE, COORDINATOR_ROLE],
        )
        self.assertEqual(telemetry["totals"]["toolCalls"], 3)

    def test_the_monolithic_arm_records_exactly_one(self) -> None:
        monolithic = MonolithicLLMStrategy(
            transport=scripted([choice_reply()]), budget=BUDGET
        )
        monolithic.propose(**views())
        telemetry = monolithic.telemetry()
        self.assertEqual([call["agentRole"] for call in telemetry["calls"]], [MONOLITHIC_ROLE])
        self.assertEqual(telemetry["totals"]["toolCalls"], 1)

    def test_every_call_record_carries_tokens_latency_and_a_prompt_hash(self) -> None:
        proposed = RoleSeparatedLLMCoordinator(
            transport=scripted(role_separated_script()), budget=BUDGET
        )
        proposed.propose(**views())
        for call in proposed.telemetry()["calls"]:
            with self.subTest(agent_role=call["agentRole"]):
                self.assertEqual(call["totalTokens"], call["inputTokens"] + call["outputTokens"])
                self.assertGreater(call["latencyMs"], 0.0)
                self.assertTrue(call["promptHash"])
                self.assertEqual(call["toolCalls"], 1)

    def test_each_role_gets_its_own_prompt_hash(self) -> None:
        # Three separate calls, or the separation would be cosmetic.
        proposed = RoleSeparatedLLMCoordinator(
            transport=scripted(role_separated_script()), budget=BUDGET
        )
        proposed.propose(**views())
        hashes = {call["promptHash"] for call in proposed.telemetry()["calls"]}
        self.assertEqual(len(hashes), 3)

    def test_the_telemetry_is_json_serialisable(self) -> None:
        # It becomes a run record: normalized CSV/JSON/Parquet (§12).
        proposed = RoleSeparatedLLMCoordinator(
            transport=scripted(role_separated_script()), budget=BUDGET
        )
        proposed.propose(**views())
        rendered = json.loads(json.dumps(proposed.telemetry()))
        self.assertEqual(rendered["proposals"], 1)
        self.assertEqual(rendered["fallbacks"], [])

    def test_a_fallback_is_counted_with_the_cause_that_produced_it(self) -> None:
        proposed = RoleSeparatedLLMCoordinator(
            transport=scripted(["not json at all"]), budget=BUDGET
        )
        proposed.propose(**views())
        fallbacks = proposed.telemetry()["fallbacks"]
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(fallbacks[0]["cause"], "not-json")
        self.assertEqual(fallbacks[0]["agentRole"], INTENT_ROLE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
