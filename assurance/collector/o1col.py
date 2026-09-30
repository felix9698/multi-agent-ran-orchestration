"""Hardware-free O1 PM and KPM source adapters for the Measurement Collector.

The adapters deliberately only turn source records into :class:`RawSample`.
They do not aggregate, decide a verdict, or read a directory/network socket.
The caller supplies PM paths and an injected byte reader; KPM is supplied as
an iterable of JSONL lines.  This makes their evidence boundary testable
without an O1 endpoint or an E2 connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple
import xml.etree.ElementTree as ET

from assurance.collector.collector import SampleSink
from assurance.collector.samples import ClockHealth, MissingInterval, RawSample
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.core.timebase import format_utc, parse_utc

__all__ = [
    "AfterWindowEvidence",
    "KpmJsonlAdapter",
    "KpmParseResult",
    "O1PmFileAdapter",
    "correlate_after_window",
]


_NR_CELL_DU = re.compile(r"(?:^|,)NRCellDU=([^,]+)")
_DURATION = re.compile(r"^PT([1-9][0-9]*)S$")


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> Iterable[ET.Element]:
    return (child for child in element.iter() if _tag(child) == name)


def _child(element: ET.Element, name: str) -> Optional[ET.Element]:
    return next(_children(element, name), None)


def _canonical_timestamp(value: str) -> str:
    return format_utc(parse_utc(value))


def _duration_ms(value: str) -> int:
    match = _DURATION.match(value or "")
    if match is None:
        raise ValueError("PM granularity must be a positive whole-second ISO-8601 duration")
    return int(match.group(1)) * 1000


def _number(value: str) -> int | float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError("measurement value is not numeric") from exc
    if not parsed == parsed or parsed in (float("inf"), float("-inf")):
        raise ValueError("measurement value must be finite")
    return int(parsed) if parsed.is_integer() else parsed


def _unit(counter: str) -> str:
    config_units = {
        "RAN.UE.DlPrbCap": "PRB",
        "RAN.UE.PfWeight": "ratio",
        "RAN.Cell.DlMcsBounds": "MCS-index",
        "RAN.Cell.TxAttenuationDb": "dB",
    }
    if counter in config_units:
        return config_units[counter]
    if counter in {"RRU.PrbDl", "RRU.PrbUl"}:
        return "percent"
    if "Thp" in counter:
        return "kbit/s"
    return "1"


class O1PmFileAdapter:
    """Convert caller-selected TS 32.435 files using an injected byte reader."""

    def __init__(self, *, read_bytes: Callable[[str], bytes], source_id: str = "o1-pm") -> None:
        self._read_bytes = read_bytes
        self._source_id = source_id
        self._sink: Optional[SampleSink] = None
        self._sequences: Dict[str, int] = {}
        self._scope: Mapping[str, str] = {}
        self._clock_health = ClockHealth.UNKNOWN

    def bind_sink(self, sink: SampleSink) -> None:
        if self._sink is not None:
            raise RuntimeError("a collector delivers to exactly one Kernel sink")
        self._sink = sink

    def collect(self, paths: Iterable[str]) -> Tuple[RawSample, ...]:
        """Read exactly *paths*; no polling, directory enumeration, or network I/O."""
        samples = []
        for path in paths:
            raw = self._read_bytes(path)
            if not isinstance(raw, bytes):
                raise TypeError("read_bytes must return bytes")
            samples.extend(self._parse(raw, path))
        for sample in samples:
            self._scope = dict(sample.scope_snapshot)
            self._clock_health = sample.clock_health
            if self._sink is not None:
                self._sink(sample)
        return tuple(samples)

    def poll(self, *, now: str) -> Sequence[RawSample]:
        """Protocol-compatible no-op: path selection belongs to the caller."""
        _canonical_timestamp(now)
        return ()

    def clock_health(self) -> ClockHealth:
        return self._clock_health

    def scope_snapshot(self) -> Mapping[str, str]:
        return dict(self._scope)

    def describe_source(self) -> Mapping[str, Any]:
        return {"sourceId": self._source_id, "kind": "o1-pm-file", "networkAccess": False}

    def _next_sequence(self, counter: str) -> int:
        sequence = self._sequences.get(counter, 0)
        self._sequences[counter] = sequence + 1
        return sequence

    def _parse(self, raw: bytes, path: str) -> Tuple[RawSample, ...]:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise ValueError("PM payload is not well-formed XML") from exc
        if _tag(root) != "measCollecFile":
            raise ValueError("PM payload is not a measCollecFile")
        trace_hash = hashlib.sha256(raw).hexdigest()
        samples = []
        for meas_data in _children(root, "measData"):
            managed = _child(meas_data, "managedElement")
            managed_dn = "" if managed is None else (managed.get("localDn") or "")
            for info in (item for item in meas_data if _tag(item) == "measInfo"):
                granularity = _child(info, "granPeriod")
                if granularity is None:
                    raise ValueError("PM measInfo has no granPeriod")
                cadence_ms = _duration_ms(granularity.get("duration", ""))
                observed_at = _canonical_timestamp(granularity.get("endTime", ""))
                start = format_utc(parse_utc(observed_at) - timedelta(milliseconds=cadence_ms))
                names = {
                    node.get("p"): (node.text or "").strip()
                    for node in (item for item in info if _tag(item) == "measType")
                    if node.get("p") and (node.text or "").strip()
                }
                for value_node in (item for item in info if _tag(item) == "measValue"):
                    ldn = value_node.get("measObjLdn") or ""
                    nr_cell = _NR_CELL_DU.search(ldn)
                    scope = {
                        "managedElementDn": managed_dn,
                        "measObjLdn": ldn,
                        "nrCellDu": nr_cell.group(1) if nr_cell else "",
                    }
                    raw_values = {
                        node.get("p"): (node.text or "").strip()
                        for node in (item for item in value_node if _tag(item) == "r")
                        if node.get("p")
                    }
                    suspect = (_child(value_node, "suspect").text if _child(value_node, "suspect") is not None else "false")
                    suspect = suspect.strip().lower() == "true"
                    for position, counter in names.items():
                        text = raw_values.get(position)
                        missing = text in (None, "", "NIL")
                        # RawSample has a numeric field.  Its missing interval is the
                        # authoritative marker; zero is never a usable replacement.
                        value = 0 if missing else _number(text)
                        missing_intervals = (
                            (MissingInterval(start, observed_at, "O1 NIL or absent value"),)
                            if missing else ()
                        )
                        clock = (ClockHealth.DRIFTING_OUT_OF_BOUND if suspect else ClockHealth.UNKNOWN)
                        sample_id = hashlib.sha256(
                            f"{trace_hash}:{path}:{counter}:{ldn}:{position}".encode("utf-8")
                        ).hexdigest()
                        samples.append(RawSample(
                            sample_id=sample_id, counter_id=counter,
                            value=TypedQuantity(value, _unit(counter), Provenance.MEASURED, sample_id),
                            scope_snapshot=scope, observed_at=observed_at,
                            cadence_ms=cadence_ms, clock_health=clock, trace_hash=trace_hash,
                            sequence=self._next_sequence(counter), missing_intervals=missing_intervals,
                        ))
        return tuple(samples)


class KpmEpochMismatch(ValueError):
    """이 노드의 실측이 **멀쩡한데 우리가 거부해서** 사라졌다.

    2026-09-21: `missing_records` 가 `invalid_records` 의 **문자 그대로의 복사본**이라,
    "JSON 한 줄이 깨졌다"(무해)와 "gNB 가 재기동해 epoch 이 올랐고 그 노드의 KPM 이
    한 줄도 안 통과한다"(치명 -- Kernel 에는 MissingInterval 조차 안 간다)가 한 숫자로
    뭉개졌다.  두 사건은 조치가 완전히 다르므로 칸을 나눈다.
    """


@dataclass(frozen=True)
class KpmParseResult:
    samples: Tuple[RawSample, ...]
    invalid_records: int
    missing_records: int


class KpmJsonlAdapter:
    """Convert KPM format1 and format3 JSONL; malformed/epoch-mismatched lines close."""

    def __init__(self, *, expected_epochs: Mapping[str, int], source_id: str = "a1-kpm") -> None:
        self._expected_epochs = dict(expected_epochs)
        self._source_id = source_id
        self._sink: Optional[SampleSink] = None
        self._sequences: Dict[str, int] = {}
        self._scope: Mapping[str, str] = {}
        self._clock_health = ClockHealth.UNKNOWN

    def bind_sink(self, sink: SampleSink) -> None:
        if self._sink is not None:
            raise RuntimeError("a collector delivers to exactly one Kernel sink")
        self._sink = sink

    def parse_lines(self, lines: Iterable[str]) -> KpmParseResult:
        samples = []
        invalid = 0      # 입력이 못 쓸 상태였다 (깨진 JSON, 모양이 틀린 레코드)
        missing = 0      # 멀쩡한 실측을 우리가 거부해 사라졌다 (epoch 불일치)
        for line_number, line in enumerate(lines, 1):
            try:
                record = json.loads(line)
                produced = self._parse_record(record, line, line_number)
            except KpmEpochMismatch:
                # ValueError 의 하위형이므로 **아래보다 먼저** 잡아야 한다.
                missing += 1
                continue
            except (TypeError, ValueError, json.JSONDecodeError):
                invalid += 1
                continue
            samples.extend(produced)
        for sample in samples:
            self._scope = dict(sample.scope_snapshot)
            self._clock_health = sample.clock_health
            if self._sink is not None:
                self._sink(sample)
        return KpmParseResult(tuple(samples), invalid, missing)

    def poll(self, *, now: str) -> Sequence[RawSample]:
        _canonical_timestamp(now)
        return ()

    def clock_health(self) -> ClockHealth:
        return self._clock_health

    def scope_snapshot(self) -> Mapping[str, str]:
        return dict(self._scope)

    def describe_source(self) -> Mapping[str, Any]:
        return {"sourceId": self._source_id, "kind": "kpm-jsonl", "networkAccess": False}

    def _next_sequence(self, counter: str) -> int:
        sequence = self._sequences.get(counter, 0)
        self._sequences[counter] = sequence + 1
        return sequence

    def _parse_record(self, record: Mapping[str, Any], raw_line: str, line_number: int) -> Tuple[RawSample, ...]:
        if record.get("event") != "kpm_indication":
            raise ValueError("not a kpm indication")
        node = record.get("e2_node")
        epoch = record.get("connection_epoch")
        if not isinstance(node, str) or not isinstance(epoch, int):
            raise ValueError("KPM scope lacks e2 node or connection epoch")
        if self._expected_epochs.get(node) != epoch:
            raise KpmEpochMismatch(
                f"KPM connection epoch mismatch: {node} sent {epoch}, "
                f"we expect {self._expected_epochs.get(node)}")
        received_us = record.get("recv_unix_us")
        if isinstance(received_us, bool) or not isinstance(received_us, int):
            raise ValueError("KPM record lacks recv_unix_us")
        observed_at = format_utc(datetime.fromtimestamp(received_us / 1_000_000, tz=timezone.utc))
        base_scope = {"slot": str(record.get("slot", "")), "e2_node": node, "connection_epoch": str(epoch)}
        trace_hash = hashlib.sha256(raw_line.encode("utf-8")).hexdigest()
        rows: list[tuple[Mapping[str, Any], Mapping[str, str]]] = []
        if isinstance(record.get("measurements"), list):
            rows.extend((metric, base_scope) for metric in record["measurements"] if isinstance(metric, Mapping))
        if isinstance(record.get("ues"), list):
            for ue in record["ues"]:
                if not isinstance(ue, Mapping) or not isinstance(ue.get("measurements"), list):
                    raise ValueError("KPM format3 UE is malformed")
                ue_scope = dict(base_scope)
                for key in ("amf_ue_ngap_id", "ran_ue_id"):
                    if key in ue:
                        ue_scope[key] = str(ue[key])
                if isinstance(ue.get("guami"), Mapping):
                    ue_scope["guami"] = json.dumps(ue["guami"], sort_keys=True, separators=(",", ":"))
                rows.extend((metric, ue_scope) for metric in ue["measurements"] if isinstance(metric, Mapping))
        samples = []
        for metric, scope in rows:
            name, kind, value = metric.get("name"), metric.get("type"), metric.get("value")
            if not isinstance(name, str) or kind not in {"int", "real"} or isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            sample_id = hashlib.sha256(f"{trace_hash}:{line_number}:{name}:{len(samples)}".encode("utf-8")).hexdigest()
            samples.append(RawSample(
                sample_id=sample_id, counter_id=name,
                value=TypedQuantity(value, _unit(name), Provenance.MEASURED, sample_id),
                scope_snapshot=scope, observed_at=observed_at, cadence_ms=1000,
                clock_health=ClockHealth.UNKNOWN, trace_hash=trace_hash,
                sequence=self._next_sequence(name),
            ))
        return tuple(samples)


@dataclass(frozen=True)
class AfterWindowEvidence:
    window_start: str
    window_end: str
    samples: Tuple[RawSample, ...]
    complete: bool


def correlate_after_window(*, observing_started_at: str, contract: Any,
                           samples: Iterable[RawSample], now: str) -> AfterWindowEvidence:
    """Select a contract counter's samples in its first AFTER evidence window.

    ``complete`` says only that the configured window has elapsed.  Coverage,
    gaps, clock health, hold, and verdict remain evaluator-owned Kernel work.
    """
    start = parse_utc(observing_started_at)
    width = getattr(contract, "window_width_ms", None)
    counter_id = getattr(contract, "counter_id", None)
    if isinstance(width, bool) or not isinstance(width, int) or width <= 0 or not isinstance(counter_id, str):
        raise TypeError("contract must expose a positive window_width_ms and counter_id")
    end = start + timedelta(milliseconds=width)
    current = parse_utc(now)
    selected = tuple(
        sample for sample in samples
        if sample.counter_id == counter_id and start <= parse_utc(sample.observed_at) <= end
        and parse_utc(sample.observed_at) <= current
    )
    return AfterWindowEvidence(
        format_utc(start), format_utc(end), selected, current >= end,
    )
