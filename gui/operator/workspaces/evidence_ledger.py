"""Evidence Ledger: the append-only stream, read straight through.

The fourth of task section 9's eight workspaces, and the one that exists so an
operator can check the system's claims rather than take them.  Four blocks:

``Append-only stream``
    Every Kernel event with its store position, kind, object, instant and
    content hash.  The position is shown because *append-only* is only
    checkable against an ordinal the operator can see; a stream rendered
    without it is a list, not a ledger.

``Evidence closure``
    One row per evidence obligation, with the cell's status, whether it is
    sealed until its target vector is released, how many contributions it
    holds, how many of those were reused across epochs, and how many are
    post-closure witnesses.  Closure progress is closed cells over all cells --
    and it is ``Unknown`` when there are no cells at all, because a case with
    nothing to close has not closed everything (task section 9.13).

``Confirmation``
    Each confirmation record's content hash, action, instant, event id and
    whether the confirmed content changed afterwards.  Nothing else: task
    section 4.3 removed the name, the role and the signature, and there is no
    field on the row that could carry one.

``Harm ledger``
    The movements themselves, one line each, so the derived balance on the
    header and in Trial & Safety can be checked against its inputs.

Like ``Trial & Safety`` this pane has no control.  Section 9.9's "the GUI
cannot modify a verdict, an evidence closure, a harm charge or a rollback
result" is expressed here as the absence of any widget that takes input.
"""

from __future__ import annotations

from typing import Any, Callable, List, Mapping, Tuple

from .. import data_class as dc
from .. import status as st
from .. import tokens
from ..sources.cockpit import CockpitSnapshot, EvidenceLedgerView
from ..viewmodel.types import SessionState

BLOCKS: Tuple[Tuple[str, str], ...] = (
    ("stream", "Append-only event stream"),
    ("closure", "Evidence closure"),
    ("confirmation", "Confirmation"),
    ("harm", "Harm ledger"),
)

#: Rows of the stream the pane draws at once.  A window, never a filter: the
#: header line always states the full count so a truncated view cannot read as
#: a short ledger.
STREAM_WINDOW: int = 200


def _unknown(reason: str, what: str) -> str:
    return (f"{st.resolve(st.UNKNOWN).glyph} "
            f"{dc.resolve(dc.UNKNOWN, reason=reason).glyph} {what}: "
            f"Unknown - {reason}")


def stream_lines(view: EvidenceLedgerView) -> Tuple[str, ...]:
    """The append-only stream, newest last, with its true length stated."""
    if not view.events:
        return (_unknown(view.unavailable_reason
                         or "the Kernel exposed no readable event stream",
                         "append-only stream"),)
    shown = view.events[-STREAM_WINDOW:]
    head = [
        f"{view.event_count} event(s) in the store; showing the last "
        f"{len(shown)}",
        f"reducer        {view.reducer_version or st.PRE_MEASUREMENT}",
        f"epoch          {view.epoch_hash or st.PRE_MEASUREMENT}",
        f"terminal hash  {view.terminal_state_hash or st.PRE_MEASUREMENT}",
        "",
        "  pos  timestamp                 kind  object  [event id]",
    ]
    return tuple(head + [f"  {entry.as_text()}" for entry in shown])


def closure_lines(view: EvidenceLedgerView) -> Tuple[str, ...]:
    """One row per obligation, with closure and witnesses kept apart."""
    if not view.cells:
        return (_unknown(view.unavailable_reason
                         or "no evidence cell is registered for this case",
                         "evidence closure"),)
    progress = view.closure_progress
    badge = dc.resolve(dc.DERIVED,
                       reason="closed cells over registered cells")
    lines: List[str] = [
        f"{badge.glyph} closure progress  "
        f"{st.PRE_MEASUREMENT if progress is None else f'{progress:.0%}'}"
        f"   ({sum(1 for c in view.cells if c.is_closed)}/{len(view.cells)} "
        f"cells closed)",
        f"open obligations  {len(view.open_obligations)}"
        + ("   (EVIDENCE_INCOMPLETE, not exhaustion)"
           if view.open_obligations else ""),
        "",
    ]
    for cell in view.cells:
        seal = (f"   sealed until {cell.sealed_until_vector_ref}"
                if cell.sealed else "")
        lines.append(f"{st.resolve(cell.gui_status).glyph} {cell.cell_id}  "
                     f"{cell.status}  {cell.target_ref}{seal}")
        lines.append(
            f"    contributions {cell.contributions}"
            f"   reused {cell.reused_contributions}"
            f"   pending {cell.pending_contributions}"
            f"   post-closure witnesses {cell.post_closure_witnesses}")
        if cell.pending_status and cell.pending_status != cell.status:
            lines.append(f"    pending status {cell.pending_status} - not yet "
                         f"settled into the cell")
        if cell.reason:
            lines.append(f"    {cell.reason}")
    for key, state in view.exhaustion_certificates:
        lines.append(f"exhaustion certificate {key}: {state}")
    return tuple(lines)


def confirmation_lines(view: EvidenceLedgerView) -> Tuple[str, ...]:
    """Content hash, action, instant, event id, and whether it still holds."""
    if not view.confirmations:
        return (_unknown("no confirmation has been recorded in this session",
                         "confirmation"),)
    lines: List[str] = []
    for record in view.confirmations:
        status = st.BLOCKED if record.invalidated else st.OK
        note = ("  - the confirmed content changed; a new confirmation is "
                "required" if record.invalidated else "")
        lines.append(f"{st.resolve(status).glyph} {record.action}  "
                     f"{record.confirmed_object_type}{note}")
        lines.append(f"    content hash {record.content_hash}")
        lines.append(f"    event {record.event_id}   at {record.timestamp}")
    return tuple(lines)


def harm_lines(view: EvidenceLedgerView) -> Tuple[str, ...]:
    """The movements the derived balance is computed from."""
    if not view.harm_movements:
        return (_unknown(view.unavailable_reason
                         or "no harm movement has been recorded for this case",
                         "harm ledger"),)
    lines: List[str] = []
    for entry in view.harm_movements:
        amount = entry.get("amount") or {}
        conservative = ("  [conservative substitute for a measurement gap]"
                        if entry.get("chargedForMissingInterval") else "")
        lines.append(
            f"  {entry.get('timestamp', st.PRE_MEASUREMENT)}  "
            f"{entry.get('movementKind', '?')}  "
            f"{amount.get('value', st.PRE_MEASUREMENT)} "
            f"{amount.get('unit', '')}  {entry.get('harmContractRef', '?')}  "
            f"{entry.get('harmKind', '?')}  trial "
            f"{entry.get('trialId', st.PRE_MEASUREMENT)}"
            f"  reason {entry.get('reason', '?')}{conservative}")
    badge = dc.resolve(dc.DERIVED, reason="arithmetic over the movements above")
    for item in view.harm:
        remaining = item.remaining
        lines.append(
            f"{badge.glyph} {item.harm_contract_ref}: charged {item.charged:g}"
            f" of {st.PRE_MEASUREMENT if item.usable is None else format(item.usable, 'g')}"
            f" {item.unit}   remaining "
            f"{st.PRE_MEASUREMENT if remaining is None else format(remaining, 'g')}")
    return tuple(lines)


BLOCK_LINES: Mapping[str, Callable[[EvidenceLedgerView], Tuple[str, ...]]] = {
    "stream": stream_lines,
    "closure": closure_lines,
    "confirmation": confirmation_lines,
    "harm": harm_lines,
}


def block_text(view: EvidenceLedgerView) -> Mapping[str, Tuple[str, ...]]:
    """Every block's text for one view."""
    return {key: BLOCK_LINES[key](view) for key, _label in BLOCKS}


class EvidenceLedgerWorkspace:
    """Read-only.  Four text widgets, no input, no callback."""

    id = "evidence_ledger"
    title = "Evidence Ledger"

    def __init__(self, bus: Any = None, *,
                 theme: str = tokens.DEFAULT_THEME,
                 scale: float = tokens.SCALE_NORMAL) -> None:
        self.bus = bus
        self.theme = theme
        self.scale = scale
        self._palette = tokens.theme(theme)
        self.frame = None
        self._texts: dict = {}
        self._view: EvidenceLedgerView = EvidenceLedgerView()
        self._unsubscribers: List[Callable[[], None]] = []

    def build(self, parent) -> None:
        import tkinter as tk

        pad = tokens.SPACING["tight"]
        self.frame = tk.Frame(parent, bg=self._palette["bg"])
        self.frame.pack(fill="both", expand=True)
        for key, label in BLOCKS:
            tk.Label(self.frame, text=label, anchor="w",
                     bg=self._palette["bg"], fg=self._palette["fg"],
                     font=tokens.font("label", scale=self.scale, bold=True)
                     ).pack(fill="x", padx=tokens.SPACING["base"],
                            pady=(pad, 0))
            widget = tk.Text(
                self.frame, height=10 if key == "stream" else 6, wrap="none",
                relief="flat", bg=self._palette["panel_alt_bg"],
                fg=self._palette["fg"],
                font=tokens.font("small", scale=self.scale, mono=True))
            widget.pack(fill="both", expand=(key == "stream"),
                        padx=tokens.SPACING["base"])
            widget.configure(state="disabled")
            self._texts[key] = widget
        self._bind_bus()
        self.on_evidence(self._view)

    @property
    def widget(self):
        return self.frame

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
            self.on_evidence(payload.evidence)

    def on_evidence(self, view: EvidenceLedgerView) -> None:
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
            widget.configure(state="disabled")

    def lines(self) -> Mapping[str, Tuple[str, ...]]:
        return block_text(self._view)

    def on_state(self, state: SessionState) -> None:
        return None

    def on_activate(self) -> None:
        if self.bus is None:
            return
        try:
            payload = self.bus.snapshot("cockpit")
        except Exception:
            return
        if isinstance(payload, CockpitSnapshot):
            self.on_evidence(payload.evidence)

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


__all__ = ["BLOCKS", "BLOCK_LINES", "STREAM_WINDOW",
           "EvidenceLedgerWorkspace", "block_text", "closure_lines",
           "confirmation_lines", "harm_lines", "stream_lines"]
