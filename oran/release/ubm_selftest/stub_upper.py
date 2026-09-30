"""Stand-in upper process used as the falsifier target.

WHAT THIS IS NOT
----------------
This is **not** the ``upper-bilateral-mock/1.0.0`` runtime, it is never packaged
into a release archive, and a green run against it is **not** evidence that the
upper artifact conforms.  Every report produced by :mod:`.driver` records
``upperUnderTest`` so a stand-in run can never be mistaken for a runtime run.

WHY IT EXISTS
-------------
The falsifiers (``UBM-ST-M01``..``M04``) have to demonstrate that the lower
double really rejects a broken upper.  That demonstration needs *some* upper on
the other end of the socket while ``oran/release/ubm/**`` is being authored in
parallel.  The two transport falsifiers (M01, M04) mutate bytes on the wire via
:mod:`.proxy`, so they carry over to the release runtime unchanged.

The process binds exactly the four authorities the deployment vector assigns to
the upper, terminates real TLS on all of them, never binds ``a1.apiRoot``, and
reads no wall clock on the request path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import ssl
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contract_model import FrozenContract, jcs_sha256, sha256_bytes
from .wire import ServerReply, TlsHttpServer, authority_of, json_request, origin_of

STUB_IDENTITY = "SELFTEST_STUB_UPPER_NOT_RELEASE_RUNTIME"
_ZERO_OID = "0" * 40
_REDACTED = ("authorization", "proxy-authorization", "cookie", "set-cookie",
             "x-api-key", "x-auth-token")
_CREDENTIAL_NAME_TOKENS = ("token", "secret", "credential", "password", "api-key", "api_key")


def _redact(headers: Mapping[str, str]) -> tuple[list[str], str]:
    kept = {
        name.lower(): value for name, value in headers.items()
        if name.lower() not in _REDACTED
        and not any(token in name.lower() for token in _CREDENTIAL_NAME_TOKENS)
    }
    block = "".join("%s: %s\n" % (name, kept[name]) for name in sorted(kept))
    return sorted(kept), hashlib.sha256(block.encode("utf-8")).hexdigest()


def _canonicalise(raw: bytes, identifiers: Sequence[tuple[str, str]]) -> bytes:
    text = bytes(raw or b"")
    for value, token in identifiers:
        if value:
            text = text.replace(value.encode("utf-8"), token.encode("utf-8"))
    return text


def _digest(headers: Mapping[str, str], body: bytes,
            identifiers: Sequence[tuple[str, str]] = ()) -> dict[str, Any]:
    names, block = _redact(headers)
    record: dict[str, Any] = {
        "headerNamesPresent": names,
        "headerBlockSha256": block,
        "bodyByteCount": len(body),
        "bodyRawSha256": sha256_bytes(body),
        "contentType": headers.get("content-type"),
    }
    if body:
        try:
            record["bodyJcsSha256"] = jcs_sha256(json.loads(body.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    if identifiers:
        # Identifier-canonical companions, same contract as the release
        # runtime: the raw digest embeds a freshly minted identifier and could
        # only be excluded from a two-run comparison, so a canonical one is
        # published beside it and compared instead.
        canonical_headers = {
            name: _canonicalise(str(value).encode("utf-8"),
                                identifiers).decode("utf-8", "replace")
            for name, value in headers.items()
        }
        record["headerBlockCanonicalSha256"] = _redact(canonical_headers)[1]
        record["bodyCanonicalSha256"] = sha256_bytes(_canonicalise(body, identifiers))
    return record


class LogicalClock:
    """Logical time only; the request path never reads the wall clock."""

    def __init__(self, origin_iso: str) -> None:
        self.origin_iso = origin_iso
        self._ms = 0

    def tick(self) -> int:
        self._ms += 1
        return self._ms

    @property
    def now_ms(self) -> int:
        return self._ms


@dataclass
class _State:
    policies: dict[str, Any] = field(default_factory=dict)
    statuses: dict[str, Any] = field(default_factory=dict)
    data_jobs: dict[str, Any] = field(default_factory=dict)
    bindings: dict[str, Any] = field(default_factory=dict)
    accepted: dict[str, list[str]] = field(default_factory=dict)
    dme_registrations: dict[str, Any] = field(default_factory=dict)
    subscriptions: dict[str, Any] = field(default_factory=dict)
    status_subscription: Any = None
    capability_jcs: str | None = None
    o1_profile_loaded: bool = False
    perf_metric_job: Any = None
    process_intent_calls: int = 0
    status_callbacks_received: int = 0
    last_status_callback_status: int | None = None
    evidence_commits: int = 0
    normal_ran_writes: int = 0
    rollback_ran_writes: int = 0
    exchanges: list[dict[str, Any]] = field(default_factory=list)
    harness_operations: list[dict[str, Any]] = field(default_factory=list)
    scenario_id: str | None = None
    ledger: list[dict[str, Any]] = field(default_factory=list)
    coordinator: dict[str, Any] | None = None
    before_snapshot: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        body = {
            "policyCount": len(self.policies),
            "statusCount": len(self.statuses),
            "dmeDataJobCount": len(self.data_jobs),
            "deliveryBindingCount": len(self.bindings),
            "acceptedPushPayloadCounts": {
                key: len(value) for key, value in sorted(self.accepted.items())},
            "subscriptionCount": len(self.subscriptions),
            "evidenceCommitCount": self.evidence_commits,
            "policyIds": sorted(self.policies),
        }
        return dict(body, jcsSha256=jcs_sha256(body))

    def reset(self, scenario_id: str | None) -> None:
        self.__init__()  # type: ignore[misc]
        self.scenario_id = scenario_id


class StubUpper:
    def __init__(self, startup: Mapping[str, Any]) -> None:
        self.startup = dict(startup)
        self.vector_path = Path(self.startup["vectorPath"])
        self.vector_bytes = self.vector_path.read_bytes()
        self.vector = json.loads(self.vector_bytes.decode("utf-8"))
        self.vector_sha256 = sha256_bytes(self.vector_bytes)
        self.contract = FrozenContract(Path(self.startup["contractBundle"]))
        self.secret_map: dict[str, str] = dict(self.startup.get("secretMap", {}))
        self.state = _State()
        self.clock = LogicalClock(self.startup.get("logicalOrigin", "2026-08-04T00:00:00Z"))
        self.lock = threading.RLock()
        self.attempted_authorities: list[str] = []
        self._servers: list[TlsHttpServer] = []
        self._stop = threading.Event()
        self._capture_dir = Path(self.startup.get("stateDir", ".")) / "captures"

        reporting = self.vector["o1"]["fileDataReporting"]
        self.origins = {
            "nonrt": origin_of(self.vector["r1"]["apiRoot"]),
            "rapp": origin_of(self.vector["r1"]["callbackApi"]["rootUri"]),
            "o1_provider": origin_of(reporting["mnsRoot"]),
            "o1_consumer": origin_of(reporting["consumerReference"]),
        }
        self.lower_a1 = origin_of(self.vector["a1"]["apiRoot"])
        self.allowlist = sorted({authority_of(uri) for uri in self.origins.values()}
                                | {authority_of(self.lower_a1)})
        self.push_base_path = self._path_of(self.vector["r1"]["dme"]["policyEvidencePushBaseUri"])
        self.a1_status_path = self._path_of(self.vector["a1"]["statusCallbackRoot"])
        # Contract-literal expansion of #/endpointTemplates/o1NotificationRecipient.
        roots = self.contract.resolve_roots(self.vector)
        recipient = self.contract.endpoint_templates["o1NotificationRecipient"].replace(
            "{o1ConsumerRoot}", str(roots["o1ConsumerRoot"]))
        self.o1_notification_path = self._path_of(recipient)
        self.tls = self._tls_context()

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _path_of(uri: str) -> str:
        from urllib.parse import urlsplit
        return urlsplit(uri).path or "/"

    def _tls_context(self) -> ssl.SSLContext:
        certificate = self.secret_map[self.startup["tlsCertificateRef"]]
        private_key = self.secret_map[self.startup["tlsPrivateKeyRef"]]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certificate, private_key)
        return context

    def _client_context(self) -> ssl.SSLContext:
        truststore = self.secret_map[self.startup["tlsTruststoreRef"]]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(truststore)
        return context

    def _assigned_identifiers(self) -> Sequence[tuple[str, str]]:
        """Identifiers this run has assigned so far, longest first.

        Same contract as the release runtime: the canonical digests replace
        these values with role tokens so a two-run comparison can use them.
        """
        with self.lock:
            found = {value: "{ASSIGNED:policyId}" for value in self.state.policies}
            found.update({value: "{ASSIGNED:dataJobId}"
                          for value in self.state.data_jobs})
        return sorted(((value, token) for value, token in found.items()
                       if isinstance(value, str) and len(value) >= 8),
                      key=lambda item: len(item[0]), reverse=True)

    def _record(self, *, step_id: str, direction: str, role: str, peer: str, method: str,
                uri: str, request_headers: Mapping[str, str], request_body: bytes,
                status: int, response_headers: Mapping[str, str], response_body: bytes,
                endpoint_ref: str | None = None, bindings: Mapping[str, str] | None = None,
                captured: Mapping[str, Any] | None = None) -> None:
        response = _digest(response_headers, response_body,
                           self._assigned_identifiers())
        response["status"] = status
        location = response_headers.get("Location") or response_headers.get("location")
        response["locationHeader"] = location
        response["locationLastPathSegment"] = (
            [segment for segment in location.split("/") if segment][-1] if location else None)
        with self.lock:
            self.state.exchanges.append({
                "sequence": len(self.state.exchanges),
                "stepId": step_id,
                "stepIndex": len(self.state.exchanges),
                "logicalTimeMs": self.clock.tick(),
                "direction": direction,
                "role": role,
                "peer": peer,
                "transport": {"scheme": "https",
                              "peerAuthority": authority_of(uri),
                              "tlsProtocol": "TLSv1.3",
                              "peerAuthenticated": False},
                "method": method,
                "resolvedUri": uri,
                "endpointRef": endpoint_ref,
                "bindings": {key: str(value) for key, value in (bindings or {}).items()},
                "request": _digest(request_headers, request_body,
                                   self._assigned_identifiers()),
                "response": response,
                "attemptCount": 1,
                "retryPolicy": "NO_RETRY_DECLARED",
                "timeoutMs": int(self.vector["timeouts"]["defaultStepMs"]),
                "timedOut": False,
                "correlationId": None,
                "idempotencyKey": None,
                "capturedOutputs": dict(captured or {}),
                "declaredExpectedHttpStatus": None,
            })

    # -- servers -------------------------------------------------------
    def start(self) -> None:
        for component, origin in self.origins.items():
            authority = authority_of(origin)
            host, _, port = authority.rpartition(":")
            server = TlsHttpServer(
                host=host.strip("[]"), port=int(port), ssl_context=self.tls,
                handler=self._make_handler(component), name=component)
            server.start()
            self._servers.append(server)

    def stop(self) -> None:
        for server in self._servers:
            server.stop()
        self._servers = []
        self._stop.set()

    def wait(self) -> None:
        self._stop.wait()

    def _make_handler(self, component: str):
        def handler(method: str, path: str, headers: Mapping[str, str], body: bytes) -> ServerReply:
            clean = path.split("?", 1)[0]
            if clean.startswith("/ubm/v1/"):
                return self._control_plane(method, clean, body)
            if clean.startswith("/harness/"):
                return self._harness(component, method, clean, headers, body)
            if component == "nonrt":
                return self._nonrt(method, clean, headers, body)
            if component == "rapp":
                return self._rapp(method, clean, headers, body)
            if component == "o1_consumer":
                return self._o1_consumer(method, clean, headers, body)
            if component == "o1_provider":
                return self._o1_provider(method, clean, headers, body)
            return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})
        return handler

    # -- release control plane ------------------------------------------
    def _control_plane(self, method: str, path: str, body: bytes) -> ServerReply:
        if path == "/ubm/v1/readiness" and method == "GET":
            return ServerReply.json(200, {
                "ready": True,
                "upperUnderTest": STUB_IDENTITY,
                "components": {name: True for name in self.origins},
                "vectorSha256": self.vector_sha256,
                "releaseContentSha256": self.startup.get(
                    "upperReleaseContentSha256", "0" * 64),
                "placeholderFree": True,
                "o1NotificationPath": self.o1_notification_path,
            })
        if path == "/ubm/v1/export" and method == "POST":
            payload = json.loads(body or b"{}")
            return ServerReply.json(200, self.export(Path(payload.get("outDir", self._capture_dir))))
        if path == "/ubm/v1/stop" and method == "POST":
            threading.Thread(target=self.stop, daemon=True).start()
            return ServerReply.json(200, {"stopping": True})
        return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})

    # -- integration control surface ------------------------------------
    def _harness(self, component: str, method: str, path: str,
                 headers: Mapping[str, str], body: bytes) -> ServerReply:
        if path == "/harness/state" and method == "GET":
            with self.lock:
                self.state.harness_operations.append({
                    "sequence": len(self.state.harness_operations),
                    "logicalTimeMs": self.clock.tick(),
                    "component": component, "op": "STATE_READ", "allowed": True,
                    "argumentKeys": [], "httpStatus": 200, "event": "ICS_STATE_READ",
                })
                return ServerReply.json(200, {"state": self._component_state(component)})
        if path != "/harness/op" or method != "POST":
            return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})
        document = json.loads(body or b"{}")
        op = document.get("op")
        arguments = {key: value for key, value in document.items() if key != "op"}
        allowed = op in _ALLOWED_OPERATIONS.get(component, ())
        with self.lock:
            self.state.harness_operations.append({
                "sequence": len(self.state.harness_operations),
                "logicalTimeMs": self.clock.tick(),
                "component": component, "op": str(op), "allowed": allowed,
                "argumentKeys": sorted(arguments),
                "httpStatus": 200 if allowed else 404,
                "event": "ICS_OP" if allowed else "ICS_OP_NOT_ALLOWED",
            })
        if not allowed:
            return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})
        return ServerReply.json(200, {"outputs": self._operation(component, op, arguments)})

    def _component_state(self, component: str) -> dict[str, Any]:
        with self.lock:
            if component == "nonrt":
                return {"policyCount": len(self.state.policies),
                        "statusCount": len(self.state.statuses),
                        "dataJobCount": len(self.state.data_jobs),
                        "dmeRegistrationCount": len(self.state.dme_registrations)}
            if component == "rapp":
                return {"processIntentCalls": self.state.process_intent_calls,
                        "statusCallbacksReceived": self.state.status_callbacks_received,
                        "lastStatusCallbackStatus": self.state.last_status_callback_status,
                        "deliveryBindingCount": len(self.state.bindings),
                        "acceptedPushPayloads": sum(
                            len(value) for value in self.state.accepted.values())}
            if component == "o1_consumer":
                return {"subscriptionCount": len(self.state.subscriptions),
                        "profileLoaded": int(self.state.o1_profile_loaded),
                        "evidenceCommits": self.state.evidence_commits}
            return {"subscriptionCount": len(self.state.subscriptions)}

    def _operation(self, component: str, op: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        with self.lock:
            if op == "RESET_SCENARIO_STATE":
                if component == "nonrt":
                    self.state.reset(arguments.get("scenarioId"))
                    self.clock = LogicalClock(self.clock.origin_iso)
                    self.state.before_snapshot = self.state.snapshot()
                    # Scenario isolation covers observation counters too, or the
                    # order-permutation gate would see leakage.
                    self.attempted_authorities = []
                return {"resetCompleted": True}
            if op in {"START_R1_A1_SERVICE", "START_R1_DME_APIS"}:
                return {"started": True}
            if op == "INSTALL_R1_STATUS_SUBSCRIPTION":
                self.state.status_subscription = arguments.get("subscriptionId")
                self.state.subscriptions["r1-status"] = arguments.get("subscriptionId")
                return {"subscriptionId": arguments.get("subscriptionId")}
            if op == "INSTALL_DME_REGISTRATION":
                self.state.dme_registrations[arguments["dmeTypeId"]] = dict(arguments)
                return {"registrationId": arguments.get("registrationId")}
            if op == "INSTALL_ACTIVE_DATA_JOB":
                self.state.data_jobs[arguments["dataJobId"]] = arguments["job"]
                return {"dataJobId": arguments["dataJobId"]}
            if op == "R1_DME_BINDING_PRECREATE":
                self.state.bindings[arguments["deliveryBindingId"]] = {
                    "job": arguments.get("job"), "dataJobId": None}
                self.state.accepted.setdefault(arguments["deliveryBindingId"], [])
                return {"deliveryBindingId": arguments["deliveryBindingId"]}
            if op == "R1_DME_BINDING_COMMIT":
                binding = self.state.bindings.setdefault(
                    arguments["deliveryBindingId"], {"job": None, "dataJobId": None})
                binding["dataJobId"] = arguments["dataJobId"]
                return {"deliveryBindingId": arguments["deliveryBindingId"],
                        "dataJobId": arguments["dataJobId"]}
            if op == "LOAD_CAPABILITY":
                manifest = self.contract.fixture("fixture://capabilityManifest")
                expected = jcs_sha256(manifest)
                declared = arguments.get("manifestJcsSha256")
                if declared is not None and declared != expected:
                    raise ValueError("G-CAP-1: capability manifest digest mismatch")
                self.state.capability_jcs = expected
                return {"ready": True, "manifestJcsSha256": expected}
            if op == "LOAD_O1_PROFILE":
                self.state.o1_profile_loaded = True
                return {"profileLoaded": True}
            if op == "INSTALL_O1_SUBSCRIPTION" or op == "INSTALL_PROVIDER_SUBSCRIPTION":
                self.state.subscriptions[arguments["subscriptionId"]] = dict(arguments)
                return {"subscriptionId": arguments["subscriptionId"]}
            if op == "SET_PERF_METRIC_JOB":
                self.state.perf_metric_job = dict(arguments)
                return {"administrativeState": arguments.get("administrativeState"),
                        "operationalState": "ENABLED"}
            if op == "O1_NORMALIZE":
                records = [self.contract.fixture("fixture://afterEvidence"),
                           self.contract.fixture("fixture://afterEvidenceCell2")]
                # Same substitution the release runtime performs: the record's
                # correlation names the identifier THIS run assigned, never the
                # catalog literal (#/outputBindings/responseAssignedIdentifiers).
                assigned = next(iter(self.state.policies), None)
                if assigned is not None:
                    records = json.loads(json.dumps(records))
                    for record in records:
                        if isinstance(record.get("correlation"), dict):
                            record["correlation"]["policyId"] = assigned
                self.state.evidence_commits = 0
                return {"records": records, "commitEligibleRecords": len(records),
                        "rejectedRecords": 0, "duplicateRecords": 0,
                        "samples": sum(len(record.get("samples", [])) for record in records)}
            if op == "COORDINATOR_PROCESS_INTENT":
                self.state.process_intent_calls += 1
                self.state.normal_ran_writes = 1
                history = [{"from": "S%d" % index, "to": "S%d" % (index + 1),
                            "outcome": None, "origin": "REAL"} for index in range(6)]
                self.state.ledger.append({
                    "sequence": len(self.state.ledger), "kind": "NORMAL",
                    "simulated": True, "logicalTimeMs": self.clock.tick()})
                outputs = {
                    "processIntentCalls": 1,
                    "coordinatorExecutionMode": "REAL_PROCESS_INTENT",
                    "fsmHistory": history,
                    "terminalOutcome": "commit_original",
                    "terminalEvidenceRef": "evidence://%s" % uuid.uuid4(),
                    "ledgerReferences": ["ledger://intent", "ledger://policy",
                                         "ledger://action", "ledger://evidence"],
                    "profileError": None,
                    "routedTo": "OranIntentCoordinator",
                }
                self.state.coordinator = outputs
                return outputs
            raise ValueError("unsupported harness operation %r" % op)

    # -- O-RAN surfaces --------------------------------------------------
    def _nonrt(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> ServerReply:
        origin = self.origins["nonrt"]
        if path == "/a1-policy-management/v1/policies" and method == "POST":
            document = json.loads(body or b"{}")
            policy_id = str(uuid.uuid4())
            with self.lock:
                self.state.policies[policy_id] = document
            location = "%s/a1-policy-management/v1/policies/%s" % (origin, policy_id)
            reply = ServerReply.json(201, document, {"Location": location, "Version": "1.0.0"})
            self._record(step_id="r1-create", direction="UPPER_INBOUND", role="SERVER",
                         peer="LOWER_RUNNER", method=method, uri=origin + path,
                         request_headers=headers, request_body=body, status=201,
                         response_headers=reply.headers, response_body=reply.body,
                         endpoint_ref="#/endpointTemplates/r1Policies",
                         captured={"policyId": policy_id,
                                   "policyObjectJcsSha256": jcs_sha256(document["policyObject"])})
            self._reconcile_to_a1(document, policy_id)
            return reply
        prefix = "/a1-policy-management/v1/policies/"
        if path.startswith(prefix) and method == "GET":
            remainder = path[len(prefix):]
            policy_id = remainder[:-len("/status")] if remainder.endswith("/status") else remainder
            with self.lock:
                if policy_id not in self.state.policies:
                    return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})
                if remainder.endswith("/status"):
                    status = self.state.statuses.get(policy_id)
                    if status is None:
                        return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})
                    reply = ServerReply.json(200, status, {"Version": "1.0.0"})
                    self._record(step_id="r1-status-query", direction="UPPER_INBOUND",
                                 role="SERVER", peer="LOWER_RUNNER", method=method,
                                 uri=origin + path, request_headers=headers, request_body=body,
                                 status=200, response_headers=reply.headers,
                                 response_body=reply.body,
                                 endpoint_ref="#/endpointTemplates/r1PolicyStatus",
                                 bindings={"policyId": policy_id})
                    return reply
                return ServerReply.json(200, self.state.policies[policy_id], {"Version": "1.0.0"})
        if path == "/data-access/v2/data-jobs" and method == "POST":
            document = json.loads(body or b"{}")
            data_job_id = str(uuid.uuid4())
            stored = dict(document, dataJobInfoStatus="RUNNING")
            with self.lock:
                self.state.data_jobs[data_job_id] = stored
            location = "%s/data-access/v2/data-jobs/%s" % (origin, data_job_id)
            reply = ServerReply.json(201, stored,
                                     {"Location": location, "Version": "2.0.0-alpha.2"})
            self._record(step_id="create-dme-job", direction="UPPER_INBOUND", role="SERVER",
                         peer="LOWER_RUNNER", method=method, uri=origin + path,
                         request_headers=headers, request_body=body, status=201,
                         response_headers=reply.headers, response_body=reply.body,
                         endpoint_ref="#/endpointTemplates/r1DmeDataJobs",
                         captured={"dataJobId": data_job_id})
            return reply
        job_prefix = "/data-access/v2/data-jobs/"
        if path.startswith(job_prefix) and path.endswith("/status") and method == "GET":
            data_job_id = path[len(job_prefix):-len("/status")]
            with self.lock:
                job = self.state.data_jobs.get(data_job_id)
                if job is None:
                    return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})
                binding = str(job["pushDeliveryDetailsHttp"]["dataPushUri"]).rstrip("/").rsplit("/", 1)[-1]
                count = len(self.state.accepted.get(binding, []))
            reply = ServerReply.json(200, {"dataJobInfoStatus": "RUNNING",
                                           "acceptedPushPayloadCount": count},
                                     {"Version": "2.0.0-alpha.2"})
            self._record(step_id="dme-status-query", direction="UPPER_INBOUND", role="SERVER",
                         peer="LOWER_RUNNER", method=method, uri=origin + path,
                         request_headers=headers, request_body=body, status=200,
                         response_headers=reply.headers, response_body=reply.body,
                         endpoint_ref="#/endpointTemplates/r1DmeDataJobStatus",
                         bindings={"dataJobId": data_job_id})
            return reply
        return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})

    def _reconcile_to_a1(self, document: Mapping[str, Any], policy_id: str) -> None:
        """C1: really issue the A1 PUT, byte-identically."""
        policy_object = document["policyObject"]
        uri = "%s/A1-P/v2/policytypes/%s/policies/%s" % (
            self.lower_a1, document["policyTypeId"], policy_id)
        payload = json.dumps(policy_object, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        authority = authority_of(self.lower_a1)
        self.attempted_authorities.append(authority)
        response = json_request(
            origin=self.lower_a1, method="PUT",
            path=uri[len(self.lower_a1):], payload=policy_object,
            headers={"Content-Type": "application/json"},
            ssl_context=self._client_context(),
            timeout_ms=int(self.vector["timeouts"]["defaultStepMs"]))
        self._record(step_id="observe-a1-put", direction="UPPER_OUTBOUND", role="CLIENT",
                     peer="LOWER_A1", method="PUT", uri=uri,
                     request_headers={"content-type": "application/json"},
                     request_body=payload, status=response.status,
                     response_headers=response.headers, response_body=response.body,
                     endpoint_ref="#/endpointTemplates/a1Policy",
                     bindings={"policyTypeId": document["policyTypeId"], "policyId": policy_id},
                     captured={"policyObjectJcsSha256": jcs_sha256(policy_object)})

    def _rapp(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> ServerReply:
        origin = self.origins["rapp"]
        if path == "/r1/a1-policy-status" and method == "POST":
            with self.lock:
                self.state.status_callbacks_received += 1
                self.state.last_status_callback_status = 204
            reply = ServerReply(204)
            self._record(step_id="r1-status-to-rapp", direction="UPPER_INBOUND", role="SERVER",
                         peer="UPPER_SELF", method=method, uri=origin + path,
                         request_headers=headers, request_body=body, status=204,
                         response_headers={}, response_body=b"",
                         endpoint_ref="#/endpointTemplates/r1PolicyStatusDestination")
            return reply
        if path == self.a1_status_path and method == "POST":
            status = json.loads(body or b"{}")
            with self.lock:
                if not self.state.policies:
                    return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})
                policy_id = next(iter(self.state.policies))
                self.state.statuses[policy_id] = status
            self._record(step_id="a1-status-to-framework", direction="UPPER_INBOUND",
                         role="SERVER", peer="LOWER_RUNNER", method=method, uri=origin + path,
                         request_headers=headers, request_body=body, status=204,
                         response_headers={}, response_body=b"")
            self._relay_status(policy_id, status)
            return ServerReply(204)
        if path.startswith(self.push_base_path + "/") and method == "POST":
            binding = path[len(self.push_base_path) + 1:]
            if "/" in binding:
                return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})
            with self.lock:
                if binding not in self.state.bindings:
                    return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})
                document = json.loads(body or b"{}")
                if isinstance(document, list):
                    return ServerReply.json(400, {"code": "AIC_INVALID_PAYLOAD"})
                digest = jcs_sha256(document)
                accepted = self.state.accepted.setdefault(binding, [])
                if digest not in accepted:
                    accepted.append(digest)
                    self.state.evidence_commits += 1
            self._record(step_id="dme-push", direction="UPPER_INBOUND", role="SERVER",
                         peer="LOWER_RUNNER", method=method, uri=origin + path,
                         request_headers=headers, request_body=body, status=204,
                         response_headers={}, response_body=b"",
                         endpoint_ref="#/endpointTemplates/r1DmePushDestination",
                         bindings={"deliveryBindingId": binding})
            return ServerReply(204)
        return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})

    def _relay_status(self, policy_id: str, status: Mapping[str, Any]) -> None:
        """C3: the framework relays the status to the rApp callback (upper-internal)."""
        with self.lock:
            subscription = self.state.status_subscription
        if subscription is None:
            return
        origin = self.origins["rapp"]
        payload = {"subscriptionId": subscription,
                   "policyStates": [{"policyId": policy_id, "policyStatusObject": status}]}
        authority = authority_of(origin)
        self.attempted_authorities.append(authority)
        response = json_request(
            origin=origin, method="POST", path="/r1/a1-policy-status", payload=payload,
            headers={"Version": "1.0.0"}, ssl_context=self._client_context(),
            timeout_ms=int(self.vector["timeouts"]["defaultStepMs"]))
        if response.status != 204:  # fail closed, never silently ignored
            raise RuntimeError("R1 status relay returned %d" % response.status)

    def _o1_consumer(self, method: str, path: str, headers: Mapping[str, str],
                     body: bytes) -> ServerReply:
        if path == self.o1_notification_path and method == "POST":
            self._record(step_id="o1-notify", direction="UPPER_INBOUND", role="SERVER",
                         peer="LOWER_RUNNER", method=method,
                         uri=self.origins["o1_consumer"] + path,
                         request_headers=headers, request_body=body, status=204,
                         response_headers={}, response_body=b"",
                         endpoint_ref="#/endpointTemplates/o1NotificationRecipient")
            return ServerReply(204)
        return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})

    def _o1_provider(self, method: str, path: str, headers: Mapping[str, str],
                     body: bytes) -> ServerReply:
        return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})

    # -- capture ---------------------------------------------------------
    def capture_document(self) -> dict[str, Any]:
        with self.lock:
            scenario_id = self.state.scenario_id or "SC-083"
            model = self.contract.scenario(scenario_id)
            coordinator = self.state.coordinator
            after = self.state.snapshot()
            before = self.state.before_snapshot or after
            reporting = self.vector["o1"]["fileDataReporting"]
            return {
                "schemaVersion": "oran-aic-upper-bilateral-mock-capture/1.0.0",
                "captureId": str(uuid.uuid4()),
                "notAVerdict": True,
                "oracleOwnership":
                    "LOWER_FROZEN_RUNNER_51d73ca098743b25fe074d184904e695af37fd95",
                "run": {
                    "runId": self.startup.get("runId", "ubm-selftest"),
                    "scenarioId": scenario_id,
                    "startedAtLogical": self.clock.origin_iso,
                    "endedAtLogical": self.clock.origin_iso,
                    "sequenceHighWaterMark": len(self.state.exchanges),
                    "orderingRule": "ATMS_THEN_ARRAY_ORDER",
                    "stepIndexBase": 0,
                },
                "revisions": {
                    "upperReleaseId": "upper-bilateral-mock",
                    "upperReleaseVersion": "1.0.0",
                    "upperReleaseContentSha256": self.startup.get(
                        "upperReleaseContentSha256", "0" * 64),
                    "upperReleaseManifestSha256":
                        self.startup.get("upperReleaseManifestSha256", "0" * 64),
                    "upperSourceCommit": self.startup.get("upperSourceCommit", _ZERO_OID),
                    "upperSourceTree": self.startup.get("upperSourceTree", _ZERO_OID),
                    "testedCodeCommit": self.startup.get("testedCodeCommit", _ZERO_OID),
                    "lowerReleaseName": "lower-local-69-1.0.1-51d73ca",
                    "lowerReleaseCommit": "51d73ca098743b25fe074d184904e695af37fd95",
                    "lowerReleaseTree": "54ca268c08f03f2610c65c0ef5f0b25c3091d8ef",
                    "lowerReleaseArchiveSha256":
                        "3e3bdfbfd8552b356f31cd2f01c249d45ade3dc91d90642b1a8a3d021c2a8b2d",
                },
                "contract": {
                    "contractProfile": "oran-aic/1.0.1",
                    "correctedHandoff": "1.0.1",
                    "handoffManifestSha256":
                        "40c2a1b6a53058853da6f595eb7d28bfae0beb46be2b615a31c74d4476e49047",
                    "bundleManifestSha256":
                        "c01dfb46518af0e6f2687e073158ae3e408f199ecd09a1405b1f162c98d7c0b1",
                    "catalogSha256": self.contract.catalog_sha256,
                    "runnerContractSha256": self.contract.runner_contract_sha256,
                    "profileAssignmentSha256":
                        "3091b8afcad362c2dffaaa97acb1a551786ca8f2d41b70edef38fcb7ee9fde2c",
                    "deploymentVectorSchemaSha256":
                        "eb67fc5812914d50c249460616ad1152f610494e557b5f58b333204d51301948",
                },
                "deployment": {
                    "vectorVersion": "oran-aic-deployment-test-vector/1.0.0",
                    "vectorSha256": self.vector_sha256,
                    "basedOnLowerVectorSha256":
                        "288f2fbc120bf49064a0fc4439323b0d4752f583976b9f19972e8e80072f550f",
                    "bindingDocSha256": self.startup.get("bindingDocSha256", "0" * 64),
                    "placeholderFree": True,
                    "resolvedRoots": {
                        "r1ApiRoot": self.vector["r1"]["apiRoot"],
                        "rAppCallbackRoot": self.vector["r1"]["callbackApi"]["rootUri"],
                        "a1ApiRoot": self.vector["a1"]["apiRoot"],
                        "a1StatusCallbackRoot": self.vector["a1"]["statusCallbackRoot"],
                        "policyEvidencePushBaseUri":
                            self.vector["r1"]["dme"]["policyEvidencePushBaseUri"],
                        "mnsRoot": reporting["mnsRoot"],
                        "o1ConsumerRoot": reporting["consumerReference"],
                    },
                },
                "scenario": {
                    "scenarioId": scenario_id,
                    "catalogPointer": model.catalog_pointer,
                    "expectedPointer": model.expected_pointer,
                    "rulesPointer": model.rules_pointer,
                    "fixtureMode": "EXACT_BUNDLE",
                    "executionProfile": "bilateral-mock",
                    "counterpartProvisioning": "UPPER_SUPPLIED_AGREED_MOCK",
                    "declaredFaultCount": 0,
                },
                "logicalClock": {
                    "mode": "FIXED_LOGICAL",
                    "origin": self.clock.origin_iso,
                    "evaluationNow": self.clock.origin_iso,
                    "wallClockReadsOnRequestPath": 0,
                    "hiddenSleepCount": 0,
                },
                "exchanges": list(self.state.exchanges),
                "harnessOperations": [
                    dict(entry, op="STATE_READ" if entry["op"] == "STATE_READ" else entry["op"])
                    for entry in self.state.harness_operations
                ],
                "state": {"before": before, "after": after, "diffPointers": []},
                "coordinator": {
                    "processIntentCallsBefore": 0,
                    "processIntentCallsAfter": self.state.process_intent_calls,
                    "processIntentCallsDelta": self.state.process_intent_calls,
                    "executionMode": "REAL_PROCESS_INTENT" if coordinator else "NOT_INVOKED",
                    "fsmHistory": coordinator["fsmHistory"] if coordinator else [],
                    "terminalOutcome": coordinator["terminalOutcome"] if coordinator else None,
                    "terminalOutcomeCount": 1 if coordinator else 0,
                    "terminalEvidenceRef": coordinator["terminalEvidenceRef"] if coordinator else None,
                    "ledgerReferences": coordinator["ledgerReferences"] if coordinator else [],
                    "requiresRealCoordinatorExecution": bool(
                        model.expected.get("requiresRealCoordinatorExecution", False)),
                },
                "simulatedWrites": {
                    "normalRanWrites": self.state.normal_ran_writes,
                    "rollbackRanWrites": self.state.rollback_ran_writes,
                    "ledger": list(self.state.ledger),
                },
                # The stand-in carries NO egress guard -- that lives in the
                # release runtime, which this package may not import.  It says
                # so rather than copying the release's numbers: a capture from
                # the stand-in must be distinguishable from a measured one, and
                # `guardInstalled: false` is what makes the difference visible
                # to anyone comparing the two documents.
                "externalCalls": {
                    "externalLiveTargetCalls": 0,
                    "hardwareCalls": 0,
                    "attemptedAuthorities": sorted(set(self.attempted_authorities)),
                    "authorityAllowlist": self.allowlist,
                    "guardInstalled": False,
                    "guardArmedNow": False,
                    "scope": "scenario",
                    "guardMethod": "in-package stand-in; it runs no egress guard, "
                                   "so these counters are not measurements",
                    "hardwareDefinitionSource": "not declared by the stand-in",
                    "connectionAttempts": 0,
                    "approvedConnectionAttempts": 0,
                    "violations": [],
                    "liveOutboundConnections": 0,
                    "peerAttributionAmbiguities": [],
                },
                "appliedRules": [
                    {"ruleId": rule, "declaredInCatalog": True,
                     "inputs": ["scenario.rules"], "observations": {}}
                    for rule in model.rules
                ],
                "redaction": {
                    "policy": "oran-aic-upper-bilateral-mock-redaction/1.0.0",
                    "redactedHeaderNames": [],
                    "redactedBodyPointers": [],
                    "secretReferencesObserved": sorted(self.secret_map),
                    "secretValuesCaptured": False,
                    "credentialMaterialCaptured": False,
                },
            }

    def export(self, out_dir: Path) -> dict[str, str]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        document = self.capture_document()
        raw = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        target = out_dir / ("capture-%s.json" % (self.state.scenario_id or "unknown"))
        target.write_bytes(raw)
        return {"capturePath": str(target), "captureSha256": sha256_bytes(raw)}


_ALLOWED_OPERATIONS = {
    "nonrt": frozenset({"RESET_SCENARIO_STATE", "START_R1_A1_SERVICE", "START_R1_DME_APIS",
                        "INSTALL_R1_STATUS_SUBSCRIPTION", "INSTALL_DME_REGISTRATION",
                        "INSTALL_ACTIVE_DATA_JOB"}),
    "rapp": frozenset({"RESET_SCENARIO_STATE", "R1_DME_BINDING_PRECREATE",
                       "R1_DME_BINDING_COMMIT", "COORDINATOR_PROCESS_INTENT"}),
    "o1_consumer": frozenset({"RESET_SCENARIO_STATE", "LOAD_O1_PROFILE", "LOAD_CAPABILITY",
                              "INSTALL_O1_SUBSCRIPTION", "SET_PERF_METRIC_JOB", "O1_NORMALIZE"}),
    "o1_provider": frozenset({"RESET_SCENARIO_STATE", "INSTALL_PROVIDER_SUBSCRIPTION"}),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=STUB_IDENTITY)
    parser.add_argument("--startup", required=True)
    arguments = parser.parse_args(argv)
    startup = json.loads(Path(arguments.startup).read_text("utf-8"))
    stub = StubUpper(startup)
    stub.start()

    def _terminate(*_: Any) -> None:
        stub.stop()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    try:
        stub.wait()
    finally:
        stub.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
