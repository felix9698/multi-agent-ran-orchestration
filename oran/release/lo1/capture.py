"""Machine-readable capture for the upper-live-o1-harness release (S2 seam).

The capture document is an *observation* record.  It never carries a verdict:
the oracle is the lower frozen runner and lives in the scenario catalog, which
this document references by JSON pointer and never copies.

This module is the single writer seam.  W1 (the O1 runtime) never assembles the
document; it calls the ``emit_*`` and ``set_*`` seams frozen in
``work-split.1.0.0.json#/frozenInterfaces``.  Every fragment is validated against
the frozen ``capture-schema.1.0.0.json`` bytes *before* it is appended, so an
invalid observation fails the request that produced it rather than surfacing at
export time.

Two live-mode properties differ from the bilateral release:

* SC-084 declares ``materialization.time.mode = LIVE_OBSERVED``, so wall-clock
  reads on the request path are legitimate and required.  :class:`ObservedClock`
  records them; what stays forbidden is a *hidden* sleep standing in for a
  declared observable event, which ``clock.hiddenSleepCount`` measures.
* Raw wire artefacts (NETCONF frames, notification bodies, retrieved PM files)
  are stored verbatim by :class:`RawStore` **before** they are parsed, and every
  digest in the document is taken over the stored bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from xml.etree import ElementTree

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from oran.contract.jcs import CanonicalizationError, canonicalize_bytes

from .o1_readiness import readiness_is_complete

SCHEMA_ID = "urn:oran-aic:upper-live-o1-harness:capture:1.0.0"
SCHEMA_FILE_NAME = "capture-schema.1.0.0.json"
SCHEMA_VERSION = "oran-aic-upper-live-o1-harness-capture/1.0.0"
SCHEMA_ID_V2 = "urn:oran-aic:upper-live-o1-harness:capture:2.0.0"
SCHEMA_FILE_NAME_V2 = "capture-schema.2.0.0.json"
SCHEMA_VERSION_V2 = "oran-aic-upper-live-o1-harness-capture/2.0.0"
_SUPPORTED_SCHEMA_IDS = frozenset({SCHEMA_ID, SCHEMA_ID_V2})
REDACTION_POLICY = "oran-aic-upper-live-o1-harness-redaction/1.0.0"

ORACLE_SOURCE: dict[str, Any] = {
    "origin": "SCENARIO_CATALOG_1_0_1",
    "expectedPointer": "/scenarios/83/expected",
    "rulesPointer": "/scenarios/83/rules",
    "assignmentPointer": "/assignments/65",
    "valuesCopiedIntoThisDocument": 0,
}
ORACLE_OWNERSHIP = "LOWER_SIDE_CONFORMANCE_RUNNER_UNDER_LIVE_O1_AUTHORITY"

#: Header names that are never captured, not even as names.
REDACTED_HEADER_NAMES = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "x-auth-token",
})
#: Any header whose name matches this pattern is redacted as well.
REDACTED_HEADER_PATTERN = re.compile(
    r"(?i)(authorization|cookie|token|secret|credential|password|api[-_]?key)")

#: Credential markers G-SEC-1 scans for, applied here to the STORED raw bytes so
#: the redaction claim is a measurement rather than an assertion.
CREDENTIAL_MARKERS = (
    b"Bearer ", b"password=", b"secret=", b"api-key=", b"apikey=",
)
PRIVATE_KEY_MARKERS = (
    b"-----BEGIN RSA PRIVATE KEY", b"-----BEGIN EC PRIVATE KEY",
    b"-----BEGIN OPENSSH PRIVATE KEY", b"-----BEGIN PRIVATE KEY",
    b"-----BEGIN ENCRYPTED PRIVATE KEY", b"PuTTY-User-Key-File",
)

RAW_KINDS = ("o1", "netconf", "notify", "release")
_RAW_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

_CAPTURE_NAMESPACE = uuid.UUID("6f1e2a71-4b0c-4f2a-9a4f-1c5f0c9d7b22")


class CaptureError(RuntimeError):
    """A capture fragment was rejected; the request must fail closed."""


# -- schema location and validation ---------------------------------------

def _schema_search_roots() -> tuple[Path, ...]:
    here = Path(__file__).resolve()
    # <repo>/oran/release/lo1/capture.py -> <repo>/docs/upper-live-o1-harness
    repo_root = here.parents[3]
    # <release>/lib/oran/release/lo1/capture.py -> <release>/spec
    release_root = here.parents[4]
    return (
        repo_root / "docs" / "upper-live-o1-harness",
        release_root / "spec",
        here.parent / "spec",
    )


def default_schema_path() -> Path:
    """Locate the frozen capture schema in a repo or in an extracted release."""
    for root in _schema_search_roots():
        candidate = root / SCHEMA_FILE_NAME
        if candidate.is_file():
            return candidate
    raise CaptureError("capture schema %s was not found" % SCHEMA_FILE_NAME)


def corrected_schema_path() -> Path:
    """Locate the incompatible capture schema required by release 1.0.3."""
    for root in _schema_search_roots():
        candidate = root / SCHEMA_FILE_NAME_V2
        if candidate.is_file():
            return candidate
    raise CaptureError("capture schema %s was not found" % SCHEMA_FILE_NAME_V2)


_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}
_VALIDATOR_CACHE: dict[tuple[str, str], Draft202012Validator] = {}
_CACHE_LOCK = threading.RLock()


def load_schema(schema_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(schema_path) if schema_path is not None else default_schema_path()
    key = str(path.resolve())
    with _CACHE_LOCK:
        cached = _SCHEMA_CACHE.get(key)
        if cached is None:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("$id") not in _SUPPORTED_SCHEMA_IDS:
                raise CaptureError("capture schema $id mismatch: %s" % cached.get("$id"))
            _SCHEMA_CACHE[key] = cached
        return cached


def _validator(schema_path: Path, fragment: str) -> Draft202012Validator:
    key = (str(schema_path.resolve()), fragment)
    with _CACHE_LOCK:
        cached = _VALIDATOR_CACHE.get(key)
        if cached is None:
            schema = load_schema(schema_path)
            schema_id = str(schema["$id"])
            registry = Registry().with_resource(
                schema_id, Resource.from_contents(schema))
            target = schema if not fragment else {"$ref": schema_id + fragment}
            cached = Draft202012Validator(
                target, registry=registry, format_checker=FormatChecker())
            _VALIDATOR_CACHE[key] = cached
        return cached


def validate_capture(document: Mapping[str, Any], *, schema_path: Path,
                     capture_root: Path | None = None) -> None:
    """Validate a whole capture document against the frozen schema bytes.

    Two adjudications, in order.  The first is the schema's.  The second is the
    one the withdrawn 1.0.1 had no way to make: a document that declares
    ``run.disposition == COMPLETED`` must carry a readiness ledger in which
    every §10.2 state is confirmed.  It is a property OF THE DOCUMENT, so a
    capture produced elsewhere is judged by the same rule as one this process
    wrote, and an absent ledger is a refusal rather than an omission.
    """
    path = Path(schema_path)
    errors = sorted(_validator(path, "").iter_errors(document),
                    key=lambda error: list(error.absolute_path))
    if errors:
        first = errors[0]
        raise CaptureError("capture document is invalid at %s: %s" % (
            "/" + "/".join(str(part) for part in first.absolute_path), first.message))
    _validate_execution_window(document)
    run = document.get("run")
    disposition = str(run.get("disposition")) if isinstance(run, Mapping) else ""
    if disposition == "COMPLETED":
        window = run.get("executionWindow") if isinstance(run, Mapping) else None
        if not isinstance(window, Mapping) or window.get("withinWindow") is not True:
            raise CaptureError(
                "capture document is invalid at /run/disposition: COMPLETED "
                "requires the whole observed run to remain inside its approved "
                "execution window")
        complete, reasons = readiness_is_complete(document)
        if not complete:
            raise CaptureError(
                "capture document is invalid at /run/disposition: COMPLETED "
                "requires an established O1 readiness lifecycle (%s)"
                % ", ".join(reasons))
    if load_schema(path).get("$id") == SCHEMA_ID_V2:
        _validate_netconf_semantics(document, capture_root=capture_root)
        if disposition == "COMPLETED":
            complete, reasons = upper_body_coverage_is_complete(
                document, require_final=True)
            if not complete:
                raise CaptureError(
                    "capture document is invalid at /run/disposition: COMPLETED "
                    "requires full upper measured-body coverage (%s)"
                    % ", ".join(reasons))


def _utc_instant(value: Any) -> datetime:
    text = str(value)
    if not text.endswith("Z"):
        raise ValueError("only UTC instants ending in Z are accepted")
    return datetime.fromisoformat(text[:-1] + "+00:00")


def _validate_execution_window(document: Mapping[str, Any]) -> None:
    """Recompute the window claim from the observed run endpoints.

    A long-lived harness can cross ``approvedNotAfter`` after its admission
    gate has passed.  ``withinWindow`` therefore cannot be copied from the
    admission decision; it is true only when the entire observed interval lies
    within the authority-approved interval.
    """
    run = document.get("run")
    if not isinstance(run, Mapping):  # JSON Schema reports the structural error
        return
    window = run.get("executionWindow")
    if not isinstance(window, Mapping):
        return
    try:
        started = _utc_instant(run.get("startedAt"))
        ended = _utc_instant(run.get("endedAt"))
        approved_start = _utc_instant(window.get("approvedNotBefore"))
        approved_end = _utc_instant(window.get("approvedNotAfter"))
    except (TypeError, ValueError) as exc:
        raise _semantic_error("LO1-CAP-EXECUTION-WINDOW", str(exc)) from None
    observed = approved_start <= started <= ended <= approved_end
    if bool(window.get("withinWindow")) is not observed:
        raise _semantic_error(
            "LO1-CAP-EXECUTION-WINDOW",
            "withinWindow does not match the observed start/end interval")


_EXCHANGE_OPERATIONS = frozenset({
    "HTTP", "R1_DME_QUERY", "R1_DME_DATA_JOB", "R1_DME_PUBLISH",
    "A1_EMIT_STATUS", "O1_NOTIFY",
})


def upper_body_coverage_is_complete(
        document: Mapping[str, Any], *, require_final: bool,
        role_table: Mapping[str, Any] | None = None) -> tuple[bool, list[str]]:
    """Re-derive completion of the upper-owned SC-084 body.

    This is deliberately not the frozen oracle.  It only establishes that the
    upper surfaces/clients/in-process components assigned by the role table
    were each exercised exactly once.  The lower runner remains the sole owner
    of HTTP/status/outcome adjudication and of the SC-084 verdict.
    """
    if role_table is None:
        from .ics import load_role_table  # local import avoids a module cycle

        role_table = load_role_table()
    rows = role_table.get("steps") if isinstance(role_table, Mapping) else None
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return False, ["ROLE_TABLE_STEPS_ABSENT"]

    failures: list[str] = []
    expected_exchanges: list[tuple[int, str, str, str]] = []
    expected_retrievals = 0
    expected_notifications = 0
    expected_normalizations = 0
    expected_coordinator = 0
    expected_stubs: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            failures.append("ROLE_TABLE_STEP_MALFORMED")
            continue
        obligation = str(row.get("upperObligation", ""))
        operation = str(row.get("op", ""))
        step_id = str(row.get("id", ""))
        try:
            step_index = int(str(row.get("catalogPointer", "")).rsplit("/", 1)[1])
        except (IndexError, ValueError):
            failures.append("ROLE_TABLE_STEP_POINTER_MALFORMED:%s" % step_id)
            continue
        if obligation == "REQUIRED_SURFACE" and operation == "O1_NOTIFY":
            expected_notifications += 1
        elif (obligation in {"REQUIRED_SURFACE", "REQUIRED_CLIENT"}
                and operation in _EXCHANGE_OPERATIONS):
            # REQUIRED_CLIENT always crosses outbound.  The R1 status relay is
            # the one REQUIRED_SURFACE whose inbound half is intentionally
            # suppressed because both endpoints are upper-owned; its single
            # authoritative record is the live outbound CLIENT exchange.
            outbound_self_relay = (
                step_id == "r1-status-to-rapp"
                and str(row.get("hostOwner")) == "UPPER_HARNESS"
                and str(row.get("actor")) == "NON_RT_RIC_FRAMEWORK")
            direction = ("UPPER_OUTBOUND"
                         if (obligation == "REQUIRED_CLIENT"
                             or outbound_self_relay)
                         else "UPPER_INBOUND")
            role = "CLIENT" if direction == "UPPER_OUTBOUND" else "SERVER"
            expected_exchanges.append((step_index, step_id, direction, role))
        elif obligation == "REQUIRED_CLIENT" and operation == "O1_RETRIEVE":
            expected_retrievals += 1
        elif obligation == "REQUIRED_INPROC" and operation == "O1_NORMALIZE":
            expected_normalizations += 1
        elif (obligation == "REQUIRED_INPROC"
              and operation == "COORDINATOR_PROCESS_INTENT"):
            expected_coordinator += 1
        elif obligation == "REQUIRED_CAPABILITY_ROUTING_ASSIGNED":
            expected_stubs[operation] = step_id

    exchanges = document.get("exchanges")
    observed_exchanges: list[tuple[int, str, str, str]] = []
    if isinstance(exchanges, Sequence) and not isinstance(exchanges, (str, bytes)):
        for item in exchanges:
            if isinstance(item, Mapping):
                try:
                    observed_exchanges.append(
                        (int(item.get("stepIndex")), str(item.get("stepId")),
                         str(item.get("direction")), str(item.get("role"))))
                except (TypeError, ValueError):
                    failures.append("EXCHANGE_IDENTITY_MALFORMED")
            else:
                failures.append("EXCHANGE_MALFORMED")
    else:
        failures.append("EXCHANGE_VECTOR_ABSENT")
    if observed_exchanges != expected_exchanges:
        failures.append(
            "EXCHANGE_COVERAGE_MISMATCH:expected=%s:observed=%s"
            % (expected_exchanges, observed_exchanges))

    notifications = document.get("notifications")
    accepted_notifications = [] if not isinstance(notifications, Sequence) else [
        item for item in notifications
        if isinstance(item, Mapping)
        and item.get("accepted") is True
        and item.get("responseStatus") == 204
        and item.get("durablyAcceptedBeforeResponse") is True
        and item.get("subscriptionIdMatched") is True]
    if len(accepted_notifications) != expected_notifications:
        failures.append("O1_NOTIFICATION_COVERAGE:%d/%d" % (
            len(accepted_notifications), expected_notifications))

    retrievals = document.get("retrievals")
    successful_retrievals = [] if not isinstance(retrievals, Sequence) else [
        item for item in retrievals
        if isinstance(item, Mapping)
        and item.get("sourceStepId") == "o1-notify"
        and item.get("outcome") == "RETRIEVED"]
    if len(successful_retrievals) != expected_retrievals:
        failures.append("O1_RETRIEVAL_COVERAGE:%d/%d" % (
            len(successful_retrievals), expected_retrievals))

    normalization = document.get("normalization")
    invocations = (normalization.get("invocations", -1)
                   if isinstance(normalization, Mapping) else -1)
    if invocations != expected_normalizations:
        failures.append("O1_NORMALIZATION_COVERAGE:%s/%d" % (
            invocations, expected_normalizations))

    coordinator = document.get("coordinator")
    if not isinstance(coordinator, Mapping):
        failures.append("COORDINATOR_COVERAGE_ABSENT")
    elif expected_coordinator != 1 or not (
            coordinator.get("processIntentCallsDelta") == 1
            and coordinator.get("executionMode") == "REAL_PROCESS_INTENT"
            and coordinator.get("requiresRealCoordinatorExecution") is True):
        failures.append("COORDINATOR_REAL_CALL_COVERAGE")

    stubs = document.get("deterministicStubs")
    if not isinstance(stubs, Mapping):
        failures.append("DETERMINISTIC_STUB_COVERAGE_ABSENT")
    else:
        routing = str(stubs.get("routing", "UNRESOLVED"))
        buckets = {
            "KPM_SNAPSHOT": "kpmSnapshots",
            "E2_CONTROL_RESULT": "controlResults",
            "READBACK": "readbacks",
        }
        if routing == "UPPER_DETERMINISTIC_STUB":
            for operation, step_id in expected_stubs.items():
                records = stubs.get(buckets.get(operation, ""), [])
                observed = [str(item.get("stepId")) for item in records
                            if isinstance(item, Mapping)] \
                    if isinstance(records, Sequence) else []
                if observed != [step_id]:
                    failures.append("STUB_COVERAGE_%s:%s" % (operation, observed))
        elif routing == "LOWER_IMPLEMENTATION_UNDER_TEST":
            for name in buckets.values():
                if stubs.get(name) not in ([], ()):
                    failures.append("LOWER_ROUTING_HAS_UPPER_%s" % name)
        else:
            failures.append("DETERMINISTIC_STUB_ROUTING_UNRESOLVED")

    ready, readiness_failures = readiness_is_complete(document)
    if not ready:
        failures.extend(readiness_failures)
    netconf = document.get("netconf")
    segment = netconf.get("measuredSegment") if isinstance(netconf, Mapping) else None
    delta = segment.get("delta") if isinstance(segment, Mapping) else None
    if not isinstance(delta, Mapping) or delta.get("measuredRpcDelta") != 0:
        failures.append("MEASURED_NETCONF_RPC_DELTA_NONZERO_OR_ABSENT")

    if require_final:
        cleanup = document.get("cleanup")
        if not isinstance(cleanup, Mapping) or cleanup.get("complete") is not True \
                or list(cleanup.get("residual") or []):
            failures.append("CLEANUP_INCOMPLETE")
        readiness = netconf.get("readiness") if isinstance(netconf, Mapping) else None
        order = readiness.get("teardownOrder", []) \
            if isinstance(readiness, Mapping) else []
        if "IN_FLIGHT_DRAINED" not in order:
            failures.append("MEASURED_ADMISSION_DRAIN_UNPROVEN")
        run = document.get("run")
        window = run.get("executionWindow") if isinstance(run, Mapping) else None
        if not isinstance(window, Mapping) or window.get("withinWindow") is not True:
            failures.append("EXECUTION_WINDOW_EXPIRED")
    return not failures, failures


_NETCONF_BASE_11 = "urn:ietf:params:netconf:base:1.1"
_NETCONF_EOM = b"]]>]]>"
_COUNTER_PAIRS = (
    ("measuredRpcDelta", "rpcRecordCount"),
    ("clientToServerOctetDelta", "clientToServerOctetCount"),
    ("serverToClientOctetDelta", "serverToClientOctetCount"),
    ("clientToServerMessageDelta", "clientToServerMessageCount"),
    ("serverToClientMessageDelta", "serverToClientMessageCount"),
    ("clientToServerChunkDelta", "clientToServerChunkCount"),
    ("serverToClientChunkDelta", "serverToClientChunkCount"),
)


def _semantic_error(code: str, detail: str) -> CaptureError:
    return CaptureError("%s: %s" % (code, detail))


def _validate_netconf_semantics(document: Mapping[str, Any], *,
                                capture_root: Path | None) -> None:
    """Re-derive the NETCONF assertions that JSON Schema cannot express.

    Version 2 deliberately refuses to trust counters, a framing label, or the
    decoded RPC list by themselves.  When raw artefacts are available the
    validator independently parses the two complete SSH-subsystem plaintext
    streams and proves a one-to-one relationship with the capture records.
    """
    _validate_sequence_high_water(document)
    netconf = document.get("netconf")
    if not isinstance(netconf, Mapping):
        return
    session = netconf.get("session")
    rpcs = netconf.get("rpcs")
    rpcs = list(rpcs) if isinstance(rpcs, Sequence) else []
    if any(isinstance(record, Mapping) and record.get("phase") == "MEASURED"
           for record in rpcs):
        raise _semantic_error(
            "LO1-CAP-MEASURED-RPC",
            "the measured segment contains a NETCONF RPC record")
    if isinstance(session, Mapping):
        server_caps = set(str(item) for item in
                          session.get("advertisedCapabilities", ()))
        client_caps = set(str(item) for item in
                          session.get("clientAdvertisedCapabilities", ()))
        if (_NETCONF_BASE_11 in server_caps and _NETCONF_BASE_11 in client_caps
                and session.get("negotiatedFraming") != "CHUNKED"):
            raise _semantic_error(
                "LO1-CAP-BASE11-NOT-CHUNKED",
                "both peers advertised base:1.1 but the capture did not "
                "record RFC 6242 chunked framing")

    segment = netconf.get("measuredSegment")
    disposition = (document.get("run") or {}).get("disposition")
    if disposition == "COMPLETED" and not isinstance(segment, Mapping):
        raise _semantic_error(
            "LO1-CAP-MEASURED-SEGMENT-MISSING",
            "COMPLETED requires observed start/end NETCONF counters")
    if not isinstance(segment, Mapping):
        return
    if not isinstance(session, Mapping):
        raise _semantic_error(
            "LO1-CAP-SESSION-BINDING",
            "a measured segment has no NETCONF session")
    wire = session.get("wireEvidence")
    if not isinstance(wire, Mapping) or segment.get("sessionId") != wire.get("sessionId"):
        raise _semantic_error(
            "LO1-CAP-SESSION-BINDING",
            "the measured segment and wire evidence identify different sessions")

    start = segment.get("start")
    end = segment.get("end")
    delta = segment.get("delta")
    if not all(isinstance(value, Mapping) for value in (start, end, delta)):
        raise _semantic_error("LO1-CAP-MEASURED-DELTA",
                              "counter snapshots are incomplete")
    assert isinstance(start, Mapping) and isinstance(end, Mapping)
    assert isinstance(delta, Mapping)
    if (int(end["captureSequence"]) < int(start["captureSequence"])
            or int(end["monotonicNs"]) < int(start["monotonicNs"])):
        raise _semantic_error("LO1-CAP-MEASURED-DELTA",
                              "measured counter ordering moved backwards")
    for delta_name, counter_name in _COUNTER_PAIRS:
        observed = int(end[counter_name]) - int(start[counter_name])
        declared = int(delta[delta_name])
        if observed != declared or declared != 0:
            raise _semantic_error(
                "LO1-CAP-MEASURED-DELTA",
                "%s is declared %d but independently recomputes to %d; "
                "all measured NETCONF deltas must be zero"
                % (delta_name, declared, observed))

    lifecycle_sequences = [int(record["sequence"]) for record in rpcs
                           if isinstance(record, Mapping)
                           and record.get("phase") == "LIFECYCLE"]
    teardown_sequences = [int(record["sequence"]) for record in rpcs
                          if isinstance(record, Mapping)
                          and record.get("phase") == "TEARDOWN"]
    if lifecycle_sequences and max(lifecycle_sequences) >= int(start["captureSequence"]):
        raise _semantic_error("LO1-CAP-MEASURED-BOUNDARY",
                              "a lifecycle RPC is not before the measured segment")
    if teardown_sequences and min(teardown_sequences) < int(end["captureSequence"]):
        raise _semantic_error("LO1-CAP-MEASURED-BOUNDARY",
                              "a teardown RPC precedes the measured segment end")

    if capture_root is not None:
        _validate_wire_evidence(session, rpcs, capture_root=Path(capture_root),
                                segment=segment)


def _capture_sequences(document: Mapping[str, Any]) -> list[int]:
    """Enumerate every schema-defined global sequence-bearing record."""
    groups: list[Any] = [
        document.get("exchanges", ()),
        document.get("harnessOperations", ()),
        (document.get("netconf") or {}).get("rpcs", ()),
        document.get("notifications", ()),
        document.get("retrievals", ()),
    ]
    stubs = document.get("deterministicStubs") or {}
    for name in ("kpmSnapshots", "controlResults", "readbacks", "writeLedger"):
        groups.append(stubs.get(name, ()))
    values: list[int] = []
    for group in groups:
        if not isinstance(group, Sequence) or isinstance(group, (str, bytes)):
            continue
        values.extend(int(item["sequence"]) for item in group
                      if isinstance(item, Mapping) and "sequence" in item)
    return values


def _validate_sequence_high_water(document: Mapping[str, Any]) -> None:
    run = document.get("run") or {}
    declared = int(run.get("sequenceHighWaterMark", -1))
    exact = max(_capture_sequences(document) or [0])
    if declared != exact:
        raise _semantic_error(
            "LO1-CAP-SEQUENCE-HIGH-WATER",
            "sequenceHighWaterMark is %d but all sequence-bearing records "
            "re-derive %d" % (declared, exact))


def _raw_bytes(descriptor: Mapping[str, Any], *, capture_root: Path) -> bytes:
    relative = str(descriptor.get("artifactPath", ""))
    root = capture_root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise _semantic_error("LO1-CAP-RAW-PATH",
                              "a raw artefact escapes the capture root") from exc
    if not path.is_file():
        raise _semantic_error("LO1-CAP-RAW-MISSING", relative)
    payload = path.read_bytes()
    if (len(payload) != int(descriptor.get("byteCount", -1))
            or hashlib.sha256(payload).hexdigest() != descriptor.get("sha256")):
        raise _semantic_error("LO1-CAP-RAW-DIGEST", relative)
    return payload


def _parse_chunked_stream(
        payload: bytes) -> tuple[list[bytes], list[int], list[int]]:
    messages: list[bytes] = []
    chunk_counts: list[int] = []
    end_offsets: list[int] = []
    offset = 0
    while offset < len(payload):
        parts: list[bytes] = []
        message_chunks = 0
        while True:
            if payload.startswith(b"\n##\n", offset):
                if message_chunks == 0:
                    raise _semantic_error(
                        "LO1-CAP-WIRE-FRAMING",
                        "an RFC 6242 chunked message must contain at least one chunk")
                offset += 4
                messages.append(b"".join(parts))
                chunk_counts.append(message_chunks)
                end_offsets.append(offset)
                break
            if not payload.startswith(b"\n#", offset):
                raise _semantic_error("LO1-CAP-WIRE-FRAMING",
                                      "post-hello data is not RFC 6242 chunked")
            size_start = offset + 2
            size_end = payload.find(b"\n", size_start)
            if size_end < 0:
                raise _semantic_error("LO1-CAP-WIRE-FRAMING",
                                      "a chunk header is incomplete")
            digits = payload[size_start:size_end]
            if (not digits.isdigit() or digits.startswith(b"0")):
                raise _semantic_error("LO1-CAP-WIRE-FRAMING",
                                      "a chunk size is not canonical")
            size = int(digits)
            data_start = size_end + 1
            data_end = data_start + size
            if data_end > len(payload):
                raise _semantic_error("LO1-CAP-WIRE-FRAMING",
                                      "a chunk body is incomplete")
            parts.append(payload[data_start:data_end])
            message_chunks += 1
            offset = data_end
    return messages, chunk_counts, end_offsets


def _capabilities_from_hello(payload: bytes) -> list[str]:
    try:
        root = ElementTree.fromstring(payload.decode("utf-8"))
    except (UnicodeDecodeError, ElementTree.ParseError) as exc:
        raise _semantic_error("LO1-CAP-WIRE-HELLO", "hello XML does not parse") from exc
    if root.tag.rsplit("}", 1)[-1] != "hello":
        raise _semantic_error("LO1-CAP-WIRE-HELLO",
                              "the first wire message is not NETCONF hello")
    return sorted({(node.text or "").strip() for node in root.iter()
                   if node.tag.rsplit("}", 1)[-1] == "capability"
                   and (node.text or "").strip()})


def _decode_direction(
        payload: bytes, *, label: str
        ) -> tuple[bytes, list[bytes], list[int], list[int]]:
    marker = payload.find(_NETCONF_EOM)
    if marker < 0:
        raise _semantic_error("LO1-CAP-WIRE-SUMMARY",
                              "%s has no hello end marker" % label)
    hello = payload[:marker]
    post = payload[marker + len(_NETCONF_EOM):]
    if _NETCONF_EOM in post:
        raise _semantic_error("LO1-CAP-WIRE-SUMMARY",
                              "%s contains post-hello EOM framing" % label)
    messages, chunk_counts, end_offsets = _parse_chunked_stream(post)
    return hello, messages, chunk_counts, end_offsets


def _validate_wire_evidence(session: Mapping[str, Any],
                            rpcs: Sequence[Mapping[str, Any]], *,
                            capture_root: Path,
                            segment: Mapping[str, Any]) -> None:
    wire = session["wireEvidence"]
    client_wire = _raw_bytes(wire["clientToServerRaw"], capture_root=capture_root)
    server_wire = _raw_bytes(wire["serverToClientRaw"], capture_root=capture_root)
    client_hello, requests, client_chunk_counts, client_ends = _decode_direction(
        client_wire, label="client-to-server")
    server_hello, replies, server_chunk_counts, server_ends = _decode_direction(
        server_wire, label="server-to-client")
    expected_summary = {
        "clientToServerMessageCount": 1 + len(requests),
        "serverToClientMessageCount": 1 + len(replies),
        "clientToServerChunkCount": sum(client_chunk_counts),
        "serverToClientChunkCount": sum(server_chunk_counts),
        "clientToServerHelloEomCount": 1,
        "serverToClientHelloEomCount": 1,
        "clientToServerPostHelloEomCount": 0,
        "serverToClientPostHelloEomCount": 0,
    }
    for name, observed in expected_summary.items():
        if int(wire.get(name, -1)) != observed:
            raise _semantic_error(
                "LO1-CAP-WIRE-SUMMARY",
                "%s is %r but raw wire re-derives %d"
                % (name, wire.get(name), observed))
    if (_raw_bytes(session["clientHelloRaw"], capture_root=capture_root) != client_hello
            or _raw_bytes(session["helloRaw"], capture_root=capture_root) != server_hello):
        raise _semantic_error("LO1-CAP-WIRE-BIJECTION",
                              "hello descriptors do not match the wire streams")
    derived_session_id = hashlib.sha256(client_hello + server_hello).hexdigest()
    if wire.get("sessionId") != derived_session_id:
        raise _semantic_error("LO1-CAP-SESSION-BINDING",
                              "sessionId is not derived from both hello bytes")
    if (_capabilities_from_hello(client_hello)
            != sorted(str(item) for item in session["clientAdvertisedCapabilities"])
            or _capabilities_from_hello(server_hello)
            != sorted(str(item) for item in session["advertisedCapabilities"])):
        raise _semantic_error("LO1-CAP-WIRE-BIJECTION",
                              "advertised capabilities differ from the hello bytes")
    if len(requests) != len(rpcs) or len(replies) != len(rpcs):
        raise _semantic_error("LO1-CAP-WIRE-BIJECTION",
                              "wire message count differs from RPC records")
    for index, record in enumerate(rpcs):
        request_raw = _raw_bytes(record["requestRaw"], capture_root=capture_root)
        reply_raw = _raw_bytes(record["replyRaw"], capture_root=capture_root)
        if request_raw != requests[index] or reply_raw != replies[index]:
            raise _semantic_error("LO1-CAP-WIRE-BIJECTION",
                                  "RPC %d differs from the wire stream" % index)
        if record.get("requestFixtureSha256") != hashlib.sha256(request_raw).hexdigest():
            raise _semantic_error("LO1-CAP-WIRE-BIJECTION",
                                  "RPC %d fixture digest differs from request bytes"
                                  % index)
    phases = [str(record["phase"]) for record in rpcs]
    lifecycle_count = phases.count("LIFECYCLE")
    if phases != (["LIFECYCLE"] * lifecycle_count
                  + ["TEARDOWN"] * phases.count("TEARDOWN")):
        raise _semantic_error("LO1-CAP-MEASURED-BOUNDARY",
                              "RPC phases are not lifecycle then teardown")

    def prefix_octets(hello: bytes, ends: Sequence[int], count: int) -> int:
        post_octets = int(ends[count - 1]) if count else 0
        return len(hello) + len(_NETCONF_EOM) + post_octets

    end = segment["end"]
    boundary_counts = {
        "clientToServerOctetCount": prefix_octets(
            client_hello, client_ends, lifecycle_count),
        "serverToClientOctetCount": prefix_octets(
            server_hello, server_ends, lifecycle_count),
        "clientToServerMessageCount": 1 + lifecycle_count,
        "serverToClientMessageCount": 1 + lifecycle_count,
        "clientToServerChunkCount": sum(client_chunk_counts[:lifecycle_count]),
        "serverToClientChunkCount": sum(server_chunk_counts[:lifecycle_count]),
        "rpcRecordCount": lifecycle_count,
    }
    for name, observed in boundary_counts.items():
        if int(end[name]) != observed:
            raise _semantic_error("LO1-CAP-WIRE-SUMMARY",
                                  "%s does not match the measured-boundary "
                                  "wire prefix" % name)


def validate_fragment(fragment: Mapping[str, Any], *, schema_path: Path,
                      pointer: str) -> None:
    path = Path(schema_path)
    errors = sorted(_validator(path, pointer).iter_errors(fragment),
                    key=lambda error: list(error.absolute_path))
    if errors:
        first = errors[0]
        raise CaptureError("capture fragment %s is invalid at %s: %s" % (
            pointer, "/" + "/".join(str(part) for part in first.absolute_path),
            first.message))


# -- redaction and digests -------------------------------------------------

def redact_headers(headers: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """Split headers into the retained set and the redacted *names*.

    The retained mapping still carries values so a digest can be taken over the
    redacted canonical block; it is never written into the document.
    """
    retained: dict[str, str] = {}
    redacted: list[str] = []
    for name, value in headers.items():
        text = str(name)
        lowered = text.lower()
        if lowered in REDACTED_HEADER_NAMES or REDACTED_HEADER_PATTERN.search(text):
            if text not in redacted:
                redacted.append(text)
            continue
        retained[text] = str(value)
    return retained, sorted(redacted)


def canonical_header_block(headers: Mapping[str, str]) -> bytes:
    retained, _ = redact_headers(headers)
    lines = sorted("%s: %s\n" % (str(name).lower(), str(value))
                   for name, value in retained.items())
    return "".join(lines).encode("utf-8")


def header_block_sha256(headers: Mapping[str, str]) -> str:
    """SHA-256 over the REDACTED canonical header block."""
    return hashlib.sha256(canonical_header_block(headers)).hexdigest()


def header_names_present(headers: Mapping[str, str]) -> list[str]:
    retained, _ = redact_headers(headers)
    return sorted({str(name) for name in retained})


def body_digests(raw: bytes, *, content_type: str | None) -> dict[str, Any]:
    """Raw-byte digest plus, when the body parses as JSON, its JCS digest."""
    payload = bytes(raw or b"")
    result: dict[str, Any] = {
        "contentType": content_type if content_type else None,
        "bodyByteCount": len(payload),
        "bodyRawSha256": hashlib.sha256(payload).hexdigest(),
    }
    if payload:
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return result
        try:
            result["bodyJcsSha256"] = hashlib.sha256(
                canonicalize_bytes(decoded)).hexdigest()
        except CanonicalizationError:
            return result
    return result


def message_digest(headers: Mapping[str, str], raw: bytes,
                   *, content_type: str | None = None,
                   identifiers: Sequence[tuple[str, str]] = (),
                   ) -> dict[str, Any]:
    """Assemble one ``$defs/messageDigest`` object from wire material.

    ``identifiers`` are the per-run server-assigned identifiers.  When supplied,
    companion digests are computed over the same material with each identifier
    replaced by its role token, which is what makes a two-run comparison able to
    keep the slot under byte comparison instead of exempting it.
    """
    if content_type is None:
        for name, value in headers.items():
            if str(name).lower() == "content-type":
                content_type = str(value)
                break
    record: dict[str, Any] = {
        "headerNamesPresent": header_names_present(headers),
        "headerBlockSha256": header_block_sha256(headers),
    }
    record.update(body_digests(raw, content_type=content_type))
    if identifiers:
        retained, _redacted = redact_headers(headers)
        canonical_headers = {
            name: _canonicalise(str(value).encode("utf-8"), identifiers).decode(
                "utf-8", "replace")
            for name, value in retained.items()
        }
        record["headerBlockCanonicalSha256"] = header_block_sha256(canonical_headers)
        record["bodyCanonicalSha256"] = hashlib.sha256(
            _canonicalise(raw, identifiers)).hexdigest()
    return record


def _canonicalise(raw: bytes, identifiers: Sequence[tuple[str, str]]) -> bytes:
    text = bytes(raw or b"")
    for value, token in identifiers:
        if value:
            text = text.replace(value.encode("utf-8"), token.encode("utf-8"))
    return text


def scan_raw_artifacts(capture_root: Path) -> dict[str, int]:
    """G-SEC-1/G-SEC-2 over the STORED raw bytes, not only over text files.

    The capture keeps raw PM files, NETCONF frames and notification bodies
    verbatim, so a redaction claim is only credible once those bytes have been
    scanned.  The counts are measurements; they are never asserted to be zero.
    """
    root = Path(capture_root) / "raw"
    scanned = 0
    credential_hits = 0
    private_key_hits = 0
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            scanned += 1
            payload = path.read_bytes()
            credential_hits += sum(
                payload.count(marker) for marker in CREDENTIAL_MARKERS)
            private_key_hits += sum(
                payload.count(marker) for marker in PRIVATE_KEY_MARKERS)
    return {
        "filesScanned": scanned,
        "credentialMarkerHits": credential_hits,
        "privateKeyBlockHits": private_key_hits,
    }


# -- clock and raw store ---------------------------------------------------

class ObservedClock:
    """Live-observed UTC clock.

    SC-084 inverts the bilateral rule: reading the wall clock on the request
    path is required here, because ``atMs`` is an earliest-start offset and
    ``atMs: null`` means "immediately after the previous step's outputs are
    captured".  What remains forbidden is a hidden sleep standing in for a
    declared observable event, so every sleep must be declared through
    :meth:`note_sleep`; an undeclared one increments ``hiddenSleepCount``.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._monotonic_origin_ns = int(time.monotonic_ns())
        self._started_at = self._now()
        self._hidden_sleeps = 0
        self._declared_sleeps = 0

    @staticmethod
    def _now() -> str:
        moment = datetime.now(timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + (
            "%03dZ" % (moment.microsecond // 1000))

    def now(self) -> str:
        return self._now()

    def monotonic_offset_ms(self) -> int:
        return max(0, (int(time.monotonic_ns()) - self._monotonic_origin_ns) // 1_000_000)

    def note_sleep(self, *, declared: bool) -> None:
        with self._lock:
            if declared:
                self._declared_sleeps += 1
            else:
                self._hidden_sleeps += 1

    @property
    def monotonic_origin_ns(self) -> int:
        return self._monotonic_origin_ns

    @property
    def started_at(self) -> str:
        return self._started_at

    @property
    def hidden_sleep_count(self) -> int:
        with self._lock:
            return self._hidden_sleeps

    @property
    def declared_sleep_count(self) -> int:
        with self._lock:
            return self._declared_sleeps


class RawStore:
    """Write wire artefacts verbatim under the capture root BEFORE parsing.

    ``put`` returns the ``$defs/rawBytes`` object the schema expects, with the
    digest taken over the bytes actually written; ``truncated`` is a schema
    constant ``false`` because a truncated artefact is not evidence.
    """

    def __init__(self, *, capture_root: Path) -> None:
        self._root = Path(capture_root)
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root

    def put(self, raw: bytes, *, kind: str, name: str,
            media_type: str | None = None) -> dict[str, Any]:
        if kind not in RAW_KINDS:
            raise CaptureError(
                "raw artefact kind must be one of %s" % ", ".join(RAW_KINDS))
        if not _RAW_NAME.match(str(name)):
            raise CaptureError("raw artefact name %r is not a safe file name" % name)
        payload = bytes(raw or b"")
        relative = "raw/%s/%s" % (kind, name)
        target = self._root / relative
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        return {
            "byteCount": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "artifactPath": relative,
            "mediaType": str(media_type) if media_type else None,
            "truncated": False,
        }


# -- the recorder ----------------------------------------------------------

_EMPTY_SNAPSHOT: dict[str, Any] = {
    "jcsSha256": hashlib.sha256(canonicalize_bytes({})).hexdigest(),
    "policyCount": 0,
    "statusCount": 0,
    "dmeDataJobCount": 0,
    "deliveryBindingCount": 0,
    "acceptedPushPayloadCounts": {},
    "subscriptionCount": 0,
    "evidenceCommitCount": 0,
    "retrievedFileCount": 0,
    "perfMetricJobPresent": None,
    "policyIds": [],
}

_EMPTY_NETCONF: dict[str, Any] = {
    "enabled": False,
    "assignment": {
        "adapterActionsAssignedToUpper": [],
        "authorityRecordDigest": None,
        "roleTablePointer": "/initialStates",
    },
    "session": None,
    "rpcs": [],
    "perfMetricJob": None,
    # The §10.2 readiness ledger.  ``None`` means the run never established one,
    # which is a refusal at ``COMPLETED`` rather than a silent pass.
    "readiness": None,
}

_EMPTY_NORMALIZATION: dict[str, Any] = {
    "invocations": 0,
    "parserInvocations": 0,
    "commitEligibleCount": 0,
    "rejectedCount": 0,
    "duplicateCount": 0,
    "records": [],
}

_EMPTY_STUBS: dict[str, Any] = {
    "routing": "UNRESOLVED",
    "kpmSnapshots": [],
    "controlResults": [],
    "readbacks": [],
    "writeLedger": [],
    "normalRanWrites": 0,
    "rollbackRanWrites": 0,
}

_EMPTY_COORDINATOR: dict[str, Any] = {
    "processIntentCallsBefore": 0,
    "processIntentCallsAfter": 0,
    "processIntentCallsDelta": 0,
    "executionMode": "NOT_INVOKED",
    "fsmHistory": [],
    "terminalOutcome": None,
    "terminalOutcomeCount": 0,
    "terminalEvidenceRef": None,
    "ledgerReferences": [],
    "requiresRealCoordinatorExecution": False,
    "syntheticTransitionCount": 0,
    "evidenceRecordsSuppliedFromNormalization": [],
}

RETRY_POLICY = ("declared-timeout-and-backoff only; no implicit retry and no "
                "hidden sleep on the request path")


class CaptureRecorder:
    """The single capture writer.  Every seam validates before it appends."""

    def __init__(self, *, run_id: str, scenario_id: str,
                 envelope: Mapping[str, Any], clock: ObservedClock,
                 raw_store: RawStore) -> None:
        self._lock = threading.RLock()
        self._run_id = str(run_id)
        self._scenario_id = str(scenario_id)
        self._envelope = json.loads(json.dumps(dict(envelope)))
        self._clock = clock
        self._raw_store = raw_store
        self._schema_path = Path(self._envelope.pop(
            "schemaPath", str(default_schema_path())))
        schema_id = str(load_schema(self._schema_path).get("$id", ""))
        self._schema_version = (
            SCHEMA_VERSION_V2 if schema_id == SCHEMA_ID_V2 else SCHEMA_VERSION)
        self._sequence = 0
        self._exchanges: list[dict[str, Any]] = []
        self._harness_operations: list[dict[str, Any]] = []
        self._netconf = json.loads(json.dumps(_EMPTY_NETCONF))
        if self._schema_version == SCHEMA_VERSION_V2:
            self._netconf["measuredSegment"] = None
        self._notifications: list[dict[str, Any]] = []
        self._retrievals: list[dict[str, Any]] = []
        self._normalization = json.loads(json.dumps(_EMPTY_NORMALIZATION))
        self._state = {"before": dict(_EMPTY_SNAPSHOT), "after": dict(_EMPTY_SNAPSHOT),
                       "diffPointers": []}
        self._coordinator = json.loads(json.dumps(_EMPTY_COORDINATOR))
        self._coordinator["requiresRealCoordinatorExecution"] = bool(
            self._envelope.get("requiresRealCoordinatorExecution", False))
        self._coordinator["nowInput"] = clock.now()
        self._stubs = json.loads(json.dumps(_EMPTY_STUBS))
        self._cleanup: dict[str, Any] = {
            "path": "NORMAL", "complete": False, "actions": [], "residual": []}
        self._retry_attempts: list[dict[str, Any]] = []
        self._idempotency_keys: list[str] = []
        self._duplicate_suppressions = 0
        self._applied_rules: list[dict[str, Any]] = []
        self._attempted_authorities: list[str] = []
        self._skew_probes: list[dict[str, Any]] = []
        self._egress: Any = None
        self._gate: Any = None
        self._disposition = "ABORTED_ERROR"
        self._secret_references: list[str] = []
        self._redacted_header_names: list[str] = []
        self._redacted_body_pointers: list[str] = []
        self._assigned_identifiers: dict[str, str] = {}
        self._started_at = clock.started_at

    # -- identity ---------------------------------------------------------
    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def scenario_id(self) -> str:
        return self._scenario_id

    @property
    def clock(self) -> ObservedClock:
        return self._clock

    @property
    def raw_store(self) -> RawStore:
        return self._raw_store

    @property
    def schema_path(self) -> Path:
        return self._schema_path

    def register_assigned_identifier(self, value: Any, role: str) -> None:
        """Declare an identifier this run assigned, so digests can canonicalise."""
        text = str(value or "")
        if len(text) < 8:
            return
        with self._lock:
            self._assigned_identifiers.setdefault(text, "{SERVER_ASSIGNED:%s}" % role)

    def assigned_identifiers(self) -> list[tuple[str, str]]:
        """Longest-first, so one identifier cannot mask a prefix of another."""
        with self._lock:
            return sorted(self._assigned_identifiers.items(),
                          key=lambda item: len(item[0]), reverse=True)

    def bind_egress_guard(self, guard: Any) -> None:
        """Attach the running egress guard; its counters become the capture's."""
        with self._lock:
            self._egress = guard

    # -- writers ----------------------------------------------------------
    def next_sequence(self) -> int:
        with self._lock:
            value = self._sequence
            self._sequence += 1
            return value

    def emit(self, record: Mapping[str, Any]) -> int:
        fragment = self._fragment(record, "#/$defs/exchange")
        with self._lock:
            self._exchanges.append(fragment)
            authority = fragment.get("transport", {}).get("peerAuthority")
            if authority and authority not in self._attempted_authorities:
                self._attempted_authorities.append(authority)
            return int(fragment["sequence"])

    def emit_harness_operation(self, record: Mapping[str, Any]) -> int:
        fragment = self._fragment(record, "#/$defs/harnessOperation")
        with self._lock:
            self._harness_operations.append(fragment)
            return int(fragment["sequence"])

    def emit_netconf_rpc(self, record: Mapping[str, Any]) -> int:
        fragment = self._fragment(
            record, "#/properties/netconf/properties/rpcs/items")
        with self._lock:
            self._netconf["rpcs"].append(fragment)
            return int(fragment["sequence"])

    def emit_notification(self, record: Mapping[str, Any]) -> int:
        fragment = self._fragment(record, "#/properties/notifications/items")
        with self._lock:
            self._notifications.append(fragment)
            return int(fragment["sequence"])

    def emit_retrieval(self, record: Mapping[str, Any]) -> int:
        fragment = self._fragment(record, "#/properties/retrievals/items")
        with self._lock:
            self._retrievals.append(fragment)
            return int(fragment["sequence"])

    def emit_cleanup_action(self, record: Mapping[str, Any]) -> int:
        fragment = self._fragment(
            record, "#/properties/cleanup/properties/actions/items")
        with self._lock:
            self._cleanup["actions"].append(fragment)
            return len(self._cleanup["actions"]) - 1

    def emit_retry_attempt(self, record: Mapping[str, Any]) -> int:
        fragment = self._fragment(
            record, "#/properties/retries/properties/attempts/items")
        with self._lock:
            self._retry_attempts.append(fragment)
            return len(self._retry_attempts) - 1

    def _fragment(self, record: Mapping[str, Any], pointer: str) -> dict[str, Any]:
        fragment = json.loads(json.dumps(dict(record)))
        validate_fragment(fragment, schema_path=self._schema_path, pointer=pointer)
        return fragment

    # -- setters ----------------------------------------------------------
    def set_netconf_session(self, session: Mapping[str, Any] | None) -> None:
        with self._lock:
            self._netconf["session"] = (
                None if session is None
                else json.loads(json.dumps(dict(session))))

    def set_netconf_assignment(self, assignment: Mapping[str, Any]) -> None:
        fragment = json.loads(json.dumps(dict(assignment)))
        with self._lock:
            merged = dict(self._netconf["assignment"])
            merged.update(fragment)
            self._netconf["assignment"] = merged
            self._netconf["enabled"] = bool(
                merged.get("adapterActionsAssignedToUpper"))

    def set_perf_metric_job(self, job: Mapping[str, Any] | None) -> None:
        with self._lock:
            self._netconf["perfMetricJob"] = (
                None if job is None else json.loads(json.dumps(dict(job))))

    def set_o1_readiness(self, readiness: Mapping[str, Any] | None) -> None:
        """Publish the §10.2 readiness ledger as it stands right now.

        The ledger is republished on every transition rather than assembled at
        export time, so a run that dies mid-readiness still exports the exact
        set of postconditions it had confirmed when it died.
        """
        fragment = (None if readiness is None
                    else json.loads(json.dumps(dict(readiness))))
        if fragment is not None:
            validate_fragment(fragment, schema_path=self._schema_path,
                              pointer="#/$defs/o1Readiness")
        with self._lock:
            self._netconf["readiness"] = fragment

    def set_netconf_measured_segment(
            self, segment: Mapping[str, Any] | None) -> None:
        """Bind the measured-body counter boundary to capture schema 2.0.0."""
        if self._schema_version != SCHEMA_VERSION_V2:
            raise CaptureError(
                "a NETCONF measured segment requires capture schema 2.0.0")
        fragment = (None if segment is None
                    else json.loads(json.dumps(dict(segment))))
        if fragment is not None:
            validate_fragment(
                fragment, schema_path=self._schema_path,
                pointer="#/properties/netconf/properties/measuredSegment")
        with self._lock:
            self._netconf["measuredSegment"] = fragment

    def set_normalization(self, normalization: Mapping[str, Any]) -> None:
        fragment = json.loads(json.dumps(dict(normalization)))
        with self._lock:
            merged = dict(self._normalization)
            merged.update(fragment)
            self._normalization = merged

    def set_state(self, *, when: str, snapshot: Mapping[str, Any]) -> None:
        if when not in {"before", "after"}:
            raise CaptureError("state snapshots are 'before' or 'after'")
        fragment = self._fragment(snapshot, "#/$defs/stateSnapshot")
        with self._lock:
            self._state[when] = fragment
            self._state["diffPointers"] = _diff_pointers(
                self._state["before"], self._state["after"])

    def set_coordinator(self, observations: Mapping[str, Any]) -> None:
        with self._lock:
            merged = dict(self._coordinator)
            merged.update(json.loads(json.dumps(dict(observations))))
            self._coordinator = merged

    def set_deterministic_stubs(self, observations: Mapping[str, Any]) -> None:
        with self._lock:
            merged = dict(self._stubs)
            merged.update(json.loads(json.dumps(dict(observations))))
            self._stubs = merged

    def set_cleanup_path(self, *, path: str, complete: bool,
                         residual: Sequence[str]) -> None:
        if path not in {"NORMAL", "FAILURE"}:
            raise CaptureError("cleanup path is NORMAL or FAILURE")
        remaining = [str(item) for item in residual]
        with self._lock:
            self._cleanup["path"] = path
            self._cleanup["residual"] = remaining
            # ``complete`` is MEASURED as ``residual`` being empty; a caller
            # claiming completion with residual entries is refused rather than
            # believed.
            self._cleanup["complete"] = bool(complete) and not remaining

    def set_gate_result(self, result: Any) -> None:
        with self._lock:
            self._gate = result

    def set_disposition(self, disposition: str) -> None:
        allowed = {"COMPLETED", "ABORTED_PREFLIGHT", "ABORTED_GATE",
                   "ABORTED_TIMEOUT", "ABORTED_GUARD", "ABORTED_ERROR"}
        if disposition not in allowed:
            raise CaptureError("unknown run disposition %s" % disposition)
        with self._lock:
            if disposition == "COMPLETED":
                # ``COMPLETED`` is a claim about the whole SC-084 run, and
                # SC-084's mandatory O1 initial states are part of it.  1.0.1
                # could reach this value having established none of them; the
                # recorder now refuses to write it unless its own readiness
                # ledger says every postcondition was confirmed.
                complete, reasons = readiness_is_complete(
                    {"netconf": self._netconf})
                if not complete:
                    raise CaptureError(
                        "a run cannot be dispositioned COMPLETED while the "
                        "O1 readiness lifecycle is unestablished: %s"
                        % ", ".join(reasons))
            self._disposition = disposition

    def add_applied_rule(self, rule_id: str, *, inputs: Sequence[str],
                         observations: Mapping[str, Any]) -> None:
        declared = list(self._envelope.get("declaredRuleIds", []))
        record = {
            "ruleId": str(rule_id),
            "declaredInCatalog": str(rule_id) in declared,
            "inputs": [str(item) for item in inputs],
            "observations": json.loads(json.dumps(dict(observations))),
        }
        with self._lock:
            self._applied_rules = [item for item in self._applied_rules
                                   if item["ruleId"] != record["ruleId"]]
            self._applied_rules.append(record)
            self._applied_rules.sort(key=lambda item: item["ruleId"])

    def note_secret_reference(self, reference: str) -> None:
        with self._lock:
            if reference not in self._secret_references:
                self._secret_references.append(str(reference))

    def note_redacted_headers(self, names: Iterable[str]) -> None:
        with self._lock:
            for name in names:
                if name not in self._redacted_header_names:
                    self._redacted_header_names.append(str(name))

    def note_redacted_body_pointer(self, pointer: str) -> None:
        with self._lock:
            if pointer not in self._redacted_body_pointers:
                self._redacted_body_pointers.append(str(pointer))

    def note_skew_probe(self, *, peer: str, observed_skew_ms: int) -> None:
        with self._lock:
            self._skew_probes.append({
                "at": self._clock.now(), "peer": str(peer),
                "observedSkewMs": int(observed_skew_ms)})

    def note_idempotency_key(self, key: str) -> None:
        with self._lock:
            if key not in self._idempotency_keys:
                self._idempotency_keys.append(str(key))

    def note_duplicate_suppression(self) -> None:
        with self._lock:
            self._duplicate_suppressions += 1

    # -- readers ----------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            exchanges = sorted(self._exchanges, key=lambda item: item["sequence"])
            harness = sorted(self._harness_operations,
                             key=lambda item: item["sequence"])
            high_water = max(
                [item["sequence"] for item in exchanges]
                + [item["sequence"] for item in harness]
                + [item["sequence"] for item in self._netconf["rpcs"]]
                + [item["sequence"] for item in self._notifications]
                + [item["sequence"] for item in self._retrievals]
                + [item["sequence"] for name in (
                    "kpmSnapshots", "controlResults", "readbacks", "writeLedger")
                   for item in self._stubs[name]]
                + [0])
            gate = self._gate
            if gate is None:
                raise CaptureError(
                    "a capture cannot be assembled before the identity/authority "
                    "gate has decided; profile and authority are its output")
            observed_end = self._clock.now()
            try:
                within_window = (
                    _utc_instant(gate.execution_window[0])
                    <= _utc_instant(self._started_at)
                    <= _utc_instant(observed_end)
                    <= _utc_instant(gate.execution_window[1]))
            except (TypeError, ValueError) as exc:
                raise CaptureError(
                    "the admitted execution window is unreadable: %s" % exc) from None
            document = {
                "schemaVersion": self._schema_version,
                "captureId": str(uuid.uuid5(
                    _CAPTURE_NAMESPACE, self._run_id + "|" + self._scenario_id)),
                "notAVerdict": True,
                "oracleOwnership": ORACLE_OWNERSHIP,
                "oracleSource": dict(ORACLE_SOURCE),
                "profile": gate.as_capture_profile(),
                "run": {
                    "runId": self._run_id,
                    "scenarioId": self._scenario_id,
                    "startedAt": self._started_at,
                    "endedAt": observed_end,
                    "sequenceHighWaterMark": high_water,
                    "orderingRule": "MONOTONIC_OBSERVED_THEN_ARRAY_ORDER",
                    "stepIndexBase": 0,
                    "executionWindow": {
                        "approvedNotBefore": gate.execution_window[0],
                        "approvedNotAfter": gate.execution_window[1],
                        "withinWindow": within_window,
                    },
                    "disposition": self._disposition,
                },
                "revisions": self._envelope["revisions"],
                "contract": self._envelope["contract"],
                "provider": self._envelope["provider"],
                "authority": gate.as_capture_authority(),
                "deployment": self._envelope["deployment"],
                "scenario": self._envelope["scenario"],
                "clock": {
                    "mode": "LIVE_OBSERVED",
                    "source": "SYSTEM_UTC",
                    "monotonicOriginNs": self._clock.monotonic_origin_ns,
                    "observedStart": self._started_at,
                    "observedEnd": observed_end,
                    "skewProbes": list(self._skew_probes),
                    "orderingViolations": _ordering_violations(exchanges),
                    "hiddenSleepCount": self._clock.hidden_sleep_count,
                },
                "exchanges": exchanges,
                "harnessOperations": harness,
                "netconf": self._netconf,
                "notifications": sorted(self._notifications,
                                        key=lambda item: item["sequence"]),
                "retrievals": sorted(self._retrievals,
                                     key=lambda item: item["sequence"]),
                "normalization": self._normalization,
                "state": self._state,
                "coordinator": self._coordinator,
                "deterministicStubs": self._stubs,
                "cleanup": self._cleanup,
                "retries": {
                    "policy": str(self._envelope.get("retryPolicy", RETRY_POLICY)),
                    "attempts": list(self._retry_attempts),
                    "idempotencyKeysUsed": sorted(self._idempotency_keys),
                    "duplicateSuppressions": self._duplicate_suppressions,
                },
                "externalCalls": self._external_calls(),
                "appliedRules": self._applied_rules,
                "redaction": {
                    "policy": REDACTION_POLICY,
                    "redactedHeaderNames": sorted(self._redacted_header_names),
                    "redactedBodyPointers": sorted(self._redacted_body_pointers),
                    "secretReferencesObserved": sorted(self._secret_references),
                    "secretValuesCaptured": False,
                    "credentialMaterialCaptured": False,
                    "rawArtifactScan": scan_raw_artifacts(self._raw_store.root),
                },
            }
            return document

    def _external_calls(self) -> dict[str, Any]:
        """Read the counters off the guard; never write a literal zero.

        With no guard bound there is nothing to report, and a document that
        printed zeros it did not measure would be worse than no document, so the
        assembly fails closed instead.
        """
        guard = self._egress
        if guard is None:
            raise CaptureError(
                "no egress guard is bound to this recorder; the external-call "
                "counters would be claims rather than measurements")
        observed = guard.observations(scope="scenario")
        attempted = sorted(set(self._attempted_authorities)
                           | set(observed["attemptedAuthorities"]))
        return {
            "targetLedger": [dict(item) for item in observed["targetLedger"]],
            "hardwareCalls": int(observed["hardwareCalls"]),
            "externalLiveTargetCalls": int(observed["externalLiveTargetCalls"]),
            "forbiddenEgressAttempts": int(observed["forbiddenEgressAttempts"]),
            "attemptedAuthorities": attempted,
            "authorityAllowlist": list(self._envelope["authorityAllowlist"]),
            "guardInstalled": bool(observed["guardInstalled"]),
            "guardArmedNow": bool(observed["guardArmedNow"]),
            "guardMethods": list(observed["guardMethods"]),
            "scope": "scenario",
            "hardwareDefinitionSource": str(observed["hardwareDefinitionSource"]),
            "connectionAttempts": int(observed["connectionAttempts"]),
            "approvedConnectionAttempts": int(observed["approvedConnectionAttempts"]),
            "violations": [dict(item) for item in observed["violations"]],
            "peerAttributionAmbiguities": [
                dict(item) for item in observed["peerAttributionAmbiguities"]],
        }

    def export(self, path: Path) -> str:
        """Validate, write deterministically and return the byte digest."""
        document = self.snapshot()
        validate_capture(document, schema_path=self._schema_path,
                         capture_root=self._raw_store.root)
        payload = (json.dumps(document, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":")) + "\n").encode("utf-8")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return hashlib.sha256(payload).hexdigest()


def _ordering_violations(exchanges: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One entry per pair whose recorded order contradicts its timestamps."""
    violations: list[dict[str, Any]] = []
    previous: Mapping[str, Any] | None = None
    for record in exchanges:
        if previous is not None:
            earlier = str(previous.get("observedAt", ""))
            later = str(record.get("observedAt", ""))
            if earlier and later and later < earlier:
                violations.append({
                    "earlierRef": "/exchanges/%d" % int(previous["sequence"]),
                    "laterRef": "/exchanges/%d" % int(record["sequence"]),
                    "deltaMs": int(record.get("monotonicOffsetMs", 0))
                    - int(previous.get("monotonicOffsetMs", 0)),
                })
        previous = record
    return violations


def _diff_pointers(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    pointers: list[str] = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            pointers.append("/" + str(key).replace("~", "~0").replace("/", "~1"))
    return pointers
