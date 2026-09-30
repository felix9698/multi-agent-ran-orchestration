"""Shared building blocks for the contract families.

Owner lane: **KCON** (see ``docs/architecture/SEAMS-GATE2.md``).

Every contract family in this package is versioned and content-addressed
(design section 6.2).  The identity fields that makes that true are the same
five for all of them, so they live here once rather than being retyped -- and,
more importantly, so a family that forgets one fails to construct instead of
producing an object nothing can address.

The defensive copies below matter for a specific reason.  A contract is frozen
into an evidence epoch and then hashed (section 6.3); if a family held the
caller's own ``dict``, a later mutation of that dict would silently change what
the epoch claims to have frozen while the recorded hash stayed the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence, Tuple

__all__ = ["ContractIdentity", "frozen_mapping", "frozen_tuple"]


def frozen_mapping(value: Mapping[str, Any]) -> Dict[str, Any]:
    """A defensive plain-``dict`` copy with string keys."""
    if not isinstance(value, Mapping):
        raise TypeError(f"expected a mapping, got {type(value).__name__}")
    return {str(key): item for key, item in value.items()}


def frozen_tuple(value: Sequence[Any]) -> Tuple[Any, ...]:
    """A defensive ``tuple`` copy; refuses a bare string.

    A bare string passed where a sequence of ids was expected would iterate
    into characters, which is the kind of mistake that produces a catalog with
    the right cardinality and the wrong membership.
    """
    if isinstance(value, (str, bytes)):
        raise TypeError("expected a sequence of items, got a string")
    return tuple(value)


@dataclass(frozen=True)
class ContractIdentity:
    """Identity carried by every versioned, content-addressed contract.

    Attributes
    ----------
    contract_id:
        Stable project identifier for the contract, independent of version --
        e.g. ``"target/TrafficSteeringPreference"``.  A project contract
        identifier, never presented as an ETSI or O-RAN standard term
        (design section 9).
    version:
        Semantic version of this contract's content.
    schema_version:
        Version of the *schema* the contract is written against.  Distinct
        from :attr:`version`: a schema bump changes what fields exist, a
        version bump changes what they say.
    document_status:
        Recorded as a plain token here (``"NORMATIVE"`` / ``"DRAFT"`` /
        ``"ILLUSTRATIVE"``); the per-number status lives on each
        :class:`assurance.core.provenance.TypedQuantity`.
    standard_mapping:
        Explicit map from this project contract to the published
        policy / interface / service-model versions it applies (design section
        9, task section 7.8).  Empty means "not yet mapped", which
        :func:`assurance.contracts.validation.validate_contract` treats as
        fail-closed for an objective contract.
    """

    contract_id: str
    version: str
    schema_version: str
    document_status: str
    standard_mapping: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "standard_mapping", frozen_mapping(self.standard_mapping))
