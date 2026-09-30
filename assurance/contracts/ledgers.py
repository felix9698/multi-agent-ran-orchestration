"""Evidence Ledger, Harm Ledger, compatibility records and contributions.

Owner lane: **KCON** (record types); the ledgers themselves are written only
by the Kernel, and only through the settlement event.

Design section 6.2 lists "Evidence Ledger, Harm Ledger, compatibility records,
and contributions"; section 8 fixes the rules these record types have to make
expressible:

* invalid, unprovable or insufficient traces do not fill closure quotas;
* the same trace or dependency group cannot be counted as independent trials;
* dormant evidence stays sealed until its target vector is active;
* a historical pass is provisional until a full confirmation trial;
* cross-epoch reuse needs deterministic compatibility checks over semantics,
  contracts, scope, baseline, environment, TTL, drift, complete windows/hold
  and evidence dependency;
* a compatible pass after a closed fail is appended as a post-closure witness
  and does not rewrite history.

The dependency-group rule is why :attr:`EvidenceContribution.dependency_group`
exists.  Two trials that shared a measurement window, a baseline or a
calibration run are not independent evidence, and a ledger that only counted
rows would never notice.

The ledgers are append-only.  There is no update or delete on these records --
a correction is another record, and a post-closure witness is an append.  That
is also the cutover for GAP-05 in ``docs/architecture/GATE1-MAP.md``, where the
legacy path constructed a fresh reserve ledger per episode so accounting
restarted whenever the context did.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Tuple

from assurance.contracts.common import frozen_mapping, frozen_tuple
from assurance.contracts.harm import HarmKind
from assurance.core.axes import (
    EvidenceCellStatus,
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
)
from assurance.core.provenance import TypedQuantity

__all__ = [
    "CompatibilityCheck",
    "CompatibilityRecord",
    "EvidenceCell",
    "EvidenceContribution",
    "EvidenceLedgerRecord",
    "HarmLedgerRecord",
    "MovementKind",
    "ReserveMovement",
]


class CompatibilityCheck(Enum):
    """The nine deterministic checks cross-epoch reuse must pass.

    Design section 8 enumerates them; task section 5.15 repeats the list.  All
    nine must pass -- ``ALL`` is not a member, and a partially compatible
    contribution is not admitted at a reduced weight.  Reporting them
    individually is what lets the Cockpit and the paper say *why* a reuse was
    refused (design section 13's "evidence reuse" statistics).
    """

    SEMANTICS = "SEMANTICS"
    CONTRACTS = "CONTRACTS"
    SCOPE = "SCOPE"
    BASELINE = "BASELINE"
    ENVIRONMENT = "ENVIRONMENT"
    TTL = "TTL"
    DRIFT = "DRIFT"
    COMPLETE_WINDOW_AND_HOLD = "COMPLETE_WINDOW_AND_HOLD"
    EVIDENCE_DEPENDENCY = "EVIDENCE_DEPENDENCY"


@dataclass(frozen=True)
class EvidenceContribution:
    """One trial's contribution to one evidence obligation.

    Attributes
    ----------
    contribution_id / trial_ref / candidate_semantic_hash:
        What produced it and what it is about.  The semantic hash rather than
        the candidate id, so a contribution stays meaningful across epochs.
    execution_validity / measurement_sufficiency / predicate_verdict:
        The three axes recorded separately (design section 8).  Whether this
        contribution counts toward closure is decided by
        :func:`assurance.core.axes.counts_toward_closure`, never by reading
        the verdict alone.
    trace_refs:
        Raw trace identifiers.  Reused traces are detectable because the same
        reference appears twice.
    dependency_group:
        Identifier shared by contributions that are not independent -- same
        measurement window, same baseline, same calibration.  Contributions in
        one group count once.
    is_post_closure_witness:
        Appended after the cell closed.  Recorded, never counted into the
        closed quota (design section 8).
    reused_from_epoch:
        Set when this contribution is a derived reuse from an earlier epoch;
        paired with a :class:`CompatibilityRecord`.
    """

    contribution_id: str
    trial_ref: str
    candidate_semantic_hash: str
    execution_validity: ExecutionValidity
    measurement_sufficiency: MeasurementSufficiency
    predicate_verdict: PredicateVerdict
    trace_refs: Tuple[str, ...]
    dependency_group: Optional[str] = None
    is_post_closure_witness: bool = False
    reused_from_epoch: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace_refs", frozen_tuple(self.trace_refs))


@dataclass(frozen=True)
class EvidenceCell:
    """One evidence obligation and its current status.

    ``required_independent_contributions`` is the closure quota.  It counts
    *independent* contributions -- distinct dependency groups -- which is why
    the count cannot be taken from ``len(contributions)``.
    """

    cell_id: str
    target_ref: str
    candidate_semantic_hash: str
    status: EvidenceCellStatus
    required_independent_contributions: int
    contributions: Tuple[EvidenceContribution, ...] = ()
    #: Set while the owning target vector is not yet active (design 8).
    sealed_until_vector_ref: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "contributions", frozen_tuple(self.contributions))


@dataclass(frozen=True)
class EvidenceLedgerRecord:
    """One append-only entry in the Evidence Ledger.

    A record is a *statement about* a cell at a point in the event stream, not
    the cell's mutable current value.  Replaying the records in order
    reproduces the ledger (design section 4.3), which is only true if each
    record is self-contained.
    """

    record_id: str
    event_ref: str
    epoch_ref: str
    case_ref: str
    cell: EvidenceCell
    #: The contribution this record adds, if any.  ``None`` for a record that
    #: only changes a cell's status -- sealing, budget-locking, closing.
    added_contribution: Optional[EvidenceContribution] = None
    #: Prior status, so a replay can verify the transition rather than assume
    #: the record was applied to the state it expected.
    previous_status: Optional[EvidenceCellStatus] = None


class MovementKind(Enum):
    """Direction of a harm-reserve movement.

    Three kinds, matching design section 7: reserve is taken before the trial
    (step 5), then charged or returned by the single settlement event
    (step 10).  All three are recorded, so the reserve at any point in the
    stream is reconstructible by replay instead of being trusted as a running
    total.
    """

    RESERVE = "RESERVE"
    CHARGE = "CHARGE"
    RETURN = "RETURN"


@dataclass(frozen=True)
class ReserveMovement:
    """A single movement of harm reserve, with its amount and reason."""

    movement_id: str
    kind: MovementKind
    amount: TypedQuantity
    reason: str


@dataclass(frozen=True)
class HarmLedgerRecord:
    """One append-only entry in the Harm Ledger.

    Attributes
    ----------
    harm_kind:
        Which bucket (task section 5.7).  Target debt never nets against
        trial-induced harm.
    movement:
        The reserve movement this record makes.
    charged_for_missing_interval:
        True when the amount is the contract's conservative substitute for a
        gap rather than an observation (design section 8).  Recorded so the
        paper can report how much of the charge was measured and how much was
        conservatively assumed.
    epoch_ref / case_ref / trial_ref:
        Keys.  The ledger is keyed by contract, case and epoch so an epoch or
        release transition is an explicit, auditable event and cannot erase
        charges or trial counts.
    """

    record_id: str
    event_ref: str
    epoch_ref: str
    case_ref: str
    harm_contract_ref: str
    harm_kind: HarmKind
    movement: ReserveMovement
    trial_ref: Optional[str] = None
    charged_for_missing_interval: bool = False


@dataclass(frozen=True)
class CompatibilityRecord:
    """The result of the nine cross-epoch reuse checks.

    ``admitted`` is stored rather than derived so the record states what the
    Kernel actually did, and a later change to the check set cannot silently
    re-interpret an old record.  A replay compares the two.
    """

    record_id: str
    source_epoch_ref: str
    target_epoch_ref: str
    candidate_semantic_hash: str
    #: Every check and whether it passed.  All nine must be present.
    results: Mapping[str, bool]
    admitted: bool
    #: Why, when ``admitted`` is False.  Display and analysis only.
    refusal_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", frozen_mapping(self.results))
