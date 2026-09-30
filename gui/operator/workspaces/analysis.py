"""Analysis & Results workspace.

Owner: track **T3**.  Requirement: phaseB_task.md section 4.

The workspace is three things stacked:

``RunView``
    A plain-data snapshot of one stored run - samples, metric index, events,
    summary, statistics, config snapshot.  Built by :func:`load_run_view`,
    which is a **worker-thread** function: it is the only place in this module
    that touches a store, and no workspace method calls it.  That is what keeps
    the Tk thread free of I/O, and it is what the responsiveness gate asserts by
    AST.

:class:`AnalysisModel`
    Every decision the workspace makes - which metrics exist, what a tile says,
    which series a chart draws, what the goal line is, how two runs compare.
    Pure python, no toolkit, hermetically testable.

:class:`AnalysisWorkspace`
    The Tk rendering.  It reads the model and nothing else.

Four requirements of section 4 are load-bearing and are implemented as rules
rather than as screen furniture:

* **metric availability is a first-class panel**, not a footnote.  Every metric
  is listed with its availability, source class, scope, unit, sampling interval,
  timestamp and quality - including the unsupported ones, with their reason.  A
  metric missing from the UI teaches the operator nothing.
* **the goal line comes from the run's own config snapshot.**  8.0 Mbps offline
  and 3.5 live; a module constant would mislabel half the runs.
* **series from different source classes never share a line**, even when they
  nominally measure the same quantity.  ``DRB.UEThpDl`` from O1 and
  ``throughput_mbps`` from the experiment record are different measurements from
  different paths.
* **repeat-run comparison preserves n and the experimental conditions**, and its
  statistics come from ``experiments/metrics.py`` so they equal the paper
  pipeline's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, Final, List, Mapping, Optional,
                    Sequence, Tuple)

from gui.operator import status as st
from gui.operator import tokens
from gui.operator.store.metric_index import registry_metrics
from gui.operator.viewmodel.types import MetricAvailabilityView
from gui.operator.widgets.metric_tile import MetricTileModel
from gui.operator.widgets.tables import ColumnSpec, TableModel

#: Columns of the metric-availability panel, in registry order.
METRIC_COLUMNS: Final[Tuple[ColumnSpec, ...]] = (
    ColumnSpec("display", "Metric", width=220),
    ColumnSpec("status", "Availability", width=130, kind="status"),
    ColumnSpec("source", "Source", width=130),
    ColumnSpec("scopeLevel", "Scope", width=70),
    ColumnSpec("unit", "Unit", width=80),
    ColumnSpec("samplingIntervalMs", "Interval", width=90, kind="number",
               unit="ms", decimals=0),
    ColumnSpec("lastSampleAt", "Observed", width=170, kind="time"),
    ColumnSpec("sampleCount", "n", width=50, kind="number", decimals=0),
    ColumnSpec("quality", "Quality", width=90),
    ColumnSpec("reason", "Reason", width=420),
)

#: The eight per-run result groups section 4 requires.
RESULT_GROUPS: Final[Tuple[str, ...]] = (
    "manifest", "configSnapshot", "rawTelemetry", "normalizedTelemetry",
    "decisionTrace", "eventTimeline", "summaryStatistics", "figures",
)


@dataclass(frozen=True)
class RunView:
    """One stored run, as plain data.  Built off the Tk thread."""

    run_id: str
    mode: str
    disposition: str
    is_success: bool
    run_dir: str
    samples: Tuple[Mapping[str, Any], ...] = ()
    metric_index: Mapping[str, Any] = field(default_factory=dict)
    events: Tuple[Mapping[str, Any], ...] = ()
    episodes: Tuple[Mapping[str, Any], ...] = ()
    cycles: Tuple[Mapping[str, Any], ...] = ()
    llm_calls: Tuple[Mapping[str, Any], ...] = ()
    summary: Mapping[str, Any] = field(default_factory=dict)
    statistics: Optional[Mapping[str, Any]] = None
    config_snapshot: Mapping[str, Any] = field(default_factory=dict)
    manifest: Mapping[str, Any] = field(default_factory=dict)

    @property
    def data_issues(self) -> Tuple[Mapping[str, Any], ...]:
        return tuple(self.manifest.get("dataIssues") or ())

    @property
    def uses_relative_time(self) -> bool:
        """True when the source carried no wall clock.

        The axis label changes accordingly.  A run whose samples carry only
        elapsed seconds is never given a synthesized absolute clock.
        """
        return any(s.get("tUtc") is None for s in self.samples)

    def scopes_for(self, metric: str) -> Tuple[str, ...]:
        seen = []
        for sample in self.samples:
            if sample.get("metric") != metric:
                continue
            scope_id = (sample.get("scope") or {}).get("id")
            if scope_id and scope_id not in seen:
                seen.append(scope_id)
        return tuple(seen)

    def latest(self, metric: str,
               scope_id: Optional[str] = None) -> Optional[Mapping[str, Any]]:
        found = None
        for sample in self.samples:
            if sample.get("metric") != metric:
                continue
            if scope_id and (sample.get("scope") or {}).get("id") != scope_id:
                continue
            found = sample
        return found

    def goal_mbps(self) -> Optional[float]:
        """The I2 target, from this run's snapshot and never from a constant."""
        summary_goal = (self.summary.get("goal") or {}).get("targetMbps")
        if isinstance(summary_goal, (int, float)):
            return float(summary_goal)
        intent = ((self.config_snapshot.get("meta") or {})
                  .get("config", {}).get("intent_config") or {})
        value = intent.get("throughput_target_mbps")
        return float(value) if isinstance(value, (int, float)) else None


def load_run_view(store) -> RunView:
    """Read a stored run into a :class:`RunView`.

    WORKER THREAD ONLY.  This is the single place in the workspace module that
    reads a store; every widget and every ``on_state`` reads the resulting view.
    """
    return RunView(
        run_id=store.run_id, mode=store.mode, disposition=store.disposition,
        is_success=store.is_success, run_dir=str(store.run_dir),
        samples=tuple(store.read_telemetry()),
        metric_index=dict(store.read_metric_index()),
        events=tuple(store.read_events()),
        episodes=tuple(store.read_episodes()),
        cycles=tuple(store.read_cycles()),
        llm_calls=tuple(store.read_llm_calls()),
        summary=dict(store.read_summary()),
        statistics=store.read_statistics(),
        config_snapshot=dict(store.read_config_snapshot()),
        manifest=dict(store.read_manifest()))


@dataclass
class Selection:
    """What the operator is currently looking at."""

    metric: Optional[str] = None
    scope_ids: Tuple[str, ...] = ()
    window_s: Optional[float] = None
    comparison_run_ids: Tuple[str, ...] = ()
    show_unavailable: bool = True
    intent_id: Optional[str] = None
    policy_id: Optional[str] = None


class AnalysisModel:
    """Every decision the Analysis workspace makes.  No toolkit."""

    def __init__(self) -> None:
        self.runs: Dict[str, RunView] = {}
        self.primary_run_id: Optional[str] = None
        self.selection = Selection()
        self._registry = registry_metrics()

    # -- runs --------------------------------------------------------------- #

    def add_run(self, view: RunView, *, primary: bool = False) -> None:
        self.runs[view.run_id] = view
        if primary or self.primary_run_id is None:
            self.primary_run_id = view.run_id

    @property
    def primary(self) -> Optional[RunView]:
        return self.runs.get(self.primary_run_id or "")

    def display_name(self, metric: str) -> str:
        spec = self._registry.get(metric)
        return str(spec.get("display", metric)) if spec else metric

    # -- metric availability panel ------------------------------------------ #

    def metric_rows(self) -> List[Dict[str, Any]]:
        """One row per metric, unsupported ones included with their reason."""
        run = self.primary
        if run is None:
            return []
        rows = []
        for metric, entry in sorted(run.metric_index.items()):
            quality = entry.get("qualityCounts") or {}
            rows.append({
                "id": metric,
                "display": self.display_name(metric),
                "status": entry.get("status"),
                "source": entry.get("source"),
                "scopeLevel": entry.get("scopeLevel"),
                "unit": entry.get("unit"),
                "samplingIntervalMs": entry.get("samplingIntervalMs"),
                "lastSampleAt": entry.get("lastSampleAt"),
                "sampleCount": entry.get("sampleCount"),
                "quality": ", ".join(f"{k}:{v}" for k, v in sorted(quality.items()))
                or None,
                "reason": entry.get("reason")
                or ("" if entry.get("status") == "OK" else "no reason recorded"),
                "gapId": entry.get("gapId"),
            })
        if not self.selection.show_unavailable:
            rows = [r for r in rows if r["status"] == "OK"]
        return rows

    def metric_availability_views(self) -> List[MetricAvailabilityView]:
        """The metric panel as view models the shell can publish.

        Added at integration.  ``SessionState.metrics`` existed and nothing
        filled it, so the Demo View's headline KPI card had nothing to show and
        fell back to a sample count - which is not a KPI.

        Availability travels with the value, never behind it.  ``last_value`` is
        set only when the index says the metric is OK *and* a sample exists;
        an unsupported or unavailable metric keeps a null value and its stated
        reason, so a card can never render a number the run did not measure.
        """
        run = self.primary
        if run is None:
            return []
        views: List[MetricAvailabilityView] = []
        for metric, entry in sorted(run.metric_index.items()):
            status = str(entry.get("status") or "UNKNOWN")
            sample = run.latest(metric) if status == "OK" else None
            value = (sample or {}).get("value")
            spec = self._registry.get(metric) or {}
            views.append(MetricAvailabilityView(
                metric=metric,
                display=self.display_name(metric),
                status=status,
                reason=entry.get("reason")
                or (None if status == "OK" else "no reason recorded"),
                gap_id=entry.get("gapId"),
                source_class=entry.get("source"),
                scope_level=entry.get("scopeLevel"),
                unit=entry.get("unit"),
                sampling_interval_ms=entry.get("samplingIntervalMs"),
                sample_count=int(entry.get("sampleCount") or 0),
                last_sample_at=entry.get("lastSampleAt"),
                derived=bool(spec.get("derived", False)),
                last_value=float(value)
                if isinstance(value, (int, float))
                and not isinstance(value, bool) else None,
                last_value_quality=(sample or {}).get("quality"),
                last_value_scope=((sample or {}).get("scope") or {}).get("id"),
            ))
        return views

    def metric_table(self) -> TableModel:
        model = TableModel(columns=METRIC_COLUMNS)
        model.set_rows(self.metric_rows())
        model.set_sort("status")
        return model

    def tiles(self, metrics: Sequence[str]) -> List[MetricTileModel]:
        run = self.primary
        if run is None:
            return []
        tiles = []
        for metric in metrics:
            entry = run.metric_index.get(metric)
            if entry is None:
                continue
            spec = self._registry.get(metric) or {}
            tiles.append(MetricTileModel.from_index(
                metric, entry, display=self.display_name(metric),
                latest=run.latest(metric),
                decimals=int(spec.get("decimals", 2))))
        return tiles

    # -- charts ------------------------------------------------------------- #

    def chart_series(self, metric: str, *,
                     run_ids: Optional[Sequence[str]] = None,
                     intent_id: Optional[str] = None,
                     policy_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Series descriptors for one metric across one or more runs.

        Every series is keyed by ``(runId, scope, metric)``.  Two runs of the
        same metric are two series, never one concatenated line, and a metric
        from a different source class is never folded in.

        ``intent_id`` / ``policy_id`` restrict each series to the span over which
        that correlation id was observed on the timeline, and the restriction is
        reported on every series as ``restrictedTo`` so a reader can see the
        chart is a *slice*.  The association is between the id and a time span,
        never between the id and a sample - a sample carries no intent or policy
        field, and pretending otherwise would be a fabricated attribution.
        """
        chosen = list(run_ids or [self.primary_run_id or ""])
        window = self.correlation_window(intent_id=intent_id, policy_id=policy_id)
        restriction = None
        if window is not None:
            restriction = {
                "intentId": intent_id, "policyId": policy_id,
                "from": window[0], "to": window[1],
                "basis": "the span over which this id was observed on the "
                         "timeline; samples carry no intent or policy field",
            }
        series: List[Dict[str, Any]] = []
        index = 0
        for run_id in chosen:
            run = self.runs.get(run_id)
            if run is None:
                continue
            entry = run.metric_index.get(metric) or {}
            source_class = entry.get("source", "")
            for scope_id in (run.scopes_for(metric) or ("",)):
                points = [
                    ((s.get("tRelS") if run.uses_relative_time else s.get("seq")),
                     s.get("value"))
                    for s in run.samples
                    if s.get("metric") == metric
                    and (not scope_id
                         or (s.get("scope") or {}).get("id") == scope_id)]
                if window is not None:
                    points = [(x, v) for x, v in points
                              if x is not None and window[0] <= float(x) <= window[1]]
                style = tokens.series_style(index)
                index += 1
                series.append({
                    "seriesId": f"{run_id}:{metric}:{scope_id or 'all'}",
                    "label": f"{self.display_name(metric)}"
                             + (f" [{scope_id}]" if scope_id else "")
                             + (f" ({run_id})" if len(chosen) > 1 else ""),
                    "runId": run_id,
                    "scope": scope_id,
                    "sourceClass": source_class,
                    "unit": entry.get("unit", ""),
                    "status": entry.get("status", "UNKNOWN"),
                    "statusReason": entry.get("reason"),
                    "derived": bool(entry.get("derived")),
                    "points": tuple(points),
                    "restrictedTo": restriction,
                    **style,
                })
        return series

    def selection_axes(self) -> Dict[str, List[str]]:
        """What the operator may select on, enumerated from the run itself.

        Section 4 asks for KPI selection by cell, UE, intent, policy and run.
        Cell and UE are telemetry *scopes* and come from the samples; intent and
        policy are not - a TelemetrySample has no intent or policy field, and
        inventing one would be a fabricated association.  They are enumerated
        from the correlation ids the timeline actually carries, and selecting one
        restricts the chart to the span over which that id was observed.  Run is
        the loaded set.
        """
        run = self.primary
        if run is None:
            return {"cell": [], "ue": [], "intent": [], "policy": [], "run": []}
        cells, ues = [], []
        for sample in run.samples:
            scope = sample.get("scope") or {}
            bucket = cells if scope.get("level") == "CELL" else (
                ues if scope.get("level") == "UE" else None)
            if bucket is not None and scope.get("id") and scope["id"] not in bucket:
                bucket.append(scope["id"])
        intents, policies = [], []
        for event in run.events:
            ids = event.get("ids") or {}
            if ids.get("intentId") and ids["intentId"] not in intents:
                intents.append(ids["intentId"])
            if ids.get("policyId") and ids["policyId"] not in policies:
                policies.append(ids["policyId"])
        return {"cell": sorted(cells), "ue": sorted(ues),
                "intent": sorted(intents), "policy": sorted(policies),
                "run": sorted(self.runs)}

    def correlation_window(self, *, intent_id: Optional[str] = None,
                           policy_id: Optional[str] = None
                           ) -> Optional[Tuple[float, float]]:
        """The x span over which an intent or policy id was observed.

        ``None`` when the id never appears with a placeable time.  A selection
        that cannot be placed restricts nothing rather than silently showing an
        arbitrary window.
        """
        run = self.primary
        if run is None or (intent_id is None and policy_id is None):
            return None
        marks = []
        for event in run.events:
            ids = event.get("ids") or {}
            if intent_id is not None and ids.get("intentId") != intent_id:
                continue
            if policy_id is not None and ids.get("policyId") != policy_id:
                continue
            x = event.get("tRelS")
            if x is None:
                x = event.get("seq")
            if x is not None:
                marks.append(float(x))
        return (min(marks), max(marks)) if marks else None

    def axis_label(self) -> Tuple[str, str]:
        run = self.primary
        if run is not None and run.uses_relative_time:
            return "elapsed", "s"
        return "sample", ""

    def goal_line(self, metric: str) -> Tuple[Optional[float], str]:
        run = self.primary
        if run is None or metric != "throughput_mbps":
            return None, ""
        return run.goal_mbps(), "I2 target"

    def annotations(self) -> List[Dict[str, Any]]:
        run = self.primary
        if run is None:
            return []
        marks = []
        for event in run.events:
            if not event.get("annotatable"):
                continue
            x = event.get("tRelS")
            if x is None:
                x = event.get("seq")
            if x is None:
                continue
            marks.append({"kind": event.get("kind"), "x": float(x),
                          "label": event.get("title") or event.get("kind"),
                          "origin": event.get("origin", "OBSERVED"),
                          "annotatable": True})
        return marks

    def phase_bands(self) -> List[Dict[str, Any]]:
        """Shaded phase spans, from the run's own PHASE_CHANGE events."""
        run = self.primary
        if run is None:
            return []
        changes = [e for e in run.events if e.get("kind") == "PHASE_CHANGE"]
        bands = []
        for current, following in zip(changes, list(changes[1:]) + [None]):
            start = current.get("tRelS")
            if start is None:
                continue
            end = (following or {}).get("tRelS")
            if end is None:
                end = max((s.get("tRelS") or 0.0) for s in run.samples) \
                    if run.samples else start
            bands.append({"name": (current.get("detail") or {}).get("phase"),
                          "start": float(start), "end": float(end)})
        return bands

    # -- results ------------------------------------------------------------ #

    def results_bundle(self) -> Dict[str, Any]:
        """The eight per-run result groups section 4 requires, as one object."""
        run = self.primary
        if run is None:
            return {}
        outcomes = run.summary.get("outcomes") or {}
        latencies = [e.get("latencyMs") for e in run.episodes
                     if isinstance(e.get("latencyMs"), (int, float))]
        return {
            "manifest": {"runId": run.run_id, "mode": run.mode,
                         "disposition": run.disposition,
                         "isSuccess": run.is_success,
                         "sources": run.manifest.get("sources", [])},
            "configSnapshot": run.config_snapshot,
            "rawTelemetry": f"{run.run_dir}/raw",
            "normalizedTelemetry": {"sampleCount": len(run.samples),
                                    "metrics": sorted(run.metric_index)},
            "decisionTrace": {
                "episodes": len(run.episodes), "cycles": len(run.cycles),
                "llmCalls": len(run.llm_calls),
                "outcomes": outcomes,
                "terminalOutcomes": run.summary.get("terminalOutcomes") or {},
                "meanLatencyMs": (sum(latencies) / len(latencies)
                                  if latencies else None),
                "negotiations": run.summary.get("negotiations"),
                "rollbacks": run.summary.get("rollbacks"),
            },
            "eventTimeline": {"events": len(run.events),
                              "lanes": sorted({e.get("lane") for e in run.events
                                               if e.get("lane")})},
            "summaryStatistics": {"summary": run.summary,
                                  "statistics": run.statistics},
            "figures": sorted(k for k in (run.manifest.get("artifacts") or {})
                              if k.startswith("figures/")),
            # Not optional: section 4 requires missing, stale and quality
            # problems to be visible in the results, not only in a log.
            "dataIssues": list(run.data_issues),
        }

    def compare_runs(self, metric: str,
                     run_ids: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """Repeat-run comparison with n, mean, std and a 95% CI.

        The statistics come from ``experiments/metrics.py``, so a figure drawn
        here and one drawn by the offline pipeline agree.  The experimental
        conditions travel with the numbers: a comparison that has lost the
        conditions is not a comparison.
        """
        from experiments.metrics import confidence_interval

        chosen = list(run_ids or sorted(self.runs))
        rows = {}
        for run_id in chosen:
            run = self.runs.get(run_id)
            if run is None:
                continue
            values = [s["value"] for s in run.samples
                      if s.get("metric") == metric and s.get("value") is not None]
            nulls = sum(1 for s in run.samples
                        if s.get("metric") == metric and s.get("value") is None)
            interval = confidence_interval(values) if values else None
            rows[run_id] = {
                "n": len(values),
                "excludedNullSamples": nulls,
                "mean": interval["mean"] if interval else None,
                "std": interval["std"] if interval else None,
                "ci95": ({"low": interval["low"], "high": interval["high"],
                          "halfWidth": interval["half_width"]}
                         if interval else None),
                "mode": run.mode,
                "disposition": run.disposition,
                "conditions": {
                    "goalMbps": run.goal_mbps(),
                    "sourceClass": (run.metric_index.get(metric) or {}).get("source"),
                    "profileId": run.manifest.get("profileId"),
                    "meta": (run.config_snapshot.get("meta") or {}).get("mode"),
                },
            }
        return {"metric": metric, "runs": rows,
                "statistic": "mean with 95% t-CI (experiments.metrics)"}

    def before_during_after(self, metric: str, *,
                            boundaries: Sequence[float]) -> Dict[str, Any]:
        """Split one metric at the given x boundaries.

        Used for the before/during/after comparison within a single run.  A
        window with no real sample reports ``n = 0`` rather than borrowing from
        its neighbour.
        """
        from experiments.metrics import confidence_interval

        run = self.primary
        if run is None:
            return {}
        edges = [float("-inf")] + [float(b) for b in boundaries] + [float("inf")]
        names = ["before", "during", "after"][: len(edges) - 1] or []
        while len(names) < len(edges) - 1:
            names.append(f"window{len(names)}")
        windows: Dict[str, Any] = {}
        for name, low, high in zip(names, edges, edges[1:]):
            values = []
            for sample in run.samples:
                if sample.get("metric") != metric or sample.get("value") is None:
                    continue
                x = sample.get("tRelS")
                if x is None:
                    x = sample.get("seq")
                if low <= float(x) < high:
                    values.append(float(sample["value"]))
            interval = confidence_interval(values) if values else None
            windows[name] = {"n": len(values),
                             "mean": interval["mean"] if interval else None,
                             "ci95": ({"low": interval["low"],
                                       "high": interval["high"]}
                                      if interval else None)}
        return {"metric": metric, "windows": windows,
                "goalMbps": run.goal_mbps() if metric == "throughput_mbps" else None}

    # -- gui state ---------------------------------------------------------- #

    def gui_state(self) -> Dict[str, Any]:
        return {"metric": self.selection.metric,
                "scopeIds": list(self.selection.scope_ids),
                "windowS": self.selection.window_s,
                "comparisonRunIds": list(self.selection.comparison_run_ids),
                "showUnavailable": self.selection.show_unavailable,
                "intentId": self.selection.intent_id,
                "policyId": self.selection.policy_id}

    def restore_gui_state(self, state: Mapping[str, Any]) -> None:
        self.selection = Selection(
            metric=state.get("metric"),
            scope_ids=tuple(state.get("scopeIds") or ()),
            window_s=state.get("windowS"),
            comparison_run_ids=tuple(state.get("comparisonRunIds") or ()),
            show_unavailable=bool(state.get("showUnavailable", True)),
            intent_id=state.get("intentId"),
            policy_id=state.get("policyId"))


class AnalysisWorkspace:
    """The Tk rendering of :class:`AnalysisModel`.

    Implements the workspace protocol.  ``on_state`` and every refresh read the
    model and nothing else - no store, no source, no exporter - which is what
    keeps the Tk thread free and what the responsiveness gate asserts.
    """

    id = "analysis"
    title = "Analysis & Results"

    def __init__(self, *, theme: str = tokens.DEFAULT_THEME,
                 on_export: Optional[Callable[[str], None]] = None,
                 on_load_run: Optional[Callable[[str], None]] = None) -> None:
        self.model = AnalysisModel()
        self.theme_name = theme
        #: Injected by the shell and run on a worker thread.  The workspace
        #: never exports inline: an export is file I/O and the Tk thread does
        #: none.
        self.on_export = on_export
        #: Open a stored run *for reading*.  Deliberately not the same action as
        #: loading a Replay source: this one adds a run to the charts and
        #: changes nothing about the session, while a Replay load makes a
        #: recording the session's source and can back a REPLAY run.  They were
        #: one method for a while and the difference is exactly the kind that
        #: gets lost - so they are two controls, on two screens, with two names.
        self.on_load_run = on_load_run
        self.frame = None
        self.chart = None
        self.table = None
        self.tiles: List[Any] = []
        self.buttons: dict = {}
        self.run_path = None
        self._built = False

    def build(self, parent) -> None:
        import tkinter as tk
        from tkinter import ttk

        from gui.operator.widgets.charts import TimeSeriesChart
        from gui.operator.widgets.metric_tile import MetricTile
        from gui.operator.widgets.tables import DenseTable

        self.frame = ttk.Frame(parent)
        # Every workspace packs itself into the notebook page the shell hands
        # it; this one did not, so the tab rendered as an empty rectangle in an
        # assembled console while every hermetic test passed.  No headless test
        # can see this - it is a geometry-manager fact - which is why it
        # survived a track review and only appeared under Xvfb.
        self.frame.pack(fill="both", expand=True)

        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill="x", padx=4, pady=4)
        ttk.Label(toolbar, text="Run directory").pack(side="left", padx=(0, 4))
        self.run_path = tk.Entry(toolbar)
        self.run_path.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.buttons["load_run"] = ttk.Button(
            toolbar, text="Open stored run (view only)",
            command=self._load_run)
        self.buttons["load_run"].pack(side="left", padx=2)
        self.buttons["export"] = ttk.Button(
            toolbar, text="Export data and metadata",
            command=lambda: self.request_export("operator-export"))
        self.buttons["export"].pack(side="left", padx=2)
        ttk.Label(toolbar,
                  text="Opening a run here only charts it; it does not become "
                       "the session's source. Use Load Replay capture/run in "
                       "Live Operations for that."
                  ).pack(side="left", padx=6)

        panes = ttk.PanedWindow(self.frame, orient="vertical")
        panes.pack(fill="both", expand=True)

        top = ttk.Frame(panes)
        self.tile_row = ttk.Frame(top)
        self.tile_row.pack(fill="x")
        x_label, x_unit = self.model.axis_label()
        self.chart = TimeSeriesChart(top, title="KPI", y_label="value",
                                     y_unit="", x_label=x_label, x_unit=x_unit,
                                     theme=self.theme_name)
        self.chart.pack(fill="both", expand=True)
        panes.add(top, weight=3)

        bottom = ttk.Frame(panes)
        self.table = DenseTable(bottom, columns=METRIC_COLUMNS,
                                theme=self.theme_name, id_key="id")
        self.table.pack(fill="both", expand=True)
        panes.add(bottom, weight=2)
        self._built = True

    def on_state(self, state) -> None:
        """Tk thread; must return quickly.  Reads the model only."""
        if not self._built:
            return
        self.refresh()

    def refresh(self) -> None:
        if not self._built:
            return
        self.table.set_rows(self.model.metric_rows())
        metric = self.model.selection.metric
        if metric:
            from gui.operator.widgets.charts import SeriesSpec

            descriptors = self.model.chart_series(
                metric, intent_id=self.model.selection.intent_id,
                policy_id=self.model.selection.policy_id)
            specs = [SeriesSpec(series_id=d["seriesId"], label=d["label"],
                                color=d["color"], linestyle=d["linestyle"],
                                marker=d["marker"], unit=d["unit"],
                                source_class=d["sourceClass"], scope=d["scope"],
                                derived=d["derived"], status=d["status"],
                                status_reason=d["statusReason"])
                     for d in descriptors]
            self.chart.set_series(specs)
            for descriptor in descriptors:
                if descriptor["status"] != "OK":
                    self.chart.set_unavailable(
                        descriptor["seriesId"], descriptor["status"],
                        descriptor["statusReason"] or "no reason recorded")
                    continue
                for x, value in descriptor["points"]:
                    if x is not None:
                        self.chart.append(descriptor["seriesId"], float(x), value)
            goal, label = self.model.goal_line(metric)
            self.chart.set_goal_line(goal, label)
            self.chart.set_annotations(self.model.annotations())
            self.chart.model.phases = tuple(self.model.phase_bands())
            self.chart.refresh(force=True)

    def on_activate(self) -> None:
        self.refresh()

    def on_deactivate(self) -> None:
        """Release the canvas so a workspace off screen costs nothing."""
        if self.chart is not None:
            self.chart.model.set_window(self.model.selection.window_s)

    def gui_state(self) -> Mapping[str, Any]:
        return self.model.gui_state()

    def restore_gui_state(self, state: Mapping[str, Any]) -> None:
        self.model.restore_gui_state(state)
        self.refresh()

    def request_export(self, figure_id: str) -> None:
        """Hand an export to the shell's worker.  Never runs it inline."""
        if self.on_export is not None:
            self.on_export(figure_id)

    def _load_run(self) -> None:
        """Hand a run directory to the shell's worker.  Reads nothing here."""
        if self.on_load_run is None:
            return
        path = self.run_path.get().strip() if self.run_path is not None else ""
        self.on_load_run(path)


__all__ = ["METRIC_COLUMNS", "RESULT_GROUPS", "AnalysisModel",
           "AnalysisWorkspace", "RunView", "Selection", "load_run_view"]
