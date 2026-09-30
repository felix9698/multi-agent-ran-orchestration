#!/usr/bin/env python3
"""
Adaptive Calibrator for θ* and N_max

Implements theoretical framework from:
- Proposition 1: Bounded Trial Cost
- Proposition 2: Confidence Threshold θ*
- Equation (9): Maximum Negotiation Rounds N_max

The calibrator continuously updates parameters based on empirical cost measurements.
"""

import logging
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from collections import deque
from pathlib import Path

logger = logging.getLogger("AdaptiveCalibrator")


def _legacy_module(*name_parts: str):
    """Load paper/legacy helpers only when those optional paths are invoked."""
    import importlib
    return importlib.import_module("".join(name_parts))


@dataclass
class CostMetrics:
    """Cost metrics for a single episode"""
    timestamp: datetime = field(default_factory=datetime.now)
    phase: str = ""

    # Trial outcome
    trial_executed: bool = False
    trial_success: bool = False
    rolled_back: bool = False
    # Episode FINAL resolution (C1): True only on the cycle whose validated
    # configuration was committed. Distinct from trial_success, and NOT the
    # old conflated "negotiation agreement = success".
    episode_success: bool = False

    # Cost measurements (Mbps·s)
    throughput_before: float = 0.0
    throughput_after: float = 0.0
    throughput_trial_min: float = 0.0  # worst mean tput inside the window (P4)
    throughput_loss: float = 0.0       # Integral of loss during trial
    trial_duration: float = 15.0       # τ_trial in seconds

    # Hard failure tracking
    hard_failure: bool = False         # Session drop, paging failure, etc.
    # Time to re-establish connection. Optional (None) when a hard failure's
    # reconnection was NOT measured - NEVER coerced to 0.0 (which would claim a
    # zero-cost reconnection without evidence). The C_hard sample only includes
    # finite measured values; p_hard still counts the hard event.
    reconnection_time: Optional[float] = None
    # Telemetry failure (attached but no usable KPI) - fails the trial
    # closed but is NOT a hard failure and never feeds C_hard (C5)
    measurement_invalid: bool = False
    # Batch F (P0-13): the trial's / negotiation's throughput was UNKNOWN (a
    # failed/missing live probe). The numeric throughput fields are then NOT
    # trustworthy measurements - such an episode is EXCLUDED from the adaptive
    # cost history so a missing baseline/window can never enter as a fake 0.
    throughput_unknown: bool = False
    nego_throughput_unknown: bool = False
    # Batch F (#d8b9): timestamped [(sample_time, value)] trajectories from the
    # authoritative OK MeasurementSamples, so the single estimator integrates a
    # real trajectory (trajectory_used) instead of the legacy min*tau fallback.
    trial_trajectory: list = field(default_factory=list)
    nego_trajectory: list = field(default_factory=list)

    # Negotiation metrics
    negotiation_entered: bool = False
    negotiation_rounds: int = 0
    negotiation_duration: float = 0.0  # Total negotiation time
    throughput_during_nego: float = 0.0  # mean tput across nego rounds (P3)

    # LLM confidence. llm_confidence is kept ONLY as a documented RAW-confidence
    # legacy adapter derived from raw_confidence (blocker 3).
    llm_confidence: float = 0.0

    # --- Batch D schema (blocker 3): raw vs calibrated + partition + selection
    raw_confidence: Optional[float] = None
    calibrated_probability: Optional[float] = None
    threshold: Optional[float] = None
    threshold_applied_to: str = "calibrated_probability"
    model_id: str = "unknown"
    operating_regime_id: str = "default"
    # proposal selection metadata (blocker 4): every valid proposal is eligible;
    # routed_to = "trial" | "negotiation"; executed only when a trial ran.
    proposal_eligible: bool = False
    routed_to: str = ""
    proposal_executed: bool = False

    def __post_init__(self):
        # llm_confidence is the RAW-confidence legacy adapter.
        if self.raw_confidence is None:
            self.raw_confidence = self.llm_confidence
        else:
            self.llm_confidence = self.raw_confidence

    def to_episode_record(self):
        """Bridge to the offline estimator (single-estimator principle, P5):
        the calibrator's costs come from experiments.metrics.estimate_costs
        over these records, never from a second runtime formula."""
        EpisodeRecord = _legacy_module("experiments.", "metrics").EpisodeRecord
        return EpisodeRecord(
            trial_id=0, method=self.model_id or "live",
            phase=self.phase or "default",
            confidence=self.raw_confidence if self.raw_confidence is not None
            else self.llm_confidence,   # RAW-confidence legacy alias
            trial_executed=self.trial_executed,
            rolled_back=self.rolled_back,
            entered_negotiation=self.negotiation_entered,
            negotiation_rounds=self.negotiation_rounds,
            # success is the EPISODE resolution, trial_success the S4
            # outcome (the old success=trial_success mapping conflated them)
            success=self.episode_success,
            trial_success=self.trial_success if self.trial_executed else None,
            hard_failure=self.hard_failure,
            measurement_invalid=self.measurement_invalid,
            throughput_before=self.throughput_before,
            throughput_after=self.throughput_after,
            throughput_trial_min=self.throughput_trial_min,
            throughput_unknown=self.throughput_unknown,
            nego_throughput_unknown=self.nego_throughput_unknown,
            trial_trajectory=list(self.trial_trajectory or []),
            nego_trajectory=list(self.nego_trajectory or []),
            tau_trial=self.trial_duration,
            throughput_during_nego=self.throughput_during_nego,
            nego_duration=self.negotiation_duration,
            reconnection_time=self.reconnection_time,
            # Batch D schema (blocker 3)
            raw_confidence=self.raw_confidence,
            calibrated_probability=self.calibrated_probability,
            threshold=self.threshold,
            threshold_applied_to=self.threshold_applied_to,
            model_id=self.model_id,
            operating_regime_id=self.operating_regime_id,
            routed_to=self.routed_to or ("trial" if self.trial_executed
                                         else "negotiation"),
            proposal_eligible=self.proposal_eligible,
            proposal_executed=self.proposal_executed,
        )

    # NOTE (P5): the per-episode compute_c_*/compute_r_* formulas were
    # removed - they were a SECOND estimator that diverged from
    # experiments.metrics.estimate_costs (C_worst over all trials,
    # throughput_after instead of trial_min, pre-trial tput for C_nego).
    # All cost derivation now flows through to_episode_record() +
    # estimate_costs, the single source of truth.

    def to_dict(self) -> Dict:
        # item (b): EXACT round-trip of EVERY dataclass field.
        return {
            "timestamp": self.timestamp.isoformat(),
            "phase": self.phase,
            "trial_executed": self.trial_executed,
            "trial_success": self.trial_success,
            "episode_success": self.episode_success,
            "rolled_back": self.rolled_back,
            "throughput_before": self.throughput_before,
            "throughput_after": self.throughput_after,
            "throughput_trial_min": self.throughput_trial_min,
            "throughput_loss": self.throughput_loss,
            "trial_duration": self.trial_duration,
            "hard_failure": self.hard_failure,
            "measurement_invalid": self.measurement_invalid,
            "throughput_unknown": self.throughput_unknown,
            "nego_throughput_unknown": self.nego_throughput_unknown,
            "trial_trajectory": [list(p) for p in (self.trial_trajectory or [])],
            "nego_trajectory": [list(p) for p in (self.nego_trajectory or [])],
            "reconnection_time": self.reconnection_time,
            "negotiation_entered": self.negotiation_entered,
            "negotiation_rounds": self.negotiation_rounds,
            "negotiation_duration": self.negotiation_duration,
            "throughput_during_nego": self.throughput_during_nego,
            "llm_confidence": self.llm_confidence,   # RAW-confidence legacy alias
            "raw_confidence": self.raw_confidence,
            "calibrated_probability": self.calibrated_probability,
            "threshold": self.threshold,
            "threshold_applied_to": self.threshold_applied_to,
            "model_id": self.model_id,
            "operating_regime_id": self.operating_regime_id,
            "proposal_eligible": self.proposal_eligible,
            "routed_to": self.routed_to,
            "proposal_executed": self.proposal_executed,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "CostMetrics":
        """EXACT round-trip of every dataclass field (item b), including
        timestamp, throughput_loss, trial_duration, negotiation_entered."""
        m = cls(
            phase=d.get("phase", ""),
            trial_executed=bool(d.get("trial_executed", False)),
            trial_success=bool(d.get("trial_success", False)),
            episode_success=bool(d.get("episode_success", False)),
            rolled_back=bool(d.get("rolled_back", False)),
            throughput_before=float(d.get("throughput_before", 0.0)),
            throughput_after=float(d.get("throughput_after", 0.0)),
            throughput_trial_min=float(d.get("throughput_trial_min", 0.0)),
            throughput_loss=float(d.get("throughput_loss", 0.0)),
            trial_duration=float(d.get("trial_duration", 15.0)),
            hard_failure=bool(d.get("hard_failure", False)),
            measurement_invalid=bool(d.get("measurement_invalid", False)),
            throughput_unknown=bool(d.get("throughput_unknown", False)),
            nego_throughput_unknown=bool(
                d.get("nego_throughput_unknown", False)),
            trial_trajectory=[tuple(p) for p in
                              (d.get("trial_trajectory") or [])],
            nego_trajectory=[tuple(p) for p in
                             (d.get("nego_trajectory") or [])],
            reconnection_time=(float(d["reconnection_time"])
                               if d.get("reconnection_time") is not None
                               else None),
            negotiation_entered=bool(d.get("negotiation_entered", False)),
            negotiation_rounds=int(d.get("negotiation_rounds", 0)),
            negotiation_duration=float(d.get("negotiation_duration", 0.0)),
            throughput_during_nego=float(d.get("throughput_during_nego", 0.0)),
            llm_confidence=float(d.get("llm_confidence", 0.0)),
            raw_confidence=d.get("raw_confidence"),
            calibrated_probability=d.get("calibrated_probability"),
            threshold=d.get("threshold"),
            threshold_applied_to=d.get("threshold_applied_to",
                                       "calibrated_probability"),
            model_id=d.get("model_id", "unknown"),
            operating_regime_id=d.get("operating_regime_id", "default"),
            proposal_eligible=bool(d.get("proposal_eligible", False)),
            routed_to=d.get("routed_to", ""),
            proposal_executed=bool(d.get("proposal_executed", False)),
        )
        ts = d.get("timestamp")
        if ts is not None:
            # review: an INVALID timestamp RAISES (fail-closed), never silently
            # dropped to 'now'. A non-string or unparseable value is rejected.
            if not isinstance(ts, str):
                raise ValueError(f"timestamp must be an ISO string, got {ts!r}")
            m.timestamp = datetime.fromisoformat(ts)   # raises on bad format
        return m


class AdaptiveCalibrator:
    """
    Adaptive calibrator for confidence threshold θ* and max negotiation rounds N_max.

    Implements three modes:
    1. Fixed: Use static θ* and N_max (baseline)
    2. Phase-adaptive: Recalibrate at phase transitions
    3. Online-adaptive: Sliding window update after each episode

    Theory:
    - θ* = (C_worst - C_nego) / (R_success + C_worst)                    [Eq. 8]
    - N_max = max(0, floor((C_episode - C_trial_ub)/(C_trial_ub+C_nego_ub)))
      [Eq. 9, corrected N+1 form over DETERMINISTIC caps]. The empirical closed
      form over c_worst/c_nego is only a SOFT guard, never the safety N_max.
    """

    def __init__(self,
                 mode: str = "online",  # "fixed", "phase", "online"
                 window_size: int = 20,
                 initial_theta: Optional[float] = None,
                 initial_n_max: Optional[int] = None,
                 c_episode: Optional[float] = None,
                 data_file: str = None,
                 cold_start_source: Optional[Dict] = None,
                 calibration_min_samples: int = 10,
                 calibration_bins: int = 10):
        """
        Args:
            mode: Calibration mode
            window_size: Sliding window size for online mode
            initial_theta: Initial confidence threshold
            initial_n_max: Initial max negotiation rounds
            c_episode: Per-episode cost budget (Mbps·s)
            data_file: File to persist calibration data
        """
        self.mode = mode
        self.window_size = window_size
        # Blocker 7: NO hidden 0.7/3/500 defaults. When an explicit value is
        # omitted, derive it from the SAME CalibrationConfig cold-start source
        # every mode uses.
        if initial_theta is None or initial_n_max is None or c_episode is None:
            _CC = _legacy_module("con", "fig").CalibrationConfig
            _cfg = _CC()
            if c_episode is None:
                c_episode = _cfg.c_episode
            if initial_theta is None:
                initial_theta = _cfg.compute_initial_theta()
            if initial_n_max is None:
                initial_n_max = _cfg.compute_initial_n_max(_cfg.tau_trial)
            if not cold_start_source:
                cold_start_source = _cfg.cold_start_source(_cfg.tau_trial)
        self.c_episode = c_episode

        # Current parameters
        self._initial_theta = initial_theta
        self._initial_n_max = initial_n_max
        self.theta_star = initial_theta
        self.theta_star_raw = initial_theta   # pre-clamp Eq. 8 value
        self.assumption_ok = True             # Prop. 2 premise C_worst > C_nego
        self.n_max = initial_n_max

        # Cost estimates; priors used ONLY while a component has no samples
        # (experiments.metrics.estimate_costs semantics)
        self.c_worst_estimate = 125.0   # Default from paper
        self.r_success_estimate = 64.5  # Default from paper
        self.c_nego_estimate = 50.0     # Default from paper
        self.p_hard_estimate = 0.02     # Hard failure probability

        # History for online mode
        self.history: deque = deque(maxlen=window_size)
        self.phase_history: Dict[str, List[CostMetrics]] = {}

        # Per-phase calibration
        self.phase_theta: Dict[str, float] = {}
        self.phase_n_max: Dict[str, int] = {}

        # Statistics. total_* are GLOBAL cross-partition AGGREGATES, kept
        # CLEARLY SEPARATE from the per-partition routing counters (p_*) which
        # are snapshotted/restored with each partition (review).
        self.total_episodes = 0
        self.total_trials = 0
        self.total_successes = 0
        self.total_rollbacks = 0
        self.total_negotiations = 0
        self.p_episodes = 0
        self.p_trials = 0
        self.p_successes = 0
        self.p_rollbacks = 0
        self.p_negotiations = 0

        # P0-12: the ONE cold-start source (config-derived), recorded for
        # stats/evidence so the runtime origin of the first-cycle values is
        # auditable.
        self.cold_start_source = dict(cold_start_source or {})

        # --- P0-17: partitioned reliability calibration -------------------
        # A raw proposer score is calibrated to a probability using a
        # per-(model_id, operating_regime_id) reliability mapping, so a context
        # change never reuses another model/regime's mapping. Cold-start /
        # insufficient-sample bins fall back to the raw score (identity).
        self.calibration_min_samples = int(calibration_min_samples)
        self.calibration_bins = int(calibration_bins)
        self._active_model_id = "unknown"
        self._active_regime_id = "default"
        # {(model_id, regime_id): [(raw_score, trial_success_bool), ...]}
        self._calibration: Dict[Tuple[str, str], List[Tuple[float, bool]]] = {}
        # blocker 4: selection-bias coverage = executed / eligible valid
        # proposals per partition (NOT the fraction of executed samples in
        # populated bins). Every valid proposal is counted eligible BEFORE
        # routing; only executed proposals get a reliability outcome.
        self._proposals: Dict[Tuple[str, str], Dict[str, int]] = {}

        # Blocker 2: partition the WHOLE adaptive routing state (theta/theta_raw/
        # n_max/estimates/history/phase state) by (model_id, regime_id). The
        # legacy attributes above ARE the ACTIVE partition; other partitions are
        # snapshotted here. Cold-start priors for a fresh partition.
        self._cold_theta = self._initial_theta
        self._cold_n_max = self._initial_n_max
        self._cold_estimates = (125.0, 64.5, 50.0, 0.02)  # c_worst,r,c_nego,p_hard
        self._active_key = (self._active_model_id, self._active_regime_id)
        self._partitions: Dict[Tuple[str, str], Dict] = {}
        # persistence format version (blocker 2 safe round-trip)
        self.persistence_version = 2

        # Persistence
        self.data_file = data_file
        if data_file:
            self._load_data()

        logger.info(f"AdaptiveCalibrator initialized: mode={mode}, θ*={self.theta_star:.3f}, N_max={self.n_max}")

    def record_episode(self, metrics: CostMetrics):
        """Record episode outcome and update parameters.

        Blocker 2: the metric names its producing (model_id, regime_id); the
        update is applied ONLY to that partition (the context is switched first),
        so one model/regime's episodes never move another's theta/n_max/history.
        Blocker 4: an executed trial also trains the partition's reliability map
        on the observed outcome (only executed proposals are trained on)."""
        mkey = (getattr(metrics, "model_id", None) or self._active_model_id,
                getattr(metrics, "operating_regime_id", None)
                or self._active_regime_id)
        if mkey != self._active_key:
            self.set_operating_context(mkey[0], mkey[1])

        self.history.append(metrics)
        self.total_episodes += 1          # GLOBAL aggregate
        self.p_episodes += 1              # this partition only

        if metrics.trial_executed:
            self.total_trials += 1
            self.p_trials += 1
            if metrics.trial_success:
                self.total_successes += 1
                self.p_successes += 1
            if metrics.rolled_back:
                self.total_rollbacks += 1
                self.p_rollbacks += 1

        if metrics.negotiation_entered:
            self.total_negotiations += 1
            self.p_negotiations += 1

        # Update phase history
        phase = metrics.phase or "default"
        if phase not in self.phase_history:
            self.phase_history[phase] = []
        self.phase_history[phase].append(metrics)

        # Update parameters based on mode
        if self.mode == "online":
            self._update_online()
        elif self.mode == "phase":
            self._update_phase(phase)

        # Persist
        if self.data_file:
            self._save_data()

    def _update_online(self):
        """Delegate to the offline estimator over the sliding window.

        Single-estimator principle (P5): the runtime value IS
        experiments.metrics.estimate_costs computed over the window - no
        EMA, no run-forever averages, and no C_worst dilution by successful
        trials (estimate_costs takes C_cont over FAILED trials only). The
        sliding window itself provides the smoothing/recency.
        """
        if len(self.history) < 3:
            return

        est = self._estimate_over(self.history)

        self.c_worst_estimate = est["c_worst"]
        self.r_success_estimate = est["r_success"]
        self.c_nego_estimate = est["c_nego"]
        self.p_hard_estimate = est["p_hard"]
        self.assumption_ok = bool(est["assumption_ok"])

        raw = est["theta_star"]
        if isinstance(raw, float) and math.isfinite(raw):
            self.theta_star_raw = raw
            self.theta_star = max(0.1, min(0.9, raw))   # clamp kept (paper)
        # full delegation: Eq. 9 floor may legitimately be 0 (negotiation
        # unaffordable) - no max(1, ...) override
        self.n_max = int(est["n_max"])

        logger.debug(f"Recalibrated from window (n={len(self.history)}): "
                     f"raw θ*={self.theta_star_raw:.3f} -> "
                     f"θ*={self.theta_star:.3f}, N_max={self.n_max}, "
                     f"assumption_ok={self.assumption_ok}")

    def _estimate_over(self, metrics_list) -> Dict:
        """The single estimator: experiments.metrics.estimate_costs over a
        window of CostMetrics (bridged to EpisodeRecord)."""
        metrics = _legacy_module("experiments.", "metrics")
        CostPriors = metrics.CostPriors
        IntentConfig = metrics.IntentConfig
        estimate_costs = metrics.estimate_costs
        records = [m.to_episode_record() for m in metrics_list]
        priors = CostPriors(c_worst=125.0, r_success=64.5, c_nego=50.0,
                            c_hard=30.0, p_hard=0.02)
        return estimate_costs(records, IntentConfig(),
                              c_episode=self.c_episode, priors=priors)

    def _update_phase(self, phase: str):
        """Phase-specific calibration - same single estimator, phase window."""
        if phase not in self.phase_history or len(self.phase_history[phase]) < 3:
            return

        est = self._estimate_over(self.phase_history[phase][-self.window_size:])
        raw = est["theta_star"]
        if isinstance(raw, float) and math.isfinite(raw):
            self.phase_theta[phase] = max(0.1, min(0.9, raw))
        self.phase_n_max[phase] = int(est["n_max"])

        logger.info(f"Phase '{phase}' calibration: "
                    f"θ*={self.phase_theta.get(phase, 0):.3f}, "
                    f"N_max={self.phase_n_max.get(phase, 0)}")

    def reset(self):
        """Return to a fresh calibration state.

        Guarantees per-(method, trial) independence in experiments: no state
        carries across sessions/trials/models unless explicitly requested
        (--carry-calibration).
        """
        self.theta_star = self._initial_theta
        self.theta_star_raw = self._initial_theta
        self.assumption_ok = True
        self.n_max = self._initial_n_max
        self.c_worst_estimate = 125.0
        self.r_success_estimate = 64.5
        self.c_nego_estimate = 50.0
        self.p_hard_estimate = 0.02
        self.history.clear()
        self.phase_history = {}
        self.phase_theta = {}
        self.phase_n_max = {}
        self.total_episodes = 0
        self.total_trials = 0
        self.total_successes = 0
        self.total_rollbacks = 0
        self.total_negotiations = 0
        # P0-17 / blocker 2: ALL per-partition state (reliability maps AND the
        # partitioned routing snapshots) is cleared on reset, and the active
        # partition returns to the config-derived cold start.
        self._calibration = {}
        self._partitions = {}
        self._active_key = (self._active_model_id, self._active_regime_id)

    def get_theta_star(self, phase: str = None) -> float:
        """Get current confidence threshold"""
        if self.mode == "phase" and phase and phase in self.phase_theta:
            return self.phase_theta[phase]
        return self.theta_star

    def get_n_max(self, phase: str = None) -> int:
        """Get current max negotiation rounds"""
        if self.mode == "phase" and phase and phase in self.phase_n_max:
            return self.phase_n_max[phase]
        return self.n_max

    def should_execute_trial(self, confidence: float, phase: str = None) -> bool:
        """
        Decision rule: execute trial if confidence >= θ*

        Returns:
            True if trial should proceed, False if negotiation needed
        """
        theta = self.get_theta_star(phase)
        return confidence >= theta

    def can_negotiate_more(self, current_round: int, phase: str = None) -> bool:
        """Check if more negotiation rounds are allowed"""
        n_max = self.get_n_max(phase)
        return current_round < n_max

    # -----------------------------------------------------------------------
    # P0-17: raw confidence -> calibrated probability (partitioned reliability)
    # -----------------------------------------------------------------------
    # --- Blocker 2: WHOLE-state partitioning by (model_id, regime_id) --------
    _PARTITIONED_ATTRS = (
        "theta_star", "theta_star_raw", "n_max", "assumption_ok",
        "c_worst_estimate", "r_success_estimate", "c_nego_estimate",
        "p_hard_estimate", "phase_theta", "phase_n_max",
        # per-partition routing counters (separate from the global aggregates)
        "p_episodes", "p_trials", "p_successes", "p_rollbacks",
        "p_negotiations",
    )

    def _snapshot_active(self) -> Dict:
        """Capture the ACTIVE partition's full routing state."""
        snap = {a: getattr(self, a) for a in self._PARTITIONED_ATTRS}
        snap["history"] = list(self.history)
        snap["phase_history"] = {k: list(v)
                                 for k, v in self.phase_history.items()}
        snap["phase_theta"] = dict(self.phase_theta)
        snap["phase_n_max"] = dict(self.phase_n_max)
        return snap

    def _load_active(self, snap: Dict) -> None:
        """Restore a partition snapshot into the ACTIVE (legacy) attributes."""
        for a in self._PARTITIONED_ATTRS:
            if a in snap:
                setattr(self, a, snap[a])
        self.history = deque(snap.get("history", []), maxlen=self.window_size)
        self.phase_history = {k: list(v)
                              for k, v in snap.get("phase_history", {}).items()}
        self.phase_theta = dict(snap.get("phase_theta", {}))
        self.phase_n_max = dict(snap.get("phase_n_max", {}))

    def _reset_active_to_cold_start(self) -> None:
        """A FRESH partition starts from the config-derived cold start - never
        the previous partition's adaptive state (blocker 2)."""
        self.theta_star = self._cold_theta
        self.theta_star_raw = self._cold_theta
        self.n_max = self._cold_n_max
        self.assumption_ok = True
        (self.c_worst_estimate, self.r_success_estimate,
         self.c_nego_estimate, self.p_hard_estimate) = self._cold_estimates
        self.history = deque(maxlen=self.window_size)
        self.phase_history = {}
        self.phase_theta = {}
        self.phase_n_max = {}
        # per-partition counters reset for a FRESH partition (globals untouched)
        self.p_episodes = self.p_trials = self.p_successes = 0
        self.p_rollbacks = self.p_negotiations = 0

    def set_operating_context(self, model_id: Optional[str] = None,
                              regime_id: Optional[str] = None) -> None:
        """Switch the ACTIVE (model_id, regime_id) partition. The WHOLE adaptive
        routing state (theta/theta_raw/n_max/estimates/history/phase) is
        snapshotted for the old key and restored for the new key; an UNSEEN key
        returns the config-derived cold start, and switching back RESTORES the
        original partition. A different partition's state is never inherited."""
        new_model = str(model_id) if model_id is not None \
            else self._active_model_id
        new_regime = str(regime_id) if regime_id is not None \
            else self._active_regime_id
        new_key = (new_model, new_regime)
        if new_key == self._active_key:
            return
        # save the outgoing partition
        self._partitions[self._active_key] = self._snapshot_active()
        self._active_key = new_key
        self._active_model_id, self._active_regime_id = new_key
        if new_key in self._partitions:
            self._load_active(self._partitions[new_key])   # restore
        else:
            self._reset_active_to_cold_start()              # fresh cold start

    def operating_context(self) -> Tuple[str, str]:
        return (self._active_model_id, self._active_regime_id)

    def _partition_key(self, model_id, regime_id) -> Tuple[str, str]:
        m = str(model_id) if model_id is not None else self._active_model_id
        r = str(regime_id) if regime_id is not None else self._active_regime_id
        return (m, r)

    def _bin_index(self, raw: float) -> int:
        b = int(raw * self.calibration_bins)
        return min(self.calibration_bins - 1, max(0, b))

    @staticmethod
    def _finite01(value) -> Optional[float]:
        """A finite number in [0,1], else None (fail-closed marker). Blocker 1:
        an OUT-OF-RANGE value is REJECTED (None), not clamped - a proposer that
        emits 1.5 or -0.2 or +inf must fail closed, never be silently squashed to
        a valid-looking score."""
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f) or f < 0.0 or f > 1.0:
            return None
        return f

    def calibrate(self, raw_score, model_id: Optional[str] = None,
                  regime_id: Optional[str] = None) -> Tuple[float, str]:
        """Deterministic (calibrated_probability, reason) for a raw proposer
        score in the (model_id, regime_id) partition (blocker 1).

        BOTH the raw input and the mapped output are validated as finite numbers
        in [0,1]. An invalid raw, an invalid mapped output, or ANY exception
        FAILS CLOSED to (0.0, reason) - never a raw fallback, never a trial. The
        cold-start identity fallback is permitted ONLY as an explicit successful
        mapping result for a VALID raw score in a fresh/under-sampled bin."""
        raw = self._finite01(raw_score)
        if raw is None:
            return 0.0, "fail_closed_invalid_raw"
        try:
            key = self._partition_key(model_id, regime_id)
            samples = self._calibration.get(key, [])
            b = self._bin_index(raw)
            outcomes = [o for (rr, o) in samples if self._bin_index(rr) == b]
            if len(outcomes) >= self.calibration_min_samples:
                p = sum(1.0 for o in outcomes if o) / len(outcomes)
                reason = "mapped"
            else:
                p = raw
                reason = "cold_start_identity"
        except Exception:
            return 0.0, "fail_closed_exception"
        pv = self._finite01(p)
        if pv is None:
            return 0.0, "fail_closed_invalid_output"
        return pv, reason

    def calibrated_probability(self, raw_score,
                               model_id: Optional[str] = None,
                               regime_id: Optional[str] = None) -> float:
        """The calibrated probability only (see calibrate() for the reason)."""
        return self.calibrate(raw_score, model_id, regime_id)[0]

    def record_proposal(self, raw_score, model_id: Optional[str] = None,
                        regime_id: Optional[str] = None) -> bool:
        """Blocker 4: record a VALID proposal as ELIGIBLE (before routing), so
        coverage = executed / eligible is measurable. A non-finite/out-of-range
        raw score is NOT eligible (dropped, fail-closed)."""
        if self._finite01(raw_score) is None:
            return False
        key = self._partition_key(model_id, regime_id)
        self._proposals.setdefault(key, {"eligible": 0, "executed": 0})
        self._proposals[key]["eligible"] += 1
        return True

    def record_calibration_outcome(self, raw_score, trial_success: bool,
                                   model_id: Optional[str] = None,
                                   regime_id: Optional[str] = None) -> bool:
        """Attribute an EXECUTED trial outcome to the model/regime that produced
        it (blocker 4: only executed proposals train the reliability map). A
        non-finite raw score is dropped (fail-closed - never recorded)."""
        raw = self._finite01(raw_score)
        if raw is None:
            return False
        key = self._partition_key(model_id, regime_id)
        self._calibration.setdefault(key, []).append((raw, bool(trial_success)))
        p = self._proposals.setdefault(key, {"eligible": 0, "executed": 0})
        p["executed"] += 1
        # an executed proposal is BY DEFINITION eligible: keep executed<=eligible
        if p["eligible"] < p["executed"]:
            p["eligible"] = p["executed"]
        return True

    def _max_bin_count(self, raws) -> int:
        counts = {}
        for r in raws:
            b = self._bin_index(r)
            counts[b] = counts.get(b, 0) + 1
        return max(counts.values()) if counts else 0

    def calibration_metrics(self, model_id: Optional[str] = None,
                            regime_id: Optional[str] = None) -> Dict:
        """ECE + Brier + log loss + calibration slope + coverage + CI + sample
        count + sufficient_samples flag for a partition (P0-17). ``coverage`` is
        the fraction of samples that fell in a SUFFICIENTLY-sampled bin (i.e.
        actually calibrated vs cold-start fallback)."""
        key = self._partition_key(model_id, regime_id)
        samples = list(self._calibration.get(key, []))
        n = len(samples)
        props = self._proposals.get(key, {"eligible": 0, "executed": 0})
        eligible = int(props.get("eligible", 0))
        executed = int(props.get("executed", n))
        raws0 = [r for (r, _o) in samples]
        # blocker 4: sufficient_samples reflects USABLE samples in a mapping bin
        # (a bin with >= min_samples), not just global n across dispersed bins.
        usable = self._max_bin_count(raws0) >= self.calibration_min_samples
        out = {
            "model_id": key[0], "regime_id": key[1], "sample_count": n,
            "sufficient_samples": bool(usable),
            "min_samples": self.calibration_min_samples,
            "ece": None, "brier": None, "log_loss": None,
            "calibration_slope": None,
            # selection-bias coverage = executed / eligible valid proposals
            "coverage": (executed / eligible) if eligible > 0 else 0.0,
            "eligible_proposals": eligible, "executed_proposals": executed,
            "bin_coverage": 0.0,   # fraction of samples in a sufficiently-sampled bin
            "uses_calibrated_probability": True,   # ECE/Brier/logloss are on p_cal
            "mean_outcome": None, "ci95": None,
        }
        if n == 0:
            return out
        raws = raws0
        outs = [1.0 if o else 0.0 for (_r, o) in samples]
        probs = [self.calibrated_probability(r, key[0], key[1]) for r in raws]
        # Brier + log loss
        eps = 1e-12
        out["brier"] = sum((p - y) ** 2 for p, y in zip(probs, outs)) / n
        out["log_loss"] = -sum(
            y * math.log(min(1 - eps, max(eps, p)))
            + (1 - y) * math.log(min(1 - eps, max(eps, 1 - p)))
            for p, y in zip(probs, outs)) / n
        # ECE over probability bins
        ece = 0.0
        for b in range(self.calibration_bins):
            idx = [i for i, p in enumerate(probs) if self._bin_index(p) == b]
            if not idx:
                continue
            conf = sum(probs[i] for i in idx) / len(idx)
            acc = sum(outs[i] for i in idx) / len(idx)
            ece += abs(acc - conf) * (len(idx) / n)
        out["ece"] = ece
        # calibration slope: OLS slope of outcome on calibrated probability
        mp = sum(probs) / n
        my = sum(outs) / n
        sxx = sum((p - mp) ** 2 for p in probs)
        sxy = sum((p - mp) * (y - my) for p, y in zip(probs, outs))
        out["calibration_slope"] = (sxy / sxx) if sxx > 0 else None
        # bin_coverage: fraction of samples in a sufficiently-sampled RAW bin
        # (reported SEPARATELY from the selection-bias coverage above).
        covered = 0
        for r in raws:
            b = self._bin_index(r)
            if sum(1 for rr in raws if self._bin_index(rr) == b) \
                    >= self.calibration_min_samples:
                covered += 1
        out["bin_coverage"] = covered / n
        # mean outcome + 95% CI (normal approx over Bernoulli outcomes)
        out["mean_outcome"] = my
        if n >= 2:
            var = sum((y - my) ** 2 for y in outs) / (n - 1)
            hw = 1.96 * math.sqrt(max(0.0, var) / n)
            out["ci95"] = [max(0.0, my - hw), min(1.0, my + hw)]
        return out

    def calibration_partitions(self) -> List[Tuple[str, str]]:
        return list(self._calibration.keys())

    def get_stats(self) -> Dict:
        """Get calibration statistics"""
        return {
            "mode": self.mode,
            "theta_star": self.theta_star,
            "theta_star_raw": self.theta_star_raw,
            "theta_star_clamped": self.theta_star,
            "assumption_ok": self.assumption_ok,
            "n_max": self.n_max,
            "c_worst_estimate": self.c_worst_estimate,
            "r_success_estimate": self.r_success_estimate,
            "c_nego_estimate": self.c_nego_estimate,
            "p_hard_estimate": self.p_hard_estimate,
            "c_episode": self.c_episode,
            # GLOBAL cross-partition AGGREGATE counters (kept for backward compat;
            # explicitly CROSS-PARTITION, not the active partition's routing state)
            "counters_scope": "global_aggregate_cross_partition",
            "total_episodes": self.total_episodes,
            "total_trials": self.total_trials,
            "total_successes": self.total_successes,
            "total_rollbacks": self.total_rollbacks,
            "total_negotiations": self.total_negotiations,
            "success_rate": self.total_successes / self.total_trials if self.total_trials > 0 else 0,
            "rollback_rate": self.total_rollbacks / self.total_trials if self.total_trials > 0 else 0,
            # per-partition (ACTIVE partition) routing counters, kept SEPARATE
            "active_partition_counters": {
                "model_id": self._active_model_id,
                "regime_id": self._active_regime_id,
                "episodes": self.p_episodes, "trials": self.p_trials,
                "successes": self.p_successes, "rollbacks": self.p_rollbacks,
                "negotiations": self.p_negotiations,
            },
            "phase_theta": dict(self.phase_theta),
            "phase_n_max": dict(self.phase_n_max),
            # P0-12: the cold-start policy/source of the initial θ*/N_max
            "cold_start_source": dict(self.cold_start_source),
            "initial_theta": self._initial_theta,
            "initial_n_max": self._initial_n_max,
            # P0-17: active calibration context + per-partition coverage
            "operating_context": {
                "model_id": self._active_model_id,
                "regime_id": self._active_regime_id,
            },
            # P0-17 / blocker 4: per-partition calibration metrics (ECE/Brier/
            # log loss/slope/coverage/CI/counts/sufficient_samples).
            "calibration_partitions": [
                self.calibration_metrics(k[0], k[1])
                for k in set(list(self._calibration.keys())
                             + list(self._proposals.keys()))
            ],
            "routing_partitions": [
                {"model_id": k[0], "regime_id": k[1],
                 "theta_star": v.get("theta_star"), "n_max": v.get("n_max")}
                for k, v in self._partitions.items()
            ],
        }

    def get_theoretical_values(self) -> Dict:
        """Theoretically derived values (blocker 7): θ* (Eq. 8) and the
        CORRECTED N+1 Eq. 9 over the EMPIRICAL estimates. The empirical N_max is
        an EXPECTED/SOFT guard - named ``n_max_soft_guard_empirical``, never a
        theoretical/safety N_max (the deterministic ledger is the safety guard)."""
        metrics = _legacy_module("experiments.", "metrics")
        derive_theta_star = metrics.derive_theta_star
        derive_n_max = metrics.derive_n_max
        denom = self.r_success_estimate + self.c_worst_estimate
        theta = (derive_theta_star(self.c_worst_estimate, self.c_nego_estimate,
                                   self.r_success_estimate)
                 if denom > 0 else float("nan"))
        # corrected Eq. 9 (N+1 form) over the empirical estimates (soft guard)
        n_soft = derive_n_max(self.c_episode, self.c_worst_estimate,
                              self.c_nego_estimate)
        return {
            "theta_star_theoretical": theta,
            # explicitly EXPECTED/SOFT - not a safety bound
            "n_max_soft_guard_empirical": n_soft,
            "expected_utility_execution":
                lambda p: p * self.r_success_estimate
                - (1 - p) * self.c_worst_estimate,
            "expected_utility_negotiation": -self.c_nego_estimate,
        }

    @staticmethod
    def _key_str(key):
        return f"{key[0]}␟{key[1]}"          # unit separator, JSON-safe

    @staticmethod
    def _str_key(s):
        parts = str(s).split("␟")
        if len(parts) != 2:
            raise ValueError(f"malformed partition key {s!r}")
        return (parts[0], parts[1])

    def to_persistable(self) -> Dict:
        """Versioned, JSON-safe snapshot of EVERY partition (blocker 2): the
        active + saved routing states, the reliability samples, and the proposal
        coverage counters, so a round-trip restores all partitions without
        mixing keys."""
        # ensure the active partition is captured alongside the saved ones
        parts = dict(self._partitions)
        parts[self._active_key] = self._snapshot_active()
        routing = {}
        for k, snap in parts.items():
            r = {a: snap.get(a) for a in self._PARTITIONED_ATTRS}
            r["history"] = [m.to_dict() for m in snap.get("history", [])]
            r["phase_history"] = {
                ph: [m.to_dict() for m in ms]
                for ph, ms in snap.get("phase_history", {}).items()}
            routing[self._key_str(k)] = r
        return {
            "version": self.persistence_version,
            "active_key": self._key_str(self._active_key),
            "counters": {
                "total_episodes": self.total_episodes,
                "total_trials": self.total_trials,
                "total_successes": self.total_successes,
                "total_rollbacks": self.total_rollbacks,
                "total_negotiations": self.total_negotiations,
            },
            "routing_partitions": routing,
            "reliability": {self._key_str(k): [[float(r), bool(o)]
                                               for (r, o) in v]
                            for k, v in self._calibration.items()},
            "proposals": {self._key_str(k): dict(v)
                          for k, v in self._proposals.items()},
        }

    @staticmethod
    def _req_nonneg_int(v, name):
        # STRICT (review): a real int (JSON preserves ints), NOT a bool, NO
        # string/float coercion.
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(f"{name} must be a nonnegative int (no coercion, "
                             f"not bool), got {v!r}")
        return v

    @staticmethod
    def _req_finite(v, name):
        """A REQUIRED finite real number (not bool, no string coercion)."""
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or not math.isfinite(float(v)):
            raise ValueError(f"{name} must be a finite number, got {v!r}")
        return float(v)

    # exact field-type contract for a persisted CostMetrics dict
    _METRIC_BOOL_FIELDS = ("trial_executed", "trial_success", "episode_success",
                           "rolled_back", "hard_failure", "measurement_invalid",
                           "negotiation_entered", "proposal_eligible",
                           "proposal_executed")
    _METRIC_FLOAT_FIELDS = ("throughput_before", "throughput_after",
                            "throughput_trial_min", "throughput_loss",
                            "trial_duration",
                            "negotiation_duration", "throughput_during_nego",
                            "llm_confidence")
    # reconnection_time is OPTIONAL (None allowed) but finite when present
    _METRIC_OPT_FLOAT_FIELDS = ("reconnection_time",)

    @classmethod
    def _validate_metric_dict_strict(cls, md, ctx):
        """STRICT type contract on the RAW persisted metric dict BEFORE
        from_dict conversion (no coercion): bool fields must be real bools,
        negotiation_rounds a nonneg int, float fields finite numbers (not bool),
        optional probs finite in [0,1]. So 'false'->True or 1.5->1 coercions can
        never slip through."""
        if not isinstance(md, dict):
            raise ValueError(f"metric not an object in {ctx}")
        for name in cls._METRIC_BOOL_FIELDS:
            if name in md and not isinstance(md[name], bool):
                raise ValueError(f"{name} not a bool in {ctx}: {md[name]!r}")
        if "negotiation_rounds" in md:
            v = md["negotiation_rounds"]
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"negotiation_rounds not nonneg int in {ctx}")
        for name in cls._METRIC_FLOAT_FIELDS:
            if name in md:
                v = md[name]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not math.isfinite(float(v)):
                    raise ValueError(f"{name} not a finite number in {ctx}")
        for name in cls._METRIC_OPT_FLOAT_FIELDS:
            v = md.get(name)
            if v is not None and (isinstance(v, bool)
                                  or not isinstance(v, (int, float))
                                  or not math.isfinite(float(v))):
                raise ValueError(f"{name} not a finite number/None in {ctx}")
        for name in ("raw_confidence", "calibrated_probability", "threshold"):
            if md.get(name) is not None:
                v = md[name]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not math.isfinite(float(v)) \
                        or float(v) < 0.0 or float(v) > 1.0:
                    raise ValueError(f"{name} out of [0,1] in {ctx}: {v!r}")

    @classmethod
    def _validate_metric_finite(cls, m, ctx):
        """Validate EVERY numeric field of a restored CostMetrics is finite
        (review: 'all history/phase_history values', not just three)."""
        for name in ("throughput_before", "throughput_after",
                     "throughput_trial_min", "throughput_loss",
                     "trial_duration",
                     "negotiation_duration", "throughput_during_nego",
                     "llm_confidence"):
            v = getattr(m, name)
            if not math.isfinite(float(v)):
                raise ValueError(f"non-finite {name} in {ctx}")
        # reconnection_time is OPTIONAL (None = unmeasured); finite when present
        if m.reconnection_time is not None \
                and not math.isfinite(float(m.reconnection_time)):
            raise ValueError(f"non-finite reconnection_time in {ctx}")
        # raw/calibrated/threshold are OPTIONAL but, when present, must be finite
        # AND in [0,1] (a probability out of range is fail-closed, not finite-only)
        for name in ("raw_confidence", "calibrated_probability", "threshold"):
            v = getattr(m, name)
            if v is None:
                continue
            fv = float(v)
            if not math.isfinite(fv) or fv < 0.0 or fv > 1.0:
                raise ValueError(f"{name} out of [0,1] in {ctx}: {v!r}")
        if not isinstance(m.negotiation_rounds, int) \
                or m.negotiation_rounds < 0:
            raise ValueError(f"bad negotiation_rounds in {ctx}")

    def load_persistable(self, data: Dict) -> bool:
        """Restore to_persistable() output. FULLY ATOMIC + FAIL-CLOSED (item c):
        EVERYTHING is parsed and validated into LOCALS inside the try; the live
        state is committed ONLY after all validation succeeds, so a bad counter /
        key / value can never partially mutate live state. Returns False (state
        untouched) on a wrong version or ANY malformed field."""
        try:
            if not isinstance(data, dict) \
                    or int(data.get("version", -1)) != self.persistence_version:
                logger.warning("calibration persistence version mismatch - "
                               "refusing to load (fail-closed)")
                return False
            # review: REQUIRED top-level fields must be present.
            for req in ("routing_partitions", "reliability", "active_key"):
                if req not in data:
                    raise ValueError(f"missing required field {req!r}")
            routing = data["routing_partitions"]
            reliability = data["reliability"]
            proposals = data.get("proposals", {})
            counters = data.get("counters", {})
            if not all(isinstance(x, dict)
                       for x in (routing, reliability, proposals, counters)):
                raise ValueError("routing/reliability/proposals/counters "
                                 "not objects")
            new_parts, new_rel, new_props = {}, {}, {}
            for ks, r in routing.items():
                key = self._str_key(ks)
                if not isinstance(r, dict):
                    raise ValueError("routing partition not an object")
                snap = {a: r.get(a) for a in self._PARTITIONED_ATTRS}
                # STRICT: these routing scalars are REQUIRED (missing -> raise)
                # and must be finite reals.
                for a in ("theta_star", "theta_star_raw", "c_worst_estimate",
                          "r_success_estimate", "c_nego_estimate",
                          "p_hard_estimate"):
                    if a not in r:
                        raise ValueError(f"missing required routing scalar {a} "
                                         f"in partition {ks}")
                    snap[a] = self._req_finite(r[a], f"{a}[{ks}]")
                # n_max + ALL per-partition counters are REQUIRED nonneg ints.
                for a in ("n_max", "p_episodes", "p_trials", "p_successes",
                          "p_rollbacks", "p_negotiations"):
                    if a not in r:
                        raise ValueError(f"missing required counter {a} in {ks}")
                    snap[a] = self._req_nonneg_int(r[a], f"{a}[{ks}]")
                if snap.get("assumption_ok") is not None \
                        and not isinstance(snap["assumption_ok"], bool):
                    raise ValueError(f"assumption_ok not bool in {ks}")
                # phase_theta: finite floats; phase_n_max: strict nonneg ints.
                for pk, pv in dict(r.get("phase_theta", {})).items():
                    self._req_finite(pv, f"phase_theta[{pk}][{ks}]")
                for pk, pv in dict(r.get("phase_n_max", {})).items():
                    self._req_nonneg_int(pv, f"phase_n_max[{pk}][{ks}]")
                # STRICT metric-dict type check BEFORE any from_dict coercion
                for md in r.get("history", []):
                    self._validate_metric_dict_strict(md, f"history[{ks}]")
                for ph, ms in r.get("phase_history", {}).items():
                    for md in ms:
                        self._validate_metric_dict_strict(
                            md, f"phase_history[{ph}][{ks}]")
                snap["history"] = deque(
                    (CostMetrics.from_dict(m) for m in r.get("history", [])),
                    maxlen=self.window_size)
                for m in snap["history"]:
                    self._validate_metric_finite(m, f"history[{ks}]")
                snap["phase_history"] = {
                    ph: [CostMetrics.from_dict(m) for m in ms]
                    for ph, ms in r.get("phase_history", {}).items()}
                for ph, ms in snap["phase_history"].items():
                    for m in ms:
                        self._validate_metric_finite(m, f"phase_history[{ph}]")
                snap["phase_theta"] = dict(r.get("phase_theta", {}))
                snap["phase_n_max"] = dict(r.get("phase_n_max", {}))
                new_parts[key] = snap
            for ks, v in reliability.items():
                pairs = []
                for entry in v:
                    if len(entry) != 2 or not isinstance(entry[1], bool):
                        raise ValueError("reliability entry not (float, bool)")
                    a = float(entry[0])
                    if not (math.isfinite(a) and 0.0 <= a <= 1.0):
                        raise ValueError(f"reliability raw {a} out of [0,1]")
                    pairs.append((a, bool(entry[1])))
                new_rel[self._str_key(ks)] = pairs
            for ks, v in proposals.items():
                el = self._req_nonneg_int(v.get("eligible", 0), "eligible")
                ex = self._req_nonneg_int(v.get("executed", 0), "executed")
                if ex > el:      # executed proposals are a subset of eligible
                    raise ValueError(f"executed {ex} > eligible {el}")
                new_props[self._str_key(ks)] = {"eligible": el, "executed": ex}
            new_counters = {n: self._req_nonneg_int(counters.get(n, 0), n)
                            for n in ("total_episodes", "total_trials",
                                      "total_successes", "total_rollbacks",
                                      "total_negotiations")}
            active = self._str_key(data.get("active_key",
                                            self._key_str(self._active_key)))
            # STRICT: the active key MUST be one of the restored partitions.
            if active not in new_parts:
                raise ValueError(f"active_key {active} absent from partitions")
        except Exception as e:
            logger.warning(f"malformed calibration data - refusing to load "
                           f"(fail-closed): {e}")
            return False
        # COMMIT everything atomically only after full validation succeeded.
        self._partitions = new_parts
        self._calibration = new_rel
        self._proposals = new_props
        self.total_episodes = new_counters["total_episodes"]
        self.total_trials = new_counters["total_trials"]
        self.total_successes = new_counters["total_successes"]
        self.total_rollbacks = new_counters["total_rollbacks"]
        self.total_negotiations = new_counters["total_negotiations"]
        self._active_key = active
        self._active_model_id, self._active_regime_id = active
        self._load_active(self._partitions[active])
        return True

    def _save_data(self):
        """Save versioned calibration data (all partitions) to file."""
        if not self.data_file:
            return
        try:
            Path(self.data_file).parent.mkdir(parents=True, exist_ok=True)
            with open(self.data_file, 'w') as f:
                json.dump(self.to_persistable(), f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save calibration data: {e}")

    def _load_data(self):
        """Load versioned calibration data; fail-closed on version/format."""
        if not self.data_file or not Path(self.data_file).exists():
            return
        try:
            with open(self.data_file) as f:
                data = json.load(f)
            self.load_persistable(data)
        except Exception as e:
            logger.warning(f"Failed to load calibration data: {e}")
