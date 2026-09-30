"""P1 — the owner-lexicographic preference order, and its exact ordinal encoding.

2026-09-23, decided by the scenario author in reply to `V45_DESIGN_QUESTIONS.md`:
the comparison key is the **recorded P1 lexicographic owner order**, not a target
id and not the existing weighted scalar ``target.cost``.  Equal weighted costs
need not mean equal P1 preference, so ``cost`` must never stand in for this.

The key, per target:

* for each requirement dimension, its concession normalized by that
  requirement's own authorized original-to-limit span (the value the target
  record already carries in ``concession``);
* averaged **within each owner over that owner's adjustable dimensions only** --
  a protected dimension (``steps``/``deadlineSteps`` of 0) contributes no
  zero-valued entry to the mean, which would otherwise dilute it;
* compared lexicographically in the recorded owner order.

For the frozen seven-requirement workload the vector takes finitely many values,
so an exact ordinal encoding exists (the reply's ``p_rank``).  It is an **order**
encoding: a rank of 12 is not "twice as conceded" as 6, and no percentage
improvement may be computed from rank arithmetic.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, Mapping, Sequence, Tuple

__all__ = [
    "P1_RANK_CEILING",
    "owner_concessions",
    "p1_vector",
    "p_rank",
    "preference_table",
    "rank_scales",
]

#: The reply's encoding for the frozen seven-requirement grid.  ``a`` is the
#: operator's single adjustable dimension, ``b``/``c`` the ue1 and ue2 ones, and
#: ``d`` the ue3 mean over three adjustable dimensions.  The multipliers are the
#: products of the coarser digits' cardinalities, so the sum is a strict ordinal
#: image of the lexicographic tuple -- :func:`preference_table` verifies that on
#: the real domain rather than trusting it.
_FROZEN_SCALES: Dict[str, Tuple[int, int]] = {
    # owner: (denominator that makes the mean integral, place value)
    "operator-gnb2": (1, 36),
    "ue1-video": (2, 12),
    "ue2-map": (2, 4),
    "ue3-incumbent": (3, 1),
}
P1_RANK_CEILING = 71


def _adjustable(authorization: Mapping[str, Any]) -> Dict[str, Tuple[str, ...]]:
    """owner -> its adjustable requirement dimensions, in declared key order.

    A dimension is adjustable when its own step count is non-zero: ``steps`` for
    a threshold key, ``deadlineSteps`` for the ``#deadline`` companion.  The two
    are separate dimensions -- the scenario adjusts ue3's ratio and its deadline
    independently -- so they are counted separately here.
    """
    out: Dict[str, list] = {}
    for key in sorted(authorization):
        record = authorization[key] or {}
        owner = str(record.get("owner") or "")
        if not owner:
            continue
        if int(record.get("steps") or 0) > 0:
            out.setdefault(owner, []).append(key)
        if int(record.get("deadlineSteps") or 0) > 0:
            out.setdefault(owner, []).append(f"{key}#deadline")
    return {owner: tuple(keys) for owner, keys in out.items()}


def owner_concessions(target: Mapping[str, Any], authorization: Mapping[str, Any],
                      ) -> Dict[str, Fraction]:
    """owner -> mean normalized concession over that owner's adjustable dimensions.

    Exact rationals, not floats: the mean of three thirds must compare equal to
    one, and ``0.1 + 0.2`` arithmetic in an ordering key is how a comparator
    starts disagreeing with its own encoding.
    """
    concession = target.get("concession") or {}
    out: Dict[str, Fraction] = {}
    for owner, keys in _adjustable(authorization).items():
        # A level's concession is q/steps, but records carry it as a float (7.2 of 7.5..6.0
        # reads 0.19999999999999987); snap it back to the small rational it is, or p_rank
        # refuses a perfectly valid target (Codex review of v4.7, 2026-09-25).
        present = [Fraction(str(concession[key])).limit_denominator(10000)
                   for key in keys if key in concession]
        out[owner] = sum(present, Fraction(0)) / len(present) if present else Fraction(0)
    return out


def p1_vector(target: Mapping[str, Any], authorization: Mapping[str, Any],
              owner_priority: Sequence[str]) -> Tuple[Fraction, ...]:
    """The P1 key: owner means in the recorded priority order."""
    means = owner_concessions(target, authorization)
    return tuple(means.get(owner, Fraction(0)) for owner in owner_priority)


def rank_scales(authorization: Mapping[str, Any], owner_priority: Sequence[str],
                ) -> Dict[str, Tuple[int, int]]:
    """The (denominator, place value) per owner used by :func:`p_rank`.

    Returns the frozen table when the workload matches it, so the published
    ``p_rank`` of a target is reproducible from the reply's formula.  A workload
    it does not match gets no encoding: :func:`p_rank` then refuses rather than
    inventing multipliers whose order nobody verified.
    """
    adjustable = _adjustable(authorization)
    frozen = (set(adjustable) == set(_FROZEN_SCALES) and list(owner_priority) == [
        "operator-gnb2", "ue1-video", "ue2-map", "ue3-incumbent"]
        and {o: len(k) for o, k in adjustable.items()}
        == {"operator-gnb2": 1, "ue1-video": 1, "ue2-map": 1, "ue3-incumbent": 3}
        and all(int((authorization[k.split("#")[0]] or {}).get(
            "deadlineSteps" if k.endswith("#deadline") else "steps") or 0) in (1, 2)
            for keys in adjustable.values() for k in keys))
    if frozen:
        return dict(_FROZEN_SCALES)
    return generic_scales(authorization, owner_priority)


def generic_scales(authorization: Mapping[str, Any], owner_priority: Sequence[str],
                   ) -> Dict[str, Tuple[int, int]]:
    """(denominator, place value) for any step counts (v4.7, 2026-09-25).

    An owner's mean over n adjustable dimensions with step counts s_i is a multiple
    of 1 / (n * lcm(s_i)), so ``denominator = n * lcm(s_i)`` makes it an integer in
    ``0 .. denominator``.  Place values are mixed radix over those ranges, last owner
    least significant, so the sum is a strict ordinal image of the lexicographic tuple
    (checked by :func:`preference_table`).  An owner with no adjustable dimension
    contributes a single digit 0.
    """
    from math import lcm
    adjustable = _adjustable(authorization)
    denominators: Dict[str, int] = {}
    for owner in owner_priority:
        keys = adjustable.get(owner, ())
        steps = [int((authorization[k.split("#")[0]] or {}).get(
            "deadlineSteps" if k.endswith("#deadline") else "steps") or 0) for k in keys]
        denominators[owner] = len(steps) * lcm(*steps) if steps else 1
    scales: Dict[str, Tuple[int, int]] = {}
    place = 1
    for owner in reversed(list(owner_priority)):
        scales[owner] = (denominators[owner], place)
        place *= denominators[owner] + 1
    return scales


def p_rank(target: Mapping[str, Any], authorization: Mapping[str, Any],
           owner_priority: Sequence[str]) -> int:
    """``0`` (every owner at its original level) through :data:`P1_RANK_CEILING`.

    A smaller rank is the preferred one.  Raises for a workload the frozen
    encoding was not verified against -- :func:`preference_table` is the
    check that it holds.
    """
    scales = rank_scales(authorization, owner_priority)
    if not scales:
        raise ValueError("no verified p_rank encoding for this authorization")
    total = 0
    for owner, value in zip(owner_priority, p1_vector(target, authorization, owner_priority)):
        denominator, place = scales[owner]
        digit = value * denominator
        if digit.denominator != 1:
            raise ValueError(f"{owner} mean {value} is not integral at scale {denominator}")
        total += int(digit) * place
    return total


def preference_table(targets: Sequence[Mapping[str, Any]], authorization: Mapping[str, Any],
                     owner_priority: Sequence[str]) -> Dict[str, Any]:
    """Every target's vector, its rank, and the proof that the two agree.

    ``agrees`` is the reply's required verification: for every ordered pair of
    targets the rank comparison must give the same answer as the lexicographic
    tuple comparison.  The existing weighted scalar travels in its own field
    (``weightedCost``) so nothing downstream can mistake it for the key.
    """
    rows = []
    for target in targets:
        vector = p1_vector(target, authorization, owner_priority)
        rows.append({
            "targetId": target.get("targetId"),
            "ownerConcessions": {owner: str(value)
                                 for owner, value in zip(owner_priority, vector)},
            "vector": [str(value) for value in vector],
            "pRank": p_rank(target, authorization, owner_priority),
            "weightedCost": target.get("cost"),
        })
    agrees = True
    for left in rows:
        for right in rows:
            lhs = tuple(Fraction(v) for v in left["vector"])
            rhs = tuple(Fraction(v) for v in right["vector"])
            if ((lhs < rhs) != (left["pRank"] < right["pRank"])
                    or (lhs == rhs) != (left["pRank"] == right["pRank"])):
                agrees = False
    return {"rule": "P1: lexicographic(" + ", ".join(f"D_{o}" for o in owner_priority) + ")",
            "ownerPriority": list(owner_priority),
            "rankCeiling": P1_RANK_CEILING,
            "classes": len({row["pRank"] for row in rows}),
            "targets": len(rows),
            "rankAgreesWithTupleOrder": agrees,
            "rows": rows}
