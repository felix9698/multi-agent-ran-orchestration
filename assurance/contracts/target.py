"""Target Contract, Target Option, Target Vector and Target Release Policy.

Owner lane: **KCON**.

Design section 6.2 lists these three together, and section 6.4 explains why
the vector is a first-class object rather than a list of goals: the Operator
confirms *the complete ordered target-vector list before execution*, and the
Kernel may activate the next vector only against a valid exhaustion
certificate for the current one.  Order is content, so it is hashed and
confirmed with everything else.

The seven objective families of design section 10 are target contracts, not
subclasses.  ``PIN_TO_CELL`` is a target contract too, kept under its exact
existing identifier: section 10 requires it to remain an exact regression path
and forbids silently renaming it into the Traffic Steering family, so any
relationship is expressed through :attr:`TargetContract.related_contracts`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional, Tuple

from assurance.contracts.common import ContractIdentity, frozen_mapping, frozen_tuple
from assurance.core.provenance import TypedQuantity

__all__ = [
    "ComparisonOperator",
    "TargetContract",
    "TargetOption",
    "TargetPredicate",
    "TargetReleasePolicy",
    "TargetVector",
    "TypedConstraint",
]


class ComparisonOperator(Enum):
    """How a measured value is compared with a target value."""

    GREATER_OR_EQUAL = "GREATER_OR_EQUAL"
    LESS_OR_EQUAL = "LESS_OR_EQUAL"
    EQUAL = "EQUAL"
    NOT_EQUAL = "NOT_EQUAL"
    #: Membership in an enumerated set, e.g. "serving cell in {A, B}".
    MEMBER_OF = "MEMBER_OF"


@dataclass(frozen=True)
class TypedConstraint:
    """A typed constraint over one measured quantity.

    Design section 6.2 requires capability and composition manifests to carry
    typed constraints; the same type is used for target predicates, because a
    constraint on what a capability can do and a constraint on what the trial
    must achieve are the same shape and must be comparable without conversion.

    ``bound`` is a :class:`~assurance.core.provenance.TypedQuantity`, so a
    constraint carries the unit and provenance of its own threshold.  That is
    what stops an ``ILLUSTRATIVE`` figure from a slide becoming a success
    threshold (task section 5.5).
    """

    #: The measurement contract id whose output is constrained.
    measurement_ref: str
    operator: ComparisonOperator
    bound: TypedQuantity
    #: For ``MEMBER_OF``: the permitted values.  Empty otherwise.
    allowed_values: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_values", frozen_tuple(self.allowed_values))


@dataclass(frozen=True)
class TargetPredicate:
    """One condition a trial is judged against.

    ``mandatory`` is the field that carries design section 7's success rule:
    "Success requires all mandatory predicates to pass in the same live trial
    and validity region through the full hold period."  A non-mandatory
    predicate is recorded and reported but cannot by itself deny success.
    """

    predicate_id: str
    constraint: TypedConstraint
    mandatory: bool = True
    #: Human-readable statement for the Cockpit.  Display only; it never
    #: participates in evaluation.
    description: str = ""


@dataclass(frozen=True)
class TargetOption(ContractIdentity):
    """One admissible way to pursue a target.

    An option is what the finite candidate catalog is generated over (design
    section 6.3 freezes "candidate generator version, universe cardinality,
    membership, semantic hashes, and catalog hash").  It names the capability
    that would act and the parameter space that capability exposes -- never a
    command payload, which only the Write Gateway may construct.
    """

    #: Capability manifest id that would perform this option.
    capability_ref: str
    #: Bounded parameter space, e.g. ``{"targetCellId": ["87654321", "12345678"]}``.
    #: Finite by construction: an unbounded range would make the catalog
    #: cardinality undefined.
    parameter_space: Mapping[str, Tuple[str, ...]]
    #: Constraints the option itself must satisfy to be applicable.
    preconditions: Tuple[TypedConstraint, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self,
            "parameter_space",
            {key: frozen_tuple(values) for key, values in frozen_mapping(self.parameter_space).items()},
        )
        object.__setattr__(self, "preconditions", frozen_tuple(self.preconditions))


@dataclass(frozen=True)
class TargetContract(ContractIdentity):
    """What one objective means, in measurable terms.

    Attributes
    ----------
    objective_family:
        Project contract identifier of the objective family, e.g.
        ``"TrafficSteeringPreference"`` or ``"PIN_TO_CELL"``.  Section 9: an
        objective name is a project contract identifier, not a standard term;
        the mapping to published versions lives in
        :attr:`~assurance.contracts.common.ContractIdentity.standard_mapping`.
    scope_selector:
        The entities this target applies to.
    predicates:
        The conditions judged at the end of the hold period.
    options:
        The admissible ways to pursue it.
    validity_region:
        Constraints that must hold for the whole trial for its evidence to be
        valid.  Leaving the region is a stop reason that outranks the semantic
        verdict (design section 7), not a failed predicate.
    hold_ms:
        Overall hold requirement for the target, distinct from any single
        measurement contract's hold.
    related_contracts:
        Explicit, versioned relationships to other target contracts -- how
        ``PIN_TO_CELL`` relates to the Traffic Steering family, for example
        (design section 10).
    """

    objective_family: str
    scope_selector: Mapping[str, str]
    predicates: Tuple[TargetPredicate, ...]
    options: Tuple[TargetOption, ...]
    validity_region: Tuple[TypedConstraint, ...] = ()
    hold_ms: int = 0
    related_contracts: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "scope_selector", frozen_mapping(self.scope_selector))
        object.__setattr__(self, "predicates", frozen_tuple(self.predicates))
        object.__setattr__(self, "options", frozen_tuple(self.options))
        object.__setattr__(self, "validity_region", frozen_tuple(self.validity_region))
        object.__setattr__(self, "related_contracts", frozen_mapping(self.related_contracts))


@dataclass(frozen=True)
class TargetVector(ContractIdentity):
    """An ordered list of target contracts the Operator confirmed together.

    Order is normative (design section 6.4): the Kernel activates entry *n+1*
    only when entry *n* has a valid exhaustion certificate under the current
    frozen catalog.  ``EVIDENCE_INCOMPLETE`` is the answer when obligations
    remain -- see :func:`assurance.core.axes.aggregate_from_cells`.
    """

    #: Target contract ids in confirmed order.
    ordered_target_refs: Tuple[str, ...]
    #: Optional logical Intent Profile this vector belongs to.  A profile is
    #: experiment data, not a human, approver or authority (design 4.1).
    intent_profile_id: Optional[str] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self, "ordered_target_refs", frozen_tuple(self.ordered_target_refs)
        )


@dataclass(frozen=True)
class TargetReleasePolicy(ContractIdentity):
    """When the Kernel may move from one target vector to the next.

    Attributes
    ----------
    require_exhaustion_certificate:
        Design section 6.4's rule.  ``True`` in every shipped policy; the
        field exists so a replay fixture can express a historical run that
        predates the rule, not so a live case can switch it off.
    seal_dormant_evidence:
        Whether evidence for not-yet-active vectors stays sealed from the
        planning and agent view (design section 8).
    max_active_vectors:
        Structural guard: more than one active vector would let two targets
        charge the same harm reserve concurrently.
    """

    require_exhaustion_certificate: bool = True
    seal_dormant_evidence: bool = True
    max_active_vectors: int = 1
