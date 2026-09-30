"""Shared building blocks for every ``NextCandidateProposal`` producer.

Owner lane: **KAGT**.  New file, added under
``docs/architecture/SEAMS-GATE2.md`` section 3.

:func:`assurance.advisors.strategy.deterministic_fallback` and every concrete
:class:`~assurance.advisors.strategy.AdvisoryStrategy` in
:mod:`assurance.advisors.strategies` build the same shaped proposal from the
same read-only views.  This module is where that shape and the one shared
rule -- "select only AVAILABLE candidates", "respect the same budget view" --
live once, so design section 12's six comparable strategies (and the
fallback that stands in for a seventh, failed one) cannot drift from each
other by accident.

Deliberately dependency-light: nothing here imports
:mod:`assurance.advisors.strategy`, so that module can import this one
without a cycle.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Sequence

from assurance.advisors.catalog_view import CatalogEntry
from assurance.advisors.messages import (
    MAX_EXPLANATION_CHARS,
    AdvisoryKind,
    AdvisoryMessage,
    NextCandidateProposal,
)
from assurance.core.components import ComponentId

__all__ = ["available_candidates", "budget_exhausted", "build_next_candidate_proposal"]


def available_candidates(catalog_view: Sequence[Any]) -> List[Any]:
    """The candidates in *catalog_view* usable for a trial right now.

    A :class:`~assurance.advisors.catalog_view.CatalogEntry` is filtered by
    its carried availability; a bare candidate (no availability attached) is
    treated as already-filtered by the caller.
    """
    out: List[Any] = []
    for entry in catalog_view:
        if isinstance(entry, CatalogEntry):
            if entry.is_available:
                out.append(entry.candidate)
            continue
        out.append(entry)
    return out


def budget_exhausted(budget_view: Mapping[str, Any]) -> bool:
    """True when the read-only budget view already reports no room left.

    Recognises ``trials_used``/``max_trials`` and
    ``proposals_used``/``max_proposals`` when present as ints; any other
    shape is treated as "not exhausted" rather than guessed at -- the exact
    key set of a Kernel-owned budget view (deadline, trial cap, proposal cap,
    harm reserve; design section 8) is not fixed by Gate 2.
    """
    trials_used = budget_view.get("trials_used")
    max_trials = budget_view.get("max_trials")
    if isinstance(trials_used, int) and isinstance(max_trials, int) and trials_used >= max_trials:
        return True
    proposals_used = budget_view.get("proposals_used")
    max_proposals = budget_view.get("max_proposals")
    if (
        isinstance(proposals_used, int)
        and isinstance(max_proposals, int)
        and proposals_used >= max_proposals
    ):
        return True
    return False


def build_next_candidate_proposal(
    *,
    message_id: str,
    candidate_id: str,
    rationale: str,
    correlation_id: str,
    epoch_hash: str,
    now: str,
) -> AdvisoryMessage:
    """Wrap one candidate id and rationale into a well-formed advisory."""
    return AdvisoryMessage(
        message_id=message_id,
        kind=AdvisoryKind.NEXT_CANDIDATE_PROPOSAL,
        issued_by=ComponentId.EVIDENCE_COORDINATOR,
        correlation_id=correlation_id,
        epoch_hash=epoch_hash,
        created_at=now,
        body=NextCandidateProposal(
            candidate_id=candidate_id, rationale=rationale[:MAX_EXPLANATION_CHARS],
        ),
    )
