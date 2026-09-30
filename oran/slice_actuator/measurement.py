"""Effect evidence contract for slice-labelled TS 28.552 counters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .model import SliceIdentity


# TS 28.552 measurement names.  Slice correlation is expressed by the
# mandatory S-NSSAI measurement label, not by inventing a suffixed metric name.
TS_28552_SLICE_COUNTERS = {
    "RRU.PrbTotDl": "percent",
    "DRB.UEThpDl": "kbit/s",
}


@dataclass(frozen=True)
class CounterSample:
    name: str
    value: int | float
    unit: str
    scope: Mapping[str, Any]
    observed_at: str
    source: str
    trace_id: str


@dataclass(frozen=True)
class CoreSliceEvidence:
    """Core session evidence independently bound to the same S-NSSAI/trial."""

    scope: Mapping[str, Any]
    observed_at: str
    source: str
    trace_id: str
    session_ref: str


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("evidence observed_at must be RFC3339 date-time") from exc
    if parsed.tzinfo is None:
        raise ValueError("evidence observed_at must carry a timezone")
    return parsed


class MockMeasurementCollector:
    """Hermetic adapter with the same fail-closed checks required live."""

    def __init__(self) -> None:
        self._samples: list[tuple[CounterSample, SliceIdentity]] = []
        self._core: list[tuple[CoreSliceEvidence, SliceIdentity]] = []

    def publish(self, sample: CounterSample) -> None:
        expected_unit = TS_28552_SLICE_COUNTERS.get(sample.name)
        if expected_unit is None:
            raise ValueError(f"{sample.name} is not an admitted TS 28.552 slice counter")
        if sample.unit != expected_unit:
            raise ValueError(f"{sample.name} must use unit {expected_unit}")
        try:
            identity = SliceIdentity.from_scope(sample.scope)
        except ValueError as exc:
            raise ValueError("TS 28.552 effect sample requires an S-NSSAI measurement label") from exc
        if isinstance(sample.value, bool) or not isinstance(sample.value, (int, float)):
            raise ValueError("measurement value must be numeric")
        if sample.value < 0 or (sample.name == "RRU.PrbTotDl" and sample.value > 100):
            raise ValueError(f"{sample.name} value is outside its defined range")
        if not sample.observed_at or not sample.source or not sample.trace_id:
            raise ValueError("measurement provenance and trace correlation are required")
        _instant(sample.observed_at)
        self._samples.append((sample, identity))

    def publish_core(self, evidence: CoreSliceEvidence) -> None:
        try:
            identity = SliceIdentity.from_scope(evidence.scope)
        except ValueError as exc:
            raise ValueError("Core evidence requires an S-NSSAI session label") from exc
        if not evidence.source or not evidence.trace_id or not evidence.session_ref:
            raise ValueError("Core evidence provenance, trace, and session reference are required")
        _instant(evidence.observed_at)
        self._core.append((evidence, identity))

    @staticmethod
    def _in_effect_window(observed_at: str, not_before: datetime,
                          not_after: datetime) -> bool:
        observed = _instant(observed_at)
        return not_before <= observed <= not_after

    def ran_correlated(self, identity: SliceIdentity, trace_id: str, *,
                       not_before: datetime, not_after: datetime) -> bool:
        return any(
            candidate == identity and sample.trace_id == trace_id
            and self._in_effect_window(sample.observed_at, not_before, not_after)
            for sample, candidate in self._samples
        )

    def core_correlated(self, identity: SliceIdentity, trace_id: str, *,
                        not_before: datetime, not_after: datetime) -> bool:
        return any(
            candidate == identity and evidence.trace_id == trace_id
            and self._in_effect_window(evidence.observed_at, not_before, not_after)
            for evidence, candidate in self._core
        )

    @property
    def samples(self) -> tuple[CounterSample, ...]:
        return tuple(sample for sample, _ in self._samples)

    @property
    def core_evidence(self) -> tuple[CoreSliceEvidence, ...]:
        return tuple(evidence for evidence, _ in self._core)
