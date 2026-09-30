"""Intent & Decision workspace.

This renderer consumes only ``SessionState`` and callback commands supplied by
the shell.  It performs no coordinator, LLM, lifecycle or storage operation on
the Tk thread.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, Any, Callable, List, Mapping, Optional

from gui.operator.status import describe, resolve
from gui.operator.viewmodel.types import IntentRowView, SessionState
from gui.operator.widgets.correlation_trace import CorrelationTracePanel
from gui.operator.widgets.decision_panel import CalibrationPanel, DecisionPanel
from gui.operator.widgets.intent_form import IntentForm
from gui.operator.widgets.agent_sitting import AgentSittingPanel
from gui.operator.widgets.intent_table import IntentTable


class IntentDecisionWorkspace:
    id = "intent_decision"
    title = "Intent & Decision"

    def __init__(self, bus=None, *,
                 on_preview: Optional[Callable[[Mapping[str, Any]], None]] = None,
                 on_submit: Optional[Callable[[Mapping[str, Any]], None]] = None,
                 on_withdraw: Optional[Callable[[IntentRowView], None]] = None,
                 on_model_switch: Optional[Callable[[str], None]] = None,
                 on_model_refresh: Optional[Callable[[], None]] = None,
                 on_role_models: Optional[Callable[[Mapping[str, Optional[str]]], None]] = None,
                 role_models: Optional[Mapping[str, Optional[str]]] = None,
                 on_run_sitting=None, on_stop_sitting=None, intent_set_path=None) -> None:
        self.intent_set_path = intent_set_path
        self.bus = bus
        self._commands = {
            "preview": on_preview, "submit": on_submit, "withdraw": on_withdraw,
            "switch": on_model_switch, "refresh": on_model_refresh,
            "role_models": on_role_models,
            "run_sitting": on_run_sitting, "stop_sitting": on_stop_sitting,
        }
        # Each agent uses the model the operator picks here.
        self._role_models: Dict[str, Optional[str]] = {
            "target": None, "control": None, "trajectory": None, "monolith": None, **dict(role_models or {})}
        self._state = SessionState()
        self._root = None
        self._unsubscribers: List[Callable[[], None]] = []
        self._selected_intent: Optional[str] = None

    def build(self, parent) -> None:
        import tkinter as tk
        from tkinter import ttk

        self._root = ttk.Frame(parent)
        self._root.pack(fill="both", expand=True)
        top = ttk.PanedWindow(self._root, orient="horizontal")
        top.pack(fill="both", expand=True, padx=5, pady=5)
        left = ttk.Frame(top)
        right = ttk.Frame(top)
        top.add(left, weight=4)
        top.add(right, weight=6)

        def place_workspace_sash(event=None) -> None:
            width = event.width if event is not None else self._root.winfo_width()
            if width < 1000:
                return
            target = int(width * 0.45)
            try:
                if abs(top.sashpos(0) - target) > 3:
                    top.sashpos(0, target)
            except tk.TclError:
                pass

        self._root.after_idle(place_workspace_sash)
        top.bind("<Configure>", place_workspace_sash, add=True)

        self.form = IntentForm(left, on_preview=self._commands["preview"],
                               on_submit=self._commands["submit"])
        self.form.pack(fill="x", pady=(0, 5))
        self.table = IntentTable(left, on_select=self._select)
        self.table.pack(fill="both", expand=True)
        actions = ttk.Frame(left)
        actions.pack(fill="x", pady=4)
        self._withdraw_button = ttk.Button(actions, text="Withdraw selected…",
                                           command=self._withdraw)
        self._withdraw_button.pack(side="right")

        model_bar = ttk.LabelFrame(right, text="LLM backend / model")
        model_bar.pack(fill="x", pady=(0, 5))
        self._model = tk.StringVar()
        self._model_combo = ttk.Combobox(model_bar, textvariable=self._model,
                                         state="readonly", width=36)
        self._model_combo.pack(side="left", padx=5, pady=4)
        self._model_status = ttk.Label(model_bar, text="? Unknown")
        self._model_status.pack(side="left", padx=5)
        ttk.Button(model_bar, text="Select for next episode",
                   command=self._switch_model).pack(side="right", padx=4)
        ttk.Button(model_bar, text="Refresh", command=self._refresh_models).pack(side="right")

        results = ttk.Notebook(right)
        results.pack(fill="both", expand=True)
        sitting_page = ttk.Frame(results)
        context_page = ttk.Frame(results)
        results.add(sitting_page, text="Agent sitting")
        results.add(context_page, text="Submission context")
        self.sitting = AgentSittingPanel(
            sitting_page, sentence=lambda: self.form.values()["intentText"],
            on_run=self._commands["run_sitting"], on_stop=self._commands["stop_sitting"],
            on_models=self._commands["role_models"], choices=self._role_models, intent_set_path=self.intent_set_path)
        self.sitting.frame.pack(fill="both", expand=True, pady=(0, 5))

        self.calibration = CalibrationPanel(context_page)
        self.calibration.pack(fill="x", pady=(0, 5))
        self.correlation = CorrelationTracePanel(context_page)
        self.correlation.pack(fill="x", pady=(0, 5))
        self.decision = DecisionPanel(context_page)
        self.decision.pack(fill="both", expand=True)
        self._bind_bus()
        self.on_state(self._state)
        if self.bus is not None:
            record = self.bus.snapshot("decision")
            if isinstance(record, Mapping):
                self.sitting.set_record(record)

    @property
    def widget(self):
        return self._root

    def _bind_bus(self) -> None:
        if self.bus is None:
            return
        for channel in ("session", "intent", "decision", "llm"):
            try:
                self._unsubscribers.append(self.bus.subscribe(channel, self._on_bus))
            except Exception:
                pass

    def _on_bus(self, payload: Any) -> None:
        # Shell normally publishes complete SessionState snapshots.  The
        # channel-specific fallback keeps the workspace useful in isolation.
        if isinstance(payload, Mapping) and payload.get("kind") == "agent-sitting":
            self.sitting.set_record(payload)
        elif isinstance(payload, SessionState):
            self.on_state(payload)
        elif channel_value := getattr(payload, "__class__", None):
            name = channel_value.__name__
            if name == "DecisionView":
                self.on_state(replace(self._state, decision=payload))
            elif name == "IntentRowView":
                rows = tuple(r for r in self._state.intents if r.intent_id != payload.intent_id) + (payload,)
                self.on_state(replace(self._state, intents=rows))

    def on_state(self, state: SessionState) -> None:
        self._state = state
        if self._root is None:
            return
        self.table.set_rows(state.intents)
        self.decision.set_decision(state.decision)
        self.correlation.set_trace(state.correlation_trace)
        self.calibration.set_calibration(state.calibration)
        names = tuple(item.name for item in state.llm_backends)
        self._model_combo.configure(values=names)
        self.sitting.set_backends(names)
        if state.active_backend:
            self._model.set(state.active_backend)
        selected = next((item for item in state.llm_backends
                         if item.name == self._model.get()), None)
        if selected is None:
            self._model_status.configure(text="? Unknown · no observation")
        else:
            text = describe(resolve(selected.availability,
                                    reason=selected.availability_reason))
            latency = (f" · last {selected.last_latency_ms:.0f} ms"
                       if selected.last_latency_ms is not None else "")
            self._model_status.configure(text=text + latency)

    def _select(self, intent_id: str) -> None:
        self._selected_intent = intent_id

    def _withdraw(self) -> None:
        row = next((r for r in self._state.intents
                    if r.intent_id == self._selected_intent), None)
        callback = self._commands["withdraw"]
        if row is not None and callback is not None:
            callback(row)

    def _switch_model(self) -> None:
        callback = self._commands["switch"]
        if callback is not None and self._model.get():
            callback(self._model.get())

    def _refresh_models(self) -> None:
        callback = self._commands["refresh"]
        if callback is not None:
            callback()

    def on_activate(self) -> None:
        if self.bus is not None:
            try:
                state = self.bus.snapshot("session")
                if isinstance(state, SessionState):
                    self.on_state(state)
            except Exception:
                pass

    def on_deactivate(self) -> None:
        return None

    def gui_state(self) -> Mapping[str, Any]:
        return {"selectedIntent": self._selected_intent,
                "search": self.table.search.get() if self._root is not None else "",
                "lifecycle": self.table.lifecycle.get() if self._root is not None else "ALL",
                "model": self._model.get() if self._root is not None else None}

    def restore_gui_state(self, state: Mapping[str, Any]) -> None:
        self._selected_intent = state.get("selectedIntent")
        if self._root is not None:
            self.table.search.set(str(state.get("search") or ""))
            lifecycle = str(state.get("lifecycle") or "ALL")
            self.table.lifecycle.set(lifecycle)
            self._model.set(str(state.get("model") or ""))

    def destroy(self) -> None:
        for unsubscribe in self._unsubscribers:
            try:
                unsubscribe()
            except Exception:
                pass
        self._unsubscribers.clear()
        if self._root is not None:
            self._root.destroy()
            self._root = None


# Shorter integration name.
IntentDecision = IntentDecisionWorkspace


__all__ = ["IntentDecision", "IntentDecisionWorkspace"]
