"""The seven objective families, their standard mapping, and what is true today.

Added at Gate 4; ownership and frozen seats in
``docs/architecture/SEAMS-GATE4.md``.

Two things live here and nothing else:

:mod:`assurance.objectives.registry`
    Complete.  The declarative, machine-readable record of every objective
    family -- its project contract identifier, the published policy, interface
    and service-model versions it is mapped to, whether the current frozen
    deployment can accept a submission for it, its support state and its
    evidence level -- plus the validator that refuses a record which overstates
    any of those.

:mod:`assurance.objectives.family` and the seven family modules
    Frozen seats.  Each family module is one lane's file, with six methods
    raising :class:`NotImplementedError` until that lane fills them.  A seat
    returning a placeholder would let the shared scenario matrix report a pass
    over nothing, which is the failure mode Gate 4's acceptance names directly:
    no objective is supported on name and schema alone.

Nothing in this package reaches an endpoint, imports the deployment or calls a
model.  The capability facts in the registry are literals carrying the released
document they were read from; ``tests/assurance/test_oseam_registry.py`` is
what checks they still agree with ``oran/integration/objectives.py``.
"""

from __future__ import annotations

from assurance.objectives.family import (
    ExpectedConfiguration,
    ExpectedOutcome,
    KpiDeclaration,
    KpiUse,
    ObjectiveContractBundle,
    ObjectiveFamilyModule,
    PolicyLifecycle,
    ScenarioName,
)
from assurance.objectives.qoe_and_tsp import QoEandTSPFamily
from assurance.objectives.qoe_target import QoETargetFamily
from assurance.objectives.qos_and_tsp import QoSandTSPFamily
from assurance.objectives.qos_target import QoSTargetFamily
from assurance.objectives.registry import (
    OBJECTIVE_FAMILIES,
    OBJECTIVE_REGISTRY,
    PIN_REGRESSION_FAMILY,
    PROJECT_IDENTIFIER_NOTICE,
    DeploymentCapability,
    EvidenceLevel,
    O1Measurement,
    ObjectiveRecord,
    Premise,
    PremiseKind,
    RegistryError,
    ServiceModelMapping,
    StandardMapping,
    SupportState,
    record_for,
    registry_view,
    validate_record,
    validate_registry,
)
from assurance.objectives.slice_sla_target import SliceSLATargetFamily
from assurance.objectives.traffic_steering import TrafficSteeringPreferenceFamily
from assurance.objectives.ue_level_target import UELevelTargetFamily

#: The family module for each of the seven families, keyed by family name.
#: Built here rather than in ``registry.py`` so the registry stays pure data:
#: a record must be readable without importing seven modules that raise.
FAMILY_MODULES = {
    TrafficSteeringPreferenceFamily.family: TrafficSteeringPreferenceFamily,
    QoSTargetFamily.family: QoSTargetFamily,
    UELevelTargetFamily.family: UELevelTargetFamily,
    QoSandTSPFamily.family: QoSandTSPFamily,
    QoETargetFamily.family: QoETargetFamily,
    QoEandTSPFamily.family: QoEandTSPFamily,
    SliceSLATargetFamily.family: SliceSLATargetFamily,
}

__all__ = [
    "DeploymentCapability", "EvidenceLevel", "ExpectedConfiguration",
    "ExpectedOutcome", "FAMILY_MODULES", "KpiDeclaration", "KpiUse",
    "O1Measurement", "OBJECTIVE_FAMILIES", "OBJECTIVE_REGISTRY",
    "ObjectiveContractBundle", "ObjectiveFamilyModule", "ObjectiveRecord",
    "PIN_REGRESSION_FAMILY", "PROJECT_IDENTIFIER_NOTICE", "PolicyLifecycle",
    "Premise", "PremiseKind", "QoEandTSPFamily", "QoETargetFamily",
    "QoSTargetFamily", "QoSandTSPFamily", "RegistryError", "ScenarioName",
    "ServiceModelMapping", "SliceSLATargetFamily", "StandardMapping",
    "SupportState", "TrafficSteeringPreferenceFamily", "UELevelTargetFamily",
    "record_for", "registry_view", "validate_record", "validate_registry",
]
