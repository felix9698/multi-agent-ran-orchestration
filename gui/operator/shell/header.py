"""The always-visible header.

phaseB_task.md section 1 names seven things the operator must be able to read at
any moment without navigating anywhere:

1. the session/run id and the profile,
2. the experiment state and elapsed time,
3. the system health roll-up,
4. the current LLM backend/model,
5. the data recording state,
6. the most recent warning or error,

plus - added by this design rather than by the task, and non-negotiable - the
**mode badge**.  A console that can replay a stored run must never let a
replayed run read as a live one, and the header is where that is settled.

:func:`header_fields` is pure: it turns a :class:`SessionState` into the exact
strings the bar renders.  That is what makes the header assertable in a hermetic
test, and it is why no formatting logic lives in the widget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from .. import status as st
from .. import tokens
from ..viewmodel.types import SessionState

#: Mode badges.  ``LIVE`` is the only one that means a live radio; every other
#: value is rendered with its own colour and its own text so a screenshot of the
#: header is unambiguous.
MODE_LABELS: dict = {
    "DISCONNECTED": "DISCONNECTED",
    "LIVE": "LIVE",
    "REPLAY": "REPLAY",
    "SYNTHETIC": "SYNTHETIC",
    "EMULATED": "EMULATED",
}


@dataclass(frozen=True)
class HeaderField:
    """One header cell: label, value, status and an optional secondary line."""

    key: str
    label: str
    value: str
    status: str = st.OK
    detail: Optional[str] = None

    @property
    def glyph(self) -> str:
        return st.resolve(self.status).glyph

    def as_text(self) -> str:
        """Glyph-led single line, for logs, tests and low-fidelity screenshots."""
        text = f"{self.glyph} {self.label}: {self.value}"
        return f"{text} ({self.detail})" if self.detail else text


def format_duration(seconds: Optional[float]) -> str:
    """``h:mm:ss`` elapsed rendering, or the pre-measurement placeholder."""
    if seconds is None or isinstance(seconds, bool):
        return st.PRE_MEASUREMENT
    if not isinstance(seconds, (int, float)):
        return st.PRE_MEASUREMENT
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        return st.PRE_MEASUREMENT
    if seconds < 0:
        return st.PRE_MEASUREMENT
    total = int(seconds)
    return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def format_bytes(count: Optional[int]) -> str:
    """Compact byte rendering.  ``0`` is a real value and renders as ``0 B``."""
    if count is None or isinstance(count, bool) or not isinstance(count, int):
        return st.PRE_MEASUREMENT
    if count < 1024:
        return f"{count} B"
    if count < 1024 ** 2:
        return f"{count / 1024:.1f} KiB"
    if count < 1024 ** 3:
        return f"{count / 1024 ** 2:.1f} MiB"
    return f"{count / 1024 ** 3:.2f} GiB"


def mode_status(mode: str) -> str:
    """GUI status used to colour the mode badge.

    A replayed or synthetic run is not an error and not a degradation - it is a
    different *kind* of run - so it takes ``NOT_APPLICABLE``'s neutral slot
    rather than borrowing a health colour that would misread as a fault.

    ``DISCONNECTED`` is the console before any session: nothing has been
    contacted and nothing is being observed.  It reads UNKNOWN, because "we do
    not know" is exactly the state, and it must not look like a healthy idle.
    """
    if mode == "DISCONNECTED":
        return st.UNKNOWN
    return st.OK if mode == "LIVE" else st.NOT_APPLICABLE


def _health_detail(counts: Mapping[str, int]) -> Optional[str]:
    if not counts:
        return None
    ordered = sorted(counts.items(),
                     key=lambda kv: (-st.STATUS_SPECS.get(
                         kv[0], st.STATUS_SPECS[st.UNKNOWN]).severity, kv[0]))
    return "  ".join(f"{st.resolve(k).glyph}{k}:{v}" for k, v in ordered if v)


def header_fields(state: SessionState, *,
                  bus_stats: Optional[Mapping[str, Any]] = None
                  ) -> Tuple[HeaderField, ...]:
    """Project a :class:`SessionState` onto the seven header cells plus the mode.

    Nothing here invents a value.  An absent run id, an absent profile and an
    absent backend all render the em-dash placeholder, because a plausible
    default in the header is the fastest way to mislead an operator.
    """
    run_id = state.run_id or st.PRE_MEASUREMENT
    profile = state.profile_id or st.PRE_MEASUREMENT
    if state.profile_valid is False:
        profile_status, profile_detail = st.ERROR, "profile failed validation"
    elif state.profile_valid is None:
        profile_status, profile_detail = st.UNKNOWN, "not validated"
    else:
        profile_status, profile_detail = st.OK, None

    disposition = state.disposition or "UNKNOWN"
    run_status = {
        "RUNNING": st.OK, "COMPLETED": st.OK, "ABORTED": st.BLOCKED,
        "FAILED": st.ERROR, "INTERRUPTED": st.DEGRADED,
    }.get(disposition, st.UNKNOWN)

    backend = state.active_backend or st.PRE_MEASUREMENT
    backend_status, backend_detail = st.UNKNOWN, "no backend selected"
    for entry in state.llm_backends:
        if entry.is_active or entry.name == state.active_backend:
            backend = entry.name
            if entry.model_version:
                backend = f"{entry.name} / {entry.model_version}"
            backend_status = entry.availability or st.UNKNOWN
            backend_detail = entry.availability_reason
            break

    if state.recording:
        # A zero byte count means "the store has not reported one yet", not
        # "nothing was written".  Rendering it as 0 B would be a measurement
        # claim the console cannot make.
        recording_value = ("recording  " + format_bytes(state.recorded_bytes)
                           if state.recorded_bytes
                           else f"recording  {st.PRE_MEASUREMENT} written")
        recording_status = st.OK
    else:
        recording_value = "not recording"
        recording_status = st.NOT_APPLICABLE if state.run_id else st.UNKNOWN

    warning = state.last_warning
    if warning is None:
        warning_value, warning_status, warning_detail = "none", st.OK, None
    else:
        warning_value = warning.title or warning.kind
        warning_status = st.ERROR if warning.severity == "ERROR" else st.DEGRADED
        ids = [f"{name}={value}" for name, value in (
            ("intent", warning.intent_id), ("policy", warning.policy_id),
            ("run", warning.run_id), ("component", warning.component)) if value]
        stamp = st.format_utc(warning.t_utc) if warning.t_utc else None
        warning_detail = "  ".join([p for p in ([stamp] if stamp else []) + ids])

    dropped = int(state.dropped_updates or 0)
    if bus_stats:
        dropped = int(bus_stats.get("dropped", dropped) or 0)

    fields = [
        HeaderField("run", "Run", run_id, run_status,
                    f"disposition {disposition}"),
        HeaderField("mode", "Mode", MODE_LABELS.get(state.mode, state.mode),
                    mode_status(state.mode),
                    None if state.is_live else "not a live radio"),
        HeaderField("profile", "Profile", profile, profile_status,
                    profile_detail),
        HeaderField("elapsed", "Elapsed", format_duration(state.elapsed_s),
                    st.OK if state.elapsed_s is not None else st.UNKNOWN,
                    f"started {st.format_utc(state.started_at)}"
                    if state.started_at else None),
        HeaderField("health", "Health", st.resolve(state.health).label,
                    state.health, _health_detail(state.health_counts)),
        HeaderField("llm", "LLM", backend, backend_status, backend_detail),
        HeaderField("recording", "Recording", recording_value, recording_status,
                    f"{dropped} update(s) shed" if dropped else None),
        HeaderField("alert", "Latest alert", warning_value, warning_status,
                    warning_detail),
    ]
    return tuple(fields)


class HeaderBar:
    """The Tk rendering of :func:`header_fields`.

    Holds a frame rather than subclassing one so this module imports with no
    toolkit present.
    """

    def __init__(self, *, theme: str = tokens.DEFAULT_THEME,
                 scale: float = tokens.SCALE_NORMAL) -> None:
        self.theme = theme
        self.scale = scale
        self.frame = None
        self._cells: dict = {}
        self._palette = tokens.theme(theme)

    def build(self, parent) -> None:
        import tkinter as tk

        pad = tokens.SPACING["tight"]
        self.frame = tk.Frame(parent, bg=self._palette["panel_bg"])
        for column, field in enumerate(header_fields(SessionState())):
            cell = tk.Frame(self.frame, bg=self._palette["panel_bg"])
            cell.grid(row=0, column=column, sticky="nsew",
                      padx=pad, pady=tokens.SPACING["hair"])
            self.frame.grid_columnconfigure(column, weight=1, uniform="hdr")
            label = tk.Label(cell, text=field.label, anchor="w",
                             bg=self._palette["panel_bg"],
                             fg=self._palette["fg_muted"],
                             font=tokens.font("micro", scale=self.scale))
            value = tk.Label(cell, text=st.PRE_MEASUREMENT, anchor="w",
                             bg=self._palette["panel_bg"],
                             fg=self._palette["fg"],
                             font=tokens.font("label", scale=self.scale,
                                              bold=True))
            detail = tk.Label(cell, text="", anchor="w",
                              bg=self._palette["panel_bg"],
                              fg=self._palette["fg_muted"],
                              font=tokens.font("micro", scale=self.scale))
            label.pack(fill="x")
            value.pack(fill="x")
            detail.pack(fill="x")
            self._cells[field.key] = (value, detail)

    def update(self, state: SessionState, *,
               bus_stats: Optional[Mapping[str, Any]] = None) -> None:
        """Tk thread only.  Repaints the cells; performs no I/O."""
        if self.frame is None:
            return
        for field in header_fields(state, bus_stats=bus_stats):
            widgets = self._cells.get(field.key)
            if widgets is None:
                continue
            value, detail = widgets
            value.configure(text=f"{field.glyph} {field.value}",
                            fg=tokens.status_color(field.status, self.theme))
            detail.configure(text=field.detail or "")


__all__ = ["HeaderBar", "HeaderField", "MODE_LABELS", "format_bytes",
           "format_duration", "header_fields", "mode_status"]
