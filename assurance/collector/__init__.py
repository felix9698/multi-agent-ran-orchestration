"""The Measurement Collector (design section 4.5).

Owner lane: **KAGT** for :mod:`assurance.collector.collector`;
:mod:`assurance.collector.samples` is complete and has no owner.

Raw counters, scope snapshots, timestamps, cadence, clock health, missing
intervals and trace hashes, delivered straight to the Kernel.  An agent summary
is never a substitute for a raw measurement, and no sample passes through an
agent on its way in.

Gate 2 also ships, in files KAGT added at its own discretion (see
``docs/architecture/SEAMS-GATE2.md`` section 3):

* :mod:`assurance.collector.gap_detection` -- the pure missing-interval
  arithmetic design section 8 requires instead of zero/last-value
  substitution.
* :mod:`assurance.collector.mock_source` -- a scripted
  :class:`~assurance.collector.collector.MeasurementCollector` and four
  scenario builders (normal cadence, gap, clock drift, stale) for
  hardware-free tests.
"""

from __future__ import annotations

from assurance.collector.collector import (
    DELIVERS_TO_KERNEL_ONLY,
    MeasurementCollector,
    SampleSink,
)
from assurance.collector.gap_detection import compute_missing_intervals
from assurance.collector.mock_source import (
    MockMeasurementSource,
    clock_drift_source,
    gap_source,
    normal_timeseries_source,
    stale_source,
)
from assurance.collector.o1col import (
    AfterWindowEvidence,
    KpmJsonlAdapter,
    KpmParseResult,
    O1PmFileAdapter,
    correlate_after_window,
)
from assurance.collector.samples import ClockHealth, MissingInterval, RawSample

__all__ = [
    "DELIVERS_TO_KERNEL_ONLY",
    "ClockHealth",
    "AfterWindowEvidence",
    "KpmJsonlAdapter",
    "KpmParseResult",
    "MeasurementCollector",
    "MissingInterval",
    "MockMeasurementSource",
    "RawSample",
    "O1PmFileAdapter",
    "SampleSink",
    "clock_drift_source",
    "compute_missing_intervals",
    "correlate_after_window",
    "gap_source",
    "normal_timeseries_source",
    "stale_source",
]
