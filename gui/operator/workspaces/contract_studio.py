"""Contract Studio: the Cockpit's Kernel intent-entry workspace (Gate 3).

Design section 11's second workspace, built to the minimum the Gate 3
acceptance names: natural-language intent, the structured contract preview the
Kernel would act on, the confirmation over that preview's content hash, the
trial's separated axes while it runs, and the two Operator controls that stop
it.  The remaining workspaces, and the rest of this one's surface -- Intent
Profile editing, capability browsing, batch plans -- are Gate 6.

This renderer holds no rule.  It reads
:class:`~gui.operator.sources.kernel_live.KernelSessionView` off the state bus
and paints it; every control raises an *action* the console routes onto a
worker.  There is no widget here whose value becomes a verdict, an evidence
closure, a harm charge or a rollback result: those arrive already decided and
are drawn read-only, which is task section 9.9 expressed as a missing
capability rather than as a disabled button.

The four axes are four rows on purpose.  A single "trial status" line would be
smaller and would be a lie -- design section 8 keeps execution validity,
measurement sufficiency, predicate verdict and trial outcome separate precisely
because a stale trace and a failed predicate are different facts about a
candidate, and the operator acts differently on each.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .. import data_class as dc
from .. import status as st
from .. import tokens
from ..sources.cockpit import (
    CockpitSnapshot,
    ContractContextView,
    CorrelationStep,
)
from ..sources.kernel_live import (
    ORIGIN_AGENT,
    ORIGIN_KERNEL,
    ORIGIN_LABELS,
    ORIGIN_OPERATOR,
    AxisView,
    KernelSessionView,
    mode_badge,
)
from ..viewmodel.types import SessionState

#: The controls this workspace offers, in press order, declared as data so a
#: reachability test can assert that every one of them has a button *and* that
#: the console routes the action it raises.  The two halves are what "an
#: operator can reach it" means; either alone is satisfiable by a console that
#: does nothing.
KERNEL_ACTIONS: Tuple[Tuple[str, str], ...] = (
    ("kernel_draft", "Draft contract"),
    ("kernel_confirm", "Review & Confirm"),
    ("kernel_start", "Confirm and Start"),
    ("kernel_abort", "Abort"),
    ("kernel_estop", "Emergency Stop"),
)

#: Axis field -> the label the operator reads.  Ordered: execution first,
#: because an invalid execution makes the rest of the row uninterpretable.
AXIS_ROWS: Tuple[Tuple[str, str], ...] = (
    ("execution_validity", "Execution validity"),
    ("measurement_sufficiency", "Measurement sufficiency"),
    ("predicate_verdict", "Predicate verdict"),
    ("trial_outcome", "Trial outcome"),
)


def preview_lines(view: KernelSessionView) -> Tuple[str, ...]:
    """The structured contract preview, as text.

    Pure, and separate from the widget, so what the operator is asked to
    confirm can be asserted without a display -- and so the confirmation text
    and the confirmed content hash cannot drift apart into two renderings.
    """
    preview = view.preview
    if preview is None:
        return (f"{st.PRE_MEASUREMENT} no intent has been read yet",)
    lines: List[str] = [
        f"[{ORIGIN_LABELS[ORIGIN_AGENT]}] read as objective "
        f"{preview.objective_family}",
        f"  scope           {preview.scope_selector or st.PRE_MEASUREMENT}",
    ]
    for constraint in preview.draft_constraints:
        lines.append(f"  draft bound     {constraint}")
    for unsupported in preview.unsupported_requests:
        lines.append(f"  {st.resolve(st.UNSUPPORTED).glyph} unsupported    "
                     f"{unsupported}")
    lines += [
        "",
        "[Frozen epoch] the instance the Kernel would act on",
        f"  epoch           {preview.epoch_hash}",
        f"  candidate       {preview.candidate_id} ({preview.availability})",
        f"  target          {preview.target_ref}",
        f"  parameters      {dict(preview.parameters)}",
        f"  semantic hash   {preview.semantic_hash}",
        "",
        f"  content hash    {preview.content_hash()}",
        f"  document status {preview.document_status}",
    ]
    confirmed = view.confirmed
    if confirmed is not None and view.confirmation_valid:
        record = confirmed.confirmation
        lines += [
            f"[{ORIGIN_LABELS[ORIGIN_OPERATOR]}] {record.action.value} at "
            f"{record.timestamp}",
            f"  event           {record.event_id}",
            f"  document status {confirmed.document_status}",
        ]
    elif view.invalidated_confirmations:
        glyph = st.resolve(st.BLOCKED).glyph
        lines.append(f"{glyph} the confirmed content changed; confirm again "
                     f"({len(view.invalidated_confirmations)} invalidated)")
    else:
        lines.append(f"{st.resolve(st.UNKNOWN).glyph} not confirmed")
    return tuple(lines)


def settlement_lines(view: KernelSessionView) -> Tuple[str, ...]:
    """The Kernel's terminal facts, as text.  Never editable, never inferred."""
    head = f"[{ORIGIN_LABELS[ORIGIN_KERNEL]}]"
    settlement = view.settlement
    if settlement is None:
        polls = (f"{view.poll_count}/{view.max_polls} polls at "
                 f"{view.cadence_ms} ms") if view.max_polls else "not started"
        refusal = f" - {view.refusal}" if view.refusal else ""
        return (f"{head} {view.stage}: {polls}{refusal}",)
    lines = [
        f"{head} {settlement.trial_state}",
        f"  outcome         {settlement.outcome}",
        f"  stop reason     {settlement.stop_reason or st.PRE_MEASUREMENT}",
        f"  evidence cell   {settlement.evidence_status or st.PRE_MEASUREMENT}",
        f"  case terminal   "
        f"{settlement.case_termination or st.PRE_MEASUREMENT}",
    ]
    for charge in settlement.harm_charges:
        lines.append(f"  harm            {charge}")
    for operation, outcome in settlement.gateway_operations:
        lines.append(f"  gateway         {operation} -> {outcome}")
    if settlement.detail:
        lines.append(f"  detail          {settlement.detail}")
    return tuple(lines)


def context_lines(context: ContractContextView) -> Tuple[str, ...]:
    """Task section 9.2's six things, for the objective now in front of us.

    Intent Profile, target options, harm limits, measurement/hold, capability
    limitation and unsupported reason -- all read off the *frozen epoch*, so
    the operator is shown what the Kernel would act on rather than what a form
    was last set to.  Every one that is absent says so with its reason; none is
    quietly dropped, because a missing harm limit and a harm limit of zero call
    for opposite actions.
    """
    if context.unavailable_reason and not context.measurements:
        return (f"{st.resolve(st.UNKNOWN).glyph} "
                f"{dc.resolve(dc.UNKNOWN, reason=context.unavailable_reason).glyph}"
                f" contract context: Unknown - {context.unavailable_reason}",)
    lines: List[str] = [
        "Intent Profile",
        f"  intent          {context.intent_text or st.PRE_MEASUREMENT}",
        f"  objective       {context.objective_family or st.PRE_MEASUREMENT}",
        f"  scope           {context.scope_selector or st.PRE_MEASUREMENT}",
        "",
        "Objective / standard mapping "
        f"[{st.resolve(context.standard_mapping_status).label}]",
    ]
    if context.standard_mapping:
        lines += [f"  {label:<15} {value}"
                  for label, value in context.standard_mapping]
    else:
        lines.append(f"  {st.resolve(st.UNSUPPORTED).glyph} "
                     f"{context.standard_mapping_reason or 'not declared'}")
    if context.standard_mapping_reason:
        lines.append(f"  {st.resolve(st.BLOCKED).glyph} "
                     f"{context.standard_mapping_reason}")

    lines += ["", f"Target options  (target {context.target_ref or st.PRE_MEASUREMENT},"
                  f" hold {context.hold_ms if context.hold_ms is not None else st.PRE_MEASUREMENT} ms)"]
    if not context.options:
        lines.append(f"  {st.resolve(st.UNKNOWN).glyph} no target option is "
                     f"frozen for this objective")
    for option in context.options:
        lines.append(f"  {option.option_ref}  [{option.document_status}]  "
                     f"capability {option.capability_ref}")
        lines.append(f"      parameter space {dict(option.parameter_space)}")
        for precondition in option.preconditions:
            lines.append(f"      precondition   {precondition}")
    for predicate_id, description, mandatory in context.predicates:
        lines.append(f"  predicate {predicate_id} "
                     f"({'mandatory' if mandatory else 'optional'}): "
                     f"{description}")

    lines += ["", "Harm limits"]
    if not context.harm_limits:
        lines.append(f"  {st.resolve(st.UNKNOWN).glyph} no harm contract is "
                     f"frozen in this epoch")
    for bound in context.harm_limits:
        lines.append(f"  {bound.harm_contract_ref}  {bound.bound_id}  "
                     f"{bound.harm_kind}")
        lines.append(
            f"      admissible {bound.admissible_value} {bound.unit}"
            f"   measured {bound.measured_value}"
            f"   conservative margin {bound.conservative_margin}")
        lines.append(
            f"      enforced timeout {bound.enforced_timeout_ms} ms"
            f"   scope {dict(bound.operating_scope)}"
            f"   reserve {bound.reserve} {bound.unit}")
        lines.append(
            f"      proof {bound.proof_ref or st.PRE_MEASUREMENT}"
            f"   calibration "
            f"{', '.join(bound.calibration_records) or st.PRE_MEASUREMENT}"
            f"   missing-interval charge {bound.missing_interval_charge}")

    lines += ["", "Measurement / hold"]
    if not context.measurements:
        lines.append(f"  {st.resolve(st.UNKNOWN).glyph} no measurement "
                     f"contract is frozen in this epoch")
    for row in context.measurements:
        lines.append(f"  {row.measurement_ref}  counter {row.counter_id}  "
                     f"scope {dict(row.scope_selector)}")
        lines.append(
            f"      cadence {row.cadence_ms} ms   window {row.window_width_ms}"
            f"/{row.window_stride_ms} ms {row.overlap}   hold {row.hold_ms} ms"
            f"   freshness {row.freshness_bound_ms} ms")
        lines.append(
            f"      clock {row.clock_requirement}   aggregation "
            f"{row.aggregation}   estimator {row.estimator}   gap "
            f"{row.gap_policy}   minimum entities {row.minimum_entity_count}")

    lines += ["", "Capability limitation"]
    if not context.capabilities:
        lines.append(f"  {st.resolve(st.UNKNOWN).glyph} no capability "
                     f"manifest is frozen in this epoch")
    for capability in context.capabilities:
        lines.append(f"  {capability.capability_id}  objectives "
                     f"{', '.join(capability.supported_objectives) or st.PRE_MEASUREMENT}")
        lines.append(f"      actuators {', '.join(capability.actuator_refs) or st.PRE_MEASUREMENT}"
                     f"   interfaces {dict(capability.interface_versions)}")
        for constraint in capability.constraints:
            lines.append(f"      constraint {constraint}")

    lines += ["", "Unsupported in this reading"]
    if not context.unsupported_requests:
        lines.append("  none - every part of the intent was expressible in "
                     "this epoch's contracts")
    for request in context.unsupported_requests:
        lines.append(f"  {st.resolve(st.UNSUPPORTED).glyph} {request}")
    return tuple(lines)


def chain_lines(chain: Tuple[CorrelationStep, ...],
                correlation_id: Optional[str] = None) -> Tuple[str, ...]:
    """Task section 9.1's one correlation, from the sentence to the recovery."""
    if not chain:
        reason = "no Kernel session is attached to this console"
        return (f"{st.resolve(st.UNKNOWN).glyph} "
                f"{dc.resolve(dc.UNKNOWN, reason=reason).glyph} "
                f"correlation: Unknown - {reason}",)
    head = [f"correlation id  {correlation_id or st.PRE_MEASUREMENT}", ""]
    return tuple(head + [f"  {step.as_text()}" for step in chain])


def axis_text(axes: AxisView) -> Tuple[Tuple[str, str, str], ...]:
    """``(label, glyph, value)`` for each axis, resolved on its own table."""
    resolved = axes.resolved()
    rows = []
    for field, label in AXIS_ROWS:
        value = getattr(axes, field)
        rows.append((label, resolved[field].glyph, str(value)))
    return tuple(rows)


class ContractStudioWorkspace:
    """Natural language in, a Kernel terminal out, with the steps visible."""

    id = "contract_studio"
    title = "Contract Studio"

    def __init__(self, bus: Any = None, *,
                 on_action: Optional[Callable[[str, str], None]] = None,
                 theme: str = tokens.DEFAULT_THEME,
                 scale: float = tokens.SCALE_NORMAL) -> None:
        self.bus = bus
        self._on_action = on_action
        self.theme = theme
        self.scale = scale
        self._palette = tokens.theme(theme)
        self.frame = None
        self.intent_entry = None
        #: action key -> the button that raises it.
        self.buttons: Dict[str, Any] = {}
        self._axis_labels: Dict[str, Any] = {}
        self._mode_label = None
        self._preview_text = None
        self._settlement_text = None
        self._context_text = None
        self._chain_text = None
        self._view: Optional[KernelSessionView] = None
        self._snapshot: Optional[CockpitSnapshot] = None
        self._unsubscribers: List[Callable[[], None]] = []

    # -- build --------------------------------------------------------------

    def build(self, parent) -> None:
        import tkinter as tk

        pad = tokens.SPACING["tight"]
        self.frame = tk.Frame(parent, bg=self._palette["bg"])
        self.frame.pack(fill="both", expand=True)

        header = tk.Frame(self.frame, bg=self._palette["panel_bg"])
        header.pack(fill="x", padx=tokens.SPACING["base"], pady=pad)
        tk.Label(header, text="Kernel submission", anchor="w",
                 bg=self._palette["panel_bg"], fg=self._palette["fg"],
                 font=tokens.font("subhead", scale=self.scale, bold=True)
                 ).pack(side="left", padx=tokens.SPACING["base"])
        # The mode badge sits beside the title rather than at the bottom: a
        # screenshot cropped to the controls must still say what it was reading.
        self._mode_label = tk.Label(
            header, text="", anchor="e", bg=self._palette["panel_bg"],
            fg=self._palette["fg_muted"],
            font=tokens.font("small", scale=self.scale, bold=True))
        self._mode_label.pack(side="right", padx=tokens.SPACING["base"])

        entry_row = tk.Frame(self.frame, bg=self._palette["bg"])
        entry_row.pack(fill="x", padx=tokens.SPACING["base"], pady=pad)
        tk.Label(entry_row, text="Intent", anchor="w",
                 bg=self._palette["bg"], fg=self._palette["fg_muted"],
                 font=tokens.font("label", scale=self.scale)
                 ).pack(side="left", padx=(0, tokens.SPACING["hair"]))
        self.intent_entry = tk.Entry(
            entry_row, bg=self._palette["panel_alt_bg"], fg=self._palette["fg"],
            insertbackground=self._palette["fg"], relief="flat",
            font=tokens.font("body", scale=self.scale))
        self.intent_entry.pack(side="left", fill="x", expand=True)

        controls = tk.Frame(self.frame, bg=self._palette["bg"])
        controls.pack(fill="x", padx=tokens.SPACING["base"], pady=pad)
        for key, label in KERNEL_ACTIONS:
            self.buttons[key] = tk.Button(
                controls, text=label, relief="flat",
                bg=self._palette["panel_alt_bg"], fg=self._palette["fg"],
                font=tokens.font("small", scale=self.scale),
                command=lambda k=key: self._fire(k))
            self.buttons[key].pack(side="left", padx=1)

        body = tk.Frame(self.frame, bg=self._palette["bg"])
        body.pack(fill="both", expand=True, padx=tokens.SPACING["base"])

        tk.Label(body, text="One correlation: intent to recovery", anchor="w",
                 bg=self._palette["bg"], fg=self._palette["fg"],
                 font=tokens.font("label", scale=self.scale, bold=True)
                 ).pack(fill="x", pady=(pad, 0))
        self._chain_text = tk.Text(
            body, height=9, wrap="none", relief="flat",
            bg=self._palette["panel_alt_bg"], fg=self._palette["fg"],
            font=tokens.font("small", scale=self.scale, mono=True))
        self._chain_text.pack(fill="x")
        self._chain_text.configure(state="disabled")

        tk.Label(body, text="Structured contract preview", anchor="w",
                 bg=self._palette["bg"], fg=self._palette["fg"],
                 font=tokens.font("label", scale=self.scale, bold=True)
                 ).pack(fill="x", pady=(pad, 0))
        self._preview_text = tk.Text(
            body, height=14, wrap="none", relief="flat",
            bg=self._palette["panel_alt_bg"], fg=self._palette["fg"],
            font=tokens.font("small", scale=self.scale, mono=True))
        self._preview_text.pack(fill="both", expand=True)
        self._preview_text.configure(state="disabled")

        tk.Label(body,
                 text="Intent Profile, target options, harm limits, "
                      "measurement/hold, capability limitation",
                 anchor="w", bg=self._palette["bg"], fg=self._palette["fg"],
                 font=tokens.font("label", scale=self.scale, bold=True)
                 ).pack(fill="x", pady=(pad, 0))
        self._context_text = tk.Text(
            body, height=14, wrap="none", relief="flat",
            bg=self._palette["panel_alt_bg"], fg=self._palette["fg"],
            font=tokens.font("small", scale=self.scale, mono=True))
        self._context_text.pack(fill="both", expand=True)
        self._context_text.configure(state="disabled")

        axes = tk.Frame(self.frame, bg=self._palette["panel_bg"])
        axes.pack(fill="x", padx=tokens.SPACING["base"], pady=pad)
        tk.Label(axes,
                 text=f"Separated decision axes [{ORIGIN_LABELS[ORIGIN_KERNEL]}]",
                 anchor="w", bg=self._palette["panel_bg"],
                 fg=self._palette["fg"],
                 font=tokens.font("label", scale=self.scale, bold=True)
                 ).pack(fill="x", padx=tokens.SPACING["base"], pady=(pad, 0))
        for field, label in AXIS_ROWS:
            row = tk.Frame(axes, bg=self._palette["panel_bg"])
            row.pack(fill="x", padx=tokens.SPACING["base"])
            tk.Label(row, text=label, width=26, anchor="w",
                     bg=self._palette["panel_bg"], fg=self._palette["fg_muted"],
                     font=tokens.font("small", scale=self.scale)
                     ).pack(side="left")
            self._axis_labels[field] = tk.Label(
                row, text=st.PRE_MEASUREMENT, anchor="w",
                bg=self._palette["panel_bg"], fg=self._palette["fg"],
                font=tokens.font("small", scale=self.scale))
            self._axis_labels[field].pack(side="left")

        tk.Label(self.frame, text="Settlement", anchor="w",
                 bg=self._palette["bg"], fg=self._palette["fg"],
                 font=tokens.font("label", scale=self.scale, bold=True)
                 ).pack(fill="x", padx=tokens.SPACING["base"])
        self._settlement_text = tk.Text(
            self.frame, height=9, wrap="none", relief="flat",
            bg=self._palette["panel_alt_bg"], fg=self._palette["fg"],
            font=tokens.font("small", scale=self.scale, mono=True))
        self._settlement_text.pack(fill="x", padx=tokens.SPACING["base"],
                                   pady=(0, tokens.SPACING["base"]))
        self._settlement_text.configure(state="disabled")

        self._bind_bus()
        if self._view is not None:
            self.on_kernel_view(self._view)
        else:
            self._repaint_mode(None)
        if self._snapshot is not None:
            self.on_cockpit_snapshot(self._snapshot)
        else:
            self._write(self._chain_text, chain_lines(()))
            self._write(self._context_text,
                        context_lines(ContractContextView(
                            unavailable_reason="no Kernel session is attached "
                                               "to this console")))

    @property
    def widget(self):
        return self.frame

    def _fire(self, key: str) -> None:
        if self._on_action is None:
            return
        payload = ""
        if key == "kernel_draft" and self.intent_entry is not None:
            payload = self.intent_entry.get().strip()
        self._on_action(key, payload)

    # -- state --------------------------------------------------------------

    def _bind_bus(self) -> None:
        if self.bus is None:
            return
        try:
            self._unsubscribers.append(
                self.bus.subscribe("decision", self._on_bus))
            self._unsubscribers.append(
                self.bus.subscribe("cockpit", self._on_cockpit))
        except Exception:
            pass

    def _on_bus(self, payload: Any) -> None:
        # The decision channel also carries the legacy Coordinator's
        # DecisionView.  This workspace paints Kernel sessions and lets
        # anything else pass, rather than guessing what a foreign record meant.
        if isinstance(payload, KernelSessionView):
            self.on_kernel_view(payload)

    def _on_cockpit(self, payload: Any) -> None:
        if isinstance(payload, CockpitSnapshot):
            self.on_cockpit_snapshot(payload)

    def on_cockpit_snapshot(self, snapshot: CockpitSnapshot) -> None:
        """The Kernel's own projection: the correlation chain and the context.

        Kept apart from :meth:`on_kernel_view` because the two arrive from
        different places -- the session's own emit, and the console's
        projection of the Kernel -- and a pane that merged them would repaint
        one from the other's instant.
        """
        self._snapshot = snapshot
        if self.frame is None:
            return
        self._write(self._chain_text,
                    chain_lines(snapshot.chain, snapshot.correlation_id))
        self._write(self._context_text, context_lines(snapshot.context))

    def on_kernel_view(self, view: KernelSessionView) -> None:
        self._view = view
        if self.frame is None:
            return
        self._repaint_mode(view)
        self._write(self._preview_text, preview_lines(view))
        self._write(self._settlement_text, settlement_lines(view))
        resolved = view.axes.resolved()
        for field, _label in AXIS_ROWS:
            widget = self._axis_labels.get(field)
            if widget is None:
                continue
            entry = resolved[field]
            widget.configure(
                text=f"{entry.glyph} {getattr(view.axes, field)}",
                fg=tokens.status_color(entry.status, self.theme))

    def _repaint_mode(self, view: Optional[KernelSessionView]) -> None:
        if self._mode_label is None:
            return
        if view is None:
            self._mode_label.configure(
                text=f"{st.resolve(st.UNKNOWN).glyph} no Kernel session",
                fg=tokens.status_color(st.UNKNOWN, self.theme))
            return
        badge = mode_badge(view.mode)
        reason = f" - {badge.reason}" if badge.reason else ""
        self._mode_label.configure(text=f"{badge.glyph} {view.mode}{reason}",
                                   fg=tokens.status_color(badge.status,
                                                          self.theme))

    def _write(self, widget, lines: Tuple[str, ...]) -> None:
        if widget is None:
            return
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", "\n".join(lines))
        # Read-only again immediately: there is no edit of this text that could
        # change anything, and a text box the operator can type into implies
        # there is.
        widget.configure(state="disabled")

    # -- workspace protocol -------------------------------------------------

    def on_state(self, state: SessionState) -> None:
        """The console repaints every workspace with the session state.

        This one has nothing to take from it: a Kernel session is not a
        Coordinator session, and borrowing its mode or its decision here would
        be exactly the "one badge for two different things" the mode rules
        forbid.
        """
        return None

    def on_activate(self) -> None:
        if self.bus is None:
            return
        try:
            payload = self.bus.snapshot("decision")
            cockpit_payload = self.bus.snapshot("cockpit")
        except Exception:
            return
        if isinstance(payload, KernelSessionView):
            self.on_kernel_view(payload)
        if isinstance(cockpit_payload, CockpitSnapshot):
            self.on_cockpit_snapshot(cockpit_payload)

    def on_deactivate(self) -> None:
        return None

    def gui_state(self) -> Mapping[str, Any]:
        return {"intentText": (self.intent_entry.get()
                               if self.intent_entry is not None else "")}

    def restore_gui_state(self, state: Mapping[str, Any]) -> None:
        if self.intent_entry is None:
            return
        self.intent_entry.delete(0, "end")
        self.intent_entry.insert(0, str(state.get("intentText") or ""))

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


ContractStudio = ContractStudioWorkspace

__all__ = ["AXIS_ROWS", "KERNEL_ACTIONS", "ContractStudio",
           "ContractStudioWorkspace", "axis_text", "chain_lines",
           "context_lines", "preview_lines", "settlement_lines"]
