"""Read-only integration and user-preference workspace."""

from __future__ import annotations

from typing import Any, Mapping

from gui.operator.status import describe, resolve
from gui.operator.viewmodel.types import SessionState
from gui.operator.widgets.topology import version_cell


def integration_snapshot(state: SessionState) -> Mapping[str, Any]:
    """Expose integration facts without reaching through the state boundary."""
    backends = []
    for backend in state.llm_backends:
        backends.append({
            "name": backend.name,
            "model": backend.model_version or "—",
            "availability": describe(resolve(backend.availability, reason=backend.availability_reason)),
            "active": backend.is_active,
            "credentials": tuple({"reference": ref, "configured": backend.credential_configured}
                                 for ref in backend.credential_refs),
        })
    components = tuple({
        "label": component.label,
        "kind": component.kind,
        "status": describe(resolve(component.status, reason=component.status_reason)),
        "source": component.source or "—",
        # Same rule and same words as the topology grid's version column: what
        # the manifest declares, or Unsupported with the reason and the gap id.
        "version": version_cell(component),
    } for component in state.components)
    metrics = tuple({
        "metric": metric.display,
        "status": describe(resolve(metric.status, reason=metric.reason)),
        "source": metric.source_class or "—",
        "unit": metric.unit or "—",
    } for metric in state.metrics)
    provenance = state.deployment_provenance
    if provenance is None:
        objectives_line = "? Unknown (no deployment is bound)"
        objective_rows: tuple = ()
        composed_release_binding = "—"
        composition_basis = "—"
    else:
        objective_rows = tuple(
            f"{item.objective}: {'EXECUTABLE' if item.executable else 'EXTENSION_ONLY'}"
            + (f" (a1PolicyType={item.a1_policy_type})" if item.a1_policy_type else "")
            + (f" - {item.reason}" if item.reason else "")
            for item in provenance.objectives)
        objectives_line = describe(resolve(
            provenance.objectives_status, reason=provenance.objectives_reason))
        composed_release_binding = provenance.composed_release_binding or "—"
        composition_basis = provenance.composition_basis or "—"
    return {"llm_backends": tuple(backends), "profile": state.profile_id or "—",
            "components": components, "metrics": metrics, "mode": state.mode,
            "composed_release_binding": composed_release_binding,
            "composition_basis": composition_basis,
            "objectives_line": objectives_line, "objective_rows": objective_rows}


class SettingsWorkspace:
    """Settings view that is purely a SessionState projection on refresh."""

    id = "settings"
    title = "Settings & Integration"

    def __init__(self) -> None:
        self._frame = None
        self._text = None
        self._provenance = None
        self._graph_window_var = None
        self._refresh_var = None
        self._recording_var = None
        self._preference_status = None
        self._state = SessionState()
        self._ui_state = {"graph_window_s": None, "refresh_ms": 1000, "recording": True}

    def build(self, parent) -> None:
        import tkinter as tk

        self._frame = tk.Frame(parent)
        self._frame.pack(fill="both", expand=True)
        preferences = tk.LabelFrame(self._frame, text="Display and recording preferences", padx=8, pady=8)
        preferences.pack(fill="x", padx=12, pady=(12, 6))
        self._graph_window_var = tk.StringVar(value=self._format_graph_window())
        self._refresh_var = tk.StringVar(value=str(self._ui_state["refresh_ms"]))
        self._recording_var = tk.BooleanVar(value=bool(self._ui_state["recording"]))
        tk.Label(preferences, text="Graph window (s; blank = full run)").grid(row=0, column=0, sticky="w")
        tk.Entry(preferences, textvariable=self._graph_window_var, width=10).grid(row=0, column=1, padx=(6, 16))
        tk.Label(preferences, text="Refresh (ms)").grid(row=0, column=2, sticky="w")
        tk.Spinbox(preferences, from_=100, to=60000, increment=100,
                   textvariable=self._refresh_var, width=8).grid(row=0, column=3, padx=(6, 16))
        tk.Checkbutton(preferences, text="Record session artifacts",
                       variable=self._recording_var).grid(row=0, column=4, sticky="w")
        self._preference_status = tk.Label(preferences, anchor="w")
        self._preference_status.grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))
        self._graph_window_var.trace_add("write", self._on_preference_changed)
        self._refresh_var.trace_add("write", self._on_preference_changed)
        self._recording_var.trace_add("write", self._on_preference_changed)

        panels = tk.PanedWindow(self._frame, orient="horizontal")
        panels.pack(fill="both", expand=True, padx=12, pady=(6, 12))
        integration = tk.LabelFrame(panels, text="Integration")
        provenance = tk.LabelFrame(panels, text="Read-only provenance")
        panels.add(integration, stretch="always")
        panels.add(provenance, stretch="always")
        self._text = tk.Text(integration, height=24, wrap="word", state="disabled", font=("DejaVu Sans Mono", 11))
        self._text.pack(fill="both", expand=True, padx=6, pady=6)
        self._provenance = tk.Text(provenance, height=24, wrap="word", state="disabled", font=("DejaVu Sans Mono", 11))
        self._provenance.pack(fill="both", expand=True, padx=6, pady=6)
        self.on_state(self._state)

    def _format_graph_window(self) -> str:
        value = self._ui_state["graph_window_s"]
        return "" if value is None else str(value)

    def _on_preference_changed(self, *_unused) -> None:
        if self._graph_window_var is None:
            return
        window_text = self._graph_window_var.get().strip()
        try:
            graph_window = None if not window_text else float(window_text)
            if graph_window is not None and graph_window <= 0:
                raise ValueError
            refresh_ms = int(self._refresh_var.get())
            if refresh_ms <= 0:
                raise ValueError
        except ValueError:
            if self._preference_status is not None:
                self._preference_status.configure(text="Enter a positive graph window and refresh interval")
            return
        self._ui_state.update(graph_window_s=graph_window, refresh_ms=refresh_ms,
                              recording=bool(self._recording_var.get()))
        if self._preference_status is not None:
            recording = "enabled" if self._ui_state["recording"] else "disabled"
            self._preference_status.configure(
                text=f"Refresh {refresh_ms} ms; recording preference {recording}")

    def on_state(self, state: SessionState) -> None:
        self._state = state
        if self._text is None:
            return
        data = integration_snapshot(state)
        lines = [f"Mode: {data['mode']}", f"Profile: {data['profile']}",
                 f"Graph window: {self._format_graph_window() or 'full run'}",
                 f"Refresh: {self._ui_state['refresh_ms']} ms",
                 f"Recording preference: {self._ui_state['recording']}",
                 "", "Deployment binding:",
                 f"- Composed release binding: {data['composed_release_binding']}",
                 f"- Composition basis: {data['composition_basis']}",
                 f"- Advertised objectives: {data['objectives_line']}"]
        lines.extend(f"  - {row}" for row in data["objective_rows"])
        lines.extend(["", "LLM backends:"])
        for backend in data["llm_backends"]:
            lines.append(f"- {backend['name']} / {backend['model']}: {backend['availability']}")
            for reference in backend["credentials"]:
                lines.append(f"  credential reference: {reference['reference']} (configured: {reference['configured']})")
        lines.append("\nIntegration inventory:")
        lines.extend(f"- {item['label']} [{item['kind']}]: {item['status']} | {item['source']} | {item['version']}"
                     for item in data["components"])
        lines.append("\nCapabilities and metrics:")
        lines.extend(f"- {item['metric']}: {item['status']} | {item['source']} | {item['unit']}"
                     for item in data["metrics"])
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("1.0", "\n".join(lines))
        self._text.configure(state="disabled")
        self._render_provenance(state)

    def _render_provenance(self, state: SessionState) -> None:
        if self._provenance is None:
            return
        lines = []
        for component in state.components:
            if not component.detail:
                continue
            lines.append(f"{component.label} | source: {component.source or '—'}")
            lines.extend(f"  {key}: {value}" for key, value in sorted(component.detail.items()))
        if not lines:
            lines.append("No component provenance is available for this session.")
        self._provenance.configure(state="normal")
        self._provenance.delete("1.0", "end")
        self._provenance.insert("1.0", "\n".join(lines))
        self._provenance.configure(state="disabled")

    def on_activate(self) -> None:
        if self._frame is not None:
            self._frame.focus_set()

    def on_deactivate(self) -> None:
        return None

    def gui_state(self) -> Mapping[str, Any]:
        return dict(self._ui_state)

    def restore_gui_state(self, state: Mapping[str, Any]) -> None:
        for key in self._ui_state:
            if key in state:
                self._ui_state[key] = state[key]
        if self._graph_window_var is not None:
            self._graph_window_var.set(self._format_graph_window())
            self._refresh_var.set(str(self._ui_state["refresh_ms"]))
            self._recording_var.set(bool(self._ui_state["recording"]))
