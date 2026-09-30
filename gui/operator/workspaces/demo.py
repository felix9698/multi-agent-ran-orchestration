"""Full-screen-safe presentation projection of the shared session state."""

from __future__ import annotations

from typing import Any, Mapping

from gui.operator.status import describe, resolve
from gui.operator.viewmodel.types import SessionState
from gui.operator.widgets.constellation import ConstellationPanel, constellation_state
from gui.operator.widgets.kpi_hero import KpiHero


def demo_snapshot(state: SessionState) -> Mapping[str, Any]:
    """Build display data without changing the shared session context."""
    decision = state.decision
    summary = state.summary
    return {
        "run_id": state.run_id or "—",
        "mode_badge": state.mode,
        "mode_badge_can_hide": False,
        "health": describe(resolve(state.health)),
        "intent": decision.intent_text if decision else "No submitted intent",
        "decision": decision.eq12_state if decision and decision.eq12_state else "—",
        "flow": " → ".join(segment.label for segment in state.readiness) or "No readiness evidence",
        "outcome": summary.disposition if summary else state.disposition,
    }


def demo_state_label(health: str, disposition: str, *, case: str | None = None) -> str:
    """Give the error matrix a textual, non-colour-only presentation label."""
    suffix = f" ({case.replace('_', ' ').title()})" if case and case != "NORMAL" else ""
    return f"{describe(resolve(health), include_reason=False)} — {disposition}{suffix}"


class DemoWorkspace:
    """One high-contrast workspace which consumes only ``SessionState``."""

    id = "demo"
    title = "Demo View"

    def __init__(self) -> None:
        self._frame = None
        self._labels = {}
        self._kpi = None
        self._radio = None
        self._state = SessionState()
        self._fullscreen = False

    def build(self, parent) -> None:
        import tkinter as tk

        self._frame = tk.Frame(parent, bg="#ffffff")
        self._frame.pack(fill="both", expand=True)
        header = tk.Frame(self._frame, bg="#111111")
        header.pack(fill="x")
        self._labels["mode"] = tk.Label(header, bg="#111111", fg="#ffffff", font=("DejaVu Sans", 18, "bold"))
        self._labels["run"] = tk.Label(header, bg="#111111", fg="#ffffff", font=("DejaVu Sans", 14))
        self._labels["mode"].pack(side="left", padx=16, pady=10)
        self._labels["run"].pack(side="right", padx=16, pady=10)
        body = tk.Frame(self._frame, bg="#ffffff")
        body.pack(fill="both", expand=True, padx=16, pady=16)
        for key, heading in (("intent", "Operator Intent"), ("decision", "S0–S6 / Decision"),
                             ("flow", "Intent → policy / xApp"), ("health", "System Health"),
                             ("outcome", "Before / After Outcome")):
            card = tk.LabelFrame(body, text=heading, font=("DejaVu Sans", 14, "bold"), padx=8, pady=8)
            card.pack(fill="x", pady=4)
            label = tk.Label(card, anchor="w", justify="left", font=("DejaVu Sans", 16))
            label.pack(fill="x")
            self._labels[key] = label
        lower = tk.Frame(body)
        lower.pack(fill="both", expand=True, pady=8)
        kpi_parent = tk.LabelFrame(lower, text="Headline KPI", font=("DejaVu Sans", 14, "bold"))
        kpi_parent.pack(side="left", fill="both", expand=True, padx=(0, 4))
        self._kpi = KpiHero(kpi_parent)
        radio_parent = tk.LabelFrame(lower, text="Radio state", font=("DejaVu Sans", 14, "bold"))
        radio_parent.pack(side="left", fill="both", expand=True, padx=(4, 0))
        self._radio = ConstellationPanel(radio_parent)
        self.on_state(self._state)

    def on_state(self, state: SessionState) -> None:
        self._state = state
        if self._frame is None:
            return
        view = demo_snapshot(state)
        self._labels["mode"].configure(text=f"MODE: {view['mode_badge']}")
        self._labels["run"].configure(text=f"Run {view['run_id']}")
        for key in ("intent", "decision", "flow", "health", "outcome"):
            self._labels[key].configure(text=view[key])
        if self._kpi is not None:
            self._kpi.set_metric(next((metric for metric in state.metrics if metric.status == "OK"), None))
        if self._radio is not None:
            self._radio.set_state(constellation_state(state.metrics, ()))

    def on_activate(self) -> None:
        if self._frame is not None:
            self._frame.focus_set()

    def on_deactivate(self) -> None:
        return None

    def gui_state(self) -> Mapping[str, Any]:
        return {"fullscreen": self._fullscreen}

    def restore_gui_state(self, state: Mapping[str, Any]) -> None:
        self._fullscreen = bool(state.get("fullscreen", False))

    def toggle_fullscreen(self) -> None:
        self._fullscreen = not self._fullscreen
        if self._frame is not None:
            self._frame.winfo_toplevel().attributes("-fullscreen", self._fullscreen)
