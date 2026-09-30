"""Frozen objective-axis to action-composition declarations.

Exactly one PRIMARY action in every allowed set maps an epoch-frozen candidate
axis.  SUPPLEMENTARY actions may support that candidate but are never valid on
their own.  This module is static policy data; it grants no permit and performs
no identity, role, or deployment verification.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Tuple

from assurance.contracts.harm import HarmKind

__all__ = [
    "ActionRole", "CombinationConstraint", "ForbiddenCombination",
    "HARM_AGGREGATION_POLICY", "HarmAggregationPolicy", "HarmAggregationRule",
    "LIVE_CAP_FAMILIES", "OBJECTIVE_ACTION_POLICIES", "ObjectiveActionPolicy",
    "PolicyActionBinding", "apply_order", "live_cap_admissible", "policy_for",
    "rollback_order",
]

#: The only two objective families whose composition may carry a **live**
#: ``ue-dl-prb-cap``.  ``SliceSLATarget`` keeps the cap in its hardware-free
#: composition table -- the slice study contracted it and the entry is
#: preserved rather than deleted -- but that family is not submittable on this
#: deployment, and the Action-102 contract limits live admission to the two
#: families that already carry the PRIMARY steering action the cap supplements
#: (``docs/architecture/A1-ACTION102-CONTRACT.md`` section 7.3).
LIVE_CAP_FAMILIES: Tuple[str, ...] = ("QoSTarget", "UELevelTarget")


class ActionRole(str, Enum):
    PRIMARY = "PRIMARY"
    SUPPLEMENTARY = "SUPPLEMENTARY"


class HarmAggregationRule(str, Enum):
    SUM = "SUM"
    MAX = "MAX"


@dataclass(frozen=True)
class HarmAggregationPolicy:
    harm_kind: HarmKind
    rule: HarmAggregationRule
    rationale: str


@dataclass(frozen=True)
class PolicyActionBinding:
    action_id: str
    classification: ActionRole
    candidate_axis: str | None
    rationale: str

    def __post_init__(self) -> None:
        if self.classification is ActionRole.PRIMARY and not self.candidate_axis:
            raise ValueError("PRIMARY action requires a candidate_axis")
        if self.classification is ActionRole.SUPPLEMENTARY and self.candidate_axis is not None:
            raise ValueError("SUPPLEMENTARY action cannot map a candidate_axis")


@dataclass(frozen=True)
class CombinationConstraint:
    constraint_id: str
    action_ids: Tuple[str, ...]
    rationale: str
    verifying_layer: str


@dataclass(frozen=True)
class ForbiddenCombination:
    action_ids: Tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ObjectiveActionPolicy:
    objective_family: str
    action_bindings: Tuple[PolicyActionBinding, ...]
    allowed_combinations: Tuple[Tuple[str, ...], ...]
    constraints: Tuple[CombinationConstraint, ...]
    forbidden: Tuple[ForbiddenCombination, ...]
    rationale: str

    @property
    def allowed_action_ids(self) -> Tuple[str, ...]:
        return tuple(binding.action_id for binding in self.action_bindings)

    @property
    def primary_bindings(self) -> Tuple[PolicyActionBinding, ...]:
        return tuple(binding for binding in self.action_bindings
                     if binding.classification is ActionRole.PRIMARY)


STEER = "cell-steering"
QUOTA = "slice-prb-quota"
CAP = "ue-dl-prb-cap"
PRIORITY = "scheduler-priority"
RF_ATTENUATION = "dl-rf-attenuation"
MCS = "dl-mcs-bounds"


def _primary(action_id: str, axis: str, rationale: str) -> PolicyActionBinding:
    return PolicyActionBinding(action_id, ActionRole.PRIMARY, axis, rationale)


def _supplementary(action_id: str, rationale: str) -> PolicyActionBinding:
    return PolicyActionBinding(action_id, ActionRole.SUPPLEMENTARY, None, rationale)


HARM_AGGREGATION_POLICY: Mapping[HarmKind, HarmAggregationPolicy] = MappingProxyType({
    HarmKind.TRIAL_INDUCED: HarmAggregationPolicy(
        HarmKind.TRIAL_INDUCED, HarmAggregationRule.SUM,
        "Each applied control may independently impose trial harm; sum is the conservative admission bound.",
    ),
    HarmKind.CONTRACT: HarmAggregationPolicy(
        HarmKind.CONTRACT, HarmAggregationRule.MAX,
        "Standing contract conditions coexist over one interval; max avoids charging the same condition per control.",
    ),
    HarmKind.TARGET_DEBT: HarmAggregationPolicy(
        HarmKind.TARGET_DEBT, HarmAggregationRule.MAX,
        "One objective verdict measures one shortfall; multiple controls do not duplicate target debt.",
    ),
})

_UE_SCOPE = CombinationConstraint(
    "ue-scope-identity", (STEER, CAP, PRIORITY),
    "Every present UE-scoped action must carry a non-empty objectiveUeId.",
    "Kernel admission cross-checks the selector against the epoch-frozen target scope and attributed UE evidence.",
)
_CAP_FLOOR = CombinationConstraint(
    "target-cap-throughput-floor", (CAP,),
    "For a family with an assurance throughput KPI, a positive cap must control a non-target heavy UE.",
    "Kernel admission cross-checks controlledUeRole against the target scope and decision-KPI snapshot.",
)
_PRIORITY_ROLE = CombinationConstraint(
    "priority-target-role", (PRIORITY,),
    "QoS scheduler priority must name the target UE rather than an unrelated UE.",
    "Kernel admission cross-checks controlledUeRole and objectiveUeId against the target scope.",
)
_QOS_ROLES = CombinationConstraint(
    "qos-resource-role-separation", (CAP, PRIORITY),
    "A relief cap and target priority must use distinct RNTIs and complementary roles.",
    "Kernel admission verifies roles; Write Gateway re-resolves RNTIs from fresh attribution before apply.",
)
_RNTI_REFRESH = CombinationConstraint(
    "rnti-refresh-after-primary", (STEER, CAP, PRIORITY),
    "RNTI-keyed supplementary parameters are re-resolved after steering readback because RNTI is cell-local.",
    "Write Gateway pre-apply under the Kernel permit, using fresh KPM UE attribution after PRIMARY readback.",
)
_SLICE_RELATION = CombinationConstraint(
    "slice-cap-outside-objective", (QUOTA, CAP),
    "A supplementary cap must target a UE attributed outside the protected S-NSSAI.",
    "Kernel admission cross-checks sliceRelation against the epoch target S-NSSAI and fresh attribution.",
)

_CELL_FLOOR_FORBIDDEN = (
    ForbiddenCombination((RF_ATTENUATION,),
        "Cell-wide attenuation can directly reduce SINR and defeat the objective floor; it is also non-RC."),
    ForbiddenCombination((MCS,),
        "Cell-wide MCS bounds can defeat the floor for every UE; caller-supplied coupling claims cannot make that safe."),
)

_STEER = _primary(STEER, "servingCell",
                  "RC Style 3 is the actuator for the frozen servingCell candidate axis.")
_CAP = _supplementary(CAP, "Congestion relief only; it never selects a candidate.")
_PRIORITY = _supplementary(PRIORITY, "Target scheduling support only; it never selects a candidate.")
_QUOTA = _primary(QUOTA, "slicePrbQuota",
                  "RC Style 2/Action 6 maps the frozen slicePrbQuota candidate axis.")

_STEERING_WITH_RESOURCES = (
    (STEER,), (STEER, CAP), (STEER, PRIORITY), (STEER, CAP, PRIORITY),
)

_POLICIES = (
    ObjectiveActionPolicy(
        "TrafficSteeringPreference", (_STEER,), ((STEER,),), (_UE_SCOPE,), (),
        "The preference axis is servingCell and grants no resource-control authority.",
    ),
    ObjectiveActionPolicy(
        "QoSTarget", (_STEER, _CAP, _PRIORITY), _STEERING_WITH_RESOURCES,
        (_UE_SCOPE, _CAP_FLOOR, _PRIORITY_ROLE, _QOS_ROLES, _RNTI_REFRESH),
        _CELL_FLOOR_FORBIDDEN,
        "Serving-cell selection is PRIMARY; UE resource controls are explicitly SUPPLEMENTARY.",
    ),
    ObjectiveActionPolicy(
        "UELevelTarget", (_STEER, _CAP, _PRIORITY),
        ((STEER,), (STEER, CAP), (STEER, PRIORITY)),
        # target-cap-throughput-floor applies here too: the Action-102 contract
        # admits a cap only on a heavy UE that is *not* the objective UE, in
        # both live families.  A cap on the objective UE is the one composition
        # that could take the very UE the target protects below its floor.
        (_UE_SCOPE, _CAP_FLOOR, _RNTI_REFRESH),
        (ForbiddenCombination((CAP, PRIORITY),
            "Two scheduler-resource controls on the objective UE have ambiguous precedence."),),
        "The frozen axis is servingCell; per-UE resource levers cannot stand alone.",
    ),
    ObjectiveActionPolicy(
        "QoSandTSP", (_STEER, _CAP, _PRIORITY), _STEERING_WITH_RESOURCES,
        (_UE_SCOPE, _CAP_FLOOR, _PRIORITY_ROLE, _QOS_ROLES, _RNTI_REFRESH),
        _CELL_FLOOR_FORBIDDEN,
        "The joint servingCell axis is PRIMARY and both component predicate sets remain mandatory.",
    ),
    ObjectiveActionPolicy(
        "QoETarget", (_STEER,), ((STEER,),), (_UE_SCOPE,),
        _CELL_FLOOR_FORBIDDEN,
        "The frozen QoE candidate axis is servingCell; cell-wide MCS cannot be admitted safely.",
    ),
    ObjectiveActionPolicy(
        "QoEandTSP", (_STEER,), ((STEER,),), (_UE_SCOPE,),
        _CELL_FLOOR_FORBIDDEN,
        "The joint candidate axis is servingCell; cell-wide MCS cannot be admitted safely.",
    ),
    # The supplementary Slice cap is forced outside the protected S-NSSAI.
    # That stronger relation excludes a target-slice cap outright, so a
    # separate target-cap-throughput-floor constraint would be redundant.
    ObjectiveActionPolicy(
        "SliceSLATarget", (_QUOTA, _CAP), ((QUOTA,), (QUOTA, CAP)),
        (_UE_SCOPE, _SLICE_RELATION), _CELL_FLOOR_FORBIDDEN,
        "slicePrbQuota is PRIMARY; an out-of-slice UE cap is SUPPLEMENTARY and never standalone.",
    ),
)

OBJECTIVE_ACTION_POLICIES: Mapping[str, ObjectiveActionPolicy] = MappingProxyType(
    {policy.objective_family: policy for policy in _POLICIES}
)


def policy_for(objective_family: str) -> ObjectiveActionPolicy:
    return OBJECTIVE_ACTION_POLICIES[objective_family]


def live_cap_admissible(objective_family: str) -> bool:
    """True only where a **live** ``ue-dl-prb-cap`` may be composed.

    Static policy data, not a permit: this says which families the Action-102
    contract allows the supplementary cap in at all.  Everything else that
    makes a cap admissible -- the frozen candidate value, the fresh non-target
    identity, the certified harm bound, the objective floor predicate -- is
    still checked, and this answering ``True`` grants none of it.
    """
    return objective_family in LIVE_CAP_FAMILIES


def apply_order(action_ids: Tuple[str, ...], objective_family: str) -> Tuple[str, ...]:
    """The order a composition's actions are applied in: PRIMARY first.

    The steering policy that selects the frozen candidate axis goes first
    because every supplementary action is keyed to the cell it lands on -- an
    RNTI is cell-local, so a cap written before the primary readback is a cap
    on an identity that may no longer exist.  Supplementary actions follow in
    the policy's declared binding order, which is fixed data rather than
    proposal order.
    """
    policy = policy_for(objective_family)
    declared = [binding.action_id for binding in policy.action_bindings]
    present = set(action_ids)
    primary = [b.action_id for b in policy.primary_bindings if b.action_id in present]
    supplementary = [
        action_id for action_id in declared
        if action_id in present and action_id not in primary
    ]
    return tuple(primary + supplementary)


def rollback_order(action_ids: Tuple[str, ...], objective_family: str) -> Tuple[str, ...]:
    """Exactly the reverse of :func:`apply_order`.

    Reverse order is not a preference.  Undoing a composition forwards can pass
    through a configuration that was never valid -- a restored cap on a cell the
    primary action has already moved away from -- so the supplementary change
    comes off first and the PRIMARY one last.
    """
    return tuple(reversed(apply_order(action_ids, objective_family)))
