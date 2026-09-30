"""Metric availability index for the SessionStore.

Owner: track **T3**.

Neutral by location as well as by dependency: it lives at the top level so
``assurance/batch/`` can share the one run-directory schema without importing a
console (Gate 2).  ``gui/operator/store/metric_index.py`` re-exports it unchanged.

Authority: ``docs/phase-b-gui/metric-registry.1.0.0.json``.

phaseB_task.md section 4 forbids synthesizing an unsupported KPI or substituting
a different one for it, and requires every metric to display its availability,
source, scope, unit, sampling interval, timestamp and quality.  This module is
the mechanical form of that rule: the registry decides what *may* exist for a
given source, the capability manifest decides what this deployment *declares*,
and the observed samples decide what actually *arrived*.  Nothing else may add a
metric to the index, and no metric may leave it.

The discovery rules, verbatim from the registry:

* the capability manifest's ``decisionKpis`` / ``assuranceKpis`` are the
  authoritative list for a deployment; the registry supplies presentation
  metadata for ids it knows;
* a metric the manifest declares and the registry does not know is still
  plotted, with unit and scope from the manifest and its id as the display name -
  it is never dropped;
* a metric the registry knows and the manifest does not declare renders
  ``UNSUPPORTED`` with ``NOT_DECLARED_BY_CAPABILITY_MANIFEST``;
* **without** a manifest, every O-RAN-sourced metric renders ``UNKNOWN`` with
  ``CAPABILITY_MANIFEST_ABSENT``.  It does not fall back to a hard-coded list,
  because a hard-coded list is exactly the fixed-testbed assumption section 6
  forbids.

Stdlib-only, no toolkit import: the export path and the hermetic tests both use
it without a display.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Final, Iterable, Mapping, Optional

from runstore.records import metric_index_entry

#: Repository-relative path to the registry.  Read at call time rather than at
#: import time so a test can point at a fixture registry.
REGISTRY_PATH: Final[Path] = (
    Path(__file__).resolve().parents[1] / "docs" / "phase-b-gui"
    / "metric-registry.1.0.0.json")

#: Adapter/source id -> the registry's availability key.
AVAILABILITY_KEY: Final[Dict[str, str]] = {
    "live": "live",
    "lo1-capture": "replayLo1Capture",
    "experiment-run": "replayExperimentRun",
    # The registry declares no availability under this key, so every registry
    # metric resolves UNSUPPORTED for a Kernel event stream.  That is the true
    # answer -- an assurance stream carries the counters its contracts bound and
    # none of the nineteen KPI metrics -- and it is reached by the registry's
    # own default rather than by nineteen new entries asserting it.
    "liveconsole-run": "replayLiveConsole",
}

#: Source classes that come through an O-RAN boundary and therefore depend on the
#: capability manifest.  ``EXPERIMENT_RECORD`` and ``RAPP_INTERNAL`` do not: they
#: are measured by the research path and by the coordinator itself, and a missing
#: manifest says nothing about them.
MANIFEST_GOVERNED_SOURCES: Final[tuple] = ("O1_DME", "R1_POLICY", "R1_DME")

REASON_NOT_DECLARED: Final[str] = "NOT_DECLARED_BY_CAPABILITY_MANIFEST"
REASON_NO_MANIFEST: Final[str] = "CAPABILITY_MANIFEST_ABSENT"
REASON_NO_SAMPLES: Final[str] = "DECLARED_BUT_NO_SAMPLE_DELIVERED"

_REGISTRY_CACHE: Dict[str, Any] = {}


def load_registry(path: Optional[Path] = None) -> Mapping[str, Any]:
    """Load and memoize the metric registry."""
    target = Path(path or REGISTRY_PATH)
    key = str(target)
    if key not in _REGISTRY_CACHE:
        _REGISTRY_CACHE[key] = json.loads(target.read_text(encoding="utf-8"))
    return _REGISTRY_CACHE[key]


def registry_metrics(path: Optional[Path] = None) -> Dict[str, Mapping[str, Any]]:
    """The registry keyed by metric id."""
    return {m["id"]: m for m in load_registry(path)["metrics"]}


def declared_metrics(manifest: Optional[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    """Metric ids the capability manifest declares, with their declared metadata.

    ``decisionKpis`` and ``assuranceKpis`` are merged; where both declare the same
    id the assurance entry wins, because that is the one delivered as evidence
    over the O1 path this console can actually read.
    """
    if not manifest:
        return {}
    found: Dict[str, Mapping[str, Any]] = {}
    for key in ("decisionKpis", "assuranceKpis"):
        for entry in manifest.get(key) or ():
            if isinstance(entry, Mapping) and entry.get("name"):
                found[str(entry["name"])] = entry
            elif isinstance(entry, str):
                found.setdefault(entry, {"name": entry})
    return found


class MetricIndexBuilder:
    """Accumulates observed samples and emits ``normalized/metric-index.json``.

    Usage: construct with the source id and the capability manifest (or ``None``),
    feed it every telemetry sample the adapter wrote, then call :meth:`build`.
    The builder never invents a metric and never drops one.
    """

    def __init__(self, *, source_id: str,
                 capability_manifest: Optional[Mapping[str, Any]] = None,
                 registry_path: Optional[Path] = None) -> None:
        self.source_id = source_id
        self.availability_key = AVAILABILITY_KEY.get(source_id, "live")
        self.manifest = capability_manifest
        self.declared = declared_metrics(capability_manifest)
        self.registry = registry_metrics(registry_path)
        self._observed: Dict[str, Dict[str, Any]] = {}

    # -- accumulation ------------------------------------------------------- #

    def observe(self, sample: Mapping[str, Any]) -> None:
        """Fold one telemetry sample into the running availability picture."""
        metric = sample["metric"]
        seen = self._observed.setdefault(metric, {
            "count": 0, "scope_ids": [], "qualities": {}, "first": None,
            "last": None, "unit": sample.get("unit", ""),
            "scope_level": sample.get("scope", {}).get("level", "RUN"),
            "interval": sample.get("samplingIntervalMs"),
            "boundary": sample.get("source", {}).get("boundary"),
            "derived": bool(sample.get("derived")),
            "real_values": 0,
        })
        seen["count"] += 1
        if sample.get("value") is not None:
            seen["real_values"] += 1
        scope_id = sample.get("scope", {}).get("id")
        if scope_id and scope_id not in seen["scope_ids"]:
            seen["scope_ids"].append(scope_id)
        quality = sample.get("quality", "OK")
        seen["qualities"][quality] = seen["qualities"].get(quality, 0) + 1
        stamp = sample.get("tUtc")
        if stamp:
            if seen["first"] is None or stamp < seen["first"]:
                seen["first"] = stamp
            if seen["last"] is None or stamp > seen["last"]:
                seen["last"] = stamp

    def observe_all(self, samples: Iterable[Mapping[str, Any]]) -> None:
        for sample in samples:
            self.observe(sample)

    # -- resolution --------------------------------------------------------- #

    def _source_class(self, spec: Mapping[str, Any]) -> str:
        sources = spec.get("sources") or ["NONE"]
        return str(sources[0])

    def _resolve(self, metric_id: str, spec: Mapping[str, Any]) -> Dict[str, Any]:
        availability = (spec.get("availability") or {}).get(
            self.availability_key, "UNSUPPORTED")
        registry_reason = (spec.get("availability") or {}).get("reason")
        source_class = self._source_class(spec)
        observed = self._observed.get(metric_id)
        count = observed["count"] if observed else 0
        scope_level = spec.get("scopeLevel", "RUN")
        unit = spec.get("unit", "")
        gap_id = spec.get("gapId")

        common = dict(source=source_class, scope_level=scope_level, unit=unit,
                      gap_id=gap_id)

        if availability == "UNSUPPORTED":
            return metric_index_entry(
                status="UNSUPPORTED",
                reason=registry_reason or "no source declares this metric",
                **common)

        if availability == "UNAVAILABLE":
            return metric_index_entry(
                status="UNAVAILABLE",
                reason=registry_reason or "the source delivered no value",
                **common)

        if availability in ("AVAILABLE_IF_DECLARED",):
            if self.manifest is None:
                return metric_index_entry(status="UNKNOWN",
                                          reason=REASON_NO_MANIFEST, **common)
            if metric_id not in self.declared:
                return metric_index_entry(status="UNSUPPORTED",
                                          reason=REASON_NOT_DECLARED, **common)

        # A metric that declares inputs is UNSUPPORTED until they are measured,
        # whatever its availability wording says.  This is the registry's
        # constellation rule in general form: the derived view falls back only
        # when a real series exists, and it must NEVER draw a cloud from no
        # input - the behaviour the legacy dashboard had and this one drops.
        required = spec.get("requires") or ()
        missing = [r for r in required if not self._observed.get(r)]
        if missing:
            return metric_index_entry(
                status="UNSUPPORTED",
                reason=("a derived visualization requires a measured "
                        + ", ".join(missing)),
                **common)

        if count == 0:
            reason = (REASON_NO_SAMPLES if metric_id in self.declared
                      else "no sample was recorded for this run")
            return metric_index_entry(status="UNAVAILABLE", reason=reason, **common)

        return self._observed_entry(metric_id, spec, observed)

    def _observed_entry(self, metric_id: str, spec: Mapping[str, Any],
                        observed: Mapping[str, Any]) -> Dict[str, Any]:
        qualities = observed["qualities"]
        status = "OK"
        reason = None
        if observed["real_values"] == 0:
            status, reason = "UNAVAILABLE", "every recorded sample is null"
        elif qualities.get("STALE"):
            status, reason = "STALE", f"{qualities['STALE']} stale sample(s)"
        elif qualities.get("MISSING") or qualities.get("NOT_AVAILABLE"):
            status = "DEGRADED"
            reason = (f"{qualities.get('MISSING', 0) + qualities.get('NOT_AVAILABLE', 0)}"
                      " sample(s) carry no value")
        elif qualities.get("SUSPECT") or qualities.get("AMBIGUOUS"):
            status = "DEGRADED"
            reason = "the source flagged sample quality"
        return metric_index_entry(
            status=status, reason=reason,
            source=self._source_class(spec),
            scope_level=observed["scope_level"] or spec.get("scopeLevel", "RUN"),
            unit=observed["unit"] or spec.get("unit", ""),
            gap_id=spec.get("gapId"),
            scope_ids=observed["scope_ids"],
            sampling_interval_ms=observed["interval"],
            sample_count=observed["count"],
            first_sample_at=observed["first"],
            last_sample_at=observed["last"],
            quality_counts=qualities,
            derived=observed["derived"])

    # -- output ------------------------------------------------------------- #

    def build(self) -> Dict[str, Any]:
        """The complete index: every registry metric plus every declared one."""
        index: Dict[str, Any] = {}
        for metric_id, spec in self.registry.items():
            index[metric_id] = self._resolve(metric_id, spec)

        # A manifest-declared metric the registry does not know is still plotted,
        # with unit and scope taken from the manifest.  Dropping it would hide a
        # capability this deployment actually offers.
        for metric_id, declared in self.declared.items():
            if metric_id in index:
                continue
            observed = self._observed.get(metric_id)
            unit = str(declared.get("unit", ""))
            scope_level = "CELL" if declared.get("managedObjectClass") else "RUN"
            spec = {"sources": ["O1_DME"], "unit": unit, "scopeLevel": scope_level}
            if observed:
                index[metric_id] = self._observed_entry(metric_id, spec, observed)
            else:
                index[metric_id] = metric_index_entry(
                    status="UNAVAILABLE", reason=REASON_NO_SAMPLES,
                    source="O1_DME", scope_level=scope_level, unit=unit)

        # Anything observed that neither the registry nor the manifest names is a
        # genuine surprise.  It is recorded as UNKNOWN rather than discarded: a
        # sample that exists and is invisible is the worse failure.
        for metric_id, observed in self._observed.items():
            if metric_id in index:
                continue
            index[metric_id] = metric_index_entry(
                status="UNKNOWN",
                reason="observed but declared by neither the registry nor the "
                       "capability manifest",
                source=observed["boundary"] or "RAPP_INTERNAL",
                scope_level=observed["scope_level"], unit=observed["unit"],
                scope_ids=observed["scope_ids"],
                sample_count=observed["count"],
                first_sample_at=observed["first"],
                last_sample_at=observed["last"],
                quality_counts=observed["qualities"],
                derived=observed["derived"])
        return index

    def display_names(self) -> Dict[str, str]:
        """Display name per metric id, for the availability panel."""
        names = {mid: spec.get("display", mid)
                 for mid, spec in self.registry.items()}
        for metric_id in self.declared:
            names.setdefault(metric_id, metric_id)
        return names


__all__ = [
    "AVAILABILITY_KEY", "MANIFEST_GOVERNED_SOURCES", "MetricIndexBuilder",
    "REASON_NOT_DECLARED", "REASON_NO_MANIFEST", "REASON_NO_SAMPLES",
    "REGISTRY_PATH", "declared_metrics", "load_registry", "registry_metrics",
]
