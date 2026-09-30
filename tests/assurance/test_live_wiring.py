"""Gate 3 live wiring stays file-bound, injected, and hardware-free."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from assurance.gateway.commands import GatewayOperation, build_command
from assurance.gateway.token import TokenKind
from assurance.gateway.write_gateway import GatewayOutcome
from tests.assurance.kgw_support import token


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binding_document(source: Path, *, source_digest: str | None = None) -> dict:
    return {
        "schemaVersion": "assurance-live-binding/1.0.0",
        "bindingId": "oran-lab-live-20260819",
        "sources": [{"path": str(source), "sha256": source_digest or _sha256(source)}],
        "r1": {
            "apiRoot": "https://192.168.50.1:18443/r1",
            "nearRtRicId": "near-rt-ric-lics-lab-001",
            "policyTypeId": "AIC_UECellSteering_1.0.0",
            "secretRefs": {
                "mtlsCa": "file:/run/secrets/ca.crt",
                "mtlsClientCertificate": "file:/run/secrets/r1-client.crt",
                "mtlsClientPrivateKey": "file:/run/secrets/r1-client.key",
                "oauthToken": "file:/run/secrets/r1-token",
            },
            "polling": {"cadenceMs": 100, "deadlineMs": 250},
        },
        "a1p": {
            "apiRoot": "https://192.168.50.1:9444/A1-P/v2",
            "secretRefs": {
                "mtlsCa": "file:/run/secrets/ca.crt",
                "mtlsClientCertificate": "file:/run/secrets/a1p-client.crt",
                "mtlsClientPrivateKey": "file:/run/secrets/a1p-client.key",
                "oauthToken": "file:/run/secrets/a1p-token",
            },
        },
        "o1": {
            "httpsRoot": "https://192.168.50.1:8443/o1",
            "netconf": "ssh://192.168.50.1:830",
            "sftp": "sftp://192.168.50.1:2022/pm",
            "pmDirectory": "/var/lib/oran/pm",
            "secretRefs": {
                "mtlsCa": "file:/run/secrets/ca.crt",
                "mtlsClientCertificate": "file:/run/secrets/o1-client.crt",
                "mtlsClientPrivateKey": "file:/run/secrets/o1-client.key",
                "oauthToken": "file:/run/secrets/o1-token",
            },
        },
        "kpm": {
            "jsonlPath": "/var/lib/oran/kpm.jsonl",
            "expectedEpochs": {
                "ngran=02;plmn=208-095-2;nb=0000003584/00;cudu=none:00000000000000000000": 161,
                "ngran=02;plmn=208-095-2;nb=0000002816/00;cudu=none:00000000000000000000": 162,
            },
        },
        "e2Nodes": ["0x00000e00", "0x00000b00"],
        "cells": [12345678, 87654321],
        "plmn": {"mcc": "208", "mnc": "95"},
    }


class LiveBindingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "deployment-record.json"
        self.source.write_text('{"session":"live"}\n', encoding="utf-8")
        self.path = self.root / "binding.json"
        self.path.write_text(json.dumps(_binding_document(self.source)), encoding="utf-8")

    def test_loads_secret_free_live_identity_and_verifies_source_digest(self):
        from assurance.contracts.live_binding import load_assurance_live_binding

        binding = load_assurance_live_binding(self.path)

        self.assertEqual(binding.r1.api_root, "https://192.168.50.1:18443/r1")
        self.assertEqual(binding.e2_nodes, ("0x00000e00", "0x00000b00"))
        self.assertEqual(binding.cells, (12345678, 87654321))
        self.assertEqual(binding.r1.deployment.secret_refs["oauthToken"], "file:/run/secrets/r1-token")
        self.assertEqual(binding.a1p.secret_refs["mtlsClientCertificate"], "file:/run/secrets/a1p-client.crt")
        self.assertEqual(binding.o1.secret_refs["oauthToken"], "file:/run/secrets/o1-token")

    def test_refuses_changed_identity_source_before_binding_it(self):
        from assurance.contracts.live_binding import LiveBindingError, load_assurance_live_binding

        self.source.write_text('{"session":"changed"}\n', encoding="utf-8")

        with self.assertRaises(LiveBindingError):
            load_assurance_live_binding(self.path)


class _FakePolicyPort:
    def __init__(self):
        self.statuses = [
            {"aicStatus": {"episodeTerminal": False, "readback": {"result": "PENDING"}}},
            {"aicStatus": {"episodeTerminal": False, "readback": {"result": "PENDING"}}},
            {"enforceStatus": "ENFORCED", "aicStatus": {"episodeTerminal": True, "readback": {"result": "VERIFIED", "observedServingCell": 87654321}}},
        ]
        self.status_calls = []

    def get_policy_type(self, policy_type_id):
        return {"policyTypeId": policy_type_id}

    def create_policy(self, near_rt_ric_id, policy_type_id, policy_object):
        return {"policyId": "policy-1"}

    def update_policy(self, policy_id, policy_object):
        return {}

    def delete_policy(self, policy_id):
        return None

    def get_policy_status(self, policy_id):
        self.status_calls.append(policy_id)
        return self.statuses.pop(0)


class LiveR1FactoryTests(unittest.TestCase):
    def test_factory_polls_injected_r1_status_until_verified_readback(self):
        from assurance.gateway.live import build_live_r1_adapter_from_transport
        from assurance.contracts.live_binding import load_assurance_live_binding

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text("{}", encoding="utf-8")
            path = root / "binding.json"
            path.write_text(json.dumps(_binding_document(source)), encoding="utf-8")
            binding = load_assurance_live_binding(path)
            port = _FakePolicyPort()
            elapsed = [0]
            adapter = build_live_r1_adapter_from_transport(
                binding, transport_factory=lambda received: port if received is binding.r1 and received.deployment.secret_refs["oauthToken"] == "file:/run/secrets/r1-token" else self.fail("wrong binding"),
                policy_builder=lambda command: {"validated": command["axis"]},
                monotonic_ms=lambda: elapsed[0], sleep_ms=lambda ms: elapsed.__setitem__(0, elapsed[0] + ms),
            )
            commit = token(TokenKind.COMMIT)
            command = build_command(commit, GatewayOperation.APPLY, axis="servingCell", value=87654321, scope={"ue": "UE1"})
            self.assertIs(adapter.dispatch(token=commit, command=command).outcome, GatewayOutcome.ACKED)
            final = token(TokenKind.FINALIZE_LIVE)
            result = adapter.dispatch(token=final, command=build_command(final, GatewayOperation.FINALIZE, scope={"ue": "UE1"}))

        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(port.status_calls, ["policy-1", "policy-1", "policy-1"])
        self.assertEqual(elapsed[0], 100)

    def test_poller_stops_at_terminal_status_without_unbounded_gets(self):
        from assurance.gateway.live import R1StatusPoller

        port = _FakePolicyPort()
        port.statuses = [{"aicStatus": {"episodeTerminal": True, "readback": {"result": "PENDING"}}}]
        poller = R1StatusPoller(port, cadence_ms=100, deadline_ms=250, monotonic_ms=lambda: 0, sleep_ms=lambda _: self.fail("must not sleep"))

        self.assertIsNone(poller.readback(scope={}, transaction_id="tx", policy_id="policy-1"))
        self.assertEqual(port.status_calls, ["policy-1"])


class CollectorConfigurationTests(unittest.TestCase):
    def test_collector_configuration_injects_pm_directory_and_kpm_path(self):
        from assurance.collector.live import load_live_collector_configuration
        from assurance.contracts.live_binding import load_assurance_live_binding

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text("{}", encoding="utf-8")
            path = root / "binding.json"
            path.write_text(json.dumps(_binding_document(source)), encoding="utf-8")
            configuration = load_live_collector_configuration(load_assurance_live_binding(path))

        self.assertEqual(configuration.pm_directory, "/var/lib/oran/pm")
        self.assertEqual(configuration.kpm_jsonl_path, "/var/lib/oran/kpm.jsonl")
        self.assertEqual(configuration.o1_pm.describe_source()["sourceId"], "live-o1-pm")

    def test_committed_live_kpm_path_is_explicitly_validated_when_present(self):
        from assurance.collector.live import validate_kpm_jsonl
        from assurance.contracts.live_binding import load_assurance_live_binding

        binding = load_assurance_live_binding(Path("deployment/assurance-live-binding.1.0.0.json"))
        report = validate_kpm_jsonl(binding.kpm_jsonl_path, expected_epochs=binding.kpm_expected_epochs, max_lines=16)

        self.assertIn(report.state, {"MISSING", "UNREADABLE", "VALID", "COMPATIBLE", "PARTIAL"})
        if report.exists:
            # The live stream's epochs and fill state are runtime state that
            # changes on every re-pin, so the read-only validation asserts the
            # adapter parsed the real file into a non-error verdict, not one
            # specific fill state.  A freshly rotated stream is legitimately
            # PARTIAL until its window fills; a settled one is COMPATIBLE.
            self.assertIn(report.state, {"VALID", "COMPATIBLE", "PARTIAL", "EMPTY"})
            self.assertEqual(report.lines_examined, 16)
            # 2026-09-22: `PARTIAL` 은 정의상 표본이 0 이다
            # (`"COMPATIBLE" if result.samples else "PARTIAL"`).  그런데 이 시험은
            # PARTIAL 을 허용하면서 동시에 표본 > 0 을 요구해 그 경우 절대 통과할 수
            # 없었다 -- capture 는 08-19 부터 append-only 라 머리 16줄이 지금 pin 보다
            # 훨씬 낡았고(실측 invalid 11/16), 그래서 늘 PARTIAL 이다.  표본은 파싱이
            # 성사된 상태에서만 요구한다.
            if report.state in {"VALID", "COMPATIBLE"}:
                self.assertGreater(report.sample_count, 0)

    def test_live_kpm_validation_reports_presence_and_parses_read_only_file(self):
        from assurance.collector.live import validate_kpm_jsonl

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kpm.jsonl"
            path.write_text(json.dumps({
                "event": "kpm_indication", "e2_node": "node-1", "connection_epoch": 7,
                "recv_unix_us": 1_700_000_000_000_000, "slot": 0,
                "measurements": [{"name": "RRC.ConnMean", "type": "int", "value": 2}],
            }) + "\n", encoding="utf-8")
            report = validate_kpm_jsonl(path, expected_epochs={"node-1": 7}, max_lines=1)

        self.assertTrue(report.exists)
        self.assertEqual(report.lines_examined, 1)
        self.assertEqual(report.sample_count, 1)
        self.assertEqual(report.invalid_records, 0)

    def test_kpm_metadata_records_do_not_mask_compatible_indications(self):
        from assurance.collector.live import validate_kpm_jsonl

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kpm.jsonl"
            path.write_text("{\"event\": \"e2_nodes\", \"count\": 1}\n" + json.dumps({
                "event": "kpm_indication", "e2_node": "node-1", "connection_epoch": 7,
                "recv_unix_us": 1_700_000_000_000_000, "slot": 0,
                "measurements": [{"name": "RRC.ConnMean", "type": "int", "value": 2}],
            }) + "\n", encoding="utf-8")
            report = validate_kpm_jsonl(path, expected_epochs={"node-1": 7}, max_lines=2)

        self.assertEqual(report.state, "COMPATIBLE")
        self.assertEqual(report.sample_count, 1)
        self.assertEqual(report.invalid_records, 1)

    def test_missing_kpm_file_is_an_explicit_report_not_a_skipped_test(self):
        from assurance.collector.live import validate_kpm_jsonl

        report = validate_kpm_jsonl("/definitely/not/a/kpm.jsonl", expected_epochs={})

        self.assertFalse(report.exists)
        self.assertEqual(report.state, "MISSING")


class ReceiptTests(unittest.TestCase):
    def test_receipt_round_trip_is_readable_stale_aware_and_not_ota_evidence(self):
        from tools.labctl.receipt import Probe, create_receipt, read_receipt

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            receipt = create_receipt(
                inventory={"profileId": "pc1", "components": ["5GC", "gNB1", "gNB2", "UE1", "UE2"]},
                probes=(Probe("5GC", lambda: (True, "ready")), Probe("gNB1", lambda: (False, "not ready"))),
                configuration_identity={"bindingId": "oran-lab-live-20260819"},
                started_at="2026-08-21T09:00:00.000000Z", output_path=output,
                now="2026-08-21T09:00:10.000000Z",
            )
            view = read_receipt(output, now="2026-08-21T09:01:00.000000Z", maximum_age_ms=120_000)

        self.assertEqual(receipt["readiness"]["state"], "NOT_READY")
        self.assertFalse(view.stale)
        self.assertFalse(view.counts_as_candidate)
        self.assertFalse(view.counts_as_ota_evidence)

    def test_labctl_receipt_command_writes_a_probe_only_receipt(self):
        from tools.labctl.cli import main
        from tools.labctl.receipt import Probe

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.json"
            inventory.write_text(json.dumps({
                "schemaVersion": "oran-aic-labctl-inventory/1.0.0", "profileId": "pc1",
                "objective": "PIN_TO_CELL", "stateRoot": str(root / "runs"),
                "hosts": {"pc1": {"transport": "local"}},
                "components": [{"id": "gNB1", "host": "pc1", "stage": 0, "status": {"argv": ["true"]}}],
            }), encoding="utf-8")
            output = root / "receipt.json"
            status = main(["--inventory", str(inventory), "receipt", "--output", str(output)], probes=(Probe("gNB1", lambda: (True, "mock ready")),))

            body = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(status, 0)
        self.assertEqual(body["readiness"]["state"], "READY")
        self.assertFalse(body["boundary"]["cockpitMayStartLabSetup"])
