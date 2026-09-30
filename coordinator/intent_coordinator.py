#!/usr/bin/env python3
"""
Main Intent Coordinator

Integrates all components for Multi-UE LLM-based intent coordination:
- Multi-UE metrics collection
- Multi-LLM backend management
- Adaptive calibration (θ*, N_max)
- State machine (S0-S6)
- GUI integration
"""

from __future__ import annotations

import collections
import copy
import dataclasses
import hashlib
import logging
import math
import re
import threading
import time
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Callable, Tuple
from dataclasses import dataclass, field

from decision.intent_model import (
    Intent, IntentType, IntentStatus, IntentPriority, ConstraintType,
    NetworkState, Action, Alternative, FeasibilityPrediction
)
from calibration.adaptive_calibrator import AdaptiveCalibrator, CostMetrics
from coordinator.episode_types import (
    TerminalOutcome, TerminalReason, MonitorVerdict, EvidenceRecord,
    TerminalRecord, new_id, Observation,
    SafetyState, ConfigRestoreVerdict, PhysicalRecoveryVerdict,
    RollbackOutcome, RecoveryClearanceResult, ActuationTransaction,
    ReserveLedger,
)
from coordinator import schema as _schema
from coordinator.schema import SchemaError
from coordinator import commit_binding as _cb
from coordinator.fsm import (
    FSMState, FSMEvent, event_for_target, IllegalTransition,
    UnknownSystemEvent, EpisodeDeadline, OperationTimeout, call_with_timeout,
    PolicyResponse,
)
# Batch E (P0-15 / P0-16): real history ablation + hot-swap proposal boundary.
from coordinator.history import (
    HistoryMode, HistoryRecord, HistoryValidationError,
    snapshot_records, restore_records, select_relevant,
)
from coordinator.proposer import (
    ProposerContext, new_request_id,
    SWITCH_PENDING, SWITCH_APPLIED, SWITCH_FAILED, SWITCH_COALESCED,
)
# Batch F (P0-13/P0-14): live-measurement contract + shared scheduler.
from coordinator.measurement import (
    MeasurementSample, MeasurementPurpose, MeasurementStatus, MeasurementError,
    MeasurementScheduler, MeasurementBusy, ProbeConfig, ProbeMode,
    capacity_offered_load_ok, new_probe_id,
)

logger = logging.getLogger("IntentCoordinator")


def _legacy_module(*name_parts: str):
    """Resolve a legacy-only module without making it a release dependency."""
    import importlib
    return importlib.import_module("".join(name_parts))


def _bs_of_gnb(gnb_id: str) -> str:
    """Map a gNB id to its BS id ROBUSTLY (gnb1->bs1, gnb10->bs10), NOT by the
    last character (P0-18: forbidden last-character topology fallback)."""
    return str(gnb_id).replace("gnb", "bs", 1)


def _nonneg_offered_load(mbps) -> float:
    """A finite, >= 0 offered rate in Mbit/s for set_offered_load (rejects
    bool/NaN/inf/negative), mirroring the environment driver's numeric guards so
    a bogus rate raises before any iperf is launched (fail closed)."""
    if isinstance(mbps, bool):
        raise ValueError(f"offered load must be a number, got bool {mbps!r}")
    try:
        f = float(mbps)
    except (TypeError, ValueError):
        raise ValueError(f"offered load is not a number: {mbps!r}")
    if not math.isfinite(f) or f < 0:
        raise ValueError(f"offered load must be finite and >= 0, got {f!r}")
    return f


# Batch B (P0-1 step 8): a latched episode does NOT return to S0 - the FSM
# stays in this terminal safety state until an operator recovery API clears
# the latch.
S_TECHNICAL_FAILSAFE = "S_TECHNICAL_FAILSAFE"


class IntentParseError(ValueError):
    """LLM intent parse rejected (fail-closed).

    Raised when the parsed intent carries an unsupported type, an unknown
    constraint, or a missing/non-finite target value. Unknown/malformed input
    must never silently coerce into a valid-looking intent (it did, before
    this fix: unknown type -> THROUGHPUT_GOAL, unknown constraint -> MAX,
    missing value -> 0.0) because that fail-open path can end in a RAN action.
    """


class MalformedResponseError(ValueError):
    """A backend feasibility response violated its contract (fail-closed).

    Raised when `response.success` is missing or not an exact bool. Such a
    response is malformed: bool()-coercing it would let a truthy non-bool (e.g.
    the string "false") route to a trial, so the whole cycle fails closed to a
    TechnicalFailsafe rather than silently coercing a proposal-generated verdict.
    """


# Intent types _evaluate_intent can actually measure. Anything else is
# fail-closed: rejected at parse, never "already satisfied" at S1, never
# satisfied at S4 (see _evaluate_intent).
SUPPORTED_INTENT_TYPES = frozenset({
    IntentType.THROUGHPUT_GOAL,
    IntentType.THROUGHPUT_FAIRNESS,
    IntentType.LATENCY_GOAL,
    IntentType.POWER_CONSTRAINT,
})


def profile_bounded_action_space(network_config) -> "ActionSpaceConfig":
    """Action-space bounds clamped to the ACTIVE radio profile (H2).

    The configured PRB upper bound (106, the largest profile) is capped to
    min(num_prb) across the configured gNBs, so a 24-PRB cell never treats
    a 106-PRB proposal as in-bounds - the enforcement layer's clip
    provenance would otherwise be wrong (the value LOOKS legal while the
    scheduler can physically grant only 24). Returns a COPY; the shared
    config singleton is never mutated.
    """
    space = getattr(network_config, "action_space", None)
    if space is None:
        # Legacy-only fallback.  The packaged O-RAN profile supplies an
        # explicit action-space object and therefore never imports the lab
        # configuration module or its hardware addresses.
        space = _legacy_module("con", "fig").ActionSpaceConfig()
    prbs = [g.num_prb for g in getattr(network_config, "gnbs", {}).values()
            if getattr(g, "num_prb", 0)]
    if not prbs:
        return space
    profile = min(prbs)
    return dataclasses.replace(
        space,
        prb_cap_max=min(space.prb_cap_max, profile),
        ue_prb_cap_max=min(space.ue_prb_cap_max, profile))


# ---------------------------------------------------------------------------
# Negotiation policies (S5). A policy maps one candidate Alternative to
# "accept" | "reject"; the coordinator consults it once per round.
# ---------------------------------------------------------------------------

def auto_accept_first(alt: Alternative) -> str:
    """Default policy: accept the first valid alternative (legacy behavior)."""
    return "accept"


def make_scripted_policy(decisions: List[str]) -> Callable[[Alternative], str]:
    """Deterministic per-round script for tests/experiments, e.g.
    make_scripted_policy(["reject", "reject", "accept"]). Once the script is
    exhausted every further round is rejected."""
    seq = list(decisions)
    state = {"i": 0}

    def policy(alt: Alternative) -> str:
        i = state["i"]
        state["i"] = i + 1
        return seq[i] if i < len(seq) else "reject"

    return policy


class IntentManager:
    """Manages active intents"""

    def __init__(self):
        self.intents: Dict[str, Intent] = {}
        self.history: List[Intent] = []

    def add(self, intent: Intent) -> bool:
        """Add an intent - REPLACING any monitored intent with the same
        (type, scope).

        A coordination episode that commits (possibly a negotiated
        relaxation) UPDATES the operator's intent lifecycle; without the
        replacement every successful episode would add a UUID-distinct
        duplicate beside the registered one, and the monitored set - the
        S2 context and the S4 validation set - would accumulate stale
        copies across a trial (C7). The replaced version moves to history.
        """
        key = self._scope_key(intent)
        for existing in list(self.intents.values()):
            if (existing is not intent
                    and existing.status in self.MONITORED_STATUSES
                    and existing.type == intent.type
                    and self._scope_key(existing) == key):
                self.history.append(existing)
                del self.intents[existing.id]
        self.intents[intent.id] = intent
        return True

    @staticmethod
    def _scope_key(intent: Intent):
        """Order-insensitive identity of an intent's FULL scope (ue/bs/cell
        ids + area): ['ue1','ue2'] and ['ue2','ue1'] are the same scope; a
        different cell list or area is a different intent."""
        s = intent.scope
        return (frozenset(map(str, s.ue_ids or [])),
                frozenset(map(str, s.bs_ids or [])),
                frozenset(map(str, s.cell_ids or [])),
                s.area_id)

    def remove(self, intent_id: str) -> bool:
        """Remove intent"""
        if intent_id in self.intents:
            self.history.append(self.intents[intent_id])
            del self.intents[intent_id]
            return True
        return False

    # Statuses under continuous assurance. A SATISFIED intent stays
    # monitored - satisfaction is a compliance observation, not the end of
    # the intent's lifecycle (C2): drift must flip it to VIOLATED and
    # re-enter coordination. VIOLATED intents obviously stay monitored too.
    MONITORED_STATUSES = (IntentStatus.ACTIVE, IntentStatus.SATISFIED,
                          IntentStatus.VIOLATED)

    def get_active(self) -> List[Intent]:
        """Get active intents"""
        return [i for i in self.intents.values() if i.status == IntentStatus.ACTIVE]

    def get_monitored(self) -> List[Intent]:
        """Intents under continuous assurance (ACTIVE + SATISFIED +
        VIOLATED). This is the set the monitor re-evaluates every period
        and the set S1/S2/S4 coordinate against - a satisfied intent must
        not vanish from validation (C2)."""
        return [i for i in self.intents.values()
                if i.status in self.MONITORED_STATUSES]

    def get_all(self) -> List[Intent]:
        """Get all intents"""
        return list(self.intents.values())

    def clear(self):
        """Clear all intents"""
        self.history.extend(self.intents.values())
        self.intents.clear()

    def update_status(self, intent_id: str, status: IntentStatus):
        """Update intent status"""
        if intent_id in self.intents:
            self.intents[intent_id].status = status

    def snapshot_state(self) -> Dict:
        """Shallow snapshot of the FULL intent lifecycle (Gate-review item 2):
        the ``intents`` map and the ``history`` list, preserving intent-object
        IDENTITY so an aborted admission can be rolled back to the exact
        pre-admission state (old intent restored, new intent absent, history
        unchanged)."""
        return {"intents": dict(self.intents), "history": list(self.history)}

    def restore_state(self, snap: Dict) -> None:
        """Restore a snapshot_state() bundle - drops any partial mutation an
        aborted admission left behind."""
        self.intents = dict(snap.get("intents", {}))
        self.history = list(snap.get("history", []))


class IntentCoordinator:
    """
    Main coordinator for LLM-based intent coordination.

    Implements the closed-loop coordination framework:
    1. Analysis Layer (S0-S2): Conflict screening + LLM feasibility
    2. Execution Layer (S3-S4): Trial execution + Validation
    3. Negotiation Layer (S5): Alternative generation

    Supports:
    - Multiple UEs with per-UE and cross-UE intents
    - Multiple LLM backends for model-agnostic operation
    - Adaptive θ* and N_max calibration
    """

    def __init__(self, config: "Config" = None, gui=None, *, collector=None,
                 executor=None, llm_manager=None):
        """
        Args:
            config: Configuration object
            gui: GUI instance for visualization
            collector: optional collection port.  ``None`` keeps the legacy
                MultiUECollector wiring exactly as before.
            executor: optional actuation port.  ``None`` keeps the legacy
                OAIExecutor wiring exactly as before.
            llm_manager: optional model-manager port. ``None`` discovers the
                legacy runtime backends; O-RAN and hermetic profiles inject a
                pinned/deterministic manager instead.
        """
        if config is None:
            # Preserve the legacy GUI/OAI entry point without making its lab
            # configuration a transitive dependency of the release artifact.
            config = _legacy_module("con", "fig").get_config()
        self.config = config
        self.gui = gui

        # Initialize components
        logger.info("Initializing Intent Coordinator...")

        # Multi-UE collector (hardware mode by default; simulation only for
        # single-host RFsim development - config.simulation_mode)
        sim_mode = bool(getattr(self.config, "simulation_mode", False))
        ue_configs = {
            ue_id: {
                "host": getattr(ue, "ip", None) or ue.hostname,
                "ssh_user": ue.ssh_user,
                "gnb": getattr(ue, "initial_serving_gnb", None),
            }
            for ue_id, ue in self.config.network.ues.items()
        }
        if collector is None:
            # Resolve the legacy implementation only on the legacy branch.
            # importlib keeps its hardware dependency outside the statically
            # reachable O-RAN graph; O-RAN profiles inject this port and never
            # execute this branch.
            cls = _legacy_module(
                "collectors.", "multi_ue_collector").MultiUECollector
            collector = cls(ue_configs, simulation_mode=sim_mode)
        self.ue_collector = collector
        logger.info(f"UE Collector initialized: {list(ue_configs.keys())} "
                    f"(simulation={sim_mode})")

        # Batch F (P0-13/P0-14): the SHARED measurement scheduler (background +
        # trial/negotiation probes acquire ONE collection lock so overlapping
        # iperf cannot run) and the explicit probe config. Injected into the
        # collector so both paths share them. _now is an injectable EPOCH clock
        # in the SAME domain as action_apply_time. _throughput_probe_fn defaults
        # to the collector's live probe but is injectable for deterministic tests
        # (never a real subprocess in tests).
        self._measurement_scheduler = MeasurementScheduler(
            default_timeout_s=float(getattr(self, "probe_lock_timeout_s", 10.0)))
        self._now = time.time
        self._throughput_probe_fn = None
        # Live measurement fix: when a §1-4 background offered load is active, the
        # KPI throughput is measured PASSIVELY (UE tun rx_bytes delta) instead of
        # by an active iperf3 injection that would collide with the load on the
        # UE's single iperf3 server and read a spurious 0. Injectable probe fn for
        # deterministic tests; the switch defaults on (off in sim / no-load).
        self._goodput_probe_fn = None
        self.passive_goodput_when_loaded = True
        self._probe_config = ProbeConfig()
        # FAIL-CLOSED at construction (coordinator review): the collector MUST
        # accept the shared scheduler AND the default probe config - a swallowed
        # failure here would build a coordinator whose collector is unconfigured
        # while self._probe_config claims a config (the same metadata/probe
        # mismatch). No try/except: a missing/throwing setter aborts construction.
        setter = getattr(self.ue_collector, "set_scheduler", None)
        if not callable(setter):
            raise RuntimeError(
                "collector does not expose a callable set_scheduler; refusing "
                "to build a coordinator whose probes cannot share the scheduler")
        setter(self._measurement_scheduler)
        # install the default probe config through the fail-closed atomic path
        # (validates + requires a callable collector setter + propagates).
        self.set_probe_config(self._probe_config)

        # RAN executor: runtime TX power offsets via the patched OAI telnet
        # command "ci rfatt" (see executor/oai_executor.py)
        if executor is None:
            # Legacy-only dynamic resolution; see the collector seam above.
            cls = _legacy_module("executor.", "oai_executor").OAIExecutor
            executor = cls()
        self.executor = executor
        # P8 RNTI integrity guard: the executor consults the coordinator's
        # UE->RNTI registration map first when reinterpreting a stale RNTI,
        # and remaps are propagated back into the coordinator's cache
        self.executor.rnti_resolver = self._resolve_stale_rnti
        self.executor.on_rnti_remap = self._on_rnti_remap

        # Action-space bounds U for enforcement-layer clipping.
        # Multi-dimensional (Extension Strategy d>=2): power + PRB + scheduling
        # priority + MCS. Each axis is clipped to its bound before it reaches the
        # RAN (Proposition 1). The legacy dB attributes are kept for callers that
        # still reference the power axis directly. H2: the PRB bounds are
        # clamped to the active radio profile (min num_prb across gNBs).
        self.action_space: ActionSpaceConfig = profile_bounded_action_space(
            self.config.network)
        self.action_min_db = self.action_space.power_offset_min_db
        self.action_max_db = self.action_space.power_offset_max_db

        # Per-UE addressing (variable UE count): map each logical UE to its
        # serving gNB (from config) and cache its RNTI once resolved. Lets the LLM
        # address the PRB / scheduling-priority axes at an individual UE (ueN_*)
        # for intra-cell fairness whenever a cell is shared (current testbed:
        # UE1+UE2 on gNB1), not just per-cell (bsN_*).
        self.ue_serving_gnb: Dict[str, str] = {
            ue_id: getattr(ue, "initial_serving_gnb", None)
            for ue_id, ue in self.config.network.ues.items()
        }
        self.ue_rnti: Dict[str, int] = {}

        # Throughput cache: iperf3 measured every tau seconds in hardware
        # mode. Each entry is timestamped (_last_tput_ts) so a STALE value
        # can never mask a fresh measurement failure (C5): _merge_throughput
        # only fills from the cache within the validity window.
        self._last_throughput: Dict[str, float] = {}
        self._last_tput_ts: Dict[str, float] = {}
        self._last_tp_time = 0.0
        self.tp_interval_s = 15.0  # KPI collection period from the paper
        self.tau_trial_s = float(getattr(self.config.calibration, "tau_trial", 15.0))
        # hard-failure bookkeeping: UEs attached at S3 entry + reconnection cap.
        # Blocker 6: the reconnection cap is the SAME declared config duration
        # the deterministic c_hard_ub is derived from (not an independent 30.0).
        self._pre_trial_attached: set = set()
        self.hard_failure_cap_s = float(getattr(
            self.config.calibration, "hard_failure_cap_s", 30.0))
        # Blocker 6: tag the config's action-space provenance so the ledger's
        # cap audit traces back to the active bounds/profile.
        try:
            self.config.calibration.action_space_profile = (
                f"prb<= {self.action_space.prb_cap_max}, "
                f"power[{self.action_space.power_offset_min_db},"
                f"{self.action_space.power_offset_max_db}]")
        except Exception:
            pass

        # LLM backend manager. H2: the system prompt advertises the
        # profile-clamped bounds, so the LLM reasons over the same action
        # space the enforcement layer will accept.
        if llm_manager is None:
            # The interactive/legacy runtime still performs its normal backend
            # discovery.  Delivery profiles inject a pinned manager and must
            # not make every cloud/provider adapter a transitive release
            # dependency merely by importing the coordinator.
            llm_manager = _legacy_module(
                "decision.", "llm_backend").LLMBackendManager()
        self.llm_manager = llm_manager
        self.llm_manager.action_space = self.action_space
        # P0-18: the LLM prompt enumerates the CONFIGURED UE set + serving cells
        # dynamically (no hardcoded 2-UE example).
        self.llm_manager.ue_ids = list(self.ue_serving_gnb.keys())
        self.llm_manager.ue_serving = dict(self.ue_serving_gnb)
        logger.info(f"LLM Backends available: {self.llm_manager.get_available_names()}")

        # Intent manager
        self.intent_manager = IntentManager()

        # P0-8: bounded continuous-assurance re-entry scheduler state.
        self.assurance_queue_max = self.ASSURANCE_QUEUE_MAX
        self.assurance_completed_max = self.ASSURANCE_COMPLETED_MAX
        self._ensure_assurance_state()

        # Adaptive calibrator. No data_file: calibration state must NOT
        # carry across sessions/trials/models (5-trial independence); pass
        # one explicitly only for deliberate carry-over experiments.
        # P0-12: BOTH initial_theta AND initial_n_max come from the ONE
        # cold-start source (CalibrationConfig), not hidden 0.7/3 defaults, so
        # the runtime first-cycle values equal the config outputs exactly.
        _cal_cfg = self.config.calibration
        self.calibrator = AdaptiveCalibrator(
            mode="online",
            initial_theta=_cal_cfg.compute_initial_theta(),
            initial_n_max=_cal_cfg.compute_initial_n_max(_cal_cfg.tau_trial),
            c_episode=_cal_cfg.c_episode,
            cold_start_source=_cal_cfg.cold_start_source(_cal_cfg.tau_trial),
        )
        # experiment phase label for episode records (set by the runner)
        self.current_phase: str = ""
        logger.info(f"Calibrator initialized: θ*={self.calibrator.theta_star:.3f}, N_max={self.calibrator.n_max}")

        # State
        self.current_state = "S0"
        self.running = False
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Batch C (P0-7): bounded execution.  An ABSOLUTE monotonic episode
        # deadline is fixed at episode start; every external call (model /
        # policy / alternative generator) is given a timeout no greater than
        # the remaining episode time, and the FSM step count is capped - so a
        # non-returning dependency can never defeat the wall-clock bound.
        cal = getattr(self.config, "calibration", None)
        self.episode_budget_s = float(getattr(cal, "episode_budget_s", 120.0))
        self.model_call_timeout_s = float(
            getattr(cal, "model_call_timeout_s", 30.0))
        self.policy_call_timeout_s = float(
            getattr(cal, "policy_call_timeout_s", 15.0))
        self.max_fsm_steps = int(getattr(cal, "max_fsm_steps", 256))
        # Batch C (P0-6): how fresh a validation observation must be at commit.
        self.commit_freshness_s = float(
            getattr(cal, "commit_freshness_s", self.tau_trial_s + 15.0))
        self._deadline: Optional[EpisodeDeadline] = None
        self._fsm_steps = 0

        # Batch B (P0-1): typed post-actuation safety state + latch record.
        # LATCHED_FAILSAFE blocks every new episode/cycle/negotiation/write
        # until clear_safety_latch() verifies readback against the baseline.
        self.safety_state = SafetyState.READY
        self._safety_latch: Optional[Dict] = None

        # Evidence identifier chain (P0-6 / P1-6): one run id per coordinator
        # session; each episode/cycle/proposal draws a fresh child id so an
        # action can be traced back to the exact proposal + model that made it.
        self.experiment_run_id = new_id("run")
        self._episode_seq = 0

        # History reservoir (Batch E / P0-15). Holds finalized HistoryRecord
        # objects (ONLINE mode appends; FROZEN reads a defensive snapshot;
        # DISABLED passes ZERO records). Kept a plain list so test skeletons'
        # `history_reservoir = []` reset stays compatible.
        self.history_reservoir: List = []
        self.max_history = 100
        # ablation mode - default ONLINE so an empty cold-start reservoir is
        # indistinguishable from the previous "history used" behaviour.
        self.history_mode: HistoryMode = HistoryMode.ONLINE_RESERVOIR
        # FROZEN-mode retrieval reads this defensive immutable snapshot and
        # never learns new records into it.
        self._frozen_history: Tuple[HistoryRecord, ...] = tuple()

        # Batch E (P0-16): model hot-swap PROPOSAL BOUNDARY. A switch requested
        # while a cycle is active is QUEUED and applied atomically only at the
        # next proposal boundary; the pinned ProposerContext freezes the ACTUAL
        # backend object/handle for the whole cycle. The mutex guards ONLY the
        # small queue append/drain - never a model/custom callback.
        self._proposer_ctx: Optional[ProposerContext] = None
        self._switch_lock = threading.Lock()
        self._switch_queue: List[Dict] = []
        self._switch_audit: List[Dict] = []
        # raw (possibly enum/object) switch targets kept OUT of the public audit,
        # keyed by request id, so the audit stays JSON-safe.
        self._switch_raw_targets: Dict[str, Any] = {}
        # signature of the intent evaluated at the current episode's feasibility
        # (captured for the finalized history record; reset per episode).
        self._last_intent_signature: Optional[str] = None

        # Callbacks
        self.on_state_change: Optional[Callable] = None
        self.on_negotiation_needed: Optional[Callable] = None
        # C2 drift re-entry hook: fired once per SATISFIED/ACTIVE -> VIOLATED
        # transition observed by the monitor (the runner/GUI decides whether
        # to trigger a re-coordination episode)
        self.on_intent_violated: Optional[Callable] = None

        # S5 negotiation: per-round policy (on_negotiation_needed wins when
        # set) and an injectable alternative generator for later rounds
        # (None => LLM-backed self._generate_alternatives).
        self.negotiation_policy: Callable[[Alternative], str] = auto_accept_first
        self.generate_alternatives_fn: Optional[Callable] = None

        # H1: episode-level single-flight lock - the WHOLE S0-S6
        # transaction is serialized, so concurrent process_intent calls
        # (GUI + runner + drift hook) can never interleave their
        # snapshot / trial / rollback / calibration state. The in-flight
        # flag additionally rejects SAME-THREAD recursion (a callback
        # inside the episode calling back), which an RLock alone would let
        # nest a full S0-S6 between the outer snapshot and rollback.
        self._episode_lock = threading.RLock()
        self._episode_in_flight = False

        # Metrics thread
        self._metrics_thread = None
        self._stop_event = threading.Event()

        # P0-19 LIVE exogenous-environment actuator: the offered-load background
        # traffic generator (iperf3 UDP into the serving UEs). Built LAZILY on the
        # first non-zero set_offered_load() call so the offline/emulated/RFsim
        # paths carry ZERO extra threads or subprocesses; torn down in stop().
        self._offered_load_gen = None

        logger.info("Intent Coordinator initialized")

    def _exclude_unprovisioned_ues(self, active) -> None:
        """Trim the LIVE topology to the provisioned ACTIVE UE set. Unprovisioned
        UEs are logical EXPANSION SLOTS - dropped from collection, per-UE
        addressing and the LLM prompt so nothing runs against a guessed identity.
        Each excluded UE is logged ONCE at start (operator visibility). Idempotent
        and safe when nothing is excluded."""
        try:
            active_set = set(active)
        except TypeError:
            return                       # non-iterable (e.g. a mock) -> no trim
        active_set = {str(u) for u in active_set}
        configured = list(self.ue_serving_gnb.keys()) if getattr(
            self, "ue_serving_gnb", None) else list(
            getattr(getattr(self, "config", None), "network", None).ues.keys())
        excluded = [u for u in configured if u not in active_set]
        for ue in excluded:
            msg = (f"{ue}: not provisioned - excluded from live topology "
                   f"(expansion slot)")
            # _log_gui logs to the logger AND the GUI (once); do not also log
            # directly or the start-time notice would be duplicated.
            try:
                self._log_gui(msg)
            except Exception:
                logger.info(msg)
            try:
                self.ue_collector.remove_ue(ue)
            except Exception:
                pass
        if getattr(self, "ue_serving_gnb", None):
            self.ue_serving_gnb = {u: g for u, g in self.ue_serving_gnb.items()
                                   if u in active_set}
        if isinstance(getattr(self, "ue_rnti", None), dict):
            self.ue_rnti = {u: r for u, r in self.ue_rnti.items()
                            if u in active_set}
        lm = getattr(self, "llm_manager", None)
        if lm is not None:
            if getattr(lm, "ue_ids", None):
                lm.ue_ids = [u for u in lm.ue_ids if u in active_set]
            if isinstance(getattr(lm, "ue_serving", None), dict):
                lm.ue_serving = {u: g for u, g in lm.ue_serving.items()
                                 if u in active_set}

    def start(self):
        """Start coordinator"""
        # UE count is variable. On the REAL-hardware live path, unprovisioned UEs
        # are LOGICAL EXPANSION SLOTS: they are excluded here (never driven with a
        # guessed placeholder - handoff line 1181) so a testbed with fewer
        # physical UEs than the logical topology still starts. The gate fails
        # closed ONLY when NO UE is provisioned. The emulated SimCollector
        # (emulated=True) and RFsim simulation_mode paths use no real hardware and
        # are exempt.
        col = self.ue_collector
        if not getattr(col, "emulated", False) and \
                not getattr(col, "simulation_mode", False):
            cfg = getattr(self, "config", None)
            if cfg is not None and hasattr(cfg, "validate_live_topology"):
                active = cfg.validate_live_topology()   # raises iff 0 provisioned
                self._exclude_unprovisioned_ues(active)
        self.running = True
        self._stop_event.clear()

        # Start metrics collection thread
        self._metrics_thread = threading.Thread(target=self._metrics_loop, daemon=True)
        self._metrics_thread.start()

        logger.info("Coordinator started")
        self._log_gui("Coordinator started")

    def stop(self):
        """Stop coordinator"""
        self.running = False
        self._stop_event.set()

        # tear the offered-load background traffic down FIRST so no iperf3 burst
        # outlives the session (fail-safe cleanup; §1-4 live env actuator).
        self._stop_offered_load()

        if self._metrics_thread:
            self._metrics_thread.join(timeout=2)

        self.ue_collector.shutdown()
        self.llm_manager.shutdown()
        logger.info("Coordinator stopped")

    # ------------------------------------------------------------------ #
    # §1-4 LIVE exogenous-environment hook: offered traffic load (P0-19)  #
    # ------------------------------------------------------------------ #

    def _offered_load_targets(self) -> Dict[str, str]:
        """The {ue_id: tun_ip} of the CURRENTLY-SERVING, attached UEs to load.

        Membership follows the CONFIGURED serving relationship / LIVE_UE_SET
        (the collector is built from config.network.ues, already trimmed by
        LIVE_UE_SET), and the tun IP is the same one the KPI probe pushes to
        (UEMetrics.ip_address). Resolved FRESH on every burst so a re-attached
        UE's new IP is picked up and a detached UE is dropped. An unattached /
        IP-less UE is skipped (never loaded blindly)."""
        targets: Dict[str, str] = {}
        col = getattr(self, "ue_collector", None)
        for ue_id, collector in getattr(col, "collectors", {}).items():
            last = collector.get_last_metrics() if collector else None
            ip = getattr(last, "ip_address", None) if last else None
            if last is not None and getattr(last, "attached", False) and ip:
                targets[ue_id] = ip
        return targets

    def set_offered_load(self, mbps: float) -> None:
        """LIVE §1-4 exogenous-environment hook (P0-19): drive the BACKGROUND
        offered traffic load to ``mbps`` Mbit/s.

        This is the ``load_setter`` the runner injects into the
        OfferedLoadEnvironmentDriver (see runner._resolve_offered_load_setter):
        the driver calls it ONCE per phase, and this hook must keep that rate
        going for the whole phase. It (re)arms a continuous, container-timeout-
        wrapped iperf3 UDP generator into the serving UEs, coordinated with the
        KPI probe through the shared MeasurementScheduler. ``mbps == 0`` STOPS the
        load; a rate change cleans up the old generator before starting the new
        one (see coordinator.offered_load.BackgroundLoadGenerator). Built lazily,
        so an offline/emulated coordinator that never calls this spawns nothing.

        Fail-closed input validation (finite, >= 0) mirrors the environment
        driver's own numeric guards - a bogus rate raises rather than silently
        launching a garbage iperf."""
        rate = _nonneg_offered_load(mbps)
        gen = self._offered_load_gen
        if gen is None:
            if rate == 0.0:
                return                          # nothing running, nothing to stop
            # Legacy live-environment actuator, resolved only when explicitly
            # enabled. O-RAN profiles never import this hardware-side module.
            generator_cls = _legacy_module(
                "coordinator.", "offered_load").BackgroundLoadGenerator
            gen = generator_cls(
                self._offered_load_targets,
                scheduler=getattr(self, "_measurement_scheduler", None))
            self._offered_load_gen = gen
        gen.set_rate(rate)

    def _offered_load_active(self) -> bool:
        """True when the §1-4 background offered load is currently driving traffic
        (a non-zero-rate generator exists). While it is, an active iperf3 KPI
        probe would share the UE's single iperf3 server with the load and read a
        spurious 0 - so the KPI is measured passively instead (see
        _use_passive_goodput)."""
        gen = getattr(self, "_offered_load_gen", None)
        try:
            return bool(gen is not None
                        and float(getattr(gen, "rate_mbps", 0.0)) > 0.0)
        except (TypeError, ValueError):
            return False

    def _use_passive_goodput(self) -> bool:
        """Route the KPI throughput through the PASSIVE rx_bytes goodput probe
        (no iperf3 injection) instead of the active iperf3 probe, WHEN a §1-4
        background offered load is active. This is the live-measurement fix: the
        active probe and the load contend for the UE's one iperf3 server and the
        loser reads 0.00; the passive counter delta cannot collide. Disabled in
        simulation mode and when no load is active (the active probe is the honest
        choice there), and only when the collector exposes the passive probe."""
        if not getattr(self, "passive_goodput_when_loaded", True):
            return False
        if not self._offered_load_active():
            return False
        col = getattr(self, "ue_collector", None)
        if col is None or getattr(col, "simulation_mode", False):
            return False
        return callable(getattr(col, "get_goodput_passive_all", None))

    def _run_goodput_probe(self) -> Dict[str, float]:
        """Invoke the PASSIVE goodput probe (injectable for tests; defaults to the
        collector's tun rx_bytes probe). Mirrors _run_throughput_probe but never
        injects traffic, so it must NOT be run under the shared measurement lock
        (the offered load has to keep flowing during the counter window)."""
        fn = getattr(self, "_goodput_probe_fn", None)
        if fn is None:
            fn = self.ue_collector.get_goodput_passive_all
        return fn(self._probe_cfg().duration_s) or {}

    def _stop_offered_load(self) -> None:
        """Fail-safe teardown of the offered-load generator (called from stop());
        a missing/throwing generator never blocks coordinator shutdown."""
        gen = getattr(self, "_offered_load_gen", None)
        if gen is not None:
            try:
                gen.stop()
            except Exception as e:
                logger.debug("offered-load generator stop failed: %s", e)

    def _metrics_loop(self):
        """Background metrics collection"""
        while not self._stop_event.is_set():
            try:
                metrics = self.ue_collector.collect_all()

                # Periodic throughput measurement (iperf3, 2 s UDP session
                # every tp_interval_s - matches the paper's 15 s KPI loop)
                if not self.ue_collector.simulation_mode:
                    any_attached = any(m.attached for m in metrics.values())
                    if any_attached and (time.time() - self._last_tp_time
                                         > self.tp_interval_s):
                        try:
                            # Batch F (P0-13): the BACKGROUND throughput probe
                            # goes through the SAME shared scheduler as the trial
                            # (via _measure_throughput_detailed), so background
                            # and S4 iperf can never overlap for the same UE.
                            bg, per_ue = self._measure_throughput_detailed(
                                MeasurementPurpose.BACKGROUND, label="background")
                            if not bg.is_unknown():
                                self._update_throughput_cache(per_ue)
                                self._last_tp_time = time.time()
                        except Exception as e:
                            logger.debug(f"throughput measurement failed: {e}")

                self._merge_throughput(metrics)

                for ue_id, m in metrics.items():
                    if self.gui:
                        self.gui.update_ue_metrics(ue_id, m.to_dict())

                # Check intent satisfaction
                self._check_intent_satisfaction(metrics)

            except Exception as e:
                logger.error(f"Metrics collection error: {e}")

            self._stop_event.wait(2.0)  # 2 second interval

    def _update_throughput_cache(self, tps: Dict[str, float]):
        """Record fresh iperf3 measurements with their timestamp."""
        if not hasattr(self, "_last_tput_ts"):   # __new__-built test skeletons
            self._last_tput_ts = {}
        now = time.time()
        for ue_id, tput in (tps or {}).items():
            self._last_throughput[ue_id] = tput
            self._last_tput_ts[ue_id] = now

    def _merge_throughput(self, metrics: Dict[str, UEMetrics]):
        """Fill in the most recent iperf3 throughput for UEs missing it -
        but ONLY within the cache validity window (2x the measurement
        period). A stale cache entry must not mask a fresh measurement
        failure (C5): past the window the throughput stays None, so the
        S4 measurement-invalid detector can see the loss.
        """
        if not hasattr(self, "_last_tput_ts"):   # __new__-built test skeletons
            self._last_tput_ts = {}
        validity = 2.0 * float(getattr(self, "tp_interval_s", 15.0))
        now = time.time()
        for ue_id, m in metrics.items():
            if m.throughput_mbps is None and ue_id in self._last_throughput:
                age = now - self._last_tput_ts.get(ue_id, float("-inf"))
                if age <= validity:
                    m.throughput_mbps = self._last_throughput[ue_id]

    def _check_intent_satisfaction(self, metrics: Dict[str, UEMetrics]):
        """Continuous assurance (C2): re-evaluate every MONITORED intent
        (ACTIVE + SATISFIED + VIOLATED) each period.

        satisfied -> SATISFIED; violated -> VIOLATED (drift detection - the
        on_intent_violated hook fires once per transition so the operator/
        runner can re-enter coordination); unknown (telemetry gap) -> the
        status is kept, so a measurement outage cannot flap a satisfied
        intent into violation or vice versa.
        """
        for intent in self.intent_manager.get_monitored():
            try:
                verdict = self._evaluate_intent_verdict(intent, metrics)
            except ValueError as e:
                # unsupported type: fail-closed, keep status untouched
                logger.debug(f"intent {intent.id}: {e}")
                continue
            if verdict is MonitorVerdict.SATISFIED:
                previous = intent.status
                intent.status = IntentStatus.SATISFIED
                # review #4: on a verified SATISFIED (re)transition, retire the
                # completed-suppression key so a genuine later re-violation of
                # the SAME intent enqueues again (recovery closes the epoch).
                if previous is not IntentStatus.SATISFIED:
                    try:
                        self._retire_assurance_key(intent)
                    except Exception as e:
                        logger.warning(f"assurance retire failed: {e}")
            elif verdict is MonitorVerdict.VIOLATED:
                previous = intent.status
                intent.status = IntentStatus.VIOLATED
                if previous != IntentStatus.VIOLATED:
                    self._log_gui(f"Intent {intent.id} VIOLATED "
                                  f"(drift from {previous.name})")
                    # P0-8: wire the production drift violation into the bounded
                    # assurance scheduler carrying the EXACT typed Intent, the
                    # originating intent-set version, the re-entry reason and the
                    # AUTHORITATIVE originating evidence id (review #6): the
                    # committed EvidenceRecord.actuation_trial_id bound to this
                    # intent at successful settlement - never a caller-forged
                    # result field. The drain API (deterministic / synchronous)
                    # later re-enters coordination via resolve_pending_intent -
                    # no LLM reparse, no unbounded recursion, no background thread.
                    try:
                        self.enqueue_assurance_violation(
                            intent, reason="assurance_drift",
                            evidence_id=self._assurance_evidence_id_for(intent))
                    except Exception as e:
                        logger.warning(f"assurance enqueue failed: {e}")
                    if self.on_intent_violated:
                        try:
                            self.on_intent_violated(intent)
                        except Exception as e:
                            logger.warning(f"on_intent_violated failed: {e}")
            # verdict is UNKNOWN: keep the current status (no flap)

    # -----------------------------------------------------------------------
    # P0-8: bounded continuous-assurance re-entry scheduler.
    #
    # Production drift violations are enqueued (not fired straight back into a
    # recursive callback) with the exact typed Intent + provenance, deduplicated
    # by a stable semantic key, and later drained DETERMINISTICALLY by the
    # runner/tests. Every drained re-entry traverses the full episode pipeline
    # via resolve_pending_intent (single-flight, deadline, latch, FSM, commit
    # invariant, evidence) - a latched coordinator never actuates.
    # -----------------------------------------------------------------------
    ASSURANCE_QUEUE_MAX = 64
    ASSURANCE_COMPLETED_MAX = 256

    def _ensure_assurance_state(self) -> None:
        """Lazily initialise the scheduler containers + their dedicated lock
        (safe on ``__new__``-built test skeletons that skip ``__init__``).

        The lock guards EVERY read/write of the queue and the pending / in-flight
        / completed / overflow / seq bookkeeping (coordinator review #3). It is
        NEVER held across resolve_pending_intent."""
        if not hasattr(self, "_assurance_lock"):
            self._assurance_lock = threading.RLock()
        if not hasattr(self, "_assurance_queue"):
            self._assurance_queue = collections.deque()  # FIFO of entry dicts
            self._assurance_pending_keys = set()  # keys queued, not yet drained
            self._assurance_inflight_keys = set() # keys currently draining
            self._assurance_completed = collections.deque()  # bounded recency
            self._assurance_completed_set = set()
            self._assurance_seq = 0
            # observable fail-closed overflow state (coordinator review #5)
            self._assurance_overflow_count = 0
            self._assurance_last_overflow = None
            # review #6: intent id -> AUTHORITATIVE committed evidence id
            # (EvidenceRecord.actuation_trial_id), bound at successful settlement.
            self._intent_evidence_id = {}

    @staticmethod
    def _describe_intent(intent) -> str:
        """A short human-readable label for a typed Intent (used as the episode
        ``intent_text`` surface for a typed re-entry)."""
        if intent is None:
            return ""
        try:
            typ = getattr(getattr(intent, "type", None), "value",
                          str(getattr(intent, "type", "")))
            t = getattr(intent, "target", None)
            if t is not None:
                cons = getattr(getattr(t, "constraint_type", None), "value", "")
                return (f"{typ} {cons} "
                        f"{getattr(t, 'target_value', '')}").strip()
            return str(typ)
        except Exception:
            return str(intent)

    def _assurance_dedup_key(self, intent) -> str:
        """A STABLE semantic dedup key for an intent (P0-8): the FULL semantic
        content hash (type + target + scope), so two events for the same
        violation - or two semantic ALIASES with different ids - collapse to a
        single scheduler entry and cannot spawn unbounded episodes."""
        return "asr:" + self._intent_content_hash(intent)

    def enqueue_assurance_violation(self, intent: Intent, *,
                                    reason: str = "assurance_violation",
                                    evidence_id: Optional[str] = None,
                                    intent_set_version: Optional[str] = None
                                    ) -> bool:
        """Enqueue a continuous-assurance violation for later deterministic
        re-entry. Returns True if newly enqueued, False if suppressed (a
        duplicate already pending/in-flight/recently-completed) or dropped (the
        bounded queue is full - fail-closed, never unbounded).

        The entry carries the EXACT typed Intent, the originating intent-set
        version, the re-entry reason and the originating evidence id. All queue /
        bookkeeping mutations are performed under the scheduler lock (review #3);
        the (potentially work-doing) intent-set-version hash is computed OUTSIDE
        the lock."""
        self._ensure_assurance_state()
        key = self._assurance_dedup_key(intent)
        isv = (intent_set_version if intent_set_version is not None
               else self._intent_set_version())          # outside the lock
        with self._assurance_lock:
            # Bounded pending / in-flight / completed suppression: a duplicate
            # violation event can never create a second episode for the same
            # semantic intent while one is queued, draining, or just completed.
            if (key in self._assurance_pending_keys
                    or key in self._assurance_inflight_keys
                    or key in self._assurance_completed_set):
                logger.debug(f"assurance violation {key} suppressed (duplicate)")
                return False
            if len(self._assurance_queue) >= int(getattr(
                    self, "assurance_queue_max", self.ASSURANCE_QUEUE_MAX)):
                # OBSERVABLE fail-closed overflow (review #5): bump a monotonic
                # counter and record the last-overflow audit (semantic key,
                # reason, origin version, evidence id, time). The intent stays
                # VIOLATED (the caller does not change status) and NOTHING is
                # actuated - the violation is simply not (re)queued.
                self._assurance_overflow_count += 1
                self._assurance_last_overflow = {
                    "dedup_key": key,
                    "reason": reason,
                    "origin_intent_set_version": isv,
                    "origin_evidence_id": evidence_id,
                    "time": time.time(),
                    "overflow_count": self._assurance_overflow_count,
                }
                logger.warning("assurance scheduler queue full - dropping "
                               "violation (bounded, fail-closed)")
                return False
            self._assurance_seq += 1
            self._assurance_queue.append({
                "intent": intent,                   # the EXACT typed Intent
                "dedup_key": key,
                "reason": reason,
                "origin_intent_set_version": isv,
                "origin_evidence_id": evidence_id,
                "seq": self._assurance_seq,
            })
            self._assurance_pending_keys.add(key)
            return True

    def assurance_queue_depth(self) -> int:
        """Number of violations currently queued (pending drain)."""
        self._ensure_assurance_state()
        with self._assurance_lock:
            return len(self._assurance_queue)

    def assurance_overflow_state(self) -> Dict:
        """Observable fail-closed overflow state (review #5): the monotonic
        overflow count and the last-overflow audit (or None)."""
        self._ensure_assurance_state()
        with self._assurance_lock:
            return {
                "overflow_count": self._assurance_overflow_count,
                "last_overflow": (dict(self._assurance_last_overflow)
                                  if self._assurance_last_overflow else None),
            }

    def _retire_assurance_key(self, intent: Intent) -> None:
        """Clear an intent's dedup key from the completed-suppression set on a
        verified SATISFIED transition (review #4), so a genuine later
        satisfied->violated recurrence enqueues again instead of being
        suppressed until 256 unrelated keys evict it. Pending / in-flight keys
        are left untouched (a queued/draining episode for it is legitimate)."""
        self._ensure_assurance_state()
        key = self._assurance_dedup_key(intent)
        with self._assurance_lock:
            if key in self._assurance_completed_set:
                self._assurance_completed_set.discard(key)
                try:
                    self._assurance_completed.remove(key)
                except ValueError:
                    pass

    def _bind_committed_evidence_id(self, intent, trial_id) -> None:
        """Bind the AUTHORITATIVE committed evidence id (the settled
        EvidenceRecord.actuation_trial_id) to the admitted intent at successful
        settlement (review #6), so a LATER assurance drift on that intent can
        carry a real origin evidence id - not a caller-forgeable result field.

        The COMMITTED semantic content hash is stored alongside the trial id
        (review item B), so an in-place target/scope mutation after commit
        cannot cause the old evidence to be misattributed to the changed
        intent."""
        if intent is None or not trial_id:
            return
        self._ensure_assurance_state()
        iid = getattr(intent, "id", None)
        if iid is None:
            return
        content = self._intent_content_hash(intent)      # outside the lock
        with self._assurance_lock:
            self._intent_evidence_id[iid] = {
                "trial_id": trial_id, "content_hash": content}

    def _assurance_evidence_id_for(self, intent) -> Optional[str]:
        """The authoritative committed evidence id bound to this intent at its
        last successful settlement (review #6/B), or None if it was never
        committed by this coordinator OR its semantic content has CHANGED since
        (a mutated target/scope must not claim the old evidence)."""
        self._ensure_assurance_state()
        iid = getattr(intent, "id", None)
        current = self._intent_content_hash(intent)      # outside the lock
        with self._assurance_lock:
            rec = self._intent_evidence_id.get(iid)
        if not rec:
            return None
        if rec.get("content_hash") != current:
            return None                                  # semantics changed
        return rec.get("trial_id")

    def _mark_assurance_completed(self, key: str) -> None:
        """Record a drained key in the bounded completed-recency set so an
        immediately re-fired duplicate is suppressed, while old keys evict so a
        genuinely recurring violation can eventually re-enter. Caller must NOT
        hold the lock (this takes it)."""
        self._ensure_assurance_state()
        cap = int(getattr(self, "assurance_completed_max",
                          self.ASSURANCE_COMPLETED_MAX))
        with self._assurance_lock:
            if key in self._assurance_completed_set:
                return
            self._assurance_completed.append(key)
            self._assurance_completed_set.add(key)
            while len(self._assurance_completed) > cap:
                old = self._assurance_completed.popleft()
                self._assurance_completed_set.discard(old)

    def drain_assurance_queue(self, *, max_items: Optional[int] = None
                              ) -> List[Dict]:
        """DETERMINISTIC, SYNCHRONOUS drain of the assurance queue (P0-8): pop
        each queued violation in FIFO order and re-enter coordination through
        resolve_pending_intent (the typed core - NO reparse). Returns the list
        of episode result dicts.

        This is the runner/test-facing API that REPLACES unbounded callback
        recursion / an unmanaged background thread. It must not run re-entrantly
        inside an episode (that would break single-flight): calling it while an
        episode is in flight fails closed with a RuntimeError. ``max_items``
        bounds how many entries are processed in one drain (budget).

        The scheduler lock is held ONLY for the O(1) pop + bookkeeping mutations
        (review #3); it is RELEASED across resolve_pending_intent (a full
        episode)."""
        self._ensure_assurance_state()
        if getattr(self, "_episode_in_flight", False):
            raise RuntimeError(
                "drain_assurance_queue called during an in-flight episode - "
                "no recursive re-entry (single-flight, P0-8)")
        results: List[Dict] = []
        processed = 0
        while True:
            if max_items is not None and processed >= int(max_items):
                break
            with self._assurance_lock:
                if not self._assurance_queue:
                    break
                entry = self._assurance_queue.popleft()
                key = entry["dedup_key"]
                self._assurance_pending_keys.discard(key)
                self._assurance_inflight_keys.add(key)
            try:
                trigger_context = {
                    "reentry": True,
                    "reentry_reason": entry["reason"],
                    "origin_intent_set_version":
                        entry["origin_intent_set_version"],
                    "origin_evidence_id": entry["origin_evidence_id"],
                    "assurance_seq": entry["seq"],
                }
                # NOTE: the scheduler lock is NOT held here (review #3).
                results.append(self.resolve_pending_intent(
                    entry["intent"], trigger_context=trigger_context))
            finally:
                with self._assurance_lock:
                    self._assurance_inflight_keys.discard(key)
                self._mark_assurance_completed(key)
            processed += 1
        return results

    @staticmethod
    def _finite(value) -> bool:
        """True for a real, finite KPI observation (None/NaN/Inf -> False)."""
        return value is not None and math.isfinite(value)

    # The comparison direction each evaluator implements. An intent whose
    # constraint_type disagrees would be evaluated backwards, so it is
    # unsupported (ValueError) regardless of how it entered the system -
    # _parse_intent rejects it for LLM-parsed intents, this enforces the
    # same contract for programmatic/active ones.
    _EVAL_DIRECTION = {
        IntentType.THROUGHPUT_GOAL: ConstraintType.MIN,
        IntentType.THROUGHPUT_FAIRNESS: ConstraintType.MAX,
        IntentType.LATENCY_GOAL: ConstraintType.MAX,
        IntentType.POWER_CONSTRAINT: ConstraintType.MAX,
    }

    def _evaluate_intent(self, intent: Intent, metrics: Dict[str, UEMetrics]) -> bool:
        """Legacy BOOL adapter over the authoritative typed evaluator.

        Fail-closed: True only for a positively VERIFIED intent (SATISFIED);
        VIOLATED and UNKNOWN are both False. Kept because existing callers/
        tests consume a bool; the authority is _evaluate_intent_verdict.
        Raises ValueError for unsupported types/directions (propagated).
        """
        return (self._evaluate_intent_verdict(intent, metrics)
                is MonitorVerdict.SATISFIED)

    def _evaluate_intent_tristate(self, intent: Intent,
                                  metrics: Dict[str, UEMetrics]) -> str:
        """Legacy STRING adapter -> "satisfied" | "violated" | "unknown".

        Kept for existing string consumers/tests; the authority is
        _evaluate_intent_verdict (the enum values ARE these strings)."""
        return self._evaluate_intent_verdict(intent, metrics).value

    def evaluate_joint_intent_set(self, pending: Optional[Intent],
                                  monitored: List[Intent],
                                  metrics: Dict[str, UEMetrics]) -> Dict:
        """P0-9: the ONE authoritative joint evaluation of the whole intent set
        (pending + every monitored intent) against ONE fresh observation bundle.

        Every S4 validation and the no-conflict/already-satisfied shortcut route
        through here, so a single typed verdict rule governs both. Returns the
        per-intent typed MonitorVerdict values plus the joint satisfaction flag:

          joint_satisfied is True ONLY when the set is non-empty and EVERY
          member evaluates to SATISFIED. A single VIOLATED or UNKNOWN (a
          telemetry gap or an unsupported/unverifiable intent) makes the joint
          verdict False - it is never coerced to satisfied.

        Deduplication is SEMANTIC, not blind-by-id (coordinator review #1):
        a later entry is dropped ONLY when it is the SAME object or a
        semantically identical clone (same full content hash) of an earlier one.
        If the SAME id reappears with DIFFERENT full semantic content
        (type/target/scope), that is a CONFLICT - the id is emitted with an
        explicit UNKNOWN verdict and joint_satisfied is forced False, so a
        differing pending intent can never be silently dropped by the
        monitored-first ordering. Two DISTINCT ids with identical semantics stay
        independently represented.
        """
        # first pass: order + detect same-id-different-content conflicts.
        first_by_id: Dict[str, str] = {}     # id -> first content hash seen
        seen_objs = set()
        conflict_ids = set()
        ordered: List[Intent] = []
        for intent in list(monitored or []) + ([pending] if pending is not None
                                               else []):
            if intent is None:
                continue
            if id(intent) in seen_objs:
                continue                     # exact same object: true duplicate
            iid = getattr(intent, "id", None)
            content = self._intent_content_hash(intent)
            if iid is not None:
                if iid in first_by_id:
                    if first_by_id[iid] == content:
                        seen_objs.add(id(intent))
                        continue             # semantically identical clone: drop
                    # SAME id, DIFFERENT semantics -> conflict (fail-closed).
                    conflict_ids.add(iid)
                    seen_objs.add(id(intent))
                    continue
                first_by_id[iid] = content
            seen_objs.add(id(intent))
            ordered.append(intent)

        verdict_objs: Dict[str, MonitorVerdict] = {}
        verdicts: Dict[str, str] = {}
        any_violated = any_unknown = False
        for intent in ordered:
            iid = getattr(intent, "id", None) or f"anon-{id(intent)}"
            if iid in conflict_ids:
                # a conflicting id cannot be trusted to one verdict: UNKNOWN.
                verdict = MonitorVerdict.UNKNOWN
            else:
                try:
                    verdict = self._evaluate_intent_verdict(intent, metrics)
                except ValueError as e:
                    # unsupported/malformed intent: fail-closed UNKNOWN, never
                    # silently satisfied.
                    logger.warning(f"joint eval: intent "
                                   f"{getattr(intent, 'id', '?')}: {e}")
                    verdict = MonitorVerdict.UNKNOWN
            verdict_objs[iid] = verdict
            verdicts[iid] = verdict.value
            if verdict is MonitorVerdict.VIOLATED:
                any_violated = True
            elif verdict is not MonitorVerdict.SATISFIED:
                any_unknown = True
        # any same-id/different-content conflict forces a non-satisfied joint,
        # even if its (single kept) entry happened to evaluate satisfied.
        if conflict_ids:
            any_unknown = True
        joint_satisfied = bool(ordered) and not any_violated and not any_unknown
        return {
            "verdicts": verdicts,
            "verdict_objs": verdict_objs,
            "joint_satisfied": joint_satisfied,
            "any_violated": any_violated,
            "any_unknown": any_unknown,
            "conflict_ids": tuple(sorted(conflict_ids)),
            "required_intent_ids": tuple(
                getattr(i, "id", None) or f"anon-{id(i)}" for i in ordered),
        }

    def _evaluate_intent_verdict(self, intent: Intent,
                                 metrics: Dict[str, UEMetrics]) -> MonitorVerdict:
        """Authoritative typed intent evaluation (section 3.2): returns a
        MonitorVerdict SATISFIED | VIOLATED | UNKNOWN.

        VIOLATED is a POSITIVE observation of non-compliance (a measured KPI
        breaks the target, or a scoped UE is detached). UNKNOWN means the
        needed measurements are missing or non-finite (telemetry gap): S1/S4
        callers fail closed on it, the continuous monitor keeps the previous
        status - UNKNOWN is NEVER turned into SATISFIED. A confirmed violation
        on any scoped UE outranks unknowns elsewhere. Raises ValueError for
        unsupported types/directions.
        """
        expected_dir = self._EVAL_DIRECTION.get(intent.type)
        if (expected_dir is not None and intent.target is not None
                and intent.target.constraint_type != expected_dir):
            raise ValueError(
                f"constraint {intent.target.constraint_type.value!r} "
                f"unsupported for {getattr(intent.type, 'value', intent.type)!r} "
                f"(evaluator implements {expected_dir.value!r} only, "
                f"fail-closed)")
        # A non-finite TARGET is a malformed intent: comparisons against NaN
        # are all False, which silently fail-OPENED (NaN throughput floor ->
        # never "below target" -> satisfied). _parse_intent rejects these
        # for LLM-parsed intents; this guards programmatic ones.
        if (intent.target is not None
                and not self._finite(intent.target.target_value)):
            raise ValueError(
                f"non-finite target value {intent.target.target_value!r} "
                f"(fail-closed)")

        if intent.type == IntentType.THROUGHPUT_GOAL:
            ue_ids = intent.scope.ue_ids or list(metrics.keys())
            if not ue_ids:
                return MonitorVerdict.UNKNOWN   # nothing to verify against
            violated = unknown = False
            for ue_id in ue_ids:
                m = metrics.get(ue_id)
                if m is None:
                    unknown = True
                elif not getattr(m, "attached", True):
                    violated = True    # a detached UE is not served at all
                elif not self._finite(m.throughput_mbps):
                    unknown = True     # attached but no usable measurement
                elif m.throughput_mbps < intent.target.target_value:
                    violated = True
            if violated:
                return MonitorVerdict.VIOLATED
            return (MonitorVerdict.UNKNOWN if unknown
                    else MonitorVerdict.SATISFIED)

        elif intent.type == IntentType.THROUGHPUT_FAIRNESS:
            # P0-9: fairness is evaluated over EXACTLY the intent's scoped UE
            # population when scope.ue_ids is supplied - an unrelated UE must
            # never widen or narrow the fairness set. When the scope is empty
            # the legacy every-observed-UE semantics are preserved.
            raw_scoped = [str(u) for u in (intent.scope.ue_ids or [])]
            # Dedup repeated scope members (coordinator review #2): [ue1, ue1]
            # is ONE UE, never two - a duplicated id must not fake a >=2 member
            # population. Require at least two UNIQUE members after dedup.
            scoped = list(dict.fromkeys(raw_scoped))
            if raw_scoped:
                if len(scoped) < 2:
                    return MonitorVerdict.UNKNOWN   # < 2 unique members
                throughputs = []
                violated = unknown = False
                for ue_id in scoped:
                    m = metrics.get(ue_id)
                    if m is None:
                        unknown = True          # scoped member absent: gap
                    elif not getattr(m, "attached", True):
                        violated = True          # detached member is not served
                    elif not self._finite(getattr(m, "throughput_mbps", None)):
                        unknown = True           # attached but no usable KPI
                    else:
                        throughputs.append(m.throughput_mbps)
                # A CONFIRMED detached member outranks an unknown elsewhere
                # (same rule as the per-UE branches); a detached UE whose stale
                # cached throughput was merged still fails here on attachment.
                if violated:
                    return MonitorVerdict.VIOLATED
                # Every scoped member must be present, attached and finite:
                # a missing/unusable member leaves the fairness set incomplete,
                # so it can never be positively SATISFIED (fail-closed UNKNOWN).
                if unknown or len(throughputs) != len(scoped):
                    return MonitorVerdict.UNKNOWN
            else:
                # Legacy: EVERY observed UE must report a finite throughput.
                # Filtering non-finite samples out would let [10, 10, NaN] pass
                # a variance ceiling (fairness among an unverifiable population).
                throughputs = [m.throughput_mbps for m in metrics.values()]
                if len(throughputs) < 2:
                    return MonitorVerdict.UNKNOWN  # need >= 2 measurements
                if not all(self._finite(t) for t in throughputs):
                    return MonitorVerdict.UNKNOWN
            mean = sum(throughputs) / len(throughputs)
            variance = sum((t - mean) ** 2 for t in throughputs) / len(throughputs)
            return (MonitorVerdict.SATISFIED
                    if variance <= intent.target.target_value
                    else MonitorVerdict.VIOLATED)

        elif intent.type == IntentType.LATENCY_GOAL:
            ue_ids = intent.scope.ue_ids or list(metrics.keys())
            if not ue_ids:
                return MonitorVerdict.UNKNOWN
            violated = unknown = False
            for ue_id in ue_ids:
                m = metrics.get(ue_id)
                if m is None:
                    unknown = True
                elif not getattr(m, "attached", True):
                    violated = True
                elif not self._finite(m.latency_ms):
                    unknown = True
                elif m.latency_ms > intent.target.target_value:
                    violated = True
            if violated:
                return MonitorVerdict.VIOLATED
            return (MonitorVerdict.UNKNOWN if unknown
                    else MonitorVerdict.SATISFIED)

        elif intent.type == IntentType.POWER_CONSTRAINT:
            # |power offset| must stay within the target bound. With
            # scope.bs_ids the constraint is NARROWED to those gNBs
            # ("bs2" -> "gnb2"; the paper's I1 guards BS2 only, C7); an
            # empty scope keeps the legacy every-gNB semantics. A CONFIRMED
            # out-of-bound offset outranks an unknown elsewhere (same rule
            # as the per-UE branches).
            offsets = self.executor.get_all_offsets()
            bs_ids = list(getattr(intent.scope, "bs_ids", []) or [])
            missing_scope = False
            if bs_ids:
                wanted = {("gnb" + str(b)[-1]) if str(b).startswith("bs")
                          else str(b) for b in bs_ids}
                missing_scope = not wanted <= set(offsets)
                offsets = {g: o for g, o in offsets.items() if g in wanted}
            if not offsets:
                return MonitorVerdict.UNKNOWN  # no observable offsets
            bound = abs(intent.target.target_value)
            if any(self._finite(off) and abs(off) > bound
                   for off in offsets.values()):
                return MonitorVerdict.VIOLATED
            if missing_scope or not all(self._finite(off)
                                        for off in offsets.values()):
                return MonitorVerdict.UNKNOWN
            return MonitorVerdict.SATISFIED

        # Fail-closed: an intent type this coordinator cannot measure must
        # never evaluate as satisfied (RSRP_CONSTRAINT / ENERGY_EFFICIENCY /
        # COVERAGE_GOAL previously returned True here). Callers catch this
        # and treat the intent as NOT satisfied. getattr: intent.type may be
        # a malformed non-enum value - the error must still be a ValueError,
        # never an AttributeError that would escape the callers' handlers.
        raise ValueError(
            f"unsupported intent type "
            f"{getattr(intent.type, 'value', intent.type)!r}: no evaluator "
            f"(fail-closed)")

    def process_intent(self, intent_text: str) -> Dict:
        """Backward-compatible natural-language entry point (P0-8 wrapper).

        Delegates to ``process_intent_text``; kept so existing callers/tests
        that pass raw operator text keep working unchanged. The typed core is
        ``resolve_pending_intent`` (used for assurance re-entry, no reparse).
        """
        return self.process_intent_text(intent_text)

    def process_intent_text(self, intent_text: str, *,
                            trigger_context: Optional[Dict] = None) -> Dict:
        """P0-8: the natural-language wrapper. Runs the S0-S6 episode with the
        raw operator text - the LLM parse happens INSIDE the locked core so a
        malformed input still fails closed to a finalized episode with the
        schema verdict on its evidence record."""
        return self._run_episode_single_flight(
            pending_ref=intent_text,
            body=lambda: self._process_intent_locked(
                intent_text=intent_text, trigger_context=trigger_context))

    def resolve_pending_intent(self, intent: Intent, *,
                               trigger_context: Optional[Dict] = None) -> Dict:
        """P0-8: the TYPED core entry point. Runs the S0-S6 episode against an
        already-typed Intent with NO LLM reparse - used for continuous-assurance
        re-entry so a drift violation carries the exact typed Intent forward.

        Re-entry traverses the IDENTICAL pipeline as a fresh operator episode:
        the single-flight lock, the absolute episode deadline, the risk/
        calibration ledger, the safety latch (a latched coordinator fails closed
        to TechnicalFailsafe and never actuates), the FSM, the commit invariant,
        the terminal-outcome and the evidence contracts. The trigger context is
        preserved in the result, evidence and audit records."""
        return self._run_episode_single_flight(
            pending_ref=intent,
            body=lambda: self._process_intent_locked(
                typed_intent=intent, trigger_context=trigger_context))

    def _run_episode_single_flight(self, *, pending_ref, body) -> Dict:
        """Shared single-flight harness for every episode entry point (P0-8).

        H1: the whole episode is a SINGLE-FLIGHT transaction - concurrent or
        re-entrant callers (including an assurance drain that fires mid-episode)
        are rejected rather than allowed to interleave S0-S6 snapshots/rollback
        targets. Settlement runs while still single-flight.
        """
        # P1-3 (blocker 2): the AUTHORITATIVE episode total is measured here, at
        # the public single-flight boundary, on a MONOTONIC clock - so it covers
        # the whole completed transaction (body + settlement + finalization)
        # exactly once, including the re-entry-rejection path.
        _episode_t0 = time.monotonic()
        lock = getattr(self, "_episode_lock", None)
        if lock is None:
            # __new__-built test skeletons skip __init__; the lazy path is
            # not itself race-free, but those skeletons are single-threaded
            lock = self._episode_lock = threading.RLock()
        with lock:
            if getattr(self, "_episode_in_flight", False):
                # same-thread recursion (an in-episode callback called back
                # into an episode entry): a nested S0-S6 between the outer
                # snapshot and rollback would break single-flight - reject
                # explicitly rather than deadlock (plain Lock) or silently
                # interleave (bare RLock).
                msg = ("episode already in flight - re-entrant "
                       "process_intent rejected (single-flight, H1)")
                logger.warning(msg)
                reentry = {"success": False,
                           "intent_text": (pending_ref
                                           if isinstance(pending_ref, str)
                                           else str(pending_ref)),
                           "resolution": "reject", "error": msg,
                           "latency_ms": None}
                finalized = self._finalize_episode(
                    reentry, TerminalOutcome.PENDING_NOT_ADMITTED,
                    TerminalReason.SINGLE_FLIGHT_REJECTED,
                    pending_intent=pending_ref)
                # P1-3 (blocker 2/review-4): stamp the ACTUAL measured elapsed
                # AFTER finalization so the total covers finalization too - never
                # a hard-coded 0 and never missing the finalize time.
                if isinstance(finalized, dict):
                    finalized["latency_ms"] = (
                        time.monotonic() - _episode_t0) * 1000.0
                return finalized
            self._episode_in_flight = True
            self._pending_history = None
            final_result = None
            result = None
            try:
                result = body()
                # item 1 (final): SETTLEMENT (admission) runs while the episode
                # is STILL single-flight. The transaction was kept OPEN through
                # _process_intent_locked's own S0 cleanup; a custom / observable
                # IntentManager.add must NOT be able to re-enter an episode on
                # this same RLock thread while the validated configuration is
                # pending. _episode_in_flight is cleared ONLY in this OUTERMOST
                # finally, after settlement returns or raises.
                final_result = self._settle_after_cleanup(result)
                return final_result
            finally:
                # Batch E (two-phase history + outer pin lifetime): PHASE 2 -
                # publish EXACTLY ONE history record matching the TRULY FINAL
                # outcome (settlement may have downgraded a commit), attributed
                # to the ORIGINAL pinned cycle. The pin is STILL live here, so a
                # settlement-failure downgrade still names the original
                # proposer/model. THEN clear the stale pin (queued switch
                # requests are preserved for the next episode). Order matters:
                # publish -> clear pin -> release single-flight.
                published = final_result if final_result is not None \
                    else result
                # P1-3 (blocker 2): stamp the AUTHORITATIVE total ONCE, here at the
                # completed public boundary (after settlement), overriding the
                # provisional body-only value. Monotonic elapsed; covers the whole
                # transaction exactly once.
                if isinstance(published, dict):
                    published["latency_ms"] = (
                        time.monotonic() - _episode_t0) * 1000.0
                if published is not None:
                    try:
                        self._publish_history(published)
                    except Exception as pub_err:
                        logger.error(f"history publish failed: {pub_err}")
                    # GUI (Sec IV / Eq.12): report the TRULY-FINAL terminal
                    # outcome here - AFTER settlement may have downgraded a
                    # commit - so the dashboard's terminal-state indicator can
                    # never show an outcome the settlement later revoked. Pure
                    # display side-effect, isolated via _safe_gui.
                    if isinstance(published, dict):
                        self._safe_gui("update_terminal_outcome",
                                       published.get("terminal_outcome"))
                self._proposer_ctx = None
                self._episode_in_flight = False

    def _process_intent_locked(self, intent_text: Optional[str] = None, *,
                               typed_intent: Optional[Intent] = None,
                               trigger_context: Optional[Dict] = None) -> Dict:
        """The S0-S6 episode body (call via an entry point that holds the
        episode lock).

        Exactly one of ``intent_text`` (natural-language: parsed at S0) or
        ``typed_intent`` (P0-8 typed re-entry: used as-is, NO reparse) is
        supplied. ``trigger_context`` carries the re-entry provenance and is
        preserved on the result / evidence / audit records."""
        start_time = time.time()
        # Batch G (P1-6): the ACTUAL monotonic episode-start timestamp (distinct
        # from the epoch start_time), carried into the raw record so an episode has
        # a real monotonic time anchor (not only a deterministic sequence number).
        episode_monotonic_s = time.monotonic()
        display_text = (intent_text if intent_text is not None
                        else self._describe_intent(typed_intent))
        result = {
            "success": False,
            "intent_text": display_text,
            "resolution": None,
            "latency_ms": 0,
            "episode_monotonic_s": episode_monotonic_s,
        }
        # P0-8: preserve the trigger context on the result (and, via
        # _build_evidence / _audit_from_tx, on the evidence and audit records).
        # DEEP-COPY once at episode entry (review gap 1): a shallow dict() would
        # keep the caller's NESTED objects aliased, so a caller mutation before
        # the tx captures the context could still corrupt provenance. Coordinator
        # state and the public result each get their OWN independent deep copy.
        self._active_trigger_context = copy.deepcopy(dict(trigger_context or {}))
        if self._active_trigger_context:
            result["trigger_context"] = copy.deepcopy(self._active_trigger_context)
        # Blocker 8: fresh per-episode trusted calibration provenance (bound at
        # S2 routing; the authoritative source for evidence/finalization).
        self._active_calibration = {}
        # Review: FREEZE one (model_id, operating_regime_id) context pair per
        # episode and reuse it for N_max / theta / calibration / proposal
        # counting / tx / evidence - never re-reading the active backend
        # mid-cycle. _calibration_context_ok is False when the partition could
        # not be selected (a fail-closed condition, no model-dependent trial).
        self._episode_context = None
        self._calibration_context_ok = True
        # Batch E (P0-15): the finalized-record signature is captured at THIS
        # episode's feasibility; clear it so a parse-fail episode (which never
        # reaches feasibility) never learns a stale prior signature.
        self._last_intent_signature = None
        self._begin_episode(result)
        # fresh transaction slot per episode (a prior episode's settled tx must
        # not be re-settled by this episode's outer settlement).
        self._active_txn = None
        # the live result (so the commit gate can read the current identifier
        # chain from the last cycle at settlement).
        self._active_result = result
        # Batch C (P0-7): fix ONE absolute monotonic deadline for the whole
        # episode and reset the FSM step budget.  Every bounded external call is
        # sub-bounded to the remaining time from here.
        self._deadline = EpisodeDeadline(
            float(getattr(self, "episode_budget_s", 120.0)))
        self._fsm_steps = 0
        # Batch D (P0-10): fresh per-episode runtime reserve ledger. Reserves
        # deterministic worst-case cost before the initial trial / each
        # negotiation / each revised trial; the AUTHORITATIVE budget guard.
        self._reserve_ledger = self._new_reserve_ledger()
        result["reserve_ledger"] = self._reserve_ledger.to_dict()
        # per-episode strict-schema verdict (reset before each proposal; read
        # into the cycle/evidence record). Default valid so a monkeypatched
        # boundary that does not set it is treated as schema-valid.
        self._cur_schema_valid = True
        self._cur_schema_reason = None
        # The pending intent is preserved on EVERY terminal path (P0-4): raw
        # text until parsed, the structured Intent afterwards. For a P0-8 typed
        # re-entry the pending intent is the typed Intent from the very start.
        # committed_revision stays None unless a revised intent is executed.
        pending_ref = typed_intent if typed_intent is not None else intent_text
        committed_ref = None

        try:
            # Batch B (P0-1): a LATCHED coordinator refuses EVERY new episode
            # until an operator recovery API (clear_safety_latch) verifies
            # readback against the recovery/baseline target. No parse, no LLM,
            # no cycle, no write happens - fail closed to TechnicalFailsafe.
            if self._is_latched():
                result["error"] = ("coordinator latched in TechnicalFailsafe - "
                                   "operator recovery required before any new "
                                   "episode")
                self._log_gui(result["error"])
                return self._finalize_episode(
                    result, TerminalOutcome.TECHNICAL_FAILSAFE,
                    TerminalReason.SAFETY_LATCHED, pending_intent=pending_ref)

            # S0: Normal operation - obtain the typed intent
            self._transition("S0")
            self._log_gui(f"Processing: {display_text}")

            # Batch E (P0-16): the FIRST proposal boundary. Drain any queued
            # switch, apply it atomically, and PIN the backend object BEFORE the
            # parse - so the parser and this episode's cycle 0 (parse ->
            # feasibility -> revision) are all served by the SAME pinned model
            # even if the manager's active backend is switched mid-episode.
            self._open_proposal_boundary(0, None, result.get("episode_id"))

            if typed_intent is not None:
                # P0-8 typed re-entry: the Intent is already validated (it came
                # from a prior admitted/monitored episode). NO LLM reparse - use
                # it verbatim so a drift violation carries the exact typed Intent
                # forward. The schema verdict is trivially valid for a typed
                # re-entry (there is no natural-language surface to reject).
                new_intent = typed_intent
                self._cur_schema_valid = True
                self._cur_schema_reason = None
            else:
                # Parse intent using LLM (fail-closed: unsupported/malformed
                # content is rejected here and never reaches the RAN).
                # Batch G (P1-3): measure the parse-latency segment in a finally so
                # a parse rejection/exception still records the real latency.
                _parse_t0 = time.monotonic()
                try:
                    parsed = self._parse_intent(intent_text)
                except IntentParseError as e:
                    result["parse_ms"] = (time.monotonic() - _parse_t0) * 1000.0
                    result["error"] = f"intent rejected (fail-closed): {e}"
                    self._log_gui(result["error"])
                    # P0-5: surface the actual schema verdict + reason even though
                    # NO cycle exists (the evidence record binds it).
                    result["schema_valid"] = False
                    result["schema_reject_reason"] = str(e)
                    self._cur_schema_valid = False
                    self._cur_schema_reason = str(e)
                    # pending intent preserved as the RAW text (parse failed)
                    return self._finalize_episode(
                        result, TerminalOutcome.PENDING_NOT_ADMITTED,
                        TerminalReason.INPUT_SCHEMA_REJECTED,
                        pending_intent=pending_ref)
                result["parse_ms"] = (time.monotonic() - _parse_t0) * 1000.0
                if not parsed:
                    result["error"] = "Failed to parse intent"
                    return self._finalize_episode(
                        result, TerminalOutcome.PENDING_NOT_ADMITTED,
                        TerminalReason.PARSE_FAILED, pending_intent=pending_ref)
                new_intent = parsed["intent"]

            pending_ref = new_intent   # structured pending intent from here on
            # Programmatic path guard (LLM-parsed intents are already
            # restricted by _parse_intent): an intent type without an
            # evaluator can never be validated at S4, so it must not reach
            # S2/S3 at all (fail-closed) - previously it would have sailed
            # through and "validated" via the fail-open evaluator.
            if new_intent.type not in SUPPORTED_INTENT_TYPES:
                result["error"] = (
                    f"intent rejected (fail-closed): unsupported intent type "
                    f"{getattr(new_intent.type, 'value', new_intent.type)!r}")
                self._log_gui(result["error"])
                return self._finalize_episode(
                    result, TerminalOutcome.PENDING_NOT_ADMITTED,
                    TerminalReason.UNSUPPORTED_INTENT,
                    pending_intent=pending_ref)
            result["parsed_intent"] = new_intent.to_dict()

            # S1: Conflict screening. The coordination context is the full
            # MONITORED set (ACTIVE + SATISFIED + VIOLATED): a satisfied
            # intent must still be screened against, given to the LLM, and
            # validated at S4 (C2) - it used to drop out once satisfied.
            self._transition("S1")
            active_intents = self.intent_manager.get_monitored()
            has_conflict = self._check_conflicts(new_intent, active_intents)

            if not has_conflict:
                # P0-9: the already-satisfied shortcut routes through the ONE
                # authoritative joint evaluation over the pending intent AND the
                # whole monitored set, using exactly ONE fresh observation
                # bundle. It must NEVER admit if any monitored or pending intent
                # is VIOLATED or UNKNOWN.
                # review (item A): bound the collection window - start BEFORE
                # the single collect_all, end AFTER - and preserve the EXACT
                # deep-copied per-UE bundle (attachment + every KPI), not a lossy
                # scalar summary.
                collect_start = time.time()
                metrics = self.ue_collector.collect_all()   # exactly one bundle
                collect_end = time.time()
                joint = self.evaluate_joint_intent_set(
                    new_intent, active_intents, metrics)
                # record the typed joint verdicts as evidence for both branches
                result["monitor_verdicts"] = dict(joint["verdicts"])
                # the EXACT observed per-UE data, deep-copied so a later mutation
                # of the live metrics cannot change the recorded evidence.
                bundle = {
                    ue_id: copy.deepcopy(m.to_dict()) if hasattr(m, "to_dict")
                    else copy.deepcopy(m)
                    for ue_id, m in metrics.items()}
                # review (item A / P0-9): record the EXACT single collect_all
                # bundle as ONE timestamped Observation at result level, so the
                # no-cycle shortcut's evidence carries a real (single) observation
                # with the typed verdicts and the full per-UE bundle - and NO
                # fabricated action / read-back.
                result["observations"] = [Observation(
                    source="joint_shortcut",
                    value=self._mean_tput_of(metrics),
                    sample_time=collect_end,
                    collection_start=collect_start, collection_end=collect_end,
                    freshness_verdict="fresh", details=bundle).to_dict()]
                result["joint_evaluation"] = {
                    "joint_satisfied": bool(joint["joint_satisfied"]),
                    "verdicts": dict(joint["verdicts"]),
                    "any_violated": bool(joint["any_violated"]),
                    "any_unknown": bool(joint["any_unknown"]),
                    "required_intent_ids": list(joint["required_intent_ids"]),
                }
                if joint["joint_satisfied"]:
                    # The pending intent AND every monitored intent are JOINTLY
                    # satisfied under one fresh bundle. Under the P0-6 commit
                    # invariant an actuation-free "already satisfied" admission
                    # cannot be represented honestly (there is no write /
                    # read-back to bind), so we do NOT invent a fake write/
                    # read-back: the episode stays PendingNotAdmitted /
                    # ALREADY_SATISFIED_UNVERIFIED - but only AFTER the joint
                    # evaluation, with the typed joint verdicts recorded.
                    self._log_gui(
                        "Intent + monitored set jointly satisfied - not admitted "
                        "(actuation-free admission unrepresentable under P0-6)")
                    return self._finalize_episode(
                        result, TerminalOutcome.PENDING_NOT_ADMITTED,
                        TerminalReason.ALREADY_SATISFIED_UNVERIFIED,
                        pending_intent=pending_ref)
                # NOT jointly satisfied (a monitored or pending intent is
                # VIOLATED / UNKNOWN): the shortcut must not admit - fall through
                # to a coordination cycle that can actuate to repair the set.

            # === Autonomous coordination cycles (C1) ===
            # One cycle = S2 feasibility -> theta* routing -> optional
            # S3 trial + S4 validation. An accepted NEGOTIATION alternative
            # no longer resolves the episode by agreement alone: its
            # modified intent is MATERIALIZED and re-executed as a new
            # cycle, and only an S4-validated configuration makes
            # result["success"] True (execution success). The agreement
            # itself is reported separately in result["agreement"].
            #
            # Eq. 9 budget (Codex acceptance #3): ONE frozen per-episode
            # negotiation-round budget. N_max bounds the TOTAL policy
            # consultations across the whole episode - every cycle's
            # negotiation draws from the same remaining budget - replacing
            # the previous double use (outer cycle budget + independent
            # per-negotiation round budget) that allowed O(N_max^2) total
            # consultations. Re-entries are implicitly bounded too: each
            # S5 -> S2 re-entry requires an ACCEPTED round, so
            # cycles <= accepted rounds <= N_max. Frozen at episode start:
            # mid-episode recalibration must not inflate the budget
            # (observed >100 cycles in emulated runs before the freeze).
            cycles = 0
            current_intent = new_intent
            result["cycles"] = []
            result["agreement"] = {"accepted": False, "alternative_id": None,
                                   "target_value": None}
            # Blocker 2 (context order): FREEZE this episode's (model, regime)
            # partition BEFORE the N_max budget, so the frozen budget is that
            # partition's (never a previously-active model's). A failed selection
            # sets _calibration_context_ok=False -> routing fails closed later.
            self._freeze_episode_context()
            get_n_max = getattr(self.calibrator, "get_n_max", None)
            episode_n_max = (int(get_n_max()) if callable(get_n_max)
                             else int(getattr(self.calibrator, "n_max", 0)))
            remaining_rounds = episode_n_max
            # terminal decision for this episode (P0-4): set exactly once at
            # each loop exit, then funneled through _finalize_episode below.
            episode_outcome = None
            episode_reason = None
            while True:
                # mint the cycle id WHEN the cycle begins (P0-6/P1-6 identifier
                # honesty): it is persisted here and reused in terminal evidence,
                # never invented at finalization.
                # P0-6 (coordinator review): the episode/fsm ids are on EVERY
                # cycle BEFORE authorization, so the authorization + evidence
                # bind the SAME complete identifier chain (episode id was
                # previously None on the authorization).
                cycle = {"cycle": cycles, "cycle_id": new_id("cycle"),
                         "episode_id": result.get("episode_id"),
                         "fsm_step_id": result.get("fsm_step_id")}
                result["cycles"].append(cycle)
                # Batch E (P0-16): cycle 0 keeps the pre-parse pin (never re-read
                # the manager, so an external mid-cycle mutation cannot leak in)
                # - only its cycle_id is attached. A RE-ENTRY cycle (cycles > 0)
                # is a NEW proposal boundary: drain + apply any queued switch and
                # re-pin, so the NEXT cycle uses the newly applied model and its
                # own calibration partition.
                if cycles == 0:
                    self._refine_pin_cycle(0, cycle["cycle_id"])
                else:
                    self._open_proposal_boundary(
                        cycles, cycle["cycle_id"], result.get("episode_id"))
                # Batch G (P1-6): stamp CYCLE-LOCAL provenance AFTER the pin, so a
                # re-entry cycle carries its OWN pinned proposer/model + its OWN
                # (revised) current_intent snapshot - never the original cycle's.
                self._stamp_cycle_provenance(cycle, current_intent)
                cycle_active = self._coordination_context(current_intent)
                try:
                    feasibility, committed = self._coordination_cycle(
                        cycle, current_intent, cycle_active)
                except Exception:
                    # legacy top-level contract: an S4 error that rolled
                    # back must surface rolled_back on the result
                    if cycle.get("rolled_back"):
                        result["rolled_back"] = True
                    # exactly-one-record-per-cycle holds on exception paths
                    # too, once a routing decision was made (a pre-S2 crash
                    # has nothing calibratable to record)
                    if cycle.get("routed_to"):
                        try:
                            self._record_cycle(cycle, episode_success=False)
                        except Exception as rec_err:
                            logger.warning(f"cycle record failed: {rec_err}")
                    raise
                # top-level feasibility mirrors the LAST cycle (GUI/legacy)
                result["feasibility"] = {
                    "feasible": feasibility.feasible,
                    "confidence": feasibility.confidence,
                    "reasoning": feasibility.reasoning
                }

                if committed:
                    # cycle 0 committed the original intent; a later cycle
                    # committed a revised (relaxed) intent that was actually
                    # re-executed and S4-validated (CommitRevised requires
                    # execution, not mere agreement - P0-4). BOTH commits name
                    # the EXACT executed intent: current_intent is the original
                    # on cycle 0 (revision 0) and the executed revision after.
                    episode_outcome = (TerminalOutcome.COMMIT_ORIGINAL
                                       if cycles == 0
                                       else TerminalOutcome.COMMIT_REVISED)
                    episode_reason = TerminalReason.COMMIT_VERIFIED
                    committed_ref = current_intent
                    # stash the actual settlement objects on the OPEN
                    # transaction; the outer settlement (post-cleanup) admits.
                    tx_open = getattr(self, "_active_txn", None)
                    if tx_open is not None:
                        tx_open.pending_intent = pending_ref
                        tx_open.committed_intent = committed_ref
                        tx_open.commit_outcome = episode_outcome
                        tx_open.commit_reason = episode_reason
                    self._record_cycle(cycle, episode_success=True)
                    break

                # Batch B (P0-3, item 4): a hard-failure cycle may demand a
                # TYPED terminal outcome (config-restore-unverified, or
                # physical-recovery failed/unknown) that must NOT be softened
                # into negotiation. Honor it before any latch/negotiation path.
                override = cycle.get("terminal_override")
                if override is not None:
                    # P0-10: a pre-actuation budget block never wrote, so it is
                    # NOT a rollback; only reflect a real rollback here.
                    result["rolled_back"] = bool(cycle.get("rolled_back", False))
                    episode_outcome, episode_reason = override
                    self._log_gui(f"Terminal override: "
                                  f"{episode_reason.value}; no negotiation")
                    self._record_cycle(cycle, episode_success=False)
                    break

                # Batch B (P0-1): if this cycle's rollback could not be verified
                # it latched TechnicalFailsafe. NO later LLM, negotiation,
                # alternate, cycle, or write may run - fail closed immediately.
                if self._is_latched():
                    self._log_gui("Rollback unverified - latched "
                                  "TechnicalFailsafe; no negotiation/re-entry")
                    result["rolled_back"] = True
                    episode_outcome = TerminalOutcome.TECHNICAL_FAILSAFE
                    episode_reason = TerminalReason.RESTORE_UNVERIFIED
                    self._record_cycle(cycle, episode_success=False)
                    break

                # S5: negotiation over this cycle's alternatives
                self._transition("S5")
                if cycle.get("routed_to") == "negotiation":
                    reason = ("infeasible" if not feasibility.feasible else
                              f"low_confidence ({feasibility.confidence:.2f}"
                              f" < {cycle['theta_star']:.2f})")
                    self._log_gui(f"Negotiation needed: {reason}")
                # P0-10: reserve the negotiation callback's DETERMINISTIC worst
                # case BEFORE invoking it. Insufficient budget stops before the
                # callback (no callback, no write).
                _ledger = getattr(self, "_reserve_ledger", None)
                _nego_rid = None
                if _ledger is not None:
                    if not _ledger.reserve_negotiation(
                            cycle_index=cycle.get("cycle"),
                            cycle_id=cycle.get("cycle_id")):
                        cycle["reserve_ledger"] = _ledger.to_dict()
                        self._log_gui("Insufficient episode budget to reserve "
                                      "the negotiation callback - stopping "
                                      "(P0-10)")
                        episode_outcome = TerminalOutcome.PENDING_NOT_ADMITTED
                        episode_reason = (
                            TerminalReason.INSUFFICIENT_NEGOTIATION_BUDGET)
                        self._record_cycle(cycle, episode_success=False)
                        break
                    _nego_rid = _ledger.last_reservation_id
                # Blocker 5: settle the negotiation reservation with its MEASURED
                # actual cost. On a callback exception/timeout the settlement is
                # recorded UNKNOWN (elapsed/error) rather than omitted.
                _nego_t0 = time.monotonic()
                try:
                    nego = self._negotiate(current_intent,
                                           feasibility.alternatives,
                                           max_rounds=remaining_rounds)
                except Exception as _neg_err:
                    if _ledger is not None and _nego_rid is not None:
                        _ledger.settle(_nego_rid, ReserveLedger.UNKNOWN,
                                       error=str(_neg_err),
                                       elapsed=time.monotonic() - _nego_t0)
                    raise
                finally:
                    # P1-3: retain the MEASURED monotonic negotiation elapsed on
                    # THIS cycle on BOTH success and timeout/exception - never lost,
                    # never a fabricated value (a segment that ran gets its real
                    # elapsed even when it raised).
                    cycle["negotiation_ms"] = (time.monotonic() - _nego_t0) * 1000.0
                if _ledger is not None and _nego_rid is not None:
                    _nego_target = None
                    try:
                        if current_intent.target is not None:
                            _nego_target = float(
                                current_intent.target.target_value)
                    except Exception:
                        _nego_target = None
                    self._settle_negotiation_actual(
                        _ledger, _nego_rid, nego.get("stats") or {},
                        _nego_target)
                cycle["nego_stats"] = nego["stats"]
                # Batch F (#7): propagate the EXACT per-round NEGOTIATION samples
                # (incl. UNKNOWN + error) into the cycle's measurement_samples so
                # they survive into the EvidenceRecord / raw export (the evidence
                # builders read cycle["measurement_samples"]).
                _ns = nego["stats"].get("measurement_samples") or []
                if _ns:
                    cycle.setdefault("measurement_samples", [])
                    cycle["measurement_samples"].extend(copy.deepcopy(_ns))
                    _txn = getattr(self, "_active_txn", None)
                    if _txn is not None:
                        _txn.measurement_samples = tuple(
                            cycle["measurement_samples"])
                remaining_rounds -= int(nego["stats"].get("rounds", 0))
                accepted = nego.get("accepted")
                # A5 (coordinator review): a schema failure during S5 dynamic
                # regeneration happens AFTER the S2 schema verdict was captured -
                # propagate the NEW false verdict/reason onto the cycle (and the
                # result mirror + evidence + runner metric read it).
                if not getattr(self, "_cur_schema_valid", True):
                    cycle["schema_valid"] = False
                    cycle["schema_reject_reason"] = getattr(
                        self, "_cur_schema_reason", None)

                if accepted is not None:
                    # AGREEMENT (an operator/policy accepted a relaxation).
                    # Not execution success - that requires re-execution
                    # and S4 validation of the modified intent.
                    result["agreement"] = {
                        "accepted": True,
                        "alternative_id": accepted.id,
                        "target_value": self._relaxation_target(accepted)[1],
                    }
                    modified = getattr(accepted, "modified_intent", None)
                    materializable = (
                        modified is not None
                        and modified.type in SUPPORTED_INTENT_TYPES
                        and modified.target is not None
                        and math.isfinite(
                            float(modified.target.target_value)))
                    if not materializable:
                        # fail-closed: an agreement that cannot be executed
                        # and re-verified must not resolve the episode
                        nego["stats"]["terminated_by"] = "unmaterializable"
                        self._log_gui("Accepted alternative carries no "
                                      "executable modified intent - reject")
                        episode_outcome = TerminalOutcome.PENDING_NOT_ADMITTED
                        episode_reason = (
                            TerminalReason.UNMATERIALIZABLE_AGREEMENT)
                        self._record_cycle(cycle, episode_success=False)
                        break
                    # No explicit cycle gate: the shared frozen round
                    # budget already bounds re-entries - the accept above
                    # consumed a round, and once remaining_rounds hits 0
                    # the NEXT cycle's negotiation terminates by "n_max"
                    # with zero consultations.
                    # materialize + RE-EXECUTE: the accepted relaxation
                    # becomes the next cycle's intent (S2 re-entry)
                    self._record_cycle(cycle, episode_success=False)
                    cycles += 1
                    current_intent = modified
                    self._log_gui(
                        f"Re-entering coordination (cycle {cycles}) with "
                        f"relaxed target "
                        f"{modified.target.target_value:g}")
                    continue

                # no acceptable alternative: the episode is rejected
                episode_outcome = TerminalOutcome.PENDING_NOT_ADMITTED
                episode_reason = TerminalReason.NO_ACCEPTABLE_ALTERNATIVE
                self._record_cycle(cycle, episode_success=False)
                break

            # top-level mirror of the LAST cycle (GUI / legacy consumers /
            # the runner's legacy single-record fallback)
            last_cycle = result["cycles"][-1]
            for key in ("trial_success", "trial_stats", "nego_stats",
                        "rolled_back", "restore_verified", "hard_failure",
                        "measurement_invalid", "reconnection_time",
                        "clipped", "schema_valid", "schema_reject_reason",
                        # Batch F (#726e): mirror the measurement provenance to
                        # the top-level result so consumers see it there too.
                        "pre_action_baseline", "probe_config",
                        "measurement_samples"):
                if key in last_cycle:
                    result[key] = last_cycle[key]

            # S6: Resolution. Every non-exception path funnels through the
            # single finalizer (P0-4): the terminal outcome is the source of
            # truth and success/resolution are derived from it.  A LATCHED
            # episode stays pinned in S_TECHNICAL_FAILSAFE (P0-7): it must NOT
            # transition to S6 (an illegal failsafe->S6 edge).
            # P0-10: a PRE-ACTUATION budget block terminates the FSM still in S2
            # (no S3 trial, no S5 negotiation ran), and S2->S6 is not a legal
            # edge; that pending-not-admitted terminal finalizes directly (like
            # the latched path) rather than forcing an illegal transition.
            if not self._is_latched() and self.current_state != "S2":
                self._transition("S6")
            if episode_outcome is None:
                # defensive: the loop always sets an outcome before break
                episode_outcome = TerminalOutcome.PENDING_NOT_ADMITTED
                episode_reason = TerminalReason.NEGOTIATION_TERMINATED
            # item 2: build the PROVISIONAL terminal record (evidence FIRST). A
            # commit stays "pending" - the transaction is NOT yet admitted or
            # marked verified. The outer process_intent settles it AFTER this
            # method's S0/history cleanup finally runs. An evidence-build
            # failure here already downgrades to failsafe (fail-closed), and the
            # outer settlement then rolls the applied trial back.
            return self._finalize_episode(
                result, episode_outcome, episode_reason,
                pending_intent=pending_ref, committed_revision=committed_ref)

        except (IllegalTransition, UnknownSystemEvent) as e:
            # P0-7 / coordinator review D1: an illegal FSM transition or an
            # UNKNOWN SYSTEM EVENT (e.g. an unknown policy/system response) is a
            # broken invariant -> TechnicalFailsafe.  It must ENTER + PIN the
            # typed failsafe state, not emit a TechnicalFailsafe terminal then
            # silently clean up to S0: LATCH here so the FSM state, the terminal
            # claim, and the executor latch AGREE, and the NEXT process_intent is
            # blocked until explicit operator clearance.  (An illegal transition
            # already latched inside _fail_fsm; an UnknownSystemEvent from a
            # policy callback did not, so latch it now.)
            logger.error(f"FSM fail-closed during episode: {e}")
            if not self._is_latched():
                self._latch_failsafe({"restore_error": str(e),
                                      "fsm_reason": str(e)})
            result["error"] = str(e)
            result["rolled_back"] = bool(getattr(
                getattr(self, "_active_txn", None), "rollback_done", False))
            reason = (TerminalReason.ILLEGAL_TRANSITION
                      if isinstance(e, IllegalTransition)
                      else TerminalReason.UNKNOWN_SYSTEM_EVENT)
            return self._finalize_episode(
                result, TerminalOutcome.TECHNICAL_FAILSAFE,
                reason, pending_intent=pending_ref)

        except OperationTimeout as e:
            # P0-7: a bounded external call (or the absolute deadline) timed out.
            # The cycle's own finally has already rolled back any applied write
            # (exactly once) and latched iff the restore could not be verified.
            #   * post-actuation timeout, restore UNVERIFIED -> TechnicalFailsafe
            #   * pre-actuation OR post-actuation-but-restore-VERIFIED
            #     -> PendingNotAdmitted (nothing unverified was committed)
            logger.warning(f"bounded-call/deadline timeout: {e}")
            result["error"] = f"deadline/timeout: {e}"
            tx = getattr(self, "_active_txn", None)
            if tx is not None and getattr(tx, "rollback_done", False):
                result["rolled_back"] = True
            if self._is_latched():
                result["rolled_back"] = True
                return self._finalize_episode(
                    result, TerminalOutcome.TECHNICAL_FAILSAFE,
                    TerminalReason.TIMEOUT_UNRECOVERED,
                    pending_intent=pending_ref)
            return self._finalize_episode(
                result, TerminalOutcome.PENDING_NOT_ADMITTED,
                TerminalReason.DEADLINE_EXHAUSTED, pending_intent=pending_ref)

        except Exception as e:
            logger.error(f"Error processing intent: {e}")
            result["error"] = str(e)
            # An illegal transition, unknown system event, or otherwise
            # uncaught fault means the system cannot confirm a safe state:
            # fail-closed to TechnicalFailsafe (P0-4 decision table). Batch B:
            # a post-write exception rolls back in the cycle's finally; if that
            # rollback could NOT be verified the coordinator is now latched, so
            # name the reason RESTORE_UNVERIFIED (else the generic internal
            # error). Both are TechnicalFailsafe.
            reason = (TerminalReason.RESTORE_UNVERIFIED if self._is_latched()
                      else TerminalReason.INTERNAL_ERROR)
            if self._is_latched():
                result["rolled_back"] = True
            return self._finalize_episode(
                result, TerminalOutcome.TECHNICAL_FAILSAFE,
                reason, pending_intent=pending_ref)

        finally:
            # Final cleanup MUST be exception-safe (P0-4 exhaustiveness): a
            # failing S0 state callback or history append must not let the
            # episode escape without a terminal outcome. On cleanup failure we
            # downgrade the already-computed result to TechnicalFailsafe/
            # InternalError (fail-closed) rather than propagate.
            #
            # Batch B (P0-1 step 8): a LATCHED episode does NOT return to S0 -
            # the FSM stays pinned in S_TECHNICAL_FAILSAFE. Only history append
            # runs (audit), so a latched failsafe never silently re-enters S0.
            # P1-3 (blocker 2): PROVISIONAL body-only total on a MONOTONIC clock
            # (not wall-clock time.time). The AUTHORITATIVE episode total, covering
            # settlement/finalization, is stamped once at the public single-flight
            # boundary (_run_episode_single_flight) and overrides this.
            result["latency_ms"] = (time.monotonic() - episode_monotonic_s) * 1000.0
            try:
                if not self._is_latched():
                    self._transition("S0")
                # Batch E (two-phase history, coordinator review): PHASE 1 -
                # VALIDATE the finalizable history record BEFORE admission, so a
                # history-construction failure DOWNGRADES here (pre-admission,
                # Batch A). The record is NOT appended yet and the pin is NOT
                # cleared - publication + pin-clear happen in the OUTERMOST
                # single-flight finally AFTER settlement decides the final
                # outcome (so a settlement downgrade can never leave a stale
                # commit record, and final attribution still sees the pin).
                self._prepare_history(result)
            except Exception as cleanup_err:
                logger.error(f"episode cleanup failed: {cleanup_err}")
                prior = result.get("error")
                result["error"] = (
                    f"{prior}; cleanup failed: {cleanup_err}" if prior
                    else f"cleanup failed: {cleanup_err}")
                try:
                    self._finalize_episode(
                        result, TerminalOutcome.TECHNICAL_FAILSAFE,
                        TerminalReason.INTERNAL_ERROR,
                        pending_intent=pending_ref)
                except Exception as stamp_err:
                    # last resort: stamp minimal failsafe fields directly
                    logger.error(f"failsafe stamp failed: {stamp_err}")
                    result["success"] = False
                    result["resolution"] = "reject"
                    result["terminal_outcome"] = (
                        TerminalOutcome.TECHNICAL_FAILSAFE.value)
                    result["terminal_reason"] = (
                        TerminalReason.INTERNAL_ERROR.value)

        # Unreachable: the try body and the except handler both return via
        # the finalizer above. Kept as a defensive fail-closed net so no
        # path can ever escape without a terminal outcome.
        return self._finalize_episode(
            result, TerminalOutcome.TECHNICAL_FAILSAFE,
            TerminalReason.INTERNAL_ERROR, pending_intent=pending_ref)

    def _coordination_context(self, current_intent: Intent) -> List[Intent]:
        """Monitored intents EXCLUDING the current intent's own lineage.

        The registered twin (same type + scope - the intent a successful
        commit would REPLACE) must not appear in the coordination context:
        a RELAXED re-execution would otherwise be screened, proposed
        against, and S4-validated against the very target it is relaxing
        (C1 x C7), making relaxation futile. Every consumer of the
        context - the cycle's S2/S4 AND the S5 alternative regeneration -
        goes through this one helper.
        """
        twin_key = IntentManager._scope_key(current_intent)
        return [i for i in self.intent_manager.get_monitored()
                if not (i.type == current_intent.type
                        and IntentManager._scope_key(i) == twin_key)]

    def _stamp_cycle_provenance(self, cycle: Dict, current_intent) -> None:
        """Batch G (P1-6): record CYCLE-LOCAL provenance at the cycle boundary,
        AFTER this cycle's proposal-boundary pin. Every re-entry re-stamps, so a
        REVISED cycle carries ITS OWN pinned proposer/model and ITS OWN (revised)
        intent snapshot - never the original cycle's facts. Records:
          * cycle_monotonic_s      - this cycle's monotonic timestamp;
          * proposer_id/model_version - the PINNED proposer context read AFTER the
            pin, so a mid-episode model hot-swap is reflected per cycle;
          * current_intent (+ id/type/full target/full scope) - an IMMUTABLE deep
            snapshot of THIS cycle's intent, decoupled from the live object so a
            later in-place mutation cannot rewrite an earlier cycle's facts;
          * intent_content_hash    - the content hash of that exact intent."""
        cycle["cycle_monotonic_s"] = time.monotonic()
        # best-effort provenance (consistent with _authorize_action): a proposer/
        # model lookup failure must NOT fail-closed the write path pre-trial -
        # proposer_id/model_version are provenance metadata, not commit-gate
        # inputs. Degrade to the honest "unknown" (None=unknown), never fabricate;
        # any genuinely required provenance is still re-checked (and fails closed)
        # at evidence finalization.
        try:
            proposer_id, model_version = self._proposer_context()
        except Exception:
            proposer_id = model_version = "unknown"
        cycle["proposer_id"] = proposer_id or "unknown"
        cycle["model_version"] = model_version or "unknown"
        if current_intent is not None:
            snap = copy.deepcopy(current_intent.to_dict())   # frozen snapshot
            cycle["current_intent"] = snap
            cycle["current_intent_id"] = snap.get("id")
            cycle["current_intent_type"] = snap.get("type")
            cycle["current_intent_target"] = snap.get("target")
            cycle["current_intent_scope"] = snap.get("scope")
            cycle["intent_content_hash"] = self._intent_content_hash(
                current_intent)
            # the pending-intent digest of THIS cycle's FROZEN snapshot (pi-...),
            # computed at the boundary. The tx and the terminal EvidenceRecord bind
            # from this per-cycle hash (revised cycles differ); the result-level
            # _pending_intent_hash is only a NO-CYCLE fallback.
            cycle["pending_intent_hash"] = self._hash_intent_basis(snap)
        else:
            cycle["current_intent"] = None
            cycle["current_intent_id"] = None
            cycle["current_intent_type"] = None
            cycle["current_intent_target"] = None
            cycle["current_intent_scope"] = None
            cycle["intent_content_hash"] = None
            cycle["pending_intent_hash"] = None

    def _stamp_proposal_stage(self, cycle: Dict) -> None:
        """Batch G (P1-6/P1-3): copy THIS cycle's proposal-stage stamps into
        `cycle`. Exception-safe: called in a FINALLY around the feasibility model
        call so a model TIMEOUT/exception still records the HONEST fields - the
        REAL prompt hash (stamped pre-call, retained on timeout), the measured
        inference latency, proposal_generated=False, proposal_id=None,
        schema_valid=None. proposal_id is minted ONLY when a proposal was
        actually generated (never a fabricated stage id)."""
        proposal_generated = bool(getattr(self, "_cur_proposal_generated", False))
        cycle["proposal_generated"] = proposal_generated
        if proposal_generated:
            if not cycle.get("proposal_id"):
                cycle["proposal_id"] = new_id("prop")
                # Batch E (P0-16): refine THIS cycle's pin with the real proposal
                # id (same backend handle) and stamp the applied switch-audit.
                self._bind_proposal_id(cycle["proposal_id"])
        else:
            cycle["proposal_id"] = None
        # honest schema verdict: the REAL bool when a proposal was validated, else
        # None (no proposal generated). NEVER bool()-coerced into a fake False.
        cycle["schema_valid"] = getattr(self, "_cur_schema_valid", None)
        cycle["schema_reject_reason"] = getattr(self, "_cur_schema_reason", None)
        # the REAL prompt hash of this cycle's feasibility prompt (retained even
        # on a model timeout/exception).
        cycle["prompt_hash"] = getattr(self, "_cur_prompt_hash", None)
        # the REAL measured proposal-inference latency (ms); None only if the
        # bounded call never ran (e.g. prompt-hash construction raised).
        cycle["inference_ms"] = getattr(self, "_cur_inference_ms", None)

    def _coordination_cycle(self, cycle: Dict, current_intent: Intent,
                            active_intents: List[Intent]):
        """One autonomous coordination cycle: S2 -> theta* routing ->
        optional S3 trial + S4 validation (C1).

        Fills `cycle` IN PLACE (confidence, theta_star, routed_to, trial
        fields, rollback/hard-failure flags) so the caller sees partial
        state even if an exception propagates. Returns (feasibility,
        committed): committed=True means this cycle's configuration passed
        S4 and was committed - the episode resolves. S5 negotiation and
        cycle re-entry are the caller's loop.
        """
        # per-cycle strict-schema verdict reset (P0-5 / P1-6): None until a
        # proposal is actually validated - set True on a validation PASS, False on
        # a schema REJECT, and left None when NO proposal was generated (a model
        # failure). NEVER a default True that fabricates a verdict for a cycle that
        # produced no proposal.
        self._cur_schema_valid = None
        self._cur_schema_reason = None
        # explicit source fact: whether a proposal was actually GENERATED this
        # cycle (the model returned output). Gates proposal_id minting + the raw
        # exporter's proposal-stage decision (not inferred from the prompt hash).
        self._cur_proposal_generated = False
        # Batch G (P1-6): the REAL prompt hash of THIS cycle's feasibility prompt,
        # stamped by _analyze_feasibility at generation. None until (and unless) a
        # proposal prompt is actually generated this cycle.
        self._cur_prompt_hash = None
        # Batch G (P1-3): the REAL measured proposal-inference latency (ms) of this
        # cycle's model call. None until the call runs (never a total-episode
        # latency split across cycles).
        self._cur_inference_ms = None
        # schema-validation latency + admission/calibration/routing latency; the
        # cycle's schema_admission_ms is their sum (None if neither measured).
        self._cur_schema_ms = None
        self._cur_admission_ms = None
        # P0-7: an already-expired absolute deadline ends the episode
        # PendingNotAdmitted BEFORE any model call or write (pre-actuation).
        if self._deadline_expired():
            raise OperationTimeout("episode deadline exceeded before feasibility")

        # S2: LLM feasibility analysis
        self._transition("S2")
        network_state = self._get_network_state()
        try:
            feasibility = self._analyze_feasibility(current_intent,
                                                    active_intents, network_state)
        finally:
            # Batch G (P1-6/P1-3): copy the proposal-stage provenance into `cycle`
            # EVEN on a model TIMEOUT/exception, so a fail-closed terminal still
            # carries the honest fields (real prompt hash retained, measured
            # inference latency, proposal_generated=False, proposal_id=None,
            # schema_valid=None). Mints proposal_id only when actually generated.
            self._stamp_proposal_stage(cycle)
        cycle["confidence"] = feasibility.confidence
        cycle["feasible"] = feasibility.feasible
        cycle["target_value"] = (float(current_intent.target.target_value)
                                 if current_intent.target is not None
                                 else None)
        # Batch G (P1-3): start the ADMISSION timer - the calibration / theta
        # routing / budget-reservation / admission decision below. It is summed
        # with the schema-validation latency into schema_admission_ms at the
        # routing decision (which is measured on EVERY branch).
        self._cur_adm_t0 = time.monotonic()

        # Update GUI with LLM analysis (isolated pure-display side-effect)
        if self.gui:
            # P1-3 (blocker 2/7): send the ACTUAL measured proposal-inference
            # latency (monotonic), or None when it was not measured - NEVER a
            # hard-coded 0. The dashboard renders None as n/a.
            self._safe_gui("update_llm_analysis", {
                "model": self.llm_manager.active_backend_name() or "unknown",
                "latency_ms": getattr(self, "_cur_inference_ms", None),
                "feasible": feasibility.feasible,
                "confidence": feasibility.confidence,
                "reasoning": feasibility.reasoning,
                "alternatives": [a.to_dict() for a in feasibility.alternatives]
            })

        # Review: use the FROZEN (model, regime) pair for the whole cycle - the
        # calibrator partition was selected at episode start, so theta*/N_max/
        # calibration/proposal-counting/tx all share ONE pair (no mid-cycle
        # backend re-read).
        _model, _regime = self._frozen_context()
        # Decision based on confidence threshold.
        # P0-17: route on the CALIBRATED PROBABILITY (a deterministic reliability
        # mapping of the raw proposer score, partitioned by model+regime), NOT
        # the raw score. A non-finite raw score fails closed (calibrated 0.0 ->
        # never a trial). At cold start the mapping is identity.
        theta_star = self.calibrator.get_theta_star()
        raw_conf = feasibility.confidence
        # Review: a FAILED partition selection fails CLOSED before any model-
        # dependent routing/actuation - force calibrated=0 (never a trial).
        if not getattr(self, "_calibration_context_ok", True):
            calibrated, calib_reason = 0.0, "fail_closed_context_selection"
        else:
            calibrated, calib_reason = self._calibrated_probability(raw_conf)
        # Blocker 8: capture the AUTHORITATIVE calibration provenance into
        # coordinator-owned, DEEP-COPIED per-cycle state BEFORE any external
        # callback, so a later forged result/cycle mutation cannot survive.
        self._bind_cycle_calibration(
            cycle, raw_conf, calibrated, theta_star, calib_reason)
        if self.gui:
            self._safe_gui("update_calibration", self.calibrator.get_stats())

        if not (feasibility.feasible and calibrated >= theta_star):
            self._finalize_schema_admission(cycle)
            cycle["routed_to"] = "negotiation"
            return feasibility, False

        # P0-10: reserve the initial/revised trial's DETERMINISTIC worst case
        # BEFORE any actuation. Insufficient pre-actuation budget ends the
        # episode PendingNotAdmitted with a specific typed reason and NO write.
        ledger = getattr(self, "_reserve_ledger", None)
        if ledger is not None:
            if not ledger.reserve_trial(
                    ("revised_trial" if cycle.get("cycle", 0)
                     else "initial_trial"),
                    cycle_index=cycle.get("cycle"),
                    cycle_id=cycle.get("cycle_id")):
                self._finalize_schema_admission(cycle)
                cycle["routed_to"] = "budget_blocked"
                cycle["reserve_ledger"] = ledger.to_dict()
                cycle["terminal_override"] = (
                    TerminalOutcome.PENDING_NOT_ADMITTED,
                    TerminalReason.INSUFFICIENT_TRIAL_BUDGET)
                self._log_gui("Insufficient episode budget to reserve the trial "
                              "worst case - not actuated (P0-10)")
                return feasibility, False
            # link this cycle's trial reservation to its later settlement
            cycle["trial_reservation_id"] = ledger.last_reservation_id

        self._finalize_schema_admission(cycle)
        cycle["routed_to"] = "trial"
        # S3: Trial execution
        self._transition("S3")
        self._log_gui(f"Trial execution (calib={calibrated:.2f} "
                      f">= θ*={theta_star:.2f}, raw={raw_conf:.2f})")

        # S3-entry: the attached-UE set (for S4 hard-failure detection) comes
        # from collect_all - used ONLY for ATTACHMENT, never as a throughput
        # source. Batch F (P0-13): the cost baseline is a REAL fresh PRE_ACTION
        # throughput probe taken immediately before authorization/write under the
        # shared scheduler; a MISSING/failed probe stays UNKNOWN (value=None) and
        # is EXCLUDED from calibration/cost - never a cached / coerced-0 value.
        try:
            pre = self.ue_collector.collect_all()
            self._pre_trial_attached = {
                u for u, m in pre.items()
                if getattr(m, "attached", False)}
        except Exception as e:
            logger.debug(f"S3-entry attachment probe failed: {e}")
            self._pre_trial_attached = set()
        baseline = self._measure_throughput(
            MeasurementPurpose.PRE_ACTION, label="s3_baseline",
            ue_scope=sorted(self._pre_trial_attached))
        self._pending_baseline_sample = baseline
        tp_before = baseline.value          # None when UNKNOWN (never a cache)

        # === Post-actuation safety transaction (P0-2, Gate-review 1/2/3) ===
        # A SHARED, mutable transaction context is the single source of truth
        # for "did a REAL device write happen" (tx.actual_real_write_performed),
        # updated by _execute_trial immediately after the first successful
        # executor write. The finally uses THAT flag (not trial_result.success),
        # so a mid-write exception that never returns still rolls back exactly
        # once. commit_verified is NOT set here: admission + commit settlement is
        # deferred to the caller's finalization (item 2), so an evidence/finalizer
        # failure after a write rolls back and admits nothing.
        tx = ActuationTransaction(simulation=bool(getattr(
            getattr(self, "ue_collector", None), "simulation_mode", False)))
        # P0-8 (review bug 2): capture the re-entry trigger context onto the
        # transaction as a DEEP COPY, BEFORE any authorization/write. From here
        # on the authoritative commit reflection trusts ONLY tx.trigger_context,
        # so a forged result/cycle trigger field can never survive settlement.
        tx.trigger_context = copy.deepcopy(
            getattr(self, "_active_trigger_context", {}) or {})
        # Blocker 8: bind the trusted calibration provenance onto the tx (deep
        # copy) BEFORE any authorization/write, so the authoritative commit
        # reflection uses ONLY tx.calibration.
        tx.calibration = copy.deepcopy(
            getattr(self, "_active_calibration", {}) or {})
        self._active_txn = tx
        # Batch F (P0-13): bind the trusted PRE_ACTION baseline sample onto the
        # tx BEFORE any callback/write (a missing baseline is UNKNOWN, excluded
        # from calibration/cost - never 0).
        tx.pre_action_baseline = (self._pending_baseline_sample.to_dict()
                                  if getattr(self, "_pending_baseline_sample",
                                             None) is not None else None)
        # Batch F (#7): bind the exact probe config on the tx BEFORE any callback,
        # so the committed evidence's probe_config is the trusted bound value -
        # not a post-S4 mutable self._probe_cfg() that a callback could swap.
        tx.probe_config = copy.deepcopy(self._probe_cfg().to_dict())
        tx.measurement_samples = ()      # populated by _capture_commit_evidence
        # Batch F (#726e): the public cycle carries INDEPENDENT deep copies of
        # the trusted provenance (NOT aliases of tx), so a callback mutating the
        # cycle can never reach through into the trusted tx-bound values.
        cycle["pre_action_baseline"] = copy.deepcopy(tx.pre_action_baseline)
        cycle["probe_config"] = copy.deepcopy(tx.probe_config)
        self.safety_state = SafetyState.ACTUATING
        # P0-6: canonicalize the authorized action and bind a stable hash +
        # authorization artifact (intent-set version, identifiers, expiry)
        # BEFORE the write.  The executor receives ONLY this canonical action.
        # A strict action-schema rejection (unknown key / non-finite value)
        # routes to negotiation without any write.
        try:
            self._authorize_action(feasibility, cycle, tx, current_intent)
        except SchemaError as e:
            self._note_schema_invalid(f"action: {e}")
            cycle["schema_valid"] = False
            cycle["schema_reject_reason"] = str(e)
            cycle["trial_success"] = False
            # Batch F (P0-13): a no-trial reject keeps the baseline HONEST - a
            # missing baseline stays None (UNKNOWN), never coerced to 0.0.
            cycle["trial_stats"] = {"tput_before": tp_before,
                                    "tput_min": tp_before,
                                    "tput_after": tp_before, "tau": 0.0,
                                    "baseline_unknown": tp_before is None}
            if not self._is_latched():
                self.safety_state = SafetyState.READY
            self._log_gui(f"Action proposal rejected (schema): {e}")
            return feasibility, False
        trial_result = None
        try:
            # Batch G (P1-3): measure the executor-write segment (snapshot +
            # applied writes) in a finally so a raising apply still records it.
            _ew_t0 = time.monotonic()
            try:
                trial_result = self._execute_trial(feasibility)
            finally:
                cycle["executor_write_ms"] = (time.monotonic() - _ew_t0) * 1000.0
            cycle["clipped"] = trial_result.get("clipped", [])
            # (identifier honesty, P0-6) the actuation trial id is minted by
            # _execute_trial at the pre-write point (after a verified snapshot,
            # before the first write) and the authorization rebound to carry it.
            # Copy both onto the cycle so the authorization and the evidence bind
            # the SAME real write attempt.  A snapshot/verify failure before any
            # write leaves tx.actuation_trial_id None -> no id, no write.
            if getattr(tx, "actuation_trial_id", None):
                cycle["actuation_trial_id"] = tx.actuation_trial_id
                if tx.authorization is not None:
                    cycle["authorization"] = tx.authorization.to_dict()

            if not trial_result["success"]:
                # Apply failed / partial apply. A PARTIAL write is rolled back
                # inside _execute_trial through the SAME rollback transaction
                # (P0-1 step 7); tx.rollback_done is set there. Surface the
                # verdict onto the cycle. No window ran: zero-duration stats.
                cycle["trial_success"] = False
                # Batch F: no window ran - keep the baseline HONEST (None stays
                # None/UNKNOWN, never coerced to 0.0).
                cycle["trial_stats"] = {
                    "tput_before": tp_before,
                    "tput_min": tp_before,
                    "tput_after": tp_before,
                    "tau": 0.0,
                    "baseline_unknown": tp_before is None,
                }
                self._absorb_trial_rollback(cycle, trial_result)
                self._stash_cycle_audit(cycle, tx)
                return feasibility, False

            # P0-7: a post-actuation deadline overrun must NOT leave the write
            # applied.  Raising here enters the cycle's finally, which rolls
            # back exactly once (then the outcome follows the verified restore).
            if self._deadline_expired():
                raise OperationTimeout(
                    "episode deadline exceeded after actuation")
            # S4 transition is now INSIDE the transaction (P0-2): an S4 state /
            # GUI notification exception after the write rolls back in finally.
            self._transition("S4")
            # P0-7 (coordinator review): the absolute deadline is enforced
            # THROUGH validation - the S4 window is a bounded external call, so
            # a NON-RETURNING post-write validation/dependency still returns
            # within the episode budget (OperationTimeout -> finally rolls back
            # exactly once), not only a manually-expired deadline.
            # Batch G (P1-3): measure the S4 validation-window segment in a
            # finally so a timeout/exception still records the elapsed latency.
            _val_t0 = time.monotonic()
            try:
                validation = self._bounded_call(
                    lambda: self._validate_trial(current_intent, active_intents),
                    float(getattr(self, "episode_budget_s", 120.0)),
                    label="S4 validation window")
            finally:
                cycle["validation_ms"] = (time.monotonic() - _val_t0) * 1000.0
            # Batch F (review #1): copy the EXACT validation measurement_samples
            # into the cycle IMMEDIATELY for EVERY S4 outcome (success, failed,
            # UNKNOWN, rollback) so the cycle / raw export never loses the
            # authoritative window samples. A successful commit's
            # _capture_commit_evidence then replaces this with the trusted
            # tx-deep-copy, and negotiation appends to it.
            cycle["measurement_samples"] = [
                copy.deepcopy(m)
                for m in (validation.get("measurement_samples") or [])]
            # and re-check the deadline BEFORE pending_commit/settlement.
            if self._deadline_expired():
                raise OperationTimeout(
                    "episode deadline exceeded during/after validation")
            all_ok = bool(validation["all_satisfied"])
            tx.joint_satisfied = all_ok   # derived/cache boolean only (P0-6)
            # Bind the EXACT system-owned required intent-ID set for this cycle
            # (item 5): the commit gate re-checks the TYPED per-intent verdicts
            # over THIS set, independent of what the validation self-reports, so
            # a corrupt all_satisfied=True cannot omit an intent or slip a
            # 'violated' verdict past the gate.
            tx.required_intent_ids = tuple(
                getattr(i, "id", None)
                for i in list(active_intents) + [current_intent])
            cycle["trial_success"] = bool(trial_result["success"] and all_ok)
            # Batch F (#d8b9): a TIMESTAMPED [(sample_time, value)] trajectory
            # built from the AUTHORITATIVE OK window samples, so _record_cycle /
            # the estimator use honest numerical integration (trajectory_used)
            # rather than the legacy min*tau fallback. UNKNOWN points stay out of
            # the integration (they remain explicit samples elsewhere).
            _traj = [(s["sample_time"], s["value"])
                     for s in (validation.get("measurement_samples") or [])
                     if s.get("status") == "ok" and s.get("value") is not None]
            cycle["trial_stats"] = {
                "tput_before": tp_before,   # None = unmeasured
                "tput_min": validation.get("tput_min", 0.0),
                "tput_after": validation.get("tput_after", 0.0),
                "tau": self.tau_trial_s,
                "trajectory": _traj,
                # Batch F (review #2): the EXPLICIT window-UNKNOWN flag (incl. a
                # transient OK,UNKNOWN,OK) - carried so the calibrator/runner
                # exclude this trial from cost even when tput_min/after are real.
                "throughput_unknown": bool(
                    validation.get("throughput_unknown")),
            }
            cycle["hard_failure"] = bool(validation.get("hard_failure", False))
            cycle["reconnection_time"] = float(
                validation.get("reconnection_time", 0.0))
            cycle["measurement_invalid"] = bool(
                validation.get("measurement_invalid", False))

            if validation.get("recovery_pending"):
                # P0-3 (item 4): a hard failure stopped the window early. Roll
                # back FIRST (mark rollback_done so the finally does not
                # double-roll). ONLY if the config restore VERIFIED do we then
                # sample physical recovery UNDER the restored config; if the
                # config was NOT restored we do NOT sample/wait - record
                # physical UNKNOWN and terminate failsafe.
                tx.rollback_done = True
                rb = self._run_rollback_transaction(
                    tx.snapshot, cycle, first_write_time=tx.first_write_time,
                    actual_real_write=tx.actual_real_write_performed,
                    audit_extra=self._audit_from_tx(tx, "hard_failure"))
                self._stash_cycle_audit(cycle, tx)
                if rb.config_restore_verdict is not ConfigRestoreVerdict.VERIFIED:
                    # config NOT restored -> do not observe recovery (item 4).
                    cycle["physical_recovery_verdict"] = (
                        PhysicalRecoveryVerdict.UNKNOWN.value)
                    cycle["terminal_override"] = (
                        TerminalOutcome.TECHNICAL_FAILSAFE,
                        TerminalReason.RESTORE_UNVERIFIED)
                    return feasibility, False
                # config VERIFIED -> observe physical recovery under restore.
                # Batch G (P1-3): measure the recovery-observation segment in a
                # finally so a timeout/exception still records the latency.
                _rec_t0 = time.monotonic()
                try:
                    recovery = self._check_physical_recovery(
                        validation.get("failed_ues") or [],
                        first_write_time=tx.first_write_time,
                        rollback_complete_time=rb.restore_complete_time)
                finally:
                    cycle["recovery_ms"] = (time.monotonic() - _rec_t0) * 1000.0
                v = recovery["physical_recovery_verdict"]
                cycle["physical_recovery_verdict"] = v
                cycle["reconnection_time"] = float(
                    recovery.get("reconnection_time", 0.0))
                merged = dict(cycle.get("rollback_result") or {})
                merged.update({
                    "physical_recovery_verdict": v,
                    "recovery_start_time": recovery.get("recovery_start_time"),
                    "recovery_complete_time":
                        recovery.get("recovery_complete_time"),
                })
                cycle["rollback_result"] = merged
                if v == PhysicalRecoveryVerdict.RECOVERED.value:
                    # service is back under the restored config -> proceed per
                    # the existing safe policy (caller negotiates / rejects).
                    return feasibility, False
                # NOT_RECOVERED or UNKNOWN: do NOT negotiate/revise/write again
                # (item 4). Terminate failsafe with a TYPED, distinct reason.
                cycle["terminal_override"] = (
                    TerminalOutcome.TECHNICAL_FAILSAFE,
                    TerminalReason.PHYSICAL_RECOVERY_FAILED
                    if v == PhysicalRecoveryVerdict.NOT_RECOVERED.value
                    else TerminalReason.PHYSICAL_RECOVERY_UNKNOWN)
                return feasibility, False

            if all_ok:
                # VALIDATED. DEFER admission + commit settlement to finalization
                # (item 2): leave the trial applied, admit NOTHING yet. The
                # caller settles atomically with mandatory evidence/TerminalRecord
                # finalization; an evidence failure there rolls this back and
                # admits nothing. pending_commit tells the finally NOT to roll
                # back a validated-but-unsettled trial.
                tx.pending_commit = True
                # P0-6: capture the post-action observations + a FRESH final
                # device read-back of the canonical action, so the atomic
                # commit gate at settlement can re-verify freshness and
                # readback == canonical applied action.
                self._capture_commit_evidence(tx, cycle, validation)
                # item 2 (final): DEFER the ACTIVE promotion to settlement. The
                # intent must NOT be marked ACTIVE (i.e. monitored) until the
                # commit is actually admitted, so an aborted admission leaves
                # its prior status intact.
                self._stash_cycle_audit(cycle, tx)
                self._log_gui("Trial validated - pending commit "
                              "(finalization deferred)")
                return feasibility, True

            # Not satisfied: NO commit. The finally rolls back exactly once;
            # the caller negotiates (never re-trial the same action).
            self._log_gui("Trial failed - rollback")
            return feasibility, False

        except Exception:
            # A post-write exception (S4 state/GUI callback, or validation
            # consumption): the finally rolls back exactly once; record honest
            # fail-closed fields, then fail closed (re-raise). The window is
            # untrusted -> measurement-invalid (excluded from cost samples).
            cycle["trial_success"] = False
            cycle["measurement_invalid"] = True
            cycle["trial_stats"] = {
                "tput_before": tp_before,
                "tput_min": 0.0, "tput_after": 0.0, "tau": 0.0}
            cycle.pop("hard_failure", None)
            cycle.pop("reconnection_time", None)
            raise
        finally:
            # Exactly-once rollback for every non-commit / non-pending exit
            # AFTER a real write. tx.actual_real_write_performed is authoritative
            # (set right after the first successful executor write), so a
            # mid-write exception that never returned still rolls back exactly
            # once. A partial-apply / hard-failure path already set
            # tx.rollback_done, so it is not double-rolled. A pending_commit
            # (validated, awaiting settlement) is NOT rolled back here.
            wrote = tx.actual_real_write_performed or bool(
                trial_result and trial_result.get("success"))
            if wrote and not tx.commit_verified and not tx.pending_commit \
                    and not tx.rollback_done:
                tx.rollback_done = True
                snap = tx.snapshot or (trial_result or {}).get("snapshot") or {}
                self._run_rollback_transaction(
                    snap, cycle, first_write_time=tx.first_write_time,
                    actual_real_write=tx.actual_real_write_performed,
                    audit_extra=self._audit_from_tx(tx, cycle.get("failed_axis")))
                self._stash_cycle_audit(cycle, tx)
            # pending_commit keeps the transaction OPEN (ACTUATING) for the
            # caller's settlement; otherwise LATCHED stays latched, else READY.
            if not tx.pending_commit and not self._is_latched():
                self.safety_state = SafetyState.READY

    def _absorb_trial_rollback(self, cycle: Dict, trial_result: Dict) -> None:
        """Copy a partial-apply rollback verdict (recorded inside
        _execute_trial via the shared rollback transaction) onto the cycle, so
        the cycle record and the caller's latch check see it (P0-1 step 7)."""
        if trial_result.get("rollback_result") is None:
            return
        cycle["rolled_back"] = True
        cycle["rollback_result"] = trial_result["rollback_result"]
        rv = trial_result.get("restore_verified")
        if rv is not None:
            cycle["restore_verified"] = bool(rv)
        verdict = trial_result.get("config_restore_verdict")
        if verdict is not None:
            cycle["config_restore_verdict"] = verdict

    # LLM parse output -> (IntentType, required constraint direction). Only
    # types _evaluate_intent supports are accepted, and only with the
    # constraint direction its evaluator actually implements (throughput is
    # a floor, latency/fairness/power are ceilings) - a mismatched direction
    # would parse fine but be evaluated backwards, so it is rejected
    # (fail-closed), never coerced.
    _PARSE_TYPE_MAP = {
        "throughput_goal": (IntentType.THROUGHPUT_GOAL, "min"),
        "latency_goal": (IntentType.LATENCY_GOAL, "max"),
        "power_constraint": (IntentType.POWER_CONSTRAINT, "max"),
        "throughput_fairness": (IntentType.THROUGHPUT_FAIRNESS, "max"),
    }

    def _parse_intent(self, intent_text: str) -> Optional[Dict]:
        """Parse natural language to Intent object.

        Returns None when the LLM call itself failed; raises IntentParseError
        when the LLM answered but the content is unsupported or malformed
        (unknown type/constraint, missing or non-finite value). Fail-closed:
        no fallback coercion, because a coerced intent can reach the RAN.
        """
        prompt = f"""Parse this network intent into structured form:
"{intent_text}"

Output JSON:
{{
    "type": "throughput_goal|latency_goal|power_constraint|throughput_fairness",
    "constraint": "min|max",
    "value": <number>,
    "unit": "Mbps|ms|dB",
    "scope": {{"ue_ids": [], "bs_ids": []}},
    "description": "brief description"
}}"""

        # P0-7: the model call is bounded by the remaining episode time, so a
        # non-returning backend cannot hang the parse (pre-actuation).
        response = self._bounded_call(
            lambda: self._pinned_generate(prompt),
            float(getattr(self, "model_call_timeout_s", 30.0)),
            label="intent-parse model call")
        if not response.success:
            return None

        # P0-5: strictly parse the RAW model content so a duplicate JSON key or
        # a non-standard NaN/Infinity constant is rejected (the lenient
        # parsed_json would have hidden both).  Fall back to parsed_json only
        # when a test double supplies no content.
        content = getattr(response, "content", "") or ""
        try:
            if content.strip():
                data = _schema.strict_extract_object(content)
            else:
                data = response.parsed_json
            if not data:
                # an empty / missing object is a PARSE failure (no usable
                # content), not a schema rejection -> None (PARSE_FAILED)
                return None
            # full required key set + exact JSON types (value a JSON number;
            # scope an object of ue_ids/bs_ids string arrays; no unknown/nested
            # type-invalid field) (P0-5).
            _schema.validate_intent_parse(data)
        except SchemaError as e:
            raise IntentParseError(f"schema: {e}")
        type_str = data.get("type")
        entry = self._PARSE_TYPE_MAP.get(type_str)
        if entry is None:
            raise IntentParseError(f"unsupported intent type {type_str!r}")
        intent_type, required_constraint = entry

        constraint_str = data.get("constraint")
        if constraint_str not in ("min", "max"):
            raise IntentParseError(f"unknown constraint {constraint_str!r} "
                                   f"(expected 'min' or 'max')")
        if constraint_str != required_constraint:
            raise IntentParseError(
                f"constraint {constraint_str!r} unsupported for {type_str!r} "
                f"(evaluator implements {required_constraint!r} only)")

        # value was already fully validated (present, finite JSON number) inside
        # validate_intent_parse (P0-5); just convert it here.
        value = float(data["value"])

        from decision.intent_model import IntentTarget, IntentScope, ConstraintType

        intent = Intent(
            type=intent_type,
            target=IntentTarget(
                kpi_name=type_str.split("_")[0],
                constraint_type=(ConstraintType.MIN if constraint_str == "min"
                                 else ConstraintType.MAX),
                target_value=value,
                unit=data.get("unit", "")
            ),
            scope=IntentScope(
                ue_ids=data.get("scope", {}).get("ue_ids", []),
                bs_ids=data.get("scope", {}).get("bs_ids", [])
            ),
            description=data.get("description", intent_text)
        )

        return {"intent": intent, "raw": data}

    def _check_conflicts(self, new_intent: Intent, active_intents: List[Intent]) -> bool:
        """S1 conflict screening: check the new intent against active ones.

        NOTE: this method is the ONLY conflict-screening logic in the
        system. decision/conflict_detector.py was removed (P10) - it was
        never wired into the loop.
        """
        # Simple conflict detection
        for active in active_intents:
            # Same type, opposite constraints
            if new_intent.type == active.type:
                if (new_intent.target.constraint_type != active.target.constraint_type):
                    return True
        return False

    def _get_network_state(self) -> NetworkState:
        """Get current network state (per-UE KPIs + current configuration)"""
        state = NetworkState()
        metrics = self.ue_collector.collect_all()
        self._merge_throughput(metrics)

        for ue_id, m in metrics.items():
            ue_state = m.to_dict()
            # Batch G (P0-18): surface each UE's serving cell so a proposer maps a
            # per-UE deficit to the RIGHT cell dynamically (shared-cell UEs -> the
            # same BS), instead of a hardcoded ue1->bs1/ue2->bs2 index convention.
            gnb = self.ue_serving_gnb.get(ue_id)
            if gnb:
                ue_state["serving_gnb"] = gnb
                ue_state["serving_bs"] = _bs_of_gnb(gnb)
            state.set_ue_state(ue_id, ue_state)

        # Current configuration: the full d>=2 action vector per gNB, so the LLM
        # sees every controllable axis (not just power). Neutral values (prb_cap
        # 0 = full carrier, sched_priority 1.0, mcs_offset 0 = unconstrained)
        # tell the LLM the axis is currently unconstrained.
        for gnb_id, gstate in self.executor.states.items():
            bs_id = _bs_of_gnb(gnb_id)  # gnb1 -> bs1 (robust, not last-char)
            bs_state = {
                "power_offset_db": gstate.power_offset_db,
                "prb_cap": gstate.prb_cap,
                "sched_priority": gstate.sched_priority,
                "mcs_offset": gstate.mcs_offset,
            }
            # Surface active per-UE (per-RNTI) overrides so the LLM sees intra-cell
            # partitioning already in effect. Only added when present, so the
            # power-only / no-per-UE path produces the identical d=1 state dict.
            ue_ov = self._ue_overrides_for(gnb_id, gstate)
            if ue_ov:
                bs_state["ue_overrides"] = ue_ov
            state.set_bs_state(bs_id, bs_state)

        return state

    def _ue_overrides_for(self, gnb_id: str, gstate) -> Dict[str, Dict]:
        """Non-neutral per-UE overrides on one gNB, labelled by UE id where the
        RNTI is known (else "rnti_xxxx"): {label: {sched_priority?, prb_cap?}}."""
        rev = {rnti: ue_id for ue_id, rnti in self.ue_rnti.items()
               if rnti is not None and self.ue_serving_gnb.get(ue_id) == gnb_id}
        out: Dict[str, Dict] = {}
        for rnti, w in gstate.ue_sched_priority.items():
            if abs(w - self.action_space.sched_priority_neutral) > 1e-4:
                out.setdefault(rev.get(rnti, f"rnti_{rnti:04x}"), {})["sched_priority"] = w
        for rnti, p in gstate.ue_prb_cap.items():
            if p != self.action_space.prb_uncapped:
                out.setdefault(rev.get(rnti, f"rnti_{rnti:04x}"), {})["prb_cap"] = p
        return out

    def _analyze_feasibility(self, new_intent: Intent, active_intents: List[Intent],
                            network_state: NetworkState) -> FeasibilityPrediction:
        """Analyze feasibility using LLM"""
        active_dicts = [i.to_dict() for i in active_intents]
        new_dict = new_intent.to_dict()
        state_dict = network_state.to_dict()

        # Batch E (P0-15): capture THIS intent's content signature with the SAME
        # hash used for retrieval, so a later finalized record and the retrieval
        # query key on identical signatures.
        try:
            self._last_intent_signature = self._intent_content_hash(new_intent)
        except Exception:
            self._last_intent_signature = ""
        # the retrieved history is the DETERMINISTIC relevance selection for this
        # (model, regime) partition - or ZERO records under DISABLED - replacing
        # the old last-five recency window. The PINNED backend serves the call
        # (P0-16).
        history = self._history_for_prompt(new_intent)

        # Batch G (P1-6): stamp the REAL prompt hash BEFORE the bounded model
        # call, so a model TIMEOUT/exception (which abandons the response) still
        # retains the exact prompt hash of the prompt it generated. A FAILURE to
        # construct/hash the prompt is NOT swallowed: it propagates HERE, before
        # any bounded model call is issued (so the backend call count stays zero
        # and the cycle fails closed with a None prompt hash), rather than being
        # masked as an empty hash that then rides through a real model call.
        self._cur_prompt_hash = self.llm_manager.build_feasibility_prompt_hash(
            active_dicts, new_dict, state_dict, history)
        # P0-7: bound the model call by the remaining episode time.
        # Batch G (P1-3): measure the REAL proposal-inference latency (the wall
        # time of the bounded model call), linked to this cycle. The timer is in a
        # finally so a model TIMEOUT/exception still records the elapsed latency
        # (the real time spent) rather than losing it.
        _inf_t0 = time.monotonic()
        try:
            response = self._bounded_call(
                lambda: self._pinned_analyze_feasibility(
                    active_dicts, new_dict, state_dict, history),
                float(getattr(self, "model_call_timeout_s", 30.0)),
                label="feasibility model call")
        finally:
            self._cur_inference_ms = (time.monotonic() - _inf_t0) * 1000.0

        # Batch G: prefer the response's stamped prompt hash when present, but
        # NEVER discard the pre-call hash (retained on timeout/failure).
        _resp_ph = getattr(response, "prompt_hash", None)
        if _resp_ph:
            self._cur_prompt_hash = _resp_ph
        # Batch G: response.success is the EXPLICIT proposal-generated source fact
        # and MUST be an exact bool. A missing/non-bool success is a MALFORMED
        # backend response and FAILS CLOSED (raise) - never bool()-coerced (a
        # truthy non-bool such as the string "false" would wrongly route to a
        # trial). The cycle's outer handler turns this into a TechnicalFailsafe.
        success = getattr(response, "success", None)
        if not isinstance(success, bool):
            raise MalformedResponseError(
                f"backend feasibility response.success is not a bool: {success!r}")
        # a PROPOSAL was generated IFF the model returned output (an explicit
        # source fact for the proposal stage - NOT inferred from the prompt hash).
        self._cur_proposal_generated = success

        if not success:
            # a model FAILURE (no output) is not a schema violation - leave the
            # schema verdict valid; simply route to negotiation as infeasible.
            return FeasibilityPrediction(
                feasible=False, confidence=0.0, reasoning="LLM analysis failed")

        # P0-5: strictly validate the feasibility object.  `feasible` must be a
        # JSON boolean (so "false" cannot route to a trial); `confidence` a
        # finite number in [0,1]; NaN/Infinity/duplicate-key rejected.  A schema
        # rejection is a WHOLE-proposal rejection (fail-closed to infeasible ->
        # negotiation), recorded in the schema verdict for the evidence metric.
        content = getattr(response, "content", "") or ""
        # Batch G (P1-3): measure the STRICT-SCHEMA validation latency (whole-
        # proposal validation + action-vector parse). The finally records it even
        # on a schema-rejection return (no measurement lost); the admission /
        # calibration / routing portion is measured separately in the cycle and
        # summed into schema_admission_ms.
        _sch_t0 = time.monotonic()
        try:
            source = content if content.strip() else json.dumps(
                response.parsed_json if response.parsed_json is not None else {})
            data = _schema.validate_feasibility(source)
            # (validate_feasibility already strictly validates the alternatives
            # array when present; the dynamic {"alternatives": ...} response
            # shape is validated only at the S5 boundary.)
            # P0-5 (coordinator review): validate the proposed action vector
            # TOPOLOGY-AWARE whenever the field is present - EVEN when
            # feasible=false - so a malformed whole proposal never receives
            # schema_valid=true (unknown/typo key or non-finite action value
            # rejects the whole proposal here, not only at execute time).
            pc = data.get("proposed_config")
            if pc:
                self._parse_action_vector(pc)
        except SchemaError as e:
            self._cur_schema_ms = (time.monotonic() - _sch_t0) * 1000.0
            self._note_schema_invalid(f"feasibility: {e}")
            self._log_gui(f"Feasibility proposal rejected (schema): {e}")
            return FeasibilityPrediction(
                feasible=False, confidence=0.0,
                reasoning=f"schema rejected: {e}")

        # schema validation PASSED: record the real True verdict (never a default)
        # and its measured latency (P1-3).
        self._cur_schema_valid = True
        self._cur_schema_ms = (time.monotonic() - _sch_t0) * 1000.0

        # shared parser: alternatives with a numeric target_value carry a
        # modified_intent so S5's monotonic-relaxation rule can compare them
        alternatives = self._alternatives_from_json(data, new_intent)

        return FeasibilityPrediction(
            feasible=data["feasible"],
            confidence=data["confidence"],
            reasoning=data.get("reasoning", ""),
            proposed_config=data.get("proposed_config", {}),
            expected_kpi=data.get("expected_kpi", {}),
            alternatives=alternatives
        )

    def _finalize_schema_admission(self, cycle: Dict) -> None:
        """Batch G (P1-3): write the cycle's schema_admission_ms = the measured
        strict-schema validation latency PLUS the admission (calibration / theta
        routing / budget reservation) latency. Called on EVERY routing branch so
        the segment is measured for trial-routed AND negotiation/budget-blocked
        cycles. None only if neither part was measured (no proposal stage)."""
        sch = getattr(self, "_cur_schema_ms", None)
        adm_t0 = getattr(self, "_cur_adm_t0", None)
        adm = ((time.monotonic() - adm_t0) * 1000.0) if adm_t0 is not None else None
        parts = [p for p in (sch, adm) if p is not None]
        cycle["schema_admission_ms"] = sum(parts) if parts else None

    def _note_schema_invalid(self, reason: str) -> None:
        """Record a strict-schema rejection for the current proposal (P0-5), so
        the cycle record and the evidence ``schema_valid`` verdict carry the
        ACTUAL validator result and its reason (the runner metric uses it)."""
        self._cur_schema_valid = False
        self._cur_schema_reason = str(reason)

    # CLOSED, ANCHORED action-key grammar (P0-5, coordinator review): a key is
    # an action ONLY if it FULL-matches <prefix><N>_<axis-suffix>.  No substring
    # matching, so 'bs1_power_typo' and 'junk_bs1_power_offset_suffix' are NOT
    # actions (they reject the whole proposal).  Per-cell prefixes (bs / the
    # documented gnb, cell aliases) -> gnb<N>; the per-UE prefix is ue.
    _KEY_RE = re.compile(r"^(bs|gnb|cell|ue)(\d+)_([a-z_]+)$")
    # per-cell axis suffix -> (canonical axis, is_absolute_mcs_cap).  Only the
    # proven suffixes (bs*_power_offset/prb/sched_priority/mcs_offset, the
    # bs*_mcs_cap absolute-cap alias, and the prb_alloc legacy alias).
    _CELL_AXIS_SUFFIX = {
        "power_offset": ("power_offset", False),
        "prb": ("prb", False),
        "prb_alloc": ("prb", False),
        "sched_priority": ("sched_priority", False),
        "mcs_offset": ("mcs_offset", False),
        "mcs_cap": ("mcs_offset", True),
        "mcs_max": ("mcs_offset", True),
    }
    # per-UE axis suffix -> only the two per-UE-addressable axes (prb, sched
    # priority); a per-UE key on a cell-only axis (power/mcs) is NOT an action.
    _UE_AXIS_SUFFIX = {
        "prb": ("prb", False),
        "prb_alloc": ("prb", False),
        "sched_priority": ("sched_priority", False),
    }

    def _parse_axis_key(self, key: str):
        """Parse a proposed_config key into (gnb_id, ue_id, axis, is_abs_mcs_cap)
        via the CLOSED, anchored grammar (P0-5).

        Exactly one of gnb_id / ue_id is set:
          * per-cell keys (bs1_*, gnb2_*, cell1_*) -> gnb_id set, ue_id None;
          * per-UE keys (ue1_prb, ue3_sched_priority, ue2_prb_alloc) -> ue_id set,
            gnb_id None (serving gNB resolved later).  Per-UE addressing is
            accepted ONLY for the two per-UE axes (prb, sched_priority).
        Returns None for anything that is NOT a full-match action key (non-action
        metadata, a suffix/prefix typo, or a per-UE key on a cell-only axis).

        The key is matched EXACTLY - no strip()/lower() coercion (coordinator
        review): 'BS1_power_offset' or ' bs1_power_offset' (whitespace/case) are
        NOT actions."""
        if not isinstance(key, str):
            return None
        m = self._KEY_RE.match(key)
        if m is None:
            return None
        prefix, num, suffix = m.group(1), m.group(2), m.group(3)
        if prefix == "ue":
            entry = self._UE_AXIS_SUFFIX.get(suffix)
            if entry is None:
                return None                # cell-only axis or unknown suffix
            axis, abs_cap = entry
            return None, "ue" + num, axis, abs_cap
        entry = self._CELL_AXIS_SUFFIX.get(suffix)
        if entry is None:
            return None
        axis, abs_cap = entry
        return "gnb" + num, None, axis, abs_cap

    # The ONLY non-action keys tolerated inside a proposed_config: envelope
    # metadata the model may echo alongside the action vector (P0-5, coordinator
    # review).  Every OTHER unrecognized key - including a typo-like action key
    # that matches no axis - REJECTS the whole proposal, so an unknown action can
    # never be silently dropped.
    # ONLY the proven legacy envelope-metadata keys are tolerated inside a
    # proposed_config (coordinator review): speculative explanation / rationale /
    # note / comment / description keys are NOT whitelisted and reject the whole
    # proposal like any other unknown key.
    _ENVELOPE_METADATA_KEYS = frozenset({"reasoning", "expected_kpi"})

    def _parse_action_vector(self, proposed: Dict):
        """Parse proposed_config into a list of (gnb_id, axis, raw_value, ue_id).

        Understands the d>=2 schema keys, per-cell (bs1_power_offset, bs2_prb,
        bs1_sched_priority, bs2_mcs_offset) and per-UE (ue1_sched_priority,
        ue3_prb). For a per-UE key ue_id is set and gnb_id is the UE's serving
        gNB (from config); for a per-cell key ue_id is None. Absolute MCS caps
        are converted to the offset convention here.

        P0-5 (strict action-proposal schema): a key that RESOLVES TO AN ACTION
        AXIS but targets an unconfigured gNB/UE is an UNKNOWN ACTION KEY and
        REJECTS THE WHOLE PROPOSAL (raise SchemaError) - never silently dropped;
        and every action value MUST be a FINITE number, so a NaN/Infinity can
        never reach the executor to be clipped to a boundary.  A key that parses
        as NEITHER an action nor an explicitly whitelisted envelope-metadata key
        (``reasoning``/``expected_kpi``/...) - i.e. a typo-like unknown key -
        ALSO rejects the whole proposal (coordinator review): only whitelisted
        envelope metadata is skipped, everything else fails closed."""
        actions = []
        seen_canonical: Dict[str, str] = {}   # canonical key -> proposal key
        for key, value in (proposed or {}).items():
            parsed = self._parse_axis_key(key)
            if parsed is None:
                # whitelisted envelope metadata is tolerated, but its VALUE is
                # still validated (reasoning: string; expected_kpi: object of
                # finite numbers) - not merely skipped (coordinator review).
                # EXACT key match: 'Reasoning'/' reasoning ' are NOT whitelisted.
                if key == "reasoning":
                    _schema._require_str(value, "proposed_config.reasoning")
                    continue
                if key == "expected_kpi":
                    _schema._validate_kpi_object(value,
                                                 "proposed_config.expected_kpi")
                    continue
                raise SchemaError(
                    f"unrecognized proposal key {key!r} rejects the whole "
                    f"proposal (not an action axis, not envelope metadata)")
            gnb_id, ue_id, axis, abs_cap = parsed
            # finite-number contract FIRST (rejects NaN/Infinity/-Infinity,
            # bools, strings) BEFORE any target lookup or clipping - a NaN action
            # is rejected regardless of how the coordinator is wired.
            raw = _schema.require_finite_number(value, f"proposed_config[{key!r}]")
            if ue_id is not None:
                gnb_id = self.ue_serving_gnb.get(ue_id)
                if gnb_id is None:
                    raise SchemaError(
                        f"action key {key!r} targets an unconfigured UE "
                        f"(unknown action key)")
            if gnb_id not in self.executor.states:
                raise SchemaError(
                    f"action key {key!r} targets an unconfigured gNB "
                    f"(unknown action key)")
            if abs_cap:
                raw = raw - self.action_space.base_mcs_cap  # cap -> offset
            # CANONICAL AXIS COLLISION (coordinator review item 2): two DISTINCT
            # proposal keys / aliases / prefix-aliases that resolve to the SAME
            # canonical (gnb, ue-or-cell, axis) - e.g. bs1_prb + bs1_prb_alloc,
            # or bs1_X + gnb1_X - reject the WHOLE proposal BEFORE any write, so
            # the executor receives at most ONE unique canonical action per axis
            # (never a double write / ambiguous canonical evidence).
            ckey = _cb.canonical_action_key(gnb_id, axis, ue_id)
            if ckey in seen_canonical:
                raise SchemaError(
                    f"action key {key!r} collides with {seen_canonical[ckey]!r} "
                    f"(both resolve to the same canonical axis {ckey!r})")
            seen_canonical[ckey] = key
            actions.append((gnb_id, axis, raw, ue_id))
        return actions

    def _authorize_action(self, feasibility: FeasibilityPrediction,
                          cycle: Dict, tx: "ActuationTransaction",
                          current_intent: Intent) -> None:
        """Canonicalize the authorized action and bind a stable hash +
        authorization artifact BEFORE the first write (P0-6 / 3.3).

        The proposal is strictly parsed (unknown key / non-finite value ->
        SchemaError, propagated to route to negotiation without a write) and
        clipped to the enforcement bound U; that clipped, enforcement-approved
        vector IS the canonical action the executor applies.  The authorization
        binds the canonical hash to the intent-set version, the authorized
        revision id, the identifier chain, and an expiry no longer than the
        remaining episode time.  Stored on the transaction (the commit gate
        re-checks it) and on the cycle (the evidence record binds it)."""
        # No generic-exception degradation (coordinator review C2): a strict
        # schema rejection propagates as SchemaError (-> negotiation, no write);
        # a topology/action-derivation failure propagates as an EXPLICIT internal
        # error (-> TechnicalFailsafe).  It NEVER silently degrades to an empty
        # authorization that would look valid.
        try:
            actions = self._parse_action_vector(feasibility.proposed_config or {})
        except SchemaError:
            raise
        except Exception as e:
            raise RuntimeError(f"action authorization derivation failed: {e}")
        clipped = []
        clip_events = []
        for gnb_id, axis, raw, ue_id in actions:
            per_ue = ue_id is not None
            applied = self.action_space.clip(axis, raw, per_ue=per_ue)
            if self._is_clipped(axis, raw, applied, per_ue=per_ue):
                clip_events.append({"gnb_id": gnb_id, "ue_id": ue_id,
                                    "axis": axis, "proposed": raw,
                                    "applied": applied})
            clipped.append((gnb_id, axis, applied, ue_id))
        canonical = _cb.canonicalize_action(clipped)
        action_hash = _cb.stable_action_hash(canonical)
        # the immutable authorized action vector the executor will consume
        # (never re-derived from the raw proposal after this point, P0-6).
        tx.authorized_vector = tuple(clipped)
        # ALIAS ISOLATION (coordinator correction #6 item 1): clip_events are
        # dicts that later flow to the public trial/cycle; tx keeps its OWN
        # deep copies so a public mutation of result["clipped"]/cycle["clipped"]
        # cannot reach back into the trusted tx provenance.
        tx.clip_events = tuple(copy.deepcopy(e) for e in clip_events)
        # P0-6: snapshot the REQUESTED action at authorization (deep copy) so
        # _execute_trial can never overwrite it from a later-mutated raw
        # proposal.
        tx.requested_action = copy.deepcopy(dict(feasibility.proposed_config
                                                 or {}))
        # NOTE (P0-6, coordinator review): the actuation_trial_id is NOT minted
        # here - snapshot capture/verification can still fail before any write,
        # which would leave a dishonest id.  The canonical action is FIXED now
        # (before snapshot/write); the trial id is minted, and the authorization
        # rebound to carry it, only at the pre-write point in _execute_trial.
        d = getattr(self, "_deadline", None)
        ttl = (d.remaining() if d is not None
               else float(getattr(self, "episode_budget_s", 120.0)))
        auth = _cb.bind_authorization(
            canonical,
            authorized_revision_id=getattr(current_intent, "id", None),
            authorized_intent_hash=self._intent_content_hash(current_intent),
            intent_set_version=self._intent_set_version(),
            now=time.time(), ttl_s=ttl,
            episode_id=cycle.get("episode_id"),
            cycle_id=cycle.get("cycle_id"),
            proposal_id=cycle.get("proposal_id"),
            actuation_trial_id=None)    # bound at the pre-write point
        tx.canonical_action = dict(canonical)
        tx.canonical_action_hash = action_hash
        tx.authorization = auth
        cycle["canonical_action"] = dict(canonical)
        cycle["canonical_action_hash"] = action_hash
        cycle["authorization"] = auth.to_dict()
        # TRUSTED provenance bound HERE (before the write), so the post-final-gate
        # publication reads ONLY these already-bound primitives - it never calls
        # an external llm_manager (_proposer_context) or re-reads a mutable
        # result/cycle after the gate (coordinator review #5 items 1/3).
        # (best-effort provenance - it is NOT a commit-gate input, so a proposer/
        # hash lookup failure here must not fail-closed the authorization; the
        # provisional _build_evidence still exercises the post-write failure
        # paths.)
        result = getattr(self, "_active_result", {}) or {}
        try:
            proposer_id, model_version = self._proposer_context()
        except Exception:
            proposer_id = model_version = "unknown"
        tx.proposer_id = proposer_id or "unknown"
        tx.model_version = model_version or "unknown"
        tx.experiment_run_id = (result.get("experiment_run_id")
                                or getattr(self, "experiment_run_id", "")
                                or "unknown")
        # Batch E: bind the preallocated evidence-record identity (trusted, from
        # the coordinator-owned result) so the commit evidence reuses the SAME id.
        tx.evidence_record_id = result.get("evidence_record_id")
        tx.episode_id = (cycle.get("episode_id") or result.get("episode_id")
                         or "unknown")
        tx.fsm_step_id = (cycle.get("fsm_step_id") or result.get("fsm_step_id")
                          or "unknown")
        # Batch G (P1-6): bind the tx pending-intent hash from THIS cycle's frozen
        # snapshot digest (stamped at the boundary), so a revised cycle binds its
        # OWN intent. The result-level _pending_intent_hash is only a NO-CYCLE
        # fallback (a terminal that never stamped a cycle snapshot).
        try:
            tx.pending_intent_hash = (cycle.get("pending_intent_hash")
                                      or self._pending_intent_hash(result))
        except Exception:
            tx.pending_intent_hash = "unknown"
        # Batch G (P1-6): bind the EXACT REAL prompt hash of THIS cycle's proposal
        # (stamped at generation) onto the transaction BEFORE the write, so the
        # finalized EvidenceRecord carries it from trusted bound provenance - not
        # a re-read of the mutable cycle after the gate. Kept as the exact value
        # (None if somehow absent) - NEVER coerced to '' - so the pre-write S3
        # invariant can fail closed when a write would proceed without it.
        tx.prompt_hash = (cycle.get("prompt_hash")
                          or getattr(self, "_cur_prompt_hash", None))

    @staticmethod
    def _sample_admissible_for_commit(d: Dict, action_apply_time) -> bool:
        """A measurement-sample dict may enter commit evidence ONLY when it is a
        LIVE, FRESH, non-UNKNOWN, POST-ACTION (in_window / commit_readback)
        sample taken at/after the trusted action_apply_time (Batch F). Any
        cached / pre-action / background / stale / UNKNOWN sample is rejected."""
        if not isinstance(d, dict):
            return False
        if d.get("status") != "ok" or d.get("value") is None:
            return False
        if d.get("cache_status") != "live":
            return False
        if d.get("freshness") != "fresh":
            return False
        if d.get("purpose") not in ("in_window", "commit_readback"):
            return False
        st = d.get("sample_time")
        if action_apply_time is None or not isinstance(st, (int, float)) \
                or isinstance(st, bool):
            return False
        try:
            return float(st) >= float(action_apply_time)
        except (TypeError, ValueError):
            return False

    def _capture_commit_evidence(self, tx: "ActuationTransaction",
                                 cycle: Dict, validation: Dict) -> None:
        """Record the post-action observations + a FRESH final device read-back
        of the canonical action onto the transaction, for the atomic commit
        gate (P0-6).  Only meaningful after a REAL device write; a
        simulation / no-write trial leaves them empty (the gate does not
        require them there)."""
        # Batch F (P0-13, coordinator review): the commit gate sees ONLY
        # observations derived from ADMISSIBLE-FOR-COMMIT MeasurementSamples -
        # live, fresh, non-UNKNOWN, POST-ACTION (in_window/commit_readback). A
        # cached / pre-action / stale / UNKNOWN sample is DROPPED here, so it can
        # never validate a commit (a probe failure leaves NO admissible sample
        # and the gate's observations_required check fails closed).
        apply_t = getattr(tx, "action_apply_time", None)
        msamples = validation.get("measurement_samples") or []
        obs = []
        if msamples:
            # AUTHORITATIVE path (real S4): derive commit observations ONLY from
            # ADMISSIBLE MeasurementSamples (drops UNKNOWN/stale/pre-action/cache).
            for d in msamples:
                if not self._sample_admissible_for_commit(d, apply_t):
                    continue
                st = d.get("sample_time")
                obs.append({
                    "source": d.get("source"), "value": d.get("value"),
                    "status": d.get("status"),
                    "cache_status": d.get("cache_status"),
                    "probe_id": d.get("probe_id"), "sample_time": st,
                    "collection_start": st, "collection_end": st,
                    "freshness_verdict": d.get("freshness"),
                })
        else:
            # LEGACY path (direct-call unit fixtures supply Observations): carry
            # full provenance; the commit-binding freshness gate validates them.
            for o in validation.get("observations") or []:
                d = o.to_dict() if isinstance(o, Observation) else (
                    dict(o) if isinstance(o, dict) else None)
                if d is None:
                    continue
                obs.append({
                    "source": d.get("source"),
                    "sample_time": d.get("sample_time"),
                    "collection_start": d.get("collection_start"),
                    "collection_end": d.get("collection_end"),
                    "freshness_verdict": d.get("freshness_verdict"),
                })
        # ALIAS ISOLATION (coordinator correction #6 item 1): tx carries the
        # TRUSTED provenance and the public cycle carries a display copy; they
        # must be INDEPENDENT objects so that a callback mutating the public
        # cycle (e.g. flipping a verdict violated->satisfied during provisional
        # finalize) can never reach through a shared alias and change what the
        # typed commit gate reads from tx.
        tx.observations = tuple(copy.deepcopy(o) for o in obs)
        cycle["observations"] = [copy.deepcopy(o) for o in obs]
        tx.measurement_samples = tuple(copy.deepcopy(m) for m in msamples)
        cycle["measurement_samples"] = [copy.deepcopy(m) for m in msamples]
        cycle["action_apply_time"] = tx.action_apply_time
        cycle["action_apply_monotonic_s"] = getattr(
            tx, "action_apply_monotonic_s", None)
        cycle["snapshot_readback_time"] = tx.snapshot_readback_time
        # P0-6 (coordinator review B4): the ACTUAL per-intent MonitorVerdict
        # values from S4 flow into tx/cycle/EvidenceRecord (the required field
        # must not be left empty on a real validation).  tx gets its OWN deep
        # copy (the trusted gate input); the cycle gets a SEPARATE deep copy.
        mv_src = dict(validation.get("monitor_verdicts") or {})
        tx.monitor_verdicts = copy.deepcopy(mv_src)
        cycle["monitor_verdicts"] = copy.deepcopy(mv_src)
        # NOTE: the commit-gate read-back is done FRESH at settlement in
        # _verify_commit (not reused from here); this S4 capture is evidence.
        if tx.actual_real_write_performed:
            # Batch G (P1-3): measure the device final-readback segment in a
            # finally so a raising readback still records the latency.
            _rb_t0 = time.monotonic()
            try:
                cycle["final_readback"] = self._readback_canonical(tx)
            finally:
                cycle["readback_ms"] = (time.monotonic() - _rb_t0) * 1000.0
            cycle["final_readback_time"] = tx.final_readback_time

    def _readback_canonical(self, tx: "ActuationTransaction"
                            ) -> Optional[Dict[str, float]]:
        """Fresh device read-back of the AUTHORIZED canonical action vector, in
        the SAME canonical representation as the authorized action (P0-6).

        Reads back exactly the axes the executor was authorized to apply
        (``tx.authorized_vector``), NOT a separately-fabricated applied list, so
        the commit gate compares like-for-like.  An empty authorized vector
        reads back nothing (``{}``), which matches an empty canonical.  Returns
        None if any authorized axis cannot be read (an unverifiable read-back
        blocks the commit)."""
        out: Dict[str, float] = {}
        for gnb_id, axis, _value, ue_id in (tx.authorized_vector or ()):
            rnti = self._resolve_ue_rnti(ue_id) if ue_id is not None else None
            try:
                val = self.executor.get_axis(gnb_id, axis, rnti)
            except Exception as e:
                logger.warning(f"final read-back of {axis} failed: {e}")
                return None
            # STRICT (coordinator review): a device read-back value MUST be a
            # finite JSON number - str / bool / NaN / +-Infinity / wrong type is
            # UNVERIFIABLE (never coerced) and blocks the commit.
            fv = _cb._finite_num(val)
            if fv is None:
                logger.warning(f"final read-back of {axis} is not a finite "
                               f"number ({val!r}) - unverifiable")
                return None
            out[_cb.canonical_action_key(gnb_id, axis, ue_id)] = fv
        return out

    def _bind_trial_id_prewrite(self, tx: "ActuationTransaction") -> None:
        """Mint the actuation trial id ONCE at the pre-write point (after a
        verified snapshot capture, immediately before the first actual nonempty
        executor write) and REBIND the authorization to carry it, keeping the
        canonical action fixed (P0-6, coordinator review).  A snapshot/verify
        failure earlier returns before this, so no dishonest id is minted.

        Batch G (P1-6): a real S3 write is FAIL-CLOSED on its prompt hash - no
        actuation may proceed without the bound, nonempty prompt hash of the
        proposal that produced this action. (A no-prompt pre-proposal terminal
        never reaches this point.)"""
        if not getattr(tx, "prompt_hash", None):
            raise ValueError(
                "refusing S3 actuation without a bound prompt hash (P1-6): the "
                "proposal that produced this write has no recorded prompt hash")
        if not tx.actuation_trial_id:
            tx.actuation_trial_id = new_id("trial")
        auth = getattr(tx, "authorization", None)
        if auth is not None and getattr(
                auth, "actuation_trial_id", None) != tx.actuation_trial_id:
            tx.authorization = dataclasses.replace(
                auth, actuation_trial_id=tx.actuation_trial_id)

    def _is_clipped(self, axis: str, raw: float, applied: float,
                    per_ue: bool = False) -> bool:
        """True only when `raw` was genuinely outside U (ignores rounding)."""
        lo, hi = self.action_space.bounds(axis, per_ue)
        if axis == "prb":
            if raw <= 0:
                return False  # uncapped sentinel is legal, not a clip
            return raw < lo - 0.5 or raw > hi + 0.5
        return raw < lo - 1e-9 or raw > hi + 1e-9

    def register_ue_rnti(self, ue_id: str, rnti: int):
        """Register/override the RNTI for a logical UE. Required for UEs that
        share a cell (UE1/UE2 on BS1), which get_connected_rnti cannot tell
        apart -- see OPEN_QUESTION in docs/action_space_d2.md."""
        self.ue_rnti[ue_id] = int(rnti)

    def _resolve_stale_rnti(self, gnb_id: str, old_rnti: int):
        """P8: registry-based reinterpretation of a stale RNTI.

        If the operator/registry already re-registered the (single) UE that
        serves this gNB under a NEW RNTI, use that registration; otherwise
        None and the executor falls back to device probes.
        """
        cands = [r for ue, r in self.ue_rnti.items()
                 if self.ue_serving_gnb.get(ue) == gnb_id and r is not None]
        fresh = [r for r in cands if r != old_rnti]
        if len(cands) == 1 and len(fresh) == 1:
            return fresh[0]
        return None

    def _on_rnti_remap(self, gnb_id: str, old_rnti: int, new_rnti: int):
        """P8: keep the coordinator's UE->RNTI cache coherent with an
        executor-detected re-establishment remap."""
        for ue_id, rnti in list(self.ue_rnti.items()):
            if rnti == old_rnti and self.ue_serving_gnb.get(ue_id) == gnb_id:
                self.ue_rnti[ue_id] = new_rnti
                logger.info(f"{ue_id}: RNTI cache updated "
                            f"{old_rnti:04x} -> {new_rnti:04x}")

    def _resolve_ue_rnti(self, ue_id: str):
        """RNTI of a logical UE for per-UE addressing, or None if unresolved.

        Resolution order: (1) an explicitly registered RNTI (register_ue_rnti,
        e.g. operator- or discovery-provided); (2) get_connected_rnti, which
        returns the single UE on the serving cell -- works for a UE alone on its
        cell (UE3 on BS2) but returns None when >1 UE shares the cell (UE1/UE2 on
        BS1), so those must be registered first.
        """
        gnb_id = self.ue_serving_gnb.get(ue_id)
        if gnb_id is None:
            return None
        rnti = self.ue_rnti.get(ue_id)
        if rnti is not None:
            return rnti
        rnti = self.executor.get_connected_rnti(gnb_id)
        if rnti is not None:
            self.ue_rnti[ue_id] = rnti  # cache for the rest of the session
        return rnti

    def _execute_trial(self, feasibility: FeasibilityPrediction) -> Dict:
        """Apply the LLM-proposed multi-axis configuration to the live RAN.

        Enforcement layer: EVERY axis of the proposed action vector (power, PRB,
        scheduling priority, MCS) is clipped to its action-space bound U before
        it touches hardware (Proposition 1); genuine out-of-bounds clips are
        counted (paper metric: executor enforcement statistics). Actions may be
        addressed per-cell (bsN_*) or per-UE (ueN_*, resolved to an RNTI at apply
        time); per-UE axes are clipped to the per-UE bounds. The pre-trial state
        (ALL axes, ALL gNBs, incl. per-UE overrides) is snapshotted for
        deterministic rollback. Backward compatible: a proposal carrying only
        power offsets behaves exactly like the d=1 baseline.
        """
        result = {"success": False, "snapshot": {}, "clipped": [], "applied": []}

        # Shared transaction context (Gate-review item 1): the SINGLE source of
        # truth for "did a real device write happen". Updated below immediately
        # AFTER the first successful executor write, so a mid-write exception
        # that never returns still records the write and the caller's finally
        # rolls back exactly once.
        tx = self._get_txn()
        # CANONICAL-ONLY EXECUTION (coordinator review C1): without a VALID
        # authorization (bound authorization + canonical action hash + a
        # NONEMPTY authorized vector) this method performs ZERO writes and fails
        # closed.  There is NO raw-proposal parse/clip/write fallback - the
        # executor only ever receives the authorized canonical action.
        auth = getattr(tx, "authorization", None)
        authorized = tuple(getattr(tx, "authorized_vector", ()) or ())
        if auth is None or not getattr(tx, "canonical_action_hash", "") \
                or not authorized:
            self._log_gui("Trial rejected: no valid authorized action bound "
                          "(fail-closed, zero writes)")
            return result

        tx.simulation = bool(getattr(self.ue_collector, "simulation_mode", False))
        raw_snapshot = self.executor.snapshot()
        # P0-6: record the verified snapshot read-back time immediately after
        # the (device-verified) snapshot capture.
        tx.snapshot_readback_time = time.time()
        # ALIAS ISOLATION (coordinator review #5 item 2): the TRUSTED snapshot on
        # the transaction is an INDEPENDENT deep copy - it does NOT alias the
        # executor's returned structure NOR the result's copy, so a later public
        # mutation of result['snapshot'] / cycle['snapshot'] (or a mutating
        # executor) can never corrupt the snapshot used for rollback/evidence.
        snapshot = copy.deepcopy(raw_snapshot)
        tx.snapshot = snapshot
        result["snapshot"] = copy.deepcopy(raw_snapshot)
        # requested_action was snapshotted (deep-copied) at authorization; never
        # overwrite it from the raw proposal here.

        # consume ONLY the authorized canonical action vector.
        clipped_actions = list(authorized)
        # deep copy: the public result must not alias the trusted tx.clip_events
        # dicts (coordinator correction #6 item 1).
        result["clipped"] = [copy.deepcopy(e)
                             for e in (getattr(tx, "clip_events", ()) or ())]
        # record the canonical of what the executor is actually handed, so the
        # commit gate can recompute the hash from it (not a cached string).
        tx.applied_canonical = _cb.canonicalize_action(clipped_actions)

        axis_summary = ", ".join(f"{(ue_id or gnb_id)}.{a}={v:g}"
                                 for gnb_id, a, v, ue_id in clipped_actions)

        if self.ue_collector.simulation_mode:
            # A simulated "would-apply" is NOT an actual executor write attempt:
            # actuation_trial_id stays None for a no-write S3 route (coordinator
            # review / existing evidence contract).
            self._log_gui(f"Trial (simulation): would apply [{axis_summary}]")
            result["applied"] = [{"gnb_id": g, "ue_id": u, "axis": a,
                                  "value": v, "ok": True}
                                 for g, a, v, u in clipped_actions]
            result["first_write_time"] = time.time()
            result["success"] = True
            tx.applied = tuple(result["applied"])
            # simulation is NOT a real device write: tx.actual_real_write stays
            # False, so a simulation UNKNOWN restore never latches (item 3).
            return result

        # Gate A follow-up (Codex acceptance #4): an axis whose pre-trial
        # snapshot could NOT be read from the device (snapshot_source ==
        # "mirror") has no provable pre-state - a trial touching it cannot
        # guarantee rollback to the true device state, so it is an
        # UNQUALIFIED axis and the trial fails closed (apply-failure path
        # -> negotiation). Power stays device-readable even on a stock gNB
        # (ci rfatt), so the d=1 power-only path is unaffected.
        for gnb_id, axis, _val, ue_id in clipped_actions:
            src = (snapshot.get(gnb_id) or {}).get("snapshot_source") or {}
            key = ("ue_prb" if (axis == "prb" and ue_id is not None) else
                   "ue_sched" if (axis == "sched_priority"
                                  and ue_id is not None) else axis)
            # fail-closed: only an EXPLICIT device-read provenance
            # qualifies - a missing/unknown provenance (legacy snapshot,
            # from_device=False) is just as unprovable as a mirror one
            if src.get(key) != "device":
                self._log_gui(
                    f"Trial rejected: axis {axis} on {ue_id or gnb_id} has "
                    f"no device-readable pre-trial state (unqualified axis, "
                    f"fail-closed)")
                return result

        applied_ok = True
        minted = False        # trial id minted right before the FIRST apply
        for gnb_id, axis, val, ue_id in clipped_actions:
            rnti = None
            if ue_id is not None:
                # Per-UE action: resolve the UE's RNTI FIRST.  An unresolved
                # RNTI is ZERO executor attempts (coordinator review C3): the
                # trial id is NOT minted and no write is issued.
                rnti = self._resolve_ue_rnti(ue_id)
                if rnti is None:
                    applied_ok = False
                    tx.failed_axis = axis
                    result["applied"].append({"gnb_id": gnb_id, "ue_id": ue_id,
                                              "axis": axis, "value": val,
                                              "ok": False})
                    self._log_gui(f"Applied {ue_id} {axis}: FAILED "
                                  f"(could not resolve RNTI)")
                    break
            # About to make the FIRST actual executor.apply_axis attempt (after
            # snapshot qualification AND RNTI resolution): mint the trial id and
            # rebind the authorization to carry it (P0-6, coordinator review C3).
            if not minted:
                self._bind_trial_id_prewrite(tx)
                minted = True
            # Batch G (P1-6/P0-19): capture the monotonic APPLY-ATTEMPT timestamp
            # IMMEDIATELY BEFORE this axis's executor.apply_axis call, so every
            # applied event carries its OWN attempt time (not one repeated post-
            # return stamp) and an EXCEPTION path still records a real time. The
            # transaction's first action_apply_monotonic_s is the FIRST attempt.
            attempt_t = time.monotonic()
            if tx.action_apply_monotonic_s is None:
                tx.action_apply_monotonic_s = attempt_t
            # An apply that RAISES has UNKNOWN side-effect status: treat it
            # conservatively as a write ATTEMPT so the rollback path runs exactly
            # once (coordinator review C5), rather than assuming zero write.
            try:
                if rnti is not None:
                    ok = self.executor.apply_axis(gnb_id, axis, val, rnti=rnti)
                else:
                    ok = self.executor.apply_axis(gnb_id, axis, val)
            except Exception:
                tx.actual_real_write_performed = True
                if tx.first_write_time is None:
                    nowt = time.time()
                    tx.first_write_time = nowt
                    tx.action_apply_time = nowt
                tx.failed_axis = axis
                result["applied"].append({"gnb_id": gnb_id, "ue_id": ue_id,
                                          "axis": axis, "value": val,
                                          "ok": False,
                                          "attempt_monotonic_s": attempt_t})
                tx.applied = tuple(result["applied"])
                raise
            # Record the FIRST successful REAL write on the shared tx, right
            # after the executor confirms it (item 1/3): a later axis raising or
            # failing still leaves this recorded, so the finally rolls back once.
            if ok and not tx.actual_real_write_performed:
                now = time.time()
                tx.note_real_write(now, axis)
                result["first_write_time"] = now
            if not ok:
                tx.failed_axis = axis
            applied_ok = applied_ok and ok
            result["applied"].append({"gnb_id": gnb_id, "ue_id": ue_id,
                                      "axis": axis, "value": val, "ok": ok,
                                      "attempt_monotonic_s": attempt_t})
            tx.applied = tuple(result["applied"])
            self._log_gui(f"Applied {ue_id or gnb_id} {axis} = {val:g}: "
                          f"{'OK' if ok else 'FAILED'}")

        if not applied_ok:
            # Partial application - restore ALL axes through the SHARED rollback
            # transaction (P0-1 step 7), so a partial-apply restore FAILURE (or
            # an UNKNOWN restore after a REAL write, item 3) latches
            # TechnicalFailsafe. The verdict/latch is recorded on `result` and
            # absorbed onto the cycle by the caller; success stays False.
            self._log_gui("Trial application failed - restoring snapshot")
            tx.rollback_done = True
            outcome = self._run_rollback_transaction(
                snapshot, None, first_write_time=tx.first_write_time,
                actual_real_write=tx.actual_real_write_performed,
                audit_extra=self._audit_from_tx(tx, tx.failed_axis or "partial_apply"))
            result["rollback_result"] = outcome.to_dict()
            result["restore_verified"] = outcome.restore_verified
            result["config_restore_verdict"] = (
                outcome.config_restore_verdict.value)
            return result

        result["success"] = True
        return result

    def _validate_trial(self, new_intent: Intent, active_intents: List[Intent],
                        stop_on_hard_failure: bool = True) -> Dict:
        """S4: observe KPIs across the trial window by POLLING, so the cost
        estimator gets a measured trajectory instead of one end-of-window
        sample:
          tput_min   = min over samples of the mean UE throughput
          tput_after = mean UE throughput of the last sample
          hard_failure: a UE attached at S3 entry DETACHES for 2 consecutive
          samples (RRC/session loss).
          measurement_invalid: a UE stays ATTACHED but reports no usable
          throughput (None/NaN) for 2 consecutive samples - a telemetry
          failure (iperf/SSH/parser), NOT a session loss. It fails the trial
          closed (rollback) but must never contribute a C_hard sample (C5).

        Batch B (P0-3): ``stop_on_hard_failure`` is the DEFAULT-SAFE behavior.
        On the FIRST confirmed hard failure the sampling window STOPS
        immediately - no more samples are collected and the in-window
        reconnection wait is NOT performed here.  The caller then rolls back
        the configuration FIRST and measures physical recovery UNDER the
        restored config (_check_physical_recovery), so a harmful action is not
        held applied through a reconnection wait.  The returned dict carries
        ``recovery_pending=True`` + ``failed_ues`` so the transaction knows to
        run the post-rollback recovery phase.

        Passing ``stop_on_hard_failure=False`` selects the LEGACY behavior
        (keep sampling, await reconnection in-window, report reconnection_time)
        - retained only for the direct-call reconnection unit tests.
        """
        if self.ue_collector.simulation_mode:
            time.sleep(2.0)
            metrics = self.ue_collector.collect_all()
            self._merge_throughput(metrics)
            return self._build_validation(
                metrics, new_intent, active_intents,
                samples=[self._mean_tput_of(metrics)],
                trajectory=[self._snap_trajectory(metrics)],
                hard_failure=False, reconnection_time=0.0,
                measurement_invalid=False)

        tau = float(self.tau_trial_s)
        poll = max(0.05, tau / 10.0)
        pre_attached = set(getattr(self, "_pre_trial_attached", None) or [])
        apply_t = getattr(getattr(self, "_active_txn", None),
                          "action_apply_time", None)

        t0 = time.time()
        samples: List[Optional[float]] = []
        sample_times: List[float] = []
        window_samples: List = []        # Batch F: the EXACT live IN_WINDOW samples
        trajectory: List[Dict] = []
        detach_streak: Dict[str, int] = {u: 0 for u in pre_attached}
        invalid_streak: Dict[str, int] = {u: 0 for u in pre_attached}
        failed_ues: set = set()     # hard failures (detach)
        invalid_ues: set = set()    # attached but no usable KPI
        fail_t: Optional[float] = None
        reattach_t: Optional[float] = None
        metrics: Dict = {}

        def _ue_reattached(m) -> bool:
            # Reconnection is an ATTACHMENT event (the RRC/session loss is
            # over) - it must not require a working throughput probe, or a
            # post-detach telemetry failure would inflate reconnection_time
            # and contaminate C_hard with non-RRC time (C5).
            return m is not None and getattr(m, "attached", False)

        # fixed sample count (~tau/poll): a deterministic number of window
        # samples per trial, so emulated runs with the same seed consume the
        # channel RNG identically regardless of scheduling jitter
        n_samples = max(1, int(round(tau / poll))) if tau > 0 else 1
        for i in range(n_samples):
            # Batch F (P0-13): the AUTHORITATIVE throughput KPI is a REAL
            # structured live probe taken DURING the window under the SHARED
            # scheduler - carrying its OWN unique probe id + epoch sample_time
            # (never a cached repeat / post-aggregation id). A probe failure /
            # lock timeout is an explicit UNKNOWN (value=None), excluded from the
            # trajectory, never coerced to 0.
            tp_sample, probe_per_ue = self._measure_throughput_detailed(
                MeasurementPurpose.IN_WINDOW, action_apply_time=apply_t,
                label="s4_window", ue_scope=sorted(pre_attached))
            window_samples.append(tp_sample)
            samples.append(tp_sample.value)         # None when UNKNOWN
            sample_times.append(tp_sample.sample_time)
            # attachment / detach telemetry is a SEPARATE collect (its throughput
            # plays NO role in the authoritative KPI). OVERRIDE each UE's
            # throughput with the AUTHORITATIVE live probe value (None when the
            # probe did not measure it) - so a stale collect_all cache can NEVER
            # satisfy a throughput intent (P0-13, coordinator review).
            metrics = self.ue_collector.collect_all()
            for _ue, _m in metrics.items():
                try:
                    _m.throughput_mbps = probe_per_ue.get(_ue)
                except Exception:
                    pass
            trajectory.append(self._snap_trajectory(metrics))

            attached_now: Dict[str, bool] = {}
            for ue_id in pre_attached:
                m = metrics.get(ue_id)
                attached = m is not None and getattr(m, "attached", False)
                attached_now[ue_id] = attached
                has_kpi = attached and self._finite(
                    getattr(m, "throughput_mbps", None))
                detach_streak[ue_id] = (detach_streak[ue_id] + 1
                                        if not attached else 0)
                invalid_streak[ue_id] = (invalid_streak[ue_id] + 1
                                         if attached and not has_kpi else 0)
                if detach_streak[ue_id] >= 2 and ue_id not in failed_ues:
                    failed_ues.add(ue_id)
                    if fail_t is None:
                        fail_t = time.time()
                        self._log_gui(f"HARD FAILURE: {ue_id} lost during "
                                      f"trial window")
                    # a NEW failure re-opens the outage: a reattach latched
                    # for the earlier UE(s) no longer covers every failed UE
                    reattach_t = None
                if invalid_streak[ue_id] >= 2 and ue_id not in invalid_ues:
                    invalid_ues.add(ue_id)
                    self._log_gui(f"MEASUREMENT INVALID: {ue_id} attached "
                                  f"but reporting no usable throughput")
            # the reconnection clock stops when every FAILED UE is attached
            # again (attachment-based; see _ue_reattached)
            if (fail_t is not None and reattach_t is None and failed_ues
                    and all(attached_now.get(u, False) for u in failed_ues)):
                reattach_t = time.time()

            # P0-3: STOP sampling immediately on the first confirmed hard
            # failure. No further validation sample is collected - the caller
            # rolls back BEFORE any reconnection/recovery wait.
            if stop_on_hard_failure and failed_ues:
                self._log_gui("Hard failure confirmed - stopping trial window "
                              "(rollback precedes recovery, P0-3)")
                break

            if i < n_samples - 1:
                time.sleep(min(poll, max(0.0, tau - (time.time() - t0))))

        cap = float(getattr(self, "hard_failure_cap_s", 30.0))
        # LEGACY ONLY: await reconnection in-window (pre-rollback). In the
        # default-safe P0-3 path this is skipped - reconnection/recovery is
        # measured AFTER rollback, under the restored config.
        if (not stop_on_hard_failure) and fail_t is not None \
                and reattach_t is None:
            while cap - (time.time() - fail_t) > 0:
                m2 = self.ue_collector.collect_all()
                if all(_ue_reattached(m2.get(u)) for u in failed_ues):
                    reattach_t = time.time()
                    break
                remaining = cap - (time.time() - fail_t)
                if remaining <= 0:
                    break
                time.sleep(min(poll, remaining))
        reconnection_time = 0.0
        if fail_t is not None and not stop_on_hard_failure:
            reconnection_time = min(cap, (reattach_t or time.time()) - fail_t)

        # Batch F (P0-13, coordinator review A): ANY authoritative IN_WINDOW
        # UNKNOWN sample (probe failure / lock timeout / no data) - even a single
        # TRANSIENT one between OK samples (OK, UNKNOWN, OK) - fails the window
        # CLOSED: all_satisfied=False AND measurement_invalid=True, so an UNKNOWN
        # can never be smoothed into a "satisfied throughout window" claim. The
        # ONLY exception is a CONFIRMED DETACH (session loss), which is a
        # hard_failure and must not be re-tagged as telemetry (C_hard integrity).
        # a window sample is BAD when it is UNKNOWN or a non-admissible OK sample
        # (e.g. STALE: epoch before the trusted action_apply_time). Any bad
        # sample fails the window closed (review #87d28.2).
        any_window_unknown = (
            not window_samples
            or any(s.is_unknown() or not s.admissible_for_commit(apply_t)
                   for s in window_samples))
        telemetry_unknown = any_window_unknown and not failed_ues
        out = self._build_validation(
            metrics, new_intent, active_intents, samples=samples,
            trajectory=trajectory, hard_failure=bool(failed_ues),
            reconnection_time=reconnection_time,
            measurement_invalid=bool(invalid_ues) or telemetry_unknown,
            failed_ues=sorted(failed_ues),
            recovery_pending=bool(stop_on_hard_failure and failed_ues),
            throughput_unknown=any_window_unknown)
        # P0-6/P0-13: each in-window sample becomes an Observation whose freshness
        # HONESTLY reflects the underlying MeasurementSample - an UNKNOWN sample
        # is NEVER marked 'fresh' (so it can never pass the commit gate).
        out["observations"] = [
            Observation(source=m.source, value=m.value,
                        sample_time=m.sample_time, collection_start=m.sample_time,
                        collection_end=m.sample_time,
                        freshness_verdict=(m.freshness if not m.is_unknown()
                                           else "unknown")).to_dict()
            for m in (window_samples or [])]
        # Batch F (P0-13): the EXACT live IN_WINDOW MeasurementSamples taken
        # during the window (each already has its OWN unique probe id + epoch
        # sample_time from the real probe - NOT minted after aggregation).
        out["measurement_samples"] = [
            m.to_dict() for m in (window_samples or [])]
        return out

    def _build_validation(self, metrics: Dict, new_intent: Intent,
                          active_intents: List[Intent],
                          samples: List[Optional[float]],
                          trajectory: List[Dict], hard_failure: bool,
                          reconnection_time: float,
                          measurement_invalid: bool = False,
                          failed_ues: Optional[List[str]] = None,
                          recovery_pending: bool = False,
                          throughput_unknown: bool = False) -> Dict:
        """Assemble the S4 validation dict from the polled window."""
        # P0-9: S4 validation uses the SAME authoritative joint evaluation as
        # the already-satisfied shortcut, over this single polled bundle.
        joint = self.evaluate_joint_intent_set(new_intent, active_intents,
                                               metrics)
        verdicts = dict(joint["verdicts"])   # per-intent typed verdict values
        results = {iid: (v == MonitorVerdict.SATISFIED.value)
                   for iid, v in verdicts.items()}
        # only a positively SATISFIED verdict on EVERY intent passes S4;
        # VIOLATED and UNKNOWN both fail closed (UNKNOWN is never success).
        all_satisfied = bool(joint["joint_satisfied"])
        if hard_failure:
            all_satisfied = False   # a lost UE can never validate a trial
        if measurement_invalid:
            # fail-closed: an unverifiable window can never validate a trial
            # (the trial rolls back), but this is a telemetry failure, NOT a
            # session loss - it stays out of the C_hard cost samples
            all_satisfied = False
        vals = [s for s in samples if s is not None]
        # Batch F (P0-13, review A): ANY authoritative IN_WINDOW UNKNOWN (a
        # transient one too) OR no valid sample at all -> the window CANNOT claim
        # satisfaction (fail closed). A cache never substitutes here.
        if throughput_unknown or not vals:
            all_satisfied = False
        return {
            "all_satisfied": all_satisfied,
            "intent_results": results,
            "monitor_verdicts": verdicts,
            "metrics": {ue_id: m.to_dict() for ue_id, m in metrics.items()},
            "trajectory": trajectory,
            # Batch F (P0-13): NO valid samples -> None (UNKNOWN), never 0.0. A
            # missing/UNKNOWN throughput must not read as a real 0 Mbps.
            "tput_min": (min(vals) if vals else None),
            "tput_after": (vals[-1] if vals else None),
            # Batch F (review #2): TRUE for a transient UNKNOWN (OK,UNKNOWN,OK)
            # too - not only when there are zero valid samples.
            "throughput_unknown": bool(throughput_unknown or not vals),
            "hard_failure": hard_failure,
            "reconnection_time": reconnection_time,
            "measurement_invalid": measurement_invalid,
            "failed_ues": list(failed_ues or []),
            "recovery_pending": recovery_pending,
        }

    @staticmethod
    def _snap_trajectory(metrics: Dict) -> Dict:
        """One trajectory point: per-UE attachment + throughput."""
        return {ue_id: {"attached": bool(getattr(m, "attached", False)),
                        "throughput_mbps": getattr(m, "throughput_mbps", None)}
                for ue_id, m in metrics.items()}

    def _check_physical_recovery(self, failed_ues: List[str],
                                 first_write_time: Optional[float] = None,
                                 rollback_complete_time: Optional[float] = None
                                 ) -> Dict:
        """P0-3: measure PHYSICAL service recovery UNDER the RESTORED config,
        AFTER the rollback - kept SEPARATE from config-restore verification.

        RECOVERED iff every failed UE re-attaches AND reports a finite KPI
        within hard_failure_cap_s of the restore; NOT_RECOVERED if the cap
        elapses first; UNKNOWN when recovery cannot be observed (no failed
        UEs / simulation / collector error).  UNKNOWN is never success.
        """
        verdict = PhysicalRecoveryVerdict.UNKNOWN
        failed = list(failed_ues or [])
        recovery_start = time.time()
        result = {
            "physical_recovery_verdict": verdict.value,
            "reconnection_time": 0.0,
            "recovery_start_time": recovery_start,
            "recovery_complete_time": recovery_start,
            "first_write_time": first_write_time,
            "rollback_complete_time": rollback_complete_time,
        }
        if not failed or getattr(self.ue_collector, "simulation_mode", False):
            # nothing observable -> UNKNOWN (never promoted to success)
            return result

        cap = float(getattr(self, "hard_failure_cap_s", 30.0))
        poll = max(0.05, cap / 20.0)

        def _recovered(m) -> bool:
            # recovery under the restored config = re-attached AND a usable KPI
            return (m is not None and getattr(m, "attached", False)
                    and self._finite(getattr(m, "throughput_mbps", None)))

        reattach_t: Optional[float] = None
        observed = False        # did we ever get a usable observation at all?
        while True:
            try:
                metrics = self.ue_collector.collect_all()
                self._merge_throughput(metrics)
            except Exception as e:
                logger.debug(f"recovery sample failed: {e}")
                metrics = {}
            if metrics and any(u in metrics for u in failed):
                observed = True
            if metrics and all(_recovered(metrics.get(u)) for u in failed):
                reattach_t = time.time()
                verdict = PhysicalRecoveryVerdict.RECOVERED
                break
            elapsed = time.time() - recovery_start
            if elapsed >= cap:
                # observed still-detached -> NOT_RECOVERED; never observable
                # (collector error / absent) -> UNKNOWN (never success)
                verdict = (PhysicalRecoveryVerdict.NOT_RECOVERED if observed
                           else PhysicalRecoveryVerdict.UNKNOWN)
                break
            time.sleep(min(poll, max(0.0, cap - elapsed)))

        now = time.time()
        result["physical_recovery_verdict"] = verdict.value
        result["recovery_complete_time"] = reattach_t or now
        result["reconnection_time"] = min(
            cap, (reattach_t or now) - recovery_start)
        return result

    def _rollback(self, snapshot: Dict) -> Optional[bool]:
        """Deterministic rollback to the pre-trial configuration (low level).

        Returns the VERIFIED restore outcome (P7): True only when the
        post-restore device read-back audit matched the snapshot on every
        axis; False when the audit did NOT match; None when no verification
        applies (simulation / no snapshot).

        Batch B: this is the low-level restore primitive.  It does NOT decide
        latch/verdict policy - the latch, typed verdict, exposure timing and
        fail-closed classification live in _run_rollback_transaction(), so
        BOTH the S4 transaction and a partial-apply failure share one policy.
        """
        self._log_gui("Rolling back configuration...")
        if self.ue_collector.simulation_mode:
            return None
        if not snapshot:
            logger.warning("rollback requested with empty snapshot")
            return None
        ok = self.executor.restore(snapshot)
        if not ok:
            self._log_gui("ROLLBACK FAILED - latching TechnicalFailsafe")
        return bool(ok)

    # ------------------------------------------------------------------
    # Batch B safety transaction: latch + typed rollback (P0-1 / P0-2)
    # ------------------------------------------------------------------

    def _is_latched(self) -> bool:
        """True iff the coordinator is in the LATCHED_FAILSAFE safety state."""
        return getattr(self, "safety_state", SafetyState.READY) is (
            SafetyState.LATCHED_FAILSAFE)

    def _latch_failsafe(self, audit: Dict) -> None:
        """Engage the persistent safety latch (P0-1).

        Sets SafetyState.LATCHED_FAILSAFE, records the audit bundle (failed
        axis, requested action, observed readback, restore error, baseline
        snapshot), latches the executor so DIRECT writes are also refused, and
        pins the FSM in S_TECHNICAL_FAILSAFE (it must NOT return to S0).
        Idempotent: a second call keeps the first audit record.
        """
        already = self._is_latched()
        self.safety_state = SafetyState.LATCHED_FAILSAFE
        if not already:
            self._safety_latch = dict(audit or {})
        executor = getattr(self, "executor", None)
        if executor is not None and hasattr(executor, "latch_failsafe"):
            try:
                executor.latch_failsafe(audit.get("restore_error")
                                        or "rollback unverified")
            except Exception as e:
                logger.error(f"executor latch failed: {e}")
        try:
            self._transition(S_TECHNICAL_FAILSAFE)
        except Exception as e:
            # a state callback must never prevent the latch itself
            logger.error(f"failsafe transition callback failed: {e}")

    def _get_txn(self) -> "ActuationTransaction":
        """The active transaction context, lazily created (direct _execute_trial
        callers - e.g. unit tests - get a fresh one)."""
        tx = getattr(self, "_active_txn", None)
        if tx is None:
            tx = self._active_txn = ActuationTransaction()
        return tx

    def _audit_from_tx(self, tx: "ActuationTransaction",
                       failed_axis: Optional[str] = None) -> Dict:
        """Build the audit_extra bundle for a rollback from the transaction
        (Gate-review item 8): the requested action, applied axes, and failed
        axis - populated from the ACTUAL transaction, not left None."""
        # ALIAS ISOLATION (coordinator correction #6 item 1): the audit bundle
        # is published on a public rollback record; deep-copy the (possibly
        # nested) requested/applied structures so a downstream public mutation
        # cannot reach back into the trusted tx provenance.
        return {
            "failed_axis": failed_axis or tx.failed_axis,
            "requested_action": copy.deepcopy(dict(tx.requested_action or {})),
            "applied_action": copy.deepcopy(list(tx.applied or ())),
            # P0-8 (review bug 2): preserve the re-entry provenance from the
            # TRUSTED transaction copy (not the mutable self._active_trigger_
            # context / result), so the audit reflects what was bound at tx
            # creation. Empty for a direct (non-re-entry) episode.
            "trigger_context": copy.deepcopy(dict(
                getattr(tx, "trigger_context", {}) or {})),
        }

    def _stash_cycle_audit(self, cycle: Dict, tx: "ActuationTransaction") -> None:
        """Persist the transaction's FULL action/snapshot/identity provenance
        onto the cycle so EVERY terminal record - including a rollback/exception
        one, even when _execute_trial raised before returning - carries the
        trial id, rebound authorization, canonical action + hash,
        snapshot/action timestamps, and requested/applied action (P0-6,
        coordinator review C4).

        ALIAS ISOLATION (coordinator review #5 item 2): every field copied onto
        the (public, mutable) cycle is an INDEPENDENT deep copy, so a public
        mutation of cycle['snapshot'] / requested / applied / canonical /
        authorization can NEVER mutate the trusted transaction fields."""
        cycle["snapshot"] = copy.deepcopy(tx.snapshot)
        cycle["requested_action"] = copy.deepcopy(dict(tx.requested_action or {}))
        cycle["applied_action"] = copy.deepcopy(list(tx.applied or ()))
        # identity + canonical binding (survive rollback/exception terminals)
        if getattr(tx, "actuation_trial_id", None):
            cycle["actuation_trial_id"] = tx.actuation_trial_id
        if getattr(tx, "authorization", None) is not None:
            try:
                cycle["authorization"] = tx.authorization.to_dict()
            except Exception:
                pass
        if getattr(tx, "canonical_action", None):
            cycle["canonical_action"] = copy.deepcopy(dict(tx.canonical_action))
        if getattr(tx, "canonical_action_hash", ""):
            cycle["canonical_action_hash"] = tx.canonical_action_hash
        cycle["snapshot_readback_time"] = getattr(tx, "snapshot_readback_time",
                                                  None)
        cycle["action_apply_time"] = getattr(tx, "action_apply_time", None)
        cycle["action_apply_monotonic_s"] = getattr(
            tx, "action_apply_monotonic_s", None)
        # re-entry provenance (review gap 2): EVERY actuated terminal cycle -
        # including a rollback/exception one - must carry the trusted re-entry
        # context from the TRANSACTION (never self/result). Set it from a deep
        # copy when present, remove any stale/forged value when the tx has none.
        _tc = copy.deepcopy(dict(getattr(tx, "trigger_context", {}) or {}))
        if _tc:
            cycle["trigger_context"] = _tc
        else:
            cycle.pop("trigger_context", None)
        report = getattr(getattr(self, "executor", None), "last_restore_report",
                         None)
        if report:
            try:
                cycle["restore_readback"] = copy.deepcopy(report)
            except Exception:
                cycle["restore_readback"] = report

    def _run_rollback_transaction(self, snapshot: Dict, cycle: Optional[Dict],
                                  *, first_write_time: Optional[float] = None,
                                  actual_real_write: bool = False,
                                  audit_extra: Optional[Dict] = None
                                  ) -> RollbackOutcome:
        """The SINGLE rollback path (P0-1 step 7 / P0-2): restore + typed
        verdict + exposure timing + fail-closed latch.

        Latch policy (Gate-review item 3): a FAILED restore always latches; an
        UNKNOWN restore latches too IFF a REAL device write happened
        (``actual_real_write``), because an unverified restore after actuation
        cannot be trusted. A simulation / no-write UNKNOWN does NOT latch.
        """
        self.safety_state = SafetyState.RESTORING
        rb_start = time.time()
        _rb_mono0 = time.monotonic()      # Batch G (P1-3): rollback-latency timer
        restore_ok: Optional[bool]
        err: Optional[str] = None
        # ALIAS ISOLATION (coordinator review #5 item 2): hand _rollback (and
        # thus a possibly-mutating executor.restore) an INDEPENDENT deep copy, so
        # the trusted snapshot/evidence can never be corrupted mid-restore.
        try:
            rollback_snapshot = copy.deepcopy(snapshot)
        except Exception:
            rollback_snapshot = snapshot
        try:
            restore_ok = self._rollback(rollback_snapshot)
        except Exception as e:                     # a raising restore is FAILED
            restore_ok = False
            err = str(e)
            logger.error(f"rollback raised: {e}")
        rb_done = time.time()
        # Batch G (P1-3): record the measured rollback latency on the cycle (the
        # restore ran regardless of verdict, so this is always meaningful here).
        if cycle is not None:
            cycle["rollback_ms"] = (time.monotonic() - _rb_mono0) * 1000.0
        verdict = ConfigRestoreVerdict.from_restore_bool(restore_ok)
        # item 3: FAILED always latches; UNKNOWN latches iff a real write ran.
        latched = (verdict is ConfigRestoreVerdict.FAILED
                   or (verdict is ConfigRestoreVerdict.UNKNOWN
                       and actual_real_write))
        # capture the executor's post-restore read-back report for audit (item 8)
        readback = None
        try:
            rep = getattr(getattr(self, "executor", None),
                          "last_restore_report", None)
            readback = copy.deepcopy(rep) if rep else rep
        except Exception:
            readback = None
        outcome = RollbackOutcome(
            config_restore_verdict=verdict,
            restore_verified=restore_ok,
            latched=latched,
            first_write_time=first_write_time,
            rollback_start_time=rb_start,
            restore_complete_time=rb_done,
            tau_exposure_to_rollback=(rb_start - first_write_time
                                      if first_write_time is not None else None),
            tau_exposure_to_restore=(rb_done - first_write_time
                                     if first_write_time is not None else None),
            error=err,
        )
        if cycle is not None:
            cycle["rolled_back"] = True
            if restore_ok is not None:
                cycle["restore_verified"] = bool(restore_ok)
            cycle["config_restore_verdict"] = verdict.value
            cycle["rollback_result"] = outcome.to_dict()
            if readback:
                cycle["restore_readback"] = readback
        if latched:
            extra = dict(audit_extra or {})
            audit = {
                "failed_axis": extra.get("failed_axis"),
                "requested_action": extra.get("requested_action"),
                "applied_action": extra.get("applied_action"),
                "observed_readback": readback,
                "config_restore_verdict": verdict.value,
                "restore_error": err or "restore read-back mismatch/unverified",
                "baseline_snapshot": snapshot,
                # re-entry provenance (review gap 3): carry the trusted trigger
                # context (deep-copied from _audit_from_tx's tx copy) into the
                # latch audit, so the re-entry reason / origin evidence survives
                # a restore-unverified TechnicalFailsafe latch.
                "trigger_context": copy.deepcopy(
                    dict(extra.get("trigger_context") or {})),
            }
            self._latch_failsafe(audit)
        else:
            self.safety_state = SafetyState.READY
        return outcome

    def clear_safety_latch(self, recovery_target: Optional[Dict] = None
                           ) -> RecoveryClearanceResult:
        """Operator recovery API (P0-1 step 6): the ONLY way to release the
        latch.  Verifies the current device readback against the recovery /
        baseline target by re-running a verified restore to it; the latch is
        released ONLY on a VERIFIED match.  UNKNOWN / FAILED keep it engaged
        (UNKNOWN is never success).  A no-op when not latched.
        """
        if not self._is_latched():
            return RecoveryClearanceResult(
                cleared=False, verdict=ConfigRestoreVerdict.UNKNOWN,
                detail="not latched")
        latch = self._safety_latch or {}
        target = (recovery_target if recovery_target is not None
                  else latch.get("baseline_snapshot"))
        if not target:
            return RecoveryClearanceResult(
                cleared=False, verdict=ConfigRestoreVerdict.UNKNOWN,
                detail="no recovery/baseline target to verify against")
        executor = getattr(self, "executor", None)
        if executor is None or not hasattr(executor, "recover_and_clear"):
            return RecoveryClearanceResult(
                cleared=False, verdict=ConfigRestoreVerdict.UNKNOWN,
                detail="no executor recover_and_clear available")
        # item 5: delegate to executor.recover_and_clear, which re-applies the
        # target AND FULLY re-reads every applicable target axis (not only the
        # diff-touched ones), then clears the EXECUTOR latch IFF that full
        # verification passes. An exception leaves BOTH latches engaged.
        try:
            executor_cleared = bool(executor.recover_and_clear(target))
        except Exception as e:
            logger.error(f"executor recover_and_clear raised: {e}")
            return RecoveryClearanceResult(
                cleared=False, verdict=ConfigRestoreVerdict.FAILED,
                detail=f"executor clearance raised: {e}")
        if not executor_cleared:
            # full read-back did not verify: executor latch stays engaged, so
            # the coordinator latch MUST stay engaged too (atomic, item 5).
            return RecoveryClearanceResult(
                cleared=False, verdict=ConfigRestoreVerdict.FAILED,
                detail="full read-back did not verify against target")
        # executor latch is ACTUALLY cleared now -> release the coordinator
        # latch and return the FSM to S0 (READY only after the executor cleared).
        self.safety_state = SafetyState.READY
        self._safety_latch = None
        # PRIVILEGED exit from the TechnicalFailsafe sink after verified
        # recovery (the ONLY legal way out - _transition('S0') would be refused).
        try:
            self._force_state(FSMState.S0)
        except Exception as e:
            logger.error(f"post-recovery S0 transition failed: {e}")
        return RecoveryClearanceResult(
            cleared=True, verdict=ConfigRestoreVerdict.VERIFIED,
            detail="full read-back verified against target")

    def _negotiate(self, new_intent: Intent, alternatives: List[Alternative],
                   max_rounds: Optional[int] = None) -> Dict:
        """Round-based candidate SELECTION for one negotiation (S5).

        Each round consults the negotiation policy on ONE candidate
        alternative; rejected candidates feed the next round's regeneration.
        The deterministic monotonic-relaxation rule (Section III) discards
        any alternative that relaxes LESS than the previous round's
        candidate (one regeneration attempt per round; a second violation
        terminates).

        Budget (Codex acceptance #3): `max_rounds` is the episode's
        REMAINING frozen negotiation budget - process_intent passes it so
        every cycle's negotiation draws from ONE shared N_max (Eq. 9); the
        total policy consultations per episode can never exceed N_max.
        None (standalone use) freezes the calibrator's current N_max.

        C1 contract: this method only SELECTS - an accepted alternative is
        an agreement, not a resolution. The caller must materialize its
        modified intent and re-execute (S2 -> S3 -> S4); episode success
        exists only after S4 validation.

        Returns {"accepted": Alternative|None, "stats"} where stats =
        {rounds, duration_s, tput_during_nego, terminated_by} and
        terminated_by in {"accept", "n_max", "no_alternative", "reject"}
        (the caller may overwrite it with "unmaterializable" when the
        agreement cannot be re-executed).
        """
        if max_rounds is None:
            get_n_max = getattr(self.calibrator, "get_n_max", None)
            max_rounds = (int(get_n_max()) if callable(get_n_max)
                          else int(getattr(self.calibrator, "n_max", 0)))
        rounds = 0
        # monotonicity anchor: the last ENGAGED candidate that carried a
        # numeric target (persists across rounds so omitting target_value
        # cannot reset the guard)
        prev_ctype = None
        prev_target: Optional[float] = None
        rejected_history: List[Alternative] = []
        tput_samples: List[float] = []
        nego_samples: List = []      # Batch F: the fresh per-round NEGOTIATION probes
        accepted: Optional[Alternative] = None
        terminated_by: Optional[str] = None
        # perf_counter: sub-ms negotiations must yield a nonzero duration
        # (time.time() granularity on Windows can round them to exactly 0)
        t0 = time.perf_counter()
        gen = self.generate_alternatives_fn or self._generate_alternatives
        # P0-7 (coordinator review item 3): EVERY generator invocation - the
        # built-in AND a custom generate_alternatives_fn (invoked inline) - is
        # bounded by min(operation timeout, remaining episode time), so a
        # non-returning custom generator cannot defeat the episode absolute
        # deadline.  OperationTimeout propagates into the typed episode outcome.
        gen_timeout = float(getattr(self, "model_call_timeout_s", 30.0))

        def _call_gen():
            return self._bounded_call(
                lambda: gen(new_intent, rejected_history), gen_timeout,
                label="alternative generator") or []

        while rounds < max_rounds:
            if rounds == 0 and alternatives:
                alts = list(alternatives)
            else:
                alts = _call_gen()
            alt = next((a for a in alts if a is not None), None)
            if alt is None:
                terminated_by = "no_alternative"
                break

            # Batch F (P0-13): one FRESH structured NEGOTIATION probe per engaged
            # round (under the shared scheduler), taken before the monotonic
            # guard so a round aborted by re-violation still contributes. A probe
            # failure / lock timeout is an explicit UNKNOWN carried to the ledger
            # / evidence / raw - never a cached value and never silently 0.
            _nscope = sorted(getattr(getattr(new_intent, "scope", None),
                                     "ue_ids", None) or [])
            nsample = self._measure_throughput(
                MeasurementPurpose.NEGOTIATION, label=f"nego_round_{rounds}",
                ue_scope=_nscope)
            nego_samples.append(nsample)
            if not nsample.is_unknown():
                tput_samples.append(nsample.value)

            if not self._is_monotonic_relaxation(prev_ctype, prev_target, alt):
                # discard + one regeneration attempt within the same round
                self._log_gui(f"Alternative '{alt.id}' violates monotonic "
                              f"relaxation - discarding and regenerating")
                rejected_history.append(alt)
                regen = _call_gen()
                alt = next((a for a in regen if a is not None), None)
                if alt is None:
                    terminated_by = "no_alternative"
                    break
                if not self._is_monotonic_relaxation(prev_ctype, prev_target,
                                                     alt):
                    # aborted before any policy consultation: not counted in
                    # `rounds` (rounds = policy consultations, per Eq. 9)
                    terminated_by = "reject"
                    break

            decision = self._negotiation_decision(alt)
            rounds += 1
            if decision == "accept":
                accepted = alt
                terminated_by = "accept"
                break
            rejected_history.append(alt)
            ct, v = self._relaxation_target(alt)
            if v is not None:
                prev_ctype, prev_target = ct, v

        if terminated_by is None:
            terminated_by = "n_max"   # while-condition exhausted the budget

        # Batch F (review #3): if ANY engaged round's probe was UNKNOWN, the
        # AGGREGATE negotiation cost is UNMEASURED - a partial mean over only the
        # OK rounds would EXTRAPOLATE across the missing ones. tput_during_nego is
        # then None (ledger settles UNKNOWN); the per-round OK/UNKNOWN samples are
        # preserved for the valid subset and evidence.
        any_nego_unknown = any(s.is_unknown() for s in nego_samples)
        nego_unmeasured = any_nego_unknown or not tput_samples
        stats = {
            "rounds": rounds,
            "duration_s": time.perf_counter() - t0,
            "tput_during_nego": (None if nego_unmeasured
                                 else sum(tput_samples) / len(tput_samples)),
            "tput_during_nego_unknown": nego_unmeasured,
            "measurement_samples": [s.to_dict() for s in nego_samples],
            # Batch F (#d8b9): timestamped [(sample_time, value)] negotiation
            # trajectory from the OK per-round probes (UNKNOWN rounds excluded
            # from integration but retained as explicit samples above).
            "trajectory": [(s.sample_time, s.value) for s in nego_samples
                           if not s.is_unknown() and s.value is not None],
            "terminated_by": terminated_by,
        }

        if accepted is not None:
            self._log_gui(f"Negotiation accepted alternative: "
                          f"{accepted.description}")
        return {"accepted": accepted, "stats": stats}

    def _negotiation_decision(self, alt: Alternative) -> str:
        """Consult the per-round DECISION-BEARING policy (callback takes
        precedence).

        Batch B callback isolation: an unknown policy response is NEVER
        silently accepted. Only an explicit ``"accept"`` accepts; every other
        value (incl. ``None`` / an unrecognized string) fails CLOSED to
        ``"reject"`` on BOTH the callback and the policy path, so a relaxation
        the operator did not clearly approve is never actuated.
        """
        # P0-7: the decision-bearing callback speaks the strict PolicyResponse
        # enum.  Only the exact recognized values ("accept"/"reject") are
        # honoured; an UNKNOWN / corrupt response (an unrecognized string, None,
        # a wrong type) is a broken system contract -> raise UnknownSystemEvent
        # (the caller fails the episode closed to TechnicalFailsafe with NO
        # actuation).  The callback is bounded by the remaining episode time, so
        # a hung policy cannot defeat the wall-clock bound.
        timeout = float(getattr(self, "policy_call_timeout_s", 15.0))
        if self.on_negotiation_needed:
            raw = self._bounded_call(
                lambda: self.on_negotiation_needed([alt]), timeout,
                label="negotiation policy callback")
        else:
            policy = self.negotiation_policy or auto_accept_first
            raw = self._bounded_call(lambda: policy(alt), timeout,
                                     label="negotiation policy")
        if not PolicyResponse.is_recognized(raw):
            raise UnknownSystemEvent(
                f"unknown negotiation policy response {raw!r} "
                f"(expected 'accept' or 'reject')")
        return PolicyResponse.parse(raw).value

    @staticmethod
    def _relaxation_target(alt: Optional[Alternative]):
        """(constraint_type, target_value) of an alternative's relaxed intent,
        or (None, None) when it carries no comparable target."""
        mi = getattr(alt, "modified_intent", None)
        if mi is None or mi.target is None:
            return None, None
        return mi.target.constraint_type, float(mi.target.target_value)

    @staticmethod
    def _is_monotonic_relaxation(prev_ctype, prev_target: Optional[float],
                                 alt: Alternative) -> bool:
        """Section-III deterministic rule: successive alternatives must relax
        at least as far as the anchor - the last engaged candidate with a
        numeric target (MIN constraint: target_k <= anchor; MAX: >= anchor).

        The guard is a safety property, so once an anchor exists a candidate
        WITHOUT a numeric target is unverifiable and FAILS (an LLM cannot
        bypass the rule by omitting target_value). With no anchor yet
        (e.g. round 1) any candidate passes."""
        if prev_target is None:
            return True
        _, v = IntentCoordinator._relaxation_target(alt)
        if v is None:
            return False
        if prev_ctype == ConstraintType.MAX:
            return v >= prev_target
        return v <= prev_target   # MIN and default

    @staticmethod
    def _mean_tput_of(metrics: Dict) -> Optional[float]:
        """Mean throughput across UEMetrics values (None if no data)."""
        vals = [m.throughput_mbps for m in metrics.values()
                if getattr(m, "throughput_mbps", None) is not None]
        return sum(vals) / len(vals) if vals else None

    def _mean_ue_throughput(self) -> Optional[float]:
        """One KPI sample: mean throughput across UEs (None if unavailable)."""
        try:
            metrics = self.ue_collector.collect_all()
        except Exception as e:
            logger.debug(f"negotiation KPI sample failed: {e}")
            return None
        return self._mean_tput_of(metrics)

    # ------------------------------------------------------------------
    # Batch F (P0-13/P0-14): the live-measurement API. Every throughput sample
    # is a validated MeasurementSample with provenance + an EPOCH sample_time,
    # taken under the SHARED scheduler; a probe failure / lock timeout / non-
    # saturating capacity request is an EXPLICIT UNKNOWN, never a cached 0.
    # ------------------------------------------------------------------

    def _baseline_sample(self, tp_before) -> MeasurementSample:
        """Wrap the S3 pre-action baseline as a PRE_ACTION MeasurementSample: a
        finite value -> OK; a missing/non-finite baseline -> explicit UNKNOWN
        (value=None), so it is EXCLUDED from calibration/cost deltas, never 0."""
        cfg = self._probe_cfg()
        common = dict(metric="ue_throughput_mbps",
                      purpose=MeasurementPurpose.PRE_ACTION,
                      source=f"iperf3_{cfg.protocol}_{cfg.direction}",
                      direction=cfg.direction, duration_s=cfg.duration_s,
                      probe_config=cfg.to_dict(), cache_status="live",
                      freshness="fresh")
        st = self._epoch_now()
        pid = new_probe_id()
        if isinstance(tp_before, (int, float)) and not isinstance(
                tp_before, bool) and math.isfinite(float(tp_before)):
            return MeasurementSample.ok(value=float(tp_before), sample_time=st,
                                        probe_id=pid, **common)
        return MeasurementSample.unknown(
            sample_time=st, probe_id=pid,
            error="no pre-action throughput baseline measured", **{
                **common, "freshness": "unknown"})

    def baseline_value_or_none(self, tx) -> Optional[float]:
        """The pre-action baseline value ONLY when it is a real OK sample; None
        (UNKNOWN) otherwise - callers must exclude UNKNOWN from cost deltas."""
        b = getattr(tx, "pre_action_baseline", None)
        if isinstance(b, dict) and b.get("status") == "ok":
            v = b.get("value")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
        return None

    def set_probe_config(self, probe_config) -> None:
        """Install the explicit ProbeConfig on the coordinator AND the collector
        FAIL-CLOSED and ATOMICALLY (coordinator review): a validated ProbeConfig
        is REQUIRED; the collector MUST expose a callable set_probe_config; the
        config is propagated to the collector FIRST and self._probe_config is
        updated ONLY after the collector accepts it. On ANY failure the previous
        coordinator + collector config is PRESERVED (no partial mismatch) and the
        error PROPAGATES - so metadata can never claim a config the probe does
        not actually use."""
        if not isinstance(probe_config, ProbeConfig):
            raise MeasurementError(
                f"probe_config must be a ProbeConfig, got "
                f"{type(probe_config).__name__}")
        collector = getattr(self, "ue_collector", None)
        setter = getattr(collector, "set_probe_config", None)
        if not callable(setter):
            raise RuntimeError(
                "collector does not expose a callable set_probe_config; "
                "refusing to install a probe config the collector cannot apply "
                "(would cause a metadata/probe mismatch)")
        prev_collector = getattr(collector, "probe_config", None)
        try:
            setter(probe_config)                     # collector FIRST
        except Exception as e:
            # restore the collector's previous config (best-effort) so no
            # partial state survives; self._probe_config is UNCHANGED.
            try:
                setter(prev_collector)
            except Exception:
                pass
            raise RuntimeError(
                f"collector rejected the probe config (no change applied): {e}")
        self._probe_config = probe_config            # only after success

    def _probe_cfg(self) -> ProbeConfig:
        cfg = getattr(self, "_probe_config", None)
        return cfg if isinstance(cfg, ProbeConfig) else ProbeConfig()

    def _epoch_now(self) -> float:
        """EPOCH clock (injectable), skeleton-safe (defaults to time.time)."""
        fn = getattr(self, "_now", None)
        return fn() if callable(fn) else time.time()

    def _run_throughput_probe(self) -> Dict[str, float]:
        """Invoke the underlying throughput probe (injectable for tests;
        defaults to the collector's live per-UE iperf probe)."""
        fn = getattr(self, "_throughput_probe_fn", None)
        if fn is None:
            fn = self.ue_collector.get_throughput_all
        return fn(self._probe_cfg().duration_s) or {}

    def _measure_throughput(self, purpose, *, action_apply_time=None,
                            ue_scope=None, label: str = "probe",
                            freshness: str = "fresh") -> MeasurementSample:
        """Take ONE throughput MeasurementSample (see _measure_throughput_
        detailed). Returns just the aggregate sample."""
        return self._measure_throughput_detailed(
            purpose, action_apply_time=action_apply_time, ue_scope=ue_scope,
            label=label, freshness=freshness)[0]

    def _measure_throughput_detailed(self, purpose, *, action_apply_time=None,
                                     ue_scope=None, label: str = "probe",
                                     freshness: str = "fresh"):
        """Take ONE throughput sample and RETURN ``(MeasurementSample,
        per_ue_dict)``. The per-UE dict is returned (never stashed on shared
        state) so concurrent background + trial probes cannot cross-contaminate.

        Fail-closed to an explicit UNKNOWN (value=None + error, never 0/NaN) on:
        a non-saturating CAPACITY request, a shared-scheduler lock timeout, a
        probe exception, or no usable per-UE data. The EPOCH ``sample_time`` is
        stamped AFTER the probe completes (so an in-window sample is naturally
        post-action), in the SAME clock domain as action_apply_time."""
        cfg = self._probe_cfg()
        purpose = MeasurementPurpose.coerce(purpose)
        pid = new_probe_id()
        scope = tuple(ue_scope or ())
        # Live measurement fix: with a background offered load active, measure the
        # KPI PASSIVELY (tun rx_bytes delta) so it can't collide with the load on
        # the UE's single iperf3 server (that collision read a spurious 0). The
        # source name is honest about which path produced the value.
        passive = self._use_passive_goodput()
        source = ("tun_rx_bytes_goodput_downlink" if passive
                  else f"iperf3_{cfg.protocol}_{cfg.direction}")
        common = dict(metric="ue_throughput_mbps", purpose=purpose,
                      source=source,
                      direction=cfg.direction, duration_s=cfg.duration_s,
                      ue_scope=scope, probe_config=cfg.to_dict())
        # CAPACITY mode requires a saturating offered load - else it may NOT be
        # reported as a capacity measurement (fail-closed, P0-14).
        floor = float(getattr(self, "capacity_saturation_floor_mbps", 100.0))
        if not capacity_offered_load_ok(cfg, floor):
            return (MeasurementSample.unknown(
                sample_time=self._epoch_now(), probe_id=pid,
                error=(f"non-saturating offered load {cfg.offered_load_mbps} "
                       f"Mbps < capacity floor {floor} Mbps"),
                cache_status="live", freshness="unknown", **common), {})
        sched = getattr(self, "_measurement_scheduler", None)
        try:
            if passive:
                # NO shared lock: the passive counter read injects nothing, so it
                # cannot collide with the load - and it MUST let the load keep
                # flowing during the window (holding the lock would pause it).
                data = self._run_goodput_probe()
            elif sched is not None:
                to = float(getattr(self, "probe_lock_timeout_s",
                                   sched.default_timeout_s))
                with sched.probe(label=label, timeout_s=to):
                    data = self._run_throughput_probe()
            else:
                data = self._run_throughput_probe()
        except MeasurementBusy as e:
            return (MeasurementSample.unknown(
                sample_time=self._epoch_now(), probe_id=pid, error=str(e),
                cache_status="live", freshness="unknown", **common), {})
        except Exception as e:
            return (MeasurementSample.unknown(
                sample_time=self._epoch_now(), probe_id=pid,
                error=f"probe failed: {e}", cache_status="live",
                freshness="unknown", **common), {})
        # the EXACT per-UE probe data (authoritative; NOT a collect_all cache).
        per_ue = {k: float(v) for k, v in (data or {}).items()
                  if isinstance(v, (int, float)) and not isinstance(v, bool)
                  and math.isfinite(float(v))}
        st = self._epoch_now()    # EPOCH, stamped AFTER the probe (post-action)
        if not per_ue:
            return (MeasurementSample.unknown(
                sample_time=st, probe_id=pid,
                error="no usable per-UE throughput", cache_status="live",
                freshness="unknown", **common), {})
        # Batch F (review #87d28.2): an IN_WINDOW sample whose epoch is BEFORE
        # the trusted action_apply_time (a regressed/injected clock) is STALE,
        # not fresh - so it can never validate the window or a commit.
        eff_fresh = freshness
        if (purpose == MeasurementPurpose.IN_WINDOW
                and action_apply_time is not None):
            try:
                if st < float(action_apply_time):
                    eff_fresh = "stale"
            except (TypeError, ValueError):
                eff_fresh = "stale"
        # Batch F (review #87d28.3): the OK sample carries the EXACT MEASURED UE
        # scope (the per-UE keys the probe actually returned), not ().
        ok_common = dict(common)
        ok_common["ue_scope"] = tuple(sorted(per_ue.keys()))
        return (MeasurementSample.ok(
            value=sum(per_ue.values()) / len(per_ue), sample_time=st,
            probe_id=pid, cache_status="live", freshness=eff_fresh,
            **ok_common), per_ue)

    def _generate_alternatives(self, new_intent: Intent,
                               rejected_history: List[Alternative]
                               ) -> List[Alternative]:
        """LLM-backed alternative generation for rounds beyond the first.

        Uses the SAME twin-excluded context as the cycle (C1 x C7): the
        regenerated alternatives must not treat the intent's own original
        target as a separate constraint to preserve.
        """
        try:
            active = [i.to_dict()
                      for i in self._coordination_context(new_intent)]
            failed = new_intent.to_dict()
            failed["rejected_alternatives"] = [
                {"id": a.id, "description": a.description,
                 "target_value": self._relaxation_target(a)[1]}
                for a in rejected_history]
            state = self._get_network_state()
            # P0-7: bound the alternative-generation model call by the remaining
            # episode time.
            response = self._bounded_call(
                lambda: self._pinned_generate_alternatives(
                    active, failed, getattr(state, "ue_states", {}) or {}),
                float(getattr(self, "model_call_timeout_s", 30.0)),
                label="alternative-generation model call")
        except OperationTimeout:
            raise
        except Exception as e:
            logger.warning(f"alternative generation failed: {e}")
            return []
        if not getattr(response, "success", False):
            return []
        # P0-5 (coordinator review): STRICT-parse the RAW response content
        # (duplicate keys / NaN/Infinity / missing/unknown/type-invalid fields)
        # and validate the alternatives BEFORE _alternatives_from_json; a
        # malformed dynamic response records the actual invalid verdict and
        # yields NO executable alternative.
        content = getattr(response, "content", "") or ""
        try:
            source = content if content.strip() else json.dumps(
                response.parsed_json if response.parsed_json is not None else {})
            data = _schema.strict_extract_object(source)
            _schema.validate_alternatives(data)
        except SchemaError as e:
            self._note_schema_invalid(f"alternatives: {e}")
            self._log_gui(f"Dynamic alternatives rejected (schema): {e}")
            return []
        return self._alternatives_from_json(data, new_intent)

    @staticmethod
    def _alternatives_from_json(data: Dict, base_intent: Intent
                                ) -> List[Alternative]:
        """Defensive parse of an LLM 'alternatives' array into Alternative
        objects; entries with a numeric target become a modified_intent so the
        monotonic-relaxation rule can compare them."""
        alts = []
        for i, a in enumerate(data.get("alternatives", []) or []):
            if not isinstance(a, dict):
                continue
            modified = None
            tv = a.get("target_value", a.get("value"))
            if tv is not None:
                try:
                    modified = copy.deepcopy(base_intent)
                    modified.target.target_value = float(tv)
                except (TypeError, ValueError):
                    modified = None
            alts.append(Alternative(
                id=str(a.get("id", f"alt{i + 1}")),
                description=str(a.get("description", "")),
                modified_intent=modified,
                confidence=float(a.get("confidence", 0.5) or 0.5),
            ))
        return alts

    @staticmethod
    def _mean_throughput(ue_states: Dict[str, Dict]) -> Optional[float]:
        """Mean throughput across UE state dicts (key: throughput_mbps)"""
        values = [s.get("throughput_mbps") for s in ue_states.values()
                  if s.get("throughput_mbps") is not None]
        return sum(values) / len(values) if values else None

    def _new_reserve_ledger(self) -> ReserveLedger:
        """Build this episode's reserve ledger from the DETERMINISTIC config
        caps (P0-10/P0-11), tau-scaled. Safe on __new__ test skeletons (getattr
        fallbacks). C_episode follows the calibrator (emulation scales it)."""
        tau = float(getattr(self, "tau_trial_s", 15.0))
        cfg = getattr(getattr(self, "config", None), "calibration", None)
        # The operator's DECLARED per-episode budget is the config C_episode
        # (an absolute Mbps*s cap). The emulation scales calibrator.c_episode by
        # tau/15 only to keep the empirical N_max guard sane at small tau; the
        # deterministic ledger's caps (c_hard_cap etc.) do NOT scale with tau, so
        # the ledger must use the unscaled config budget - else a tiny-tau sim
        # would block every trial. Fall back to the calibrator/500 for __new__
        # test skeletons that have no config.
        if cfg is not None:
            cep = float(getattr(cfg, "c_episode", 500.0))
            cont = float(getattr(cfg, "kpi_loss_cap_mbps", 10.0)) * max(0.0, tau)
            hard = float(cfg.c_hard_ub())      # honest derived hard-failure cap
            nego = float(cfg.c_nego_ub())      # honest derived negotiation cap
            audit = cfg.caps_audit(tau)
        else:
            # test-harness fallback (no config): an unconstrained budget so a
            # __new__ skeleton that is exercising OTHER behavior is never
            # budget-blocked; the deterministic per-unit caps still apply.
            cep = 1e9
            cont, hard, nego = 10.0 * max(0.0, tau), 300.0, 50.0
            audit = {"source": "test_harness_fallback"}
        return ReserveLedger(cep, cont, hard, nego, caps_audit=audit)

    def _settle_negotiation_actual(self, ledger, rid, nstats, target) -> None:
        """Settle a negotiation reservation with its MEASURED actual cost
        (blocker 5): prefer the timestamped trajectory; else the flagged
        duration*finite-deficit fallback = max(0, target - tput_during_nego) *
        duration. A MISSING trajectory AND missing/non-finite tput/target/
        duration is settled UNKNOWN with actual=None and a measurement_missing
        reason - never SETTLED actual=0.0 (that would claim zero cost without
        evidence)."""
        dur = nstats.get("duration_s")
        tput = nstats.get("tput_during_nego")
        traj = nstats.get("trajectory")
        comps = {"duration_s": dur, "tput_during_nego": tput, "target": target}
        # Batch F (review #3): ANY UNKNOWN engaged round makes the AGGREGATE
        # negotiation cost unmeasured - the OK-subset trajectory must NOT be
        # extrapolated across the missing round(s). Settle UNKNOWN (the per-round
        # samples/valid subset are preserved on the stats for evidence).
        if nstats.get("tput_during_nego_unknown"):
            ledger.settle(rid, ledger.UNKNOWN, actual=None,
                          method="measurement_missing", components=comps,
                          error="negotiation cost unmeasured "
                                "(>=1 round UNKNOWN; no extrapolation)")
            return
        actual, method = None, None
        if traj and target is not None and self._finite(target):
            try:
                from calibration.trajectory import trajectory_cost as _tc
                area = _tc(traj, deficit_ref=float(target))
                if area is not None:
                    actual, method = float(area), "trajectory"
            except Exception:
                actual = None
        if actual is None and all(v is not None and self._finite(v)
                                  for v in (tput, target, dur)):
            actual = max(0.0, float(target) - float(tput)) * max(0.0, float(dur))
            method = "duration_deficit"
        if actual is None:
            # no trajectory and no finite duration*deficit -> DO NOT claim 0
            ledger.settle(rid, ledger.UNKNOWN, actual=None,
                          method="measurement_missing", components=comps,
                          error="negotiation cost unmeasured "
                                "(no trajectory/finite deficit)")
            return
        ledger.settle(rid, ledger.SETTLED, actual=actual, method=method,
                      components=comps)

    def _operating_regime_id(self) -> str:
        """The operating-regime id for calibration partitioning (P0-17): the
        current experiment phase, or 'default'."""
        return str(getattr(self, "current_phase", "") or "default")

    def _active_model_id(self) -> str:
        """The proposer/model id for calibration partitioning (P0-17).

        Prefers the PINNED cycle context (P0-16) so a mid-cycle switch of the
        manager's active backend never re-partitions THIS cycle's calibration;
        the next cycle re-pins at its own proposal boundary."""
        ctx = getattr(self, "_proposer_ctx", None)
        if ctx is not None and ctx.model_version:
            return str(ctx.model_version)
        mgr = getattr(self, "llm_manager", None)
        if mgr is not None:
            try:
                name = mgr.active_backend_name()
                if name:
                    return str(name)
            except Exception:
                pass
        return "unknown"

    def _freeze_episode_context(self) -> Tuple[str, str]:
        """FREEZE this episode's (model_id, operating_regime_id) ONCE and select
        the calibrator partition for it (review). If the partition cannot be
        selected, set _calibration_context_ok=False so downstream routing fails
        CLOSED (calibrated=0, no model-dependent trial) rather than silently
        reusing the previously-active partition (cross-contamination)."""
        if self._episode_context is not None:
            return self._episode_context
        pair = (self._active_model_id(), self._operating_regime_id())
        self._episode_context = pair
        sctx = getattr(self.calibrator, "set_operating_context", None)
        if callable(sctx):
            try:
                sctx(pair[0], pair[1])
                self._calibration_context_ok = True
            except Exception as e:
                self._calibration_context_ok = False
                logger.error(f"calibration context selection FAILED for "
                             f"{pair} - failing closed: {e}")
        return pair

    def _frozen_context(self) -> Tuple[str, str]:
        """The frozen (model, regime) pair for this episode (freezing if needed
        so N_max/theta/calibration/proposal/tx/evidence all share ONE pair)."""
        if self._episode_context is None:
            return self._freeze_episode_context()
        return self._episode_context

    @staticmethod
    def _finite01_or_none(value):
        """A finite number in [0,1] else None (blocker 1: out-of-range rejected,
        never clamped)."""
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f) or f < 0.0 or f > 1.0:
            return None
        return f

    def _calibrated_probability(self, raw_conf) -> Tuple[float, str]:
        """Route-time (calibrated_probability, reason) (P0-17 / blocker 1).

        BOTH raw and the calibrator's output are validated as finite [0,1]; an
        invalid raw / invalid output / ANY exception FAILS CLOSED to (0.0, reason)
        - NEVER a raw fallback and never a trial. A calibrator lacking the
        calibration API (e.g. a test FakeCalibrator) uses the raw score as the
        calibrated probability, but still fail-closed on non-finite/out-of-range."""
        cal = getattr(self, "calibrator", None)
        model, regime = self._frozen_context()   # ONE frozen pair per episode
        # RAW PREVALIDATION (review): reject a non-finite / out-of-range raw
        # score BEFORE any calibration call.
        if self._finite01_or_none(raw_conf) is None:
            return 0.0, "fail_closed_invalid_raw"
        # EXACTLY ONE calibration call (review): ``calibrate`` returns
        # (probability, reason) in a single call; a calibrator that only exposes
        # the legacy ``calibrated_probability`` is called once (reason 'mapped').
        cfn = getattr(cal, "calibrate", None)
        if callable(cfn):
            try:
                p, reason = cfn(raw_conf, model, regime)
            except Exception:
                return 0.0, "fail_closed_exception"
        else:
            pfn = getattr(cal, "calibrated_probability", None)
            if callable(pfn):
                try:
                    p, reason = pfn(raw_conf, model, regime), "mapped"
                except Exception:
                    return 0.0, "fail_closed_exception"
            else:
                # no calibration API: the (already validated) raw is the identity
                return float(raw_conf), "no_calibration_api_identity"
        pv = self._finite01_or_none(p)   # validate the mapped OUTPUT too
        return (pv, reason) if pv is not None \
            else (0.0, "fail_closed_invalid_output")

    def _bind_cycle_calibration(self, cycle, raw, calibrated, threshold,
                                reason) -> None:
        """Blocker 8: capture the AUTHORITATIVE calibration provenance into
        coordinator-owned, DEEP-COPIED per-cycle state BEFORE any external
        callback. The cycle dict mirrors it for display/CostMetrics, but the
        TRUSTED source used at finalization is self._active_calibration - a
        forged result/cycle field can never survive."""
        model, regime = self._frozen_context()   # ONE frozen pair per episode
        cold = {}
        try:
            cold = dict(getattr(self.calibrator, "cold_start_source", {}) or {})
        except Exception:
            cold = {}
        # blocker 4 (coverage consistency): record the proposal ELIGIBLE and
        # BIND the ACTUAL bool the calibrator returned, so the exported
        # proposal_eligible EXACTLY matches the calibrator's eligible counter
        # (an invalid raw counts eligible=False in BOTH). An exception -> False +
        # explicit reason.
        rp = getattr(self.calibrator, "record_proposal", None)
        if callable(rp):
            try:
                proposal_eligible = bool(rp(raw, model, regime))
                elig_reason = ("eligible" if proposal_eligible
                               else "not_eligible_invalid_raw")
            except Exception as e:
                proposal_eligible, elig_reason = False, "eligible_exception"
                logger.debug(f"record_proposal failed: {e}")
        else:
            # no record_proposal API: a valid raw score is eligible (best effort)
            proposal_eligible = self._finite01_or_none(raw) is not None
            elig_reason = ("eligible" if proposal_eligible
                           else "not_eligible_invalid_raw")
        # store the VALIDATED raw (None when out-of-range/non-finite) so the
        # evidence stays JSON/[0,1]-valid; the invalidity is captured in the
        # reasons (calibration_reason / proposal_eligible_reason).
        raw_valid = self._finite01_or_none(raw)
        trusted = {
            "raw_confidence": raw_valid,
            "calibrated_probability": calibrated,
            "threshold": threshold,
            "threshold_applied_to": "calibrated_probability",
            "model_id": model,
            "operating_regime_id": regime,
            "calibration_reason": reason,
            "cold_start_source": cold,
            "proposal_eligible": proposal_eligible,
            "proposal_eligible_reason": elig_reason,
        }
        self._active_calibration = copy.deepcopy(trusted)
        # mirror onto the (mutable) cycle for display + CostMetrics conversion
        cycle["theta_star"] = threshold      # legacy field consumers still read
        cycle["raw_confidence"] = raw_valid
        cycle["calibrated_probability"] = calibrated
        cycle["threshold"] = threshold
        cycle["threshold_applied_to"] = "calibrated_probability"
        cycle["calibration_reason"] = reason
        cycle["cold_start_source"] = copy.deepcopy(cold)
        cycle["proposal_eligible"] = proposal_eligible
        cycle["proposal_eligible_reason"] = elig_reason
        cycle["calibration_context"] = {"model_id": model,
                                        "operating_regime_id": regime}

    def _resolve_calibration(self) -> Dict:
        """The AUTHORITATIVE calibration provenance (blocker 8): the deep copy
        bound on the active transaction when one exists, else the coordinator-
        owned _active_calibration. NEVER the mutable result/cycle. Empty {} for
        an episode that never reached S2 routing."""
        tx = getattr(self, "_active_txn", None)
        if tx is not None and getattr(tx, "calibration", None):
            return copy.deepcopy(dict(tx.calibration))
        src = getattr(self, "_active_calibration", None)
        return copy.deepcopy(dict(src)) if src else {}

    def _record_cycle(self, cycle: Dict, episode_success: bool) -> None:
        """Record ONE coordination cycle for adaptive calibration (C1 / A8).

        Exactly one CostMetrics per cycle - no duplicates, no omissions:
        each cycle carries its own LLM confidence and its own trial
        outcome, so cycles are the statistical unit for ECE and cost
        estimation (a re-executed episode contributes each failed trial's
        C_cont sample AND the final successful trial's R_success sample).
        episode_success is the episode's FINAL resolution - True only on
        the committing cycle.
        """
        tstats = cycle.get("trial_stats") or {}
        nstats = cycle.get("nego_stats") or {}
        trial_executed = cycle.get("trial_success") is not None
        tb = tstats.get("tput_before")
        # blocker 3/8: TRUSTED calibration provenance for the CostMetrics schema
        # (raw/calibrated/threshold/model/regime) - never the mutable cycle.
        _cal = self._resolve_calibration()
        metrics = CostMetrics(
            phase=getattr(self, "current_phase", "") or "",
            trial_executed=trial_executed,
            trial_success=bool(cycle.get("trial_success", False)),
            rolled_back=bool(cycle.get("rolled_back", False)),
            hard_failure=bool(cycle.get("hard_failure", False)),
            measurement_invalid=bool(cycle.get("measurement_invalid", False)),
            # Batch F (review B): the throughput was UNKNOWN (missing/failed
            # baseline or window probe) - excluded from the adaptive cost history
            # so a missing baseline can never enter as a fake 0. Set from the
            # trusted trial/nego validity, NOT the numeric 0.0.
            throughput_unknown=bool(
                tstats.get("throughput_unknown")
                or tstats.get("tput_before") is None
                or tstats.get("tput_after") is None
                or tstats.get("tput_min") is None
                or tstats.get("baseline_unknown")),
            nego_throughput_unknown=bool(
                nstats.get("tput_during_nego_unknown")),
            # Batch F (#d8b9): the timestamped trajectories so the single
            # estimator integrates a real trajectory (trajectory_used).
            trial_trajectory=[tuple(p) for p in (tstats.get("trajectory") or [])],
            nego_trajectory=[tuple(p) for p in (nstats.get("trajectory") or [])],
            # Optional: an UNMEASURED reconnection stays None (never coerced to
            # 0.0, which would claim a zero-cost reconnection without evidence).
            reconnection_time=(float(cycle["reconnection_time"])
                               if cycle.get("reconnection_time") is not None
                               else None),
            throughput_before=float(tb) if tb is not None else 0.0,
            # Batch F: an UNKNOWN (None) window throughput is NOT a real 0 - the
            # honest UNKNOWN lives in the measurement samples + baseline, and the
            # cost estimator's trajectory/C_cont path skips None. The legacy
            # numeric EpisodeRecord field takes 0.0 only because it is float-typed
            # AND such a trial is measurement_invalid (excluded from R_success).
            throughput_after=(float(tstats["tput_after"])
                              if tstats.get("tput_after") is not None else 0.0),
            throughput_trial_min=(float(tstats["tput_min"])
                                  if tstats.get("tput_min") is not None
                                  else 0.0),
            trial_duration=float(tstats.get("tau", self.tau_trial_s)),
            negotiation_entered=bool(nstats),
            negotiation_rounds=int(nstats.get("rounds", 0)),
            negotiation_duration=float(nstats.get("duration_s", 0.0)),
            throughput_during_nego=(float(nstats["tput_during_nego"])
                                    if nstats.get("tput_during_nego") is not None
                                    else 0.0),
            llm_confidence=float(cycle.get("confidence", 0.0)),
            episode_success=bool(episode_success),
            # Batch D schema (blocker 3): raw vs calibrated + partition + routing
            raw_confidence=_cal.get("raw_confidence"),
            calibrated_probability=_cal.get("calibrated_probability"),
            threshold=_cal.get("threshold"),
            threshold_applied_to="calibrated_probability",
            model_id=str(_cal.get("model_id") or self._frozen_context()[0]),
            operating_regime_id=str(_cal.get("operating_regime_id")
                                    or self._frozen_context()[1]),
            # coverage consistency: the ACTUAL recorded eligibility bool (matches
            # the calibrator counter), NOT bool(_cal).
            proposal_eligible=bool(_cal.get("proposal_eligible", False)),
            routed_to=str(cycle.get("routed_to") or ""),
            proposal_executed=bool(trial_executed),
        )
        self.calibrator.record_episode(metrics)
        # Blocker 5: SETTLE the trial reservation with its MEASURED actual cost,
        # linked by reservation id. Prefer the timestamped S4 trajectory; only a
        # flagged legacy fallback uses the scalar min*tau. The hard-failure
        # actual is recorded as a SEPARATE component.
        ledger = getattr(self, "_reserve_ledger", None)
        rid = cycle.get("trial_reservation_id")
        if ledger is not None and trial_executed and rid is not None:
            tau_v = float(tstats.get("tau", getattr(self, "tau_trial_s", 15.0)))
            tmin = tstats.get("tput_min")
            traj = tstats.get("trajectory") or cycle.get("trial_trajectory")
            actual, method = None, None
            # prefer timestamped S4 trajectory (deficit vs measured baseline)
            if traj and tb is not None and self._finite(tb):
                try:
                    from calibration.trajectory import trajectory_cost as _tc
                    area = _tc(traj, deficit_ref=float(tb))
                    if area is not None:
                        actual, method = float(area), "trajectory"
                except Exception:
                    actual = None
            # flagged legacy scalar fallback ONLY when both baseline and window
            # min are real finite measurements (never claim 0 without evidence).
            if actual is None and tb is not None and self._finite(tb) \
                    and tmin is not None and self._finite(tmin):
                actual = max(0.0, float(tb) - float(tmin)) * max(0.0, tau_v)
                method = "legacy_min_tau"
            if actual is None:
                ledger.settle(rid, ledger.UNKNOWN, actual=None,
                              method="measurement_missing",
                              error="trial cost unmeasured "
                                    "(no trajectory / finite baseline+min)")
            else:
                continuous = actual
                comps = {"continuous": continuous}
                rc = cycle.get("reconnection_time")
                if cycle.get("hard_failure") and (rc is None
                                                  or not self._finite(rc)):
                    # hard failure with an UNMEASURED reconnection/exposure cost:
                    # the total is incomplete -> PARTIAL_UNKNOWN, actual=None, the
                    # measured continuous part kept ONLY as a lower bound (never
                    # claim the whole actual is just the continuous component).
                    comps["hard_failure"] = None
                    comps["hard_failure_measurement_missing"] = True
                    ledger.settle(rid, ledger.PARTIAL_UNKNOWN, actual=None,
                                  actual_lower_bound=continuous, method=method,
                                  components=comps,
                                  error="hard-failure reconnection cost "
                                        "unmeasured")
                else:
                    if cycle.get("hard_failure"):
                        hard_actual = float(tb) * float(rc)   # SEPARATE component
                        comps["hard_failure"] = hard_actual
                        actual = continuous + hard_actual
                    ledger.settle(rid, ledger.SETTLED, actual=actual,
                                  method=method, components=comps)
        # Blocker 3/4/8: attribute the EXECUTED trial outcome to the TRUSTED
        # model/regime + raw score (never the mutable cycle), so the reliability
        # map trains only on executed proposals in the correct partition.
        if trial_executed:
            rec_fn = getattr(self.calibrator, "record_calibration_outcome", None)
            if callable(rec_fn):
                try:
                    rec_fn(_cal.get("raw_confidence"),
                           bool(cycle.get("trial_success", False)),
                           _cal.get("model_id"),
                           _cal.get("operating_regime_id"))
                except Exception as e:
                    logger.debug(f"calibration record failed: {e}")

    # ------------------------------------------------------------------
    # Batch E (P0-15): mode-aware history retrieval + finalized append.
    # ------------------------------------------------------------------

    @staticmethod
    def _intent_axis_hint(intent) -> Tuple[str, ...]:
        """A DETERMINISTIC action-axis hint for the retrieval query, derived
        from the intent KPI/type (never invented per-record evidence - it only
        biases relevance scoring toward records that touched similar axes)."""
        kpi = ""
        t = getattr(intent, "target", None)
        if t is not None:
            kpi = str(getattr(t, "kpi_name", "") or "")
        typ = getattr(getattr(intent, "type", None), "value",
                      str(getattr(intent, "type", "")))
        key = (kpi or typ or "").lower()
        table = {
            "throughput": ("prb", "mcs"),
            "throughput_goal": ("prb", "mcs"),
            "throughput_fairness": ("prb",),
            "latency": ("sched_priority", "prb"),
            "latency_goal": ("sched_priority", "prb"),
            "power": ("tx_power",),
            "power_constraint": ("tx_power",),
        }
        for k, axes in table.items():
            if k in key:
                return axes
        return tuple()

    def _history_for_prompt(self, new_intent) -> Optional[List[Dict]]:
        """The history records to inject into the feasibility prompt (P0-15).

        DISABLED  -> None (ZERO records; the selector is NEVER called).
        FROZEN    -> deterministic selection over the DEFENSIVE immutable
                     frozen snapshot (never learned into).
        ONLINE    -> deterministic selection over the live reservoir.
        Selection is partitioned to EXACTLY this cycle's (model_version,
        operating_regime) - no cross-model/regime leakage."""
        mode = HistoryMode.coerce(getattr(self, "history_mode",
                                          HistoryMode.ONLINE_RESERVOIR))
        if mode == HistoryMode.DISABLED:
            return None
        if mode == HistoryMode.FROZEN_RESERVOIR:
            dataset = snapshot_records(getattr(self, "_frozen_history", ()))
        else:
            # coerce any legacy dict entries so the selector sees records.
            dataset = snapshot_records(getattr(self, "history_reservoir", []))
        if not dataset:
            return None
        model_version, regime = self._frozen_context()
        # proposer id is the PINNED cycle proposer (partition isolation is by
        # proposer + model + regime, matching how records are attributed).
        proposer_id, pinned_model = self._proposer_context()
        model_version = model_version or pinned_model
        try:
            signature = self._intent_content_hash(new_intent)
        except Exception:
            signature = ""
        axes = self._intent_axis_hint(new_intent)
        selected = select_relevant(
            dataset, intent_signature=signature, action_axes=axes,
            operating_regime=regime, proposer_id=proposer_id,
            model_version=model_version, mode=mode, limit=5)
        return [r.to_dict() for r in selected] or None

    def _history_warranted(self, result: Dict) -> bool:
        """True iff a finalized history record is WARRANTED for this result:
        ONLINE mode + an intent signature captured at feasibility + a real
        evidence-record id on the finalized bundle. When not warranted there is
        simply nothing to learn (NOT a failure - no downgrade)."""
        mode = HistoryMode.coerce(getattr(self, "history_mode",
                                          HistoryMode.ONLINE_RESERVOIR))
        if mode != HistoryMode.ONLINE_RESERVOIR:
            return False
        if not str(getattr(self, "_last_intent_signature", "") or ""):
            return False
        evidence = result.get("evidence") or {}
        erid = evidence.get("evidence_record_id")
        return isinstance(erid, str) and bool(erid)

    def _build_history_record(self, result: Dict) -> "HistoryRecord":
        """Build (but do NOT append) the finalized HistoryRecord from the
        TRUSTED evidence bundle + the coordinator-owned PINNED cycle context.
        The evidence identity is ONLY EvidenceRecord.evidence_record_id (never a
        trial/episode alias). Assumes _history_warranted(result) is True; RAISES
        HistoryValidationError on a malformed record (the caller decides whether
        that is a fail-closed skip or a pre-admission downgrade)."""
        evidence = result.get("evidence") or {}
        evidence_id = evidence.get("evidence_record_id")
        signature = str(getattr(self, "_last_intent_signature", "") or "")
        # proposer/model: the coordinator-owned PINNED cycle context (still live).
        proposer_id, model_version = self._proposer_context()
        # operating regime: the trusted frozen partition.
        regime = ""
        cal_ctx = evidence.get("calibration_context")
        if isinstance(cal_ctx, dict):
            regime = str(cal_ctx.get("operating_regime_id") or "")
        if not regime:
            try:
                regime = self._frozen_context()[1]
            except Exception:
                regime = self._operating_regime_id()
        # action axes actually touched (empty for a no-write outcome).
        canonical = evidence.get("canonical_action")
        if isinstance(canonical, dict) and canonical:
            axes = tuple(sorted(str(k) for k in canonical.keys()))
        else:
            axes = tuple()
        return HistoryRecord(
            intent_signature=signature, action_axes=axes,
            operating_regime=regime or "default",
            proposer_id=str(proposer_id or "unknown"),
            model_version=str(model_version or proposer_id or "unknown"),
            evidence_id=evidence_id,
            terminal_outcome=str(result.get("terminal_outcome") or ""),
            terminal_reason=str(result.get("terminal_reason") or ""),
            created_at=datetime.now().isoformat())

    def _append_history_record(self, record: "HistoryRecord") -> None:
        res = getattr(self, "history_reservoir", None)
        if res is None:
            res = self.history_reservoir = []
        res.append(record)
        max_hist = int(getattr(self, "max_history", 100) or 100)
        if len(res) > max_hist:
            self.history_reservoir = res[-max_hist:]

    def _prepare_history(self, result: Dict) -> None:
        """PHASE 1 (before admission): VALIDATE that the finalizable history
        record can be built, so a history-construction failure DOWNGRADES the
        episode BEFORE intent_manager.add (Batch A: history-finalization failure
        fails closed pre-admission). Stores a provisional candidate but does NOT
        append it - publication happens only AFTER settlement decides the final
        outcome. A warranted-but-MALFORMED record RAISES (the caller's cleanup
        finally downgrades)."""
        self._pending_history = None
        if not self._history_warranted(result):
            return
        # a build failure here PROPAGATES -> pre-admission downgrade.
        self._pending_history = self._build_history_record(result)

    def _publish_history(self, final_result: Dict) -> None:
        """PHASE 2 (after settlement): publish EXACTLY ONE record matching the
        TRULY FINAL evidence/outcome, attributed to the ORIGINAL pinned cycle
        (the pin is still live here). If settlement downgraded/replaced the
        outcome, the provisional commit candidate is DISCARDED and the record is
        rebuilt from the final result, so no stale commit entry survives. A
        build failure at this point is fail-closed (skip, log) - it is too late
        to downgrade, but a false record is never published."""
        # The provisional candidate (self._pending_history) is DISCARDED here -
        # it existed only to validate buildability BEFORE admission (Batch A
        # pre-admission downgrade). The AUTHORITATIVE record is always rebuilt
        # from the FINAL trusted result while the pin is still live, so its
        # outcome / reason / evidence_record_id / proposer / model always equal
        # the final EvidenceRecord (a settlement downgrade can never leave a
        # stale commit or a mismatched reason).
        self._pending_history = None
        try:
            if not self._history_warranted(final_result):
                return                       # settlement made it non-learnable
            record = self._build_history_record(final_result)
            self._append_history_record(record)
        except HistoryValidationError as e:
            logger.warning(f"final history record rejected (fail-closed): {e}")
        except Exception as e:
            logger.warning(f"final history publish failed (skipped): {e}")

    def _add_to_history(self, result: Dict):
        """Single-phase build+append convenience (direct/test use). The EPISODE
        path uses the two-phase _prepare_history/_publish_history instead. A
        malformed/unwarranted record is a fail-closed skip here."""
        if not self._history_warranted(result):
            return
        try:
            record = self._build_history_record(result)
        except HistoryValidationError as e:
            logger.warning(f"history record rejected (fail-closed): {e}")
            return
        except Exception as e:
            logger.warning(f"history record build failed (skipped): {e}")
            return
        self._append_history_record(record)

    # ------------------------------------------------------------------
    # Terminal outcome + evidence finalization (Batch A: sections 3 /
    # P0-4 / P0-6). Every episode return funnels through _finalize_episode.
    # ------------------------------------------------------------------

    def _begin_episode(self, result: Dict) -> None:
        """Stamp the episode-level identifier chain onto ``result`` (P0-6).

        Idempotent, and safe on ``__new__``-built test skeletons that skip
        ``__init__`` (all reads use getattr with a lazy fallback)."""
        # Batch E: PREALLOCATE the evidence-record identity FIRST and outside the
        # idempotent guard, so it is ALWAYS present (every authoritative record
        # reuses this EXACT value; finalization never invents a second id).
        # Minted exactly once - a re-entry sees it already set and keeps it.
        if not result.get("evidence_record_id"):
            result["evidence_record_id"] = new_id("evid")
        if result.get("episode_id"):
            return
        run_id = getattr(self, "experiment_run_id", None)
        if not run_id:
            run_id = self.experiment_run_id = new_id("run")
        self._episode_seq = getattr(self, "_episode_seq", 0) + 1
        result["experiment_run_id"] = run_id
        result["episode_id"] = new_id("ep")
        # fsm_step_id identifies this FSM pass; minted once at episode start
        # and reused in the terminal evidence (not invented at finalization).
        result["fsm_step_id"] = new_id("fsm")

    def _resolve_trigger_context(self) -> Dict:
        """The AUTHORITATIVE episode re-entry provenance (review bug).

        PURE (no external callback): resolved from the ACTIVE transaction's
        captured deep copy when a transaction exists, otherwise the
        coordinator-owned deep-copied ``_active_trigger_context`` (a
        pre-actuation / no-tx failure). NEVER from the mutable result / cycle,
        so a caller/rollback-callback that forges ``result['trigger_context']``
        cannot corrupt the emitted provenance on ANY terminal outcome. Returns
        a fresh deep copy (``{}`` for a direct, non-re-entry episode)."""
        tx = getattr(self, "_active_txn", None)
        if tx is not None:
            src = getattr(tx, "trigger_context", None)
        else:
            src = getattr(self, "_active_trigger_context", None)
        return copy.deepcopy(dict(src)) if src else {}

    def _normalize_result_trigger_context(self, result: Dict) -> Dict:
        """Overwrite ``result['trigger_context']`` from the trusted source (a
        fresh deep copy) or REMOVE it when empty, and return the trusted value.
        Called at the START of finalization so evidence is built from a result
        that already carries only trusted provenance (review bug)."""
        trusted = self._resolve_trigger_context()
        if trusted:
            result["trigger_context"] = copy.deepcopy(trusted)
        else:
            result.pop("trigger_context", None)
        return trusted

    def _finalize_episode(self, result: Dict, outcome: TerminalOutcome,
                          reason: TerminalReason, *,
                          pending_intent=None,
                          committed_revision=None) -> Dict:
        """The SINGLE finalization funnel (P0-4 step 3).

        Builds the validated ``TerminalRecord`` bundle (outcome + reason +
        mandatory evidence + pending intent + committed revision) and derives
        every legacy field FROM it: ``success`` (commit-only), ``resolution``,
        ``terminal_outcome``/``terminal_reason``, and the explicit
        ``pending_intent``/``committed_revision`` (P0-4). The pending intent is
        preserved on EVERY path; committed_revision names the executed intent
        for BOTH commit outcomes (the original as revision 0 for CommitOriginal,
        the executed revised intent for CommitRevised) and is None for every
        non-commit outcome. TerminalRecord validation guarantees a mismatched
        (outcome, reason) or an inconsistent terminal/evidence record can never
        be emitted.
        """
        self._begin_episode(result)
        # review bug: normalize the result's re-entry provenance from the TRUSTED
        # source (active tx / coordinator-owned ctx) BEFORE building evidence, so
        # NON-commit terminals cannot emit a forged result/evidence trigger
        # context. The commit path additionally re-reflects from tx at settlement.
        self._normalize_result_trigger_context(result)
        # Batch D (P0-10): refresh the top-level ledger snapshot from the live
        # per-episode ledger, so result/evidence agree on the final reservations
        # (the episode-start snapshot was empty). Blocker 5: any accepted
        # reservation still unsettled at the terminal (an exception/timeout
        # aborted its transition) is settled UNKNOWN so it is never omitted.
        _rl = getattr(self, "_reserve_ledger", None)
        if _rl is not None:
            for _rid in _rl.unsettled_accepted_ids():
                _rl.settle(_rid, _rl.UNKNOWN,
                           error="episode terminated before settlement")
            result["reserve_ledger"] = _rl.to_dict()
        # Blocker 8: normalize the last cycle's calibration fields from the
        # TRUSTED source, so a forged cycle raw/calibrated/threshold cannot
        # survive into the emitted record.
        _tc = self._resolve_calibration()
        _cyc = result.get("cycles") or []
        if _cyc and _tc:
            last = _cyc[-1]
            for k in ("raw_confidence", "calibrated_probability", "threshold",
                      "threshold_applied_to"):
                last[k] = _tc.get(k)
            last["calibration_context"] = copy.deepcopy(_tc)
        # item 2: a WHOLE _build_evidence exception (not only a returned
        # build_err) must fail closed, never escape process_intent.
        try:
            evidence, build_err = self._build_evidence(result, outcome, reason)
        except Exception as e:
            logger.error(f"evidence construction raised: {e}")
            evidence, build_err = None, str(e)
        if build_err is not None:
            # FAIL-CLOSED (not fail-open): the required evidence could not be
            # built, so the episode CANNOT be trusted as a commit/admission.
            # Atomically downgrade the whole terminal decision to
            # TechnicalFailsafe/InternalError, clear the committed revision,
            # and stamp a minimal evidence bundle whose OWN terminal fields
            # match the downgrade. The ROOT exception text is preserved in BOTH
            # the result error and the audit evidence.
            outcome = TerminalOutcome.TECHNICAL_FAILSAFE
            reason = TerminalReason.INTERNAL_ERROR
            committed_revision = None
            note = (f"evidence unavailable - downgraded to failsafe: "
                    f"{build_err}")
            prior = result.get("error")
            result["error"] = f"{prior}; {note}" if prior else note
            evidence = self._minimal_evidence(result, outcome, reason,
                                              build_err)
        record = TerminalRecord(
            outcome=outcome, reason=reason, evidence=evidence,
            pending_intent=pending_intent,
            committed_revision=committed_revision)
        result.update(record.to_dict())
        return result

    def _settle_after_cleanup(self, result: Dict) -> Dict:
        """Settle a still-OPEN transaction AFTER _process_intent_locked's
        S0/history cleanup finally has run (coordinator follow-up to item 2).

        A validated commit was left PENDING (applied, not admitted) so the
        cleanup ran with the transaction still open. Now:
          * if the cleanup left a COMMIT record intact -> admit + mark verified
            (a genuine commit);
          * if the cleanup DOWNGRADED the commit (a post-write final-S0 callback
            or history-finalization failure) -> the record is already
            TechnicalFailsafe; roll the applied trial back EXACTLY ONCE and admit
            nothing.
        Non-commit / non-actuating episodes settle to themselves.
        """
        tx = getattr(self, "_active_txn", None)
        if tx is None or not tx.pending_commit or tx.commit_verified \
                or tx.rollback_done:
            return result
        # SETTLEMENT-LEVEL LAST-RESORT BACKSTOP (coordinator review item 1):
        # BOTH commit-invariant gates and EVERY pre-admission settlement
        # operation run under this guard.  Any UNEXPECTED exception before a
        # COMPLETED admission (commit_verified) must NOT escape: it fails closed
        # to a typed non-commit, admits nothing, keeps commit_verified False,
        # rolls back a real write exactly once, and preserves the root error.
        try:
            outcome_val = result.get("terminal_outcome")
            if outcome_val in (TerminalOutcome.COMMIT_ORIGINAL.value,
                               TerminalOutcome.COMMIT_REVISED.value):
                return self._commit_settle(result, tx)
            # cleanup downgraded the pending commit -> roll back once, admit
            # nothing
            logger.error("pending commit downgraded during cleanup - "
                         "rolling back")
            if tx.actual_real_write_performed and not tx.rollback_done:
                tx.rollback_done = True
                self._run_rollback_transaction(
                    tx.snapshot, None, first_write_time=tx.first_write_time,
                    actual_real_write=tx.actual_real_write_performed,
                    audit_extra=self._audit_from_tx(tx, "cleanup_downgrade"))
            if not self._is_latched():
                self.safety_state = SafetyState.READY
            return result
        except Exception as e:
            # If admission already completed (commit_verified) the exception is
            # NOT pre-admission - re-raise rather than mask a real commit.
            if getattr(tx, "commit_verified", False):
                raise
            return self._settlement_failsafe(result, tx, e)

    def _settlement_failsafe(self, result: Dict, tx: "ActuationTransaction",
                             err: Exception) -> Dict:
        """Last-resort settlement backstop (coordinator review item 1).

        Any unexpected exception thrown by a commit gate or a pre-admission
        settlement operation (e.g. _intent_content_hash raising on a corrupted
        committed intent) lands here.  Fails closed to TechnicalFailsafe
        (RestoreUnverified when the latch requires it, else InternalError),
        admits nothing, keeps commit_verified False, rolls a real write back
        EXACTLY ONCE, preserves the root error in result/evidence, and NEVER
        escapes."""
        logger.error(f"settlement exception - failing closed: {err}")
        pending_ref = getattr(tx, "pending_intent", None)
        # roll back a REAL write exactly once (commit_verified stays False)
        if getattr(tx, "actual_real_write_performed", False) \
                and not tx.rollback_done:
            tx.rollback_done = True
            try:
                self._run_rollback_transaction(
                    tx.snapshot, None, first_write_time=tx.first_write_time,
                    actual_real_write=tx.actual_real_write_performed,
                    audit_extra=self._audit_from_tx(tx, "settlement_exception"))
            except Exception as e2:
                logger.error(f"rollback during settlement failsafe failed: {e2}")
            result["rolled_back"] = True
        tx.pending_commit = False       # settled (failed): do not re-settle
        if not self._is_latched():
            self.safety_state = SafetyState.READY
        prior = result.get("error")
        note = f"settlement exception: {err}"
        result["error"] = f"{prior}; {note}" if prior else note
        reason = (TerminalReason.RESTORE_UNVERIFIED if self._is_latched()
                  else TerminalReason.INTERNAL_ERROR)
        try:
            return self._finalize_episode(
                result, TerminalOutcome.TECHNICAL_FAILSAFE, reason,
                pending_intent=pending_ref, committed_revision=None)
        except Exception as e3:
            # even finalization failed: stamp minimal typed fields (never escape)
            logger.error(f"settlement failsafe finalize failed: {e3}")
            result["success"] = False
            result["resolution"] = "reject"
            result["terminal_outcome"] = TerminalOutcome.TECHNICAL_FAILSAFE.value
            result["terminal_reason"] = reason.value
            return result

    @staticmethod
    def _snapshot_intent_manager(im):
        """Snapshot the intent-manager lifecycle BEFORE admission (item 2), so a
        partially-mutating add that then raises can be rolled back to the EXACT
        pre-admission state. Prefers a native snapshot_state(); otherwise
        shallow-copies whichever known list/dict lifecycle attributes exist
        (supports test managers with an ``added`` list, etc.)."""
        native = getattr(im, "snapshot_state", None)
        if callable(native):
            try:
                return ("native", native())
            except Exception as e:
                logger.warning(f"intent-manager snapshot_state failed: {e}")
        attrs = {}
        for name in ("added", "intents", "history", "_intents", "_history"):
            if hasattr(im, name):
                val = getattr(im, name)
                if isinstance(val, dict):
                    attrs[name] = dict(val)
                elif isinstance(val, list):
                    attrs[name] = list(val)
        return ("attrs", attrs)

    @staticmethod
    def _restore_intent_manager(im, snap) -> None:
        """Restore a _snapshot_intent_manager() bundle (item 2)."""
        kind, data = snap
        if kind == "native":
            fn = getattr(im, "restore_state", None)
            if callable(fn):
                try:
                    fn(data)
                    return
                except Exception as e:
                    logger.error(f"intent-manager restore_state failed: {e}")
        for name, val in (data or {}).items():
            try:
                setattr(im, name, val)
            except Exception as e:
                logger.error(f"intent-manager restore of {name} failed: {e}")

    def _commit_settle(self, result: Dict, tx: "ActuationTransaction") -> Dict:
        """Admit the intent + mark the transaction verified for a commit whose
        provisional record survived cleanup (item 2). The evidence was already
        built in the provisional record; admission is the last settlement step
        AND is made TRANSACTIONAL over the intent-manager lifecycle.

        Before admission the manager state and the intent's prior status are
        snapshotted. On success the intent is promoted to ACTIVE (deferred from
        the cycle). On an explicit False OR ANY exception after a partial
        mutation, the EXACT pre-admission manager state and the pending intent's
        prior status are restored (old intent back, new absent, history
        unchanged), the applied trial is rolled back EXACTLY ONCE, and a
        TechnicalFailsafe record is returned.
        """
        # P0-6 / 3.3: the ATOMIC commit gate runs FIRST, immediately before
        # settlement.  Any mismatch (hash / read-back / freshness / expiry /
        # intent-set / joint satisfaction / latch / open obligation) BLOCKS the
        # commit and hands control to the safe transaction path - a single
        # mid-S4 read-back is not sufficient.
        # P0-7 (coordinator review): the absolute deadline is enforced BEFORE
        # settlement too - a deadline overrun here takes the safe transaction
        # path (rollback; PendingNotAdmitted on a verified restore).
        if self._deadline_expired():
            return self._block_commit(result, tx, _cb.CommitVerdict(
                False, "deadline_exhausted",
                "episode deadline exceeded before settlement"))
        # P1-3 (blocker 1/4): bind the FRESH pre-settlement read-back (+ its
        # honest timestamp) and the AUTHORITATIVE commit-gate readback latency onto
        # the cycle in a FINALLY, so a _verify_commit that RAISES (e.g. the
        # read-back itself raised) still preserves the measured timer rather than
        # losing it. readback_ms reports the authoritative commit-gate read-back
        # (measured in _verify_commit), overriding the S4 evidence timing.
        try:
            verdict = self._verify_commit(tx)
        finally:
            cycles = result.get("cycles") or []
            if cycles:
                cycles[-1]["final_readback"] = getattr(tx, "final_readback", None)
                cycles[-1]["final_readback_time"] = getattr(
                    tx, "final_readback_time", None)
                _crb = getattr(tx, "commit_readback_ms", None)
                if _crb is not None:
                    cycles[-1]["readback_ms"] = _crb
        tx.commit_check = verdict.to_dict()
        result["commit_check"] = verdict.to_dict()
        if not verdict.ok:
            return self._block_commit(result, tx, verdict)

        committed_ref = tx.committed_intent
        pending_ref = tx.pending_intent

        # Acquire the intent-manager snapshot + the intent's prior status NOW,
        # BEFORE the evidence re-stamp / final gate (coordinator review): a
        # CUSTOM snapshot_state() is external/user code that could mutate the
        # device or consume the deadline, so it must NOT run after the final
        # gate.  After the final gate only the pure reflection + im.add run.
        im = self.intent_manager
        im_snapshot = self._snapshot_intent_manager(im)
        prior_status = getattr(committed_ref, "status", None)

        # (1) Re-stamp the evidence with the commit-check verdict BEFORE any
        # admission or commit_verified mutation (coordinator review): the
        # committed evidence carries the full commit-invariant provenance, AND
        # _finalize_episode fails CLOSED by RETURNING a TechnicalFailsafe
        # downgrade (or may raise) when the evidence cannot be built.  Because
        # NOTHING has been admitted yet, either failure just aborts the commit -
        # it can never leave an admitted intent behind a non-commit record.
        if tx.commit_outcome is not None and tx.commit_reason is not None:
            try:
                result = self._finalize_episode(
                    result, tx.commit_outcome, tx.commit_reason,
                    pending_intent=pending_ref, committed_revision=committed_ref)
            except Exception as e:
                logger.error(f"commit evidence finalization raised: {e}")
                return self._fail_commit_after_gate(
                    result, tx, f"commit evidence finalization raised: {e}")
            # if finalization DOWNGRADED away from the intended commit, do NOT
            # admit - the evidence could not be trusted (fail closed).
            if result.get("terminal_outcome") != tx.commit_outcome.value:
                return self._fail_commit_after_gate(
                    result, tx, "commit evidence downgraded during re-stamp")

        # (1b) THE FINAL GATE (coordinator review): the evidence re-stamp above
        # is the LAST callback/external evidence build.  It may itself have
        # changed the device / fired callbacks / advanced the clock.  So the
        # VERY LAST thing before admission is a fresh deadline + authorization +
        # device-read-back gate, and AFTER it there is NO callback/external call
        # (no further _finalize_episode) - only a PURE DATA-ONLY update of the
        # already-built evidence record to reflect THIS final gate.  This closes
        # the circular hole: no third finalize can reopen the gap.
        if self._deadline_expired():
            return self._fail_commit_after_gate(
                result, tx, "episode deadline exceeded before admission")
        verdict2 = self._verify_commit(tx)     # fresh read-back, final gate
        if not verdict2.ok:
            # safe transaction path (rollback; PendingNotAdmitted on a verified
            # restore, TechnicalFailsafe on an unverified one).
            return self._block_commit(result, tx, verdict2)
        # PURE DATA-ONLY reflection of the final gate into the emitted evidence
        # (no _finalize_episode / callbacks that could reopen the gap).
        tx.commit_check = verdict2.to_dict()
        self._reflect_final_gate(result, tx, verdict2)

        # (2) ONLY NOW admit transactionally over the intent-manager lifecycle
        # (the snapshot + prior status were captured BEFORE the final gate).
        # NOTHING external (callback / device read-back / evidence rebuild /
        # custom snapshot_state) runs between the final gate above and add().
        try:
            add_ret = im.add(committed_ref)
            if add_ret is False:                       # explicit admission reject
                raise RuntimeError("intent admission rejected")
            # admission succeeded: NOW promote to ACTIVE (deferred from cycle)
            try:
                committed_ref.status = IntentStatus.ACTIVE
            except Exception:
                pass
            tx.commit_verified = True                  # commit is now REAL
            # review #6: bind the authoritative committed evidence id
            # (actuation_trial_id) to the admitted intent, for a later
            # assurance-drift re-entry to carry as its origin evidence id.
            self._bind_committed_evidence_id(
                committed_ref, getattr(tx, "actuation_trial_id", None))
            if not self._is_latched():
                self.safety_state = SafetyState.READY
            return result                              # verified commit stands
        except Exception as e:
            logger.error(f"commit admission failed - failing closed: {e}")
            # restore the ENTIRE pre-admission lifecycle + the intent's prior
            # status, so a partially-mutating add leaves NOTHING behind (item 2)
            self._restore_intent_manager(im, im_snapshot)
            if prior_status is not None:
                try:
                    committed_ref.status = prior_status
                except Exception:
                    pass
            if tx.actual_real_write_performed and not tx.rollback_done:
                tx.rollback_done = True
                self._run_rollback_transaction(
                    tx.snapshot, None, first_write_time=tx.first_write_time,
                    actual_real_write=tx.actual_real_write_performed,
                    audit_extra=self._audit_from_tx(tx, "commit_admission"))
            prior = result.get("error")
            note = f"commit admission failed: {e}"
            result["error"] = f"{prior}; {note}" if prior else note
            reason2 = (TerminalReason.RESTORE_UNVERIFIED if self._is_latched()
                       else TerminalReason.INTERNAL_ERROR)
            return self._finalize_episode(
                result, TerminalOutcome.TECHNICAL_FAILSAFE, reason2,
                pending_intent=pending_ref, committed_revision=None)

    def _reflect_final_gate(self, result: Dict, tx: "ActuationTransaction",
                            verdict) -> None:
        """PURE DATA-ONLY publication of the AUTHORITATIVE committed evidence
        (coordinator review item 4).

        After the LAST callback/evidence build and the final gate, the emitted
        EvidenceRecord is REBUILT from the immutable transaction + authorization
        binding data - NOT from the mutable cycle/result dicts a caller could
        forge between the provisional finalize and settlement.  This rewrites
        ALL commit-critical fields consistently (canonical action/hash,
        authorization, requested/applied/clipped action, snapshot/timestamps,
        observations, monitor verdicts, trial/identifier chain, revision/
        intent-set binding, final readback/check, terminal fields), so no forged
        value can survive.  It calls NO callback / device read-back / deadline
        op, so the post-gate hole stays closed.  On any failure the settlement
        backstop (item 1) rolls back."""
        cv = copy.deepcopy(verdict.to_dict())
        result["commit_check"] = cv
        auth = getattr(tx, "authorization", None)
        if auth is None:
            # no authorization to trust: keep the minimal data-only reflection.
            ev0 = result.get("evidence")
            if isinstance(ev0, dict):
                ev0["commit_check"] = cv
            return
        # AUTHORITATIVE, from the immutable transaction + authorization ONLY.
        # This publication calls NO external llm_manager / device / clock /
        # deadline / user code and derives NOTHING from a mutable result/cycle -
        # every field is an already-bound trusted primitive/immutable structure
        # captured before the final gate, DEEP-COPIED here (coordinator review #5
        # items 1/3/4).  So no post-gate call can reopen the readback/deadline
        # gap and no forged mutable value can survive.
        applied = list(getattr(tx, "applied", ()) or ())
        applied_map = {
            f"{a.get('gnb_id')}.{a.get('ue_id') or 'cell'}.{a.get('axis')}":
                a.get("value")
            for a in applied if isinstance(a, dict)}
        rb = getattr(tx, "final_readback", None)
        # AUTHORITATIVE identifier chain, from trusted tx/auth bindings only.
        trusted_run_id = tx.experiment_run_id or "unknown"
        trusted_episode_id = (auth.episode_id or tx.episode_id or "unknown")
        trusted_fsm_step_id = tx.fsm_step_id or "unknown"
        # AUTHORITATIVE re-entry provenance (review bug 2): the deep copy bound
        # onto the transaction at creation, never the mutable result/cycle.
        trusted_trigger = dict(getattr(tx, "trigger_context", {}) or {})
        # Batch D provenance sources (last cycle calibration + episode ledger).
        _cyc_list = result.get("cycles") or []
        _last_cyc = _cyc_list[-1] if _cyc_list else {}
        _rl = getattr(self, "_reserve_ledger", None)
        # blocker 8: trusted calibration from the tx (bound before the write)
        _trusted_cal = dict(getattr(tx, "calibration", {}) or {})
        record = EvidenceRecord(
            experiment_run_id=trusted_run_id,
            episode_id=trusted_episode_id,
            fsm_step_id=trusted_fsm_step_id,
            # REQUIRED (Batch E): the EXACT preallocated identity bound on the tx
            # (== result's), never invented/'unknown' at the commit gate.
            evidence_record_id=(getattr(tx, "evidence_record_id", None)
                                or result.get("evidence_record_id")),
            proposer_id=tx.proposer_id or "unknown",         # bound (no call)
            model_version=tx.model_version or "unknown",     # bound (no call)
            intent_set_version=auth.intent_set_version,      # bound (immutable)
            pending_intent_hash=tx.pending_intent_hash or "unknown",  # bound
            prompt_hash=getattr(tx, "prompt_hash", None),      # bound (P1-6)
            cycle_id=auth.cycle_id,
            proposal_id=auth.proposal_id,
            actuation_trial_id=auth.actuation_trial_id,
            authorized_revision_id=auth.authorized_revision_id,
            requested_action=copy.deepcopy(dict(
                getattr(tx, "requested_action", {}) or {})),
            clipped_action=copy.deepcopy(applied_map),
            canonical_action=dict(auth.canonical_action),    # AUTHORITATIVE
            canonical_action_hash=auth.canonical_action_hash,
            snapshot=copy.deepcopy(dict(getattr(tx, "snapshot", {}) or {})),
            snapshot_readback_time=getattr(tx, "snapshot_readback_time", None),
            action_apply_time=getattr(tx, "action_apply_time", None),
            action_apply_monotonic_s=getattr(tx, "action_apply_monotonic_s", None),
            final_readback=(copy.deepcopy(dict(rb))
                            if isinstance(rb, dict) else rb),
            final_readback_time=getattr(tx, "final_readback_time", None),
            observations=tuple(copy.deepcopy(dict(o)) if isinstance(o, dict)
                               else o
                               for o in getattr(tx, "observations", ()) or ()),
            monitor_verdicts=copy.deepcopy(dict(
                getattr(tx, "monitor_verdicts", {}) or {})),
            rollback_result=None,
            authorization=auth.to_dict(),                    # AUTHORITATIVE
            schema_valid=True,                               # a commit is valid
            schema_reject_reason=None,
            commit_check=cv,
            # NEVER copy the mutable result['error'] into the authoritative
            # commit evidence after the gate (coordinator correction #6 item 2):
            # a committed record is a clean success, so bind the trusted value
            # None rather than a caller-forgeable post-gate error string.
            error=None,
            # P0-8 (review bug 2): rebuild the re-entry provenance from the
            # TRUSTED transaction copy bound at tx creation - a forged result/
            # cycle trigger field cannot survive. None for a direct episode.
            trigger_context=(copy.deepcopy(dict(trusted_trigger))
                             if trusted_trigger else None),
            # Batch D (blocker 8): calibration provenance from the TRUSTED
            # tx.calibration (bound at tx creation), NEVER the mutable cycle - a
            # forged 999/FORGED cycle value cannot survive the reflection.
            raw_confidence=_trusted_cal.get("raw_confidence"),
            calibrated_probability=_trusted_cal.get("calibrated_probability"),
            threshold=_trusted_cal.get("threshold"),
            threshold_applied_to=(_trusted_cal.get("threshold_applied_to")
                                  if _trusted_cal else None),
            calibration_context=(copy.deepcopy(_trusted_cal)
                                 if _trusted_cal else None),
            reserve_ledger=(_rl.to_dict() if _rl is not None else None),
            # Batch F (#7): measurement provenance from the TRUSTED tx ONLY (the
            # exact bound baseline / probe_config / S4 sample tuple) - never a
            # mutable last-cycle / self._probe_cfg() a callback could forge.
            pre_action_baseline=getattr(tx, "pre_action_baseline", None),
            probe_config=getattr(tx, "probe_config", None),
            measurement_samples=tuple(getattr(tx, "measurement_samples", ())
                                      or ()),
            cold_start_source=(copy.deepcopy(_trusted_cal.get(
                "cold_start_source")) if _trusted_cal.get(
                "cold_start_source") else None),
            terminal_outcome=getattr(tx.commit_outcome, "value", None),
            terminal_reason=getattr(tx.commit_reason, "value", None),
        )
        result["evidence"] = record.to_dict()
        # TOP-LEVEL IDENTIFIER CHAIN (coordinator correction #6 item 2): the
        # returned result identifiers MUST equal the authoritative evidence
        # identifiers (existing contract/tests: result IDs == evidence IDs).
        # Data-only overwrite from the SAME trusted tx/auth bindings, so a forged
        # top-level result['experiment_run_id']/['fsm_step_id'] cannot survive.
        result["experiment_run_id"] = trusted_run_id
        result["episode_id"] = trusted_episode_id
        result["fsm_step_id"] = trusted_fsm_step_id
        # TOP-LEVEL re-entry provenance (review bug 2): overwrite from the trusted
        # tx copy when present, or REMOVE it entirely for a direct episode - so a
        # forged result['trigger_context'] cannot survive and result/evidence
        # stay consistent (both carry the trusted ctx, or both carry none).
        if trusted_trigger:
            result["trigger_context"] = copy.deepcopy(trusted_trigger)
        else:
            result.pop("trigger_context", None)
        # keep the last cycle consistent with the authoritative record (so any
        # later honest re-serialization matches); the EMITTED record is the one
        # in result['evidence'].
        cyc = (result.get("cycles") or [])
        if cyc:
            cyc[-1]["canonical_action"] = dict(auth.canonical_action)
            cyc[-1]["canonical_action_hash"] = auth.canonical_action_hash
            cyc[-1]["authorization"] = auth.to_dict()
            cyc[-1]["final_readback"] = (dict(rb) if isinstance(rb, dict) else rb)
            cyc[-1]["final_readback_time"] = getattr(
                tx, "final_readback_time", None)
            cyc[-1]["commit_check"] = cv
            # full identifier chain from trusted bindings (item 2).
            cyc[-1]["episode_id"] = trusted_episode_id
            cyc[-1]["fsm_step_id"] = trusted_fsm_step_id
            cyc[-1]["cycle_id"] = auth.cycle_id
            cyc[-1]["proposal_id"] = auth.proposal_id
            cyc[-1]["actuation_trial_id"] = auth.actuation_trial_id
            # last-cycle re-entry provenance from the trusted tx copy (review
            # bug 2): present -> set the trusted ctx, absent -> remove a forged
            # one, so the cycle stays consistent with result/evidence.
            if trusted_trigger:
                cyc[-1]["trigger_context"] = copy.deepcopy(trusted_trigger)
            else:
                cyc[-1].pop("trigger_context", None)
            # blocker 8: normalize the last cycle's calibration fields from the
            # TRUSTED tx.calibration too, so a forged 999/FORGED cycle value
            # cannot survive the reflection.
            if _trusted_cal:
                for _k in ("raw_confidence", "calibrated_probability",
                           "threshold", "threshold_applied_to"):
                    cyc[-1][_k] = _trusted_cal.get(_k)
                cyc[-1]["calibration_context"] = copy.deepcopy(_trusted_cal)

    def _current_identifier_chain(self, tx: "ActuationTransaction") -> Dict:
        """The identifier chain of the CURRENT write attempt, as the commit gate
        must see it: the episode/cycle/proposal ids from the LAST cycle record
        and the trial id minted on the transaction (P0-6).  For a real write the
        authorization must match this exactly."""
        cycles = None
        result = getattr(self, "_active_result", None)
        if isinstance(result, dict):
            cycles = result.get("cycles")
        last = (cycles[-1] if cycles else {})
        return {
            "episode_id": last.get("episode_id"),
            "cycle_id": last.get("cycle_id"),
            "proposal_id": last.get("proposal_id"),
            "actuation_trial_id": getattr(tx, "actuation_trial_id", None),
        }

    def _verify_commit(self, tx: "ActuationTransaction") -> "_cb.CommitVerdict":
        """Evaluate the atomic commit invariant over the transaction (P0-6 /
        3.3).  Read-back and observation freshness are enforced only for a REAL
        device write; a simulation / no-write commit still re-checks hash,
        authorization validity, intent-set version, joint satisfaction, latch,
        and open-obligation."""
        real_write = bool(getattr(tx, "actual_real_write_performed", False))
        # Recompute the hash from the canonical action ACTUALLY handed to the
        # executor (tx.applied_canonical), rather than trusting a cached hash
        # string, so a divergence between authorized and applied is caught
        # (coordinator review).  When the real write path did not record it (a
        # monkeypatched executor in a settlement-lifecycle unit test), fall back
        # to the bound canonical - those tests do not exercise the write path.
        applied_canonical = getattr(tx, "applied_canonical", None)
        if applied_canonical is None:
            applied_canonical = getattr(tx, "canonical_action", {}) or {}
        applied_hash = _cb.stable_action_hash(applied_canonical)
        # P0-6 (coordinator review): perform a NEW device read-back HERE,
        # immediately before settlement - do NOT reuse the S4 capture.  A
        # state/finalizer callback that changed the device between S4 and
        # settlement is caught, and the fresh read-back time is recorded
        # honestly.
        if real_write:
            # P1-3 (blocker 4): time the AUTHORITATIVE commit-verification
            # read-back (the read-back that actually GATES the commit), measured
            # even if the read raises (finally). This - not the earlier S4
            # evidence capture - is what readback_ms reports, so a
            # non-authoritative duplicate is never silently timed instead.
            _crb_t0 = time.monotonic()
            try:
                fresh_readback = self._readback_canonical(tx)
            finally:
                tx.commit_readback_ms = (time.monotonic() - _crb_t0) * 1000.0
            tx.final_readback = fresh_readback
            tx.final_readback_time = time.time()
        else:
            fresh_readback = None
        committed = getattr(tx, "committed_intent", None)
        auth = getattr(tx, "authorization", None)
        chain = self._current_identifier_chain(tx)
        ctx = _cb.CommitContext(
            authorization=auth,
            applied_action_hash=applied_hash,
            now=time.time(),
            intent_set_version_now=self._intent_set_version(),
            committed_intent_hash_now=self._intent_content_hash(committed),
            committed_revision_id_now=getattr(committed, "id", None),
            current_identifier_chain=chain,
            required_intent_ids=tuple(getattr(tx, "required_intent_ids", ())
                                      or ()),
            monitor_verdicts=dict(getattr(tx, "monitor_verdicts", {}) or {}),
            joint_satisfied=bool(getattr(tx, "joint_satisfied", False)),
            safety_latched=self._is_latched(),
            rollback_obligation_open=bool(getattr(tx, "rollback_done", False)),
            readback_required=real_write,
            final_readback_action=fresh_readback,
            observations_required=real_write,
            action_apply_time=getattr(tx, "action_apply_time", None),
            freshness_max_age_s=float(getattr(self, "commit_freshness_s", 30.0)),
            observations=tuple(getattr(tx, "observations", ()) or ()),
        )
        return _cb.verify_commit_invariant(ctx)

    def _block_commit(self, result: Dict, tx: "ActuationTransaction",
                      verdict: "_cb.CommitVerdict") -> Dict:
        """The atomic commit gate FAILED - block the commit and take the safe
        transaction path (P0-6): roll the applied trial back exactly once, admit
        NOTHING, and finalize PendingNotAdmitted on a verified restore or
        TechnicalFailsafe when the restore could not be verified."""
        logger.error(f"commit invariant blocked ({verdict.code}): "
                     f"{verdict.detail}")
        # record the BLOCKING verdict (not a stale earlier ok) in the audit.
        tx.commit_check = verdict.to_dict()
        result["commit_check"] = verdict.to_dict()
        prior = result.get("error")
        note = f"commit blocked [{verdict.code}]: {verdict.detail}"
        result["error"] = f"{prior}; {note}" if prior else note
        if getattr(tx, "actual_real_write_performed", False) \
                and not tx.rollback_done:
            tx.rollback_done = True
            self._run_rollback_transaction(
                tx.snapshot, None, first_write_time=tx.first_write_time,
                actual_real_write=tx.actual_real_write_performed,
                audit_extra=self._audit_from_tx(tx, f"commit_block:{verdict.code}"))
            result["rolled_back"] = True
        if not self._is_latched():
            self.safety_state = SafetyState.READY
        pending_ref = tx.pending_intent
        if self._is_latched():
            return self._finalize_episode(
                result, TerminalOutcome.TECHNICAL_FAILSAFE,
                TerminalReason.RESTORE_UNVERIFIED,
                pending_intent=pending_ref, committed_revision=None)
        return self._finalize_episode(
            result, TerminalOutcome.PENDING_NOT_ADMITTED,
            TerminalReason.COMMIT_REVERIFICATION_FAILED,
            pending_intent=pending_ref, committed_revision=None)

    def _fail_commit_after_gate(self, result: Dict,
                                tx: "ActuationTransaction", note: str) -> Dict:
        """The commit passed the invariant gate but the post-gate evidence
        re-stamp could NOT be trusted (it raised, or downgraded away from the
        intended commit).  Because admission has NOT happened yet, fail closed:
        admit nothing, leave commit_verified False, roll the applied trial back
        EXACTLY ONCE (real write), and return a TechnicalFailsafe record
        (coordinator review)."""
        logger.error(f"commit aborted after gate: {note}")
        prior = result.get("error")
        result["error"] = f"{prior}; {note}" if prior else note
        if tx.actual_real_write_performed and not tx.rollback_done:
            tx.rollback_done = True
            self._run_rollback_transaction(
                tx.snapshot, None, first_write_time=tx.first_write_time,
                actual_real_write=tx.actual_real_write_performed,
                audit_extra=self._audit_from_tx(tx, "post_gate_evidence"))
            result["rolled_back"] = True
        if not self._is_latched():
            self.safety_state = SafetyState.READY
        # Ensure the returned record is TechnicalFailsafe.  When the re-stamp
        # DOWNGRADED it is already TechnicalFailsafe; when the re-stamp RAISED
        # the record is still the provisional commit, so rebuild it closed here
        # (this rebuild uses the minimal-evidence fallback and cannot re-raise).
        reason2 = (TerminalReason.RESTORE_UNVERIFIED if self._is_latched()
                   else TerminalReason.INTERNAL_ERROR)
        if result.get("terminal_outcome") != (
                TerminalOutcome.TECHNICAL_FAILSAFE.value):
            return self._finalize_episode(
                result, TerminalOutcome.TECHNICAL_FAILSAFE, reason2,
                pending_intent=tx.pending_intent, committed_revision=None)
        return result

    def _build_evidence(self, result: Dict, outcome: TerminalOutcome,
                        reason: TerminalReason):
        """Build the immutable evidence record for this episode (P0-6).

        Returns ``(evidence, error)``. ``error`` is None on success; on failure
        it is the ROOT exception text and ``evidence`` is None, so the caller
        fails closed (downgrade) while preserving the root cause in the audit
        record rather than presenting a commit/admission with an evidence error.
        Stage identifiers (episode/cycle/proposal/trial) are REUSED from where
        they were minted, not re-invented here."""
        try:
            run_id = (result.get("experiment_run_id")
                      or getattr(self, "experiment_run_id", "")
                      or new_id("run"))
            episode_id = result.get("episode_id") or new_id("ep")
            fsm_step_id = result.get("fsm_step_id") or new_id("fsm")
            cycles = result.get("cycles") or []
            last = cycles[-1] if cycles else {}
            proposer_id, model_version = self._proposer_context()
            # review bug: consume the TRUSTED re-entry provenance directly from
            # the authoritative source (tx / coordinator ctx), never the mutable
            # result. INVARIANT: _finalize_episode already normalized the result
            # from the SAME source, so they must agree - a mismatch means a forged
            # result leaked past normalization.
            trusted_trigger = self._resolve_trigger_context()
            assert (result.get("trigger_context") or {}) == (
                trusted_trigger or {}), "trigger_context normalization drift"
            # item 8: bind the ACTUAL action/snapshot/rollback provenance from
            # the transaction (stashed on the cycle) into the terminal evidence,
            # so the emitted record - not just the private latch dict - carries
            # the requested/applied action, the verified snapshot, the executor
            # restore read-back report, and the rollback result. (Canonical
            # action + hash + authorization remain Batch C.)
            applied = last.get("applied_action") or []
            applied_map = {
                f"{a.get('gnb_id')}.{a.get('ue_id') or 'cell'}.{a.get('axis')}":
                    a.get("value")
                for a in applied if isinstance(a, dict)}
            # cycle_id/proposal_id/actuation_trial_id are honestly None when
            # their stage never ran (pre-cycle rejection, negotiation-only
            # cycle, no-write S3 route).
            # Batch C provenance (P0-5 / P0-6): canonical action + stable hash,
            # authorization artifact, post-action observations, the fresh final
            # read-back, the schema verdict, and the atomic commit-check result.
            # prefer the cycle observations; fall back to the result-level
            # single-bundle Observation for a no-cycle terminal (review #7: the
            # already-satisfied shortcut records exactly one timestamped bundle).
            observations = tuple(last.get("observations")
                                 or result.get("observations") or ())
            _rl = getattr(self, "_reserve_ledger", None)
            _trusted_cal = self._resolve_calibration()   # blocker 8: trusted
            return EvidenceRecord(
                experiment_run_id=run_id,
                episode_id=episode_id,
                fsm_step_id=fsm_step_id,
                # REQUIRED (Batch E): the preallocated identity, never invented.
                # Absent -> EvidenceRecord.__post_init__ raises -> fail closed to
                # a downgrade (no second id is minted here).
                evidence_record_id=result.get("evidence_record_id") or "",
                proposer_id=proposer_id,
                model_version=model_version,
                intent_set_version=self._intent_set_version(),
                # Batch G (P1-6): the pending-intent hash of the LAST cycle's frozen
                # snapshot (a revised terminal reflects its final intent); the
                # result-level digest is only a NO-CYCLE fallback.
                pending_intent_hash=(last.get("pending_intent_hash")
                                     or self._pending_intent_hash(result)),
                # Batch G (P1-6): the REAL prompt hash of the last cycle's proposal
                # (None for a no-proposal terminal).
                prompt_hash=last.get("prompt_hash"),
                # Batch D provenance from the TRUSTED calibration source (blocker
                # 8), never the mutable cycle; ledger from the live episode ledger.
                raw_confidence=_trusted_cal.get("raw_confidence"),
                calibrated_probability=_trusted_cal.get("calibrated_probability"),
                threshold=_trusted_cal.get("threshold"),
                threshold_applied_to=(_trusted_cal.get("threshold_applied_to")
                                      if _trusted_cal else None),
                calibration_context=(copy.deepcopy(_trusted_cal)
                                     if _trusted_cal else None),
                reserve_ledger=(_rl.to_dict() if _rl is not None
                                else result.get("reserve_ledger")),
                # Batch F: measurement provenance (pre-action baseline + probe
                # config + post-action samples) survives into the evidence.
                pre_action_baseline=last.get("pre_action_baseline"),
                probe_config=self._probe_cfg().to_dict(),
                measurement_samples=tuple(last.get("measurement_samples")
                                          or ()),
                cold_start_source=(copy.deepcopy(_trusted_cal.get(
                    "cold_start_source")) if _trusted_cal.get(
                    "cold_start_source") else None),
                cycle_id=last.get("cycle_id"),
                proposal_id=last.get("proposal_id"),
                actuation_trial_id=last.get("actuation_trial_id"),
                authorized_revision_id=(
                    (last.get("authorization") or {}).get(
                        "authorized_revision_id")),
                requested_action=last.get("requested_action") or {},
                clipped_action=applied_map,
                canonical_action=last.get("canonical_action") or {},
                canonical_action_hash=last.get("canonical_action_hash") or "",
                snapshot=last.get("snapshot") or {},
                snapshot_readback_time=last.get("snapshot_readback_time"),
                action_apply_time=last.get("action_apply_time"),
                # Batch G (P1-6): a rollback / technical terminal must PRESERVE the
                # monotonic apply timestamp (the last cycle's) - it was omitted
                # here, so a non-commit terminal lost the real apply-time anchor.
                action_apply_monotonic_s=last.get("action_apply_monotonic_s"),
                final_readback=(last.get("final_readback")
                                if last.get("final_readback") is not None
                                else last.get("restore_readback")),
                final_readback_time=last.get("final_readback_time"),
                observations=observations,
                # prefer the cycle verdicts; fall back to the result-level joint
                # verdicts for a no-cycle terminal (e.g. the P0-9 already-
                # satisfied shortcut records the typed joint evidence there).
                monitor_verdicts=(last.get("monitor_verdicts")
                                  or result.get("monitor_verdicts") or {}),
                rollback_result=last.get("rollback_result"),
                authorization=last.get("authorization"),
                # prefer the cycle verdict; fall back to the result-level verdict
                # for episodes that never ran a cycle (e.g. an intent-parse
                # schema rejection) so schema_valid is surfaced honestly (P0-5).
                schema_valid=(last.get("schema_valid")
                              if "schema_valid" in last
                              else result.get("schema_valid")),
                schema_reject_reason=(last.get("schema_reject_reason")
                                      if "schema_reject_reason" in last
                                      else result.get("schema_reject_reason")),
                commit_check=result.get("commit_check"),
                # P0-8 / review bug: re-entry provenance from the TRUSTED source
                # (None for a direct operator episode).
                trigger_context=(copy.deepcopy(trusted_trigger)
                                 if trusted_trigger else None),
                terminal_outcome=outcome.value,
                terminal_reason=reason.value,
            ), None
        except Exception as e:
            logger.warning(f"evidence construction failed: {e}")
            return None, str(e)

    def _minimal_evidence(self, result: Dict, outcome: TerminalOutcome,
                          reason: TerminalReason,
                          error: str) -> EvidenceRecord:
        """A minimal, auditable evidence bundle for the fail-closed downgrade
        path. Carries the episode ids (best-effort) + the error, and its OWN
        terminal fields MATCH the downgraded (outcome, reason)."""
        return EvidenceRecord(
            experiment_run_id=(result.get("experiment_run_id")
                               or getattr(self, "experiment_run_id", "")
                               or "unknown"),
            episode_id=result.get("episode_id") or "unknown",
            fsm_step_id=result.get("fsm_step_id") or "unknown",
            # REQUIRED (Batch E): reuse the EXACT preallocated identity (always
            # present via _begin_episode); never invent a second id here.
            evidence_record_id=result.get("evidence_record_id"),
            proposer_id="unknown",
            model_version="unknown",
            intent_set_version="unknown",
            pending_intent_hash="unknown",
            # Batch G (P1-6): the last cycle's real prompt hash if a proposal ran
            # (None for a no-proposal downgrade) - never fabricated.
            prompt_hash=(result.get("cycles") or [{}])[-1].get("prompt_hash"),
            error=f"evidence_construction_failed: {error}",
            # review bug: TRUSTED re-entry provenance (tx / coordinator ctx),
            # never the mutable result - even the fail-closed downgrade bundle.
            trigger_context=(lambda t: copy.deepcopy(t) if t else None)(
                self._resolve_trigger_context()),
            terminal_outcome=outcome.value,
            terminal_reason=reason.value,
        )

    def _proposer_context(self):
        """(proposer_id, model_version) for THIS cycle (P0-6 / P0-16).

        Reads the PINNED ProposerContext frozen at the cycle's proposal
        boundary, so evidence/calibration/history attribution bind the cycle's
        ORIGINAL proposer+model even if the manager's active backend was
        externally mutated or a switch was requested mid-cycle. Falls back to
        the live active backend name only when nothing is pinned (idle / a
        __new__ test skeleton)."""
        ctx = getattr(self, "_proposer_ctx", None)
        if ctx is not None:
            return (ctx.proposer_id or "unknown",
                    ctx.model_version or ctx.proposer_id or "unknown")
        mgr = getattr(self, "llm_manager", None)
        name = None
        if mgr is not None:
            try:
                name = mgr.active_backend_name()
            except Exception:
                name = None
        name = name or "unknown"
        return name, name

    def _intent_content_hash(self, intent) -> str:
        """Stable content hash of the FULL semantic content of one intent (P0-6):
        type + target (kpi/constraint/value/unit) + scope (ue/bs/cell ids +
        area) - NOT the id/status.  An in-place target/scope mutation changes
        this hash."""
        if intent is None:
            return ""
        t = getattr(intent, "target", None)
        s = getattr(intent, "scope", None)
        def _ids(attr):
            return sorted(map(str, getattr(s, attr, None) or [])) if s else []
        payload = {
            "type": getattr(getattr(intent, "type", None), "value",
                            str(getattr(intent, "type", ""))),
            "kpi": getattr(t, "kpi_name", None) if t else None,
            "constraint": getattr(getattr(t, "constraint_type", None), "value",
                                  None) if t else None,
            "value": (float(t.target_value) if t is not None
                      and getattr(t, "target_value", None) is not None else None),
            "unit": getattr(t, "unit", None) if t else None,
            "scope": {"ue": _ids("ue_ids"), "bs": _ids("bs_ids"),
                      "cell": _ids("cell_ids"),
                      "area": getattr(s, "area_id", None) if s else None},
        }
        return _cb.content_hash(payload, prefix="ic-")

    def _intent_set_version(self) -> str:
        """A stable version tag for the current monitored intent set (P0-6).

        Hashes the FULL semantic content of every monitored intent (id + content
        hash + status), so it changes not only on add/remove/status change but
        also on an IN-PLACE target/scope MUTATION of any monitored intent (which
        a commit-time re-check must detect)."""
        mgr = getattr(self, "intent_manager", None)
        if mgr is None:
            # deliberately-missing manager: only a non-production test skeleton.
            monitored = []
        else:
            # FAIL CLOSED (coordinator review): a present manager whose
            # get_monitored raises / is unusable must NOT be swallowed and
            # hashed as an empty set - that would make a repeated dependency
            # failure look like an UNCHANGED intent set at commit.  Let it
            # propagate (pre-write -> zero writes; settlement -> caught by the
            # settlement backstop and rolled back once).
            monitored = mgr.get_monitored()
        sig = "|".join(sorted(
            f"{getattr(i, 'id', '')}:"
            f"{getattr(getattr(i, 'status', None), 'name', '')}:"
            f"{self._intent_content_hash(i)}"
            for i in monitored))
        return "iset-" + hashlib.sha1(sig.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _hash_intent_basis(basis) -> str:
        """The canonical pending-intent digest (pi-...) of an intent basis dict
        (Intent.to_dict() OR the raw-text fallback). Shared so a per-CYCLE frozen
        current_intent snapshot and the result-level fallback produce the SAME
        digest for the SAME content."""
        payload = json.dumps(basis, sort_keys=True, default=str)
        return "pi-" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def _pending_intent_hash(self, result: Dict) -> str:
        """Content hash of the pending intent (P0-6). Prefers the structured
        parsed intent; falls back to the raw text when parsing did not
        complete (e.g. a rejected/malformed input)."""
        basis = result.get("parsed_intent")
        if basis is None:
            basis = {"intent_text": result.get("intent_text", "")}
        return self._hash_intent_basis(basis)

    def _transition(self, state: str):
        """Transition the FSM, validated against the total legal (state, event)
        table (P0-7).

        The call sites still name the TARGET state; this resolves the unique
        event that connects (current -> target) and rejects anything the table
        does not permit, so an illegal transition or an unknown state can never
        silently CREATE a state.  The failsafe sink (TECHNICAL_FAILSAFE) and the
        idle reset (S0) are universal targets.  A per-episode FSM step budget
        bounds runaway loops.  An illegal transition / step-budget overflow
        latches TechnicalFailsafe and raises IllegalTransition (the caller's
        handler finalises the episode)."""
        old_state = self.current_state
        target = FSMState.from_name(state)

        # Entering the failsafe sink is ALWAYS legal (a broken invariant must be
        # able to fail closed from any state), including an idempotent re-entry
        # from the sink itself; not subject to the step budget.
        if target is FSMState.TECHNICAL_FAILSAFE:
            self._enter_state(old_state, state)
            return

        # Unknown / corrupt CURRENT state must latch and raise - NEVER assume S0
        # (a corrupt state cannot be trusted to reason about the next move).
        cur = FSMState.from_name(old_state)
        if cur is None:
            self._fail_fsm(f"corrupt current FSM state {old_state!r}")
            raise IllegalTransition(f"corrupt current FSM state {old_state!r}")

        # TechnicalFailsafe is a PERSISTENT SINK (P0-7): only clear_safety_latch()
        # after a verified recovery may leave it (via _force_state).  Any
        # ordinary transition out of it - including RESET/S0 - is illegal.
        if cur is FSMState.TECHNICAL_FAILSAFE:
            self._fail_fsm(f"illegal transition out of TechnicalFailsafe sink "
                           f"to {state!r}")
            raise IllegalTransition(
                f"TechnicalFailsafe is a persistent sink; cannot transition "
                f"to {state!r} (only verified recovery may)")

        if target is None:
            # never silently create a state (P0-7)
            self._fail_fsm(f"unknown FSM state {state!r}")
            raise IllegalTransition(f"unknown FSM state {state!r}")

        steps = getattr(self, "_fsm_steps", 0) + 1
        self._fsm_steps = steps
        max_steps = int(getattr(self, "max_fsm_steps", 256) or 256)
        if steps > max_steps:
            self._fail_fsm(f"max FSM step count {max_steps} exceeded")
            raise IllegalTransition(
                f"max FSM step count {max_steps} exceeded")

        try:
            event_for_target(cur, target)          # validate the edge (P0-7)
        except IllegalTransition as e:
            self._fail_fsm(str(e))
            raise

        self._enter_state(old_state, state)

    def _enter_state(self, old_state: str, state: str) -> None:
        """Commit a state change + fire notifications (used by the validated
        _transition and by the failsafe-sink / privileged-recovery paths)."""
        self.current_state = state
        logger.debug(f"State: {old_state} -> {state}")
        self._notify_state(old_state, state)

    def _force_state(self, state) -> None:
        """PRIVILEGED state set - the ONLY legal way to LEAVE the
        TechnicalFailsafe sink, used exclusively by clear_safety_latch() AFTER a
        verified operator recovery (P0-7).  Bypasses the transition table."""
        name = state.value if isinstance(state, FSMState) else str(state)
        old = self.current_state
        self.current_state = name
        self._notify_state(old, name)

    def _notify_state(self, old_state: str, state: str) -> None:
        """Fire the GUI state update + the on_state_change hook.

        Neither is isolated: the S4 STATE-transition notification is a
        safety-relevant hook, so a failure AFTER the first write fails CLOSED
        through the transaction backstop (an exactly-once rollback in the
        cycle's finally) rather than being swallowed (Batch B)."""
        if self.gui:
            self.gui.update_state(state)
        if self.on_state_change:
            self.on_state_change(old_state, state)

    def _fail_fsm(self, reason: str) -> None:
        """Latch TechnicalFailsafe for an illegal transition / unknown event /
        step-budget overflow (P0-7).  Pins the FSM in S_TECHNICAL_FAILSAFE and
        engages the persistent latch so no further negotiation/actuation runs."""
        logger.error(f"FSM fail-closed: {reason}")
        try:
            self._latch_failsafe({"restore_error": reason,
                                  "fsm_reason": reason})
        except Exception as e:
            logger.error(f"failsafe latch during FSM failure failed: {e}")
            self.current_state = S_TECHNICAL_FAILSAFE

    # ------------------------------------------------------------------
    # Batch C bounded execution: episode deadline + timed external calls
    # ------------------------------------------------------------------

    def _deadline_expired(self) -> bool:
        """True iff an ABSOLUTE episode deadline is set and has passed.  Returns
        False when no deadline is set (direct-call unit skeletons), so those
        keep working unbounded."""
        d = getattr(self, "_deadline", None)
        return d is not None and d.expired()

    def _bounded_call(self, fn: Callable[[], object], timeout_s: float,
                      label: str):
        """Run one external call with a timeout no greater than the remaining
        episode time (P0-7).  Without a deadline (direct unit calls) the call
        runs inline unbounded, preserving existing behaviour.  Raises
        OperationTimeout if the call does not return within its bound - so a
        non-returning dependency cannot defeat the wall-clock bound."""
        d = getattr(self, "_deadline", None)
        if d is None:
            return fn()
        return call_with_timeout(fn, d.op_timeout(timeout_s), label=label)

    def _safe_gui(self, method: str, *args) -> None:
        """Invoke a PURE-DISPLAY GUI callback with its exception ISOLATED
        (Batch B callback isolation).

        GUI display updates (log, llm-analysis, calibration panels) are
        genuinely optional side-effects: a flaky GUI must never abort the
        control transaction, so their exceptions are caught and logged here.
        This is DISTINCT from (a) decision-bearing policy callbacks
        (negotiation), which stay separate and are never silently accepted,
        and (b) the S4 STATE transition / on_state_change hook, whose failure
        after the first write fails CLOSED through the transaction backstop
        (an exactly-once rollback) rather than being swallowed.
        """
        gui = getattr(self, "gui", None)
        if gui is None:
            return
        fn = getattr(gui, method, None)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as e:
            logger.warning(f"GUI callback {method} failed (isolated): {e}")

    def _log_gui(self, message: str):
        """Log message (logger always; GUI display isolated via _safe_gui)."""
        logger.info(message)
        self._safe_gui("log", message)

    def set_llm_backend(self, backend) -> bool:
        """Switch LLM backend (accepts an LLMBackendType or a name string,
        including local models like 'local:qwen3-coder').

        Batch E (P0-16): the switch is only immediate when the coordinator is
        IDLE. While a cycle is active it is QUEUED (non-blocking) and applied
        atomically at the NEXT proposal boundary, so the in-flight cycle's
        parse/feasibility/revision and all its terminal evidence keep producing
        the cycle's ORIGINAL proposer/model. Returns True when applied OR
        queued; False when an idle apply failed (fail-closed, no partial
        change)."""
        target = self._switch_target_name(backend)
        self._ensure_switch_state()
        if getattr(self, "_episode_in_flight", False):
            # active: enqueue an audited request. The mutex is held ONLY to
            # append (never while calling the model/backend). The public audit
            # record holds JSON-SAFE PRIMITIVES ONLY - the raw enum/object target
            # lives in a SEPARATE internal map keyed by request id.
            rid = new_request_id()
            req = {"request_id": rid,
                   "requested_at": datetime.now().isoformat(),
                   "target": target,
                   "status": SWITCH_PENDING, "applied_at": None,
                   "episode_id": None, "cycle_index": None, "cycle_id": None,
                   "proposal_id": None, "first_effective_cycle": None,
                   "superseded_by": None, "error": None}
            with self._switch_lock:
                self._switch_raw_targets[rid] = backend
                self._switch_queue.append(req)
                self._switch_audit.append(req)
            logger.info(f"backend switch to {target!r} QUEUED "
                        f"(cycle active) - request {rid}")
            return True
        # idle: apply immediately (compatible with the historical behaviour).
        ok = False
        try:
            ok = bool(self.llm_manager.set_backend(backend))
        except Exception as e:
            logger.error(f"idle backend switch to {target!r} failed: {e}")
            ok = False
        # an idle switch may clear a stale pin (no episode is in flight, so
        # evidence/history attribution for the prior episode already finished);
        # the next episode re-pins at its first boundary.
        if ok:
            self._proposer_ctx = None
        with self._switch_lock:
            self._switch_audit.append({
                "request_id": new_request_id(),
                "requested_at": datetime.now().isoformat(),
                "target": target,
                "status": SWITCH_APPLIED if ok else SWITCH_FAILED,
                "applied_at": datetime.now().isoformat() if ok else None,
                "episode_id": None, "cycle_index": None, "cycle_id": None,
                "proposal_id": None, "first_effective_cycle": None,
                "superseded_by": None,
                "error": None if ok else "idle set_backend returned false"})
        return ok

    def _ensure_switch_state(self) -> None:
        """Lazily materialize the switch queue/audit/lock (safe on __new__-built
        test skeletons that skip __init__)."""
        if getattr(self, "_switch_lock", None) is None:
            self._switch_lock = threading.Lock()
        if getattr(self, "_switch_audit", None) is None:
            self._switch_audit = []
        if getattr(self, "_switch_queue", None) is None:
            self._switch_queue = []
        if getattr(self, "_switch_raw_targets", None) is None:
            self._switch_raw_targets = {}

    @staticmethod
    def _switch_target_name(backend) -> str:
        """A stable, JSON-safe name for a switch target (enum or string)."""
        return str(getattr(backend, "value", backend))

    def get_switch_audit(self) -> List[Dict]:
        """A deep copy of the full backend-switch audit trail (P0-16), read
        UNDER the switch mutex so a concurrent settlement publish is never
        observed half-applied. JSON-safe primitives only (no enum/object)."""
        self._ensure_switch_state()
        with self._switch_lock:
            return copy.deepcopy(list(self._switch_audit))

    # ------------------------------------------------------------------
    # Batch E (P0-16): the PROPOSAL BOUNDARY - drain queued switches, apply the
    # last atomically, then PIN the actual backend object for the whole cycle.
    # ------------------------------------------------------------------

    def _open_proposal_boundary(self, cycle_index: int,
                                cycle_id: Optional[str],
                                episode_id: Optional[str]) -> ProposerContext:
        """Drain the switch queue, apply the resulting target atomically, and
        PIN the ACTUAL active backend object for this cycle.

        Deterministic multi-request semantics: queued requests are FIFO; all but
        the LAST are audited COALESCED (superseded_by the last, first_effective
        cycle None - never applied) and only the last is APPLIED - one atomic
        flip per boundary. A failed/unavailable apply fails CLOSED: the manager
        selection is restored, the PREVIOUS pinned handle is RETAINED (never a
        re-read of a partially-mutated manager), and the request is audited
        FAILED with no first-effective cycle. The backend call happens OUTSIDE
        the mutex; the settlement fields are PUBLISHED atomically UNDER it."""
        self._ensure_switch_state()
        # 1) drain the queue under the mutex ONLY (no model/backend call held).
        with self._switch_lock:
            drained: List[Dict] = list(self._switch_queue)
            self._switch_queue = []
        if not drained:
            # no queued switch: pin the current active backend for this cycle.
            return self._pin_active_backend(cycle_index, cycle_id, episode_id,
                                            None)
        last = drained[-1]
        raw_target = self._switch_raw_targets.get(last["request_id"], None)
        # 2) apply the LAST request OUTSIDE the mutex (transactional; restore the
        #    manager selection on any failure so there is never a partial flip).
        mgr = getattr(self, "llm_manager", None)
        snap = None
        snap_fn = getattr(mgr, "active_selection_snapshot", None)
        if callable(snap_fn):
            try:
                snap = snap_fn()
            except Exception:
                snap = None
        ok = False
        err = None
        try:
            ok = bool(mgr.set_backend(raw_target))
        except Exception as e:
            err = str(e)
            ok = False
        if not ok:
            # fail-closed: restore any partial mutation (a fake/legacy manager
            # that mutated then raised does not self-restore).
            restore_fn = getattr(mgr, "restore_active_selection", None)
            if snap is not None and callable(restore_fn):
                try:
                    restore_fn(snap)
                except Exception as re:
                    logger.error(f"selection restore after failed switch "
                                 f"failed: {re}")
        applied_at = datetime.now().isoformat() if ok else None
        # 3) PUBLISH the settlement fields atomically UNDER the mutex.
        with self._switch_lock:
            for sup in drained[:-1]:
                sup["status"] = SWITCH_COALESCED
                sup["superseded_by"] = last["request_id"]
                sup["applied_at"] = None
                sup["first_effective_cycle"] = None    # never applied
            last["episode_id"] = episode_id
            last["cycle_index"] = cycle_index
            last["cycle_id"] = cycle_id
            if ok:
                last["status"] = SWITCH_APPLIED
                last["applied_at"] = applied_at
                last["first_effective_cycle"] = cycle_index
            else:
                last["status"] = SWITCH_FAILED
                last["applied_at"] = None
                last["first_effective_cycle"] = None   # failed => no effect
                last["error"] = err or "set_backend returned false (fail-closed)"
            for rid in [d["request_id"] for d in drained]:
                self._switch_raw_targets.pop(rid, None)
        if ok:
            # apply succeeded: pin the NEW active backend object for this cycle.
            return self._pin_active_backend(cycle_index, cycle_id, episode_id,
                                            last["request_id"])
        # apply FAILED: RETAIN the previous pinned handle (do NOT re-read the
        # manager); just refine the cycle provenance onto it.
        logger.error(f"queued backend switch to {last['target']!r} FAILED at "
                     f"boundary - retaining previous pinned backend")
        if getattr(self, "_proposer_ctx", None) is not None:
            self._refine_pin_cycle(cycle_index, cycle_id)
            return self._proposer_ctx
        # no prior pin (only possible if the very first boundary's switch failed):
        # pin whatever the (restored) manager still exposes.
        return self._pin_active_backend(cycle_index, cycle_id, episode_id, None)

    def _pin_active_backend(self, cycle_index: int, cycle_id: Optional[str],
                            episode_id: Optional[str],
                            applied_request_id: Optional[str]
                            ) -> ProposerContext:
        """Resolve the manager's CURRENT active backend HANDLE and freeze it
        into an immutable ProposerContext. Re-partitions Batch D calibration
        to the (new) pinned model when it differs from the frozen partition."""
        mgr = getattr(self, "llm_manager", None)
        obj = name = model_version = None
        if mgr is not None:
            try:
                obj = mgr.active_backend_object()
            except Exception:
                obj = None
            try:
                name = mgr.active_backend_name()
            except Exception:
                name = None
            try:
                model_version = mgr.active_model_version()
            except Exception:
                model_version = None
        name = name or "unknown"
        model_version = model_version or name
        ctx = ProposerContext(
            proposer_id=name, backend_name=name, model_version=model_version,
            backend_object=obj, cycle_index=cycle_index, cycle_id=cycle_id,
            episode_id=episode_id, applied_request_id=applied_request_id)
        self._proposer_ctx = ctx
        # A hot-swap means the NEXT cycle uses the newly applied model + ITS OWN
        # cold/restored calibration partition. Re-freeze the Batch D (model,
        # regime) context iff the pinned model differs from the frozen one. The
        # frozen N_max budget (captured at episode start) is deliberately NOT
        # recomputed here (it bounds total consultations across the episode).
        prev = getattr(self, "_episode_context", None)
        if prev is not None and prev[0] != model_version:
            self._episode_context = None
            self._freeze_episode_context()
        return ctx

    def _refine_pin_cycle(self, cycle_index: int,
                          cycle_id: Optional[str]) -> None:
        """Attach this cycle's cycle_id to the EXISTING pin WITHOUT re-reading
        the manager (so an external active-backend mutation cannot leak into a
        cycle that was already pinned at the pre-parse boundary)."""
        ctx = getattr(self, "_proposer_ctx", None)
        if ctx is None:
            return
        self._proposer_ctx = ProposerContext(
            proposer_id=ctx.proposer_id, backend_name=ctx.backend_name,
            model_version=ctx.model_version, backend_object=ctx.backend_object,
            cycle_index=cycle_index, cycle_id=cycle_id,
            proposal_id=ctx.proposal_id, episode_id=ctx.episode_id,
            applied_request_id=ctx.applied_request_id)

    def _bind_proposal_id(self, proposal_id: Optional[str]) -> None:
        """When S2 mints this cycle's proposal_id, refine the EXISTING pin with
        it WITHOUT changing the backend handle, and stamp it onto the APPLIED
        switch-audit record for this cycle (exact proposal linkage, P0-16)."""
        ctx = getattr(self, "_proposer_ctx", None)
        if ctx is not None and proposal_id:
            self._proposer_ctx = ctx.with_proposal(proposal_id)
        # stamp the applied audit record for this pinned cycle (if a switch was
        # applied at this cycle's boundary).
        applied_rid = getattr(self._proposer_ctx, "applied_request_id", None) \
            if getattr(self, "_proposer_ctx", None) is not None else None
        if applied_rid and getattr(self, "_switch_lock", None) is not None:
            with self._switch_lock:
                for rec in getattr(self, "_switch_audit", []):
                    if rec.get("request_id") == applied_rid:
                        rec["proposal_id"] = proposal_id
                        break

    def _pinned_backend_obj(self):
        """The pinned backend handle for this cycle, or None (fall back to the
        manager's active backend)."""
        ctx = getattr(self, "_proposer_ctx", None)
        return ctx.backend_object if ctx is not None else None

    def _pinned_generate(self, prompt: str, system_prompt: str = ""):
        """generate() served by the PINNED backend handle when available."""
        obj = self._pinned_backend_obj()
        if obj is not None:
            return self.llm_manager.generate_with(obj, prompt, system_prompt)
        return self.llm_manager.generate(prompt, system_prompt)

    def _pinned_analyze_feasibility(self, active_dicts, new_dict, state_dict,
                                    history):
        """analyze_feasibility() served by the PINNED backend handle."""
        obj = self._pinned_backend_obj()
        if obj is not None:
            return self.llm_manager.analyze_feasibility_with(
                obj, active_dicts, new_dict, state_dict, history)
        return self.llm_manager.analyze_feasibility(
            active_dicts, new_dict, state_dict, history)

    def _pinned_generate_alternatives(self, active, failed, state):
        """generate_alternatives() served by the PINNED backend handle."""
        obj = self._pinned_backend_obj()
        if obj is not None:
            return self.llm_manager.generate_alternatives_with(
                obj, active, failed, state)
        return self.llm_manager.generate_alternatives(active, failed, state)

    # ------------------------------------------------------------------
    # Batch E (P0-15): history-ablation snapshot / restore / reset APIs.
    # ------------------------------------------------------------------

    def set_history_mode(self, mode) -> HistoryMode:
        """Fail-closed set of the history-ablation mode (an invalid mode
        RAISES rather than silently falling through to 'use history')."""
        self.history_mode = HistoryMode.coerce(mode)
        return self.history_mode

    def history_snapshot(self) -> Tuple[HistoryRecord, ...]:
        """A DEFENSIVE IMMUTABLE snapshot of the live reservoir (cannot be
        mutated or aliased back in)."""
        return snapshot_records(self.history_reservoir)

    def restore_history_snapshot(self, snapshot) -> None:
        """Rebuild the live reservoir from a snapshot into a FRESH list (the
        caller's / frozen snapshot is never mutated or aliased)."""
        self.history_reservoir = restore_records(snapshot)

    def set_frozen_history(self, snapshot) -> None:
        """Install the FROZEN-mode retrieval dataset from a defensive immutable
        snapshot (never learned into)."""
        self._frozen_history = snapshot_records(snapshot)

    def clear_history(self) -> None:
        """Clear the live reservoir ONLY (calibration is reset separately)."""
        self.history_reservoir = []

    def reset_history(self) -> None:
        """Reset BOTH the live reservoir and the frozen retrieval dataset
        (distinct from calibration reset)."""
        self.history_reservoir = []
        self._frozen_history = tuple()

    def refresh_llm_backends(self) -> list:
        """Re-discover local (LiteLLM) models; returns all selectable names."""
        self.llm_manager.refresh_dynamic()
        return self.llm_manager.get_available_names()

    def get_stats(self) -> Dict:
        """Get coordinator statistics"""
        return {
            "session_id": self.session_id,
            "active_intents": len(self.intent_manager.get_active()),
            "monitored_intents": len(self.intent_manager.get_monitored()),
            "history_size": len(self.history_reservoir),
            "calibration": self.calibrator.get_stats(),
            "llm_backend": self.llm_manager.active_backend_name(),
            "available_backends": self.llm_manager.get_available_names()
        }
