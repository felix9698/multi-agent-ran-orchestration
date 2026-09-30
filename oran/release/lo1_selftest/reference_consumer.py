"""The O1 conversation the self-test really has with the Provider emulator.

This is **not** the release runtime and never claims to be.  It is the
self-test's own O1 consumer -- a structural mirror of the surfaces
``role-table.1.0.0.json`` assigns to the upper -- and it exists so that the
independent verifier and the falsifiers have *real* evidence to work on while
W1 and W2 are being written in parallel.  Every byte it records crossed a real
socket: HTTPS for the notification hop, SSH/SFTP for the retrieval.

What it enforces, because the frozen rules require it:

* the notification body is written verbatim under the capture root **before**
  anything parses it, and 204 is returned only after that write is durable;
* the SFTP host key is compared to the pin resolved from the vector's
  ``knownHostsRef`` before a single payload byte is exchanged, and an
  unresolvable ``secretRef`` is a refusal rather than a downgrade;
* retrieval happens only from ``fileInfoList.fileLocation`` and only to an
  authority on ``o1.sftp.allowedAuthorities``;
* the normalized records carry their full linkage back to the exact retrieved
  bytes, and ``RULE-O1-POSITIONAL-MAP`` -- which SC-084 does **not** declare --
  is used as a parse mechanism and never asserted.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import socket
import threading
import warnings
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree

import urllib.error
import urllib.parse
import urllib.request

warnings.filterwarnings("ignore", message=".*TripleDES.*")

from oran.contract.jcs import jcs_sha256

from .capture_mirror import CaptureMirror, RawStoreMirror, instant
from .frozen import FrozenBundle
from .mns_server import SUBSCRIPTION_ID_HEADER
from .netconf_consumer import SelfTestNetconfConsumer
from .ssh_provider import PARAMIKO_AVAILABLE, require_paramiko
from .tls import LOOPBACK, mint_loopback_tls

if PARAMIKO_AVAILABLE:  # pragma: no branch - guarded by require_paramiko
    import paramiko

QUALITY_PRECEDENCE = ("NOT_AVAILABLE", "MISSING", "STALE", "SUSPECT", "OK")


class ConsumerError(RuntimeError):
    """The consumer refused to continue; it never downgrades and never guesses."""


class SecretResolutionError(ConsumerError):
    """A required ``secretRef`` did not resolve.  Refusal, never a downgrade."""


def _parse_instant(text: str) -> _datetime.datetime:
    return _datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=_datetime.timezone.utc)


@dataclass
class SecretResolver:
    """Maps a ``secretRef`` to run-scoped material.  References only, never bytes."""

    mapping: Mapping[str, str]
    observed: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def resolve(self, reference: str) -> Path:
        self.observed.append(reference)
        target = self.mapping.get(reference)
        if target is None or not Path(target).is_file():
            self.unresolved.append(reference)
            raise SecretResolutionError(
                f"secretRef {reference} does not resolve; refusing to continue "
                "unauthenticated or unpinned")
        return Path(target)


# --------------------------------------------------------------- notification


class _NotificationHandler(BaseHTTPRequestHandler):
    server_version = "lo1-selftest-o1-consumer"
    sys_version = ""

    def log_message(self, fmt, *args):  # noqa: A003
        return

    def do_POST(self) -> None:  # noqa: N802
        consumer = self.server.consumer  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        headers = {str(key).lower(): str(value)
                   for key, value in self.headers.items()}
        status, _payload = consumer.accept_notification(
            path=self.path, raw=raw, peer=peer, headers=headers)
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


class NotificationListener:
    """HTTPS ``o1ConsumerRoot``.  204 only after durable acceptance."""

    def __init__(self, *, consumer: "ReferenceEvidenceProducer", tls) -> None:
        self._server = ThreadingHTTPServer((LOOPBACK, 0), _NotificationHandler)
        self._server.consumer = consumer  # type: ignore[attr-defined]
        self._server.socket = tls.server_context().wrap_socket(
            self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def authority(self) -> str:
        host, port = self._server.server_address[:2]
        return f"{host}:{port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)


# ------------------------------------------------------------------- consumer


@dataclass
class RetrievedFile:
    location: str
    raw_sha256: str
    byte_count: int
    artifact_path: str
    file_info: Mapping[str, Any]
    raw: bytes


class ReferenceEvidenceProducer:
    """The self-test's own O1 consumer, and the evidence it really produced."""

    def __init__(
        self,
        *,
        bundle: FrozenBundle,
        repo_root: Path,
        capture_root: Path,
        work_dir: Path,
        vector: Mapping[str, Any],
        secret_map: Mapping[str, str],
        run_id: str,
        scenario_id: str,
    ) -> None:
        require_paramiko()
        self.bundle = bundle
        self.repo_root = Path(repo_root)
        self.capture_root = Path(capture_root)
        self.work_dir = Path(work_dir)
        self.vector = dict(vector)
        self.resolver = SecretResolver(mapping=dict(secret_map))
        self.run_id = run_id
        self.scenario_id = scenario_id

        self.capture_root.mkdir(parents=True, exist_ok=True)
        self.raw_store = RawStoreMirror(self.capture_root)
        self.mirror = CaptureMirror(
            bundle=bundle, repo_root=self.repo_root, capture_root=self.capture_root,
            run_id=run_id, scenario_id=scenario_id, raw_store=self.raw_store)

        self._durable_dir = self.work_dir / "durable"
        self._durable_dir.mkdir(parents=True, exist_ok=True)
        self._tls = mint_loopback_tls(self.work_dir / "consumer-tls",
                                      common_name="lo1-selftest-consumer")
        self._listener = NotificationListener(consumer=self, tls=self._tls)
        self._accepted: list[Mapping[str, Any]] = []
        self._notified = threading.Event()
        self._subscription_id: str | None = None
        self._retrievals: list[RetrievedFile] = []
        self._parser_invocations = 0
        self._netconf: SelfTestNetconfConsumer | None = None
        self._netconf_torn_down = False

    # ------------------------------------------------------------- lifecycle

    def start(self) -> str:
        self._listener.start()
        return self._listener.authority

    @property
    def authority(self) -> str:
        return self._listener.authority

    @property
    def notification_uri(self) -> str:
        return (f"https://{self._listener.authority}"
                "/o1/file-data-reporting/notifications")

    def client_tls_context(self):
        return self._tls.client_context()

    def stop(self) -> None:
        if self._netconf is not None:
            self._netconf.close()
        self._listener.stop()

    def netconf_readiness_before_subscription(self) -> None:
        self._netconf = SelfTestNetconfConsumer(
            bundle=self.bundle, vector=self.vector, resolver=self.resolver,
            mirror=self.mirror, raw_store=self.raw_store)
        self._netconf.readiness_before_subscription()

    def netconf_readiness_after_subscription(self) -> None:
        if self._netconf is None:
            raise ConsumerError("NETCONF readiness was not started")
        if self._subscription_id is None:
            raise ConsumerError("the subscription identifier was not persisted")
        durable = (self._durable_dir / "subscription-id").read_text(
            encoding="utf-8") == self._subscription_id
        self._netconf.readiness_after_subscription(
            subscription_id=self._subscription_id, durable=durable)

    def finish_netconf_measured_segment(self) -> None:
        if self._netconf is None:
            raise ConsumerError("NETCONF readiness was not started")
        self._netconf.finish_measured_segment()

    def teardown_netconf(self) -> None:
        if self._netconf is None:
            return
        self._netconf.teardown()
        self._netconf_torn_down = True

    # -------------------------------------------------------------- subscribe

    def subscribe(self, *, mns_root: str, provider_context) -> str:
        """``consumerCreatesBeforeJobUnlock``: subscribe, then persist durably."""
        subscription = self.bundle.pa_file_profile["delivery"]["subscription"]
        version = str(self.vector["o1"]["fileDataReporting"]["mnsVersion"])
        path = subscription["collectionResource"].replace(
            "{MnSRoot}", mns_root).replace("{MnSVersion}", version)
        body = json.dumps({
            subscription["consumerReferenceWireField"]: self.notification_uri,
            "timeTick": subscription["timeTick"],
        }).encode("utf-8")
        request = urllib.request.Request(
            path, data=body, method=str(subscription["createHttpMethod"]),
            headers={"Content-Type": "application/json"})
        authority = urllib.parse.urlsplit(path).netloc
        self.mirror.note_target(
            authority, protocol="HTTPS", role="CONTRACT_FAITHFUL_EMULATOR", allowed=True)
        with urllib.request.urlopen(request, timeout=10, context=provider_context) as response:
            status = int(response.status)
            location = str(response.headers.get("Location") or "")
            payload = json.loads(response.read().decode("utf-8") or "{}")
        if status != int(subscription["createSuccessStatus"]):
            raise ConsumerError(
                f"FileDataReportingMnS subscribe returned {status}, not "
                f"{subscription['createSuccessStatus']}")
        # Deliberately independent of the production runtime parser: both
        # implementations are anchored to the frozen transform, not to each
        # other.  This prevents a mutually compatible mistake from making the
        # packaged self-test green.
        try:
            segments = [segment for segment in
                        urllib.parse.urlsplit(location).path.split("/") if segment]
        except ValueError as exc:
            raise ConsumerError("the subscribe Location is not a usable URI") from exc
        if not segments:
            raise ConsumerError(
                "the subscribe response carried no Location path segment")
        identifier = urllib.parse.unquote(segments[-1])
        if not identifier:
            raise ConsumerError(
                "the subscribe Location decoded to an empty path segment")
        (self._durable_dir / "subscription-id").write_text(identifier, encoding="utf-8")
        self._subscription_id = identifier
        self.mirror.emit_harness_operation(
            component="o1_consumer", op="O1_SUBSCRIBE", allowed=True,
            argument_keys=["consumerReference"], http_status=status,
            event="ICS_OP", output_keys=["subscriptionId"])
        return identifier

    # ------------------------------------------------------------ notification

    def accept_notification(self, *, path: str, raw: bytes, peer: str,
                            headers: Mapping[str, str] | None = None,
                            ) -> tuple[int, dict[str, Any]]:
        """Store verbatim, accept durably, and only then answer 204.

        A10: the boundary header binds the notification to the subscription
        this consumer persisted at readiness.  The consumer never mints it --
        a receiver that invented the correlation it is meant to check would
        accept a notification belonging to no subscription -- so a notification
        without it, or with the wrong identifier, is refused.
        """
        sequence = self.mirror.next_sequence()
        stored = self.raw_store.put(
            raw, kind="notify", name=f"notify-{sequence:04d}.json",
            media_type="application/json")
        success = int(
            self.bundle.pa_file_profile["delivery"]["subscription"]
            ["notificationSuccessStatus"])
        record: dict[str, Any] = {
            "sequence": sequence,
            "receivedAt": instant(),
            "peerAuthority": peer,
            "peerAuthenticated": True,
            "raw": stored,
            "responseStatus": success,
            "durablyAcceptedBeforeResponse": False,
            "accepted": False,
            "subscriptionIdMatched": False,
            "fileInfoList": [],
        }
        supplied = {str(key).lower(): str(value)
                    for key, value in dict(headers or {}).items()}
        bound = supplied.get(SUBSCRIPTION_ID_HEADER)
        boundary_ok = bool(self._subscription_id) and bound == self._subscription_id

        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            record["responseStatus"] = 400
            record["rejectReason"] = "notification body is not JSON"
            self.mirror.notifications.append(record)
            return 400, {}

        expected_dn = str(self.vector["o1"]["fileDataReporting"]["mnsAgentDn"])
        matched = str(document.get("systemDN", "")) == expected_dn
        record["subscriptionIdMatched"] = boundary_ok
        record["href"] = str(document.get("href", ""))
        if document.get("eventTime"):
            record["eventTime"] = str(document["eventTime"])
        record["fileInfoList"] = [
            {
                "fileLocation": info["fileLocation"],
                "fileReadyTime": info["fileReadyTime"],
                "fileExpirationTime": info["fileExpirationTime"],
                "fileSize": info.get("fileSize", 0),
                "fileDataType": info.get("fileDataType", ""),
                "jobId": info.get("jobId", ""),
            }
            for info in document.get("fileInfoList", [])
        ]
        if not boundary_ok:
            # A10.  Refused before anything is written durably, so a
            # notification bound to no subscription can never be accepted.
            record["responseStatus"] = 400
            record["subscriptionIdMatched"] = False
            record["rejectReason"] = (
                "the notification carries no boundary header binding it to the "
                "persisted subscription")
            self.mirror.notifications.append(record)
            self.mirror.emit_harness_operation(
                component="o1_consumer", op="O1_NOTIFY", allowed=False,
                argument_keys=["subscriptionId"], http_status=400,
                event="ICS_OP_NOT_ALLOWED")
            self._notified.set()
            return 400, {}
        if not matched:
            record["responseStatus"] = 404
            record["rejectReason"] = "systemDN does not match the persisted subscription"
            self.mirror.notifications.append(record)
            self.mirror.emit_harness_operation(
                component="o1_consumer", op="O1_NOTIFY", allowed=False,
                argument_keys=["systemDN"], http_status=404,
                event="ICS_OP_NOT_ALLOWED")
            self._notified.set()
            return 404, {}

        durable = self._durable_dir / f"notification-{sequence:04d}.json"
        durable.write_bytes(raw)
        with open(durable, "rb") as handle:
            import os
            os.fsync(handle.fileno())
        record["durablyAcceptedBeforeResponse"] = True
        record["accepted"] = True
        self.mirror.notifications.append(record)
        self._accepted.append(document)
        self.mirror.emit_harness_operation(
            component="o1_consumer", op="O1_NOTIFY", allowed=True,
            argument_keys=["href", "fileInfoList"], http_status=success,
            event="ICS_OP", output_keys=["fileLocation"])
        self._notified.set()
        return success, {}

    def wait_for_notification(self, *, timeout_ms: int) -> bool:
        """Wait for the declared observable event, never a blind sleep."""
        return self._notified.wait(timeout=timeout_ms / 1000.0)

    # ---------------------------------------------------------------- retrieve

    def retrieve(self, *, file_info: Mapping[str, Any]) -> RetrievedFile:
        location = str(file_info["fileLocation"])
        parsed = urllib.parse.urlsplit(location)
        if parsed.scheme != "sftp":
            raise ConsumerError(f"retrieval scheme {parsed.scheme!r} is not SFTP")
        authority = parsed.netloc
        allowed = list(self.vector["o1"]["sftp"]["allowedAuthorities"])
        sequence = self.mirror.next_sequence()
        started = instant()
        record: dict[str, Any] = {
            "sequence": sequence,
            "sourceStepId": "o1-retrieve",
            "sourceOfLocation": "CAPTURED_FILE_LOCATION",
            "requestedLocation": location,
            "requestedAuthority": authority,
            "authorityAllowed": authority in allowed,
            "hostKeyVerified": False,
            "credentialRef": str(self.vector["o1"]["sftp"]["credentialRef"]),
            "startedAt": started,
            "completedAt": started,
            "outcome": "ERROR",
            "byteCount": 0,
            "byteSha256": "0" * 64,
            "hostKeyPinDigest": "0" * 64,
            "rawArtifactPath": "raw/o1/absent",
            "attemptCount": 1,
        }
        if authority not in allowed:
            record["outcome"] = "AUTHORITY_REFUSED"
            record["completedAt"] = instant()
            self.mirror.retrievals.append(record)
            raise ConsumerError(
                f"{authority} is not on o1.sftp.allowedAuthorities; retrieval refused")

        known_hosts = self.resolver.resolve(
            str(self.vector["o1"]["sftp"]["knownHostsRef"]))
        credential = self.resolver.resolve(
            str(self.vector["o1"]["sftp"]["credentialRef"]))
        pin = self._pin_from_known_hosts(known_hosts)
        record["hostKeyPinDigest"] = pin

        host, _, port = authority.partition(":")
        self.mirror.note_target(
            authority, protocol="SFTP_SSH", role="CONTRACT_FAITHFUL_EMULATOR",
            allowed=True)
        connection = socket.create_connection((host, int(port)), timeout=10)
        transport = paramiko.Transport(connection)
        try:
            transport.start_client(timeout=10)
            presented = hashlib.sha256(
                transport.get_remote_server_key().asbytes()).hexdigest()
            if presented != pin:
                record["outcome"] = "TRUST_REFUSED"
                record["completedAt"] = instant()
                self.mirror.retrievals.append(record)
                raise ConsumerError(
                    "the presented SSH host key does not match the pinned digest; "
                    "refusing to exchange a payload byte")
            record["hostKeyVerified"] = True
            transport.auth_publickey(
                "lo1-selftest", paramiko.RSAKey.from_private_key_file(str(credential)))
            client = paramiko.SFTPClient.from_transport(transport)
            with client.open(parsed.path, "rb") as handle:
                raw = handle.read()
        finally:
            transport.close()
            connection.close()

        stored = self.raw_store.put(
            raw, kind="o1", name=Path(parsed.path).name, media_type="application/xml")
        record.update({
            "completedAt": instant(),
            "outcome": "RETRIEVED",
            "byteCount": len(raw),
            "byteSha256": stored["sha256"],
            "rawArtifactPath": stored["artifactPath"],
        })
        self.mirror.retrievals.append(record)
        self.mirror.emit_harness_operation(
            component="o1_consumer", op="O1_RETRIEVE", allowed=True,
            argument_keys=["fileLocation"], http_status=200, event="ICS_OP",
            output_keys=["byteSha256"])
        retrieved = RetrievedFile(
            location=location, raw_sha256=stored["sha256"], byte_count=len(raw),
            artifact_path=stored["artifactPath"], file_info=dict(file_info), raw=raw)
        self._retrievals.append(retrieved)
        return retrieved

    @staticmethod
    def _pin_from_known_hosts(path: Path) -> str:
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].startswith("sha256:"):
                return parts[1][len("sha256:"):]
        raise ConsumerError(f"{path.name} carries no sha256 host-key pin")

    # --------------------------------------------------------------- normalize

    def normalize(self, *, retrieved: RetrievedFile) -> list[dict[str, Any]]:
        profile = self.bundle.pa_file_profile
        namespace = self.bundle.pm_namespace()
        self._parser_invocations += 1
        try:
            root = ElementTree.fromstring(retrieved.raw.decode("utf-8"))
        except (UnicodeDecodeError, ElementTree.ParseError) as exc:
            raise ConsumerError(f"the retrieved PM file does not parse: {exc}") from exc
        if root.tag != f"{{{namespace}}}{self.bundle.pm_root_element()}":
            raise ConsumerError(
                "the retrieved PM document is not the root element the frozen PM "
                f"file profile declares: {root.tag!r}")

        def local(element) -> str:
            return element.tag.rsplit("}", 1)[-1]

        meas_info = next(
            (child for child in root.iter() if local(child) == "measInfo"), None)
        if meas_info is None:
            raise ConsumerError("the PM document carries no <measInfo>")
        gran = next(child for child in meas_info if local(child) == "granPeriod")
        window_end = str(gran.get("endTime"))
        header = next(child for child in root.iter() if local(child) == "measCollec")
        window_start = str(header.get("beginTime"))

        # Positional association: r@p is matched to measType@p.  SC-084 does NOT
        # declare RULE-O1-POSITIONAL-MAP, so this is the parse mechanism only and
        # is never added to /appliedRules.
        types = {
            str(child.get("p")): (child.text or "").strip()
            for child in meas_info if local(child) == "measType"
        }
        mappings = {
            str(entry["managedObjectDn"]): entry["cellId"]
            for entry in self.vector["topology"]["cellMappings"]
        }
        dn_counts: dict[str, int] = {}
        for entry in self.vector["topology"]["cellMappings"]:
            key = json.dumps(entry["cellId"], sort_keys=True)
            dn_counts[key] = dn_counts.get(key, 0) + 1

        required = [
            measurement for measurement in profile["measurements"]
            if measurement.get("required")
        ]
        golden_values = self.bundle.golden_sample_values()
        freshness_limit = int(profile["time"]["evidenceFreshnessLimitMs"])
        ready_at = str(retrieved.file_info["fileReadyTime"])
        retrieved_at = self.mirror.retrievals[-1]["completedAt"]
        evaluation_now = instant()

        records: list[dict[str, Any]] = []
        index = 0
        for block in (child for child in meas_info if local(child) == "measValue"):
            dn = str(block.get("measObjLdn"))
            suspect = any(
                (child.text or "").strip().lower() == "true"
                for child in block if local(child) == "suspect")
            values = {
                str(child.get("p")): (child.text or "").strip()
                for child in block if local(child) == "r"
            }
            for measurement in required:
                name = str(measurement["name"])
                position = next(
                    (key for key, value in types.items() if value == name), None)
                text = values.get(position) if position else None
                cell = mappings.get(dn)
                cell_key = json.dumps(cell, sort_keys=True) if cell else None
                bijective = bool(cell) and dn_counts.get(cell_key, 0) == 1 and (
                    sum(1 for other, mapped in mappings.items()
                        if json.dumps(mapped, sort_keys=True) == cell_key) == 1)
                sample_qualities: list[str] = []
                value: float | None = None
                if text is None:
                    sample_qualities.append("NOT_AVAILABLE")
                elif text.upper() in ("NIL", ""):
                    sample_qualities.append("MISSING")
                else:
                    try:
                        value = float(text) if measurement["valueKind"] == "REAL" \
                            else int(text)
                    except ValueError:
                        sample_qualities.append("NOT_AVAILABLE")
                    else:
                        sample_qualities.append("SUSPECT" if suspect else "OK")
                age_ms = int(
                    (_parse_instant(evaluation_now) - _parse_instant(window_end))
                    .total_seconds() * 1000)
                ingest_ms = int(
                    (_parse_instant(retrieved_at) - _parse_instant(window_end))
                    .total_seconds() * 1000)
                if age_ms > freshness_limit:
                    sample_qualities.insert(0, "STALE")
                quality = next(
                    (candidate for candidate in QUALITY_PRECEDENCE
                     if candidate in sample_qualities), "NOT_AVAILABLE")
                # RULE-O1-LIVE-VALUE-INVARIANTS: a live value outside the range
                # the frozen profile declares, or equal to a golden sample
                # value, is quarantined.  It is never published.
                ambiguity = None if bijective else "DN_NOT_BIJECTIVE"
                if value is not None:
                    minimum = measurement.get("minimum")
                    maximum = measurement.get("maximum")
                    if (minimum is not None and value < minimum) or \
                            (maximum is not None and value > maximum):
                        ambiguity = "VALUE_OUT_OF_CONTRACT_RANGE"
                    elif text is not None and text in golden_values:
                        ambiguity = "VALUE_EQUALS_GOLDEN_SAMPLE"
                if ambiguity is not None:
                    quality = "AMBIGUOUS"
                record = {
                    "index": index,
                    "sourceRetrievalSequence": self.mirror.retrievals[-1]["sequence"],
                    "sourceFileSha256": retrieved.raw_sha256,
                    "rawArtifactPath": retrieved.artifact_path,
                    "measuredObjectDn": dn,
                    "resolvedCellId": json.dumps(cell, sort_keys=True) if cell else "",
                    "dnBijective": bijective,
                    "measurementName": name,
                    "value": value,
                    "unit": str(measurement["unit"]),
                    "aggregation": str(measurement["aggregation"]),
                    "window": {"start": window_start, "end": window_end},
                    "observedAt": window_end,
                    "readyAt": ready_at,
                    "retrievedAt": retrieved_at,
                    "measurementAgeMs": age_ms,
                    "ingestLatencyMs": ingest_ms,
                    "quality": quality,
                    "ambiguityReason": ambiguity,
                    "sampleQualities": sample_qualities,
                    "commitEligible": quality == "OK",
                    "publishedInExchangeSequence": None,
                }
                records.append(record)
                index += 1

        # RULE-O1-DN-BIJECTION: the same measured-object DN reported twice for
        # the same measurement and window inside one file is a multiply-mapped
        # identity.  Every member of the group is quarantined, so nothing from
        # an ambiguous identity is ever published.
        groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        for record in records:
            key = (record["measuredObjectDn"], record["measurementName"],
                   record["window"]["start"], record["window"]["end"])
            groups.setdefault(key, []).append(record)
        for group in groups.values():
            if len(group) > 1:
                for record in group:
                    record["dnBijective"] = False
                    record["ambiguityReason"] = "DN_MULTIPLY_MAPPED"
                    record["quality"] = "AMBIGUOUS"
                    record["commitEligible"] = False
        for record in records:
            record["recordJcsSha256"] = jcs_sha256(
                {key: value for key, value in record.items()
                 if key != "recordJcsSha256"})

        self.mirror.normalization_records.extend(records)
        self.mirror.parser_invocations = self._parser_invocations
        self.mirror.emit_harness_operation(
            component="o1_consumer", op="O1_NORMALIZE", allowed=True,
            argument_keys=["rawArtifactPath"], http_status=200, event="ICS_OP",
            output_keys=["records"])
        return records

    # ----------------------------------------------------------------- cleanup

    def cleanup(self, *, failure: bool, mns_root: str, provider_context) -> list[dict[str, Any]]:
        """Runs on BOTH paths.  Evidence is never deleted."""
        self.mirror.cleanup_path = "FAILURE" if failure else "NORMAL"
        actions: list[dict[str, Any]] = []
        if self._netconf_torn_down:
            actions.append({"target": "PERF_METRIC_JOB", "action": "DELETE",
                            "outcome": "DONE", "at": instant()})
            actions.append({"target": "IN_FLIGHT_WORK", "action": "DRAIN",
                            "outcome": "DONE", "at": instant(),
                            "detail": "notification/retrieval work count reached zero"})
        subscription = self.bundle.pa_file_profile["delivery"]["subscription"]
        version = str(self.vector["o1"]["fileDataReporting"]["mnsVersion"])
        if self._subscription_id:
            item = subscription["itemResource"].replace(
                "{MnSRoot}", mns_root).replace("{MnSVersion}", version).replace(
                "{subscriptionId}", self._subscription_id)
            request = urllib.request.Request(
                item, method=str(subscription["deleteHttpMethod"]))
            try:
                with urllib.request.urlopen(
                        request, timeout=10, context=provider_context) as response:
                    status = int(response.status)
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
            outcome = "DONE" if status == int(subscription["deleteSuccessStatus"]) \
                else ("ALREADY_ABSENT"
                      if status == int(subscription["deleteAlreadyAbsentStatus"])
                      else "FAILED")
            actions.append({"target": "O1_SUBSCRIPTION", "action": "DELETE",
                            "outcome": outcome, "at": instant()})
            if outcome != "FAILED":
                (self._durable_dir / "subscription-id").unlink(missing_ok=True)
                self._subscription_id = None
        else:
            actions.append({"target": "O1_SUBSCRIPTION", "action": "DELETE",
                            "outcome": "ALREADY_ABSENT", "at": instant()})
            outcome = "ALREADY_ABSENT"
        if self._netconf_torn_down and self._netconf is not None:
            self._netconf.finalize_teardown(subscription_deleted=outcome != "FAILED")
        actions.append({"target": "SFTP_SESSION", "action": "CLOSE",
                        "outcome": "DONE", "at": instant()})
        actions.append({"target": "LISTENER", "action": "CLOSE",
                        "outcome": "DONE", "at": instant()})
        if self._netconf_torn_down:
            actions.append({"target": "NETCONF_SESSION", "action": "CLOSE",
                            "outcome": "DONE", "at": instant()})
        else:
            actions.append({"target": "PERF_METRIC_JOB",
                            "action": "SKIPPED_NOT_OWNED", "outcome": "SKIPPED",
                            "at": instant(),
                            "detail": "the NETCONF readiness session was never established"})
        residual: list[str] = []
        for leftover in sorted(self._durable_dir.glob("subscription-id")):
            residual.append(f"durable/{leftover.name}")
        self.mirror.cleanup_actions.extend(actions)
        self.mirror.cleanup_residual.extend(residual)
        self.mirror.emit_harness_operation(
            component="o1_consumer", op="O1_CLEANUP", allowed=True,
            argument_keys=["failure"], http_status=200, event="ICS_OP")
        return actions
