"""Pure missing-interval arithmetic for an expected-cadence observation window.

Owner lane: **KAGT**.  New file, added under
``docs/architecture/SEAMS-GATE2.md`` section 3.

Design section 8 forbids filling a gap with zero or the last value: a gap
must come back as an explicit
:class:`~assurance.collector.samples.MissingInterval`, never interpolated
away.  :func:`compute_missing_intervals` is the arithmetic that finds those
gaps -- given a window and a cadence, which expected observation instants
have nothing near them -- so a collector implementation does not have to
hand-roll gap math to honour that rule.

Pure and total: no clock read, no I/O, deterministic in its inputs.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Sequence, Tuple

from assurance.collector.samples import MissingInterval
from assurance.core.timebase import format_utc, parse_utc

__all__ = ["compute_missing_intervals"]


def compute_missing_intervals(
    *,
    window_start: str,
    window_end: str,
    cadence_ms: int,
    observed_at: Sequence[str],
    reason: str = "no observation",
) -> Tuple[MissingInterval, ...]:
    """Gaps in an expected-cadence window with no matching observation.

    Generates the expected observation instants from *window_start* to
    *window_end* (inclusive) stepped by *cadence_ms*, and reports every
    maximal run of consecutive expected instants with no *observed_at*
    timestamp within half a cadence of it as one
    :class:`~assurance.collector.samples.MissingInterval` -- spanning from
    the first missed instant of the run to one cadence past the last missed
    instant of that same run, so every interval this function returns has a
    positive duration by construction.

    An empty result means every expected instant in the window was observed.
    It does not mean the window had no expected instants: a window narrower
    than one cadence has an empty expected-instant list to begin with, and
    also returns ``()``.

    Never fills a gap with zero or a repeated value -- the one thing this
    function does is turn "nothing was observed here" into a reported
    record rather than a silent absence.
    """
    if cadence_ms <= 0:
        raise ValueError("cadence_ms must be positive")
    start = parse_utc(window_start)
    end = parse_utc(window_end)
    if end < start:
        raise ValueError("window_end must not be before window_start")

    step = timedelta(milliseconds=cadence_ms)
    tolerance = step / 2
    observed = sorted(parse_utc(timestamp) for timestamp in observed_at)

    expected = []
    cursor = start
    while cursor <= end:
        expected.append(cursor)
        cursor += step

    def _has_nearby_observation(instant) -> bool:
        return any(abs((ts - instant).total_seconds()) <= tolerance.total_seconds() for ts in observed)

    missing_runs = []
    run_start = None
    run_last = None
    for instant in expected:
        if _has_nearby_observation(instant):
            if run_start is not None:
                missing_runs.append((run_start, run_last))
                run_start = None
                run_last = None
        else:
            if run_start is None:
                run_start = instant
            run_last = instant
    if run_start is not None:
        missing_runs.append((run_start, run_last))

    return tuple(
        MissingInterval(format_utc(run_start), format_utc(run_last + step), reason)
        for run_start, run_last in missing_runs
    )
