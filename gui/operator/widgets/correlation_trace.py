"""The correlation-id trace panel.

Final integration section 4.7 requires that, after a natural-language Intent
is submitted, an operator can follow one correlation id across eight items:
the original Intent and its revision, the three-stage LLM judgement and its
final verdict, the generated rApp/R1 policy identity, the A1-P lifecycle,
the selected UE and target cell, whether an E2 control write was attempted
and how many, the KPM readback and O1 assurance, and the coordinator's FSM
terminal state with its commit/rollback result.

This panel only renders :class:`~gui.operator.viewmodel.types.CorrelationTraceView`.
It computes nothing: every value, status and reason already comes from the
projection in ``gui.operator.sources.live.project_correlation_trace``, so an
absent item shows ``UNKNOWN``/``UNSUPPORTED`` with its stated reason rather
than a blank the operator might mistake for zero observations.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from gui.operator.status import PRE_MEASUREMENT, describe, resolve
from gui.operator.viewmodel.types import CorrelationTraceView


def _describe(status: Optional[str], detail: Optional[str], reason: Optional[str]) -> str:
    text = describe(resolve(status or "UNKNOWN", reason=reason))
    return f"{text}: {detail}" if detail else text


class CorrelationTracePanel:
    """The eight-item trace for the correlation id of the last episode."""

    #: (row key, label) - one row per item in section 4.7's order, plus the
    #: correlation id itself as the header row every other row is keyed to.
    _ROWS = (
        ("correlation_id", "Correlation id"),
        ("intent", "1. Intent + revision"),
        ("verdict", "2. Three-stage judgement + verdict"),
        ("policy_identity", "3. rApp/R1 policy identity"),
        ("policy_lifecycle", "4. A1-P lifecycle/status"),
        ("target", "5. UE + target cell"),
        ("e2_control", "6. E2 control attempt + write count"),
        ("assurance", "7. KPM readback + O1 assurance"),
        ("fsm", "8. FSM terminal state + commit/rollback"),
    )

    def __init__(self, parent) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.frame = ttk.LabelFrame(parent, text="Correlation trace")
        self._vars: Dict[str, Any] = {}
        for row, (key, label) in enumerate(self._ROWS):
            ttk.Label(self.frame, text=label).grid(
                row=row, column=0, sticky="nw", padx=4, pady=2)
            var = tk.StringVar(value=PRE_MEASUREMENT)
            ttk.Label(self.frame, textvariable=var, wraplength=520,
                      justify="left").grid(row=row, column=1, sticky="nw",
                                           padx=4, pady=2)
            self._vars[key] = var
        self.frame.columnconfigure(1, weight=1)

    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    def grid(self, **kwargs) -> None:
        self.frame.grid(**kwargs)

    def destroy(self) -> None:
        self.frame.destroy()

    def set_trace(self, trace: Optional[CorrelationTraceView]) -> None:
        if trace is None:
            for var in self._vars.values():
                var.set(PRE_MEASUREMENT)
            return
        self._vars["correlation_id"].set(_describe(
            trace.correlation_id_status, trace.correlation_id,
            trace.correlation_id_reason))
        self._vars["intent"].set(_describe(
            trace.intent_status,
            f"{trace.intent_id or PRE_MEASUREMENT} rev "
            f"{trace.intent_revision or PRE_MEASUREMENT}",
            trace.intent_reason))
        stages = ", ".join(f"{stage.stage_id}={stage.state}"
                           for stage in trace.llm_stages) or PRE_MEASUREMENT
        self._vars["verdict"].set(_describe(
            trace.verdict_status, f"{stages} -> {trace.verdict_detail or PRE_MEASUREMENT}",
            trace.verdict_reason))
        self._vars["policy_identity"].set(_describe(
            trace.policy_identity_status,
            f"{trace.policy_id or PRE_MEASUREMENT} "
            f"({trace.policy_type_id or PRE_MEASUREMENT} rev "
            f"{trace.policy_revision or PRE_MEASUREMENT})",
            trace.policy_identity_reason))
        self._vars["policy_lifecycle"].set(_describe(
            trace.policy_lifecycle_status,
            f"policyState={trace.policy_state or PRE_MEASUREMENT} "
            f"enforceStatus={trace.enforce_status or PRE_MEASUREMENT} "
            f"episodeState={trace.episode_state or PRE_MEASUREMENT}",
            trace.policy_lifecycle_reason))
        cells = ", ".join(trace.target_cells) or PRE_MEASUREMENT
        self._vars["target"].set(_describe(
            trace.target_status,
            f"UE {trace.ue_id or PRE_MEASUREMENT} · cell(s) {cells} "
            f"[{trace.target_source or PRE_MEASUREMENT}]",
            trace.target_reason))
        attempted = (PRE_MEASUREMENT if trace.e2_control_attempted is None
                    else str(trace.e2_control_attempted))
        count = (PRE_MEASUREMENT if trace.e2_write_count is None
                else str(trace.e2_write_count))
        self._vars["e2_control"].set(_describe(
            trace.e2_status,
            f"attempted={attempted} result={trace.e2_control_result or PRE_MEASUREMENT} "
            f"writes={count}",
            trace.e2_reason))
        self._vars["assurance"].set(_describe(
            trace.assurance_status,
            f"readback={trace.readback_result or PRE_MEASUREMENT} "
            f"assurance={trace.assurance_decision or PRE_MEASUREMENT} "
            f"quality={trace.evidence_quality or PRE_MEASUREMENT}",
            trace.assurance_reason))
        rolled_back = (PRE_MEASUREMENT if trace.rolled_back is None
                      else str(trace.rolled_back))
        self._vars["fsm"].set(_describe(
            trace.fsm_status,
            f"{trace.eq12_state or PRE_MEASUREMENT} "
            f"({trace.terminal_outcome or PRE_MEASUREMENT}) "
            f"rolledBack={rolled_back}",
            trace.fsm_reason))


__all__ = ["CorrelationTracePanel"]
