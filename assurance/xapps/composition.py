"""Action Composition Coordinator: Policy + common KPI snapshot -> actions.

The coordinator recommends *Action compositions* -- it does not run xApps and
it does not let xApps propose.  Specialist xApps register capability
manifests instead of producing Action Proposals; the composition decision is
made here, against the frozen objective composition policy and a common KPI
snapshot, and its output is a typed :class:`CandidateActionSet` that must
still pass Assurance Kernel admission and obtain Write Gateway permits
before anything reaches equipment.

Reuse over reinvention: allowed combinations, roles, forbidden pairs,
constraints, combined harm, apply/rollback order, selector and RNTI
requirements all come from the existing
:func:`assurance.advisors.action_space.resolve_composition` over
:data:`assurance.actions.composition_policy.OBJECTIVE_ACTION_POLICIES`.  The
coordinator adds what that read-only validator does not do: snapshot
freshness, contradiction detection, current-value binding, expiry, and
replanning.

**Policy-to-value boundary (deliberate, recorded):** this repository has no
deterministic logic that derives concrete supplementary control values (a PRB
cap number, a PF weight, an attenuation step) from a Policy alone, and no
arbitrary LLM or optimizer is added to the production path here.  Parameter
values therefore enter as caller proposals -- produced by the existing frozen
candidate catalog or advisory strategies -- and the coordinator validates,
binds and assembles them.  ``PROPOSAL_VALUE_BOUNDARY`` states this on every
produced set.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from assurance.actions import action_catalog, validate_action_parameters
from assurance.advisors.action_space import (
    AdvisoryAction, CombinedHarmView, ResolvedComposition, resolve_composition,
    _combined_harm,
)
from assurance.contracts.capability import DeploymentBinding
from assurance.core.addressing import content_hash
from assurance.core.provenance import DocumentStatus, TypedQuantity
from assurance.core.timebase import is_utc_timestamp
from assurance.xapps.snapshot import CommonKpiSnapshot, advance_timestamp

__all__ = [
    "PROPOSAL_VALUE_BOUNDARY",
    "ActionCompositionCoordinator",
    "CandidateActionSet",
    "CompositionCoordinatorError",
    "ContradictoryActionError",
    "RecommendedAction",
    "StaleSnapshotError",
]

PROPOSAL_VALUE_BOUNDARY = (
    "Concrete parameter values are caller proposals from the frozen candidate "
    "generators or advisory strategies; this coordinator validates and "
    "assembles them under the frozen composition policy but derives no value "
    "from a Policy by itself, and no LLM or optimizer output is admissible "
    "here."
)


class CompositionCoordinatorError(ValueError):
    """The coordinator refuses to recommend from the given inputs."""


class StaleSnapshotError(CompositionCoordinatorError):
    """The common KPI snapshot is too old (or future-dated) to plan from."""


class ContradictoryActionError(CompositionCoordinatorError):
    """Two proposals set the same parameter of the same target differently."""


@dataclass(frozen=True)
class RecommendedAction:
    """One recommended Action with its execution-relevant declarations."""

    action_id: str
    action_family: str
    parameters: Mapping[str, Any]
    target_selector: Mapping[str, Any]
    #: counter id -> canonical dict of the snapshot's current value, when the
    #: snapshot delivered one.  Absence is recorded as a precondition, never
    #: invented.
    current_values: Mapping[str, Any]
    preconditions: Tuple[str, ...]
    success_conditions: Tuple[str, ...]
    abort_conditions: Tuple[str, ...]
    expected_kpi_impact: Tuple[TypedQuantity, ...]
    rollback_required: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        object.__setattr__(self, "target_selector",
                           MappingProxyType(dict(self.target_selector)))
        object.__setattr__(self, "current_values",
                           MappingProxyType(dict(self.current_values)))
        for quantity in self.expected_kpi_impact:
            if not isinstance(quantity, TypedQuantity):
                raise CompositionCoordinatorError(
                    "expected_kpi_impact entries must be TypedQuantity")
            if quantity.document_status is not DocumentStatus.DRAFT:
                raise CompositionCoordinatorError(
                    "an expected KPI impact is advisory and must be DRAFT; "
                    f"{quantity.document_status.value} would be admissible at "
                    "runtime and a recommendation may not set admissible numbers")

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "actionId": self.action_id,
            "actionFamily": self.action_family,
            "parameters": dict(self.parameters),
            "targetSelector": dict(self.target_selector),
            "currentValues": dict(self.current_values),
            "preconditions": list(self.preconditions),
            "successConditions": list(self.success_conditions),
            "abortConditions": list(self.abort_conditions),
            "expectedKpiImpact": [q.to_canonical_dict()
                                  for q in self.expected_kpi_impact],
            "rollbackRequired": self.rollback_required,
        }


@dataclass(frozen=True)
class CandidateActionSet:
    """A typed Action-composition recommendation, awaiting admission.

    This is a declaration, not a permit: the set carries no write authority,
    and every consumer downstream (XApp Execution Coordinator, Assurance
    Kernel, Write Gateway) re-verifies it.  ``resolved`` keeps the full
    backward-compatible :class:`ResolvedComposition` so existing consumers of
    that contract keep working unchanged.
    """

    set_id: str
    objective_family: str
    policy_ref: str
    snapshot_id: str
    snapshot_taken_at: str
    snapshot_hash: str
    actions: Tuple[RecommendedAction, ...]
    resolved: ResolvedComposition
    created_at: str
    expires_at: str
    provenance_note: str = PROPOSAL_VALUE_BOUNDARY
    replanned_from: Optional[str] = None
    replan_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions", tuple(self.actions))
        for name in ("created_at", "expires_at", "snapshot_taken_at"):
            if not is_utc_timestamp(getattr(self, name)):
                raise CompositionCoordinatorError(
                    f"{name} is not canonical UTC: {getattr(self, name)!r}")

    # -- delegated composition facts --------------------------------------

    @property
    def primary_action_id(self) -> str:
        return self.resolved.primary_action_id

    @property
    def apply_order(self) -> Tuple[str, ...]:
        return self.resolved.apply_order

    @property
    def rollback_order(self) -> Tuple[str, ...]:
        return self.resolved.rollback_order

    @property
    def combined_harm(self) -> CombinedHarmView:
        return self.resolved.combined_harm

    def action_for(self, action_id: str) -> RecommendedAction:
        for action in self.actions:
            if action.action_id == action_id:
                return action
        raise CompositionCoordinatorError(f"no action {action_id!r} in this set")

    def is_expired(self, now: str) -> bool:
        from assurance.core.timebase import parse_utc
        return parse_utc(now) >= parse_utc(self.expires_at)

    def to_canonical_dict(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "setId": self.set_id,
            "objectiveFamily": self.objective_family,
            "policyRef": self.policy_ref,
            "snapshotId": self.snapshot_id,
            "snapshotTakenAt": self.snapshot_taken_at,
            "snapshotHash": self.snapshot_hash,
            "actions": [action.to_canonical_dict() for action in self.actions],
            "primaryActionId": self.primary_action_id,
            "applyOrder": list(self.apply_order),
            "rollbackOrder": list(self.rollback_order),
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "provenanceNote": self.provenance_note,
        }
        if self.replanned_from is not None:
            record["replannedFrom"] = self.replanned_from
            record["replanReason"] = self.replan_reason
        return record

    def content_hash(self) -> str:
        return content_hash(self.to_canonical_dict())


class ActionCompositionCoordinator:
    """Recommends Action compositions; holds no equipment-write authority.

    The class deliberately has no reference to the Write Gateway, to any
    adapter, or to any transport: its one output is a declaration the
    Assurance Kernel must still admit.
    """

    def __init__(
        self,
        *,
        deployment: DeploymentBinding,
        snapshot_freshness_bound_ms: int = 5_000,
        validity_window_ms: int = 30_000,
    ) -> None:
        if snapshot_freshness_bound_ms <= 0 or validity_window_ms <= 0:
            raise CompositionCoordinatorError(
                "freshness and validity bounds must be positive")
        self._deployment = deployment
        self._freshness_bound_ms = snapshot_freshness_bound_ms
        self._validity_window_ms = validity_window_ms
        self._catalog = {item.action_id: item for item in action_catalog(deployment)}

    # -- recommendation ----------------------------------------------------

    def recommend(
        self,
        *,
        objective_family: str,
        proposals: Sequence[AdvisoryAction],
        snapshot: CommonKpiSnapshot,
        now: str,
        for_live: bool = True,
        expected_impacts: Mapping[str, Sequence[TypedQuantity]] = MappingProxyType({}),
        policy_ref: Optional[str] = None,
        replanned_from: Optional[str] = None,
        replan_reason: str = "",
    ) -> CandidateActionSet:
        """Validate and assemble one composition into a candidate set.

        Refusal order: snapshot type and freshness, proposal contradiction,
        then the full frozen-policy resolution
        (:func:`resolve_composition`), which enforces allowed combinations,
        forbidden pairs, per-family constraints and parameter bounds.
        """
        if not isinstance(snapshot, CommonKpiSnapshot):
            raise CompositionCoordinatorError(
                "recommend() needs a CommonKpiSnapshot assembled from "
                "Measurement Collector samples")
        if not snapshot.is_fresh(now, freshness_bound_ms=self._freshness_bound_ms):
            raise StaleSnapshotError(
                f"snapshot {snapshot.snapshot_id} taken at {snapshot.taken_at} "
                f"is stale at {now} (bound {self._freshness_bound_ms} ms); "
                "collect a fresh snapshot and replan")
        self._refuse_contradictions(proposals)

        resolved = resolve_composition(
            proposals, self._deployment, objective_family, for_live=for_live)

        actions = tuple(
            self._recommended_action(action, resolved, snapshot,
                                     tuple(expected_impacts.get(action.action_id, ())))
            for action in resolved.actions
        )
        expires_at = advance_timestamp(snapshot.taken_at, self._validity_window_ms)
        body = {
            "objectiveFamily": objective_family,
            "snapshotHash": snapshot.content_hash(),
            "actions": [action.to_canonical_dict() for action in actions],
        }
        set_id = f"candidate-set/{content_hash(body)[:16]}"
        return CandidateActionSet(
            set_id=set_id,
            objective_family=objective_family,
            policy_ref=policy_ref or f"objective/{objective_family}",
            snapshot_id=snapshot.snapshot_id,
            snapshot_taken_at=snapshot.taken_at,
            snapshot_hash=snapshot.content_hash(),
            actions=actions,
            resolved=resolved,
            created_at=now,
            expires_at=expires_at,
            replanned_from=replanned_from,
            replan_reason=replan_reason,
        )

    def replan(
        self,
        *,
        previous: CandidateActionSet,
        reason: str,
        objective_family: str,
        proposals: Sequence[AdvisoryAction],
        snapshot: CommonKpiSnapshot,
        now: str,
        for_live: bool = True,
        expected_impacts: Mapping[str, Sequence[TypedQuantity]] = MappingProxyType({}),
    ) -> CandidateActionSet:
        """Recompute a composition after ``REPLAN_REQUIRED``, on fresh KPI.

        The new snapshot must actually be newer than the one the invalidated
        set was planned from -- replanning on the same stale evidence would
        reproduce the same invalid plan.
        """
        if not reason.strip():
            raise CompositionCoordinatorError("a replan needs its reason recorded")
        if snapshot.snapshot_id == previous.snapshot_id:
            raise StaleSnapshotError(
                "replan requires a fresh KPI snapshot; "
                f"{snapshot.snapshot_id} is the snapshot the invalidated set "
                "was planned from")
        return self.recommend(
            objective_family=objective_family,
            proposals=proposals,
            snapshot=snapshot,
            now=now,
            for_live=for_live,
            expected_impacts=expected_impacts,
            policy_ref=previous.policy_ref,
            replanned_from=previous.set_id,
            replan_reason=reason,
        )

    def declare_standalone_action_set(
        self,
        *,
        action: AdvisoryAction,
        basis: str,
        snapshot: CommonKpiSnapshot,
        now: str,
    ) -> CandidateActionSet:
        """Declare one Action outside any objective composition policy.

        No objective family in ``composition_policy.py`` admits
        ``dl-rf-attenuation`` (it is non-RC, cell-wide, and forbidden for
        every floor family), so a power Action can only reach the execution
        coordinator through this explicit, basis-carrying declaration -- for
        example lab power management on the hardware-free path.  The
        declaration does not invent a Policy: ``policy_ref`` records the
        stated basis verbatim, and the set still needs Kernel admission and a
        Gateway permit like every other set.
        """
        if not basis.strip():
            raise CompositionCoordinatorError(
                "a standalone action set needs its declared basis recorded")
        if not isinstance(snapshot, CommonKpiSnapshot):
            raise CompositionCoordinatorError(
                "declare_standalone_action_set() needs a CommonKpiSnapshot")
        if not snapshot.is_fresh(now, freshness_bound_ms=self._freshness_bound_ms):
            raise StaleSnapshotError(
                f"snapshot {snapshot.snapshot_id} is stale at {now}")
        contract = self._catalog.get(action.action_id)
        if contract is None:
            raise CompositionCoordinatorError(
                f"unknown action {action.action_id!r}")
        if contract.tier != "A":
            raise CompositionCoordinatorError(
                f"{action.action_id}: Tier {contract.tier} actions are "
                "contract-only and cannot be declared standalone")
        validate_action_parameters(contract, action.parameters)
        axis = contract.binding.parameters[0].name
        resolved = ResolvedComposition(
            objective_family="StandaloneDeclaredAction",
            actions=(action,),
            primary_action_id=action.action_id,
            primary_candidate_axis=axis,
            apply_order=(action.action_id,),
            rollback_order=(action.action_id,),
            combined_harm=_combined_harm((action,), self._catalog),
            selector_verifications=(),
            rnti_bindings=(),
        )
        recommended = self._recommended_action(action, resolved, snapshot, ())
        expires_at = advance_timestamp(snapshot.taken_at, self._validity_window_ms)
        body = {
            "standaloneBasis": basis,
            "snapshotHash": snapshot.content_hash(),
            "action": recommended.to_canonical_dict(),
        }
        return CandidateActionSet(
            set_id=f"candidate-set/{content_hash(body)[:16]}",
            objective_family="StandaloneDeclaredAction",
            policy_ref=f"standalone:{basis}",
            snapshot_id=snapshot.snapshot_id,
            snapshot_taken_at=snapshot.taken_at,
            snapshot_hash=snapshot.content_hash(),
            actions=(recommended,),
            resolved=resolved,
            created_at=now,
            expires_at=expires_at,
        )

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _refuse_contradictions(proposals: Sequence[AdvisoryAction]) -> None:
        """Two proposals for one Action with different values are a
        contradiction and are refused here, before policy resolution."""
        seen: Dict[str, AdvisoryAction] = {}
        for proposal in proposals:
            if not isinstance(proposal, AdvisoryAction):
                raise CompositionCoordinatorError(
                    "proposals must be AdvisoryAction instances")
            earlier = seen.get(proposal.action_id)
            if earlier is not None and dict(earlier.parameters) != dict(proposal.parameters):
                raise ContradictoryActionError(
                    f"{proposal.action_id}: two proposals set "
                    f"{sorted(proposal.parameters)} on the same target to "
                    "different values; a contradictory composition is refused "
                    "at the composition stage")
            seen[proposal.action_id] = proposal

    def _recommended_action(
        self,
        action: AdvisoryAction,
        resolved: ResolvedComposition,
        snapshot: CommonKpiSnapshot,
        impacts: Tuple[TypedQuantity, ...],
    ) -> RecommendedAction:
        contract = self._catalog[action.action_id]
        readback_counter = contract.counter.counter_id
        current_entry = snapshot.latest(readback_counter)
        current_values: Dict[str, Any] = {}
        preconditions = [
            f"{requirement.selector_name}={requirement.claimed_value!r} verified by "
            f"{requirement.verifying_layer}"
            for requirement in resolved.selector_verifications
            if requirement.action_id == action.action_id
        ]
        for binding in resolved.rnti_bindings:
            if binding.action_id == action.action_id:
                preconditions.append(
                    f"rnti resolved {binding.resolution_semantics} after "
                    f"{binding.resolution_after_action_id} by {binding.verifying_layer}")
        if current_entry is not None:
            current_values[readback_counter] = current_entry.value.to_canonical_dict()
        else:
            preconditions.append(
                f"CONFIGURATION_REREAD of {readback_counter} before apply: the "
                "snapshot delivered no current value and none is invented")
        return RecommendedAction(
            action_id=action.action_id,
            action_family=self._family_of(action.action_id),
            parameters=action.parameters,
            target_selector=action.target_selector,
            current_values=current_values,
            preconditions=tuple(preconditions),
            success_conditions=(
                f"readback {contract.binding.readback_measurement_ref} confirms "
                "the applied value (APPLY_READBACK_COMMIT)",
            ),
            abort_conditions=(contract.watchdog.contract_id,),
            expected_kpi_impact=impacts,
            rollback_required=contract.binding.rollback_supported,
        )

    @staticmethod
    def _family_of(action_id: str) -> str:
        from assurance.xapps.manifest import ACTION_FAMILY_BY_ID
        return ACTION_FAMILY_BY_ID[action_id]
