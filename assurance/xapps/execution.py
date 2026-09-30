"""XApp Execution Coordinator: map admitted Actions to xApps, in order.

This coordinator decides no new Action and changes no Action value.  It maps
each recommended Action to the one registered xApp that owns it, checks
inter-xApp conflicts and dependencies, and produces a typed
:class:`XAppExecutionPlan` with execution order, re-measurement, hold and
rollback semantics.  Choosing a different action family, a different target
cell, or a different parameter value here would be a role violation; the
class has no code path that constructs modified parameters.

Execution priority belongs to the **execution step** -- the pairing of one
xApp with one concrete Action -- never to the xApp itself.  There is no
"Traffic Steering is always first" rule anywhere in this module: order falls
out of concrete-Action dependencies (a steering readback that re-keys RNTIs,
a power change that invalidates the state a handover was judged on), so an
"inter-xApp control conflict" is always, concretely, a conflict between the
Actions assigned to those xApps.

A produced plan grants nothing.  It must pass Assurance Kernel admission,
and each step's equipment write happens only under a Write Gateway permit
(:class:`assurance.gateway.token.KernelToken`); the step records which token
kinds it needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from assurance.core.addressing import content_hash
from assurance.core.timebase import is_utc_timestamp, parse_utc
from assurance.xapps.composition import CandidateActionSet, RecommendedAction
from assurance.xapps.manifest import XAppCapabilityManifest
from assurance.xapps.registry import (
    LiveSelectionState, XAppCapabilityRegistry,
)
from assurance.xapps.snapshot import (
    SERVING_CELL_ATTRIBUTION_COUNTER, advance_timestamp,
)

__all__ = [
    "CoordinationOutcome",
    "ExecutionCoordinationResult",
    "KERNEL_ADMISSION_REQUIRED_NOTE",
    "RecentExecution",
    "StepPrecondition",
    "StepRefusal",
    "XAppExecutionCoordinator",
    "XAppExecutionPlan",
    "XAppExecutionStep",
]

KERNEL_ADMISSION_REQUIRED_NOTE = (
    "This plan is a declaration. No step may reach equipment before Assurance "
    "Kernel admission and a Write Gateway permit for that step; the "
    "coordinators are not equipment writers."
)


class CoordinationOutcome(Enum):
    """The typed answers this coordinator can give."""

    PLANNED = "PLANNED"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    NOT_WIRED = "NOT_WIRED"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    BLOCKED_BY_SAFETY = "BLOCKED_BY_SAFETY"
    BLOCKED_BY_POLICY = "BLOCKED_BY_POLICY"


class PreconditionKind(Enum):
    """What must hold before one step may execute."""

    PRIMARY_READBACK_CONFIRMED = "PRIMARY_READBACK_CONFIRMED"
    SERVING_CELL_REVERIFIED = "SERVING_CELL_REVERIFIED"
    RNTI_REVERIFIED = "RNTI_REVERIFIED"
    FRESH_SNAPSHOT_REQUIRED = "FRESH_SNAPSHOT_REQUIRED"
    CONFIGURATION_REREAD = "CONFIGURATION_REREAD"
    SELECTOR_VERIFIED = "SELECTOR_VERIFIED"


@dataclass(frozen=True)
class StepPrecondition:
    kind: PreconditionKind
    subject: str
    detail: str = ""

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind.value, "subject": self.subject,
                "detail": self.detail}


@dataclass(frozen=True)
class StepRefusal:
    """Why one Action could not be planned (or is held) -- never rewritten."""

    action_id: str
    outcome: CoordinationOutcome
    reason: str
    xapp_id: Optional[str] = None


@dataclass(frozen=True)
class RecentExecution:
    """One already-approved, already-applied Action the planner must respect."""

    action_id: str
    target_key: str
    applied_parameters: Mapping[str, Any]
    previous_parameters: Mapping[str, Any]
    completed_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "applied_parameters",
                           dict(self.applied_parameters))
        object.__setattr__(self, "previous_parameters",
                           dict(self.previous_parameters))
        if not is_utc_timestamp(self.completed_at):
            raise ValueError(
                f"completed_at is not canonical UTC: {self.completed_at!r}")


@dataclass(frozen=True)
class XAppExecutionStep:
    """One (xApp, concrete Action) execution unit inside a plan."""

    step_id: str
    xapp_id: str
    action_id: str
    action_family: str
    parameters: Mapping[str, Any]
    target_selector: Mapping[str, Any]
    execution_priority: int
    candidate_set_id: str
    preconditions: Tuple[StepPrecondition, ...]
    required_permit_kinds: Tuple[str, ...]
    readback_measurement_ref: str
    post_readback_measurements: Tuple[str, ...]
    proceed_condition: str
    on_failure_rollback: str
    invalidates_step_ids: Tuple[str, ...] = ()
    revalidation: str = ""
    held: bool = False
    hold_reason: str = ""
    affected_scope: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", dict(self.parameters))
        object.__setattr__(self, "target_selector", dict(self.target_selector))

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "stepId": self.step_id,
            "xappId": self.xapp_id,
            "actionId": self.action_id,
            "actionFamily": self.action_family,
            "parameters": dict(self.parameters),
            "targetSelector": dict(self.target_selector),
            "executionPriority": self.execution_priority,
            "candidateSetId": self.candidate_set_id,
            "preconditions": [p.to_canonical_dict() for p in self.preconditions],
            "requiredPermitKinds": list(self.required_permit_kinds),
            "readbackMeasurementRef": self.readback_measurement_ref,
            "postReadbackMeasurements": list(self.post_readback_measurements),
            "proceedCondition": self.proceed_condition,
            "onFailureRollback": self.on_failure_rollback,
            "invalidatesStepIds": list(self.invalidates_step_ids),
            "revalidation": self.revalidation,
            "held": self.held,
            "holdReason": self.hold_reason,
            "affectedScope": list(self.affected_scope),
        }


@dataclass(frozen=True)
class XAppExecutionPlan:
    """The ordered, typed execution plan awaiting Kernel admission."""

    plan_id: str
    created_at: str
    objective_refs: Tuple[str, ...]
    candidate_set_ids: Tuple[str, ...]
    snapshot_ids: Tuple[str, ...]
    steps: Tuple[XAppExecutionStep, ...]
    rollback_order: Tuple[str, ...]
    abort_conditions: Tuple[str, ...]
    combined_harm_refs: Tuple[str, ...]
    watchdog_refs: Tuple[str, ...]
    deadline: str
    expires_at: str
    admission_note: str = KERNEL_ADMISSION_REQUIRED_NOTE

    def step_for(self, action_id: str) -> XAppExecutionStep:
        for step in self.steps:
            if step.action_id == action_id:
                return step
        raise ValueError(f"no step for action {action_id!r}")

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "planId": self.plan_id,
            "createdAt": self.created_at,
            "objectiveRefs": list(self.objective_refs),
            "candidateSetIds": list(self.candidate_set_ids),
            "snapshotIds": list(self.snapshot_ids),
            "steps": [step.to_canonical_dict() for step in self.steps],
            "rollbackOrder": list(self.rollback_order),
            "abortConditions": list(self.abort_conditions),
            "combinedHarmRefs": list(self.combined_harm_refs),
            "watchdogRefs": list(self.watchdog_refs),
            "deadline": self.deadline,
            "expiresAt": self.expires_at,
            "admissionNote": self.admission_note,
        }

    def content_hash(self) -> str:
        return content_hash(self.to_canonical_dict())


@dataclass(frozen=True)
class ExecutionCoordinationResult:
    """Either a plan, or the typed reason there is none.  Never both empty."""

    outcome: CoordinationOutcome
    plan: Optional[XAppExecutionPlan] = None
    refusals: Tuple[StepRefusal, ...] = ()
    replan_required: bool = False
    detail: str = ""


_LIVE_STATE_TO_OUTCOME: Mapping[LiveSelectionState, CoordinationOutcome] = {
    LiveSelectionState.NOT_REGISTERED: CoordinationOutcome.UNSUPPORTED_CAPABILITY,
    LiveSelectionState.NOT_IN_COORDINATED_LIVE_SET: CoordinationOutcome.UNSUPPORTED_CAPABILITY,
    LiveSelectionState.NO_RUNTIME_STATUS: CoordinationOutcome.NOT_WIRED,
    LiveSelectionState.NOT_DEPLOYED: CoordinationOutcome.NOT_WIRED,
    LiveSelectionState.UNHEALTHY: CoordinationOutcome.NOT_WIRED,
    LiveSelectionState.NOT_WIRED: CoordinationOutcome.NOT_WIRED,
    LiveSelectionState.SERVICE_MODEL_UNAVAILABLE: CoordinationOutcome.NOT_WIRED,
    LiveSelectionState.STALE_HEARTBEAT: CoordinationOutcome.NOT_WIRED,
    LiveSelectionState.PAUSED: CoordinationOutcome.NOT_WIRED,
}


class XAppExecutionCoordinator:
    """Maps Candidate Action Sets onto live xApps and orders their steps."""

    def __init__(
        self,
        *,
        registry: XAppCapabilityRegistry,
        snapshot_freshness_bound_ms: int = 5_000,
        plan_deadline_ms: int = 60_000,
    ) -> None:
        self._registry = registry
        self._freshness_bound_ms = snapshot_freshness_bound_ms
        self._plan_deadline_ms = plan_deadline_ms

    # -- planning ----------------------------------------------------------

    def plan(
        self,
        candidate_sets: Sequence[CandidateActionSet],
        *,
        now: str,
        recent_executions: Sequence[RecentExecution] = (),
    ) -> ExecutionCoordinationResult:
        """Produce a plan, or a typed refusal.  Actions are never modified."""
        if not candidate_sets:
            return ExecutionCoordinationResult(
                CoordinationOutcome.REPLAN_REQUIRED, replan_required=True,
                detail="no candidate action set was given")
        for candidate_set in candidate_sets:
            if not isinstance(candidate_set, CandidateActionSet):
                raise TypeError("plan() takes CandidateActionSet instances")

        stale = self._stale_refusals(candidate_sets, now, recent_executions)
        if stale:
            return ExecutionCoordinationResult(
                CoordinationOutcome.STALE_SNAPSHOT, refusals=tuple(stale),
                replan_required=True,
                detail="one or more sets rest on stale KPI evidence; "
                       "collect a fresh snapshot and replan")

        contradiction = self._cross_set_contradiction(candidate_sets)
        if contradiction is not None:
            return ExecutionCoordinationResult(
                CoordinationOutcome.REPLAN_REQUIRED,
                refusals=(contradiction,), replan_required=True,
                detail="contradictory Actions reached the execution "
                       "coordinator; failing closed")

        assignments, blocking = self._assign_owners(candidate_sets, now)
        if blocking:
            worst = blocking[0].outcome
            return ExecutionCoordinationResult(
                worst, refusals=tuple(blocking), replan_required=True,
                detail="at least one Action has no executable xApp; the "
                       "Action is returned unchanged for replanning")

        policy_refusal = self._cooldown_refusal(assignments, recent_executions, now)
        if policy_refusal is not None:
            return ExecutionCoordinationResult(
                CoordinationOutcome.BLOCKED_BY_POLICY,
                refusals=(policy_refusal,), replan_required=True,
                detail="an opposite Action is still inside its owner's "
                       "cooldown window")

        steps, refusals = self._order_steps(assignments, candidate_sets)
        plan = self._assemble_plan(candidate_sets, steps, now)
        replan_required = any(step.revalidation == "REPLAN_REQUIRED"
                              for step in plan.steps)
        return ExecutionCoordinationResult(
            CoordinationOutcome.PLANNED, plan=plan, refusals=tuple(refusals),
            replan_required=replan_required)

    # -- refusal passes ----------------------------------------------------

    def _stale_refusals(
        self,
        candidate_sets: Sequence[CandidateActionSet],
        now: str,
        recent_executions: Sequence[RecentExecution],
    ) -> List[StepRefusal]:
        refusals: List[StepRefusal] = []
        for candidate_set in candidate_sets:
            age_ms = (parse_utc(now) - parse_utc(candidate_set.snapshot_taken_at)) \
                .total_seconds() * 1000
            if candidate_set.is_expired(now):
                refusals.append(StepRefusal(
                    candidate_set.set_id, CoordinationOutcome.STALE_SNAPSHOT,
                    f"candidate set expired at {candidate_set.expires_at}"))
            elif not 0 <= age_ms <= self._freshness_bound_ms:
                refusals.append(StepRefusal(
                    candidate_set.set_id, CoordinationOutcome.STALE_SNAPSHOT,
                    f"snapshot {candidate_set.snapshot_id} age {age_ms:.0f} ms "
                    f"exceeds {self._freshness_bound_ms} ms"))
            else:
                for recent in recent_executions:
                    if parse_utc(candidate_set.snapshot_taken_at) \
                            < parse_utc(recent.completed_at):
                        refusals.append(StepRefusal(
                            candidate_set.set_id,
                            CoordinationOutcome.STALE_SNAPSHOT,
                            f"snapshot {candidate_set.snapshot_id} predates the "
                            f"approved execution of {recent.action_id} on "
                            f"{recent.target_key}; planning (or rolling back) "
                            "from it could silently revert that approved "
                            "Action"))
                        break
        return refusals

    @staticmethod
    def _target_key(action: RecommendedAction) -> str:
        selector = dict(action.target_selector)
        identity_keys = ("objectiveUeId", "cellId", "sst", "sd")
        parts = [f"{key}={selector[key]}" for key in identity_keys
                 if key in selector]
        identity_parameters = {"rnti", "sst", "sd", "targetPrimaryCellId"}
        for name in sorted(action.parameters):
            if name in identity_parameters:
                parts.append(f"{name}={action.parameters[name]}")
        return ";".join(parts) if parts else action.action_id

    def _cross_set_contradiction(
        self, candidate_sets: Sequence[CandidateActionSet]
    ) -> Optional[StepRefusal]:
        seen: Dict[Tuple[str, str], Tuple[str, Mapping[str, Any]]] = {}
        for candidate_set in candidate_sets:
            for action in candidate_set.actions:
                key = (action.action_id, self._target_key(action))
                earlier = seen.get(key)
                if earlier is not None \
                        and dict(earlier[1]) != dict(action.parameters):
                    return StepRefusal(
                        action.action_id, CoordinationOutcome.REPLAN_REQUIRED,
                        f"{action.action_id} on {key[1]!r} is set to different "
                        f"values by {earlier[0]} and {candidate_set.set_id}; "
                        "fail closed and replan")
                seen[key] = (candidate_set.set_id, action.parameters)
        return None

    def _assign_owners(
        self, candidate_sets: Sequence[CandidateActionSet], now: str
    ) -> Tuple[List[Tuple[CandidateActionSet, RecommendedAction,
                          XAppCapabilityManifest]], List[StepRefusal]]:
        assignments: List[Tuple[CandidateActionSet, RecommendedAction,
                                XAppCapabilityManifest]] = []
        blocking: List[StepRefusal] = []
        for candidate_set in candidate_sets:
            for action in candidate_set.actions:
                selection = self._registry.live_xapp_for(action.action_id, now=now)
                if selection.selectable:
                    assignments.append((candidate_set, action, selection.manifest))
                    continue
                outcome = _LIVE_STATE_TO_OUTCOME[selection.state]
                blocking.append(StepRefusal(
                    action.action_id, outcome,
                    f"{selection.state.value}: {selection.detail}",
                    xapp_id=None if selection.manifest is None
                    else selection.manifest.xapp_id))
        order = {CoordinationOutcome.UNSUPPORTED_CAPABILITY: 0,
                 CoordinationOutcome.NOT_WIRED: 1}
        blocking.sort(key=lambda refusal: (order.get(refusal.outcome, 2),
                                           refusal.action_id))
        return assignments, blocking

    def _cooldown_refusal(
        self,
        assignments: Sequence[Tuple[CandidateActionSet, RecommendedAction,
                                    XAppCapabilityManifest]],
        recent_executions: Sequence[RecentExecution],
        now: str,
    ) -> Optional[StepRefusal]:
        for candidate_set, action, manifest in assignments:
            target_key = self._target_key(action)
            for recent in recent_executions:
                if recent.action_id != action.action_id \
                        or recent.target_key != target_key:
                    continue
                age_ms = (parse_utc(now) - parse_utc(recent.completed_at)) \
                    .total_seconds() * 1000
                if age_ms >= manifest.cooldown_ms:
                    continue
                if self._is_opposite(action.parameters,
                                     recent.applied_parameters,
                                     recent.previous_parameters):
                    return StepRefusal(
                        action.action_id, CoordinationOutcome.BLOCKED_BY_POLICY,
                        f"opposite Action on {target_key!r} inside "
                        f"{manifest.xapp_id}'s {manifest.cooldown_ms} ms "
                        "cooldown window", xapp_id=manifest.xapp_id)
        return None

    @staticmethod
    def _is_opposite(
        proposed: Mapping[str, Any],
        applied: Mapping[str, Any],
        previous: Mapping[str, Any],
    ) -> bool:
        """True when the proposal reverses the direction of the last change.

        Undeterminable direction counts as opposite: inside a cooldown window
        the conservative reading is refusal, not permission.
        """
        determinable = False
        for name, new_value in proposed.items():
            if name not in applied or name not in previous:
                continue
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       for v in (new_value, applied[name], previous[name])):
                continue
            last_delta = applied[name] - previous[name]
            new_delta = new_value - applied[name]
            if last_delta == 0 or new_delta == 0:
                continue
            determinable = True
            if (last_delta > 0) != (new_delta > 0):
                return True
        return not determinable

    # -- ordering ----------------------------------------------------------

    def _order_steps(
        self,
        assignments: Sequence[Tuple[CandidateActionSet, RecommendedAction,
                                    XAppCapabilityManifest]],
        candidate_sets: Sequence[CandidateActionSet],
    ) -> Tuple[List[XAppExecutionStep], List[StepRefusal]]:
        steer_entries = [(candidate_set, action, manifest)
                         for candidate_set, action, manifest in assignments
                         if action.action_family == "steer"]
        steer_targets = {
            str(action.parameters.get("targetPrimaryCellId"))
            for _, action, _ in steer_entries
        }
        steered_ues = {
            str(action.target_selector.get("objectiveUeId"))
            for _, action, _ in steer_entries
        }

        refusals: List[StepRefusal] = []
        ordered: List[Tuple[int, CandidateActionSet, RecommendedAction,
                            XAppCapabilityManifest, Dict[str, Any]]] = []

        for candidate_set, action, manifest in assignments:
            extras: Dict[str, Any] = {}
            layer = 1
            if action.action_family == "steer":
                layer = 1
            elif action.action_family in {"cap", "priority"}:
                ue = str(action.target_selector.get("objectiveUeId"))
                if ue in steered_ues:
                    layer = 2
                    extras["depends_on_steer"] = True
                    stated_cell = action.target_selector.get("servingCellId")
                    steer_target = self._steer_target_for_ue(steer_entries, ue)
                    if stated_cell is not None and steer_target is not None \
                            and str(stated_cell) != str(steer_target):
                        # The scheduler step was composed against the source
                        # cell; the handover invalidates it.
                        extras["invalidated_by_steer"] = True
                        extras["revalidation"] = "REPLAN_REQUIRED"
                        refusals.append(StepRefusal(
                            action.action_id,
                            CoordinationOutcome.REPLAN_REQUIRED,
                            f"source-cell step (servingCellId={stated_cell}) is "
                            "invalidated by the handover to "
                            f"{steer_target}; replan on a post-handover "
                            "snapshot or revalidate against the target cell",
                            xapp_id=manifest.xapp_id))
                    else:
                        extras["revalidation"] = "REVALIDATE_AGAINST_TARGET_CELL"
            elif action.action_family == "rfatt":
                cell = str(action.target_selector.get("cellId"))
                if cell in steer_targets:
                    layer = 2
                    extras["held_for_steer"] = True
                    refusals.append(StepRefusal(
                        action.action_id, CoordinationOutcome.BLOCKED_BY_SAFETY,
                        f"cell {cell} is the handover target of a steering "
                        "Action in this plan; the power change is held until "
                        "the handover readback and a fresh KPI snapshot prove "
                        "target-cell safety", xapp_id=manifest.xapp_id))
            elif action.action_family in {"quota", "mcs"}:
                layer = 1
            ordered.append((layer, candidate_set, action, manifest, extras))

        ordered.sort(key=lambda item: (item[0], item[2].action_id))
        steps: List[XAppExecutionStep] = []
        step_ids: Dict[str, str] = {}
        for index, (layer, candidate_set, action, manifest, extras) \
                in enumerate(ordered, start=1):
            step_id = f"step/{index:03d}/{manifest.xapp_id.split('/')[-1]}/{action.action_id}"
            step_ids[action.action_id] = step_id
            steps.append(self._build_step(
                step_id, layer, candidate_set, action, manifest, extras))

        # A successful steer invalidates the source-cell steps behind it.
        invalidated = tuple(
            step_ids[action.action_id]
            for _, _, action, _, extras in ordered
            if extras.get("invalidated_by_steer"))
        if invalidated:
            steps = [
                step if step.action_family != "steer" else XAppExecutionStep(
                    **{**_step_kwargs(step),
                       "invalidates_step_ids": invalidated})
                for step in steps
            ]
        return steps, refusals

    @staticmethod
    def _steer_target_for_ue(
        steer_entries: Sequence[Tuple[CandidateActionSet, RecommendedAction,
                                      XAppCapabilityManifest]],
        ue: str,
    ) -> Optional[str]:
        for _, action, _ in steer_entries:
            if str(action.target_selector.get("objectiveUeId")) == ue:
                return str(action.parameters.get("targetPrimaryCellId"))
        return None

    def _build_step(
        self,
        step_id: str,
        layer: int,
        candidate_set: CandidateActionSet,
        action: RecommendedAction,
        manifest: XAppCapabilityManifest,
        extras: Mapping[str, Any],
    ) -> XAppExecutionStep:
        binding = manifest.binding_for(action.action_id)
        preconditions: List[StepPrecondition] = [
            StepPrecondition(PreconditionKind.SELECTOR_VERIFIED, text)
            for text in action.preconditions
        ]
        post_measurements: List[str] = [binding.readback_measurement_ref]
        proceed = "readback confirms the applied value"
        held = False
        hold_reason = ""
        revalidation = str(extras.get("revalidation", ""))

        if action.action_family == "steer":
            post_measurements.append(SERVING_CELL_ATTRIBUTION_COUNTER)
            proceed = (
                "handover readback confirmed; the UE's new serving cell and "
                "RNTI are re-read from fresh KPM attribution and a new common "
                "KPI snapshot is collected before any dependent step")
        if extras.get("depends_on_steer"):
            ue = str(action.target_selector.get("objectiveUeId"))
            preconditions.extend((
                StepPrecondition(PreconditionKind.PRIMARY_READBACK_CONFIRMED,
                                 "cell-steering",
                                 "the handover readback must confirm before "
                                 "this step is considered"),
                StepPrecondition(PreconditionKind.SERVING_CELL_REVERIFIED, ue,
                                 "serving cell re-read from fresh attribution"),
                StepPrecondition(PreconditionKind.RNTI_REVERIFIED, ue,
                                 "RNTI is cell-local; re-resolve after the "
                                 "handover readback"),
                StepPrecondition(PreconditionKind.FRESH_SNAPSHOT_REQUIRED,
                                 candidate_set.snapshot_id,
                                 "a post-handover common KPI snapshot is "
                                 "required"),
            ))
        if extras.get("held_for_steer"):
            held = True
            hold_reason = (
                "the target cell's state was an input to the handover "
                "decision; hold until the handover readback and a fresh KPI "
                "snapshot prove the change is safe")
            preconditions.extend((
                StepPrecondition(PreconditionKind.PRIMARY_READBACK_CONFIRMED,
                                 "cell-steering", hold_reason),
                StepPrecondition(PreconditionKind.FRESH_SNAPSHOT_REQUIRED,
                                 candidate_set.snapshot_id,
                                 "post-handover snapshot must precede the "
                                 "power change"),
            ))

        affected: Tuple[str, ...] = ()
        if action.action_family == "rfatt":
            affected = self._affected_ues(candidate_set, action)
            if not affected:
                preconditions.append(StepPrecondition(
                    PreconditionKind.FRESH_SNAPSHOT_REQUIRED,
                    str(action.target_selector.get("cellId")),
                    "the snapshot carries no attribution for this cell; the "
                    "affected active UE range must be measured before a "
                    "cell-wide power change"))

        return XAppExecutionStep(
            step_id=step_id,
            xapp_id=manifest.xapp_id,
            action_id=action.action_id,
            action_family=action.action_family,
            parameters=action.parameters,
            target_selector=action.target_selector,
            execution_priority=layer,
            candidate_set_id=candidate_set.set_id,
            preconditions=tuple(preconditions),
            required_permit_kinds=("PREPARE", "COMMIT"),
            readback_measurement_ref=binding.readback_measurement_ref,
            post_readback_measurements=tuple(post_measurements),
            proceed_condition=proceed,
            on_failure_rollback=(
                f"reverse this step from its recorded baseline, then continue "
                f"reverse rollback per the plan's rollback order"),
            revalidation=revalidation,
            held=held,
            hold_reason=hold_reason,
            affected_scope=affected,
        )

    @staticmethod
    def _affected_ues(candidate_set: CandidateActionSet,
                      action: RecommendedAction) -> Tuple[str, ...]:
        """Active UEs on the power-changed cell, from delivered attribution.

        The candidate set does not embed the snapshot, so this reads the
        attribution the composition coordinator copied into the recommended
        action's current-value/selector declarations when available;
        otherwise the caller-provided selector's ``affectedUeIds`` claim.  An
        empty answer stays empty -- the plan then requires measurement first.
        """
        claimed = action.target_selector.get("affectedUeIds")
        if isinstance(claimed, (list, tuple)):
            return tuple(str(item) for item in claimed)
        return ()

    def _assemble_plan(
        self,
        candidate_sets: Sequence[CandidateActionSet],
        steps: Sequence[XAppExecutionStep],
        now: str,
    ) -> XAppExecutionPlan:
        harm_refs = []
        watchdog_refs = []
        abort_conditions = []
        for candidate_set in candidate_sets:
            for bound in candidate_set.combined_harm.by_kind.values():
                harm_refs.extend(bound.constituent_harm_contract_refs)
            for watchdog in candidate_set.combined_harm.required_watchdogs:
                watchdog_refs.append(watchdog.contract_id)
            for action in candidate_set.actions:
                abort_conditions.extend(action.abort_conditions)
        body = {
            "sets": [candidate_set.set_id for candidate_set in candidate_sets],
            "steps": [step.to_canonical_dict() for step in steps],
        }
        expires_at = min(candidate_set.expires_at
                         for candidate_set in candidate_sets)
        return XAppExecutionPlan(
            plan_id=f"xapp-plan/{content_hash(body)[:16]}",
            created_at=now,
            objective_refs=tuple(dict.fromkeys(
                candidate_set.policy_ref for candidate_set in candidate_sets)),
            candidate_set_ids=tuple(candidate_set.set_id
                                    for candidate_set in candidate_sets),
            snapshot_ids=tuple(dict.fromkeys(
                candidate_set.snapshot_id for candidate_set in candidate_sets)),
            steps=tuple(steps),
            rollback_order=tuple(step.step_id for step in reversed(steps)),
            abort_conditions=tuple(dict.fromkeys(abort_conditions)),
            combined_harm_refs=tuple(dict.fromkeys(harm_refs)),
            watchdog_refs=tuple(dict.fromkeys(watchdog_refs)),
            deadline=advance_timestamp(now, self._plan_deadline_ms),
            expires_at=expires_at,
        )


def _step_kwargs(step: XAppExecutionStep) -> Dict[str, Any]:
    return {
        "step_id": step.step_id,
        "xapp_id": step.xapp_id,
        "action_id": step.action_id,
        "action_family": step.action_family,
        "parameters": step.parameters,
        "target_selector": step.target_selector,
        "execution_priority": step.execution_priority,
        "candidate_set_id": step.candidate_set_id,
        "preconditions": step.preconditions,
        "required_permit_kinds": step.required_permit_kinds,
        "readback_measurement_ref": step.readback_measurement_ref,
        "post_readback_measurements": step.post_readback_measurements,
        "proceed_condition": step.proceed_condition,
        "on_failure_rollback": step.on_failure_rollback,
        "invalidates_step_ids": step.invalidates_step_ids,
        "revalidation": step.revalidation,
        "held": step.held,
        "hold_reason": step.hold_reason,
        "affected_scope": step.affected_scope,
    }
