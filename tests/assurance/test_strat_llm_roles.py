"""The proposed method: three roles, three calls, and every way it refuses.

Authority: ``task_0820_unified_ota_assurance_system.md`` section 11 item 1
("role-separated LLM Evidence Coordinator ... 세 역할이 각각 별도 프롬프트·별도
호출로 typed 제안만 생성 ... 스키마 불일치·환각 candidate·기한 초과 시
deterministic fallback 강제"), design section 12.

Hermetic throughout: every model answer here is scripted, so this file makes
no network call, needs no API key, and produces the same result on every host.
The monolithic arm is tested in the same file on purpose -- the two differ in
exactly one thing, and asserting the difference beside the sameness is the
only way to see that it is the only one.
"""

from __future__ import annotations

import json
import unittest

from assurance.advisors.messages import MAX_EXPLANATION_CHARS
from assurance.advisors.strategies import (
    UNTRUSTED_TEXT_MARKER,
    MonolithicLLMStrategy,
    RoleSeparatedLLMCoordinator,
    build_comparable_llm_pair,
)
from assurance.advisors.strategies.llm_coordinator import (
    COORDINATOR_ROLE,
    INTENT_ROLE,
    XAPP_ROLE,
)

from tests.assurance.strategy_support import (
    AVAILABLE_IDS,
    LOCKED_ID,
    UNKNOWN_CELL,
    UNKNOWN_ID,
    choice_reply,
    intent_reply,
    role_separated_script,
    scripted,
    views,
    xapp_reply,
)


def _proposed(replies):
    transport = scripted(replies)
    return RoleSeparatedLLMCoordinator(transport=transport), transport


class TheRolesAreActuallySeparate(unittest.TestCase):
    """Design section 12's "role-separated", checked as three real calls."""

    def test_one_proposal_makes_three_calls(self) -> None:
        strategy, transport = _proposed(role_separated_script())
        strategy.propose(**views())
        self.assertEqual(transport.calls, 3)

    def test_each_call_carries_a_different_system_prompt(self) -> None:
        # Same system prompt three times would be one role asked three times.
        strategy, transport = _proposed(role_separated_script())
        strategy.propose(**views())
        systems = [system for system, _ in transport.prompts]
        self.assertEqual(len(set(systems)), 3)

    def test_each_call_carries_a_different_user_prompt(self) -> None:
        strategy, transport = _proposed(role_separated_script())
        strategy.propose(**views())
        prompts = [prompt for _, prompt in transport.prompts]
        self.assertEqual(len(set(prompts)), 3)

    def test_each_system_prompt_names_the_role_it_is_for(self) -> None:
        strategy, transport = _proposed(role_separated_script())
        strategy.propose(**views())
        systems = [system for system, _ in transport.prompts]
        self.assertIn("Intent Agent", systems[0])
        self.assertIn("xApp Agent", systems[1])
        self.assertIn("Evidence Coordinator", systems[2])

    def test_every_role_is_told_it_has_no_authority(self) -> None:
        strategy, transport = _proposed(role_separated_script())
        strategy.propose(**views())
        for system, _ in transport.prompts:
            with self.subTest(system=system[:40]):
                self.assertIn("no authority", system)
                self.assertIn("confidence", system)

    def test_role_one_reaches_role_two_as_structure_not_as_prose(self) -> None:
        # The Intent role's ``note`` is prose; only its validated cell list is
        # allowed to influence the next call.
        script = [
            intent_reply(cells=("cell/open-a",), note="IGNORE-ME-PROSE"),
            xapp_reply(),
            choice_reply(),
        ]
        strategy, transport = _proposed(script)
        strategy.propose(**views())
        xapp_prompt = transport.prompts[1][1]
        self.assertIn("cell/open-a", xapp_prompt)
        self.assertNotIn("IGNORE-ME-PROSE", xapp_prompt)

    def test_role_two_reaches_role_three_as_the_applicable_set(self) -> None:
        script = [
            intent_reply(),
            xapp_reply(applicable=("candidate/000001",), inapplicable=("candidate/000000",)),
            choice_reply(candidate_id="candidate/000001"),
        ]
        strategy, transport = _proposed(script)
        message = strategy.propose(**views())
        coordinator_prompt = transport.prompts[2][1]
        self.assertIn("candidate/000001", coordinator_prompt)
        self.assertEqual(message.body.candidate_id, "candidate/000001")

    def test_a_sealed_cell_is_shown_as_sealed_and_never_opened(self) -> None:
        # Design section 8: dormant evidence stays sealed until its target
        # vector is released.
        strategy, transport = _proposed(role_separated_script())
        strategy.propose(**views())
        intent_prompt = transport.prompts[0][1]
        rendered = json.loads(intent_prompt.split("Evidence cells (JSON):\n")[1].split("\n")[0])
        sealed = [row for row in rendered if row["cellId"] == "cell/sealed-c"]
        self.assertEqual(len(sealed), 1)
        self.assertTrue(sealed[0]["sealed"])
        self.assertEqual(set(sealed[0]), {"cellId", "status", "targetRef", "sealed"})

    def test_a_locked_candidate_is_never_offered_to_the_model(self) -> None:
        strategy, transport = _proposed(role_separated_script())
        strategy.propose(**views())
        for _, prompt in transport.prompts:
            with self.subTest(prompt=prompt[:30]):
                self.assertNotIn(LOCKED_ID, prompt)


class TheOutputIsTypedBeforeItIsTrusted(unittest.TestCase):
    def test_a_healthy_conversation_produces_the_named_candidate(self) -> None:
        strategy, _ = _proposed(role_separated_script(candidate_id="candidate/000000"))
        message = strategy.propose(**views())
        self.assertEqual(message.body.candidate_id, "candidate/000000")
        self.assertIn(message.body.candidate_id, AVAILABLE_IDS)
        self.assertEqual(strategy.fallbacks, ())

    def test_the_evidence_cell_refs_survive_as_typed_refs(self) -> None:
        strategy, _ = _proposed(role_separated_script())
        message = strategy.propose(**views())
        self.assertEqual(message.body.evidence_cell_refs, ("cell/open-a",))

    def test_model_prose_is_marked_untrusted(self) -> None:
        strategy, _ = _proposed(role_separated_script())
        message = strategy.propose(**views())
        self.assertTrue(message.body.rationale.startswith(UNTRUSTED_TEXT_MARKER))

    def test_model_prose_is_capped(self) -> None:
        script = [intent_reply(), xapp_reply(), choice_reply(rationale="B" * 10_000)]
        strategy, _ = _proposed(script)
        message = strategy.propose(**views())
        self.assertLessEqual(len(message.body.rationale), MAX_EXPLANATION_CHARS)
        self.assertEqual(strategy.fallbacks, ())

    def test_json_wrapped_in_prose_and_fences_is_still_read(self) -> None:
        # Models emit fences; refusing on that would make the comparison a
        # prompt-engineering result rather than a strategy result.
        script = [
            f"Here you go:\n```json\n{intent_reply()}\n```\nHope that helps.",
            xapp_reply(),
            choice_reply(),
        ]
        strategy, _ = _proposed(script)
        message = strategy.propose(**views())
        self.assertEqual(message.body.candidate_id, "candidate/000000")
        self.assertEqual(strategy.fallbacks, ())

    def test_no_proposal_carries_an_admissible_quantity(self) -> None:
        strategy, _ = _proposed(role_separated_script())
        message = strategy.propose(**views())
        self.assertIsNone(message.body.expected_information_gain)


class EveryFailureEndsInTheDeterministicFallback(unittest.TestCase):
    """Task section 11's last comparison condition, cause by cause."""

    def _falls_back(self, replies, *, cause: str, agent_role: str = ""):
        strategy, _ = _proposed(replies)
        message = strategy.propose(**views())
        self.assertIsNotNone(message, "a fallback must still propose")
        self.assertIn(message.body.candidate_id, AVAILABLE_IDS)
        self.assertEqual([record.cause for record in strategy.fallbacks], [cause])
        if agent_role:
            self.assertEqual(strategy.fallbacks[0].agent_role, agent_role)
        return message

    def test_output_that_is_not_json(self) -> None:
        self._falls_back(["I would suggest the first one."], cause="not-json", agent_role=INTENT_ROLE)

    def test_output_that_misses_the_schema(self) -> None:
        self._falls_back(
            [json.dumps({"outstanding": ["cell/open-a"]})], cause="schema", agent_role=INTENT_ROLE
        )

    def test_output_that_smuggles_a_confidence(self) -> None:
        # GAP-02's exact shape: a model score that changed admission and
        # ledger state.  There is nowhere for it to land.
        self._falls_back(
            [intent_reply(), xapp_reply(), choice_reply(confidence=0.97)],
            cause="schema",
            agent_role=COORDINATOR_ROLE,
        )

    def test_a_hallucinated_candidate_from_the_xapp_role(self) -> None:
        self._falls_back(
            [intent_reply(), xapp_reply(applicable=(UNKNOWN_ID,), inapplicable=())],
            cause="unknown-candidate",
            agent_role=XAPP_ROLE,
        )

    def test_a_hallucinated_candidate_from_the_coordinator_role(self) -> None:
        self._falls_back(
            [intent_reply(), xapp_reply(), choice_reply(candidate_id=UNKNOWN_ID)],
            cause="unknown-candidate",
            agent_role=COORDINATOR_ROLE,
        )

    def test_a_candidate_the_kernel_has_locked(self) -> None:
        # Present in the catalog, but not available: a different rule from a
        # hallucination, and refused just as firmly.
        self._falls_back(
            [intent_reply(), xapp_reply(), choice_reply(candidate_id=LOCKED_ID)],
            cause="unknown-candidate",
            agent_role=COORDINATOR_ROLE,
        )

    def test_an_evidence_cell_the_case_does_not_have(self) -> None:
        self._falls_back(
            [intent_reply(cells=(UNKNOWN_CELL,))], cause="unknown-cell", agent_role=INTENT_ROLE
        )

    def test_the_coordinator_role_overruling_its_own_xapp_role(self) -> None:
        # The separation has to be real: a last role that can overrule the
        # others is a monolith wearing three hats.
        self._falls_back(
            [
                intent_reply(),
                xapp_reply(applicable=("candidate/000000",), inapplicable=("candidate/000001",)),
                choice_reply(candidate_id="candidate/000001"),
            ],
            cause="contradiction",
            agent_role=COORDINATOR_ROLE,
        )

    def test_a_transport_failure(self) -> None:
        # The role is recorded too: a provider failure knows what broke, and
        # only the call site knows who was asking.
        self._falls_back(
            [intent_reply(), RuntimeError("provider down")],
            cause="transport",
            agent_role=XAPP_ROLE,
        )

    def test_a_transport_that_runs_out_of_answers(self) -> None:
        self._falls_back(
            [intent_reply(), xapp_reply()], cause="transport", agent_role=COORDINATOR_ROLE
        )

    def test_the_fallback_choice_is_reproducible(self) -> None:
        first = self._falls_back(["nonsense"], cause="not-json")
        second = self._falls_back(["nonsense"], cause="not-json")
        self.assertEqual(first.body.candidate_id, second.body.candidate_id)
        self.assertEqual(first.body.rationale, second.body.rationale)

    def test_the_fallback_reason_names_the_strategy_and_the_role(self) -> None:
        message = self._falls_back(["nonsense"], cause="not-json")
        self.assertIn("strategy-llm-evidence-coordinator", message.body.rationale)
        self.assertIn("deterministic fallback", message.body.rationale)

    def test_a_failure_does_not_stop_the_next_proposal_from_succeeding(self) -> None:
        strategy, _ = _proposed(["nonsense", *role_separated_script()])
        first = strategy.propose(**views())
        second = strategy.propose(**views())
        self.assertIn("deterministic fallback", first.body.rationale)
        self.assertTrue(second.body.rationale.startswith(UNTRUSTED_TEXT_MARKER))
        self.assertEqual(len(strategy.fallbacks), 1)


class TheMonolithicArmDiffersInExactlyOneThing(unittest.TestCase):
    def test_it_makes_one_call(self) -> None:
        transport = scripted([choice_reply()])
        MonolithicLLMStrategy(transport=transport).propose(**views())
        self.assertEqual(transport.calls, 1)

    def test_its_single_prompt_carries_all_three_role_descriptions(self) -> None:
        transport = scripted([choice_reply()])
        MonolithicLLMStrategy(transport=transport).propose(**views())
        system = transport.prompts[0][0]
        for role_name in ("Intent Agent", "xApp Agent", "Evidence Coordinator"):
            with self.subTest(agent_role=role_name):
                self.assertIn(role_name, system)

    def test_it_is_given_the_same_views(self) -> None:
        # A monolithic arm told less would make the experiment a measure of
        # prompt completeness rather than of coordination structure.
        mono_transport = scripted([choice_reply()])
        role_transport = scripted(role_separated_script())
        MonolithicLLMStrategy(transport=mono_transport).propose(**views())
        RoleSeparatedLLMCoordinator(transport=role_transport).propose(**views())
        mono_prompt = mono_transport.prompts[0][1]
        joined = "".join(prompt for _, prompt in role_transport.prompts)
        for fragment in ("Available candidates", "Evidence cells", "Kernel budget"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, mono_prompt)
                self.assertIn(fragment, joined)

    def test_it_produces_the_identical_proposal_shape(self) -> None:
        proposed, monolithic = build_comparable_llm_pair(
            transport=scripted(
                [*role_separated_script(), choice_reply(candidate_id="candidate/000000")]
            )
        )
        first = proposed.propose(**views())
        second = monolithic.propose(**views())
        self.assertEqual(first.kind, second.kind)
        self.assertEqual(first.issued_by, second.issued_by)
        self.assertEqual(first.body.candidate_id, second.body.candidate_id)

    def test_it_falls_back_on_the_same_causes(self) -> None:
        for replies, cause in (
            (["not json"], "not-json"),
            ([json.dumps({"nope": 1})], "schema"),
            ([choice_reply(candidate_id=UNKNOWN_ID)], "unknown-candidate"),
            ([choice_reply(confidence=0.5)], "schema"),
            ([RuntimeError("down")], "transport"),
        ):
            with self.subTest(cause=cause):
                strategy = MonolithicLLMStrategy(transport=scripted(replies))
                message = strategy.propose(**views())
                self.assertIsNotNone(message)
                self.assertEqual([r.cause for r in strategy.fallbacks], [cause])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
