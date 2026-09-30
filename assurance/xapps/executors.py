"""Hardware-free specialist xApp executors.

Each executor validates and applies the kind of Action assigned to it, and
nothing else.  It decides no Policy goal, proposes no Action, and refuses
any assignment outside its manifest's closed ownership set.  Its output is a
:class:`~assurance.xapps.assignment.XAppExecutionReport` -- execution status
plus readback -- never a proposal.

**Permit boundary.** Every write goes through the executor's
:class:`HardwareFreeConfigStore` and only under a live, matching
:class:`~assurance.gateway.token.KernelToken`.  A missing, expired, wrong-kind
or mismatched permit raises :class:`PermitRequiredError` before any state is
touched.  This mirrors the Write Gateway's single-writer rule at the
hardware-free layer; it does not replace it -- the store here is an
in-memory stand-in, and no transport of any kind is opened.

**Hardware-free only.** These executors are the coordination layer's
hermetic actuation stand-ins.  The live UE scheduling and cell power
primitives in this repository are lab telnet knobs
(``oai_patches/d2_actionspace_runtime_knobs.patch``) with no deployed E2
path, and the production Traffic Steering xApp is an external release
artifact; neither is executed from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from assurance.gateway.token import KernelToken, TokenKind
from assurance.xapps.assignment import (
    XAppExecutionAssignment, XAppExecutionReport, XAppExecutionStatus,
)
from assurance.xapps.manifest import XAppCapabilityManifest, XAppKind
from assurance.xapps.snapshot import CommonKpiSnapshot

__all__ = [
    "CellPowerXApp",
    "ExecutorError",
    "HardwareFreeConfigStore",
    "PermitRequiredError",
    "SpecialistXAppExecutor",
    "TrafficSteeringXApp",
    "UeSchedulerXApp",
]


class ExecutorError(ValueError):
    """An executor refuses an assignment for a structural reason."""


class PermitRequiredError(ExecutorError):
    """An equipment write was attempted without a valid Write Gateway permit."""


class HardwareFreeConfigStore:
    """A hermetic configuration surface keyed by (action axis, target).

    In-memory only.  It exists so the executors' apply/readback/rollback
    behaviour is testable without any gNB, telnet port, FlexRIC process or
    network -- and so a write without a permit has a concrete thing it is
    blocked from touching.
    """

    def __init__(self, initial: Optional[Mapping[str, Any]] = None) -> None:
        self._values: Dict[str, Any] = dict(initial or {})

    def read(self, axis: str) -> Any:
        return self._values.get(axis)

    def apply(self, axis: str, value: Any) -> None:
        self._values[axis] = value

    def as_dict(self) -> Dict[str, Any]:
        return dict(self._values)


@dataclass(frozen=True)
class _Baseline:
    axis: str
    value: Any


class SpecialistXAppExecutor:
    """Shared fail-closed execution skeleton for the specialist xApps."""

    #: Operation name per owned action id; subclasses fill it in.
    OPERATIONS: Mapping[str, str] = {}

    def __init__(self, *, manifest: XAppCapabilityManifest,
                 store: HardwareFreeConfigStore) -> None:
        if set(self.OPERATIONS) != set(manifest.owned_action_ids):
            raise ExecutorError(
                f"{manifest.xapp_id}: executor operations "
                f"{sorted(self.OPERATIONS)} do not match the manifest's owned "
                f"actions {sorted(manifest.owned_action_ids)}")
        self._manifest = manifest
        self._store = store
        self._baselines: Dict[str, Tuple[_Baseline, ...]] = {}

    @property
    def manifest(self) -> XAppCapabilityManifest:
        return self._manifest

    # -- the one runtime entry point --------------------------------------

    def execute(
        self,
        assignment: XAppExecutionAssignment,
        *,
        permit: KernelToken,
        snapshot: CommonKpiSnapshot,
        now: str,
    ) -> XAppExecutionReport:
        """Validate, apply under permit, read back, report."""
        self._require_permit(assignment, permit, now, TokenKind.COMMIT)
        if assignment.xapp_id != self._manifest.xapp_id:
            return self._report(assignment, XAppExecutionStatus.REJECTED_CAPABILITY,
                                {}, now,
                                f"assignment addressed to {assignment.xapp_id}, "
                                f"this executor is {self._manifest.xapp_id}")
        if not self._manifest.owns_action(assignment.action_id):
            return self._report(
                assignment, XAppExecutionStatus.REJECTED_CAPABILITY, {}, now,
                f"{self._manifest.xapp_id} does not own "
                f"{assignment.action_id!r}; its closed ownership set is "
                f"{sorted(self._manifest.owned_action_ids)}")
        if snapshot.snapshot_id != assignment.snapshot_id \
                or snapshot.content_hash() != assignment.snapshot_hash:
            return self._report(
                assignment, XAppExecutionStatus.REJECTED_STALE_STATE, {}, now,
                "the presented snapshot is not the one the assignment was "
                "planned from")
        stale = self._precheck(assignment, snapshot, now)
        if stale is not None:
            return self._report(assignment,
                                XAppExecutionStatus.REJECTED_STALE_STATE,
                                {}, now, stale)

        axes = self._axes(assignment)
        self._baselines[assignment.assignment_id] = tuple(
            _Baseline(axis, self._store.read(axis)) for axis, _ in axes)
        for axis, value in axes:
            self._store.apply(axis, value)
        readback = {axis: self._store.read(axis) for axis, _ in axes}
        applied = dict(axes)
        if readback != applied:
            return self._report(assignment, XAppExecutionStatus.FAILED,
                                readback, now,
                                "readback does not confirm the applied value")
        return self._report(assignment, XAppExecutionStatus.SUCCEEDED,
                            self._enrich_readback(assignment, snapshot, readback),
                            now, "")

    def rollback(
        self,
        assignment: XAppExecutionAssignment,
        *,
        permit: KernelToken,
        now: str,
    ) -> XAppExecutionReport:
        """Restore the recorded pre-apply baseline, under permit."""
        self._require_permit(assignment, permit, now, TokenKind.REVERSE_ROLLBACK)
        baselines = self._baselines.get(assignment.assignment_id)
        if baselines is None:
            return self._report(assignment, XAppExecutionStatus.ROLLBACK_FAILED,
                                {}, now,
                                "no recorded baseline for this assignment")
        for baseline in reversed(baselines):
            self._store.apply(baseline.axis, baseline.value)
        readback = {baseline.axis: self._store.read(baseline.axis)
                    for baseline in baselines}
        expected = {baseline.axis: baseline.value for baseline in baselines}
        status = XAppExecutionStatus.ROLLBACK_SUCCEEDED \
            if readback == expected else XAppExecutionStatus.ROLLBACK_FAILED
        return self._report(assignment, status, readback, now, "")

    # -- fail-closed internals --------------------------------------------

    def _require_permit(self, assignment: XAppExecutionAssignment,
                        permit: Any, now: str, kind: TokenKind) -> None:
        if not isinstance(permit, KernelToken):
            raise PermitRequiredError(
                "equipment write blocked: no Write Gateway permit "
                "(KernelToken) was presented")
        if not permit.authorises(kind):
            raise PermitRequiredError(
                f"equipment write blocked: permit authorises "
                f"{permit.token_kind.value}, not {kind.value}")
        if permit.is_expired(now):
            raise PermitRequiredError(
                f"equipment write blocked: permit lease expired at "
                f"{permit.lease_expiry}")
        if kind is TokenKind.COMMIT \
                and permit.content_hash() != assignment.permit_ref:
            raise PermitRequiredError(
                "equipment write blocked: the presented permit is not the one "
                "this assignment references")

    def _precheck(self, assignment: XAppExecutionAssignment,
                  snapshot: CommonKpiSnapshot, now: str) -> Optional[str]:
        """Subclass hook: return a stale-state reason, or ``None``."""
        return None

    def _axes(self, assignment: XAppExecutionAssignment) \
            -> Tuple[Tuple[str, Any], ...]:
        """The (config axis, value) pairs this assignment writes."""
        raise NotImplementedError

    def _enrich_readback(self, assignment: XAppExecutionAssignment,
                         snapshot: CommonKpiSnapshot,
                         readback: Dict[str, Any]) -> Dict[str, Any]:
        return readback

    def _report(self, assignment: XAppExecutionAssignment,
                status: XAppExecutionStatus, readback: Mapping[str, Any],
                now: str, detail: str) -> XAppExecutionReport:
        return XAppExecutionReport(
            assignment_id=assignment.assignment_id,
            xapp_id=self._manifest.xapp_id,
            action_id=assignment.action_id,
            status=status,
            readback=readback,
            completed_at=now,
            detail=detail,
        )


class TrafficSteeringXApp(SpecialistXAppExecutor):
    """Executes ``steer`` and nothing else.

    Hardware-free stand-in: the production Traffic Steering xApp is the
    released external artifact reached over R1 -> Non-RT RIC -> A1-P; this
    class exercises the same assignment contract against the hermetic store.
    """

    OPERATIONS = {"cell-steering": "STEER_UE_TO_CELL"}

    def __init__(self, *, manifest: XAppCapabilityManifest,
                 store: HardwareFreeConfigStore) -> None:
        if manifest.kind is not XAppKind.TRAFFIC_STEERING:
            raise ExecutorError("TrafficSteeringXApp needs a TRAFFIC_STEERING manifest")
        super().__init__(manifest=manifest, store=store)

    def _axes(self, assignment: XAppExecutionAssignment) \
            -> Tuple[Tuple[str, Any], ...]:
        ue = assignment.target_selector["objectiveUeId"]
        return ((f"ue/{ue}/servingCell",
                 assignment.parameters["targetPrimaryCellId"]),)


class UeSchedulerXApp(SpecialistXAppExecutor):
    """Executes ``SET_UE_PRB_CAP`` and ``SET_UE_PF_WEIGHT`` and nothing else.

    RNTI is cell-local and changes on handover or re-attach, so the
    precondition check re-verifies the UE's serving cell and RNTI from the
    presented snapshot's KPM attribution immediately before applying.
    """

    OPERATIONS = {
        "ue-dl-prb-cap": "SET_UE_PRB_CAP",
        "scheduler-priority": "SET_UE_PF_WEIGHT",
    }

    def __init__(self, *, manifest: XAppCapabilityManifest,
                 store: HardwareFreeConfigStore) -> None:
        if manifest.kind is not XAppKind.UE_SCHEDULER:
            raise ExecutorError("UeSchedulerXApp needs a UE_SCHEDULER manifest")
        super().__init__(manifest=manifest, store=store)

    def _precheck(self, assignment: XAppExecutionAssignment,
                  snapshot: CommonKpiSnapshot, now: str) -> Optional[str]:
        ue = str(assignment.target_selector.get("objectiveUeId", ""))
        if not ue:
            return "no objectiveUeId in the assignment target selector"
        attribution = snapshot.serving_cell_of(ue)
        if attribution is None:
            return (f"the snapshot carries no serving-cell attribution for "
                    f"{ue!r}; serving cell and RNTI cannot be re-verified")
        expected_cell = assignment.target_selector.get("servingCellId")
        if expected_cell is not None \
                and str(attribution.value.value) != str(expected_cell) \
                and attribution.scope.get("cellId") != str(expected_cell):
            return (f"{ue!r} is no longer served by cell {expected_cell!r}; "
                    "the UE moved and this step must be replanned")
        attributed_rnti = attribution.scope.get("rnti")
        assigned_rnti = assignment.parameters.get("rnti")
        if attributed_rnti is None:
            return (f"the attribution for {ue!r} carries no RNTI; a cell-local "
                    "identifier cannot be assumed")
        if assigned_rnti is not None \
                and int(attributed_rnti, 0) != int(assigned_rnti):
            return (f"RNTI changed for {ue!r}: assignment says "
                    f"{assigned_rnti:#x}, fresh attribution says "
                    f"{attributed_rnti}; re-resolve before executing")
        return None

    def _axes(self, assignment: XAppExecutionAssignment) \
            -> Tuple[Tuple[str, Any], ...]:
        rnti = assignment.parameters["rnti"]
        if assignment.action_id == "ue-dl-prb-cap":
            return ((f"ue/{rnti:#06x}/dlPrbCap",
                     assignment.parameters["maxDlPrbs"]),)
        return ((f"ue/{rnti:#06x}/pfWeight", assignment.parameters["pfWeight"]),)


class CellPowerXApp(SpecialistXAppExecutor):
    """Executes ``SET_CELL_TX_ATTENUATION`` and nothing else.

    Direction (verified against the checked-in knob): ``txAttenuationDb`` is
    attenuation **below** maximum gain, so increasing it REDUCES downlink
    transmit power -- ``executor/oai_executor.py`` models delivered power as
    ``BASE_ATT_DB - tx_att_db`` over the ``ci rfatt`` knob from
    ``oai_patches/d2_actionspace_runtime_knobs.patch``.  The effect is
    cell-wide, so the success readback carries the affected active UE range
    from the presented snapshot alongside the rollback baseline.
    """

    OPERATIONS = {"dl-rf-attenuation": "SET_CELL_TX_ATTENUATION"}

    def __init__(self, *, manifest: XAppCapabilityManifest,
                 store: HardwareFreeConfigStore) -> None:
        if manifest.kind is not XAppKind.CELL_POWER:
            raise ExecutorError("CellPowerXApp needs a CELL_POWER manifest")
        super().__init__(manifest=manifest, store=store)

    def _precheck(self, assignment: XAppExecutionAssignment,
                  snapshot: CommonKpiSnapshot, now: str) -> Optional[str]:
        if not str(assignment.target_selector.get("cellId", "")):
            return "no cellId in the assignment target selector"
        return None

    def _axes(self, assignment: XAppExecutionAssignment) \
            -> Tuple[Tuple[str, Any], ...]:
        cell = assignment.target_selector["cellId"]
        return ((f"cell/{cell}/txAttenuationDb",
                 assignment.parameters["txAttenuationDb"]),)

    def _enrich_readback(self, assignment: XAppExecutionAssignment,
                         snapshot: CommonKpiSnapshot,
                         readback: Dict[str, Any]) -> Dict[str, Any]:
        cell = str(assignment.target_selector["cellId"])
        baselines = self._baselines[assignment.assignment_id]
        enriched = dict(readback)
        enriched["rollbackBaseline"] = {b.axis: b.value for b in baselines}
        enriched["affectedActiveUeIds"] = list(snapshot.active_ue_ids(cell))
        enriched["powerDirectionNote"] = (
            "increased txAttenuationDb lowers DL transmit power "
            "(attenuation below max gain)")
        return enriched
