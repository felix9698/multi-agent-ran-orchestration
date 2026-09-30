"""Shared hermetic fixtures for the xApp coordination layer tests.

Not a test module (repo convention: non-``test*.py`` files beside the tests
are shared harnesses).  Everything here is in-memory: no gNB, no telnet, no
FlexRIC, no network.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from assurance.advisors.action_space import AdvisoryAction
from assurance.collector.samples import ClockHealth, RawSample
from assurance.contracts.capability import DeploymentBinding, TransportSecurity
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.gateway.token import KernelToken, TokenKind
from assurance.xapps import (
    CommonKpiSnapshot, XAppRuntimeStatus, build_default_xapp_coordination,
    snapshot_from_samples,
)

NOW = "2026-08-31T10:00:01.000000Z"
TAKEN = "2026-08-31T10:00:00.000000Z"
LATER = "2026-08-31T10:10:00.000000Z"
TRACE_HASH = "a" * 64
CONFIG_HASH = "b" * 64

SOURCE_CELL = "12345678"
TARGET_CELL = "87654321"
TARGET_UE = "ue-target"
HEAVY_UE = "ue-heavy"
TARGET_RNTI = 0x2222
HEAVY_RNTI = 0x1111


def deployment() -> DeploymentBinding:
    return DeploymentBinding(
        contract_id="deployment/xapp-coordination-hf", version="1.0.0",
        schema_version="1.0.0", document_status="NORMATIVE",
        standard_mapping={"test": "hardware-free"},
        endpoint_id="mock-write-gateway", base_url="https://127.0.0.1:9443",
        transport_security=TransportSecurity.MTLS,
        secret_refs={"clientCert": "env:HF_CLIENT_CERT"})


def raw_sample(sample_id: str, counter_id: str, value: float, unit: str,
               scope: Mapping[str, str], *, observed_at: str = TAKEN,
               sequence: int = 0) -> RawSample:
    return RawSample(
        sample_id=sample_id, counter_id=counter_id,
        value=TypedQuantity(value, unit, Provenance.MEASURED,
                            f"raw/{sample_id}"),
        scope_snapshot=scope, observed_at=observed_at, cadence_ms=1000,
        clock_health=ClockHealth.SYNCHRONISED, trace_hash=TRACE_HASH,
        sequence=sequence)


def attribution_snapshot(*, snapshot_id: str = "snap/attribution-1",
                         taken_at: str = TAKEN,
                         target_cell_of_target_ue: str = SOURCE_CELL,
                         target_rnti: int = TARGET_RNTI,
                         extra_samples: Sequence[RawSample] = ()) \
        -> CommonKpiSnapshot:
    """Both UEs attributed on the source cell, plus any extra samples."""
    samples = [
        raw_sample("s-target", "UE.ServingCell",
                   int(target_cell_of_target_ue), "NCI",
                   {"ueId": TARGET_UE, "cellId": target_cell_of_target_ue,
                    "rnti": f"{target_rnti:#06x}"}),
        raw_sample("s-heavy", "UE.ServingCell", int(SOURCE_CELL), "NCI",
                   {"ueId": HEAVY_UE, "cellId": SOURCE_CELL,
                    "rnti": f"{HEAVY_RNTI:#06x}"}, sequence=1),
    ]
    samples.extend(extra_samples)
    return snapshot_from_samples(snapshot_id=snapshot_id, taken_at=taken_at,
                                 samples=samples)


def steer_action(target_cell: str = TARGET_CELL) -> AdvisoryAction:
    return AdvisoryAction("cell-steering",
                          {"targetPrimaryCellId": int(target_cell)},
                          {"objectiveUeId": TARGET_UE})


def priority_action(*, serving_cell: Optional[str] = SOURCE_CELL,
                    rnti: int = TARGET_RNTI,
                    pf_weight: float = 2.0) -> AdvisoryAction:
    selector = {
        "objectiveUeId": TARGET_UE,
        "controlledUeRole": "TARGET_UE",
        "rntiBinding": "RESOLVE_AFTER_PRIMARY_READBACK",
    }
    if serving_cell is not None:
        selector["servingCellId"] = serving_cell
    return AdvisoryAction("scheduler-priority",
                          {"rnti": rnti, "pfWeight": pf_weight}, selector)


def cap_action(*, rnti: int = HEAVY_RNTI, max_dl_prbs: int = 20) -> AdvisoryAction:
    return AdvisoryAction(
        "ue-dl-prb-cap", {"rnti": rnti, "maxDlPrbs": max_dl_prbs},
        {"objectiveUeId": TARGET_UE, "controlledUeRole": "NON_TARGET_HEAVY_UE",
         "rntiBinding": "RESOLVE_AFTER_PRIMARY_READBACK",
         "sliceRelation": "OUTSIDE_OBJECTIVE_SLICE"})


def power_action(cell: str = TARGET_CELL,
                 attenuation_db: float = 6.0) -> AdvisoryAction:
    return AdvisoryAction("dl-rf-attenuation",
                          {"txAttenuationDb": attenuation_db},
                          {"cellId": cell})


def live_status(xapp_id: str, *, heartbeat_at: str = TAKEN,
                e2_connected: bool = True, deployed: bool = True,
                healthy: bool = True, paused: bool = False) -> XAppRuntimeStatus:
    return XAppRuntimeStatus(
        xapp_id=xapp_id, deployed=deployed, healthy=healthy,
        e2_connected=e2_connected, service_model_available=True,
        last_heartbeat_at=heartbeat_at, paused=paused)


def coordination_runtime(*, wire_live: Sequence[str] = (
        "xapp/traffic-steering", "xapp/ue-scheduler", "xapp/cell-power")):
    """The default coordination runtime with hardware-free live statuses.

    Marking the three coordinated xApps live here is a hermetic test
    convenience, not a deployment claim: the manifests still record the real
    path states (external source / hardware-free only).
    """
    runtime = build_default_xapp_coordination(deployment())
    for xapp_id in wire_live:
        runtime.registry.update_runtime_status(live_status(xapp_id))
    return runtime


def permit(*, kind: TokenKind = TokenKind.COMMIT,
           issued_at: str = TAKEN, lease_expiry: str = LATER,
           sequence: int = 0) -> KernelToken:
    return KernelToken(
        token_kind=kind, transaction_id="txn/xapp-hf-1",
        trial_id="case/xapp-hf:trial:1", fencing_token=1,
        command_sequence=sequence, lease_expiry=lease_expiry,
        expected_config_hash=CONFIG_HASH, idempotency_key="idem/xapp-hf-1",
        issued_at=issued_at)
