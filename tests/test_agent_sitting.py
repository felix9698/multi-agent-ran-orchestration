"""The Agent sitting, hermetically: T rows, C columns, one joint case, one episode.

Composed by ``tools.liveconsole.agent.build_hardware_free_agent_sitting``, which
is :func:`~tools.liveconsole.agent.build_agent_sitting` -- the root
``main.py --live --agent`` uses -- over the seeded RAN emulator of
:mod:`tools.hfconsole.agent_env`.  The Kernel, the Write Gateway, the frozen
catalog, the trial state machine and the roll-back are the real ones; the
radio, the R1 transport, the clock and the models are injected.  No lab file,
no socket, no LLM API: every model answer here is scripted by
:class:`assurance.coordination.ScriptedResolver`.

Contract v2 added four things this file has to prove end to end: the **intake**
asks the operator before any model call and refuses to start rather than guess
at a bound; a control candidate is the **functions** used together, translated
onto the axes under the sitting's unselected-function rule; the **predictor**
is separate code from the emulator whose table every method is handed; and an
observation has a **window and a validity**, so a thin window is ``UNKNOWN``
rather than an average and an answer that arrives after its numbers expired is
re-asked once on fresh ones before the deterministic rule takes over.
"""

from __future__ import annotations

import hashlib
import json

from assurance.coordination import DecisionUnavailable
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from assurance.coordination import (
    METHOD_BASIC_MONOLITH, METHOD_DETERMINISTIC, METHOD_INTERNAL_MONOLITH,
    METHOD_THREE_AGENT, METHOD_THREE_AGENT_COVERAGE, Authorization, BasicInputs,
    GenerationOptions, Preference, RoleAgents, RoleModels, ScriptedResolver,
    TargetInputs, budget_terms_in,
)
from assurance.coordination.agents import (
    ControlInputs, MonolithFormInputs, ScriptedResponse,
)
from assurance.coordination.tc import ControlCandidate, catalog_product_controls, expand_targets
from assurance.objectives.joint import (
    DEFAULT_ATTENUATION_LADDER, DEFAULT_CAP_LADDER, DEFAULT_MCS_LADDER,
    DEFAULT_PF_LADDER, DEFAULT_SLICE_QUOTA_LADDER,
)
from tools.hfconsole.agent_env import (
    EmulatedRan, HermeticDeployment, three_ue_topology,
)
from tools.liveconsole import LiveConsoleError, load_live_deployment
from tools.liveconsole import agent as agent_module
from tools.liveconsole.agent import (
    BOUNDARY_KINDS, DEFAULT_AXIS_KINDS, EPISODE_BOUNDARY, INITIAL_TRIAL_INDEX,
    MAX_CHANGED_ENTRIES, NON_TRIAL_KINDS, TIMING_COLD_START, TIMING_PREPARED,
    AgentRequest, ClarificationNeeded, _axis_specs, _compatibility_rules,
    _EmulatedIdentity, _function_catalog, _preference_for, _refuse_uncarried_axes,
    build_hardware_free_agent_sitting, parse_agent_intents, write_agent_evidence,
)
from tools.liveconsole.build import live_capable_families
from tools.liveconsole.kpi_observer import TunRateObserver, resolve_ue_hosts

REPO_ROOT = Path(__file__).resolve().parents[1]
HOME_NCI, TARGET_NCI = "12345678", "87654321"

#: Two owners on one cell: the pair only both fit when one of them is moved.
I1 = "I1: UE ueId=131 needs at least 3.0 Mbps downlink, relaxable to 2.0"
I2 = "I2: UE ueId=132 needs at least 1.5 Mbps downlink, relaxable to 1.0"

#: What a valid Target answer looks like for that pair: the compact form of
#: contract v2 section 2.3 -- T0 exactly, then the authorized steps and bound
#: per requirement, which the executor expands into the whole product.
T_ANSWER = {
    # No ``t0``: the code builds T0 from the intents (handoff 2026-09-18 section
    # 4.1).  An echoed T0 that disagreed with a sentence used to be replaced by
    # the deterministic T; with no fallback (2026-09-19) it would refuse the
    # sitting instead, so the fixture no longer echoes one.
    "levels": {"I1.r1": {"steps": 2, "bound": 2.0},
               "I2.r1": {"steps": 1, "bound": 1.0}},
    "constraints": ["I1 never below 2.0 Mbps", "I2 never below 1.0 Mbps"],
    # The agent selects which authorized directions to carry. This pair's whole
    # domain is 3 x 2 = 6, so selecting all five alternatives besides T0 is
    # inside the limit and keeps the expansion this fixture was written around.
    "alternatives": [{"targetId": "TA", "levels": {"I1.r1": 0, "I2.r1": 1}},
                     {"targetId": "TB", "levels": {"I1.r1": 1, "I2.r1": 0}},
                     {"targetId": "TC", "levels": {"I1.r1": 1, "I2.r1": 1}},
                     {"targetId": "TD", "levels": {"I1.r1": 2, "I2.r1": 0}},
                     {"targetId": "TE", "levels": {"I1.r1": 2, "I2.r1": 1}}],
    "ranking": {"costRule": "normalized-concession",
                "tieBreak": "lexicographic(D_max, D_mean) then intent order"},
    "rationale": "each owner's own signed steps, nothing capped",
}

#: ... and a valid Control answer: the functions used together, not axes.
C_ANSWER = {
    "candidates": [
        {"controlId": "C1",
         "functions": [{"functionId": "steer", "scope": "ue@131",
                        "policy": {"servingCell": TARGET_NCI}}],
         "predicted": {"I1.r1": 4.0, "I2.r1": "unknown"},
         "uncertainty": {"I1.r1": 0.6}, "predictedTarget": "T0",
         "applicability": ["131 is attached"],
         "evidenceRefs": ["prediction:steer-131"],
         "rationale": "moving 131 frees the cell for both"},
        {"controlId": "C2",
         "functions": [{"functionId": "steer", "scope": "ue@132",
                        "policy": {"servingCell": TARGET_NCI}}],
         "predicted": {}, "uncertainty": {}, "predictedTarget": "T0",
         "applicability": ["132 is attached"], "evidenceRefs": [],
         "rationale": "moving 132 instead"},
    ],
    "rationale": "one single-function move per owner",
}

PAIR = {"controlId": "C1", "targetId": "T0",
        "rationale": "moving 131 leaves each owner the whole cell"}


#: One rung per ladder in this file's fixtures.
FIXTURE_CAP: Sequence[int] = (12,)
FIXTURE_PF: Sequence[float] = (2.0,)


def fixture_scale(request: AgentRequest, ue_ids: Sequence[str]) -> AgentRequest:
    """Keep a fixture sitting small enough to freeze in seconds.

    The default exposure is the cap and the scheduler weight on **every**
    intent UE with the contract's four-rung ladders, which over two UEs and
    two cells is 1024 candidates -- and this Kernel's epoch freeze is
    superlinear in the catalog: 64 candidates take about ten seconds and 324
    about seven minutes on this machine (the W3 report has the numbers and
    the cause, which is the reducer's per-event deep copy).  Freezing the
    default here would make this file unrunnable and would test the scale
    rather than the wiring.

    So a fixture exposes steering plus whichever kinds the test states a
    ladder for, one rung for any UE it did not name.  What the default
    exposure *is*, and what a sitting over the ceiling does, is asserted in
    :class:`TheExposedAxes` and :class:`TheCatalogCeiling`, where nothing has
    to be frozen at all.  A test that states ``axes`` itself is left alone.
    """
    if tuple(request.axes) != tuple(DEFAULT_AXIS_KINDS):
        return request
    kinds, caps, weights = ["servingCell"], dict(request.caps), dict(request.pf_weights)
    if caps:
        kinds.append("dlPrbCap")
        caps = {ue: tuple(caps.get(ue) or FIXTURE_CAP) for ue in ue_ids}
    if weights:
        kinds.append("pfWeight")
        weights = {ue: tuple(weights.get(ue) or FIXTURE_PF) for ue in ue_ids}
    return replace(request, axes=tuple(kinds), caps=caps, pf_weights=weights)


def slow(answer: Mapping[str, Any], latency_ms: float) -> ScriptedResponse:
    """A scripted answer that costs time.

    ``MOCK`` charges the **real** latency of a model call to the virtual clock,
    so an answer that takes longer than an observation's validity is stale
    exactly as it would be live -- without anybody waiting for it here.
    """
    body = json.dumps(dict(answer))
    return ScriptedResponse(success=True, content=body, parsed_json=dict(answer),
                            latency_ms=float(latency_ms))


class AgentSittingFixture(unittest.TestCase):
    """One hardware-free sitting per test, on its own temporary deployment."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = temporary.name

    def build(self, sentences: Sequence[str] = (I1, I2), *, ran: Any = None,
              resolver: Any = None, prepared: Any = None, **request_kwargs: Any):
        request = AgentRequest(sentences=tuple(sentences), **request_kwargs)
        return self.build_request(request, ran=ran, resolver=resolver,
                                  prepared=prepared)

    def build_request(self, request: AgentRequest, *, ran: Any = None,
                      resolver: Any = None, **kwargs: Any):
        ran = ran or EmulatedRan(ues={"131": HOME_NCI, "132": HOME_NCI},
                                 cells={HOME_NCI: 5.0, TARGET_NCI: 5.0},
                                 offered_load_mbps={"131": 4.0, "132": 4.0},
                                 noise_sigma=0.0)
        return build_hardware_free_agent_sitting(
            fixture_scale(request, list(ran.ues)), tmp_dir=self.tmp, ran=ran,
            role_resolver=resolver, stamp="20260907T120000Z", **kwargs)

    @staticmethod
    def scripted(answers: Mapping[str, Sequence[Any]], *, method: str = METHOD_THREE_AGENT):
        resolver, models = ScriptedResolver.by_role(answers, method=method)
        return resolver, models.to_record()


class TheIntakeAsksBeforeAnyModelCall(AgentSittingFixture):
    """Contract v2 section 2.2: what the operator did not say, asked first."""

    #: A sentence that says nothing about relaxation.  Missing information --
    #: **not** a refusal to relax, which is the whole point of the checklist.
    SILENT = "I3: UE ueId=131 needs at least 3.0 Mbps downlink"

    def test_a_sentence_with_no_relaxation_phrase_stops_the_sitting(self) -> None:
        with self.assertRaises(ClarificationNeeded) as caught:
            self.build([self.SILENT, I2], method=METHOD_DETERMINISTIC)
        questions = caught.exception.questions
        self.assertEqual(["I3"], [item["intentId"] for item in questions])
        self.assertEqual(["steps"], [item["field"] for item in questions])
        self.assertFalse(caught.exception.refused)
        self.assertIn("steps", str(caught.exception))

    def test_the_operators_answer_resolves_it_and_the_sitting_runs(self) -> None:
        request = AgentRequest(sentences=(self.SILENT, I2),
                               method=METHOD_DETERMINISTIC, budget_trials=2)
        with self.assertRaises(ClarificationNeeded) as caught:
            self.build_request(request)
        answers = {item["intentId"]: {"steps": 2, "bound": 2.0}
                   for item in caught.exception.questions}
        sitting = self.build_request(request.with_answers(answers))
        self.assertEqual(1, sitting.request.clarification_round)
        self.assertEqual((3.0, 2.5, 2.0),
                         sitting.intents[0].requirement.levels)
        self.assertEqual([], sitting.intake["missing"])
        self.assertEqual(1, sitting.intake["rounds"])
        self.assertEqual({"I3": {"steps": 2, "bound": 2.0}}, sitting.intake["answers"])

    def test_it_refuses_to_start_after_two_rounds(self) -> None:
        request = AgentRequest(sentences=(self.SILENT, I2),
                               method=METHOD_DETERMINISTIC)
        # Two rounds of answers that do not answer the question.
        request = request.with_answers({"I3": {"owner": "somebody"}})
        request = request.with_answers({"I3": {"owner": "somebody else"}})
        self.assertEqual(2, request.clarification_round)
        with self.assertRaises(ClarificationNeeded) as caught:
            self.build_request(request)
        self.assertTrue(caught.exception.refused)
        self.assertIn("will not start", str(caught.exception))

    def test_the_intake_is_a_recorded_call_of_its_own_phase(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=1)
        intake = [call for call in sitting.agents.calls if call.role == "intake"]
        self.assertEqual(1, len(intake))
        self.assertEqual("intake", intake[0].phase)
        self.assertTrue(intake[0].accepted)
        # An intake call is not a model invocation and is not charged as one.
        self.assertEqual(0, sitting.resource_cost()["llmCalls"])

    def test_the_target_agents_own_questions_do_not_stop_the_sitting(self) -> None:
        # 2026-09-20: the model is not asked for questions; one it sends anyway is
        # ignored and the sitting is prepared as usual.
        asking = dict(T_ANSWER, missingInformation=[
            {"intentId": "I2", "field": "bound",
             "question": "how far may the map data be relaxed?"}])
        resolver, models = self.scripted({"target": [asking], "control": [C_ANSWER]})
        sitting = self.build(resolver=resolver, role_models=models)
        self.assertIn("T0", sitting.contract.target_ids)


class ThePreparationFormsTAndC(AgentSittingFixture):

    def test_the_target_and_control_agents_form_the_rows_and_the_columns(self) -> None:
        resolver, models = self.scripted({"target": [T_ANSWER], "control": [C_ANSWER]})
        sitting = self.build(resolver=resolver, role_models=models,
                             method=METHOD_THREE_AGENT, budget_trials=4)
        self.assertEqual("MOCK", sitting.mode)
        # 3 levels on I1 x 2 on I2 = the whole authorized product, T0 first.
        self.assertEqual(("T0", "T1", "T2", "T3", "T4", "T5", "T6"),
                         sitting.contract.target_ids)
        self.assertEqual({"I1.r1": 3.0, "I2.r1": 1.5}, sitting.contract.t0.requirements)
        self.assertEqual([0.0], [t.cost for t in sitting.contract.targets][:1])
        # Contract v2 section 3: the order is the owners' normalized concession,
        # not sum_i w_i q_i^2.  The squared cost is still recorded on every
        # target and along this order it is deliberately not monotone -- an
        # integer level counts subdivisions, so I1 cut into two steps and I2
        # into one were charged 4w and w for the same full concession.  (Since
        # 2026-09-19 the signed steps stand, and on this board the order happens
        # to come out monotone.)
        self.assertEqual([0.0, 1.0, 1.0, 2.0, 4.0, 5.0, 8.0],
                         [t.cost for t in sitting.contract.targets])
        self.assertEqual(("C0", "C1", "C2"), sitting.controls.control_ids)
        # Every control resolves to one candidate of the frozen catalog.
        self.assertEqual(set(sitting.controls.control_ids), set(sitting.catalog_of_control))
        self.assertEqual((), sitting.unmapped)
        # C0 is always the configuration already applied.
        self.assertEqual({"servingCell@131": HOME_NCI, "servingCell@132": HOME_NCI},
                         sitting.controls.candidate("C0").configuration)
        self.assertEqual(4, sitting.preview()["catalogCardinality"])

    def test_a_candidate_is_the_functions_used_together(self) -> None:
        resolver, models = self.scripted({"target": [T_ANSWER], "control": [C_ANSWER]})
        sitting = self.build(resolver=resolver, role_models=models)
        candidate = sitting.controls.candidate("C1")
        self.assertEqual(["steer"], [item.function_id for item in candidate.functions])
        self.assertEqual("ue@131", candidate.functions[0].scope)
        # ... and the executor's translation of them onto the axes is what the
        # frozen catalog and the Kernel actually see.
        self.assertEqual({"servingCell@131": TARGET_NCI, "servingCell@132": HOME_NCI},
                         candidate.configuration)

    def test_the_catalog_names_only_what_this_sitting_exposes(self) -> None:
        # The cap and the weight are on *every* UE now (contract v3 section
        # 3), and UEs offered the same ladder share one function, so the
        # catalog is three functions over two scopes each rather than one
        # function per UE.
        sitting = self.build(method=METHOD_DETERMINISTIC,
                             caps={"131": (12,), "132": (12,)},
                             pf_weights={"131": (2.0,), "132": (2.0,)})
        catalog = {spec.function_id: spec for spec in sitting.catalog}
        self.assertEqual({"steer", "ue-dl-prb-cap", "ue-sched-priority"}, set(catalog))
        for function_id in catalog:
            self.assertEqual(("ue@131", "ue@132"), catalog[function_id].scopes)
        self.assertEqual("maxDlPrbs", catalog["ue-dl-prb-cap"].axis_field)
        self.assertEqual("pfWeight", catalog["ue-sched-priority"].axis_field)
        # This sitting did not expose the cell- and slice-scoped kinds, so
        # they are absent -- exposure, not capability, is what decides.
        for unexposed in ("mcs", "attenuation", "slice"):
            self.assertNotIn(unexposed, json.dumps(sitting.catalog.to_record()).lower())

    def test_an_unselected_function_goes_back_to_its_baseline_by_default(self) -> None:
        answer = {"candidates": [
            {"controlId": "CX", "functions": [
                {"functionId": "ue-dl-prb-cap", "scope": "ue@132",
                 "policy": {"maxDlPrbs": "12"}}], "predictedTarget": "T0"}],
            "rationale": "cap 132 and leave the cells alone"}
        resolver, models = self.scripted({"target": [T_ANSWER], "control": [answer]})
        # Both UEs are offered the same ladder, so the cap is one shared
        # function rather than one per UE -- which is the name the answer above
        # uses, and the sharing rule of contract v2 section 3.1.
        sitting = self.build(resolver=resolver, role_models=models,
                             caps={"131": (6, 12), "132": (6, 12)})
        candidate = sitting.controls.candidate("CX")
        self.assertEqual("12", candidate.configuration["dlPrbCap@132"])
        self.assertEqual(HOME_NCI, candidate.configuration["servingCell@131"])

    def test_keep_current_leaves_the_running_functions_where_they_are(self) -> None:
        answer = {"candidates": [
            {"controlId": "CX", "functions": [
                {"functionId": "ue-dl-prb-cap", "scope": "ue@132",
                 "policy": {"maxDlPrbs": "12"}}], "predictedTarget": "T0"}],
            "rationale": "cap 132"}
        resolver, models = self.scripted({"target": [T_ANSWER], "control": [answer]})
        sitting = self.build(resolver=resolver, role_models=models,
                             caps={"131": (6, 12), "132": (6, 12)},
                             settings={"unselectedFunctionRule": "keep-current"})
        self.assertEqual("keep-current", sitting.unselected_rule)
        self.assertEqual("12", sitting.controls.candidate("CX").configuration["dlPrbCap@132"])
        # Nothing is running off its baseline yet, so both rules agree here;
        # what the rule changes is which value an unselected axis is given.
        self.assertEqual(HOME_NCI,
                         sitting.controls.candidate("CX").configuration["servingCell@132"])

    def test_a_function_the_catalog_does_not_have_is_excluded(self) -> None:
        answer = {"candidates": [
            {"controlId": "CX", "functions": [
                {"functionId": "mcs-clamp", "scope": "ue@131",
                 "policy": {"mcs": "9"}}]},
            C_ANSWER["candidates"][0]], "rationale": "one of these is not ours"}
        resolver, models = self.scripted({"target": [T_ANSWER], "control": [answer]})
        sitting = self.build(resolver=resolver, role_models=models)
        self.assertEqual(("C0", "C1"), sitting.controls.control_ids)
        dropped = [call for call in sitting.agents.calls
                   if call.role == "control"][0].dropped
        self.assertTrue(any("CX" in item for item in dropped), dropped)

    def test_a_bound_past_the_signed_one_is_clipped_and_t0_is_kept_exact(self) -> None:
        answer = dict(T_ANSWER, levels={"I1.r1": {"steps": 2, "bound": 0.5},
                                        "I2.r1": {"steps": 1, "bound": 1.0}})
        resolver, models = self.scripted({"target": [answer], "control": [C_ANSWER]})
        sitting = self.build(resolver=resolver, role_models=models)
        self.assertEqual({"I1.r1": 3.0, "I2.r1": 1.5}, sitting.contract.t0.requirements)
        for target in sitting.contract.targets:
            self.assertGreaterEqual(target.requirements["I1.r1"], 2.0)
        dropped = [call for call in sitting.agents.calls if call.role == "target"][0].dropped
        # 2026-09-19: a "levels" key in the answer is ignored; the signed range stands.
        self.assertTrue(any("signed steps stand" in item for item in dropped), dropped)

    def test_a_sitting_with_one_intent_is_still_a_sitting(self) -> None:
        sitting = self.build([I1], method=METHOD_DETERMINISTIC, budget_trials=2)
        self.assertEqual(("I1",), tuple(i.intent_id for i in sitting.intents))
        self.assertIn("T0", sitting.contract.target_ids)
        self.assertTrue(sitting.controls.control_ids)

    def test_this_consoles_own_objective_sentences_still_compose(self) -> None:
        rows = parse_agent_intents(AgentRequest(sentences=(
            f"Hold the UE-level serving cell at {TARGET_NCI} nci for ueId=131",)))
        self.assertEqual("UELevelTarget", rows[0].family)
        self.assertEqual(int(TARGET_NCI), rows[0].named_cell)
        self.assertEqual("servingCell", rows[0].intent.requirement.kpi)
        self.assertEqual("==", rows[0].intent.requirement.op)

    def test_refusals_before_anything_is_written(self) -> None:
        with self.assertRaises(LiveConsoleError) as caught:
            self.build(["I1: needs at least 3.0 Mbps downlink"])
        self.assertIn("ueId", str(caught.exception))
        with self.assertRaises(LiveConsoleError):
            AgentRequest(sentences=(), intents=())
        with self.assertRaises(LiveConsoleError) as caught:
            AgentRequest(sentences=(I1,), method="board-search")
        self.assertIn("unknown coordination method", str(caught.exception))
        with self.assertRaises(LiveConsoleError) as caught:
            AgentRequest(sentences=(I1,), settings={"unselectedFunctionRule": "invent"})
        self.assertIn("unselected-function rule", str(caught.exception))
        with self.assertRaises(LiveConsoleError):
            self.build([I1], method=METHOD_DETERMINISTIC).run()

    def test_an_unstated_retain_is_four_per_trial_and_not_the_drops_board(self) -> None:
        """``DEFAULT_RETAIN_PER_TRIAL`` is a per-trial scaling policy, not the
        drop's fixed cap.

        The integrated reply of 2026-09-14 section 4 sets "at most 12
        configurations including baseline", which is ``retain=11`` because
        ``retain`` counts the candidates besides ``C0``.  The 4K default
        predates it and applies only where nobody states ``retain``; every
        runner the paper is measured on states it.  Both counted rather than
        read off the constants, because reading constants is exactly what let
        11, 12 and 4K disagree unnoticed.
        """
        wide = dict(method=METHOD_DETERMINISTIC, budget_trials=4,
                    caps={"131": (6, 12, 18, 24), "132": (6, 12, 18, 24)})
        unstated = self.build(**wide)
        self.assertEqual(agent_module.DEFAULT_RETAIN_PER_TRIAL * 4,
                         unstated.intake["settings"]["retain"])
        # retain is a ceiling on C, not a quota: since 2026-09-19 a steer excludes a
        # cap on the same UE, and this two-UE board no longer has 16 (or 11)
        # admissible moves, so C is as large as the space allows and never larger.
        self.assertLessEqual(len(unstated.controls.control_ids),
                             agent_module.DEFAULT_RETAIN_PER_TRIAL * 4 + 1)
        stated = self.build(settings={"retain": 11}, **wide)
        self.assertLessEqual(len(stated.controls.control_ids), 12)
        self.assertGreater(len(stated.controls.control_ids), 1)
        self.assertEqual("C0", stated.controls.control_ids[0])


class TargetSeesTheDeploymentAndItsEffects(AgentSittingFixture):
    """Section 3 of the integrated reply: Target selects, so it is shown state.

    "Target needs evidence to distinguish useful concessions from an arbitrary
    ranked prefix" -- and, in the same breath, "do not send full Cartesian
    policy prediction tables".  Both hold at once because the call site hands
    over the very object Control is given and the input position compacts it:
    the families the table exercises and the predictor's own prose, never the
    per-configuration rows.
    """

    def recorded(self, **kwargs: Any):
        """One sitting, keeping the ``TargetInputs`` its call site built."""
        seen: List[Any] = []
        real = agent_module.TargetInputs

        def record(*args: Any, **inner: Any) -> Any:
            seen.append(real(*args, **inner))
            return seen[-1]

        with patch.object(agent_module, "TargetInputs", record):
            sitting = self.build(**kwargs)
        self.assertEqual(1, len(seen))
        return sitting, seen[0]

    def three_agent(self, **kwargs: Any):
        resolver, models = self.scripted({"target": [T_ANSWER], "control": [C_ANSWER]})
        return self.recorded(resolver=resolver, role_models=models,
                             method=METHOD_THREE_AGENT, **kwargs)

    def test_target_is_given_the_running_state_and_the_effect_evidence(self) -> None:
        _sitting, inputs = self.three_agent()
        self.assertEqual({"131", "132"}, set(inputs.network_state["ues"]))
        self.assertEqual(HOME_NCI, inputs.network_state["ues"]["131"]["servingCell"])
        # Owner instructions 2026-09-19/20: no prediction table and no
        # code-built associations -- the measurements only.
        self.assertEqual({"observations"}, set(inputs.effect_evidence))
        self.assertEqual(dict(inputs.network_state),
                         inputs.payload()["input.network_state"])

    def test_the_effect_evidence_does_not_reach_the_target_prompt(self) -> None:
        """2026-09-20 오너 승인: 목표 구성은 실측 근거에 의존하지 않는다.

        인텐트·인가·선호가 같으면 T 를 재사용할 수 있어야 하는데 실측은 판마다
        달라진다.  그래서 Target 의 payload 에서 `input.effect_evidence` 를 뺐다
        (`SINGLE_CALL.md` 변경 이력 2026-09-20).  dataclass 의 칸은 남아 있으므로
        **payload 로** 판정해야 한다 -- 칸만 보면 아직 가는 줄 안다.
        """
        _sitting, inputs = self.three_agent()
        self.assertNotIn("input.effect_evidence", inputs.payload())
        families = [str(axis) for axis in inputs.network_state.get("appliedConfiguration", {})]
        self.assertTrue(families)
        # This fixture exposes steering alone, so that is the whole axis list.
        self.assertTrue(all(str(name).startswith("servingCell") for name in families),
                        families)

    def test_it_still_forms_targets_when_there_is_no_evidence_to_give(self) -> None:
        # Both inputs default to empty precisely so that a caller with neither
        # still forms targets -- it just forms them blind.
        with patch.object(agent_module, "model_effect_evidence", return_value={}):
            sitting, inputs = self.three_agent()
        self.assertEqual({}, dict(inputs.effect_evidence))
        self.assertEqual(("T0", "T1", "T2", "T3", "T4", "T5", "T6"),
                         sitting.contract.target_ids)

    def test_the_internal_monolith_still_reads_controls_whole_table(self) -> None:
        formation = {**T_ANSWER, "candidates": C_ANSWER["candidates"],
                     "rationale": "T and C in one call"}
        resolver, models = self.scripted({"monolith": [formation, PAIR]},
                                         method=METHOD_INTERNAL_MONOLITH)
        _sitting, inputs = self.recorded(resolver=resolver, role_models=models,
                                         method=METHOD_INTERNAL_MONOLITH)
        payload = MonolithFormInputs(
            target=inputs,
            control=ControlInputs(network_state=inputs.network_state,
                                  effect_evidence=inputs.effect_evidence)).payload()
        # One call, one evidence position: Control's full table overwrites the
        # compact copy rather than the compaction quietly starving it.
        self.assertEqual(dict(inputs.effect_evidence),
                         payload["input.effect_evidence"])
        self.assertNotIn("controlKpiAssociations", payload["input.effect_evidence"])


class TheOwnerPreferenceProfile(AgentSittingFixture):
    """Section 3 of the integrated reply: P1/P2/P3 reach the running sitting.

    The profile is chosen by ``AIC_PREFERENCE`` and injected once, where the
    sitting's authorization is built, so a campaign that declares a preference
    gets a board ranked by it instead of by a bare default.
    """

    def sitting(self):
        return self.build(method=METHOD_DETERMINISTIC, budget_trials=2)

    @staticmethod
    def order(contract, preference):
        """The board in ``preference`` order, by the ids it was expanded with."""
        return tuple(target.target_id
                     for target in replace(contract, preference=preference).ranked())

    def test_each_profile_is_the_owner_order_it_names(self) -> None:
        intents = self.sitting().intents
        base, p1, p2, p3 = (_preference_for(intents, name)
                            for name in (None, "P1", "P2", "P3"))
        # This console names an owner after its own intent, so the owner order
        # is the declared one and P2 is that order rotated: (D2, D3, D1).
        self.assertEqual(("I1", "I2"), base.owner_priority)
        self.assertEqual(("I1", "I2"), p1.owner_priority)
        self.assertEqual(("I2", "I1"), p2.owner_priority)
        self.assertEqual(base.owner_priority, p3.owner_priority)
        # P1 and P2 rank on the owners' concessions alone; only P3 leads with
        # (D_max, D_mean), which is what preference_key reads out of the rule.
        self.assertNotIn("D_max", p1.rule)
        self.assertNotIn("D_max", p2.rule)
        self.assertIn("D_max", p3.rule)
        # Every profile names itself in the rule the episode record serializes.
        for name in ("P1", "P2", "P3"):
            self.assertTrue(_preference_for(intents, name).rule.startswith(name + ":"))

    def test_an_unset_variable_ranks_exactly_as_it_did_before(self) -> None:
        sitting = self.sitting()
        base = _preference_for(sitting.intents, None)
        self.assertEqual(Preference.from_intents(sitting.intents), base)
        self.assertEqual(base, sitting.contract.preference)
        # P3 is that same ranking under its name -- the key is unchanged, so
        # the board comes out in the identical order.
        self.assertEqual(self.order(sitting.contract, base),
                         self.order(sitting.contract, _preference_for(sitting.intents, "P3")))

    def test_the_owner_order_is_the_boards_order_not_decoration(self) -> None:
        # I1 concedes in three levels and I2 in two, so which owner is asked to
        # yield first genuinely reorders the authorized domain.  Asserted on
        # Omega rather than on this sitting's T: under v3.1 a fallback T is only
        # the mandatory targets (T0 and the boundary), whose order no profile
        # can change, while the ranking law itself is what this pins.
        sitting = self.sitting()
        omega = expand_targets(sitting.contract.authorization)
        self.assertNotEqual(
            self.order(omega, _preference_for(sitting.intents, "P1")),
            self.order(omega, _preference_for(sitting.intents, "P2")))

    def test_the_environment_variable_reaches_t_and_the_record(self) -> None:
        with patch.dict(os.environ, {"AIC_PREFERENCE": "P2"}):
            sitting = self.sitting()
        preference = sitting.contract.preference
        self.assertEqual(("I2", "I1"), preference.owner_priority)
        self.assertTrue(preference.rule.startswith("P2:"))
        # The same object the authorization carries, and what the record says.
        self.assertEqual(preference, sitting.contract.authorization.preference)
        self.assertEqual(preference.to_record(),
                         sitting.contract.to_record()["preference"])

    def test_a_name_that_is_not_a_profile_changes_nothing(self) -> None:
        intents = self.sitting().intents
        for name in ("", "P4", "nonsense"):
            with self.subTest(name=name):
                self.assertEqual(Preference.from_intents(intents),
                                 _preference_for(intents, name))


class ThePredictorIsSeparateFromTheGroundTruth(AgentSittingFixture):
    """Contract v2 section 4: estimates, and where they come from."""

    def test_the_effect_evidence_carries_associations_not_predictions(self) -> None:
        # Owner instruction 2026-09-19.
        resolver, models = self.scripted({"target": [T_ANSWER], "control": [C_ANSWER]})
        sitting = self.build(resolver=resolver, role_models=models)
        # 2026-09-20: and no code-built associations either -- Control makes them.
        for role in ("scripted:target", "scripted:control"):
            prompt = resolver.prompts_for(role)[0]
            for gone in ("predictorDescription", '"predictions"', "capacityMbps",
                         "uncertaintyNote", "controlKpiAssociations"):
                self.assertNotIn(gone, prompt)
        evidence = sitting.effect_evidence()
        self.assertEqual({"observations"}, set(evidence))

    def test_the_prediction_is_an_estimate_not_the_emulators_own_number(self) -> None:
        # Same configuration, two models: the emulator adds seeded measurement
        # noise the predictor cannot see, and the predictor charges a handover
        # transient the emulator only applies to a UE that actually moved.  An
        # agent ranking candidates with this is not reading the ground truth.
        ran = EmulatedRan(ues={"131": HOME_NCI, "132": HOME_NCI},
                          cells={HOME_NCI: 5.0, TARGET_NCI: 5.0},
                          offered_load_mbps={"131": 4.0, "132": 4.0},
                          noise_sigma=0.25, seed=7)
        sitting = self.build(ran=ran, method=METHOD_DETERMINISTIC, budget_trials=1)
        moved = {"servingCell@131": TARGET_NCI, "servingCell@132": HOME_NCI}
        predicted = sitting.predictor.predict(moved)
        ran.apply("servingCell@131", "131", TARGET_NCI)
        ran.advance(10000)
        truth = ran.dl_goodput()
        self.assertNotEqual(round(predicted["dlGoodputMbps@131"][0], 6),
                            round(truth["131"], 6))
        self.assertGreater(predicted["dlGoodputMbps@131"][1], 0.0)

    def test_every_candidate_carries_the_target_it_is_predicted_to_satisfy(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=1)
        # The fallback still ranks by the predictor, internally (2026-09-19).
        for candidate in sitting.controls.candidates[1:]:
            self.assertIn(candidate.predictor_target,
                          sitting.contract.target_ids + ("",))
            self.assertFalse(candidate.predicted)
            self.assertFalse(any("predictor" in ref for ref in candidate.evidence_refs))

    def test_it_is_calibrated_from_what_the_trials_measured(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=2)
        self.assertEqual([], sitting.predictor_versions)
        sitting.confirm()
        sitting.run()
        self.assertTrue(sitting.predictor_versions)
        self.assertEqual("calibrated",
                         sitting.predictor.describe()["calibration"]["state"])
        self.assertTrue(sitting.predictor.capacity_scale)


class TheTrajectorySearchesTheGrid(AgentSittingFixture):

    def test_the_scripted_trajectory_reaches_the_original_targets(self) -> None:
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER], "trajectory": [PAIR]})
        sitting = self.build(resolver=resolver, role_models=models, budget_trials=4)
        sitting.confirm()
        seen: List[Dict[str, Any]] = []
        summary = sitting.run(on_trial=seen.append)
        self.assertEqual("T0_SUCCESS", summary["termination"])
        self.assertEqual(1, len(seen))
        trial = seen[0]
        self.assertEqual("C1", trial["controlId"])
        self.assertEqual(TARGET_NCI, trial["configuration"]["servingCell@131"])
        self.assertGreaterEqual(trial["kpis"]["dlGoodputMbps@131"], 3.0)
        # One trial fills the whole column: every authorized target is judged.
        # 2026-09-23: 판정은 이제 **Ω 전체**에 대해 일어난다(결정 §2) -- 선택된
        # T 로 확인하면 3A·IM 만 6~8 열이고 BM 만 144 열이던 그 비대칭을 다시
        # 못 박는 셈이다.
        self.assertEqual(set(sitting.evaluation_contract.target_ids),
                         set(trial["verdicts"]))
        self.assertTrue(trial["success"]["T0"])
        for target_id in sitting.contract.target_ids:
            self.assertEqual({"C1": "PASS"}, summary["grid"]["cells"][target_id])
        best = summary["bestAttained"]
        self.assertEqual("T0", best["targetId"])
        self.assertEqual(0.0, best["concession"]["max"])

    def test_the_trajectory_is_shown_the_best_it_has_and_the_gaps_it_has_left(self) -> None:
        answers = [PAIR, {"controlId": "C2", "targetId": "T0", "rationale": "try 132"}]
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER], "trajectory": answers})
        sitting = self.build(["I1: UE ueId=131 needs at least 4.5 Mbps downlink, "
                              "relaxable to 2.0", I2],
                             resolver=resolver, role_models=models, budget_trials=2)
        sitting.confirm()
        sitting.run()
        first, second = resolver.prompts_for("scripted:trajectory")[:2]

        def inputs_of(prompt: str) -> dict:
            """The INPUTS object, read as JSON rather than as formatted text.

            This used to assert on the substring ``'"input.observed_best": null'``,
            which pinned the serialiser's whitespace as well as its content and
            broke when the prompt went compact.  What the contract actually says
            is that the *value* is null, so read the value.
            """
            import json as _json
            body = prompt.split("INPUTS:\n", 1)[1].split("\n\nOUTPUT SCHEMA:\n")[0]
            return _json.loads(body)

        # With no history, both are null rather than an empty object claiming
        # a gap of zero (SINGLE_CALL.md).
        payload = inputs_of(first)
        self.assertIn("input.observed_best", payload)
        self.assertIsNone(payload["input.observed_best"])
        self.assertIn("input.kpi_gaps", payload)
        self.assertIsNone(payload["input.kpi_gaps"])
        # With history, the executor -- not the model -- computes them.
        self.assertIn("input.observed_best", second)
        self.assertIn("againstT0", second)
        self.assertIn("shortfall", second)
        for prompt in (first, second):
            self.assertEqual([], budget_terms_in(prompt))

    def test_every_observation_carries_its_window_end_and_its_validity(self) -> None:
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER],
            # T1, not T5: a refused Target falls back to the mandatory targets
            # (T0 and the boundary) under v3.1, never the whole expansion.
            "trajectory": [{"controlId": "C2", "targetId": "T1", "rationale": "132"},
                           PAIR]})
        sitting = self.build(["I1: UE ueId=131 needs at least 4.5 Mbps downlink, "
                              "relaxable to 2.0", I2],
                             resolver=resolver, role_models=models, budget_trials=2)
        sitting.confirm()
        sitting.run()
        rows = sitting.observations()
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["windowEnd"])
            self.assertTrue(row["validUntil"])
            self.assertGreater(row["validUntil"], row["windowEnd"])
            self.assertIn("expired", row)
            self.assertTrue(row["valid"])
        self.assertIn("validUntil", resolver.prompts_for("scripted:trajectory")[1])

    def test_a_relaxed_success_does_not_stop_the_search_by_default(self) -> None:
        # 4.5 Mbps is out of this cell's reach for 131, so the sitting meets a
        # relaxed target on its first trial and keeps looking for T0 until the
        # frozen budget is spent.
        sitting = self.build(["I1: UE ueId=131 needs at least 4.5 Mbps downlink, "
                              "relaxable to 2.0", I2],
                             method=METHOD_DETERMINISTIC, budget_trials=2)
        sitting.confirm()
        summary = sitting.run()
        self.assertEqual("BUDGET_EXHAUSTED", summary["termination"])
        self.assertEqual(2, summary["trials"])
        self.assertFalse(summary["t0Success"])
        self.assertTrue(any(any(trial.success.values()) for trial in sitting.grid.trials))
        best = summary["bestAttained"]
        self.assertNotEqual("T0", best["targetId"])
        self.assertGreater(best["concession"]["max"], 0.0)

    def test_a_repeated_cell_is_revised_once_and_is_not_exhaustion(self) -> None:
        """Re-offering a spent cell ends the walk only when nothing is left.

        The executor returns the rejection reason and asks once more inside the
        same decision deadline.  This script always answers ``C0``, so the
        revision fails too -- but candidates remain eligible, so the episode
        stops at ``PROPOSAL_FAILURE``.  ``CATALOG_EXHAUSTED`` is an exhaustion
        certificate and a rejected proposal is not one.
        """
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER],
            "trajectory": [{"targetId": "T5", "controlId": "C0", "rationale": "hold"}]})
        sitting = self.build(resolver=resolver, role_models=models, budget_trials=3)
        sitting.confirm()
        summary = sitting.run()
        self.assertEqual("PROPOSAL_FAILURE", summary["termination"])
        self.assertIn("already been tried", summary["detail"])
        self.assertIn("still eligible", summary["detail"])
        self.assertEqual(1, summary["trials"])

    def test_a_superseded_best_control_is_re_applied_on_a_retention_case(self) -> None:
        # Budget 3 with a 4.5 Mbps floor 131 cannot reach: the deterministic
        # walk meets a relaxed target on some trial and later trials move the
        # radio elsewhere.  The search case admits each candidate once, so the
        # best control comes back on a retention case of its own, opened on
        # what the radio holds at that moment, and the deployment ends up
        # holding the best attained control -- judged on its own window.
        sitting = self.build(["I1: UE ueId=131 needs at least 4.5 Mbps downlink, "
                              "relaxable to 2.0", I2],
                             method=METHOD_DETERMINISTIC, budget_trials=3)
        sitting.confirm()
        summary = sitting.run()
        self.assertEqual("BUDGET_EXHAUSTED", summary["termination"])
        best = summary["bestAttained"]
        retained = sitting.retained
        self.assertEqual(best["controlId"], retained["controlId"])
        self.assertEqual(best["targetId"], retained["targetId"])
        self.assertTrue(retained["qualified"], retained)
        self.assertEqual(sitting.controls.candidate(best["controlId"]).configuration,
                         sitting.applied_configuration())
        if retained.get("retentionCase", "").endswith("/retention"):
            self.assertIsNotNone(sitting.retention_runtime)
            self.assertEqual("SUCCESS", sitting.retention_kernel_termination)
            self.assertEqual("SETTLED_SUCCESS", retained["trial"]["kernel"]["terminalState"])
        self.assertEqual(3, summary["trials"], "the retention trial is not a search column")

    def test_retention_refuses_a_sentinel_return_instead_of_spending_a_permit(self) -> None:
        """Asking a held supplementary axis back to its sentinel is inexpressible.

        The equipment leaves a sentinel by CREATEing a policy and returns to it
        by WITHDRAWing one, and the Kernel grants no STOP after a settlement, so
        retention cannot express the return.  Enabling the scope takeover on
        2026-09-17 I deleted this guard along with the one the takeover really
        did retire (a *non*-sentinel move, which adoption made expressible), and
        the next retention applied ``dlPrbCap@ue2 = "0"`` to earn
        ``VALIDATE failed: an applied cap is 5..24 PRB ... got 0`` -- a permit
        spent on a refusal that reads like a defect (episode d69fd188).

        The axis here is the attenuation, not that cap.  Later the same morning
        the cap turned out to be applicable at ``0`` after all (the producer
        schema reads "0 removes the cap"), so it no longer exercises this guard;
        a composite axis carries several policy leaves and has no single wire
        scalar, which is a refusal of a different and still-real kind.
        """
        from types import SimpleNamespace
        from tools.liveconsole.agent import AgentSitting

        best = SimpleNamespace(control_id="C0", target_id="T7")
        candidate = SimpleNamespace(configuration={"txAttenuationDb@ue1": "0.0"})

        class _Stub:
            """Only what ``_retain`` reads on the way to the guard."""

            contract = SimpleNamespace(t0=SimpleNamespace(target_id="T0"))
            # 2026-09-23: 채점은 Ω 전체에 대해 일어나고 `best_attained` 도 그것을
            # 받는다(결정 §2).  스텁도 그 필드를 들어야 `_retain` 이 돈다.
            evaluation_contract = contract
            grid = SimpleNamespace(trials=(), best_attained=lambda _contract: best)
            controls = SimpleNamespace(candidate=lambda _id: candidate)
            request = SimpleNamespace(retain_best=True)
            catalog_of_control: dict = {}
            retained = None
            # Reached only if the guard is gone; then the assertions below fail
            # on ``detail`` rather than on a missing attribute.
            runtime = SimpleNamespace(catalog_entries=lambda: ())
            retention_factory = None
            _spent_elsewhere = staticmethod(lambda: frozenset())

            _disconnect_trial_ids = staticmethod(lambda: frozenset())
            _unaddressable_ues = staticmethod(lambda: ())
            applied_configuration = staticmethod(lambda: {"txAttenuationDb@ue1": "6.0"})
            _axes_needing_withdrawal = staticmethod(
                AgentSitting._axes_needing_withdrawal.__func__
                if hasattr(AgentSitting._axes_needing_withdrawal, "__func__")
                else AgentSitting._axes_needing_withdrawal)

            def _run_trial(self, *args, **kwargs):
                raise AssertionError(
                    "retention spent a permit on a restore no permit expresses")

            def _v5_order(self):
                return ()   # not a v5 sitting: retention picks from this arm's T

        stub = _Stub()
        AgentSitting._retain(stub, 1, None)
        self.assertFalse(stub.retained["qualified"])
        self.assertEqual(["txAttenuationDb@ue1"], stub.retained["withdrawalOnlyAxes"])
        self.assertIn("no permit", stub.retained["detail"])

    def test_retention_can_be_switched_off_for_search_only_experiments(self) -> None:
        sitting = self.build(["I1: UE ueId=131 needs at least 4.5 Mbps downlink, "
                              "relaxable to 2.0", I2],
                             method=METHOD_DETERMINISTIC, budget_trials=3, retain_best=False)
        sitting.confirm()
        sitting.run()
        self.assertFalse(sitting.retained["qualified"])
        self.assertIn("retain_best=False", sitting.retained["detail"])
        self.assertIsNone(sitting.retention_runtime)

    def test_stopping_after_a_relaxed_success_is_one_stated_rule(self) -> None:
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER],
            "trajectory": [{"targetId": "T5", "controlId": "C0", "rationale": "hold"}]})
        sitting = self.build(resolver=resolver, role_models=models, budget_trials=3,
                             continue_after_relaxed_success=False)
        sitting.confirm()
        summary = sitting.run()
        self.assertEqual("OPERATOR_STOP", summary["termination"])
        self.assertEqual(1, summary["trials"])

    def test_the_deterministic_arm_runs_with_no_model_at_all(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=4)
        sitting.confirm()
        summary = sitting.run()
        self.assertEqual({"target": None, "control": None, "trajectory": None,
                          "monolith": None}, summary["roleModels"])
        self.assertTrue(summary["calls"])
        for call in summary["calls"]:
            self.assertEqual("deterministic", call["model"])
            if call["role"] != "intake":
                self.assertEqual("no model assigned to this role", call["fallbackReason"])
        self.assertEqual(0, sitting.resource_cost()["llmCalls"])
        self.assertEqual("T0_SUCCESS", summary["termination"])

    def test_the_catalog_runs_out_before_the_budget_does(self) -> None:
        # Three controls (the baseline and one move per UE) against a budget of
        # six: the deterministic trajectory has nothing untried left first.
        sitting = self.build(["I3: UE ueId=131 needs at least 9.0 Mbps downlink, "
                              "non-relaxable"],
                             method=METHOD_DETERMINISTIC, budget_trials=6)
        sitting.confirm()
        summary = sitting.run()
        self.assertEqual("CATALOG_EXHAUSTED", summary["termination"])
        self.assertFalse(summary["t0Success"])
        self.assertIsNone(summary["bestAttained"])

    def test_a_trial_that_attains_no_target_is_rolled_back_not_kept(self) -> None:
        # 2026-09-15 attempts 45/51/53/55: the joint Kernel predicates are
        # serving-cell membership only, so a control that met no target settled
        # live, held its axis and ended the search after one trial.
        sitting = self.build(["I3: UE ueId=131 needs at least 9.0 Mbps downlink, "
                              "non-relaxable"],
                             method=METHOD_DETERMINISTIC, budget_trials=6)
        sitting.confirm()
        sitting.run()
        trials = [trial for trial in sitting.grid.trials if trial.trial_index > 0]
        self.assertTrue(trials)
        for trial in trials:
            self.assertFalse(any(trial.success.values()))
            self.assertEqual("SEMANTIC_NON_SUCCESS", trial.kernel["stopReason"])
            self.assertEqual("SETTLED_NON_SUCCESS", trial.kernel["terminalState"])
            self.assertTrue(trial.rolled_back)

    def test_the_operator_can_stop_the_sitting(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=4)
        sitting.confirm()
        sitting.request_stop()
        summary = sitting.run()
        self.assertEqual("OPERATOR_STOP", summary["termination"])
        self.assertEqual(0, summary["trials"])


class TheObservationRules(AgentSittingFixture):
    """Contract v2 section 6: the window, the coverage and the validity."""

    def test_the_stated_hold_reaches_the_kernel_case(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=1,
                             settings={"observation": {
                                 "dlGoodputMbps": {"settleMs": 2000, "windowMs": 4000,
                                                   "validityMs": 60000}}})
        self.assertEqual(6000, sitting.rules.hold_ms())
        self.assertEqual(6000, sitting.joint.bundle.target.hold_ms)
        from gui.operator.sources.kernel_live import polling_plan
        self.assertEqual(6000, polling_plan(sitting.runtime.kernel).hold_ms)

    def test_an_unstated_rule_takes_the_deployments_own_hold(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=1)
        rules = sitting.rules.to_record()
        self.assertEqual(sitting.joint.bundle.target.hold_ms,
                         rules["dlGoodputMbps"]["settleMs"]
                         + rules["dlGoodputMbps"]["windowMs"])
        self.assertEqual("mean", rules["dlGoodputMbps"]["statistic"])
        self.assertEqual("last", rules["servingCell"]["statistic"])

    def test_the_rules_are_in_the_episode_the_metrics_read(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=1)
        sitting.confirm()
        sitting.run()
        record = sitting.episode().to_record()
        self.assertIn("measurementRules", record)
        self.assertIn("validityMs", record["measurementRules"]["dlGoodputMbps"])
        self.assertEqual(record["intake"]["rounds"], 0)

    def test_a_thin_window_is_unknown_rather_than_an_average(self) -> None:
        # An observer that answers one poll in five: the KPI is measured, but
        # never often enough to cover the stated window, so nothing is
        # established and the whole column is UNKNOWN.  A thin window can never
        # prove a success and is never quietly averaged into one either.
        class _Sparse:
            def __init__(self, inner):
                self.inner, self.calls = inner, 0

            def sample(self):
                self.calls += 1
                return dict(self.inner.sample()) if self.calls % 5 == 1 else {}

        from tools.hfconsole.agent_env import EmulatedKpiObserver
        ran = EmulatedRan(ues={"131": HOME_NCI, "132": HOME_NCI},
                          cells={HOME_NCI: 5.0, TARGET_NCI: 5.0},
                          offered_load_mbps={"131": 4.0, "132": 4.0}, noise_sigma=0.0)
        sitting = self.build_request(
            AgentRequest(sentences=(I1, I2), method=METHOD_DETERMINISTIC,
                         budget_trials=1,
                         settings={"observation": {
                             "dlGoodputMbps": {"settleMs": 1000, "windowMs": 8000,
                                               "validityMs": 60000},
                             "servingCell": {"settleMs": 1000, "windowMs": 8000,
                                             "validityMs": 60000}}}),
            ran=ran, kpi_observer=_Sparse(EmulatedKpiObserver(ran)))
        sitting.confirm()
        sitting.run()
        trial = sitting.grid.trials[0]
        self.assertTrue(trial.window["unknownKpis"])
        self.assertNotIn("dlGoodputMbps@131", trial.kpis)
        self.assertEqual("UNKNOWN", trial.cell("T0"))
        self.assertLess(trial.window["coverage"]["dlGoodputMbps@131"], 0.5)


class TheTimingOfADecision(AgentSittingFixture):
    """Contract v2 sections 6 and 7: nobody is cut off, and time is charged."""

    def test_a_late_answer_is_recorded_as_stale_but_still_executed(self) -> None:
        # Owner scenario 2026-09-22 section 3: age alone no longer discards an
        # answer.  The second decision takes 20 virtual seconds -- longer than
        # the serving cell's 10 s validity -- so the numbers it was shown
        # expired while it was thinking.  The answer is still applied; success
        # is judged by the observation taken AFTER it is applied.  The record
        # keeps ``stale_at_arrival`` so the ageing is still visible.
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER],
            "trajectory": [PAIR,
                           # T1: the fallback T is the mandatory set under v3.1.
                           slow({"controlId": "C2", "targetId": "T1",
                                 "rationale": "slow"}, 20000.0)]})
        sitting = self.build(["I1: UE ueId=131 needs at least 4.5 Mbps downlink, "
                              "relaxable to 2.0", I2],
                             resolver=resolver, role_models=models, budget_trials=2)
        sitting.confirm()
        sitting.run()
        # Nothing is re-observed and nothing is asked twice: two questions, two
        # trials, and the late one carries the mark rather than being thrown away.
        self.assertEqual([], sitting.reobservations)
        stale = [call for call in sitting.agents.calls if call.stale_at_arrival]
        self.assertEqual(1, len(stale))
        self.assertEqual(2, len(resolver.prompts_for("scripted:trajectory")))
        self.assertEqual(2, len(sitting.grid.trials))
        self.assertNotIn("re-ask", [event["kind"] for event in sitting.non_trial_events])

    def test_a_late_answer_is_not_re_asked_even_twice_running(self) -> None:
        # The old contract asked again after every stale answer, so two slow
        # answers cost two extra calls and two re-measurements.  Under the owner
        # scenario of 2026-09-22 neither is spent: both late answers are applied
        # and the episode runs its budget out on trials instead.
        slow_pair = slow({"controlId": "C2", "targetId": "T5", "rationale": "slow"},
                         20000.0)
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER],
            "trajectory": [PAIR, slow_pair, slow_pair]})
        sitting = self.build(["I1: UE ueId=131 needs at least 4.5 Mbps downlink, "
                              "relaxable to 2.0", I2],
                             resolver=resolver, role_models=models, budget_trials=2,
                             deadline_ms=600000)
        sitting.confirm()
        sitting.run()
        self.assertEqual([], sitting.reobservations)
        self.assertEqual(2, len(resolver.prompts_for("scripted:trajectory")))
        decided = sitting.grid.trials[-1].decision
        self.assertNotEqual("deterministic", decided["model"])
        self.assertIsNone(decided.get("fallbackReason"))
        self.assertEqual(0, [event["kind"] for event in
                             sitting.non_trial_events].count("re-ask"))

    def test_a_formation_that_never_answers_leaves_a_countable_record(self) -> None:
        # 2026-09-19: nothing stands in for a refused T, the sitting is refused,
        # and its episode file says FORMATION_FAILURE with every call and reason.
        from tools.liveconsole.agent import FORMATION_FAILURE, FormationFailed
        resolver, models = self.scripted({"target": ["not JSON", "still not JSON"],
                                          "control": [C_ANSWER]})
        with self.assertRaises(FormationFailed):
            self.build(resolver=resolver, role_models=models, budget_trials=2)
        episodes = list(Path(self.tmp).rglob("AGENT-*-episode.json"))
        self.assertEqual(1, len(episodes))
        document = json.loads(episodes[0].read_text(encoding="utf-8"))
        self.assertEqual(FORMATION_FAILURE, document["termination"]["reason"])
        self.assertEqual(METHOD_THREE_AGENT, document["method"])
        self.assertEqual(1, document["formation"]["reAsks"])
        self.assertTrue(any(call["role"] == "target" for call in document["calls"]))

    def test_an_answer_refused_every_time_is_asked_again_until_the_deadline(self) -> None:
        wrong = {"controlId": "C99", "targetId": "T1", "rationale": "not in C"}
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER],
            "trajectory": [PAIR] + [wrong] * 2000})
        sitting = self.build(["I1: UE ueId=131 needs at least 4.5 Mbps downlink, "
                              "relaxable to 2.0", I2],
                             resolver=resolver, role_models=models, budget_trials=4,
                             deadline_ms=120000)
        sitting.confirm()
        summary = sitting.run()
        self.assertEqual("DEADLINE", summary["termination"])
        self.assertGreater(len(resolver.prompts_for("scripted:trajectory")), 3)
        for call in sitting.agents.calls:
            if call.phase == "intake":
                continue            # the intake checklist is code by design, not a choice
            self.assertNotEqual("deterministic", call.model, (call.role, call.phase))
            self.assertIsNone(call.fallback_reason)
        self.assertTrue(any(event["kind"] == "re-ask" and "C99" in event["detail"]
                            for event in sitting.non_trial_events))

    def test_the_role_default_generation_budget_is_recorded_on_every_call(self) -> None:
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER], "trajectory": [PAIR]})
        sitting = self.build(resolver=resolver, role_models=models, budget_trials=2)
        sitting.confirm()
        sitting.run()
        target_call = [call for call in sitting.agents.calls if call.role == "target"][0]
        self.assertEqual(GenerationOptions.for_role("target").thinking_budget_tokens,
                         target_call.options["thinkingBudgetTokens"])
        self.assertEqual("default", target_call.options["source"])
        self.assertEqual(
            GenerationOptions.for_role("target").thinking_budget_tokens,
            resolver.options_for("scripted:target")[0]["thinkingBudgetTokens"])

    def test_the_operators_own_budget_wins_over_the_default(self) -> None:
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER], "trajectory": [PAIR]})
        sitting = self.build(resolver=resolver, role_models=models, budget_trials=1,
                             settings={"generation": {
                                 "target": {"maxTokens": 111,
                                            "thinkingBudgetTokens": 222}}})
        target_call = [call for call in sitting.agents.calls if call.role == "target"][0]
        self.assertEqual(222, target_call.options["thinkingBudgetTokens"])
        self.assertEqual(111, target_call.options["maxTokens"])
        self.assertEqual("operator", target_call.options["source"])

    def test_a_latency_calibration_changes_the_budget_that_is_asked_for(self) -> None:
        calibration = Path(self.tmp) / "agent-latency-calibration.json"
        calibration.write_text(json.dumps({
            "schemaVersion": "agent-latency-calibration/1.0.0",
            "models": {"scripted:target": {"target": [
                {"thinkingBudgetTokens": 700, "p50LatencyMs": 500,
                 "p95LatencyMs": 1000},
                {"thinkingBudgetTokens": 90000, "p50LatencyMs": 300000,
                 "p95LatencyMs": 600000}]}}}), encoding="utf-8")
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER], "trajectory": [PAIR]})
        sitting = self.build(resolver=resolver, role_models=models, budget_trials=1,
                             settings={"latencyCalibrationPath": str(calibration)})
        target_call = [call for call in sitting.agents.calls if call.role == "target"][0]
        self.assertEqual(700, target_call.options["thinkingBudgetTokens"])
        self.assertEqual("calibration", target_call.options["source"])


class TheMonolithArms(AgentSittingFixture):

    BASIC = {
        "instructions": [{"functionId": "steer", "scope": "ue@131",
                          "policy": {"servingCell": TARGET_NCI}}],
        "requirements": [{"intentId": "I1", "reqId": "I1.r1", "owner": "I1",
                          "scope": "ue@131", "kpi": "dlGoodputMbps", "op": ">=",
                          "value": 3.0, "unit": "Mbps"},
                         {"intentId": "I2", "reqId": "I2.r1", "owner": "I2",
                          "scope": "ue@132", "kpi": "dlGoodputMbps", "op": ">=",
                          "value": 1.5, "unit": "Mbps"}],
        "rationale": "move 131 to the empty cell",
    }

    def test_the_basic_monolith_is_never_shown_our_targets_or_candidates(self) -> None:
        resolver, models = self.scripted({"monolith": [self.BASIC]},
                                         method=METHOD_BASIC_MONOLITH)
        sitting = self.build(resolver=resolver, role_models=models,
                             method=METHOD_BASIC_MONOLITH, budget_trials=3)
        sitting.confirm()
        summary = sitting.run()
        self.assertEqual("T0_SUCCESS", summary["termination"])
        prompt = resolver.prompts_for("scripted:monolith")[0]
        # 2026-09-17 오너 드롭(Runtime alignment 2·4): `input.observed_best` 는 이제
        # **와야 한다** -- 온라인 선택기 둘은 받는데 이 팔만 못 받으면 비교가 불공정하다.
        # 금지로 남는 것은 우리 쪽 **준비물과 자리번호**다: 계약 · 후보 집합 · 후보별 예측 ·
        # 후보 id · 대안 목록.  형제 단언이 `test_coordination_agents.py` 에 있다.
        for forbidden in ("input.target_contract", "input.control_candidates",
                          "predictedTarget", "controlId", "alternatives"):
            self.assertNotIn(forbidden, prompt, f"{forbidden} reached the basic monolith")
        self.assertIn("input.observed_best", prompt,
                      "the best result must reach this arm (owner drop, alignment 4)")
        self.assertEqual([], budget_terms_in(prompt))
        # It does get the same raw material as everybody else.
        for shared in ("input.intents", "input.authorization",
                       "input.function_catalog", "input.compatibility",
                       "input.network_state", "input.effect_evidence",
                       "input.observations"):
            self.assertIn(shared, prompt)
        # Its instructions are still translated onto a candidate of the frozen
        # catalog, and judged by exactly the same predicates.
        trial = sitting.grid.trials[0]
        self.assertIn(trial.control_id, sitting.catalog_of_control)
        self.assertEqual(sitting.catalog_of_control[trial.control_id],
                         trial.catalog_candidate_id)
        self.assertEqual(TARGET_NCI, trial.configuration["servingCell@131"])

    def test_the_basic_monolith_sees_its_own_observations_and_no_target_id(self) -> None:
        answers = [
            {"instructions": [{"functionId": "steer", "scope": "ue@132",
                               "policy": {"servingCell": TARGET_NCI}}],
             "requirements": [{"reqId": "I1.r1", "value": 4.5}],
             "rationale": "move 132 out of the way"},
            {"instructions": [{"functionId": "steer", "scope": "ue@131",
                               "policy": {"servingCell": TARGET_NCI}}],
             "requirements": [{"reqId": "I1.r1", "value": 4.5}],
             "rationale": "move 131 instead"}]
        resolver, models = self.scripted({"monolith": answers},
                                         method=METHOD_BASIC_MONOLITH)
        sitting = self.build(["I1: UE ueId=131 needs at least 4.5 Mbps downlink, "
                              "relaxable to 2.0", I2],
                             resolver=resolver, role_models=models,
                             method=METHOD_BASIC_MONOLITH, budget_trials=2)
        sitting.confirm()
        summary = sitting.run()
        self.assertEqual("BUDGET_EXHAUSTED", summary["termination"])
        self.assertEqual(2, summary["trials"])
        second = resolver.prompts_for("scripted:monolith")[1]
        # It is shown what its own last configuration measured, and nothing that
        # names one of our targets or columns.
        self.assertIn("dlGoodputMbps@131", second)
        self.assertIn("validUntil", second)
        for forbidden in ("targetId", "controlId", "verdicts",
                          "input.control_candidates"):
            self.assertNotIn(forbidden, second)

    def test_an_instruction_outside_the_catalog_is_asked_again_never_replaced(self) -> None:
        answer = {"instructions": [{"functionId": "slice-quota", "scope": "ue@131",
                                    "policy": {"quota": "40"}}],
                  "requirements": [{"reqId": "I1.r1", "value": 3.0}],
                  "rationale": "not one of ours"}
        resolver, models = self.scripted({"monolith": [answer]},
                                         method=METHOD_BASIC_MONOLITH)
        sitting = self.build(resolver=resolver, role_models=models,
                             method=METHOD_BASIC_MONOLITH, budget_trials=1)
        sitting.confirm()
        summary = sitting.run()
        call = [item for item in sitting.agents.calls if item.role == "monolith"][0]
        self.assertFalse(call.accepted)
        self.assertEqual(1, call.repair_retries)          # no B: the single repair
        self.assertIsNone(call.fallback_reason)
        self.assertTrue(all(t.trial_index == 0 for t in sitting.grid.trials))  # no live trial
        self.assertEqual("PROPOSAL_FAILURE", summary["termination"])

    def test_the_internal_monolith_forms_then_selects_on_one_model(self) -> None:
        formation = {**T_ANSWER, "candidates": C_ANSWER["candidates"],
                     "rationale": "T and C in one call"}
        resolver, models = self.scripted({"monolith": [formation, PAIR]},
                                         method=METHOD_INTERNAL_MONOLITH)
        sitting = self.build(resolver=resolver, role_models=models,
                             method=METHOD_INTERNAL_MONOLITH, budget_trials=3)
        self.assertEqual(("T0", "T1", "T2", "T3", "T4", "T5", "T6"),
                         sitting.contract.target_ids)
        self.assertEqual(("C0", "C1", "C2"), sitting.controls.control_ids)
        sitting.confirm()
        summary = sitting.run()
        self.assertEqual("T0_SUCCESS", summary["termination"])
        phases = [call["phase"] for call in summary["calls"]]
        self.assertEqual(["intake", "formation", "selection"], phases)
        self.assertEqual(["intake", "monolith", "monolith"],
                         [call["role"] for call in summary["calls"]])


class TheOfflineMockModel(AgentSittingFixture):
    """``mock:agent`` -- a stand-in for a model, so the whole path runs offline."""

    def sitting(self, sentences, **kwargs):
        models = {role: "mock:agent"
                  for role in ("target", "control", "trajectory", "monolith")}
        return self.build(sentences, role_models=models,
                          method=METHOD_THREE_AGENT, **kwargs)

    def test_it_reaches_the_original_target_on_the_reachable_case(self) -> None:
        sitting = self.sitting([I1, I2], budget_trials=4)
        sitting.confirm()
        summary = sitting.run()
        self.assertEqual("T0_SUCCESS", summary["termination"])
        accepted = [call for call in sitting.agents.calls
                    if call.role != "intake" and call.accepted]
        self.assertTrue(accepted, "mock:agent answered nothing the executor accepted")
        for call in accepted:
            self.assertEqual("mock:agent", call.model)

    def test_it_settles_for_an_authorized_concession_when_t0_is_out_of_reach(self) -> None:
        sitting = self.sitting(["I1: UE ueId=131 needs at least 4.5 Mbps downlink, "
                                "relaxable to 2.0", I2], budget_trials=4)
        sitting.confirm()
        summary = sitting.run()
        self.assertFalse(summary["t0Success"])
        best = summary["bestAttained"]
        self.assertIsNotNone(best, summary["grid"])
        self.assertNotEqual("T0", best["targetId"])
        # 2026-09-23: `bestAttained.targetId` 는 이제 **Ω** 의 id 다(결정 §2).
        # 선택된 T 는 `from_compact` 에서 T1..Tn 으로 재번호되므로 그쪽에서
        # 찾으면 T0 말고는 None 이 나온다 -- 코드에서 막은 그 함정이다.
        self.assertGreater(
            sitting.evaluation_contract.target(best["targetId"]).cost, 0.0)


class TheServiceHorizon(AgentSittingFixture):
    """B stops the search; H keeps the service under observation."""

    def test_the_service_is_watched_after_the_search_has_stopped(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=1,
                             horizon_ms=60000)
        sitting.confirm()
        sitting.run()
        self.assertEqual(1, len(sitting.grid.trials), "H is not a licence for more trials")
        watched = sitting.preflight["horizonObservation"]
        self.assertEqual(60000, watched["horizonMs"])
        self.assertGreater(watched["samples"], 0)
        self.assertGreaterEqual(watched["elapsedMs"], 60000)
        last = sitting.service_trace[-1]
        self.assertTrue(last["kpis"], "the last sample carries the service, not an empty row")

    def test_a_horizon_shorter_than_the_deadline_is_refused(self) -> None:
        with self.assertRaises(LiveConsoleError) as raised:
            AgentRequest(sentences=(I1,), deadline_ms=60000, horizon_ms=30000)
        self.assertIn("horizon", str(raised.exception).lower())

    def test_no_horizon_means_no_post_search_observation(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=1)
        sitting.confirm()
        sitting.run()
        self.assertNotIn("horizonObservation", sitting.preflight)


class TheEpisodeRecord(AgentSittingFixture):

    def test_the_record_carries_every_key_the_metrics_read(self) -> None:
        broken = {"t0": {"targetId": "T0", "requirements": {"I1.r1": 9.9, "I2.r1": 1.5}},
                  "levels": {}, "rationale": "T0 moved off its original"}
        resolver, models = self.scripted({
            "target": [broken, T_ANSWER], "control": [C_ANSWER], "trajectory": [PAIR]})
        sitting = self.build(resolver=resolver, role_models=models, budget_trials=4,
                             condition={"name": "contention-boundary",
                                        "offeredLoadMbps": {"131": 4, "132": 4}},
                             block=1, repetition=2)
        sitting.confirm()
        sitting.run()
        record = sitting.episode_record()
        # The shared EpisodeRecord is still 1.1.0; what the executor writes
        # carries contract v4's keys, which the metrics read under 1.3.0.
        self.assertEqual("agent-episode/1.3.0", record["schemaVersion"])
        self.assertEqual("agent-episode/1.1.0",
                         sitting.episode().to_record()["schemaVersion"])
        for key in ("episodeId", "method", "sessionMode", "condition", "block",
                    "repetition", "models", "intents", "T", "C", "budget", "timing",
                    "calls", "trials", "serviceTrace", "bestAttained", "retained",
                    "firstSuccess", "t0Success", "termination", "resourceCost",
                    "measurementRules", "intake",
                    # contract v4: the counting, the boundaries and the board.
                    "boundaries", "nonTrialEvents", "prepared"):
            self.assertIn(key, record)
        self.assertEqual({"injected", "tHash", "cHash"}, set(record["prepared"]))
        self.assertFalse(record["prepared"]["injected"])
        self.assertFalse(record["preparedInjected"])
        self.assertEqual("three-agent", record["method"])
        self.assertEqual("MOCK", record["sessionMode"])
        self.assertEqual(1, record["block"])
        self.assertEqual(2, record["repetition"])
        self.assertEqual("contention-boundary", record["condition"]["name"])
        self.assertTrue(record["t0Success"])
        self.assertEqual("T0_SUCCESS", record["termination"]["reason"])
        self.assertEqual({"trialsK", "deadlineBMs", "horizonHMs", "qualityThresholdsA",
                          "binDeltaMs"}, set(record["budget"]))
        self.assertEqual({"prepStart", "prepEnd", "prepMs", "timingMode", "t0", "end"},
                         set(record["timing"]))
        self.assertTrue(record["serviceTrace"])
        self.assertIn("kpis", record["serviceTrace"][0])
        # T now carries what each target conceded and what that cost.
        self.assertIn("cost", record["T"]["t0"])
        self.assertIn("weights", record["T"])
        # ... and every call says which phase it was and what it was asked to spend.
        for call in record["calls"]:
            self.assertIn(call["phase"],
                          ("intake", "formation", "clarification", "selection",
                           "retention"))
            self.assertIn("options", call)
            self.assertIn("staleAtArrival", call)
        trial = record["trials"][0]
        for key in ("trialIndex", "proposedTargetId", "controlId", "configuration",
                    "catalogCandidateId", "decisionLatencyMs", "appliedAt", "window",
                    "kpis", "verdicts", "success", "kernel", "rolledBack", "decision",
                    "counted"):
            self.assertIn(key, trial)
        self.assertTrue(trial["window"]["valid"])
        self.assertIn("validUntil", trial["window"])
        self.assertIn("coverage", trial["window"])

    def test_the_resource_cost_counts_the_repair_retry_too(self) -> None:
        broken = "not json at all"
        resolver, models = self.scripted({
            "target": [broken, T_ANSWER], "control": [C_ANSWER], "trajectory": [PAIR]})
        sitting = self.build(resolver=resolver, role_models=models, budget_trials=4)
        sitting.confirm()
        sitting.run()
        target_call = [call for call in sitting.agents.calls if call.role == "target"][0]
        self.assertEqual(1, target_call.repair_retries)
        self.assertTrue(target_call.accepted)
        cost = sitting.resource_cost()
        # target 1 + 1 repair, control 1, trajectory 1 = four model invocations;
        # the intake is not one of them.
        self.assertEqual(4, cost["llmCalls"])
        self.assertEqual(4, len(resolver.calls))
        self.assertGreater(cost["inputTokens"], 0)
        self.assertGreater(cost["outputTokens"], 0)
        self.assertEqual(1, len(cost["decisionLatenciesMs"]))

    def test_the_resource_cost_says_whether_its_token_totals_are_complete(self) -> None:
        """The sums are lower bounds; the aggregate has to admit when it is one.

        ``CallRecord`` adds a generation the provider reported no count for as
        0, so a total that silently dropped one reads exactly like a measured
        total.  ``tokensComplete`` is the same ``all``-fold one level up from
        the call row: false once ANY call feeding the sums was incomplete.
        """
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER], "trajectory": [PAIR]})
        sitting = self.build(resolver=resolver, role_models=models, budget_trials=4)
        sitting.confirm()
        sitting.run()
        complete = sitting.resource_cost()
        self.assertTrue(complete["tokensComplete"])
        sitting.agents.calls[0].tokens_complete = False
        incomplete = sitting.resource_cost()
        self.assertFalse(incomplete["tokensComplete"])
        # Carried beside the sums, never derived from them: the totals are
        # identical and only the flag tells the two readings apart.
        self.assertEqual(complete["inputTokens"], incomplete["inputTokens"])
        self.assertEqual(complete["outputTokens"], incomplete["outputTokens"])

    def test_the_exact_requests_are_written_beside_the_episode(self) -> None:
        """What occupied the input tokens has to be answerable from disk.

        ``CallRecord`` keeps ``prompt``/``system_prompt``/``raw`` in memory and
        out of the episode record, so nothing on disk could say what a 49 165
        token Control request contained -- and a reconstruction from saved
        inputs is not the captured request.  They are written as one file per
        call, with only the path, size and digest in the record.
        """
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER],
            "trajectory": [PAIR]})
        sitting = self.build(resolver=resolver, role_models=models,
                             budget_trials=1)
        sitting.confirm()
        sitting.run()
        written = write_agent_evidence(sitting)
        document = json.loads(Path(written["episode"]).read_text(encoding="utf-8"))
        artifacts = document["execution"]["promptArtifacts"]
        served = [c for c in sitting.agents.calls if c.prompt]
        self.assertEqual(len(served), len(artifacts["calls"]))

        directory = Path(written["episode"]).parent / artifacts["directory"]
        for entry in artifacts["calls"]:
            with self.subTest(role=entry["role"]):
                self.assertIn("request", entry)
                path = directory / entry["request"]["path"]
                text = path.read_text(encoding="utf-8")
                self.assertEqual(entry["request"]["bytes"],
                                 len(text.encode("utf-8")))
                self.assertEqual(
                    entry["request"]["sha256"],
                    hashlib.sha256(text.encode("utf-8")).hexdigest())
                self.assertTrue(text.startswith("INPUTS:\n"))

        roles = [entry["role"] for entry in artifacts["calls"]]
        # Target 과 Control 은 **동시에** 돈다(2026-09-20 오너 지시).  둘 중 어느 쪽이
        # 먼저 기록될지는 지연이 정하므로 그 순서를 주장하면 안 된다 -- 주장할 수 있는
        # 것은 셋이 다 있고 선택 호출이 구성 뒤라는 것뿐이다.
        self.assertEqual({"target", "control", "trajectory"}, set(roles))
        self.assertEqual("trajectory", roles[-1])
        self.assertEqual(3, len(roles))

    def test_each_call_records_which_inputs_it_carried(self) -> None:
        """The record has to say who saw ``T`` and ``C``, not only who built them.

        Every episode record keeps ``T`` and ``C`` at top level, for every arm,
        because they are the comparison grid the executor judges on.  The basic
        monolith never receives either -- but its record's ``T``/``C`` only say
        ``provenance.model = "deterministic"``, which names the builder, not the
        reader.  ``inputKeys`` names the reader.
        """
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER], "trajectory": [PAIR]})
        sitting = self.build(resolver=resolver, role_models=models,
                             budget_trials=1)
        sitting.confirm()
        sitting.run()
        by_role: Dict[str, List[str]] = {}
        for call in sitting.agents.calls:
            if call.input_keys:
                by_role.setdefault(call.role, list(call.input_keys))

        # 2026-09-20 오너 지시: Control 은 완성된 T 를 **기다리지 않는다** -- 두 구성
        # 호출이 동시에 시작하므로 Control 의 입력에 `input.target_contract` 는 없다
        # (`SINGLE_CALL.md` 변경 이력).  Trajectory 는 둘 다 받는다.
        self.assertNotIn("input.target_contract", by_role["control"])
        self.assertNotIn("input.control_candidates", by_role["control"])
        self.assertIn("input.control_candidates", by_role["trajectory"])
        self.assertIn("input.target_contract", by_role["trajectory"])
        self.assertNotIn("input.target_contract", by_role["target"])

    def test_the_basic_monolith_record_shows_it_never_saw_t_or_c(self) -> None:
        """The arm's whole definition, visible in the artefact rather than asserted."""
        sitting = self.build(method=METHOD_BASIC_MONOLITH, budget_trials=1)
        sitting.confirm()
        sitting.run()
        written = write_agent_evidence(sitting)
        document = json.loads(Path(written["episode"]).read_text(encoding="utf-8"))
        # The grid is still recorded -- the executor judges on it.
        self.assertIn("T", document)
        self.assertIn("C", document)
        for call in document["calls"]:
            if call["role"] != "monolith":
                continue
            with self.subTest(call=call["startedAt"]):
                self.assertNotIn("input.target_contract", call["inputKeys"])
                self.assertNotIn("input.control_candidates", call["inputKeys"])
                self.assertIn("input.function_catalog", call["inputKeys"])

    def test_the_episode_record_does_not_carry_the_prompts_themselves(self) -> None:
        """The record stays small; the prompts live next to it."""
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER], "trajectory": [PAIR]})
        sitting = self.build(resolver=resolver, role_models=models,
                             budget_trials=1)
        sitting.confirm()
        sitting.run()
        written = write_agent_evidence(sitting)
        document = json.loads(Path(written["episode"]).read_text(encoding="utf-8"))
        for call in document["calls"]:
            self.assertNotIn("prompt", call)
            self.assertNotIn("systemPrompt", call)
            self.assertNotIn("raw", call)

    def test_the_evidence_is_the_episode_beside_the_kernel_stream(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=3)
        sitting.confirm()
        sitting.run()
        written = write_agent_evidence(sitting)
        self.assertTrue(written["episode"].endswith("-episode.json"))
        document = json.loads(Path(written["episode"]).read_text(encoding="utf-8"))
        self.assertEqual("agent-episode/1.3.0", document["schemaVersion"])
        self.assertEqual(sitting.case_id, document["execution"]["caseId"])
        self.assertEqual(sitting.confirmation_hash,
                         document["execution"]["confirmationHash"])
        self.assertEqual(len(document["trials"]),
                         len(document["execution"]["kernelTrials"]))
        # The predictor is versioned in the episode: what it was, and what the
        # trials taught it.
        self.assertTrue(document["execution"]["predictor"]["versions"])
        self.assertEqual("calibrated",
                         document["execution"]["predictor"]["final"]["calibration"]["state"])
        events = Path(written["events"]).read_text(encoding="utf-8").splitlines()
        self.assertGreater(len(events), 20)
        self.assertEqual(len(document["trials"]),
                         sum(1 for line in events if '"TrialOpened"' in line))


class TheThreeUeScenario(AgentSittingFixture):

    def test_three_owners_three_axes_one_case(self) -> None:
        ran = three_ue_topology(condition="contention-boundary", noise_sigma=0.0)
        sitting = self.build(
            [I1, I2, "I3: UE ueId=133 needs at least 3.0 Mbps downlink, relaxable to 2.0"],
            ran=ran, method=METHOD_DETERMINISTIC, budget_trials=6,
            # One rung per UE: three UEs over two cells with the contract's
            # own ladders is 32768 candidates, which is over the ceiling and
            # would not freeze in this file's lifetime either way.
            caps={"131": (12,), "132": (12,), "133": (12,)})
        self.assertEqual(("I1", "I2", "I3"), tuple(i.intent_id for i in sitting.intents))
        self.assertIn("dlPrbCap@132", sitting.action_space)
        self.assertIn("ue-dl-prb-cap", sitting.catalog.function_ids)
        sitting.confirm()
        summary = sitting.run()
        self.assertIn(summary["termination"], {"T0_SUCCESS", "BUDGET_EXHAUSTED",
                                               "CATALOG_EXHAUSTED"})
        self.assertTrue(sitting.grid.trials)
        # Nothing here counts to three: the grid is |Omega| x |C| whatever they are.
        # 2026-09-23 결정 §1: 판정 도메인은 **형성된 T 가 아니라 공통 Omega** 다 --
        # 세 방식이 같은 행을 놓고 채점되지 않으면 달성 집합을 비교할 수 없다.
        # 형성된 T 는 그 Omega 의 부분집합이어야 한다(이름이 같은 자리번호가 아니라
        # 같은 벡터를 가리키는지는 `test_evaluation_domain_is_common` 가 본다).
        self.assertEqual(set(sitting.evaluation_contract.target_ids),
                         set(summary["grid"]["cells"]))
        self.assertLessEqual(set(sitting.contract.target_ids),
                             set(sitting.evaluation_contract.target_ids))


class TheExposedAxes(unittest.TestCase):
    """Contract v3 section 3: what a sitting declares, before it freezes it.

    Every assertion here is about the *declaration*, so nothing is composed
    and nothing is frozen -- which is what makes it affordable to state the
    default exposure at its real size (1024 candidates over two UEs) instead
    of at the one-rung size the sitting fixtures above have to use.
    """

    CELLS = (int(HOME_NCI), int(TARGET_NCI))

    def axes(self, request: AgentRequest, *, ue_ids: Sequence[str] = ("131", "132"),
             cells: Sequence[int] = (), slices: Sequence[str] = ()):
        cells = tuple(cells or self.CELLS)
        rows = parse_agent_intents(request, families=live_capable_families())
        identities = {ue: _EmulatedIdentity(ue, str(cells[0])) for ue in ue_ids}
        return _axis_specs(rows, identities, request, list(cells), slices)

    def space(self, axes) -> Dict[str, Sequence[str]]:
        return {spec.axis: tuple(spec.values) for spec in axes.declared}

    def test_the_cap_and_the_weight_are_on_every_intent_ue_by_default(self) -> None:
        space = self.space(self.axes(AgentRequest(sentences=(I1, I2))))
        self.assertEqual(
            {"servingCell@131", "servingCell@132", "dlPrbCap@131", "dlPrbCap@132",
             "pfWeight@131", "pfWeight@132"}, set(space))
        # The contract's ladders, canonicalised by the declaration that owns
        # them: the uncapped sentinel and the neutral weight are the
        # baselines, so they lead and are never candidates twice.
        for ue in ("131", "132"):
            self.assertEqual(("0", "6", "12", "18"), space[f"dlPrbCap@{ue}"])
            self.assertEqual(("1.0", "0.5", "2.0", "4.0"), space[f"pfWeight@{ue}"])
        self.assertEqual({0, 18, 12, 6}, set(DEFAULT_CAP_LADDER))
        self.assertEqual({0.5, 1.0, 2.0, 4.0}, set(DEFAULT_PF_LADDER))

    def test_a_flag_changes_one_scopes_ladder_and_not_who_is_exposed(self) -> None:
        space = self.space(self.axes(AgentRequest(
            sentences=(I1, I2), caps={"132": (6,)})))
        self.assertEqual(("0", "6"), space["dlPrbCap@132"])
        self.assertEqual(("0", "6", "12", "18"), space["dlPrbCap@131"])

    def test_all_exposes_every_cell_and_every_named_slice(self) -> None:
        axes = self.axes(AgentRequest(sentences=(I1, I2), axes=("all",)),
                         slices=("1",))
        space = self.space(axes)
        for cell in self.CELLS:
            self.assertEqual(DEFAULT_MCS_LADDER, space[f"dlMcsBounds@{cell}"])
            self.assertEqual(DEFAULT_ATTENUATION_LADDER, space[f"txAttenuationDb@{cell}"])
        self.assertEqual(DEFAULT_SLICE_QUOTA_LADDER, space["slicePrbQuota@1"])
        # The contract's own arithmetic: 4 x 16 x 16 x 9 x 9 x 3.
        product = 1
        for values in space.values():
            product *= len(values)
        self.assertEqual(248832, product)

    def test_a_stated_attenuation_ladder_exposes_only_the_stated_cells(self) -> None:
        # v4.7 cell power (owner 2026-09-25): naming gnb2's ladder must not hand gnb1 the
        # default 0/6/12 dB -- gnb1 only holds its UEs in a 2 dB window.
        space = self.space(self.axes(AgentRequest(
            sentences=(I1, I2), axes=("servingCell", "txAttenuationDb"),
            tx_attenuations={TARGET_NCI: ("13.0", "16.0")})))
        self.assertIn(f"txAttenuationDb@{TARGET_NCI}", space)
        self.assertNotIn(f"txAttenuationDb@{HOME_NCI}", space)
        # Nothing stated: every advertised cell, as before.
        space = self.space(self.axes(AgentRequest(
            sentences=(I1, I2), axes=("servingCell", "txAttenuationDb"))))
        for cell in self.CELLS:
            self.assertIn(f"txAttenuationDb@{cell}", space)

    def test_the_cell_and_slice_axes_are_scoped_to_a_cell_and_a_slice(self) -> None:
        axes = self.axes(AgentRequest(sentences=(I1, I2), axes=("all",)),
                         slices=("1",))
        scopes = {spec.axis: spec.scope for spec in axes.declared}
        self.assertEqual(f"cell@{HOME_NCI}", scopes[f"dlMcsBounds@{HOME_NCI}"])
        self.assertEqual(f"cell@{HOME_NCI}", scopes[f"txAttenuationDb@{HOME_NCI}"])
        self.assertEqual("slice@1", scopes["slicePrbQuota@1"])
        self.assertEqual("ue@131", scopes["dlPrbCap@131"])

    def test_the_function_catalog_names_the_new_kinds_at_their_own_scope(self) -> None:
        axes = self.axes(AgentRequest(sentences=(I1, I2), axes=("all",)),
                         slices=("1",))
        catalog = {spec.function_id: spec for spec in _function_catalog(axes.declared)}
        self.assertIn("cell-dl-mcs-bounds", catalog)
        self.assertIn("cell-tx-attenuation", catalog)
        self.assertIn("slice-prb-quota", catalog)
        self.assertEqual((f"cell@{HOME_NCI}", f"cell@{TARGET_NCI}"),
                         catalog["cell-dl-mcs-bounds"].scopes)
        self.assertEqual(("slice@1",), catalog["slice-prb-quota"].scopes)
        self.assertEqual("dlMcsBounds@<cell>", catalog["cell-dl-mcs-bounds"].axis)
        self.assertEqual("our_rc_xapp", catalog["slice-prb-quota"].xapp)

    def test_a_slice_kind_with_no_snssai_is_refused_by_name(self) -> None:
        with self.assertRaises(LiveConsoleError) as raised:
            self.axes(AgentRequest(sentences=(I1,),
                                   axes=("servingCell", "slicePrbQuota")),
                      ue_ids=("131",))
        self.assertIn("slicePrbQuota", str(raised.exception))
        self.assertIn("--slice-axis", str(raised.exception))

    def test_an_unknown_kind_and_a_dropped_steering_axis_are_refused(self) -> None:
        with self.assertRaises(LiveConsoleError) as unknown:
            AgentRequest(sentences=(I1,), axes=("servingCell", "mcs"))
        self.assertIn("'mcs'", str(unknown.exception))
        self.assertIn("dlMcsBounds", str(unknown.exception))
        with self.assertRaises(LiveConsoleError) as dropped:
            AgentRequest(sentences=(I1,), axes=("dlPrbCap",))
        self.assertIn("servingCell", str(dropped.exception))

    def test_all_is_one_word_for_every_kind(self) -> None:
        self.assertEqual(
            ("servingCell", "dlPrbCap", "pfWeight", "dlMcsBounds",
             "txAttenuationDb", "slicePrbQuota"),
            AgentRequest(sentences=(I1,), axes=("all",)).axes)
        self.assertEqual(("servingCell", "dlPrbCap", "pfWeight"), DEFAULT_AXIS_KINDS)


class TheCommonLimitOnChangedEntries(AgentSittingFixture):
    """The integrated reply of 2026-09-14 section 4: "use the common limit of
    four changed function/scope entries per configuration".

    **Common**, so it is one number on the rules every arm is handed, set where
    those rules are built rather than per method -- and every arm that can
    produce a candidate is shown refusing past it here.  A limit that held on
    three arms and not the fourth would leave that arm the only unconstrained
    one in the comparison, which is worse than no limit at all; this project
    has twice shipped a setting that existed and never took effect, so "the
    constant is set" is not what these tests assert.

    Nothing here freezes a catalog.  The declaration (:func:`_axis_specs`) and
    the agents are enough to say what each arm does with a candidate that
    moves five entries, which is what makes it affordable to ask it of all of
    them at the deployment's real three-UE size.
    """

    CELLS = (int(HOME_NCI), int(TARGET_NCI))
    I3 = "I3: UE ueId=133 needs at least 3.0 Mbps downlink, relaxable to 2.0"

    def declared(self, sentences: Sequence[str], ue_ids: Sequence[str]):
        """The catalog, the rules and the intents this sitting would declare."""
        request = AgentRequest(sentences=tuple(sentences))
        rows = parse_agent_intents(request, families=live_capable_families())
        identities = {ue: _EmulatedIdentity(ue, str(self.CELLS[0])) for ue in ue_ids}
        axes = _axis_specs(rows, identities, request, list(self.CELLS), [])
        catalog = _function_catalog(axes.declared)
        space = {spec.axis: tuple(spec.values) for spec in axes.declared}
        return (catalog, _compatibility_rules(catalog, ue_ids, space),
                tuple(row.intent for row in rows))

    def three_ues(self):
        # This class isolates the common limit.  Since 2026-09-19 a steer excludes a
        # cap or a weight on the same UE, which on three UEs would leave no way to
        # reach four entries at all, so the steer pairs are set aside here; they
        # have their own test (tests/test_steer_scheduler_exclusion.py).
        catalog, rules, intents = self.declared((I1, I2, self.I3), ("131", "132", "133"))
        rules = replace(rules, exclusive_axes=tuple(
            pair for pair in rules.exclusive_axes if "servingCell" not in pair))
        return catalog, rules, intents

    @staticmethod
    def steer(ue: str) -> Dict[str, Any]:
        return {"functionId": "steer", "scope": f"ue@{ue}",
                "policy": {"servingCell": TARGET_NCI}}

    @staticmethod
    def cap(ue: str) -> Dict[str, Any]:
        return {"functionId": "ue-dl-prb-cap", "scope": f"ue@{ue}",
                "policy": {"maxDlPrbs": "12"}}

    @property
    def four(self) -> List[Dict[str, Any]]:
        """Four entries over three UEs: at the limit, and nothing else refuses it."""
        return [self.steer("131"), self.steer("132"),
                self.cap("131"), self.cap("132")]

    @property
    def five(self) -> List[Dict[str, Any]]:
        """The same, one entry past the limit.  The cap and the weight are never
        both here, so the only rule this can break is the common limit."""
        return self.four + [self.cap("133")]

    def answer(self) -> Dict[str, Any]:
        return {"candidates": [
            {"controlId": "C1", "functions": self.four, "rationale": "at the limit"},
            {"controlId": "C2", "functions": self.five, "rationale": "one past it"}],
            "rationale": "one inside the limit and one over it"}

    # -- the one point the arms share ----------------------------------------

    def test_the_rules_every_arm_is_handed_carry_the_limit(self) -> None:
        _catalog, rules, _intents = self.three_ues()
        self.assertEqual(4, MAX_CHANGED_ENTRIES)
        self.assertEqual(MAX_CHANGED_ENTRIES, rules.max_changed_entries)
        # ... and it survives the round trip the episode record makes it take.
        self.assertEqual(MAX_CHANGED_ENTRIES, rules.to_record()["maxChangedEntries"])

    def test_the_built_sitting_carries_it_to_its_agents_and_its_record(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=2)
        self.assertEqual(MAX_CHANGED_ENTRIES, sitting.compatibility.max_changed_entries)
        self.assertEqual(MAX_CHANGED_ENTRIES,
                         sitting.preflight["compatibilityRules"]["maxChangedEntries"])

    # -- per arm --------------------------------------------------------------

    def test_the_model_control_arms_refuse_a_five_entry_candidate(self) -> None:
        catalog, rules, _intents = self.three_ues()
        for method in (METHOD_THREE_AGENT, METHOD_THREE_AGENT_COVERAGE):
            with self.subTest(method=method):
                resolver, models = ScriptedResolver.by_role(
                    {"control": [self.answer()]}, method=method)
                controls, record = RoleAgents(
                    models=models, resolver=resolver).form_controls(
                        ControlInputs(function_catalog=catalog, compatibility=rules))
                self.assertTrue(record.accepted)
                self.assertEqual(("C0", "C1"), controls.control_ids)
                self.assertTrue(any("C2" in note and "per configuration" in note
                                    for note in record.dropped), record.dropped)

    def test_the_internal_monolith_refuses_a_five_entry_candidate(self) -> None:
        catalog, rules, intents = self.three_ues()
        formation = {
            "t0": {"targetId": "T0",
                   "requirements": {"I1.r1": 3.0, "I2.r1": 1.5, "I3.r1": 3.0}},
            "levels": {"I1.r1": {"steps": 1, "bound": 2.0},
                       "I2.r1": {"steps": 1, "bound": 1.0},
                       "I3.r1": {"steps": 1, "bound": 2.0}},
            "alternatives": [{"targetId": "TA",
                              "levels": {"I1.r1": 1, "I2.r1": 0, "I3.r1": 0}}],
            "ranking": {"costRule": "normalized-concession"},
            "candidates": self.answer()["candidates"],
            "rationale": "T and C in one call"}
        resolver, models = ScriptedResolver.by_role({"monolith": [formation]},
                                                    method=METHOD_INTERNAL_MONOLITH)
        (_contract, controls), record = RoleAgents(
            models=models, resolver=resolver).monolith_form(MonolithFormInputs(
                target=TargetInputs(intents=intents,
                                    authorization=Authorization.from_intents(intents)),
                control=ControlInputs(function_catalog=catalog, compatibility=rules)))
        self.assertTrue(record.accepted)
        self.assertEqual(("C0", "C1"), controls.control_ids)
        self.assertTrue(any("C2" in note and "per configuration" in note
                            for note in record.dropped), record.dropped)

    def test_the_basic_monoliths_own_instructions_are_refused_past_the_limit(self) -> None:
        """The basic monolith names functions directly, so its answer reaches
        ``CompatibilityRules.refusals`` rather than the candidate validator."""
        catalog, rules, intents = self.three_ues()
        inputs = BasicInputs(intents=intents,
                             authorization=Authorization.from_intents(intents),
                             function_catalog=catalog, compatibility=rules)
        for label, instructions, accepted in (("at the limit", self.four, True),
                                              ("one past it", self.five, False)):
            with self.subTest(label):
                resolver, models = ScriptedResolver.by_role({"monolith": [
                    {"instructions": instructions,
                     "requirements": [{"reqId": "I1.r1", "value": 3.0}],
                     "rationale": label}]}, method=METHOD_BASIC_MONOLITH)
                agents = RoleAgents(models=models, resolver=resolver)
                if accepted:
                    _decision, record = agents.basic_monolith_decide(inputs)
                    self.assertTrue(record.accepted)
                else:
                    with self.assertRaises(DecisionUnavailable) as caught:
                        agents.basic_monolith_decide(inputs)
                    self.assertIn("per configuration", caught.exception.reason)

    def test_the_enumerated_product_every_arm_falls_back_to_stops_at_the_limit(self) -> None:
        """The deterministic ``C`` -- the basic monolith's whole preparation and
        every other arm's fallback -- is enumerated by ``catalog_product_controls``,
        which asks the rules before the validator ever sees a candidate."""
        # 2026-09-17: 이 불변식은 `catalog_product_controls` 의 것이므로 **그 층에서**
        # 잰다.  대체 경로 래퍼는 오너 지시로 이제 "남길 만큼만" 만들고(정책이 retain 을
        # 말하지 않으면 1 개), 그래서 래퍼를 통해서는 이 모양을 더 이상 볼 수 없다 --
        # 열거 자체의 규칙은 바뀌지 않았다.
        from assurance.coordination.tc import catalog_product_controls
        catalog, rules, _intents = self.three_ues()
        capped = catalog_product_controls(catalog, compatibility=rules)
        self.assertEqual(4, max(len(item.functions) for item in capped))
        # ... and it is the limit that bounds it, not the shape of the catalog:
        # the same three UEs without it reach six.
        uncapped = catalog_product_controls(
            catalog, compatibility=replace(rules, max_changed_entries=None))
        self.assertEqual(6, max(len(item.functions) for item in uncapped))
        self.assertLess(len(capped), len(uncapped))
        # 그리고 래퍼의 새 계약: 정책이 말하지 않으면 하나만 만든다(C0 는 검증기가 붙인다).
        agents = RoleAgents(models=RoleModels(method=METHOD_BASIC_MONOLITH))
        one = agents._deterministic_controls_for(
            ControlInputs(function_catalog=catalog, compatibility=rules))
        self.assertLessEqual(len(one.candidates), 2)

    def test_a_candidate_that_names_axes_instead_of_functions_is_capped_too(self) -> None:
        """A model Control answer may give a ``configuration`` and no
        ``functions``; that row never reaches ``refusals()``, so the validator
        counts the same quantity itself."""
        catalog, rules, _intents = self.three_ues()
        over = {f"servingCell@{ue}": TARGET_NCI for ue in ("131", "132", "133")}
        over.update({"dlPrbCap@131": "12", "dlPrbCap@132": "12"})
        answer = {"candidates": [
            {"controlId": "CX", "configuration": over, "rationale": "five axes"},
            {"controlId": "CY", "configuration": {"dlPrbCap@131": "12"},
             "rationale": "one axis"}], "rationale": "axes, not functions"}
        resolver, models = ScriptedResolver.by_role({"control": [answer]})
        controls, record = RoleAgents(
            models=models, resolver=resolver).form_controls(
                ControlInputs(function_catalog=catalog, compatibility=rules))
        self.assertEqual(("C0", "CY"), controls.control_ids)
        self.assertTrue(any("CX" in note and "per configuration" in note
                            for note in record.dropped), record.dropped)

    # -- what it does at the current operating point --------------------------

    def test_the_limit_is_slack_on_the_two_ue_sitting_this_lab_runs(self) -> None:
        """Honest about its own reach: over **two** intent UEs the default axes
        cannot produce a fifth changed entry at all -- the cap and the weight
        are mutually exclusive per UE, so the widest configuration the product
        admits is two UEs x (a steer and a cap) = exactly four.  The limit is
        correct there and refuses nothing.  It starts to bind at the third UE
        (the test above) and at ``--axes all``.
        """
        catalog, rules, _intents = self.declared((I1, I2), ("131", "132"))
        baselines = catalog.baselines({})
        uncapped = catalog_product_controls(
            catalog, baselines,
            compatibility=replace(rules, max_changed_entries=None))
        self.assertEqual(4, max(len(item.functions) for item in uncapped))
        self.assertEqual(len(uncapped), len(catalog_product_controls(
            catalog, baselines, compatibility=rules)))


class TheCatalogCeiling(AgentSittingFixture):
    """Contract v3 section 3: a sitting bigger than the ceiling refuses.

    The refusal happens in ``compose_joint``, before a single contract is
    built, so these are the cheapest tests in the file even though they name
    the largest sittings.
    """

    def refusal(self, **request_kwargs: Any) -> str:
        # Deliberately not through ``build_request``: the fixture narrows a
        # request that states no axes, and the whole point here is the size
        # of the one nobody narrowed.
        with self.assertRaises(LiveConsoleError) as raised:
            build_hardware_free_agent_sitting(
                AgentRequest(sentences=(I1, I2), **request_kwargs), tmp_dir=self.tmp,
                ran=EmulatedRan(ues={"131": HOME_NCI, "132": HOME_NCI},
                                cells={HOME_NCI: 5.0, TARGET_NCI: 5.0},
                                offered_load_mbps={"131": 4.0, "132": 4.0},
                                noise_sigma=0.0),
                stamp="20260907T120000Z")
        return str(raised.exception)

    def test_every_axis_at_once_is_refused_and_the_message_names_the_axes(self) -> None:
        message = self.refusal(axes=("all",), max_catalog_cardinality=4096)
        # No slice is named on this deployment's emulator beyond the one its
        # UEs are on, so the product is the contract's 248832.
        self.assertIn("this sitting exposes 248832 combinations", message)
        self.assertIn("steer 4 x cap 16 x pf 16 x mcs 9 x atten 9 x quota 3", message)
        self.assertIn("the ceiling is 4096", message)

    def test_the_refusal_names_both_ways_forward(self) -> None:
        message = self.refusal(axes=("all",), max_catalog_cardinality=4096)
        self.assertIn("--axes", message)
        self.assertIn("--max-catalog", message)
        # 2026-09-19: the freeze is a domain; raising the ceiling costs no freeze time.
        self.assertIn("not an enumeration", message)

    def test_the_operator_can_lower_the_ceiling_deliberately(self) -> None:
        message = self.refusal(max_catalog_cardinality=16)
        self.assertIn("the ceiling is 16", message)
        self.assertIn("steer 4 x cap 16 x pf 16", message)

    def test_narrowing_one_ladder_gets_a_refused_sitting_under_the_ceiling(self) -> None:
        # The same sitting, with the two UE ladders at one rung each: 4 x 4 x
        # 4 = 64, which composes.  Nothing was truncated to get there.
        sitting = self.build_request(AgentRequest(
            sentences=(I1, I2), method=METHOD_DETERMINISTIC,
            axes=("servingCell", "dlPrbCap", "pfWeight"),
            caps={"131": (12,), "132": (12,)},
            pf_weights={"131": (2.0,), "132": (2.0,)}))
        self.assertEqual(64, sitting.catalog_cardinality)
        self.assertEqual(["servingCell", "dlPrbCap", "pfWeight"],
                         sitting.preflight["exposedAxisKinds"])
        self.assertEqual(1_000_000_000, sitting.preflight["catalogCeiling"])

    def test_a_ceiling_of_nothing_is_not_a_ceiling(self) -> None:
        with self.assertRaises(LiveConsoleError) as raised:
            AgentRequest(sentences=(I1,), max_catalog_cardinality=0)
        self.assertIn("--max-catalog", str(raised.exception))


class TheLivePathRefusesByName(unittest.TestCase):
    """Contract v3 section 4: an axis this deployment cannot carry is named.

    The hardware-free runtime answers every one of these ports itself, which
    is why its sittings compose; this asserts what the *live* root does with
    the same declarations and no injected adapter -- refuse the axis, name
    it, and say how to proceed.  It never substitutes a mock.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        profile = HermeticDeployment.write(
            temporary.name, ues={"131": HOME_NCI, "132": HOME_NCI},
            cells={HOME_NCI: 5.0, TARGET_NCI: 5.0})
        self.deployment = load_live_deployment(profile)

    def axes(self, kinds: Sequence[str], **request_kwargs: Any):
        request = AgentRequest(sentences=(I1,), axes=tuple(kinds), **request_kwargs)
        rows = parse_agent_intents(request, families=live_capable_families())
        identities = {"131": _EmulatedIdentity("131", HOME_NCI)}
        return _axis_specs(rows, identities, request,
                           [int(HOME_NCI), int(TARGET_NCI)], ("1",))

    def refusal(self, kind: str) -> str:
        axes = self.axes(("servingCell", kind))
        with self.assertRaises(LiveConsoleError) as raised:
            _refuse_uncarried_axes(axes, deployment=self.deployment, overrides={},
                                   exposed=("servingCell", kind))
        return str(raised.exception)

    def test_cap_and_pf_have_wired_readers(self) -> None:
        _refuse_uncarried_axes(self.axes(("servingCell", "dlPrbCap", "pfWeight")),
                               deployment=self.deployment, overrides={},
                               exposed=("servingCell", "dlPrbCap", "pfWeight"))

    def test_the_remaining_unwired_axes_are_refused_by_name(self) -> None:
        # Two, not three: the cell transmit-power axis left this list on
        # 2026-09-16 when KpmCellConfigReader gave RAN.Cell.TxAttenuationDb the
        # cell-scoped reader it was waiting for, and the reader was confirmed
        # live against nb 2816 epoch 822.  The other two still have no reader:
        # RAN.Cell.DlMcsBounds is no_value on the wire and the slice quota has
        # no counter at all.
        for kind, counter in (("dlMcsBounds", "RAN.Cell.DlMcsBounds"),
                              ("slicePrbQuota", "AIC_SliceSLATarget_1.0.0")):
            with self.subTest(kind=kind):
                message = self.refusal(kind)
                # The axis, at its own scope, and the missing piece.
                self.assertIn(f"{kind}@", message)
                self.assertIn(counter, message)
                self.assertIn("--axes servingCell", message)

    def test_the_power_axis_is_carried_and_is_no_longer_refused(self) -> None:
        # The positive half of the same change: exposing it must now compose.
        _refuse_uncarried_axes(self.axes(("servingCell", "txAttenuationDb")),
                               deployment=self.deployment, overrides={},
                               exposed=("servingCell", "txAttenuationDb"))

    def test_an_injected_adapter_answers_the_question_itself(self) -> None:
        axes = self.axes(("servingCell", "pfWeight"))
        _refuse_uncarried_axes(axes, deployment=self.deployment,
                               overrides={"r1-pf@131": object()},
                               exposed=("servingCell", "pfWeight"))

    def test_a_profile_with_no_action_producer_is_a_different_refusal(self) -> None:
        deployment = replace(self.deployment, action_producer=None)
        axes = self.axes(("servingCell", "dlPrbCap"))
        with self.assertRaises(LiveConsoleError) as raised:
            _refuse_uncarried_axes(axes, deployment=deployment, overrides={},
                                   exposed=("servingCell", "dlPrbCap"))
        self.assertIn("no action producer serving AIC_UeDlPrbCap_1.0.0",
                      str(raised.exception))


class TheHeadlessEntryPoint(unittest.TestCase):
    """``main.py --hardware-free --no-gui --agent``, as contract section 9 states it."""

    def run_main(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "main.py", "--hardware-free", "--no-gui", "--agent",
             *arguments], cwd=str(REPO_ROOT), capture_output=True, text=True,
            timeout=900, stdin=subprocess.DEVNULL)

    def test_intents_json_complete_and_missing_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intents.json"
            records = [{"intentId": f"I{i}", "owner": f"owner-{i}", "ueId": str(130+i),
                        "priority": i, "requirement": {"reqId": f"I{i}.r1", "kpi": "dlGoodputMbps",
                        "scope": f"ue@{130+i}", "op": ">=", "value": value, "unit": "Mbps",
                        "steps": 2, "bound": bound}} for i, value, bound in ((1, 3.0, 2.0), (2, 1.5, 1.0))]
            flags = ["--budget", "6", "--target-agent-model", "mock:agent",
                     "--control-agent-model", "mock:agent", "--trajectory-agent-model", "mock:agent"]
            path.write_text(json.dumps(records))
            completed = self.run_main("--intents-json", str(path), *flags)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("T0_SUCCESS", completed.stdout)
            del records[1]["requirement"]["bound"]
            path.write_text(json.dumps({"intents": records}))
            completed = self.run_main("--intents-json", str(path), *flags)
            self.assertEqual(completed.returncode, 3, completed.stdout + completed.stderr)
            self.assertIn("bound", completed.stdout)
            self.assertIn("needs an answer", completed.stdout)
            completed = self.run_main("--intents-json", str(path), "--answers", '{"I2":{"bound":1.0}}', *flags)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            del records[0]["owner"]
            path.write_text(json.dumps(records))
            completed = self.run_main("--intents-json", str(path), *flags)
            self.assertEqual(completed.returncode, 3)
            self.assertIn("missing required field: owner", completed.stdout)

    def test_a_reachable_original_target_exits_zero(self) -> None:
        completed = self.run_main("--method", "deterministic", "--cmd", I1, "--cmd", I2,
                                  "--budget", "6")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("termination        : T0_SUCCESS", completed.stdout)
        self.assertIn("wrote episode", completed.stdout)
        # The printout says what the sitting is actually doing.
        self.assertIn("function steer", completed.stdout)
        self.assertIn("observe dlGoodputMbps", completed.stdout)
        self.assertIn("unselected rule    : baseline", completed.stdout)
        self.assertIn("aiming at", completed.stdout)
        self.assertRegex(completed.stdout, r"target T0\s+: cost\s+0\.00")

    def test_an_unreachable_target_exits_two_with_a_spent_budget(self) -> None:
        completed = self.run_main(
            "--method", "deterministic", "--budget", "2",
            "--cmd", "I1: UE ueId=131 needs at least 9.0 Mbps downlink, non-relaxable")
        self.assertEqual(2, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("termination        :", completed.stdout)

    def test_the_cli_states_the_timing_mode_the_boundaries_and_trial_zero(self) -> None:
        completed = self.run_main(
            "--method", "deterministic", "--cmd", I1, "--cmd", I2, "--budget", "4",
            "--timing-mode", "cold-start", "--initial-measurement",
            "--boundary", "exogenous:the interferer was switched on")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("timing mode        : cold-start", completed.stdout)
        self.assertIn("charged to B", completed.stdout)
        self.assertIn("boundary           : exogenous at start :: the interferer",
                      completed.stdout)
        self.assertIn("not a trial        : sample", completed.stdout)
        self.assertIn("the initial measurement is trial 0 and is not counted",
                      completed.stdout)

    def test_a_boundary_kind_the_executor_does_not_know_is_refused(self) -> None:
        completed = self.run_main("--method", "deterministic", "--cmd", I1,
                                  "--cmd", I2, "--budget", "2",
                                  "--boundary", "fading:the channel faded")
        self.assertEqual(3, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("never reset a budget", completed.stdout)

    def test_an_unanswered_intake_refuses_rather_than_guessing(self) -> None:
        completed = self.run_main(
            "--method", "deterministic", "--budget", "2",
            "--cmd", "I1: UE ueId=131 needs at least 3.0 Mbps downlink")
        self.assertEqual(3, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("needs an answer", completed.stdout)
        self.assertIn("refused before anything was submitted", completed.stdout)

    def test_the_answers_flag_settles_it_and_the_sitting_runs(self) -> None:
        completed = self.run_main(
            "--method", "deterministic", "--budget", "4",
            "--cmd", "I1: UE ueId=131 needs at least 3.0 Mbps downlink",
            "--cmd", I2,
            "--answers", json.dumps({"I1": {"steps": 2, "bound": 2.0}}))
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("termination        : T0_SUCCESS", completed.stdout)

    def test_the_observation_rule_flag_reaches_the_kernel_case(self) -> None:
        completed = self.run_main(
            "--method", "deterministic", "--budget", "2", "--cmd", I1, "--cmd", I2,
            "--observe", "dlGoodputMbps=2000:4000:60000",
            "--unselected-rule", "keep-current", "--retain", "6")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("observe dlGoodputMbps: settle 2000 ms, window 4000 ms",
                      completed.stdout)
        self.assertIn("unselected rule    : keep-current", completed.stdout)

    def test_no_sentence_is_refused_before_anything_starts(self) -> None:
        completed = self.run_main("--method", "deterministic")
        self.assertEqual(3, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("refused before anything was submitted", completed.stdout)

    def test_the_axes_flag_reaches_the_executor_and_refuses_by_name(self) -> None:
        # ``--axes all`` is the whole space in one word, and on this
        # deployment the whole space is over the ceiling, so the CLI's job is
        # to carry the refusal through unchanged rather than to truncate.
        result = self.run_main("--cmd", I1, "--cmd", I2, "--axes", "all",
                               "--max-catalog", "4096", "--method", "deterministic")
        self.assertEqual(3, result.returncode, result.stdout + result.stderr)
        self.assertIn("248832 combinations", result.stdout)
        self.assertIn("--max-catalog", result.stdout)

    def test_the_max_catalog_flag_lowers_the_ceiling(self) -> None:
        result = self.run_main("--cmd", I1, "--cmd", I2, "--max-catalog", "16",
                               "--method", "deterministic")
        self.assertEqual(3, result.returncode, result.stdout + result.stderr)
        self.assertIn("the ceiling is 16", result.stdout)
        self.assertIn("steer 4 x cap 16 x pf 16", result.stdout)

    def test_the_per_kind_ladder_flags_narrow_one_scope_each(self) -> None:
        result = self.run_main(
            "--cmd", I1, "--cmd", I2, "--method", "deterministic", "--budget", "2",
            "--axes", "servingCell,dlPrbCap,pfWeight",
            "--cap-axis", "131:12", "--cap-axis", "132:12",
            "--pf-axis", "131:2.0", "--pf-axis", "132:2.0")
        self.assertIn("catalog            : 64 candidates", result.stdout,
                      result.stdout + result.stderr)

    def test_a_retired_role_flag_names_the_role_that_replaced_it(self) -> None:
        completed = self.run_main("--cmd", I1, "--search-agent-model", "claude-sonnet")
        self.assertEqual(2, completed.returncode)
        self.assertIn("--trajectory-agent-model", completed.stderr)


class TheLiveKpiObserver(unittest.TestCase):
    """:class:`TunRateObserver`, with the ssh runner faked (contract section 5)."""

    class _Runner:
        def __init__(self, answers: Mapping[str, Sequence[Any]]) -> None:
            self.answers = {host: list(items) for host, items in answers.items()}
            self.commands: List[List[str]] = []

        def run(self, argv, capture_output=True, text=True, timeout=None):
            self.commands.append(list(argv))
            queue = self.answers.get(argv[-2], [])
            answer = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else None)
            if isinstance(answer, BaseException):
                raise answer
            if answer is None:
                return type("R", (), {"returncode": 255, "stdout": "", "stderr": "no route"})()
            return type("R", (), {"returncode": 0, "stdout": f"{answer}\n", "stderr": ""})()

    def observer(self, answers, **kwargs):
        clock = {"ms": 0.0}

        def monotonic_ms() -> float:
            clock["ms"] += 1000.0
            return clock["ms"]

        runner = self._Runner(answers)
        return TunRateObserver(hosts={"131": "ue1", "132": "ue2"}, runner=runner,
                               monotonic_ms=monotonic_ms, **kwargs), runner

    def test_the_first_sample_has_nothing_to_difference_against(self) -> None:
        observer, runner = self.observer({"ue1": [0, 750000], "ue2": [0, 250000]})
        self.assertEqual({}, observer.sample())
        self.assertEqual(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "ue1",
                          "cat /sys/class/net/oaitun_ue1/statistics/rx_bytes"],
                         runner.commands[0])

    def test_the_rate_is_the_byte_delta_over_the_elapsed_time(self) -> None:
        observer, _runner = self.observer({"ue1": [0, 750000], "ue2": [0, 250000]})
        observer.sample()
        second = observer.sample()
        # 750000 B in 1 s = 6.0 Mbps; 250000 B in 1 s = 2.0 Mbps.
        self.assertAlmostEqual(6.0, second["dlGoodputMbps@131"], places=6)
        self.assertAlmostEqual(2.0, second["dlGoodputMbps@132"], places=6)

    def test_an_unreachable_ue_contributes_no_key_and_no_exception(self) -> None:
        observer, _runner = self.observer({"ue1": [0, 750000]})
        observer.sample()
        second = observer.sample()
        self.assertIn("dlGoodputMbps@131", second)
        self.assertNotIn("dlGoodputMbps@132", second)
        self.assertTrue(any(item["ueId"] == "132" for item in observer.failures))

    def test_the_serving_cell_comes_from_the_attribution_reader(self) -> None:
        observer, _runner = self.observer({"ue1": [10], "ue2": [10]},
                                          serving_cells=lambda: {"131": 12345678})
        self.assertEqual("12345678", observer.sample()["servingCell@131"])

    def test_the_hosts_are_the_profiles_first_and_the_labs_env_second(self) -> None:
        """env 의 자리는 **이 판이 다루는 UE 전체**에 대해 매겨진다.

        2026-09-22 codex 감사: 미해결 UE 만 env 에 넘기면 거기서 1 부터 다시 번호가
        매겨져, 프로파일이 131 만 이름 붙였을 때 132 에 `HW_UE1_HOST` -- 131 의 기계 --
        가 붙는다.  그러면 131 의 처리량이 132 의 goodput 으로 기록되고 아무 데서도
        안 터진다.  `HW_UE1_HOST` 는 첫 UE 의 기계이지 "다음 빈자리" 가 아니다.
        (같은 규칙을 `test_ue_host_mapping_is_positional_over_all_ues` 가 따로 지킨다.)
        """
        profile = {"liveConsole": {"ueHosts": {"131": "ue-a"}}}
        self.assertEqual({"131": "ue-a"}, resolve_ue_hosts(
            ["131", "132"], profile_document=profile, env={"HW_UE1_HOST": "ue2"}))
        self.assertEqual({"131": "ue-a", "132": "machine-2"}, resolve_ue_hosts(
            ["131", "132"], profile_document=profile,
            env={"HW_UE1_HOST": "ue2", "HW_UE2_HOST": "machine-2"}))
        self.assertEqual({}, resolve_ue_hosts(["131"], profile_document={}, env={}))


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- #
# contract v4 -- timing modes, trial counting, boundaries, the prepared board
# --------------------------------------------------------------------------- #

#: A preparation that costs five virtual seconds.  ``MOCK`` charges the real
#: latency of a scripted answer to the virtual clock, so a cold start really
#: has spent that much of B before its first trial.
SLOW_PREPARATION_MS = 5000.0


class TheTwoTimingModes(AgentSittingFixture):
    """``exp_metrics.md`` section 3: where the episode clock starts.

    The two modes share one B and differ only in whether preparation is inside
    it.  They are never mixed in one summary, which is why the mode is on the
    record and not inferred from the numbers.
    """

    def slow_preparation(self, **request_kwargs: Any):
        resolver, models = self.scripted({
            "target": [slow(T_ANSWER, SLOW_PREPARATION_MS)],
            "control": [C_ANSWER], "trajectory": [PAIR]})
        return self.build(resolver=resolver, role_models=models, budget_trials=2,
                          **request_kwargs)

    def test_a_prepared_sitting_starts_its_clock_after_the_preparation(self) -> None:
        # Six seconds: enough for one 4 s hold (amendment v3.1 reserves the
        # whole observation before a trial starts), not for it plus the 5 s
        # preparation, which a cold start would have charged.
        sitting = self.slow_preparation(deadline_ms=6000)
        sitting.confirm()
        summary = sitting.run()
        record = sitting.episode_record()
        self.assertEqual(TIMING_PREPARED, record["timing"]["timingMode"])
        self.assertGreaterEqual(record["timing"]["prepMs"], SLOW_PREPARATION_MS)
        # t0 is the live trigger, taken after everything preparation did, so a
        # B of six seconds still buys the search a trial.
        self.assertGreaterEqual(record["timing"]["t0"], record["timing"]["prepEnd"])
        self.assertGreaterEqual(summary["trials"], 1)

    def test_a_cold_start_charges_the_preparation_to_the_same_b(self) -> None:
        sitting = self.slow_preparation(deadline_ms=3000,
                                        timing_mode=TIMING_COLD_START)
        record_before_run = sitting.timing
        # t0 is the release of the required inputs -- where the composition
        # began -- so it is the preparation's own start, not a later trigger.
        self.assertEqual(record_before_run["prepStart"], record_before_run["t0"])
        self.assertIn("deadlineDuringPreparation", sitting.preflight)
        sitting.confirm()
        summary = sitting.run()
        # B expired while T and C were being formed: a real outcome, with
        # nothing applied, and not an error.
        self.assertEqual("DEADLINE", summary["termination"])
        self.assertEqual(0, summary["trials"])
        self.assertEqual([], sitting.runtime.trials)
        record = sitting.episode_record()
        self.assertEqual(TIMING_COLD_START, record["timing"]["timingMode"])
        self.assertEqual({"prepStart", "prepEnd", "prepMs", "timingMode", "t0", "end"},
                         set(record["timing"]))

    def test_the_clock_is_not_reset_so_the_elapsed_time_includes_preparation(self) -> None:
        # The same sitting with room to run: a cold start's total elapsed time
        # is measured from t0 = input release, so it carries the preparation.
        sitting = self.slow_preparation(timing_mode=TIMING_COLD_START)
        sitting.confirm()
        sitting.run()
        cost = sitting.resource_cost()
        self.assertGreaterEqual(cost["prepMs"], SLOW_PREPARATION_MS)
        self.assertGreaterEqual(cost["totalMs"], SLOW_PREPARATION_MS)
        self.assertGreaterEqual(sitting.grid.trials[0].elapsed_ms, SLOW_PREPARATION_MS)

    def test_an_unknown_timing_mode_is_refused_by_name(self) -> None:
        with self.assertRaises(LiveConsoleError) as caught:
            AgentRequest(sentences=(I1,), timing_mode="warm")
        self.assertIn("prepared", str(caught.exception))
        self.assertIn("cold-start", str(caught.exception))


class EveryTrialStartsFromTheBaseline(AgentSittingFixture):
    """Owner decision 2026-09-20: each trial is judged on the frozen C0.

    A passing trial is returned to the baseline through the ordinary rollback
    (``BASELINE_RESET`` -> ``PASS_RESET``); the next one starts only after the
    recovery reread confirmed it, and the reset's time is charged to B.
    """

    def run_three(self, **kwargs):
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=3, **kwargs)
        sitting.confirm()
        summary = sitting.run()
        return sitting, summary

    def test_a_passing_trial_is_reset_and_its_verdict_kept(self) -> None:
        sitting, _summary = self.run_three()
        resets = [event for event in sitting.non_trial_events
                  if event["kind"] == "reset-to-baseline"]
        live = [t for t in sitting.grid.trials if t.trial_index != 0]
        self.assertTrue(live)
        for trial in live:
            self.assertNotEqual("SETTLED_SUCCESS", trial.kernel["terminalState"], trial.kernel)
        passed = [t for t in live if t.kernel["outcome"] == "PASS_RESET"]
        self.assertEqual(len(passed), len([e for e in resets]))
        for event in resets:
            self.assertTrue(event["restored"])
            self.assertIsNotNone(event["durationMs"])
        # Every trial after the first starts from the baseline it froze at.
        for trial in live:
            self.assertEqual(dict(sitting.baselines),
                             {axis: sitting.runtime.trial_baseline(trial.kernel["trialId"]).get(axis, value)
                              for axis, value in sitting.baselines.items()})

    def test_cap_and_priority_trials_each_start_from_the_baseline(self) -> None:
        sitting = self.build_request(AgentRequest(
            sentences=(I1, I2), method=METHOD_DETERMINISTIC, budget_trials=4,
            axes=("servingCell", "dlPrbCap", "pfWeight"),
            caps={"131": (12,), "132": (12,)}, pf_weights={"131": (2.0,), "132": (2.0,)}))
        sitting.confirm()
        sitting.run()
        live = [t for t in sitting.grid.trials if t.trial_index != 0]
        self.assertTrue(live)
        for trial in live:
            started = sitting.runtime.trial_baseline(trial.kernel["trialId"])
            for axis, value in sitting.baselines.items():
                self.assertEqual(str(value), str(started.get(axis, value)), (trial.control_id, axis))
            self.assertNotEqual("SETTLED_SUCCESS", trial.kernel["terminalState"])
        # And the deployment is back at C0 after the search (before retention).
        self.assertTrue(all(e.get("restored") for e in sitting.non_trial_events
                            if e["kind"] == "reset-to-baseline"))

    def test_turned_off_a_passing_trial_finalizes_live_as_before(self) -> None:
        sitting, _summary = self.run_three(reset_each_trial=False)
        self.assertFalse([e for e in sitting.non_trial_events
                          if e["kind"] == "reset-to-baseline"])

    def test_the_reset_does_not_exhaust_the_catalog_while_untried_candidates_remain(self) -> None:
        sitting, summary = self.run_three()
        if summary["termination"] == "CATALOG_EXHAUSTED":
            self.assertEqual(0, sitting._eligible_remaining())


class TheTrialCountingRules(AgentSittingFixture):
    """``exp_metrics.md`` section 3, contract v4 section 2, made explicit."""

    @staticmethod
    def served() -> EmulatedRan:
        """Both owners already served: nothing has to be applied to meet T0."""
        return EmulatedRan(ues={"131": HOME_NCI, "132": HOME_NCI},
                           cells={HOME_NCI: 5.0, TARGET_NCI: 5.0},
                           offered_load_mbps={"131": 2.0, "132": 2.0},
                           noise_sigma=0.0)

    def test_every_live_trial_is_counted_once_at_execution_start(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=2)
        sitting.confirm()
        sitting.run()
        record = sitting.episode_record()
        self.assertTrue(record["trials"])
        self.assertTrue(all(trial["counted"] for trial in record["trials"]))
        self.assertEqual([1, 2][:len(record["trials"])],
                         [trial["trialIndex"] for trial in record["trials"]])

    def test_the_initial_measurement_is_trial_zero_and_is_not_counted(self) -> None:
        sitting = self.build(["I1: UE ueId=131 needs at least 4.5 Mbps downlink, "
                              "relaxable to 2.0", I2],
                             method=METHOD_DETERMINISTIC, budget_trials=2,
                             initial_measurement=True)
        sitting.confirm()
        sitting.run()
        record = sitting.episode_record()
        first = record["trials"][0]
        self.assertEqual(INITIAL_TRIAL_INDEX, first["trialIndex"])
        self.assertFalse(first["counted"])
        self.assertEqual(0.0, first["elapsedMs"])
        self.assertIsNone(first["kernel"]["trialId"])
        # It is judged by the same predicates as any trial, against the
        # targets authorized at t0 ...
        # 2026-09-23: 판정은 Ω 전체에 대해 일어난다(결정 §2).
        self.assertEqual(set(sitting.evaluation_contract.target_ids),
                         set(first["verdicts"]))
        # ... and it does not move the index of the first live trial, each of
        # which is counted.
        live = [trial for trial in record["trials"] if trial["counted"]]
        self.assertEqual([1, 2][:len(live)], [trial["trialIndex"] for trial in live])
        self.assertEqual(len(live), len(sitting.runtime.trials))

    def test_a_supported_initial_success_is_recorded_at_trial_zero_and_time_zero(self) -> None:
        sitting = self.build(
            ["I1: UE ueId=131 needs at least 1.0 Mbps downlink, relaxable to 0.5",
             "I2: UE ueId=132 needs at least 1.0 Mbps downlink, relaxable to 0.5"],
            ran=self.served(), method=METHOD_DETERMINISTIC, budget_trials=3,
            initial_measurement=True)
        sitting.confirm()
        summary = sitting.run()
        self.assertEqual("T0_SUCCESS", summary["termination"])
        record = sitting.episode_record()
        self.assertTrue(record["t0Success"])
        self.assertEqual(INITIAL_TRIAL_INDEX, record["firstSuccess"]["trialIndex"])
        self.assertEqual(0.0, record["firstSuccess"]["elapsedMs"])
        # Nothing was applied to reach it: the Kernel was never asked.
        self.assertEqual([], sitting.runtime.trials)
        self.assertFalse(record["trials"][0]["counted"])

    def test_a_late_answer_costs_no_re_measurement_and_both_trials_count(self) -> None:
        # Owner scenario 2026-09-22 section 3: a slow answer used to buy a
        # charged re-measurement before the question was put again.  It no
        # longer does -- the answer is applied as it stands -- so the episode
        # shows no ``remeasurement`` event and still counts both trials.
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER],
            "trajectory": [PAIR,
                           slow({"controlId": "C2", "targetId": "T5",
                                 "rationale": "slow"}, 20000.0)]})
        sitting = self.build(["I1: UE ueId=131 needs at least 4.5 Mbps downlink, "
                              "relaxable to 2.0", I2],
                             resolver=resolver, role_models=models, budget_trials=2)
        sitting.confirm()
        sitting.run()
        record = sitting.episode_record()
        events = [item for item in record["nonTrialEvents"]
                  if item["kind"] == "remeasurement"]
        self.assertEqual([], events)
        self.assertEqual(2, len([t for t in record["trials"] if t["counted"]]))
        self.assertTrue(any(call.stale_at_arrival for call in sitting.agents.calls))

    def test_a_proposal_the_catalog_will_not_admit_again_is_not_a_trial(self) -> None:
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER],
            "trajectory": [{"targetId": "T5", "controlId": "C0", "rationale": "hold"}]})
        sitting = self.build(resolver=resolver, role_models=models, budget_trials=3)
        sitting.confirm()
        summary = sitting.run()
        self.assertEqual("PROPOSAL_FAILURE", summary["termination"])
        rejected = [item for item in sitting.non_trial_events
                    if item["kind"] == "rejected-proposal"]
        # The proposal is refused, the reason goes back, and the one bounded
        # revision is refused too: two rejections, still no trial.
        self.assertEqual(2, len(rejected))
        self.assertEqual({"C0"}, {item["controlId"] for item in rejected})
        self.assertEqual(1, summary["trials"])

    def test_the_revision_prompt_carries_why_the_proposal_was_refused(self) -> None:
        # 2026-09-30 v5.4t: the re-ask was byte-identical, so a model that answers the same
        # input the same way (qwen3) proposed the tried configuration 57-98 times a board.
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER],
            "trajectory": [{"targetId": "T5", "controlId": "C0", "rationale": "hold"}]})
        sitting = self.build(resolver=resolver, role_models=models, budget_trials=3)
        sitting.confirm()
        sitting.run()
        selections = [call.prompt for call in sitting.agents.calls if call.phase == "selection"]
        self.assertGreaterEqual(len(selections), 2)
        self.assertNotIn("previous answer was refused", selections[-2])   # the refused ask
        self.assertIn("Your previous answer was refused: C0 ", selections[-1])  # its revision
        self.assertEqual("", sitting.agents.rejection_note)

    def test_a_rejected_proposal_spends_the_original_clock(self) -> None:
        """The refused call is charged to B, and the revision does not reset it.

        A rejection that cost nothing would make an unusable proposal free, so
        a method that proposes badly would look as cheap as one that proposes
        well.  Both rejections carry an elapsed time on the episode clock and
        the second is later than the first.
        """
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER],
            "trajectory": [{"targetId": "T5", "controlId": "C0", "rationale": "hold"}]})
        sitting = self.build(resolver=resolver, role_models=models, budget_trials=3)
        sitting.confirm()
        summary = sitting.run()
        rejected = [item for item in sitting.non_trial_events
                    if item["kind"] == "rejected-proposal"]
        self.assertEqual(2, len(rejected))
        for item in rejected:
            self.assertGreater(item["elapsedMs"], 0.0)
        self.assertLessEqual(rejected[0]["elapsedMs"], rejected[1]["elapsedMs"])
        self.assertEqual("PROPOSAL_FAILURE", summary["termination"])

    def test_one_valid_observation_evaluates_every_target(self) -> None:
        """One admitted window decides every column of T, not just the aimed one.

        The trajectory aims at a single cell, but the row it produces is scored
        against the whole contract: a configuration that happens to satisfy a
        relaxed target must be seen to do so without spending another trial.
        """
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=1,
                             initial_measurement=True)
        sitting.confirm()
        sitting.run()
        record = sitting.episode_record()
        live = [row for row in record["trials"] if row["counted"]]
        self.assertEqual(1, len(live))
        verdicts = live[0]["verdicts"]
        # 2026-09-23: 판정은 이제 **Ω 전체**에 대해 일어난다(결정 §2) -- 선택된
        # T 로 확인하면 3A·IM 만 6~8 열이고 BM 만 144 열이던 그 비대칭을 다시
        # 못 박는 셈이다.
        self.assertEqual({target.target_id for target in sitting.evaluation_contract.targets},
                         set(verdicts))
        for column in verdicts.values():
            self.assertTrue(column)

    def test_an_interrupted_dispatched_trial_stays_counted(self) -> None:
        """Counting happens at execution start, so an interrupted trial counts.

        Charging only the trials that finished cleanly would quietly shrink the
        denominator of every comparison: a method whose executions abort would
        report the same trial count as one whose executions settle.
        """
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=1,
                             initial_measurement=True)
        sitting.confirm()
        sitting.run()
        record = sitting.episode_record()
        live = [row for row in record["trials"] if row["counted"]]
        self.assertEqual(1, len(live))
        index = int(live[0]["trialIndex"])
        # Counting is recorded at dispatch, so it does not depend on how the
        # execution ended -- clearing the outcome must not clear the count.
        sitting.trial_flags.setdefault(index, {})["counted"] = True
        again = sitting.episode_record()
        self.assertEqual([index],
                         [int(row["trialIndex"]) for row in again["trials"]
                          if row["counted"]])

    def test_the_sampling_outside_a_trial_is_a_charged_non_trial_event(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=1,
                             horizon_ms=60000)
        sitting.confirm()
        sitting.run()
        kinds = [item["kind"] for item in sitting.non_trial_events]
        self.assertEqual(2, kinds.count("sample"))
        for item in sitting.non_trial_events:
            self.assertIn(item["kind"], NON_TRIAL_KINDS)
            self.assertEqual({"at", "kind", "detail", "elapsedMs"},
                             set(item) - {"controlId", "candidateId", "trialIndex",
                                          "samples", "durationMs", "restored"})

    def test_a_decision_that_outruns_its_allowance_is_a_charged_event(self) -> None:
        """A late answer is recorded and used; it neither ends the episode nor crashes.

        ``_decision_overrun`` declares a ``decision-timeout`` event and then
        ends the sitting at ``PROPOSAL_FAILURE``.  The kind was missing from
        ``NON_TRIAL_KINDS``, so the declaration raised ``LiveConsoleError``
        instead -- and on live hardware that path is taken constantly, because
        the observation bundle expires in 10 s while a decision call takes
        7-122 s.  Every such episode was lost with an exception rather than
        recorded with a reason.
        """
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER],
            "trajectory": [{"targetId": "T0", "controlId": "C0", "rationale": "hold"}]})
        sitting = self.build(resolver=resolver, role_models=models,
                             budget_trials=3, decision_deadline_ms=0)
        sitting.confirm()
        summary = sitting.run()
        # 오너 지시(2026-09-18): 넘긴 결정은 기록하고 **기다려 쓴다** -- 판을 끝내지 않는다.
        # 끝났다면 허용 시간 때문이 아니라 답의 내용 때문이어야 한다 (이 대본의 답은
        # 이미 해 본 C0 라 검증이 거절한다 -- 늦게 온 답이 실제로 쓰였다는 증거다).
        detail = str(sitting.episode_record()["termination"].get("detail") or "")
        self.assertNotIn("decision allowance", detail)
        self.assertIn("rejected-proposal",
                      [item["kind"] for item in sitting.non_trial_events])
        timeouts = [item for item in sitting.non_trial_events
                    if item["kind"] == "decision-timeout"]
        self.assertGreaterEqual(len(timeouts), 1)
        self.assertIn("decision-timeout", NON_TRIAL_KINDS)
        self.assertIn("recorded, and the answer is used", timeouts[0]["detail"])

    def test_an_unknown_non_trial_kind_is_refused(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=1)
        with self.assertRaises(LiveConsoleError) as caught:
            sitting.declare_non_trial_event("guess", "nothing this executor knows")
        self.assertIn("rejected-proposal", str(caught.exception))


class TheEpisodeBoundaries(AgentSittingFixture):
    """``exp_metrics.md`` section 1: predeclared, recorded, and never a reset."""

    def test_only_the_three_declared_kinds_are_boundaries(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=1)
        for kind in BOUNDARY_KINDS:
            self.assertEqual(kind, sitting.declare_boundary(kind, "declared")["kind"])
        for not_a_boundary in ("fading", "control-change", "new-approval"):
            with self.assertRaises(LiveConsoleError) as caught:
                sitting.declare_boundary(not_a_boundary, "not a boundary")
            self.assertIn("never reset a budget", str(caught.exception))

    def test_a_predeclared_boundary_is_on_the_episode(self) -> None:
        sitting = self.build(
            method=METHOD_DETERMINISTIC, budget_trials=1,
            boundaries=({"kind": "exogenous", "at": "2026-09-08T00:00:00Z",
                         "detail": "the interferer was switched on"},))
        sitting.confirm()
        sitting.run()
        record = sitting.episode_record()
        self.assertEqual([{"at": "2026-09-08T00:00:00Z", "kind": "exogenous",
                           "detail": "the interferer was switched on",
                           "invalidatesTC": False}],
                         record["boundaries"])

    def test_unstamped_initial_boundaries_use_the_preparation_declaration_time(self) -> None:
        for mode in (TIMING_PREPARED, TIMING_COLD_START):
            with self.subTest(mode=mode):
                resolver, models = self.scripted({
                    "target": [slow(T_ANSWER, SLOW_PREPARATION_MS)],
                    "control": [C_ANSWER], "trajectory": [PAIR]})
                rows = ({"kind": "exogenous", "detail": "initial trigger"},
                        {"kind": "policy", "at": "", "detail": "initial policy"},
                        {"kind": "intent", "at": " \t", "detail": "initial intent"},
                        {"kind": "exogenous", "at": None, "detail": "initial input"})
                sitting = self.build(resolver=resolver, role_models=models,
                                     budget_trials=1, timing_mode=mode, boundaries=rows)
                self.assertLess(sitting.timing["prepStart"], sitting.timing["prepEnd"])
                self.assertEqual([sitting.timing["prepStart"]] * len(rows),
                                 [row["at"] for row in sitting.boundaries])
                self.assertEqual(["", "", " \t", ""],
                                 [row["at"] for row in sitting.request.boundaries])
                self.assertEqual(1, sitting.request.budget_trials)
                self.assertEqual([], sitting.grid.trials)
                if mode == TIMING_COLD_START:
                    self.assertEqual(sitting.timing["prepStart"], sitting.timing["t0"])
                else:
                    self.assertNotIn("t0", sitting.timing)

    def test_initial_boundary_is_not_restamped_at_the_prepared_trigger(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=1,
                             initial_measurement=True,
                             boundaries=({"kind": "exogenous", "detail": "initial trigger"},))
        declared_at = sitting.timing["prepStart"]
        sitting.clock.sleep_ms(500)
        sitting.confirm()
        sitting.run()
        record = sitting.episode_record()
        self.assertEqual(declared_at, record["boundaries"][0]["at"])
        self.assertGreater(record["timing"]["t0"], declared_at)
        self.assertGreaterEqual(record["trials"][0]["window"]["start"], declared_at)
        self.assertFalse(record["trials"][0]["counted"])
        self.assertEqual(1, sitting.request.budget_trials)

    def test_explicit_initial_boundary_times_are_preserved_in_both_modes(self) -> None:
        timestamp = "2026-09-08T00:00:00Z"
        for mode in (TIMING_PREPARED, TIMING_COLD_START):
            with self.subTest(mode=mode):
                sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=1,
                                     timing_mode=mode, boundaries=(
                                         {"kind": "exogenous", "at": timestamp,
                                          "detail": "explicit source time"},))
                self.assertEqual(timestamp, sitting.boundaries[0]["at"])
                self.assertEqual(timestamp, sitting.request.boundaries[0]["at"])

    def test_dynamic_and_unknown_historical_boundaries_are_not_restamped(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=1,
                             boundaries=({"kind": "exogenous", "detail": "initial"},))
        initial = dict(sitting.boundaries[0])
        sitting.clock.sleep_ms(300)
        declared_at = sitting.clock.now()
        dynamic = sitting.declare_boundary("exogenous", "actual later change")
        self.assertEqual(declared_at, dynamic["at"])
        historical = {"kind": "exogenous", "at": "", "detail": "unresolved old event",
                      "invalidatesTC": False}
        sitting.boundaries.append(historical)
        sitting.clock.sleep_ms(300)
        record = sitting.episode_record()
        self.assertEqual(initial, record["boundaries"][0])
        self.assertEqual(declared_at, record["boundaries"][1]["at"])
        self.assertEqual("", record["boundaries"][2]["at"])
        self.assertEqual("", historical["at"])

    def test_an_exogenous_boundary_marks_what_came_before_it_and_resets_nothing(self) -> None:
        declared: List[Dict[str, Any]] = []
        # A floor 131 cannot reach on this cell, so the walk keeps going after
        # the boundary instead of ending at T0 on its first trial.
        sitting = self.build(["I1: UE ueId=131 needs at least 4.5 Mbps downlink, "
                              "relaxable to 2.0", I2],
                             method=METHOD_DETERMINISTIC, budget_trials=3)
        sitting.confirm()

        def on_trial(trial: Mapping[str, Any]) -> None:
            if not declared:
                declared.append(sitting.declare_boundary(
                    "exogenous", "the offered load rose on the neighbour cell"))
                declared.append({"startedMs": sitting.started_ms,
                                 "budget": int(sitting.request.budget_trials)})

        summary = sitting.run(on_trial=on_trial)
        # The budget and the clock are exactly what they were: a boundary is
        # recorded, never a reset.
        self.assertEqual(declared[1]["startedMs"], sitting.started_ms)
        self.assertEqual(declared[1]["budget"], int(sitting.request.budget_trials))
        self.assertGreater(summary["trials"], 1)
        record = sitting.episode_record()
        at = record["boundaries"][0]["at"]
        self.assertEqual(at, record["trials"][0]["beforeBoundary"])
        for trial in record["trials"][1:]:
            self.assertNotIn("beforeBoundary", trial)

    def test_an_intent_boundary_stops_the_search_because_t_and_c_no_longer_hold(self) -> None:
        sitting = self.build(method=METHOD_DETERMINISTIC, budget_trials=3)
        sitting.confirm()

        def on_trial(trial: Mapping[str, Any]) -> None:
            if not sitting.boundaries:
                sitting.declare_boundary("intent", "the owner rewrote I2")

        summary = sitting.run(on_trial=on_trial)
        self.assertEqual(EPISODE_BOUNDARY, summary["termination"])
        self.assertIn("re-formed by composing the next sitting", summary["detail"])
        self.assertEqual(1, summary["trials"])
        self.assertTrue(sitting.boundaries[0]["invalidatesTC"])

    def test_a_request_with_an_unknown_boundary_kind_is_refused(self) -> None:
        with self.assertRaises(LiveConsoleError) as caught:
            AgentRequest(sentences=(I1,), boundaries=({"kind": "fading"},))
        self.assertIn("intent, policy, exogenous", str(caught.exception))


class ThePreparedBoardSeam(AgentSittingFixture):
    """Contract v4 section 4: the same T and C, byte for byte, across methods."""

    def formed(self):
        resolver, models = self.scripted({
            "target": [T_ANSWER], "control": [C_ANSWER], "trajectory": [PAIR]})
        return self.build(resolver=resolver, role_models=models, budget_trials=2)

    def test_an_injected_board_skips_the_target_and_the_control_calls(self) -> None:
        first = self.formed()
        # Only a trajectory answer is scripted: if this sitting formed its own
        # T or C the scripted resolver would have nothing to answer with.
        resolver, models = self.scripted({"trajectory": [PAIR]})
        second = self.build(resolver=resolver, role_models=models, budget_trials=2,
                            prepared=(first.contract, first.controls))
        roles = [call.role for call in second.agents.calls]
        self.assertNotIn("target", roles)
        self.assertNotIn("control", roles)
        self.assertTrue(second.prepared["injected"])
        self.assertFalse(first.prepared["injected"])
        # The board is the same board, and its hashes say so on both episodes.
        self.assertEqual(first.prepared["tHash"], second.prepared["tHash"])
        self.assertEqual(first.prepared["cHash"], second.prepared["cHash"])
        self.assertEqual(first.contract.target_ids, second.contract.target_ids)
        self.assertEqual(first.controls.control_ids, second.controls.control_ids)
        self.assertEqual(second.prepared, second.episode_record()["prepared"])
        self.assertTrue(second.episode_record()["preparedInjected"])

    def test_the_injected_board_is_what_the_trajectory_then_searches(self) -> None:
        first = self.formed()
        resolver, models = self.scripted({"trajectory": [PAIR]})
        second = self.build(resolver=resolver, role_models=models, budget_trials=1,
                            prepared=(first.contract, first.controls))
        second.confirm()
        summary = second.run()
        self.assertEqual("C1", summary["grid"]["controls"][1])
        self.assertEqual(1, summary["trials"])
        self.assertEqual("C1", second.grid.trials[0].control_id)

    def test_an_injected_c_the_frozen_catalog_will_not_admit_is_refused(self) -> None:
        first = self.formed()
        unadmitted = replace(first.controls, candidates=first.controls.candidates + (
            ControlCandidate(control_id="CX",
                             configuration={"servingCell@131": "99999999",
                                            "servingCell@132": HOME_NCI},
                             applicability=("a cell this deployment never advertised",)),))
        with self.assertRaises(LiveConsoleError) as caught:
            self.build(method=METHOD_DETERMINISTIC, budget_trials=1,
                       prepared=(first.contract, unadmitted))
        self.assertIn("does not admit", str(caught.exception))
        self.assertIn("servingCell@131=99999999", str(caught.exception))


class SharedPrerequisiteFailures(AgentSittingFixture):
    """Reply 2026-09-14 section 7: a failure every candidate shares returns
    through the common stop path; a candidate-specific one does not."""

    #: Goals the emulator cannot reach, so no trial ends the search by meeting
    #: one and the loop is free to show what it does with a failure.  Four
    #: candidates, so "stopped early" and "tried them all" are far apart.
    UNREACHABLE = ("I3: UE ueId=131 needs at least 9.0 Mbps downlink, "
                   "non-relaxable",
                   "I4: UE ueId=132 needs at least 9.0 Mbps downlink, "
                   "non-relaxable")

    def _drive(self, outcome: Any, stop_reason: Any, *, budget: int = 8):
        from assurance.core.states import TrialState

        sitting = self.build(self.UNREACHABLE, method=METHOD_DETERMINISTIC,
                             budget_trials=budget)
        sitting.confirm()
        run = sitting.runtime.run_candidate

        def failing(*args: Any, **kwargs: Any):
            trial_id, report = run(*args, **kwargs)
            return trial_id, replace(report,
                                     terminal_state=TrialState.SETTLED_NON_SUCCESS,
                                     outcome=outcome, stop_reason=stop_reason)

        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch.object(sitting.runtime, "run_candidate",
                             side_effect=failing) as calls:
            sitting.run()
        return sitting, calls

    def test_an_execution_error_every_candidate_hits_stops_the_episode(self) -> None:
        from assurance.core.axes import TrialOutcome

        sitting, calls = self._drive(TrialOutcome.EXEC_ERROR, None)
        # The second candidate reproducing it is the proof that it is shared:
        # the remaining four trials of the budget are not spent on it.
        self.assertEqual(2, calls.call_count)
        self.assertEqual("EXECUTION_FAILURE", sitting.termination)
        self.assertIn("every candidate", sitting.termination_detail)

    def test_a_partial_apply_with_nothing_staged_is_shared_too(self) -> None:
        from assurance.core.axes import TrialOutcome
        from assurance.core.states import StopReason

        sitting, calls = self._drive(TrialOutcome.SAFETY_STOPPED,
                                     StopReason.PARTIAL_APPLY)
        self.assertEqual(2, calls.call_count)
        self.assertEqual("EXECUTION_FAILURE", sitting.termination)

    def test_a_candidate_specific_rejection_still_tries_the_next_candidate(self) -> None:
        from assurance.core.axes import TrialOutcome
        from assurance.core.states import StopReason

        sitting, calls = self._drive(TrialOutcome.FAIL,
                                     StopReason.SEMANTIC_NON_SUCCESS)
        # A configuration that simply did not meet its requirement says nothing
        # about the next one, so the search goes on until the catalog or the
        # budget runs out -- never through EXECUTION_FAILURE.
        self.assertGreater(calls.call_count, 2)
        self.assertIn(sitting.termination,
                      ("CATALOG_EXHAUSTED", "BUDGET_EXHAUSTED"))

    def test_one_execution_failure_on_its_own_does_not_end_the_episode(self) -> None:
        """The first one may still be this candidate's own fault."""
        from assurance.core.axes import TrialOutcome
        from assurance.core.states import TrialState

        sitting = self.build(self.UNREACHABLE, method=METHOD_DETERMINISTIC,
                             budget_trials=8)
        sitting.confirm()
        run = sitting.runtime.run_candidate
        seen: List[int] = []

        def once(*args: Any, **kwargs: Any):
            trial_id, report = run(*args, **kwargs)
            seen.append(1)
            if len(seen) == 1:
                return trial_id, replace(
                    report, terminal_state=TrialState.SETTLED_NON_SUCCESS,
                    outcome=TrialOutcome.EXEC_ERROR, stop_reason=None)
            return trial_id, report

        with patch.object(sitting.runtime, "run_candidate", side_effect=once) as calls:
            sitting.run()
        self.assertGreater(calls.call_count, 2)
        self.assertNotEqual("EXECUTION_FAILURE", sitting.termination)


class TheDeadlineReachesThePredictor(unittest.TestCase):
    """핸드오프 §7: 마감값이 network_state 뿐 아니라 **예측기**에도 간다.

    18:0x 수정은 network_state 에만 넣어 effect_evidence.echoDeadlineMs 가 계속 {} 였고
    마감 비율 예측이 한 번도 나오지 않았다.  둘은 이제 ``intent_deadlines`` 한 곳에서 뽑는다.
    """

    def test_a_deadline_intent_puts_its_deadline_into_the_evidence(self):
        from dataclasses import replace as _replace
        from assurance.coordination.tc import Intent
        from assurance.coordination import JointEffectPredictor, NetworkState
        from tools.liveconsole.agent import intent_deadlines
        i2d = Intent.from_record({
            "intentId": "I2d", "owner": "ue2-map", "priority": 2, "ueId": "330", "weight": 2.0,
            "requirement": {"reqId": "I2d.r1", "kpi": "deadlineSuccessRatio", "op": ">=",
                            "value": 0.9, "bound": 0.9, "steps": 0, "unit": "ratio",
                            "deadlineMs": 2000, "deadlineBound": 3000, "deadlineSteps": 2}})
        self.assertEqual({"330": 2000.0}, intent_deadlines([i2d]))
        state = NetworkState(cells={"12345678": 14.5}, ues={"330": "12345678"},
                             offered_load_mbps={"330": 8.0})
        state = _replace(state, echo_deadline_ms=intent_deadlines([i2d]))
        evidence = JointEffectPredictor(state).effect_evidence({}, {})
        self.assertEqual({"330": 2000.0}, evidence["predictorDescription"]["echoDeadlineMs"])
