"""The Evidence Coordinator role, as a single strategy-backed caller.

Owner lane: **KAGT**.  New file, added under
``docs/architecture/SEAMS-GATE2.md`` section 3.

Design section 12: the Evidence Coordinator "is replaceable through one
typed interface."  :class:`StrategyBackedEvidenceCoordinator` *is* the one
caller that interface plugs into -- it accepts any
:class:`~assurance.advisors.strategy.AdvisoryStrategy` built independently
(whichever of the six design section 12 names the run picked) and turns its
``propose(...)`` into the
:class:`~assurance.advisors.roles.EvidenceCoordinator` role's
``propose_next(...)``, applying a hard wall-clock timeout and falling back to
:func:`assurance.advisors.strategy.deterministic_fallback` whenever the
strategy raises, times out, or returns something that is not a well-formed
proposal for the request it was given.

This is the concrete mechanism behind design section 15's verification
requirement, "deterministic fallback after agent timeout or malformed
output", and behind section 8's "every case terminates finitely": a case
whose progress depended on a strategy responding would have no bound on how
long it could stall, so the coordinator itself is the bound.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

from assurance.advisors.messages import AdvisoryKind, AdvisoryMessage
from assurance.advisors.strategy import AdvisoryStrategy, deterministic_fallback
from assurance.core.components import ComponentId

__all__ = ["FallbackEvent", "StrategyBackedEvidenceCoordinator"]


@dataclass(frozen=True)
class FallbackEvent:
    """One recorded activation of the deterministic fallback.

    Kept so a test -- and, later, the paper's "fallback usage" statistic,
    design section 13 -- can see why the fallback fired without re-deriving
    it from logs.
    """

    correlation_id: str
    reason: str


class StrategyBackedEvidenceCoordinator:
    """:class:`~assurance.advisors.roles.EvidenceCoordinator` over a swappable
    :class:`~assurance.advisors.strategy.AdvisoryStrategy`.

    Swapping strategies means constructing a new coordinator around a
    different one; there is no method that mutates the strategy in place,
    because a strategy change is a run-configuration decision (design
    section 13), not something the coordinator itself should be asked to do
    mid-case.
    """

    def __init__(self, *, strategy: AdvisoryStrategy, timeout_seconds: float = 2.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._strategy = strategy
        self._timeout_seconds = timeout_seconds
        self._fallback_events: list = []

    @property
    def strategy(self) -> AdvisoryStrategy:
        return self._strategy

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def fallback_events(self) -> Tuple[FallbackEvent, ...]:
        """Every time the deterministic fallback fired, in call order."""
        return tuple(self._fallback_events)

    def propose_next(
        self,
        *,
        catalog_view: Sequence[Any],
        evidence_view: Mapping[str, Any],
        budget_view: Mapping[str, Any],
        correlation_id: str,
        epoch_hash: str,
        now: str,
    ) -> Optional[AdvisoryMessage]:
        """Produce a :class:`~assurance.advisors.messages.NextCandidateProposal`.

        Delegates to the configured strategy under a hard timeout.  On
        success, returns exactly what the strategy returned.  On timeout, on
        any exception, or on a return value that is not a well-formed
        proposal answering *this* request, records a
        :class:`FallbackEvent` and returns
        :func:`assurance.advisors.strategy.deterministic_fallback` instead --
        which itself may legitimately return ``None`` when no candidate is
        available.
        """
        result, failure_reason = self._attempt(
            catalog_view=catalog_view,
            evidence_view=evidence_view,
            budget_view=budget_view,
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
            now=now,
        )
        if failure_reason is None:
            return result

        self._fallback_events.append(
            FallbackEvent(correlation_id=correlation_id, reason=failure_reason)
        )
        return deterministic_fallback(
            catalog_view=catalog_view,
            evidence_view=evidence_view,
            budget_view=budget_view,
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
            now=now,
            failure_reason=failure_reason,
        )

    # -- internals -----------------------------------------------------

    def _attempt(
        self, **kwargs: Any
    ) -> Tuple[Optional[AdvisoryMessage], Optional[str]]:
        """Run the strategy once, under the timeout.

        Returns ``(result, None)`` on a well-formed answer, or
        ``(None, failure_reason)`` otherwise.  The worker thread is released
        with ``wait=False`` on every exit path, including timeout, so this
        method itself never blocks past :attr:`timeout_seconds` even if the
        strategy keeps running in the background -- "must not block
        indefinitely" (design section 12) describes the caller's contract,
        not a promise that a runaway strategy thread stops instantly.
        """
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._strategy.propose, **kwargs)
        try:
            result = future.result(timeout=self._timeout_seconds)
        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False)
            return None, f"timeout after {self._timeout_seconds}s"
        except Exception as exc:  # noqa: BLE001 - any strategy failure triggers fallback
            executor.shutdown(wait=False)
            return None, f"exception: {exc!r}"
        executor.shutdown(wait=False)

        malformed = self._malformed_reason(result, kwargs["correlation_id"], kwargs["epoch_hash"])
        if malformed is not None:
            return None, malformed
        return result, None

    @staticmethod
    def _malformed_reason(
        result: Any, correlation_id: str, epoch_hash: str
    ) -> Optional[str]:
        """Why *result* is not usable, or ``None`` when it is fine.

        ``None`` itself is always fine -- a strategy declining to propose is
        not malformed output (design section 12's docstring for
        :meth:`~assurance.advisors.strategy.AdvisoryStrategy.propose`).
        """
        if result is None:
            return None
        if not isinstance(result, AdvisoryMessage):
            return f"malformed: not an AdvisoryMessage ({type(result).__name__})"
        if result.kind is not AdvisoryKind.NEXT_CANDIDATE_PROPOSAL:
            return f"malformed: wrong kind ({result.kind.value})"
        if result.issued_by is not ComponentId.EVIDENCE_COORDINATOR:
            return f"malformed: wrong issuer ({result.issued_by.value})"
        if result.correlation_id != correlation_id:
            return "malformed: correlation_id does not match the request"
        if result.epoch_hash != epoch_hash:
            return "malformed: epoch_hash does not match the request"
        return None
