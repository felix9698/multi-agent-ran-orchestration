"""The comparisons decision section 5.3 (2026-09-23 reply) names, from episode records.

Kept apart from :mod:`experiments.agent_metrics` because those figures are
guarded by a digest of every pre-existing statistic; these are new and are
wired into ``summarize`` as one additive key.

Every quantity is read off the record the sitting wrote -- nothing is
re-measured.  The domain is the episode's own pinned Omega
(:func:`agent_metrics.omega_targets`), which expands the same authorization the
sitting judged against, so ``trial['success']`` keys and Omega ids line up.

``p_rank`` is an *order* encoding verified only for the frozen v4.5 workload
(:mod:`assurance.coordination.preference`).  An episode it was not verified
against gets ``None`` for every rank-based quantity rather than an invented
number, and is counted as ``rankUnavailable``.
"""
from __future__ import annotations

from statistics import fmean
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from assurance.coordination.preference import p1_vector, p_rank

#: Predeclared thresholds (reply section 5.3): original target; operator, UE1
#: and UE2 unchanged with at most one third UE3 mean concession; any authorized.
QUALITY_THRESHOLDS: Tuple[int, ...] = (0, 1, 71)

INITIAL_T0 = "t0_satisfied"
INITIAL_RELAXED = "relaxed_target_only"
INITIAL_NONE = "no_authorized_target_satisfied"
INITIAL_UNKNOWN = "unknown"
INITIAL_STATES = (INITIAL_T0, INITIAL_RELAXED, INITIAL_NONE, INITIAL_UNKNOWN)


def _valid(trial: Mapping[str, Any]) -> bool:
    """A trial whose observation may carry attainment credit (reply section 3.4)."""
    validity = trial.get("observationValidity")
    if isinstance(validity, Mapping) and "valid" in validity:
        return bool(validity["valid"])
    return bool((trial.get("window") or {}).get("valid"))


def _ranks(episode: Mapping[str, Any]) -> Tuple[Dict[str, Optional[int]], Dict[str, Any]]:
    """Omega id -> p_rank (or None), and Omega id -> the target record."""
    from experiments.agent_metrics import omega_targets
    omega = omega_targets(episode)
    authorization = omega.authorization.to_record()
    order = tuple(omega.preference.owner_priority)
    ranks: Dict[str, Optional[int]] = {}
    records: Dict[str, Any] = {}
    for target in omega.targets:
        record = target.to_record()
        records[target.target_id] = record
        try:
            ranks[target.target_id] = p_rank(record, authorization, order)
        except (ValueError, KeyError, TypeError, ZeroDivisionError):
            ranks[target.target_id] = None
    owners = {req_id: entry.owner for req_id, entry in omega.authorization.requirements.items()}
    return ranks, {"records": records, "authorization": authorization, "order": order,
                   "t0": omega.t0.target_id, "owners": owners}


def _best_rank(trial: Mapping[str, Any], ranks: Mapping[str, Optional[int]]
               ) -> Tuple[Optional[int], Optional[str]]:
    best: Tuple[Optional[int], Optional[str]] = (None, None)
    for target_id, ok in dict(trial.get("success") or {}).items():
        if not ok:
            continue
        rank = ranks.get(str(target_id))
        if rank is None:
            continue
        if best[0] is None or rank < best[0]:
            best = (rank, str(target_id))
    return best


def episode_quality(episode: Mapping[str, Any]) -> Dict[str, Any]:
    """Section 5.3 for one episode: initial state, T_q, efficiency, final preference."""
    trials = sorted(episode.get("trials") or (), key=lambda t: int(t.get("trialIndex", 0)))
    deadline = (episode.get("budget") or {}).get("deadlineBMs")
    ranks, domain = _ranks(episode)
    rank_available = any(rank is not None for rank in ranks.values())

    # -- initial state (the reference observation, trial 0) --------------------
    reference = next((t for t in trials if int(t.get("trialIndex", -1)) == 0), None)
    if reference is None or not _valid(reference):
        initial = INITIAL_UNKNOWN
        initial_rank = None
    else:
        success = dict(reference.get("success") or {})
        if success.get(domain["t0"]):
            initial = INITIAL_T0
        elif any(success.values()):
            initial = INITIAL_RELAXED
        else:
            initial = INITIAL_NONE
        initial_rank = _best_rank(reference, ranks)[0]

    # -- which owners' ORIGINAL requirements already held at the reference ------
    # Reply section 1: "report how often the operator requirement and the complete
    # T0 are initially satisfied".  Per owner, from the reference's own T0
    # verdicts; unknown when the reference is invalid or a verdict is not PASS/FAIL.
    owner_met: Dict[str, Optional[bool]] = {}
    if reference is not None and _valid(reference):
        t0_verdicts = dict((reference.get("verdicts") or {}).get(domain["t0"]) or {})
        by_owner: Dict[str, List[Optional[bool]]] = {}
        for req_id, owner in domain["owners"].items():
            verdict = t0_verdicts.get(req_id)
            by_owner.setdefault(owner, []).append(
                True if verdict == "PASS" else False if verdict == "FAIL" else None)
        for owner, values in by_owner.items():
            owner_met[owner] = None if None in values else all(values)
    else:
        owner_met = {owner: None for owner in set(domain["owners"].values())}

    # -- efficiency --------------------------------------------------------------
    counted = [t for t in trials if t.get("counted")]
    reference_counted = bool(reference is not None and reference.get("counted"))
    formal = len(counted)
    invalid = sum(1 for t in counted if not _valid(t))

    # -- T_q and dispatches to each threshold -----------------------------------
    within = lambda t: deadline is None or float(t.get("elapsedMs") or 0.0) <= float(deadline)
    t_q: Dict[str, Optional[float]] = {}
    dispatches_to: Dict[str, Optional[int]] = {}
    already: Dict[str, Optional[bool]] = {}
    for q in QUALITY_THRESHOLDS:
        key = str(q)
        t_q[key] = None
        dispatches_to[key] = None
        already[key] = (None if initial == INITIAL_UNKNOWN or not rank_available
                        else (initial_rank is not None and initial_rank <= q))
        if not rank_available:
            continue
        used = 0
        for trial in trials:
            if trial.get("counted"):
                used += 1
            if not _valid(trial) or not within(trial):
                continue
            rank, _target = _best_rank(trial, ranks)
            if rank is not None and rank <= q:
                t_q[key] = float(trial.get("elapsedMs") or 0.0)
                dispatches_to[key] = used
                break

    # -- final preference by B --------------------------------------------------
    best: Tuple[Optional[int], Optional[str]] = (None, None)
    for trial in trials:
        if _valid(trial) and within(trial):
            rank, target_id = _best_rank(trial, ranks)
            if rank is not None and (best[0] is None or rank < best[0]):
                best = (rank, target_id)
    vector = None
    if best[1] is not None:
        vector = [str(v) for v in p1_vector(domain["records"][best[1]],
                                            domain["authorization"], domain["order"])]
    retained = dict(episode.get("retained") or {})
    termination = dict(episode.get("termination") or {})
    return {
        "episodeId": episode.get("episodeId"),
        "method": episode.get("method"),
        "rankAvailable": rank_available,
        "deadlineBMs": deadline,
        "initialState": initial,
        "initialBestRank": initial_rank,
        "initialOwnerOriginalMet": owner_met,
        "tQMs": t_q,
        "alreadyMetAtReference": already,
        "dispatchesToQ": dispatches_to,
        "formalDispatches": formal,
        "searchDispatches": formal - (1 if reference_counted else 0),
        "invalidDispatches": invalid,
        "bestRankByB": best[0],
        "bestTargetByB": best[1],
        "bestOwnerVectorByB": vector,
        "finalConfiguration": {"controlId": retained.get("controlId"),
                               "retentionQualified": bool(retained.get("qualified")),
                               "detail": retained.get("detail")},
        "termination": {"reason": termination.get("reason"),
                        "kernelTermination": termination.get("kernelTermination")},
    }


def _restricted(rows: Sequence[Mapping[str, Any]], key: str) -> Dict[str, Any]:
    """mean(min(T_q, B)) with its attained/total count (reply section 5.3).

    Non-attainment is infinity before the minimum, so it enters as B -- the
    restricted mean time *without* attainment, not an assumption that it
    succeeded at B.
    """
    usable = [row for row in rows if row["rankAvailable"] and row["deadlineBMs"] is not None]
    attained = [row for row in usable if row["tQMs"][key] is not None]
    values = [min(row["tQMs"][key] if row["tQMs"][key] is not None else float("inf"),
                  float(row["deadlineBMs"])) for row in usable]
    return {"attained": len(attained), "total": len(usable),
            "fraction": (len(attained) / len(usable)) if usable else None,
            "restrictedMeanMs": fmean(values) if values else None,
            "conditionalMeanMs": (fmean(row["tQMs"][key] for row in attained)
                                  if attained else None),
            "meanDispatchesWhenAttained": (fmean(row["dispatchesToQ"][key] for row in attained)
                                           if attained else None)}


def quality_summary(episodes: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Section 5.3 over one cohort.  Every started episode stays in the denominator."""
    rows = [episode_quality(episode) for episode in episodes]
    initial = {state: sum(1 for row in rows if row["initialState"] == state)
               for state in INITIAL_STATES}
    thresholds = {}
    for q in QUALITY_THRESHOLDS:
        key = str(q)
        # Descriptive subgroup: the valid reference did not already meet q.  Arms may
        # differ in it; these are not identical starting states (reply section 5.3).
        unmet = [row for row in rows if row["alreadyMetAtReference"][key] is False]
        thresholds[key] = {"all": _restricted(rows, key), "unmetAtReference": _restricted(unmet, key)}
    usable = [row for row in rows if row["rankAvailable"]]
    resolved = [row for row in usable if row["bestRankByB"] is not None]
    owners = sorted({owner for row in rows for owner in row["initialOwnerOriginalMet"]})
    owner_initial = {owner: {
        "met": sum(1 for row in rows if row["initialOwnerOriginalMet"].get(owner) is True),
        "notMet": sum(1 for row in rows if row["initialOwnerOriginalMet"].get(owner) is False),
        "unknown": sum(1 for row in rows if row["initialOwnerOriginalMet"].get(owner) is None),
    } for owner in owners}
    return {
        "N": len(rows),
        "rankUnavailable": len(rows) - len(usable),
        "initialState": initial,
        "initialOwnerOriginalMet": owner_initial,
        "tQ": thresholds,
        "efficiency": {
            "formalDispatches": fmean(row["formalDispatches"] for row in rows) if rows else None,
            "searchDispatches": fmean(row["searchDispatches"] for row in rows) if rows else None,
            "invalidDispatches": sum(row["invalidDispatches"] for row in rows),
        },
        "finalPreference": {
            "unresolvedFraction": (1 - len(resolved) / len(usable)) if usable else None,
            "bestRankByB": [row["bestRankByB"] for row in rows],
            "retentionQualified": sum(1 for row in rows
                                      if row["finalConfiguration"]["retentionQualified"]),
        },
        "episodes": rows,
    }
