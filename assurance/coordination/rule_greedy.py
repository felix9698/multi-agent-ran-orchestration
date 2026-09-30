"""``rule-greedy``: a deterministic, measurement-driven greedy baseline (owner 2026-09-30).

One sentence for the paper: at every trial the rule reads the same observations the
model methods read, picks the requirement with the largest normalized shortfall at
the best configuration measured so far, and applies the first untried single-axis
step from a fixed rule table for that requirement's kind; a step that makes the
total shortfall worse is abandoned because the next step starts again from the best
configuration (greedy hill climbing with best-so-far restart).

It runs in the basic monolith's place -- the same per-trial proposal path, Kernel,
Gateway and verdicts -- selected by naming ``rule-greedy`` as the monolith model.
No model is called.

Rule table (a step changes one axis by one step, then compatibility decides):

* a UE's service (goodput, deadline) short:  lift the UE's own PRB cap; move one
  competitor on its cell to the other cell; double the UE's PF weight; cap one
  competitor tighter; lower the cell's TX attenuation by one step.
* a cell's energy (``cellTxAttenuationDb``) short: raise that cell's attenuation
  by one step.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

RULE_GREEDY = "rule-greedy"

def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scope_id(scope: str) -> str:
    return str(scope).split("@", 1)[-1]


def _observed(kpis: Mapping[str, Any], req: Mapping[str, Any]) -> Optional[float]:
    value = kpis.get(f"{req.get('kpi')}@{_scope_id(req.get('scope', ''))}")
    if isinstance(value, Mapping):          # deadlineSuccessRatio: {"byDeadlineMs": {"100": r}}
        by = value.get("byDeadlineMs") or {}
        want = _num(req.get("deadlineMs"))
        for key, ratio in by.items():
            if want is None or _num(key) == want:
                return _num(ratio)
        return None
    return _num(value)


def shortfall(kpis: Mapping[str, Any], req: Mapping[str, Any]) -> float:
    """How far one observation is from the requirement's original value, in units
    of its authorized relaxation range (0 = met, missing KPI = 0: not judged)."""
    observed = _observed(kpis, req)
    original = _num(req.get("original"))
    if observed is None or original is None:
        return 0.0
    limit = _num(req.get("limit"))
    span = abs(original - limit) if limit is not None else 0.0
    span = max(span, 0.05 * abs(original), 1e-6)
    gap = (original - observed) if req.get("op", ">=") in (">=", ">") else (observed - original)
    return max(0.0, gap) / span


def _values(domains: Mapping[str, Any], axis: str) -> List[str]:
    """The catalog's own strings for ``axis`` (full ``kind@scope`` first, then the
    kind alone), in numeric order."""
    spec = domains.get(axis) or domains.get(axis.split("@", 1)[0])
    if not spec:
        return []
    if spec[0] == "values":
        vals = [str(v) for v in spec[1]]
    else:                                   # a range the catalog did not expand
        lo, hi, step = spec[1], spec[2], spec[3]
        whole = float(step).is_integer() and float(lo).is_integer()
        vals = [str(int(round(lo + i * step))) if whole else f"{lo + i * step:.2f}".rstrip("0")
                for i in range(int(round((hi - lo) / step)) + 1)]
        vals = [v + "0" if v.endswith(".") else v for v in vals]
    numeric = [v for v in vals if _num(v) is not None]
    return sorted(numeric, key=lambda v: _num(v)) + [v for v in vals if _num(v) is None]


def _neighbour(vals: Sequence[str], current: str, up: bool) -> Optional[str]:
    now = _num(current)
    if now is None:
        return None
    pool = [v for v in vals if _num(v) is not None
            and ((_num(v) > now + 1e-9) if up else (_num(v) < now - 1e-9))]
    return (pool[0] if up else pool[-1]) if pool else None


def _uncapped(value: Any) -> bool:
    return str(value) in ("0", "0.0", "", "None")


def _moves(req: Mapping[str, Any], config: Mapping[str, str], cells: Sequence[str],
           domains: Mapping[str, Any]) -> List[Tuple[str, Dict[str, str]]]:
    """The rule table for one short requirement: ``(why, changes)`` in order."""
    kpi, target = str(req.get("kpi", "")), _scope_id(req.get("scope", ""))
    out: List[Tuple[str, Dict[str, str]]] = []
    if kpi.startswith("cell") and "Attenuation" in kpi:
        axis = f"txAttenuationDb@{target}"
        up = _neighbour(_values(domains, axis), config.get(axis, ""), True)
        if up is not None:
            out.append((f"{req['_id']} short: raise {axis} one step", {axis: up}))
        return out
    ue = target
    cell = config.get(f"servingCell@{ue}")
    competitors = sorted(k.split("@", 1)[1] for k, v in config.items()
                         if k.startswith("servingCell@") and v == cell
                         and k != f"servingCell@{ue}")
    if (not _uncapped(config.get(f"dlPrbCap@{ue}", "0"))
            and "0" in _values(domains, f"dlPrbCap@{ue}")):
        out.append((f"{req['_id']} short: lift {ue}'s own PRB cap", {f"dlPrbCap@{ue}": "0"}))
    for other_ue in competitors:
        for dest in (c for c in cells if c != cell):
            out.append((f"{req['_id']} short: move competitor {other_ue} off {ue}'s cell",
                        {f"servingCell@{other_ue}": dest}))
    weight = _num(config.get(f"pfWeight@{ue}", "1.0")) or 1.0
    doubled = [v for v in _values(domains, f"pfWeight@{ue}")
               if abs((_num(v) or 0) - 2 * weight) < 1e-9]
    if doubled:
        out.append((f"{req['_id']} short: double {ue}'s PF weight",
                    {f"pfWeight@{ue}": doubled[0]}))
    for other_ue in competitors:
        caps = [v for v in _values(domains, f"dlPrbCap@{other_ue}") if not _uncapped(v)]
        # the coarse ladder first, then every tighter listed cap below it
        coarse = sorted((v for v in caps if (_num(v) or 0) in (25, 20, 15, 10, 5)),
                        key=lambda v: -(_num(v) or 0))
        floor = min((_num(v) for v in coarse), default=float("inf"))
        finer = sorted((v for v in caps if (_num(v) or 0) < floor), key=lambda v: -(_num(v) or 0))
        current = config.get(f"dlPrbCap@{other_ue}", "0")
        for cap in coarse + finer:                                        # loosest tighter cap first
            if _uncapped(current) or (_num(cap) or 0) < (_num(current) or 0):
                out.append((f"{req['_id']} short: cap competitor {other_ue} at {cap} PRB",
                            {f"dlPrbCap@{other_ue}": cap}))
    if cell and f"txAttenuationDb@{cell}" in config:
        axis = f"txAttenuationDb@{cell}"
        down = _neighbour(_values(domains, axis), config[axis], False)
        if down is not None:
            out.append((f"{req['_id']} short: lower {axis} one step", {axis: down}))
    return out


def admits(domains: Mapping[str, Any], axis: str, value: str) -> bool:
    """Is ``value`` in the catalog's domain for ``axis`` (``kind@scope``)?"""
    spec = domains.get(axis) or domains.get(axis.split("@", 1)[0])
    if spec is None:
        return False
    if spec[0] == "values":
        return str(value) in {str(v) for v in spec[1]}
    number = _num(value)
    lo, hi, step = spec[1], spec[2], spec[3]
    return (number is not None and lo - 1e-9 <= number <= hi + 1e-9
            and abs((number - lo) / step - round((number - lo) / step)) < 1e-6)


def decide(requirements: Mapping[str, Mapping[str, Any]],
           observations: Sequence[Mapping[str, Any]],
           applied: Mapping[str, str],
           tried: Sequence[Mapping[str, str]],
           domains: Mapping[str, Any],
           refused: Any = None) -> Optional[Tuple[Dict[str, str], str]]:
    """The next full configuration and why, or ``None`` when no rule step is left.

    ``domains`` maps an axis kind (``servingCell``, ``dlPrbCap``, ``pfWeight``,
    ``txAttenuationDb``) to ``("values", [...])`` or ``("range", lo, hi, step)`` from
    the function catalog; a step whose value the catalog does not admit is skipped.
    ``refused(configuration) -> bool`` is the deployment's compatibility check."""
    cells = sorted({v for axis, spec in domains.items() if axis.split("@", 1)[0] == "servingCell"
                    for v in _values(domains, axis)})
    reqs = {rid: dict(r, _id=rid) for rid, r in requirements.items()}
    scored = []
    for obs in observations:
        if not obs.get("valid", True) or not obs.get("configuration"):
            continue
        per = {rid: shortfall(obs.get("kpis") or {}, r) for rid, r in reqs.items()}
        scored.append((sum(per.values()), int(obs.get("trialIndex", 0) or 0),
                       {**dict(applied), **dict(obs["configuration"])}, per))
    # Best-first over what was measured (board 952, 2026-09-30: a pure best-so-far restart
    # stopped after two trials at a local optimum).  The best configuration's steps come
    # first; when they are spent, the next best measured configuration's short requirements
    # are worked on, and so on; the applied configuration last.
    starts = [(cfg, per) for _t, _i, cfg, per in sorted(scored, key=lambda item: (item[0], item[1]))]
    if not scored:
        # No valid measurement yet (no initial measurement, or its window was invalid):
        # every requirement counts as short and the rule table is walked in order
        # rather than stopping the board (Codex 2026-09-30).
        starts = [(dict(applied), {rid: 1.0 for rid in reqs})]
    elif all(cfg != dict(applied) for cfg, _per in starts):
        starts.append((dict(applied), starts[0][1]))
    seen = {tuple(sorted(dict(t).items())) for t in tried}
    done = set()
    for start, per in starts:
        key = tuple(sorted(start.items()))
        if key in done:
            continue
        done.add(key)
        order = sorted(reqs, key=lambda rid: (-per.get(rid, 0.0), rid))
        for rid in order:
            if per.get(rid, 0.0) <= 0.0:
                continue
            for why, changes in _moves(reqs[rid], start, cells, domains):
                candidate = {**start, **changes}
                if not all(admits(domains, axis, value) for axis, value in changes.items()):
                    continue
                if candidate == start or tuple(sorted(candidate.items())) in seen:
                    continue
                if refused is not None and refused(candidate):
                    continue
                return candidate, why
    return None


if __name__ == "__main__":      # self-check
    reqs = {"I2d.r1": {"kpi": "deadlineSuccessRatio", "scope": "ue@ue2", "op": ">=",
                       "original": 0.9, "limit": 0.6, "deadlineMs": 100.0},
            "I4e.r1": {"kpi": "cellTxAttenuationDb", "scope": "cell@A", "op": ">=",
                       "original": 20.0, "limit": 8.0}}
    c0 = {"servingCell@ue2": "A", "servingCell@ue3": "A", "dlPrbCap@ue2": "15", "dlPrbCap@ue3": "0",
          "pfWeight@ue2": "1.0", "pfWeight@ue3": "1.0", "txAttenuationDb@A": "8.0"}
    obs = [{"trialIndex": 0, "valid": True, "configuration": c0,
            "kpis": {"deadlineSuccessRatio@ue2": {"byDeadlineMs": {"100": 0.0}}, "cellTxAttenuationDb@A": 8.0}}]
    dom = {"servingCell": ("values", ["A", "B"]), "dlPrbCap": ("range", 0.0, 37.0, 1.0),
           "pfWeight": ("range", 0.25, 8.0, 0.25), "txAttenuationDb": ("range", 8.0, 20.0, 0.5)}
    got = decide(reqs, obs, c0, [c0], dom)
    assert got and got[0]["dlPrbCap@ue2"] == "0", got          # deadline worst (3.0 vs 1.0): lift own cap
    obs.append({"trialIndex": 1, "valid": True, "configuration": got[0],
                "kpis": {"deadlineSuccessRatio@ue2": {"byDeadlineMs": {"100": 0.95}}, "cellTxAttenuationDb@A": 8.0}})
    nxt = decide(reqs, obs, got[0], [c0, got[0]], dom)
    assert nxt and nxt[0]["txAttenuationDb@A"] == "8.5", nxt    # deadline met -> energy step from the best
    print("ok")
