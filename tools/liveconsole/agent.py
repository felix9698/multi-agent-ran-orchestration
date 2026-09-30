"""The Agent's sitting: ``T`` rows x ``C`` columns, one joint case, one episode.

``build_live_session`` (:mod:`tools.liveconsole.build`) runs one sentence as
one case with one candidate.  This module is the Agent mode the owner asked
for, and it is the **executor** of the three-agent coordination the paper
measures:

* every sentence typed together is one intent of one **intent set**;
* the **Target** agent turns those intents and the owner's authorized
  relaxation limits into ``T`` -- the original requirement vector ``T0`` plus
  the alternatives the owner signed for.  ``T`` is the rows of the board;
* the **Control** agent turns ``T`` and the deployment's action space into
  ``C`` -- joint executable configurations.  ``C`` is the columns;
* the **Trajectory** agent picks the next ``(target, control)`` cell;
* one trial applies one control, and the single KPI vector it produces is
  judged against **every** authorized target, so one trial fills a whole
  column of the grid.

Five methods share this one executor: ``three-agent`` (the triad, each role on
an operator-chosen model), ``internal-monolith`` (one model: a formation call,
then a selection call per decision), ``basic-monolith`` (one model, one call
per decision, no grid, and it never receives our ``T``/``C``) and
``deterministic`` (no model at all: the fallback rules).  Which one runs
changes only who answers; the Kernel path, the judgement and the record are
identical, which is what makes the arms comparable.

What stays exactly as it was: the Kernel admits, freezes, permits, judges its
own predicates and settles; the Write Gateway is the only writer; every effect
travels R1 -> A1-P producer -> xApp -> E2SM-RC; readbacks are corroborated;
the producer's scope rules are honoured; a verified policy left by an earlier
sitting is a named precondition the operator authorises.  **No model output is
ever a verdict**: the target judgement is
:func:`assurance.coordination.judge` over measured KPIs, deterministic
executor code, and the roll-back is the Kernel's.

The same objects run ``MOCK`` and ``LIVE``.  In ``MOCK`` the KPM stream, the
policy port, the actuation adapters, the clock and the KPI observer come from
:mod:`tools.hfconsole.agent_env`; in ``LIVE`` this root composes all of them
itself and the KPI observer is :class:`~tools.liveconsole.kpi_observer.TunRateObserver`.
Nothing in the loop knows which.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from assurance.core.timebase import format_utc, parse_utc
from contextlib import contextmanager
import sys
import os
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from assurance.advisors.grammar import IntentGrammarEntry, IntentParseError, parse_utterance, scope_selector
from assurance.collector.live import build_live_collectors
from assurance.gateway.r1_operation_journal import JsonlR1OperationJournal
from assurance.contracts.target import ComparisonOperator
from assurance.coordination.rule_greedy import RULE_GREEDY
from assurance.coordination import (
    Authorization, BasicInputs, ClarificationNeeded as _AgentClarificationNeeded,
    CompatibilityRules, ControlCandidate, ControlCandidates, ControlInputs,
    DEFAULT_OBSERVATION_RULES, EPISODE_SCHEMA, EpisodeRecord, FunctionCatalog,
    FunctionSpec, GenerationOptions, Grid, IntakeChecklist, Intent,
    JointEffectPredictor, METHODS, METHOD_BASIC_MONOLITH, METHOD_DETERMINISTIC,
    FUNCTION_NAMING, METHOD_INTERNAL_MONOLITH, METHOD_THREE_AGENT,
    MonolithFormInputs, NetworkState,
    Observation, ObservationRules, PHASE_CLARIFICATION, PHASE_FORMATION,
    PHASE_INTAKE, PolicyField, Preference, ROLES, RULE_BASELINE, Requirement, RoleAgents,
    RoleModels, TargetContract, TargetInputs, TrajectoryInputs, Trial, UNKNOWN,
    UNSELECTED_FUNCTION_RULES, axis_kind, deterministic_controls, expand_targets, omega_or_sparse,
    intent_from_sentence, kpi_gaps, load_latency_calibration, merge_answers,
    observed_best, omega_contract, shown_target_ids, judge_target, verdict_of, PASS, translate_functions, validate_control_candidates, model_effect_evidence,
    DecisionUnavailable,
)
from assurance.coordination.agents import CallRecord, _original_requirements
from assurance.coordination import BasicDecision
from assurance.core.addressing import content_hash
from assurance.core.states import StopReason
from assurance.kernel.kernel import KernelRefusal
from assurance.live.joint_runtime import (
    JointLiveRuntime, SteeringParticipantSpec, SupplementaryParticipantSpec,
    build_joint_live_runtime,
)
from assurance.live.pin_to_cell_driver import (
    KpmUeAttributionReader, LivePinToCellDeployment, LiveTiming, LiveUeObservation, kpm_node_nb_id,
)
from assurance.objectives.action102_support import (
    ATTENUATION_ACTION_ID, CAP_ACTION_ID, MCS_ACTION_ID, PRIORITY_ACTION_ID,
    SLICE_QUOTA_ACTION_ID, SUPPLEMENTARY_ACTIONS,
)
from assurance.objectives.joint import (
    DEFAULT_ATTENUATION_LADDER, DEFAULT_CAP_LADDER, DEFAULT_MAX_CATALOG_CARDINALITY,
    DEFAULT_MCS_LADDER, DEFAULT_PF_LADDER, DEFAULT_SLICE_QUOTA_LADDER,
    INTENT_KIND_KPI, INTENT_KIND_SERVING_CELL, CapAxisSpec, CatalogTooLargeError,
    IntentSpec, JointComposition, JointCompositionError, McsBoundsAxisSpec,
    PriorityAxisSpec, SlicePrbQuotaAxisSpec, SteeringAxisSpec,
    TxAttenuationAxisSpec, catalog_cardinality, compose_joint,
)
# The composition's own ceiling check, borrowed rather than restated: the
# hardware-free root has to apply the operator's ceiling *before* it composes
# the emulator's runtime, and there must be exactly one refusal message.
from assurance.objectives.joint import _refuse_oversized_catalog
from gui.operator.sources.kernel_live import MODE_LIVE, polling_plan
from tools.g3ota.composition import (
    KpmTail, LivePolicyBuilder, RecordingPolicyPort, WallClockPorts,
    build_policy_type_discovery, build_r1_policy_port, clear_ue_scope,
    kpm_slot_occupancy, live_topology, producer_episode, scope_occupants,
)
from tools.g5ota.objective_live import FAMILY_UTTERANCES
from tools.liveconsole.build import (
    A1P_OBJECTIVE_KIND, LiveConsoleError, _FanOutTail, _build_supplementary, _plus_ms,
    _stamp, _write_scope_archive, family_for_utterance, live_capable_families,
    observe_selected_ue, scope_preconditions, session_mode,
    joint_serving_attribution, supplementary_policy_window_ms, CONCLUDE_RESERVE_MS,
)
from tools.liveconsole.kpi_observer import (
    CELL_GOODPUT_KPI, CELL_TX_ATTENUATION_KPI, DEADLINE_RATIO_KPI, SERVING_CELL_KPI,
    CompositeKpiObserver, KpiObserver, KpmCellAttenuationObserver, cell_attenuations_from_lines,
    KpiObserverError,
    LiveFlowGoodputObserver, LiveTaggedEchoObserver, TunRateObserver, build_profile_observer,
    live_echo_sources, live_flow_sources, resolve_ue_hosts, serving_cells_from_reader,
)
from tools.liveconsole.profile import LiveDeployment, load_live_deployment
# The judged window is wall-clock while a request's ``issuedAtMs`` is on the
# source UE's own monotonic clock, and ``_parse_time`` is the one parser the
# observation rules already read both shapes with.  Borrowed rather than
# restated (as ``_refuse_oversized_catalog`` is above) so the anchoring below
# cannot drift from the rule it anchors to.
from assurance.coordination.intake import _parse_time
from assurance.coordination.preference import p_rank as _p1_rank_of
from assurance.coordination.retention import (
    NO_ATTAINMENT as RETENTION_NO_ATTAINMENT, decide_retention, next_baseline,
    transition_scope,
)



def _echo_sources(observer: Any) -> Tuple[LiveTaggedEchoObserver, ...]:
    """Every live tagged-echo source behind a (possibly composite) observer."""
    sources = (observer.observers if isinstance(observer, CompositeKpiObserver)
               else (observer,))
    return tuple(source for source in sources
                 if isinstance(source, LiveTaggedEchoObserver))


def _service_row(observer: Any, now: Callable[[], str], errors: List[str]) -> Dict[str, Any]:
    """One service sample: its KPIs, whether the poll itself worked, and every
    collection gap the observer reported during it, timestamped.

    ``intervals`` is the source-clock span each interval KPI (goodput) covers.
    A failed source for one UE is a gap for that UE, not an invalid row: the
    other UEs' values in the same poll stay usable."""
    known = {id(row) for row in getattr(observer, 'failures', ())}
    valid = True
    try:
        observed = dict(observer.sample())
    except Exception as exc:  # an unreadable UE is a missing sample
        observed, valid = {}, False
        errors.append(f"{type(exc).__name__}: {exc}")
    stamp = now()
    gaps = [row for row in getattr(observer, 'failures', ()) if id(row) not in known]
    for gap in gaps:
        gap.setdefault("at", stamp)         # the same dicts kpiObserverFailures holds
    intervals = getattr(observer, 'intervals', None)
    return {"t": stamp, "kpis": observed, "valid": valid,
            "gaps": [dict(gap) for gap in gaps],
            "intervals": dict(intervals) if valid and isinstance(intervals, Mapping) else {}}


def _our_side_gap(gap: Mapping[str, Any]) -> bool:
    """Is this observer gap the bed's (a source that stopped) rather than a result?

    Everything the observer reports is a source that could not be read -- the load
    generator waiting for a UE that left ('no complete running heartbeat'), a
    re-bound or restarted source, an unreachable host, a tun that no longer exists --
    **except** a flow-payload mismatch: the tun received and our flow did not, which
    a control that starved the flow produces just as well.  That one is a result of
    the method and stays in the data (2026-09-22 codex audit).
    """
    gap = dict(gap or {})
    if str(gap.get("kind", "")) == "flow-payload-mismatch":
        return False
    return "flow payload did not advance" not in str(gap.get("error", ""))


def _unattributed(row: Mapping[str, Any], ue: str) -> bool:
    value = dict(row.get("kpis") or {}).get(f"{SERVING_CELL_KPI}@{ue}")
    return value is None or value == UNKNOWN


def _mark_excision(row: Dict[str, Any], history: Sequence[Mapping[str, Any]]) -> None:
    """Mark a service row that falls in an our-side interruption (``row["excised"]``).

    Two causes are read off the row itself, causally, so a live hold can be extended
    the moment it happens: an observer gap of the bed's (:func:`_our_side_gap`), and a
    UE that has been on no cell for longer than :data:`HANDOVER_NORMAL_MS` since it was
    last seen attributed.  A UE never seen attributed in this trace is not judged here.
    """
    reasons: List[str] = []
    # A poll that failed as a whole (``valid`` false) is a source that stopped too.
    if (not row.get("valid", True)
            or any(_our_side_gap(gap) for gap in row.get("gaps") or ())):
        reasons.append("observer-gap")
    stamp = _parse_time(row.get("t"))
    prefix = f"{SERVING_CELL_KPI}@"
    seen = {key[len(prefix):] for item in history[-600:]
            for key in dict(item.get("kpis") or {}) if key.startswith(prefix)}
    for ue in sorted(seen):
        if stamp is None or not _unattributed(row, ue):
            continue
        last = next((_parse_time(item.get("t")) for item in reversed(history)
                     if not _unattributed(item, ue)), None)
        if last is not None and stamp - last > HANDOVER_NORMAL_MS:
            reasons.append(f"handover-overlong@{ue}")
    if reasons:
        row["excised"] = reasons


def _wait_spans(waits: Iterable[Mapping[str, Any]]) -> List[Tuple[float, float]]:
    """``[(start, end)]`` in ms of recorded waits (``{"start": iso, "end": iso}``)."""
    spans = []
    for wait in waits or ():
        start, end = _parse_time(wait.get("start")), _parse_time(wait.get("end"))
        if start is not None and end is not None and end > start:
            spans.append((start, end))
    return spans


def _excised_spans(rows: Sequence[Mapping[str, Any]], cadence_ms: float,
                   keys: Tuple[str, ...] = ("excised", "holdExtension"),
                   ) -> List[Tuple[float, float]]:
    """``[(start, end)]`` ms each flagged row stands for: the whole gap since the row
    before it (one cadence for the first row).  A flagged row after 66 s without a
    sample stands for those 66 s -- the bed produced nothing then, which is the point."""
    spans: List[Tuple[float, float]] = []
    previous = None
    for row in rows:
        stamp = _parse_time(row.get("t"))
        if stamp is None:
            continue
        if any(row.get(key) for key in keys):
            spans.append((previous if previous is not None else stamp - float(cadence_ms), stamp))
        previous = stamp
    return spans


def _union_ms(spans: Iterable[Tuple[float, float]]) -> float:
    """Wall-clock length of the union of ``spans``, each merged run capped at one
    :data:`HARDWARE_WAIT_CAP_MS` (owner 2026-09-23: "너무 많이 기다리지 마라")."""
    merged: List[List[float]] = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(min(end - start, HARDWARE_WAIT_CAP_MS) for start, end in merged)


def _excluded_ms(rows: Sequence[Mapping[str, Any]], cadence_ms: float,
                 keys: Tuple[str, ...] = ("excised", "holdExtension"),
                 waits: Sequence[Tuple[float, float]] = ()) -> float:
    """Wall-clock milliseconds the flagged rows stand for, beyond what ``waits``
    already cover (a wait is its own record and relieves its own seconds).

    2026-09-24: this used to count **samples x cadence** -- a row stood for at most
    one cadence.  Every our-side stretch that produces no samples (an observer gap,
    a roll-back, a UE on no cell) was then charged to B almost whole: v46r10 board
    151803 excised 69 s of a 419 s stretch (76 samples) and ended ``DEADLINE``;
    board 153808 excised 136 s of 326 s.  Now it is the union of the time spans.
    """
    waits = list(waits)
    return max(0.0, _union_ms([*_excised_spans(rows, cadence_ms, keys), *waits])
               - _union_ms(waits))


def _without_excised(rows: Sequence[Mapping[str, Any]], cadence_ms: float
                     ) -> List[Dict[str, Any]]:
    """The rows a judgement may use, on a clock the excised time has been removed from.

    An excised row is dropped -- not a failure, not ``UNKNOWN``, a sample that does
    not exist -- and every earlier row is moved later by the time the excised rows
    after it stood for, so a window of width W ending at the last row reaches back W
    of *usable* time.  With nothing excised the rows come back unchanged.
    """
    if not any(row.get("excised") for row in rows):
        return [dict(row) for row in rows]
    stamps = [_parse_time(row.get("t")) for row in rows]
    kept: List[Dict[str, Any]] = []
    shift = 0.0
    for index in range(len(rows) - 1, -1, -1):
        row, stamp = rows[index], stamps[index]
        if row.get("excised"):
            before = next((stamps[j] for j in range(index - 1, -1, -1)
                           if stamps[j] is not None), None)
            gap = float(cadence_ms) if stamp is None or before is None else stamp - before
            shift += max(0.0, min(gap, float(cadence_ms)))
            continue
        kept.append(dict(row) if stamp is None or not shift
                    else {**dict(row), "t": stamp + shift})
    kept.reverse()
    return kept


def trial_reserve_ms(hold_ms: float, cadence_ms: float, trailing_ms: float) -> float:
    """What B must still hold for a trial to be started: its hold polls plus the
    trailing collection its longest deadline is owed (amendment 5/8).  One formula
    for the sitting's own check and for the Kernel case deadline composed from it."""
    cadence = max(1, int(cadence_ms))
    hold_polls = max(1, -(-int(hold_ms) // cadence)) + 1
    return float((hold_polls - (-int(trailing_ms) // cadence)) * cadence)


class _FormationSampler:
    """The service stream from input release until :meth:`AgentSitting.run`.

    A cold start's ``t0`` is the release of the inputs, and the service
    shortage is scored over ``[t0, t0 + H]`` -- Target/Control formation
    included.  Nothing else samples then, so this polls the same observer on
    the same clock and cadence into the rows that become ``serviceTrace``.  On
    the wall clock it is a background thread, stopped by the sitting's first
    own sample, by an operator stop, by a failed build, or at ``until_ms``; on
    a virtual clock preparation is one charged sleep, so :meth:`sample_for`
    samples through it inline instead.  No permit, no write, no trial.
    """

    def __init__(self, observer: Any, clock: Any, cadence_ms: int,
                 until_ms: Optional[float] = None) -> None:
        self.observer, self.clock = observer, clock
        self.cadence_ms = max(1, int(cadence_ms))
        self.until_ms = until_ms
        self.rows: List[Dict[str, Any]] = []
        self.errors: List[str] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _sample(self) -> None:
        row = _service_row(self.observer, self.clock.now, self.errors)
        _mark_excision(row, self.rows)
        self.rows.append(row)

    def sample_for(self, duration_ms: float) -> None:
        end = float(self.clock.monotonic_ms()) + float(duration_ms)
        while not self._stop.is_set():
            self._sample()
            left = end - float(self.clock.monotonic_ms())
            if left <= 0:
                break
            self.clock.sleep_ms(int(min(self.cadence_ms, left)))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="formation-sampler",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        due = float(self.clock.monotonic_ms())
        while not self._stop.is_set():
            if self.until_ms is not None and float(self.clock.monotonic_ms()) >= self.until_ms:
                break
            self._sample()
            due += self.cadence_ms
            self._stop.wait(max(0.0, due - float(self.clock.monotonic_ms())) / 1000.0)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()


def issued_cohort_ratios(rows: Sequence[Mapping[str, Any]], hold_end: Any,
                         rules: ObservationRules,
                         sources: Sequence[LiveTaggedEchoObserver],
                         sleep_ms: Optional[Callable[[int], None]] = None,
                         audit: Optional[Dict[str, Any]] = None, held: bool = True,
                         ) -> Dict[str, Any]:
    """``deadlineSuccessRatio`` over the requests **issued** in the judged window.

    The rule's own value for this KPI comes from differencing the source's
    *cumulative* counters between the sample at the window start and the last
    one.  A request becomes eligible when its deadline passes, so what that
    difference counts is the requests that **matured** inside the window --
    not the ones issued in it.  The two sets coincide only when the issue rate
    is flat across the preceding deadline, which is exactly when the defect is
    invisible.  This re-reads the same raw log for the cohort the owner's
    requirement actually names, and the value it returns replaces the
    differenced one wherever a live source can be re-read for it.

    **Anchoring the two clocks.**  The bounds the source filters on are on its
    own monotonic clock; the sitting's window is wall-clock.  Nothing is
    converted between the two epochs and no offset is estimated.  Instead the
    boundary is carried by the samples themselves: a live tagged-echo sample
    records the source's ``observedAtMs`` beside the sitting's ``t``, so the
    window is translated by taking the ``observedAtMs`` of the very two samples
    the rule would have differenced -- the last sample at or before
    ``hold_end - window`` (the settle boundary, since ``hold = settle +
    window``) and the last sample of the hold.  The cohort is therefore bounded
    by instants the source itself stamped.

    **Trailing collection.**  Every request issued in the window is owed its
    full deadline before the cohort can be read at all, so the extra snapshot
    is taken one longest-deadline *after* the window closed rather than at its
    edge.  The source still checks this itself and says
    ``collection-ended-early`` if it was read too soon.

    ``UNKNOWN`` -- the sitting's existing representation of an observation that
    establishes nothing, dropped from the KPI vector and recorded in
    ``unknownKpis`` -- whenever the source cannot support a service result: an
    empty cohort, a collection that ended early, a broken match, or a source
    that could not be re-read.  Never a fabricated ratio, and never a 0.0.

    ``audit``, when given, receives one record per KPI key: the issued count,
    the matched counts by deadline, validity with its ``invalidReason``, the
    source-clock window bounds and the cohort's sequence identity -- the same
    record for a valid cohort and an unusable one.  ``held=False`` says the
    judged configuration was not kept in place through the trailing
    collection (an emergency stop rolled it back first): no cohort is read and
    every source KPI is ``UNKNOWN`` with that reason.
    """
    end = _parse_time(hold_end)
    if not sources or end is None:
        return {}
    audit = {} if audit is None else audit
    deadlines = [float(value) for source in sources for value in source.deadlines_ms]
    if held and sleep_ms is not None and deadlines:
        sleep_ms(int(max(deadlines)))
    results: Dict[str, Any] = {}
    for source in sources:
        for ue, alias in dict(source.hosts).items():
            key = f"{DEADLINE_RATIO_KPI}@{ue}"
            # Fail closed from here: a cohort that cannot be established leaves
            # the KPI UNKNOWN rather than falling back on the matured set.
            results[key] = UNKNOWN
            audit[key] = {"valid": False, "invalidReason": None, "issued": None,
                          "matchedByDeadlineMs": {}, "levels": {}}
            if not held:
                audit[key]["invalidReason"] = "configuration-not-held-through-trailing-collection"
                continue
            series: List[Tuple[float, float]] = []
            for row in rows or ():
                row = dict(row or {})
                stamp = _parse_time(row.get("t", row.get("at")))
                value = dict(row.get("kpis") or {}).get(key)
                at = (_parse_time(dict(value).get("observedAtMs"))
                      if isinstance(value, Mapping) else None)
                if stamp is not None and at is not None and stamp <= end + 1e-6:
                    series.append((stamp, at))
            if not series:
                audit[key]["invalidReason"] = "source-not-sampled-in-hold"
                continue
            series.sort(key=lambda item: item[0])
            start = end - float(rules.rule_for(key).window_ms)
            before = [at for stamp, at in series if stamp <= start + 1e-6]
            inside = [at for stamp, at in series if stamp > start + 1e-6]
            first = before[-1] if before else (inside[0] if inside else None)
            last = series[-1][1]
            if first is None or last <= first:
                audit[key]["invalidReason"] = "window-not-bounded-by-two-source-samples"
                continue
            audit[key].update(windowStartMs=first, windowEndMs=last)
            failed = len(source.failures)
            probe = replace(source, hosts={ue: alias}, previous_source={},
                            window_start_ms=first, window_end_ms=last)
            value = probe.sample().get(key)
            if not isinstance(value, Mapping):
                # Unreadable or stale source: not a service result.  ``probe``
                # shares the source's failure list, so the error is there.
                audit[key]["invalidReason"] = "source-unreadable"
                audit[key]["error"] = next((row.get("error") for row in
                                            source.failures[failed:][-1:]), None)
                continue
            audit[key].update(observedAtMs=value.get("observedAtMs"),
                              cohort=dict(value.get("window") or {}))
            ratios: Dict[str, Any] = {}
            reasons: List[str] = []
            for deadline, counts in dict(value.get("byDeadlineMs") or {}).items():
                counts = dict(counts)
                issued = int(counts.get("issued", 0))
                audit[key]["issued"] = issued
                audit[key]["matchedByDeadlineMs"][str(deadline)] = int(counts.get("completed", 0))
                # ``valid`` is the source's own verdict on the cohort.  An
                # invalid level stays out of the vector (UNKNOWN for that level
                # alone) and its reason is kept, so the observation is invalid.
                level_valid = bool(counts.get("valid")) and issued > 0
                reason = None if level_valid else str(
                    counts.get("invalidReason") or "no-issued-requests")
                audit[key]["levels"][str(deadline)] = {"valid": level_valid,
                                                       "invalidReason": reason}
                if not level_valid:
                    reasons.append(f"{deadline}ms:{reason}")
                    continue
                # The denominator is every issued request, the ones that never
                # answered included; a valid cohort has been given its full
                # deadline, so nothing is still in flight.
                ratios[str(deadline)] = round(
                    min(1.0, max(0.0, int(counts["completed"]) / issued)), 6)
            if ratios:
                results[key] = {"byDeadlineMs": ratios}
            audit[key]["valid"] = bool(ratios) and not reasons
            audit[key]["invalidReason"] = (
                "; ".join(reasons) if reasons else None if ratios else "no-deadline-level")
    return results


__all__ = [
    "AGENT_EPISODE_SCHEMA",
    "ALL_AXES",
    "AXIS_KINDS",
    "AgentRequest",
    "AgentSitting",
    "BOUNDARY_KINDS",
    "ClarificationNeeded",
    "DEFAULT_AXIS_KINDS",
    "EPISODE_BOUNDARY",
    "EXCISED_TOTAL_CAP_MS",
    "HARDWARE_UNAVAILABLE",
    "HARDWARE_WAIT_CAP_MS",
    "INITIAL_TRIAL_INDEX",
    "IntentRow",
    "MAX_CLARIFICATION_ROUNDS",
    "NON_TRIAL_KINDS",
    "TIMING_COLD_START",
    "TIMING_MODES",
    "TIMING_PREPARED",
    "build_agent_sitting",
    "build_hardware_free_agent_sitting",
    "parse_agent_intents",
    "write_agent_evidence",
]

#: ``C``'s default size, as a multiple of the trial budget K (owner, 2026-09-08).
#: The frozen catalog stays the whole executable universe; this is only how much
#: of it the Trajectory role reads each round.
DEFAULT_RETAIN_PER_TRIAL = 4

#: What :meth:`AgentSitting.episode_record` writes.  The shared
#: :class:`~assurance.coordination.EpisodeRecord` is still ``EPISODE_SCHEMA``
#: (1.1.0); the executor's own record adds contract v4's counting, boundary,
#: non-trial-event and prepared-board keys on top of it, and 1.2.0 is what
#: ``experiments/agent_metrics.py`` reads those keys under -- including the
#: rule that a success window must complete within B, which 1.1.0 did not
#: impose.
#:
#: 1.3.0 is not a new key set.  It is three changes a reader cannot detect by
#: probing for keys, which is why the token had to move instead of the record
#: merely growing:
#:
#: * ``window.unknownKpis`` widened.  It still means "this KPI could not be
#:   judged", but it now also receives a KPI whose *issued* cohort was unusable
#:   (:func:`issued_cohort_ratios` fails closed on an empty cohort, a
#:   collection that ended early, a broken match or an unreadable source), not
#:   only one whose coverage fell below the minimum.  Same key, two causes, and
#:   nothing in the record separates them.
#: * Per-generation ``inputTokens``/``outputTokens`` became nullable -- ``null``
#:   is "the provider reported nothing", which a zero used to swallow -- and the
#:   prompt compaction moved the input-token baseline on the grid-carrying arms.
#:   Token counts are not comparable across this boundary.
#: * ``T.alternatives`` changed population for the internal monolith, whose
#:   formation schema gained ``alternatives``: a full-domain expansion became a
#:   bounded selection, so ``selectedCount`` is not the same quantity before and
#:   after.
#:
#: Any reader that enumerates versions must gain this one in the SAME change:
#: ``experiments/agent_metrics.load_episodes`` is a closed allow-list and
#: silently returns zero episodes for a version it does not list.
AGENT_EPISODE_SCHEMA = "agent-episode/1.3.0"

#: The experiment definition an episode ran under (amendment
#: ``v3.1-select10-existing3``).  ``AIC_EXPERIMENT_VERSION`` overrides it, so a
#: block started under another definition keeps its own label.
EXPERIMENT_VERSION = "v3.1-select10-existing3"


@functools.lru_cache(maxsize=1)
def _code_revision() -> Optional[str]:
    """The checked-out commit, read from ``.git`` itself; ``None`` when unavailable.

    No ``git`` process: an episode record is written inside hermetic runs that
    forbid subprocesses.  It names the commit only, not uncommitted edits.
    """
    git = Path(__file__).resolve().parents[2] / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="ascii").strip()
        if not head.startswith("ref: "):
            return head or None
        ref = head[len("ref: "):]
        if (git / ref).is_file():
            return (git / ref).read_text(encoding="ascii").strip() or None
        for line in (git / "packed-refs").read_text(encoding="ascii").splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0]
    except (OSError, UnicodeError):
        pass
    return None

#: 판의 거동을 정하는 소스 트리.  `_source_digest` 가 이 아래만 읽는다.
#: `experiments/` 는 **뺀다** -- 판은 그 오프라인 분석 코드를 부르지 않는다(유일한 예외
#: `experiments/metrics.py` 는 `assurance/batch` 가 쓰므로 따로 넣는다).  넣으면 지표를
#: 고칠 때마다 해시가 바뀌어, 판의 거동이 그대로인데도 경계처럼 보인다.
_SOURCE_ROOTS: Tuple[str, ...] = ("assurance", "tools/liveconsole", "decision", "oran")
_SOURCE_FILES: Tuple[str, ...] = ("experiments/metrics.py",)


def _source_digest() -> Optional[str]:
    """지금 트리의 소스 내용 해시.  `codeRevision` 이 못 말하는 것을 말한다.

    `_code_revision()` 은 **커밋만** 말한다(그 docstring이 그렇게 적혀 있다).  트리가
    더러우면 미커밋 수정이 판의 거동을 바꾸는데도 모든 판이 같은 커밋 해시를 적고,
    고치기 전 판과 고친 뒤 판이 **기록만 보고는 구분되지 않는다** (2026-09-23 실측:
    blocks18-v46 의 네 판이 전부 `0d3d8a6b2…`).  경계 시각을 사람이 기억해야 하는 기록은
    기록이 아니다.

    `git` 프로세스를 띄우지 않는다 -- 판 기록은 subprocess 를 금지하는 hermetic 실행
    안에서도 쓰인다.  읽기는 한 판에 한 번이고 수 MB 라 10분짜리 판에 무시할 만하다.
    """
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    try:
        paths = sorted({*(path for name in _SOURCE_ROOTS
                          for path in (root / name).rglob("*.py")
                          if "__pycache__" not in path.parts),
                        *(root / name for name in _SOURCE_FILES)})
        for path in paths:
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(path.read_bytes())
    except (OSError, ValueError):
        return None
    return digest.hexdigest()


#: 이 프로세스가 불러온 코드의 해시 -- 판 기록에는 이 값을 싣는다.
_SOURCE_DIGEST_AT_IMPORT = _source_digest()

#: The joint-composition family an intent folds into when its sentence names
#: none.  ``UELevelTarget`` is the one family whose evidence this deployment
#: (and the hardware-free emulator) delivers end to end for any UE.
DEFAULT_JOINT_FAMILY = "UELevelTarget"

#: Termination reasons (contract 2.5).  ``T0_SUCCESS`` is the only exit code 0.
T0_SUCCESS = "T0_SUCCESS"
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
CATALOG_EXHAUSTED = "CATALOG_EXHAUSTED"
DEADLINE = "DEADLINE"
OPERATOR_STOP = "OPERATOR_STOP"
KERNEL_TERMINATED = "KERNEL_TERMINATED"
EXECUTION_FAILURE = "EXECUTION_FAILURE"
#: A proposal that could not be dispatched after one bounded revision.  Never
#: an exhaustion claim: CATALOG_EXHAUSTED requires that no eligible candidate
#: remains, which this does not establish.
PROPOSAL_FAILURE = "PROPOSAL_FAILURE"
#: A declared episode boundary whose defining inputs are the ones ``T`` and
#: ``C`` were formed from (contract v4 section 3): the sitting may not keep
#: proposing cells of a board whose rows and columns no longer describe the
#: episode, and it cannot re-form them inside a frozen epoch, so it stops here
#: and the runner composes the next sitting.
EPISODE_BOUNDARY = "EPISODE_BOUNDARY"
#: The bed could not be brought back within the waits below: a UE that did not
#: return, a rebind that never succeeded, or our-side interruptions adding up
#: past :data:`EXCISED_TOTAL_CAP_MS`.  Separate from ``DEADLINE`` on purpose --
#: B is never charged for these, so mixing them into ``DEADLINE`` would count
#: a hardware fault as a method that ran out of search time.
HARDWARE_UNAVAILABLE = "HARDWARE_UNAVAILABLE"

# -- our-side interruptions ("볼드모트": the interval does not exist) ---------- #
# 오너 지시(2026-09-23): 우리 측 문제로 UE 가 끊기거나 핸드오버가 오래 걸린 구간은
# 시간으로 세지 않고, 그동안의 KPM 표본도 쓰지 않는다 -- 판은 그 구간이 없었던 것처럼
# 이어간다.  단 "너무 많이 기다리지 마라": 예전엔 40분씩 기다려 판이 진행되지 않았다.
#: One wait for the bed (a dropped UE, a role not yet addressable, a rebind that
#: keeps failing).  Measured returns on 2026-09-23: 78 s and 173 s; the keeper's
#: mid-board revival gap on 2026-09-18: median 10 s, p99 21 s.
HARDWARE_WAIT_CAP_MS = 300_000.0
#: Everything one board excised -- waits, observer gaps, over-long handovers and
#: the hold extensions that refill a judged window.  Past this the bed is not in a
#: state to run a board: the sitting ends :data:`HARDWARE_UNAVAILABLE` and the
#: campaign moves on to the next one.
EXCISED_TOTAL_CAP_MS = 600_000.0
#: A handover that leaves the UE on no cell for longer than this is the bed's, not
#: the steering's: measured handovers were almost all <= 4.4 s.  The first 4.4 s
#: stay a cost of the control that steered; only the excess is excised.
HANDOVER_NORMAL_MS = 4_400.0
#: Between two attempts to rebind a re-registered UE whose new id is not yet
#: observable (the observation window itself is ~10 s).
REBIND_RETRY_MS = 5_000
#: How long composition polls the KPM stream for a cell's live attenuation before
#: refusing the power axis rather than guessing its baseline.
CELL_BASELINE_WAIT_MS = 15_000

#: Stop reasons that say the *execution path* failed rather than that this
#: configuration was a poor choice: the write got partway and could not be
#: confirmed, or the adapter errored outright.  Reply 2026-09-14 section 7 --
#: a shared prerequisite failure "should return through the common episode
#: stop/recovery handling, rather than consume eight model choices on
#: configurations that all require the same unavailable baseline."
EXECUTION_PATH_STOPS = ("PARTIAL_APPLY", "EXECUTION_ERROR")


def _execution_path_failure(trial: "Trial") -> Optional[Tuple[str, str]]:
    """The signature of an execution-path failure, or ``None`` if this trial
    is not one.

    Read from the Kernel's own enums -- ``TrialOutcome`` and the stop reason --
    and never from a message, because the two cases the reply distinguishes are
    already separate values there.  A trial whose *predicates* failed
    (``SEMANTIC_NON_SUCCESS``) is deliberately not a signature: that is a
    candidate-specific rejection and another candidate may still work.
    """
    kernel = trial.kernel or {}
    outcome = str(kernel.get("outcome") or "")
    stop = str(kernel.get("stopReason") or "")
    if outcome == "EXEC_ERROR" or stop in EXECUTION_PATH_STOPS:
        return (outcome, stop)
    return None

#: The two timing modes of ``exp_metrics.md`` section 3, kept separate and
#: never mixed in one summary.
TIMING_PREPARED = "prepared"

#: How many predictor rows ``input.effect_evidence`` carries.
#:
#: v2 (``owner-policy-joint-control-v2``): **the full product is no longer sent
#: to anybody.**  A captured formation request measured 83,050 B, of which
#: ``input.effect_evidence`` was 76,542 B -- 93.2% -- and its ``predictions``
#: list was 74,797 B over 144 rows, one per configuration the catalog admits.
#: Everything the model was asked to *interpret* (intents, authorization, the
#: function catalog, compatibility) came to 6.8%.  Enumerating and predicting
#: the whole Cartesian product in code, then asking the model to rank it, is
#: what the handoff removes: the agent should compose from function profiles
#: and compact effect evidence, not re-rank a finished table.
#:
#: ``prediction_table`` already produces exactly the compact form when the
#: product does not fit: the baseline, every single-axis move, and joint moves
#: while they fit.  So the change is the *limit*, not new code.  13 = 1 baseline
#: + 12 single-axis moves for the six axes this deployment exposes (3+2+3 cap
#: rungs and 2+2+2 cells, minus the baseline in each); it keeps the whole
#: single-function picture and drops the pairwise tail, taking the block from
#: 29,185 B to 2,631 B on the measured action space.
EVIDENCE_ROWS = 13
TIMING_COLD_START = "cold-start"
TIMING_MODES: Tuple[str, ...] = (TIMING_PREPARED, TIMING_COLD_START)

#: What a declared episode boundary may come from (``exp_metrics.md`` section
#: 1: "original-intent, owner-policy, or exogenous-condition changes").  Normal
#: fading, a control change and a new approval under the same policy are none
#: of these and are therefore not boundaries.
BOUNDARY_KINDS: Tuple[str, ...] = ("intent", "policy", "exogenous")

#: Which boundary kinds invalidate the prepared ``T`` and ``C``.  Their
#: defining inputs are the intents and the owner's authorization policy, so a
#: change to either re-forms them; an exogenous condition change does not
#: (contract v4 section 3, last sentence).
TC_INVALIDATING_BOUNDARIES: Tuple[str, ...] = ("intent", "policy")

#: The events that are charged to the episode but are **not** trials
#: (``exp_metrics.md`` section 3: "Rejected proposals, KPI samples, and
#: rollback alone are separate events, not trials; their time remains
#: charged").  ``remeasurement`` is the executor's re-observation of the
#: applied configuration -- no permit, no write, no candidate spent.
#: ``decision-timeout`` is one of ours, not ``exp_metrics.md``'s: a decision
#: that outran its allowance consumed episode time and produced no dispatch,
#: which is exactly the "charged but not a trial" shape the section describes.
#: It was *declared* by ``_decision_overrun`` and missing from this tuple, so
#: the one path that records a late answer raised ``LiveConsoleError`` and took
#: the whole episode down instead of ending it as ``PROPOSAL_FAILURE`` --
#: reliably, because the observation bundle expires in 10 s while a decision
#: call takes 7-122 s.
#: 2026-09-18 오너 지시로 두 초과 모두 **판을 끝내지 않고 기록만** 한다 --
#: ``formation-overrun`` 은 첫 제안 허용 시간 초과다.
#: 2026-09-22: ``unjudgeable-window`` 은 계약이 판정에 쓰는 관측 키 하나가 창에
#: 없어 "아무 목표도 달성하지 못했다" 를 **보일 수 없는** 창이다.  침묵을 실패로
#: 읽지 않으려고 `_attains_no_target` 이 이것을 선언하는데, 여기 빠져 있어
#: `decision-timeout` 때와 **똑같이** 판을 통째로 떨어뜨렸다(09-22 18:01 판에서 3회).
NON_TRIAL_KINDS: Tuple[str, ...] = (
    "decision-timeout", "formation-overrun", "re-ask", "rejected-proposal",
    "remeasurement", "reset-to-baseline", "rollback", "sample",
    "unjudgeable-window")

#: ``MOCK`` only: the least virtual time a refused answer costs before it is
#: asked again (a live call always costs its own wall time).
MOCK_REASK_FLOOR_MS = 1000

#: The index the common initial measurement is recorded at, for every method
#: (``exp_metrics.md`` section 3).  It is never counted as a live trial.
INITIAL_TRIAL_INDEX = 0

#: How many times the operator is asked before the sitting refuses to start
#: (contract v2 section 2.2).
MAX_CLARIFICATION_ROUNDS = 2

#: Every action-axis kind this executor can expose, in the order a refusal
#: names them.  The names are the deployment's own (``FUNCTION_NAMING``), not
#: a second vocabulary invented here.
AXIS_KINDS: Tuple[str, ...] = tuple(FUNCTION_NAMING)

#: The word ``--axes`` takes to mean "every kind this build knows".
ALL_AXES = "all"

#: What a sitting exposes when the operator says nothing (contract v3 section
#: 3).  Steering, the DL PRB cap and the scheduler weight on **every intent
#: UE** -- with two UEs over two cells that is 4 x 16 x 16 = 1024 candidates,
#: sixteen times what the same sitting exposed before cap and PF were on by
#: default, and comfortably inside the stated ceiling.  The cell- and
#: slice-scoped kinds are declared, wired and laddered, and the operator turns
#: them on deliberately with ``--axes`` because the product of all six is
#: 248832 -- over any ceiling worth freezing.  ``--axes all`` is one flag for
#: the whole space, and it refuses by name rather than truncating.
DEFAULT_AXIS_KINDS: Tuple[str, ...] = ("servingCell", "dlPrbCap", "pfWeight")

#: How many function/scope entries one configuration may move off baseline.
#: The integrated reply of 2026-09-14 section 4: "use the common limit of four
#: changed function/scope entries per configuration" -- *common*, so it is set
#: here, where every arm's rules are built, rather than per method.
MAX_CHANGED_ENTRIES = 4

#: Steering is not optional: ``compose_joint`` requires every intent UE to
#: have its steering action declared, so dropping this kind would leave the
#: sitting with an intent nothing can act on.
MANDATORY_AXIS_KIND = "servingCell"

#: The per-kind ladders a sitting uses when the operator overrides none.
DEFAULT_LADDERS: Mapping[str, Tuple[Any, ...]] = {
    "dlPrbCap": DEFAULT_CAP_LADDER, "pfWeight": DEFAULT_PF_LADDER,
    "dlMcsBounds": DEFAULT_MCS_LADDER,
    "txAttenuationDb": DEFAULT_ATTENUATION_LADDER,
    "slicePrbQuota": DEFAULT_SLICE_QUOTA_LADDER,
}

#: Which supplementary action carries which axis kind, so one loop can compose
#: all five without a second table of its own.
SUPPLEMENTARY_BY_KIND: Mapping[str, str] = {
    SUPPLEMENTARY_ACTIONS[action_id].axis: action_id for action_id in (
        CAP_ACTION_ID, PRIORITY_ACTION_ID, MCS_ACTION_ID, ATTENUATION_ACTION_ID,
        SLICE_QUOTA_ACTION_ID)}

#: What this build cannot yet carry to the radio, and why -- named per axis
#: kind so a refusal says which axis and which missing piece, never "unsupported".
#: The action producer serves all five policy types; what is missing is the
#: *configuration readback* that proves the write landed, and a control whose
#: effect cannot be corroborated is not composed at all rather than composed
#: and always UNKNOWN.  ``tools/campaign5/live_run.py`` records the same two
#: facts about the cell-scoped families on this wire.
#: ``txAttenuationDb`` left this table on 2026-09-16: build.KpmCellConfigReader reads
#: RAN.Cell.TxAttenuationDb by (e2_node, connection_epoch), which is how the node-level
#: indication is actually keyed on this wire, and it was verified against the live
#: stream returning {"txAttenuationDb": 0.0} for nb=2816 epoch 822 and SCOPE_MISMATCH
#: for a cell it does not serve.
LIVE_UNCARRIED_AXES: Mapping[str, str] = {
    "dlMcsBounds": ("a cell-scoped configuration reader for "
                    "RAN.Cell.DlMcsBounds (two policy leaves against one "
                    "scalar KPM record, so no single record can say which)"),
    "slicePrbQuota": ("any wire encoder at all: AIC_SliceSLATarget_1.0.0 is "
                      "not one of this deployment's Campaign 5 families"),
}

#: What the predictor assumes about a cell and a UE when the request's
#: condition says nothing.  Both are the deployed 24-PRB carrier's own numbers
#: (``docs/handoff``), and both are corrected by ``calibrate`` after the first
#: observed trial -- an estimate is allowed to start wrong, it is not allowed
#: to stay wrong.
DEFAULT_CELL_CAPACITY_MBPS = 5.0
DEFAULT_OFFERED_LOAD_MBPS = 4.0
DEFAULT_PRB_TOTAL = 24.0


class ClarificationNeeded(LiveConsoleError):
    """The sitting cannot start until the operator answers these questions.

    Not a failure: contract v2 section 2.2's clarification loop.  The intake
    checklist runs before any model call, and the Target agent may itself ask
    for something the checklist could not know it needed; either way the caller
    (the Cockpit's question form, ``--answers``, or the terminal) answers,
    calls :meth:`AgentRequest.with_answers` and builds again.  After
    :data:`MAX_CLARIFICATION_ROUNDS` rounds the sitting refuses to start with
    the open questions named, rather than guessing at a bound nobody signed.
    """

    def __init__(self, questions: Sequence[Mapping[str, Any]], *,
                 round_index: int = 0, refused: bool = False) -> None:
        self.questions = [dict(item) for item in questions]
        self.round = int(round_index)
        self.refused = bool(refused)
        head = ("this sitting will not start: after "
                f"{MAX_CLARIFICATION_ROUNDS} rounds these are still unanswered"
                if refused else
                "the sitting needs the operator to answer before it can start")
        super().__init__(head + ":\n  " + "\n  ".join(
            f"[{item.get('intentId', '?')}] {item.get('field', '?')}: "
            f"{item.get('question', '')}" for item in self.questions))


def _axis_kinds(stated: Any) -> Tuple[str, ...]:
    """The axis kinds a sitting exposes, in the declaration's own order.

    ``all`` is the whole space in one word.  An unknown kind is refused by
    name rather than ignored, and steering cannot be dropped: an intent whose
    UE has no steering axis is an intent nothing can act on, which is the one
    thing ``compose_joint`` will not compose.
    """
    if isinstance(stated, str):
        stated = [stated]
    named = [str(item).strip() for item in (stated or ()) if str(item).strip()]
    if not named:
        named = list(DEFAULT_AXIS_KINDS)
    if any(item == ALL_AXES for item in named):
        return tuple(AXIS_KINDS)
    unknown = [item for item in named if item not in AXIS_KINDS]
    if unknown:
        raise LiveConsoleError(
            "unknown action-axis kind" + ("s " if len(unknown) > 1 else " ")
            + ", ".join(repr(item) for item in unknown) + "; --axes takes "
            + ", ".join(AXIS_KINDS) + " or the word " + repr(ALL_AXES))
    if MANDATORY_AXIS_KIND not in named:
        raise LiveConsoleError(
            f"--axes cannot drop {MANDATORY_AXIS_KIND}: every intent UE needs "
            "its steering action declared, so a sitting without it has an "
            "intent no configuration can act on")
    return tuple(kind for kind in AXIS_KINDS if kind in set(named))


# --------------------------------------------------------------------------- #
# what the operator asked for
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AgentRequest:
    """One sitting's whole request, in one record.

    ``sentences`` are the intents in the order typed (``I1``, ``I2``, ... are
    their ids unless a sentence is prefixed ``<id>:``); ``intents`` is the same
    thing already structured, as the Cockpit's intent-set widget holds it
    (contract 2.1 rows).  Either may be used, and both may be used together.

    ``axes`` names the axis *kinds* this sitting exposes; the per-kind
    mappings (``caps``, ``pf_weights``, ``mcs_bounds``, ``tx_attenuations``,
    ``slice_quotas``) override one scope's ladder and never decide exposure --
    an exposed kind covers **every** intent UE, advertised cell or named
    S-NSSAI, with :data:`DEFAULT_LADDERS` where the operator stated nothing.
    ``cells`` are the cells a steering axis may take.  Every count -- intents,
    owners, UEs, axes, targets -- is read from the request; nothing here is
    fixed at three.
    """

    sentences: Tuple[str, ...] = ()
    intents: Tuple[Mapping[str, Any], ...] = ()
    method: str = METHOD_THREE_AGENT
    #: Which model carries which role (``target`` / ``control`` /
    #: ``trajectory`` / ``monolith``); a missing or ``None`` role runs the
    #: deterministic rule.  The operator picks these in the Cockpit.
    role_models: Mapping[str, Optional[str]] = field(default_factory=dict)
    budget_trials: int = 16
    deadline_ms: Optional[int] = None
    horizon_ms: Optional[int] = None
    #: Reply 2026-09-14 section 5.  Time zero is input release for every method.
    #: ``formation_deadline_ms`` bounds the wall clock to the **first executable
    #: proposal** -- preparation plus first selection for the prepared methods,
    #: the first direct proposal for the basic monolith -- so the first-proposal
    #: allowance is comparable across methods.  ``decision_deadline_ms`` bounds
    #: each subsequent decision, including tool use and one bounded revision.
    #: Both are executor-enforced and never shown to a model; a late answer
    #: cannot authorise a dispatch, and a timeout is an outcome rather than
    #: permission to restart the clock.
    formation_deadline_ms: Optional[int] = None
    decision_deadline_ms: Optional[int] = None
    #: ``exp_metrics.md`` section 3, contract v4 section 1.  ``prepared``: the
    #: live trigger ``t0`` is taken **after** preparation, so ``prepMs`` is
    #: charged separately and B is measured from the trigger.  ``cold-start``:
    #: ``t0`` is the moment the required inputs are released, before the Target
    #: and Control calls, so preparation consumes B -- no clock reset, no
    #: synchronisation wait.  The same B is used in both; only where the clock
    #: starts differs, and the two modes are never mixed in one summary.
    timing_mode: str = TIMING_PREPARED
    #: Predeclared episode boundaries (``exp_metrics.md`` section 1), each
    #: ``{"kind": "intent"|"policy"|"exogenous", "detail": ..., "at": ...}``.
    #: A boundary is a change of original intent, of owner policy, or of an
    #: exogenous condition -- never normal fading, a control change or a new
    #: approval under the same policy, and it never resets a budget.
    boundaries: Tuple[Mapping[str, Any], ...] = ()
    #: Measure the applied configuration once before the search starts and
    #: record it as **trial 0** (``exp_metrics.md`` section 3): a common
    #: initial measurement, ``counted: false``, excluded from the recovery
    #: trial counts, and scored against the targets authorized at ``t0`` -- so
    #: a success it supports is at trial index 0 and elapsed 0 for every
    #: method.  Off unless the campaign asks for it, because an episode that
    #: does not measure one has no trial 0 to exclude.
    initial_measurement: bool = False
    quality_thresholds: Tuple[float, ...] = (0.0, 0.25, 0.5, 1.0)
    bin_delta_ms: int = 5000
    continue_after_relaxed_success: bool = True
    #: Deploy the best attained control at the end of the sitting.  Searching
    #: is not deploying (``exp_metrics.md`` section 2): the best *attained*
    #: target is what some trial reached, the *retained* one is what the
    #: deployment is actually left holding, and only a run whose last trial
    #: already left it applied gets those for free.
    retain_best: bool = True
    #: Owner decision 2026-09-20: every trial is judged on the frozen baseline
    #: C0 -- a judged trial is rolled back (``StopReason.BASELINE_RESET``) and
    #: the next one starts only after the recovery reread confirmed C0.  Off,
    #: a passing trial finalizes live as before.
    reset_each_trial: bool = True
    #: 2026-09-23 결정(시나리오 작성자 §5.1): 기준 관측을 **정식 첫 시행**으로 만든다 --
    #: `N_max` 에 세고, 달성 시각을 유효 판정 완료 시점으로 적고, 결과를 제어가 만든
    #: 해결이 아니라 `initially_T0_satisfied` 로 이름 붙인다.  기존 판은 이 관측을
    #: `counted: false` · elapsed 0 으로 적었고, 그래서 T0_MET 8판의 달성 시각이 0 인데
    #: 실제 창 종료는 t0 에서 23~97초 뒤였다.  기본값은 꺼짐 -- 켜면 그 전 판과 셈이
    #: 달라지므로 새 캠페인에서만 켠다.
    formal_reference_trial: bool = False
    #: 2026-09-23 결정 §3: 유효한 결과가 **이전 역사 최고 이하**면 완전 설정을 유지하고,
    #: 아니면 확인된 직전 설정으로 복원한다.  유지된 설정이 다음 시행의 복구 기준선이
    #: 되고, C0 는 후보를 전개하는 고정 기준으로만 남는다(무조건 롤백 목적지가 아니다).
    #: 기존 런타임은 `reset_each_trial` 로 매 시행을 C0 로 돌렸고, 목표를 충족한 제어
    #: 시행 24건 전부가 되돌려졌다 -- 다른 실험이다.  기본값은 꺼짐.
    retain_on_improvement: bool = False
    #: Which axis kinds this sitting exposes (contract v3 section 3).  The
    #: word ``all`` stands for every kind this build knows.
    axes: Tuple[str, ...] = DEFAULT_AXIS_KINDS
    caps: Mapping[str, Tuple[int, ...]] = field(default_factory=dict)
    pf_weights: Mapping[str, Tuple[float, ...]] = field(default_factory=dict)
    #: Per-cell / per-S-NSSAI ladder overrides, keyed by the scope target the
    #: axis is named for (``{"12345678": ("0..28", "0..16")}``).
    mcs_bounds: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    tx_attenuations: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    slice_quotas: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    #: How many combinations one epoch may freeze.  Over it the composition
    #: refuses by name before anything is written; W1 measured the freeze at
    #: about 2.5 ms and 2.5 kB per candidate, so the default is about ten
    #: seconds and ten megabytes once per sitting.
    max_catalog_cardinality: int = DEFAULT_MAX_CATALOG_CARDINALITY
    cells: Tuple[int, ...] = ()
    #: Free-form, recorded verbatim in the episode: the offered load and the
    #: name of the scenario condition this episode ran under.
    condition: Mapping[str, Any] = field(default_factory=dict)
    block: int = 0
    repetition: int = 0
    episode_id: str = ""
    withdraw_verified: bool = False
    #: Freeze the catalog over the axis *values* that appear in ``C`` (plus the
    #: baselines) rather than over every declared value.  Axes are never
    #: dropped: a cap or weight axis that ``C`` never moves keeps its first
    #: declared value, so the frozen counter set does not depend on ``C``.
    restrict_catalog_to_candidates: bool = True
    max_candidates: Optional[int] = None
    objective_floor_kbps: float = 500.0
    controlled_reserve_kbps: float = 1500.0
    cap_calibration_ref: str = "calibration/live/ue-dl-prb-cap"
    #: The sitting settings of contract v2 (trial count, per-KPI observation
    #: rules, deadline/horizon, the stop rule, retention, the per-role
    #: generation budget, the unselected-function rule, how many candidates to
    #: retain).  **Never** attached to an intent and never shown to a model:
    #: the observation *time and validity* are shown, the trial count and the
    #: deadline are not.  Anything not stated here falls back to the matching
    #: top-level field of this request, and the observation rules fall back to
    #: the contract's own stated defaults per KPI kind.
    settings: Mapping[str, Any] = field(default_factory=dict)
    #: What the operator answered when the intake asked (contract v2 2.2):
    #: ``{"I2": {"steps": 2, "bound": 1.0}, "sitting": {"trialsK": 8}}``.
    answers: Mapping[str, Any] = field(default_factory=dict)
    #: How many times the operator has already been asked.
    clarification_round: int = 0

    def __post_init__(self) -> None:
        sentences = tuple(str(item).strip() for item in self.sentences if str(item).strip())
        object.__setattr__(self, "sentences", sentences)
        object.__setattr__(self, "intents", tuple(dict(item) for item in self.intents))
        if not sentences and not self.intents:
            raise LiveConsoleError("the Agent needs at least one intent")
        if (self.deadline_ms is not None and self.horizon_ms is not None
                and int(self.horizon_ms) < int(self.deadline_ms)):
            # exp_metrics.md section 1: H >= B.  A horizon shorter than the
            # deadline would ask the sitting to stop watching the service while
            # the search it is meant to cover is still allowed to run.
            raise LiveConsoleError(
                f"the service horizon H ({self.horizon_ms} ms) is shorter than the "
                f"search deadline B ({self.deadline_ms} ms); H covers the whole "
                "sitting and everything after it, so it is never the smaller of the two")
        method = str(self.method or "").strip() or METHOD_THREE_AGENT
        if method not in METHODS:
            raise LiveConsoleError(
                f"unknown coordination method {method!r}; the methods are "
                + ", ".join(METHODS))
        object.__setattr__(self, "method", method)
        timing_mode = str(self.timing_mode or "").strip() or TIMING_PREPARED
        if timing_mode not in TIMING_MODES:
            raise LiveConsoleError(
                f"unknown timing mode {timing_mode!r}; the modes are "
                + ", ".join(TIMING_MODES))
        object.__setattr__(self, "timing_mode", timing_mode)
        object.__setattr__(self, "boundaries",
                           tuple(_boundary_record(item) for item in self.boundaries))
        object.__setattr__(self, "initial_measurement", bool(self.initial_measurement))
        models = RoleModels.from_mapping(dict(self.role_models))
        if method == METHOD_DETERMINISTIC:
            models = RoleModels(method=method)
        else:
            models = replace(models, method=method)
        object.__setattr__(self, "role_models", models.to_record())
        object.__setattr__(self, "caps", {str(k): tuple(int(v) for v in vs)
                                          for k, vs in dict(self.caps).items()})
        object.__setattr__(self, "pf_weights", {str(k): tuple(float(v) for v in vs)
                                                for k, vs in dict(self.pf_weights).items()})
        for name in ("mcs_bounds", "tx_attenuations", "slice_quotas"):
            object.__setattr__(self, name, {
                str(k): tuple(str(v) for v in vs)
                for k, vs in dict(getattr(self, name)).items()})
        object.__setattr__(self, "axes", _axis_kinds(self.axes))
        if int(self.max_catalog_cardinality) <= 0:
            raise LiveConsoleError(
                "the catalog ceiling is a positive number of candidates; "
                "--max-catalog states how many one epoch may freeze")
        object.__setattr__(self, "max_catalog_cardinality",
                           int(self.max_catalog_cardinality))
        object.__setattr__(self, "cells", tuple(int(item) for item in self.cells))
        object.__setattr__(self, "quality_thresholds",
                           tuple(float(item) for item in self.quality_thresholds))
        object.__setattr__(self, "condition", dict(self.condition))
        object.__setattr__(self, "settings", dict(self.settings or {}))
        object.__setattr__(self, "answers", {
            str(key): dict(value or {}) for key, value in dict(self.answers or {}).items()})
        object.__setattr__(self, "clarification_round", int(self.clarification_round or 0))
        rule = str(dict(self.settings).get("unselectedFunctionRule", RULE_BASELINE)
                   or RULE_BASELINE)
        if rule not in UNSELECTED_FUNCTION_RULES:
            raise LiveConsoleError(
                f"unknown unselected-function rule {rule!r}; one of "
                + ", ".join(UNSELECTED_FUNCTION_RULES))
        if int(self.budget_trials) <= 0:
            raise LiveConsoleError("the search budget must be a positive number of trials")

    @property
    def models(self) -> RoleModels:
        return RoleModels.from_mapping({**dict(self.role_models), "method": self.method})

    # -- the sitting settings (contract v2 section 2.2) --------------------- #

    def sitting_settings(self, kpis: Sequence[str] = (),
                         observation_defaults: Optional[Mapping[str, Any]] = None,
                         ) -> Dict[str, Any]:
        """Every setting the intake checklist asks about, in one mapping.

        The top-level request fields are the operator's answers to most of
        them, so a request that was valid before contract v2 is a complete
        sitting now too.  A KPI kind the operator stated no rule for takes
        ``observation_defaults`` -- which the executor derives from the
        deployment's own frozen hold, so an unconfigured sitting observes
        exactly what it observed before those rules existed -- and, failing
        that, the contract's own stated defaults (section 6).  A kind neither
        stated nor defaulted stays missing and is asked about.
        """
        stated = dict(self.settings)
        settings: Dict[str, Any] = {
            "trialsK": int(stated.get("trialsK", self.budget_trials)),
            "deadlineMs": stated.get("deadlineMs", self.deadline_ms),
            "horizonMs": stated.get("horizonMs", self.horizon_ms),
            "formationDeadlineMs": stated.get("formationDeadlineMs",
                                              self.formation_deadline_ms),
            "decisionDeadlineMs": stated.get("decisionDeadlineMs",
                                             self.decision_deadline_ms),
            "stopAfterRelaxedSuccess": bool(stated.get(
                "stopAfterRelaxedSuccess", not self.continue_after_relaxed_success)),
            "retention": stated.get(
                "retention", "bestAttained" if self.retain_best else "none"),
            # The frozen catalog stays the whole executable universe -- any
            # combination remains legal and reachable.  ``C`` is the smaller
            # thing: the working set the Trajectory role reads every round.
            # Its default is **four times the trial budget** (owner, 2026-09-08):
            # a search of K trials can touch at most K of C, so 4K leaves real
            # alternatives after every failure while keeping one decision's
            # prompt near 17k tokens instead of the 270k a whole 1024-candidate
            # product would cost.  ``--retain`` overrides; 0 means no ceiling.
            #
            # That is a per-trial scaling policy, NOT a second answer to the
            # fixed cap the integrated reply of 2026-09-14 section 4 sets ("at
            # most 12 configurations including baseline", which is ``retain=11``
            # because ``retain`` counts the candidates besides C0).  It predates
            # the drop and governs only a sitting nobody gave ``--retain`` --
            # which no board the paper is measured on is: the OTA runner
            # (``experiment_results/ota-20260911/atomic_formal_run_guarded.py``)
            # and ``tools/campaign5/run_agent_experiments.py`` both state it.
            # Deliberately left at 4: at the OTA budget K=4 it would build 17
            # configurations, so an unstated ``retain`` is visibly not the
            # drop's board rather than a near-miss of it.  Counted, not read off
            # this constant, in ``tests/test_agent_sitting.py``.
            "retain": (int(stated["retain"]) if str(stated.get("retain", "")).strip() not in ("", "None")
                       else (int(self.max_candidates) if self.max_candidates
                             else DEFAULT_RETAIN_PER_TRIAL * int(
                                 stated.get("trialsK", self.budget_trials) or self.budget_trials))),
            "unselectedFunctionRule": str(stated.get("unselectedFunctionRule",
                                                     RULE_BASELINE)),
            "generation": {str(role): dict(value or {}) for role, value
                           in dict(stated.get("generation") or {}).items()},
            "latencyCalibrationPath": stated.get("latencyCalibrationPath"),
            "requireOwner": bool(stated.get("requireOwner", False)),
        }
        rules = {str(kind): dict(rule) if isinstance(rule, Mapping) else rule
                 for kind, rule in dict(stated.get("observation") or {}).items()}
        fallback = {str(kind): dict(rule) for kind, rule
                    in dict(observation_defaults or {}).items()}
        for kind in kpis:
            if kind in rules:
                continue
            if str(kind) in fallback:
                rules[str(kind)] = dict(fallback[str(kind)])
                continue
            default = DEFAULT_OBSERVATION_RULES.get(str(kind))
            if default is not None:
                rules[str(kind)] = default.to_record()
        settings["observation"] = rules
        return settings

    def observation_rules(self, kpis: Sequence[str] = (),
                          observation_defaults: Optional[Mapping[str, Any]] = None,
                          ) -> ObservationRules:
        return ObservationRules.from_record(
            self.sitting_settings(kpis, observation_defaults).get("observation") or {})

    def with_answers(self, answers: Mapping[str, Any]) -> "AgentRequest":
        """The same request with the operator's answers merged in, one round on.

        Answers accumulate: a second round adds to what the first one settled,
        so the executor never asks twice for something already given.
        """
        merged = {key: dict(value) for key, value in dict(self.answers).items()}
        for key, value in dict(answers or {}).items():
            merged.setdefault(str(key), {}).update(dict(value or {}))
        return replace(self, answers=merged,
                       clarification_round=int(self.clarification_round) + 1)

    def to_record(self) -> Dict[str, Any]:
        return {
            "sentences": list(self.sentences),
            "intents": [dict(item) for item in self.intents],
            "method": self.method,
            "roleModels": dict(self.role_models),
            "budgetTrials": int(self.budget_trials),
            "deadlineMs": self.deadline_ms,
            "horizonMs": self.horizon_ms,
            "timingMode": self.timing_mode,
            "boundaries": [dict(item) for item in self.boundaries],
            "initialMeasurement": bool(self.initial_measurement),
            "qualityThresholds": list(self.quality_thresholds),
            "binDeltaMs": int(self.bin_delta_ms),
            "continueAfterRelaxedSuccess": bool(self.continue_after_relaxed_success),
            "retainBest": bool(self.retain_best),
            "caps": {k: list(v) for k, v in self.caps.items()},
            "pfWeights": {k: list(v) for k, v in self.pf_weights.items()},
            "cells": list(self.cells),
            "condition": dict(self.condition),
            "block": int(self.block), "repetition": int(self.repetition),
            "withdrawVerified": bool(self.withdraw_verified),
            "restrictCatalogToCandidates": bool(self.restrict_catalog_to_candidates),
            "maxCandidates": (None if self.max_candidates is None
                              else int(self.max_candidates)),
            "objectiveFloorKbps": float(self.objective_floor_kbps),
            "controlledReserveKbps": float(self.controlled_reserve_kbps),
            "capCalibrationRef": self.cap_calibration_ref,
            "settings": dict(self.settings),
            "answers": {k: dict(v) for k, v in self.answers.items()},
            "clarificationRound": int(self.clarification_round),
        }


def _boundary_record(item: Mapping[str, Any], *, at: str = "") -> Dict[str, Any]:
    """One declared episode boundary, checked against the three stated kinds.

    ``exp_metrics.md`` section 1 predeclares boundaries from original-intent,
    owner-policy or exogenous-condition changes.  Anything else -- fading, a
    control change, a new approval under the same policy -- is **not** a
    boundary, so a kind this does not know is refused by name rather than
    recorded as a fourth kind that would then be allowed to reset a budget.
    """
    record = dict(item or {})
    kind = str(record.get("kind", "")).strip()
    if kind not in BOUNDARY_KINDS:
        raise LiveConsoleError(
            f"unknown episode-boundary kind {kind!r}; a boundary is one of "
            + ", ".join(BOUNDARY_KINDS)
            + ".  Normal fading, a control change and a new approval under the "
              "same policy are not boundaries and never reset a budget")
    return {"at": str(record.get("at", "") or at),
            "kind": kind,
            "detail": str(record.get("detail", "")),
            "invalidatesTC": kind in TC_INVALIDATING_BOUNDARIES}


# --------------------------------------------------------------------------- #
# the intents, as typed or as structured
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IntentRow:
    """One intent, in both vocabularies it has to live in.

    ``intent`` is the owner's requirement (contract 2.1) -- what ``T`` is built
    from and what the KPI vector is judged against.  ``family`` and
    ``named_cell`` are what the *joint Kernel case* folds: which objective
    family states this intent as an O-RAN objective, and which serving cell its
    predicate names.  A sentence that names no cell keeps the UE where it is.
    """

    intent: Intent
    family: str
    named_cell: Optional[int]

    @property
    def ue_id(self) -> str:
        return self.intent.ue_id

    @property
    def cell_scope(self) -> Optional[str]:
        """이 요구가 UE 가 아니라 **셀**을 향하면 그 NCI, 아니면 None.

        owner 가 셀 사업자인 요구(`cellGoodputMbps`)는 `ueId` 가 없고 scope 가
        `cell@<nci>` 다.  아래의 UE 목록들은 전부 이 행을 건너뛰어야 한다 -- 빈 UE 를
        섞으면 축 조립과 신원 해석이 존재하지 않는 UE 를 찾는다.  그래도 이 행의
        `intent` 는 계약에 그대로 들어가고 T 와 KPI 벡터에서 판정된다.
        """
        scope = str(self.intent.requirement.scope or "")
        kind, _, target = scope.partition("@")
        return target or None if kind == "cell" else None


def _split_id(sentence: str, position: int) -> Tuple[str, str]:
    """``"I2: Steer ..."`` names its id; otherwise ``I<position>``."""
    head, separator, rest = sentence.partition(":")
    if separator and head.strip() and " " not in head.strip() and len(head.strip()) <= 12:
        return head.strip(), rest.strip()
    return f"I{position}", sentence.strip()


def _named_cell(sentence: str, family: str) -> Optional[int]:
    """The cell the sentence names, read by the same grammar the case uses."""
    spec = FAMILY_UTTERANCES.get(family)
    if spec is None:
        return None
    registry = {family: IntentGrammarEntry(
        objective_family=family, keywords=tuple(spec["keywords"]),
        measurement_ref="measurement/agent-selection-only",
        default_operator=ComparisonOperator.EQUAL, default_unit="nci")}
    try:
        _family, _scope, constraints, _unsupported = parse_utterance(
            sentence, objective_registry=registry, source_record="agent-selection")
    except IntentParseError:
        return None
    if not constraints:
        return None
    return int(constraints[0].bound.value)


def _family_of(sentence: str, families: Sequence[str]) -> Optional[str]:
    try:
        return family_for_utterance(sentence, families=families)
    except LiveConsoleError:
        return None


def _row_from_sentence(sentence: str, position: int,
                       families: Sequence[str]) -> IntentRow:
    """One typed sentence, read by both grammars, in that order.

    The requirement grammar of :func:`assurance.coordination.intent_from_sentence`
    reads a Mbps threshold and its authorized relaxation limit.  A sentence it
    cannot read is offered to the frozen objective grammar this console has
    always used, whose serving-cell sentence becomes a ``servingCell ==``
    requirement -- so both the new intent set and every sentence an operator
    already knows compose into the same sitting.
    """
    intent_id, body = _split_id(sentence, position)
    family = _family_of(body, families)
    try:
        intent = intent_from_sentence(sentence, intent_id)
    except ValueError as requirement_error:
        if family is None:
            raise LiveConsoleError(
                f"intent {intent_id} could not be read ({sentence!r}): "
                f"{requirement_error}. Write a threshold ('at least 3.0 Mbps "
                "downlink, relaxable to 2.0') or one of this console's objective "
                "sentences, and name the UE with ueId=<amfUeNgapId>.") from None
        scope = scope_selector(body)
        ue = str(scope.get("ueId") or "")
        if not ue or not ue.isdigit():
            raise LiveConsoleError(
                f"intent {intent_id} names no UE ({sentence!r}); with several intents "
                "in one sitting each sentence says which UE it is about with "
                "ueId=<amfUeNgapId>") from None
        cell = _named_cell(body, family)
        if cell is None:
            raise LiveConsoleError(
                f"intent {intent_id} names no cell ({sentence!r}); the frozen grammar "
                "reads the first number as the serving cell the intent is about") from None
        requirement = Requirement(req_id=f"{intent_id}.r1", kpi=SERVING_CELL_KPI,
                                  scope=f"ue@{ue}", op="==", value=str(int(cell)),
                                  unit="nci")
        intent = Intent(intent_id=intent_id, owner=intent_id, ue_id=ue,
                        requirement=requirement, priority=1, sentence=sentence)
        return IntentRow(intent=intent, family=family, named_cell=int(cell))
    if intent.requirement.kpi == SERVING_CELL_KPI:
        try:
            named: Optional[int] = int(str(intent.requirement.value))
        except ValueError:
            named = _named_cell(body, family) if family else None
    else:
        # A throughput sentence names a threshold, not a cell.  The objective
        # grammar reads "the first number" and would happily read 3 out of
        # "3.0 Mbps", so a cell is taken from it only when it looks like an NCI
        # and is not the UE's own id.
        named = _named_cell(body, family) if family else None
        if named is not None and (len(str(named)) < 4 or str(named) == intent.ue_id):
            named = None
    return IntentRow(intent=intent, family=family or DEFAULT_JOINT_FAMILY,
                     named_cell=named)


#: `LiveTiming` 의 기본값을 운영자가 덮을 수 있는 자리.  환경에서 온 값만 바꾸므로,
#: 아무것도 주지 않으면 동작이 예전과 같다.
_LIVE_TIMING_ENV = {
    # 정책의 `actionDeadlineMs` / `rollbackTimeoutMs` 가 되는 값.  프로듀서는 이 시간이
    # 지나면 액션을 실패로 본다 (`oran/mocks/nearrt/producer.py:740` 이 같은 규칙이다).
    #
    # **기본 10초는 실측 확증 시간보다 짧다.**  2026-09-23 실기 측정: 조종 정책 하나를
    # 혼자 쏘았을 때 KPM 이 목표 셀을 보기까지 6.0초, 프로듀서 readback 이 VERIFIED 가
    # 되기까지 **7.8초**.  여유가 2.2초뿐이고, 판은 조종을 혼자 쓰지 않는다 -- 9축을 한
    # 트랜잭션으로 쓰므로 앞의 축들이 그 여유를 먹는다.  그래서 판마다 조종 시행이
    # `PARTIAL_APPLY 9/9 acknowledged (readback confirmed only 2)` 로 판정되고, 그 롤백이
    # 절반쯤 넘어간 UE 를 붕 띄웠다.  한계값이 제 분포보다 낮은 전형적인 결함이다.
    'AIC_ENFORCED_TIMEOUT_MS': 'enforced_timeout_ms',
    'AIC_FRESHNESS_BOUND_MS': 'freshness_bound_ms',
    'AIC_HOLD_MS': 'hold_ms',
    'AIC_CASE_DEADLINE_MS': 'case_deadline_ms',
}


def _live_timing_overrides() -> Dict[str, int]:
    """환경이 준 `LiveTiming` 덮어쓰기.  잘못된 값은 **조용히 무시하지 않고** 거절한다."""
    out: Dict[str, int] = {}
    for name, field in _LIVE_TIMING_ENV.items():
        raw = os.environ.get(name)
        if raw is None or not str(raw).strip():
            continue
        try:
            value = int(str(raw).strip())
        except ValueError:
            raise LiveConsoleError(
                f"{name} must be a whole number of milliseconds, got {raw!r}")
        if value <= 0:
            raise LiveConsoleError(f"{name} must be positive, got {value}")
        out[field] = value
    return out


def _first_ue_row(rows: Sequence[IntentRow]) -> IntentRow:
    """UE 를 향하는 **첫** 행.

    예전에는 `rows[0]` 을 그냥 썼다.  코퍼스의 첫 인텐트가 owner 가 셀 사업자인 요구
    (scope `cell@<nci>`) 이면 그 행에는 `ue_id` 가 없고, `identities[rows[0].ue_id]` 가
    `KeyError: ''` 로 판을 죽인다 (2026-09-23 시도 363).  **행의 순서는 오너가 코퍼스를
    어떻게 적었는지일 뿐이므로 거기에 신원을 걸면 안 된다.**
    """
    for row in rows:
        if row.ue_id:
            return row
    raise LiveConsoleError(
        "this sitting has no intent addressed to a UE; a joint composition needs at "
        "least one, because the supplementary axes are built around a UE identity")


def _row_from_record(record: Mapping[str, Any], position: int,
                     families: Sequence[str]) -> IntentRow:
    """One structured row, as the Cockpit's intent set holds it."""
    record = dict(record)
    if not record.get("intentId"):
        record["intentId"] = f"I{position}"
    intent = Intent.from_record(record)
    scope = str(intent.requirement.scope or "")
    if not intent.ue_id and not scope.startswith("cell@"):
        raise LiveConsoleError(
            f"intent {intent.intent_id} names no ueId; a joint sitting addresses "
            "each intent to one UE, unless its requirement is scoped to a cell "
            "(scope 'cell@<nci>', e.g. the operator's cellGoodputMbps)")
    family = str(record.get("family") or "").strip()
    if family and family not in families:
        raise LiveConsoleError(
            f"intent {intent.intent_id} names objective family {family!r}, which this "
            "deployment cannot run live; capable: " + ", ".join(families))
    cell = record.get("targetCell", record.get("namedCell"))
    named: Optional[int]
    if cell not in (None, ""):
        named = int(cell)
    elif intent.requirement.kpi == SERVING_CELL_KPI:
        named = int(str(intent.requirement.value))
    else:
        named = None
    return IntentRow(intent=intent, family=family or DEFAULT_JOINT_FAMILY, named_cell=named)


def parse_agent_intents(request: AgentRequest, *,
                        families: Sequence[str] = ()) -> Tuple[IntentRow, ...]:
    """Every intent of the set, in the order the operator gave them."""
    families = tuple(families) or live_capable_families()
    rows: List[IntentRow] = []
    for position, sentence in enumerate(request.sentences, 1):
        rows.append(_row_from_sentence(sentence, position, families))
    for offset, record in enumerate(request.intents, len(rows) + 1):
        rows.append(_row_from_record(record, offset, families))
    ids = [row.intent.intent_id for row in rows]
    if len(set(ids)) != len(ids):
        raise LiveConsoleError(f"intent ids must be unique: {ids}")
    return tuple(rows)


# --------------------------------------------------------------------------- #
# the action space
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _AxisSet:
    """Every axis this sitting declares, kept by kind rather than by position.

    Six kinds do not fit in a tuple anybody can read, and ``compose_joint``
    takes them by keyword anyway, so the set carries itself to the composer.
    """

    steering: Tuple[SteeringAxisSpec, ...] = ()
    caps: Tuple[CapAxisSpec, ...] = ()
    weights: Tuple[PriorityAxisSpec, ...] = ()
    mcs: Tuple[McsBoundsAxisSpec, ...] = ()
    attenuation: Tuple[TxAttenuationAxisSpec, ...] = ()
    quota: Tuple[SlicePrbQuotaAxisSpec, ...] = ()

    @property
    def declared(self) -> Tuple[Any, ...]:
        """Every spec, in the order the refusal message factors them."""
        return (*self.steering, *self.caps, *self.weights, *self.mcs,
                *self.attenuation, *self.quota)

    @property
    def supplementary(self) -> Tuple[Any, ...]:
        """Everything that is not steering: one loop composes them all."""
        return (*self.caps, *self.weights, *self.mcs, *self.attenuation,
                *self.quota)

    def compose_kwargs(self) -> Dict[str, Any]:
        return {"steering_axes": self.steering, "cap_axes": self.caps,
                "priority_axes": self.weights, "mcs_axes": self.mcs,
                "attenuation_axes": self.attenuation,
                "slice_quota_axes": self.quota}


def _ladder(request: AgentRequest, kind: str, scope_target: str) -> Tuple[Any, ...]:
    """This scope's ladder: what the operator stated for it, else the default."""
    stated = {"dlPrbCap": request.caps, "pfWeight": request.pf_weights,
              "dlMcsBounds": request.mcs_bounds,
              "txAttenuationDb": request.tx_attenuations,
              "slicePrbQuota": request.slice_quotas}[kind]
    return tuple(dict(stated).get(str(scope_target)) or DEFAULT_LADDERS[kind])


CELL_BASELINE_MAX_AGE_S = 10.0


def _live_cell_attenuations(read_new_lines: Any, adapter: Any,
                            cells: Sequence[int], topology: Any,
                            max_age_s: Optional[float] = None) -> Dict[int, str]:
    """각 셀이 **지금 있는** 감쇠 — 없으면 그 셀은 빠진다(선언 상수를 쓰게 된다).

    KPM 은 `RAN.Cell.TxAttenuationDb` 를 노드 범위로 싣고, 스코프에 셀 NCI 가 없으므로
    `e2_node` 로 셀을 가른다.  실측(2026-09-18): nb=2816 → 10.0, nb=3584 → 0.0.
    """
    try:
        lines = read_new_lines()
    except Exception:  # noqa: BLE001 - 못 읽으면 선언 상수로 간다
        return {}
    return cell_attenuations_from_lines(lines, adapter, cells, topology, max_age_s=max_age_s)


def _await_cell_attenuations(read_new_lines: Any, adapter: Any, cells: Sequence[int],
                             topology: Any, clock: Any, *, wait_ms: float) -> Dict[int, str]:
    """:func:`_live_cell_attenuations`, polled until every cell has a value or
    ``wait_ms`` passes.  Each read only sees lines that arrived since the last, so the
    values are merged, a later one replacing an earlier one."""
    found: Dict[int, str] = {}
    wanted = {int(cell) for cell in cells}
    limit = float(clock.monotonic_ms()) + float(wait_ms)
    while True:
        # Live only: a record older than 10 s may predate a restore (Codex review 2026-09-25).
        found.update(_live_cell_attenuations(read_new_lines, adapter, cells, topology,
                                             max_age_s=CELL_BASELINE_MAX_AGE_S))
        if wanted <= set(found) or float(clock.monotonic_ms()) >= limit:
            return found
        clock.sleep_ms(1000)


def _axis_specs(rows: Sequence[IntentRow], identities: Mapping[str, LiveUeObservation],
                request: AgentRequest, cells: Sequence[int],
                slices: Sequence[Any] = (),
                cell_baselines: Optional[Mapping[int, str]] = None) -> _AxisSet:
    """The axes this sitting declares, one spec per scoped knob.

    Exposure follows the *kind*, not the flags: an exposed UE kind covers
    every UE this sitting knows about, an exposed cell kind covers every cell
    the deployment advertises, and the slice kind covers the S-NSSAIs the
    deployment names.  A per-scope flag changes that scope's ladder and
    nothing else, so no axis is ever silently absent because nobody named it.
    The one exception is ``txAttenuationDb`` (2026-09-25): once any cell's ladder is
    stated, only the stated cells get the power axis.
    """
    kinds = set(request.axes)
    ue_ids = list(dict.fromkeys(
        [row.ue_id for row in rows if row.ue_id]
        + list(request.caps) + list(request.pf_weights)))
    steering = tuple(
        SteeringAxisSpec(ue, int(identities[ue].serving_nci), tuple(cells))
        for ue in dict.fromkeys(row.ue_id for row in rows if row.ue_id))
    caps = tuple(
        CapAxisSpec(ue, int(identities[ue].serving_nci),
                    tuple(int(item) for item in _ladder(request, "dlPrbCap", ue)),
                    request.controlled_reserve_kbps, request.cap_calibration_ref)
        for ue in ue_ids) if "dlPrbCap" in kinds else ()
    weights = tuple(
        PriorityAxisSpec(ue, int(identities[ue].serving_nci),
                         tuple(float(item) for item in _ladder(request, "pfWeight", ue)))
        for ue in ue_ids) if "pfWeight" in kinds else ()
    cell_ids = list(dict.fromkeys(int(cell) for cell in cells))
    mcs = tuple(McsBoundsAxisSpec(cell, _ladder(request, "dlMcsBounds", str(cell)))
                for cell in cell_ids) if "dlMcsBounds" in kinds else ()
    # 2026-09-18: 기준값은 **그 셀이 지금 있는 값**이다(`cell_baselines`).  못 읽었으면
    # `None` 이 가고 선언 상수를 쓴다 -- 조립을 멈추지 않는다.
    # 2026-09-25 (v4.7 셀 전력): 사다리를 **한 셀이라도** 적었으면 적은 셀에만 축을 준다.
    # 적지 않은 셀은 기본 0/6/12 dB 를 받던 것이 gnb1(2 dB 창에서만 사는 셀)을 위험에
    # 빠뜨린다.  아무것도 적지 않았으면 종전대로 모든 셀이 기본 사다리를 받는다.
    stated_cells = {str(cell) for cell in dict(request.tx_attenuations or {})}
    attenuation = tuple(
        TxAttenuationAxisSpec(cell, _ladder(request, "txAttenuationDb", str(cell)),
                              observed_baseline=(cell_baselines or {}).get(int(cell)))
        for cell in cell_ids
        if not stated_cells or str(cell) in stated_cells) if "txAttenuationDb" in kinds else ()
    quota: Tuple[SlicePrbQuotaAxisSpec, ...] = ()
    if "slicePrbQuota" in kinds:
        named = list(dict.fromkeys(
            [str(item) for item in slices] + list(request.slice_quotas)))
        if not named:
            # Never a silent absence: the kind was asked for and this
            # deployment names no S-NSSAI to scope it to.
            raise LiveConsoleError(
                "the slicePrbQuota axis was exposed but this deployment names "
                "no S-NSSAI to scope it to; name one with --slice-axis "
                "<sst>:<dedicated:min:max>,... or drop the kind with --axes")
        quota = tuple(
            SlicePrbQuotaAxisSpec(int(sst), quotas=_ladder(request, "slicePrbQuota", sst))
            for sst in named)
    return _AxisSet(steering=steering, caps=caps, weights=weights, mcs=mcs,
                    attenuation=attenuation, quota=quota)


#: The actuation-adapter key one supplementary axis is carried on.  The two
#: UE knobs keep the keys this executor and the emulator have always used;
#: the three new kinds take the adapter their own declaration names.
_ADAPTER_KEYS: Mapping[str, str] = {
    "dlPrbCap": "r1-cap", "pfWeight": "r1-pf",
    **{SUPPLEMENTARY_ACTIONS[action_id].axis: SUPPLEMENTARY_ACTIONS[action_id].adapter
       for action_id in (MCS_ACTION_ID, ATTENUATION_ACTION_ID, SLICE_QUOTA_ACTION_ID)},
}


def _adapter_key(spec: Any) -> str:
    """``dlPrbCap@131`` -> ``r1-cap@131``; ``dlMcsBounds@12345678`` -> ``r1-mcs@12345678``."""
    return f"{_ADAPTER_KEYS[axis_kind(spec.axis)]}@{_scope_target(spec)}"


def _refuse_uncarried_axes(axes: _AxisSet, *, deployment: LiveDeployment,
                           overrides: Mapping[str, Any],
                           exposed: Sequence[str]) -> None:
    """Refuse, **by name**, every exposed axis this deployment cannot carry.

    Two separate facts, and the message says which one it is: the profile's
    action producer serves no policy type for the axis, or the producer serves
    it but nothing on this wire reads the configuration back
    (:data:`LIVE_UNCARRIED_AXES`).  Either way the axis is refused before a
    contract is composed, never quietly dropped and never given a mock: a
    control whose effect cannot be corroborated would be a write nobody can
    verify, which is exactly what the readback exists to prevent.

    An injected adapter answers both questions on its own, which is what makes
    the hardware-free runtime the same composition with different ports.
    """
    producer = deployment.action_producer
    for spec in axes.supplementary:
        key = _adapter_key(spec)
        if key in overrides:
            continue
        kind = axis_kind(spec.axis)
        declared = SUPPLEMENTARY_ACTIONS[SUPPLEMENTARY_BY_KIND[kind]]
        remaining = ", ".join(item for item in exposed if item != kind)
        way_out = (f" Drop the kind with --axes {remaining}, or compose the "
                   f"sitting over an actuation adapter for {key}.")
        if producer is None or producer.for_action(declared.action_id) is None:
            raise LiveConsoleError(
                f"the {spec.axis} axis is exposed but this profile names no "
                f"action producer serving {declared.policy_type_id}."
                + way_out)
        reason = LIVE_UNCARRIED_AXES.get(kind)
        if reason:
            raise LiveConsoleError(
                f"the {spec.axis} axis is composed by the joint contract and "
                f"{declared.policy_type_id} is served, but this deployment "
                f"cannot verify it: {reason} is not wired in this build."
                + way_out)


def _ceiling_refusal(exc: CatalogTooLargeError) -> str:
    """The composition's own refusal, with both ways forward named.

    The message already names each axis kind and how much it multiplies by;
    what it cannot know is that the ceiling itself is an operator setting.
    W1 measured the freeze at about 2.5 ms and 2.5 kB per candidate, so the
    number is a wall-clock and memory choice, not a taste.
    """
    # 2026-09-19: the freeze is a domain now and no longer costs per candidate,
    # so the ceiling is only a guard against a mistyped ladder.
    return (str(exc) + ", or raise the ceiling with --max-catalog <n> (the "
            "epoch freezes the axes' allowed values, not an enumeration, so the "
            "ceiling guards against a mistyped ladder, not freeze cost)")


def _intent_specs(rows: Sequence[IntentRow], home_of: Callable[[str], int],
                  served_cells: Sequence[int]) -> Tuple[IntentSpec, ...]:
    """The intents as the joint contract folds them.

    An intent that named a cell pins it.  An intent whose condition is a KPI
    names none, so its Kernel-side cell predicate is membership in the cells
    the deployment advertises -- otherwise the Kernel would judge the very
    control that met the KPI a non-success and roll it back, and the winning
    configuration could never stay applied.
    """
    specs: List[IntentSpec] = []
    for row in rows:
        if not row.ue_id:
            continue          # 셀 사업자 요구는 UE 를 향한 O-RAN objective 가 아니다
        home = int(home_of(row.ue_id))
        named = int(row.named_cell) if row.named_cell is not None else home
        kpi_kind = row.intent.requirement.kpi != SERVING_CELL_KPI
        specs.append(IntentSpec(
            intent_id=row.intent.intent_id, family=row.family, ue_id=row.ue_id,
            home_nci=home, target_nci=named, sentence=row.intent.sentence,
            kind=INTENT_KIND_KPI if kpi_kind else INTENT_KIND_SERVING_CELL,
            served_cells=tuple(int(cell) for cell in served_cells)))
    return tuple(specs)


def _action_space(specs: Sequence[Any]) -> Dict[str, Tuple[str, ...]]:
    return {spec.axis: tuple(spec.values) for spec in specs}


def _baselines(specs: Sequence[Any]) -> Dict[str, str]:
    return {spec.axis: str(spec.baseline) for spec in specs}


def _registered_functions(specs: Sequence[Any]) -> List[Dict[str, Any]]:
    """What the deployment can actually move, per scoped axis (the v1 view).

    Keyed on the spec's own ``scope`` -- ``ue@<id>``, ``cell@<nci>`` or
    ``slice@<sst>`` -- because half the axes are no longer about a UE.
    """
    return [{"actionId": spec.action_id, "axis": spec.axis,
             "scope": str(spec.scope), "baseline": str(spec.baseline),
             "values": list(spec.values)} for spec in specs]


def _scope_target(spec: Any) -> str:
    """What one spec's scope is named for: the UE, the cell or the S-NSSAI."""
    return str(spec.scope).split("@", 1)[1]


def _function_catalog(specs: Sequence[Any], prb_total: Optional[float] = None) -> FunctionCatalog:
    """The exposed functions, as contract v2 section 3.1 states them.

    One :class:`FunctionSpec` per (axis kind, value list): the scopes a
    function serves with the *same* policy values share it, exactly as the
    contract's example shares one ``steer`` across two UEs, and a scope whose
    values differ (its own cap ladder) gets a function of its own rather than
    being offered somebody else's values.  The naming is
    :data:`~assurance.coordination.FUNCTION_NAMING` -- the deployment's own
    function ids, one per axis kind, cell- and slice-scoped ones included.
    """
    groups: Dict[Tuple[str, Tuple[str, ...]], List[Any]] = {}
    for spec in specs:
        kind = axis_kind(spec.axis)
        if kind not in FUNCTION_NAMING:
            continue
        groups.setdefault((kind, tuple(str(value) for value in spec.values)),
                          []).append(spec)
    per_kind: Dict[str, int] = {}
    for kind, _values in groups:
        per_kind[kind] = per_kind.get(kind, 0) + 1
    rows: List[FunctionSpec] = []
    for (kind, values), members in groups.items():
        naming = FUNCTION_NAMING[kind]
        function_id = str(naming["functionId"])
        if per_kind[kind] > 1:
            function_id += "@" + "-".join(_scope_target(member) for member in members)
        field_name = str(naming["policyField"])
        rows.append(FunctionSpec(
            function_id=function_id, xapp=str(naming["xapp"]),
            action_id=str(members[0].action_id),
            scopes=tuple(str(member.scope) for member in members),
            policy_fields={field_name: PolicyField(
                name=field_name, values=values, unit=str(naming["unit"]),
                baseline=str(members[0].baseline))},
            prerequisites=tuple(naming["prerequisites"]),
            axis=str(naming["axisTemplate"]), axis_field=field_name,
            description=str(naming.get("description", ""))
            + (f" Each cell has {prb_total:g} PRBs."
               if kind == "dlPrbCap" and prb_total else "")))
    return FunctionCatalog(tuple(rows))


def _compatibility(ue_ids: Sequence[str], space: Mapping[str, Sequence[str]]) -> Tuple[str, ...]:
    """The one per-candidate rule the composition policy states for this build."""
    stated: List[str] = []
    for ue in ue_ids:
        if f"dlPrbCap@{ue}" in space and f"pfWeight@{ue}" in space:
            stated.append(f"dlPrbCap@{ue} and pfWeight@{ue} may not both move off "
                          "their baseline in one configuration")
        for kind in ("pfWeight", "dlPrbCap"):
            if f"servingCell@{ue}" in space and f"{kind}@{ue}" in space:
                stated.append(f"servingCell@{ue} and {kind}@{ue} may not both move off "
                              "their baseline in one configuration: the UE's scheduler "
                              "setting stays on the cell it leaves")
    return tuple(stated)


def _compatibility_rules(catalog: FunctionCatalog, ue_ids: Sequence[str],
                         space: Mapping[str, Sequence[str]]) -> CompatibilityRules:
    """The same co-use truth, in the shape the Control agent is given it.

    The cap and the scheduler weight are two ways of asking the scheduler for
    the same thing on one UE, so a candidate may not select both there; a steer
    is applied before a cap on the same UE, because the cap is scoped to the
    cell the UE ends up on.
    """
    caps = [spec.function_id for spec in catalog
            if axis_kind(spec.axis) == "dlPrbCap"]
    weights = [spec.function_id for spec in catalog
               if axis_kind(spec.axis) == "pfWeight"]
    steers = [spec.function_id for spec in catalog
              if axis_kind(spec.axis) == "servingCell"]
    return CompatibilityRules(
        mutually_exclusive=tuple((cap, weight) for cap in caps for weight in weights),
        precedence=tuple((steer, cap) for steer in steers for cap in caps),
        max_changed_entries=MAX_CHANGED_ENTRIES,
        # 2026-09-19 board 20260919T145410: pfWeight@ue1 8.0 settled on 87654321, then
        # a candidate steered ue1 to 12345678 keeping 8.0.  The policy is cell-scoped,
        # so after the handover ue1 ran at 1.0; COMMIT read PARTIAL_APPLY and the
        # rollback could not bring 8.0 back -> lockdown.  A steer and a per-UE
        # scheduler setting on one UE cannot be expressed together.
        exclusive_axes=(("dlPrbCap", "pfWeight"), ("servingCell", "pfWeight"),
                        ("servingCell", "dlPrbCap")),
        notes=_compatibility(ue_ids, space))


def _default_observation_rules(kinds: Sequence[str], hold_ms: int
                               ) -> Dict[str, Dict[str, Any]]:
    """The rules a sitting observes under when the operator states none.

    Not the contract's illustrative 5 s / 30 s: the **deployment's own** frozen
    hold, split in half.  Settling over the first half and measuring the second
    is exactly what this executor did before per-KPI rules existed -- a change
    whose effect needs a moment to appear (a handover's re-attachment, a cap
    taking hold in the scheduler) is measured after it happened rather than
    across it -- so an unconfigured sitting observes precisely what it always
    observed, now said out loud and recorded.  An operator who wants the
    contract's longer window states it (``--observe``).
    """
    hold = max(2, int(hold_ms))
    settle = hold // 2
    window = max(1, hold - settle)
    rows: Dict[str, Dict[str, Any]] = {}
    for kind in dict.fromkeys(str(item) for item in kinds):
        categorical = kind == SERVING_CELL_KPI
        rows[kind] = {"settleMs": settle, "windowMs": window,
                      "statistic": "last" if categorical else "mean",
                      "minCoverage": 0.5,
                      "validityMs": 10000 if categorical else 60000}
    return rows


def intent_deadlines(intents: Sequence[Any]) -> Dict[str, float]:
    """UE -> 그 인텐트가 부른 마감 ms.  예측기와 network_state 가 **같은 값**을 쓴다.

    ``intents`` 는 ``Intent`` 데이터클래스 목록이다 -- ``.get()`` 으로 읽어 판 13 개를
    즉사시킨 적이 있다(2026-09-18 18:0x).
    """
    found: Dict[str, float] = {}
    for intent in intents:
        requirement = getattr(intent, "requirement", None)
        deadline = getattr(requirement, "deadline_ms", None) if requirement is not None else None
        ue = getattr(intent, "ue_id", None)
        if ue and deadline:
            try:
                found[str(ue)] = float(deadline)
            except (TypeError, ValueError):
                pass
    return found


def _predictor_state(request: AgentRequest, identities: Mapping[str, Any],
                     cells: Sequence[Any], baselines: Mapping[str, str],
                     ue_slices: Optional[Mapping[str, str]] = None,
                     ) -> NetworkState:
    """What the joint-effect predictor is told about this deployment.

    Read from the **request's condition** and from what the UEs were observed
    doing, never from the emulator: a predictor that can see the ground truth
    is not a predictor, and the paper's claim is about an agent acting on
    estimates.  Anything the condition does not state takes the deployed
    carrier's own numbers and is corrected by ``calibrate`` after the first
    observed trial.
    """
    condition = dict(request.condition or {})

    def per_key(value: Any, keys: Sequence[str], default: float) -> Dict[str, float]:
        if isinstance(value, Mapping):
            return {str(key): float(dict(value).get(str(key), default)) for key in keys}
        number = default if value is None else float(value)
        return {str(key): number for key in keys}

    cell_ids = [str(cell) for cell in cells]
    ue_ids = [str(ue) for ue in identities]
    capacities = per_key(condition.get("cellCapacityMbps"), cell_ids,
                         DEFAULT_CELL_CAPACITY_MBPS)
    loads = per_key(condition.get("offeredLoadMbps"), ue_ids, DEFAULT_OFFERED_LOAD_MBPS)
    serving = {ue: str(identities[ue].serving_nci) for ue in ue_ids}
    for cell in serving.values():
        capacities.setdefault(cell, DEFAULT_CELL_CAPACITY_MBPS)
    prb_total = float(condition.get("prbTotal", DEFAULT_PRB_TOTAL) or DEFAULT_PRB_TOTAL)
    caps: Dict[str, float] = {}
    weights: Dict[str, float] = {}
    bounds: Dict[str, str] = {}
    attenuation: Dict[str, float] = {}
    quotas: Dict[str, str] = {}
    for axis, value in dict(baselines or {}).items():
        kind, _, target = str(axis).partition("@")
        # The cell- and slice-scoped baselines are the composite strings the
        # predictor's own state carries; only the two UE knobs are numbers.
        if kind == "dlMcsBounds":
            bounds[target] = str(value)
            continue
        if kind == "slicePrbQuota":
            quotas[target] = str(value)
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if kind == "txAttenuationDb":
            attenuation[target] = number
            continue
        if target not in serving:
            continue
        if kind == "dlPrbCap":
            caps[target] = prb_total if number <= 0 else number
        elif kind == "pfWeight":
            weights[target] = number
    return NetworkState(
        cells=capacities, ues=serving, offered_load_mbps=loads,
        pf_weights=weights, caps_prb=caps, prb_total=prb_total,
        mcs_bounds=bounds, tx_attenuation_db=attenuation,
        ue_slices=dict(ue_slices or {}), slice_quota=quotas,
        unselected_function_rule=str(
            request.sitting_settings().get("unselectedFunctionRule", RULE_BASELINE)))


@dataclass
class _SittingAgents(RoleAgents):
    """The roles, with the operator's per-role generation budget on top.

    Precedence, contract v2 section 7: what the operator stated for the role >
    the largest calibrated budget whose p95 fits inside the shortest validity >
    the role's default.  Nothing here is a cutoff: it is what the model is
    *asked* to spend.
    """

    generation: Mapping[str, Any] = field(default_factory=dict)

    def options_for(self, prompt_role: str, model: Optional[str] = None
                    ) -> GenerationOptions:
        stated = dict(self.generation or {}).get(str(prompt_role))
        if stated:
            record = dict(GenerationOptions.for_role(prompt_role).to_record())
            record.update(dict(stated))
            record["source"] = "operator"
            return GenerationOptions.from_record(record)
        return super().options_for(prompt_role, model)


def _incompatible(configuration: Mapping[str, str], ue_ids: Sequence[str],
                  baselines: Mapping[str, str]) -> Optional[str]:
    for ue in ue_ids:
        cap, weight = configuration.get(f"dlPrbCap@{ue}"), configuration.get(f"pfWeight@{ue}")
        if (cap is not None and weight is not None
                and cap != baselines.get(f"dlPrbCap@{ue}")
                and weight != baselines.get(f"pfWeight@{ue}")):
            return f"dlPrbCap@{ue} and pfWeight@{ue} both move off their baseline"
    return None


#: Which ladder field one composite spec keeps its rungs in.
_RUNG_FIELDS: Mapping[str, str] = {
    "dlMcsBounds": "bounds", "txAttenuationDb": "attenuations",
    "slicePrbQuota": "quotas"}


def _restricted_specs(controls: ControlCandidates, axes: _AxisSet) -> _AxisSet:
    """Freeze the catalog over the values ``C`` actually uses.

    Values only: an axis is never dropped, because the frozen counter set --
    and therefore every counter-sample loader a composition root prepared --
    follows the *axes*, not their value lists.  An axis that no candidate
    moves keeps its first declared value and simply never gets tried.
    """
    used: Dict[str, set] = {}
    for candidate in controls.candidates:
        for axis, value in candidate.configuration.items():
            used.setdefault(axis, set()).add(str(value))
    new_steering = tuple(
        replace(spec, cells=tuple(sorted(
            {int(value) for value in used.get(spec.axis, set()) if str(value).isdigit()}
            | {int(spec.baseline_nci)})))
        for spec in axes.steering)
    new_caps = []
    for spec in axes.caps:
        kept = tuple(sorted({int(float(value)) for value in used.get(spec.axis, set())
                             if str(value) != str(spec.baseline)}))
        new_caps.append(replace(spec, caps=kept or spec.caps[:1]))
    new_weights = []
    for spec in axes.weights:
        kept = tuple(sorted({float(value) for value in used.get(spec.axis, set())
                             if str(value) != str(spec.baseline)}))
        new_weights.append(replace(spec, weights=kept or spec.weights[:1]))
    composite: Dict[str, List[Any]] = {}
    for spec in (*axes.mcs, *axes.attenuation, *axes.quota):
        # A composite rung is one opaque string, so the ladder's own order is
        # the only order there is: keep the declared rungs C used, in place.
        chosen = used.get(spec.axis, set())
        kept = tuple(rung for rung in spec.rungs
                     if rung in chosen and rung != str(spec.baseline))
        field_name = _RUNG_FIELDS[axis_kind(spec.axis)]
        composite.setdefault(axis_kind(spec.axis), []).append(
            replace(spec, **{field_name: kept or spec.rungs[:1]}))
    return _AxisSet(
        steering=new_steering, caps=tuple(new_caps), weights=tuple(new_weights),
        mcs=tuple(composite.get("dlMcsBounds", ())),
        attenuation=tuple(composite.get("txAttenuationDb", ())),
        quota=tuple(composite.get("slicePrbQuota", ())))



# --------------------------------------------------------------------------- #
# the sitting
# --------------------------------------------------------------------------- #


def _sentinel_is_applicable(declared: Any) -> bool:
    """Can this axis's baseline be *applied*, or only reached by withdrawing?

    The question the caller needs is "would a policy body carrying this value
    be built", and the declaration answers it directly -- ``wire_value`` is the
    same call the builder makes.  A multi-leaf axis (mcs bounds, attenuation,
    slice quota) refuses here for a different reason ("no single wire scalar"),
    so it keeps today's conservative treatment; none of them is in the v4
    campaign's axis set and widening that is a separate change.
    """
    # 2026-09-18: `wire_value` 만 묻던 것을 고쳤다.  그 호출은 **단일 스칼라 축 전용**이고
    # 다엽 축은 "ask for policy_values instead" 로 거부한다 -- 거부를 "적용 불가" 로 읽으면
    # 값을 **만들 수 있는 축까지** 철회 전용이 된다.
    #
    # 위 주석이 "none of them is in the v4 campaign's axis set" 이라 넘어갔는데, 2026-09-18
    # 에 **전력 축이 v4 축 집합에 들어갔다**(오너 지시 "3 단위로 두 셀 다 적용해").
    # 실측: `dl-rf-attenuation` 은 `leaves=('txAttenuationDb',)` 로 **잎이 하나**인데
    # `is_composite=True` 라 `wire_value('0.0')` 이 거부되고, `policy_values('0.0')` 는
    # `{'txAttenuationDb': 0.0}` 을 멀쩡히 돌려준다.
    #
    # 그래서 **빌더가 실제로 쓰는 방식**으로 묻는다: 단일 스칼라면 `wire_value`,
    # 다엽이면 `policy_values`.  둘 다 거부하는 축만 철회 전용으로 남는다.
    # 2026-09-18 정정: 한때 다엽 축이면 `policy_values` 로 묻게 바꿨다가 되돌렸다.
    # **라이브 쓰기 경로는 `wire_value` 만 쓴다** (`build.py:1038`,
    # `controlled_scope_builder` -- 셀 범위 축도 같은 빌더를 탄다).  그러니
    # `policy_values` 로 값이 만들어진다고 해서 **쓸 수 있는 것이 아니다**:
    # 감쇠는 되읽기는 되는데(`scalar_leaf_readback` 로 따로 동작, 2026-09-18 에
    # 두 셀 `OBSERVED` 확인) **쓰기가 안 되는** 상태다.
    #
    # 그러므로 이 판정은 빌더가 실제로 부르는 것과 같아야 한다 -- `wire_value`.
    # 다엽 축의 기준 복귀를 쓰기로 표현하려면 빌더가 `policy_values` 를 받게 해야 하고,
    # 그것은 쓰기 경로를 바꾸는 별개 변경이다.
    try:
        declared.wire_value(declared.baseline)
    except Exception:  # noqa: BLE001 - any refusal means "not applicable"
        return False
    return True


@dataclass
class AgentSitting:
    """One sitting: the prepared ``T``/``C``, the frozen case, and the loop."""

    deployment: LiveDeployment
    request: AgentRequest
    mode: str
    rows: Tuple[IntentRow, ...]
    joint: JointComposition
    runtime: JointLiveRuntime
    identities: Mapping[str, LiveUeObservation]
    #: 탐색이 겨냥하는 것 -- Target 역이 만든 T.  방식마다 크기가 다르다(3A·IM 은 6~8,
    #: BM 은 Ω 전체).  **채점에 쓰면 안 된다.**
    contract: TargetContract
    #: **모든 유효 관측이 채점되는 것** -- 인가에서 펼친 Ω 전체.  2026-09-23 결정:
    #: 한 객체가 "탐색 안내" 와 "채점" 두 일을 하고 있었고, 두 일이 일치하는 건
    #: basic-monolith 뿐이어서 3A 는 7개, IM 은 8개, BM 은 144개 목표에 채점됐다 --
    #: BM 이 최대 18배 많은 표적을 가진 셈이라 방식 간 비교가 무효였다.
    #: `omega_targets` 가 이미 같은 산술을 하고 있었으므로 새 기계장치는 없다.
    evaluation_contract: TargetContract
    controls: ControlCandidates
    grid: Grid
    catalog_of_control: Dict[str, str]
    action_space: Mapping[str, Tuple[str, ...]]
    baselines: Mapping[str, str]
    observer: Any
    agents: RoleAgents
    clock: Any
    stamp: str
    evidence_prefix: Path
    preflight: Dict[str, Any]
    policy_port: Any
    policy_builders: List[Any]
    supplementary: Tuple[Any, ...]
    #: Contract v2 section 6: how each KPI kind is observed, and for how long
    #: its answer stays usable.  A sitting setting, never attached to an intent.
    rules: ObservationRules = field(default_factory=ObservationRules)
    #: Contract v2 section 4: the one joint-effect model every method is given.
    #: Its predictions are evidence, never a verdict; it is calibrated from what
    #: the trials actually measured.
    predictor: Optional[JointEffectPredictor] = None
    #: The function catalog and the co-use rules the models are shown.
    catalog: Optional[FunctionCatalog] = None
    #: How many combinations the frozen catalog admits -- the executable
    #: universe this sitting may search, and the size of the prediction table.
    catalog_cardinality: int = 0
    compatibility: CompatibilityRules = field(default_factory=CompatibilityRules)
    unselected_rule: str = RULE_BASELINE
    #: What the intake asked, what came back, and how many rounds it took.
    intake: Dict[str, Any] = field(default_factory=dict)
    #: One entry per calibration: the model form and its state at that moment.
    predictor_versions: List[Dict[str, Any]] = field(default_factory=list)
    #: Windows measured *outside* a trial, because a decision came back after
    #: its observations had expired (contract v2 section 6).
    reobservations: List[Observation] = field(default_factory=list)
    injected: Tuple[str, ...] = ()
    unmapped: Tuple[str, ...] = ()
    #: Builds a second joint case over the same participants, adapters and
    #: reader, for the retention step: the search case admits each candidate
    #: once (the Kernel's no-oscillation rule), so a best control that later
    #: trials superseded can only be re-applied on a case of its own.
    retention_factory: Optional[Callable[..., JointLiveRuntime]] = None
    retention_runtime: Optional[JointLiveRuntime] = None
    retention_kernel_termination: Optional[str] = None
    #: 유지 판정이 비교하는 **이전** 역사 최고 P1 순위.  `decide_retention` 은 현재
    #: 결과를 이 값과 견주므로, 같은 결과가 자기 자신과 비교되지 않도록 판정 뒤에 갱신한다.
    previous_best_rank: float = field(default=RETENTION_NO_ATTAINMENT)
    #: 지금 시행이 복구해야 할 기준선.  유지가 일어나면 유지된 설정이 이것이 된다.
    retention_baseline: Dict[str, str] = field(default_factory=dict)
    #: 판정 기록 하나당 한 줄 -- 역사적 달성과 성공한 유지를 따로 보고하기 위해.
    retention_decisions: List[Dict[str, Any]] = field(default_factory=list)
    #: Recomposes the case over new ids when a UE re-registered between trials,
    #: and resolves a UE label to its current id (docs/design/ue-identity-continuity.md).
    rebind_factory: Optional[Callable[..., Dict[str, Any]]] = None
    amf_of: Optional[Callable[[str], Optional[int]]] = None
    #: Cases retired by a rebind, oldest first; their trials still count.
    retired_runtimes: List[JointLiveRuntime] = field(default_factory=list)
    #: One record per rebind: which UE, old and new id, when, what was freed.
    identity_rebinds: List[Dict[str, Any]] = field(default_factory=list)
    #: One record per hardware disconnect: a UE this case addressed lost its id.
    #: Evidence for the report's exclusion footnote, never an input to a verdict.
    hardware_disconnects: List[Dict[str, Any]] = field(default_factory=list)
    #: Milliseconds composition spent waiting for a UE that was unaddressable, before
    #: this sitting existed.  Added to the in-sitting waits by :meth:`_hardware_wait_ms`.
    composition_wait_ms: float = 0.0
    #: The same waits with their start and end, for the excision record.  Shared with
    #: composition, so a rebind's recomposition wait lands here too.
    composition_wait_log: List[Dict[str, Any]] = field(default_factory=list)
    #: Trial roll-backs (STOP, REVERSE_ROLLBACK, recovery reread, hand-back), with
    #: start, end and ``waitedMs``.  Owner 2026-09-24: "복구 시간은 우리가 안치기로
    #: 했는데 전체 제한에서" -- excised from B like a hardware wait, one capped at
    #: :data:`HARDWARE_WAIT_CAP_MS`, all of them sharing :data:`EXCISED_TOTAL_CAP_MS`.
    recovery_wait_log: List[Dict[str, Any]] = field(default_factory=list)
    service_trace: List[Dict[str, Any]] = field(default_factory=list)
    timing: Dict[str, Any] = field(default_factory=dict)
    #: Contract v4 section 3: every boundary this episode declared, predeclared
    #: ones first.  A boundary is recorded, never acted on as a budget reset.
    boundaries: List[Dict[str, Any]] = field(default_factory=list)
    #: Contract v4 section 2: what was charged to the episode but is not a
    #: trial -- rejected proposals, re-measurements, roll-backs and the KPI
    #: sampling that happens outside a trial.
    non_trial_events: List[Dict[str, Any]] = field(default_factory=list)
    #: Per trial index, the counting facts of contract v4 section 2:
    #: ``counted`` and, once a boundary has been declared, ``beforeBoundary``.
    trial_flags: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    #: Contract v4 section 4: whether this sitting was handed ``T`` and ``C``
    #: instead of forming them, and the content hash of both either way.
    prepared: Dict[str, Any] = field(default_factory=dict)
    #: The common initial measurement, when the request asked for one.  It is
    #: recorded in the grid at index 0 and never counted as a live trial.
    initial_trial: Optional[Trial] = None
    #: The monotonic reading ``t0`` was taken at.  A cold start is handed the
    #: reading from before the Target and Control calls, which is what makes B
    #: run through the preparation; a prepared sitting takes its own in
    #: :meth:`run`.  Kept off the episode's ``timing``, which stays the six
    #: stated keys, because it is a clock reading and not a timestamp.
    t0_monotonic_ms: Optional[float] = None
    #: What the deployment is left holding -- contract 2.5's ``retained``.
    retained: Dict[str, Any] = field(default_factory=dict)
    termination: Optional[str] = None
    termination_detail: str = ""
    #: The last execution-path failure signature seen.  A *repeat* of it on the
    #: next candidate is what proves the cause is shared rather than specific to
    #: one configuration, and ends the episode instead of spending the rest of
    #: the budget on candidates that all need the same unavailable baseline.
    last_execution_failure: Optional[Tuple[str, str]] = None
    kernel_termination: Optional[str] = None
    confirmation_hash: str = ""
    stopped: bool = False
    started_ms: float = 0.0
    ended_ms: float = 0.0
    _cached_cadence_ms: int = 0
    #: A cold start's service sampling from input release; stopped by :meth:`run`.
    formation_sampler: Optional[_FormationSampler] = None

    @property
    def _cadence_ms(self) -> int:
        """How often this case's own polling plan samples, in milliseconds."""
        if not self._cached_cadence_ms:
            self._cached_cadence_ms = max(1, int(polling_plan(self.runtime.kernel).cadence_ms))
        return self._cached_cadence_ms

    # -- what the operator reviews ----------------------------------------- #

    @property
    def is_live(self) -> bool:
        return self.mode == MODE_LIVE

    @property
    def case_id(self) -> str:
        return (self.retired_runtimes[0] if self.retired_runtimes else self.runtime).case_id

    @property
    def intents(self) -> Tuple[Intent, ...]:
        return tuple(row.intent for row in self.rows)

    @property
    def method(self) -> str:
        return self.request.method

    def preview(self) -> Dict[str, Any]:
        """The content the operator confirms before anything runs."""
        # 2026-09-19: the catalog is a frozen domain -- its size is recorded,
        # and only the points C names are listed (the product is never walked).
        named = [self.runtime.candidate_entry(candidate_id)
                 for candidate_id in dict.fromkeys(self.catalog_of_control.values())]
        return {
            "request": self.request.to_record(),
            "joint": self.joint.describe(),
            "epochHash": self.runtime.epoch_hash(),
            "catalogCardinality": self.runtime.catalog_size(),
            "candidates": [{"candidateId": entry.candidate.candidate_id,
                            "parameters": dict(entry.candidate.parameters)}
                           for entry in named if entry is not None],
            "intents": [intent.to_record() for intent in self.intents],
            "T": self.contract.to_record(),
            "C": self.controls.to_record(),
            "controlToCandidate": dict(self.catalog_of_control),
            "unmappedControls": list(self.unmapped),
            "method": self.method,
            "roleModels": self.agents.models.to_record(),
            "actionSpace": {axis: list(values) for axis, values in self.action_space.items()},
            "functionCatalog": (self.catalog.to_record() if self.catalog is not None else []),
            "compatibility": self.compatibility.to_record(),
            "unselectedFunctionRule": self.unselected_rule,
            "measurementRules": self.rules.to_record(),
            "intake": dict(self.intake),
            "predictor": (None if self.predictor is None
                          else dict(self.predictor.describe())),
            "preconditions": list(self.preflight.get("preconditions", [])),
            "budgetTrials": int(self.request.budget_trials),
            "timingMode": self.timing_mode,
            "prepared": dict(self.prepared),
            # ``statedAt`` is what the operator wrote, which is empty for an
            # initial boundary the sitting stamped with the preparation time.
            # The console needs both: the resolved time is what the episode is
            # measured against, and the stated one is how it should be read
            # back to the person who declared it.
            "boundaries": [{**dict(item), "statedAt": str(stated.get("at") or "")}
                           for item, stated in zip(self.boundaries,
                                                   self.request.boundaries)],
        }

    def confirm(self) -> str:
        """Record the confirmation over the preview's content hash."""
        self.confirmation_hash = content_hash(self.preview())
        return self.confirmation_hash

    def request_stop(self) -> None:
        self.stopped = True
        if self.formation_sampler is not None:
            self.formation_sampler.stop()

    # -- timing mode, boundaries and non-trial events (contract v4) --------- #

    @property
    def timing_mode(self) -> str:
        """``prepared`` or ``cold-start`` -- where this episode's clock starts."""
        return str(self.timing.get("timingMode", TIMING_PREPARED))

    @property
    def is_cold_start(self) -> bool:
        return self.timing_mode == TIMING_COLD_START

    def declare_boundary(self, kind: str, detail: str = "", *,
                         at: Optional[str] = None) -> Dict[str, Any]:
        """Declare an episode boundary; record it, and reset nothing.

        ``exp_metrics.md`` section 1.  Three things follow from a boundary and
        none of them is a budget reset:

        * every observation this episode already made is **before** it, so a
          success it established cannot establish success after it -- each such
          trial is marked ``beforeBoundary`` with the boundary's timestamp;
        * ``T`` and ``C`` are reused while their defining inputs are unchanged.
          An ``intent`` or a ``policy`` boundary changes exactly those inputs,
          and this executor cannot re-form the board inside a frozen epoch, so
          the search stops with :data:`EPISODE_BOUNDARY` and the runner
          composes the next sitting.  An ``exogenous`` boundary leaves the
          board standing and the search runs on;
        * normal fading, a control change and a new approval under the same
          policy are not boundaries at all -- :func:`_boundary_record` refuses
          any kind but the three.
        """
        record = _boundary_record({"kind": kind, "detail": detail, "at": at},
                                  at=self.clock.now())
        record["elapsedMs"] = round(self._elapsed_ms(), 3) if self.started_ms else 0.0
        self.boundaries.append(record)
        for trial in self.grid.trials:
            flags = self.trial_flags.setdefault(int(trial.trial_index), {})
            flags.setdefault("beforeBoundary", record["at"])
        if record["invalidatesTC"]:
            self._end(EPISODE_BOUNDARY,
                      f"a declared {record['kind']} boundary at {record['at']} changed "
                      "the inputs T and C were formed from; they are re-formed by "
                      "composing the next sitting, not inside this frozen epoch")
            self.stopped = True
        return record

    def declare_non_trial_event(self, kind: str, detail: str = "",
                                **extra: Any) -> Dict[str, Any]:
        """Charge an event to the episode without counting it as a trial.

        ``exp_metrics.md`` section 3: a rejected proposal, a KPI sample and a
        roll-back on its own each keep their elapsed time on the episode, and
        none of them is an admitted live evaluation.
        """
        if str(kind) not in NON_TRIAL_KINDS:
            raise LiveConsoleError(
                f"unknown non-trial event kind {kind!r}; the kinds are "
                + ", ".join(NON_TRIAL_KINDS))
        record: Dict[str, Any] = {
            "at": self.clock.now(), "kind": str(kind), "detail": str(detail),
            "elapsedMs": round(self._elapsed_ms(), 3) if self.started_ms else 0.0}
        record.update({str(key): value for key, value in extra.items()})
        self.non_trial_events.append(record)
        return record

    def trial_record(self, trial: Trial) -> Dict[str, Any]:
        """One trial's record with contract v4 section 2's counting facts on it.

        ``counted`` is the document's own rule, applied where the document
        applies it: **every admitted live evaluation counts once, at execution
        start** -- an aborted one and a deliberate re-measurement of an
        unchanged control included -- and the common initial measurement at
        index 0 does not.  A proposal the Kernel never admitted never became a
        trial at all; it is in :attr:`non_trial_events`.
        """
        record = trial.to_record()
        flags = dict(self.trial_flags.get(int(trial.trial_index), {}))
        record["counted"] = bool(flags.get(
            "counted", int(trial.trial_index) != INITIAL_TRIAL_INDEX))
        if flags.get("beforeBoundary"):
            record["beforeBoundary"] = str(flags["beforeBoundary"])
        return record

    # -- the applied configuration ----------------------------------------- #

    def applied_configuration(self) -> Dict[str, str]:
        """What the gateway believes is on the radio right now.

        After the retention step the retention case's gateway holds the newest
        belief: it wrote last.
        """
        holder = self.retention_runtime if self.retention_runtime is not None else self.runtime
        live = {str(k): str(v) for k, v in dict(holder.live_baseline()).items()}
        return {axis: live.get(axis, str(value)) for axis, value in self.baselines.items()}

    def network_state(self) -> Dict[str, Any]:
        """``input.network_state``: what is running, on what, measuring what.

        The applied configuration, the cells and UEs, the latest KPIs with the
        moment they were observed, and the rule the unselected functions
        follow.  No trial count, no deadline, no termination option.
        """
        latest = self.latest_kpis()
        state: Dict[str, Any] = {
            "ues": {ue: {"amfUeNgapId": observation.amf_ue_ngap_id,
                         "servingCell": str(observation.serving_nci),
                         "observedAt": observation.observed_at}
                    for ue, observation in self.identities.items()},
            "appliedConfiguration": self.applied_configuration(),
            "unselectedFunctionRule": self.unselected_rule,
        }
        if self.predictor is not None:
            for ue, entry in state["ues"].items():
                entry["offeredLoadMbps"] = self.predictor.state.offered_load_mbps.get(ue)
                # 2026-09-18: 조립 때 실은 마감을 판 도중에도 그대로 싣는다.  빠지면
                # 두 번째 호출부터 `deadlineSuccessRatio` 예측이 사라져, 같은 판 안에서
                # 첫 결정과 나머지 결정이 **다른 정보**로 내려진다.
                if ue in self.predictor.state.echo_deadline_ms:
                    entry["echoDeadlineMs"] = self.predictor.state.echo_deadline_ms[ue]
            state["cells"] = {cell: {"capacityMbps": round(capacity, 6)}
                              for cell, capacity in self.predictor.state.cells.items()}
            state["prbTotal"] = self.predictor.state.prb_total
        if latest:
            state["latestKpis"] = dict(latest)
            state["latestObservedAt"] = self.latest_observed_at
        return state

    def latest_kpis(self) -> Dict[str, Any]:
        """The most recent measured vector, trial or re-observation."""
        rows = self.valid_observations()
        return dict(rows[-1].kpis) if rows else {}

    @property
    def latest_observed_at(self) -> str:
        rows = self.valid_observations()
        return rows[-1].window_end if rows else ""

    def valid_observations(self) -> Tuple[Observation, ...]:
        """Every window whose coverage held, in the order it was measured."""
        # §4.2: 커널이 관측 불충분·실행 실패로 닫은 창은 빼낸다(window.valid 만으로는 모자랐다).
        rows = [Observation.from_trial(trial) for trial in self.grid.trials
                if trial.observation_valid]
        rows.extend(item for item in self.reobservations if item.valid)
        rows.sort(key=lambda item: (item.window_end, item.trial_index))
        return tuple(rows)

    def observations(self, *, with_verdicts: bool = True) -> Tuple[Dict[str, Any], ...]:
        """``input.observations``: what each configuration actually measured.

        Timestamps, the window end, the moment the numbers stop being usable
        and the validity flag are allowed in a model input; the remaining trial
        count, the deadline and the termination options are not, and there is
        no field here that carries them (``SINGLE_CALL.md``, executor
        boundaries).

        ``with_verdicts`` is off for the basic monolith: a verdict is keyed by
        one of *our* target ids, and that arm is never shown our ``T``.
        """
        verdicts = {trial.trial_index: {target: dict(values)
                                        for target, values in trial.verdicts.items()}
                    for trial in self.grid.trials}
        rows: List[Dict[str, Any]] = []
        now = self.clock.now()
        for observation in self.valid_observations():
            row = observation.to_record()
            row.pop("functions", None)
            row["expired"] = bool(observation.valid_until
                                  and str(observation.valid_until) < str(now))
            if with_verdicts:
                if observation.trial_index in verdicts:
                    row["verdicts"] = verdicts[observation.trial_index]
            else:
                row.pop("controlId", None)
            rows.append(row)
        return tuple(rows)

    def effect_evidence(self) -> Dict[str, Any]:
        """``input.effect_evidence``: the predictor's table, and what was measured.

        Contract v2 section 4: a single-call model cannot reach for a tool
        half-way through its answer, so the executor asks the predictor first
        and hands the answer over.  The table covers the product of the exposed
        axes when it fits in 64 rows, otherwise the baseline, every
        single-function move and the joint moves that fit.
        """
        observations = [
            {"configuration": dict(item.configuration), "kpis": dict(item.kpis),
             "observedAt": item.observed_at, "windowEnd": item.window_end,
             "validUntil": item.valid_until, "valid": bool(item.valid)}
            for item in self.valid_observations()]
        # Owner instruction 2026-09-19: which control is related to which KPI,
        # and what was measured -- no predicted value, direction or rank.
        return model_effect_evidence(self.catalog, self.network_state(), observations)

    def calibrate_predictor(self) -> None:
        """Move the predictor toward what this sitting actually measured."""
        if self.predictor is None:
            return
        rows = [{"configuration": dict(item.configuration), "kpis": dict(item.kpis),
                 "valid": True} for item in self.valid_observations()]
        if not rows:
            return
        self.predictor.calibrate(rows)
        version = dict(self.predictor.describe())
        version["afterObservations"] = len(rows)
        version["at"] = self.clock.now()
        self.predictor_versions.append(version)
    # -- the loop ---------------------------------------------------------- #

    def _elapsed_ms(self) -> float:
        return float(self.clock.monotonic_ms()) - self.started_ms

    def _hardware_wait_ms(self) -> float:
        """Milliseconds of our-side time this sitting is relieved of, composition included.

        2026-09-24: the **union of time spans** -- trace stretches flagged our-side
        (each flagged row standing for the whole gap before it), composition waits,
        disconnect and rebind waits, and trial roll-backs -- merged where they overlap
        and each merged run capped at :data:`HARDWARE_WAIT_CAP_MS`.  It used to be a sum
        of per-source numbers in which the trace counted samples x cadence, so a
        stretch with few samples was charged to B almost whole (board 151803: 69 s
        relieved of a 419 s stretch).  B, the Kernel case deadline, ``hardwareWaitMs``
        and the excision record all read this one number.
        """
        rows = getattr(self, "service_trace", None) or ()
        return (_union_ms([*_excised_spans(rows, self._trace_cadence_ms()),
                           *self._wait_spans()]) + self._unanchored_wait_ms())

    def _unanchored_wait_ms(self) -> float:
        """Waits recorded as a number with no time span to put in the union (none
        live: every wait writes its span as it goes; a record built elsewhere may not)."""
        logged = sum(float(item.get("waitedMs") or 0.0)
                     for item in getattr(self, "composition_wait_log", ()) or ())
        extra = max(0.0, float(getattr(self, "composition_wait_ms", 0.0) or 0.0) - logged)
        for item in self.hardware_disconnects:
            covered = _union_ms(_wait_spans(item.get("waits") or ()))
            extra += max(0.0, float(item.get("waitedMs") or 0.0) - covered)
        return extra

    def _disconnect_wait_ms(self) -> float:
        return sum(float(item.get("waitedMs") or 0.0) for item in self.hardware_disconnects)

    def _recovery_wait_ms(self) -> float:
        return sum(float(item.get("waitedMs") or 0.0)
                   for item in getattr(self, "recovery_wait_log", ()) or ())

    def _charge_recovery(self, started_at: Optional[str], ended_at: Optional[str]) -> None:
        """Excise one trial's roll-back from B (owner 2026-09-24), at most one cap."""
        if not (started_at and ended_at):
            return
        start = parse_utc(started_at)
        took = (parse_utc(ended_at) - start).total_seconds() * 1000.0
        if took <= 0:
            return
        waited = min(took, HARDWARE_WAIT_CAP_MS)
        end = format_utc(start + timedelta(milliseconds=waited))
        self.recovery_wait_log.append({"start": started_at, "end": end,
                                       "waitedMs": round(waited, 3)})

    def _excised_trace_ms(self) -> float:
        """Service time excised from the trace itself (our-side gaps, over-long
        handovers, hold extensions) -- current while it is happening, since every
        sample is marked when it is taken (owner 2026-09-23: "볼드모트")."""
        rows = getattr(self, "service_trace", None) or ()
        if not rows:
            return 0.0
        return _excluded_ms(rows, self._trace_cadence_ms(), waits=self._wait_spans())

    def _wait_spans(self) -> List[Tuple[float, float]]:
        return _wait_spans([*(getattr(self, "composition_wait_log", ()) or ()),
                            *(getattr(self, "recovery_wait_log", ()) or ()),
                            *(wait for item in self.hardware_disconnects
                              for wait in item.get("waits") or ())])

    def _trial_reserve_ms(self) -> float:
        plan = polling_plan(self.runtime.kernel)
        return trial_reserve_ms(plan.hold_ms, plan.cadence_ms, self._trailing_ms())

    def _trace_cadence_ms(self) -> float:
        try:
            return float(self._cadence_ms)
        except Exception:  # noqa: BLE001 - a sitting assembled without a Kernel (tests)
            return 1000.0

    def _excision_record(self) -> Dict[str, Any]:
        """Every our-side interval this episode excised, merged, with its evidence.

        The service rows name why each was excised (``excised``) or that it only
        refilled a judged window (``holdExtension``); the waits come from their own
        records.  Nothing here is judged: it is what the judgements left out.
        """
        rows = list(self.service_trace or ())
        cadence = self._trace_cadence_ms()
        intervals: List[Dict[str, Any]] = []
        previous: Optional[Any] = None
        for row in rows:
            flags = list(row.get("excised") or ()) + (
                ["hold-extension"] if row.get("holdExtension") else [])
            if flags:
                last = intervals[-1] if intervals else None
                if last is not None and last.get("_open"):
                    last["end"] = row.get("t")
                    last["samples"] += 1
                    last["reasons"] = sorted(set(last["reasons"]) | set(flags))
                else:
                    intervals.append({"start": previous if previous is not None else row.get("t"),
                                      "end": row.get("t"), "samples": 1,
                                      "reasons": sorted(set(flags)), "_open": True})
            elif intervals:
                intervals[-1]["_open"] = False
            previous = row.get("t")
        for item in intervals:
            item.pop("_open", None)
        for item in getattr(self, "composition_wait_log", ()) or ():
            intervals.append({**dict(item), "reasons": ["composition-wait"], "samples": 0})
        for item in getattr(self, "recovery_wait_log", ()) or ():
            intervals.append({**dict(item), "reasons": ["recovery"], "samples": 0})
        for item in self.hardware_disconnects:
            for wait in item.get("waits") or ():
                intervals.append({**dict(wait), "samples": 0})
        return {"intervals": sorted(intervals, key=lambda item: str(item.get("start"))),
                "excisedSamples": sum(1 for row in rows if row.get("excised")),
                "holdExtensionSamples": sum(1 for row in rows if row.get("holdExtension")
                                            and not row.get("excised")),
                "excisedTraceMs": self._excised_trace_ms(),
                "totalMs": self._hardware_wait_ms(), "capMs": EXCISED_TOTAL_CAP_MS,
                "runCapMs": HARDWARE_WAIT_CAP_MS}

    def _hardware_wait_record(self) -> Dict[str, float]:
        """``hardwareWaitMs``: what the bed cost this episode, by where it was spent."""
        return {"compositionMs": float(self.composition_wait_ms),
                "disconnectMs": self._disconnect_wait_ms(),
                "recoveryMs": self._recovery_wait_ms(),
                "excisedTraceMs": self._excised_trace_ms(),
                "totalMs": self._hardware_wait_ms(),
                "capMs": EXCISED_TOTAL_CAP_MS}

    def _charge_model_time(self, record: Any) -> None:
        """A slow decision is charged as time, never cut off.

        ``MOCK``: the virtual clock advances by the **real** wall-clock latency
        of the call, so an observation's validity behaves exactly as it would
        live (contract v2 section 6).  ``LIVE``: the wall clock has already
        moved; sleeping again would charge the same seconds twice.
        """
        latency = (float(getattr(record, "latency_ms", 0.0) or 0.0)
                   - float(getattr(record, "charged_ms", 0.0) or 0.0))
        if latency > 0 and not self.is_live:
            self.clock.sleep_ms(int(latency))

    def _formation_overrun(self) -> bool:
        """Has the allowance to the first executable proposal been spent?

        Reply 2026-09-14 section 5: the first-proposal allowance is measured from
        input release and is the same for every method, so a prepared method's
        preparation and first selection are charged against it exactly as the
        basic monolith's first direct proposal is.
        """
        budget_ms = self.request.formation_deadline_ms
        if budget_ms is None or self._trials_used_total():
            return False
        # Waiting for a UE the radio dropped is not this method's latency.  The allowance
        # exists to compare methods against each other, so charging it for a hardware
        # outage compares the bed instead -- and with composition now waiting for an absent
        # UE (2026-09-16) a three-minute gap would spend the whole 240 s before the first
        # proposal was even asked for.  The wait is excluded; everything else still counts.
        if self._elapsed_ms() - self._hardware_wait_ms() < float(budget_ms):
            return False
        # 오너 지시(2026-09-18 05:47 "그냥 기다리면 안되나 시간을 따로 기록하면 되잖아",
        # 23:47 재확인): 허용 시간을 넘겨도 **판을 끝내지 않는다.**  넘긴 사실과 시각은
        # 비시행 사건으로 한 번 남기고, 답을 기다려 그대로 쓴다.
        if not getattr(self, "_formation_overrun_recorded", False):
            self._formation_overrun_recorded = True
            self.declare_non_trial_event(
                "formation-overrun",
                f"the first executable proposal was not formed within {budget_ms} ms "
                f"of input release; recorded, and the sitting keeps waiting for it",
                allowanceMs=budget_ms,
                elapsedMs=round(self._elapsed_ms() - self._hardware_wait_ms(), 3))
        return False

    def _decision_overrun(self, started_ms: float, calls_before: int = 0) -> bool:
        """Did this decision outrun its own allowance?

        A late answer cannot authorise a dispatch: the work it consumed stays in
        the episode totals, the outcome is recorded, and the clock is not reset.

        The reason carries **how** the allowance went (handoff 2026-09-18
        section 5.2).  "no executable proposal within the 60000 ms decision
        allowance" alone was read as "one model call took over a minute", which
        it need not be: the same line covers several calls, a revision, and
        local validation.  Splitting model latency from the rest here means the
        census can tell those apart without joining three logs, and no reader
        has to guess which one an arm hit.
        """
        budget_ms = self.request.decision_deadline_ms
        if budget_ms is None:
            return False
        spent = float(self.clock.monotonic_ms()) - float(started_ms)
        if spent < float(budget_ms):
            return False
        made = list(self.agents.calls)[int(calls_before):]
        model_ms = sum(float(getattr(call, "latency_ms", 0.0) or 0.0) for call in made)
        stale = sum(1 for call in made if getattr(call, "stale_at_arrival", False))
        slowest = max((float(getattr(call, "latency_ms", 0.0) or 0.0) for call in made),
                      default=0.0)
        breakdown = (
            f"{len(made)} model call(s) took {model_ms:.0f} ms (slowest "
            f"{slowest:.0f} ms), the remaining {max(0.0, spent - model_ms):.0f} ms "
            f"was local work; {stale} answer(s) arrived stale")
        self.declare_non_trial_event(
            "decision-timeout",
            f"the decision took {spent:.0f} ms against an allowance of "
            f"{budget_ms} ms; recorded, and the answer is used -- "
            + breakdown,
            calls=len(made), modelMs=round(model_ms, 3),
            localMs=round(max(0.0, spent - model_ms), 3),
            slowestCallMs=round(slowest, 3), staleAnswers=stale)
        # 오너 지시(2026-09-18): 넘긴 결정도 **기다려 쓴다.**  위 사건이 소요 시간과 분해를
        # 남기므로 비교는 `decision-timeout` 사건 수와 `decisionLatencyMs` 로 한다.
        return False

    def _eligible_remaining(self) -> int:
        """How many frozen-catalog candidates the Kernel still admits and the radio can reach.

        Only candidates some control of C names count: a proposal must name a
        control, so a catalog combination outside C cannot be dispatched in this
        sitting.  Counting those, 2026-09-15 attempt 51 ended PROPOSAL_FAILURE
        "while 421 candidates were still eligible" when every control of C was
        spent or held by the settled pfWeight@ue1 -- which is exhaustion.
        """
        applied = self.applied_configuration()          # 카탈로그 전체에 한 번만 읽는다
        reachable = {str(self.catalog_of_control[control_id])
                     for control_id in self.catalog_of_control
                     if not self._withdrawal_blocked(control_id, applied)}
        spent = self._spent_elsewhere()
        if self.method == METHOD_BASIC_MONOLITH:
            # The basic monolith is not confined to C: its universe is the
            # whole frozen domain.  Remaining = cardinality - spent here and in
            # retired cases.  An upper bound: points the compatibility rules or
            # a settled policy put out of reach are not subtracted (counting
            # them exactly means walking the product).
            return max(0, int(self.runtime.catalog_size())
                       - len(set(self.runtime.spent_ids()) | spent))
        return sum(1 for candidate_id in reachable
                   if candidate_id not in spent and self._available(candidate_id))

    def _transition_exceeds(self, control_id: str) -> str:
        """왜 이 후보를 **지금 상태에서** 적용할 수 없는지, 전이 한도로 본 이유.

        2026-09-23 결정 §3.3: "The limit of four function/scope changes must be
        checked against the **actual transition**; do not silently bypass it to
        apply a candidate."  후보 승인 때 세는 수(`tc.py` 의
        `validate_control_candidates`)는 **C0 기준**이다 -- C0 는 후보를 전개하는 고정
        기준이라 그것이 맞다.  그런데 유지 모드에서는 배치가 C1 에 앉아 있을 수 있고,
        C1→C2 전이는 두 후보가 서로 다른 축을 쓰면 최대 8축을 움직인다.  둘 다 C0 와는
        4축만 다르므로 승인 검사는 통과한다 (codex 감사 #5).

        빈 문자열이면 한도 안이다.  유지 모드가 아니면 매 시행이 기준선에서 출발하므로
        전이가 곧 승인 때 센 수이고, 이 검사는 아무것도 새로 막지 않는다.
        """
        limit = getattr(self.compatibility, "max_changed_entries", None)
        if limit is None:
            limit = MAX_CHANGED_ENTRIES
        candidate = (self.controls.candidate(control_id)
                     if self.controls is not None else None)
        if candidate is None:
            return ""
        applied = dict(self.applied_configuration() or {})
        if not applied:
            return ""
        moved = sorted(axis for axis, value in dict(candidate.configuration).items()
                       if str(applied.get(axis, value)) != str(value))
        if len(moved) <= int(limit):
            return ""
        return (f"{control_id} moves {len(moved)} function/scope entries from the "
                f"configuration now applied ({', '.join(moved)}); at most {int(limit)} "
                "may change per transition")

    def _withdrawal_blocked(self, control_id: str,
                            applied: Optional[Mapping[str, str]] = None) -> Tuple[str, ...]:
        """Axes this control would return to a sentinel a finalized policy holds.

        2026-09-15 attempt 27: C04 settled live with dlPrbCap@ue3 18, and the next
        two proposals asked that axis back to "0".  Only withdrawing the settled
        policy could do that and no permit expresses it (``_axes_needing_withdrawal``),
        so each spent a trial on PREPARE REJECTED and the pair ended the sitting
        as an execution failure.  Such a control is not executable, not failed.
        """
        candidate = self.controls.candidate(control_id)
        if candidate is None:
            return ()
        # 2026-09-22: `applied_configuration()` 은 게이트웨이의 live_baseline 을 조회한다.
        # `_eligible_remaining()` 이 카탈로그의 control 마다 이 함수를 부르므로, 조회가
        # control 수만큼(이 판 512회) 반복됐다.  시팅이 한 시행에서 코어 하나를 100% 로
        # 태우며 5분 넘게 아무것도 출력하지 않던 정체가 이것이다.  한 번 읽어 넘긴다.
        return self._axes_needing_withdrawal(
            {str(axis): str(value) for axis, value in candidate.configuration.items()},
            self.applied_configuration() if applied is None else applied)

    def _candidate_status(self) -> Dict[str, Dict[str, Any]]:
        """Kernel-owned status per control id, for the model to read.

        C keeps its frozen membership, cardinality and identities; only this
        status moves.  Until it was carried the model could not tell a spent
        candidate from a fresh one, proposed one it had already run, and the
        executor closed the episode with eligible candidates untouched.
        """
        by_candidate = {str(candidate_id): self.runtime.candidate_entry(str(candidate_id))
                        for candidate_id in dict(self.catalog_of_control).values()}
        spent = self._spent_elsewhere()
        rows: Dict[str, Dict[str, Any]] = {}
        for control_id, candidate_id in dict(self.catalog_of_control).items():
            entry = by_candidate.get(str(candidate_id))
            if entry is None:
                continue
            availability = ("CONSUMED" if str(candidate_id) in spent
                            else str(entry.availability.value))
            rows[str(control_id)] = {
                "candidateId": str(candidate_id),
                "availability": availability,
                "eligible": availability == "AVAILABLE",
                "tried": availability in ("CONSUMED", "IN_FLIGHT", "LOCKED"),
            }
            blocked = self._withdrawal_blocked(control_id) if availability == "AVAILABLE" else ()
            if blocked:
                rows[str(control_id)]["eligible"] = False
                rows[str(control_id)]["withdrawalOnlyAxes"] = list(blocked)
        # What each spent candidate actually produced, so a revision can be
        # informed rather than blind.  The runtime keeps one record per trial.
        by_control = {str(v): str(k) for k, v in dict(self.catalog_of_control).items()}
        for record in self._all_kernel_trials():
            control_id = by_control.get(str(dict(record).get("candidateId") or ""))
            row = rows.get(control_id or "")
            if row is None:
                continue
            row["priorOutcome"] = dict(record).get("terminalState")
            row["priorDetail"] = dict(record).get("detail")
        return rows

    def _shown_ids(self) -> Dict[str, str]:
        """Omega id → 선택기에게 보일 id.  벡터가 안 바뀌므로 판에 한 번 만든다."""
        cached = getattr(self, "_shown_ids_cache", None)
        if cached is None:
            cached = shown_target_ids(self.evaluation_contract, self.contract)
            self._shown_ids_cache = cached
        return cached

    def _shown(self, target_id: Any) -> Any:
        if target_id is None:
            return None
        return self._shown_ids().get(str(target_id), target_id)

    def _trajectory_inputs(self) -> TrajectoryInputs:
        # 2026-09-23 (codex 감사 R-1): 판정·최고·격차는 Omega 로 채점되고 Omega 이름을
        # 달고 나온다.  선택기는 같은 프롬프트에서 T 를 T 이름으로 받고 T 이름으로
        # 답한다 -- 철자가 겹치는 다른 벡터다.  **보여 줄 때만** 번역한다
        # (`shown_target_ids`): T 에 같은 벡터가 있으면 그 T 이름, 없으면 `omega:` 접두.
        # 3A 와 IM 이 이 한 자리를 같이 쓰므로 두 방식에 같은 직렬화다(결정 §2).
        shown = self._shown
        observations = []
        for row in self.observations():
            row = dict(row)
            if "verdicts" in row:
                row["verdicts"] = {shown(target): verdict
                                   for target, verdict in dict(row["verdicts"]).items()}
            observations.append(row)
        best = observed_best(self.valid_observations(), self.contract)
        if self._v5_order():
            best = self._v5_online()
        elif best is not None:
            best = dict(best)
            best["targetId"] = shown(best.get("targetId"))
            best.update(self._history(with_ids=True))
        # **Ω 로 잰다.**  `cheapest_unsatisfied` 가 선택된 T 안에서만 다음 목표를
        # 고르면, 각 방식은 자기가 적은 사다리로만 유도된다 -- 없애려는 비대칭 그
        # 자체다.  `observed_best` 는 인자와 무관하게 이미 Ω 로 펼치므로(그쪽은
        # `inPreparedT` 진단을 위해 선택된 T 를 계속 받는다) 여기만 바꾼다.
        gaps = kpi_gaps(self.latest_kpis(), self.evaluation_contract)
        if gaps is not None:
            gaps = dict(gaps)
            gaps["nextTargetId"] = shown(gaps.get("nextTargetId"))
            rows = {}
            for req_id, row in dict(gaps.get("perRequirement") or {}).items():
                row = dict(row)
                for side in ("againstT0", "againstNext"):
                    if isinstance(row.get(side), Mapping):
                        row[side] = {**dict(row[side]),
                                     "targetId": shown(row[side].get("targetId"))}
                rows[req_id] = row
            gaps["perRequirement"] = rows
        return TrajectoryInputs(
            target_contract=self.contract, control_candidates=self.controls,
            network_state=self.network_state(), observations=tuple(observations),
            observed_best=best,
            kpi_gaps=gaps,
            grid=self.grid, candidate_status=self._candidate_status())

    def _basic_inputs(self) -> BasicInputs:
        # 2026-09-17 (오너 드롭 RAN_AGENT_PROMPTS_REVISED_20260917.md, Runtime
        # alignment 2·4): 온라인 선택기(Trajectory/Select)는 `observed_best` 와
        # `kpi_gaps` 를 받는데 이 팔만 못 받고 있었다 -- "Expose equivalent ...
        # best-result information to the online selectors and BM."
        # 목표 id 는 이 팔에 뜻이 없으므로(T 를 안 받는다) 같은 항목이 정한 대로
        # **요구조건 레벨을 함께** 싣는다.
        return BasicInputs(
            intents=self.intents, authorization=self.contract.authorization,
            function_catalog=self.catalog, compatibility=self.compatibility,
            network_state=self.network_state(),
            effect_evidence=self.effect_evidence(),
            observations=self.observations(with_verdicts=False),
            # Trial 0 dispatches nothing (Codex review of f05dcd319): its configuration is tried
            # only through a valid observation, so a failed initial measurement can be retaken.
            dispatched=tuple(dict(trial.configuration) for trial in self.grid.trials
                             if trial.configuration and trial.trial_index != 0),
            observed_best=self._v5_online() if self._v5_order() else self._best_with_levels(),
            kpi_gaps=self._gaps_without_target_ids())

    def _gaps_without_target_ids(self) -> Optional[Dict[str, Any]]:
        """``kpi_gaps`` for the arm that never sees our ``T``.

        The gaps carry the substance this arm needs -- observed value, the
        threshold it is measured against, the shortfall and the verdict -- but
        they name the thresholds by **our** target ids (``againstT0.targetId``,
        ``againstNext.targetId``, ``nextTargetId``).  Those are positional
        labels of our own ladder; the numbers are what mean anything here.
        """
        gaps = kpi_gaps(self.latest_kpis(), self.evaluation_contract)
        if not gaps:
            return None
        found = dict(gaps)
        found.pop("nextTargetId", None)
        rows = {}
        for req_id, row in dict(found.get("perRequirement") or {}).items():
            entry = dict(row)
            for key in ("againstT0", "againstNext"):
                against = entry.get(key)
                if isinstance(against, Mapping):
                    entry[key] = {k: v for k, v in dict(against).items()
                                  if k != "targetId"}
            rows[req_id] = entry
        if rows:
            found["perRequirement"] = rows
        return found

    def _best_with_levels(self) -> Optional[Dict[str, Any]]:
        """``observed_best`` for an arm that never sees our ``T``.

        `observed_best` names the target by **our** id, which is opaque to the
        basic monolith.  The drop requires the levels and preference beside it,
        so the id is kept for provenance and the levels are spelled out.
        """
        best = observed_best(self.valid_observations(), self.contract)
        if not best:
            return None
        found = dict(best)
        # **우리 목표 id 는 떼어낸다.**  이 팔은 우리 `T` 를 보지 않는다는 불변식이
        # 따로 있고(`test_the_basic_monolith_sees_its_own_observations_and_no_target_id`),
        # 드롭도 "Include its requirement levels and preference information" 이라고 했지
        # 목표 id 를 주라고 하지 않았다.  뜻을 나르는 것은 **레벨**이고 id 는 우리 쪽
        # 자리번호일 뿐이다([[target-ids-are-positional-compare-level-vectors]]).
        # 우리 쪽 **자리번호는 전부** 뗀다 -- 목표 id 와 후보 id 둘 다.  이 팔이 보면
        # 안 되는 것의 목록은 테스트가 갖고 있다(targetId · controlId · verdicts ·
        # input.control_candidates).  남기는 것은 숫자와 레벨 -- 뜻을 나르는 쪽이다.
        named = found.pop("targetId", None)
        found.pop("controlId", None)
        # 결정 §2: 같은 **종류**의 피드백을 모든 선택기에 -- 역사도 레벨로 준다.
        found.update(self._history(with_ids=False))
        for target in self.contract.targets:
            if target.target_id == named:
                found["levels"] = dict(target.levels)
                found["deadlineLevels"] = dict(target.deadline_levels)
                break
        return found

    def _best_summary(self, best: Optional[Mapping[str, Any]], *, with_ids: bool
                      ) -> Optional[Dict[str, Any]]:
        """역사 한 점: 어떤 목표를, 어느 시행이, 어떤 P1 으로 뒷받침했나."""
        if not best:
            return None
        row: Dict[str, Any] = {"p1": best.get("p1"), "trialIndex": best.get("trialIndex"),
                               "observationRefs": list(best.get("observationRefs") or [])}
        if with_ids:
            row["targetId"] = self._shown(best.get("targetId"))
        else:
            # T 를 안 받는 방식: 자리번호 대신 **레벨**을 준다 (`_best_with_levels` 와 같은 규칙).
            target = self.evaluation_contract.target(str(best.get("targetId")))
            if target is not None:
                row["levels"] = dict(target.levels)
                row["deadlineLevels"] = dict(target.deadline_levels)
        return row

    def _history(self, *, with_ids: bool) -> Dict[str, Any]:
        """직전 역사 최고와 이번 시행이 뒷받침한 최고 (2026-09-23 결정 §2, O-2).

        결정 §2 가 모든 선택기에 주라고 한 네 가지 중 하나가 **"The previous and updated
        historical best, with supporting trial references"** 다.  `observed_best` 가 이미
        갱신된 역사 최고(유효 관측 전체, P1 순)이므로, 여기서 더하는 것은 두 가지다:
        마지막 시행 **이전까지의** 최고와, 마지막 시행 **하나만의** 최고.  둘을 나란히
        두면 선택기가 "방금 시행이 역사를 넘었나" 를 스스로 읽는다 -- 우리가 해석을
        얹지 않는다.  유지 판정이 비교하는 바로 그 두 값이다.
        """
        valid = self.valid_observations()
        if not valid:
            return {}
        latest = max(int(item.trial_index) for item in valid)
        before = [item for item in valid if int(item.trial_index) != latest]
        only = [item for item in valid if int(item.trial_index) == latest]
        return {"latestTrialIndex": latest,
                "previousBest": self._best_summary(
                    observed_best(before, self.contract), with_ids=with_ids),
                "latestTrialBest": self._best_summary(
                    observed_best(only, self.contract), with_ids=with_ids)}

    #: Re-asks spent on this decision when no B bounds them (see ``_may_reask``).
    _reasks_without_b = 0

    def _may_reask(self, reason: str = "", record: Any = None) -> bool:
        """Another question fits B: record why it is asked, and say whether to ask.

        ``MOCK`` charges the refused generation's time here, before asking again,
        with a floor: a scripted refusal costs microseconds of real time, and
        re-asking until B on that clock would never reach B.
        """
        if record is not None and not self.is_live:
            generations = list(getattr(record, "generations", ()) or ())
            latency = float((generations[-1].get("latencyMs") if generations else 0.0) or 0.0)
            step = max(latency, MOCK_REASK_FLOOR_MS)
            self.clock.sleep_ms(int(step))
            setattr(record, "charged_ms", float(getattr(record, "charged_ms", 0.0)) + latency)
        if self._past_deadline():
            return False
        if self.request.deadline_ms is None:
            # No B, nothing to bound the re-asking with: the single repair a
            # decision always had (hermetic sittings, the operator console).
            used = (int(getattr(record, "repair_retries", 0)) if record is not None
                    else self._reasks_without_b)
            if used >= 1:
                return False
            if record is None:
                self._reasks_without_b += 1
        self.declare_non_trial_event("re-ask", reason or "the answer was refused")
        return True

    def _select(self, inputs: TrajectoryInputs):
        if self.method == METHOD_INTERNAL_MONOLITH:
            return self.agents.monolith_select(inputs)
        return self.agents.select_next(inputs)

    def _decide_basic(self, inputs: BasicInputs):
        script = os.environ.get(CALIBRATION_SCRIPT_ENV)
        if script:
            return scripted_calibration_decision(inputs, script,
                                                 os.environ.get("AIC_CAMPAIGN", ""))
        return self.agents.basic_monolith_decide(inputs)

    def _next_decision(self) -> Tuple[Optional[Tuple[str, str]], Dict[str, Any]]:
        """One decision, one call: which target, applied by which control.

        The returned record carries its own ``decisionLatencyMs``, measured here.  It
        used to be stamped by the loop after the *first* call only, so a decision asked
        again after a rejected proposal went into the grid at 0 ms (2026-09-23 audit),
        skewing the latency every method is compared on.
        """
        started = float(self.clock.monotonic_ms())
        pair, record = self._next_decision_untimed()
        record["decisionLatencyMs"] = float(self.clock.monotonic_ms()) - started
        return pair, record

    def _next_decision_untimed(self) -> Tuple[Optional[Tuple[str, str]], Dict[str, Any]]:
        """:meth:`_next_decision` without the stopwatch.

        **Age alone no longer discards an answer** (owner scenario 2026-09-22
        section 3).  Past KPIs are reference material for the next control
        choice; what has to hold at execution time is that the chosen control is
        still applicable, and success is judged by the *new* observation taken
        after it is applied -- not by the numbers the model was shown.  So a
        late answer is kept, marked ``stale_at_arrival`` so the record still says
        the numbers had aged, and executed.

        Applicability is still checked before anything is written, but it is
        checked where it always was -- the catalog admission and the Write
        Gateway, which refuse a control that settled, was exhausted or moved
        under the answer.  A guard here would never fire: an answer naming a
        control that is not a candidate is refused by the agent layer before it
        ever reaches this loop (measured 2026-09-22), so re-observation is no
        longer reachable from a late answer.

        The decision and trial *time limits* are unchanged; they are charged in
        ``_charge_model_time`` and bounded by ``B``, which is a separate matter
        from how old the observations were.
        """
        basic = self.method == METHOD_BASIC_MONOLITH
        build = self._basic_inputs if basic else self._trajectory_inputs
        ask = self._decide_basic if basic else self._select

        # Owner instruction 2026-09-19: no deterministic rule ever answers for
        # the model.  A refused answer is asked again inside ``_decide`` (the
        # reason is recorded as a ``re-ask`` event by ``_may_reask``); a stale
        # one is re-observed and asked again.  Both stop only when B does.
        self.agents.may_reask = self._may_reask
        inputs = build()
        while True:
            try:
                decision, record = ask(inputs)
            except DecisionUnavailable as exc:
                self._charge_model_time(exc.record)
                return None, _decision_record(exc.record, None, None)
            self._charge_model_time(record)
            if self._answered_stale(inputs):
                # 기록에는 남기되 답을 버리지는 않는다.  성공 판정은 적용 뒤의
                # 새 관측이 하므로, 모델이 본 숫자가 낡았다는 사실 자체는
                # 판정에 쓰이지 않는다.
                record.stale_at_arrival = True
            if decision is None:
                return None, _decision_record(record, None, None)
            break
        if basic:
            control_id = self._control_for(decision.configuration)
            return (self.contract.t0.target_id, control_id), _decision_record(
                record, self.contract.t0.target_id, control_id)
        return ((decision.target_id, decision.control_id),
                _decision_record(record, decision.target_id, decision.control_id))

    def _answered_stale(self, inputs: Any) -> bool:
        """Did this answer arrive after the numbers it was given expired?

        Only the observations that were **still valid when the call was made**
        count: one already past its validity was history when it was handed
        over, and re-observing would never make it fresh again (contract v2
        section 6).
        """
        fresh = [row for row in inputs.observations if not dict(row).get("expired")]
        return bool(fresh) and self.rules.is_stale(fresh, self.clock.now())

    def _reobserve(self) -> Observation:
        """One window on the applied configuration -- not a trial, but charged.

        Since the owner scenario of 2026-09-22 a late answer no longer triggers
        this: age alone does not discard an answer, so nothing in the decision
        loop calls it.  It stays as the sitting's measurement primitive -- the
        one way to take a window without asking the Kernel for anything.

        The Kernel is not asked for anything: nothing is applied, nothing is
        judged, no candidate is spent.  The sitting simply measures what is
        already running so the next question is asked over numbers that are
        still valid.
        """
        applied = self.applied_configuration()
        control_id = next((candidate.control_id for candidate in self.controls.candidates
                           if candidate.configuration == applied), "")
        started = self.clock.now()
        rows: List[Dict[str, Any]] = []
        polls = max(1, -(-int(self.rules.hold_ms()) // max(1, int(self._cadence_ms))))
        due = float(self.clock.monotonic_ms()) + float(self._cadence_ms)
        for _index in range(polls):
            self._sleep_to(due)
            rows.append(self._sample_service_trace())
            due += float(self._cadence_ms)
        self._extend_hold(rows, polls, int(self._cadence_ms), in_trial=False)
        window_end = self.clock.now()
        kpis, unknown, coverage, cohorts = self._aggregate_window(rows, window_end)
        window = self._window_record(started, window_end, rows, kpis, unknown,
                                     coverage, cohorts)
        observation = Observation(
            control_id=control_id, trial_index=0, configuration=dict(applied),
            kpis=kpis, observed_at=started, window_end=window_end,
            valid_until=self._valid_until(kpis, window_end),
            valid=bool(kpis) and bool(window["valid"]),
            reason="re-observed because the last answer arrived stale")
        self.reobservations.append(observation)
        self.preflight.setdefault("reobservations", []).append(
            {"at": window_end, "controlId": control_id, "coverage": coverage,
             "unknownKpis": unknown, "valid": window["valid"],
             "invalidReason": window["reason"] or None, "cohorts": window["cohorts"],
             "trailingCollectionEnd": window["trailingCollectionEnd"]})
        self.declare_non_trial_event(
            "remeasurement", "the applied configuration was re-observed because the "
                             "last answer arrived after its numbers had expired; "
                             "nothing was applied and no candidate was spent",
            controlId=control_id)
        return observation

    def _aggregate_window(self, rows: Sequence[Mapping[str, Any]], hold_end: str,
                          *, held: bool = True, waited: bool = False,
                          ) -> Tuple[Dict[str, Any], List[str], Dict[str, float], Dict[str, Any]]:
        """One KPI vector from a hold's samples, under the sitting's own rules.

        Contract v2 section 6: the samples in ``[holdEnd - window, holdEnd]``,
        the stated statistic, and ``UNKNOWN`` when the coverage is below the
        minimum.  A KPI that came back ``UNKNOWN`` is **left out** of the
        vector, so :func:`assurance.coordination.judge` reads it as missing --
        a thin window can never establish a success, and it is never quietly
        averaged into one either.  The window record says which ones and how
        thin.

        The fourth value is the window record's ``cohorts`` part: one audit
        record per issued-cohort KPI (:func:`issued_cohort_ratios`) and, when a
        trailing collection ran, the moment it completed -- which is later than
        the window end by at least the longest deadline.  ``waited`` says the
        caller already kept the configuration applied that long (a trial's
        runtime does), so no second wait is taken.
        """
        # An excised sample is not a failure and not UNKNOWN: it does not exist, and
        # the window reaches back over usable time only (owner 2026-09-23).
        rows = _without_excised(rows, self._cadence_ms)
        aggregated = self.rules.aggregate(rows, hold_end, self._cadence_ms)
        coverage = self.rules.coverage(rows, hold_end, self._cadence_ms)
        # A live tagged-echo source is re-read once, after the window closed,
        # for the requests *issued* in it -- the cohort the requirement names,
        # rather than the matured set differencing produces.  With no such
        # source this is empty and the vector is exactly what it always was.
        audit: Dict[str, Any] = {}
        cohort = issued_cohort_ratios(
            rows, hold_end, self.rules, _echo_sources(self.observer),
            sleep_ms=None if waited else self.clock.sleep_ms, audit=audit, held=held)
        for key, value in cohort.items():
            # The cohort replaces the differenced value, never the coverage rule: a window
            # mostly lost to a disconnect stays UNKNOWN however its few requests fared.
            minimum = float(self.rules.rule_for(key).min_coverage)
            if value != UNKNOWN and float(coverage.get(key, 0.0)) < minimum:
                value = UNKNOWN
                audit.setdefault(key, {}).update(
                    valid=False, invalidReason="sample-coverage-below-minimum",
                    coverage=round(float(coverage.get(key, 0.0)), 6), minCoverage=minimum)
            aggregated[key] = value
        # **파생값은 자기 입력의 유효성을 물려받는다** (2026-09-23, 시나리오 검토 §1).
        # `cellGoodputMbps@<nci>` 는 그 창의 `servingCell@<ue>` 로 UE 를 셀에 묶어 만든
        # 합계다.  그런데 두 KPI 의 `minCoverage` 가 다르다 -- 소속은 1.0, 셀 합계는 0.8.
        # 그래서 표본 하나만 빠져도 소속은 UNKNOWN 이 되는데 합계는 통과했다:
        # v4.4 전수 144 시행 중 **21건(15%)** 이 "셀 총량은 발행, 소속은 UNKNOWN" 이었고,
        # 그 판의 사업자 요구 판정은 기록이 모른다고 말하는 소속에 기대고 있었다.
        # fail-closed 로 만든 집계(`cell_goodput_totals` 는 UE 하나만 빠져도 {} 를 낸다)에
        # 뚫린 fail-open 구멍이라, 여기서 막는다.
        stale_membership = sorted(
            key for key, value in aggregated.items()
            if value == UNKNOWN and key.startswith(f"{SERVING_CELL_KPI}@"))
        if stale_membership:
            for key in [k for k in aggregated if k.startswith(f"{CELL_GOODPUT_KPI}@")]:
                if aggregated[key] == UNKNOWN:
                    continue
                aggregated[key] = UNKNOWN
                audit.setdefault(key, {}).update(
                    valid=False, invalidReason="serving-cell-membership-unknown",
                    membershipUnknown=stale_membership)
        kpis = {key: value for key, value in aggregated.items() if value != UNKNOWN}
        unknown = sorted(key for key, value in aggregated.items() if value == UNKNOWN)
        cohorts = {"byKpi": audit,
                   "trailingCollectionEnd": self.clock.now() if audit and held else None}
        return kpis, unknown, {key: round(value, 6) for key, value in coverage.items()}, cohorts

    def _trailing_ms(self) -> float:
        """How long after a window its last issued request is owed: the longest
        deadline any live tagged-echo source measures, 0 without one."""
        return max((float(value) for source in _echo_sources(self.observer)
                    for value in source.deadlines_ms), default=0.0)

    def _valid_until(self, kpis: Mapping[str, Any], window_end: str) -> str:
        """The moment the first of these numbers stops being usable."""
        stamps = [self.rules.valid_until(key, window_end) for key in kpis]
        return min((str(item) for item in stamps if item), default=str(window_end))

    def _window_record(self, started: str, window_end: str, rows: Sequence[Any],
                       kpis: Mapping[str, Any], unknown: List[str],
                       coverage: Mapping[str, float], cohorts: Mapping[str, Any]
                       ) -> Dict[str, Any]:
        """One observation window; an unusable issued cohort makes it invalid.

        A cohort the source could not establish is already ``UNKNOWN`` in the
        vector; this also says so on the window, with the reason, so the
        observation can never be read as a valid one that merely lacked a KPI.
        """
        invalid = sorted(f"{key} cohort invalid: {record.get('invalidReason')}"
                         for key, record in dict(cohorts.get("byKpi") or {}).items()
                         if not record.get("valid"))
        reason = ("no observation window was opened" if not rows
                  else "; ".join(invalid))
        excised = sum(1 for row in rows if row.get("excised"))
        return {"start": started, "end": window_end, "valid": bool(rows) and not invalid,
                "reason": reason, "samples": len(rows) - excised, "coverage": dict(coverage),
                "excisedSamples": excised,
                "extensionSamples": sum(1 for row in rows if row.get("holdExtension")),
                "unknownKpis": unknown,
                "validUntil": self._valid_until(kpis, window_end),
                "cohorts": dict(cohorts.get("byKpi") or {}),
                "trailingCollectionEnd": cohorts.get("trailingCollectionEnd")}
    def _control_for(self, configuration: Mapping[str, str]) -> str:
        """The column this configuration is; a new one is added when it is new.

        The basic monolith answers with values, never with one of our ids -- it
        has never seen ``C``.  Mapping its answer onto the same column index
        every other arm uses is what makes the arms comparable at all, and it
        is deterministic executor code, not a judgement.
        """
        wanted = tuple(sorted((str(k), str(v)) for k, v in dict(configuration).items()))
        for candidate in self.controls.candidates:
            if candidate.signature == wanted:
                return candidate.control_id
        control_id = f"M{len(self.controls.candidates)}"
        added = ControlCandidate(control_id=control_id, configuration=dict(configuration),
                                 applicability=("answered by the basic monolith",))
        self.controls = replace(self.controls,
                                candidates=self.controls.candidates + (added,))
        self.grid.controls = self.controls
        entry = self._catalog_entry(added.configuration)
        if entry is not None:
            self.catalog_of_control[control_id] = entry
        return control_id

    def _catalog_entry(self, configuration: Mapping[str, str]) -> Optional[str]:
        """Membership: the frozen point this configuration is, or ``None``."""
        entry = self.runtime.find_candidate(configuration)
        return None if entry is None else str(entry.candidate.candidate_id)

    def run(self, *, on_trial: Optional[Callable[[Dict[str, Any]], None]] = None,
            on_decision: Optional[Callable[[Dict[str, Any]], None]] = None,
            ) -> Dict[str, Any]:
        """Trial after trial until a stated reason ends the sitting."""
        if not self.confirmation_hash:
            raise LiveConsoleError("the Agent sitting has not been confirmed")
        plan = polling_plan(self.runtime.kernel)
        hold_polls = max(1, -(-int(plan.hold_ms) // int(plan.cadence_ms))) + 1
        if self.is_cold_start:
            # ``exp_metrics.md`` section 3: in a cold start ``t0`` is the moment
            # the required inputs were released -- before the Target and the
            # Control calls -- so preparation has already consumed part of B.
            # No clock reset here, and no synchronisation wait: the loop's own
            # deadline check below is what ends a sitting whose B expired while
            # T and C were being formed, with nothing applied.
            self.started_ms = float(self.t0_monotonic_ms or 0.0)
            if self.formation_sampler is not None:
                # Sampling hands over to the loop below with no overlap.
                self.formation_sampler.stop()
                if self.formation_sampler.errors:
                    self.preflight.setdefault("observerErrors", []).extend(
                        self.formation_sampler.errors)
        else:
            self.started_ms = float(self.clock.monotonic_ms())
            self.t0_monotonic_ms = self.started_ms
            self.timing["t0"] = self.clock.now()
        self._sample_service_trace()
        self.declare_non_trial_event(
            "sample", "the applied configuration sampled once at t0, before the search")
        budget = int(self.request.budget_trials)
        # Amendment 5/8: a trial is started only when B still holds its whole
        # hold plus the trailing collection its longest deadline is owed.  The
        # judging window itself is never shortened to fit.
        trailing_ms = self._trailing_ms()
        reserve_ms = trial_reserve_ms(plan.hold_ms, plan.cadence_ms, trailing_ms)
        if self.request.initial_measurement and not self._past_deadline(
                float(self.rules.hold_ms()) + trailing_ms):
            self._initial_measurement()
        while True:
            if self.termination is not None:
                # Trial 0 already met T0, or a declared boundary stopped the
                # search: the sitting has its stated reason and does not open
                # another trial to re-establish it.
                break
            if self.stopped:
                self._end(OPERATOR_STOP, "stopped by the operator before the next trial")
                break
            if self._trials_used_total() >= budget:
                self._end(BUDGET_EXHAUSTED,
                          f"the frozen budget of {budget} trials is spent; a spent "
                          "budget is not a proof that no configuration exists")
                break
            gone = [ue for ue in self._unaddressable_ues() if self.amf_of(ue) is None]
            if gone:
                self._record_disconnect(gone, None)
                if not self._await_reregistration(gone, reserve_ms):
                    break
            if not self._rebind_if_reregistered():
                break                                   # never onto a dead id
            if self._past_deadline(reserve_ms):
                break
            if self._formation_overrun():
                break
            started = float(self.clock.monotonic_ms())
            calls_before = len(self.agents.calls)
            self._reasks_without_b = 0
            pair, decision = self._next_decision()
            if self._decision_overrun(started, calls_before):
                if on_decision is not None:
                    on_decision(decision)
                break
            if on_decision is not None:
                on_decision(decision)
            if pair is None and self._past_deadline():
                break                                   # B ran out while re-asking: DEADLINE
            if pair is None:
                self._end(CATALOG_EXHAUSTED if not self._eligible_remaining()
                          else PROPOSAL_FAILURE,
                          "no untried control remains that this sitting may apply"
                          if not self._eligible_remaining() else
                          "the decision produced no executable proposal while "
                          f"{self._eligible_remaining()} candidates were still eligible")
                break
            # Owner instruction 2026-09-19: a rejected proposal is asked again
            # -- the rejection is recorded and reaches the agent through the
            # candidate status -- until B runs out, with no revision cap of our
            # own.  A rejection is still not an exhaustion certificate; only an
            # empty eligible set is.
            reason = ""
            while True:
                target_id, control_id = pair
                candidate_id = self.catalog_of_control.get(control_id)
                if candidate_id is None:
                    reason = f"{control_id} names no candidate of the frozen catalog"
                elif not self._available(candidate_id):
                    reason = (f"{control_id} has already been tried; the frozen "
                              "catalog admits each candidate once")
                elif self._transition_exceeds(control_id):
                    reason = self._transition_exceeds(control_id)
                elif self._withdrawal_blocked(control_id):
                    reason = (f"{control_id} returns {', '.join(self._withdrawal_blocked(control_id))} "
                              "to its baseline while a policy an earlier trial settled live "
                              "holds it, which only withdrawing that policy could do; another "
                              "value on those axes is applicable, the baseline is not")
                else:
                    reason = ""
                if not reason:
                    break
                self.declare_non_trial_event(
                    "rejected-proposal", reason,
                    controlId=control_id, candidateId=candidate_id)
                if not self._eligible_remaining() or not self._may_reask(reason):
                    break
                self.agents.rejection_note = reason      # the model hears why (09-30)
                try:
                    pair, decision = self._next_decision()
                finally:
                    self.agents.rejection_note = ""
                if on_decision is not None:
                    on_decision(decision)
                if pair is None:
                    break
            if (pair is None or reason) and self._past_deadline():
                break                                   # B ran out: DEADLINE
            if pair is None or reason:
                remaining = self._eligible_remaining()
                self._end(CATALOG_EXHAUSTED if not remaining else PROPOSAL_FAILURE,
                          "no eligible candidate remains in the frozen catalog"
                          if not remaining else
                          f"no executable proposal while {remaining} candidates were "
                          f"still eligible: {reason}")
                break
            if self._past_deadline(reserve_ms):
                break
            target_id, control_id = pair
            candidate_id = self.catalog_of_control.get(control_id)
            before_trial = self.applied_configuration()
            trial = self._run_trial(target_id, control_id, candidate_id, decision, hold_polls, plan)
            if trial is None:
                break
            if on_trial is not None:
                on_trial(trial.to_record())
            gone = self._unaddressable_ues()
            if gone:
                self._record_disconnect(gone, trial)
            if trial.kernel.get("terminalState") == "INCIDENT_LOCKDOWN":
                # Only a control whose context vanished is explained by the disconnect: if
                # this trial changed an axis of a UE that is still here, that UE's recovery
                # is unverified and the lockdown stands.
                touched = sorted({axis.partition("@")[2] for axis, value in trial.configuration.items()
                                  if str(before_trial.get(axis)) != str(value)})
                if not gone:
                    # The radio is ahead of the identity file: give it the few seconds the
                    # runner needs to republish before calling this an execution failure.
                    gone = self._settle_identity_evidence(self.identities, reserve_ms)
                    if gone:
                        self._record_disconnect(gone, trial)
                # A UE the trial changed is answered for by its own axes: read back at the
                # baseline means its control was undone whatever the combined read said.
                reverted = self._reverted_axes(trial, before_trial)
                unverified = sorted({axis.partition("@")[2] for axis, observed in reverted.items()
                                     if observed is None
                                     or str(observed) != str(before_trial.get(axis))})
                present = [ue for ue in unverified if ue not in gone]
                if not gone or present:
                    self._end(EXECUTION_FAILURE,
                              "recovery was not verified; the Kernel locked down the trial"
                              + (f" ({', '.join(present)} changed by the trial and still "
                                 "registered, so a disconnect does not explain it)"
                                 if gone and present else ""))
                    break
                entry = self._record_disconnect(gone, None)["trials"][-1]
                entry.update(excused=True, touchedUes=touched, revertedAxes=dict(reverted),
                             goneUes=list(gone))
                # The recovery could not be read because the UE it addressed is gone
                # (docs/design/ue-identity-continuity.md): a hardware disconnect, not an
                # execution result.  The locked case is retired by the rebind, and the
                # new case verifies the live configuration by readback before it writes.
                if not self._resume_after_disconnect(gone, reserve_ms):
                    break
                self.last_execution_failure = None
                continue
            # Reply 2026-09-14 section 7.  One execution-path failure can still
            # be this candidate's own; the *same* one again, on a candidate the
            # frozen catalog admits only once and which is therefore a different
            # configuration, is a prerequisite every candidate shares.  Asking
            # the model to choose six more configurations that all need the same
            # unavailable baseline is what burned the budget, so the episode
            # returns here through the common stop path instead.
            signature = None if gone else _execution_path_failure(trial)
            if signature is not None and signature == self.last_execution_failure:
                outcome, stop = signature
                self._end(EXECUTION_FAILURE,
                          f"{control_id} failed to execute the same way as the "
                          f"candidate before it ({outcome}"
                          + (f"/{stop}" if stop else "")
                          + "); the prerequisite this needs is unavailable to "
                            "every candidate, so the remaining budget would buy "
                            "the same failure again")
                break
            self.last_execution_failure = signature
            # A calibration walks its whole list: a met target must not cut the
            # measurement short (v4.7 plan step 1).
            if trial.success.get(self.contract.t0.target_id) and not _calibrating():
                self._end(T0_SUCCESS, f"{control_id} meets every original requirement")
                break
            if (any(trial.success.values()) and not self.request.continue_after_relaxed_success
                    and not _calibrating()):
                self._end(OPERATOR_STOP,
                          "a relaxed target was met and this sitting was configured to "
                          "stop there")
                break
            if self.stopped:
                self._end(OPERATOR_STOP, "stopped by the operator")
                break
        self._retain(hold_polls, plan)
        try:
            self.kernel_termination = self.runtime.terminate().value
        except KernelRefusal as exc:
            self.kernel_termination = None
            self.preflight["kernelTerminationRefusal"] = str(exc)
        self._observe_to_horizon(int(plan.cadence_ms))
        if self.kernel_termination is None and self.termination == DEADLINE:
            # The case deadline is B less one trial's reserve as it stood at case open,
            # so a search that stopped on B has normally already reached it.  Should
            # the reserve have been read differently, the horizon (H >= B, moved by the
            # same exempt time) is past it by now: ask once more rather than leave the
            # Kernel's terminal column blank.
            try:
                self.kernel_termination = self.runtime.terminate().value
                self.preflight["kernelTerminationFirstRefusal"] = self.preflight.pop(
                    "kernelTerminationRefusal", None)
            except KernelRefusal as exc:
                self.preflight["kernelTerminationRefusal"] = str(exc)
        self.ended_ms = float(self.clock.monotonic_ms())
        self.timing["end"] = self.clock.now()
        return self.summary()

    def _observe_to_horizon(self, cadence_ms: int) -> None:
        """Watch the service until ``t0 + H``, after the search has stopped.

        The search ends at a stated reason (T0, the budget, the catalog, B, the
        operator).  The *service* does not: ``exp_metrics.md`` section 4 scores
        the deficit over the whole ``[t0, t0 + H]``, post-search operation
        included, so what the deployment is left holding is measured for as
        long as it is claimed to serve.  Nothing here is a trial: no Kernel
        permit, no write, only the observer and the clock.
        """
        horizon_ms = self.request.horizon_ms
        if horizon_ms is None:
            return
        # A board the hardware ended has no service left to score: the UEs it would
        # observe are the ones that are gone.  Board 471 (09-24) sat here eight more
        # silent minutes after HARDWARE_UNAVAILABLE -- H plus the excised 300 s --
        # racing the keeper's 15-minute idle reaper.
        if self.termination == HARDWARE_UNAVAILABLE:
            self.preflight["horizonStoppedEarly"] = {
                "atMs": round(self._elapsed_ms(), 3), "horizonMs": int(horizon_ms),
                "reason": "hardware-unavailable"}
            return
        cadence = max(1, int(cadence_ms))
        last_note = float(self.clock.monotonic_ms())
        samples = 0
        due = float(self.clock.monotonic_ms()) + cadence
        # H is service time, and our-side interruptions are not service time: the
        # horizon moves by exactly what B was relieved of (owner 2026-09-23).  Before,
        # a 173 s wait for a re-registered UE left this loop with zero samples.
        while self._elapsed_ms() - self._hardware_wait_ms() < float(horizon_ms):
            if self.stopped or self._hardware_wait_ms() >= EXCISED_TOTAL_CAP_MS:
                self.preflight["horizonStoppedEarly"] = {
                    "atMs": round(self._elapsed_ms(), 3), "horizonMs": int(horizon_ms),
                    "reason": "operator" if self.stopped else "excision-cap"}
                break
            self._sleep_to(due)
            self._sample_service_trace()
            due += cadence
            samples += 1
            # A line a minute, so a long horizon is not mistaken for a stalled board.
            if float(self.clock.monotonic_ms()) - last_note >= 60_000.0:
                last_note = float(self.clock.monotonic_ms())
                print(f"horizon            : {(self._elapsed_ms() - self._hardware_wait_ms()) / 1000:.0f} s "
                      f"of {int(horizon_ms) / 1000:.0f} s of service observed after the search "
                      "stopped", flush=True)
        self.preflight["horizonObservation"] = {
            "horizonMs": int(horizon_ms), "samples": samples,
            "elapsedMs": round(self._elapsed_ms(), 3),
            "cadenceMs": cadence}
        if samples:
            self.declare_non_trial_event(
                "sample", f"{samples} service samples over the horizon after the "
                          "search stopped; no permit, no write, no candidate spent",
                samples=samples)

    def _initial_measurement(self) -> Optional[Trial]:
        """Measure what is already applied, once, as **trial 0**.

        ``exp_metrics.md`` section 3: "A common initial failed measurement is
        trial 0, excluded from recovery trial counts.  If valid for the current
        episode, score it against targets authorized at t0; supported success
        is recorded at trial 0 and time 0 for every method."  So it is judged
        by exactly the same predicates as a live trial and it is a column of
        the same board -- what it is not is an *admitted live evaluation*: the
        Kernel is not asked for anything, nothing is written, no candidate of
        the frozen catalog is spent, and it is ``counted: false``.
        """
        applied = self.applied_configuration()
        control_id = next((candidate.control_id for candidate in self.controls.candidates
                           if candidate.configuration == applied), "C0")
        started = self.clock.now()
        rows: List[Dict[str, Any]] = []
        polls = max(1, -(-int(self.rules.hold_ms()) // max(1, int(self._cadence_ms))))
        due = float(self.clock.monotonic_ms()) + float(self._cadence_ms)
        for _index in range(polls):
            self._sleep_to(due)
            rows.append(self._sample_service_trace())
            due += float(self._cadence_ms)
        window_end = self.clock.now()
        kpis, unknown, coverage, cohorts = self._aggregate_window(rows, window_end)
        trial = Trial(
            trial_index=INITIAL_TRIAL_INDEX, proposed_target_id=self.contract.t0.target_id,
            control_id=control_id, configuration=dict(applied),
            catalog_candidate_id="", decision_latency_ms=0.0, applied_at=started,
            window=self._window_record(started, window_end, rows, kpis, unknown,
                                       coverage, cohorts),
            kpis=kpis,
            kernel={"trialId": None, "terminalState": None, "outcome": None,
                    "stopReason": None, "evidenceStatus": None,
                    "detail": "the common initial measurement asks the Kernel for "
                              "nothing: it measures the configuration already applied"},
            rolled_back=False,
            decision={"role": "initial-measurement", "model": "deterministic",
                      "phase": "initial-measurement", "fallbackReason": None,
                      "repairRetries": 0, "targetId": self.contract.t0.target_id,
                      "controlId": control_id, "decisionLatencyMs": 0.0,
                      "rationale": "the episode's common initial measurement, scored "
                                   "against the targets authorized at t0"},
            # 기본: 모든 방식에 시각 0 -- 문서가 기준 관측을 elapsed 0 으로 고정했다.
            # `formal_reference_trial` 이면 **실제 경과**를 적는다: 이 관측도 준비 뒤에
            # 일어나고 관측 구간을 쓰므로, 0 으로 적으면 준비와 측정 시간이 사라진다
            # (실측: T0_MET 8판의 창 종료가 t0 에서 23~97초 뒤, 중앙 76.9초).
            elapsed_ms=self._elapsed_ms() if self.request.formal_reference_trial else 0.0)
        trial.judge_against(self.evaluation_contract, self.intents)
        # 기본은 세지 않는다(문서).  정식 기준 시행이면 `N_max` 의 한 칸을 쓴다 --
        # 공통 실행자가 확인된 기준 설정을 파견하는 것이고, 무변경이면 그것도 기록한다.
        self.trial_flags.setdefault(INITIAL_TRIAL_INDEX, {})["counted"] = bool(
            self.request.formal_reference_trial)
        self.initial_trial = trial
        self.grid.record(trial)
        self._seed_retention_from_initial(trial)
        self.calibrate_predictor()
        if trial.success.get(self.contract.t0.target_id) and not _calibrating():
            # **제어가 만든 해결이 아니다.**  공통 실행자가 이미 적용된 설정을 잰
            # 결과이므로, 이 결말은 LLM 의 제어 결정에 귀속되지 않는다.
            self._end(T0_SUCCESS,
                      "initially_T0_satisfied: the common initial measurement already "
                      "meets every original requirement before any control was applied"
                      + ("" if self.request.formal_reference_trial else
                         "; the success is recorded at trial 0 and elapsed 0"))
        return trial

    def _disconnect_trial_ids(self) -> set:
        """Lockdowns a hardware disconnect explains: every control the trial changed was on a
        UE whose context vanished (see the lockdown branch of :meth:`run`)."""
        return {str(entry.get("trialId")) for item in self.hardware_disconnects
                for entry in item.get("trials", ()) if entry.get("excused")}

    def _trials_used_total(self) -> int:
        """Trials across every case of this sitting; a rebind is not a budget reset."""
        return (sum(int(item.trials_used()) for item in self.retired_runtimes)
                + int(self.runtime.trials_used()))

    def _spent_elsewhere(self) -> set:
        """Candidates a retired case already admitted: the catalog is keyed by UE label,
        so the same candidate id on the rebound case is the same configuration."""
        return {candidate_id for item in self.retired_runtimes
                for candidate_id in item.spent_ids()}

    def _all_kernel_trials(self) -> List[Any]:
        return [record for item in (*self.retired_runtimes, self.runtime)
                for record in (getattr(item, "trials", ()) or ())]

    def _unaddressable_ues(self) -> List[str]:
        """UEs of this case the network no longer gives the id the case addresses.

        A numeric UE label is its own id, so only role-labelled sittings can see one.
        """
        if self.amf_of is None:
            return []
        return [ue for ue, observation in self.identities.items()
                if self.amf_of(ue) != int(observation.amf_ue_ngap_id)]

    #: How long a lockdown waits for the identity evidence to catch up with the radio.
    #: The runner republishes the role identity from KPM every 2 s under a 4 s freshness
    #: bound, so a UE that vanished during the trial that locked down is still listed for
    #: a few seconds afterwards (2026-09-16 attempt 109: ue3 left KPM at 09:51:00 and the
    #: rollback read failed at 09:51:02, with the file still naming it).
    IDENTITY_SETTLE_MS = 12000

    def _settle_identity_evidence(self, touched: Sequence[str], reserve_ms: float) -> List[str]:
        """Poll until a UE this trial changed is seen unaddressable, or the evidence settles."""
        if self.amf_of is None:
            return []
        limit = float(self.clock.monotonic_ms()) + float(self.IDENTITY_SETTLE_MS)
        while True:
            gone = [ue for ue in self._unaddressable_ues() if ue in touched]
            if gone or float(self.clock.monotonic_ms()) >= limit:
                return gone
            if self.stopped or self._past_deadline(reserve_ms):
                return []
            self.clock.sleep_ms(1000)

    def _reverted_axes(self, trial: Trial, baseline: Mapping[str, Any]) -> Dict[str, Any]:
        """What the gateway last read for each axis this trial changed, from its own evidence.

        A combined read fails as a whole when any participant cannot observe, so an axis
        whose own adapter did read its baseline back is verified even then (2026-09-16
        attempt 109: ue2's cap read 0 while ue3, which the trial never touched, had left
        the stream).  Only the adapters' own observations are used; nothing is assumed.
        """
        observed: Dict[str, Any] = {}
        gateway = self.runtime.gateway
        for reference in gateway.evidence_references():
            if f":{trial.kernel.get('trialId')}:" not in reference and \
                    str(trial.kernel.get("trialId") or "") not in reference:
                continue
            record = dict(gateway.evidence(reference))
            if record.get("operation") != "READ":
                continue
            for axis, value in dict(record.get("observedConfig") or {}).items():
                observed[str(axis)] = str(value)
        return {axis: observed.get(axis) for axis, value in trial.configuration.items()
                if str(baseline.get(axis)) != str(value)}

    def _record_disconnect(self, ues: Sequence[str], trial: Optional[Trial]) -> Dict[str, Any]:
        """Open (or extend) the disconnect record for these UEs; evidence only."""
        open_ = next((item for item in reversed(self.hardware_disconnects)
                      if item.get("reregistered") is None
                      and set(item["ues"]) == set(ues)), None)
        if open_ is None:
            open_ = {"ues": {ue: {"previousAmfUeNgapId": int(self.identities[ue].amf_ue_ngap_id)}
                             for ue in ues},
                     "detectedAt": self.clock.now(), "reregistered": None,
                     "caseId": self.runtime.case_id, "trials": []}
            self.hardware_disconnects.append(open_)
        if trial is not None:
            open_["trials"].append({
                "trialIndex": trial.trial_index, "trialId": trial.kernel.get("trialId"),
                "terminalState": trial.kernel.get("terminalState"),
                "controlId": trial.control_id, "configuration": dict(trial.configuration),
                "appliedAt": trial.applied_at})
        return open_

    #: Longest one wait for a UE the radio dropped (:data:`HARDWARE_WAIT_CAP_MS`).  B is
    #: not charged for the wait (the owner's 2026-09-16 instruction), so without a bound
    #: of its own a UE that never returns would be ended by the runner's
    #: AIC_EPISODE_CAP_S, which kills the board before its record is written.
    HARDWARE_WAIT_CAP_MS = HARDWARE_WAIT_CAP_MS

    def _our_side_wait_over(self, started_ms: float, what: str) -> bool:
        """Has an our-side wait run past its cap?  One check for every such wait.

        Owner 2026-09-23 ("너무 많이 기다리지 마라"): one wait at most
        :attr:`HARDWARE_WAIT_CAP_MS`, one board at most :data:`EXCISED_TOTAL_CAP_MS`
        excised; either ends the board ``HARDWARE_UNAVAILABLE`` (kept, not discarded).
        It also prints one progress line a minute, so a long wait is not mistaken
        for a hung runner and reaped (board 470: 16 min without a line).
        """
        waited = float(self.clock.monotonic_ms()) - float(started_ms)
        total = self._hardware_wait_ms()
        minute = int(waited // 60_000)
        if minute and (started_ms, minute) != getattr(self, "_wait_note", None):
            self._wait_note = (started_ms, minute)
            print(f"waiting            : {what}, {round(waited / 1000)} s of "
                  f"{round(self.HARDWARE_WAIT_CAP_MS / 1000)} s; board excised "
                  f"{round(total / 1000)} s of {round(EXCISED_TOTAL_CAP_MS / 1000)} s",
                  flush=True)
        if waited >= self.HARDWARE_WAIT_CAP_MS:
            self._end(HARDWARE_UNAVAILABLE,
                      f"{what} was awaited for {round(waited / 1000)} s, past the "
                      f"{round(self.HARDWARE_WAIT_CAP_MS / 1000)} s one wait may take")
            return True
        if total >= EXCISED_TOTAL_CAP_MS:
            self._end(HARDWARE_UNAVAILABLE,
                      f"our-side interruptions add up to {round(total / 1000)} s, past the "
                      f"{round(EXCISED_TOTAL_CAP_MS / 1000)} s one board may excise")
            return True
        return False

    def _await_reregistration(self, ues: Sequence[str], reserve_ms: float) -> bool:
        """Wait until every UE has an id again; the gap is missing, not a result.

        B is relieved of the wait while it lasts, so what ends it is the operator, the
        per-wait cap (:attr:`HARDWARE_WAIT_CAP_MS`) or the board's cumulative cap
        (:data:`EXCISED_TOTAL_CAP_MS`, checked by :meth:`_past_deadline`) -- the last
        two as :data:`HARDWARE_UNAVAILABLE`, never as ``DEADLINE``: B did not run out.
        """
        record = self._record_disconnect(ues, None)
        started = float(self.clock.monotonic_ms())
        prior = float(record.get("waitedMs") or 0.0)
        wait = {"start": self.clock.now(), "end": None, "reasons": ["ue-disconnect"]}
        record.setdefault("waits", []).append(wait)

        def waited() -> float:
            return float(self.clock.monotonic_ms()) - started

        while any(self.amf_of(ue) is None for ue in ues):
            # Kept current while waiting: the deadline is relieved of exactly this
            # number (``_hardware_wait_ms``), and a figure written only once the wait
            # ended relieved nothing *during* it -- board 462 (2026-09-23) gave up
            # after 33 s on a B that was being charged for the wait itself, and the
            # steered UE came back 78 s later.
            record["waitedMs"] = prior + waited()
            wait["end"] = self.clock.now()
            if self.stopped:
                self._end(OPERATOR_STOP, "stopped by the operator while a disconnected UE was awaited")
            if not self.stopped:
                self._our_side_wait_over(started, f"a hardware disconnect of {', '.join(ues)}")
            if (self.stopped or self.termination is not None
                    or self._past_deadline(reserve_ms)):
                record.update(reregistered=False, waitedMs=prior + waited())
                wait["end"] = self.clock.now()
                return False
            self.clock.sleep_ms(1000)
        record.update(reregistered=True, reregisteredAt=self.clock.now(),
                      waitedMs=prior + waited())
        wait["end"] = self.clock.now()
        for ue in ues:
            record["ues"][ue]["amfUeNgapId"] = self.amf_of(ue)
        return True

    def _resume_after_disconnect(self, ues: Sequence[str], reserve_ms: float) -> bool:
        """After a lockdown a disconnect caused: wait for the UEs, then rebind onto a new case."""
        if int(self.request.budget_trials) - self._trials_used_total() <= 0:
            return True  # nothing left to try: the loop's budget check ends the search
        if not self._await_reregistration(ues, reserve_ms):
            return False
        before = len(self.identity_rebinds)
        if not self._rebind_if_reregistered():
            return False    # the rebind never succeeded; the sitting has its stated reason
        rebound = (len(self.identity_rebinds) > before
                   and self.identity_rebinds[-1].get("outcome") == "REBOUND")
        if not rebound:
            # The lockdown was excused on the premise that the UE's context vanished; a UE
            # back under the same id, or one that could not be rebound, does not bear it out.
            for item in self.hardware_disconnects:
                for entry in item.get("trials", ()):
                    if entry.get("excused") and set(item["ues"]) >= set(ues):
                        entry.update(excused=False, excuseRevoked=(
                            "same-id" if len(self.identity_rebinds) == before else "not-rebound"))
            self._end(EXECUTION_FAILURE,
                      "recovery was not verified; the Kernel locked down the trial and "
                      + ("the disconnected UE came back under the same id, so the lockdown "
                         "stands" if len(self.identity_rebinds) == before else
                         "the re-registered UE could not be rebound: "
                         + str(self.identity_rebinds[-1].get("detail"))))
            return False
        return True

    def _rebind_if_reregistered(self, force: Optional[str] = None) -> bool:
        """Between trials: a UE the network re-registered under a new id is addressed anew.

        Nothing is judged across the gap; the new case opens on what the radio
        holds now, with the re-registered UE's own axes at their baseline (its
        context, and any control on it, went with the old id).

        ``force`` recomposes onto a fresh case even when no id changed -- the one
        use is a Kernel case whose deadline ran out while B still had time, because
        B was relieved of our-side interruptions the open case could not know of.

        Returns ``False`` only when the sitting must not go on: the re-registered UE
        could not be rebound and the wait for it ended (:data:`HARDWARE_UNAVAILABLE`,
        the operator, or B).  A trial is never fired on the old case at an id the
        network no longer gives -- 2026-09-19..23 did exactly that after NOT_REBOUND.
        """
        if self.rebind_factory is None or self.amf_of is None:
            return True
        changed = [ue for ue, observation in self.identities.items()
                   for amf in (self.amf_of(ue),)
                   if amf is not None and int(amf) != int(observation.amf_ue_ngap_id)]
        remaining = int(self.request.budget_trials) - self._trials_used_total()
        if (not changed and force is None) or remaining <= 0:
            return True  # with nothing left to try, the budget check ends the search
        applied = dict(self.applied_configuration())
        for axis in list(applied):
            ue = axis.partition("@")[2]
            if ue in changed and axis in self.baselines:
                applied[axis] = str(self.baselines[axis])
        record: Dict[str, Any] = {
            "at": self.clock.now(), "index": len(self.identity_rebinds) + 1,
            "reason": force or "re-registration",
            "ues": {ue: {"previousAmfUeNgapId": int(self.identities[ue].amf_ue_ngap_id),
                         "previousObservedAt": self.identities[ue].observed_at}
                    for ue in changed},
            "trialsBefore": self._trials_used_total(), "retiredCase": self.runtime.case_id}
        # The re-registered UE is registered but not yet observable under its new id
        # (the observation window is ~10 s).  Waiting for it is a hardware wait like
        # any other: charged to no clock, bounded by the same caps.
        started = float(self.clock.monotonic_ms())
        disconnect: Optional[Dict[str, Any]] = None
        wait: Dict[str, Any] = {}
        prior = 0.0
        failures: List[str] = []
        #: What the recomposition itself already charged as a composition wait
        #: (``_await_addressable`` inside the factory); not charged twice.
        charged_elsewhere = 0.0

        def waited_here() -> float:
            return float(self.clock.monotonic_ms()) - started - charged_elsewhere

        while True:
            before = float(self.composition_wait_ms)
            try:
                result = self.rebind_factory(dict(self.identities), changed, applied,
                                             record["index"], remaining_trials=remaining)
                break
            except Exception as exc:  # noqa: BLE001 - the record carries it
                failures.append(f"{type(exc).__name__}: {exc}"[:500])
            finally:
                charged_elsewhere += float(self.composition_wait_ms) - before
            waited = waited_here()
            if changed and disconnect is None:
                disconnect = self._record_disconnect(changed, None)
                disconnect["cause"] = "rebind-failed"
                prior = float(disconnect.get("waitedMs") or 0.0)
                wait = {"start": self.clock.now(), "end": None, "reasons": ["rebind-retry"]}
                disconnect.setdefault("waits", []).append(wait)
            if disconnect is not None:
                disconnect["waitedMs"] = prior + waited
                wait["end"] = self.clock.now()
            if not changed:
                pass                               # a forced rollover is tried once
            elif self.stopped:
                self._end(OPERATOR_STOP, "stopped by the operator while a rebind was retried")
            elif waited >= HARDWARE_WAIT_CAP_MS:
                self._end(HARDWARE_UNAVAILABLE,
                          f"{', '.join(changed)} re-registered but could not be rebound "
                          f"within {round(HARDWARE_WAIT_CAP_MS / 1000)} s: {failures[-1]}")
            elif not self._past_deadline():
                self.clock.sleep_ms(REBIND_RETRY_MS)
                continue
            record["outcome"] = "NOT_REBOUND"
            record["detail"] = failures[-1]
            record["attempts"] = len(failures)
            self.identity_rebinds.append(record)
            if disconnect is not None:
                disconnect["reregistered"] = False
            return False
        if failures:
            record["attempts"] = len(failures) + 1
            record["failedAttempts"] = failures
        if disconnect is not None:
            disconnect["waitedMs"] = prior + waited_here()
            wait["end"] = self.clock.now()
        try:
            record["retiredKernelTermination"] = self.runtime.terminate().value
        except KernelRefusal as exc:
            record["retiredKernelTermination"] = None
            record["retiredTerminationRefusal"] = str(exc)
        self.retired_runtimes.append(self.runtime)
        self.runtime = result["runtime"]
        self.identities = dict(result["identities"])
        self.policy_builders = list(self.policy_builders) + list(result.get("builders") or ())
        if result.get("supplementary"):
            # The retired participants' readback decisions are evidence; their stream view is not.
            record["retiredSupplementary"] = [{
                "adapterKey": item.adapter_key, "axis": item.axis,
                "expectedAttribution": item.expected.to_record(),
                "readbackLog": [dict(entry) for entry in item.counter_reader.reads]}
                for item in self.supplementary if hasattr(item, "counter_reader")]
            for item in self.supplementary:
                close = getattr(item, "close", None)
                if callable(close):
                    close()
            self.supplementary = tuple(result["supplementary"])
        for ue in changed:
            record["ues"][ue].update(
                amfUeNgapId=int(self.identities[ue].amf_ue_ngap_id),
                servingNci=int(self.identities[ue].serving_nci),
                observedAt=self.identities[ue].observed_at)
        for item in self.hardware_disconnects:
            if item.get("reregistered") is None and set(item["ues"]) <= set(changed):
                # A retried rebind already wrote how long it waited; keep it.
                item.update(reregistered=True, reregisteredAt=record["at"],
                            waitedMs=float(item.get("waitedMs") or 0.0))
                for ue in item["ues"]:
                    item["ues"][ue]["amfUeNgapId"] = int(self.identities[ue].amf_ue_ngap_id)
        record.update(outcome="REBOUND", case=self.runtime.case_id,
                      initialConfiguration=dict(result.get("initialConfiguration") or applied),
                      freed={ue: len(items) for ue, items in dict(result.get("freed") or {}).items()})
        self.identity_rebinds.append(record)
        return True

    def _available(self, candidate_id: str) -> bool:
        if str(candidate_id) in self._spent_elsewhere():
            return False
        entry = self.runtime.candidate_entry(str(candidate_id))
        return entry is not None and str(entry.availability.value) == "AVAILABLE"

    @staticmethod
    def _axes_needing_withdrawal(wanted: Mapping[str, str],
                                 applied: Mapping[str, str]) -> Tuple[str, ...]:
        """Axes retention could only restore by *withdrawing* a live policy.

        A supplementary axis rests at a sentinel -- ``dlPrbCap`` at ``"0"``,
        ``pfWeight`` at ``"1.0"`` -- that is a baseline and a restore value,
        never an applied candidate: ``action102_support.wire_value`` refuses it
        before a policy body exists, and the contract keeps it outside the APPLY
        domain on purpose.  The equipment leaves that sentinel by *creating* a
        policy and returns to it by *withdrawing* one, which is an UNDO the
        gateway only issues while the trial that applied it is still live
        (``kernel.issue_token`` grants no STOP after ``SETTLED_SUCCESS``).

        So when the retained control asks an axis back to its sentinel and the
        deployment is holding something else there, re-applying is not merely
        unavailable -- it is inexpressible, and attempting it spends a permit to
        earn a ``VALIDATE failed`` that reads like a defect.  Naming the axes
        here lets the record say what is actually live instead, which is what
        ``exp_metrics.md`` section 2 asks ``retained`` to report.

        Moving a held axis to another *non*-sentinel value used to be counted
        here for the same reason: 2026-09-15 attempt 45 settled dlPrbCap@ue3 18,
        then proposed 12 and 6 on it, and both commits were REJECTED because
        the settled binding still owned the (policy type, scope).  That is no
        longer true.  2026-09-16 measured the cost of treating it as blocked --
        every episode of that day which ended CATALOG_EXHAUSTED or
        KERNEL_TERMINATED did so on one of these rejections, eight in all and
        every one on a @ue1 axis -- and the adapter now lets a later
        transaction *adopt* the policy a finished one left live
        (``R1PolicyAdapter._adopt_takeable_policy``), which the producer takes
        as an in-place update.  So a non-sentinel move is expressible again and
        only the return to the sentinel, which needs a DELETE no permit
        expresses after a settlement, stays blocked.
        """
        blocked = []
        for axis, value in sorted(wanted.items()):
            action_id = SUPPLEMENTARY_BY_KIND.get(axis_kind(axis))
            if action_id is None:
                continue
            declared = SUPPLEMENTARY_ACTIONS[action_id]
            sentinel = str(declared.baseline)
            held = str(applied.get(axis, sentinel))
            if held == sentinel or str(value) != sentinel:
                continue
            # A sentinel is not automatically inexpressible.  Two of this
            # deployment's axes rest at one and they are not alike:
            #
            #   dlPrbCap "0"   wire_value refuses it ("an applied cap is 5..24
            #                  PRB"), so the equipment can only get there by
            #                  withdrawing -- genuinely blocked.
            #   pfWeight "1.0" wire_value accepts it.  A weight of one IS a
            #                  setting, and the takeover writes it into the
            #                  live policy like any other value.
            #
            # Treating them alike marked whole catalogs dead: after one trial
            # settled pfWeight@ue2, all 11 candidates of 2026-09-17's episode
            # 2c70f79d came back eligible=False and the sitting ended
            # CATALOG_EXHAUSTED with one counted trial.  The live record
            # disproves that blanket rule -- episode 2c70f79d's own retention
            # applied pfWeight@ue2 4.0 -> 1.0 and finalized (partialApply
            # false, CLOSED_PASS).  So ask the axis instead of assuming.
            if _sentinel_is_applicable(declared):
                continue
            blocked.append(axis)
        return tuple(blocked)

    def _past_deadline(self, reserve_ms: float = 0.0) -> bool:
        """Has the search deadline B passed, or can it no longer hold ``reserve_ms``?

        ``exp_metrics.md`` section 1 gives B and H two different jobs: **B** is
        the deadline the search runs under, and **H** is the *service* horizon
        (section 4: "observe actual service throughout [t0, t0 + H], including
        reasoning, settling, rollback, and post-search operation"), with
        ``H >= B``.  So H must not end the sitting -- it keeps the observation
        running after the search has stopped, which is exactly the interval the
        deficit bins are computed over.

        ``reserve_ms`` is what the next step needs before B -- an observation
        hold plus its trailing collection -- so a trial that could not finish
        its judged observation inside B is never started.

        What B charges is the search, not the radio.  The owner's 2026-09-16
        instruction -- "the total time limit: the breakage is purely the
        hardware's fault, so just wait" -- was first met by dropping B
        entirely, which left the 480 s attainment curves with no clock to be
        drawn against.  The owner's decision of the same day is this instead:
        keep B at 480 s and relieve it of the hardware wait, exactly as
        :meth:`_formation_overrun` already relieves the formation allowance.
        The relief is :meth:`_hardware_wait_ms`, which is read off the
        disconnect records, so the seconds forgiven here are the same seconds
        ``hardwareWaitMs`` reports.
        """
        exempt = self._hardware_wait_ms()
        if exempt >= EXCISED_TOTAL_CAP_MS:
            # 오너 지시(2026-09-23): 시간을 빼 주는 대신 너무 오래 기다리지 않는다.
            self._end(HARDWARE_UNAVAILABLE,
                      f"our-side interruptions add up to {round(exempt / 1000)} s, past the "
                      f"{round(EXCISED_TOTAL_CAP_MS / 1000)} s one board may excise; the bed "
                      "is not in a state to run this board")
            return True
        spent = self._elapsed_ms() - exempt
        for name, budget_ms in (("deadlineMs", self.request.deadline_ms),):
            if budget_ms is None:
                continue
            if spent >= float(budget_ms):
                self._end(DEADLINE, f"the sitting's {name} of {budget_ms} ms is spent")
                return True
            if reserve_ms > 0 and spent + float(reserve_ms) > float(budget_ms):
                self._end(DEADLINE, f"the sitting's {name} of {budget_ms} ms cannot hold "
                                    f"another observation and its trailing collection "
                                    f"({round(float(reserve_ms))} ms)")
                return True
        return False

    def _retain(self, hold_polls: int, plan: Any) -> None:
        """Leave the deployment holding the best attained control, or say why not.

        Searching and deploying are two different claims (``exp_metrics.md``
        section 2).  The *best attained* target is the one some trial reached;
        the *retained* one is what the deployment is actually left holding when
        the sitting ends.  They coincide only when the trial that attained it
        settled a success and was therefore not rolled back.  Otherwise the
        control has to be applied once more -- and the frozen catalog admits
        each candidate exactly once, so sometimes it cannot be, which this
        record says rather than claiming a deployment that did not happen.
        """
        # **Ω 에서 고른다.**  `success` 는 이제 Ω id 로 키가 되고, 선택된 T 의
        # alternatives 는 `from_compact` 에서 `T1..Tn` 으로 **재번호**되므로 같은 철자가
        # 서로 다른 요구 벡터다.  선택된 T 를 넘기면 `contract.target(target_id)` 가
        # T0 말고는 못 찾아 달성을 조용히 잃는다 (2026-09-23).
        best = self.grid.best_attained(self.evaluation_contract)
        control_id = best.control_id if best is not None else "C0"
        target_id = best.target_id if best is not None else self.contract.t0.target_id
        v5_attains = None
        if self._v5_order():
            # v5 (owner 2026-09-26; Codex review #1 of the v5 design): the retained control is
            # the common evaluator's best over the whole Omega -- the same p the selector and
            # the report use -- not the best column of this arm's T, which under v5 usually
            # attains nothing and fell back to C0 (boards 681, 684: "retained C0 for T0 ...
            # refused" while the report's best was trial 2 at p=11).
            from assurance.coordination.concession import evaluate_trials
            order = self._v5_order()

            def v5_attains(trial: Any) -> bool:
                # The evaluator's own validity rule (window and observation) and verdict.
                return bool(trial is not None and evaluate_trials(
                    self.intents, order, [trial], by_requirement=True,
                    weighted=self._v51())["trials"][0]["attained"])
            report = self._v5_online()["bestSoFar"] or {}
            supporting = next((trial for trial in self.grid.trials
                               if trial.trial_index == report.get("trialIndex")), None)
            if report.get("attained") and supporting is not None:
                control_id = supporting.control_id
                target_id = f"omega:p={report.get('p')}"
            else:
                control_id, target_id = "C0", self.contract.t0.target_id
        found: Dict[str, Any] = {"targetId": target_id, "controlId": control_id,
                                 "qualified": False, "detail": ""}
        excused = self._disconnect_trial_ids()
        if any(trial.kernel.get("terminalState") == "INCIDENT_LOCKDOWN"
               and trial.kernel.get("trialId") not in excused
               for trial in self.grid.trials):
            found["detail"] = "retention is blocked by an unresolved recovery incident"
            self.retained = found
            return
        gone = self._unaddressable_ues()
        if gone:
            found["detail"] = ("retention was not attempted: " + ", ".join(gone)
                               + " holds no id this case addresses (hardware disconnect)")
            self.retained = found
            return
        if getattr(self, "termination", None) == HARDWARE_UNAVAILABLE:
            found["detail"] = ("retention was not attempted: the bed stayed unavailable past "
                               "the waits one board may take (hardware disconnect)")
            self.retained = found
            return
        candidate = self.controls.candidate(control_id)
        if candidate is None:
            found["detail"] = f"{control_id} is not one of this sitting's controls"
            self.retained = found
            return
        if not self.request.retain_best:
            found["detail"] = ("this sitting only measured the search; retention was "
                               "not asked for (retain_best=False)")
            self.retained = found
            return
        applied = self.applied_configuration()
        wanted = {str(axis): str(value) for axis, value in candidate.configuration.items()}
        if applied == wanted:
            held = next((trial for trial in reversed(self.grid.trials)
                         if trial.control_id == control_id), None)
            found["qualified"] = (v5_attains(held) if v5_attains is not None
                                  else bool(held is not None and held.success.get(target_id)))
            found["trialIndex"] = None if held is None else held.trial_index
            found["detail"] = ("the trial that attained it settled and was not rolled "
                               "back, so the deployment already holds it")
            self.retained = found
            return
        # Retention used to stop here.  Restoring a supplementary axis to its
        # baseline IS a withdrawal, the Kernel grants the settled trial no STOP,
        # and so the deployment was left holding a control nobody had asked to
        # keep -- v4's first full episode ended "qualified=False ... first needs
        # that live policy withdrawn" on dlPrbCap@ue2.  The adapter can now
        # adopt a finished transaction's policy and withdraw it
        # (R1Adapter._adopt_takeable_policy on the UNDO path), so the retention
        # trial below expresses the restore like any other change.  The axes are
        # still recorded, because a reader wants to know the retention had to
        # withdraw rather than apply.
        withdrawal_only = self._axes_needing_withdrawal(wanted, applied)
        if withdrawal_only:
            found["withdrawalOnlyAxes"] = list(withdrawal_only)
            # Adoption made a *non-sentinel* move expressible again; it did not
            # make the sentinel one expressible, and enabling the takeover this
            # morning I deleted this return with the rest.  The retention then
            # applied C0's ``dlPrbCap@ue2 = "0"`` and earned exactly the
            # ``VALIDATE failed: an applied cap is 5..24 PRB ... got 0`` the
            # paragraph above says this guard exists to avoid -- a permit spent
            # on a refusal that reads like a defect (2026-09-17, d69fd188).
            # The restore is a DELETE and the trial that applied the policy has
            # settled, so no permit expresses it here; say what is live instead.
            found["detail"] = (
                "retention would have to withdraw a policy a settled trial left "
                "live on " + ", ".join(withdrawal_only) + ", which no permit "
                f"expresses after a settlement; the deployment is left holding {applied}")
            self.retained = found
            return
        candidate_id = self.catalog_of_control.get(control_id)
        search_runtime = self.runtime
        on_retention_case = False
        if candidate_id is None or not self._available(candidate_id):
            # The search case admits each candidate once, so the best control
            # -- rolled back, or simply superseded by the trials that followed
            # it -- is re-applied on a retention case of its own: same joint
            # contract, same participants and adapters, a fresh epoch, one
            # trial.  Without a factory (an injected runtime the composition
            # root did not describe) the record says so instead of pretending.
            if self.retention_factory is None:
                found["detail"] = (
                    f"{control_id} was superseded and the search case's frozen catalog "
                    f"admits each candidate once; no retention case could be composed, "
                    f"so the deployment is left holding {applied}")
                self.retained = found
                return
            try:
                self.retention_runtime = self.retention_factory(dict(applied))
            except Exception as exc:  # noqa: BLE001 - the record carries the reason
                found["detail"] = (f"the retention case could not be composed "
                                   f"({type(exc).__name__}: {exc}); the deployment is "
                                   f"left holding {applied}")
                self.retained = found
                return
            candidate_id = _candidate_id_for(self.retention_runtime, wanted)
            if candidate_id is None:
                found["detail"] = (f"the retention case's catalog has no candidate for "
                                   f"{wanted}; the deployment is left holding {applied}")
                self.retained = found
                return
            self.runtime = self.retention_runtime
            on_retention_case = True
        try:
            trial = self._run_trial(
                target_id, control_id, candidate_id,
                {"role": "retention", "model": "deterministic", "phase": "retention",
                 "fallbackReason": None, "repairRetries": 0,
                 "rationale": "the executor re-applies the best attained control so the "
                              "sitting leaves the deployment holding it",
                 "targetId": target_id, "controlId": control_id, "decisionLatencyMs": 0.0},
                hold_polls, plan, record_in_grid=False)
        finally:
            if on_retention_case:
                try:
                    self.retention_kernel_termination = self.retention_runtime.terminate().value
                except KernelRefusal as exc:
                    self.retention_kernel_termination = None
                    self.preflight["retentionTerminationRefusal"] = str(exc)
                self.runtime = search_runtime
        if trial is None:
            found["detail"] = "the Kernel refused the retention trial"
            self.retained = found
            return
        found["retentionCase"] = (self.retention_runtime.case_id if on_retention_case
                                  else self.case_id)
        found["qualified"] = (v5_attains(trial) if v5_attains is not None
                              else bool(trial.success.get(target_id)))
        found["trialIndex"] = trial.trial_index
        found["detail"] = ("re-applied at the end of the sitting and judged on its own "
                           "observation window")
        found["trial"] = trial.to_record()
        self.retained = found

    def _extend_hold(self, samples: List[Dict[str, Any]], needed: int, cadence_ms: int,
                     *, in_trial: bool = True) -> int:
        """Keep observing until ``needed`` usable samples, when some were excised.

        Owner 2026-09-23: an excised sample does not exist, so a judged window that
        lost some is extended by as many as it lost -- and the extension is not
        charged to B either (the rows carry ``holdExtension``).  It stops at the
        operator, or when the board's cumulative excision reaches its cap; the window
        is then judged on what it has, which the coverage rule may call ``UNKNOWN``.
        The collector keeps being polled through the runtime, so the Kernel's
        freshness bound still holds when the hold closes.
        """
        observe = (getattr(getattr(self.runtime, "path", None), "observe", None)
                   if in_trial else None)
        added = 0
        started = float(self.clock.monotonic_ms())
        while sum(1 for row in samples if not row.get("excised")) < needed:
            # 2026-09-24 board 470: only the board-wide cap was checked here, and since
            # 6e468b2d0 one continuous stretch counts at most HARDWARE_WAIT_CAP_MS toward
            # it -- a UE that never came back kept the refill going until the keeper
            # reaped the board 16 min later.  Both caps, one shared check.
            if self.stopped or self._our_side_wait_over(started, "a judged window's refill"):
                break
            if callable(observe):
                observe(ticks=1, tick_ms=int(cadence_ms))
            else:
                self.clock.sleep_ms(int(cadence_ms))
            samples.append(self._sample_service_trace(holdExtension=True))
            added += 1
        return added

    def _run_trial(self, target_id: str, control_id: str, candidate_id: str,
                   decision: Dict[str, Any], hold_polls: int, plan: Any,
                   *, record_in_grid: bool = True, _rolled: bool = False) -> Optional[Trial]:
        """Open, apply, observe, judge and record one trial.

        ``record_in_grid`` is off for the retention trial: it is a deployment,
        not a search step, and counting it as a column would change every
        search metric the episode reports.  Its Kernel trial, its gateway
        evidence and its events are in the record like any other.
        """
        candidate = self.controls.candidate(control_id)
        configuration = dict(candidate.configuration) if candidate is not None else {}
        samples: List[Dict[str, Any]] = []
        started = self.clock.now()
        stamps: Dict[str, str] = {}
        held: Dict[str, Any] = {}

        def on_poll(index: int) -> None:
            samples.append(self._sample_service_trace())
            if index == int(hold_polls):
                # The last hold poll: refill what an our-side interruption excised,
                # before the runtime closes the hold.
                self._extend_hold(samples, int(hold_polls), int(plan.cadence_ms))

        def on_phase(name: str) -> None:
            stamps.setdefault(name, self.clock.now())
            if name == "hold-end":
                # The judged window closes here; later polls are the trailing
                # collection's service samples, not part of it.
                held["rows"] = list(samples)
            elif name == "trailing-end":
                # Amendment 5: the runtime kept the judged configuration applied
                # for the longest deadline after the hold, so the issued cohort
                # is read now -- before FINALIZE/rollback -- with no extra wait.
                held["aggregate"] = aggregate(held.get("rows", samples), waited=True)
                held["attainsNothing"] = self._attains_no_target(
                    started, held["aggregate"], held.get("rows", samples))
                # 유지 판정은 **되돌림을 정하기 전에** 나와야 한다.  창은 여기서 이미
                # 집계돼 있고(FINALIZE 전), `conclude_stop_reasons` 는 이 뒤에 불린다.
                if self.request.retain_on_improvement:
                    held["retention"] = self._retention_for(held["aggregate"], configuration)

        def aggregate(rows: List[Dict[str, Any]], *, waited: bool = False,
                      kept: bool = True) -> Tuple[str, Any]:
            # FINALIZE/rollback can take longer than the measurement window.
            # Those operations do not collect service samples: the hold ends at
            # the last observation, not when its later execution receipt
            # arrives.  Keep validity anchored there too; completion latency is
            # still charged by elapsed_ms below.
            window_end = str(rows[-1]["t"]) if rows else self.clock.now()
            return window_end, self._aggregate_window(rows, window_end, held=kept,
                                                      waited=waited)

        try:
            trial_id, report = self.runtime.run_candidate(
                candidate_id, observation_polls=hold_polls, cadence_ms=plan.cadence_ms,
                settle_ms=min(int(plan.cadence_ms), 250),
                stop_requested=lambda: self.stopped, on_poll=on_poll, on_phase=on_phase,
                trailing_ms=int(self._trailing_ms()),
                conclude_stop_reasons=lambda: (
                    (StopReason.SEMANTIC_NON_SUCCESS,) if held.get("attainsNothing")
                    # 유지 모드: 판정이 `retain` 이면 되돌리지 않는다 -- 그 설정이 다음
                    # 시행의 복구 기준선이 된다(결정 §3.3).  아니면 종전대로 되돌린다.
                    else () if (self.request.retain_on_improvement
                                and (held.get("retention") or {}).get("retain"))
                    # 2026-09-23 (codex 감사 #4): 유지 모드에서 판정이 **거절**이면
                    # 결정 §3.2 가 "확인된 직전 설정으로 복원" 하라고 한다.  여기서
                    # `reset_each_trial` 에만 기대면 그 플래그가 꺼진 조합에서 더 나쁜
                    # 설정이 그대로 남고, 기록은 "유지 안 함" 이라 **기록과 무선이 어긋난다**.
                    # The retention trial is the one deployment that stays live.
                    else (StopReason.BASELINE_RESET,) if (
                        record_in_grid and (self.request.retain_on_improvement
                                            or self.request.reset_each_trial))
                    else ()))
        except KernelRefusal as exc:
            if (str(exc) == "CASE_DEADLINE_REACHED" and record_in_grid and not _rolled
                    and not self._past_deadline()):
                # The Kernel's case deadline is B as it stood when the case opened;
                # B has since been relieved of an our-side interruption the case
                # cannot know of.  Nothing was applied: roll onto a fresh case whose
                # deadline is what B holds now, and ask again once.
                if self._rebind_if_reregistered(force="kernel-deadline-rollover"):
                    return self._run_trial(target_id, control_id, candidate_id, decision,
                                           hold_polls, plan, record_in_grid=record_in_grid,
                                           _rolled=True)
            if self.termination is not None:
                return None
            # Never admitted, so never an admitted live evaluation: the
            # proposal is a charged non-trial event, not a trial that failed.
            self.declare_non_trial_event(
                "rejected-proposal",
                f"the Kernel refused {control_id} on {candidate_id}: {exc}",
                controlId=control_id, candidateId=candidate_id)
            self._end(KERNEL_TERMINATED, f"the Kernel refused another trial: {exc}")
            return None
        if "aggregate" not in held:
            # The trailing collection did not complete with the candidate still
            # applied (an emergency stop, or nothing was applied): an issued
            # cohort read now would expire under the restored baseline, so it
            # is not read.
            held["aggregate"] = aggregate(held.get("rows", samples), kept=False)
        window_end, (kpis, unknown, coverage, cohorts) = held["aggregate"]
        rows = held.get("rows", samples)
        window = self._window_record(started, window_end, rows, kpis, unknown,
                                     coverage, cohorts)
        recovery = dict(self.runtime.trial_recovery(trial_id))
        terminal = report.terminal_state.value
        # 2026-09-23 (codex 감사 #7): 복구를 **요청했는지**를 `applyCounted` 로 재고 있었다.
        # 실측 판(formal38guarded-20260923T060007)에서 커널은 REVERSE_ROLLBACK 을 거쳐
        # INCIDENT_LOCKDOWN 으로 갔는데, 적용이 확인되지 않아 `applyCounted` 가 거짓이라
        # 기록은 `requested=false · restored=null · unresolved=false` 였다 -- 실제로는
        # 미해결 복구인 판이 기록상 복구가 필요 없던 판으로 남는다.  잠금은 **커널의
        # 전이**이지 적용 확인의 함수가 아니다.
        locked_down = (terminal == "INCIDENT_LOCKDOWN"
                       or recovery["resolution"] == "INCIDENT_LOCKDOWN")
        requested = ((bool(recovery["applied"]) or locked_down)
                     and terminal != "SETTLED_SUCCESS")
        # True only on the Kernel's verified recovery (its reread and confirm;
        # settlement later relabels the transaction ``SETTLED``), False on a
        # lockdown; anything else -- a recovery timeout included -- is not
        # proof of restoration.
        restored: Optional[bool] = None
        if locked_down:
            restored = False
        elif recovery["recoveryVerified"]:
            restored = True
        execution = {
            "dispatchId": trial_id,
            "applicationStartedAt": stamps.get("apply-start"),
            "appliedAt": stamps.get("applied"),
            "partialApply": (None if report.stop_reason is None else
                             report.stop_reason.value) == "PARTIAL_APPLY",
            "holdEndedAt": window_end,
            "trailingCollectionEnd": window["trailingCollectionEnd"],
            # FINALIZE's reread (verification) and the settle wait happen
            # inside the Kernel's conclude step; its start and end bracket them.
            "concludeStartedAt": stamps.get("conclude-start"),
            "settledAt": stamps.get("concluded"),
        }
        recovery_record = {
            "requested": requested,
            "requestedAt": stamps.get("conclude-start") if requested else None,
            "restored": restored if requested else None,
            "resolution": recovery["resolution"],
            "resolvedAt": stamps.get("concluded") if requested and restored is not None else None,
            # 잠금은 그 자체로 미해결이다 -- 요청 플래그가 무엇이든.
            "unresolved": bool(locked_down or (requested and restored is not True)),
        }
        trial = Trial(
            # Trial 0 is the common initial measurement and is not a live
            # trial, so it does not move the index of the first one.
            trial_index=len([item for item in self.grid.trials
                             if item.trial_index != INITIAL_TRIAL_INDEX]) + 1,
            proposed_target_id=target_id,
            control_id=control_id, configuration=configuration,
            catalog_candidate_id=candidate_id,
            decision_latency_ms=float(decision.get("decisionLatencyMs", 0.0) or 0.0),
            applied_at=stamps.get("apply-start", started), window=window, kpis=kpis,
            kernel={"trialId": trial_id, "terminalState": report.terminal_state.value,
                    "outcome": report.outcome.value,
                    "stopReason": None if report.stop_reason is None else report.stop_reason.value,
                    "evidenceStatus": report.evidence_status, "detail": report.detail,
                    "execution": execution, "recovery": recovery_record},
            rolled_back=report.terminal_state.value != "SETTLED_SUCCESS",
            decision=dict(decision), elapsed_ms=self._elapsed_ms())
        trial.judge_against(self.evaluation_contract, self.intents)
        # 결정 §5.1 (2026-09-23): 관측창 종료·판정 완료·유지 확인·복구 완료·탐색 종료를
        # **따로** 적는다.  현재 시행의 복구는 그 시행의 달성 시각에 들어가지 않아야
        # 하는데, 구분할 재료가 기록에 없었다.  창 종료는 `holdEndedAt`, 복구 완료는
        # `recovery.resolvedAt` 에 이미 있다; 여기서 판정 완료를 더한다.
        stamps_record = dict(trial.kernel.get("execution") or {})
        stamps_record["evaluationCompletedAt"] = self.clock.now()
        trial.kernel["execution"] = stamps_record
        # **역사적 달성과 성공한 유지를 가른다** (결정 §3.4).  성능은 홀드 종료 시점에
        # 이미 자격을 얻었을 수 있지만, 적용 상태가 확인되지 않거나 복구가 남아 있으면
        # 유지는 되지 않는다.  KPI 가 좋아 보인다고 커널의 잠금을 덮어쓰지 않는다.
        # 2026-09-23 (codex 감사 #2): `retention_decisions[-1]` 를 건드리고 있었다.
        # 이 시행이 `trailing-end` 에 닿지 못하면(적용 실패·중도 중단) 이 시행의 기록은
        # 아예 없고, 그러면 **이전 시행의** 유지가 철회되고 그 기준선까지 되돌아간다.
        # `held["retention"]` 은 이 시행이 방금 만든 바로 그 객체다.
        record = (held.get("retention") or {}) if self.request.retain_on_improvement else {}
        if record:
            confirmed = report.terminal_state.value == "SETTLED_SUCCESS"
            outstanding = bool((recovery_record or {}).get("unresolved")) or trial.rolled_back
            if record.get("retain") and (not confirmed or outstanding):
                record["retain"] = False
                record["reason"] = ("applied-state-unconfirmed" if not confirmed
                                    else "recovery-outstanding")
                record["withdrawnAfterConclusion"] = True
                # 기준선은 유지되지 않았으므로 되돌린다 -- 유지된 줄 알고 다음 시행의
                # 복구 목적지로 쓰면 있지도 않은 상태로 복구하려 든다.
                self.retention_baseline = dict(record.get("baselineBefore") or {})
            # 유지 판정이 실행 상태까지 확인된 시각 (§5.1) -- 남았든 철회됐든.
            record["retentionConfirmedAt"] = self.clock.now()
        # Counted once, at execution start: an aborted execution and a
        # deliberate re-measurement of an unchanged control count too, and a
        # boundary already declared puts this trial after it, not before.
        self.trial_flags.setdefault(int(trial.trial_index), {})["counted"] = True
        if trial.kernel.get("stopReason") == StopReason.BASELINE_RESET.value or trial.rolled_back:
            # 2026-09-24 오너: 복구 시간은 전체 제한(B)에 청구하지 않는다.  예전 주석은
            # "The reset's own time ... is charged to B" 였다 -- 첫 v46r10 판(151803)이
            # 복구 약 280 초에 B 를 다 써 DEADLINE 으로 끝났다.
            self._charge_recovery(stamps.get("conclude-start"), stamps.get("concluded"))
        if trial.kernel.get("stopReason") == StopReason.BASELINE_RESET.value:
            started_reset = stamps.get("conclude-start")
            ended_reset = stamps.get("concluded")
            took = (None if not (started_reset and ended_reset) else round(
                (parse_utc(ended_reset) - parse_utc(started_reset)).total_seconds() * 1000.0, 3))
            self.declare_non_trial_event(
                "reset-to-baseline", f"{control_id} was returned to the baseline after "
                                     f"trial {trial.trial_index} (case policy)",
                controlId=control_id, trialIndex=int(trial.trial_index), durationMs=took,
                restored=restored)
        elif trial.rolled_back:
            self.declare_non_trial_event(
                "rollback", f"{control_id} was rolled back after trial "
                            f"{trial.trial_index}; the roll-back is excised from B "
                            "and is not a trial of its own",
                controlId=control_id, trialIndex=int(trial.trial_index))
        if record_in_grid:
            self.grid.record(trial)
            self.calibrate_predictor()
        return trial

    def _v5_order(self) -> Tuple[str, ...]:
        return tuple(getattr(self.contract.preference, "coordinate_order", ()) or ())

    def _v51(self) -> bool:
        from assurance.coordination.concession import weighted_rule
        return weighted_rule(self.contract.preference)

    def _v5_online(self) -> Dict[str, Any]:
        """v5 online evaluation, handed to every selector (3A, IM, BM alike): the latest trial
        judged over the whole Omega by the common evaluator, the best so far and the trial that
        supports it, and the configuration the deployment holds now.  Results of different
        trials are never combined."""
        from assurance.coordination.concession import evaluate_trials
        order = self._v5_order()
        report = evaluate_trials(self.intents, order, self.grid.trials, by_requirement=True,
                                 weighted=self._v51())
        rows = report["trials"]
        return {"evaluator": "common, whole Omega, precision 1/20",
                "coordinateOrder": list(order),
                "current": rows[-1] if rows else None,
                "bestSoFar": report["best"],
                "heldConfiguration": dict(self.retention_baseline or {})
                or dict(self.applied_configuration() or {})}

    def _best_p1_rank(self, kpis: Mapping[str, Any]
                      ) -> Tuple[float, Optional[str]]:
        """이 KPI 벡터가 뒷받침하는 Omega 목표 중 **P1 이 가장 좋은** 것과 그 순위.

        목표 id 도 가중 `cost` 도 아니다: 얼린 도메인에서 `cost` 는 P1 이 다른 목표쌍
        20건에 같은 값을 준다 (2026-09-23 결정 §3.1).
        """
        coordinate_order = self._v5_order()
        if coordinate_order:
            # v5 (owner 2026-09-26): the common evaluator over the whole Omega, the same p the
            # online selector and the final report use -- not the targets this arm's T holds.
            from assurance.coordination.concession import evaluate, requirements_from_intents
            verdict = evaluate(requirements_from_intents(self.intents, by_requirement=True),
                               coordinate_order, dict(kpis or {}), self._v51())
            if not verdict.attained:
                return RETENTION_NO_ATTAINMENT, None
            return float(verdict.p), f"omega:p={verdict.p}"
        authorization = self.evaluation_contract.authorization
        order = tuple(self.evaluation_contract.preference.owner_priority)
        best, best_target = RETENTION_NO_ATTAINMENT, None
        for target in self.evaluation_contract.targets:
            if verdict_of(judge_target(kpis, target, authorization)) != PASS:
                continue
            try:
                rank = float(_p1_rank_of(target.to_record(),
                                         authorization.to_record(), order))
            except (ValueError, KeyError, TypeError, ZeroDivisionError):
                continue
            if rank < best:
                best, best_target = rank, target.target_id
        return best, best_target

    def _seed_retention_from_initial(self, trial: Trial) -> None:
        """기준 관측이 역사 최고와 복구 기준선의 **출발값**이다 (codex 감사 #3).

        이것을 안 하면 기준 관측이 P1 6 을 달성해도 `previous_best_rank` 가 무한대로
        남아, 그보다 **나쁜** 첫 제어 시행이 `first-attainment` 로 유지된다.  기준선도
        비어 있어 첫 시행의 `baselineBefore` 가 `{}` 로 기록된다.
        """
        if not self.request.retain_on_improvement:
            return
        applied = dict(self.applied_configuration() or {})
        if applied:
            self.retention_baseline = applied
        window = dict(getattr(trial, "window", {}) or {})
        if not window.get("valid", True):
            return
        best, _target = self._best_p1_rank(dict(trial.kpis or {}))
        if best < self.previous_best_rank:
            self.previous_best_rank = best

    def _retention_for(self, aggregate: Tuple[str, Any],
                       configuration: Mapping[str, str]) -> Dict[str, Any]:
        """Decide retention from this window, before FINALIZE or rollback.

        The window is already aggregated at ``trailing-end``, so this runs on the
        same numbers the grid will record -- not on a second measurement.  The
        best P1 rank over **Omega** is the comparison key: never a target id and
        never the weighted ``cost``, which on the frozen domain takes the same
        value for 20 pairs of targets that differ in P1 (2026-09-23 decision §3.1).

        Execution state is not known yet at this point -- the Kernel has not
        concluded -- so the two conditions the decision separates are handled in
        two places: the *performance* half is decided here, and the caller
        withdraws retention if the applied state turns out unconfirmed or a
        recovery is outstanding (§3.4).  That is why the record keeps
        ``performanceQualifies`` beside ``retain``.
        """
        _window_end, (kpis, unknown, _coverage, _cohorts) = aggregate
        missing = [key for key in self._required_observation_keys() if key not in kpis]
        # 2026-09-23 라이브 실측: 유지 판정 23건이 **전부** `observation-invalid` 였다 --
        # 평가기가 유효라고 한 창(`observationValidity.valid = True`)까지.  `unknown` 을
        # 통째로 보고 있었는데, 그 안에는 요구조건이 아닌 진단 KPI
        # (`lowDeliveryRunBins@ue*`, 이 베드에서 적용범위 0.36~0.57 로 늘 UNKNOWN)가 들어
        # 있다.  그래서 이 베드에서는 유지가 **한 번도 일어날 수 없었고**, 실패는 복원
        # 쪽이라 아무 데서도 안 터졌다.  유효성은 평가기와 같은 규칙이어야 한다: 판정에
        # 쓰는 **요구 KPI** 가 창에 있는가 (UNKNOWN 인 요구 키는 `kpis` 에서 빠지므로
        # `missing` 에 잡힌다).  커널 결과 쪽 무효(잠금·부분 적용)는 결론 뒤 철회가 본다.
        valid = not missing
        best, best_target = self._best_p1_rank(kpis) if valid else (
            RETENTION_NO_ATTAINMENT, None)
        decision = decide_retention(
            trial_is_valid=valid, current_best=best,
            previous_best=self.previous_best_rank,
            # 이 시점에 아는 것: 적용은 되었고(런타임이 홀드를 유지했다) 아직 결론이
            # 나지 않았다.  확인·복구는 결론 뒤에 `_withdraw_retention` 이 본다.
            configuration_confirmed=True, recovery_required=False)
        record = dict(decision.to_record())
        record.update(targetId=best_target,
                      appliedConfiguration=dict(configuration),
                      baselineBefore=dict(self.retention_baseline),
                      observationValid=valid,
                      missingObservationKeys=missing, unknownKpis=list(unknown))
        if decision.retain:
            record["transitionScope"] = list(
                transition_scope(self.retention_baseline, configuration))
            self.retention_baseline = next_baseline(
                decision, configuration, self.retention_baseline)
        if best < self.previous_best_rank:
            self.previous_best_rank = best
        self.retention_decisions.append(record)
        return record

    def _attains_no_target(self, started: str, aggregate: Tuple[str, Any],
                           rows: List[Dict[str, Any]]) -> bool:
        """Judged in full, and no authorized target passes on this window.

        The Kernel's joint predicates are serving-cell membership only, so a
        control that met no target at all still settled live, held its axis
        and blocked every later control that moves it (2026-09-15 attempts 45,
        51, 53, 55: the search ended after one trial).  The same raw-KPI
        judgement the grid records is taken here before conclusion; a trial
        that attains nothing is stopped as SEMANTIC_NON_SUCCESS and rolled back.
        A window with any unknown KPI is not a KPI failure and is left to the
        Kernel's own decision.

        2026-09-22 (codex 감사): `unknown` 은 **등장한** 키 중 coverage 미달만 담는다.
        필수 키가 모든 poll 에서 통째로 빠지면 그 키는 `unknown` 에도 없으므로
        `not unknown` 이 참이 되고, 아무 목표도 통과하지 못한 것으로 읽혀
        SEMANTIC_NON_SUCCESS 와 롤백까지 갔다 -- **관측원 장애가 KPI 실패로 둔갑**한다.
        그래서 계약이 요구하는 관측 키가 실제로 창에 있는지도 함께 본다.  하나라도
        없으면 그 창은 판정 불가이고, 실패가 아니다.
        """
        window_end, (kpis, unknown, coverage, cohorts) = aggregate
        missing = [key for key in self._required_observation_keys() if key not in kpis]
        if missing:
            self.declare_non_trial_event(
                "unjudgeable-window",
                "the window is missing observation keys the contract requires, so it "
                "cannot show that no target was attained: " + ", ".join(sorted(missing)))
            return False
        probe = Trial(trial_index=0, kpis=kpis, window=self._window_record(
            started, window_end, rows, kpis, unknown, coverage, cohorts))
        if self._v5_order():
            # v5 (Codex review 2026-09-26): attainment is the common evaluator over the whole
            # Omega -- the same verdict that decides retention -- not this arm's T.
            from assurance.coordination.concession import evaluate, requirements_from_intents
            verdict = evaluate(requirements_from_intents(self.intents, by_requirement=True),
                               self._v5_order(), dict(kpis or {}), self._v51())
            return bool(probe.window_valid and not unknown and not verdict.attained)
        probe.judge_against(self.evaluation_contract, self.intents)
        return bool(probe.window_valid and not unknown and probe.success
                    and not any(probe.success.values()))

    def _required_observation_keys(self) -> Tuple[str, ...]:
        """계약이 판정에 쓰는 관측 키.  하나라도 창에 없으면 그 창은 판정 불가다."""
        keys = []
        for row in (self.intents or ()):
            requirement = getattr(getattr(row, "intent", row), "requirement", None)
            key = getattr(requirement, "observation_key", None)
            if key and str(key) not in keys:
                keys.append(str(key))
        return tuple(keys)

    def _sleep_to(self, due_ms: float) -> None:
        """Sleep only what is left of the tick, so the time a collection costs
        does not accumulate on every tick and push the real sample spacing past
        the cadence the coverage rule is scored against."""
        remaining = float(due_ms) - float(self.clock.monotonic_ms())
        if remaining > 0:
            self.clock.sleep_ms(int(round(remaining)))

    def _sample_service_trace(self, **flags: Any) -> Dict[str, Any]:
        row = _service_row(self.observer, self.clock.now,
                           self.preflight.setdefault("observerErrors", []))
        row.update(flags)
        _mark_excision(row, self.service_trace)
        self.service_trace.append(dict(row))
        return row
    def _end(self, reason: str, detail: str) -> None:
        if self.termination is None:
            self.termination, self.termination_detail = reason, detail
            # §5.1: 탐색 종료 시각.  그 뒤의 서비스 감시는 별도 구간이다.
            self.search_terminated_at = self.clock.now()

    # -- what the sitting produced ----------------------------------------- #

    def resource_cost(self) -> Dict[str, Any]:
        models = self.agents.models
        calls = self.agents.calls
        attempts = sum(
            0 if call.role not in ROLES or models.model_for(call.role) in (None, RULE_GREEDY)
            else 1 + call.repair_retries
            for call in calls)
        return {
            "prepMs": float(self.timing.get("prepMs", 0.0)),
            "totalMs": float(self.ended_ms - self.started_ms),
            "llmCalls": attempts,
            "inputTokens": sum(int(call.input_tokens) for call in calls),
            "outputTokens": sum(int(call.output_tokens) for call in calls),
            # Both sums above are LOWER BOUNDS whenever a generation reported no
            # count, because CallRecord adds an unreported generation as 0.  This
            # is the same ``all``-fold one level up from
            # ``CallRecord.tokens_complete`` -- false once ANY call feeding the
            # totals was incomplete -- so the aggregate carries its own
            # completeness rather than reading like a measured total.  Same
            # predicate and same type as the call row, so a reader who knows that
            # flag needs no new vocabulary here.  The sums are unchanged.
            "tokensComplete": all(call.tokens_complete for call in calls),
            "decisionLatenciesMs": [float(trial.decision_latency_ms)
                                    for trial in self.grid.trials],
        }

    def episode(self) -> EpisodeRecord:
        """The sitting as contract v2's ``agent-episode/1.1.0`` record."""
        # **Ω 에서 고른다.**  `success` 는 이제 Ω id 로 키가 되고, 선택된 T 의
        # alternatives 는 `from_compact` 에서 `T1..Tn` 으로 **재번호**되므로 같은 철자가
        # 서로 다른 요구 벡터다.  선택된 T 를 넘기면 `contract.target(target_id)` 가
        # T0 말고는 못 찾아 달성을 조용히 잃는다 (2026-09-23).
        best = self.grid.best_attained(self.evaluation_contract)
        first = self.grid.first_success()
        t0 = self.contract.t0.target_id
        return EpisodeRecord(
            episode_id=self.request.episode_id or f"{self.case_id}",
            method=self.method, session_mode=self.mode,
            condition=dict(self.request.condition),
            block=int(self.request.block), repetition=int(self.request.repetition),
            models=self.agents.models.to_record(),
            intents=self.intents, contract=self.contract, controls=self.controls,
            budget={"trialsK": int(self.request.budget_trials),
                    "deadlineBMs": self.request.deadline_ms,
                    "horizonHMs": self.request.horizon_ms,
                    "qualityThresholdsA": list(self.request.quality_thresholds),
                    "binDeltaMs": int(self.request.bin_delta_ms)},
            timing=dict(self.timing),
            calls=[call.to_record() for call in self.agents.calls],
            trials=list(self.grid.trials),
            service_trace=list(self.service_trace),
            best_attained=best,
            retained=dict(self.retained) if self.retained else
                     {"targetId": None, "controlId": None, "qualified": False,
                      "detail": "the sitting did not run"},
            first_success={"trialIndex": first.trial_index,
                           "elapsedMs": first.elapsed_ms} if first is not None else {},
            retention_decisions=list(self.retention_decisions),
            formal_reference_trial=bool(self.request.formal_reference_trial),
            retain_on_improvement=bool(self.request.retain_on_improvement),
            t0_success=any(trial.success.get(t0) for trial in self.grid.trials),
            termination={"reason": self.termination or BUDGET_EXHAUSTED,
                         "detail": self.termination_detail,
                         "kernelTermination": self.kernel_termination,
                         "searchTerminatedAt": getattr(self, "search_terminated_at", None)},
            resource_cost=self.resource_cost(),
            measurement_rules=self.rules.to_record(),
            intake=dict(self.intake))

    def episode_record(self) -> Dict[str, Any]:
        """The episode as the JSON the metrics read, with contract v4 on it.

        :meth:`episode` is the shared :class:`~assurance.coordination.EpisodeRecord`
        every arm of the paper has always produced; this adds the four things
        contract v4 asks the executor to make explicit rather than implied, and
        it is the record :func:`write_agent_evidence` writes:

        * ``trials[].counted`` -- what the document counts as a live trial, and
          ``trials[].beforeBoundary`` on any trial a declared boundary came
          after;
        * ``nonTrialEvents`` -- rejected proposals, re-measurements, roll-backs
          and out-of-trial sampling, each with the elapsed time it charged;
        * ``boundaries`` -- every boundary this episode declared;
        * ``prepared`` -- whether ``T`` and ``C`` were injected, and their
          content hashes either way, so a trajectory ablation can prove the
          board was byte-identical across methods.
        """
        record = self.episode().to_record()
        record["schemaVersion"] = AGENT_EPISODE_SCHEMA
        record["kpiObserverFailures"] = list(getattr(self.observer, 'failures', ()))
        if hasattr(self.observer, 'describe'):
            record["kpiSources"] = self.observer.describe()
        record["trials"] = [self.trial_record(trial) for trial in self.grid.trials]
        # v4.7 paper metric (.orca/drops/V47_FINAL_PLAN.md 3.2-3.4): the best target in the
        # common Omega each observation supports, at 0.05 precision, lexicographic owners.
        # It never decides anything in the sitting and must never break this record.
        try:
            from assurance.coordination.concession import evaluate_trials
            preference = self.contract.preference
            order = tuple(getattr(preference, "coordinate_order", ()) or ())
            from assurance.coordination.concession import weighted_rule
            record["paperEvaluation"] = evaluate_trials(
                self.intents, order or tuple(preference.owner_priority), self.grid.trials,
                by_requirement=bool(order), weighted=weighted_rule(preference))
        except Exception as exc:  # noqa: BLE001
            record["paperEvaluation"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        record["boundaries"] = [dict(item) for item in self.boundaries]
        record["nonTrialEvents"] = [dict(item) for item in self.non_trial_events]
        record["identityRebinds"] = [dict(item) for item in self.identity_rebinds]
        record["hardwareDisconnects"] = [dict(item) for item in self.hardware_disconnects]
        # What the radio cost this episode, in one place a footnote can quote: the
        # composition wait is not a disconnect record (the sitting did not exist yet)
        # and would otherwise be invisible -- attempt 129 waited 64 s for ue3 and its
        # episode still reported hardwareDisconnects 0.
        record["hardwareWaitMs"] = self._hardware_wait_record()
        record["excision"] = self._excision_record()
        record["prepared"] = dict(self.prepared)
        # The contract's own field name, beside the dict that carries the two
        # hashes: a reader that only wants to know whether the board was
        # injected should not have to open a sub-object for it.
        record["preparedInjected"] = bool(self.prepared.get("injected", False))
        # Amendment 6, episode row: what definition, code, request and load
        # this episode ran under, stated rather than implied by the condition.
        record["experimentVersion"] = (os.environ.get("AIC_EXPERIMENT_VERSION")
                                       or EXPERIMENT_VERSION)
        record["codeRevision"] = _code_revision()
        # 커밋 해시는 미커밋 수정을 못 말한다; 이것이 말한다.
        # **판이 시작할 때(모듈을 불러온 때)의 값**이다 -- 기록 시점에 다시 재면 판이 도는
        # 동안 바뀐 파일까지 섞여, 실제로 돈 코드와 다른 해시가 찍힌다(2026-09-23 시도 456).
        record["sourceDigest"] = _SOURCE_DIGEST_AT_IMPORT
        record["request"] = self.request.to_record()
        record["offeredLoadMbps"] = self.request.condition.get("offeredLoadMbps")
        # Completion row: the last actual service observation (``timing.end``
        # is only when the sitting returned) and every recovery the record
        # cannot show as confirmed.
        trials = [trial.to_record() for trial in self.grid.trials]
        excused = self._disconnect_trial_ids()
        if self.retained and self.retained.get("trial"):
            trials.append(dict(self.retained["trial"]))
        unresolved = [{"trialIndex": trial["trialIndex"],
                       "trialId": trial["kernel"].get("trialId"),
                       "terminalState": trial["kernel"].get("terminalState"),
                       "resolution": trial["kernel"]["recovery"].get("resolution"),
                       "hardwareDisconnect": trial["kernel"].get("trialId") in excused}
                      for trial in trials
                      if dict(trial["kernel"].get("recovery") or {}).get("unresolved")]
        for name in ("kernelTerminationRefusal", "retentionTerminationRefusal"):
            if self.preflight.get(name):
                unresolved.append({"kernelTermination": name,
                                   "detail": str(self.preflight[name])})
        record["completion"] = {
            "endedAt": self.timing.get("end"),
            "finalObservationAt": (self.service_trace[-1]["t"]
                                   if self.service_trace else None),
            "unresolved": unresolved}
        return record

    def summary(self) -> Dict[str, Any]:
        # **Ω 에서 고른다.**  `success` 는 이제 Ω id 로 키가 되고, 선택된 T 의
        # alternatives 는 `from_compact` 에서 `T1..Tn` 으로 **재번호**되므로 같은 철자가
        # 서로 다른 요구 벡터다.  선택된 T 를 넘기면 `contract.target(target_id)` 가
        # T0 말고는 못 찾아 달성을 조용히 잃는다 (2026-09-23).
        best = self.grid.best_attained(self.evaluation_contract)
        return {
            "termination": self.termination,
            "detail": self.termination_detail,
            "kernelTermination": self.kernel_termination,
            "method": self.method,
            "trials": len(self.grid.trials),
            "targets": list(self.contract.target_ids),
            "controls": list(self.controls.control_ids),
            "grid": self.grid.to_record(),
            "t0Success": any(trial.success.get(self.contract.t0.target_id)
                             for trial in self.grid.trials),
            "bestAttained": None if best is None else best.to_record(),
            "retained": dict(self.retained) if self.retained else
                        {"targetId": None, "controlId": None, "qualified": False,
                         "detail": "the sitting did not run"},
            "roleModels": self.agents.models.to_record(),
            "calls": [call.to_record() for call in self.agents.calls],
            "measurementRules": self.rules.to_record(),
            "intake": dict(self.intake),
            "predictor": (None if self.predictor is None
                          else dict(self.predictor.describe())),
            "reobservations": [item.to_record() for item in self.reobservations],
            "timingMode": self.timing_mode,
            "boundaries": [dict(item) for item in self.boundaries],
            "nonTrialEvents": [dict(item) for item in self.non_trial_events],
            "identityRebinds": [dict(item) for item in self.identity_rebinds],
            "hardwareDisconnects": [dict(item) for item in self.hardware_disconnects],
            "hardwareWaitMs": self._hardware_wait_record(),
            "prepared": dict(self.prepared),
        }


def _decision_record(record: Any, target_id: Optional[str],
                     control_id: Optional[str]) -> Dict[str, Any]:
    return {"role": record.role, "model": record.model, "phase": record.phase,
            "fallbackReason": record.fallback_reason, "rationale": record.rationale,
            "repairRetries": int(record.repair_retries),
            "options": dict(getattr(record, "options", {}) or {}),
            "staleAtArrival": bool(getattr(record, "stale_at_arrival", False)),
            "targetId": target_id, "controlId": control_id}


# --------------------------------------------------------------------------- #
# composition
# --------------------------------------------------------------------------- #


#: The owner preference profiles of the integrated reply, section 3.  ``P1`` is
#: ``(D1, D2, D3)`` ascending, ``P2`` is the same rule over the rotated owner
#: order ``(D2, D3, D1)``, and ``P3`` puts ``(D_max, D_mean)`` in front of that
#: tail -- which is exactly what :func:`preference_key` does whenever the rule
#: names ``D_max``.  The final tie-break is already the full ordered level
#: tuple: :func:`expand_targets` appends the level combination to the sort key.
PREFERENCE_PROFILES = ("P1", "P2", "P3")


def _preference_for(intents: Sequence[Intent],
                    profile: Optional[str] = None) -> Preference:
    """The owner preference this sitting ranks targets by.

    Selected by ``AIC_PREFERENCE=P1|P2|P3`` so a campaign can hold everything
    else fixed and vary only the owner preference.  Unset -- or any name that
    is not a profile -- keeps :meth:`Preference.from_intents`, whose default
    rule already names ``D_max``; ``P3`` is that same ranking under its name,
    so no setting of this variable changes what an unset one does.

    The profile name is carried in ``rule``, which the episode record already
    serializes (``T.preference.rule``), so the record says which profile the
    episode actually ran under rather than leaving it to be inferred.
    """
    base = Preference.from_intents(intents)
    if base.coordinate_order:
        return base      # v5: the single owner's own coordinate order; owner profiles do not apply
    name = str(profile if profile is not None
               else os.environ.get("AIC_PREFERENCE", "")).strip().upper()
    if name not in PREFERENCE_PROFILES:
        return base
    if name == "P3":
        return replace(base, rule=f"{name}: {base.rule}")
    owners = base.owner_priority
    if name == "P2":
        owners = owners[1:] + owners[:1]
    # No ``D_max`` in the rule: preference_key then compares the owners'
    # normalized concessions in this order and nothing else.
    return replace(base, owner_priority=owners,
                   rule=f"{name}: lexicographic("
                        + ", ".join(f"D_{owner}" for owner in owners) + ")")


#: How old the runner's role identity entry may be before a role is treated as unresolved.
ROLE_IDENTITY_MAX_AGE_S = 60.0


def role_identity_resolver(profile_document: Mapping[str, Any], *,
                           max_age_s: float = ROLE_IDENTITY_MAX_AGE_S,
                           clock: Callable[[], float] = None) -> Callable[[str], Optional[int]]:
    """The AMF UE NGAP id the network gives a UE *label* now.

    A numeric label is its own id (every existing caller).  A role label
    (``ue1``) is read from ``liveConsole.ueIdentityPath``, which the guarded
    runner rewrites as the AMF re-registers the UE; an absent, unreadable or
    stale entry resolves to ``None`` and the caller keeps its refusal path
    (docs/design/ue-identity-continuity.md).
    """
    import time as _time
    now = clock or _time.time
    path = str(((profile_document or {}).get("liveConsole") or {}).get("ueIdentityPath") or "")

    def amf_of(label: str) -> Optional[int]:
        label = str(label)
        if label.isdigit():
            return int(label)
        if not path:
            return None
        try:
            entry = (json.loads(Path(path).read_text(encoding="utf-8")).get("roles") or {}).get(label)
            if not entry or now() - float(entry["writtenAtUnix"]) > max_age_s:
                return None
            return int(entry["amfUeNgapId"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    return amf_of


#: How many times composition rebuilds a supplementary axis whose UE went unaddressable
#: again between the wait and the baseline read.  Each round costs one r1 deadline plus
#: two seconds, so this is a handful of seconds of slack, not a second waiting room.
SUPPLEMENTARY_BUILD_ATTEMPTS = 4

#: How long composition waits for a role to be addressable again before giving up on it.
#: Generous on purpose: with the overall deadline B removed (2026-09-16) the sitting is no
#: longer racing a clock, and a UE that re-registers takes 20-25 s to attach plus whatever
#: the radio needed first -- attempt 120's pair was away for three minutes.
ROLE_ADDRESSABLE_WAIT_MS = int(HARDWARE_WAIT_CAP_MS)   # one wait, same cap as every other


def _await_addressable(*, reader: Any, amf_of: Callable[[str], Optional[int]], ue: str,
                       clock: Any, freshness_ms: int, allowed_cells: Sequence[int],
                       previous: Any, max_wait_ms: Optional[float] = None) -> Any:
    """The UE's observation once the role is addressable again, waiting rather than refusing.

    Returns ``previous`` unchanged when the role already resolves to the id it was pinned
    to and that id is still attributed on an admitted cell -- the common case, and no wait.
    Otherwise it re-resolves the role (the runner republishes it from fresh KPM every 2 s)
    and re-observes, so a UE that came back under a new AMF id is followed instead of being
    refused.  Raises the same refusal :func:`_current_amf` would have raised if the role is
    still unaddressable when the wait is spent, so nothing becomes a silent success.
    """
    allowed = frozenset(int(cell) for cell in allowed_cells)
    bound = float(ROLE_ADDRESSABLE_WAIT_MS if max_wait_ms is None
                  else min(float(max_wait_ms), ROLE_ADDRESSABLE_WAIT_MS))
    limit = float(clock.monotonic_ms()) + bound
    while True:
        amf = amf_of(ue)
        if amf is not None:
            try:
                observed = observe_selected_ue(
                    reader, now=clock.now, sleep_ms=clock.sleep_ms,
                    freshness_ms=freshness_ms, amf_ue_ngap_id=int(amf))
            except LiveConsoleError:
                observed = None
            if observed is not None and int(observed.serving_nci) in allowed:
                return observed
        if float(clock.monotonic_ms()) >= limit:
            if amf is None:
                return _current_amf(amf_of, ue)   # raises with the reason
            raise LiveConsoleError(
                f"UE {ue} holds amfUeNgapId {amf} but no fresh indication on an admitted "
                f"cell arrived within {round(bound)} ms; the axis cannot be built")
        clock.sleep_ms(2000)


def role_serving_cells(raw: Mapping[str, str], ue_ids: Sequence[str],
                       amf_of: Callable[[str], Optional[int]]) -> Dict[str, str]:
    """``{amfUeNgapId: nci}`` from KPM → ``{UE label: nci}``, **roles only**.

    An id no role currently holds is dropped, not passed through under its raw
    number: it is either not one of this sitting's UEs, or a UE whose identity
    is in transition (KPM keeps a re-registered UE's old id for a few seconds,
    and the new one reaches the role only once the resolver catches up).
    Either way nothing can be said about *our* UE's membership from it, and a
    raw key beside the role key breaks the cell total and trips the
    membership sweep (see the comment where this is called).
    """
    labels = {str(amf): ue for ue in ue_ids for amf in (amf_of(ue),) if amf is not None}
    return {labels[str(key)]: value for key, value in dict(raw).items()
            if str(key) in labels}


def _current_amf(amf_of: Callable[[str], Optional[int]], label: str) -> int:
    amf = amf_of(label)
    if amf is None:
        raise LiveConsoleError(
            f"UE {label} has no current amfUeNgapId: the runner's role identity entry is "
            "absent or stale, so the UE cannot be addressed now")
    return int(amf)


CALIBRATION_SCRIPT_ENV = "AIC_CALIBRATION_SCRIPT"


def _calibrating() -> bool:
    """A calibration board measures its whole list; no target ends it early."""
    # Only a calibration campaign may run the scripted list (Codex review #15): the same
    # variable left over in a v4.7 environment must not switch off the success exits.
    return bool(os.environ.get(CALIBRATION_SCRIPT_ENV)) and \
        os.environ.get("AIC_CAMPAIGN", "").endswith("cal")


def scripted_calibration_decision(inputs: BasicInputs, script_path: str, campaign: str):
    """Calibration only (v4.7 plan step 1, 2026-09-25): answer the basic-monolith
    decision with the next untried configuration from a declared list, no model.

    Every trial still goes through the same apply / observe / rollback / verdict path, so
    a fixed set of representative configurations is measured under the exact conditions a
    campaign sees.  The owner's rule of 2026-09-19 -- no deterministic rule ever answers
    for a model -- holds for experiment campaigns, so this refuses to run unless the
    campaign name marks a calibration (``...cal``).  The list is JSON: a list of partial
    configurations (axis -> value) laid over the baseline; an exhausted list stops the board.
    """
    if not campaign.endswith("cal"):
        raise RuntimeError(f"{CALIBRATION_SCRIPT_ENV} is set but campaign {campaign!r} is "
                           "not a calibration campaign (name must end in 'cal')")
    overrides = json.loads(Path(script_path).read_text(encoding="utf-8"))
    tried = {tuple(sorted((str(k), str(v)) for k, v in dict(item).items()))
             for item in inputs.tried_configurations}
    record = CallRecord(role="basic-monolith", model="scripted-calibration",
                        phase="selection", accepted=True)
    for index, override in enumerate(overrides):
        configuration = dict(inputs.baselines)
        configuration.update({str(k): str(v) for k, v in dict(override).items()})
        if tuple(sorted(configuration.items())) in tried:
            continue
        aimed, records = _original_requirements(inputs)
        record.rationale = f"calibration script entry {index}: {dict(override)}"
        return BasicDecision(configuration=configuration, requirements=aimed,
                             requirement_records=tuple(records),
                             rationale=record.rationale), record
    record.rationale = "calibration script exhausted"
    return None, record


def build_agent_sitting(profile: Any, request: AgentRequest, **kwargs: Any) -> AgentSitting:
    """Compose one Agent sitting; see :func:`_compose_agent_sitting`.

    A cold start's service sampler begins before the Target and Control calls,
    so a build that refuses after that point stops it here rather than leaving
    it polling the UEs for an episode that never started.
    """
    samplers: List[_FormationSampler] = []
    try:
        return _compose_agent_sitting(profile, request, _started_samplers=samplers, **kwargs)
    except BaseException:
        for sampler in samplers:
            sampler.stop()
        raise


def _compose_agent_sitting(
    profile: Any,
    request: AgentRequest,
    *,
    _started_samplers: List[Any],
    read_new_lines: Optional[Callable[[], Sequence[str]]] = None,
    policy_port: Optional[Any] = None,
    adapter_overrides: Optional[Mapping[str, Any]] = None,
    action_policy_ports: Optional[Mapping[str, Any]] = None,
    ports: Optional[Any] = None,
    scope_clearer: Optional[Callable[..., Sequence[Mapping[str, Any]]]] = None,
    stamp: Optional[str] = None,
    counter_sample_loaders: Optional[Mapping[str, Any]] = None,
    arrival: Optional[Callable[[], str]] = None,
    role_resolver: Optional[Callable[[str], Any]] = None,
    kpi_observer: Optional[KpiObserver] = None,
    slices: Sequence[Any] = (),
    ue_slices: Optional[Mapping[str, str]] = None,
    prepared: Optional[Tuple[TargetContract, ControlCandidates]] = None,
) -> AgentSitting:
    """Compose one Agent sitting over the official O-RAN control path.

    Every keyword after ``request`` is an injection seam for the hermetic tests
    and for the hardware-free runtime, and using any of them makes the sitting
    ``MOCK`` exactly as it does for
    :func:`tools.liveconsole.build.build_live_session`.

    The preparation is contract section 5's: read the intents, derive the
    owner's authorization and preference, let Target and Control (or one
    monolith, or the deterministic rules) form ``T`` and ``C``, freeze the
    joint Kernel case over the axis values ``C`` uses, and map each control
    onto its catalog candidate.  Nothing is written to the deployment here.

    ``prepared=(contract, controls)`` is contract v4 section 4's injection
    seam: the Target and the Control calls are skipped entirely and *that*
    exact ``T`` and ``C`` are used, so a comparison can hold the board
    byte-identical across methods and let only the Trajectory decision differ
    (``exp_metrics.md`` figure 5).  The sitting records ``preparedInjected``
    and the content hash of both; an injected ``C`` that names an axis value
    the frozen catalog does not admit is **refused** by name rather than
    silently dropped, because a dropped column would no longer be the same
    board the other arm ran.

    ``request.timing_mode`` decides where the episode clock starts
    (``exp_metrics.md`` section 3).  In ``prepared`` the live trigger ``t0`` is
    taken when :meth:`AgentSitting.run` begins, after everything here; in
    ``cold-start`` ``t0`` is the moment the required inputs were released --
    the first line of this function -- so the preparation below consumes B and
    a sitting whose B expires while ``T`` and ``C`` are being formed ends
    ``DEADLINE`` with nothing applied.
    """
    deployment = (profile if isinstance(profile, LiveDeployment)
                  else load_live_deployment(profile))
    injected = tuple(name for name, value in (
        ("read_new_lines", read_new_lines), ("policy_port", policy_port),
        ("adapter_override", adapter_overrides), ("ports", ports),
        ("scope_clearer", scope_clearer),
        ("counter_sample_loaders", counter_sample_loaders), ("arrival", arrival),
        ("kpi_observer", kpi_observer),
    ) if value is not None)
    mode = session_mode(injected)
    clock = ports or WallClockPorts
    sitting_stamp = stamp or _stamp()
    prep_start = clock.now()
    prep_started_ms = float(clock.monotonic_ms())
    binding = deployment.binding
    timing = LiveTiming(**_live_timing_overrides())
    topology = live_topology(binding, deployment.capability)
    policy_type_id = binding.r1.policy_type_id
    families = live_capable_families()
    topology_cells = tuple(sorted(set(topology.nb_id_to_nci.values())))
    cells = request.cells or topology_cells
    for cell in cells:
        if cell not in topology_cells:
            raise LiveConsoleError(
                f"{cell} is not a cell this deployment advertises; the capability "
                "manifest names " + ", ".join(str(c) for c in topology_cells))

    # Refuse an unconfigured live KPI before policy discovery or scope writes.
    rows = parse_agent_intents(request, families=families)
    ue_ids = list(dict.fromkeys([row.ue_id for row in rows if row.ue_id]
                                + list(request.caps) + list(request.pf_weights)))
    profile_document = _profile_document(deployment)
    amf_of = role_identity_resolver(profile_document)
    hosts = (resolve_ue_hosts(ue_ids, profile_document=profile_document)
             if mode == MODE_LIVE else {})
    if mode == MODE_LIVE:
        try:
            observation_keys = [row.intent.requirement.observation_key for row in rows]
            live_echo_sources(observation_keys, profile_document=profile_document, hosts=hosts)
            live_flow_sources(observation_keys, profile_document=profile_document, hosts=hosts)
        except KpiObserverError as exc:
            raise LiveConsoleError(str(exc)) from None

    tail_lines = (read_new_lines if read_new_lines is not None
                  else KpmTail(deployment.kpm_jsonl_path).read_new_lines)
    fan_out = _FanOutTail(tail_lines)
    reader = KpmUeAttributionReader(read_new_lines=fan_out.consumer(), topology=topology)
    # 셀 축 기준값을 읽을 소비자는 **여기서** 붙인다.  나중에 붙은 소비자는 그 전에
    # 흘러간 줄을 받지 못하고(`drain` 은 그 시점의 소비자들에게만 나눠 준다), 축 조립은
    # 게이트를 통과한 **직후**라 그 사이 새 줄이 아직 안 쌓였을 수 있다.  2026-09-18 판
    # 01:37:34 의 `[txAttenuationDb=['0.0', '13.0', ...]` 가 그 증거다 -- 조립 시점에
    # 붙인 소비자가 빈 손이라 기준값이 라이브 10.0 이 아니라 선언 상수 0.0 이 됐다.
    baseline_tail = fan_out.consumer()
    if policy_port is None:
        state_dir = deployment.r1_state_dir / sitting_stamp
        state_dir.mkdir(parents=True, exist_ok=True)
        policy_port = build_r1_policy_port(
            deployment.values, state_path=state_dir / "r1-state.json")
    port = RecordingPolicyPort(policy_port)
    discovery = build_policy_type_discovery(
        port, policy_type_id=policy_type_id, capability_manifest=deployment.capability)
    deployment.evidence_dir.mkdir(parents=True, exist_ok=True)
    clear = scope_clearer or clear_ue_scope
    kpm_adapter = build_live_collectors(binding)[1]
    case_stamp = sitting_stamp
    prefix = deployment.evidence_dir / f"AGENT-{case_stamp}"
    case_id = f"case/liveconsole-agent:{case_stamp}"

    # -- the intents, in both vocabularies ---------------------------------- #
    for row in rows:
        if row.named_cell is not None and int(row.named_cell) not in topology_cells:
            raise LiveConsoleError(
                f"intent {row.intent.intent_id} names cell {row.named_cell}, which this "
                "deployment does not advertise ("
                + ", ".join(str(c) for c in topology_cells) + ")")

    # -- every UE observed now, its scope freed, its preconditions named ----- #
    identities: Dict[str, LiveUeObservation] = {}
    #: What composition spent waiting for an unaddressable UE, handed to the sitting so the
    #: formation allowance is not charged for a radio outage (see AgentSitting._hardware_wait_ms).
    composition_wait_ms = [0.0]
    #: The same waits with start and end, for the episode's excision record.
    composition_wait_log: List[Dict[str, Any]] = []
    #: The sitting, once it exists: a rebind's recomposition reads its clocks (B spent,
    #: our-side time excised) and charges its own waits to it, not to a copy.
    sitting_ref: Dict[str, Any] = {}

    def _exempt_ms() -> float:
        sitting = sitting_ref.get("sitting")
        if sitting is not None:
            return sitting._hardware_wait_ms()
        # The same union the sitting takes over (``_hardware_wait_ms``, 2026-09-24).
        return _union_ms([*(_excised_spans(sampler.rows, float(timing.cadence_ms))
                            if sampler is not None else ()),
                          *_wait_spans(composition_wait_log)])

    def _charge_composition_wait(started_ms: float, started_at: str) -> None:
        waited = float(clock.monotonic_ms()) - started_ms
        composition_wait_ms[0] += waited
        sitting = sitting_ref.get("sitting")
        if sitting is not None:
            sitting.composition_wait_ms = float(sitting.composition_wait_ms) + waited
        if waited > 0:
            composition_wait_log.append({"start": started_at, "end": clock.now(),
                                         "waitedMs": waited})
    preconditions: List[str] = []
    cleared: Dict[str, List[Dict[str, Any]]] = {}
    occupants: Dict[str, Any] = {}
    for ue in ue_ids:
        identity = observe_selected_ue(
            reader, now=clock.now, sleep_ms=clock.sleep_ms,
            freshness_ms=timing.freshness_bound_ms, amf_ue_ngap_id=_current_amf(amf_of, ue))
        identities[ue] = identity
        archive_path = Path(f"{prefix}-scope-archive-{ue}.json")

        def archive(records: Sequence[Mapping[str, Any]], *, _path=archive_path, _ue=identity) -> None:
            _write_scope_archive(_path, records, at=clock.now(),
                                 amf_ue_ngap_id=_ue.amf_ue_ngap_id,
                                 database=deployment.producer_database)

        occupants[ue] = scope_occupants(deployment.producer_database,
                                        amf_ue_ngap_id=identity.amf_ue_ngap_id)
        cleared[ue] = [dict(entry) for entry in clear(
            binding, state_database=deployment.producer_database,
            amf_ue_ngap_id=identity.amf_ue_ngap_id, policy_type_id=policy_type_id,
            withdraw_verified=False, archive=archive)]
        standing = scope_preconditions(deployment.producer_database,
                                       amf_ue_ngap_id=identity.amf_ue_ngap_id,
                                       policy_type_id=policy_type_id)
        preconditions.extend(standing)
        if standing and request.withdraw_verified:
            cleared[ue].extend(dict(entry) for entry in clear(
                binding, state_database=deployment.producer_database,
                amf_ue_ngap_id=identity.amf_ue_ngap_id, policy_type_id=policy_type_id,
                withdraw_verified=True, archive=archive))
    if preconditions and not request.withdraw_verified:
        raise LiveConsoleError(
            "this sitting needs a precondition met and nobody authorised it: "
            + "; ".join(preconditions)
            + ". Re-run with --withdraw-verified-scope to authorise the withdrawal "
            "(the record is archived first).")

    # -- the declared action space ------------------------------------------ #
    # 2026-09-18 (오너 결정 "(다)로 해"): 셀 축의 기준값을 **라이브에서 읽어** 넘긴다.
    #
    # 선언 상수만 쓰면 배포가 그 값에 있지 않을 때 허가증이 라이브와 어긋나
    # `REJECTED_CONFIG_MISMATCH` 로 **모든 시행이 쓰기 전에 거절된다** — gnb2 는 배포
    # 시점부터 10 dB 인데 선언은 `0.0` 이라, 전력 축을 켠 뒤 완주 판 5 개 중 3 개가
    # 그렇게 죽었다.  되읽기는 이미 "그 셀이 지금 있는 값" 을 보므로 기준값도 같아야 한다.
    #
    # 스트림에 그 셀의 값이 아직 없으면 `None` 을 넘겨 **종전대로 선언 상수**를 쓴다 --
    # 못 읽었다고 조립을 멈추지 않는다.
    # `_FanOutConsumer.__call__` 이 스스로 `drain()` 을 부른다(`build.py:551`) --
    # 2026-09-18 에 여기서 `drain` 을 따로 부르게 고쳤다가 되돌렸다.  검사가 그것을
    # 잡았다: drain 없이도 값이 나온다.
    #
    # 2026-09-23: 한 번만 읽고 못 읽으면 선언 상수 0.0 이 기준값이 됐고(기록도 없이),
    # 라이브가 10 dB 인 셀에서는 **모든 시행이 REJECTED_CONFIG_MISMATCH** 로 쓰기 전에
    # 거절됐다 -- 조립을 멈추지 않은 대가가 판 전체였다.  그래서 전력 축이 노출된 라이브
    # 판은 다른 관측처럼 **기다려 읽고**, 끝내 못 읽은 셀이 있으면 추측하지 않고 이름을
    # 대어 거절한다.  축이 노출되지 않았거나 하드웨어-프리 판이면 종전대로 한 번 읽는다.
    # Only the cells that will carry an attenuation axis need a baseline (Codex review
    # 2026-09-25 #4): with the axis stated for gnb2 alone, gnb1's counter is irrelevant and
    # must not abort the sitting.  Same rule as _axis_specs.
    stated_att = {int(cell) for cell in dict(request.tx_attenuations or {})}
    att_cells = [int(cell) for cell in cells if not stated_att or int(cell) in stated_att]
    if mode == MODE_LIVE and "txAttenuationDb" in request.axes:
        live_baselines = _await_cell_attenuations(
            baseline_tail, kpm_adapter, att_cells, topology, clock,
            wait_ms=CELL_BASELINE_WAIT_MS)
        unread = [cell for cell in att_cells if cell not in live_baselines]
        if unread:
            raise LiveConsoleError(
                "txAttenuationDb is exposed but the live attenuation of cell(s) "
                + ", ".join(str(cell) for cell in unread)
                + f" was not on the KPM stream within {CELL_BASELINE_WAIT_MS} ms; the "
                "baseline is not guessed (a declared 0.0 against a live 10 dB refuses "
                "every trial as REJECTED_CONFIG_MISMATCH)")
    else:
        live_baselines = _live_cell_attenuations(baseline_tail, kpm_adapter,
                                                 att_cells, topology)
    axes = _axis_specs(rows, identities, request, cells, slices,
                       cell_baselines=live_baselines)
    _refuse_uncarried_axes(axes, deployment=deployment,
                           overrides=dict(adapter_overrides or {}),
                           exposed=request.axes)
    declared = axes.declared
    space = _action_space(declared)
    baselines = _baselines(declared)
    functions = _registered_functions(declared)
    catalog = _function_catalog(declared, prb_total=float(
        dict(request.condition or {}).get("prbTotal") or DEFAULT_PRB_TOTAL))
    compatibility = _compatibility(ue_ids, space)
    compatibility_rules = _compatibility_rules(catalog, ue_ids, space)

    # -- how long this deployment holds one observation open ---------------- #
    # Composed once, purely, to read the fold of the objective families' own
    # holds: that is what an unstated observation rule defaults to, so a
    # sitting nobody configured observes exactly what it always did.
    probe_specs = _intent_specs(
        rows, lambda ue: identities[ue].serving_nci, topology_cells)
    try:
        probe = compose_joint(
            intents=probe_specs, deployment_binding=binding.r1.deployment,
            budget_trials=int(request.budget_trials),
            max_catalog_cardinality=int(request.max_catalog_cardinality),
            **axes.compose_kwargs())
    except CatalogTooLargeError as exc:
        raise LiveConsoleError(_ceiling_refusal(exc)) from None
    except JointCompositionError as exc:
        raise LiveConsoleError(str(exc)) from None
    family_hold_ms = int(probe.bundle.target.hold_ms)

    # -- intake: is the operator's own input complete? ----------------------- #
    # Deterministic, and **before** any model call: asking a model to guess at
    # a bound nobody signed is exactly what this checklist exists to prevent
    # (contract v2 section 2.2).
    intents = tuple(row.intent for row in rows)
    kpi_kinds = tuple(dict.fromkeys(
        [row.intent.requirement.kpi for row in rows] + [SERVING_CELL_KPI]))
    settings = request.sitting_settings(
        kpi_kinds, _default_observation_rules(kpi_kinds, family_hold_ms))
    # The owner preference is injected here, at the one place the sitting's
    # authorization is built, so it reaches every reader of it: T's ranking,
    # the Target/Control prompts' ``input.authorization`` and the episode
    # record.  merge_answers carries the preference through, and from_compact
    # keeps the rule and the owner order when a Target answer is expanded, so
    # a model cannot re-rank the owner's own board.
    intents, authorization, settings = merge_answers(
        intents, Authorization.from_intents(intents,
                                            preference=_preference_for(intents)),
        request.answers, settings)
    rows = tuple(replace(row, intent=intent) for row, intent in zip(rows, intents))
    rules = ObservationRules.from_record(settings.get("observation") or {})
    unselected_rule = str(settings.get("unselectedFunctionRule") or RULE_BASELINE)
    stated_retain = settings.get("retain") or request.max_candidates
    retain = None if stated_retain in (None, "", 0) else int(stated_retain)

    # This console's own grammar gives an intent that names no owner its own
    # id as the owner (one sentence, one owner), so "owner" is never actually
    # unset here and asking about it would be noise.  An operator who wants it
    # asked anyway says so: ``settings["requireOwner"] = True``.
    checklist = IntakeChecklist(require_owner=bool(settings.get("requireOwner", False)))
    missing = checklist.check(intents, authorization, settings)
    intake_record: Dict[str, Any] = {
        "missing": [dict(item) for item in missing],
        "answers": {key: dict(value) for key, value in request.answers.items()},
        "rounds": int(request.clarification_round),
        "settings": {key: value for key, value in settings.items()
                     if key != "observation"},
    }
    if missing:
        raise ClarificationNeeded(
            missing, round_index=int(request.clarification_round),
            refused=int(request.clarification_round) >= MAX_CLARIFICATION_ROUNDS)

    # -- real KPI sources, before any model call ---------------------------- #
    observer = kpi_observer
    source_preflight: Dict[str, Any] = {}
    if observer is None:
        by_amf_cells = serving_cells_from_reader(KpmUeAttributionReader(
            read_new_lines=fan_out.consumer(), topology=topology))

        def serving_cells(_raw=by_amf_cells) -> Dict[str, str]:
            # Observation keys carry the UE label; the stream carries the id it holds now.
            # 2026-09-23: **역할에 대응하지 않는 id 는 내지 않는다.**  예전엔
            # `labels.get(key, key)` 로 원번호를 그대로 흘렸다.  KPM 은 재등록한 UE 의
            # 옛 번호를 몇 초 더 싣고, 새 번호는 신원 해석기가 따라잡기 전까지 역할에
            # 안 붙는다 -- 그 사이 `servingCell@530` 같은 키가 `servingCell@ue1` 옆에
            # 나란히 나왔다.  둘 다 해롭다: (1) `cell_goodput_totals` 는 소속 키 집합과
            # goodput 키 집합이 **같아야** 합을 내므로, goodput 이 없는 원번호 하나가
            # 그 표본의 셀 합을 통째로 지웠다(셀 KPI 적용범위 0.53~0.73 의 한 원인);
            # (2) 창 집계에서 그 원번호가 UNKNOWN 이 되면 소속 불명 청소가 **모든** 셀
            # KPI 를 거둬 사업자 요구 I0c 가 빠지고 관측 전체가 무효가 됐다
            # (formal38guarded-20260923T064157 기준 관측: servingCell@530/533/534
            # 적용범위 0.6/0.4/0.07, 역할 키 셋은 1.0).  역할에 안 붙은 id 는 이 판의
            # UE 가 아니거나 신원이 전이 중인 것이고, 어느 쪽이든 소속을 말할 수 없다.
            return role_serving_cells(_raw(), ue_ids, amf_of)
        if mode == MODE_LIVE:
            try:
                observer = build_profile_observer(
                    [intent.requirement for intent in intents], hosts=hosts,
                    profile_document=profile_document, serving_cells=serving_cells)
                # 사업자 에너지 인텐트(cellTxAttenuationDb@<nci>, v4.7): 그 셀의 감쇠를
                # KPM 에서 되읽어 매 폴 발행한다.  인텐트가 없으면 관측원도 없다.
                energy_cells = tuple(sorted({
                    int(str(intent.requirement.scope).split("@", 1)[1])
                    for intent in intents
                    if intent.requirement.kpi == CELL_TX_ATTENUATION_KPI
                    and str(intent.requirement.scope or "").startswith("cell@")}))
                if energy_cells:
                    attenuation_source = KpmCellAttenuationObserver(
                        read_new_lines=fan_out.consumer(), adapter=kpm_adapter,
                        topology=topology, cells=energy_cells)
                    observer = CompositeKpiObserver(
                        (*(observer.observers if isinstance(observer, CompositeKpiObserver)
                           else (observer,)), attenuation_source))
                sources = (observer.observers if isinstance(observer, CompositeKpiObserver)
                           else (observer,))
                for source in sources:
                    if isinstance(source, LiveFlowGoodputObserver):
                        sample, label = source.preflight(), "flow-goodput"
                    elif isinstance(source, LiveTaggedEchoObserver):
                        sample, label = source.sample(), "tagged-echo"
                    else:
                        continue
                    if not sample:
                        raise KpiObserverError(
                            f"{label} source is not ready: " + str(source.failures[-1:]))
                    source_preflight.update(sample)
            except KpiObserverError as exc:
                raise LiveConsoleError(str(exc)) from None
        else:
            observer = TunRateObserver(hosts={}, serving_cells=serving_cells)
    sampler: Optional[_FormationSampler] = None
    if request.timing_mode == TIMING_COLD_START:
        horizon = request.horizon_ms if request.horizon_ms is not None else request.deadline_ms
        sampler = _FormationSampler(
            observer, clock, int(timing.cadence_ms),
            until_ms=None if horizon is None else prep_started_ms + float(horizon))
        _started_samplers.append(sampler)
        if mode == MODE_LIVE:
            sampler.start()

    # -- the predictor every method is given -------------------------------- #
    # 핸드오프 §7 (2026-09-19): 마감값을 **예측기에도** 준다.  18:0x 수정은 아래
    # network_state 에만 넣어서 effect_evidence.echoDeadlineMs 가 계속 {} 였고, 마감
    # 비율 예측이 한 번도 나오지 않았다 -- 예측기는 이 줄에서 이미 만들어진다.
    _deadline_by_ue = intent_deadlines(intents)
    _state = _predictor_state(request, identities, cells, baselines, ue_slices)
    if _deadline_by_ue:
        _state = replace(_state, echo_deadline_ms={**dict(_state.echo_deadline_ms),
                                                   **_deadline_by_ue})
    predictor = JointEffectPredictor(_state)

    # -- T and C ------------------------------------------------------------ #
    models = request.models
    calibration = _latency_calibration(settings, deployment)
    agent_kwargs: Dict[str, Any] = {
        "models": models, "predictor": predictor,
        "generation": dict(settings.get("generation") or {}),
        "latency_calibration": calibration or None,
        "min_validity_ms": rules.min_validity_ms(),
    }
    if role_resolver is not None:
        agent_kwargs["resolver"] = role_resolver
    agents = _SittingAgents(**agent_kwargs)
    agents.calls.append(CallRecord(
        role="intake", model="deterministic", phase=PHASE_INTAKE,
        started_at=prep_start, accepted=True,
        rationale=("the operator's own input is complete: "
                   f"{len(intents)} intents, {len(kpi_kinds)} KPI kinds, "
                   f"{len(rules.kpis)} observation rules"),
        options={}, questions=tuple(dict(item) for item in missing)))

    # 2026-09-18: `echoDeadlineMs` 가 여기 없어서 **마감 예측이 어느 UE 에도 붙지
    # 않았다.**  `PredictorState.from_record` 는 `ues[ue].echoDeadlineMs` 에서만 읽고
    # (`predictor.py:336`), 그 필드의 규약은 "an intent that names a deadline puts it
    # here, nothing else does" 다.  I2d 가 `requirement.deadlineMs = 2000` 을 들고
    # 있는데 아무도 옮기지 않아 `effect_evidence.echoDeadlineMs` 가 `{}` 였고,
    # 그래서 예측표가 `deadlineSuccessRatio` 를 한 번도 내지 못했다 -- 에이전트는
    # I2d 목표를 겨냥할 근거 없이 골라야 했다.
    # `intents` 는 `Intent` 데이터클래스 목록이지 dict 가 아니다 -- 2026-09-18 18:0x 에
    # `.get()` 으로 읽어 `AttributeError` 로 **판 13개를 즉사시켰다.**  필드로 읽는다.
    _echo_deadlines = _deadline_by_ue      # 예측기와 같은 값 -- 한 곳에서만 뽑는다
    network_state = {
        "ues": {ue: {"amfUeNgapId": item.amf_ue_ngap_id,
                     "servingCell": str(item.serving_nci),
                     "observedAt": item.observed_at,
                     "offeredLoadMbps": predictor.state.offered_load_mbps.get(ue),
                     **({"echoDeadlineMs": _echo_deadlines[ue]}
                        if ue in _echo_deadlines else {})}
                for ue, item in identities.items()},
        "cells": {str(cell): {"capacityMbps": predictor.state.cells.get(str(cell))}
                  for cell in cells},
        "prbTotal": predictor.state.prb_total,
        "appliedConfiguration": dict(baselines),
        "unselectedFunctionRule": unselected_rule}
    universe = catalog_cardinality(declared)
    policy = {"retain": retain,
              "kpiDeficitRule": "sum of normalized shortfalls against T0, lower first",
              "catalogCardinality": int(universe), "mustIncludeBaseline": True}
    if request.max_candidates:
        policy["maxCandidates"] = int(request.max_candidates)
    # v2: formation gets the same compact evidence as every other call.  This
    # used to pass ``limit=int(universe)`` on the argument that "a search that
    # may try the whole product should be able to see the whole product" -- and
    # the measured consequence was 144 rows and 93% of the request.  The
    # handoff removes full-product prediction inputs from **both** preparation
    # and the basic monolith, so there is one evidence shape for all of them.
    # Owner instruction 2026-09-19: associations only (see model_effect_evidence).
    evidence = model_effect_evidence(catalog, network_state)
    # Target selects which authorized concessions are worth preparing, and it
    # cannot tell a useful concession from an arbitrary ranked prefix without
    # the initial service state and what this deployment can do to service.
    # ``TargetInputs`` compacts the evidence itself -- families and prose, not
    # Control's per-configuration table -- so this hands over the same object
    # Control gets and lets the input position drop the Cartesian block.
    target_inputs = TargetInputs(intents=intents, authorization=authorization,
                                 network_state=network_state, effect_evidence=evidence)
    # 2026-09-20 오너 승인: Control 도 원본 인텐트·인가를 직접 읽는다.  완성된 T 를
    # 기다리지 않으므로 두 구성 호출을 동시에 시작할 수 있다.
    control_inputs = ControlInputs(
        intents=intents, authorization=authorization,
        function_catalog=catalog, compatibility=compatibility_rules,
        network_state=network_state, effect_evidence=evidence,
        construction_policy=policy)
    phase = (PHASE_CLARIFICATION if request.clarification_round
             else PHASE_FORMATION)
    if prepared is not None:
        # Contract v4 section 4: the board is given, not formed.  No Target
        # call, no Control call, no monolith formation -- which is exactly what
        # makes a trajectory-only ablation an ablation: every arm reads the
        # same rows and the same columns, and only the choice of cell differs.
        contract, controls = prepared
        _refuse_unadmitted_injection(controls, space)
    elif request.method == METHOD_INTERNAL_MONOLITH:
        with _formation_reasks(agents, request, clock, prep_started_ms, prefix):
            (contract, controls), _record = agents.monolith_form(
                MonolithFormInputs(target=target_inputs, control=control_inputs))
    elif request.method == METHOD_BASIC_MONOLITH:
        # The basic monolith is never shown a target contract or a candidate
        # set; these are the executor's own row and column index, so that the
        # arms are judged by exactly the same predicates over the same grid.
        contract = omega_or_sparse(authorization)
        controls = agents._deterministic_controls_for(
            replace(control_inputs, target_contract=contract))
    else:
        # Target and Control no longer depend on each other, so the two
        # formation calls run at once: the sitting waits for the slower one
        # instead of their sum.  Control keeps its own re-ask window.
        import concurrent.futures as _cf
        pool = _cf.ThreadPoolExecutor(max_workers=1)

        def _form_controls():
            with _formation_reasks(agents, request, clock, prep_started_ms, prefix):
                candidates, _rec = agents.form_controls(control_inputs)
            return candidates

        control_future = pool.submit(_form_controls)
        pool.shutdown(wait=False)
        try:
            with _formation_reasks(agents, request, clock, prep_started_ms, prefix):
                contract, _record = agents.form_targets(target_inputs, phase=phase)
        except _AgentClarificationNeeded as asked:
            control_future.result()   # never leave the parallel call running
            intake_record["missing"] = [dict(item) for item in asked.questions]
            raise ClarificationNeeded(
                asked.questions, round_index=int(request.clarification_round),
                refused=int(request.clarification_round) >= MAX_CLARIFICATION_ROUNDS
            ) from None
        controls = control_future.result()


    dropped: List[str] = []
    kept = []
    for candidate in controls.candidates:
        reason = _incompatible(candidate.configuration, ue_ids, baselines)
        if reason is not None and candidate.control_id != "C0":
            dropped.append(f"{candidate.control_id}: {reason}")
            continue
        kept.append(candidate)
    if dropped and prepared is not None:
        # An injected board is used whole or not at all: a column quietly
        # dropped here is a column the other arm still had, and the ablation
        # would no longer be comparing the same C.
        raise LiveConsoleError(
            "the injected C names a control this deployment cannot apply "
            "together: " + "; ".join(dropped))
    controls = replace(controls, candidates=tuple(kept))

    # -- the frozen joint case ---------------------------------------------- #
    if request.restrict_catalog_to_candidates and request.method in (
            METHOD_THREE_AGENT, METHOD_INTERNAL_MONOLITH):
        axes = _restricted_specs(controls, axes)
        frozen = axes.declared
        # C already names the function IDs the Control agent was offered.
        # Regrouping functions after per-UE value restriction renames them
        # (e.g. steer -> steer@UE1-UE3), invalidating otherwise legal C.
        # Restrict the Kernel's axis domains, not the public function IDs.
        frozen_catalog = catalog
        controls, _dropped = validate_control_candidates(
            controls, _action_space(frozen), _baselines(frozen),
            catalog=frozen_catalog,
            compatibility=_compatibility_rules(frozen_catalog, ue_ids,
                                               _action_space(frozen)),
            applied=_baselines(frozen), rule=unselected_rule, retain=retain)
        catalog = frozen_catalog
        if _dropped and prepared is not None:
            raise LiveConsoleError(
                "the injected C does not survive this sitting's own validation: "
                + "; ".join(_dropped))
        dropped.extend(_dropped)

    intent_specs = _intent_specs(
        rows, lambda ue: identities[ue].serving_nci, topology_cells)
    try:
        joint = compose_joint(
            intents=intent_specs, deployment_binding=binding.r1.deployment,
            budget_trials=int(request.budget_trials), hold_ms=rules.hold_ms() or None,
            # Kernel case 마감은 **여기서 넘기지 않는다** -- 이 조성은 미리보기·preflight·
            # 유지(retention) 용이고, 실제 case 는 `_compose` 가 **case 를 여는 순간**
            # `_case_deadline_ms` 로 마감을 넣어 다시 조성한다.
            #   * 2026-09-21 에 B 를 그대로 넘겼다가 되돌렸다: 두 시계는 기준점이 다르다
            #     (에피소드 `elapsed - hardware_wait`, Kernel 은 case 개시부터 벽시계).
            #     cold-start 에서 준비 120초 판은 에피소드가 B 에 닿아도 Kernel 경과가
            #     360초라 `CASE_NOT_TERMINABLE` 이었다.
            #   * 2026-09-23: 넘기는 값은 "case 를 여는 시점에 B 에 남은 것 - 시행 하나의
            #     예비분" 이다.  그러면 실행기가 B 로 멈추는 시각은 언제나 Kernel 마감 뒤다.
            #   * 장비 대기(도려낸 시간)로 B 가 늘어 Kernel 이 먼저 닫히면 `open_trial` 의
            #     `CASE_DEADLINE_REACHED` 를 받아 새 case 로 넘어간다(판을 끝내지 않는다).
            max_catalog_cardinality=int(request.max_catalog_cardinality),
            **axes.compose_kwargs())
    except CatalogTooLargeError as exc:
        raise LiveConsoleError(_ceiling_refusal(exc)) from None
    except JointCompositionError as exc:
        raise LiveConsoleError(str(exc)) from None

    def _unused_event_store_path(prefix: Any, case_id_now: str) -> Path:
        """이 조립만의 추락 대비 이벤트 파일.  이미 있는 이름은 쓰지 않는다.

        **첫 조립만 접미사가 없다** -- 판이 끝날 때 쓰는 파일과 같은 이름
        (`{prefix}-events.jsonl`)이어야 강제 종료된 판의 증거를 읽는 쪽이 평소 자리에서
        찾는다.  재바인드 조립은 형제 파일로 간다.
        """
        tail = (case_id_now[len(case_id):] if case_id_now.startswith(case_id)
                else case_id_now)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", tail).strip("-")
        stem = f"{prefix}-{safe}" if safe else str(prefix)
        path, index = Path(f"{stem}-events.jsonl"), 1
        while path.exists():
            path = Path(f"{stem}-events.{index}.jsonl")
            index += 1
        return path

    def _joint_for(budget: Optional[int], deadline_ms: Optional[int] = None) -> JointComposition:
        return compose_joint(
            intents=intent_specs, deployment_binding=binding.r1.deployment,
            budget_trials=int(request.budget_trials if budget is None else budget),
            hold_ms=rules.hold_ms() or None, deadline_ms=deadline_ms,
            max_catalog_cardinality=int(request.max_catalog_cardinality),
            **axes.compose_kwargs())

    def _case_deadline_ms(composed: JointComposition) -> Optional[int]:
        """The Kernel case deadline for a case opening **now**: what B has left.

        The two clocks disagree on two things, and both are settled here rather than
        by handing the Kernel B itself (2026-09-21, reverted; see the comment at the
        first composition):

        * **origin** -- B runs from ``t0``, the Kernel from case open.  So the case is
          given B *minus what the episode has spent*, spent being elapsed since
          ``t0`` less every our-side interruption (the owner's rule: no clock is
          charged for the bed).  A prepared sitting has no ``t0`` before it runs, so
          it has spent nothing; its first case opens a little early, never late.
        * **the last trial** -- the sitting stops starting trials once B cannot hold
          one more hold and its trailing collection (``trial_reserve_ms``), and stops
          *there* with ``DEADLINE``.  A case deadline at the full B would still be
          ahead of it, and the Kernel would refuse ``CASE_NOT_TERMINABLE`` (49
          episodes, 2026-09-22..23).  So the reserve comes off too.

        Excised time after the case opened moves B but not this deadline, so the
        Kernel may run out first; ``_run_trial`` then rolls onto a fresh case
        (``kernel-deadline-rollover``) instead of ending the sitting.
        """
        if request.deadline_ms is None:
            return None
        sitting = sitting_ref.get("sitting")
        if sitting is not None and sitting.t0_monotonic_ms is not None:
            spent = (float(clock.monotonic_ms()) - float(sitting.t0_monotonic_ms)
                     - sitting._hardware_wait_ms())
            reserve = sitting._trial_reserve_ms()
        else:
            spent = (float(clock.monotonic_ms()) - prep_started_ms - _exempt_ms()
                     if request.timing_mode == TIMING_COLD_START else 0.0)
            contracts = list(composed.bundle.measurements) + [
                item for pair in dict(composed.steering_measurements).values() for item in pair]
            trailing = max((float(value) for source in _echo_sources(observer)
                            for value in source.deadlines_ms), default=0.0)
            reserve = trial_reserve_ms(
                max(int(item.hold_ms) for item in contracts) if contracts else family_hold_ms,
                min(int(item.cadence_ms) for item in contracts) if contracts
                else int(timing.cadence_ms), trailing)
        return max(1, int(float(request.deadline_ms) - spent - reserve))

    def _compose(identities: Mapping[str, LiveUeObservation], case_id_now: str,
                 initial_configuration: Optional[Mapping[str, Any]] = None,
                 budget_trials: Optional[int] = None, _attempt: int = 0) -> Dict[str, Any]:
        """Participants and the joint case over them, for the identities given.

        Called once for the search case and again whenever a UE re-registered
        under a new id between trials (docs/design/ue-identity-continuity.md).
        ``budget_trials`` recomposes the same joint with a smaller trial bound.
        """
        joint_now = joint if budget_trials is None else _joint_for(budget_trials)
        # -- participants -------------------------------------------------------- #
        builders: List[Any] = []
        overrides = dict(adapter_overrides or {})
        steering: List[SteeringParticipantSpec] = []
        #: The id each steering participant's policy body is pinned to.
        steering_pins = {spec.ue_id: int(identities[spec.ue_id].amf_ue_ngap_id)
                         for spec in axes.steering}
        for axis_spec in axes.steering:
            ue = axis_spec.ue_id
            identity = identities[ue]
            other = next((cell for cell in topology_cells if cell != identity.serving_nci),
                         identity.serving_nci)
            if other == identity.serving_nci:
                raise LiveConsoleError("the deployment names only one cell; nothing can be steered")
            pin_deployment = LivePinToCellDeployment(
                home_nci=int(identity.serving_nci), target_nci=int(other),
                ue_scope_id=str(identity.amf_ue_ngap_id), topology=topology,
                r1_deployment=binding.r1.deployment)
            first_family = next(row.family for row in rows if row.ue_id == ue)

            def factory(kernel: Any, composed: JointComposition, participant: SteeringParticipantSpec,
                        *, _pin=pin_deployment, _identity=identity, _family=first_family) -> Any:
                # 2026-09-20: the factory is called again whenever the role's AMF id
                # changes (joint_runtime._builder_following_the_ue), so a body built
                # after a re-registration names the UE the role *is* -- a hand-back
                # written against the dead id reached no radio at all (board 123728).
                current = participant.amf_of() if participant.amf_of is not None else None
                if current is not None and str(current) != str(_pin.ue_scope_id):
                    _pin = replace(_pin, ue_scope_id=str(current))
                    _identity = replace(_identity, amf_ue_ngap_id=int(current))
                minimum, maximum = composed.steering_measurements[participant.ue_id]
                builder = LivePolicyBuilder(
                    kernel=kernel,
                    contracts={"measurement_min": minimum, "measurement_max": maximum,
                               "target": composed.bundle.target,
                               "case_policy": composed.bundle.case_policy,
                               "harm": composed.bundle.harm,
                               "actuator": composed.actuators_by_axis[participant.axis]},
                    deployment=_pin, identity=_identity, case_id=case_id_now,
                    policy_type_discovery=discovery, capability_manifest=deployment.capability,
                    objective_kind=A1P_OBJECTIVE_KIND[_family],
                    axis=participant.axis, scope_key=f"ue@{participant.ue_id}")
                builders.append(builder)
                return builder

            key = f"r1-steer@{ue}"
            # The readback asks the role again at every read instead of holding the id
            # this loop just captured (2026-09-16 attempts 130 and 144: ue1 re-registered
            # 24 -> 26 after composition, KPM carried 26, and every PREPARE still asked
            # for 24).  A numeric label resolves to itself, so this is a no-op for the
            # sittings that address a UE by number.
            steering.append(SteeringParticipantSpec(
                ue_id=ue, identity=identity, policy_port=port, policy_builder_factory=factory,
                adapter_key=key, adapter_override=overrides.get(key),
                amf_of=(lambda _ue=ue: amf_of(_ue)) if amf_of is not None else None))

        # Every non-steering axis is one supplementary participant, and the same
        # loop composes all five kinds: an injected adapter is used as it stands
        # (that is the hardware-free seam), and everything else goes to the radio
        # through the action producer.  ``_refuse_uncarried_axes`` has already
        # refused, by name, any axis this deployment cannot carry, so nothing here
        # falls back to a mock.
        supplementary: List[SupplementaryParticipantSpec] = []
        composed_caps: List[Any] = []
        action_state_dir: Optional[Path] = None
        service_horizon = request.horizon_ms if request.horizon_ms is not None else request.deadline_ms

        def _sitting_end_ms() -> Optional[float]:
            """``t0 + H`` on the monotonic clock, **moved by the exempt time so far**.

            Read at every policy write, not once at composition: 2026-09-19 board
            093728 waited 173 s for a re-registered UE, the settled pfWeight policy
            expired at t0 + 480 s while B -- relieved of the wait -- still ran, and
            the readback saw 4.0 -> 1.0 and locked the trial down.
            """
            if request.timing_mode != TIMING_COLD_START or service_horizon is None:
                return None
            return prep_started_ms + float(service_horizon) + _exempt_ms()
        _uncomposed: List[str] = []   # 조립하지 못한 축과 그 이유 (조용히 빠지지 않게)
        for spec in axes.supplementary:
            key = _adapter_key(spec)
            if key in overrides:
                supplementary.append(SupplementaryParticipantSpec(
                    action_id=spec.action_id, axis=spec.axis, adapter_key=key,
                    adapter=overrides[key]))
                continue
            producer = deployment.action_producer
            if action_state_dir is None:
                action_state_dir = ((producer.state_dir if producer is not None else None)
                                    or (deployment.r1_state_dir / "action")) / case_stamp
                action_state_dir.mkdir(parents=True, exist_ok=True)
            window_ms = supplementary_policy_window_ms(
                enforced_timeout_ms=timing.enforced_timeout_ms,
                r1_deadline_ms=int(binding.r1.deadline_ms),
                hold_ms=joint_now.bundle.target.hold_ms,
                freshness_bound_ms=timing.freshness_bound_ms)
            attribution_provider = None
            readback_attribution_provider = None
            # Both UE-scoped allocation families follow the UE across a steer.  A cap
            # composed without the resolver keeps the cell the UE was first observed
            # on, so "move UE3 to gNB2 and cap UE3" (reference R5) would build its
            # policy for the cell UE3 just left and read the counter there too.
            # 2026-09-18: 보조 축이 **전부 UE 범위**라고 가정하고 있었다.  전력 축은
            # 셀 범위(`TxAttenuationAxisSpec.cell_nci`)라 `spec.ue_id` 가 없고, 켜는
            # 순간 `AttributeError: 'TxAttenuationAxisSpec' object has no attribute
            # 'ue_id'` 로 **판마다 즉사**했다.
            #
            # 조립기 쪽은 이미 준비돼 있다(`build.py`: `declared.scope_kind ==
            # "NRCellDU"` 면 `KpmCellConfigReader`).  그 주석이 규약도 말한다 --
            # "The cell this acts on is the one the controlled UE is served by --
            #  a cell-scoped control needs no UE identity of its own".
            # 그러므로 셀 축에는 **그 셀이 서빙하는 UE** 를 controlled 로 준다.
            controlled_ue = getattr(spec, "ue_id", None)
            if controlled_ue is None:
                cell = int(getattr(spec, "cell_nci", 0))
                controlled_ue = next(
                    (ue for ue, obs in identities.items()
                     if int(getattr(obs, "serving_nci", -1)) == cell), None)
                if controlled_ue is None:
                    # 그 셀에 붙은 UE 가 없으면 이 축은 이 판에서 조립하지 않는다.
                    # 조용히 빠지지 않도록 이유를 남긴다.
                    _uncomposed.append(f"{spec.axis}: no UE is served by cell {cell}")
                    continue
            steer = None
            if axis_kind(spec.axis) in ("pfWeight", "dlPrbCap"):
                steer = next((item for item in axes.steering if item.ue_id == controlled_ue), None)
            # A UE that is momentarily unaddressable is waited for, not refused.  Composition
            # used to fail closed the instant its UE left the stream, and on 2026-09-16 that
            # killed attempts 120, 127 and 128 after both model calls had been paid for: the
            # gate had passed with all three UEs attributed and one of them left between the
            # identity pin and this build.  A UE the radio dropped is a hardware fault, not a
            # cost of the control being measured (the owner's call, same day), so this waits
            # for the role to be addressable again and re-pins to the id it holds now.  Inside
            # a case the id stays pinned -- that part is unchanged; only the pin's moment moves.
            attempt = 0
            while True:
                if steer is not None:
                    _waited_from, _waited_at = float(clock.monotonic_ms()), clock.now()
                    # One wait is capped at HARDWARE_WAIT_CAP_MS and every wait of the
                    # board shares EXCISED_TOTAL_CAP_MS (owner 2026-09-23: "너무 많이
                    # 기다리지 마라").
                    _left = EXCISED_TOTAL_CAP_MS - _exempt_ms()
                    if _left <= 0:
                        raise LiveConsoleError(
                            f"{HARDWARE_UNAVAILABLE}: our-side interruptions already add up "
                            f"to {round(_exempt_ms() / 1000)} s of the "
                            f"{round(EXCISED_TOTAL_CAP_MS / 1000)} s one board may excise")
                    try:
                        identities[controlled_ue] = _await_addressable(
                            reader=reader, amf_of=amf_of, ue=controlled_ue, clock=clock,
                            freshness_ms=timing.freshness_bound_ms, allowed_cells=steer.cells,
                            previous=identities[controlled_ue],
                            max_wait_ms=min(ROLE_ADDRESSABLE_WAIT_MS, _left))
                    finally:
                        _charge_composition_wait(_waited_from, _waited_at)
                    # The provider re-resolved the cell and epoch on every read but
                    # asked for a UE number frozen in this default argument, so a UE
                    # that re-registered mid-episode was looked up under the id it no
                    # longer had and the contracted readback produced no observation
                    # at all.  That is what locked three trials of attempt 158 down --
                    # r1-priority@ue1, r1-cap@ue1 and r1-steer@ue2, the two UEs whose
                    # identities moved during the episode, each "apply dispatched,
                    # readback unavailable ... answered UNKNOWN".  The steering
                    # readback was given the live resolver this morning; the
                    # supplementary axes were left behind.  Resolve per read here too,
                    # and keep the composition-time number as the fallback for the
                    # moments the resolver's own entry is stale.
                    _pinned = int(identities[controlled_ue].amf_ue_ngap_id)
                    if amf_of is None:
                        _amf_now = lambda _amf=_pinned: _amf          # noqa: E731
                    else:
                        def _amf_now(_ue=controlled_ue, _fallback=_pinned):
                            resolved = amf_of(_ue)
                            return _fallback if resolved is None else int(resolved)
                    # Two providers, deliberately.  The policy BODY keeps the id the
                    # case was composed over -- a trial's transaction addresses that
                    # one and a rebind between trials is the only way to a new one --
                    # while the READBACK follows the role, because a UE that
                    # re-registered answers to a new id and asking KPM for the old one
                    # produces no observation at all (attempt 158 lost three trials to
                    # that).  Handing the live resolver to both is what made the old
                    # participant build a body for ue id 35 after a re-registration.
                    attribution_provider = joint_serving_attribution(
                        reader=reader, ue_id=controlled_ue, allowed_cells=steer.cells,
                        now=clock.now, freshness_bound_ms=timing.freshness_bound_ms,
                        amf_of=lambda _amf=_pinned: _amf)
                    readback_attribution_provider = joint_serving_attribution(
                        reader=reader, ue_id=controlled_ue, allowed_cells=steer.cells,
                        now=clock.now, freshness_bound_ms=timing.freshness_bound_ms,
                        amf_of=_amf_now)
                try:
                    participant = _build_supplementary(
                    deployment=deployment, action_id=SUPPLEMENTARY_BY_KIND[axis_kind(spec.axis)],
                    identity=identities[_first_ue_row(rows).ue_id], controlled=identities[controlled_ue],
                    cap_candidates=spec.caps if isinstance(spec, CapAxisSpec) else (),
                    objective_floor_kbps=request.objective_floor_kbps,
                    controlled_reserve_kbps=request.controlled_reserve_kbps,
                    calibration_ref=request.cap_calibration_ref, state_dir=action_state_dir,
                    clock=clock, kpm_tail=fan_out.consumer(), kpm_adapter=kpm_adapter,
                    freshness_bound_ms=timing.freshness_bound_ms,
                    injected_port=(action_policy_ports or {}).get(SUPPLEMENTARY_BY_KIND[axis_kind(spec.axis)]),
                    validity={}, axis=spec.axis, controlled_scope_key=f"controlledUe@{controlled_ue}",
                    validity_provider=lambda command, _w=window_ms: {
                        "notBefore": clock.now(),
                        "notAfter": _plus_ms(clock.now(), held_policy_window_ms(
                            _w, now_ms=float(clock.monotonic_ms()), sitting_end_ms=_sitting_end_ms()))},
                    # 2026-09-18: 어댑터 키는 **그 축의 범위**로 이름 지어야 한다.
                    # `state_tag` 가 그대로 `adapter_key = f"{adapter}{state_tag}"`
                    # (`build.py:1708`)가 되는데, 셀 범위 축에 `@{controlled_ue}` 를 붙이면
                    # `r1-power@ue2` 가 된다 -- 셀 12345678 의 축인데 ue2 이름이 붙고,
                    # 심지어 그 셀을 쓰는 UE 도 아니다(ue1·ue3 가 쓴다).  판 01:37·02:27 기록이
                    # 그 증거이고, 되읽기 실패가 `r1-power@ue2 answered UNKNOWN` 으로 찍혔다.
                    state_tag=("@%s" % _scope_target(spec)
                               if getattr(spec, "ue_id", None) is None
                               else f"@{controlled_ue}"),
                    attribution_provider=attribution_provider,
                    readback_attribution_provider=readback_attribution_provider)
                except LiveConsoleError as exc:
                    attempt += 1
                    if steer is None or "ATTRIBUTION_UNAVAILABLE" not in str(exc) \
                            or attempt >= SUPPLEMENTARY_BUILD_ATTEMPTS:
                        raise
                    # The UE went away again between the wait and the baseline read; go round.
                    clock.sleep_ms(2000)
                    continue
                break
            composed_caps.append(participant)
            supplementary.append(SupplementaryParticipantSpec(
                action_id=spec.action_id, axis=spec.axis, adapter_key=participant.adapter_key,
                adapter=participant.adapter, counter_reader=participant.counter_reader,
                describe=participant.describe))

        if _uncomposed:
            # 조립하지 못한 축은 **이름을 대고 말한다.**  조용히 빠지면 그 축이 있는 줄
            # 알고 판을 읽게 되고, 그것이 오늘 밤 내내 고친 종류의 결함이다.
            print("composition left an exposed axis out: " + "; ".join(_uncomposed),
                  file=sys.stderr, flush=True)

        # Give steering the operation journal the supplementary axes have always
        # had.  It lands beside theirs, in the same per-case directory, so a run's
        # steering write count and duration are a file rather than a
        # reconstruction.  2026-09-17: the producer's ``503 AIC_INTERNAL_ERROR``
        # is the only steering failure we see, and it falls entirely on writes that
        # move a UE BACK to its baseline cell: 4 of 10 such restores against 0 of
        # 204 moves away and 0 of 237 no-op plans (p=1.2e-07 over 213 sittings).
        # Restores are rare, so the failure hid for weeks, and it looked at first
        # like a late-in-the-episode effect -- a restore can only follow a move,
        # so it is always late.  The writes themselves could not be timed, which
        # is what this journal fixes; the supplementary axes go to a different
        # producer, so their journals cannot answer for this one.
        # Only the operation journal: a binding
        # journal would make steering durable across cases, which the joint
        # composition does not want.
        if action_state_dir is not None:
            steering = [replace(spec, operation_journal=JsonlR1OperationJournal(
                action_state_dir / f"{spec.key}-operations.jsonl"))
                if spec.adapter_override is None and spec.operation_journal is None
                else spec for spec in steering]

        runtime_kwargs: Dict[str, Any] = {}
        if counter_sample_loaders is not None:
            runtime_kwargs["counter_sample_loaders"] = dict(counter_sample_loaders)
        if arrival is not None:
            runtime_kwargs["arrival"] = arrival
        # 이벤트를 **쌓이는 대로 디스크에** 남긴다.  기본 저장소는 메모리이고 파일 저장은
        # 판이 끝날 때 한 번인데, 러너 timeout 이나 서비스 재기동으로 판이 강제 종료되면
        # **그 판의 증거가 통째로 사라진다** -- 시행은 실제로 돌았고 라디오도 만졌는데
        # 기록만 없어, 분모에서도 빠지고 무엇이 일어났는지도 알 수 없다(2026-09-21).
        #
        # 경로는 **조립마다** 달라야 한다.  `_compose` 는 UE 재바인드마다 다시 불리고
        # (`{case_id}/rebind-N`), 같은 파일에 새 저장소를 열면 그 저장소가 앞 조립의
        # 이벤트를 재생해 `replayed idempotency key` 로 판이 죽는다 -- 판 하나에 파일
        # 하나로 뒀다가 정확히 그렇게 됐다(2026-09-21, 시험 5건이 같은 사인으로 실패).
        # 재생은 애초에 원하는 동작이 아니다: 이 저장소는 **추락 대비 기록**이지
        # 이어받기가 아니므로, 이미 있는 파일은 절대 다시 열지 않는다.
        # The case opens on the next line, so its deadline is read now -- after any
        # wait above, not before it (the supplementary loop can wait minutes).
        case_deadline = _case_deadline_ms(joint_now)
        if case_deadline is not None:
            joint_now = _joint_for(budget_trials, case_deadline)
        runtime = build_joint_live_runtime(
            joint=joint_now, binding=binding, steering=steering, supplementary=supplementary,
            reader=reader, now=clock.now, monotonic_ms=clock.monotonic_ms,
            sleep_ms=clock.sleep_ms, case_id=case_id_now,
            event_store_path=_unused_event_store_path(prefix, case_id_now),
            initial_configuration=initial_configuration, **runtime_kwargs)

        # 2026-09-20, board 20260919T193249: the supplementary loop above re-pins a
        # UE that re-registered while it waited (``_await_addressable`` writes the
        # new id into ``identities``), but the steering participants were built
        # before it with the old id.  One case then addressed ue1 as 331 for
        # steering and 334 for everything else; the sitting's identities said
        # 334, so no rebind ever fired, and the steering policy went out for a
        # UE that no longer existed (AIC_SCOPE_NOT_FOUND).  A case must address
        # one id per UE: if a pin moved during composition, compose again over
        # the ids as they are now.  Nothing has been written yet.
        moved = [ue for ue, pinned in steering_pins.items()
                 if int(identities[ue].amf_ue_ngap_id) != pinned]
        if moved and _attempt < 3:
            return _compose(identities, case_id_now, initial_configuration=initial_configuration,
                            budget_trials=budget_trials, _attempt=_attempt + 1)
        if moved:
            raise LiveConsoleError(
                "UE ids kept moving while the case was composed ("
                + ", ".join(moved) + "); the case would address two ids for one UE")
        return {"runtime": runtime, "builders": builders, "composed_caps": composed_caps,
                "steering": steering, "supplementary": supplementary,
                "runtime_kwargs": runtime_kwargs}

    current: Dict[str, Any] = {"composition": _compose(identities, case_id)}
    runtime = current["composition"]["runtime"]
    builders = current["composition"]["builders"]
    composed_caps = current["composition"]["composed_caps"]

    def retention_factory(applied: Optional[Mapping[str, Any]] = None) -> JointLiveRuntime:
        """A second case over the same participants, for the retention step.

        It opens on what the radio holds *now* (the search case's finalized
        trials moved it off the declared baseline), or the Write Gateway would
        refuse the permit as a configuration mismatch.
        """
        composed = current["composition"]
        # One trial, so the case is terminable once it is spent (with the search's
        # budget it answered CASE_NOT_TERMINABLE after its single trial).
        return build_joint_live_runtime(
            joint=_joint_for(1), binding=binding, steering=composed["steering"],
            supplementary=composed["supplementary"],
            reader=reader, now=clock.now, monotonic_ms=clock.monotonic_ms,
            sleep_ms=clock.sleep_ms, case_id=f"{case_id}/retention",
            initial_configuration=applied, **composed["runtime_kwargs"])

    def rebind_factory(identities_now: Mapping[str, LiveUeObservation], changed: Sequence[str],
                       applied: Mapping[str, Any], index: int,
                       remaining_trials: Optional[int] = None) -> Dict[str, Any]:
        """Re-address re-registered UEs: observe their new ids, free both scopes, recompose.

        The frozen catalog is keyed by UE label, so the new case admits the same
        candidates; only the ids its participants address change.  The old id's
        UE context is gone with the re-registration, so its records are archived
        and withdrawn even when verified.
        """
        fresh = dict(identities_now)
        applied = dict(applied)
        freed: Dict[str, List[Dict[str, Any]]] = {}
        for ue in changed:
            previous = fresh[ue]
            observed = observe_selected_ue(
                reader, now=clock.now, sleep_ms=clock.sleep_ms,
                freshness_ms=timing.freshness_bound_ms, amf_ue_ngap_id=_current_amf(amf_of, ue))
            fresh[ue] = observed
            # A re-registered UE attaches wherever the radio put it, not where the old case
            # left it: the new case opens on the cell the UE is observed on now.
            if f"servingCell@{ue}" in applied:
                applied[f"servingCell@{ue}"] = str(observed.serving_nci)
            freed[ue] = []
            for amf, verified in ((previous.amf_ue_ngap_id, True), (observed.amf_ue_ngap_id, False)):
                archive_path = Path(f"{prefix}-scope-archive-{ue}-rebind{index}-{amf}.json")

                def archive(records: Sequence[Mapping[str, Any]], *, _path=archive_path, _amf=amf) -> None:
                    _write_scope_archive(_path, records, at=clock.now(), amf_ue_ngap_id=_amf,
                                         database=deployment.producer_database)

                freed[ue].extend(dict(entry) for entry in clear(
                    binding, state_database=deployment.producer_database, amf_ue_ngap_id=amf,
                    policy_type_id=policy_type_id, withdraw_verified=verified, archive=archive))
        # The new case holds only what the sitting has left, so it closes when the sitting's
        # budget does (a case with trials to spare refuses termination).
        composed = _compose(fresh, f"{case_id}/rebind-{index}", initial_configuration=applied,
                            budget_trials=remaining_trials)
        current["composition"] = composed
        return {"runtime": composed["runtime"], "identities": fresh, "freed": freed,
                "initialConfiguration": applied,
                "builders": composed["builders"], "supplementary": tuple(composed["composed_caps"])}

    # -- C onto the frozen catalog ------------------------------------------- #
    mapping: Dict[str, str] = {}
    unmapped: List[str] = []
    for candidate in controls.candidates:
        found = _candidate_id_for(runtime, candidate.configuration)
        if found is None:
            unmapped.append(f"{candidate.control_id}: {dict(candidate.configuration)} "
                            "is not a candidate of the frozen catalog")
            continue
        mapping[candidate.control_id] = found
    if unmapped and prepared is not None:
        raise LiveConsoleError(
            "the injected C names an axis value the frozen catalog does not admit: "
            + "; ".join(unmapped))
    controls = replace(controls, candidates=tuple(
        item for item in controls.candidates if item.control_id in mapping))
    if not controls.candidates:
        raise LiveConsoleError(
            "no control candidate maps onto the frozen catalog: " + "; ".join(unmapped))

    # The formed T may name intermediate authorized deadlines. Measure those
    # too, alongside D0; never judge all rows using the selected row's ratio.
    if isinstance(observer, CompositeKpiObserver):
        observer.include_contract_deadlines(contract)

    preflight: Dict[str, Any] = {
        "at": prep_start, "sessionMode": mode, "injectedPorts": list(injected),
        "caseId": case_id, "objective": joint.bundle.family, "method": request.method,
        "deployment": deployment.summary(),
        "intents": [{"intentId": row.intent.intent_id, "sentence": row.intent.sentence,
                     "family": row.family, "ueId": row.ue_id or None,
                     "cellScope": row.cell_scope, "namedCell": row.named_cell,
                     "requirement": row.intent.requirement.to_record()} for row in rows],
        "observedUes": {ue: {"amfUeNgapId": obs.amf_ue_ngap_id, "guAmI": dict(obs.gu_ami),
                             "servingCell": obs.serving_nci, "e2Node": obs.e2_node,
                             "connectionEpoch": obs.connection_epoch,
                             "observedAt": obs.observed_at}
                        for ue, obs in identities.items()},
        "scopeOccupants": occupants, "scopeCleared": cleared,
        "preconditions": list(preconditions),
        "kpmSlots": kpm_slot_occupancy(deployment.kpm_jsonl_path),
        "policyTypeIds": list(discovery.get("policyTypeIds") or ()),
        "cells": list(cells),
        "registeredFunctions": functions,
        "exposedAxisKinds": list(request.axes),
        "axisScopes": dict(joint.axis_scopes),
        "catalogCeiling": int(request.max_catalog_cardinality),
        "functionCatalog": catalog.to_record(),
        "compatibility": list(compatibility),
        "compatibilityRules": compatibility_rules.to_record(),
        "unselectedFunctionRule": unselected_rule,
        "measurementRules": rules.to_record(),
        "intake": dict(intake_record),
        "predictor": dict(predictor.describe()),
        "generationOptions": {role: agents.options_for(role, models.model_for(
            role if role in ROLES else "monolith")).to_record()
            for role in ("target", "control", "trajectory", "monolith-form",
                         "monolith-select", "basic-monolith")},
        "droppedCandidates": dropped,
        "unmappedControls": unmapped,
        "roleModels": models.to_record(),
        "kpiObserver": type(observer).__name__,
        "kpiSources": observer.describe() if hasattr(observer, 'describe') else {},
        "sourcePreflight": source_preflight,
    }
    # Contract v4 section 1: preparation costs the virtual clock what the
    # model calls really took, so a ``cold-start`` sitting whose Target and
    # Control answers were slow has genuinely spent that much of B before its
    # first trial.  ``LIVE`` needs nothing: the wall clock has already moved,
    # and sleeping again would charge the same seconds twice.
    if mode != MODE_LIVE:
        preparation_ms = sum(float(getattr(call, "latency_ms", 0.0) or 0.0)
                             for call in agents.calls)
        if preparation_ms > 0:
            if sampler is not None:
                sampler.sample_for(int(preparation_ms))
            else:
                clock.sleep_ms(int(preparation_ms))
    prep_end = clock.now()
    prep_ms = float(clock.monotonic_ms()) - prep_started_ms
    board = {"injected": prepared is not None,
             "tHash": content_hash(contract.to_record()),
             "cHash": content_hash(controls.to_record())}
    if prepared is not None:
        preflight["preparedInjected"] = dict(board)
    # Not ``timing``: that name is the LiveTiming the compose/rebind closures read later.
    timing_record: Dict[str, Any] = {
        "prepStart": prep_start, "prepEnd": prep_end, "prepMs": prep_ms,
        "timingMode": request.timing_mode}
    if request.timing_mode == TIMING_COLD_START:
        # ``t0`` is the release of the required inputs, which is where this
        # function began -- so B has been running throughout the preparation
        # above and :meth:`AgentSitting.run` resets no clock.
        timing_record["t0"] = prep_start
        if (request.deadline_ms is not None
                and prep_ms >= float(request.deadline_ms)):
            preflight["deadlineDuringPreparation"] = {
                "prepMs": prep_ms, "deadlineBMs": int(request.deadline_ms),
                "detail": ("B expired while T and C were being formed; the sitting "
                           "ends DEADLINE with nothing applied, which is an outcome "
                           "and not an error")}
    # Ω 를 판마다 한 번 펼친다.  `omega_targets` 는 펼칠 수 없는 인가에서 contract 가
    # 든 것으로 되돌아가므로(좁은 비교는 정직하지만 crash 는 아니다) 그 되돌림을 그대로
    # 쓴다 -- 두 번째 정의를 만들지 않는다.
    evaluation_contract = omega_contract(contract)
    # 2026-09-23 (codex 감사 #1): 격자는 **Ω 위에** 선다.  칸은 이미 Ω 이름으로 채워지는데
    # (`judge_against(evaluation_contract)`) 격자의 contract 가 형성된 T 면
    # `to_record()["targets"]` 는 T 이름을, `cells` 는 Ω 이름을 말하고, 인자 없이 부른
    # `best_attained()` 는 T 에서 Ω 이름을 찾는다 -- 같은 철자의 **다른 벡터**다.
    grid = Grid(contract=evaluation_contract, controls=controls)
    sitting = AgentSitting(
        deployment=deployment, request=request, mode=mode, rows=rows, joint=joint,
        runtime=runtime, identities=identities, contract=contract,
        evaluation_contract=evaluation_contract, controls=controls,
        grid=grid, catalog_of_control=mapping, action_space=space, baselines=baselines,
        observer=observer, agents=agents, clock=clock, stamp=sitting_stamp,
        evidence_prefix=prefix, preflight=preflight, policy_port=port,
        policy_builders=builders, supplementary=tuple(composed_caps),
        rules=rules, predictor=predictor, catalog=catalog,
        catalog_cardinality=int(universe),
        compatibility=compatibility_rules, unselected_rule=unselected_rule,
        intake=dict(intake_record), injected=injected,
        unmapped=tuple(unmapped), retention_factory=retention_factory,
        rebind_factory=rebind_factory, amf_of=amf_of,
        composition_wait_ms=composition_wait_ms[0],
        composition_wait_log=composition_wait_log,
        timing=timing_record, prepared=board,
        # An initial boundary the operator declared without a source time is
        # the release of the inputs this sitting was prepared from, so it is
        # stamped once here with the preparation declaration time.  The request
        # keeps what was written: the episode record has to show both what the
        # operator stated and what the sitting resolved it to.
        boundaries=[{**item, "at": (str(item.get("at") or "").strip() or prep_start)}
                    for item in request.boundaries],
        t0_monotonic_ms=(prep_started_ms
                         if request.timing_mode == TIMING_COLD_START else None),
        service_trace=sampler.rows if sampler is not None else [],
        formation_sampler=sampler)
    # From here on a recomposition (a rebind) charges its waits to the sitting itself
    # and reads B off the sitting's clocks -- never off a copy taken now.
    sitting_ref["sitting"] = sitting
    return sitting


class FormationFailed(LiveConsoleError):
    """No usable T/C arrived within the formation allowance; nothing was formed for it."""


#: The sitting never started: no usable T/C arrived within the formation
#: allowance (2026-09-19, no deterministic stand-in).  Written to the board's
#: episode file so a formation failure is counted per method.
FORMATION_FAILURE = "FORMATION_FAILURE"


def _write_formation_failure(prefix: Any, request: Any, agents: Any,
                             exc: "DecisionUnavailable", clock: Any) -> Path:
    """The episode file of a sitting that failed at formation: who was asked,
    how often, why each answer was refused, and the termination."""
    path = Path(f"{prefix}-episode.json")
    record = exc.record
    document = {
        "schemaVersion": EPISODE_SCHEMA, "method": request.method,
        "episodeStarted": False,
        "termination": {"reason": FORMATION_FAILURE,
                        "detail": f"{record.role} produced no usable answer after "
                                  f"{record.repair_retries} re-asks: {exc.reason}"},
        "formation": {"role": record.role, "phase": record.phase,
                      "reAsks": int(record.repair_retries), "reason": exc.reason,
                      "refusals": [g.get("refusedBecause") for g in record.generations]},
        "calls": [call.to_record() for call in agents.calls],
        "trials": [], "at": clock.now() if hasattr(clock, "now") else None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=1, default=str),
                    encoding="utf-8")
    return path


@contextmanager
def _formation_reasks(agents: Any, request: Any, clock: Any, started_ms: float,
                      prefix: Any = None):
    """Re-ask a formation call within the formation allowance, never substitute.

    Owner instruction 2026-09-19: the executor forms no T and no C in a
    model's place.  A refused answer is asked again, with the reason, while
    the first-proposal allowance (``formation_deadline_ms``) still has time;
    with no allowance stated the single repair stands.  Out of time, the
    sitting is not composed.
    """
    budget = request.formation_deadline_ms
    # 2026-09-29 (board 938): Target and Control form in parallel on one ``agents``; the
    # first to finish used to clear ``may_reask`` while the other was still re-asking, so a
    # Target slower than Control (local qwen3: 116 s vs 52 s) got one repair, not the
    # allowance.  The hook is cleared only when the last holder leaves.
    with _FORMATION_REASK_LOCK:
        agents._formation_reask_holders = getattr(agents, "_formation_reask_holders", 0) + 1
        agents.may_reask = (None if budget is None else
                            (lambda *_: float(clock.monotonic_ms()) - started_ms
                             < float(budget)))
    try:
        yield
    except DecisionUnavailable as exc:
        written = (None if prefix is None
                   else _write_formation_failure(prefix, request, agents, exc, clock))
        raise FormationFailed(
            f"{FORMATION_FAILURE}: formation produced no usable answer ({exc.record.role}, "
            f"{exc.record.repair_retries} re-asks): {exc.reason}"
            + (f" -- recorded in {written}" if written else "")) from None
    finally:
        with _FORMATION_REASK_LOCK:
            agents._formation_reask_holders -= 1
            if agents._formation_reask_holders <= 0:
                agents.may_reask = None


_FORMATION_REASK_LOCK = threading.Lock()


def held_policy_window_ms(trial_window_ms: int, *, now_ms: float,
                          sitting_end_ms: Optional[float]) -> int:
    """How long a supplementary policy written now must stay on the radio.

    A trial that settles live keeps its binding -- and so the scope -- for the
    rest of the sitting, and the sitting reports that configuration as applied.
    With only the per-trial window the producer restored the baseline under it:
    2026-09-15 attempt 51 settled pfWeight@ue1 4.0 at 00:55:43, KPM showed 1.0
    again at 00:58:38 (window 176 s), and the episode still ended at 01:00:45
    reporting the deployment holding 4.0.  So the window reaches the end of the
    service horizon plus the conclusion reserve; a failed trial is still rolled
    back by its own DELETE, and expiry stays the backstop just after the sitting.
    """
    if sitting_end_ms is None:
        return int(trial_window_ms)
    # Plus one trial window: a trial admitted just before the horizon runs past
    # it, and a settled policy it does not rewrite must not expire under it.
    # 2026-09-19 board 093728: pfWeight@ue2 settled by trial 1 expired at the
    # horizon + reserve (09:45:53) while trial 3, started 09:45:15, was still
    # in its hold; the reread saw 4.0 -> 1.0, EXECUTION_ERROR, lockdown.
    remaining = int(sitting_end_ms - now_ms) + int(trial_window_ms) + CONCLUDE_RESERVE_MS
    return max(int(trial_window_ms), remaining)


def _refuse_unadmitted_injection(controls: ControlCandidates,
                                 space: Mapping[str, Tuple[str, ...]]) -> None:
    """Refuse an injected ``C`` this sitting's axes cannot carry (v4 section 4).

    The refusal names the control, the axis and the value, because the operator
    who injected the board is the one who has to change it -- and because a
    column dropped instead of refused would leave two arms of the ablation
    running boards that only look identical.
    """
    refusals: List[str] = []
    for candidate in controls.candidates:
        for axis, value in dict(candidate.configuration).items():
            if str(axis) not in space:
                refusals.append(f"{candidate.control_id}: this sitting exposes no "
                                f"axis {axis}")
            elif str(value) not in tuple(str(item) for item in space[str(axis)]):
                refusals.append(
                    f"{candidate.control_id}: {axis}={value} is not one of "
                    + ", ".join(str(item) for item in space[str(axis)]))
    if refusals:
        raise LiveConsoleError(
            "the injected C names an axis value the frozen catalog does not admit: "
            + "; ".join(refusals))


def _latency_calibration(settings: Mapping[str, Any],
                         deployment: LiveDeployment) -> Dict[str, Any]:
    """``<runs_root>/agent-latency-calibration.json``, when the operator has one.

    Contract v2 section 7: with a calibration on disk the executor picks, per
    role, the largest budget whose p95 still fits inside the shortest validity
    window, so an answer arrives while the numbers it was given are still
    usable.  Without one the role defaults stand -- a missing or unreadable
    file is not an error, it is simply no calibration.
    """
    stated = dict(settings or {}).get("latencyCalibrationPath")
    candidates: List[Path] = []
    if stated:
        candidates.append(Path(str(stated)))
    else:
        candidates.append(Path(deployment.evidence_dir).parent
                          / "agent-latency-calibration.json")
    for path in candidates:
        table = load_latency_calibration(path)
        if table:
            table = dict(table)
            table["sourcePath"] = str(path)
            return table
    return {}


def _candidate_id_for(runtime: JointLiveRuntime, configuration: Mapping[str, str]) -> Optional[str]:
    """The frozen catalog's candidate with exactly this configuration, if any."""
    entry = runtime.find_candidate(configuration)
    return None if entry is None else str(entry.candidate.candidate_id)


def _profile_document(deployment: LiveDeployment) -> Dict[str, Any]:
    try:
        return json.loads(Path(deployment.document_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
# the hardware-free twin
# --------------------------------------------------------------------------- #


def build_hardware_free_agent_sitting(request: AgentRequest, *, tmp_dir: Any,
                                      ran: Any = None, condition: str = "contention-boundary",
                                      seed: int = 0, noise_sigma: float = 0.03,
                                      cell_capacity_mbps: float = 5.0,
                                      role_resolver: Optional[Callable[[str], Any]] = None,
                                      stamp: Optional[str] = None,
                                      kpi_observer: Optional[KpiObserver] = None,
                                      prepared: Optional[
                                          Tuple[TargetContract, ControlCandidates]] = None,
                                      ) -> AgentSitting:
    """The same sitting, with the radio emulated (contract section 6).

    The emulator's ports are the sitting's injection seams, so the session is
    ``MOCK`` and this composition root never opens a socket, an ssh connection
    or a lab file.  Which UEs and which axes the emulator has to carry is read
    from the request itself -- the same reading the sitting does -- so a
    hardware-free run and a live run differ only in who answers the ports.
    """
    from tools.hfconsole.agent_env import (  # local: the emulator is not a live import
        CONDITIONS, EmulatedKpiObserver, EmulatedRan, build_hardware_free_agent_runtime,
    )

    rows = parse_agent_intents(request, families=live_capable_families())
    ue_ids = list(dict.fromkeys([row.ue_id for row in rows if row.ue_id]
                                + list(request.caps) + list(request.pf_weights)))
    cells = tuple(str(cell) for cell in (request.cells or (12345678, 87654321)))
    if ran is None:
        default = {"131": cells[0], "132": cells[0],
                   "133": cells[-1] if len(cells) > 1 else cells[0]}
        ran = EmulatedRan(
            ues={ue: default.get(ue, cells[0]) for ue in ue_ids},
            cells={cell: float(cell_capacity_mbps) for cell in cells},
            offered_load_mbps={ue: CONDITIONS.get(condition, 4.0) for ue in ue_ids},
            seed=seed, noise_sigma=noise_sigma)
    identities = {ue: _EmulatedIdentity(ue, ran.serving_cell(ue)) for ue in ue_ids}
    # The emulator names the S-NSSAI every UE is on, so a slice axis has
    # something real to be scoped to here; a live profile does not, which is
    # why the live root takes the S-NSSAIs from the operator instead.
    ue_slices = {ue: str(sst) for ue, sst in dict(getattr(ran, "ue_slices", {})).items()}
    slices = tuple(dict.fromkeys(ue_slices.values()))
    axes = _axis_specs(rows, identities, request, [int(cell) for cell in cells], slices)
    # Before the emulator's own runtime is composed, not after: that call
    # freezes the epoch, and an operator who is going to be told the sitting
    # is too big should be told it now rather than at the end of the freeze.
    try:
        _refuse_oversized_catalog(axes.declared, int(request.max_catalog_cardinality))
    except CatalogTooLargeError as exc:
        raise LiveConsoleError(_ceiling_refusal(exc)) from None
    env = build_hardware_free_agent_runtime(
        intents_spec=_intent_specs(rows, lambda ue: int(ran.serving_cell(ue)),
                                   [int(cell) for cell in cells]),
        axes_spec=axes.declared, ran=ran,
        budget_trials=int(request.budget_trials), tmp_dir=tmp_dir)
    # The emulator's counter loaders are keyed by the counter ids of the joint
    # it just composed; the sitting composes the same axes, so the catalog is
    # frozen over the declared values rather than over C's subset.
    return build_agent_sitting(
        env.profile_path, replace(request, restrict_catalog_to_candidates=False),
        role_resolver=role_resolver, stamp=stamp,
        kpi_observer=kpi_observer or EmulatedKpiObserver(ran),
        slices=slices, ue_slices=ue_slices, prepared=prepared,
        **env.sitting_kwargs())


@dataclass(frozen=True)
class _EmulatedIdentity:
    """Just enough of a :class:`LiveUeObservation` to derive the axis specs."""

    ue_id: str
    cell: str

    @property
    def serving_nci(self) -> int:
        return int(self.cell)


# --------------------------------------------------------------------------- #
# evidence
# --------------------------------------------------------------------------- #


#: 귀책 분류. 오너 규칙(2026-09-21): **에이전트가 잘못 고른 판은 결과로 남기고,
#: 우리 코드나 장비가 망친 판은 데이터로 쓰지 않는다.**  `experiments/agent_episodes.py`
#: 의 `_external_failure()` 가 이미 그 규칙을 구현해 두었지만 --- `cause == "external"`
#: 이고 `methodCaused is not True` 인 것만 제외 --- **라이브 판은 그 모듈을 지나지 않아서**
#: `failures` 가 한 번도 채워진 적이 없었다(2026-09-21 전수: 321판 중 0판).
#: 그래서 원복 오탐·두 시계 같은 우리 코드 결함으로 죽은 판이 전부 집계에 섞여 있었다.
#:
#: 증거가 있는 것만 분류한다.  분류하지 않으면 그 판은 **데이터로 남는다** --- 의심만으로
#: 판을 버리면 불리한 결과를 조용히 지우는 것과 구별되지 않는다.
def attribute_failures(document: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """이 판을 못 쓰게 만든 외부 원인을 증거와 함께 나열한다 (없으면 빈 목록)."""
    failures: List[Dict[str, Any]] = []

    def add(kind: str, reason: str) -> None:
        failures.append({"kind": kind, "cause": "external",
                         "methodCaused": False, "reason": reason})

    # 2026-09-23 (오너 지시 "볼드모트"): UE 끊김·재등록 대기·관측원 공백·정상보다 긴
    # 핸드오버는 **판을 버리는 사유가 아니다.**  그 구간은 시간으로 세지 않고 그 표본은
    # 판정에 쓰지 않는다(`excision`) -- 판은 그 구간이 없었던 것처럼 이어간 것이다.
    # 그래서 예전의 두 규칙("hardwareDisconnects 가 하나라도", "kpiObserverFailures 가
    # 하나라도" 면 판 전체를 external-equipment-failure 로)은 없앴다; 09-22·23 판
    # 102/102 가 거기 걸려 데이터에서 빠지고 있었다.
    #
    # 남는 것은 **도려낸 뒤에도 판정할 수 없는 판**뿐이다 -- 우리 측 중단이 한 번의 대기
    # 상한이나 판당 누적 상한을 넘어 판이 `HARDWARE_UNAVAILABLE` 로 끝난 경우.  그 판은
    # 방법이 무엇을 골랐든 탐색을 마치지 못했으므로 증거(도려낸 구간)와 함께 적는다.
    # flow-payload-mismatch 처럼 **방법의 결과일 수 있는 것**은 애초에 도려내지 않는다.
    termination = document.get("termination")
    reason = (termination or {}).get("reason") if isinstance(termination, dict) else None
    if reason == HARDWARE_UNAVAILABLE:
        excision = document.get("excision") if isinstance(document.get("excision"), dict) else {}
        add("external-equipment-failure",
            "HARDWARE_UNAVAILABLE: 우리 측 중단이 상한을 넘어 판을 판정할 수 없다 -- "
            + str((termination or {}).get("detail", "")))
        failures[-1]["evidence"] = {
            "excisedTotalMs": excision.get("totalMs"),
            "capMs": excision.get("capMs"),
            "intervals": list(excision.get("intervals") or ())}

    # 두 시계.  에피소드 마감 B 와 Kernel 의 case 마감이 다른 시계라, 호출자가 자기
    # 마감으로 멈추면 Kernel 이 CASE_NOT_TERMINABLE 로 거절하고 종료상태 칸이 빈다.
    # 2026-09-21 실측 35판이 여기 걸렸고 그중 6판은 인텐트를 전부 충족한 판이었다.
    completion = document.get("completion")
    unresolved = (completion or {}).get("unresolved") if isinstance(completion, dict) else None
    if isinstance(unresolved, list):
        for item in unresolved:
            if "CASE_NOT_TERMINABLE" in str((item or {}).get("detail", "")):
                add("harness-defect",
                    "CASE_NOT_TERMINABLE: 실행기와 Kernel 이 서로 다른 마감을 쓴다")
                break

    # 여기에 "원복 오탐" 규칙이 있었다 (2026-09-21). **제거했다 — 내가 틀렸다.**
    # `observed == permit expected` 를 "라디오는 제대로 돌아갔는데 비교가 틀렸다" 로 읽고
    # 17건 전부 오탐이라고 보고했으나, `expected_config_hash` 는
    # `trial["observedConfigHash"]` -- **게이트웨이가 행동하기 전에 관측해야 하는 값**이고
    # 원복 직전이면 그것은 *적용된* 상태다.  그러므로 원복 뒤 observed 가 그것과 같다는 건
    # **아무것도 안 움직였다**는 뜻이고 진짜 원복 실패다.
    # `assurance/gateway/gateway.py:705-725` 의 주석이 이 오독을 정확히 경고하고 있다
    # ("한 시간을 잡아먹고 안전 검사를 약화시킬 뻔했다").  이 규칙을 남겨 뒀으면
    # **진짜 안전 사건을 '우리 결함' 으로 분류해 판을 버렸을 것이다.**
    return failures


def write_agent_evidence(sitting: AgentSitting) -> Dict[str, str]:
    """Write the episode beside the Kernel's own event stream."""
    directory = sitting.deployment.evidence_dir
    directory.mkdir(parents=True, exist_ok=True)
    episode_path = Path(f"{sitting.evidence_prefix}-episode.json")
    events_path = Path(f"{sitting.evidence_prefix}-events.jsonl")
    runtime = sitting.runtime
    document = sitting.episode_record()
    document["execution"] = {
        "caseId": sitting.case_id,
        "predictor": {"versions": [dict(item) for item in sitting.predictor_versions],
                      "final": (None if sitting.predictor is None
                                else dict(sitting.predictor.describe()))},
        "reobservations": [item.to_record() for item in sitting.reobservations],
        "confirmationHash": sitting.confirmation_hash,
        "preflight": dict(sitting.preflight),
        "contract": {"epochHash": runtime.epoch_hash(),
                     "terminalStateHash": runtime.path.terminal_state_hash()},
        "kernelTrials": [dict(item) for item in runtime.trials],
        "retiredCases": [{
            "caseId": item.case_id, "epochHash": item.epoch_hash(),
            "eventsFile": Path(f"{sitting.evidence_prefix}-events-retired{index}.jsonl").name,
            "policyIds": {k: list(v) for k, v in item.policy_ids().items()},
            "kernelTrials": [dict(trial) for trial in item.trials],
            "gatewayRefusals": [dict(entry) for entry in item.gateway.refusals()],
            "gatewayEvidence": {
                reference: {k: v for k, v in dict(item.gateway.evidence(reference)).items()
                            if k != "command"}
                for reference in item.gateway.evidence_references()},
        } for index, item in enumerate(sitting.retired_runtimes, 1)],
        "retentionCase": None if sitting.retention_runtime is None else {
            "caseId": sitting.retention_runtime.case_id,
            "epochHash": sitting.retention_runtime.epoch_hash(),
            "kernelTrials": [dict(item) for item in sitting.retention_runtime.trials],
            "kernelTermination": sitting.retention_kernel_termination,
            "gatewayRefusals": [dict(item) for item in sitting.retention_runtime.gateway.refusals()],
        },
        "grid": sitting.grid.to_record(),
        "controlToCandidate": dict(sitting.catalog_of_control),
        "policyIds": {k: list(v) for k, v in runtime.policy_ids().items()},
        "policyBodies": [body for builder in sitting.policy_builders for body in builder.bodies],
        "policyStatus": producer_episode(
            sitting.deployment.producer_database,
            sorted({pid for case in (*sitting.retired_runtimes, runtime)
                    for ids in case.policy_ids().values() for pid in ids})),
        "transportCalls": [dict(call) for call in getattr(sitting.policy_port, "calls", ())],
        "supplementary": [{
            "actionId": p.action_id, "adapterKey": p.adapter_key, "axis": p.axis,
            "policyTypeId": p.policy_type_id,
            "controlledUe": dict(p.controlled_ue),
            "expectedAttribution": p.expected.to_record(),
            "readbackLog": [dict(e) for e in p.counter_reader.reads],
            **dict(p.describe()),
        } for p in sitting.supplementary],
        "gatewayRefusals": [dict(item) for item in runtime.gateway.refusals()],
        "gatewayEvidence": {
            reference: {k: v for k, v in dict(runtime.gateway.evidence(reference)).items()
                        if k != "command"}
            for reference in runtime.gateway.evidence_references()},
        "afterEvidence": {
            "kpmSlots": kpm_slot_occupancy(sitting.deployment.kpm_jsonl_path),
            "readerRejectedRecords": runtime.reader.rejected_records,
            # 총계만으로는 **무엇을 해야 하는지** 알 수 없다: `epoch` 이 오르면 gNB 가
            # 재기동해 재핀이 필요하고, `nbId`/`topology` 는 설정 문제이며, `json`/`fields`/
            # `ueEntry`/`ueFields` 는 입력이 깨진 것이라 무해하다.  사유는 reader 안에만
            # 있었고 판 기록에는 총계만 실렸다 -- 판이 끝나면 사라진다(2026-09-21).
            "readerRejectedByReason": dict(
                getattr(runtime.reader, "rejected_by_reason", {}) or {}),
        },
    }
    # The exact requests and responses, one file per call.  ``CallRecord``
    # keeps ``prompt``/``system_prompt``/``raw`` in memory and deliberately
    # leaves them out of the episode record, which would otherwise carry the
    # whole prompt per trial -- but then nothing on disk can answer "what
    # occupied those 49 165 input tokens?", and a reconstruction from saved
    # inputs is not the captured request.  So: written beside the episode,
    # with only the path, the byte count and the digest going into the record.
    prompts = directory / f"{Path(sitting.evidence_prefix).name}-prompts"
    index: List[Dict[str, Any]] = []
    for position, call in enumerate(sitting.agents.calls):
        if not (call.prompt or call.system_prompt or call.raw):
            continue
        prompts.mkdir(parents=True, exist_ok=True)
        entry: Dict[str, Any] = {"call": position, "role": call.role,
                                 "phase": call.phase, "model": call.model,
                                 "startedAt": call.started_at}
        for name, text in (("system", call.system_prompt),
                           ("request", call.prompt), ("response", call.raw)):
            if not text:
                continue
            path = prompts / f"{position:03d}-{call.role}-{name}.txt"
            path.write_text(text, encoding="utf-8")
            entry[name] = {
                "path": path.name, "bytes": len(text.encode("utf-8")),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
        index.append(entry)
    if index:
        document["execution"]["promptArtifacts"] = {
            "directory": prompts.name, "calls": index}

    # 귀책은 문서가 완성된 뒤에 매긴다 -- execution 과 termination 이 다 모여야 한다.
    failures = attribute_failures(document)
    if failures:
        document["failures"] = failures
    # `excluded` 는 **여기서 쓰지 않는다.**  제외는 `experiments/agent_episodes.py` 의
    # 사전 선언 규칙(`EXCLUSION_RULES`, `exclusion_rule=` 인자)이 정한다 -- 판을 보고 나서
    # 버릴지 정하면 불리한 결과를 고르는 것과 구별되지 않는다.  내 몫은 **증거를 남기는
    # 것**이고, 그 규칙이 `failures` 에서 `cause == "external"` 이고
    # `methodCaused is not True` 인 것만 읽어 간다.
    # (2026-09-21: 처음엔 여기서 `excluded` 를 무조건 썼다가 그 러너의 재시도 루프가
    #  발동해 한 블록의 판이 두 벌씩 집계됐다 -- `test_agent_episodes` 의 `4 != 8`.)
    episode_path.write_text(json.dumps(document, indent=2, sort_keys=True, default=str) + "\n",
                            encoding="utf-8")
    events_path.write_text(
        "".join(json.dumps(envelope.to_canonical_dict(), sort_keys=True,
                           separators=(",", ":")) + "\n"
                for envelope in runtime.event_store.iterate()),
        encoding="utf-8")
    # A case a rebind retired keeps its own event stream beside the current one.
    for index, item in enumerate(sitting.retired_runtimes, 1):
        Path(f"{sitting.evidence_prefix}-events-retired{index}.jsonl").write_text(
            "".join(json.dumps(envelope.to_canonical_dict(), sort_keys=True,
                               separators=(",", ":")) + "\n"
                    for envelope in item.event_store.iterate()),
            encoding="utf-8")
    return {"episode": str(episode_path), "events": str(events_path)}
