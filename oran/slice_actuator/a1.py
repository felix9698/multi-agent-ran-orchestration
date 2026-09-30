"""A1-P v2-shaped in-memory producer for ``AIC_SliceSLATarget_1.0.0``.

The service surface is deliberately independent of HTTP so the same lifecycle
can be embedded into the existing xApp backend or exercised hermetically.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from oran.contract.jcs import jcs_sha256
from .model import SliceIdentity, quota_from_policy


POLICY_TYPE_ID = "AIC_SliceSLATarget_1.0.0"
_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_DIR = _ROOT / "contracts" / "oran-aic" / "gate8-slice-actuator-hf"


class A1Error(ValueError):
    pass


class A1ValidationError(A1Error):
    pass


class A1Conflict(A1Error):
    pass


class A1NotFound(A1Error):
    pass


@dataclass(frozen=True)
class PutResult:
    http_status: int
    policy_digest: str


@dataclass(frozen=True)
class HttpResponse:
    """Transport-neutral result for the A1-P v2 route adapter."""

    status: int
    body: Any = None


def _load(name: str) -> dict[str, Any]:
    return json.loads((_SCHEMA_DIR / name).read_text(encoding="utf-8"))


class A1PolicyProducer:
    """Discover, validate, store, status, and delete the new policy type."""

    def __init__(self, *, additional_types: tuple[str, ...] = ()) -> None:
        self._policy_schema = _load(f"{POLICY_TYPE_ID}.policy.schema.json")
        self._status_schema = _load(f"{POLICY_TYPE_ID}.status.schema.json")
        Draft202012Validator.check_schema(self._policy_schema)
        Draft202012Validator.check_schema(self._status_schema)
        self._policy_validator = Draft202012Validator(self._policy_schema, format_checker=FormatChecker())
        self._status_validator = Draft202012Validator(self._status_schema)
        # Types advertised *beside* quota by the same in-repo producer service
        # (design section 4.6 Option B).  This producer owns only the quota
        # lifecycle; the additional types' policy/status lifecycles live in
        # ``oran.campaign5.producer.Campaign5PolicyProducer``.  Advertising them
        # here lets one discovery surface report all five without this class
        # gaining slice-foreign policy handling.
        seen = {POLICY_TYPE_ID}
        extra: list[str] = []
        for name in additional_types:
            if name in seen:
                raise A1Conflict(f"duplicate advertised policy type {name}")
            seen.add(name)
            extra.append(name)
        self._additional_types = tuple(extra)
        self._policies: dict[str, dict[str, Any]] = {}
        self._statuses: dict[str, dict[str, Any]] = {}
        self._scope_owner: dict[str, str] = {}
        self._delete_handler: Callable[[str], None] | None = None
        self._delete_capability = object()

    def bind_delete_handler(self, handler: Callable[[str], None]) -> object:
        """Bind the rollback-owning worker and return its deletion capability."""
        if self._delete_handler is not None and self._delete_handler != handler:
            raise A1Conflict("A1 DELETE already has a rollback worker")
        self._delete_handler = handler
        return self._delete_capability

    def _check_type(self, policy_type_id: str) -> None:
        if policy_type_id != POLICY_TYPE_ID:
            raise A1NotFound(f"unknown policy type {policy_type_id}")

    def get_policytypes(self) -> list[str]:
        """Return the A1-P v2 policy type identifier collection.

        Quota's own type first, then any Campaign 5 types advertised beside it.
        The default (no ``additional_types``) keeps the historical single-type
        collection unchanged.
        """
        return [POLICY_TYPE_ID, *self._additional_types]

    def get_policytype(self, policy_type_id: str) -> dict[str, Any]:
        self._check_type(policy_type_id)
        return {
            "policySchema": copy.deepcopy(self._policy_schema),
            "statusSchema": copy.deepcopy(self._status_schema),
        }

    def schema_digests(self) -> dict[str, str]:
        """Expose reproducibility evidence without extending the A1 resource."""
        return {
            "policySchemaJcsSha256": jcs_sha256(self._policy_schema),
            "statusSchemaJcsSha256": jcs_sha256(self._status_schema),
        }

    def _validate_policy(self, policy: Mapping[str, Any]) -> tuple[dict[str, Any], SliceIdentity]:
        value = copy.deepcopy(dict(policy))
        errors = sorted(self._policy_validator.iter_errors(value), key=lambda error: list(error.absolute_path))
        if errors:
            first = errors[0]
            path = ".".join(str(item) for item in first.absolute_path) or "$"
            raise A1ValidationError(f"{path}: {first.message}")
        try:
            quota = quota_from_policy(value)
        except ValueError as exc:
            raise A1ValidationError(str(exc)) from exc
        not_before = datetime.fromisoformat(value["validity"]["notBefore"].replace("Z", "+00:00"))
        not_after = datetime.fromisoformat(value["validity"]["notAfter"].replace("Z", "+00:00"))
        if not_before >= not_after:
            raise A1ValidationError("validity.notBefore must precede validity.notAfter")
        return value, quota.identity

    def put_policy(self, policy_type_id: str, policy_id: str,
                   policy: Mapping[str, Any]) -> PutResult:
        self._check_type(policy_type_id)
        if not policy_id or len(policy_id) > 255:
            raise A1ValidationError("policy id must contain 1..255 characters")
        value, identity = self._validate_policy(policy)
        digest = jcs_sha256(value)
        current = self._policies.get(policy_id)
        if current is not None:
            if jcs_sha256(current) == digest:
                return PutResult(200, digest)
            current_identity = quota_from_policy(current).identity
            if current_identity != identity:
                raise A1Conflict("policy id cannot move to a different slice scope")
            current_trace = current["trace"]
            new_trace = value["trace"]
            if (
                new_trace["revision"] <= current_trace["revision"]
                or new_trace["fencingToken"] <= current_trace["fencingToken"]
            ):
                raise A1Conflict(
                    "policy id update requires a newer revision and fencingToken"
                )
            self._policies[policy_id] = value
            self._statuses[policy_id] = self._pending_status(policy_id, digest)
            return PutResult(200, digest)
        owner = self._scope_owner.get(identity.key())
        if owner is not None and owner != policy_id:
            raise A1Conflict(f"slice scope already owned by policy {owner}")
        self._policies[policy_id] = value
        self._scope_owner[identity.key()] = policy_id
        status = self._pending_status(policy_id, digest)
        self._statuses[policy_id] = status
        return PutResult(201, digest)

    def _pending_status(self, policy_id: str, digest: str) -> dict[str, Any]:
        status = {
            "policyTypeId": POLICY_TYPE_ID,
            "policyId": policy_id,
            "policyState": "ACTIVE",
            "enforceStatus": "PENDING",
            "policyDigest": digest,
            "deliveryAcknowledged": False,
            "readbackVerified": False,
            "measurementCorrelated": False,
            "coreEvidenceCorrelated": False,
            "reason": "awaiting typed near-RT worker effect evidence",
            "previousQuota": None,
        }
        self._status_validator.validate(status)
        return status

    def get_policy(self, policy_type_id: str, policy_id: str) -> dict[str, Any]:
        self._check_type(policy_type_id)
        try:
            return copy.deepcopy(self._policies[policy_id])
        except KeyError as exc:
            raise A1NotFound(f"unknown policy {policy_id}") from exc

    def list_policies(self, policy_type_id: str) -> list[str]:
        self._check_type(policy_type_id)
        return sorted(self._policies)

    def get_status(self, policy_type_id: str, policy_id: str) -> dict[str, Any]:
        self._check_type(policy_type_id)
        try:
            return copy.deepcopy(self._statuses[policy_id])
        except KeyError as exc:
            raise A1NotFound(f"unknown policy {policy_id}") from exc

    def record_status(self, policy_id: str, *, delivery_acknowledged: bool,
                      readback_verified: bool, measurement_correlated: bool,
                      core_evidence_correlated: bool,
                      previous_quota: Mapping[str, int] | None, reason: str) -> None:
        if policy_id not in self._statuses:
            raise A1NotFound(f"unknown policy {policy_id}")
        status = copy.deepcopy(self._statuses[policy_id])
        status.update({
            "deliveryAcknowledged": delivery_acknowledged,
            "readbackVerified": readback_verified,
            "measurementCorrelated": measurement_correlated,
            "coreEvidenceCorrelated": core_evidence_correlated,
            "previousQuota": copy.deepcopy(previous_quota),
            "reason": reason,
            "enforceStatus": (
                "ENFORCED"
                if delivery_acknowledged and readback_verified
                and measurement_correlated and core_evidence_correlated
                else "NOT_ENFORCED"
            ),
        })
        self._status_validator.validate(status)
        self._statuses[policy_id] = status

    def record_rolled_back(self, policy_id: str, restored_quota: Mapping[str, int]) -> None:
        if policy_id not in self._statuses:
            raise A1NotFound(f"unknown policy {policy_id}")
        status = copy.deepcopy(self._statuses[policy_id])
        status.update({
            "enforceStatus": "ROLLED_BACK",
            "deliveryAcknowledged": True,
            "readbackVerified": True,
            "measurementCorrelated": False,
            "coreEvidenceCorrelated": False,
            "previousQuota": copy.deepcopy(dict(restored_quota)),
            "reason": "exact previous slice quota restored and read back",
        })
        self._status_validator.validate(status)
        self._statuses[policy_id] = status

    def _delete_after_rollback(self, policy_type_id: str, policy_id: str,
                               capability: object) -> int:
        """Remove storage only when called by the bound rollback worker."""
        self._check_type(policy_type_id)
        if capability is not self._delete_capability or self._delete_handler is None:
            raise A1Conflict("policy deletion requires the bound rollback worker")
        try:
            policy = self._policies.pop(policy_id)
            self._statuses.pop(policy_id)
        except KeyError as exc:
            raise A1NotFound(f"unknown policy {policy_id}") from exc
        self._scope_owner.pop(quota_from_policy(policy).identity.key(), None)
        return 204

    def handle(self, method: str, path: str,
               body: Mapping[str, Any] | None = None) -> HttpResponse:
        """Serve the required A1-P v2 resource paths without an HTTP dependency."""
        verb = method.upper()
        parts = [part for part in path.split("/") if part]
        try:
            if parts == ["A1-P", "v2", "policytypes"] and verb == "GET":
                return HttpResponse(200, self.get_policytypes())
            if len(parts) == 4 and parts[:3] == ["A1-P", "v2", "policytypes"] and verb == "GET":
                return HttpResponse(200, self.get_policytype(parts[3]))
            if (
                len(parts) == 5
                and parts[:3] == ["A1-P", "v2", "policytypes"]
                and parts[4] == "policies"
                and verb == "GET"
            ):
                return HttpResponse(200, self.list_policies(parts[3]))
            if len(parts) >= 6 and parts[:3] == ["A1-P", "v2", "policytypes"] and parts[4] == "policies":
                policy_type_id, policy_id = parts[3], parts[5]
                if len(parts) == 7 and parts[6] == "status" and verb == "GET":
                    return HttpResponse(200, self.get_status(policy_type_id, policy_id))
                if len(parts) != 6:
                    return HttpResponse(404, {"error": "unknown A1-P resource"})
                if verb == "PUT":
                    if body is None:
                        raise A1ValidationError("PUT requires a JSON policy body")
                    result = self.put_policy(policy_type_id, policy_id, body)
                    return HttpResponse(result.http_status, {"policyDigest": result.policy_digest})
                if verb == "GET":
                    return HttpResponse(200, self.get_policy(policy_type_id, policy_id))
                if verb == "DELETE":
                    self._check_type(policy_type_id)
                    if self._delete_handler is None:
                        raise A1Conflict("DELETE requires a bound rollback worker")
                    self._delete_handler(policy_id)
                    return HttpResponse(204)
            return HttpResponse(404, {"error": "unknown A1-P resource"})
        except A1Conflict as exc:
            return HttpResponse(409, {"error": str(exc)})
        except A1ValidationError as exc:
            return HttpResponse(400, {"error": str(exc)})
        except A1NotFound as exc:
            return HttpResponse(404, {"error": str(exc)})
