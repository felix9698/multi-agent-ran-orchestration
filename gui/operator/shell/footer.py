"""The always-visible footer: intent entry, primary actions, bus health, clock.

The footer carries the natural-language intent field because phaseB_task.md
section 3 asks for the existing input experience to be *preserved*, and in the
legacy console that field is always on screen.  Making it part of the chrome
rather than of one workspace means an operator can submit an intent from
wherever they are - including while watching the topology.

The footer also surfaces the bus drop counter.  That is deliberate: the state
bus sheds the oldest record under back-pressure, and a shed record that nobody
can see would be a silent data loss in a research instrument.

:func:`footer_fields` and :func:`footer_actions` are pure so both the readout
and the enable/disable rules are assertable without a display.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Tuple

from .. import status as st
from .. import tokens
from ..viewmodel.types import SessionState


@dataclass(frozen=True)
class FooterField:
    """One footer readout."""

    key: str
    label: str
    value: str
    status: str = st.OK

    @property
    def glyph(self) -> str:
        return st.resolve(self.status).glyph


@dataclass(frozen=True)
class ActionSpec:
    """One primary action and whether it is currently legal.

    A disabled action keeps its ``reason``.  A control that simply disappears
    when it is unavailable teaches the operator nothing, which is the same rule
    the Settings view follows for unsupported connections.
    """

    key: str
    label: str
    enabled: bool
    reason: Optional[str] = None
    destructive: bool = False


def utc_now_text(now: Optional[datetime] = None) -> str:
    """Second-precision UTC.  Local time is never displayed anywhere."""
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def footer_fields(state: SessionState, *,
                  bus_stats: Optional[Mapping[str, Any]] = None,
                  now: Optional[datetime] = None) -> Tuple[FooterField, ...]:
    """The footer readouts: bus health and the UTC clock."""
    stats = dict(bus_stats or {})
    dropped = int(stats.get("dropped", state.dropped_updates) or 0)
    depth = stats.get("depth")
    rejected = int(stats.get("rejected", 0) or 0)
    fields = [
        FooterField("dropped", "Shed updates", str(dropped),
                    st.OK if dropped == 0 else st.DEGRADED),
        FooterField("depth", "Bus depth",
                    st.PRE_MEASUREMENT if depth is None else str(depth),
                    st.OK if depth is not None else st.UNKNOWN),
        FooterField("utc", "UTC", utc_now_text(now), st.OK),
    ]
    if rejected:
        fields.insert(1, FooterField("rejected", "Misrouted", str(rejected),
                                     st.ERROR))
    return tuple(fields)


def footer_actions(state: SessionState) -> Tuple[ActionSpec, ...]:
    """Which primary actions are legal for ``state``.

    ``Start Experiment`` deliberately does not appear while a run is already
    open, and abort is only offered for a run that is actually running - a
    destructive control that is available when it cannot do anything is a
    misleading control.
    """
    running = state.disposition == "RUNNING" and state.run_id is not None
    return (
        ActionSpec("preflight", "Run Preflight", not running,
                   "a session is already running" if running else None),
        ActionSpec("start", "Start Experiment", not running,
                   "a session is already running" if running else None),
        ActionSpec("submit", "Submit Intent", running,
                   None if running else "start a session first"),
        ActionSpec("stop", "Stop and Finalize", running,
                   None if running else "no session is running"),
        ActionSpec("abort", "Abort", running,
                   None if running else "no session is running",
                   destructive=True),
    )


class FooterBar:
    """The Tk rendering of the footer.

    Holds callbacks rather than logic: the intent text and the action key are
    handed straight to the console, which decides what confirmation the action
    needs and which worker thread runs it.  No I/O happens here.
    """

    def __init__(self, *, theme: str = tokens.DEFAULT_THEME,
                 scale: float = tokens.SCALE_NORMAL,
                 on_action: Optional[Callable[[str, str], None]] = None) -> None:
        self.theme = theme
        self.scale = scale
        self.on_action = on_action
        self.frame = None
        self.entry = None
        self._palette = tokens.theme(theme)
        self._readouts: dict = {}
        self._buttons: dict = {}

    def build(self, parent) -> None:
        import tkinter as tk

        pad = tokens.SPACING["tight"]
        self.frame = tk.Frame(parent, bg=self._palette["panel_bg"])

        tk.Label(self.frame, text="Intent", bg=self._palette["panel_bg"],
                 fg=self._palette["fg_muted"],
                 font=tokens.font("small", scale=self.scale)
                 ).pack(side="left", padx=(pad, tokens.SPACING["hair"]))
        self.entry = tk.Entry(self.frame, bg=self._palette["panel_alt_bg"],
                              fg=self._palette["fg"],
                              insertbackground=self._palette["fg"],
                              font=tokens.font("body", scale=self.scale),
                              relief="flat")
        self.entry.pack(side="left", fill="x", expand=True, padx=pad)
        self.entry.bind("<Return>", lambda _event: self._fire("submit"))

        for spec in footer_actions(SessionState()):
            button = tk.Button(
                self.frame, text=spec.label, relief="flat",
                bg=self._palette["accent"] if not spec.destructive
                else tokens.status_color(st.ERROR, self.theme),
                fg=self._palette["accent_fg"],
                activebackground=self._palette["selection"],
                font=tokens.font("small", scale=self.scale),
                command=lambda key=spec.key: self._fire(key))
            button.pack(side="left", padx=tokens.SPACING["hair"])
            self._buttons[spec.key] = button

        for field in footer_fields(SessionState()):
            label = tk.Label(self.frame, text="", bg=self._palette["panel_bg"],
                             fg=self._palette["fg_muted"],
                             font=tokens.font("micro", scale=self.scale))
            label.pack(side="right", padx=pad)
            self._readouts[field.key] = label

    def _fire(self, key: str) -> None:
        if self.on_action is None:
            return
        text = self.entry.get().strip() if self.entry is not None else ""
        self.on_action(key, text)

    def clear_intent(self) -> None:
        if self.entry is not None:
            self.entry.delete(0, "end")

    def update(self, state: SessionState, *,
               bus_stats: Optional[Mapping[str, Any]] = None) -> None:
        """Tk thread only."""
        if self.frame is None:
            return
        for field in footer_fields(state, bus_stats=bus_stats):
            widget = self._readouts.get(field.key)
            if widget is not None:
                widget.configure(
                    text=f"{field.glyph} {field.label} {field.value}",
                    fg=tokens.status_color(field.status, self.theme))
        for spec in footer_actions(state):
            button = self._buttons.get(spec.key)
            if button is None:
                continue
            button.configure(state="normal" if spec.enabled else "disabled")


__all__ = ["ActionSpec", "FooterBar", "FooterField", "footer_actions",
           "footer_fields", "utc_now_text"]
