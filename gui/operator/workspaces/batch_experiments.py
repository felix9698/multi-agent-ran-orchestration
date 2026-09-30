"""Batch Experiments: one bounded plan, confirmed once, then repeated.

The fifth of task section 9's eight workspaces, and the pane task section 12
specifies field by field.  Its shape follows section 9.4's asymmetry with the
Interactive Run exactly:

* an interactive run confirms **one case** and starts it;
* a batch run confirms **one bounded plan** and then performs a repetition
  whose scope does not change.

So the form is editable up to the confirmation and refuses edits after the
start, the confirmation is taken over the plan's content hash, and an edit
between the two invalidates the confirmation instead of silently carrying it
onto different content.

The pane draws four blocks: the plan form, the enumerated bounded case list
(what "bounded" means, shown rather than asserted), the confirmation and its
validity, and the eight paper-grade outputs with whether each was actually
written.  Nothing here computes a result: the numbers come from the batch
runner's summary, which comes from the Kernel.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .. import data_class as dc
from .. import status as st
from .. import tokens
from ..sources.batch import PLAN_FIELDS, BatchConsoleView
from ..viewmodel.types import SessionState

#: The controls this pane offers, in press order.  ``batch_confirm`` is the
#: single ``Confirm Batch Plan`` of task section 4.1; there is deliberately no
#: control that edits a result.
BATCH_ACTIONS: Tuple[Tuple[str, str], ...] = (
    ("batch_confirm", "Confirm Batch Plan"),
    ("batch_start", "Start Batch"),
)

BLOCKS: Tuple[Tuple[str, str], ...] = (
    ("plan", "Bounded plan"),
    ("cases", "Cases in this plan"),
    ("confirmation", "Confirmation"),
    ("artifacts", "Paper-grade outputs"),
)


def plan_lines(view: BatchConsoleView) -> Tuple[str, ...]:
    """The plan as text, with its content hash and its refusal if it has one."""
    draft = view.draft.as_mapping()
    lines: List[str] = [f"stage          {view.stage}"]
    if view.scope_locked:
        lines.append("scope          LOCKED - the confirmed plan is running; "
                     "its scope cannot change")
    lines.append("")
    for key, label, help_text in PLAN_FIELDS:
        lines.append(f"  {label:<26} {draft.get(key, st.PRE_MEASUREMENT)}"
                     f"   ({help_text})")
    lines.append("")
    if view.plan_content_hash:
        lines.append(f"  content hash   {view.plan_content_hash}")
    else:
        reason = view.refusal or "the draft is not an admissible plan"
        lines.append(f"  {st.resolve(st.BLOCKED).glyph} content hash   "
                     f"{st.PRE_MEASUREMENT} - {reason}"
                     + (f": {view.refusal_detail}" if view.refusal_detail
                        else ""))
    return tuple(lines)


def case_lines(view: BatchConsoleView) -> Tuple[str, ...]:
    """The enumerated cases.  Bounded means countable, so it is counted."""
    if view.case_count is None:
        reason = view.refusal or "the draft is not an admissible plan"
        return (f"{st.resolve(st.UNKNOWN).glyph} "
                f"{dc.resolve(dc.UNKNOWN, reason=reason).glyph} cases: "
                f"Unknown - {reason}",)
    badge = dc.resolve(dc.DERIVED,
                       reason="objective x profile x repeat, ordered by seed")
    head = [f"{badge.glyph} {view.case_count} case(s), bounded by the plan's "
            f"own budget and enumerated before anything runs"]
    return tuple(head + [f"  {line}" for line in view.cases])


def confirmation_lines(view: BatchConsoleView) -> Tuple[str, ...]:
    """One confirmation over the whole plan, and whether it still covers it."""
    record = view.confirmation
    if record is None:
        return (f"{st.resolve(st.UNKNOWN).glyph} no confirmation has been "
                f"taken; Start Batch is refused without one",)
    if view.confirmation_valid:
        status, note = st.OK, ""
    else:
        status, note = st.BLOCKED, ("  - the plan content changed; confirm the "
                                    "plan again")
    return (
        f"{st.resolve(status).glyph} {record.action.value}{note}",
        f"    content hash {record.confirmed_content_hash}",
        f"    event {record.event_id}   at {record.timestamp}",
        f"    changed after confirmation "
        f"{'yes' if record.changed_after_confirmation else 'no'}",
    )


def artifact_lines(view: BatchConsoleView) -> Tuple[str, ...]:
    """The eight outputs, each with what was written and what was not."""
    lines: List[str] = []
    if view.run_dir:
        lines.append(f"run directory  {view.run_dir}")
        counts = (view.summary or {}).get("trialCounts") or {}
        if counts:
            lines.append(f"trials         valid {counts.get('valid', '?')}  "
                         f"invalid {counts.get('invalid', '?')}  "
                         f"error {counts.get('error', '?')}  "
                         f"total {counts.get('total', '?')}")
        lines.append("")
    for row in view.artifacts:
        reason = f" - {row.reason}" if row.reason else ""
        lines.append(f"{st.resolve(row.status).glyph} {row.label}{reason}")
        for path in row.present:
            lines.append(f"      {path}")
        for path in row.missing:
            lines.append(f"      {st.resolve(st.UNSUPPORTED).glyph} {path} "
                         f"(not written)")
    return tuple(lines)


BLOCK_LINES: Mapping[str, Callable[[BatchConsoleView], Tuple[str, ...]]] = {
    "plan": plan_lines,
    "cases": case_lines,
    "confirmation": confirmation_lines,
    "artifacts": artifact_lines,
}


def block_text(view: BatchConsoleView) -> Mapping[str, Tuple[str, ...]]:
    return {key: BLOCK_LINES[key](view) for key, _label in BLOCKS}


def control_enabled(view: BatchConsoleView) -> Mapping[str, Tuple[bool, str]]:
    """``action -> (enabled, reason)``.  A disabled control keeps its reason.

    Pure, so the enable rules are assertable without a display and so a control
    can never be live in a state the session would refuse.
    """
    if view.scope_locked:
        locked = "the confirmed plan is running; its scope cannot change"
        return {"batch_confirm": (False, locked),
                "batch_start": (False, locked)}
    if not view.plan_content_hash:
        reason = view.refusal or "the draft is not an admissible plan"
        return {"batch_confirm": (False, reason),
                "batch_start": (False, reason)}
    if not view.confirmation_valid:
        return {"batch_confirm": (True, ""),
                "batch_start": (False, "confirm the bounded plan first")}
    return {"batch_confirm": (True, ""), "batch_start": (True, "")}


class BatchExperimentsWorkspace:
    """The plan form, the bounded case list, the confirmation, the outputs."""

    id = "batch_experiments"
    title = "Batch Experiments"

    def __init__(self, bus: Any = None, *,
                 on_action: Optional[Callable[[str, str], None]] = None,
                 on_edit: Optional[Callable[[str, str], None]] = None,
                 theme: str = tokens.DEFAULT_THEME,
                 scale: float = tokens.SCALE_NORMAL) -> None:
        self.bus = bus
        self._on_action = on_action
        self._on_edit = on_edit
        self.theme = theme
        self.scale = scale
        self._palette = tokens.theme(theme)
        self.frame = None
        self.entries: Dict[str, Any] = {}
        self.buttons: Dict[str, Any] = {}
        self._texts: dict = {}
        self._view: BatchConsoleView = BatchConsoleView()
        self._unsubscribers: List[Callable[[], None]] = []

    # -- build --------------------------------------------------------------

    def build(self, parent) -> None:
        import tkinter as tk

        pad = tokens.SPACING["tight"]
        self.frame = tk.Frame(parent, bg=self._palette["bg"])
        self.frame.pack(fill="both", expand=True)

        form = tk.Frame(self.frame, bg=self._palette["bg"])
        form.pack(fill="x", padx=tokens.SPACING["base"], pady=pad)
        for index, (key, label, _help) in enumerate(PLAN_FIELDS):
            row, column = divmod(index, 3)
            cell = tk.Frame(form, bg=self._palette["bg"])
            cell.grid(row=row, column=column, sticky="ew", padx=pad)
            form.grid_columnconfigure(column, weight=1, uniform="batch")
            tk.Label(cell, text=label, anchor="w", bg=self._palette["bg"],
                     fg=self._palette["fg_muted"],
                     font=tokens.font("micro", scale=self.scale)
                     ).pack(fill="x")
            entry = tk.Entry(cell, bg=self._palette["panel_alt_bg"],
                             fg=self._palette["fg"],
                             insertbackground=self._palette["fg"],
                             relief="flat",
                             font=tokens.font("small", scale=self.scale))
            entry.pack(fill="x")
            entry.bind("<FocusOut>", lambda _e, k=key: self._edit(k))
            entry.bind("<Return>", lambda _e, k=key: self._edit(k))
            self.entries[key] = entry

        controls = tk.Frame(self.frame, bg=self._palette["bg"])
        controls.pack(fill="x", padx=tokens.SPACING["base"], pady=pad)
        for key, label in BATCH_ACTIONS:
            self.buttons[key] = tk.Button(
                controls, text=label, relief="flat",
                bg=self._palette["panel_alt_bg"], fg=self._palette["fg"],
                font=tokens.font("small", scale=self.scale),
                command=lambda k=key: self._fire(k))
            self.buttons[key].pack(side="left", padx=1)

        for key, label in BLOCKS:
            if key == "plan":
                continue
            tk.Label(self.frame, text=label, anchor="w",
                     bg=self._palette["bg"], fg=self._palette["fg"],
                     font=tokens.font("label", scale=self.scale, bold=True)
                     ).pack(fill="x", padx=tokens.SPACING["base"],
                            pady=(pad, 0))
            widget = tk.Text(
                self.frame, height=7, wrap="none", relief="flat",
                bg=self._palette["panel_alt_bg"], fg=self._palette["fg"],
                font=tokens.font("small", scale=self.scale, mono=True))
            widget.pack(fill="both", expand=(key == "artifacts"),
                        padx=tokens.SPACING["base"])
            widget.configure(state="disabled")
            self._texts[key] = widget

        self._bind_bus()
        self.on_batch_view(self._view)

    @property
    def widget(self):
        return self.frame

    def _fire(self, key: str) -> None:
        if self._on_action is not None:
            self._on_action(key, "")

    def _edit(self, key: str) -> None:
        entry = self.entries.get(key)
        if entry is None or self._on_edit is None:
            return
        self._on_edit(key, entry.get().strip())

    # -- state --------------------------------------------------------------

    def _bind_bus(self) -> None:
        if self.bus is None:
            return
        try:
            self._unsubscribers.append(self.bus.subscribe("batch",
                                                          self._on_bus))
        except Exception:
            pass

    def _on_bus(self, payload: Any) -> None:
        if isinstance(payload, BatchConsoleView):
            self.on_batch_view(payload)

    def on_batch_view(self, view: BatchConsoleView) -> None:
        self._view = view
        if self.frame is None:
            return
        draft = view.draft.as_mapping()
        for key, entry in self.entries.items():
            value = str(draft.get(key, ""))
            if entry.get() != value:
                entry.delete(0, "end")
                entry.insert(0, value)
            # Section 9.4's scope lock, expressed on the widget the operator
            # would otherwise type into.
            entry.configure(state="disabled" if view.scope_locked else "normal")
        for key, (enabled, _reason) in control_enabled(view).items():
            button = self.buttons.get(key)
            if button is not None:
                button.configure(state="normal" if enabled else "disabled")
        for key, lines in block_text(view).items():
            widget = self._texts.get(key)
            if widget is None:
                continue
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", "\n".join(lines))
            widget.configure(state="disabled")

    def lines(self) -> Mapping[str, Tuple[str, ...]]:
        return block_text(self._view)

    # -- workspace protocol -------------------------------------------------

    def on_state(self, state: SessionState) -> None:
        return None

    def on_activate(self) -> None:
        if self.bus is None:
            return
        try:
            payload = self.bus.snapshot("batch")
        except Exception:
            return
        if isinstance(payload, BatchConsoleView):
            self.on_batch_view(payload)

    def on_deactivate(self) -> None:
        return None

    def gui_state(self) -> Mapping[str, Any]:
        return dict(self._view.draft.as_mapping())

    def restore_gui_state(self, state: Mapping[str, Any]) -> None:
        if self._on_edit is None:
            return
        for key, _label, _help in PLAN_FIELDS:
            if key in state:
                self._on_edit(key, str(state[key]))

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


__all__ = ["BATCH_ACTIONS", "BLOCKS", "BLOCK_LINES",
           "BatchExperimentsWorkspace", "artifact_lines", "block_text",
           "case_lines", "confirmation_lines", "control_enabled",
           "plan_lines"]
