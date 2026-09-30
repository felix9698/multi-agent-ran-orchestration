"""Dense filterable table.

Owner: track **T3**; consumed by T1, T2 and T4 through the constructor signature
frozen in ``file-ownership.1.0.0.json``.

``ttk.Treeview`` is the widget the existing console never used, and it is what
makes the density §1 asks for achievable without a new toolkit.  As everywhere
in this track the decisions live in a pure model (:class:`TableModel`) and the
widget only paints them, so filtering and sorting are testable without a display
and a renderer swap stays contained.

Two rules that look small and are not:

* **sorting is on the value, not on the rendering.**  A status column sorts by
  its severity rank and a number column sorts numerically, so ``ERROR`` never
  sorts between ``DEGRADED`` and ``OK`` alphabetically and ``10`` never sorts
  before ``9``.  Sorting by the painted string is how a table quietly lies about
  which element is worst.
* **an unmeasured cell renders the em-dash placeholder**, never ``0`` and never
  an empty string that reads as zero in a spreadsheet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Final, List, Mapping, Optional, Sequence, Tuple

from gui.operator import status as st
from gui.operator import tokens

#: Cell kinds a column may declare.
COLUMN_KINDS: Final[tuple] = ("text", "status", "number", "time", "age")


@dataclass(frozen=True)
class ColumnSpec:
    """One column of a dense table."""

    key: str
    heading: str
    width: int = 120
    anchor: str = "w"
    kind: str = "text"
    unit: Optional[str] = None
    decimals: int = 2


def render_cell(value: Any, column: ColumnSpec) -> str:
    """The displayed text for one cell.

    ``None`` always renders as the placeholder.  The distinction between "zero"
    and "not measured" is load bearing everywhere else in this system and a
    table is the easiest place to lose it.
    """
    if value is None:
        return st.PRE_MEASUREMENT
    if column.kind == "status":
        return st.describe(st.resolve(str(value)), include_reason=False)
    if column.kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return st.PRE_MEASUREMENT
        if value != value or value in (float("inf"), float("-inf")):
            return st.PRE_MEASUREMENT
        text = f"{float(value):.{column.decimals}f}"
        return f"{text} {column.unit}".strip() if column.unit else text
    if column.kind == "time":
        return st.format_utc(str(value))
    if column.kind == "age":
        return st.format_age(value if isinstance(value, (int, float)) else None)
    return str(value)


def sort_key(value: Any, column: ColumnSpec):
    """The value a column sorts on.

    Deliberately not the rendered string: a status sorts by severity so the
    worst element reaches the top, and a number sorts numerically.  Missing
    values sort last in either direction, because "unknown" is not "smallest".
    """
    if column.kind == "status":
        spec = st.STATUS_SPECS.get(str(value))
        return (0, -(spec.severity if spec else -1), str(value))
    if column.kind in ("number", "age"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return (1, 0.0, "")
        if value != value:
            return (1, 0.0, "")
        return (0, float(value), "")
    if value is None:
        return (1, 0.0, "")
    return (0, 0.0, str(value).lower())


@dataclass
class TableModel:
    """Rows plus the filter and sort applied to them.  No toolkit."""

    columns: Tuple[ColumnSpec, ...]
    rows: Tuple[Mapping[str, Any], ...] = ()
    filter_text: str = ""
    filter_fields: Optional[Tuple[str, ...]] = None
    sort_column: Optional[str] = None
    sort_descending: bool = False
    max_rows: int = tokens.TABLE_MAX_ROWS

    def set_rows(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.rows = tuple(rows)

    def set_filter(self, text: str, *,
                   fields: Optional[Sequence[str]] = None) -> None:
        self.filter_text = text or ""
        self.filter_fields = tuple(fields) if fields else None

    def set_sort(self, column: str, descending: bool = False) -> None:
        self.sort_column = column
        self.sort_descending = descending

    def _matches(self, row: Mapping[str, Any]) -> bool:
        needle = self.filter_text.strip().lower()
        if not needle:
            return True
        keys = self.filter_fields or tuple(c.key for c in self.columns)
        for key in keys:
            value = row.get(key)
            if value is None:
                continue
            if needle in str(value).lower():
                return True
        return False

    def visible(self) -> List[Mapping[str, Any]]:
        """Filtered and sorted rows, capped at ``max_rows``.

        The cap is a rendering budget, never a silent truncation of meaning:
        :meth:`overflow` reports how many rows it hid so the view can say so.
        """
        rows = [r for r in self.rows if self._matches(r)]
        if self.sort_column:
            column = next((c for c in self.columns if c.key == self.sort_column),
                          None)
            if column is not None:
                rows.sort(key=lambda r: sort_key(r.get(column.key), column),
                          reverse=self.sort_descending)
        return rows[: self.max_rows]

    def overflow(self) -> int:
        return max(0, len([r for r in self.rows if self._matches(r)])
                   - self.max_rows)

    def rendered(self) -> List[Tuple[str, ...]]:
        """Every visible row as display strings, in column order."""
        return [tuple(render_cell(row.get(c.key), c) for c in self.columns)
                for row in self.visible()]


class DenseTable:
    """``ttk.Treeview`` wrapper over a :class:`TableModel`.

    Constructor signature frozen in ``file-ownership.1.0.0.json``; T1, T2 and T4
    code against it.
    """

    def __init__(self, parent, *, columns: Sequence[ColumnSpec],
                 theme: str = tokens.DEFAULT_THEME,
                 on_select: Optional[Callable[[str], None]] = None,
                 id_key: str = "id",
                 height: int = 12) -> None:
        from tkinter import ttk

        self.model = TableModel(columns=tuple(columns))
        self.id_key = id_key
        self._on_select = on_select
        self._palette = tokens.theme(theme)
        keys = [c.key for c in self.model.columns]
        self.frame = ttk.Frame(parent)
        self.tree = ttk.Treeview(self.frame, columns=keys, show="headings",
                                 height=height, selectmode="browse")
        for column in self.model.columns:
            self.tree.heading(
                column.key, text=column.heading,
                command=lambda key=column.key: self._toggle_sort(key))
            self.tree.column(column.key, width=column.width,
                             anchor=column.anchor, stretch=False)
        scroll = ttk.Scrollbar(self.frame, orient="vertical",
                               command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.frame.rowconfigure(0, weight=1)
        self.frame.columnconfigure(0, weight=1)
        if on_select is not None:
            self.tree.bind("<<TreeviewSelect>>", self._selection_changed)

    def grid(self, **kwargs):
        self.frame.grid(**kwargs)
        return self

    def pack(self, **kwargs):
        self.frame.pack(**kwargs)
        return self

    # -- data --------------------------------------------------------------- #

    def set_rows(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.model.set_rows(rows)
        self.refresh()

    def set_filter(self, text: str, *,
                   fields: Optional[Sequence[str]] = None) -> None:
        self.model.set_filter(text, fields=fields)
        self.refresh()

    def set_sort(self, column: str, descending: bool = False) -> None:
        self.model.set_sort(column, descending)
        self.refresh()

    def _toggle_sort(self, key: str) -> None:
        descending = not (self.model.sort_column == key
                          and not self.model.sort_descending)
        self.set_sort(key, descending)

    def refresh(self) -> None:
        """Repaint.  Tk thread only."""
        self.tree.delete(*self.tree.get_children())
        for row, rendered in zip(self.model.visible(), self.model.rendered()):
            identity = str(row.get(self.id_key, ""))
            self.tree.insert("", "end", iid=identity or None, values=rendered)

    def selected(self) -> Optional[str]:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def _selection_changed(self, _event) -> None:
        identity = self.selected()
        if identity is not None and self._on_select is not None:
            self._on_select(identity)


__all__ = ["COLUMN_KINDS", "ColumnSpec", "DenseTable", "TableModel",
           "render_cell", "sort_key"]
