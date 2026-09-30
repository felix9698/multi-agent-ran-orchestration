"""Time-series chart.

Owner: track **T3**; consumed by T1, T2 and T4 through the constructor signature
frozen in ``file-ownership.1.0.0.json``.

matplotlib on a ``FigureCanvasTkAgg``, which is what the existing console
already runs and what produces the paper figures - one engine on screen and on
paper, so a chart and its exported figure cannot disagree.

The named risk in DESIGN.md section 12 is matplotlib's real-time redraw cost.
It is mitigated structurally, not by hope, and all of it lives in the pure
:class:`ChartModel`:

* a **bounded ring buffer** per series, so memory is capped whatever the data
  rate;
* a **frame budget** - at most ``CHART_REFRESH_MAX_HZ`` redraws per second,
  independent of how fast records arrive.  Under pressure the *refresh rate*
  degrades and the data never does;
* **decimation above a point threshold, for display only**.  An export always
  draws from the full series, and a decimated view records the fact in the
  figure metadata's processing block, so a decimated chart can never be mistaken
  for the full one.

Three honesty rules, identical to the exporter's because they are the same
rules:

* ``None`` creates a **gap**.  Never a zero, never an interpolated point.
* fewer than three real samples renders as **discrete points** with a sparse
  note, never as a line through them.
* an unavailable series stays **in the legend with its status**, so its absence
  is visible.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import (Any, Callable, Deque, Dict, Final, List, Mapping, Optional,
                    Sequence, Tuple)

from gui.operator import tokens
from gui.operator.export.figure_export import (SPARSE_THRESHOLD, FigureSpec,
                                               SeriesData, export_figure)


@dataclass(frozen=True)
class SeriesSpec:
    """Identity and styling for one chart series."""

    series_id: str
    label: str
    color: str = ""
    linestyle: str = "-"
    marker: str = ""
    unit: str = ""
    source_class: str = ""
    scope: str = ""
    derived: bool = False
    status: str = "OK"
    status_reason: Optional[str] = None

    @classmethod
    def styled(cls, index: int, series_id: str, label: str, **fields: Any
               ) -> "SeriesSpec":
        """Apply the palette so a series differs by more than colour."""
        style = tokens.series_style(index)
        return cls(series_id=series_id, label=label, color=style["color"],
                   linestyle=style["linestyle"], marker=style["marker"],
                   **fields)


class ChartModel:
    """Everything the chart decides, decided without a toolkit."""

    def __init__(self, *, max_points: int = tokens.CHART_MAX_POINTS,
                 decimation_threshold: int = tokens.CHART_DECIMATION_THRESHOLD
                 ) -> None:
        self.max_points = max_points
        self.decimation_threshold = decimation_threshold
        self.specs: Dict[str, SeriesSpec] = {}
        self.order: List[str] = []
        self.points: Dict[str, Deque[Tuple[float, Optional[float]]]] = {}
        self.window_s: Optional[float] = None
        self.goal_value: Optional[float] = None
        self.goal_label: str = "goal"
        self.annotations: Tuple[Mapping[str, Any], ...] = ()
        self.phases: Tuple[Mapping[str, Any], ...] = ()
        self.dropped: int = 0

    # -- series ------------------------------------------------------------- #

    def set_series(self, series: Sequence[SeriesSpec]) -> None:
        self.specs = {s.series_id: s for s in series}
        self.order = [s.series_id for s in series]
        for series_id in self.order:
            self.points.setdefault(
                series_id, deque(maxlen=self.max_points))
        for series_id in list(self.points):
            if series_id not in self.specs:
                del self.points[series_id]

    def append(self, series_id: str, t: float,
               value: Optional[float]) -> None:
        """Add one point.  ``None`` creates a gap, never a zero."""
        buffer = self.points.get(series_id)
        if buffer is None:
            return
        if len(buffer) == buffer.maxlen:
            self.dropped += 1
        buffer.append((float(t), None if value is None else float(value)))

    def extend(self, series_id: str,
               points: Sequence[Tuple[float, Optional[float]]]) -> None:
        for t, value in points:
            self.append(series_id, t, value)

    def set_unavailable(self, series_id: str, status: str, reason: str) -> None:
        spec = self.specs.get(series_id)
        if spec is None:
            return
        self.specs[series_id] = SeriesSpec(
            series_id=spec.series_id, label=spec.label, color=spec.color,
            linestyle=spec.linestyle, marker=spec.marker, unit=spec.unit,
            source_class=spec.source_class, scope=spec.scope,
            derived=spec.derived, status=status, status_reason=reason)
        self.points[series_id] = deque(maxlen=self.max_points)

    # -- view --------------------------------------------------------------- #

    def set_window(self, seconds: Optional[float]) -> None:
        self.window_s = seconds

    def set_goal_line(self, value: Optional[float], label: str = "goal") -> None:
        self.goal_value = value
        self.goal_label = label

    def set_annotations(self, events: Sequence[Mapping[str, Any]]) -> None:
        """Annotations from timeline events.

        Only ``annotatable`` events with a usable x land on the chart.  An event
        with no time on this axis is skipped rather than drawn at an invented
        position.
        """
        marks = []
        for event in events:
            if not event.get("annotatable"):
                continue
            x = event.get("x")
            if x is None:
                x = event.get("tRelS")
            if x is None:
                continue
            marks.append({"kind": event.get("kind"), "x": float(x),
                          "label": event.get("title") or event.get("kind"),
                          "origin": event.get("origin", "OBSERVED")})
        self.annotations = tuple(marks)

    def windowed(self, series_id: str) -> List[Tuple[float, Optional[float]]]:
        points = list(self.points.get(series_id, ()))
        if self.window_s is None or not points:
            return points
        latest = max(x for x, _v in points)
        return [(x, v) for x, v in points if x >= latest - self.window_s]

    def decimate(self, points: Sequence[Tuple[float, Optional[float]]]
                 ) -> Tuple[List[Tuple[float, Optional[float]]], Optional[str]]:
        """Thin a series for DISPLAY only, and say so.

        Stride sampling, never averaging: an averaged point is a value that was
        never measured, and this is a display optimisation, not an analysis.
        Nulls are always kept, because dropping a gap would make the series look
        continuous when it is not.
        """
        if len(points) <= self.decimation_threshold:
            return list(points), None
        stride = (len(points) // self.decimation_threshold) + 1
        thinned = [p for index, p in enumerate(points)
                   if index % stride == 0 or p[1] is None]
        note = (f"display decimation: every {stride}th point of {len(points)} "
                "(nulls preserved); exports draw the full series")
        return thinned, note

    def series_data(self, *, run_id: Optional[str] = None) -> List[SeriesData]:
        """The series in exporter form, so screen and paper share one path."""
        out = []
        for series_id in self.order:
            spec = self.specs[series_id]
            out.append(SeriesData(
                series_id=series_id, label=spec.label,
                points=tuple(self.points.get(series_id, ())),
                unit=spec.unit, source_class=spec.source_class,
                scope=spec.scope, run_id=run_id, derived=spec.derived,
                status=spec.status, status_reason=spec.status_reason))
        return out

    def sparse_series(self) -> List[str]:
        return [s.series_id for s in self.series_data() if s.is_sparse]


class FrameBudget:
    """At most N redraws a second, whatever the data rate.

    Separate from :class:`ChartModel` so the budget can be driven by a fake
    clock in a test - a timing rule that can only be observed in wall-clock time
    is a timing rule nobody verifies.
    """

    def __init__(self, max_hz: float = tokens.CHART_REFRESH_MAX_HZ,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.interval = 1.0 / float(max_hz)
        self._clock = clock
        self._last = float("-inf")
        self.skipped = 0

    def allow(self) -> bool:
        now = self._clock()
        if now - self._last < self.interval:
            self.skipped += 1
            return False
        self._last = now
        return True

    def stamp(self) -> None:
        """Record a redraw that bypassed the budget.

        A forced redraw still consumes the frame: if it did not, alternating
        forced and unforced calls would redraw at the data rate, which is
        exactly what the budget exists to prevent.
        """
        self._last = self._clock()


class TimeSeriesChart:
    """matplotlib chart embedded in Tk.  Constructor signature frozen.

    The widget owns no data of its own: it renders :class:`ChartModel` and
    honours :class:`FrameBudget`.  It performs no I/O, so it can be driven from
    the Tk thread without ever blocking it.
    """

    def __init__(self, parent, *, title: str, y_label: str, y_unit: str,
                 x_label: str = "time", x_unit: str = "s",
                 theme: str = tokens.DEFAULT_THEME, height_px: int = 220,
                 max_points: int = tokens.CHART_MAX_POINTS) -> None:
        import matplotlib

        matplotlib.use("TkAgg", force=False)
        matplotlib.rcParams["pdf.fonttype"] = tokens.FIGURE_FONTTYPE
        matplotlib.rcParams["ps.fonttype"] = tokens.FIGURE_FONTTYPE
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from matplotlib.figure import Figure
        from tkinter import ttk

        self.title = title
        self.x_label, self.x_unit = x_label, x_unit
        self.y_label, self.y_unit = y_label, y_unit
        self.theme_name = theme
        self.palette = tokens.theme(theme)
        self.model = ChartModel(max_points=max_points)
        self.budget = FrameBudget()

        self.frame = ttk.Frame(parent)
        self.figure = Figure(figsize=(6.0, height_px / 100.0), dpi=100)
        self.axes = self.figure.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.figure, master=self.frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self._drawn = False

    def grid(self, **kwargs):
        self.frame.grid(**kwargs)
        return self

    def pack(self, **kwargs):
        self.frame.pack(**kwargs)
        return self

    # -- data (delegates to the model) -------------------------------------- #

    def set_series(self, series: Sequence[SeriesSpec]) -> None:
        self.model.set_series(series)

    def append(self, series_id: str, t: float, value: Optional[float]) -> None:
        self.model.append(series_id, t, value)

    def set_annotations(self, events: Sequence[Mapping[str, Any]]) -> None:
        self.model.set_annotations(events)

    def set_window(self, seconds: Optional[float]) -> None:
        self.model.set_window(seconds)

    def set_goal_line(self, value: Optional[float], label: str = "goal") -> None:
        self.model.set_goal_line(value, label)

    def set_unavailable(self, series_id: str, status: str, reason: str) -> None:
        self.model.set_unavailable(series_id, status, reason)

    # -- rendering ---------------------------------------------------------- #

    def refresh(self, *, force: bool = False) -> bool:
        """Redraw within the frame budget.  Tk thread only.

        Returns whether a redraw happened, so a caller can surface the skip
        count rather than wonder why the chart looks still.
        """
        if force:
            self.budget.stamp()
        elif not self.budget.allow():
            return False
        self.axes.clear()
        for band in self.model.phases:
            self.axes.axvspan(float(band["start"]), float(band["end"]),
                              color=tokens.PHASE_FILL.get(str(band.get("name")),
                                                          "#F5F5F5"),
                              zorder=0, linewidth=0)
        for series_id in self.model.order:
            spec = self.model.specs[series_id]
            if spec.status != "OK":
                self.axes.plot([], [], color=spec.color or None,
                               linestyle=spec.linestyle, marker=spec.marker,
                               label=f"{spec.label} - {spec.status}")
                continue
            points, _note = self.model.decimate(self.model.windowed(series_id))
            if not points:
                self.axes.plot([], [], color=spec.color or None,
                               label=f"{spec.label} (n=0)")
                continue
            xs = [x for x, _v in points]
            ys = [float("nan") if v is None else v for _x, v in points]
            real = sum(1 for _x, v in points if v is not None)
            if real < SPARSE_THRESHOLD:
                self.axes.plot(xs, ys, linestyle="none", marker=spec.marker or "o",
                               color=spec.color or None,
                               label=f"{spec.label} (sparse data, n={real})")
            else:
                self.axes.plot(xs, ys, linestyle=spec.linestyle,
                               marker=spec.marker, markersize=3,
                               color=spec.color or None,
                               label=f"{spec.label} (n={real})")
        if self.model.goal_value is not None:
            self.axes.axhline(self.model.goal_value, color="#444444",
                              linestyle=":", linewidth=1.0,
                              label=f"{self.model.goal_label} = "
                                    f"{self.model.goal_value:g}")
        for annotation in self.model.annotations:
            colour = tokens.ANNOTATION_COLORS.get(str(annotation.get("kind")),
                                                  "#666666")
            self.axes.axvline(annotation["x"], color=colour,
                              linestyle="--" if annotation.get("origin") != "DERIVED"
                              else ":", linewidth=0.8, alpha=0.8)
        self.axes.set_title(self.title, fontsize=9)
        self.axes.set_xlabel(f"{self.x_label} ({self.x_unit})" if self.x_unit
                             else self.x_label, fontsize=8)
        self.axes.set_ylabel(f"{self.y_label} ({self.y_unit})" if self.y_unit
                             else self.y_label, fontsize=8)
        self.axes.grid(True, linewidth=0.3, alpha=0.4)
        self.axes.legend(fontsize=6, loc="best")
        self.figure.tight_layout()
        self.canvas.draw_idle()
        self._drawn = True
        return True

    def export(self, store, figure_id: str, *,
               formats: Sequence[str] = tokens.FIGURE_FORMATS,
               processing: Optional[Mapping[str, str]] = None,
               derived_visualization: bool = False) -> Mapping[str, Any]:
        """Export exactly what is on screen, from the full data.

        The chart hands the *whole* series to the exporter, not the decimated
        display copy, and records the decimation in the processing block if one
        was applied - so an exported figure is never the thinned view.
        """
        series = self.model.series_data(run_id=store.run_id)
        block = dict(processing or {
            "smoothing": "none", "exclusions": "none", "aggregation": "none",
            "missingValueHandling":
                "nulls are preserved as gaps; never interpolated, never zero"})
        _points, note = self.model.decimate(
            [p for series_id in self.model.order
             for p in self.model.windowed(series_id)])
        block["decimation"] = note or "none (the export draws the full series)"
        spec = FigureSpec(
            figure_id=figure_id, title=self.title,
            x_label=self.x_label, x_unit=self.x_unit,
            y_label=self.y_label, y_unit=self.y_unit,
            goal_value=self.model.goal_value, goal_label=self.model.goal_label,
            annotations=self.model.annotations, phases=self.model.phases,
            processing=block, derived_visualization=derived_visualization)
        return export_figure(store, spec, series, formats=formats)


__all__ = ["ChartModel", "FrameBudget", "SeriesSpec", "TimeSeriesChart"]
