"""The Cockpit's always-visible header row, and its one control.

Task section 9 names seven things that must be readable at any moment without
navigating anywhere:

1. current case / trial,
2. Kernel state,
3. active Target Vector,
4. harm reserve / charge,
5. measurement freshness / clock status,
6. Live / Replay / Disconnected mode,
7. Emergency Stop.

Six of the seven are readouts and the seventh is a control, and that asymmetry
is the point: Emergency Stop is the one thing an operator must be able to reach
from *every* workspace, so it lives in the chrome rather than in a pane.
Pressing it raises the console's ``kernel_estop`` action, which is task section
9.10's chain -- Kernel ``OPERATOR_ABORT``, Write Gateway stop, rollback,
recovery -- and nothing else: this widget does not drive a gateway and does not
decide a terminal.

The projection itself is :func:`gui.operator.sources.cockpit.header_items`,
which is pure.  This module is geometry, colour and one button.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from .. import status as st
from .. import tokens
from ..sources.cockpit import (
    COCKPIT_HEADER_KEYS,
    CockpitHeaderView,
    CockpitSnapshot,
    HeaderItem,
    header_items,
)

#: The action the Emergency Stop button raises.  The same key the Contract
#: Studio's own button raises, deliberately: two controls that mean the same
#: thing must route to the same handler or they will diverge.
EMERGENCY_STOP_ACTION: str = "kernel_estop"


def header_line(view: CockpitHeaderView) -> str:
    """The whole header as one line, for logs and hermetic assertions."""
    return "   ".join(item.as_text() for item in header_items(view))


class CockpitHeaderBar:
    """The Tk rendering of the seven items.  Holds a frame; imports lazily."""

    def __init__(self, *, theme: str = tokens.DEFAULT_THEME,
                 scale: float = tokens.SCALE_NORMAL,
                 on_action: Optional[Callable[[str, str], None]] = None
                 ) -> None:
        self.theme = theme
        self.scale = scale
        self.on_action = on_action
        self.frame = None
        self.stop_button = None
        self._palette = tokens.theme(theme)
        self._cells: Dict[str, Any] = {}
        self._view = CockpitHeaderView()

    def build(self, parent) -> None:
        import tkinter as tk

        pad = tokens.SPACING["tight"]
        self.frame = tk.Frame(parent, bg=self._palette["panel_alt_bg"])
        for column, item in enumerate(header_items(CockpitHeaderView())):
            cell = tk.Frame(self.frame, bg=self._palette["panel_alt_bg"])
            cell.grid(row=0, column=column, sticky="nsew", padx=pad,
                      pady=tokens.SPACING["hair"])
            self.frame.grid_columnconfigure(column, weight=1,
                                            uniform="cockpit-hdr")
            tk.Label(cell, text=item.label, anchor="w",
                     bg=self._palette["panel_alt_bg"],
                     fg=self._palette["fg_muted"],
                     font=tokens.font("micro", scale=self.scale)).pack(fill="x")
            if item.key == "emergency_stop":
                # The one control in the chrome.  Destructive colouring is not
                # decoration: it is the only button here that changes the
                # deployment, and it must not look like a readout.
                self.stop_button = tk.Button(
                    cell, text="EMERGENCY STOP", relief="flat",
                    bg=tokens.status_color(st.ERROR, self.theme),
                    fg=self._palette["accent_fg"],
                    font=tokens.font("label", scale=self.scale, bold=True),
                    command=self._fire_stop)
                self.stop_button.pack(fill="x")
                value = self.stop_button
            else:
                value = tk.Label(cell, text=st.PRE_MEASUREMENT, anchor="w",
                                 bg=self._palette["panel_alt_bg"],
                                 fg=self._palette["fg"],
                                 font=tokens.font("label", scale=self.scale,
                                                  bold=True))
                value.pack(fill="x")
            detail = tk.Label(cell, text="", anchor="w",
                              bg=self._palette["panel_alt_bg"],
                              fg=self._palette["fg_muted"],
                              font=tokens.font("micro", scale=self.scale))
            detail.pack(fill="x")
            self._cells[item.key] = (value, detail)

    def _fire_stop(self) -> None:
        if self.on_action is not None:
            self.on_action(EMERGENCY_STOP_ACTION, "")

    def update(self, view: CockpitHeaderView) -> None:
        """Tk thread only.  Repaints the cells; performs no I/O."""
        self._view = view
        if self.frame is None:
            return
        for item in header_items(view):
            widgets = self._cells.get(item.key)
            if widgets is None:
                continue
            value, detail = widgets
            if item.key == "emergency_stop":
                # Never hidden and never disabled: the console offers the stop
                # whenever there is a Kernel session, and when there is nothing
                # applied the Kernel itself answers that there is nothing to
                # stop.  A stop button that vanished under load would be the
                # one control an operator could not find when they needed it.
                value.configure(text=f"EMERGENCY STOP  ({item.value})")
            else:
                value.configure(
                    text=f"{st.resolve(item.status).glyph} {item.badge.glyph} "
                         f"{item.value}",
                    fg=tokens.status_color(item.status, self.theme))
            detail.configure(text=item.detail or "")

    def update_from_snapshot(self, snapshot: Any) -> None:
        """Accept a whole :class:`CockpitSnapshot` and take its header."""
        if isinstance(snapshot, CockpitSnapshot):
            self.update(snapshot.header)

    def items(self) -> Tuple[HeaderItem, ...]:
        """What the bar currently reads, for a test with no display."""
        return header_items(self._view)


__all__ = ["COCKPIT_HEADER_KEYS", "CockpitHeaderBar", "EMERGENCY_STOP_ACTION",
           "header_line"]
