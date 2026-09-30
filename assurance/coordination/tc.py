"""T (targets) and C (controls): the board the three agents search over.

One **intent** is one owner's sentence plus the structured requirement behind
it.  The **target contract** ``T`` is the original requirement vector ``T0``
plus every alternative the owner authorized (each requirement relaxed no
further than its own signed limit).  The **control candidates** ``C`` are the
joint executable configurations -- one value per action axis -- that the
deployment can actually apply.

``T`` are the rows of the board and ``C`` the columns.  A trial applies one
control, observes one KPI vector, and that single vector is judged against
**every** authorized target: one trial therefore fills a whole column, which
is why the search is over a grid and not over a list.

Everything here is deterministic and pure.  An LLM proposes a ``T`` or a ``C``
or a next cell; this module is what checks the proposal against the owner's
authorization and the deployment's action space, what computes the concession
metrics of ``exp_metrics.md`` section 2, and what judges a measured KPI vector
against a target.  No model output ever becomes a verdict: ``judge`` reads
measured numbers only.

Nothing here counts intents, owners, UEs, axes or targets: every dimension is
read from its argument.
"""

from __future__ import annotations

import itertools
import json
import math
import re
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "AuthorizedRequirement",
    "Authorization",
    "BasicDecision",
    "BestAttained",
    "CompatibilityRules",
    "ControlCandidate",
    "ControlCandidates",
    "ControlValidationError",
    "EpisodeRecord",
    "FAIL",
    "FunctionCatalog",
    "FunctionSelection",
    "FunctionSpec",
    "FUNCTION_NAMING",
    "AXIS_SCOPE_KINDS",
    "Grid",
    "Intent",
    "JointCondition",
    "LevelQuota",
    "Observation",
    "OwnerModeSet",
    "PASS",
    "PolicyField",
    "Preference",
    "Requirement",
    "RULE_BASELINE",
    "RULE_KEEP_CURRENT",
    "UNSELECTED_FUNCTION_RULES",
    "TARGET_CONTRACT_SCHEMA",
    "CONTROL_CANDIDATES_SCHEMA",
    "COST_RULE_SUM_W_Q2",
    "COST_RULE_NORMALIZED_CONCESSION",
    "DEFAULT_TIE_BREAK",
    "EPISODE_SCHEMA",
    "TERMINATION_REASONS",
    "KPI_GOODPUT",
    "KPI_SERVING_CELL",
    "KPI_DEADLINE_RATIO",
    "DEADLINE_RATIO_UNIT",
    "DEADLINE_CONCESSION_SUFFIX",
    "KPI_CELL_GOODPUT",
    "KPI_CELL_TX_ATTENUATION",
    "SUPPORTED_KPIS",
    "Target",
    "TargetContract",
    "TargetValidationError",
    "TrajectoryDecision",
    "Trial",
    "UNKNOWN",
    "axis_kind",
    "concession_of",
    "cost_of",
    "deterministic_configuration",
    "deterministic_controls",
    "catalog_product_controls",
    "cover_controls",
    "deterministic_function_moves",
    "deterministic_targets",
    "deterministic_trajectory",
    "expand_targets",
    "omega_or_sparse",
    "sparse_targets",
    "intent_from_sentence",
    "judge",
    "judge_target",
    "kpi_gaps",
    "kpi_key",
    "mode_constraint_from_record",
    "observed_best",
    "omega_contract",
    "observation_at_deadline",
    "scope_for",
    "translate_functions",
    "validate_control_candidates",
    "validate_target_contract",
    "verdict_of",
]

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

TARGET_CONTRACT_SCHEMA = "agent-target-contract/1.1.0"
CONTROL_CANDIDATES_SCHEMA = "agent-control-candidates/1.1.0"
EPISODE_SCHEMA = "agent-episode/1.1.0"

#: The weighted square of contract v2 section 2.1, ``cost = sum_i w_i q_i^2``.
#: Kept because :func:`cost_of` still computes it and older records name it, but
#: it is no longer what anything is ranked by -- see
#: :data:`COST_RULE_NORMALIZED_CONCESSION`.
COST_RULE_SUM_W_Q2 = "sum_w_q2"

#: What actually orders targets, and therefore what a model is told:
#: :func:`preference_key` compares the owners' normalized concessions ``D_o``.
#: The revision of 2026-09-14 asks for the weighted square to leave the running
#: prompts and the active selection path, and a ``costRule`` naming a rule the
#: code does not follow is precisely that: the value travels to every model in
#: ``input.authorization`` and ``input.target_contract.ranking``, so naming the
#: square there invited the model to re-rank ``T`` by a quantity the owner's
#: preference ignores.  An integer level counts subdivisions, so the square
#: ranks the same concession differently depending on how finely ``T`` split the
#: interval; ``D_o`` does not.
COST_RULE_NORMALIZED_CONCESSION = "normalized-concession"
DEFAULT_TIE_BREAK = "lexicographic(D_max, D_mean) then intent order"

#: What happens to an axis whose function a candidate did **not** select
#: (contract v2 section 3.2, ``network_state.unselectedFunctionRule``).
RULE_BASELINE = "baseline"
RULE_KEEP_CURRENT = "keep-current"
UNSELECTED_FUNCTION_RULES: Tuple[str, ...] = (RULE_BASELINE, RULE_KEEP_CURRENT)

#: How many equally spaced levels an old record's bare ``relaxLimit`` means.
#: The v1 deterministic expansion put alternatives at half of the limit and at
#: the limit, which is exactly ``steps = 2``.
LEGACY_STEPS = 2

#: Not a cap on T -- the owner forbids capping it -- but the size past
#: which an authorization is an operator mistake rather than a contract.
MAX_EXPANSION = 100000

#: Why an episode stopped (contract section 2.5).
TERMINATION_REASONS: Tuple[str, ...] = (
    "T0_SUCCESS", "BUDGET_EXHAUSTED", "CATALOG_EXHAUSTED", "DEADLINE",
    "OPERATOR_STOP", "KERNEL_TERMINATED", "EXECUTION_FAILURE",
    # A proposal the executor could not dispatch -- it named no catalog entry,
    # or it re-offered one already spent -- after the agent was given the
    # rejection reason and one bounded revision.  Distinct from
    # CATALOG_EXHAUSTED, which may only be claimed when no eligible candidate
    # remains: a rejected proposal is not by itself an exhaustion certificate.
    "PROPOSAL_FAILURE",
    # 2026-09-23: our-side interruption outlasted its bound (one wait 5 min, 10 min
    # excised per episode).  Not DEADLINE: B is never charged for it.
    "HARDWARE_UNAVAILABLE",
)

#: The KPIs this build can both emulate and observe live.
KPI_GOODPUT = "dlGoodputMbps"
KPI_SERVING_CELL = "servingCell"

#: Scenario I4 (``exp_3UEscenario.md``): "fraction of tagged echo requests
#: completed within D1 at least R1", on a flow distinct from I1's.  The
#: threshold ``R1`` is the requirement's ``value``; the deadline ``D1`` rides
#: beside it as :attr:`Requirement.deadline_ms`, because the two are relaxed
#: under **separate** authorizations -- "bounded deadline extension; any
#: reliability concession separately authorized".  The observation key stays
#: ``deadlineSuccessRatio@<ue>``: one KPI, measured at whichever deadline the
#: target in force names.
KPI_DEADLINE_RATIO = "deadlineSuccessRatio"

#: A ratio is a fraction of eligible issued requests, never a percentage and
#: never a count.
DEADLINE_RATIO_UNIT = "fraction"

#: How a deadline concession is reported in :func:`concession_of`: the same
#: ``q_r`` arithmetic, filed under the requirement it belongs to so an owner's
#: ``D_o`` averages both levels of the same intent.
DEADLINE_CONCESSION_SUFFIX = "#deadline"

#: v4 (OTA_SCENARIO_REDESIGN_20260916 section 3): sustained delivery, measured
#: as the longest run of consecutive 1-s bins below a declared floor.  It rides
#: the same per-sample stream as :data:`KPI_GOODPUT` -- the rule's ``lowRun``
#: statistic is what makes it a different question of the same numbers -- so it
#: costs no extra collection.  The requirement is a ceiling (``<=`` a number of
#: bins) and is fixed across targets, so it carries no authorized level and does
#: not enlarge the target space.
KPI_LOW_DELIVERY_RUN = "lowDeliveryRunBins"

#: 셀이 실어 나른 하향 총량 -- 그 셀에 붙어 있는 UE 들의 :data:`KPI_GOODPUT` 합이다.
#: **owner 가 UE 가 아니라 셀 사업자인 유일한 KPI** 이므로 scope 도 ``cell@<nci>`` 다.
#:
#: 왜 이것이 인텐트가 될 수 있는가: 우리가 쓰는 값이 아니라 **돌려받는 값**이다.
#: 액션(cap/PF/조종)을 고르면 셀 총량은 스케줄러와 무선이 정한다.  반대로
#: ``txAttenuationDb`` 같은 설정값은 에이전트가 그대로 써서 달성할 수 있으므로
#: 인텐트가 아니라 액션이거나 실험 조건이다.
#:
#: 왜 KPM 의 ``DRB.UEThpDl`` 합이 아닌가: 그것은 gNB RLC 가 **내보낸** 바이트라
#: 재전송·헤더를 포함하고 도달 여부와 무관하다.  사업자가 셀 용량을 말할 때 묻는
#: 것은 실어 나른 **유용한** 트래픽이므로 UE 가 실제로 받은 goodput 을 합한다.
KPI_CELL_GOODPUT = "cellGoodputMbps"

#: 셀의 **설정된** 하향 송신 감쇠(dB), KPM ``RAN.Cell.TxAttenuationDb`` 되읽기 -- 에너지의
#: 대리 지표.  위 규칙대로라면 에이전트가 그대로 쓰는 설정값은 액션이지 인텐트가 아니다.
#: v4.7 (오너 2026-09-25) 은 그래도 사업자 에너지 인텐트로 둔다: 감쇠를 올리면 그 셀 UE 의
#: goodput 이 떨어지므로 **혼자서는 달성되지만 다른 소유자와 함께는 공짜가 아니다**.  판정은
#: 우리가 쓴 값이 아니라 gNB 가 KPM 으로 돌려준 값으로 한다.
KPI_CELL_TX_ATTENUATION = "cellTxAttenuationDb"

SUPPORTED_KPIS: Tuple[str, ...] = (KPI_GOODPUT, KPI_SERVING_CELL,
                                   KPI_DEADLINE_RATIO, KPI_LOW_DELIVERY_RUN,
                                   KPI_CELL_GOODPUT, KPI_CELL_TX_ATTENUATION)

_FLOOR = ">="
_CEILING = "<="
_EQUAL = "=="
_OPERATORS: Tuple[str, ...] = (_FLOOR, _CEILING, _EQUAL)

#: ``v3.1-select10-existing3`` (OTA_IMPLEMENTATION_AMENDMENT_20260914 section 3).
#: ``T = mandatory_targets union model_additional_targets`` with the mandatory
#: part derived by code -- the original plus every maximally weakened authorized
#: target -- and at most :data:`MAX_MODEL_ADDITIONS` distinct additions chosen by
#: the model, never more than :data:`MAX_TARGETS` in all.  The same numbers bind
#: Target and the internal monolith: the arms differ in *which* additions they
#: choose, never in how many they are allowed.
#:
#: This supersedes "the original plus at most nine" and its 4/3/2 allocation.
#: The mandatory points are what make the target expression error finite
#: (:func:`expression_error`); without one of them some authorized target has no
#: selected target that implies it, and the worst case is unbounded.
MAX_TARGETS: int = 10
#: Raised from 6 to 8 for v4 (OTA_SCENARIO_REDESIGN_20260916 section 4).  Under
#: the v3.1 authority the mandatory part was the original plus three maximally
#: weakened boundaries, so six additions filled ``MAX_TARGETS`` exactly.  v4's
#: owner permissions are independent, which leaves a single maximal target and
#: therefore only two mandatory points, and six additions would have left two of
#: the ten places unusable.  Eight restores the ceiling as the binding limit.
MAX_MODEL_ADDITIONS: int = 6
#: Alternatives besides ``T0`` -- mandatory boundaries plus additions.  Kept under
#: its old name because records and callers read it as the per-``T`` ceiling.
# v4.7 (.orca/drops/V47_FINAL_PLAN.md section 3.1): ``AIC_T_CAP`` is the whole-T ceiling
# including T0 (9 for v4.7).  The two mandatory points stay, so the model adds up to cap - 2.
# Unset keeps the constants above byte-for-byte.
import os as _os
if _os.environ.get("AIC_T_CAP"):
    MAX_TARGETS = int(_os.environ["AIC_T_CAP"])
    MAX_MODEL_ADDITIONS = MAX_TARGETS - 2
MAX_SELECTED_ALTERNATIVES: int = MAX_TARGETS - 1

_TOLERANCE = 1e-9


class TargetValidationError(ValueError):
    """``T0`` itself is not an authorized target; the caller must fall back."""


def _clean(text: Any) -> str:
    return str(text or "").strip()


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return None if math.isnan(float(value)) else float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _same_value(left: Any, right: Any) -> bool:
    a, b = _as_float(left), _as_float(right)
    if a is not None and b is not None:
        return abs(a - b) <= _TOLERANCE
    return _clean(left) == _clean(right)


def _relaxation(original: Optional[float], steps: Any, bound: Any
                ) -> Tuple[Optional[int], Optional[float]]:
    """Normalize one authorized concession into ``(steps, bound)``.

    The same rule the threshold uses, factored out so the deadline of a
    :data:`KPI_DEADLINE_RATIO` requirement is read exactly as its ratio is:
    ``None`` steps means the operator said nothing (missing information), ``0``
    means they refused, and a bound equal to the original is a refusal too.  A
    bound with no step count is the v1 phrasing and means
    :data:`LEGACY_STEPS`.
    """
    if original is None:
        return None, None
    count = None if steps is None else int(steps)
    limit = _as_float(bound)
    if count is None and limit is not None:
        count = LEGACY_STEPS
    if count is not None and count <= 0:
        return 0, None
    if limit is not None and abs(limit - original) <= _TOLERANCE:
        return 0, None
    if count is not None and limit is None:
        return count, None
    return count, limit


def kpi_key(kpi: str, scope: str) -> str:
    """``dlGoodputMbps`` on ``ue@131`` is observed as ``dlGoodputMbps@131``."""
    return f"{_clean(kpi)}@{scope_target(scope)}"


def scope_target(scope: str) -> str:
    """``ue@131`` -> ``131``; a bare id is its own target."""
    text = _clean(scope)
    return text.split("@", 1)[1] if "@" in text else text


#: The scope-kind placeholder in a function's axis template.
_AXIS_PLACEHOLDER = re.compile(r"<[A-Za-z]+>")

#: How each action axis is spoken about in a function catalog (contract v2
#: section 3.1, widened by v3 section 3): the function's id, which xApp carries
#: it, the policy field a model sets, its unit, the scope kind the function is
#: written at, and what has to be true before it can be used.  These are the
#: deployment's own names -- the axis stays the executor's translation key, and
#: a model never has to know it.
#:
#: Stated here, once, so the executor, the Cockpit and the runner call the same
#: knob by the same name.  ``axisTemplate`` is what
#: :meth:`FunctionSpec.axis_for` substitutes the scope target into.
FUNCTION_NAMING: Mapping[str, Mapping[str, Any]] = MappingProxyType({
    "servingCell": MappingProxyType({
        "functionId": "steer", "xapp": "traffic-steering",
        "policyField": "servingCell", "unit": "nci", "scopeKind": "ue",
        "axisTemplate": "servingCell@<ue>",
        "description": "Hands the UE over to the named serving cell.",
        "prerequisites": ("the UE is attached", "the target cell is advertised")}),
    "dlPrbCap": MappingProxyType({
        "functionId": "ue-dl-prb-cap", "xapp": "our_rc_xapp",
        "policyField": "maxDlPrbs", "unit": "PRB", "scopeKind": "ue",
        "axisTemplate": "dlPrbCap@<ue>",
        # 구 Upper/Lower 경계 어휘는 현행 표면에서 금지돼 있다
        # (`tests/test_current_surface_vocabulary.py`).  뜻은 그대로 두고 말만 바꿨다.
        "description": ("Ceiling on the number of downlink PRBs the scheduler may "
                        "allocate to this UE in a slot; 0 means no limit."),
        "prerequisites": ("the UE is attached",
                          "the controlled UE keeps its reserve")}),
    "pfWeight": MappingProxyType({
        "functionId": "ue-sched-priority", "xapp": "our_rc_xapp",
        "policyField": "pfWeight", "unit": "weight", "scopeKind": "ue",
        "axisTemplate": "pfWeight@<ue>",
        "description": ("Proportional-fair scheduling weight of this UE for the downlink; "
                        "1.0 is the default every UE starts with."),
        "prerequisites": ("the UE is attached",)}),
    "dlMcsBounds": MappingProxyType({
        "functionId": "cell-dl-mcs-bounds", "xapp": "our_rc_xapp",
        "policyField": "dlMcsBounds", "unit": "MCS-index", "scopeKind": "cell",
        "axisTemplate": "dlMcsBounds@<cell>",
        # 2026-09-21: 여섯 축 중 이 둘만 설명 없이 모델에 갔다.  그러면 축 선택 분포가
        # 방식의 차이가 아니라 **내 입력 결손**을 재게 된다.
        "description": ("Lowest and highest downlink modulation-and-coding index the "
                        "scheduler may pick; it bounds every UE on the cell."),
        "prerequisites": ("the cell is advertised",
                          "every UE on the cell shares the bounds")}),
    "txAttenuationDb": MappingProxyType({
        "functionId": "cell-tx-attenuation", "xapp": "our_rc_xapp",
        "policyField": "txAttenuationDb", "unit": "dB", "scopeKind": "cell",
        "axisTemplate": "txAttenuationDb@<cell>",
        "description": ("Downlink transmit attenuation of this cell in dB; a larger value "
                        "is less transmit power."),
        "prerequisites": ("the cell is advertised",)}),
    "slicePrbQuota": MappingProxyType({
        "functionId": "slice-prb-quota", "xapp": "our_rc_xapp",
        "policyField": "slicePrbQuota", "unit": "percent", "scopeKind": "slice",
        "axisTemplate": "slicePrbQuota@<sst>",
        "description": ("Percentage of the cell's downlink PRBs reserved for this "
                        "slice; it bounds every UE on the slice together."),
        "prerequisites": ("the S-NSSAI is configured on the cell",
                          "dedicated <= min <= max")}),
})

#: axis kind -> the scope prefix its function is written at.
AXIS_SCOPE_KINDS: Mapping[str, str] = MappingProxyType({
    kind: str(naming["scopeKind"]) for kind, naming in FUNCTION_NAMING.items()})


def axis_kind(axis: str) -> str:
    """``dlMcsBounds@12345678`` -> ``dlMcsBounds``."""
    return _clean(axis).split("@", 1)[0]


def scope_for(axis: str) -> str:
    """The scope an axis is written at: ``dlMcsBounds@123`` -> ``cell@123``.

    The inverse of :meth:`FunctionSpec.axis_for`, for the executor that has an
    axis in hand and needs the scope to route it.
    """
    kind = axis_kind(axis)
    prefix = AXIS_SCOPE_KINDS.get(kind)
    if prefix is None:
        raise ValueError(f"{axis!r} is not one of the declared axis kinds: "
                         + ", ".join(sorted(AXIS_SCOPE_KINDS)))
    return f"{prefix}@{scope_target(axis)}"


# --------------------------------------------------------------------------- #
# section 2.1 -- intents and requirements
# --------------------------------------------------------------------------- #


class ControlValidationError(ValueError):
    """A control candidate names a function, scope or policy value that is not
    on the sitting's own catalog; the candidate is excluded, never guessed at."""


@dataclass(frozen=True)
class Requirement:
    """One owner-signed condition: a KPI on a scope, an operator and a value.

    Relaxation is authorized as **a number of steps and a bound** (contract v2
    section 2.1): ``steps = L`` equally spaced levels from the original to the
    ``bound``, so ``value(q) = original + (bound - original) * q / L`` for
    ``q = 0 .. L``.  The **floor is the bound**: a non-relaxable requirement is
    ``steps = 0`` and its floor is its own original.  Any further protected
    minimum is a :class:`JointCondition` on the sitting, never a field here.

    ``steps is None`` means the operator has not said anything about
    relaxation.  That is *missing information* -- the intake checklist asks for
    it -- and is deliberately different from ``steps = 0``, which is the
    operator explicitly refusing to relax.  Both expand to the single level
    ``[original]`` so nothing downstream has to special-case an unanswered
    intent.

    ``relax_limit`` / ``relaxable`` are the v1 keys.  They are still read from
    old records (a bare ``relaxLimit`` means ``steps = 2``, which is exactly
    what the v1 expansion produced) and still written, so an episode written
    before this contract still loads.

    A :data:`KPI_DEADLINE_RATIO` requirement carries a **second** authorized
    concession beside the threshold: ``deadline_ms`` is the deadline ``D1`` the
    ratio is measured at, and ``deadline_steps`` / ``deadline_bound`` are what
    the owner signed for extending it.  The scenario keeps the two apart on
    purpose -- "bounded deadline extension; any reliability concession
    separately authorized" -- so the expansion treats them as two requirement
    levels of the same intent rather than as one blended knob.  A requirement
    with no deadline is every other KPI and is untouched by any of this.
    """

    req_id: str
    kpi: str
    scope: str
    op: str
    value: Any
    unit: str = ""
    steps: Optional[int] = None
    bound: Optional[float] = None
    relax_limit: Optional[float] = None
    relaxable: bool = True
    deadline_ms: Optional[float] = None
    deadline_steps: Optional[int] = None
    deadline_bound: Optional[float] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "req_id", _clean(self.req_id))
        # The deadline is normalized first and unconditionally: a categorical
        # requirement returns early below, and a deadline that survived that
        # return unnormalized would be a raw string on a frozen dataclass.
        deadline = _as_float(self.deadline_ms)
        object.__setattr__(self, "deadline_ms", deadline)
        d_steps, d_bound = _relaxation(deadline, self.deadline_steps,
                                       self.deadline_bound)
        object.__setattr__(self, "deadline_steps", d_steps)
        object.__setattr__(self, "deadline_bound", d_bound)
        object.__setattr__(self, "kpi", _clean(self.kpi))
        object.__setattr__(self, "scope", _clean(self.scope))
        object.__setattr__(self, "op", _clean(self.op))
        object.__setattr__(self, "unit", _clean(self.unit))
        if not self.req_id:
            raise ValueError("a requirement needs a reqId")
        if self.op not in _OPERATORS:
            raise ValueError(f"requirement {self.req_id}: operator {self.op!r} "
                             f"is not one of {list(_OPERATORS)}")
        original = _as_float(self.value)
        if original is None or self.op == _EQUAL:
            # a categorical requirement (a serving cell NCI) is never relaxable
            # and never numeric: 87654321 is a name, not a quantity
            object.__setattr__(self, "value", _clean(self.value))
            object.__setattr__(self, "steps", 0)
            object.__setattr__(self, "bound", None)
            object.__setattr__(self, "relax_limit", None)
            object.__setattr__(self, "relaxable", False)
            return
        object.__setattr__(self, "value", original)

        steps = self.steps
        bound = _as_float(self.bound)
        if steps is not None:
            steps = int(steps)
        if not self.relaxable:                     # a v1 record saying "no"
            steps, bound = 0, None
        if bound is None:                          # a v1 record's bare limit
            legacy = _as_float(self.relax_limit)
            if legacy is not None:
                bound = legacy
                if steps is None:
                    steps = (LEGACY_STEPS if abs(legacy - original) > _TOLERANCE
                             else 0)
        if steps is None and bound is not None:
            steps = LEGACY_STEPS
        if steps is not None and steps <= 0:
            steps, bound = 0, None
        if bound is not None and abs(bound - original) <= _TOLERANCE:
            steps, bound = 0, None
        object.__setattr__(self, "steps", steps)
        object.__setattr__(self, "bound", bound)
        object.__setattr__(self, "relax_limit",
                           bound if bound is not None else original)
        object.__setattr__(self, "relaxable", bool(steps) and bound is not None)

    # -- what the operator authorized ----------------------------------------

    @property
    def relaxation_stated(self) -> bool:
        """Did the operator actually say how far this may be relaxed?

        ``steps = 0`` (explicitly non-relaxable) counts as stated; ``steps``
        left unset, or set without a bound, does not.
        """
        if self.steps is None:
            return False
        return self.steps == 0 or self.bound is not None

    @property
    def levels(self) -> Tuple[Any, ...]:
        """``[original .. bound]`` in ``steps`` equal intervals; ``q`` indexes it."""
        if not self.relaxable:
            return (self.value,)
        span = float(self.bound) - float(self.value)
        count = int(self.steps)
        return tuple(round(float(self.value) + span * q / count, 9)
                     for q in range(count + 1))

    @property
    def floor(self) -> Any:
        """The bound is the floor; a non-relaxable requirement floors at itself."""
        return self.bound if self.relaxable else self.value

    def value_at(self, q: int) -> Any:
        levels = self.levels
        index = int(q)
        if index < 0 or index >= len(levels):
            raise ValueError(f"requirement {self.req_id}: level {q} is outside "
                             f"0..{len(levels) - 1}")
        return levels[index]

    @property
    def observation_key(self) -> str:
        return kpi_key(self.kpi, self.scope)

    @property
    def numeric(self) -> bool:
        return isinstance(self.value, float)

    # -- the deadline, when this requirement has one -------------------------

    @property
    def has_deadline(self) -> bool:
        """Is this a ``ratio within D`` requirement at all?"""
        return self.deadline_ms is not None

    @property
    def deadline_relaxable(self) -> bool:
        return bool(self.deadline_steps) and self.deadline_bound is not None

    @property
    def deadline_stated(self) -> bool:
        """Did the operator say whether the deadline may be extended?

        Unlike the threshold, silence here is **not** a gap the checklist has
        to close: a deadline nobody authorized an extension for simply stays
        where it is.  Steps given without a bound is still a gap.
        """
        if self.deadline_steps is None:
            return self.deadline_bound is None
        return self.deadline_steps == 0 or self.deadline_bound is not None

    @property
    def deadline_levels(self) -> Tuple[Any, ...]:
        """``[D1 .. deadline_bound]`` in ``deadline_steps`` equal intervals."""
        if self.deadline_ms is None:
            return ()
        if not self.deadline_relaxable:
            return (self.deadline_ms,)
        span = float(self.deadline_bound) - float(self.deadline_ms)
        count = int(self.deadline_steps)
        return tuple(round(float(self.deadline_ms) + span * q / count, 9)
                     for q in range(count + 1))

    def deadline_at(self, q: int) -> Any:
        levels = self.deadline_levels
        index = int(q)
        if index < 0 or index >= len(levels):
            raise ValueError(f"requirement {self.req_id}: deadline level {q} is "
                             f"outside 0..{len(levels) - 1}")
        return levels[index]

    def to_record(self) -> Dict[str, Any]:
        record = {"reqId": self.req_id, "kpi": self.kpi, "scope": self.scope,
                  "op": self.op, "value": self.value, "unit": self.unit,
                  "steps": self.steps, "bound": self.bound,
                  "levels": list(self.levels),
                  # the v1 keys, so a reader written against 1.0.0 still works
                  "relaxLimit": self.relax_limit, "relaxable": self.relaxable}
        # Written only when there is one, so every record this build already
        # emits stays byte-identical.
        if self.has_deadline:
            record.update({"deadlineMs": self.deadline_ms,
                           "deadlineSteps": self.deadline_steps,
                           "deadlineBound": self.deadline_bound,
                           "deadlineLevels": list(self.deadline_levels)})
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Requirement":
        record = dict(record or {})
        steps = record.get("steps", record.get("relaxSteps"))
        deadline_steps = record.get("deadlineSteps", record.get("deadline_steps"))
        return cls(req_id=record.get("reqId", record.get("req_id", "")),
                   kpi=record.get("kpi", ""), scope=record.get("scope", ""),
                   op=record.get("op", _FLOOR), value=record.get("value"),
                   unit=record.get("unit", ""),
                   steps=None if steps is None else int(steps),
                   bound=record.get("bound"),
                   relax_limit=record.get("relaxLimit", record.get("relax_limit")),
                   relaxable=bool(record.get("relaxable", True)),
                   deadline_ms=record.get("deadlineMs", record.get("deadline_ms")),
                   deadline_steps=(None if deadline_steps is None
                                   else int(deadline_steps)),
                   deadline_bound=record.get("deadlineBound",
                                             record.get("deadline_bound")))


@dataclass(frozen=True)
class Intent:
    """One operator sentence and the requirement it stands for.

    ``weight`` is the finite priority weight ``w_i`` of ``SINGLE_CALL.md``.
    When the operator gives only a ``priority``, the sitting derives
    ``w_i = 1 + (max_priority - priority_i)`` -- a higher priority (a smaller
    number) earns a larger weight, so relaxing it costs more.
    """

    intent_id: str
    owner: str
    ue_id: str
    requirement: Requirement
    priority: int = 1
    sentence: str = ""
    weight: Optional[float] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _clean(self.intent_id))
        object.__setattr__(self, "owner", _clean(self.owner) or self.intent_id)
        object.__setattr__(self, "ue_id", _clean(self.ue_id))
        object.__setattr__(self, "sentence", _clean(self.sentence))
        object.__setattr__(self, "priority", int(self.priority))
        weight = _as_float(self.weight)
        object.__setattr__(self, "weight", weight)
        if not self.intent_id:
            raise ValueError("an intent needs an intentId")
        # A structured record (the Cockpit's intent-set form, --intents-json)
        # names its UE once, on the intent.  Deriving the requirement's scope
        # from it means the KPI key is `dlGoodputMbps@131` either way: without
        # this, a record that omits `scope` measures a real value and then
        # judges every target UNKNOWN against `dlGoodputMbps@`, which reads as
        # "the sitting learned nothing" when in fact the operator only left
        # out a field they had already given.
        if self.ue_id and not self.requirement.scope:
            object.__setattr__(self, "requirement",
                               replace(self.requirement, scope=f"ue@{self.ue_id}"))

    def weight_or_default(self, max_priority: int) -> float:
        """The operator's own ``w_i``, or the priority-derived default."""
        if self.weight is not None and self.weight > 0:
            return float(self.weight)
        return float(1 + max(0, int(max_priority) - int(self.priority)))

    def to_record(self) -> Dict[str, Any]:
        record = {"intentId": self.intent_id, "owner": self.owner, "ueId": self.ue_id,
                  "priority": self.priority, "sentence": self.sentence,
                  "requirement": self.requirement.to_record()}
        if self.weight is not None:
            record["weight"] = self.weight
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Intent":
        return cls(intent_id=record.get("intentId", record.get("intent_id", "")),
                   owner=record.get("owner", ""),
                   ue_id=str(record.get("ueId", record.get("ue_id", ""))),
                   requirement=Requirement.from_record(record.get("requirement", {})),
                   priority=int(record.get("priority", 1)),
                   sentence=record.get("sentence", ""),
                   weight=record.get("weight"))


_SENTENCE_ID = re.compile(r"^\s*([A-Za-z][\w-]*)\s*:\s*")
_UE_ID = re.compile(r"ue\s*id\s*=\s*([0-9]+)", re.IGNORECASE)
_OWNER = re.compile(r"owner\s+([\w.@-]+)", re.IGNORECASE)
_PRIORITY = re.compile(r"priority\s+([0-9]+)", re.IGNORECASE)
_CELL = re.compile(r"(?:serving\s+cell|cell)\s+([0-9]{3,})", re.IGNORECASE)
_MBPS = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*mbps", re.IGNORECASE)
_AT_LEAST = re.compile(r"at\s+least|no\s+less\s+than|minimum\s+of|floor\s+of", re.IGNORECASE)
_AT_MOST = re.compile(r"at\s+most|no\s+more\s+than|maximum\s+of|ceiling\s+of", re.IGNORECASE)
_RELAX = re.compile(r"(?:relaxable\s+(?:down\s+)?(?:to|up\s+to)|down\s+to|up\s+to|"
                    r"relaxable\s+by)\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
#: "relaxable in 2 steps to 1.0" -- the steps-and-bound form of contract v2.
_RELAX_STEPS = re.compile(
    r"relaxable\s+in\s+([0-9]+)\s+steps?\s+(?:down\s+)?to\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE)
#: "not relaxable" / "non-relaxable" -- the operator saying zero steps out loud.
_NON_RELAXABLE = re.compile(r"non[-\s]?relaxable|not\s+relaxable", re.IGNORECASE)

#: Scenario I4's own sentence: "at least 95% of tagged echo requests within
#: 50 ms for ueId=131, relaxable in 2 steps to 0.9".  The flow is named (a
#: tagged echo request), the reliability is a percentage or a bare fraction and
#: the deadline is a duration in milliseconds.
_TAGGED_ECHO = re.compile(r"tagged\s+echo|echo\s+requests?", re.IGNORECASE)
_PERCENT = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(?:%|per\s?cent|percent)",
                      re.IGNORECASE)
_FRACTION = re.compile(r"(?:at\s+least|no\s+less\s+than|minimum\s+of|floor\s+of)"
                       r"\s+([0-9]*\.?[0-9]+)", re.IGNORECASE)
_DEADLINE_MS = re.compile(r"(?:within|inside|under|by)\s+([0-9]+(?:\.[0-9]+)?)\s*"
                          r"(ms|milliseconds?|s|seconds?)\b", re.IGNORECASE)
#: The deadline's **own** authorization, kept apart from the ratio's on
#: purpose: "bounded deadline extension; any reliability concession separately
#: authorized".  It is matched -- and removed -- before the ratio's relaxation
#: phrase is read, so "deadline relaxable in 1 step to 80 ms" can never be
#: mistaken for a ratio bound of 80.
_DEADLINE_RELAX_STEPS = re.compile(
    r"deadline\s+(?:relaxable|extendable|extensible|extended|extension)\s+in\s+"
    r"([0-9]+)\s+steps?\s+(?:up\s+)?to\s*([0-9]+(?:\.[0-9]+)?)\s*"
    r"(ms|milliseconds?|s|seconds?)\b", re.IGNORECASE)
_DEADLINE_RELAX = re.compile(
    r"deadline\s+(?:relaxable|extendable|extensible|extended|extension)\s+"
    r"(?:up\s+)?to\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|milliseconds?|s|seconds?)\b",
    re.IGNORECASE)


def _milliseconds(number: str, unit: str) -> float:
    """``50 ms`` and ``0.05 s`` are the same deadline."""
    scale = 1000.0 if _clean(unit).lower().startswith("s") else 1.0
    return float(number) * scale


def _as_fraction(value: float) -> float:
    """``95`` written where a fraction belongs is 95 per cent.

    A reliability ratio cannot exceed 1, so a number above it is the operator
    speaking in per cent; anything at or below 1 is taken exactly as written.
    """
    return value / 100.0 if value > 1.0 else value


def _deadline_requirement(req_id: str, scope: str, body: str, text: str
                          ) -> Requirement:
    """The ``deadlineSuccessRatio`` half of :func:`intent_from_sentence`.

    ``D1`` is deliberately **not** invented when the sentence leaves it out: a
    requirement with no deadline is returned, the intake checklist asks for
    ``deadlineMs`` like any other missing field, and nothing downstream gets a
    guessed deadline to measure against.
    """
    if _AT_MOST.search(body) is not None and _AT_LEAST.search(body) is None:
        raise ValueError(
            f"the sentence {text!r} states a deadline-success ratio as a ceiling; "
            "a completed-within-the-deadline fraction is a floor (at least R1)")
    percent = _PERCENT.search(body)
    fraction = _FRACTION.search(body)
    if percent is not None:
        value = float(percent.group(1)) / 100.0
    elif fraction is not None:
        value = _as_fraction(float(fraction.group(1)))
    else:
        raise ValueError(f"the sentence {text!r} names no completed fraction; write "
                         "'at least 95% of tagged echo requests'")

    # The deadline's own authorization is read -- and removed -- first, so the
    # ratio's relaxation phrase cannot pick up the deadline's number.
    deadline_steps: Optional[int] = None
    deadline_bound: Optional[float] = None
    deadline_steps_match = _DEADLINE_RELAX_STEPS.search(body)
    deadline_relax_match = _DEADLINE_RELAX.search(body)
    if deadline_steps_match is not None:
        deadline_steps = int(deadline_steps_match.group(1))
        deadline_bound = _milliseconds(deadline_steps_match.group(2),
                                       deadline_steps_match.group(3))
    elif deadline_relax_match is not None:
        deadline_steps = LEGACY_STEPS
        deadline_bound = _milliseconds(deadline_relax_match.group(1),
                                       deadline_relax_match.group(2))
    ratio_body = _DEADLINE_RELAX_STEPS.sub(" ", body)
    ratio_body = _DEADLINE_RELAX.sub(" ", ratio_body)

    deadline_ms: Optional[float] = None
    deadline_match = _DEADLINE_MS.search(ratio_body)
    if deadline_match is not None:
        deadline_ms = _milliseconds(deadline_match.group(1), deadline_match.group(2))

    steps_match = _RELAX_STEPS.search(ratio_body)
    relax_match = _RELAX.search(ratio_body)
    if steps_match is not None:
        steps: Optional[int] = int(steps_match.group(1))
        bound: Optional[float] = _as_fraction(float(steps_match.group(2)))
    elif relax_match is not None:
        steps, bound = LEGACY_STEPS, _as_fraction(float(relax_match.group(1)))
    elif _NON_RELAXABLE.search(ratio_body) is not None:
        steps, bound = 0, None
    else:
        steps, bound = None, None
    return Requirement(req_id=req_id, kpi=KPI_DEADLINE_RATIO, scope=scope,
                       op=_FLOOR, value=value, unit=DEADLINE_RATIO_UNIT,
                       steps=steps, bound=bound, deadline_ms=deadline_ms,
                       deadline_steps=deadline_steps, deadline_bound=deadline_bound)


def intent_from_sentence(sentence: str, intent_id: Optional[str] = None, *,
                         owner: Optional[str] = None,
                         priority: Optional[int] = None) -> Intent:
    """Read one operator sentence into an :class:`Intent`.

    Small and forgiving on purpose: it reads ``ueId=<n>``, a number with
    ``Mbps`` under ``at least`` / ``at most``, an authorized limit written as
    ``relaxable to <n>`` / ``down to <n>`` / ``up to <n>``, ``priority <n>``,
    ``owner <name>`` and ``serving cell <nci>``.  A leading ``I1:`` names the
    intent when the caller did not.  Anything it cannot read is a
    :class:`ValueError` carrying the sentence, never a guess.

    Scenario I4's sentence -- "at least 95% of tagged echo requests within
    50 ms for ueId=131, relaxable in 2 steps to 0.9" -- reads into a
    :data:`KPI_DEADLINE_RATIO` requirement whose ``value`` is ``R1`` and whose
    ``deadline_ms`` is ``D1``; a deadline extension is authorized in its own
    phrase ("deadline relaxable in 1 step to 80 ms") because the two are
    separately signed.
    """
    text = _clean(sentence)
    if not text:
        raise ValueError("empty intent sentence")
    body = text
    match = _SENTENCE_ID.match(body)
    if match is not None:
        intent_id = _clean(intent_id) or match.group(1)
        body = body[match.end():]
    intent_id = _clean(intent_id)
    if not intent_id:
        raise ValueError(f"no intent id for the sentence {text!r}; write 'I1: ...' "
                         "or pass intent_id")

    ue_match = _UE_ID.search(body)
    if ue_match is None:
        raise ValueError(f"no 'ueId=<n>' in the sentence {text!r}")
    ue_id = ue_match.group(1)

    resolved_owner = _clean(owner)
    if not resolved_owner:
        owner_match = _OWNER.search(body)
        resolved_owner = owner_match.group(1) if owner_match else intent_id
    if priority is None:
        priority_match = _PRIORITY.search(body)
        priority = int(priority_match.group(1)) if priority_match else 1

    req_id = f"{intent_id}.r1"
    scope = f"ue@{ue_id}"
    cell_match = _CELL.search(body)
    mbps_match = _MBPS.search(body)
    if _TAGGED_ECHO.search(body) is not None:
        requirement = _deadline_requirement(req_id, scope, body, text)
    elif mbps_match is not None:
        floor = _AT_LEAST.search(body) is not None
        ceiling = _AT_MOST.search(body) is not None
        if floor == ceiling:
            raise ValueError(
                f"the sentence {text!r} does not say 'at least' or 'at most' for its "
                "Mbps threshold")
        value = float(mbps_match.group(1))
        steps_match = _RELAX_STEPS.search(body)
        relax_match = _RELAX.search(body)
        if steps_match is not None:
            steps: Optional[int] = int(steps_match.group(1))
            bound: Optional[float] = float(steps_match.group(2))
        elif relax_match is not None:
            # a bound with no step count: the v1 phrasing, two equal steps
            steps, bound = LEGACY_STEPS, float(relax_match.group(1))
        elif _NON_RELAXABLE.search(body) is not None:
            steps, bound = 0, None
        else:
            # the operator said nothing: missing, not non-relaxable
            steps, bound = None, None
        requirement = Requirement(
            req_id=req_id, kpi=KPI_GOODPUT, scope=scope,
            op=_FLOOR if floor else _CEILING, value=value, unit="Mbps",
            steps=steps, bound=bound)
    elif cell_match is not None:
        requirement = Requirement(req_id=req_id, kpi=KPI_SERVING_CELL, scope=scope,
                                  op=_EQUAL, value=cell_match.group(1), unit="nci")
    else:
        raise ValueError(f"no supported KPI in the sentence {text!r}; write a Mbps "
                         "threshold or a serving cell")
    return Intent(intent_id=intent_id, owner=resolved_owner, ue_id=ue_id,
                  requirement=requirement, priority=priority, sentence=text)


# --------------------------------------------------------------------------- #
# authorization and preference
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class JointCondition:
    """A predicate on one observed KPI key that binds the **whole** sitting.

    Contract v2 section 2.1: the per-requirement floor *is* the bound, so a
    further protected minimum -- "whatever else you do, UE 133 keeps 0.5 Mbps"
    -- is a joint condition, never a field on somebody's requirement.  Every
    target must respect it (a combination that would authorize less is dropped
    from ``T``) and every trial is checked against it.
    """

    kpi: str
    op: str
    value: Any
    unit: str = ""
    owner: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "kpi", _clean(self.kpi))
        object.__setattr__(self, "op", _clean(self.op) or _FLOOR)
        object.__setattr__(self, "unit", _clean(self.unit))
        object.__setattr__(self, "owner", _clean(self.owner))
        if not self.kpi:
            raise ValueError("a joint condition needs a KPI key")
        if self.op not in _OPERATORS:
            raise ValueError(f"joint condition on {self.kpi}: operator {self.op!r} "
                             f"is not one of {list(_OPERATORS)}")

    def holds(self, observed: Any) -> Optional[bool]:
        """``None`` when nothing was observed for this key."""
        if observed is None:
            return None
        measured, wanted = _as_float(observed), _as_float(self.value)
        if measured is None or wanted is None:
            return _clean(observed) == _clean(self.value)
        if self.op == _FLOOR:
            return measured >= wanted - _TOLERANCE
        if self.op == _CEILING:
            return measured <= wanted + _TOLERANCE
        return abs(measured - wanted) <= _TOLERANCE

    def allows_threshold(self, threshold: Any) -> bool:
        """May a target promise only ``threshold`` on this key?

        A target that authorizes less than the protected minimum would let a
        trial pass while the condition is broken, so it is not an authorized
        target at all.
        """
        return bool(self.holds(threshold))

    def to_record(self) -> Dict[str, Any]:
        return {"kpi": self.kpi, "op": self.op, "value": self.value,
                "unit": self.unit, "owner": self.owner}

    @classmethod
    def from_record(cls, record: Any) -> Optional["JointCondition"]:
        """``None`` for anything that is not a structured condition (a free
        sentence the operator wrote stays a sentence).

        ``kpi`` is the *observed* key, so ``scope`` -- which every requirement
        record carries -- composes into it exactly as
        :attr:`AuthorizedRequirement.observation_key` does.  Writing the
        condition the natural way (``kpi: dlGoodputMbps``, ``scope: ue@132``)
        used to bind nothing at all, because :meth:`Authorization.condition_for`
        matches the composed key and the record kept the bare KPI.  A record
        that already carries the composed key keeps it untouched.
        """
        if isinstance(record, JointCondition):
            return record
        if not isinstance(record, Mapping):
            return None
        kpi = _clean(record.get("kpi") or record.get("kpiKey"))
        if not kpi or "value" not in record:
            return None
        scope = _clean(record.get("scope"))
        if scope and "@" not in kpi:
            kpi = kpi_key(kpi, scope)
        return cls(kpi=kpi, op=_clean(record.get("op", _FLOOR)),
                   value=record.get("value"), unit=_clean(record.get("unit")),
                   owner=_clean(record.get("owner")))


def _axis_id(req_id: str, deadline: bool = False) -> str:
    """How one relaxation axis is named: ``I2.g`` and ``I2.d#deadline``.

    The same spelling :func:`concession_of` already files a deadline concession
    under, so a constraint names the dimensions in the vocabulary the record
    already uses instead of inventing a second one.
    """
    req_id = _clean(req_id)
    return f"{req_id}{DEADLINE_CONCESSION_SUFFIX}" if deadline else req_id


@dataclass(frozen=True)
class OwnerModeSet:
    """The level combinations one owner signed for, *across* their axes.

    A :class:`JointCondition` is a predicate on one observed KPI value, so it
    can only ever carve on one axis at a time: it cannot say "relax the goodput
    **or** the deadline, never both".  That is a restriction on the owner's
    joint mode ``(g, d)``, and this is it -- ``axes`` names the dimensions in
    order and ``allow`` lists the level tuples permitted over them.

    Nothing here knows what a goodput or a deadline is: ``axes`` are whatever
    :func:`_axis_id` spells, so the same type carries an owner with three axes
    or one.  An axis the target does not name counts as its level ``0``, which
    is what "did not concede on that dimension" means everywhere else.
    """

    axes: Tuple[str, ...]
    allow: Tuple[Tuple[int, ...], ...]
    owner: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "axes",
                           tuple(_clean(axis) for axis in self.axes if _clean(axis)))
        object.__setattr__(self, "owner", _clean(self.owner))
        if not self.axes:
            raise ValueError("an owner mode set needs at least one axis")
        modes = []
        for mode in self.allow or ():
            row = tuple(int(level) for level in mode)
            if len(row) != len(self.axes):
                raise ValueError(
                    f"mode {row} names {len(row)} levels but the set is written "
                    f"over {len(self.axes)} axes {list(self.axes)}")
            if row not in modes:
                modes.append(row)
        if not modes:
            raise ValueError(
                f"the mode set over {list(self.axes)} permits nothing at all")
        object.__setattr__(self, "allow", tuple(modes))

    def permits(self, levels: Mapping[str, int]) -> bool:
        return tuple(int(levels.get(axis, 0)) for axis in self.axes) in self.allow

    def describe(self) -> str:
        modes = ", ".join("(" + ",".join(str(level) for level in mode) + ")"
                          for mode in self.allow)
        who = f"{self.owner} " if self.owner else ""
        return (f"{who}may only sit at ({', '.join(self.axes)}) = {modes}")

    def to_record(self) -> Dict[str, Any]:
        return {"owner": self.owner, "axes": list(self.axes),
                "allow": [list(mode) for mode in self.allow]}


@dataclass(frozen=True)
class LevelQuota:
    """How many of a set of axes may sit at ``level`` or past it, at once.

    The cross-*owner* rule -- "at most one owner may use the extended deadline"
    -- is a cardinality over the sitting, not a predicate any single owner can
    state, so it is neither a :class:`JointCondition` nor an
    :class:`OwnerModeSet`.

    The test is ``>= level`` rather than ``== level`` so that an authorization
    which later subdivides the same interval more finely still means "at or
    past the extension", instead of quietly counting nobody.
    """

    axes: Tuple[str, ...]
    at_most: int
    level: int = 1
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "axes",
                           tuple(_clean(axis) for axis in self.axes if _clean(axis)))
        object.__setattr__(self, "at_most", int(self.at_most))
        object.__setattr__(self, "level", int(self.level))
        object.__setattr__(self, "note", _clean(self.note))
        if not self.axes:
            raise ValueError("a level quota needs at least one axis")
        if self.at_most < 0:
            raise ValueError("a level quota cannot admit a negative count")

    def permits(self, levels: Mapping[str, int]) -> bool:
        used = sum(1 for axis in self.axes if int(levels.get(axis, 0)) >= self.level)
        return used <= self.at_most

    def describe(self) -> str:
        return (self.note or
                f"at most {self.at_most} of ({', '.join(self.axes)}) may sit at "
                f"level {self.level} or past it")

    def to_record(self) -> Dict[str, Any]:
        return {"axes": list(self.axes), "atMost": self.at_most,
                "level": self.level, "note": self.note}


#: What :attr:`Authorization.mode_constraints` may hold.
ModeConstraint = (OwnerModeSet, LevelQuota)


def mode_constraint_from_record(record: Any) -> Optional[Any]:
    """Read either shape back; ``None`` for anything that is not one.

    The discriminator is the field that carries the meaning -- ``allow`` for a
    mode set, ``atMost`` for a quota -- so the stored record reads as what it
    is and needs no ``kind`` tag beside it.
    """
    if isinstance(record, ModeConstraint):
        return record
    if not isinstance(record, Mapping):
        return None
    axes = tuple(_clean(axis) for axis in (record.get("axes") or ()) if _clean(axis))
    if not axes:
        return None
    if record.get("allow") is not None:
        return OwnerModeSet(axes=axes, allow=tuple(tuple(mode) for mode in record["allow"]),
                            owner=_clean(record.get("owner")))
    at_most = record.get("atMost", record.get("at_most"))
    if at_most is None:
        return None
    return LevelQuota(axes=axes, at_most=int(at_most),
                      level=int(record.get("level", 1)),
                      note=_clean(record.get("note")))


@dataclass(frozen=True)
class AuthorizedRequirement:
    """What the owner signed for one requirement: its original, its steps and
    its bound, and the finite priority weight relaxing it is charged at.

    ``limit`` is kept as the v1 name for the bound (the floor), so a reader
    written against 1.0.0 still sees the same number.
    """

    req_id: str
    op: str
    original: Any
    limit: Any
    owner: str = ""
    unit: str = ""
    kpi: str = ""
    scope: str = ""
    steps: int = 0
    weight: float = 1.0
    #: ``D1`` and what the owner separately signed for extending it; ``None``
    #: for every requirement that is not a deadline-success ratio.
    deadline_ms: Optional[float] = None
    deadline_steps: int = 0
    deadline_bound: Optional[float] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "req_id", _clean(self.req_id))
        object.__setattr__(self, "op", _clean(self.op) or _FLOOR)
        object.__setattr__(self, "owner", _clean(self.owner))
        object.__setattr__(self, "unit", _clean(self.unit))
        object.__setattr__(self, "kpi", _clean(self.kpi))
        object.__setattr__(self, "scope", _clean(self.scope))
        object.__setattr__(self, "weight", float(self.weight or 1.0))
        deadline = _as_float(self.deadline_ms)
        object.__setattr__(self, "deadline_ms", deadline)
        d_steps, d_bound = _relaxation(deadline, self.deadline_steps or 0,
                                       self.deadline_bound)
        object.__setattr__(self, "deadline_steps", int(d_steps or 0))
        object.__setattr__(self, "deadline_bound",
                           d_bound if d_steps else deadline)
        original, limit = _as_float(self.original), _as_float(self.limit)
        steps = int(self.steps or 0)
        if original is None or limit is None or abs(limit - original) <= _TOLERANCE:
            steps = 0
        if steps <= 0:
            steps = 0
            object.__setattr__(self, "limit", self.original)
        object.__setattr__(self, "steps", steps)

    @property
    def bound(self) -> Any:
        """The operator's bound; the floor of this requirement."""
        return self.limit

    @property
    def floor(self) -> Any:
        return self.limit

    @property
    def relaxable(self) -> bool:
        return self.steps > 0

    @property
    def levels(self) -> Tuple[Any, ...]:
        """``value(q) = original + (bound - original) * q / L``, ``q = 0 .. L``."""
        if not self.relaxable:
            return (self.original,)
        original, bound = float(self.original), float(self.limit)
        return tuple(round(original + (bound - original) * q / self.steps, 9)
                     for q in range(self.steps + 1))

    @property
    def observation_key(self) -> str:
        return kpi_key(self.kpi, self.scope) if self.kpi else ""

    def level_of(self, value: Any) -> Optional[int]:
        """Which ``q`` is this threshold, if it is one of the levels?"""
        for index, level in enumerate(self.levels):
            if _same_value(level, value):
                return index
        return None

    # -- the deadline this ratio is measured at ------------------------------

    @property
    def has_deadline(self) -> bool:
        return self.deadline_ms is not None

    @property
    def deadline_relaxable(self) -> bool:
        return bool(self.deadline_steps) and self.deadline_bound is not None

    @property
    def deadline_levels(self) -> Tuple[Any, ...]:
        """``D(q) = D1 + (bound - D1) * q / L``, ``q = 0 .. L``; ``()`` for a
        requirement that carries no deadline at all."""
        if self.deadline_ms is None:
            return ()
        if not self.deadline_relaxable:
            return (self.deadline_ms,)
        original, bound = float(self.deadline_ms), float(self.deadline_bound)
        return tuple(round(original + (bound - original) * q / self.deadline_steps, 9)
                     for q in range(self.deadline_steps + 1))

    def authorizes_deadline(self, value: Any) -> bool:
        """Is this deadline between ``D1`` and the separately signed bound?"""
        original, candidate = self.deadline_ms, _as_float(value)
        if original is None or candidate is None:
            return False
        bound = _as_float(self.deadline_bound)
        if bound is None or not self.deadline_relaxable:
            return abs(candidate - original) <= _TOLERANCE
        low, high = min(original, bound), max(original, bound)
        return low - _TOLERANCE <= candidate <= high + _TOLERANCE

    def authorizes(self, value: Any) -> bool:
        """Is ``value`` between the original and the signed bound, inclusive?

        A floor may be relaxed *down* to its bound and no further; a ceiling
        may be relaxed *up* to its bound and no further; a non-relaxable
        requirement keeps its original exactly.  The check is on the interval,
        not on the discrete levels: a compact ``T`` the Target agent returns
        may subdivide the same interval differently.
        """
        original, limit = _as_float(self.original), _as_float(self.limit)
        candidate = _as_float(value)
        if original is None or candidate is None:
            return _clean(value) == _clean(self.original)
        if limit is None or not self.relaxable:
            return abs(candidate - original) <= _TOLERANCE
        low, high = min(original, limit), max(original, limit)
        return low - _TOLERANCE <= candidate <= high + _TOLERANCE

    def to_record(self) -> Dict[str, Any]:
        record = {"original": self.original, "limit": self.limit, "op": self.op,
                  "owner": self.owner, "unit": self.unit, "kpi": self.kpi,
                  "scope": self.scope, "steps": self.steps, "bound": self.limit,
                  "levels": list(self.levels), "weight": self.weight}
        if self.has_deadline:
            record.update({"deadlineMs": self.deadline_ms,
                           "deadlineSteps": self.deadline_steps,
                           "deadlineBound": self.deadline_bound,
                           "deadlineLevels": list(self.deadline_levels)})
        return record

    @classmethod
    def from_record(cls, req_id: str, record: Mapping[str, Any]
                    ) -> "AuthorizedRequirement":
        record = dict(record or {})
        bound = record.get("bound", record.get("limit"))
        steps = record.get("steps")
        original = record.get("original")
        if steps is None:
            # a v1 entry: a bare limit meant two equally spaced levels
            steps = (LEGACY_STEPS
                     if not _same_value(bound, original) and bound is not None else 0)
        deadline_steps = record.get("deadlineSteps")
        deadline_bound = record.get("deadlineBound")
        deadline_ms = record.get("deadlineMs")
        if deadline_steps is None:
            deadline_steps = (LEGACY_STEPS
                              if deadline_bound is not None
                              and not _same_value(deadline_bound, deadline_ms) else 0)
        return cls(req_id=str(req_id), op=_clean(record.get("op", _FLOOR)),
                   original=original, limit=bound,
                   owner=_clean(record.get("owner")), unit=_clean(record.get("unit")),
                   kpi=_clean(record.get("kpi")), scope=_clean(record.get("scope")),
                   steps=int(steps or 0), weight=float(record.get("weight", 1.0) or 1.0),
                   deadline_ms=deadline_ms, deadline_steps=int(deadline_steps or 0),
                   deadline_bound=deadline_bound)


@dataclass(frozen=True)
class Authorization:
    """One sitting's signed authorization: every requirement's steps and bound,
    the joint conditions, and how targets are ranked.

    ``to_record()`` is still the v1 per-requirement mapping (that is what the
    episode record's ``T.authorization`` has always been); ``to_full_record()``
    is the contract v2 object the Target agent is given as
    ``input.authorization``.
    """

    requirements: Dict[str, AuthorizedRequirement] = field(default_factory=dict)
    joint_conditions: Tuple[JointCondition, ...] = ()
    preference: "Preference" = None  # type: ignore[assignment]
    #: Cross-axis and cross-owner shaping of the domain: what a joint condition
    #: structurally cannot say, because it is a predicate on a single KPI value.
    mode_constraints: Tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "requirements", dict(self.requirements))
        # Both carvings *remove* combinations from T, so an entry that silently
        # fails to read authorizes a wider domain than was signed -- and because
        # target ids are positional, every id past the missing carving names a
        # different vector, which makes attainment and rank-gap figures wrong
        # while looking entirely plausible.  Refuse instead of dropping.
        #
        # A *free sentence* is the one exception, and only because it was never
        # going to bind: :meth:`JointCondition.from_record` documents that prose
        # stays prose.  It is dropped here rather than carried, because this
        # container cannot hold it -- ``to_full_record`` calls ``to_record`` on
        # every entry and ``condition_for`` reads ``.kpi``, so a string would
        # break the expansion and the live prompt.  Prose still round-trips
        # where it is actually kept: ``TargetContract.to_record`` passes a
        # non-:class:`JointCondition` entry through verbatim.
        conditions = []
        for item in self.joint_conditions or ():
            condition = JointCondition.from_record(item)
            if condition is not None:
                conditions.append(condition)
            elif isinstance(item, Mapping):
                raise ValueError(
                    f"unreadable joint condition {item!r}; dropping it would "
                    "authorize a wider domain than was signed")
        object.__setattr__(self, "joint_conditions", tuple(conditions))
        constraints = []
        for item in self.mode_constraints or ():
            constraint = mode_constraint_from_record(item)
            if constraint is None:
                raise ValueError(
                    f"unreadable mode constraint {item!r}; dropping it would "
                    "authorize a wider domain than was signed")
            constraints.append(constraint)
        object.__setattr__(self, "mode_constraints", tuple(constraints))
        # An axis nobody carries would silently shape the domain around a level
        # that is always 0 -- it would keep T0, drop most of the rest, and look
        # like a smaller authorization rather than like the typo it is.
        known = set()
        for req_id, entry in self.requirements.items():
            known.add(_axis_id(req_id))
            if entry.has_deadline:
                known.add(_axis_id(req_id, True))
        for constraint in self.mode_constraints:
            unknown = [axis for axis in constraint.axes if axis not in known]
            if unknown:
                raise ValueError(
                    f"the constraint {constraint.describe()!r} names "
                    f"{', '.join(unknown)}, which no requirement carries")
        if self.preference is None:
            object.__setattr__(self, "preference", Preference())

    @classmethod
    def from_intents(cls, intents: Sequence[Intent], *,
                     joint_conditions: Sequence[Any] = (),
                     mode_constraints: Sequence[Any] = (),
                     preference: Optional["Preference"] = None) -> "Authorization":
        intents = list(intents)
        max_priority = max((intent.priority for intent in intents), default=1)
        table: Dict[str, AuthorizedRequirement] = {}
        for intent in intents:
            requirement = intent.requirement
            table[requirement.req_id] = AuthorizedRequirement(
                req_id=requirement.req_id, op=requirement.op,
                original=requirement.value,
                limit=(requirement.bound if requirement.relaxable
                       else requirement.value),
                owner=intent.owner, unit=requirement.unit,
                kpi=requirement.kpi, scope=requirement.scope,
                steps=int(requirement.steps or 0),
                weight=intent.weight_or_default(max_priority),
                deadline_ms=requirement.deadline_ms,
                deadline_steps=int(requirement.deadline_steps or 0),
                deadline_bound=requirement.deadline_bound)
        return cls(table, joint_conditions=tuple(joint_conditions),
                   preference=preference or Preference.from_intents(intents),
                   mode_constraints=tuple(mode_constraints))

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Authorization":
        """Reads both shapes: the v1 per-requirement mapping and the v2 object."""
        record = dict(record or {})
        rows = record.get("requirements")
        conditions: Sequence[Any] = ()
        constraints: Sequence[Any] = ()
        preference: Optional[Preference] = None
        if isinstance(rows, Mapping):
            conditions = record.get("jointConditions", ()) or ()
            constraints = record.get("modeConstraints", ()) or ()
            preference = Preference.from_record(record.get("preference", {}) or {})
        else:
            rows = record
        table = {}
        for req_id, entry in dict(rows or {}).items():
            if not isinstance(entry, Mapping):
                continue
            table[str(req_id)] = AuthorizedRequirement.from_record(req_id, entry)
        return cls(table, joint_conditions=tuple(conditions), preference=preference,
                   mode_constraints=tuple(constraints))

    @property
    def req_ids(self) -> Tuple[str, ...]:
        return tuple(self.requirements)

    def owners(self) -> Tuple[str, ...]:
        seen: List[str] = []
        for entry in self.requirements.values():
            if entry.owner and entry.owner not in seen:
                seen.append(entry.owner)
        return tuple(seen)

    def get(self, req_id: str) -> Optional[AuthorizedRequirement]:
        return self.requirements.get(str(req_id))

    def originals(self) -> Dict[str, Any]:
        return {req_id: entry.original for req_id, entry in self.requirements.items()}

    def weights(self) -> Dict[str, float]:
        return {req_id: entry.weight for req_id, entry in self.requirements.items()}

    def condition_for(self, observation_key: str) -> Optional[JointCondition]:
        for condition in self.joint_conditions:
            if condition.kpi == str(observation_key):
                return condition
        return None

    def to_record(self) -> Dict[str, Any]:
        return {req_id: entry.to_record() for req_id, entry in self.requirements.items()}

    def to_full_record(self) -> Dict[str, Any]:
        """``input.authorization`` of ``SINGLE_CALL.md`` (contract v2 section 2.1)."""
        return {"requirements": self.to_record(),
                "jointConditions": [item.to_record() for item in self.joint_conditions],
                "modeConstraints": [item.to_record() for item in self.mode_constraints],
                "preference": self.preference.to_record()}


@dataclass(frozen=True)
class Preference:
    """How targets are compared; priority changes order, it never drops an owner.

    ``rule`` and ``owner_priority`` are the ranking: :func:`preference_key`
    compares the owners' normalized concessions ``D_o`` lexicographically in
    ``owner_priority`` order, and a ``rule`` naming ``D_max`` puts
    ``(D_max, D_mean)`` in front of that tail.  ``P1`` and ``P2`` are the same
    rule over a different owner order; ``P3`` is the ``D_max`` form.

    ``cost_rule`` names the ordering that is actually applied,
    ``normalized-concession``.  :func:`cost_of` still computes the weighted
    square and records it on every target as a diagnostic, but it has not
    ordered anything for two contract versions, and a record that named it was
    read by models as the ranking.
    """

    rule: str = "lexicographic(D_max, D_mean)"
    owner_priority: Tuple[str, ...] = ()
    cost_rule: str = COST_RULE_NORMALIZED_CONCESSION
    tie_break: str = DEFAULT_TIE_BREAK
    #: v5 (owner 2026-09-26): with one owner declaring every requirement, targets are compared
    #: coordinate by coordinate in this requirement order, on k = ceil(20 q) -- no D_max /
    #: D_mean, and equal k vectors are equally preferred.  Empty = the owner ranking above.
    coordinate_order: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule", _clean(self.rule) or "lexicographic(D_max, D_mean)")
        object.__setattr__(self, "owner_priority",
                           tuple(_clean(item) for item in self.owner_priority if _clean(item)))
        object.__setattr__(self, "cost_rule",
                           _clean(self.cost_rule) or COST_RULE_NORMALIZED_CONCESSION)
        object.__setattr__(self, "tie_break", _clean(self.tie_break) or DEFAULT_TIE_BREAK)
        object.__setattr__(self, "coordinate_order",
                           tuple(_clean(item) for item in self.coordinate_order if _clean(item)))

    @classmethod
    def from_intents(cls, intents: Sequence[Intent]) -> "Preference":
        ordered = sorted(intents, key=lambda intent: (intent.priority, intent.intent_id))
        owners: List[str] = []
        for intent in ordered:
            if intent.owner not in owners:
                owners.append(intent.owner)
        # Only a corpus that declares the v5 design turns this on (Codex review 2026-09-26:
        # inferring it from "one owner" changed flag-absent runs).  The record carries the
        # order, so a replay keeps whatever the sitting ran under.
        if (len(owners) == 1 and len(ordered) > 1
                and _os.environ.get("AIC_DESIGN", "").strip().lower() == "v5"):
            # One owner declares every requirement (v5): its own priority order is the ranking.
            order = tuple(intent.requirement.req_id for intent in ordered)
            if _os.environ.get("AIC_V51", "").strip() == "1":
                # v5.1 (owner 2026-09-26): ROC weights from the same priority order, over the
                # relaxable requirements; the protected one only gates.
                from assurance.coordination.concession import roc_weights
                ranked = [intent.requirement.req_id for intent in ordered
                          if ((intent.requirement.steps or 0) > 0 and intent.requirement.bound is not None)
                          or ((getattr(intent.requirement, "deadline_steps", 0) or 0) > 0
                              and getattr(intent.requirement, "deadline_bound", None) is not None)]
                weights = roc_weights(len(ranked))
                terms = " + ".join(f"{w} k[{req}]" for req, w in zip(ranked, weights))
                return cls(owner_priority=tuple(owners), coordinate_order=order,
                           rule=f"v5.1: weighted concession ({terms}) / {20 * sum(weights)}, "
                                f"k = ceil(20 q); smaller is better",
                           tie_break="none: equal weighted sums are equally preferred")
            return cls(owner_priority=tuple(owners), coordinate_order=order,
                       rule=f"v5: coordinate-lexicographic({', '.join(order)}) on k = ceil(20 q)",
                       tie_break="none: equal k vectors are equally preferred")
        return cls(owner_priority=tuple(owners))

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Preference":
        record = dict(record or {})
        return cls(rule=record.get("rule", ""),
                   owner_priority=tuple(record.get("ownerPriority", ()) or ()),
                   cost_rule=record.get("costRule", ""),
                   tie_break=record.get("tieBreak", ""),
                   coordinate_order=tuple(record.get("coordinateOrder", ()) or ()))

    def to_record(self) -> Dict[str, Any]:
        out = {"rule": self.rule, "ownerPriority": list(self.owner_priority),
               "costRule": self.cost_rule, "tieBreak": self.tie_break}
        if self.coordinate_order:
            out["coordinateOrder"] = list(self.coordinate_order)
        return out


# --------------------------------------------------------------------------- #
# section 2.2 -- T
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Target:
    """One row of the board: a threshold for every requirement.

    ``levels`` is the relaxation level ``q`` this row uses per requirement and
    ``cost`` is ``sum_i w_i q_i^2`` -- both empty on a target read from a v1
    record, both derivable with :func:`cost_of`.

    ``deadlines`` is the second authorized level of a
    :data:`KPI_DEADLINE_RATIO` requirement: the deadline ``D`` this row's ratio
    is to be **measured at**, in milliseconds, with ``deadline_levels`` its own
    ``q``.  The observation key does not change with it -- the executor tells
    the observer which deadline is in force before the hold, and the ratio it
    reports back is the ratio at that deadline.  Every other requirement leaves
    both mappings empty and nothing about it changes.
    """

    target_id: str
    requirements: Dict[str, Any] = field(default_factory=dict)
    concession: Dict[str, float] = field(default_factory=dict)
    levels: Dict[str, int] = field(default_factory=dict)
    cost: float = 0.0
    deadlines: Dict[str, Any] = field(default_factory=dict)
    deadline_levels: Dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", _clean(self.target_id))
        object.__setattr__(self, "requirements",
                           {str(k): v for k, v in dict(self.requirements).items()})
        object.__setattr__(self, "concession",
                           {str(k): float(v) for k, v in dict(self.concession or {}).items()})
        object.__setattr__(self, "levels",
                           {str(k): int(v) for k, v in dict(self.levels or {}).items()})
        object.__setattr__(self, "cost", float(self.cost or 0.0))
        object.__setattr__(self, "deadlines",
                           {str(k): v for k, v in dict(self.deadlines or {}).items()})
        object.__setattr__(self, "deadline_levels",
                           {str(k): int(v)
                            for k, v in dict(self.deadline_levels or {}).items()})
        if not self.target_id:
            raise ValueError("a target needs a targetId")

    def value_for(self, req_id: str) -> Any:
        return self.requirements.get(str(req_id))

    def level_for(self, req_id: str) -> Optional[int]:
        return self.levels.get(str(req_id))

    def deadline_for(self, req_id: str) -> Optional[float]:
        """The deadline this row's ratio is measured at, or ``None``.

        The seam the executor reads before a hold: the observer is told this
        number, and the ``deadlineSuccessRatio@<ue>`` it answers with is the
        fraction completed within *it*.
        """
        value = self.deadlines.get(str(req_id))
        return None if value is None else _as_float(value)

    def deadline_level_for(self, req_id: str) -> Optional[int]:
        return self.deadline_levels.get(str(req_id))

    def to_record(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {"targetId": self.target_id,
                                  "requirements": dict(self.requirements)}
        if self.concession:
            record["concession"] = dict(self.concession)
        if self.levels:
            record["levels"] = dict(self.levels)
        record["cost"] = self.cost
        if self.deadlines:
            record["deadlines"] = dict(self.deadlines)
            record["deadlineLevels"] = dict(self.deadline_levels)
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Target":
        record = dict(record or {})
        return cls(target_id=record.get("targetId", record.get("target_id", "")),
                   requirements=record.get("requirements", {}),
                   concession=record.get("concession", {}),
                   levels=record.get("levels", {}) or {},
                   cost=float(record.get("cost", 0.0) or 0.0),
                   deadlines=record.get("deadlines", {}) or {},
                   deadline_levels=record.get("deadlineLevels",
                                              record.get("deadline_levels", {})) or {})


def _fold_deadline_axis_rows(rows: Mapping[str, Any], req_ids: Sequence[str],
                             notes: List[str]) -> Dict[str, Any]:
    """Read a ``reqId#deadline`` levels row as that requirement's deadline half.

    ``#deadline`` is this module's own axis spelling for a deadline concession
    (``DEADLINE_CONCESSION_SUFFIX``), and a model that writes
    ``{"I1d.r1#deadline": {"steps": 0, "bound": 2000}}`` means exactly
    ``deadlineSteps``/``deadlineBound`` on ``I1d.r1``.  Refusing it cost a
    repair call and ended 2026-09-15 attempt 28 on the 240 s formation
    deadline.  Only a row whose requirement exists and whose own row carries no
    deadline half is folded; the values are still clipped by
    ``_adjust_deadline``, and a conflict stays an unknown-requirement refusal.
    """
    folded = dict(rows)
    for key in [key for key in rows if str(key).endswith(DEADLINE_CONCESSION_SUFFIX)]:
        req_id = str(key)[: -len(DEADLINE_CONCESSION_SUFFIX)]
        row = rows[key]
        base = folded.get(req_id)
        if (req_id not in req_ids or not isinstance(row, Mapping)
                or (isinstance(base, Mapping)
                    and ("deadlineSteps" in base or "deadlineBound" in base))):
            continue
        merged = dict(base) if isinstance(base, Mapping) else {}
        if "steps" in row or "L" in row:
            merged["deadlineSteps"] = row.get("steps", row.get("L"))
        if "bound" in row or "value" in row:
            merged["deadlineBound"] = row.get("bound", row.get("value"))
        folded[req_id] = merged
        del folded[key]
        notes.append(f"{req_id}: deadline levels read from the {key} row")
    return folded


def _adjust_deadline(entry: "AuthorizedRequirement", row: Mapping[str, Any],
                     notes: List[str]) -> "AuthorizedRequirement":
    """The deadline half of a compact ``T`` row, clipped onto what was signed.

    A model may subdivide the authorized extension differently; it may not
    invent a deadline for a requirement that has none, and it may not stretch
    one past the separately signed bound.  Every refusal is a note, never a
    silent correction.
    """
    steps = row.get("deadlineSteps")
    bound = row.get("deadlineBound")
    if steps is None and bound is None:
        return entry
    if not entry.has_deadline:
        notes.append(f"{entry.req_id}: carries no deadline; the proposed "
                     "deadline levels were refused")
        return entry
    steps = int(steps) if _as_float(steps) is not None else entry.deadline_steps
    if not entry.deadline_relaxable:
        if steps:
            notes.append(f"{entry.req_id}: the deadline extension is not "
                         f"authorized; the proposed {steps} steps were refused")
        return entry
    if bound is None or not entry.authorizes_deadline(bound):
        notes.append(f"{entry.req_id}: deadline bound {bound} is outside "
                     f"[{entry.deadline_ms}, {entry.deadline_bound}] ms; the "
                     "signed bound stands")
        bound = entry.deadline_bound
    if steps <= 0:
        notes.append(f"{entry.req_id}: {steps} deadline steps refused; the "
                     "signed steps stand")
        steps = entry.deadline_steps
    return replace(entry, deadline_bound=bound, deadline_steps=int(steps))


@dataclass(frozen=True)
class TargetContract:
    """``T``: ``T0``, the authorized alternatives, and how to compare them."""

    t0: Target
    alternatives: Tuple[Target, ...] = ()
    authorization: Authorization = field(default_factory=Authorization)
    preference: Preference = field(default_factory=Preference)
    joint_conditions: Tuple[Any, ...] = ()
    provenance: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = TARGET_CONTRACT_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "alternatives", tuple(self.alternatives))
        if not self.joint_conditions and self.authorization is not None:
            object.__setattr__(self, "joint_conditions",
                               tuple(self.authorization.joint_conditions))
        object.__setattr__(self, "joint_conditions", tuple(self.joint_conditions))
        object.__setattr__(self, "provenance", dict(self.provenance or {}))

    @property
    def targets(self) -> Tuple[Target, ...]:
        return (self.t0,) + self.alternatives

    @property
    def target_ids(self) -> Tuple[str, ...]:
        return tuple(target.target_id for target in self.targets)

    def target(self, target_id: str) -> Optional[Target]:
        for candidate in self.targets:
            if candidate.target_id == str(target_id):
                return candidate
        return None

    def ranked(self) -> Tuple[Target, ...]:
        """Targets by the owner's preference -- lowest normalized concession
        first -- then the order they were expanded in."""
        order = {target.target_id: index for index, target in enumerate(self.targets)}

        def key(target: Target) -> Tuple[float, ...]:
            return preference_key(target, self.authorization,
                                  self.preference) + (order[target.target_id],)

        return tuple(sorted(self.targets, key=key))

    def cheapest_unsatisfied(self, kpis: Mapping[str, Any], *,
                             include_t0: bool = True) -> Optional[Target]:
        """The lowest-cost target the measured vector does **not** already meet.

        With ``include_t0=False`` this is the *next preferred* target of
        ``SINGLE_CALL.md``: the cheapest authorized alternative still to be
        earned, which is what the KPI gaps are measured against beside ``T0``.
        """
        for target in self.ranked():
            if not include_t0 and target.target_id == self.t0.target_id:
                continue
            verdicts = judge_target(kpis, target, self.authorization)
            if verdict_of(verdicts) != PASS:
                return target
        return None

    def compact_record(self) -> Dict[str, Any]:
        """``T`` as the Target agent represents it: ``t0``, per-requirement
        ``levels``, restated constraints and the ranking rule."""
        levels = {}
        for req_id, entry in self.authorization.requirements.items():
            levels[req_id] = {"steps": entry.steps, "bound": entry.limit}
            if entry.has_deadline:
                levels[req_id].update({"deadlineMs": entry.deadline_ms,
                                       "deadlineSteps": entry.deadline_steps,
                                       "deadlineBound": entry.deadline_bound})
        constraints = [f"{req_id} never below {entry.limit} {entry.unit}".strip()
                       for req_id, entry in self.authorization.requirements.items()]
        constraints += [
            f"{req_id} is measured within {entry.deadline_ms} ms and the deadline "
            f"never past {entry.deadline_bound} ms"
            for req_id, entry in self.authorization.requirements.items()
            if entry.has_deadline]
        constraints += [f"{item.kpi} {item.op} {item.value} {item.unit}".strip()
                        for item in self.authorization.joint_conditions]
        constraints += [item.describe()
                        for item in self.authorization.mode_constraints]
        return {"t0": self.t0.to_record(), "levels": levels,
                "constraints": constraints,
                "ranking": {"costRule": self.preference.cost_rule,
                            "tieBreak": self.preference.tie_break}}

    def to_record(self) -> Dict[str, Any]:
        return {"schemaVersion": self.schema_version,
                "t0": self.t0.to_record(),
                "alternatives": [target.to_record() for target in self.alternatives],
                "authorization": self.authorization.to_record(),
                "weights": self.authorization.weights(),
                "preference": self.preference.to_record(),
                "jointConditions": [item.to_record() if isinstance(item, JointCondition)
                                    else item for item in self.joint_conditions],
                "modeConstraints": [item.to_record() for item
                                    in self.authorization.mode_constraints],
                "compact": self.compact_record(),
                "provenance": dict(self.provenance)}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "TargetContract":
        record = dict(record or {})
        authorization = Authorization.from_record(record.get("authorization", {}))
        conditions = tuple(record.get("jointConditions", ()) or ())
        constraints = tuple(record.get("modeConstraints", ()) or ())
        preference = Preference.from_record(record.get("preference", {}))
        if conditions or constraints or preference:
            authorization = replace(
                authorization,
                joint_conditions=tuple(conditions) or authorization.joint_conditions,
                mode_constraints=tuple(constraints) or authorization.mode_constraints,
                preference=preference)
        return cls(t0=Target.from_record(record.get("t0", {})),
                   alternatives=tuple(Target.from_record(item)
                                      for item in record.get("alternatives", ()) or ()),
                   authorization=authorization,
                   preference=preference,
                   joint_conditions=tuple(authorization.joint_conditions) or conditions,
                   provenance=dict(record.get("provenance", {}) or {}),
                   schema_version=_clean(record.get("schemaVersion")) or TARGET_CONTRACT_SCHEMA)

    @classmethod
    def from_compact(cls, t0: Mapping[str, Any], levels: Mapping[str, Any],
                     constraints: Sequence[Any], ranking: Mapping[str, Any],
                     authorization: Authorization,
                     selection: Sequence[Any] = ()) -> "TargetContract":
        """Expand the compact ``T`` the Target agent returns.

        ``t0`` must be the originals exactly, ``levels`` is
        ``{reqId: {"steps": L, "bound": value}}`` and must stay inside the
        operator's own ``[bound, original]`` interval (a non-relaxable
        requirement stays at ``steps = 0``), ``constraints`` is the model's
        restatement (kept as provenance -- the binding conditions are the
        operator's) and ``ranking`` names the cost rule and the tie-break.

        ``selection`` is the Target agent's own choice of which alternatives to
        carry: a sequence of ``{"levels": {reqId: q, ...}}`` rows naming level
        indices only.  Every threshold, cost and concession is still computed
        from the owner's authorization, so the agent chooses directions and
        never a value.  A row the authorization does not admit is dropped and
        noted; an explicit empty ``selection`` means the agent kept only
        ``T0``, and a selection with no admissible row is refused so the agent
        can revise it.
        """
        req_ids = list(authorization.req_ids)
        if not req_ids:
            raise TargetValidationError("the authorization names no requirement")
        t0 = dict(t0 or {})
        stated = dict(t0.get("requirements") or {})
        problems: List[str] = []
        unknown = [req_id for req_id in stated if req_id not in req_ids]
        if unknown:
            problems.append(f"T0 names unknown requirement {', '.join(sorted(unknown))}")
        for req_id in req_ids:
            entry = authorization.requirements[req_id]
            if req_id not in stated:
                problems.append(f"T0 does not name {req_id}")
            elif not _same_value(stated[req_id], entry.original):
                problems.append(f"T0 moved {req_id} off its original {entry.original}")
        if problems:
            raise TargetValidationError("; ".join(problems))

        rows = dict(levels or {})
        notes: List[str] = []
        rows = _fold_deadline_axis_rows(rows, req_ids, notes)
        unknown = [req_id for req_id in rows if req_id not in req_ids]
        if unknown:
            raise TargetValidationError(
                f"levels name unknown requirement {', '.join(sorted(unknown))}")

        adjusted: Dict[str, AuthorizedRequirement] = {}
        for req_id in req_ids:
            entry = authorization.requirements[req_id]
            row = rows.get(req_id)
            if not isinstance(row, Mapping):
                if entry.relaxable:
                    notes.append(f"{req_id}: no levels given; the signed steps stand")
                adjusted[req_id] = entry
                continue
            steps = row.get("steps", row.get("L"))
            bound = row.get("bound", row.get("value"))
            steps = int(steps) if _as_float(steps) is not None else entry.steps
            entry = _adjust_deadline(entry, row, notes)
            if not entry.relaxable:
                if steps:
                    notes.append(f"{req_id}: non-relaxable; the proposed "
                                 f"{steps} steps were refused")
                adjusted[req_id] = entry
                continue
            if bound is None or not entry.authorizes(bound):
                notes.append(f"{req_id}: bound {bound} is outside "
                             f"[{entry.limit}, {entry.original}]; the signed bound stands")
                bound = entry.limit
            if steps <= 0:
                notes.append(f"{req_id}: {steps} steps refused; the signed steps stand")
                steps = entry.steps
            adjusted[req_id] = replace(entry, limit=bound, steps=int(steps))

        ranking = dict(ranking or {})
        preference = replace(
            authorization.preference,
            cost_rule=_clean(ranking.get("costRule")) or authorization.preference.cost_rule,
            tie_break=_clean(ranking.get("tieBreak")) or authorization.preference.tie_break)
        # A domain too large to expand (v4.7 without a ladder: 21 levels per coordinate)
        # holds only T0, the all-at-limit target and the rows the agent selected.
        chosen_combinations = []
        for row in selection or ():
            if not isinstance(row, Mapping) or not isinstance(row.get("levels"), Mapping):
                continue
            try:
                chosen_combinations.append(tuple(
                    (int(dict(row["levels"]).get(r, 0)),
                     int(dict(row.get("deadlineLevels") or {}).get(r, 0))) for r in req_ids))
            except (TypeError, ValueError):
                continue  # refused with its reason by _selected_alternatives
        expanded = omega_or_sparse(replace(authorization, requirements=adjusted,
                                           preference=preference), chosen_combinations)
        provenance: Dict[str, Any] = {
            "model": None, "fallback": None, "rationale": "",
            "constraints": [str(item) for item in (constraints or ())],
            "notes": notes}
        expanded = _selected_alternatives(expanded, selection, req_ids, notes,
                                          provenance)
        return replace(expanded, provenance=provenance)


def _level_at(value: Any, original: Any, bound: Any, steps: int) -> Optional[float]:
    """Where ``value`` sits on ``[original .. bound]``, in step units.

    A compact ``T`` may subdivide the same interval differently, so the
    fractional position is used rather than an invented level; that keeps the
    ordering monotone.  The threshold and the deadline are read the same way.
    """
    start, end, candidate = _as_float(original), _as_float(bound), _as_float(value)
    if start is None or end is None or candidate is None or int(steps) <= 0:
        return None
    span = end - start
    fraction = 0.0 if abs(span) <= _TOLERANCE else (candidate - start) / span
    return max(0.0, fraction) * int(steps)


def cost_of(target: Target, authorization: Authorization) -> float:
    """``sum_i w_i q_i^2`` -- the owner's ranking cost of one target.

    ``q_i`` is the target's own level when it has one, otherwise the level its
    threshold sits at (a compact ``T`` may subdivide the interval differently,
    in which case the fractional position is used, which keeps the ordering
    monotone without inventing a level).

    A deadline extension is charged the **same way and at the same weight**: it
    is a second authorized level of the same intent, so a target that both
    lowers the ratio and stretches the deadline pays for both, which is what
    stops the search from treating a deadline as free.
    """
    total = 0.0
    for req_id, entry in authorization.requirements.items():
        level = target.levels.get(req_id)
        if level is None:
            if req_id not in target.requirements or not entry.relaxable:
                level = None
            else:
                level = _level_at(target.requirements[req_id], entry.original,
                                  entry.limit, entry.steps)
        if level is not None:
            total += float(entry.weight) * float(level) ** 2
        deadline_level = target.deadline_levels.get(req_id)
        if deadline_level is None and entry.deadline_relaxable:
            deadline_level = _level_at(target.deadlines.get(req_id), entry.deadline_ms,
                                       entry.deadline_bound, entry.deadline_steps)
        if deadline_level is not None:
            total += float(entry.weight) * float(deadline_level) ** 2
    return round(total, 9)


def expand_targets(authorization: Authorization) -> TargetContract:
    """Every combination of authorized levels, ranked by ``sum_i w_i q_i^2``.

    The product of the per-requirement levels (``prod (L_i + 1)`` targets)
    minus the combinations a joint condition or a floor refuses; ``T0`` first,
    then increasing cost, ties broken lexicographically by ``(D_max, D_mean)``
    and then by the requirements' own order.  This is both the deterministic
    ``T`` and the fallback when a Target answer is refused.
    """
    req_ids = list(authorization.req_ids)
    if not req_ids:
        raise TargetValidationError("the authorization names no requirement")

    # ``T`` is deliberately not capped -- the owner's instruction is explicit
    # about that -- but a product this large is an operator mistake (a stray
    # thousand-step authorization), not a target contract, and it would take
    # the sitting down with it.  Say so instead of hanging.
    #: How many levels each requirement contributes: the threshold's, times
    #: the deadline's when the owner separately authorized an extension.
    def _counts(entry: AuthorizedRequirement) -> Tuple[int, int]:
        return (max(1, len(entry.levels)), max(1, len(entry.deadline_levels)))

    product = 1
    for req_id in req_ids:
        thresholds, deadlines = _counts(authorization.requirements[req_id])
        product *= thresholds * deadlines
    if product > MAX_EXPANSION:
        raise TargetValidationError(
            f"the authorized levels expand to {product} targets, past the "
            f"{MAX_EXPANSION} this executor can hold; reduce the steps")

    combinations: List[Tuple[Tuple[int, int], ...]] = [()]
    for req_id in req_ids:
        thresholds, deadlines = _counts(authorization.requirements[req_id])
        combinations = [prefix + ((q, d),) for prefix in combinations
                        for q in range(thresholds) for d in range(deadlines)]

    rows: List[Tuple[Tuple[float, float, float, Tuple[Tuple[int, int], ...]],
                     Target]] = []
    zero = tuple((0, 0) for _ in req_ids)
    t0: Optional[Target] = None
    for combination in combinations:
        target = _target_for(authorization, req_ids, combination)
        if target is None:
            continue
        if combination == zero:
            t0 = replace(target, target_id="T0")
            continue
        rows.append((preference_key(target, authorization) + (combination,), target))

    if t0 is None:
        raise TargetValidationError(
            "T0 itself is refused by a joint condition or a floor")
    rows.sort(key=lambda item: item[0])
    alternatives = tuple(replace(target, target_id=f"T{index + 1}")
                         for index, (_, target) in enumerate(rows))
    return TargetContract(
        t0=t0, alternatives=alternatives, authorization=authorization,
        preference=authorization.preference,
        joint_conditions=tuple(authorization.joint_conditions),
        provenance={"model": "deterministic", "fallback": None,
                    "rationale": "every authorized combination of relaxation "
                                 "levels, ranked by the owners' normalized "
                                 "concession, lowest first"})



def _target_for(authorization: Authorization, req_ids: Sequence[str],
                combination: Sequence[Tuple[int, int]]) -> Optional["Target"]:
    """The target of one combination of (threshold level, deadline level) per
    requirement, or None when a floor, a condition or a mode set refuses it.
    Factored out of :func:`expand_targets` (2026-09-25) so a target can be built
    without expanding every combination."""
    requirements: Dict[str, Any] = {}
    levels: Dict[str, int] = {}
    deadlines: Dict[str, Any] = {}
    deadline_levels: Dict[str, int] = {}
    for req_id, (q, d) in zip(req_ids, combination):
        entry = authorization.requirements[req_id]
        if not 0 <= int(q) < max(1, len(entry.levels)):
            return None
        value = entry.levels[q]
        condition = authorization.condition_for(entry.observation_key)
        if condition is not None and not condition.allows_threshold(value):
            return None
        if entry.relaxable and not entry.authorizes(value):
            return None  # below the floor; unreachable by construction
        requirements[req_id] = value
        levels[req_id] = q
        authorized_deadlines = entry.deadline_levels
        if authorized_deadlines:
            if not 0 <= int(d) < len(authorized_deadlines):
                return None
            deadlines[req_id] = authorized_deadlines[d]
            deadline_levels[req_id] = d
        elif int(d) != 0:
            return None
    # The cross-axis and cross-owner shaping, checked on the *whole* combination:
    # neither an owner's permitted (g, d) mode set nor "at most one owner extends"
    # can be decided one requirement at a time.
    if authorization.mode_constraints:
        axis_levels: Dict[str, int] = {_axis_id(req): q for req, q in levels.items()}
        axis_levels.update({_axis_id(req, True): d for req, d in deadline_levels.items()})
        if any(not constraint.permits(axis_levels)
               for constraint in authorization.mode_constraints):
            return None
    target = Target(target_id="T?", requirements=requirements, levels=levels,
                    deadlines=deadlines, deadline_levels=deadline_levels)
    cost = cost_of(target, authorization)
    quality = concession_of(target, authorization)
    return replace(target, cost=cost, concession=dict(quality["perRequirement"]))


def sparse_targets(authorization: Authorization,
                   combinations: Sequence[Sequence[Tuple[int, int]]] = ()
                   ) -> TargetContract:
    """``T0``, the all-at-limit boundary target, and the given combinations --
    without expanding the whole grid (v4.7, owner 2026-09-25: no ladder; the Target
    agent cuts the interval itself at 1/20 resolution, which is 21**n targets).

    Only valid when the authorization carries no mode constraint and no joint
    condition: then the single maximum of Omega is every coordinate at its limit,
    so :func:`boundary_targets` over this set is exactly the boundary over Omega.
    Otherwise the caller must expand."""
    if authorization.mode_constraints or authorization.joint_conditions:
        raise TargetValidationError("a sparse target set cannot carry joint conditions")
    req_ids = list(authorization.req_ids)
    zero = tuple((0, 0) for _ in req_ids)
    top = tuple((max(1, len(authorization.requirements[r].levels)) - 1,
                 max(1, len(authorization.requirements[r].deadline_levels)) - 1)
                for r in req_ids)
    t0 = _target_for(authorization, req_ids, zero)
    if t0 is None:
        raise TargetValidationError("T0 itself is refused by a floor")
    rows, seen = [], {zero}
    for combination in [top] + [tuple((int(q), int(d)) for q, d in c) for c in combinations]:
        if combination in seen:
            continue
        seen.add(combination)
        target = _target_for(authorization, req_ids, combination)
        if target is not None:
            rows.append((preference_key(target, authorization) + (combination,), target))
    rows.sort(key=lambda item: item[0])
    return TargetContract(
        t0=replace(t0, target_id="T0"),
        alternatives=tuple(replace(t, target_id=f"T{i + 1}") for i, (_, t) in enumerate(rows)),
        authorization=authorization, preference=authorization.preference,
        joint_conditions=tuple(authorization.joint_conditions),
        provenance={"model": "deterministic", "fallback": None,
                    "rationale": "T0, the all-at-limit boundary and the named combinations "
                                 "(no full grid: v4.7 targets without a ladder)"})


def omega_or_sparse(authorization: Authorization,
                    combinations: Sequence[Sequence[Tuple[int, int]]] = ()) -> TargetContract:
    """The full expansion when it fits, else :func:`sparse_targets`."""
    try:
        return expand_targets(authorization)
    except TargetValidationError:
        if authorization.mode_constraints or authorization.joint_conditions:
            raise
        return sparse_targets(authorization, combinations)


def _level_vector(target: "Target", authorization: "Authorization") -> Dict[str, int]:
    """Every axis's level, threshold and deadline alike -- the ``[g..,d..]`` vector."""
    return _axis_levels(target, authorization)


def _implies(sigma: Mapping[str, int], tau: Mapping[str, int]) -> bool:
    """``sigma`` is at least as relaxed as ``tau`` on every axis, so meeting
    ``sigma`` meets ``tau``.  An axis a vector does not name sits at level 0."""
    axes = set(sigma) | set(tau)
    return all(int(sigma.get(axis, 0)) >= int(tau.get(axis, 0)) for axis in axes)


def boundary_targets(contract: "TargetContract") -> Tuple["Target", ...]:
    """The maximally weakened authorized targets: none is implied by another.

    Derived from the immutable authority rather than listed, so a changed owner
    table cannot leave a stale anchor behind.  For the v3.1 authority this is
    exactly the three branch ends the amendment names --
    ``[2,2,2,0,0,0]``, ``[2,0,2,0,1,0]``, ``[2,2,1,0,0,1]`` -- and a test pins it.
    ``T0`` is returned only when the domain has no relaxation at all.
    """
    authorization = contract.authorization
    if authorization.preference is not None and authorization.preference.coordinate_order:
        # v5 (owner 2026-09-26): only T0 is mandatory; the all-at-limit target stays in Omega
        # and the Target agent decides whether T carries it.
        return (contract.t0,)
    omega = (contract.t0,) + tuple(contract.alternatives)
    vectors = [(_level_vector(target, authorization), target) for target in omega]
    # Maximal elements without the all-pairs scan (2026-09-25, v4.7: 46,656 targets made the
    # pairwise loop ~2e9 comparisons and stalled formation).  Visit vectors by decreasing
    # level sum: anything that dominates a vector has a larger sum and was visited first,
    # and by transitivity it is itself dominated by a maximal vector already kept -- so
    # checking against the kept maxima is exact.  Identical vectors never dominate each
    # other, as before.  Order of the result follows the original domain order.
    order = sorted(range(len(vectors)),
                   key=lambda i: -sum(int(v) for v in vectors[i][0].values()))
    kept: List[int] = []
    for i in order:
        vector = vectors[i][0]
        if any(vectors[k][0] != vector and _implies(vectors[k][0], vector) for k in kept):
            continue
        kept.append(i)
    boundary = [vectors[i][1] for i in sorted(kept)]
    return tuple(target for target in boundary if target is not contract.t0) or (contract.t0,)


def expression_error(selected: Sequence["Target"], omega: Sequence["Target"],
                     authorization: "Authorization") -> Optional[float]:
    """The reviewed worst-case target expression error ``epsilon_T``.

    For each ``tau`` in ``omega`` take the selected ``sigma`` that implies it
    (componentwise at least as relaxed) at the smallest *replacement cost*, the
    largest positive increase in any owner's normalized concession; ``epsilon``
    is the largest of those minima.  ``None`` is infinity: some authorized target
    has no selected target that implies it.  At most ``len(omega) * len(selected)``
    comparisons, no model, no radio, no optimizer.
    """
    def owner_concession(target: "Target") -> Dict[str, float]:
        return dict(concession_of(target, authorization)["perOwner"])

    chosen = [(_level_vector(target, authorization), owner_concession(target))
              for target in selected]
    worst = 0.0
    for tau in omega:
        tau_vector, tau_owner = _level_vector(tau, authorization), owner_concession(tau)
        best: Optional[float] = None
        for sigma_vector, sigma_owner in chosen:
            if not _implies(sigma_vector, tau_vector):
                continue
            owners = set(sigma_owner) | set(tau_owner)
            cost = max([max(0.0, sigma_owner.get(owner, 0.0) - tau_owner.get(owner, 0.0))
                        for owner in owners] or [0.0])
            best = cost if best is None else min(best, cost)
        if best is None:
            return None
        worst = max(worst, best)
    return round(worst, 9)


def target_diagnostics(chosen: Sequence["Target"], omega_contract: "TargetContract"
                       ) -> Dict[str, Any]:
    """What the amendment asks every formation to record about its ``T``."""
    authorization = omega_contract.authorization
    omega = (omega_contract.t0,) + tuple(omega_contract.alternatives)
    mandatory = (omega_contract.t0,) + boundary_targets(omega_contract)
    held = {tuple(sorted(_level_vector(t, authorization).items())) for t in chosen}
    missing = [dict(_level_vector(t, authorization)) for t in mandatory
               if tuple(sorted(_level_vector(t, authorization).items())) not in held]
    epsilon = expression_error(chosen, omega, authorization)
    return {"epsilon": epsilon, "epsilonInfinite": epsilon is None,
            "missingMandatory": missing, "omegaSize": len(omega),
            "targetCount": len(chosen), "mandatoryCount": len(mandatory),
            # An offline structural diagnostic only: not a prediction that any
            # target is attainable, and never a reason to re-form T.
            "definition": "max over Omega of the cheapest implying selected target's "
                          "largest owner-concession increase"}


def mandatory_contract(contract: "TargetContract") -> "TargetContract":
    """``T`` holding only the mandatory targets, in the owner's order.

    The deterministic formation and every failed model formation land here.  It
    replaces the old fallback of the whole expansion: the amendment forbids a
    full-Omega ``T``, and a fallback must not be read as a model's choice.
    """
    boundary = boundary_targets(contract)
    order = {id(target): index for index, target in enumerate(contract.alternatives)}
    kept = sorted((t for t in boundary if t is not contract.t0), key=lambda t: order[id(t)])
    renumbered = tuple(replace(target, target_id=f"T{index + 1}")
                       for index, target in enumerate(kept))
    provenance = dict(contract.provenance or {})
    provenance["targetMembership"] = {
        "mandatory": [dict(_level_vector(t, contract.authorization))
                      for t in (contract.t0,) + tuple(kept)],
        "modelAdditions": []}
    provenance["expressionError"] = target_diagnostics((contract.t0,) + renumbered, contract)
    return replace(contract, alternatives=renumbered, provenance=provenance)


def model_addition_limit(authorization: "Authorization", boundary_count: int = 0) -> int:
    """How many distinct additions the model may select -- the one number the validator
    enforces and the Target / monolith-formation prompts state.  v5: T0 is the only mandatory
    target, so MAX_TARGETS - 1; otherwise the v4 rule beside the boundary targets."""
    preference = getattr(authorization, "preference", None)
    if preference is not None and preference.coordinate_order:
        return MAX_TARGETS - 1
    return min(MAX_MODEL_ADDITIONS, MAX_TARGETS - 1 - boundary_count)


def _selected_alternatives(contract: "TargetContract", selection: Sequence[Any],
                           req_ids: Sequence[str], notes: List[str],
                           provenance: Optional[Dict[str, Any]] = None
                           ) -> "TargetContract":
    """``T`` = the mandatory targets plus the model's additions, in owner order.

    ``contract`` is the full authorized expansion (``Omega``); it is never
    rewritten, only looked up.  The mandatory part -- ``T0`` and the maximally
    weakened boundary targets -- is added by code on every path, so a model that
    returns an **explicit empty** list gets exactly the mandatory set, and none of
    it is credited to the model.

    Amendment section 3.3, row by row:

    * an exact duplicate, or a restatement of a mandatory target, is removed and
      logged in ``modelSelection.removed`` -- before the limit is counted, so it
      never spends an addition;
    * a row with no levels, a non-integer level, or a vector the owner did not
      authorize is **refused** (``TargetValidationError``), which sends the answer
      down the existing counted revision path;
    * more than :data:`MAX_MODEL_ADDITIONS` genuine distinct additions is refused
      the same way.  Nothing is truncated: which additions are useful is the
      judgement being asked for, and cutting the surplus would substitute ours.

    ``modelSelection.order`` keeps the model's own sequence and advisory
    ``selectionRole``/``reason``; ``targetMembership`` stores mandatory and
    model-authored membership separately; ``expressionError`` is the exact
    epsilon over ``Omega`` for the ``T`` that resulted.
    """
    rows = [row for row in (selection or ()) if isinstance(row, Mapping)]
    authorization = contract.authorization
    deadline_reqs = sorted({req for target in contract.alternatives
                            for req in target.deadline_levels})

    # 2026-09-30 (v54t, local qwen3): a requirement every authorized target holds at level 0 (the
    # protected one, I2g.r1) has only that level; a row that leaves it out means 0.  Without this,
    # every qwen3 Target row (it wrote the four relaxable requirements only) missed the lookup and
    # was refused as "outside the owner-authorized targets" -- 10 of 13 formations failed.
    fixed_zero = {req for req in req_ids
                  if all(int(t.levels.get(req, 0) or 0) == 0
                         for t in (contract.t0, *contract.alternatives))}

    def key_of(levels: Mapping[str, Any], deadlines: Mapping[str, Any]
               ) -> Tuple[Tuple[Tuple[str, int], ...], Tuple[Tuple[str, int], ...]]:
        levels = {**{req: 0 for req in fixed_zero}, **dict(levels)}
        return (tuple(sorted((str(req), int(q)) for req, q in levels.items()
                             if str(req) in req_ids)),
                tuple(sorted((req, int(dict(deadlines or {}).get(req, 0)))
                             for req in deadline_reqs)))

    by_levels = {key_of(target.levels, target.deadline_levels): target
                 for target in contract.alternatives}
    boundary = [t for t in boundary_targets(contract) if t is not contract.t0]
    mandatory_ids = {id(contract.t0)} | {id(t) for t in boundary}
    t0_key = key_of(contract.t0.levels, contract.t0.deadline_levels)

    additions: List["Target"] = []
    said: Dict[int, Tuple[Optional[str], Optional[str], Optional[List[str]]]] = {}
    refused: List[str] = []
    removed: Dict[str, List[Any]] = {"duplicates": [], "restatedMandatory": []}
    for row in rows:
        wanted = row.get("levels")
        if not isinstance(wanted, Mapping):
            refused.append("an addition names no levels")
            continue
        try:
            key = key_of(wanted, row.get("deadlineLevels") or {})
        except (TypeError, ValueError):
            refused.append(f"an addition has a level that is not a whole number: "
                           f"{dict(wanted)!r}")
            continue
        target = contract.t0 if key == t0_key else by_levels.get(key)
        if target is None:
            # Say which declaration the row exceeds.  "the authorization does not
            # admit" alone read as the owner's limit, so on 2026-09-19 (board
            # 20260919T151448) the model rewrote its alternatives three times
            # while the real cause -- its own ``levels`` declaring 0 steps where
            # the owner allowed 2 -- stayed put, and T took 222 s.
            declared = {req: max([int(t.levels.get(req, 0)) for t in
                                  (contract.t0, *contract.alternatives)] or [0])
                        for req in req_ids}
            over = [f"{req} level {int(q)} but the owner authorizes {declared.get(str(req), 0)} "
                    f"step(s) for it" for req, q in dict(wanted).items()
                    if str(req) in declared and int(q) > declared[str(req)]]
            refused.append(
                f"{dict(wanted)!r} with deadline levels "
                f"{dict(row.get('deadlineLevels') or {})!r} is outside the owner-authorized "
                f"targets"
                + (f" ({'; '.join(over)}); lower the alternative to the authorized steps"
                   if over else
                   " (this combination of levels and deadline levels is not authorized)"))
            continue
        if id(target) in mandatory_ids:
            removed["restatedMandatory"].append(dict(row))
            continue
        if target in additions:
            removed["duplicates"].append(dict(row))
            continue
        additions.append(target)
        refs = row.get("evidenceRefs")
        said[id(target)] = (_clean(row.get("selectionRole")) or None,
                            _clean(row.get("reason")) or None,
                            [str(ref) for ref in refs] if isinstance(refs, Sequence)
                            and not isinstance(refs, str) else None)
    limit = model_addition_limit(authorization, len(boundary))
    if len(additions) > limit:
        refused.append(f"{len(additions)} distinct additional targets were selected; "
                       f"at most {limit} are allowed besides the "
                       f"{1 + len(boundary)} mandatory targets")
    if refused:
        # The counted revision path: the caller turns this into a refusal the
        # model gets one bounded chance to correct.  Nothing is cut or dropped.
        raise TargetValidationError("; ".join(refused))

    for bucket, label in (("restatedMandatory", "restated a mandatory target"),
                          ("duplicates", "repeated an addition")):
        if removed[bucket]:
            notes.append(f"{len(removed[bucket])} row(s) {label}; removed before "
                         f"the addition limit was counted")
    order = {id(target): index for index, target in enumerate(contract.alternatives)}
    kept = sorted(boundary + additions, key=lambda target: order[id(target)])
    renumbered = tuple(replace(target, target_id=f"T{index + 1}")
                       for index, target in enumerate(kept))
    ids = {id(target): renumbered[index].target_id for index, target in enumerate(kept)}
    if not additions:
        notes.append("the answer selected no additional target; T is the "
                     f"{1 + len(boundary)} mandatory targets")
    notes.append(f"T holds {1 + len(boundary)} mandatory and {len(additions)} "
                 f"model-selected targets of {1 + len(contract.alternatives)} authorized")
    if provenance is not None:
        provenance["modelSelection"] = {
            "order": [{"levels": dict(target.levels),
                       "deadlineLevels": dict(target.deadline_levels),
                       "targetId": ids[id(target)],
                       "selectionRole": said[id(target)][0],
                       "reason": said[id(target)][1],
                       "evidenceRefs": said[id(target)][2]} for target in additions],
            "removed": removed}
        provenance["targetMembership"] = {
            "mandatory": [{"targetId": "T0" if target is contract.t0 else ids[id(target)],
                           "levels": dict(_level_vector(target, authorization))}
                          for target in [contract.t0] + sorted(boundary, key=lambda t: order[id(t)])],
            "modelAdditions": [{"targetId": ids[id(target)],
                                "levels": dict(_level_vector(target, authorization))}
                               for target in additions]}
        provenance["expressionError"] = target_diagnostics(
            (contract.t0,) + renumbered, contract)
    return replace(contract, alternatives=renumbered)


def _axis_levels(target: Target, authorization: Authorization) -> Dict[str, int]:
    """Which level each of ``target``'s axes sits at, keyed by :func:`_axis_id`.

    A target carries its levels when the expansion built it, but one that came
    back from a model or a record may carry only thresholds, so the level is
    recovered from the value when it has to be.  A deadline the target does not
    name is level ``0`` -- not extending it is what "unnamed" means everywhere
    else in this module.
    """
    found: Dict[str, int] = {}
    for req_id, entry in authorization.requirements.items():
        level = target.levels.get(req_id)
        if level is None and req_id in target.requirements:
            level = entry.level_of(target.requirements[req_id])
        if level is not None:
            found[_axis_id(req_id)] = int(level)
        if not entry.has_deadline:
            continue
        deadline_level = target.deadline_levels.get(req_id)
        if deadline_level is None:
            stretched = target.deadlines.get(req_id)
            deadline_level = 0
            if stretched is not None:
                for index, value in enumerate(entry.deadline_levels):
                    if _same_value(value, stretched):
                        deadline_level = index
                        break
        found[_axis_id(req_id, True)] = int(deadline_level)
    return found


def validate_target_contract(contract: Any,
                             authorization: Optional[Authorization] = None,
                             ) -> Tuple[TargetContract, List[str]]:
    """Check a ``T`` against what the owner signed.

    Two shapes arrive here.  A **compact** answer (the mapping the Target agent
    returns: ``t0`` / ``levels`` / ``constraints`` / ``ranking``) is expanded by
    :meth:`TargetContract.from_compact`, which is where the level bounds are
    clipped back onto the signed interval.  An already **expanded** contract
    keeps the v1 behaviour: the alternatives the owner did not authorize are
    dropped and reported, and ``T0`` is never touched.

    An unauthorized or incomplete ``T0`` is a :class:`TargetValidationError` in
    both shapes: there is nothing to clean, the caller has to fall back.
    """
    if isinstance(contract, Mapping):
        if authorization is None:
            raise TargetValidationError(
                "a compact target contract needs the owner's authorization")
        expanded = TargetContract.from_compact(
            contract.get("t0", {}), contract.get("levels", {}),
            contract.get("constraints", ()) or (), contract.get("ranking", {}) or {},
            authorization, contract.get("alternatives", ()) or ())
        return expanded, list(expanded.provenance.get("notes", ()))

    authorization = authorization or contract.authorization
    req_ids = list(authorization.req_ids)
    if not req_ids:
        raise TargetValidationError("the authorization names no requirement")

    def problems(target: Target) -> List[str]:
        found: List[str] = []
        missing = [req_id for req_id in req_ids if req_id not in target.requirements]
        if missing:
            found.append(f"does not name {', '.join(sorted(missing))}")
        extra = [req_id for req_id in target.requirements if req_id not in req_ids]
        if extra:
            found.append(f"names unknown requirement {', '.join(sorted(extra))}")
        for req_id in req_ids:
            if req_id not in target.requirements:
                continue
            entry = authorization.get(req_id)
            value = target.requirements[req_id]
            if entry is None:
                continue
            if not entry.authorizes(value):
                found.append(
                    f"{req_id}={value} is outside the authorized range "
                    f"[{entry.original} {entry.op} {entry.limit}]")
                continue
            condition = authorization.condition_for(entry.observation_key)
            if condition is not None and not condition.allows_threshold(value):
                found.append(f"{req_id}={value} breaks the joint condition "
                             f"{condition.kpi} {condition.op} {condition.value}")
        for req_id, stretched in target.deadlines.items():
            entry = authorization.get(req_id)
            if entry is None:
                continue
            if not entry.has_deadline:
                found.append(f"{req_id} names a deadline {stretched} ms but the "
                             "owner authorized none")
            elif not entry.authorizes_deadline(stretched):
                found.append(
                    f"{req_id} deadline {stretched} ms is outside the separately "
                    f"authorized [{entry.deadline_ms}, {entry.deadline_bound}] ms")
        if authorization.mode_constraints:
            axis_levels = _axis_levels(target, authorization)
            for constraint in authorization.mode_constraints:
                if not constraint.permits(axis_levels):
                    found.append(f"breaks the authorized modes: "
                                 f"{constraint.describe()}")
        return found

    t0_problems = problems(contract.t0)
    for req_id in req_ids:
        entry = authorization.get(req_id)
        if entry is None or req_id not in contract.t0.requirements:
            continue
        if not _same_value(contract.t0.requirements[req_id], entry.original):
            t0_problems.append(f"T0 moved {req_id} off its original {entry.original}")
        if (entry.has_deadline and req_id in contract.t0.deadlines
                and not _same_value(contract.t0.deadlines[req_id], entry.deadline_ms)):
            t0_problems.append(f"T0 moved {req_id} off its original deadline "
                               f"{entry.deadline_ms} ms")
    if t0_problems:
        raise TargetValidationError(
            f"{contract.t0.target_id}: " + "; ".join(t0_problems))

    kept: List[Target] = []
    dropped: List[str] = []
    seen = {contract.t0.target_id}
    for target in contract.alternatives:
        if target.target_id in seen:
            dropped.append(f"{target.target_id}: duplicate target id")
            continue
        found = problems(target)
        if found:
            dropped.append(f"{target.target_id}: " + "; ".join(found))
            continue
        seen.add(target.target_id)
        kept.append(replace(target, concession=dict(
            concession_of(target, authorization)["perRequirement"]),
            cost=cost_of(target, authorization)))
    t0 = replace(contract.t0,
                 concession=dict(concession_of(contract.t0, authorization)["perRequirement"]),
                 cost=cost_of(contract.t0, authorization))
    return replace(contract, t0=t0, alternatives=tuple(kept),
                   authorization=authorization), dropped
def concession_of(target: Target, authorization: Authorization) -> Dict[str, Any]:
    """The normalized concession of one target -- ``exp_metrics.md`` section 2.

    ``q_r = (b_r - v_r) / (b_r - l_r)`` for a floor and
    ``q_r = (v_r - b_r) / (l_r - b_r)`` for a ceiling; a permitted
    strengthening is ``0``; a non-relaxable requirement is ``0`` when it keeps
    its original and **invalid** otherwise.  ``q_r`` above 1 is unauthorized,
    reported as-is and listed in ``invalid`` -- never clipped into a success.

    ``D_o`` is the mean of ``q_r`` over the requirements of one owner that are
    **authorized to move**, ``mean`` the mean of ``D_o`` across owners and
    ``max`` the largest ``D_o``.  Requirements nobody may relax are reported in
    ``perRequirement`` but kept out of the owner's denominator: a fixed success
    ratio contributes a permanent ``0`` that would otherwise halve the
    concession it sits beside.
    """
    per_requirement: Dict[str, float] = {}
    invalid: List[str] = []
    by_owner: Dict[str, List[float]] = {}
    for req_id, entry in authorization.requirements.items():
        if req_id not in target.requirements:
            invalid.append(req_id)
            continue
        value = target.requirements[req_id]
        original, limit = _as_float(entry.original), _as_float(entry.limit)
        candidate = _as_float(value)
        if original is None or candidate is None:
            quality = 0.0 if _clean(value) == _clean(entry.original) else 1.0
            if quality:
                invalid.append(req_id)
        elif limit is None or abs(limit - original) <= _TOLERANCE:
            quality = 0.0 if abs(candidate - original) <= _TOLERANCE else 1.0
            if quality:
                invalid.append(req_id)
        elif entry.op == _CEILING:
            quality = (candidate - original) / (limit - original)
        else:
            quality = (original - candidate) / (original - limit)
        if quality < 0.0:
            quality = 0.0  # a permitted strengthening costs nothing
        if quality > 1.0 + _TOLERANCE and req_id not in invalid:
            invalid.append(req_id)
        per_requirement[req_id] = quality
        if entry.relaxable:
            by_owner.setdefault(entry.owner or req_id, []).append(quality)

        # The deadline is the intent's *second* authorized concession, so it
        # gets its own q_r under the same owner rather than being folded into
        # the threshold's -- "any reliability concession separately
        # authorized" cuts both ways: separately signed, separately reported.
        if not entry.has_deadline or req_id not in target.deadlines:
            continue
        deadline_id = f"{req_id}{DEADLINE_CONCESSION_SUFFIX}"
        stretched = _as_float(target.deadlines[req_id])
        original, bound = entry.deadline_ms, _as_float(entry.deadline_bound)
        if stretched is None:
            deadline_quality = 1.0
            invalid.append(deadline_id)
        elif bound is None or abs(bound - original) <= _TOLERANCE:
            deadline_quality = 0.0 if abs(stretched - original) <= _TOLERANCE else 1.0
            if deadline_quality:
                invalid.append(deadline_id)
        else:
            # a deadline is only ever *extended*, so the ceiling arithmetic
            deadline_quality = (stretched - original) / (bound - original)
        if deadline_quality < 0.0:
            deadline_quality = 0.0        # a tightened deadline costs nothing
        if deadline_quality > 1.0 + _TOLERANCE and deadline_id not in invalid:
            invalid.append(deadline_id)
        per_requirement[deadline_id] = deadline_quality
        if entry.deadline_relaxable:
            by_owner.setdefault(entry.owner or req_id, []).append(deadline_quality)

    per_owner = {owner: sum(values) / len(values) for owner, values in by_owner.items()}
    values = list(per_owner.values())
    return {"perRequirement": per_requirement, "perOwner": per_owner,
            "mean": (sum(values) / len(values)) if values else 0.0,
            "max": max(values) if values else 0.0,
            "invalid": sorted(invalid)}


def preference_key(target: Target, authorization: Authorization,
                   preference: Optional[Preference] = None) -> Tuple[float, ...]:
    """Where ``target`` sits in the owner's ranking -- contract v2 section 3.

    Lexicographic over the owners' *normalized* concessions ``D_o``, taken in
    ``owner_priority`` order: ``P1`` is ``(D1, D2, D3)`` and ``P2`` is the same
    rule with the owners declared in a different order.  A ``rule`` naming
    ``D_max`` -- ``P3`` -- puts ``(D_max, D_mean)`` in front of that tail.

    Integer levels are deliberately not what orders targets.  ``q`` counts
    subdivisions, so ``sum_i w_i q_i^2`` charges a full goodput relaxation
    ``4w`` against a full deadline extension's ``w`` although each is the whole
    of what its owner authorized; normalized, both are ``1``.  Ranking on levels
    would also give one target two different places depending on how finely
    ``T`` happened to subdivide the same interval.  :func:`cost_of` is still
    recorded on every target -- it is just not the ordering.
    """
    preference = preference or authorization.preference
    quality = concession_of(target, authorization)
    if preference.coordinate_order and (preference.rule or "").startswith("v5.1:"):
        # v5.1: one weighted sum, parsed from the recorded rule so a replay ranks as it ran.
        per_req = quality["perRequirement"]
        terms = re.findall(r"(\d+) k\[([^\]]+)\]", preference.rule)
        # A deadline requirement is charged for its ratio and its deadline, as the evaluator does.
        k = lambda key: math.ceil(float(per_req.get(key, 0.0)) * 20 - 1e-9)
        return (float(sum(int(w) * (k(req) + k(req + "#deadline")) for w, req in terms)),)
    if preference.coordinate_order:
        # v5: k = ceil(20 q) per coordinate, in the declared order; nothing in front of it.
        per_req = quality["perRequirement"]
        return tuple(float(math.ceil(float(per_req.get(req, 0.0)) * 20 - 1e-9))
                     for req in preference.coordinate_order)
    per_owner = quality["perOwner"]
    owners = preference.owner_priority or tuple(sorted(per_owner))
    tail = tuple(round(float(per_owner.get(owner, 0.0)), 9) for owner in owners)
    if "D_max" in (preference.rule or ""):
        return (round(float(quality["max"]), 9),
                round(float(quality["mean"]), 9)) + tail
    return tail


def deterministic_targets(intents: Sequence[Intent], *,
                          preference: Optional[Preference] = None,
                          joint_conditions: Sequence[Any] = (),
                          ) -> TargetContract:
    """The deterministic ``T``: every authorized combination of levels.

    This is :func:`expand_targets` over the authorization the intents carry --
    the same expansion the executor uses when a Target answer is refused.  No
    model, no randomness.
    """
    authorization = Authorization.from_intents(
        list(intents), joint_conditions=joint_conditions,
        preference=preference or Preference.from_intents(list(intents)))
    return omega_or_sparse(authorization)


# --------------------------------------------------------------------------- #
# section 2.3 -- C
# --------------------------------------------------------------------------- #


def policy_ranges() -> bool:
    """v5.1 (``AIC_V51=1``, or ``AIC_POLICY_RANGE=1`` alone): models see numeric policy fields
    as ``min``/``max``."""
    return (_os.environ.get("AIC_POLICY_RANGE", "").strip() == "1"
            or _os.environ.get("AIC_V51", "").strip() == "1")


def _policy_numbers(values: Sequence[str]) -> Optional[List[float]]:
    try:
        numbers = [float(value) for value in values]
    except (TypeError, ValueError):
        return None
    return numbers if numbers and all(math.isfinite(n) for n in numbers) else None


def snap_policy_value(allowed: Sequence[str], value: Any) -> Optional[str]:
    """The grid value nearest a number inside the field's range; ``None`` outside it
    or when either side is not numeric.  Ties go to the lower value."""
    numbers = _policy_numbers(allowed)
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if not numbers or not math.isfinite(x) or not min(numbers) <= x <= max(numbers):
        return None
    return min(zip(numbers, allowed), key=lambda pair: (abs(pair[0] - x), pair[0]))[1]


def ranged_policy_field(record: Mapping[str, Any]) -> Dict[str, Any]:
    """How a model sees one policy-field record under ``AIC_POLICY_RANGE=1``: a numeric
    ladder becomes ``min``/``max`` (cell ids stay listed).  Records themselves keep the grid."""
    values = [str(v) for v in record.get("values", ()) or ()]
    numbers = _policy_numbers(values) if record.get("unit") != "nci" else None
    if not numbers:
        return dict(record)
    out = {key: value for key, value in record.items() if key != "values"}
    out["min"], out["max"] = values[numbers.index(min(numbers))], values[numbers.index(max(numbers))]
    if _os.environ.get("AIC_V52", "").strip() == "1":   # v5.2: the resolution the radio applies
        from assurance.coordination.v52 import step_of
        step = step_of(numbers)
        if step:
            out["step"] = step
    return out


def snap_function_rows(rows: Any, catalog: Optional["FunctionCatalog"]) -> Any:
    """``functions``/``instructions`` rows with each off-grid numeric policy value moved onto
    the frozen grid, so every record and every later prompt shows what is applied.  Values that
    cannot be snapped are left for ``translate_functions`` to refuse.  A no-op unless range mode."""
    if not policy_ranges() or catalog is None or not isinstance(rows, Sequence) or isinstance(rows, str):
        return rows
    out = []
    for row in rows:
        spec = catalog.function(row.get("functionId")) if isinstance(row, Mapping) else None
        if spec is None or not isinstance(row.get("policy"), Mapping):
            out.append(row)
            continue
        policy = dict(row["policy"])
        for name, value in policy.items():
            entry = spec.policy_fields.get(str(name))
            if entry is not None and entry.unit != "nci" and str(value) not in entry.values:
                snapped = snap_policy_value(entry.values, value)
                if snapped is not None:
                    policy[name] = snapped
        out.append(dict(row, policy=policy))
    return out


def snap_configuration(configuration: Any, space: Mapping[str, Sequence[str]]) -> Any:
    """A configuration-form answer (axis -> value) snapped the same way; ``servingCell`` never."""
    if not policy_ranges() or not isinstance(configuration, Mapping):
        return configuration
    out = dict(configuration)
    for axis, value in out.items():
        allowed = tuple(str(v) for v in space.get(str(axis), ()) or ())
        if allowed and not str(axis).startswith("servingCell") and str(value) not in allowed:
            snapped = snap_policy_value(allowed, value)
            if snapped is not None:
                out[axis] = snapped
    return out


@dataclass(frozen=True)
class PolicyField:
    """One field of one function's policy: what it may be set to, and to what
    it returns when the function is not selected."""

    name: str
    values: Tuple[str, ...] = ()
    unit: str = ""
    baseline: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _clean(self.name))
        object.__setattr__(self, "values", tuple(str(item) for item in self.values))
        object.__setattr__(self, "unit", _clean(self.unit))
        if self.baseline is not None:
            object.__setattr__(self, "baseline", str(self.baseline))
        if not self.name:
            raise ValueError("a policy field needs a name")

    def to_record(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {"values": list(self.values), "unit": self.unit}
        if self.baseline is not None:
            record["baseline"] = self.baseline
        return record

    @classmethod
    def from_record(cls, name: str, record: Mapping[str, Any]) -> "PolicyField":
        record = dict(record or {})
        return cls(name=str(name), values=tuple(record.get("values", ()) or ()),
                   unit=_clean(record.get("unit")), baseline=record.get("baseline"))


@dataclass(frozen=True)
class FunctionSpec:
    """One xApp function this sitting exposes (contract v2 section 3.1).

    ``axis`` is the executor's translation key -- ``dlPrbCap@<ue>`` becomes
    ``dlPrbCap@132`` for the scope ``ue@132``.  The models see it but never
    need it: they speak functions, policies and scopes.
    """

    function_id: str
    xapp: str = ""
    action_id: str = ""
    scopes: Tuple[str, ...] = ()
    policy_fields: Dict[str, PolicyField] = field(default_factory=dict)
    prerequisites: Tuple[str, ...] = ()
    axis: str = ""
    axis_field: str = ""
    #: What the function adjusts -- a definition, never an effect (owner, 2026-09-20).
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "function_id", _clean(self.function_id))
        object.__setattr__(self, "description", _clean(self.description))
        object.__setattr__(self, "xapp", _clean(self.xapp))
        object.__setattr__(self, "action_id", _clean(self.action_id))
        object.__setattr__(self, "scopes", tuple(_clean(item) for item in self.scopes))
        fields = {}
        for name, entry in dict(self.policy_fields or {}).items():
            fields[str(name)] = (entry if isinstance(entry, PolicyField)
                                 else PolicyField.from_record(name, entry))
        object.__setattr__(self, "policy_fields", fields)
        object.__setattr__(self, "prerequisites",
                           tuple(_clean(item) for item in self.prerequisites))
        object.__setattr__(self, "axis", _clean(self.axis))
        axis_field = _clean(self.axis_field)
        if not axis_field and fields:
            axis_field = next(iter(fields))
        object.__setattr__(self, "axis_field", axis_field)
        if not self.function_id:
            raise ValueError("a function needs a functionId")
        if not self.axis:
            raise ValueError(f"function {self.function_id} needs an axis")

    def axis_for(self, scope: str) -> str:
        """``servingCell@<ue>`` on ``ue@131`` is the axis ``servingCell@131``.

        The placeholder names the *kind* of scope the function is written at --
        ``<ue>``, ``<cell>``, ``<sst>`` -- and every kind is substituted the
        same way, from the scope's own target.  Naming the kind is what keeps a
        cell-scoped function from silently reading as a UE-scoped one when the
        two are listed side by side.
        """
        target = scope_target(scope)
        substituted = _AXIS_PLACEHOLDER.sub(target, self.axis)
        if substituted != self.axis:
            return substituted
        if self.axis.endswith("@"):
            return self.axis + target
        return self.axis

    def values_for(self, field_name: str) -> Tuple[str, ...]:
        entry = self.policy_fields.get(str(field_name))
        return entry.values if entry is not None else ()

    @property
    def axis_values(self) -> Tuple[str, ...]:
        return self.values_for(self.axis_field)

    @property
    def axis_baseline(self) -> Optional[str]:
        entry = self.policy_fields.get(self.axis_field)
        if entry is None:
            return None
        if entry.baseline is not None:
            return entry.baseline
        return entry.values[0] if entry.values else None

    def to_record(self) -> Dict[str, Any]:
        record = {"functionId": self.function_id, "xapp": self.xapp,
                "actionId": self.action_id, "scopes": list(self.scopes),
                "policyFields": {name: entry.to_record()
                                 for name, entry in self.policy_fields.items()},
                "prerequisites": list(self.prerequisites), "axis": self.axis,
                "axisField": self.axis_field}
        if self.description:
            record["description"] = self.description
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "FunctionSpec":
        record = dict(record or {})
        return cls(function_id=record.get("functionId", record.get("function_id", "")),
                   xapp=record.get("xapp", ""), action_id=str(record.get("actionId", "")),
                   scopes=tuple(record.get("scopes", ()) or ()),
                   policy_fields=dict(record.get("policyFields", {}) or {}),
                   prerequisites=tuple(record.get("prerequisites", ()) or ()),
                   axis=record.get("axis", ""),
                   axis_field=record.get("axisField", record.get("axis_field", "")),
                   description=record.get("description", ""))


@dataclass(frozen=True)
class FunctionCatalog:
    """The functions this sitting exposes, and the axis space they span."""

    functions: Tuple[FunctionSpec, ...] = ()

    def __post_init__(self) -> None:
        rows = tuple(item if isinstance(item, FunctionSpec)
                     else FunctionSpec.from_record(item) for item in self.functions)
        object.__setattr__(self, "functions", rows)

    def __iter__(self):
        return iter(self.functions)

    def __len__(self) -> int:
        return len(self.functions)

    @property
    def function_ids(self) -> Tuple[str, ...]:
        return tuple(spec.function_id for spec in self.functions)

    def function(self, function_id: str) -> Optional[FunctionSpec]:
        for spec in self.functions:
            if spec.function_id == str(function_id):
                return spec
        return None

    def axes(self) -> Dict[str, Tuple[str, ...]]:
        """The action space: one axis per (function, scope), with its values."""
        space: Dict[str, Tuple[str, ...]] = {}
        for spec in self.functions:
            for scope in spec.scopes:
                space[spec.axis_for(scope)] = spec.axis_values
        return space

    def axis_owner(self, axis: str) -> Optional[Tuple[FunctionSpec, str]]:
        for spec in self.functions:
            for scope in spec.scopes:
                if spec.axis_for(scope) == str(axis):
                    return spec, scope
        return None

    def baselines(self, applied: Optional[Mapping[str, Any]] = None
                  ) -> Dict[str, str]:
        """Each axis's deactivated value; ``applied`` fills in what the catalog
        cannot know (which cell a UE is on right now)."""
        applied = {str(k): str(v) for k, v in dict(applied or {}).items()}
        baseline: Dict[str, str] = {}
        for spec in self.functions:
            for scope in spec.scopes:
                axis = spec.axis_for(scope)
                value = spec.axis_baseline
                if axis in applied:
                    value = applied[axis]
                if value is not None:
                    baseline[axis] = str(value)
        return baseline

    def to_record(self) -> List[Dict[str, Any]]:
        return [spec.to_record() for spec in self.functions]

    @classmethod
    def from_record(cls, record: Any) -> "FunctionCatalog":
        if isinstance(record, FunctionCatalog):
            return record
        rows = record.get("functions", ()) if isinstance(record, Mapping) else record
        return cls(tuple(FunctionSpec.from_record(item) for item in (rows or ())))


@dataclass(frozen=True)
class CompatibilityRules:
    """``input.compatibility``: what may be used together, and in what order.

    Nothing here is a new integrity gate -- it is the deployment's own co-use
    truth, handed to the models as text and applied by the executor when it
    translates a candidate.
    """

    mutually_exclusive: Tuple[Tuple[str, str], ...] = ()
    precedence: Tuple[Tuple[str, str], ...] = ()
    max_functions_per_scope: Optional[int] = None
    #: The per-*configuration* cap: how many function/scope entries one
    #: candidate may change at once.  ``max_functions_per_scope`` cannot say
    #: this -- it is tested inside one scope group, so five entries on five
    #: scopes are five groups of one and it never fires.
    max_changed_entries: Optional[int] = None
    notes: Tuple[str, ...] = ()
    #: 핸드오프 2026-09-18 §3: **변환된 최종 설정**에서 같은 범위(UE·셀)에 함께 켜질 수
    #: 없는 축 종류 쌍.  `mutually_exclusive` 는 모델이 적은 함수 이름과 범위 표기를
    #: 비교하므로, 표기가 다르거나 설정만 적은 후보는 빠져나간다 -- BM 판
    #: 20260917T184817 시행 4 가 ue2 에 cap 18 과 PF 4.0 을 함께 적용했다.
    exclusive_axes: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "exclusive_axes",
                           tuple(tuple(_clean(name) for name in pair)[:2]
                                 for pair in self.exclusive_axes))
        object.__setattr__(self, "mutually_exclusive",
                           tuple(tuple(_clean(name) for name in pair)[:2]
                                 for pair in self.mutually_exclusive))
        object.__setattr__(self, "precedence",
                           tuple(tuple(_clean(name) for name in pair)[:2]
                                 for pair in self.precedence))
        object.__setattr__(self, "notes",
                           tuple(_clean(item) for item in self.notes if _clean(item)))
        if self.max_functions_per_scope is not None:
            object.__setattr__(self, "max_functions_per_scope",
                               int(self.max_functions_per_scope))
        if self.max_changed_entries is not None:
            object.__setattr__(self, "max_changed_entries",
                               int(self.max_changed_entries))

    def configuration_refusals(self, configuration: Mapping[str, Any],
                               baselines: Optional[Mapping[str, Any]] = None) -> List[str]:
        """같은 범위에서 함께 켜질 수 없는 두 축이 **둘 다 기준값이 아니면** 거절한다.

        기준값을 포함했다는 이유만으로 충돌로 보지 않는다(§3.2) -- 비교 대상은 실제로
        켜진 값이다.  축 이름은 ``kind@scope`` 형식이다.
        """
        base = {str(k): str(v) for k, v in dict(baselines or {}).items()}
        active: Dict[str, set] = {}
        for axis, value in dict(configuration).items():
            kind, _, scope = str(axis).partition("@")
            if scope and base.get(str(axis)) != str(value):
                active.setdefault(scope, set()).add(kind)
        found: List[str] = []
        for scope, kinds in sorted(active.items()):
            for left, right in self.exclusive_axes:
                if left in kinds and right in kinds:
                    found.append(f"{left} and {right} may not both be active on {scope}")
        return found

    def refusals(self, selections: Sequence["FunctionSelection"]) -> List[str]:
        """Why this set of selections cannot be applied together, if it cannot."""
        found: List[str] = []
        # Before the by-scope grouping, because this one is a statement about
        # the whole configuration: the grouping below would split these across
        # scopes and never see the total.
        if (self.max_changed_entries is not None
                and len(selections) > self.max_changed_entries):
            found.append(f"{len(selections)} changed function/scope entries; at "
                         f"most {self.max_changed_entries} may change per "
                         "configuration")
        by_scope: Dict[str, List[str]] = {}
        for selection in selections:
            by_scope.setdefault(selection.scope, []).append(selection.function_id)
        for scope, names in by_scope.items():
            for left, right in self.mutually_exclusive:
                if left in names and right in names:
                    found.append(f"{left} and {right} may not both act on {scope}")
            if (self.max_functions_per_scope is not None
                    and len(names) > self.max_functions_per_scope):
                found.append(f"{len(names)} functions on {scope}; at most "
                             f"{self.max_functions_per_scope} may act together")
            duplicates = sorted({name for name in names if names.count(name) > 1})
            for name in duplicates:
                found.append(f"{name} is selected twice on {scope}")
        return found

    def ordered(self, selections: Sequence["FunctionSelection"]
                ) -> Tuple["FunctionSelection", ...]:
        """The selections in the deployment's precedence order (stable otherwise)."""
        rank: Dict[str, int] = {}
        for before, after in self.precedence:
            rank.setdefault(before, 0)
            rank[after] = max(rank.get(after, 0), rank[before] + 1)
        return tuple(sorted(
            selections,
            key=lambda item: (rank.get(item.function_id, 0),
                              list(selections).index(item))))

    def to_record(self) -> Dict[str, Any]:
        return {"mutuallyExclusive": [list(pair) for pair in self.mutually_exclusive],
                "precedence": [list(pair) for pair in self.precedence],
                "maxFunctionsPerScope": self.max_functions_per_scope,
                "maxChangedEntries": self.max_changed_entries,
                "exclusiveAxes": [list(pair) for pair in self.exclusive_axes],
                "notes": list(self.notes)}

    @classmethod
    def from_record(cls, record: Any) -> "CompatibilityRules":
        if isinstance(record, CompatibilityRules):
            return record
        if isinstance(record, Mapping):
            return cls(
                mutually_exclusive=tuple(tuple(pair) for pair in
                                         record.get("mutuallyExclusive", ()) or ()),
                precedence=tuple(tuple(pair) for pair in
                                 record.get("precedence", ()) or ()),
                max_functions_per_scope=record.get("maxFunctionsPerScope"),
                max_changed_entries=record.get("maxChangedEntries"),
                exclusive_axes=tuple(tuple(pair) for pair in
                                     record.get("exclusiveAxes", ()) or ()),
                notes=tuple(record.get("notes", ()) or ()))
        return cls(notes=tuple(str(item) for item in (record or ())))


@dataclass(frozen=True)
class FunctionSelection:
    """One function a candidate uses, with the policy and scope it is given."""

    function_id: str
    scope: str
    policy: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "function_id", _clean(self.function_id))
        object.__setattr__(self, "scope", _clean(self.scope))
        object.__setattr__(self, "policy",
                           {str(k): v for k, v in dict(self.policy or {}).items()})
        if not self.function_id:
            raise ValueError("a function selection needs a functionId")

    def to_record(self) -> Dict[str, Any]:
        return {"functionId": self.function_id, "scope": self.scope,
                "policy": dict(self.policy)}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "FunctionSelection":
        record = dict(record or {})
        return cls(function_id=record.get("functionId", record.get("function_id", "")),
                   scope=record.get("scope", ""),
                   policy=dict(record.get("policy", {}) or {}))


@dataclass(frozen=True)
class ControlCandidate:
    """One column of the board: the functions used together, and what they do.

    ``functions`` is what the Control agent answers; ``configuration`` is the
    executor's translation of it onto the action axes, which is what the frozen
    candidate catalog and the Kernel actually see.  A candidate read from a v1
    record has only ``configuration`` and still works.
    """

    control_id: str
    configuration: Dict[str, str] = field(default_factory=dict)
    effect_estimate: Dict[str, Any] = field(default_factory=dict)
    applicability: Tuple[str, ...] = ()
    interactions: str = ""
    functions: Tuple[FunctionSelection, ...] = ()
    predicted: Dict[str, Any] = field(default_factory=dict)
    uncertainty: Dict[str, Any] = field(default_factory=dict)
    predicted_target: str = ""
    evidence_refs: Tuple[str, ...] = ()
    rationale: str = ""
    #: The KPIs Control says this configuration is related to -- keys only, no
    #: direction, size or value (owner instruction 2026-09-20: "control 이 연관만
    #: 만들게 하고 값 추정은 하지 말게 하라").
    related_kpis: Tuple[str, ...] = ()
    #: The target the executor's predictor would aim this column at.  Internal:
    #: the deterministic fallbacks rank by it, and ``to_record`` -- which is what
    #: a model receives -- never writes it (owner instruction 2026-09-19).
    predictor_target: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "control_id", _clean(self.control_id))
        object.__setattr__(self, "configuration",
                           {str(k): str(v) for k, v in dict(self.configuration).items()})
        related = self.related_kpis
        if isinstance(related, str) or not isinstance(related, (list, tuple)):
            related = ()
        object.__setattr__(self, "related_kpis",
                           tuple(dict.fromkeys(str(item) for item in related if str(item).strip())))
        object.__setattr__(self, "functions",
                           tuple(item if isinstance(item, FunctionSelection)
                                 else FunctionSelection.from_record(item)
                                 for item in self.functions))
        def table(value: Any) -> Dict[str, Any]:
            """The model's table, or ``{}`` when it answered with something else.

            These three keys are omitted from ``to_record`` when they are empty,
            and a model reading that schema has answered the gap with the bare
            string ``"unknown"`` -- which used to take the whole episode down
            inside ``dict()``.  A malformed prediction is not a statement about
            the radio, so it is dropped rather than raised on.
            """
            if isinstance(value, Mapping):
                return dict(value)
            try:
                return dict(value or {})
            except (TypeError, ValueError):
                return {}

        predicted = table(self.predicted)
        estimate = table(self.effect_estimate)
        # the v1 name and the v2 name are the same table
        if predicted and not estimate:
            estimate = dict(predicted)
        elif estimate and not predicted:
            predicted = dict(estimate)
        object.__setattr__(self, "predicted", predicted)
        object.__setattr__(self, "effect_estimate", estimate)
        object.__setattr__(self, "uncertainty", table(self.uncertainty))
        object.__setattr__(self, "predicted_target", _clean(self.predicted_target))
        object.__setattr__(self, "evidence_refs",
                           tuple(_clean(item) for item in self.evidence_refs))
        object.__setattr__(self, "applicability",
                           tuple(_clean(item) for item in self.applicability))
        object.__setattr__(self, "interactions", _clean(self.interactions))
        object.__setattr__(self, "rationale", _clean(self.rationale))
        if not self.control_id:
            raise ValueError("a control candidate needs a controlId")

    @property
    def signature(self) -> Tuple[Tuple[str, str], ...]:
        return tuple(sorted(self.configuration.items()))

    def changes_from(self, configuration: Mapping[str, str]) -> int:
        current = {str(k): str(v) for k, v in dict(configuration or {}).items()}
        return sum(1 for axis, value in self.configuration.items()
                   if current.get(axis) != value)

    def to_record(self) -> Dict[str, Any]:
        """The candidate as the model receives it.

        Empty positions are omitted rather than written out.  ``predicted``,
        ``uncertainty`` and ``effectEstimate`` are frequently ``{}`` -- a
        baseline candidate has no prediction to make -- and an empty container
        tells the model nothing while still costing prompt.  On a measured
        episode the candidate list carried eight of these.  ``from_record``
        defaults every one of these keys, so the round trip is unchanged; the
        identity-bearing fields (``controlId``, ``configuration``) are always
        written, empty or not, because their absence would be a different
        statement.
        """
        record: Dict[str, Any] = {"controlId": self.control_id,
                                  "configuration": dict(self.configuration)}
        optional = (("functions", [item.to_record() for item in self.functions]),
                    ("predicted", dict(self.predicted)),
                    ("uncertainty", dict(self.uncertainty)),
                    ("predictedTarget", self.predicted_target),
                    ("relatedKpis", list(self.related_kpis)),
                    ("evidenceRefs", list(self.evidence_refs)),
                    ("effectEstimate", dict(self.effect_estimate)),
                    ("applicability", list(self.applicability)),
                    ("interactions", self.interactions),
                    ("rationale", self.rationale))
        for key, value in optional:
            if value:
                record[key] = value
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ControlCandidate":
        record = dict(record or {})
        return cls(control_id=record.get("controlId", record.get("control_id", "")),
                   configuration=record.get("configuration", {}) or {},
                   effect_estimate=record.get("effectEstimate", {}) or {},
                   applicability=tuple(record.get("applicability", ()) or ()),
                   interactions=record.get("interactions", ""),
                   functions=tuple(record.get("functions", ()) or ()),
                   predicted=record.get("predicted", {}) or {},
                   uncertainty=record.get("uncertainty", {}) or {},
                   predicted_target=record.get("predictedTarget", ""),
                   evidence_refs=tuple(record.get("evidenceRefs", ()) or ()),
                   rationale=record.get("rationale", ""),
                   related_kpis=record.get("relatedKpis", ()) or ())


def translate_functions(candidate: ControlCandidate, catalog: FunctionCatalog,
                        baselines: Optional[Mapping[str, Any]] = None,
                        applied: Optional[Mapping[str, Any]] = None,
                        rule: str = RULE_BASELINE) -> Dict[str, str]:
    """A candidate's selected functions become one value per action axis.

    The selected functions set their own axis.  Every other axis follows
    ``rule``: ``baseline`` deactivates it (contract v2 section 3.2's default)
    and ``keep-current`` leaves the value that is applied right now.  Anything
    the catalog does not list -- an unknown function, a scope that function
    does not serve, a policy field or a value it does not offer, two functions
    writing one axis -- is a :class:`ControlValidationError`.
    """
    rule = _clean(rule) or RULE_BASELINE
    if rule not in UNSELECTED_FUNCTION_RULES:
        raise ControlValidationError(
            f"unknown unselected-function rule {rule!r}; "
            f"one of {list(UNSELECTED_FUNCTION_RULES)}")
    space = catalog.axes()
    baseline = catalog.baselines(applied if rule == RULE_KEEP_CURRENT else None)
    baseline.update({str(k): str(v) for k, v in dict(baselines or {}).items()
                     if str(k) in space})
    current = {str(k): str(v) for k, v in dict(applied or {}).items()}

    configuration: Dict[str, str] = {}
    for axis in space:
        if rule == RULE_KEEP_CURRENT and axis in current:
            configuration[axis] = current[axis]
        elif axis in baseline:
            configuration[axis] = baseline[axis]

    written: Dict[str, str] = {}
    for selection in candidate.functions:
        spec = catalog.function(selection.function_id)
        if spec is None:
            raise ControlValidationError(
                f"unknown function {selection.function_id!r}; "
                f"the catalog offers {list(catalog.function_ids)}")
        scope = selection.scope or (spec.scopes[0] if len(spec.scopes) == 1 else "")
        if scope not in spec.scopes:
            # 2026-09-27 v5.2 block 0: the IM form wrote "ue3" for "ue@ue3" on 8 of 10 candidates and
            # the board ran on C0 plus two probes.  A bare name that matches exactly one listed scope's
            # suffix is that scope; anything else is still refused.
            same = [s for s in spec.scopes if str(s).split("@", 1)[-1] == str(scope)]
            if len(same) == 1:
                scope = same[0]
        if scope not in spec.scopes:
            raise ControlValidationError(
                f"{spec.function_id} does not act on {selection.scope!r}; "
                f"its scopes are {list(spec.scopes)}")
        axis = spec.axis_for(scope)
        if axis in written:
            raise ControlValidationError(
                f"two functions write {axis} in {candidate.control_id}")
        policy = dict(selection.policy)
        unknown = [name for name in policy if name not in spec.policy_fields]
        if unknown:
            raise ControlValidationError(
                f"{spec.function_id} has no policy field "
                f"{', '.join(sorted(unknown))}; it has "
                f"{list(spec.policy_fields)}")
        for name, value in list(policy.items()):
            allowed = spec.values_for(name)
            if allowed and str(value) not in allowed:
                snapped = (snap_policy_value(allowed, value)
                           if policy_ranges() and spec.policy_fields[name].unit != "nci" else None)
                if snapped is None:
                    raise ControlValidationError(
                        f"{spec.function_id}.{name}={value} is not in {list(allowed)}"
                        if not policy_ranges() else
                        f"{spec.function_id}.{name}={value} is outside its range "
                        f"{spec.policy_fields[name].to_record()}")
                policy[name] = snapped
        if spec.axis_field not in policy:
            raise ControlValidationError(
                f"{spec.function_id} on {scope} does not set its "
                f"{spec.axis_field} policy")
        written[axis] = str(policy[spec.axis_field])
    configuration.update(written)
    return configuration
@dataclass(frozen=True)
class ControlCandidates:
    """``C``: the joint configurations, the functions behind them, and the axis
    values they may use."""

    candidates: Tuple[ControlCandidate, ...] = ()
    action_space: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    construction_policy: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)
    catalog: Optional[FunctionCatalog] = None
    schema_version: str = CONTROL_CANDIDATES_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "action_space",
                           {str(axis): tuple(str(item) for item in values)
                            for axis, values in dict(self.action_space).items()})
        object.__setattr__(self, "construction_policy", dict(self.construction_policy or {}))
        object.__setattr__(self, "provenance", dict(self.provenance or {}))
        if self.catalog is not None and not isinstance(self.catalog, FunctionCatalog):
            object.__setattr__(self, "catalog", FunctionCatalog.from_record(self.catalog))

    @property
    def control_ids(self) -> Tuple[str, ...]:
        return tuple(candidate.control_id for candidate in self.candidates)

    def candidate(self, control_id: str) -> Optional[ControlCandidate]:
        for item in self.candidates:
            if item.control_id == str(control_id):
                return item
        return None

    def to_record(self) -> Dict[str, Any]:
        record = {"schemaVersion": self.schema_version,
                  "candidates": [item.to_record() for item in self.candidates],
                  "actionSpace": {axis: list(values)
                                  for axis, values in self.action_space.items()},
                  "constructionPolicy": dict(self.construction_policy),
                  "provenance": dict(self.provenance)}
        if self.catalog is not None:
            record["functionCatalog"] = self.catalog.to_record()
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ControlCandidates":
        record = dict(record or {})
        catalog = record.get("functionCatalog")
        return cls(candidates=tuple(ControlCandidate.from_record(item)
                                    for item in record.get("candidates", ()) or ()),
                   action_space=record.get("actionSpace", {}),
                   construction_policy=record.get("constructionPolicy", {}),
                   provenance=dict(record.get("provenance", {}) or {}),
                   catalog=(FunctionCatalog.from_record(catalog) if catalog else None),
                   schema_version=(_clean(record.get("schemaVersion"))
                                   or CONTROL_CANDIDATES_SCHEMA))


def validate_control_candidates(candidates: ControlCandidates,
                                action_space: Optional[Mapping[str, Sequence[str]]] = None,
                                baselines: Optional[Mapping[str, str]] = None,
                                *,
                                catalog: Optional[FunctionCatalog] = None,
                                compatibility: Optional[CompatibilityRules] = None,
                                applied: Optional[Mapping[str, str]] = None,
                                rule: str = RULE_BASELINE,
                                retain: Optional[int] = None,
                                ) -> Tuple[ControlCandidates, List[str]]:
    """Keep only executable candidates; always keep the baseline as ``C0``.

    With a ``catalog`` this is the v2 path: each candidate's **functions** are
    translated onto the axes (:func:`translate_functions`) under the sitting's
    unselected-function ``rule``, the co-use rules are applied, candidates that
    translate to the same configuration are merged, and at most ``retain`` of
    them survive **in the agent's own order**.  Without one it is the v1 path:
    the configuration is checked axis by axis against the action space.

    Either way the refusals are returned as text and recorded, never raised: an
    unmappable candidate is excluded from ``C``, not a failure of the sitting.
    """
    catalog = catalog if catalog is not None else candidates.catalog
    space = {str(axis): tuple(str(item) for item in values)
             for axis, values in dict(action_space or candidates.action_space
                                      or (catalog.axes() if catalog else {})).items()}
    if not space:
        raise ValueError("a control candidate set needs an action space")
    compatibility = (compatibility if compatibility is not None
                     else CompatibilityRules())
    baseline = {str(axis): str(value) for axis, value in dict(baselines or {}).items()}
    if catalog is not None:
        for axis, value in catalog.baselines(applied).items():
            baseline.setdefault(axis, value)
    for axis, values in space.items():
        if axis not in baseline and values:
            baseline[axis] = values[0]
    for axis in [axis for axis in baseline if axis not in space]:
        baseline.pop(axis)

    kept: List[ControlCandidate] = []
    dropped: List[str] = []
    seen_ids: Dict[str, ControlCandidate] = {}
    seen_signature: Dict[Tuple[Tuple[str, str], ...], str] = {}

    baseline_candidate = ControlCandidate(
        control_id="C0", configuration=dict(baseline), functions=(),
        applicability=("baseline: no function selected",),
        rationale="the configuration the deployment is already in")
    kept.append(baseline_candidate)
    seen_ids["C0"] = baseline_candidate
    seen_signature[baseline_candidate.signature] = "C0"

    limit = None if retain is None else max(0, int(retain))
    for candidate in candidates.candidates:
        if candidate.control_id == "C0" and not candidate.functions:
            continue
        problems: List[str] = []
        if catalog is not None and candidate.functions:
            problems.extend(compatibility.refusals(candidate.functions))
            if not problems:
                try:
                    configuration = translate_functions(
                        candidate, catalog, baseline, applied, rule)
                except ControlValidationError as exc:
                    problems.append(str(exc))
                    configuration = {}
        else:
            configuration = dict(baseline)
            for axis, value in candidate.configuration.items():
                if axis not in space:
                    problems.append(f"unknown action axis {axis}")
                    continue
                if str(value) not in space[axis]:
                    problems.append(f"{axis}={value} is not in {list(space[axis])}")
                    continue
                configuration[axis] = str(value)
            # A candidate that names only a ``configuration`` never reaches
            # ``refusals()`` -- it has no functions to hand it -- so the
            # per-configuration cap is applied here instead, against the same
            # quantity ``changes_from`` counts.  Model Control arms do produce
            # these (a row with ``configuration`` and no ``functions`` is
            # accepted), so without this the cap would hold on three arms.
            changed = sum(1 for axis, value in configuration.items()
                          if baseline.get(axis) != value)
            if (compatibility.max_changed_entries is not None
                    and changed > compatibility.max_changed_entries):
                problems.append(
                    f"{changed} changed function/scope entries; at most "
                    f"{compatibility.max_changed_entries} may change per "
                    "configuration")
        if not problems:
            # 함수 경로와 설정만 적은 경로가 **같은 최종 설정 검사**를 받는다(§3).
            problems.extend(compatibility.configuration_refusals(configuration, baseline))
        if problems:
            dropped.append(f"{candidate.control_id}: " + "; ".join(problems))
            continue
        merged = replace(candidate, configuration=configuration)
        if merged.signature in seen_signature:
            other = seen_signature[merged.signature]
            if other != merged.control_id:
                dropped.append(f"{candidate.control_id}: same configuration as {other}")
            continue
        if merged.control_id in seen_ids:
            dropped.append(f"{candidate.control_id}: duplicate control id")
            continue
        if limit is not None and len(kept) - 1 >= limit:
            dropped.append(f"{candidate.control_id}: beyond the {limit} candidates "
                           "the construction policy retains")
            continue
        seen_ids[merged.control_id] = merged
        seen_signature[merged.signature] = merged.control_id
        kept.append(merged)

    return replace(candidates, candidates=tuple(kept), action_space=space,
                   catalog=catalog), dropped
def _refusal_note(selections: Sequence["FunctionSelection"],
                  refusals: Sequence[str]) -> str:
    """One refused combination, named the way ``validate_control_candidates``
    names its own drops: what was selected, then why it cannot be applied.

    The enumerators have no control id to name -- ids are assigned by position
    after the refusals are known -- so the shape stands in for one.
    """
    shape = " + ".join(f"{item.function_id} on {item.scope}"
                       for item in selections)
    return f"{shape}: " + "; ".join(refusals)


#: Per-UE KPI families an association names.  Measured keys, no direction.
ASSOCIATED_KPIS: Tuple[str, ...] = ("dlGoodputMbps", "deadlineSuccessRatio")


def control_kpi_associations(catalog: Optional["FunctionCatalog"],
                             network_state: Optional[Mapping[str, Any]],
                             kpis: Sequence[str] = ASSOCIATED_KPIS) -> List[Dict[str, Any]]:
    """Which KPIs each function/scope is associated with -- nothing more.

    Owner instruction 2026-09-19: the predictor may say only which control and
    which KPI are related, never an increase, a decrease, a size or a rank.
    The predictor's per-configuration table led the models to follow its
    ranking instead of reasoning (Control dropped power and steering as
    "costly", C shrank to five, and two arms explored almost identically).

    Derived from the current placement in ``network_state.ues[*].servingCell``:
    a UE-scoped cap or priority is associated with the UEs sharing that UE's
    cell; steering with the UE's own serving cell and the UEs of the cells it
    leaves and may join; a cell's transmit attenuation with that cell's UEs and
    every other cell's UEs (inter-cell interference).
    """
    if catalog is None:
        return []
    ues = dict(dict(network_state or {}).get("ues") or {})
    cell_of = {str(ue): str(dict(row).get("servingCell", "")) for ue, row in ues.items()}

    def keys(targets: Sequence[str]) -> List[str]:
        return [f"{kpi}@{ue}" for ue in sorted(set(targets)) for kpi in kpis]

    def in_cells(cells: Sequence[str]) -> List[str]:
        wanted = {str(cell) for cell in cells}
        return [ue for ue, cell in cell_of.items() if cell in wanted]

    rows: List[Dict[str, Any]] = []
    for spec in catalog.functions:
        for scope in spec.scopes:
            axis = spec.axis_for(scope)
            kind = axis_kind(axis)
            target = scope_target(scope)
            if scope.startswith("cell@"):
                related = keys(list(cell_of))           # its own cell and the neighbours
            elif kind == "servingCell":
                cells = [cell_of.get(target, "")] + list(spec.axis_values)
                related = [f"servingCell@{target}"] + keys(in_cells(cells) + [target])
            else:
                related = keys(in_cells([cell_of.get(target, "")]) + [target])
            rows.append({"functionId": spec.function_id, "scope": scope,
                         "axis": axis, "associatedKpis": related})
    return rows


def model_effect_evidence(catalog: Optional["FunctionCatalog"],
                          network_state: Optional[Mapping[str, Any]],
                          observations: Sequence[Mapping[str, Any]] = ()) -> Dict[str, Any]:
    """``input.effect_evidence`` as the models see it: measurements only.

    2026-09-19: the code-made association table is no longer handed over --
    which KPIs a control affects is for the Control agent to infer, and the
    basic monolith gets no such structure.  ``control_kpi_associations`` stays
    for records and tests.
    """
    return {"observations": [dict(item) for item in observations]}


def catalog_product_controls(catalog: FunctionCatalog,
                             baselines: Optional[Mapping[str, str]] = None,
                             *, compatibility: Optional[CompatibilityRules] = None,
                             dropped: Optional[List[str]] = None,
                             limit: Optional[int] = None,
                             ) -> Tuple[ControlCandidate, ...]:
    """Every executable combination the frozen catalog admits, as candidates.

    The Kernel freezes the *whole* product of the exposed axes, so that product
    -- not a shortlist of single-axis moves -- is what a RAN search may try.
    This enumerates it: one candidate per combination, each selecting exactly
    the functions whose axis is off its baseline (the all-baseline combination
    is ``C0``, added by :func:`validate_control_candidates`).  Combinations the
    compatibility rules refuse are left out -- and the reason is appended to
    ``dropped`` when one is supplied, so a combination that vanishes from the
    product says why it did, the way the validator's own drops do.
    """
    compatibility = compatibility or CompatibilityRules()
    baseline = catalog.baselines(baselines)
    baseline.update({str(k): str(v) for k, v in dict(baselines or {}).items()})

    # axis -> (its function spec, its scope, its values), in a stable order
    axes: List[Tuple[str, FunctionSpec, str, Tuple[str, ...]]] = []
    for spec in sorted(catalog.functions, key=lambda item: item.function_id):
        for scope in spec.scopes:
            axis = spec.axis_for(scope)
            axes.append((axis, spec, scope, tuple(str(v) for v in spec.axis_values)))
    axes.sort(key=lambda row: row[0])

    rows: List[ControlCandidate] = []
    for combination in itertools.product(*[values for _a, _s, _sc, values in axes]):
        selections: List[FunctionSelection] = []
        for (axis, spec, scope, _values), value in zip(axes, combination):
            if str(value) == baseline.get(axis):
                continue
            selections.append(FunctionSelection(
                function_id=spec.function_id, scope=scope,
                policy={spec.axis_field: str(value)}))
        if not selections:
            continue  # the all-baseline combination is C0
        refused = compatibility.refusals(tuple(selections))
        if refused:
            if dropped is not None:
                dropped.append(_refusal_note(selections, refused))
            continue
        rows.append(ControlCandidate(
            control_id=f"C{len(rows) + 1}", functions=tuple(selections),
            applicability=tuple(f"{item.function_id} on {item.scope}"
                                for item in selections),
            rationale=("one executable combination of the frozen catalog"
                       if len(selections) > 1 else "one function on its own")))

        # 2026-09-17 (오너 지시): `limit` 은 **결정론적 대체 경로**만 쓴다.
        # 그 경로는 모델 호출이 터졌을 때만 돌고(실측 569 호출 중 4 회, 68 판 중 2 판,
        # 사유는 관측 지연과 API 503 -- 둘 다 일시 장애), **그 판의 데이터는 버린다.**
        # 전역 최적일 필요가 없으니 집행 가능한 후보 하나면 된다.  곱을 통째로 만드는
        # 것이 액션 공간을 촘촘하게 못 만드는 유일한 이유였다.  `None` 이면 종전과 같다.
        if limit is not None and len(rows) >= int(limit):
            # 멈춘 것은 **거절이 아니다.**  `dropped` 는 '규칙이 물리친 조합' 을 뜻하므로
            # 여기에 멈춤을 섞으면 "거절이 없었으면 아무것도 기록하지 않는다" 는 계약이
            # 깨진다.  경계 자체는 부르는 쪽이 `limit` 을 준 사실로 이미 알고 있고,
            # 대체 경로는 그것을 자기 provenance 의 rationale 로 적는다.
            break
    return tuple(rows)


def cover_controls(candidates: Sequence[ControlCandidate], budget: int
                   ) -> Tuple[ControlCandidate, ...]:
    """``budget`` candidates that span the product, not its first ``budget``.

    A frozen catalog's product is mostly near-duplicates: the same functions on
    the same scopes at neighbouring values.  Cutting the first *n* of it would
    hand the search every value of one axis and none of another.  So the cover
    groups the product by **shape** -- which functions on which scopes move --
    takes the shapes simplest-first, and inside each shape spreads its picks
    evenly over that shape's own value order, round-robin across shapes until
    the budget is spent.  Every shape the catalog admits is therefore
    represented before any shape is sampled twice.
    """
    rows = list(candidates)
    if budget <= 0 or len(rows) <= budget:
        return tuple(rows)
    shapes: Dict[Tuple[Tuple[str, str], ...], List[ControlCandidate]] = {}
    for item in rows:
        key = tuple(sorted((selection.function_id, selection.scope)
                           for selection in item.functions))
        shapes.setdefault(key, []).append(item)
    order = sorted(shapes, key=lambda key: (len(key), key))
    # Inside a shape, an evenly spaced walk over its members: first, last, then
    # the midpoints, so two picks from one shape are as far apart as possible.
    walks: Dict[Any, List[ControlCandidate]] = {}
    for key in order:
        members = shapes[key]
        picks: List[ControlCandidate] = []
        remaining = list(range(len(members)))
        while remaining:
            step = max(1, len(remaining) // max(1, (len(members) - len(picks))))
            index = remaining.pop(0)
            picks.append(members[index])
            remaining = remaining[step - 1:] + remaining[:step - 1] if step > 1 else remaining
        walks[key] = picks
    chosen: List[ControlCandidate] = []
    while len(chosen) < budget:
        progressed = False
        for key in order:
            if walks[key]:
                chosen.append(walks[key].pop(0))
                progressed = True
                if len(chosen) >= budget:
                    break
        if not progressed:
            break
    return tuple(replace(item, control_id=f"C{index + 1}")
                 for index, item in enumerate(chosen))


def deterministic_function_moves(catalog: FunctionCatalog,
                                 baselines: Optional[Mapping[str, str]] = None,
                                 *, compatibility: Optional[CompatibilityRules] = None,
                                 pairs: bool = True,
                                 dropped: Optional[List[str]] = None,
                                 ) -> Tuple[ControlCandidate, ...]:
    """Every single-function move over the catalog, then the pairwise joints.

    The deterministic ``C``: for each function, each scope and each policy
    value that is not the baseline, one candidate that selects only that
    function; then, when ``pairs``, every compatible pair of those moves on
    different axes.  Ordering is by function id, scope and the axis's own value
    order, so the enumeration is stable.  ``C0`` is added by
    :func:`validate_control_candidates`, not here.

    A pair the compatibility rules refuse is left out, and the reason is
    appended to ``dropped`` when one is supplied -- a candidate that disappears
    with no recorded cause looks exactly like one that was never enumerated.
    """
    compatibility = compatibility or CompatibilityRules()
    baseline = catalog.baselines(baselines)
    baseline.update({str(k): str(v) for k, v in dict(baselines or {}).items()})

    singles: List[FunctionSelection] = []
    for spec in sorted(catalog.functions, key=lambda item: item.function_id):
        for scope in spec.scopes:
            axis = spec.axis_for(scope)
            for value in spec.axis_values:
                if str(value) == baseline.get(axis):
                    continue
                singles.append(FunctionSelection(
                    function_id=spec.function_id, scope=scope,
                    policy={spec.axis_field: str(value)}))

    rows: List[ControlCandidate] = []
    for selection in singles:
        rows.append(ControlCandidate(
            control_id=f"C{len(rows) + 1}", functions=(selection,),
            applicability=(f"{selection.function_id} on {selection.scope}",),
            rationale="one function on its own"))
    if pairs:
        for index, left in enumerate(singles):
            for right in singles[index + 1:]:
                if left.scope == right.scope and left.function_id == right.function_id:
                    continue
                pair = (left, right)
                refused = compatibility.refusals(pair)
                if refused:
                    if dropped is not None:
                        dropped.append(_refusal_note(pair, refused))
                    continue
                rows.append(ControlCandidate(
                    control_id=f"C{len(rows) + 1}", functions=pair,
                    applicability=(f"{left.function_id} on {left.scope}",
                                   f"{right.function_id} on {right.scope}"),
                    rationale="two functions used together"))
    return tuple(rows)


def deterministic_controls(action_space: Mapping[str, Sequence[str]],
                           baselines: Optional[Mapping[str, str]] = None,
                           max_candidates: int = 12,
                           *, construction_policy: Optional[Mapping[str, Any]] = None,
                           ) -> ControlCandidates:
    """The baseline plus up to ``max_candidates`` single-axis moves, ordered by
    axis then by the axis's own value order.  No model, no randomness."""
    space = {str(axis): tuple(str(item) for item in values)
             for axis, values in dict(action_space).items()}
    baseline = {str(axis): str(value) for axis, value in dict(baselines or {}).items()}
    for axis, values in space.items():
        if axis not in baseline and values:
            baseline[axis] = values[0]

    moves: List[ControlCandidate] = []
    index = 0
    for axis in sorted(space):
        for value in space[axis]:
            if str(value) == baseline.get(axis):
                continue
            if len(moves) >= max(0, int(max_candidates)):
                break
            index += 1
            configuration = dict(baseline)
            configuration[axis] = str(value)
            moves.append(ControlCandidate(
                control_id=f"C{index}", configuration=configuration,
                effect_estimate={}, applicability=(f"single-axis move on {axis}",),
                interactions=""))
        if len(moves) >= max(0, int(max_candidates)):
            break

    policy = dict(construction_policy or {})
    policy.setdefault("maxCandidates", int(max_candidates))
    policy.setdefault("mustIncludeBaseline", True)
    candidates = ControlCandidates(
        candidates=tuple(moves), action_space=space, construction_policy=policy,
        provenance={"model": "deterministic", "fallback": None,
                    "rationale": "baseline plus single-axis moves"})
    cleaned, _ = validate_control_candidates(candidates, space, baseline)
    return cleaned


def deterministic_configuration(action_space: Mapping[str, Sequence[str]],
                                baselines: Optional[Mapping[str, str]],
                                current: Optional[Mapping[str, str]],
                                tried: Sequence[Mapping[str, str]] = (),
                                ) -> Optional[Dict[str, str]]:
    """The untried single-axis configuration closest to what is applied now.

    This is the basic monolith's fallback: it has no ``C`` to choose from, so
    it walks the same action space directly.
    """
    candidates = deterministic_controls(action_space, baselines,
                                        max_candidates=_axis_move_count(action_space, baselines))
    already = {tuple(sorted((str(k), str(v)) for k, v in dict(item).items()))
               for item in tried}
    applied = {str(k): str(v) for k, v in dict(current or {}).items()}
    remaining = [candidate for candidate in candidates.candidates
                 if candidate.signature not in already]
    if not remaining:
        return None
    remaining.sort(key=lambda candidate: (candidate.changes_from(applied),
                                          candidates.control_ids.index(candidate.control_id)))
    return dict(remaining[0].configuration)


def _axis_move_count(action_space: Mapping[str, Sequence[str]],
                     baselines: Optional[Mapping[str, str]]) -> int:
    space = {str(axis): tuple(str(item) for item in values)
             for axis, values in dict(action_space).items()}
    baseline = {str(axis): str(value) for axis, value in dict(baselines or {}).items()}
    for axis, values in space.items():
        if axis not in baseline and values:
            baseline[axis] = values[0]
    return sum(sum(1 for value in values if value != baseline.get(axis))
               for axis, values in space.items())


# --------------------------------------------------------------------------- #
# section 2.4 -- judgement, trials and the grid
# --------------------------------------------------------------------------- #


def observation_at_deadline(value: Any, deadline_ms: Any,
                            original_deadline_ms: Any = None) -> Any:
    """Select a ratio measured at D, never reuse a looser D as T0 evidence.

    Live windows retain every measured deadline under ``byDeadlineMs``. Legacy
    scalar observations only describe the original deadline; they cannot
    establish a separately relaxed one.
    """
    deadline = _as_float(deadline_ms)
    if deadline is None or deadline <= 0:
        return None
    if isinstance(value, Mapping):
        levels = value.get("byDeadlineMs", {})
        if not isinstance(levels, Mapping):
            return None
        value = next((ratio for key, ratio in levels.items()
                      if _same_value(key, deadline)), None)
    elif not _same_value(deadline, original_deadline_ms):
        return None
    ratio = _as_float(value)
    return ratio if ratio is not None and 0 <= ratio <= 1 else None


def _observed_requirement(observations: Mapping[str, Any], requirement: Any,
                          target: Target) -> Any:
    value = observations.get(requirement.observation_key)
    if requirement.kpi == KPI_DEADLINE_RATIO:
        return observation_at_deadline(
            value, target.deadline_for(requirement.req_id) or requirement.deadline_ms,
            requirement.deadline_ms)
    return value


def judge(kpis: Mapping[str, Any], target: Target,
          intents: Sequence[Intent]) -> Dict[str, str]:
    """Judge one measured KPI vector against one target.

    Deterministic executor code over measured numbers: ``PASS`` when the
    observation meets the target's threshold, ``FAIL`` when it does not,
    ``UNKNOWN`` when the observation is missing (a missing window can never
    establish a success -- ``exp_metrics.md`` section 2).
    """
    observations = dict(kpis or {})
    verdicts: Dict[str, str] = {}
    for intent in intents:
        requirement = intent.requirement
        req_id = requirement.req_id
        if req_id not in target.requirements:
            verdicts[req_id] = UNKNOWN
            continue
        threshold = target.requirements[req_id]
        observed = _observed_requirement(observations, requirement, target)
        if observed is None:
            verdicts[req_id] = UNKNOWN
            continue
        measured, wanted = _as_float(observed), _as_float(threshold)
        if measured is None or wanted is None:
            verdicts[req_id] = (PASS if _clean(observed) == _clean(threshold) else FAIL)
            continue
        if requirement.op == _FLOOR:
            verdicts[req_id] = PASS if measured >= wanted - _TOLERANCE else FAIL
        elif requirement.op == _CEILING:
            verdicts[req_id] = PASS if measured <= wanted + _TOLERANCE else FAIL
        else:
            verdicts[req_id] = PASS if abs(measured - wanted) <= _TOLERANCE else FAIL
    return verdicts


def verdict_of(per_requirement: Mapping[str, str]) -> str:
    """One target's cell: ``PASS`` only when every requirement passed."""
    values = list(dict(per_requirement or {}).values())
    if not values:
        return UNKNOWN
    if any(value == FAIL for value in values):
        return FAIL
    if any(value == UNKNOWN for value in values):
        return UNKNOWN
    return PASS


def _compare(observed: Any, threshold: Any, op: str) -> str:
    if observed is None:
        return UNKNOWN
    measured, wanted = _as_float(observed), _as_float(threshold)
    if measured is None or wanted is None:
        return PASS if _clean(observed) == _clean(threshold) else FAIL
    if op == _FLOOR:
        return PASS if measured >= wanted - _TOLERANCE else FAIL
    if op == _CEILING:
        return PASS if measured <= wanted + _TOLERANCE else FAIL
    return PASS if abs(measured - wanted) <= _TOLERANCE else FAIL


def judge_target(kpis: Mapping[str, Any], target: Target,
                 authorization: Authorization) -> Dict[str, str]:
    """Judge one measured KPI vector against one target, using the
    authorization's own KPI keys and operators.

    The same deterministic arithmetic as :func:`judge`, without needing the
    intents: the executor's evaluator, ``observed_best`` and ``kpi_gaps`` all
    work off the contract alone.  A joint condition the vector breaks fails
    every target, because a target that rides over a protected minimum was
    never authorized.
    """
    observations = dict(kpis or {})
    verdicts: Dict[str, str] = {}
    for req_id, entry in authorization.requirements.items():
        if req_id not in target.requirements:
            verdicts[req_id] = UNKNOWN
            continue
        observed = _observed_requirement(observations, entry, target)
        if observed is None:
            # The same rule ``judge`` has always used, and this twin did not:
            # a requirement whose KPI the window never carried is UNKNOWN, not
            # FAIL.  Both block a PASS, so ``observed_best`` was safe either
            # way -- but everything that counts shortfalls read "the UE missed
            # its floor" where the truth was "nobody measured that UE"
            # (handoff 2026-09-18 section 4.2).  A missing window can never
            # establish a success and must not manufacture a failure.
            verdicts[req_id] = UNKNOWN
            continue
        verdicts[req_id] = _compare(observed, target.requirements[req_id], entry.op)
    for index, condition in enumerate(authorization.joint_conditions):
        holds = condition.holds(observations.get(condition.kpi))
        verdicts[f"joint[{index}]:{condition.kpi}"] = (
            UNKNOWN if holds is None else (PASS if holds else FAIL))
    return verdicts


@dataclass(frozen=True)
class Observation:
    """One measured window: what was applied, what came out, and until when the
    numbers may still be used (contract v2 section 6)."""

    control_id: str = ""
    trial_index: int = 0
    configuration: Dict[str, str] = field(default_factory=dict)
    functions: Tuple[Dict[str, Any], ...] = ()
    kpis: Dict[str, Any] = field(default_factory=dict)
    observed_at: str = ""
    window_end: str = ""
    valid_until: str = ""
    valid: bool = True
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "control_id", _clean(self.control_id))
        object.__setattr__(self, "configuration",
                           {str(k): str(v) for k, v in dict(self.configuration or {}).items()})
        object.__setattr__(self, "functions",
                           tuple(dict(item) for item in self.functions or ()))
        object.__setattr__(self, "kpis", dict(self.kpis or {}))
        object.__setattr__(self, "trial_index", int(self.trial_index or 0))

    @property
    def reference(self) -> str:
        return f"trial:{self.trial_index}"

    def to_record(self) -> Dict[str, Any]:
        return {"controlId": self.control_id, "trialIndex": self.trial_index,
                "configuration": dict(self.configuration),
                "functions": [dict(item) for item in self.functions],
                "kpis": dict(self.kpis), "observedAt": self.observed_at,
                "windowEnd": self.window_end, "validUntil": self.valid_until,
                "valid": bool(self.valid), "reason": self.reason}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Observation":
        record = dict(record or {})
        window = dict(record.get("window", {}) or {})
        return cls(control_id=record.get("controlId", ""),
                   trial_index=int(record.get("trialIndex", 0) or 0),
                   configuration=record.get("configuration", {}) or {},
                   functions=tuple(record.get("functions", ()) or ()),
                   kpis=record.get("kpis", {}) or {},
                   observed_at=_clean(record.get("observedAt")
                                      or record.get("appliedAt")),
                   window_end=_clean(record.get("windowEnd") or window.get("end")),
                   valid_until=_clean(record.get("validUntil")),
                   valid=bool(record.get("valid", window.get("valid", True))),
                   reason=_clean(record.get("reason") or window.get("reason")))

    @classmethod
    def from_trial(cls, trial: "Trial") -> "Observation":
        window = dict(trial.window or {})
        return cls(control_id=trial.control_id, trial_index=trial.trial_index,
                   configuration=dict(trial.configuration),
                   functions=tuple(dict(item) for item in
                                   (trial.decision.get("functions") or ())),
                   kpis=dict(trial.kpis),
                   observed_at=_clean(window.get("start") or trial.applied_at),
                   window_end=_clean(window.get("end")),
                   valid_until=_clean(window.get("validUntil")),
                   valid=trial.observation_valid,
                   reason=_clean(trial.observation_validity["reason"]
                                 or window.get("reason")))


def _observations_of(source: Any) -> Tuple[Observation, ...]:
    if source is None:
        return ()
    if isinstance(source, Grid):
        return tuple(Observation.from_trial(trial) for trial in source.trials)
    rows: List[Observation] = []
    for item in source:
        if isinstance(item, Observation):
            rows.append(item)
        elif isinstance(item, Trial):
            rows.append(Observation.from_trial(item))
        elif isinstance(item, Mapping):
            rows.append(Observation.from_record(item))
    return tuple(rows)


def omega_targets(contract: TargetContract) -> Tuple[Target, ...]:
    """The owners' whole authorized grid, derived from the authorization.

    Every arm has to be judged on the same Omega, never on the ``T`` it
    happened to form: the basic-monolith arm carries all of Omega while the
    three-agent and internal arms carry the handful their Target step listed,
    so a PASS count taken over ``contract.targets`` compares different
    denominators (handoff 2026-09-18 section 4.3).

    ``expand_targets`` already builds exactly this grid -- ``from_compact``
    calls it and *then* narrows it down to the arm's selection -- so this is
    the same arithmetic the contract was cut from, not a second definition.
    An authorization that cannot expand (an operator mistake, too many steps)
    falls back to what the contract carries rather than taking a sitting down:
    a narrower comparison is still honest, a crash is not.
    """
    try:
        return expand_targets(contract.authorization).targets
    except TargetValidationError:
        return contract.targets


def _p1_of(target: Any, authorization: Any, contract: TargetContract) -> Dict[str, Any]:
    """The target's P1 key: the owner vector in the recorded order, and its rank.

    The rank is an **order** encoding (0 = every owner at its original level);
    it is not a cardinal amount of concession, so no percentage may be computed
    from it.  A workload the encoding was not verified against gets ``None``
    rather than an invented number.
    """
    from assurance.coordination.preference import p1_vector, p_rank
    order = tuple(contract.preference.owner_priority) or (
        "operator-gnb2", "ue1-video", "ue2-map", "ue3-incumbent")
    record = target.to_record() if hasattr(target, "to_record") else dict(target)
    auth = (authorization.to_record() if hasattr(authorization, "to_record")
            else dict(authorization))
    try:
        vector = p1_vector(record, auth, order)
        rank = p_rank(record, auth, order)
    except (ValueError, KeyError, TypeError, ZeroDivisionError):
        return {"ownerPriority": list(order), "vector": None, "rank": None}
    return {"ownerPriority": list(order),
            "vector": [str(value) for value in vector], "rank": rank}


def omega_contract(contract: TargetContract) -> TargetContract:
    """The same grid as :func:`omega_targets`, as a contract that can be judged against.

    2026-09-23: the sitting needs a *contract* for ``Trial.judge_against``, not a
    tuple, because judging walks ``contract.targets`` -- and ``targets`` is a
    property over ``t0`` + ``alternatives``, so it cannot be substituted with
    ``dataclasses.replace``.  Same expansion, same fallback: an authorization
    that cannot expand keeps the contract it was given, because a narrower
    comparison is still honest and a crash is not.
    """
    try:
        return expand_targets(contract.authorization)
    except TargetValidationError:
        return contract


#: 선택기에게 보이는 Omega 목표 중 **이 방식의 T 에 없는** 것에 붙는 접두.
OMEGA_ONLY_PREFIX = "omega:"


def _level_signature(target: Any) -> Tuple[Tuple[Any, ...], Tuple[Any, ...]]:
    """자리번호가 아니라 **요구 벡터**로 목표를 짝짓는 열쇠."""
    return (tuple(sorted(dict(target.levels).items())),
            tuple(sorted(dict(target.deadline_levels).items())))


def shown_target_ids(evaluation: TargetContract, formed: TargetContract) -> Dict[str, str]:
    """Omega 목표 id → 선택기에게 보여 줄 id (2026-09-23, codex 감사 R-1).

    형성된 T 는 `T1..Tn` 으로 **재번호**되고 Omega 는 격자 자신의 번호를 쓴다 --
    철자만 같고 서로 다른 요구 벡터다.  실측: 선택된 `T1` 은 `I1g.r1=1`, Omega 의
    `T1` 은 `I3g.r1=1`.  그런데 선택기는 `input.target_contract`(T 이름)와 Omega 로
    채점한 판정·최고·격차(Omega 이름)를 **한 프롬프트에서** 받았고, 답은 T 이름으로
    검증된다.  다른 벡터의 PASS 를 자기 목표의 PASS 로 읽거나, Omega 이름으로 답했다가
    거절될 수 있었다.

    그래서 보여 줄 때만 번역한다:
    - T 에 **같은 벡터**가 있으면 그 T 의 id (선택기가 아는 이름, 답할 수 있는 이름)
    - 없으면 ``omega:<Omega id>`` -- 결정 §2 대로 T 밖의 달성도 **숨기지 않되**,
      T 의 이름과 절대 안 겹치고 의도 목표로 답할 수 없음이 이름에 드러난다.

    기록(격자 칸·success 키)은 번역하지 않는다 -- 분석은 벡터로 짝짓는다.
    """
    by_signature = {_level_signature(target): target.target_id for target in formed.targets}
    return {target.target_id: by_signature.get(_level_signature(target),
                                               f"{OMEGA_ONLY_PREFIX}{target.target_id}")
            for target in evaluation.targets}


def observed_best(source: Any, contract: TargetContract) -> Optional[Dict[str, Any]]:
    """``input.observed_best``: the cheapest target the **valid** observations
    actually support, and what supported it (contract v2 section 5).

    ``None`` when no observation establishes any target -- which is not the
    same as a failure: a window that never came back is ``UNKNOWN`` and can
    never establish a success.
    """
    authorization = contract.authorization
    # Judge over the owners' whole Omega, not over the T this arm happened to
    # form.  Walking ``contract.targets`` made the three-agent and internal
    # arms blind to an attainment they had not listed, so their PASS counts
    # were never comparable with basic-monolith's, which carries all of Omega
    # (handoff 2026-09-18 section 4.3).  The grid belongs to the owners.
    # 2026-09-22 (codex 감사 #10): `target_id` 로 비교하고 있었다.  그런데 선택된 T 는
    # `from_compact` 에서 `T1..Tn` 으로 **재번호**되고(2239행) omega 는 grid 자신의 번호를
    # 쓴다 -- 철자만 같고 서로 다른 요구 벡터다.  그래서 준비하지도 않은 목표를
    # "준비했다" 고 적을 수 있었다.  자리번호가 아니라 **레벨 벡터**로 짝지어야 한다.
    def _signature(target: Any) -> Tuple[Tuple[Any, ...], Tuple[Any, ...]]:
        return (tuple(sorted(dict(target.levels).items())),
                tuple(sorted(dict(target.deadline_levels).items())))

    prepared = {_signature(target) for target in contract.targets}
    omega = omega_targets(contract)
    best: Optional[Tuple[Tuple[float, float, float], Dict[str, Any]]] = None
    for observation in _observations_of(source):
        if not observation.valid:
            continue
        for target in omega:
            verdicts = judge_target(observation.kpis, target, authorization)
            if verdict_of(verdicts) != PASS:
                continue
            cost = cost_of(target, authorization)
            quality = concession_of(target, authorization)
            p1 = _p1_of(target, authorization, contract)
            # 2026-09-23 (codex 감사 #9): P1 을 **싣기만** 하고 고르기는 가중 비용으로
            # 했다.  유지 판정의 역사 최고는 P1 으로 정해지므로, 두 순서가 갈리는
            # 목표쌍(실측 144 중 20건)에서 선택기가 보는 최고와 유지가 비교하는 최고가
            # **서로 다른 목표**가 된다.  결정 §3.1 이 P1 을 비교 키로 못박았으니 고르는
            # 것도 P1 이다.  인코딩이 검증되지 않은 워크로드(rank None)는 종전 키로
            # 떨어진다 -- 없는 순서를 지어내지 않는다.
            rank = p1.get("rank")
            key = ((0, rank) if rank is not None else (1, 0),
                   cost, quality["max"], quality["mean"])
            found = {"targetId": target.target_id, "cost": cost,
                     "concession": {"perOwner": dict(quality["perOwner"]),
                                    "mean": quality["mean"], "max": quality["max"]},
                     # **P1 을 함께 싣는다** (2026-09-23 결정).  `cost` 는 가중 스칼라라
                     # 같은 값이 서로 다른 P1 순위를 가질 수 있다 -- 실제 144목표에서
                     # 20건 있다.  선택기가 무엇으로 비교해야 하는지 모호하면 안 된다.
                     "p1": p1,
                     "controlId": observation.control_id,
                     "trialIndex": observation.trial_index,
                     "observationRefs": [observation.reference],
                     # Diagnostic only: whether this arm had listed the target
                     # it turned out to meet.  Never a filter.
                     "inPreparedT": _signature(target) in prepared,
                     "kpis": dict(observation.kpis)}
            if best is None or key < best[0]:
                best = (key, found)
            elif key == best[0]:
                refs = best[1]["observationRefs"]
                if observation.reference not in refs:
                    refs.append(observation.reference)
    return None if best is None else best[1]


def kpi_gaps(latest_kpis: Optional[Mapping[str, Any]], contract: TargetContract,
             next_target: Optional[Target] = None) -> Optional[Dict[str, Any]]:
    """``input.kpi_gaps``: how far the measured vector is from ``T0`` and from
    the next preferred target (contract v2 section 5).

    ``None`` when there is no KPI yet -- the models are told "no measurement",
    not "a gap of zero".  Owner, scope, KPI and unit are preserved so the
    numbers stay attributable.
    """
    observations = dict(latest_kpis or {})
    if not observations:
        return None
    authorization = contract.authorization
    if next_target is None:
        next_target = contract.cheapest_unsatisfied(observations, include_t0=False)

    def shortfall(entry: AuthorizedRequirement, observed: Any,
                  threshold: Any) -> Optional[float]:
        measured, wanted = _as_float(observed), _as_float(threshold)
        if measured is None or wanted is None:
            return None
        if entry.op == _FLOOR:
            return round(max(0.0, wanted - measured), 9)
        if entry.op == _CEILING:
            return round(max(0.0, measured - wanted), 9)
        return 0.0 if abs(measured - wanted) <= _TOLERANCE else None

    rows: Dict[str, Any] = {}
    for req_id, entry in authorization.requirements.items():
        observed = _observed_requirement(observations, entry, contract.t0)
        row: Dict[str, Any] = {"owner": entry.owner, "scope": entry.scope,
                               "kpi": entry.kpi, "unit": entry.unit,
                               "op": entry.op, "observed": observed}
        if entry.has_deadline:
            row["observedByDeadlineMs"] = observations.get(entry.observation_key)
        t0_value = contract.t0.requirements.get(req_id)
        row["againstT0"] = {"targetId": contract.t0.target_id, "threshold": t0_value,
                            "shortfall": shortfall(entry, observed, t0_value),
                            "verdict": _compare(observed, t0_value, entry.op)}
        if next_target is not None and next_target.target_id != contract.t0.target_id:
            value = next_target.requirements.get(req_id)
            observed = _observed_requirement(observations, entry, next_target)
            row["againstNext"] = {"targetId": next_target.target_id,
                                  "threshold": value,
                                  "shortfall": shortfall(entry, observed, value),
                                  "verdict": _compare(observed, value, entry.op)}
        else:
            row["againstNext"] = None
        rows[req_id] = row
    conditions = []
    for condition in authorization.joint_conditions:
        observed = observations.get(condition.kpi)
        holds = condition.holds(observed)
        conditions.append({"kpi": condition.kpi, "op": condition.op,
                           "value": condition.value, "unit": condition.unit,
                           "owner": condition.owner, "observed": observed,
                           "holds": holds})
    return {"perRequirement": rows, "jointConditions": conditions,
            "nextTargetId": next_target.target_id if next_target is not None else None}


#: 커널이 이렇게 닫은 창은 그 설정의 유효한 관측이 아니다 (핸드오프 §4.2).
#: ``INDETERMINATE``·``INVALID`` 는 측정 불충분, 나머지는 실행이 제안대로 서지 않았다.
_OBSERVATION_VOID_OUTCOMES = frozenset({
    "INDETERMINATE", "INVALID", "EXEC_ERROR", "OPERATOR_ABORTED", "SAFETY_STOPPED",
    "RECOVERY_FAILED"})


@dataclass
class Trial:
    """One admitted live evaluation and everything it produced."""

    trial_index: int
    proposed_target_id: str = ""
    control_id: str = ""
    configuration: Dict[str, str] = field(default_factory=dict)
    catalog_candidate_id: str = ""
    decision_latency_ms: float = 0.0
    applied_at: str = ""
    window: Dict[str, Any] = field(default_factory=dict)
    kpis: Dict[str, Any] = field(default_factory=dict)
    verdicts: Dict[str, Dict[str, str]] = field(default_factory=dict)
    success: Dict[str, bool] = field(default_factory=dict)
    kernel: Dict[str, Any] = field(default_factory=dict)
    #: 이 시행이 돌 때의 UE 신원 세대.  재등록이 나면 올라간다.  retention 이
    #: 게이트웨이의 믿음만으로 이전 성공을 자격으로 쓰지 못하게 하는 데 쓴다
    #: (2026-09-22 codex 감사).
    rolled_back: bool = False
    decision: Dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    @property
    def window_valid(self) -> bool:
        return bool(self.window.get("valid", True))

    @property
    def observation_validity(self) -> Dict[str, Any]:
        """핸드오프 2026-09-18 §4.2: 이 창이 **이 설정의 유효한 관측**인가.

        수집기의 창 판정(``window.valid``)만으로는 모자랐다 -- BM 판 20260917T222430
        시행 1 은 커널이 ``INDETERMINATE`` 로 닫았는데 ``success`` 에 부분 PASS 8 개가
        남았다.  커널이 관측 불충분·실행 실패로 닫은 창, 적용이 어긋난 창
        (``PARTIAL_APPLY``)은 그 설정의 관측이 아니다.  rollback 자체는 관측을 무효로
        만들지 않는다 -- 창은 rollback 전에 재였다.  초기 측정(커널 없음)은 창 판정만 본다.
        """
        if not self.window_valid:
            return {"valid": False,
                    "reason": "window: " + (_clean(self.window.get("reason")) or "invalid")}
        kernel = dict(self.kernel or {})
        outcome = _clean(kernel.get("outcome"))
        if outcome in _OBSERVATION_VOID_OUTCOMES:
            return {"valid": False, "reason": f"kernel outcome {outcome}"}
        if _clean(kernel.get("stopReason")) == "PARTIAL_APPLY":
            return {"valid": False,
                    "reason": "PARTIAL_APPLY: the measured configuration is not the proposed one"}
        missing = self.missing_requirements
        if missing:
            # §4.2·§6.2(3A 20260918T040406 시행 4): 필수 UE/KPI 가 빠진 창은 그 요구를
            # UNKNOWN 으로 두고, joint attainment 나 재사용 가능한 관측으로 인정하지 않는다.
            # 다른 요구의 확실한 미달은 ``verdicts`` 에 그대로 남는다.
            return {"valid": False, "reason": "required KPI missing: " + ", ".join(missing)}
        return {"valid": True, "reason": ""}

    @property
    def missing_requirements(self) -> List[str]:
        """어느 목표에서든 판정이 UNKNOWN 인 요구조건 -- 그 KPI 를 이 창이 싣지 않았다."""
        return sorted({req for verdicts in self.verdicts.values()
                       for req, value in dict(verdicts).items() if value == UNKNOWN})

    @property
    def observation_valid(self) -> bool:
        return bool(self.observation_validity["valid"])

    @property
    def execution_status(self) -> Dict[str, Any]:
        """§4.2 의 첫째 축: 적용·되읽기·정착·커널 종료·rollback -- 달성과 따로 남긴다."""
        kernel = dict(self.kernel or {})
        execution = dict(kernel.get("execution") or {})
        recovery = dict(kernel.get("recovery") or {})
        return {"terminalState": kernel.get("terminalState"),
                "outcome": kernel.get("outcome"),
                "stopReason": kernel.get("stopReason"),
                "applied": bool(execution.get("appliedAt")) if kernel else None,
                "partialApply": execution.get("partialApply"),
                "rolledBack": bool(self.rolled_back),
                "recoveryUnresolved": recovery.get("unresolved")}

    @property
    def attainment(self) -> Dict[str, Any]:
        """§4.2 의 셋째 축: 유효한 증거가 목표의 요구조건을 전부 만족하는가.

        관측이 무효면 ``UNKNOWN`` 이다 -- 0 으로 채우지도, 부분 PASS 를 달성으로 치지도
        않는다.  요구조건별 판정(``verdicts``)의 확실한 미달은 그대로 남는다.
        """
        if not self.observation_valid:
            return {"status": UNKNOWN, "targets": [],
                    "missingRequirements": self.missing_requirements}
        passed = sorted(target for target, ok in self.success.items() if ok)
        return {"status": "ATTAINED" if passed else "NOT_ATTAINED", "targets": passed}

    def cell(self, target_id: str) -> str:
        """The board cell this trial wrote for one target."""
        if str(target_id) not in self.verdicts:
            return UNKNOWN
        if not self.observation_valid:
            return UNKNOWN
        return verdict_of(self.verdicts[str(target_id)])

    def judge_against(self, contract: TargetContract,
                      intents: Sequence[Intent]) -> "Trial":
        """Fill ``verdicts`` and ``success`` for every authorized target."""
        for target in contract.targets:
            self.verdicts[target.target_id] = judge(self.kpis, target, intents)
        # 유효성은 판정이 다 나온 뒤에 본다 -- 빠진 KPI 는 판정의 UNKNOWN 으로 드러난다.
        valid = self.observation_valid
        for target_id, per_requirement in self.verdicts.items():
            self.success[target_id] = valid and verdict_of(per_requirement) == PASS
        return self

    def to_record(self) -> Dict[str, Any]:
        return {"trialIndex": self.trial_index,
                "proposedTargetId": self.proposed_target_id,
                "controlId": self.control_id,
                "configuration": dict(self.configuration),
                "catalogCandidateId": self.catalog_candidate_id,
                "decisionLatencyMs": self.decision_latency_ms,
                "appliedAt": self.applied_at,
                "elapsedMs": self.elapsed_ms,
                "window": dict(self.window),
                "kpis": dict(self.kpis),
                "verdicts": {target: dict(values)
                             for target, values in self.verdicts.items()},
                "success": dict(self.success),
                "executionStatus": self.execution_status,
                "observationValidity": self.observation_validity,
                "attainment": self.attainment,
                "kernel": dict(self.kernel),
                "rolledBack": bool(self.rolled_back),
                "decision": dict(self.decision)}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Trial":
        record = dict(record or {})
        return cls(trial_index=int(record.get("trialIndex", 0)),
                   proposed_target_id=_clean(record.get("proposedTargetId")),
                   control_id=_clean(record.get("controlId")),
                   configuration=dict(record.get("configuration", {}) or {}),
                   catalog_candidate_id=_clean(record.get("catalogCandidateId")),
                   decision_latency_ms=float(record.get("decisionLatencyMs", 0.0) or 0.0),
                   applied_at=_clean(record.get("appliedAt")),
                   window=dict(record.get("window", {}) or {}),
                   kpis=dict(record.get("kpis", {}) or {}),
                   verdicts={str(k): dict(v) for k, v in
                             dict(record.get("verdicts", {}) or {}).items()},
                   success={str(k): bool(v) for k, v in
                            dict(record.get("success", {}) or {}).items()},
                   kernel=dict(record.get("kernel", {}) or {}),
                   rolled_back=bool(record.get("rolledBack", False)),
                   decision=dict(record.get("decision", {}) or {}),
                   elapsed_ms=float(record.get("elapsedMs", 0.0) or 0.0))


@dataclass(frozen=True)
class BestAttained:
    """The successful target minimizing ``(D_max, D_mean)`` at the cutoff."""

    target_id: str
    control_id: str
    trial_index: int
    elapsed_ms: float
    concession: Dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> Dict[str, Any]:
        quality = dict(self.concession)
        return {"targetId": self.target_id, "controlId": self.control_id,
                "trialIndex": self.trial_index, "elapsedMs": self.elapsed_ms,
                "concession": {"perOwner": dict(quality.get("perOwner", {})),
                               "perRequirement": dict(quality.get("perRequirement", {})),
                               "mean": quality.get("mean", 0.0),
                               "max": quality.get("max", 0.0)}}


@dataclass(frozen=True)
class TrajectoryDecision:
    """One cell of the board: which target, applied by which control."""

    target_id: str
    control_id: str
    rationale: str = ""

    def to_record(self) -> Dict[str, Any]:
        return {"targetId": self.target_id, "controlId": self.control_id,
                "rationale": self.rationale}


@dataclass(frozen=True)
class BasicDecision:
    """The basic monolith's answer: one configuration and what it aims at.

    It never sees our ``T``/``C``, so it answers with real values, not ids;
    the executor maps the configuration onto a catalog candidate and judges
    the result with exactly the same predicates as every other method.
    """

    configuration: Dict[str, str] = field(default_factory=dict)
    requirements: Dict[str, Any] = field(default_factory=dict)
    requirement_records: Tuple[Dict[str, Any], ...] = ()
    rationale: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "configuration",
                           {str(k): str(v) for k, v in dict(self.configuration).items()})
        object.__setattr__(self, "requirements", dict(self.requirements or {}))
        object.__setattr__(self, "requirement_records",
                           tuple(dict(item) for item in self.requirement_records))

    def as_target(self, target_id: str = "B0") -> Target:
        return Target(target_id=target_id, requirements=dict(self.requirements))

    def to_record(self) -> Dict[str, Any]:
        return {"configuration": dict(self.configuration),
                "requirements": [dict(item) for item in self.requirement_records]
                or dict(self.requirements),
                "rationale": self.rationale}


@dataclass
class Grid:
    """``T`` rows by ``C`` columns, filled one whole column per trial."""

    contract: Optional[TargetContract] = None
    controls: Optional[ControlCandidates] = None
    trials: List[Trial] = field(default_factory=list)
    cells: Dict[str, Dict[str, str]] = field(default_factory=dict)

    def record(self, trial: Trial) -> Trial:
        """Write one trial's whole column into the grid."""
        self.trials.append(trial)
        for target_id in trial.verdicts:
            self.cells.setdefault(str(target_id), {})[trial.control_id] = trial.cell(target_id)
        return trial

    def cell(self, target_id: str, control_id: str) -> Optional[str]:
        return self.cells.get(str(target_id), {}).get(str(control_id))

    def tried_controls(self) -> Tuple[str, ...]:
        seen: List[str] = []
        for trial in self.trials:
            if trial.control_id and trial.control_id not in seen:
                seen.append(trial.control_id)
        return tuple(seen)

    def untried_controls(self, controls: Optional[ControlCandidates] = None
                         ) -> Tuple[str, ...]:
        controls = controls or self.controls
        if controls is None:
            return ()
        tried = set(self.tried_controls())
        return tuple(control_id for control_id in controls.control_ids
                     if control_id not in tried)

    def tried_configurations(self) -> Tuple[Dict[str, str], ...]:
        return tuple(dict(trial.configuration) for trial in self.trials
                     if trial.configuration)

    def successes(self) -> Tuple[Tuple[str, Trial], ...]:
        found: List[Tuple[str, Trial]] = []
        for trial in self.trials:
            for target_id, ok in trial.success.items():
                if ok:
                    found.append((str(target_id), trial))
        return tuple(found)

    def best_attained(self, contract: Optional[TargetContract] = None
                      ) -> Optional[BestAttained]:
        """The satisfied target the owner's preference ranks first.

        Its record carries that target's ``D_o`` per owner alongside ``max`` and
        ``mean``, which is what contract v2 section 4 asks to be reported.
        """
        contract = contract or self.contract
        if contract is None:
            return None
        best: Optional[Tuple[Tuple[float, ...], BestAttained]] = None
        for target_id, trial in self.successes():
            target = contract.target(target_id)
            if target is None:
                continue
            quality = concession_of(target, contract.authorization)
            key = preference_key(target, contract.authorization,
                                 contract.preference) + (trial.trial_index,)
            found = BestAttained(target_id=target_id, control_id=trial.control_id,
                                 trial_index=trial.trial_index,
                                 elapsed_ms=trial.elapsed_ms, concession=quality)
            if best is None or key < best[0]:
                best = (key, found)
        return None if best is None else best[1]

    def first_success(self) -> Optional[Trial]:
        for trial in self.trials:
            if any(trial.success.values()):
                return trial
        return None

    def to_record(self) -> Dict[str, Any]:
        return {"targets": list(self.contract.target_ids) if self.contract else [],
                "controls": list(self.controls.control_ids) if self.controls else [],
                "cells": {target: dict(row) for target, row in self.cells.items()}}


def deterministic_trajectory(contract: TargetContract, controls: ControlCandidates,
                             grid: Grid,
                             applied_configuration: Optional[Mapping[str, str]] = None,
                             ) -> Optional[TrajectoryDecision]:
    """The untried control with the fewest axis changes from what is applied,
    paired with the lowest-concession target that control has not satisfied."""
    untried = grid.untried_controls(controls)
    if not untried:
        return None
    applied = {str(k): str(v) for k, v in dict(applied_configuration or {}).items()}
    order = {control_id: index for index, control_id in enumerate(controls.control_ids)}

    def cost(control_id: str) -> Tuple[int, int]:
        candidate = controls.candidate(control_id)
        changes = candidate.changes_from(applied) if candidate is not None else 0
        return (changes, order.get(control_id, 0))

    control_id = min(untried, key=cost)
    for target in contract.ranked():
        if grid.cell(target.target_id, control_id) != PASS:
            return TrajectoryDecision(
                target_id=target.target_id, control_id=control_id,
                rationale=("fewest authorized concessions on the untried control "
                           "closest to the applied configuration"))
    return TrajectoryDecision(target_id=contract.t0.target_id, control_id=control_id,
                              rationale="untried control closest to the applied configuration")


# --------------------------------------------------------------------------- #
# section 2.5 -- the episode record
# --------------------------------------------------------------------------- #


@dataclass
class EpisodeRecord:
    """One run of one method on one intent set under one condition.

    Deliberately thin: the executor builds it from parts it already has, and
    ``to_record()`` is the JSON the metrics and figures read.
    """

    episode_id: str = ""
    method: str = ""
    session_mode: str = "MOCK"
    condition: Dict[str, Any] = field(default_factory=dict)
    block: int = 0
    repetition: int = 0
    models: Dict[str, Any] = field(default_factory=dict)
    intents: Tuple[Intent, ...] = ()
    contract: Optional[TargetContract] = None
    controls: Optional[ControlCandidates] = None
    budget: Dict[str, Any] = field(default_factory=dict)
    timing: Dict[str, Any] = field(default_factory=dict)
    calls: List[Dict[str, Any]] = field(default_factory=list)
    trials: List[Trial] = field(default_factory=list)
    service_trace: List[Dict[str, Any]] = field(default_factory=list)
    best_attained: Optional[BestAttained] = None
    retained: Dict[str, Any] = field(default_factory=dict)
    first_success: Dict[str, Any] = field(default_factory=dict)
    #: 유지 판정 한 줄 per 시행 (2026-09-23 결정 §3).  **역사적 달성과 성공한 유지를
    #: 따로** 싣는다: `performanceQualifies` 는 P1 비교가 통과했는지, `retain` 은 그
    #: 설정이 실제로 남았는지다.  결론 뒤 철회되면 `withdrawnAfterConclusion` 이 붙는다.
    retention_decisions: List[Dict[str, Any]] = field(default_factory=list)
    #: 세는 방식을 바꾸는 두 정책, 판이 실제로 무엇을 켜고 돌았는지 (2026-09-23).
    formal_reference_trial: bool = False
    retain_on_improvement: bool = False
    t0_success: bool = False
    termination: Dict[str, Any] = field(default_factory=dict)
    resource_cost: Dict[str, Any] = field(default_factory=dict)
    #: contract v2 section 6: how each KPI kind was observed, and for how long
    #: the answer stayed usable.  The metrics already read this key.
    measurement_rules: Dict[str, Any] = field(default_factory=dict)
    #: contract v2 section 2.2: what the checklist and the Target agent asked
    #: the operator, what came back, and how many rounds it took.
    intake: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = EPISODE_SCHEMA

    def to_record(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "episodeId": self.episode_id,
            "method": self.method,
            "sessionMode": self.session_mode,
            "condition": dict(self.condition),
            "block": int(self.block),
            "repetition": int(self.repetition),
            "models": dict(self.models),
            "intents": [intent.to_record() for intent in self.intents],
            "T": self.contract.to_record() if self.contract is not None else None,
            "C": self.controls.to_record() if self.controls is not None else None,
            "budget": dict(self.budget),
            "timing": dict(self.timing),
            "calls": [dict(call) for call in self.calls],
            "trials": [trial.to_record() for trial in self.trials],
            "serviceTrace": [dict(sample) for sample in self.service_trace],
            "bestAttained": (self.best_attained.to_record()
                             if self.best_attained is not None else None),
            "retained": dict(self.retained),
            "firstSuccess": dict(self.first_success),
            "retentionDecisions": [dict(row) for row in self.retention_decisions],
            # 2026-09-23: 이 두 정책은 **세는 방식을 바꾼다** -- 기준 관측을 N_max 에
            # 세는지, 충족한 설정을 남기는지.  `condition.name` 은 코퍼스 이름이라
            # 이것을 말해 주지 못했고, 켜고 돈 판과 끄고 돈 판이 기록만 보고는
            # 구분되지 않았다(원장의 `version` 밖에 없었다).  판이 스스로 적는다.
            "formalReferenceTrial": bool(self.formal_reference_trial),
            "retainOnImprovement": bool(self.retain_on_improvement),
            "t0Success": bool(self.t0_success),
            "termination": dict(self.termination),
            "resourceCost": dict(self.resource_cost),
            "measurementRules": dict(self.measurement_rules),
            "intake": dict(self.intake),
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_record(), indent=indent, sort_keys=False)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "EpisodeRecord":
        record = dict(record or {})
        contract = record.get("T")
        controls = record.get("C")
        best = record.get("bestAttained")
        return cls(
            episode_id=_clean(record.get("episodeId")),
            method=_clean(record.get("method")),
            session_mode=_clean(record.get("sessionMode")) or "MOCK",
            condition=dict(record.get("condition", {}) or {}),
            block=int(record.get("block", 0) or 0),
            repetition=int(record.get("repetition", 0) or 0),
            models=dict(record.get("models", {}) or {}),
            intents=tuple(Intent.from_record(item)
                          for item in record.get("intents", ()) or ()),
            contract=TargetContract.from_record(contract) if contract else None,
            controls=ControlCandidates.from_record(controls) if controls else None,
            budget=dict(record.get("budget", {}) or {}),
            timing=dict(record.get("timing", {}) or {}),
            calls=[dict(item) for item in record.get("calls", ()) or ()],
            trials=[Trial.from_record(item) for item in record.get("trials", ()) or ()],
            service_trace=[dict(item) for item in record.get("serviceTrace", ()) or ()],
            best_attained=(BestAttained(
                target_id=_clean(best.get("targetId")),
                control_id=_clean(best.get("controlId")),
                trial_index=int(best.get("trialIndex", 0) or 0),
                elapsed_ms=float(best.get("elapsedMs", 0.0) or 0.0),
                concession=dict(best.get("concession", {}) or {})) if best else None),
            retained=dict(record.get("retained", {}) or {}),
            first_success=dict(record.get("firstSuccess", {}) or {}),
            retention_decisions=[dict(row) for row in
                                 (record.get("retentionDecisions") or [])],
            formal_reference_trial=bool(record.get("formalReferenceTrial", False)),
            retain_on_improvement=bool(record.get("retainOnImprovement", False)),
            t0_success=bool(record.get("t0Success", False)),
            termination=dict(record.get("termination", {}) or {}),
            resource_cost=dict(record.get("resourceCost", {}) or {}),
            measurement_rules=dict(record.get("measurementRules", {}) or {}),
            intake=dict(record.get("intake", {}) or {}),
            schema_version=_clean(record.get("schemaVersion")) or EPISODE_SCHEMA)
