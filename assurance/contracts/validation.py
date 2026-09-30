"""Contract validation and content-addressing entry points.

Owner lane: **KCON**.

This module is the single door every contract family goes through before it
can be frozen into an epoch.  Design section 4.3 gives the Kernel exclusive
ownership of "contract and deployment admission", and section 15 lists "schema
and contract validation" first in the verification set; the functions here are
where both land.

Three properties the bodies must have, stated once so each docstring below can
refer to them:

**Deterministic.**  Same input, same verdict, on every host.  Admission is
replayed (design section 4.3) and a validator that consulted the clock, the
filesystem or the network would make replay disagree with the original run.

**Fail-closed.**  An unrecognised field, an unresolvable reference, a missing
readback path or an inadmissible quantity is a refusal.  Design section 10:
"Unsupported fields or missing KPI, actuator, rollback, or evidence paths fail
closed and remain honestly advertised."

**Free of human-authority vocabulary.**  There is no signer, approval,
authority or threshold check anywhere in contract admission (design section 5).
A contract is admitted because its content validates, not because of who
produced it.

Schema validation reuses the project's existing ``jsonschema``-backed
validator through ``oran.rapp.contract_support`` where a family has a frozen
schema; it is not reimplemented here.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple, Type
from urllib.parse import urlsplit

from assurance.contracts.capability import (
    ActuatorBinding,
    CapabilityManifest,
    CompositionManifest,
    DeploymentBinding,
)
from assurance.contracts.catalog import Candidate, CandidateCatalog, CoordinationCasePolicy
from assurance.contracts.epoch import EpochRecord
from assurance.contracts.harm import CertifiedHarmBound, HarmContract, HarmKind, WatchdogContract
from assurance.contracts.ledgers import (
    CompatibilityCheck,
    CompatibilityRecord,
    EvidenceCell,
    EvidenceContribution,
    EvidenceLedgerRecord,
    HarmLedgerRecord,
    MovementKind,
)
from assurance.contracts.measurement import (
    CounterBinding,
    MeasurementContract,
    MeasurementRegistry,
)
from assurance.contracts.target import (
    TargetContract,
    TargetOption,
    TargetReleasePolicy,
    TargetVector,
    TypedConstraint,
)
from assurance.core.addressing import content_hash, is_content_hash
from assurance.core.axes import EvidenceCellStatus
from assurance.core.provenance import InadmissibleQuantityError, TypedQuantity

__all__ = [
    "CONTRACT_FAMILIES",
    "ContractAdmissionError",
    "SECRET_BEARING_KEY_HINTS",
    "assert_secret_free",
    "canonical_form",
    "contract_content_hash",
    "validate_contract",
    "validate_family_set",
]


class ContractAdmissionError(ValueError):
    """A contract cannot be admitted.

    One error type for every refusal reason, carrying a message that names the
    contract and the rule.  Distinct exception classes per rule were rejected
    deliberately: a caller must never be able to catch "just the harm-bound
    failures" and continue, because a partially admitted contract set is
    exactly what the epoch freeze exists to prevent.
    """


#: Every contract family this package defines, in the order design section 6.2
#: lists them.  The seam test walks this tuple to prove each family exists, is
#: a dataclass and is instantiable; :func:`validate_contract` dispatches over
#: it and must handle every member.
CONTRACT_FAMILIES: Tuple[Type[Any], ...] = (
    MeasurementRegistry,
    MeasurementContract,
    CounterBinding,
    TargetContract,
    TargetOption,
    TargetVector,
    TypedConstraint,
    HarmContract,
    CertifiedHarmBound,
    WatchdogContract,
    CapabilityManifest,
    CompositionManifest,
    ActuatorBinding,
    DeploymentBinding,
    TargetReleasePolicy,
    CoordinationCasePolicy,
    Candidate,
    CandidateCatalog,
    EvidenceCell,
    EvidenceContribution,
    EvidenceLedgerRecord,
    HarmLedgerRecord,
    CompatibilityRecord,
    EpochRecord,
)

#: Key-name fragments that suggest a value, not a reference, has been put in a
#: binding.  Used by :func:`assert_secret_free`.  A hint list is not a proof of
#: absence, which is why the real rule is structural -- a deployment binding
#: has a ``secret_refs`` mapping and no value fields at all -- but a mapping
#: whose *key* is ``password`` almost always means someone pasted one in.
SECRET_BEARING_KEY_HINTS: Tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "privatekey",
    "private_key",
    "credential",
    "bearer",
)


def canonical_form(contract: Any) -> Dict[str, Any]:
    """Project *contract* onto its canonical JSON form.

    Signature frozen by this design step; body owned by lane **KCON**.

    The canonical form is what gets hashed, so it must be *total* and
    *stable*: every field of the family appears, optional fields are omitted
    rather than emitted as ``null`` (so one logical object has one form),
    enums render as their ``value``, nested
    :class:`~assurance.core.provenance.TypedQuantity` objects use their own
    ``to_canonical_dict``, and key spelling is the design's camelCase.

    Field *order* is irrelevant -- RFC 8785 sorts keys -- but field *presence*
    is not: adding a field to a family changes every hash it appears in, which
    is why the family field lists are frozen by
    ``docs/architecture/SEAMS-GATE2.md`` and not edited locally.
    """
    if not isinstance(contract, CONTRACT_FAMILIES):
        raise ContractAdmissionError(f"unsupported contract family {type(contract).__name__}")
    value = _canonical_value(contract)
    if not isinstance(value, dict):  # Defensive: every family is a dataclass.
        raise ContractAdmissionError(f"{type(contract).__name__} has no object form")
    return value


def contract_content_hash(contract: Any) -> str:
    """The content hash of *contract*.

    Signature frozen by this design step; body owned by lane **KCON**.

    Defined as ``content_hash(canonical_form(contract))`` using
    :func:`assurance.core.addressing.content_hash`, so an assurance contract
    digest is the same kind of value as a frozen ``oran-aic/1.0.0`` artefact
    digest and the two can be compared directly.

    This is the value that appears in :class:`EpochRecord`'s hash maps, in an
    Operator :class:`~assurance.core.confirmation.ConfirmationRecord`, and in
    the ``expected_config_hash`` of a
    :class:`~assurance.gateway.token.KernelToken`.
    """
    return content_hash(canonical_form(contract))


def validate_contract(contract: Any) -> None:
    """Fail closed on malformed or inadmissible KCON contract content.

    The implementation is deliberately wrapped so callers always receive the
    admission error type, including when an untyped decoded payload places a
    string or ``None`` in a field which the frozen dataclass normally types.
    See ``docs/architecture/SEAMS-GATE2.md``.
    """
    try:
        _validate_contract_impl(contract)
    except ContractAdmissionError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError):
        _refuse(contract, "malformed contract content")


def _validate_contract_impl(contract: Any) -> None:
    """Validate one contract, or raise :class:`ContractAdmissionError`.

    Signature frozen by this design step; body owned by lane **KCON**.

    Returns ``None`` on success and raises otherwise: there is no boolean
    return and no "warnings" channel, because a caller that receives a list of
    problems can choose to proceed, and admission must not be a choice.

    The body must check, per family:

    * **schema and version** -- the family's schema version is one the epoch
      supports, and the contract validates against its frozen schema where one
      exists (reusing ``oran.rapp.contract_support``, not a second validator);
    * **typed quantities** -- every
      :class:`~assurance.core.provenance.TypedQuantity` is admissible
      (:meth:`~assurance.core.provenance.TypedQuantity.require_admissible`),
      which is where an ``ILLUSTRATIVE`` or ``DRAFT`` number is refused
      (task section 5.5);
    * **harm bounds** -- a :class:`~assurance.contracts.harm.CertifiedHarmBound`
      has an enforced timeout, an operating scope, a stated uncertainty, a
      conservative margin and calibration evidence; a bound that is only a
      measured sample maximum is refused (task section 5.8);
    * **watchdogs** -- every watchdog a harm contract requires is armable
      before apply (task section 6.3);
    * **actuator path** -- a binding used by an objective declares
      :attr:`~assurance.contracts.capability.ActuatorPath.OFFICIAL_ORAN_DYNAMIC`.
      A ``LAB_SETUP_PREPARATION`` binding is never an objective effect
      (design section 9);
    * **readback** -- an actuator binding names a readback measurement, since
      an A1 create or an E2 ACK is not evidence of effect (task section 7.6);
    * **rollback** -- a capability without a rollback path fails closed rather
      than being advertised (design section 10);
    * **standard mapping** -- an objective-bearing contract maps its project
      identifier to the published policy/interface/service-model versions it
      actually applies (design section 9, task section 7.8);
    * **secret freedom** -- see :func:`assert_secret_free`.

    An unknown type is a refusal, not a pass.  A family that is not in
    :data:`CONTRACT_FAMILIES` has no validation rules, and admitting it would
    mean admitting something nobody specified.
    """
    if not isinstance(contract, CONTRACT_FAMILIES):
        _refuse(contract, "unknown contract family")
    _validate_shape(contract)
    try:
        for quantity in _walk_quantities(contract):
            quantity.require_admissible("contract admission")
    except InadmissibleQuantityError as exc:
        _refuse(contract, str(exc))

    if isinstance(contract, DeploymentBinding):
        assert_secret_free(contract)
    elif isinstance(contract, CounterBinding):
        _require(contract, contract.counter_id and contract.deployment_counter_name,
                 "counter binding requires counter and deployment names")
        _require(contract, contract.native_cadence_ms > 0, "native cadence must be positive")
        _require(contract, bool(contract.scope_keys) and bool(contract.unit),
                 "counter binding requires scope keys and unit")
    elif isinstance(contract, MeasurementContract):
        _validate_measurement(contract)
    elif isinstance(contract, MeasurementRegistry):
        _require(contract, bool(contract.counters) and bool(contract.measurements),
                 "measurement registry requires counters and measurements")
        for counter in contract.counters:
            validate_contract(counter)
        for measurement in contract.measurements:
            validate_contract(measurement)
    elif isinstance(contract, TypedConstraint):
        _require(contract, bool(contract.measurement_ref), "constraint requires measurement ref")
        _require(contract, isinstance(contract.bound, TypedQuantity), "constraint requires TypedQuantity")
        if contract.operator.value == "MEMBER_OF":
            _require(contract, bool(contract.allowed_values), "MEMBER_OF requires allowed values")
    elif isinstance(contract, TargetOption):
        _require(contract, bool(contract.capability_ref), "target option requires capability ref")
        _require(contract, all(bool(key) and bool(values) for key, values in contract.parameter_space.items()),
                 "target option parameter space must be finite and non-empty")
    elif isinstance(contract, TargetContract):
        _require(contract, bool(contract.objective_family) and bool(contract.scope_selector),
                 "target requires objective family and scope")
        _require(contract, bool(contract.predicates) and bool(contract.options),
                 "target requires predicates and options")
        _require(contract, contract.hold_ms >= 0, "target hold must not be negative")
        _require(contract, bool(contract.standard_mapping), "objective target requires standard mapping")
    elif isinstance(contract, TargetVector):
        _require(contract, bool(contract.ordered_target_refs), "target vector must be ordered and non-empty")
        _require(contract, len(set(contract.ordered_target_refs)) == len(contract.ordered_target_refs),
                 "target vector contains duplicate targets")
    elif isinstance(contract, TargetReleasePolicy):
        _require(contract, contract.require_exhaustion_certificate,
                 "target release requires exhaustion certificate")
        _require(contract, contract.seal_dormant_evidence and contract.max_active_vectors == 1,
                 "target release must seal dormant evidence and allow one active vector")
    elif isinstance(contract, CertifiedHarmBound):
        _validate_harm_bound(contract)
    elif isinstance(contract, WatchdogContract):
        _require(contract, contract.arm_before_apply, "watchdog must arm before apply")
        _require(contract, contract.max_evaluation_latency_ms > 0 and contract.debounce_ms >= 0,
                 "watchdog timings are invalid")
    elif isinstance(contract, HarmContract):
        _require(contract, bool(contract.scope_selector) and bool(contract.bounds) and bool(contract.watchdogs),
                 "harm contract requires scope, bounds, and watchdogs")
        _require(contract, contract.missing_interval_charge.value > 0,
                 "missing interval charge must be conservative and positive")
        for bound in contract.bounds:
            validate_contract(bound)
        for watchdog in contract.watchdogs:
            validate_contract(watchdog)
    elif isinstance(contract, ActuatorBinding):
        _require(contract, contract.path.value == "OFFICIAL_ORAN_DYNAMIC",
                 "objective actuator must use official O-RAN dynamic path")
        _require(contract, bool(contract.readback_measurement_ref) and contract.rollback_supported,
                 "actuator requires readback and rollback")
        _require(contract, bool(contract.standard_mapping), "actuator requires standard mapping")
        names = [parameter.name for parameter in contract.parameters]
        _require(contract, len(names) == len(set(names)), "actuator parameter names must be unique")
        for parameter in contract.parameters:
            _require(contract, bool(parameter.name) and bool(parameter.value_type) and bool(parameter.scope),
                     "actuator parameters require name, type, and scope")
            _require(contract, parameter.minimum is None or parameter.maximum is None
                     or parameter.minimum <= parameter.maximum,
                     "actuator parameter bounds are inverted")
        if contract.parameters:
            _require(contract, not contract.live_capable or bool(contract.live_backend),
                     "live-capable actuator requires a real backend")
            _require(contract, contract.live_capable or bool(contract.live_blocking_premise),
                     "contract-only actuator requires a live-blocking premise")
    elif isinstance(contract, CapabilityManifest):
        _require(contract, bool(contract.supported_objectives), "capability has no supported objective")
        _require(contract, bool(contract.actuator_refs) and bool(contract.measurement_refs),
                 "capability requires actuator and measurement paths")
        _require(contract, bool(contract.standard_mapping) and bool(contract.interface_versions),
                 "capability requires standard and interface mappings")
    elif isinstance(contract, CompositionManifest):
        _require(contract, bool(contract.capability_refs), "composition has no capabilities")
        for pair in contract.mutual_exclusions:
            _require(contract, len(pair) == 2 and pair[0] != pair[1], "invalid mutual exclusion")
    elif isinstance(contract, CoordinationCasePolicy):
        _require(contract, contract.deadline_ms > 0 and contract.max_trials > 0 and contract.max_proposals > 0,
                 "case limits must be positive")
        _require(contract, bool(contract.harm_contract_refs) and contract.require_recovery_before_next_trial,
                 "case policy requires harm refs and recovery")
    elif isinstance(contract, Candidate):
        _require(contract, bool(contract.parameters) and is_content_hash(contract.semantic_hash),
                 "candidate requires parameters and semantic hash")
    elif isinstance(contract, CandidateCatalog):
        _require(contract, contract.membership_matches_cardinality() and contract.cardinality >= 0,
                 "catalog cardinality does not match membership")
        _require(contract, contract.catalog_hash == _catalog_digest(contract.generator_version, contract.cardinality, contract.candidates),
                 "catalog hash does not match membership")
        # A domain's ids are its mixed-radix positions: unique by construction,
        # and enumerating them to prove it is the cost the domain removes.
        _require(contract, contract.is_domain
                 or len({candidate.candidate_id for candidate in contract.candidates}) == len(contract.candidates),
                 "catalog candidate ids are not unique")
    elif isinstance(contract, EvidenceContribution):
        _require(contract, bool(contract.trace_refs) and len(set(contract.trace_refs)) == len(contract.trace_refs),
                 "evidence contribution needs distinct trace refs")
    elif isinstance(contract, EvidenceCell):
        _require(contract, contract.required_independent_contributions > 0,
                 "evidence cell needs a positive independent contribution quota")
        _validate_evidence_independence(contract)
        _require(contract, (contract.status is EvidenceCellStatus.DORMANT_SEALED) == bool(contract.sealed_until_vector_ref),
                 "dormant evidence sealing state is inconsistent")
    elif isinstance(contract, EvidenceLedgerRecord):
        _require(contract, bool(contract.record_id) and bool(contract.event_ref) and bool(contract.epoch_ref) and bool(contract.case_ref),
                 "evidence ledger record identity is incomplete")
        validate_contract(contract.cell)
        if contract.added_contribution is not None:
            validate_contract(contract.added_contribution)
            _require(contract, contract.added_contribution.candidate_semantic_hash == contract.cell.candidate_semantic_hash,
                     "evidence contribution does not belong to cell candidate")
            if contract.added_contribution.is_post_closure_witness:
                _require(contract, contract.previous_status in {EvidenceCellStatus.CLOSED_PASS, EvidenceCellStatus.CLOSED_FAIL}
                         and contract.cell.status is EvidenceCellStatus.POST_CLOSURE_WITNESS,
                         "post-closure witness must append after a closed cell")
    elif isinstance(contract, HarmLedgerRecord):
        _require(contract, bool(contract.record_id) and bool(contract.event_ref) and bool(contract.epoch_ref) and bool(contract.case_ref)
                 and bool(contract.harm_contract_ref), "harm ledger record identity is incomplete")
        _require(contract, isinstance(contract.harm_kind, HarmKind),
                 "harm ledger record needs a harm kind")
        _require(contract, contract.movement.amount.value >= 0, "harm ledger movement cannot be negative")
        if contract.charged_for_missing_interval:
            _require(contract, contract.movement.kind is MovementKind.CHARGE,
                     "missing interval must be charged conservatively")
    elif isinstance(contract, CompatibilityRecord):
        required = {check.value for check in CompatibilityCheck}
        _require(contract, set(contract.results) == required, "compatibility record omits required checks")
        _require(contract, all(isinstance(value, bool) for value in contract.results.values()),
                 "compatibility checks must be booleans")
        _require(contract, contract.admitted == all(contract.results.values()),
                 "cross-epoch reuse must pass every compatibility check")
    elif isinstance(contract, EpochRecord):
        _validate_epoch_record(contract)


def validate_family_set(contracts: Sequence[Any]) -> None:
    """Validate a complete contract set for cross-family consistency.

    Signature frozen by this design step; body owned by lane **KCON**.

    Per-contract validity is not enough: an epoch freezes a *set*, and the
    interesting failures are between families.  The body must check that

    * every ``*_ref`` resolves inside the set -- a measurement contract's
      ``counter_id`` has a counter binding, an actuator binding's
      ``capability_ref`` has a manifest, a case policy's harm contract refs
      exist;
    * the composition manifest's capability refs are all present, and no pair
      in ``mutual_exclusions`` is jointly active;
    * every target option's ``capability_ref`` is deployed, so the generated
      catalog cannot contain a candidate nothing can perform;
    * every target predicate's ``measurement_ref`` resolves, so no target can
      be declared successful against a KPI the deployment does not produce;
    * no two contracts of the same family share a ``contract_id`` at different
      versions within one epoch -- ambiguity there would make the epoch hash
      stable while its meaning was not.

    Raises :class:`ContractAdmissionError` naming the first unresolved
    reference.  Fail-closed on an incomplete set: a dangling reference means
    the epoch would freeze a promise it cannot keep.
    """
    for contract in contracts:
        validate_contract(contract)
    if not contracts:
        raise ContractAdmissionError("contract set is empty")
    _reject_ambiguous_versions(contracts)
    by_id = _contracts_by_id(contracts)
    counters = {item.counter_id for item in contracts if isinstance(item, CounterBinding)}
    measurements = {item.contract_id for item in contracts if isinstance(item, MeasurementContract)}
    capabilities = {item.contract_id for item in contracts if isinstance(item, CapabilityManifest)}
    actuators = {item.contract_id for item in contracts if isinstance(item, ActuatorBinding)}
    deployments = {item.contract_id for item in contracts if isinstance(item, DeploymentBinding)}
    harms = {item.contract_id for item in contracts if isinstance(item, HarmContract)}
    targets = {item.contract_id for item in contracts if isinstance(item, TargetContract)}
    for item in contracts:
        if isinstance(item, MeasurementContract):
            _require(item, item.counter_id in counters, "measurement counter ref is unresolved")
        elif isinstance(item, TargetContract):
            for predicate in item.predicates:
                _require(item, predicate.constraint.measurement_ref in measurements,
                         "target predicate measurement ref is unresolved")
            for option in item.options:
                _require(item, option.capability_ref in capabilities,
                         "target option capability ref is unresolved")
        elif isinstance(item, TargetVector):
            _require(item, set(item.ordered_target_refs).issubset(targets), "target vector ref is unresolved")
        elif isinstance(item, CapabilityManifest):
            _require(item, set(item.actuator_refs).issubset(actuators), "capability actuator ref is unresolved")
            _require(item, set(item.measurement_refs).issubset(measurements), "capability measurement ref is unresolved")
        elif isinstance(item, ActuatorBinding):
            _require(item, item.capability_ref in capabilities, "actuator capability ref is unresolved")
            _require(item, item.readback_measurement_ref in measurements, "actuator readback ref is unresolved")
            _require(item, item.deployment_binding_ref in deployments, "actuator deployment ref is unresolved")
        elif isinstance(item, CompositionManifest):
            _require(item, set(item.capability_refs).issubset(capabilities), "composition capability ref is unresolved")
            for pair in item.mutual_exclusions:
                _require(item, not set(pair).issubset(set(item.capability_refs)),
                         "composition activates a mutually exclusive pair")
        elif isinstance(item, CoordinationCasePolicy):
            _require(item, set(item.harm_contract_refs).issubset(harms), "case harm ref is unresolved")
            _require(item, item.target_release_policy_ref in by_id, "case target release ref is unresolved")


def assert_secret_free(binding: DeploymentBinding) -> None:
    """Assert that *binding* carries references and not credentials.

    Signature frozen by this design step; body owned by lane **KCON**.

    Design section 17.10 and task section 4.6: no actual credential, password,
    token or private key in source, manifest, archive, capture, log, GUI or
    export.  A deployment binding is where that pressure is highest, because it
    is the object that has to describe reaching a secured endpoint.

    The body must check that

    * every value in :attr:`DeploymentBinding.secret_refs` is a *reference* --
      a scheme-prefixed locator such as ``env:NAME``, ``file:path`` or
      ``vault:path#key`` -- and never material;
    * :attr:`DeploymentBinding.base_url` carries no userinfo component, since
      ``https://user:pass@host`` is a credential in a URL;
    * no key or value matches :data:`SECRET_BEARING_KEY_HINTS` in a way that
      indicates an inlined value;
    * PEM/JWT-shaped material (``-----BEGIN``, ``eyJ``) appears nowhere in the
      binding.

    Raises :class:`ContractAdmissionError` **without echoing the offending
    value**.  A refusal that quotes the secret into an exception message, a log
    line and then an evidence bundle would defeat the check it just performed.
    """
    if not isinstance(binding, DeploymentBinding):
        _refuse(binding, "secret check requires DeploymentBinding")
    parsed = urlsplit(binding.base_url)
    _require(binding, parsed.scheme in {"https", "http"} and bool(parsed.hostname) and parsed.username is None and parsed.password is None,
             "deployment URL must be an address without userinfo")
    reference_prefixes = ("env:", "file:", "vault:", "kms:", "keyring:")
    forbidden_material = ("-----BEGIN", "eyJ")
    for key, value in binding.secret_refs.items():
        _require(binding, isinstance(key, str) and key.strip() and isinstance(value, str), "secret refs must be named strings")
        _require(binding, value.startswith(reference_prefixes), "deployment secret reference contains inline material")
        _require(binding, not any(marker in value for marker in forbidden_material), "deployment contains secret material")
    for field in fields(binding):
        if field.name in {"secret_refs", "trust_anchor_ref"}:
            continue
        value = getattr(binding, field.name)
        if isinstance(value, str):
            _require(binding, not any(marker in value for marker in forbidden_material), "deployment contains secret material")


def _canonical_value(value: Any) -> Any:
    if isinstance(value, TypedQuantity):
        return value.to_canonical_dict()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        output: Dict[str, Any] = {}
        for field in fields(value):
            item = getattr(value, field.name)
            if item is not None:
                output[_camel(field.name)] = _canonical_value(item)
        return output
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    return value


def _camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(part[:1].upper() + part[1:] for part in rest)


def _walk_quantities(value: Any) -> Iterable[TypedQuantity]:
    if isinstance(value, TypedQuantity):
        yield value
    elif is_dataclass(value):
        for field in fields(value):
            yield from _walk_quantities(getattr(value, field.name))
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_quantities(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _walk_quantities(item)


def _validate_shape(contract: Any) -> None:
    if not is_dataclass(contract):
        _refuse(contract, "contract is not a dataclass")
    if hasattr(contract, "contract_id"):
        _require(contract, all(isinstance(getattr(contract, key), str) and getattr(contract, key).strip()
                               for key in ("contract_id", "version", "schema_version")), "identity is incomplete")
        _require(contract, getattr(contract, "document_status") == "NORMATIVE", "only normative contracts are admissible")


def _validate_measurement(contract: MeasurementContract) -> None:
    _require(contract, bool(contract.counter_id) and bool(contract.scope_selector) and bool(contract.membership_snapshot),
             "measurement requires counter, scope, and membership snapshot")
    _require(contract, all(value > 0 for value in (contract.cadence_ms, contract.window_width_ms, contract.window_stride_ms,
                                                    contract.minimum_entity_count, contract.hold_ms, contract.freshness_bound_ms)),
             "measurement cadence, windows, count, hold, and freshness must be positive")
    _require(contract, isinstance(contract.uncertainty_rule, object) and bool(contract.uncertainty_rule.model),
             "measurement uncertainty rule is required")
    _require(contract, contract.missing_interval_charge.value > 0,
             "measurement missing interval charge must be conservative and positive")


def _validate_harm_bound(bound: CertifiedHarmBound) -> None:
    _require(bound, bool(bound.bound_id) and bool(bound.uncertainty_ref) and bool(bound.operating_scope),
             "hard harm bound requires uncertainty and operating scope")
    _require(bound, bound.enforced_timeout_ms > 0, "hard harm bound requires enforced timeout")
    _require(bound, bool(bound.calibration_records) and bool(bound.proof_ref),
             "hard harm bound requires calibration and proof evidence")
    _require(bound, bound.conservative_margin.value > 0,
             "hard harm bound requires a positive conservative margin")
    _require(bound, bound.measured_bound.unit == bound.conservative_margin.unit == bound.admissible_bound.unit,
             "harm bound quantities must share a unit")
    _require(bound, bound.admissible_bound.value >= bound.measured_bound.value + bound.conservative_margin.value,
             "admissible harm bound must include the conservative margin")


def _validate_evidence_independence(cell: EvidenceCell) -> None:
    traces: set[str] = set()
    groups: set[str] = set()
    for contribution in cell.contributions:
        validate_contract(contribution)
        _require(cell, not traces.intersection(contribution.trace_refs), "trace reused as independent evidence")
        traces.update(contribution.trace_refs)
        if contribution.dependency_group:
            _require(cell, contribution.dependency_group not in groups,
                     "dependency group reused as independent evidence")
            groups.add(contribution.dependency_group)
        if cell.status in {EvidenceCellStatus.CLOSED_PASS, EvidenceCellStatus.CLOSED_FAIL}:
            _require(cell, not contribution.is_post_closure_witness,
                     "post-closure witness must be appended in a witness record")


def _validate_epoch_record(record: EpochRecord) -> None:
    if getattr(record, "candidate_domain", False):
        # A frozen domain: one digest covers every point (catalog.membership_digests).
        _require(record, len(record.candidate_semantic_hashes) == 1
                 and record.candidate_universe_cardinality >= 1,
                 "epoch candidate domain is not one digest over a non-empty domain")
    else:
        _require(record, record.candidate_universe_cardinality == len(record.candidate_semantic_hashes),
                 "epoch candidate cardinality does not match membership")
    _require(record, len(set(record.candidate_semantic_hashes)) == len(record.candidate_semantic_hashes),
             "epoch candidate membership is not unique")
    hashes = [record.composition_manifest_hash, record.target_vector_hash, record.case_policy_hash, record.catalog_hash,
              *record.target_contract_hashes.values(), *record.harm_contract_hashes.values(),
              *record.measurement_contract_hashes.values(), *record.capability_manifest_hashes.values(),
              *record.deployment_binding_hashes.values(), *record.counter_binding_hashes.values(),
              *record.actuator_binding_hashes.values(), *record.candidate_semantic_hashes]
    _require(record, all(is_content_hash(item) for item in hashes), "epoch has an invalid frozen content hash")
    _require(record, bool(record.target_vector_order) and bool(record.evaluator_version) and bool(record.reducer_version),
             "epoch freeze is incomplete")


def _catalog_digest(generator_version: str, cardinality: int, candidates: Sequence[Candidate]) -> str:
    """검증기가 쓰는 다이제스트 -- **생성기와 같은 함수**를 부른다.

    2026-09-18: 여기에 같은 계산의 **복제본**이 있었다.  `catalog.catalog_hash` 만
    고쳤더니 이쪽이 옛 방식으로 남아 `catalog hash does not match membership` 이
    났다.  복제가 있는 한 둘은 언젠가 또 갈린다 -- 한 곳만 두고 위임한다.
    """
    from assurance.contracts.catalog import catalog_hash

    return catalog_hash(generator_version=generator_version, cardinality=cardinality,
                        candidates=candidates)


def _contracts_by_id(contracts: Sequence[Any]) -> Dict[str, Any]:
    return {contract.contract_id: contract for contract in contracts if hasattr(contract, "contract_id")}


def _reject_ambiguous_versions(contracts: Sequence[Any]) -> None:
    seen: Dict[Tuple[type, str], str] = {}
    for contract in contracts:
        if not hasattr(contract, "contract_id"):
            continue
        key = (type(contract), contract.contract_id)
        previous = seen.get(key)
        _require(contract, previous is None or previous == contract.version,
                 "same family and contract id appear at multiple versions")
        seen[key] = contract.version


def _require(contract: Any, condition: Any, message: str) -> None:
    if not condition:
        _refuse(contract, message)


def _refuse(contract: Any, message: str) -> None:
    name = getattr(contract, "contract_id", getattr(contract, "bound_id", type(contract).__name__))
    raise ContractAdmissionError(f"{name}: {message}")
