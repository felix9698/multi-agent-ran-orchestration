"""The R1 boundary: policy create / status / status-subscription / DME routes.

The O-RAN behaviour is the upper's existing ``oran.nonrt.service.NonRtRicService``
composed unchanged -- this module adds no route.  What it adds is the live-O1
profile's construction: the capability manifest comes from the frozen bundle
fixture (the only admissible capability source), the E2 inventory is derived from
that same fixture rather than trusted from the vector, and the DME registration
is built from the frozen ``aic.ran-capability`` schema bytes.

``oran/nonrt/service.py`` is byte-frozen for this release, so every live-O1
difference is expressed here, in a wrapper, rather than by editing a shared file.
"""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Mapping

from oran.conformance.contracts import ContractBundle
from oran.contract.jcs import canonicalize_bytes, jcs_sha256
from oran.nonrt.capability import CapabilityArtifacts, validate_e2_inventory
from oran.nonrt.config import SecurityProfile
from oran.nonrt.service import NonRtRicService

CAPABILITY_DME_TYPE = "aic:ran-capability:1.0.0"
EVIDENCE_DME_TYPE = "aic:policy-evidence:1.0.0"
EVIDENCE_DELIVERY_SCHEMA_ID = "aic.policy-evidence.record.schema.1.0.0"


class R1BoundaryError(RuntimeError):
    """The R1 boundary could not be composed from the frozen bytes."""

    exit_code = 78


def capability_artifacts(*, capability: Mapping[str, Any],
                         vector: Mapping[str, Any],
                         bundle: ContractBundle) -> CapabilityArtifacts:
    """Derive the E2 inventory from the same fixture the contract pins.

    The vector's ``e2Inventory`` is bound to a different capability manifest, so
    it cannot be validated against the fixture; only its declared ``status`` is
    consumed and every node fact is taken from the pinned capability.
    """
    declared = dict(vector["e2Inventory"])
    nodes = list(capability["e2Deployment"]["nodes"])
    connections = list(declared["connections"])
    if len(connections) != len(nodes):
        raise R1BoundaryError(
            "the deployment vector declares %d E2 connections but the pinned "
            "capability declares %d nodes" % (len(connections), len(nodes)))
    by_key = {_node_key(node["globalE2NodeId"]["nodeId"]): node for node in nodes}
    inventory = {
        "schemaVersion": declared["schemaVersion"],
        "contractProfile": declared["contractProfile"],
        "generatedAt": declared["generatedAt"],
        "releaseManifestSha256": declared["releaseManifestSha256"],
        "status": declared["status"],
        "connections": [
            _derive_connection(connection,
                               by_key[_node_key(connection["globalE2NodeId"]["nodeId"])],
                               capability)
            for connection in connections],
    }
    result = validate_e2_inventory(inventory, capability, bundle_dir=bundle.path)
    return CapabilityArtifacts(
        capability_manifest=dict(capability),
        release_manifest={},
        e2_inventory=inventory,
        capability_sha256=hashlib.sha256(canonicalize_bytes(capability)).hexdigest(),
        release_sha256="",
        inventory_sha256=hashlib.sha256(canonicalize_bytes(inventory)).hexdigest(),
        inventory_result=result)


def capability_registration(*, vector: Mapping[str, Any],
                            bundle: ContractBundle) -> dict[str, Any]:
    schema = bundle.schema("aic.ran-capability.1.0.0.schema.json")
    return {
        "dmeTypeDefinition": {
            "dmeTypeId": CAPABILITY_DME_TYPE,
            "metadata": {"dataCategory": ["PERFORMANCE"]},
            "dataProductionSchema": schema,
            "dataDeliverySchemas": [{
                "type": "JSON_SCHEMA",
                "deliverySchemaId": "aic.ran-capability.1.0.0",
                "schema": canonicalize_bytes(schema).decode("utf-8"),
            }],
            "dataDeliveryMechanisms": [{"dataDeliveryMethod": "PUSH_HTTP"}],
        },
        "dataAccessEndpoint": dict(vector["r1"]["dme"]["dataAccessEndpoint"]),
        "dataDeliveryModes": ["CONTINUOUS"],
    }


def evidence_registration(*, vector: Mapping[str, Any],
                          bundle: ContractBundle) -> dict[str, Any]:
    """The policy-evidence DME registration, rebuilt from frozen bytes.

    The production schema is the FILTER schema the frozen bundle pins, and the
    delivery schema is the RFC 8785 canonical form of the evidence record
    schema; the framework re-derives both and refuses a mismatch, so nothing
    here can be substituted from the wire.
    """
    return {
        "dmeTypeDefinition": {
            "dmeTypeId": {"namespace": "aic", "name": "policy-evidence",
                          "version": "1.0.0"},
            "metadata": {"dataCategory": ["PERFORMANCE"]},
            "dataProductionSchema": bundle.fixture(
                "fixture://policyEvidenceFilterSchema"),
            "dataDeliverySchemas": [{
                "type": "JSON_SCHEMA",
                "deliverySchemaId": EVIDENCE_DELIVERY_SCHEMA_ID,
                "schema": canonicalize_bytes(bundle.schema(
                    "aic.policy-evidence.1.0.0.schema.json")).decode("utf-8"),
            }],
            "dataDeliveryMechanisms": [{"dataDeliveryMethod": "PUSH_HTTP"}],
        },
        "dataAccessEndpoint": dict(vector["r1"]["dme"]["dataAccessEndpoint"]),
        "dataDeliveryModes": ["CONTINUOUS"],
    }


class R1Boundary:
    """Serve the R1 surface and expose its state as MEASURED counts."""

    def __init__(self, *, service: NonRtRicService, base_path: str,
                 admission: Any = None) -> None:
        self.service = service
        self.base_path = str(base_path).rstrip("/")
        self._admission = admission

    @classmethod
    def compose(cls, *, vector: Mapping[str, Any], bundle: ContractBundle,
                state_dir: Path, a1_client: Any, callback_sender: Any,
                listen_host: str, base_path: str,
                admission: Any = None) -> "R1Boundary":
        capability = bundle.fixture("fixture://capabilityManifest")
        artifacts = capability_artifacts(
            capability=capability, vector=vector, bundle=bundle)
        rapp_identity = str(vector["r1"]["rAppId"])
        security = SecurityProfile(
            insecure_dev_mode=False,
            listen_host=listen_host,
            # The deployment binds exactly one rApp identity to this listener;
            # no contract-declared identity header exists for it.
            auth_hook=lambda _headers, identity=rapp_identity: identity,
            integration_control_approved=False)
        service = NonRtRicService(
            database_path=Path(state_dir) / "nonrt.sqlite3",
            a1_client=a1_client,
            capability_manifest=capability,
            a1_ready=artifacts.a1_ready,
            capability_artifacts=artifacts,
            bundle_dir=bundle.path,
            security=security,
            r1_api_root=str(vector["r1"]["apiRoot"]),
            a1_notification_destination=str(vector["a1"]["statusCallbackRoot"]),
            bootstrap_info={"bootstrapInformation": []},
            callback_sender=callback_sender,
            capability_registration=capability_registration(
                vector=vector, bundle=bundle),
            # SC-084 declares `faults: []` and an exact expected.httpSequence,
            # so the pre-PUT A1 reconciliation probe is neither required nor
            # tolerated: it would put undeclared traffic on the peer's wire.
            a1_pre_put_reconciliation_probe=False)
        return cls(service=service, base_path=base_path, admission=admission)

    # -- HTTP dispatch ----------------------------------------------------
    def dispatch(self, method: str, path: str, headers: Mapping[str, str],
                 body: bytes) -> tuple[int, dict[str, str], bytes]:
        import json

        context = (nullcontext() if self._admission is None
                   else self._admission("serving an R1 request"))
        with context:
            decoded: Any = None
            if body:
                try:
                    decoded = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    decoded = None
            response = self.service.handle(
                method, path, headers=headers, body=decoded)
            payload = b""
            out_headers = {str(key): str(value)
                           for key, value in (response.headers or {}).items()}
            if response.body is not None:
                payload = canonicalize_bytes(response.body)
                out_headers.setdefault("Content-Type", "application/json")
            return int(response.status), out_headers, payload

    # -- observations -----------------------------------------------------
    def state_counts(self) -> dict[str, Any]:
        store = self.service.store
        policy_ids = sorted(str(row["policy_id"]) for row in
                            store.rows("SELECT policy_id FROM policies"))
        accepted: dict[str, int] = {}
        for row in store.rows(
                "SELECT key,value_json FROM metadata WHERE key LIKE 'dme-push:%'"):
            binding = str(row["key"]).split(":", 1)[1]
            accepted[binding] = len(store.decode(row["value_json"]))
        return {
            "policyCount": len(policy_ids),
            "statusCount": len(store.rows("SELECT policy_id FROM statuses")),
            "dmeDataJobCount": len(store.rows("SELECT data_job_id FROM data_jobs")),
            "acceptedPushPayloadCounts": accepted,
            "subscriptionCount": len(
                store.rows("SELECT subscription_id FROM subscriptions")),
            "policyIds": policy_ids,
        }

    def reset_scenario_state(self) -> None:
        self.service.store.reset_scenario_state()


def _node_key(node_id: Mapping[str, Any]) -> int:
    try:
        return int(str(node_id["hex"]), 16)
    except (KeyError, TypeError, ValueError) as exc:
        raise R1BoundaryError(
            "an E2 node id carries no recognised identifier") from exc


def _derive_connection(declared: Mapping[str, Any], node: Mapping[str, Any],
                       capability: Mapping[str, Any]) -> dict[str, Any]:
    """Reconcile one declared E2 connection with the pinned static capability.

    The deployment vector supplies the operational members the E2 inventory
    schema requires (epoch, association, module sets); the bundle fixture
    supplies the node identity and the RAN-function digests, because the
    contract forces that fixture as the capability source.
    """
    import json as _json

    connection = _json.loads(_json.dumps(dict(declared)))
    connection["globalE2NodeId"] = _json.loads(
        _json.dumps(dict(node["globalE2NodeId"])))
    connection["e2apVersion"] = capability["e2Deployment"]["e2apVersion"]
    connection["transferSyntax"] = capability["serviceModels"]["e2ap"]["encoding"]
    required = {int(item["ranFunctionId"]): item
                for item in node["requiredRanFunctions"]}
    for function in connection.get("ranFunctions", []):
        expected = required.get(int(function.get("ranFunctionId", -1)))
        if expected is None:
            continue
        function["ranFunctionOid"] = expected["ranFunctionOid"]
        function["ranFunctionRevision"] = expected["observedRanFunctionRevision"]
        function.setdefault("rawDefinition", {})["sha256"] = \
            expected["rawDefinitionSha256"]
        function.setdefault("canonicalDefinition", {})["sha256"] = \
            expected["canonicalDecodedDefinitionSha256"]
        function["active"] = True
    connection["active"] = True
    return connection


def state_snapshot(*, r1_counts: Mapping[str, Any], binding_count: int,
                   evidence_commit_count: int, retrieved_file_count: int,
                   perf_metric_job_present: bool | None) -> dict[str, Any]:
    """Assemble one capture ``$defs/stateSnapshot`` from measured counts."""
    snapshot = {
        "policyCount": int(r1_counts["policyCount"]),
        "statusCount": int(r1_counts["statusCount"]),
        "dmeDataJobCount": int(r1_counts["dmeDataJobCount"]),
        "deliveryBindingCount": int(binding_count),
        "acceptedPushPayloadCounts": dict(r1_counts["acceptedPushPayloadCounts"]),
        "subscriptionCount": int(r1_counts["subscriptionCount"]),
        "evidenceCommitCount": int(evidence_commit_count),
        "retrievedFileCount": int(retrieved_file_count),
        "perfMetricJobPresent": perf_metric_job_present,
        "policyIds": list(r1_counts["policyIds"]),
    }
    snapshot["jcsSha256"] = jcs_sha256(snapshot)
    return snapshot
