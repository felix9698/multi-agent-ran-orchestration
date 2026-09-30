"""Versioned, content-addressed contract families (design section 6).

Owner lane: **KCON**.  Every module in this package belongs to that lane; see
``docs/architecture/SEAMS-GATE2.md``.

The families are data and are complete: the other three Gate 2 lanes construct
them in their own tests without waiting for KCON.  What is *frozen* here is the
behaviour that turns a contract into an admitted, epoch-frozen object --
:func:`~assurance.contracts.validation.validate_contract`,
:func:`~assurance.contracts.validation.contract_content_hash`,
:func:`~assurance.contracts.epoch.freeze_epoch` and
:func:`~assurance.contracts.catalog.generate_catalog`.

Nothing in this package reaches an endpoint.  A contract describes what may be
asked for and how the answer is measured; asking is the Write Gateway's job and
measuring is the Measurement Collector's.
"""

from __future__ import annotations

from assurance.contracts.capability import (
    ActuatorDeploymentState,
    ActuatorBinding,
    ActuatorParameter,
    ActuatorPath,
    CapabilityManifest,
    CompositionManifest,
    DeploymentBinding,
    TransportSecurity,
)
from assurance.contracts.catalog import (
    Candidate,
    CandidateCatalog,
    CoordinationCasePolicy,
    catalog_hash,
    generate_catalog,
)
from assurance.contracts.common import ContractIdentity
from assurance.contracts.epoch import EpochRecord, epoch_hash, freeze_epoch
from assurance.contracts.harm import (
    CertifiedHarmBound,
    HarmContract,
    HarmKind,
    WatchdogAction,
    WatchdogContract,
)
from assurance.contracts.ledgers import (
    CompatibilityCheck,
    CompatibilityRecord,
    EvidenceCell,
    EvidenceContribution,
    EvidenceLedgerRecord,
    HarmLedgerRecord,
    MovementKind,
    ReserveMovement,
)
from assurance.contracts.measurement import (
    Aggregation,
    ClockRequirement,
    CounterBinding,
    Estimator,
    GapPolicy,
    MeasurementContract,
    MeasurementRegistry,
    MeasurementSource,
    OverlapPolicy,
    UncertaintyRule,
)
from assurance.contracts.target import (
    ComparisonOperator,
    TargetContract,
    TargetOption,
    TargetPredicate,
    TargetReleasePolicy,
    TargetVector,
    TypedConstraint,
)
from assurance.contracts.validation import (
    CONTRACT_FAMILIES,
    ContractAdmissionError,
    assert_secret_free,
    canonical_form,
    contract_content_hash,
    validate_contract,
    validate_family_set,
)

__all__ = [
    "ActuatorBinding", "ActuatorPath", "Aggregation", "CONTRACT_FAMILIES",
    "ActuatorDeploymentState", "ActuatorParameter", "Candidate", "CandidateCatalog", "CapabilityManifest", "CertifiedHarmBound",
    "ClockRequirement", "CompatibilityCheck", "CompatibilityRecord",
    "ComparisonOperator", "CompositionManifest", "ContractAdmissionError",
    "ContractIdentity", "CoordinationCasePolicy", "CounterBinding",
    "DeploymentBinding", "EpochRecord", "Estimator", "EvidenceCell",
    "EvidenceContribution", "EvidenceLedgerRecord", "GapPolicy", "HarmContract",
    "HarmKind", "HarmLedgerRecord", "MeasurementContract", "MeasurementRegistry",
    "MeasurementSource", "MovementKind", "OverlapPolicy", "ReserveMovement",
    "TargetContract", "TargetOption", "TargetPredicate", "TargetReleasePolicy",
    "TargetVector", "TransportSecurity", "TypedConstraint", "UncertaintyRule",
    "WatchdogAction", "WatchdogContract", "assert_secret_free", "canonical_form",
    "catalog_hash", "contract_content_hash", "epoch_hash", "freeze_epoch",
    "generate_catalog", "validate_contract", "validate_family_set",
]
