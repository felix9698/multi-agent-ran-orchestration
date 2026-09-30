"""Objective-aware, read-only action composition resolution.

Resolution creates declarations for later Kernel/Write-Gateway verification.
It grants no admission, permit, identity assertion, role assertion, or effect.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Dict, Mapping, Sequence, Tuple

from assurance.actions import action_catalog, action_catalog_view, validate_action_parameters
from assurance.actions.composition_policy import (
    HARM_AGGREGATION_POLICY, OBJECTIVE_ACTION_POLICIES, ActionRole,
    CombinationConstraint, HarmAggregationRule, ObjectiveActionPolicy,
)
from assurance.contracts.capability import DeploymentBinding
from assurance.contracts.harm import HarmKind, WatchdogContract
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.objectives import FAMILY_MODULES, KpiUse, record_for

__all__ = [
    "ActionNotAllowedForFamilyError", "AdvisoryAction", "CombinedHarmBound",
    "CombinedHarmView", "CombinedMissingIntervalCharge",
    "CompositionConstraintViolationError", "CompositionResolutionError",
    "DuplicateActionError", "EmptyCompositionError", "ForbiddenCombinationError",
    "LiveCompositionNotSubmittableError", "ResolvedComposition",
    "RntiBindingRequirement", "SelectorVerificationRequirement",
    "UnknownActionError", "UnknownObjectiveFamilyError", "advisory_action_space",
    "resolve_composition",
]


class CompositionResolutionError(ValueError):
    pass


class UnknownObjectiveFamilyError(CompositionResolutionError):
    pass


class UnknownActionError(CompositionResolutionError):
    pass


class ActionNotAllowedForFamilyError(CompositionResolutionError):
    pass


class ForbiddenCombinationError(CompositionResolutionError):
    pass


class CompositionConstraintViolationError(CompositionResolutionError):
    pass


class DuplicateActionError(CompositionResolutionError):
    pass


class EmptyCompositionError(CompositionResolutionError):
    pass


class LiveCompositionNotSubmittableError(CompositionResolutionError):
    pass


@dataclass(frozen=True)
class AdvisoryAction:
    action_id: str
    parameters: Mapping[str, Any]
    target_selector: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        object.__setattr__(self, "target_selector",
                           MappingProxyType(dict(self.target_selector)))


@dataclass(frozen=True)
class CombinedHarmBound:
    harm_kind: HarmKind
    aggregation_rule: HarmAggregationRule
    constituent_bound_refs: Tuple[str, ...]
    constituent_harm_contract_refs: Tuple[str, ...]
    admissible_bound: TypedQuantity
    required_reserve: TypedQuantity
    rationale: str


@dataclass(frozen=True)
class CombinedMissingIntervalCharge:
    harm_contract_ref: str
    harm_kind: HarmKind
    charge: TypedQuantity


@dataclass(frozen=True)
class CombinedHarmView:
    """All harm declarations needed by a later Kernel admission consumer."""

    by_kind: Mapping[HarmKind, CombinedHarmBound]
    required_watchdogs: Tuple[WatchdogContract, ...]
    missing_interval_charges: Tuple[CombinedMissingIntervalCharge, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_kind", MappingProxyType(dict(self.by_kind)))
        object.__setattr__(self, "required_watchdogs", tuple(self.required_watchdogs))
        object.__setattr__(self, "missing_interval_charges",
                           tuple(self.missing_interval_charges))


@dataclass(frozen=True)
class SelectorVerificationRequirement:
    action_id: str
    selector_name: str
    claimed_value: Any
    verifying_layer: str
    evidence_basis: str


@dataclass(frozen=True)
class RntiBindingRequirement:
    action_id: str
    parameter_name: str
    resolution_semantics: str
    resolution_after_action_id: str
    objective_ue_id: str
    attribution_measurement_refs: Tuple[str, ...]
    verifying_layer: str


@dataclass(frozen=True)
class ResolvedComposition:
    objective_family: str
    actions: Tuple[AdvisoryAction, ...]
    primary_action_id: str
    primary_candidate_axis: str
    apply_order: Tuple[str, ...]
    rollback_order: Tuple[str, ...]
    combined_harm: CombinedHarmView
    selector_verifications: Tuple[SelectorVerificationRequirement, ...]
    rnti_bindings: Tuple[RntiBindingRequirement, ...]


def advisory_action_space(deployment: DeploymentBinding) -> Tuple[Mapping[str, Any], ...]:
    return action_catalog_view(deployment)


def _matching_combination(policy: ObjectiveActionPolicy,
                          action_ids: frozenset[str]) -> Tuple[str, ...] | None:
    return next((combination for combination in policy.allowed_combinations
                 if frozenset(combination) == action_ids), None)


def _require_selector(action: AdvisoryAction, name: str, constraint_id: str,
                      expected: Any = None) -> Any:
    if name not in action.target_selector:
        raise CompositionConstraintViolationError(
            f"{constraint_id}: {action.action_id}.target_selector.{name} is required")
    value = action.target_selector[name]
    if not isinstance(value, str):
        raise CompositionConstraintViolationError(
            f"{constraint_id}: {action.action_id}.target_selector.{name} must be "
            f"a non-empty string, got {type(value).__name__}")
    if not value.strip():
        raise CompositionConstraintViolationError(
            f"{constraint_id}: {action.action_id}.target_selector.{name} is empty")
    if expected is not None and value != expected:
        raise CompositionConstraintViolationError(
            f"{constraint_id}: {action.action_id}.target_selector.{name} must be "
            f"{expected!r}, got {value!r}")
    return value


def _validate_selector_consistency(actions: Tuple[AdvisoryAction, ...]) -> None:
    """Reject selector claims that cannot both describe the controlled UE."""
    for action in actions:
        selector = action.target_selector
        if (selector.get("controlledUeRole") == "TARGET_UE"
                and selector.get("sliceRelation") == "OUTSIDE_OBJECTIVE_SLICE"):
            raise CompositionConstraintViolationError(
                f"selector-consistency: {action.action_id} cannot identify TARGET_UE "
                "and OUTSIDE_OBJECTIVE_SLICE at the same time")


def _ue_scope_identity(constraint: CombinationConstraint,
                       by_id: Mapping[str, AdvisoryAction], family_module: Any) -> None:
    del family_module
    objective_ue_ids = []
    for action_id in constraint.action_ids:
        if action_id in by_id:
            objective_ue_ids.append(_require_selector(
                by_id[action_id], "objectiveUeId", constraint.constraint_id))
    if len(set(objective_ue_ids)) > 1:
        raise CompositionConstraintViolationError(
            f"{constraint.constraint_id}: objectiveUeId values differ")


def _family_has_throughput_floor(family_module: Any) -> bool:
    return any(kpi.use is KpiUse.ASSURANCE and kpi.unit.lower() in {"kbit/s", "mbit/s"}
               for kpi in family_module.kpi_declaration())


def _target_cap_floor(constraint: CombinationConstraint,
                      by_id: Mapping[str, AdvisoryAction], family_module: Any) -> None:
    cap = by_id.get("ue-dl-prb-cap")
    if cap is not None and cap.parameters["maxDlPrbs"] > 0 \
            and _family_has_throughput_floor(family_module):
        _require_selector(cap, "controlledUeRole", constraint.constraint_id,
                          "NON_TARGET_HEAVY_UE")


def _priority_target_role(constraint: CombinationConstraint,
                          by_id: Mapping[str, AdvisoryAction], family_module: Any) -> None:
    del family_module
    priority = by_id.get("scheduler-priority")
    if priority is not None:
        _require_selector(priority, "controlledUeRole", constraint.constraint_id,
                          "TARGET_UE")


def _qos_role_separation(constraint: CombinationConstraint,
                         by_id: Mapping[str, AdvisoryAction], family_module: Any) -> None:
    del family_module
    cap, priority = by_id.get("ue-dl-prb-cap"), by_id.get("scheduler-priority")
    if cap is None or priority is None:
        return
    _require_selector(cap, "controlledUeRole", constraint.constraint_id,
                      "NON_TARGET_HEAVY_UE")
    _require_selector(priority, "controlledUeRole", constraint.constraint_id, "TARGET_UE")
    if cap.parameters["rnti"] == priority.parameters["rnti"]:
        raise CompositionConstraintViolationError(
            f"{constraint.constraint_id}: cap and priority must use distinct RNTIs")


def _rnti_refresh(constraint: CombinationConstraint,
                  by_id: Mapping[str, AdvisoryAction], family_module: Any) -> None:
    del family_module
    if "cell-steering" not in by_id:
        return
    for action_id in ("ue-dl-prb-cap", "scheduler-priority"):
        if action_id in by_id:
            _require_selector(by_id[action_id], "rntiBinding", constraint.constraint_id,
                              "RESOLVE_AFTER_PRIMARY_READBACK")


def _slice_relation(constraint: CombinationConstraint,
                    by_id: Mapping[str, AdvisoryAction], family_module: Any) -> None:
    del family_module
    cap = by_id.get("ue-dl-prb-cap")
    if cap is not None:
        _require_selector(cap, "sliceRelation", constraint.constraint_id,
                          "OUTSIDE_OBJECTIVE_SLICE")


_CONSTRAINT_HANDLERS: Mapping[str, Callable[[CombinationConstraint,
                                             Mapping[str, AdvisoryAction], Any], None]] = {
    "ue-scope-identity": _ue_scope_identity,
    "target-cap-throughput-floor": _target_cap_floor,
    "priority-target-role": _priority_target_role,
    "qos-resource-role-separation": _qos_role_separation,
    "rnti-refresh-after-primary": _rnti_refresh,
    "slice-cap-outside-objective": _slice_relation,
}


def _validate_constraints(policy: ObjectiveActionPolicy,
                          actions: Tuple[AdvisoryAction, ...], family_module: Any) -> None:
    by_id = {action.action_id: action for action in actions}
    for constraint in policy.constraints:
        try:
            handler = _CONSTRAINT_HANDLERS[constraint.constraint_id]
        except KeyError as exc:
            raise CompositionConstraintViolationError(
                f"no enforcement handler for declared constraint {constraint.constraint_id}") from exc
        handler(constraint, by_id, family_module)


def _aggregate(values: Sequence[TypedQuantity], rule: HarmAggregationRule) -> float:
    if rule is HarmAggregationRule.SUM:
        return float(sum(value.value for value in values))
    return float(max(value.value for value in values))


def _combined_harm(actions: Tuple[AdvisoryAction, ...], catalog: Mapping[str, Any]) \
        -> CombinedHarmView:
    grouped: Dict[HarmKind, list[Any]] = {}
    contracts = tuple(catalog[action.action_id] for action in actions)
    for contract in contracts:
        grouped.setdefault(contract.harm.harm_kind, []).append(contract)
    combined: Dict[HarmKind, CombinedHarmBound] = {}
    for harm_kind, kind_contracts in grouped.items():
        aggregation = HARM_AGGREGATION_POLICY[harm_kind]
        bounds = tuple(bound for contract in kind_contracts for bound in contract.harm.bounds)
        bound_values = tuple(bound.admissible_bound for bound in bounds)
        reserve_values = tuple(contract.harm.reserve for contract in kind_contracts)
        units = {value.unit for value in bound_values + reserve_values}
        if len(units) != 1:
            raise CompositionConstraintViolationError(
                f"combined harm for {harm_kind.value} has incompatible units: {sorted(units)}")
        unit = next(iter(units))
        contract_refs = tuple(contract.harm.contract_id for contract in kind_contracts)
        bound_refs = tuple(bound.bound_id for bound in bounds)
        combined[harm_kind] = CombinedHarmBound(
            harm_kind, aggregation.rule, bound_refs, contract_refs,
            TypedQuantity(_aggregate(bound_values, aggregation.rule), unit,
                Provenance.DERIVED, f"composition/{harm_kind.value.lower()}/admissible-bound",
                derivation_rule=f"{aggregation.rule.value.lower()}_certified_bounds",
                input_refs=bound_refs),
            TypedQuantity(_aggregate(reserve_values, aggregation.rule), unit,
                Provenance.DERIVED, f"composition/{harm_kind.value.lower()}/required-reserve",
                derivation_rule=f"{aggregation.rule.value.lower()}_harm_reserves",
                input_refs=contract_refs),
            aggregation.rationale,
        )
    return CombinedHarmView(
        by_kind=combined,
        required_watchdogs=tuple(watchdog for contract in contracts
                                 for watchdog in contract.harm.watchdogs),
        missing_interval_charges=tuple(
            CombinedMissingIntervalCharge(contract.harm.contract_id,
                                          contract.harm.harm_kind,
                                          contract.harm.missing_interval_charge)
            for contract in contracts),
    )


def _selector_verifications(actions: Tuple[AdvisoryAction, ...]) \
        -> Tuple[SelectorVerificationRequirement, ...]:
    rules = {
        "objectiveUeId": (
            "Kernel admission",
            "epoch-frozen target scope plus attributed UE evidence"),
        "controlledUeRole": (
            "Kernel admission",
            "epoch target identity plus the frozen decision-KPI snapshot"),
        "sliceRelation": (
            "Kernel admission",
            "epoch target S-NSSAI plus fresh UE-to-slice attribution"),
    }
    return tuple(
        SelectorVerificationRequirement(action.action_id, name,
                                        action.target_selector[name], layer, basis)
        for action in actions
        for name, (layer, basis) in rules.items()
        if name in action.target_selector
    )


def _rnti_requirements(actions: Tuple[AdvisoryAction, ...], family_module: Any) \
        -> Tuple[RntiBindingRequirement, ...]:
    if not any(action.action_id == "cell-steering" for action in actions):
        return ()
    attribution_refs = tuple(kpi.measurement_ref for kpi in family_module.kpi_declaration()
                             if "UE" in kpi.scope_level and "KPM" in kpi.source_interface)
    requirements = []
    for action in actions:
        if "rnti" not in action.parameters:
            continue
        requirements.append(RntiBindingRequirement(
            action_id=action.action_id,
            parameter_name="rnti",
            resolution_semantics="RESOLVE_AFTER_PRIMARY_READBACK",
            resolution_after_action_id="cell-steering",
            objective_ue_id=str(action.target_selector["objectiveUeId"]),
            attribution_measurement_refs=attribution_refs,
            verifying_layer="Write Gateway pre-apply under Kernel permit",
        ))
    return tuple(requirements)


def resolve_composition(proposals: Sequence[AdvisoryAction],
                        deployment: DeploymentBinding,
                        objective_family: str, *,
                        for_live: bool = True) -> ResolvedComposition:
    """Resolve one frozen objective composition; perform no admission or effect."""
    try:
        policy = OBJECTIVE_ACTION_POLICIES[objective_family]
        family_module = FAMILY_MODULES[objective_family]()
    except KeyError as exc:
        raise UnknownObjectiveFamilyError(f"unknown objective family: {objective_family}") from exc
    if not proposals:
        raise EmptyCompositionError("action composition is empty")
    if for_live:
        capability = record_for(objective_family).deployment_capability
        if not capability.submittable:
            reasons = "; ".join(capability.blocking_reasons)
            raise LiveCompositionNotSubmittableError(
                f"{objective_family}: deployment is not live-submittable: {reasons}")

    catalog = {item.action_id: item for item in action_catalog(deployment)}
    by_id: Dict[str, AdvisoryAction] = {}
    for proposal in proposals:
        if proposal.action_id in by_id:
            raise DuplicateActionError(f"duplicate action in composition: {proposal.action_id}")
        if proposal.action_id not in catalog:
            raise UnknownActionError(f"unknown action: {proposal.action_id}")
        by_id[proposal.action_id] = proposal
    action_ids = frozenset(by_id)

    tier_b = tuple(action_id for action_id in action_ids if catalog[action_id].tier == "B")
    if for_live and tier_b:
        reasons = tuple(catalog[action_id].binding.live_blocking_premise for action_id in tier_b)
        raise ForbiddenCombinationError(
            f"Tier B actions are contract-only in live compositions: {sorted(tier_b)}; "
            f"blocking premises: {list(reasons)}")
    for forbidden in policy.forbidden:
        if frozenset(forbidden.action_ids) <= action_ids:
            raise ForbiddenCombinationError(
                f"{objective_family}: forbidden combination {sorted(forbidden.action_ids)}: "
                f"{forbidden.reason}")
    foreign = action_ids - frozenset(policy.allowed_action_ids)
    if foreign:
        raise ActionNotAllowedForFamilyError(
            f"{objective_family}: actions not allowed for family: {sorted(foreign)}")
    combination = _matching_combination(policy, action_ids)
    if combination is None:
        raise ForbiddenCombinationError(
            f"{objective_family}: action set is not an allowed combination: {sorted(action_ids)}")

    bindings = {binding.action_id: binding for binding in policy.action_bindings}
    primary = tuple(bindings[action_id] for action_id in combination
                    if bindings[action_id].classification is ActionRole.PRIMARY)
    if len(primary) != 1:
        raise CompositionConstraintViolationError(
            f"{objective_family}: composition requires exactly one PRIMARY action, got {len(primary)}")
    actions = tuple(by_id[action_id] for action_id in combination)
    for action in actions:
        validate_action_parameters(catalog[action.action_id], action.parameters)
    _validate_selector_consistency(actions)
    _validate_constraints(policy, actions, family_module)

    apply_order = tuple(action.action_id for action in actions)
    return ResolvedComposition(
        objective_family=objective_family,
        actions=actions,
        primary_action_id=primary[0].action_id,
        primary_candidate_axis=str(primary[0].candidate_axis),
        apply_order=apply_order,
        rollback_order=tuple(reversed(apply_order)),
        combined_harm=_combined_harm(actions, catalog),
        selector_verifications=_selector_verifications(actions),
        rnti_bindings=_rnti_requirements(actions, family_module),
    )
