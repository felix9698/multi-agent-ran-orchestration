#!/usr/bin/env python3
"""
Environment vs. coordinator-action axis separation (Batch G, P0-19).

The paper must be able to attribute a KPI change to EITHER an exogenous
environment change OR a coordinator action - never a shared knob. So:

  * ENVIRONMENT_AXES (offered load, channel gains, external disturbance, UE
    position) and ACTION_AXES (power_offset/rfatt, prb, sched_priority,
    mcs_offset) are DISJOINT. Their intersection is checked at startup and any
    overlap fails closed (a run that mixes them is rejected, not silently run).
  * One CANONICAL environment trace is generated PER paired block and replayed
    IDENTICALLY to every method, regardless of method order or how much RNG a
    method consumes. The trace is a concrete list of events materialised ONCE
    from the block seed, so replaying it is by-value and cannot drift.
  * Environment events and coordinator action events are stored as SEPARATE
    typed streams, each event carrying a timestamp and id.
  * A deterministic content hash of the canonical trace is recorded in the
    experiment record so any two runs can be proven to have used the same trace.

This module is stdlib-only (hashlib/json), consistent with metrics.py.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Fail-closed numeric validators (reject bool / NaN / inf)                    #
# --------------------------------------------------------------------------- #

def _finite(name: str, value) -> float:
    """Finite number, rejecting bool (a bool is not a valid timing/value)."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, got bool {value!r}")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} is not a number: {value!r}")
    if not math.isfinite(f):
        raise ValueError(f"{name} must be finite (no NaN/inf), got {f!r}")
    return f


def _finite_nonneg(name: str, value) -> float:
    """Finite and >= 0 (schedule offsets / timestamps)."""
    f = _finite(name, value)
    if f < 0:
        raise ValueError(f"{name} must be >= 0, got {f}")
    return f


def _finite_positive(name: str, value) -> float:
    """Finite and > 0 (durations, intervals, monotonic timestamps)."""
    f = _finite(name, value)
    if f <= 0:
        raise ValueError(f"{name} must be > 0, got {f}")
    return f


def _positive_int(name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive int, got {value!r}")
    return value


# --------------------------------------------------------------------------- #
# Axis taxonomy                                                               #
# --------------------------------------------------------------------------- #

# Coordinator ACTION axes (what the closed loop controls). "rfatt" is the OTA
# telnet name of the power axis and is INCLUDED so an environment generator can
# never write it (the handoff: "if rfatt is an action use offered load / channel
# / external disturbance as environment").
ACTION_AXES = frozenset({
    "power_offset", "rfatt", "prb", "sched_priority", "mcs_offset",
})

# EXOGENOUS environment axes (the phase script / channel). DISJOINT from the
# action axes by construction.
ENVIRONMENT_AXES = frozenset({
    "channel_serving_gain_db",
    "channel_neighbor_gain_db",
    "offered_load_mbps",
    "external_disturbance_db",
    "ue_position",
})


class AxisOverlapError(ValueError):
    """Raised when the environment and action axis sets intersect, or an
    environment event tries to write an action knob (fail-closed, P0-19)."""


def axis_intersection() -> frozenset:
    return ACTION_AXES & ENVIRONMENT_AXES


def assert_axes_disjoint() -> None:
    """Startup check: reject any environment/action axis overlap."""
    overlap = axis_intersection()
    if overlap:
        raise AxisOverlapError(
            f"environment and action axes overlap: {sorted(overlap)} "
            f"(environment generator must not write a coordinator action knob)")


# canonical phase-key -> ENVIRONMENT_AXES mapping (P0-19). Every exogenous
# environment axis a phase can request; NONE is a coordinator action knob. This
# is the SINGLE source of truth shared by the runner (_phase_requested_env) and
# any ExogenousEnvironmentDriver, so the "what the phase asked for" set can never
# diverge between what the runner validates and what a driver applies.
PHASE_KEY_TO_ENV_AXIS: Dict[str, str] = {
    "serving_gain": "channel_serving_gain_db",
    "neighbor_gain": "channel_neighbor_gain_db",
    "offered_load_mbps": "offered_load_mbps",
    "external_disturbance_db": "external_disturbance_db",
    "ue_position": "ue_position",
}


def phase_environment_request(phase: Dict) -> Dict[str, float]:
    """The {ENVIRONMENT axis: finite value} a phase requests (P0-19).

    Channel gains are ALWAYS present (default 0); offered load / disturbance /
    position are present only when the phase supplied them. Every value is finite
    (bool/NaN/inf rejected); every key is an ENVIRONMENT axis - a phase that
    carries a coordinator ACTION knob (rfatt / power_offset / prb / sched_priority
    / mcs_offset) is REJECTED, never silently ignored. This is the single source
    of truth for both the runner's live/emulated preflight and a driver's apply().
    """
    action_keys = set(phase) & ACTION_AXES
    if action_keys:
        raise AxisOverlapError(
            f"phase {phase.get('name')!r} carries coordinator ACTION axes "
            f"{sorted(action_keys)} - forbidden in an environment phase")
    requested: Dict[str, float] = {}
    for pkey, env_axis in PHASE_KEY_TO_ENV_AXIS.items():
        v = phase.get(pkey)
        if v is not None:
            requested[env_axis] = _finite(f"phase.{pkey}", v)
    requested.setdefault(
        "channel_serving_gain_db",
        _finite("phase.serving_gain", phase.get("serving_gain", 0)))
    requested.setdefault(
        "channel_neighbor_gain_db",
        _finite("phase.neighbor_gain", phase.get("neighbor_gain", 0)))
    return requested


# --------------------------------------------------------------------------- #
# Typed event streams                                                         #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EnvironmentEvent:
    """One exogenous environment change (a separate typed stream from actions).

    `t_s` is the SCHEDULED offset (planned seconds from the block start) - part
    of the canonical, reproducible trace. `applied_monotonic_s` is the ACTUAL
    monotonic clock time the event was applied during a live run (None until
    applied); it varies run-to-run and is deliberately EXCLUDED from the trace
    hash so the canonical schedule stays reproducible.
    """
    event_id: str
    t_s: float                        # scheduled offset (>= 0)
    axis: str
    value: float
    phase_idx: Optional[int] = None
    phase_name: Optional[str] = None      # the human phase name (e.g. "Nominal")
    phase_label: Optional[str] = None     # the UNIQUE identity "p{idx}:{name}"
    stream: str = "environment"
    applied_monotonic_s: Optional[float] = None

    def __post_init__(self):
        if self.stream != "environment":
            raise AxisOverlapError(
                f"environment event {self.event_id!r} has a forged stream "
                f"{self.stream!r} (must be exactly 'environment')")
        if self.axis not in ENVIRONMENT_AXES:
            # an environment event that names an ACTION axis is exactly the
            # forbidden overlap - reject it (P0-19 counterexample).
            raise AxisOverlapError(
                f"environment event {self.event_id!r} writes non-environment "
                f"axis {self.axis!r} (action knobs are forbidden in the "
                f"environment stream)")
        # fail-closed timings/value: reject bool/NaN/inf; scheduled offset >= 0.
        object.__setattr__(self, "t_s",
                           _finite_nonneg(f"{self.event_id}.t_s", self.t_s))
        object.__setattr__(self, "value",
                           _finite(f"{self.event_id}.value", self.value))
        # unique phase identity (P0-19): a real non-bool phase_idx, a nonempty
        # phase_name and the EXACT phase_label "p{idx}:{name}" - so two phases
        # with the SAME name (the two Nominal phases) stay DISTINCT.
        if self.phase_idx is not None:
            if isinstance(self.phase_idx, bool) \
                    or not isinstance(self.phase_idx, int) or self.phase_idx < 0:
                raise ValueError(
                    f"{self.event_id}.phase_idx must be a non-negative non-bool "
                    f"int, got {self.phase_idx!r}")
            name = self.phase_name
            if not isinstance(name, str) or not name.strip():
                raise ValueError(
                    f"{self.event_id}.phase_name must be a nonempty string when "
                    f"phase_idx is set, got {name!r}")
            expect = f"p{self.phase_idx}:{name}"
            if self.phase_label != expect:
                raise ValueError(
                    f"{self.event_id}.phase_label must be {expect!r} "
                    f"(p{{idx}}:{{name}}), got {self.phase_label!r}")
        if self.applied_monotonic_s is not None:
            object.__setattr__(self, "applied_monotonic_s", _finite_positive(
                f"{self.event_id}.applied_monotonic_s", self.applied_monotonic_s))

    @property
    def scheduled_offset_s(self) -> float:
        """Explicit alias: t_s is the scheduled offset, not the applied time."""
        return self.t_s

    def with_applied(self, monotonic_s: float) -> "EnvironmentEvent":
        """Return a copy stamped with the ACTUAL applied monotonic timestamp."""
        return replace(self, applied_monotonic_s=_finite_positive(
            f"{self.event_id}.applied_monotonic_s", monotonic_s))

    def canonical_content(self) -> list:
        """The reproducible semantic content (EXCLUDES applied time / event_id):
        scheduled offset, axis, value, and the FULL UNIQUE phase identity
        (phase_idx + phase_name + phase_label), stream. Including phase_name AND
        phase_label means two phases sharing a name still hash distinctly. The
        float t/value are LOSSLESS (repr round-trip) - NEVER round()-collapsed,
        so a sub-rounding difference in t or value changes the hash."""
        return [float(self.t_s), self.axis, float(self.value), self.phase_idx,
                self.phase_name, self.phase_label, self.stream]

    def to_dict(self) -> Dict:
        return {"event_id": self.event_id, "t_s": self.t_s,
                "scheduled_offset_s": self.t_s, "axis": self.axis,
                "value": self.value, "phase_idx": self.phase_idx,
                "phase_name": self.phase_name, "phase_label": self.phase_label,
                "stream": self.stream,
                "applied_monotonic_s": self.applied_monotonic_s}


@dataclass(frozen=True)
class ActionEvent:
    """One coordinator action application (separate typed stream from environment).

    Like EnvironmentEvent, `t_s` is the SCHEDULED/logical offset and
    `applied_monotonic_s` is the real monotonic time the write hit the executor.
    """
    event_id: str
    t_s: float
    axis: str
    value: float
    target: str                       # bs_id or ue_id the action addressed
    proposal_id: Optional[str] = None
    actuation_trial_id: Optional[str] = None
    stream: str = "action"
    applied_monotonic_s: Optional[float] = None

    def __post_init__(self):
        if self.stream != "action":
            raise AxisOverlapError(
                f"action event {self.event_id!r} has a forged stream "
                f"{self.stream!r} (must be exactly 'action')")
        if self.axis not in ACTION_AXES:
            raise AxisOverlapError(
                f"action event {self.event_id!r} writes non-action axis "
                f"{self.axis!r}")
        object.__setattr__(self, "t_s",
                           _finite_nonneg(f"{self.event_id}.t_s", self.t_s))
        object.__setattr__(self, "value",
                           _finite(f"{self.event_id}.value", self.value))
        if self.applied_monotonic_s is not None:
            object.__setattr__(self, "applied_monotonic_s", _finite_positive(
                f"{self.event_id}.applied_monotonic_s", self.applied_monotonic_s))

    def with_applied(self, monotonic_s: float) -> "ActionEvent":
        return replace(self, applied_monotonic_s=_finite_positive(
            f"{self.event_id}.applied_monotonic_s", monotonic_s))

    def to_dict(self) -> Dict:
        return {"event_id": self.event_id, "t_s": self.t_s,
                "scheduled_offset_s": self.t_s, "axis": self.axis,
                "value": self.value, "target": self.target,
                "proposal_id": self.proposal_id,
                "actuation_trial_id": self.actuation_trial_id,
                "stream": self.stream,
                "applied_monotonic_s": self.applied_monotonic_s}


# --------------------------------------------------------------------------- #
# Canonical environment trace                                                 #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EnvironmentTrace:
    """A concrete, materialised sequence of environment events for ONE block.

    Because the events are a fixed tuple built once from the block seed, the same
    trace REPLAYS IDENTICALLY to every method: replay() returns the events by
    value and consumes NO randomness, so method order and per-method RNG usage
    cannot change what any method sees.
    """
    block_id: str
    seed: int
    events: Tuple[EnvironmentEvent, ...]

    def __post_init__(self):
        # defence in depth: the events already validate themselves, but the trace
        # additionally REQUIRES a FULL unique phase identity on every event (a
        # standalone EnvironmentEvent may omit it, but a canonical TRACE may not)
        # and that every event is on an environment axis.
        seen_identity: Dict[int, tuple] = {}
        for ev in self.events:
            if ev.axis not in ENVIRONMENT_AXES:
                raise AxisOverlapError(
                    f"trace {self.block_id!r} event {ev.event_id!r} is not an "
                    f"environment axis: {ev.axis!r}")
            # UNIQUE phase identity (P0-19): non-bool non-negative int phase_idx,
            # nonempty phase_name, and phase_label == "p{idx}:{name}". Optional /
            # malformed identity is REJECTED at the trace level.
            if isinstance(ev.phase_idx, bool) \
                    or not isinstance(ev.phase_idx, int) or ev.phase_idx < 0:
                raise ValueError(
                    f"trace {self.block_id!r} event {ev.event_id!r} has an "
                    f"invalid/missing phase_idx {ev.phase_idx!r}")
            if not isinstance(ev.phase_name, str) or not ev.phase_name.strip():
                raise ValueError(
                    f"trace {self.block_id!r} event {ev.event_id!r} has an "
                    f"invalid/missing phase_name {ev.phase_name!r}")
            if ev.phase_label != f"p{ev.phase_idx}:{ev.phase_name}":
                raise ValueError(
                    f"trace {self.block_id!r} event {ev.event_id!r} phase_label "
                    f"{ev.phase_label!r} != expected "
                    f"'p{ev.phase_idx}:{ev.phase_name}'")
            # CROSS-EVENT consistency (P0-19): all events sharing a phase_idx MUST
            # carry the IDENTICAL (phase_name, phase_label) - two different phases
            # can never be collapsed under one index.
            ident = (ev.phase_name, ev.phase_label)
            prev = seen_identity.get(ev.phase_idx)
            if prev is not None and prev != ident:
                raise ValueError(
                    f"trace {self.block_id!r} phase index {ev.phase_idx} has "
                    f"INCONSISTENT identity across events: {prev} vs {ident}")
            seen_identity[ev.phase_idx] = ident

    def replay(self) -> Tuple[EnvironmentEvent, ...]:
        """Return the events by value (deterministic; consumes no RNG)."""
        return tuple(self.events)

    def canonical_hash(self) -> str:
        """Stable FULL SHA-256 content hash (64 hex, prefixed 'envtrace-sha256-')
        over the ORDERED FULL semantic content of every event - scheduled offset,
        axis, value, and the unique phase identity (phase_idx + phase_name +
        phase_label) + stream (canonical_content()) - so two traces that differ
        in ANY semantic field (including a phase label/name) hash differently.
        Independent of process, method order, RNG state and applied (runtime)
        timestamps (those are EXCLUDED). Full SHA-256, never a truncated SHA-1."""
        body = [e.canonical_content() for e in self.events]
        blob = json.dumps(body, sort_keys=True, separators=(",", ":"),
                          allow_nan=False)
        return "envtrace-sha256-" + hashlib.sha256(
            blob.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict:
        return {"block_id": self.block_id, "seed": int(self.seed),
                "hash": self.canonical_hash(),
                "n_events": len(self.events),
                "events": [e.to_dict() for e in self.events]}


def generate_environment_trace(
        phases: List[Dict], block_id: str, seed: int,
        offered_load_mbps: float = 12.0,
        steps_per_phase: int = 1,
        kpi_interval_s: float = 15.0) -> EnvironmentTrace:
    """Materialise the canonical environment trace for ONE paired block.

    The environment is the phase script: per phase we emit the serving/neighbor
    CHANNEL gains and the offered load as ENVIRONMENT events (never rfatt / an
    action knob). Determinism comes ONLY from `seed` + the phase table, so two
    blocks with the same seed produce byte-identical traces and any method order
    replays the same events. A small seeded per-phase external-disturbance event
    is included to exercise a non-phase-locked environment axis; it is drawn from
    a private RNG that no method touches.
    """
    import random as _random
    assert_axes_disjoint()
    # fail-closed timings (reject bool/NaN/inf/nonpositive) BEFORE any event.
    offered_load_mbps = _finite_positive("offered_load_mbps", offered_load_mbps)
    kpi_interval_s = _finite_positive("kpi_interval_s", kpi_interval_s)
    steps_per_phase = _positive_int("steps_per_phase", steps_per_phase)
    # the disturbance RNG is seeded by SEED ONLY (NOT block_id): two blocks with
    # the same seed + phases but different run/block labels produce the SAME
    # canonical semantic trace/hash (the block label is provenance, not content).
    rng = _random.Random(f"env:{seed}")
    events: List[EnvironmentEvent] = []
    t = 0.0
    n = 0
    for pidx, ph in enumerate(phases):
        # a phase dict may NEVER carry a coordinator ACTION knob (rfatt /
        # power_offset / prb / sched_priority / mcs_offset) - that would mix an
        # exogenous change with a control action. Reject it, never silently
        # ignore (P0-19).
        action_keys = set(ph) & ACTION_AXES
        if action_keys:
            raise AxisOverlapError(
                f"phase {pidx} carries coordinator ACTION axes "
                f"{sorted(action_keys)} - forbidden in an environment phase")
        sgain = _finite("serving_gain", ph.get("serving_gain", 0))
        ngain = _finite("neighbor_gain", ph.get("neighbor_gain", 0))
        # UNIQUE phase identity (P0-19): the human name is REQUIRED and nonempty;
        # the label is the exact "p{idx}:{name}" so the two Nominal phases (idx 0
        # and idx 4) stay DISTINCT (p0:Nominal vs p4:Nominal).
        name = ph.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"phase {pidx} must have a nonempty 'name', got {name!r}")
        label = f"p{pidx}:{name}"
        # one canonical disturbance per phase (seeded, method-independent)
        disturb = round(rng.uniform(-0.5, 0.5), 3)
        events.append(EnvironmentEvent(
            event_id=f"{block_id}:e{n}", t_s=t,
            axis="channel_serving_gain_db", value=sgain,
            phase_idx=pidx, phase_name=name, phase_label=label)); n += 1
        events.append(EnvironmentEvent(
            event_id=f"{block_id}:e{n}", t_s=t,
            axis="channel_neighbor_gain_db", value=ngain,
            phase_idx=pidx, phase_name=name, phase_label=label)); n += 1
        events.append(EnvironmentEvent(
            event_id=f"{block_id}:e{n}", t_s=t,
            axis="offered_load_mbps", value=float(offered_load_mbps),
            phase_idx=pidx, phase_name=name, phase_label=label)); n += 1
        events.append(EnvironmentEvent(
            event_id=f"{block_id}:e{n}", t_s=t,
            axis="external_disturbance_db", value=disturb,
            phase_idx=pidx, phase_name=name, phase_label=label)); n += 1
        t += steps_per_phase * kpi_interval_s
    return EnvironmentTrace(block_id=block_id, seed=int(seed),
                            events=tuple(events))


@dataclass
class EnvironmentActionLog:
    """Two SEPARATE typed streams recorded during a run (P0-19): the replayed
    environment events and the coordinator action events. Kept apart so a reader
    can distinguish exogenous change from control intervention."""
    environment: List[EnvironmentEvent] = field(default_factory=list)
    action: List[ActionEvent] = field(default_factory=list)

    def record_environment(self, ev: EnvironmentEvent) -> None:
        self.environment.append(ev)

    def record_action(self, ev: ActionEvent) -> None:
        self.action.append(ev)

    def to_dict(self) -> Dict:
        return {"environment": [e.to_dict() for e in self.environment],
                "action": [e.to_dict() for e in self.action]}


# --------------------------------------------------------------------------- #
# Live-mode exogenous environment driver (P0-19)                              #
# --------------------------------------------------------------------------- #

class ExogenousEnvironmentDriver:
    """LIVE-mode exogenous environment driver (P0-19).

    In live mode the coordinator controls the ACTION axes (power/rfatt, prb,
    sched_priority, mcs), so the exogenous environment MUST be driven through a
    PHYSICALLY SEPARATE mechanism that touches ONLY ENVIRONMENT_AXES - an offered
    load generator, an external attenuator, a shielded-path adjustment, or a UE
    position change - NEVER a coordinator action knob. A driver must be
    EXPLICITLY APPROVED (operator-qualified) before the live runner uses it; an
    absent or unapproved driver FAILS CLOSED with ZERO executor writes.

    Subclasses declare `supported_axes` (a nonempty subset of ENVIRONMENT_AXES),
    set `approved=True` only once the physical mechanism is qualified, and
    implement `apply(phase, phase_idx, phase_label)` to physically set the
    phase's environment - returning the {axis: value} actually applied (the
    runner validates it is within ENVIRONMENT_AXES and covers every requested
    axis, so a partial driver cannot silently drop one)."""
    supported_axes: frozenset = frozenset()
    approved: bool = False
    name: str = "abstract-exogenous-driver"

    def apply(self, phase: Dict, phase_idx: int, phase_label: str) -> Dict:
        raise NotImplementedError(
            "ExogenousEnvironmentDriver.apply must be implemented")


def validate_exogenous_driver(driver) -> None:
    """Fail-closed qualification of a LIVE exogenous environment driver (P0-19):
    it must exist, be EXPLICITLY approved, and declare ONLY environment axes."""
    if driver is None:
        raise AxisOverlapError(
            "live mode requires an explicitly injected exogenous environment "
            "driver (offered load / external attenuator / shielded path / UE "
            "position); none provided - refusing to run (fail closed)")
    # EXACT approval: `approved` must be the boolean True, not merely truthy (a
    # stray non-bool such as 1 or "yes" is a malformed flag, not a qualification).
    if getattr(driver, "approved", None) is not True:
        raise AxisOverlapError(
            f"exogenous environment driver "
            f"{getattr(driver, 'name', driver)!r} is NOT explicitly approved "
            f"(approved must be exactly True) - refusing (fail closed)")
    if not callable(getattr(driver, "apply", None)):
        raise AxisOverlapError(
            f"exogenous environment driver "
            f"{getattr(driver, 'name', driver)!r} has no callable apply() - "
            f"refusing (fail closed)")
    axes = frozenset(getattr(driver, "supported_axes", frozenset()))
    if not axes or not (axes <= ENVIRONMENT_AXES):
        raise AxisOverlapError(
            f"exogenous driver axes {sorted(axes)} must be a nonempty subset of "
            f"ENVIRONMENT_AXES (never a coordinator action knob)")


def require_driver_capability(driver, requested_axes) -> None:
    """Fail-closed CAPABILITY check (P0-19): the driver must SUPPORT every
    requested environment axis. An insufficient driver applies NOTHING (this
    raises BEFORE apply), never a partial write."""
    supported = frozenset(getattr(driver, "supported_axes", frozenset()))
    missing = set(requested_axes) - supported
    if missing:
        raise AxisOverlapError(
            f"exogenous driver lacks capability for axes {sorted(missing)} "
            f"(supported: {sorted(supported)}) - refusing to apply (fail closed)")


def validate_applied_environment(applied, requested) -> None:
    """Fail-closed validation of what a driver ACTUALLY applied (P0-19).

    `requested` is the {axis: value} map the phase asked for. Every applied axis
    must be an ENVIRONMENT axis (never an action knob); every REQUESTED axis must
    be covered (a partial/legacy driver cannot SILENTLY DROP an axis); and the
    applied VALUE must MATCH the requested value (a driver that reports a wrong
    value is rejected)."""
    if not isinstance(applied, dict):
        raise AxisOverlapError(
            "exogenous driver did not report the applied {axis: value} map")
    bad = set(applied) - ENVIRONMENT_AXES
    if bad:
        raise AxisOverlapError(
            f"exogenous driver wrote non-environment axes {sorted(bad)}")
    # EXACT axis set: no EXTRA (unrequested) environment axes either - the driver
    # must apply precisely what the phase requested, nothing more.
    extra = set(applied) - set(requested)
    if extra:
        raise AxisOverlapError(
            f"exogenous driver applied UNREQUESTED environment axes "
            f"{sorted(extra)} (must apply exactly the requested axes)")
    for axis, want in requested.items():
        if axis not in applied:
            raise AxisOverlapError(
                f"exogenous driver silently dropped requested environment axis "
                f"{axis!r} (a partial driver must fail closed, not drop)")
        got = applied[axis]
        if isinstance(got, bool) or not isinstance(got, (int, float)) \
                or not math.isfinite(float(got)) \
                or abs(float(got) - float(want)) > 1e-9:
            raise AxisOverlapError(
                f"exogenous driver applied a WRONG value for {axis!r}: "
                f"got {got!r}, requested {want!r}")


# --------------------------------------------------------------------------- #
# Concrete offered-load exogenous driver (§1-4 honest testbed driver)         #
# --------------------------------------------------------------------------- #

# The operator-approval environment variable. It must be EXACTLY "1" to qualify
# a driver; anything else (unset, "0", "true", "yes") leaves it UNAPPROVED. The
# code NEVER sets this itself - it is an out-of-band operator gesture.
APPROVAL_ENV_VAR = "AIC_ENV_DRIVER_APPROVED"


class OfferedLoadEnvironmentDriver(ExogenousEnvironmentDriver):
    """Live exogenous environment driver that varies ONLY the offered traffic
    load (§1-4 honest testbed driver, P0-19).

    Rationale (paper §1-4): §1-4 characterises the exogenous environment as the
    RAN operating condition - "cell load, interference, scheduler behavior"
    (§I) and the observation x_k = "traffic load, UE associations, radio
    measurements, and available RAN resources" (§II-B). The conflict it defines
    (active J1 = UE throughput >= 8 Mbit/s, Eq.4; arriving J2 = powerConsumption
    <= P_th, Fig 1) is driven by RESOURCE COMPETITION, not by a channel-gain
    script. §1-4 never prescribes a dB channel sweep (that is the §V-B table,
    which the operator has invalidated).

    On THIS testbed there is NO external attenuator, and the gNB TX attenuation
    (`ci rfatt`) is a COORDINATOR ACTION axis - using it to drive the environment
    would corrupt the action/environment separation (AxisOverlapError). The only
    exogenous axis that can be driven HONESTLY is therefore the offered traffic
    load (an iperf3 UDP generator), which modulates shared-cell resource
    competition. This driver:

      * supports the offered-load axis AND the two channel-gain axes - but ONLY
        at the trivial ZERO value (a 0 dB channel "change" is a physical no-op, so
        it can be reported honestly without an attenuator). A phase requesting a
        NON-ZERO channel gain FAILS CLOSED (this hardware cannot apply it);
      * physically sets the offered load through an injected `load_setter`
        callable (the real iperf3 offered-rate hook on the testbed; a recording
        double in tests). A missing load_setter FAILS CLOSED - it never pretends
        to have driven the environment;
      * is UNAPPROVED by default. `approved` becomes True ONLY via an explicit
        constructor argument or the operator env var (see `from_env`); the class
        never self-approves (fail-closed operator gate preserved).
    """

    supported_axes = frozenset({
        "offered_load_mbps",
        "channel_serving_gain_db",
        "channel_neighbor_gain_db",
    })
    name = "offered-load-iperf3"

    def __init__(self, load_setter: Optional[Callable[[float], None]] = None, *,
                 approved: bool = False, name: Optional[str] = None) -> None:
        if load_setter is not None and not callable(load_setter):
            raise ValueError("load_setter must be callable or None")
        self._load_setter = load_setter
        # EXACTLY True qualifies (validate_exogenous_driver checks `is True`); any
        # other value (including a truthy 1/"yes") stays unapproved. The default
        # is False - the code does not approve itself.
        self.approved = (approved is True)
        if name is not None:
            self.name = str(name)

    @classmethod
    def from_env(cls, load_setter: Optional[Callable[[float], None]] = None, *,
                 environ: Optional[Dict[str, str]] = None,
                 name: Optional[str] = None) -> "OfferedLoadEnvironmentDriver":
        """Build a driver whose approval comes ONLY from the operator env var
        (AIC_ENV_DRIVER_APPROVED == "1"). The env var is read here, not decided by
        the class; an unset/other value yields an UNAPPROVED driver that the live
        runner then refuses (fail closed). This is the sanctioned CLI path."""
        env = os.environ if environ is None else environ
        approved = env.get(APPROVAL_ENV_VAR) == "1"
        return cls(load_setter, approved=approved, name=name)

    def apply(self, phase: Dict, phase_idx: int, phase_label: str) -> Dict:
        """Physically apply ONE phase's exogenous environment: set the offered
        load, and assert every channel gain is a no-op zero. Returns the EXACT
        {axis: value} map the runner validates against the request. FAILS CLOSED
        (zero side effects) on a non-zero channel gain or a missing load_setter -
        BEFORE the load is ever touched, so a rejected phase moves nothing."""
        requested = phase_environment_request(phase)
        # 1) channel gains must be honest no-ops: this testbed has no attenuator,
        #    so a non-zero channel gain cannot be applied - fail closed BEFORE any
        #    load write (no partial side effect).
        for axis in ("channel_serving_gain_db", "channel_neighbor_gain_db"):
            val = requested.get(axis, 0.0)
            if abs(float(val)) > 1e-9:
                raise AxisOverlapError(
                    f"{self.name}: phase {phase_label!r} requests a non-zero "
                    f"{axis}={val} but this testbed has NO attenuator to drive "
                    f"the channel - refusing (fail closed). Drive the environment "
                    f"through offered load only, or provide a real channel driver")
        # 2) any axis beyond what this driver supports is out of capability -
        #    fail closed (e.g. a disturbance / ue_position axis this driver cannot
        #    physically move). require_driver_capability also enforces this at
        #    preflight; re-checked here so a direct apply() cannot bypass it.
        require_driver_capability(self, requested.keys())
        # 3) physically drive the offered load. A missing setter means the driver
        #    cannot honestly move the environment - fail closed rather than lie.
        if "offered_load_mbps" in requested:
            if self._load_setter is None:
                raise AxisOverlapError(
                    f"{self.name}: no load_setter wired - cannot physically apply "
                    f"offered_load_mbps={requested['offered_load_mbps']} "
                    f"(fail closed; wire the iperf3 offered-rate hook)")
            self._load_setter(float(requested["offered_load_mbps"]))
        # 4) report EXACTLY what was applied: the honest channel zeros plus the
        #    offered load actually set. Matches the request key-for-key so
        #    validate_applied_environment accepts it.
        return dict(requested)
