"""Measurement Registry, Measurement Contract and Counter Binding.

Owner lane: **KCON**.

Design section 6.2 requires a content-addressed Measurement Registry and
Measurement Contract; task section 5.6 fixes the field list and is unusually
specific about it:

    Measurement Contract에 counter, scope, membership snapshot, cadence,
    window width/stride, overlap, aggregation, estimator, minimum entity
    count, hold, gap/missing, freshness, clock와 uncertainty rule을 빠짐없이
    포함한다.

Every one of those is a field below.  The list is long because each item is a
way a KPI number can be right in isolation and wrong as evidence: the right
counter over the wrong scope, the right window at the wrong cadence, an
average over two UEs where the contract needed ten, a value that arrived after
the trial ended.  Design section 8 then forbids the two convenient repairs --
"Missing intervals receive conservative contract-defined charge, never zero or
last-value substitution" -- which is why :class:`GapPolicy` has no member for
either of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Tuple

from assurance.contracts.common import ContractIdentity, frozen_mapping, frozen_tuple
from assurance.core.provenance import TypedQuantity

__all__ = [
    "Aggregation",
    "ClockRequirement",
    "CounterBinding",
    "Estimator",
    "GapPolicy",
    "MeasurementContract",
    "MeasurementRegistry",
    "MeasurementSource",
    "OverlapPolicy",
    "UncertaintyRule",
]


class MeasurementSource(Enum):
    """Which interface a counter is read from.

    Kept explicit because correlation rules differ per source: an E2 KPM
    report and an O1 PM file for the same cell do not share a timebase, and
    design section 10 requires QoE evidence to come from a real application or
    UE observation correlated to the trial rather than inferred from RAN
    counters.
    """

    E2_KPM = "E2_KPM"
    O1_PM = "O1_PM"
    CORE_PM = "CORE_PM"
    UE_APPLICATION = "UE_APPLICATION"
    #: Readback of the applied configuration itself, not a performance count.
    CONFIGURATION_READBACK = "CONFIGURATION_READBACK"


class OverlapPolicy(Enum):
    """Whether consecutive evaluation windows may share samples.

    ``DISJOINT`` is the safe default: overlapping windows make two "independent"
    evaluations share a trace, which design section 8 forbids counting as
    independent trials.
    """

    DISJOINT = "DISJOINT"
    SLIDING_ALLOWED = "SLIDING_ALLOWED"


class Aggregation(Enum):
    """How samples inside one window collapse to one number."""

    MEAN = "MEAN"
    MEDIAN = "MEDIAN"
    P95 = "P95"
    P99 = "P99"
    MIN = "MIN"
    MAX = "MAX"
    SUM = "SUM"
    COUNT = "COUNT"
    RATIO = "RATIO"


class Estimator(Enum):
    """Which estimator computes the aggregate.

    Separate from :class:`Aggregation` because "the 95th percentile" does not
    say whether it is the empirical order statistic or an interpolated one,
    and two implementations that disagree produce two verdicts from one trace.
    """

    SAMPLE_MEAN = "SAMPLE_MEAN"
    TRIMMED_MEAN = "TRIMMED_MEAN"
    EMPIRICAL_QUANTILE = "EMPIRICAL_QUANTILE"
    INTERPOLATED_QUANTILE = "INTERPOLATED_QUANTILE"
    RATIO_OF_SUMS = "RATIO_OF_SUMS"


class GapPolicy(Enum):
    """What a missing interval does to the window.

    Two members only.  Design section 8: a gap takes "conservative
    contract-defined charge, never zero or last-value substitution", so there
    is deliberately no ``ZERO_FILL`` and no ``LAST_VALUE_HOLD`` to select.
    """

    #: Charge the contract's conservative substitute value and continue.
    CONSERVATIVE_CHARGE = "CONSERVATIVE_CHARGE"
    #: Discard the window; it cannot support a verdict.
    REJECT_WINDOW = "REJECT_WINDOW"


class ClockRequirement(Enum):
    """How healthy the collector clock must be for the window to count."""

    SYNCHRONISED_REQUIRED = "SYNCHRONISED_REQUIRED"
    DRIFT_BOUNDED = "DRIFT_BOUNDED"


@dataclass(frozen=True)
class UncertaintyRule:
    """How much the measured value may be wrong, and in which direction.

    Task section 5.8 forbids declaring a hard harm bound from an observed
    sample maximum alone; an admission bound needs uncertainty and a
    conservative margin.  This is that uncertainty, attached to the
    measurement rather than to the bound, so every contract that consumes the
    counter inherits the same statement.
    """

    #: Identifier of the uncertainty model, e.g. ``"normal_ci"``,
    #: ``"bounded_absolute"``, ``"quantisation"``.
    model: str
    #: The model's parameter, with unit and provenance.
    parameter: TypedQuantity
    #: Coverage the parameter describes, e.g. ``0.95``.  ``None`` for models
    #: that state an absolute bound rather than a confidence level.
    coverage: Optional[float] = None
    #: Direction the margin must be applied in when the value is used for
    #: admission: ``"upper"``, ``"lower"`` or ``"two_sided"``.
    conservative_direction: str = "two_sided"


@dataclass(frozen=True)
class CounterBinding:
    """Binds one contract counter to one deployment-specific counter.

    Design section 6.2 lists Counter Binding beside Actuator Binding because
    they are the two places where a portable contract meets a particular
    testbed.  The binding carries no credential: reaching the source is the
    Deployment Binding's job (see :mod:`assurance.contracts.capability`).
    """

    #: Contract-side counter identifier, referenced by measurement contracts.
    counter_id: str
    #: Name the counter has on the wire in this deployment.
    deployment_counter_name: str
    source: MeasurementSource
    #: Scope keys this counter is reported per, e.g. ``("cellId", "ueId")``.
    scope_keys: Tuple[str, ...]
    unit: str
    #: Native reporting cadence in milliseconds.
    native_cadence_ms: int
    #: Reference to the Deployment Binding that reaches this source.
    deployment_binding_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_keys", frozen_tuple(self.scope_keys))


@dataclass(frozen=True)
class MeasurementContract(ContractIdentity):
    """One fully specified way to turn raw counters into an evaluable number.

    Attributes
    ----------
    counter_id:
        The contract counter this measures; resolved through a
        :class:`CounterBinding`.
    scope_selector:
        Which entities the measurement covers, e.g.
        ``{"cellId": "87654321"}``.
    membership_snapshot:
        The exact entity identifiers in scope when the epoch froze.  A
        snapshot, not a live query: a UE attaching mid-trial must not silently
        change the denominator.
    cadence_ms:
        Sampling cadence the contract requires, which may be coarser than the
        counter's native cadence.
    window_width_ms / window_stride_ms:
        Evaluation window geometry.
    overlap:
        Whether consecutive windows may share samples.
    aggregation / estimator:
        How a window collapses to one number.
    minimum_entity_count:
        Fewest in-scope entities for the window to be usable.  Below it the
        window is ``INSUFFICIENT_COVERAGE`` on the measurement-sufficiency
        axis, never a value.
    hold_ms:
        How long the condition must hold, in the same live trial and validity
        region, before success may be decided (design section 7).
    gap_policy / missing_interval_charge:
        What a gap costs.  The charge is a
        :class:`~assurance.core.provenance.TypedQuantity` so its own
        provenance is recorded -- a conservative substitute is contract data,
        not a measurement.
    freshness_bound_ms:
        Oldest an observation may be and still count.
    clock_requirement:
        Collector clock health needed for the window to be usable.
    uncertainty_rule:
        See :class:`UncertaintyRule`.
    """

    counter_id: str
    scope_selector: Mapping[str, str]
    membership_snapshot: Tuple[str, ...]
    cadence_ms: int
    window_width_ms: int
    window_stride_ms: int
    overlap: OverlapPolicy
    aggregation: Aggregation
    estimator: Estimator
    minimum_entity_count: int
    hold_ms: int
    gap_policy: GapPolicy
    missing_interval_charge: TypedQuantity
    freshness_bound_ms: int
    clock_requirement: ClockRequirement
    uncertainty_rule: UncertaintyRule

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "scope_selector", frozen_mapping(self.scope_selector))
        object.__setattr__(
            self, "membership_snapshot", frozen_tuple(self.membership_snapshot)
        )


@dataclass(frozen=True)
class MeasurementRegistry(ContractIdentity):
    """The set of counters and measurement contracts an epoch may use.

    A registry rather than a loose collection because design section 6.3
    freezes it: after the epoch is frozen, no measurement contract can be
    added, so an agent cannot invent a KPI that suits its proposal.
    """

    counters: Tuple[CounterBinding, ...]
    measurements: Tuple[MeasurementContract, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "counters", frozen_tuple(self.counters))
        object.__setattr__(self, "measurements", frozen_tuple(self.measurements))
