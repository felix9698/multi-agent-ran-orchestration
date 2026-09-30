"""v4.7 common evaluator: the best target in a continuous Omega that an observation supports.

Omega is given per requirement as an original level and an authorized limit (``limit is None``
= protected).  The evaluator does not enumerate targets.  For one observation it finds, per
coordinate, the smallest concession the observation supports, discretised at a common
precision (1/PRECISION of the original-to-limit span, positive concessions rounded UP so
0.01 never reads as 0).  Owners are compared lexicographically in the declared order on the
sum of their adjustable coordinates' bins (the same order as their mean, since each owner's
coordinate count is fixed); protected coordinates only gate satisfaction.  ``p`` encodes that
order as one integer (mixed radix), so ranks never depend on which targets a method proposed.

A deadline requirement moves two coordinates at once (success ratio down, deadline up).  It is
judged from the ratios the same request records give at each authorized deadline on the
precision grid, choosing the pair with the fewest bins -- never a single latency turned into a
concession.  Falling below a limit or a protected level is non-attainment, not concession 1.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Sequence, Tuple

PRECISION = 20


def v51() -> bool:
    """``AIC_V51=1`` (owner 2026-09-26): the v5.1 design -- ROC-weighted concession sum instead
    of the coordinate-lexicographic p, and control policies shown as ranges."""
    import os
    return os.environ.get("AIC_V51", "").strip() == "1"


def roc_weights(n: int) -> Tuple[int, ...]:
    """Rank-order-centroid weights of ``n`` ranked coordinates as integers over ``n * lcm(1..n)``:
    w_r = (1/n) sum_{j=r..n} 1/j.  n=4 -> (25, 13, 7, 3) / 48."""
    lcm = 1
    for j in range(1, n + 1):
        lcm = lcm * j // math.gcd(lcm, j)
    return tuple(sum(lcm // j for j in range(r, n + 1)) for r in range(1, n + 1))


@dataclass(frozen=True)
class Level:
    """One ``>=`` requirement: throughput or success ratio."""
    req_id: str
    owner: str
    kpi_key: str                 # key in the observation, e.g. "dlGoodputMbps@ue2"
    original: float
    limit: Optional[float]       # None: protected


@dataclass(frozen=True)
class Deadline:
    """Success ratio >= r within deadline d; both authorized to move (ratio down, d up)."""
    req_id: str
    owner: str
    kpi_key: str                 # observation value: {deadline_ms(str|int): ratio}
    ratio_original: float
    ratio_limit: float
    deadline_original_ms: float
    deadline_limit_ms: float


def _bins(original: float, limit: float, value: float) -> Optional[int]:
    """Concession bins for a lower-bounded level, or None below the limit."""
    if value >= original:
        return 0
    if value < limit:
        return None
    return max(1, math.ceil((original - value) / (original - limit) * PRECISION - 1e-9))


def _by_ms(observed) -> Dict[float, float]:
    observed = dict(observed or {})
    if isinstance(observed.get("byDeadlineMs"), Mapping):   # the trial KPI shape
        observed = observed["byDeadlineMs"]
    return {float(k): float(v) for k, v in observed.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


def _deadline_bins(req: Deadline, observed: Mapping) -> Optional[Tuple[int, int]]:
    """(ratio bins, deadline bins) with the fewest total bins, or None."""
    by_ms = _by_ms(observed)
    best = None
    for j in range(PRECISION + 1):
        d = req.deadline_original_ms + j * (req.deadline_limit_ms - req.deadline_original_ms) / PRECISION
        ratio = next((r for ms, r in by_ms.items() if abs(ms - d) < 1e-6), None)
        if ratio is None:
            continue
        i = _bins(req.ratio_original, req.ratio_limit, ratio)
        if i is None:
            continue
        if best is None or i + j < sum(best):
            best = (i, j)
    return best


@dataclass(frozen=True)
class Verdict:
    attained: bool
    t0: bool = False
    owner_bins: Tuple[int, ...] = ()
    p: Optional[int] = None
    coordinates: Tuple[Tuple[str, int], ...] = ()
    reason: str = ""


def _adjustable(req) -> bool:
    if isinstance(req, Deadline):
        return (req.ratio_limit != req.ratio_original
                or req.deadline_limit_ms != req.deadline_original_ms)
    return req.limit is not None


def weighted_rule(preference) -> bool:
    """A sitting's scoring is the one its recorded preference names -- never the environment
    at replay time (Codex review of 43485aabf)."""
    return str(getattr(preference, "rule", "") or "").startswith("v5.1:")


def evaluate(requirements: Sequence, owners: Sequence[str],
             observation: Mapping[str, object], weighted: Optional[bool] = None) -> Verdict:
    """The most preferred target in Omega this observation supports."""
    sums = {o: 0 for o in owners}
    width = {o: 0 for o in owners}          # adjustable coordinates per owner
    coords = []
    for req in requirements:
        value = observation.get(req.kpi_key)
        if value is None:
            return Verdict(False, reason=f"{req.req_id}: unobserved")
        if isinstance(req, Deadline):
            pair = _deadline_bins(req, value)
            if pair is None:
                return Verdict(False, reason=f"{req.req_id}: below every authorized (ratio, deadline)")
            sums[req.owner] += sum(pair)
            width[req.owner] += ((req.ratio_limit != req.ratio_original)
                                 + (req.deadline_limit_ms != req.deadline_original_ms))
            coords += [(f"{req.req_id}.ratio", pair[0]), (f"{req.req_id}.deadline", pair[1])]
            continue
        if req.limit is None:
            if float(value) < req.original:
                return Verdict(False, reason=f"{req.req_id}: protected level not met")
            coords.append((req.req_id, 0))
            continue
        b = _bins(req.original, req.limit, float(value))
        if b is None:
            return Verdict(False, reason=f"{req.req_id}: below the authorized limit")
        sums[req.owner] += b
        width[req.owner] += 1
        coords.append((req.req_id, b))
    owner_bins = tuple(sums[o] for o in owners)
    p = 0
    if v51() if weighted is None else weighted:
        # v5.1: p = sum_r w_r k_r with ROC weights over the ranked adjustable units (protected
        # ones only gate); A = 1 - p / (PRECISION * sum(w)).  n=4: (25 kE + 13 k1 + 7 k2 + 3 k3).
        ranked = [o for o in owners if width[o]]
        for o, w in zip(ranked, roc_weights(len(ranked))):
            p += w * sums[o]
        return Verdict(True, t0=not any(owner_bins), owner_bins=owner_bins, p=p,
                       coordinates=tuple(coords))
    for o in owners:                        # mixed radix, first owner most significant
        p = p * (PRECISION * width[o] + 1) + sums[o]
    return Verdict(True, t0=not any(owner_bins), owner_bins=owner_bins, p=p,
                   coordinates=tuple(coords))


def supports(target: Mapping[str, object], requirements: Sequence,
             observation: Mapping[str, object]) -> bool:
    """Does the observation meet one proposed target (a Target agent's T entry)?

    ``target`` maps req_id -> level; a Deadline req_id maps to (ratio, deadline_ms)."""
    for req in requirements:
        want = target.get(req.req_id)
        if want is None:
            continue
        value = observation.get(req.kpi_key)
        if value is None:
            return False
        if isinstance(req, Deadline):
            ratio, ms = want
            if _by_ms(value).get(float(ms), -1.0) < float(ratio):
                return False
        elif float(value) < float(want):
            return False
    return True


def in_omega(target: Mapping[str, object], requirements: Sequence) -> bool:
    """A proposed target is authorized: every level between its original and limit."""
    for req in requirements:
        want = target.get(req.req_id)
        if want is None:
            return False
        if isinstance(req, Deadline):
            ratio, ms = want
            if not (req.ratio_limit <= float(ratio) <= req.ratio_original
                    and req.deadline_original_ms <= float(ms) <= req.deadline_limit_ms):
                return False
        elif req.limit is None:
            if float(want) != req.original:
                return False
        elif not (req.limit <= float(want) <= req.original):
            return False
    return True


def requirements_from_intents(intents: Sequence, by_requirement: bool = False) -> Tuple:
    """The coordination Intents of a sitting as evaluator requirements.

    A relaxable requirement (steps > 0 with a bound) spans original..bound; otherwise it is
    protected.  A deadline-ratio requirement moves its ratio and/or its deadline.

    ``by_requirement`` (v5): each requirement is its own comparison unit, so owners passed to
    :func:`evaluate` are requirement ids and ``p`` is 21^3 kE + 21^2 k1 + 21 k2 + k3."""
    out = []
    for intent in intents:
        r = intent.requirement
        key = r.observation_key
        owner = r.req_id if by_requirement else intent.owner
        ratio_limit = float(r.bound) if (r.steps or 0) > 0 and r.bound is not None else None
        if r.kpi == "deadlineSuccessRatio":
            d0 = float(r.deadline_ms or 0.0)
            dl = (float(r.deadline_bound) if (r.deadline_steps or 0) > 0
                  and r.deadline_bound is not None else d0)
            out.append(Deadline(r.req_id, owner, key, float(r.value),
                                float(r.value) if ratio_limit is None else ratio_limit, d0, dl))
        else:
            out.append(Level(r.req_id, owner, key, float(r.value), ratio_limit))
    return tuple(out)


def ranking(intents: Sequence, preference) -> Tuple[Tuple, Tuple[str, ...]]:
    """(requirements, comparison units) for a sitting's preference: the v5 coordinate order
    when the preference carries one, else the owner priority."""
    order = tuple(getattr(preference, "coordinate_order", ()) or ())
    if order:
        return requirements_from_intents(intents, by_requirement=True), order
    return requirements_from_intents(intents), tuple(getattr(preference, "owner_priority", ()) or ())


def evaluate_trials(intents: Sequence, owners: Sequence[str], trials: Sequence,
                    by_requirement: bool = False, weighted: Optional[bool] = None) -> Dict[str, object]:
    """The paper metric for every trial of a sitting (v4.7 plan 3.2-3.3): the best target in
    Omega each observation supports, and the episode's best (smallest p)."""
    reqs = requirements_from_intents(intents, by_requirement=by_requirement)
    rows, best = [], None
    for trial in trials:
        # An invalid observation window supports no target (Codex review #4); it stays in
        # the record as unassessable rather than disappearing.
        # 2026-09-26 (Codex review of v5): the full observation validity, not only the window --
        # a Kernel execution failure or a partial apply is unassessable, never an attainment.
        validity = getattr(trial, "observation_validity", None)
        if getattr(trial, "window_valid", True) is False or (
                isinstance(validity, dict) and validity.get("valid") is False):
            rows.append({"trialIndex": getattr(trial, "trial_index", None), "attained": False,
                         "t0": False, "p": None, "ownerBins": [], "coordinates": [],
                         "reason": "observation window invalid: unassessable"})
            continue
        v = evaluate(reqs, owners, dict(getattr(trial, "kpis", {}) or {}), weighted)
        row = {"trialIndex": getattr(trial, "trial_index", None), "attained": v.attained,
               "t0": v.t0, "p": v.p, "ownerBins": list(v.owner_bins),
               "coordinates": [list(c) for c in v.coordinates], "reason": v.reason}
        rows.append(row)
        if v.attained and (best is None or v.p < best["p"]):
            best = row
    out = {"precision": PRECISION, "ownerPriority": list(owners), "trials": rows, "best": best}
    if v51() if weighted is None else weighted:
        # A = 1 - p / pScale over the adjustable units only (Codex review of 43485aabf: a
        # protected deadline counted here inflated the scale).
        n = sum(1 for req in reqs if _adjustable(req))
        weights = roc_weights(n)
        out.update(rule="v5.1: ROC-weighted concession sum", rocWeights=list(weights),
                   pScale=PRECISION * sum(weights))
    return out
