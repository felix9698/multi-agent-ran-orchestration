"""Metric tile - a value with everything needed to judge it.

Owner: track **T3**.  Authority: ``docs/phase-b-gui/metric-registry.1.0.0.json``
rendering rules.

> *A metric tile always shows: value, unit, source class, scope, sampling
> interval, observation timestamp, age and quality.  A bare number with no
> provenance is not an acceptable rendering.*

The tile is split in two on purpose.  :class:`MetricTileModel` is pure data and
pure functions - it decides what the tile *says*, and it is exercised by
hermetic tests with no display.  :class:`MetricTile` only paints what the model
decided.  A renderer swap therefore cannot change the semantics, and a semantic
change cannot hide inside a widget.

Two rules the model enforces rather than hopes for:

* a metric whose status is not ``OK`` shows the status glyph and the reason **in
  place of a value** - never a stale number with a warning badge beside it;
* a value that was never measured shows the em-dash placeholder inherited from
  ``gui/dashboard.py``.  A default, a prior, a zero or a last-known value from
  another run may never occupy that slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from gui.operator import status as st
from gui.operator import tokens


@dataclass(frozen=True)
class MetricTileModel:
    """Everything one tile displays, decided without a toolkit."""

    metric: str
    display: str
    status: str = "OK"
    reason: Optional[str] = None
    gap_id: Optional[str] = None
    value: Optional[float] = None
    unit: str = ""
    decimals: int = 2
    source_class: Optional[str] = None
    scope_level: Optional[str] = None
    scope_id: Optional[str] = None
    sampling_interval_ms: Optional[int] = None
    observed_at: Optional[str] = None
    age_ms: Optional[float] = None
    freshness_budget_ms: Optional[int] = None
    quality: Optional[str] = None
    sample_count: int = 0
    derived: bool = False
    derivation: Optional[str] = None

    # -- construction ------------------------------------------------------- #

    @classmethod
    def from_index(cls, metric: str, entry: Mapping[str, Any], *,
                   display: Optional[str] = None,
                   latest: Optional[Mapping[str, Any]] = None,
                   decimals: int = 2) -> "MetricTileModel":
        """Build from a metric-index entry plus, where it exists, a latest sample.

        The index is the authority on *availability*; the sample only supplies a
        number.  Reading it the other way round is how an unsupported metric
        acquires a plausible value.
        """
        sample = latest or {}
        scope = sample.get("scope") or {}
        return cls(
            metric=metric,
            display=display or metric,
            status=str(entry.get("status", "UNKNOWN")),
            reason=entry.get("reason"),
            gap_id=entry.get("gapId"),
            value=sample.get("value"),
            unit=str(entry.get("unit") or sample.get("unit") or ""),
            decimals=decimals,
            source_class=entry.get("source"),
            scope_level=entry.get("scopeLevel") or scope.get("level"),
            scope_id=scope.get("id") or (
                (entry.get("scopeIds") or [None])[0]),
            sampling_interval_ms=entry.get("samplingIntervalMs"),
            observed_at=sample.get("tUtc") or entry.get("lastSampleAt"),
            age_ms=sample.get("ageMs"),
            quality=sample.get("quality"),
            sample_count=int(entry.get("sampleCount") or 0),
            derived=bool(entry.get("derived") or sample.get("derived")),
            derivation=sample.get("derivation"))

    # -- rendering decisions ------------------------------------------------ #

    @property
    def resolved(self) -> st.Resolved:
        return st.resolve(self.status, reason=self.reason, gap_id=self.gap_id)

    @property
    def shows_a_value(self) -> bool:
        """A value is shown only when the metric is actually reporting one."""
        return self.status in ("OK", "DEGRADED", "STALE") and self.value is not None

    def value_text(self) -> str:
        """The primary line.

        Three cases, and the middle one is the easy mistake:

        * reporting -> the number and its unit;
        * ``OK`` but nothing measured yet -> the em-dash placeholder inherited
          from ``gui/dashboard.py``.  "OK" describes the metric's availability,
          not a value, and painting the status where a number belongs would read
          as a measurement;
        * not ``OK`` -> the status glyph and label, so the tile never shows a
          number the status contradicts.
        """
        if not self.shows_a_value:
            if self.status == "OK":
                return st.PRE_MEASUREMENT
            return st.describe(self.resolved, include_reason=False)
        text = f"{float(self.value):.{self.decimals}f}"
        return f"{text} {self.unit}".strip() if self.unit else text

    def reason_text(self) -> str:
        """The explanatory line.  Never empty for a non-OK status."""
        if self.status == "OK":
            return ""
        parts = [self.reason or "no reason recorded"]
        if self.gap_id:
            parts.append(f"[{self.gap_id}]")
        return " ".join(parts)

    def freshness(self) -> str:
        budget = self.freshness_budget_ms or st.FRESHNESS_BUDGET_MS.get(
            "O1_PM_SAMPLE")
        return st.freshness(self.age_ms, budget)

    def provenance_lines(self) -> Tuple[str, ...]:
        """The provenance block.  Every line is required by the registry."""
        scope = " ".join(x for x in (self.scope_level, self.scope_id) if x)
        interval = (f"{self.sampling_interval_ms} ms"
                    if self.sampling_interval_ms else st.PRE_MEASUREMENT)
        lines = [
            f"source: {self.source_class or st.PRE_MEASUREMENT}",
            f"scope: {scope or st.PRE_MEASUREMENT}",
            f"interval: {interval}",
            f"observed: {st.format_utc(self.observed_at)}",
            f"age: {st.format_age(self.age_ms)} ({self.freshness()})",
            f"quality: {self.quality or st.PRE_MEASUREMENT}",
            f"n: {self.sample_count}",
        ]
        if self.derived:
            lines.append("Derived visualization"
                         + (f": {self.derivation}" if self.derivation else ""))
        return tuple(lines)

    def color(self, theme_name: str = tokens.DEFAULT_THEME) -> str:
        return tokens.status_color(self.status, theme_name)


class MetricTile:
    """The Tk rendering of one :class:`MetricTileModel`.

    Constructed with a parent widget; ``set_model`` repaints.  All widget work
    happens on the Tk thread by contract - the tile performs no I/O of its own
    and reads nothing but the model it is handed.
    """

    def __init__(self, parent, *, theme: str = tokens.DEFAULT_THEME,
                 scale: float = tokens.SCALE_NORMAL,
                 show_provenance: bool = True) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.theme_name = theme
        self.scale = scale
        self.show_provenance = show_provenance
        self._palette = tokens.theme(theme)
        self.frame = ttk.Frame(parent, padding=tokens.SPACING["base"])
        self._title = tk.Label(self.frame, anchor="w",
                               bg=self._palette["panel_bg"],
                               fg=self._palette["fg_muted"],
                               font=tokens.font("small", scale=scale))
        self._value = tk.Label(self.frame, anchor="w",
                               bg=self._palette["panel_bg"],
                               fg=self._palette["fg"],
                               font=tokens.font("head", scale=scale, bold=True))
        self._reason = tk.Label(self.frame, anchor="w", justify="left",
                                wraplength=int(240 * scale),
                                bg=self._palette["panel_bg"],
                                fg=self._palette["fg_muted"],
                                font=tokens.font("micro", scale=scale))
        self._provenance = tk.Label(self.frame, anchor="w", justify="left",
                                    bg=self._palette["panel_bg"],
                                    fg=self._palette["fg_muted"],
                                    font=tokens.font("micro", scale=scale))
        self._title.grid(row=0, column=0, sticky="ew")
        self._value.grid(row=1, column=0, sticky="ew")
        self._reason.grid(row=2, column=0, sticky="ew")
        if show_provenance:
            self._provenance.grid(row=3, column=0, sticky="ew")
        self.frame.columnconfigure(0, weight=1)
        self.model: Optional[MetricTileModel] = None

    def grid(self, **kwargs):
        self.frame.grid(**kwargs)
        return self

    def pack(self, **kwargs):
        self.frame.pack(**kwargs)
        return self

    def set_model(self, model: MetricTileModel) -> None:
        self.model = model
        self._title.configure(text=model.display)
        self._value.configure(text=model.value_text(),
                              fg=(self._palette["fg"] if model.shows_a_value
                                  else model.color(self.theme_name)))
        self._reason.configure(text=model.reason_text())
        if self.show_provenance:
            self._provenance.configure(text="\n".join(model.provenance_lines()))


__all__ = ["MetricTile", "MetricTileModel"]
