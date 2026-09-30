#!/usr/bin/env python3
"""
Experiment Runner for the journal 5-phase intent-coordination evaluation.

Reproduces the paper experiment (Section V):
  * 5-phase scenario  : Nominal -> Degradation -> Impairment -> Recovery ->
    Nominal, 300 s = 20 x 15 s KPI steps, serving/neighbor gains per phase.
  * Dual intents      : I1 = |BS2 power offset| <= 3 dB;  I2 = UE throughput
    >= 8 Mbps.
  * Methods compared  : the P0-20 canonical six - rule_based, score_heuristic,
    rl_controller, llm_no_history, fixed_threshold, adaptive (headline: adaptive).
    llm_with_history is a KNOWN integration method only, not a primary default.
  * Metrics           : all 10 paper metrics via experiments/metrics.py
    (dual-intent satisfaction, rollback, negotiation, consecutive-rollback,
     ECE, enforcement/clip stats, hard failures, cost -> theta*/N_max, per-UE
     KPIs, 95% CIs) + latency breakdown.
  * Figures           : throughput timeline, metrics table, reliability diagram,
    consecutive-rollback histogram, per-UE KPI (experiments/figures.py).
  * Multi-LLM compare : same scenario across LLM backends. The HEADLINE run's
    decision LLM MUST be claude-sonnet (task requirement); the multi-LLM sweep
    is a separate experiment that includes claude-sonnet as the reference.

TWO EXECUTION MODES
  * mode="synthetic" (Phase 1, DEFAULT): records come from experiments/synthetic
    - NO live RAN, LLM, or network. Everything downstream (metrics, figures,
      report) is identical to the live path, so this validates the whole harness
      offline.
  * mode="live" (Phase 2): drives the real IntentCoordinator (process_intent
    over the S0-S6 loop) against the powered-up testbed. The coordination path
    of each episode is reconstructed non-invasively from the coordinator's
    existing on_state_change callback (no changes to the coordinator control
    flow). Guarded so it can only run when a coordinator is supplied.
"""

from __future__ import annotations

import os
import csv
import json
import time
import hashlib
import logging
import argparse
import importlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from experiments.metrics import (
    StepRecord, EpisodeRecord, ClipEvent, IntentConfig, CostPriors,
    compute_all_metrics, compute_multi_method, derive_theta_star, derive_n_max,
    live_intent_config, validated_i2_target, _is_real_number,
)

logger = logging.getLogger("ExperimentRunner")

# P0-20: the primary comparison default IS the canonical required SIX (single
# source of truth = paired_runner.REQUIRED_METHODS). llm_with_history stays a
# KNOWN integration method only, NOT a primary default. The natural headline
# method is 'adaptive'.
from experiments.paired_runner import REQUIRED_METHODS as _REQUIRED_METHODS
DEFAULT_METHODS = list(_REQUIRED_METHODS)
DEFAULT_HEADLINE_METHOD = "adaptive"
HEADLINE_BACKEND = "claude-sonnet"   # task requirement for the verification run


def _resolve_intent_cfg(args) -> Optional[IntentConfig]:
    """The IntentConfig for THIS run, or None to keep the offline default.

    LIVE / OFFLINE SEPARATION (the whole point of this function):

      * ``--mode live``  -> ``live_intent_config()``: I2 = the MEASURED,
        hardware-dependent ``config.LIVE_I2_TARGET_MBPS``. The 8.0 Mbps offline
        default is ABOVE the real 24-PRB cell's single-UE ceiling (4.82 Mbps),
        so on hardware it can never be satisfied by any UE at any time - strict
        satisfaction would collapse to 0 and theta*'s premise C_worst > C_nego
        would break (docs/ue_stability_and_capacity.md §4-5).
      * ``--mode synthetic`` / ``emulated`` -> ``None``, so
        ``ExperimentRunner``'s ``cfg or IntentConfig()`` fallback keeps the
        8.0 Mbps paper-model default BIT-FOR-BIT. That default is calibrated
        against ``config.REFERENCE_CEILING_MBPS`` and shared with
        ``experiments.synthetic``'s frozen profiles; offline behaviour must not
        change.
      * ``--i2-target-mbps X`` -> an EXPLICIT operator override that wins in
        ANY mode (validated: bool/NaN/inf/<=0 rejected). This is the hook for a
        hardware swap: re-measure, pass the new number, no code edit.
    """
    override = getattr(args, "i2_target_mbps", None)
    live = getattr(args, "mode", None) == "live"
    if live:
        cfg = live_intent_config(override)
        logger.info("live I2 target = %g Mbps (%s; measured hardware ceiling, "
                    "docs/ue_stability_and_capacity.md)",
                    cfg.throughput_target_mbps,
                    "--i2-target-mbps override" if override is not None
                    else "config.LIVE_I2_TARGET_MBPS")
        return cfg
    if override is not None:
        target = validated_i2_target(override)
        logger.info("I2 target overridden to %g Mbps (--i2-target-mbps) in "
                    "--mode %s", target, getattr(args, "mode", None))
        return IntentConfig(throughput_target_mbps=target)
    return None                     # offline default (8.0) untouched


def _apply_probe_args(runner, args) -> None:
    """Thread the CLI throughput-probe args (Batch F P0-14) onto the runner's
    validated ProbeConfig (rejects bool/NaN/inf/negative/zero)."""
    from coordinator.measurement import ProbeConfig, _finite_positive
    runner.probe_config = ProbeConfig(
        offered_load_mbps=args.offered_load_mbps,
        protocol=args.probe_protocol,
        direction=args.probe_direction,
        duration_s=args.probe_duration_s,
        mode=args.probe_mode)
    # validate the floor with the SAME positive-finite validator (a NaN/inf/<=0
    # floor must never reach metadata / break JSON finiteness).
    runner.capacity_saturation_floor_mbps = _finite_positive(
        "capacity_floor_mbps", args.capacity_floor_mbps)


def _resolve_offered_load_setter(coordinator):
    """The physical offered-load hook for a LIVE run, or None.

    On the real testbed the exogenous environment is driven by re-arming the
    iperf3 UDP generator at a new offered rate. That hook lives on the live
    coordinator's measurement path; we look for a callable `set_offered_load`
    and use it. If the coordinator does not expose one (e.g. no traffic
    generator wired yet), we return None - the OfferedLoadEnvironmentDriver then
    FAILS CLOSED at apply() rather than pretending to have driven the load. We
    never fabricate a generator here."""
    if coordinator is None:
        return None
    hook = getattr(coordinator, "set_offered_load", None)
    return hook if callable(hook) else None


def _apply_env_args(runner, args, coordinator) -> None:
    """Thread the §1-4 environment CLI args onto the runner (P0-19):

      * --phase-profile sec14-load selects the §1-4 load-driven phase structure
        (channel gains == 0, offered load swept); legacy-5phase keeps the §V-B
        channel-gain table (default, so offline/emulated tests are unchanged).
      * --env-driver offered-load injects an OfferedLoadEnvironmentDriver whose
        approval comes ONLY from the operator env var AIC_ENV_DRIVER_APPROVED
        (never from this code); an unapproved/absent driver makes a LIVE run fail
        closed in _preflight_environment. `none` (default) leaves env_driver None
        (emulated runs use the ChannelModel; a live run without a driver fails
        closed - unchanged)."""
    if getattr(args, "phase_profile", "legacy-5phase") == "sec14-load":
        from config import sec1_4_load_phases
        runner.phases = sec1_4_load_phases(
            nominal_mbps=args.offered_load_mbps,
            congested_mbps=2.0 * args.offered_load_mbps)
    if getattr(args, "env_driver", "none") == "offered-load":
        from experiments.environment import OfferedLoadEnvironmentDriver
        driver = OfferedLoadEnvironmentDriver.from_env(
            load_setter=_resolve_offered_load_setter(coordinator))
        if not driver.approved:
            logger.warning(
                "offered-load env driver is NOT approved (%s != '1'); a live "
                "run will fail closed in preflight until the operator approves it",
                "AIC_ENV_DRIVER_APPROVED")
        elif _resolve_offered_load_setter(coordinator) is None:
            logger.warning(
                "offered-load env driver approved but no live offered-load hook "
                "(coordinator.set_offered_load) is wired; apply() will fail "
                "closed until the iperf3 generator hook is provided")
        runner.env_driver = driver


def _git_hash() -> Optional[str]:
    """Short git commit hash of the repo (None outside a git checkout)."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(Path(__file__).resolve().parent.parent))
        return out.stdout.strip() or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Live-mode episode path tracer (non-invasive)
# ---------------------------------------------------------------------------

class _StateTracer:
    """Reconstruct the S0-S6 coordination path from on_state_change events.

    Wired to coordinator.on_state_change for the duration of one process_intent
    call. From the observed state sequence we derive:
        trial_executed  = "S3" in seq
        entered_nego    = "S5" in seq
        rolled_back     = trial_executed and "S5" seen AFTER "S4"
        routed_to       = "trial" if "S3" in seq else "negotiation"
    This needs ZERO changes to the coordinator's control logic.
    """

    def __init__(self):
        self.seq: List[str] = []

    def __call__(self, old_state: str, new_state: str):
        self.seq.append(new_state)

    def reset(self):
        self.seq = []

    def summarize(self) -> Dict:
        seq = self.seq
        trial_executed = "S3" in seq
        entered_nego = "S5" in seq
        rolled_back = False
        if trial_executed and "S4" in seq and "S5" in seq:
            # S5 after S4 => trial validated & failed => rollback
            rolled_back = seq.index("S5") > seq.index("S4")
        routed_to = "trial" if trial_executed else "negotiation"
        return {
            "trial_executed": trial_executed,
            "entered_negotiation": entered_nego,
            "rolled_back": rolled_back,
            "routed_to": routed_to,
            "sequence": list(seq),
        }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class ExperimentRunner:
    """Drives the 5-phase experiment and turns records into paper metrics/figures."""

    def __init__(self, coordinator=None, output_dir: str = "experiment_results",
                 cfg: Optional[IntentConfig] = None,
                 phases: Optional[List[Dict]] = None,
                 c_episode: float = 500.0,
                 kpi_interval_s: float = 15.0,
                 steps_per_phase: int = 4):
        self.coordinator = coordinator
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg or IntentConfig()
        self.c_episode = c_episode
        self.kpi_interval_s = kpi_interval_s
        self.steps_per_phase = steps_per_phase
        # calibration carry-over across (method, trial) is opt-in only
        # (--carry-calibration); default preserves 5-trial independence
        self.carry_calibration = False
        # Batch E (P0-15): history-ablation controls. history_policy is the
        # with-history treatment ("online" DEFAULT learns finalized outcomes
        # from the shared starting snapshot so records actually become available;
        # "frozen" reads a controlled immutable snapshot and never learns, and
        # REQUIRES a non-empty pre-collected snapshot - a frozen+empty run is
        # rejected, never silently a zero-record "measured history"). Both paired
        # methods start each reset scope from the SAME controlled snapshot.
        # history_reset_scope is validated deterministic reset granularity.
        self.history_policy = "online"
        self.history_reset_scope = "trial"
        self.paper_history_snapshot: Tuple = tuple()
        self.history_snapshot_id = "empty"
        # Batch E (P0-15): deterministic PAIRED-ABLATION reseed. Before EACH
        # (method, trial) the ChannelModel + DeterministicMockBackend are
        # reseeded (and their counters reset) from a seed derived PURELY from
        # (experiment_seed, trial_id, model/config block) - NEVER from a
        # preceding treatment's consumed state. So llm_no_history and
        # llm_with_history begin trial T from IDENTICAL random state regardless
        # of method order OR how much randomness either treatment consumes;
        # history availability is the only deliberate difference.
        self.experiment_seed = 12345
        self.pair_state_enabled = True
        # ONLY these methods form the paired ablation and are reseeded together;
        # unrelated baselines (rule_based/score_heuristic/rl_controller) are
        # NEVER reseeded (they must not be accidentally paired).
        self.paired_methods = ("llm_no_history", "llm_with_history")
        # a stable, versioned pair-seed scheme id recorded in metadata so a run
        # is reproducible even if the derivation changes in a later version.
        self.pair_seed_scheme = "sha1(experiment_seed|trial_id|model_config)/v1"
        # Batch F (P0-14): the EXPLICIT throughput-probe configuration (offered
        # load / protocol / direction / duration / mode) installed on the
        # coordinator and recorded in every result's metadata. Reproducible and
        # validated (ProbeConfig rejects bool/NaN/inf/negative/zero).
        from coordinator.measurement import ProbeConfig
        self.probe_config = ProbeConfig()
        self.capacity_saturation_floor_mbps = 100.0
        # emulated mode: phase environment goes to the ChannelModel, NOT the
        # executor (action and environment are separate inputs, OQ-EXP-1)
        self.channel_model = None
        # applied-time log of environment applications (P0-19: stamp the ACTUAL
        # monotonic time each canonical-trace environment step was applied). One
        # record per UNIQUE phase identity (phase_label = "p{idx}:{name}"); reset
        # per (method, trial) so evidence is never inherited across method runs.
        self._env_apply_log = []
        # P0-19: the LIVE exogenous environment driver (offered load / external
        # attenuator / shielded path / UE position). It touches ONLY
        # ENVIRONMENT_AXES - NEVER a coordinator action knob. It MUST be
        # explicitly injected AND approved for a live run; a live run without it
        # FAILS CLOSED (zero executor writes). Emulated runs use the ChannelModel
        # as the exogenous driver instead and leave this None.
        self.env_driver = None
        # the LLM backend captured at run_live entry (proposer-reset, P0-20)
        self._llm_backend_name = None
        # P10: skip (instead of just warning about) unwired baselines in
        # live/emulated runs; theta mode + figure column recorded/threaded
        self.skip_unwired = False
        self.theta_mode = "adaptive"
        self.figures_column = "double"
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        # phases: default to the paper table (+3/0, -2/+1, -5/+2, 0/0, +3/0)
        self.phases = phases or [
            {"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0},
            {"name": "Degradation", "serving_gain": -2, "neighbor_gain": 1},
            {"name": "Impairment", "serving_gain": -5, "neighbor_gain": 2},
            {"name": "Recovery", "serving_gain": 0, "neighbor_gain": 0},
            {"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0},
        ]

    # -- record generation ---------------------------------------------------

    def run_synthetic(self, methods: List[str], trials: int = 5, seed: int = 12345
                      ) -> Tuple[List[StepRecord], List[EpisodeRecord]]:
        """Phase-1 offline records (no hardware)."""
        from experiments.synthetic import generate_experiment
        return generate_experiment(methods, trials=trials, phases=self.phases,
                                   steps_per_phase=self.steps_per_phase,
                                   cfg=self.cfg, seed=seed)

    def run_live(self, methods: List[str], trials: int = 5
                 ) -> Tuple[List[StepRecord], List[EpisodeRecord]]:
        """Phase-2 live records from the real coordinator (requires powered testbed).

        NOTE: this method NEVER runs in Phase 1 - it requires a coordinator and a
        live RAN. It is structurally complete so Phase 2 can activate it. See
        docs/experiment_plan.md for the Phase-2 checklist.
        """
        if self.coordinator is None:
            raise RuntimeError("run_live requires a live IntentCoordinator "
                               "(Phase 2). Use mode='synthetic' for offline.")
        # P0-20 (Codex-5): reject any UNKNOWN / DUPLICATE method BEFORE any write
        # (zero-write), via the SAME canonical validator the paired runner uses -
        # a non-canonical method is never silently executed.
        from experiments.paired_runner import validate_method_set
        validate_method_set(methods)
        # P0-19: BEFORE any method/action, prove the environment and action axis
        # sets are DISJOINT. A run that could mix an exogenous change with a
        # coordinator action on the same knob is rejected here, not silently run.
        from experiments.environment import assert_axes_disjoint
        assert_axes_disjoint()
        # P0-19: PREFLIGHT the exogenous environment for EVERY phase HERE - before
        # any executor reset / method configure / action write - so a run that
        # cannot honestly drive the environment (live driver absent/unapproved/
        # insufficient, or an emulated axis the ChannelModel cannot apply) FAILS
        # CLOSED with ZERO executor writes.
        self._preflight_environment()
        from coordinator.history import HistoryResetScope
        scope = HistoryResetScope.coerce(self.history_reset_scope)  # fail-closed
        steps: List[StepRecord] = []
        episodes: List[EpisodeRecord] = []
        c = self.coordinator
        # Batch F (P0-14, review #67db.1): installing the experiment's explicit
        # ProbeConfig is MANDATORY for live/emulated - a coordinator that cannot
        # accept it would run a DEFAULT probe while the metadata claims the
        # requested config, so FAIL CLOSED rather than silently mismeasure.
        setter = getattr(c, "set_probe_config", None)
        if not callable(setter):
            raise RuntimeError(
                "live/emulated coordinator does not accept set_probe_config; "
                "refusing to run with an unverified default probe (Batch F)")
        setter(self.probe_config)
        c.capacity_saturation_floor_mbps = float(
            self.capacity_saturation_floor_mbps)
        # Batch G (P0-20 review): capture the intended LLM backend at run ENTRY,
        # BEFORE any per-method reconfiguration. Baseline methods swap in a
        # 'baseline:*' proposer, so an llm_* method that follows one in the
        # RANDOMISED order must RESET the proposer back to this LLM - otherwise it
        # would silently inherit the preceding baseline backend. Captured once
        # from the pristine coordinator (fail closed if it is already a baseline).
        entry_backend = self._active_model_key(c)
        if str(entry_backend).startswith("baseline:"):
            raise RuntimeError(
                "run_live entered with a baseline proposer already active "
                f"({entry_backend!r}); refusing to run so llm_* methods cannot "
                "inherit a leaked baseline. Select the LLM backend first.")
        # P0-20 (C): an UNIDENTIFIABLE entry model cannot be reset to / verified
        # against, so llm_* methods could silently inherit a leaked baseline -
        # fail closed rather than run with an 'unknown' proposer identity.
        if str(entry_backend) == "unknown" or not str(entry_backend).strip():
            raise RuntimeError(
                "run_live could not identify the entry proposer model (active "
                "model 'unknown') - refusing to run (P0-20)")
        self._llm_backend_name = entry_backend
        # P0-20 (B): capture + VALIDATE the calibrator mode so every method sets
        # its OWN mode explicitly (no cross-method leak across the randomized
        # order). 'adaptive' uses the LEARNING mode (online), NEVER the invalid
        # 'adaptive' string that silently disables calibrator updates.
        _cal = getattr(c, "calibrator", None)
        entry_cal_mode = getattr(_cal, "mode", "online") if _cal is not None \
            else "online"
        if entry_cal_mode not in ("fixed", "phase", "online"):
            raise RuntimeError(
                f"calibrator entry mode {entry_cal_mode!r} is not a valid mode "
                f"(fixed/phase/online) - refusing to run (P0-20)")
        self._entry_cal_mode = entry_cal_mode
        self._adaptive_cal_mode = (entry_cal_mode if entry_cal_mode != "fixed"
                                   else "online")
        # P0-20 (Codex-4): capture the ENTRY history mode too, so run_live can
        # restore + verify the calibrator mode AND history mode (not just the
        # backend) at the end - no method's mode leaks past the run.
        self._entry_history_mode = getattr(c, "history_mode", None)
        # Batch E: experiment_seed is AUTHORITATIVE and must NOT be inferred from
        # mutable backend private state (mock._seed may itself be a derived pair
        # seed after a prior run, which would silently change the master seed on
        # a repeated run_live and break reproducibility). run()/CLI threads it in;
        # a direct caller keeps the deterministic default or sets it explicitly.
        seen_model = [None]                       # for MODEL-scope reset

        def _reset_if(scope_hit: bool):
            if scope_hit:
                self._reset_history_reservoir(c)

        # P0-20 (D): the REAL active proposer/model identity captured DURING each
        # method's execution (before the finally restores the entry backend), so
        # a baseline record is never mislabeled as the entry LLM.
        self._method_model_ids = {}
        # RUN scope: reset ONCE before the whole run (accumulates thereafter).
        _reset_if(scope == HistoryResetScope.RUN)
        body_ok = False
        body_exc = None
        try:
            for method in methods:
                # _configure_method FAILS CLOSED (raises) on a missing baseline
                # backend rather than silently skipping (P0-20), and resets +
                # VERIFIES the proposer identity for non-baseline methods.
                if not self._configure_method(method):
                    continue
                # capture the verified active proposer NOW (execution evidence).
                self._method_model_ids[method] = self._active_model_key(c)
                _reset_if(scope == HistoryResetScope.METHOD)
                for tid in range(1, trials + 1):
                    _reset_if(scope == HistoryResetScope.TRIAL)
                    if scope == HistoryResetScope.MODEL:
                        cur = self._active_model_key(c)
                        if cur != seen_model[0]:
                            self._reset_history_reservoir(c)
                            seen_model[0] = cur
                    s, e = self._run_live_trial(method, tid)
                    steps.extend(s)
                    episodes.extend(e)
            body_ok = True
        except BaseException as be:               # noqa: BLE001 - re-raised
            body_exc = be
            raise
        finally:
            # RESTORE + VERIFY the ENTRY proposer, calibrator mode AND history
            # mode (P0-20-C/Codex-4: NOT warning-only, and NOT just the backend).
            # A restore failure is FAIL-CLOSED and OBSERVABLE: if the body already
            # raised, the restore failure is CHAINED to it (never hidden).
            reasons = []
            setter = getattr(c, "set_llm_backend", None)
            if entry_backend is not None:
                if callable(setter):
                    try:
                        setter(entry_backend)
                    except Exception as e:
                        reasons.append(f"backend restore raised: {e}")
                # ALWAYS compare the active proposer identity, even with no setter
                # (Codex-7): a coordinator left on a non-entry proposer fails.
                if self._active_model_key(c) != entry_backend:
                    reasons.append(
                        f"backend not restored (active "
                        f"{self._active_model_key(c)!r} != {entry_backend!r})")
            cal = getattr(c, "calibrator", None)
            if cal is not None and getattr(self, "_entry_cal_mode", None):
                cal.mode = self._entry_cal_mode
                if getattr(cal, "mode", None) != self._entry_cal_mode:
                    reasons.append("calibrator mode not restored")
            if getattr(self, "_entry_history_mode", None) is not None:
                if hasattr(c, "set_history_mode"):
                    try:
                        c.set_history_mode(self._entry_history_mode)
                    except Exception as e:
                        reasons.append(f"history mode restore raised: {e}")
                # ALWAYS verify the current mode matches entry, even with no
                # setter (Codex-7): a leaked history mode fails closed.
                if getattr(c, "history_mode", None) != self._entry_history_mode:
                    reasons.append("history mode not restored")
            if reasons:
                msg = ("run_live could not restore + verify the entry "
                       f"proposer/calibrator/history state (P0-20): "
                       f"{'; '.join(reasons)}")
                if body_exc is not None:
                    raise RuntimeError(msg) from body_exc
                raise RuntimeError(msg)
        return steps, episodes

    def _fresh_calibration(self, coordinator):
        """Reset calibration state before a (method, trial) unless carry-over
        was explicitly requested (--carry-calibration).

        History reset is SEPARATE (_fresh_history) - a calibration reset never
        touches the history reservoir and vice versa (P0-15)."""
        if self.carry_calibration:
            return
        cal = getattr(coordinator, "calibrator", None)
        if cal is not None and hasattr(cal, "reset"):
            cal.reset()

    def _reset_history_reservoir(self, coordinator) -> None:
        """Reset the coordinator's history to the SHARED controlled starting
        snapshot (P0-15). This is the reset primitive the reset-scope logic
        invokes; it is DISTINCT from calibration reset and never touches it."""
        c = coordinator
        if c is None or not hasattr(c, "restore_history_snapshot"):
            return
        shared = self.paper_history_snapshot
        c.restore_history_snapshot(shared)
        if hasattr(c, "set_frozen_history"):
            c.set_frozen_history(shared)

    def _mock_backend(self, coordinator):
        """The registered deterministic mock backend, if any (paired ablation)."""
        mgr = getattr(coordinator, "llm_manager", None)
        dyn = getattr(mgr, "dynamic_backends", None) if mgr is not None else None
        if isinstance(dyn, dict):
            return dyn.get("mock:deterministic")
        return None

    def _model_config_block(self, coordinator) -> str:
        """A STABLE per-(model, config) block string that is IDENTICAL for the
        paired llm_no_history / llm_with_history methods (they share the model
        and config, differing only in history availability). Used in the pair
        seed so different models/configs get different sequences while a paired
        pair shares one."""
        model = self._active_model_key(coordinator)
        return (f"{model}|c_ep={self.c_episode}|kpi={self.kpi_interval_s}|"
                f"spp={self.steps_per_phase}")

    def _pair_seed(self, coordinator, trial_id: int) -> int:
        """A DETERMINISTIC per-trial seed derived PURELY from (experiment_seed,
        trial_id, model/config block) via SHA-1 (never Python hash(), which is
        salted/non-reproducible). It does NOT depend on any treatment's consumed
        state, so paired methods reseed identically regardless of method order or
        how much randomness either treatment consumes."""
        block = f"{int(self.experiment_seed)}|{int(trial_id)}|" \
                f"{self._model_config_block(coordinator)}"
        digest = hashlib.sha1(block.encode("utf-8")).hexdigest()
        return int(digest[:12], 16)

    def _pair_state_sync(self, coordinator, method: str, trial_id: int) -> None:
        """RESEED the ChannelModel + DeterministicMockBackend (RNG + counters)
        to this trial's DETERMINISTIC pair seed BEFORE the (method, trial) runs,
        so PAIRED methods begin trial T from IDENTICAL random state independent
        of method order and of how much randomness a preceding treatment
        consumed. ONLY the configured paired methods are reseeded - unrelated
        baselines are left untouched."""
        if not self.pair_state_enabled or method not in self.paired_methods:
            return
        seed = self._pair_seed(coordinator, trial_id)
        cm = self.channel_model
        if cm is not None and hasattr(cm, "reseed"):
            cm.reseed(seed)
        mock = self._mock_backend(coordinator)
        if mock is not None and hasattr(mock, "reset"):
            mock.reset(seed)                # reseeds RNG + resets counters

    def _active_model_key(self, coordinator) -> str:
        """The coordinator's current active model id (for MODEL reset scope)."""
        c = coordinator
        for attr in ("_active_model_id",):
            fn = getattr(c, attr, None)
            if callable(fn):
                try:
                    return str(fn())
                except Exception:
                    pass
        mgr = getattr(c, "llm_manager", None)
        name = getattr(mgr, "active_backend_name", None)
        if callable(name):
            try:
                return str(name())
            except Exception:
                pass
        return "unknown"

    # Batch G (P0-20): these baselines are NOW wired as real proposer backends
    # ("baseline:<method>") that drive the SAME coordinator pipeline. Retained as
    # a name so a coordinator that lacks the wired backend still fails loudly
    # rather than silently emitting coordinator-default records.
    UNWIRED_BASELINES = ("rule_based", "score_heuristic", "rl_controller")

    def _configure_method(self, method: str) -> bool:
        """Configure the coordinator for one method; False => skip it.

        The rule/score/RL baselines are wired to a dedicated proposer backend
        ("baseline:<method>") so they run through the identical safety pipeline.
        If that backend cannot be selected we FAIL CLOSED (skip) rather than
        silently record the coordinator's default behaviour as the named
        baseline (P0-20: no baseline mislabeled as default).
        """
        c = self.coordinator
        setter = getattr(c, "set_llm_backend", None)
        if method in self.UNWIRED_BASELINES:
            # FAIL CLOSED: set the wired baseline backend and VERIFY it is the
            # EXACT active proposer (P0-20-C): a truthy no-op setter is NOT
            # accepted - the baseline must genuinely be active, never the
            # coordinator default mislabeled, and never silently skipped.
            want = f"baseline:{method}"
            if callable(setter):
                setter(want)
            active = self._active_model_key(c)
            if active != want:
                raise RuntimeError(
                    f"baseline {method!r} could not be made the ACTIVE proposer "
                    f"(wanted {want!r}, active {active!r}) - refusing (P0-20: a "
                    f"missing baseline must fail, never mislabel the default)")
            logger.info("baseline %r wired + verified active", method)
        else:
            # NON-baseline (llm_*): RESET the proposer to the captured entry LLM
            # and VERIFY the EXACT identity (P0-20-C: no truthy no-op) so it can
            # never inherit a baseline left active by a preceding method in the
            # randomised order.
            want = getattr(self, "_llm_backend_name", None)
            if want is not None:
                if callable(setter):
                    setter(want)
                active = self._active_model_key(c)
                if active != want:
                    raise RuntimeError(
                        f"could not reset proposer to LLM {want!r} for method "
                        f"{method!r} (active {active!r}) - would inherit a "
                        f"leaked baseline (P0-20)")
        # CALIBRATOR MODE (P0-20-B): set EXPLICITLY for EVERY method so no method
        # inherits the previous method's mode across the randomized order.
        # fixed_threshold FREEZES theta* ('fixed'); 'adaptive' LEARNS (the
        # captured online/learning mode, NEVER the invalid 'adaptive' string
        # that silently disables updates); all others use the captured entry mode.
        cal = getattr(c, "calibrator", None)
        if cal is not None:
            if method == "fixed_threshold":
                cal.mode = "fixed"
            elif method == "adaptive":
                cal.mode = getattr(self, "_adaptive_cal_mode", "online")
            else:
                cal.mode = getattr(self, "_entry_cal_mode", "online")
        # HISTORY MODE (Batch E / P0-15, P0-20-B): set EXPLICITLY for EVERY method
        # so none inherits the previous method's history mode. llm_no_history ->
        # DISABLED, llm_with_history -> configured policy, fixed_threshold /
        # adaptive -> the llm_with_history mode (they are LLM-with-history), and
        # every baseline -> the default (ONLINE). The per-trial reset re-installs
        # the shared snapshot.
        if method in ("fixed_threshold", "adaptive"):
            self._apply_history_mode(c, "llm_with_history")
        else:
            self._apply_history_mode(c, method)
        return True

    def _history_mode_for(self, method: str):
        """The HistoryMode a method runs under (fail-closed via the coordinator
        enum). llm_no_history -> DISABLED; llm_with_history -> the configured
        FROZEN/ONLINE policy; anything else -> ONLINE (the default)."""
        from coordinator.history import HistoryMode, HistoryPolicy
        if method == "llm_no_history":
            return HistoryMode.DISABLED
        if method == "llm_with_history":
            pol = HistoryPolicy.coerce(self.history_policy)   # fail-closed
            return (HistoryMode.ONLINE_RESERVOIR
                    if pol == HistoryPolicy.ONLINE
                    else HistoryMode.FROZEN_RESERVOIR)
        return HistoryMode.ONLINE_RESERVOIR

    def _apply_history_mode(self, c, method: str) -> None:
        """Set the method's history MODE (does NOT reset the reservoir - that is
        the reset-scope logic's job). Validates FROZEN never runs against an
        empty snapshot (a frozen-empty measurement is rejected, not silent).
        Idempotent; safe when the coordinator has no history API (coordinator
        =None in synthetic mode)."""
        from coordinator.history import HistoryPolicy
        if method == "llm_with_history":
            pol = HistoryPolicy.coerce(self.history_policy)   # fail-closed
            if pol == HistoryPolicy.FROZEN and not self.paper_history_snapshot:
                raise ValueError(
                    "history_policy='frozen' requires a NON-EMPTY pre-collected "
                    "paper_history_snapshot; a frozen+empty run injects zero "
                    "records and is not a meaningful with-history treatment "
                    "(use history_policy='online' or provide a snapshot)")
        if c is None or not hasattr(c, "set_history_mode"):
            return
        mode = self._history_mode_for(method)
        if mode is not None:
            c.set_history_mode(mode)

    def _register_paper_intents(self, c) -> None:
        """Register the paper's dual intents as ACTIVE intents (C7).

        I1 (|power_bs offset| <= bound, scoped to the constrained BS) and
        I2 (per-UE throughput >= target) both enter the coordinator's
        MONITORED set, so S1 conflict screening, the S2 LLM context and S4
        validation coordinate over the SAME active-intent pair - the driver
        no longer treats I1 as an offline-only metric. Cleared first for
        (method, trial) independence.
        """
        from decision.intent_model import (
            ConstraintType, Intent, IntentScope, IntentStatus, IntentTarget,
            IntentType,
        )
        mgr = getattr(c, "intent_manager", None)
        if mgr is None:
            return
        mgr.clear()
        i1 = Intent(
            type=IntentType.POWER_CONSTRAINT,
            target=IntentTarget(kpi_name="tx_power",
                                constraint_type=ConstraintType.MAX,
                                target_value=self.cfg.power_bound_db,
                                unit="dB"),
            scope=IntentScope(bs_ids=[self.cfg.power_bs]),
            description=(f"I1: |{self.cfg.power_bs} power offset| <= "
                         f"{self.cfg.power_bound_db:g} dB"))
        i1.status = IntentStatus.ACTIVE
        i2 = Intent(
            type=IntentType.THROUGHPUT_GOAL,
            target=IntentTarget(kpi_name="throughput",
                                constraint_type=ConstraintType.MIN,
                                target_value=self.cfg.throughput_target_mbps,
                                unit="Mbps"),
            scope=IntentScope(ue_ids=list(self.cfg.throughput_ue_ids)),
            description=(f"I2: UE downlink throughput >= "
                         f"{self.cfg.throughput_target_mbps:g} Mbps"))
        i2.status = IntentStatus.ACTIVE
        mgr.add(i1)
        mgr.add(i2)

    def _run_live_trial(self, method: str, trial_id: int
                        ) -> Tuple[List[StepRecord], List[EpisodeRecord]]:
        """One live (method, trial): walk phases, coordinate, collect KPIs."""
        c = self.coordinator
        # P0-19: reset the environment-apply evidence log for THIS (method, trial)
        # so a phase's applied timestamp is never inherited from another method
        # or a prior trial. One record is appended per UNIQUE phase identity only
        # AFTER the environment is successfully applied.
        self._env_apply_log = []
        # Batch E (P0-15): capture/restore the paired-ablation RNG state so
        # paired methods begin THIS trial from identical random draws, BEFORE
        # calibration reset (which is deterministic) and the trial body run.
        self._pair_state_sync(c, method, trial_id)
        self._fresh_calibration(c)
        # ensure THIS method's history MODE is set (idempotent); the reservoir
        # reset itself is driven by the configured reset scope in run_live, not
        # here (calibration reset stays separate above).
        self._apply_history_mode(c, method)
        # (method, trial) independence also covers ACTIONS: start each trial
        # from the neutral action vector (same carry-over opt-out)
        ex = getattr(c, "executor", None)
        if (not self.carry_calibration and ex is not None
                and hasattr(ex, "reset_all_axes")):
            ex.reset_all_axes()
        # C7: both paper intents live in the coordinator's monitored set
        self._register_paper_intents(c)
        steps: List[StepRecord] = []
        episodes: List[EpisodeRecord] = []
        tracer = _StateTracer()
        prev_cb = getattr(c, "on_state_change", None)
        c.on_state_change = tracer
        step_idx = 0
        try:
            for enum_idx, ph in enumerate(self.phases):
                # UNIQUE phase identity (P0-19): name is required + nonempty; the
                # label "p{idx}:{name}" keeps the two Nominal phases distinct. A
                # trace-derived phase carries its OWN phase_idx/phase_label (so it
                # matches the canonical trace exactly); a plain phase table uses
                # the enumeration index.
                _pname = ph.get("name")
                if not isinstance(_pname, str) or not _pname.strip():
                    raise ValueError(
                        f"phase {enum_idx} must have a nonempty 'name', "
                        f"got {_pname!r}")
                phase_idx = ph.get("phase_idx")
                if phase_idx is None:
                    phase_idx = enum_idx
                _plabel = ph.get("phase_label") or f"p{phase_idx}:{_pname}"
                self._apply_phase_environment(ph, phase_idx, _plabel)
                for _s in range(self.steps_per_phase):
                    t0 = time.time()
                    # collect KPIs for this timestep
                    metrics = c.ue_collector.collect_all()
                    if hasattr(c, "_merge_throughput"):
                        c._merge_throughput(metrics)
                    # Batch F (#8): the live/raw step KPIs use HONEST radio
                    # names (gnb_ul_avg_rsrp_dbm / gnb_ul_snr_db + source) - never
                    # the ambiguous rsrp/sinr that hide gNB-UL vs UE-DL.
                    ue_kpis = {}
                    for ue, m in metrics.items():
                        hk = m.honest_radio_kpis()
                        ue_kpis[ue] = {
                            "throughput_mbps": m.throughput_mbps,
                            "gnb_ul_avg_rsrp_dbm": hk.get("gnb_ul_avg_rsrp_dbm"),
                            "gnb_ul_snr_db": hk.get("gnb_ul_snr_db"),
                            "ue_dl_ss_rsrp_dbm": hk.get("ue_dl_ss_rsrp_dbm"),
                            "ue_dl_sinr_db": hk.get("ue_dl_sinr_db"),
                            "radio_source": hk.get("radio_source"),
                            "radio_direction": hk.get("radio_direction"),
                        }
                    action_offsets = self._read_action_offsets()
                    steps.append(StepRecord(
                        trial_id=trial_id, method=method, phase=ph["name"],
                        step_idx=step_idx, t_s=step_idx * self.kpi_interval_s,
                        # P1-5: keep repeated phase names (first vs last Nominal)
                        # distinct through aggregation/plotting.
                        phase_idx=phase_idx, phase_label=_plabel,
                        ue_kpis=ue_kpis, action_offsets=action_offsets))

                    # trigger a coordination episode when I2 is violated
                    if self._i2_violated(ue_kpis):
                        tracer.reset()
                        tp_before = self._mean_tput(ue_kpis)
                        result = c.process_intent(
                            f"Ensure UE downlink throughput >= "
                            f"{self.cfg.throughput_target_mbps:g} Mbps")
                        episodes.extend(self._episode_from_result(
                            method, trial_id, ph["name"], result,
                            tracer.summarize(), tp_before, phase_idx=phase_idx))

                    # pace to the KPI interval (skip in fast test envs)
                    elapsed = time.time() - t0
                    if self.kpi_interval_s > 0:
                        time.sleep(max(0.0, self.kpi_interval_s - elapsed))
                    step_idx += 1
        finally:
            c.on_state_change = prev_cb
        return steps, episodes

    # canonical phase-key -> ENVIRONMENT_AXES mapping (P0-19). Every exogenous
    # environment axis a phase can request; NONE is a coordinator action knob.
    # Sourced from experiments.environment so the runner and any exogenous driver
    # share ONE definition (they must agree on what a phase asked for).
    from experiments.environment import PHASE_KEY_TO_ENV_AXIS as _PHASE_TO_ENV_AXIS
    # environment axes the EMULATED ChannelModel can actually apply (P0-19
    # capability set); ue_position is NOT one - a phase requesting it under the
    # emulated driver fails closed rather than being silently ignored.
    _CHANNEL_ENV_AXES = frozenset({
        "channel_serving_gain_db", "channel_neighbor_gain_db",
        "offered_load_mbps", "external_disturbance_db",
    })

    def _phase_requested_env(self, phase: Dict) -> Dict[str, float]:
        """The {ENVIRONMENT axis: finite value} a phase requests (P0-19). Channel
        gains are always present (default 0); offered load / disturbance /
        position are present when the canonical trace supplied them. Every value
        is finite (bool/NaN/inf rejected); every key is an ENVIRONMENT axis -
        never an action knob. Delegates to the single shared helper so the
        runner's preflight/apply and a driver's apply() can never diverge."""
        from experiments.environment import phase_environment_request
        return phase_environment_request(phase)

    def _preflight_environment(self):
        """P0-19: prove the EXOGENOUS environment can be applied for EVERY phase
        BEFORE any executor reset / method configure / action write, so a run
        that cannot honestly drive the environment FAILS CLOSED with ZERO
        executor writes. For each phase this validates the requested axes/values
        (finite) and the driver's CAPABILITY: emulated -> the ChannelModel must
        support every requested axis; live -> an approved driver must exist and
        support every requested axis. Value application/matching still happens at
        apply time (and is validated there)."""
        from experiments.environment import (
            AxisOverlapError, validate_exogenous_driver,
            require_driver_capability)
        live = self.channel_model is None
        if live:
            validate_exogenous_driver(self.env_driver)
        for enum_idx, ph in enumerate(self.phases or []):
            pname = ph.get("name")
            if not isinstance(pname, str) or not pname.strip():
                raise ValueError(
                    f"phase {enum_idx} must have a nonempty 'name', got "
                    f"{pname!r}")
            requested = self._phase_requested_env(ph)   # validates finiteness
            if live:
                require_driver_capability(self.env_driver, requested.keys())
            else:
                unsupported = set(requested) - self._CHANNEL_ENV_AXES
                if unsupported or not callable(
                        getattr(self.channel_model, "set_environment", None)):
                    raise AxisOverlapError(
                        f"emulated ChannelModel cannot apply environment axes "
                        f"{sorted(unsupported)} for phase {enum_idx} "
                        f"({pname!r}); supports {sorted(self._CHANNEL_ENV_AXES)} "
                        f"or has no callable set_environment - refusing (P0-19)")

    def _apply_phase_environment(self, phase: Dict, phase_idx: int,
                                 phase_label: str):
        """Apply ONE phase's EXOGENOUS environment (P0-19) - NEVER through a
        coordinator action knob.

        Emulated mode: the ChannelModel is the exogenous driver (serving/neighbor
        channel gains + offered load + external disturbance) - the full signature,
        with NO TypeError fallback that would silently drop offered load /
        disturbance. Live mode: an explicitly injected, APPROVED exogenous
        environment driver (offered load / external attenuator / shielded path /
        UE position) applies the phase and reports the {axis: value} it moved; an
        absent/unapproved driver, or one that touches a non-environment axis or
        silently drops a requested axis, FAILS CLOSED with ZERO executor writes.
        The environment is NEVER written to executor.set_power_offset / any action
        axis. The ACTUAL applied monotonic time is recorded (keyed by the UNIQUE
        phase identity) ONLY after a successful application.
        """
        if hasattr(self.coordinator, "current_phase"):
            self.coordinator.current_phase = phase.get("name", "")
        # the environment axes/values this phase requests (every key an
        # ENVIRONMENT axis, every value a finite number).
        requested = self._phase_requested_env(phase)

        if self.channel_model is not None:
            # EMULATED exogenous driver: the ChannelModel. CAPABILITY CHECK first
            # - a requested axis the ChannelModel cannot apply (e.g. ue_position)
            # must FAIL, never be silently ignored (P0-19). Full signature, NO
            # TypeError fallback that would drop offered load / disturbance.
            from experiments.environment import AxisOverlapError
            unsupported = set(requested) - self._CHANNEL_ENV_AXES
            if unsupported or not callable(
                    getattr(self.channel_model, "set_environment", None)):
                raise AxisOverlapError(
                    f"emulated ChannelModel cannot apply environment axes "
                    f"{sorted(unsupported)} (supports "
                    f"{sorted(self._CHANNEL_ENV_AXES)}) or has no callable "
                    f"set_environment - refusing to silently ignore them (P0-19)")
            self.channel_model.set_environment(
                requested["channel_serving_gain_db"],
                requested["channel_neighbor_gain_db"],
                external_disturbance_db=requested.get(
                    "external_disturbance_db", 0.0),
                offered_load_mbps=requested.get("offered_load_mbps"))
        else:
            # LIVE: require an explicitly injected, APPROVED exogenous driver that
            # touches ONLY environment axes. Absent/unapproved -> FAIL CLOSED
            # BEFORE any write; the environment is NEVER driven through the
            # executor action knob (no set_power_offset here at all).
            from experiments.environment import (
                validate_exogenous_driver, require_driver_capability,
                validate_applied_environment)
            validate_exogenous_driver(self.env_driver)
            # CAPABILITY (before apply): an insufficient driver applies NOTHING
            # (fail closed), never a partial write.
            require_driver_capability(self.env_driver, requested.keys())
            applied = self.env_driver.apply(phase, phase_idx, phase_label)
            # a partial/legacy driver cannot silently drop a requested axis or
            # report a wrong value, and cannot touch a non-environment axis.
            validate_applied_environment(applied, requested)

        # record the ACTUAL applied monotonic time ONLY after a SUCCESSFUL apply,
        # keyed by the UNIQUE phase identity (never the bare name - the two
        # Nominal phases must stay distinct: p0:Nominal vs p4:Nominal).
        self._env_apply_log.append({
            "phase_idx": phase_idx,
            "phase_name": phase.get("name"),
            "phase_label": phase_label,
            "applied_monotonic_s": time.monotonic(),
            "requested_axes": sorted(requested.keys()),
        })

    def _read_action_offsets(self) -> Dict[str, float]:
        """Recorded action offsets - only what is actually observable.

        Fail-closed (C4): a gNB with no observable offset is OMITTED, never
        defaulted to 0.0, so the I1 metric can distinguish "missing
        constrained offset" (unsatisfied) from a measured zero (satisfied).
        """
        ex = getattr(self.coordinator, "executor", None)
        if ex is None:
            return {}
        offs = ex.get_all_offsets()
        out = {}
        if "gnb1" in offs:
            out["bs1"] = offs["gnb1"]
        if "gnb2" in offs:
            out["bs2"] = offs["gnb2"]
        return out

    def _i2_violated(self, ue_kpis: Dict[str, Dict]) -> bool:
        """One source of truth: the metric layer's satisfaction predicate
        (missing configured UEs and NaN/Inf observations count as violated)."""
        for ue in self.cfg.i2_ue_scope(ue_kpis.keys()):
            t = ue_kpis.get(ue, {}).get("throughput_mbps")
            if not self.cfg.i2_satisfied_ue(t):
                return True
        return False

    @staticmethod
    def _clip_axis_label(c: Dict) -> str:
        """Target-qualified ACTUAL axis of a clip event (H2 cleanup: every
        clip used to be hardcoded '<gnb>_power', mislabeling PRB/MCS/
        scheduling-priority clips in the enforcement statistics)."""
        target = c.get("ue_id") or c.get("gnb_id") or "?"
        return f"{target}_{c.get('axis', 'power')}"

    @staticmethod
    def _mean_tput(ue_kpis: Dict[str, Dict]) -> float:
        vals = [k.get("throughput_mbps") for k in ue_kpis.values()
                if k.get("throughput_mbps") is not None]
        return sum(vals) / len(vals) if vals else 0.0

    def _episode_from_result(self, method: str, trial_id: int, phase: str,
                             result: Dict, trace: Dict, tp_before: float,
                             phase_idx: Optional[int] = None
                             ) -> List[EpisodeRecord]:
        """Build EpisodeRecords from a process_intent result + state trace.

        C1: one record PER COORDINATION CYCLE (result["cycles"]) - each
        cycle carries its own S2 confidence and its own trial outcome, the
        statistical unit for ECE and cost estimation. `success` on every
        record is the episode's FINAL resolution. Results without a cycles
        list (legacy/mocked) fall back to a single top-level record.
        """
        cycles = result.get("cycles")
        if cycles:
            # P1-3 (blocker 1): NEVER split the episode total across cycles. Each
            # cycle carries its OWN measured segment latencies; the real total is
            # attributed ONCE (to cycle 0) inside _episode_from_cycle. A 90 ms
            # 3-cycle episode must not become three 30 ms inference samples.
            return [self._episode_from_cycle(method, trial_id, phase, result,
                                             cyc, tp_before,
                                             phase_idx=phase_idx)
                    for cyc in cycles]
        return [self._legacy_episode_from_result(method, trial_id, phase,
                                                 result, trace, tp_before,
                                                 phase_idx=phase_idx)]

    @staticmethod
    def _budget_from_ledger(led: Optional[Dict], cycle_id=None,
                            cycle_index=None) -> Dict:
        """Honest PER-CYCLE budget provenance from a reserve-ledger dict (Batch
        G). Filters the ledger's reservation entries to THIS cycle (by cycle_id
        when the entry has one, else by cycle_index) so a cycle row carries ONLY
        its own reservations/settlements - never the episode aggregate. For the
        matching entries:
          * before   = cap - first_matching.reserved_before
          * reserved = sum of ACCEPTED matching amounts
          * after    = cap - last_matching.cumulative_reserved
          * debited  = sum of settlement actuals ONLY when EVERY accepted matching
                       entry is settled with status==settled; otherwise None (an
                       unset / partial / unknown accepted entry), with
                       lower_bound = sum(known settled actual) + sum(partial
                       actual_lower_bound), and an honest status.
          * rejected-only cycle -> reserved=0, debited=0 (before/after unchanged,
            since a rejected reservation does not move cumulative_reserved).
        No matching entries (or no ledger) -> all None. The returned ledger slice
        carries ONLY the matching entries and their settlements/reservation ids -
        never the whole episode ledger."""
        none_budget = {"before": None, "reserved": None, "debited": None,
                       "lower_bound": None, "status": None, "after": None,
                       "ledger": None}
        if not led:
            return dict(none_budget)
        entries = led.get("entries") or []
        settlements = led.get("settlements") or {}
        cap = led.get("c_episode")

        def _matches(e):
            eid = e.get("cycle_id")
            if cycle_id is not None and eid is not None:
                return eid == cycle_id
            if cycle_index is not None:
                return e.get("cycle_index") == cycle_index
            if cycle_id is not None:
                return eid == cycle_id
            return False

        matching = [e for e in entries if _matches(e)]
        if not matching:
            return dict(none_budget)

        first, last = matching[0], matching[-1]
        _rb = first.get("reserved_before")
        _cr = last.get("cumulative_reserved")
        before = (cap - _rb) if (cap is not None and _rb is not None) else None
        after = (cap - _cr) if (cap is not None and _cr is not None) else None

        accepted = [e for e in matching if e.get("accepted")]
        reserved = sum(float(e.get("amount") or 0.0) for e in accepted)

        match_rids = [e.get("reservation_id") for e in matching]
        ledger_slice = {
            "c_episode": cap,
            "entries": [dict(e) for e in matching],
            "settlements": {rid: dict(settlements[rid]) for rid in match_rids
                            if rid in settlements},
        }

        if not accepted:
            # rejected-only cycle: nothing reserved or spent; before/after are
            # unchanged (cumulative_reserved == reserved_before on a rejection).
            return {"before": before, "reserved": 0.0, "debited": 0.0,
                    "lower_bound": 0.0, "status": "settled", "after": after,
                    "ledger": ledger_slice}

        all_settled = True
        known_actual = 0.0
        lower_bound = 0.0
        for e in accepted:
            s = settlements.get(e.get("reservation_id"))
            if not e.get("settled") or s is None:
                all_settled = False          # accepted but unsettled -> UNKNOWN
                continue
            st = s.get("status")
            if st == "settled":
                a = s.get("actual")
                if a is None:
                    # a SETTLED status with a MISSING actual is not a real total:
                    # treat as UNKNOWN (debit null), never a fabricated settled 0.
                    all_settled = False
                else:
                    known_actual += float(a)
                    lower_bound += float(a)
            elif st == "partial_unknown":
                all_settled = False
                lb = s.get("actual_lower_bound")
                if lb is not None:
                    lower_bound += float(lb)
            else:                            # unknown: no lower-bound contribution
                all_settled = False

        if all_settled:
            return {"before": before, "reserved": reserved,
                    "debited": known_actual, "lower_bound": known_actual,
                    "status": "settled", "after": after, "ledger": ledger_slice}
        return {"before": before, "reserved": reserved, "debited": None,
                "lower_bound": lower_bound, "status": "unknown",
                "after": after, "ledger": ledger_slice}

    @staticmethod
    def _applied_map(applied) -> Dict:
        """The SAME flattened applied-map the coordinator's EvidenceRecord builds
        for its clipped_action: {gnb.ue_or_cell.axis: value} over the cycle's
        actual applied-write list. NEVER canonical_action relabeled as clipped."""
        return {
            f"{a.get('gnb_id')}.{a.get('ue_id') or 'cell'}.{a.get('axis')}":
                a.get("value")
            for a in (applied or []) if isinstance(a, dict)}

    def _episode_from_cycle(self, method: str, trial_id: int, phase: str,
                            result: Dict, cyc: Dict, tp_before: float,
                            phase_idx: Optional[int] = None) -> EpisodeRecord:
        """One EpisodeRecord for one coordination cycle (C1). NEVER receives a
        per-cycle split of the episode total (P1-3 blocker 1): each cycle's
        latency fields are its OWN measured segments; the episode total is the
        real measured value attributed once to cycle 0."""
        nstats = cyc.get("nego_stats") or {}
        tstats = cyc.get("trial_stats") or {}
        trial_success = cyc.get("trial_success")
        clips = [ClipEvent(axis=self._clip_axis_label(c),
                           proposed=float(c.get("proposed", 0.0)),
                           applied=float(c.get("applied", 0.0)))
                 for c in cyc.get("clipped", []) or []]
        # Batch D raw/calibrated schema (blocker 3): carry the recorded raw vs
        # calibrated fields from the cycle into the exported EpisodeRecord.
        _raw = cyc.get("raw_confidence", cyc.get("confidence", 0.0))
        _cctx = cyc.get("calibration_context") or {}
        # Batch G (P1-6 review blocker): CYCLE-LOCAL provenance. Each EpisodeRecord
        # row is ONE coordination cycle and MUST carry THAT cycle's own facts - the
        # proposer/model pinned for this cycle, this cycle's (possibly REVISED)
        # current-intent snapshot + content hash, prompt/action/readback. The final
        # per-episode EvidenceRecord (result["evidence"]) and the top-level
        # result["pending_intent"] are the LAST cycle's values only; copying them
        # into every row would overwrite a revised re-entry cycle with the original
        # intent, so they are NOT sourced here (they are cross-checked against the
        # FINAL cycle below, never copied into earlier rows).
        _ledger = cyc.get("reserve_ledger") or result.get("reserve_ledger") or {}
        # PER-CYCLE budget: filter the ledger to THIS cycle's own reservations -
        # no row repeats the episode aggregate.
        _budget = self._budget_from_ledger(_ledger, cycle_id=cyc.get("cycle_id"),
                                           cycle_index=cyc.get("cycle"))
        # EXPLICIT parse source fact: this cycle carries a parsed Intent snapshot
        # ONLY when the coordinator stamped one (cyc["current_intent"]); an absent
        # snapshot is a pre-parse row (id None). The exporter keys on the bool, so
        # a parsed-but-id-missing cycle fails closed instead of masking.
        _pi = cyc.get("current_intent")
        _pi_parsed = isinstance(_pi, dict)
        _pi_id = (cyc.get("current_intent_id")
                  or (_pi.get("id") if _pi_parsed else None))
        # the FULL exact type/target/scope from THIS cycle's intent snapshot (scope
        # is the {ue_ids, bs_ids} dict, NOT flattened to a UE-id list).
        _pi_type = (cyc.get("current_intent_type")
                    or (_pi.get("type") if _pi_parsed else None))
        _pi_target = (cyc.get("current_intent_target")
                      if cyc.get("current_intent_target") is not None
                      else (_pi.get("target") if _pi_parsed else None))
        _pi_scope = (cyc.get("current_intent_scope")
                     if cyc.get("current_intent_scope") is not None
                     else (_pi.get("scope") if _pi_parsed else None))
        # the flattened applied-map for THIS cycle (same shape EvidenceRecord uses
        # for clipped_action) - built from the actual applied-write list, never the
        # canonical action relabeled as clipped.
        _clipped_map = self._applied_map(cyc.get("applied_action"))
        # FINAL-cycle consistency (never copied into earlier rows): the LAST cycle's
        # own provenance MUST agree with the finalized EvidenceRecord - a mismatch
        # is a real integrity fault and FAILS CLOSED (ValueError), not a warning.
        # Each field is checked ONLY when the Evidence actually provides it (not
        # None). Earlier cycles are never checked (a revision legitimately differs).
        _cycles = result.get("cycles") or []
        if _cycles and cyc is _cycles[-1]:
            _fe = result.get("evidence") or {}
            for _k, _cv, _ev in (
                    # P1-6 (review item 4): run/fsm/terminal that the raw exporter
                    # threads from top-level/cycle MUST agree with the FINALIZED
                    # EvidenceRecord - a top-level value inconsistent with the
                    # authoritative evidence FAILS CLOSED (checked only when the
                    # Evidence provides the field).
                    ("experiment_run_id", result.get("experiment_run_id"),
                     _fe.get("experiment_run_id")),
                    ("fsm_step_id",
                     cyc.get("fsm_step_id") or result.get("fsm_step_id"),
                     _fe.get("fsm_step_id")),
                    ("terminal_outcome", result.get("terminal_outcome"),
                     _fe.get("terminal_outcome")),
                    ("terminal_reason", result.get("terminal_reason"),
                     _fe.get("terminal_reason")),
                    # the raw exporter uses the CYCLE's identifier chain, so the
                    # FINAL cycle's ids must EXACTLY match the authoritative
                    # finalized EvidenceRecord (checked only when the Evidence
                    # provides a meaningful value; stage-honest None is preserved).
                    ("episode_id", cyc.get("episode_id"), _fe.get("episode_id")),
                    ("cycle_id", cyc.get("cycle_id"), _fe.get("cycle_id")),
                    ("proposal_id", cyc.get("proposal_id"),
                     _fe.get("proposal_id")),
                    ("actuation_trial_id", cyc.get("actuation_trial_id"),
                     _fe.get("actuation_trial_id")),
                    ("proposer_id", cyc.get("proposer_id"),
                     _fe.get("proposer_id")),
                    ("model_version", cyc.get("model_version"),
                     _fe.get("model_version")),
                    ("pending_intent_hash", cyc.get("pending_intent_hash"),
                     _fe.get("pending_intent_hash")),
                    ("prompt_hash", cyc.get("prompt_hash"),
                     _fe.get("prompt_hash")),
                    ("requested_action", cyc.get("requested_action"),
                     _fe.get("requested_action")),
                    ("clipped_action", _clipped_map, _fe.get("clipped_action")),
                    ("canonical_action", cyc.get("canonical_action"),
                     _fe.get("canonical_action")),
                    ("canonical_action_hash", cyc.get("canonical_action_hash"),
                     _fe.get("canonical_action_hash"))):
                # check whenever the Evidence provides a MEANINGFUL value (not
                # None / "" / {} - the terminal builder's benign defaults). A
                # MISSING cycle value against a meaningful Evidence value is itself
                # an inconsistency and FAILS CLOSED (no cycle-present skip): the
                # cycle stamps proposer/model/pending-hash/prompt at its boundary,
                # so a None there for a provided Evidence field is a real fault.
                if _ev and _cv != _ev:
                    raise ValueError(
                        f"final cycle {_k} {_cv!r} != finalized evidence {_ev!r} "
                        f"- refusing to emit an inconsistent per-cycle record")
        return EpisodeRecord(
            trial_id=trial_id, method=method, phase=phase,
            phase_idx=phase_idx,
            # CYCLE-LOCAL monotonic timestamp when the coordinator stamped one
            # (each re-entry cycle gets its own), else the episode-level value.
            episode_monotonic_s=(cyc.get("cycle_monotonic_s")
                                 if cyc.get("cycle_monotonic_s") is not None
                                 else result.get("episode_monotonic_s")),
            cycle_idx=int(cyc.get("cycle", 0)),
            confidence=float(cyc.get("confidence", 0.0)),
            raw_confidence=(float(_raw) if _raw is not None else None),
            calibrated_probability=cyc.get("calibrated_probability"),
            threshold=cyc.get("threshold", cyc.get("theta_star")),
            threshold_applied_to=cyc.get("threshold_applied_to",
                                         "calibrated_probability"),
            model_id=str(_cctx.get("model_id", "unknown")),
            operating_regime_id=str(_cctx.get("operating_regime_id",
                                              phase or "default")),
            # coverage consistency: the ACTUAL recorded eligibility bool
            # (matches the calibrator counter), NOT merely 'raw_confidence in cyc'
            proposal_eligible=bool(cyc.get("proposal_eligible", False)),
            proposal_executed=(trial_success is not None),
            # theta* as it stood AT this cycle's decision (recorded in-loop)
            theta_star=float(cyc.get("theta_star", 0.0)),
            predicted_feasible=bool(cyc.get("feasible", False)),
            # P0-5: the ACTUAL strict-schema validator verdict for this cycle's
            # proposal (recorded in-loop), not merely "no top-level error".
            # P0-5 / P1-6: the ACTUAL recorded strict-schema verdict, threaded
            # WITHOUT bool()-coercion (True pass / False reject / None when no
            # proposal was generated). The explicit proposal-generated source fact
            # is threaded too, so the raw exporter never infers the proposal stage
            # from the prompt hash.
            schema_valid=cyc.get("schema_valid"),
            proposal_generated=cyc.get("proposal_generated"),
            routed_to=cyc.get("routed_to", "negotiation"),
            trial_executed=trial_success is not None,
            rolled_back=bool(cyc.get("rolled_back", False)),
            entered_negotiation=bool(nstats),
            negotiation_rounds=int(nstats.get("rounds", 0)),
            # episode FINAL resolution (same value on every cycle record)
            success=bool(result.get("success", False)),
            trial_success=(None if trial_success is None
                           else bool(trial_success)),
            restore_verified=cyc.get("restore_verified"),
            hard_failure=bool(cyc.get("hard_failure", False)),
            measurement_invalid=bool(cyc.get("measurement_invalid", False)),
            clips=clips,
            tau_trial=float(tstats.get(
                "tau", getattr(self.coordinator, "tau_trial_s", 15.0))),
            throughput_before=(float(tstats["tput_before"])
                               if tstats.get("tput_before") is not None
                               else (tp_before if tp_before is not None
                                     else None)),
            # Batch F/G: an UNKNOWN (None) throughput is flagged (throughput_unknown,
            # excluded from cost). The numeric field STAYS None - never a fabricated
            # 0 Mbps; the honest UNKNOWN lives in throughput_unknown + the samples.
            throughput_after=(float(tstats["tput_after"])
                              if tstats.get("tput_after") is not None else None),
            throughput_trial_min=(float(tstats["tput_min"])
                                  if tstats.get("tput_min") is not None
                                  else None),
            throughput_during_nego=(float(nstats["tput_during_nego"])
                                    if nstats.get("tput_during_nego") is not None
                                    else None),
            throughput_unknown=bool(
                tstats.get("throughput_unknown")
                or tstats.get("tput_before") is None
                or tstats.get("tput_after") is None
                or tstats.get("tput_min") is None
                or tstats.get("baseline_unknown")),
            nego_throughput_unknown=bool(
                nstats.get("tput_during_nego_unknown")),
            nego_duration=float(nstats.get("duration_s", 0.0)),
            # Batch G (E): a MISSING reconnection time stays None (UNKNOWN) - never
            # coerced to 0.0 (which would fabricate an instant reconnection).
            reconnection_time=(float(cyc["reconnection_time"])
                               if cyc.get("reconnection_time") is not None
                               else None),
            # Batch F (#d8b9): timestamped trajectories so live/emulated costs
            # integrate the real trajectory (trajectory_used) not legacy min*tau.
            trial_trajectory=[tuple(p) for p in (tstats.get("trajectory") or [])],
            nego_trajectory=[tuple(p) for p in (nstats.get("trajectory") or [])],
            # Batch F (#726e): the exact measurement provenance into raw export.
            pre_action_baseline=cyc.get("pre_action_baseline"),
            probe_config=cyc.get("probe_config"),
            measurement_samples=list(cyc.get("measurement_samples") or []),
            # Batch G (P0-20 review): thread the coordinator's REAL evidence
            # identifiers + applied timestamps so the raw export never fabricates
            # them. actuation_trial_id is present ONLY when a real S3 write ran.
            evidence_episode_id=cyc.get("episode_id"),
            evidence_cycle_id=cyc.get("cycle_id"),
            evidence_proposal_id=cyc.get("proposal_id"),
            evidence_actuation_trial_id=cyc.get("actuation_trial_id"),
            action_apply_time=cyc.get("action_apply_time"),
            action_apply_monotonic_s=cyc.get("action_apply_monotonic_s"),
            final_readback_time=cyc.get("final_readback_time"),
            applied_action=cyc.get("applied_action"),
            readback_action=cyc.get("final_readback"),
            # Batch G (P1-6 review blocker): thread the REST of the real evidence
            # provenance + terminal + budget so the raw export never fabricates
            # them. A field the coordinator did not produce for THIS terminal
            # (e.g. no proposal / no write) is preserved as None.
            evidence_experiment_run_id=result.get("experiment_run_id"),
            evidence_fsm_step_id=cyc.get("fsm_step_id") or result.get("fsm_step_id"),
            evidence_was_parsed_intent=_pi_parsed,
            evidence_pending_intent_id=_pi_id,
            evidence_intent_type=_pi_type,
            evidence_intent_target=_pi_target,
            evidence_intent_scope=_pi_scope,
            # CYCLE-LOCAL provenance: THIS cycle's own content hash, pinned
            # proposer/model, and prompt hash - never the finalized episode
            # evidence (which is the LAST cycle's only).
            evidence_pending_intent_hash=cyc.get("intent_content_hash"),
            evidence_proposer_id=cyc.get("proposer_id"),
            evidence_model_version=cyc.get("model_version"),
            # the cycle's stamped-at-generation prompt hash (retained on timeout).
            evidence_prompt_hash=cyc.get("prompt_hash"),
            # preserve EXACT {} vs None (empty recorded action vs no action at all)
            evidence_requested_action=cyc.get("requested_action"),
            # clipped_action is the flattened applied-map (gnb.ue_or_cell.axis ->
            # value) built from THIS cycle's actual applied writes - the SAME shape
            # EvidenceRecord uses. NEVER the canonical action relabeled as clipped.
            evidence_clipped_action=_clipped_map,
            evidence_canonical_action=cyc.get("canonical_action"),
            evidence_canonical_action_hash=cyc.get("canonical_action_hash"),
            # the coordinator's REAL per-episode terminal decision (same value on
            # every cycle record of the episode); missing stays None.
            terminal_outcome=result.get("terminal_outcome"),
            terminal_reason=result.get("terminal_reason"),
            # reserve-ledger budget snapshot (Mbps*s); an UNKNOWN/partial debit
            # stays None (with a lower bound + status + full ledger), never a
            # fabricated 0. None throughout when no ledger ran.
            budget_before=_budget["before"],
            budget_reserved=_budget["reserved"],
            budget_debited=_budget["debited"],
            budget_debited_lower_bound=_budget["lower_bound"],
            budget_debit_status=_budget["status"],
            budget_after=_budget["after"],
            reserve_ledger=_budget["ledger"],
            # Batch G (P1-3): REAL per-segment latency threaded from the
            # coordinator's measured timestamps - NEVER a total-episode latency
            # split across cycles. Each is None when its segment was not measured.
            #   inference_ms   = the measured proposal model-call latency (cycle)
            #   negotiation_ms = the measured S5 negotiation duration (cycle)
            #   total_latency_ms = the ACTUAL measured total episode latency,
            #     attributed ONCE (to cycle 0) so the episode total is not
            #     multiply counted across a multi-cycle episode's records.
            inference_ms=cyc.get("inference_ms"),
            # parse is an EPISODE-level segment (one parse per episode); attribute
            # it once, to cycle 0, so a multi-cycle episode does not repeat it.
            parse_ms=(float(result["parse_ms"])
                      if result.get("parse_ms") is not None
                      and int(cyc.get("cycle", 0)) == 0 else None),
            schema_admission_ms=cyc.get("schema_admission_ms"),
            executor_write_ms=cyc.get("executor_write_ms"),
            readback_ms=cyc.get("readback_ms"),
            validation_ms=cyc.get("validation_ms"),
            rollback_ms=cyc.get("rollback_ms"),
            recovery_ms=cyc.get("recovery_ms"),
            # P1-3: prefer the cycle's EXCEPTION-SAFE monotonic negotiation timer
            # (retained on timeout/error); fall back to the nego-stats duration for
            # legacy/mocked cycles that predate the cycle-level timer.
            negotiation_ms=(float(cyc["negotiation_ms"])
                            if cyc.get("negotiation_ms") is not None
                            else (float(nstats["duration_s"]) * 1000.0
                                  if nstats.get("duration_s") is not None
                                  else None)),
            total_latency_ms=(float(result["latency_ms"])
                              if result.get("latency_ms") is not None
                              and int(cyc.get("cycle", 0)) == 0 else None),
        )

    def _legacy_episode_from_result(self, method: str, trial_id: int,
                                    phase: str, result: Dict, trace: Dict,
                                    tp_before: float,
                                    phase_idx: Optional[int] = None
                                    ) -> EpisodeRecord:
        """Single-record fallback for results without a cycles list."""
        feas = result.get("feasibility", {}) or {}
        # real episode-level provenance for a NO-CYCLE terminal (Batch G P1-6).
        _lpi = result.get("pending_intent")
        _lparsed = isinstance(_lpi, dict)
        _lev = result.get("evidence") or {}
        _lbudget = self._budget_from_ledger(result.get("reserve_ledger") or {})
        clips = [ClipEvent(axis=self._clip_axis_label(c),
                           proposed=float(c.get("proposed", 0.0)),
                           applied=float(c.get("applied", 0.0)))
                 for c in result.get("clipped", []) or []]
        theta = 0.0
        if hasattr(self.coordinator, "calibrator"):
            theta = self.coordinator.calibrator.get_theta_star()
        # P1-3 (blocker 1): NO fake-zero total. The record below uses the real
        # measured result["latency_ms"] with None preserved when unmeasured.
        trial_success = result.get("trial_success")
        tstats = result.get("trial_stats") or {}
        nstats = result.get("nego_stats") or {}
        ep = EpisodeRecord(
            trial_id=trial_id, method=method, phase=phase,
            phase_idx=phase_idx,
            episode_monotonic_s=result.get("episode_monotonic_s"),
            # a NO-CYCLE terminal never generated a proposal (explicit fact for
            # the raw exporter's proposal-stage / schema-verdict decisions).
            proposal_generated=False,
            confidence=float(feas.get("confidence", 0.0)),
            theta_star=theta,
            predicted_feasible=bool(feas.get("feasible", False)),
            # P0-5 (coordinator review): the recorded strict-schema verdict;
            # a MISSING verdict is FAIL-CLOSED false, never inferred true from
            # the absence of a top-level error.
            schema_valid=bool(result.get("schema_valid", False)),
            routed_to=trace["routed_to"],
            trial_executed=trace["trial_executed"],
            rolled_back=trace["rolled_back"],
            entered_negotiation=trace["entered_negotiation"],
            negotiation_rounds=int(nstats.get("rounds", 0)),
            # episode FINAL resolution (negotiation included); the trial (S4)
            # outcome is carried separately in trial_success.
            success=bool(result.get("success", False)),
            trial_success=None if trial_success is None else bool(trial_success),
            restore_verified=result.get("restore_verified"),
            hard_failure=bool(result.get("hard_failure", False)),
            measurement_invalid=bool(result.get("measurement_invalid", False)),
            clips=clips,
            # measured cost-trajectory fields (P4): trial window stats from
            # the coordinator's polled S4 validation + negotiation stats
            tau_trial=float(tstats.get(
                "tau", getattr(self.coordinator, "tau_trial_s", 15.0))),
            # prefer the coordinator's S3-entry baseline (a measured 0.0 is
            # valid); fall back to the runner's step-level measurement only
            # when the coordinator did not measure (None/absent)
            throughput_before=(float(tstats["tput_before"])
                               if tstats.get("tput_before") is not None
                               else (tp_before if tp_before is not None
                                     else None)),
            # UNKNOWN throughput STAYS None (flagged by throughput_unknown), never
            # a fabricated 0 Mbps.
            throughput_after=(float(tstats["tput_after"])
                              if tstats.get("tput_after") is not None else None),
            throughput_trial_min=(float(tstats["tput_min"])
                                  if tstats.get("tput_min") is not None
                                  else None),
            throughput_during_nego=(float(nstats["tput_during_nego"])
                                    if nstats.get("tput_during_nego") is not None
                                    else None),
            throughput_unknown=bool(
                tstats.get("throughput_unknown")
                or tstats.get("tput_before") is None
                or tstats.get("tput_after") is None
                or tstats.get("tput_min") is None
                or tstats.get("baseline_unknown")),
            nego_throughput_unknown=bool(
                nstats.get("tput_during_nego_unknown")),
            nego_duration=float(nstats.get("duration_s", 0.0)),
            # Batch G (E): a MISSING reconnection time stays None (UNKNOWN).
            reconnection_time=(float(result["reconnection_time"])
                               if result.get("reconnection_time") is not None
                               else None),
            # Batch F (#d8b9 / #726e): trajectories + measurement provenance.
            trial_trajectory=[tuple(p) for p in (tstats.get("trajectory") or [])],
            nego_trajectory=[tuple(p) for p in (nstats.get("trajectory") or [])],
            # Batch F (review): this is the NO-CYCLES fallback - read the TOP-
            # LEVEL result mirror (never a cycles[] that does not exist here).
            pre_action_baseline=result.get("pre_action_baseline"),
            probe_config=result.get("probe_config"),
            measurement_samples=list(result.get("measurement_samples") or []),
            # Batch G (P1-6): a NO-CYCLE terminal (pre-parse / single-flight /
            # latched) still carries its REAL episode-level provenance so the raw
            # export uses the coordinator's ids, not fabrications. Cycle-stage ids
            # (cycle/proposal/actuation/canonical/prompt/budget) are honestly None
            # here - no cycle ran. was_parsed_intent is the explicit parse fact.
            evidence_experiment_run_id=result.get("experiment_run_id"),
            evidence_fsm_step_id=result.get("fsm_step_id"),
            evidence_was_parsed_intent=_lparsed,
            evidence_pending_intent_id=(_lpi.get("id") if _lparsed else None),
            evidence_intent_type=(_lpi.get("type") if _lparsed else None),
            evidence_intent_target=(_lpi.get("target") if _lparsed else None),
            evidence_intent_scope=(_lpi.get("scope") if _lparsed else None),
            evidence_pending_intent_hash=_lev.get("pending_intent_hash"),
            evidence_proposer_id=_lev.get("proposer_id"),
            evidence_model_version=_lev.get("model_version"),
            terminal_outcome=result.get("terminal_outcome"),
            terminal_reason=result.get("terminal_reason"),
            budget_before=_lbudget["before"],
            budget_reserved=_lbudget["reserved"],
            budget_debited=_lbudget["debited"],
            budget_debited_lower_bound=_lbudget["lower_bound"],
            budget_debit_status=_lbudget["status"],
            budget_after=_lbudget["after"],
            reserve_ledger=_lbudget["ledger"],
            # Batch G (P1-3): the ACTUAL measured total episode latency (a NO-CYCLE
            # terminal has exactly one record, so it is attributed here). Other
            # component segments are None (no cycle ran). parse_ms is the real
            # measured parse latency; inference_ms is NEVER the total.
            parse_ms=(float(result["parse_ms"])
                      if result.get("parse_ms") is not None else None),
            total_latency_ms=(float(result["latency_ms"])
                              if result.get("latency_ms") is not None else None),
        )
        return ep

    # -- analysis / persistence ---------------------------------------------

    def analyze(self, steps: List[StepRecord], episodes: List[EpisodeRecord],
                priors: Optional[CostPriors] = None) -> Dict[str, Dict]:
        """Compute the full metric set per method."""
        return compute_multi_method(steps, episodes, self.cfg, self.c_episode, priors)

    def save_records(self, steps: List[StepRecord], episodes: List[EpisodeRecord],
                     label: str = "experiment"):
        """Persist raw records (CSV + JSON) so Phase 2 / re-analysis can reload."""
        base = self.output_dir / f"{label}_{self.session_id}"
        # steps CSV (flattened: one row per (step, ue))
        with open(f"{base}_steps.csv", "w", newline="") as f:
            w = csv.writer(f)
            # Batch F (#8): HONEST CSV headers - never ambiguous rsrp/sinr.
            w.writerow(["trial_id", "method", "phase", "step_idx", "t_s", "ue",
                        "throughput_mbps", "gnb_ul_avg_rsrp_dbm", "gnb_ul_snr_db",
                        "ue_dl_ss_rsrp_dbm", "ue_dl_sinr_db", "radio_source",
                        "bs1_off", "bs2_off"])
            for s in steps:
                for ue, k in s.ue_kpis.items():
                    w.writerow([s.trial_id, s.method, s.phase, s.step_idx, s.t_s,
                                ue, k.get("throughput_mbps"),
                                k.get("gnb_ul_avg_rsrp_dbm"), k.get("gnb_ul_snr_db"),
                                k.get("ue_dl_ss_rsrp_dbm"), k.get("ue_dl_sinr_db"),
                                k.get("radio_source"),
                                s.action_offsets.get("bs1"),
                                s.action_offsets.get("bs2")])
        # Batch G: STRICT finite JSON over every artifact - NaN/Inf are
        # sanitised to null (explicit unknown), never emitted or faked to 0.
        from experiments.paired_runner import sanitize_finite
        with open(f"{base}_steps.json", "w") as f:
            json.dump(sanitize_finite([s.to_dict() for s in steps]), f,
                      indent=2, allow_nan=False)
        with open(f"{base}_episodes.json", "w") as f:
            json.dump(sanitize_finite([e.to_dict() for e in episodes]), f,
                      indent=2, allow_nan=False)
        return str(base)

    def save_metrics(self, metrics: Dict, label: str = "experiment",
                     meta: Optional[Dict] = None) -> str:
        """Persist metrics; with `meta`, a reproducibility header (_meta:
        seed, git hash, mode, theta mode, config snapshot) is prepended."""
        path = self.output_dir / f"{label}_{self.session_id}_metrics.json"
        payload = {"_meta": meta, **metrics} if meta is not None else metrics
        # Batch G: the synthetic metrics JSON previously emitted NaN for empty
        # statistics; sanitise NaN/Inf -> null (explicit unknown) and forbid
        # non-finite constants (allow_nan=False) over the whole artifact.
        from experiments.paired_runner import sanitize_finite
        with open(path, "w") as f:
            json.dump(sanitize_finite(payload), f, indent=2, default=str,
                      allow_nan=False)
        return str(path)

    def _run_meta(self, mode: str, seed: int) -> Dict:
        """Reproducibility snapshot recorded into metrics.json (P10)."""
        from dataclasses import asdict
        # Batch E (P0-15): record the history-ablation setup + switch audit so
        # the raw outputs document exactly how the paired methods differed.
        history_meta = {
            "policy": self.history_policy,
            "reset_scope": self.history_reset_scope,
            "with_history_mode": ("online_reservoir"
                                  if self.history_policy == "online"
                                  else "frozen_reservoir"),
            "no_history_mode": "disabled",
            "snapshot_id": self.history_snapshot_id,
            "shared_start_snapshot_size": len(self.paper_history_snapshot),
            "paired_rng_state": bool(self.pair_state_enabled),
            # reproducibility of the deterministic paired-ablation reseed:
            "pair_master_seed": int(self.experiment_seed),
            "pair_seed_scheme": self.pair_seed_scheme,
            "paired_methods": list(self.paired_methods),
        }
        # Batch F (P0-14): the reproducible throughput-probe configuration.
        probe_meta = dict(self.probe_config.to_dict())
        probe_meta["capacity_saturation_floor_mbps"] = \
            self.capacity_saturation_floor_mbps
        if mode == "synthetic":
            # the synthetic path draws llm_with_history vs llm_no_history from
            # the FIXED METHOD_PROFILES (a synthetic assumption), NOT the real
            # history mechanism - label it so a reader never mistakes the
            # profile-driven gap for a measured history effect.
            history_meta["synthetic_assumption"] = (
                "in synthetic mode the with/no-history gap is drawn from the "
                "fixed METHOD_PROFILES, not the live history retrieval/append "
                "path (which is exercised only in live/emulated mode)")
        switch_audit = []
        get_audit = getattr(self.coordinator, "get_switch_audit", None)
        if callable(get_audit):
            try:
                switch_audit = get_audit()
            except Exception:
                switch_audit = []
        return {
            "seed": seed,
            "git_hash": _git_hash(),
            "mode": mode,
            "theta_mode": self.theta_mode,
            # P0-20: this single-runner (NON-paired) result is NEVER paper-ready.
            # The paper performance table admits ONLY via
            # PairedBlockRunner.paper_export over a paired, attested run.
            "paper_ready": False,
            "paper_export": ("use PairedBlockRunner.paper_export over a paired "
                             "attested live_ota run; a non-paired run is not "
                             "paper data"),
            "history": history_meta,
            "model_switch_audit": switch_audit,
            "probe": probe_meta,
            "config": {
                "c_episode": self.c_episode,
                "kpi_interval_s": self.kpi_interval_s,
                "steps_per_phase": self.steps_per_phase,
                "phases": self.phases,
                "intent_config": asdict(self.cfg),
                "tau_trial_s": getattr(self.coordinator, "tau_trial_s", None),
                "carry_calibration": self.carry_calibration,
                "skip_unwired": self.skip_unwired,
            },
        }

    def make_figures(self, steps, episodes, metrics, headline_method=None,
                     label: str = "experiment") -> Dict:
        from experiments import figures
        sub = self.output_dir / f"{label}_{self.session_id}_figures"
        return figures.generate_all_figures(
            steps, episodes, metrics, cfg=self.cfg,
            output_dir=str(sub), headline_method=headline_method,
            column=self.figures_column)

    # -- top-level orchestration --------------------------------------------

    def run(self, mode: str = "synthetic", methods: Optional[List[str]] = None,
            trials: int = 5, make_figures: bool = True, seed: int = 12345,
            headline_method: str = DEFAULT_HEADLINE_METHOD) -> Dict:
        """Run the experiment end-to-end and produce metrics + figures + report."""
        methods = methods or DEFAULT_METHODS
        # Batch E: the experiment seed drives the DETERMINISTIC per-(trial,model)
        # paired-ablation reseed (independent of any treatment's consumption).
        self.experiment_seed = int(seed)
        logger.info("Experiment: mode=%s methods=%s trials=%d", mode, methods, trials)

        if mode in ("live", "emulated"):
            # emulated drives the SAME real-coordinator path as live; only
            # the injected executor/collector/LLM differ (see emulation.py)
            steps, episodes = self.run_live(methods, trials)
        elif mode == "synthetic":
            steps, episodes = self.run_synthetic(methods, trials, seed)
        else:
            raise ValueError(f"unknown mode {mode!r} "
                             f"(use 'synthetic', 'emulated' or 'live')")

        metrics = self.analyze(steps, episodes)
        rec_base = self.save_records(steps, episodes)
        met_path = self.save_metrics(metrics, meta=self._run_meta(mode, seed))
        figs = {}
        if make_figures:
            figs = self.make_figures(steps, episodes, metrics, headline_method)

        report = self.generate_report(metrics)
        rep_path = self.output_dir / f"report_{self.session_id}.txt"
        rep_path.write_text(report)
        logger.info("Saved records=%s metrics=%s report=%s", rec_base, met_path, rep_path)
        return {"metrics": metrics, "figures": figs, "report": report,
                "records_base": rec_base, "metrics_path": met_path,
                "n_steps": len(steps), "n_episodes": len(episodes),
                # P0-20: a single-runner (NON-paired) run is NEVER paper data;
                # only PairedBlockRunner.paper_export admits to the paper table.
                "paper_ready": False}

    # backward-compatible entry point
    def run_experiment(self, trials: int = 5, methods: Optional[List[str]] = None,
                       mode: str = "synthetic") -> Dict:
        return self.run(mode=mode, methods=methods, trials=trials)["metrics"]

    def generate_report(self, metrics: Dict[str, Dict]) -> str:
        lines = ["=" * 72, "5-PHASE INTENT-COORDINATION EXPERIMENT REPORT",
                 "=" * 72, f"Session: {self.session_id}",
                 f"C_episode budget: {self.c_episode} Mbps*s",
                 f"Intents: I1 |{self.cfg.power_bs} offset| <= {self.cfg.power_bound_db} dB, "
                 f"I2 throughput >= {self.cfg.throughput_target_mbps} Mbps", ""]
        header = (f"{'method':20s} {'strict-sat':>10s} {'rollback':>9s} "
                  f"{'nego':>7s} {'ECE':>6s} {'theta*':>7s} {'N_max':>5s} "
                  f"{'hardfail':>8s}")
        lines.append(header)
        lines.append("-" * len(header))
        any_dagger = False
        for method, res in metrics.items():
            # PRIMARY (P1-2): strict joint satisfaction. May be None (no
            # windows) - shown as n/a, never a fake 0%.
            sj = res["strict_joint_satisfaction"]
            sj_rate = sj["strict_joint_rate"]
            ci = sj["ci95"]
            ds_str = "n/a" if sj_rate is None else f"{sj_rate:.1%}"
            rb = res["rollback"]["rate"]
            ng = res["negotiation"]["rate"]
            ece = res["ece"]["ece"]
            cost = res["cost_estimate"]
            hf = res["hard_failures"]["count"]
            dagger = "" if cost.get("assumption_ok", True) else "†"
            any_dagger = any_dagger or bool(dagger)
            lines.append(f"{method:20s} {ds_str:>10s} {rb:8.1%} {ng:6.1%} "
                         f"{ece:6.3f} {cost['theta_star']:7.3f}{dagger} "
                         f"{cost['n_max']:5d} {hf:8d}")
            ci_txt = ("n/a" if ci is None
                      else f"[{ci['low']:.3f}, {ci['high']:.3f}], n={ci['n']}")
            # diagnostic secondary: the legacy per-(UE,step) AVERAGED dual-sat.
            davg = res["dual_intent_satisfaction"]["overall"][
                "dual_satisfaction_rate"]
            lines.append(f"{'':20s}   (95% CI strict-sat: {ci_txt}; "
                         f"diag dual-sat avg={davg:.1%}; "
                         f"windows {sj['n_verifiable_windows']} verifiable"
                         f"/{sj['n_unverifiable_windows']} invalid "
                         f"[{sj['windows_status']}]; "
                         f"ECE n={res['ece']['n']}, bins={res['ece']['n_bins']})")
            # P1-2: the strict subresult's OWN violation-area / recovery /
            # rollback-verified, reported together on one line (NOT the legacy
            # rollback count). Units are labelled and no-sample shows n/a.
            va = sj["violation_area"]
            vt, vp = va["throughput"], va["power_i1"]
            lb = lambda d: "≥" if d["is_lower_bound"] else ""
            rec = sj["recovery"]
            mrec = rec["mean_time_to_recovery_s"]
            mrec_txt = "n/a" if mrec is None else f"{mrec:.1f}s"
            rbv = sj["rollback_verified"]
            rbv_txt = "n/a" if rbv["rate"] is None else f"{rbv['rate']:.1%}"
            lines.append(
                f"{'':20s}   strict detail: viol-area "
                f"tput={lb(vt)}{vt['value']:.2f} {vt['unit']} "
                f"power={lb(vp)}{vp['value']:.2f} {vp['unit']}; "
                f"recovery mean={mrec_txt} (rec={rec['n_recovered']},"
                f"cens={rec['n_censored_unrecovered']},"
                f"unk={rec['n_recovered_unknown_timing']}); "
                f"rollback-verified {rbv_txt} "
                f"({rbv['n_verified_true']}/{rbv['n_rollbacks']}, "
                f"false={rbv['n_verified_false']},unk={rbv['n_verified_unknown']})")
        lines.append("")
        lines.append("Per-phase dual-intent satisfaction "
                     "(DIAGNOSTIC SECONDARY; primary = strict joint):")
        for method, res in metrics.items():
            byp = res["dual_intent_satisfaction"]["by_phase"]
            cells = "  ".join(f"{p}={byp[p]['dual_satisfaction_rate']:.0%}" for p in byp)
            lines.append(f"  {method:20s} {cells}")
        lines.append("")
        lines.append("Cost parameters (DERIVED from measured episodes -> Eq.8/Eq.9):")
        for method, res in metrics.items():
            c = res["cost_estimate"]
            der = c["derived"]
            flag = "" if all(der.values()) else "  [* some components used priors]"
            deg = c.get("degenerate", {})
            if any(deg.values()):
                flag += ("  [!] degenerate (all-zero samples): "
                         + ",".join(k for k, v in deg.items() if v))
            n_inv = c.get("samples", {}).get("n_invalid", 0)
            if n_inv:
                flag += (f"  [i] {n_inv} measurement-invalid trial(s) "
                         f"excluded from cost samples")
            th = c["theta_star"]
            if c.get("assumption_ok", True):
                theta_txt = f"theta*={th:.3f}"
            else:
                any_dagger = True
                clamped = 0.1 if (isinstance(th, float) and th != th) \
                    else max(0.1, min(0.9, th))
                theta_txt = f"theta*={th:.3f}† (clamped {clamped:.3f})"
            lines.append(f"  {method:20s} R_success={c['r_success']:.1f} "
                         f"C_worst={c['c_worst']:.1f} C_nego={c['c_nego']:.1f} "
                         f"-> {theta_txt} N_max={c['n_max']}{flag}")
        if any_dagger:
            lines.append("")
            lines.append("† premise C_worst > C_nego violated; clamped value "
                         "shown (the live calibrator clamps theta* to "
                         "[0.1, 0.9])")
        # P1-3 (blocker: consumers report the component breakdown WITH units and
        # per-segment sample coverage; unmeasured segments show n/a, never 0).
        lines.append("")
        lines.append("Component latency (P1-3, unit=ms; MEASURED samples only):")
        for method, res in metrics.items():
            cl = res.get("component_latency", {})
            lines.append(f"  {method}:")
            shown = 0
            for seg, d in cl.items():
                if not isinstance(d, dict) or "n_measured" not in d:
                    continue                       # skip 'n'/'linkage'
                if d["status"] == "not_instrumented":
                    continue                       # keep the report concise
                mean = d.get("mean_ms")
                mean_txt = "n/a" if mean is None else f"{mean:.1f}ms"
                lines.append(
                    f"    {seg:18s} mean={mean_txt} "
                    f"(measured={d['n_measured']},unknown={d['n_unknown']},"
                    f"missing={d['n_missing']}) [{d['status']}]")
                shown += 1
            if not shown:
                lines.append("    (no segments instrumented)")
            lk = cl.get("linkage")
            if lk:
                lines.append(f"    linkage: {lk['status']} "
                             f"({lk['n_records_with_violations']} record(s), "
                             f"{lk['n_violations']} violation(s))")
        # P1-4: regret vs the DECLARED FINITE reference (never a global optimum);
        # four SEPARATE components in Mbps*s summing exactly to total, with
        # per-component coverage counts.
        lines.append("")
        lines.append("Regret decomposition (P1-4, unit=Mbps*s; vs declared finite "
                     "reference, NOT a global optimum):")
        for method, res in metrics.items():
            rg = res.get("regret")
            if not rg:
                continue
            def _c(key):
                d = rg[key]
                lb = "≥" if d.get("is_lower_bound") else ""
                return (f"{lb}{d['value']:.1f} "
                        f"(n={d['n_counted']}/{d['n_applicable']},"
                        f"unk={d['n_unknown']})")
            lines.append(
                f"  {method:20s} decision={_c('decision_regret')} "
                f"exec={_c('execution_cost')} nego={_c('negotiation_cost')} "
                f"hard={_c('hard_failure_cost')} "
                f"-> total={rg['total_regret']['value']:.1f}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Multi-LLM comparison
# ---------------------------------------------------------------------------

class MultiLLMComparison:
    """Same 5-phase scenario across LLM backends (paper Table III + reliability).

    The HEADLINE verification run uses claude-sonnet (task requirement). This
    comparison is a SEPARATE experiment that sweeps several backends, always
    including claude-sonnet as the reference row.
    """

    def __init__(self, coordinator=None, output_dir: str = "experiment_results",
                 cfg: Optional[IntentConfig] = None, c_episode: float = 500.0):
        self.runner = ExperimentRunner(coordinator, output_dir, cfg, c_episode=c_episode)
        self.coordinator = coordinator
        self.output_dir = Path(output_dir)
        self.cfg = cfg or IntentConfig()
        self.c_episode = c_episode
        self.session_id = self.runner.session_id

    def run(self, models: Optional[List[str]] = None, trials: int = 5,
            mode: str = "synthetic", make_figures: bool = True,
            meta: Optional[Dict] = None) -> Dict:
        models = models or ["claude-sonnet", "gpt-4o", "gpt-4o-mini",
                            "local:gpt-oss", "llama-3", "phi-3"]
        if HEADLINE_BACKEND not in models:
            logger.warning("headline backend %s missing from comparison - adding it",
                           HEADLINE_BACKEND)
            models = [HEADLINE_BACKEND] + models

        if mode == "synthetic":
            from experiments.synthetic import generate_model_comparison
            steps, episodes = generate_model_comparison(
                models, trials=trials, phases=self.runner.phases,
                steps_per_phase=self.runner.steps_per_phase, cfg=self.cfg)
        elif mode in ("live", "emulated"):
            steps, episodes = self._run_live(models, trials)
        else:
            raise ValueError(f"unknown mode {mode!r}")

        metrics = compute_multi_method(steps, episodes, self.cfg, self.c_episode)
        table = self._comparison_table(metrics)

        figs = {}
        if make_figures:
            from experiments import figures
            sub = self.output_dir / f"llm_comparison_{self.session_id}_figures"
            theta = metrics.get(HEADLINE_BACKEND, {}).get(
                "cost_estimate", {}).get("theta_star")
            figs = figures.generate_all_figures(
                steps, episodes, metrics, cfg=self.cfg, theta_star=theta,
                output_dir=str(sub), headline_method=HEADLINE_BACKEND,
                column=self.runner.figures_column)

        out = self.output_dir / f"llm_comparison_{self.session_id}.json"
        payload = {"table": table, "metrics": metrics}
        if meta is not None:
            payload = {"_meta": meta, **payload}
        with open(out, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        return {"table": table, "metrics": metrics, "figures": figs,
                "report": self._render_table(table), "path": str(out)}

    def _run_live(self, models: List[str], trials: int):
        if self.coordinator is None:
            raise RuntimeError("live multi-LLM comparison requires a coordinator")
        steps, episodes = [], []
        for model in models:
            ok = self.coordinator.set_llm_backend(model)
            if not ok:
                logger.warning("backend %s unavailable - skipping", model)
                continue
            s, e = self.runner.run_live([model], trials)
            steps.extend(s)
            episodes.extend(e)
        return steps, episodes

    def _comparison_table(self, metrics: Dict[str, Dict]) -> List[Dict]:
        rows = []
        for model, res in metrics.items():
            # PRIMARY (P1-2): strict joint (None -> no windows, never fake 0).
            sj_rate = res["strict_joint_satisfaction"]["strict_joint_rate"]
            rows.append({
                "model": model,
                "strict_joint_pct": (None if sj_rate is None else 100 * sj_rate),
                # legacy per-(UE,step) AVERAGE kept as DIAGNOSTIC SECONDARY only.
                "dual_satisfaction_pct": 100 * res["dual_intent_satisfaction"]["overall"]["dual_satisfaction_rate"],
                "rollback_pct": 100 * res["rollback"]["rate"],
                "negotiation_pct": 100 * res["negotiation"]["rate"],
                "ece": res["ece"]["ece"],
                "avg_inference_ms": res["latency"]["inference_ms"],
                "theta_star": res["cost_estimate"]["theta_star"],
                "n_max": res["cost_estimate"]["n_max"],
                "assumption_ok": res["cost_estimate"].get("assumption_ok", True),
                "is_headline": model == HEADLINE_BACKEND,
            })
        return rows

    def _render_table(self, table: List[Dict]) -> str:
        lines = ["Multi-LLM comparison (paper Table III):", ""]
        # PRIMARY column is strict-sat% (P1-2); dual-sat% kept as diagnostic.
        hdr = (f"{'model':18s} {'strict-sat%':>11s} {'dual-sat%':>9s} "
               f"{'rollback%':>10s} {'nego%':>6s} {'ECE':>6s} "
               f"{'infer(ms)':>10s} {'theta*':>7s}")
        lines.append(hdr)
        lines.append("-" * len(hdr))
        daggered = []
        for r in table:
            star = " *" if r["is_headline"] else ""
            dagger = "" if r.get("assumption_ok", True) else "†"
            if dagger:
                daggered.append(r)
            sj = r.get("strict_joint_pct")
            sj_str = "n/a" if sj is None else f"{sj:.1f}"
            # P1-3 (blocker 7): an uninstrumented / NaN / Inf / negative inference
            # latency -> n/a, never crash or print a fake/negative number.
            inf = r.get("avg_inference_ms")
            inf_str = f"{inf:.0f}" if _is_real_number(inf) and inf >= 0 else "n/a"
            lines.append(f"{r['model']:18s} {sj_str:>11s} "
                         f"{r['dual_satisfaction_pct']:9.1f} "
                         f"{r['rollback_pct']:10.1f} {r['negotiation_pct']:6.1f} "
                         f"{r['ece']:6.3f} {inf_str:>10s} "
                         f"{r['theta_star']:7.3f}{dagger}{star}")
        lines.append("")
        lines.append("* = headline / verification backend (claude-sonnet, required)")
        if daggered:
            clamped_txt = ", ".join(
                f"{r['model']}={self._clamped_theta(r['theta_star']):.3f}"
                for r in daggered)
            lines.append("† premise C_worst > C_nego violated; the live "
                         f"calibrator clamps to [0.1, 0.9]: {clamped_txt}")
        return "\n".join(lines)

    @staticmethod
    def _clamped_theta(theta) -> float:
        if isinstance(theta, float) and theta != theta:   # NaN
            return 0.1
        return max(0.1, min(0.9, float(theta)))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _make_live_coordinator(llm_backend: str, theta_mode: str = "adaptive"):
    """Construct + start a live IntentCoordinator (Phase-2 testbed runs)."""
    from config import get_config
    from coordinator.intent_coordinator import IntentCoordinator
    cfg = get_config()
    # Fail-closed at the live experiment entry ONLY when no UE is provisioned
    # (guessed placeholders are not acceptable). Unprovisioned UEs are expansion
    # slots, excluded (not fatal). coordinator.start() re-checks and trims to the
    # active set; this surfaces a zero-provisioned misconfig with a clear
    # live-path message before any hardware thread launches.
    cfg.validate_live_topology()
    coordinator = IntentCoordinator(config=cfg)
    if not coordinator.set_llm_backend(llm_backend):
        logger.warning("LLM backend %s unavailable; using coordinator default",
                       llm_backend)
    if theta_mode == "fixed":
        # fixed-vs-adaptive ablation: keep the initial theta*/N_max untouched
        coordinator.calibrator.mode = "fixed"
    coordinator.start()
    return coordinator


def build_arg_parser() -> argparse.ArgumentParser:
    """The runner CLI parser (separate from main() so it is testable without
    executing a run)."""
    ap = argparse.ArgumentParser(description="5-phase intent-coordination experiment")
    ap.add_argument("--mode", choices=["synthetic", "emulated", "live"],
                    default="synthetic",
                    help="synthetic=offline harness validation (Phase 1); "
                         "emulated=real decision pipeline over an emulated "
                         "RAN + mock LLM; live=real testbed (Phase 2)")
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--methods", nargs="*", default=None,
                    help=f"default: {DEFAULT_METHODS}")
    ap.add_argument("--compare-llms", action="store_true",
                    help="run the multi-LLM comparison instead of the method sweep")
    ap.add_argument("--models", nargs="*", default=None,
                    help="LLM backends for --compare-llms")
    ap.add_argument("--llm", default=HEADLINE_BACKEND,
                    help="headline decision LLM (verification run must be claude-sonnet)")
    ap.add_argument("--output", default="experiment_results")
    ap.add_argument("--no-figures", action="store_true")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--theta", choices=["adaptive", "fixed"], default="adaptive",
                    help="fixed keeps the initial theta*/N_max (ablation)")
    ap.add_argument("--carry-calibration", action="store_true",
                    help="carry calibration state across (method, trial) "
                         "runs instead of resetting (explicit opt-in)")
    ap.add_argument("--history-policy", choices=["online", "frozen"],
                    default="online",
                    help="Batch E: with-history treatment. online (default) "
                         "learns finalized outcomes from the shared starting "
                         "snapshot; frozen reads a controlled immutable "
                         "snapshot and REQUIRES a non-empty pre-collected one")
    ap.add_argument("--history-reset-scope",
                    choices=["run", "trial", "method", "model"],
                    default="trial",
                    help="Batch E: how often the history reservoir resets to "
                         "the controlled starting snapshot")
    # Batch F (P0-14): explicit throughput-probe configuration.
    ap.add_argument("--offered-load-mbps", type=float, default=12.0,
                    help="Batch F: iperf UDP offered load in Mbps (validated; "
                         "no longer a hidden 12M constant)")
    ap.add_argument("--probe-protocol", choices=["udp"], default="udp",
                    help="Batch F: throughput probe protocol (only udp is "
                         "implemented; unsupported values are rejected)")
    ap.add_argument("--probe-direction", choices=["downlink"],
                    default="downlink",
                    help="Batch F: probe direction (only downlink is "
                         "implemented; unsupported values are rejected)")
    ap.add_argument("--probe-duration-s", type=float, default=2.0,
                    help="Batch F: probe duration in seconds")
    ap.add_argument("--probe-mode", choices=["goodput", "capacity"],
                    default="goodput",
                    help="Batch F: goodput (rate at offered load) vs capacity "
                         "(saturating); capacity requires a saturating load")
    ap.add_argument("--capacity-floor-mbps", type=float, default=100.0,
                    help="Batch F: min offered load for a valid CAPACITY probe")
    ap.add_argument("--skip-unwired", action="store_true",
                    help="skip baseline methods not wired to the live/"
                         "emulated coordinator (now the DEFAULT in those "
                         "modes; kept for backward compatibility)")
    ap.add_argument("--include-unwired", action="store_true",
                    help="live/emulated: run the unwired baselines anyway - "
                         "their records reflect the coordinator's DEFAULT "
                         "behavior, NOT the named baseline, and must not be "
                         "used for baseline comparison claims (Gate B [A1])")
    ap.add_argument("--column", choices=["double", "single"], default="double",
                    help="figure width preset: single = IEEE single-column "
                         "(3.5 in) variants")
    # §1-4 (P0-19): phase structure + live exogenous environment driver.
    ap.add_argument("--phase-profile",
                    choices=["legacy-5phase", "sec14-load"],
                    default="legacy-5phase",
                    help="legacy-5phase = §V-B channel-gain sweep (default, "
                         "emulated only); sec14-load = §1-4 load-driven phases "
                         "(channel gains 0, offered load swept) - the honest "
                         "live structure (docs/paper_alignment_sec1_4.md)")
    # I2 target: live uses the MEASURED hardware value, offline keeps 8.0.
    ap.add_argument("--i2-target-mbps", type=float, default=None,
                    help="override the I2 per-UE DL throughput target (Mbps). "
                         "Default: config.LIVE_I2_TARGET_MBPS in --mode live "
                         "(measured on THIS hardware - see "
                         "docs/ue_stability_and_capacity.md); the 8.0 Mbps "
                         "paper-model default in synthetic/emulated. Re-measure "
                         "and pass the new value after a UE/radio swap")
    ap.add_argument("--env-driver", choices=["none", "offered-load"],
                    default="none",
                    help="live exogenous environment driver. offered-load "
                         "injects an OfferedLoadEnvironmentDriver approved ONLY "
                         "by the operator env var AIC_ENV_DRIVER_APPROVED=1; "
                         "none (default) leaves it unset (live run fails closed)")
    return ap


def main():
    args = build_arg_parser().parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.mode == "live" and args.llm != HEADLINE_BACKEND and not args.compare_llms:
        logger.warning("HEADLINE verification run should use %s (got --llm %s)",
                       HEADLINE_BACKEND, args.llm)

    # LIVE runs get the measured hardware I2 target; synthetic/emulated get
    # None and therefore the unchanged 8.0 paper default (see
    # _resolve_intent_cfg). Resolved BEFORE any coordinator is built so an
    # invalid --i2-target-mbps fails fast, with zero hardware contact.
    intent_cfg = _resolve_intent_cfg(args)

    if args.compare_llms:
        # Live/emulated comparison needs the same coordinator injection as
        # the non-compare branch (MultiLLMComparison._run_live is guarded).
        coordinator = None
        channel = None
        if args.mode == "live":
            coordinator = _make_live_coordinator(args.llm, args.theta)
        elif args.mode == "emulated":
            builder = importlib.import_module(
                "experiments.emulation").build_emulated_coordinator
            coordinator, channel = builder(
                seed=args.seed, theta_mode=args.theta)
        try:
            cmp = MultiLLMComparison(coordinator=coordinator,
                                     output_dir=args.output,
                                     cfg=intent_cfg)
            cmp.runner.carry_calibration = args.carry_calibration
            cmp.runner.history_policy = args.history_policy
            cmp.runner.history_reset_scope = args.history_reset_scope
            _apply_probe_args(cmp.runner, args)
            _apply_env_args(cmp.runner, args, coordinator)
            cmp.runner.channel_model = channel
            # Gate B [A1]: unwired baselines are skipped BY DEFAULT in
            # live/emulated runs (their records are not the named
            # baseline); --include-unwired is the explicit opt-in
            cmp.runner.skip_unwired = (
                args.skip_unwired
                or (args.mode in ("live", "emulated")
                    and not args.include_unwired))
            cmp.runner.theta_mode = args.theta
            cmp.runner.figures_column = args.column
            res = cmp.run(models=args.models, trials=args.trials, mode=args.mode,
                          make_figures=not args.no_figures,
                          meta=cmp.runner._run_meta(args.mode, args.seed))
            print("\n" + res["report"])
            print(f"\nSaved: {res['path']}")
        finally:
            if coordinator is not None:
                coordinator.stop()
    else:
        # Phase-2 live integration: construct + inject a live IntentCoordinator
        # (executor + collector + LLM) so run_live has a real testbed to drive.
        coordinator = None
        channel = None
        if args.mode == "live":
            coordinator = _make_live_coordinator(args.llm, args.theta)
        elif args.mode == "emulated":
            builder = importlib.import_module(
                "experiments.emulation").build_emulated_coordinator
            # note: no coordinator.start() - determinism (see emulation.py)
            coordinator, channel = builder(
                seed=args.seed, theta_mode=args.theta)
        try:
            runner = ExperimentRunner(
                coordinator=coordinator, output_dir=args.output,
                cfg=intent_cfg,
                kpi_interval_s=(0.0 if args.mode == "emulated" else 15.0),
                c_episode=(500.0 * 0.5 / 15.0 if args.mode == "emulated"
                           else 500.0))
            runner.carry_calibration = args.carry_calibration
            runner.history_policy = args.history_policy
            runner.history_reset_scope = args.history_reset_scope
            _apply_probe_args(runner, args)
            _apply_env_args(runner, args, coordinator)
            runner.channel_model = channel
            # Gate B [A1]: unwired baselines are skipped BY DEFAULT in
            # live/emulated runs (their records are not the named
            # baseline); --include-unwired is the explicit opt-in
            runner.skip_unwired = (
                args.skip_unwired
                or (args.mode in ("live", "emulated")
                    and not args.include_unwired))
            runner.theta_mode = args.theta
            runner.figures_column = args.column
            res = runner.run(mode=args.mode, methods=args.methods, trials=args.trials,
                             make_figures=not args.no_figures, seed=args.seed,
                             headline_method=DEFAULT_HEADLINE_METHOD)
            print("\n" + res["report"])
            print(f"\n{res['n_steps']} step records, {res['n_episodes']} episodes")
        finally:
            if coordinator is not None:
                coordinator.stop()


if __name__ == "__main__":
    main()
