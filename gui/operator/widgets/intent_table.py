"""Searchable intent table with three explicitly separate status axes."""

from __future__ import annotations

from typing import Callable, Optional, Sequence, Tuple

from gui.operator.status import PRE_MEASUREMENT, describe, resolve
from gui.operator.viewmodel.types import IntentRowView


LIFECYCLES = ("ALL", "ACTIVE", "PENDING", "CONFLICT", "NEGOTIATING",
              "SATISFIED", "VIOLATED", "FAILSAFE", "WITHDRAWN")


class IntentTable:
    """Composition, toolkit imported in the constructor - see ``FSMView``."""

    COLUMNS = (
        "id", "revision", "scope", "priority", "validity", "stage",
        "outcome", "intent_axis", "policy_axis", "evidence_axis",
        "policy_id", "freshness", "updated",
    )

    def __init__(self, parent, *, on_select: Optional[Callable[[str], None]] = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.frame = ttk.LabelFrame(
            parent, text="Intent lifecycle · separate authority axes")
        self._rows: Tuple[IntentRowView, ...] = ()
        self._on_select = on_select
        bar = ttk.Frame(self.frame)
        bar.pack(fill="x", padx=4, pady=3)
        ttk.Label(bar, text="Search").pack(side="left")
        self.search = tk.StringVar()
        search_entry = ttk.Entry(bar, textvariable=self.search, width=30)
        search_entry.pack(side="left", padx=(3, 10))
        ttk.Label(bar, text="Lifecycle").pack(side="left")
        self.lifecycle = tk.StringVar(value="ALL")
        combo = ttk.Combobox(bar, state="readonly", textvariable=self.lifecycle,
                             values=LIFECYCLES, width=14)
        combo.pack(side="left", padx=3)
        self.search.trace_add("write", lambda *_: self._render())
        self.lifecycle.trace_add("write", lambda *_: self._render())

        headings = {
            "id": "Intent ID", "revision": "Rev", "scope": "Target / scope",
            "priority": "Priority", "validity": "Validity", "stage": "S0–S6",
            "outcome": "Terminal outcome", "intent_axis": "Intent",
            "policy_axis": "A1 policy", "evidence_axis": "Evidence",
            "policy_id": "Policy ID", "freshness": "Evidence freshness",
            "updated": "Last update",
        }
        self.tree = ttk.Treeview(self.frame, columns=self.COLUMNS,
                                 show="headings", height=10)
        for key in self.COLUMNS:
            self.tree.heading(key, text=headings[key])
            self.tree.column(key, width=105 if key not in {"scope", "updated"} else 145,
                             anchor="w", stretch=True)
        yscroll = ttk.Scrollbar(self.frame, orient="vertical",
                                command=self.tree.yview)
        xscroll = ttk.Scrollbar(self.frame, orient="horizontal",
                                command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.pack(fill="both", expand=True, side="top")
        yscroll.place(relx=1.0, rely=0.18, relheight=0.72, anchor="ne")
        xscroll.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", self._selected)

    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    def grid(self, **kwargs) -> None:
        self.frame.grid(**kwargs)

    def destroy(self) -> None:
        self.frame.destroy()

    @staticmethod
    def _badge(status: Optional[str], detail: Optional[str] = None) -> str:
        return describe(resolve(status or "UNKNOWN", reason=detail))

    def set_rows(self, rows: Sequence[IntentRowView]) -> None:
        self._rows = tuple(rows)
        self._render()

    def _render(self) -> None:
        query = self.search.get().strip().casefold()
        lifecycle = self.lifecycle.get()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for row in self._rows:
            haystack = " ".join((row.intent_id, row.text, row.target_scope or "",
                                 row.policy_id or "")).casefold()
            if query and query not in haystack:
                continue
            if lifecycle != "ALL" and row.lifecycle != lifecycle:
                continue
            # Three cells, never one aggregate status.
            intent_axis = row.eq12_state or row.intent_state or PRE_MEASUREMENT
            values = (
                row.intent_id, row.revision or PRE_MEASUREMENT,
                row.target_scope or PRE_MEASUREMENT, row.priority or PRE_MEASUREMENT,
                row.validity or PRE_MEASUREMENT, row.intent_state or PRE_MEASUREMENT,
                row.terminal_outcome or PRE_MEASUREMENT, intent_axis,
                self._badge(row.policy_status, row.policy_status_detail),
                self._badge(row.evidence_status, row.evidence_quality),
                row.policy_id or PRE_MEASUREMENT,
                row.evidence_freshness or PRE_MEASUREMENT,
                row.last_update_at or PRE_MEASUREMENT,
            )
            self.tree.insert("", "end", iid=row.intent_id, values=values)

    def _selected(self, _event=None) -> None:
        selection = self.tree.selection()
        if selection and self._on_select is not None:
            self._on_select(selection[0])

    def selected(self) -> Optional[str]:
        selection = self.tree.selection()
        return selection[0] if selection else None


__all__ = ["IntentTable", "LIFECYCLES"]
