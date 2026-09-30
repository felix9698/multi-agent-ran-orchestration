"""Honest radio-state panel used by the presentation workspace.

The panel has one deliberately narrow contract: it renders supplied IQ points,
or a clearly marked derived view when actual SINR samples make that useful.  It
never makes points up for a visually pleasing display.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence, Tuple

from gui.operator.viewmodel.types import MetricAvailabilityView


@dataclass(frozen=True)
class ConstellationState:
    """Data safe to pass from the state layer to the small canvas renderer."""

    status: str
    reason: str | None
    label: str
    points: Tuple[Tuple[float, float], ...] = ()
    derived: bool = False


def constellation_state(metrics: Sequence[MetricAvailabilityView],
                        sinr_values: Iterable[float]) -> ConstellationState:
    """Return the only presentation state supported by available evidence."""
    sinr = tuple(float(value) for value in sinr_values)
    sinr_metric = next((item for item in metrics if "SINR" in item.metric.upper()), None)
    if sinr_metric is not None and sinr_metric.status == "OK" and len(sinr) >= 3:
        points = tuple((index - (len(sinr) - 1) / 2.0, value) for index, value in enumerate(sinr))
        return ConstellationState("OK", None, "Derived visualization", points, True)
    return ConstellationState("UNSUPPORTED", "GAP-10: no IQ source declared", "Radio state")


class ConstellationPanel:
    """A lightweight canvas panel; all updates occur on the Tk thread."""

    def __init__(self, parent, *, theme: str = "light") -> None:
        import tkinter as tk

        self._tk = tk
        self._canvas = tk.Canvas(parent, height=200, highlightthickness=1)
        self._canvas.pack(fill="both", expand=True)
        self.set_state(ConstellationState("UNSUPPORTED", "GAP-10: no IQ source declared", "Radio state"))

    def set_state(self, state: ConstellationState) -> None:
        self._canvas.delete("all")
        width = max(self._canvas.winfo_width(), 300)
        height = max(self._canvas.winfo_height(), 200)
        self._canvas.create_text(12, 12, anchor="nw", text=state.label, font=("DejaVu Sans", 14, "bold"))
        if state.status != "OK":
            self._canvas.create_text(width / 2, height / 2, text=f"⊘ Unsupported\n{state.reason or ''}",
                                     justify="center", font=("DejaVu Sans", 12))
            return
        self._canvas.create_line(20, height / 2, width - 20, height / 2, fill="#777777")
        self._canvas.create_line(width / 2, 40, width / 2, height - 20, fill="#777777")
        for x_value, y_value in state.points:
            x = width / 2 + x_value * 28
            y = height / 2 - y_value * 8
            self._canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#0072B2", outline="")
