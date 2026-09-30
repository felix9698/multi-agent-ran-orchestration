"""One composition point for the xApp coordination layer.

:func:`build_default_xapp_coordination` assembles the default registry (the
five specialist manifests with repository-honest path states) and the two
coordinators, so the default runtime can adopt the layer through a single
call.  Nothing here opens a transport, issues a permit, or bypasses the
Assurance Kernel: the produced coordinators emit declarations that still
travel the existing admission path

    Candidate Action Set -> XApp Execution Plan -> Kernel admission
    -> Write Gateway permit -> (selected xApp / approved adapter) -> FlexRIC
    -> E2 -> OAI gNB -> Measurement Collector -> evaluation / replan.
"""

from __future__ import annotations

from dataclasses import dataclass

from assurance.contracts.capability import DeploymentBinding
from assurance.xapps.composition import ActionCompositionCoordinator
from assurance.xapps.execution import XAppExecutionCoordinator
from assurance.xapps.registry import (
    XAppCapabilityRegistry, default_capability_manifests,
)

__all__ = ["XAppCoordinationRuntime", "build_default_xapp_coordination"]


@dataclass(frozen=True)
class XAppCoordinationRuntime:
    """The assembled coordination layer, ready for the default runtime."""

    registry: XAppCapabilityRegistry
    composition_coordinator: ActionCompositionCoordinator
    execution_coordinator: XAppExecutionCoordinator


def build_default_xapp_coordination(
    deployment: DeploymentBinding,
    *,
    snapshot_freshness_bound_ms: int = 5_000,
    validity_window_ms: int = 30_000,
    plan_deadline_ms: int = 60_000,
) -> XAppCoordinationRuntime:
    """Register the default manifests and wire the two coordinators."""
    registry = XAppCapabilityRegistry(deployment=deployment)
    for manifest in default_capability_manifests(deployment):
        registry.register(manifest)
    composition = ActionCompositionCoordinator(
        deployment=deployment,
        snapshot_freshness_bound_ms=snapshot_freshness_bound_ms,
        validity_window_ms=validity_window_ms,
    )
    execution = XAppExecutionCoordinator(
        registry=registry,
        snapshot_freshness_bound_ms=snapshot_freshness_bound_ms,
        plan_deadline_ms=plan_deadline_ms,
    )
    return XAppCoordinationRuntime(
        registry=registry,
        composition_coordinator=composition,
        execution_coordinator=execution,
    )
