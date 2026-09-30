"""Hermetic O1 provider mock for the fixed File PA profile.

It exposes adapter methods instead of binding a deployment socket.  ``dispatch``
can be wired to a stdlib HTTP server by integration tests; its harness methods
are explicitly development-only and must only be enabled on loopback.
"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from oran.o1.core import HttpResult, O1Error, _utc


@dataclass
class ProviderFile:
    info: dict[str, Any]
    data: bytes


class O1ProviderMock:
    REQUIRED_CAPABILITIES = {
        "urn:ietf:params:netconf:base:1.1",
        "urn:ietf:params:netconf:capability:writable-running:1.0",
        "urn:ietf:params:netconf:capability:validate:1.1",
        "urn:ietf:params:netconf:capability:rollback-on-error:1.0",
        "urn:ietf:params:netconf:capability:xpath:1.0",
    }

    def __init__(self, *, subscription_path: str, dev_loopback_insecure: bool = False):
        self.subscription_path = subscription_path.rstrip("/")
        self.dev_loopback_insecure = dev_loopback_insecure
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self.files: list[ProviderFile] = []
        self.faults: dict[str, dict[str, Any]] = {}
        self.netconf_log: list[bytes] = []
        self.job_administrative_state = "LOCKED"
        self.job_operational_state = "DISABLED"
        self.subscription_post_count = 0
        self.subscription_delete_count = 0
        self.delete_already_absent_converged = False

    def create_subscription(self, body: Mapping[str, Any]) -> HttpResult:
        if set(body) != {"consumerReference", "timeTick"} or body.get("timeTick") != 0 or not isinstance(body.get("consumerReference"), str):
            return HttpResult(400, body={"code": "AIC_SCHEMA_INVALID"})
        sid = str(uuid.uuid4())
        self.subscription_post_count += 1
        representation = {"consumerReference": body["consumerReference"], "timeTick": 0}
        self.subscriptions[sid] = representation
        return HttpResult(201, {"Location": f"{self.subscription_path}/{sid}"}, representation)

    def delete_subscription(self, sid: str) -> HttpResult:
        self.subscription_delete_count += 1
        if self.faults.pop("DROP_HTTP_RESPONSE", None) is not None:
            return HttpResult(None)
        if sid not in self.subscriptions:
            self.delete_already_absent_converged = True
            return HttpResult(404)
        del self.subscriptions[sid]
        return HttpResult(204)

    def add_file(self, info: Mapping[str, Any], data: bytes) -> None:
        if len(data) != int(info.get("fileSize", -1)):
            raise O1Error("AIC_O1_FILE_SIZE_MISMATCH", "mock file metadata size does not match its bytes")
        self.files.append(ProviderFile(dict(info), data))

    @staticmethod
    def generate_pm_xml(*, window_start: str, window_end: str, job_id: str,
                        measurements: Mapping[str, Mapping[str, float | int | None]],
                        measured_entity_dn: str = "SubNetwork=oran-lab,ManagedElement=oai-gnb",
                        meas_info_id: str = "oran-aic-pm") -> bytes:
        """Create the one allowed 32.435 V10 grammar branch for Phase A.

        ``measurements`` maps a measObjLdn to the contracted measurement names.
        ``None`` is serialized as ``NIL`` rather than an invented numeric zero.
        """
        start, end = _utc(window_start), _utc(window_end)
        seconds = int((end - start).total_seconds())
        if seconds <= 0:
            raise O1Error("AIC_O1_PM_INVALID", "PM window must be positive")
        rows = []
        for ldn, values in measurements.items():
            if set(values).difference({"RRU.PrbDl", "DRB.UEThpDl", "suspect"}) or "RRU.PrbDl" not in values:
                raise O1Error("AIC_O1_PM_INVALID", "provider measurements violate fixed PM profile")
            def encode(value: float | int | None) -> str:
                return "NIL" if value is None else str(value)
            optional = f'\n        <r p="2">{encode(values["DRB.UEThpDl"])}</r>' if "DRB.UEThpDl" in values else ""
            rows.append(f'''      <measValue measObjLdn="{ldn}">
        <r p="1">{encode(values["RRU.PrbDl"])}</r>{optional}
        <suspect>{str(bool(values.get("suspect", False))).lower()}</suspect>
      </measValue>''')
        types = '<measType p="1">RRU.PrbDl</measType>' + ('\n      <measType p="2">DRB.UEThpDl</measType>' if any("DRB.UEThpDl" in values for values in measurements.values()) else "")
        xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<?xml-stylesheet type="text/xsl" href="MeasDataCollection.xsl"?>
<measCollecFile xmlns="http://www.3gpp.org/ftp/specs/archive/32_series/32.435#measCollec">
  <fileHeader fileFormatVersion="32.435 V10.0" vendorName="O1ProviderMock">
    <fileSender localDn="{measured_entity_dn}" elementType="O-DU"/>
    <measCollec beginTime="{window_start}"/>
  </fileHeader>
  <measData>
    <managedElement localDn="{measured_entity_dn}" userLabel="o1-provider-mock" swVersion="phase-a"/>
    <measInfo measInfoId="{meas_info_id}">
      <job jobId="{job_id}"/>
      <granPeriod duration="PT{seconds}S" endTime="{window_end}"/>
      <repPeriod duration="PT{seconds}S"/>
      {types}
{chr(10).join(rows)}
    </measInfo>
  </measData>
  <fileFooter><measCollec endTime="{window_end}"/></fileFooter>
</measCollecFile>'''
        return xml.encode("utf-8")

    def list_files(self, *, file_data_type: str, begin_time: str, end_time: str) -> HttpResult:
        if file_data_type != "PERFORMANCE":
            return HttpResult(200, body=[])
        begin, end = _utc(begin_time), _utc(end_time)
        values = [f.info for f in self.files if f.info.get("fileDataType") == "PERFORMANCE" and begin <= _utc(f.info["fileReadyTime"]) < end]
        return HttpResult(200, body=values)

    def fetch_sftp(self, location: str, connect_timeout_ms: int, read_timeout_ms: int) -> bytes:
        if "TLS_HANDSHAKE_REJECT" in self.faults:
            raise O1Error("AIC_O1_TLS_HANDSHAKE_REJECT", "development TLS failure injection")
        for provider_file in self.files:
            if provider_file.info.get("fileLocation") == location:
                data = provider_file.data
                fault = self.faults.pop("FLIP_RETRIEVED_BYTE", None)
                if fault is not None:
                    index = int(fault.get("byteOffset", 0))
                    if not 0 <= index < len(data):
                        raise O1Error("AIC_O1_FILE_SIZE_MISMATCH", "fault byte offset outside file")
                    data = data[:index] + bytes([data[index] ^ 1]) + data[index + 1:]
                return data
        raise O1Error("AIC_O1_SFTP_RETRIEVAL_FAILED", "file is absent")

    def send_notification(self, notification: Mapping[str, Any], receiver: Callable[[Mapping[str, Any]], int]) -> int | None:
        if self.faults.pop("DROP_O1_NOTIFICATION", None) is not None:
            return None
        return receiver(notification)

    @staticmethod
    def _schema_mount_reply() -> bytes:
        return b'''<nc:rpc-reply xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0"
 xmlns:sm="urn:ietf:params:xml:ns:yang:ietf-yang-schema-mount"
 xmlns:sn="urn:3gpp:sa5:_3gpp-common-subnetwork"
 xmlns:yl="urn:ietf:params:xml:ns:yang:ietf-yang-library"><nc:data>
 <sm:schema-mounts><sm:mount-point><sm:module>_3gpp-common-subnetwork</sm:module>
 <sm:label>children-of-SubNetwork</sm:label><sm:config>true</sm:config>
 <sm:schema-ref><sm:inline/></sm:schema-ref></sm:mount-point></sm:schema-mounts>
 <sn:SubNetwork><sn:id>oran-lab</sn:id><yl:yang-library/></sn:SubNetwork>
 </nc:data></nc:rpc-reply>'''

    def netconf(self, payload: bytes) -> bytes | dict[str, Any]:
        """Records exact request bytes and models the tiny desired-state subset."""
        self.netconf_log.append(payload)
        if b"schema-mounts" in payload and b"yang-library" in payload:
            return self._schema_mount_reply()
        if b"administrativeState" in payload and b"UNLOCKED" in payload:
            self.job_administrative_state, self.job_operational_state = "UNLOCKED", "ENABLED"
        elif b"administrativeState" in payload and b"LOCKED" in payload:
            self.job_administrative_state, self.job_operational_state = "LOCKED", "DISABLED"
        elif b"operation=\"delete\"" in payload:
            self.job_administrative_state, self.job_operational_state = "ABSENT", "DISABLED"
        return {"ok": True, "administrativeState": self.job_administrative_state, "operationalState": self.job_operational_state}

    # Development-only scenario-runner control plane, never an O-RAN interface.
    def harness_fault(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if not self.dev_loopback_insecure:
            raise O1Error("AIC_HARNESS_DISABLED", "harness control plane is disabled outside loopback dev mode")
        fault = body.get("fault")
        if fault not in {"DROP_O1_NOTIFICATION", "FLIP_RETRIEVED_BYTE", "TLS_HANDSHAKE_REJECT", "DROP_HTTP_RESPONSE"}:
            raise O1Error("AIC_SCHEMA_INVALID", "unsupported O1 provider fault")
        self.faults[str(fault)] = dict(body.get("boundary") or {})
        return {"outputs": {}}

    def harness_op(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if not self.dev_loopback_insecure:
            raise O1Error("AIC_HARNESS_DISABLED", "harness control plane is disabled outside loopback dev mode")
        op = body.get("op")
        if op == "RESET_SCENARIO_STATE":
            self.subscriptions.clear(); self.files.clear(); self.faults.clear()
            self.netconf_log.clear(); self.job_administrative_state = "LOCKED"
            self.job_operational_state = "DISABLED"
            self.subscription_post_count = 0
            self.subscription_delete_count = 0
            self.delete_already_absent_converged = False
            return {"outputs": {"resetCompleted": True}}
        if op == "INSTALL_PROVIDER_SUBSCRIPTION":
            subscription_id = body.get("subscriptionId")
            if not isinstance(subscription_id, str) or not subscription_id:
                raise O1Error("AIC_SCHEMA_INVALID", "subscriptionId is required")
            self.subscriptions[subscription_id] = {
                "consumerReference": str(body.get("consumerReference", "")), "timeTick": 0}
            return {"outputs": {"ready": True}}
        if op == "DELETE_PROVIDER_SUBSCRIPTION_IF_PRESENT":
            subscription_id = body.get("subscriptionId")
            if isinstance(subscription_id, str):
                self.subscriptions.pop(subscription_id, None)
            return {"outputs": {"ready": True}}
        if op == "SELECT_SECURITY_FIXTURE":
            return {"outputs": {"ready": True}}
        if op == "CONFIGURE_LIVE_PM_PROFILE":
            infos = body.get("fileInfos")
            if infos is None and body.get("initialState") == \
                    "CONFIGURE_LIVE_O1_OK_RECORD_FOR_EACH_POLICY_CELL":
                return {"outputs": {"ready": True}}
            window = body.get("measurementWindow")
            mappings = body.get("cellMappings")
            job_id = body.get("jobId")
            if (not isinstance(infos, list) or not isinstance(window, Mapping)
                    or not isinstance(mappings, list) or not isinstance(job_id, str)):
                raise O1Error("AIC_SCHEMA_INVALID", "live PM profile inputs are incomplete")
            encoded = body.get("artifactBase64")
            if isinstance(encoded, str):
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except ValueError as exc:
                    raise O1Error("AIC_SCHEMA_INVALID", "live PM artifact is not base64") from exc
            else:
                measurements = {
                    str(item["managedObjectDn"]): {
                        "RRU.PrbDl": 37 + index,
                        "DRB.UEThpDl": 1000.0 + index,
                    }
                    for index, item in enumerate(mappings)
                    if isinstance(item, Mapping) and isinstance(item.get("managedObjectDn"), str)
                }
                raw = self.generate_pm_xml(
                    window_start=str(window.get("start")), window_end=str(window.get("end")),
                    job_id=job_id, measurements=measurements)
            self.files.clear()
            for item in infos:
                if not isinstance(item, Mapping):
                    raise O1Error("AIC_SCHEMA_INVALID", "FileInfo must be an object")
                info = dict(item)
                info["fileSize"] = len(raw)
                self.add_file(info, raw)
            return {"outputs": {"ready": True}}
        if op == "O1_SFTP_RETRIEVE":
            source = body.get("source")
            if not isinstance(source, str):
                raise O1Error("AIC_O1_SFTP_LOCATION_INVALID", "SFTP source is required")
            payload = self.fetch_sftp(
                source, int(body.get("connectTimeoutMs", 5000)),
                int(body.get("readTimeoutMs", 30000)))
            provider_file = next(
                item for item in self.files
                if item.info.get("fileLocation") == source)
            # This is the provider's simulated transport-completion clock, not
            # a runner copy of fileReadyTime.  A distinct instant lets temporal
            # assertions detect retrievals that cross fileExpirationTime.
            retrieved_at = (
                _utc(str(provider_file.info["fileReadyTime"])) + timedelta(milliseconds=1)
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            return {"outputs": {
                "artifactBase64": base64.b64encode(payload).decode("ascii"),
                "retrievedAt": retrieved_at,
            }}
        if op == "O1_FILE_LIST":
            result = self.list_files(file_data_type=str(body.get("fileDataType", "")), begin_time=str(body.get("beginTime", "")), end_time=str(body.get("endTime", "")))
            return {"outputs": {"files": result.body}}
        raise O1Error("AIC_SCHEMA_INVALID", "unsupported O1 provider harness operation")

    def harness_restart(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if not self.dev_loopback_insecure or body.get("component") != "O1_PROVIDER":
            raise O1Error("AIC_HARNESS_DISABLED", "provider restart hook unavailable")
        # subscriptions/files represent provider durable state, transient fault/log state does not.
        self.faults.clear(); self.netconf_log.clear()
        return {"outputs": {}}

    def harness_state(self) -> dict[str, Any]:
        return {
            "subscriptionCount": len(self.subscriptions),
            "subscriptionPostCount": self.subscription_post_count,
            "subscriptionDeleteCount": self.subscription_delete_count,
            "subscriptionResource": "PRESENT" if self.subscriptions else "DELETED",
            "deleteAlreadyAbsentConverged": self.delete_already_absent_converged,
            "fileCount": len(self.files),
            "sourceFileObjectsCreated": len(self.files),
            "jobAdministrativeState": self.job_administrative_state,
            "jobOperationalState": self.job_operational_state,
        }

    def dispatch(self, method: str, path: str, body: Mapping[str, Any] | None = None, query: Mapping[str, str] | None = None) -> HttpResult:
        """Minimal HTTP-shaped adapter useful to black-box harnesses."""
        body, query = body or {}, query or {}
        if path == "/harness/fault" and method == "POST":
            return HttpResult(200, body=self.harness_fault(body))
        if path == "/harness/op" and method == "POST":
            return HttpResult(200, body=self.harness_op(body))
        if path == "/harness/restart" and method == "POST":
            return HttpResult(200, body=self.harness_restart(body))
        if path == "/harness/state" and method == "GET":
            return HttpResult(200, body=self.harness_state())
        if self.faults.pop("TLS_HANDSHAKE_REJECT", None) is not None:
            # The loopback server cannot instantiate a second real TLS stack,
            # so closing the application connection is its observable
            # transport-level handshake rejection.  No route is dispatched.
            raise O1Error(
                "AIC_O1_TLS_HANDSHAKE_REJECT",
                "development TLS failure injection",
            )
        if path == self.subscription_path and method == "POST":
            return self.create_subscription(body)
        if path.startswith(self.subscription_path + "/") and method == "DELETE":
            return self.delete_subscription(path.rsplit("/", 1)[-1])
        if path.endswith("/files") and method == "GET":
            required = {"fileDataType", "beginTime", "endTime"}
            if set(query) != required:
                return HttpResult(400, body={"code": "AIC_SCHEMA_INVALID"})
            return self.list_files(file_data_type=query["fileDataType"], begin_time=query["beginTime"], end_time=query["endTime"])
        return HttpResult(404)
