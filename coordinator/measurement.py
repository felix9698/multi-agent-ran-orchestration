#!/usr/bin/env python3
"""Batch F (P0-13 / P0-14): live-measurement contract.

A structured, validated, JSON-safe measurement Sample carrying value-or-UNKNOWN
plus full provenance (metric identity + direction + source, an epoch sample_time
in the SAME clock domain as action_apply_time, an optional monotonic timestamp,
duration, probe id, UE/BS scope, measurement error, freshness/cache status, and
the probe configuration). UNKNOWN is ALWAYS explicit (value=None + status +
error) - never NaN / inf / 0.

Also: an explicit PROBE CONFIG (offered load / protocol / direction / duration /
mode) that distinguishes GOODPUT-at-offered-load from a SATURATING CAPACITY
measurement, and a shared MeasurementScheduler (a single collection lock used by
BOTH the background metrics thread and the trial/negotiation probes) with
bounded, fail-closed acquisition so overlapping iperf for the same UE/resource
can never run.

Nothing here opens a socket, runs iperf, or touches hardware - it is the
contract + the scheduler; the actual probes are injected by the collector.
"""

from __future__ import annotations

import copy
import math
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Tuple


class MeasurementError(ValueError):
    """A malformed measurement sample / probe config (fail-closed)."""


def _deep_freeze_jsonsafe(value, *, ctx: str = "value"):
    """Recursively return a DEEPLY-IMMUTABLE, JSON-finite representation: dicts ->
    MappingProxyType, lists/tuples -> tuples; every float is validated finite
    (NaN/inf rejected) so json.dumps(..., allow_nan=False) can never fail on a
    frozen payload. Bools/ints/str/None pass through."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MeasurementError(f"{ctx} must be JSON-finite, got {value!r}")
        return value
    if isinstance(value, (int, str)) or value is None:
        return value
    if isinstance(value, (dict, MappingProxyType)):
        return MappingProxyType({
            str(k): _deep_freeze_jsonsafe(v, ctx=f"{ctx}.{k}")
            for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze_jsonsafe(v, ctx=f"{ctx}[]") for v in value)
    raise MeasurementError(
        f"{ctx} has un-serializable type {type(value).__name__}")


def _thaw_jsonsafe(value):
    """Convert a frozen representation back to ordinary mutable JSON-safe
    dict/list values (for to_dict())."""
    if isinstance(value, (MappingProxyType, dict)):
        return {k: _thaw_jsonsafe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_jsonsafe(v) for v in value]
    return value


def _valid_timeout(name: str, value) -> float:
    """A finite timeout >= 0 (fail-closed: NaN/inf/negative/non-number reject)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MeasurementError(f"{name} must be a number, got {value!r}")
    f = float(value)
    if not math.isfinite(f) or f < 0.0:
        raise MeasurementError(
            f"{name} must be a finite >= 0 timeout, got {value!r}")
    return f


class MeasurementPurpose(Enum):
    """WHY a sample was taken - admission/commit accept only post-action
    IN_WINDOW / COMMIT_READBACK samples, never PRE_ACTION / BACKGROUND."""
    PRE_ACTION = "pre_action"        # S3 baseline, immediately before the write
    IN_WINDOW = "in_window"          # S4 validation trajectory (post-action)
    NEGOTIATION = "negotiation"      # S5 per-round cost sample (fresh)
    BACKGROUND = "background"        # the background monitor thread
    COMMIT_READBACK = "commit_readback"   # the fresh pre-admission device read

    @classmethod
    def coerce(cls, value: Any) -> "MeasurementPurpose":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            v = value.strip().lower()
            for m in cls:
                if m.value == v or m.name.lower() == v:
                    return m
        raise MeasurementError(f"invalid MeasurementPurpose {value!r}")


class MeasurementStatus(Enum):
    """OK (a real finite value) vs UNKNOWN (no trustworthy value - explicit,
    never coerced to 0)."""
    OK = "ok"
    UNKNOWN = "unknown"


class ProbeMode(Enum):
    """GOODPUT measures the delivered rate AT a fixed offered load; CAPACITY
    measures the saturating throughput and REQUIRES a saturating offered load."""
    GOODPUT = "goodput"
    CAPACITY = "capacity"

    @classmethod
    def coerce(cls, value: Any) -> "ProbeMode":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            v = value.strip().lower()
            for m in cls:
                if m.value == v or m.name.lower() == v:
                    return m
        raise MeasurementError(f"invalid ProbeMode {value!r}")


def _finite_positive(name: str, value: Any, *, allow_zero: bool = False) -> float:
    """A strictly finite number that is > 0 (or >= 0 when allow_zero). Rejects
    bool / non-number / NaN / inf / negative (and zero unless allowed)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MeasurementError(f"{name} must be a number, got {value!r}")
    f = float(value)
    if not math.isfinite(f):
        raise MeasurementError(f"{name} must be finite, got {value!r}")
    if f < 0.0 or (f == 0.0 and not allow_zero):
        raise MeasurementError(
            f"{name} must be {'>= 0' if allow_zero else '> 0'}, got {value!r}")
    return f


# The ONLY throughput-probe combination the executor actually implements (an
# iperf3 UDP downlink session). Anything else is fail-closed rejected rather
# than advertised + silently mislabelled (P0-14, coordinator review #6).
SUPPORTED_PROTOCOLS = ("udp",)
SUPPORTED_DIRECTIONS = ("downlink",)

# A CAPACITY measurement requires the offered load to actually SATURATE the
# link. A self-declared low floor may NOT be used to relabel a small offered
# load as capacity: the effective floor is at least this trustworthy minimum, so
# a 12 Mbps probe can NEVER be accepted as a capacity measurement.
MIN_CAPACITY_SATURATION_FLOOR_MBPS = 50.0


def new_probe_id() -> str:
    """A unique probe id (each probe invocation gets its own)."""
    return f"probe-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class ProbeConfig:
    """The reproducible configuration of a throughput probe (P0-14). Every
    result / sample / evidence record carries this so offered load, protocol,
    direction, duration and mode are auditable and reproducible."""
    offered_load_mbps: float = 12.0
    protocol: str = "udp"
    direction: str = "downlink"
    duration_s: float = 2.0
    mode: ProbeMode = ProbeMode.GOODPUT

    def __post_init__(self):
        object.__setattr__(self, "offered_load_mbps",
                           _finite_positive("offered_load_mbps",
                                            self.offered_load_mbps))
        object.__setattr__(self, "duration_s",
                           _finite_positive("duration_s", self.duration_s))
        # FAIL-CLOSED: only the actually-implemented (udp, downlink) combination
        # is accepted - an unsupported protocol/direction is rejected rather than
        # advertised and then mislabelled by the udp-downlink executor (#6).
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise MeasurementError(
                f"unsupported protocol {self.protocol!r}; only "
                f"{list(SUPPORTED_PROTOCOLS)} is implemented")
        if self.direction not in SUPPORTED_DIRECTIONS:
            raise MeasurementError(
                f"unsupported direction {self.direction!r}; only "
                f"{list(SUPPORTED_DIRECTIONS)} is implemented")
        object.__setattr__(self, "mode", ProbeMode.coerce(self.mode))

    def to_dict(self) -> Dict:
        return {
            "offered_load_mbps": self.offered_load_mbps,
            "protocol": self.protocol,
            "direction": self.direction,
            "duration_s": self.duration_s,
            "mode": self.mode.value,
        }

    def udp_rate_arg(self) -> str:
        """The iperf3 -b argument for the offered load (e.g. '12M')."""
        return f"{self.offered_load_mbps:g}M"

    _ALLOWED_KEYS = frozenset(("offered_load_mbps", "protocol", "direction",
                               "duration_s", "mode"))

    @classmethod
    def from_dict(cls, d: Dict) -> "ProbeConfig":
        """Build a CANONICAL ProbeConfig from a dict, rejecting unknown keys and
        invalid values (so an arbitrary {'foo':'bar'} payload can never be
        accepted as a probe config)."""
        if not isinstance(d, (dict, MappingProxyType)):
            raise MeasurementError("ProbeConfig dict expected")
        unknown = set(d) - cls._ALLOWED_KEYS
        if unknown:
            raise MeasurementError(
                f"unknown ProbeConfig keys: {sorted(unknown)}")
        if "offered_load_mbps" not in d:
            raise MeasurementError("ProbeConfig requires offered_load_mbps")
        return cls(offered_load_mbps=d["offered_load_mbps"],
                   protocol=d.get("protocol", "udp"),
                   direction=d.get("direction", "downlink"),
                   duration_s=d.get("duration_s", 2.0),
                   mode=d.get("mode", ProbeMode.GOODPUT))


def capacity_offered_load_ok(cfg: ProbeConfig,
                             saturation_floor_mbps: float) -> bool:
    """A CAPACITY measurement is only valid when the offered load actually
    SATURATES the link (>= the saturation floor). A GOODPUT probe is always
    valid at its stated offered load. A non-saturating offered load may NEVER be
    accepted/labelled as a capacity measurement (fail-closed, P0-14)."""
    if cfg.mode != ProbeMode.CAPACITY:
        return True
    floor = _finite_positive("saturation_floor_mbps", saturation_floor_mbps)
    # the EFFECTIVE floor is at least the trustworthy minimum, so a self-declared
    # low floor can never validate a small (e.g. 12 Mbps) offered load as
    # capacity (#6).
    effective = max(floor, MIN_CAPACITY_SATURATION_FLOOR_MBPS)
    return cfg.offered_load_mbps >= effective


@dataclass(frozen=True)
class MeasurementSample:
    """One validated, JSON-safe measurement (P0-13).

    ``value`` is None for an UNKNOWN sample (status=UNKNOWN + error), otherwise a
    finite float (NaN/inf are rejected - never a fake 0). ``sample_time`` is an
    EPOCH timestamp in the SAME clock domain as the transaction's
    action_apply_time (so post-action freshness is a valid comparison);
    ``monotonic_time`` is an OPTIONAL monotonic reading (never compared to epoch
    time). ``source`` names the honest measured source/direction; ``probe_id`` is
    unique per probe invocation; ``purpose`` separates PRE_ACTION / IN_WINDOW /
    NEGOTIATION / BACKGROUND / COMMIT_READBACK.
    """
    metric: str
    value: Optional[float]
    status: MeasurementStatus
    purpose: MeasurementPurpose
    source: str
    sample_time: float
    probe_id: str
    direction: str = "downlink"
    monotonic_time: Optional[float] = None
    duration_s: Optional[float] = None
    freshness: str = "unknown"          # fresh | stale | unknown
    cache_status: str = "live"          # live | cached
    ue_scope: Tuple[str, ...] = ()
    bs_scope: Tuple[str, ...] = ()
    error: Optional[str] = None
    probe_config: Optional[Dict] = None

    def __post_init__(self):
        if not isinstance(self.metric, str) or not self.metric:
            raise MeasurementError("metric must be a non-empty string")
        object.__setattr__(self, "status", MeasurementStatus(self.status)
                           if not isinstance(self.status, MeasurementStatus)
                           else self.status)
        object.__setattr__(self, "purpose",
                           MeasurementPurpose.coerce(self.purpose))
        # value: finite float OR None (UNKNOWN). NaN/inf/bool are NEVER a value.
        v = self.value
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not math.isfinite(float(v)):
                raise MeasurementError(
                    f"sample value must be a finite number or None, got {v!r}")
            object.__setattr__(self, "value", float(v))
        # UNKNOWN <=> value is None (explicit, never a coerced 0). An UNKNOWN
        # sample MUST carry a non-empty error string (the reason).
        if self.status == MeasurementStatus.UNKNOWN:
            if self.value is not None:
                raise MeasurementError("UNKNOWN sample must have value=None")
            if not isinstance(self.error, str) or not self.error:
                raise MeasurementError(
                    "UNKNOWN sample must carry a non-empty error string")
        if self.status == MeasurementStatus.OK and self.value is None:
            raise MeasurementError("OK sample must carry a finite value")
        if not isinstance(self.sample_time, (int, float)) \
                or isinstance(self.sample_time, bool) \
                or not math.isfinite(float(self.sample_time)):
            raise MeasurementError("sample_time must be a finite epoch number")
        object.__setattr__(self, "sample_time", float(self.sample_time))
        if not isinstance(self.probe_id, str) or not self.probe_id:
            raise MeasurementError("probe_id must be a non-empty string")
        # provenance / status strings must be from the known vocabularies so a
        # sample can never carry a nonsense/JSON-unsafe label.
        if not isinstance(self.source, str) or not self.source:
            raise MeasurementError("source must be a non-empty string")
        if self.direction not in ("downlink", "uplink"):
            raise MeasurementError(
                f"direction must be downlink|uplink, got {self.direction!r}")
        if self.freshness not in ("fresh", "stale", "unknown"):
            raise MeasurementError(
                f"freshness must be fresh|stale|unknown, got {self.freshness!r}")
        if self.cache_status not in ("live", "cached"):
            raise MeasurementError(
                f"cache_status must be live|cached, got {self.cache_status!r}")
        if self.error is not None and not isinstance(self.error, str):
            raise MeasurementError("error must be a string or None")
        # optional monotonic_time must be a finite number when present.
        if self.monotonic_time is not None:
            mt = self.monotonic_time
            if isinstance(mt, bool) or not isinstance(mt, (int, float)) \
                    or not math.isfinite(float(mt)):
                raise MeasurementError(
                    f"monotonic_time must be a finite number or None, got {mt!r}")
            object.__setattr__(self, "monotonic_time", float(mt))
        # duration_s must be a POSITIVE finite number when present (a probe with
        # a negative/zero duration is meaningless).
        if self.duration_s is not None:
            ds = self.duration_s
            if isinstance(ds, bool) or not isinstance(ds, (int, float)) \
                    or not math.isfinite(float(ds)) or float(ds) <= 0.0:
                raise MeasurementError(
                    f"duration_s must be a positive finite number or None, "
                    f"got {ds!r}")
            object.__setattr__(self, "duration_s", float(ds))
        # a CACHED sample can never be reported as FRESH (contradiction).
        if self.cache_status == "cached" and self.freshness == "fresh":
            raise MeasurementError(
                "a cached sample cannot be freshness='fresh'")
        # a 'live' sample must not claim a cache source in its name.
        if self.cache_status == "live" and "cache" in str(self.source).lower():
            raise MeasurementError(
                "a live sample must not name a cache source")
        object.__setattr__(self, "ue_scope", tuple(str(u) for u in self.ue_scope))
        object.__setattr__(self, "bs_scope", tuple(str(b) for b in self.bs_scope))
        # CANONICALIZE the probe_config through ProbeConfig (rejects unknown keys
        # / bad values like {'foo':'bar'}), then DEEP-FREEZE the canonical to_dict
        # into a transitively-immutable, JSON-finite representation: a
        # caller/internal mutation (sample.probe_config['x']=9) RAISES, and a
        # nested NaN is rejected here (never only at a later json.dumps). to_dict
        # thaws it to a plain dict.
        if self.probe_config is not None:
            if not isinstance(self.probe_config, (dict, MappingProxyType)):
                raise MeasurementError("probe_config must be a dict or None")
            canonical = ProbeConfig.from_dict(dict(self.probe_config)).to_dict()
            object.__setattr__(self, "probe_config",
                               _deep_freeze_jsonsafe(canonical,
                                                     ctx="probe_config"))
            # CROSS-FIELD consistency for a throughput sample: the sample's
            # direction / duration must AGREE with its probe_config (a sample can
            # never claim uplink while its probe config is downlink, or a 999s
            # duration against a 2s config).
            if self.direction != canonical["direction"]:
                raise MeasurementError(
                    f"sample direction {self.direction!r} != probe_config "
                    f"direction {canonical['direction']!r}")
            if self.duration_s is not None \
                    and float(self.duration_s) != float(canonical["duration_s"]):
                raise MeasurementError(
                    f"sample duration_s {self.duration_s!r} != probe_config "
                    f"duration_s {canonical['duration_s']!r}")

    # --- semantics --------------------------------------------------------
    def is_unknown(self) -> bool:
        return self.status == MeasurementStatus.UNKNOWN or self.value is None

    def is_post_action(self, action_apply_time: Optional[float]) -> bool:
        """True iff this sample was taken AT OR AFTER the trusted (epoch)
        action_apply_time - the admission/commit freshness requirement. A
        PRE_ACTION / BACKGROUND sample or a missing apply time is NEVER
        post-action."""
        if self.purpose in (MeasurementPurpose.PRE_ACTION,
                             MeasurementPurpose.BACKGROUND):
            return False
        if action_apply_time is None:
            return False
        try:
            return self.sample_time >= float(action_apply_time)
        except (TypeError, ValueError):
            return False

    def admissible_for_commit(self, action_apply_time: Optional[float]) -> bool:
        """A sample may enter admission/commit evidence ONLY when it is a live,
        fresh, post-action, non-UNKNOWN measurement whose PURPOSE is IN_WINDOW or
        COMMIT_READBACK - a NEGOTIATION / PRE_ACTION / BACKGROUND sample is NEVER
        admissible even if fresh + post-action (P0-13, matches the coordinator's
        _sample_admissible_for_commit)."""
        return (self.purpose in (MeasurementPurpose.IN_WINDOW,
                                 MeasurementPurpose.COMMIT_READBACK)
                and not self.is_unknown()
                and self.cache_status == "live"
                and self.freshness == "fresh"
                and self.is_post_action(action_apply_time))

    def to_dict(self) -> Dict:
        return {
            "metric": self.metric,
            "value": self.value,                 # None for UNKNOWN (JSON-safe)
            "status": self.status.value,
            "purpose": self.purpose.value,
            "source": self.source,
            "direction": self.direction,
            "sample_time": self.sample_time,
            "monotonic_time": self.monotonic_time,
            "duration_s": self.duration_s,
            "freshness": self.freshness,
            "cache_status": self.cache_status,
            "probe_id": self.probe_id,
            "ue_scope": list(self.ue_scope),
            "bs_scope": list(self.bs_scope),
            "error": self.error,
            "probe_config": (_thaw_jsonsafe(self.probe_config)
                             if self.probe_config is not None else None),
        }

    # --- constructors -----------------------------------------------------
    @classmethod
    def unknown(cls, metric: str, purpose, source: str, sample_time: float,
                probe_id: str, error: str, **kw) -> "MeasurementSample":
        """Build an explicit UNKNOWN sample (value=None + error) - never a 0."""
        kw.pop("value", None)
        kw.pop("status", None)
        return cls(metric=metric, value=None, status=MeasurementStatus.UNKNOWN,
                   purpose=purpose, source=source, sample_time=sample_time,
                   probe_id=probe_id, error=error, **kw)

    @classmethod
    def ok(cls, metric: str, value: float, purpose, source: str,
           sample_time: float, probe_id: str, **kw) -> "MeasurementSample":
        return cls(metric=metric, value=value, status=MeasurementStatus.OK,
                   purpose=purpose, source=source, sample_time=sample_time,
                   probe_id=probe_id, **kw)


# ---------------------------------------------------------------------------
# Shared measurement scheduler / collection lock (P0-13 item 8)
# ---------------------------------------------------------------------------
class MeasurementBusy(Exception):
    """The shared measurement resource could not be acquired within the bound
    (a probe is already running) - the caller settles UNKNOWN (fail-closed),
    never overlapping the in-flight probe."""


class MeasurementScheduler:
    """A SINGLE collection lock shared by BOTH the background metrics thread and
    the trial/negotiation probes, so two iperf sessions for the same UE/resource
    can never overlap. Acquisition is BOUNDED (a timeout) and FAIL-CLOSED (raise
    MeasurementBusy rather than block forever or run concurrently). The lock
    covers the ACTUAL probe lifetime via the ``probe`` context manager."""

    def __init__(self, default_timeout_s: float = 10.0):
        self._lock = threading.Lock()
        # fail-closed: a NaN/inf/negative default timeout is rejected.
        self.default_timeout_s = _valid_timeout("default_timeout_s",
                                                default_timeout_s)
        # observability: the currently-held probe (None when free) + a peak
        # concurrency counter that a test can assert never exceeds 1.
        self._active: Optional[str] = None
        self._active_lock = threading.Lock()
        self._peak_concurrency = 0
        self._current = 0

    def try_acquire(self, timeout_s: Optional[float] = None) -> bool:
        to = (self.default_timeout_s if timeout_s is None
              else _valid_timeout("timeout_s", timeout_s))
        return self._lock.acquire(timeout=max(0.0, to))

    def release(self) -> None:
        try:
            self._lock.release()
        except RuntimeError:
            pass

    class _Probe:
        def __init__(self, sched, label, timeout_s):
            self._sched = sched
            self._label = label
            self._timeout = timeout_s
            self._held = False

        def __enter__(self):
            if not self._sched.try_acquire(self._timeout):
                raise MeasurementBusy(
                    f"measurement resource busy - probe {self._label!r} could "
                    f"not acquire within {self._timeout}s")
            self._held = True
            with self._sched._active_lock:
                self._sched._active = self._label
                self._sched._current += 1
                self._sched._peak_concurrency = max(
                    self._sched._peak_concurrency, self._sched._current)
            return self

        def __exit__(self, *exc):
            if self._held:
                with self._sched._active_lock:
                    self._sched._current -= 1
                    self._sched._active = None
                self._sched.release()
                self._held = False
            return False

    def probe(self, label: str = "probe",
              timeout_s: Optional[float] = None) -> "_Probe":
        """A context manager covering the probe lifetime; raises MeasurementBusy
        if the shared resource cannot be acquired within the bound."""
        to = (self.default_timeout_s if timeout_s is None
              else _valid_timeout("timeout_s", timeout_s))
        return MeasurementScheduler._Probe(self, label, to)

    @property
    def active_probe(self) -> Optional[str]:
        with self._active_lock:
            return self._active

    @property
    def peak_concurrency(self) -> int:
        with self._active_lock:
            return self._peak_concurrency
