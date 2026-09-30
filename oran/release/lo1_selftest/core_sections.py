"""Core-runtime capture sections: from the runtime when it exists, synthetic otherwise.

``exchanges``, ``coordinator``, ``deterministicStubs`` and ``state`` are W2's
evidence.  Until ``oran/release/lo1/**`` exists there is no producer for them,
and the self-test must not pretend otherwise.  Two things follow:

* the sections built here are labelled ``SYNTHETIC_FALSIFIER_SUBSTRATE`` in the
  bundle's sidecar report, and the independent verifier reports that label back
  from the artefacts, so a substrate can never be read as a run;
* they are built from the frozen **step vector** -- step ids, ops, endpoint
  refs and declared statuses -- and never from ``/scenarios/83/expected``.  The
  oracle stays where it belongs: the verifier reads it, the evidence does not
  carry it.  A substrate that encoded the oracle would make the verifier's own
  adjudication circular.

Shape, not oracle: a healthy run has exactly one real ``process_intent`` call,
an all-``REAL`` S0-S6 history and zero synthetic transitions because that is
what ``COORDINATOR_PROCESS_INTENT`` means, not because a catalog counter says
so.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from typing import Any, Mapping

from oran.contract.jcs import jcs_sha256

from .capture_mirror import instant
from .frozen import FrozenBundle

#: Ops that put a status on the wire and therefore appear in ``/exchanges``.
#: ``O1_NOTIFY`` also carries a status, but it is inbound and belongs to
#: ``/notifications``; the verifier composes the two in catalog order.
HTTP_LIKE_OPS = ("HTTP", "R1_DME_DATA_JOB", "R1_DME_PUBLISH", "R1_DME_QUERY",
                 "A1_EMIT_STATUS")


def declared_status(step) -> int | None:
    """The status the frozen step declares, under either of its two names."""
    for member in ("expectedHttpStatus", "callbackExpectedHttpStatus"):
        if step.get(member) is not None:
            return int(step[member])
    return None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _root_for(step: Mapping[str, Any], vector: Mapping[str, Any]) -> str:
    endpoint_ref = str(step.get("endpointRef") or "")
    name = endpoint_ref.rsplit("/", 1)[-1]
    r1_root = vector["r1"]["apiRoot"]
    if name.startswith("r1DmePush"):
        return vector["r1"]["dme"]["policyEvidencePushBaseUri"]
    if name.startswith("r1PolicyStatusDestination"):
        return vector["r1"]["callbackApi"]["rootUri"]
    if step.get("op") == "A1_EMIT_STATUS":
        return vector["a1"]["statusCallbackRoot"]
    if name.startswith("a1"):
        return vector["a1"]["apiRoot"]
    return r1_root


def _message(body: Any, *, status: int | None = None) -> dict[str, Any]:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    message: dict[str, Any] = {
        "headerNamesPresent": ["Content-Type", "Version"],
        "headerBlockSha256": _digest("content-type:application/json\nversion:1.0.0"),
        "contentType": "application/json",
        "bodyByteCount": len(raw),
        "bodyRawSha256": hashlib.sha256(raw).hexdigest(),
        "bodyJcsSha256": jcs_sha256(body) if isinstance(body, (dict, list)) else None,
        "headerBlockCanonicalSha256": _digest("canonical-header-block"),
        "bodyCanonicalSha256": _digest("canonical-body"),
    }
    if message["bodyJcsSha256"] is None:
        del message["bodyJcsSha256"]
    if status is not None:
        message["status"] = status
    return message


def synthetic_core_sections(
    bundle: FrozenBundle,
    *,
    vector: Mapping[str, Any],
    scenario_id: str,
    normalization_records: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Core sections shaped by the frozen step vector, carrying no oracle value."""
    steps = bundle.steps(scenario_id)
    binding_id = str(vector["r1"]["dme"]["activeDeliveryBindingId"])
    policy_id = "lo1-selftest-policy-0001"
    data_job_id = "lo1-selftest-data-job-0001"

    exchanges: list[dict[str, Any]] = []
    sequence = 0
    push_count = 0
    for index, step in enumerate(steps):
        if step.get("op") not in HTTP_LIKE_OPS:
            continue
        declared = declared_status(step)
        root = _root_for(step, vector).rstrip("/")
        endpoint_ref = step.get("endpointRef")
        path = "/" + str(step["id"])
        resolved = f"{root}{path}"
        parsed = urllib.parse.urlsplit(resolved)
        method = str(step.get("method") or (
            "POST" if step.get("op") in (
                "R1_DME_DATA_JOB", "R1_DME_PUBLISH", "A1_EMIT_STATUS") else "GET"))
        outbound = step.get("actor") == "NON_RT_RIC_FRAMEWORK"
        captured: dict[str, Any] = {}
        if step.get("id") == "r1-create":
            captured["policyId"] = policy_id
        if step.get("id") == "create-dme-job":
            captured["dataJobId"] = data_job_id
            captured["deliveryBindingId"] = binding_id
        if step.get("op") == "R1_DME_PUBLISH":
            push_count += 1
            record = normalization_records[push_count - 1] \
                if len(normalization_records) >= push_count else None
            if record is not None:
                captured["evidenceRecordJcsSha256"] = record["recordJcsSha256"]
        exchange = {
            "sequence": sequence,
            "stepId": str(step["id"]),
            "stepIndex": index,
            "observedAt": instant(),
            "monotonicOffsetMs": sequence * 3,
            "direction": "UPPER_OUTBOUND" if outbound else "UPPER_INBOUND",
            "role": "CLIENT" if outbound else "SERVER",
            "peer": "LOWER_A1" if str(step["id"]) == "observe-a1-put" else "LOWER_RUNNER",
            "transport": {
                "scheme": "https",
                "peerAuthority": parsed.netloc,
                "tlsProtocol": "TLSv1.3",
                "peerAuthenticated": True,
            },
            "method": method,
            "resolvedUri": resolved,
            "endpointRef": endpoint_ref if endpoint_ref else None,
            "bindings": {
                key: str(value) for key, value in (step.get("bindings") or {}).items()
                if isinstance(value, str)
            },
            "request": _message({"stepId": str(step["id"])}),
            "response": _message(
                {"stepId": str(step["id"]), "ok": True},
                status=int(declared) if declared else 200),
            "attemptCount": 1,
            "timeoutMs": int(vector["timeouts"]["defaultStepMs"]),
            "timedOut": False,
            "correlationId": str(vector["policy"]["trace"]["correlationId"]),
            "idempotencyKey": str(vector["policy"]["trace"]["idempotencyKey"]),
            "capturedOutputs": captured,
            "declaredExpectedHttpStatus": int(declared) if declared else None,
        }
        if str(step["id"]) == "r1-create":
            exchange["response"]["locationHeader"] = f"/policies/{policy_id}"
            exchange["response"]["locationLastPathSegment"] = policy_id
        if str(step["id"]) == "create-dme-job":
            exchange["response"]["locationHeader"] = f"/data-jobs/{data_job_id}"
            exchange["response"]["locationLastPathSegment"] = data_job_id
        exchanges.append(exchange)
        sequence += 1

    stub_steps = [step for step in steps
                  if step.get("op") in ("KPM_SNAPSHOT", "E2_CONTROL_RESULT", "READBACK")]
    observations = {"KPM_SNAPSHOT": [], "E2_CONTROL_RESULT": [], "READBACK": []}
    for offset, step in enumerate(stub_steps):
        observations[str(step["op"])].append({
            "stepId": str(step["id"]),
            "sequence": offset,
            "at": instant(),
            "payloadJcsSha256": jcs_sha256({"stepId": str(step["id"])}),
            "servedBy": "UPPER_DETERMINISTIC_STUB",
        })
    write_ledger = [
        {"sequence": 0, "kind": "NORMAL", "live": False, "at": instant(),
         "targetRef": "deterministic-stub"}
    ] if observations["E2_CONTROL_RESULT"] else []

    def snapshot(*, after: bool) -> dict[str, Any]:
        body = {
            "policyCount": 1 if after else 0,
            "statusCount": 1 if after else 0,
            "dmeDataJobCount": 1 if after else 0,
            "deliveryBindingCount": 1 if after else 0,
            "acceptedPushPayloadCounts": {binding_id: push_count} if after else {},
            "subscriptionCount": 1 if after else 1,
            "evidenceCommitCount": sum(
                1 for record in normalization_records if record["commitEligible"]
            ) if after else 0,
            "retrievedFileCount": 1 if after else 0,
            "perfMetricJobPresent": None,
            "policyIds": [policy_id] if after else [],
        }
        body["jcsSha256"] = jcs_sha256(body)
        return body

    coordinator = {
        "processIntentCallsBefore": 0,
        "processIntentCallsAfter": 1,
        "processIntentCallsDelta": 1,
        "executionMode": "REAL_PROCESS_INTENT",
        "fsmHistory": [
            {"from": "S0", "to": "S1", "outcome": None, "origin": "REAL"},
            {"from": "S1", "to": "S2", "outcome": None, "origin": "REAL"},
            {"from": "S2", "to": "S3", "outcome": None, "origin": "REAL"},
            {"from": "S3", "to": "S4", "outcome": None, "origin": "REAL"},
            {"from": "S4", "to": "S6", "outcome": "Admitted", "origin": "REAL"},
        ],
        "terminalOutcome": "Admitted",
        "terminalOutcomeCount": 1,
        "terminalEvidenceRef": "ledger://lo1-selftest/evidence/terminal",
        "ledgerReferences": ["ledger://lo1-selftest/evidence/0",
                             "ledger://lo1-selftest/evidence/1"],
        "requiresRealCoordinatorExecution": True,
        "syntheticTransitionCount": 0,
        "evidenceRecordsSuppliedFromNormalization": [
            record["recordJcsSha256"] for record in normalization_records],
        "nowInput": normalization_records[0]["window"]["end"]
        if normalization_records else instant(),
    }

    return {
        "exchanges": exchanges,
        "coordinator": coordinator,
        "deterministicStubs": {
            "routing": "UPPER_DETERMINISTIC_STUB",
            "kpmSnapshots": observations["KPM_SNAPSHOT"],
            "controlResults": observations["E2_CONTROL_RESULT"],
            "readbacks": observations["READBACK"],
            "writeLedger": write_ledger,
            "normalRanWrites": sum(
                1 for entry in write_ledger if entry["kind"] == "NORMAL"),
            "rollbackRanWrites": sum(
                1 for entry in write_ledger if entry["kind"] == "ROLLBACK"),
        },
        "state": {
            "before": snapshot(after=False),
            "after": snapshot(after=True),
            "diffPointers": ["/policyCount", "/statusCount", "/dmeDataJobCount",
                             "/evidenceCommitCount", "/retrievedFileCount"],
        },
    }
