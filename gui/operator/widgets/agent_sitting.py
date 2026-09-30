"""Read-only episode projection and operator controls for an Agent sitting."""
from __future__ import annotations

import json

MODEL_SLOTS = ("target", "control", "trajectory", "monolith")
METHODS = ("three-agent", "three-agent-coverage", "internal-monolith",
           "basic-monolith", "deterministic")


# Request field names, not action declarations or codecs.
LADDER_FIELDS = {"dlPrbCap": "caps", "pfWeight": "pf_weights",
                 "dlMcsBounds": "mcs_bounds", "txAttenuationDb": "tx_attenuations",
                 "slicePrbQuota": "slice_quotas"}


def axis_settings(kinds, ladders, ceiling):
    """Parse scope:values entries; composite quota values keep their colons."""
    result = {"axes": list(kinds), "max_catalog_cardinality": int(ceiling)}
    for kind, text in ladders.items():
        scopes = {}
        for entry in text.split(";"):
            if not entry.strip():
                continue
            scope, separator, values = entry.partition(":")
            if not separator or not scope.strip() or not values.strip():
                raise ValueError(f"{kind}: use scope:value,value; scope:value,value")
            convert = int if kind == "dlPrbCap" else float if kind == "pfWeight" else str
            scopes[scope.strip()] = tuple(convert(v.strip()) for v in values.split(","))
        result[LADDER_FIELDS[kind]] = scopes
    return result


def cardinality_text(episode):
    preflight = episode.get("execution", {}).get("preflight", {})
    policy = (episode.get("C") or {}).get("constructionPolicy", {})
    declared = policy.get("catalogCardinality", "pending")
    frozen = episode.get("catalogCardinality", "pending")
    ceiling = preflight.get("catalogCeiling", "pending")
    kinds = ", ".join(preflight.get("exposedAxisKinds", ())) or "pending"
    return f"Exposed combinations: {declared} · Frozen: {frozen} · Ceiling: {ceiling} · Axes: {kinds}"


def answer_payload(payload, questions, values, round_index):
    """Use the executor's accumulating answer semantics for another composition."""
    from tools.liveconsole.agent import AgentRequest
    answers = {}
    for question, text in zip(questions, values):
        if not text.strip():
            continue
        try:
            value = json.loads(text)
        except ValueError:
            value = text
        answers.setdefault(question["intentId"], {})[question["field"]] = value
    request = AgentRequest(sentences=tuple(payload.get("sentences", ())),
                           intents=tuple(payload.get("intents", ())),
                           answers=payload.get("answers", {}),
                           clarification_round=round_index).with_answers(answers)
    return {**payload, "answers": dict(request.answers),
            "clarificationRound": request.clarification_round}


def intent_entry(sentence, index, fields):
    from tools.liveconsole.agent import AgentRequest, parse_agent_intents
    import re
    named = sentence if re.match(r"^\s*[A-Za-z][\w.-]*\s*:", sentence) else f"I{index}: {sentence}"
    record = parse_agent_intents(AgentRequest(sentences=(named,)))[0].intent.to_record()
    for key in ("levels", "relaxLimit", "relaxable"):
        record["requirement"].pop(key, None)
    for key, value in fields.items():
        if str(value).strip():
            number = float(value) if key in ("bound", "weight") else int(value)
            (record["requirement"] if key in ("steps", "bound") else record)[key] = number
    req = record["requirement"]
    label = (f"{sentence} | steps={req.get('steps') if req.get('steps') is not None else 'missing'} "
             f"bound={req.get('value') if req.get('steps') == 0 else req.get('bound') if req.get('bound') is not None else 'missing'} "
             f"priority={record.get('priority')} weight={record.get('weight') or 'from priority'}")
    return record, label



INTENT_FIELDS = ("owner", "ueId", "kpi", "op", "value", "unit", "steps", "bound", "priority", "weight", "note")


def intent_fields(record):
    req = record.get("requirement", {})
    return {key: str(value) if value is not None else "" for key, value in {
        **{key: record.get(key, "") for key in ("owner", "ueId", "priority", "weight")},
        **{key: req.get(key, "") for key in ("kpi", "op", "value", "unit", "steps", "bound")},
        "note": record.get("sentence", "")}.items()}


def fields_record(fields, index, previous=None):
    """Convert operator fields without inventing missing authorization."""
    import math
    record = dict(previous or {})
    for key in ("owner", "ueId", "priority", "weight", "sentence"):
        record.pop(key, None)
    record.setdefault("intentId", f"I{index}")
    req = {"reqId": record.get("requirement", {}).get("reqId", record["intentId"] + ".r1")}
    missing = []
    for key in INTENT_FIELDS:
        text = str(fields.get(key, "")).strip()
        required = key not in ("weight", "note", "bound") or (key == "bound" and fields.get("steps") not in ("0", 0))
        if not text:
            if required:
                missing.append(key)
            continue
        try:
            value = text
            if key in ("steps", "priority"):
                value = int(text)
                if key == "steps" and value < 0:
                    raise ValueError()
            elif key in ("weight", "bound") or (key == "value" and fields.get("kpi") != "servingCell"):
                value = float(text)
                if not math.isfinite(value):
                    raise ValueError()
        except ValueError:
            missing.append(key)
            continue
        if key == "note":
            record["sentence"] = value
        elif key in ("owner", "ueId", "priority", "weight"):
            record[key] = value
        else:
            req[key] = value
    if not str(fields.get("weight", "")).strip():
        record.pop("weight", None)
    if not str(fields.get("note", "")).strip():
        record.pop("sentence", None)
    req["scope"] = "ue@" + str(record.get("ueId", ""))
    if req.get("steps") == 0:
        req.pop("bound", None)
    record["requirement"] = req
    return record, missing


def save_intent_set(path, records):
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"intents": records}, indent=2) + "\n")
    temporary.replace(path)


def load_intent_set(path):
    from pathlib import Path
    if not path or not Path(path).exists():
        return []
    data = json.loads(Path(path).read_text())
    return list(data["intents"])

def _record_rows(episode):
    """The agent-episode/1.3.0 facts the operator has to be able to read.

    Every one of these changes what a number in the paper means, so none of
    them may live only in the file: the timing mode says what B measured, the
    excluded flag says whether the episode counts at all, the boundaries say
    what a success before them may still establish, the non-trial events say
    where charged time went that was not a trial, and the injected pair says
    the run did not form its own T and C.
    """
    timing = episode.get("timing") or {}
    budget = episode.get("budget") or {}
    cost = episode.get("resourceCost") or {}
    condition = episode.get("condition") or {}
    preflight = ((episode.get("execution") or {}).get("preflight") or {})
    prepared = episode.get("prepared") or preflight.get("preparedInjected") or {}
    excluded = episode.get("excluded") or {}
    events = episode.get("nonTrialEvents") or []
    kinds = {}
    for item in events:
        kinds[item.get("kind", "?")] = kinds.get(item.get("kind", "?"), 0) + 1
    rows = [
        ("Timing mode", str(timing.get("timingMode", "prepared")),
         f"prep {cost.get('prepMs', timing.get('prepMs', 0)):.0f} ms"
         + (" charged to B" if timing.get("timingMode") == "cold-start" else " before t0")),
        ("Budgets", f"K {budget.get('trialsK', '?')}",
         f"B {budget.get('deadlineBMs')} ms · H {budget.get('horizonHMs')} ms · bin {budget.get('binDeltaMs')} ms"),
        ("Counted trials",
         f"{sum(1 for t in episode.get('trials', []) if t.get('counted', True))}"
         f" of {len(episode.get('trials', []))}",
         "trial 0 and rejected proposals are charged but not counted"),
        ("Non-trial events", str(len(events)),
         ", ".join(f"{kind} x{count}" for kind, count in sorted(kinds.items())) or "none"),
        ("Boundaries", str(len(episode.get("boundaries") or [])),
         "; ".join(f"{b.get('kind')} at {b.get('at')}" for b in (episode.get("boundaries") or []))
         or "none declared"),
        ("Prepared T/C", "injected" if prepared else "formed by this sitting",
         f"T {str(prepared.get('tHash', ''))[:12]} C {str(prepared.get('cHash', ''))[:12]}"
         if prepared else ""),
        ("Ablation", str(condition.get("ablation", "none")),
         ", ".join(condition.get("methodsWithoutGrid", [])) or ""),
        ("Excluded", "yes" if excluded else "no",
         f"{excluded.get('rule', '')}: {excluded.get('reason', '')}" if excluded else ""),
        ("Reuse", str(cost.get("reuseCount", 0) or 0),
         f"prior prep {cost.get('priorPrepMs', 0) or 0} ms · group {cost.get('reuseGroup', '')}"),
        ("Schema", str(episode.get("schemaVersion", "")), str(episode.get("episodeId", ""))),
    ]
    return rows


def episode_rows(episode):
    """Project recorded observations; never infer a verdict from a KPI."""
    contract = episode.get("T") or {}
    targets = ([contract["t0"]] if contract.get("t0") else []) + list(contract.get("alternatives", []))
    controls = (episode.get("C") or {}).get("candidates", [])
    trials = episode.get("trials", [])
    cells = {}
    for trial in trials:
        for target_id, passed in trial.get("success", {}).items():
            cells[target_id, trial["controlId"]] = (
                "UNKNOWN" if not trial.get("window", {}).get("valid", False)
                else "PASS" if passed else "FAIL")
    best = episode.get("bestAttained") or {}
    dump = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)
    decisions = list(episode.get("calls", []))
    for trial in trials:
        if trial.get("decision"):
            decision = trial["decision"]
            decisions.append({**decision,
                "role": f"{decision.get('role', 'agent')} · trial {trial.get('trialIndex', '')}",
                "targetId": trial.get("proposedTargetId", decision.get("targetId", ""))})
    return {
        "targets": [(t["targetId"], dump(t.get("requirements", {})), dump(t.get("levels", {})), t.get("cost", 0)) for t in targets],
        "controls": [(c["controlId"], dump(c.get("functions", [])), c.get("predictedTarget", "unknown"), dump(c.get("configuration", {}))) for c in controls],
        "catalog": [(f.get("functionId"), f.get("xapp"), dump(f.get("scopes", [])),
                     dump(f.get("policyFields", {})), dump(f.get("prerequisites", [])))
                    for f in episode.get("functionCatalog", [])],
        "grid_columns": [c["controlId"] for c in controls],
        "grid": [(t["targetId"], *[
            cells.get((t["targetId"], c["controlId"]), "untried") +
            (" * best" if (t["targetId"], c["controlId"]) == (best.get("targetId"), best.get("controlId")) else "")
            for c in controls]) for t in targets],
        "trials": [(t.get("trialIndex"), t.get("proposedTargetId"), t.get("controlId"),
                    t.get("kernel", {}).get("terminalState", ""), dump(t.get("kpis", {})),
                    # agent-episode/1.3.0: what the metrics may count, and what a
                    # declared boundary made unusable for a later claim.
                    "counted" if t.get("counted", True) else "not counted",
                    t.get("beforeBoundary", "")) for t in trials],
        "record": _record_rows(episode),
        "decisions": [(c.get("role", ""), c.get("model", "deterministic"),
                       c.get("fallbackReason") or c.get("fallback") or "", c.get("rationale", ""), c.get("targetId", c.get("proposedTargetId", "")),
                       dump(c.get("options", {})), c.get("staleAtArrival", False))
                      for c in decisions],
    }


class AgentSittingPanel:
    def __init__(self, parent, *, sentence, on_run=None, on_stop=None, on_models=None, choices=None, intent_set_path=None):
        import tkinter as tk
        from tkinter import ttk
        style = ttk.Style(parent)
        for name in ("Intent.TEntry", "Intent.TCombobox"):
            style.map(name, foreground=[("invalid", "#b00020")],
                      fieldbackground=[("invalid", "#fff0f0")])
        self.frame = ttk.LabelFrame(parent, text="Agent sitting")
        self.sentence, self.on_run, self.on_stop, self.on_models = sentence, on_run, on_stop, on_models
        self.episode = {}
        self.choices = dict(choices or {})
        self.entries = []
        self.intent_set_path = intent_set_path
        self.running = False
        self.row_errors = []
        self.row_vars, self.row_widgets = [], []
        intent_set = ttk.LabelFrame(self.frame, text="Intent set")
        intent_set.pack(fill="x")
        count_bar = ttk.Frame(intent_set)
        count_bar.pack(fill="x")
        self.intent_count = tk.IntVar(value=1)
        ttk.Label(count_bar, text="Intent count").pack(side="left")
        ttk.Spinbox(count_bar, from_=1, to=8, width=4, textvariable=self.intent_count).pack(side="left")
        ttk.Button(count_bar, text="Create rows", command=self.create_rows).pack(side="left")
        self.row_tabs = ttk.Notebook(intent_set)
        self.row_tabs.pack(fill="x")
        self.entry_fields = {}
        entry_form = ttk.Frame(self.frame)
        entry_form.pack(fill="x")
        for column, key in enumerate(("steps", "bound", "priority", "weight")):
            ttk.Label(entry_form, text=key.title() + " (optional)").grid(row=0, column=column)
            self.entry_fields[key] = tk.StringVar()
            ttk.Entry(entry_form, textvariable=self.entry_fields[key], width=14).grid(row=1, column=column)
        self.intents = tk.Listbox(self.frame, height=3, exportselection=False)
        self.intents.pack(fill="x", padx=5)
        actions = ttk.Frame(self.frame)
        actions.pack(fill="x")
        self.add_button = ttk.Button(actions, text="Add current sentence", command=self.add_current)
        self.add_button.pack(side="left")
        ttk.Button(actions, text="Remove selected", command=self.remove_selected).pack(side="left")
        ttk.Button(actions, text="Clear", command=self.clear).pack(side="left")
        settings = ttk.Frame(self.frame)
        settings.pack(fill="x")
        self.method = tk.StringVar(value=self.choices.get("method", "three-agent"))
        ttk.Label(settings, text="Method").grid(row=0, column=0, sticky="w")
        method_combo = ttk.Combobox(settings, textvariable=self.method, values=METHODS, state="readonly")
        method_combo.grid(row=0, column=1, sticky="ew")
        method_combo.bind("<<ComboboxSelected>>", self._method_changed)
        self.models, self.combos = {}, {}
        for index, slot in enumerate(MODEL_SLOTS, 1):
            ttk.Label(settings, text="Monolith model" if slot == "monolith" else slot.title() + " agent").grid(row=index, column=0, sticky="w")
            self.models[slot] = tk.StringVar(value=self.choices.get(slot) or "deterministic")
            combo = ttk.Combobox(settings, textvariable=self.models[slot], values=("deterministic",), state="readonly")
            combo.grid(row=index, column=1, sticky="ew")
            combo.bind("<<ComboboxSelected>>", self._save)
            self.combos[slot] = combo
        settings.columnconfigure(1, weight=1)
        footer = ttk.Frame(self.frame)
        footer.pack(fill="x")
        self.budget = tk.IntVar(value=self.choices.get("settings", {}).get("trialsK", 16))
        self.run_button = ttk.Button(footer, text="Run sitting", command=self.run)
        self.run_button.pack(side="left", padx=5)
        self.missing_button = ttk.Button(footer, text="Complete missing fields", command=self.complete_missing)
        self.missing_button.pack(side="left")
        self.stop_button = ttk.Button(footer, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        self.status_text = tk.StringVar(value="Add intents, choose models, then Run sitting")
        ttk.Label(self.frame, textvariable=self.status_text, wraplength=600).pack(fill="x")
        notebook = ttk.Notebook(self.frame)
        notebook.pack(fill="both", expand=True)
        self._build_settings(notebook)
        self._build_axes(notebook)
        self.question_frame = ttk.LabelFrame(self.frame, text="Intake questions")
        self.question_frame.pack(fill="x")
        self.question_values = []
        self.tables = {}
        for key, title, columns in (
            ("targets", "T targets", ("Target", "Requirement values", "Levels", "Cost")),
            ("controls", "C controls", ("Control", "Functions / policy / scope", "Predicted target", "Configuration")),
            ("catalog", "Function catalog", ("Function", "xApp", "Scopes", "Policy fields", "Prerequisites")),
            ("grid", "Grid", ("Target",)),
            ("trials", "Trials", ("Index", "Target", "Control", "Kernel state", "KPIs",
                                  "Counted", "Before boundary")),
            ("decisions", "Decisions", ("Agent", "Model", "Fallback reason", "Rationale", "Target aim", "Options", "Stale at arrival")),
            ("record", "Run record", ("Fact", "Value", "Detail")),
        ):
            page = ttk.Frame(notebook)
            notebook.add(page, text=title)
            tree = ttk.Treeview(page, columns=columns, show="headings", height=7)
            tree.grid(row=0, column=0, sticky="nsew")
            for col in columns:
                tree.heading(col, text=col)
                tree.column(col, width=180, stretch=True)
            ttk.Scrollbar(page, orient="horizontal", command=tree.xview).grid(row=1, column=0, sticky="ew")
            ttk.Scrollbar(page, orient="vertical", command=tree.yview).grid(row=0, column=1, sticky="ns")
            tree.configure(xscrollcommand=page.grid_slaves(row=1)[0].set,
                           yscrollcommand=page.grid_slaves(column=1)[0].set)
            page.columnconfigure(0, weight=1)
            page.rowconfigure(0, weight=1)
            self.tables[key] = tree
        self._method_changed(save=False)
        try:
            self.entries = load_intent_set(self.intent_set_path)
            self._render_rows()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.status_text.set(f"Could not restore intent set: {exc}")

    def create_rows(self):
        try:
            count = self.intent_count.get()
            if not 1 <= count <= 8:
                raise ValueError("Intent count must be 1..8")
        except Exception as exc:
            self.status_text.set(str(exc))
            return
        self.entries = self.entries[:count]
        while len(self.entries) < count:
            used = {row.get("intentId") for row in self.entries}
            index = next(i for i in range(1, 9) if f"I{i}" not in used)
            self.entries.append({"intentId": f"I{index}"})
        self._render_rows()

    def _render_rows(self):
        import tkinter as tk
        from tkinter import ttk
        for child in self.row_tabs.winfo_children():
            child.destroy()
        self.row_vars, self.row_widgets = [], []
        for index, record in enumerate(self.entries):
            page = ttk.Frame(self.row_tabs)
            self.row_tabs.add(page, text=record.get("intentId", f"I{index + 1}"))
            values = intent_fields(record)
            variables, widgets = {}, {}
            for col, key in enumerate(INTENT_FIELDS):
                row, column = divmod(col, 4)
                ttk.Label(page, text=key + (" (0 = non-relaxable)" if key == "steps" else "")).grid(row=row*2, column=column, sticky="w")
                var = tk.StringVar(value=values.get(key, ""))
                variables[key] = var
                options = {"kpi": ("dlGoodputMbps", "servingCell"), "op": (">=", "<=", "=="), "unit": ("Mbps", "nci")}
                widget = (ttk.Combobox(page, textvariable=var, values=options[key], width=14, state="readonly", style="Intent.TCombobox")
                          if key in options else ttk.Entry(page, textvariable=var, width=16, style="Intent.TEntry"))
                widget.grid(row=row*2+1, column=column, sticky="ew")
                widgets[key] = widget
            self.row_vars.append(variables)
            self.row_widgets.append(widgets)
            for key, var in variables.items():
                var.trace_add("write", lambda *_, i=index, k=key: self._row_edited(i, k))
        if self.entries:
            self.intent_count.set(len(self.entries))
        self._validate_rows()

    def _row_edited(self, index, key):
        variables = self.row_vars[index]
        if key == "kpi":
            cell = variables["kpi"].get() == "servingCell"
            variables["unit"].set("nci" if cell else "Mbps")
            if cell:
                variables["steps"].set("0")
                variables["op"].set("==")
        self._validate_rows()

    def _validate_rows(self):
        errors = []
        self.intents.delete(0, "end")
        for index, variables in enumerate(self.row_vars):
            fields = {key: var.get() for key, var in variables.items()}
            record, missing = fields_record(fields, index + 1, self.entries[index])
            self.entries[index] = record
            for key, widget in self.row_widgets[index].items():
                widget.state(["invalid" if key in missing else "!invalid"])
            self.row_widgets[index]["bound"].configure(state="disabled" if fields["steps"] == "0" or fields["kpi"] == "servingCell" else "normal")
            errors.extend(f"{record['intentId']}: {key}" for key in missing)
            req = record["requirement"]
            self.intents.insert("end", f"{record['intentId']} · {record.get('ueId', '')} · {req.get('kpi', '')} steps={req.get('steps', 'missing')} bound={req.get('bound', 'missing')}")
        self.row_errors = errors
        self.run_button.configure(state="disabled" if errors or not self.entries or self.running else "normal")
        self.status_text.set("Missing or invalid: " + "; ".join(errors) if errors else "Intent set ready" if self.entries else "Choose an intent count and Create rows, or add a sentence")
        if self.intent_set_path:
            try:
                save_intent_set(self.intent_set_path, self.entries)
            except OSError as exc:
                self.status_text.set(f"Could not save intent set: {exc}")

    def complete_missing(self):
        questions = []
        for index, variables in enumerate(self.row_vars):
            _, missing = fields_record({key: var.get() for key, var in variables.items()}, index + 1, self.entries[index])
            questions.extend({"intentId": self.entries[index]["intentId"], "field": field,
                              "question": f"Enter {field} for this intent"} for field in missing)
        self.show_questions({"questions": questions, "requestPayload": self.payload(), "clarificationRound": 0})

    def add_current(self):
        from tools.liveconsole import LiveConsoleError
        value = self.sentence().strip()
        if not value:
            return
        empty_index = next((i for i, row in enumerate(self.entries) if not row.get("ueId") and not row.get("sentence")), None)
        if len(self.entries) >= 8 and empty_index is None:
            self.status_text.set("Intent count must be 1..8")
            return
        used = {row.get("intentId") for i, row in enumerate(self.entries) if i != empty_index}
        next_index = next(i for i in range(1, 9) if f"I{i}" not in used)
        try:
            record, _ = intent_entry(value, next_index,
                                    {key: var.get() for key, var in self.entry_fields.items()})
        except (ValueError, LiveConsoleError):
            record = {"intentId": f"I{next_index}", "sentence": value}
        record["_fromSentence"] = True
        if empty_index is None:
            self.entries.append(record)
        else:
            self.entries[empty_index] = record
        self._render_rows()

    def remove_selected(self):
        for index in reversed(self.intents.curselection()):
            del self.entries[index]
        self._render_rows()

    def clear(self):
        self.entries.clear()
        self._render_rows()

    def _build_settings(self, notebook):
        import tkinter as tk
        from tkinter import ttk
        from assurance.coordination.intake import GENERATION_DEFAULTS
        page = ttk.Frame(notebook)
        notebook.add(page, text="Sitting settings")
        ttk.Label(page, text="Trials K (per sitting)").pack(anchor="w")
        ttk.Spinbox(page, from_=1, to=1000000, textvariable=self.budget, width=7).pack(anchor="w")
        tabs = ttk.Notebook(page)
        tabs.pack(fill="both", expand=True)
        self.setting_vars = {}
        saved = self.choices.get("settings", {})
        # ``retain`` blank means the executor's own default, four times K
        # (contract v2/v3): a number here is a deliberate narrowing.
        groups = {"General": {"timingMode": "prepared", "deadlineMs": None, "horizonMs": None,
                  "stopAfterRelaxedSuccess": False, "retention": "bestAttained", "retain": None,
                  "unselectedFunctionRule": "baseline"}}
        for kpi in ("dlGoodputMbps", "servingCell"):
            groups[kpi] = {"settleMs": 1500, "windowMs": 1500,
                           "statistic": "mean" if kpi == "dlGoodputMbps" else "last",
                           "minCoverage": 0.5, "validityMs": 60000 if kpi == "dlGoodputMbps" else 10000}
        for slot, defaults in GENERATION_DEFAULTS.items():
            if slot != "default":
                groups[slot] = defaults
        for group, defaults in groups.items():
            pane = ttk.Frame(tabs)
            tabs.add(pane, text=group)
            self.setting_vars[group] = {}
            previous = saved if group == "General" else saved.get(
                "observation" if group in ("dlGoodputMbps", "servingCell") else "generation", {}).get(group, {})
            for row, (key, default) in enumerate(defaults.items()):
                value = previous.get(key, default)
                var = tk.StringVar(value=json.dumps(value) if not isinstance(value, str) else value)
                self.setting_vars[group][key] = var
                ttk.Label(pane, text=key).grid(row=row, column=0, sticky="w")
                options = {"statistic": ("mean", "last", "min", "max"),
                           "retention": ("bestAttained", "none"),
                           "timingMode": ("prepared", "cold-start"),
                           "unselectedFunctionRule": ("baseline", "keep-current"),
                           "reasoningEffort": ("low", "medium", "high"),
                           "stopAfterRelaxedSuccess": ("true", "false"), "jsonMode": ("true", "false")}
                if key in options:
                    ttk.Combobox(pane, textvariable=var, values=options[key], state="readonly").grid(row=row, column=1)
                else:
                    ttk.Entry(pane, textvariable=var).grid(row=row, column=1)
        ttk.Button(page, text="Save sitting settings", command=self._save).pack(anchor="w")

    def _build_axes(self, notebook):
        import tkinter as tk
        from tkinter import ttk
        from tools.liveconsole.agent import AXIS_KINDS, DEFAULT_AXIS_KINDS, DEFAULT_LADDERS, DEFAULT_MAX_CATALOG_CARDINALITY
        page = ttk.Frame(notebook)
        notebook.add(page, text="Axes and ladders")
        saved = self.choices.get("settings", {}).get("axisExposure", {})
        selected = saved.get("axes", DEFAULT_AXIS_KINDS)
        self.axis_vars, self.ladder_vars = {}, {}
        ttk.Label(page, text="Each kind covers every intent UE, advertised cell, or deployment slice.\nOverrides: scope:value,value; scope:value,value (blank uses the displayed default).", wraplength=650).grid(row=0, column=0, columnspan=3, sticky="w")
        for row, kind in enumerate(AXIS_KINDS, 1):
            var = tk.BooleanVar(value=kind in selected or "all" in selected)
            self.axis_vars[kind] = var
            ttk.Checkbutton(page, text=kind, variable=var,
                            state="disabled" if kind == "servingCell" else "normal").grid(row=row, column=0, sticky="w")
            if kind in LADDER_FIELDS:
                overrides = saved.get(LADDER_FIELDS[kind], {})
                text = "; ".join(f"{scope}:" + ",".join(map(str, values)) for scope, values in overrides.items())
                self.ladder_vars[kind] = tk.StringVar(value=text)
                ttk.Entry(page, textvariable=self.ladder_vars[kind], width=42).grid(row=row, column=1, sticky="ew")
                ttk.Label(page, text=", ".join(map(str, DEFAULT_LADDERS[kind]))).grid(row=row, column=2, sticky="w")
        self.catalog_ceiling = tk.StringVar(value=str(saved.get("max_catalog_cardinality", DEFAULT_MAX_CATALOG_CARDINALITY)))
        ttk.Label(page, text="maxCatalogCardinality").grid(row=7, column=0, sticky="w")
        ttk.Entry(page, textvariable=self.catalog_ceiling).grid(row=7, column=1, sticky="w")
        self.cardinality = tk.StringVar(value=cardinality_text({}))
        ttk.Label(page, textvariable=self.cardinality, wraplength=650).grid(row=8, column=0, columnspan=3, sticky="w")
        ttk.Button(page, text="Save axes and ladders", command=self._save).grid(row=9, column=0)
        page.columnconfigure(1, weight=1)

    def sitting_settings(self):
        settings = {"trialsK": self.budget.get(), "observation": {}, "generation": {}}
        for group, fields in self.setting_vars.items():
            values = {}
            for key, var in fields.items():
                text = var.get().strip()
                try:
                    values[key] = json.loads(text) if text else None
                except ValueError:
                    values[key] = text
            if group == "General":
                settings.update(values)
            else:
                settings["observation" if group in ("dlGoodputMbps", "servingCell") else "generation"][group] = values
        settings["axisExposure"] = axis_settings(
            [kind for kind, var in self.axis_vars.items() if var.get()],
            {kind: var.get() for kind, var in self.ladder_vars.items()}, self.catalog_ceiling.get())
        return settings

    def show_questions(self, record):
        import tkinter as tk
        from tkinter import ttk
        for child in self.question_frame.winfo_children():
            child.destroy()
        self.question_values = []
        self.question_record = record
        for row, question in enumerate(record.get("questions", [])):
            for col, key in enumerate(("intentId", "field", "question")):
                ttk.Label(self.question_frame, text=question.get(key, ""), wraplength=350).grid(row=row, column=col, sticky="w")
            var = tk.StringVar()
            self.question_values.append(var)
            ttk.Entry(self.question_frame, textvariable=var).grid(row=row, column=3)
        if self.question_values:
            self.answer_button = ttk.Button(self.question_frame, text="Answer and continue",
                                           command=self.answer_and_continue,
                                           state="disabled" if record.get("refused") else "normal")
            self.answer_button.grid(row=len(self.question_values), column=0, columnspan=4)

    def answer_and_continue(self):
        record = self.question_record
        if record.get("refused"):
            return
        payload = answer_payload(record["requestPayload"], record["questions"],
                                 [var.get() for var in self.question_values], record.get("clarificationRound", 0))
        for question, var in zip(record["questions"], self.question_values):
            if not var.get().strip():
                continue
            for index, entry in enumerate(self.entries):
                if entry["intentId"] == question["intentId"] and question["field"] in self.row_vars[index]:
                    self.row_vars[index][question["field"]].set(var.get())
        payload["intents"] = self.payload()["intents"]
        if self.on_run:
            self.on_run(payload)

    def selected_models(self):
        return {slot: None if var.get() == "deterministic" else var.get() for slot, var in self.models.items()}

    def _save(self, event=None):
        if self.on_models:
            try:
                self.on_models({**self.selected_models(), "method": self.method.get(),
                                "settings": self.sitting_settings()})
            except Exception as exc:
                self.status_text.set(str(exc))

    def _method_changed(self, event=None, *, save=True):
        for slot, combo in self.combos.items():
            active = (self.method.get() == "three-agent" and slot != "monolith" or
                      self.method.get() in ("internal-monolith", "basic-monolith") and slot == "monolith")
            combo.configure(state="readonly" if active else "disabled")
        if save:
            self._save()

    def set_backends(self, names):
        for combo in self.combos.values():
            combo.configure(values=tuple(dict.fromkeys(("deterministic",) + tuple(names))))

    def payload(self):
        return {"intents": [{k: v for k, v in row.items() if k != "_fromSentence"} for row in self.entries],
                "sentences": [row["sentence"] for row in self.entries if row.get("_fromSentence") and row.get("sentence")], "settings": self.sitting_settings(), "method": self.method.get(),
                "roleModels": self.selected_models(), "budgetTrials": self.budget.get()}

    def run(self):
        if self.row_errors or not self.intents.size():
            self.status_text.set("Add at least one intent")
            return
        try:
            payload = self.payload()
            if payload["budgetTrials"] < 1:
                raise ValueError("Budget K must be positive")
        except Exception as exc:
            self.status_text.set(str(exc))
            return
        if self.on_run:
            self.on_run(payload)

    def stop(self):
        if self.on_stop:
            self.on_stop()

    def set_record(self, record):
        if record.get("kind") != "agent-sitting":
            return
        self.show_questions(record)
        self.episode = record.get("episode") or {}
        if record.get("functionCatalog"):
            self.episode = {**self.episode, "functionCatalog": record["functionCatalog"]}
        self.cardinality.set(cardinality_text(self.episode))
        self.status_text.set(record.get("status", ""))
        running = record.get("running", False)
        self.running = running
        self.run_button.configure(state="disabled" if running or self.row_errors or not self.entries else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        rows = episode_rows(self.episode)
        grid = self.tables["grid"]
        columns = ("Target", *rows["grid_columns"])
        grid.configure(columns=columns)
        for col in columns:
            grid.heading(col, text=col)
            grid.column(col, width=140)
        for key, tree in self.tables.items():
            tree.delete(*tree.get_children())
            for row in rows[key]:
                tree.insert("", "end", values=row)
