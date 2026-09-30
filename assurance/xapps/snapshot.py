"""The common KPI snapshot the coordination layer plans against.

KPI measurement originates at the gNB and travels one way:

    gNB PHY/MAC/RLC/PDCP/RRC -> gNB E2 Agent -> E2 Indication -> Near-RT RIC
    -> Measurement Collector -> RawSample -> CommonKpiSnapshot

A snapshot is assembled from Measurement Collector
:class:`~assurance.collector.samples.RawSample` objects and from nothing else.
An xApp cannot fabricate a KPI into this path: ``RawSample``'s constructor
already refuses any value that is not ``Provenance.MEASURED`` from
``ComponentId.MEASUREMENT_COLLECTOR``, and :func:`snapshot_from_samples`
accepts ``RawSample`` only.

The whole snapshot is an input to the Action Composition Coordinator, which
recommends action compositions from it.  A specialist xApp does not receive
the whole snapshot in order to decide actions of its own; it receives the
subset needed to check execution preconditions and to perform post-execution
readback (:meth:`CommonKpiSnapshot.subset_for_counters`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from assurance.collector.samples import ClockHealth, RawSample
from assurance.core.addressing import content_hash
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.core.timebase import format_utc, is_utc_timestamp, parse_utc

__all__ = [
    "SERVING_CELL_ATTRIBUTION_COUNTER",
    "CommonKpiSnapshot",
    "KpiSnapshotEntry",
    "SnapshotError",
    "advance_timestamp",
    "snapshot_from_samples",
]

#: The per-UE serving-cell attribution counter this deployment delivers over
#: E2SM-KPM Style 4 (basis: ``assurance/objectives/registry.py`` ``_KPM_STYLE4``
#: ``parameters=("UE.ServingCell",)``).  It is the identity source for serving
#: cell and RNTI re-verification after a handover.
SERVING_CELL_ATTRIBUTION_COUNTER = "UE.ServingCell"


class SnapshotError(ValueError):
    """A KPI snapshot cannot be assembled or used as requested."""


def advance_timestamp(timestamp: str, delta_ms: int) -> str:
    """*timestamp* moved *delta_ms* forward, in the canonical UTC form."""
    return format_utc(parse_utc(timestamp) + timedelta(milliseconds=delta_ms))


@dataclass(frozen=True)
class KpiSnapshotEntry:
    """One measured observation carried into a snapshot.

    Every field is copied from the :class:`RawSample` that produced it, so an
    entry is walkable back to raw collector evidence through ``sample_id`` and
    ``sample_hash``.  The value must remain ``MEASURED``: a derived or draft
    quantity in a snapshot would be a summary wearing a measurement's clothes.
    """

    counter_id: str
    value: TypedQuantity
    scope: Mapping[str, str]
    observed_at: str
    clock_health: ClockHealth
    sample_id: str
    sample_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.counter_id, str) or not self.counter_id.strip():
            raise SnapshotError("entry counter_id must be a non-empty string")
        if not isinstance(self.value, TypedQuantity):
            raise SnapshotError("entry value must be a TypedQuantity")
        if self.value.provenance is not Provenance.MEASURED:
            raise SnapshotError(
                "a snapshot entry carries a MEASURED value; "
                f"{self.value.provenance.value} is not a measurement"
            )
        if not isinstance(self.clock_health, ClockHealth):
            raise SnapshotError("entry clock_health must be a ClockHealth member")
        if not is_utc_timestamp(self.observed_at):
            raise SnapshotError(f"entry observed_at is not canonical UTC: {self.observed_at!r}")
        object.__setattr__(
            self, "scope", {str(k): str(v) for k, v in dict(self.scope).items()}
        )

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "counterId": self.counter_id,
            "value": self.value.to_canonical_dict(),
            "scope": dict(self.scope),
            "observedAt": self.observed_at,
            "clockHealth": self.clock_health.value,
            "sampleId": self.sample_id,
            "sampleHash": self.sample_hash,
        }


@dataclass(frozen=True)
class CommonKpiSnapshot:
    """A timestamped, versioned, content-addressed view of measured KPIs.

    ``schema_version`` plus :meth:`content_hash` are the snapshot's version;
    ``taken_at`` is the assembly instant every freshness decision is judged
    against.  The snapshot grants nothing: it is planning input for the Action
    Composition Coordinator and precondition/readback input for assigned
    xApps.
    """

    snapshot_id: str
    taken_at: str
    entries: Tuple[KpiSnapshotEntry, ...]
    schema_version: str = ASSURANCE_SCHEMA_VERSION
    parent_snapshot_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id.strip():
            raise SnapshotError("snapshot_id must be a non-empty string")
        if not is_utc_timestamp(self.taken_at):
            raise SnapshotError(f"taken_at is not canonical UTC: {self.taken_at!r}")
        object.__setattr__(self, "entries", tuple(self.entries))
        for entry in self.entries:
            if not isinstance(entry, KpiSnapshotEntry):
                raise SnapshotError("entries must be KpiSnapshotEntry instances")

    # -- freshness ---------------------------------------------------------

    def is_fresh(self, now: str, *, freshness_bound_ms: int) -> bool:
        """True when the snapshot is newer than the bound and not future-dated."""
        age_ms = (parse_utc(now) - parse_utc(self.taken_at)).total_seconds() * 1000
        return 0 <= age_ms <= freshness_bound_ms

    # -- lookup ------------------------------------------------------------

    def entries_for(self, counter_id: str, **scope: str) -> Tuple[KpiSnapshotEntry, ...]:
        """Entries for one counter whose scope contains every given pair."""
        wanted = {str(k): str(v) for k, v in scope.items()}
        return tuple(
            entry for entry in self.entries
            if entry.counter_id == counter_id
            and all(entry.scope.get(key) == value for key, value in wanted.items())
        )

    def latest(self, counter_id: str, **scope: str) -> Optional[KpiSnapshotEntry]:
        """The newest matching entry, or ``None`` when the KPI is absent.

        Absence is an answer, not an error: a caller that needs the KPI must
        fail closed itself rather than have a value invented here.
        """
        matches = self.entries_for(counter_id, **scope)
        if not matches:
            return None
        return max(matches, key=lambda entry: (entry.observed_at, entry.sample_id))

    def serving_cell_of(self, ue_id: str) -> Optional[KpiSnapshotEntry]:
        """The newest serving-cell attribution entry for one logical UE."""
        return self.latest(SERVING_CELL_ATTRIBUTION_COUNTER, ueId=ue_id)

    def active_ue_ids(self, cell_id: str) -> Tuple[str, ...]:
        """Logical UE ids attributed to *cell_id* by this snapshot, sorted.

        Derived only from delivered attribution entries; an empty result means
        the snapshot carries no attribution for the cell, not that the cell is
        empty.
        """
        found = sorted({
            entry.scope["ueId"]
            for entry in self.entries
            if entry.counter_id == SERVING_CELL_ATTRIBUTION_COUNTER
            and entry.scope.get("cellId") == str(cell_id)
            and "ueId" in entry.scope
        })
        return tuple(found)

    def subset_for_counters(
        self, counter_ids: Sequence[str], *, subset_id: str
    ) -> "CommonKpiSnapshot":
        """The precondition/readback subset handed to one assigned xApp."""
        wanted = tuple(counter_ids)
        if not wanted:
            raise SnapshotError("a subset needs at least one counter id")
        return CommonKpiSnapshot(
            snapshot_id=subset_id,
            taken_at=self.taken_at,
            entries=tuple(entry for entry in self.entries if entry.counter_id in wanted),
            schema_version=self.schema_version,
            parent_snapshot_id=self.snapshot_id,
        )

    # -- content addressing ------------------------------------------------

    def to_canonical_dict(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "snapshotId": self.snapshot_id,
            "takenAt": self.taken_at,
            "schemaVersion": self.schema_version,
            "entries": [entry.to_canonical_dict() for entry in self.entries],
        }
        if self.parent_snapshot_id is not None:
            record["parentSnapshotId"] = self.parent_snapshot_id
        return record

    def content_hash(self) -> str:
        return content_hash(self.to_canonical_dict())


def snapshot_from_samples(
    *, snapshot_id: str, taken_at: str, samples: Sequence[RawSample]
) -> CommonKpiSnapshot:
    """Assemble a snapshot from collector samples, and from nothing else.

    The type check is the measurement-direction guarantee: only the
    Measurement Collector produces :class:`RawSample`, so nothing an advisory
    agent or an xApp writes can enter a snapshot through this door.
    """
    entries = []
    for sample in samples:
        if not isinstance(sample, RawSample):
            raise SnapshotError(
                "a snapshot is assembled from Measurement Collector RawSample "
                f"objects only, got {type(sample).__name__}"
            )
        entries.append(KpiSnapshotEntry(
            counter_id=sample.counter_id,
            value=sample.value,
            scope=sample.scope_snapshot,
            observed_at=sample.observed_at,
            clock_health=sample.clock_health,
            sample_id=sample.sample_id,
            sample_hash=sample.content_hash(),
        ))
    entries.sort(key=lambda entry: (entry.counter_id, entry.observed_at, entry.sample_id))
    return CommonKpiSnapshot(
        snapshot_id=snapshot_id, taken_at=taken_at, entries=tuple(entries)
    )
