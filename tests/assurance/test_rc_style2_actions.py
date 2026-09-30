"""Hardware-free contracts for the redirected Style 2 UE-action work."""

from __future__ import annotations

import unittest
from pathlib import Path

from assurance.actions.catalog import ActionParameterError
from assurance.contracts.capability import ActuatorPath, DeploymentBinding, TransportSecurity
from assurance.contracts.rc_style2_actions import (
    rc_style2_actions,
    validate_rc_style2_parameters,
)
from assurance.gateway.gateway import TokenBoundWriteGateway
from assurance.gateway.mock_adapter import FaultInjection, MockActuationAdapter
from assurance.gateway.plan import config_hash
from assurance.gateway.token import KernelToken, TokenKind
from assurance.gateway.write_gateway import GatewayOutcome
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION


def deployment() -> DeploymentBinding:
    return DeploymentBinding(
        contract_id="deployment/rc-style2-actions",
        version="1.0.0",
        schema_version=ASSURANCE_SCHEMA_VERSION,
        document_status="NORMATIVE",
        standard_mapping={"e2sm-rc": "1.03"},
        endpoint_id="near-rt-ric",
        base_url="https://near-rt.invalid",
        transport_security=TransportSecurity.MTLS,
        secret_refs={"clientCertificate": "file:/run/secrets/rc.crt"},
    )


def permit(kind: TokenKind, expected: str, sequence: int) -> KernelToken:
    return KernelToken(
        token_kind=kind, transaction_id="tx-style2", trial_id="trial-style2",
        fencing_token=1, command_sequence=sequence,
        lease_expiry="2026-08-22T10:00:00.000000Z", expected_config_hash=expected,
        idempotency_key=f"tx-style2:{kind.value}:{sequence}",
        issued_at="2026-08-22T09:00:00.000000Z",
    )


class RcStyle2ActionContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.actions = {item.key: item for item in rc_style2_actions(deployment())}

    def test_the_three_bindings_are_official_and_readback_named_by_catalog(self):
        self.assertEqual(set(self.actions), {"ue-dl-prb-cap", "scheduler-priority", "dl-mcs-bounds"})
        for action in self.actions.values():
            self.assertEqual(action.binding.path, ActuatorPath.OFFICIAL_ORAN_DYNAMIC)
        self.assertEqual(self.actions["ue-dl-prb-cap"].source.counter.deployment_counter_name, "RAN.UE.DlPrbCap")
        self.assertEqual(self.actions["scheduler-priority"].source.counter.deployment_counter_name, "RAN.UE.PfWeight")
        self.assertEqual(self.actions["dl-mcs-bounds"].source.counter.deployment_counter_name, "RAN.Cell.DlMcsBounds")
        self.assertEqual(self.actions["ue-dl-prb-cap"].scope, "UE")
        self.assertEqual(self.actions["scheduler-priority"].scope, "UE")
        self.assertEqual(self.actions["dl-mcs-bounds"].scope, "NRCellDU")
        self.assertTrue(all("PATCH_TEXT_PROPOSED" in item.disposition for item in self.actions.values()))

    def test_catalog_remains_the_sole_live_capability_authority(self):
        for action in self.actions.values():
            with self.subTest(action=action.key):
                self.assertEqual(action.binding.live_capable, action.source.binding.live_capable)
                self.assertEqual(action.binding.live_backend, action.source.binding.live_backend)
                self.assertEqual(
                    action.binding.live_blocking_premise,
                    action.source.binding.live_blocking_premise,
                )
                self.assertIn("NOT_REBUILT_INTO_DEPLOYED_BINARY", action.premises)
                self.assertIn("KERNEL_GATEWAY_WIRING_PENDING", action.premises)
                self.assertIn("NO_OTA_EVIDENCE", action.premises)

    def test_patch_exists_and_ids_are_labeled_deployment_local(self):
        repo_root = Path(__file__).resolve().parents[2]
        expected = {
            "dl-mcs-bounds": (101, (201, 202, 203)),
            "ue-dl-prb-cap": (102, (211, 212)),
            "scheduler-priority": (103, (221, 222)),
        }
        for key, (action_id, parameter_ids) in expected.items():
            action = self.actions[key]
            with self.subTest(action=key):
                self.assertTrue((repo_root / action.patch_text).is_file())
                self.assertEqual(action.deployment_local_action_id, action_id)
                self.assertEqual(action.deployment_local_parameter_ids, parameter_ids)
                self.assertEqual(
                    action.binding.service_model["actionIdDisposition"],
                    "definition-dependent/deployment-local",
                )
                self.assertEqual(action.binding.service_model["deploymentLocalActionId"], str(action_id))
                self.assertEqual(
                    action.binding.service_model["deploymentLocalParameterIds"],
                    ",".join(str(item) for item in parameter_ids),
                )

    def test_catalog_rejects_bad_values_before_gateway_writes(self):
        invalid = {
            "ue-dl-prb-cap": {"rnti": 0x4601, "maxDlPrbs": 276},
            "scheduler-priority": {"rnti": 0x4601, "pfWeight": 0},
            "dl-mcs-bounds": {"maxDlMcs": 4, "minDlMcs": 5},
        }
        for key, values in invalid.items():
            with self.subTest(action=key):
                with self.assertRaises(ActionParameterError):
                    validate_rc_style2_parameters(self.actions[key], values)

    def test_each_action_round_trips_and_faults_through_the_sanctioned_mock(self):
        cases = {
            "ue-dl-prb-cap": ({"rnti": 0x4601, "maxDlPrbs": 0}, {"rnti": 0x4601, "maxDlPrbs": 12}, {"ueAnchorRef": "ue-1"}),
            "scheduler-priority": ({"rnti": 0x4601, "pfWeight": 1.0}, {"rnti": 0x4601, "pfWeight": 2.0}, {"ueAnchorRef": "ue-1"}),
            "dl-mcs-bounds": ({"maxDlMcs": 28, "minDlMcs": 0}, {"maxDlMcs": 10, "minDlMcs": 4}, {"nrCellDuId": "cell-1"}),
        }
        for key, (before, after, scope) in cases.items():
            with self.subTest(action=key):
                validate_rc_style2_parameters(self.actions[key], after)
                baseline, applied = {key: before}, {key: after}
                plan = {"adapter": "official", "scope": scope, "baselineConfig": baseline,
                        "steps": [{"axis": key, "value": after}]}
                base_hash, applied_hash = config_hash(baseline), config_hash(applied)
                adapter = MockActuationAdapter(config=baseline)
                gateway = TokenBoundWriteGateway(adapters={"official": adapter}, safe_state=baseline,
                                                 clock=lambda: "2026-08-22T09:00:00.000000Z")
                self.assertEqual(gateway.prepare(token=permit(TokenKind.PREPARE, base_hash, 0), plan=plan).outcome, GatewayOutcome.ACKED)
                self.assertEqual(gateway.ready(token=permit(TokenKind.READY, base_hash, 1)).outcome, GatewayOutcome.ACKED)
                self.assertEqual(gateway.commit(token=permit(TokenKind.COMMIT, base_hash, 2)).outcome, GatewayOutcome.ACKED)
                self.assertEqual(adapter.snapshot(), applied)
                self.assertEqual(gateway.stop(token=permit(TokenKind.STOP, applied_hash, 3)).outcome, GatewayOutcome.ACKED)
                self.assertEqual(gateway.reverse_rollback(token=permit(TokenKind.REVERSE_ROLLBACK, applied_hash, 4)).outcome, GatewayOutcome.ACKED)
                self.assertEqual(adapter.snapshot(), baseline)

                faulted = MockActuationAdapter(config=baseline, faults=FaultInjection(fail_axes=frozenset({key})))
                faulty = TokenBoundWriteGateway(adapters={"official": faulted}, safe_state=baseline,
                                                clock=lambda: "2026-08-22T09:00:00.000000Z")
                self.assertEqual(faulty.prepare(token=permit(TokenKind.PREPARE, base_hash, 0), plan=plan).outcome, GatewayOutcome.ACKED)
                self.assertEqual(faulty.ready(token=permit(TokenKind.READY, base_hash, 1)).outcome, GatewayOutcome.ACKED)
                self.assertEqual(faulty.commit(token=permit(TokenKind.COMMIT, base_hash, 2)).outcome, GatewayOutcome.REJECTED)
                self.assertEqual(faulted.snapshot(), baseline)
