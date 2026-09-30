"""XApp Capability Registry: registration, lookup, and runtime liveness.

The registry answers two different questions and keeps them apart:

* **Static capability** -- which registered xApp owns which catalog Action.
  Ownership is closed (:data:`~assurance.xapps.manifest.XAPP_KIND_OWNED_ACTIONS`)
  and exclusive: one Action, one owner.
* **Runtime state** -- whether that xApp is deployed, healthy, E2-connected,
  heartbeat-fresh and unpaused *right now* (:class:`XAppRuntimeStatus`).

A capability without a live wire is not a selectable live xApp: selection
returns a typed classification (``NOT_WIRED``, ``STALE_HEARTBEAT``, ...)
instead of quietly treating the manifest as availability.

:func:`default_capability_manifests` builds the five specialist manifests
from the action catalog and records, honestly, what this repository can and
cannot claim for each -- including that the production Traffic Steering xApp
source lives outside this repository, and that the UE scheduling and cell
power primitives are lab telnet knobs with no deployed E2 path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple

from assurance.actions import action_catalog
from assurance.contracts.capability import DeploymentBinding
from assurance.core.timebase import is_utc_timestamp, parse_utc
from assurance.objectives import FAMILY_MODULES, record_for
from assurance.objectives.registry import SupportState
from assurance.xapps.manifest import (
    _parameter_dict, ACTION_FAMILY_BY_ID, KpiRequirementPurpose,
    XAppActionBinding, XAppCapabilityManifest, XAppExecutionPathState,
    XAppKind, XAppKpiRequirement, XAppManifestError, validate_xapp_manifest,
)

__all__ = [
    "CapabilityOwnershipError",
    "DEFAULT_HEARTBEAT_FRESHNESS_MS",
    "INITIAL_COORDINATED_LIVE_SET",
    "LiveSelection",
    "XAppCapabilityRegistry",
    "XAppRegistryError",
    "XAppRuntimeStatus",
    "default_capability_manifests",
]


class XAppRegistryError(ValueError):
    """The registry refuses a registration or a query."""


class CapabilityOwnershipError(XAppRegistryError):
    """An xApp tried to claim an Action outside its closed ownership set."""


#: The initial coordinated live set.  Slice Resource is preserved at its
#: current implementation level but not coordinated live (no slice-scoped
#: measurement or OTA effect oracle); Link Adaptation is excluded because its
#: cell-wide MCS effect is forbidden for every floor objective
#: (``assurance/actions/composition_policy.py`` ``_CELL_FLOOR_FORBIDDEN``).
INITIAL_COORDINATED_LIVE_SET: Tuple[XAppKind, ...] = (
    XAppKind.TRAFFIC_STEERING,
    XAppKind.UE_SCHEDULER,
    XAppKind.CELL_POWER,
)

DEFAULT_HEARTBEAT_FRESHNESS_MS = 10_000


class LiveSelectionState(Enum):
    """Why one xApp is, or is not, selectable for live execution now."""

    LIVE = "LIVE"
    NO_RUNTIME_STATUS = "NO_RUNTIME_STATUS"
    NOT_DEPLOYED = "NOT_DEPLOYED"
    UNHEALTHY = "UNHEALTHY"
    NOT_WIRED = "NOT_WIRED"
    SERVICE_MODEL_UNAVAILABLE = "SERVICE_MODEL_UNAVAILABLE"
    STALE_HEARTBEAT = "STALE_HEARTBEAT"
    PAUSED = "PAUSED"
    NOT_IN_COORDINATED_LIVE_SET = "NOT_IN_COORDINATED_LIVE_SET"
    NOT_REGISTERED = "NOT_REGISTERED"


@dataclass(frozen=True)
class XAppRuntimeStatus:
    """The mutable runtime facts about one registered xApp, as a record."""

    xapp_id: str
    deployed: bool
    healthy: bool
    e2_connected: bool
    service_model_available: bool
    last_heartbeat_at: Optional[str] = None
    paused: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.xapp_id, str) or not self.xapp_id.strip():
            raise XAppRegistryError("runtime status needs a non-empty xapp_id")
        if self.last_heartbeat_at is not None \
                and not is_utc_timestamp(self.last_heartbeat_at):
            raise XAppRegistryError(
                f"last_heartbeat_at is not canonical UTC: {self.last_heartbeat_at!r}")


@dataclass(frozen=True)
class LiveSelection:
    """One selection answer: the xApp, or the reason there is none."""

    state: LiveSelectionState
    manifest: Optional[XAppCapabilityManifest] = None
    detail: str = ""

    @property
    def selectable(self) -> bool:
        return self.state is LiveSelectionState.LIVE


class XAppCapabilityRegistry:
    """Registration and lookup for specialist xApp capability manifests."""

    def __init__(
        self,
        *,
        coordinated_live_set: Tuple[XAppKind, ...] = INITIAL_COORDINATED_LIVE_SET,
        heartbeat_freshness_ms: int = DEFAULT_HEARTBEAT_FRESHNESS_MS,
        deployment: Optional[DeploymentBinding] = None,
    ) -> None:
        if heartbeat_freshness_ms <= 0:
            raise XAppRegistryError("heartbeat_freshness_ms must be positive")
        self._coordinated_live_set = tuple(coordinated_live_set)
        self._heartbeat_freshness_ms = heartbeat_freshness_ms
        self._deployment = deployment
        self._manifests: Dict[str, XAppCapabilityManifest] = {}
        self._owner_by_action: Dict[str, str] = {}
        self._runtime: Dict[str, XAppRuntimeStatus] = {}

    # -- registration ------------------------------------------------------

    def register(self, manifest: XAppCapabilityManifest) -> None:
        """Admit one manifest, fail-closed on every ownership violation."""
        if not isinstance(manifest, XAppCapabilityManifest):
            raise XAppRegistryError("register() takes an XAppCapabilityManifest")
        try:
            validate_xapp_manifest(manifest, deployment=self._deployment)
        except XAppManifestError as exc:
            raise CapabilityOwnershipError(str(exc)) from exc
        if manifest.xapp_id in self._manifests:
            raise XAppRegistryError(f"{manifest.xapp_id}: already registered")
        for action_id in manifest.owned_action_ids:
            holder = self._owner_by_action.get(action_id)
            if holder is not None:
                raise CapabilityOwnershipError(
                    f"{manifest.xapp_id}: action {action_id!r} is already owned by "
                    f"{holder}; one Action has one owner")
        self._manifests[manifest.xapp_id] = manifest
        for action_id in manifest.owned_action_ids:
            self._owner_by_action[action_id] = manifest.xapp_id

    # -- static lookup -----------------------------------------------------

    def manifests(self) -> Tuple[XAppCapabilityManifest, ...]:
        return tuple(self._manifests[xapp_id] for xapp_id in sorted(self._manifests))

    def manifest_for(self, xapp_id: str) -> XAppCapabilityManifest:
        try:
            return self._manifests[xapp_id]
        except KeyError as exc:
            raise XAppRegistryError(f"unknown xApp {xapp_id!r}") from exc

    def owner_of(self, action_id: str) -> Optional[XAppCapabilityManifest]:
        """The registered owner of one catalog Action, or ``None``."""
        xapp_id = self._owner_by_action.get(action_id)
        return None if xapp_id is None else self._manifests[xapp_id]

    def in_coordinated_live_set(self, xapp_id: str) -> bool:
        return self.manifest_for(xapp_id).kind in self._coordinated_live_set

    # -- runtime state -----------------------------------------------------

    def update_runtime_status(self, status: XAppRuntimeStatus) -> None:
        if status.xapp_id not in self._manifests:
            raise XAppRegistryError(
                f"runtime status for unregistered xApp {status.xapp_id!r}")
        self._runtime[status.xapp_id] = status

    def runtime_status(self, xapp_id: str) -> Optional[XAppRuntimeStatus]:
        return self._runtime.get(xapp_id)

    # -- live selection ----------------------------------------------------

    def live_selection(self, xapp_id: str, *, now: str) -> LiveSelection:
        """Whether one xApp may execute live now, with the honest reason."""
        manifest = self._manifests.get(xapp_id)
        if manifest is None:
            return LiveSelection(LiveSelectionState.NOT_REGISTERED,
                                 detail=f"{xapp_id!r} is not registered")
        if manifest.kind not in self._coordinated_live_set:
            return LiveSelection(
                LiveSelectionState.NOT_IN_COORDINATED_LIVE_SET, manifest,
                f"{manifest.kind.value} is outside the coordinated live set")
        status = self._runtime.get(xapp_id)
        if status is None:
            return LiveSelection(LiveSelectionState.NO_RUNTIME_STATUS, manifest,
                                 "no runtime status has been reported")
        if status.paused:
            return LiveSelection(LiveSelectionState.PAUSED, manifest, status.note)
        if not status.deployed:
            return LiveSelection(LiveSelectionState.NOT_DEPLOYED, manifest, status.note)
        if not status.healthy:
            return LiveSelection(LiveSelectionState.UNHEALTHY, manifest, status.note)
        if not status.e2_connected:
            return LiveSelection(
                LiveSelectionState.NOT_WIRED, manifest,
                "capability exists but no live E2 path is connected")
        if not status.service_model_available:
            return LiveSelection(LiveSelectionState.SERVICE_MODEL_UNAVAILABLE,
                                 manifest, status.note)
        if status.last_heartbeat_at is None:
            return LiveSelection(LiveSelectionState.STALE_HEARTBEAT, manifest,
                                 "no heartbeat has been observed")
        age_ms = (parse_utc(now) - parse_utc(status.last_heartbeat_at)) \
            .total_seconds() * 1000
        if not 0 <= age_ms <= self._heartbeat_freshness_ms:
            return LiveSelection(
                LiveSelectionState.STALE_HEARTBEAT, manifest,
                f"heartbeat age {age_ms:.0f} ms exceeds "
                f"{self._heartbeat_freshness_ms} ms")
        return LiveSelection(LiveSelectionState.LIVE, manifest)

    def live_xapp_for(self, action_id: str, *, now: str) -> LiveSelection:
        """The live owner of one Action, or the typed reason there is none."""
        owner = self.owner_of(action_id)
        if owner is None:
            return LiveSelection(
                LiveSelectionState.NOT_REGISTERED,
                detail=f"no registered xApp owns action {action_id!r}")
        return self.live_selection(owner.xapp_id, now=now)


# --------------------------------------------------------------------------- #
# default manifests
# --------------------------------------------------------------------------- #

_TELNET_KNOB_BLOCKER = (
    "No deployed E2SM-RC RAN-function definition advertises this control; the "
    "checked-in primitive is the lab telnet knob from "
    "oai_patches/d2_actionspace_runtime_knobs.patch, which is a "
    "LAB_SETUP-adjacent research path, not the official A1/E2 path. Executing "
    "this Action live over E2 requires an encoder, an OAI E2 Agent handler and "
    "an advertised RAN-function definition that do not exist in this "
    "repository."
)


def _kpm_attribution_requirements(purposes: Tuple[KpiRequirementPurpose, ...]) \
        -> Tuple[XAppKpiRequirement, ...]:
    """The per-UE serving-cell attribution KPI, from the frozen TS family."""
    module = FAMILY_MODULES["TrafficSteeringPreference"]()
    found = []
    for kpi in module.kpi_declaration():
        if "UE" in kpi.scope_level and "KPM" in kpi.source_interface:
            for purpose in purposes:
                found.append(XAppKpiRequirement(
                    measurement_ref=kpi.measurement_ref,
                    purpose=purpose,
                    scope_level=kpi.scope_level,
                    source_interface=kpi.source_interface,
                    basis=("assurance/objectives/traffic_steering.py "
                           "kpi_declaration(); E2SM-KPM Style 4 UE attribution"),
                ))
    if not found:
        raise XAppRegistryError(
            "the TrafficSteeringPreference family no longer declares a KPM "
            "UE-attribution KPI; the default manifests cannot be built honestly")
    return tuple(found)


def _readback_requirement(action_id: str, contract) -> XAppKpiRequirement:
    return XAppKpiRequirement(
        measurement_ref=contract.binding.readback_measurement_ref,
        purpose=KpiRequirementPurpose.READBACK,
        scope_level=contract.binding.parameters[0].scope,
        source_interface="CONFIGURATION_READBACK",
        basis=f"assurance/actions/catalog.py action {action_id!r} readback contract",
    )


def _binding_from_catalog(contract) -> XAppActionBinding:
    return XAppActionBinding(
        action_id=contract.action_id,
        action_family=ACTION_FAMILY_BY_ID[contract.action_id],
        service_model=dict(contract.binding.service_model),
        parameters=tuple(_parameter_dict(p) for p in contract.binding.parameters),
        readback_measurement_ref=contract.binding.readback_measurement_ref,
        rollback_supported=contract.binding.rollback_supported,
        deployment_state=contract.binding.deployment_state,
        live_backend=contract.binding.live_backend,
    )


def default_capability_manifests(
    deployment: DeploymentBinding,
) -> Tuple[XAppCapabilityManifest, ...]:
    """The five specialist manifests, with repository-honest path states.

    * Traffic Steering: the executing xApp is the released external artifact
      (``oran-aic-lower-integration/1.0.0``); the path itself is OTA-verified
      by the TrafficSteeringPreference objective record, so the manifest says
      ``EXTERNAL_SOURCE_REQUIRED`` -- this repository cannot rebuild or
      re-verify that xApp from source.
    * UE Scheduler / Cell Power: ``HARDWARE_FREE_ONLY`` with the telnet-knob
      blocker recorded; neither has a deployed E2 path.
    * Slice Resource: ``HARDWARE_FREE_ONLY``; the Style 2 / Action 6 encoder
      fork exists under ``src/xapp/flexric_adapter`` but the SliceSLATarget
      objective is not submittable, so no production-ready or OTA claim.
    * Link Adaptation: ``HARDWARE_FREE_ONLY`` and outside the coordinated
      live set; its capability is preserved, not deleted.
    """
    catalog = {item.action_id: item for item in action_catalog(deployment)}
    ts_record = record_for("TrafficSteeringPreference")
    if ts_record.support_state is not SupportState.OTA_LIVE_VERIFIED:
        raise XAppRegistryError(
            "the objective registry no longer records TrafficSteeringPreference "
            "as OTA-verified; rebuild the Traffic Steering manifest honestly")
    slice_record = record_for("SliceSLATarget")

    steering = catalog["cell-steering"]
    cap = catalog["ue-dl-prb-cap"]
    priority = catalog["scheduler-priority"]
    rfatt = catalog["dl-rf-attenuation"]
    quota = catalog["slice-prb-quota"]
    mcs = catalog["dl-mcs-bounds"]

    both = (KpiRequirementPurpose.PRECONDITION, KpiRequirementPurpose.READBACK)

    traffic_steering = XAppCapabilityManifest(
        manifest_id="xapp-manifest/traffic-steering",
        xapp_id="xapp/traffic-steering",
        xapp_version="1.0.0",
        kind=XAppKind.TRAFFIC_STEERING,
        action_bindings=(_binding_from_catalog(steering),),
        target_scopes=("UE",),
        required_identifiers=("objectiveUeId", "servingCellId", "targetPrimaryCellId"),
        required_kpis=_kpm_attribution_requirements(both)
        + (_readback_requirement("cell-steering", steering),),
        execution_path_state=XAppExecutionPathState.EXTERNAL_SOURCE_REQUIRED,
        rollback_supported=True,
        cooldown_ms=10_000,
        max_concurrent_assignments=1,
        production_blockers=(),
        external_source_required=True,
        external_source_ref=(
            "released artifact oran-aic-lower-integration/1.0.0 (external "
            "recipient-phase-b-pin-to-cell source tree; not in this repository)"),
        provenance_note=(
            "The A1/E2SM-RC Style 3 Action 1 steering path is OTA-verified by "
            "objective/TrafficSteeringPreference (Gate 5 evidence); this "
            "repository holds the contracts and the coordination layer, not the "
            "executing xApp source."),
    )

    ue_scheduler = XAppCapabilityManifest(
        manifest_id="xapp-manifest/ue-scheduler",
        xapp_id="xapp/ue-scheduler",
        xapp_version="1.0.0",
        kind=XAppKind.UE_SCHEDULER,
        action_bindings=(_binding_from_catalog(cap), _binding_from_catalog(priority)),
        target_scopes=("UE",),
        required_identifiers=(
            "objectiveUeId", "controlledUeId", "rnti", "servingCellId",
            "e2NodeId", "connectionEpoch"),
        required_kpis=_kpm_attribution_requirements(
            (KpiRequirementPurpose.PRECONDITION,))
        + (_readback_requirement("ue-dl-prb-cap", cap),
           _readback_requirement("scheduler-priority", priority)),
        execution_path_state=XAppExecutionPathState.HARDWARE_FREE_ONLY,
        rollback_supported=True,
        cooldown_ms=5_000,
        # One Action-102 writer per (e2NodeId, servingCell, RNTI,
        # connectionEpoch).  The contract fixes this at 1 rather than 2: a
        # second concurrent assignment on this xApp is a second writer on the
        # same UE scope, and leader failover must not be able to create one
        # (docs/architecture/A1-ACTION102-CONTRACT.md section 4.2).
        max_concurrent_assignments=1,
        production_blockers=(
            _TELNET_KNOB_BLOCKER,
            "ue-dl-prb-cap is HARDWARE_FREE_ONLY until the released "
            "oran-aic-lower-integration successor advertises "
            "AIC_UeDlPrbCap_2.0.0 and the deployed RAN-function definition "
            "carries Style 2 / Action 102 / 211-212; it becomes NOT_WIRED when "
            "that lower half exists and reaches a live state only after the "
            "raw OTA evidence gate.",
        ),
        provenance_note=(
            "RNTI is cell-local and changes on handover or re-attach; every "
            "assignment re-verifies serving cell and RNTI from fresh KPM "
            "attribution immediately before execution.  For ue-dl-prb-cap the "
            "controlled UE is a different, heavy, non-target UE and the "
            "objective UE's DRB.UEThpDl floor stays a mandatory predicate: the "
            "cap is SUPPLEMENTARY to a PRIMARY steering action and is admitted "
            "live only in QoSTarget and UELevelTarget."),
    )

    cell_power = XAppCapabilityManifest(
        manifest_id="xapp-manifest/cell-power",
        xapp_id="xapp/cell-power",
        xapp_version="1.0.0",
        kind=XAppKind.CELL_POWER,
        action_bindings=(_binding_from_catalog(rfatt),),
        target_scopes=("NRCellDU",),
        required_identifiers=("cellId",),
        required_kpis=_kpm_attribution_requirements(
            (KpiRequirementPurpose.PRECONDITION,))
        + (_readback_requirement("dl-rf-attenuation", rfatt),),
        execution_path_state=XAppExecutionPathState.HARDWARE_FREE_ONLY,
        rollback_supported=True,
        cooldown_ms=15_000,
        max_concurrent_assignments=1,
        production_blockers=(
            _TELNET_KNOB_BLOCKER,
            "dl-rf-attenuation is explicitly not a standard E2SM-RC action "
            "(assurance/actions/catalog.py: PROJECT-CUSTOM, O1-adjacent).",
        ),
        provenance_note=(
            "Direction: increasing txAttenuationDb REDUCES DL transmit power "
            "(attenuation dB below max gain; basis executor/oai_executor.py "
            "and oai_patches/d2_actionspace_runtime_knobs.patch). The effect "
            "is cell-wide, so every plan step carries the affected active UE "
            "range from the KPI snapshot."),
    )

    slice_resource = XAppCapabilityManifest(
        manifest_id="xapp-manifest/slice-resource",
        xapp_id="xapp/slice-resource",
        xapp_version="1.0.0",
        kind=XAppKind.SLICE_RESOURCE,
        action_bindings=(_binding_from_catalog(quota),),
        target_scopes=("S-NSSAI",),
        required_identifiers=("sst", "sd"),
        required_kpis=(_readback_requirement("slice-prb-quota", quota),),
        execution_path_state=XAppExecutionPathState.HARDWARE_FREE_ONLY,
        rollback_supported=True,
        cooldown_ms=15_000,
        max_concurrent_assignments=1,
        production_blockers=tuple(
            slice_record.deployment_capability.blocking_reasons),
        provenance_note=(
            "The Style 2 / Action 6 encoder fork exists hardware-free under "
            "src/xapp/flexric_adapter; SliceSLATarget is not submittable in "
            "this deployment, so this xApp is preserved at its current level "
            "and is neither production-ready nor OTA-verified."),
    )

    link_adaptation = XAppCapabilityManifest(
        manifest_id="xapp-manifest/link-adaptation",
        xapp_id="xapp/link-adaptation",
        xapp_version="1.0.0",
        kind=XAppKind.LINK_ADAPTATION,
        action_bindings=(_binding_from_catalog(mcs),),
        target_scopes=("NRCellDU",),
        required_identifiers=("cellId",),
        required_kpis=(_readback_requirement("dl-mcs-bounds", mcs),),
        execution_path_state=XAppExecutionPathState.HARDWARE_FREE_ONLY,
        rollback_supported=True,
        cooldown_ms=15_000,
        max_concurrent_assignments=1,
        production_blockers=(
            _TELNET_KNOB_BLOCKER,
            "Cell-wide MCS bounds can defeat every floor objective; the "
            "composition policy forbids them for all floor families, so this "
            "xApp stays outside the initial coordinated live set.",
        ),
        provenance_note=(
            "Capability preserved, not deleted; excluded from the initial "
            "coordinated live set for cell-wide safety reasons."),
    )

    return (traffic_steering, ue_scheduler, cell_power, slice_resource,
            link_adaptation)
