"""Target, Control and Trajectory: three single-call agents on three models.

The owner's triad.  **Target** turns the intents and the owner's authorization
into ``T``; **Control** turns ``T`` and the deployment's action space into
``C``; **Trajectory** picks the next ``(target, control)`` cell of the board.
Each is exactly **one** model call per decision, each on a model the operator
chose in the Cockpit -- that is the paper's "model-agnostic" claim made
concrete, and the role separation whose contribution the ablation measures.

Two comparison arms live here too.  The **internal monolith** is one model
doing the same work in two kinds of call (a formation call producing ``T`` and
``C``, then a selection call per decision).  The **basic monolith** is one
model, one call per decision, with no grid: it never receives our ``T``/``C``,
our rankings or a trajectory recommendation, and answers with a configuration
plus the requirements it aims at.  The **deterministic** arm is the fallbacks
alone -- no model at all.

Boundaries, from ``orc_task/SINGLE_CALL.md``:

* the system prompt is the role's English instruction, verbatim;
* the user prompt is ``INPUTS:`` JSON, ``OUTPUT SCHEMA:`` JSON, and the
  instruction to return only the JSON object;
* **no** input ever carries the remaining trial count, the deadline, the
  budget or a termination option.  Observation timestamps, window validity and
  the currently applied configuration are operational facts and stay.

Every answer is parsed, validated against the role's schema and checked
against the **closed sets** it was given (a target must stay inside the
owner's signed limits; a control value must exist on its axis; an id must
resolve to something in the call's own inputs).  A refused answer gets exactly
one repair retry that appends the validation error; if that fails too, the
deterministic rule answers and the record says why.  A model can therefore
never invent a target, an axis value or a candidate, and never issues a
verdict: judging is :func:`assurance.coordination.tc.judge` over measured
KPIs, and the Kernel still admits, permits, settles and rolls back.
"""

from __future__ import annotations

import inspect
import hashlib
import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

#: The project's one "what did the provider actually report" reader, reused
#: rather than reimplemented here.  It returns ``None`` for an absent or
#: unparseable count and keeps a genuine provider-reported 0 as 0 -- exactly
#: the distinction this module needs, and already covered by
#: ``tests/assurance/test_strat_failclosed.py``.  Imported (not moved) so the
#: advisory transport and this executor cannot drift apart on what a missing
#: count means.  ``assurance.advisors`` imports nothing from
#: ``assurance.coordination``, so this direction adds no cycle.
from assurance.advisors.strategies.transport import _reported_count
from assurance.coordination.intake import GenerationOptions, choose_from_calibration
from assurance.coordination.predictor import JointEffectPredictor, NetworkState
from assurance.coordination.tc import (
    Authorization, BasicDecision, CompatibilityRules, ControlCandidate,
    ControlCandidates, ControlValidationError, FunctionCatalog, FunctionSelection,
    Grid, Intent, Observation, Preference, RULE_BASELINE, Target, TargetContract,
    TargetValidationError, TrajectoryDecision, cost_of, preference_key,
    catalog_product_controls, cover_controls, deterministic_function_moves,
    deterministic_trajectory,
    expand_targets, mandatory_contract, omega_or_sparse, translate_functions,
    validate_control_candidates,
    validate_target_contract,
)

__all__ = [
    "AnswerRefused",
    "BasicInputs",
    "CallRecord",
    "ClarificationNeeded",
    "ControlInputs",
    "DEFAULT_ROLE_MODELS_FILENAME",
    "DETERMINISTIC",
    "METHODS",
    "METHOD_BASIC_MONOLITH",
    "METHOD_DETERMINISTIC",
    "METHOD_INTERNAL_MONOLITH",
    "METHOD_THREE_AGENT",
    "METHOD_THREE_AGENT_COVERAGE",
    "COVERAGE_METHODS",
    "CONTROL_COVERAGE_SYSTEM_PROMPT",
    "MonolithFormInputs",
    "PHASES",
    "PHASE_CLARIFICATION",
    "PHASE_FORMATION",
    "PHASE_INTAKE",
    "PHASE_RETENTION",
    "PHASE_SELECTION",
    "PROMPT_SOURCE_HEADINGS",
    "ROLES",
    "ROLE_CONTROL",
    "ROLE_MONOLITH",
    "ROLE_TARGET",
    "ROLE_TRAJECTORY",
    "ROLE_MODELS_SCHEMA",
    "RoleAgents",
    "RoleModels",
    "ScriptedResolver",
    "SYSTEM_PROMPTS",
    "TargetInputs",
    "TrajectoryInputs",
    "budget_terms_in",
    "default_resolver",
    "load_role_models_file",
    "save_role_models_file",
]

ROLE_TARGET = "target"
ROLE_CONTROL = "control"
ROLE_TRAJECTORY = "trajectory"
ROLE_MONOLITH = "monolith"
ROLES: Tuple[str, ...] = (ROLE_TARGET, ROLE_CONTROL, ROLE_TRAJECTORY, ROLE_MONOLITH)

METHOD_THREE_AGENT = "three-agent"
METHOD_INTERNAL_MONOLITH = "internal-monolith"
METHOD_BASIC_MONOLITH = "basic-monolith"
METHOD_DETERMINISTIC = "deterministic"
#: The monolith "model" name that runs the rule-greedy baseline instead of a model.
from assurance.coordination.rule_greedy import RULE_GREEDY  # noqa: E402
#: The same three roles, with **one** substitution: the Control agent is asked
#: for a C that spans the executable combinations instead of the top ones by
#: predicted concession cost.  Everything else -- Target, Trajectory, the
#: inputs, the schema, the retained count, the Kernel -- is identical, so a
#: difference between this and ``three-agent`` is attributable to that one
#: instruction.  Its Control prompt is **ours**, written for this comparison;
#: ``SINGLE_CALL.md``'s text stays untouched and remains what ``three-agent``
#: sends (owner, 2026-09-08).
METHOD_THREE_AGENT_COVERAGE = "three-agent-coverage"
METHODS: Tuple[str, ...] = (METHOD_THREE_AGENT, METHOD_THREE_AGENT_COVERAGE,
                            METHOD_INTERNAL_MONOLITH, METHOD_BASIC_MONOLITH,
                            METHOD_DETERMINISTIC)

#: The methods whose Control role spans instead of ranking.
COVERAGE_METHODS: Tuple[str, ...] = (METHOD_THREE_AGENT_COVERAGE,)

#: The model name that means "no LLM for this role; use the deterministic rule".
DETERMINISTIC = "deterministic"

#: Where the Cockpit leaves the operator's choice for the next sitting,
#: relative to the runs root.
DEFAULT_ROLE_MODELS_FILENAME = "agent-role-models.json"
ROLE_MODELS_SCHEMA = "agent-role-models/2.0.0"

#: The phases a call can belong to (contract v2, episode record 1.1.0).
PHASE_INTAKE = "intake"
PHASE_FORMATION = "formation"
PHASE_CLARIFICATION = "clarification"
PHASE_SELECTION = "selection"
PHASE_RETENTION = "retention"
PHASES: Tuple[str, ...] = (PHASE_INTAKE, PHASE_FORMATION, PHASE_CLARIFICATION,
                           PHASE_SELECTION, PHASE_RETENTION)


# --------------------------------------------------------------------------- #
# the verbatim role instructions (orc_task/SINGLE_CALL.md)
# --------------------------------------------------------------------------- #

TARGET_SYSTEM_PROMPT = (
    "You prepare a fixed set T of owner-authorized RAN targets. Read input.intents, input.authorization, and input.network_state.\n"
    "\n"
    "The code includes the unmodified original target T0 and input.mandatory_targets. Preserve these anchors, requirement identities, authorized levels, service floors, and joint conditions. Follow the exact concession definitions and preference ordering in input.authorization. Keep protected requirements fixed.\n"
    "\n"
    "In alternatives, return up to six additional distinct authorized level vectors, excluding the anchors, and aim to fill that limit with useful ones; return fewer only when fewer distinct authorized vectors exist. Use intent semantics, owner preferences, and initial service state to choose useful intermediate concessions and alternative tradeoffs. Unknown control effects do not establish infeasibility. Do not force a cumulative relaxation path or diversity across requirements.\n"
    "\n"
    "Return only the required JSON fields. Include alternatives even when empty. For each added target, provide its level vector, one brief reason, and an empty evidenceRefs array. Do not repeat the intent or authorization objects."
)

from assurance.coordination.tc import MAX_MODEL_ADDITIONS as _MAX_ADDITIONS
from assurance.coordination.tc import (policy_ranges, ranged_policy_field, snap_configuration,
                                        snap_function_rows)
if _MAX_ADDITIONS != 6:   # v4.7 AIC_T_CAP: say the number the validator admits
    _WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 7: "seven", 8: "eight"}
    TARGET_SYSTEM_PROMPT = TARGET_SYSTEM_PROMPT.replace(
        "up to six additional", f"up to {_WORDS.get(_MAX_ADDITIONS, _MAX_ADDITIONS)} additional")

CONTROL_SYSTEM_PROMPT = (
    "You construct a fixed set C of joint RAN control configurations. Read input.intents and input.authorization to identify the declared requirements and their authorized alternatives. Each configuration specifies the participating functions, their policies, and their scopes.\n"
    "\n"
    "Use function descriptions, current network state, and measured effect evidence to identify how each configuration may change resource use and affect target KPIs. Assess the combined configuration, including interactions among functions and effects on other users sharing resources. Use the supplied KPI-deficit rules to interpret measured shortfalls.\n"
    "\n"
    "Aim to fill the configured candidate limit with useful, distinct joint-control configurations across the declared requirements and their authorized alternatives. Select combinations and control levels for the distinct effects, tradeoffs, or informative tests they offer. Include the reference configuration within this limit.\n"
    "\n"
    "Respect catalog bounds, prerequisites, and compatibility rules. Specify each configuration relative to the common reference. Avoid duplicate applied configurations.\n"
    "\n"
    "Return only the required JSON fields. For each control candidate, report related KPIs without predicted values, relevant existing observation IDs when available, and one brief rationale explaining its intended qualitative effect and reason for inclusion."
)

#: Ours, not ``SINGLE_CALL.md``'s: the coverage comparison's Control
#: instruction.  It differs from ``CONTROL_SYSTEM_PROMPT`` in exactly one
#: respect -- what to do when there are more executable combinations than the
#: retained count -- so the comparison isolates that choice.
CONTROL_COVERAGE_SYSTEM_PROMPT = (
    "Read T from input.target_contract. Construct C as joint configurations. "
    "Each candidate selects functions to use together and specifies a policy "
    "and scope for each. Respect the function catalog, policy bounds, "
    "compatibility rules, and current state. Use the supplied control-KPI "
    "associations and measured effect evidence, and infer the effects yourself, "
    "to evaluate combinations against the complete target contract T. There are more executable "
    "combinations than you may retain, so make the retained set span them: "
    "cover the distinct sets of functions and scopes that can move, and "
    "spread the policy values you choose across each function's permitted "
    "range, rather than filling the set with the combinations of lowest "
    "predicted concession cost. Keep the combinations predicted to satisfy a "
    "target, and keep informative ones whose predicted outcome is uncertain. "
    "Retain the configured number of candidates. Attach uncertainty and "
    "applicability conditions. Mark unsupported estimates as unknown. Return "
    "C in the supplied JSON schema with a brief rationale."
    " Each candidate's predicted values appear once; do not restate them under another name. evidenceRefs are identifiers, not sentences. Keep applicability and rationale to one short clause each."
    ' Let the candidates exercise the different function families the catalog exposes rather than leaving one of them at its baseline throughout, and keep the ranking by lowest concession cost exactly as it is.'
)


TRAJECTORY_SYSTEM_PROMPT = (
    "You select one currently applicable control from the fixed candidates C. Use T, current network state, the candidates' related KPIs and rationales, valid observation history, and execution-error records. Keep T and C fixed.\n"
    "\n"
    "Follow the supplied owner preference. Use the evaluator's best supported target over the entire owner-authorized range as the current result. If none is supported, seek a preferred attainable target. Otherwise, seek a more preferred result.\n"
    "\n"
    "Use candidate rationales to compare plausible improvements and tradeoffs. Give applicable valid observations priority over the candidates' initial rationales. Consider informative trials when uncertainty limits the choice, within the supplied operating constraints. Distinguish measured requirement failures from execution or observation errors. Do not select a control that has already been tried.\n"
    "\n"
    "Return only the required JSON fields containing one control ID, an intended target ID from T, and one brief rationale. The intended target identifies the trial's purpose. The evaluator assesses the observation over the entire owner-authorized range."
)

MONOLITH_FORM_SYSTEM_PROMPT = (
    "You jointly prepare a fixed target set T and a fixed set C of joint RAN control configurations. Read input.intents, input.authorization, and input.network_state to identify the declared requirements and their authorized alternatives. Each configuration specifies the participating functions, their policies, and their scopes.\n"
    "\n"
    "The code includes the unmodified original target T0 and input.mandatory_targets. Preserve these anchors, requirement identities, authorized levels, service floors, and joint conditions. Follow the exact concession definitions and preference ordering in input.authorization. Keep protected requirements fixed. In alternatives, return up to six additional distinct authorized level vectors, excluding the anchors, and aim to fill that limit with useful ones; return fewer only when fewer distinct authorized vectors exist. Use intent semantics, owner preferences, and initial service state to choose useful intermediate concessions and alternative tradeoffs. Unknown control effects do not establish infeasibility. Do not force a cumulative relaxation path or diversity across requirements.\n"
    "\n"
    "Use function descriptions, current network state, and measured effect evidence to identify how each configuration may change resource use and affect target KPIs. Assess the combined configuration, including interactions among functions and effects on other users sharing resources. Use the supplied KPI-deficit rules to interpret measured shortfalls.\n"
    "\n"
    "Aim to fill the configured candidate limit with useful, distinct joint-control configurations across the declared requirements and their authorized alternatives. Select combinations and control levels for the distinct effects, tradeoffs, or informative tests they offer. Include the reference configuration within this limit.\n"
    "\n"
    "Respect catalog bounds, prerequisites, and compatibility rules. Specify each configuration relative to the common reference. Avoid duplicate applied configurations.\n"
    "\n"
    "Return only the required JSON fields. Include alternatives even when empty. For each added target, provide its level vector, one brief reason, and an empty evidenceRefs array. For each control candidate, report related KPIs without predicted values, relevant existing observation IDs when available, and one brief rationale explaining its intended qualitative effect and reason for inclusion. Do not repeat the intent or authorization objects."
)
if _MAX_ADDITIONS != 6:   # the IM formation prompt promises the same number as 3A's Target
    MONOLITH_FORM_SYSTEM_PROMPT = MONOLITH_FORM_SYSTEM_PROMPT.replace(
        "up to six additional", f"up to {_WORDS.get(_MAX_ADDITIONS, _MAX_ADDITIONS)} additional")


def prompt_with_additions(prompt: str, authorization) -> str:
    """The Target / monolith-formation prompt stating the number of additions the validator
    admits for this authorization (``tc.model_addition_limit``) -- one value for both."""
    from assurance.coordination.tc import model_addition_limit
    n = model_addition_limit(authorization)
    if n == _MAX_ADDITIONS:
        return prompt
    said = _WORDS.get(_MAX_ADDITIONS, _MAX_ADDITIONS) if _MAX_ADDITIONS != 6 else "six"
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
             8: "eight", 9: "nine"}
    return prompt.replace(f"up to {said} additional", f"up to {words.get(n, n)} additional")

# 2026-09-17 (오너 드롭 RAN_AGENT_PROMPTS_REVISED_20260917.md):
# "Keep 3A-Trajectory and IM-Select identical."  복사본을 두면 언젠가 한쪽만
# 고쳐진다 -- **별칭**으로 두어 드리프트를 구조적으로 불가능하게 한다.
MONOLITH_SELECT_SYSTEM_PROMPT = TRAJECTORY_SYSTEM_PROMPT

# 2026-09-17 (오너 지시): 세 팔을 **같은 지시** 아래 비교하기 위해 반복 회피 문장을 더했다.
# `three-agent`(trajectory)와 `internal-monolith`(선택)는
# "Avoid repeating controls with still-valid observations." 를 받는데 여기에만 없었다.
# basic 에는 후보 ID(`controls`)가 없고 대신 `input.tried_configurations` 를 받으므로
# **같은 뜻을 이 팔의 어휘로** 옮겼다.  이 문장이 없던 동안의 basic 판은 비교에서 제외한다.
BASIC_MONOLITH_SYSTEM_PROMPT = (
    "Decide which xApps to run together and their policies and scopes to satisfy the most preferred owner-authorized requirements under the supplied operating constraints. Use the intents, authorization, exact owner preference, xApp capabilities and interactions, current network state, and measured effect evidence; infer yourself which KPIs each xApp affects.\n"
    "\n"
    "Use accumulated valid observations, execution-error records, and the evaluator's best supported result over the entire authorized range. If no authorized combination of requirements has been satisfied, seek a preferred attainable one. Otherwise, seek a more preferred result. Consider uncertainty and informative trials when useful. You may reuse previous calculations.\n"
    "\n"
    "Respect authorized requirement levels, service floors, joint conditions, policy bounds, and compatibility rules. Distinguish measured requirement failures from execution or observation errors. Estimates and errors do not establish satisfaction or infeasibility. Do not select a configuration listed in tried_configurations.\n"
    "\n"
    "Return only the required JSON fields: one set of xApp instructions with policies and scopes, and one brief rationale."
)

SYSTEM_PROMPTS: Dict[str, str] = {
    ROLE_TARGET: TARGET_SYSTEM_PROMPT,
    ROLE_CONTROL: CONTROL_SYSTEM_PROMPT,
    ROLE_TRAJECTORY: TRAJECTORY_SYSTEM_PROMPT,
    "monolith-form": MONOLITH_FORM_SYSTEM_PROMPT,
    "monolith-select": MONOLITH_SELECT_SYSTEM_PROMPT,
    "basic-monolith": BASIC_MONOLITH_SYSTEM_PROMPT,
}

#: The headings of ``orc_task/SINGLE_CALL.md`` each prompt is copied from; the
#: test reads the file and compares, so the two can never drift apart.
PROMPT_SOURCE_HEADINGS: Dict[str, str] = {
    ROLE_TARGET: "Target agent",
    ROLE_CONTROL: "Control agent",
    ROLE_TRAJECTORY: "Trajectory agent",
    "monolith-form": "내부 monolith 구성 호출",
    "monolith-select": "내부 monolith 선택 호출",
    "basic-monolith": "기본 monolith",
}


# --------------------------------------------------------------------------- #
# the output schemas handed to the model
# --------------------------------------------------------------------------- #

#: 핸드오프 2026-09-18 §4.1: T0 본문은 **모델이 되받아 적지 않는다** -- 코드가 원본
#: 인텐트·인가로 만든다(`_validated_contract`).  그래서 두 형성 스키마에서 ``t0`` 를 뺐다.
_TARGET_T0_SHAPE: Dict[str, Any] = {"targetId": "T0",
                                    "requirements": {"<reqId>": "<original value>"}}
# 2026-09-19 (오너: "프롬프트는 어떻게 되어 있는데?"): 지시문은 오너가 허용한 단계를
# **보존하라**고 하는데 이 형식은 ``levels`` 로 단계·한계를 다시 적게 했고, 실행기는
# 오너 입력 대신 그 값으로 허용 범위를 만들었다.  판 20260919T151448 의 Target 은
# bound 를 원래값(9.0)으로 적어 완화 목표가 전부 사라졌고, 네 번 다시 물어 222 초가
# 걸렸다.  허용 범위는 input.authorization 이 정한다 -- 모델은 대안만 낸다.
_TARGET_SCHEMA: Dict[str, Any] = {
    # ``alternatives`` are the model's ADDITIONAL targets only (v3.1,
    # amendment section 3.3; eight for v4, lowered to six on 2026-09-22 by the
    # owner so the instruction matches what the validator can admit:
    # `min(MAX_MODEL_ADDITIONS, MAX_TARGETS - 1 - len(boundary))` is 6 with the
    # three boundary targets this corpus carries, and the model kept losing
    # boards by obeying the old "eight"): at most
    # :data:`MAX_MODEL_ADDITIONS` distinct authorized vectors besides the
    # mandatory targets code always includes.  ``reason`` and
    # ``evidenceRefs`` are what the amendment asks each addition to carry.  Both
    # are **advisory**: recorded in ``provenance["modelSelection"]``, never read
    # back to decide membership, ordering, validation or any verdict.  The
    # 4/3/2 ``selectionRole`` categories are superseded and no longer requested.
    "alternatives": [{"targetId": "<id>",
                      "levels": {"<reqId>": "<relaxation level index>"},
                      "deadlineLevels": {"<reqId>": "<0 unless this alternative "
                                                    "also extends that measurement>"},
                      "reason": "<one short reason for adding it>",
                      "evidenceRefs": ["<existing evidence id, not a sentence>"]}],
    # 2026-09-20 (오너: "확인 질문이랑 제약 둘 다 빼"): ``constraints`` was a
    # restatement nothing read, and ``missingInformation`` let a model stop an
    # unattended board on a question nobody would answer.  Both are gone.
    # ``costRule`` is gone: ranking is :func:`preference_key` over the owners'
    # normalized concessions, and ``cost_of`` is recorded but explicitly does
    # not order.  Asking the model to restate the rule named in
    # ``input.authorization`` bought an echo of a defunct quantity and read as
    # though the answer had authority over the ordering.  ``from_compact``
    # falls back to ``authorization.preference.cost_rule`` when the key is
    # absent, so the recorded contract is unchanged.
    # ``ranking`` is gone too (2026-09-19 sweep): the order is the owner's
    # (input.authorization); a tie-break written here used to replace it.
    "rationale": "<one brief rationale>",
}

_CONTROL_SCHEMA: Dict[str, Any] = {
    "candidates": [{
        "controlId": "<new id>",
        "functions": [{"functionId": "<one functionId of the catalog>",
                       "scope": "<one scope that function serves>",
                       "policy": {"<policy field>": "<one listed value>"}}],
        # 2026-09-20 (owner): relations only -- no predicted value, no
        # uncertainty, no target judgement.
        "relatedKpis": ["<KPI key such as dlGoodputMbps@ue2 this configuration "
                        "is related to>"],
        "evidenceRefs": ["<an observation id, not a sentence>"],
        "rationale": "<intended qualitative effect and reason for inclusion, one sentence>"}],
    "rationale": "<one brief rationale>",
}

_PAIR_SCHEMA: Dict[str, Any] = {
    "controlId": "<one controlId out of C>",
    "targetId": "<one targetId out of T>",
    "rationale": "<one brief rationale>",
}

_MONOLITH_FORM_SCHEMA: Dict[str, Any] = {
    # The internal monolith selects its own T under the same rights as Target.
    # Leaving this field out was the asymmetry that made its prompt ask for ten
    # alternatives with nowhere to put them: the answer validated, the executor
    # read "did not narrow" and kept the whole expansion, and the provenance
    # still named the model.  Both prepared arms carry the field.
    "alternatives": _TARGET_SCHEMA["alternatives"],
    "candidates": _CONTROL_SCHEMA["candidates"],
    "rationale": "<one brief rationale>",
}

#: Required policy content and a short rationale -- nothing else.  The
#: requirement restatement is gone: the basic monolith was made to copy back
#: every intent's reqId/kpi/op/value/unit, and no executor read ever used it.
#: ``_next_decision`` maps its configuration onto a column and judges it
#: against ``T0``, so what it "aims at" is the originals by construction.
#: Asking for the copy only bought tokens and one more way to be refused.
_BASIC_SCHEMA: Dict[str, Any] = {
    "instructions": [{"functionId": "<one functionId of the catalog>",
                      "scope": "<one scope that function serves>",
                      "policy": {"<policy field>": "<one listed value>"}}],
    "rationale": "<one brief rationale>",
}


#: Words that must never reach a model input (``SINGLE_CALL.md``, executor
#: boundaries).  This is a diagnostic for tests and for the executor's own
#: self-check, not a gate: the payload builders below simply have no field to
#: carry them.
_BUDGET_TERMS: Tuple[str, ...] = (
    "remaining trial", "trials remaining", "trialsk", "trialsK", "budget",
    "deadline", "time limit", "horizon", "terminate", "termination",
    "stop condition", "maxtrials", "max trials", "elapsed budget",
)


def budget_terms_in(text: str) -> List[str]:
    """Which forbidden budget/deadline words appear in a prompt, if any.

    Whole words only.  An owner's KPI may legitimately be *named* after a
    deadline -- the scenario's I4 is "the fraction of tagged echo requests
    completed within D1", carried as ``deadlineSuccessRatio`` and
    ``echoDeadlineMs`` -- and that is a service requirement the model must see.
    What it must not see is the *experiment's* deadline B, which would appear
    as the bare word.  Matching on substrings could not tell the two apart and
    refused the KPI along with the budget.
    """
    lowered = str(text or "").lower()
    found = []
    for term in _BUDGET_TERMS:
        term = term.lower()
        pattern = r"\b" + re.escape(term) + r"\b" if " " not in term else re.escape(term)
        if re.search(pattern, lowered):
            found.append(term)
    return sorted(set(found))


# --------------------------------------------------------------------------- #
# the operator's assignment
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RoleModels:
    """Which model carries which role; ``None`` means the deterministic rule.

    ``method`` says which arm is being run; it is a separate field of the
    handoff file, not a role.
    """

    target: Optional[str] = None
    control: Optional[str] = None
    trajectory: Optional[str] = None
    monolith: Optional[str] = None
    method: str = METHOD_THREE_AGENT

    def __post_init__(self) -> None:
        for role in ROLES:
            value = getattr(self, role)
            if value is not None:
                value = str(value).strip()
                object.__setattr__(self, role, value or None)
            if getattr(self, role) == DETERMINISTIC:
                object.__setattr__(self, role, None)
        method = str(self.method or "").strip() or METHOD_THREE_AGENT
        object.__setattr__(self, "method", method)

    @classmethod
    def from_mapping(cls, mapping: Optional[Mapping[str, Any]]) -> "RoleModels":
        mapping = dict(mapping or {})
        method = mapping.pop("method", METHOD_THREE_AGENT)
        unknown = sorted(set(mapping) - set(ROLES))
        if unknown:
            raise ValueError(f"unknown coordination roles {unknown}; roles are {list(ROLES)}")
        return cls(method=method, **{role: mapping.get(role) for role in ROLES})

    def model_for(self, role: str) -> Optional[str]:
        if role not in ROLES:
            raise ValueError(f"unknown coordination role {role!r}")
        return getattr(self, role)

    @property
    def any_llm(self) -> bool:
        return any(getattr(self, role) is not None for role in ROLES)

    def to_record(self) -> Dict[str, Optional[str]]:
        """Only the roles: ``method`` is written beside this, not inside it."""
        return {role: getattr(self, role) for role in ROLES}


def load_role_models_file(path: Path) -> RoleModels:
    """The Cockpit's handoff file.

    A missing file, or a 1.0.0 file written by the previous (intent / action /
    search) triad, reads as all-deterministic rather than raising: the old keys
    name roles that no longer exist, so there is no model to carry forward.
    """
    path = Path(path)
    if not path.is_file():
        return RoleModels()
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return RoleModels()
    if not isinstance(document, dict):
        return RoleModels()
    models = document.get("roleModels", document)
    if not isinstance(models, dict):
        return RoleModels()
    method = str(document.get("method", METHOD_THREE_AGENT) or METHOD_THREE_AGENT)
    if set(models) - set(ROLES) - {"method"}:
        # a legacy assignment; its roles no longer exist
        return RoleModels(method=method)
    chosen = {role: models.get(role) for role in ROLES}
    return RoleModels(method=method, **chosen)


def save_role_models_file(path: Path, models: RoleModels, *, chosen_at: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"schemaVersion": ROLE_MODELS_SCHEMA, "method": models.method,
                   "roleModels": models.to_record(), "chosenAt": chosen_at},
                  handle, indent=2)
        handle.write("\n")
    return path


# --------------------------------------------------------------------------- #
# one metered call
# --------------------------------------------------------------------------- #


class AnswerRefused(ValueError):
    """A model answer did not survive parsing, its schema or its closed sets."""


class TransportFailure(Exception):
    """The request got no model answer at all: timeout, connection, 408/429/5xx.

    Kept apart from :class:`AnswerRefused` so it is retried as the same request
    instead of being pasted into the next prompt as "your previous answer was
    refused" -- there was no answer -- and so it never counts as a repair.
    """


#: Seconds slept before each transport retry; its length is the retry count.
TRANSPORT_BACKOFF_S: Tuple[float, ...] = (2.0, 4.0, 8.0)
#: 2026-09-23 오너 "너무 오래 기다리지 마라": a call that **timed out** already spent the
#: whole call timeout (300 s), so it is not re-sent -- four of them were a 20-minute
#: decision.  Only fast failures (connection reset, 429, 5xx) are re-sent, and never
#: once this much time has gone into the attempts.
TRANSPORT_RETRY_WINDOW_S = 60.0


class DecisionUnavailable(Exception):
    """No usable model answer arrived while re-asking was still allowed.

    Owner instruction 2026-09-19: "fallback 은 연구 교란밖에 안 한다".  The
    executor never chooses a target, a control or a candidate set in a
    model's place; the caller ends the sitting under its own rule instead.
    """

    def __init__(self, reason: str, record: "CallRecord") -> None:
        super().__init__(reason)
        self.reason = reason
        self.record = record


class ClarificationNeeded(Exception):
    """The Target agent answered with questions instead of a ``T``.

    Not a failure: the operator authorized less than the model needs to build
    ``T``, so the executor routes ``questions`` to whoever is running the
    sitting and calls again with the answers merged in (contract v2 section
    2.2's clarification loop).  ``record`` is the call that asked.
    """

    def __init__(self, questions: Sequence[Mapping[str, Any]],
                 record: "CallRecord") -> None:
        self.questions = [dict(item) for item in questions]
        self.record = record
        super().__init__(
            "the target agent needs "
            + "; ".join(f"{item.get('intentId', '?')}.{item.get('field', '?')}"
                        for item in self.questions))


@dataclass
class CallRecord:
    """One decision's provenance -- contract section 2.5 ``calls[]``.

    ``prompt`` / ``system_prompt`` / ``raw`` stay in memory (tests assert on
    them, the Cockpit shows them) and are deliberately not written into the
    episode record, which would otherwise carry the whole prompt per trial.

    ``options`` is what the model was asked to spend (contract v2 section 7)
    and ``stale_at_arrival`` is the executor's finding that the answer came
    back after the observations it was given had expired (section 6).
    """

    role: str
    model: str
    phase: str
    started_at: str = ""
    latency_ms: float = 0.0
    #: Accumulated across every generation, including retries.  These stay
    #: plain ints: a generation the provider gave no count for adds 0, so the
    #: total is a LOWER BOUND, never a claim of completeness.  Summing-as-0 is
    #: the deliberate choice over refusing to total -- the figure stays usable
    #: and every existing reader keeps doing integer arithmetic on it -- but it
    #: is only honest while the understatement is visible, which is what
    #: ``tokens_complete`` below is for.  Never read one without the other.
    input_tokens: int = 0
    output_tokens: int = 0
    #: False once ANY generation reported no count, i.e. the totals above are
    #: incomplete.  Without this the two readings "the provider said 0" and
    #: "the provider said nothing" collapse into the same total with nothing
    #: left to tell them apart.  The per-generation ``inputTokens`` /
    #: ``outputTokens`` keep the unknowns individually (as ``null``); this is
    #: the one-glance summary for whoever reads only the call row.
    tokens_complete: bool = True
    accepted: bool = False
    fallback_reason: Optional[str] = None
    repair_retries: int = 0
    #: Re-sends of the same request after a transport failure (no answer came
    #: back).  Not repairs: ``repair_retries`` counts only answers that came
    #: back and were refused.
    transport_retries: int = 0
    rationale: str = ""
    dropped: Tuple[str, ...] = ()
    options: Dict[str, Any] = field(default_factory=dict)
    stale_at_arrival: bool = False
    #: The ``input.*`` positions this call carried, sorted.  The episode record
    #: keeps ``T`` and ``C`` at top level for **every** arm, because they are
    #: the comparison grid the executor judges on -- so without this a reader
    #: cannot tell that the basic monolith never received either.  Its ``T`` and
    #: ``C`` carry ``provenance.model = "deterministic"``, which says who *built*
    #: them, not who *saw* them.
    input_keys: Tuple[str, ...] = ()
    questions: Tuple[Dict[str, Any], ...] = ()
    prompt: str = ""
    system_prompt: str = ""
    raw: str = ""
    # Keep every generation, including refused answers and repairs. ``model``
    # remains the selected decision label (or DETERMINISTIC after fallback).
    generations: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def used_llm(self) -> bool:
        return self.accepted and self.model not in (DETERMINISTIC, RULE_GREEDY)

    @property
    def served_model(self) -> Optional[str]:
        """Which model the provider said actually answered, or ``None``.

        ``model`` above is the label this executor *asked* for, and the proxy
        this lab talks to advertises obfuscated ids -- so the two are not the
        same claim.  ``None`` is "unknown", never the requested label: a
        fallback that reported nothing must not read back as a normal run.
        The last successful generation is the one whose answer was used.
        """
        for item in reversed(self.generations):
            if item.get("responseSuccess"):
                return item.get("responseModel") or None
        return None

    def to_record(self) -> Dict[str, Any]:
        record = {"role": self.role, "model": self.model, "phase": self.phase,
                  "startedAt": self.started_at, "latencyMs": self.latency_ms,
                  "inputTokens": self.input_tokens, "outputTokens": self.output_tokens,
                  # Always written, like ``servedModel``: a total that silently
                  # dropped an unreported generation reads exactly like a
                  # complete one, so the lower-bound-ness travels with the
                  # numbers instead of living only in this class's docstring.
                  "tokensComplete": bool(self.tokens_complete),
                  "accepted": bool(self.accepted), "fallbackReason": self.fallback_reason,
                  "repairRetries": int(self.repair_retries),
                  "transportRetries": int(self.transport_retries),
                  "rationale": self.rationale, "dropped": list(self.dropped),
                  "options": dict(self.options),
                  "inputKeys": list(self.input_keys),
                  # Always written, null included: an absent key would read as
                  # "nobody looked", a null says the provider reported nothing.
                  "servedModel": self.served_model,
                  "staleAtArrival": bool(self.stale_at_arrival)}
        if self.questions:
            record["missingInformation"] = [dict(item) for item in self.questions]
        if self.generations:
            record["generations"] = [dict(item) for item in self.generations]
        return record


def _sha256_text(text: Any) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _cache_and_scope(usage: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Cache counts and what ``inputTokens`` covers, read from the usage shape.

    The provider families disagree, and summing across them without saying so
    double counts or under counts silently.  Anthropic Messages reports
    ``input_tokens`` *excluding* ``cache_read_input_tokens`` and
    ``cache_creation_input_tokens``; OpenAI reports ``prompt_tokens`` *including*
    ``prompt_tokens_details.cached_tokens``.  Reasoning is inside the output
    count for both.  Nothing is inferred: a key the provider did not send stays
    ``None``, and an unrecognised envelope leaves the scope unknown.
    """
    found: Dict[str, Any] = {"cacheReadInputTokens": None,
                             "cacheCreationInputTokens": None, "usageScope": None}
    if not isinstance(usage, Mapping):
        return found

    def count(value: Any) -> Optional[int]:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    if "input_tokens" in usage:
        found["cacheReadInputTokens"] = count(usage.get("cache_read_input_tokens"))
        found["cacheCreationInputTokens"] = count(usage.get("cache_creation_input_tokens"))
        found["usageScope"] = {"inputTokens": "excludes cache read and cache creation",
                               "reasoningTokens": "included in outputTokens"}
    elif "prompt_tokens" in usage:
        details = usage.get("prompt_tokens_details")
        if isinstance(details, Mapping):
            found["cacheReadInputTokens"] = count(details.get("cached_tokens"))
        found["usageScope"] = {"inputTokens": "includes cached prompt tokens",
                               "reasoningTokens": "included in outputTokens"}
    return found


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _reject_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    """``object_pairs_hook``: `json.loads` 는 중복 키의 **마지막** 값만 남겨
    모순된 답을 조용히 하나로 만든다.  모델이 `"value": 8.0` 뒤에 `"value": 5.6`
    을 내면 원문을 읽은 사람과 코드가 서로 다른 제안을 보게 된다 -- 원문이 증거인
    실험에서 그건 기록의 훼손이다.  `coordinator/schema.py` 가 예전부터 이 훅을
    쓰는데 이 경로에만 없었다 (2026-09-22).
    """
    seen: Dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise AnswerRefused(f"duplicate JSON key {key!r} (ambiguous object)")
        seen[key] = value
    return seen


def _reject_constant(token: str) -> Any:
    """`NaN`/`Infinity` 는 표준 JSON 이 아니고 float() 이 되므로 축 경계로 조용히
    clip 된다.  숫자 레벨 벡터를 제안받는 경로에서는 거절이 유일하게 안전하다."""
    raise AnswerRefused(f"non-standard JSON constant {token!r} (not finite)")


def _extract_object(text: str) -> Dict[str, Any]:
    """Pull the one JSON object out of a model's answer (fenced or bare)."""
    if not isinstance(text, str) or not text.strip():
        raise AnswerRefused("empty completion")
    fenced = _FENCE.search(text)
    body = fenced.group(1) if fenced else text
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise AnswerRefused("no JSON object in the completion")
    try:
        parsed = json.loads(body[start:end + 1],
                            object_pairs_hook=_reject_duplicate_keys,
                            parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise AnswerRefused(f"{exc.msg} at position {exc.pos}") from exc
    if not isinstance(parsed, dict):
        raise AnswerRefused(f"top level is {type(parsed).__name__}, not an object")
    return parsed


#: Predictor output that never reaches a model (owner instruction 2026-09-19):
#: its table, its description (formula, capacity, SNR, handover time, error
#: band) and its uncertainty note.  Since the same day's follow-up ("연관표는
#: 원래 control 이 만들어야 하고 BM 은 이런 게 없어야") the code-made
#: ``controlKpiAssociations`` go too: which KPIs a control touches is the
#: Control agent's inference, and the basic monolith gets no structure.  Only
#: the measured ``observations`` remain in ``input.effect_evidence``.
_PREDICTOR_EVIDENCE_KEYS = ("predictions", "predictorDescription", "uncertaintyNote",
                            "controlFamilies", "controlKpiAssociations", "note")


def _without_predictor(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Every model input, with whatever the predictor produced taken out.

    One choke point for all six calls, so no arm can receive a number another
    arm is denied.  Measured observations, ``observed_best`` and ``kpi_gaps``
    are measurements and stay.  A candidate's own ``predicted`` answer stays
    (it is a model's reasoning); an ``evidenceRefs`` entry naming the
    predictor does not.
    """
    out = dict(payload)
    evidence = out.get("input.effect_evidence")
    if isinstance(evidence, Mapping):
        out["input.effect_evidence"] = {key: value for key, value in evidence.items()
                                        if key not in _PREDICTOR_EVIDENCE_KEYS}
    state = out.get("input.network_state")
    if isinstance(state, Mapping) and isinstance(state.get("cells"), Mapping):
        # ``capacityMbps`` is the predictor's per-cell capacity parameter.
        cells = {cell: {k: v for k, v in dict(row).items() if k != "capacityMbps"}
                 for cell, row in dict(state["cells"]).items()}
        state = dict(state)
        if any(cells.values()):
            state["cells"] = cells
        else:
            state.pop("cells")
        out["input.network_state"] = state
    rows = out.get("input.control_candidates")
    if isinstance(rows, list):
        cleaned = []
        for row in rows:
            row = dict(row)
            refs = [ref for ref in row.get("evidenceRefs") or []
                    if not str(ref).startswith("predictor")]
            if refs:
                row["evidenceRefs"] = refs
            else:
                row.pop("evidenceRefs", None)
            cleaned.append(row)
        out["input.control_candidates"] = cleaned
    return out


def _ranged(node: Any) -> Any:
    """Every ``policyFields`` record in a payload as the model sees it under range mode."""
    if isinstance(node, Mapping):
        return {key: ({name: ranged_policy_field(entry) if isinstance(entry, Mapping) else entry
                       for name, entry in value.items()}
                      if key == "policyFields" and isinstance(value, Mapping) else _ranged(value))
                for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [_ranged(item) for item in node]
    return node


def _user_prompt(payload: Mapping[str, Any], schema: Mapping[str, Any]) -> str:
    """``INPUTS:`` JSON, ``OUTPUT SCHEMA:`` JSON, and the return instruction.

    Serialised compactly.  ``indent=1`` cost 42% of the prompt in whitespace on
    a measured Trajectory call (17 798 B of content became 25 291 B), and
    indentation is worse per token than per byte because each newline-plus-space
    run tokenises separately.  The model receives exactly the same object either
    way, so this is size removed, not information.

    It falls hardest on the arms that carry a grid -- three-agent and internal
    monolith both send ``T`` and ``C`` on every decision -- which is why it
    inflated the measured cost of the very separation the ablation exists to
    price.  The basic monolith is unaffected in kind, only in degree.
    """
    payload = _without_predictor(payload)
    if policy_ranges():
        schema = json.loads(json.dumps(schema).replace(
            "<one listed value>", "<one listed value, or any number from min to max where the field gives min and max>"))
        payload = _ranged(payload)
    return ("INPUTS:\n" + json.dumps(payload, separators=(",", ":"), default=str)
            + "\n\nOUTPUT SCHEMA:\n" + json.dumps(schema, separators=(",", ":"))
            + "\n\nReturn only the JSON object.")


def default_resolver(model_name: str) -> Any:
    """The project's backend manager, resolved lazily so tests stay hermetic."""
    from decision.llm_backend import LLMBackendManager  # local import on purpose

    backend = LLMBackendManager().resolve_object(model_name)
    if backend is None:
        raise LookupError(f"no LLM backend resolves the model name {model_name!r}")
    return backend


# --------------------------------------------------------------------------- #
# a scripted backend for hermetic tests
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScriptedResponse:
    success: bool
    content: str
    parsed_json: Optional[Dict[str, Any]] = None
    latency_ms: float = 12.0
    input_tokens: int = 100
    output_tokens: int = 40
    error: Optional[str] = None


@dataclass(frozen=True)
class ScriptedCall:
    model: str
    system_prompt: str
    prompt: str
    options: Dict[str, Any] = field(default_factory=dict)


class _ScriptedBackend:
    def __init__(self, resolver: "ScriptedResolver", model: str) -> None:
        self._resolver = resolver
        self._model = model

    def generate(self, prompt: str, system_prompt: str = "",
                 options: Optional[Mapping[str, Any]] = None) -> ScriptedResponse:
        return self._resolver._answer(self._model, prompt, system_prompt, options)


class ScriptedResolver:
    """Canned answers per model name; every prompt is captured for assertions.

    An answer may be a ``dict`` (dumped to JSON), a ``str`` (sent as-is, so a
    test can script an unparseable or schema-breaking answer), an ``Exception``
    (raised from the backend) or ``None`` (a failed response).  The last
    scripted answer repeats once the queue is empty.  The generation options
    the executor chose are captured per call, so a test can assert that a role
    was asked for the budget it should have been asked for.
    """

    def __init__(self, answers: Optional[Mapping[str, Sequence[Any]]] = None, *,
                 default: Any = None) -> None:
        self.answers: Dict[str, List[Any]] = {
            str(model): list(items) for model, items in dict(answers or {}).items()}
        self.default = default
        self.calls: List[ScriptedCall] = []

    @classmethod
    def by_role(cls, answers: Mapping[str, Sequence[Any]], *,
                method: str = METHOD_THREE_AGENT,
                default: Any = None) -> Tuple["ScriptedResolver", RoleModels]:
        """``{"target": [...], "trajectory": [...]}`` -> a resolver and the
        :class:`RoleModels` that route each role to it."""
        keyed = {f"scripted:{role}": list(items) for role, items in dict(answers).items()}
        chosen = {role: (f"scripted:{role}" if role in answers else None) for role in ROLES}
        return cls(keyed, default=default), RoleModels(method=method, **chosen)

    def __call__(self, model_name: str) -> _ScriptedBackend:
        return _ScriptedBackend(self, str(model_name))

    @property
    def prompts(self) -> List[str]:
        return [call.prompt for call in self.calls]

    def prompts_for(self, model_name: str) -> List[str]:
        return [call.prompt for call in self.calls if call.model == str(model_name)]

    def options_for(self, model_name: str) -> List[Dict[str, Any]]:
        return [dict(call.options or {}) for call in self.calls
                if call.model == str(model_name)]

    def _answer(self, model: str, prompt: str, system_prompt: str,
                options: Optional[Mapping[str, Any]] = None) -> ScriptedResponse:
        self.calls.append(ScriptedCall(model=model, system_prompt=system_prompt,
                                       prompt=prompt, options=dict(options or {})))
        queue = self.answers.get(model)
        queue = self.answers.get(model)
        if queue:
            answer = queue.pop(0) if len(queue) > 1 else queue[0]
        else:
            answer = self.default
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            return ScriptedResponse(success=False, content="", error="scripted failure")
        if isinstance(answer, ScriptedResponse):
            return answer
        if isinstance(answer, str):
            return ScriptedResponse(success=True, content=answer)
        content = json.dumps(answer)
        return ScriptedResponse(success=True, content=content, parsed_json=dict(answer))


# --------------------------------------------------------------------------- #
# the inputs each role is given (contract section 4; SINGLE_CALL.md)
# --------------------------------------------------------------------------- #


def _configuration(mapping: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    return {str(k): str(v) for k, v in dict(mapping or {}).items()}


def _catalog_baselines(catalog: Any, applied: Mapping[str, str]) -> Dict[str, str]:
    """C0 per axis; the applied configuration only says where each UE is now.

    ``FunctionCatalog.baselines(applied)`` overrides *every* axis with what is
    applied, so after a trial settled ``pfWeight@ue2 = 4.0`` the basic
    monolith's "baseline" was 4.0: the baseline rule kept it under the next
    answer's ``dlPrbCap@ue2 = 18``, and the cap/PF exclusion compared 4.0 with
    itself and let both through (2026-09-19 board 20260919T093728, M13).
    """
    placement = {axis: value for axis, value in dict(applied).items()
                 if str(axis).partition("@")[0] == "servingCell"}
    return catalog.baselines(placement)


def _space(mapping: Optional[Mapping[str, Sequence[Any]]]) -> Dict[str, List[str]]:
    return {str(axis): [str(item) for item in values]
            for axis, values in dict(mapping or {}).items()}


def _catalog_record(catalog: Any) -> List[Dict[str, Any]]:
    if catalog is None:
        return []
    if isinstance(catalog, FunctionCatalog):
        return catalog.to_record()
    return [dict(item) for item in catalog]


def _compatibility_record(rules: Any) -> Dict[str, Any]:
    if rules is None:
        return CompatibilityRules().to_record()
    if isinstance(rules, CompatibilityRules):
        return rules.to_record()
    return CompatibilityRules.from_record(rules).to_record()


def _compact_effect_evidence(evidence: Optional[Mapping[str, Any]]
                             ) -> Dict[str, Any]:
    """Target's share of ``input.effect_evidence``: families, not a table.

    Target now selects which authorized concessions are worth preparing, and
    it cannot tell a useful concession from an arbitrary ranked prefix without
    knowing what the deployment can actually do to service.  What it must
    *not* get is Control's per-configuration prediction table: that is the
    Cartesian block the handoff removed from every prompt, and re-ranking it
    is Control's job, not Target's.  So the predictions are dropped and only
    the axes they exercise are kept, beside the predictor's own prose about
    what each family does to service.
    """
    found = dict(evidence or {})
    rows = found.get("predictions") or []
    families = sorted({str(axis) for row in rows
                       for axis in dict(dict(row).get("configuration") or {})})
    compact = {key: value for key, value in found.items() if key != "predictions"}
    if families:
        compact["controlFamilies"] = families
    return compact


@dataclass(frozen=True)
class TargetInputs:
    """The owner agreements, the initial service state and compact evidence.

    The intents carry their own sentence, owner, scope, KPI condition and unit;
    the authorization carries the steps, the bound, the joint conditions, the
    finite priority weights and the ranking rule.  ``network_state`` is the
    initial per-UE snapshot with its observation times, and
    ``effect_evidence`` is compacted by :func:`_compact_effect_evidence` --
    which control families exist and what they do to service, never the
    prediction table.  Both default to empty: a caller that has neither yet
    still forms targets, it just forms them blind.  No action space, no budget.
    """

    intents: Tuple[Intent, ...] = ()
    authorization: Authorization = field(default_factory=Authorization)
    network_state: Mapping[str, Any] = field(default_factory=dict)
    effect_evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "intents", tuple(self.intents))

    @property
    def preference(self) -> Preference:
        return self.authorization.preference

    @property
    def joint_conditions(self) -> Tuple[Any, ...]:
        return tuple(self.authorization.joint_conditions)

    def payload(self) -> Dict[str, Any]:
        return {"input.intents": [intent.to_record() for intent in self.intents],
                "input.authorization": self.authorization.to_full_record(),
                # Derived from the frozen authority, never from a model: the
                # original plus every maximally weakened authorized target.  The
                # model is shown them so it can add *beyond* them; code adds them
                # to ``T`` whatever the answer says.
                "input.mandatory_targets": _mandatory_rows(self.authorization),
                # 2026-09-20 오너 승인: 목표 구성은 실측 근거에 의존하지 않는다 --
                # 인텐트·인가·선호가 같으면 T 를 재사용할 수 있어야 하고, 실측은
                # 판마다 달라진다.  현재 구성(소속 셀·부하)은 남긴다: 어느 UE 가
                # 붐비는 셀에 있는지 모르면 어떤 양보가 쓸모 있는지 고를 수 없다.
                "input.network_state": dict(self.network_state or {})}


def _control_authorization(authorization: Authorization) -> Dict[str, Any]:
    """``input.authorization`` as Control reads it: the bounds, not the ranking."""
    record = dict(authorization.to_full_record())
    record.pop("preference", None)
    return record


def _mandatory_rows(authorization: Authorization) -> List[Dict[str, Any]]:
    """The mandatory targets in the shape an addition is written in."""
    contract = mandatory_contract(omega_or_sparse(authorization))
    return [{"targetId": target.target_id, "levels": dict(target.levels),
             "deadlineLevels": dict(target.deadline_levels)}
            for target in (contract.t0,) + tuple(contract.alternatives)]


@dataclass(frozen=True)
class ControlInputs:
    """The six ``input.*`` positions of the Control call (contract v2 3.3)."""

    #: Kept for the code paths that need the formed T (the deterministic
    #: control set, validation): it is no longer sent to the model.
    target_contract: Optional[TargetContract] = None
    intents: Tuple[Any, ...] = ()
    authorization: Optional[Authorization] = None
    function_catalog: Optional[FunctionCatalog] = None
    compatibility: Optional[CompatibilityRules] = None
    network_state: Mapping[str, Any] = field(default_factory=dict)
    effect_evidence: Mapping[str, Any] = field(default_factory=dict)
    construction_policy: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.function_catalog is not None and not isinstance(
                self.function_catalog, FunctionCatalog):
            object.__setattr__(self, "function_catalog",
                               FunctionCatalog.from_record(self.function_catalog))
        if self.compatibility is not None and not isinstance(
                self.compatibility, CompatibilityRules):
            object.__setattr__(self, "compatibility",
                               CompatibilityRules.from_record(self.compatibility))

    # -- what the executor needs, derived from the catalog and the state -----

    @property
    def action_space(self) -> Dict[str, Tuple[str, ...]]:
        return self.function_catalog.axes() if self.function_catalog else {}

    @property
    def applied_configuration(self) -> Dict[str, str]:
        return _configuration(dict(self.network_state or {}).get(
            "appliedConfiguration", {}))

    @property
    def baselines(self) -> Dict[str, str]:
        if self.function_catalog is None:
            return {}
        return _catalog_baselines(self.function_catalog, self.applied_configuration)

    @property
    def unselected_function_rule(self) -> str:
        return str(dict(self.network_state or {}).get(
            "unselectedFunctionRule", RULE_BASELINE) or RULE_BASELINE)

    #: How many candidates besides ``C0`` a model Control answer may carry.
    #: The integrated reply of 2026-09-14 section 4: "It constructs at most 12
    #: configurations **including baseline**."  ``validate_control_candidates``
    #: prepends the baseline unconditionally and then caps on ``len(kept) - 1``,
    #: so ``retain`` counts the non-baseline candidates and the drop's twelve is
    #: ``11`` here.  The live runner passes ``--retain`` explicitly, which is
    #: exactly why this default needs a test: a wrong one never fails a run.
    @property
    def retain(self) -> int:
        return int(dict(self.construction_policy or {}).get("retain", 11) or 11)

    def payload(self) -> Dict[str, Any]:
        # 2026-09-20 오너 승인: Control 은 Target 이 고른 T 를 기다리지 않는다.
        # 선언된 요구조건과 인가된 대안은 원본 인텐트·인가에 이미 있으므로 두
        # 역할을 동시에 시작할 수 있다.  선호 순서는 빼둔다 -- 어떤 목표를 먼저
        # 좇을지는 Target 과 Trajectory 의 판단이다.
        authorization = self.authorization
        return {
            "input.intents": [intent.to_record() for intent in self.intents],
            "input.authorization": (_control_authorization(authorization)
                                    if authorization is not None else None),
            "input.function_catalog": _catalog_record(self.function_catalog),
            "input.compatibility": _compatibility_record(self.compatibility),
            "input.network_state": dict(self.network_state or {}),
            "input.effect_evidence": dict(self.effect_evidence or {}),
            "input.construction_policy": dict(self.construction_policy or {}),
        }


@dataclass(frozen=True)
class TrajectoryInputs:
    """The six ``input.*`` positions of the Trajectory call (contract v2 5).

    ``observed_best`` and ``kpi_gaps`` are computed by the executor from the
    measured KPIs -- ``SINGLE_CALL.md`` is explicit that the external evaluator
    does that, not the model.
    """

    target_contract: Optional[TargetContract] = None
    control_candidates: Optional[ControlCandidates] = None
    network_state: Mapping[str, Any] = field(default_factory=dict)
    observations: Tuple[Mapping[str, Any], ...] = ()
    observed_best: Optional[Mapping[str, Any]] = None
    kpi_gaps: Optional[Mapping[str, Any]] = None
    grid: Optional[Grid] = None
    #: Kernel-owned status per control id: availability, whether it has already
    #: been tried in this epoch, and the outcome it produced.  C keeps its frozen
    #: membership and identities; only this status moves.  Without it the model
    #: proposes a spent candidate, the executor rejects it, and the episode ends
    #: with eligible candidates still unexplored.
    candidate_status: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "observations",
                           tuple(item.to_record() if isinstance(item, Observation)
                                 else dict(item) for item in self.observations))

    @property
    def applied_configuration(self) -> Dict[str, str]:
        return _configuration(dict(self.network_state or {}).get(
            "appliedConfiguration", {}))

    def _with_status(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """One candidate record plus its Kernel-owned status, if known.

        C keeps its frozen membership, cardinality and identities; only the
        status moves.  Without it the model cannot see what it has already
        spent, proposes it again, and the executor ends the episode with
        eligible candidates still unexplored.
        """
        status = dict(self.candidate_status or {}).get(str(record.get("controlId")))
        if not status:
            return dict(record)
        merged = dict(record)
        merged["status"] = dict(status)
        return merged

    def _candidate_block(self) -> List[Dict[str, Any]]:
        """The baseline once, then each candidate's policy delta.

        ``ControlCandidate.to_record`` writes the **complete** configuration --
        every axis of the frozen catalog, whatever the candidate actually
        moves -- and writes ``functions`` beside it, which is the same choice
        expressed as the policy delta the model itself answered with.  Carrying
        both sends the whole action space once per candidate: on a three-UE
        sitting that is twelve copies of nine axes, and every one of them is
        paid again on every subsequent decision, by exactly the two arms that
        carry a grid.  ``effectEstimate`` is a third copy of the same thing --
        it is ``predicted`` under its v1 name, which
        :class:`ControlCandidate` fills in from the other whenever one is
        missing.

        So: ``C0`` keeps its configuration, because ``C0`` *is* the baseline
        and that is where it is stored once; every candidate that states its
        ``functions`` carries those and not the expansion of them.  Nothing is
        lost -- ``translate_functions`` is what expanded the delta in the first
        place, the executor keeps the expanded object in memory, and
        ``input.network_state.appliedConfiguration`` names the baseline too.
        A candidate read from a v1 record has no ``functions`` and keeps its
        configuration, so the compaction can never empty a row.

        What the drop asks to keep is kept: the expected joint effects
        (``predicted``), ``uncertainty``, ``applicability``, ``evidenceRefs``
        and the one-line rationale.
        """
        controls = self.control_candidates
        if controls is None:
            return []
        rows: List[Dict[str, Any]] = []
        for index, item in enumerate(controls.candidates):
            record = self._with_status(item.to_record())
            # 2026-09-20 (owner): a candidate reaches the selector as what it
            # does, what it is related to and Control's one-line rationale (its
            # intended qualitative effect) -- nothing estimated about it.
            keep = {"controlId", "functions", "relatedKpis", "rationale", "status"}
            if not (index and record.get("functions")):
                keep.add("configuration")
            rows.append({key: value for key, value in record.items() if key in keep})
        return rows

    def payload(self) -> Dict[str, Any]:
        contract = self.target_contract
        return {
            "input.target_contract": (contract.to_record()
                                      if contract is not None else None),
            "input.control_candidates": self._candidate_block(),
            "input.network_state": dict(self.network_state or {}),
            "input.observations": [dict(item) for item in self.observations],
            "input.observed_best": (dict(self.observed_best)
                                    if self.observed_best else None),
            "input.kpi_gaps": dict(self.kpi_gaps) if self.kpi_gaps else None,
        }


@dataclass(frozen=True)
class MonolithFormInputs:
    """Everything Target and Control receive, in one call.

    ``input.target_contract`` is dropped: this call is what produces it.
    """

    target: TargetInputs = field(default_factory=TargetInputs)
    control: ControlInputs = field(default_factory=ControlInputs)

    def payload(self) -> Dict[str, Any]:
        # One call: Target's full authorization (it carries the preference
        # ordering this arm still needs) wins over Control's reduced view.
        payload = dict(self.control.payload())
        payload.pop("input.target_contract", None)
        payload.update(self.target.payload())
        return payload


@dataclass(frozen=True)
class BasicInputs:
    """The basic monolith's source information -- no ``T``, no ``C``, no grid.

    It gets the same original intents, owner authorization, function catalog,
    compatibility, state, effect evidence and observations as everyone else; it
    does not get our prepared targets, our candidate set, our rankings or a
    trajectory recommendation (``SINGLE_CALL.md``, executor boundaries).
    """

    intents: Tuple[Intent, ...] = ()
    authorization: Authorization = field(default_factory=Authorization)
    function_catalog: Optional[FunctionCatalog] = None
    compatibility: Optional[CompatibilityRules] = None
    network_state: Mapping[str, Any] = field(default_factory=dict)
    effect_evidence: Mapping[str, Any] = field(default_factory=dict)
    observations: Tuple[Mapping[str, Any], ...] = ()
    #: Every configuration a trial dispatched, including trials whose window was unjudgeable
    #: and so left no observation (2026-09-26, pilot board 733: the missing one was proposed
    #: again, refused as already tried, and the board spun until its deadline).
    dispatched: Tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "intents", tuple(self.intents))
        if self.function_catalog is not None and not isinstance(
                self.function_catalog, FunctionCatalog):
            object.__setattr__(self, "function_catalog",
                               FunctionCatalog.from_record(self.function_catalog))
        if self.compatibility is not None and not isinstance(
                self.compatibility, CompatibilityRules):
            object.__setattr__(self, "compatibility",
                               CompatibilityRules.from_record(self.compatibility))
        object.__setattr__(self, "observations",
                           tuple(item.to_record() if isinstance(item, Observation)
                                 else dict(item) for item in self.observations))

    @property
    def action_space(self) -> Dict[str, Tuple[str, ...]]:
        return self.function_catalog.axes() if self.function_catalog else {}

    @property
    def applied_configuration(self) -> Dict[str, str]:
        return _configuration(dict(self.network_state or {}).get(
            "appliedConfiguration", {}))

    @property
    def baselines(self) -> Dict[str, str]:
        if self.function_catalog is None:
            return {}
        return _catalog_baselines(self.function_catalog, self.applied_configuration)

    @property
    def unselected_function_rule(self) -> str:
        return str(dict(self.network_state or {}).get(
            "unselectedFunctionRule", RULE_BASELINE) or RULE_BASELINE)

    @property
    def tried_configurations(self) -> Tuple[Dict[str, str], ...]:
        seen, out = set(), []
        for configuration in [dict(item).get("configuration") for item in self.observations] + \
                list(self.dispatched):
            if not configuration:
                continue
            item = _configuration(configuration)
            key = tuple(sorted(item.items()))
            if key not in seen:
                seen.add(key)
                out.append(item)
        return tuple(out)

    observed_best: Optional[Mapping[str, Any]] = None
    kpi_gaps: Optional[Mapping[str, Any]] = None

    def payload(self) -> Dict[str, Any]:
        return {
            "input.intents": [intent.to_record() for intent in self.intents],
            "input.authorization": self.authorization.to_full_record(),
            "input.function_catalog": _catalog_record(self.function_catalog),
            "input.compatibility": _compatibility_record(self.compatibility),
            "input.network_state": dict(self.network_state or {}),
            "input.effect_evidence": dict(self.effect_evidence or {}),
            "input.observations": [dict(item) for item in self.observations],
            # The basic monolith gets no prepared C, so the only way it can know
            # what it has already run is its own history.  The property existed
            # but never reached the model, which is how it re-proposed a spent
            # configuration and ended the episode.
            "input.tried_configurations": [dict(item)
                                           for item in self.tried_configurations],
            # 2026-09-17 (오너 드롭, Runtime alignment 항목 2·4):
            # "Expose equivalent current-state, valid-history, execution-error,
            #  applicability, and best-result information to the online
            #  selectors and BM."  Trajectory/Select 는 `observed_best` 와
            # `kpi_gaps` 를 받는데 이 팔만 못 받고 있었다.
            # T 가 없는 팔이므로 목표 id 만으로는 뜻이 없다 -- 같은 항목이
            # "Include its requirement levels and preference information when it
            #  is absent from T" 라고 정한 대로 **레벨을 함께** 싣는다.
            "input.observed_best": (dict(self.observed_best)
                                    if self.observed_best else None),
            "input.kpi_gaps": dict(self.kpi_gaps) if self.kpi_gaps else None,
        }


# --------------------------------------------------------------------------- #
# the agents
# --------------------------------------------------------------------------- #


@dataclass
class RoleAgents:
    """The roles, each optionally on its own LLM, each one call per decision.

    ``resolver(model_name)`` must return an object with
    ``generate(prompt, system_prompt)`` -- and, when it supports them,
    ``options`` -- returning a response carrying ``success`` / ``content``
    (and, when it has them, ``parsed_json``, ``latency_ms``, ``input_tokens``,
    ``output_tokens``).  The project's backends already do, and
    :class:`ScriptedResolver` does for tests.  A backend whose ``generate``
    does not take ``options`` is called without them, so nothing here depends
    on every backend having been upgraded first.

    ``predictor`` is the sitting's central joint-effect model.  It is what the
    deterministic Control and Trajectory rules rank with, and what the executor
    re-derives every candidate's ``predictedTarget`` from -- an agent's own
    ranking is recorded beside it, never instead of it.

    ``options_for`` is how the executor asks for a generation budget per role
    (contract v2 section 7); with a latency calibration loaded into
    ``latency_calibration`` and a validity window in ``min_validity_ms`` the
    budget is chosen from the calibration instead of the defaults.
    """

    models: RoleModels = field(default_factory=RoleModels)
    resolver: Callable[[str], Any] = default_resolver
    calls: List[CallRecord] = field(default_factory=list)
    predictor: Optional[JointEffectPredictor] = None
    latency_calibration: Optional[Mapping[str, Any]] = None
    min_validity_ms: Optional[int] = None
    #: Whether another re-ask still fits the time the caller allows (B for a
    #: decision, the formation allowance for T and C).  ``None`` -- a caller
    #: with no clock -- keeps the single repair it always had.
    may_reask: Optional[Callable[..., bool]] = None
    #: Why the sitting refused the last decision (e.g. "M11 has already been tried"), appended
    #: to the next decision's prompt.  09-30 v5.4t: the re-ask was byte-identical, so qwen3
    #: returned the same tried configuration 57-98 times per board and ran 1-5 trials.
    rejection_note: str = ""
    _backends: Dict[str, Any] = field(default_factory=dict, repr=False)

    # -- plumbing --------------------------------------------------------------

    @property
    def method(self) -> str:
        return self.models.method

    def _backend(self, model_name: str) -> Any:
        if model_name not in self._backends:
            self._backends[model_name] = self.resolver(model_name)
        return self._backends[model_name]

    def options_for(self, prompt_role: str, model: Optional[str] = None
                    ) -> GenerationOptions:
        """What this role is asked to spend on one call."""
        if self.latency_calibration:
            return choose_from_calibration(self.latency_calibration, prompt_role,
                                           self.min_validity_ms, model)
        return GenerationOptions.for_role(prompt_role)

    @staticmethod
    def _accepts_options(backend: Any) -> bool:
        generate = getattr(backend, "generate", None)
        if generate is None:
            return False
        try:
            signature = inspect.signature(generate)
        except (TypeError, ValueError):
            return False
        for parameter in signature.parameters.values():
            if parameter.name == "options":
                return True
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                return True
        return False

    @staticmethod
    def _model_identifier(value: Any) -> Optional[str]:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _safe_backend_error(value: Any, backend: Any) -> str:
        message = str(value)
        credential = getattr(backend, "api_key", None)
        if isinstance(credential, str) and credential:
            message = message.replace(credential, "[REDACTED]")
        return message

    def _generate(self, model_name: str, prompt: str, system_prompt: str,
                  record: CallRecord,
                  options: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """One metered call; raises :class:`AnswerRefused` on anything unusable.

        Nothing here cuts the model off: ``options`` is what the model is asked
        to spend, and the backend's own network timeout is the only hang guard.
        """
        started = time.monotonic()
        generation = {"attempt": len(record.generations) + 1,
                      "selectedModel": model_name, "requestedRoute": None,
                      "responseModel": None, "startedAt": _now(),
                      "responseSuccess": False, "latencyMs": 0.0,
                      # ``None`` is UNKNOWN, and it is the right starting value:
                      # a generation whose backend raised never got a count, and
                      # 0 there would claim the attempt was free.  Overwritten
                      # below by whatever the provider actually reported.
                      "inputTokens": None, "outputTokens": None,
                      # Per attempt, never accumulated.  ``record.output_tokens``
                      # sums every attempt while ``record.raw`` keeps only the
                      # last one's text, so a persisted 945-byte body was being
                      # compared against *both* attempts' tokens.  These two keep
                      # the per-attempt facts separable from those totals.
                      # ``responseBytes`` is the body as received -- never the
                      # parsed object re-serialized, which would silently answer
                      # a different question -- and 0 here means a call that
                      # received no body at all (``errorType`` says why).
                      # ``refusedBecause`` is written for every generation, null
                      # included: a key that appeared only on failure would make
                      # absence ambiguous.  ``usage`` is seeded here for exactly
                      # that reason: it is written after the call returns, so a
                      # generation whose backend *raised* used to carry no
                      # ``usage`` key at all -- absent and null saying two
                      # different things about the same unknown.
                      # ``sentOptions`` is the settings counterpart: what the
                      # backend reported actually putting on the wire, beside
                      # the *requested* ``record.options``.  The thinking budget
                      # is gated on the model id, so a record showing a
                      # requested 4,000 could not tell a reader that none was
                      # sent -- the same class of error as a fallback reading
                      # as a normal run.
                      "responseBytes": 0, "refusedBecause": None,
                      # Set (to the error) only when no answer came back at
                      # all -- a transport failure, which is not a refusal.
                      "transportFailure": None,
                      "sentOptions": None, "usage": None,
                      # v3.1 accounting (amendment section 6, "LLM call"): an
                      # independent end time -- ``latencyMs`` is the backend's
                      # own figure and could not place the call on the episode
                      # clock -- the reasoning and cache counts the provider
                      # exposed, what ``inputTokens`` does and does not include,
                      # and this attempt's actual request and response text.
                      # Every count starts ``None`` (unknown), exactly like the
                      # two totals above.
                      "endedAt": None, "reasoningTokens": None,
                      "cacheReadInputTokens": None, "cacheCreationInputTokens": None,
                      "usageScope": None,
                      "request": {"systemPromptSha256": _sha256_text(system_prompt),
                                  "prompt": prompt},
                      "responseText": None}
        record.generations.append(generation)
        backend = None
        try:
            backend = self._backend(model_name)
            generation["requestedRoute"] = self._model_identifier(getattr(backend, "model", None))
            if options and self._accepts_options(backend):
                response = backend.generate(prompt, system_prompt, options=dict(options))
            else:
                response = backend.generate(prompt, system_prompt)
        except Exception as exc:  # a role must never take the sitting down
            generation["endedAt"] = _now()
            generation["latencyMs"] = (time.monotonic() - started) * 1000.0
            generation["errorType"] = type(exc).__name__
            record.latency_ms += generation["latencyMs"]
            # This attempt reported no counts and never reaches the accumulation
            # below, so the flag is set here too: a call whose every attempt
            # raised would otherwise publish a complete-looking total of 0.
            record.tokens_complete = False
            message = self._safe_backend_error(exc, backend)
            if isinstance(exc, AnswerRefused):
                generation["refusedBecause"] = message
                raise AnswerRefused(message) from None
            generation["refusedBecause"] = (
                f"{model_name} raised {type(exc).__name__}: {message}")
            raise AnswerRefused(generation["refusedBecause"]) from None
        generation["endedAt"] = _now()
        latency = float(getattr(response, "latency_ms", 0.0) or 0.0)
        generation["latencyMs"] = latency if latency > 0 else (time.monotonic() - started) * 1000.0
        # What the provider reported, or ``None`` when it reported nothing.  The
        # old ``int(getattr(..., 0) or 0)`` turned an unreported count into a
        # measured zero, which is the one thing a cost record must not invent.
        generation["inputTokens"] = _reported_count(response, "input_tokens")
        generation["outputTokens"] = _reported_count(response, "output_tokens")
        # The provider's own usage object, kept beside the two totals we read
        # from it: the totals alone could not answer why a 945-byte response was
        # billed as 2,195 output tokens. Absent when the provider reported none,
        # which is itself worth recording.
        envelope = getattr(response, "usage", None)
        generation["usage"] = dict(envelope) if isinstance(envelope, Mapping) else None
        generation["reasoningTokens"] = _reported_count(response, "reasoning_tokens")
        generation.update(_cache_and_scope(generation["usage"]))
        # The effective settings the backend reported sending.  Every backend
        # that actually issues a call passes its ``effective`` dict; the sites
        # that pass nothing are pre-call guards (no key, no client, no pinned
        # backend) where no request was sent at all, so ``None`` is the honest
        # answer there rather than an echo of what was asked for.
        sent = getattr(response, "options", None)
        generation["sentOptions"] = dict(sent) if isinstance(sent, Mapping) else None
        # 결정 §4 (2026-09-23): "Record truncation".  공급자의 stop_reason 은 백엔드마다
        # 다른 자리에 있어 여섯 경로를 다 고쳐야 하지만, max_tokens 절단의 사실은 두 수로
        # 이미 정해진다 -- 보고된 출력 토큰이 **전송된** 한도에 닿았는가.  둘 중 하나라도
        # 모르면 모른다(None)고 적는다.  2026-09-23 v4.6 판 54 생성에서 한 건도 없었다.
        limit = (generation["sentOptions"] or {}).get("maxTokens")
        out = generation.get("outputTokens")
        generation["truncatedAtLimit"] = (None if limit is None or out is None
                                          else int(out) >= int(limit))
        generation["requestedRoute"] = (self._model_identifier(getattr(response, "requested_model", None))
                                        or generation["requestedRoute"])
        # A legacy response.model often repeats the request alias: never infer
        # the actual response model from that field or from the selected label.
        generation["responseModel"] = self._model_identifier(getattr(response, "response_model", None))
        generation["responseSuccess"] = bool(getattr(response, "success", False))
        record.latency_ms += generation["latencyMs"]
        # An unknown contributes 0 to the running total and flips the record's
        # completeness flag.  See ``CallRecord.input_tokens`` for why this sums
        # rather than refuses: the total stays a usable lower bound, and
        # ``tokensComplete`` is what stops a reader mistaking it for the truth.
        if generation["inputTokens"] is None or generation["outputTokens"] is None:
            record.tokens_complete = False
        record.input_tokens += generation["inputTokens"] or 0
        record.output_tokens += generation["outputTokens"] or 0
        raw = str(getattr(response, "content", "") or "")
        record.raw = raw
        generation["responseText"] = raw
        # This attempt's body, measured before the refusal check below so that a
        # refused-but-answering generation still carries its size.  ``record.raw``
        # keeps only the last attempt's text, so without this a repaired call
        # leaves the earlier attempt's token count with nothing to compare to.
        generation["responseBytes"] = len(raw.encode("utf-8"))
        if not generation["responseSuccess"]:
            error = self._safe_backend_error(getattr(response, "error", "") or "no success", backend)
            if getattr(response, "transport_failure", False) is True:
                generation["transportFailure"] = f"{model_name} transport: {error}"
                raise TransportFailure(generation["transportFailure"])
            generation["refusedBecause"] = f"{model_name} did not answer: {error}"
            raise AnswerRefused(generation["refusedBecause"])
        parsed = getattr(response, "parsed_json", None)
        return dict(parsed) if isinstance(parsed, dict) else _extract_object(raw)

    def _send(self, model_name: str, prompt: str, system_prompt: str,
              record: CallRecord, options: Mapping[str, Any]) -> Dict[str, Any]:
        """``_generate``, re-sending the same request after transport failures.

        Exponential backoff, ``len(TRANSPORT_BACKOFF_S)`` retries.  Neither the
        prompt nor ``repair_retries`` nor the caller's re-ask budget is touched:
        nothing was answered, so there is nothing to repair.  Every attempt is
        still a generation row, with ``transportFailure`` saying why.
        """
        started = time.monotonic()
        for delay in (0.0,) + tuple(TRANSPORT_BACKOFF_S):
            if delay:
                record.transport_retries += 1
                time.sleep(delay)
            try:
                return self._generate(model_name, prompt, system_prompt, record, options)
            except TransportFailure as exc:
                last = exc
                text = str(exc).lower()
                if ("timeout" in text or "timed out" in text
                        or time.monotonic() - started >= TRANSPORT_RETRY_WINDOW_S):
                    break
        record.accepted = False
        self.calls.append(record)
        raise DecisionUnavailable(f"transport failed {record.transport_retries + 1} times: {last}",
                                  record)

    def _decide(self, role: str, phase: str, system_prompt: str,
                payload: Mapping[str, Any], schema: Mapping[str, Any],
                accept: Callable[[Dict[str, Any], CallRecord], Any],
                fallback: Callable[[], Any],
                prompt_role: Optional[str] = None) -> Tuple[Any, CallRecord]:
        """One decision: one call, at most one repair retry, else the rule."""
        model_name = self.models.model_for(role)
        from assurance.coordination import v52 as _v52
        if _v52.enabled():   # v5.2: the v5.2c system texts and the model's view of the inputs
            _kind = _v52.kind(prompt_role or role, phase, payload)
            system_prompt = _v52.system(_kind)   # v5.3 texts under AIC_V53=1
            payload = _v52.view(_kind, payload)
        prompt = _user_prompt(payload, schema)
        if self.rejection_note:
            prompt += ("\n\nYour previous answer was refused: " + self.rejection_note
                       + "\nChoose a different answer and return only the JSON object.")
        options = self.options_for(prompt_role or role, model_name)
        record = CallRecord(role=role, model=str(model_name or DETERMINISTIC),
                            phase=phase, started_at=_now(), prompt=prompt,
                            system_prompt=system_prompt,
                            # Which ``input.*`` positions this call actually
                            # carried.  The episode record keeps ``T`` and ``C``
                            # at top level for every arm -- they are the
                            # comparison grid the executor judges on -- so a
                            # reader cannot otherwise tell that the basic
                            # monolith never received them.  Its ``T``/``C``
                            # say ``provenance.model = "deterministic"``, which
                            # is easy to miss and does not say *who saw what*.
                            # This does.
                            input_keys=tuple(sorted(str(key) for key in payload)),
                            options=options.to_record())
        if model_name is None:
            record.model = DETERMINISTIC
            record.fallback_reason = "no model assigned to this role"
            return self._fall_back(record, fallback)

        refusal = ""
        attempt = 0
        while True:
            if attempt:
                if not (self.may_reask(refusal, record) if self.may_reask is not None
                        else attempt < 2):
                    break
                record.repair_retries += 1
                prompt = (record.prompt + "\n\nYour previous answer was refused: "
                          + refusal + "\nCorrect it and return only the JSON object.")
            attempt += 1
            record.dropped, record.rationale = (), ""
            try:
                parsed = self._send(model_name, prompt, system_prompt, record,
                                    options.as_options())
                value = accept(parsed, record)
            except ClarificationNeeded:
                self.calls.append(record)
                raise
            except AnswerRefused as exc:
                refusal = str(exc)
                # A validator refusal of an answer the backend delivered fine
                # lands here, and only this attempt's row can hold the reason:
                # ``fallback_reason`` is written only when BOTH attempts fail, so
                # a repaired call would otherwise lose what was wrong with the
                # first answer.  ``_generate`` has already filled in the refusals
                # it knows about, which are more specific than ``str(exc)``.
                if record.generations and record.generations[-1].get("refusedBecause") is None:
                    record.generations[-1]["refusedBecause"] = refusal
                continue
            record.accepted = True
            self.calls.append(record)
            return value, record
        # Owner instruction 2026-09-19: no deterministic stand-in for a model.
        # ``fallbackReason`` stays in the record (analysis reads the key) and
        # is always null on a model-assigned role.
        record.accepted = False
        record.rationale = record.rationale or ""
        self.calls.append(record)
        raise DecisionUnavailable(refusal or "the answer was refused", record)

    def _fall_back(self, record: CallRecord, fallback: Callable[[], Any]
                   ) -> Tuple[Any, CallRecord]:
        value = fallback()
        record.accepted = False
        record.rationale = record.rationale or "deterministic rule"
        self.calls.append(record)
        return value, record

    def to_record(self) -> Dict[str, Any]:
        return {"method": self.models.method,
                "roleModels": self.models.to_record(),
                "calls": [call.to_record() for call in self.calls]}

    # -- Target ---------------------------------------------------------------

    def form_targets(self, inputs: TargetInputs, *,
                     phase: str = PHASE_FORMATION
                     ) -> Tuple[TargetContract, CallRecord]:
        """One call: the intents and the owner's authorization become ``T``.

        The answer is either a complete compact ``T`` or a non-empty
        ``missingInformation``.  The second is not a failure -- it raises
        :class:`ClarificationNeeded` so the executor can ask the operator and
        call again with ``phase="clarification"``.
        """
        authorization = inputs.authorization or Authorization.from_intents(inputs.intents)

        def accept(parsed: Dict[str, Any], record: CallRecord) -> TargetContract:
            # No clarification from the model (see _TARGET_SCHEMA): a question
            # it sends anyway is not an answer and does not stop the sitting.
            questions = ()
            if questions and parsed.get("alternatives") is None:
                record.questions = tuple(questions)
                record.rationale = str(parsed.get("rationale", "") or "")
                record.accepted = True
                raise ClarificationNeeded(questions, record)
            contract, notes = _validated_contract(parsed, authorization)
            record.dropped = tuple(notes)
            record.rationale = str(parsed.get("rationale", "") or "")
            if questions:
                record.questions = tuple(questions)
            return _with_provenance(contract, self.models.target, None,
                                    record.rationale)

        def fallback() -> TargetContract:
            # The mandatory targets, not the whole expansion: the amendment
            # forbids a full-Omega T, and a fallback T must not look like a
            # selection anyone made.
            return mandatory_contract(omega_or_sparse(authorization))

        value, record = self._decide(ROLE_TARGET, phase,
                                     prompt_with_additions(TARGET_SYSTEM_PROMPT, authorization),
                                     inputs.payload(), _TARGET_SCHEMA, accept, fallback)
        if not record.accepted:
            value = _with_provenance(value, DETERMINISTIC, record.fallback_reason,
                                     "deterministic target contract")
        return value, record

    # -- Control --------------------------------------------------------------

    def form_controls(self, inputs: ControlInputs) -> Tuple[ControlCandidates, CallRecord]:
        """One call: ``T`` and the function catalog become ``C``."""

        def accept(parsed: Dict[str, Any], record: CallRecord) -> ControlCandidates:
            candidates = _controls_from_answer(parsed, inputs, self.models.control)
            cleaned, dropped = _validated_controls(candidates, inputs)
            cleaned = self._with_predicted_targets(cleaned, inputs)
            record.dropped = tuple(dropped)
            record.rationale = str(parsed.get("rationale", "") or "")
            return _with_control_provenance(cleaned, self.models.control, None,
                                            record.rationale)

        def fallback() -> ControlCandidates:
            return self._deterministic_controls_for(inputs)

        prompt = (CONTROL_COVERAGE_SYSTEM_PROMPT
                  if self.models.method in COVERAGE_METHODS else CONTROL_SYSTEM_PROMPT)
        value, record = self._decide(ROLE_CONTROL, PHASE_FORMATION, prompt,
                                     inputs.payload(), _CONTROL_SCHEMA, accept, fallback)
        if not record.accepted:
            value = _with_control_provenance(value, DETERMINISTIC, record.fallback_reason,
                                             "deterministic candidate set")
        return value, record

    # -- the predictor the executor re-derives every ranking from -------------

    def _predictor_for(self, inputs: Any) -> Optional[JointEffectPredictor]:
        if self.predictor is not None:
            return self.predictor
        state = dict(getattr(inputs, "network_state", {}) or {})
        if not state:
            return None
        try:
            return JointEffectPredictor(NetworkState.from_record(state))
        except Exception:  # a missing state must never take the sitting down
            return None

    def _with_predicted_targets(self, candidates: ControlCandidates,
                                inputs: Any) -> ControlCandidates:
        """Re-derive ``predictedTarget`` from the predictor, keeping the agent's
        own answer beside it (contract v2 section 3.3)."""
        contract = getattr(inputs, "target_contract", None)
        predictor = self._predictor_for(inputs)
        if contract is None or predictor is None:
            return candidates
        rows = []
        for candidate in candidates.candidates:
            predicted = predictor.predict(candidate.configuration)
            kpis = {key: value for key, (value, _u) in predicted.items()}
            # Owner instruction 2026-09-19: the executor's prediction stays
            # internal.  The agent's own ``predicted``/``uncertainty``/
            # ``predictedTarget`` are kept as it answered -- never filled in or
            # overwritten from the predictor, because the candidates go on to
            # Trajectory's prompt.
            rows.append(replace(
                candidate,
                predictor_target=_best_predicted_target(kpis, contract)))
        return replace(candidates, candidates=tuple(rows))

    def _deterministic_controls_for(self, inputs: Any) -> ControlCandidates:
        """``C0`` and every executable combination the frozen catalog admits,
        ranked by the cost of the target each is predicted to satisfy.

        The whole product is the default: the Kernel freezes it anyway, and a
        RAN search that only ever saw single-axis moves and their pairs would
        be answering a smaller question than the one it was asked.  A stated
        ``retain`` narrows it deliberately; nothing narrows it by accident.
        """
        catalog = getattr(inputs, "function_catalog", None)
        policy = dict(getattr(inputs, "construction_policy", {}) or {})
        stated = policy.get("retain", policy.get("maxCandidates"))
        retain = None if stated in (None, "", 0) else int(stated)
        if catalog is None:
            return ControlCandidates(
                candidates=(), action_space={}, construction_policy=policy,
                provenance={"model": DETERMINISTIC, "fallback": None,
                            "rationale": "no function catalog was exposed"})
        applied = getattr(inputs, "applied_configuration", {}) or {}
        baselines = getattr(inputs, "baselines", {}) or {}
        compatibility = getattr(inputs, "compatibility", None)
        # Both drop channels are collected.  Without them a combination the
        # compatibility rules refused during enumeration read exactly like one
        # that was never enumerated at all, and this arm has no ``CallRecord``
        # to say otherwise -- see the ``provenance["dropped"]`` note below.
        refused: List[str] = []
        # 2026-09-17 (오너 지시): "대체경로는 여러개 가질 필요도 없고 그냥 동작하는
        # configuration 딱 하나만 가지게 해".  이 경로는 모델이 터졌을 때만 돌고
        # (실측 569 호출 중 4 회 · 68 판 중 2 판, 사유는 관측 지연과 API 503),
        # **그 판의 데이터는 버린다** -- 대체 경로의 우수성을 주장할 일이 없다.
        # 그러므로 전역 최적을 고를 이유가 없고, 곱을 통째로 만들 이유는 더더욱 없다.
        # 곱을 다 만드는 것이 **액션 공간을 촘촘하게 못 만드는 유일한 이유**였다.
        # 그래서 **남길 만큼만 만든다**: 정책이 `retain` 을 말하면 그만큼, 말하지 않으면
        # `_FALLBACK_KEEP`(1) 만.  곱을 통째로 만드는 일은 더 이상 없다.  C0 는 검증기가
        # 따로 붙이므로(`validate_control_candidates`) 실제 후보 수는 그보다 하나 많다.
        product = catalog_product_controls(catalog, baselines,
                                           compatibility=compatibility,
                                           dropped=refused,
                                           limit=(int(retain) if retain
                                                  else _FALLBACK_KEEP))
        # Rank first, then cut -- the rule the LLM Control agents are given
        # verbatim ("rank by the lowest concession cost each combination is
        # predicted to satisfy ... retain the configured number of top
        # candidates").  Selecting a spanning cover *before* ranking would give
        # this arm a construction policy the model arms were never given, and
        # SINGLE_CALL.md forbids adding a separate diversity quota to the
        # supplied policy.  The cover survives only as the tie-break among
        # combinations of equal predicted cost, where the rule says nothing.
        # 2026-09-17 (오너 지시): 이 경로는 **모델이 터졌을 때만** 쓰이고 -- 실측 569 호출 중
        # 4 회(0.7%), 68 판 중 2 판 -- 사유는 관측 지연과 API 503, 둘 다 일시 장애다.
        # 그 판의 데이터는 어차피 버린다(대체 경로의 우수성을 주장할 일이 없다).
        # 그런 경로가 **액션 공간의 촘촘함을 제약하면 앞뒤가 바뀐다**: 곱을 통째로
        # 만들기 때문에 감쇠 사다리를 촘촘히 하면 여기서 먼저 터진다.
        # 그래서 **만드는 개수에 상한**을 둔다.  곱이 상한보다 작으면(오늘 1296) 동작은
        # 지금과 **한 글자도 다르지 않다**; 커질 때만 앞에서 잘라 비용을 묶는다.
        # 순위-그다음-절단이라는 규칙의 모양은 그대로 남는다.
        rows = list(product)
        spanning = self.models.method in COVERAGE_METHODS
        # Ties are broken by enumeration order, which is what the stated rule
        # leaves them at.  A spanning tie-break here would quietly give every
        # method the coverage method's construction policy, which is the whole
        # thing ``three-agent-coverage`` exists to compare against.
        cover_order = ({item.signature: index for index, item
                        in enumerate(cover_controls(product, len(product)))}
                       if spanning else {})

        contract = getattr(inputs, "target_contract", None)
        predictor = self._predictor_for(inputs)
        if predictor is not None:
            translated = []
            for candidate in rows:
                try:
                    configuration = translate_functions(
                        candidate, catalog, baselines, applied,
                        getattr(inputs, "unselected_function_rule", RULE_BASELINE))
                except ControlValidationError:
                    continue
                predicted = predictor.predict(configuration)
                kpis = {key: value for key, (value, _u) in predicted.items()}
                target = (_best_predicted_target(kpis, contract)
                          if contract is not None else "")
                # The ranking is unchanged; the predictor's numbers are simply
                # not written where a later prompt would carry them.
                translated.append(replace(
                    candidate, configuration=configuration,
                    predictor_target=target or ""))
            if contract is not None:
                def rank(item: Tuple[int, ControlCandidate]) -> Tuple[Any, ...]:
                    order, candidate = item
                    tie = cover_order.get(candidate.signature, order)
                    target = (contract.target(candidate.predictor_target)
                              if candidate.predictor_target else None)
                    if target is None:
                        # "Place combinations predicted to satisfy no target
                        # after those that satisfy one" -- SINGLE_CALL.md.
                        return (1, (), tie)
                    return (0, preference_key(target, contract.authorization,
                                              contract.preference), tie)
                translated = [candidate for _order, candidate
                              in sorted(enumerate(translated), key=rank)]
            rows = translated
        if retain is not None and len(rows) > int(retain):
            # The coverage method's own construction policy: span the product
            # rather than take its cheapest.  Its Control prompt asks the model
            # for the same thing, so the fallback answers the same question.
            rows = (list(cover_controls(rows, int(retain))) if spanning
                    else rows[:int(retain)])

        candidates = ControlCandidates(
            candidates=tuple(replace(candidate, control_id=f"C{index + 1}")
                             for index, candidate in enumerate(rows)),
            action_space=catalog.axes(), construction_policy=policy, catalog=catalog,
            provenance={"model": DETERMINISTIC, "fallback": None,
                        "rationale": (
                            "the deterministic fallback: %d candidate(s) built "
                            "from the catalog, by predicted target cost -- the "
                            "rest of the product was never enumerated"
                            % (int(retain) if retain else _FALLBACK_KEEP))})
        cleaned, dropped = validate_control_candidates(
            candidates, catalog.axes(), baselines, catalog=catalog,
            compatibility=compatibility, applied=applied,
            rule=getattr(inputs, "unselected_function_rule", RULE_BASELINE),
            retain=retain)
        # ``provenance`` is the only out-channel this function has: it returns
        # candidates rather than a record, and its three callers are
        # ``fallback()`` closures invoked without a ``CallRecord``.  It is
        # already serialized by ``to_record()``.  Deliberately *not* copied onto
        # ``record.dropped`` -- that would change what every deterministic
        # fallback reports, which is a separate decision.
        return replace(cleaned, provenance={**cleaned.provenance,
                                            "dropped": refused + dropped})

    # -- Trajectory -----------------------------------------------------------

    def select_next(self, inputs: TrajectoryInputs
                    ) -> Tuple[Optional[TrajectoryDecision], CallRecord]:
        """One call: the next control, and the target it is aimed at."""
        return self._select(ROLE_TRAJECTORY, ROLE_TRAJECTORY,
                            TRAJECTORY_SYSTEM_PROMPT, inputs)

    # -- internal monolith ----------------------------------------------------

    def monolith_form(self, inputs: MonolithFormInputs
                      ) -> Tuple[Tuple[TargetContract, ControlCandidates], CallRecord]:
        """One call on one model: ``T`` and ``C`` together."""
        target_inputs, control_inputs = inputs.target, inputs.control
        authorization = (target_inputs.authorization
                         or Authorization.from_intents(target_inputs.intents))
        model = self.models.monolith

        def accept(parsed: Dict[str, Any], record: CallRecord
                   ) -> Tuple[TargetContract, ControlCandidates]:
            contract, notes = _validated_contract(parsed, authorization)
            with_contract = replace(control_inputs, target_contract=contract)
            candidates = _controls_from_answer(parsed, with_contract, model)
            candidates, dropped_controls = _validated_controls(candidates, with_contract)
            candidates = self._with_predicted_targets(candidates, with_contract)
            record.dropped = tuple(notes) + tuple(dropped_controls)
            record.rationale = str(parsed.get("rationale", "") or "")
            return (_with_provenance(contract, model, None, record.rationale),
                    _with_control_provenance(candidates, model, None, record.rationale))

        def fallback() -> Tuple[TargetContract, ControlCandidates]:
            contract = mandatory_contract(omega_or_sparse(authorization))
            return (contract,
                    self._deterministic_controls_for(
                        replace(control_inputs, target_contract=contract)))

        value, record = self._decide(ROLE_MONOLITH, PHASE_FORMATION,
                                     prompt_with_additions(MONOLITH_FORM_SYSTEM_PROMPT,
                                                           authorization), inputs.payload(),
                                     _MONOLITH_FORM_SCHEMA, accept, fallback,
                                     prompt_role="monolith-form")
        if not record.accepted:
            contract, candidates = value
            value = (_with_provenance(contract, DETERMINISTIC, record.fallback_reason,
                                      "deterministic target contract"),
                     _with_control_provenance(candidates, DETERMINISTIC,
                                              record.fallback_reason,
                                              "deterministic candidate set"))
        return value, record

    def monolith_select(self, inputs: TrajectoryInputs
                        ) -> Tuple[Optional[TrajectoryDecision], CallRecord]:
        """One call on the same model: the next control, from the prepared ``C``."""
        return self._select(ROLE_MONOLITH, "monolith-select",
                            MONOLITH_SELECT_SYSTEM_PROMPT, inputs)

    def _select(self, role: str, prompt_role: str, system_prompt: str,
                inputs: TrajectoryInputs
                ) -> Tuple[Optional[TrajectoryDecision], CallRecord]:
        contract, controls = inputs.target_contract, inputs.control_candidates
        grid = inputs.grid if inputs.grid is not None else Grid(contract, controls)

        def accept(parsed: Dict[str, Any], record: CallRecord) -> TrajectoryDecision:
            target_id = str(parsed.get("targetId", "") or "").strip()
            control_id = str(parsed.get("controlId", "") or "").strip()
            known_targets = contract.target_ids if contract is not None else ()
            known_controls = controls.control_ids if controls is not None else ()
            if target_id not in known_targets:
                raise AnswerRefused(
                    f"targetId {target_id!r} is not one of {list(known_targets)}")
            if control_id not in known_controls:
                raise AnswerRefused(
                    f"controlId {control_id!r} is not one of {list(known_controls)}")
            record.rationale = str(parsed.get("rationale", "") or "")
            return TrajectoryDecision(target_id=target_id, control_id=control_id,
                                      rationale=record.rationale)

        def fallback() -> Optional[TrajectoryDecision]:
            return self._deterministic_trajectory(inputs, grid)

        return self._decide(role, PHASE_SELECTION, system_prompt, inputs.payload(),
                            _PAIR_SCHEMA, accept, fallback, prompt_role=prompt_role)

    def _deterministic_trajectory(self, inputs: TrajectoryInputs, grid: Grid
                                  ) -> Optional[TrajectoryDecision]:
        """The untried control whose prediction satisfies the cheapest target;
        its own ``predictedTarget`` is what it is aimed at."""
        contract, controls = inputs.target_contract, inputs.control_candidates
        if contract is None or controls is None:
            return None
        # T and C are formed in parallel, so C never saw the contract and no
        # candidate carries a ``predictorTarget``: ranking them would read as
        # "no combination satisfies any target" and hand every trial the
        # baseline.  Trajectory *does* hold T, so the aim is re-derived here --
        # once, and only when Control could not supply it.
        if not any(candidate.predictor_target or candidate.predicted_target
                   for candidate in controls.candidates):
            controls = self._with_predicted_targets(controls, inputs)
        tried = set(grid.tried_controls())
        for item in inputs.observations:
            control_id = str(dict(item).get("controlId") or "")
            if control_id and dict(item).get("valid", True):
                tried.add(control_id)
        untried = [candidate for candidate in controls.candidates
                   if candidate.control_id not in tried]
        if not untried:
            return deterministic_trajectory(contract, controls, grid,
                                            inputs.applied_configuration)

        def aim(candidate: ControlCandidate) -> str:
            return candidate.predictor_target or candidate.predicted_target

        def rank(candidate: ControlCandidate) -> Tuple[Any, ...]:
            target = (contract.target(aim(candidate)) if aim(candidate) else None)
            order = controls.control_ids.index(candidate.control_id)
            if target is None:
                return (1, (), order)
            return (0, preference_key(target, contract.authorization,
                                      contract.preference), order)

        chosen = min(untried, key=rank)
        target_id = aim(chosen) or contract.t0.target_id
        if contract.target(target_id) is None:
            target_id = contract.t0.target_id
        return TrajectoryDecision(
            target_id=target_id, control_id=chosen.control_id,
            rationale="the untried control whose prediction satisfies the "
                      "lowest-cost target")

    # -- basic monolith -------------------------------------------------------

    def basic_monolith_decide(self, inputs: BasicInputs
                              ) -> Tuple[Optional[BasicDecision], CallRecord]:
        """One call, no grid: the xApp instructions and the requirements they
        aim at.  The executor translates the instructions exactly as it
        translates a ``C`` candidate."""
        catalog = inputs.function_catalog
        baseline = inputs.baselines
        applied = inputs.applied_configuration
        rule = inputs.unselected_function_rule

        def accept(parsed: Dict[str, Any], record: CallRecord) -> BasicDecision:
            rows = parsed.get("instructions")
            if isinstance(rows, Mapping):
                rows = [rows]
            if not isinstance(rows, Sequence) or isinstance(rows, str) or not rows:
                raise AnswerRefused("no instructions in the answer")
            selections = []
            for row in snap_function_rows(rows, catalog):
                if not isinstance(row, Mapping):
                    raise AnswerRefused("an instruction is not an object")
                selections.append(FunctionSelection.from_record(row))
            if catalog is None:
                raise AnswerRefused("no function catalog was exposed to translate "
                                    "these instructions against")
            selections = _without_baseline_rows(selections, catalog, baseline, rule)
            refusals = (inputs.compatibility or CompatibilityRules()).refusals(selections)
            if refusals:
                raise AnswerRefused("; ".join(refusals))
            try:
                configuration = translate_functions(
                    ControlCandidate(control_id="B0", functions=tuple(selections)),
                    catalog, baseline, applied, rule)
            except ControlValidationError as exc:
                raise AnswerRefused(str(exc)) from exc
            # §3: 준비된 C 와 같은 최종 설정 검사.  거절 사유는 기존 repair 경로로 간다.
            clash = (inputs.compatibility or CompatibilityRules()).configuration_refusals(
                configuration, baseline)
            if clash:
                raise AnswerRefused("; ".join(clash))
            aimed, records = _original_requirements(inputs)
            record.rationale = str(parsed.get("rationale", "") or "")
            return BasicDecision(configuration=configuration, requirements=aimed,
                                 requirement_records=tuple(records),
                                 rationale=record.rationale)

        def fallback() -> Optional[BasicDecision]:
            candidates = self._deterministic_controls_for(inputs)
            tried = {tuple(sorted(item.items()))
                     for item in inputs.tried_configurations}
            rules = inputs.compatibility or CompatibilityRules()
            for candidate in candidates.candidates:
                if candidate.signature in tried:
                    continue
                if rules.configuration_refusals(candidate.configuration, baseline):
                    continue
                aimed, records = _original_requirements(inputs)
                return BasicDecision(
                    configuration=dict(candidate.configuration),
                    requirements=aimed,
                    requirement_records=tuple(records),
                    rationale="deterministic: the untried configuration whose "
                              "prediction is best, aiming at the original "
                              "requirements")
            return None

        if self.models.model_for(ROLE_MONOLITH) == RULE_GREEDY:
            return self._rule_greedy_decide(inputs)
        return self._decide(ROLE_MONOLITH, PHASE_SELECTION, BASIC_MONOLITH_SYSTEM_PROMPT,
                            inputs.payload(), _BASIC_SCHEMA, accept, fallback,
                            prompt_role="basic-monolith")

    def _rule_greedy_decide(self, inputs: BasicInputs
                            ) -> Tuple[Optional[BasicDecision], CallRecord]:
        """The ``rule-greedy`` baseline in the basic monolith's place (see
        :mod:`assurance.coordination.rule_greedy`).  No model is called."""
        from assurance.coordination import rule_greedy
        record = CallRecord(role=ROLE_MONOLITH, model=RULE_GREEDY, phase=PHASE_SELECTION,
                            started_at=_now(), prompt="", system_prompt="")
        catalog = inputs.function_catalog
        # One domain per full axis (``dlPrbCap@ue3``): a catalog may give scopes their
        # own ladders, and the strings are the frozen catalog's own (Codex 2026-09-30).
        domains: Dict[str, Any] = {axis: ("values", [str(v) for v in values])
                                   for axis, values in (catalog.axes() if catalog else {}).items()}
        rules = inputs.compatibility or CompatibilityRules()
        # The four-entry limit counts from C0 and, separately, from the applied
        # configuration -- the sitting refuses a transition over it (``_transition_exceeds``).
        limit = rules.max_changed_entries if rules.max_changed_entries is not None else 4

        def _changed(configuration, reference):
            reference = dict(reference or {})
            return sum(1 for axis, value in configuration.items()
                       if str(reference.get(axis, value)) != str(value))

        # Compatibility is counted against the sitting's fixed C0 (trial 0), not
        # ``inputs.baselines``, which follows a retained handover (Codex 2026-09-30).
        baseline = next((dict(o.get("configuration") or {}) for o in inputs.observations
                         if int(o.get("trialIndex", -1) or 0) == 0 and o.get("configuration")),
                        inputs.baselines)
        found = rule_greedy.decide(
            dict(inputs.authorization.to_full_record().get("requirements") or {}),
            list(inputs.observations), inputs.applied_configuration,
            list(inputs.tried_configurations),
            domains,
            refused=lambda configuration: bool(
                rules.configuration_refusals(configuration, baseline))
            or _changed(configuration, baseline) > limit
            or _changed(configuration, inputs.applied_configuration) > limit)
        record.accepted = found is not None
        record.rationale = found[1] if found else "rule-greedy: no untried rule step left"
        self.calls.append(record)
        if found is None:
            return None, record
        aimed, records = _original_requirements(inputs)
        return BasicDecision(configuration=found[0], requirements=aimed,
                             requirement_records=tuple(records),
                             rationale=record.rationale), record


# --------------------------------------------------------------------------- #
# answer -> object, and the provenance stamp
# --------------------------------------------------------------------------- #


def _questions_of(parsed: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The Target agent's ``missingInformation``, in the intake's own shape."""
    rows = parsed.get("missingInformation")
    if isinstance(rows, Mapping):
        rows = [rows]
    if not isinstance(rows, Sequence) or isinstance(rows, str):
        return []
    questions: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        question = {"intentId": str(row.get("intentId", "") or ""),
                    "field": str(row.get("field", "") or ""),
                    "question": str(row.get("question", "") or "")}
        if question["question"] or question["field"]:
            questions.append(question)
    return questions


#: 정책이 `retain` 을 말하지 않을 때 결정론적 대체 경로가 만들 후보 수.  이 경로는 모델이 터졌을 때만
#: 쓰이고 그 판의 데이터는 버리므로, 전역 최적일 필요가 없다 -- 집행 가능한 후보 집합이면
#: 된다.  카탈로그 기수 자체의 상한(`--max-catalog`)과는 **다른 것**이다: 저쪽은 epoch 이
#: 얼릴 수 있는 영역의 크기이고, 이쪽은 대체 경로가 실제로 만들 개수다.
_FALLBACK_KEEP = 1


def _validated_contract(parsed: Mapping[str, Any], authorization: Authorization
                        ) -> Tuple[TargetContract, List[str]]:
    """The compact answer becomes the expanded ``T``, or the call is refused."""
    # 2026-09-17 (오너 드롭 RAN_AGENT_PROMPTS_REVISED_20260917.md, Runtime
    # alignment 1): 새 프롬프트는 "The code includes the unmodified original
    # target T0" 이라고 말하는데 여기서는 그 본문을 **답에서** 요구하고 있었다.
    # 그 말을 믿고 t0 를 빼는 답이 나오면 형성 호출이 통째로 거절되고 결정론적
    # 대체로 떨어진다 -- 조용히 실험을 갉아먹는 종류의 불일치다.
    #
    # 2026-09-18 핸드오프 §4.1 (더 최근 오너 지시): 스키마에서 t0 를 **뺐다** -- 모델은
    # T0 를 되받아 적지 않고, 코드가 원본(`authorization`)으로 만든다.  아래는 그대로다.
    # 답이 t0 를 내지 않으면 코드가 원본으로 채운다.  t0 를 **낸** 답은 지금까지와 똑같이
    # 검사된다 -- 원본에서 벗어나면 `tc.py` 가 "T0 moved <req> off its original"
    # 로 거절한다.  가드는 프롬프트 문장이 아니라 그 코드에 있다.
    # **없는 것**과 **비어 있는 것**은 다르다.  `t0` 가 아예 없는 답은 프롬프트를
    # 그대로 믿은 답이므로 채워 준다.  `t0` 는 냈는데 `requirements` 가 비어 있는
    # 답은 그냥 잘못된 답이고, 수리 프롬프트로 되물어야 한다 -- 둘을 한 덩어리로
    # 묶으면 수리 경로가 통째로 죽는다(기존 테스트가 그것을 잡아 주었다).
    given = parsed.get("t0")
    if given is None:
        originals = {req_id: entry.original
                     for req_id, entry in (authorization.requirements or {}).items()}
        if not originals:
            raise AnswerRefused("no t0 in the answer and no authorized original to supply it")
        parsed = dict(parsed)
        parsed["t0"] = {"targetId": "T0", "requirements": originals}
    elif not isinstance(given, Mapping):
        raise AnswerRefused("t0 is not an object")
    elif not dict(given).get("requirements"):
        raise AnswerRefused("t0 names no requirements")
    # The model does not restate the authorized levels (see _TARGET_SCHEMA): the
    # expansion is the owner's, so anything under "levels" is ignored.
    levels: Mapping[str, Any] = {}
    constraints: Sequence[str] = ()   # not asked for; see _TARGET_SCHEMA
    # A missing field is a malformed answer, not an instruction to keep the
    # whole authorized domain: the model was asked to select, and substituting
    # the full expansion would attribute a choice it never made to it.  An
    # explicit empty list is a different answer -- "only the original" -- and
    # is handled downstream.
    selection = parsed.get("alternatives")
    if selection is None:
        raise AnswerRefused("the answer names no alternatives")
    if isinstance(selection, str) or not isinstance(selection, Sequence):
        raise AnswerRefused("alternatives is not a list")
    try:
        contract, notes = validate_target_contract(
            {"t0": parsed.get("t0"), "levels": levels or {},
             "constraints": list(constraints),
             # The owner's order stands; a model-written ranking is ignored.
             "ranking": {},
             "alternatives": list(selection)},
            authorization)
    except TargetValidationError as exc:
        raise AnswerRefused(str(exc)) from exc
    from assurance.coordination import v52 as _v52
    if _v52.v53():   # v5.3 section 4: the Target rule is checked, never padded or trimmed
        # (Codex) on the answer as given: the validator above drops duplicates and non-objects and
        # int()s fractional levels, so its eight could be a trimmed nine or a coerced answer.
        auth = authorization.to_record()
        refusal = (_v52.check_raw_targets(selection, set(auth))
                   or _v52.check_targets([t.levels for t in contract.alternatives], auth))
        if refusal:
            raise AnswerRefused(refusal + ("; dropped: " + "; ".join(notes) if notes else ""))
    return contract, notes


def _without_baseline_rows(selections, catalog, baseline, rule=None):
    """Instructions that restate C0 change nothing: drop them before the changed-entry count and the
    exclusivity rules see them.  2026-09-27 v5.2 boards 802/804: told to "explicitly repeat settings
    you intend to preserve", the basic monolith listed all ten settings (cap 0, pfWeight 1.0 ...),
    every first answer was refused (10 changed entries > 4; cap and priority on one UE) and each
    decision took a re-ask, 73-112 s.  Omitted settings revert to C0, so a C0-valued row is a no-op."""
    from assurance.coordination.tc import RULE_BASELINE
    if rule != RULE_BASELINE:          # (Codex) under keep-current a C0 row is a real reset
        return list(selections)
    kept = []
    base = {str(k): str(v) for k, v in dict(baseline or {}).items()}
    for sel in selections:
        spec = catalog.function(sel.function_id)
        try:
            axis = spec.axis_for(sel.scope) if spec is not None else None
        except Exception:  # noqa: BLE001 - an unknown scope is for translate_functions to refuse
            axis = None
        values = list(sel.policy.values())
        field_ok = spec is not None and list(sel.policy) == [getattr(spec, "axis_field", None)]
        if axis is not None and axis in base and len(values) == 1 and field_ok:   # (Codex) no typo
            a, b = str(values[0]), base[axis]
            try:
                same = float(a) == float(b)
            except (TypeError, ValueError):
                same = a == b
            if same:
                continue
        kept.append(sel)
    return kept or list(selections)   # all at C0: keep them, translate/tried rules decide


def _controls_from_answer(parsed: Mapping[str, Any], inputs: Any,
                          model: Optional[str]) -> ControlCandidates:
    rows = parsed.get("candidates")
    if not isinstance(rows, Sequence) or isinstance(rows, str) or not rows:
        raise AnswerRefused("no candidates in the answer")
    catalog = getattr(inputs, "function_catalog", None)
    candidates = []
    for item in rows:
        if not isinstance(item, Mapping):
            raise AnswerRefused("a candidate is not an object")
        has_functions = isinstance(item.get("functions"), Sequence) and item["functions"]
        has_configuration = isinstance(item.get("configuration"), Mapping)
        if not has_functions and not has_configuration:
            # "Selects nothing" is the baseline, and the contract says the
            # baseline is always present -- ``validate_control_candidates``
            # adds it as C0 itself.  A model that includes it is answering
            # correctly, so skip the row instead of refusing the whole answer:
            # refusing sent every C to the deterministic fallback and the model
            # path never ran.
            continue
        # 2026-09-20 (owner): nobody estimates values.  A model that sends them
        # anyway is not refused -- the configuration is still its answer -- but
        # the values are dropped here and never recorded or passed on.
        item = {key: value for key, value in item.items()
                if key not in ("predicted", "uncertainty", "predictedTarget",
                               "effectEstimate")}
        if has_functions:
            item["functions"] = snap_function_rows(item["functions"], catalog)
        if has_configuration:
            item["configuration"] = snap_configuration(
                item["configuration"], catalog.axes() if catalog is not None
                else getattr(inputs, "action_space", {}) or {})
        candidates.append(ControlCandidate.from_record(item))
    if not candidates:
        raise AnswerRefused("the answer names only the baseline")
    return ControlCandidates(
        candidates=tuple(candidates),
        action_space=(catalog.axes() if catalog is not None
                      else getattr(inputs, "action_space", {}) or {}),
        construction_policy=dict(getattr(inputs, "construction_policy", {}) or {}),
        catalog=catalog,
        provenance={"model": model, "fallback": None, "rationale": ""})


def _validated_controls(candidates: ControlCandidates, inputs: Any
                        ) -> Tuple[ControlCandidates, List[str]]:
    catalog = getattr(inputs, "function_catalog", None)
    cleaned, dropped = validate_control_candidates(
        candidates,
        (catalog.axes() if catalog is not None
         else getattr(inputs, "action_space", {}) or candidates.action_space),
        getattr(inputs, "baselines", {}) or {},
        catalog=catalog,
        compatibility=getattr(inputs, "compatibility", None),
        applied=getattr(inputs, "applied_configuration", {}) or {},
        rule=getattr(inputs, "unselected_function_rule", RULE_BASELINE),
        retain=getattr(inputs, "retain", None))
    if len(cleaned.candidates) <= 1:
        raise AnswerRefused(
            "no executable candidate beyond the baseline survived validation: "
            + "; ".join(dropped))
    return cleaned, dropped


def _original_requirements(inputs: "BasicInputs"
                           ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """What a basic-monolith answer aims at: the operator's own originals.

    This used to be read back out of the answer, which made a restatement of
    every intent mandatory and gave a model one more way to be refused on
    something the executor already knew.  ``_next_decision`` judges the basic
    arm against ``T0`` whatever the answer says it aimed at, so the originals
    are the only honest reading -- and the accepted and fallback paths now
    give the same one.
    """
    records = [{"intentId": intent.intent_id, "reqId": intent.requirement.req_id,
                "owner": intent.owner, "scope": intent.requirement.scope,
                "kpi": intent.requirement.kpi, "op": intent.requirement.op,
                "value": intent.requirement.value, "unit": intent.requirement.unit}
               for intent in inputs.intents]
    return inputs.authorization.originals(), records


def _best_predicted_target(kpis: Mapping[str, Any], contract: TargetContract
                           ) -> str:
    """The most preferred target a predicted KPI vector would satisfy, or ``""``.

    An estimate, recorded as one: it orders candidates and it never becomes a
    verdict -- the verdict is :func:`assurance.coordination.tc.judge_target`
    over the **measured** vector after the trial.
    """
    from assurance.coordination.tc import PASS, judge_target, verdict_of

    best, best_key = "", None
    for target in contract.targets:
        if verdict_of(judge_target(kpis, target, contract.authorization)) != PASS:
            continue
        key = preference_key(target, contract.authorization, contract.preference)
        if best_key is None or key < best_key:
            best, best_key = target.target_id, key
    return best


def _with_provenance(contract: TargetContract, model: Optional[str],
                     fallback_reason: Optional[str], rationale: str) -> TargetContract:
    provenance = dict(contract.provenance or {})
    provenance.update({"model": model or DETERMINISTIC, "fallback": fallback_reason,
                       "rationale": rationale})
    return replace(contract, provenance=provenance)


def _with_control_provenance(candidates: ControlCandidates, model: Optional[str],
                             fallback_reason: Optional[str],
                             rationale: str) -> ControlCandidates:
    provenance = dict(candidates.provenance or {})
    provenance.update({"model": model or DETERMINISTIC, "fallback": fallback_reason,
                       "rationale": rationale})
    return replace(candidates, provenance=provenance)
