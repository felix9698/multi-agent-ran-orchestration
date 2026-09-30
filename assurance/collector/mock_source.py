"""A scripted :class:`~assurance.collector.collector.MeasurementCollector`
for Gate 2's hardware-free tests.

Owner lane: **KAGT**.  New file, added under
``docs/architecture/SEAMS-GATE2.md`` section 3.

Design section 4.5's collector delivers raw counters, scope snapshots,
timestamps, clock health, missing intervals and trace hashes straight to the
Kernel.  :class:`MockMeasurementSource` replays a pre-built script of
:class:`~assurance.collector.samples.RawSample` batches -- built by hand or
by one of the four scenario functions below -- so a test can exercise the
Kernel-facing side of the collector protocol without a gNB, an E2
termination or a clock.  Four scenarios cover what design section 4.5 and
task section 5.6 name explicitly: a normal cadence, a gap, clock drift, and a
stale delivery.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from assurance.collector.collector import SampleSink
from assurance.collector.gap_detection import compute_missing_intervals
from assurance.collector.samples import ClockHealth, MissingInterval, RawSample
from assurance.core.addressing import content_hash
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.core.timebase import format_utc, parse_utc

__all__ = [
    "MockMeasurementSource",
    "clock_drift_source",
    "gap_source",
    "normal_timeseries_source",
    "stale_source",
]


class MockMeasurementSource:
    """Replays a fixed, ordered script of :class:`RawSample` batches.

    Constructed with the entire script up front: the same script replayed
    twice produces the same :meth:`poll` sequence, matching the
    reproducibility the rest of this package requires everywhere else.
    :meth:`poll` releases every not-yet-delivered batch whose samples are
    all ``observed_at <= now``, in script order, and forwards each released
    sample to the bound sink (if any) -- so a caller can either read the
    return value directly or bind a sink and let delivery happen as a side
    effect, matching whichever half of the protocol its own test cares
    about.
    """

    def __init__(
        self,
        *,
        script: Sequence[Sequence[RawSample]],
        scope: Optional[Mapping[str, str]] = None,
        source_id: str = "mock-measurement-source",
    ) -> None:
        self._script: Tuple[Tuple[RawSample, ...], ...] = tuple(
            tuple(batch) for batch in script
        )
        self._cursor = 0
        self._sink: Optional[SampleSink] = None
        self._scope: Mapping[str, str] = dict(scope or {})
        self._clock_health: ClockHealth = ClockHealth.UNKNOWN
        self._source_id = source_id

    def bind_sink(self, sink: SampleSink) -> None:
        """Bind the Kernel's ingest callback.  Exactly one; a second raises."""
        if self._sink is not None:
            raise RuntimeError(
                "a sink is already bound; a collector delivers to exactly one "
                "Kernel ingest point (design section 4.5)"
            )
        self._sink = sink

    def poll(self, *, now: str) -> Sequence[RawSample]:
        """Every scripted batch not yet released whose samples are all due by *now*."""
        now_instant = parse_utc(now)
        released: List[RawSample] = []
        while self._cursor < len(self._script):
            batch = self._script[self._cursor]
            if any(parse_utc(sample.observed_at) > now_instant for sample in batch):
                break
            self._cursor += 1
            released.extend(batch)
        for sample in released:
            self._scope = dict(sample.scope_snapshot)
            self._clock_health = sample.clock_health
            if self._sink is not None:
                self._sink(sample)
        return tuple(released)

    def clock_health(self) -> ClockHealth:
        """The clock health of the most recently released sample.

        :attr:`~assurance.collector.samples.ClockHealth.UNKNOWN` before the
        first :meth:`poll` -- fail-closed, never a guess.
        """
        return self._clock_health

    def scope_snapshot(self) -> Mapping[str, str]:
        """The scope of the most recently released sample, or the
        constructor-provided base scope before the first :meth:`poll`."""
        return dict(self._scope)

    def describe_source(self) -> Mapping[str, Any]:
        """Static provenance for the run's environment record.  No credential."""
        return {
            "sourceId": self._source_id,
            "kind": "mock",
            "scriptBatchCount": len(self._script),
            "scriptSampleCount": sum(len(batch) for batch in self._script),
        }


def _sample(
    *,
    sample_id: str,
    counter_id: str,
    value: float,
    unit: str,
    scope: Mapping[str, str],
    observed_at: str,
    cadence_ms: int,
    clock_health: ClockHealth,
    sequence: int,
    source_record: str,
    missing_intervals: Tuple[MissingInterval, ...] = (),
) -> RawSample:
    return RawSample(
        sample_id=sample_id,
        counter_id=counter_id,
        value=TypedQuantity(value, unit, Provenance.MEASURED, source_record),
        scope_snapshot=scope,
        observed_at=observed_at,
        cadence_ms=cadence_ms,
        clock_health=clock_health,
        trace_hash=content_hash({"sampleId": sample_id, "counterId": counter_id}),
        sequence=sequence,
        missing_intervals=missing_intervals,
    )


def normal_timeseries_source(
    *,
    counter_id: str = "dl_throughput",
    scope: Mapping[str, str],
    start: str,
    cadence_ms: int = 1000,
    count: int = 5,
    value: float = 3.4,
    unit: str = "Mbps",
    source_id: str = "mock-normal",
) -> MockMeasurementSource:
    """A healthy, gap-free, synchronised-clock time series."""
    start_instant = parse_utc(start)
    step = timedelta(milliseconds=cadence_ms)
    script = [
        (
            _sample(
                sample_id=f"{source_id}-{i}", counter_id=counter_id, value=value, unit=unit,
                scope=scope, observed_at=format_utc(start_instant + step * i),
                cadence_ms=cadence_ms, clock_health=ClockHealth.SYNCHRONISED,
                sequence=i, source_record=source_id,
            ),
        )
        for i in range(count)
    ]
    return MockMeasurementSource(script=script, scope=scope, source_id=source_id)


def gap_source(
    *,
    counter_id: str = "dl_throughput",
    scope: Mapping[str, str],
    start: str,
    cadence_ms: int = 1000,
    count: int = 5,
    gap_at: int = 2,
    value: float = 3.4,
    unit: str = "Mbps",
    source_id: str = "mock-gap",
    reason: str = "collector restart",
) -> MockMeasurementSource:
    """The same series as :func:`normal_timeseries_source`, with the sample at
    index *gap_at* dropped and the next sample carrying the resulting
    :class:`~assurance.collector.samples.MissingInterval` -- computed by
    :func:`~assurance.collector.gap_detection.compute_missing_intervals`, not
    hand-built, so the scenario and the gap arithmetic cannot disagree.
    """
    if not (0 <= gap_at < count):
        raise ValueError("gap_at must index a sample within [0, count)")
    start_instant = parse_utc(start)
    step = timedelta(milliseconds=cadence_ms)
    script = []
    sequence = 0
    for i in range(count):
        if i == gap_at:
            continue
        observed_at = format_utc(start_instant + step * i)
        missing: Tuple[MissingInterval, ...] = ()
        if i == gap_at + 1:
            gap_instant = format_utc(start_instant + step * gap_at)
            missing = compute_missing_intervals(
                window_start=gap_instant, window_end=gap_instant,
                cadence_ms=cadence_ms, observed_at=(), reason=reason,
            )
        script.append((
            _sample(
                sample_id=f"{source_id}-{i}", counter_id=counter_id, value=value, unit=unit,
                scope=scope, observed_at=observed_at, cadence_ms=cadence_ms,
                clock_health=ClockHealth.SYNCHRONISED, sequence=sequence,
                source_record=source_id, missing_intervals=missing,
            ),
        ))
        sequence += 1
    return MockMeasurementSource(script=script, scope=scope, source_id=source_id)


def clock_drift_source(
    *,
    counter_id: str = "dl_throughput",
    scope: Mapping[str, str],
    start: str,
    cadence_ms: int = 1000,
    count: int = 5,
    drift_at: int = 2,
    drifted_health: ClockHealth = ClockHealth.DRIFTING_OUT_OF_BOUND,
    value: float = 3.4,
    unit: str = "Mbps",
    source_id: str = "mock-clock-drift",
) -> MockMeasurementSource:
    """A series whose clock health degrades to *drifted_health* from index
    *drift_at* onward -- no gap, no missing interval, just timestamps that
    stop being trustworthy (design section 4.5:
    :attr:`~assurance.core.axes.MeasurementSufficiency.CLOCK_UNHEALTHY`).
    """
    start_instant = parse_utc(start)
    step = timedelta(milliseconds=cadence_ms)
    script = [
        (
            _sample(
                sample_id=f"{source_id}-{i}", counter_id=counter_id, value=value, unit=unit,
                scope=scope, observed_at=format_utc(start_instant + step * i),
                cadence_ms=cadence_ms,
                clock_health=ClockHealth.SYNCHRONISED if i < drift_at else drifted_health,
                sequence=i, source_record=source_id,
            ),
        )
        for i in range(count)
    ]
    return MockMeasurementSource(script=script, scope=scope, source_id=source_id)


def stale_source(
    *,
    counter_id: str = "dl_throughput",
    scope: Mapping[str, str],
    observed_at: str,
    cadence_ms: int = 1000,
    value: float = 3.4,
    unit: str = "Mbps",
    source_id: str = "mock-stale",
) -> MockMeasurementSource:
    """A single sample, timestamped long before the ``now`` a caller will
    poll with.  Still returned by :meth:`MockMeasurementSource.poll` --
    never dropped -- so the caller sees it and can apply
    :meth:`~assurance.collector.samples.RawSample.is_fresh` itself; a
    collector deciding staleness on the source's behalf would be the
    aggregation design section 4.5 forbids it from doing.
    """
    sample = _sample(
        sample_id=f"{source_id}-0", counter_id=counter_id, value=value, unit=unit,
        scope=scope, observed_at=observed_at, cadence_ms=cadence_ms,
        clock_health=ClockHealth.SYNCHRONISED, sequence=0, source_record=source_id,
    )
    return MockMeasurementSource(script=[(sample,)], scope=scope, source_id=source_id)
