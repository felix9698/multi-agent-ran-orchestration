"""Provenance and typed quantities.

Complete module: pure data with constructor-enforced invariants, no owner.

Design section 6.1: *every operational number has a unit, source record,
document status and one provenance value*.  Task section 5.2-5.5 adds the two
rules that give the requirement teeth -- ``DERIVED`` keeps its derivation rule
and input references, and ``ILLUSTRATIVE`` values never reach runtime
admission, a harm bound or a success threshold.

Both rules are enforced in :class:`TypedQuantity`'s constructor rather than in a
downstream validator.  A number that reaches the Kernel has, by then, already
been copied into a reserve ledger, a watchdog arm or a predicate comparison;
the only place where "this figure came from a slide, not from the testbed"
can still be caught cheaply is where the figure is created.  That is also the
concrete cutover for GAP-02 in ``docs/architecture/GATE1-MAP.md``: the old path
let a model-produced confidence float straight into admission because nothing
about the float recorded where it came from.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Final, Mapping, Optional, Sequence, Tuple, Union

from assurance.core.addressing import content_hash

__all__ = [
    "DocumentStatus",
    "InadmissibleQuantityError",
    "Provenance",
    "TypedQuantity",
    "UNIT_DIMENSIONLESS",
    "UNIT_PATTERN",
]

Number = Union[int, float]

#: A unit is a short token: ``Mbps``, ``dBm``, ``ms``, ``PRB``, ``1``.  The
#: registry of legal units per counter belongs to the Measurement Contract
#: (task section 5.6); this module only refuses an empty or whitespace unit,
#: because a unitless operational number is the failure the requirement names.
UNIT_PATTERN: Final[re.Pattern] = re.compile(r"^[A-Za-z0-9_%/^.*-]{1,32}$")

#: Explicit spelling for a ratio or a count, so "no unit" is never blank.
UNIT_DIMENSIONLESS: Final[str] = "1"


class Provenance(Enum):
    """Where an operational number came from (design section 6.1).

    Exactly these five.  The set is closed on purpose: a sixth value would be
    a place to hide a number whose origin nobody checked.
    """

    #: The Operator confirmed this value through a GUI control (section 5).
    OPERATOR_CONFIRMED = "OPERATOR_CONFIRMED"
    #: Bound by a capability or composition manifest admitted into the epoch.
    MANIFEST_BOUND = "MANIFEST_BOUND"
    #: Observed by the Measurement Collector from a raw counter (section 4.5).
    MEASURED = "MEASURED"
    #: Fixed by the experiment plan: repetitions, seeds, windows, budgets.
    EXPERIMENT_CONFIG = "EXPERIMENT_CONFIG"
    #: Computed from other values; keeps its rule and inputs (section 6.1).
    DERIVED = "DERIVED"


class DocumentStatus(Enum):
    """How much weight the source document carries.

    Only :attr:`NORMATIVE` is admissible at runtime.  ``DRAFT`` is fail-closed
    for the same reason ``ILLUSTRATIVE`` is: a value that has not been frozen
    into an epoch cannot be the thing a trial is judged against.

    OPEN_QUESTION: the design names ``ILLUSTRATIVE`` explicitly and leaves the
    remaining document statuses unenumerated.  ``NORMATIVE`` / ``DRAFT`` are
    the minimum needed to express "frozen" versus "not yet frozen"; adding a
    fourth status is a ``docs/architecture/SEAMS-GATE2.md`` amendment.
    """

    #: Frozen into an admitted contract; usable for admission and thresholds.
    NORMATIVE = "NORMATIVE"
    #: Recorded but not yet frozen into an epoch.  Not admissible.
    DRAFT = "DRAFT"
    #: Documentation or presentation material.  Never admissible (task 5.5).
    ILLUSTRATIVE = "ILLUSTRATIVE"


class InadmissibleQuantityError(ValueError):
    """A quantity was offered to a runtime decision it may not inform.

    Raised by :meth:`TypedQuantity.require_admissible`, which is what the
    Kernel calls before a number becomes an admission input, a harm bound or a
    success threshold.
    """


@dataclass(frozen=True, order=False)
class TypedQuantity:
    """One operational number with everything needed to defend it.

    Attributes
    ----------
    value:
        A finite ``int`` or ``float``.  ``NaN`` and infinities are refused:
        they cannot be canonicalised (RFC 8785), so a record containing one
        could not be content-addressed, and a comparison against one silently
        returns ``False`` in every direction.
    unit:
        A non-empty unit token.  See :data:`UNIT_PATTERN`.
    provenance:
        Exactly one :class:`Provenance`.
    source_record:
        Identifier or content hash of the record this number was read from --
        a raw sample id, a manifest digest, an experiment plan id, a
        confirmation event id.  Non-empty: "somewhere" is not a source.
    document_status:
        :class:`DocumentStatus`; defaults to :attr:`DocumentStatus.NORMATIVE`
        because the common case is a frozen contract value, and the two
        inadmissible statuses have to be stated deliberately.
    derivation_rule:
        Required for ``DERIVED``, forbidden otherwise.  A short identifier of
        the rule (``"mean_over_window"``, ``"p95"``, ``"a_minus_b"``), not a
        prose explanation -- replay has to re-run it.
    input_refs:
        Required for ``DERIVED``, forbidden otherwise.  The source records the
        rule consumed, so a derived number is walkable back to raw evidence.

    Both directions of the ``DERIVED`` rule are enforced.  Requiring the rule
    on ``DERIVED`` stops an unexplained computed number; forbidding it on the
    other four stops the opposite dodge, where a computed number is labelled
    ``MEASURED`` and carries its derivation in a field nobody reads.
    """

    value: Number
    unit: str
    provenance: Provenance
    source_record: str
    document_status: DocumentStatus = DocumentStatus.NORMATIVE
    derivation_rule: Optional[str] = None
    input_refs: Tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise TypeError(f"value must be a real number, got {type(self.value).__name__}")
        if not math.isfinite(self.value):
            raise ValueError("value must be finite; NaN/inf cannot be canonicalised")
        if not isinstance(self.unit, str) or not UNIT_PATTERN.match(self.unit):
            raise ValueError(f"unit must be a non-empty unit token, got {self.unit!r}")
        if not isinstance(self.provenance, Provenance):
            raise TypeError("provenance must be a Provenance member")
        if not isinstance(self.document_status, DocumentStatus):
            raise TypeError("document_status must be a DocumentStatus member")
        if not isinstance(self.source_record, str) or not self.source_record.strip():
            raise ValueError("source_record must name the record this number came from")

        object.__setattr__(self, "input_refs", tuple(self.input_refs))
        for ref in self.input_refs:
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError("input_refs entries must be non-empty record references")

        if self.provenance is Provenance.DERIVED:
            if not self.derivation_rule or not str(self.derivation_rule).strip():
                raise ValueError("DERIVED requires a derivation_rule (design section 6.1)")
            if not self.input_refs:
                raise ValueError("DERIVED requires input_refs (design section 6.1)")
        else:
            if self.derivation_rule is not None:
                raise ValueError(
                    "derivation_rule is only meaningful for DERIVED; "
                    f"{self.provenance.value} must not carry one"
                )
            if self.input_refs:
                raise ValueError(
                    "input_refs is only meaningful for DERIVED; "
                    f"{self.provenance.value} must not carry any"
                )

    # -- admission ---------------------------------------------------------

    @property
    def admissible_for_runtime(self) -> bool:
        """True only for a ``NORMATIVE`` value (task section 5.5)."""
        return self.document_status is DocumentStatus.NORMATIVE

    def require_admissible(self, purpose: str) -> "TypedQuantity":
        """Return ``self`` if admissible, else raise.

        *purpose* names the decision the number was about to inform
        (``"harm bound"``, ``"success threshold"``, ``"contract admission"``)
        so the refusal says what would have gone wrong.
        """
        if not self.admissible_for_runtime:
            raise InadmissibleQuantityError(
                f"{self.document_status.value} quantity from {self.source_record!r} "
                f"may not be used for {purpose} (task section 5.5)"
            )
        return self

    # -- content addressing ------------------------------------------------

    def to_canonical_dict(self) -> Dict[str, Any]:
        """The canonical wire form, in the design's camelCase spelling.

        Optional derivation fields are omitted rather than emitted as ``null``
        so a non-derived quantity has one canonical form, and therefore one
        digest, regardless of how it was constructed.
        """
        record: Dict[str, Any] = {
            "value": self.value,
            "unit": self.unit,
            "provenance": self.provenance.value,
            "sourceRecord": self.source_record,
            "documentStatus": self.document_status.value,
        }
        if self.provenance is Provenance.DERIVED:
            record["derivationRule"] = self.derivation_rule
            record["inputRefs"] = list(self.input_refs)
        return record

    def content_hash(self) -> str:
        """Canonical digest of this quantity."""
        return content_hash(self.to_canonical_dict())

    @classmethod
    def from_canonical_dict(cls, record: Mapping[str, Any]) -> "TypedQuantity":
        """Rebuild a quantity from :meth:`to_canonical_dict` output."""
        refs: Sequence[str] = record.get("inputRefs", ()) or ()
        return cls(
            value=record["value"],
            unit=record["unit"],
            provenance=Provenance(record["provenance"]),
            source_record=record["sourceRecord"],
            document_status=DocumentStatus(record["documentStatus"]),
            derivation_rule=record.get("derivationRule"),
            input_refs=tuple(refs),
        )
