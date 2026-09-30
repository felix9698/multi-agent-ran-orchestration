"""The self-test deployment vector, built at run time from frozen bytes.

``G-EP-1`` bars a routable endpoint literal from every packaged file *except*
``oran/release/lo1_selftest/**`` and ``tests/lo1/**``.  This module is that
declared exception and it uses the exception for one purpose only: to bind the
self-test's own loopback listeners.  Every other value is either read from the
frozen bundle (``vectorVersion``, ``expectedPolicyCellCount``,
``notificationWindowMs``, the two ``fileInfo`` consts, the recovery-file
cardinality, the policy-evidence schema and its digests) or supplied by the
Provider emulator at run time (the three Provider authorities).

Nothing here is packaged as a default for a live run: the shipped
``deployment-vector.1.0.0.template.json`` carries ``${LO1:...}`` placeholders
and this builder is never on the live path.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import random
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from oran.contract.jcs import canonicalize, jcs_sha256

from .frozen import FrozenBundle, json_pointer

LOOPBACK = "127.0.0.1"

#: ``secretRef`` names the self-test uses.  The frozen vector schema's
#: ``secretRef`` pattern admits ``secret|vault|k8s-secret`` only, so these are
#: references and never paths; the resolver maps them to run-scoped files.
SECRET_NETCONF_KNOWN_HOSTS = "secret://lo1-selftest/o1/netconf/known-hosts"
SECRET_NETCONF_CREDENTIAL = "secret://lo1-selftest/o1/netconf/credential"
SECRET_SFTP_KNOWN_HOSTS = "secret://lo1-selftest/o1/sftp/known-hosts"
SECRET_SFTP_CREDENTIAL = "secret://lo1-selftest/o1/sftp/credential"
SECRET_TRUSTSTORE = "secret://lo1-selftest/security/truststore"
SECRET_R1_CLIENT = "secret://lo1-selftest/security/r1-client-credential"
SECRET_O1_NOTIFICATION = "secret://lo1-selftest/security/o1-notification-credential"


class VectorError(RuntimeError):
    """The self-test vector could not be derived from the frozen bytes."""


@dataclass(frozen=True)
class UpperListeners:
    """The four origins the role table marks ``UPPER_HARNESS``, plus lower A1."""

    r1: str
    rapp: str
    o1_consumer: str
    a1_status: str
    lower_a1: str
    dme_push: str

    @classmethod
    def loopback(cls, ports: Mapping[str, int]) -> "UpperListeners":
        return cls(
            r1=f"{LOOPBACK}:{ports['r1']}",
            rapp=f"{LOOPBACK}:{ports['rapp']}",
            o1_consumer=f"{LOOPBACK}:{ports['o1_consumer']}",
            a1_status=f"{LOOPBACK}:{ports['a1_status']}",
            lower_a1=f"{LOOPBACK}:{ports['lower_a1']}",
            dme_push=f"{LOOPBACK}:{ports['rapp']}",
        )


def _instant(offset_seconds: int = 0) -> str:
    moment = _datetime.datetime.now(_datetime.timezone.utc).replace(microsecond=0)
    moment += _datetime.timedelta(seconds=offset_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _const(schema: Mapping[str, Any], pointer: str) -> Any:
    node = json_pointer(schema, pointer)
    if not isinstance(node, Mapping) or "const" not in node:
        raise VectorError(f"{pointer} is not a frozen const")
    return node["const"]


def _cardinality(schema: Mapping[str, Any], pointer: str) -> int:
    node = json_pointer(schema, pointer)
    minimum = node.get("minItems")
    maximum = node.get("maxItems")
    if minimum is None:
        raise VectorError(f"{pointer} declares no cardinality")
    if maximum is not None and maximum != minimum:
        raise VectorError(f"{pointer} cardinality is a range, not a pin")
    return int(minimum)


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bitstring(value: int, bits: int = 32) -> dict[str, Any]:
    digits = (bits + 3) // 4
    return {"hex": "0x" + format(value, "x").rjust(digits, "0"), "bitLength": bits}


def _kpm_capability() -> dict[str, Any]:
    return {
        "reportStyles": [
            {
                "styleType": 1,
                "actionDefinitionFormat": 1,
                "indicationHeaderFormat": 1,
                "indicationMessageFormat": 1,
                "measurements": [
                    {"measurementType": "NAME", "name": "RRU.PrbDl", "labels": []},
                ],
            },
            {
                "styleType": 4,
                "actionDefinitionFormat": 4,
                "indicationHeaderFormat": 1,
                "indicationMessageFormat": 3,
                "measurements": [
                    {"measurementType": "NAME", "name": "RRU.PrbTotDl",
                     "labels": ["UE_ID"]},
                ],
            },
        ]
    }


def _rc_capability() -> dict[str, Any]:
    return {
        "styleType": 3,
        "actionId": 1,
        "headerFormat": 1,
        "messageFormat": 1,
        "outcomeFormat": 1,
        "ranParameterTree": [
            {
                "id": 1,
                "name": "Target Primary Cell ID",
                "valueType": "STRUCTURE",
                "mandatory": True,
                "minOccurs": 1,
                "maxOccurs": 1,
                "children": [
                    {
                        "id": 2,
                        "name": "NR CGI",
                        "valueType": "ELEMENT",
                        "mandatory": True,
                        "minOccurs": 1,
                        "maxOccurs": 1,
                        "children": [],
                    },
                ],
            },
        ],
        "nrCgiEncodingProfileSha256": _digest_text("lo1-selftest/nr-cgi-encoding-profile"),
    }


def _ran_functions(node_label: str, observed_at: str) -> list[dict[str, Any]]:
    module_set = _digest_text("lo1-selftest/e2/module-set")
    functions = []
    for identifier, short_name, oid, revision, version, capability in (
        (2, "ORAN-E2SM-KPM", "1.3.6.1.4.1.53148.1.2.2.2", 2, "2.03", _kpm_capability()),
        (3, "ORAN-E2SM-RC", "1.3.6.1.4.1.53148.1.1.2.3", 1, "1.03", _rc_capability()),
    ):
        functions.append({
            "ranFunctionId": identifier,
            "ranFunctionRevision": revision,
            "ranFunctionOid": oid,
            "shortName": short_name,
            "serviceModelVersion": version,
            "rawDefinition": {
                "path": f"selftest-artifacts/e2/{node_label}/{identifier}/definition.aper",
                "sha256": _digest_text(f"raw/{node_label}/{identifier}"),
            },
            "decodedDefinition": {
                "path": f"selftest-artifacts/e2/{node_label}/{identifier}/definition.json",
                "sha256": _digest_text(f"decoded/{node_label}/{identifier}"),
            },
            "canonicalDefinition": {
                "algorithm": "RFC8785_JSON",
                "toolVersion": "lo1-selftest-inventory-builder/1.0.0",
                "sha256": _digest_text(f"canonical/{node_label}/{identifier}"),
            },
            "moduleSetSha256": module_set,
            "observedAt": observed_at,
            "active": True,
            "capability": capability,
        })
    return functions


def build_e2_inventory(*, observed_at: str) -> dict[str, Any]:
    """A minimal inventory that satisfies the frozen READY implications.

    SC-084 drives no E2 step -- the decision/control/readback boundary is the
    deterministic stub -- but the frozen vector schema still requires a
    schema-valid inventory, so one is derived rather than borrowed.
    """
    connections = []
    for index, node in enumerate((0x00000e00, 0x00000e01), start=1):
        connections.append({
            "globalE2NodeId": {
                "nodeType": "GNB",
                "plmn": {"mcc": "208", "mnc": "95", "mncDigitLength": 2},
                "nodeId": _bitstring(node),
            },
            "connectionEpoch": 1,
            "associationId": f"lo1-selftest-assoc-{index}",
            "acceptedSetupAt": observed_at,
            "e2apVersion": "2.03",
            "transferSyntax": "APER",
            "rawE2SetupPdu": {
                "path": f"selftest-artifacts/e2/gnb{index}/e2-setup.aper",
                "sha256": _digest_text(f"e2-setup/{index}"),
            },
            "decoderModuleSetSha256": _digest_text("lo1-selftest/e2/module-set"),
            "active": True,
            "ranFunctions": _ran_functions(f"gnb{index}", observed_at),
        })
    return {
        "schemaVersion": "oran-aic-e2-capability-inventory/1.0.0",
        "contractProfile": "oran-aic/1.0.0",
        "releaseManifestSha256": _digest_text("lo1-selftest/release-manifest"),
        "generatedAt": observed_at,
        "status": "READY",
        "connections": connections,
    }


def build_self_test_vector(
    bundle: FrozenBundle,
    *,
    upper: UpperListeners,
    provider_mns_root: str,
    provider_sftp_authority: str,
    provider_netconf_endpoint: str,
    mns_version: str = "v1",
    seed: int = 20260811,
) -> dict[str, Any]:
    # Trace identifiers are derived from the run seed rather than minted at
    # random, so two self-test runs with the same seed agree on every slot the
    # volatile allowlist does NOT excuse (G-DET-1).
    rng = random.Random(seed)

    def _uuid() -> str:
        return str(uuid.UUID(int=rng.getrandbits(128), version=4))

    schema = bundle.vector_schema
    observed_at = _instant()
    window_start = _instant(-120)
    window_end = _instant(-60)

    policy_evidence_schema = bundle.document("aic.policy-evidence.1.0.0.schema.json")
    filter_schema = bundle.document("aic.policy-evidence-filter.1.0.0.schema.json")

    expected_cells = _const(
        schema, "/properties/o1/properties/live/properties/expectedPolicyCellCount")
    ambiguous_count = _cardinality(
        schema,
        "/properties/o1/properties/live/properties/recoveryFiles"
        "/properties/ambiguousCandidates")
    cell_mapping_count = json_pointer(
        schema, "/properties/topology/properties/cellMappings")["minItems"]
    file_format = _const(schema, "/$defs/fileInfo/properties/fileFormat")
    file_data_type = _const(schema, "/$defs/fileInfo/properties/fileDataType")
    notification_window = _const(
        schema, "/properties/timeouts/properties/notificationWindowMs")
    live_capture_floor = bundle.live_capture_lower_bound_ms()
    job_id = str(bundle.pa_file_profile["perfMetricJob"]["jobId"])

    def file_info(name: str, *, unique: bool) -> dict[str, Any]:
        return {
            "fileLocation": f"sftp://{provider_sftp_authority}/pm/{name}",
            "fileSize": 1024 if unique else 2048,
            "fileReadyTime": window_end,
            "fileExpirationTime": _instant(86400),
            "fileCompression": "",
            "fileFormat": file_format,
            "fileDataType": file_data_type,
            "jobId": job_id,
        }

    cells = []
    for index in range(int(cell_mapping_count)):
        cells.append({
            "cellId": {
                "plmnId": {"mcc": "208", "mnc": "95"},
                "cId": {"ncI": 3584 + index},
            },
            "managedObjectDn": f"GNBDUFunction=oai-du,NRCellDU={index + 1}",
        })

    vector: dict[str, Any] = {
        "vectorVersion": _const(schema, "/properties/vectorVersion"),
        "r1": {
            "apiRoot": f"https://{upper.r1}/r1",
            "rAppId": "lo1-selftest-rapp",
            "publishesGeneralRequestResponseApi": False,
            "statusSubscriptionId": "lo1-selftest-status-subscription",
            "callbackApi": {
                "apiName": "lo1-selftest-callback",
                "aefId": "lo1-selftest-aef",
                "rootUri": f"https://{upper.rapp}/callbacks",
                "ipv4Addr": upper.rapp.split(":")[0],
                "port": int(upper.rapp.split(":")[1]),
                "resourceUri": "/callbacks/r1-status",
            },
            "dme": {
                "policyEvidencePushBaseUri": f"https://{upper.dme_push}/dme/push",
                "activeDataJobId": "lo1-selftest-data-job",
                "activeDeliveryBindingId": _uuid().replace("-", "") + f"{rng.getrandbits(24):06x}",
                "dataAccessEndpoint": {
                    "ipv4Addr": upper.rapp.split(":")[0],
                    "port": int(upper.rapp.split(":")[1]),
                    "securityMethods": ["PKI"],
                },
            },
        },
        "a1": {
            "apiRoot": f"https://{upper.lower_a1}/A1-P/v2",
            "statusCallbackRoot": f"https://{upper.a1_status}/a1-status",
        },
        "e2Inventory": build_e2_inventory(observed_at=observed_at),
        "o1": {
            "netconf": {
                "endpoint": provider_netconf_endpoint,
                "knownHostsRef": SECRET_NETCONF_KNOWN_HOSTS,
                "credentialRef": SECRET_NETCONF_CREDENTIAL,
            },
            "fileDataReporting": {
                "mnsRoot": provider_mns_root,
                "mnsVersion": mns_version,
                "mnsAgentDn": "SubNetwork=oran-lab,ManagedElement=oai-gnb,MnsAgent=o1-agent",
                "consumerReference":
                    f"https://{upper.o1_consumer}/o1/file-data-reporting/notifications",
                "subscriptionId": "lo1-selftest-o1-subscription",
            },
            "sftp": {
                "allowedAuthorities": [provider_sftp_authority],
                "knownHostsRef": SECRET_SFTP_KNOWN_HOSTS,
                "credentialRef": SECRET_SFTP_CREDENTIAL,
            },
            "perfMetricJob": {
                "jobId": job_id,
                "managedObjectDn":
                    "SubNetwork=oran-lab,ManagedElement=oai-gnb,"
                    f"PerfMetricJob={job_id}",
                "managedObjectUri":
                    f"{provider_mns_root}/o1/managed-objects/"
                    f"SubNetwork=oran-lab,ManagedElement=oai-gnb,PerfMetricJob={job_id}",
            },
            "live": {
                "expectedPolicyCellCount": expected_cells,
                "expectedMeasurementWindow": {"start": window_start, "end": window_end},
                "recoveryFiles": {
                    "uniqueCandidate": file_info("recovery-unique.xml", unique=True),
                    "ambiguousCandidates": [
                        file_info(f"recovery-ambiguous-{index}.xml", unique=False)
                        for index in range(int(ambiguous_count))
                    ],
                },
            },
        },
        "topology": {
            "nearRtRicId": "lo1-selftest-near-rt-ric",
            "ueId": {"guAmfUeNgapId": "0x0000000001"},
            "servingCell": cells[0],
            "targetCell": cells[1],
            "cellMappings": cells,
        },
        "policy": {
            "validity": {"notBefore": _instant(-60), "expiresAt": _instant(3600)},
            "trace": {
                "intentId": _uuid(),
                "idempotencyKey": f"lo1-selftest:{rng.getrandbits(64):016x}",
                "correlationId": _uuid(),
                "producerId": "agentic-intent-coordinator",
            },
        },
        "security": {
            "truststoreRef": SECRET_TRUSTSTORE,
            "r1ClientCredentialRef": SECRET_R1_CLIENT,
            "o1NotificationCredentialRef": SECRET_O1_NOTIFICATION,
        },
        "timeouts": {
            "defaultStepMs": 15000,
            "liveCaptureMs": max(live_capture_floor, 1),
            "notificationWindowMs": notification_window,
        },
        "schemas": {
            "policyEvidenceRecordSchema": policy_evidence_schema,
            "policyEvidenceRecordSchemaCanonicalJson":
                canonicalize(policy_evidence_schema),
            "policyEvidenceRecordSchemaJcsSha256": jcs_sha256(policy_evidence_schema),
            "policyEvidenceFilterSchemaJcsSha256": jcs_sha256(filter_schema),
        },
    }
    return vector


def vector_digest(vector: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(vector, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def validate_against_frozen_schema(vector: Mapping[str, Any], bundle: FrozenBundle) -> None:
    """Validate against the FROZEN schema bytes; no local shadow rewrite."""
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    registry = Registry()
    for path in sorted(bundle.root.glob("*schema.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:  # pragma: no cover - defensive
            continue
        if not isinstance(document, dict) or "$schema" not in document:
            continue
        resource = Resource.from_contents(document, default_specification=DRAFT202012)
        registry = registry.with_resource(path.name, resource)
        identifier = document.get("$id")
        if isinstance(identifier, str):
            registry = registry.with_resource(identifier, resource)
    schema = bundle.vector_schema
    validator = Draft202012Validator(
        schema, registry=registry, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(vector), key=lambda error: list(error.path))
    if errors:
        rendered = "; ".join(
            f"/{'/'.join(str(part) for part in error.path)}: {error.message}"
            for error in errors[:5])
        raise VectorError(f"the vector fails frozen-schema validation: {rendered}")
