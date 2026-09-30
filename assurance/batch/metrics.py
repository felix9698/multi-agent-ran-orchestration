"""Pure aggregation for normalized Batch trial records (stdlib only)."""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, Mapping

from experiments.metrics import confidence_interval, mean_std


def derive_batch_metrics(records: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    all_rows = list(records)
    rows = [row for row in all_rows if bool(row.get("included", True))]
    outcomes = Counter(str(row.get("outcome", "ERROR")) for row in all_rows)
    validity = Counter(str(row.get("validity", "ERROR")) for row in all_rows)
    wall_clock = [float(row["wallClockS"]) for row in rows if isinstance(row.get("wallClockS"), (int, float))]
    closure = [float(row["closureProgress"]) for row in rows if isinstance(row.get("closureProgress"), (int, float))]
    efficiency = [float(row["closureEfficiency"]) for row in rows if isinstance(row.get("closureEfficiency"), (int, float))]
    recovery = [float(row["recoveryS"]) for row in rows if isinstance(row.get("recoveryS"), (int, float))]
    harm_charge = [float(row["harmCharge"]) for row in rows if isinstance(row.get("harmCharge"), (int, float))]
    harm_reserve = [float(row["harmReserve"]) for row in rows if isinstance(row.get("harmReserve"), (int, float))]
    target_debt = [float(row["targetDebt"]) for row in rows if isinstance(row.get("targetDebt"), (int, float))]
    reject = sum(int(row.get("proposalRejects", 0)) for row in rows)
    stale = sum(int(row.get("proposalStale", 0)) for row in rows)
    fallback = sum(int(row.get("fallbacks", 0)) for row in rows)
    invariant = sum(int(row.get("invariantViolations", 0)) for row in rows)
    tokens = sum(int(row.get("agentTokens", 0)) for row in rows)
    tools = sum(int(row.get("agentToolCalls", 0)) for row in rows)
    latency = [float(row["agentLatencyMs"]) for row in rows if isinstance(row.get("agentLatencyMs"), (int, float))]
    satisfaction = [bool(row.get("targetSatisfied", False)) for row in rows]
    hold = [bool(row.get("holdSatisfied", False)) for row in rows]
    return {
        "trialCounts": {"valid": validity["VALID"], "invalid": validity["INVALID"],
                        "error": validity["ERROR"], "total": len(all_rows)},
        "inclusion": {"rule": all_rows[0].get("inclusionRule", "all_terminal") if all_rows else "all_terminal",
                      "included": len(rows), "excluded": len(all_rows) - len(rows)},
        "wallClock": _summary(wall_clock),
        "closure": {"progress": _summary(closure), "efficiency": _summary(efficiency)},
        "harm": {"reserve": _summary(harm_reserve), "charge": _summary(harm_charge),
                 "targetDebt": _summary(target_debt),
                 "limitsRespected": all(bool(row.get("harmLimitRespected", False)) for row in rows)},
        "outcomes": dict(sorted(outcomes.items())),
        "rollbackRate": _rate(rows, "rolledBack"), "recoveryTime": _summary(recovery),
        "incidentCount": sum(bool(row.get("incident", False)) for row in rows),
        "invariantViolations": invariant,
        "proposals": {"rejected": reject, "stale": stale, "fallback": fallback},
        "agents": {"tokens": tokens, "toolCalls": tools, "latencyMs": _summary(latency)},
        "kpis": {"KPM": _mean_field(rows, "kpm"), "O1": _mean_field(rows, "o1"),
                 "Core": _mean_field(rows, "core"), "RAN": _mean_field(rows, "ran"),
                 "UEApplication": _mean_field(rows, "ueApplication")},
        "target": {"satisfactionRate": sum(satisfaction) / len(rows) if rows else None,
                   "holdRate": sum(hold) / len(rows) if rows else None},
        "evidence": {"reuse": sum(int(row.get("evidenceReuse", 0)) for row in rows),
                     "confirmations": sum(int(row.get("confirmations", 0)) for row in rows),
                     "postClosureWitnesses": sum(int(row.get("postClosureWitnesses", 0)) for row in rows)},
    }


def _summary(values: list[float]) -> Dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None, "std": None, "ci95": None}
    mean, std = mean_std(values)
    ci = confidence_interval(values)
    return {"n": len(values), "mean": mean, "std": std, "ci95": ci}


def _rate(rows: list[Mapping[str, Any]], key: str) -> float | None:
    return sum(bool(row.get(key, False)) for row in rows) / len(rows) if rows else None


def _mean_field(rows: list[Mapping[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
    return sum(values) / len(values) if values else None
