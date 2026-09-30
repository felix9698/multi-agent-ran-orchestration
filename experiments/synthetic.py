#!/usr/bin/env python3
"""
Synthetic / mock record generator for OFFLINE experiment-harness validation.

Phase 1 has NO live RAN (USRP powered down). This module fabricates realistic
StepRecord / EpisodeRecord streams for the 5-phase scenario so that:

  * the metric pipeline (experiments/metrics.py) can be unit-tested end-to-end
    with no hardware, LLM, or network;
  * the figures (experiments/figures.py) and the runner's offline path can be
    exercised and eyeballed before Phase 2;
  * the multi-LLM comparison can be demonstrated offline.

It is deterministic given a seed (seeded via a STRING passed to random.Random,
which CPython hashes reproducibly across processes - unlike builtin hash()).
The numbers are plausible, NOT measured - Phase 2 replaces this generator with
real records from the live coordinator. The pre-action throughput baselines per
phase (12 / 5.5 / 3.7 Mbps) match the paper's Fig. throughput description.

Modelling contract (so the derived metrics come out sane and self-consistent):
  * I1 (|BS2 offset| <= 3 dB) is almost always respected - the enforcement layer
    keeps actions bounded - so dual-intent satisfaction is gated mainly by I2
    (throughput), differentiated by each method's `effectiveness`.
  * A SUCCESSFUL trial lifts throughput toward a ceiling; a FAILED trial harms
    it (throughput crashes to a low floor) and then rolls back -> negotiation.
    This makes C_cont (failed-trial degradation) exceed C_nego (negotiation
    deficit), yielding theta* in (0,1) as the paper's cost structure requires.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from config import ActionSpaceConfig, CalibrationConfig, REFERENCE_CEILING_MBPS
from experiments.metrics import (
    StepRecord, EpisodeRecord, ClipEvent, IntentConfig,
)

TAU_TRIAL_S = 15.0        # trial observation window (paper)
# P0-12: the ONE cold-start source. synthetic no longer hardcodes theta*=0.40 -
# it derives the SAME initial θ* the coordinator/emulated modes use from
# CalibrationConfig, so every mode shares one calibration source.
SYNTHETIC_COLD_START_THETA = CalibrationConfig().compute_initial_theta()
# physical action-space bound U the enforcement layer clips to - coupled to
# the coordinator's configured bound instead of a hardcoded copy (P10)
EXEC_BOUND_DB = ActionSpaceConfig().power_offset_max_db
NEGO_ROUND_S = 5.0        # per-round negotiation latency (LLM + consumer reply)
RECOVER_CEILING_MBPS = REFERENCE_CEILING_MBPS   # single source of truth (config)

# Pre-action (baseline) DL throughput per phase, Mbps (paper Fig. description).
PHASE_BASELINE_MBPS = {
    "Nominal": 12.0,
    "Degradation": 5.5,
    "Impairment": 3.7,
    "Recovery": 10.0,
}

# Emulated serving/neighbor channel gains per phase (paper Table; dB).
DEFAULT_PHASES = [
    {"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0},
    {"name": "Degradation", "serving_gain": -2, "neighbor_gain": 1},
    {"name": "Impairment", "serving_gain": -5, "neighbor_gain": 2},
    {"name": "Recovery", "serving_gain": 0, "neighbor_gain": 0},
    {"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0},
]


@dataclass
class MethodProfile:
    """Behavioural knobs that make the five methods differ believably.

    `effectiveness` in [0,1] doubles as (a) the fraction of the throughput gap a
    successful action recovers and (b) the per-trial success probability.
    `calibration_bias` shifts reported confidence vs. true success prob
    (positive = overconfident). `nego_prop` = extra chance a trial-eligible
    decision is instead routed to negotiation (conservatism). `overshoot_prop` =
    chance the method proposes an out-of-bound (|BS2|>3 dB) action (=> I1 risk +
    clip). `hard_prop` = chance a failed trial triggers a hard failure.
    `inference_ms` = decision latency.
    """
    effectiveness: float
    calibration_bias: float
    nego_prop: float
    overshoot_prop: float
    hard_prop: float
    inference_ms: float
    schema_reject_prop: float = 0.0


# Baselines matching the paper's qualitative story: LLM+History best-calibrated
# and most effective; rule-based worst; RL/score in between.
METHOD_PROFILES = {
    "rule_based":       MethodProfile(0.45, 0.22, 0.10, 0.22, 0.020, 5.0),
    "score_heuristic":  MethodProfile(0.62, 0.15, 0.16, 0.16, 0.012, 8.0),
    "rl_controller":    MethodProfile(0.70, 0.12, 0.14, 0.10, 0.010, 12.0),
    "llm_no_history":   MethodProfile(0.83, 0.14, 0.20, 0.07, 0.006, 1400.0, 0.03),
    "llm_with_history": MethodProfile(0.93, 0.04, 0.15, 0.04, 0.003, 1550.0, 0.01),
}

# Per-model profiles for the multi-LLM comparison (all run "llm_with_history"
# logic but differ in effectiveness / calibration / latency). claude-sonnet is
# the required headline backend and the reference profile here.
MODEL_PROFILES = {
    "claude-sonnet":    MethodProfile(0.93, 0.04, 0.15, 0.04, 0.003, 1550.0, 0.01),
    "gpt-4o":           MethodProfile(0.91, 0.06, 0.16, 0.05, 0.004, 1250.0, 0.01),
    "gpt-4o-mini":      MethodProfile(0.84, 0.10, 0.19, 0.07, 0.005, 780.0, 0.02),
    "local:gpt-oss":    MethodProfile(0.82, 0.12, 0.20, 0.08, 0.006, 2600.0, 0.04),
    "llama-3":          MethodProfile(0.76, 0.16, 0.23, 0.10, 0.008, 2200.0, 0.05),
    "phi-3":            MethodProfile(0.70, 0.20, 0.26, 0.12, 0.010, 900.0, 0.07),
}


# P0-20: fixed_threshold / adaptive share the llm_with_history PROPOSER profile
# (they differ ONLY in the calibrator theta mode, which the synthetic harness
# does not model). This is an EXPLICIT documented alias, NEVER a silent fallback
# for an unknown/typo method.
_SYNTHETIC_PROFILE_ALIAS = {
    "fixed_threshold": "llm_with_history",
    "adaptive": "llm_with_history",
}


def profile_for_method(method: str) -> "MethodProfile":
    """The synthetic MethodProfile for a comparison method (P0-20). Rejects an
    UNKNOWN method (no silent llm_with_history fallback); maps the theta-ablation
    pair (fixed_threshold/adaptive) to the llm_with_history proposer profile."""
    key = _SYNTHETIC_PROFILE_ALIAS.get(method, method)
    if key not in METHOD_PROFILES:
        raise ValueError(
            f"no synthetic profile for method {method!r} - refusing to "
            f"substitute (known: {sorted(METHOD_PROFILES)})")
    return METHOD_PROFILES[key]


def _phase_seq(phases: Optional[List[Dict]]) -> List[Dict]:
    return phases if phases else DEFAULT_PHASES


def _rng(seed, method, tid) -> random.Random:
    # STRING seed => reproducible across processes (builtin hash() is not).
    return random.Random(f"{seed}:{method}:{tid}")


def generate_run(method: str, trial_id: int, profile: MethodProfile,
                 cfg: IntentConfig, phases: List[Dict],
                 steps_per_phase: int, rng: random.Random,
                 ue_ids: Tuple[str, ...] = ("ue1", "ue2"),
                 ) -> Tuple[List[StepRecord], List[EpisodeRecord]]:
    """Generate one (method, trial) run: StepRecords + coordination EpisodeRecords."""
    steps: List[StepRecord] = []
    episodes: List[EpisodeRecord] = []
    step_idx = 0
    target = cfg.throughput_target_mbps

    for phase_idx, ph in enumerate(phases):
        name = ph["name"]
        base = PHASE_BASELINE_MBPS.get(name, 8.0)
        need_action = base < target  # degraded phase -> coordinator must act

        # --- coordination action + episode (one per degraded phase) ----------
        overshoot = need_action and (rng.random() < profile.overshoot_prop)
        if need_action:
            # in-bound action stays within +/-3 dB (I1); effective methods use
            # more of the budget; overshoot cases exceed it (I1 violation + clip)
            bs2_offset = round(min(3.0, 1.4 + profile.effectiveness * 1.6), 2)
            if overshoot:
                bs2_offset = round(3.0 + rng.uniform(1.0, 4.0), 2)
            ep = _make_episode(method, trial_id, name, profile, cfg, base,
                               bs2_offset, overshoot, rng)
            # Batch G (P1-6): the phase index disambiguates repeated phase names
            # (the first vs last Nominal) in the raw record's unique phase label.
            ep.phase_idx = phase_idx
            episodes.append(ep)
            # throughput the method actually sustains this phase (post-action):
            if ep.success:
                sustained = base + profile.effectiveness * (RECOVER_CEILING_MBPS - base)
            else:
                # rolled back to baseline (no improvement) after a failed trial
                sustained = base
            # the offset actually left in effect on BS2 after the episode
            applied_offset = bs2_offset if ep.success else 0.0
        else:
            bs2_offset = round(rng.uniform(-0.5, 0.5), 2)
            applied_offset = bs2_offset
            sustained = base

        # --- per-timestep KPI samples ---------------------------------------
        for _s in range(steps_per_phase):
            for i, ue in enumerate(ue_ids):
                jitter = rng.gauss(0, 0.7)
                # UE2 (served by BS2) tracks the neighbor action slightly more
                t = sustained + (0.3 if ue == "ue1" else -0.3) + jitter
                t = max(0.3, t)
                rsrp = max(-100.0, min(-70.0, -88.0 + rng.uniform(-6, 6)))
                sinr = 6.0 + (t - 4.0) * 1.2 + rng.gauss(0, 1.5)
                kpis = {"throughput_mbps": round(t, 2),
                        "rsrp": round(rsrp, 1), "sinr": round(sinr, 1)}
                # store per-UE under the same step (one StepRecord per timestep)
                if i == 0:
                    cur = StepRecord(
                        trial_id=trial_id, method=method, phase=name,
                        step_idx=step_idx, t_s=step_idx * TAU_TRIAL_S,
                        # P1-5: keep the first vs last Nominal DISTINCT, using the
                        # repo-canonical identity p{idx}:{name} (same form as
                        # environment.py / paired_runner.py / runner).
                        phase_idx=phase_idx,
                        phase_label=f"p{phase_idx}:{name}",
                        ue_kpis={ue: kpis},
                        action_offsets={"bs1": 0.0, "bs2": round(applied_offset, 2)},
                    )
                else:
                    cur.ue_kpis[ue] = kpis
            steps.append(cur)
            step_idx += 1

    return steps, episodes


def _make_episode(method: str, trial_id: int, phase: str, profile: MethodProfile,
                  cfg: IntentConfig, base: float, bs2_offset: float,
                  overshoot: bool, rng: random.Random) -> EpisodeRecord:
    target = cfg.throughput_target_mbps
    p_success_true = min(0.98, max(0.05, profile.effectiveness + rng.gauss(0, 0.04)))
    confidence = min(0.99, max(0.02, p_success_true + profile.calibration_bias
                               + rng.gauss(0, 0.035)))
    schema_valid = rng.random() >= profile.schema_reject_prop
    # P0-12: config-derived cold-start θ* (the ONE source), not a hardcode.
    theta = SYNTHETIC_COLD_START_THETA

    # Blocker 3: emit DISTINCT raw vs calibrated fields and route on the
    # CALIBRATED probability. Synthetic has no per-partition reliability history,
    # so the calibrated probability is the identity of a FRESH (model, regime)
    # partition (documented) - not merely a renamed theta.
    raw_conf = round(confidence, 3)
    calibrated = raw_conf   # fresh-partition identity (no trained mapping here)
    ep = EpisodeRecord(
        trial_id=trial_id, method=method, phase=phase,
        confidence=raw_conf, theta_star=theta,
        raw_confidence=raw_conf, calibrated_probability=calibrated,
        threshold=theta, threshold_applied_to="calibrated_probability",
        model_id=method, operating_regime_id=phase, proposal_eligible=True,
        predicted_feasible=schema_valid and calibrated >= 0.5,
        schema_valid=schema_valid, tau_trial=TAU_TRIAL_S,
        throughput_before=round(base, 2),
        kpi_ms=round(rng.uniform(120, 260), 1),
        prompt_ms=round(rng.uniform(1, 6), 1),
        inference_ms=round(profile.inference_ms * rng.uniform(0.85, 1.15), 1),
        action_ms=round(rng.uniform(180, 380), 1),
    )

    # S2 routing on the CALIBRATED probability (blocker 3): negotiate if
    # schema-invalid, below theta*, or a conservatism draw.
    route_nego = (not schema_valid) or (calibrated < theta) \
        or (rng.random() < profile.nego_prop)

    if route_nego:
        ep.routed_to = "negotiation"
        ep.entered_negotiation = True
        ep.negotiation_rounds = rng.randint(1, 2)
        ep.nego_duration = ep.negotiation_rounds * NEGO_ROUND_S
        ep.throughput_during_nego = round(base + rng.uniform(-0.3, 0.3), 2)
        ep.success = False
        return ep

    # --- trial executed (S3) --------------------------------------------------
    ep.routed_to = "trial"
    ep.trial_executed = True
    ep.proposal_executed = True   # blocker 4: coverage = executed / eligible

    if abs(bs2_offset) > EXEC_BOUND_DB:
        applied = round(max(-EXEC_BOUND_DB, min(EXEC_BOUND_DB, bs2_offset)), 2)
        ep.clips.append(ClipEvent("bs2_power", bs2_offset, applied))
    else:
        applied = round(bs2_offset, 2)
    # the action ACTUALLY applied on this (simulated) S3 write - so the raw
    # evidence export can build a non-empty action stream for the actuation
    # (P0-20). Synthetic has no device readback; that is left None honestly.
    ep.applied_action = {"bs2": {"power_offset": applied}}

    i1_ok = abs(bs2_offset) <= cfg.power_bound_db + 1e-9   # S4 checks the +/-3 intent
    trial_success = (rng.random() < p_success_true) and i1_ok

    if trial_success:
        ep.success = True
        ep.rolled_back = False
        gain = profile.effectiveness * (RECOVER_CEILING_MBPS - base)
        ep.throughput_after = round(base + max(0.0, gain + rng.gauss(0, 0.4)), 2)
        ep.throughput_trial_min = round(base - rng.uniform(0.0, 0.5), 2)
    else:
        # failed trial: harmful action crashes throughput toward a low floor,
        # then deterministic rollback -> negotiation (Section IV-B).
        ep.success = False
        ep.rolled_back = True
        ep.throughput_trial_min = round(rng.uniform(0.4, 2.2), 2)
        ep.throughput_after = round(base + rng.uniform(-0.2, 0.2), 2)  # restored
        ep.entered_negotiation = True
        ep.negotiation_rounds = rng.randint(1, 2)
        ep.nego_duration = ep.negotiation_rounds * NEGO_ROUND_S
        ep.throughput_during_nego = round(base + rng.uniform(-0.3, 0.3), 2)
        if rng.random() < profile.hard_prop:
            ep.hard_failure = True
            ep.reconnection_time = round(rng.uniform(1.0, 3.0), 2)

    return ep


def _primary_ue_ids() -> Tuple[str, ...]:
    """The synthetic PRIMARY UE set, DERIVED FROM CONFIGURATION and restricted to
    the CURRENTLY PROVISIONED real UEs, so the default synthetic experiment
    reflects what actually EXISTS (today: ue1, ue2) rather than a fixed 3-UE
    assumption. A logical-but-unprovisioned UE (ue3 before its host/IMSI is
    supplied) is EXCLUDED from the default and propagates automatically once
    provisioned - the same fail-closed intent as the live/emulated defaults
    (docs/ue_count_variability.md). UE count is variable: callers wanting a
    specific scenario (legacy 2-UE, or a 3-UE what-if) pass ue_ids EXPLICITLY.
    Falls back to the full config UE set, then the legacy pair, if config is
    unavailable or nothing is flagged provisioned."""
    try:
        from config import get_config
        ues = get_config().network.ues
        provisioned = []
        for ue_id, ue in ues.items():
            check = getattr(ue, "is_physically_provisioned", None)
            if check is None or (callable(check) and check()):
                provisioned.append(ue_id)
        if provisioned:
            return tuple(provisioned)
        return tuple(ues.keys()) or ("ue1", "ue2")
    except Exception:
        return ("ue1", "ue2")


def generate_experiment(methods: List[str], trials: int = 5,
                        phases: Optional[List[Dict]] = None,
                        steps_per_phase: int = 4,
                        cfg: Optional[IntentConfig] = None,
                        seed: int = 12345,
                        ue_ids: Optional[Tuple[str, ...]] = None
                        ) -> Tuple[List[StepRecord], List[EpisodeRecord]]:
    """Generate a full synthetic experiment across methods & trials.

    `ue_ids` DEFAULTS to the config-derived PROVISIONED UE set (currently the
    2-UE ue1/ue2 real set; UE count is variable and ue3+ join once provisioned).
    Callers wanting a specific scenario pass ue_ids EXPLICITLY (e.g. a 3-UE
    what-if with ("ue1","ue2","ue3")).
    Returns (steps, episodes) flat lists ready for metrics.compute_multi_method.
    """
    cfg = cfg or IntentConfig()
    ue_ids = ue_ids or _primary_ue_ids()
    phases = _phase_seq(phases)
    all_steps: List[StepRecord] = []
    all_eps: List[EpisodeRecord] = []
    for method in methods:
        profile = profile_for_method(method)   # reject unknown; no silent sub
        for tid in range(1, trials + 1):
            s, e = generate_run(method, tid, profile, cfg, phases,
                                steps_per_phase, _rng(seed, method, tid),
                                ue_ids=ue_ids)
            all_steps.extend(s)
            all_eps.extend(e)
    return all_steps, all_eps


def generate_model_comparison(models: List[str], trials: int = 5,
                              phases: Optional[List[Dict]] = None,
                              steps_per_phase: int = 4,
                              cfg: Optional[IntentConfig] = None,
                              seed: int = 999,
                              ue_ids: Optional[Tuple[str, ...]] = None
                              ) -> Tuple[List[StepRecord], List[EpisodeRecord]]:
    """Generate synthetic records for the multi-LLM comparison.

    Each model is emitted as its own "method" label (== model name) so the
    metric pipeline separates them. claude-sonnet is included as the headline.
    `ue_ids` DEFAULTS to the config-derived PROVISIONED UE set (currently 2-UE
    ue1/ue2; variable, ue3+ join once provisioned). Pass ue_ids for a scenario.
    """
    cfg = cfg or IntentConfig()
    ue_ids = ue_ids or _primary_ue_ids()
    phases = _phase_seq(phases)
    all_steps: List[StepRecord] = []
    all_eps: List[EpisodeRecord] = []
    for model in models:
        profile = MODEL_PROFILES.get(model, MODEL_PROFILES["claude-sonnet"])
        for tid in range(1, trials + 1):
            s, e = generate_run(model, tid, profile, cfg, phases,
                                steps_per_phase, _rng(seed, model, tid),
                                ue_ids=ue_ids)
            all_steps.extend(s)
            all_eps.extend(e)
    return all_steps, all_eps


if __name__ == "__main__":
    from experiments.metrics import compute_multi_method
    steps, eps = generate_experiment(list(METHOD_PROFILES.keys()))
    print(f"generated {len(steps)} step records, {len(eps)} episodes")
    m = compute_multi_method(steps, eps)
    for method, res in m.items():
        # PRIMARY (P1-2) strict joint; dual-sat average is diagnostic secondary.
        sj = res["strict_joint_satisfaction"]["strict_joint_rate"]
        sj_str = "n/a" if sj is None else f"{sj:5.1%}"
        ds = res["dual_intent_satisfaction"]["overall"]["dual_satisfaction_rate"]
        rb = res["rollback"]["rate"]
        ng = res["negotiation"]["rate"]
        ece = res["ece"]["ece"]
        cost = res["cost_estimate"]
        print(f"  {method:18s} strict-sat={sj_str} (diag dual-sat={ds:5.1%}) "
              f"rollback={rb:5.1%} nego={ng:5.1%} "
              f"ECE={ece:.3f} theta*={cost['theta_star']:.3f} N_max={cost['n_max']}")
