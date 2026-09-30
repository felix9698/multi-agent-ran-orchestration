"""XApp Capability Manifest: what one specialist xApp may execute.

An **Action** is a concrete control applied to the base station; an **xApp**
is the software that validates and executes the kind of Action assigned to
it.  This module types that distinction: an
:class:`XAppCapabilityManifest` registers which catalog Actions an xApp owns,
what it needs to execute them, and how far its execution path has actually
been verified.  It never proposes Actions and it never grants a write: the
Assurance Kernel and the Write Gateway keep that authority.

The manifest is a static, machine-readable contract determined by code and
deployment state -- not a free-form document an LLM regenerates per run.  It
reuses the project's canonical-serialization idiom: a camelCase canonical
dict, an RFC 8785 content hash (:func:`assurance.core.addressing.content_hash`),
and a loss-free round trip (:func:`export_manifest_json` /
:func:`parse_manifest_json`).

This is a *semantic capability manifest*.  It is not an O-RAN Software
Community deployment descriptor (no such descriptor exists in this
repository); if one is later introduced, it must be bridged with an explicit
adapter rather than replaced by this type.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

from assurance.actions import action_catalog
from assurance.contracts.capability import ActuatorDeploymentState, DeploymentBinding
from assurance.core.addressing import canonical_bytes, content_hash
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION

__all__ = [
    "ACTION_FAMILY_BY_ID",
    "XAPP_KIND_OWNED_ACTIONS",
    "KpiRequirementPurpose",
    "XAppActionBinding",
    "XAppCapabilityManifest",
    "XAppExecutionPathState",
    "XAppKind",
    "XAppKpiRequirement",
    "XAppManifestError",
    "export_manifest_json",
    "parse_manifest_json",
    "validate_xapp_manifest",
]


class XAppManifestError(ValueError):
    """A manifest overstates, mis-owns, or cannot be admitted."""


class XAppKind(Enum):
    """The specialist xApp kinds this coordination layer recognises."""

    TRAFFIC_STEERING = "TRAFFIC_STEERING"
    UE_SCHEDULER = "UE_SCHEDULER"
    CELL_POWER = "CELL_POWER"
    SLICE_RESOURCE = "SLICE_RESOURCE"
    LINK_ADAPTATION = "LINK_ADAPTATION"


class XAppExecutionPathState(Enum):
    """How far this xApp's own execution path is actually verified.

    Deliberately not the same axis as
    :class:`~assurance.contracts.capability.ActuatorDeploymentState`: an
    action contract can be hardware-free verified while the xApp that would
    execute it over E2 does not exist in this deployment.

    ``OTA_LIVE_VERIFIED``
        The path has produced retained OTA evidence in this deployment.
    ``HARDWARE_FREE_ONLY``
        Contract round trips and hermetic execution exist; no live E2 path.
    ``NOT_WIRED``
        A live backend primitive exists but is not connected to the official
        E2 path in this deployment.
    ``EXTERNAL_SOURCE_REQUIRED``
        The executing software lives outside this repository.
    ``UNSUPPORTED``
        No execution path of any kind exists.
    """

    OTA_LIVE_VERIFIED = "OTA_LIVE_VERIFIED"
    HARDWARE_FREE_ONLY = "HARDWARE_FREE_ONLY"
    NOT_WIRED = "NOT_WIRED"
    EXTERNAL_SOURCE_REQUIRED = "EXTERNAL_SOURCE_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"


class KpiRequirementPurpose(Enum):
    """Why an xApp needs a KPI: to check preconditions or to read back.

    An xApp receives KPI subsets for these two purposes only.  There is no
    ``DECISION`` purpose here on purpose -- composition decisions belong to
    the Action Composition Coordinator, not to the executing xApp.
    """

    PRECONDITION = "PRECONDITION"
    READBACK = "READBACK"


#: Action family per catalog action id.  Tier B actions carry no family and
#: are therefore not ownable by any specialist xApp in this layer.
ACTION_FAMILY_BY_ID: Mapping[str, str] = MappingProxyType({
    "cell-steering": "steer",
    "ue-dl-prb-cap": "cap",
    "scheduler-priority": "priority",
    "slice-prb-quota": "quota",
    "dl-rf-attenuation": "rfatt",
    "dl-mcs-bounds": "mcs",
})

#: The closed capability-ownership boundary.  A kind may own exactly these
#: catalog actions and nothing else; registration and execution both refuse
#: anything outside the set.
XAPP_KIND_OWNED_ACTIONS: Mapping[XAppKind, Tuple[str, ...]] = MappingProxyType({
    XAppKind.TRAFFIC_STEERING: ("cell-steering",),
    XAppKind.UE_SCHEDULER: ("ue-dl-prb-cap", "scheduler-priority"),
    XAppKind.CELL_POWER: ("dl-rf-attenuation",),
    XAppKind.SLICE_RESOURCE: ("slice-prb-quota",),
    XAppKind.LINK_ADAPTATION: ("dl-mcs-bounds",),
})


@dataclass(frozen=True)
class XAppKpiRequirement:
    """One KPI an xApp needs, with the code or document that says so."""

    measurement_ref: str
    purpose: KpiRequirementPurpose
    scope_level: str
    source_interface: str
    basis: str

    def __post_init__(self) -> None:
        if not isinstance(self.purpose, KpiRequirementPurpose):
            raise XAppManifestError("purpose must be a KpiRequirementPurpose member")
        for name in ("measurement_ref", "scope_level", "source_interface", "basis"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise XAppManifestError(f"KPI requirement {name} must be non-empty")

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "measurementRef": self.measurement_ref,
            "purpose": self.purpose.value,
            "scopeLevel": self.scope_level,
            "sourceInterface": self.source_interface,
            "basis": self.basis,
        }

    @classmethod
    def from_canonical_dict(cls, record: Mapping[str, Any]) -> "XAppKpiRequirement":
        return cls(
            measurement_ref=record["measurementRef"],
            purpose=KpiRequirementPurpose(record["purpose"]),
            scope_level=record["scopeLevel"],
            source_interface=record["sourceInterface"],
            basis=record["basis"],
        )


@dataclass(frozen=True)
class XAppActionBinding:
    """One owned Action, with the actuation facts copied from the catalog.

    The action catalog (:mod:`assurance.actions.catalog`) stays the source of
    truth; :func:`validate_xapp_manifest` cross-checks these copies against it
    so a manifest cannot drift from the contract it claims to execute.
    """

    action_id: str
    action_family: str
    service_model: Mapping[str, str]
    parameters: Tuple[Mapping[str, Any], ...]
    readback_measurement_ref: str
    rollback_supported: bool
    deployment_state: ActuatorDeploymentState
    live_backend: Optional[str] = None

    def __post_init__(self) -> None:
        expected_family = ACTION_FAMILY_BY_ID.get(self.action_id)
        if expected_family is None:
            raise XAppManifestError(
                f"{self.action_id}: not an ownable action family "
                "(Tier B / unknown actions cannot be owned by a specialist xApp)"
            )
        if self.action_family != expected_family:
            raise XAppManifestError(
                f"{self.action_id}: family must be {expected_family!r}, "
                f"got {self.action_family!r}"
            )
        if not isinstance(self.deployment_state, ActuatorDeploymentState):
            raise XAppManifestError("deployment_state must be an ActuatorDeploymentState")
        object.__setattr__(self, "service_model",
                           MappingProxyType({str(k): str(v)
                                             for k, v in dict(self.service_model).items()}))
        object.__setattr__(self, "parameters",
                           tuple(MappingProxyType(dict(p)) for p in self.parameters))

    def to_canonical_dict(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "actionId": self.action_id,
            "actionFamily": self.action_family,
            "serviceModel": dict(self.service_model),
            "parameters": [dict(parameter) for parameter in self.parameters],
            "readbackMeasurementRef": self.readback_measurement_ref,
            "rollbackSupported": self.rollback_supported,
            "deploymentState": self.deployment_state.value,
        }
        if self.live_backend is not None:
            record["liveBackend"] = self.live_backend
        return record

    @classmethod
    def from_canonical_dict(cls, record: Mapping[str, Any]) -> "XAppActionBinding":
        return cls(
            action_id=record["actionId"],
            action_family=record["actionFamily"],
            service_model=record["serviceModel"],
            parameters=tuple(record["parameters"]),
            readback_measurement_ref=record["readbackMeasurementRef"],
            rollback_supported=record["rollbackSupported"],
            deployment_state=ActuatorDeploymentState(record["deploymentState"]),
            live_backend=record.get("liveBackend"),
        )


@dataclass(frozen=True)
class XAppCapabilityManifest:
    """One specialist xApp's static capability contract.

    Static capability only: whether the xApp is deployed, healthy, E2
    connected or paused *right now* lives in
    :class:`assurance.xapps.registry.XAppRuntimeStatus`, not here.  The
    constructor is fail-closed on the two claims that matter most --
    ownership outside the closed boundary, and a verification state the
    recorded blockers contradict.
    """

    manifest_id: str
    xapp_id: str
    xapp_version: str
    kind: XAppKind
    action_bindings: Tuple[XAppActionBinding, ...]
    target_scopes: Tuple[str, ...]
    required_identifiers: Tuple[str, ...]
    required_kpis: Tuple[XAppKpiRequirement, ...]
    execution_path_state: XAppExecutionPathState
    rollback_supported: bool
    cooldown_ms: int
    max_concurrent_assignments: int
    production_blockers: Tuple[str, ...] = ()
    external_source_required: bool = False
    external_source_ref: str = ""
    provenance_note: str = ""
    schema_version: str = ASSURANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("manifest_id", "xapp_id", "xapp_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise XAppManifestError(f"{name} must be a non-empty string")
        if not isinstance(self.kind, XAppKind):
            raise XAppManifestError("kind must be an XAppKind member")
        if not isinstance(self.execution_path_state, XAppExecutionPathState):
            raise XAppManifestError(
                "execution_path_state must be an XAppExecutionPathState member")
        object.__setattr__(self, "action_bindings", tuple(self.action_bindings))
        object.__setattr__(self, "target_scopes", tuple(self.target_scopes))
        object.__setattr__(self, "required_identifiers", tuple(self.required_identifiers))
        object.__setattr__(self, "required_kpis", tuple(self.required_kpis))
        object.__setattr__(self, "production_blockers", tuple(self.production_blockers))

        if not self.action_bindings:
            raise XAppManifestError(f"{self.xapp_id}: a manifest owns at least one action")
        owned = tuple(binding.action_id for binding in self.action_bindings)
        if len(set(owned)) != len(owned):
            raise XAppManifestError(f"{self.xapp_id}: duplicate owned action")
        allowed = set(XAPP_KIND_OWNED_ACTIONS[self.kind])
        foreign = sorted(set(owned) - allowed)
        if foreign:
            raise XAppManifestError(
                f"{self.xapp_id}: {self.kind.value} may not own {foreign}; "
                f"its closed ownership set is {sorted(allowed)}"
            )
        if isinstance(self.cooldown_ms, bool) or not isinstance(self.cooldown_ms, int) \
                or self.cooldown_ms < 0:
            raise XAppManifestError("cooldown_ms must be a non-negative int")
        if isinstance(self.max_concurrent_assignments, bool) \
                or not isinstance(self.max_concurrent_assignments, int) \
                or self.max_concurrent_assignments < 1:
            raise XAppManifestError("max_concurrent_assignments must be >= 1")

        if (self.execution_path_state is XAppExecutionPathState.OTA_LIVE_VERIFIED
                and self.production_blockers):
            raise XAppManifestError(
                f"{self.xapp_id}: OTA_LIVE_VERIFIED cannot coexist with production "
                f"blockers {list(self.production_blockers)}; state the honest path state"
            )
        if self.external_source_required and not self.external_source_ref.strip():
            raise XAppManifestError(
                f"{self.xapp_id}: external_source_required needs external_source_ref")

    # -- derived views -----------------------------------------------------

    @property
    def owned_action_ids(self) -> Tuple[str, ...]:
        return tuple(binding.action_id for binding in self.action_bindings)

    @property
    def action_families(self) -> Tuple[str, ...]:
        return tuple(binding.action_family for binding in self.action_bindings)

    def owns_action(self, action_id: str) -> bool:
        return action_id in self.owned_action_ids

    def binding_for(self, action_id: str) -> XAppActionBinding:
        for binding in self.action_bindings:
            if binding.action_id == action_id:
                return binding
        raise XAppManifestError(f"{self.xapp_id} does not own action {action_id!r}")

    # -- content addressing ------------------------------------------------

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "manifestId": self.manifest_id,
            "xappId": self.xapp_id,
            "xappVersion": self.xapp_version,
            "schemaVersion": self.schema_version,
            "kind": self.kind.value,
            "actionBindings": [b.to_canonical_dict() for b in self.action_bindings],
            "targetScopes": list(self.target_scopes),
            "requiredIdentifiers": list(self.required_identifiers),
            "requiredKpis": [k.to_canonical_dict() for k in self.required_kpis],
            "executionPathState": self.execution_path_state.value,
            "rollbackSupported": self.rollback_supported,
            "cooldownMs": self.cooldown_ms,
            "maxConcurrentAssignments": self.max_concurrent_assignments,
            "productionBlockers": list(self.production_blockers),
            "externalSourceRequired": self.external_source_required,
            "externalSourceRef": self.external_source_ref,
            "provenanceNote": self.provenance_note,
        }

    def content_hash(self) -> str:
        return content_hash(self.to_canonical_dict())

    @classmethod
    def from_canonical_dict(cls, record: Mapping[str, Any]) -> "XAppCapabilityManifest":
        return cls(
            manifest_id=record["manifestId"],
            xapp_id=record["xappId"],
            xapp_version=record["xappVersion"],
            kind=XAppKind(record["kind"]),
            action_bindings=tuple(
                XAppActionBinding.from_canonical_dict(b) for b in record["actionBindings"]
            ),
            target_scopes=tuple(record["targetScopes"]),
            required_identifiers=tuple(record["requiredIdentifiers"]),
            required_kpis=tuple(
                XAppKpiRequirement.from_canonical_dict(k) for k in record["requiredKpis"]
            ),
            execution_path_state=XAppExecutionPathState(record["executionPathState"]),
            rollback_supported=record["rollbackSupported"],
            cooldown_ms=record["cooldownMs"],
            max_concurrent_assignments=record["maxConcurrentAssignments"],
            production_blockers=tuple(record["productionBlockers"]),
            external_source_required=record["externalSourceRequired"],
            external_source_ref=record["externalSourceRef"],
            provenance_note=record["provenanceNote"],
            schema_version=record["schemaVersion"],
        )


def export_manifest_json(manifest: XAppCapabilityManifest) -> str:
    """The manifest as RFC 8785 canonical JSON text."""
    return canonical_bytes(manifest.to_canonical_dict()).decode("utf-8")


def parse_manifest_json(text: str) -> XAppCapabilityManifest:
    """Rebuild a manifest from :func:`export_manifest_json` output.

    Round-trip property: ``parse_manifest_json(export_manifest_json(m)) == m``
    and both share one content hash.
    """
    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        raise XAppManifestError(f"manifest JSON does not parse: {exc}") from exc
    if not isinstance(record, dict):
        raise XAppManifestError("manifest JSON must be an object")
    try:
        return XAppCapabilityManifest.from_canonical_dict(record)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, XAppManifestError):
            raise
        raise XAppManifestError(f"manifest JSON is not admissible: {exc!r}") from exc


def validate_xapp_manifest(
    manifest: XAppCapabilityManifest, *, deployment: Optional[DeploymentBinding] = None
) -> None:
    """Fail closed on a manifest that drifted from the action catalog.

    With a *deployment*, every owned action's copied service model,
    parameters, readback reference and rollback claim are compared against
    :func:`assurance.actions.action_catalog` -- the frozen source of truth.
    Without one, only the constructor-level invariants (already enforced) and
    the ownership boundary are re-checked.
    """
    allowed = set(XAPP_KIND_OWNED_ACTIONS[manifest.kind])
    foreign = sorted(set(manifest.owned_action_ids) - allowed)
    if foreign:
        raise XAppManifestError(
            f"{manifest.xapp_id}: owns actions outside its boundary: {foreign}")
    if deployment is None:
        return
    catalog = {item.action_id: item for item in action_catalog(deployment)}
    for binding in manifest.action_bindings:
        contract = catalog.get(binding.action_id)
        if contract is None:
            raise XAppManifestError(
                f"{manifest.xapp_id}: {binding.action_id} is not in the action catalog")
        if dict(binding.service_model) != dict(contract.binding.service_model):
            raise XAppManifestError(
                f"{manifest.xapp_id}: {binding.action_id} service model drifted "
                "from the catalog contract")
        expected_parameters = [_parameter_dict(p) for p in contract.binding.parameters]
        if [dict(p) for p in binding.parameters] != expected_parameters:
            raise XAppManifestError(
                f"{manifest.xapp_id}: {binding.action_id} parameter space drifted "
                "from the catalog contract")
        if binding.readback_measurement_ref != contract.binding.readback_measurement_ref:
            raise XAppManifestError(
                f"{manifest.xapp_id}: {binding.action_id} readback ref drifted")
        if binding.rollback_supported != contract.binding.rollback_supported:
            raise XAppManifestError(
                f"{manifest.xapp_id}: {binding.action_id} rollback claim drifted")


def _parameter_dict(parameter: Any) -> Dict[str, Any]:
    """One catalog actuator parameter in the manifest's canonical spelling."""
    record: Dict[str, Any] = {
        "name": parameter.name,
        "type": parameter.value_type,
        "scope": parameter.scope,
        "unit": parameter.unit,
    }
    if parameter.minimum is not None:
        record["minimum"] = parameter.minimum
    if parameter.maximum is not None:
        record["maximum"] = parameter.maximum
    if parameter.allowed_values:
        record["allowedValues"] = list(parameter.allowed_values)
    return record
