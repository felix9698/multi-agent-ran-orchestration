"""Objective Registry: what is true of each objective, drawn and nothing else.

Owner lane: **OBJ3** (``docs/architecture/SEAMS-GATE4.md`` section 3).  Gate 4's
fourth acceptance item -- "objective별 actual capability/evidence level이
registry와 GUI에 정직하게 표시" (task section 13) -- is this pane.

It is the most deliberately inert workspace in the console, and every part of
that is a rule rather than a shortcut:

**No control.**  There is no button, no entry, no checkbox, no selection and no
callback.  The pane holds two text widgets, both disabled the moment they are
written.  Design section 11 and task section 9.9 forbid a GUI path that changes
a verdict or an evidence closure; a support state is the same kind of fact one
step further back, so the honest expression of "the operator cannot change this"
is a pane with nothing to press.  ``tests/gui/test_obj3_registry_projection.py``
asserts that structurally over this module's syntax tree, not by clicking around
and finding nothing.

**No computation.**  Every line comes from
:mod:`gui.operator.sources.objective_registry`, which reads
:func:`assurance.objectives.registry.registry_view`.  This module chooses
geometry and colour; it decides nothing.  If the registry says a family is
unsupported and why, the pane says exactly that, at exactly that length.

**No state to take.**  :meth:`ObjectiveRegistryWorkspace.on_state` ignores the
session state, and :meth:`~ObjectiveRegistryWorkspace.gui_state` returns an empty
mapping: nothing here depends on a Live or Replay session, and a registry that
changed with the session would be a claim about the session rather than about the
deployment.  The pane reads the same in Live, in Replay and Disconnected, which
is what makes it usable as the answer to "what can this system actually do".
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from .. import tokens
from ..sources.objective_registry import (
    ObjectiveRegistryView,
    detail_lines,
    project_registry,
    summary_text,
)
from ..viewmodel.types import SessionState

#: The pane's two regions, in the order they are stacked.  Declared as data so a
#: test can name a region without reaching for a private attribute.
PANE_REGIONS: Tuple[Tuple[str, str], ...] = (
    ("summary", "Objective support and evidence, at a glance"),
    ("detail", "Per objective: standard mapping, premises, blocking reasons"),
)

#: The two-axis reminder shown above the table.  On screen rather than only in a
#: docstring, because the confusion it prevents -- reading a hardware-free
#: verification as a testbed result -- is the operator's to make, not the
#: reader's of this file.
AXIS_NOTE = (
    "Support state = how far implementation and verification got. "
    "Evidence level = what was actually observed. "
    "Only OTA raw evidence comes from the radio; a hardware-free round trip "
    "never does."
)


class ObjectiveRegistryWorkspace:
    """A read-only projection of the objective registry.  No control surface."""

    id = "objective_registry"
    title = "Objective Registry"

    def __init__(self, *, theme: str = tokens.DEFAULT_THEME,
                 scale: float = tokens.SCALE_NORMAL,
                 view: Optional[ObjectiveRegistryView] = None) -> None:
        self.theme = theme
        self.scale = scale
        self._palette = tokens.theme(theme)
        #: The projection this pane draws.  Injectable so a test can drive the
        #: five support states through the real renderer; ``None`` means the
        #: real registry, which is what the console always gets.
        self.view: ObjectiveRegistryView = (
            project_registry() if view is None else view)
        self.frame = None
        self._texts: Dict[str, Any] = {}

    # -- build --------------------------------------------------------------

    def build(self, parent) -> None:
        import tkinter as tk

        pad = tokens.SPACING["tight"]
        self.frame = tk.Frame(parent, bg=self._palette["bg"])
        self.frame.pack(fill="both", expand=True)

        header = tk.Frame(self.frame, bg=self._palette["panel_bg"])
        header.pack(fill="x", padx=tokens.SPACING["base"], pady=pad)
        tk.Label(header, text="Objective registry", anchor="w",
                 bg=self._palette["panel_bg"], fg=self._palette["fg"],
                 font=tokens.font("subhead", scale=self.scale, bold=True)
                 ).pack(side="left", padx=tokens.SPACING["base"])
        tk.Label(header, text="read-only", anchor="e",
                 bg=self._palette["panel_bg"], fg=self._palette["fg_muted"],
                 font=tokens.font("small", scale=self.scale, bold=True)
                 ).pack(side="right", padx=tokens.SPACING["base"])

        # Task section 7.7 travels with the projection: these names are the
        # project's, not names ETSI or O-RAN defined.
        tk.Label(self.frame, text=self.view.identifier_notice, anchor="w",
                 justify="left", wraplength=1100,
                 bg=self._palette["bg"], fg=self._palette["fg_muted"],
                 font=tokens.font("small", scale=self.scale)
                 ).pack(fill="x", padx=tokens.SPACING["base"])
        tk.Label(self.frame, text=AXIS_NOTE, anchor="w", justify="left",
                 wraplength=1100,
                 bg=self._palette["bg"], fg=self._palette["fg_muted"],
                 font=tokens.font("small", scale=self.scale)
                 ).pack(fill="x", padx=tokens.SPACING["base"], pady=(0, pad))

        for region, caption in PANE_REGIONS:
            tk.Label(self.frame, text=caption, anchor="w",
                     bg=self._palette["bg"], fg=self._palette["fg"],
                     font=tokens.font("label", scale=self.scale, bold=True)
                     ).pack(fill="x", padx=tokens.SPACING["base"])
            widget = tk.Text(
                self.frame, height=11 if region == "summary" else 26,
                wrap="none", relief="flat",
                bg=self._palette["panel_alt_bg"], fg=self._palette["fg"],
                font=tokens.font("small", scale=self.scale, mono=True))
            widget.pack(fill="both", expand=(region == "detail"),
                        padx=tokens.SPACING["base"], pady=(0, pad))
            widget.configure(state="disabled")
            self._texts[region] = widget

        self.repaint()

    @property
    def widget(self):
        return self.frame

    # -- painting -----------------------------------------------------------

    def lines(self) -> Dict[str, Tuple[str, ...]]:
        """What each region says, without a display.

        The same text the widgets receive, so a headless test reads the pane
        rather than a description of it.
        """
        summary: List[str] = [
            f"{len(self.view.rows)} objectives  "
            f"(seven families plus the preserved regression contract "
            f"{self.view.regression_contract})",
            "",
        ]
        summary.extend(summary_text(self.view))
        detail: List[str] = []
        for row in self.view.rows:
            detail.extend(detail_lines(row))
            detail.append("")
        return {"summary": tuple(summary), "detail": tuple(detail)}

    def repaint(self) -> None:
        for region, text in self.lines().items():
            self._write(self._texts.get(region), text)

    def _write(self, widget, lines: Tuple[str, ...]) -> None:
        if widget is None:
            return
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", "\n".join(lines))
        # Disabled again immediately.  There is no edit of this text that could
        # change what the registry says, and a text box the operator can type
        # into implies there is.
        widget.configure(state="disabled")

    # -- workspace protocol -------------------------------------------------

    def on_state(self, state: SessionState) -> None:
        """Nothing to take.

        The registry is a fact about the deployment and the code, not about the
        session: a pane that changed with Live/Replay/Disconnected would be
        saying the objective catalog depends on what the console is connected
        to, which is false and would be the more dangerous kind of false.
        """
        return None

    def on_activate(self) -> None:
        return None

    def on_deactivate(self) -> None:
        return None

    def gui_state(self) -> Mapping[str, Any]:
        """Nothing is typed here, so nothing is saved."""
        return {}

    def restore_gui_state(self, state: Mapping[str, Any]) -> None:
        return None

    def destroy(self) -> None:
        if self.frame is not None:
            self.frame.destroy()
            self.frame = None


ObjectiveRegistry = ObjectiveRegistryWorkspace

__all__ = ["AXIS_NOTE", "PANE_REGIONS", "ObjectiveRegistry",
           "ObjectiveRegistryWorkspace"]
