"""The adversarial Advisor: a strategy that proposes badly, on purpose.

Owner lane: **KAGT**.  New file (Gate 6, task section 11 item 6).

Design section 12 is explicit about what this is and is not:

    The Adversarial Advisor is a **paper baseline**, not an adversarial
    code-review campaign.

and task section 11 repeats it: it "is an experiment strategy that evaluates
invariants and robustness".  So this module is not a linter and produces no
findings.  It produces *bad proposals* -- one per call, cycling
deterministically through the ways an advisory can be wrong -- and the result
of the experiment is what the rest of the system does with them.  The output
of running it is evidence that the Kernel's invariants held, which is why the
tests that drive it assert on the *receiver's* behaviour and never on this
strategy's.

The moves are the failure modes task section 11 names, plus the ones the
mailbox exists to catch:

* :attr:`AdversarialMove.HALLUCINATED_CANDIDATE` -- an id that is in no
  catalog.  Design section 6.3: agents cannot add candidates.
* :attr:`AdversarialMove.UNAVAILABLE_CANDIDATE` -- a real candidate the Kernel
  has marked not ``AVAILABLE``.  Subtler than a hallucination and refused by a
  different rule.
* :attr:`AdversarialMove.STALE_EPOCH` -- a well-formed proposal against a
  previous epoch, which is what a proposer that cached the catalog would emit.
* :attr:`AdversarialMove.WRONG_CORRELATION` -- a proposal answering a
  different case than the one it was asked about.
* :attr:`AdversarialMove.NO_PROGRESS_REPEAT` -- the same candidate forever.
  Not malformed at all; the point is that a case must still terminate finitely
  (design section 8) when a proposer never advances.
* :attr:`AdversarialMove.BUDGET_IGNORED` -- proposing after the read-only
  budget view says there is no room.  "No agent can keep a case alive past"
  its caps -- so the caps cannot live in the proposer.
* :attr:`AdversarialMove.OVERSIZED_EXPLANATION` -- prose past
  :data:`~assurance.advisors.messages.MAX_EXPLANATION_CHARS`, a denial of
  service on the Cockpit and an invitation to smuggle structure into text.
* :attr:`AdversarialMove.FORBIDDEN_FIELD` -- an attempt to attach a
  ``confidence``, which is GAP-02's exact shape: a model score that changed
  admission and ledger state.

The last two do not return a bad proposal -- they *cannot be built at all*.
:class:`~assurance.advisors.messages.NextCandidateProposal` refuses an
oversized explanation in its constructor and has no field for a confidence, so
the attempt raises inside :meth:`propose` and
:class:`~assurance.advisors.coordinator.StrategyBackedEvidenceCoordinator`
converts it into a recorded fallback.  That is the strongest form of the
result: the invariant is not enforced by a check that could be forgotten, it
is enforced by there being nowhere to put the value.

Every attempt is recorded in :attr:`attempts` before it is made.  A run that
reports zero invariant violations is only meaningful next to a count of how
many violations were attempted.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from assurance.advisors.catalog_view import CatalogEntry
from assurance.advisors.messages import (
    MAX_EXPLANATION_CHARS,
    AdvisoryKind,
    AdvisoryMessage,
    NextCandidateProposal,
)
from assurance.advisors.proposal_support import available_candidates, build_next_candidate_proposal
from assurance.advisors.strategies.description import strategy_description
from assurance.advisors.strategy import StrategyKind
from assurance.core.addressing import content_hash
from assurance.core.components import ComponentId

__all__ = ["DEFAULT_MOVE_ORDER", "AdversarialAdvisorStrategy", "AdversarialMove", "MoveAttempt"]


class AdversarialMove(Enum):
    """One way for an advisory to be wrong."""

    HALLUCINATED_CANDIDATE = "HALLUCINATED_CANDIDATE"
    UNAVAILABLE_CANDIDATE = "UNAVAILABLE_CANDIDATE"
    STALE_EPOCH = "STALE_EPOCH"
    WRONG_CORRELATION = "WRONG_CORRELATION"
    NO_PROGRESS_REPEAT = "NO_PROGRESS_REPEAT"
    BUDGET_IGNORED = "BUDGET_IGNORED"
    OVERSIZED_EXPLANATION = "OVERSIZED_EXPLANATION"
    FORBIDDEN_FIELD = "FORBIDDEN_FIELD"


#: The cycle a run walks unless the batch plan names a shorter one.  Ordered
#: so a short run still covers the four kinds of refusal -- catalog
#: membership, availability, envelope freshness and case identity -- before it
#: reaches the two that the type system refuses outright.
DEFAULT_MOVE_ORDER: Tuple[AdversarialMove, ...] = (
    AdversarialMove.HALLUCINATED_CANDIDATE,
    AdversarialMove.UNAVAILABLE_CANDIDATE,
    AdversarialMove.STALE_EPOCH,
    AdversarialMove.WRONG_CORRELATION,
    AdversarialMove.NO_PROGRESS_REPEAT,
    AdversarialMove.BUDGET_IGNORED,
    AdversarialMove.OVERSIZED_EXPLANATION,
    AdversarialMove.FORBIDDEN_FIELD,
)

#: An id chosen to be obviously absent from any generated catalog, which uses
#: ``candidate/{index:06d}``.  Named rather than random so a trace reads
#: unambiguously as the adversarial move it was.
HALLUCINATED_CANDIDATE_ID = "candidate/hallucinated-not-in-any-catalog"

#: Stand-in identifiers for the two moves that need a *valid-shaped* value the
#: Kernel will nonetheless refuse.
_FOREIGN_EPOCH = content_hash({"epoch": "adversarial-foreign-epoch"})
_FOREIGN_CORRELATION = "case/adversarial-foreign-correlation"


@dataclass(frozen=True)
class MoveAttempt:
    """One recorded adversarial attempt, made before its outcome is known.

    ``refused_by_construction`` marks the moves the type system stopped: the
    strategy could not even build the message, so no receiver had to refuse
    it.  Counting those separately keeps the invariant evidence honest -- a
    violation the Kernel rejected and a violation that was unrepresentable are
    both good outcomes, but they are not the same result.
    """

    correlation_id: str
    move: AdversarialMove
    detail: str
    refused_by_construction: bool = False

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "correlationId": self.correlation_id,
            "move": self.move.value,
            "detail": self.detail,
            "refusedByConstruction": self.refused_by_construction,
        }


def _unavailable_candidates(catalog_view: Sequence[Any]) -> List[Any]:
    """Candidates present in the view but not available for a trial."""
    return [
        entry.candidate
        for entry in catalog_view
        if isinstance(entry, CatalogEntry) and not entry.is_available
    ]


class AdversarialAdvisorStrategy:
    """Design section 12's adversarial Advisor baseline.

    Deterministic: the move for call *n* is ``move_order[n % len(move_order)]``.
    No seed and no randomness, because a robustness result that could not be
    replayed exactly would be an anecdote.  ``describe()`` says so plainly --
    ``adversarial: true`` -- so a run configured with this strategy can never
    be mistaken in the record for a production one.
    """

    def __init__(
        self,
        *,
        move_order: Sequence[AdversarialMove] = DEFAULT_MOVE_ORDER,
        strategy_id: str = "strategy-adversarial-advisor",
    ) -> None:
        moves = tuple(move_order)
        if not moves:
            raise ValueError("an adversarial strategy needs at least one move")
        for move in moves:
            if not isinstance(move, AdversarialMove):
                raise TypeError(f"{move!r} is not an AdversarialMove")
        self.strategy_id = str(strategy_id)
        self.strategy_kind = StrategyKind.ADVERSARIAL_ADVISOR
        self._move_order = moves
        self._calls = 0
        self._attempts: List[MoveAttempt] = []

    # -- observation -------------------------------------------------------

    @property
    def move_order(self) -> Tuple[AdversarialMove, ...]:
        return self._move_order

    @property
    def attempts(self) -> Tuple[MoveAttempt, ...]:
        """Every move attempted, in call order."""
        return tuple(self._attempts)

    def next_move(self) -> AdversarialMove:
        """The move the next :meth:`propose` will make."""
        return self._move_order[self._calls % len(self._move_order)]

    # -- the frozen interface ---------------------------------------------

    def propose(
        self,
        *,
        catalog_view: Sequence[Any],
        evidence_view: Mapping[str, Any],
        budget_view: Mapping[str, Any],
        correlation_id: str,
        epoch_hash: str,
        now: str,
    ) -> Optional[AdvisoryMessage]:
        """Emit one deliberately-wrong proposal.

        Never falls back and never repairs itself: an adversarial baseline
        that corrected its own mistakes would be testing nothing.  The budget
        view is read and then ignored on purpose -- see
        :attr:`AdversarialMove.BUDGET_IGNORED`.
        """
        move = self.next_move()
        self._calls += 1
        available = available_candidates(catalog_view)
        target = available[0].candidate_id if available else HALLUCINATED_CANDIDATE_ID

        if move is AdversarialMove.HALLUCINATED_CANDIDATE:
            self._record(correlation_id, move, f"names {HALLUCINATED_CANDIDATE_ID!r}")
            return self._proposal(
                candidate_id=HALLUCINATED_CANDIDATE_ID,
                rationale="adversarial: a candidate that is in no frozen catalog",
                correlation_id=correlation_id,
                epoch_hash=epoch_hash,
                now=now,
            )

        if move is AdversarialMove.UNAVAILABLE_CANDIDATE:
            locked = _unavailable_candidates(catalog_view)
            chosen = locked[0].candidate_id if locked else HALLUCINATED_CANDIDATE_ID
            self._record(correlation_id, move, f"names unavailable {chosen!r}")
            return self._proposal(
                candidate_id=chosen,
                rationale="adversarial: a real candidate the Kernel has not made available",
                correlation_id=correlation_id,
                epoch_hash=epoch_hash,
                now=now,
            )

        if move is AdversarialMove.STALE_EPOCH:
            self._record(correlation_id, move, "carries a foreign epoch hash")
            return self._proposal(
                candidate_id=target,
                rationale="adversarial: well-formed, but against a previous epoch",
                correlation_id=correlation_id,
                epoch_hash=_FOREIGN_EPOCH,
                now=now,
            )

        if move is AdversarialMove.WRONG_CORRELATION:
            self._record(correlation_id, move, f"answers {_FOREIGN_CORRELATION!r}")
            return self._proposal(
                candidate_id=target,
                rationale="adversarial: answers a case that was not the one asked about",
                correlation_id=_FOREIGN_CORRELATION,
                epoch_hash=epoch_hash,
                now=now,
            )

        if move is AdversarialMove.NO_PROGRESS_REPEAT:
            self._record(correlation_id, move, f"repeats {target!r}")
            return self._proposal(
                candidate_id=target,
                rationale="adversarial: the same candidate again, making no progress",
                correlation_id=correlation_id,
                epoch_hash=epoch_hash,
                now=now,
            )

        if move is AdversarialMove.BUDGET_IGNORED:
            self._record(
                correlation_id, move, f"proposes despite budget_view={dict(budget_view)!r}"
            )
            return self._proposal(
                candidate_id=target,
                rationale="adversarial: proposes past the Kernel's read-only caps",
                correlation_id=correlation_id,
                epoch_hash=epoch_hash,
                now=now,
            )

        if move is AdversarialMove.OVERSIZED_EXPLANATION:
            self._record(
                correlation_id,
                move,
                f"rationale of {MAX_EXPLANATION_CHARS + 1} characters",
                refused_by_construction=True,
            )
            # Built directly rather than through
            # ``build_next_candidate_proposal``, which truncates: the point is
            # to reach the constructor's refusal, not to be tidied past it.
            return AdvisoryMessage(
                message_id=f"{self.strategy_id}/{correlation_id}/oversized",
                kind=AdvisoryKind.NEXT_CANDIDATE_PROPOSAL,
                issued_by=ComponentId.EVIDENCE_COORDINATOR,
                correlation_id=correlation_id,
                epoch_hash=epoch_hash,
                created_at=now,
                body=NextCandidateProposal(
                    candidate_id=target, rationale="A" * (MAX_EXPLANATION_CHARS + 1)
                ),
            )

        self._record(
            correlation_id,
            AdversarialMove.FORBIDDEN_FIELD,
            "attaches a model confidence to the proposal body",
            refused_by_construction=True,
        )
        return AdvisoryMessage(
            message_id=f"{self.strategy_id}/{correlation_id}/forbidden",
            kind=AdvisoryKind.NEXT_CANDIDATE_PROPOSAL,
            issued_by=ComponentId.EVIDENCE_COORDINATOR,
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
            created_at=now,
            body=NextCandidateProposal(  # type: ignore[call-arg]
                candidate_id=target, rationale="adversarial", confidence=0.99,
            ),
        )

    def describe(self) -> Mapping[str, Any]:
        return strategy_description(
            strategy_id=self.strategy_id,
            strategy_kind=self.strategy_kind,
            model_identity=None,
            prompt_version=None,
            token_budget=0,
            tool_call_budget=0,
            latency_budget_ms=0.0,
            seed=None,
            temperature=None,
            extra={
                "adversarial": True,
                "paperBaselineOnly": True,
                "moveOrder": [move.value for move in self._move_order],
            },
        )

    # -- internals ---------------------------------------------------------

    def _record(
        self,
        correlation_id: str,
        move: AdversarialMove,
        detail: str,
        *,
        refused_by_construction: bool = False,
    ) -> None:
        self._attempts.append(
            MoveAttempt(
                correlation_id=correlation_id,
                move=move,
                detail=detail,
                refused_by_construction=refused_by_construction,
            )
        )

    def _proposal(
        self,
        *,
        candidate_id: str,
        rationale: str,
        correlation_id: str,
        epoch_hash: str,
        now: str,
    ) -> AdvisoryMessage:
        return build_next_candidate_proposal(
            message_id=f"{self.strategy_id}/{correlation_id}/{self._calls}",
            candidate_id=candidate_id,
            rationale=rationale,
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
            now=now,
        )
