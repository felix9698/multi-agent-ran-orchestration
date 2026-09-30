"""Trial & Safety: what the Kernel is doing to the deployment, and its guards.

The third of task section 9's eight workspaces.  Contract Studio answers "what
was submitted"; this pane answers the two questions that follow while a trial
runs -- *what is being tried right now*, and *what would stop it*.  Four blocks,
in the order an operator needs them:

``Candidate / trial``
    The epoch-frozen catalog with the Kernel's availability on each entry, and
    the case's trials with their lifecycle state.  Availability is shown for
    every candidate including the ones that cannot be tried, with the reason:
    a catalog that hid its unavailable entries would make an exhausted epoch
    look like a small one.

``Harm reserve / charge``
    Reserve, charge, return and remaining per harm contract, plus how much of
    the charge was a conservative substitute for a measurement gap rather than
    an observation.  Every number is arithmetic over the append-only harm
    ledger and is badged ``DERIVED`` for that reason.

``Watchdog``
    Which watchdogs the Kernel armed before it allowed the apply, and which
    harm contracts they were armed against (task section 6.3).  ``not armed``
    on a trial that has applied is a fault, and it reads as one.

``Rollback / recovery``
    Whether the post-commit path ran: stop, reverse rollback, recovery reread
    and recovery verification (task section 6.10).

Nothing on this screen is editable and nothing on it is a control.  Task
section 9.9 forbids a GUI path that changes a verdict, an evidence closure, a
harm charge or a rollback result, and the honest expression of that is a pane
with no widget that accepts input -- not a disabled button.  Emergency Stop is
the console's one control over a running trial and it lives in the header,
where it is reachable from every workspace.
"""

from __future__ import annotations

from typing import Any, Callable, List, Mapping, Tuple

from .. import data_class as dc
from .. import status as st
from .. import tokens
from ..sources.cockpit import CockpitSnapshot, TrialSafetyView
from ..viewmodel.types import SessionState

#: The four blocks, in reading order.  Declared as data so a test can assert
#: the pane renders all four rather than three.
BLOCKS: Tuple[Tuple[str, str], ...] = (
    ("candidates", "Candidate / trial"),
    ("harm", "Harm reserve / charge"),
    ("watchdog", "Watchdog"),
    ("rollback", "Rollback / recovery"),
)


def _unavailable(view: TrialSafetyView, what: str) -> Tuple[str, ...]:
    reason = view.unavailable_reason or "no Kernel session is attached"
    return (f"{st.resolve(st.UNKNOWN).glyph} "
            f"{dc.resolve(dc.UNKNOWN, reason=reason).glyph} {what}: "
            f"Unknown - {reason}",)


def candidate_lines(view: TrialSafetyView) -> Tuple[str, ...]:
    """The frozen catalog and the case's trials, as text.

    Pure and separate from the widget, which is what lets the acceptance test
    read exactly what an operator would read.
    """
    if view.unavailable_reason and not view.candidates and not view.trials:
        return _unavailable(view, "candidate / trial")
    lines: List[str] = [
        f"epoch          {view.epoch_hash or st.PRE_MEASUREMENT}",
        f"target vector  {view.active_vector or st.PRE_MEASUREMENT}"
        + ("" if view.active_vector
           else "  (no vector released for this case)"),
        f"trials         {view.trials_used}"
        + (f"/{view.max_trials}" if view.max_trials else "")
        + (f"   deadline {view.deadline_at}" if view.deadline_at else ""),
        "",
        "frozen candidate catalog [KERNEL_DECISION: availability]",
    ]
    if not view.candidates:
        lines.append(f"  {st.resolve(st.UNKNOWN).glyph} no catalog is frozen "
                     f"in this epoch")
    for row in view.candidates:
        reason = f"  - {row.reason}" if row.reason else ""
        lines.append(f"  {st.resolve(row.status).glyph} {row.candidate_id}  "
                     f"{row.availability}  {row.target_ref}{reason}")
    lines += ["", "trials [KERNEL_DECISION]"]
    if not view.trials:
        lines.append(f"  {st.resolve(st.UNKNOWN).glyph} no trial has been "
                     f"opened for this case")
    for trial in view.trials:
        stop = f"  stop {trial.stop_reason}" if trial.stop_reason else ""
        lines.append(f"  {st.resolve(trial.status).glyph} {trial.trial_id}  "
                     f"{trial.state}  outcome {trial.outcome}{stop}")
        lines.append(f"      candidate {trial.candidate_id or st.PRE_MEASUREMENT}"
                     f"   apply counted "
                     f"{'yes' if trial.apply_counted else 'no'}"
                     f"   harm clock "
                     f"{trial.harm_clock_started_at or st.PRE_MEASUREMENT}")
    if view.case_terminal:
        lines += ["", f"case terminal  {view.case_terminal}"]
    if view.case_paused:
        lines.append("case paused    new trials are not being scheduled; "
                     "an active hardware transaction is never paused")
    return tuple(lines)


def harm_lines(view: TrialSafetyView) -> Tuple[str, ...]:
    """Reserve arithmetic, marked as arithmetic, never as an observation."""
    if not view.harm:
        reason = (view.unavailable_reason
                  or "no harm contract is frozen in this epoch")
        return (f"{st.resolve(st.UNKNOWN).glyph} "
                f"{dc.resolve(dc.UNKNOWN, reason=reason).glyph} harm: "
                f"Unknown - {reason}",)
    badge = dc.resolve(dc.DERIVED, reason="arithmetic over the harm ledger")
    lines: List[str] = [f"{badge.as_text()}"]
    for item in view.harm:
        if item.usable is None:
            lines.append(f"  {st.resolve(item.status).glyph} "
                         f"{item.harm_contract_ref}: {st.PRE_MEASUREMENT} "
                         f"- {item.reason}")
            continue
        remaining = item.remaining
        lines.append(
            f"  {st.resolve(item.status).glyph} {item.harm_contract_ref}  "
            f"reserve {item.usable:g} {item.unit}   reserved "
            f"{item.reserved:g}   charged {item.charged:g}   returned "
            f"{item.returned:g}   remaining "
            f"{st.PRE_MEASUREMENT if remaining is None else format(remaining, 'g')}")
        lines.append(
            f"      limit respected "
            f"{'yes' if item.limit_respected else 'NO'}"
            f"   charged for missing interval "
            f"{item.charged_for_missing_interval:g} (conservative substitute, "
            f"not an observation)")
        if item.reason:
            lines.append(f"      {item.reason}")
    return tuple(lines)


def watchdog_lines(view: TrialSafetyView) -> Tuple[str, ...]:
    """Which guards were armed before the Kernel allowed an apply."""
    if not view.trials:
        return _unavailable(view, "watchdog")
    lines: List[str] = []
    for trial in view.trials:
        armed = "armed" if trial.guards_armed else "not armed"
        status = (st.OK if trial.guards_armed
                  else st.ERROR if trial.apply_counted else st.UNKNOWN)
        note = ("" if trial.guards_armed
                else "  - this trial applied without armed guards"
                if trial.apply_counted
                else "  - nothing has been applied yet")
        lines.append(f"{st.resolve(status).glyph} {trial.trial_id}: {armed}"
                     f"{note}")
        lines.append(f"    watchdogs      "
                     f"{', '.join(trial.watchdog_ids) or st.PRE_MEASUREMENT}")
        lines.append(f"    harm contracts "
                     f"{', '.join(trial.armed_harm_contract_refs) or st.PRE_MEASUREMENT}")
    if view.resource_locks:
        lines += ["", "resource locks"]
        lines += [f"    {resource} -> {owner}"
                  for resource, owner in view.resource_locks]
    return tuple(lines)


def rollback_lines(view: TrialSafetyView) -> Tuple[str, ...]:
    """The post-commit path, per trial, exactly as the Kernel recorded it."""
    if not view.trials:
        return _unavailable(view, "rollback / recovery")
    lines: List[str] = []
    if view.emergency_stop_requested:
        lines.append(f"{st.resolve(st.BLOCKED).glyph} Emergency Stop raised: "
                     f"Kernel OPERATOR_ABORT -> Write Gateway stop -> rollback "
                     f"-> recovery")
    for trial in view.trials:
        rolled = "yes" if trial.rolled_back else "no"
        lines.append(f"{st.resolve(trial.status).glyph} {trial.trial_id}  "
                     f"state {trial.state}  rolled back {rolled}")
        lines.append(f"    commit acked     "
                     f"{'yes' if trial.commit_acknowledged else 'no'}"
                     f"   finalize acked "
                     f"{'yes' if trial.finalize_acknowledged else 'no'}")
        lines.append(f"    config reread    "
                     f"{'yes' if trial.configuration_reread else 'no'}"
                     f"   recovery reread "
                     f"{'yes' if trial.recovery_reread else 'no'}"
                     f"   recovery verified "
                     f"{'yes' if trial.recovery_verified else 'no'}")
        lines.append(f"    settlement       "
                     f"{trial.settlement_event_id or st.PRE_MEASUREMENT}")
    return tuple(lines)


#: block id -> the pure projection that fills it.
BLOCK_LINES: Mapping[str, Callable[[TrialSafetyView], Tuple[str, ...]]] = {
    "candidates": candidate_lines,
    "harm": harm_lines,
    "watchdog": watchdog_lines,
    "rollback": rollback_lines,
}


def block_text(view: TrialSafetyView) -> Mapping[str, Tuple[str, ...]]:
    """Every block's text for one view.  The whole pane, without a display."""
    return {key: BLOCK_LINES[key](view) for key, _label in BLOCKS}


class TrialSafetyWorkspace:
    """Read-only.  Holds four text widgets and not one input."""

    id = "trial_safety"
    title = "Trial & Safety"

    def __init__(self, bus: Any = None, *,
                 theme: str = tokens.DEFAULT_THEME,
                 scale: float = tokens.SCALE_NORMAL) -> None:
        self.bus = bus
        self.theme = theme
        self.scale = scale
        self._palette = tokens.theme(theme)
        self.frame = None
        self._texts: dict = {}
        self._legend = None
        self._view: TrialSafetyView = TrialSafetyView()
        self._unsubscribers: List[Callable[[], None]] = []

    # -- build --------------------------------------------------------------

    def build(self, parent) -> None:
        import tkinter as tk

        pad = tokens.SPACING["tight"]
        self.frame = tk.Frame(parent, bg=self._palette["bg"])
        self.frame.pack(fill="both", expand=True)

        self._legend = tk.Label(
            self.frame, text="  ".join(dc.legend()), anchor="w", justify="left",
            bg=self._palette["panel_bg"], fg=self._palette["fg_muted"],
            font=tokens.font("micro", scale=self.scale))
        self._legend.pack(fill="x", padx=tokens.SPACING["base"], pady=(pad, 0))

        for key, label in BLOCKS:
            tk.Label(self.frame, text=label, anchor="w",
                     bg=self._palette["bg"], fg=self._palette["fg"],
                     font=tokens.font("label", scale=self.scale, bold=True)
                     ).pack(fill="x", padx=tokens.SPACING["base"],
                            pady=(pad, 0))
            widget = tk.Text(
                self.frame, height=8 if key == "candidates" else 6,
                wrap="none", relief="flat",
                bg=self._palette["panel_alt_bg"], fg=self._palette["fg"],
                font=tokens.font("small", scale=self.scale, mono=True))
            widget.pack(fill="both", expand=(key == "candidates"),
                        padx=tokens.SPACING["base"])
            widget.configure(state="disabled")
            self._texts[key] = widget

        self._bind_bus()
        self.on_trial_safety(self._view)

    @property
    def widget(self):
        return self.frame

    # -- state --------------------------------------------------------------

    def _bind_bus(self) -> None:
        if self.bus is None:
            return
        try:
            self._unsubscribers.append(self.bus.subscribe("cockpit",
                                                          self._on_bus))
        except Exception:
            pass

    def _on_bus(self, payload: Any) -> None:
        if isinstance(payload, CockpitSnapshot):
            self.on_trial_safety(payload.trial_safety)

    def on_trial_safety(self, view: TrialSafetyView) -> None:
        self._view = view
        if self.frame is None:
            return
        for key, lines in block_text(view).items():
            widget = self._texts.get(key)
            if widget is None:
                continue
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", "\n".join(lines))
            # Disabled again immediately.  There is no edit of this text that
            # could change a harm charge or a rollback result, and a box the
            # operator can type into implies there is.
            widget.configure(state="disabled")

    def lines(self) -> Mapping[str, Tuple[str, ...]]:
        """What the pane currently reads, for a test with no display."""
        return block_text(self._view)

    # -- workspace protocol -------------------------------------------------

    def on_state(self, state: SessionState) -> None:
        """The Kernel's trial state is not the Coordinator's session state.

        Taking a mode or an outcome from ``SessionState`` here would be the
        "one badge for two different things" the mode rules forbid, so this
        pane reads the cockpit channel and nothing else.
        """
        return None

    def on_activate(self) -> None:
        if self.bus is None:
            return
        try:
            payload = self.bus.snapshot("cockpit")
        except Exception:
            return
        if isinstance(payload, CockpitSnapshot):
            self.on_trial_safety(payload.trial_safety)

    def on_deactivate(self) -> None:
        return None

    def gui_state(self) -> Mapping[str, Any]:
        return {}

    def restore_gui_state(self, state: Mapping[str, Any]) -> None:
        return None

    def destroy(self) -> None:
        for unsubscribe in self._unsubscribers:
            try:
                unsubscribe()
            except Exception:
                pass
        self._unsubscribers.clear()
        if self.frame is not None:
            self.frame.destroy()
            self.frame = None


__all__ = ["BLOCKS", "BLOCK_LINES", "TrialSafetyWorkspace", "block_text",
           "candidate_lines", "harm_lines", "rollback_lines",
           "watchdog_lines"]
