"""Contract-faithful lower test double.

The double stands in for the lower frozen runner **and** for the lower-owned
Near-RT A1-P producer / xApp / E2 stub.  It is driven exclusively from the bytes
of ``scenario-catalog.1.0.1.json`` and ``scenario-runner-contract.1.0.1.json``:

* every route it serves is expanded from ``#/endpointTemplates`` with root
  ``{a1ApiRoot}``; anything else is counted as an unknown route and answered
  ``404`` (UBM-ST-D01);
* every step operation and every initial state is looked up in the runner
  contract; an undeclared name raises instead of being skipped (UBM-ST-C01);
* the double never reads the upper's internal state.  The one exchange that is
  invisible on the wire (``r1-status-to-rapp``, upper client *and* upper server)
  is taken from the declared Integration Control Surface state read and is
  labelled with its provenance in the observations.

Crucially, the A1 status the double returns for ``query-status`` is **derived**
from the policy body it actually received plus the bundle capability fixture --
it is never copied from the step's ``assertBody``.  Copying it would make the
oracle self-fulfilling, which is precisely the circularity this suite exists to
rule out.
"""

from __future__ import annotations

import json
import re
import ssl
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import contract_model as cm
from .contract_model import (
    ExpressionScope,
    FrozenContract,
    RouteTable,
    ScenarioModel,
    UnknownRouteError,
    apply_json_patch,
    expand_endpoint,
    jcs_sha256,
    json_pointer,
    resolve_expressions,
    sha256_bytes,
)
from .mutations import M02_DROP_ONE_DME_PUSH, M03_PREDICT_POLICY_ID
from .wire import ServerReply, TlsHttpServer, authority_of, json_request, origin_of, request

_COMPONENT_ORIGIN_ATTRIBUTE = {
    "nonrt": "r1",
    "rapp": "rapp",
    "o1_provider": "o1_provider",
    "o1_consumer": "o1_consumer",
}

_LOCAL_COMPONENT = "lower"

#: The runner-contract operations the lower side owns end-to-end; none of them
#: touches the upper.  Derived from ``#/operations/<op>/kind`` plus the fact that
#: the Near-RT/E2 side is ``LOWER_OWNED_IMPLEMENTATION_UNDER_TEST``.
_LOWER_LOCAL_OPS = frozenset({
    "KPM_SNAPSHOT", "E2_CONTROL_RESULT", "READBACK", "E2_STUB_LOG",
    "DECISION_RESULT", "E2_INVENTORY_VALIDATE", "O1_RETRIEVE",
})


_PLACEHOLDER_RE = re.compile(
    r"\$\{|<[A-Z_]+>|PLACEHOLDER|TODO|CHANGEME|example\.(com|test|invalid)")

#: The seven vector fields the frozen schema constrains with ``$defs.httpsUri``.
_HTTPS_FIELDS = (
    ("r1", "apiRoot"),
    ("r1", "callbackApi", "rootUri"),
    ("r1", "dme", "policyEvidencePushBaseUri"),
    ("a1", "apiRoot"),
    ("a1", "statusCallbackRoot"),
    ("o1", "fileDataReporting", "mnsRoot"),
    ("o1", "fileDataReporting", "consumerReference"),
)


class PreflightError(RuntimeError):
    """``preflight.mustCompleteBeforeAnyNetworkSideEffect`` was not satisfied."""


class StepFailure(AssertionError):
    """A declared expectation was not met; the scenario fails deterministically."""


@dataclass(frozen=True)
class LowerDoubleResult:
    scenario_id: str
    http_sequence: tuple[int, ...]
    observations: Mapping[str, Any]
    unknown_routes: int


@dataclass
class _A1State:
    """Lower-owned Near-RT state: A1-P resources plus the E2 stub boundary log."""

    policy_types: dict[str, dict[str, Any]] = field(default_factory=dict)
    policies: dict[tuple[str, str], Any] = field(default_factory=dict)
    statuses: dict[tuple[str, str], Any] = field(default_factory=dict)
    observed: list[dict[str, Any]] = field(default_factory=list)
    e2_log: dict[str, int] = field(default_factory=lambda: {
        "controlRequests": 0, "normalEffects": 0, "rollbackEffects": 0})
    dependencies_ready: bool = False
    capability: Any = None
    unknown_routes: int = 0
    served: list[dict[str, Any]] = field(default_factory=list)

    def reset(self) -> None:
        self.policy_types.clear()
        self.policies.clear()
        self.statuses.clear()
        self.observed.clear()
        self.served.clear()
        self.e2_log = {"controlRequests": 0, "normalEffects": 0, "rollbackEffects": 0}
        self.dependencies_ready = False
        self.capability = None


def _json_dumps_canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dotted_get(document: Any, dotted: str) -> Any:
    current = document
    for token in dotted.split("."):
        if not isinstance(current, Mapping) or token not in current:
            raise StepFailure("assertBody path %r is absent from the response body" % dotted)
        current = current[token]
    return current


class ContractFaithfulLowerDouble:
    """Frozen interface per ``work-split.1.0.0.json#/frozenInterfaces``."""

    def __init__(self, *, catalog_path: Path, runner_contract_path: Path,
                 vector: Mapping[str, Any], ssl_context: ssl.SSLContext,
                 upper: Any,
                 client_ssl_context: ssl.SSLContext | None = None,
                 integration_control_surface_path: Path | None = None,
                 connect_overrides: Mapping[str, str] | None = None,
                 listen_authority: str | None = None,
                 driver_mutations: Sequence[str] = (),
                 observation_deadline_ms: int | None = None) -> None:
        catalog_path = Path(catalog_path)
        runner_contract_path = Path(runner_contract_path)
        if catalog_path.parent != runner_contract_path.parent:
            raise cm.ContractFaithfulnessError(
                "catalog and runner contract must come from the same frozen bundle")
        self.contract = FrozenContract(catalog_path.parent)
        self.vector = json.loads(json.dumps(vector))
        self.upper = upper
        self._server_context = ssl_context
        self._client_context = client_ssl_context
        self._connect_overrides = dict(connect_overrides or {})
        self._driver_mutations = frozenset(driver_mutations)
        # Contract default: deployment.timeouts.defaultStepMs.  A caller may
        # shorten it, and the self-test report records the value it used.
        self._deadline_ms = observation_deadline_ms
        self._ics_spec = json.loads(Path(integration_control_surface_path).read_text("utf-8")) \
            if integration_control_surface_path else None
        if self._deadline_ms is None:
            self._deadline_ms = int(self.vector["timeouts"]["defaultStepMs"])
        self.roots = self.contract.resolve_roots(self.vector)
        self._route_table = RouteTable(
            self.contract.endpoint_templates, "a1ApiRoot", str(self.roots["a1ApiRoot"]))
        self._state = _A1State()
        self._lock = threading.Lock()
        self._server: TlsHttpServer | None = None
        listen = listen_authority or authority_of(self.vector["a1"]["apiRoot"])
        host, _, port = listen.rpartition(":")
        self._listen_host = host.strip("[]")
        self._listen_port = int(port)
        self._upper_404s: list[str] = []
        self._ics_state_reads: list[str] = []
        self.preflight_result: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def preflight(self) -> dict[str, Any]:
        """Run the declared checks before any network side effect.

        Implements the head of
        ``scenario-runner-contract.1.0.1.json#/preflight/checksInOrder``:
        parse the machine artifacts, validate the deployment vector against the
        **frozen, unshadowed** Draft 2020-12 schema, and refuse a placeholder or
        a plaintext endpoint.  Failure raises before a socket is bound, so the
        disposition can never be an inferred success.
        """
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        bundle = self.contract.bundle_dir
        schema_name = self.contract.runner["deploymentVectorSchemaRef"]

        def _retrieve(uri: str) -> Any:
            target = bundle / uri
            if not target.exists():
                raise LookupError(uri)
            return Resource.from_contents(
                json.loads(target.read_text("utf-8")), default_specification=DRAFT202012)

        schema_path = bundle / schema_name
        schema = json.loads(schema_path.read_text("utf-8"))
        validator = Draft202012Validator(
            schema, format_checker=FormatChecker(), registry=Registry(retrieve=_retrieve))
        errors = ["%s: %s" % ("/".join(str(part) for part in error.path), error.message)
                  for error in validator.iter_errors(self.vector)]
        if errors:
            raise PreflightError(
                "AIC_RUNNER deployment vector failed the frozen schema: %s" % errors)

        rendered = json.dumps(self.vector, ensure_ascii=False)
        placeholder = _PLACEHOLDER_RE.search(rendered)
        if placeholder is not None:
            raise PreflightError("deployment vector still carries %r" % placeholder.group(0))

        for path in _HTTPS_FIELDS:
            value = self.vector
            for token in path:
                value = value[token]
            if not str(value).startswith("https://"):
                raise PreflightError("%s is not an https endpoint" % "/".join(path))
        return {
            "vectorSha256": sha256_bytes(json.dumps(
                self.vector, ensure_ascii=False, sort_keys=True).encode("utf-8")),
            "schema": schema_name,
            "schemaSha256": cm.sha256_file(schema_path),
            "shadowUsed": False,
            "placeholderFree": True,
        }

    def start(self) -> None:
        self.preflight_result = self.preflight()
        self._server = TlsHttpServer(
            host=self._listen_host, port=self._listen_port,
            ssl_context=self._server_context, handler=self._serve, name="lower-a1")
        self._server.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None

    @property
    def unknown_route_count(self) -> int:
        return self._state.unknown_routes

    @property
    def listen_authority(self) -> str:
        return "%s:%d" % (self._listen_host, self._listen_port)

    @property
    def served_route_names(self) -> tuple[str, ...]:
        return self._route_table.template_names()

    # ------------------------------------------------------------------
    # A1 listener  (lower-owned implementation under test, stood in for here)
    # ------------------------------------------------------------------
    def _serve(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> ServerReply:
        request_path = path.split("?", 1)[0]
        try:
            matched = self._route_table.match(request_path)
        except UnknownRouteError:
            with self._lock:
                self._state.unknown_routes += 1
                self._state.served.append(
                    {"method": method, "path": request_path, "status": 404, "known": False})
            return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})
        reply = self._serve_known(method, matched, headers, body)
        with self._lock:
            self._state.served.append({
                "method": method, "path": request_path, "status": reply.status,
                "template": matched.template_name, "known": True,
            })
        return reply

    def _serve_known(self, method: str, matched: Any, headers: Mapping[str, str],
                     body: bytes) -> ServerReply:
        name = matched.template_name
        bindings = matched.bindings
        if name == "a1PolicyType" and method in {"PUT", "GET"}:
            policy_type_id = bindings["policyTypeId"]
            if method == "PUT":
                with self._lock:
                    self._state.policy_types[policy_type_id] = json.loads(body or b"{}")
                return ServerReply.json(201, {"policyTypeId": policy_type_id})
            with self._lock:
                if policy_type_id not in self._state.policy_types:
                    return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})
                return ServerReply.json(200, self._state.policy_types[policy_type_id])
        if name == "a1Policy":
            return self._serve_policy(method, bindings, headers, body)
        if name == "a1PolicyStatus" and method == "GET":
            key = (bindings["policyTypeId"], bindings["policyId"])
            with self._lock:
                if key not in self._state.statuses:
                    return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})
                return ServerReply.json(200, self._state.statuses[key])
        if name == "a1Policies" and method == "GET":
            with self._lock:
                policy_type_id = bindings["policyTypeId"]
                return ServerReply.json(200, [
                    key[1] for key in self._state.policies if key[0] == policy_type_id])
        if name == "a1PolicyTypes" and method == "GET":
            with self._lock:
                return ServerReply.json(200, sorted(self._state.policy_types))
        return ServerReply.json(405, {"code": "AIC_RESOURCE_NOT_FOUND"})

    def _serve_policy(self, method: str, bindings: Mapping[str, str],
                      headers: Mapping[str, str], body: bytes) -> ServerReply:
        key = (bindings["policyTypeId"], bindings["policyId"])
        if method == "GET":
            with self._lock:
                if key not in self._state.policies:
                    return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})
                return ServerReply.json(200, self._state.policies[key])
        if method != "PUT":
            return ServerReply.json(405, {"code": "AIC_RESOURCE_NOT_FOUND"})
        with self._lock:
            if key[0] not in self._state.policy_types:
                return ServerReply.json(404, {"code": "AIC_RESOURCE_NOT_FOUND"})
            created = key not in self._state.policies
            parsed: Any
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            self._state.policies[key] = parsed
            self._state.observed.append({
                "method": "PUT",
                "policyTypeId": key[0],
                "policyId": key[1],
                "rawSha256": sha256_bytes(body),
                "raw": body,
                "body": parsed,
                "status": 201 if created else 200,
                "headerNames": sorted(headers),
            })
            status = self._derive_status(key, parsed)
            self._state.statuses[key] = status
        return ServerReply.json(201 if created else 200, {"policyId": key[1]})

    def _derive_status(self, key: tuple[str, str], policy_object: Any) -> dict[str, Any]:
        """Admission derived from the received body + the bundle capability fixture.

        Never derived from the scenario's ``assertBody``: that would make the
        oracle prove itself.
        """
        enforce_reason: str | None = None
        error_code: str | None = None
        if not isinstance(policy_object, Mapping):
            enforce_reason, error_code = "STATEMENT_NOT_APPLICABLE", "AIC_CELL_NOT_ALLOWED"
        else:
            capability = self._state.capability or {}
            known_cells = {
                jcs_sha256(cell["cellId"])
                for cell in (capability.get("topology", {}) or {}).get("cells", [])
            }
            deployment_ue = self.vector["topology"]["ueId"]
            scope_ue = (policy_object.get("scope") or {}).get("ueId")
            if scope_ue != deployment_ue:
                enforce_reason, error_code = "SCOPE_NOT_APPLICABLE", "AIC_SCOPE_NOT_FOUND"
            else:
                envelope = (policy_object.get("steeringObjective") or {}).get("actionEnvelope") or {}
                for cell in envelope.get("allowedCells", []):
                    if jcs_sha256(cell) not in known_cells:
                        enforce_reason, error_code = (
                            "STATEMENT_NOT_APPLICABLE", "AIC_CELL_NOT_ALLOWED")
                        break
        if enforce_reason is None:
            return {
                "enforceStatus": "ENFORCED",
                "aicStatus": {"policyId": key[1], "policyState": "ACTIVE"},
            }
        return {
            "enforceStatus": "NOT_ENFORCED",
            "enforceReason": enforce_reason,
            "aicStatus": {
                "policyId": key[1],
                "policyState": "NOT_ENFORCED",
                "error": {"code": error_code},
            },
        }

    # ------------------------------------------------------------------
    # upper-facing helpers
    # ------------------------------------------------------------------
    def _origin_for(self, uri: str) -> str:
        return origin_of(uri)

    def _connect_authority(self, origin: str) -> str:
        return self._connect_overrides.get(origin, authority_of(origin))

    def _client(self) -> ssl.SSLContext:
        if self._client_context is None:
            raise StepFailure("no TLS client context was supplied to the lower double")
        return self._client_context

    def _call(self, *, method: str, uri: str, payload: Any = None,
              headers: Mapping[str, str] | None = None,
              raw_body: bytes | None = None) -> Any:
        origin = self._origin_for(uri)
        path = uri[len(origin):] or "/"
        common = {
            "origin": origin, "method": method, "path": path,
            "ssl_context": self._client(),
            "connect_authority": self._connect_authority(origin),
            "timeout_ms": int(self.vector["timeouts"]["defaultStepMs"]),
        }
        if raw_body is not None:
            merged = dict(headers or {})
            merged.setdefault("Content-Type", "application/json")
            response = request(headers=merged, body=raw_body, **common)
        else:
            response = json_request(payload=payload, headers=headers, **common)
        if response.status == 404 and origin in self.upper.upper_origins():
            self._upper_404s.append("%s %s" % (method, uri))
        return response

    def _component_origin(self, component: str) -> str:
        return getattr(self.upper, _COMPONENT_ORIGIN_ATTRIBUTE[component])

    def _ics(self, component: str, op: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        origin = self._component_origin(component)
        response = json_request(
            origin=origin, method="POST", path="/harness/op",
            payload={"op": op, **dict(arguments)},
            ssl_context=self._client(), connect_authority=self._connect_authority(origin),
            timeout_ms=int(self.vector["timeouts"]["defaultStepMs"]))
        if response.status != 200:
            raise StepFailure(
                "integration control surface rejected %s on %s: HTTP %d"
                % (op, component, response.status))
        document = response.json() or {}
        if "outputs" not in document:
            raise StepFailure("harness response for %s carries no outputs member" % op)
        return dict(document["outputs"])

    def _ics_state(self, component: str) -> dict[str, Any]:
        origin = self._component_origin(component)
        response = json_request(
            origin=origin, method="GET", path="/harness/state",
            ssl_context=self._client(), connect_authority=self._connect_authority(origin),
            timeout_ms=int(self.vector["timeouts"]["defaultStepMs"]))
        if response.status != 200:
            raise StepFailure("harness state read failed on %s: HTTP %d" % (component, response.status))
        self._ics_state_reads.append(component)
        document = response.json() or {}
        return dict(document.get("state", document))

    # ------------------------------------------------------------------
    # scenario execution
    # ------------------------------------------------------------------
    def run_scenario(self, scenario_id: str) -> LowerDoubleResult:
        model = self.contract.scenario(scenario_id)
        self._upper_404s = []
        self._ics_state_reads = []
        failures: list[str] = []
        http_sequence: list[int] = []
        normalized: list[dict[str, Any]] = []
        outputs: dict[str, dict[str, Any]] = {}
        counters_at_start: dict[str, Any] = {}
        try:
            counters_at_start = self._reset(model)
            self._seed(model)
            for index, step in enumerate(model.steps):
                self._run_step(model, index, step, outputs, http_sequence, normalized)
        except StepFailure as failure:
            failures.append(str(failure))
        except cm.ContractFaithfulnessError as failure:
            failures.append("CONTRACT_FAITHFULNESS: %s" % failure)

        expected_sequence = tuple(model.expected.get("httpSequence") or ())
        if not failures and tuple(http_sequence) != expected_sequence:
            failures.append(
                "httpSequence %s does not equal expected.httpSequence %s"
                % (list(http_sequence), list(expected_sequence)))
        observations: dict[str, Any] = {
            "disposition": "PASS" if not failures else "FAIL",
            "failures": failures,
            "expectedHttpSequence": list(expected_sequence),
            "normalized": normalized,
            "capturedOutputs": {
                step_id: {
                    key: value for key, value in values.items()
                    if not isinstance(value, (bytes, bytearray))
                }
                for step_id, values in outputs.items()
            },
            "e2StubLog": dict(self._state.e2_log),
            "observedA1Puts": len(self._state.observed),
            "servedRoutes": [dict(entry) for entry in self._state.served],
            "upper404s": list(self._upper_404s),
            "unknownRoutes": self._state.unknown_routes,
            "countersAtScenarioStart": counters_at_start,
            "icsStateReads": list(self._ics_state_reads),
            "preflight": dict(self.preflight_result or {}),
            "driverMutations": sorted(self._driver_mutations),
            "oracleOwnership": (
                "LOWER_FROZEN_RUNNER_51d73ca098743b25fe074d184904e695af37fd95"),
            "dispositionScope": (
                "UPPER_ARTIFACT_SELF_TEST only; not bilateral acceptance"),
        }
        return LowerDoubleResult(
            scenario_id=scenario_id,
            http_sequence=tuple(http_sequence),
            observations=observations,
            unknown_routes=self._state.unknown_routes,
        )

    # -- reset / seed --------------------------------------------------
    def _reset(self, model: ScenarioModel) -> dict[str, Any]:
        with self._lock:
            self._state.reset()
            self._state.unknown_routes = 0
        counters: dict[str, Any] = {}
        for component in _COMPONENT_ORIGIN_ATTRIBUTE:
            self._ics(component, "RESET_SCENARIO_STATE", {"scenarioId": model.scenario_id})
        for component in _COMPONENT_ORIGIN_ATTRIBUTE:
            state = self._ics_state(component)
            counters[component] = {
                key: value for key, value in sorted(state.items())
                if isinstance(value, int)
            }
            non_zero = {key: value for key, value in counters[component].items() if value != 0}
            if non_zero:
                raise StepFailure(
                    "state leaked into %s at scenario start: %s" % (component, non_zero))
        return counters

    def _seed_plan(self, model: ScenarioModel) -> list[tuple[str, str, str, tuple[str, ...]]]:
        """(token, adapterAction, component, fanout) with the frozen bytes as authority."""
        plan: list[tuple[str, str, str, tuple[str, ...]]] = []
        spec_entries = {}
        if self._ics_spec is not None:
            source = self._ics_spec.get("operationSourceMap", {}).get(model.scenario_id, {})
            spec_entries = {entry["token"]: entry for entry in source.get("initialState", [])}
        for token in model.initial_state:
            action = self.contract.adapter_action(token)
            entry = spec_entries.get(token)
            if entry is not None and entry.get("adapterAction") != action:
                raise cm.ContractFaithfulnessError(
                    "integration-control-surface spec claims adapterAction %r for %s but the "
                    "frozen runner contract declares %r"
                    % (entry.get("adapterAction"), token, action))
            component = entry.get("component") if entry else self._component_for(action)
            fanout = tuple(entry.get("fanout", ())) if entry else ()
            plan.append((token, action, component or _LOCAL_COMPONENT, fanout))
        return plan

    def _component_for(self, action: str) -> str:
        if self._ics_spec is None:
            return _LOCAL_COMPONENT
        allowed = self._ics_spec.get("allowedOperations", {})
        owners = [name for name, ops in allowed.items() if action in ops]
        if len(owners) == 1:
            return owners[0]
        return _LOCAL_COMPONENT

    def _seed(self, model: ScenarioModel) -> None:
        scope = ExpressionScope(
            deployment=self.vector, constants=self.contract.constants, steps={})
        standard = self.contract.catalog["standardInitialStates"]
        for token, action, component, fanout in self._seed_plan(model):
            declared = resolve_expressions(standard.get(token, {}), scope)
            arguments = self._seed_arguments(action, declared, scope)
            if component == _LOCAL_COMPONENT:
                self._local_seed(action, arguments)
                continue
            self._ics(component, action, arguments)
            for item in fanout:
                target, _, fan_op = item.partition(":")
                self._ics(target, fan_op, self._fanout_arguments(fan_op, arguments))

    def _seed_arguments(self, action: str, declared: Mapping[str, Any],
                        scope: ExpressionScope) -> dict[str, Any]:
        vector = self.vector
        if action == "LOAD_O1_PROFILE":
            return {"profileRef": "fixture://o1Profile",
                    "netconfProfileRef": "fixture://o1NetconfYangProfile"}
        if action == "LOAD_CAPABILITY":
            manifest = self.contract.fixture("fixture://capabilityManifest")
            self._state.capability = manifest
            return {"manifestRef": "fixture://capabilityManifest",
                    "manifestJcsSha256": jcs_sha256(manifest)}
        if action == "INSTALL_DME_REGISTRATION":
            registration = self.contract.runner["r1WireContracts"]["dmeRegistration"]
            return {
                "dmeTypeId": "aic:policy-evidence:1.0.0",
                "registrationId": self.contract.constants["dmeRegistrationId"],
                "dataAccessEndpoint": vector["r1"]["dme"]["dataAccessEndpoint"],
                "deliverySchemaId": registration["deliverySchema"]["deliverySchemaId"],
                "dataDeliveryMethod": registration["deliveryMechanism"]["dataDeliveryMethod"],
            }
        if action == "INSTALL_ACTIVE_DATA_JOB":
            return {
                "dataJobId": declared["dataJobId"],
                "deliveryBindingId": declared["deliveryBindingId"],
                "durableBindingToDataJobId": declared.get("durableBindingToDataJobId", True),
                "job": declared["dataJobInfo"],
            }
        if action == "START_R1_A1_SERVICE":
            return {"apiRoot": vector["r1"]["apiRoot"]}
        if action == "START_R1_DME_APIS":
            return {"apiRoot": vector["r1"]["apiRoot"]}
        if action == "INSTALL_R1_STATUS_SUBSCRIPTION":
            return {"subscriptionId": vector["r1"]["statusSubscriptionId"],
                    "statusDestination": self.contract.endpoint_templates[
                        "r1PolicyStatusDestination"].replace(
                            "{rAppCallbackRoot}", str(self.roots["rAppCallbackRoot"]))}
        if action == "INSTALL_O1_SUBSCRIPTION":
            reporting = vector["o1"]["fileDataReporting"]
            return {"subscriptionId": reporting["subscriptionId"],
                    "consumerReference": reporting["consumerReference"]}
        if action == "SET_PERF_METRIC_JOB":
            return {"jobId": vector["o1"]["perfMetricJob"]["jobId"],
                    "administrativeState": "UNLOCKED"}
        if action == "A1_INSTALL_POLICY_TYPE":
            return {"policyTypeId": self.contract.constants["policyTypeId"]}
        if action == "SET_DEPENDENCIES":
            return {"ready": True}
        raise cm.UnknownOperationError(
            "no argument derivation is declared for adapter action %r" % action)

    def _fanout_arguments(self, op: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if op == "R1_DME_BINDING_PRECREATE":
            return {"deliveryBindingId": arguments["deliveryBindingId"], "job": arguments["job"]}
        if op == "R1_DME_BINDING_COMMIT":
            return {"deliveryBindingId": arguments["deliveryBindingId"],
                    "dataJobId": arguments["dataJobId"]}
        if op == "INSTALL_PROVIDER_SUBSCRIPTION":
            return {"subscriptionId": arguments["subscriptionId"],
                    "consumerReference": arguments["consumerReference"]}
        raise cm.UnknownOperationError("no fanout derivation for %r" % op)

    def _local_seed(self, action: str, arguments: Mapping[str, Any]) -> None:
        with self._lock:
            if action == "A1_INSTALL_POLICY_TYPE":
                self._state.policy_types[arguments["policyTypeId"]] = {
                    "policyTypeId": arguments["policyTypeId"]}
            elif action == "SET_DEPENDENCIES":
                self._state.dependencies_ready = True
            elif action == "LOAD_CAPABILITY":
                self._state.capability = self.contract.fixture("fixture://capabilityManifest")
            else:
                raise cm.UnknownOperationError(
                    "lower-local seed %r is not declared for the bilateral profile" % action)

    # -- steps ----------------------------------------------------------
    def _scope(self, outputs: Mapping[str, Any]) -> ExpressionScope:
        return ExpressionScope(
            deployment=self.vector, constants=self.contract.constants, steps=outputs)

    def _resolve(self, value: Any, outputs: Mapping[str, Any]) -> Any:
        resolved = resolve_expressions(self.contract.resolve_fixture_refs(value), self._scope(outputs))
        cm.assert_no_unresolved(resolved, "step material")
        return resolved

    def _run_step(self, model: ScenarioModel, index: int, step: Mapping[str, Any],
                  outputs: dict[str, dict[str, Any]], http_sequence: list[int],
                  normalized: list[dict[str, Any]]) -> None:
        op = step["op"]
        self.contract.operation(op)
        step_id = step["id"]
        if op in _LOWER_LOCAL_OPS:
            outputs[step_id] = self._lower_local_step(step)
            return
        if op == "O1_NORMALIZE":
            outputs[step_id] = self._normalize_step(step, outputs)
            return
        if op == "COORDINATOR_PROCESS_INTENT":
            outputs[step_id] = self._coordinator_step(step, outputs)
            return
        if op == "A1_EMIT_STATUS":
            outputs[step_id] = self._emit_status_step(step, outputs, http_sequence, normalized, index)
            return
        if op == "O1_NOTIFY":
            outputs[step_id] = self._notify_step(step, outputs, http_sequence, normalized, index)
            return
        if op in {"HTTP", "R1_DME_QUERY", "R1_DME_DATA_JOB", "R1_DME_PUBLISH"}:
            outputs[step_id] = self._network_step(
                model, index, step, outputs, http_sequence, normalized)
            return
        raise cm.UnknownOperationError("operation %r has no bilateral execution path" % op)

    def _lower_local_step(self, step: Mapping[str, Any]) -> dict[str, Any]:
        op = step["op"]
        if op == "KPM_SNAPSHOT":
            return {
                "snapshot": {"fresh": step["fresh"],
                             "measurement": step.get("measurement"),
                             "value": step.get("value")},
                "servingCellNcI": step.get("observedServingNcI"),
                "selectedTargetNcI": step.get("uniqueTargetNcI"),
            }
        if op == "E2_CONTROL_RESULT":
            with self._lock:
                self._state.e2_log["controlRequests"] += 1
                if step["effectApplied"]:
                    self._state.e2_log["normalEffects"] += 1
            return {"result": step["result"], "effectApplied": step["effectApplied"]}
        if op == "READBACK":
            return {"servingCellNcI": step.get("servingCellNcI"), "quality": step["quality"]}
        if op == "E2_STUB_LOG":
            with self._lock:
                observed = dict(self._state.e2_log)
            for key, value in step["assert"].items():
                if observed.get(key) != value:
                    raise StepFailure(
                        "E2 stub boundary log %s=%r, expected %r" % (key, observed.get(key), value))
            return {"boundaryLog": observed}
        if op == "O1_RETRIEVE":
            raw = self.contract.fixture_bytes(step["artifactRef"])
            return {"bytesSha256": sha256_bytes(raw), "byteSha256": sha256_bytes(raw),
                    "artifactRef": step["artifactRef"], "byteCount": len(raw)}
        raise cm.UnknownOperationError("lower-local operation %r is undeclared" % op)

    def _normalize_step(self, step: Mapping[str, Any],
                        outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        source = outputs[step["inputFromStep"]]
        produced = self._ics("o1_consumer", "O1_NORMALIZE", {
            "inputFromStep": step["inputFromStep"],
            "artifactRef": source["artifactRef"],
            "byteSha256": source["byteSha256"],
        })
        if "records" not in produced:
            raise StepFailure("O1_NORMALIZE produced no records output")
        expected_records = [self.contract.fixture(ref) for ref in step.get("assertRecordRefs", ())]
        if expected_records:
            observed = produced["records"]
            if len(observed) != len(expected_records):
                raise StepFailure(
                    "O1_NORMALIZE produced %d records, catalog declares %d"
                    % (len(observed), len(expected_records)))
            for position, (left, right) in enumerate(zip(observed, expected_records)):
                # Identifier-canonical on both sides, exactly as for
                # assertBodyRef: the record's correlation.policyId is the one
                # the counterpart assigned this run, and the catalog fixture
                # necessarily names the declared literal.
                if (self._canonical_jcs_sha256(left, outputs)
                        != self._declared_jcs_sha256(right, outputs)):
                    raise StepFailure(
                        "normalized record %d does not equal %s "
                        "(identifier-canonical observed %s vs declared %s)"
                        % (position, step["assertRecordRefs"][position],
                           self._canonical_jcs_sha256(left, outputs),
                           self._declared_jcs_sha256(right, outputs)))
        return produced

    def _coordinator_step(self, step: Mapping[str, Any],
                          outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        required = self.contract.operation("COORDINATOR_PROCESS_INTENT")["required"]
        arguments = {name: self._resolve(step[name], outputs) for name in required}
        before = self._ics_state("rapp").get("processIntentCalls", 0)
        produced = self._ics("rapp", "COORDINATOR_PROCESS_INTENT", arguments)
        after = self._ics_state("rapp").get("processIntentCalls", 0)
        if after - before != 1:
            raise StepFailure("processIntentCalls delta is %d, contract requires exactly 1"
                              % (after - before))
        if produced.get("profileError"):
            raise StepFailure("coordinator reported profileError %r" % produced["profileError"])
        history = produced.get("fsmHistory") or []
        if not history or any(entry.get("origin") != "REAL" for entry in history):
            raise StepFailure("coordinator FSM history is empty or not all-REAL")
        visited = {entry["from"] for entry in history} | {entry["to"] for entry in history}
        if not {"S0", "S6"}.issubset(visited):
            raise StepFailure("coordinator FSM history does not span S0..S6: %s" % sorted(visited))
        if not produced.get("terminalOutcome"):
            raise StepFailure("coordinator produced no terminal outcome")
        if not produced.get("ledgerReferences"):
            raise StepFailure("coordinator produced no ledger references")
        return produced

    def _emit_status_step(self, step: Mapping[str, Any], outputs: dict[str, dict[str, Any]],
                          http_sequence: list[int], normalized: list[dict[str, Any]],
                          index: int) -> dict[str, Any]:
        status = self.contract.fixture(step["statusRef"]) if "statusRef" in step else step["status"]
        # scenario-runner-contract.1.0.1.json #/outputBindings/
        # responseAssignedIdentifiers: a later step references the identifier the
        # creating response assigned, never a predicted one.  The reference
        # runner substitutes the last captured policyId into the emitted
        # snapshot (oran/conformance/runner.py A1_EMIT_STATUS); a producer that
        # emitted the catalog literal instead would be naming a policy the
        # counterpart never assigned.
        assigned = [values.get("policyId") for values in outputs.values()
                    if isinstance(values, Mapping) and isinstance(values.get("policyId"), str)]
        if assigned:
            status = json.loads(json.dumps(status))
            if isinstance(status.get("aicStatus"), Mapping):
                status["aicStatus"]["policyId"] = assigned[-1]
        with self._lock:
            if not self._state.policies:
                raise StepFailure("A1_EMIT_STATUS has no A1 policy resource to attach to")
            key = next(iter(self._state.policies))
            # scenario-runner-contract.1.0.1.json #/operations/A1_EMIT_STATUS:
            # "An explicit replay=true re-delivers an EXISTING snapshot without
            # persistence."  Either way the producer holds this snapshot when
            # the callback goes out, so a consumer that answers a notification
            # by querying the authoritative resource finds it.  What `replay`
            # suppresses is the *new-snapshot* bookkeeping (the duplicate and
            # regression checks), not the resource itself; leaving the
            # admission status derived at PUT time in place would serve a
            # different, earlier artifact than the one just delivered.
            self._state.statuses[key] = status
        destination = str(self.vector["a1"]["statusCallbackRoot"])
        response = self._call(method="POST", uri=destination, payload=status,
                              headers={"Version": "1.0.0"})
        expected = step.get("callbackExpectedHttpStatus")
        http_sequence.append(response.status)
        normalized.append({
            "stepId": step["id"], "op": step["op"], "method": "POST",
            "uri": destination,
            "requestJcsSha256": self._canonical_jcs_sha256(status, outputs),
            "status": response.status,
            "responseSha256": self._canonical_bytes_sha256(response.body, outputs),
        })
        if expected is not None and response.status != expected:
            raise StepFailure(
                "A1 status callback returned %d, catalog declares %d (body %r)"
                % (response.status, expected, response.body))
        return {"statusSnapshot": status, "callbackStatus": response.status}

    def _notify_step(self, step: Mapping[str, Any], outputs: dict[str, dict[str, Any]],
                     http_sequence: list[int], normalized: list[dict[str, Any]],
                     index: int) -> dict[str, Any]:
        notification = self.contract.fixture(step["notificationRef"])
        template = self.contract.endpoint_templates["o1NotificationRecipient"]
        uri = expand_endpoint(template, roots=self.roots, bindings={}, label=step["id"])
        response = self._call(method="POST", uri=uri, payload=notification)
        http_sequence.append(response.status)
        normalized.append({
            "stepId": step["id"], "op": step["op"], "method": "POST", "uri": uri,
            "requestJcsSha256": self._canonical_jcs_sha256(notification, outputs),
            "status": response.status,
            "responseSha256": self._canonical_bytes_sha256(response.body, outputs),
        })
        if response.status != 204:
            raise StepFailure(
                "O1_NOTIFY returned %d; the contract requires 204 after durable acceptance "
                "(resolved recipient %s)" % (response.status, uri))
        return {"status": response.status, "notification": notification}

    # -- HTTP-bearing steps ---------------------------------------------
    def _network_step(self, model: ScenarioModel, index: int, step: Mapping[str, Any],
                      outputs: dict[str, dict[str, Any]], http_sequence: list[int],
                      normalized: list[dict[str, Any]]) -> dict[str, Any]:
        step_id = step["id"]
        template = self.contract.endpoint_template(step["endpointRef"])
        bindings = self._resolve(step.get("bindings", {}), outputs)
        if M03_PREDICT_POLICY_ID in self._driver_mutations and "policyId" in bindings:
            bindings = dict(bindings, policyId=self.contract.constants["policyId"])
        uri = expand_endpoint(template, roots=self.roots, bindings=bindings, label=step_id)

        if step.get("runnerMode") == "CAPTURE_AND_RESPOND":
            return self._capture_and_respond(step, bindings, uri, outputs,
                                             http_sequence, normalized)
        if step.get("actor") == "NON_RT_RIC_FRAMEWORK":
            return self._upper_internal_exchange(step, uri, http_sequence, normalized)

        if M02_DROP_ONE_DME_PUSH in self._driver_mutations and step["op"] == "R1_DME_PUBLISH" \
                and step_id.endswith("-2"):
            return {"status": None, "accepted": False}

        payload, raw_body = self._request_body(step, outputs)
        if step["op"] == "R1_DME_PUBLISH":
            self._assert_publish_constraints(step, payload)
        pre_create = step.get("preCreate")
        if pre_create is not None and pre_create.get("persistBeforeRequest"):
            binding_id = self._resolve(pre_create["deliveryBindingId"], outputs)
            self._ics("rapp", "R1_DME_BINDING_PRECREATE",
                      {"deliveryBindingId": binding_id, "job": payload})

        headers = dict(step.get("headers") or {})
        method = self._method_for(step)
        response = self._call(method=method, uri=uri, payload=payload,
                              raw_body=raw_body, headers=headers)
        http_sequence.append(response.status)
        normalized.append({
            "stepId": step_id, "op": step["op"], "method": method,
            "uri": self._mask(uri, outputs), "status": response.status,
            "requestJcsSha256": self._canonical_jcs_sha256(payload, outputs),
            "responseSha256": self._canonical_bytes_sha256(response.body, outputs),
        })
        expected = cm.declared_expected_status(step)
        if expected is not None and response.status != expected:
            raise StepFailure(
                "%s returned %d, catalog declares %d" % (step_id, response.status, expected))
        body = self._json_body(response, step_id)
        captured = self._apply_capture(step, response, body, uri)
        self._assert_response_headers(step, response, captured, outputs)
        self._assert_body(step, body, outputs)
        if step.get("assertAcceptedPushPayloadCount") is not None:
            observed = (body or {}).get("acceptedPushPayloadCount")
            if observed != step["assertAcceptedPushPayloadCount"]:
                raise StepFailure(
                    "acceptedPushPayloadCount is %r, catalog declares %r"
                    % (observed, step["assertAcceptedPushPayloadCount"]))
        if pre_create is not None and "dataJobId" in captured:
            self._ics("rapp", "R1_DME_BINDING_COMMIT", {
                "deliveryBindingId": self._resolve(pre_create["deliveryBindingId"], outputs),
                "dataJobId": captured["dataJobId"],
            })
        produced: dict[str, Any] = {"status": response.status, "headers": dict(response.headers),
                                    "body": body}
        produced.update(captured)
        if step["op"] == "R1_DME_PUBLISH":
            produced["accepted"] = response.status == 204
        return produced

    def _method_for(self, step: Mapping[str, Any]) -> str:
        """The HTTP method, from the step or from the operation's declared semantics."""
        if "method" in step:
            return str(step["method"])
        declaration = self.contract.operation(step["op"])
        if "method" in declaration.get("required", ()):
            raise cm.UnresolvedBindingError(
                "operation %s requires a declared method but step %s carries none"
                % (step["op"], step["id"]))
        semantics = declaration.get("semantics", "")
        if step["op"] == "R1_DME_PUBLISH":
            # "POST exactly one schema-valid policy-evidence record"
            if not semantics.startswith("POST"):
                raise cm.ContractFaithfulnessError(
                    "R1_DME_PUBLISH semantics no longer declare POST")
            return "POST"
        if step["op"] == "R1_DME_QUERY":
            # "Query DataJobInfo/status or a negotiated ONE_TIME PULL_HTTP URI only."
            if not semantics.startswith("Query"):
                raise cm.ContractFaithfulnessError(
                    "R1_DME_QUERY semantics no longer declare a query")
            return "GET"
        raise cm.UnresolvedBindingError(
            "no method is derivable for operation %s (step %s)" % (step["op"], step["id"]))

    def _request_body(self, step: Mapping[str, Any],
                      outputs: dict[str, dict[str, Any]]) -> tuple[Any, bytes | None]:
        if "bodyRef" in step:
            return self.contract.fixture(step["bodyRef"]), None
        if "evidenceRecordRef" in step:
            return self._resolve(step["evidenceRecordRef"], outputs), None
        if "body" not in step:
            return None, None
        payload = self._resolve(step["body"], outputs)
        if "jsonPatch" in step:
            payload = apply_json_patch(payload, step["jsonPatch"])
        return payload, None

    def _assert_publish_constraints(self, step: Mapping[str, Any], payload: Any) -> None:
        atomicity = self.contract.runner["dmeRecordAtomicity"]
        if atomicity["arrayOrEnvelopeForbidden"] and isinstance(payload, list):
            raise StepFailure("R1_DME_PUBLISH payload must be a single record, not an array")
        rendered = json.dumps(payload, ensure_ascii=False)
        for forbidden in step.get("payloadMustNotContain", ()):
            if ('"%s"' % forbidden) in rendered:
                raise StepFailure("publish payload carries forbidden member %r" % forbidden)

    def _capture_and_respond(self, step: Mapping[str, Any], bindings: Mapping[str, Any],
                             uri: str, outputs: dict[str, dict[str, Any]],
                             http_sequence: list[int],
                             normalized: list[dict[str, Any]]) -> dict[str, Any]:
        """Observe the upper's own outbound call on the lower-owned A1 listener."""
        method = step["method"]
        policy_type_id = str(bindings["policyTypeId"])
        policy_id = str(bindings["policyId"])
        deadline = time.monotonic() + self._deadline_ms / 1000.0
        observed: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            with self._lock:
                for entry in self._state.observed:
                    if (entry["method"] == method
                            and entry["policyTypeId"] == policy_type_id
                            and entry["policyId"] == policy_id
                            and not entry.get("consumed")):
                        entry["consumed"] = True
                        observed = entry
                        break
            if observed is not None:
                break
            time.sleep(0.005)
        if observed is None:
            raise StepFailure(
                "the upper never issued %s %s; CAPTURE_AND_RESPOND observed nothing"
                % (method, uri))
        declared, _ = self._request_body(step, outputs)
        if declared is not None:
            if observed["body"] is None:
                raise StepFailure("the upper forwarded a body that is not JSON")
            if jcs_sha256(observed["body"]) != jcs_sha256(declared):
                raise StepFailure(
                    "the body the upper forwarded to A1 is not byte-equivalent to the declared "
                    "material (observed JCS %s, declared JCS %s)"
                    % (jcs_sha256(observed["body"]), jcs_sha256(declared)))
        http_sequence.append(observed["status"])
        normalized.append({
            "stepId": step["id"], "op": step["op"], "method": method,
            "uri": self._mask(uri, outputs), "status": observed["status"],
            "requestJcsSha256": self._canonical_jcs_sha256(observed["body"], outputs),
            "responseSha256": None,
        })
        expected = cm.declared_expected_status(step)
        if expected is not None and observed["status"] != expected:
            raise StepFailure("observed A1 %s answered %d, catalog declares %d"
                              % (method, observed["status"], expected))
        return {"status": observed["status"], "body": observed["body"],
                "rawSha256": observed["rawSha256"]}

    def _upper_internal_exchange(self, step: Mapping[str, Any], uri: str,
                                 http_sequence: list[int],
                                 normalized: list[dict[str, Any]]) -> dict[str, Any]:
        """The only exchange with an upper client *and* an upper server.

        Invisible on the lower's wire by construction, so its status comes from
        the declared Integration Control Surface state read, tagged with its
        provenance.  It is never inferred from the catalog alone.
        """
        state = self._ics_state("rapp")
        received = state.get("statusCallbacksReceived")
        status = state.get("lastStatusCallbackStatus")
        if received is None or status is None:
            raise StepFailure(
                "the rApp component does not report statusCallbacksReceived / "
                "lastStatusCallbackStatus, so %s cannot be evidenced" % step["id"])
        if received < 1:
            raise StepFailure("the upper never relayed the R1 status to its rApp callback")
        http_sequence.append(int(status))
        normalized.append({
            "stepId": step["id"], "op": step["op"], "method": step.get("method"),
            "uri": uri, "status": int(status), "requestJcsSha256": None,
            "responseSha256": None, "provenance": "AGREED_UPPER_OBSERVATION",
        })
        expected = cm.declared_expected_status(step)
        if expected is not None and int(status) != expected:
            raise StepFailure("upper-internal relay reported %s, catalog declares %d"
                              % (status, expected))
        return {"status": int(status), "provenance": "AGREED_UPPER_OBSERVATION"}

    # -- assertions and captures ----------------------------------------
    def _json_body(self, response: Any, step_id: str) -> Any:
        if not response.body:
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            raise StepFailure("%s response body is not JSON" % step_id) from None

    def _apply_capture(self, step: Mapping[str, Any], response: Any, body: Any,
                       uri: str) -> dict[str, Any]:
        captured: dict[str, Any] = {}
        for name, rule in (step.get("capture") or {}).items():
            source = rule["from"]
            if source == "response.status":
                value: Any = response.status
            elif source.startswith("response.headers."):
                value = response.header(source[len("response.headers."):])
            elif source == "response.body":
                value = body
            elif source.startswith("response.body."):
                value = json_pointer(body, source[len("response.body."):])
            else:
                raise cm.ContractFaithfulnessError("capture source %r is undeclared" % source)
            transform = rule.get("transform", "IDENTITY")
            if transform == "LOCATION_LAST_PATH_SEGMENT":
                if not isinstance(value, str) or not value:
                    raise StepFailure("capture %s has no Location to transform" % name)
                from urllib.parse import unquote, urlsplit as _split
                segments = [item for item in _split(value).path.split("/") if item]
                if not segments:
                    raise StepFailure("Location %r has no final path segment" % value)
                value = unquote(segments[-1])
            elif transform == "JCS_SHA256":
                value = jcs_sha256(value)
            elif transform != "IDENTITY" and transform != "JSON_POINTER":
                raise cm.ContractFaithfulnessError("transform %r is undeclared" % transform)
            self._validate_capture(name, value, rule.get("validate"))
            captured[name] = value
        return captured

    def _validate_capture(self, name: str, value: Any, predicate: str | None) -> None:
        if predicate is None:
            return
        declared = self.contract.runner["outputBindings"]["validationPredicates"]
        if predicate not in declared:
            raise cm.ContractFaithfulnessError("validation predicate %r is undeclared" % predicate)
        if predicate in {"NON_EMPTY_STRING", "OPAQUE_ID"}:
            if not isinstance(value, str) or not value:
                raise StepFailure("capture %s failed %s" % (name, predicate))
        elif predicate == "JSON_OBJECT":
            if not isinstance(value, Mapping):
                raise StepFailure("capture %s failed JSON_OBJECT" % name)
        elif predicate == "JSON_ARRAY":
            if not isinstance(value, list):
                raise StepFailure("capture %s failed JSON_ARRAY" % name)
        elif predicate == "UUID":
            import uuid as _uuid
            try:
                _uuid.UUID(str(value))
            except ValueError:
                raise StepFailure("capture %s failed UUID" % name) from None

    def _assert_response_headers(self, step: Mapping[str, Any], response: Any,
                                 captured: Mapping[str, Any],
                                 outputs: Mapping[str, Any]) -> None:
        for name, declared in (step.get("assertResponseHeaders") or {}).items():
            observed = response.header(name)
            if isinstance(declared, str) and declared.startswith("#/endpointTemplates/"):
                template = self.contract.endpoint_template(declared)
                bindings = dict(captured)
                expected_uri = expand_endpoint(
                    template, roots=self.roots, bindings=bindings, label=step["id"])
                if observed != expected_uri:
                    raise StepFailure(
                        "%s header is %r, the catalog requires the %s expansion %r"
                        % (name, observed, declared, expected_uri))
                continue
            if observed != declared:
                raise StepFailure("%s header is %r, catalog declares %r" % (name, observed, declared))

    def _assert_body(self, step: Mapping[str, Any], body: Any,
                     outputs: Mapping[str, Any]) -> None:
        if "assertBodyRef" in step:
            expected = self.contract.fixture(step["assertBodyRef"])
            # The fixture names the catalog's declared policy identifier, while
            # the response necessarily carries the one the counterpart assigned
            # this run (#/outputBindings/responseAssignedIdentifiers forbids
            # predicting it).  Canonicalise BOTH to the same role token and then
            # compare every remaining byte -- weaker only for the identifier,
            # which is the one value that provably cannot match.
            observed_digest = self._canonical_jcs_sha256(body, outputs)
            expected_digest = self._declared_jcs_sha256(expected, outputs)
            if observed_digest != expected_digest:
                raise StepFailure(
                    "%s body is not byte-equivalent to %s (observed JCS %s, "
                    "declared JCS %s, both identifier-canonical)"
                    % (step["id"], step["assertBodyRef"], observed_digest,
                       expected_digest))
        for dotted, expected_value in (step.get("assertBody") or {}).items():
            observed = _dotted_get(body, dotted)
            if observed != expected_value:
                raise StepFailure(
                    "%s body %s is %r, catalog declares %r"
                    % (step["id"], dotted, observed, expected_value))

    def _assigned_identifiers(self, outputs: Mapping[str, Any]) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for values in outputs.values():
            for key in ("policyId", "dataJobId"):
                value = values.get(key)
                if isinstance(value, str) and value:
                    found.append((value, "{%s}" % key))
        return sorted(found, key=lambda item: len(item[0]), reverse=True)

    def _mask(self, uri: str, outputs: Mapping[str, Any]) -> str:
        """Replace server-assigned identifiers so two runs compare byte-for-byte."""
        masked = uri
        for value, token in self._assigned_identifiers(outputs):
            masked = masked.replace(value, token)
        return masked

    def _canonical_jcs_sha256(self, value: Any, outputs: Mapping[str, Any]) -> str | None:
        """JCS digest over the identifier-canonical form of ``value``.

        Substituting the assigned identifiers *before* hashing keeps the digest
        run-invariant while still detecting any other byte change, which is
        strictly stronger than excluding the digest from the comparison.
        """
        if value is None:
            return None
        rendered = _json_dumps_canonical(value)
        for identifier, token in self._assigned_identifiers(outputs):
            rendered = rendered.replace(identifier, token)
        return sha256_bytes(rendered.encode("utf-8"))

    def _declared_jcs_sha256(self, value: Any, outputs: Mapping[str, Any]) -> str:
        """JCS digest of declared material with catalog identifiers tokenised.

        The mirror image of :meth:`_canonical_jcs_sha256`: the catalog literal
        ``constants.policyId`` stands where a run assigns its own identifier, so
        it is replaced by the same role token -- and only when this step actually
        has an assigned identifier to stand in for.
        """
        rendered = _json_dumps_canonical(value)
        assigned = {token for _identifier, token in self._assigned_identifiers(outputs)}
        declared_policy_id = self.contract.constants.get("policyId")
        if "{policyId}" in assigned and isinstance(declared_policy_id, str):
            rendered = rendered.replace(declared_policy_id, "{policyId}")
        return sha256_bytes(rendered.encode("utf-8"))

    def _canonical_bytes_sha256(self, raw: bytes, outputs: Mapping[str, Any]) -> str:
        text = raw.decode("utf-8", errors="replace")
        for identifier, token in self._assigned_identifiers(outputs):
            text = text.replace(identifier, token)
        return sha256_bytes(text.encode("utf-8"))
