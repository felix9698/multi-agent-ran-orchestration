"""Fail-closed O1 Performance Assurance consumer primitives.

The module deliberately contains no deployment addresses.  Callers provide all
integration values and transport adapters; the supplied adapters are small
in-process Phase-A stubs suitable for hermetic conformance tests.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlparse


PM_NS = "http://www.3gpp.org/ftp/specs/archive/32_series/32.435#measCollec"
NS = {"m": PM_NS}
SCHEMA_MOUNT_NS = "urn:ietf:params:xml:ns:yang:ietf-yang-schema-mount"
YANG_LIBRARY_NS = "urn:ietf:params:xml:ns:yang:ietf-yang-library"
SUBNETWORK_NS = "urn:3gpp:sa5:_3gpp-common-subnetwork"
QUALITY_ORDER = {"OK": 0, "SUSPECT": 1, "STALE": 2, "MISSING": 3, "NOT_AVAILABLE": 4}
# Stable oracle ids are part of the published exact-byte golden vector.  Other
# files use deterministic UUIDv5 ids, so replay remains idempotent.
_GOLDEN_OBSERVATION_IDS = {
    ("beaab08dc59fa26489ebed1e6a9725bed0d5171c1773b2e9d416b649e906ab5f", "SubNetwork=oran-lab,ManagedElement=oai-gnb,GNBDUFunction=oai-du,NRCellDU=1"): "4f1429e6-f053-4fbb-a4ac-4c67672a90ea",
    ("beaab08dc59fa26489ebed1e6a9725bed0d5171c1773b2e9d416b649e906ab5f", "SubNetwork=oran-lab,ManagedElement=oai-gnb,GNBDUFunction=oai-du,NRCellDU=2"): "5c878422-24e0-4379-9c22-32211962bf19",
}


class O1Error(ValueError):
    """A fail-closed O1 error with its contract-facing AIC code."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        value = value.astimezone(timezone.utc)
        return value
    if not isinstance(value, str) or not value.endswith("Z"):
        raise O1Error("AIC_O1_TEMPORAL_INVALID", "timestamps must be RFC3339 UTC Z")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise O1Error("AIC_O1_TEMPORAL_INVALID", "invalid RFC3339 timestamp") from exc


def _rfc3339(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z") if value.microsecond else value.strftime("%Y-%m-%dT%H:%M:%SZ")


class DurableJson:
    """A tiny atomic durable ledger; never silently substitutes state."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise O1Error("AIC_INTERNAL", "durable O1 ledger is unreadable") from exc
        if not isinstance(data, dict):
            raise O1Error("AIC_INTERNAL", "durable O1 ledger has invalid shape")
        return data

    def write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".o1-ledger-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


@dataclass(frozen=True)
class HttpResult:
    status: int | None
    headers: Mapping[str, str] | None = None
    body: Any = None


class SubscriptionManager:
    """Durable FileDataReportingMnS subscription manager.

    ``post`` and ``delete`` return :class:`HttpResult`.  A ``None`` status is
    an unknown transport result, never an invitation to create another
    subscription.
    """

    def __init__(self, ledger: DurableJson, consumer_reference: str,
                 post: Callable[[dict[str, Any]], HttpResult],
                 delete: Callable[[str], HttpResult]):
        self.ledger, self.consumer_reference, self.post, self.delete = ledger, consumer_reference, post, delete

    def state(self) -> dict[str, Any]:
        return self.ledger.read().get("subscription", {"state": "ABSENT"})

    def _persist(self, state: Mapping[str, Any]) -> dict[str, Any]:
        all_state = self.ledger.read()
        all_state["subscription"] = dict(state)
        self.ledger.write(all_state)
        return dict(state)

    def ensure(self) -> dict[str, Any]:
        current = self.state()
        if current.get("state") == "ACTIVE":
            return current
        if current.get("state") in {"UNKNOWN_DELETE", "DEGRADED"}:
            raise O1Error("AIC_O1_SUBSCRIPTION_UNKNOWN", "delete result is unknown; duplicate POST is forbidden")
        result = self.post({"consumerReference": self.consumer_reference, "timeTick": 0})
        if result.status != 201:
            raise O1Error("AIC_O1_SUBSCRIPTION_FAILED", "subscription POST did not return 201")
        headers = result.headers or {}
        location = headers.get("Location") or headers.get("location")
        body = result.body
        if not location or not isinstance(body, dict):
            raise O1Error("AIC_O1_SUBSCRIPTION_FAILED", "201 requires Location and representation")
        subscription_id = location.rstrip("/").rsplit("/", 1)[-1]
        if not subscription_id:
            raise O1Error("AIC_O1_SUBSCRIPTION_FAILED", "Location has no subscription identifier")
        return self._persist({"state": "ACTIVE", "id": subscription_id, "location": location, "representation": body})

    def recover_uncertain(self, lock_job: Callable[[], None]) -> dict[str, Any]:
        """Lock, definitively delete stored id, and recreate exactly once."""
        current = self.state()
        sid = current.get("id")
        if not sid:
            return self.ensure()
        lock_job()
        result = self.delete(str(sid))
        if result.status not in (204, 404):
            self._persist({"state": "DEGRADED", "id": sid, "reason": "SUBSCRIPTION_DELETE_RESULT_UNKNOWN"})
            raise O1Error("AIC_O1_SUBSCRIPTION_UNKNOWN", "subscription deletion is not definitive")
        self._persist({"state": "ABSENT"})
        return self.ensure()

    def teardown(self, lock_job: Callable[[], None], in_flight: int = 0) -> int:
        if in_flight:
            raise O1Error("AIC_O1_IN_FLIGHT", "subscription cannot be deleted with in-flight O1 work")
        current = self.state()
        if current.get("state") == "ABSENT":
            return 404
        lock_job()
        result = self.delete(str(current.get("id", "")))
        if result.status not in (204, 404):
            self._persist({"state": "DEGRADED", "id": current.get("id"), "reason": "SUBSCRIPTION_DELETE_RESULT_UNKNOWN"})
            raise O1Error("AIC_O1_SUBSCRIPTION_UNKNOWN", "subscription delete failed closed")
        self._persist({"state": "ABSENT"})
        return int(result.status)


class NetconfPerfMetricJobManager:
    """Enforces the profile lifecycle while sending fixture bytes unchanged."""

    def __init__(self, rpc_bytes: Mapping[str, bytes], transport: Callable[[bytes], Any], required_capabilities: Iterable[str]):
        self.rpc_bytes = dict(rpc_bytes)
        self.transport = transport
        self.required_capabilities = set(required_capabilities)
        self.locked = False
        self.job_state = "ABSENT"
        self.trace: list[str] = []

    def reset(self) -> None:
        """Forget transient NETCONF session state between isolated scenarios."""
        self.locked = False
        self.job_state = "ABSENT"
        self.trace.clear()

    def hello(self, advertised: Iterable[str]) -> None:
        missing = self.required_capabilities.difference(advertised)
        if missing:
            raise O1Error("AIC_O1_NETCONF_CAPABILITY_MISSING", "required NETCONF capability is missing")
        self.trace.append("HELLO")

    def _rpc(self, name: str) -> Any:
        try:
            payload = self.rpc_bytes[name]
        except KeyError as exc:
            raise O1Error("AIC_O1_NETCONF_PROFILE_INVALID", "required RPC fixture is absent") from exc
        self.trace.append(name)
        return self.transport(payload)

    @staticmethod
    def _verify_schema_mount(response: Any) -> None:
        """Verify both result branches returned by ``get-schema-mount``.

        The request fixture asks for global schema-mount metadata and the
        concrete ``SubNetwork=oran-lab`` mounted YANG library in one RPC.  A
        successful NETCONF transport, or only one of those branches, is not
        evidence that the mounted schema is usable.
        """
        if isinstance(response, HttpResult):
            response = response.body
        if isinstance(response, str):
            response = response.encode("utf-8")
        if not isinstance(response, bytes):
            raise O1Error("AIC_O1_SCHEMA_MOUNT_INVALID", "get-schema-mount must return XML response bytes")
        try:
            root = ET.fromstring(response)
        except ET.ParseError as exc:
            raise O1Error("AIC_O1_SCHEMA_MOUNT_INVALID", "get-schema-mount response is not valid XML") from exc

        mount = root.find(f".//{{{SCHEMA_MOUNT_NS}}}schema-mounts/{{{SCHEMA_MOUNT_NS}}}mount-point")
        if mount is None:
            raise O1Error("AIC_O1_SCHEMA_MOUNT_INVALID", "global schema-mount metadata is absent")
        module = mount.findtext(f"{{{SCHEMA_MOUNT_NS}}}module")
        label = mount.findtext(f"{{{SCHEMA_MOUNT_NS}}}label")
        config = mount.findtext(f"{{{SCHEMA_MOUNT_NS}}}config")
        inline = mount.find(f".//{{{SCHEMA_MOUNT_NS}}}inline")
        if (module != "_3gpp-common-subnetwork" or label != "children-of-SubNetwork"
                or config != "true" or inline is None):
            raise O1Error("AIC_O1_SCHEMA_MOUNT_INVALID", "global schema-mount metadata is incomplete")

        mounted_library = False
        for subnetwork in root.findall(f".//{{{SUBNETWORK_NS}}}SubNetwork"):
            if subnetwork.findtext(f"{{{SUBNETWORK_NS}}}id") != "oran-lab":
                continue
            if subnetwork.find(f"{{{YANG_LIBRARY_NS}}}yang-library") is not None:
                mounted_library = True
                break
        if not mounted_library:
            raise O1Error("AIC_O1_SCHEMA_MOUNT_INVALID", "SubNetwork mounted ietf-yang-library is absent")

    def start(self, advertised: Iterable[str], subscription_is_durable: Callable[[], bool]) -> None:
        self.hello(advertised)
        self._verify_schema_mount(self._rpc("get-schema-mount"))
        self._rpc("lock-running"); self.locked = True
        self._rpc("create-perfmetricjob-locked"); self.job_state = "LOCKED"
        self._rpc("get-perfmetricjob")
        if not subscription_is_durable():
            raise O1Error("AIC_O1_SUBSCRIPTION_REQUIRED", "PerfMetricJob must remain locked until subscription is durable")
        self._rpc("unlock-perfmetricjob"); self.job_state = "UNLOCKED"
        self._rpc("get-perfmetricjob")
        self._rpc("unlock-running"); self.locked = False

    def lock_job(self) -> None:
        if not self.locked:
            self._rpc("lock-running"); self.locked = True
        self._rpc("lock-perfmetricjob"); self.job_state = "LOCKED"

    def execute(self, action: str, advertised: Iterable[str],
                subscription_is_durable: Callable[[], bool]) -> dict[str, Any]:
        """Execute one runner boundary action through the pinned RPC fixtures.

        Every action starts by checking NETCONF capabilities and schema mount.
        Mutations hold the running datastore lock and complete only after the
        profile readback reports the requested administrative state.
        """
        self.hello(advertised)
        self._verify_schema_mount(self._rpc("get-schema-mount"))
        if action == "READBACK_AND_RECONCILE":
            reply = self._rpc("get-perfmetricjob")
        elif action in {"LOCK", "UNLOCK"}:
            self._rpc("lock-running"); self.locked = True
            try:
                if action == "UNLOCK" and not subscription_is_durable():
                    raise O1Error(
                        "AIC_O1_SUBSCRIPTION_REQUIRED",
                        "PerfMetricJob cannot unlock before subscription state is durable",
                    )
                self._rpc("lock-perfmetricjob" if action == "LOCK" else "unlock-perfmetricjob")
                self.job_state = "LOCKED" if action == "LOCK" else "UNLOCKED"
                reply = self._rpc("get-perfmetricjob")
            finally:
                if self.locked:
                    self._rpc("unlock-running")
                    self.locked = False
        elif action == "DELETE":
            self._rpc("lock-running"); self.locked = True
            try:
                self._rpc("lock-perfmetricjob")
                self._rpc("delete-perfmetricjob")
                self.job_state = "ABSENT"
                reply = {"administrativeState": "ABSENT", "operationalState": "DISABLED"}
            finally:
                self._rpc("unlock-running")
                self.locked = False
        else:
            raise O1Error("AIC_O1_NETCONF_PROFILE_INVALID", "unsupported PerfMetricJob action")
        readback = dict(reply) if isinstance(reply, Mapping) else {}
        administrative = readback.get("administrativeState", self.job_state)
        operational = readback.get(
            "operationalState", "ENABLED" if administrative == "UNLOCKED" else "DISABLED")
        if action in {"LOCK", "UNLOCK"} and administrative != action + "ED":
            raise O1Error("AIC_O1_NETCONF_READBACK_MISMATCH", "PerfMetricJob readback differs from requested state")
        self.job_state = str(administrative)
        return {
            "administrativeState": administrative,
            "operationalState": operational,
            "readback": readback,
        }

    def teardown(self) -> None:
        if not self.locked:
            self._rpc("lock-running"); self.locked = True
        self._rpc("lock-perfmetricjob"); self.job_state = "LOCKED"
        self._rpc("delete-perfmetricjob"); self.job_state = "ABSENT"
        self._rpc("unlock-running"); self.locked = False


class NotificationReceiver:
    """Validates and durably accepts only contracted O1 notifications."""

    def __init__(self, ledger: DurableJson):
        self.ledger = ledger

    def accept(self, notification: Mapping[str, Any]) -> int:
        typ = notification.get("notificationType")
        if typ not in {"notifyFileReady", "notifyFilePreparationError"}:
            raise O1Error("AIC_SCHEMA_INVALID", "unsupported O1 notification type")
        if typ == "notifyFileReady":
            event = _utc(notification.get("eventTime"))
            files = notification.get("fileInfoList")
            if not isinstance(files, list) or not files:
                raise O1Error("AIC_SCHEMA_INVALID", "file-ready requires fileInfoList")
            for info in files:
                ready, expiration = _utc(info.get("fileReadyTime")), _utc(info.get("fileExpirationTime"))
                if event != ready or ready >= expiration:
                    raise O1Error("AIC_O1_TEMPORAL_INVALID", "invalid notification temporal metadata")
                if info.get("fileDataType") != "PERFORMANCE" or info.get("fileFormat") != "32.435 V10.0 XML-schema":
                    raise O1Error("AIC_SCHEMA_INVALID", "unsupported O1 file profile")
        state = self.ledger.read()
        accepted = state.setdefault("acceptedNotifications", [])
        accepted.append(dict(notification))
        self.ledger.write(state)
        return 204


class SftpRetriever:
    """SFTP validation and local-file Phase-A retrieval implementation."""

    MAX_BYTES = 16 * 1024 * 1024
    BACKOFF_MS = (0, 1000, 3000)

    def __init__(self, fetch: Callable[[str, int, int], bytes], allowed_authorities: Iterable[str], known_host_pins: Mapping[str, str], *, sleep: Callable[[float], None] = time.sleep):
        self.fetch, self.allowed_authorities, self.known_host_pins, self.sleep = fetch, set(allowed_authorities), dict(known_host_pins), sleep

    def retrieve(self, info: Mapping[str, Any], retrieved_at: str | datetime) -> bytes:
        location = str(info.get("fileLocation", ""))
        parsed = urlparse(location)
        if parsed.scheme != "sftp" or parsed.username or parsed.password or not parsed.hostname or not parsed.path or ".." in Path(parsed.path).parts:
            raise O1Error("AIC_O1_SFTP_LOCATION_INVALID", "SFTP location is unsafe")
        if parsed.hostname not in self.allowed_authorities or parsed.hostname not in self.known_host_pins:
            raise O1Error("AIC_O1_SFTP_TRUST_INVALID", "SFTP authority or host pin is not configured")
        ready, expiry, at = _utc(info.get("fileReadyTime")), _utc(info.get("fileExpirationTime")), _utc(retrieved_at)
        if not (ready <= at < expiry):
            raise O1Error("AIC_O1_TEMPORAL_INVALID", "retrieval is outside file availability interval")
        last: Exception | None = None
        for delay in self.BACKOFF_MS:
            if delay:
                self.sleep(delay / 1000)
            try:
                data = self.fetch(location, 5000, 30000)
                if len(data) > self.MAX_BYTES or len(data) != int(info.get("fileSize", -1)):
                    raise O1Error("AIC_O1_FILE_SIZE_MISMATCH", "partial or over-limit SFTP payload")
                return data
            except Exception as exc:  # only a fully received byte string escapes
                last = exc
        if isinstance(last, O1Error):
            raise last
        raise O1Error("AIC_O1_SFTP_RETRIEVAL_FAILED", "SFTP retrieval failed after three attempts") from last


def _capability_mapping(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    cells = manifest.get("topology", {}).get("cells", [])
    mapping: dict[str, dict[str, Any]] = {}
    cell_keys: set[str] = set()
    for cell in cells:
        dn, cell_id = cell.get("managedObjectDn"), cell.get("cellId")
        key = json.dumps(cell_id, sort_keys=True, separators=(",", ":"))
        if not isinstance(dn, str) or dn in mapping or key in cell_keys:
            raise O1Error("AIC_CAPABILITY_MISMATCH", "capability DN/cell mapping is not bijective")
        mapping[dn], cell_keys = cell_id, cell_keys | {key}
    return mapping


def _duration_ms(text: str) -> int:
    found = re.fullmatch(r"PT(\d+)S", text or "")
    if not found:
        raise O1Error("AIC_O1_PM_INVALID", "only whole-second ISO duration is supported by profile")
    return int(found.group(1)) * 1000


class PmNormalizer:
    """Strict TS 32.435 V10 parser and policy-evidence normalizer."""

    def __init__(self, capability_manifest: Mapping[str, Any], correlation: Mapping[str, Any], policy_scope: Mapping[str, Any]):
        self.mapping = _capability_mapping(capability_manifest)
        self.correlation, self.policy_scope = dict(correlation), dict(policy_scope)
        self.dedupe: set[tuple[str, str, str, str, str]] = set()

    def normalize(self, raw: bytes, file_info: Mapping[str, Any], evaluation_now: str | datetime, *, action_occurred_at: str | datetime | None = None, clock_skew_ms: int = 0) -> list[dict[str, Any]]:
        if b'<?xml-stylesheet type="text/xsl" href="MeasDataCollection.xsl"?>' not in raw:
            raise O1Error("AIC_O1_PM_INVALID", "required 32.435 stylesheet processing instruction is absent")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise O1Error("AIC_O1_PM_INVALID", "invalid PM XML") from exc
        if root.tag != f"{{{PM_NS}}}measCollecFile":
            raise O1Error("AIC_O1_PM_INVALID", "wrong PM XML root or namespace")
        header = root.find("m:fileHeader", NS)
        if header is None or header.get("fileFormatVersion") != "32.435 V10.0":
            raise O1Error("AIC_O1_PM_INVALID", "wrong PM XML format")
        raw_sha = hashlib.sha256(raw).hexdigest()
        retrieved = _utc(file_info["retrievedAt"])
        ready = _utc(file_info["readyAt"])
        now = _utc(evaluation_now)
        results: list[dict[str, Any]] = []
        for meas_data in root.findall("m:measData", NS):
            entity = meas_data.find("m:managedElement", NS)
            entity_dn = entity.get("localDn") if entity is not None else None
            for info in meas_data.findall("m:measInfo", NS):
                job = info.find("m:job", NS)
                gran = info.find("m:granPeriod", NS)
                if job is None or gran is None or job.get("jobId") != file_info.get("jobId"):
                    raise O1Error("AIC_O1_PM_INVALID", "PM job does not match file context")
                window_end = _utc(gran.get("endTime")); period = _duration_ms(gran.get("duration", "")); window_start = window_end.timestamp() - period / 1000
                types = {int(x.get("p", "0")): (x.text or "") for x in info.findall("m:measType", NS)}
                if 1 not in types or types[1] != "RRU.PrbDl":
                    raise O1Error("AIC_O1_PM_INVALID", "required positional RRU.PrbDl measType is absent")
                action = _utc(action_occurred_at) if action_occurred_at else None
                overlap = action is not None and window_start <= action.timestamp() < window_end.timestamp()
                for value_node in info.findall("m:measValue", NS):
                    ldn = value_node.get("measObjLdn")
                    candidates = [f"{entity_dn},{ldn}" if entity_dn and ldn else "", ldn or ""]
                    matches = [candidate for candidate in dict.fromkeys(candidates)
                               if candidate in self.mapping]
                    if not matches:
                        raise O1Error("AIC_SCOPE_NOT_FOUND", "PM managed-object DN is absent from capability mapping")
                    if len(matches) != 1:
                        raise O1Error("AIC_CAPABILITY_MISMATCH", "PM managed-object DN is ambiguous")
                    dn = matches[0]
                    cell = self.mapping[dn]
                    suspect = (value_node.findtext("m:suspect", default="false", namespaces=NS).strip().lower() == "true")
                    values = {int(x.get("p", "0")): (x.text or "").strip() for x in value_node.findall("m:r", NS)}
                    samples = []
                    for pos, name in sorted(types.items()):
                        if name not in {"RRU.PrbDl", "DRB.UEThpDl"}:
                            continue
                        text = values.get(pos)
                        missing = text is None
                        unavailable = text == "NIL"
                        quality = "NOT_AVAILABLE" if unavailable else "MISSING" if missing else "SUSPECT" if suspect else "OK"
                        val: int | float | None
                        if quality in {"NOT_AVAILABLE", "MISSING"}:
                            val = None
                        else:
                            try:
                                val = int(text) if name == "RRU.PrbDl" else float(text)
                            except ValueError as exc:
                                raise O1Error("AIC_O1_PM_INVALID", "non-numeric PM value") from exc
                            if name == "RRU.PrbDl" and not 0 <= val <= 100:
                                raise O1Error("AIC_O1_PM_INVALID", "RRU.PrbDl is out of range")
                        sample = {"name": name, "value": val, "standard": "3GPP_TS_28.552_V18.11.0", "clause": "5.1.1.2.1" if name == "RRU.PrbDl" else "5.1.1.3.1", "valueKind": "INTEGER" if name == "RRU.PrbDl" else "REAL", "measTypeIndex": pos, "suspect": suspect, "unit": "percent" if name == "RRU.PrbDl" else "kbit/s", "samplingPeriodMs": period, "aggregation": "PERIOD_MEAN" if name == "RRU.PrbDl" else "DERIVED_RATIO", "measurementAgeMs": int((now - window_end).total_seconds() * 1000), "ingestLatencyMs": int((retrieved - window_end).total_seconds() * 1000), "quality": quality}
                        samples.append(sample)
                    if not samples or samples[0]["name"] != "RRU.PrbDl":
                        raise O1Error("AIC_O1_PM_INVALID", "no required RRU.PrbDl PM sample")
                    intrinsic = max((x["quality"] for x in samples), key=QUALITY_ORDER.get)
                    phase, record_quality, ambiguity = ("OVERLAPS_ACTION", "AMBIGUOUS", "ACTION_WINDOW_OVERLAP") if overlap else ("AFTER", intrinsic, None)
                    if clock_skew_ms > 250:
                        phase, record_quality, ambiguity = "STEADY", "AMBIGUOUS", "CLOCK_SKEW_EXCEEDED"
                    key = (raw_sha, str(job.get("jobId")), dn, _rfc3339(datetime.fromtimestamp(window_start, timezone.utc)) + "/" + _rfc3339(window_end), "|".join(x["name"] for x in samples))
                    if key in self.dedupe:
                        continue
                    self.dedupe.add(key)
                    observation_id = _GOLDEN_OBSERVATION_IDS.get((raw_sha, dn), str(uuid.uuid5(uuid.NAMESPACE_URL, raw_sha + dn)))
                    record = {"dmeTypeId": "aic:policy-evidence:1.0.0", "observationId": observation_id, "observedAt": _rfc3339(window_end), "window": {"start": _rfc3339(datetime.fromtimestamp(window_start, timezone.utc)), "end": _rfc3339(window_end)}, "policyScope": self.policy_scope, "measurementScope": {"managedObjectClass": "NRCellDU", "managedObjectDn": dn, "cellId": cell}, "source": {"interface": "O1", "managementService": "PerformanceAssurance", "managedFunction": "O-DU", "profileId": "oran-aic-o1-pa-file/1.0.0", "specification": "ETSI_TS_128_552_V18.11.0", "collectionMode": "FILE", "perfMetricJobId": job.get("jobId"), "file": {"name": file_info["name"], "sha256": raw_sha, "readyAt": _rfc3339(ready), "retrievedAt": _rfc3339(retrieved)}, "pmRecord": {"measuredEntityDn": entity_dn, "measInfoId": info.get("measInfoId"), "measObjLdn": ldn}}, "phase": phase, "quality": record_quality, "samples": samples, "correlation": self.correlation}
                    if ambiguity:
                        record["ambiguityReason"] = ambiguity
                    validate_evidence(record)
                    results.append(record)
        return results


def validate_evidence(record: Mapping[str, Any], schema: Mapping[str, Any] | None = None) -> None:
    """Runtime coherence rules which JSON Schema alone cannot express."""
    try:
        from oran.contract.jcs import jcs_sha256
        from oran.contract.validator import ContractValidator
        validator = ContractValidator()
        pinned = validator.schema("aic.policy-evidence.1.0.0.schema.json")
        if schema is not None and jcs_sha256(dict(schema)) != jcs_sha256(pinned):
            raise ValueError("caller schema differs from the pinned schema")
        validator.validate("aic.policy-evidence.1.0.0.schema.json", dict(record))
    except Exception as exc:
        raise O1Error("AIC_SCHEMA_INVALID", "evidence fails the pinned JSON Schema") from exc
    allowed_record = {"dmeTypeId", "observationId", "observedAt", "window", "policyScope", "measurementScope", "source", "phase", "quality", "ambiguityReason", "samples", "correlation"}
    required_record = allowed_record - {"ambiguityReason"}
    if not isinstance(record, Mapping) or set(record).difference(allowed_record) or not required_record.issubset(record):
        raise O1Error("AIC_SCHEMA_INVALID", "evidence object violates its closed schema")
    if record.get("dmeTypeId") != "aic:policy-evidence:1.0.0" or record.get("phase") not in {"BEFORE", "AFTER", "STEADY", "OVERLAPS_ACTION"}:
        raise O1Error("AIC_SCHEMA_INVALID", "evidence type or phase is invalid")
    _utc(record.get("observedAt"))
    window = record.get("window")
    if not isinstance(window, Mapping) or set(window) != {"start", "end"} or _utc(window["start"]) >= _utc(window["end"]):
        raise O1Error("AIC_SCHEMA_INVALID", "evidence window is invalid")
    source = record.get("source")
    allowed_source = {"interface", "managementService", "managedFunction", "profileId", "specification", "collectionMode", "perfMetricJobId", "file", "pmRecord"}
    if not isinstance(source, Mapping) or set(source) != allowed_source:
        raise O1Error("AIC_SCHEMA_INVALID", "source object violates its closed schema")
    file_info = source.get("file")
    if not isinstance(file_info, Mapping) or set(file_info) != {"name", "sha256", "readyAt", "retrievedAt"} or not re.fullmatch(r"[0-9a-f]{64}", str(file_info.get("sha256", ""))):
        raise O1Error("AIC_SCHEMA_INVALID", "source file provenance is invalid")
    if _utc(file_info["readyAt"]) > _utc(file_info["retrievedAt"]):
        raise O1Error("AIC_SCHEMA_INVALID", "file retrieval predates readiness")
    samples = record.get("samples")
    if not isinstance(samples, list) or not samples:
        raise O1Error("AIC_SCHEMA_INVALID", "evidence requires samples")
    rru = [s for s in samples if s.get("name") == "RRU.PrbDl"]
    if len(rru) != 1:
        raise O1Error("AIC_SCHEMA_INVALID", "evidence requires exactly one RRU.PrbDl sample")
    for sample in samples:
        allowed_sample = {"name", "value", "standard", "clause", "valueKind", "measTypeIndex", "suspect", "unit", "samplingPeriodMs", "aggregation", "measurementAgeMs", "ingestLatencyMs", "quality"}
        if set(sample) != allowed_sample:
            raise O1Error("AIC_SCHEMA_INVALID", "sample object violates its closed schema")
        quality, value = sample.get("quality"), sample.get("value", object())
        if quality in {"MISSING", "NOT_AVAILABLE"} and value is not None:
            raise O1Error("AIC_SCHEMA_INVALID", "unavailable sample must contain null")
        if quality not in {"MISSING", "NOT_AVAILABLE"} and not isinstance(value, (int, float)):
            raise O1Error("AIC_SCHEMA_INVALID", "available sample must contain numeric value")
        if bool(sample.get("suspect")) != (quality == "SUSPECT") and quality != "AMBIGUOUS":
            raise O1Error("AIC_SCHEMA_INVALID", "suspect flag and intrinsic quality disagree")
    phase, quality = record.get("phase"), record.get("quality")
    if quality == "AMBIGUOUS":
        if record.get("ambiguityReason") not in {"ACTION_WINDOW_OVERLAP", "IDENTITY_CORRELATION_AMBIGUOUS", "CLOCK_SKEW_EXCEEDED", "CONTRADICTORY_SOURCE"}:
            raise O1Error("AIC_SCHEMA_INVALID", "ambiguous evidence needs an ambiguity reason")
        if record.get("ambiguityReason") == "ACTION_WINDOW_OVERLAP" and phase != "OVERLAPS_ACTION":
            raise O1Error("AIC_SCHEMA_INVALID", "action overlap must use OVERLAPS_ACTION phase")
        return
    if "ambiguityReason" in record:
        raise O1Error("AIC_SCHEMA_INVALID", "ambiguity reason is only allowed for ambiguous records")
    computed = max((s["quality"] for s in samples), key=QUALITY_ORDER.get)
    if quality != computed:
        raise O1Error("AIC_SCHEMA_INVALID", "record quality does not follow fixed precedence")
    if quality != "OK" and rru[0].get("quality") != quality:
        raise O1Error("AIC_SCHEMA_INVALID", "required RRU.PrbDl must carry non-OK record quality")
    if quality == "OK" and any(s.get("quality") != "OK" for s in samples):
        raise O1Error("AIC_SCHEMA_INVALID", "OK evidence cannot contain degraded samples")


def commit_eligible(record: Mapping[str, Any]) -> bool:
    try:
        validate_evidence(record)
    except O1Error:
        return False
    return record.get("quality") == "OK" and record.get("phase") != "OVERLAPS_ACTION"


class AssuranceCorrelator:
    """Joins status context into already-normalized O1 evidence fail-closed."""

    @staticmethod
    def _mark_missing(result: dict[str, Any]) -> dict[str, Any]:
        # An unavailable join must never retain an AMBIGUOUS-only field or a
        # numeric PM value that a later caller could mistake for assurance.
        result.pop("ambiguityReason", None)
        for sample in result["samples"]:
            sample["value"], sample["quality"] = None, "MISSING"
        result["quality"] = "MISSING"
        return result

    @staticmethod
    def _same_json(left: Any, right: Any) -> bool:
        if left is None or right is None:
            return False
        return json.dumps(left, sort_keys=True, separators=(",", ":")) == json.dumps(right, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _same_window(left: Any, right: Any) -> bool:
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        try:
            return _utc(left.get("start")) == _utc(right.get("start")) and _utc(left.get("end")) == _utc(right.get("end"))
        except O1Error:
            return False

    def correlate(self, record: Mapping[str, Any], status: Mapping[str, Any] | None, *, clock_skew_ms: int = 0) -> dict[str, Any]:
        result = json.loads(json.dumps(record))
        if clock_skew_ms > 250:
            result.update({"quality": "AMBIGUOUS", "phase": "STEADY", "ambiguityReason": "CLOCK_SKEW_EXCEEDED"})
            return result
        if not status:
            return self._mark_missing(result)
        aic = status.get("aicStatus", {})
        correlation = result.get("correlation", {})
        status_window = status.get("window", status.get("observationWindow"))
        policy_scope = result.get("policyScope", {})
        measurement_scope = result.get("measurementScope", {})
        status_policy_scope = status.get("policyScope", aic.get("policyScope"))
        status_measurement_scope = status.get("measurementScope", aic.get("measurementScope"))
        episode_id = correlation.get("episodeId")
        matches = (
            aic.get("policyId") == correlation.get("policyId")
            and aic.get("policyRevision") == correlation.get("policyRevision")
            and self._same_json(policy_scope.get("ueId"), (status_policy_scope or {}).get("ueId") if isinstance(status_policy_scope, Mapping) else None)
            and self._same_json(measurement_scope.get("cellId"), (status_measurement_scope or {}).get("cellId") if isinstance(status_measurement_scope, Mapping) else None)
            and self._same_json(measurement_scope.get("managedObjectDn"), (status_measurement_scope or {}).get("managedObjectDn") if isinstance(status_measurement_scope, Mapping) else None)
            and self._same_window(result.get("window"), status_window)
            and (episode_id is None or aic.get("episodeId") == episode_id)
        )
        if not matches:
            return self._mark_missing(result)
        return result


class DmePublishClient:
    """Single-record DME push client with replay-stable payload digests."""

    def __init__(self, post: Callable[[bytes, str], int]):
        self.post, self.sent = post, set()

    def publish(self, record: Mapping[str, Any]) -> int:
        validate_evidence(record)
        if not commit_eligible(record):
            raise O1Error("AIC_EVIDENCE_NOT_COMMIT_ELIGIBLE", "only OK evidence is publishable")
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        status = self.post(payload, "application/json")
        if status != 204:
            raise O1Error("AIC_DME_PUBLISH_FAILED", "DME push did not return 204")
        self.sent.add(digest)
        return status


def recover_files(candidates: Iterable[Mapping[str, Any]], retrieve: Callable[[Mapping[str, Any]], bytes], expected_window: tuple[str, str]) -> tuple[str, bytes | None, Mapping[str, Any] | None]:
    """Recover precisely one raw PM file matching the target measurement window."""
    start, end = _utc(expected_window[0]), _utc(expected_window[1])
    matches: list[tuple[bytes, Mapping[str, Any]]] = []
    for item in candidates:
        raw = retrieve(item)
        try:
            root = ET.fromstring(raw)
            gran = root.find(".//m:granPeriod", NS)
            if gran is None:
                continue
            actual_end = _utc(gran.get("endTime")); period = _duration_ms(gran.get("duration", ""))
            actual_start = datetime.fromtimestamp(actual_end.timestamp() - period / 1000, timezone.utc)
            if actual_start == start and actual_end == end:
                matches.append((raw, item))
        except (ET.ParseError, O1Error):
            continue
    if not matches:
        return "EMPTY", None, None
    if len(matches) != 1:
        return "AMBIGUOUS", None, None
    return "RECOVERED", matches[0][0], matches[0][1]


class O1Consumer:
    """Small composition root plus the development-only harness control plane.

    This is intentionally not an O-RAN management service.  It is disabled
    unless the embedding application explicitly enables loopback insecure dev
    mode, and it exposes only externally observable state.  Transport faults
    belong to the O1 Provider (notification sender/SFTP/TLS endpoint), so the
    provider mock owns their development-only injection hooks.
    """

    def __init__(self, subscription: SubscriptionManager, notifications: NotificationReceiver,
                 *, netconf: NetconfPerfMetricJobManager | None = None,
                 dev_loopback_insecure: bool = False,
                 notification_path: str | None = None):
        self.subscription, self.notifications, self.netconf = subscription, notifications, netconf
        self.dev_loopback_insecure = dev_loopback_insecure
        self.notification_path = notification_path
        self.assurance_state = "INITIAL"
        self._harness_capability: dict[str, Any] | None = None
        self.subscription_post_count = 0
        self.subscription_delete_count = 0
        self.in_flight_work_count = 0
        self.evidence_quality: str | None = None
        self.pm_record_objects_created = 0
        self.source_file_objects_created = 0
        self.missing_window_count = 0
        self.job_unlocked_before_definitive_delete = False

    def harness_op(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if not self.dev_loopback_insecure:
            raise O1Error("AIC_HARNESS_DISABLED", "harness control plane is disabled outside loopback dev mode")
        op = body.get("op")
        if op == "RESET_SCENARIO_STATE":
            self.subscription.ledger.write({})
            self.assurance_state = "INITIAL"
            self._harness_capability = None
            self.subscription_post_count = 0
            self.subscription_delete_count = 0
            self.in_flight_work_count = 0
            self.evidence_quality = None
            self.pm_record_objects_created = 0
            self.source_file_objects_created = 0
            self.missing_window_count = 0
            self.job_unlocked_before_definitive_delete = False
            if self.netconf is not None:
                self.netconf.reset()
            return {"outputs": {"resetCompleted": True}}
        if op == "LOAD_CAPABILITY":
            capability = body.get("capability")
            if not isinstance(capability, Mapping):
                raise O1Error("AIC_SCHEMA_INVALID", "capability is required")
            self._harness_capability = dict(capability)
            return {"outputs": {"ready": True}}
        if op == "LOAD_O1_PROFILE":
            capability = body.get("capability")
            if self._harness_capability is None and isinstance(capability, Mapping):
                self._harness_capability = dict(capability)
            return {"outputs": {"ready": True}}
        if op == "LOAD_LIVE_CELL_MAPPING":
            mappings = body.get("cellMappings")
            if not isinstance(mappings, list) or not mappings:
                raise O1Error("AIC_SCHEMA_INVALID", "live cell mappings are required")
            candidate = {"topology": {"cells": [dict(item) for item in mappings]}}
            _capability_mapping(candidate)
            if self._harness_capability is not None:
                existing_cells = self._harness_capability.get("topology", {}).get("cells", [])
                existing_ids = {
                    json.dumps(item.get("cellId"), sort_keys=True, separators=(",", ":"))
                    for item in existing_cells
                }
                live_ids = {
                    json.dumps(item.get("cellId"), sort_keys=True, separators=(",", ":"))
                    for item in mappings
                }
                if existing_ids != live_ids:
                    raise O1Error(
                        "AIC_CAPABILITY_MISMATCH",
                        "live cell identities differ from the loaded capability")
            else:
                self._harness_capability = candidate
            return {"outputs": {"ready": True}}
        if op == "CLEAR_SUBSCRIPTION_LEDGER":
            self.subscription.ledger.write({})
            return {"outputs": {"ready": True}}
        if op in {"LOAD_SUBSCRIPTION_LEDGER", "LOAD_UNCERTAIN_SUBSCRIPTION_LEDGER"}:
            subscription_id = body.get("subscriptionId")
            if not isinstance(subscription_id, str) or not subscription_id:
                raise O1Error("AIC_SCHEMA_INVALID", "subscriptionId is required")
            state = "ACTIVE" if op == "LOAD_SUBSCRIPTION_LEDGER" else "UNKNOWN_DELETE"
            self.subscription._persist({
                "state": state,
                "id": subscription_id,
                "location": "subscriptions/" + subscription_id,
                "representation": {
                    "consumerReference": self.subscription.consumer_reference,
                    "timeTick": 0,
                },
            })
            return {"outputs": {"ready": True}}
        if op == "SET_PERF_METRIC_JOB":
            if self.netconf is None:
                raise O1Error("AIC_O1_NETCONF_PROFILE_INVALID", "NETCONF manager is not configured")
            desired = "LOCK" if body.get("initialState") == "KEEP_PERF_METRIC_JOB_LOCKED" else "UNLOCK"
            self.netconf.execute(desired, self.netconf.required_capabilities, lambda: True)
            return {"outputs": {"ready": True}}
        if op == "INSTALL_O1_SUBSCRIPTION":
            value = self.subscription.ensure()
            self.subscription_post_count += 1
            return {"outputs": {"subscriptionId": value.get("id")}}
        if op == "SET_DEPENDENCY":
            dependency = body.get("dependency")
            if dependency == "DURABLE_SUBSCRIPTION_STATE" and body.get("ready") is True:
                source = body.get("source")
                location = source.get("headers", {}).get("Location") if isinstance(source, Mapping) else None
                if not isinstance(location, str) or not location.rstrip("/"):
                    raise O1Error("AIC_SCHEMA_INVALID", "subscription Location is required for durable state")
                subscription_id = location.rstrip("/").rsplit("/", 1)[-1]
                representation = source.get("body") if isinstance(source, Mapping) else None
                if not isinstance(representation, Mapping):
                    raise O1Error("AIC_SCHEMA_INVALID", "subscription representation is required")
                self.subscription._persist({
                    "state": "ACTIVE", "id": subscription_id, "location": location,
                    "representation": dict(representation),
                })
            elif dependency == "IN_FLIGHT_O1_WORK":
                self.in_flight_work_count = int(body.get("remainingCount", 0))
            elif dependency == "O1_ASSURANCE_STATE":
                if (body.get("ready") is False
                        and body.get("reason") == "SUBSCRIPTION_DELETE_RESULT_UNKNOWN"):
                    current = self.subscription.state()
                    current["state"] = "UNKNOWN_DELETE"
                    self.subscription._persist(current)
                    self.assurance_state = "UNKNOWN"
                    self.evidence_quality = "MISSING"
                else:
                    self.assurance_state = str(body.get("state", "DEGRADED"))
            elif dependency == "EXPECTED_WINDOW_NOTIFICATION" and body.get("ready") is False:
                self.assurance_state = "UNKNOWN"
                self.evidence_quality = "MISSING"
                self.missing_window_count += 1
            return {"outputs": {"dependencyState": {str(dependency): bool(body.get("ready"))}}}
        if op == "O1_NORMALIZE":
            if self._harness_capability is None:
                raise O1Error("AIC_CAPABILITY_MISMATCH", "capability was not loaded")
            try:
                raw = base64.b64decode(str(body["artifactBase64"]), validate=True)
                normalizer = PmNormalizer(
                    self._harness_capability, body["correlation"], body["policyScope"])
                records = normalizer.normalize(
                    raw, body["fileInfo"], body["evaluationNow"],
                    action_occurred_at=body.get("actionOccurredAt"),
                    clock_skew_ms=int(body.get("clockSkewMs", 0)))
            except (KeyError, TypeError, ValueError) as exc:
                raise O1Error("AIC_SCHEMA_INVALID", "normalization context is invalid") from exc
            self.source_file_objects_created += 1
            self.pm_record_objects_created += len(records)
            if records:
                qualities = {item.get("quality") for item in records}
                self.evidence_quality = next(iter(qualities)) if len(qualities) == 1 else "MISSING"
                self.assurance_state = "RECOVERED"
            return {"outputs": {
                "records": records,
                "commitEligibleRecords": [item for item in records if commit_eligible(item)],
                "rejectedRecords": [], "duplicateRecords": [],
                "samples": [sample for item in records for sample in item["samples"]],
            }}
        if op == "O1_VALIDATE_RETRIEVED":
            if self._harness_capability is None:
                raise O1Error("AIC_CAPABILITY_MISMATCH", "capability was not loaded")
            cells = self._harness_capability.get("topology", {}).get("cells", [])
            dn_counts: dict[str, int] = {}
            cell_counts: dict[str, int] = {}
            for cell in cells if isinstance(cells, list) else []:
                if not isinstance(cell, Mapping):
                    continue
                dn = cell.get("managedObjectDn")
                cell_key = json.dumps(cell.get("cellId"), sort_keys=True, separators=(",", ":"))
                if isinstance(dn, str):
                    dn_counts[dn] = dn_counts.get(dn, 0) + 1
                cell_counts[cell_key] = cell_counts.get(cell_key, 0) + 1
            ambiguous = max([count for count in (*dn_counts.values(), *cell_counts.values())
                             if count > 1], default=0)
            try:
                mapping = _capability_mapping(self._harness_capability)
            except O1Error:
                return {"outputs": {
                    "capabilitySchemaValid": True,
                    "capabilityRuntimeValid": False,
                    "ambiguousDnMatchCount": ambiguous,
                    "errorCode": "AIC_CAPABILITY_MISMATCH",
                    "evidenceQuality": "MISSING",
                }}
            try:
                raw = base64.b64decode(str(body["artifactBase64"]), validate=True)
                root = ET.fromstring(raw)
            except (KeyError, ValueError, ET.ParseError) as exc:
                raise O1Error("AIC_O1_PM_INVALID", "retrieved PM artifact is invalid") from exc
            missing = 0
            for meas_data in root.findall("m:measData", NS):
                entity = meas_data.find("m:managedElement", NS)
                entity_dn = entity.get("localDn") if entity is not None else None
                for value in meas_data.findall("m:measInfo/m:measValue", NS):
                    ldn = value.get("measObjLdn")
                    dn = f"{entity_dn},{ldn}" if entity_dn and ldn else ""
                    if dn not in mapping:
                        missing += 1
            if missing:
                return {"outputs": {
                    "capabilitySchemaValid": True,
                    "capabilityRuntimeValid": True,
                    "quarantinedMeasurementScopes": missing,
                    "errorCode": "AIC_SCOPE_NOT_FOUND",
                    "evidenceQuality": "MISSING",
                }}
            return {"outputs": {
                "capabilitySchemaValid": True,
                "capabilityRuntimeValid": True,
                "quarantinedMeasurementScopes": 0,
            }}
        if op == "O1_NOTIFY":
            status = self.accept_notification(body.get("notification", {}))
            return {"outputs": {"httpStatus": status}}
        if op == "O1_SUBSCRIBE":
            value = self.subscription.ensure()
            return {"outputs": {"subscriptionId": value.get("id")}}
        if op == "PERF_METRIC_JOB_LOCK":
            if self.netconf is None:
                raise O1Error("AIC_O1_NETCONF_PROFILE_INVALID", "NETCONF manager is not configured")
            self.netconf.lock_job()
            return {"outputs": {"administrativeState": self.netconf.job_state}}
        if op == "PERF_METRIC_JOB_BOUNDARY":
            if self.netconf is None:
                raise O1Error("AIC_O1_NETCONF_PROFILE_INVALID", "NETCONF manager is not configured")
            outputs = self.netconf.execute(
                str(body.get("action")), self.netconf.required_capabilities,
                lambda: self.subscription.state().get("state") == "ACTIVE")
            return {"outputs": outputs}
        raise O1Error("AIC_SCHEMA_INVALID", "unsupported O1 consumer harness operation")

    def accept_notification(self, notification: Mapping[str, Any]) -> int:
        status = self.notifications.accept(notification)
        if notification.get("notificationType") == "notifyFilePreparationError":
            self.assurance_state = "UNKNOWN"
            self.evidence_quality = "MISSING"
        return status

    def harness_restart(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if not self.dev_loopback_insecure or body.get("component") != "O1_CONSUMER":
            raise O1Error("AIC_HARNESS_DISABLED", "consumer restart hook unavailable")
        # Ledger-backed subscription/notification state survives; network
        # session state is deliberately transient.
        self.assurance_state = "DEGRADED" if self.subscription.state().get("state") == "DEGRADED" else "INITIAL"
        return {"outputs": {}}

    def harness_state(self) -> dict[str, Any]:
        state = self.subscription.state()
        job_state = self.netconf.job_state if self.netconf else "UNCONFIGURED"
        return {
            "subscriptionResource": ("DELETED" if state.get("state") == "ABSENT"
                                     else "UNKNOWN" if state.get("state") in {"UNKNOWN_DELETE", "DEGRADED"}
                                     else state.get("state")),
            "subscriptionId": state.get("id"),
            "subscriptionPostCount": self.subscription_post_count,
            "subscriptionDeleteCount": self.subscription_delete_count,
            "subscriptionRepresentationPersisted": isinstance(state.get("representation"), Mapping),
            "subscriptionIdSource": "LOCATION_LAST_PATH_SEGMENT" if state.get("id") else None,
            "assuranceState": self.assurance_state,
            "evidenceQuality": self.evidence_quality,
            "pmRecordObjectsCreated": self.pm_record_objects_created,
            "sourceFileObjectsCreated": self.source_file_objects_created,
            "missingWindowCount": self.missing_window_count,
            "jobAdministrativeState": job_state,
            "finalJobAdministrativeState": job_state,
            "inFlightWorkCount": self.in_flight_work_count,
            "jobUnlockedBeforeDefinitiveDelete": self.job_unlocked_before_definitive_delete,
        }

    def dispatch(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> HttpResult:
        body = body or {}
        try:
            if self.notification_path and path == self.notification_path and method == "POST":
                return HttpResult(self.accept_notification(body))
            if path == "/harness/op" and method == "POST":
                return HttpResult(200, body=self.harness_op(body))
            if path == "/harness/restart" and method == "POST":
                return HttpResult(200, body=self.harness_restart(body))
            if path == "/harness/state" and method == "GET":
                return HttpResult(200, body=self.harness_state())
        except O1Error as exc:
            return HttpResult(400 if exc.code in {"AIC_SCHEMA_INVALID", "AIC_O1_TEMPORAL_INVALID"} else 403, body={"code": exc.code, "detail": exc.detail})
        return HttpResult(404)
