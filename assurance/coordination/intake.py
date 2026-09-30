"""The intake checklist, the observation rules and the generation options.

Three deterministic things the executor does **around** the model calls, all of
them contract v2:

* **Intake** (section 2.2).  Before any call, is the operator's own input
  complete?  Missing owner, unit, or -- the one that actually bites -- a
  sentence that says nothing at all about relaxation, which is *missing*, not
  a refusal to relax.  The checklist answers in the same
  ``{"intentId", "field", "question"}`` shape the Target agent uses for its own
  ``missingInformation``, so the Cockpit's question form and the headless
  ``--answers`` file serve both.
* **Observation rules** (section 6).  Per KPI kind: how long to let the
  configuration settle, how long a window to measure over, which statistic,
  how much coverage a window needs before it counts, and how long the answer
  stays usable.  ``UNKNOWN`` for a thin window, never a quietly averaged
  number, and every observation carries the moment it stops being valid.
* **Generation options** (section 7).  Nobody cuts a model off mid-thought:
  timeliness comes from the budget the model is *told* about, from lean inputs
  and from calibration.  When a latency calibration is on disk the executor
  picks, per role, the largest budget whose p95 still fits inside the shortest
  validity window; otherwise the defaults here.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from assurance.coordination.tc import (
    Authorization, Intent, KPI_CELL_GOODPUT, KPI_CELL_TX_ATTENUATION, KPI_DEADLINE_RATIO, KPI_GOODPUT, KPI_SERVING_CELL,
    Requirement, UNKNOWN, mode_constraint_from_record,
)

__all__ = [
    "DEFAULT_OBSERVATION_RULES",
    "STATISTIC_RATIO",
    "GENERATION_DEFAULTS",
    "CALIBRATION_ROLES",
    "GenerationOptions",
    "IntakeChecklist",
    "KpiObservationRule",
    "ObservationRules",
    "REQUIRED_SITTING_SETTINGS",
    "SITTING",
    "load_latency_calibration",
    "merge_answers",
]

#: The ``intentId`` a sitting-wide question is filed under.
SITTING = "sitting"

#: What the executor needs from the operator before the sitting may start
#: (contract v2 section 2.2), and what to ask when it is not there.
REQUIRED_SITTING_SETTINGS: Tuple[Tuple[str, str], ...] = (
    ("trialsK", "How many trials may this sitting run?"),
    ("deadlineMs", "Is there a wall-clock deadline for the sitting? "
                   "Answer null for none."),
    ("horizonMs", "Is there a horizon after which the result no longer counts? "
                  "Answer null for none."),
    ("stopAfterRelaxedSuccess",
     "Stop the sitting when a relaxed target succeeds, or keep going for T0?"),
    ("retention", "What is retained at the end: the last applied configuration, "
                  "or the best attained one?"),
)


# --------------------------------------------------------------------------- #
# section 6 -- observation rules
# --------------------------------------------------------------------------- #


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(number) else number


def _parse_time(value: Any) -> Optional[float]:
    """A sample stamp as milliseconds: a number stays, an ISO string is read."""
    number = _as_float(value)
    if number is not None:
        return number
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp() * 1000.0


#: The statistic a deadline-success ratio is aggregated with.  It is named
#: separately because it is **not** a mean of per-sample ratios: the window's
#: value is ``completed_within_D1 / eligible_issued`` over the whole window,
#: and averaging per-sample ratios would weight a window's thin samples the
#: same as its busy ones.
STATISTIC_RATIO = "ratio"
STATISTIC_CONSISTENT = "consistent"

#: The statistic a delivery-continuity requirement is aggregated with: the
#: longest run of consecutive samples strictly below :attr:`KpiObservationRule.floor`.
#: A mean cannot tell one long outage from a scatter of dips, and only the second
#: is tolerable for sustained delivery, so the run length is its own statistic
#: rather than a reading of the mean.  Missing samples need no second number
#: here: ``min_coverage`` already refuses a thin window as ``UNKNOWN``, which is
#: the rule the redesign asks for -- invalid collection stays unknown and is
#: never a continuity violation.
STATISTIC_LOW_RUN = "lowRun"

#: The v4 continuity KPI this statistic serves.  Named here rather than imported
#: from ``tc`` because ``tc`` imports this module.
KPI_LOW_DELIVERY_RUN = "lowDeliveryRunBins"

#: What a :data:`STATISTIC_RATIO` sample carries, in the order each is looked
#: for.  Both counters are **cumulative** -- an observer reports what it has
#: counted so far and the window differences them -- and ``eligible`` counts
#: every issued request whose deadline has already passed, answered or not.
_RATIO_ELIGIBLE_KEYS: Tuple[str, ...] = ("eligible", "eligibleIssued", "issued")
_RATIO_COMPLETED_KEYS: Tuple[str, ...] = ("completed", "completedWithinDeadline")


def low_delivery_runs(values: Sequence[Any], floor: Any) -> Tuple[int, int, list]:
    """Longest run of consecutive low bins, as the pair (proven, permitted).

    ``values`` is one entry per bin in time order: a number that was collected,
    or ``None`` for a bin that was not.  A bin is *low* when its value is
    strictly below ``floor``.

    Two numbers rather than one guess, because a bin that was never collected is
    neither a delivered second nor a lost one and cannot decide a run by itself:
    ``proven`` lets a missing bin break a run (the longest run the record
    establishes) and ``permitted`` reads every missing bin as low (the longest
    run the record allows).  On the live path ``min_coverage`` already refuses a
    thin window, so :meth:`KpiObservationRule.aggregate` needs only ``proven``;
    an offline scorer over a whole horizon uses both.

    This is the one implementation and it lives here, on the live path, because
    the package may not import ``experiments`` (tests/assurance/test_seams.py);
    ``experiments.agent_metrics`` imports it from here so the live verdict and
    the offline one cannot drift apart.
    """
    floor = float(floor)

    def longest(missing_is_low):
        best = run = 0
        start = None
        found = []
        for index, value in enumerate(values):
            number = _as_float(value)
            low = missing_is_low if number is None else number < floor
            if low:
                run += 1
                start = index if start is None else start
            else:
                if run:
                    found.append({"startBin": start, "bins": run})
                best, run, start = max(best, run), 0, None
        if run:
            found.append({"startBin": start, "bins": run})
        return max(best, run), found

    proven, runs = longest(False)
    permitted, _ = longest(True)
    return proven, permitted, runs


def continuity_verdict(proven: int, permitted: int, max_run_bins: int) -> str:
    """FAIL when the record proves a break, PASS when it rules one out, else UNKNOWN."""
    if proven > max_run_bins:
        return "FAIL"
    return "PASS" if permitted <= max_run_bins else "UNKNOWN"


def _ratio_counters(value: Any) -> Optional[Tuple[float, float]]:
    """``(eligible_issued, completed_within_deadline)`` of one sample."""
    if not isinstance(value, Mapping):
        return None
    row = dict(value)
    eligible = next((_as_float(row[key]) for key in _RATIO_ELIGIBLE_KEYS
                     if key in row), None)
    completed = next((_as_float(row[key]) for key in _RATIO_COMPLETED_KEYS
                      if key in row), None)
    if eligible is None or completed is None:
        return None
    return (eligible, completed)


@dataclass(frozen=True)
class KpiObservationRule:
    """How one KPI kind is measured and how long its answer stays usable."""

    kpi: str
    settle_ms: int = 0
    window_ms: int = 30000
    statistic: str = "mean"
    min_coverage: float = 0.8
    validity_ms: int = 60000
    #: Only :data:`STATISTIC_LOW_RUN` reads this: the value a sample must fall
    #: strictly below to count as a low bin, in the KPI's own unit.
    floor: Optional[float] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kpi", str(self.kpi or "").strip())
        object.__setattr__(self, "settle_ms", int(self.settle_ms or 0))
        object.__setattr__(self, "window_ms", int(self.window_ms or 0))
        object.__setattr__(self, "statistic", str(self.statistic or "mean").strip())
        object.__setattr__(self, "min_coverage", float(self.min_coverage or 0.0))
        object.__setattr__(self, "validity_ms", int(self.validity_ms or 0))
        object.__setattr__(self, "floor",
                           None if self.floor is None else float(self.floor))
        if self.statistic == STATISTIC_LOW_RUN and self.floor is None:
            raise ValueError(
                f"{self.kpi}: the {STATISTIC_LOW_RUN} statistic needs a floor; "
                "without one every sample is above it and no run is ever counted")

    @property
    def hold_ms(self) -> int:
        return self.settle_ms + self.window_ms

    def to_record(self) -> Dict[str, Any]:
        record = {"settleMs": self.settle_ms, "windowMs": self.window_ms,
                  "statistic": self.statistic, "minCoverage": self.min_coverage,
                  "validityMs": self.validity_ms}
        if self.floor is not None:
            record["floor"] = self.floor
        return record

    @classmethod
    def from_record(cls, kpi: str, record: Mapping[str, Any]) -> "KpiObservationRule":
        record = dict(record or {})
        default = DEFAULT_OBSERVATION_RULES.get(
            str(kpi), KpiObservationRule(kpi=str(kpi)))
        return cls(kpi=str(kpi),
                   settle_ms=int(record.get("settleMs", default.settle_ms) or 0),
                   window_ms=int(record.get("windowMs", default.window_ms) or 0),
                   statistic=str(record.get("statistic", default.statistic)),
                   min_coverage=float(record.get("minCoverage", default.min_coverage) or 0.0),
                   validity_ms=int(record.get("validityMs", default.validity_ms) or 0),
                   floor=(record.get("floor", default.floor)))

    def aggregate(self, samples: Sequence[Tuple[float, Any]], hold_end: float,
                  interval_ms: Optional[float] = None) -> Tuple[Any, float]:
        """The statistic over ``[hold_end - window, hold_end]``, and its coverage.

        ``UNKNOWN`` when the coverage is below ``min_coverage`` -- a thin window
        can never establish a success, which is exactly the fail-closed rule the
        metrics depend on.
        """
        start = float(hold_end) - self.window_ms
        inside = [(stamp, value) for stamp, value in samples
                  if value is not None and start - 1e-6 <= stamp <= hold_end + 1e-6]
        expected = self._expected(samples, interval_ms)
        coverage = 1.0 if expected <= 0 else min(1.0, len(inside) / expected)
        if not inside or coverage < self.min_coverage - 1e-9:
            return UNKNOWN, coverage
        if self.statistic == STATISTIC_RATIO:
            if any(isinstance(value, Mapping) and "byDeadlineMs" in value
                   for _stamp, value in inside):
                return self._deadline_ratios(samples, hold_end, interval_ms), coverage
            return self._ratio(samples, start, float(hold_end)), coverage
        values = [value for _stamp, value in inside]
        numbers = [_as_float(value) for value in values]
        if self.statistic == STATISTIC_LOW_RUN:
            # The samples in the window, in time order; a sample that carries no
            # number is a bin that was not collected and breaks the run rather
            # than extending it.  A window too thin to judge never reaches here:
            # the coverage gate above already returned UNKNOWN.
            proven, _permitted, _runs = low_delivery_runs(numbers, self.floor)
            return proven, coverage
        if self.statistic == "last":
            return values[-1], coverage
        if self.statistic == STATISTIC_CONSISTENT:
            # One value only if every present sample agrees (2026-09-25, owner): a missed poll
            # no longer voids the membership, a change inside the window (a handover) does.
            return (values[0] if all(value == values[0] for value in values) else UNKNOWN), coverage
        if self.statistic == "first":
            return values[0], coverage
        usable = [number for number in numbers if number is not None]
        if not usable:
            return values[-1], coverage
        if self.statistic == "min":
            return min(usable), coverage
        if self.statistic == "max":
            return max(usable), coverage
        if self.statistic == "median":
            usable.sort()
            middle = len(usable) // 2
            value = (usable[middle] if len(usable) % 2
                     else (usable[middle - 1] + usable[middle]) / 2.0)
            return round(value, 6), coverage
        return round(sum(usable) / len(usable), 6), coverage

    def _deadline_ratios(self, samples: Sequence[Tuple[float, Any]],
                         hold_end: float, interval_ms: Optional[float]) -> Any:
        """Each D is differenced independently over this same observation window."""
        start = float(hold_end) - self.window_ms
        rows = sorted(((stamp, value) for stamp, value in samples
                       if stamp <= hold_end + 1e-6 and isinstance(value, Mapping)),
                      key=lambda item: item[0])
        before = [(stamp, value) for stamp, value in rows if stamp <= start + 1e-6]
        rows = before[-1:] + [(stamp, value) for stamp, value in rows
                             if stamp > start + 1e-6]
        if not rows:
            return UNKNOWN
        source = rows[0][1].get("source")
        if any(value.get("source") != source for _stamp, value in rows):
            return UNKNOWN
        if source is not None:
            stamps = [_as_float(value.get("observedAtMs")) for _stamp, value in rows]
            if any(stamp is None for stamp in stamps) or any(
                    b <= a for a, b in zip(stamps, stamps[1:])):
                return UNKNOWN
        deadlines = set().union(*(dict(value.get("byDeadlineMs") or {})
                                  for _stamp, value in rows))
        ratios = {}
        for deadline in sorted(deadlines):
            series = [(stamp, dict(value.get("byDeadlineMs") or {}).get(deadline))
                      for stamp, value in rows]
            counters = [_ratio_counters(value) for _stamp, value in series]
            usable = [row for row in counters if row is not None]
            # A reset is not a second flow whose lifetime totals can be joined
            # to the first. Missing deadline levels remain independently unknown.
            if any(e < 0 or c < 0 or c > e for e, c in usable) or any(
                    b[0] < a[0] or b[1] < a[1] for a, b in zip(usable, usable[1:])):
                continue
            ratio, _coverage = self.aggregate(series, hold_end, interval_ms)
            if ratio != UNKNOWN:
                ratios[str(deadline)] = ratio
        return {"byDeadlineMs": ratios} if ratios else UNKNOWN

    def _ratio(self, samples: Sequence[Tuple[float, Any]], start: float,
               hold_end: float) -> Any:
        """``completed_within_D1 / eligible_issued`` over the window.

        The denominator is **every eligible issued request**, including the
        ones that never answered (``exp_metrics.md`` section 4, restated in
        ``exp_3UEscenario.md``: "deadline success uses all eligible issued
        requests in the denominator, including missing responses").  The
        observer's counters already carry that -- a request becomes eligible
        when its deadline has passed, whether or not a response arrived -- so
        the rule differences them rather than re-deriving them.

        A window with **no** issued request is ``UNKNOWN``, never ``1.0``: a
        ratio nobody offered a request to has not established anything.
        """
        rows = [(stamp, _ratio_counters(value)) for stamp, value in samples
                if stamp <= hold_end + 1e-6]
        rows = [(stamp, counters) for stamp, counters in rows if counters is not None]
        if not rows:
            return UNKNOWN            # nothing here carries the two counters
        rows.sort(key=lambda item: item[0])
        # The window is differenced against the counter as it stood **at** its
        # start, so the sample on the boundary is the baseline rather than the
        # first thing counted; with nothing before the window the earliest
        # sample inside it is the best baseline there is, and the interval it
        # opens is honestly left out.
        inside = [item for item in rows if item[0] > start + 1e-6]
        if not inside:
            return UNKNOWN
        before = [item for item in rows if item[0] <= start + 1e-6]
        base = before[-1][1] if before else inside[0][1]
        last = inside[-1][1]
        eligible, completed = last[0] - base[0], last[1] - base[1]
        if eligible < 0 or completed < 0:
            # the flow restarted inside the window; its own totals are all the
            # window can honestly say, and they are still counters, not a mean
            eligible, completed = last[0], last[1]
        if eligible <= 0:
            return UNKNOWN
        return round(min(1.0, max(0.0, completed / eligible)), 6)

    def _expected(self, samples: Sequence[Tuple[float, Any]],
                  interval_ms: Optional[float]) -> float:
        interval = _as_float(interval_ms)
        if interval is None or interval <= 0:
            stamps = sorted(stamp for stamp, _value in samples)
            deltas = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
            if not deltas:
                return 0.0
            deltas.sort()
            interval = deltas[len(deltas) // 2]
        if interval <= 0:
            return 0.0
        return max(1.0, self.window_ms / interval)


DEFAULT_OBSERVATION_RULES: Dict[str, KpiObservationRule] = {
    KPI_GOODPUT: KpiObservationRule(
        kpi=KPI_GOODPUT, settle_ms=5000, window_ms=30000, statistic="mean",
        min_coverage=0.8, validity_ms=60000),
    # 셀 총량은 UE goodput 표본을 합한 것이라 같은 창·같은 통계를 쓴다.  한 UE 라도
    # 그 창에서 안 읽히면 합계를 말할 수 없으므로 관측기가 키 자체를 안 낸다(= UNKNOWN).
    KPI_CELL_GOODPUT: KpiObservationRule(
        kpi=KPI_CELL_GOODPUT, settle_ms=5000, window_ms=30000, statistic="mean",
        min_coverage=0.8, validity_ms=60000),
    # 설정값 되읽기(에너지 대리 지표)라 평균할 것이 없다 -- 창의 마지막 값이 지금 설정이다.
    # coverage·유효기간은 goodput 과 같게 둔다: 관측 유효기간은 번들의 KPI 최솟값이
    # 지배하므로 여기만 짧으면 번들 전체가 짧아진다.
    # "consistent", not "last" (Codex review 2026-09-25 #2): a setting that changed inside
    # the window -- a rollback -- is UNKNOWN, and with the observer publishing only fresh
    # records a telemetry gap costs coverage instead of carrying the old value forward.
    KPI_CELL_TX_ATTENUATION: KpiObservationRule(
        kpi=KPI_CELL_TX_ATTENUATION, settle_ms=5000, window_ms=30000, statistic=STATISTIC_CONSISTENT,
        # 1.0 (Codex round 2): a trailing gap must not leave a covered-enough window standing
        # on an expired value.  The observer republishes a record for 10 s, so one late KPM
        # indication does not cost a poll; a longer outage makes the window UNKNOWN.
        # v5.2 (2026-09-27): 0.8 like membership.  A poll that ran 2 s instead of 1 s voided v5.2
        # windows at 13/15 coverage while every poll that ran carried the value; "consistent"
        # still makes a window whose setting changed UNKNOWN, and the observer publishes nothing
        # older than 10 s, so a real telemetry gap still costs coverage.
        min_coverage=0.8 if os.environ.get("AIC_V52") == "1" else 1.0, validity_ms=60000),
    # 2026-09-25 (owner, from v4.7 block 4): "last" with coverage 1.0 voided 20-30% of trials
    # over one missed poll, yet could not see a handover inside the window.  With
    # AIC_SERVINGCELL_CONSISTENT=1 membership needs 0.8 coverage and every present sample on
    # one cell.  Off by default, so earlier blocks and replays keep their rule.
    KPI_SERVING_CELL: KpiObservationRule(
        kpi=KPI_SERVING_CELL, settle_ms=0, window_ms=3000,
        statistic=STATISTIC_CONSISTENT if os.environ.get("AIC_SERVINGCELL_CONSISTENT") == "1" else "last",
        min_coverage=0.8 if os.environ.get("AIC_SERVINGCELL_CONSISTENT") == "1" else 1.0,
        validity_ms=10000),
    KPI_DEADLINE_RATIO: KpiObservationRule(
        kpi=KPI_DEADLINE_RATIO, settle_ms=5000, window_ms=30000,
        statistic=STATISTIC_RATIO, min_coverage=0.8, validity_ms=60000),
    # v4 delivery continuity.  It rides the goodput samples, so it takes the
    # goodput's settle, window and validity; only the statistic differs.  The
    # floor is deliberately NOT defaulted: it is a scenario number (0.5 L1 in
    # the redesign), and a wrong one silently counts no run at all, so
    # KpiObservationRule refuses a lowRun rule without one and the operator has
    # to state it.  This entry exists so --observe can supply the floor to a
    # rule whose other fields are already right.
    KPI_LOW_DELIVERY_RUN: KpiObservationRule(
        kpi=KPI_LOW_DELIVERY_RUN, settle_ms=5000, window_ms=30000,
        statistic=STATISTIC_LOW_RUN, min_coverage=0.8, validity_ms=60000,
        floor=float("inf")),
}


@dataclass(frozen=True)
class ObservationRules:
    """One rule per KPI kind; a sitting setting, never attached to an intent.

    The models are told the observation **time and validity** (``SINGLE_CALL.md``
    allows it) but never the trial count or the deadline.
    """

    rules: Dict[str, KpiObservationRule] = field(default_factory=dict)

    def __post_init__(self) -> None:
        rows = {}
        for kpi, rule in dict(self.rules or {}).items():
            rows[str(kpi)] = (rule if isinstance(rule, KpiObservationRule)
                              else KpiObservationRule.from_record(kpi, rule))
        object.__setattr__(self, "rules", rows)

    @classmethod
    def defaults(cls, kpis: Sequence[str] = ()) -> "ObservationRules":
        kpis = tuple(kpis) or tuple(DEFAULT_OBSERVATION_RULES)
        rows = {}
        for kpi in kpis:
            rows[str(kpi)] = DEFAULT_OBSERVATION_RULES.get(
                str(kpi), KpiObservationRule(kpi=str(kpi)))
        return cls(rows)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ObservationRules":
        return cls({str(kpi): KpiObservationRule.from_record(kpi, entry)
                    for kpi, entry in dict(record or {}).items()})

    def rule_for(self, kpi: str) -> KpiObservationRule:
        text = str(kpi or "")
        kind = text.split("@", 1)[0]
        if kind in self.rules:
            return self.rules[kind]
        if kind in DEFAULT_OBSERVATION_RULES:
            return DEFAULT_OBSERVATION_RULES[kind]
        return KpiObservationRule(kpi=kind)

    @property
    def kpis(self) -> Tuple[str, ...]:
        return tuple(self.rules)

    def hold_ms(self) -> int:
        """``max(settle + window)`` over the KPI kinds ``T`` uses -- what the
        joint composition holds the case open for."""
        return max((rule.hold_ms for rule in self.rules.values()), default=0)

    def min_validity_ms(self) -> int:
        return min((rule.validity_ms for rule in self.rules.values()
                    if rule.validity_ms > 0), default=0)

    def valid_until(self, kpi: str, window_end: Any) -> Any:
        """``windowEnd + validityMs``, in whatever shape ``window_end`` came in."""
        rule = self.rule_for(kpi)
        if isinstance(window_end, (int, float)) and not isinstance(window_end, bool):
            return float(window_end) + rule.validity_ms
        stamp = _parse_time(window_end)
        if stamp is None:
            return window_end
        moment = datetime.fromtimestamp(stamp / 1000.0, tz=timezone.utc)
        moment += timedelta(milliseconds=rule.validity_ms)
        return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def earliest_valid_until(self, observations: Sequence[Mapping[str, Any]]
                             ) -> Optional[float]:
        """The moment the first of these observations stops being usable."""
        stamps = []
        for item in observations or ():
            stamp = _parse_time(dict(item or {}).get("validUntil"))
            if stamp is not None:
                stamps.append(stamp)
        return min(stamps) if stamps else None

    def is_stale(self, observations: Sequence[Mapping[str, Any]],
                 answered_at: Any) -> bool:
        """Did the answer arrive after the observations it was given expired?"""
        earliest = self.earliest_valid_until(observations)
        answered = _parse_time(answered_at)
        if earliest is None or answered is None:
            return False
        return answered > earliest

    def aggregate(self, samples: Sequence[Mapping[str, Any]], hold_end: Any,
                  interval_ms: Optional[float] = None,
                  ) -> Dict[str, Any]:
        """Every KPI of a sample series, judged at the same hold end.

        ``samples`` are ``{"t": <ms or ISO>, "kpis": {key: value}}`` rows -- the
        service trace the executor already keeps.
        """
        end = _parse_time(hold_end)
        series: Dict[str, List[Tuple[float, Any]]] = {}
        for row in samples or ():
            row = dict(row or {})
            stamp = _parse_time(row.get("t", row.get("at")))
            if stamp is None:
                continue
            for key, value in dict(row.get("kpis") or {}).items():
                series.setdefault(str(key), []).append((stamp, value))
        if end is None:
            end = max((stamp for rows in series.values() for stamp, _ in rows),
                      default=0.0)
        result: Dict[str, Any] = {}
        for key, rows in series.items():
            rows.sort(key=lambda item: item[0])
            value, _coverage = self.rule_for(key).aggregate(rows, end, interval_ms)
            result[key] = value
        return result

    def coverage(self, samples: Sequence[Mapping[str, Any]], hold_end: Any,
                 interval_ms: Optional[float] = None) -> Dict[str, float]:
        end = _parse_time(hold_end)
        series: Dict[str, List[Tuple[float, Any]]] = {}
        for row in samples or ():
            row = dict(row or {})
            stamp = _parse_time(row.get("t", row.get("at")))
            if stamp is None:
                continue
            for key, value in dict(row.get("kpis") or {}).items():
                series.setdefault(str(key), []).append((stamp, value))
        if end is None:
            end = max((stamp for rows in series.values() for stamp, _ in rows),
                      default=0.0)
        result: Dict[str, float] = {}
        for key, rows in series.items():
            rows.sort(key=lambda item: item[0])
            _value, coverage = self.rule_for(key).aggregate(rows, end, interval_ms)
            result[key] = coverage
        return result

    def to_record(self) -> Dict[str, Any]:
        return {kpi: rule.to_record() for kpi, rule in self.rules.items()}


# --------------------------------------------------------------------------- #
# section 7 -- generation options
# --------------------------------------------------------------------------- #


#: Per role: the formation calls think longer than the selection calls.  These
#: are what the model is *told*, not a wall the executor puts up.
GENERATION_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "default": {"maxTokens": 1500, "thinkingBudgetTokens": 4000,
                "reasoningEffort": "medium", "jsonMode": True},
    # 4000, not 3000.  ``maxTokens`` caps the *visible* completion, and when the
    # formation JSON needs more the answer is truncated, fails role validation,
    # and a repair retry runs the whole call again.  Measured over 30 live
    # episodes of 2026-09-16/17:
    #
    #             maxTokens  출력 중앙  p90    최대    재호출  그 판의 셈시행
    #   target      3000      2171     2694   4901    1/30    재호출 시 1.0 대 3.8
    #   control     3000      4034     4966  10065    2/30    재호출 시 0.0 대 4.0
    #
    # **Every formation repair killed or crippled its episode.**  Formation with
    # no repair is 42.5 + 78.3 = 121 s; one control repair makes it 226 s, and
    # the 240 s formation deadline ("the first executable proposal was not
    # formed within 240000 ms") then ends the case with **zero** counted trials.
    # Two of thirty episodes died exactly that way, a third was crippled.
    #
    # Raising the cap costs nothing on the calls that already fit -- a model
    # does not fill a budget it does not need, and 28 of 30 control answers were
    # already under it -- while the calls that did not fit currently pay for a
    # second full call at ~19 ms per output token.  ``monolith-form`` has always
    # been 4000; this only stops the split roles from being tighter than the
    # single call that does both their jobs at once.
    "target": {"maxTokens": 4000, "thinkingBudgetTokens": 8000,
               "reasoningEffort": "high", "jsonMode": True},
    "control": {"maxTokens": 4000, "thinkingBudgetTokens": 8000,
                "reasoningEffort": "high", "jsonMode": True},
    "monolith-form": {"maxTokens": 4000, "thinkingBudgetTokens": 12000,
                      "reasoningEffort": "high", "jsonMode": True},
    # **선택 호출은 전부 2000** (2026-09-23 결정).  이 셋이 세 방식의 선택 단계다.
    # 이전 값은 1000 / 1000 / 1500 이었는데, 3A 의 `trajectory` 와 IM 의
    # `monolith-select` 는 **같은 시스템 프롬프트**를 쓰면서 러너가 2000 대 1000 을
    # 실어 보냈다(`atomic_formal_run_guarded.py` 의 `--generation`) -- 짝지은
    # 선택기 비교가 토큰 예산 2배 차이로 오염돼 있었다.  2000 은 기존 3A 허용량을
    # 깎지 않는 쪽으로 고른 공통값이며 최적이라는 주장이 아니다.  **한도를 다시
    # 벌리려면 세 줄을 함께 고쳐라** -- 하나만 고치면 그 방식만 달라진다.
    "trajectory": {"maxTokens": 2000, "thinkingBudgetTokens": 2000,
                   "reasoningEffort": "medium", "jsonMode": True},
    "monolith-select": {"maxTokens": 2000, "thinkingBudgetTokens": 2000,
                        "reasoningEffort": "medium", "jsonMode": True},
    "basic-monolith": {"maxTokens": 2000, "thinkingBudgetTokens": 4000,
                       "reasoningEffort": "medium", "jsonMode": True},
}

#: How much room the executor leaves between a call's p95 and the moment its
#: observations expire.
CALIBRATION_MARGIN_MS = 5000


@dataclass(frozen=True)
class GenerationOptions:
    """What a model is asked to spend on one call (contract v2 section 7).

    Nothing here cuts a generation off: the backend's own network timeout is
    the only hang guard, and a call that runs long is charged as time, not
    truncated.
    """

    max_tokens: int = 1500
    thinking_budget_tokens: int = 4000
    reasoning_effort: str = "medium"
    json_mode: bool = True
    source: str = "default"

    @classmethod
    def for_role(cls, role: str) -> "GenerationOptions":
        record = GENERATION_DEFAULTS.get(str(role), GENERATION_DEFAULTS["default"])
        return cls(max_tokens=int(record["maxTokens"]),
                   thinking_budget_tokens=int(record["thinkingBudgetTokens"]),
                   reasoning_effort=str(record["reasoningEffort"]),
                   json_mode=bool(record["jsonMode"]),
                   source="default")

    def to_record(self) -> Dict[str, Any]:
        return {"maxTokens": self.max_tokens,
                "thinkingBudgetTokens": self.thinking_budget_tokens,
                "reasoningEffort": self.reasoning_effort,
                "jsonMode": self.json_mode, "source": self.source}

    def as_options(self) -> Dict[str, Any]:
        """The mapping handed to ``backend.generate(..., options=...)``."""
        return {"maxTokens": self.max_tokens,
                "thinkingBudgetTokens": self.thinking_budget_tokens,
                "reasoningEffort": self.reasoning_effort,
                "jsonMode": self.json_mode}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "GenerationOptions":
        record = dict(record or {})
        base = GENERATION_DEFAULTS["default"]
        return cls(max_tokens=int(record.get("maxTokens", base["maxTokens"])),
                   thinking_budget_tokens=int(record.get(
                       "thinkingBudgetTokens", base["thinkingBudgetTokens"])),
                   reasoning_effort=str(record.get("reasoningEffort",
                                                   base["reasoningEffort"])),
                   json_mode=bool(record.get("jsonMode", base["jsonMode"])),
                   source=str(record.get("source", "record")))


#: The prompt roles a calibration is written per; also how a model-keyed
#: document is told apart from a role-keyed one.
CALIBRATION_ROLES: Tuple[str, ...] = (
    "target", "control", "trajectory", "monolith-form", "monolith-select",
    "basic-monolith",
)


def _calibration_rows(table: Any, role: str, model: Optional[str] = None
                      ) -> List[Dict[str, Any]]:
    """Every ``(budget, p95)`` row a calibration document has for this role.

    Deliberately forgiving about the document's exact nesting: the calibration
    is written by a different tool and a missing or oddly shaped file must fall
    back to the defaults, never raise.  Two shapes are in use and both are
    read -- ``{"models": {model: {role: [...]}}}`` and
    ``tools/campaign5/calibrate_agent_latency.py``'s own
    ``{model: {role: {budget: {"p50": .., "p95": .., "n": .., "accepted": ..}}}}``,
    which names no wrapper and is recognised by having no role key at the top.
    """
    if not isinstance(table, Mapping):
        return []
    document = dict(table)
    models = document.get("models")
    if not isinstance(models, Mapping) and not any(
            name in document for name in CALIBRATION_ROLES):
        models = {name: entry for name, entry in document.items()
                  if isinstance(entry, Mapping)}
    if isinstance(models, Mapping) and models:
        if model is not None and str(model) in models:
            document = dict(models[str(model)] or {})
        elif len(models) == 1:
            document = dict(next(iter(models.values())) or {})
        else:
            merged: List[Dict[str, Any]] = []
            for entry in models.values():
                merged.extend(_calibration_rows(entry, role, None))
            return merged
    rows = document.get(str(role))
    if rows is None and isinstance(document.get("roles"), Mapping):
        rows = dict(document["roles"]).get(str(role))
    if isinstance(rows, Mapping):
        rows = [dict(entry or {}, thinkingBudgetTokens=key)
                for key, entry in rows.items()]
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    found: List[Dict[str, Any]] = []
    for entry in rows:
        if not isinstance(entry, Mapping):
            continue
        entry = dict(entry)
        budget = entry.get("thinkingBudgetTokens", entry.get("budget"))
        if isinstance(budget, Mapping):
            options = dict(budget)
            budget = options.get("thinkingBudgetTokens")
        else:
            options = {k: v for k, v in entry.items()
                       if k in ("maxTokens", "thinkingBudgetTokens",
                                "reasoningEffort", "jsonMode")}
        p95 = entry.get("p95LatencyMs", entry.get("p95"))
        if _as_float(budget) is None or _as_float(p95) is None:
            continue
        options["thinkingBudgetTokens"] = int(_as_float(budget))
        found.append({"options": options, "p95": float(_as_float(p95)),
                      "p50": _as_float(entry.get("p50LatencyMs", entry.get("p50")))})
    return found


def load_latency_calibration(path: Any) -> Dict[str, Any]:
    """``<runs_root>/agent-latency-calibration.json``, or ``{}`` when absent."""
    try:
        with open(Path(path), encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return document if isinstance(document, dict) else {}


def choose_from_calibration(table: Any, role: str, min_validity_ms: Optional[int],
                            model: Optional[str] = None,
                            margin_ms: int = CALIBRATION_MARGIN_MS,
                            ) -> GenerationOptions:
    """The largest calibrated budget whose p95 still fits inside the validity.

    With no calibration, no fitting budget or no validity window to fit inside,
    the role's default stands -- the point is never to truncate a model, only to
    ask for less thinking when the observations would go stale first.
    """
    default = GenerationOptions.for_role(role)
    rows = _calibration_rows(table, role, model)
    if not rows:
        return default
    budget = None if not min_validity_ms else max(0, int(min_validity_ms) - int(margin_ms))
    if not budget:
        return default
    fitting = [row for row in rows if row["p95"] <= budget]
    if not fitting:
        return default
    best = max(fitting, key=lambda row: row["options"].get("thinkingBudgetTokens", 0))
    options = dict(default.as_options())
    options.update(best["options"])
    # `maxTokens` 는 **지연 노브가 아니라 절단선**이다.  보정은 관측이 상하기 전에 답이
    # 오도록 **생각 예산**을 깎는 장치이므로, 출력 상한까지 함께 깎을 이유가 없다.
    # 보정은 상한을 **올릴 수는 있어도 내릴 수는 없다.**
    #
    # 2026-09-21 정정: 이 가드를 넣을 때 "최근 40판의 3000 이 여기서 나왔다" 고 적었는데
    # **틀렸다.**  운영자가 `--generation` 으로 값을 주면 `_SittingAgents.options_for()`
    # 가 보정 경로에 **들어가기 전에** 반환한다(`tools/liveconsole/agent.py:1671`).
    # 그 3000 은 러너의 지시였고 보정표는 관여하지 않았다.  이 가드는 지금 발화하지 않는
    # **잠복 가드**다 -- 운영자 지시가 없고 보정표만 있는 판에서만 의미가 있다.
    return GenerationOptions(
        max_tokens=max(default.max_tokens,
                       int(options.get("maxTokens", default.max_tokens))),
        thinking_budget_tokens=int(options.get("thinkingBudgetTokens",
                                               default.thinking_budget_tokens)),
        reasoning_effort=str(options.get("reasoningEffort", default.reasoning_effort)),
        json_mode=bool(options.get("jsonMode", default.json_mode)),
        source="calibration")


GenerationOptions.choose_from_calibration = staticmethod(  # type: ignore[attr-defined]
    choose_from_calibration)
__all__.append("choose_from_calibration")


# --------------------------------------------------------------------------- #
# section 2.2 -- the intake checklist
# --------------------------------------------------------------------------- #


def _question(intent_id: str, field_name: str, question: str) -> Dict[str, str]:
    return {"intentId": intent_id, "field": field_name, "question": question}


@dataclass(frozen=True)
class IntakeChecklist:
    """Is the operator's own input complete enough to start?

    Deterministic and **before** any model call: asking a model to guess at a
    bound the operator never signed is exactly the failure this checklist
    exists to prevent.  It answers in the Target agent's own
    ``missingInformation`` shape so one question form serves both.
    """

    require_unit: bool = True
    require_owner: bool = True
    sitting_settings: Tuple[Tuple[str, str], ...] = REQUIRED_SITTING_SETTINGS

    def check(self, intents: Sequence[Intent],
              authorization: Optional[Authorization] = None,
              settings: Optional[Mapping[str, Any]] = None,
              ) -> List[Dict[str, str]]:
        missing: List[Dict[str, str]] = []
        for intent in intents or ():
            missing.extend(self._check_intent(intent))
        missing.extend(self._check_settings(intents, settings))
        return missing

    def _check_intent(self, intent: Intent) -> List[Dict[str, str]]:
        found: List[Dict[str, str]] = []
        requirement = intent.requirement
        if self.require_owner and (not intent.owner or intent.owner == intent.intent_id):
            found.append(_question(intent.intent_id, "owner",
                                   f"Who owns {intent.intent_id}?"))
        # owner 가 UE 가 아니라 셀 사업자인 요구는 `ueId` 가 없고 scope 가 `cell@<nci>` 다
        # (KPI_CELL_GOODPUT).  그 요구에 "어느 UE 인가" 를 물으면 답이 없고, 답 없는 질문
        # 하나가 **판 전체를 제출 전에 거절시킨다** -- 2026-09-23 시도 361 이 그렇게 죽었다.
        # 판정의 기준은 "UE 를 아는가" 가 아니라 **관측 키를 만들 수 있는가** 다.
        if not intent.ue_id and not str(requirement.scope or "").startswith("cell@"):
            found.append(_question(intent.intent_id, "ueId",
                                   f"Which UE does {intent.intent_id} apply to? "
                                   "(or give requirement.scope as 'cell@<nci>' if it "
                                   "is the operator's cell-level requirement)"))
        if not requirement.kpi:
            found.append(_question(intent.intent_id, "kpi",
                                   f"Which KPI does {intent.intent_id} constrain?"))
        if not requirement.op:
            found.append(_question(intent.intent_id, "op",
                                   f"Is {intent.intent_id} a floor (>=) or a "
                                   "ceiling (<=)?"))
        if requirement.value is None or requirement.value == "":
            found.append(_question(intent.intent_id, "value",
                                   f"What threshold does {intent.intent_id} require?"))
        if self.require_unit and not requirement.unit:
            found.append(_question(intent.intent_id, "unit",
                                   f"What unit is {intent.intent_id}'s threshold in?"))
        if requirement.numeric and not requirement.relaxation_stated:
            if requirement.steps is None:
                found.append(_question(
                    intent.intent_id, "steps",
                    f"How far may {intent.intent_id} be relaxed? Give a number of "
                    "steps and the bound, or say it is not relaxable (steps 0)."))
            else:
                found.append(_question(
                    intent.intent_id, "bound",
                    f"{intent.intent_id} authorizes {requirement.steps} relaxation "
                    "steps but names no bound; what is the bound?"))
        found.extend(self._check_deadline(intent))
        return found

    def _check_deadline(self, intent: Intent) -> List[Dict[str, str]]:
        """``D1`` is a field like any other, and it is asked for like one.

        A deadline-success ratio is meaningless without the deadline it is a
        ratio *within*, so a missing ``deadlineMs`` is missing information, not
        something a model may pick.  Silence about **extending** the deadline
        is different: the scenario authorizes a bounded extension separately,
        and an operator who signed none has said all there is to say.  Steps
        signed with no bound is still a gap.
        """
        requirement = intent.requirement
        if requirement.kpi != KPI_DEADLINE_RATIO:
            return []
        if not requirement.has_deadline:
            return [_question(
                intent.intent_id, "deadlineMs",
                f"Within how many milliseconds must a tagged echo request "
                f"complete for {intent.intent_id}?")]
        if not requirement.deadline_stated:
            return [_question(
                intent.intent_id, "deadlineBound",
                f"{intent.intent_id} authorizes {requirement.deadline_steps} "
                "deadline-extension steps but names no bound; how far may the "
                "deadline be extended, in milliseconds?")]
        return []

    def _check_settings(self, intents: Sequence[Intent],
                        settings: Optional[Mapping[str, Any]]) -> List[Dict[str, str]]:
        settings = dict(settings or {})
        found: List[Dict[str, str]] = []
        for name, question in self.sitting_settings:
            if name not in settings:
                found.append(_question(SITTING, name, question))
        rules = settings.get("observation")
        rules = (rules.rules if isinstance(rules, ObservationRules)
                 else dict(rules or {}))
        kinds: List[str] = []
        for intent in intents or ():
            kind = intent.requirement.kpi
            if kind and kind not in kinds:
                kinds.append(kind)
        for kind in kinds:
            if kind not in rules:
                found.append(_question(
                    SITTING, f"observation.{kind}",
                    f"How is {kind} observed? Give settle, window, statistic, "
                    "minimum coverage and validity."))
        return found


def merge_answers(intents: Sequence[Intent], authorization: Optional[Authorization],
                  answers: Optional[Mapping[str, Any]],
                  settings: Optional[Mapping[str, Any]] = None,
                  ) -> Tuple[Tuple[Intent, ...], Authorization, Dict[str, Any]]:
    """Fold the operator's answers back into the intents and the settings.

    ``answers`` is ``{"I2": {"steps": 2, "bound": 1.0}, "sitting": {"trialsK": 8}}``
    -- and, for a deadline-success ratio, ``{"I4": {"deadlineMs": 50}}``
    -- the shape the Cockpit's question form posts and ``--answers`` carries.

    The sitting also carries what shapes the *domain*: ``jointConditions`` and
    ``modeConstraints``.  Both are rebuilt onto the authorization here, because
    this is where the authorization is rebuilt -- anything this does not carry
    is gone by the time the domain is expanded, however carefully it was signed.
    Unknown intent ids and unknown fields are ignored rather than refused: an
    operator answering more than was asked is not an error.
    """
    answers = dict(answers or {})
    settings = dict(settings or {})
    rows: List[Intent] = []
    for intent in intents or ():
        answer = dict(answers.get(intent.intent_id) or {})
        if not answer:
            rows.append(intent)
            continue
        requirement = intent.requirement
        changes: Dict[str, Any] = {}
        for name in ("kpi", "scope", "op", "value", "unit", "steps", "bound",
                     "deadlineMs", "deadlineSteps", "deadlineBound"):
            if name in answer:
                changes[name] = answer[name]
        if changes:
            record = requirement.to_record()
            record.pop("relaxLimit", None)
            record.pop("relaxable", None)
            record.pop("deadlineLevels", None)
            record.update({_camel(name): value for name, value in changes.items()})
            if "steps" in changes or "bound" in changes:
                record.setdefault("steps", requirement.steps)
                record.setdefault("bound", requirement.bound)
            if any(name.startswith("deadline") for name in changes):
                record.setdefault("deadlineMs", requirement.deadline_ms)
                record.setdefault("deadlineSteps", requirement.deadline_steps)
                record.setdefault("deadlineBound", requirement.deadline_bound)
            requirement = Requirement.from_record(record)
        intent_changes: Dict[str, Any] = {}
        if "owner" in answer:
            intent_changes["owner"] = str(answer["owner"])
        if "ueId" in answer:
            intent_changes["ue_id"] = str(answer["ueId"])
        if "priority" in answer:
            intent_changes["priority"] = int(answer["priority"])
        if "weight" in answer:
            intent_changes["weight"] = float(answer["weight"])
        rows.append(replace(intent, requirement=requirement, **intent_changes))

    sitting = dict(answers.get(SITTING) or {})
    merged_settings = dict(settings)
    merged_settings.update(sitting)
    if "observation" in sitting and "observation" in settings:
        rules = dict(settings.get("observation") or {})
        rules.update(dict(sitting.get("observation") or {}))
        merged_settings["observation"] = rules

    conditions = (authorization.joint_conditions if authorization is not None else ())
    constraints = (authorization.mode_constraints if authorization is not None else ())
    preference = authorization.preference if authorization is not None else None
    merged_conditions = sitting.get("jointConditions", conditions)
    merged_constraints = _mode_constraints(sitting.get("modeConstraints", constraints))
    return (tuple(rows),
            Authorization.from_intents(rows, joint_conditions=merged_conditions,
                                       mode_constraints=merged_constraints,
                                       preference=preference),
            merged_settings)


def _mode_constraints(supplied: Any) -> Tuple[Any, ...]:
    """Every entry read back, or a refusal naming the one that would not read.

    :class:`Authorization` refuses an unreadable carving too: its
    ``__post_init__`` raises rather than dropping, because a mode set whose key
    is misspelt would shape nothing, expand to the *unshaped* domain, and look
    like a sitting nobody constrained rather than like the typo it is.  (A free
    sentence is the one thing it still drops, and only because prose was never
    going to bind.)

    That leaves this guard redundant but not dead, and it is kept deliberately:
    it runs first -- :func:`apply_answers` calls it while building the arguments,
    before the :class:`Authorization` is constructed -- so an operator sees
    *this* message, which names the shape an answer has to have, instead of the
    container's more general one.  The behaviour is the same either way: the
    same ``ValueError`` the deeper checks (an unknown axis, a mode of the wrong
    width) already raise and the console already prints as "refused before
    anything was submitted".
    """
    rows: List[Any] = []
    for item in supplied or ():
        constraint = mode_constraint_from_record(item)
        if constraint is None:
            raise ValueError(
                f"this is not a mode constraint: {item!r}; an owner mode set "
                "names 'axes' and 'allow', a level quota 'axes' and 'atMost'")
        rows.append(constraint)
    return tuple(rows)


def _camel(name: str) -> str:
    return {"req_id": "reqId"}.get(name, name)
