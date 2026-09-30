"""The machine-readable objective registry, and the rules that keep it honest.

Owner lane: **OBJ3** for the record contents once a lane's evidence changes;
the schema and the validator below are frozen by
``docs/architecture/SEAMS-GATE4.md``.

Gate 4's acceptance has four parts, and the last one is the reason this module
exists: "objective별 actual capability/evidence level이 registry와 GUI에
정직하게 표시" (task section 13).  Design section 16 fixes the vocabulary --
five support states, no objective advertised above its actual evidence level.

Three axes, deliberately not collapsed into one:

**Support state** -- how far this family's own implementation and verification
has actually got, as one of the five values design section 16 lists.

**Deployment capability** -- whether the *current frozen deployment* can accept
a submission for it at all, which is a fact about the released A1 policy types
and not about our code.  ``SliceSLATarget`` is the case that makes the
separation necessary: the published E2SM-RC Style 2 / Action 6 semantics are a
valid design input, while the current deployment has neither its
encoder/handler chain nor an A1 policy type for it.  Collapsing the axes would
force a choice between hiding the mapping and implying the submission.

**Evidence level** -- what has actually been observed.  A name and a schema are
:attr:`EvidenceLevel.CONTRACT_DECLARED`, which
:func:`validate_record` refuses to pair with any state above "in progress":
design section 15 and Gate 4 both say a mock or replay result is never OTA
evidence, and the registry is where that stops being a promise.

An objective name here is a **project contract identifier**.  ETSI and O-RAN do
not define ``TrafficSteeringPreference``, ``QoSandTSP`` or ``PIN_TO_CELL``;
what they define is the A1 interface, the E2 service models and the PM file
formats those names are mapped *to*, which is what :class:`StandardMapping`
records (task sections 7.7 and 7.8).

Nothing in this module imports the deployment.  ``oran.integration`` is on the
package's forbidden-import list (``tests/assurance/test_seams.py``), so the
capability facts below are recorded as literals with the released document or
repository path they came from in ``basis``, and
``tests/assurance/test_oseam_registry.py`` is what checks the literals still
agree with ``oran/integration/objectives.py``.  That direction is deliberate: a
registry that read the deployment could not be replayed, and a registry that
disagreed with it must fail a test rather than silently re-derive itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

__all__ = [
    "DeploymentCapability",
    "EvidenceLevel",
    "O1Measurement",
    "OBJECTIVE_FAMILIES",
    "OBJECTIVE_REGISTRY",
    "ObjectiveRecord",
    "PIN_REGRESSION_FAMILY",
    "PROJECT_IDENTIFIER_NOTICE",
    "Premise",
    "PremiseKind",
    "RegistryError",
    "ServiceModelMapping",
    "StandardMapping",
    "SupportState",
    "record_for",
    "registry_view",
    "validate_record",
    "validate_registry",
]


#: Stated once, carried into every projection of the registry.  Task section
#: 7.7: a project objective name is never documented as if ETSI or O-RAN had
#: defined it.
PROJECT_IDENTIFIER_NOTICE = (
    "Objective family names in this registry are project contract identifiers. "
    "ETSI and O-RAN do not define them. The published policy, interface and "
    "service-model versions each family is mapped to are recorded per record."
)

#: The seven families of task section 8, in the order the task lists them.
OBJECTIVE_FAMILIES: Tuple[str, ...] = (
    "TrafficSteeringPreference",
    "QoSTarget",
    "UELevelTarget",
    "QoSandTSP",
    "QoETarget",
    "QoEandTSP",
    "SliceSLATarget",
)

#: The existing exact-regression contract, kept under its own identifier.  Task
#: section 8 additional condition: ``PIN_TO_CELL`` is not renamed into the new
#: Traffic Steering family, and its relationship to that family is versioned
#: rather than assumed.  The string is the ``objective_family`` value the frozen
#: ``tests/assurance/pin_to_cell_support.py`` contract set already carries.
PIN_REGRESSION_FAMILY = "UeCellSteeringPinToCell"


class RegistryError(ValueError):
    """A registry record cannot be admitted.

    One error type, for the same reason
    :class:`assurance.contracts.validation.ContractAdmissionError` is one type:
    a caller able to catch "only the evidence-level failures" and continue is a
    caller able to publish an overstated support claim.
    """


class SupportState(Enum):
    """The five states design section 16 permits, and no sixth.

    ``IMPLEMENTATION_IN_PROGRESS``
        Contracts, catalog or scenario matrix are not complete yet.  The
        honest state for a family whose module is still a frozen seat.
    ``HARDWARE_FREE_VERIFIED``
        The complete hardware-free matrix of task section 8 chain item 11 has
        run and passed against the real Kernel, Gateway and Collector.
    ``OTA_VERIFICATION_PENDING``
        Hardware-free verified; no OTA run yet.
    ``OTA_LIVE_VERIFIED``
        Verified on the physical testbed with raw evidence retained.
    ``UNSUPPORTED_BY_CURRENT_DEPLOYMENT``
        A premise the objective needs -- a measurement source, a scope
        identity, an actuator, a rollback or an evidence oracle -- does not
        exist in this deployment.  Design section 10: such a field fails closed
        and stays honestly advertised rather than being quietly dropped from
        the list.
    """

    IMPLEMENTATION_IN_PROGRESS = "IMPLEMENTATION_IN_PROGRESS"
    HARDWARE_FREE_VERIFIED = "HARDWARE_FREE_VERIFIED"
    OTA_VERIFICATION_PENDING = "OTA_VERIFICATION_PENDING"
    OTA_LIVE_VERIFIED = "OTA_LIVE_VERIFIED"
    UNSUPPORTED_BY_CURRENT_DEPLOYMENT = "UNSUPPORTED_BY_CURRENT_DEPLOYMENT"


class EvidenceLevel(Enum):
    """What has actually been observed for a family, in increasing order."""

    #: Nothing has been run.
    NONE = "NONE"
    #: Contract content and standard mapping exist; nothing has executed.
    #: Gate 4: "No objective may be marked supported on name/schema alone."
    CONTRACT_DECLARED = "CONTRACT_DECLARED"
    #: The hardware-free matrix ran against the real components over mocks.
    #: Never OTA evidence, however complete (design section 15).
    HARDWARE_FREE_ROUND_TRIP = "HARDWARE_FREE_ROUND_TRIP"
    #: Raw evidence from the physical testbed, retained and referenced.
    OTA_RAW_EVIDENCE = "OTA_RAW_EVIDENCE"


_EVIDENCE_ORDER: Mapping[EvidenceLevel, int] = {
    EvidenceLevel.NONE: 0,
    EvidenceLevel.CONTRACT_DECLARED: 1,
    EvidenceLevel.HARDWARE_FREE_ROUND_TRIP: 2,
    EvidenceLevel.OTA_RAW_EVIDENCE: 3,
}

_SUPPORT_ORDER: Mapping[SupportState, int] = {
    SupportState.UNSUPPORTED_BY_CURRENT_DEPLOYMENT: 0,
    SupportState.IMPLEMENTATION_IN_PROGRESS: 1,
    SupportState.HARDWARE_FREE_VERIFIED: 2,
    SupportState.OTA_VERIFICATION_PENDING: 3,
    SupportState.OTA_LIVE_VERIFIED: 4,
}


class PremiseKind(Enum):
    """What kind of thing an objective needs before it can be supported.

    The kind matters because the consequences differ.  A missing A1 policy type
    blocks *submission* only: the contract chain, the catalog, the evaluator
    and the whole hardware-free matrix still run, so the family can honestly
    reach :attr:`SupportState.HARDWARE_FREE_VERIFIED`.  A missing measurement
    source, scope identity, actuator, rollback path or evidence oracle blocks
    the objective itself -- design section 10 fails those closed -- so no state
    above "in progress" other than
    :attr:`SupportState.UNSUPPORTED_BY_CURRENT_DEPLOYMENT` is available.
    """

    A1_POLICY_TYPE = "A1_POLICY_TYPE"
    MEASUREMENT_SOURCE = "MEASUREMENT_SOURCE"
    SCOPE_IDENTITY = "SCOPE_IDENTITY"
    ACTUATOR_PATH = "ACTUATOR_PATH"
    ROLLBACK_PATH = "ROLLBACK_PATH"
    EVIDENCE_ORACLE = "EVIDENCE_ORACLE"


#: Premise kinds whose absence makes the objective itself unsupportable, as
#: opposed to merely unsubmittable in this deployment.
BLOCKING_PREMISE_KINDS: Tuple[PremiseKind, ...] = (
    PremiseKind.MEASUREMENT_SOURCE,
    PremiseKind.SCOPE_IDENTITY,
    PremiseKind.ACTUATOR_PATH,
    PremiseKind.ROLLBACK_PATH,
    PremiseKind.EVIDENCE_ORACLE,
)


@dataclass(frozen=True)
class Premise:
    """One thing the objective needs, whether it exists, and how we know."""

    kind: PremiseKind
    statement: str
    met: bool
    #: Where the answer comes from: a repository path, a released document or a
    #: retained evidence file.  An unmet premise with no basis is a guess.
    basis: str
    #: True when the premise gates live/OTA promotion but not a hermetic
    #: contract round-trip.  This keeps deployment gaps visible without
    #: misreporting hardware-free evidence as absent.
    live_only: bool = False


@dataclass(frozen=True)
class ServiceModelMapping:
    """One published service model, style and action this family maps onto."""

    service_model: str
    version: str
    ran_function_id: int
    #: ``"CONTROL"`` or ``"REPORT"``.
    procedure: str
    style_id: int
    style_name: str
    #: Control action id, or ``None`` for a report style.
    action_id: Optional[int] = None
    action_name: str = ""
    #: RAN parameters or measurement names carried, as the deployment exposes
    #: them.
    parameters: Tuple[str, ...] = ()
    #: Where this mapping is recorded in the frozen deployment.
    basis: str = ""


@dataclass(frozen=True)
class O1Measurement:
    """One O1 performance measurement, with the specification that defines it."""

    name: str
    #: PM file format, e.g. ``"3GPP TS 32.435 V10.0"``.
    file_format: str
    #: Measurement definition, e.g. ``"3GPP TS 28.552 V18.11.0"``.
    definition: str
    clause: str
    unit: str
    scope_level: str
    delivered_by_deployment: bool
    basis: str = ""


@dataclass(frozen=True)
class StandardMapping:
    """The published versions a project contract identifier is mapped onto.

    Task section 7.8 requires exactly this and nothing less: every objective
    mapped explicitly to the published policy / interface / service-model
    versions it applies, plus the capability the current deployment exposes.
    The second half is :class:`DeploymentCapability`; this half is the first.

    ``policy_type_kind`` exists so a project-defined A1 policy type is never
    read as a published one.  The A1 *interface* is published; the policy type
    carried over it in this deployment is ours.
    """

    #: Published A1 interface version the submission would use.
    policy_interface: str
    #: Policy type identifier in the deployment, or ``None`` when none exists.
    policy_type_id: Optional[str]
    #: ``"PROJECT_CONTRACT_POLICY_TYPE"`` or ``"NONE"``.
    policy_type_kind: str
    control_service_models: Tuple[ServiceModelMapping, ...] = ()
    measurement_service_models: Tuple[ServiceModelMapping, ...] = ()
    o1_measurements: Tuple[O1Measurement, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class DeploymentCapability:
    """Whether the current frozen deployment can accept this objective today.

    ``submittable`` is a fact about the released composition, not an ambition.
    ``oran/integration/objectives.py`` narrows the final composition to one
    executable objective kind and refuses everything else before the first R1
    call; anything this registry marks submittable must survive that gate.
    """

    submittable: bool
    a1_policy_type_present: bool
    #: The document or module the claim is read from.
    basis: str
    #: Why not, when ``submittable`` is ``False``.  One entry per distinct
    #: reason; never empty for an unsubmittable objective.
    blocking_reasons: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ObjectiveRecord:
    """One objective family's honest position, as data.

    Attributes
    ----------
    project_contract_id:
        ``"objective/<family>"``.  A project identifier (task section 7.7).
    family:
        The family name used by ``TargetContract.objective_family``.
    lane:
        Owning lane from ``docs/architecture/SEAMS-GATE4.md`` section 3.
    intent_summary:
        What a natural-language intent for this family means, in one sentence.
    standard_mapping / deployment_capability / support_state / evidence_level:
        The three axes described in the module docstring, plus the mapping.
    premises:
        What the objective needs and whether the deployment has it.
    component_families:
        For a composite family, the two families it combines.  Task section 8
        forbids composing a combined objective out of separate passes, so a
        composite may never be recorded above the weakest of its components.
    related_contracts:
        Versioned relationships to other contracts -- specifically how the
        Traffic Steering family relates to ``PIN_TO_CELL``.
    evidence_refs:
        Retained raw evidence, as repository paths.  Required for
        ``OTA_LIVE_VERIFIED``.
    hardware_free_scenarios:
        The scenario names of the shared harness this family has actually
        passed.  Required for ``HARDWARE_FREE_VERIFIED`` and above.
    is_regression_contract:
        ``True`` only for the preserved ``PIN_TO_CELL`` record.
    """

    project_contract_id: str
    family: str
    lane: str
    intent_summary: str
    standard_mapping: StandardMapping
    deployment_capability: DeploymentCapability
    support_state: SupportState
    evidence_level: EvidenceLevel
    premises: Tuple[Premise, ...] = ()
    component_families: Tuple[str, ...] = ()
    related_contracts: Mapping[str, str] = field(default_factory=dict)
    evidence_refs: Tuple[str, ...] = ()
    hardware_free_scenarios: Tuple[str, ...] = ()
    is_regression_contract: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "related_contracts", dict(self.related_contracts))

    @property
    def joint_trial_required(self) -> bool:
        """Whether every component must be judged inside one live trial."""
        return bool(self.component_families)

    def unmet_premises(self) -> Tuple[Premise, ...]:
        return tuple(premise for premise in self.premises if not premise.met)

    def as_dict(self) -> Dict[str, Any]:
        """A plain projection for the Cockpit, the export and the tests."""
        return {
            "projectContractId": self.project_contract_id,
            "identifierKind": "PROJECT_CONTRACT_IDENTIFIER",
            "family": self.family,
            "lane": self.lane,
            "intentSummary": self.intent_summary,
            "supportState": self.support_state.value,
            "evidenceLevel": self.evidence_level.value,
            "evidenceIsOta": self.evidence_level is EvidenceLevel.OTA_RAW_EVIDENCE,
            "deploymentCapability": {
                "submittable": self.deployment_capability.submittable,
                "a1PolicyTypePresent": self.deployment_capability.a1_policy_type_present,
                "basis": self.deployment_capability.basis,
                "blockingReasons": list(self.deployment_capability.blocking_reasons),
            },
            "standardMapping": {
                "policyInterface": self.standard_mapping.policy_interface,
                "policyTypeId": self.standard_mapping.policy_type_id,
                "policyTypeKind": self.standard_mapping.policy_type_kind,
                "controlServiceModels": [
                    _service_model_dict(item)
                    for item in self.standard_mapping.control_service_models
                ],
                "measurementServiceModels": [
                    _service_model_dict(item)
                    for item in self.standard_mapping.measurement_service_models
                ],
                "o1Measurements": [
                    _o1_dict(item) for item in self.standard_mapping.o1_measurements
                ],
                "notes": self.standard_mapping.notes,
            },
            "premises": [
                {
                    "kind": premise.kind.value,
                    "statement": premise.statement,
                    "met": premise.met,
                    "basis": premise.basis,
                    "liveOnly": premise.live_only,
                }
                for premise in self.premises
            ],
            "componentFamilies": list(self.component_families),
            "jointTrialRequired": self.joint_trial_required,
            "relatedContracts": dict(self.related_contracts),
            "evidenceRefs": list(self.evidence_refs),
            "hardwareFreeScenarios": list(self.hardware_free_scenarios),
            "isRegressionContract": self.is_regression_contract,
            "identifierNotice": PROJECT_IDENTIFIER_NOTICE,
        }


def _service_model_dict(item: ServiceModelMapping) -> Dict[str, Any]:
    return {
        "serviceModel": item.service_model,
        "version": item.version,
        "ranFunctionId": item.ran_function_id,
        "procedure": item.procedure,
        "styleId": item.style_id,
        "styleName": item.style_name,
        "actionId": item.action_id,
        "actionName": item.action_name,
        "parameters": list(item.parameters),
        "basis": item.basis,
    }


def _o1_dict(item: O1Measurement) -> Dict[str, Any]:
    return {
        "name": item.name,
        "fileFormat": item.file_format,
        "definition": item.definition,
        "clause": item.clause,
        "unit": item.unit,
        "scopeLevel": item.scope_level,
        "deliveredByDeployment": item.delivered_by_deployment,
        "basis": item.basis,
    }


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

#: Task section 7.9: this system has no ``A2`` interface, and no document or
#: record may invent one.  Matched on token boundaries so a hex digest or a
#: word like ``DATA2`` cannot trip it, and case-insensitively so ``a2`` in a
#: prose note is caught too.
_A2_TOKEN = re.compile(r"(?<![0-9A-Za-z])a2(?![0-9A-Za-z])", re.IGNORECASE)


def validate_record(record: ObjectiveRecord) -> None:
    """Fail closed on a record that overstates, mis-names, or invents.

    Returns ``None`` and raises :class:`RegistryError` otherwise.  The rules,
    in the order they are checked:

    1. the family is one of the seven, or the preserved regression contract;
    2. the project contract identifier is ``objective/<family>`` -- a project
       identifier with an explicit prefix, never a bare standard-looking name;
    3. no record anywhere mentions an ``A2`` interface (task section 7.9);
    4. the standard mapping is non-empty and does not present the project name
       as a standard term;
    5. an unsubmittable objective states at least one blocking reason, and a
       submittable one has an A1 policy type and no blocking reasons;
    6. ``OTA_LIVE_VERIFIED`` requires OTA raw evidence, retained evidence refs
       and a submittable deployment;
    7. ``HARDWARE_FREE_VERIFIED`` and ``OTA_VERIFICATION_PENDING`` require a
       recorded hardware-free round trip and the scenario names that produced
       it -- name and schema alone are not support (Gate 4);
    8. an unmet blocking premise leaves only "in progress" or "unsupported";
    9. a composite family names exactly two component families.
    """
    if record.family not in OBJECTIVE_FAMILIES and record.family != PIN_REGRESSION_FAMILY:
        _refuse(record, f"unknown objective family {record.family!r}")
    if record.project_contract_id != f"objective/{record.family}":
        _refuse(
            record,
            "project contract identifier must be 'objective/<family>', got "
            f"{record.project_contract_id!r}",
        )
    if not record.intent_summary.strip():
        _refuse(record, "record states no intent meaning")
    if record.lane not in {"OBJ1", "OBJ2", "OBJ3"}:
        _refuse(record, f"unknown owning lane {record.lane!r}")

    for path, text in _strings(record.as_dict()):
        if _A2_TOKEN.search(text):
            _refuse(record, f"names a non-existent A2 interface at {path}")

    mapping = record.standard_mapping
    if not mapping.policy_interface.strip():
        _refuse(record, "standard mapping names no published policy interface")
    if not (
        mapping.control_service_models
        or mapping.measurement_service_models
        or mapping.o1_measurements
    ):
        _refuse(record, "standard mapping names no service model or measurement")
    if (mapping.policy_type_id is None) != (mapping.policy_type_kind == "NONE"):
        _refuse(record, "policy type id and policy type kind disagree")
    if mapping.policy_type_kind not in {"PROJECT_CONTRACT_POLICY_TYPE", "NONE"}:
        _refuse(record, f"unknown policy type kind {mapping.policy_type_kind!r}")
    for path, text in _strings(record.as_dict()["standardMapping"]):
        if path.endswith("notes"):
            continue
        if record.family in text:
            _refuse(
                record,
                f"standard mapping presents the project name {record.family!r} as a "
                f"published term at {path}",
            )

    capability = record.deployment_capability
    if not capability.basis.strip():
        _refuse(record, "deployment capability states no basis")
    if capability.submittable:
        if not capability.a1_policy_type_present:
            _refuse(record, "submittable objective has no A1 policy type")
        if capability.blocking_reasons:
            _refuse(record, "submittable objective still lists blocking reasons")
        if mapping.policy_type_id is None:
            _refuse(record, "submittable objective maps to no policy type")
    elif not capability.blocking_reasons:
        _refuse(record, "unsubmittable objective states no blocking reason")

    level = _EVIDENCE_ORDER[record.evidence_level]
    if record.support_state is SupportState.OTA_LIVE_VERIFIED:
        if record.evidence_level is not EvidenceLevel.OTA_RAW_EVIDENCE:
            _refuse(record, "OTA_LIVE_VERIFIED without OTA raw evidence")
        if not record.evidence_refs:
            _refuse(record, "OTA_LIVE_VERIFIED without retained evidence references")
        if not capability.submittable:
            _refuse(record, "OTA_LIVE_VERIFIED for an objective the deployment refuses")
    if record.support_state in {
        SupportState.HARDWARE_FREE_VERIFIED,
        SupportState.OTA_VERIFICATION_PENDING,
    }:
        if level < _EVIDENCE_ORDER[EvidenceLevel.HARDWARE_FREE_ROUND_TRIP]:
            _refuse(
                record,
                f"{record.support_state.value} claimed at evidence level "
                f"{record.evidence_level.value}; name and schema are not support",
            )
        if not record.hardware_free_scenarios:
            _refuse(record, "hardware-free support claimed with no scenario named")
    if record.evidence_level is EvidenceLevel.OTA_RAW_EVIDENCE and not record.evidence_refs:
        _refuse(record, "OTA evidence level with no retained evidence")
    if record.evidence_refs and record.evidence_level is not EvidenceLevel.OTA_RAW_EVIDENCE:
        _refuse(
            record,
            "evidence references are OTA raw evidence; a hardware-free run is "
            "recorded in hardware_free_scenarios instead",
        )

    blocking = [
        premise
        for premise in record.unmet_premises()
        if premise.kind in BLOCKING_PREMISE_KINDS
    ]
    hardware_free_only = (
        record.support_state is SupportState.HARDWARE_FREE_VERIFIED
        and all(premise.live_only for premise in blocking)
    )
    if blocking and not hardware_free_only and record.support_state not in {
        SupportState.IMPLEMENTATION_IN_PROGRESS,
        SupportState.UNSUPPORTED_BY_CURRENT_DEPLOYMENT,
    }:
        names = ", ".join(premise.kind.value for premise in blocking)
        _refuse(
            record,
            f"support state {record.support_state.value} with unmet premise(s) {names}",
        )
    if blocking and record.support_state in {
        SupportState.OTA_VERIFICATION_PENDING, SupportState.OTA_LIVE_VERIFIED
    }:
        names = ", ".join(premise.kind.value for premise in blocking)
        _refuse(record, f"OTA-facing state with unmet live premise(s) {names}")
    for premise in record.premises:
        if not premise.basis.strip():
            _refuse(record, f"premise {premise.kind.value} states no basis")

    if record.component_families and len(record.component_families) != 2:
        _refuse(record, "a composite objective combines exactly two families")
    if record.is_regression_contract and record.family != PIN_REGRESSION_FAMILY:
        _refuse(record, "only the preserved PIN_TO_CELL contract is a regression record")


def validate_registry(records: Sequence[ObjectiveRecord]) -> None:
    """Validate the complete set, then the rules that are only visible across it.

    * every one of the seven families appears exactly once, plus the preserved
      regression record and nothing else;
    * a composite's component families exist in the set;
    * a composite is never recorded above the weakest of its components -- the
      registry-level reading of task section 8's rule that a combined objective
      may not be assembled out of separate passes.
    """
    for record in records:
        validate_record(record)

    families = [record.family for record in records]
    if len(set(families)) != len(families):
        raise RegistryError(f"duplicate objective family in registry: {families}")
    expected = set(OBJECTIVE_FAMILIES) | {PIN_REGRESSION_FAMILY}
    if set(families) != expected:
        missing = sorted(expected - set(families))
        extra = sorted(set(families) - expected)
        raise RegistryError(
            f"registry membership mismatch; missing={missing} unexpected={extra}"
        )

    by_family = {record.family: record for record in records}
    for record in records:
        if not record.component_families:
            continue
        for component in record.component_families:
            if component not in by_family:
                _refuse(record, f"component family {component!r} is not in the registry")
        weakest = min(
            _SUPPORT_ORDER[by_family[name].support_state]
            for name in record.component_families
        )
        if _SUPPORT_ORDER[record.support_state] > weakest:
            _refuse(
                record,
                "a combined objective cannot be more supported than its weakest "
                "component; separate passes may not be composed",
            )


def record_for(family: str) -> ObjectiveRecord:
    """The registry record for one family, or :class:`KeyError`."""
    for record in OBJECTIVE_REGISTRY:
        if record.family == family:
            return record
    raise KeyError(family)


def registry_view() -> Dict[str, Any]:
    """The whole registry as plain data, for the Cockpit and the export.

    Carries the identifier notice at the top level as well as on every record:
    a projection that dropped it would be a document presenting project names
    as standard ones, which is what task section 7.7 forbids.
    """
    return {
        "identifierNotice": PROJECT_IDENTIFIER_NOTICE,
        "families": list(OBJECTIVE_FAMILIES),
        "regressionContract": PIN_REGRESSION_FAMILY,
        "records": [record.as_dict() for record in OBJECTIVE_REGISTRY],
    }


def _strings(value: Any, path: str = "") -> Tuple[Tuple[str, str], ...]:
    """Every string in a projection, with the path it sits at."""
    found: list = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            found.extend(_strings(item, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_strings(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        found.append((path, value))
    return tuple(found)


def _refuse(record: ObjectiveRecord, message: str) -> None:
    raise RegistryError(f"{record.project_contract_id}: {message}")


# --------------------------------------------------------------------------- #
# the records
# --------------------------------------------------------------------------- #

#: Where every deployment-capability claim below is read from.  One string,
#: because they are all read from the same three released artefacts and a
#: reader should be able to check them all in one place.
_CAPABILITY_BASIS = (
    "oran/integration/objectives.py EXECUTABLE_OBJECTIVES; "
    "docs/integration/final-integration-binding.1.0.0.json scope; "
    "docs/integration/CLAIM-BOUNDARY.md section 3"
)

#: Version of the assurance-family -> wire-objective-kind mapping below.
#: Bumped whenever an entry is added, removed or repointed, because the mapping
#: is the thing that decides which project objective may reach the radio.
ASSURANCE_FAMILY_WIRE_KIND_MAPPING_VERSION = "1.3.0"

#: Task section 7.8: each objective mapped explicitly onto the published
#: versions actually applied and onto the current deployment capability.  This
#: is the second half of that -- the project contract identifier on the left,
#: and on the right the *wire* objective kind the deployment advertises and the
#: A1 policy body actually carries.
#:
#: Section 7.7 is why the two columns exist at all.  The left names are this
#: project's; only the right one is what the frozen policy type enumerates, and
#: writing a project name where a standard kind belongs would document the
#: former as the latter.  A family absent from this table has no wire kind and
#: is refused before the first R1 call rather than translated into some
#: neighbouring objective.
ASSURANCE_FAMILY_TO_WIRE_KIND: Mapping[str, str] = {
    "UeCellSteeringPinToCell": "PIN_TO_CELL",
    "TrafficSteeringPreference": "PIN_TO_CELL",
    "UELevelTarget": "PIN_TO_CELL",
    "QoSTarget": "PIN_TO_CELL",
    "QoSandTSP": "PIN_TO_CELL",
    "QoETarget": "PIN_TO_CELL",
    "QoEandTSP": "PIN_TO_CELL",
}

#: Why the four project families map onto the advertised kind at version 1.2.
#: Recorded here rather than in a commit message because the registry is what a
#: reader consults to find out whether a claim is earned.
ASSURANCE_FAMILY_WIRE_KIND_BASIS = (
    "Dispatcher ruling of 2026-08-25, on Operator terminal instruction, closing "
    "the condition this registry itself named: TrafficSteeringPreference's record "
    "said expressing a steering preference through the advertised kind was 'a "
    "versioned mapping OBJ1 owns and has not yet established'. This is that "
    "mapping. Version 1.2 adds QoSTarget and QoSandTSP under the dispatcher's "
    "2026-08-25 ruling: their actuator is cell steering and QoS is achieved "
    "through cell selection. The basis is that on this deployment the five "
    "actuation -- a single-cell action envelope on the one advertised control "
    "axis, serving_cell -- and one delivered evidence counter, the E2SM-KPM "
    "Style 4 per-UE serving-cell attribution that Gate 3 drove OTA in both "
    "directions. QoS predicates remain the distinct O1 RRU.PrbDl source, "
    "historically delivered as TS 32.435 for both cells, and the retained KPM "
    "Format 3 per-UE delivered counter. Present O1 delivery readiness is not "
    "implied by this hardware-free mapping. What differs between them is the target "
    "contract's predicates, "
    "which is where a difference between objectives belongs. Nothing is invented: "
    "the wire kind is the one the capability manifest advertises, and the policy "
    "body is unchanged."
)

_WIRE_KIND_BASIS_REF = (
    "assurance/objectives/registry.py ASSURANCE_FAMILY_TO_WIRE_KIND v1.2.0; "
    "docs/architecture/GATE4-ACCEPTANCE.md section on the wire-kind mapping"
)

_A1_INTERFACE = "A1-P v2 (Non-RT RIC to Near-RT RIC policy interface)"

#: The one A1 policy type the frozen deployment exposes.  Project-defined and
#: labelled as such: the interface is published, the type is ours.
_FROZEN_POLICY_TYPE = "AIC_UECellSteering_1.0.0"

_RC_STYLE3 = ServiceModelMapping(
    service_model="E2SM-RC",
    version="1.03",
    ran_function_id=3,
    procedure="CONTROL",
    style_id=3,
    style_name="Connected Mode Mobility Control",
    action_id=1,
    action_name="Handover Control",
    parameters=("targetPrimaryCellId",),
    basis=(
        "release profile E2SM-RC-STYLE3-ACTION1 (oran/nonrt/capability.py); "
        "control path E2SM_RC_1.03_STYLE3_ACTION1 in the deployed composition "
        "(published artifact oran-aic-lower-integration/1.0.0)"
    ),
)

_RC_STYLE2 = ServiceModelMapping(
    service_model="E2SM-RC",
    version="1.03",
    ran_function_id=3,
    procedure="CONTROL",
    style_id=2,
    style_name="Radio Resource Allocation Control",
    action_id=6,
    action_name="Slice-level PRB quota control",
    parameters=("slice_prb_policy_ratio",),
    basis=(
        "deployed composition capability manifest "
        "experimentalExtensions.E2SM_RC_STYLE2_ACTION6_QOS "
        "(serviceModel E2SM-RC_1.03_STYLE2_ACTION6, a1PolicyType NONE)"
    ),
)

# Gate 8's SliceSLATarget-only mapping.  This is deliberately separate from
# ``_RC_STYLE2`` because the QoS registry entries remain owned by the QAX lane.
# The full nested tree replaces no QoS field and makes no live-deployment claim.
_SLICE_RC_STYLE2_HF = ServiceModelMapping(
    service_model="E2SM-RC",
    version="1.03",
    ran_function_id=3,
    procedure="CONTROL",
    style_id=2,
    style_name="Radio Resource Allocation Control",
    action_id=6,
    action_name="Slice-level PRB quota control",
    parameters=(
        "RRM Policy Ratio List",
        "RRM Policy Ratio Group",
        "RRM Policy Member List",
        "PLMN Identity",
        "S-NSSAI(SST,optional SD)",
        "minPrbPolicyRatio",
        "maxPrbPolicyRatio",
        "dedicatedPrbPolicyRatio",
    ),
    basis=(
        "O-RAN.WG3.E2SM-RC-v01.03 section 8.4.3.6; "
        "oran/slice_actuator/vectors/style2-action6-dual-slice.json; "
        "oai_patches/e2sm_rc_style2_action6_slice_prb.patch (hardware-free only)"
    ),
)

_KPM_STYLE4 = ServiceModelMapping(
    service_model="E2SM-KPM",
    version="2.03",
    ran_function_id=2,
    procedure="REPORT",
    style_id=4,
    style_name="Condition-based UE-level report",
    action_name="",
    parameters=("UE.ServingCell",),
    basis=(
        "deployed composition capability manifest decisionKpis[UE.ServingCell] "
        "(E2SM_KPM_STYLE4_UEID_NODE_ATTRIBUTION, 1000 ms, requiredFor PIN_TO_CELL "
        "and READBACK); docs/integration/upper-registry-mapping.1.0.0.json"
    ),
)

_KPM_STYLE4_QOS = ServiceModelMapping(
    service_model="E2SM-KPM",
    version="2.03",
    ran_function_id=2,
    procedure="REPORT",
    style_id=4,
    style_name="Condition-based UE-level report",
    action_name="",
    parameters=("UE.ServingCell", "DRB.UEThpDl", "RRU.PrbTotDl"),
    basis=(
        "KPM Format 3 live per-UE indications retained in "
        "docs/integration/evidence/GATE3-OTA-20260824T080400Z-kpm-attribution.json; "
        "UE.ServingCell is the attribution/readback identity, while "
        "DRB.UEThpDl and RRU.PrbTotDl remain distinct per-UE delivered counters "
        "and are never aliased onto O1 cell-scope RRU.PrbDl"
    ),
)

_KPM_STYLE1 = ServiceModelMapping(
    service_model="E2SM-KPM",
    version="2.03",
    ran_function_id=2,
    procedure="REPORT",
    style_id=1,
    style_name="Periodic cell-level report",
    action_name="",
    parameters=("RRU.PrbDl",),
    basis="E2 capability inventory reportStyles (docs/phase-a/raw/*/inventory.json)",
)

_O1_PRB_DL = O1Measurement(
    name="RRU.PrbDl",
    file_format="3GPP TS 32.435 V10.0",
    definition="3GPP TS 28.552 V18.11.0",
    clause="5.1.1.2.1",
    unit="percent",
    scope_level="NRCellDU",
    delivered_by_deployment=True,
    basis=(
        "oran/o1/core.py parser; Provider 1.0.4 delivered this measType for both "
        "cells (docs/integration/CLAIM-BOUNDARY.md section 5)"
    ),
)

_O1_UE_THP_DL = O1Measurement(
    name="DRB.UEThpDl",
    file_format="3GPP TS 32.435 V10.0",
    definition="3GPP TS 28.552 V18.11.0",
    clause="5.1.1.3.1",
    unit="kbit/s",
    scope_level="NRCellDU",
    delivered_by_deployment=False,
    basis=(
        "declared optional (required false) by the deployed composition capability "
        "manifest and "
        "not delivered by the running Provider 1.0.4; the live confirmation of "
        "GAP-05 (docs/integration/CLAIM-BOUNDARY.md section 5)"
    ),
)

_ROLLBACK_MET = Premise(
    kind=PremiseKind.ROLLBACK_PATH,
    statement=(
        "the Write Gateway reverses every axis of a staged plan from its own "
        "snapshot and verifies recovery before settlement"
    ),
    met=True,
    basis="assurance/gateway/gateway.py; tests/assurance/test_kgw_recovery.py",
)


def _steering_actuator_premise(met: bool = True) -> Premise:
    return Premise(
        kind=PremiseKind.ACTUATOR_PATH,
        statement=(
            "an OFFICIAL_ORAN_DYNAMIC actuator exists end to end: R1 to Non-RT RIC "
            "to A1-P to xApp to FlexRIC to E2SM-RC Style 3 Action 1 to the gNB"
        ),
        met=met,
        basis=(
            "docs/integration/evidence/GATE3-OTA-20260824T085045Z-run.json "
            "(gatewayLog, policyIds, axes.trialOutcome SUCCESS)"
        ),
    )


OBJECTIVE_REGISTRY: Tuple[ObjectiveRecord, ...] = (
    # ---------------------------------------------------------------- OBJ1 --
    ObjectiveRecord(
        project_contract_id="objective/TrafficSteeringPreference",
        family="TrafficSteeringPreference",
        lane="OBJ1",
        intent_summary=(
            "Prefer that the UEs in scope be served by a stated cell of the "
            "deployment, and hold that preference for the whole hold period."
        ),
        standard_mapping=StandardMapping(
            policy_interface=_A1_INTERFACE,
            policy_type_id=_FROZEN_POLICY_TYPE,
            policy_type_kind="PROJECT_CONTRACT_POLICY_TYPE",
            control_service_models=(_RC_STYLE3,),
            measurement_service_models=(_KPM_STYLE4,),
            o1_measurements=(_O1_PRB_DL,),
            notes=(
                "The actuation and readback path is the one Gate 3 exercised on "
                "real equipment. What the frozen policy type advertises is the "
                "objective kind PIN_TO_CELL; expressing a steering preference "
                "through it is the versioned mapping this module now carries as "
                "ASSURANCE_FAMILY_TO_WIRE_KIND v1.2.0, which is what makes this "
                "family submittable. The project identifier stays on the left of "
                "that table and the advertised kind on the right; neither is "
                "written as the other."
            ),
        ),
        deployment_capability=DeploymentCapability(
            submittable=True,
            a1_policy_type_present=True,
            basis=_CAPABILITY_BASIS + "; " + _WIRE_KIND_BASIS_REF,
            blocking_reasons=(),
        ),
        support_state=SupportState.OTA_LIVE_VERIFIED,
        evidence_level=EvidenceLevel.OTA_RAW_EVIDENCE,
        evidence_refs=(
            # Gate 5, 2026-08-25. One success and one non-success on real
            # radio, per docs/integration/evidence/GATE5-OTA-RECORD.md.
            "docs/integration/evidence/"
            "GATE5-OTA-TrafficSteeringPreference-20260825T092005Z-run.json",
            "docs/integration/evidence/"
            "GATE5-OTA-TrafficSteeringPreference-20260825T092005Z-events.jsonl",
            "docs/integration/evidence/"
            "GATE5-OTA-TrafficSteeringPreference-20260825T092005Z-kpm-attribution.json",
            "docs/integration/evidence/"
            "GATE5-OTA-TrafficSteeringPreference-20260825T092005Z-gnb2-target-handover.log",
            "docs/integration/evidence/"
            "GATE5-OTA-TrafficSteeringPreference-20260825T092549Z-run.json",
            "docs/integration/evidence/"
            "GATE5-OTA-TrafficSteeringPreference-20260825T092549Z-events.jsonl",
            "docs/integration/evidence/GATE5-OTA-RECORD.md",
            "docs/integration/evidence/GATE5-OTA-SHA256SUMS",
        ),
        hardware_free_scenarios=(
            "positive", "negative", "malformed", "conflict", "stale", "missing",
            "timeout", "partial-effect", "duplicate", "fault",
        ),
        premises=(
            _steering_actuator_premise(),
            _ROLLBACK_MET,
            Premise(
                kind=PremiseKind.MEASUREMENT_SOURCE,
                statement=(
                    "per-UE serving-cell identity is reported at 1000 ms over "
                    "E2SM-KPM UE-level attribution"
                ),
                met=True,
                basis=(
                    "deployed composition capability manifest "
                    "decisionKpis[UE.ServingCell]; "
                    "docs/integration/evidence/"
                    "GATE3-OTA-20260824T085045Z-kpm-attribution.json"
                ),
            ),
            Premise(
                kind=PremiseKind.EVIDENCE_ORACLE,
                statement=(
                    "a corroborated AFTER-window readback decides the verdict, not "
                    "an A1 create or an E2 acknowledgement"
                ),
                met=True,
                basis="assurance/live/pin_to_cell_driver.py; task section 7.6",
            ),
        ),
        related_contracts={
            PIN_REGRESSION_FAMILY: (
                "docs/architecture/SEAMS-GATE4.md section 6, mapping version 1.2.0: "
                "PIN_TO_CELL is the single-cell, cardinality-one case of this "
                "family's parameter space. The regression contract is preserved "
                "unchanged and is not renamed into this family."
            )
        },
    ),
    ObjectiveRecord(
        project_contract_id="objective/QoSTarget",
        family="QoSTarget",
        lane="OBJ1",
        intent_summary=(
            "Hold a stated downlink quality-of-service floor for the cell in "
            "scope by selecting the serving cell whose measured QoS satisfies it."
        ),
        standard_mapping=StandardMapping(
            policy_interface=_A1_INTERFACE,
            policy_type_id=_FROZEN_POLICY_TYPE,
            policy_type_kind="PROJECT_CONTRACT_POLICY_TYPE",
            control_service_models=(_RC_STYLE3,),
            measurement_service_models=(_KPM_STYLE4_QOS,),
            o1_measurements=(_O1_PRB_DL, _O1_UE_THP_DL),
            notes=(
                "Actuator = cell steering; QoS is achieved through cell selection. "
                "Wire-kind mapping v1.2.0 carries the project family as PIN_TO_CELL "
                "in the released AIC_UECellSteering policy body. The mandatory QoS "
                "predicates remain O1 RRU.PrbDl from an UNLOCKED PerfMetricJob and "
                "the delivered KPM Format 3 per-UE DRB.UEThpDl counter. The O1 "
                "DRB.UEThpDl declaration remains undelivered and is not used. No A1 "
                "type or backend fork is added; Style 2 / Action 6 is deferred to "
                "the Gate 8 SliceSLATarget actuator design."
            ),
        ),
        deployment_capability=DeploymentCapability(
            submittable=True,
            a1_policy_type_present=True,
            basis=_CAPABILITY_BASIS + "; " + _WIRE_KIND_BASIS_REF,
            blocking_reasons=(),
        ),
        support_state=SupportState.OTA_LIVE_VERIFIED,
        evidence_level=EvidenceLevel.OTA_RAW_EVIDENCE,
        evidence_refs=(
            # Gate 5, 2026-08-28, E2 epochs 211 (nb 3584) / 209 (nb 2816).
            # Re-run under the corrected runtime: the permit's lease is sized
            # at issuance and carried verbatim, and a producer-claimed O1
            # timestamp is promoted only where this host's own record of the
            # file's arrival agrees with it.  One 120 s hold holds both
            # mandatory predicates -- RRU.PrbDl mean 25.0 %, per-UE
            # DRB.UEThpDl mean 2164.5 kbit/s, every sample SYNCHRONISED, no
            # missing intervals.  The runs this supersedes are retained on
            # disk and narrated in the record; they are not cited as support.
            "docs/integration/evidence/"
            "GATE5-OTA-QoSTarget-20260828T140345Z-run.json",
            "docs/integration/evidence/"
            "GATE5-OTA-QoSTarget-20260828T140345Z-events.jsonl",
            "docs/integration/evidence/"
            "GATE5-OTA-QoSTarget-20260828T140345Z-scope-archive.json",
            "docs/integration/evidence/GATE5-OTA-RECORD.md",
            "docs/integration/evidence/GATE5-OTA-SHA256SUMS",
        ),
        hardware_free_scenarios=(
            "positive", "negative", "malformed", "conflict", "stale", "missing",
            "timeout", "partial-effect", "duplicate", "fault",
        ),
        premises=(
            Premise(
                kind=PremiseKind.A1_POLICY_TYPE,
                statement=(
                    "the released AIC UE cell-steering policy type can carry the "
                    "versioned PIN_TO_CELL wire mapping"
                ),
                met=True,
                basis=_WIRE_KIND_BASIS_REF,
            ),
            _steering_actuator_premise(),
            Premise(
                kind=PremiseKind.MEASUREMENT_SOURCE,
                statement=(
                    "the deployment delivers the O1 cell-scope PRB source and the "
                    "KPM Format 3 per-UE throughput source, and judges the QoS "
                    "predicates from them over one hold without aliasing their "
                    "scopes"
                ),
                met=True,
                basis=(
                    "both counters delivered and judged over the same 120 s hold "
                    "in docs/integration/evidence/"
                    "GATE5-OTA-QoSTarget-20260828T140345Z-run.json: three "
                    "TS 32.435 RRU.PrbDl samples on NRCellDU-1 and three KPM "
                    "Format 3 DRB.UEThpDl samples scoped to the UE, all "
                    "SYNCHRONISED with no missing intervals. The gNB2 export "
                    "wiring that had left delivery NOT_PASSED at Gate 5 stage 1 "
                    "is fixed: docs/integration/evidence/"
                    "GATE5-O1PM-20260825-gnb2-export-wiring.md"
                ),
            ),
            _ROLLBACK_MET,
        ),
    ),
    ObjectiveRecord(
        project_contract_id="objective/QoSandTSP",
        family="QoSandTSP",
        lane="OBJ1",
        intent_summary=(
            "Hold the quality-of-service floor and the steering preference "
            "together, judged inside one trial rather than assembled from two."
        ),
        standard_mapping=StandardMapping(
            policy_interface=_A1_INTERFACE,
            policy_type_id=_FROZEN_POLICY_TYPE,
            policy_type_kind="PROJECT_CONTRACT_POLICY_TYPE",
            control_service_models=(_RC_STYLE3,),
            measurement_service_models=(_KPM_STYLE4_QOS,),
            o1_measurements=(_O1_PRB_DL, _O1_UE_THP_DL),
            notes=(
                "Actuator = cell steering; QoS is achieved through cell selection. "
                "Wire-kind mapping v1.2.0 carries one PIN_TO_CELL policy action. "
                "Task section 8 still makes the QoS predicates (O1 RRU.PrbDl and "
                "KPM Format 3 per-UE DRB.UEThpDl) plus the TSP serving-cell MIN/MAX "
                "predicates mandatory inside the same trial and validity region. "
                "No Style 2 / Action 6 or new A1 type is claimed here."
            ),
        ),
        deployment_capability=DeploymentCapability(
            submittable=True,
            a1_policy_type_present=True,
            basis=_CAPABILITY_BASIS + "; " + _WIRE_KIND_BASIS_REF,
            blocking_reasons=(),
        ),
        support_state=SupportState.OTA_LIVE_VERIFIED,
        evidence_level=EvidenceLevel.OTA_RAW_EVIDENCE,
        evidence_refs=(
            # Gate 5, 2026-08-28, re-run under the corrected runtime.  One
            # trial, one hold, four mandatory predicates: dl-prb-headroom and
            # ue-throughput-floor from the QoS half, serving-cell-preferred
            # min/max from the steering half, all PASS with the serving cell
            # held at 87654321 for all 121 identity samples and every sample
            # SYNCHRONISED.  This is the joint judgement the family claims,
            # not two component results assembled afterwards.
            "docs/integration/evidence/"
            "GATE5-OTA-QoSandTSP-20260828T140635Z-run.json",
            "docs/integration/evidence/"
            "GATE5-OTA-QoSandTSP-20260828T140635Z-events.jsonl",
            "docs/integration/evidence/"
            "GATE5-OTA-QoSandTSP-20260828T140635Z-scope-archive.json",
            "docs/integration/evidence/GATE5-OTA-RECORD.md",
            "docs/integration/evidence/GATE5-OTA-SHA256SUMS",
        ),
        hardware_free_scenarios=(
            "positive", "negative", "malformed", "conflict", "stale", "missing",
            "timeout", "partial-effect", "duplicate", "fault",
        ),
        component_families=("QoSTarget", "TrafficSteeringPreference"),
        premises=(
            Premise(
                kind=PremiseKind.A1_POLICY_TYPE,
                statement=(
                    "one released steering policy type carries the shared "
                    "servingCell actuation for both component objectives"
                ),
                met=True,
                basis=_WIRE_KIND_BASIS_REF,
            ),
            _steering_actuator_premise(),
            Premise(
                kind=PremiseKind.MEASUREMENT_SOURCE,
                statement=(
                    "the deployment delivers the O1 QoS source, the KPM Format 3 "
                    "QoS source and KPM serving-cell attribution together, inside "
                    "one trial rather than separately"
                ),
                met=True,
                basis=(
                    "all three delivered over one 120 s hold in "
                    "docs/integration/evidence/"
                    "GATE5-OTA-QoSandTSP-20260828T140635Z-run.json: RRU.PrbDl on "
                    "NRCellDU-1, per-UE DRB.UEThpDl, and 121 serving-cell identity "
                    "samples all reading 87654321, no missing intervals"
                ),
            ),
            Premise(
                kind=PremiseKind.EVIDENCE_ORACLE,
                statement=(
                    "one trial can carry both component predicate sets as mandatory "
                    "and judge them over the same hold"
                ),
                met=True,
                basis=(
                    "assurance/contracts/target.py TargetContract.predicates; "
                    "the shared harness asserts single-trial joint judgement "
                    "(tests/assurance/objective_harness.py)"
                ),
            ),
            _ROLLBACK_MET,
        ),
    ),
    # ---------------------------------------------------------------- OBJ2 --
    ObjectiveRecord(
        project_contract_id="objective/UELevelTarget",
        family="UELevelTarget",
        lane="OBJ2",
        intent_summary=(
            "Hold a stated per-UE condition for one identified UE for the whole "
            "hold period, rather than a cell-wide aggregate."
        ),
        standard_mapping=StandardMapping(
            policy_interface=_A1_INTERFACE,
            policy_type_id=_FROZEN_POLICY_TYPE,
            policy_type_kind="PROJECT_CONTRACT_POLICY_TYPE",
            control_service_models=(_RC_STYLE3,),
            measurement_service_models=(_KPM_STYLE4,),
            notes=(
                "The one per-UE quantity this deployment declares is the serving "
                "cell identity, which is an identity and not a performance "
                "measurement. Per-UE RRU.PrbTotDl is declared by no capability "
                "entry and may not be aliased onto cell-scope RRU.PrbDl, so a "
                "per-UE RATE target still has no source here and this family does "
                "not state one: its mandatory predicates are the MIN/MAX identity "
                "pair over the delivered serving-cell attribution, which needs no "
                "rate. That is the limit of what this family claims, and it is why "
                "the missing rate measurement is a scope limit rather than a "
                "submission block. The steering it does state reaches the radio "
                "through ASSURANCE_FAMILY_TO_WIRE_KIND v1.2.0."
            ),
        ),
        deployment_capability=DeploymentCapability(
            submittable=True,
            a1_policy_type_present=True,
            basis=_CAPABILITY_BASIS + "; " + _WIRE_KIND_BASIS_REF,
            blocking_reasons=(),
        ),
        support_state=SupportState.OTA_LIVE_VERIFIED,
        evidence_level=EvidenceLevel.OTA_RAW_EVIDENCE,
        evidence_refs=(
            # Gate 5, 2026-08-25.  The reverse direction, 87654321 -> 12345678,
            # which this family expresses because it reads the scope's cells
            # rather than fixing them in module constants.
            "docs/integration/evidence/"
            "GATE5-OTA-UELevelTarget-20260825T092437Z-run.json",
            "docs/integration/evidence/"
            "GATE5-OTA-UELevelTarget-20260825T092437Z-events.jsonl",
            "docs/integration/evidence/"
            "GATE5-OTA-UELevelTarget-20260825T092437Z-kpm-attribution.json",
            "docs/integration/evidence/"
            "GATE5-OTA-UELevelTarget-20260825T092437Z-gnb2-source-handover.log",
            "docs/integration/evidence/"
            "GATE5-OTA-UELevelTarget-20260825T092437Z-gnb1-target-handover.log",
            "docs/integration/evidence/"
            "GATE5-OTA-UELevelTarget-20260825T092437Z-scope-archive.json",
            "docs/integration/evidence/"
            "GATE5-OTA-UELevelTarget-20260825T092618Z-run.json",
            "docs/integration/evidence/"
            "GATE5-OTA-UELevelTarget-20260825T092618Z-events.jsonl",
            "docs/integration/evidence/GATE5-OTA-RECORD.md",
            "docs/integration/evidence/GATE5-OTA-SHA256SUMS",
        ),
        hardware_free_scenarios=(
            "positive",
            "negative",
            "malformed",
            "conflict",
            "stale",
            "missing",
            "timeout",
            "partial-effect",
            "duplicate",
            "fault",
        ),
        premises=(
            Premise(
                kind=PremiseKind.SCOPE_IDENTITY,
                statement=(
                    "a UE can be identified and its records attributed to one E2 "
                    "node across the trial"
                ),
                met=True,
                basis=(
                    "E2SM-KPM UE-level attribution by amfUeNgapId, retained in "
                    "docs/integration/evidence/"
                    "GATE3-OTA-20260824T085045Z-kpm-attribution.json"
                ),
            ),
            Premise(
                kind=PremiseKind.MEASUREMENT_SOURCE,
                statement=(
                    "a per-UE serving-cell identity is reported at 1000 ms over "
                    "E2SM-KPM UE-level attribution, which is the per-UE "
                    "quantity this family's predicates are scoped to; see "
                    "deployment_capability.blocking_reasons and "
                    "standard_mapping.notes for what remains out of scope"
                ),
                met=True,
                basis=(
                    "deployed composition capability manifest "
                    "decisionKpis[UE.ServingCell]; "
                    "docs/integration/evidence/"
                    "GATE3-OTA-20260824T085045Z-kpm-attribution.json"
                ),
            ),
            _steering_actuator_premise(),
            _ROLLBACK_MET,
            Premise(
                kind=PremiseKind.EVIDENCE_ORACLE,
                statement=(
                    "a corroborated AFTER-window readback of the identity pair "
                    "decides the verdict, not an A1 create or an E2 "
                    "acknowledgement"
                ),
                met=True,
                basis="assurance/live/pin_to_cell_driver.py; task section 7.6",
            ),
        ),
    ),
    ObjectiveRecord(
        project_contract_id="objective/QoETarget",
        family="QoETarget",
        lane="OBJ2",
        intent_summary=(
            "Hold a stated application-level quality of experience for the UEs "
            "in scope, judged from a real application or UE observation source."
        ),
        standard_mapping=StandardMapping(
            policy_interface=_A1_INTERFACE,
            policy_type_id=_FROZEN_POLICY_TYPE,
            policy_type_kind="PROJECT_CONTRACT_POLICY_TYPE",
            control_service_models=(_RC_STYLE3,),
            measurement_service_models=(_KPM_STYLE4,),
            notes=(
                "No published measurement in this deployment carries an "
                "application-level experience quantity. Design section 10 permits "
                "this family only where a real application or UE observation "
                "source is correlated to the trial episode, so the mapping below "
                "records the control path this family would use and states plainly "
                "that its measurement half does not exist here."
            ),
        ),
        deployment_capability=DeploymentCapability(
            submittable=True, a1_policy_type_present=True,
            basis=_CAPABILITY_BASIS + "; ASSURANCE_FAMILY_TO_WIRE_KIND v1.3.0",
            blocking_reasons=(),
        ),
        support_state=SupportState.HARDWARE_FREE_VERIFIED,
        evidence_level=EvidenceLevel.HARDWARE_FREE_ROUND_TRIP,
        hardware_free_scenarios=("positive", "negative", "malformed", "conflict", "stale", "missing", "timeout", "partial-effect", "duplicate", "fault"),
        premises=(
            Premise(
                kind=PremiseKind.MEASUREMENT_SOURCE,
                statement=(
                    "a real application or UE quality-of-experience source exists "
                    "and can be correlated to the trial episode"
                ),
                met=False,
                live_only=True,
                basis=(
                    "docs/integration/upper-registry-mapping.1.0.0.json declares "
                    "only O1:RRU.PrbDl, O1:DRB.UEThpDl, E2:RRU.PrbTotDl and "
                    "E2:UE.ServingCell; collectors/multi_ue_collector.py can probe "
                    "transport throughput/latency but provides no application or UE "
                    "experience observation correlated to the same trial episode"
                ),
            ),
            Premise(
                kind=PremiseKind.EVIDENCE_ORACLE,
                statement=(
                    "an experience verdict can be reproduced from retained raw "
                    "evidence"
                ),
                met=False,
                live_only=True,
                basis=(
                    "follows from the absent source: assurance/collector registers "
                    "raw O1/KPM adapters, not an application/UE QoE trace source; a "
                    "substitute derived from PRB occupancy or transport throughput "
                    "would be a different measurement reported under this objective's "
                    "name"
                ),
            ),
            _steering_actuator_premise(),
        ),
    ),
    ObjectiveRecord(
        project_contract_id="objective/QoEandTSP",
        family="QoEandTSP",
        lane="OBJ2",
        intent_summary=(
            "Hold the experience target and the steering preference together, "
            "judged inside one trial rather than assembled from two."
        ),
        standard_mapping=StandardMapping(
            policy_interface=_A1_INTERFACE,
            policy_type_id=_FROZEN_POLICY_TYPE,
            policy_type_kind="PROJECT_CONTRACT_POLICY_TYPE",
            control_service_models=(_RC_STYLE3,),
            measurement_service_models=(_KPM_STYLE4,),
            notes=(
                "Inherits the experience family's missing measurement half. The "
                "steering half is real, which is exactly why the combination may "
                "not be reported as a pass: task section 8 forbids composing a "
                "combined objective from the component that happens to work."
            ),
        ),
        deployment_capability=DeploymentCapability(
            submittable=True, a1_policy_type_present=True,
            basis=_CAPABILITY_BASIS + "; ASSURANCE_FAMILY_TO_WIRE_KIND v1.3.0",
            blocking_reasons=(),
        ),
        support_state=SupportState.HARDWARE_FREE_VERIFIED,
        evidence_level=EvidenceLevel.HARDWARE_FREE_ROUND_TRIP,
        hardware_free_scenarios=("positive", "negative", "malformed", "conflict", "stale", "missing", "timeout", "partial-effect", "duplicate", "fault"),
        component_families=("QoETarget", "TrafficSteeringPreference"),
        premises=(
            Premise(
                kind=PremiseKind.MEASUREMENT_SOURCE,
                statement=(
                    "both component predicate sets can be measured over the same "
                    "hold in one trial"
                ),
                met=False,
                live_only=True,
                basis="inherited from QoETarget: no application experience source",
            ),
        ),
    ),
    # ---------------------------------------------------------------- OBJ3 --
    ObjectiveRecord(
        project_contract_id="objective/SliceSLATarget",
        family="SliceSLATarget",
        lane="OBJ3",
        intent_summary=(
            "Hold a stated service-level condition for one preconfigured "
            "network slice, correlating Core and RAN evidence for that slice."
        ),
        standard_mapping=StandardMapping(
            policy_interface=_A1_INTERFACE,
            policy_type_id=None,
            policy_type_kind="NONE",
            control_service_models=(_SLICE_RC_STYLE2_HF,),
            measurement_service_models=(_KPM_STYLE1,),
            o1_measurements=(_O1_PRB_DL,),
            notes=(
                "Gate 8 supplies the project A1 schema, typed worker, full nested "
                "Style 2/Action 6 codec, and an unapplied OAI patch. The frozen "
                "deployed composition still carries one default slice, advertises no "
                "AIC_SliceSLATarget_1.0.0 type, and delivers no S-NSSAI-labelled "
                "RAN/Core evidence. The complete shared hardware-free contract "
                "round-trip is verified; it is neither deployment capability nor "
                "OTA evidence."
            ),
        ),
        deployment_capability=DeploymentCapability(
            submittable=False,
            a1_policy_type_present=False,
            basis=_CAPABILITY_BASIS,
            blocking_reasons=(
                "NO_SLICE_SCOPED_MEASUREMENT: no delivered measurement is "
                "dimensioned by S-NSSAI",
                "NO_CORE_SLICE_EVIDENCE: no Core-side per-slice evidence source is "
                "bound to the Measurement Collector",
                "A1_SLICE_TYPE_NOT_DEPLOYED: AIC_SliceSLATarget_1.0.0 exists as a "
                "hardware-free artifact but is not installed or advertised by the "
                "deployed composition (published artifact "
                "oran-aic-lower-integration/1.0.0)",
                "NO_LIVE_SLICE_EFFECT_ORACLE: the mock RC/KPM readback contract is "
                "not connected to live E2SM-KPM or TS 28.552 S-NSSAI-labelled PM",
            ),
        ),
        # Fail closed, and stay advertised (design section 10).  The four
        # blocking reasons retain the independent deployment gaps: the
        # objective cannot be observed end-to-end, the project A1 type is not
        # deployed, and the mock effect contract is not a live oracle. Evidence
        # level is CONTRACT_DECLARED and not higher: the standard mapping, the
        # policy lifecycle and the KPI declaration in
        # ``assurance/objectives/slice_sla_target.py`` exist and nothing has
        # executed live -- which is exactly the level Gate 4's acceptance names as
        # justifying no support claim at all.  ``evidence_refs`` stays empty
        # because it is OTA raw evidence only.
        support_state=SupportState.HARDWARE_FREE_VERIFIED,
        evidence_level=EvidenceLevel.HARDWARE_FREE_ROUND_TRIP,
        hardware_free_scenarios=("positive", "negative", "malformed", "conflict", "stale", "missing", "timeout", "partial-effect", "duplicate", "fault"),
        premises=(
            Premise(
                kind=PremiseKind.A1_POLICY_TYPE,
                statement=(
                    "a versioned A1-P policy/status type preserves SliceSLATarget "
                    "scope and quota fields through producer discovery"
                ),
                met=True,
                basis=(
                    "contracts/oran-aic/gate8-slice-actuator-hf/"
                    "AIC_SliceSLATarget_1.0.0.{policy,status}.schema.json; "
                    "tests/assurance/test_slice_actuator_a1_worker.py"
                ),
            ),
            Premise(
                kind=PremiseKind.ACTUATOR_PATH,
                statement=(
                    "the hardware-free A1 worker, FlexRIC encoder and OAI patch "
                    "implement the complete Style 2/Action 6 request path"
                ),
                met=True,
                basis=(
                    "oran/slice_actuator; src/xapp/flexric_adapter; "
                    "oai_patches/e2sm_rc_style2_action6_slice_prb.patch; "
                    "tests/assurance/test_slice_actuator_*.py"
                ),
            ),
            Premise(
                kind=PremiseKind.SCOPE_IDENTITY,
                statement=(
                    "a preconfigured slice identity exists and is distinguishable "
                    "from the cell it runs on"
                ),
                met=False,
                live_only=True,
                basis=(
                    "configs/oai/gnb.sa.band78.fr1.*.conf declare one snssaiList "
                    "entry sst=1 on both cells and configs/nrue.conf the matching "
                    "PDU session; a single default slice is not distinguishable "
                    "from the cell. tools/labctl/assets/lower-1.0.0/"
                    "start-gnbs-prb24-dual-slice-source-first.sh names external "
                    "runtime/oai dual-slice conf paths, but that launcher name is not "
                    "a retained runtime configuration or Core evidence"
                ),
            ),
            Premise(
                kind=PremiseKind.MEASUREMENT_SOURCE,
                statement="RAN and Core evidence are both available per slice",
                met=False,
                live_only=True,
                basis=(
                    "docs/integration/upper-registry-mapping.1.0.0.json declares "
                    "RRU.PrbDl at NRCellDU scope only; no Core measurement source "
                    "is registered in assurance/collector"
                ),
            ),
            Premise(
                kind=PremiseKind.ROLLBACK_PATH,
                statement=(
                    "the hardware-free worker snapshots and restores the exact "
                    "previous min/max/dedicated ratio under a newer fencing token"
                ),
                met=True,
                basis=(
                    "oran/slice_actuator/worker.py; "
                    "src/xapp/flexric_adapter/tests/slice_prb_control_harness.cc; "
                    "tests/assurance/test_slice_actuator_a1_worker.py"
                ),
            ),
            Premise(
                kind=PremiseKind.EVIDENCE_ORACLE,
                statement=(
                    "live RC quota readback and a TS 28.552 counter labelled with "
                    "the same S-NSSAI jointly decide effect; an ACK never does"
                ),
                met=False,
                live_only=True,
                basis=(
                    "oran/slice_actuator/measurement.py defines the mock contract; "
                    "no live adapter or OTA evidence exists in this gate"
                ),
            ),
        ),
    ),
    # ------------------------------------------------- preserved regression --
    ObjectiveRecord(
        project_contract_id=f"objective/{PIN_REGRESSION_FAMILY}",
        family=PIN_REGRESSION_FAMILY,
        lane="OBJ1",
        intent_summary=(
            "Pin one identified UE to one stated cell and observe that every "
            "sample of every completed window of the hold saw exactly that cell."
        ),
        standard_mapping=StandardMapping(
            policy_interface=_A1_INTERFACE,
            policy_type_id=_FROZEN_POLICY_TYPE,
            policy_type_kind="PROJECT_CONTRACT_POLICY_TYPE",
            control_service_models=(_RC_STYLE3,),
            measurement_service_models=(_KPM_STYLE4,),
            o1_measurements=(_O1_PRB_DL,),
            notes=(
                "The preserved exact-regression contract, kept under its own "
                "identifier and its own objective kind PIN_TO_CELL. It is not "
                "renamed into the new Traffic Steering family; the relationship "
                "runs the other way and is versioned in related_contracts."
            ),
        ),
        deployment_capability=DeploymentCapability(
            submittable=True,
            a1_policy_type_present=True,
            basis=_CAPABILITY_BASIS,
        ),
        support_state=SupportState.OTA_LIVE_VERIFIED,
        evidence_level=EvidenceLevel.OTA_RAW_EVIDENCE,
        premises=(
            _steering_actuator_premise(),
            _ROLLBACK_MET,
            Premise(
                kind=PremiseKind.MEASUREMENT_SOURCE,
                statement="per-UE serving-cell identity at 1000 ms over E2SM-KPM",
                met=True,
                basis=(
                    "docs/integration/evidence/"
                    "GATE3-OTA-20260824T085045Z-kpm-attribution.json"
                ),
            ),
            Premise(
                kind=PremiseKind.EVIDENCE_ORACLE,
                statement=(
                    "the terminal verdict is reproducible from the retained run "
                    "record and event stream"
                ),
                met=True,
                basis=(
                    "docs/integration/evidence/GATE3-OTA-20260824T085045Z-run.json "
                    "and -events.jsonl, digested in "
                    "docs/integration/evidence/GATE3-OTA-SHA256SUMS"
                ),
            ),
        ),
        related_contracts={
            "TrafficSteeringPreference": (
                "docs/architecture/SEAMS-GATE4.md section 6, mapping version 1.2.0: "
                "this contract is the cardinality-one case of that family's "
                "parameter space. The mapping is a stated relationship, not a "
                "rename; this record and its contract identifiers are unchanged."
            )
        },
        evidence_refs=(
            "docs/integration/evidence/GATE3-OTA-20260824T085045Z-run.json",
            "docs/integration/evidence/GATE3-OTA-20260824T085045Z-events.jsonl",
            "docs/integration/evidence/GATE3-OTA-20260824T085045Z-kpm-attribution.json",
            "docs/integration/evidence/GATE3-OTA-SHA256SUMS",
        ),
        is_regression_contract=True,
    ),
)
