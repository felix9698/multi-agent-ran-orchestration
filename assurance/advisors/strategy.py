"""The single replaceable strategy interface.

Owner lane: **KAGT**.  Signatures frozen by this design step.

Design section 12: the Evidence Coordinator "is replaceable through one typed
interface" with a role-separated LLM coordinator (the proposed method), a
deterministic strategy, a random strategy, an optimisation-based strategy, a
monolithic LLM strategy and an adversarial Advisor strategy.  "All strategies
use the same Kernel, contracts, catalog, harm limits, hardware conditions, and
evidence rules."

One interface, six implementations, is what makes the comparison a controlled
experiment rather than six systems.  If a strategy could reach anything the
others could not -- a different catalog, a private budget, a side channel to
the gateway -- the paper's comparison would be measuring the plumbing.  So the
interface below hands every strategy exactly the same read-only views and
accepts exactly the same typed proposal back.

:func:`deterministic_fallback` is required rather than optional.  Design
section 15 verifies "deterministic fallback after agent timeout or malformed
output", and section 8 requires every case to terminate finitely: a case whose
progress depended on a model responding would have no bound on how long it
could stall.  The fallback is what makes an agent's failure a recorded event
instead of a hang.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from assurance.advisors.messages import AdvisoryMessage
from assurance.advisors.proposal_support import (
    available_candidates,
    budget_exhausted,
    build_next_candidate_proposal,
)

__all__ = ["AdvisoryStrategy", "StrategyKind", "deterministic_fallback"]


class StrategyKind(Enum):
    """The six comparable strategies of design section 12.

    ``ADVERSARIAL_ADVISOR`` is a scientific baseline for invariant and
    robustness experiments -- a strategy that deliberately proposes badly, so
    the Kernel's refusals can be measured.  Design section 12 is explicit that
    it "is a paper baseline, not an adversarial code-review campaign".

    The old Coordinator is deliberately absent: design section 2.2 excludes it
    as a selectable production or comparison strategy.
    """

    #: The proposed method: role-separated LLM Evidence Coordinator.
    LLM_EVIDENCE_COORDINATOR = "LLM_EVIDENCE_COORDINATOR"
    DETERMINISTIC = "DETERMINISTIC"
    RANDOM = "RANDOM"
    OPTIMIZATION = "OPTIMIZATION"
    #: One model doing the whole job, under an equivalent token/tool budget.
    MONOLITHIC_LLM = "MONOLITHIC_LLM"
    ADVERSARIAL_ADVISOR = "ADVERSARIAL_ADVISOR"


@runtime_checkable
class AdvisoryStrategy(Protocol):
    """The one interface every proposal strategy implements."""

    #: Identifier recorded with every proposal, so the paper can attribute
    #: outcomes to a strategy without inferring it.
    strategy_id: str
    #: Which of the six this is.
    strategy_kind: StrategyKind

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
        """Propose the next candidate, or ``None``.

        Signature frozen; body owned by lane **KAGT**.

        Identical parameters to
        :meth:`assurance.advisors.roles.EvidenceCoordinator.propose_next`, on
        purpose: the LLM coordinator is one strategy among six, not a
        privileged case with extra inputs.

        A seeded strategy takes its seed at construction, not here.  Design
        section 13 requires the batch plan to fix the seed and the run to
        record it, so a strategy that drew randomness per call could not be
        reproduced from the plan.

        Must not block indefinitely.  The caller applies the strategy's
        timeout and falls back to :func:`deterministic_fallback`; a strategy
        that ignores cancellation makes that guarantee untrue.
        """
        ...

    def describe(self) -> Mapping[str, Any]:
        """Strategy provenance for the run record.

        Signature frozen; body owned by lane **KAGT**.

        Model identity, prompt version, token/tool-call budget, seed,
        temperature.  Design section 12 requires the monolithic comparison to
        use "the same foundation model and a controlled equivalent
        token/tool-call budget", which is only checkable if every strategy
        states its budget.  Must contain no credential.
        """
        ...


def deterministic_fallback(
    *,
    catalog_view: Sequence[Any],
    evidence_view: Mapping[str, Any],
    budget_view: Mapping[str, Any],
    correlation_id: str,
    epoch_hash: str,
    now: str,
    failure_reason: str,
) -> Optional[AdvisoryMessage]:
    """Produce a proposal when the configured strategy failed.

    Signature frozen; body owned by lane **KAGT**.

    Called after an agent timeout, a malformed output, a schema violation or a
    mailbox rejection.  *failure_reason* is recorded with the resulting
    proposal so design section 13's "fallback usage" statistic counts what
    actually happened rather than an untyped retry.

    The body must be **deterministic and total**: given the same catalog,
    evidence state and budget it returns the same proposal on every host and
    every run, with no clock read and no randomness.  That is what lets a
    replay reproduce a run in which the model timed out -- the fallback path
    has to be as reproducible as the happy path.

    It must select only from candidates that are ``AVAILABLE`` on the
    candidate-availability axis and must respect the same budget view as any
    other strategy.  A fallback with relaxed rules would be a way to keep a
    case alive past its caps, which design section 8 forbids outright.

    Returns ``None`` when no candidate is available.  ``None`` here is not an
    exhaustion certificate -- exhaustion remains
    :meth:`assurance.kernel.kernel.AssuranceKernel.exhaustion_certificate`'s
    decision, made from evidence cells rather than from a proposer running out
    of ideas.

    Body owned by lane **KAGT**; implemented against the shared, dependency-
    light helpers in :mod:`assurance.advisors.proposal_support` so this
    function and every concrete
    :class:`AdvisoryStrategy` in :mod:`assurance.advisors.strategies` select
    candidates and respect the budget view by the same rule.  Picks the
    lexicographically-smallest available ``candidate_id`` rather than "first
    in *catalog_view*": a total order over the id string is reproducible
    regardless of how the caller assembled the sequence, which matters most
    on exactly this path, since it runs after something upstream has already
    failed once.
    """
    if budget_exhausted(budget_view):
        return None
    available = available_candidates(catalog_view)
    if not available:
        return None
    chosen = min(available, key=lambda candidate: candidate.candidate_id)
    reason = str(failure_reason)
    return build_next_candidate_proposal(
        message_id=f"deterministic-fallback/{correlation_id}/{chosen.candidate_id}",
        candidate_id=chosen.candidate_id,
        rationale=f"deterministic fallback ({reason}): lexicographically-first available candidate",
        correlation_id=correlation_id,
        epoch_hash=epoch_hash,
        now=now,
    )
