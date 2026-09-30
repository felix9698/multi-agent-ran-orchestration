"""Raw and processed data export.

Owner: track **T3**.

phaseB_task.md section 4 requires the raw and the processed data to be
preservable and exportable in a re-analysable general format.  Three rules make
an export actually re-analysable rather than merely present:

1. **Raw exports byte-identically.**  A raw artifact is evidence; re-encoding it,
   pretty-printing it or normalizing its line endings would break the digest the
   manifest recorded and destroy the thing that made it evidence.
2. **Every file carries ``mode`` and ``runId``.**  A CSV separated from its run
   directory still says what it is.  A REPLAY export that reads as LIVE once it
   is in a spreadsheet is exactly the confusion §9 forbids.
3. **A null exports as an empty field, never as a zero.**  The CSV writer has an
   explicit empty-string rendering for ``None``, so a gap survives the round trip
   into a spreadsheet, where a ``0`` would be indistinguishable from a
   measurement.
4. **Every exported sample carries its metric's availability.**  A number on its
   own is not a measurement: the registry's status, its reason and its gap id
   travel in the same row.  A metric the registry and the capability manifest
   both fail to declare exports as ``UNKNOWN`` with that stated, so a value the
   console could not vouch for can never read as a sound measurement once the
   CSV has been detached from its run directory.  This is the rule the earlier
   version broke - it exported a value and a quality of ``OK`` for a metric the
   index had already marked ``UNKNOWN``.

Stdlib only.  No toolkit import: an export runs on a worker thread and, in the
headless paths, with no display at all.
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path
from typing import Any, Dict, Final, Iterable, List, Mapping, Optional, Sequence

#: Column order for the telemetry CSV.  Fixed rather than derived from the first
#: row, so two exports of the same run are byte-identical and a diff is
#: meaningful.
TELEMETRY_COLUMNS: Final[tuple] = (
    "seq", "tUtc", "tRelS", "metric",
    # availability leads the value on purpose: a reader scanning the row meets
    # the status before the number, and a number whose status is not OK is never
    # the first thing read.
    "availability", "availabilityReason", "gapId", "metricSourceClass",
    "value", "unit", "scopeLevel", "scopeId",
    "scopeDn", "sourceBoundary", "sourceInterface", "artifactRef",
    "windowStart", "windowEnd", "aggregation", "samplingIntervalMs", "ageMs",
    "quality", "derived", "derivation",
)

#: What an exported row says about a metric the run's index does not know.
#: Fail-closed: absence of an availability record is not evidence of
#: availability.
UNDECLARED_AVAILABILITY: Final[str] = "UNKNOWN"
UNDECLARED_REASON: Final[str] = (
    "this metric is absent from the run's metric index: neither the metric "
    "registry nor the capability manifest declares it, so the console cannot "
    "vouch for the value")

EVENT_COLUMNS: Final[tuple] = (
    "seq", "tUtc", "tRelS", "lane", "kind", "severity", "origin", "component",
    "title", "runId", "intentId", "policyId", "episodeId", "evidenceId",
    "correlationId", "annotatable", "derivation", "sourceRef",
)

CYCLE_COLUMNS: Final[tuple] = (
    "episodeId", "cycleIndex", "feasible", "rawConfidence",
    "calibratedProbability", "threshold", "thresholdAppliedTo", "routedTo",
)

EPISODE_COLUMNS: Final[tuple] = (
    "episodeId", "intentText", "terminalOutcome", "eq12State", "success",
    "hasConflict", "latencyMs", "negotiationRounds", "rolledBack",
)


class ExportError(RuntimeError):
    """An export that cannot be completed without misrepresenting the data."""


def _cell(value: Any) -> str:
    """One CSV cell.

    ``None`` becomes an empty field.  It must never become ``0``: in a
    spreadsheet a zero is a measurement and an empty cell is not, and that
    distinction is the whole reason the store keeps nulls.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def _banner(store, kind: str) -> List[str]:
    """The provenance banner every exported CSV starts with.

    ``mode`` is the mode of this session and leads the line, because it is what
    decides whether the numbers below are being observed.  ``sourceMode`` -
    what the recording was made against - follows it, and never replaces it.
    """
    summary = store.read_summary()
    lines = [
        f"# mode={store.mode} runId={store.run_id} disposition={store.disposition}",
        f"# content={kind} schema={store.SCHEMA_VERSION}",
    ]
    source_mode = summary.get("sourceMode")
    if source_mode and source_mode != store.mode:
        lines.append(f"# sourceMode={source_mode} (the recording's own mode; "
                     f"this session is {store.mode})")
    note = summary.get("sourceProvenanceNote")
    if note:
        lines.append(f"# sourceProvenanceNote={note}")
    return lines


def _write_csv(path: Path, store, kind: str, columns: Sequence[str],
               rows: Iterable[Sequence[Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        for line in _banner(store, kind):
            handle.write(line + "\n")
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_cell(v) for v in row])
    return path


def _write_json(path: Path, store, kind: str, payload: Any) -> Path:
    """JSON with the same banner, as a wrapper object rather than a comment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "mode": store.mode,
        "runId": store.run_id,
        "disposition": store.disposition,
        "schemaVersion": store.SCHEMA_VERSION,
        "content": kind,
        "data": payload,
    }
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False,
                   allow_nan=False) + "\n", encoding="utf-8")
    return path


def availability_of(metric_index: Mapping[str, Any],
                    metric: str) -> Dict[str, Any]:
    """The registry availability an exported sample must carry.

    A metric missing from the index is ``UNKNOWN`` with the reason spelled out -
    never an empty cell, which a reader would fill in with an assumption.
    """
    entry = (metric_index or {}).get(metric)
    if not isinstance(entry, Mapping):
        return {"availability": UNDECLARED_AVAILABILITY,
                "reason": UNDECLARED_REASON, "gapId": None, "source": None}
    status = str(entry.get("status", UNDECLARED_AVAILABILITY))
    return {
        "availability": status,
        "reason": entry.get("reason") or ("" if status == "OK"
                                          else "no reason recorded"),
        "gapId": entry.get("gapId"),
        "source": entry.get("source"),
    }


def _telemetry_rows(store) -> Iterable[Sequence[Any]]:
    metric_index = store.read_metric_index()
    for sample in store.read_telemetry():
        scope = sample.get("scope") or {}
        source = sample.get("source") or {}
        window = sample.get("window") or {}
        status = availability_of(metric_index, str(sample.get("metric")))
        yield [
            sample.get("seq"), sample.get("tUtc"), sample.get("tRelS"),
            sample.get("metric"),
            status["availability"], status["reason"], status["gapId"],
            status["source"],
            sample.get("value"), sample.get("unit"),
            scope.get("level"), scope.get("id"), scope.get("dn"),
            source.get("boundary"), source.get("interface"),
            source.get("artifactRef"),
            window.get("start"), window.get("end"),
            sample.get("aggregation"), sample.get("samplingIntervalMs"),
            sample.get("ageMs"), sample.get("quality"), sample.get("derived"),
            sample.get("derivation"),
        ]


def _event_rows(store) -> Iterable[Sequence[Any]]:
    for event in store.read_events():
        ids = event.get("ids") or {}
        yield [
            event.get("seq"), event.get("tUtc"), event.get("tRelS"),
            event.get("lane"), event.get("kind"), event.get("severity"),
            event.get("origin"), event.get("component"), event.get("title"),
            ids.get("runId"), ids.get("intentId"), ids.get("policyId"),
            ids.get("episodeId"), ids.get("evidenceId"), ids.get("correlationId"),
            event.get("annotatable"), event.get("derivation"),
            event.get("sourceRef"),
        ]


def _episode_rows(store) -> Iterable[Sequence[Any]]:
    for episode in store.read_episodes():
        yield [episode.get(key) for key in EPISODE_COLUMNS]


def _cycle_rows(store) -> Iterable[Sequence[Any]]:
    for cycle in store.read_cycles():
        yield [cycle.get(key) for key in CYCLE_COLUMNS]


def export_raw(store, destination) -> List[str]:
    """Copy ``raw/`` byte-for-byte.

    ``shutil.copy2`` rather than a read/write loop so metadata is preserved and
    so nothing in this path can accidentally re-encode a byte.
    """
    source = Path(store.run_dir) / "raw"
    if not source.is_dir():
        return []
    target = Path(destination) / "raw"
    written = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(source)
        out = target / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out)
        written.append(str(out.relative_to(destination)))
    return written


def export_run(store, destination, *, include_raw: bool = True,
               formats: Sequence[str] = ("csv", "json")) -> Dict[str, Any]:
    """Export one stored run into ``destination``.

    Returns the export manifest: what was written, from which run, in which
    mode.  The manifest is itself written as ``EXPORT-MANIFEST.json`` so an
    export directory is self-describing after it has been moved or zipped.
    """
    for name in formats:
        if name not in ("csv", "json"):
            raise ExportError(f"unsupported export format {name!r}")

    out = Path(destination)
    out.mkdir(parents=True, exist_ok=True)
    written: List[str] = []

    if "csv" in formats:
        written.append(str(_write_csv(
            out / "telemetry.csv", store, "normalized telemetry",
            TELEMETRY_COLUMNS, _telemetry_rows(store)).relative_to(out)))
        written.append(str(_write_csv(
            out / "events.csv", store, "event timeline",
            EVENT_COLUMNS, _event_rows(store)).relative_to(out)))
        written.append(str(_write_csv(
            out / "episodes.csv", store, "decision episodes",
            EPISODE_COLUMNS, _episode_rows(store)).relative_to(out)))
        written.append(str(_write_csv(
            out / "cycles.csv", store, "decision cycles",
            CYCLE_COLUMNS, _cycle_rows(store)).relative_to(out)))

    metric_index = store.read_metric_index()
    if "json" in formats:
        # The samples and the availability they are read under travel in ONE
        # document, so the two cannot be separated by a copy.
        written.append(str(_write_json(
            out / "telemetry.json", store, "normalized telemetry",
            {"metricIndex": metric_index,
             "undeclaredMetricRule": UNDECLARED_REASON,
             "samples": [
                 {**sample,
                  "availability": availability_of(
                      metric_index, str(sample.get("metric")))}
                 for sample in store.read_telemetry()]}).relative_to(out)))
        written.append(str(_write_json(
            out / "events.json", store, "event timeline",
            list(store.read_events())).relative_to(out)))
        written.append(str(_write_json(
            out / "decision.json", store, "decision trace",
            {"episodes": list(store.read_episodes()),
             "cycles": list(store.read_cycles()),
             "llmCalls": list(store.read_llm_calls())}).relative_to(out)))
        written.append(str(_write_json(
            out / "summary.json", store, "run summary",
            {"summary": store.read_summary(),
             "statistics": store.read_statistics(),
             "metricIndex": store.read_metric_index(),
             "configSnapshot": store.read_config_snapshot()}).relative_to(out)))

    if include_raw:
        written.extend(export_raw(store, out))

    manifest = store.read_manifest()
    summary = store.read_summary()
    observed = sorted({str(s.get("metric")) for s in store.read_telemetry()})
    export_manifest = {
        "mode": store.mode,
        "modeNote": "the mode of the SESSION this run recorded. A run read back "
                    "from a recording is REPLAY whatever the recording was made "
                    "against; sourceMode below says what that was.",
        "sourceMode": summary.get("sourceMode"),
        "sourceProvenanceNote": summary.get("sourceProvenanceNote"),
        "paperReady": summary.get("paperReady"),
        "runId": store.run_id,
        "disposition": store.disposition,
        "schemaVersion": store.SCHEMA_VERSION,
        # Availability for every metric that produced a sample, so the manifest
        # alone answers "may I trust this column".
        "metricAvailability": {
            metric: availability_of(metric_index, metric) for metric in observed},
        # A non-COMPLETED run exports its data and says so.  Nothing here
        # summarizes a partial run as a result.
        "isSuccess": store.is_success,
        "sources": manifest.get("sources", []),
        "counts": manifest.get("counts", {}),
        # The issue list travels with the export: a reader who receives only
        # this directory must still see what was missing, stale or partial.
        "dataIssues": manifest.get("dataIssues", []),
        "files": sorted(written),
    }
    (out / "EXPORT-MANIFEST.json").write_text(
        json.dumps(export_manifest, indent=2, sort_keys=True,
                   ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8")
    return export_manifest


__all__ = [
    "CYCLE_COLUMNS", "EPISODE_COLUMNS", "EVENT_COLUMNS", "ExportError",
    "TELEMETRY_COLUMNS", "export_raw", "export_run",
]
