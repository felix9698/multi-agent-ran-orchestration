"""Target, Control and Trajectory as single-call agents, hermetically.

A scripted backend stands in for every model: no API key, no network, no clock
beyond the wall time of a dict lookup.  What is asserted is the **contract**,
not a model's taste:

* every system prompt is the text of ``orc_task/SINGLE_CALL.md``, verbatim --
  the test reads the file, so the two cannot drift apart;
* the inputs are named exactly as ``SINGLE_CALL.md`` names them
  (``input.intents``, ``input.target_contract``, ``input.observed_best``, ...);
* no prompt ever carries the remaining trial count, the deadline, the budget or
  a termination option, while the observation timestamps and validity -- which
  ``SINGLE_CALL.md`` explicitly allows -- are there;
* an answer outside the closed set (a target past the owner's signed bound, a
  function the catalog does not have, a policy value off its list, an id that is
  not in the call's own inputs, unparseable text, no answer at all, an
  exception) is refused, repaired **once**, and then the deterministic rule
  answers with the reason on the record;
* the Target agent may answer with **questions** instead of a ``T``, which
  raises :class:`ClarificationNeeded` and is recorded rather than being an
  error;
* the generation options the executor chose reach the backend when it accepts
  them, and are silently dropped when it does not;
* the basic monolith's prompt carries no ``T``, no ``C`` and no grid, which is
  what makes it a fair comparison arm rather than a crippled one.
"""

from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from dataclasses import replace

from assurance.coordination import DecisionUnavailable
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

from assurance.coordination import (
    DETERMINISTIC, METHOD_BASIC_MONOLITH, METHOD_INTERNAL_MONOLITH,
    PHASE_CLARIFICATION, PHASE_FORMATION, PROMPT_SOURCE_HEADINGS, SYSTEM_PROMPTS,
    AnswerRefused, Authorization, BasicInputs, ClarificationNeeded, FunctionSelection,
    CompatibilityRules,
    ControlCandidate,
    ControlCandidates,
    ControlInputs, FunctionCatalog, GenerationOptions, Grid, JointEffectPredictor,
    MonolithFormInputs, NetworkState, Observation, RoleAgents, RoleModels,
    ScriptedResolver, TargetInputs, TrajectoryInputs, budget_terms_in,
    catalog_product_controls, expand_targets,
    intent_from_sentence, kpi_gaps, load_role_models_file, observed_best,
    save_role_models_file, validate_control_candidates,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SINGLE_CALL = REPO_ROOT / "orc_task" / "SINGLE_CALL.md"

HOME, AWAY = "12345678", "87654321"

def refused(call, *args, **kwargs):
    """Owner instruction 2026-09-19: an unusable answer is never replaced by a
    deterministic one -- the call raises, carrying its record."""
    try:
        call(*args, **kwargs)
    except DecisionUnavailable as exc:
        assert not exc.record.accepted
        assert exc.record.fallback_reason is None
        return exc.record
    raise AssertionError("a refused answer was replaced instead of raising")


CATALOG = FunctionCatalog.from_record([
    {"functionId": "steer", "xapp": "traffic-steering",
     "actionId": "AIC_UECellSteering_1.0.0", "scopes": ["ue@131", "ue@132"],
     "policyFields": {"servingCell": {"values": [HOME, AWAY], "unit": "nci"}},
     "prerequisites": ["the UE is attached"], "axis": "servingCell@<ue>"},
    {"functionId": "ue-dl-prb-cap", "xapp": "our_rc_xapp", "actionId": "102",
     "scopes": ["ue@132"],
     "policyFields": {"maxDlPrbs": {"values": [24, 12, 6], "unit": "PRB",
                                    "baseline": 24}},
     "axis": "dlPrbCap@<ue>"},
])
COMPATIBILITY = CompatibilityRules(
    mutually_exclusive=(("ue-dl-prb-cap", "ue-sched-priority"),),
    precedence=(("steer", "ue-dl-prb-cap"),))

VALID_T = {
    "t0": {"targetId": "T0", "requirements": {"I1.r1": 3.0, "I2.r1": 1.5}},
    "levels": {"I1.r1": {"steps": 2, "bound": 2.0},
               "I2.r1": {"steps": 1, "bound": 1.0}},
    "constraints": ["I1 never below 2.0 Mbps", "I2 never below 1.0 Mbps"],
    # The agent selects which authorized directions T carries; the field is
    # required, and leaving it out is a malformed answer rather than a licence
    # to keep the whole domain.
    "alternatives": [{"targetId": "TA", "levels": {"I1.r1": 1, "I2.r1": 0}},
                     {"targetId": "TB", "levels": {"I1.r1": 0, "I2.r1": 1}}],
    "ranking": {"costRule": "normalized-concession",
                "tieBreak": "lexicographic(D_max, D_mean)"},
    "rationale": "the directions worth trying, one per owner",
}
VALID_C = {
    "candidates": [
        {"controlId": "CA",
         "functions": [{"functionId": "ue-dl-prb-cap", "scope": "ue@132",
                        "policy": {"maxDlPrbs": "12"}}],
         "predicted": {"I1.r1": 3.2, "I2.r1": "unknown"},
         "uncertainty": {"I1.r1": 0.4},
         "predictedTarget": "T0",
         "applicability": ["UE 132 attached to 12345678"],
         "evidenceRefs": ["prediction:cfg-0007"],
         "rationale": "capping 132 frees PRBs for 131"},
        {"controlId": "CB",
         "functions": [{"functionId": "steer", "scope": "ue@131",
                        "policy": {"servingCell": AWAY}}],
         "predicted": {}, "uncertainty": {}, "predictedTarget": "T0",
         "applicability": [], "evidenceRefs": [], "rationale": "move 131 away"},
    ],
    "rationale": "one cap, one association",
}
VALID_PAIR = {"controlId": "CA", "targetId": "T1",
              "rationale": "the cheapest untried cell"}


def intents():
    return [
        intent_from_sentence(
            "I1: UE ueId=131 needs at least 3.0 Mbps downlink, relaxable in 2 "
            "steps to 2.0, owner ue1-video priority 1"),
        intent_from_sentence(
            "I2: UE ueId=132 needs at least 1.5 Mbps downlink, relaxable in 1 "
            "steps to 1.0, owner ue2-map priority 1"),
    ]


def network_state():
    state = NetworkState(cells={HOME: 5.0, AWAY: 5.0},
                         ues={"131": HOME, "132": HOME},
                         offered_load_mbps={"131": 4.0, "132": 4.0})
    record = state.to_record()
    record["appliedConfiguration"] = state.applied_configuration()
    record["unselectedFunctionRule"] = "baseline"
    return state, record


class AnAnswerMayLeaveT0ToTheCode(unittest.TestCase):
    """The new Target prompt says the code inserts ``T0``; the answer may omit it.

    Before 2026-09-17 ``_validated_contract`` refused any answer without ``t0``.
    The owner's drop tells the model "The code includes the unmodified original
    target T0", so a model that believes it would have had every formation call
    refused into the deterministic fallback -- silently, since a fallback is a
    normal outcome.  The schema keeps the field (the drop warns against
    discarding required schema fields); the code fills it when it is absent.
    """

    def test_a_missing_t0_is_filled_from_the_authorization(self):
        from assurance.coordination.agents import _validated_contract
        authorization = Authorization.from_intents(intents())
        contract, _notes = _validated_contract({"alternatives": []}, authorization)
        originals = {req_id: entry.original
                     for req_id, entry in authorization.requirements.items()}
        self.assertEqual({req: contract.t0.levels.get(req, 0) for req in originals},
                         {req: 0 for req in originals},
                         "the supplied T0 must sit at level 0, the unrelaxed original")

    def test_a_t0_that_moves_a_requirement_is_still_refused(self):
        """Filling an absent T0 must not weaken the guard on a supplied one."""
        from assurance.coordination.agents import _validated_contract
        authorization = Authorization.from_intents(intents())
        req_id = sorted(authorization.requirements)[0]
        moved = float(authorization.requirements[req_id].original) - 1.0
        with self.assertRaises((AnswerRefused, Exception)):
            _validated_contract(
                {"t0": {"targetId": "T0", "requirements": {req_id: moved}},
                 "alternatives": []}, authorization)


class ThePromptsAreTheOwnersOwnText(unittest.TestCase):
    """The six system prompts are ``SINGLE_CALL.md``, character for character."""

    @staticmethod
    def sections():
        text = SINGLE_CALL.read_text(encoding="utf-8")
        start = text.index("## 모델에 전달할 지시문")
        end = text.index("## 입력·출력 및 호출 설명")
        found = {}
        for part in re.split(r"^### ", text[start:end], flags=re.M)[1:]:
            heading, _, body = part.partition("\n")
            found[heading.strip()] = body.strip()
        return found

    def test_every_prompt_equals_its_section_of_the_markdown(self):
        sections = self.sections()
        self.assertEqual(len(SYSTEM_PROMPTS), 6)
        for role, heading in PROMPT_SOURCE_HEADINGS.items():
            self.assertIn(heading, sections)
            self.assertEqual(SYSTEM_PROMPTS[role], sections[heading],
                             f"the {role} prompt drifted from SINGLE_CALL.md")

    def test_no_prompt_carries_a_budget_a_deadline_or_a_trial_count(self):
        for role, prompt in SYSTEM_PROMPTS.items():
            self.assertEqual(budget_terms_in(prompt), [], f"{role} prompt")



class TheCoverageComparisonDiffersOnlyInTheControlInstruction(unittest.TestCase):
    """``three-agent-coverage``: one substituted prompt, nothing else.

    The comparison is only worth running if the two methods are identical
    everywhere else, so this asserts what is shared as firmly as what differs.
    """

    def test_only_the_control_prompt_is_substituted(self) -> None:
        from assurance.coordination.agents import (
            CONTROL_COVERAGE_SYSTEM_PROMPT, CONTROL_SYSTEM_PROMPT, ROLE_CONTROL,
            ROLE_TARGET, ROLE_TRAJECTORY, SYSTEM_PROMPTS, TARGET_SYSTEM_PROMPT,
            TRAJECTORY_SYSTEM_PROMPT)
        self.assertNotEqual(CONTROL_SYSTEM_PROMPT, CONTROL_COVERAGE_SYSTEM_PROMPT)
        # The SINGLE_CALL.md texts are untouched: the coverage prompt is ours,
        # kept beside them rather than replacing any of them.
        self.assertEqual(CONTROL_SYSTEM_PROMPT, SYSTEM_PROMPTS[ROLE_CONTROL])
        self.assertEqual(TARGET_SYSTEM_PROMPT, SYSTEM_PROMPTS[ROLE_TARGET])
        self.assertEqual(TRAJECTORY_SYSTEM_PROMPT, SYSTEM_PROMPTS[ROLE_TRAJECTORY])

    def test_the_coverage_method_sends_the_coverage_prompt(self) -> None:
        seen = {}

        def resolver(name):
            class _Backend:
                def generate(self, prompt, system_prompt="", options=None):
                    seen[system_prompt.split(".")[0]] = system_prompt
                    return _Response()
            return _Backend()

        class _Response:
            success = True
            content = "{}"
            parsed_json = {}
            latency_ms = 1.0
            input_tokens = 1
            output_tokens = 1
            options = None

        from assurance.coordination.agents import (
            CONTROL_COVERAGE_SYSTEM_PROMPT, CONTROL_SYSTEM_PROMPT,
            METHOD_THREE_AGENT, METHOD_THREE_AGENT_COVERAGE)
        for method, expected in ((METHOD_THREE_AGENT, CONTROL_SYSTEM_PROMPT),
                                 (METHOD_THREE_AGENT_COVERAGE,
                                  CONTROL_COVERAGE_SYSTEM_PROMPT)):
            with self.subTest(method=method):
                seen.clear()
                agents = RoleAgents(models=RoleModels(control="m", method=method),
                                    resolver=resolver)
                try:
                    agents.form_controls(ControlInputs())
                except DecisionUnavailable:
                    pass                            # only the prompt is under test
                self.assertIn(expected.split(".")[0], seen)


class TargetAgent(unittest.TestCase):
    """One call: the intents and the authorization become ``T``."""

    def setUp(self):
        self.intents = intents()
        self.authorization = Authorization.from_intents(self.intents)
        self.inputs = TargetInputs(intents=tuple(self.intents),
                                   authorization=self.authorization)

    def agents(self, answers, **kwargs):
        resolver, models = ScriptedResolver.by_role({"target": answers})
        return RoleAgents(models=models, resolver=resolver, **kwargs), resolver

    def test_the_inputs_are_named_exactly_as_single_call_names_them(self):
        payload = self.inputs.payload()
        # ``input.mandatory_targets`` is the v3.1 addition (amendment section
        # 3.3): the model is shown what code always includes so it adds beyond it.
        # 2026-09-20: no ``input.effect_evidence`` -- a T that depends on this
        # board's measurements cannot be reused on the next board.
        self.assertEqual(sorted(payload),
                         ["input.authorization", "input.intents",
                          "input.mandatory_targets", "input.network_state"])
        authorization = payload["input.authorization"]
        self.assertIn("jointConditions", authorization)
        self.assertEqual(authorization["requirements"]["I1.r1"]["steps"], 2)
        self.assertEqual(authorization["requirements"]["I1.r1"]["weight"], 1.0)
        # The revision of 2026-09-14 takes the weighted square out of the
        # running prompts: what a model is told is the rule that is applied.
        self.assertEqual(authorization["preference"]["costRule"],
                         "normalized-concession")
        self.assertIn("sentence", payload["input.intents"][0])

    def test_the_effect_evidence_it_gets_is_the_families_not_the_table(self):
        """Target selects concessions, so it needs to know what control can do.

        What it must not get is Control's per-configuration prediction table:
        that is the Cartesian block the handoff removed from every prompt.
        """
        inputs = TargetInputs(
            intents=tuple(self.intents), authorization=self.authorization,
            network_state={"ues": {"131": {"observedAt": "T0Z",
                                           "dlGoodputMbps": 1.2}}},
            effect_evidence={
                "predictorDescription": {"form": "capacity share"},
                "predictions": [{"configuration": {"dlPrbCap@131": "12",
                                                   "servingCell@131": "1"},
                                 "predicted": {"dlGoodputMbps@131": 2.0}}],
                "uncertaintyNote": "relative uncertainty on every goodput"})
        payload = inputs.payload()
        # 2026-09-20: the evidence is dropped entirely -- a target set that
        # depends on this board's measurements cannot be reused on the next
        # one.  The current composition stays: which UE sits on the crowded
        # cell is what makes one concession useful and another pointless.
        self.assertNotIn("input.effect_evidence", payload)
        self.assertEqual(1.2, payload["input.network_state"]["ues"]["131"]["dlGoodputMbps"])

    def test_a_caller_with_neither_state_nor_evidence_still_forms_targets(self):
        """Empty passes through; it never refuses the call."""
        agents, _resolver = self.agents([VALID_T])
        contract, record = agents.form_targets(TargetInputs(
            intents=tuple(self.intents), authorization=self.authorization))
        self.assertTrue(record.accepted)
        self.assertNotIn("input.effect_evidence", self.inputs.payload())

    def test_an_accepted_compact_answer_becomes_the_expanded_contract(self):
        agents, resolver = self.agents([VALID_T])
        contract, record = agents.form_targets(self.inputs)
        self.assertTrue(record.accepted)
        self.assertEqual(record.model, "scripted:target")
        self.assertEqual(record.phase, PHASE_FORMATION)
        # The two mandatory targets (T0 and this domain's one boundary) plus
        # the two additions the answer selected -- not the whole 2 x 3 expansion.
        self.assertEqual(len(contract.targets), 4)
        self.assertEqual(contract.provenance["rationale"], VALID_T["rationale"])
        self.assertEqual(resolver.calls[0].system_prompt, SYSTEM_PROMPTS["target"])

    def test_an_answer_without_the_alternatives_field_is_refused(self):
        """The omission that let a full-domain contract be stamped with a model.

        The field is required. Reading its absence as "did not narrow" kept all
        54 targets, accepted the call, and put the model's name on a selection
        it never made -- so a missing field is refused, repaired once, and then
        the deterministic rule answers with the reason on the record.
        """
        without = {key: value for key, value in VALID_T.items()
                   if key != "alternatives"}
        agents, _resolver = self.agents([without, without])
        with self.assertRaises(DecisionUnavailable) as caught:
            agents.form_targets(self.inputs)
        self.assertIn("no alternatives", caught.exception.reason)
        self.assertEqual(1, caught.exception.record.repair_retries)

    def test_an_explicitly_empty_selection_is_accepted_as_the_mandatory_set(self):
        agents, _resolver = self.agents([dict(VALID_T, alternatives=[])])
        contract, record = agents.form_targets(self.inputs)
        self.assertTrue(record.accepted)
        # T0 and the boundary: code includes them whatever the answer says.
        self.assertEqual(len(contract.targets), 2)

    def test_a_question_from_the_model_does_not_stop_the_formation(self):
        # 2026-09-20 (오너: "확인 질문이랑 제약 둘 다 빼"): the schema no longer asks
        # for missingInformation, and a question sent anyway is ignored -- an
        # unattended board has nobody to answer it.
        answer = dict(VALID_T, missingInformation=[
            {"intentId": "I2", "field": "bound",
             "question": "How far may the map data be relaxed?"}])
        agents, _resolver = self.agents([answer])
        contract, record = agents.form_targets(self.inputs)
        self.assertTrue(record.accepted)
        self.assertIn("T0", contract.target_ids)
        self.assertEqual((), tuple(record.questions or ()))

    def test_a_clarification_round_is_recorded_under_its_own_phase(self):
        agents, _resolver = self.agents([VALID_T])
        _contract, record = agents.form_targets(self.inputs,
                                               phase=PHASE_CLARIFICATION)
        self.assertEqual(record.phase, PHASE_CLARIFICATION)

    def test_a_bound_past_the_signed_one_is_clipped_and_reported(self):
        answer = dict(VALID_T, levels={"I1.r1": {"steps": 2, "bound": 0.5},
                                       "I2.r1": {"steps": 1, "bound": 1.0}})
        agents, _resolver = self.agents([answer])
        contract, record = agents.form_targets(self.inputs)
        self.assertTrue(record.accepted)
        # 2026-09-19: the answer no longer restates levels; a "levels" key is
        # ignored and the owner's signed range stands.
        self.assertTrue(any("signed steps stand" in note for note in record.dropped))
        for target in contract.targets:
            self.assertGreaterEqual(target.requirements["I1.r1"], 2.0)

    def test_a_wrong_t0_is_repaired_once_then_raises(self):
        broken = dict(VALID_T, t0={"targetId": "T0",
                                   "requirements": {"I1.r1": 2.0, "I2.r1": 1.5}})
        agents, resolver = self.agents([broken])
        with self.assertRaises(DecisionUnavailable) as caught:
            agents.form_targets(self.inputs)
        self.assertEqual(caught.exception.record.repair_retries, 1)
        self.assertIn("T0", caught.exception.reason)
        self.assertEqual(len(resolver.calls), 2)
        self.assertIn("was refused", resolver.calls[1].prompt)

    def test_the_repaired_answer_is_accepted_when_it_is_corrected(self):
        broken = dict(VALID_T, t0={"targetId": "T0", "requirements": {}})
        agents, _resolver = self.agents([broken, VALID_T])
        _contract, record = agents.form_targets(self.inputs)
        self.assertTrue(record.accepted)
        self.assertEqual(record.repair_retries, 1)

    def test_unparseable_text_no_answer_and_an_exception_all_raise(self):
        for answer in ("not JSON at all", None, RuntimeError("the socket died")):
            agents, _resolver = self.agents([answer])
            refused(agents.form_targets, self.inputs)

    def test_the_fallback_is_the_mandatory_set_not_the_whole_expansion(self):
        """Amendment section 3.3: "No silent truncation or full-54 fallback is
        allowed."  A failed or absent model yields the mandatory targets, which
        no one selected, recorded as the deterministic rule."""
        from assurance.coordination.tc import mandatory_contract
        agents = RoleAgents(models=RoleModels())
        contract, record = agents.form_targets(self.inputs)
        self.assertFalse(record.accepted)
        self.assertEqual([target.requirements for target in contract.targets],
                         [target.requirements for target in
                          mandatory_contract(expand_targets(self.authorization)).targets])
        self.assertLess(len(contract.targets), len(expand_targets(self.authorization).targets))

    def test_no_prompt_carries_a_budget_a_deadline_or_a_trial_count(self):
        agents, resolver = self.agents([VALID_T])
        agents.form_targets(self.inputs)
        for prompt in resolver.prompts:
            self.assertEqual(budget_terms_in(prompt), [])


class ControlAgent(unittest.TestCase):
    """One call: ``T`` and the function catalog become ``C``."""

    def setUp(self):
        self.authorization = Authorization.from_intents(intents())
        self.contract = expand_targets(self.authorization)
        self.state, self.record = network_state()
        self.predictor = JointEffectPredictor(self.state)
        self.inputs = ControlInputs(
            intents=tuple(intents()), authorization=self.authorization,
            target_contract=self.contract, function_catalog=CATALOG,
            compatibility=COMPATIBILITY, network_state=self.record,
            effect_evidence=self.predictor.effect_evidence(
                CATALOG.axes(), CATALOG.baselines(self.state.applied_configuration())),
            construction_policy={"retain": 8,
                                 "kpiDeficitRule": "sum of normalized shortfalls "
                                                   "against T0, lower first"})

    def agents(self, answers):
        resolver, models = ScriptedResolver.by_role({"control": answers})
        return RoleAgents(models=models, resolver=resolver,
                          predictor=self.predictor), resolver

    def test_the_inputs_are_the_six_single_call_positions(self):
        # 2026-09-20: the formed ``T`` is gone and the declared requirements
        # take its place, so Control no longer waits for Target.
        self.assertEqual(sorted(self.inputs.payload()), [
            "input.authorization", "input.compatibility",
            "input.construction_policy", "input.effect_evidence",
            "input.function_catalog", "input.intents",
            "input.network_state"])

    def test_control_is_not_told_which_target_to_prefer(self):
        """The ranking is Target's and Trajectory's; Control gets the bounds."""
        authorization = self.inputs.payload()["input.authorization"]
        self.assertIn("requirements", authorization)
        self.assertNotIn("preference", authorization)

    def test_the_catalog_is_only_what_the_sitting_exposes(self):
        catalog = self.inputs.payload()["input.function_catalog"]
        self.assertEqual([row["functionId"] for row in catalog],
                         ["steer", "ue-dl-prb-cap"])
        for row in catalog:
            self.assertNotIn("mcs", json.dumps(row).lower())

    def test_an_accepted_answer_is_translated_onto_the_axes(self):
        agents, _resolver = self.agents([VALID_C])
        candidates, record = agents.form_controls(self.inputs)
        self.assertTrue(record.accepted)
        self.assertEqual(candidates.control_ids, ("C0", "CA", "CB"))
        self.assertEqual(candidates.candidate("CA").configuration["dlPrbCap@132"],
                         "12")
        self.assertEqual(candidates.candidate("CB").configuration["servingCell@131"],
                         AWAY)

    def test_the_executor_rederives_the_predicted_target_internally(self):
        # Owner instruction 2026-09-19: the predictor's target stays internal.
        agents, _resolver = self.agents([VALID_C])
        candidates, _record = agents.form_controls(self.inputs)
        candidate = candidates.candidate("CA")
        self.assertIn(candidate.predictor_target, self.contract.target_ids + ("",))
        self.assertNotIn("predictorTarget", candidate.to_record())
        self.assertFalse(any(str(ref).startswith("predictor")
                             for ref in candidate.evidence_refs))
        self.assertIn("prediction:cfg-0007", candidate.evidence_refs)

    def test_a_value_claim_in_the_answer_is_dropped(self):
        # 2026-09-20 (owner): Control states relations, not values.
        answer = json.loads(json.dumps(VALID_C))
        answer["candidates"][0]["predictedTarget"] = "T5"
        agents, _resolver = self.agents([answer])
        candidates, _record = agents.form_controls(self.inputs)
        self.assertEqual("", candidates.candidate("CA").predicted_target)
        self.assertEqual({}, candidates.candidate("CA").predicted)

    def test_a_function_the_catalog_does_not_have_is_refused(self):
        answer = {"candidates": [
            {"controlId": "CX", "functions": [
                {"functionId": "mcs-clamp", "scope": "ue@131",
                 "policy": {"mcs": "9"}}]}], "rationale": "..."}
        agents, _resolver = self.agents([answer])
        record = refused(agents.form_controls, self.inputs)
        self.assertEqual(record.repair_retries, 1)

    def test_a_policy_value_off_its_list_is_refused(self):
        answer = {"candidates": [
            {"controlId": "CX", "functions": [
                {"functionId": "ue-dl-prb-cap", "scope": "ue@132",
                 "policy": {"maxDlPrbs": "18"}}]}], "rationale": "..."}
        agents, _resolver = self.agents([answer])
        refused(agents.form_controls, self.inputs)

    def test_a_compatibility_refusal_drops_that_candidate_only(self):
        answer = {"candidates": [
            {"controlId": "CX", "functions": [
                {"functionId": "ue-dl-prb-cap", "scope": "ue@132",
                 "policy": {"maxDlPrbs": "12"}},
                {"functionId": "ue-dl-prb-cap", "scope": "ue@132",
                 "policy": {"maxDlPrbs": "6"}}]},
            VALID_C["candidates"][1]], "rationale": "..."}
        agents, _resolver = self.agents([answer])
        candidates, record = agents.form_controls(self.inputs)
        self.assertTrue(record.accepted)
        self.assertEqual(candidates.control_ids, ("C0", "CB"))
        self.assertTrue(any("CX" in note for note in record.dropped))

    def test_the_fallback_is_the_predictor_ranked_function_moves(self):
        agents = RoleAgents(models=RoleModels(), predictor=self.predictor)
        candidates, record = agents.form_controls(self.inputs)
        self.assertFalse(record.accepted)
        self.assertEqual(candidates.control_ids[0], "C0")
        self.assertGreater(len(candidates.candidates), 1)
        self.assertLessEqual(len(candidates.candidates), 9)
        for candidate in candidates.candidates[1:]:
            self.assertTrue(candidate.functions)


class TheRetainedCandidateCountIsTheDropsTwelve(unittest.TestCase):
    """``retain`` counts the candidates *besides* the baseline.

    The integrated reply of 2026-09-14 section 4: "It constructs at most 12
    configurations **including baseline**."  :func:`validate_control_candidates`
    prepends ``C0`` unconditionally and then caps on ``len(kept) - 1``, so the
    drop's twelve is ``retain = 11``.  Reading it as ``12`` would put thirteen
    configurations in every arm's ``C`` -- an off-by-one in the size of the
    search space each method is compared on.

    Pinned because nothing else pinned it.  The live runner passes ``--retain``
    explicitly, so a wrong default never fails a live run: it silently governs
    only the model Control arms' validation (``retain=getattr(inputs,
    "retain", None)``) and any caller that states no construction policy.
    """

    def setUp(self):
        self.baselines = CATALOG.baselines({})
        self.product = catalog_product_controls(
            CATALOG, self.baselines, compatibility=COMPATIBILITY)

    def validated(self, retain):
        candidates = ControlCandidates(candidates=self.product,
                                       action_space=CATALOG.axes(),
                                       catalog=CATALOG)
        kept, _dropped = validate_control_candidates(
            candidates, CATALOG.axes(), self.baselines, catalog=CATALOG,
            compatibility=COMPATIBILITY, retain=retain)
        return kept.candidates

    def test_the_default_is_eleven_besides_the_baseline(self):
        self.assertEqual(11, ControlInputs().retain)

    def test_the_default_is_twelve_configurations_including_the_baseline(self):
        kept = self.validated(ControlInputs().retain)
        self.assertEqual(12, len(kept))
        self.assertEqual("C0", kept[0].control_id)

    def test_retain_excludes_the_baseline_rather_than_counting_it(self):
        """The semantics the count hangs on: three retained is *four*
        configurations, because ``C0`` is not one of the three."""
        kept = self.validated(3)
        self.assertEqual(4, len(kept))
        self.assertEqual("C0", kept[0].control_id)

    def test_a_stated_retain_still_wins_over_the_default(self):
        self.assertEqual(
            6, ControlInputs(construction_policy={"retain": 6}).retain)


class TheDeterministicControlsSayWhatVanished(unittest.TestCase):
    """The fallback enumerates the catalog's product and then validates it, and
    both steps refuse combinations.  Neither refusal was collected: the
    product's ``dropped=`` out-parameter went unused and the validator's was
    assigned to ``_dropped``, so a combination that vanished during enumeration
    read exactly like one that was never enumerated.

    This arm has no ``CallRecord`` to say otherwise -- its three callers are
    ``fallback()`` closures invoked without one -- so ``provenance``, which
    ``to_record()`` already serializes, is the channel.
    """

    def fallback(self, compatibility):
        state, record = network_state()
        predictor = JointEffectPredictor(state)
        inputs = ControlInputs(
            target_contract=expand_targets(Authorization.from_intents(intents())),
            function_catalog=CATALOG, compatibility=compatibility,
            network_state=record,
            effect_evidence=predictor.effect_evidence(
                CATALOG.axes(),
                CATALOG.baselines(state.applied_configuration())),
            construction_policy={"retain": 8})
        agents = RoleAgents(models=RoleModels(), predictor=predictor)
        return agents.form_controls(inputs)

    def test_a_combination_the_rules_refused_says_why(self):
        candidates, record = self.fallback(
            CompatibilityRules(max_changed_entries=1))
        self.assertFalse(record.accepted)
        dropped = candidates.provenance["dropped"]
        self.assertTrue(dropped)
        # ``_refusal_note``'s shape, the one the validator's own drops use:
        # what was selected, then why it cannot be applied.
        self.assertTrue(any("per configuration" in note for note in dropped))
        self.assertTrue(all(" on ue@" in note and ": " in note for note in dropped))

    def test_an_enumeration_that_refused_nothing_records_nothing(self):
        candidates, _record = self.fallback(CompatibilityRules())
        self.assertEqual([], candidates.provenance["dropped"])

    def test_the_call_record_is_deliberately_left_alone(self):
        """Copying these onto ``record.dropped`` would change what every
        deterministic fallback reports, which is a separate decision."""
        candidates, record = self.fallback(
            CompatibilityRules(max_changed_entries=1))
        self.assertTrue(candidates.provenance["dropped"])
        self.assertEqual((), record.dropped)


class TrajectoryAgent(unittest.TestCase):
    """One call: the next control, and the target it is aimed at."""

    def setUp(self):
        self.authorization = Authorization.from_intents(intents())
        self.contract = expand_targets(self.authorization)
        self.state, self.record = network_state()
        self.predictor = JointEffectPredictor(self.state)
        agents = RoleAgents(models=RoleModels(), predictor=self.predictor)
        self.controls, _ = agents.form_controls(ControlInputs(
            target_contract=self.contract, function_catalog=CATALOG,
            compatibility=COMPATIBILITY, network_state=self.record,
            construction_policy={"retain": 6}))
        self.observations = (
            Observation(control_id=self.controls.control_ids[1], trial_index=1,
                        configuration=dict(self.state.applied_configuration()),
                        kpis={"dlGoodputMbps@131": 2.6, "dlGoodputMbps@132": 1.6},
                        observed_at="2026-09-07T10:00:00.000000Z",
                        window_end="2026-09-07T10:00:30.000000Z",
                        valid_until="2026-09-07T10:01:30.000000Z", valid=True),)
        self.inputs = TrajectoryInputs(
            target_contract=self.contract, control_candidates=self.controls,
            network_state=self.record, observations=self.observations,
            observed_best=observed_best(self.observations, self.contract),
            kpi_gaps=kpi_gaps({"dlGoodputMbps@131": 2.6, "dlGoodputMbps@132": 1.6},
                              self.contract))

    def agents(self, answers):
        resolver, models = ScriptedResolver.by_role({"trajectory": answers})
        return RoleAgents(models=models, resolver=resolver,
                          predictor=self.predictor), resolver

    def test_the_inputs_are_the_six_single_call_positions(self):
        self.assertEqual(sorted(self.inputs.payload()), [
            "input.control_candidates", "input.kpi_gaps", "input.network_state",
            "input.observations", "input.observed_best", "input.target_contract"])

    def test_the_baseline_is_stored_once_and_the_rest_are_deltas(self):
        """``C`` is encoded as a baseline plus policy deltas, not as N complete
        configurations with the prediction table written out twice."""
        block = self.inputs.payload()["input.control_candidates"]
        self.assertEqual([row["controlId"] for row in block],
                         list(self.controls.control_ids))
        self.assertEqual(block[0]["controlId"], "C0")
        self.assertTrue(block[0]["configuration"])  # the baseline, stored once
        self.assertGreater(len(block), 1)
        for row in block[1:]:
            with self.subTest(control=row["controlId"]):
                self.assertTrue(row["functions"])  # the policy delta ...
                self.assertNotIn("configuration", row)  # ... not its expansion
                self.assertNotIn("effectEstimate", row)  # ``predicted`` twice
                # 2026-09-19: these fallback columns carry no predictor output
                for gone in ("predicted", "uncertainty", "predictedTarget"):
                    self.assertNotIn(gone, row, gone)

    def test_a_candidate_without_functions_keeps_its_configuration(self):
        """A v1 candidate has no delta to carry, so compacting it would empty it."""
        controls = replace(self.controls, candidates=(
            self.controls.candidates[0],
            ControlCandidate(control_id="V1",
                             configuration={"dlPrbCap@132": "12"})))
        block = TrajectoryInputs(target_contract=self.contract,
                                 control_candidates=controls).payload()
        self.assertEqual(block["input.control_candidates"][1]["configuration"],
                         {"dlPrbCap@132": "12"})

    def test_the_observations_carry_their_validity(self):
        payload = self.inputs.payload()
        observation = payload["input.observations"][0]
        self.assertIn("validUntil", observation)
        self.assertIn("windowEnd", observation)
        self.assertTrue(observation["valid"])

    def test_the_executor_computes_observed_best_and_the_gaps(self):
        payload = self.inputs.payload()
        self.assertEqual(payload["input.observed_best"]["cost"], 1.0)
        self.assertEqual(
            payload["input.kpi_gaps"]["perRequirement"]["I1.r1"]["againstT0"][
                "shortfall"], 0.4)

    def test_no_history_at_all_is_an_empty_list_and_a_null(self):
        payload = TrajectoryInputs(target_contract=self.contract,
                                   control_candidates=self.controls).payload()
        self.assertEqual(payload["input.observations"], [])
        self.assertIsNone(payload["input.observed_best"])
        self.assertIsNone(payload["input.kpi_gaps"])

    def test_an_accepted_pair_resolves_to_ids_of_its_own_inputs(self):
        answer = dict(VALID_PAIR, controlId=self.controls.control_ids[2],
                      targetId="T1")
        agents, resolver = self.agents([answer])
        decision, record = agents.select_next(self.inputs)
        self.assertTrue(record.accepted)
        self.assertEqual(decision.control_id, self.controls.control_ids[2])
        self.assertEqual(decision.target_id, "T1")
        self.assertEqual(resolver.calls[0].system_prompt,
                         SYSTEM_PROMPTS["trajectory"])

    def test_an_id_that_is_not_in_the_inputs_is_refused(self):
        agents, _resolver = self.agents([{"controlId": "C99", "targetId": "T1",
                                          "rationale": "..."}])
        with self.assertRaises(DecisionUnavailable) as caught:
            agents.select_next(self.inputs)
        self.assertIn("C99", caught.exception.reason)

    def test_the_fallback_takes_an_untried_control_by_predicted_cost(self):
        agents = RoleAgents(models=RoleModels(), predictor=self.predictor)
        decision, record = agents.select_next(self.inputs)
        self.assertFalse(record.accepted)
        self.assertNotEqual(decision.control_id, self.controls.control_ids[1])
        self.assertIn(decision.target_id, self.contract.target_ids)

    def test_no_prompt_carries_a_budget_a_deadline_or_a_trial_count(self):
        agents, resolver = self.agents([VALID_PAIR])
        try:
            agents.select_next(self.inputs)
        except DecisionUnavailable:
            pass                                    # only the prompts are under test
        for prompt in resolver.prompts:
            self.assertEqual(budget_terms_in(prompt), [])


class TheGenerationOptionsReachTheBackend(unittest.TestCase):
    """What the model is asked to spend, and what happens when it cannot hear."""

    def setUp(self):
        self.inputs = TargetInputs(intents=tuple(intents()),
                                   authorization=Authorization.from_intents(intents()))

    def test_the_role_default_is_passed_to_a_backend_that_accepts_options(self):
        resolver, models = ScriptedResolver.by_role({"target": [VALID_T]})
        agents = RoleAgents(models=models, resolver=resolver)
        _contract, record = agents.form_targets(self.inputs)
        sent = resolver.options_for("scripted:target")[0]
        self.assertEqual(sent["thinkingBudgetTokens"],
                         GenerationOptions.for_role("target").thinking_budget_tokens)
        self.assertEqual(record.options["source"], "default")

    def test_a_calibration_picks_the_budget_that_fits_the_validity(self):
        calibration = {"models": {"scripted:target": {"target": [
            {"thinkingBudgetTokens": 1000, "p95LatencyMs": 2000},
            {"thinkingBudgetTokens": 30000, "p95LatencyMs": 90000}]}}}
        resolver, models = ScriptedResolver.by_role({"target": [VALID_T]})
        agents = RoleAgents(models=models, resolver=resolver,
                            latency_calibration=calibration, min_validity_ms=10000)
        _contract, record = agents.form_targets(self.inputs)
        self.assertEqual(record.options["thinkingBudgetTokens"], 1000)
        self.assertEqual(record.options["source"], "calibration")

    def test_a_backend_that_does_not_take_options_is_called_without_them(self):
        seen = []

        class OldBackend:
            def generate(self, prompt, system_prompt=""):
                seen.append(prompt)
                return type("R", (), {"success": True, "content": json.dumps(VALID_T),
                                      "parsed_json": VALID_T, "latency_ms": 5.0,
                                      "input_tokens": 10, "output_tokens": 5})()

        agents = RoleAgents(models=RoleModels(target="old"),
                            resolver=lambda name: OldBackend())
        _contract, record = agents.form_targets(self.inputs)
        self.assertTrue(record.accepted)
        self.assertEqual(len(seen), 1)
        self.assertTrue(record.options)          # recorded even though not sent


class TheInternalMonolith(unittest.TestCase):
    """One model, a formation call and then selection calls."""

    def setUp(self):
        self.authorization = Authorization.from_intents(intents())
        self.state, self.record = network_state()
        self.predictor = JointEffectPredictor(self.state)
        self.inputs = MonolithFormInputs(
            target=TargetInputs(intents=tuple(intents()),
                                authorization=self.authorization),
            control=ControlInputs(function_catalog=CATALOG,
                                  compatibility=COMPATIBILITY,
                                  network_state=self.record,
                                  construction_policy={"retain": 6}))

    def test_the_formation_call_receives_both_roles_inputs_but_no_contract(self):
        payload = self.inputs.payload()
        self.assertIn("input.intents", payload)
        self.assertIn("input.function_catalog", payload)
        self.assertNotIn("input.target_contract", payload)

    def test_one_answer_produces_both_t_and_c(self):
        answer = dict(VALID_T)
        answer.update({"candidates": VALID_C["candidates"]})
        resolver, models = ScriptedResolver.by_role(
            {"monolith": [answer]}, method=METHOD_INTERNAL_MONOLITH)
        agents = RoleAgents(models=models, resolver=resolver,
                            predictor=self.predictor)
        (contract, candidates), record = agents.monolith_form(self.inputs)
        self.assertTrue(record.accepted)
        # The internal monolith selects its own T under the same rights as
        # Target: the two mandatory targets plus the two additions it carried.
        self.assertEqual(len(contract.targets), 4)
        self.assertEqual(candidates.control_ids, ("C0", "CA", "CB"))
        self.assertEqual(resolver.calls[0].system_prompt,
                         SYSTEM_PROMPTS["monolith-form"])

    def test_the_selection_call_uses_the_trajectory_instruction(self):
        contract = expand_targets(self.authorization)
        agents = RoleAgents(models=RoleModels(monolith=None,
                                              method=METHOD_INTERNAL_MONOLITH),
                            predictor=self.predictor)
        controls, _ = agents.form_controls(ControlInputs(
            target_contract=contract, function_catalog=CATALOG,
            compatibility=COMPATIBILITY, network_state=self.record,
            construction_policy={"retain": 4}))
        resolver, models = ScriptedResolver.by_role(
            {"monolith": [{"controlId": controls.control_ids[1], "targetId": "T0",
                           "rationale": "..."}]},
            method=METHOD_INTERNAL_MONOLITH)
        agents = RoleAgents(models=models, resolver=resolver,
                            predictor=self.predictor)
        decision, record = agents.monolith_select(TrajectoryInputs(
            target_contract=contract, control_candidates=controls,
            network_state=self.record))
        self.assertTrue(record.accepted)
        self.assertEqual(decision.control_id, controls.control_ids[1])
        self.assertEqual(resolver.calls[0].system_prompt,
                         SYSTEM_PROMPTS["monolith-select"])


class TheBasicMonolith(unittest.TestCase):
    """One model, one call, the raw material and nothing of ours."""

    def setUp(self):
        self.state, self.record = network_state()
        self.predictor = JointEffectPredictor(self.state)
        self.inputs = BasicInputs(
            intents=tuple(intents()),
            authorization=Authorization.from_intents(intents()),
            function_catalog=CATALOG, compatibility=COMPATIBILITY,
            network_state=self.record,
            effect_evidence=self.predictor.effect_evidence(
                CATALOG.axes(), CATALOG.baselines(self.state.applied_configuration())),
            observations=())

    def test_the_best_result_reaches_this_arm_with_its_levels(self):
        """The online selectors get ``observed_best``; so must this arm.

        The drop of 2026-09-17 (Runtime alignment 2 and 4) requires the best
        observed result to reach the basic monolith too, and -- because this
        arm is never shown our ``T`` -- to carry the requirement levels beside
        the opaque target id.  Before the fix ``_basic_inputs`` passed neither
        field, so the arm chose with strictly less information than the two it
        is compared against.
        """
        # 목표 id 는 **없다** -- 이 팔은 우리 T 를 보지 않는다.  뜻을 나르는 것은 levels 다.
        best = {"cost": 2.0, "kpis": {"dlGoodputMbps@ue1": 7.4},
                "levels": {"I1g.r1": 1}, "deadlineLevels": {"I2d.r1": 0}}
        gaps = {"I1g.r1": {"shortfall": 1.6, "unit": "Mbps"}}
        payload = replace(self.inputs, observed_best=best, kpi_gaps=gaps).payload()
        self.assertEqual(payload["input.observed_best"], best)
        self.assertEqual(payload["input.kpi_gaps"], gaps)
        self.assertIn("levels", payload["input.observed_best"],
                      "an opaque target id is not usable by an arm without T")
        self.assertNotIn("targetId", payload["input.observed_best"],
                         "our target ids must not reach this arm")

    def test_no_best_result_is_null_not_an_empty_object(self):
        """"no measurement" and "a best of nothing" must not look the same."""
        payload = self.inputs.payload()
        self.assertIsNone(payload["input.observed_best"])
        self.assertIsNone(payload["input.kpi_gaps"])

    def agents(self, answers):
        resolver, models = ScriptedResolver.by_role(
            {"monolith": answers}, method=METHOD_BASIC_MONOLITH)
        return RoleAgents(models=models, resolver=resolver,
                          predictor=self.predictor), resolver

    def test_its_prompt_carries_no_t_no_c_and_no_grid(self):
        """Our prepared ``T``, ``C`` and grid stay out of this arm's prompt.

        ``observed_best`` used to be on this list too.  The owner's drop of
        2026-09-17 (Runtime alignment 2 and 4) reverses that one item on
        purpose: the best observed result **must** reach this arm, carrying the
        requirement levels, or it decides with strictly less information than
        the two arms it is compared against.  What stays forbidden is the
        prepared machinery -- the contract, the candidate set, the grid and the
        per-candidate predictions -- because those are the structure under test.
        """
        agents, resolver = self.agents([{
            "instructions": [{"functionId": "ue-dl-prb-cap", "scope": "ue@132",
                              "policy": {"maxDlPrbs": "12"}}],
            "rationale": "cap the map data"}])
        agents.basic_monolith_decide(self.inputs)
        prompt = resolver.prompts[0]
        for forbidden in ("target_contract", "control_candidates",
                          "observedGrid", "predictedTarget"):
            self.assertNotIn(forbidden, prompt)
        self.assertIn("input.observed_best", prompt,
                      "the drop requires the best result to reach this arm")
        self.assertEqual(budget_terms_in(prompt), [])

    def test_it_is_asked_for_policy_and_a_rationale_and_nothing_else(self):
        """The requirement restatement is gone from the schema and the prompt.

        Every intent's reqId/kpi/op/value/unit had to be copied back, and no
        executor read ever used the copy: the arm is judged against ``T0``
        whatever it says it aimed at.  So the originals come from the inputs,
        on the accepted path exactly as on the deterministic one.
        """
        from assurance.coordination.agents import _BASIC_SCHEMA
        self.assertEqual(sorted(_BASIC_SCHEMA), ["instructions", "rationale"])
        self.assertNotIn("requirements it aims to satisfy",
                         SYSTEM_PROMPTS["basic-monolith"])

    def test_its_instructions_translate_exactly_like_a_c_candidate(self):
        agents, _resolver = self.agents([{
            "instructions": [{"functionId": "ue-dl-prb-cap", "scope": "ue@132",
                              "policy": {"maxDlPrbs": "12"}}],
            "rationale": "cap the map data"}])
        decision, record = agents.basic_monolith_decide(self.inputs)
        self.assertTrue(record.accepted)
        self.assertEqual(decision.configuration["dlPrbCap@132"], "12")
        # The executor's own originals, not a restatement it asked the model for.
        self.assertEqual(decision.requirements, {"I1.r1": 3.0, "I2.r1": 1.5})
        self.assertEqual({"I1.r1", "I2.r1"},
                         {row["reqId"] for row in decision.requirement_records})

    def test_an_instruction_outside_the_catalog_is_refused(self):
        agents, _resolver = self.agents([{
            "instructions": [{"functionId": "slice-quota", "scope": "ue@132",
                              "policy": {"quota": "40"}}], "rationale": "..."}])
        refused(agents.basic_monolith_decide, self.inputs)   # no deterministic answer

    def test_the_fallback_aims_at_the_original_requirements(self):
        agents = RoleAgents(models=RoleModels(method=METHOD_BASIC_MONOLITH),
                            predictor=self.predictor)
        decision, record = agents.basic_monolith_decide(self.inputs)
        self.assertFalse(record.accepted)
        self.assertEqual(decision.requirements, {"I1.r1": 3.0, "I2.r1": 1.5})


class TheRecordSaysWhoActuallyAnswered(unittest.TestCase):
    """``calls[].servedModel``: the provider's own id, or null for unknown.

    ``model`` is the label the executor asked for, and the proxy this lab
    talks to advertises obfuscated ids -- so a record that only kept the
    request cannot tell a real three-agent run from a fallback.
    """

    def record(self, generations):
        from assurance.coordination.agents import CallRecord
        return CallRecord(role="target", model="claude-sonnet", phase=PHASE_FORMATION,
                          generations=list(generations))

    def test_the_accepted_generations_reported_model_is_carried(self):
        record = self.record([
            {"responseSuccess": False, "responseModel": "refused-one"},
            {"responseSuccess": True, "responseModel": "gpt-5.6-luna"}])
        self.assertEqual("gpt-5.6-luna", record.served_model)
        self.assertEqual("gpt-5.6-luna", record.to_record()["servedModel"])
        # Never confused with the label that was requested.
        self.assertEqual("claude-sonnet", record.to_record()["model"])

    def test_a_provider_that_reported_nothing_stays_null(self):
        for generations in ([], [{"responseSuccess": True, "responseModel": None}],
                            [{"responseSuccess": True, "responseModel": ""}]):
            with self.subTest(generations=generations):
                record = self.record(generations)
                self.assertIsNone(record.served_model)
                # The key is written anyway: null is "unknown", not "unlooked".
                self.assertIn("servedModel", record.to_record())
                self.assertIsNone(record.to_record()["servedModel"])


class TheOperatorsAssignment(unittest.TestCase):
    """Which model carries which role, and the Cockpit's handoff file."""

    def test_the_roles_are_target_control_trajectory_and_monolith(self):
        models = RoleModels.from_mapping({"target": "claude-sonnet",
                                          "trajectory": "litellm:qwen3-32b"})
        self.assertEqual(models.model_for("target"), "claude-sonnet")
        self.assertIsNone(models.model_for("control"))
        self.assertTrue(models.any_llm)

    def test_the_word_deterministic_means_no_model_for_that_role(self):
        self.assertIsNone(RoleModels(target=DETERMINISTIC).model_for("target"))

    def test_the_handoff_file_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent-role-models.json"
            save_role_models_file(path, RoleModels(target="claude-sonnet",
                                                   method=METHOD_INTERNAL_MONOLITH))
            again = load_role_models_file(path)
            self.assertEqual(again.target, "claude-sonnet")
            self.assertEqual(again.method, METHOD_INTERNAL_MONOLITH)

    def test_a_legacy_file_reads_as_all_deterministic_rather_than_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent-role-models.json"
            path.write_text(json.dumps({"schemaVersion": "agent-role-models/1.0.0",
                                        "roleModels": {"intent": "claude-sonnet",
                                                       "action": "gpt-4o"}}),
                            encoding="utf-8")
            self.assertFalse(load_role_models_file(path).any_llm)

    def test_a_missing_file_is_all_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(
                load_role_models_file(Path(directory) / "nothing.json").any_llm)


class TheTokenAccountingIsAuditablePerGeneration(unittest.TestCase):
    """``generations[].responseBytes`` and ``refusedBecause``: per-attempt facts.

    ``record.output_tokens`` accumulates across attempts while ``record.raw``
    keeps only the last attempt's text, so a persisted 945-byte body was being
    compared against *both* attempts' tokens (5,988+6,009 in and 1,138+1,057
    out, recorded as 11,997/2,195).  De-aggregating is still not enough -- the
    accepted generation alone was 1,057 output tokens against 945 bytes, and a
    BPE token covers at least one byte -- so what these two fields buy is the
    ability to *state* that comparison, never to close it.  No total moves here
    and the residual stays unattributable.
    """

    BAD = '{"ok": false, "why": "refused by the validator"}'
    GOOD = '{"ok": true}'

    def agents(self, answers):
        resolver, models = ScriptedResolver.by_role({"target": list(answers)})
        return RoleAgents(models=models, resolver=resolver)

    def decide(self, agents):
        def accept(payload, record):
            if not payload.get("ok"):
                raise AnswerRefused("the validator refused this answer")
            return "accepted"

        try:
            return agents._decide("target", PHASE_FORMATION, "system", {}, {},
                                  accept, lambda: "deterministic-result")
        except DecisionUnavailable as exc:
            return None, exc.record

    def test_a_repaired_call_keeps_each_attempts_bytes_and_its_refusal(self):
        value, record = self.decide(self.agents([self.BAD, self.GOOD]))
        self.assertEqual("accepted", value)
        self.assertEqual(1, record.repair_retries)
        first, second = record.generations
        # Distinct bodies, each measured as it was received.
        self.assertEqual(len(self.BAD.encode("utf-8")), first["responseBytes"])
        self.assertEqual(len(self.GOOD.encode("utf-8")), second["responseBytes"])
        self.assertNotEqual(first["responseBytes"], second["responseBytes"])
        # Why attempt 1 was thrown away -- which used to survive only when
        # *every* attempt failed, so a repaired call lost it entirely.
        self.assertEqual("the validator refused this answer",
                         first["refusedBecause"])
        self.assertIsNone(second["refusedBecause"])
        # The call-level reason stays untouched: the call as a whole succeeded.
        self.assertIsNone(record.fallback_reason)

    def test_the_accumulated_totals_are_exactly_what_they_were(self):
        """Pinned: this change is additive and must not move a single total."""
        _, record = self.decide(self.agents([self.BAD, self.GOOD]))
        # Two scripted responses at the dataclass defaults, summed as before.
        self.assertEqual(200, record.input_tokens)
        self.assertEqual(80, record.output_tokens)
        self.assertEqual(24.0, record.latency_ms)
        # ``raw`` still means "the last body", overwritten by the repair.
        self.assertEqual(self.GOOD, record.raw)

    def test_one_successful_call_records_one_generation_refused_by_nothing(self):
        _, record = self.decide(self.agents([self.GOOD]))
        generation, = record.generations
        self.assertTrue(generation["responseSuccess"])
        self.assertEqual(len(self.GOOD.encode("utf-8")),
                         generation["responseBytes"])
        # Written, not omitted: a key that appeared only on failure would read
        # as "never refused OR never looked" -- the same ambiguity a missing
        # ``alternatives`` field created.
        self.assertIn("refusedBecause", generation)
        self.assertIsNone(generation["refusedBecause"])
        self.assertIn("refusedBecause", record.to_record()["generations"][0])

    def test_the_bytes_are_the_received_text_not_the_parsed_object(self):
        """What makes visible JSON tokenization comparable to a backend total.

        A re-serialization would answer a different question: the fence, the
        whitespace and anything the model wrapped its object in were tokenized
        by the provider and are all absent from ``json.dumps(parsed)``.
        """
        fenced = '```json\n{\n  "ok": true,\n  "note": "응답"\n}\n```'
        _, record = self.decide(self.agents([fenced]))
        generation, = record.generations
        self.assertEqual(len(fenced.encode("utf-8")), generation["responseBytes"])
        # Bytes, not characters: the multibyte note costs more than its length.
        self.assertGreater(generation["responseBytes"], len(fenced))
        self.assertGreater(generation["responseBytes"],
                           len(json.dumps({"ok": True, "note": "응답"})))

    def test_a_backend_that_never_answered_records_no_bytes_and_a_reason(self):
        _, record = self.decide(self.agents([None, None]))
        self.assertFalse(record.accepted)
        for generation in record.generations:
            self.assertEqual(0, generation["responseBytes"])
            self.assertIn("scripted failure", generation["refusedBecause"])

    # -- the settings side: what was asked for vs what was sent ---------------
    #
    # ``record.options`` is the *requested* budget.  The Claude path gates the
    # thinking budget on a regex against the model id, which the proxy's
    # obfuscated ids do not match, so a record showing a requested budget could
    # not tell a reader that none was ever sent.

    @staticmethod
    def answered(*, options, content='{"ok": true}'):
        """One backend response as ``_generate`` reads it -- all by ``getattr``.

        Deliberately a namespace rather than ``LLMResponse``: nothing in this
        file should depend on that dataclass's current field defaults.
        """
        return NS(success=True, content=content, parsed_json=None,
                  latency_ms=5.0, input_tokens=2, output_tokens=3, usage=None,
                  error=None, requested_model=None, response_model="served-test",
                  options=options)

    def backend_agents(self, *responses):
        backend = NS(model="proxy-route", generate=Mock(side_effect=list(responses)))
        return RoleAgents(models=RoleModels(target="claude-sonnet"),
                          resolver=lambda _: backend)

    def test_a_budget_the_gate_refused_is_requested_but_never_sent(self):
        """The demonstrated failure: requested 8,000, sent nothing."""
        # What the Claude path's ``effective`` holds when the id does not match.
        agents = self.backend_agents(self.answered(options={"maxTokens": 3000}))
        _, record = self.decide(agents)
        generation, = record.generations
        # Asked for -- unchanged, still on the call record where it always was.
        self.assertEqual(8000, record.options["thinkingBudgetTokens"])
        # Sent -- and the absence is now visible rather than assumed.
        self.assertEqual({"maxTokens": 3000}, generation["sentOptions"])
        self.assertNotIn("thinkingBudgetTokens", generation["sentOptions"])

    def test_a_budget_that_was_enabled_shows_up_on_both_sides(self):
        agents = self.backend_agents(
            self.answered(options={"maxTokens": 8001, "thinkingBudgetTokens": 8000}))
        _, record = self.decide(agents)
        generation, = record.generations
        self.assertEqual(8000, record.options["thinkingBudgetTokens"])
        self.assertEqual(8000, generation["sentOptions"]["thinkingBudgetTokens"])

    def test_a_backend_that_reported_no_settings_records_null(self):
        """``None`` is "the backend reported nothing", never a reconstruction."""
        # A response with the attribute explicitly empty...
        _, record = self.decide(self.backend_agents(self.answered(options=None)))
        self.assertIsNone(record.generations[0]["sentOptions"])
        # ...and one carrying no such attribute at all (``ScriptedResponse``).
        _, scripted = self.decide(self.agents([self.GOOD]))
        generation, = scripted.generations
        self.assertIn("sentOptions", generation)
        self.assertIsNone(generation["sentOptions"])
        self.assertIn("sentOptions", scripted.to_record()["generations"][0])

    def test_the_sent_settings_are_a_copy_and_move_no_total(self):
        # 3000 is deliberately *not* the role default any more.  It used to be,
        # so "what the backend reported it sent" and "what this role asks for"
        # were the same number and this test could not tell them apart -- it
        # would have passed just as happily if ``record.options`` had been
        # filled from ``sentOptions``.  They differ now, so it can.
        sent = {"maxTokens": 3000}
        _, record = self.decide(self.backend_agents(self.answered(options=sent)))
        generation, = record.generations
        # Copied, so a later mutation of either side cannot rewrite the record.
        generation["sentOptions"]["maxTokens"] = 999
        self.assertEqual({"maxTokens": 3000}, sent)
        # Still additive: the requested options and the totals are untouched.
        self.assertEqual(4000, record.options["maxTokens"])
        self.assertEqual("default", record.options["source"])
        self.assertEqual(2, record.input_tokens)
        self.assertEqual(3, record.output_tokens)
        self.assertEqual(5.0, record.latency_ms)


class AnUnreportedTokenCountIsNotZero(unittest.TestCase):
    """``generations[].inputTokens``: what the provider said, or ``null``.

    The old ``int(getattr(response, "input_tokens", 0) or 0)`` gave the same
    answer -- 0 -- to three different questions: "the provider measured zero",
    "the provider reported nothing" and "there is no such field".  Only the
    first is a measurement; the other two are unknowns, and a cost record that
    prints them as 0 is inventing a number on the provider's behalf.  The
    reader is :func:`assurance.advisors.strategies.transport._reported_count`,
    reused here rather than reimplemented.
    """

    GOOD = '{"ok": true}'

    def backend_agents(self, *responses):
        backend = NS(model="proxy-route", generate=Mock(side_effect=list(responses)))
        return RoleAgents(models=RoleModels(target="claude-sonnet"),
                          resolver=lambda _: backend)

    def answered(self, **counts):
        """One successful response carrying whatever counts the test scripts."""
        return NS(success=True, content=self.GOOD, parsed_json=None,
                  latency_ms=5.0, usage=None, error=None, requested_model=None,
                  response_model="served-test", options=None, **counts)

    def decide(self, agents):
        def accept(payload, record):
            if not payload.get("ok"):
                raise AnswerRefused("the validator refused this answer")
            return "accepted"

        try:
            return agents._decide("target", PHASE_FORMATION, "system", {}, {},
                                  accept, lambda: "deterministic-result")
        except DecisionUnavailable as exc:
            return None, exc.record

    def test_counts_the_provider_reported_are_recorded_as_given(self):
        agents = self.backend_agents(self.answered(input_tokens=31, output_tokens=7))
        _, record = self.decide(agents)
        generation, = record.generations
        self.assertEqual(31, generation["inputTokens"])
        self.assertEqual(7, generation["outputTokens"])
        self.assertEqual(31, record.input_tokens)
        self.assertEqual(7, record.output_tokens)
        self.assertTrue(record.to_record()["tokensComplete"])

    def test_a_provider_that_reported_nothing_records_null_not_zero(self):
        # Attributes explicitly empty, and -- the ``ScriptedResponse`` case --
        # a response object carrying no such attribute at all.
        for label, response in (
                ("explicit None", self.answered(input_tokens=None, output_tokens=None)),
                ("attribute absent", self.answered())):
            with self.subTest(response=label):
                _, record = self.decide(self.backend_agents(response))
                generation, = record.generations
                self.assertIsNone(generation["inputTokens"])
                self.assertIsNone(generation["outputTokens"])
                # Written, never omitted: an absent key would read as "nobody
                # looked" rather than "the provider reported nothing".
                self.assertIn("inputTokens", generation)
                # And it must survive serialization as null, not as 0.
                written = record.to_record()["generations"][0]
                self.assertIn('"inputTokens": null', json.dumps(written, indent=0))

    def test_a_reported_zero_is_kept_and_is_distinguishable_from_unknown(self):
        """The one case that must NOT become null: a measured zero."""
        _, measured = self.decide(
            self.backend_agents(self.answered(input_tokens=0, output_tokens=0)))
        _, unknown = self.decide(self.backend_agents(self.answered()))
        self.assertEqual(0, measured.generations[0]["inputTokens"])
        self.assertIsNone(unknown.generations[0]["inputTokens"])
        # Both totals read 0; only ``tokensComplete`` tells them apart.
        self.assertEqual(0, measured.input_tokens)
        self.assertEqual(0, unknown.input_tokens)
        self.assertTrue(measured.to_record()["tokensComplete"])
        self.assertFalse(unknown.to_record()["tokensComplete"])

    def test_an_unknown_sums_as_zero_and_says_so_on_the_record(self):
        """The documented choice: totals stay a usable LOWER BOUND.

        Refusing to total at all would lose a figure every existing reader
        does integer arithmetic on (``tools/liveconsole/agent.py``'s
        ``resource_cost`` sums these directly), so an unknown adds 0 -- and
        ``tokensComplete`` is what stops that understatement being read as
        the truth.
        """
        agents = self.backend_agents(
            self.answered(input_tokens=10, output_tokens=4),   # refused below
            self.answered(),                                   # reported nothing
        )

        calls = {"n": 0}

        def accept(payload, record):
            calls["n"] += 1
            if calls["n"] == 1:
                raise AnswerRefused("the validator refused this answer")
            return "accepted"

        _, record = agents._decide("target", PHASE_FORMATION, "system", {}, {},
                                   accept, lambda: "deterministic-result")
        self.assertEqual(1, record.repair_retries)
        # The known attempt still counts; the unknown one adds nothing...
        self.assertEqual(10, record.input_tokens)
        self.assertEqual(4, record.output_tokens)
        # ...and the total is marked incomplete so it is not read as exact.
        self.assertFalse(record.tokens_complete)
        self.assertFalse(record.to_record()["tokensComplete"])
        # Per-generation, the two attempts stay individually legible.
        self.assertEqual(10, record.generations[0]["inputTokens"])
        self.assertIsNone(record.generations[1]["inputTokens"])

    def test_a_call_whose_backend_raised_never_claims_a_complete_total(self):
        """No response, so no counts -- and the 0 total must say it is partial."""
        backend = NS(model="proxy-route",
                     generate=Mock(side_effect=RuntimeError("transport died")))
        agents = RoleAgents(models=RoleModels(target="claude-sonnet"),
                            resolver=lambda _: backend)
        value, record = self.decide(agents)
        self.assertIsNone(value)                 # no deterministic stand-in
        self.assertEqual(0, record.input_tokens)
        self.assertFalse(record.to_record()["tokensComplete"])
        for generation in record.generations:
            self.assertIsNone(generation["inputTokens"])
            self.assertIsNone(generation["outputTokens"])

    def test_a_generation_whose_backend_raised_still_carries_a_usage_key(self):
        """Every per-generation field is written unconditionally, ``None``
        included -- the law the seed dict states about ``refusedBecause``.

        ``usage`` was assigned *after* the try/except, so it was **absent**
        rather than null on exactly the attempts that also have no counts: the
        one case where a reader most needs to tell "the provider reported no
        envelope" from "this generation never got as far as one".
        """
        backend = NS(model="proxy-route",
                     generate=Mock(side_effect=RuntimeError("transport died")))
        agents = RoleAgents(models=RoleModels(target="claude-sonnet"),
                            resolver=lambda _: backend)
        _value, record = self.decide(agents)
        self.assertTrue(record.generations)
        for generation in record.generations:
            self.assertIn("usage", generation)
            self.assertIsNone(generation["usage"])

    def test_the_scripted_defaults_still_report_and_stay_complete(self):
        """Pinned: the hermetic backend reports ints, so nothing here moves."""
        resolver, models = ScriptedResolver.by_role({"target": [self.GOOD]})
        _, record = self.decide(RoleAgents(models=models, resolver=resolver))
        self.assertEqual(100, record.input_tokens)
        self.assertEqual(40, record.output_tokens)
        self.assertTrue(record.tokens_complete)


class AMockProducedCountIsNotProviderUsage(unittest.TestCase):
    """``mock:agent`` must not leave a cost row that reads as measured.

    The stand-in used to estimate ``len(text) // 4`` and return plain ints, so
    this layer -- which only asks "did the provider report a number?" -- wrote
    them as reported and stamped ``tokensComplete: true``.  The one tell was an
    absent ``usage`` envelope, which is nested under ``generations[]`` and is
    not on the call row a per-stage cost table is built from.
    """

    def record_from_the_mock(self):
        from decision.llm_backend import MockAgentBackend
        agents = RoleAgents(models=RoleModels(target="mock:agent"),
                            resolver=lambda _: MockAgentBackend())
        intent_list = intents()
        return agents.form_targets(TargetInputs(
            intents=tuple(intent_list),
            authorization=Authorization.from_intents(intent_list)))[1]

    def test_the_call_row_says_its_totals_are_not_provider_usage(self):
        record = self.record_from_the_mock()
        self.assertFalse(record.tokens_complete)
        self.assertFalse(record.to_record()["tokensComplete"])

    def test_no_generation_carries_an_invented_count(self):
        record = self.record_from_the_mock()
        self.assertTrue(record.generations)
        for generation in record.generations:
            self.assertIsNone(generation["inputTokens"])
            self.assertIsNone(generation["outputTokens"])
            # No envelope either: there is no provider here to have one.
            self.assertIsNone(generation["usage"])


class TheSchemaNoLongerAsksForTheDefunctCostRule(unittest.TestCase):
    """``ranking.costRule`` is not asked for: it orders nothing.

    Ranking is :func:`preference_key` over the owners' normalized concessions.
    ``cost_of`` is still computed and recorded, and explicitly does not order,
    so asking the model to restate "the preference rule named in
    input.authorization" bought an echo of a defunct quantity that read as
    though the answer had authority over the ordering.  ``tieBreak`` stays:
    both prompts still say to represent ``T`` through levels, constraints and
    ranking, and the object has to remain for that sentence to be true.
    """

    def schemas(self):
        from assurance.coordination.agents import (_MONOLITH_FORM_SCHEMA,
                                                   _TARGET_SCHEMA)
        return _TARGET_SCHEMA, _MONOLITH_FORM_SCHEMA

    def test_neither_formation_schema_asks_the_model_to_restate_it(self):
        # 2026-09-19: neither the cost rule nor the tie-break is the model's to
        # write -- the order is the owner's, so the whole ranking field is gone.
        target, monolith = self.schemas()
        self.assertNotIn("ranking", target)
        self.assertNotIn("ranking", monolith)

    def test_an_answer_without_it_still_validates_on_the_signed_rule(self):
        """``from_compact`` falls back, so the recorded contract is unchanged."""
        intent_list = intents()
        authorization = Authorization.from_intents(intent_list)
        answer = dict(VALID_T, ranking={"tieBreak": "lexicographic(D_max, D_mean)"})
        resolver, models = ScriptedResolver.by_role({"target": [answer]})
        agents = RoleAgents(models=models, resolver=resolver)
        contract, record = agents.form_targets(TargetInputs(
            intents=tuple(intent_list), authorization=authorization))
        self.assertTrue(record.accepted)
        self.assertEqual(authorization.preference.cost_rule,
                         contract.preference.cost_rule)


if __name__ == "__main__":
    unittest.main()


class TheFinalConfigurationIsCheckedTheSameOnEveryArm(unittest.TestCase):
    """핸드오프 2026-09-18 §3: 같은 뜻의 설정은 팔별 표기와 무관하게 같은 판정을 받는다.

    BM 판 20260917T184817 시행 4 는 ue2 에 cap 18 과 PF 4.0 을 함께 적용했다 -- 함수
    이름·범위 표기만 보는 ``refusals()`` 를 통과했기 때문이다.  여기서는 그 검사가 아무것도
    못 잡는 상황(``mutually_exclusive`` 비어 있음)을 일부러 만들고, 변환된 최종 설정 검사
    (``exclusive_axes``)만으로 BM 답·BM 대체 경로·준비된 C 가 모두 막히는지 본다.
    """

    CAT = FunctionCatalog.from_record([
        {"functionId": "ue-dl-prb-cap", "xapp": "our_rc_xapp", "actionId": "102",
         "scopes": ["ue@131", "ue@132"],
         "policyFields": {"maxDlPrbs": {"values": [24, 12, 6], "unit": "PRB",
                                        "baseline": 24}},
         "axis": "dlPrbCap@<ue>"},
        {"functionId": "ue-sched-priority", "xapp": "our_rc_xapp", "actionId": "103",
         "scopes": ["ue@131", "ue@132"],
         # 준비 데이터(network_state)의 적용값이 8 이라 기준값은 8 이다.
         "policyFields": {"pfWeight": {"values": [8, 4, 1], "unit": "weight",
                                       "baseline": 8}},
         "axis": "pfWeight@<ue>"},
    ])
    RULES = CompatibilityRules(exclusive_axes=(("dlPrbCap", "pfWeight"),))

    def setUp(self):
        self.state, self.record = network_state()
        predictor = JointEffectPredictor(self.state)
        self.predictor = predictor
        self.inputs = BasicInputs(
            intents=tuple(intents()),
            authorization=Authorization.from_intents(intents()),
            function_catalog=self.CAT, compatibility=self.RULES,
            network_state=self.record,
            effect_evidence=predictor.effect_evidence(
                self.CAT.axes(), self.CAT.baselines(self.state.applied_configuration())),
            observations=())

    def answer(self, *rows):
        resolver, models = ScriptedResolver.by_role(
            {"monolith": [{"instructions": list(rows), "rationale": "x"}]},
            method=METHOD_BASIC_MONOLITH)
        agents = RoleAgents(models=models, resolver=resolver, predictor=self.predictor)
        return agents.basic_monolith_decide(self.inputs)

    CAP_132 = {"functionId": "ue-dl-prb-cap", "scope": "ue@132", "policy": {"maxDlPrbs": "12"}}
    PF_132 = {"functionId": "ue-sched-priority", "scope": "ue@132", "policy": {"pfWeight": "4"}}
    PF_131 = {"functionId": "ue-sched-priority", "scope": "ue@131", "policy": {"pfWeight": "4"}}
    PF_132_BASE = {"functionId": "ue-sched-priority", "scope": "ue@132", "policy": {"pfWeight": "8"}}

    def test_the_rule_reads_active_values_on_one_scope(self):
        self.assertTrue(self.RULES.configuration_refusals(
            {"dlPrbCap@132": "12", "pfWeight@132": "4"},
            {"dlPrbCap@132": "24", "pfWeight@132": "8"}))
        # 다른 UE 는 합법이다 -- 규칙은 같은 범위 안에서만이다.
        self.assertEqual([], self.RULES.configuration_refusals(
            {"dlPrbCap@132": "12", "pfWeight@131": "4"},
            {"dlPrbCap@132": "24", "pfWeight@131": "1"}))
        # 기준값이 들어 있다고 충돌로 보지 않는다 (§3.2).
        self.assertEqual([], self.RULES.configuration_refusals(
            {"dlPrbCap@132": "12", "pfWeight@132": "1"},
            {"dlPrbCap@132": "24", "pfWeight@132": "1"}))

    def test_a_settled_value_is_not_the_basic_monolith_baseline(self):
        # 2026-09-19 M13: pfWeight@132 settled at 4, the next answer set only the
        # cap, and the "baseline" handed to translation and to the exclusion was
        # the applied 4 -- so 4 stayed and cap+PF went through together.
        record = copy.deepcopy(self.record)
        record["appliedConfiguration"]["pfWeight@132"] = "4"
        self.inputs = replace(self.inputs, network_state=record)
        self.assertEqual("8", self.inputs.baselines["pfWeight@132"])
        decision, _record = self.answer(self.CAP_132)
        self.assertEqual("8", decision.configuration["pfWeight@132"])

    def test_the_basic_monolith_answer_is_refused_on_one_ue(self):
        with self.assertRaises(DecisionUnavailable):
            self.answer(self.CAP_132, self.PF_132)

    def test_the_basic_monolith_answer_on_two_ues_is_accepted(self):
        decision, record = self.answer(self.CAP_132, self.PF_131)
        self.assertTrue(record.accepted)
        self.assertEqual(decision.configuration["dlPrbCap@132"], "12")

    def test_a_baseline_weight_beside_a_cap_is_not_a_clash(self):
        _decision, record = self.answer(self.CAP_132, self.PF_132_BASE)
        self.assertTrue(record.accepted)

    def test_a_prepared_candidate_gets_the_same_verdict_in_either_format(self):
        base = self.CAT.baselines(self.state.applied_configuration())
        by_functions = ControlCandidate(control_id="CF", functions=(
            FunctionSelection.from_record(self.CAP_132),
            FunctionSelection.from_record(self.PF_132)))
        by_configuration = ControlCandidate(control_id="CS", configuration={
            **base, "dlPrbCap@132": "12", "pfWeight@132": "4"})
        for candidate in (by_functions, by_configuration):
            kept, dropped = validate_control_candidates(
                ControlCandidates(candidates=(candidate,), catalog=self.CAT),
                self.CAT.axes(), base, catalog=self.CAT if candidate.functions else None,
                compatibility=self.RULES)
            self.assertEqual(["C0"], [item.control_id for item in kept.candidates],
                             candidate.control_id)
            self.assertIn("may not both be active on 132", " ".join(dropped))


class TZeroIsBuiltByTheCode(unittest.TestCase):
    """핸드오프 2026-09-18 §4.1: T0 본문은 모델이 되받아 적지 않고 코드가 원본으로 만든다."""

    def test_neither_formation_schema_asks_for_t0(self):
        from assurance.coordination.agents import _MONOLITH_FORM_SCHEMA, _TARGET_SCHEMA
        self.assertNotIn("t0", _TARGET_SCHEMA)
        self.assertNotIn("t0", _MONOLITH_FORM_SCHEMA)

    def form(self, answer):
        intent_list = intents()
        authorization = Authorization.from_intents(intent_list)
        resolver, models = ScriptedResolver.by_role({"target": [answer]})
        agents = RoleAgents(models=models, resolver=resolver)
        return authorization, agents.form_targets(TargetInputs(
            intents=tuple(intent_list), authorization=authorization))

    def test_an_answer_without_t0_gets_the_originals(self):
        answer = {k: v for k, v in VALID_T.items() if k != "t0"}
        authorization, (contract, record) = self.form(answer)
        self.assertTrue(record.accepted)
        originals = {req: entry.original for req, entry in authorization.requirements.items()}
        self.assertEqual({k: float(v) for k, v in originals.items()},
                         {k: float(v) for k, v in contract.t0.requirements.items()})

    def test_a_t0_the_model_redefines_is_refused(self):
        moved = dict(VALID_T, t0={"targetId": "T0", "requirements": {"I1.r1": 1.0, "I2.r1": 1.5}})
        with self.assertRaises(DecisionUnavailable):
            self.form(moved)


class AnAmbiguousAnswerIsRefusedNotSilentlyCollapsed(unittest.TestCase):
    """중복 키·비유한 상수는 재질문 대상이다 (2026-09-22).

    `json.loads` 는 중복 키의 **마지막** 값만 남기므로, 모델이 한 필드를 두 번
    내면 원문을 읽은 사람과 코드가 서로 다른 제안을 본다.  `NaN` 은 float() 이
    되어 축 경계로 조용히 clip 된다.  `coordinator/schema.py` 가 예전부터 이 두
    거절을 하는데 에이전트 경로에만 없었다.
    """

    def test_a_duplicated_field_is_refused(self):
        from assurance.coordination.agents import _extract_object, AnswerRefused
        with self.assertRaises(AnswerRefused):
            _extract_object('{"value": 8.0, "value": 5.6}')

    def test_a_non_finite_constant_is_refused(self):
        from assurance.coordination.agents import _extract_object, AnswerRefused
        with self.assertRaises(AnswerRefused):
            _extract_object('{"value": NaN}')

    def test_an_ordinary_answer_still_parses(self):
        from assurance.coordination.agents import _extract_object
        self.assertEqual({"value": 8.0}, _extract_object('```json\n{"value": 8.0}\n```'))



