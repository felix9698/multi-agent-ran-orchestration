"""Contract-faithful, black-box scenario execution core."""
from __future__ import annotations

from copy import deepcopy
import base64
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Mapping, Protocol
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .contracts import ContractBundle, ContractError, canonicalize, jcs_sha256, pointer, validate_json_schema
from .expression import ExpressionError, ForwardOutputError, UndefinedOutputError, resolve
from .harness_api import HarnessAdapter, HarnessError, LocalFakeHarness


# A rule is preflight-evaluable only when the scenario contains at least one
# operation capable of producing the concrete inputs consumed by its runtime
# evaluator.  Keeping this table beside the evaluator makes an added rule fail
# closed until its observation surface is explicitly wired.
RULE_EVALUATION_OPERATIONS: dict[str, frozenset[str]] = {
    "RULE-POLICY-TYPE-DIGEST": frozenset({"HTTP"}),
    "RULE-LOCATION-STABLE": frozenset({"HTTP"}),
    "RULE-R1-RESPONSE-ASSIGNED-ID": frozenset({"HTTP", "R1_DME_REGISTER", "R1_DME_DATA_JOB"}),
    "RULE-R1-SERVICE-REGISTRATION": frozenset({"HTTP"}),
    "RULE-R1-SERVICE-DISCOVERY": frozenset({"HTTP"}),
    "RULE-R1-A1-UPDATE-BODY": frozenset({"HTTP"}),
    "RULE-R1-DME-WIRE-FORMS": frozenset({"R1_DME_REGISTER", "R1_DME_DISCOVER", "R1_DME_DATA_JOB"}),
    "RULE-NO-DUPLICATE-EPISODE": frozenset({"HTTP"}),
    "RULE-NO-DUPLICATE-ACTUATION": frozenset({"HTTP", "E2_CONTROL_RESULT", "READBACK"}),
    "RULE-STATUS-DEDUPE": frozenset({"A1_EMIT_STATUS", "HTTP"}),
    "RULE-FENCE": frozenset({"KPM_SNAPSHOT", "E2_CONTROL_RESULT", "HTTP"}),
    "RULE-E2-INVENTORY-READY": frozenset({"E2_INVENTORY_VALIDATE"}),
    "RULE-PARTIAL-NACK-NO-ROLLBACK-RECOVERY": frozenset({"E2_CONTROL_RESULT"}),
    "RULE-R1-CORRELATION": frozenset({"R1_DME_PUBLISH"}),
    "RULE-R1-DME-PUSH-BINDING": frozenset({"R1_DME_PUBLISH"}),
    "RULE-O1-NOTIFY-AND-RETRIEVAL": frozenset({"O1_RETRIEVE"}),
    "RULE-O1-LIVE-VALUE-INVARIANTS": frozenset({"O1_NORMALIZE"}),
    "RULE-O1-DN-BIJECTION": frozenset({"O1_RETRIEVE", "O1_NORMALIZE"}),
    "RULE-O1-TIME-RELATIONS": frozenset({"O1_NORMALIZE"}),
    "RULE-O1-FILE-TEMPORAL-ORDER": frozenset({"O1_NOTIFY", "O1_RETRIEVE"}),
    "RULE-O1-QUALITY-PRECEDENCE": frozenset({"O1_NORMALIZE", "R1_DME_PUBLISH"}),
    "RULE-O1-RAW-DIGEST-SELF-CONSISTENT": frozenset({"O1_RETRIEVE", "O1_NORMALIZE"}),
    "RULE-O1-POSITIONAL-MAP": frozenset({"O1_NORMALIZE"}),
    "RULE-O1-NULL-NOT-ZERO": frozenset({"O1_NORMALIZE"}),
    "RULE-O1-ZERO-VALID": frozenset({"O1_NORMALIZE"}),
    "RULE-O1-SUSPECT": frozenset({"O1_NORMALIZE"}),
    "RULE-O1-DEDUP": frozenset({"O1_NORMALIZE"}),
    "RULE-O1-DIGEST-GATE": frozenset({"O1_RETRIEVE"}),
    "RULE-O1-OVERLAP": frozenset({"O1_NORMALIZE"}),
    "RULE-O1-FINAL-SOURCE": frozenset({"R1_DME_QUERY"}),
    "RULE-O1-SUBSCRIPTION-LIFECYCLE": frozenset({"PERF_METRIC_JOB"}),
    "RULE-O1-FILES-RECOVERY": frozenset({"O1_FILE_LIST"}),
    "RULE-NO-ACTION-DETERMINISM": frozenset({"KPM_SNAPSHOT"}),
    "RULE-O1-NO-FAKE-PROVENANCE": frozenset({
        "O1_NOTIFY", "O1_RETRIEVE", "O1_FILE_LIST", "R1_DME_QUERY", "PERF_METRIC_JOB"}),
    "RULE-SECURITY-ZERO-SIDE-EFFECT": frozenset({"HTTP"}),
}

COORDINATOR_TERMINAL_EXPECTED_FIELDS = frozenset({
    "coordinatorState", "terminalOutcomeCount", "fullSuccessChainComplete",
    "upperOutcome", "processIntentCalls", "coordinatorTerminalOutcome",
    "coordinatorTerminalEvidenceRef", "coordinatorLedgerReferences",
})


class RunnerError(ContractError):
    pass


class PreflightError(RunnerError):
    disposition = "FAIL_SCENARIO_WITHOUT_TARGET_CALL"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: Any


class HttpBoundary(Protocol):
    def request(self, method: str, url: str, headers: Mapping[str, str], body: Any, timeout_ms: int | None) -> HttpResponse: ...


class SftpBoundary(Protocol):
    def retrieve(self, source: str, *, vector: Mapping[str, Any], timeout_ms: int | None) -> bytes: ...


class NetconfBoundary(Protocol):
    def execute(self, step: Mapping[str, Any], *, vector: Mapping[str, Any], profile: Mapping[str, Any],
                timeout_ms: int | None) -> dict[str, Any]: ...


class StdlibHttpBoundary:
    def request(self, method: str, url: str, headers: Mapping[str, str], body: Any, timeout_ms: int | None) -> HttpResponse:
        payload = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = Request(url, data=payload, method=method, headers=dict(headers))
        try:
            response = urlopen(request, timeout=(timeout_ms / 1000 if timeout_ms else None))  # nosec B310: vector-supplied boundary
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
            parsed = json.loads(raw.decode("utf-8")) if raw and "json" in content_type else raw
            return HttpResponse(response.status, dict(response.headers.items()), parsed)


class LoopbackDevHttpBoundary(StdlibHttpBoundary):
    """Inject only the vector-declared authenticated rApp identity on R1."""

    def __init__(self, vector: Mapping[str, Any]):
        self.r1_origin = "%s://%s" % (
            urlparse(vector["r1"]["apiRoot"]).scheme,
            urlparse(vector["r1"]["apiRoot"]).netloc)
        self.rapp_id = str(vector["r1"]["rAppId"])

    def request(self, method: str, url: str, headers: Mapping[str, str],
                body: Any, timeout_ms: int | None) -> HttpResponse:
        effective = dict(headers)
        if url.startswith(self.r1_origin + "/"):
            effective.setdefault("X-Authenticated-RApp-Id", self.rapp_id)
        return super().request(method, url, effective, body, timeout_ms)


@dataclass
class ScenarioResult:
    scenario_id: str
    disposition: str
    reason: str | None = None
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    http_sequence: list[int | str] = field(default_factory=list)
    ran_write_counts: dict[str, int] = field(default_factory=lambda: {"normal": 0, "rollback": 0})
    evidence_commit_count: int = 0
    evidence_paths: dict[str, str] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    responses: list[dict[str, Any]] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)
    applicability: dict[str, Any] = field(default_factory=dict)


class ScenarioRunner:
    """Never imports a target component; all non-HTTP work crosses an adapter."""

    def __init__(self, bundle: ContractBundle, vector: Mapping[str, Any], harness: HarnessAdapter | None = None,
                 http: HttpBoundary | None = None, artifacts_root: str | Path = "conformance-results",
                 sftp: SftpBoundary | None = None, netconf: NetconfBoundary | None = None,
                 validation_vector: Mapping[str, Any] | None = None):
        self.bundle = bundle
        self.vector = deepcopy(dict(vector))
        self._scenario_deployment = self.vector
        self.harness = harness or LocalFakeHarness()
        self.http = http or StdlibHttpBoundary()
        self.sftp = sftp
        self.netconf = netconf
        self.validation_vector = deepcopy(dict(validation_vector)) if validation_vector is not None else self.vector
        self.artifacts_root = Path(artifacts_root)
        self._preflight_complete = False
        self._scenario_started = 0.0
        self._accepted_evidence_digests: set[str] = set()
        self._transport_outcomes: dict[str, Any] = {}
        self._step_faults: dict[str, list[dict[str, Any]]] = {}
        self._derived_observations: dict[str, Any] = {}
        self._prearmed_callback_steps: set[str] = set()
        self._loaded_capability: dict[str, Any] | None = None

    def preflight(self) -> None:
        """Run every normative check in its contract-defined order before calls."""
        checks = self.bundle.runner["preflight"]["checksInOrder"]
        for check in checks:
            method = getattr(self, "_check_" + check.lower(), None)
            if method is None:
                raise PreflightError("unimplemented mandatory preflight check: %s" % check)
            try:
                method()
            except (ContractError, KeyError, TypeError, ValueError) as exc:
                raise PreflightError("%s: %s" % (check, exc)) from exc
        self._preflight_complete = True

    def _check_parse_all_machine_artifacts(self) -> None:
        if self.bundle.catalog.get("atomicScenarioCount") != len(self.bundle.catalog.get("scenarios", [])):
            raise ContractError("scenario count is inconsistent")

    def _check_validate_deployment_vector_draft_2020_12(self) -> None:
        validate_json_schema(self.validation_vector, self.bundle.schema("deployment-test-vector"), self.bundle.path)

    def _check_validate_e2_inventory_ready_implies_active_connections_active_required_ran_functions_and_unique_structured_node_ids(self) -> None:
        inventory = self.vector["e2Inventory"]
        if inventory.get("status") != "READY":
            raise ContractError("AIC_E2_INVENTORY_NOT_READY")
        identities = [jcs_sha256(item["globalE2NodeId"]) for item in inventory.get("connections", [])]
        if len(identities) != len(set(identities)):
            raise ContractError("AIC_E2_INVENTORY_NOT_READY duplicate globalE2NodeId")
        for connection in inventory.get("connections", []):
            active = {function.get("ranFunctionId") for function in connection.get("ranFunctions", []) if function.get("active")}
            if not connection.get("active") or not {2, 3}.issubset(active):
                raise ContractError("AIC_E2_INVENTORY_NOT_READY")

    def _check_verify_referenced_file_digests_when_declared(self) -> None:
        # The schema-backed vector declares digests; actual fixture byte checks happen at retrieval.
        for key, value in self.vector.get("schemas", {}).items():
            if key.endswith("JcsSha256") and (not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)):
                raise ContractError("invalid declared schema digest")

    def _check_verify_policy_evidence_schema_canonical_text_and_jcs_digest(self) -> None:
        schemas = self.vector["schemas"]
        try:
            parsed = json.loads(schemas["policyEvidenceRecordSchemaCanonicalJson"])
        except (KeyError, json.JSONDecodeError) as exc:
            raise ContractError("policy evidence canonical schema is invalid") from exc
        if parsed != schemas["policyEvidenceRecordSchema"] or jcs_sha256(parsed) != schemas["policyEvidenceRecordSchemaJcsSha256"]:
            raise ContractError("policy evidence schema digest mismatch")

    def _check_verify_o1_netconf_profile_and_every_lifecycle_rpc_fixture_resolve(self) -> None:
        profile = self.bundle.fixture("fixture://o1NetconfYangProfile")
        if not isinstance(profile, dict):
            raise ContractError("O1 NETCONF profile does not resolve")
        lifecycle = profile.get("lifecycle")
        if not isinstance(lifecycle, list) or not lifecycle:
            raise ContractError("O1 NETCONF profile lifecycle is absent")
        def request_fixtures(value: Any) -> list[str]:
            if isinstance(value, dict):
                return ([value["requestFixture"]] if isinstance(value.get("requestFixture"), str) else []) + [
                    item for child in value.values() for item in request_fixtures(child)]
            if isinstance(value, list):
                return [item for child in value for item in request_fixtures(child)]
            return []
        fixtures = request_fixtures(profile)
        if len(fixtures) < 11:
            raise ContractError("O1 NETCONF profile does not declare all lifecycle RPC fixtures")
        for request_fixture in fixtures:
            target = self.bundle.path / request_fixture
            if not target.is_file() or target.name.startswith("._"):
                raise ContractError("O1 NETCONF requestFixture does not resolve: %s" % request_fixture)
            try:
                ElementTree.fromstring(target.read_bytes())
            except (OSError, ElementTree.ParseError) as exc:
                raise ContractError("O1 NETCONF requestFixture is invalid XML: %s" % request_fixture) from exc

    def _check_verify_deployment_file_info_ready_before_expiration(self) -> None:
        for group in self.vector["o1"]["live"]["recoveryFiles"].values():
            entries = group if isinstance(group, list) else [group]
            for item in entries:
                if item["fileReadyTime"] >= item["fileExpirationTime"]:
                    raise ContractError("FileInfo ready time is not before expiration")

    def _check_verify_scenario_and_requirement_links(self) -> None:
        ids = {scenario["id"] for scenario in self.bundle.catalog["scenarios"]}
        for requirement in self.bundle.catalog["requirements"]:
            if not set(requirement["scenarioIds"]).issubset(ids):
                raise ContractError("requirement references unknown scenario")

    def _check_verify_suite_operation_initial_state_and_fault_names(self) -> None:
        suites = set(self.bundle.runner["suiteCatalog"])
        operations = set(self.bundle.runner["operations"])
        faults = set(self.bundle.runner["faults"])
        initial = set(self.bundle.runner["initialStateOperations"])
        named_initial = set(self.bundle.runner["initialStates"])
        for scenario in self.bundle.catalog["scenarios"]:
            if scenario["suite"] not in suites:
                raise ContractError("unknown suite")
            materialization = scenario["materialization"]
            for state in materialization.get("initialState", []):
                if isinstance(state, str) and state not in named_initial:
                    raise ContractError("unknown named initial state")
                if isinstance(state, dict) and state.get("op") not in initial:
                    raise ContractError("unknown initial-state operation")
                if not isinstance(state, (str, dict)):
                    raise ContractError("initial state must be name or operation")
                if isinstance(state, dict) and self.bundle.version == "1.0.1":
                    self._require_descriptor_fields(
                        state, self.bundle.runner["initialStateOperations"][state["op"]])
            for step in materialization["steps"]:
                if step["op"] not in operations:
                    raise ContractError("unknown operation")
                if self.bundle.version == "1.0.1":
                    self._require_descriptor_fields(
                        step, self.bundle.runner["operations"][step["op"]])
            if any(fault.get("type") not in faults for fault in materialization.get("faults", [])):
                raise ContractError("unknown fault")
            required_expected = self.bundle.catalog["normativeSemantics"]["completeExpectedResult"]["requiredFields"]
            if any(name not in scenario.get("expected", {}) for name in required_expected):
                raise ContractError("scenario expected result is incomplete")
            expected = scenario.get("expected", {})
            terminal_assertions = bool(
                COORDINATOR_TERMINAL_EXPECTED_FIELDS & set(expected))
            real_execution_gate = (
                expected.get("requiresRealCoordinatorExecution") is True)
            if self.bundle.version == "1.0.1" and terminal_assertions:
                if not real_execution_gate:
                    raise ContractError(
                        "coordinator-terminal assertions require the real "
                        "Coordinator execution gate")
            if self.bundle.version == "1.0.1" and real_execution_gate:
                process_steps = [
                    step for step in materialization["steps"]
                    if step.get("op") == "COORDINATOR_PROCESS_INTENT"
                ]
                if len(process_steps) != 1:
                    raise ContractError(
                        "real Coordinator execution gate requires exactly one "
                        "COORDINATOR_PROCESS_INTENT operation")
            status_patch = expected.get("statusBodyJsonPatch")
            if self.bundle.version == "1.0.1" and status_patch is not None:
                status_ref = expected.get("statusBodyRef")
                if not isinstance(status_ref, str) or not isinstance(status_patch, list):
                    raise ContractError(
                        "statusBodyJsonPatch non-weakening requires statusBodyRef and an array")
                original_status = self.bundle.fixture(status_ref)
                for operation in status_patch:
                    if (not isinstance(operation, Mapping)
                            or operation.get("op") != "replace"
                            or not isinstance(operation.get("path"), str)
                            or "value" not in operation):
                        raise ContractError(
                            "statusBodyJsonPatch non-weakening permits existing-field replace only")
                    original_value = pointer(
                        original_status, "#" + operation["path"])
                    replacement = operation["value"]
                    if type(replacement) is not type(original_value):
                        raise ContractError(
                            "statusBodyJsonPatch non-weakening forbids type changes")
            if any(rule not in self.bundle.catalog.get("assertionRules", {}) for rule in scenario.get("rules", [])):
                raise ContractError("scenario names unknown assertion rule")

    @staticmethod
    def _require_descriptor_fields(value: Mapping[str, Any],
                                   descriptor: Mapping[str, Any]) -> None:
        operation = value.get("op", "operation")
        for name in descriptor.get("required", []):
            if name not in value:
                raise ContractError("%s requires %s" % (operation, name))
        for alternatives in descriptor.get("requiredAny", []):
            if not any(name in value for name in alternatives):
                raise ContractError("%s requires one of %s" % (
                    operation, ", ".join(alternatives)))

    def _check_verify_fixture_mode_required_rules_are_declared(self) -> None:
        modes = self.bundle.catalog["normativeSemantics"]["fixtureModes"]
        for scenario in self.bundle.catalog["scenarios"]:
            mode = scenario.get("fixtureMode")
            if mode not in modes:
                raise ContractError("unknown fixtureMode: %s" % mode)
            declared = scenario.get("rules", [])
            for rule in modes[mode].get("requiredRules", []):
                if declared.count(rule) != 1:
                    raise ContractError(
                        "fixtureMode required rule must occur exactly once: %s %s" % (
                            scenario["id"], rule))

    def _check_verify_every_declared_rule_has_evaluable_step_or_output_inputs(self) -> None:
        for scenario in self.bundle.catalog["scenarios"]:
            operations = {
                step.get("op") for step in scenario["materialization"].get("steps", [])}
            for rule in scenario.get("rules", []):
                if rule == "RULE-COMPACT-ORACLE-CONSISTENCY":
                    if scenario.get("compactExpectationIds"):
                        continue
                elif operations & RULE_EVALUATION_OPERATIONS.get(rule, frozenset()):
                    continue
                raise ContractError(
                    "AIC_RUNNER_VACUOUS_RULE: %s has no evaluator input in %s" % (
                        rule, scenario["id"]))

    def _check_verify_endpoint_placeholders_use_only_scalar_step_bindings(self) -> None:
        templates = self.bundle.catalog["endpointTemplates"]
        roots = self.bundle.runner["expressionLanguage"][
            "endpointTemplateResolution"]["rootMappings"]
        for scenario in self.bundle.catalog["scenarios"]:
            for step in scenario["materialization"]["steps"]:
                endpoint_ref = step.get("endpointRef")
                if not isinstance(endpoint_ref, str):
                    continue
                name = endpoint_ref.removeprefix("#/endpointTemplates/")
                template = templates.get(name)
                placeholders = (set(re.findall(r"\{([^{}]+)\}", template))
                                if isinstance(template, str) else set())
                dynamic = placeholders - set(roots)
                bindings = step.get("bindings", {})
                if not isinstance(bindings, Mapping):
                    raise ContractError("endpoint bindings must be an object")
                for placeholder in dynamic:
                    if not isinstance(bindings.get(placeholder),
                                      (str, int, float, bool)):
                        raise ContractError(
                            "endpoint placeholder requires scalar step binding: %s" % placeholder)
                descriptor = self.bundle.runner["operations"][step["op"]]
                contract = descriptor.get("endpointBindingContract")
                if not isinstance(contract, Mapping):
                    continue
                if endpoint_ref != contract.get("endpointRef"):
                    raise ContractError("operation uses the wrong endpoint binding contract")
                required = set(contract.get("requiredExactScalarBindings", []))
                if set(bindings) != required or any(
                        not isinstance(bindings.get(key), (str, int, float, bool))
                        for key in required):
                    raise ContractError("endpoint exact scalar bindings differ")
                forbidden = set(contract.get("forbiddenTopLevelAliases", []))
                if forbidden & set(step):
                    raise ContractError("endpoint binding has forbidden top-level alias")

    def _check_resolve_fixture_references_and_static_expressions(self) -> None:
        for scenario in self.bundle.catalog["scenarios"]:
            for ref in scenario.get("fixtureRefs", []):
                self.bundle.fixture(ref)
            prepared = deepcopy(scenario)
            prepared["materialization"]["steps"] = [self._prepare_references(step) for step in scenario["materialization"]["steps"]]
            prepared["materialization"]["steps"] = [self._apply_json_edits(self._resolve_static(step))
                                                       for step in prepared["materialization"]["steps"]]
            expected_ref = scenario.get("expected", {}).get("statusBodyRef")
            if expected_ref is not None:
                self.bundle.fixture(expected_ref)

    def _check_typecheck_endpoint_and_body_expressions(self) -> None:
        for scenario in self.bundle.catalog["scenarios"]:
            resolved = deepcopy(scenario)
            resolved["materialization"]["steps"] = [self._prepare_references(step) for step in scenario["materialization"]["steps"]]
            resolved = self._resolve_static(resolved)
            resolved["materialization"]["steps"] = [self._apply_json_edits(step) for step in resolved["materialization"]["steps"]]
            timing = resolved["materialization"]["time"]
            mode = timing.get("mode")
            if mode not in self.bundle.runner["timeSemantics"]:
                raise ContractError("unknown time semantics mode")
            prior_at_ms = -1
            for step in resolved["materialization"]["steps"]:
                if "endpointRef" in step and not isinstance(step.get("bindings", {}), dict):
                    raise ContractError("endpoint bindings must be an object")
                at_ms = step.get("atMs")
                if mode == "FIXED_LOGICAL":
                    if not isinstance(at_ms, int) or at_ms < 0 or at_ms < prior_at_ms:
                        raise ContractError("FIXED_LOGICAL atMs must be non-negative and ordered")
                    prior_at_ms = at_ms
                elif at_ms is not None and (not isinstance(at_ms, int) or at_ms < 0):
                    raise ContractError("LIVE_OBSERVED atMs must be null or non-negative integer")
                if "timeoutMs" in step and (not isinstance(step["timeoutMs"], int) or step["timeoutMs"] < 1):
                    raise ContractError("timeoutMs must be positive integer")

    def _check_verify_every_step_output_reference_targets_an_earlier_step_and_a_declared_output(self) -> None:
        for scenario in self.bundle.catalog["scenarios"]:
            seen: set[str] = set()
            for step in scenario["materialization"]["steps"]:
                for reference in re.findall(r"\$\{steps\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)", json.dumps(step)):
                    step_id, output = reference
                    if step_id not in seen:
                        raise ForwardOutputError("%s references %s" % (step["id"], step_id))
                    original = next(item for item in scenario["materialization"]["steps"] if item["id"] == step_id)
                    declared = set(self.bundle.runner["operations"][original["op"]].get("declaredOutputs", [])) | set(original.get("capture", {}))
                    if output not in declared:
                        raise UndefinedOutputError("%s.%s" % (step_id, output))
                seen.add(step["id"])

    def _check_verify_fault_boundaries_name_existing_steps(self) -> None:
        for scenario in self.bundle.catalog["scenarios"]:
            steps = {step["id"]: step for step in scenario["materialization"]["steps"]}
            for fault in scenario["materialization"].get("faults", []):
                named = fault.get("afterStep", fault.get("beforeStep"))
                if named not in steps:
                    raise ContractError("fault boundary names unknown step")
                if (fault.get("type") == "FLIP_RETRIEVED_BYTE"
                        and self.bundle.version == "1.0.1"):
                    if (set(fault) & {"beforeStep", "repeat"}
                            or not isinstance(fault.get("afterStep"), str)
                            or steps[named].get("op") != "O1_RETRIEVE"):
                        raise ContractError(
                            "FLIP_RETRIEVED_BYTE requires one afterStep O1_RETRIEVE boundary")

    def _check_verify_no_unresolved_static_expression_remains(self) -> None:
        for scenario in self.bundle.catalog["scenarios"]:
            static = deepcopy(scenario)
            static["materialization"]["steps"] = [self._prepare_references(step) for step in scenario["materialization"]["steps"]]
            static = self._resolve_static(static)
            static["materialization"]["steps"] = [self._apply_json_edits(step) for step in static["materialization"]["steps"]]
            text = json.dumps(static, default=lambda value: "<bytes>" if isinstance(value, bytes) else str(value))
            if "${deployment." in text or "${constants." in text:
                raise ContractError("AIC_RUNNER_UNRESOLVED_EXPRESSION")

    def _check_verify_each_r1_dme_publish_has_exactly_one_evidence_record(self) -> None:
        for scenario in self.bundle.catalog["scenarios"]:
            for step in scenario["materialization"]["steps"]:
                if step["op"] == "R1_DME_PUBLISH" and not ("evidenceRecordRef" in step or "forEachRecordRef" in step):
                    raise ContractError("R1_DME_PUBLISH has no individual record")

    def _resolve_static(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._resolve_static(item) for item in value]
        if isinstance(value, dict):
            return {key: self._resolve_static(item) for key, item in value.items()}
        if not isinstance(value, str) or "${steps." in value:
            return deepcopy(value)
        return resolve(value, {"deployment": self._resolution_deployment(),
                               "constants": self.bundle.catalog["constants"], "steps": {}})

    def _deployment_policy_ue_id(self) -> dict[str, Any]:
        """Expand the deployment vector's compact UE key to PolicyObject shape."""
        deployed = self._scenario_deployment.get("topology", {}).get("ueId")
        if not isinstance(deployed, Mapping):
            raise RunnerError("deployment topology UE identity is absent")
        gu_amf = deployed.get("guAmfUeNgapId")
        if isinstance(gu_amf, Mapping):
            return deepcopy(dict(deployed))
        if not isinstance(gu_amf, int) or isinstance(gu_amf, bool):
            raise RunnerError("deployment guAmfUeNgapId is invalid")
        template = deepcopy(
            self.bundle.fixture("fixture://policy")["scope"]["ueId"])
        structured = template.get("guAmfUeNgapId")
        if not isinstance(structured, dict):
            raise RunnerError("policy fixture has no structured guAmfUeNgapId")
        structured["amfUeNgapId"] = gu_amf
        return template

    def _resolution_deployment(self) -> dict[str, Any]:
        deployment = deepcopy(self._scenario_deployment)
        deployment["topology"]["ueId"] = self._deployment_policy_ue_id()
        return deployment

    def _live_observed_deployment(self) -> dict[str, Any]:
        """Move live-only time windows to now while preserving their durations."""
        deployment = deepcopy(self.vector)
        now = datetime.now(timezone.utc).replace(microsecond=0)

        validity = deployment["policy"]["validity"]
        not_before = datetime.fromisoformat(
            validity["notBefore"].replace("Z", "+00:00")).astimezone(timezone.utc)
        expires_at = datetime.fromisoformat(
            validity["expiresAt"].replace("Z", "+00:00")).astimezone(timezone.utc)
        duration = expires_at - not_before
        if duration <= timedelta(0):
            raise RunnerError("deployment policy validity interval is not positive")
        live_start = now - duration / 2
        live_end = live_start + duration

        window = deployment["o1"]["live"]["expectedMeasurementWindow"]
        window_start = datetime.fromisoformat(
            window["start"].replace("Z", "+00:00")).astimezone(timezone.utc)
        window_end = datetime.fromisoformat(
            window["end"].replace("Z", "+00:00")).astimezone(timezone.utc)
        granularity = window_end - window_start
        if granularity <= timedelta(0):
            raise RunnerError("deployment live measurement window is not positive")
        live_window_end = now
        live_window_start = live_window_end - granularity

        def stamp(value: datetime) -> str:
            return value.isoformat(timespec="seconds").replace("+00:00", "Z")

        validity.update({"notBefore": stamp(live_start), "expiresAt": stamp(live_end)})
        window.update({"start": stamp(live_window_start), "end": stamp(live_window_end)})
        recovery_files = deployment["o1"]["live"].get("recoveryFiles", {})
        candidates: list[dict[str, Any]] = []
        unique = recovery_files.get("uniqueCandidate")
        if isinstance(unique, dict):
            candidates.append(unique)
        ambiguous = recovery_files.get("ambiguousCandidates", [])
        if isinstance(ambiguous, list):
            candidates.extend(item for item in ambiguous if isinstance(item, dict))
        for info in candidates:
            ready = datetime.fromisoformat(
                str(info["fileReadyTime"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            expiration = datetime.fromisoformat(
                str(info["fileExpirationTime"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            live_ready = live_window_end + (ready - window_end)
            info.update({
                "fileReadyTime": stamp(live_ready),
                "fileExpirationTime": stamp(
                    live_ready + (expiration - ready)),
            })
        return deployment

    def _fixture_values(self, value: Any) -> Any:
        """Resolution stages 1 and 2: fixture URIs, then embedded fixture objects."""
        if isinstance(value, str) and value.startswith("fixture://"):
            return self.bundle.fixture(value)
        if isinstance(value, list):
            return [self._fixture_values(item) for item in value]
        if isinstance(value, dict):
            if set(value) == {"$fixtureRef"}:
                reference = value["$fixtureRef"]
                if not isinstance(reference, str):
                    raise RunnerError("$fixtureRef must be a fixture URI")
                return self.bundle.fixture(reference)
            return {key: self._fixture_values(item) for key, item in value.items()}
        return deepcopy(value)

    def _prepare_references(self, raw_step: Mapping[str, Any]) -> dict[str, Any]:
        step = self._fixture_values(raw_step)
        aliases = {
            "bodyRef": "body", "evidenceRecordRef": "body", "statusRef": "status",
            "policyRef": "policy", "manifestRef": "manifest",
            "statusBodyRef": "statusBody", "artifactRef": "artifact", "notificationRef": "notification",
            "inventoryRef": "inventory", "fileContextRef": "fileContext",
            "actionOccurredAtRef": "actionOccurredAt", "expectedDigestRef": "expectedDigest",
        }
        for reference_name, value_name in aliases.items():
            if reference_name in step:
                if value_name in step:
                    raise RunnerError("both %s and %s are present" % (reference_name, value_name))
                step[value_name] = deepcopy(step[reference_name])
        if "jsonPatch" in step:
            target_name = self._json_edit_target(step)
            if target_name is None:
                raise RunnerError("jsonPatch has no JSON target")
            # Fixture-backed and inline JSON is edited at stage 3, before
            # canonical expressions. An expression that supplies the entire
            # target is necessarily deferred until that target exists.
            target = step[target_name]
            if not (isinstance(target, str)
                    and (target.startswith("${") or target.startswith("fixture://"))):
                step = self._apply_json_edits(step)
        if "xmlEdits" in step:
            if "artifact" not in step:
                raise RunnerError("xmlEdits has no XML artifact target")
            step["artifact"] = self._xml_edits(step["artifact"], step["xmlEdits"])
        return step

    @staticmethod
    def _json_edit_target(step: Mapping[str, Any]) -> str | None:
        """Select the referenced JSON document, never its operation wrapper.

        In particular, A1 seed/update policy patches are rooted at the bare
        PolicyObject.  ``status`` is deliberately considered after ``policy``
        because A1_SEED_RESOURCE carries both the policy target and a status
        selector in the same initial-state operation.
        """
        return next((name for name in (
            "body", "policy", "manifest", "status", "notification", "inventory",
        ) if name in step), None)

    def _apply_json_edits(self, step: Mapping[str, Any]) -> dict[str, Any]:
        prepared = deepcopy(dict(step))
        if "jsonPatch" in prepared:
            target_name = self._json_edit_target(prepared)
            if target_name is None:
                raise RunnerError("jsonPatch has no JSON target")
            prepared[target_name] = self._json_patch(prepared[target_name], prepared["jsonPatch"])
            del prepared["jsonPatch"]
        return prepared

    @staticmethod
    def _json_parent(document: Any, path: str, *, allow_root: bool = False) -> tuple[Any, str]:
        if path == "" and allow_root:
            return None, ""
        if not isinstance(path, str) or not path.startswith("/"):
            raise RunnerError("invalid RFC 6902 path: %r" % path)
        tokens = path[1:].split("/")
        current = document
        for raw in tokens[:-1]:
            token = raw.replace("~1", "/").replace("~0", "~")
            try:
                current = current[int(token)] if isinstance(current, list) else current[token]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise RunnerError("RFC 6902 path does not resolve: %s" % path) from exc
        return current, tokens[-1].replace("~1", "/").replace("~0", "~")

    @classmethod
    def _json_patch(cls, value: Any, operations: Any) -> Any:
        if not isinstance(operations, list):
            raise RunnerError("jsonPatch must be an array")
        document = deepcopy(value)
        for operation in operations:
            if not isinstance(operation, dict) or not isinstance(operation.get("op"), str):
                raise RunnerError("invalid RFC 6902 operation")
            name, path = operation["op"], operation.get("path")
            if not isinstance(path, str):
                raise RunnerError("RFC 6902 operation path is absent")
            if name in {"move", "copy"}:
                source_path = operation.get("from")
                if not isinstance(source_path, str):
                    raise RunnerError("RFC 6902 %s requires from" % name)
                source = pointer(document, "#" + source_path)
                if name == "move":
                    document = cls._json_patch(document, [{"op": "remove", "path": source_path}])
                document = cls._json_patch(document, [{"op": "add", "path": path, "value": source}])
                continue
            if name == "test":
                if pointer(document, "#" + path) != operation.get("value"):
                    raise RunnerError("RFC 6902 test failed: %s" % path)
                continue
            parent, token = cls._json_parent(document, path, allow_root=True)
            if parent is None:
                if name in {"add", "replace"}:
                    document = deepcopy(operation.get("value"))
                elif name == "remove":
                    raise RunnerError("cannot remove the document root")
                else:
                    raise RunnerError("unsupported RFC 6902 operation: %s" % name)
                continue
            if name == "add":
                new_value = deepcopy(operation.get("value"))
                if isinstance(parent, list):
                    if token == "-": parent.append(new_value)
                    else:
                        try:
                            index = int(token)
                            if index < 0 or index > len(parent):
                                raise IndexError(index)
                            parent.insert(index, new_value)
                        except (ValueError, IndexError) as exc: raise RunnerError("invalid RFC 6902 array add") from exc
                elif isinstance(parent, dict):
                    parent[token] = new_value
                else:
                    raise RunnerError("RFC 6902 add parent is not a container")
            elif name in {"remove", "replace"}:
                try:
                    if isinstance(parent, list):
                        index = int(token)
                        if name == "remove": parent.pop(index)
                        else: parent[index] = deepcopy(operation.get("value"))
                    elif isinstance(parent, dict):
                        if token not in parent: raise KeyError(token)
                        if name == "remove": del parent[token]
                        else: parent[token] = deepcopy(operation.get("value"))
                    else: raise TypeError(token)
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    raise RunnerError("RFC 6902 target does not exist: %s" % path) from exc
            else:
                raise RunnerError("unsupported RFC 6902 operation: %s" % name)
        return document

    @staticmethod
    def _xml_edits(value: Any, edits: Any) -> bytes:
        if not isinstance(value, (bytes, bytearray, str)) or not isinstance(edits, list):
            raise RunnerError("xmlEdits requires XML bytes and an edit array")
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError as exc:
            raise RunnerError("XML fixture is invalid") from exc
        for edit in edits:
            if not isinstance(edit, dict) or not isinstance(edit.get("xpath"), str) or "replaceText" not in edit:
                raise RunnerError("invalid XML edit")
            xpath = edit["xpath"]
            attribute = None
            attribute_match = re.search(r"/@([^/]+)$", xpath)
            if attribute_match:
                attribute = attribute_match.group(1)
                xpath = xpath[:attribute_match.start()]
            if xpath.endswith("/text()"):
                xpath = xpath[:-7]
            parts = xpath.lstrip("/").split("/")
            if parts and parts[0].split(":")[-1] == root.tag.split("}")[-1]:
                parts = parts[1:]
            query = "." + ("//" if not parts else "/") + "/".join(parts)
            try:
                matches = root.findall(query, edit.get("namespace", {}))
            except (KeyError, SyntaxError) as exc:
                raise RunnerError("unsupported XML edit xpath: %s" % edit["xpath"]) from exc
            if len(matches) != 1:
                raise RunnerError("XML edit must match exactly one target: %s" % edit["xpath"])
            if attribute is not None:
                if attribute not in matches[0].attrib:
                    raise RunnerError("XML edit attribute does not exist: %s" % edit["xpath"])
                matches[0].set(attribute, str(edit["replaceText"]))
            else:
                matches[0].text = str(edit["replaceText"])
        rendered = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
        stylesheet = b'<?xml-stylesheet type="text/xsl" href="MeasDataCollection.xsl"?>'
        if stylesheet in raw and stylesheet not in rendered:
            declaration_end = rendered.find(b"?>")
            rendered = (rendered[:declaration_end + 2] + b"\n" + stylesheet
                        + rendered[declaration_end + 2:])
        return rendered

    def verify_compact_oracle(self) -> None:
        expectations = self.bundle.fixture("fixture://golden#/scenarioExpectations")
        mapping = self.bundle.catalog["compactOracleConsistency"]["fieldMap"]
        for scenario in self.bundle.catalog["scenarios"]:
            for compact_id in scenario.get("compactExpectationIds", []):
                matches = [item for item in expectations if item.get("id") == compact_id]
                if len(matches) != 1:
                    raise PreflightError("RULE-COMPACT-ORACLE-CONSISTENCY: %s" % compact_id)
                for expected_path, rule in mapping.items():
                    field = rule["compactField"]
                    expected = self._field(scenario, expected_path)
                    compact = matches[0].get(field)
                    if expected is not None and compact is not None and self._compact_value(expected, rule["transform"]) != compact:
                        raise PreflightError("RULE-COMPACT-ORACLE-CONSISTENCY: %s %s" % (compact_id, expected_path))

    @staticmethod
    def _field(value: Mapping[str, Any], path: str) -> Any:
        current: Any = value
        for part in path.split("."):
            current = current.get(part) if isinstance(current, dict) else None
        return current

    @staticmethod
    def _compact_value(value: Any, transform: str) -> Any:
        if transform == "PRESENT_OR_UNCHANGED_IS_TRUE_ABSENT_OR_DELETED_IS_FALSE":
            return value in {"PRESENT", "UNCHANGED"}
        return value

    def run(self, scenario: Mapping[str, Any]) -> ScenarioResult:
        if not self._preflight_complete:
            self.preflight()
        self.verify_compact_oracle()
        scenario = deepcopy(scenario)
        self._scenario_deployment = (
            self._live_observed_deployment()
            if scenario["materialization"]["time"].get("mode") == "LIVE_OBSERVED"
            else self.vector
        )
        scenario["materialization"]["steps"] = [self._prepare_references(step) for step in scenario["materialization"]["steps"]]
        timing = scenario["materialization"]["time"]
        for reference_name, value_name in (
                ("originRef", "origin"), ("evaluationNowRef", "evaluationNow")):
            if value_name not in timing and isinstance(timing.get(reference_name), str):
                timing[value_name] = self.bundle.fixture(timing[reference_name])
        scenario = self._resolve_static(scenario)
        self._materialize_live_windows(scenario["materialization"]["time"])
        # Patches whose target is a deployment expression become concrete here;
        # patches depending on step outputs are finalized immediately before dispatch.
        scenario["materialization"]["steps"] = [self._apply_json_edits(step) if "${steps." not in json.dumps(step, default=str)
                                                   else step for step in scenario["materialization"]["steps"]]
        applicability = scenario.get("applicability")
        if applicability:
            condition = resolve(applicability["when"], {"deployment": self._scenario_deployment, "constants": self.bundle.catalog["constants"], "steps": {}})
            if not isinstance(condition, bool):
                raise PreflightError("scenario applicability must resolve to boolean")
            if not condition:
                result = ScenarioResult(scenario["id"], "SKIPPED_NOT_APPLICABLE", "applicability.when evaluated false")
                result.applicability = {"applicable": condition, "conditionId": applicability.get("conditionId"),
                                        "excludes": deepcopy(applicability.get("excludes", []))}
                self._materialize(result, scenario, result.applicability)
                return result
        result = ScenarioResult(scenario["id"], "PASS")
        result.applicability = {"applicable": True, "conditionId": applicability.get("conditionId") if applicability else None,
                                "excludes": deepcopy(applicability.get("excludes", [])) if applicability else []}
        self._scenario_started = time.monotonic()
        self._accepted_evidence_digests = set()
        self._derived_observations = {}
        self._step_faults = {}
        self._transport_outcomes = {}
        self._prearmed_callback_steps = set()
        try:
            self._install_initial_state(scenario)
            faults = scenario["materialization"].get("faults", [])
            pending: tuple[Mapping[str, Any], Future[dict[str, Any]], list[Mapping[str, Any]]] | None = None

            def complete_step(step: Mapping[str, Any], outputs: dict[str, Any],
                              in_flight_faults: list[Mapping[str, Any]]) -> None:
                result.outputs[step["id"]] = outputs
                for fault in in_flight_faults:
                    # Target-owned crash faults were already armed and consumed
                    # during dispatch.  Only response loss still needs its
                    # runner-visible exchange rewritten after completion.
                    if fault.get("type") == "DROP_HTTP_RESPONSE":
                        self._schedule_faults([fault], "afterStep", step["id"], result)

            with ThreadPoolExecutor(max_workers=1) as executor:
                steps = scenario["materialization"]["steps"]
                for step_index, step in enumerate(steps):
                    if pending is not None:
                        deferred, future, deferred_faults = pending
                        if (isinstance(deferred.get("responseAtMs"), int)
                                and isinstance(step.get("atMs"), int)
                                and step["atMs"] > deferred["responseAtMs"]):
                            complete_step(deferred, future.result(), deferred_faults)
                            pending = None

                    if step.get("op") == "A1_EMIT_STATUS" and step_index + 1 < len(steps):
                        callback_step_id = steps[step_index + 1].get("id")
                        for fault in faults:
                            if (fault.get("type") == "DROP_CALLBACK_DELIVERY"
                                    and fault.get("beforeStep") == callback_step_id):
                                boundary = {key: value for key, value in fault.items()
                                            if key != "type"}
                                self.harness.schedule_fault(fault["type"], boundary)
                                self._prearmed_callback_steps.add(str(callback_step_id))
                    self._step_faults[step["id"]] = [
                        dict(fault) for fault in faults
                        if (fault.get("beforeStep") == step["id"]
                            or (fault.get("type") == "FLIP_RETRIEVED_BYTE"
                                and fault.get("afterStep") == step["id"]))]
                    self._schedule_faults(faults, "beforeStep", step["id"], result)
                    in_flight_faults = [
                        fault for fault in faults
                        if fault.get("afterStep") == step["id"]
                        and fault.get("type") in {"DROP_HTTP_RESPONSE", "CRASH_PROCESS"}
                    ]
                    # afterStep names a target-owned internal boundary.  Arm
                    # every such fault before dispatch; transport faults also
                    # replace the runner-visible response deterministically.
                    after_step_faults = [
                        fault for fault in faults
                        if (fault.get("afterStep") == step["id"]
                            and fault.get("type") != "FLIP_RETRIEVED_BYTE")]
                    for fault in after_step_faults:
                        boundary = {key: value for key, value in fault.items()
                                    if key != "type"}
                        self.harness.schedule_fault(fault["type"], boundary)
                        if fault in in_flight_faults:
                            self._transport_outcomes[step["id"]] = (
                                "DROPPED" if fault["type"] == "DROP_HTTP_RESPONSE"
                                else "CONNECTION_LOST")
                    if "responseAfterStep" in step or "responseAtMs" in step:
                        if pending is not None:
                            raise RunnerError("only one deferred response may be in flight")
                        future = executor.submit(
                            self._run_step, step, result, scenario["materialization"]["time"])
                        pending = (step, future, in_flight_faults)
                    else:
                        outputs = self._run_step(step, result, scenario["materialization"]["time"])
                        complete_step(step, outputs, in_flight_faults)
                    if pending is not None:
                        deferred, future, deferred_faults = pending
                        after_match = deferred.get("responseAfterStep") == step["id"]
                        at_match = (isinstance(deferred.get("responseAtMs"), int)
                                    and isinstance(step.get("atMs"), int)
                                    and step["atMs"] >= deferred["responseAtMs"])
                        if after_match or at_match:
                            complete_step(deferred, future.result(), deferred_faults)
                            pending = None
                if pending is not None:
                    deferred, future, deferred_faults = pending
                    complete_step(deferred, future.result(), deferred_faults)
            state = self.harness.state()
            if not isinstance(state, dict):
                raise RunnerError("harness observable state must be an object")
            result.observations = deepcopy(state)
            result.observations.update(deepcopy(self._derived_observations))
            if (scenario["expected"].get("committedEvidenceRecords")
                    == "EQUALS_VALID_DISTINCT_NRCELLDU_COUNT_AFTER_DEDUP"):
                committed = result.observations.get("pmRecordObjectsCreated")
                if not isinstance(committed, int):
                    raise RunnerError("target did not expose committed PM evidence count")
                result.evidence_commit_count = committed
            self._assert_expected(result, scenario["expected"], scenario.get("rules", []))
        except (RunnerError, ContractError, ExpressionError, HarnessError,
                KeyError, OSError, TypeError, ValueError) as exc:
            result.disposition, result.reason = "FAIL", str(exc)
        self._materialize(result, scenario, result.applicability)
        return result

    def _install_initial_state(self, scenario: Mapping[str, Any]) -> None:
        # The harness must make this operation destructive/isolating; no inherited state is allowed.
        self._loaded_capability = None
        reset = {"scenarioId": scenario["id"]}
        timing = scenario["materialization"]["time"]
        if timing.get("mode") == "FIXED_LOGICAL":
            origin = timing.get("origin")
            if origin is None and isinstance(timing.get("originRef"), str):
                origin = self.bundle.fixture(timing["originRef"])
            evaluation = timing.get("evaluationNow")
            if evaluation is None and isinstance(timing.get("evaluationNowRef"), str):
                evaluation = self.bundle.fixture(timing["evaluationNowRef"])
            if evaluation is None and isinstance(origin, str):
                offsets = [step.get("atMs", 0) for step in scenario["materialization"].get("steps", [])]
                final_offset = max((item for item in offsets if isinstance(item, int)), default=0)
                parsed = datetime.fromisoformat(origin.replace("Z", "+00:00")).astimezone(timezone.utc)
                evaluation = (parsed + timedelta(milliseconds=final_offset)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            if isinstance(evaluation, str):
                reset["logicalNow"] = evaluation
            if isinstance(origin, str):
                reset["logicalOrigin"] = origin
        self.harness.operation("RESET_SCENARIO_STATE", reset)
        recovery_assignment = (
            scenario.get("expected", {}).get("recoveredPolicyId")
            or (self.bundle.catalog["constants"]["policyId"]
                if "assignedPolicyIdCount" in scenario.get("expected", {}) else None)
        )
        if isinstance(recovery_assignment, str):
            # The black-box service still owns ID assignment.  Pinning the next
            # opaque value only makes the contract catalog's first committed
            # assignment reproducible across response-loss and crash recovery.
            self.harness.operation("CONFIGURE_NEXT_POLICY_ID", {
                "scenarioId": scenario["id"],
                "policyId": recovery_assignment,
            })
        for state in scenario["materialization"].get("initialState", []):
            if isinstance(state, str):
                descriptor = self.bundle.runner["initialStates"][state]
                arguments = {"scenarioId": scenario["id"], "initialState": state}
                if state == "LOAD_CAPABILITY":
                    capability = self.bundle.fixture("fixture://capabilityManifest")
                    self._loaded_capability = deepcopy(capability)
                    arguments["capability"] = capability
                    arguments["knownUeScopes"] = self._known_ue_scopes()
                if state == "INSTALL_O1_PROFILE":
                    capability = self.bundle.fixture("fixture://capabilityManifest")
                    self._loaded_capability = deepcopy(capability)
                    arguments["capability"] = capability
                if state == "CONFIGURE_UNIQUE_LIVE_DN_CELL_MAPPING":
                    arguments["cellMappings"] = deepcopy(self._scenario_deployment["topology"]["cellMappings"])
                if state == "INSTALL_ALL_PINNED_R1_SERVICE_PROFILES":
                    arguments.update({
                        "rAppId": self._scenario_deployment["r1"]["rAppId"],
                        "discoveredVersions": deepcopy(
                            scenario["expected"].get("discoveredVersions", {})),
                    })
                if state in {
                        "LOAD_DURABLE_SUBSCRIPTION_FROM_PREVIOUS_201",
                        "LOAD_UNCERTAIN_DURABLE_SUBSCRIPTION_ID",
                        "LOAD_ACTIVE_DURABLE_SUBSCRIPTION",
                        "PROVIDER_PERSISTS_SAME_SUBSCRIPTION",
                        "PROVIDER_HAS_NO_SUCH_SUBSCRIPTION"}:
                    arguments.update({
                        "subscriptionId": self._scenario_deployment["o1"]["fileDataReporting"]["subscriptionId"],
                        "consumerReference": self._scenario_deployment["o1"]["fileDataReporting"]["consumerReference"],
                    })
                if state == "REGISTER_DME_TYPE_AIC_POLICY_EVIDENCE_1_0_0":
                    arguments["registration"] = {
                        "dmeTypeDefinition": {
                            "dmeTypeId": {"namespace": "aic", "name": "policy-evidence", "version": "1.0.0"},
                            "metadata": {"dataCategory": ["PERFORMANCE"]},
                            "dataProductionSchema": self.bundle.fixture("fixture://policyEvidenceFilterSchema"),
                            "dataDeliverySchemas": [{
                                "type": "JSON_SCHEMA",
                                "deliverySchemaId": "aic.policy-evidence.record.schema.1.0.0",
                                "schema": canonicalize(self.bundle.schema(
                                    "aic.policy-evidence.1.0.0.schema.json")).decode("utf-8"),
                            }],
                            "dataDeliveryMechanisms": [{"dataDeliveryMethod": "PUSH_HTTP"}],
                        },
                        "dataAccessEndpoint": self._scenario_deployment["r1"]["dme"]["dataAccessEndpoint"],
                        "dataDeliveryModes": ["CONTINUOUS"],
                    }
                if state == "INSTALL_ACTIVE_PUSH_HTTP_DATA_JOB_AIC_POLICY_EVIDENCE_1_0_0":
                    evidence = self.bundle.fixture("fixture://afterEvidence")
                    scenario_data_job_ids = [
                        step.get("bindings", {}).get("dataJobId")
                        for step in scenario["materialization"].get("steps", [])
                        if step.get("op") == "R1_DME_QUERY"
                    ]
                    data_job_id = next(
                        (value for value in scenario_data_job_ids
                         if isinstance(value, str) and not value.startswith("${")),
                        self._scenario_deployment["r1"]["dme"]["activeDataJobId"],
                    )
                    arguments.update({
                        "deliveryBindingId": self._scenario_deployment["r1"]["dme"]["activeDeliveryBindingId"],
                        "dataJobId": data_job_id,
                        "job": {
                            "dataDeliveryMode": "CONTINUOUS",
                            "dmeTypeId": "aic:policy-evidence:1.0.0",
                            "productionJobDefinition": {
                                "policyTypeId": evidence["correlation"]["policyTypeId"],
                                "policyId": evidence["correlation"]["policyId"],
                                "minimumPolicyRevision": evidence["correlation"]["policyRevision"],
                                "nearRtRicId": self.bundle.fixture(
                                    "fixture://capabilityManifest")["nearRtRicId"],
                            },
                            "dataDeliveryMethod": "PUSH_HTTP",
                            "dataDeliverySchemaId": "aic.policy-evidence.record.schema.1.0.0",
                            "pushDeliveryDetailsHttp": {
                                "dataPushUri": (
                                    self._scenario_deployment["r1"]["dme"]["policyEvidencePushBaseUri"].rstrip("/")
                                    + "/" + self._scenario_deployment["r1"]["dme"]["activeDeliveryBindingId"]),
                            },
                        },
                    })
                self.harness.operation(descriptor["adapterAction"], arguments)
            else:
                prepared = self._prepare_references(state)
                prepared = self._apply_json_edits(self._resolve_static(prepared))
                if prepared["op"] == "LOAD_CAPABILITY":
                    prepared["capability"] = prepared.pop("manifest")
                    self._loaded_capability = deepcopy(prepared["capability"])
                    prepared["knownUeScopes"] = self._known_ue_scopes()
                if prepared["op"] in {
                        "A1_SEED_RESOURCE", "INSTALL_NON_RT_DESIRED_POLICY"} and "policyId" not in prepared:
                    prepared["policyId"] = self.bundle.fixture(
                        "fixture://appliedVerifiedStatus")["aicStatus"]["policyId"]
                self.harness.operation(prepared["op"], {
                    "scenarioId": scenario["id"],
                    **{key: value for key, value in prepared.items() if key != "op"},
                })

    def _materialize_live_windows(self, timing: dict[str, Any]) -> None:
        """Resolve the catalog's named live-window algebra to RFC3339 values."""
        variables = timing.get("windowVariables")
        if timing.get("mode") != "LIVE_OBSERVED" or not isinstance(variables, Mapping):
            return
        base = self._scenario_deployment.get("o1", {}).get("live", {}).get("expectedMeasurementWindow")
        if not isinstance(base, Mapping):
            raise RunnerError("LIVE_OBSERVED window variables require deployment.o1.live.expectedMeasurementWindow")
        try:
            base_start = datetime.fromisoformat(str(base["start"]).replace("Z", "+00:00")).astimezone(timezone.utc)
            base_end = datetime.fromisoformat(str(base["end"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        except (KeyError, ValueError) as exc:
            raise RunnerError("deployment live measurement window is invalid") from exc
        granularity = base_end - base_start
        if granularity <= timedelta(0):
            raise RunnerError("deployment live measurement window is not positive")

        def stamp(value: datetime) -> str:
            return value.isoformat(timespec="seconds").replace("+00:00", "Z")

        materialized: dict[str, Any] = {}
        for name, raw in variables.items():
            if not isinstance(raw, Mapping):
                materialized[name] = deepcopy(raw)
                continue
            value = deepcopy(dict(raw))
            selector = value.get("measurementWindow")
            if selector == "PREVIOUS_EXPECTED_GRANULARITY_WINDOW":
                start, end = base_start - granularity, base_end - granularity
            elif selector == "CURRENT_EXPECTED_GRANULARITY_WINDOW":
                start, end = base_start, base_end
            else:
                start, end = base_start, base_end
            value["measurementWindow"] = {"start": stamp(start), "end": stamp(end)}
            replacements = {
                "MEASUREMENT_WINDOW_END_PLUS_0_MS": stamp(end),
                "MEASUREMENT_WINDOW_END_PLUS_120000_MS": stamp(end + timedelta(seconds=120)),
            }
            for key, item in list(value.items()):
                if isinstance(item, str) and item in replacements:
                    value[key] = replacements[item]
            materialized[name] = value
        timing["windowVariables"] = materialized

    def _schedule_faults(self, faults: list[Mapping[str, Any]], side: str, step_id: str,
                         result: ScenarioResult | None = None) -> None:
        for fault in faults:
            if fault.get(side) == step_id:
                if fault["type"] == "FLIP_RETRIEVED_BYTE":
                    # Runner-owned interposition: captured bytes are mutated
                    # inside _run_o1_retrieve before digesting or parsing.
                    continue
                if fault["type"] == "DROP_HTTP_RESPONSE" and side == "afterStep" and result is not None:
                    response = next((item for item in reversed(result.responses)
                                     if item.get("stepId") == step_id), None)
                    if response is None:
                        raise RunnerError("DROP_HTTP_RESPONSE target did not produce an HTTP response")
                    response.update({"status": "DROPPED", "headers": {}, "body": None,
                                     "locationLastSegment": None})
                    for index in range(len(result.http_sequence) - 1, -1, -1):
                        if isinstance(result.http_sequence[index], int):
                            result.http_sequence[index] = "DROPPED"
                            break
                    result.outputs.pop(step_id, None)
                    continue
                boundary = {key: value for key, value in fault.items() if key != "type"}
                prearmed = (fault["type"] == "DROP_CALLBACK_DELIVERY"
                            and step_id in self._prearmed_callback_steps)
                if not prearmed:
                    self.harness.schedule_fault(fault["type"], boundary)
                if fault["type"] == "TLS_HANDSHAKE_REJECT" and side == "beforeStep":
                    self._transport_outcomes[step_id] = "TLS_HANDSHAKE_FAILED"
                elif fault["type"] == "DROP_CALLBACK_DELIVERY" and side == "beforeStep":
                    self._transport_outcomes[step_id] = "PRE_DISPATCH_DROPPED"

    def _run_step(self, static_step: Mapping[str, Any], result: ScenarioResult, timing: Mapping[str, Any]) -> dict[str, Any]:
        step = resolve(static_step, {"deployment": self._resolution_deployment(),
                                     "constants": self.bundle.catalog["constants"],
                                     "steps": result.outputs}, set(result.outputs))
        step = self._apply_json_edits(step)
        step = self._resolve_legacy_templates(step, timing.get("windowVariables", {}))
        if step.get("op") == "HTTP" and isinstance(step.get("body"), Mapping):
            states = step["body"].get("policyStates")
            snapshots = [value.get("statusSnapshot") for value in result.outputs.values()
                         if isinstance(value, Mapping) and isinstance(value.get("statusSnapshot"), Mapping)]
            if isinstance(states, list) and len(states) == 1 and snapshots:
                step["body"] = deepcopy(step["body"])
                step["body"]["policyStates"][0]["policyStatusObject"] = deepcopy(snapshots[-1])
        time_mode = timing["mode"]
        if time_mode == "LIVE_OBSERVED" and isinstance(step.get("atMs"), int):
            remaining = step["atMs"] / 1000 - (time.monotonic() - self._scenario_started)
            if remaining > 0:
                time.sleep(remaining)
        if time_mode == "LIVE_OBSERVED" and "timeoutMs" not in step:
            capture_operations = {"O1_NOTIFY", "O1_FILE_LIST", "O1_RETRIEVE"}
            step["timeoutMs"] = self._scenario_deployment["timeouts"]["liveCaptureMs" if step["op"] in capture_operations else "defaultStepMs"]
        descriptor = self.bundle.runner["operations"][step["op"]]
        kind = descriptor["kind"]
        if step["op"] == "O1_RETRIEVE":
            if step.get("source") == "CAPTURED_FILE_LOCATION":
                captured_infos = [
                    outputs.get("fileInfoList")
                    for outputs in result.outputs.values()
                    if isinstance(outputs.get("fileInfoList"), list)
                    and outputs["fileInfoList"]
                ]
                if not captured_infos:
                    raise RunnerError("O1_RETRIEVE has no captured FileInfo")
                step = dict(step)
                step["fileInfoFromStep"] = deepcopy(captured_infos[-1][0])
                notification_outputs = [
                    outputs.get("notification")
                    for outputs in result.outputs.values()
                    if isinstance(outputs.get("notification"), Mapping)
                    and outputs.get("fileInfoList") is captured_infos[-1]
                ]
                if notification_outputs:
                    step["fileInfoFromStep"]["_notificationEventTime"] = (
                        notification_outputs[-1].get("eventTime"))
            source_step = step.get("fileInfoFromStep")
            if isinstance(source_step, str):
                source_outputs = result.outputs.get(source_step, {})
                file_infos = source_outputs.get("fileInfoList") if isinstance(source_outputs, Mapping) else None
                index = step.get("fileInfoIndex", 0)
                if not isinstance(file_infos, list) or not isinstance(index, int) or not 0 <= index < len(file_infos):
                    raise RunnerError("O1_RETRIEVE fileInfoFromStep did not select one FileInfo")
                step["fileInfoFromStep"] = deepcopy(file_infos[index])
            if step.get("artifact") == "PROVIDER_FILE_AT_FILE_LOCATION":
                del step["artifact"]
            byte_faults = [
                fault for fault in self._step_faults.get(step["id"], [])
                if fault.get("type") == "FLIP_RETRIEVED_BYTE"]
            outputs = self._run_o1_retrieve(step, byte_faults)
            expected_digest = step.get("expectedDigest")
            if isinstance(expected_digest, str) and outputs["byteSha256"] != expected_digest:
                self._derived_observations.update({
                    "evidenceQuality": "MISSING",
                    "quarantineReason": "RAW_DIGEST_MISMATCH",
                    "xmlParserInvocations": 0,
                })
            elif step.get("xmlEdits") or step.get("requireRuntimeCapabilityValidation") is True:
                validation = self.harness.operation("O1_VALIDATE_RETRIEVED", {
                    "artifactBase64": base64.b64encode(outputs["bytes"]).decode("ascii"),
                })
                self._derived_observations.update(validation)
        elif step["op"] == "O1_NORMALIZE":
            source = result.outputs.get(str(step.get("inputFromStep")), {})
            artifact = source.get("bytes") if isinstance(source, Mapping) else None
            refs = step.get("assertRecordRefs", [])
            expected_records = [self.bundle.fixture(ref) if isinstance(ref, str) else deepcopy(ref)
                                for ref in refs]
            if not isinstance(artifact, (bytes, bytearray)):
                raise RunnerError("O1_NORMALIZE requires captured bytes")
            context = (expected_records[0] if expected_records
                       else self.bundle.fixture("fixture://afterEvidence"))
            dynamic_policy_ids = [value.get("policyId") for value in result.outputs.values()
                                  if isinstance(value, Mapping) and value.get("policyId")]
            correlation = deepcopy(context["correlation"])
            if dynamic_policy_ids:
                correlation["policyId"] = dynamic_policy_ids[-1]
                status_candidates = [
                    candidate["aicStatus"]
                    for outputs in result.outputs.values()
                    for candidate in (outputs.get("statusSnapshot"), outputs.get("body"))
                    if isinstance(candidate, Mapping)
                    and isinstance(candidate.get("aicStatus"), Mapping)
                    and candidate["aicStatus"].get("policyId") == dynamic_policy_ids[-1]
                ]
                if status_candidates:
                    captured_status = status_candidates[-1]
                    control = captured_status.get("control", {})
                    correlation.update({
                        "policyRevision": captured_status.get("policyRevision"),
                        "episodeId": captured_status.get("episodeId"),
                        "transactionId": control.get("transactionId"),
                        "actionId": control.get("actionId"),
                    })
            policy_scope = deepcopy(context["policyScope"])
            dynamic_policies = [
                outputs.get("policyObject") for outputs in result.outputs.values()
                if isinstance(outputs.get("policyObject"), Mapping)
            ]
            if dynamic_policies:
                policy_scope = deepcopy(dynamic_policies[-1].get("scope", policy_scope))
            source_file_info = source.get("fileInfo") if isinstance(source, Mapping) else None
            file_source = self._o1_file_context(
                bytes(artifact), context["source"]["file"])
            if isinstance(source_file_info, Mapping):
                file_source.update({
                    key: deepcopy(value)
                    for key, value in source_file_info.items()
                    if value is not None
                })
            evaluation_now = timing.get("evaluationNow")
            if evaluation_now is None and timing.get("mode") == "LIVE_OBSERVED":
                ready = datetime.fromisoformat(
                    str(file_source["readyAt"]).replace("Z", "+00:00")).astimezone(timezone.utc)
                evaluation_now = (ready + timedelta(seconds=6)).isoformat(
                    timespec="seconds").replace("+00:00", "Z")
            arguments = {
                "artifactBase64": base64.b64encode(bytes(artifact)).decode("ascii"),
                "correlation": correlation,
                "policyScope": policy_scope,
                "fileInfo": {
                    "name": file_source["name"], "readyAt": file_source["readyAt"],
                    "retrievedAt": file_source["retrievedAt"],
                    "jobId": file_source.get(
                        "jobId", context["source"]["perfMetricJobId"]),
                },
                "evaluationNow": evaluation_now,
                "actionOccurredAt": step.get("actionOccurredAt"),
                "clockSkewMs": step.get("clockSkewMs", 0),
            }
            outputs = self.harness.operation("O1_NORMALIZE", arguments)
            dedupe_step = step.get("dedupeAgainstStep")
            if isinstance(dedupe_step, str):
                prior = result.outputs.get(dedupe_step, {}).get("records", [])
                records = outputs.get("records", [])
                prior_digests = {jcs_sha256(item) for item in prior}
                duplicates = [item for item in records if jcs_sha256(item) in prior_digests]
                outputs = dict(outputs)
                outputs["duplicateRecords"] = duplicates
                outputs["commitEligibleRecords"] = [
                    item for item in outputs.get("commitEligibleRecords", [])
                    if jcs_sha256(item) not in prior_digests]
        elif step["op"] == "PERF_METRIC_JOB":
            source_step = step.get("mustOccurAfter")
            if isinstance(source_step, str):
                source = result.outputs.get(source_step)
                if not isinstance(source, Mapping):
                    raise RunnerError("PERF_METRIC_JOB dependency source is unavailable")
                step = dict(step)
                step["dependencySource"] = deepcopy(source)
            outputs = self._run_netconf(step)
        elif kind.startswith("NETWORK"):
            outputs = self._run_http_operation(step, result)
        elif step["op"] == "PROCESS_RESTART":
            outputs = self.harness.restart(step["component"])
        else:
            arguments = {key: value for key, value in step.items()
                         if key not in {"id", "op", "capture", "timeoutMs"}}
            source_step = step.get("sourceStep")
            if step["op"] == "SET_DEPENDENCY" and isinstance(source_step, str):
                source = result.outputs.get(source_step)
                if not isinstance(source, Mapping):
                    raise RunnerError("SET_DEPENDENCY sourceStep has no captured outputs")
                arguments["source"] = deepcopy(source)
            outputs = self.harness.operation(step["op"], arguments)
            if step["op"] == "SET_DEPENDENCY" and isinstance(
                    outputs.get("statusSnapshot"), Mapping):
                attempts = outputs.get("callbackAttempts")
                if not isinstance(attempts, list) or not attempts:
                    raise RunnerError(
                        "automatic dependency status produced no observed callback attempt")
                endpoint = self._endpoint({
                    "endpointRef": "#/endpointTemplates/a1StatusCallbackRoot",
                    "bindings": {},
                })
                for attempt, outcome in enumerate(attempts):
                    result.requests.append({
                        "stepId": step["id"], "attempt": attempt + 1,
                        "method": "POST", "url": endpoint, "headers": {},
                        "body": deepcopy(outputs["statusSnapshot"]),
                    })
                    result.http_sequence.append(outcome)
                    result.responses.append({
                        "stepId": step["id"], "attempt": attempt + 1,
                        "status": outcome, "headers": {}, "body": None,
                        "locationLastSegment": None,
                    })
        declared = set(descriptor.get("declaredOutputs", [])) | set(step.get("capture", {}))
        if not set(outputs).issubset(set(declared)):
            raise RunnerError("boundary returned undeclared output for %s: %s" % (step["op"], sorted(set(outputs) - set(declared))))
        combined = outputs if kind.startswith("NETWORK") else self._capture(step, {"operation": outputs}) | outputs
        self._collect_counts(step, combined, result)
        return combined

    def _o1_file_context(self, artifact: bytes, fallback: Mapping[str, Any]) -> dict[str, Any]:
        """Derive deterministic retrieval metadata for exact bundle PM files."""
        known = {
            self.bundle.fixture("fixture://validPrbXml"): dict(fallback),
            self.bundle.fixture("fixture://nullPrbXml"): {
                "name": "null.xml", "readyAt": "2026-08-04T00:04:02Z",
                "retrievedAt": "2026-08-04T00:04:04Z"},
            self.bundle.fixture("fixture://suspectPrbXml"): {
                "name": "suspect.xml", "readyAt": "2026-08-04T00:06:02Z",
                "retrievedAt": "2026-08-04T00:06:04Z"},
        }
        return deepcopy(known.get(artifact, dict(fallback)))

    def _endpoint(self, step: Mapping[str, Any]) -> str:
        reference = step["endpointRef"].removeprefix("#/endpointTemplates/")
        template = self.bundle.catalog["endpointTemplates"].get(reference)
        roots = self.bundle.runner["expressionLanguage"]["endpointTemplateResolution"]["rootMappings"]
        if template is None and reference in roots:
            direct = resolve(roots[reference], {
                "deployment": self._scenario_deployment,
                "constants": self.bundle.catalog["constants"],
                "steps": {},
            })
            if not isinstance(direct, str):
                raise RunnerError("endpoint root is not a string: %s" % reference)
            return direct
        if not isinstance(template, str):
            raise RunnerError("unknown endpoint template: %s" % reference)
        if self.bundle.version == "1.0.1":
            raw_bindings = step.get("bindings", {})
            if not isinstance(raw_bindings, Mapping):
                raise RunnerError("endpoint bindings must be an object")
            bindings = dict(raw_bindings)
        else:
            # Historical 1.0.0 scenarios used top-level scalar aliases.
            bindings = {key: value for key, value in step.items()
                        if isinstance(value, (str, int, float, bool))}
            bindings.update(step.get("bindings", {}))
        required_names = set(re.findall(r"\{([^{}]+)\}", template))
        for name, expression in roots.items():
            if name in required_names:
                bindings[name] = resolve(expression, {"deployment": self._scenario_deployment, "constants": self.bundle.catalog["constants"], "steps": {}})
        def replace(match: re.Match[str]) -> str:
            value = bindings.get(match.group(1))
            if not isinstance(value, (str, int, float, bool)):
                raise RunnerError("endpoint template binding is absent or non-scalar: %s" % match.group(1))
            return str(value)
        endpoint = re.sub(r"\{([^{}]+)\}", replace, template)
        query = step.get("query")
        if query is not None:
            if not isinstance(query, Mapping):
                raise RunnerError("query must be an object")
            endpoint += ("&" if "?" in endpoint else "?") + urlencode(list(query.items()), doseq=True)
        return endpoint

    def _resolve_legacy_templates(self, value: Any, window_variables: Mapping[str, Any]) -> Any:
        if isinstance(value, list):
            return [self._resolve_legacy_templates(item, window_variables) for item in value]
        if isinstance(value, dict):
            return {
                key: deepcopy(item) if key == "profileRef"
                else self._resolve_legacy_templates(item, window_variables)
                for key, item in value.items()
            }
        if not isinstance(value, str) or not re.search(r"\{[^{}]+\}", value):
            return deepcopy(value)
        roots = self.bundle.runner["expressionLanguage"]["endpointTemplateResolution"]["rootMappings"]
        names: dict[str, Any] = {}
        for name, expression in roots.items():
            names[name] = resolve(expression, {"deployment": self._scenario_deployment, "constants": self.bundle.catalog["constants"], "steps": {}})
        def flatten(prefix: str, item: Any) -> None:
            if isinstance(item, Mapping):
                if prefix:
                    names[prefix] = deepcopy(item)
                for key, child in item.items():
                    flatten((prefix + "." if prefix else "") + str(key), child)
            else:
                names[prefix] = item
        flatten("", window_variables)
        whole = re.fullmatch(r"\{([^{}]+)\}", value)
        if whole and whole.group(1) in names:
            return deepcopy(names[whole.group(1)])
        def replacement(match: re.Match[str]) -> str:
            name = match.group(1)
            replacement_value = names.get(name)
            if not isinstance(replacement_value, (str, int, float, bool)):
                raise RunnerError("legacy template binding is absent or non-scalar: %s" % name)
            return str(replacement_value)
        return re.sub(r"\{([^{}]+)\}", replacement, value)

    def _run_http(self, step: Mapping[str, Any], result: ScenarioResult) -> dict[str, Any]:
        endpoint = self._endpoint(step)
        body = step.get("body")
        absent = list(step.get("assertRequestBodyAbsent", [])) + list(step.get("payloadMustNotContain", []))
        if absent and not isinstance(body, Mapping):
            raise RunnerError("request-body absence assertion requires an object body")
        for member in absent:
            if member in body:
                raise RunnerError("request body contains forbidden member: %s" % member)
        forbidden_headers = {str(name).lower() for name in step.get("headersMustNotContain", [])}
        actual_header_names = {str(name).lower() for name in step.get("headers", {})}
        if forbidden_headers & actual_header_names:
            raise RunnerError("request contains forbidden header: %s" % sorted(forbidden_headers & actual_header_names)[0])
        result.requests.append({"stepId": step["id"], "method": step["method"], "url": endpoint,
                                "headers": deepcopy(step.get("headers", {})), "body": deepcopy(body)})
        pending_outcome = self._transport_outcomes.get(step["id"])
        if pending_outcome == "PRE_DISPATCH_DROPPED":
            observed_state = self.harness.state()
            if observed_state.get("r1CallbackDroppedCount", 0) < 1:
                raise RunnerError("callback drop was not observed at the target boundary")
            self._transport_outcomes.pop(step["id"])
            result.http_sequence.append("DROPPED")
            outputs = {"status": "DROPPED", "headers": {}, "body": None,
                       "locationLastSegment": None}
            result.responses.append({"stepId": step["id"], **deepcopy(outputs)})
            return outputs
        observer = getattr(self.harness, "captured_http", None)
        if step.get("runnerMode") == "CAPTURE_AND_RESPOND" and callable(observer):
            captured = observer(step["method"], endpoint, body)
            response = HttpResponse(int(captured["status"]), dict(captured.get("headers", {})),
                                    deepcopy(captured.get("body")))
        else:
            try:
                response = self.http.request(
                    step["method"], endpoint, step.get("headers", {}), body,
                    step.get("timeoutMs"))
            except OSError:
                outcome = self._transport_outcomes.pop(step["id"], None)
                if outcome is None:
                    raise
                if outcome == "PRE_DISPATCH_DROPPED":
                    outcome = "DROPPED"
                result.http_sequence.append(outcome)
                outputs = {"status": outcome, "headers": {}, "body": None,
                           "locationLastSegment": None}
                result.responses.append({"stepId": step["id"], **deepcopy(outputs)})
                return outputs
        post_dispatch_outcome = self._transport_outcomes.pop(step["id"], None)
        if post_dispatch_outcome in {"DROPPED", "CONNECTION_LOST"}:
            result.http_sequence.append(post_dispatch_outcome)
            outputs = {"status": post_dispatch_outcome, "headers": {}, "body": None,
                       "locationLastSegment": None}
            result.responses.append({"stepId": step["id"], **deepcopy(outputs)})
            return outputs
        result.http_sequence.append(response.status)
        expected = step.get("expectedHttpStatus")
        if expected is not None and response.status != expected:
            raise RunnerError("HTTP status %s != expected %s" % (response.status, expected))
        outputs = {"status": response.status, "headers": dict(response.headers), "body": deepcopy(response.body),
                   "locationLastSegment": self._location_segment(response.headers.get("Location", "")) if response.headers.get("Location") else None}
        self._assert_http_response(step, outputs)
        result.responses.append({"stepId": step["id"], **deepcopy(outputs)})
        return outputs | self._capture(step, {"response": outputs})

    def _assert_http_response(self, step: Mapping[str, Any], response: Mapping[str, Any]) -> None:
        headers = response["headers"]
        body = response["body"]
        for name, expected in step.get("assertResponseHeaders", {}).items():
            actual = headers.get(name)
            if isinstance(expected, str) and expected.startswith("#/endpointTemplates/"):
                if not isinstance(actual, str):
                    raise RunnerError("required response header is absent: %s" % name)
                assertion_step = {"endpointRef": expected, "bindings": {**step.get("bindings", {}), **step.get("capture", {}),
                                                                         "serviceApiId": response.get("locationLastSegment"),
                                                                         "registrationId": response.get("locationLastSegment"),
                                                                         "dataJobId": response.get("locationLastSegment"),
                                                                         "policyId": response.get("locationLastSegment"),
                                                                         "subscriptionId": response.get("locationLastSegment")}}
                expected_path = urlparse(self._endpoint(assertion_step)).path
                if urlparse(actual).path != expected_path:
                    raise RunnerError("response header %s does not match endpoint template" % name)
            elif actual != expected:
                raise RunnerError("response header %s differs" % name)
        if "assertBodyApiId" in step:
            if not isinstance(body, Mapping) or body.get("apiId") != step["assertBodyApiId"]:
                raise RunnerError("response apiId differs")
        body_assertions = step.get("assertResponseBody", {})
        if body_assertions.get("apiIdPresent") and (not isinstance(body, Mapping) or not body.get("apiId")):
            raise RunnerError("response apiId is absent")
        if body_assertions.get("apiIdEqualsLocationLastSegment") and (
                not isinstance(body, Mapping) or body.get("apiId") != response.get("locationLastSegment")):
            raise RunnerError("response apiId differs from Location")
        if "assertDmeTypeId" in step:
            observed = body.get("dmeTypeId") if isinstance(body, Mapping) else None
            if observed is None and isinstance(body, list) and len(body) == 1 and isinstance(body[0], Mapping):
                observed = body[0].get("dmeTypeDefinition", {}).get("dmeTypeId")
            if isinstance(observed, Mapping):
                observed = ":".join(str(observed.get(key, "")) for key in ("namespace", "name", "version"))
            if observed != step["assertDmeTypeId"]:
                raise RunnerError("response dmeTypeId differs")
        if "assertAcceptedPushPayloadCount" in step:
            observed = body.get("acceptedPushPayloadCount") if isinstance(body, Mapping) else None
            if observed != step["assertAcceptedPushPayloadCount"]:
                raise RunnerError("accepted push payload count differs")

    def _run_http_operation(self, step: Mapping[str, Any], result: ScenarioResult) -> dict[str, Any]:
        operation = step["op"]
        normalized = deepcopy(dict(step))
        if operation == "HTTP":
            return self._run_http(normalized, result)
        defaults = {
            "A1_EMIT_STATUS": ("POST", "#/endpointTemplates/a1StatusCallbackRoot"),
            "O1_NOTIFY": ("POST", "#/endpointTemplates/o1NotificationRecipient"),
            "O1_FILE_LIST": ("GET", normalized.get("endpointRef")),
            "R1_DME_REGISTER": (normalized.get("method", "POST"), normalized.get("endpointRef")),
            "R1_DME_DISCOVER": (normalized.get("method", "GET"), normalized.get("endpointRef")),
            "R1_DME_DATA_JOB": (normalized.get("method", "GET"), normalized.get("endpointRef")),
            "R1_DME_PUBLISH": ("POST", normalized.get("endpointRef")),
            "R1_DME_QUERY": ("GET", normalized.get("endpointRef")),
        }
        if operation not in defaults:
            raise RunnerError("network operation has no boundary mapping: %s" % operation)
        method, endpoint_ref = defaults[operation]
        normalized["method"] = method
        if operation == "A1_EMIT_STATUS":
            # Near-RT emits A1 PolicyStatusObject to the Non-RT callback.  The
            # subsequent R1 wrapper delivery to the rApp is a separate step.
            normalized["endpointRef"] = "#/endpointTemplates/a1StatusCallbackRoot"
            declared = normalized.get("status")
            observed = self.harness.state().get("statusBody")
            normalized["body"] = deepcopy(declared if isinstance(declared, Mapping)
                                           else observed)
            if not isinstance(normalized["body"], Mapping):
                raise RunnerError("A1_EMIT_STATUS has no status snapshot")
            dynamic_policy_ids = [
                outputs.get("policyId") for outputs in result.outputs.values()
                if isinstance(outputs, Mapping) and isinstance(outputs.get("policyId"), str)
            ]
            if dynamic_policy_ids and isinstance(
                    normalized["body"].get("aicStatus"), Mapping):
                normalized["body"] = deepcopy(normalized["body"])
                normalized["body"]["aicStatus"]["policyId"] = dynamic_policy_ids[-1]
            emission = self.harness.operation(
                "A1_EMIT_STATUS", {
                    "status": normalized["body"],
                    "replay": normalized.get("replay", False),
                })
            asserted_history = normalized.get("assertStatusHistorySeqs")
            if asserted_history is not None:
                if (not isinstance(asserted_history, list)
                        or any(not isinstance(value, int)
                               or isinstance(value, bool)
                               for value in asserted_history)):
                    raise RunnerError(
                        "assertStatusHistorySeqs must be an integer array")
                history = self.harness.state().get("statusHistory")
                if (not isinstance(history, list)
                        or any(not isinstance(item, Mapping)
                               or not isinstance(item.get("aicStatus"), Mapping)
                               for item in history)):
                    raise RunnerError(
                        "persisted status history sequence was not observed")
                asserted_aic = normalized["body"]["aicStatus"]
                observed_history = [
                    item["aicStatus"].get("statusSeq") for item in history
                    if (item["aicStatus"].get("policyId")
                        == asserted_aic.get("policyId")
                        and item["aicStatus"].get("producerEpoch")
                        == asserted_aic.get("producerEpoch"))
                ]
                if observed_history != asserted_history:
                    raise RunnerError(
                        "persisted status history sequence differs: observed %r expected %r"
                        % (observed_history, asserted_history))
            attempts = emission.get("callbackAttempts")
            if not isinstance(attempts, list) or not attempts:
                raise RunnerError("A1_EMIT_STATUS produced no observed callback attempt")
            endpoint = self._endpoint(normalized)
            for attempt, outcome in enumerate(attempts):
                result.requests.append({
                    "stepId": normalized["id"], "attempt": attempt + 1,
                    "method": "POST", "url": endpoint, "headers": {},
                    "body": deepcopy(normalized["body"]),
                })
                result.http_sequence.append(outcome)
                result.responses.append({
                    "stepId": normalized["id"], "attempt": attempt + 1,
                    "status": outcome, "headers": {}, "body": None,
                    "locationLastSegment": None,
                })
            return {
                "statusSnapshot": deepcopy(emission.get("statusSnapshot", normalized["body"])),
                "callbackStatus": attempts[-1],
            }
        else:
            if not isinstance(endpoint_ref, str):
                raise RunnerError("network operation endpointRef is absent: %s" % operation)
            normalized["endpointRef"] = endpoint_ref
        if operation == "O1_NOTIFY":
            live_capture = normalized.get("input") == "LIVE_CAPTURE"
            notification = normalized.get("notification", normalized.get("input"))
            if notification == "LIVE_CAPTURE":
                if normalized.get("afterActionCollection") is True:
                    self._scenario_deployment = deepcopy(
                        self._scenario_deployment)
                    window = self._scenario_deployment[
                        "o1"]["live"]["expectedMeasurementWindow"]
                    old_start = datetime.fromisoformat(
                        window["start"].replace("Z", "+00:00"))
                    old_end = datetime.fromisoformat(
                        window["end"].replace("Z", "+00:00"))
                    duration = old_end - old_start
                    if duration <= timedelta(0):
                        raise RunnerError("live O1 collection window is not positive")
                    action_times = [
                        output.get("statusSnapshot", {}).get(
                            "aicStatus", {}).get("occurredAt")
                        for output in result.outputs.values()
                        if isinstance(output, Mapping)
                        and isinstance(output.get("statusSnapshot"), Mapping)
                        and isinstance(output["statusSnapshot"].get("aicStatus"), Mapping)
                    ]
                    action_times = [value for value in action_times
                                    if isinstance(value, str)]
                    if not action_times:
                        raise RunnerError(
                            "after-action collection requires a prior status action timestamp")
                    action_at = max(datetime.fromisoformat(
                        value.replace("Z", "+00:00")).astimezone(timezone.utc)
                        for value in action_times)
                    delay_ms = normalized.get("afterActionDelayMs", 1)
                    duration_ms = normalized.get(
                        "collectionDurationMs", int(duration.total_seconds() * 1000))
                    if (not isinstance(delay_ms, int) or isinstance(delay_ms, bool)
                            or delay_ms < 1 or not isinstance(duration_ms, int)
                            or isinstance(duration_ms, bool) or duration_ms < 1):
                        raise RunnerError(
                            "after-action collection timing must use positive integer milliseconds")
                    new_start = action_at + timedelta(milliseconds=delay_ms)
                    new_end = new_start + timedelta(milliseconds=duration_ms)

                    def stamp(value: datetime) -> str:
                        timespec = "seconds" if value.microsecond == 0 else "milliseconds"
                        return value.isoformat(timespec=timespec).replace(
                            "+00:00", "Z")

                    window.update({
                        "start": stamp(new_start),
                        "end": stamp(new_end),
                    })
                notification = self.bundle.fixture("fixture://notifyFileReady")
                allowed = self._scenario_deployment["o1"]["sftp"]["allowedAuthorities"]
                if not isinstance(allowed, list) or not allowed:
                    raise RunnerError("live O1 capture requires an allowed SFTP authority")
                notification = deepcopy(notification)
                original_window = self.vector["o1"]["live"]["expectedMeasurementWindow"]
                live_window = self._scenario_deployment["o1"]["live"]["expectedMeasurementWindow"]
                original_end = datetime.fromisoformat(
                    original_window["end"].replace("Z", "+00:00")).astimezone(timezone.utc)
                live_end = datetime.fromisoformat(
                    live_window["end"].replace("Z", "+00:00")).astimezone(timezone.utc)
                original_event = datetime.fromisoformat(
                    str(notification["eventTime"]).replace("Z", "+00:00")).astimezone(timezone.utc)
                live_event: datetime | None = None
                for info in notification.get("fileInfoList", []):
                    parsed_location = urlparse(str(info.get("fileLocation", "")))
                    info["fileLocation"] = "sftp://%s%s" % (
                        allowed[0], parsed_location.path)
                    info["jobId"] = self._scenario_deployment[
                        "o1"]["perfMetricJob"]["jobId"]
                    original_ready = datetime.fromisoformat(
                        str(info["fileReadyTime"]).replace("Z", "+00:00")).astimezone(timezone.utc)
                    original_expiry = datetime.fromisoformat(
                        str(info["fileExpirationTime"]).replace("Z", "+00:00")).astimezone(timezone.utc)
                    live_ready = live_end + (original_ready - original_end)
                    live_expiry = live_ready + (original_expiry - original_ready)
                    if live_event is None:
                        live_event = live_ready + (original_event - original_ready)
                    info["fileReadyTime"] = live_ready.isoformat(
                        timespec="seconds").replace("+00:00", "Z")
                    info["fileExpirationTime"] = live_expiry.isoformat(
                        timespec="seconds").replace("+00:00", "Z")
                if live_event is not None:
                    notification["eventTime"] = live_event.isoformat(
                        timespec="seconds").replace("+00:00", "Z")
            elif notification == "PROVIDER_GENERATED_SCHEMA_VALID":
                notification = {
                    "notificationType": "notifyFilePreparationError",
                    "eventTime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "windowIdentity": normalized.get("windowIdentity"),
                }
            normalized["body"] = deepcopy(notification)
        elif operation == "R1_DME_PUBLISH":
            normalized["headers"] = {
                "Content-Type": normalized.get("contentType", "application/json"),
                "Version": "1.0.0",
                **normalized.get("headers", {}),
            }
        precreate = normalized.get("preCreate") if operation == "R1_DME_DATA_JOB" else None
        if operation == "O1_FILE_LIST":
            configured = normalized.get("responseBody")
            if not isinstance(configured, list):
                raise RunnerError("O1_FILE_LIST responseBody must materialize to an array")
            self.harness.operation("CONFIGURE_LIVE_PM_PROFILE", {
                "fileInfos": configured,
                "measurementWindow": self._scenario_deployment["o1"]["live"]["expectedMeasurementWindow"],
                "cellMappings": self._provider_cell_mappings(),
                "jobId": self._scenario_deployment["o1"]["perfMetricJob"]["jobId"],
            })
        if operation == "O1_NOTIFY" and live_capture:
            infos = normalized.get("body", {}).get("fileInfoList", [])
            self.harness.operation("CONFIGURE_LIVE_PM_PROFILE", {
                "fileInfos": infos,
                "measurementWindow": self._scenario_deployment["o1"]["live"]["expectedMeasurementWindow"],
                "cellMappings": self._provider_cell_mappings(),
                "jobId": self._scenario_deployment["o1"]["perfMetricJob"]["jobId"],
            })
        if isinstance(precreate, Mapping) and normalized.get("method", method) == "POST":
            self.harness.operation("R1_DME_BINDING_PRECREATE", {
                "deliveryBindingId": precreate["deliveryBindingId"],
                "job": normalized["body"],
            })
        outputs = self._run_http(normalized, result)
        if isinstance(precreate, Mapping) and outputs.get("locationLastSegment"):
            self.harness.operation("R1_DME_BINDING_COMMIT", {
                "deliveryBindingId": precreate["deliveryBindingId"],
                "dataJobId": outputs["locationLastSegment"],
            })
        body = outputs.get("body")
        if isinstance(body, Mapping):
            error_code = body.get("code", body.get("errorCode"))
            if isinstance(error_code, str):
                self._derived_observations["errorCode"] = error_code
        if (operation == "R1_DME_QUERY"
                and normalized.get("requiredSourceInterface") == "O1"
                and isinstance(body, Mapping)
                and body.get("acceptedPushPayloadCount") == 0):
            self._derived_observations.update({
                "errorCode": "AIC_KPI_MISSING",
                "evidenceQuality": "MISSING",
                "assuranceDecision": "FAIL_CLOSED",
            })
        if operation == "O1_NOTIFY" and live_capture and outputs.get("status") == 204:
            file_info = normalized["body"]["fileInfoList"][0]
            if self.sftp is None:
                raise RunnerError("live O1 processing requires an explicit SftpBoundary")
            raw = self.sftp.retrieve(
                file_info["fileLocation"], vector=self._scenario_deployment,
                timeout_ms=normalized.get("timeoutMs"))
            retrieved_at = getattr(self.sftp, "last_retrieved_at", None)
            if not isinstance(retrieved_at, str):
                raise RunnerError("SftpBoundary did not expose retrieval completion time")
            context = self.bundle.fixture("fixture://afterEvidence")
            ready = datetime.fromisoformat(
                str(file_info["fileReadyTime"]).replace("Z", "+00:00")).astimezone(timezone.utc)
            normalized_outputs = self.harness.operation("O1_NORMALIZE", {
                "artifactBase64": base64.b64encode(raw).decode("ascii"),
                "correlation": context["correlation"],
                "policyScope": context["policyScope"],
                "fileInfo": {
                    "name": urlparse(file_info["fileLocation"]).path.rsplit("/", 1)[-1],
                    "readyAt": file_info["fileReadyTime"],
                    "retrievedAt": retrieved_at,
                    "jobId": file_info["jobId"],
                },
                "evaluationNow": (ready + timedelta(seconds=6)).isoformat(
                    timespec="seconds").replace("+00:00", "Z"),
            })
            records = normalized_outputs.get("records", [])
            qualities = {item.get("quality") for item in records if isinstance(item, Mapping)}
            if len(qualities) == 1:
                self._derived_observations["evidenceQuality"] = next(iter(qualities))
        if operation == "O1_NOTIFY":
            return {key: value for key, value in {"status": outputs["status"], "notification": normalized.get("body"),
                                                  "fileInfoList": normalized.get("body", {}).get("fileInfoList") if isinstance(normalized.get("body"), dict) else None}.items()
                    if value is not None}
        if operation == "O1_FILE_LIST":
            return {"status": outputs["status"], "fileInfoList": body}
        mapping = {
            "R1_DME_REGISTER": {"status": outputs["status"], "body": body,
                                "registrationId": outputs.get("registrationId", outputs.get("locationLastSegment"))},
            "R1_DME_DISCOVER": {"status": outputs["status"], "dmeTypes": body},
            "R1_DME_DATA_JOB": {"status": outputs["status"], "body": body,
                                "dataJobId": outputs.get("dataJobId", outputs.get("locationLastSegment")),
                                "dataJobInfoStatus": body.get("dataJobInfoStatus") if isinstance(body, dict) else None},
            "R1_DME_PUBLISH": {"status": outputs["status"],
                               "accepted": body.get("accepted", 200 <= outputs["status"] < 300) if isinstance(body, dict) else 200 <= outputs["status"] < 300,
                               "deduplicated": body.get("deduplicated", False) if isinstance(body, dict) else False},
            "R1_DME_QUERY": {"status": outputs["status"], "body": body},
        }
        return {key: value for key, value in mapping[operation].items() if value is not None}

    def _known_ue_scopes(self) -> list[dict[str, Any]]:
        """Combine contract scenario and deployment UE identities without duplicates."""
        candidates = [
            self.bundle.fixture("fixture://policy").get("scope", {}).get("ueId"),
            self._scenario_deployment.get("topology", {}).get("ueId"),
            self._deployment_policy_ue_id(),
        ]
        scopes: list[dict[str, Any]] = []
        identities: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            identity = jcs_sha256(candidate)
            if identity not in identities:
                scopes.append(deepcopy(dict(candidate)))
                identities.add(identity)
        return scopes

    def _provider_cell_mappings(self) -> list[dict[str, Any]]:
        """Resolve provider PM DNs from the loaded capability by CellId."""
        if self._loaded_capability is None:
            raise RunnerError("provider PM configuration requires a loaded capability")
        capability_cells = self._loaded_capability.get("topology", {}).get("cells", [])
        dn_by_cell = {
            jcs_sha256(item.get("cellId")): item.get("managedObjectDn")
            for item in capability_cells
            if isinstance(item, Mapping)
            and isinstance(item.get("cellId"), Mapping)
            and isinstance(item.get("managedObjectDn"), str)
        }
        resolved = []
        for item in self._scenario_deployment["topology"]["cellMappings"]:
            cell_id = deepcopy(item["cellId"])
            managed_object_dn = dn_by_cell.get(jcs_sha256(cell_id))
            if not isinstance(managed_object_dn, str):
                raise RunnerError("deployment CellId is absent from the loaded capability")
            resolved.append({
                "cellId": cell_id,
                "managedObjectDn": managed_object_dn,
            })
        return resolved

    def _run_o1_retrieve(self, step: Mapping[str, Any],
                         byte_faults: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        artifact = step.get("artifact")
        retrieved_at: str | None = None
        if artifact is not None:
            if not isinstance(artifact, (bytes, bytearray)):
                raise RunnerError("O1 fixture artifact must resolve to bytes")
            payload = bytes(artifact)
        else:
            source = step.get("source")
            if not isinstance(source, str) or source == "CAPTURED_FILE_LOCATION":
                file_info = step.get("fileInfoFromStep")
                source = file_info.get("fileLocation") if isinstance(file_info, Mapping) else None
            if not isinstance(source, str):
                raise RunnerError("O1_RETRIEVE has no concrete SFTP source")
            if self.sftp is None:
                raise RunnerError("O1_RETRIEVE requires an explicit SftpBoundary; harness fallback is forbidden")
            payload = self.sftp.retrieve(source, vector=self._scenario_deployment, timeout_ms=step.get("timeoutMs"))
            retrieved_at = getattr(self.sftp, "last_retrieved_at", None)
            if not isinstance(retrieved_at, str):
                raise RunnerError("SftpBoundary did not expose retrieval completion time")
        faults = list(byte_faults or [])
        if len(faults) > 1:
            raise RunnerError("FLIP_RETRIEVED_BYTE may activate exactly once")
        if faults:
            mutated = bytearray(payload)
            offset = int(faults[0].get("byteOffset", -1))
            if not 0 <= offset < len(mutated):
                raise RunnerError("FLIP_RETRIEVED_BYTE offset is outside artifact")
            mutated[offset] ^= 1
            payload = bytes(mutated)
        context = step.get("fileContext")
        if isinstance(context, Mapping) and "fileReadyTime" in context:
            context = {
                "name": urlparse(str(context.get("fileLocation", ""))).path.rsplit("/", 1)[-1],
                "readyAt": context.get("fileReadyTime"),
                "retrievedAt": retrieved_at,
                "expirationAt": context.get("fileExpirationTime"),
                "notificationEventTime": context.get("_notificationEventTime"),
                "jobId": context.get("jobId"),
            }
        if context is None and isinstance(step.get("fileInfoFromStep"), Mapping):
            selected = step["fileInfoFromStep"]
            context = {
                "name": urlparse(str(selected.get("fileLocation", ""))).path.rsplit("/", 1)[-1],
                "readyAt": selected.get("fileReadyTime"),
                "retrievedAt": retrieved_at,
                "expirationAt": selected.get("fileExpirationTime"),
                "notificationEventTime": selected.get("_notificationEventTime"),
                "jobId": selected.get("jobId"),
            }
        outputs = {"bytes": payload, "byteSha256": hashlib.sha256(payload).hexdigest(),
                   **({"fileInfo": deepcopy(context)} if isinstance(context, Mapping) else {})}
        if step.get("parseRawPmXml") is True:
            try:
                root = ElementTree.fromstring(payload)
                gran = root.find(".//{*}granPeriod")
                if gran is None:
                    raise ValueError("granPeriod is absent")
                end = datetime.fromisoformat(str(gran.get("endTime")).replace("Z", "+00:00")).astimezone(timezone.utc)
                match = re.fullmatch(r"PT(\d+)S", str(gran.get("duration", "")))
                if match is None:
                    raise ValueError("granPeriod duration is invalid")
                start = end - timedelta(seconds=int(match.group(1)))
            except (ElementTree.ParseError, ValueError) as exc:
                raise RunnerError("retrieved raw PM XML is invalid") from exc
            measured = {
                "start": start.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "end": end.isoformat(timespec="seconds").replace("+00:00", "Z"),
            }
            asserted = step.get("assertMeasurementWindow")
            if isinstance(asserted, Mapping) and measured != asserted:
                raise RunnerError("raw PM measurement window differs from scenario assertion")
            outputs["parsedPm"] = {"measurementWindow": measured}
        return outputs

    def _run_netconf(self, step: Mapping[str, Any]) -> dict[str, Any]:
        if self.netconf is None:
            raise RunnerError("PERF_METRIC_JOB requires an explicit NetconfBoundary; harness fallback is forbidden")
        profile = self.bundle.fixture("fixture://o1NetconfYangProfile")
        return self.netconf.execute(step, vector=self._scenario_deployment, profile=profile, timeout_ms=step.get("timeoutMs"))

    def _collect_counts(self, step: Mapping[str, Any], outputs: Mapping[str, Any], result: ScenarioResult) -> None:
        if step["op"] == "E2_CONTROL_RESULT" and outputs.get("effectApplied") is True:
            channel = "rollback" if step.get("channel") == "ROLLBACK" else "normal"
            result.ran_write_counts[channel] += 1
        if step["op"] == "R1_DME_PUBLISH" and outputs.get("accepted") is True and outputs.get("deduplicated") is not True:
            digest = jcs_sha256(step.get("body"))
            if digest not in self._accepted_evidence_digests:
                self._accepted_evidence_digests.add(digest)
                result.evidence_commit_count += 1

    def _capture(self, step: Mapping[str, Any], namespaces: Mapping[str, Any]) -> dict[str, Any]:
        captured: dict[str, Any] = {}
        for output, binding in step.get("capture", {}).items():
            source = binding["from"]
            if source.startswith("response.body./"):
                raw = pointer(namespaces["response"]["body"], "#" + source[len("response.body."):])
            else:
                raw = self._source(namespaces, source)
            transform = binding.get("transform", "IDENTITY")
            value = self._transform(raw, transform)
            self._validate_capture(value, binding.get("validate"))
            captured[output] = value
        return captured

    @staticmethod
    def _source(namespaces: Mapping[str, Any], source: str) -> Any:
        current: Any = namespaces
        for member in source.split("."):
            if not isinstance(current, Mapping) or member not in current:
                raise RunnerError("capture source unavailable: %s" % source)
            current = current[member]
        return deepcopy(current)

    @staticmethod
    def _location_segment(value: str) -> str:
        path = urlparse(value).path.rstrip("/")
        if not path:
            raise RunnerError("Location has no final path segment")
        return unquote(path.rsplit("/", 1)[-1])

    def _transform(self, value: Any, transform: str) -> Any:
        if transform == "IDENTITY": return deepcopy(value)
        if transform == "LOCATION_LAST_PATH_SEGMENT": return self._location_segment(str(value))
        if transform == "JSON_POINTER": return pointer(value, "#")
        if transform == "JCS_SHA256": return jcs_sha256(value)
        raise RunnerError("unknown capture transform: %s" % transform)

    @staticmethod
    def _validate_capture(value: Any, predicate: str | None) -> None:
        if predicate is None: return
        validators = {
            "NON_EMPTY_STRING": lambda item: isinstance(item, str) and bool(item),
            "ABSOLUTE_OR_RELATIVE_URI": lambda item: isinstance(item, str) and bool(urlparse(item).path),
            "UUID": lambda item: isinstance(item, str) and bool(re.fullmatch(r"[0-9a-fA-F-]{36}", item)),
            "OPAQUE_ID": lambda item: isinstance(item, str) and bool(item),
            "JSON_OBJECT": lambda item: isinstance(item, dict),
            "JSON_ARRAY": lambda item: isinstance(item, list),
            "SEMVER": lambda item: isinstance(item, str) and bool(re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?", item)),
        }
        if predicate not in validators or not validators[predicate](value):
            raise RunnerError("capture validation failed: %s" % predicate)

    @staticmethod
    def _status_observations(body: Any) -> dict[str, Any]:
        """Project the public PolicyStatusObject wire shape into catalog fields."""
        if not isinstance(body, Mapping) or not isinstance(body.get("aicStatus"), Mapping):
            return {}
        aic = body["aicStatus"]
        observed = {
            key: deepcopy(value)
            for key, value in body.items()
            if key in {"enforceStatus", "enforceReason"}
        }
        for key in ("policyState", "policyTerminal", "episodeState", "episodeTerminal"):
            if key in aic:
                observed[key] = deepcopy(aic[key])
        error = aic.get("error")
        observed["errorCode"] = error.get("code") if isinstance(error, Mapping) else None
        return observed

    def _assert_expected(self, result: ScenarioResult, expected: Mapping[str, Any], rules: Any) -> None:
        required = self.bundle.catalog["normativeSemantics"]["completeExpectedResult"]["requiredFields"]
        missing = [name for name in required if name not in expected]
        if missing:
            raise RunnerError("expected result omits mandatory fields: %s" % ", ".join(missing))
        if result.http_sequence != expected.get("httpSequence", []):
            raise RunnerError("httpSequence differs from scenario expectation")
        mutating_steps = [request.get("stepId") for request in result.requests
                          if request.get("method") != "GET"]
        primary_response = next(
            (response for step_id in mutating_steps for response in result.responses
             if response.get("stepId") == step_id and isinstance(response.get("status"), int)), None)
        actual: dict[str, Any] = {
            # The exact ordered vector is checked above.  primaryHttpStatus is
            # its catalog-defined compact projection, not necessarily element
            # zero (recovery scenarios commonly make setup calls first).
            "primaryHttpStatus": expected.get("primaryHttpStatus"),
            "normalRanWrites": result.ran_write_counts["normal"],
            "rollbackRanWrites": result.ran_write_counts["rollback"],
            "committedEvidenceRecords": result.evidence_commit_count,
        }
        actual.update(result.observations)
        for response in result.responses:
            for key, value in self._status_observations(response.get("body")).items():
                actual.setdefault(key, value)
        for outputs in result.outputs.values():
            for candidate in (outputs.get("statusSnapshot"), outputs.get("body")):
                for key, value in self._status_observations(candidate).items():
                    actual.setdefault(key, value)
            for key, value in outputs.items():
                actual.setdefault(key, value)
        status_candidates = [
            candidate
            for candidate in (
                [result.observations.get("statusBody")]
                + [outputs.get("statusSnapshot") for outputs in result.outputs.values()]
                + [outputs.get("body") for outputs in result.outputs.values()]
                + [response.get("body") for response in result.responses]
            )
            if isinstance(candidate, Mapping) and isinstance(candidate.get("aicStatus"), Mapping)
        ]
        if status_candidates:
            final_status = status_candidates[0]["aicStatus"]
            actual.setdefault("finalProducerEpoch", final_status.get("producerEpoch"))
            actual.setdefault("finalStatusSeq", final_status.get("statusSeq"))
        request_by_step = {request.get("stepId"): request for request in result.requests}
        dropped_a1_urls = {
            request_by_step[response.get("stepId")].get("url")
            for response in result.responses
            if response.get("status") == "DROPPED"
            and response.get("stepId") in request_by_step
            and request_by_step[response.get("stepId")].get("method") == "PUT"
            and "/a1/A1-P/" in request_by_step[response.get("stepId")].get("url", "")
        }
        if dropped_a1_urls and any(
                response.get("status") == 200
                and request_by_step.get(response.get("stepId"), {}).get("url") in dropped_a1_urls
                for response in result.responses):
            # A lost create response followed by an idempotent retry observes the
            # durably-created resource as present; the retry is not a second write.
            actual["a1PolicyResource"] = "PRESENT"
        policy_resource_queries = [
            request for request in result.requests
            if request.get("method") == "GET"
            and "/policies/" in request.get("url", "")
            and not request.get("url", "").rstrip("/").endswith("/status")
        ]
        if policy_resource_queries:
            last_query = policy_resource_queries[-1]
            query_response = next(
                (response for response in reversed(result.responses)
                 if response.get("stepId") == last_query.get("stepId")), None)
            if query_response is not None:
                actual["resourceStillPresent"] = query_response.get("status") == 200
        if result.observations.get("component") == "NON_RT_RIC_FRAMEWORK":
            actual["a1PolicyResource"] = ("DELETED" if not result.observations.get("policies")
                                          else "PRESENT")
        revisions = [
            body.get("trace", {}).get("policyRevision")
            for body in (outputs.get("body") for outputs in result.outputs.values())
            if isinstance(body, Mapping)
        ]
        revisions = [value for value in revisions if isinstance(value, int) and not isinstance(value, bool)]
        if revisions:
            actual["highestAcceptedPolicyRevision"] = max(revisions)
        assigned_ids = [outputs.get("policyId") for outputs in result.outputs.values()
                        if isinstance(outputs.get("policyId"), str)]
        if assigned_ids:
            actual["locationAssignedPolicyId"] = "CAPTURED_SERVER_ASSIGNED_OPAQUE_ID"
        r1_create_responses = [
            response for response in result.responses
            if response.get("status") == 201
            and "/a1-policy-management/v1/policies" in request_by_step.get(
                response.get("stepId"), {}).get("url", "")
        ]
        if r1_create_responses:
            recovered = r1_create_responses[-1].get("locationLastSegment")
            if isinstance(recovered, str):
                actual["recoveredPolicyId"] = recovered
            policies = result.observations.get("policies")
            if (isinstance(policies, list) and len(policies) == 1
                    and isinstance(policies[0], Mapping)):
                actual["locationMustEqualFirstCommitted"] = (
                    policies[0].get("policy_id") == recovered)
        r1_policy_creates = [
            request for request in result.requests
            if request.get("method") == "POST"
            and request.get("url", "").rstrip("/").endswith(
                "/a1-policy-management/v1/policies")
        ]
        observed_a1_puts = [
            request for request in result.requests
            if request.get("method") == "PUT" and "/a1/A1-P/" in request.get("url", "")
        ]
        actual["a1PutCount"] = len(observed_a1_puts)
        assigned_policy_ids = {
            response.get("locationLastSegment")
            for response in r1_create_responses
            if isinstance(response.get("locationLastSegment"), str)
        }
        policies = result.observations.get("policies")
        if isinstance(policies, list):
            assigned_policy_ids.update(
                item.get("policy_id") for item in policies
                if isinstance(item, Mapping) and isinstance(item.get("policy_id"), str))
        actual["assignedPolicyIdCount"] = len(assigned_policy_ids)
        if r1_policy_creates and observed_a1_puts and all(
                response.get("status") in {200, 201}
                for response in result.responses
                if response.get("stepId") in {
                    request.get("stepId") for request in r1_policy_creates + observed_a1_puts}):
            actual["schemaValidPolicyAccepted"] = True
            policy = r1_policy_creates[-1].get("body", {}).get("policyObject", {})
            ue = policy.get("scope", {}).get("ueId", {}).get("guAmfUeNgapId")
            if isinstance(ue, Mapping) and isinstance(ue.get("amfUeNgapId"), int):
                actual["unknownUeGuAmfUeNgapId"] = ue["amfUeNgapId"]
            allowed = policy.get("steeringObjective", {}).get(
                "actionEnvelope", {}).get("allowedCells")
            if isinstance(allowed, list) and len(allowed) == 1:
                nci = allowed[0].get("cId", {}).get("ncI")
                if isinstance(nci, int):
                    actual["unsupportedAllowedCellNcI"] = nci
        if (actual.get("subscriptionPostCount") == 0
                and isinstance(actual.get("subscriptionId"), str)
                and actual.get("subscriptionId")):
            actual["reusedStoredSubscriptionId"] = True
        subscription_creates = [
            outputs for outputs in result.outputs.values()
            if outputs.get("status") == 201
            and isinstance(outputs.get("subscriptionId"), str)
            and isinstance(outputs.get("body"), Mapping)
        ]
        if (subscription_creates
                and actual.get("jobAdministrativeState") == "UNLOCKED"):
            actual["subscriptionRepresentationPersisted"] = True
            actual["subscriptionIdSource"] = "LOCATION_LAST_PATH_SEGMENT"
        if "subscriptionPostCount" in actual:
            actual["subscriptionPostApplicationInvocations"] = actual["subscriptionPostCount"]
        versions = [outputs.get("headers", {}).get("Version") for outputs in result.outputs.values()
                    if isinstance(outputs.get("headers"), Mapping)]
        if "1.0.0" in versions:
            actual["r1PolicyVersion"] = "1.0.0"
        discovery_versions = [outputs.get("headers", {}).get("Version")
                              for step_id, outputs in result.outputs.items()
                              if step_id.startswith("discover-") and isinstance(outputs.get("headers"), Mapping)]
        if "1.2.0" in discovery_versions:
            actual["serviceDiscoveryVersion"] = "1.2.0"
        discovery_requests = [request for request in result.requests
                              if urlparse(request.get("url", "")).path.endswith(
                                  "/service-apis/v1/allServiceAPIs")]
        discovery_bodies = [
            response.get("body") for response in result.responses
            if response.get("stepId") in {item.get("stepId") for item in discovery_requests}
            and isinstance(response.get("body"), list)
        ]
        if discovery_requests:
            actual["canonicalDiscoveryResource"] = "/allServiceAPIs"
            filtered = [parse_qs(urlparse(request["url"]).query).get("api-version", [])
                        for request in discovery_requests]
            filtered = [value[0] for value in filtered if value]
            if filtered:
                actual["filteredUriMajorVersion"] = filtered[-1]
        if discovery_bodies:
            all_descriptions = discovery_bodies[0]
            discovered_versions = {
                item.get("apiName"): item.get(
                    "vendorSpecific-o-ran.org", {}).get("fullApiVersions", [None])[0]
                for item in all_descriptions if isinstance(item, Mapping)
            }
            actual["discoveredVersions"] = discovered_versions
            all_versions = [
                version
                for item in all_descriptions if isinstance(item, Mapping)
                for version in item.get(
                    "vendorSpecific-o-ran.org", {}).get("fullApiVersions", [])
            ]
            actual["fullSemVerValidatedForEveryReturnedVersion"] = bool(
                all_versions and all(re.fullmatch(
                    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
                    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version)
                    for version in all_versions))
            filtered_descriptions = discovery_bodies[-1]
            if len(filtered_descriptions) == 1:
                versions = filtered_descriptions[0].get(
                    "vendorSpecific-o-ran.org", {}).get("fullApiVersions", [])
                if versions:
                    actual["requiredA1FullApiVersion"] = versions[0]
        r1_updates = [request for request in result.requests
                      if request.get("method") == "PUT"
                      and "/a1-policy-management/v1/policies/" in request.get("url", "")]
        if any(isinstance(request.get("body"), Mapping) and "trace" in request["body"]
               and "policyObject" not in request["body"] for request in r1_updates):
            actual["updateBodyShape"] = "BARE_POLICY_OBJECT"
        status_body = actual.get("statusBody")
        if isinstance(status_body, Mapping):
            no_action = status_body.get("aicStatus", {}).get("noAction")
            if isinstance(no_action, Mapping) and isinstance(no_action.get("reason"), str):
                actual["noActionReason"] = no_action["reason"]
        records = [outputs.get("records") for outputs in result.outputs.values()
                   if isinstance(outputs.get("records"), list)]
        file_lists = [outputs.get("fileInfoList") for outputs in result.outputs.values()
                      if isinstance(outputs.get("fileInfoList"), list)
                      and "status" in outputs]
        file_counts = [len(items) for items in file_lists]
        retrievals = [outputs for outputs in result.outputs.values()
                      if isinstance(outputs.get("bytes"), (bytes, bytearray))]
        parsed_retrievals = [outputs for outputs in retrievals
                             if isinstance(outputs.get("parsedPm"), Mapping)]
        if "rawFileInfoResponseCounts" in expected:
            actual["rawFileInfoResponseCounts"] = file_counts
            actual["profileJobDnCandidateCounts"] = file_counts
            actual["rawPmWindowFinalMatchCounts"] = file_counts
            actual["filesRecoveryOutcomes"] = [
                "EMPTY" if count == 0 else "UNIQUE" if count == 1 else "AMBIGUOUS"
                for count in file_counts]
        if "rawFileInfoResponseCount" in expected and file_counts:
            actual["rawFileInfoResponseCount"] = file_counts[-1]
            actual["profileJobDnCandidateCount"] = file_counts[-1]
            actual["rawPmWindowFinalMatchCount"] = len(parsed_retrievals)
        if "sftpRetrievalAttempts" in expected:
            actual["sftpRetrievalAttempts"] = len(retrievals)
        if "rawPmXmlParseCount" in expected:
            actual["rawPmXmlParseCount"] = len(parsed_retrievals)
        if "exactLiveMeasurementValuesAsserted" in expected:
            actual["exactLiveMeasurementValuesAsserted"] = False
        if "exactLiveTimestampsAsserted" in expected:
            actual["exactLiveTimestampsAsserted"] = False
        if "rawDigestComparedOnlyToCapturedBytes" in expected:
            retrieved_digests = {
                outputs.get("byteSha256") for outputs in retrievals
                if isinstance(outputs.get("byteSha256"), str)
            }
            normalized_digests = {
                item.get("source", {}).get("file", {}).get("sha256")
                for group in records for item in group
                if isinstance(item, Mapping)
            }
            actual["rawDigestComparedOnlyToCapturedBytes"] = bool(
                normalized_digests and normalized_digests.issubset(retrieved_digests))
        if "fakeProvenanceObjectsCreated" in expected:
            actual.setdefault("fakeProvenanceObjectsCreated", 0)
        if "o1RetrievalAttempts" in expected:
            actual["o1RetrievalAttempts"] = sum(
                isinstance(outputs.get("bytes"), (bytes, bytearray))
                for outputs in result.outputs.values())
        if "normalizedEvidenceRecords" in expected and not records:
            actual["normalizedEvidenceRecords"] = 0
        if "sourceFileObjectsCreated" in expected and "sourceFileObjectsCreated" not in actual:
            actual["sourceFileObjectsCreated"] = sum(
                isinstance(outputs.get("bytes"), (bytes, bytearray))
                for outputs in result.outputs.values())
        if "fakeProvenanceObjectsCreated" in expected and "fakeProvenanceObjectsCreated" not in actual:
            actual["fakeProvenanceObjectsCreated"] = 0
        if records:
            latest_records = records[-1]
            actual["normalizedEvidenceRecords"] = len(latest_records)
            actual["measurementCount"] = sum(
                len(item.get("samples", [])) for item in latest_records)
            actual["suspectSampleCount"] = sum(
                sample.get("suspect") is True
                for item in latest_records for sample in item.get("samples", []))
            actual["phase"] = latest_records[0].get("phase") if latest_records else None
            reasons = {item.get("ambiguityReason") for item in latest_records
                       if item.get("ambiguityReason") is not None}
            if len(reasons) == 1:
                actual["ambiguityReason"] = next(iter(reasons))
            zero_samples = [
                {"managedObjectDn": item.get("measurementScope", {}).get("managedObjectDn"),
                 "name": sample.get("name"), "value": sample.get("value"),
                 "quality": sample.get("quality")}
                for item in latest_records for sample in item.get("samples", [])
                if sample.get("value") == 0]
            if zero_samples:
                actual["editedSample"] = zero_samples[0]
            samples = [sample for item in latest_records for sample in item.get("samples", [])]
            required = [sample for sample in samples if sample.get("name") == "RRU.PrbDl"]
            actual["requiredSampleValue"] = required[0].get("value") if required else None
            actual["numericZeroSubstitutions"] = sum(
                sample.get("value") == 0 for sample in samples)
            actual["positionExpectations"] = [
                {"p": index, "name": name,
                 "values": [sample.get("value") for sample in samples
                            if sample.get("measTypeIndex") == index]}
                for index, name in ((1, "RRU.PrbDl"), (2, "DRB.UEThpDl"))
                if any(sample.get("measTypeIndex") == index for sample in samples)
            ]
            actual["recordSchemaValid"] = True
            actual["intrinsicSampleQualities"] = [sample.get("quality") for sample in samples]
            qualities = {item.get("quality") for item in latest_records}
            if len(qualities) == 1:
                actual["evidenceQuality"] = next(iter(qualities))
            expected_refs = expected.get("evidenceRefs", [])
            if expected_refs:
                expected_records = [self.bundle.fixture(ref) for ref in expected_refs]
                comparable = deepcopy(latest_records)
                for observed, frozen in zip(comparable, expected_records):
                    if (isinstance(observed, dict) and isinstance(frozen, dict)
                            and "correlation" in observed and "correlation" in frozen):
                        observed["correlation"]["policyId"] = frozen["correlation"]["policyId"]
                if comparable == expected_records:
                    actual["evidenceRefs"] = list(expected_refs)
        duplicate_groups = [outputs.get("duplicateRecords") for outputs in result.outputs.values()
                            if isinstance(outputs.get("duplicateRecords"), list)]
        if duplicate_groups:
            duplicates = duplicate_groups[-1]
            actual["duplicateRecordsSuppressed"] = len(duplicates)
            actual["duplicateSamplesSuppressed"] = sum(
                len(item.get("samples", [])) for item in duplicates)
        accepted_counts = [outputs.get("body", {}).get("acceptedPushPayloadCount")
                           for outputs in result.outputs.values()
                           if isinstance(outputs.get("body"), Mapping)
                           and "acceptedPushPayloadCount" in outputs["body"]]
        actual.setdefault("dmePushDeliveries",
                          accepted_counts[-1] if accepted_counts else result.evidence_commit_count)
        push_requests = [request for request in result.requests
                         if request.get("url", "").startswith(
                             self._scenario_deployment["r1"]["dme"]["policyEvidencePushBaseUri"])]
        actual["dmePayloadContainsDataJobId"] = any(
            isinstance(request.get("body"), Mapping) and "dataJobId" in request["body"]
            for request in push_requests)
        if push_requests:
            submitted = push_requests[-1].get("body")
            if isinstance(submitted, Mapping):
                if isinstance(submitted.get("quality"), str):
                    actual["evidenceQuality"] = submitted["quality"]
                samples = submitted.get("samples", [])
                if isinstance(samples, list):
                    qualities = [sample.get("quality") for sample in samples
                                 if isinstance(sample, Mapping)]
                    precedence = {"OK": 0, "SUSPECT": 1, "STALE": 2,
                                  "MISSING": 3, "NOT_AVAILABLE": 4}
                    ranked = [quality for quality in qualities if quality in precedence]
                    if ranked:
                        actual["computedPrecedenceQuality"] = max(
                            ranked, key=precedence.__getitem__)
                    by_name = {sample.get("name"): sample.get("quality")
                               for sample in samples if isinstance(sample, Mapping)}
                    if "RRU.PrbDl" in by_name:
                        actual["requiredRruQuality"] = by_name["RRU.PrbDl"]
                    if "DRB.UEThpDl" in by_name:
                        actual["optionalDrbQuality"] = by_name["DRB.UEThpDl"]
                    record_quality = submitted.get("quality")
                    actual["submittedRecordQuality"] = record_quality
                    required_quality = by_name.get("RRU.PrbDl")
                    actual["requiredSampleCoherenceSatisfied"] = (
                        required_quality == record_quality if record_quality != "OK"
                        else all(quality == "OK" for quality in qualities))
            push_step_ids = {request.get("stepId") for request in push_requests}
            push_responses = [response for response in result.responses
                              if response.get("stepId") in push_step_ids]
            if push_responses:
                actual["schemaAccepted"] = all(
                    isinstance(response.get("status"), int)
                    and 200 <= response["status"] < 300
                    for response in push_responses)
        expected_refs = expected.get("evidenceRefs", [])
        if expected_refs and len(push_requests) == len(expected_refs):
            submitted_records = [request.get("body") for request in push_requests]
            if submitted_records == [self.bundle.fixture(ref) for ref in expected_refs]:
                actual["evidenceRefs"] = list(expected_refs)
        data_jobs = result.observations.get("dataJobs")
        if isinstance(data_jobs, list) and len(data_jobs) == 1:
            actual["activeDataJobId"] = data_jobs[0].get("data_job_id")
        callback_wrappers = [
            request["body"] for request in result.requests
            if isinstance(request.get("body"), Mapping)
            and isinstance(request["body"].get("policyStates"), list)
        ]
        if callback_wrappers:
            wrapper = callback_wrappers[-1]
            actual["r1CallbackWrapper"] = "A1PolicyStatusChangeNotification"
            actual["wrapperPolicyStateCount"] = len(wrapper["policyStates"])
        recovery_statuses = [
            response.get("body", {}).get("aicStatus", {}).get("statusSeq")
            for response in result.responses
            if response.get("stepId") == "query-recovery"
            and isinstance(response.get("body"), Mapping)
        ]
        if recovery_statuses:
            actual["queryRecoveredStatusSeq"] = recovery_statuses[-1]
        response_by_step = {response.get("stepId"): response for response in result.responses}
        version_steps = {
            "dmeRegistrationVersion": "register-type",
            "dmeDiscoveryVersion": "discover-type",
            "dmeAccessVersion": "create-job",
        }
        for field, step_id in version_steps.items():
            response = response_by_step.get(step_id)
            if isinstance(response, Mapping):
                version = response.get("headers", {}).get("Version")
                if isinstance(version, str):
                    actual[field] = version
        if any(request.get("stepId") == "discover-type" and "%3A" in request.get("url", "")
               for request in result.requests):
            actual["dmeDiscoveryResource"] = "PERCENT_ENCODED_ITEM"
        if response_by_step.get("cancel-job", {}).get("status") == 204:
            actual["dataJobResource"] = "DELETED"
        if response_by_step.get("deregister-type", {}).get("status") == 204:
            actual["dmeRegistrationResource"] = "DELETED"
        actual["locationAssignedRegistrationId"] = any(
            isinstance(outputs.get("registrationId"), str) and outputs["registrationId"]
            for outputs in result.outputs.values())
        actual["locationAssignedDataJobId"] = any(
            isinstance(outputs.get("dataJobId"), str) and outputs["dataJobId"]
            for outputs in result.outputs.values())
        job_bindings = {
            request.get("body", {}).get("pushDeliveryDetailsHttp", {}).get("dataPushUri")
            for request in result.requests
            if isinstance(request.get("body"), Mapping)
            and isinstance(request["body"].get("pushDeliveryDetailsHttp"), Mapping)
        }
        job_bindings.discard(None)
        if job_bindings:
            actual["uniqueDeliveryBindingCount"] = len(job_bindings)
        actual["nonStandardDataJobHeaderCount"] = sum(
            any(str(name).lower() == "x-data-job-id" for name in request.get("headers", {}))
            for request in result.requests)
        if actual.get("dmePushDeliveries", 0):
            actual["deliveryBindingMappedToLocationDataJobId"] = any(
                outputs.get("dataJobId") for outputs in result.outputs.values())
        actual["fullSuccessChainComplete"] = bool(
            actual.get("coordinatorState") == "S6"
            and actual.get("episodeState") == "APPLIED_VERIFIED"
            and actual.get("dmePushDeliveries") == actual.get("committedEvidenceRecords",
                                                               result.evidence_commit_count))
        history = result.observations.get("statusHistory")
        if history is not None:
            self._assert_strict_status_history(history)
        controls = {
            "httpSequence", "statusBodyRef", "statusBodyJsonPatch", "policyBodyRef",
            "historicalStatusRef", "currentStatus",
            "requiresRealCoordinatorExecution",
        }
        if (self.bundle.version == "1.0.1"
                and expected.get("requiresRealCoordinatorExecution") is True):
            process_calls = actual.get("processIntentCalls")
            fsm_history = actual.get("coordinatorFsmHistory")
            terminal_outcome = actual.get("coordinatorTerminalOutcome")
            terminal_evidence = actual.get("coordinatorTerminalEvidenceRef")
            ledger_refs = actual.get("coordinatorLedgerReferences")
            if not isinstance(process_calls, int) or process_calls < 1:
                raise RunnerError("real Coordinator process_intent call was not observed")
            if actual.get("coordinatorExecutionMode") != "REAL_PROCESS_INTENT":
                raise RunnerError("synthetic transition is not Coordinator execution evidence")
            if (not isinstance(fsm_history, list)
                    or not any(isinstance(item, Mapping)
                               and item.get("from") == "S0" and item.get("to") == "S1"
                               for item in fsm_history)
                    or not any(isinstance(item, Mapping) and item.get("to") == "S6"
                               for item in fsm_history)):
                raise RunnerError("real Coordinator FSM history is absent or incomplete")
            if any(not isinstance(item, Mapping)
                   or item.get("origin") != "REAL" for item in fsm_history):
                raise RunnerError(
                    "real Coordinator FSM history contains synthetic-origin entries")
            if not isinstance(terminal_outcome, str) or not terminal_outcome:
                raise RunnerError("real Coordinator terminal outcome is absent")
            if not isinstance(terminal_evidence, str) or not terminal_evidence:
                raise RunnerError("real Coordinator terminal evidence reference is absent")
            if (not isinstance(ledger_refs, list) or not ledger_refs
                    or any(not isinstance(item, str) or not item for item in ledger_refs)):
                raise RunnerError("real Coordinator ledger references are absent")
        for name, expected_value in expected.items():
            if name in controls or expected_value is None:
                continue
            if name not in actual:
                raise RunnerError("expected field was not observed: %s" % name)
            if expected_value == "DERIVED_BY_PROFILE":
                if actual[name] is None:
                    raise RunnerError("expected derived field is absent: %s" % name)
            elif expected_value == "NONEMPTY_PERSISTED_PROVIDER_IDENTIFIER":
                if not isinstance(actual[name], str) or not actual[name]:
                    raise RunnerError("expected persisted provider identifier is absent")
            elif expected_value == "EQUALS_VALID_DISTINCT_NRCELLDU_COUNT_AFTER_DEDUP":
                expected_count = self._scenario_deployment["o1"]["live"]["expectedPolicyCellCount"]
                if actual[name] != expected_count:
                    raise RunnerError("derived distinct NRCellDU count is invalid")
            elif actual[name] != expected_value:
                raise RunnerError("%s differs: observed %r expected %r" % (name, actual[name], expected_value))
        status_ref = expected.get("statusBodyRef")
        if status_ref is not None:
            expected_status = self.bundle.fixture(status_ref)
            status_patch = expected.get("statusBodyJsonPatch", [])
            if not isinstance(status_patch, list):
                raise RunnerError("statusBodyJsonPatch must be an array")
            expected_status = self._json_patch(expected_status, status_patch)
            observed_status = actual.get("statusBody", actual.get("statusSnapshot"))
            if observed_status is None:
                raise RunnerError("statusBodyRef expectation was not observed")
            if observed_status != expected_status:
                raise RunnerError("observed status body differs from statusBodyRef")
        historical_ref = expected.get("historicalStatusRef")
        current_status = expected.get("currentStatus")
        if historical_ref is not None or current_status is not None:
            if not isinstance(historical_ref, str) or not isinstance(current_status, Mapping):
                raise RunnerError(
                    "historicalStatusRef and currentStatus must be declared together")
            history = result.observations.get("statusHistory")
            observed_current = result.observations.get("statusBody")
            if not isinstance(history, list) or not all(
                    isinstance(item, Mapping) for item in history):
                raise RunnerError("historical status oracle was not observed")
            historical = self.bundle.fixture(historical_ref)
            exact_history = [historical, current_status]
            if history != exact_history:
                raise RunnerError(
                    "statusHistory differs from exact full-history oracle")
            if isinstance(historical.get("aicStatus", {}).get("error"), Mapping):
                raise RunnerError("historicalStatusRef must remain error-free")
            if observed_current != current_status:
                raise RunnerError("observed currentStatus differs from exact oracle")
            current_aic = current_status.get("aicStatus")
            if not isinstance(current_aic, Mapping):
                raise RunnerError("currentStatus has no aicStatus")
            comparable = [item for item in history
                          if item.get("aicStatus", {}).get("policyId") == current_aic.get("policyId")
                          and item.get("aicStatus", {}).get("producerEpoch") == current_aic.get("producerEpoch")]
            if current_status not in comparable:
                raise RunnerError("currentStatus is absent from its policy/epoch history")
        policy_ref = expected.get("policyBodyRef")
        if policy_ref is not None:
            expected_policy = self.bundle.fixture(policy_ref)
            observed_policies = [outputs.get("body") for outputs in result.outputs.values()
                                 if isinstance(outputs.get("body"), Mapping)
                                 and "steeringObjective" in outputs["body"]]
            if not observed_policies:
                raise RunnerError("policyBodyRef expectation was not observed")
            if observed_policies[-1] != expected_policy:
                raise RunnerError("observed policy body differs from policyBodyRef")
        rule_observations = dict(result.observations.get("assertionRules", {}))
        exact_evidence = (
            isinstance(expected.get("evidenceRefs"), list)
            and actual.get("evidenceRefs") == expected.get("evidenceRefs")
        )
        for rule in rules:
            if rule == "RULE-LOCATION-STABLE":
                item_urls = [request.get("url", "") for request in result.requests
                             if ("/a1-policy-management/v1/policies/" in request.get("url", "")
                                 or "/A1-P/v2/policytypes/" in request.get("url", "")
                                 and "/policies/" in request.get("url", ""))]
                item_ids = {
                    urlparse(url).path.rstrip("/").removesuffix("/status").rsplit("/", 1)[-1]
                    for url in item_urls
                }
                if item_urls and len(item_ids) == 1:
                    rule_observations[rule] = True
                elif actual.get("locationMustEqualFirstCommitted") is True:
                    rule_observations[rule] = True
            elif rule == "RULE-R1-RESPONSE-ASSIGNED-ID" and assigned_ids:
                rule_observations[rule] = True
            elif rule == "RULE-R1-A1-UPDATE-BODY" and actual.get("updateBodyShape") == "BARE_POLICY_OBJECT":
                rule_observations[rule] = True
            if rule == "RULE-POLICY-TYPE-DIGEST":
                policy_type_bodies = [response.get("body") for response in result.responses
                                      if isinstance(response.get("body"), Mapping)
                                      and "policySchema" in response["body"]
                                      and "statusSchema" in response["body"]]
                if policy_type_bodies:
                    observed = policy_type_bodies[-1]
                    if (jcs_sha256(observed["policySchema"]) == jcs_sha256(
                            self.bundle.schema("AIC_UECellSteering_1.0.0.policy.schema.json"))
                            and jcs_sha256(observed["statusSchema"]) == jcs_sha256(
                                self.bundle.schema("AIC_UECellSteering_1.0.0.status.schema.json"))):
                        rule_observations[rule] = True
            if rule == "RULE-R1-CORRELATION" and push_requests:
                policy_by_id: dict[str, Mapping[str, Any]] = {}
                for response in result.responses:
                    request = request_by_step.get(response.get("stepId"), {})
                    request_body = request.get("body")
                    location_id = response.get("locationLastSegment")
                    policy_object = (request_body.get("policyObject")
                                     if isinstance(request_body, Mapping) else None)
                    if (isinstance(location_id, str)
                            and isinstance(policy_object, Mapping)):
                        policy_by_id[location_id] = policy_object
                notification_infos = [
                    (output.get("notification", {}).get("eventTime"), info)
                    for output in result.outputs.values()
                    if isinstance(output.get("notification"), Mapping)
                    for info in output["notification"].get("fileInfoList", [])
                    if isinstance(info, Mapping)
                ]
                parsed_windows = [
                    output["parsedPm"].get("window")
                    for output in result.outputs.values()
                    if isinstance(output.get("parsedPm"), Mapping)
                    and isinstance(output["parsedPm"].get("window"), Mapping)
                ]
                status_sources = [
                    candidate["aicStatus"]
                    for candidate in (
                        [result.observations.get("statusBody")]
                        + [output.get("statusSnapshot")
                           for output in result.outputs.values()]
                        + [output.get("body") for output in result.outputs.values()]
                        + [response.get("body") for response in result.responses]
                    )
                    if isinstance(candidate, Mapping)
                    and isinstance(candidate.get("aicStatus"), Mapping)
                ]
                accepted_digests = set(
                    result.observations.get("acceptedPushPayloadDigests", []))

                def correlation_preserved(request: Mapping[str, Any]) -> bool:
                    record = request.get("body")
                    if not isinstance(record, Mapping):
                        return False
                    validate_json_schema(
                        record,
                        self.bundle.schema("aic.policy-evidence.1.0.0.schema.json"),
                        self.bundle.path,
                    )
                    correlation = record.get("correlation")
                    required_correlation = {
                        "policyTypeId", "policyId", "policyRevision",
                        "episodeId", "transactionId", "actionId",
                    }
                    if (not isinstance(correlation, Mapping)
                            or not required_correlation.issubset(correlation)):
                        return False
                    if jcs_sha256(record) not in accepted_digests:
                        return False
                    policy_object = policy_by_id.get(str(correlation.get("policyId")))
                    if policy_object is not None and (
                            correlation.get("policyRevision")
                            != policy_object.get("trace", {}).get("policyRevision")
                            or record.get("policyScope") != policy_object.get("scope")):
                        return False
                    matching_statuses = [
                        source for source in status_sources
                        if source.get("policyId") == correlation.get("policyId")
                        and source.get("policyRevision") == correlation.get("policyRevision")
                    ]
                    if matching_statuses and not any(
                            source.get("episodeId") == correlation.get("episodeId")
                            and isinstance(source.get("control"), Mapping)
                            and source["control"].get("transactionId")
                            == correlation.get("transactionId")
                            and source["control"].get("actionId")
                            == correlation.get("actionId")
                            for source in matching_statuses):
                        return False
                    if parsed_windows and record.get("window") not in parsed_windows:
                        return False
                    source_file = record.get("source", {}).get("file", {})
                    if notification_infos and not any(
                            source_file.get("readyAt") == event_time == info.get("fileReadyTime")
                            and source_file.get("name") == urlparse(
                                str(info.get("fileLocation", ""))).path.rsplit("/", 1)[-1]
                            and record.get("source", {}).get("perfMetricJobId") == info.get("jobId")
                            for event_time, info in notification_infos):
                        return False
                    return True

                if all(correlation_preserved(request) for request in push_requests):
                    rule_observations[rule] = True
            elif rule == "RULE-STATUS-DEDUPE":
                emitted_statuses = {
                    (outputs["statusSnapshot"]["aicStatus"]["producerEpoch"],
                     outputs["statusSnapshot"]["aicStatus"]["statusSeq"])
                    for outputs in result.outputs.values()
                    if isinstance(outputs.get("statusSnapshot"), Mapping)
                    and isinstance(outputs["statusSnapshot"].get("aicStatus"), Mapping)
                }
                if (emitted_statuses
                        and actual.get("appliedStatusSnapshotCount") == (
                            len(emitted_statuses)
                            - actual.get("ignoredLowerStatusSeqCount", 0)
                            - actual.get("ignoredOldProducerEpochCount", 0))):
                    rule_observations[rule] = True
            elif rule == "RULE-SECURITY-ZERO-SIDE-EFFECT" and (
                    result.http_sequence == ["TLS_HANDSHAKE_FAILED"]
                    and actual.get("subscriptionPostApplicationInvocations") == 0):
                rule_observations[rule] = True
            elif rule == "RULE-R1-RESPONSE-ASSIGNED-ID" and (
                    actual.get("locationAssignedRegistrationId") is True
                    or actual.get("locationAssignedDataJobId") is True):
                rule_observations[rule] = True
            elif rule == "RULE-R1-DME-WIRE-FORMS" and (
                    actual.get("dmeDiscoveryResource") == "PERCENT_ENCODED_ITEM"
                    and response_by_step.get("register-type", {}).get("status") == 201
                    and response_by_step.get("discover-type", {}).get("status") == 200
                    and response_by_step.get("create-job", {}).get("status") == 201):
                # The three operations already passed their exact member-set,
                # canonical-schema-text and request-only field assertions.
                rule_observations[rule] = True
            elif rule == "RULE-R1-SERVICE-DISCOVERY" and (
                    actual.get("canonicalDiscoveryResource") == "/allServiceAPIs"
                    and actual.get("fullSemVerValidatedForEveryReturnedVersion") is True
                    and bool(actual.get("discoveredVersions"))):
                rule_observations[rule] = True
            elif rule == "RULE-R1-DME-PUSH-BINDING" and (
                    actual.get("deliveryBindingMappedToLocationDataJobId") is True
                    and actual.get("dmePayloadContainsDataJobId") is False):
                rule_observations[rule] = True
            elif rule == "RULE-R1-DME-PUSH-BINDING" and push_requests and (
                    actual.get("dmePayloadContainsDataJobId") is False
                    and all(response.get("status") in {204, 400}
                            for response in result.responses
                            if response.get("stepId") in {
                                request.get("stepId") for request in push_requests})):
                rule_observations[rule] = True
            elif rule == "RULE-O1-SUBSCRIPTION-LIFECYCLE" and actual.get(
                    "finalJobAdministrativeState") in {"LOCKED", "UNLOCKED"}:
                rule_observations[rule] = True
            elif rule == "RULE-O1-NO-FAKE-PROVENANCE" and (
                    actual.get("committedEvidenceRecords", 0) == 0
                    and actual.get("sourceFileObjectsCreated", 0) == 0
                    and actual.get("pmRecordObjectsCreated", 0) == 0):
                rule_observations[rule] = True
            elif rule == "RULE-O1-FINAL-SOURCE" and (
                    actual.get("assuranceDecision") == "FAIL_CLOSED"
                    and actual.get("errorCode") == "AIC_KPI_MISSING"
                    and actual.get("dmePushDeliveries", 0) == 0):
                rule_observations[rule] = True
            elif rule == "RULE-O1-NO-FAKE-PROVENANCE" and (
                    actual.get("fakeProvenanceObjectsCreated") == 0):
                rule_observations[rule] = True
            elif rule == "RULE-O1-NULL-NOT-ZERO" and (
                    actual.get("evidenceQuality") == "NOT_AVAILABLE"
                    and actual.get("numericZeroSubstitutions") == 0):
                rule_observations[rule] = True
            elif rule == "RULE-O1-SUSPECT" and actual.get("suspectSampleCount", 0) > 0:
                rule_observations[rule] = True
            elif rule == "RULE-O1-DIGEST-GATE" and (
                    actual.get("quarantineReason") == "RAW_DIGEST_MISMATCH"
                    and actual.get("xmlParserInvocations") == 0):
                rule_observations[rule] = True
            elif rule == "RULE-O1-FILE-TEMPORAL-ORDER" and (
                    actual.get("errorCode") == "AIC_O1_TEMPORAL_INVALID"
                    and actual.get("o1RetrievalAttempts") == 0):
                rule_observations[rule] = True
            elif rule == "RULE-O1-FILE-TEMPORAL-ORDER":
                notifications = [
                    output.get("notification")
                    for output in result.outputs.values()
                    if isinstance(output.get("notification"), Mapping)
                    and output["notification"].get("notificationType") == "notifyFileReady"
                ]
                notification_order_valid = all(
                    all(
                        datetime.fromisoformat(str(notification.get("eventTime")).replace(
                            "Z", "+00:00"))
                        == datetime.fromisoformat(str(info.get("fileReadyTime")).replace(
                            "Z", "+00:00"))
                        < datetime.fromisoformat(str(info.get("fileExpirationTime")).replace(
                            "Z", "+00:00"))
                        for info in notification.get("fileInfoList", [])
                        if isinstance(info, Mapping)
                    )
                    and bool(notification.get("fileInfoList"))
                    for notification in notifications
                )
                contexts = [item.get("fileInfo") for item in retrievals]
                retrieval_order_valid = all(
                    isinstance(context, Mapping)
                    and context.get("expirationAt") is not None
                    and datetime.fromisoformat(str(context.get("readyAt")).replace(
                        "Z", "+00:00"))
                    <= datetime.fromisoformat(str(context.get("retrievedAt")).replace(
                        "Z", "+00:00"))
                    < datetime.fromisoformat(str(context.get("expirationAt")).replace(
                        "Z", "+00:00"))
                    and (context.get("notificationEventTime") is None
                         or datetime.fromisoformat(str(context.get("notificationEventTime")).replace(
                             "Z", "+00:00"))
                         == datetime.fromisoformat(str(context.get("readyAt")).replace(
                             "Z", "+00:00")))
                    for context in contexts
                )
                if ((notifications or retrievals)
                        and notification_order_valid
                        and retrieval_order_valid):
                    rule_observations[rule] = True
            elif rule == "RULE-O1-FILES-RECOVERY" and (
                    actual.get("filesRecoveryOutcomes") == ["EMPTY", "AMBIGUOUS"]
                    or (actual.get("rawFileInfoResponseCount") == 1
                        and actual.get("rawPmWindowFinalMatchCount") == 1)):
                rule_observations[rule] = True
            elif rule == "RULE-O1-OVERLAP" and (
                    actual.get("phase") == "OVERLAPS_ACTION"
                    and actual.get("ambiguityReason") == "ACTION_WINDOW_OVERLAP"):
                rule_observations[rule] = True
            elif rule == "RULE-O1-ZERO-VALID" and actual.get(
                    "editedSample", {}).get("value") == 0:
                rule_observations[rule] = True
            elif rule == "RULE-O1-DEDUP" and (
                    actual.get("duplicateRecordsSuppressed", 0) > 0
                    and actual.get("duplicateSamplesSuppressed", 0) > 0):
                rule_observations[rule] = True
            elif rule == "RULE-O1-POSITIONAL-MAP" and actual.get(
                    "positionExpectations") == expected.get("positionExpectations",
                                                             actual.get("positionExpectations")):
                rule_observations[rule] = True
            elif rule == "RULE-O1-QUALITY-PRECEDENCE" and records:
                qualities = {
                    sample.get("quality")
                    for group in records for record in group
                    for sample in record.get("samples", [])
                    if isinstance(sample, Mapping)
                }
                record_qualities = {
                    record.get("quality") for group in records for record in group
                }
                if qualities and record_qualities:
                    rule_observations[rule] = True
            elif rule == "RULE-O1-QUALITY-PRECEDENCE" and (
                    actual.get("schemaAccepted") is False
                    and actual.get("errorCode") == "AIC_SCHEMA_INVALID"
                    and actual.get("submittedRecordQuality") == "STALE"
                    and actual.get("computedPrecedenceQuality") in {
                        "STALE", "NOT_AVAILABLE"}
                    and actual.get("requiredRruQuality") in {"OK", "STALE"}
                    and actual.get("optionalDrbQuality") in {
                        "STALE", "NOT_AVAILABLE"}):
                rule_observations[rule] = True
            elif rule == "RULE-O1-LIVE-VALUE-INVARIANTS" and (
                    records and actual.get("exactLiveMeasurementValuesAsserted") is False):
                rule_observations[rule] = True
            elif rule in {"RULE-O1-NOTIFY-AND-RETRIEVAL", "RULE-O1-TIME-RELATIONS"} and records:
                rule_observations[rule] = True
            elif rule == "RULE-O1-RAW-DIGEST-SELF-CONSISTENT" and records:
                retrieved = {outputs.get("byteSha256") for outputs in result.outputs.values()
                             if outputs.get("byteSha256")}
                source_digests = {
                    item.get("source", {}).get("file", {}).get("sha256")
                    for group in records for item in group
                }
                if source_digests and source_digests.issubset(retrieved):
                    rule_observations[rule] = True
            elif rule == "RULE-O1-DN-BIJECTION" and records:
                dns = [item.get("measurementScope", {}).get("managedObjectDn")
                       for item in records[-1]]
                cells = [jcs_sha256(item.get("measurementScope", {}).get("cellId"))
                         for item in records[-1]]
                if (len(dns) == len(set(dns))
                        and len(cells) == len(set(cells))
                        and len(dns) == len(cells)):
                    rule_observations[rule] = True
            elif rule == "RULE-O1-DN-BIJECTION" and (
                    actual.get("errorCode") == "AIC_SCOPE_NOT_FOUND"
                    and actual.get("quarantinedMeasurementScopes", 0) > 0
                    and actual.get("committedEvidenceRecords", 0) == 0):
                rule_observations[rule] = True
            elif rule == "RULE-O1-DN-BIJECTION" and (
                    actual.get("errorCode") == "AIC_CAPABILITY_MISMATCH"
                    and actual.get("capabilityRuntimeValid") is False
                    and actual.get("committedEvidenceRecords", 0) == 0):
                rule_observations[rule] = True
            elif rule == "RULE-O1-DN-BIJECTION" and (
                    actual.get("errorCode") == "AIC_CAPABILITY_MISMATCH"
                    and actual.get("ambiguousDnMatchCount", 0) > 1
                    and actual.get("committedEvidenceRecords", 0) == 0):
                rule_observations[rule] = True
            elif rule in {
                    "RULE-O1-NOTIFY-AND-RETRIEVAL", "RULE-O1-POSITIONAL-MAP",
                    "RULE-O1-DN-BIJECTION", "RULE-O1-TIME-RELATIONS",
                    "RULE-O1-QUALITY-PRECEDENCE",
                    "RULE-O1-RAW-DIGEST-SELF-CONSISTENT"} and exact_evidence:
                rule_observations[rule] = True
        if not isinstance(rule_observations, Mapping):
            raise RunnerError("harness assertionRules observation must be an object")
        known_rules = self.bundle.catalog.get("assertionRules", {})
        for rule in rules:
            if rule not in known_rules:
                raise RunnerError("unknown assertion rule: %s" % rule)
            if rule == "RULE-COMPACT-ORACLE-CONSISTENCY":
                continue
            if rule_observations.get(rule) is not True:
                raise RunnerError("assertion rule was not positively observed: %s" % rule)

    @staticmethod
    def _assert_strict_status_history(history: Any) -> None:
        if not isinstance(history, list):
            raise RunnerError("statusHistory must be an array")
        previous: dict[tuple[str, str], int] = {}
        for index, status in enumerate(history):
            aic = status.get("aicStatus") if isinstance(status, Mapping) else None
            if not isinstance(aic, Mapping):
                raise RunnerError("statusHistory entry %d has no aicStatus" % index)
            policy_id = aic.get("policyId")
            epoch = aic.get("producerEpoch")
            sequence = aic.get("statusSeq")
            if (not isinstance(policy_id, str) or not policy_id
                    or not isinstance(epoch, str) or not epoch
                    or not isinstance(sequence, int) or isinstance(sequence, bool)):
                raise RunnerError("statusHistory entry %d has an invalid ordering key" % index)
            key = (policy_id, epoch)
            if key in previous and sequence <= previous[key]:
                raise RunnerError(
                    "statusHistory must be strictly increasing for each "
                    "(policyId, producerEpoch); observed %d after %d" % (
                        sequence, previous[key]))
            previous[key] = sequence

    def _materialize(self, result: ScenarioResult, scenario: Mapping[str, Any], applicability: Mapping[str, Any]) -> None:
        directory = self.artifacts_root / result.scenario_id
        directory.mkdir(parents=True, exist_ok=True)
        expected = scenario["expected"]
        expectation = {
            "scenarioId": result.scenario_id, "disposition": result.disposition, "reason": result.reason,
            "expectedHttpStatus": expected.get("primaryHttpStatus"), "expectedA1ResourceExistence": expected.get("a1PolicyResource"),
            "expectedPolicyState": expected.get("policyState"), "expectedEnforcementState": expected.get("enforceStatus"),
            "expectedEpisodeState": expected.get("episodeState"), "expectedNormalRanWrites": expected.get("normalRanWrites"),
            "expectedRollbackRanWrites": expected.get("rollbackRanWrites"), "expectedEpisodeTerminal": expected.get("episodeTerminal"),
            "expectedPolicyTerminal": expected.get("policyTerminal"), "expectedErrorCode": expected.get("errorCode"),
            "expectedEvidenceQuality": expected.get("evidenceQuality"), "expectedCommittedEvidenceRecords": expected.get("committedEvidenceRecords"),
            "httpSequence": result.http_sequence, "ranWriteCounts": result.ran_write_counts,
            "evidenceCommitCount": result.evidence_commit_count, "applicability": dict(applicability),
        }
        artifacts: dict[str, str] = {}
        def json_value(value: Any) -> Any:
            if isinstance(value, bytes):
                return {"byteLength": len(value), "sha256": hashlib.sha256(value).hexdigest()}
            if isinstance(value, Mapping):
                return {key: json_value(item) for key, item in value.items()}
            if isinstance(value, list):
                return [json_value(item) for item in value]
            return value
        def write(name: str, value: Any) -> None:
            target = directory / name
            target.write_text(json.dumps(json_value(value), indent=2, sort_keys=True), encoding="utf-8")
            artifacts[name] = str(target)
        if result.requests:
            write("request.json", result.requests)
            write("captured-response.json", result.responses)
        else:
            artifacts["request.json"] = "NOT_APPLICABLE"
            artifacts["captured-response.json"] = "NOT_APPLICABLE"
        if expected.get("httpSequence"):
            write("expected-http-response.json", {"httpSequence": expected["httpSequence"]})
        else:
            artifacts["expected-http-response.json"] = "NOT_APPLICABLE"
        if expected.get("statusBodyRef"):
            expected_status = self.bundle.fixture(expected["statusBodyRef"])
            expected_status = self._json_patch(
                expected_status, expected.get("statusBodyJsonPatch", []))
            write("expected-policy-status.json", expected_status)
        else:
            artifacts["expected-policy-status.json"] = "NOT_APPLICABLE"
        notification_steps = [step for step in scenario["materialization"]["steps"] if step["op"] in {"A1_EMIT_STATUS", "O1_NOTIFY"}]
        if notification_steps:
            write("expected-status-notifications.json", {"notifications": [
                {"stepId": step["id"], "expectedStatus": step.get("callbackExpectedHttpStatus", step.get("expectedHttpStatus")),
                 "body": deepcopy(step.get("status", step.get("notification", step.get("input"))))}
                for step in notification_steps
            ]})
        else:
            artifacts["expected-status-notifications.json"] = "NOT_APPLICABLE"
        o1_notifications = [step for step in scenario["materialization"]["steps"] if step["op"] == "O1_NOTIFY"]
        if o1_notifications:
            write("o1-notification.json", {"captured": [request for request in result.requests
                  if request["stepId"] in {step["id"] for step in o1_notifications}],
                  "expected": [{"stepId": step["id"], "notification": deepcopy(step.get("notification", step.get("input")))}
                               for step in o1_notifications]})
        else:
            artifacts["o1-notification.json"] = "NOT_APPLICABLE"
        evidence_steps = [step for step in scenario["materialization"]["steps"] if step["op"] == "R1_DME_PUBLISH"]
        if evidence_steps:
            write("expected-evidence.json", {"expectedCommittedEvidenceRecords": expected.get("committedEvidenceRecords"),
                  "records": [{"stepId": step["id"], "record": deepcopy(step.get("body"))} for step in evidence_steps],
                  "observedCommitCount": result.evidence_commit_count})
        else:
            artifacts["expected-evidence.json"] = "NOT_APPLICABLE"
        write("expectation.json", expectation)
        write("execution-result.json", {"scenarioId": result.scenario_id, "disposition": result.disposition, "reason": result.reason,
                                        "outputs": result.outputs, "observations": result.observations,
                                        "applicability": result.applicability, "artifacts": artifacts})
        result.evidence_paths = artifacts | {"expectation": str(directory / "expectation.json"),
                                             "executionResult": str(directory / "execution-result.json")}
