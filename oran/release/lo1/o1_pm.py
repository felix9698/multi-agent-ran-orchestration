"""SC-084 live-value PM parsing, normalization, and raw-byte linkage."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping


PM_NS = "http://www.3gpp.org/ftp/specs/archive/32_series/32.435#measCollec"
NS = {"m": PM_NS}


class LiveInvariantError(ValueError):
    pass


def _utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise LiveInvariantError("live PM timestamps must be RFC3339 UTC Z")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise LiveInvariantError("live PM timestamp is malformed") from exc


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    ).replace(".000Z", "Z")


def _duration(value: str) -> timedelta:
    match = re.fullmatch(r"PT([1-9][0-9]*)S", value or "")
    if not match:
        raise LiveInvariantError("only positive whole-second PM granularity is accepted")
    return timedelta(seconds=int(match.group(1)))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _jcs_sha256(value: Mapping[str, Any]) -> str:
    try:
        from oran.contract.jcs import jcs_sha256

        return jcs_sha256(dict(value))
    except (ImportError, TypeError, ValueError):
        return hashlib.sha256(_canonical(value).encode()).hexdigest()


def measurement_values(raw: bytes) -> dict[str, set[int | float]]:
    """Read sample values by declared measurement name, never by fixed position."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    if root.tag != f"{{{PM_NS}}}measCollecFile":
        return {}
    found: dict[str, set[int | float]] = {}
    for info in root.findall(".//m:measInfo", NS):
        names = {node.get("p"): (node.text or "").strip() for node in info.findall("m:measType", NS)}
        for value_node in info.findall("m:measValue", NS):
            for result in value_node.findall("m:r", NS):
                name = names.get(result.get("p"))
                text = (result.text or "").strip()
                if not name or text == "NIL":
                    continue
                try:
                    number = int(text) if name == "RRU.PrbDl" else float(text)
                except ValueError:
                    continue
                found.setdefault(name, set()).add(number)
    return found


class LivePmNormalizer:
    def __init__(
        self,
        *,
        cell_mappings: Iterable[Mapping[str, Any]],
        expected_cell_count: int,
        golden_values: Mapping[str, set[int | float]],
        freshness_limit_ms: int,
    ) -> None:
        self.expected_cell_count = int(expected_cell_count)
        self.golden_values = {name: set(values) for name, values in golden_values.items()}
        self.freshness_limit_ms = int(freshness_limit_ms)
        self.by_dn: dict[str, Any] = {}
        self.by_cell: dict[str, str] = {}
        for entry in cell_mappings:
            dn = entry.get("managedObjectDn")
            cell = entry.get("cellId")
            key = _canonical(cell)
            if not isinstance(dn, str) or not dn or dn in self.by_dn or key in self.by_cell:
                raise LiveInvariantError("deployment cellMappings is not bijective")
            self.by_dn[dn] = json.loads(json.dumps(cell))
            self.by_cell[key] = dn
        if len(self.by_dn) != self.expected_cell_count:
            raise LiveInvariantError("cellMappings count differs from expectedPolicyCellCount")

    def normalize(
        self,
        *,
        raw: bytes,
        raw_sha256: str,
        raw_artifact_path: str,
        retrieval_sequence: int,
        file_info: Mapping[str, Any],
        retrieved_at: str,
        evaluation_now: str,
        correlation: Mapping[str, Any],
        policy_scope: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if hashlib.sha256(raw).hexdigest() != raw_sha256:
            raise LiveInvariantError("raw PM digest changed before parsing")
        if b'<?xml-stylesheet type="text/xsl" href="MeasDataCollection.xsl"?>' not in raw:
            raise LiveInvariantError("PM XML lacks the frozen stylesheet processing instruction")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise LiveInvariantError("PM payload is not well-formed XML") from exc
        if root.tag != f"{{{PM_NS}}}measCollecFile":
            raise LiveInvariantError("PM XML root or namespace differs from the profile")
        header = root.find("m:fileHeader", NS)
        if header is None or header.get("fileFormatVersion") != "32.435 V10.0":
            raise LiveInvariantError("PM file format version differs from the profile")
        ready = _utc(file_info.get("fileReadyTime"))
        retrieved = _utc(retrieved_at)
        now = _utc(evaluation_now)
        if retrieved < ready or now < retrieved:
            raise LiveInvariantError("PM ready/retrieval/evaluation order is invalid")

        records: list[dict[str, Any]] = []
        seen_dns: set[str] = set()
        duplicate_count = 0
        for meas_data in root.findall("m:measData", NS):
            entity = meas_data.find("m:managedElement", NS)
            entity_dn = entity.get("localDn") if entity is not None else None
            for info in meas_data.findall("m:measInfo", NS):
                job = info.find("m:job", NS)
                gran = info.find("m:granPeriod", NS)
                if job is None or gran is None or job.get("jobId") != file_info.get("jobId"):
                    raise LiveInvariantError("PM job context differs from fileInfo")
                end = _utc(gran.get("endTime"))
                start = end - _duration(gran.get("duration", ""))
                if not start < end or ready < end or retrieved < ready:
                    raise LiveInvariantError("PM window/file temporal relation is invalid")
                age_ms = int((now - end).total_seconds() * 1000)
                latency_ms = int((retrieved - end).total_seconds() * 1000)
                if age_ms < 0 or age_ms > self.freshness_limit_ms or latency_ms < 0:
                    raise LiveInvariantError("PM record is stale or temporally inconsistent")
                names: dict[str, str] = {}
                for node in info.findall("m:measType", NS):
                    position = node.get("p")
                    name = (node.text or "").strip()
                    if not position or position in names:
                        raise LiveInvariantError("PM measType positions are absent or duplicated")
                    names[position] = name
                if "RRU.PrbDl" not in names.values():
                    raise LiveInvariantError("required RRU.PrbDl declaration is absent")
                for value_node in info.findall("m:measValue", NS):
                    local_dn = value_node.get("measObjLdn")
                    candidates = [
                        f"{entity_dn},{local_dn}" if entity_dn and local_dn else "",
                        local_dn or "",
                    ]
                    matches = [dn for dn in dict.fromkeys(candidates) if dn in self.by_dn]
                    if len(matches) != 1:
                        raise LiveInvariantError("measured object DN does not resolve bijectively")
                    dn = matches[0]
                    if dn in seen_dns:
                        duplicate_count += 1
                        raise LiveInvariantError("more than one PM record exists for a policy cell")
                    seen_dns.add(dn)
                    suspect = value_node.findtext("m:suspect", "false", NS).strip().lower() == "true"
                    raw_results = {node.get("p"): (node.text or "").strip() for node in value_node.findall("m:r", NS)}
                    samples = []
                    for position, name in names.items():
                        if name not in {"RRU.PrbDl", "DRB.UEThpDl"}:
                            continue
                        text = raw_results.get(position)
                        if text in (None, "", "NIL"):
                            value = None
                            quality = "NOT_AVAILABLE" if text == "NIL" else "MISSING"
                        else:
                            try:
                                value = int(text) if name == "RRU.PrbDl" else float(text)
                            except ValueError as exc:
                                raise LiveInvariantError("PM measurement is not numeric") from exc
                            if name == "RRU.PrbDl" and (not isinstance(value, int) or not 0 <= value <= 100):
                                raise LiveInvariantError("RRU.PrbDl is outside integer range 0..100")
                            if name == "DRB.UEThpDl" and value < 0:
                                raise LiveInvariantError("DRB.UEThpDl is negative")
                            if value in self.golden_values.get(name, set()):
                                raise LiveInvariantError("live measurement equals a forbidden golden sample value")
                            quality = "SUSPECT" if suspect else "OK"
                        samples.append(
                            {
                                "name": name,
                                "value": value,
                                "standard": "3GPP_TS_28.552_V18.11.0",
                                "clause": "5.1.1.2.1" if name == "RRU.PrbDl" else "5.1.1.3.1",
                                "valueKind": "INTEGER" if name == "RRU.PrbDl" else "REAL",
                                "measTypeIndex": int(position),
                                "suspect": suspect,
                                "unit": "percent" if name == "RRU.PrbDl" else "kbit/s",
                                "samplingPeriodMs": int((end - start).total_seconds() * 1000),
                                "aggregation": "PERIOD_MEAN" if name == "RRU.PrbDl" else "DERIVED_RATIO",
                                "measurementAgeMs": age_ms,
                                "ingestLatencyMs": latency_ms,
                                "quality": quality,
                            }
                        )
                    required = [sample for sample in samples if sample["name"] == "RRU.PrbDl"]
                    if len(required) != 1 or required[0]["quality"] != "OK":
                        raise LiveInvariantError("required RRU.PrbDl sample is not commit eligible")
                    if any(sample["quality"] != "OK" for sample in samples):
                        raise LiveInvariantError("SC-084 live evidence contains a degraded sample")
                    cell = self.by_dn[dn]
                    record = {
                        "dmeTypeId": "aic:policy-evidence:1.0.0",
                        "observationId": str(uuid.uuid5(uuid.NAMESPACE_URL, raw_sha256 + dn)),
                        "observedAt": _stamp(end),
                        "window": {"start": _stamp(start), "end": _stamp(end)},
                        "policyScope": json.loads(json.dumps(policy_scope)),
                        "measurementScope": {
                            "managedObjectClass": "NRCellDU",
                            "managedObjectDn": dn,
                            "cellId": json.loads(json.dumps(cell)),
                        },
                        "source": {
                            "interface": "O1",
                            "managementService": "PerformanceAssurance",
                            "managedFunction": "O-DU",
                            "profileId": "oran-aic-o1-pa-file/1.0.0",
                            "specification": "ETSI_TS_128_552_V18.11.0",
                            "collectionMode": "FILE",
                            "perfMetricJobId": job.get("jobId"),
                            "file": {
                                "name": raw_artifact_path.rsplit("/", 1)[-1],
                                "sha256": raw_sha256,
                                "readyAt": _stamp(ready),
                                "retrievedAt": _stamp(retrieved),
                            },
                            "pmRecord": {
                                "measuredEntityDn": entity_dn,
                                "measInfoId": info.get("measInfoId"),
                                "measObjLdn": local_dn,
                            },
                        },
                        "phase": "AFTER",
                        "quality": "OK",
                        "samples": samples,
                        "correlation": json.loads(json.dumps(correlation)),
                    }
                    records.append(record)

        if seen_dns != set(self.by_dn) or len(records) != self.expected_cell_count:
            raise LiveInvariantError("normalized records are not a bijection over policy cells")
        summaries = []
        for index, record in enumerate(records):
            required = next(sample for sample in record["samples"] if sample["name"] == "RRU.PrbDl")
            summaries.append(
                {
                    "index": index,
                    "sourceRetrievalSequence": int(retrieval_sequence),
                    "sourceFileSha256": raw_sha256,
                    "rawArtifactPath": raw_artifact_path,
                    "measuredObjectDn": record["measurementScope"]["managedObjectDn"],
                    "resolvedCellId": _canonical(record["measurementScope"]["cellId"]),
                    "dnBijective": True,
                    "measurementName": required["name"],
                    "value": required["value"],
                    "unit": required["unit"],
                    "aggregation": required["aggregation"],
                    "window": record["window"],
                    "observedAt": record["observedAt"],
                    "readyAt": record["source"]["file"]["readyAt"],
                    "retrievedAt": record["source"]["file"]["retrievedAt"],
                    "measurementAgeMs": required["measurementAgeMs"],
                    "ingestLatencyMs": required["ingestLatencyMs"],
                    "quality": record["quality"],
                    "ambiguityReason": None,
                    "sampleQualities": [sample["quality"] for sample in record["samples"]],
                    "commitEligible": True,
                    "recordJcsSha256": _jcs_sha256(record),
                    "publishedInExchangeSequence": None,
                }
            )
        return records, {
            "invocations": 1,
            "parserInvocations": 1,
            "commitEligibleCount": len(records),
            "rejectedCount": 0,
            "duplicateCount": duplicate_count,
            "records": summaries,
        }
