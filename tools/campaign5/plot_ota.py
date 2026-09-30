#!/usr/bin/env python3
"""Plot an OTA case from its immutable Cockpit record and KPM JSONL.

This is deliberately an *offline reader*.  It never opens the deployment path
recorded in a run unless the operator explicitly selects it (or accepts the
binding's ``kpmJsonlPath``); all validation and rendering is local.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import matplotlib
matplotlib.use("Agg")
from matplotlib import pyplot as plt


class PlotError(ValueError):
    """The supplied record cannot safely be attributed or plotted."""


@dataclass(frozen=True)
class Marker:
    at: datetime
    label: str


@dataclass(frozen=True)
class Sample:
    at: datetime
    ue: Optional[str]
    node: Optional[str]
    epoch: Optional[str]
    measurements: Mapping[str, float]


RUN_SCHEMAS = {"liveconsole-run/1.0.0", "liveconsole-run/1.1.0"}
GATEWAY_KINDS = {"PREPARE", "READY", "COMMIT", "REVERSE_ROLLBACK", "FINALIZE"}
UE_METRICS = ("DRB.UEThpDl", "DRB.PdcpSduVolumeDL", "RRU.PrbTotDl",
              "RAN.UE.DlPrbCap", "RAN.UE.PfWeight")
CELL_METRICS = ("RRU.PrbTotDl", "RRC.ConnMean", "RAN.Cell.DlMcsBounds",
                "RAN.Cell.TxAttenuationDb", "MR.NRScSSSINR", "L1M.SS-RSRP")
SERVING_METRICS = ("E2:UE.ServingCell", "UE.ServingCell", "servingCell")


def _instant(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _node(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    match = re.search(r"nb=(\d+)", text)
    return str(int(match.group(1))) if match else text


def _walk(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def load_run(path: Path) -> Mapping[str, Any]:
    with path.open(encoding="utf-8") as handle:
        run = json.load(handle)
    schema = run.get("schemaVersion")
    # D2 records intentionally use the same settled case shape but pre-date the
    # Cockpit schema tag.  Requiring the shape prevents a random JSON document.
    if schema not in RUN_SCHEMAS and not (path.name.startswith("CAMPAIGN5-A1-")
                                         and isinstance(run.get("settlement"), Mapping)):
        raise PlotError(f"{path}: unsupported run schema {schema!r}")
    return run


def events_for(run_path: Path) -> List[Mapping[str, Any]]:
    event_path = run_path.with_name(run_path.name.replace("-run.json", "-events.jsonl"))
    if not event_path.exists():
        raise PlotError(f"{run_path}: companion events JSONL is required")
    result = []
    for number, line in enumerate(event_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PlotError(f"{event_path}:{number}: invalid JSONL") from exc
        if _instant(event.get("timestamp")):
            result.append(event)
    return result


def named_ues(run: Mapping[str, Any]) -> set[Tuple[str, Optional[str], Optional[str]]]:
    """Return the exact (UE, node, epoch) observations named by the case."""
    result = set()
    for item in _walk(run.get("preflight", {})):
        ue = _first(item, "amfUeNgapId", "amf_ue_ngap_id", "ueId")
        epoch = _first(item, "connectionEpoch", "connection_epoch")
        node = _first(item, "e2Node", "e2_node", "nb_id")
        if ue is not None and (epoch is not None or node is not None):
            result.add((str(ue), _node(node), None if epoch is None else str(epoch)))
    # The supplementary participant in liveconsole-run/1.1.0 is a controlled
    # UE and is just as much a named scope as observedUe.
    for item in _walk(run):
        ue = _first(item, "amfUeNgapId", "amf_ue_ngap_id")
        if ue is None or not any(k in item for k in ("connectionEpoch", "connection_epoch", "e2Node", "e2_node", "nb_id")):
            continue
        epoch = _first(item, "connectionEpoch", "connection_epoch")
        node = _first(item, "e2Node", "e2_node", "nb_id")
        result.add((str(ue), _node(node), None if epoch is None else str(epoch)))
    if not result:
        raise PlotError("run has no preflight observed UE tuple")
    return result


def _measurement_map(value: Mapping[str, Any]) -> Dict[str, float]:
    raw = value.get("measurements", value.get("measurement", []))
    entries = raw.items() if isinstance(raw, Mapping) else ((x.get("name"), x) for x in raw if isinstance(x, Mapping))
    result: Dict[str, float] = {}
    for name, entry in entries:
        if not name:
            continue
        if isinstance(entry, Mapping):
            if entry.get("no_value") or entry.get("noValue"):
                continue
            entry = _first(entry, "value", "real", "integer")
        if isinstance(entry, (int, float)) and not isinstance(entry, bool):
            result[str(name)] = float(entry)
    return result


def read_kpm(path: Path) -> List[Sample]:
    samples = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PlotError(f"{path}:{number}: invalid JSONL") from exc
        # Current gate records are flat; accepting a wrapping indication keeps
        # this usable for the Style 1/3 producer representations.
        candidates = [row] + [x for x in _walk(row) if x is not row and "measurements" in x]
        for item in candidates:
            micros = _first(item, "recv_unix_us", "recvUnixUs")
            try:
                at = datetime.fromtimestamp(int(micros) / 1_000_000, tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                at = _instant(_first(item, "timestamp", "observedAt", "receivedAt"))
            measurements = _measurement_map(item)
            if at and measurements:
                ue = _first(item, "amf_ue_ngap_id", "amfUeNgapId")
                samples.append(Sample(at, None if ue is None else str(ue),
                                      _node(_first(item, "nb_id", "nbId", "e2Node")),
                                      None if _first(item, "connection_epoch", "connectionEpoch") is None else str(_first(item, "connection_epoch", "connectionEpoch")), measurements))
    return samples


def attributed(samples: Iterable[Sample], allowed: set[Tuple[str, Optional[str], Optional[str]]]) -> List[Sample]:
    result = []
    for sample in samples:
        if sample.ue is None:                 # Style 1 cell record
            result.append(sample)
            continue
        key = (sample.ue, sample.node, sample.epoch)
        if key in allowed:
            result.append(sample)
    return result


def markers(run: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> List[Marker]:
    result: List[Marker] = []
    for event in events:
        at = _instant(event.get("timestamp"))
        if not at:
            continue
        kind, payload = event.get("eventKind"), event.get("payload", {})
        if kind == "ContractAdmitted": result.append(Marker(at, "ContractAdmitted"))
        if kind == "GatewayResultRecorded" and isinstance(payload, Mapping):
            token, outcome = payload.get("tokenKind"), payload.get("outcome")
            if token in GATEWAY_KINDS: result.append(Marker(at, f"{token} {outcome or 'RECORDED'}"))
        if kind == "TrialStateChanged" and isinstance(payload, Mapping):
            state = str(payload.get("to", ""))
            if state in {"HOLDING", "EVALUATING", "INCIDENT_LOCKDOWN", "SAFETY_STOPPED"}:
                result.append(Marker(at, {"HOLDING": "hold start", "EVALUATING": "hold end"}.get(state, state)))
    for item in _walk(run.get("policyStatus", [])):
        if item.get("episodeState") == "APPLIED_VERIFIED":
            at = _instant(_first(item, "occurredAt", "observedAt"))
            if at: result.append(Marker(at, "A1 policy APPLIED_VERIFIED"))
    # A 1.1 supplementary action can fail closed before a policy is written.
    # Keep that visible on a conflict/cap graph rather than silently treating it
    # as an empty metric panel.  A run-document timestamp is mandatory for a
    # time marker; an untimestamped assertion remains a table fact, not a made-up
    # point on the x axis.
    for item in _walk(run):
        state = _first(item, "readbackState", "executionState", "outcome")
        if state not in {"COUNTER_ABSENT", "EXEC_ERROR", "REFUSED"}:
            continue
        at = _instant(_first(item, "occurredAt", "observedAt", "timestamp", "endedAt"))
        if at:
            result.append(Marker(at, f"refusal: {state}"))
    return sorted(result, key=lambda item: item.at)


def _anchor(events: Sequence[Mapping[str, Any]]) -> datetime:
    admitted = [_instant(e.get("timestamp")) for e in events if e.get("eventKind") == "ContractAdmitted"]
    if not any(admitted): raise PlotError("events contain no ContractAdmitted timestamp")
    return min(x for x in admitted if x is not None)


def _series(samples: Iterable[Sample], anchor: datetime, names: Sequence[str], *, ue: Optional[bool] = None) -> Dict[str, List[Tuple[float, float, str]]]:
    out: Dict[str, List[Tuple[float, float, str]]] = defaultdict(list)
    for sample in samples:
        if ue is not None and (sample.ue is not None) != ue: continue
        for name in names:
            if name in sample.measurements:
                suffix = f" UE {sample.ue}" if sample.ue is not None else f" cell {_node(sample.node) or '?'}"
                out[name + suffix].append(((sample.at-anchor).total_seconds(), sample.measurements[name], sample.node or ""))
    return out


def _volume_rates(samples: Iterable[Sample], anchor: datetime) -> Dict[str, List[Tuple[float, float, str]]]:
    previous: Dict[Tuple[str, str, str], Tuple[datetime, float]] = {}
    out: Dict[str, List[Tuple[float, float, str]]] = defaultdict(list)
    for sample in sorted(samples, key=lambda item: item.at):
        if sample.ue is None or "DRB.PdcpSduVolumeDL" not in sample.measurements: continue
        key = (sample.ue, sample.node or "", sample.epoch or "")
        value = sample.measurements["DRB.PdcpSduVolumeDL"]
        if key in previous:
            before_at, before = previous[key]; elapsed = (sample.at-before_at).total_seconds()
            if elapsed > 0 and value >= before:
                out[f"DRB.PdcpSduVolumeDL rate UE {sample.ue}"].append(((sample.at-anchor).total_seconds(), (value-before)*8/elapsed/1e6, sample.node or ""))
        previous[key] = (sample.at, value)
    return out


def _policy_occupancy(run: Mapping[str, Any], anchor: datetime) -> Dict[str, List[Tuple[float, float, str]]]:
    """An explicit status timeline; absent/refused policies never become writes."""
    result: Dict[str, List[Tuple[float, float, str]]] = defaultdict(list)
    for entry in run.get("policyStatus", []):
        if not isinstance(entry, Mapping):
            continue
        status = entry.get("status", {}).get("aicStatus", {})
        at = _instant(_first(status, "occurredAt", "observedAt"))
        if at:
            name = "policy " + str(entry.get("policyId", "unknown"))[:8]
            result[name].append(((at-anchor).total_seconds(), 1.0 if entry.get("present") else 0.0, ""))
    return result


def _draw(ax: Any, series: Mapping[str, Sequence[Tuple[float, float, str]]], marker_list: Sequence[Marker], anchor: datetime, title: str) -> None:
    for name, points in sorted(series.items()):
        if points: ax.plot([p[0] for p in points], [p[1] for p in points], label=name)
    for marker in marker_list:
        x = (marker.at-anchor).total_seconds()
        ax.axvline(x, color="black", linewidth=.7)
        ax.text(x, .98, marker.label, color="black", fontweight="normal", fontsize=7,
                rotation=90, va="top", transform=ax.get_xaxis_transform())
    ax.set_title(title); ax.set_xlabel("seconds relative to ContractAdmitted")
    ax.grid(True, alpha=.25)
    if series: ax.legend(fontsize=7, loc="best")


def _write_csv(path: Path, series: Mapping[str, Sequence[Tuple[float, float, str]]], marker_list: Sequence[Marker], anchor: datetime) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle); writer.writerow(["relative_seconds", "series", "value", "node_or_cell", "marker"])
        for name, points in sorted(series.items()):
            for seconds, value, node in points: writer.writerow([f"{seconds:.6f}", name, value, node, ""])
        for marker in marker_list: writer.writerow([f"{(marker.at-anchor).total_seconds():.6f}", "", "", "", marker.label])


def _latency_anchors(events: Sequence[Mapping[str, Any]], marker_list: Sequence[Marker]) -> Tuple[Optional[datetime], str, Optional[datetime], str, str]:
    """Return the two observed anchors, never substituting COMMIT's ACK time.

    A live adapter can verify inside ``commit``.  Its APPLIED_VERIFIED status is
    therefore allowed to precede the later COMMIT ACK record; ACK is an outcome,
    not the start of the operation being measured.
    """
    issued: Dict[str, datetime] = {}
    ready: Optional[datetime] = None
    prepare: Optional[datetime] = None
    for event in events:
        payload = event.get("payload", {})
        if not isinstance(payload, Mapping):
            continue
        kind = payload.get("tokenKind")
        if kind not in {"COMMIT", "READY", "PREPARE"}:
            continue
        at = _instant(_first(payload, "issuedAt", "requestTimestamp", "requestedAt"))
        if at is None and event.get("eventKind") == "TokenIssued":
            at = _instant(event.get("timestamp"))
        if at is not None:
            issued[str(kind)] = at
    for marker in marker_list:
        if marker.label == "READY ACKED": ready = marker.at
        elif marker.label == "PREPARE ACKED": prepare = marker.at
    if issued.get("COMMIT"):
        start, start_name = issued["COMMIT"], "COMMIT issue"
    elif ready:
        start, start_name = ready, "READY ACKED fallback"
    elif prepare:
        start, start_name = prepare, "PREPARE ACKED fallback"
    else:
        start, start_name = None, "missing COMMIT issue and PREPARE/READY ACKED"
    end = next((marker.at for marker in marker_list
                if marker.label == "A1 policy APPLIED_VERIFIED"), None)
    return start, start_name, end, "A1 policy APPLIED_VERIFIED" if end else "missing APPLIED_VERIFIED", ""


def _case_table(run: Mapping[str, Any], events: Sequence[Mapping[str, Any]], marker_list: Sequence[Marker], anchor: datetime) -> Dict[str, Any]:
    first = min((_instant(e.get("timestamp")) for e in events if _instant(e.get("timestamp"))), default=anchor)
    latency_start, start_name, verified, end_name, latency_reason = _latency_anchors(events, marker_list)
    latency: Any = ""
    if latency_start is None or verified is None:
        latency_reason = f"{start_name}; {end_name}"
    else:
        candidate = (verified-latency_start).total_seconds()
        if candidate < 0:
            latency_reason = ("APPLIED_VERIFIED precedes " + start_name +
                              "; refusing a negative latency")
        else:
            latency = round(candidate, 6)
    holds = [m.at for m in marker_list if m.label in {"hold start", "hold end"}]
    journal = [e for e in events if e.get("eventKind") == "GatewayResultRecorded"]
    settlement = run.get("settlement", {})
    return {"case": run.get("caseId", ""), "time_to_admit_s": round((anchor-first).total_seconds(), 6),
            "commit_issue_or_prepare_ready_acked_to_applied_verified_s": latency,
            "latency_start_anchor": start_name, "latency_end_anchor": end_name,
            "latency_unavailable_reason": latency_reason,
            "hold_duration_s": "" if len(holds) < 2 else round((holds[1]-holds[0]).total_seconds(), 6),
            "harm_reserved_returned": "; ".join(map(str, settlement.get("harmCharges", []))),
            "predicate_verdicts": json.dumps(run.get("axes", {}), sort_keys=True),
            "termination": settlement.get("caseTermination", ""), "write_count": len(journal),
            "refusal_latency_s": "" if not run.get("refusal") else round((anchor-first).total_seconds(), 6)}


def plot_case(run_path: Path, kpm_path: Optional[Path], out: Path, scenario: str, tables: bool = False, requested_ue: Optional[str] = None) -> List[Path]:
    run, events = load_run(run_path), events_for(run_path)
    allowed = named_ues(run)
    if requested_ue is not None and requested_ue not in {x[0] for x in allowed}:
        raise PlotError(f"UE {requested_ue} is not named by the run; refusing attribution")
    anchor = _anchor(events); marker_list = markers(run, events)
    samples = attributed(read_kpm(kpm_path), allowed) if kpm_path is not None else []
    if not samples and scenario != "conflict": raise PlotError("no KPM samples match a named (UE, nb_id, connection_epoch) tuple")
    per_ue = _series(samples, anchor, UE_METRICS, ue=True); per_ue.update(_volume_rates(samples, anchor))
    per_cell = _series(samples, anchor, CELL_METRICS, ue=False)
    serving = _series(samples, anchor, SERVING_METRICS, ue=True)
    all_series = dict(per_ue); all_series.update(per_cell)
    if scenario == "steer": panels = [("UE serving cell", serving), ("UE throughput", {k:v for k,v in per_ue.items() if "Thp" in k or "Volume" in k}), ("both cells PRB", {k:v for k,v in per_cell.items() if "Prb" in k})]
    elif scenario == "steer_cap": panels = [("objective and controlled throughput", {k:v for k,v in per_ue.items() if "Thp" in k or "Volume" in k}), ("controlled cap and objective serving cell", dict({k:v for k,v in per_ue.items() if "DlPrbCap" in k}, **serving))]
    elif scenario == "conflict": panels = [("policy occupancy (writes=0 refusals are markers)", _policy_occupancy(run, anchor))]
    elif scenario == "family": panels = [("family readback and throughput", all_series)]
    elif scenario == "interference": panels = [("cap and external write readback", {k:v for k,v in per_ue.items() if "DlPrbCap" in k}), ("throughput", {k:v for k,v in per_ue.items() if "Thp" in k or "Volume" in k})]
    else: raise PlotError(f"unknown scenario {scenario!r}")
    out.mkdir(parents=True, exist_ok=True)
    base = out / f"{run_path.stem.replace('-run', '')}-{scenario}"
    figure, axes = plt.subplots(len(panels), 1, figsize=(12, 3.3 * len(panels)), squeeze=False)
    exported: Dict[str, List[Tuple[float, float, str]]] = {}
    for axis, (title, values) in zip(axes[:, 0], panels): _draw(axis, values, marker_list, anchor, title); exported.update(values)
    figure.tight_layout(); png = base.with_suffix(".png"); figure.savefig(png, dpi=150); plt.close(figure)
    csv_path = base.with_suffix(".csv"); _write_csv(csv_path, exported, marker_list, anchor)
    outputs = [png, csv_path]
    if tables:
        table = out / f"{run_path.stem.replace('-run', '')}-table.csv"
        row = _case_table(run, events, marker_list, anchor)
        with table.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row)); writer.writeheader(); writer.writerow(row)
        outputs.append(table)
    return outputs


def _binding_kpm(run: Mapping[str, Any]) -> Optional[Path]:
    for item in _walk(run.get("preflight", {})):
        value = _first(item, "kpmJsonlPath", "kpm_jsonl_path")
        if isinstance(value, str): return Path(value)
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, type=Path, help="LIVECONSOLE- or CAMPAIGN5-A1- run JSON")
    parser.add_argument("--kpm-jsonl", type=Path, help="KPM gate JSONL (otherwise binding kpmJsonlPath)")
    parser.add_argument("--out", required=True, type=Path); parser.add_argument("--scenario", required=True, choices=("steer", "steer_cap", "conflict", "family", "interference"))
    parser.add_argument("--tables", action="store_true"); parser.add_argument("--ue", help="refuse unless this UE is named by every run")
    args = parser.parse_args(argv)
    for run_path in args.run:
        run = load_run(run_path); kpm = args.kpm_jsonl or _binding_kpm(run)
        if kpm is None and args.scenario != "conflict":
            raise PlotError("no --kpm-jsonl and no binding kpmJsonlPath")
        if kpm is not None and not kpm.exists():
            if args.scenario == "conflict": kpm = None
            else: raise PlotError(f"KPM JSONL does not exist: {kpm}")
        for output in plot_case(run_path, kpm, args.out, args.scenario, args.tables, args.ue): print(output)
    return 0


if __name__ == "__main__":
    try: raise SystemExit(main())
    except PlotError as exc: raise SystemExit(f"plot_ota: {exc}")
