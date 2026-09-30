"""Paper-grade figure export.

Owner: track **T3**.  Authority: ``docs/phase-b-gui/DESIGN.md`` section 4.5 and
the ``FigureMetadata`` definition in ``session-store.1.0.0.schema.json``.

One figure engine, on screen and on paper.  The palette, the Type-42 font
embedding and the 300 dpi raster setting are the ones ``experiments/figures.py``
already uses (mirrored in ``gui/operator/tokens.py``), so a chart an operator
looks at and the figure that goes into the paper come from one code path and
cannot silently disagree.  ``experiments/figures.py`` itself is not modified.

Five rules are enforced here rather than left to the caller, because each of
them is a way a figure can lie:

* **A null is a gap.**  It is never interpolated across and never plotted as
  zero.  matplotlib draws a gap for ``nan``, so nulls become ``nan`` *at the
  drawing layer only* - the source CSV keeps the empty field.
* **Fewer than three real samples is not a line.**  Such a series renders as
  discrete markers with a "sparse data (n=N)" note.  The run-012 capture's two
  ``RRU.PrbDl`` scalars are exactly this case, and drawing a line through two
  points would invent a trend that was never measured.
* **Series differ by more than colour.**  Colour plus line style plus marker,
  and categorical fills additionally carry a hatch, so the figure survives
  greyscale printing and colour vision deficiency.
* **Non-LIVE figures carry the mode watermark inside the figure**, not as an
  overlay, so a screenshot cannot lose it.  The mode used is the mode of the
  *session* (``store.mode``).  A run read back from a recording is ``REPLAY``
  even when the recording was made against a live radio, so it is watermarked -
  the earlier version took the recording's mode, saw ``LIVE`` and drew no
  watermark at all.  The recording's own mode, and any provenance note the
  producer attached, are written into the caption instead.
* **The processing block is mandatory.**  Smoothing, exclusions, aggregation and
  missing-value handling are always stated; ``"none"`` is an explicit, valid
  value.  A figure whose processing is unstated is refused rather than exported.

An unavailable series is still exported - in the legend, with its status - so
the absence is visible.  A series that quietly disappears teaches the reader
nothing.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Final, List, Mapping, Optional, Sequence, Tuple

from gui.operator import tokens

#: Below this many real values a series is drawn as points, never as a line.
SPARSE_THRESHOLD: Final[int] = 3

REQUIRED_PROCESSING: Final[tuple] = (
    "smoothing", "exclusions", "aggregation", "missingValueHandling")


class FigureExportError(RuntimeError):
    """A figure that cannot be exported without misrepresenting its data."""


@dataclass(frozen=True)
class SeriesData:
    """One plotted series and everything a reader needs to judge it.

    ``points`` are ``(x, value_or_None)``.  A ``None`` value is preserved all the
    way into the source CSV as an empty field; only the drawing layer turns it
    into a gap.
    """

    series_id: str
    label: str
    points: Tuple[Tuple[float, Optional[float]], ...] = ()
    unit: str = ""
    source_class: str = ""
    scope: str = ""
    condition: Optional[str] = None
    run_id: Optional[str] = None
    derived: bool = False
    status: str = "OK"
    status_reason: Optional[str] = None

    @property
    def real_values(self) -> Tuple[float, ...]:
        return tuple(v for _x, v in self.points if v is not None)

    @property
    def n(self) -> int:
        """Sample count.  Real values only - a gap is not a sample."""
        return len(self.real_values)

    @property
    def is_sparse(self) -> bool:
        return 0 < self.n < SPARSE_THRESHOLD

    @property
    def is_empty(self) -> bool:
        return self.n == 0


@dataclass(frozen=True)
class FigureSpec:
    """What to draw, and how it must be described."""

    figure_id: str
    title: str
    x_label: str
    x_unit: str
    y_label: str
    y_unit: str
    caption: Optional[str] = None
    goal_value: Optional[float] = None
    goal_label: str = "goal"
    annotations: Tuple[Mapping[str, Any], ...] = ()
    phases: Tuple[Mapping[str, Any], ...] = ()
    processing: Mapping[str, str] = field(default_factory=lambda: {
        "smoothing": "none", "exclusions": "none",
        "aggregation": "none", "missingValueHandling":
            "nulls are preserved as gaps; never interpolated, never zero"})
    derived_visualization: bool = False
    theme: str = "light"
    width_in: float = tokens.FIGURE_DOUBLE_COLUMN_IN
    height_in: float = 3.2


def series_from_samples(samples: Sequence[Mapping[str, Any]], *,
                        metric: str, scope_id: Optional[str] = None,
                        label: Optional[str] = None,
                        run_id: Optional[str] = None,
                        condition: Optional[str] = None,
                        status: str = "OK",
                        status_reason: Optional[str] = None) -> SeriesData:
    """Build one series from stored telemetry samples.

    The x axis is the observation time where the source declares one and the
    elapsed time where it does not.  The two are never mixed within a series,
    and a source that carries only elapsed seconds is not given a synthesized
    absolute clock.
    """
    rows = [s for s in samples if s.get("metric") == metric
            and (scope_id is None or (s.get("scope") or {}).get("id") == scope_id)]
    use_relative = any(r.get("tUtc") is None for r in rows)
    points: List[Tuple[float, Optional[float]]] = []
    for index, row in enumerate(rows):
        if use_relative:
            x = row.get("tRelS")
            x = float(x) if x is not None else float(index)
        else:
            x = float(index)
        points.append((x, row.get("value")))

    first = rows[0] if rows else {}
    return SeriesData(
        series_id=f"{metric}:{scope_id or 'all'}",
        label=label or (f"{metric} ({scope_id})" if scope_id else metric),
        points=tuple(points),
        unit=str(first.get("unit", "")),
        source_class=str((first.get("source") or {}).get("boundary", "")),
        scope=str(scope_id or (first.get("scope") or {}).get("id", "")),
        condition=condition, run_id=run_id,
        derived=bool(first.get("derived")),
        status=status, status_reason=status_reason)


def _validate(spec: FigureSpec, series: Sequence[SeriesData]) -> None:
    missing = [f for f in REQUIRED_PROCESSING if not (spec.processing or {}).get(f)]
    if missing:
        raise FigureExportError(
            f"figure {spec.figure_id}: the processing block must state "
            + ", ".join(missing) + " ('none' is a valid explicit value)")
    if not series:
        raise FigureExportError(f"figure {spec.figure_id}: no series to export")
    if spec.derived_visualization and not any(s.derived for s in series):
        raise FigureExportError(
            f"figure {spec.figure_id}: marked as a derived visualization but no "
            "series is derived")
    if any(s.derived for s in series) and not spec.derived_visualization:
        raise FigureExportError(
            f"figure {spec.figure_id}: a derived series must be exported as a "
            "derived visualization so it cannot be read as a measurement")


def _write_source_csv(path: Path, spec: FigureSpec, series: Sequence[SeriesData],
                      mode: str, run_id: str) -> None:
    """The exact rows the figure was drawn from.

    Deterministic by construction - fixed column order, series in the order
    given, no timestamp anywhere - so re-exporting the same figure yields
    byte-identical bytes and a diff means the data changed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(f"# mode={mode} runId={run_id} figureId={spec.figure_id}\n")
        handle.write(f"# x={spec.x_label} [{spec.x_unit}] "
                     f"y={spec.y_label} [{spec.y_unit}]\n")
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["seriesId", "label", "condition", "runId", "x", "value",
                         "unit", "sourceClass", "scope", "derived", "status"])
        for item in series:
            for x, value in item.points:
                writer.writerow([
                    item.series_id, item.label, item.condition or "",
                    item.run_id or "", x, "" if value is None else value,
                    item.unit, item.source_class, item.scope,
                    "true" if item.derived else "false", item.status])


def provenance_caption(store, caption: Optional[str]) -> Optional[str]:
    """Append the recording's provenance to a figure caption.

    ``FigureMetadata`` is ``additionalProperties: false``, so there is no field
    to add - and the caption is the right place regardless: it is what travels
    with the figure into a document, where a reader has no run directory to
    consult.
    """
    parts = [caption] if caption else []
    try:
        summary = store.read_summary()
    except Exception:                        # a figure must still export
        summary = {}
    source_mode = summary.get("sourceMode")
    if source_mode and source_mode != store.mode:
        parts.append(f"{store.mode} of a recorded {source_mode} run "
                     f"({store.run_id}); nothing here is being observed.")
    note = summary.get("sourceProvenanceNote")
    if note:
        parts.append(str(note))
    return " ".join(parts) if parts else None


def _metadata(spec: FigureSpec, series: Sequence[SeriesData], *, mode: str,
              run_ids: Sequence[str], source_csv: str, caption: Optional[str],
              generated_at: str) -> Dict[str, Any]:
    from experiments.metrics import confidence_interval

    entries = []
    for index, item in enumerate(series):
        style = tokens.series_style(index)
        values = list(item.real_values)
        interval = confidence_interval(values) if len(values) >= 2 else None
        entries.append({
            "label": item.label,
            "condition": item.condition,
            "runId": item.run_id,
            "n": item.n,
            "color": style["color"],
            "linestyle": "none" if item.is_sparse else style["linestyle"],
            "marker": style["marker"],
            "hatch": style["hatch"],
            "statistic": ("mean with 95% t-CI" if interval else None),
            "ci": ({"level": 0.95, "low": interval["low"],
                    "high": interval["high"],
                    "halfWidth": interval["half_width"]} if interval else None),
        })

    processing = dict(spec.processing)
    sparse = [s.label for s in series if s.is_sparse]
    if sparse:
        processing["exclusions"] = (
            processing["exclusions"]
            + f"; sparse series drawn as discrete points: {', '.join(sparse)}")

    return {
        "figureId": spec.figure_id,
        "title": spec.title,
        "caption": caption,
        "mode": mode,
        "runIds": list(run_ids),
        "generatedAt": generated_at,
        "axes": {
            "x": {"label": spec.x_label, "unit": spec.x_unit, "scale": "linear"},
            "y": {"label": spec.y_label, "unit": spec.y_unit, "scale": "linear"},
            "y2": None,
        },
        "series": entries,
        "annotations": [dict(a) for a in spec.annotations],
        "sourceData": source_csv,
        "processing": processing,
        "derivedVisualization": bool(spec.derived_visualization),
        "colorSafe": True,
    }


def _draw(spec: FigureSpec, series: Sequence[SeriesData], mode: str):
    """Render the figure.  Import matplotlib lazily and force the Agg backend.

    Lazy so that importing this module costs nothing on a headless path that
    only wants the metadata, and Agg so an export never tries to reach a display
    it does not have.
    """
    import matplotlib

    matplotlib.use("Agg", force=False)
    matplotlib.rcParams["pdf.fonttype"] = tokens.FIGURE_FONTTYPE
    matplotlib.rcParams["ps.fonttype"] = tokens.FIGURE_FONTTYPE
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(figsize=(spec.width_in, spec.height_in))

    for band in spec.phases:
        colour = tokens.PHASE_FILL.get(str(band.get("name")), "#F5F5F5")
        axes.axvspan(float(band["start"]), float(band["end"]), color=colour,
                     zorder=0, linewidth=0)

    for index, item in enumerate(series):
        style = tokens.series_style(index)
        label = item.label
        if item.status != "OK":
            # The series stays in the legend with its status, so the absence is
            # visible rather than silently dropped.
            axes.plot([], [], color=style["color"], linestyle=style["linestyle"],
                      marker=style["marker"],
                      label=f"{label} - {item.status}")
            continue
        xs = [x for x, _v in item.points]
        # nan at the drawing layer only: matplotlib renders it as a gap, and the
        # source CSV keeps the field empty.
        ys = [float("nan") if v is None else float(v) for _x, v in item.points]
        if item.is_sparse:
            axes.plot(xs, ys, linestyle="none", marker=style["marker"],
                      color=style["color"],
                      label=f"{label} (sparse data, n={item.n})")
            for x, value in item.points:
                if value is not None:
                    axes.annotate(f"{value:g}", (x, value),
                                  textcoords="offset points", xytext=(4, 4),
                                  fontsize=7)
        else:
            axes.plot(xs, ys, linestyle=style["linestyle"],
                      marker=style["marker"], markersize=3,
                      color=style["color"], label=f"{label} (n={item.n})")

    if spec.goal_value is not None:
        axes.axhline(spec.goal_value, color="#444444", linestyle=":",
                     linewidth=1.0, label=f"{spec.goal_label} = {spec.goal_value:g}")

    for annotation in spec.annotations:
        x = annotation.get("x")
        if x is None:
            continue
        colour = tokens.ANNOTATION_COLORS.get(str(annotation.get("kind")),
                                              "#666666")
        axes.axvline(float(x), color=colour, linestyle="--", linewidth=0.8,
                     alpha=0.8)
        axes.annotate(str(annotation.get("label", annotation.get("kind", ""))),
                      (float(x), 1.0), xycoords=("data", "axes fraction"),
                      textcoords="offset points", xytext=(2, -10),
                      fontsize=6, rotation=90, color=colour)

    axes.set_title(spec.title
                   + (" - Derived visualization" if spec.derived_visualization
                      else ""))
    axes.set_xlabel(f"{spec.x_label} ({spec.x_unit})" if spec.x_unit
                    else spec.x_label)
    axes.set_ylabel(f"{spec.y_label} ({spec.y_unit})" if spec.y_unit
                    else spec.y_label)
    axes.grid(True, linewidth=0.3, alpha=0.4)
    axes.legend(fontsize=6, loc="best", framealpha=0.9)

    if mode in tokens.FIGURE_WATERMARK_MODES:
        # Inside the figure, not an overlay: a screenshot cannot lose it.
        axes.text(0.5, 0.5, mode, transform=axes.transAxes, fontsize=44,
                  color="#000000", alpha=tokens.FIGURE_WATERMARK_ALPHA,
                  ha="center", va="center", rotation=30, zorder=5)
    if spec.derived_visualization:
        axes.text(0.99, 0.02, "Derived visualization", transform=axes.transAxes,
                  fontsize=7, ha="right", va="bottom", color="#B00020")

    figure.tight_layout()
    return figure, plt


def export_figure(store, spec: FigureSpec, series: Sequence[SeriesData], *,
                  formats: Sequence[str] = tokens.FIGURE_FORMATS,
                  generated_at: Optional[str] = None) -> Dict[str, Any]:
    """Draw, save and register one figure on a stored run.

    Writes ``figures/<id>.{pdf,svg,png}``, ``<id>.source.csv`` and
    ``<id>.meta.json``, and registers them through ``SessionStore.write_figure``
    so the mandatory processing block is checked a second time by the store.
    """
    _validate(spec, series)
    for name in formats:
        if name not in tokens.FIGURE_FORMATS:
            raise FigureExportError(f"unsupported figure format {name!r}")

    figures_dir = Path(store.run_dir) / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    source_csv = f"figures/{spec.figure_id}.source.csv"
    _write_source_csv(figures_dir / f"{spec.figure_id}.source.csv", spec, series,
                      store.mode, store.run_id)

    figure, plt = _draw(spec, series, store.mode)
    paths = []
    try:
        for name in formats:
            target = figures_dir / f"{spec.figure_id}.{name}"
            figure.savefig(target, dpi=tokens.FIGURE_DPI, bbox_inches="tight",
                           metadata={"Creator": None, "Producer": None,
                                     "CreationDate": None}
                           if name == "pdf" else None)
            paths.append(f"figures/{target.name}")
    finally:
        plt.close(figure)

    stamp = generated_at or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    run_ids = sorted({s.run_id for s in series if s.run_id} | {store.run_id})
    metadata = _metadata(spec, series, mode=store.mode, run_ids=run_ids,
                         source_csv=source_csv,
                         caption=provenance_caption(store, spec.caption),
                         generated_at=stamp)
    store.write_figure(spec.figure_id, paths=paths, source_csv=source_csv,
                       metadata=metadata)
    return {"figureId": spec.figure_id, "paths": paths,
            "sourceCsv": source_csv,
            "metadataPath": f"figures/{spec.figure_id}.meta.json",
            "mode": store.mode,
            "derivedVisualization": bool(spec.derived_visualization)}


def annotations_from_events(events: Sequence[Mapping[str, Any]], *,
                            x_of) -> Tuple[Dict[str, Any], ...]:
    """Chart annotations from the run's own timeline.

    Only ``annotatable`` events become annotations, and only those the caller
    can place on the axis: an event with no time on this figure's x scale is
    skipped rather than drawn at an invented position.
    """
    marks = []
    for event in events:
        if not event.get("annotatable"):
            continue
        x = x_of(event)
        if x is None:
            continue
        marks.append({"kind": event.get("kind"), "x": float(x),
                      "label": event.get("title") or event.get("kind"),
                      "origin": event.get("origin")})
    return tuple(marks)


__all__ = [
    "REQUIRED_PROCESSING", "SPARSE_THRESHOLD", "FigureExportError",
    "FigureSpec", "SeriesData", "annotations_from_events", "export_figure",
    "provenance_caption", "series_from_samples",
]
