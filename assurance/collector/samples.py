"""The raw measurement sample envelope.

Complete module: pure data with constructor-enforced invariants, no owner.

Design section 4.5 fixes the field list and the rule:

    The Measurement Collector delivers raw counters, scope snapshots,
    timestamps, clock health, missing intervals, and trace hashes directly to
    the Kernel.  Agent summaries cannot substitute for raw measurements.

Every field is one way a KPI number can be true and useless:

``scope_snapshot``
    Who the number is about, captured at observation time.  A UE attaching
    mid-window changes the denominator; a snapshot makes that visible instead
    of silently averaging over a different population.
``observed_at`` / ``cadence_ms``
    When, and how often.  A value with no cadence cannot be checked against a
    measurement contract's window geometry.
``clock_health``
    Whether the timestamp can be trusted to correlate with the trial window at
    all.  A drifting collector produces well-formed samples that align with
    the wrong trial.
``missing_intervals``
    Gaps, carried explicitly.  Design section 8 requires a conservative
    contract-defined charge for them and forbids zero or last-value
    substitution -- which is only possible if the gap is reported rather than
    interpolated away.
``trace_hash``
    Digest of the underlying raw trace, so a verdict is walkable back to the
    bytes that produced it (design section 17.5).

The constructor enforces the one rule that the type system cannot: the value's
provenance must be
:attr:`~assurance.core.provenance.Provenance.MEASURED` and its source
component must be the collector.  A ``DERIVED`` number arriving on this path
would be a summary wearing a measurement's envelope, which is exactly what
section 4.5 forbids.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Tuple

from assurance.core.addressing import content_hash, is_content_hash
from assurance.core.components import ComponentId
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.core.timebase import is_utc_timestamp, parse_utc

__all__ = ["ClockHealth", "MissingInterval", "RawSample"]


class ClockHealth(Enum):
    """How much the collector's timestamps can be trusted.

    ``UNKNOWN`` is fail-closed and is the default when the source does not
    report clock state: an unstated clock is not a healthy one, and a window
    evaluated on unstated timestamps is
    :attr:`~assurance.core.axes.MeasurementSufficiency.CLOCK_UNHEALTHY`.
    """

    SYNCHRONISED = "SYNCHRONISED"
    #: Drifting but within the measurement contract's bound.
    DRIFTING_WITHIN_BOUND = "DRIFTING_WITHIN_BOUND"
    DRIFTING_OUT_OF_BOUND = "DRIFTING_OUT_OF_BOUND"
    UNSYNCHRONISED = "UNSYNCHRONISED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class MissingInterval:
    """A gap in the observation, reported rather than filled.

    ``reason`` is recorded because the gaps are not equivalent: a collector
    restart, a detached UE and a dropped KPM subscription have different
    conservative charges under a measurement contract, and different meanings
    in the paper's telemetry-gap statistics (design section 13).
    """

    start: str
    end: str
    reason: str = ""

    def __post_init__(self) -> None:
        if not is_utc_timestamp(self.start) or not is_utc_timestamp(self.end):
            raise ValueError("missing-interval bounds must be canonical UTC timestamps")
        if parse_utc(self.end) <= parse_utc(self.start):
            raise ValueError("missing-interval end must be after start")

    def duration_ms(self) -> int:
        """Length of the gap in milliseconds."""
        delta = parse_utc(self.end) - parse_utc(self.start)
        return int(delta.total_seconds() * 1000)

    def to_canonical_dict(self) -> Dict[str, Any]:
        """Canonical wire form."""
        return {"start": self.start, "end": self.end, "reason": self.reason}


@dataclass(frozen=True)
class RawSample:
    """One raw observation on its way from the collector to the Kernel.

    Attributes
    ----------
    sample_id:
        Unique id, used for deduplication and for evidence references.
    counter_id:
        The contract counter this observes, resolved through a
        :class:`~assurance.contracts.measurement.CounterBinding`.
    value:
        The observation.  Must carry
        :attr:`~assurance.core.provenance.Provenance.MEASURED`.
    scope_snapshot:
        Entity identity at observation time, e.g.
        ``{"cellId": "87654321", "ueId": "130"}``.
    observed_at:
        Canonical UTC instant of the observation.
    cadence_ms:
        Reporting cadence this sample belongs to.  Positive.
    clock_health:
        See :class:`ClockHealth`.
    missing_intervals:
        Gaps covering the reporting interval, in ascending order and
        non-overlapping.  Empty for a complete interval.
    trace_hash:
        Digest of the raw trace this was read from.
    sequence:
        Per-counter monotonic counter, so a dropped or reordered sample is
        detectable at the Kernel boundary.
    source_component:
        Always
        :attr:`~assurance.core.components.ComponentId.MEASUREMENT_COLLECTOR`;
        enforced, so an agent cannot deliver a "measurement".
    """

    sample_id: str
    counter_id: str
    value: TypedQuantity
    scope_snapshot: Mapping[str, str]
    observed_at: str
    cadence_ms: int
    clock_health: ClockHealth
    trace_hash: str
    sequence: int
    missing_intervals: Tuple[MissingInterval, ...] = ()
    source_component: ComponentId = ComponentId.MEASUREMENT_COLLECTOR

    def __post_init__(self) -> None:
        for name in ("sample_id", "counter_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string, got {value!r}")
        if not isinstance(self.value, TypedQuantity):
            raise TypeError("value must be a TypedQuantity")
        if self.value.provenance is not Provenance.MEASURED:
            raise ValueError(
                "a raw sample carries a MEASURED value; "
                f"{self.value.provenance.value} would be a summary, not a measurement "
                "(design section 4.5)"
            )
        if not isinstance(self.scope_snapshot, Mapping):
            raise TypeError("scope_snapshot must be a mapping")
        object.__setattr__(
            self,
            "scope_snapshot",
            {str(key): str(item) for key, item in self.scope_snapshot.items()},
        )
        if not is_utc_timestamp(self.observed_at):
            raise ValueError(f"observed_at must be canonical UTC, got {self.observed_at!r}")
        if isinstance(self.cadence_ms, bool) or not isinstance(self.cadence_ms, int):
            raise TypeError("cadence_ms must be an int")
        if self.cadence_ms <= 0:
            raise ValueError("cadence_ms must be positive; a sample with no cadence "
                             "cannot be checked against a window")
        if not isinstance(self.clock_health, ClockHealth):
            raise TypeError("clock_health must be a ClockHealth member")
        if not is_content_hash(self.trace_hash):
            raise ValueError(f"trace_hash is not a digest: {self.trace_hash!r}")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("sequence must be an int")
        if self.sequence < 0:
            raise ValueError("sequence must be >= 0")
        object.__setattr__(self, "missing_intervals", tuple(self.missing_intervals))
        previous_end = None
        for interval in self.missing_intervals:
            if not isinstance(interval, MissingInterval):
                raise TypeError("missing_intervals entries must be MissingInterval")
            if previous_end is not None and parse_utc(interval.start) < previous_end:
                raise ValueError(
                    "missing_intervals must be ascending and non-overlapping; "
                    "overlapping gaps would be charged twice"
                )
            previous_end = parse_utc(interval.end)
        if self.source_component is not ComponentId.MEASUREMENT_COLLECTOR:
            raise ValueError(
                "a raw sample comes from the Measurement Collector; agent summaries "
                "cannot substitute for raw measurements (design section 4.5)"
            )

    # -- freshness and completeness ---------------------------------------

    def is_fresh(self, now: str, *, freshness_bound_ms: int) -> bool:
        """True when the observation is newer than the contract's bound.

        A future-dated sample is *not* fresh: a timestamp ahead of *now* means
        the clock is wrong, and treating it as maximally fresh would let a
        drifting collector satisfy every freshness check.
        """
        age_ms = (parse_utc(now) - parse_utc(self.observed_at)).total_seconds() * 1000
        return 0 <= age_ms <= freshness_bound_ms

    def has_gaps(self) -> bool:
        """True when any interval is missing."""
        return bool(self.missing_intervals)

    def missing_ms(self) -> int:
        """Total missing time in milliseconds."""
        return sum(interval.duration_ms() for interval in self.missing_intervals)

    # -- content addressing ------------------------------------------------

    def to_canonical_dict(self) -> Dict[str, Any]:
        """Canonical wire form, in the design's camelCase spelling."""
        return {
            "sampleId": self.sample_id,
            "counterId": self.counter_id,
            "value": self.value.to_canonical_dict(),
            "scopeSnapshot": dict(self.scope_snapshot),
            "observedAt": self.observed_at,
            "cadenceMs": self.cadence_ms,
            "clockHealth": self.clock_health.value,
            "traceHash": self.trace_hash,
            "sequence": self.sequence,
            "missingIntervals": [i.to_canonical_dict() for i in self.missing_intervals],
            "sourceComponent": self.source_component.value,
        }

    def content_hash(self) -> str:
        """Digest of the sample, recorded in the ingest event."""
        return content_hash(self.to_canonical_dict())
