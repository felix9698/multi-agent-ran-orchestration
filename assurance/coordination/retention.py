"""Retain-on-improvement: the predicate the scenario author specified, as one place.

2026-09-23 decision, in reply to `V45_DESIGN_QUESTIONS.md` §3.  The runtime that
produced the v4.4 and early v4.5 episodes was **reset-after-trial**: all 24
control trials that satisfied at least one target were rolled back to C0 and only
a separate re-application after the search could leave anything live.  That is a
different experiment from retain-on-improvement, so the rule is written here once
and the five questions it turns on are answered explicitly:

1. **Compare on what?** The recorded P1 lexicographic owner order -- never a
   target id, never the weighted scalar ``target.cost``.  On the frozen
   seven-requirement domain 20 pairs of targets share a ``cost`` while differing
   in P1, so ``cost`` cannot stand in for the key.
2. **What counts as no worse?** Equal-or-better.  Equal P1 with a *different*
   requirement vector still retains: the owners are indifferent between them, and
   the later configuration is the one already applied.
3. **Retain what?** The complete confirmed configuration within the managed
   control scope, never the subset of axes that happened to help.
4. **Does the retained configuration become the rollback baseline?** Yes.  C0
   stays the fixed reference candidates are expanded against; it is not an
   unconditional rollback destination.
5. **Retain across an execution failure?** No.  A demonstrated attainment is
   still recorded as history, but retention needs a confirmed applied state and
   no outstanding recovery.  The two are reported separately.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, Tuple

__all__ = ["RetentionDecision", "decide_retention", "next_baseline"]

#: No attainment yet.  Named rather than written as a bare float so the
#: "infinity vs infinity must not retain" rule below reads as a rule.
NO_ATTAINMENT = float("inf")


class RetentionDecision:
    """Whether to keep the applied configuration, and why -- in one record."""

    __slots__ = ("retain", "reason", "current_best", "previous_best", "qualifies")

    def __init__(self, *, retain: bool, reason: str, current_best: float,
                 previous_best: float, qualifies: bool) -> None:
        self.retain = bool(retain)
        self.reason = str(reason)
        self.current_best = current_best
        self.previous_best = previous_best
        self.qualifies = bool(qualifies)

    def to_record(self) -> dict:
        finite = lambda value: None if value == NO_ATTAINMENT else value
        return {"retain": self.retain, "reason": self.reason,
                "performanceQualifies": self.qualifies,
                "currentBestPRank": finite(self.current_best),
                "previousBestPRank": finite(self.previous_best)}


def decide_retention(*, trial_is_valid: bool, current_best: float, previous_best: float,
                     configuration_confirmed: bool, recovery_required: bool,
                     ) -> RetentionDecision:
    """The decision, exactly as specified.

    ``current_best`` / ``previous_best`` are P1 ranks (lower is preferred) or
    :data:`NO_ATTAINMENT`.  ``previous_best`` is the best **before** this trial --
    the caller updates history from valid observations separately, so that an
    equal result compares against the earlier value rather than against itself.
    """
    if not trial_is_valid:
        return RetentionDecision(retain=False, reason="observation-invalid",
                                 current_best=current_best, previous_best=previous_best,
                                 qualifies=False)
    if current_best == NO_ATTAINMENT:
        # Infinity compared with infinity must not retain an unsuccessful
        # configuration -- the finite condition is the whole point.
        return RetentionDecision(retain=False, reason="no-target-attained",
                                 current_best=current_best, previous_best=previous_best,
                                 qualifies=False)
    qualifies = current_best <= previous_best
    if not qualifies:
        return RetentionDecision(retain=False, reason="worse-than-previous-best",
                                 current_best=current_best, previous_best=previous_best,
                                 qualifies=False)
    if not configuration_confirmed:
        return RetentionDecision(retain=False, reason="applied-state-unconfirmed",
                                 current_best=current_best, previous_best=previous_best,
                                 qualifies=True)
    if recovery_required:
        return RetentionDecision(retain=False, reason="recovery-outstanding",
                                 current_best=current_best, previous_best=previous_best,
                                 qualifies=True)
    first = previous_best == NO_ATTAINMENT
    return RetentionDecision(
        retain=True,
        reason="first-attainment" if first
        else ("improved" if current_best < previous_best else "equal-preference"),
        current_best=current_best, previous_best=previous_best, qualifies=True)


def next_baseline(decision: RetentionDecision, applied: Mapping[str, str],
                  previous_baseline: Mapping[str, str]) -> dict:
    """What the next trial saves as its recovery baseline.

    A retained configuration becomes it; otherwise the baseline the trial was
    restored to carries over.  **C0 is not an unconditional rollback
    destination** -- it stays the fixed reference candidates are expanded
    against, which is a different job.
    """
    return dict(applied) if decision.retain else dict(previous_baseline)


def transition_scope(applied: Mapping[str, str], target: Mapping[str, str],
                     ) -> Tuple[str, ...]:
    """Which axes the move from ``applied`` to ``target`` actually changes.

    The four function/scope limit is checked against **the transition**, not
    against the candidate's difference from C0: applying the next complete
    candidate may have to reset axes a retained one moved, and those resets are
    changes.  Returns the axis names, sorted, so a caller can count and name them.
    """
    axes = set(applied) | set(target)
    return tuple(sorted(axis for axis in axes
                        if str(applied.get(axis)) != str(target.get(axis))))
