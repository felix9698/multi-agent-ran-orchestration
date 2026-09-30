"""Compact, non-colour-only S0-S6 and three-stage decision visualisation."""

from __future__ import annotations

from typing import Any, List, Sequence

from gui.operator.status import PRE_MEASUREMENT
from gui.operator.tokens import status_color
from gui.operator.viewmodel.types import StageView


_STATE_GLYPH = {
    "PENDING": "○", "WAITING": "◔", "RUNNING": "▶", "DONE": "●",
    "FAILED": "✕", "SKIPPED": "–",
}
_STATE_STATUS = {
    "PENDING": "UNKNOWN", "WAITING": "UNKNOWN", "RUNNING": "DEGRADED",
    "DONE": "OK", "FAILED": "ERROR", "SKIPPED": "NOT_APPLICABLE",
}


class FSMView:
    """Dense stage strip; text and glyph remain meaningful in greyscale.

    Composition rather than a ``ttk.Frame`` subclass, and the toolkit is
    imported inside the constructor.  A base class is evaluated at import time,
    so subclassing would pull tkinter into every process that merely imports
    ``gui.operator`` - including the export pipeline and any machine with no
    display.  ``tests/gui/test_shell_and_bus.py`` asserts that by AST.
    """

    def __init__(self, parent, *, title: str = "S0–S6 decision path") -> None:
        from tkinter import ttk

        self.frame = ttk.Frame(parent)
        self._title = ttk.Label(self.frame, text=title, style="Heading.TLabel")
        self._title.pack(anchor="w", padx=6, pady=(4, 2))
        self._body = ttk.Frame(self.frame)
        self._body.pack(fill="x", expand=True, padx=4, pady=4)
        self._cells: List[Any] = []

    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    def grid(self, **kwargs) -> None:
        self.frame.grid(**kwargs)

    def destroy(self) -> None:
        self.frame.destroy()

    def set_stages(self, stages: Sequence[StageView]) -> None:
        from tkinter import ttk
        import tkinter as tk

        for child in self._body.winfo_children():
            child.destroy()
        self._cells.clear()
        if not stages:
            label = ttk.Label(self._body, text=f"? {PRE_MEASUREMENT} · Unknown")
            label.pack(anchor="w")
            self._cells.append(label)
            return
        for index, stage in enumerate(stages):
            state = stage.state if stage.state in _STATE_GLYPH else "PENDING"
            duration = (f"{stage.duration_ms:.0f} ms"
                        if isinstance(stage.duration_ms, (int, float))
                        and not isinstance(stage.duration_ms, bool)
                        else PRE_MEASUREMENT)
            cell = tk.Label(
                self._body,
                text=f"{_STATE_GLYPH[state]} {stage.stage_id}\n{stage.label}\n{state} · {duration}",
                justify="center", relief="groove", padx=7, pady=5,
                foreground=status_color(_STATE_STATUS[state]),
            )
            cell.grid(row=0, column=index, sticky="nsew", padx=2)
            self._body.columnconfigure(index, weight=1)
            self._cells.append(cell)


# Name used in a few design notes.
S0S6View = FSMView


__all__ = ["FSMView", "S0S6View"]
