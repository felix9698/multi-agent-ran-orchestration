"""Structured decision evidence without a private-reasoning surface."""

from __future__ import annotations

from typing import Any, Dict, Optional

from gui.operator.status import PRE_MEASUREMENT, format_value
from gui.operator.viewmodel.types import CalibrationView, DecisionView
from gui.operator.widgets.fsm_view import FSMView
from gui.operator.widgets.modeltext import MODEL_TEXT_LABEL, TRUNCATED_LABEL


def _value(value, quantity: str = "") -> str:
    return format_value(value, quantity) if quantity else (
        PRE_MEASUREMENT if value is None else str(value))


class DecisionPanel:
    """Decision view with authoritative terminal and calibration fields.

    Composition, and the toolkit is imported in the constructor - see
    :class:`~gui.operator.widgets.fsm_view.FSMView` for why a base class would
    defeat the headless-import property.
    """

    def __init__(self, parent) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.frame = ttk.Frame(parent)
        self.fsm = FSMView(self.frame)
        self.fsm.pack(fill="x", padx=3, pady=3)
        self.llm = FSMView(self.frame, title="Three-stage LLM pipeline")
        self.llm.pack(fill="x", padx=3, pady=3)

        split = ttk.PanedWindow(self.frame, orient="horizontal")
        split.pack(fill="both", expand=True, padx=3, pady=3)
        self._facts = ttk.LabelFrame(split, text="Structured judgement")
        self._alternatives = ttk.LabelFrame(split, text="Candidates, negotiation and final choice")
        split.add(self._facts, weight=1)
        split.add(self._alternatives, weight=1)

        def place_detail_sash(event=None) -> None:
            width = (event.width if event is not None
                     else self.frame.winfo_width())
            if width < 600:
                return
            target = width // 2
            try:
                if abs(split.sashpos(0) - target) > 3:
                    split.sashpos(0, target)
            except tk.TclError:
                pass

        self.frame.after_idle(place_detail_sash)
        split.bind("<Configure>", place_detail_sash, add=True)
        self._fact_vars: Dict[str, Any] = {}
        labels = (
            ("interpreted", "Interpreted request"), ("goals", "Goals"),
            ("constraints", "Constraints"), ("conflict", "Conflict"),
            ("feasible", "Feasible"), ("raw", "Raw confidence"),
            ("calibrated", "Calibrated probability"), ("theta", "θ*"),
            ("threshold_on", "Threshold applied to"), ("route", "Routed to"),
            ("agreement", "Agreement"), ("success", "Success"),
            ("terminal", "Terminal outcome"), ("eq12", "Eq.12 state"),
            ("reason", "Terminal reason"),
        )
        for row, (key, label) in enumerate(labels):
            ttk.Label(self._facts, text=label).grid(row=row, column=0, sticky="nw", padx=4, pady=2)
            var = tk.StringVar(value=PRE_MEASUREMENT)
            ttk.Label(self._facts, textvariable=var, wraplength=420).grid(
                row=row, column=1, sticky="nw", padx=4, pady=2)
            self._fact_vars[key] = var
        self._facts.columnconfigure(1, weight=1)

        self._alternative_text = tk.Text(self._alternatives, height=9, state="disabled", wrap="word")
        self._alternative_text.pack(fill="both", expand=True, padx=4, pady=4)
        # Explicitly a bounded model summary, never a chain-of-thought panel.
        self._model_label = ttk.Label(self._alternatives, text=MODEL_TEXT_LABEL)
        self._model_label.pack(anchor="w", padx=4, pady=(4, 0))
        self._model_text = tk.Text(self._alternatives, height=4, state="disabled", wrap="word")
        self._model_text.pack(fill="x", padx=4, pady=(2, 4))

    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    def grid(self, **kwargs) -> None:
        self.frame.grid(**kwargs)

    def destroy(self) -> None:
        self.frame.destroy()

    @staticmethod
    def _set_text(widget: Any, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def set_decision(self, decision: Optional[DecisionView]) -> None:
        if decision is None:
            self.fsm.set_stages(())
            self.llm.set_stages(())
            for var in self._fact_vars.values():
                var.set(PRE_MEASUREMENT)
            self._set_text(self._alternative_text, PRE_MEASUREMENT)
            self._set_text(self._model_text, PRE_MEASUREMENT)
            return
        self.fsm.set_stages(decision.fsm_stages)
        self.llm.set_stages(decision.llm_stages)
        conflict = ("Unknown · GAP-03" if decision.has_conflict is None else
                    "Yes" if decision.has_conflict else "No")
        values = {
            "interpreted": decision.intent_text,
            "goals": ", ".join(decision.goals) or PRE_MEASUREMENT,
            "constraints": ", ".join(decision.constraints) or PRE_MEASUREMENT,
            "conflict": conflict,
            "feasible": _value(decision.feasible),
            "raw": _value(decision.raw_confidence, "confidence"),
            "calibrated": _value(decision.calibrated_probability, "confidence"),
            "theta": _value(decision.theta_star, "theta_star"),
            "threshold_on": decision.threshold_applied_to or PRE_MEASUREMENT,
            "route": decision.routed_to or PRE_MEASUREMENT,
            "agreement": _value(decision.agreement),
            "success": _value(decision.success),
            # Resolution is intentionally absent: it cannot distinguish an
            # ordinary refusal from TechnicalFailsafe.
            "terminal": decision.terminal_outcome or PRE_MEASUREMENT,
            "eq12": decision.eq12_state or PRE_MEASUREMENT,
            "reason": decision.terminal_reason or PRE_MEASUREMENT,
        }
        for key, value in values.items():
            self._fact_vars[key].set(value)
        lines = []
        for alt in decision.alternatives:
            marker = "✓ accepted" if alt.accepted else "candidate"
            confidence = _value(alt.confidence, "confidence")
            lines.append(f"{alt.alternative_id} · {marker} · confidence {confidence}\n{alt.description}")
        lines.append(f"Negotiation rounds: {_value(decision.negotiation_rounds)}")
        lines.append(f"Rollback: {_value(decision.rolled_back)}")
        self._set_text(self._alternative_text, "\n\n".join(lines))
        marker = f" · {TRUNCATED_LABEL}" if decision.reasoning_truncated else ""
        self._model_label.configure(text=MODEL_TEXT_LABEL + marker)
        self._set_text(self._model_text, decision.reasoning_summary or PRE_MEASUREMENT)


class CalibrationPanel:
    def __init__(self, parent) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.frame = ttk.LabelFrame(parent,
                                    text="Calibration · threshold · budgets")
        self._value = tk.StringVar(value=PRE_MEASUREMENT)
        ttk.Label(self.frame, textvariable=self._value, justify="left",
                  wraplength=620).pack(fill="x", padx=6, pady=5)

    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    def grid(self, **kwargs) -> None:
        self.frame.grid(**kwargs)

    def destroy(self) -> None:
        self.frame.destroy()

    def set_calibration(self, calibration: Optional[CalibrationView]) -> None:
        if calibration is None:
            self._value.set(PRE_MEASUREMENT)
            return
        lines = (
            f"Mode {calibration.mode or PRE_MEASUREMENT} · θ* {_value(calibration.theta_star, 'theta_star')} "
            f"· raw θ* {_value(calibration.theta_star_raw, 'theta_star')} · N_max {_value(calibration.n_max)}",
            f"Budget inputs C_worst {_value(calibration.c_worst)} · C_nego {_value(calibration.c_nego)} "
            f"· R_success {_value(calibration.r_success)} · C_episode {_value(calibration.c_episode)}",
            f"Used (global cross-partition): episodes {_value(calibration.total_episodes)}, "
            f"negotiations {_value(calibration.total_negotiations)}, rollbacks {_value(calibration.total_rollbacks)}",
            f"Active partition: {dict(calibration.partition_counters)} · ECE {_value(calibration.ece)} "
            f"· Brier {_value(calibration.brier)} · n {_value(calibration.sample_count)}",
        )
        self._value.set("\n".join(lines))


__all__ = ["CalibrationPanel", "DecisionPanel"]
