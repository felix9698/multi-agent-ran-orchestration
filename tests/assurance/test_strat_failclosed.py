"""Two ways a §11 ceiling could be walked around, and the refusals that close them.

Authority: ``task_0820_unified_ota_assurance_system.md`` section 11 ("전략이
바뀌어도 ... deterministic fallback으로 유한 종료한다", "monolithic LLM은
proposed method와 ... 통제된 동등 token/tool-call budget을 사용한다"), design
section 12.

Both holes were found by adversarial review of the first Gate 6 commit
(``d9b33a5a8``) and share a shape worth naming: a guard that is correct on
every input it was written for, and vacuous on one it was not.

* **The empty applicable set.**  The consistency check between the xApp role
  and the coordinator role was written ``if applicable and candidate_id not
  in applicable``.  The leading guard exists to skip the check when there is
  nothing to check against -- but the xApp role controls whether there is.
  Rating every candidate inapplicable emptied the set, skipped the check, and
  let any choice through unrefused.
* **The unpriceable token count.**  The meter enforced its ceiling on
  whatever number it was handed, and the transport handed it
  ``int(getattr(response, "input_tokens", 0) or 0)``.  A provider reporting
  no usage was priced at zero and could run without limit; a provider
  reporting a negative count *refunded* allowance, so one ``-10_000`` bought
  back a whole window.  In both cases the ceiling stayed nominally uncrossed
  while the spend behind it was unknown.

Each test below fails against the code as it stood before the fix -- see
``EveryClaimHereIsAMutationTest`` for the two lines whose reversal reproduces
each hole -- and both apply to the proposed and the monolithic arm alike,
because §11's equal budgets are only equal if they are equally hard to evade.
"""

from __future__ import annotations

import unittest

from assurance.advisors.strategies import (
    AgentBudget,
    AgentBudgetMeter,
    BudgetExceeded,
    MonolithicLLMStrategy,
    RoleSeparatedLLMCoordinator,
    build_comparable_llm_pair,
)
from assurance.advisors.strategies.budget import NEGATIVE, OVER_LIMIT, UNMETERED
from assurance.advisors.strategies.llm_coordinator import XAPP_ROLE

from tests.assurance.strategy_support import (
    AVAILABLE_IDS,
    all_inapplicable_reply,
    choice_reply,
    intent_reply,
    role_separated_script,
    scripted,
    views,
)

BUDGET = AgentBudget(max_total_tokens=1000, max_tool_calls=4, max_latency_ms=500.0)


def _all_inapplicable_script(chosen: str = "candidate/000001"):
    """The degenerate conversation: nothing applicable, then a choice anyway."""
    return [intent_reply(), all_inapplicable_reply(), choice_reply(candidate_id=chosen)]


class RatingEverythingInapplicableIsNotAWayThrough(unittest.TestCase):
    """Defect 3.  An empty applicable set is a fallback trigger, not a bypass."""

    def _run(self, chosen: str = "candidate/000001"):
        transport = scripted(_all_inapplicable_script(chosen))
        strategy = RoleSeparatedLLMCoordinator(transport=transport, budget=BUDGET)
        return strategy, transport, strategy.propose(**views())

    def test_the_degenerate_verdict_falls_back_deterministically(self) -> None:
        strategy, _, message = self._run()
        self.assertIsNotNone(message, "a fallback must still propose")
        self.assertEqual(
            [record.cause for record in strategy.fallbacks], ["no-applicable-candidate"]
        )
        self.assertEqual(strategy.fallbacks[0].agent_role, XAPP_ROLE)

    def test_the_choice_does_not_pass_unrefused(self) -> None:
        # Before the fix the coordinator role's pick was returned verbatim,
        # with no fallback recorded.  Now the proposal is the deterministic
        # fallback's, and the fallback names itself in the rationale.
        strategy, _, message = self._run(chosen="candidate/000003")
        self.assertIn("deterministic fallback", message.body.rationale)
        self.assertNotEqual(strategy.fallbacks, ())

    def test_the_fallback_still_names_an_available_candidate(self) -> None:
        _, _, message = self._run()
        self.assertIn(message.body.candidate_id, AVAILABLE_IDS)

    def test_it_is_refused_before_the_third_call_is_made(self) -> None:
        # Asking the coordinator role to choose from an empty set is the step
        # that creates the bypass, so it is not taken: two calls, not three.
        _, transport, _ = self._run()
        self.assertEqual(transport.calls, 2)

    def test_the_check_is_unconditional_when_something_is_applicable(self) -> None:
        # The ordinary contradiction path is unchanged: this is a
        # strengthening, not a replacement.  ``role_separated_script()[1]`` is
        # the healthy xApp answer -- 000000 applicable, 000001 not -- so
        # choosing 000001 is a genuine disagreement between the roles.
        strategy = RoleSeparatedLLMCoordinator(
            transport=scripted(
                [
                    intent_reply(),
                    role_separated_script()[1],
                    choice_reply(candidate_id="candidate/000001"),
                ]
            ),
            budget=BUDGET,
        )
        strategy.propose(**views())
        self.assertEqual([record.cause for record in strategy.fallbacks], ["contradiction"])

    def test_a_healthy_conversation_is_unaffected(self) -> None:
        strategy = RoleSeparatedLLMCoordinator(
            transport=scripted(role_separated_script()), budget=BUDGET
        )
        message = strategy.propose(**views())
        self.assertEqual(message.body.candidate_id, "candidate/000000")
        self.assertEqual(strategy.fallbacks, ())


class AnUnpriceableCallCannotBuyBudget(unittest.TestCase):
    """Defect 4.  The meter refuses what it cannot price, on both arms."""

    def _meter(self) -> AgentBudgetMeter:
        meter = AgentBudgetMeter(BUDGET, meter_id="failclosed-meter")
        meter.begin_proposal("case/failclosed")
        return meter

    def _charge(self, meter: AgentBudgetMeter, **overrides):
        arguments = dict(
            agent_role="r",
            model_identity="mock/scripted",
            prompt_hash="prompt-hash",
            input_tokens=10,
            output_tokens=10,
            latency_ms=1.0,
        )
        arguments.update(overrides)
        return meter.charge(**arguments)

    # -- missing counts ---------------------------------------------------

    def test_a_missing_input_count_is_refused_not_read_as_zero(self) -> None:
        meter = self._meter()
        with self.assertRaises(BudgetExceeded) as caught:
            self._charge(meter, input_tokens=None)
        self.assertEqual(caught.exception.reason, UNMETERED)
        self.assertEqual(caught.exception.dimension, "tokens")

    def test_a_missing_output_count_is_refused_too(self) -> None:
        meter = self._meter()
        with self.assertRaises(BudgetExceeded):
            self._charge(meter, output_tokens=None)

    def test_an_unpriced_call_is_still_recorded_as_unpriceable(self) -> None:
        # Honesty: the call happened, and the record says the provider
        # reported nothing rather than reporting zero.
        meter = self._meter()
        with self.assertRaises(BudgetExceeded):
            self._charge(meter, input_tokens=None)
        record = meter.records[0]
        self.assertIsNone(record.input_tokens)
        self.assertIsNone(record.total_tokens)
        self.assertFalse(record.is_priceable)
        self.assertFalse(record.to_canonical_dict()["priceable"])

    def test_one_unpriced_call_closes_the_window(self) -> None:
        # The running total is no longer a true statement about spend, and a
        # ceiling checked against an untrue total is not a ceiling.
        meter = self._meter()
        with self.assertRaises(BudgetExceeded):
            self._charge(meter, input_tokens=None)
        with self.assertRaises(BudgetExceeded) as caught:
            meter.ensure_room()
        self.assertEqual(caught.exception.reason, UNMETERED)
        self.assertEqual(dict(meter.remaining()), {"tokens": 0, "toolCalls": 0, "latencyMs": 0.0})
        self.assertTrue(meter.window_totals()["unmetered"])

    def test_the_run_record_counts_what_could_not_be_priced(self) -> None:
        # A token total that looks small because calls went unpriced is not
        # the same run as one that genuinely spent little.
        meter = self._meter()
        with self.assertRaises(BudgetExceeded):
            self._charge(meter, input_tokens=None)
        self.assertEqual(meter.cumulative_totals()["unpriceableCalls"], 1)

    # -- negative counts --------------------------------------------------

    def test_a_negative_count_is_refused(self) -> None:
        meter = self._meter()
        with self.assertRaises(BudgetExceeded) as caught:
            self._charge(meter, input_tokens=-10_000)
        self.assertEqual(caught.exception.reason, NEGATIVE)

    def test_a_negative_count_does_not_refund_the_window(self) -> None:
        # The hole in one line: before the fix, ``window.tokens += -10_000``
        # bought back an entire window's allowance.
        meter = self._meter()
        self._charge(meter, input_tokens=400, output_tokens=400)
        spent = meter.window_totals()["tokens"]
        with self.assertRaises(BudgetExceeded):
            self._charge(meter, input_tokens=-10_000, output_tokens=0)
        self.assertEqual(meter.window_totals()["tokens"], spent)
        self.assertEqual(meter.remaining()["tokens"], 0)

    def test_a_negative_latency_is_refused_as_well(self) -> None:
        # And is reported against the latency dimension, not the token one:
        # a refusal that named the wrong column would send whoever reads the
        # run record looking in the wrong place.
        meter = self._meter()
        with self.assertRaises(BudgetExceeded) as caught:
            self._charge(meter, latency_ms=-5_000.0)
        self.assertEqual(caught.exception.dimension, "latencyMs")
        self.assertEqual(caught.exception.reason, NEGATIVE)

    def test_an_ordinary_overspend_still_reports_over_limit(self) -> None:
        # The three reasons stay distinguishable, so §12's fallback breakdown
        # can tell "spent too much" from "could not be priced".
        meter = self._meter()
        with self.assertRaises(BudgetExceeded) as caught:
            self._charge(meter, input_tokens=BUDGET.max_total_tokens + 1)
        self.assertEqual(caught.exception.reason, OVER_LIMIT)

    # -- both arms --------------------------------------------------------

    def test_neither_arm_can_run_on_a_provider_that_reports_no_usage(self) -> None:
        proposed = RoleSeparatedLLMCoordinator(
            transport=scripted(role_separated_script(), input_tokens=None),
            budget=BUDGET,
        )
        monolithic = MonolithicLLMStrategy(
            transport=scripted([choice_reply()], input_tokens=None), budget=BUDGET
        )
        for strategy in (proposed, monolithic):
            with self.subTest(strategy=strategy.strategy_id):
                message = strategy.propose(**views())
                self.assertIsNotNone(message)
                self.assertEqual([r.cause for r in strategy.fallbacks], ["budget"])
                self.assertIn(UNMETERED, strategy.fallbacks[0].detail)
                self.assertIn(message.body.candidate_id, AVAILABLE_IDS)

    def test_neither_arm_can_run_on_a_provider_that_reports_negative_usage(self) -> None:
        proposed = RoleSeparatedLLMCoordinator(
            transport=scripted(role_separated_script(), output_tokens=-10_000),
            budget=BUDGET,
        )
        monolithic = MonolithicLLMStrategy(
            transport=scripted([choice_reply()], output_tokens=-10_000), budget=BUDGET
        )
        for strategy in (proposed, monolithic):
            with self.subTest(strategy=strategy.strategy_id):
                message = strategy.propose(**views())
                self.assertIsNotNone(message)
                self.assertEqual([r.cause for r in strategy.fallbacks], ["budget"])
                self.assertIn(NEGATIVE, strategy.fallbacks[0].detail)

    def test_the_refusal_stops_the_arm_that_makes_three_calls_at_the_first(self) -> None:
        # A per-window ceiling is worthless if an unpriceable first call lets
        # the remaining two through.
        transport = scripted(role_separated_script(), input_tokens=None)
        RoleSeparatedLLMCoordinator(transport=transport, budget=BUDGET).propose(**views())
        self.assertEqual(transport.calls, 1)

    def test_both_arms_still_report_the_identical_ceiling(self) -> None:
        # §11's comparison condition survives the strengthening.
        proposed, monolithic = build_comparable_llm_pair(
            transport=scripted(role_separated_script(), input_tokens=None), budget=BUDGET
        )
        proposed.propose(**views())
        monolithic.propose(**views())
        for field in ("tokenBudget", "toolCallBudget", "latencyBudgetMs"):
            with self.subTest(field=field):
                self.assertEqual(proposed.describe()[field], monolithic.describe()[field])
        self.assertEqual(
            proposed.telemetry()["totals"]["unpriceableCalls"],
            monolithic.telemetry()["totals"]["unpriceableCalls"],
        )


class TheTransportReportsRatherThanPrices(unittest.TestCase):
    """The layer split the fix depends on."""

    def test_a_provider_that_reports_nothing_yields_none_not_zero(self) -> None:
        completion = scripted(["{}"], input_tokens=None, output_tokens=None).complete(
            system_prompt="s", prompt="p"
        )
        self.assertIsNone(completion.input_tokens)
        self.assertIsNone(completion.total_tokens)

    def test_a_negative_count_is_passed_through_unclamped(self) -> None:
        # Clamping here would hide the misreport from the meter and from the
        # run record; refusing is the meter's decision, not the transport's.
        completion = scripted(["{}"], output_tokens=-5).complete(
            system_prompt="s", prompt="p"
        )
        self.assertEqual(completion.output_tokens, -5)

    def test_the_real_backend_adapter_makes_the_same_distinction(self) -> None:
        from assurance.advisors.strategies import LLMBackendTransport

        class Response:
            success = True
            content = "{}"
            model = "stand-in/model"
            latency_ms = 4.0
            output_tokens = 3
            # ``input_tokens`` deliberately absent: the shape of a provider
            # that does not report prompt usage.

        class Backend:
            def is_available(self) -> bool:
                return True

            def generate(self, prompt, system_prompt=""):
                return Response()

        completion = LLMBackendTransport(Backend()).complete(system_prompt="s", prompt="p")
        self.assertIsNone(completion.input_tokens)
        self.assertEqual(completion.output_tokens, 3)
        self.assertIsNone(completion.total_tokens)


class EveryClaimHereIsAMutationTest(unittest.TestCase):
    """Where each hole was, so reverting the fix is a one-line experiment.

    Recorded as a test rather than a comment so the two lines stay findable:
    a strengthening nobody can locate is one somebody will undo.
    """

    def test_the_applicable_guard_is_unconditional(self) -> None:
        import inspect

        from assurance.advisors.strategies import llm_coordinator

        source = inspect.getsource(llm_coordinator)
        self.assertIn("if candidate_id not in applicable:", source)
        # The mutation: restoring ``if applicable and`` makes
        # ``RatingEverythingInapplicableIsNotAWayThrough`` fail.
        self.assertNotIn("if applicable and candidate_id not in applicable:", source)

    def test_the_meter_prices_before_it_accumulates(self) -> None:
        import inspect

        from assurance.advisors.strategies import budget

        source = inspect.getsource(budget.AgentBudgetMeter.charge)
        # The mutation: deleting this branch makes
        # ``AnUnpriceableCallCannotBuyBudget`` fail.
        self.assertIn("if not record.is_priceable:", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
