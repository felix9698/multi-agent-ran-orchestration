"""Harm Contract, harm bound and Watchdog Contract.

Owner lane: **KCON**.

Design section 6.2 keeps the existing type name ``CertifiedHarmBound`` for
compatibility and then removes what used to make it "certified":

    its validity comes from deterministic proof, enforced runtime bounds,
    calibration records, operating scope, uncertainty, conservative margin,
    and content validation -- not an authority signature.

Every one of those is a field on :class:`CertifiedHarmBound`, and there is no
signer, no approval and no authority anywhere in this module.  Task section 5.8
adds the rule that makes the fields load-bearing: an observed sample maximum is
not a hard bound.  A bound needs an *enforced* timeout, stated uncertainty, a
conservative margin, an operating scope it is only claimed inside, a watchdog
that stops the trial, and calibration evidence.

Task section 5.7 separates target debt from harm: falling short of a target is
not the same as damaging a neighbour, and collapsing them would let a case
trade one for the other silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Tuple

from assurance.contracts.common import ContractIdentity, frozen_mapping, frozen_tuple
from assurance.contracts.target import TypedConstraint
from assurance.core.provenance import TypedQuantity

__all__ = [
    "CertifiedHarmBound",
    "HarmContract",
    "HarmKind",
    "WatchdogAction",
    "WatchdogContract",
]


class HarmKind(Enum):
    """What kind of cost a charge represents (task section 5.7)."""

    #: Cost imposed by running the trial itself -- degradation of a
    #: non-target UE, throughput lost to a steering change, time spent in a
    #: degraded configuration.
    TRIAL_INDUCED = "TRIAL_INDUCED"
    #: Cost the contract itself accepts as a standing condition.
    CONTRACT = "CONTRACT"
    #: Shortfall against a target.  Accounted separately and never netted
    #: against the two above.
    TARGET_DEBT = "TARGET_DEBT"


class WatchdogAction(Enum):
    """What the watchdog does when it fires.

    All three end the trial.  None of them wait for an agent or a predicate
    evaluator: design section 7 puts watchdog action above semantic KPI
    success in the fixed precedence.
    """

    #: Stop, reverse rollback, recovery reread.
    STOP_AND_ROLLBACK = "STOP_AND_ROLLBACK"
    #: Drive the deployment to its contracted safe state immediately.
    EMERGENCY_SAFE_STATE = "EMERGENCY_SAFE_STATE"
    #: Stop and refuse further trials until the incident is resolved.
    INCIDENT_LOCKDOWN = "INCIDENT_LOCKDOWN"


@dataclass(frozen=True)
class CertifiedHarmBound:
    """A bound on harm that may be used for admission.

    The name is retained for compatibility with existing contract artefacts
    (design section 6.2); nothing about it involves a certifying party.

    Attributes
    ----------
    bound_id:
        Identifier referenced by harm contracts and reserve ledgers.
    measured_bound:
        The quantity observed during calibration.  On its own this is *not*
        admissible as a hard bound (task section 5.8).
    conservative_margin:
        Margin added to :attr:`measured_bound` to reach
        :attr:`admissible_bound`.
    admissible_bound:
        The value the Kernel may actually admit against.  Recorded explicitly
        rather than computed at read time so the epoch freezes the number that
        was used, not a formula that might be re-evaluated differently.
    uncertainty_ref:
        Reference to the measurement contract's uncertainty rule that this
        margin was derived from.
    operating_scope:
        The conditions the bound is claimed inside -- PRB profile, UE count,
        traffic profile, RF conditions.  Outside them the bound is not
        claimed at all rather than extrapolated.
    enforced_timeout_ms:
        The runtime timeout that *makes* the bound true, not a hope that it
        will be.  Design section 6.2's "enforced runtime bounds".
    calibration_records:
        References to the raw calibration evidence.
    proof_ref:
        Reference to the deterministic proof or derivation that ties the
        records, margin and timeout to :attr:`admissible_bound`.
    """

    bound_id: str
    measured_bound: TypedQuantity
    conservative_margin: TypedQuantity
    admissible_bound: TypedQuantity
    uncertainty_ref: str
    operating_scope: Mapping[str, str]
    enforced_timeout_ms: int
    calibration_records: Tuple[str, ...] = ()
    proof_ref: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "operating_scope", frozen_mapping(self.operating_scope))
        object.__setattr__(
            self, "calibration_records", frozen_tuple(self.calibration_records)
        )


@dataclass(frozen=True)
class WatchdogContract(ContractIdentity):
    """A guard that must be armed before apply and stays armed until the end.

    Task section 6.3 and 6.5: apply is permitted only once every required
    watchdog is armed, and the guards stay live from apply through recovery
    verification or live finalization.  ``arm_before_apply`` is therefore
    ``True`` in every shipped watchdog; the field exists so the requirement is
    stated in data that a test can read.
    """

    watchdog_id: str
    #: The condition that fires the watchdog.
    trigger: TypedConstraint
    action: WatchdogAction
    #: How long the trigger must hold before firing.  Zero means immediate.
    debounce_ms: int = 0
    #: Hard ceiling on evaluation latency; exceeding it fires the watchdog,
    #: so a stalled evaluator cannot silently disarm the guard.
    max_evaluation_latency_ms: int = 1000
    arm_before_apply: bool = True


@dataclass(frozen=True)
class HarmContract(ContractIdentity):
    """The harm budget and guards for one target or case.

    Attributes
    ----------
    harm_kind:
        Which accounting bucket charges land in (task section 5.7).
    scope_selector:
        Who can be harmed -- typically the non-target entities.
    reserve:
        The total budget available, with unit and provenance.
    bounds:
        The admissible harm bounds this contract enforces.
    watchdogs:
        Guards that must be armed before apply.
    missing_interval_charge:
        Conservative charge applied to a gap, never zero (design section 8).
    """

    harm_kind: HarmKind
    scope_selector: Mapping[str, str]
    reserve: TypedQuantity
    bounds: Tuple[CertifiedHarmBound, ...]
    watchdogs: Tuple[WatchdogContract, ...]
    missing_interval_charge: TypedQuantity

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "scope_selector", frozen_mapping(self.scope_selector))
        object.__setattr__(self, "bounds", frozen_tuple(self.bounds))
        object.__setattr__(self, "watchdogs", frozen_tuple(self.watchdogs))
