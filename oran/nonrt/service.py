"""Pinned R1 service surface and durable Non-RT reconciliation logic."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from oran.contract.validator import ContractValidator

from ._contract import ContractValidationError, canonicalize, jcs_sha256, problem, validate
from .a1_client import A1PClient, A1ProtocolError
from .capability import CapabilityArtifacts
from .config import SecurityProfile, require_https_or_loopback
from .store import DurableStore


POLICY_TYPE_ID = "AIC_UECellSteering_1.0.0"
POLICY_SCHEMA_ID = "urn:oran-aic:schema:AIC_UECellSteering:policy:1.0.0"
STATUS_SCHEMA_ID = "urn:oran-aic:schema:AIC_UECellSteering:status:1.0.0"
R1_VERSION = "1.0.0"
SERVICE_API_VERSION = "1.2.0"
DME_REGISTRATION_VERSION = "2.0.0-alpha.2"
DME_DISCOVERY_VERSION = "2.0.0"
DME_ACCESS_VERSION = "2.0.0-alpha.2"


@dataclass(frozen=True)
class Response:
    status: int
    body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)


class ResponseDropped(RuntimeError):
    """Development harness signal: processing committed but HTTP response was lost."""


class SimulatedCrash(RuntimeError):
    """Development harness signal emitted at a durable crash boundary."""


class ServiceFailure(RuntimeError):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


CallbackSender = Callable[[str, dict[str, Any]], int]

#: A1-P statuses that mean the producer decided before anything was written, and
#: the R1 problem code each is relayed as.  5xx and transport faults stay 503.
A1_REFUSAL_CODES = {
    400: "AIC_SCHEMA_INVALID",
    404: "AIC_RESOURCE_NOT_FOUND",
    409: "AIC_POLICY_CONFLICT",
}


class NonRtRicService:
    """A framework core independent of any web framework.

    ``handle`` is the HTTP adapter boundary.  The caller provides the
    authenticated peer headers; body identity is never used as authentication.
    """

    def __init__(
        self,
        *,
        database_path: str | Path,
        a1_client: A1PClient,
        capability_manifest: Mapping[str, Any],
        a1_ready: bool,
        bundle_dir: str | Path,
        security: SecurityProfile,
        r1_api_root: str,
        a1_notification_destination: str,
        bootstrap_info: Mapping[str, Any],
        callback_sender: CallbackSender | None = None,
        capability_registration: Mapping[str, Any] | None = None,
        restart_hook: Callable[[str], None] | None = None,
        capability_artifacts: CapabilityArtifacts | None = None,
        a1_pre_put_reconciliation_probe: bool = True,
    ):
        security.validate()
        if not security.insecure_dev_mode and capability_artifacts is None:
            raise ValueError("production profile requires digest-pinned CapabilityArtifacts")
        if not security.insecure_dev_mode and capability_registration is None:
            raise ValueError("production profile requires a capability DME registration")
        if capability_artifacts is not None:
            capability_manifest = capability_artifacts.capability_manifest
            a1_ready = capability_artifacts.a1_ready
        self.database_path = str(database_path)
        self.store = DurableStore(database_path)
        # Status callbacks and authoritative queries arrive on independent
        # HTTP workers.  Serialize the read/compare/apply/audit transition so
        # a lower sequence cannot overwrite a concurrently accepted higher one.
        self._status_lock = threading.RLock()
        self.a1 = a1_client
        self.capability = dict(capability_manifest)
        self.a1_ready = bool(a1_ready)
        self.bundle_dir = Path(bundle_dir)
        self.security = security
        self.r1_api_root = r1_api_root.rstrip("/")
        self.r1_api_path = urlsplit(self.r1_api_root).path.rstrip("/")
        self.a1_notification_destination = a1_notification_destination
        self.a1_notification_path = urlsplit(a1_notification_destination).path.rstrip("/") or "/"
        self.bootstrap_info = dict(bootstrap_info)
        self.callback_sender = callback_sender
        self.restart_hook = restart_hook
        # (P0) Pre-PUT A1 reconciliation probe.  The probe is a GET of
        # #/endpointTemplates/a1Policies (plus one GET per listed policy) issued
        # *before* the create PUT so a crash between the durable ledger write
        # and the PUT cannot actuate twice (catalog requirement SEC18-36,
        # SC-048).  It is deployment-selectable because a profile whose
        # scenarios declare `faults: []` and an exact `expected.httpSequence`
        # never needs it and must not put undeclared traffic on the peer's
        # wire.  Default True: every existing caller keeps today's behaviour.
        self.a1_pre_put_reconciliation_probe = bool(a1_pre_put_reconciliation_probe)
        if self.capability.get("dmeTypeId") != "aic:ran-capability:1.0.0":
            raise ValueError("capability manifest is not aic:ran-capability:1.0.0")
        if capability_registration is not None:
            self._persist_capability_registration(dict(capability_registration))

    def handle(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: Any = None,
    ) -> Response:
        headers = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
        parsed = urlsplit(target)
        path = parsed.path.rstrip("/") or "/"
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        # ``r1_api_root`` is deployment-owned and may contain a path prefix
        # (the local profiles deliberately use ``/r1``).  Routes below are
        # expressed relative to that pinned root; accepting the prefix here
        # keeps every contract endpoint on its configured URI.
        if self.r1_api_path and path.startswith(self.r1_api_path + "/"):
            path = path[len(self.r1_api_path):]
        instance = f"urn:uuid:{uuid.uuid4()}"
        try:
            response = self._dispatch(method.upper(), path, query, headers, body)
        except (ResponseDropped, SimulatedCrash):
            raise
        except PermissionError as exc:
            return self._error(401, "AIC_INTERNAL_ERROR", str(exc), instance)
        except ContractValidationError as exc:
            return self._error(400, "AIC_SCHEMA_INVALID", str(exc), instance)
        except ServiceFailure as exc:
            return self._error(exc.status, exc.code, exc.detail, instance)
        except (A1ProtocolError, OSError) as exc:
            return self._error(503, "AIC_INTERNAL_ERROR", str(exc), instance)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._error(400, "AIC_SCHEMA_INVALID", str(exc), instance)
        except Exception as exc:  # fail closed without leaking internal details
            return self._error(500, "AIC_INTERNAL_ERROR", type(exc).__name__, instance)
        return response

    def _dispatch(
        self,
        method: str,
        path: str,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        body: Any,
    ) -> Response:
        if path.startswith("/harness/"):
            return self._harness(method, path, body)
        if method == "POST" and path == self.a1_notification_path:
            if not isinstance(body, dict):
                raise ContractValidationError("A1 PolicyStatusObject must be an object")
            policy_id = body.get("aicStatus", {}).get("policyId")
            if not policy_id:
                raise ContractValidationError("A1 status policyId is required")
            return self.receive_a1_status(policy_id, body)
        if method == "GET" and path == "/bootstrap/v1/bootstrap-info":
            return Response(200, self.bootstrap_info, {"Version": R1_VERSION})
        if path.startswith("/published-apis/v1/") and "/service-apis" in path:
            self._require_version(headers, SERVICE_API_VERSION)
            return self._service_registration(method, path, body)
        if method == "GET" and path == "/service-apis/v1/allServiceAPIs":
            self._require_version(headers, SERVICE_API_VERSION)
            return self._service_discovery(query)
        if path.startswith("/a1-policy-management/v1"):
            return self._policy_route(method, path, query, headers, body)
        if path.startswith("/data-registration/v2/production-capabilities"):
            self._require_version(headers, DME_REGISTRATION_VERSION)
            return self._dme_registration_route(method, path, body)
        if path.startswith("/data-discovery/v2/dme-types"):
            self._require_version(headers, DME_DISCOVERY_VERSION)
            return self._dme_discover(method, path)
        if path.startswith("/data-access/v2/data-jobs"):
            self._require_version(headers, DME_ACCESS_VERSION)
            return self._data_job_route(method, path, body)
        raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "resource is not supported")

    # R1 service registration and discovery
    def _service_registration(self, method: str, path: str, body: Any) -> Response:
        root, separator, service_api_id = path.partition("/service-apis/")
        if not separator:
            if method != "POST":
                raise ServiceFailure(405, "AIC_RESOURCE_NOT_FOUND", "operation is not supported")
            return self._create_service_registration(root, body)
        rapp_id = unquote(root.split("/")[3])
        return self._service_registration_item(method, rapp_id, unquote(service_api_id), body)

    def _create_service_registration(self, path: str, body: Any) -> Response:
        if not isinstance(body, dict):
            raise ContractValidationError("ServiceAPIDescription must be an object")
        if "apiId" in body:
            raise ContractValidationError("client-assigned apiId is forbidden")
        required = {"apiName", "apiVersion", "aefProfiles", "communicationType", "vendorSpecific-o-ran.org"}
        if not required.issubset(body):
            raise ContractValidationError("incomplete ServiceAPIDescription")
        if body["apiVersion"] != "v1" or body["communicationType"] != "REQUEST_RESPONSE":
            raise ContractValidationError("unsupported service API version or communication type")
        versions = body["vendorSpecific-o-ran.org"].get("fullApiVersions")
        if versions != [R1_VERSION]:
            raise ContractValidationError("fullApiVersions must be exactly [\"1.0.0\"]")
        rapp_id = unquote(path.split("/")[3])
        api_id = str(uuid.uuid4())
        response_body = dict(body)
        response_body["apiId"] = api_id
        self.store.execute(
            "INSERT INTO services(api_id,rapp_id,body_json) VALUES(?,?,?)",
            (api_id, rapp_id, self.store.encode(response_body)),
        )
        location = f"{self.r1_api_root}/published-apis/v1/{rapp_id}/service-apis/{api_id}"
        return Response(201, response_body, {"Location": location, "Version": SERVICE_API_VERSION})

    def _service_registration_item(
        self, method: str, rapp_id: str, api_id: str, body: Any
    ) -> Response:
        row = self.store.row("SELECT body_json FROM services WHERE api_id=? AND rapp_id=?", (api_id, rapp_id))
        if row is None:
            raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "service API does not exist")
        if method == "GET":
            return Response(200, self.store.decode(row["body_json"]), {"Version": SERVICE_API_VERSION})
        if method == "PUT":
            if not isinstance(body, dict) or body.get("apiId") != api_id:
                raise ContractValidationError("ServiceAPIDescription apiId must match the resource")
            self.store.execute("UPDATE services SET body_json=? WHERE api_id=?", (self.store.encode(body), api_id))
            return Response(200, body, {"Version": SERVICE_API_VERSION})
        if method == "DELETE":
            self.store.execute("DELETE FROM services WHERE api_id=?", (api_id,))
            return Response(204, headers={"Version": SERVICE_API_VERSION})
        raise ServiceFailure(405, "AIC_RESOURCE_NOT_FOUND", "operation is not supported")

    def _service_discovery(self, query: Mapping[str, str]) -> Response:
        if not query.get("api-invoker-id"):
            raise ContractValidationError("api-invoker-id is required")
        rows = self.store.rows("SELECT body_json FROM services ORDER BY api_id")
        services = [self.store.decode(row["body_json"]) for row in rows]
        api_name = query.get("api-name")
        api_version = query.get("api-version")
        if api_name:
            services = [item for item in services if item.get("apiName") == api_name]
        if api_version:
            services = [item for item in services if item.get("apiVersion") == api_version]
        for item in services:
            versions = item.get("vendorSpecific-o-ran.org", {}).get("fullApiVersions", [])
            if (not isinstance(versions, list) or not versions
                    or any(not isinstance(version, str) or re.fullmatch(
                        r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
                        r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
                        version) is None for version in versions)):
                raise ServiceFailure(409, "AIC_CAPABILITY_MISMATCH", "discovered service version mismatch")
        return Response(200, services, {"Version": SERVICE_API_VERSION})

    # R1 A1 policy management
    def _policy_route(
        self,
        method: str,
        path: str,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        body: Any,
    ) -> Response:
        if headers.get("version") != R1_VERSION:
            raise ContractValidationError("Version header must be 1.0.0")
        root = "/a1-policy-management/v1"
        suffix = path[len(root) :]
        if suffix == "/policy-types" and method == "GET":
            return self._versioned(200, self.capability["policyTypes"])
        if suffix.startswith("/policy-types/") and method == "GET":
            policy_type_id = unquote(suffix[len("/policy-types/") :])
            if policy_type_id not in self.capability["policyTypes"]:
                raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "unknown policy type")
            return self._versioned(
                200,
                {"policyTypeId": policy_type_id, "schemaSha256": self.capability["schemaDigests"]["policy"]},
            )
        if suffix == "/policies" and method == "POST":
            rapp_id = self.security.authenticated_rapp_id(headers)
            return self._create_policy(rapp_id, body)
        if suffix == "/policies" and method == "GET":
            return self._list_policies(query)
        if suffix == "/policies/subscriptions" and method == "POST":
            return self._create_subscription(body)
        if suffix.startswith("/policies/subscriptions/"):
            subscription_id = unquote(suffix[len("/policies/subscriptions/") :])
            return self._subscription_item(method, subscription_id, body)
        if suffix.startswith("/policies/"):
            item = suffix[len("/policies/") :]
            if item.endswith("/status"):
                policy_id = unquote(item[: -len("/status")])
                if method != "GET":
                    raise ServiceFailure(405, "AIC_RESOURCE_NOT_FOUND", "operation is not supported")
                return self._get_policy_status(policy_id)
            policy_id = unquote(item)
            if method == "GET":
                return self._get_policy(policy_id)
            if method == "PUT":
                return self._update_policy(policy_id, body)
            if method == "DELETE":
                return self._delete_policy(policy_id)
        raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "policy resource is not supported")

    def _create_policy(self, rapp_id: str, information: Any) -> Response:
        if not self.a1_ready:
            raise ServiceFailure(409, "AIC_E2_NOT_READY", "E2 inventory is not READY")
        if not isinstance(information, dict) or set(information) != {
            "nearRtRicId",
            "policyTypeId",
            "policyObject",
        }:
            raise ContractValidationError("PolicyObjectInformation has invalid members")
        near_rt_ric_id = information["nearRtRicId"]
        policy_type_id = information["policyTypeId"]
        policy_object = information["policyObject"]
        if near_rt_ric_id != self.capability["nearRtRicId"]:
            raise ServiceFailure(409, "AIC_CAPABILITY_MISMATCH", "nearRtRicId mismatch")
        if policy_type_id != POLICY_TYPE_ID or policy_type_id not in self.capability["policyTypes"]:
            raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "unsupported policy type")
        validate(policy_object, POLICY_SCHEMA_ID, self.bundle_dir)
        idempotency_key = policy_object["trace"]["idempotencyKey"]
        digest = jcs_sha256(information)
        with self.store.transaction() as db:
            existing = db.execute(
                """SELECT * FROM policies WHERE rapp_id=? AND near_rt_ric_id=?
                   AND policy_type_id=? AND idempotency_key=?""",
                (rapp_id, near_rt_ric_id, policy_type_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["payload_digest"] != digest:
                    raise ServiceFailure(409, "AIC_IDEMPOTENCY_CONFLICT", "same key has a different payload")
                row = existing
            else:
                configured = db.execute(
                    "SELECT value_json FROM metadata WHERE key='harness:next-policy-id'"
                ).fetchone()
                if configured is None:
                    policy_id = str(uuid.uuid4())
                else:
                    policy_id = self.store.decode(configured["value_json"])
                    db.execute("DELETE FROM metadata WHERE key='harness:next-policy-id'")
                location = f"{self.r1_api_root}/a1-policy-management/v1/policies/{policy_id}"
                db.execute(
                    """INSERT INTO policies(
                       policy_id,rapp_id,near_rt_ric_id,policy_type_id,idempotency_key,
                       payload_digest,policy_json,information_json,state,location
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        policy_id,
                        rapp_id,
                        near_rt_ric_id,
                        policy_type_id,
                        idempotency_key,
                        digest,
                        self.store.encode(policy_object),
                        self.store.encode(information),
                        "WAL_PENDING",
                        location,
                    ),
                )
                row = db.execute("SELECT * FROM policies WHERE policy_id=?", (policy_id,)).fetchone()
        if row["state"] != "ACTIVE":
            if self.store.consume_fault(
                "CRASH_PROCESS", {"stage": "AFTER_LEDGER_BEFORE_A1_PUT"}
            ):
                raise SimulatedCrash("durable ledger committed before A1-P PUT")
            self._reconcile_policy(row["policy_id"])
            row = self._policy_row(row["policy_id"])
        response = self._versioned(
            201,
            self.store.decode(row["information_json"]),
            Location=row["location"],
        )
        if self.store.consume_fault(
            "DROP_HTTP_RESPONSE", {"stage": "AFTER_R1_CREATE_COMMIT"}
        ):
            raise ResponseDropped("R1 create response dropped after durable success")
        return response

    def _reconcile_policy(self, policy_id: str) -> None:
        row = self._policy_row(policy_id)
        policy = self.store.decode(row["policy_json"])
        try:
            matches = (
                self.a1.find_by_idempotency_key(
                    row["policy_type_id"], row["idempotency_key"]
                )
                if self.a1_pre_put_reconciliation_probe
                else []
            )
            if len(matches) > 1:
                raise ServiceFailure(409, "AIC_IDEMPOTENCY_CONFLICT", "multiple A1 resources match trace")
            if matches:
                remote_id, remote_policy = matches[0]
                if remote_id != policy_id or jcs_sha256(remote_policy) != jcs_sha256(policy):
                    raise ServiceFailure(409, "AIC_IDEMPOTENCY_CONFLICT", "A1 reconciliation mismatch")
            else:
                try:
                    self.a1.put_policy(
                        row["policy_type_id"],
                        policy_id,
                        policy,
                        self.a1_notification_destination,
                    )
                except A1ProtocolError as exc:
                    status = exc.response.status
                    if status not in A1_REFUSAL_CODES:
                        raise
                    # The create was refused before anything was written: it
                    # never became a policy.  Left WAL_PENDING/UNCERTAIN it
                    # would be re-PUT by ``reconcile_desired_state``; relayed
                    # as 503 it read upstream as "may have been written".
                    self._finalize_policy_delete(policy_id)
                    raise ServiceFailure(status, A1_REFUSAL_CODES[status], str(exc)) from exc
        except ServiceFailure:
            raise
        except Exception:
            self.store.execute("UPDATE policies SET state='UNCERTAIN' WHERE policy_id=?", (policy_id,))
            raise
        self.store.execute("UPDATE policies SET state='ACTIVE' WHERE policy_id=?", (policy_id,))

    def reconcile_desired_state(self) -> None:
        """Recover framework crash and Near-RT restart from durable desired state."""
        rows = self.store.rows("SELECT * FROM policies")
        remote_by_type: dict[str, set[str]] = {}
        for row in rows:
            policy_type_id = row["policy_type_id"]
            if row["pending_operation"] == "UPDATE":
                pending = self.store.decode(row["pending_policy_json"])
                remote = self.a1.get_policy(policy_type_id, row["policy_id"])
                if remote is not None and jcs_sha256(remote) != jcs_sha256(pending):
                    current = self.store.decode(row["policy_json"])
                    if jcs_sha256(remote) != jcs_sha256(current):
                        raise ServiceFailure(
                            409,
                            "AIC_IDEMPOTENCY_CONFLICT",
                            "pending update reconciliation found an unknown A1 payload",
                        )
                if remote is None or jcs_sha256(remote) != jcs_sha256(pending):
                    self.a1.put_policy(
                        policy_type_id,
                        row["policy_id"],
                        pending,
                        self.a1_notification_destination,
                    )
                self.store.execute(
                    """UPDATE policies SET policy_json=?,pending_operation=NULL,
                       pending_policy_json=NULL WHERE policy_id=?""",
                    (self.store.encode(pending), row["policy_id"]),
                )
                continue
            if row["pending_operation"] == "DELETE":
                remote = self.a1.get_policy(policy_type_id, row["policy_id"])
                if remote is not None:
                    self.a1.delete_policy(policy_type_id, row["policy_id"])
                self._finalize_policy_delete(row["policy_id"])
                continue
            if row["state"] != "ACTIVE":
                self._reconcile_policy(row["policy_id"])
                continue
            if policy_type_id not in remote_by_type:
                remote_by_type[policy_type_id] = set(self.a1.list_policy_ids(policy_type_id))
            policy = self.store.decode(row["policy_json"])
            remote = (
                self.a1.get_policy(policy_type_id, row["policy_id"])
                if row["policy_id"] in remote_by_type[policy_type_id]
                else None
            )
            if remote is None or jcs_sha256(remote) != jcs_sha256(policy):
                self.a1.put_policy(
                    policy_type_id,
                    row["policy_id"],
                    policy,
                    self.a1_notification_destination,
                )

    def _list_policies(self, query: Mapping[str, str]) -> Response:
        clauses: list[str] = []
        params: list[str] = []
        for query_key, column in (
            ("nearRtRicId", "near_rt_ric_id"),
            ("policyTypeId", "policy_type_id"),
        ):
            if query_key in query:
                clauses.append(f"{column}=?")
                params.append(query[query_key])
        sql = "SELECT * FROM policies"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY policy_id"
        # Each listed policy names its id (2026-09-24, Q-3): without it a caller that
        # lost a CREATE's answer cannot find the policy it made and leaves it orphaned.
        return self._versioned(
            200,
            [{**self._current_information(row), "policyId": row["policy_id"]}
             for row in self.store.rows(sql, tuple(params))],
        )

    def _get_policy(self, policy_id: str) -> Response:
        row = self._policy_row(policy_id)
        return self._versioned(200, self._current_information(row))

    def _update_policy(self, policy_id: str, policy_object: Any) -> Response:
        row = self._policy_row(policy_id)
        validate(policy_object, POLICY_SCHEMA_ID, self.bundle_dir)
        old = self.store.decode(row["policy_json"])
        if policy_object["scope"]["ueId"] != old["scope"]["ueId"]:
            raise ServiceFailure(409, "AIC_POLICY_CONFLICT", "scope.ueId cannot change on update")
        if policy_object["trace"]["policyRevision"] <= old["trace"]["policyRevision"]:
            if jcs_sha256(policy_object) == jcs_sha256(old):
                return self._versioned(200, old)
            raise ServiceFailure(409, "AIC_STALE_REVISION", "policy revision is not higher")
        # Persist desired update before the external A1 call.  A crash or
        # ambiguous transport failure is reconciled by standard GET then PUT.
        self.store.execute(
            """UPDATE policies SET pending_operation='UPDATE',pending_policy_json=?
               WHERE policy_id=?""",
            (self.store.encode(policy_object), policy_id),
        )
        try:
            self.a1.put_policy(
                row["policy_type_id"], policy_id, policy_object, self.a1_notification_destination
            )
        except A1ProtocolError as exc:
            self._drop_refused_pending(policy_id, exc)
            raise
        self.store.execute(
            """UPDATE policies SET policy_json=?,pending_operation=NULL,
               pending_policy_json=NULL WHERE policy_id=?""",
            (self.store.encode(policy_object), policy_id),
        )
        return self._versioned(200, policy_object)

    def _delete_policy(self, policy_id: str) -> Response:
        row = self._policy_row(policy_id)
        self.store.execute(
            "UPDATE policies SET pending_operation='DELETE' WHERE policy_id=?", (policy_id,)
        )
        try:
            self.a1.delete_policy(row["policy_type_id"], policy_id)
        except A1ProtocolError as exc:
            if exc.response.status == 404:
                # Not a refusal: the resource is already gone at A1-P, which is
                # what DELETE wanted.  Keeping the row would let reconciliation
                # re-PUT a policy the rApp deleted.
                self._finalize_policy_delete(policy_id)
                return self._versioned(204)
            self._drop_refused_pending(policy_id, exc)
            raise
        self._finalize_policy_delete(policy_id)
        return self._versioned(204)

    def _drop_refused_pending(self, policy_id: str, exc: A1ProtocolError) -> None:
        """A 400/404/409 from A1-P is a decision taken before anything was written.

        Left pending, ``reconcile_desired_state`` would later re-send the
        refused operation -- for a hand-back refused because the UE had died,
        that is a stale pin replayed onto whatever is there then.  Relayed as
        503 it was also misread upstream as "may have been written"
        (v46r8 board 462, 2026-09-23).
        """
        status = exc.response.status
        if status not in A1_REFUSAL_CODES:
            return
        self.store.execute(
            """UPDATE policies SET pending_operation=NULL,pending_policy_json=NULL
               WHERE policy_id=?""",
            (policy_id,),
        )
        raise ServiceFailure(status, A1_REFUSAL_CODES[status], str(exc)) from exc

    def _finalize_policy_delete(self, policy_id: str) -> None:
        for job in self.store.rows("SELECT data_job_id,body_json FROM data_jobs"):
            body = self.store.decode(job["body_json"])
            if body.get("productionJobDefinition", {}).get("policyId") == policy_id:
                self.store.execute("DELETE FROM data_jobs WHERE data_job_id=?", (job["data_job_id"],))
        self.store.execute("DELETE FROM policies WHERE policy_id=?", (policy_id,))

    def expire_policies(self, now: datetime | None = None) -> list[str]:
        """Delete expired desired resources and their DME jobs.

        A scheduler may call this method; wall-clock timestamps are used only
        for the externally defined validity interval.
        """
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ValueError("expiry evaluation requires a timezone-aware instant")
        expired: list[str] = []
        for row in self.store.rows("SELECT policy_id,policy_json FROM policies"):
            policy = self.store.decode(row["policy_json"])
            expires_at = datetime.fromisoformat(
                policy["validity"]["expiresAt"].replace("Z", "+00:00")
            )
            if expires_at <= instant:
                self._delete_policy(row["policy_id"])
                expired.append(row["policy_id"])
        return expired

    def _policy_row(self, policy_id: str) -> sqlite3.Row:
        row = self.store.row("SELECT * FROM policies WHERE policy_id=?", (policy_id,))
        if row is None:
            raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "policy does not exist")
        return row

    def _current_information(self, row: sqlite3.Row) -> dict[str, Any]:
        information = self.store.decode(row["information_json"])
        information["policyObject"] = self.store.decode(row["policy_json"])
        return information

    # Status query, consumption, de-duplication and R1 wrapper relay
    def _get_policy_status(self, policy_id: str) -> Response:
        row = self._policy_row(policy_id)
        status = self.a1.get_status(row["policy_type_id"], policy_id)
        self._consume_status(policy_id, status, queried=True)
        current = self.store.row("SELECT status_json FROM statuses WHERE policy_id=?", (policy_id,))
        return self._versioned(200, self.store.decode(current["status_json"]))

    def receive_a1_status(self, policy_id: str, status: Mapping[str, Any]) -> Response:
        self._policy_row(policy_id)
        self._consume_status(policy_id, dict(status), queried=False)
        return Response(204)

    def _consume_status(self, policy_id: str, status: dict[str, Any], *, queried: bool) -> None:
        validate(status, STATUS_SCHEMA_ID, self.bundle_dir)
        aic_status = status.get("aicStatus", {})
        if aic_status.get("policyId") != policy_id:
            raise ContractValidationError("status policyId mismatch")

        query_authoritative = False
        relay = False
        with self._status_lock:
            epoch = aic_status["producerEpoch"]
            sequence = aic_status["statusSeq"]
            current = self.store.row("SELECT * FROM statuses WHERE policy_id=?", (policy_id,))
            if current is not None and current["producer_epoch"] != epoch:
                retired = self.store.row(
                    """SELECT 1 FROM status_audit
                       WHERE policy_id=? AND producer_epoch=?
                         AND disposition IN (
                             'APPLIED_QUERY','APPLIED_NOTIFICATION','APPLIED_INITIAL_STATE')
                       LIMIT 1""",
                    (policy_id, epoch),
                )
                if retired is not None:
                    self._audit_status(
                        policy_id, status,
                        "OLD_PRODUCER_EPOCH_QUERY" if queried else "OLD_PRODUCER_EPOCH")
                    return
            if not queried and (current is None or current["producer_epoch"] != epoch):
                self._audit_status(
                    policy_id, status, "NEW_EPOCH_NOTIFICATION_QUERY_REQUIRED")
                query_authoritative = True
            elif current is not None and current["producer_epoch"] == epoch:
                if sequence == current["status_seq"]:
                    self._audit_status(policy_id, status, "DUPLICATE")
                    return
                if sequence < current["status_seq"]:
                    self._audit_status(
                        policy_id, status,
                        "LATE_LOWER_QUERY" if queried else "LATE_LOWER_SEQUENCE")
                    return
            if not query_authoritative:
                self.store.execute(
                    """INSERT INTO statuses(policy_id,producer_epoch,status_seq,status_json) VALUES(?,?,?,?)
                       ON CONFLICT(policy_id) DO UPDATE SET producer_epoch=excluded.producer_epoch,
                       status_seq=excluded.status_seq,status_json=excluded.status_json""",
                    (policy_id, epoch, sequence, self.store.encode(status)),
                )
                self._audit_status(
                    policy_id, status, "APPLIED_QUERY" if queried else "APPLIED_NOTIFICATION")
                relay = True

        # Both boundaries may synchronously re-enter the status ingress path.
        # No outbound transport is allowed while the ordering lock is held.
        if query_authoritative:
            row = self._policy_row(policy_id)
            authoritative = self.a1.get_status(row["policy_type_id"], policy_id)
            self._consume_status(policy_id, authoritative, queried=True)
        elif relay:
            self._relay_status(policy_id, status)

    def _audit_status(self, policy_id: str, status: Mapping[str, Any], disposition: str) -> None:
        aic_status = status.get("aicStatus", {})
        self.store.execute(
            """INSERT INTO status_audit(policy_id,producer_epoch,status_seq,disposition,status_json)
               VALUES(?,?,?,?,?)""",
            (
                policy_id,
                aic_status.get("producerEpoch"),
                aic_status.get("statusSeq"),
                disposition,
                self.store.encode(status),
            ),
        )

    def _relay_status(self, policy_id: str, status: Mapping[str, Any]) -> None:
        if self.callback_sender is None:
            return
        for row in self.store.rows("SELECT subscription_id,body_json FROM subscriptions"):
            subscription = self.store.decode(row["body_json"])
            policy_ids = subscription.get("policyIdList", [])
            if policy_ids and policy_id not in policy_ids:
                continue
            wrapper = {
                "subscriptionId": row["subscription_id"],
                "policyStates": [{"policyId": policy_id, "policyStatusObject": status}],
            }
            boundary = {"sender": "NON_RT_RIC_FRAMEWORK", "interface": "R1_STATUS"}
            if self.store.consume_fault("DROP_CALLBACK_DELIVERY", boundary):
                self._audit_status(policy_id, status, "R1_CALLBACK_DROPPED")
                continue
            try:
                callback_status = self.callback_sender(subscription["notificationDestination"], wrapper)
            except OSError:
                self._audit_status(policy_id, status, "R1_CALLBACK_TRANSPORT_FAILED")
                continue
            if callback_status != 204:
                self._audit_status(policy_id, status, f"R1_CALLBACK_HTTP_{callback_status}")

    def _create_subscription(self, body: Any) -> Response:
        self._validate_subscription(body)
        subscription_id = str(uuid.uuid4())
        self.store.execute(
            "INSERT INTO subscriptions(subscription_id,body_json) VALUES(?,?)",
            (subscription_id, self.store.encode(body)),
        )
        location = f"{self.r1_api_root}/a1-policy-management/v1/policies/subscriptions/{subscription_id}"
        response = dict(body)
        response["subscriptionId"] = subscription_id
        return self._versioned(201, response, Location=location)

    def _subscription_item(self, method: str, subscription_id: str, body: Any) -> Response:
        row = self.store.row(
            "SELECT body_json FROM subscriptions WHERE subscription_id=?", (subscription_id,)
        )
        if row is None:
            raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "subscription does not exist")
        if method == "GET":
            response = self.store.decode(row["body_json"])
            response["subscriptionId"] = subscription_id
            return self._versioned(200, response)
        if method == "PUT":
            self._validate_subscription(body)
            self.store.execute(
                "UPDATE subscriptions SET body_json=? WHERE subscription_id=?",
                (self.store.encode(body), subscription_id),
            )
            response = dict(body)
            response["subscriptionId"] = subscription_id
            return self._versioned(200, response)
        if method == "DELETE":
            self.store.execute("DELETE FROM subscriptions WHERE subscription_id=?", (subscription_id,))
            return self._versioned(204)
        raise ServiceFailure(405, "AIC_RESOURCE_NOT_FOUND", "operation is not supported")

    @staticmethod
    def _validate_subscription(body: Any) -> None:
        if not isinstance(body, dict) or not isinstance(body.get("notificationDestination"), str):
            raise ContractValidationError("invalid PolicyStatusSubscription")
        if "policyIdList" in body and not isinstance(body["policyIdList"], list):
            raise ContractValidationError("policyIdList must be an array")

    # DME server-side registration, discovery and job negotiation
    def _persist_capability_registration(self, body: dict[str, Any]) -> None:
        self._validate_registration(body)
        dme_type_id = self._dme_type_id(body)
        if dme_type_id != "aic:ran-capability:1.0.0":
            raise ValueError("capability registration DME type mismatch")
        registration_id = "capability-manifest"
        self.store.execute(
            """INSERT INTO dme_registrations(registration_id,dme_type_id,body_json) VALUES(?,?,?)
               ON CONFLICT(registration_id) DO UPDATE SET body_json=excluded.body_json""",
            (registration_id, dme_type_id, self.store.encode(body)),
        )

    def _dme_registration_route(self, method: str, path: str, body: Any) -> Response:
        root = "/data-registration/v2/production-capabilities"
        if path == root:
            return self._dme_register(method, body)
        registration_id = unquote(path.removeprefix(root + "/"))
        if not registration_id or "/" in registration_id:
            raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "DME registration does not exist")
        row = self.store.row("SELECT 1 FROM dme_registrations WHERE registration_id=?", (registration_id,))
        if row is None:
            raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "DME registration does not exist")
        if method == "DELETE":
            self.store.execute("DELETE FROM dme_registrations WHERE registration_id=?", (registration_id,))
            return Response(204, headers={"Version": DME_REGISTRATION_VERSION})
        raise ServiceFailure(405, "AIC_RESOURCE_NOT_FOUND", "operation is not supported")

    def _dme_register(self, method: str, body: Any) -> Response:
        if method != "POST":
            raise ServiceFailure(405, "AIC_RESOURCE_NOT_FOUND", "operation is not supported")
        self._validate_registration(body)
        registration_id = str(uuid.uuid4())
        dme_type_id = self._dme_type_id(body)
        if dme_type_id != "aic:policy-evidence:1.0.0":
            raise ContractValidationError("external DME registration type is not supported")
        self._validate_evidence_registration(body)
        self.store.execute(
            "INSERT INTO dme_registrations(registration_id,dme_type_id,body_json) VALUES(?,?,?)",
            (registration_id, dme_type_id, self.store.encode(body)),
        )
        location = f"{self.r1_api_root}/data-registration/v2/production-capabilities/{registration_id}"
        return Response(201, body, {"Location": location, "Version": DME_REGISTRATION_VERSION})

    @staticmethod
    def _validate_registration(body: Any) -> None:
        if not isinstance(body, dict) or set(body) != {
            "dmeTypeDefinition",
            "dataAccessEndpoint",
            "dataDeliveryModes",
        }:
            raise ContractValidationError("DmeTypeRelatedCapabilities outer members are invalid")
        definition = body["dmeTypeDefinition"]
        required = {
            "dmeTypeId",
            "metadata",
            "dataProductionSchema",
            "dataDeliverySchemas",
            "dataDeliveryMechanisms",
        }
        if not isinstance(definition, dict) or set(definition) != required:
            raise ContractValidationError("DmeTypeDefinition is incomplete")

    @staticmethod
    def _dme_type_id(body: Mapping[str, Any]) -> str:
        identifier = body["dmeTypeDefinition"]["dmeTypeId"]
        if isinstance(identifier, str):
            return identifier
        return f"{identifier['namespace']}:{identifier['name']}:{identifier['version']}"

    def _validate_evidence_registration(self, body: Mapping[str, Any]) -> None:
        definition = body["dmeTypeDefinition"]
        validator = ContractValidator(self.bundle_dir)
        expected_schema = validator.schema("aic.policy-evidence-filter.1.0.0.schema.json")
        if jcs_sha256(definition["dataProductionSchema"]) != jcs_sha256(expected_schema):
            raise ContractValidationError("DME dataProductionSchema mismatch")
        delivery_schemas = definition["dataDeliverySchemas"]
        if not isinstance(delivery_schemas, list) or len(delivery_schemas) != 1:
            raise ContractValidationError("DME dataDeliverySchemas mismatch")
        delivery = delivery_schemas[0]
        if (not isinstance(delivery, dict)
                or set(delivery) != {"type", "deliverySchemaId", "schema"}
                or delivery.get("type") != "JSON_SCHEMA"
                or delivery.get("deliverySchemaId") != "aic.policy-evidence.record.schema.1.0.0"
                or not isinstance(delivery.get("schema"), str)):
            raise ContractValidationError("DME dataDeliverySchemas mismatch")
        try:
            parsed_schema = json.loads(delivery["schema"])
        except json.JSONDecodeError as exc:
            raise ContractValidationError("DME delivery schema is not JSON") from exc
        if canonicalize(parsed_schema).decode("utf-8") != delivery["schema"]:
            raise ContractValidationError("DME delivery schema is not RFC 8785 canonical JSON")
        if definition["dataDeliveryMechanisms"] != [{"dataDeliveryMethod": "PUSH_HTTP"}]:
            raise ContractValidationError("DME dataDeliveryMechanisms mismatch")
        if body["dataDeliveryModes"] != ["CONTINUOUS"]:
            raise ContractValidationError("DME dataDeliveryModes mismatch")

    def _dme_discover(self, method: str, path: str) -> Response:
        if method != "GET":
            raise ServiceFailure(405, "AIC_RESOURCE_NOT_FOUND", "operation is not supported")
        root = "/data-discovery/v2/dme-types"
        if path == root:
            rows = self.store.rows("SELECT body_json FROM dme_registrations ORDER BY registration_id")
        elif path.startswith(root + "/"):
            dme_type_id = unquote(path[len(root) + 1 :])
            rows = self.store.rows(
                "SELECT body_json FROM dme_registrations WHERE dme_type_id=? ORDER BY registration_id",
                (dme_type_id,),
            )
            if not rows:
                raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "DME type does not exist")
        else:
            raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "DME resource does not exist")
        return Response(200, [self.store.decode(row["body_json"]) for row in rows], {"Version": DME_DISCOVERY_VERSION})

    def _data_job_route(self, method: str, path: str, body: Any) -> Response:
        root = "/data-access/v2/data-jobs"
        if path == root:
            if method != "POST":
                raise ServiceFailure(405, "AIC_RESOURCE_NOT_FOUND", "operation is not supported")
            self._validate_data_job(body)
            self._validate_data_job_bindings(body)
            data_job_id = str(uuid.uuid4())
            response = dict(body)
            response["dataJobInfoStatus"] = "RUNNING"
            self.store.execute(
                "INSERT INTO data_jobs(data_job_id,body_json,status) VALUES(?,?,?)",
                (data_job_id, self.store.encode(response), "RUNNING"),
            )
            location = f"{self.r1_api_root}/data-access/v2/data-jobs/{data_job_id}"
            return Response(201, response, {"Location": location, "Version": DME_ACCESS_VERSION})
        item = path[len(root) + 1 :] if path.startswith(root + "/") else ""
        status_resource = item.endswith("/status")
        data_job_id = unquote(item[: -len("/status")] if status_resource else item)
        row = self.store.row("SELECT * FROM data_jobs WHERE data_job_id=?", (data_job_id,))
        if row is None:
            raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "data job does not exist")
        if status_resource:
            if method != "GET":
                raise ServiceFailure(405, "AIC_RESOURCE_NOT_FOUND", "operation is not supported")
            job = self.store.decode(row["body_json"])
            binding = job["pushDeliveryDetailsHttp"]["dataPushUri"].rstrip("/").rsplit("/", 1)[-1]
            accepted = self.store.row(
                "SELECT value_json FROM metadata WHERE key=?",
                ("dme-push:" + binding,))
            digests = self.store.decode(accepted["value_json"]) if accepted else []
            response = {
                "dataJobInfoStatus": row["status"],
                "acceptedPushPayloadCount": len(digests),
            }
            return Response(200, response, {"Version": DME_ACCESS_VERSION})
        if method == "GET":
            return Response(200, self.store.decode(row["body_json"]), {"Version": DME_ACCESS_VERSION})
        if method == "PUT":
            self._validate_data_job(body)
            self._validate_data_job_bindings(body)
            response = dict(body)
            response["dataJobInfoStatus"] = "RUNNING"
            self.store.execute(
                "UPDATE data_jobs SET body_json=?,status='RUNNING' WHERE data_job_id=?",
                (self.store.encode(response), data_job_id),
            )
            return Response(200, response, {"Version": DME_ACCESS_VERSION})
        if method == "DELETE":
            self.store.execute("DELETE FROM data_jobs WHERE data_job_id=?", (data_job_id,))
            return Response(204, headers={"Version": DME_ACCESS_VERSION})
        raise ServiceFailure(405, "AIC_RESOURCE_NOT_FOUND", "operation is not supported")

    def record_dme_push(self, delivery_binding_id: str,
                        record: Mapping[str, Any]) -> None:
        """Record a successful binding-aware DME delivery acknowledgement."""
        rows = self.store.rows("SELECT body_json FROM data_jobs")
        matches = []
        for row in rows:
            job = self.store.decode(row["body_json"])
            uri = job["pushDeliveryDetailsHttp"]["dataPushUri"]
            if uri.rstrip("/").rsplit("/", 1)[-1] == delivery_binding_id:
                matches.append(job)
        if len(matches) != 1:
            raise ContractValidationError("delivery binding does not map to exactly one data job")
        digest = jcs_sha256(record)
        key = "dme-push:" + delivery_binding_id
        existing = self.store.row("SELECT value_json FROM metadata WHERE key=?", (key,))
        values = self.store.decode(existing["value_json"]) if existing else []
        if digest not in values:
            values.append(digest)
        self.store.execute(
            "INSERT INTO metadata(key,value_json) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
            (key, self.store.encode(values)))

    @staticmethod
    def _validate_data_job(body: Any) -> None:
        required = {
            "dataDeliveryMode",
            "dmeTypeId",
            "productionJobDefinition",
            "dataDeliveryMethod",
            "dataDeliverySchemaId",
            "pushDeliveryDetailsHttp",
        }
        if not isinstance(body, dict) or set(body) != required:
            raise ContractValidationError("DataJobInfo members are invalid")
        if body["dataDeliveryMode"] != "CONTINUOUS" or body["dataDeliveryMethod"] != "PUSH_HTTP":
            raise ContractValidationError("only CONTINUOUS + PUSH_HTTP is supported")
        push = body["pushDeliveryDetailsHttp"]
        if not isinstance(push, dict) or set(push) != {"dataPushUri"}:
            raise ContractValidationError("pushDeliveryDetailsHttp must contain only dataPushUri")
        production = body["productionJobDefinition"]
        if not isinstance(production, dict) or set(production) != {
            "policyTypeId",
            "policyId",
            "minimumPolicyRevision",
            "nearRtRicId",
        }:
            raise ContractValidationError("productionJobDefinition members are invalid")

    def _validate_data_job_bindings(self, body: Mapping[str, Any]) -> None:
        if body["dmeTypeId"] != "aic:policy-evidence:1.0.0":
            raise ContractValidationError("unsupported data-job DME type")
        if body["dataDeliverySchemaId"] != "aic.policy-evidence.record.schema.1.0.0":
            raise ContractValidationError("data-job delivery schema mismatch")
        registered = self.store.row(
            "SELECT 1 FROM dme_registrations WHERE dme_type_id=?",
            (body["dmeTypeId"],),
        )
        if registered is None:
            raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "DME type is not registered")
        production = body["productionJobDefinition"]
        if (
            production["policyTypeId"] != POLICY_TYPE_ID
            or production["nearRtRicId"] != self.capability["nearRtRicId"]
        ):
            raise ServiceFailure(409, "AIC_CAPABILITY_MISMATCH", "data-job policy binding mismatch")
        minimum_revision = production["minimumPolicyRevision"]
        if (
            not isinstance(minimum_revision, int)
            or isinstance(minimum_revision, bool)
            or minimum_revision < 1
        ):
            raise ContractValidationError("minimumPolicyRevision is invalid")
        require_https_or_loopback(
            body["pushDeliveryDetailsHttp"]["dataPushUri"],
            self.security.insecure_dev_mode,
        )

    # Development-only, non-O-RAN harness control plane.  Either the insecure
    # development flag or the loopback-only two-key integration control
    # approval exposes it; with neither key it stays absent (404).
    def _harness(self, method: str, path: str, body: Any) -> Response:
        if not (self.security.insecure_dev_mode
                or self.security.integration_control_approved):
            raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "development harness is disabled")
        if method == "GET" and path == "/harness/state":
            return Response(200, self.store.snapshot())
        if method != "POST" or not isinstance(body, dict):
            raise ContractValidationError("invalid harness request")
        if path == "/harness/fault":
            fault = body.get("fault")
            boundary = body.get("boundary", {})
            if fault not in {"CRASH_PROCESS", "DROP_HTTP_RESPONSE", "DROP_CALLBACK_DELIVERY"}:
                raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "fault is not owned by Non-RT")
            if not isinstance(boundary, dict):
                raise ContractValidationError("fault boundary must be an object")
            self.store.reserve_fault(fault, boundary)
            return Response(200, {"outputs": {}})
        if path in {"/harness/restart", "/harness/op"}:
            component = body.get("component")
            operation = body.get("op", "PROCESS_RESTART")
            if operation == "RESET_SCENARIO_STATE":
                self.store.reset_scenario_state()
                return Response(200, {"outputs": {"resetCompleted": True}})
            if operation == "CONFIGURE_NEXT_POLICY_ID":
                policy_id = body.get("policyId")
                if (not isinstance(policy_id, str) or not policy_id
                        or "/" in policy_id):
                    raise ContractValidationError("next policyId is invalid")
                self.store.execute(
                    "INSERT INTO metadata(key,value_json) VALUES('harness:next-policy-id',?)",
                    (self.store.encode(policy_id),),
                )
                return Response(200, {"outputs": {"configured": True}})
            if operation in {
                    "START_R1_A1_SERVICE", "INSTALL_R1_STATUS_SUBSCRIPTION",
                    "START_SERVICE_REGISTRATION_API",
                    "INSTALL_PINNED_SERVICE_DESCRIPTIONS", "START_R1_DME_APIS",
                    "INSTALL_NON_RT_DESIRED_POLICY"}:
                if operation == "INSTALL_PINNED_SERVICE_DESCRIPTIONS":
                    versions = body.get("discoveredVersions")
                    rapp_id = body.get("rAppId")
                    if not isinstance(versions, dict) or not isinstance(rapp_id, str):
                        raise ContractValidationError("pinned service versions are required")
                    for api_name, full_version in versions.items():
                        major = str(full_version).split(".", 1)[0]
                        api_id = str(uuid.uuid4())
                        description = {
                            "apiName": api_name,
                            "apiVersion": "v" + major,
                            "aefProfiles": [{"aefId": "aef-" + api_name}],
                            "communicationType": "REQUEST_RESPONSE",
                            "vendorSpecific-o-ran.org": {
                                "fullApiVersions": [full_version]},
                            "apiId": api_id,
                        }
                        self.store.execute(
                            "INSERT INTO services(api_id,rapp_id,body_json) VALUES(?,?,?)",
                            (api_id, rapp_id, self.store.encode(description)),
                        )
                if operation == "INSTALL_NON_RT_DESIRED_POLICY":
                    policy_id, policy = body.get("policyId"), body.get("policy")
                    if not isinstance(policy_id, str) or not isinstance(policy, dict):
                        raise ContractValidationError("policy seed requires policyId and policy")
                    information = {
                        "nearRtRicId": self.capability["nearRtRicId"],
                        "policyTypeId": POLICY_TYPE_ID,
                        "policyObject": policy,
                    }
                    self.store.execute(
                        """INSERT OR REPLACE INTO policies(
                           policy_id,rapp_id,near_rt_ric_id,policy_type_id,idempotency_key,
                           payload_digest,policy_json,information_json,state,location
                           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (policy_id, "scenario-seed", self.capability["nearRtRicId"], POLICY_TYPE_ID,
                         policy["trace"]["idempotencyKey"], jcs_sha256(information),
                         self.store.encode(policy), self.store.encode(information), "ACTIVE",
                         f"{self.r1_api_root}/a1-policy-management/v1/policies/{policy_id}"),
                    )
                    status = body.get("status")
                    if isinstance(status, dict):
                        validate(status, STATUS_SCHEMA_ID, self.bundle_dir)
                        aic = status["aicStatus"]
                        if aic["policyId"] != policy_id:
                            raise ContractValidationError("seed status policyId mismatch")
                        self.store.execute(
                            """INSERT OR REPLACE INTO statuses(
                               policy_id,producer_epoch,status_seq,status_json) VALUES(?,?,?,?)""",
                            (policy_id, aic["producerEpoch"], aic["statusSeq"],
                             self.store.encode(status)),
                        )
                        self._audit_status(policy_id, status, "APPLIED_INITIAL_STATE")
                return Response(200, {"outputs": {"ready": True}})
            if operation == "INSTALL_DME_REGISTRATION":
                registration = body.get("registration")
                response = self._dme_register("POST", registration)
                return Response(200, {"outputs": {
                    "ready": response.status == 201,
                }})
            if operation == "INSTALL_ACTIVE_DATA_JOB":
                job = body.get("job")
                data_job_id = body.get("dataJobId")
                # (P0) The catalog's standardInitialStates dataJobInfo describes
                # an ALREADY INSTALLED job and therefore carries
                # dataJobInfoStatus: "RUNNING"; _validate_data_job is the
                # create-REQUEST validator and requires an exact member set
                # without it.  Accept the declared seed member, assert it says
                # RUNNING, and validate the request-shaped remainder.  The wire
                # create path is untouched.
                if isinstance(job, dict) and "dataJobInfoStatus" in job:
                    if job["dataJobInfoStatus"] != "RUNNING":
                        raise ContractValidationError(
                            "an installed data job seed must declare RUNNING")
                    job = {key: value for key, value in job.items()
                           if key != "dataJobInfoStatus"}
                self._validate_data_job(job)
                if not isinstance(data_job_id, str) or not data_job_id:
                    raise ContractValidationError("active dataJobId is required")
                if self.store.row(
                        "SELECT 1 FROM dme_registrations WHERE dme_type_id=?",
                        (job["dmeTypeId"],)) is None:
                    raise ContractValidationError("active data job requires DME registration")
                persisted = dict(job)
                persisted["dataJobInfoStatus"] = "RUNNING"
                self.store.execute(
                    "INSERT INTO data_jobs(data_job_id,body_json,status) VALUES(?,?,?)",
                    (data_job_id, self.store.encode(persisted), "RUNNING"))
                return Response(200, {"outputs": {"ready": True}})
            if operation != "PROCESS_RESTART" or component != "NON_RT_RIC_FRAMEWORK":
                raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "operation is not owned by Non-RT")
            if self.restart_hook is not None:
                self.restart_hook(component)
            self.store = DurableStore(self.database_path)
            # Recovery is driven by the durable create retry after the
            # contract's authoritative GET/PUT reconciliation sequence.  An
            # eager PUT here races that sequence and can create a second owner.
            return Response(200, {"outputs": {"restartCompleted": True}})
        raise ServiceFailure(404, "AIC_RESOURCE_NOT_FOUND", "harness resource does not exist")

    @staticmethod
    def _versioned(status: int, body: Any = None, **headers: str) -> Response:
        values = {"Version": R1_VERSION, **headers}
        return Response(status, body, values)

    @staticmethod
    def _require_version(headers: Mapping[str, str], expected: str) -> None:
        if headers.get("version") != expected:
            raise ContractValidationError(f"Version header must be {expected}")

    @staticmethod
    def _error(status: int, code: str, detail: str, instance: str) -> Response:
        return Response(
            status,
            problem(code, status, detail, instance),
            {"Content-Type": "application/problem+json"},
        )
