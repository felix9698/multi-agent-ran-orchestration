"""The confirmation flow.

phaseB_task.md section 1: any action that interrupts or withdraws part of a
running experiment must show *what it affects* and *what it will do* before the
operator commits.  ``boundary-map.1.0.0.json`` names four such actions and fixes
which of them need typed acknowledgement rather than a click.

The decision of whether a confirmation is satisfied is a pure function
(:func:`evaluate_confirmation`) so it can be asserted without a display, and so
the dialog cannot accidentally become the place where the rule lives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from .. import status as st
from .. import tokens
from ..viewmodel.types import ConfirmationSpec

SINGLE_CONFIRM: str = "SINGLE_CONFIRM"
TYPED_CONFIRM: str = "TYPED_CONFIRM"


def spec_for(action_id: str, *, title: str, targets: Tuple[str, ...],
             effects: Tuple[str, ...]) -> ConfirmationSpec:
    """Build the :class:`ConfirmationSpec` for one of the four named actions.

    The acknowledgement strength is decided here, from the action id, so a
    caller cannot downgrade a withdrawal or an abort to a single click by
    passing a different argument.
    """
    typed = action_id in ("C-INTENT-WITHDRAW", "C-SESSION-ABORT")
    phrase = {"C-INTENT-WITHDRAW": "WITHDRAW",
              "C-SESSION-ABORT": "ABORT"}.get(action_id)
    return ConfirmationSpec(
        action_id=action_id, title=title, targets=tuple(targets),
        effects=tuple(effects),
        acknowledgement=TYPED_CONFIRM if typed else SINGLE_CONFIRM,
        typed_phrase=phrase, irreversible=typed,
    )


@dataclass(frozen=True)
class ConfirmationOutcome:
    """Whether the acknowledgement satisfied the spec, and why not."""

    confirmed: bool
    reason: Optional[str] = None


def evaluate_confirmation(spec: ConfirmationSpec, *, acknowledged: bool,
                          typed: Optional[str] = None) -> ConfirmationOutcome:
    """Decide whether ``spec`` has been satisfied.

    A typed confirmation requires the exact phrase.  Matching is
    case-insensitive on surrounding whitespace only - a near miss is refused
    rather than accepted, because the whole point of the typed form is that it
    cannot be produced by a reflex click.
    """
    if not acknowledged:
        return ConfirmationOutcome(False, "not acknowledged")
    if spec.acknowledgement != TYPED_CONFIRM:
        return ConfirmationOutcome(True)
    expected = (spec.typed_phrase or "").strip()
    if not expected:
        return ConfirmationOutcome(
            False, "typed confirmation required but no phrase was specified")
    if (typed or "").strip() != expected:
        return ConfirmationOutcome(False, f"type {expected!r} to confirm")
    return ConfirmationOutcome(True)


def describe(spec: ConfirmationSpec) -> str:
    """The text the dialog shows, also used in the timeline audit record."""
    lines = [spec.title, "", "Affects:"]
    lines += [f"  - {target}" for target in spec.targets] or ["  - (none)"]
    lines += ["", "Effect:"]
    lines += [f"  - {effect}" for effect in spec.effects] or ["  - (none)"]
    if spec.irreversible:
        lines += ["", "This action cannot be undone."]
    return "\n".join(lines)


class ConfirmationDialog:
    """Modal Tk confirmation.  Holds no rule of its own.

    ``ask`` returns the :class:`ConfirmationOutcome` produced by
    :func:`evaluate_confirmation`, so the widget and the headless path agree by
    construction rather than by review.
    """

    def __init__(self, *, theme: str = tokens.DEFAULT_THEME,
                 scale: float = tokens.SCALE_NORMAL) -> None:
        self.theme = theme
        self.scale = scale
        self._palette = tokens.theme(theme)

    def ask(self, parent, spec: ConfirmationSpec) -> ConfirmationOutcome:
        import tkinter as tk

        outcome = {"value": ConfirmationOutcome(False, "dismissed")}
        window = tk.Toplevel(parent)
        window.title(spec.title)
        window.configure(bg=self._palette["panel_bg"])
        window.transient(parent)
        window.grab_set()

        tk.Label(window, text=describe(spec), justify="left", anchor="w",
                 bg=self._palette["panel_bg"], fg=self._palette["fg"],
                 font=tokens.font("body", scale=self.scale)
                 ).pack(fill="both", padx=tokens.SPACING["section"],
                        pady=tokens.SPACING["base"])

        entry = None
        if spec.acknowledgement == TYPED_CONFIRM:
            tk.Label(window, text=f"Type {spec.typed_phrase} to confirm",
                     bg=self._palette["panel_bg"],
                     fg=tokens.status_color(st.BLOCKED, self.theme),
                     font=tokens.font("small", scale=self.scale)
                     ).pack(fill="x", padx=tokens.SPACING["section"])
            entry = tk.Entry(window, bg=self._palette["panel_alt_bg"],
                             fg=self._palette["fg"], relief="flat",
                             font=tokens.font("body", scale=self.scale))
            entry.pack(fill="x", padx=tokens.SPACING["section"],
                       pady=tokens.SPACING["tight"])

        row = tk.Frame(window, bg=self._palette["panel_bg"])
        row.pack(fill="x", padx=tokens.SPACING["section"],
                 pady=tokens.SPACING["base"])

        def _finish(acknowledged: bool) -> None:
            outcome["value"] = evaluate_confirmation(
                spec, acknowledged=acknowledged,
                typed=entry.get() if entry is not None else None)
            if outcome["value"].confirmed or not acknowledged:
                window.destroy()

        tk.Button(row, text="Cancel", relief="flat",
                  command=lambda: _finish(False),
                  bg=self._palette["panel_alt_bg"], fg=self._palette["fg"],
                  font=tokens.font("small", scale=self.scale)
                  ).pack(side="right", padx=tokens.SPACING["hair"])
        tk.Button(row, text="Confirm", relief="flat",
                  command=lambda: _finish(True),
                  bg=tokens.status_color(
                      st.ERROR if spec.irreversible else st.OK, self.theme),
                  fg=self._palette["fg_inverse"],
                  font=tokens.font("small", scale=self.scale, bold=True)
                  ).pack(side="right", padx=tokens.SPACING["hair"])

        parent.wait_window(window)
        return outcome["value"]


#: Signature a console injects to make the confirmation flow testable.
ConfirmationAsker = Callable[[ConfirmationSpec], ConfirmationOutcome]

__all__ = ["ConfirmationAsker", "ConfirmationDialog", "ConfirmationOutcome",
           "SINGLE_CONFIRM", "TYPED_CONFIRM", "describe",
           "evaluate_confirmation", "spec_for"]
