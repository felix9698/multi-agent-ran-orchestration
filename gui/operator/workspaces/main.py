"""The Main workspace: one screen that carries the whole experiment.

Every other pane in this console answers one question thoroughly.  This one
answers five shallowly, at once, because that is what an operator running a
sitting actually watches and what a paper figure has to show in a single
capture:

    1. which model carries each agent role,
    2. where an intent is typed,
    3. which intents and which actions are live right now,
    4. the utility the run is delivering, ticking,
    5. whether the xApp and the E2 path underneath it are up.

It is deliberately the *shallow* view of each.  The deep view is one tab away
and is not duplicated here: no axis editor, no evidence ledger, no per-trial
table, no normalized-intent echo.  A pane that repeated them would be the
thing this pane exists to fix.

Like every workspace it reads a :class:`SessionState` and nothing else -- it
owns no source, no store and no thread.  The toolkit is imported inside
``build`` so the module stays importable without a display.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from .. import status as st
from .. import tokens
from ..viewmodel.types import SeriesSpec

#: The three roles this pane exposes.  ``monolith`` is deliberately absent: a
#: monolith sitting has no per-role choice to make, and the slot is still
#: editable in Intent & Decision.  Whatever value it already holds is carried
#: through untouched when this pane saves, so showing three here never silently
#: clears a fourth.
ROLE_SLOTS: Tuple[str, ...] = ("target", "control", "trajectory")

#: What counts as "the xApp path" for the status strip, in the order an
#: operator reads it: the thing that acts, the RIC that hosts it, the E2 link
#: it acts over, and the policy plane above.  Any component whose ``kind`` is
#: not one of these stays in Live Operations rather than crowding this row.
STATUS_KINDS: Tuple[str, ...] = ("XAPP", "NEAR_RT_RIC", "E2", "NON_RT_RIC")

#: A metric is plotted when it carries a number.  The utility chart is the
#: run's delivered KPIs over time, so a metric the session reports as
#: unsupported or stale is shown as an unavailable series rather than dropped:
#: a missing line and a flat zero must never look the same.
MAX_SERIES = 6


def _text_of(row: Any, *names: str, default: str = "") -> str:
    for name in names:
        value = getattr(row, name, None)
        if value not in (None, ""):
            return str(value)
    return default


class MainWorkspace:
    """The single-screen operator view.  ``id`` is ``main``."""

    id = "main"
    title = "Main"

    def __init__(self, *, theme: str = tokens.DEFAULT_THEME,
                 on_submit: Optional[Any] = None,
                 on_role_models: Optional[Any] = None,
                 role_models: Optional[Mapping[str, Any]] = None) -> None:
        # Every keyword is optional: ``load_workspace`` retries the constructor
        # bare when a console does not offer one, and a pane that then failed
        # would be replaced by a placeholder tab instead of raising.
        self.theme = theme
        self._on_submit = on_submit
        self._on_role_models = on_role_models
        self._role_models: Dict[str, Any] = dict(role_models or {})
        self.frame = None
        self.buttons: Dict[str, Any] = {}
        self._vars: Dict[str, Any] = {}
        self._combos: Dict[str, Any] = {}
        self._badges: Dict[str, Any] = {}
        self._intent_text = None
        self._intents = None
        self._actions = None
        self._chart = None
        self._series: List[str] = []
        self._ticks = 0.0
        self._backends: Tuple[str, ...] = ()

    # -- construction -------------------------------------------------------- #

    def build(self, parent) -> None:
        import tkinter as tk
        from tkinter import ttk

        palette = tokens.theme(self.theme)
        pad = tokens.SPACING["tight"]
        self.frame = tk.Frame(parent, bg=palette["bg"])
        self.frame.pack(fill="both", expand=True)
        self.frame.columnconfigure(0, weight=3, uniform="main")
        self.frame.columnconfigure(1, weight=4, uniform="main")
        self.frame.rowconfigure(2, weight=1)

        self._style(ttk, palette)
        self._build_agents(tk, ttk, palette, pad)
        self._build_status(tk, palette, pad)
        self._build_intent(tk, ttk, palette, pad)
        self._build_live(tk, ttk, palette, pad)

    def _style(self, ttk, palette) -> None:
        """Put the ttk widgets on the console palette.

        ``ttk.Treeview`` ignores the ``bg``/``fg`` a tk widget takes, so a pane
        that themes its frames but not its tables ends up half dark and half
        the platform default -- which is exactly what a figure caption cannot
        explain.  The style is named after this pane so it cannot reach another.
        """
        style = ttk.Style()
        style.configure("Main.Treeview", background=palette["panel_bg"],
                        fieldbackground=palette["panel_bg"], foreground=palette["fg"],
                        borderwidth=0, rowheight=20)
        style.configure("Main.Treeview.Heading", background=palette["bg"],
                        foreground=palette["fg_muted"], relief="flat")
        style.map("Main.Treeview", background=[("selected", palette["bg"])])
        style.configure("Main.TCombobox", fieldbackground=palette["panel_bg"],
                        background=palette["bg"], foreground=palette["fg"],
                        arrowcolor=palette["fg"], borderwidth=0)
        style.map("Main.TCombobox",
                  fieldbackground=[("readonly", palette["panel_bg"])],
                  foreground=[("readonly", palette["fg"])])

    def _build_agents(self, tk, ttk, palette, pad) -> None:
        box = tk.LabelFrame(self.frame, text="Agent models",
                            bg=palette["panel_bg"], fg=palette["fg"],
                            font=tokens.font("small", bold=True))
        box.grid(row=0, column=0, sticky="nsew", padx=pad, pady=pad)
        box.columnconfigure(1, weight=1)
        # Method first, then the three roles: reading order matches the
        # decision order, since the method decides whether the roles are used
        # at all.
        from ..widgets.agent_sitting import METHODS

        rows = (("method", METHODS),) + tuple((slot, ()) for slot in ROLE_SLOTS)
        for index, (key, values) in enumerate(rows):
            tk.Label(box, text=key, anchor="w", bg=palette["panel_bg"],
                     fg=palette["fg_muted"], font=tokens.font("small")).grid(
                         row=index, column=0, sticky="w", padx=(pad, pad // 2),
                         pady=1)
            variable = tk.StringVar(value=str(self._role_models.get(key) or ""))
            combo = ttk.Combobox(box, textvariable=variable, state="readonly",
                                 values=list(values), width=22,
                                 style="Main.TCombobox")
            combo.grid(row=index, column=1, sticky="ew", padx=(0, pad), pady=1)
            self._vars[key], self._combos[key] = variable, combo
        save = tk.Button(box, text="Use for the next sitting",
                         command=self._save_models,
                         font=tokens.font("small"))
        save.grid(row=len(rows), column=0, columnspan=2, sticky="ew",
                  padx=pad, pady=(pad // 2, pad))
        self.buttons["save_models"] = save

    def _build_status(self, tk, palette, pad) -> None:
        from ..widgets.statusbadge import StatusBadge

        box = tk.LabelFrame(self.frame, text="xApp and E2 path",
                            bg=palette["panel_bg"], fg=palette["fg"],
                            font=tokens.font("small", bold=True))
        box.grid(row=0, column=1, sticky="nsew", padx=pad, pady=pad)
        for index, kind in enumerate(STATUS_KINDS):
            badge = StatusBadge(theme=self.theme)
            badge.build(box, status=st.UNKNOWN, label=kind)
            badge.widget.grid(row=index, column=0, sticky="w", padx=pad, pady=1)
            self._badges[kind] = badge

    def _build_intent(self, tk, ttk, palette, pad) -> None:
        box = tk.LabelFrame(self.frame, text="Intent",
                            bg=palette["panel_bg"], fg=palette["fg"],
                            font=tokens.font("small", bold=True))
        box.grid(row=1, column=0, columnspan=2, sticky="ew", padx=pad, pady=0)
        box.columnconfigure(0, weight=1)
        self._intent_text = tk.Text(box, height=3, wrap="word",
                                    font=tokens.font("body"),
                                    bg=palette["panel_bg"], fg=palette["fg"],
                                    insertbackground=palette["fg"],
                                    relief="flat", highlightthickness=1,
                                    highlightbackground=palette["fg_muted"])
        self._intent_text.grid(row=0, column=0, sticky="ew", padx=pad, pady=pad)
        submit = tk.Button(box, text="Submit intent", command=self._submit,
                           font=tokens.font("small", bold=True))
        submit.grid(row=0, column=1, sticky="ns", padx=(0, pad), pady=pad)
        self.buttons["submit"] = submit

    def _build_live(self, tk, ttk, palette, pad) -> None:
        from ..widgets.charts import TimeSeriesChart

        left = tk.Frame(self.frame, bg=palette["bg"])
        left.grid(row=2, column=0, sticky="nsew", padx=pad, pady=pad)
        left.rowconfigure(0, weight=3)
        left.rowconfigure(1, weight=2)
        left.columnconfigure(0, weight=1)

        # Four columns, not the thirteen of the full intent table: on this pane
        # the question is which intents are live and how they are doing, and the
        # policy identifiers that answer "why" are one tab away.
        intents = tk.LabelFrame(left, text="Active intents",
                                bg=palette["panel_bg"], fg=palette["fg"],
                                font=tokens.font("small", bold=True))
        intents.grid(row=0, column=0, sticky="nsew")
        self._intents = ttk.Treeview(
            intents, columns=("intent", "state", "policy", "text"),
            show="headings", height=6, style="Main.Treeview")
        for key, heading, width in (("intent", "intent", 90),
                                    ("state", "state", 110),
                                    ("policy", "policy", 150),
                                    ("text", "text", 220)):
            self._intents.heading(key, text=heading)
            self._intents.column(key, width=width, anchor="w")
        self._intents.pack(fill="both", expand=True, padx=pad, pady=pad)

        actions = tk.LabelFrame(left, text="Actions",
                                bg=palette["panel_bg"], fg=palette["fg"],
                                font=tokens.font("small", bold=True))
        actions.grid(row=1, column=0, sticky="nsew", pady=(pad, 0))
        self._actions = ttk.Treeview(
            actions, columns=("state", "action", "confidence"),
            show="headings", height=5, style="Main.Treeview")
        for key, heading, width in (("state", "", 90),
                                    ("action", "action", 330),
                                    ("confidence", "conf", 70)):
            self._actions.heading(key, text=heading)
            self._actions.column(key, width=width, anchor="w")
        self._actions.pack(fill="both", expand=True, padx=pad, pady=pad)

        self._chart = TimeSeriesChart(
            self.frame, title="Delivered utility", y_label="value", y_unit="",
            theme=self.theme, height_px=260)
        self._chart.grid(row=2, column=1, sticky="nsew", padx=pad, pady=pad)

    # -- state --------------------------------------------------------------- #

    def on_state(self, state) -> None:
        if self.frame is None:
            return
        self._paint_models(state)
        self._paint_status(state)
        self._paint_intents(state)
        self._paint_actions(state)
        self._paint_chart(state)

    def _paint_models(self, state) -> None:
        names = tuple(str(backend.name) for backend in getattr(state, "llm_backends", ()))
        if names == self._backends:
            return
        self._backends = names
        # "deterministic" is a real choice, not an empty one: a role with no
        # model runs the deterministic rule, and the operator has to be able to
        # ask for that explicitly rather than by clearing a box.
        options = ["deterministic", *names]
        for slot in ROLE_SLOTS:
            combo = self._combos.get(slot)
            if combo is not None:
                combo.configure(values=options)

    def _paint_status(self, state) -> None:
        by_kind = {}
        for component in getattr(state, "components", ()):
            by_kind.setdefault(str(getattr(component, "kind", "")), component)
        for kind, badge in self._badges.items():
            component = by_kind.get(kind)
            if component is None:
                # Absent is not healthy and not broken: say so, in the same
                # vocabulary the rest of the console uses.
                badge.update(st.UNKNOWN, label=kind,
                             reason="no component of this kind is reported")
                continue
            badge.update(str(getattr(component, "status", st.UNKNOWN)),
                         label=_text_of(component, "label", default=kind),
                         reason=getattr(component, "status_reason", None),
                         gap_id=getattr(component, "gap_id", None))

    def _paint_intents(self, state) -> None:
        self._intents.delete(*self._intents.get_children())
        for row in getattr(state, "intents", ()):
            self._intents.insert(
                "", "end",
                values=(_text_of(row, "intent_id"),
                        _text_of(row, "intent_state", "lifecycle", default="-"),
                        _text_of(row, "policy_status", default="-"),
                        _text_of(row, "text")))

    def _paint_actions(self, state) -> None:
        self._actions.delete(*self._actions.get_children())
        decision = getattr(state, "decision", None)
        for alternative in getattr(decision, "alternatives", ()) or ():
            confidence = getattr(alternative, "confidence", None)
            self._actions.insert(
                "", "end",
                values=("applied" if getattr(alternative, "accepted", False)
                        else "candidate",
                        _text_of(alternative, "description", "alternative_id"),
                        "-" if confidence is None else f"{float(confidence):.2f}"))

    @staticmethod
    def _plotted(state) -> Tuple[Tuple[Any, ...], str, int]:
        """The metrics that may share one axis, their unit, and what was left off.

        A ratio and a bitrate on one y-axis is not a utility plot, it is two
        plots drawn on top of each other: 0.93 next to 9.4 Mbps reads as a
        collapsed KPI.  So the chart takes the largest single-unit group -- the
        per-UE goodput, in this deployment -- names that unit on the axis, and
        says how many series it is not showing.  The others are not hidden;
        they are in Analysis, at their own scale.
        """
        metrics = tuple(getattr(state, "metrics", ()) or ())
        if not metrics:
            return (), "", 0
        groups: Dict[str, List[Any]] = {}
        for metric in metrics:
            groups.setdefault(str(getattr(metric, "unit", "") or ""), []).append(metric)
        unit, chosen = max(groups.items(), key=lambda item: (len(item[1]), item[0]))
        kept = tuple(chosen[:MAX_SERIES])
        return kept, unit, len(metrics) - len(kept)

    def _paint_chart(self, state) -> None:
        metrics, unit, omitted = self._plotted(state)
        self._chart.y_unit = unit
        self._chart.y_label = "delivered"
        self._chart.title = ("Delivered utility" if not omitted else
                             f"Delivered utility ({omitted} more in Analysis)")
        ids = [str(getattr(m, "metric", "")) for m in metrics]
        if ids != self._series:
            self._series = ids
            self._chart.set_series([
                SeriesSpec(series_id=str(getattr(metric, "metric", "")),
                           label=_text_of(metric, "display", "metric"),
                           unit=_text_of(metric, "unit"),
                           source_class=_text_of(metric, "source_class"),
                           derived=bool(getattr(metric, "derived", False)),
                           # ``series_style`` also carries a bar hatch, which a
                           # line series has no field for; take the three keys
                           # a SeriesSpec actually names rather than splatting.
                           **{key: value
                              for key, value in tokens.series_style(index).items()
                              if key in ("color", "linestyle", "marker")})
                for index, metric in enumerate(metrics)])
        # One x per painted state, not per wall-clock second: the console paints
        # the visible workspace on its own tick, and elapsed_s is the run's own
        # clock whenever the session offers one.
        elapsed = getattr(state, "elapsed_s", None)
        self._ticks = float(elapsed) if elapsed is not None else self._ticks + 1.0
        for metric in metrics:
            series_id = str(getattr(metric, "metric", ""))
            value = getattr(metric, "last_value", None)
            status = str(getattr(metric, "status", "") or st.UNKNOWN)
            if value is None:
                # A gap, never a zero.  ``append(None)`` is the chart's own way
                # of saying "no sample here"; writing 0.0 would draw a KPI
                # collapse that never happened.
                self._chart.set_unavailable(
                    series_id, status,
                    _text_of(metric, "reason", default="no sample in this window"))
                self._chart.append(series_id, self._ticks, None)
            else:
                self._chart.append(series_id, self._ticks, float(value))
        self._chart.refresh()

    # -- operator actions ----------------------------------------------------- #

    def _submit(self) -> None:
        if self._on_submit is None or self._intent_text is None:
            return
        text = self._intent_text.get("1.0", "end").strip()
        if text:
            self._on_submit({"intentText": text})

    def _save_models(self) -> None:
        if self._on_role_models is None:
            return
        chosen = dict(self._role_models)
        chosen["method"] = self._vars["method"].get() or "three-agent"
        for slot in ROLE_SLOTS:
            value = self._vars[slot].get().strip()
            # An empty box and the word "deterministic" mean the same thing to
            # ``RoleModels``: no model carries this role.
            chosen[slot] = None if value in ("", "deterministic") else value
        self._role_models = chosen
        self._on_role_models(chosen)

    # -- lifecycle ------------------------------------------------------------ #

    def on_activate(self) -> None:
        if self._chart is not None:
            self._chart.refresh(force=True)

    def on_deactivate(self) -> None:
        return None

    def gui_state(self) -> Mapping[str, Any]:
        state: Dict[str, Any] = {key: var.get() for key, var in self._vars.items()}
        if self._intent_text is not None:
            state["intentText"] = self._intent_text.get("1.0", "end").strip()
        return state

    def restore_gui_state(self, state: Mapping[str, Any]) -> None:
        for key, var in self._vars.items():
            if key in state:
                var.set(str(state[key] or ""))
        text = state.get("intentText")
        if text and self._intent_text is not None:
            self._intent_text.delete("1.0", "end")
            self._intent_text.insert("1.0", str(text))


__all__ = ["MainWorkspace", "ROLE_SLOTS", "STATUS_KINDS"]
