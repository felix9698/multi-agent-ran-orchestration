"""KAGT's calling convention for the ``catalog_view`` a strategy receives.

Owner lane: **KAGT**.  New file, added under
``docs/architecture/SEAMS-GATE2.md`` section 3's "새 파일을 자기 패키지 안에
추가하는 것은 소유 레인의 재량" -- the frozen
:meth:`~assurance.advisors.strategy.AdvisoryStrategy.propose` and
:func:`~assurance.advisors.strategy.deterministic_fallback` signatures type
``catalog_view`` as ``Sequence[Any]`` deliberately, because the Kernel lane
that will eventually populate it does not exist yet in Gate 2.  Something has
to fix a concrete shape so KAGT's own strategies and tests can be written now;
this module is that shape.

:func:`~assurance.advisors.strategy.deterministic_fallback`'s docstring is
explicit that the fallback "must select only from candidates that are
``AVAILABLE`` on the candidate-availability axis" -- a property
:class:`~assurance.contracts.catalog.Candidate` itself does not carry, because
availability is Kernel-owned and changes independently of the frozen catalog
(design section 6.3).  :class:`CatalogEntry` carries it alongside the
candidate instead of on it, so a strategy never has to guess where
availability lives.
"""

from __future__ import annotations

from dataclasses import dataclass

from assurance.contracts.catalog import Candidate
from assurance.core.axes import CandidateAvailability

__all__ = ["CatalogEntry"]


@dataclass(frozen=True)
class CatalogEntry:
    """One frozen-catalog candidate together with its current availability."""

    candidate: Candidate
    availability: CandidateAvailability

    @property
    def candidate_id(self) -> str:
        return self.candidate.candidate_id

    @property
    def is_available(self) -> bool:
        return self.availability is CandidateAvailability.AVAILABLE
