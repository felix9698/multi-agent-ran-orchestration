"""Translate typed coordinator intents into the frozen A1 PolicyObject."""

from __future__ import annotations

import copy
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Sequence

from decision.intent_model import IntentPriority, IntentType

from .contract_support import (
    ContractValidationError, jcs_sha256, load_schema, validate,
)


POLICY_TYPE_ID = "AIC_UECellSteering_1.0.0"
SUPPORTED_BALANCE_INTENTS = frozenset({
    IntentType.THROUGHPUT_GOAL,
    IntentType.THROUGHPUT_FAIRNESS,
    IntentType.LATENCY_GOAL,
})
PRIORITY = {
    IntentPriority.CRITICAL: 100,
    IntentPriority.HIGH: 75,
    IntentPriority.MEDIUM: 50,
    IntentPriority.LOW: 25,
}
_ASCII_TOKEN = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


class AdmissionRejected(ContractValidationError):
    """The intent cannot be represented by the discovered deployment."""

    code = "AIC_CAPABILITY_MISMATCH"


@dataclass(frozen=True)
class PolicyTranslationContext:
    """All behavior-bearing policy values supplied by deployment/episode state.

    There are intentionally no convenience defaults: the contract forbids
    silently introducing behavior-critical defaults.
    """

    ue_id: Dict[str, Any]
    allowed_cells: Sequence[Dict[str, Any]]
    forbidden_cells: Sequence[Dict[str, Any]]
    objective_kind: str
    improvement_threshold_prb: float | None
    min_seconds_between_actuations: int
    required_kpi_freshness_ms: int
    action_deadline_ms: int
    not_before: datetime | str
    expires_at: datetime | str
    rollback_on: Sequence[str]
    rollback_timeout_ms: int
    intent_revision: int
    policy_revision: int
    correlation_id: str
    producer_id: str
    #: Contract identity of the intent, assigned by the intent producer in the
    #: same way ``correlation_id`` is.  The contract requires a UUID here, and
    #: the preserved coordinator mints its own short internal identifier for a
    #: natural-language episode, so a producer that has an O-RAN intent identity
    #: states it rather than letting an internal id leak onto the wire.  Absent,
    #: the intent object's own id is used and must itself be a UUID.
    intent_id: str | None = None


def _utc_z(value: datetime | str) -> str:
    if isinstance(value, str):
        if not value.endswith("Z"):
            raise AdmissionRejected("wire timestamp must use UTC Z")
        try:
            datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as exc:
            raise AdmissionRejected(f"invalid UTC timestamp: {value}") from exc
        return value
    if value.tzinfo is None or value.utcoffset() is None:
        raise AdmissionRejected("naive datetime is not a wire timestamp")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _intent_attr(intent: Any, name: str) -> Any:
    if isinstance(intent, Mapping):
        return intent.get(name)
    return getattr(intent, name, None)


def _intent_type(intent: Any) -> IntentType:
    raw = _intent_attr(intent, "type")
    if isinstance(raw, IntentType):
        return raw
    try:
        return IntentType(raw)
    except (TypeError, ValueError) as exc:
        raise AdmissionRejected(f"unsupported intent type: {raw!r}") from exc


def _intent_priority(intent: Any) -> IntentPriority:
    raw = _intent_attr(intent, "priority")
    if isinstance(raw, IntentPriority):
        return raw
    if isinstance(raw, str):
        try:
            return IntentPriority[raw.upper()]
        except KeyError:
            pass
    try:
        return IntentPriority(raw)
    except (TypeError, ValueError) as exc:
        raise AdmissionRejected(f"unsupported intent priority: {raw!r}") from exc


def _discovered_ids(discovery: Any) -> set[str]:
    if isinstance(discovery, Mapping):
        candidates = (discovery.get("policyTypeIds") or
                      discovery.get("policyTypes") or discovery.get("items") or [])
    else:
        candidates = discovery or []
    result = set()
    for item in candidates:
        if isinstance(item, str):
            result.add(item)
        elif isinstance(item, Mapping):
            value = item.get("policyTypeId") or item.get("id")
            if isinstance(value, str):
                result.add(value)
    return result


def _canonical_set(items: Iterable[Dict[str, Any]]) -> set[str]:
    from .contract_support import canonicalize
    return {canonicalize(item).decode("utf-8") for item in items}


def _admit(discovery: Any, capability: Mapping[str, Any], context: PolicyTranslationContext,
           objective: str) -> None:
    validate(capability, "aic:ran-capability:1.0.0")
    if POLICY_TYPE_ID not in _discovered_ids(discovery):
        raise AdmissionRejected("R1 policy-type discovery lacks frozen policy type")
    if POLICY_TYPE_ID not in capability.get("policyTypes", []):
        raise AdmissionRejected("capability manifest lacks frozen policy type")
    if objective not in capability.get("objectives", []):
        raise AdmissionRejected(f"objective {objective} is not advertised")
    if "guAmfUeNgapId" not in capability.get("ueIdFormats", []):
        raise AdmissionRejected("guAmfUeNgapId is not advertised")
    if set(context.ue_id) != {"guAmfUeNgapId"}:
        raise AdmissionRejected("scope requires exactly guAmfUeNgapId")
    type_object = discovery.get("policyTypeObject") \
        if isinstance(discovery, Mapping) else None
    if not isinstance(type_object, Mapping):
        raise AdmissionRejected("R1 policy-type detail was not discovered")
    if type_object.get("policyTypeId") != POLICY_TYPE_ID:
        raise AdmissionRejected("R1 policy-type detail identifier mismatch")
    policy_schema = type_object.get("policySchema")
    status_schema = type_object.get("statusSchema")
    if not isinstance(policy_schema, Mapping) or not isinstance(status_schema, Mapping):
        raise AdmissionRejected("R1 policy-type detail omitted policy/status schema")
    digests = capability.get("schemaDigests", {})
    if (jcs_sha256(policy_schema) != digests.get("policy") or
            jcs_sha256(status_schema) != digests.get("status") or
            jcs_sha256(policy_schema) != local_policy_schema_digest() or
            jcs_sha256(status_schema) != local_status_schema_digest()):
        raise AdmissionRejected("R1/capability/local policy schema digest mismatch")
    advertised_cells = _canonical_set(
        cell["cellId"] for cell in capability.get("topology", {}).get("cells", []))
    requested_cells = _canonical_set(
        list(context.allowed_cells) + list(context.forbidden_cells))
    if not requested_cells.issubset(advertised_cells):
        raise AdmissionRejected("action envelope contains a cell outside topology")


def translate_intent(intent: Any, *, policy_type_discovery: Any,
                     capability_manifest: Mapping[str, Any],
                     context: PolicyTranslationContext) -> Dict[str, Any]:
    """Return a schema-validated ``AIC_UECellSteering_1.0.0`` PolicyObject.

    Normal agentic throughput/fairness/latency intents map only to
    ``BALANCE_PRB_LOAD``.  ``PIN_TO_CELL`` is accepted only when explicitly
    requested by the caller for a trial/override/recovery context.  Every other
    intent fails before R1 submission; no legacy action conversion exists.
    """
    kind = _intent_type(intent)
    objective = context.objective_kind
    if objective == "BALANCE_PRB_LOAD":
        if kind not in SUPPORTED_BALANCE_INTENTS:
            raise AdmissionRejected(
                f"{kind.value} cannot use BALANCE_PRB_LOAD")
        if context.improvement_threshold_prb is None:
            raise AdmissionRejected(
                "BALANCE_PRB_LOAD requires improvementThresholdPrb")
    elif objective == "PIN_TO_CELL":
        if len(context.allowed_cells) != 1:
            raise AdmissionRejected("PIN_TO_CELL requires exactly one allowed cell")
        if context.improvement_threshold_prb is not None:
            raise AdmissionRejected(
                "PIN_TO_CELL forbids improvementThresholdPrb")
    else:
        raise AdmissionRejected(f"unsupported objective: {objective!r}")

    _admit(policy_type_discovery, capability_manifest, context, objective)
    if not context.allowed_cells:
        raise AdmissionRejected("allowedCells cannot be empty")
    if _canonical_set(context.allowed_cells) & _canonical_set(context.forbidden_cells):
        raise AdmissionRejected("allowedCells and forbiddenCells overlap")

    intent_id = str(context.intent_id or _intent_attr(intent, "id") or "")
    try:
        uuid.UUID(intent_id)
        uuid.UUID(context.correlation_id)
    except (ValueError, AttributeError) as exc:
        raise AdmissionRejected("intentId and correlationId must be UUIDs") from exc
    if context.intent_revision < 1 or context.policy_revision < 1:
        raise AdmissionRejected("intentRevision and policyRevision start at 1")
    if not _ASCII_TOKEN.fullmatch(context.producer_id):
        raise AdmissionRejected("producerId violates the frozen token grammar")
    not_before = _utc_z(context.not_before)
    expires_at = _utc_z(context.expires_at)
    if datetime.fromisoformat(expires_at[:-1] + "+00:00") <= datetime.fromisoformat(
            not_before[:-1] + "+00:00"):
        raise AdmissionRejected("expiresAt must be after notBefore")

    steering: Dict[str, Any] = {
        "kind": objective,
        "actionEnvelope": {
            "allowedCells": copy.deepcopy(list(context.allowed_cells)),
            "forbiddenCells": copy.deepcopy(list(context.forbidden_cells)),
        },
    }
    if objective == "BALANCE_PRB_LOAD":
        steering["improvementThresholdPrb"] = context.improvement_threshold_prb

    obj = {
        "scope": {"ueId": copy.deepcopy(context.ue_id)},
        "steeringObjective": steering,
        "constraints": {
            "maxActuationsPerEpisode": 1,
            "minSecondsBetweenActuations": context.min_seconds_between_actuations,
            "requiredKpiFreshnessMs": context.required_kpi_freshness_ms,
            "actionDeadlineMs": context.action_deadline_ms,
        },
        "validity": {"notBefore": not_before, "expiresAt": expires_at},
        "priority": PRIORITY[_intent_priority(intent)],
        "rollbackPolicy": {
            "on": list(context.rollback_on),
            "timeoutMs": context.rollback_timeout_ms,
        },
        "trace": {
            "intentId": intent_id,
            "intentRevision": context.intent_revision,
            "policyRevision": context.policy_revision,
            "idempotencyKey": f"{intent_id}:{context.policy_revision}",
            "correlationId": context.correlation_id,
            "producerId": context.producer_id,
        },
    }
    validate(obj, "AIC_UECellSteering_1.0.0.policy")
    return obj


def local_policy_schema_digest() -> str:
    """Digest used to compare discovery material with the frozen local schema."""
    return jcs_sha256(load_schema("AIC_UECellSteering_1.0.0.policy"))


def local_status_schema_digest() -> str:
    return jcs_sha256(load_schema("AIC_UECellSteering_1.0.0.status"))
