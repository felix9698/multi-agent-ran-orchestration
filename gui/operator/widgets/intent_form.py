"""Natural-language-first intent entry with optional structured supplements."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Optional

from gui.operator.status import PRE_MEASUREMENT


class IntentForm:
    """Composition, toolkit imported in the constructor - see ``FSMView``."""

    def __init__(self, parent, *,
                 on_preview: Optional[Callable[[Mapping[str, Any]], None]] = None,
                 on_submit: Optional[Callable[[Mapping[str, Any]], None]] = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.frame = ttk.LabelFrame(parent, text="Operator intent")
        self._on_preview = on_preview
        self._on_submit = on_submit
        self._normalized: Optional[Mapping[str, Any]] = None
        self._text = tk.Text(self.frame, height=4, wrap="word", undo=True)
        self._text.grid(row=0, column=0, columnspan=8, sticky="nsew", padx=6, pady=5)
        self.frame.columnconfigure(0, weight=2)
        self.frame.columnconfigure(2, weight=1)
        self.frame.columnconfigure(4, weight=1)
        self.frame.columnconfigure(6, weight=1)

        self.target = tk.StringVar()
        self.scope = tk.StringVar()
        self.priority = tk.StringVar(value="MEDIUM")
        self.validity = tk.StringVar()
        self.goals = tk.StringVar()
        self.constraints = tk.StringVar()
        fields = (
            ("Target", self.target), ("Scope", self.scope),
            ("Priority", self.priority), ("Validity", self.validity),
            ("Goals", self.goals), ("Constraints", self.constraints),
        )
        for index, (label, variable) in enumerate(fields):
            row = 1 + index // 3
            col = (index % 3) * 2
            ttk.Label(self.frame, text=label).grid(
                row=row, column=col, sticky="e", padx=(5, 2), pady=2)
            if label == "Priority":
                widget = ttk.Combobox(self.frame, textvariable=variable,
                                      state="readonly",
                                      values=("CRITICAL", "HIGH", "MEDIUM", "LOW"), width=14)
            else:
                widget = ttk.Entry(self.frame, textvariable=variable)
            widget.grid(row=row, column=col + 1, sticky="ew", padx=(0, 6), pady=2)

        buttons = ttk.Frame(self.frame)
        buttons.grid(row=3, column=0, columnspan=8, sticky="ew", padx=5, pady=4)
        ttk.Button(buttons, text="Preview normalized intent",
                   command=self._preview).pack(side="left")
        self._submit_button = ttk.Button(
            buttons, text="Submit intent", command=self._submit, state="normal")
        self._submit_button.pack(side="right")
        self._preview_title = ttk.Label(
            self.frame, text="Normalized intent · coordinator validator")
        self._preview_title.grid(row=4, column=0, columnspan=8, sticky="w", padx=6)
        self._preview_text = tk.Text(self.frame, height=5, wrap="word",
                                     state="disabled")
        self._preview_text.grid(row=5, column=0, columnspan=8, sticky="nsew", padx=6, pady=(2, 6))
        self.frame.rowconfigure(0, weight=1)

    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    def grid(self, **kwargs) -> None:
        self.frame.grid(**kwargs)

    def destroy(self) -> None:
        self.frame.destroy()

    def values(self) -> Mapping[str, Any]:
        return {
            "intentText": self._text.get("1.0", "end").strip(),
            "target": self.target.get().strip() or None,
            "scope": self.scope.get().strip() or None,
            "priority": self.priority.get().strip() or None,
            "validity": self.validity.get().strip() or None,
            "goals": tuple(v.strip() for v in self.goals.get().split(",") if v.strip()),
            "constraints": tuple(v.strip() for v in self.constraints.get().split(",") if v.strip()),
            "normalizedIntent": self._normalized,
        }

    def _preview(self) -> None:
        if self._on_preview is not None:
            self._on_preview(self.values())

    def _submit(self) -> None:
        values = self.values()
        if not values["intentText"]:
            self.set_normalized(None, reason="Natural-language intent is required")
            return
        if self._on_submit is not None:
            self._on_submit(values)

    def set_normalized(self, value: Optional[Mapping[str, Any]], *,
                       reason: Optional[str] = None) -> None:
        if value is None:
            self._normalized = None
            self._submit_button.configure(state="normal")
            text = f"? Unknown · {reason or 'Coordinator normalization not observed'}"
        else:
            self._normalized = dict(value)
            self._submit_button.configure(state="normal")
            text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        self._preview_text.configure(state="normal")
        self._preview_text.delete("1.0", "end")
        self._preview_text.insert("1.0", text or PRE_MEASUREMENT)
        self._preview_text.configure(state="disabled")

    def set_intent_text(self, value: str) -> None:
        self._text.delete("1.0", "end")
        self._text.insert("1.0", value)


__all__ = ["IntentForm"]
