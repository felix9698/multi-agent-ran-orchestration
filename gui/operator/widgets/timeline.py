"""The unified event timeline.

One lane view over everything the console observes: intent submissions, agentic
decisions, coordinator transitions, R1 and A1 traffic, O1 and DME evidence,
deployment feedback, session lifecycle, warnings and teardown.  phaseB_task.md
section 2 requires them in one time-ordered place, and it requires every
error-severity row to carry the intent id, the policy id, the run id, the
component, the time and the cause.

Two rules that are easy to get wrong and expensive to get wrong:

* An event the console *inferred* is never mixed in with events it *observed*.
  ``origin=DERIVED`` rows render on their own track with the derivation stated,
  because a capture's FSM transitions carry ordering but no timestamp (GAP-12)
  and reading an inferred position as a measured one would corrupt a latency
  claim.
* The buffer is bounded and the newest rows win.  A run that outlives the buffer
  loses its oldest *display* rows, never its recorded ones - the store keeps the
  full record either way.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional, Sequence, Tuple

from .. import status as st
from .. import tokens
from ..viewmodel.types import TimelineEventView

#: The lanes, in display order.
LANES: Tuple[str, ...] = (
    "INTENT", "DECISION", "COORDINATOR", "R1", "A1", "O1", "DME",
    "LOWER_FEEDBACK", "SESSION", "WARNING", "TEARDOWN",
)

#: Severity ranks used for filtering and for the row colour.
SEVERITY_RANK: dict = {"DEBUG": 0, "INFO": 1, "NOTICE": 2, "WARNING": 3,
                       "ERROR": 4, "CRITICAL": 5}

SEVERITY_STATUS: dict = {"DEBUG": st.NOT_APPLICABLE, "INFO": st.OK,
                         "NOTICE": st.OK, "WARNING": st.DEGRADED,
                         "ERROR": st.ERROR, "CRITICAL": st.ERROR}

COLUMNS: Tuple[Tuple[str, str, int], ...] = (
    ("time", "Time", 190),
    ("lane", "Lane", 130),
    ("kind", "Event", 220),
    ("severity", "Severity", 110),
    ("origin", "Origin", 100),
    ("title", "Detail", 460),
    ("ids", "Correlation", 380),
)


def event_ids(event: TimelineEventView) -> str:
    """The identifiers section 2 requires on every important row."""
    parts = [f"{name}={value}" for name, value in (
        ("intent", event.intent_id), ("policy", event.policy_id),
        ("episode", event.episode_id), ("evidence", event.evidence_id),
        ("run", event.run_id), ("component", event.component),
        ("corr", event.correlation_id)) if value]
    return "  ".join(parts)


def event_time(event: TimelineEventView) -> str:
    """Absolute UTC when it is known, elapsed seconds when only that is."""
    if event.t_utc:
        return st.format_utc(event.t_utc)
    if event.t_rel_s is not None:
        return f"+{event.t_rel_s:.3f}s"
    return st.PRE_MEASUREMENT


def event_row(event: TimelineEventView) -> Tuple[str, ...]:
    """One timeline row, already formatted."""
    origin = event.origin or "OBSERVED"
    if origin == "DERIVED":
        origin = f"{origin}: {event.derivation or 'ordering only'}"
    return (event_time(event), event.lane, event.kind, event.severity, origin,
            event.title or "", event_ids(event))


def row_status(event: TimelineEventView) -> str:
    """GUI status used to colour one row."""
    return SEVERITY_STATUS.get(event.severity, st.UNKNOWN)


def filter_events(events: Iterable[TimelineEventView], *,
                  lanes: Optional[Sequence[str]] = None,
                  min_severity: str = "DEBUG",
                  correlation: Optional[str] = None,
                  text: Optional[str] = None,
                  include_derived: bool = True
                  ) -> Tuple[TimelineEventView, ...]:
    """Apply the lane, severity, correlation and free-text filters.

    Pure and order-preserving: the timeline is already sorted by the source that
    produced it, and re-sorting here would silently reorder a capture whose
    ordering rule is authoritative.
    """
    floor = SEVERITY_RANK.get(min_severity, 0)
    lane_set = set(lanes) if lanes else None
    needle = (text or "").strip().lower() or None
    out = []
    for event in events:
        if lane_set is not None and event.lane not in lane_set:
            continue
        if SEVERITY_RANK.get(event.severity, 1) < floor:
            continue
        if not include_derived and (event.origin or "OBSERVED") == "DERIVED":
            continue
        if correlation and correlation not in (
                event.correlation_id, event.intent_id, event.policy_id,
                event.episode_id, event.run_id, event.evidence_id):
            continue
        if needle:
            haystack = " ".join(str(part) for part in event_row(event)).lower()
            if needle not in haystack:
                continue
        out.append(event)
    return tuple(out)


class TimelineView:
    """Filterable lane view with a bounded display buffer."""

    def __init__(self, *, theme: str = tokens.DEFAULT_THEME,
                 scale: float = tokens.SCALE_NORMAL,
                 max_rows: int = tokens.TIMELINE_MAX_EVENTS,
                 on_select: Optional[Callable[[str], None]] = None) -> None:
        self.theme = theme
        self.scale = scale
        self.max_rows = max(1, int(max_rows))
        self.on_select = on_select
        self.frame = None
        self.tree = None
        self.lane_filter: Optional[Tuple[str, ...]] = None
        self.min_severity = "DEBUG"
        self.correlation: Optional[str] = None
        self.search_text: Optional[str] = None
        self.include_derived = True
        self._palette = tokens.theme(theme)
        self._events: Tuple[TimelineEventView, ...] = ()

    def build(self, parent) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.frame = tk.Frame(parent, bg=self._palette["bg"])
        controls = tk.Frame(self.frame, bg=self._palette["panel_bg"])
        controls.pack(fill="x")

        tk.Label(controls, text="Lane", bg=self._palette["panel_bg"],
                 fg=self._palette["fg_muted"],
                 font=tokens.font("micro", scale=self.scale)
                 ).pack(side="left", padx=tokens.SPACING["hair"])
        self._lane_box = ttk.Combobox(controls, state="readonly", width=16,
                                      values=("ALL",) + LANES)
        self._lane_box.set("ALL")
        self._lane_box.pack(side="left", padx=tokens.SPACING["hair"])
        self._lane_box.bind("<<ComboboxSelected>>", self._on_filter_changed)

        tk.Label(controls, text="Min severity", bg=self._palette["panel_bg"],
                 fg=self._palette["fg_muted"],
                 font=tokens.font("micro", scale=self.scale)
                 ).pack(side="left", padx=tokens.SPACING["hair"])
        self._severity_box = ttk.Combobox(
            controls, state="readonly", width=10,
            values=tuple(sorted(SEVERITY_RANK, key=SEVERITY_RANK.get)))
        self._severity_box.set("DEBUG")
        self._severity_box.pack(side="left", padx=tokens.SPACING["hair"])
        self._severity_box.bind("<<ComboboxSelected>>", self._on_filter_changed)

        tk.Label(controls, text="Search", bg=self._palette["panel_bg"],
                 fg=self._palette["fg_muted"],
                 font=tokens.font("micro", scale=self.scale)
                 ).pack(side="left", padx=tokens.SPACING["hair"])
        self._search = tk.Entry(controls, width=28, relief="flat",
                                bg=self._palette["panel_alt_bg"],
                                fg=self._palette["fg"],
                                font=tokens.font("small", scale=self.scale))
        self._search.pack(side="left", padx=tokens.SPACING["hair"])
        self._search.bind("<KeyRelease>", self._on_filter_changed)

        self.tree = ttk.Treeview(self.frame,
                                 columns=[key for key, _, _ in COLUMNS],
                                 show="headings", height=14)
        for key, heading, width in COLUMNS:
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, anchor="w", stretch=True)
        scroll = ttk.Scrollbar(self.frame, orient="vertical",
                               command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for status_id in st.STATUS_SPECS:
            self.tree.tag_configure(
                status_id, foreground=tokens.status_color(status_id, self.theme))
        self.tree.tag_configure("DERIVED", font=tokens.font(
            "small", scale=self.scale, mono=True))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

    def _on_filter_changed(self, _event=None) -> None:
        lane = self._lane_box.get()
        self.lane_filter = None if lane == "ALL" else (lane,)
        self.min_severity = self._severity_box.get() or "DEBUG"
        self.search_text = self._search.get() or None
        self.repaint()

    def _on_select(self, _event=None) -> None:
        if self.on_select is None or self.tree is None:
            return
        selection = self.tree.selection()
        if selection:
            self.on_select(selection[0])

    def set_events(self, events: Sequence[TimelineEventView]) -> None:
        """Replace the display buffer, keeping the newest ``max_rows`` rows."""
        ordered = tuple(events)
        self._events = ordered[-self.max_rows:]
        self.repaint()

    def visible(self) -> Tuple[TimelineEventView, ...]:
        return filter_events(self._events, lanes=self.lane_filter,
                             min_severity=self.min_severity,
                             correlation=self.correlation,
                             text=self.search_text,
                             include_derived=self.include_derived)

    def repaint(self) -> None:
        """Tk thread only."""
        if self.tree is None:
            return
        self.tree.delete(*self.tree.get_children())
        for event in self.visible():
            tags = [row_status(event)]
            if (event.origin or "OBSERVED") == "DERIVED":
                tags.append("DERIVED")
            self.tree.insert("", "end", iid=str(event.seq),
                             values=event_row(event), tags=tuple(tags))
        children = self.tree.get_children()
        if children:
            self.tree.see(children[-1])

    def gui_state(self) -> dict:
        return {"lane": self.lane_filter, "minSeverity": self.min_severity,
                "search": self.search_text,
                "includeDerived": self.include_derived}

    def restore_gui_state(self, state: Any) -> None:
        data = dict(state or {})
        lane = data.get("lane")
        self.lane_filter = tuple(lane) if lane else None
        self.min_severity = data.get("minSeverity") or "DEBUG"
        self.search_text = data.get("search")
        self.include_derived = bool(data.get("includeDerived", True))


__all__ = ["COLUMNS", "LANES", "SEVERITY_RANK", "SEVERITY_STATUS",
           "TimelineView", "event_ids", "event_row", "event_time",
           "filter_events", "row_status"]
