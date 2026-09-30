"""Read-only, secret-free binding of Assurance to one deployed O-RAN lab."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Tuple

from assurance.contracts.capability import DeploymentBinding, TransportSecurity
from assurance.contracts.validation import assert_secret_free

__all__ = ["AssuranceLiveBinding", "LiveBindingError", "LiveR1Binding", "load_assurance_live_binding"]


class LiveBindingError(ValueError):
    """The committed binding is malformed or no longer matches its sources."""


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise LiveBindingError(f"{field} must be an object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise LiveBindingError(f"{field} must be a non-empty string")
    return value


def _endpoint(*, endpoint_id: str, api_root: str,
              secret_refs: Mapping[str, Any] | None = None) -> DeploymentBinding:
    secret_refs = secret_refs or {}
    refs = {
        str(key): _text(value, f"{endpoint_id}.secretRefs.{key}")
        for key, value in secret_refs.items()
    }
    binding = DeploymentBinding(
        contract_id=f"deployment/{endpoint_id}", version="1.0.0", schema_version="assurance/1.0.0",
        document_status="NORMATIVE", standard_mapping={"O-RAN": endpoint_id}, endpoint_id=endpoint_id,
        base_url=api_root, transport_security=TransportSecurity.MTLS_AND_OAUTH2 if refs else TransportSecurity.MTLS,
        secret_refs=refs, trust_anchor_ref=refs.get("mtlsCa"),
    )
    try:
        assert_secret_free(binding)
    except Exception as exc:
        raise LiveBindingError("binding is not secret-free") from exc
    return binding


@dataclass(frozen=True)
class LiveR1Binding:
    api_root: str
    near_rt_ric_id: str
    policy_type_id: str
    cadence_ms: int
    deadline_ms: int
    deployment: DeploymentBinding


@dataclass(frozen=True)
class AssuranceLiveBinding:
    binding_id: str
    r1: LiveR1Binding
    a1p: DeploymentBinding
    o1: DeploymentBinding
    o1_netconf: str
    o1_sftp: str
    pm_directory: str
    kpm_jsonl_path: str
    kpm_expected_epochs: Mapping[str, int]
    e2_nodes: Tuple[str, ...]
    cells: Tuple[int, ...]
    plmn: Mapping[str, str]
    source_digests: Mapping[str, str]


def _verify_sources(entries: object) -> Mapping[str, str]:
    if not isinstance(entries, list) or not entries:
        raise LiveBindingError("sources must be a non-empty array")
    verified = {}
    for entry in entries:
        body = _object(entry, "source")
        source = Path(_text(body.get("path"), "source.path"))
        expected = _text(body.get("sha256"), "source.sha256")
        try:
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
        except OSError as exc:
            raise LiveBindingError(f"cannot read identity source {source}") from exc
        if actual != expected:
            raise LiveBindingError(f"identity source digest mismatch: {source}")
        verified[str(source)] = actual
    return verified


def load_assurance_live_binding(path: str | Path) -> AssuranceLiveBinding:
    """Load a committed binding only after its immutable source digests match.

    This function reads files and creates data objects only.  It has no HTTP,
    NETCONF, SFTP, E2, or secret-resolution path.
    """
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveBindingError("cannot read live binding") from exc
    document = _object(document, "binding")
    if document.get("schemaVersion") != "assurance-live-binding/1.0.0":
        raise LiveBindingError("unsupported live binding schema")
    sources = _verify_sources(document.get("sources"))
    r1 = _object(document.get("r1"), "r1")
    polling = _object(r1.get("polling"), "r1.polling")
    cadence_ms, deadline_ms = polling.get("cadenceMs"), polling.get("deadlineMs")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (cadence_ms, deadline_ms)):
        raise LiveBindingError("r1 polling requires positive integer cadence and deadline")
    r1_root = _text(r1.get("apiRoot"), "r1.apiRoot")
    r1_binding = LiveR1Binding(
        api_root=r1_root, near_rt_ric_id=_text(r1.get("nearRtRicId"), "r1.nearRtRicId"),
        policy_type_id=_text(r1.get("policyTypeId"), "r1.policyTypeId"), cadence_ms=cadence_ms,
        deadline_ms=deadline_ms, deployment=_endpoint(
            endpoint_id="r1", api_root=r1_root,
            secret_refs=_object(r1.get("secretRefs"), "r1.secretRefs"),
        ),
    )
    a1p = _object(document.get("a1p"), "a1p")
    o1 = _object(document.get("o1"), "o1")
    kpm = _object(document.get("kpm"), "kpm")
    expected_epochs = _object(kpm.get("expectedEpochs"), "kpm.expectedEpochs")
    if any(not isinstance(node, str) or isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0 for node, epoch in expected_epochs.items()):
        raise LiveBindingError("kpm expected epochs must map nodes to non-negative integers")
    nodes = document.get("e2Nodes")
    cells = document.get("cells")
    plmn = _object(document.get("plmn"), "plmn")
    if not isinstance(nodes, list) or not all(isinstance(node, str) and node.startswith("0x") for node in nodes):
        raise LiveBindingError("e2Nodes must be hexadecimal identities")
    if not isinstance(cells, list) or not all(isinstance(cell, int) and not isinstance(cell, bool) for cell in cells):
        raise LiveBindingError("cells must be integer identities")
    return AssuranceLiveBinding(
        binding_id=_text(document.get("bindingId"), "bindingId"), r1=r1_binding,
        a1p=_endpoint(
            endpoint_id="a1p", api_root=_text(a1p.get("apiRoot"), "a1p.apiRoot"),
            secret_refs=_object(a1p.get("secretRefs"), "a1p.secretRefs"),
        ),
        o1=_endpoint(
            endpoint_id="o1", api_root=_text(o1.get("httpsRoot"), "o1.httpsRoot"),
            secret_refs=_object(o1.get("secretRefs"), "o1.secretRefs"),
        ),
        o1_netconf=_text(o1.get("netconf"), "o1.netconf"), o1_sftp=_text(o1.get("sftp"), "o1.sftp"),
        pm_directory=_text(o1.get("pmDirectory"), "o1.pmDirectory"),
        kpm_jsonl_path=_text(kpm.get("jsonlPath"), "kpm.jsonlPath"),
        kpm_expected_epochs=dict(expected_epochs), e2_nodes=tuple(nodes), cells=tuple(cells),
        plmn={"mcc": _text(plmn.get("mcc"), "plmn.mcc"), "mnc": _text(plmn.get("mnc"), "plmn.mnc")},
        source_digests=sources,
    )
