#!/usr/bin/env python3
"""Batch E (P0-15): real history ablation.

A structured, validated, JSON- and deep-copy-safe HistoryRecord, a defensive
immutable reservoir snapshot, and a DETERMINISTIC relevance selector that
replaces the old last-five recency window. The three history modes are an
explicit, validated enum:

  * DISABLED         - zero history records reach any prompt/retrieval path and
                       the selector is never called.
  * FROZEN_RESERVOIR - retrieval reads a DEFENSIVE IMMUTABLE snapshot and never
                       learns new records into the retrieval dataset.
  * ONLINE_RESERVOIR - retrieval deterministically SELECTS relevant records and
                       new records are appended ONLY after mandatory episode
                       finalization (a real terminal-outcome evidence id).

Nothing here reaches the RAN, opens a socket, or touches hardware.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

_MISSING = object()

# The canonical action-axis FAMILIES. Stored records carry raw canonical action
# keys (e.g. "bs1_prb", "gnb1.cell.power_offset") and a retrieval query carries
# family hints (e.g. "prb"); BOTH sides are normalized to one of these families
# before overlap is computed, so a real bs1_prb record overlaps a prb query and
# a bs1_power_offset record does not (P0-15 relevance, coordinator review).
AXIS_FAMILIES: Tuple[str, ...] = ("sched_priority", "power", "prb", "mcs")


def canonical_axis_family(axis: Any) -> str:
    """Map a raw action axis / canonical key OR a query hint to its canonical
    family (deterministic). Unknown tokens map to themselves (lower-cased) so
    the function is total and never silently drops information."""
    s = str(axis).lower()
    if "sched" in s or "priority" in s:
        return "sched_priority"
    if "power" in s:
        return "power"
    if "prb" in s:
        return "prb"
    if "mcs" in s:
        return "mcs"
    return s


def _validate_timestamp(value: str) -> None:
    """Reject a malformed created_at timestamp (fail-closed)."""
    try:
        datetime.fromisoformat(value)
    except Exception:
        raise HistoryValidationError(f"invalid created_at timestamp {value!r}")


class HistoryMode(Enum):
    """Validated history-ablation mode (P0-15)."""
    DISABLED = "disabled"
    FROZEN_RESERVOIR = "frozen_reservoir"
    ONLINE_RESERVOIR = "online_reservoir"

    @classmethod
    def coerce(cls, value: Any) -> "HistoryMode":
        """Fail-closed coercion: an unknown/invalid mode RAISES (a malformed
        ablation config must never silently fall through to 'use history')."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            v = value.strip().lower()
            for m in cls:
                if m.value == v or m.name.lower() == v:
                    return m
        raise ValueError(f"invalid HistoryMode {value!r}")


class HistoryPolicy(Enum):
    """The retrieval treatment for the with-history method (P0-15). FROZEN reads
    a controlled immutable snapshot and never learns; ONLINE learns finalized
    outcomes within the active reset scope."""
    FROZEN = "frozen"
    ONLINE = "online"

    @classmethod
    def coerce(cls, value: Any) -> "HistoryPolicy":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            v = value.strip().lower()
            for m in cls:
                if m.value == v or m.name.lower() == v:
                    return m
        raise ValueError(f"invalid HistoryPolicy {value!r}")


class HistoryResetScope(Enum):
    """How often the runner resets the history reservoir (P0-15). RUN keeps one
    accumulating reservoir for the whole run; METHOD resets at each method;
    TRIAL resets at each (method, trial); MODEL resets whenever the active model
    changes. Fail-closed coercion on an invalid scope."""
    RUN = "run"
    TRIAL = "trial"
    METHOD = "method"
    MODEL = "model"

    @classmethod
    def coerce(cls, value: Any) -> "HistoryResetScope":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            v = value.strip().lower()
            for m in cls:
                if m.value == v or m.name.lower() == v:
                    return m
        raise ValueError(f"invalid HistoryResetScope {value!r}")


class HistoryValidationError(ValueError):
    """A malformed HistoryRecord (fail-closed: never stored/retrieved)."""


@dataclass(frozen=True)
class HistoryRecord:
    """One finalized action->outcome record for retrieval (P0-15).

    Immutable (frozen) + deep-copy safe (tuple ``action_axes``). Carries the
    minimum fields the selector and the paper's ablation need: the intent
    SIGNATURE (a stable content hash, never the mutable intent object), the
    ACTION AXES actually touched, the OPERATING REGIME, the PROPOSER id +
    MODEL version, and the REAL terminal-outcome evidence id. ``evidence_id``
    is REQUIRED and is never invented - a record is only built AFTER mandatory
    finalization from a real evidence bundle.
    """
    intent_signature: str
    action_axes: Tuple[str, ...]
    operating_regime: str
    proposer_id: str
    model_version: str
    evidence_id: str
    terminal_outcome: str = ""
    terminal_reason: str = ""
    created_at: str = ""

    def __post_init__(self):
        # fail-closed validation: the required identity + evidence fields must
        # be non-empty strings, and action_axes a tuple of strings.
        for name in ("intent_signature", "operating_regime", "proposer_id",
                     "model_version", "evidence_id"):
            v = getattr(self, name)
            if not isinstance(v, str) or not v:
                raise HistoryValidationError(
                    f"HistoryRecord.{name} must be a non-empty string, got {v!r}")
        axes = getattr(self, "action_axes")
        if not isinstance(axes, tuple) or any(
                isinstance(a, bool) or not isinstance(a, str) or not a
                for a in axes):
            raise HistoryValidationError(
                f"HistoryRecord.action_axes must be a tuple of non-empty "
                f"strings, got {axes!r}")
        for name in ("terminal_outcome", "terminal_reason", "created_at"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, str):
                raise HistoryValidationError(f"HistoryRecord.{name} must be str")
        if self.created_at:
            _validate_timestamp(self.created_at)

    def to_dict(self) -> Dict:
        """JSON-safe dict (deep copy of the immutable content)."""
        return {
            "intent_signature": self.intent_signature,
            "action_axes": list(self.action_axes),
            "operating_regime": self.operating_regime,
            "proposer_id": self.proposer_id,
            "model_version": self.model_version,
            "evidence_id": self.evidence_id,
            "terminal_outcome": self.terminal_outcome,
            "terminal_reason": self.terminal_reason,
            "created_at": self.created_at,
        }

    _ALLOWED_KEYS = frozenset((
        "intent_signature", "action_axes", "operating_regime", "proposer_id",
        "model_version", "evidence_id", "terminal_outcome", "terminal_reason",
        "created_at"))

    @classmethod
    def from_dict(cls, d: Dict) -> "HistoryRecord":
        """STRICT deserialization (P0-15, coordinator review): an exact dict
        schema with exact JSON types. No str/int/bool coercion - a wrong type,
        an unknown key, a missing required key, a non-list axes value, a bool
        masquerading as a string, or a malformed timestamp all FAIL CLOSED."""
        if not isinstance(d, dict):
            raise HistoryValidationError("HistoryRecord dict expected")
        unknown = set(d) - cls._ALLOWED_KEYS
        if unknown:
            raise HistoryValidationError(
                f"unknown HistoryRecord keys: {sorted(unknown)}")

        def _req_str(k: str) -> str:
            v = d.get(k, _MISSING)
            if v is _MISSING:
                raise HistoryValidationError(f"missing required key {k!r}")
            if isinstance(v, bool) or not isinstance(v, str) or not v:
                raise HistoryValidationError(
                    f"{k} must be a non-empty string, got {v!r}")
            return v

        def _opt_str(k: str) -> str:
            v = d.get(k, "")
            if isinstance(v, bool) or not isinstance(v, str):
                raise HistoryValidationError(f"{k} must be a string, got {v!r}")
            return v

        axes = d.get("action_axes", _MISSING)
        if axes is _MISSING:
            raise HistoryValidationError("missing required key 'action_axes'")
        # STRICT: the documented JSON schema requires a JSON array -> a Python
        # list EXACTLY. A tuple, string, set, or any other iterable is rejected
        # (deserializing untrusted JSON never yields a tuple, so accepting one
        # would mask a hand-built / non-JSON payload).
        if type(axes) is not list:
            raise HistoryValidationError(
                f"action_axes must be a JSON list, got {type(axes).__name__}")
        ax = tuple(axes)
        for a in ax:
            if isinstance(a, bool) or not isinstance(a, str) or not a:
                raise HistoryValidationError(
                    f"action_axes items must be non-empty strings, got {a!r}")
        return cls(
            intent_signature=_req_str("intent_signature"),
            action_axes=ax,
            operating_regime=_req_str("operating_regime"),
            proposer_id=_req_str("proposer_id"),
            model_version=_req_str("model_version"),
            evidence_id=_req_str("evidence_id"),
            terminal_outcome=_opt_str("terminal_outcome"),
            terminal_reason=_opt_str("terminal_reason"),
            created_at=_opt_str("created_at"),
        )

    def partition_key(self) -> Tuple[str, str, str]:
        """The (proposer_id, model_version, operating_regime) partition this
        record belongs to - retrieval never crosses this key (no proposer /
        model / regime leakage)."""
        return (self.proposer_id, self.model_version, self.operating_regime)


def snapshot_records(records) -> Tuple[HistoryRecord, ...]:
    """A DEFENSIVE IMMUTABLE snapshot (a tuple of frozen records). Frozen
    dataclasses with tuple fields are themselves immutable, so the returned
    tuple cannot be mutated or aliased back into the live reservoir."""
    out: List[HistoryRecord] = []
    for r in (records or ()):
        if isinstance(r, HistoryRecord):
            out.append(r)            # already immutable
        elif isinstance(r, dict):
            out.append(HistoryRecord.from_dict(r))
        else:
            raise HistoryValidationError(f"un-snapshotable record {r!r}")
    return tuple(out)


def restore_records(snapshot) -> List[HistoryRecord]:
    """Rebuild a mutable working list from a snapshot WITHOUT aliasing the
    caller's/frozen snapshot: each record is immutable, but the returned list
    is fresh so appends never touch the snapshot."""
    return list(snapshot_records(snapshot))


def _axis_overlap(a: Tuple[str, ...], b: Tuple[str, ...]) -> float:
    """Jaccard overlap of two action-axis sets (0..1), computed over the
    CANONICAL FAMILIES of each side so a stored raw key (bs1_prb) overlaps a
    family query hint (prb). empty/empty -> 0."""
    sa = {canonical_axis_family(x) for x in a}
    sb = {canonical_axis_family(x) for x in b}
    if not sa and not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def select_relevant(records,
                    intent_signature: str,
                    action_axes: Tuple[str, ...],
                    operating_regime: str,
                    proposer_id: str,
                    model_version: str,
                    mode=HistoryMode.ONLINE_RESERVOIR,
                    limit: int = 5) -> List[HistoryRecord]:
    """DETERMINISTIC relevance selection (P0-15), replacing last-five recency.

    ``mode`` is COERCED first (an invalid mode FAILS CLOSED via ValueError,
    never behaves like enabled). Records are partitioned to EXACTLY the query's
    (proposer_id, model_version, operating_regime) - no proposer/model/regime
    leakage. Within the partition they are scored by (1) intent-signature exact
    match, then (2) action-axis overlap over canonical families, with a STABLE
    tie-break on (created_at, evidence_id). DISABLED returns [] and does no work.
    """
    mode = HistoryMode.coerce(mode)
    if mode == HistoryMode.DISABLED:
        return []
    pool = [r for r in (records or ())
            if r.proposer_id == proposer_id
            and r.model_version == model_version
            and r.operating_regime == operating_regime]

    def _score(r: HistoryRecord):
        sig_match = 1 if r.intent_signature == intent_signature else 0
        overlap = _axis_overlap(r.action_axes, tuple(action_axes))
        # descending score, then STABLE ascending tie-break so selection is
        # deterministic and reproducible across runs.
        return (-sig_match, -overlap, r.created_at, r.evidence_id)

    return sorted(pool, key=_score)[:max(0, int(limit))]
