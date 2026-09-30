"""The xApp coordination layer.

Registration path (deployment-time):

    xApp package -> XApp Capability Manifest -> XApp Capability Registry

Runtime path (per decision cycle):

    O-RAN Policy + common KPI Snapshot
    -> Action Composition Coordinator -> Candidate Action Set
    -> XApp Execution Coordinator -> XApp Execution Plan
    -> Assurance Kernel admission -> Write Gateway permit
    -> selected specialist xApp (or approved execution adapter)
    -> FlexRIC -> E2 Control -> OAI gNB -> Measurement Collector
    -> evaluation and, where required, replanning.

An **Action** is a concrete control applied to the base station; an **xApp**
is software that validates and executes the kind of Action assigned to it.
Specialist xApps register capability instead of proposing Actions; the
Action Composition Coordinator recommends compositions from Policy and KPI;
the XApp Execution Coordinator maps Actions to xApps and orders execution by
concrete-Action dependencies -- never by a fixed xApp rank.  Neither
coordinator is an equipment writer: Kernel admission and Gateway permits
retain all write authority.
"""

from assurance.xapps.assignment import (
    FORBIDDEN_REPORT_FIELDS, XAppExecutionAssignment, XAppExecutionReport,
    XAppExecutionStatus, assignment_for_step,
)
from assurance.xapps.composition import (
    PROPOSAL_VALUE_BOUNDARY, ActionCompositionCoordinator, CandidateActionSet,
    CompositionCoordinatorError, ContradictoryActionError, RecommendedAction,
    StaleSnapshotError,
)
from assurance.xapps.execution import (
    CoordinationOutcome, ExecutionCoordinationResult,
    KERNEL_ADMISSION_REQUIRED_NOTE, RecentExecution, StepPrecondition,
    StepRefusal, XAppExecutionCoordinator, XAppExecutionPlan,
    XAppExecutionStep,
)
from assurance.xapps.executors import (
    CellPowerXApp, ExecutorError, HardwareFreeConfigStore, PermitRequiredError,
    SpecialistXAppExecutor, TrafficSteeringXApp, UeSchedulerXApp,
)
from assurance.xapps.manifest import (
    ACTION_FAMILY_BY_ID, XAPP_KIND_OWNED_ACTIONS, KpiRequirementPurpose,
    XAppActionBinding, XAppCapabilityManifest, XAppExecutionPathState,
    XAppKind, XAppKpiRequirement, XAppManifestError, export_manifest_json,
    parse_manifest_json, validate_xapp_manifest,
)
from assurance.xapps.registry import (
    CapabilityOwnershipError, INITIAL_COORDINATED_LIVE_SET, LiveSelection,
    XAppCapabilityRegistry, XAppRegistryError, XAppRuntimeStatus,
    default_capability_manifests,
)
from assurance.xapps.runtime import (
    XAppCoordinationRuntime, build_default_xapp_coordination,
)
from assurance.xapps.snapshot import (
    SERVING_CELL_ATTRIBUTION_COUNTER, CommonKpiSnapshot, KpiSnapshotEntry,
    SnapshotError, snapshot_from_samples,
)

__all__ = [
    "ACTION_FAMILY_BY_ID",
    "FORBIDDEN_REPORT_FIELDS",
    "INITIAL_COORDINATED_LIVE_SET",
    "KERNEL_ADMISSION_REQUIRED_NOTE",
    "PROPOSAL_VALUE_BOUNDARY",
    "SERVING_CELL_ATTRIBUTION_COUNTER",
    "XAPP_KIND_OWNED_ACTIONS",
    "ActionCompositionCoordinator",
    "CandidateActionSet",
    "CapabilityOwnershipError",
    "CellPowerXApp",
    "CommonKpiSnapshot",
    "CompositionCoordinatorError",
    "ContradictoryActionError",
    "CoordinationOutcome",
    "ExecutionCoordinationResult",
    "ExecutorError",
    "HardwareFreeConfigStore",
    "KpiRequirementPurpose",
    "KpiSnapshotEntry",
    "LiveSelection",
    "PermitRequiredError",
    "RecentExecution",
    "RecommendedAction",
    "SnapshotError",
    "SpecialistXAppExecutor",
    "StaleSnapshotError",
    "StepPrecondition",
    "StepRefusal",
    "TrafficSteeringXApp",
    "UeSchedulerXApp",
    "XAppActionBinding",
    "XAppCapabilityManifest",
    "XAppCapabilityRegistry",
    "XAppCoordinationRuntime",
    "XAppExecutionAssignment",
    "XAppExecutionCoordinator",
    "XAppExecutionPathState",
    "XAppExecutionPlan",
    "XAppExecutionReport",
    "XAppExecutionStatus",
    "XAppExecutionStep",
    "XAppKind",
    "XAppKpiRequirement",
    "XAppManifestError",
    "XAppRegistryError",
    "XAppRuntimeStatus",
    "assignment_for_step",
    "build_default_xapp_coordination",
    "default_capability_manifests",
    "export_manifest_json",
    "parse_manifest_json",
    "snapshot_from_samples",
    "validate_xapp_manifest",
]
