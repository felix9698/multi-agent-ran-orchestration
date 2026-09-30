"""v5.2 (``AIC_V52=1``): the v5.2c prompt pack and the LLM-input view it asks for.

Source: ``.orca/drops/AI_RAN_v52_compact_prompts_20260927 copy.md`` (system texts verbatim) and
the owner-relayed review of 2026-09-27 15:1x (catalog without UE1-specific ids or baselines,
supported resolution, a contention sentence for every control-building call, target
trade-offs computed by code, input de-duplication).

Only what a model *reads* changes.  Records, validators and the evaluator keep the original
objects: this module rewrites the payload inside ``_user_prompt`` and picks the system text in
``_decide``, the single choke point every call of the three methods passes through.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

KEYS = ("I4e.r1", "I1g.r1", "I2d.r1", "I3g.r1")
WEIGHTS = {"I4e.r1": 25, "I1g.r1": 13, "I2d.r1": 7, "I3g.r1": 3}
FORMULA = ("p = 25 kE + 13 k1 + 7 k2 + 3 k3 over I4e.r1, I1g.r1, I2d.r1, I3g.r1; lower is better; "
           "the same p scores targets and observations.")
LEVELS = ("Each relaxable requirement has integer levels k = 0..20 evenly spaced from its original "
          "(k=0) to its authorized limit (k=20); larger k is looser. The protected requirement I2g.r1 "
          "and the fixed UE2 deadline do not change: set every deadlineLevels entry to 0.")
CELL_IDS = {"gnb1": "12345678", "gnb2": "87654321"}


def contention_cell() -> str:
    """The cell ue2 and ue3 share (placement B: gnb1; owner 2026-09-28 option B: gnb2, set by
    AIC_CONTENTION_CELL after ue3 lost its gnb1 downlink)."""
    return "gnb2" if os.environ.get("AIC_CONTENTION_CELL", "").strip() == "gnb2" else "gnb1"


def energy_cell_id() -> str:
    """The energy requirement's cell, by the rule make_v47_corpus.energy_cell uses."""
    return CELL_IDS["gnb1" if os.environ.get("AIC_V51_ENERGY_CELL", "").strip() == "gnb1" else "gnb2"]


def resources() -> str:
    shared = contention_cell()
    lone = "gnb1" if shared == "gnb2" else "gnb2"
    return (f"ue2 and ue3 share {shared} (cell {CELL_IDS[shared]}) and compete for its PRBs; ue1 is alone on "
            f"{lone} (cell {CELL_IDS[lone]}). PF weights set relative shares within one cell: scaling every "
            "competing UE on a cell by the same factor leaves their shares unchanged. A PRB cap limits one UE "
            f"and frees PRBs for the others on its cell. {shared} TX attenuation lowers {shared} downlink quality "
            "for both ue2 and ue3. Per-UE reference values are in C0.")


RESOURCES = resources()
CONTENTION = "Justify moving a UE into greater contention using supplied radio or load evidence."

HEAD = ("Follow the supplied authorization and preference. Preserve protected requirements, hard floors, "
        "bounds, and compatibility. Treat missing or invalid measurements as unknown. Return only the "
        "supplied JSON schema; each reason or rationale must be at most 20 words.")
T_BODY = (
    "Return exactly eight distinct alternatives, excluding T0. Use integer levels 0..20; 0 is original and "
    "20 is maximum concession. Keep protected levels and deadlines fixed.\n\n"
    "If measurementSupportedTarget is non-null, include its levels once. Propose seven further targets "
    "with strictly lower p through different tradeoffs and improvement sizes. These are untested goals: "
    "current measurements need not satisfy them. Do not stop because only the reference target is already "
    "satisfied.\n\n"
    "If the reference is null, propose eight authorized untested tradeoffs without claiming measured "
    "support. Authorization permits a proposal; it does not establish attainment. Use the supplied p "
    "formula and evidenceRefs=[].")
C_BODY = (
    "Return exactly ten distinct compatible candidates, excluding C0.\n\n"
    "Choose two modest nonzero changes near C0 for a control that advances a high-weight requirement but "
    "may harm services. At each value include a single-action probe and a combination retaining that value "
    "with a compensating action. These four candidates form two matched pairs.\n\n"
    "Compensation must change relative resource allocation or contention, using relative weights, caps, or "
    "placement. Equal scaling of all competing PF weights does not establish protection. Name the intended "
    "beneficiary and the service that may lose resources. " + CONTENTION + " Use the remaining six slots for "
    "other compatible combinations and useful probes. Avoid scheduler-only changes on isolated users.\n\n"
    "Express every candidate as complete changes from C0; omitted settings reset to C0. Respect supported "
    "resolution and the changed-entry limit. Report relatedKpis and supplied evidenceRefs or []. Mark "
    "unmeasured effects as hypotheses; do not predict numeric KPIs.")
SELECT = (
    HEAD + "\n\n"
    "Select one applicable control from fixed C that has not been tried in the history. Use measured "
    "outcomes to improve the best feasible preference over the full authorized domain. If none is feasible, "
    "first seek a configuration meeting all floors.\n\n"
    "Prioritize valid measured effects over candidate rationales. Compare complete configurations relative "
    "to C0; omitted settings revert to C0. Choose an informative trial only when its information can help "
    "within the remaining budget.\n\n"
    "Return controlId, an intended targetId from T, and rationale. The targetId describes the purpose; it "
    "does not restrict evaluation to T.")
SYSTEM = {
    "target": HEAD + "\n\n" + T_BODY,
    "control": HEAD + "\n\n" + C_BODY,
    "trajectory": SELECT,
    "monolith-form": (HEAD + "\n\nJointly construct the two fixed sets using the same supplied information.\n\n"
                      + T_BODY + "\n\n" + C_BODY + "\n\nReturn alternatives, candidates, and one overall rationale."),
    "monolith-select": SELECT,
    "basic-monolith": (
        HEAD + "\n\n"
        "Choose one untried compatible configuration to improve the best feasible preference over the full "
        "authorized domain. If none is feasible, first seek a configuration meeting all floors.\n\n"
        "Identify shared resources. Consider modest changes and combinations that protect services harmed by "
        "another action. Use valid measured effects before qualitative expectations. Use informative trials "
        "when useful within the remaining budget.\n\n"
        "Compensation must change relative resource allocation or contention. Equal scaling of all competing "
        "PF weights does not establish protection. Name the intended beneficiary and the service that may lose "
        "resources. " + CONTENTION + " Avoid scheduler-only changes on isolated users.\n\n"
        "Express the complete configuration as instructions changing C0. Omitted settings revert to C0, not "
        "the held configuration. Explicitly repeat settings you intend to preserve. Respect the changed-entry "
        "limit and tried configurations.\n\n"
        "Return instructions and rationale."),
}


def enabled() -> bool:
    return os.environ.get("AIC_V52", "").strip() == "1"


def kind(role: str, phase: str, payload: Mapping[str, Any]) -> str:
    """Which v5.2 system text a call gets."""
    if role in ("target", "control", "trajectory"):
        return role
    if phase == "formation" or phase == "clarification":
        return "monolith-form"
    return "basic-monolith" if "input.tried_configurations" in payload else "monolith-select"


# ----------------------------------------------------------------------------- reference

def _med(values):
    v = sorted(float(x) for x in values)
    return v[len(v) // 2] if v else None


def reference_measurement(c0: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The block reference (configuration A = C0, every UE loaded), measured before the board."""
    path = os.environ.get("AIC_V52_REFERENCE", "").strip()
    if not path:
        return None
    try:
        d = json.loads(Path(path).read_text())
        A = d["raw"]["A"]
        kpis = {f"dlGoodputMbps@{ue}": _med(A[ue]) for ue in ("ue1", "ue2", "ue3") if A.get(ue)}
        if d.get("ue2DeadlineMs") is not None and d.get("ue2SuccessAtDInA") is not None:
            kpis["deadlineSuccessRatio@ue2"] = {"byDeadlineMs": {str(d["ue2DeadlineMs"]): d["ue2SuccessAtDInA"]}}
        att = d.get("energyBaselineDb")
        if att is not None:
            kpis[f"cellTxAttenuationDb@{energy_cell_id()}"] = float(att)
        return {"source": "block reference, configuration A (C0, every UE offered its load), measured before the board",
                "measuredAt": d.get("at"), "configuration": dict(c0 or {}), "valid": bool(d.get("reference")),
                "kpis": kpis, "attenuationReadbackDb": att}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _kpi(kpis, req):
    v = kpis.get(f"{req['kpi']}@{str(req['scope']).split('@', 1)[1]}")
    return next(iter(v["byDeadlineMs"].values()), None) if isinstance(v, dict) else v


def supported_target(requirements: Mapping[str, Any], meas: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The evaluator's best vector for the reference window: per requirement the strictest level it
    meets; null when invalid, a protected floor fails, or a requirement meets no authorized level."""
    if not meas or not meas.get("valid"):
        return None
    kpis = meas["kpis"]; lv = {}
    for rid, r in requirements.items():
        # The energy requirement names its own cell (gnb1 or gnb2): the readback is that cell's.
        x = (meas.get("attenuationReadbackDb") if r.get("kpi") == "cellTxAttenuationDb" else _kpi(kpis, r))
        ok = lambda want: x is not None and (x >= want if r.get("op") == ">=" else x <= want)
        if rid not in KEYS:
            if not ok(float(r.get("original", r.get("value", 0)))):
                return None
            continue
        levels = r.get("levels") or []
        hits = [k for k, want in enumerate(levels) if ok(float(want))]
        if not hits:
            return None
        lv[rid] = min(hits)
    lv_full = dict(lv, **{rid: 0 for rid in requirements if rid not in KEYS})
    return {"levels": lv_full, "p": sum(WEIGHTS[k] * lv[k] for k in KEYS)}


# ----------------------------------------------------------------------------- views

def _compact_requirements(reqs: Mapping[str, Any]) -> Dict[str, Any]:
    out = {}
    for rid, r in reqs.items():
        keep = {k: r[k] for k in ("kpi", "scope", "unit", "op", "original", "limit", "steps", "owner",
                                  "deadlineMs", "deadlineSteps", "deadlineBound") if k in r}   # (Codex) the fixed deadline stays
        keep["protected"] = rid not in KEYS
        out[rid] = keep
    return out


def _catalog(rows):
    out = []
    for f in rows or []:
        f = dict(f)
        f.pop("actionId", None)
        fields = {}
        for name, spec in dict(f.get("policyFields") or {}).items():
            spec = {k: v for k, v in dict(spec).items() if k != "baseline"}
            fields[name] = spec
        f["policyFields"] = fields
        out.append(f)
    return out


def _changes(cfg: Mapping[str, Any], c0: Mapping[str, Any]) -> Dict[str, str]:
    out = {}
    for k, v in dict(cfg or {}).items():
        a, b = str(v), str(c0.get(k))
        try:
            if float(a) == float(b):
                continue
        except (TypeError, ValueError):
            pass
        if a != b:
            out[k] = a
    return out


_FIELD_AXIS = {"servingCell": "servingCell", "maxDlPrbs": "dlPrbCap", "pfWeight": "pfWeight",
               "txAttenuationDb": "txAttenuationDb"}


def _settings(row: Mapping[str, Any]) -> Dict[str, str]:
    """A candidate's applied settings, from ``configuration`` or its ``functions`` rows."""
    if isinstance(row.get("configuration"), Mapping):
        return {k: str(v) for k, v in row["configuration"].items()}
    out = {}
    for f in row.get("functions") or []:
        scope = str(f.get("scope", "")).split("@", 1)[-1]
        for name, value in dict(f.get("policy") or {}).items():
            out[f"{_FIELD_AXIS.get(name, name)}@{scope}"] = str(value)
    return out


def _tradeoff(levels: Mapping[str, Any], ref: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    base = (ref or {}).get("levels") or {k: 0 for k in KEYS}
    tight = [k for k in KEYS if int(levels.get(k, 0)) < int(base.get(k, 0))]
    loose = [k for k in KEYS if int(levels.get(k, 0)) > int(base.get(k, 0))]
    return {"vs": "measurementSupportedTarget" if ref else "T0", "tightened": tight, "relaxed": loose}


def _p(levels: Mapping[str, Any]) -> int:
    return sum(WEIGHTS[k] * int(levels.get(k, 0)) for k in KEYS)


def view(kind_: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The payload a v5.2 model reads for one call."""
    out = dict(payload)
    state = dict(out.get("input.network_state") or {})
    c0 = dict(state.get("appliedConfiguration") or {})
    auth = out.get("input.authorization")
    reqs = dict((auth or {}).get("requirements") or {}) if isinstance(auth, Mapping) else {}
    tc = out.get("input.target_contract")
    if not reqs and isinstance(tc, Mapping):
        reqs = dict(tc.get("authorization") or {})
    meas = reference_measurement(c0)
    ref = supported_target(reqs, meas) if reqs else None

    out.pop("input.intents", None)                      # duplicated by the authorization
    if isinstance(auth, Mapping):
        a = {k: v for k, v in auth.items() if k not in ("preference",)}
        a["requirements"] = _compact_requirements(reqs)
        out["input.authorization"] = a
    out["input.level_semantics"] = LEVELS
    out["input.preference_formula"] = FORMULA
    if "input.function_catalog" in out:
        out["input.function_catalog"] = _catalog(out["input.function_catalog"])
        out["input.resource_notes"] = resources()
    if kind_ in ("target", "control", "monolith-form", "basic-monolith"):
        out["input.initial_measurement"] = meas
        out["input.measurementSupportedTarget"] = ref
    if "input.construction_policy" in out:
        out["input.construction_policy"] = {"additionalCandidates": 10, "note": "code adds C0; return ten others"}
    if isinstance(tc, Mapping):
        alts = []
        for t in [tc.get("t0")] + list(tc.get("alternatives") or []):
            if not t:
                continue
            lv = {k: int(v) for k, v in dict(t.get("levels") or {}).items()}
            alts.append({"targetId": t.get("targetId"), "levels": lv, "p": _p(lv),
                         "tradeoff": _tradeoff(lv, ref)})
        out["input.target_contract"] = {
            "targets": alts, "requirements": _compact_requirements(reqs),
            "deadlinesMs": dict((tc.get("t0") or {}).get("deadlines") or {}),
            "measurementSupportedTarget": ref}
    rows = out.get("input.control_candidates")
    if isinstance(rows, list):
        out["input.control_candidates"] = [
            {"controlId": r.get("controlId"), "changesFromC0": _changes(_settings(r), c0),
             "relatedKpis": r.get("relatedKpis"), "rationale": r.get("rationale"),
             "tried": bool((r.get("status") or {}).get("tried")),
             **({"priorOutcome": (r.get("status") or {}).get("priorOutcome")}
                if (r.get("status") or {}).get("priorOutcome") else {})}
            for r in rows]
    obs = out.get("input.observations")
    if isinstance(obs, list):
        out["input.observations"] = [
            {k: v for k, v in dict(o).items() if k not in ("verdicts", "configuration", "validUntil", "windowEnd", "expired")}
            | {"changesFromC0": _changes(o.get("configuration"), c0)} for o in obs]
    if isinstance(out.get("input.tried_configurations"), list):
        out["input.tried_configurations"] = [_changes(c, c0) for c in out["input.tried_configurations"]]
    gaps = out.get("input.kpi_gaps")
    if isinstance(gaps, Mapping):
        per = {rid: {k: g.get(k) for k in ("observed", "unit", "op")} | {"againstT0": g.get("againstT0")}
               for rid, g in dict(gaps.get("perRequirement") or {}).items() if isinstance(g, Mapping)}
        out["input.kpi_gaps"] = {"perRequirement": per}
    ev = out.get("input.effect_evidence")
    if isinstance(ev, Mapping) and isinstance(ev.get("observations"), list):
        out["input.effect_evidence"] = {"observations": [
            {k: v for k, v in dict(o).items() if k not in ("verdicts", "configuration")}
            | {"changesFromC0": _changes(o.get("configuration"), c0)} for o in ev["observations"]]}
    return view53(kind_, payload, out) if v53() else out


def step_of(numbers) -> Optional[float]:
    """The supported resolution of a numeric ladder (smallest positive gap)."""
    xs = sorted(set(float(x) for x in numbers))
    gaps = [round(b - a, 6) for a, b in zip(xs, xs[1:]) if b > a]
    return min(gaps) if gaps else None


# ----------------------------------------------------------------------------- v5.3

# v5.3 (``AIC_V53=1`` on top of ``AIC_V52=1``): `.orca/drops/AI_RAN_v53_trial_efficiency_runbook_20260928.md`.
# The role texts below replace the v5.2c ones (sections 3); the view adds the code-computed energy
# arithmetic of section 2 (items 2-4) and sorts/scores every target by the one weighted p.
V53_COMMON = (
    "Follow the supplied authorization, compatibility and preference. Preserve protected requirements, "
    "deadlines and hard floors. Missing or invalid measurements are unknown. Distinguish measured effects "
    "from hypotheses; do not invent numeric service outcomes. Return only the supplied JSON schema. Each "
    "reason or rationale must be at most 20 words.")
V53_TARGET = (
    "Construct exactly eight distinct authorized alternatives, excluding T0. Adjustable levels are integers "
    "0..20: 0 is original; 20 is maximum concession. Keep protected levels fixed. Use the supplied weighted p.\n\n"
    "If measurementSupportedTarget is non-null and differs from T0, include it once. Use the remaining slots "
    "primarily for lower-p targets with different improvement sizes and energy-service tradeoffs. If "
    "insufficient lower-p alternatives exist, fill with distinct authorized alternatives.\n\n"
    "If it is null, include the maximum-concession target and seven untested tradeoffs spanning near-floor, "
    "intermediate and demanding requirements. Include service-preserving targets that concede energy and "
    "energy-improving targets that concede services. Do not cluster around T0. Authorization does not "
    "establish attainment. Return evidenceRefs=[].\n\n"
    "If the reference equals T0, exclude T0 and construct eight distinct authorized alternatives.")
#: 2026-09-30 (owner): AIC_ENERGY_STEPS=4 offers one to four energy steps and asks C to span them; unset
#: keeps v5.3's two steps (so a running block is not changed in the middle).
_ENERGY_SLOTS = {
    2: ("Use the two supplied energyStepValues to form four candidates: each energy change alone and the same "
        "change with service compensation. Compensation must change relative resource allocation or contention. "
        "Equal PF scaling of all competitors is not sufficient.\n\n"),
    4: ("Use the supplied energyStepValues (one to four energy improvement steps over C0) to form four candidates "
        "that cover one, two, three and four steps; pair the three- and four-step changes with service "
        "compensation. Compensation must change relative resource allocation or contention. "
        "Equal PF scaling of all competitors is not sufficient.\n\n"),
}


def energy_steps() -> int:
    return 4 if os.environ.get("AIC_ENERGY_STEPS", "").strip() == "4" else 2


V53_CONTROL_TEMPLATE = (
    "Construct exactly ten distinct compatible candidates, excluding C0, using intents, authorization, "
    "functions and evidence. Do not use generated targets.\n\n"
    "<<ENERGY_SLOTS>>"
    "Use two slots to address floor failures or the most vulnerable services without an energy increase. Use "
    "four slots for other distinct joint configurations with credible weighted benefit. Cover different "
    "tradeoffs rather than minor variants. If energyStepValues are unavailable or incompatible, use those "
    "slots for distinct admissible alternatives and explain briefly.\n\n"
    "A candidate is a complete configuration relative to C0. Omitted settings reset to C0. Respect actual "
    "resolution and the changed-entry limit. Avoid scheduler-only changes on isolated UEs. Name the "
    "beneficiary and potentially harmed service. Keep unmeasured effects hypothetical and report relatedKpis "
    "and supplied evidenceRefs or [].")
V53_CONTROL = V53_CONTROL_TEMPLATE.replace("<<ENERGY_SLOTS>>", _ENERGY_SLOTS[energy_steps()])
V53_POLICY = (
    "Use valid measurements to improve the best feasible weighted preference within the remaining trials. If "
    "no observation meets all floors, prioritize repairing the dominant floor failure while protecting the "
    "others. Otherwise seek a credible weighted improvement without crossing any floor.\n\n"
    "Judge the complete resulting configuration, including serving cells and attenuation. Omitted settings "
    "reset to C0, not the held state. Consider an action together with compensation for its service losses. "
    "Do not favor energy improvement without considering the other weighted requirements.\n\n"
    "Prefer direct improvement when supported. Use a diagnostic trial only if its outcome can change a later "
    "choice within the remaining budget. Reuse evidence from similar tested configurations; do not claim an "
    "unmeasured interaction is proven.")
V53_SELECT = (
    "Choose one applicable, untried control from fixed C. Compare measured outcomes and complete candidate "
    "configurations; candidate rationales are hypotheses. Return controlId, an intended targetId from T, and "
    "rationale. The target describes the purpose; evaluation uses the full authorized domain.")
V53_IM_OUTPUT = (
    "Perform both construction tasks in this single call. Construct controls from the supplied intents, "
    "authorization, functions and evidence, without conditioning on the generated targets. Return "
    "alternatives, candidates and one overall rationale.")
V53_BM = (
    "Propose one untried compatible configuration from the same supported action space. Use energyStepValues "
    "when relevant and consider compensating combinations, not only single changes. Avoid scheduler-only "
    "changes on isolated UEs. Equal PF scaling of all competitors does not establish protection.\n\n"
    "Return instructions relative to C0 and rationale. Explicitly include every nonbaseline setting you intend "
    "to retain. Respect actual resolution and the changed-entry limit. Identify the intended beneficiary and "
    "potentially harmed service without inventing numeric outcomes.")
#: 2026-09-30 (owner "공정하게"): the same energy sentence for the C-based methods and the basic monolith,
#: and no fixed 4-2-4 slot quota in Control -- a prompt-only replay showed the quota made Control use only
#: two energy values even when four were supplied (old text + four steps: 6 % of candidates above 9.5 dB;
#: fair text: 26 %).  On under AIC_FAIR_PROMPTS=1 so a running block is not changed.
FAIR_ENERGY = ("energyStepValues give the attenuations that earn the first energy improvement steps; any "
               "supported attenuation may be used. Consider compensating combinations, not only single changes. "
               "Compensation must change relative resource allocation or contention. Equal PF scaling of all "
               "competitors does not establish protection.")
V53_CONTROL_FAIR = (
    "Construct exactly ten distinct compatible candidates, excluding C0, using intents, authorization, "
    "functions and evidence. Do not use generated targets.\n\n"
    "Cover different tradeoffs rather than minor variants: energy improvements of different depths, repairs of "
    "floor failures or the most vulnerable services without an energy increase, and joint configurations with "
    "credible weighted benefit. " + FAIR_ENERGY + "\n\n"
    "A candidate is a complete configuration relative to C0. Omitted settings reset to C0. Respect actual "
    "resolution and the changed-entry limit. Avoid scheduler-only changes on isolated UEs. Name the "
    "beneficiary and potentially harmed service. Keep unmeasured effects hypothetical and report relatedKpis "
    "and supplied evidenceRefs or [].")
V53_BM_FAIR = (
    "Propose one untried compatible configuration from the same supported action space. " + FAIR_ENERGY +
    " Avoid scheduler-only changes on isolated UEs.\n\n"
    "Return instructions relative to C0 and rationale. Explicitly include every nonbaseline setting you intend "
    "to retain. Respect actual resolution and the changed-entry limit. Identify the intended beneficiary and "
    "potentially harmed service without inventing numeric outcomes.")


def fair_prompts() -> bool:
    return os.environ.get("AIC_FAIR_PROMPTS", "").strip() == "1"


if fair_prompts():
    V53_CONTROL, V53_BM = V53_CONTROL_FAIR, V53_BM_FAIR

SYSTEM_V53 = {
    "target": V53_COMMON + "\n\n" + V53_TARGET,
    "control": V53_COMMON + "\n\n" + V53_CONTROL,
    "trajectory": V53_COMMON + "\n\n" + V53_POLICY + "\n\n" + V53_SELECT,
    "monolith-form": V53_COMMON + "\n\n" + V53_TARGET + "\n\n" + V53_CONTROL + "\n\n" + V53_IM_OUTPUT,
    "monolith-select": V53_COMMON + "\n\n" + V53_POLICY + "\n\n" + V53_SELECT,
    "basic-monolith": V53_COMMON + "\n\n" + V53_POLICY + "\n\n" + V53_BM,
}


def v53() -> bool:
    return enabled() and os.environ.get("AIC_V53", "").strip() == "1"


def system(kind_: str) -> str:
    return (SYSTEM_V53 if v53() else SYSTEM)[kind_]


def _energy_req(reqs: Mapping[str, Any]):
    return next((r for r in reqs.values() if isinstance(r, Mapping) and r.get("kpi") == "cellTxAttenuationDb"
                 and r.get("levels")), None)


def energy_level(att: Any, req: Mapping[str, Any]) -> Optional[int]:
    """The strictest level of the energy requirement an attenuation meets (its known arithmetic)."""
    try:
        x = float(att)
    except (TypeError, ValueError):
        return None
    ge = req.get("op", ">=") == ">="
    hits = [k for k, want in enumerate(req["levels"]) if (x >= float(want) - 1e-9 if ge else x <= float(want) + 1e-9)]
    return min(hits) if hits else None


def _supported_attenuations(catalog, cell: str):
    """The attenuation values the radio applies on ``cell``: listed values, or min..max by the step."""
    for f in catalog or []:
        spec = dict((f or {}).get("policyFields") or {}).get("txAttenuationDb")
        if not spec or not any(str(s).endswith(cell) for s in (f.get("scopes") or [f.get("scope", "")])):
            continue
        if spec.get("values"):
            return sorted({float(v) for v in spec["values"]})
        try:
            lo, hi, step = float(spec["min"]), float(spec["max"]), float(spec["step"])
        except (KeyError, TypeError, ValueError):
            return None
        n = int(round((hi - lo) / step))
        return [round(lo + i * step, 6) for i in range(n + 1)]
    return None


def energy_step_values(reqs: Mapping[str, Any], catalog, c0: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Section 2 item 4: the smallest supported attenuations that improve the energy level by one,
    two, three and four steps over C0.  ``None`` when one or two steps do not exist on the supported
    grid; three and four are offered when they exist.

    2026-09-30 (owner): v5.3 offered one and two steps only, and three-agent / internal-monolith build
    their fixed C from these values -- they never went past 9.5 dB while the basic monolith, which
    proposes freely each trial, reached 10-12.5 dB (the best p in every v5.3 block).  Four steps give
    the C-based methods the same reach."""
    req, cell = _energy_req(reqs), energy_cell_id()
    base = c0.get(f"txAttenuationDb@{cell}")
    grid = _supported_attenuations(catalog, cell)
    k0 = energy_level(base, req) if req else None
    if req is None or grid is None or k0 is None:
        return None
    picks = []
    for need in range(1, energy_steps() + 1):
        v = next((a for a in grid if a > float(base) and (energy_level(a, req) is not None)
                  and k0 - energy_level(a, req) >= need), None)
        if v is None:
            if need <= 2:
                return None
            break
        picks.append({"txAttenuationDb": v, "energyLevel": energy_level(v, req),
                      "energyImprovementSteps": k0 - energy_level(v, req)})
    return {"cell": cell, "c0AttenuationDb": float(base), "c0EnergyLevel": k0, "values": picks}


_NAME = {v: k for k, v in CELL_IDS.items()}


def _resulting(cfg: Mapping[str, Any], c0: Mapping[str, Any], req) -> Dict[str, Any]:
    """Section 2 item 2: a configuration expanded over C0 -- where each UE is served and the energy
    cell's attenuation, with the energy level that attenuation earns."""
    full = dict(c0, **{k: str(v) for k, v in dict(cfg or {}).items()})
    cell = energy_cell_id()
    att = full.get(f"txAttenuationDb@{cell}")
    out = {"resultingCells": {k.split("@", 1)[1]: _NAME.get(str(v), str(v))
                              for k, v in sorted(full.items()) if k.startswith("servingCell@")},
           f"{_NAME.get(cell, cell)}AttenuationDb": None if att is None else float(att)}
    if req is not None and att is not None:
        k0, k = energy_level(c0.get(f"txAttenuationDb@{cell}"), req), energy_level(att, req)
        if k0 is not None and k is not None:
            out["energyImprovementSteps"] = k0 - k
    return out


def check_targets(levels_list, reqs: Mapping[str, Any]) -> Optional[str]:
    """Section 4: exactly eight distinct alternatives without T0; the measurement-supported target
    once when it is non-null and not T0; the maximum-concession target when it is null.  A refusal
    string, or ``None``.  Nothing is padded or trimmed."""
    vecs = [tuple(int((lv or {}).get(k, 0) or 0) for k in KEYS) for lv in levels_list]
    if len(vecs) != 8:
        return f"return exactly eight alternatives excluding T0 (got {len(vecs)} after validation)"
    if len(set(vecs)) != 8:
        return "the eight alternatives must be distinct"
    if (0,) * len(KEYS) in vecs:
        return "T0 must not be one of the eight alternatives"
    ref = supported_target(reqs, reference_measurement({}))
    if ref is None:
        if (20,) * len(KEYS) not in vecs:
            return "measurementSupportedTarget is null: include the maximum-concession target (every adjustable level 20)"
        return None
    want = tuple(int(ref["levels"].get(k, 0)) for k in KEYS)
    if any(want) and vecs.count(want) != 1:
        return f"include measurementSupportedTarget {dict(zip(KEYS, want))} exactly once"
    return None


def check_raw_targets(selection, known=None) -> Optional[str]:
    """The answer's own rows: eight objects whose levels name known requirements, are integers 0..20,
    and leave protected requirements at 0."""
    rows = list(selection or [])
    if len(rows) != 8:
        return f"return exactly eight alternatives excluding T0 (got {len(rows)})"
    for i, row in enumerate(rows, 1):
        if not isinstance(row, Mapping) or not isinstance(row.get("levels"), Mapping):
            return f"alternative {i} is not an object with levels"
        for k, v in row["levels"].items():
            if known is not None and k not in known:
                return f"alternative {i}: {k} is not an authorized requirement"
            if k not in KEYS and v not in (0, 0.0):
                return f"alternative {i}: protected {k} must stay 0"
            if isinstance(v, bool) or not isinstance(v, (int, float)) or float(v) != int(v) or not 0 <= int(v) <= 20:
                return f"alternative {i}: level {k}={v!r} is not an integer 0..20"
    return None


def view53(kind_: str, payload: Mapping[str, Any], out: Dict[str, Any]) -> Dict[str, Any]:
    """The v5.3 additions over :func:`view` (``out`` is its result)."""
    state = dict(payload.get("input.network_state") or {})
    c0 = dict(state.get("appliedConfiguration") or {})
    auth = payload.get("input.authorization")
    reqs = dict((auth or {}).get("requirements") or {}) if isinstance(auth, Mapping) else {}
    tc = payload.get("input.target_contract")
    if not reqs and isinstance(tc, Mapping):
        reqs = dict(tc.get("authorization") or {})
    req = _energy_req(reqs)
    if kind_ in ("control", "monolith-form", "basic-monolith"):
        steps = energy_step_values(reqs, payload.get("input.function_catalog"), c0)
        out["input.energyStepValues"] = steps if steps is not None else {
            "unavailable": "no supported attenuation improves the energy level by one and by two steps over C0"}
    if isinstance(out.get("input.target_contract"), Mapping):
        tcv = dict(out["input.target_contract"])
        # (Codex) the model's per-target reason lives in the contract provenance, not on the target record
        order = (((tc or {}).get("provenance") or {}).get("modelSelection") or {}).get("order") or []
        why = {o.get("targetId"): o.get("reason") for o in order if isinstance(o, Mapping) and o.get("reason")}
        tcv["targets"] = sorted(
            ({k: v for k, v in t.items() if k != "tradeoff"} | ({"reason": why[t["targetId"]]} if t["targetId"] in why else {})
             for t in tcv.get("targets") or []),
            key=lambda t: (t["p"], str(t["targetId"])))
        out["input.target_contract"] = tcv
    rows = payload.get("input.control_candidates")
    if isinstance(rows, list) and isinstance(out.get("input.control_candidates"), list):
        out["input.control_candidates"] = [
            dict(v, **_resulting(_settings(r), c0, req)) for v, r in zip(out["input.control_candidates"], rows)]
    obs = payload.get("input.observations")
    if isinstance(obs, list) and isinstance(out.get("input.observations"), list):
        out["input.observations"] = [dict(v, **_resulting(o.get("configuration"), c0, req))
                                     for v, o in zip(out["input.observations"], obs)]
    return out
