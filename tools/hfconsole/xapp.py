"""Run the verified xApp coordination layer, hardware-free, for one objective.

This is the connection the whole exercise is about: an operator's intent, for a
submittable objective, drives the **Action Composition Coordinator** (which
Action composition serves the objective, and how a cap-vs-priority conflict is
resolved by role separation) and then the **specialist xApp executors** that
actually apply each Action — here over
:class:`assurance.xapps.executors.HardwareFreeConfigStore`, each write gated by a
real :class:`KernelToken`, so the single-writer invariant is exercised with no
radio attached.

It is the same ``assurance/xapps`` layer that was verified over the air this
campaign (composition -> execution -> E2SM-RC control); only the actuation
backend is swapped for the in-memory config store.  Nothing here is OTA
evidence, and ``for_live`` is taken from the registry, not asserted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Tuple

from assurance.advisors.action_space import AdvisoryAction
from assurance.collector.samples import ClockHealth, RawSample
from assurance.contracts.capability import DeploymentBinding
from assurance.contracts.live_binding import load_assurance_live_binding
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.gateway.token import KernelToken, TokenKind
from assurance.objectives import record_for
from assurance.xapps import (
    CellPowerXApp,
    HardwareFreeConfigStore,
    TrafficSteeringXApp,
    UeSchedulerXApp,
    default_capability_manifests,
    snapshot_from_samples,
)
from assurance.xapps.assignment import XAppExecutionAssignment
from assurance.xapps.composition import ActionCompositionCoordinator

from tools.hfconsole.build import (
    DEFAULT_BINDING,
    DEFAULT_HOME_NCI,
    DEFAULT_TARGET_NCI,
)

#: Two UEs sharing the source cell: the objective's target UE (whose SLA the
#: intent serves) and a heavy neighbour (whose cap frees resources for it).
_TARGET_UE = "ue-target"
_HEAVY_UE = "ue-heavy"
_TARGET_RNTI = 0x2222
_HEAVY_RNTI = 0x1111

_NOW = "2026-08-31T10:00:01.000000Z"
_TAKEN = "2026-08-31T10:00:00.000000Z"
_LEASE = "2026-08-31T10:10:00.000000Z"
_TRACE = "a" * 64
_CONFIG = "b" * 64

#: action_id -> (specialist xApp class, its xapp_id in the default manifests).
_SPECIALIST = {
    "cell-steering": (TrafficSteeringXApp, "xapp/traffic-steering"),
    "ue-dl-prb-cap": (UeSchedulerXApp, "xapp/ue-scheduler"),
    "scheduler-priority": (UeSchedulerXApp, "xapp/ue-scheduler"),
    "dl-rf-attenuation": (CellPowerXApp, "xapp/cell-power"),
}


class XAppRoundTripError(RuntimeError):
    """The xApp coordination round-trip could not be composed."""


@dataclass(frozen=True)
class ActionActuation:
    """One resolved Action, applied hardware-free under a permit."""

    action_id: str
    xapp_id: str
    status: str
    permit_ref: str
    store_after: Mapping[str, Any]


@dataclass(frozen=True)
class XAppRoundTrip:
    """The composition decision and the hardware-free actuation that followed."""

    objective_family: str
    accepted: bool
    apply_order: Tuple[str, ...] = ()
    rollback_order: Tuple[str, ...] = ()
    actuations: Tuple[ActionActuation, ...] = ()
    refusal_type: str = ""
    refusal: str = ""


def _deployment(binding_path: str = DEFAULT_BINDING) -> DeploymentBinding:
    return load_assurance_live_binding(binding_path).r1.deployment


def _sample(sample_id: str, value: int, ue_id: str, rnti: int,
            cell: str, sequence: int) -> RawSample:
    return RawSample(
        sample_id=sample_id, counter_id="UE.ServingCell",
        value=TypedQuantity(value, "NCI", Provenance.MEASURED, f"raw/{sample_id}"),
        scope_snapshot={"ueId": ue_id, "cellId": cell, "rnti": f"{rnti:#06x}"},
        observed_at=_TAKEN, cadence_ms=1000,
        clock_health=ClockHealth.SYNCHRONISED, trace_hash=_TRACE, sequence=sequence)


def _attribution_snapshot(source_cell: str) -> Any:
    """Both UEs attributed on the source cell — the input the coordinator needs."""
    return snapshot_from_samples(
        snapshot_id="snap/hf-xapp", taken_at=_TAKEN,
        samples=[
            _sample("s-target", int(source_cell), _TARGET_UE, _TARGET_RNTI, source_cell, 0),
            _sample("s-heavy", int(source_cell), _HEAVY_UE, _HEAVY_RNTI, source_cell, 1),
        ])


def _action(action_id: str, *, source_cell: str, target_cell: str,
            same_ue_conflict: bool) -> Optional[AdvisoryAction]:
    """Build one representative proposal for a policy action id, or None."""
    if action_id == "cell-steering":
        return AdvisoryAction(
            "cell-steering", {"targetPrimaryCellId": int(target_cell)},
            {"objectiveUeId": _TARGET_UE})
    if action_id == "ue-dl-prb-cap":
        cap_rnti = _TARGET_RNTI if same_ue_conflict else _HEAVY_RNTI
        return AdvisoryAction(
            "ue-dl-prb-cap", {"rnti": cap_rnti, "maxDlPrbs": 20},
            {"objectiveUeId": _TARGET_UE, "controlledUeRole": "NON_TARGET_HEAVY_UE",
             "rntiBinding": "RESOLVE_AFTER_PRIMARY_READBACK",
             "sliceRelation": "OUTSIDE_OBJECTIVE_SLICE"})
    if action_id == "scheduler-priority":
        return AdvisoryAction(
            "scheduler-priority", {"rnti": _TARGET_RNTI, "pfWeight": 2.0},
            {"objectiveUeId": _TARGET_UE, "controlledUeRole": "TARGET_UE",
             "rntiBinding": "RESOLVE_AFTER_PRIMARY_READBACK",
             "servingCellId": source_cell})
    return None


def _combination_for(objective_family: str, *, same_ue_conflict: bool) -> Tuple[str, ...]:
    """A valid allowed combination for the objective, from the frozen policy.

    Picks the largest allowed combination whose actions this demo can build, so
    the composition exercised is the richest one the objective permits.  For the
    conflict demo we still send steer+cap+priority so the role-separation
    constraint (not the allowed-combination gate) is what refuses.
    """
    from assurance.actions.composition_policy import OBJECTIVE_ACTION_POLICIES

    if same_ue_conflict:
        return ("cell-steering", "ue-dl-prb-cap", "scheduler-priority")
    policy = OBJECTIVE_ACTION_POLICIES.get(objective_family)
    if policy is None:
        return ("cell-steering",)
    buildable = set(_SPECIALIST)
    combos = [tuple(c) for c in policy.allowed_combinations
              if set(c).issubset(buildable)]
    if not combos:
        return ("cell-steering",)
    return max(combos, key=len)


def representative_proposals(
    objective_family: str, *, source_cell: str, target_cell: str,
    same_ue_conflict: bool = False,
) -> Sequence[AdvisoryAction]:
    """Representative advisory proposals for the objective's allowed composition.

    Values are caller proposals, not derived here (the PROPOSAL_VALUE_BOUNDARY).
    ``same_ue_conflict`` puts cap and priority on the *same* UE to exercise the
    coordinator's ``qos-resource-role-separation`` refusal.
    """
    combination = _combination_for(objective_family, same_ue_conflict=same_ue_conflict)
    proposals = []
    for action_id in combination:
        action = _action(action_id, source_cell=source_cell,
                         target_cell=target_cell, same_ue_conflict=same_ue_conflict)
        if action is not None:
            proposals.append(action)
    return tuple(proposals)


def _permit(sequence: int) -> KernelToken:
    """A COMMIT permit shape the specialist executor accepts.

    A real integration issues this from the Kernel's Write Gateway; here it is a
    correctly-formed :class:`KernelToken` so the executor's single-writer check
    runs for real (right kind, live lease, matched content hash).
    """
    return KernelToken(
        token_kind=TokenKind.COMMIT, transaction_id="txn/hf-xapp",
        trial_id="case/hf-xapp:trial:1", fencing_token=1,
        command_sequence=sequence, lease_expiry=_LEASE,
        expected_config_hash=_CONFIG, idempotency_key=f"idem/hf-xapp/{sequence}",
        issued_at=_TAKEN)


def _actuate(action: Any, deployment: DeploymentBinding, snapshot: Any,
             sequence: int) -> ActionActuation:
    spec = _SPECIALIST.get(action.action_id)
    if spec is None:
        raise XAppRoundTripError(
            f"no specialist xApp for action {action.action_id!r}")
    xapp_cls, xapp_id = spec
    manifests = {m.xapp_id: m for m in default_capability_manifests(deployment)}
    store = HardwareFreeConfigStore()
    executor = xapp_cls(manifest=manifests[xapp_id], store=store)
    token = _permit(sequence)
    assignment = XAppExecutionAssignment(
        assignment_id=f"assignment/hf/{action.action_id}",
        plan_id="xapp-plan/hf", step_id=f"step/hf/{action.action_id}",
        xapp_id=xapp_id, action_id=action.action_id,
        parameters=dict(action.parameters),
        target_selector=dict(action.target_selector),
        snapshot_id=snapshot.snapshot_id, snapshot_hash=snapshot.content_hash(),
        preconditions=(), deadline="2026-08-31T10:05:00.000000Z",
        permit_ref=token.content_hash())
    report = executor.execute(assignment, permit=token, snapshot=snapshot, now=_NOW)
    return ActionActuation(
        action_id=action.action_id, xapp_id=xapp_id,
        status=str(report.status), permit_ref=token.content_hash(),
        store_after=dict(store.as_dict()))


def run_xapp_round_trip(
    objective_family: str,
    *,
    deployment: Optional[DeploymentBinding] = None,
    source_cell: str = str(DEFAULT_HOME_NCI),
    target_cell: str = str(DEFAULT_TARGET_NCI),
    same_ue_conflict: bool = False,
    actuate: bool = True,
) -> XAppRoundTrip:
    """Compose the objective's xApp Action set, then apply it hardware-free.

    * runs :meth:`ActionCompositionCoordinator.recommend` for the objective;
    * on acceptance, applies each Action through its owning specialist xApp over
      an in-memory config store, each write gated by a COMMIT permit;
    * on refusal (e.g. cap and priority on the same UE), returns the refusal so
      the caller can show what the coordinator prevented.
    """
    dep = deployment or _deployment()
    snapshot = _attribution_snapshot(source_cell)
    proposals = representative_proposals(
        objective_family, source_cell=source_cell, target_cell=target_cell,
        same_ue_conflict=same_ue_conflict)
    coordinator = ActionCompositionCoordinator(deployment=dep)
    for_live = record_for(objective_family).deployment_capability.submittable
    try:
        candidate_set = coordinator.recommend(
            objective_family=objective_family, proposals=list(proposals),
            snapshot=snapshot, now=_NOW, for_live=for_live)
    except Exception as exc:  # composition refusals are the point of the demo
        return XAppRoundTrip(
            objective_family=objective_family, accepted=False,
            refusal_type=type(exc).__name__, refusal=str(exc))

    apply_order = tuple(candidate_set.apply_order)
    actuations: Tuple[ActionActuation, ...] = ()
    if actuate:
        by_id = {a.action_id: a for a in proposals}
        applied = []
        for sequence, action_id in enumerate(apply_order):
            action = by_id.get(action_id)
            if action is None:
                continue
            applied.append(_actuate(action, dep, snapshot, sequence))
        actuations = tuple(applied)
    return XAppRoundTrip(
        objective_family=objective_family, accepted=True,
        apply_order=apply_order,
        rollback_order=tuple(candidate_set.rollback_order),
        actuations=actuations)
