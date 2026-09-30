"""The O-RAN topology grid and the readiness strip.

Two widgets, one idea: show the operator *where the flow is blocked*, not
whether a light is green.  phaseB_task.md section 2 asks for exactly that, and
it is the reason the readiness strip sits above the element grid rather than
being one more row in it.

Both widgets are dumb renderers.  The element list and the segment list are
built by :mod:`gui.operator.workspaces.live_ops`, which is where the projection
rules - and their tests - live.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from .. import status as st
from .. import tokens
from ..viewmodel.types import ComponentStatusView, ReadinessSegmentView
from .statusbadge import badge_text, badge_tooltip

#: Column layout of the element grid.  ``source`` and ``age`` are not optional
#: extras: a status with no stated provenance and no freshness is an assertion,
#: not evidence.
COLUMNS: Tuple[Tuple[str, str, int], ...] = (
    ("element", "Element", 240),
    ("kind", "Kind", 120),
    ("status", "Status", 220),
    ("readiness", "Readiness", 130),
    ("source", "Status source", 190),
    ("age", "Observed", 130),
    ("version", "Version / release / profile", 230),
    ("reason", "Reason", 320),
)


#: What the version column says when the projection reported no provenance and
#: no reason either.  A reason is always expected - ``live_ops`` attaches one -
#: so this is the floor, not the normal path: even a view built by hand states a
#: vocabulary word rather than the pre-measurement dash, which would promise a
#: measurement that is never coming.
UNREPORTED_VERSION_REASON = ("no version, release or profile was reported for "
                             "this element")


def version_cell(view: ComponentStatusView) -> str:
    """The version / release / profile cell.  Never a bare placeholder.

    phaseB_task.md section 2 asks for the applied version / release / profile of
    each element, and ``integration-field-gaps.md`` GAP-13 records that the
    capability manifest declares it only for the software it names.  Showing an
    em dash for the rest reads as "not measured yet"; the operator cannot tell
    that apart from a value the console failed to fetch.  So an element with no
    declared provenance renders the status vocabulary's ``Unsupported`` with its
    reason and gap id, which is a statement rather than a blank.
    """
    declared = " / ".join(part for part in
                          (view.version, view.release, view.profile) if part)
    if declared:
        return declared
    status = view.version_status or st.UNSUPPORTED
    # The gap id rides with the status word rather than trailing the reason.
    # This cell is one column of a table: the tail is what a narrow column
    # clips, and the gap id is the part an operator takes to the gap register.
    # The reason is never lost - it is the same string in :meth:`tooltip`.
    label = st.resolve(status).label
    if view.version_gap_id:
        label = f"{label} [{view.version_gap_id}]"
    return badge_text(status, label=label,
                      reason=view.version_reason or UNREPORTED_VERSION_REASON)


def element_row(view: ComponentStatusView) -> Tuple[str, ...]:
    """One grid row, already formatted.  Pure, so the grid is assertable."""
    age = st.format_age(view.age_ms) if view.age_ms is not None \
        else st.format_utc(view.observed_at)
    if view.freshness and view.freshness != st.FRESHNESS_UNKNOWN:
        age = f"{age} ({view.freshness})"
    return (
        view.label,
        view.kind,
        badge_text(view.status),
        view.readiness or st.PRE_MEASUREMENT,
        view.source or st.PRE_MEASUREMENT,
        age,
        version_cell(view),
        " ".join(part for part in (view.status_reason,
                                   f"[{view.gap_id}]" if view.gap_id else "")
                 if part) or "",
    )


def segment_text(segment: ReadinessSegmentView) -> str:
    """One readiness segment as glyph-led text.

    Only the *blocking* segment carries its reason.  Repeating "blocked by X" on
    every downstream segment would fill the strip with the same sentence and
    push the one that matters off the edge, so downstream segments say only that
    they were not reached.
    """
    text = badge_text(segment.status, label=segment.label)
    if segment.blocked_by:
        return f"{text} (not reached)"
    if segment.unmet_reason:
        return f"{text}: {segment.unmet_reason}"
    return text


def blocked_segment(segments: Sequence[ReadinessSegmentView]
                    ) -> Optional[ReadinessSegmentView]:
    """The first segment that is not confirmed.

    This is the answer to "which part of intent -> policy -> evidence is
    stopping me", which is the question the strip exists to answer.
    """
    for segment in segments:
        if not segment.confirmed:
            return segment
    return None


class ReadinessStrip:
    """Horizontal intent -> policy -> evidence chain."""

    def __init__(self, *, theme: str = tokens.DEFAULT_THEME,
                 scale: float = tokens.SCALE_NORMAL) -> None:
        self.theme = theme
        self.scale = scale
        self.frame = None
        self._palette = tokens.theme(theme)
        self._labels: list = []

    def build(self, parent) -> None:
        import tkinter as tk

        self.frame = tk.Frame(parent, bg=self._palette["panel_bg"])

    def update(self, segments: Sequence[ReadinessSegmentView]) -> None:
        if self.frame is None:
            return
        import tkinter as tk

        for label in self._labels:
            label.destroy()
        self._labels = []
        for index, segment in enumerate(segments):
            if index:
                arrow = tk.Label(self.frame, text="->",
                                 bg=self._palette["panel_bg"],
                                 fg=self._palette["fg_muted"],
                                 font=tokens.font("small", scale=self.scale))
                arrow.pack(side="left", padx=tokens.SPACING["hair"])
                self._labels.append(arrow)
            label = tk.Label(
                self.frame, text=segment_text(segment),
                bg=self._palette["panel_bg"],
                fg=tokens.status_color(segment.status, self.theme),
                font=tokens.font("small", scale=self.scale, bold=True))
            label.pack(side="left", padx=tokens.SPACING["tight"])
            self._labels.append(label)


class TopologyGrid:
    """The logical O-RAN element table."""

    def __init__(self, *, theme: str = tokens.DEFAULT_THEME,
                 scale: float = tokens.SCALE_NORMAL) -> None:
        self.theme = theme
        self.scale = scale
        self.frame = None
        self.tree = None
        self._palette = tokens.theme(theme)

    def build(self, parent) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.frame = tk.Frame(parent, bg=self._palette["bg"])
        self.tree = ttk.Treeview(
            self.frame, columns=[key for key, _, _ in COLUMNS],
            show="headings", height=12)
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

    def update(self, elements: Sequence[ComponentStatusView]) -> None:
        """Tk thread only.  Repaints the whole grid; it is bounded and small."""
        if self.tree is None:
            return
        self.tree.delete(*self.tree.get_children())
        ordered = sorted(
            elements,
            key=lambda view: (-st.STATUS_SPECS.get(
                view.status, st.STATUS_SPECS[st.UNKNOWN]).severity, view.label))
        for view in ordered:
            self.tree.insert("", "end", iid=view.element_id,
                             values=element_row(view), tags=(view.status,))

    def tooltip(self, view: ComponentStatusView) -> str:
        # The version line is here because its cell is the one most likely to be
        # clipped by the column: an operator who cannot read the reason in the
        # grid must still be able to reach it without leaving the screen.
        return "\n".join((
            badge_tooltip(view.status, source=view.source,
                          observed_at=view.observed_at, age_ms=view.age_ms,
                          freshness=view.freshness,
                          reason=view.status_reason),
            f"version / release / profile: {version_cell(view)}",
        ))


__all__ = ["COLUMNS", "UNREPORTED_VERSION_REASON", "ReadinessStrip",
           "TopologyGrid", "blocked_segment", "element_row", "segment_text",
           "version_cell"]
