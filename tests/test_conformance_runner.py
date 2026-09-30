"""Hermetic, schema-valid tests for the Section 18 black-box runner."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from oran.conformance.contracts import ContractBundle, ContractError, validate_json_schema
from oran.conformance.expression import ExpressionError, ForwardOutputError, UndefinedOutputError, resolve
from oran.conformance.harness_api import LocalFakeHarness
from oran.conformance.report import suite_manifest, traceability_report
from oran.conformance.runner import (
    HttpResponse,
    PreflightError,
    ScenarioResult,
    ScenarioRunner,
)
from oran.conformance.vectors import local_development_vector


CONTRACT_BUNDLE = Path(__file__).resolve().parents[1] / "contracts" / "oran-aic" / "1.0.0" / "shared-contract-bundle"
CORRECTED_AUTHORITY = Path(__file__).resolve().parents[1] / "contracts" / "oran-aic" / "1.0.1"
CORRECTED_BUNDLE = CORRECTED_AUTHORITY / "shared-contract-bundle"
ZERO = "0" * 64
NOW = "2026-08-04T00:00:00Z"


def _artifact(name: str) -> dict:
    return {"path": name, "sha256": ZERO}


def _ran_function(identifier: int) -> dict:
    common = {
        "ranFunctionId": identifier, "ranFunctionRevision": 2 if identifier == 2 else 1,
        "ranFunctionOid": "1.3.6.1.4.1.53148.1.2.2.2" if identifier == 2 else "1.3.6.1.4.1.53148.1.1.2.3",
        "shortName": "ORAN-E2SM-KPM" if identifier == 2 else "ORAN-E2SM-RC",
        "serviceModelVersion": "2.03" if identifier == 2 else "1.03",
        "rawDefinition": _artifact("raw"), "decodedDefinition": _artifact("decoded"),
        "canonicalDefinition": {"algorithm": "RFC8785_JSON", "toolVersion": "test", "sha256": ZERO},
        "moduleSetSha256": ZERO, "observedAt": NOW, "active": True,
    }
    if identifier == 2:
        common["capability"] = {"reportStyles": [
            {"styleType": 1, "actionDefinitionFormat": 1, "indicationHeaderFormat": 1,
             "indicationMessageFormat": 1, "measurements": [{"name": "RRU.PrbDl", "measurementType": "NAME", "labels": []}]},
            {"styleType": 4, "actionDefinitionFormat": 4, "indicationHeaderFormat": 1,
             "indicationMessageFormat": 3, "measurements": [{"name": "RRU.PrbDl", "measurementType": "NAME", "labels": []}]},
        ]}
    else:
        common["capability"] = {"styleType": 3, "actionId": 1, "headerFormat": 1, "messageFormat": 1,
                                "outcomeFormat": 1, "nrCgiEncodingProfileSha256": ZERO,
                                "ranParameterTree": [{"id": 1, "name": "target", "valueType": "ELEMENT",
                                                      "mandatory": True, "minOccurs": 1, "maxOccurs": 1, "children": []}]}
    return common


def valid_vector() -> dict:
    def connection(index: int) -> dict:
        return {
            "globalE2NodeId": {"nodeType": "GNB", "plmn": {"mcc": "208", "mnc": "95", "mncDigitLength": 2},
                               "nodeId": {"hex": "0x%02x" % index, "bitLength": 8}},
            "connectionEpoch": 1, "associationId": "assoc-%s" % index, "acceptedSetupAt": NOW,
            "e2apVersion": "2.03", "transferSyntax": "APER", "rawE2SetupPdu": _artifact("setup"),
            "decoderModuleSetSha256": ZERO, "active": True, "ranFunctions": [_ran_function(2), _ran_function(3)],
        }
    def file_info(suffix: str) -> dict:
        return {"fileLocation": "sftp://localhost/%s.xml" % suffix, "fileSize": 10,
                "fileReadyTime": "2026-08-04T00:02:01Z", "fileExpirationTime": "2026-08-04T01:02:01Z",
                "fileCompression": "NONE", "fileFormat": "32.435 V10.0 XML-schema", "fileDataType": "PERFORMANCE", "jobId": "job"}
    cell1 = {"cellId": {"plmnId": {"mcc": "208", "mnc": "95"}, "cId": {"ncI": 12345678}}, "managedObjectDn": "NRCellDU=1"}
    cell2 = {"cellId": {"plmnId": {"mcc": "208", "mnc": "95"}, "cId": {"ncI": 87654321}}, "managedObjectDn": "NRCellDU=2"}
    schema = {}
    return {
        "vectorVersion": "oran-aic-deployment-test-vector/1.0.0",
        "r1": {"apiRoot": "https://localhost/r1", "rAppId": "rapp", "publishesGeneralRequestResponseApi": False,
               "statusSubscriptionId": "status-sub", "callbackApi": {"apiName": "callback", "aefId": "aef",
               "rootUri": "https://localhost/callback", "ipv4Addr": "127.0.0.1", "port": 9443,
               "resourceUri": "/callback"}, "dme": {"policyEvidencePushBaseUri": "https://localhost/push",
               "activeDataJobId": "job", "activeDeliveryBindingId": "binding-0123456789012345",
               "dataAccessEndpoint": {"ipv4Addr": "127.0.0.1", "port": 9443, "securityMethods": ["OAUTH"]}}},
        "a1": {"apiRoot": "https://localhost/a1", "statusCallbackRoot": "https://localhost/a1-status"},
        "o1": {"netconf": {"endpoint": "ssh://localhost:830", "knownHostsRef": "secret://known", "credentialRef": "secret://netconf"},
               "fileDataReporting": {"mnsRoot": "https://localhost/o1", "mnsVersion": "v1", "mnsAgentDn": "agent",
                                     "consumerReference": "https://localhost/consumer", "subscriptionId": "sub"},
               "sftp": {"allowedAuthorities": ["localhost"], "knownHostsRef": "secret://known", "credentialRef": "secret://sftp"},
               "perfMetricJob": {"jobId": "job", "managedObjectDn": "job=1", "managedObjectUri": "https://localhost/job/1"},
               "live": {"expectedPolicyCellCount": 2, "expectedMeasurementWindow": {"start": NOW, "end": "2026-08-04T00:01:00Z"},
                        "recoveryFiles": {"uniqueCandidate": file_info("one"), "ambiguousCandidates": [file_info("two"), file_info("three")]}}},
        "e2Inventory": {"schemaVersion": "oran-aic-e2-capability-inventory/1.0.0", "contractProfile": "oran-aic/1.0.0",
                        "releaseManifestSha256": ZERO, "generatedAt": NOW, "status": "READY", "connections": [connection(1), connection(2)]},
        "topology": {"nearRtRicId": "ric", "ueId": {"guAmfUeNgapId": 1}, "servingCell": cell1, "targetCell": cell2,
                     "cellMappings": [cell1, cell2]},
        "policy": {"validity": {"notBefore": NOW, "expiresAt": "2026-08-05T00:00:00Z"},
                   "trace": {"intentId": "768f56d8-2d45-4c05-b00b-7b76e8e2ef61", "idempotencyKey": "key",
                             "correlationId": "f993a697-778f-4551-b88c-5a5c07a09b1d", "producerId": "producer"}},
        "security": {"truststoreRef": "secret://trust", "r1ClientCredentialRef": "secret://r1", "o1NotificationCredentialRef": "secret://o1"},
        "timeouts": {"defaultStepMs": 1000, "liveCaptureMs": 1000, "notificationWindowMs": 120000},
        "schemas": {"policyEvidenceRecordSchema": schema, "policyEvidenceRecordSchemaCanonicalJson": "{}",
                    "policyEvidenceRecordSchemaJcsSha256": hashlib.sha256(b"{}").hexdigest(),
                    "policyEvidenceFilterSchemaJcsSha256": ZERO},
    }


class FakeHttp:
    def __init__(self, responses, expected_bodies=None):
        self.responses = list(responses)
        self.expected_bodies = list(expected_bodies or [])
        self.calls = []

    def request(self, method, url, headers, body, timeout_ms):
        self.calls.append({"method": method, "url": url, "headers": deepcopy(headers),
                           "body": deepcopy(body), "timeoutMs": timeout_ms})
        if self.expected_bodies:
            expected = self.expected_bodies.pop(0)
            if body != expected:
                raise AssertionError("request body differs from fake-boundary expectation")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response if isinstance(response, HttpResponse) else HttpResponse(response, {}, {})


class FakeNetconf:
    def __init__(self):
        self.steps = []

    def execute(self, step, **_kwargs):
        self.steps.append(deepcopy(step))
        return {"administrativeState": "UNLOCKED"}


class FakeSftp:
    def __init__(self, payload=b"payload"):
        self.payload = payload
        self.sources = []
        self.last_retrieved_at = "2026-08-04T00:02:03.000Z"

    def retrieve(self, source, **_kwargs):
        self.sources.append(source)
        return self.payload


class ExpressionTests(unittest.TestCase):
    def test_whole_value_deep_copy_and_embedded_scalar(self):
        values = {"deployment": {"object": {"x": [1]}, "port": 8080}, "constants": {}, "steps": {}}
        resolved = resolve({"a": "${deployment.object}", "b": "http://x:${deployment.port}"}, values)
        self.assertEqual(resolved, {"a": {"x": [1]}, "b": "http://x:8080"})

    def test_expression_errors_are_fail_closed(self):
        values = {"deployment": {}, "constants": {}, "steps": {}}
        for error, expression in ((ExpressionError, "${deployment.missing}"),
                                  (UndefinedOutputError, "${steps.done.outputs.missing}"),
                                  (ForwardOutputError, "${steps.later.outputs.value}")):
            with self.assertRaises(error):
                resolve(expression, values, {"done"} if "done" in expression else set())

    def test_step_output_expression_accepts_runtime_result_shape(self):
        values = {"deployment": {}, "constants": {}, "steps": {"create": {"policyId": "assigned-1"}}}
        self.assertEqual(resolve("${steps.create.outputs.policyId}", values, {"create"}), "assigned-1")


class CorrectedAuthorityTests(unittest.TestCase):
    def test_discovery_defaults_to_1_0_1_and_env_can_select_historical_tree(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORAN_CONTRACT_AUTHORITY", None)
            corrected = ContractBundle.discover()
        self.assertEqual("1.0.1", corrected.version)
        self.assertEqual("oran-aic-scenario-runner/1.0.1",
                         corrected.runner["contractVersion"])

        historical = CORRECTED_AUTHORITY.parent / "1.0.0"
        with patch.dict(os.environ,
                        {"ORAN_CONTRACT_AUTHORITY": str(historical)}):
            baseline = ContractBundle.discover()
        self.assertEqual("1.0.0", baseline.version)

    def test_missing_corrected_runner_fails_without_baseline_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            authority = Path(directory) / "1.0.1"
            shutil.copytree(CORRECTED_AUTHORITY, authority)
            (authority / "shared-contract-bundle" /
             "scenario-runner-contract.1.0.1.json").unlink()
            with self.assertRaisesRegex(
                    ContractError, r"scenario-runner-contract\.1\.0\.1\.json") as caught:
                ContractBundle.discover(authority)
            self.assertNotIn("scenario-runner-contract.1.0.0.json",
                             str(caught.exception))

    def test_optional_execution_profile_assignment_is_validated_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            authority = Path(directory) / "1.0.1"
            shutil.copytree(CORRECTED_AUTHORITY, authority)
            bundle = authority / "shared-contract-bundle"
            schema = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "urn:test:execution-profile-assignment:1.0.1",
                "type": "object",
                "required": ["assignmentVersion"],
                "additionalProperties": False,
                "properties": {"assignmentVersion": {"const": "test/1.0.1"}},
            }
            (bundle / "execution-profile-assignment.1.0.1.schema.json").write_text(
                json.dumps(schema), encoding="utf-8")
            (bundle / "execution-profile-assignment.1.0.1.json").write_text(
                json.dumps({"assignmentVersion": "wrong"}), encoding="utf-8")
            with self.assertRaisesRegex(ContractError,
                                        "execution profile assignment"):
                ContractBundle.discover(authority)


class CorrectedRunnerSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = ContractBundle(CORRECTED_BUNDLE)
        cls.vector = valid_vector()

    def _scenario(self, identifier):
        return deepcopy(next(item for item in self.bundle.catalog["scenarios"]
                             if item["id"] == identifier))

    def test_corrected_bundle_completes_all_new_preflight_checks(self):
        runner = ScenarioRunner(self.bundle, self.vector)
        runner.preflight()
        self.assertTrue(runner._preflight_complete)

    def test_corrected_preflight_gate_is_active_for_the_1_0_1_bundle(self):
        """R4: the real-Coordinator gate is exercised on corrected authority."""
        self.assertEqual("1.0.1", self.bundle.version)
        bundle = deepcopy(self.bundle)
        scenario = next(item for item in bundle.catalog["scenarios"]
                        if item["id"] == "SC-084")
        scenario["materialization"]["steps"] = [
            step for step in scenario["materialization"]["steps"]
            if step["op"] != "COORDINATOR_PROCESS_INTENT"
        ]
        runner = ScenarioRunner(bundle, self.vector)
        with self.assertRaisesRegex(ContractError,
                                    "real Coordinator execution gate"):
            runner._check_verify_suite_operation_initial_state_and_fault_names()

    def test_a1_seed_materializes_inline_status_without_status_ref(self):
        harness = LocalFakeHarness()
        runner = ScenarioRunner(self.bundle, self.vector, harness=harness)
        scenario = self._scenario("SC-033")
        runner._install_initial_state(scenario)
        seed = next(call for call in harness.calls
                    if call[0:2] == ("op", "A1_SEED_RESOURCE"))
        self.assertIsInstance(seed[2]["status"], dict)
        self.assertNotIn("statusRef", seed[2])

    def test_corrected_a1_seed_rejects_removed_status_ref_alias(self):
        bundle = deepcopy(self.bundle)
        scenario = next(item for item in bundle.catalog["scenarios"]
                        if item["id"] == "SC-033")
        seed = next(item for item in scenario["materialization"]["initialState"]
                    if isinstance(item, dict)
                    and item.get("op") == "A1_SEED_RESOURCE")
        seed["statusRef"] = seed.pop("status")
        runner = ScenarioRunner(bundle, self.vector)
        with self.assertRaisesRegex(ContractError,
                                    "A1_SEED_RESOURCE requires status"):
            runner._check_verify_suite_operation_initial_state_and_fault_names()

    def test_missing_kpm_fresh_is_rejected_before_target_calls(self):
        bundle = deepcopy(self.bundle)
        scenario = next(item for item in bundle.catalog["scenarios"]
                        if item["id"] == "SC-095")
        snapshot = next(step for step in scenario["materialization"]["steps"]
                        if step["op"] == "KPM_SNAPSHOT")
        del snapshot["fresh"]
        runner = ScenarioRunner(bundle, self.vector)
        with self.assertRaisesRegex(ContractError, "KPM_SNAPSHOT.*fresh"):
            runner._check_verify_suite_operation_initial_state_and_fault_names()
        self.assertEqual([], runner.harness.calls)

    def test_flip_retrieved_byte_interposes_once_before_digest_and_parse(self):
        harness = LocalFakeHarness()
        runner = ScenarioRunner(self.bundle, self.vector, harness=harness)
        step = next(step for step in self._scenario("SC-057")["materialization"]["steps"]
                    if step["op"] == "O1_RETRIEVE")
        scenario = {
            "id": "SC-BYTE-FLIP-BOUNDARY",
            "materialization": {
                "time": {"mode": "FIXED_LOGICAL", "origin": NOW},
                "initialState": [],
                "steps": [step],
                "faults": [{"type": "FLIP_RETRIEVED_BYTE",
                            "afterStep": step["id"], "byteOffset": 100,
                            "preserveByteLength": True}],
            },
            "expected": {
                "primaryHttpStatus": None, "httpSequence": [],
                "a1PolicyResource": None, "enforceStatus": None,
                "policyState": None, "policyTerminal": None,
                "episodeState": None, "episodeTerminal": None,
                "normalRanWrites": 0, "rollbackRanWrites": 0,
                "errorCode": None, "evidenceQuality": "MISSING",
                "committedEvidenceRecords": 0,
                "quarantineReason": "RAW_DIGEST_MISMATCH",
                "xmlParserInvocations": 0,
            },
            "rules": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            runner.artifacts_root = Path(directory)
            result = runner.run(scenario)
        self.assertEqual("PASS", result.disposition, result.reason)
        self.assertFalse(any(call[0:2] == ("fault", "FLIP_RETRIEVED_BYTE")
                             for call in harness.calls))

    def test_delivery_binding_uses_only_canonical_step_bindings(self):
        runner = ScenarioRunner(self.bundle, self.vector)
        canonical = self.vector["r1"]["dme"]["activeDeliveryBindingId"]
        endpoint = runner._endpoint({
            "endpointRef": "#/endpointTemplates/r1DmePushDestination",
            "bindings": {"deliveryBindingId": canonical},
            "deliveryBindingId": "forbidden-top-level-alias",
        })
        self.assertTrue(endpoint.endswith("/" + canonical))
        with self.assertRaisesRegex(ContractError,
                                    "endpoint template binding is absent"):
            runner._endpoint({
                "endpointRef": "#/endpointTemplates/r1DmePushDestination",
                "deliveryBindingId": canonical,
            })

    def test_sc033_consumes_distinct_historical_and_current_status_oracles(self):
        runner = ScenarioRunner(self.bundle, self.vector)
        scenario = self._scenario("SC-033")
        historical = self.bundle.fixture(
            scenario["expected"]["historicalStatusRef"])
        current = deepcopy(scenario["expected"]["currentStatus"])
        result = ScenarioResult("SC-033", "PASS")
        result.http_sequence = [204]
        result.observations = {
            "a1PolicyResource": "PRESENT",
            "a1StatusCallbackAttempts": 1,
            **ScenarioRunner._status_observations(current),
            "statusBody": current,
            "statusHistory": [historical, current],
        }
        runner._assert_expected(result, scenario["expected"], [])

        inherited = deepcopy(current)
        inherited["aicStatus"]["episodeState"] = "APPLIED_VERIFIED"
        result.observations["statusBody"] = inherited
        result.observations["statusHistory"][-1] = inherited
        with self.assertRaisesRegex(ContractError, "full-history oracle"):
            runner._assert_expected(result, scenario["expected"], [])

    def test_status_body_fixture_patch_remains_an_exact_oracle(self):
        runner = ScenarioRunner(self.bundle, self.vector)
        scenario = self._scenario("SC-017")
        expected = deepcopy(scenario["expected"])
        expected["statusBodyJsonPatch"] = [
            {"op": "replace", "path": "/aicStatus/statusSeq", "value": 4},
        ]
        observed = self.bundle.fixture(expected["statusBodyRef"])
        observed["aicStatus"]["statusSeq"] = 4
        result = ScenarioResult("SC-017", "PASS")
        result.observations = {
            **ScenarioRunner._status_observations(observed),
            "a1PolicyResource": "PRESENT",
            "noActionReason": "ALREADY_ON_TARGET",
            "statusBody": observed,
            "statusHistory": [observed],
        }

        runner._assert_expected(result, expected, [])
        result.observations["statusBody"]["aicStatus"]["statusSeq"] = 3
        with self.assertRaisesRegex(ContractError, "statusBodyRef"):
            runner._assert_expected(result, expected, [])

    def test_sc033_full_history_oracle_rejects_regression_injection(self):
        runner = ScenarioRunner(self.bundle, self.vector)
        scenario = self._scenario("SC-033")
        historical = self.bundle.fixture(
            scenario["expected"]["historicalStatusRef"])
        current = deepcopy(scenario["expected"]["currentStatus"])
        result = ScenarioResult("SC-033", "PASS")
        result.http_sequence = [204]
        result.observations = {
            "a1PolicyResource": "PRESENT",
            "a1StatusCallbackAttempts": 1,
            **ScenarioRunner._status_observations(current),
            "statusBody": current,
        }

        regressed = deepcopy(current)
        regressed["aicStatus"]["statusSeq"] = 2
        result.observations["statusHistory"] = [historical, regressed, current]
        with self.assertRaisesRegex(
                ContractError, "strictly increasing"):
            runner._assert_expected(result, scenario["expected"], [])

        duplicate = deepcopy(current)
        result.observations["statusHistory"] = [
            historical, duplicate, current]
        with self.assertRaisesRegex(
                ContractError, "strictly increasing"):
            runner._assert_expected(result, scenario["expected"], [])

    def test_sc033_full_history_oracle_rejects_spurious_intermediate_status(self):
        runner = ScenarioRunner(self.bundle, self.vector)
        scenario = self._scenario("SC-033")
        historical = self.bundle.fixture(
            scenario["expected"]["historicalStatusRef"])
        current = deepcopy(scenario["expected"]["currentStatus"])
        spurious = deepcopy(current)
        spurious["aicStatus"]["policyId"] = "spurious-policy"
        result = ScenarioResult("SC-033", "PASS")
        result.http_sequence = [204]
        result.observations = {
            "a1PolicyResource": "PRESENT",
            "a1StatusCallbackAttempts": 1,
            **ScenarioRunner._status_observations(current),
            "statusBody": current,
            "statusHistory": [historical, spurious, current],
        }

        with self.assertRaisesRegex(
                ContractError, "exact full-history oracle"):
            runner._assert_expected(result, scenario["expected"], [])

    def test_every_scenario_rejects_duplicate_or_regressed_status_history(self):
        required = self.bundle.catalog["normativeSemantics"][
            "completeExpectedResult"]["requiredFields"]
        expected = {name: None for name in required}
        expected.update({
            "httpSequence": [], "normalRanWrites": 0,
            "rollbackRanWrites": 0, "committedEvidenceRecords": 0,
        })
        base = {
            "enforceStatus": "ENFORCED",
            "aicStatus": {
                "policyId": "policy-1", "producerEpoch": "epoch-1",
                "statusSeq": 1, "policyState": "ACTIVE",
                "policyTerminal": False,
            },
        }
        for label, sequences in (("duplicate", [1, 2, 2]),
                                 ("regression", [1, 3, 2])):
            with self.subTest(label=label):
                history = []
                for sequence in sequences:
                    status = deepcopy(base)
                    status["aicStatus"]["statusSeq"] = sequence
                    history.append(status)
                harness = LocalFakeHarness(observable_state={
                    "statusHistory": history,
                })
                runner = ScenarioRunner(self.bundle, self.vector, harness=harness)
                runner._preflight_complete = True
                scenario = {
                    "id": "SC-STATUS-%s" % label.upper(),
                    "materialization": {
                        "time": {"mode": "FIXED_LOGICAL", "origin": NOW},
                        "initialState": [], "steps": [], "faults": [],
                    },
                    "expected": expected,
                    "rules": [],
                }

                with tempfile.TemporaryDirectory() as directory:
                    runner.artifacts_root = Path(directory)
                    result = runner.run(scenario)

                self.assertEqual("FAIL", result.disposition)
                self.assertIn("strictly increasing", result.reason)

    def test_sc083_requires_real_process_intent_not_synthetic_transitions(self):
        runner = ScenarioRunner(self.bundle, self.vector)
        scenario = self._scenario("SC-083")
        result = ScenarioResult("SC-083", "PASS")
        result.http_sequence = deepcopy(scenario["expected"]["httpSequence"])
        result.observations = {
            "processIntentCalls": 1,
            "coordinatorExecutionMode": "SYNTHETIC_CONTRACT_TRANSITION",
            "coordinatorFsmHistory": [
                {"from": "S0", "to": "S1"}, {"from": "S4", "to": "S6"}],
            "coordinatorTerminalOutcome": "commit_original",
            "coordinatorTerminalEvidenceRef": "evid-synthetic",
            "coordinatorLedgerReferences": ["ledger-synthetic"],
        }
        with self.assertRaisesRegex(
                ContractError, "synthetic transition"):
            runner._assert_expected(result, scenario["expected"], [])

    def test_coordinator_terminal_assertions_fail_closed_without_real_mode(self):
        runner = ScenarioRunner(self.bundle, self.vector)
        scenario = self._scenario("SC-084")
        result = ScenarioResult("SC-084", "PASS")
        result.http_sequence = deepcopy(scenario["expected"]["httpSequence"])
        result.evidence_commit_count = 2
        result.observations = {
            **{key: deepcopy(value) for key, value in scenario["expected"].items()
               if key not in {"httpSequence", "primaryHttpStatus"}},
            "processIntentCalls": 0,
            "coordinatorExecutionMode": "SYNTHETIC_CONTRACT_TRANSITION",
            "coordinatorFsmHistory": [],
            "coordinatorTerminalOutcome": None,
            "coordinatorTerminalEvidenceRef": None,
            "coordinatorLedgerReferences": [],
        }

        with self.assertRaisesRegex(ContractError, "real Coordinator"):
            runner._assert_expected(result, scenario["expected"], [])

    def test_catalog_terminal_assertions_require_real_operation_and_gate(self):
        bundle = deepcopy(self.bundle)
        scenario = next(item for item in bundle.catalog["scenarios"]
                        if item["id"] == "SC-083")
        scenario["expected"].pop("requiresRealCoordinatorExecution", None)
        scenario["materialization"]["steps"] = [
            step for step in scenario["materialization"]["steps"]
            if step["op"] != "COORDINATOR_PROCESS_INTENT"
        ]

        with self.assertRaisesRegex(
                ContractError, "coordinator-terminal assertions"):
            ScenarioRunner(bundle, self.vector)._check_verify_suite_operation_initial_state_and_fault_names()

    def test_real_execution_gate_requires_process_operation_without_terminal_fields(self):
        bundle = deepcopy(self.bundle)
        scenario = next(item for item in bundle.catalog["scenarios"]
                        if item["id"] == "SC-001")
        scenario["expected"]["requiresRealCoordinatorExecution"] = True

        with self.assertRaisesRegex(
                ContractError, "real Coordinator execution gate"):
            ScenarioRunner(bundle, self.vector)._check_verify_suite_operation_initial_state_and_fault_names()

    def test_real_execution_gate_runs_without_terminal_expected_fields(self):
        runner = ScenarioRunner(self.bundle, self.vector)
        scenario = self._scenario("SC-083")
        required = self.bundle.catalog["normativeSemantics"][
            "completeExpectedResult"]["requiredFields"]
        expected = {
            field: deepcopy(scenario["expected"][field]) for field in required
        }
        expected.update({
            field: deepcopy(scenario["expected"][field])
            for field in ("primaryHttpStatus", "httpSequence",
                          "normalRanWrites", "rollbackRanWrites",
                          "committedEvidenceRecords")
        })
        expected["requiresRealCoordinatorExecution"] = True
        result = ScenarioResult("SC-083", "PASS")
        result.http_sequence = deepcopy(expected["httpSequence"])
        result.evidence_commit_count = expected["committedEvidenceRecords"]
        result.observations = {
            **{key: deepcopy(value) for key, value in expected.items()
               if key not in {"httpSequence", "primaryHttpStatus",
                              "requiresRealCoordinatorExecution"}},
            "processIntentCalls": 0,
            "coordinatorExecutionMode": "SYNTHETIC_CONTRACT_TRANSITION",
            "coordinatorFsmHistory": [],
            "coordinatorTerminalOutcome": None,
            "coordinatorTerminalEvidenceRef": None,
            "coordinatorLedgerReferences": [],
        }

        with self.assertRaisesRegex(ContractError, "real Coordinator"):
            runner._assert_expected(result, expected, [])

    def test_real_execution_gate_rejects_synthetic_origin_in_fsm_history(self):
        runner = ScenarioRunner(self.bundle, self.vector)
        scenario = self._scenario("SC-083")
        required = self.bundle.catalog["normativeSemantics"][
            "completeExpectedResult"]["requiredFields"]
        expected = {
            field: deepcopy(scenario["expected"][field]) for field in required
        }
        expected.update({
            field: deepcopy(scenario["expected"][field])
            for field in ("primaryHttpStatus", "httpSequence",
                          "normalRanWrites", "rollbackRanWrites",
                          "committedEvidenceRecords")
        })
        expected["requiresRealCoordinatorExecution"] = True
        result = ScenarioResult("SC-083", "PASS")
        result.http_sequence = deepcopy(expected["httpSequence"])
        result.evidence_commit_count = expected["committedEvidenceRecords"]
        result.observations = {
            **{key: deepcopy(value) for key, value in expected.items()
               if key not in {"httpSequence", "primaryHttpStatus",
                              "requiresRealCoordinatorExecution"}},
            "processIntentCalls": 1,
            "coordinatorExecutionMode": "REAL_PROCESS_INTENT",
            "coordinatorFsmHistory": [
                {"from": "S0", "to": "S1", "origin": "REAL"},
                {"from": "S1", "to": "S2", "origin": "SYNTHETIC"},
                {"from": "S4", "to": "S6", "origin": "REAL"},
            ],
            "coordinatorTerminalOutcome": "commit_original",
            "coordinatorTerminalEvidenceRef": "evid-real",
            "coordinatorLedgerReferences": ["ledger-real"],
        }

        with self.assertRaisesRegex(ContractError, "synthetic-origin"):
            runner._assert_expected(result, expected, [])

    def test_status_body_patch_cannot_remove_or_add_expected_structure(self):
        for operation in (
                {"op": "remove", "path": "/aicStatus/statusSeq"},
                {"op": "add", "path": "/unexpected", "value": True}):
            with self.subTest(operation=operation["op"]):
                bundle = deepcopy(self.bundle)
                scenario = next(item for item in bundle.catalog["scenarios"]
                                if item["id"] == "SC-017")
                scenario["expected"]["statusBodyJsonPatch"] = [operation]
                with self.assertRaisesRegex(ContractError, "non-weakening"):
                    ScenarioRunner(
                        bundle, self.vector
                    )._check_verify_suite_operation_initial_state_and_fault_names()

    def test_status_replay_history_oracle_rejects_unexpected_persistence(self):
        status_7 = self.bundle.fixture("fixture://appliedVerifiedStatus")
        status_99 = deepcopy(status_7)
        status_99["aicStatus"]["statusSeq"] = 99
        harness = LocalFakeHarness(
            {"A1_EMIT_STATUS": {
                "statusSnapshot": status_99,
                "callbackAttempts": [204],
            }},
            {"statusHistory": [status_7, status_99]},
        )
        runner = ScenarioRunner(self.bundle, self.vector, harness=harness)
        result = ScenarioResult("SC-094-ORACLE", "PASS")
        scenario = self._scenario("SC-094")
        step = runner._resolve_static(runner._prepare_references(
            next(item for item in scenario["materialization"]["steps"]
                 if item["id"] == "old-epoch-high-seq-replay")))

        with self.assertRaisesRegex(
                ContractError, "persisted status history sequence"):
            runner._run_step(
                step, result, scenario["materialization"]["time"])

    def test_sc084_catalog_uses_real_process_intent(self):
        scenario = self._scenario("SC-084")
        operations = [step["op"] for step in scenario["materialization"]["steps"]]

        self.assertTrue(scenario["expected"]["requiresRealCoordinatorExecution"])
        self.assertEqual(1, operations.count("COORDINATOR_PROCESS_INTENT"))
        self.assertNotIn("COORDINATOR_TRANSITION", operations)

    def test_declared_o1_rule_without_evaluable_inputs_fails_preflight(self):
        bundle = deepcopy(self.bundle)
        scenario = next(item for item in bundle.catalog["scenarios"]
                        if item["id"] == "SC-001")
        scenario["rules"].append("RULE-O1-DIGEST-GATE")
        runner = ScenarioRunner(bundle, self.vector)
        with self.assertRaisesRegex(ContractError,
                                    "AIC_RUNNER_VACUOUS_RULE"):
            runner._check_verify_every_declared_rule_has_evaluable_step_or_output_inputs()


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = ContractBundle(CONTRACT_BUNDLE)
        cls.vector = valid_vector()

    def _scenario(self, identifier):
        return deepcopy(next(item for item in self.bundle.catalog["scenarios"] if item["id"] == identifier))

    def _sc002_harness(self, normal_writes=1):
        status = self.bundle.fixture("fixture://appliedVerifiedStatus")
        state = {"a1PolicyResource": "PRESENT", "enforceStatus": "ENFORCED", "policyState": "ACTIVE",
                 "policyTerminal": False, "episodeState": "APPLIED_VERIFIED", "episodeTerminal": True,
                 "statusBody": status}
        return LocalFakeHarness({"E2_CONTROL_RESULT": {"result": "ACK", "effectApplied": normal_writes == 1}}, state)

    def test_schema_valid_vector_and_full_preflight_without_bypass(self):
        validate_json_schema(self.vector, self.bundle.schema("deployment-test-vector"), self.bundle.path)
        runner = ScenarioRunner(self.bundle, self.vector)
        runner.preflight()
        self.assertTrue(runner._preflight_complete)

    def test_preflight_negative_vector_and_missing_netconf_fixture(self):
        invalid = deepcopy(self.vector)
        del invalid["topology"]
        with self.assertRaises(PreflightError):
            ScenarioRunner(self.bundle, invalid).preflight()
        original = self.bundle.catalog["fixtureRegistry"]["o1NetconfGetSchemaMount"]
        profile = self.bundle.runner.get("unused")
        self.bundle.catalog["fixtureRegistry"]["o1NetconfGetSchemaMount"] = "missing.xml"
        try:
            # Direct registry resolution is independently fail-closed; profile paths are checked by preflight.
            with self.assertRaises(ContractError):
                self.bundle.fixture("fixture://o1NetconfGetSchemaMount")
        finally:
            self.bundle.catalog["fixtureRegistry"]["o1NetconfGetSchemaMount"] = original

    def test_each_preflight_family_rejects_its_negative(self):
        def fresh():
            return ScenarioRunner(deepcopy(self.bundle), deepcopy(self.vector))

        runner = fresh(); runner.bundle.catalog["atomicScenarioCount"] += 1
        with self.assertRaises(ContractError): runner._check_parse_all_machine_artifacts()
        runner = fresh(); del runner.vector["topology"]
        with self.assertRaises(ContractError): runner._check_validate_deployment_vector_draft_2020_12()
        runner = fresh(); runner.vector["e2Inventory"]["connections"][0]["active"] = False
        with self.assertRaises(ContractError): runner._check_validate_e2_inventory_ready_implies_active_connections_active_required_ran_functions_and_unique_structured_node_ids()
        runner = fresh(); runner.vector["schemas"]["policyEvidenceFilterSchemaJcsSha256"] = "BAD"
        with self.assertRaises(ContractError): runner._check_verify_referenced_file_digests_when_declared()
        runner = fresh(); runner.vector["schemas"]["policyEvidenceRecordSchemaCanonicalJson"] = '{"different":true}'
        with self.assertRaises(ContractError): runner._check_verify_policy_evidence_schema_canonical_text_and_jcs_digest()
        runner = fresh(); profile = runner.bundle.fixture("fixture://o1NetconfYangProfile"); profile["teardown"] = []
        real_fixture = runner.bundle.fixture
        with patch.object(runner.bundle, "fixture", side_effect=lambda ref: profile if ref == "fixture://o1NetconfYangProfile" else real_fixture(ref)):
            with self.assertRaises(ContractError): runner._check_verify_o1_netconf_profile_and_every_lifecycle_rpc_fixture_resolve()
        runner = fresh(); runner.vector["o1"]["live"]["recoveryFiles"]["uniqueCandidate"]["fileExpirationTime"] = NOW
        with self.assertRaises(ContractError): runner._check_verify_deployment_file_info_ready_before_expiration()
        runner = fresh(); runner.bundle.catalog["requirements"][0]["scenarioIds"].append("SC-MISSING")
        with self.assertRaises(ContractError): runner._check_verify_scenario_and_requirement_links()
        runner = fresh(); runner.bundle.catalog["scenarios"][0]["materialization"]["steps"][0]["op"] = "NOT_AN_OPERATION"
        with self.assertRaises(ContractError): runner._check_verify_suite_operation_initial_state_and_fault_names()
        runner = fresh(); runner.bundle.catalog["scenarios"][0]["fixtureRefs"].append("fixture://missing")
        with self.assertRaises(ContractError): runner._check_resolve_fixture_references_and_static_expressions()
        runner = fresh(); runner.bundle.catalog["scenarios"][0]["materialization"]["steps"][0]["atMs"] = None
        with self.assertRaises(ContractError): runner._check_typecheck_endpoint_and_body_expressions()
        runner = fresh(); runner.bundle.catalog["scenarios"][0]["materialization"]["steps"][0]["body"] = "${steps.future.outputs.value}"
        with self.assertRaises(ContractError): runner._check_verify_every_step_output_reference_targets_an_earlier_step_and_a_declared_output()
        runner = fresh(); runner.bundle.catalog["scenarios"][0]["materialization"]["faults"] = [{"type": next(iter(runner.bundle.runner["faults"])), "beforeStep": "missing"}]
        with self.assertRaises(ContractError): runner._check_verify_fault_boundaries_name_existing_steps()
        runner = fresh(); runner.bundle.catalog["scenarios"][0]["name"] = "${deployment.missing}"
        with self.assertRaises(ContractError): runner._check_verify_no_unresolved_static_expression_remains()
        runner = fresh()
        publish = next(step for scenario in runner.bundle.catalog["scenarios"] for step in scenario["materialization"]["steps"] if step["op"] == "R1_DME_PUBLISH")
        del publish["evidenceRecordRef"]
        with self.assertRaises(ContractError): runner._check_verify_each_r1_dme_publish_has_exactly_one_evidence_record()

    def test_sc002_resolves_body_query_and_counts_actual_write(self):
        http = FakeHttp([HttpResponse(201, {}, {})], [self.bundle.fixture("fixture://policy")])
        with tempfile.TemporaryDirectory() as temporary:
            result = ScenarioRunner(self.bundle, self.vector, self._sc002_harness(), http, temporary).run(self._scenario("SC-002"))
        self.assertEqual(result.disposition, "PASS", result.reason)
        self.assertEqual(http.calls[0]["body"], self.bundle.fixture("fixture://policy"))
        self.assertIn("notificationDestination=https%3A%2F%2Flocalhost%2Fa1-status%2Fnotifications", http.calls[0]["url"])
        self.assertEqual(result.ran_write_counts, {"normal": 1, "rollback": 0})

    def test_live_http_uses_default_step_timeout_not_o1_capture_timeout(self):
        vector = deepcopy(self.vector)
        vector["timeouts"].update({"defaultStepMs": 4321, "liveCaptureMs": 1234})
        http = FakeHttp([HttpResponse(200, {}, {})])
        runner = ScenarioRunner(self.bundle, vector, http=http)
        result = ScenarioResult("SC-LIVE-HTTP-TIMEOUT", "PASS")

        runner._run_step(
            {
                "id": "query-policy-types",
                "op": "HTTP",
                "method": "GET",
                "endpointRef": "#/endpointTemplates/a1PolicyTypes",
            },
            result,
            {"mode": "LIVE_OBSERVED"},
        )

        self.assertEqual(4321, http.calls[0]["timeoutMs"])

    def test_fixture_embedded_json_patch_xml_edit_and_explicit_protocol_adapters(self):
        runner = ScenarioRunner(self.bundle, self.vector)
        policy = runner._prepare_references({"id": "x", "op": "HTTP", "bodyRef": "fixture://policy",
                                             "jsonPatch": [{"op": "replace", "path": "/priority", "value": 99}]})
        policy = runner._apply_json_edits(policy)
        self.assertEqual(policy["body"]["priority"], 99)
        embedded = runner._prepare_references({"id": "x", "op": "R1_DME_REGISTER",
                                               "body": {"schema": {"$fixtureRef": "fixture://policyEvidenceFilterSchema"}}})
        self.assertEqual(embedded["body"]["schema"], self.bundle.fixture("fixture://policyEvidenceFilterSchema"))
        xml = runner._prepare_references({"id": "x", "op": "O1_RETRIEVE", "artifactRef": "fixture://validPrbXml",
                                          "xmlEdits": [{"namespace": {"m": "http://www.3gpp.org/ftp/specs/archive/32_series/32.435#measCollec"},
                                                        "xpath": "/m:measCollecFile/m:measData/m:measInfo/m:measValue[@measObjLdn='GNBDUFunction=oai-du,NRCellDU=1']/m:r[@p='1']/text()",
                                                        "replaceText": "0"}]})
        self.assertIn(b">0<", xml["artifact"])
        with self.assertRaisesRegex(ContractError, "SftpBoundary"):
            runner._run_o1_retrieve({"source": "sftp://localhost/a", "timeoutMs": 1})
        with self.assertRaisesRegex(ContractError, "NetconfBoundary"):
            runner._run_netconf({"op": "PERF_METRIC_JOB", "action": "LOCK"})

    def test_initial_state_json_patch_targets_bare_policy_and_manifest(self):
        runner = ScenarioRunner(self.bundle, self.vector)
        policy_state = runner._prepare_references(
            self._scenario("SC-028")["materialization"]["initialState"][0])
        self.assertEqual(policy_state["policy"]["rollbackPolicy"]["on"],
                         ["READBACK_MISMATCH"])
        self.assertEqual(policy_state["status"], "ENFORCED_ACTIVE_NO_EPISODE")
        self.assertNotIn("jsonPatch", policy_state)

        manifest_state = runner._prepare_references(
            self._scenario("SC-059")["materialization"]["initialState"][0])
        cells = manifest_state["manifest"]["topology"]["cells"]
        self.assertEqual(len(cells), 3)
        self.assertEqual(
            cells[-1]["managedObjectDn"],
            "SubNetwork=oran-lab,ManagedElement=oai-gnb,GNBDUFunction=oai-du,NRCellDU=2")
        self.assertNotIn("jsonPatch", manifest_state)

    def test_initial_state_derives_policy_and_data_job_ids_from_scenario_contract(self):
        harness = LocalFakeHarness()
        runner = ScenarioRunner(self.bundle, self.vector, harness=harness)
        runner._install_initial_state(self._scenario("SC-048"))
        install = next(call for call in harness.calls
                       if call[0:2] == ("op", "INSTALL_NON_RT_DESIRED_POLICY"))
        self.assertEqual(
            self.bundle.fixture("fixture://appliedVerifiedStatus")["aicStatus"]["policyId"],
            install[2]["policyId"],
        )

        harness = LocalFakeHarness()
        runner = ScenarioRunner(self.bundle, self.vector, harness=harness)
        runner._install_initial_state(self._scenario("SC-049"))
        install = next(call for call in harness.calls
                       if call[0:2] == ("op", "INSTALL_ACTIVE_DATA_JOB"))
        query_step = next(step for step in self._scenario("SC-049")["materialization"]["steps"]
                          if step["op"] == "R1_DME_QUERY")
        self.assertEqual(query_step["bindings"]["dataJobId"], install[2]["dataJobId"])

        harness = LocalFakeHarness()
        runner = ScenarioRunner(self.bundle, self.vector, harness=harness)
        scenario = self._scenario("SC-086")
        runner._install_initial_state(scenario)
        install = next(call for call in harness.calls
                       if call[0:2] == ("op", "INSTALL_PINNED_SERVICE_DESCRIPTIONS"))
        self.assertEqual(scenario["expected"]["discoveredVersions"],
                         install[2]["discoveredVersions"])

    def test_loaded_capability_and_deployment_ue_drive_runtime_configuration(self):
        harness = LocalFakeHarness()
        runner = ScenarioRunner(self.bundle, self.vector, harness=harness)
        runner._install_initial_state(self._scenario("SC-059"))
        load = next(call for call in harness.calls
                    if call[0:2] == ("op", "LOAD_CAPABILITY"))
        self.assertIn(self.vector["topology"]["ueId"], load[2]["knownUeScopes"])
        expanded = runner._deployment_policy_ue_id()
        self.assertEqual(1, expanded["guAmfUeNgapId"]["amfUeNgapId"])
        self.assertIn(expanded, load[2]["knownUeScopes"])

        runner._install_initial_state(self._scenario("SC-064"))
        self.assertEqual(
            self.bundle.fixture("fixture://capabilityManifest"),
            runner._loaded_capability,
        )
        self.assertEqual(2, len(runner._provider_cell_mappings()))

        custom = deepcopy(self.bundle.fixture("fixture://capabilityManifest"))
        expected_dns = []
        for index, cell in enumerate(custom["topology"]["cells"]):
            cell["managedObjectDn"] = "CustomSubNetwork=runtime,NRCellDU=%d" % index
            if any(cell["cellId"] == item["cellId"]
                   for item in self.vector["topology"]["cellMappings"]):
                expected_dns.append(cell["managedObjectDn"])
        runner._loaded_capability = custom
        self.assertEqual(
            expected_dns,
            [item["managedObjectDn"] for item in runner._provider_cell_mappings()],
        )

    def test_symbolic_distinct_cell_count_uses_target_committed_pm_records(self):
        expected = {
            "primaryHttpStatus": None,
            "httpSequence": [],
            "a1PolicyResource": None,
            "enforceStatus": None,
            "policyState": None,
            "policyTerminal": None,
            "episodeState": None,
            "episodeTerminal": None,
            "normalRanWrites": 0,
            "rollbackRanWrites": 0,
            "errorCode": None,
            "evidenceQuality": None,
            "committedEvidenceRecords":
                "EQUALS_VALID_DISTINCT_NRCELLDU_COUNT_AFTER_DEDUP",
        }
        scenario = {
            "id": "SC-SYMBOLIC-PM-COUNT",
            "materialization": {
                "time": {"mode": "FIXED_LOGICAL", "origin": NOW},
                "initialState": [], "steps": [], "faults": [],
            },
            "expected": expected,
            "rules": [],
        }
        harness = LocalFakeHarness(
            observable_state={"pmRecordObjectsCreated": 2})

        with tempfile.TemporaryDirectory() as temporary:
            result = ScenarioRunner(
                self.bundle, self.vector, harness=harness,
                artifacts_root=temporary,
            ).run(scenario)

        self.assertEqual("PASS", result.disposition, result.reason)
        self.assertEqual(2, result.evidence_commit_count)

    def test_sc002_write_count_mismatch_is_fail(self):
        http = FakeHttp([201])
        with tempfile.TemporaryDirectory() as temporary:
            result = ScenarioRunner(self.bundle, self.vector, self._sc002_harness(normal_writes=0), http, temporary).run(self._scenario("SC-002"))
        self.assertEqual(result.disposition, "FAIL")
        self.assertIn("normalRanWrites differs", result.reason)

    def test_network_operation_uses_http_and_publish_count_dedupes(self):
        step = {"id": "publish", "op": "R1_DME_PUBLISH", "endpointRef": "#/endpointTemplates/r1DmePushDestination",
                "deliveryBindingId": "binding-0123456789012345", "evidenceRecordRef": "fixture://afterEvidence",
                "expectedHttpStatus": 204, "atMs": 0}
        runner = ScenarioRunner(self.bundle, self.vector, http=FakeHttp([204]))
        prepared = runner._resolve_static(runner._prepare_references(step))
        result = ScenarioResult("test", "PASS")
        outputs = runner._run_step(prepared, result, {"mode": "FIXED_LOGICAL"})
        self.assertTrue(outputs["accepted"])
        self.assertEqual(result.evidence_commit_count, 1)
        self.assertFalse(any(call[0] == "op" and call[1] == "R1_DME_PUBLISH" for call in runner.harness.calls))

    def test_fixed_logical_step_offset_reaches_target_harness(self):
        harness = LocalFakeHarness({"KPM_SNAPSHOT": {
            "snapshot": {}, "servingCellNcI": None, "selectedTargetNcI": None}})
        runner = ScenarioRunner(self.bundle, self.vector, harness=harness)
        result = ScenarioResult("SC-095", "PASS")
        scenario = self._scenario("SC-095")
        step = runner._resolve_static(runner._prepare_references(
            next(item for item in scenario["materialization"]["steps"]
                 if item["id"] == "below-threshold-snapshot")))
        runner._run_step(step, result, scenario["materialization"]["time"])
        call = next(item for item in harness.calls if item[0:2] == ("op", "KPM_SNAPSHOT"))
        self.assertEqual(1000, call[2]["atMs"])

    def test_a1_status_fixture_rebinds_to_server_assigned_r1_policy_id(self):
        dynamic_id = "runtime-policy-id"
        expected_status = self.bundle.fixture("fixture://appliedVerifiedStatus")
        expected_status["aicStatus"]["policyId"] = dynamic_id
        harness = LocalFakeHarness({"A1_EMIT_STATUS": {
            "statusSnapshot": expected_status,
            "callbackStatus": 204,
            "callbackAttempts": [204],
        }})
        runner = ScenarioRunner(self.bundle, self.vector, harness=harness)
        result = ScenarioResult("SC-083", "PASS")
        result.outputs["r1-create"] = {"policyId": dynamic_id}
        scenario = self._scenario("SC-083")
        step = runner._resolve_static(runner._prepare_references(
            next(item for item in scenario["materialization"]["steps"]
                 if item["id"] == "a1-status-to-framework")))
        outputs = runner._run_step(step, result, scenario["materialization"]["time"])
        self.assertEqual(dynamic_id, outputs["statusSnapshot"]["aicStatus"]["policyId"])

    def test_netconf_unlock_receives_durable_subscription_source(self):
        boundary = FakeNetconf()
        runner = ScenarioRunner(self.bundle, self.vector, netconf=boundary)
        result = ScenarioResult("SC-051", "PASS")
        result.outputs["subscribe"] = {
            "status": 201,
            "headers": {"Location": "https://localhost/subscriptions/sub-1"},
            "body": {"consumerReference": "https://localhost/consumer", "timeTick": 0},
        }
        scenario = self._scenario("SC-051")
        step = runner._resolve_static(runner._prepare_references(
            next(item for item in scenario["materialization"]["steps"]
                 if item["id"] == "unlock-job")))
        runner._run_step(step, result, scenario["materialization"]["time"])
        self.assertEqual(result.outputs["subscribe"], boundary.steps[0]["dependencySource"])

    def test_o1_retrieve_resolves_captured_file_location_from_prior_notification(self):
        boundary = FakeSftp()
        runner = ScenarioRunner(self.bundle, self.vector, sftp=boundary)
        result = ScenarioResult("SC-051", "PASS")
        file_info = self.bundle.fixture("fixture://notifyFileReady")["fileInfoList"][0]
        result.outputs["capture-notify"] = {"status": 204, "fileInfoList": [file_info]}
        scenario = self._scenario("SC-051")
        step = runner._resolve_static(runner._prepare_references(
            next(item for item in scenario["materialization"]["steps"]
                 if item["id"] == "retrieve")))
        outputs = runner._run_step(step, result, scenario["materialization"]["time"])
        self.assertEqual([file_info["fileLocation"]], boundary.sources)
        self.assertEqual(file_info["jobId"], outputs["fileInfo"]["jobId"])
        self.assertEqual(boundary.last_retrieved_at,
                         outputs["fileInfo"]["retrievedAt"])
        self.assertNotEqual(outputs["fileInfo"]["readyAt"],
                            outputs["fileInfo"]["retrievedAt"])

        normalize = runner._resolve_static(runner._prepare_references(
            next(item for item in scenario["materialization"]["steps"]
                 if item["id"] == "normalize")))
        resolved = runner._resolve_legacy_templates(normalize, {})
        self.assertIn("{subscriptionId}",
                      resolved["profileRef"]["delivery"]["subscription"]["itemResource"])

    def _run_live_o1_notification(self):
        harness = LocalFakeHarness({"O1_NORMALIZE": {
            "records": [], "commitEligibleRecords": [], "rejectedRecords": [],
            "duplicateRecords": [], "samples": [],
        }})
        http = FakeHttp([HttpResponse(204, {}, None)])
        runner = ScenarioRunner(
            self.bundle, self.vector, harness=harness, http=http,
            sftp=FakeSftp(self.bundle.fixture("fixture://validPrbXml")),
        )
        runner._scenario_deployment = runner._live_observed_deployment()
        runner._loaded_capability = self.bundle.fixture("fixture://capabilityManifest")
        scenario = self._scenario("SC-084")
        step = runner._resolve_static(runner._prepare_references(
            next(item for item in scenario["materialization"]["steps"]
                 if item["id"] == "o1-notify")))
        step["afterActionCollection"] = True
        step["afterActionDelayMs"] = 1
        step["collectionDurationMs"] = 60000
        result = ScenarioResult("SC-084", "PASS")
        result.outputs["action-status"] = {"statusSnapshot": {"aicStatus": {
            "occurredAt": "2026-08-04T00:00:10Z",
        }}}
        runner._run_step(step, result,
                         scenario["materialization"]["time"])
        configure = next(call for call in harness.calls
                         if call[0:2] == ("op", "CONFIGURE_LIVE_PM_PROFILE"))
        return runner, configure[2], http.calls[0]["body"]

    def test_live_deployment_shifts_recovery_files_with_measurement_window(self):
        runner = ScenarioRunner(self.bundle, self.vector)
        deployment = runner._live_observed_deployment()

        def parsed(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))

        original_end = parsed(
            self.vector["o1"]["live"]["expectedMeasurementWindow"]["end"])
        live_end = parsed(
            deployment["o1"]["live"]["expectedMeasurementWindow"]["end"])
        original_files = self.vector["o1"]["live"]["recoveryFiles"]
        live_files = deployment["o1"]["live"]["recoveryFiles"]
        pairs = [(original_files["uniqueCandidate"], live_files["uniqueCandidate"])]
        pairs.extend(zip(original_files["ambiguousCandidates"],
                         live_files["ambiguousCandidates"]))
        for original, live in pairs:
            self.assertEqual(
                parsed(original["fileReadyTime"]) - original_end,
                parsed(live["fileReadyTime"]) - live_end,
            )
            self.assertEqual(
                parsed(original["fileExpirationTime"])
                - parsed(original["fileReadyTime"]),
                parsed(live["fileExpirationTime"])
                - parsed(live["fileReadyTime"]),
            )

    def test_live_o1_notification_event_matches_captured_file_ready_time(self):
        _runner, _configuration, notification = self._run_live_o1_notification()
        self.assertEqual(
            notification["fileInfoList"][0]["fileReadyTime"],
            notification["eventTime"],
        )

    def test_live_after_action_collection_window_starts_after_action(self):
        runner, configuration, _notification = self._run_live_o1_notification()

        self.assertEqual(configuration["measurementWindow"], {
            "start": "2026-08-04T00:00:10.001Z",
            "end": "2026-08-04T00:01:10.001Z",
        })
        self.assertEqual(valid_vector()["o1"]["live"]["expectedMeasurementWindow"],
                         runner.vector["o1"]["live"]["expectedMeasurementWindow"])

    def test_fixed_logical_after_action_collection_preserves_aliased_vector(self):
        harness = LocalFakeHarness()
        runner = ScenarioRunner(
            self.bundle, self.vector, harness=harness,
            http=FakeHttp([HttpResponse(204, {}, None)]),
            sftp=FakeSftp(self.bundle.fixture("fixture://validPrbXml")),
        )
        runner._loaded_capability = self.bundle.fixture(
            "fixture://capabilityManifest")
        self.assertIs(runner._scenario_deployment, runner.vector)
        original_vector = deepcopy(runner.vector)
        scenario = self._scenario("SC-084")
        step = runner._resolve_static(runner._prepare_references(
            next(item for item in scenario["materialization"]["steps"]
                 if item["id"] == "o1-notify")))
        step.update({
            "afterActionCollection": True,
            "afterActionDelayMs": 1,
            "collectionDurationMs": 60000,
        })
        result = ScenarioResult("SC-084-ALIASED", "PASS")
        result.outputs["action-status"] = {"statusSnapshot": {"aicStatus": {
            "occurredAt": "2026-08-04T00:00:10Z",
        }}}

        runner._run_step(step, result, {
            "mode": "FIXED_LOGICAL",
            "origin": "2026-08-04T00:00:00Z",
        })

        self.assertEqual(original_vector, runner.vector)
        self.assertIsNot(runner._scenario_deployment, runner.vector)

    def test_live_o1_profile_uses_provider_generated_xml(self):
        _runner, configuration, _notification = self._run_live_o1_notification()
        self.assertNotIn("artifactBase64", configuration)

    def test_live_o1_file_info_uses_deployment_perf_job_id(self):
        _runner, _configuration, notification = self._run_live_o1_notification()
        self.assertEqual(
            self.vector["o1"]["perfMetricJob"]["jobId"],
            notification["fileInfoList"][0]["jobId"],
        )

    def test_live_o1_normalization_uses_captured_policy_and_status_context(self):
        harness = LocalFakeHarness({"O1_NORMALIZE": {
            "records": [], "commitEligibleRecords": [], "rejectedRecords": [],
            "duplicateRecords": [], "samples": [],
        }})
        runner = ScenarioRunner(self.bundle, self.vector, harness=harness)
        runner._scenario_deployment = runner._live_observed_deployment()
        scenario = self._scenario("SC-084")
        result = ScenarioResult("SC-084", "PASS")

        policy = deepcopy(self.bundle.fixture("fixture://policy"))
        policy["scope"]["ueId"] = runner._deployment_policy_ue_id()
        status = deepcopy(self.bundle.fixture("fixture://appliedVerifiedStatus"))
        status["aicStatus"].update({
            "policyId": "live-policy-1",
            "episodeId": "a98ff8fd-9318-4c32-b166-5fd84931717c",
        })
        status["aicStatus"]["control"].update({
            "transactionId": "e4b0df9c-61a0-44bd-a9c2-139474381aaa",
            "actionId": "66bd64bd-d386-42f0-8a74-d9e766098593",
        })
        file_info = deepcopy(
            self.bundle.fixture("fixture://notifyFileReady")["fileInfoList"][0])
        result.outputs.update({
            "r1-create": {"policyId": "live-policy-1", "policyObject": policy},
            "a1-status-to-framework": {"statusSnapshot": status},
            "o1-retrieve": {
                "bytes": self.bundle.fixture("fixture://validPrbXml"),
                "fileInfo": file_info,
            },
        })
        normalize = runner._resolve_static(runner._prepare_references(
            next(step for step in scenario["materialization"]["steps"]
                 if step["id"] == "normalize-o1")))

        runner._run_step(normalize, result, scenario["materialization"]["time"])

        arguments = next(call[2] for call in harness.calls
                         if call[0:2] == ("op", "O1_NORMALIZE"))
        self.assertEqual(policy["scope"], arguments["policyScope"])
        self.assertEqual({
            "policyTypeId": self.bundle.fixture(
                "fixture://afterEvidence")["correlation"]["policyTypeId"],
            "policyId": "live-policy-1",
            "policyRevision": status["aicStatus"]["policyRevision"],
            "episodeId": status["aicStatus"]["episodeId"],
            "transactionId": status["aicStatus"]["control"]["transactionId"],
            "actionId": status["aicStatus"]["control"]["actionId"],
        }, arguments["correlation"])

    def test_exact_pm_fixture_uses_contract_retrieval_timestamp_for_normalization(self):
        records = [
            self.bundle.fixture("fixture://afterEvidence"),
            self.bundle.fixture("fixture://afterEvidenceCell2"),
        ]
        harness = LocalFakeHarness({"O1_NORMALIZE": {
            "records": records,
            "commitEligibleRecords": records,
            "rejectedRecords": [],
            "duplicateRecords": [],
            "samples": [sample for record in records for sample in record["samples"]],
        }})
        runner = ScenarioRunner(self.bundle, self.vector, harness=harness)
        scenario = self._scenario("SC-052")
        result = ScenarioResult("SC-052", "PASS")
        parse = runner._resolve_static(runner._prepare_references(
            next(step for step in scenario["materialization"]["steps"]
                 if step["id"] == "parse")))
        result.outputs["parse"] = runner._run_step(
            parse, result, scenario["materialization"]["time"])
        normalize = runner._resolve_static(runner._prepare_references(
            next(step for step in scenario["materialization"]["steps"]
                 if step["id"] == "normalize")))

        runner._run_step(normalize, result, scenario["materialization"]["time"])

        call = next(item for item in harness.calls
                    if item[0:2] == ("op", "O1_NORMALIZE"))
        self.assertEqual(
            self.bundle.fixture("fixture://afterEvidence")["source"]["file"]["retrievedAt"],
            call[2]["fileInfo"]["retrievedAt"],
        )

    def test_faults_reports_vectors_and_traceability_merge(self):
        harness = LocalFakeHarness()
        runner = ScenarioRunner(self.bundle, self.vector, harness=harness)
        runner._schedule_faults([{"type": "DROP_STATUS", "beforeStep": "x"}], "beforeStep", "x")
        self.assertEqual(harness.calls[0][0:2], ("fault", "DROP_STATUS"))
        runner._schedule_faults(
            [{"type": "TLS_HANDSHAKE_REJECT", "beforeStep": "tls"}],
            "beforeStep", "tls")
        self.assertEqual("TLS_HANDSHAKE_FAILED", runner._transport_outcomes["tls"])
        dropped = ScenarioResult("SC-DROP", "PASS", http_sequence=[201])
        dropped.outputs["create"] = {"status": 201}
        dropped.responses.append({"stepId": "create", "status": 201, "headers": {"Version": "1.0.0"},
                                  "body": {"committed": True}, "locationLastSegment": None})
        runner._schedule_faults([{"type": "DROP_HTTP_RESPONSE", "afterStep": "create"}],
                                "afterStep", "create", dropped)
        self.assertEqual(dropped.http_sequence, ["DROPPED"])
        self.assertNotIn("create", dropped.outputs)
        self.assertEqual(dropped.responses[0]["status"], "DROPPED")
        values = local_development_vector(self.vector, insecure_dev_loopback=True)
        self.assertEqual(values["r1"]["apiRoot"], "https://localhost/r1")
        bad = deepcopy(self.vector); bad["o1"]["fileDataReporting"]["consumerReference"] = "https://example.com/callback"
        with self.assertRaises(ContractError): local_development_vector(bad, insecure_dev_loopback=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = ScenarioResult("SC-001", "PASS"); second = ScenarioResult("SC-002", "FAIL", "x")
            manifest = suite_manifest([first, second], root / "manifest.json")
            self.assertEqual(manifest["summary"]["PASS"], 1)
            traceability_report(self.bundle, [first], root / "trace.json")
            merged = traceability_report(self.bundle, [second], root / "trace.json")
            rows = [row for item in merged["matrix"] for row in item["scenarios"]]
            self.assertIn({"scenarioId": "SC-001", "disposition": "PASS", "reason": None}, rows)
            self.assertIn({"scenarioId": "SC-002", "disposition": "FAIL", "reason": "x"}, rows)

    def test_tls_handshake_fault_fails_before_application_dispatch(self):
        http = FakeHttp([OSError("observed transport rejection")])
        runner = ScenarioRunner(self.bundle, self.vector, http=http)
        runner._transport_outcomes["subscribe"] = "TLS_HANDSHAKE_FAILED"
        result = ScenarioResult("SC-TLS", "PASS")
        outputs = runner._run_http({
            "id": "subscribe", "op": "HTTP", "method": "POST",
            "endpointRef": "#/endpointTemplates/o1Subscriptions",
            "body": {"consumerReference": "http://127.0.0.1/callback"},
        }, result)
        self.assertEqual("TLS_HANDSHAKE_FAILED", outputs["status"])
        self.assertEqual(1, len(http.calls), "runner must observe the target transport rejection")

    def test_declared_callback_retry_schedule_records_each_dropped_attempt(self):
        status = self.bundle.fixture("fixture://appliedVerifiedStatus")
        harness = LocalFakeHarness({"A1_EMIT_STATUS": {
            "statusSnapshot": status,
            "callbackStatus": "DROPPED",
            "callbackAttempts": ["DROPPED"] * 6,
        }})
        runner = ScenarioRunner(self.bundle, self.vector, harness=harness)
        result = ScenarioResult("SC-CALLBACK", "PASS")
        outputs = runner._run_http_operation({
            "id": "emit", "op": "A1_EMIT_STATUS", "status": status,
        }, result)
        self.assertEqual("DROPPED", outputs["callbackStatus"])
        self.assertEqual(["DROPPED"] * 6, result.http_sequence)
        self.assertEqual(6, len(result.responses))
        self.assertEqual(6, len(result.requests))

    def test_status_observations_are_projected_from_wire_response(self):
        status = self.bundle.fixture("fixture://appliedVerifiedStatus")
        observed = ScenarioRunner._status_observations(status)
        self.assertEqual("ENFORCED", observed["enforceStatus"])
        self.assertEqual("ACTIVE", observed["policyState"])
        self.assertEqual("APPLIED_VERIFIED", observed["episodeState"])
        self.assertTrue(observed["episodeTerminal"])

    def test_sc085_not_applicable_evidence_records_condition_and_excludes(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner = ScenarioRunner(self.bundle, self.vector, artifacts_root=temporary)
            result = runner.run(self._scenario("SC-085"))
            evidence = json.loads((Path(temporary) / "SC-085" / "expectation.json").read_text(encoding="utf-8"))
        self.assertEqual(result.disposition, "SKIPPED_NOT_APPLICABLE")
        self.assertEqual(evidence["applicability"]["conditionId"], "RAPP_PUBLISHES_GENERAL_REQUEST_RESPONSE_R1_API")
        self.assertEqual(evidence["applicability"]["excludes"], ["R1_CONSUMER_ONBOARDING_IDENTITY", "DME_PUSH_CALLBACK"])
        self.assertEqual(runner.harness.calls, [])


class CatalogRunnerFailClosedTests(unittest.TestCase):
    """The catalog CLI itself, rather than helpers, rejects bad raw evidence."""

    _runner_source = Path(__file__).resolve().parents[1] / "scripts" / "run_conformance_catalog.py"
    _catalog_source = (Path(__file__).resolve().parents[1] / "contracts" / "oran-aic" / "1.0.1" /
                       "shared-contract-bundle" / "scenario-catalog.1.0.1.json")

    _fake_child = r'''
import collections
import json
import os
import sys
from pathlib import Path

suite, artifacts, profile = sys.argv[1:]
root = Path(__file__).resolve().parent
catalog = json.loads((root / "contracts/oran-aic/1.0.1/shared-contract-bundle/scenario-catalog.1.0.1.json").read_text())
mode = os.environ.get("FAKE_CATALOG_MODE", "good")
if mode == "child-crash":
    print("intentional fake child crash", file=sys.stderr)
    raise SystemExit(23)
if mode == "missing-profile" and profile == "mock-alt":
    raise SystemExit(0)
if mode == "missing-suite" and suite == "o1-lifecycle-contract":
    raise SystemExit(0)
rows = [item for item in catalog["scenarios"] if item["suite"] == suite]
if mode == "zero-result":
    rows = []
if mode == "partial-result":
    rows = rows[:-1]
if mode == "duplicate-id" and rows:
    rows.append(rows[0])
results = []
for index, scenario in enumerate(rows):
    identifier = scenario["id"]
    if mode == "missing-unexpected-id" and index == 0:
        identifier = "SC-999"
    disposition = "SKIPPED_NOT_APPLICABLE" if identifier == "SC-085" else "PASS"
    if mode == "all-skip":
        disposition = "SKIPPED_NOT_APPLICABLE"
    value = {"scenarioId": identifier, "disposition": disposition, "reason": None,
             "observations": {"httpInteractions": []}}
    if mode == "revision-mismatch":
        value["catalogRunner"] = {"sourceRevision": "not-the-executed-revision"}
    result_path = Path(artifacts) / ("case-%03d" % index) / "execution-result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(value))
    results.append({"scenarioId": identifier, "disposition": disposition})
counts = collections.Counter(item["disposition"] for item in results)
Path(artifacts).mkdir(parents=True, exist_ok=True)
(Path(artifacts) / "suite-manifest.json").write_text(json.dumps({
    "scenarios": results,
    "summary": {key: counts[key] for key in ("PASS", "FAIL", "SKIPPED_NOT_APPLICABLE")},
}))
'''

    def _repository(self, directory: Path) -> tuple[Path, Path]:
        (directory / "scripts").mkdir()
        (directory / "contracts/oran-aic/1.0.1/shared-contract-bundle").mkdir(parents=True)
        shutil.copy2(self._runner_source, directory / "scripts/run_conformance_catalog.py")
        shutil.copy2(self._catalog_source,
                     directory / "contracts/oran-aic/1.0.1/shared-contract-bundle/scenario-catalog.1.0.1.json")
        (directory / "tracked-input.txt").write_text("clean\n", encoding="utf-8")
        child = directory / "fake_catalog_child.py"
        child.write_text(self._fake_child, encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=directory, check=True)
        subprocess.run(["git", "config", "user.email", "catalog-test@example.invalid"], cwd=directory, check=True)
        subprocess.run(["git", "config", "user.name", "Catalog Test"], cwd=directory, check=True)
        subprocess.run(["git", "add", "scripts", "contracts", "tracked-input.txt"], cwd=directory, check=True)
        subprocess.run(["git", "commit", "-qm", "catalog runner fixture"], cwd=directory, check=True)
        return directory / "scripts/run_conformance_catalog.py", child

    def _run_bad_catalog(self, mode: str, *, dirty: bool = False) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            runner, child = self._repository(directory)
            if dirty:
                (directory / "tracked-input.txt").write_text("dirty\n", encoding="utf-8")
            environment = dict(os.environ, FAKE_CATALOG_MODE=mode)
            command = [
                sys.executable, str(runner), "--profile", "both", "--evidence-root", "raw-negative",
                "--child-command", "%s %s {suite} {artifacts} {profile}" % (sys.executable, child),
            ]
            completed = subprocess.run(command, cwd=directory, text=True, capture_output=True,
                                       check=False, env=environment)
            self.assertNotEqual(0, completed.returncode, completed.stdout + completed.stderr)
            failure = directory / "raw-negative/catalog-runner-failure.json"
            self.assertTrue(failure.is_file(), "raw fail-closed evidence missing: " + completed.stderr)
            self.assertEqual("FAIL_CLOSED", json.loads(failure.read_text(encoding="utf-8"))["status"])

    def test_zero_result_exits_nonzero(self):
        self._run_bad_catalog("zero-result")

    def test_child_crash_exits_nonzero(self):
        self._run_bad_catalog("child-crash")

    def test_partial_result_exits_nonzero(self):
        self._run_bad_catalog("partial-result")

    def test_all_skip_exits_nonzero(self):
        self._run_bad_catalog("all-skip")

    def test_duplicate_id_exits_nonzero(self):
        self._run_bad_catalog("duplicate-id")

    def test_missing_and_unexpected_id_exits_nonzero(self):
        self._run_bad_catalog("missing-unexpected-id")

    def test_missing_profile_exits_nonzero(self):
        self._run_bad_catalog("missing-profile")

    def test_missing_suite_exits_nonzero(self):
        self._run_bad_catalog("missing-suite")

    def test_dirty_tracked_tree_exits_nonzero(self):
        self._run_bad_catalog("good", dirty=True)

    def test_raw_revision_mismatch_exits_nonzero(self):
        self._run_bad_catalog("revision-mismatch")

    def test_complete_catalog_attests_then_revalidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            runner, child = self._repository(directory)
            command = [
                sys.executable, str(runner), "--profile", "both", "--evidence-root", "raw-positive",
                "--child-command", "%s %s {suite} {artifacts} {profile}" % (sys.executable, child),
            ]
            completed = subprocess.run(command, cwd=directory, text=True, capture_output=True, check=False)
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            revalidated = subprocess.run(
                [sys.executable, str(runner), "--profile", "both", "--evidence-root", "raw-positive",
                 "--validate-existing"],
                cwd=directory, text=True, capture_output=True, check=False)
            self.assertEqual(0, revalidated.returncode, revalidated.stdout + revalidated.stderr)
            report = json.loads((directory / "raw-positive/mock-local/all-suites.json").read_text(encoding="utf-8"))
            self.assertEqual(101, sum(report["totals"].values()))
            self.assertEqual(0, report["targetCalls"]["externalLiveTargetCalls"])
            self.assertIn("sourceTreeDigest", report)


if __name__ == "__main__":
    unittest.main()
