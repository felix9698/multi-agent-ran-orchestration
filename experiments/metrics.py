#!/usr/bin/env python3
"""
Paper-grade metric computation for the 5-phase intent-coordination experiment.

This module is the SINGLE SOURCE OF TRUTH for every metric reported in the
journal paper ("Model-Agnostic Intent Coordination for Agentic RAN",
Section V-D). It is intentionally PURE and dependency-light (Python stdlib
only - no numpy/scipy/matplotlib) so the whole metric pipeline can be unit
tested OFFLINE with synthetic/mock records, with NO live RAN.

Design contract (Phase 1 offline / Phase 2 live):
  - The live experiment driver (experiments/runner.py) produces two flat lists
    of records: StepRecord (one per 15 s KPI timestep) and EpisodeRecord (one
    per S0-S6 coordination decision). It does NOTHING else metric-wise.
  - compute_all_metrics(steps, episodes, ...) turns those records into the full
    paper metric set. It never touches hardware, an LLM, or the network.
  - Therefore Phase 2 only has to "plug in" real records; the numbers below are
    computed identically whether the records came from live iperf3 or from
    experiments/synthetic.py.

Paper metric checklist (Section V-D), all implemented here:
  1. Dual-intent satisfaction rate (primary; per-phase, per-UE, overall, 95% CI)
  2. Per-UE KPI statistics (mean/std of RSRP, SINR, throughput; per phase)
  3. Rollback rate           (S3->S4 trials ending in rollback)
  4. Negotiation rate        (decisions routed to S5 instead of S3)
  5. Consecutive rollback distribution (run-length histogram; Section IV-B)
  6. Expected Calibration Error (ECE, 10 equal-width bins)
  7. Executor enforcement statistics (schema-reject %, clip %, mean/max clip)
  8. Hard failure count      (RRC drops / paging / UE disconnects)
  9. Cost parameter estimates -> theta* (Eq. 8) / N_max (Eq. 9), DERIVED from
     measured costs (NOT hardcoded)
 10. 95% confidence intervals (Student-t over the independent trials)

theta* / N_max are re-derived from the SAME closed-form equations proven in the
paper (blind re-derivation lives in docs/experiment_plan.md and is checked by
tests/test_metrics.py::TestCostDerivation):
     Eq. 8:  theta* = (C_worst - C_nego) / (R_success + C_worst)
     Eq. 9:  N_max  = floor( C_episode / (C_worst + C_nego) )
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple, Iterable

# SINGLE SOURCE OF TRUTH for the finite achievable throughput ceiling (U^ref).
# config is stdlib-only (no numeric libs), so this respects the metrics-layer
# purity rule while giving P1-4 one authoritative reference value.
from config import REFERENCE_CEILING_MBPS, LIVE_I2_TARGET_MBPS


def _is_real_number(x) -> bool:
    """True ONLY for a REAL, FINITE number that is NOT a bool. A bool must never
    be read as a measurement (True would coerce to 1.0), and None / NaN / inf are
    unverifiable. Used at the intent-evaluator entrance and the metric aggregates
    so a bool/non-finite value fails closed instead of silently satisfying."""
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x))


def _nonneg_real(x) -> bool:
    """A real, finite, NON-NEGATIVE, non-bool number. A measured 0.0 is valid;
    bool / NaN / Inf / negative is UNKNOWN. Shared by the latency (P1-3) and the
    regret (P1-4) metrics so an unknown operand is excluded, never a fake zero."""
    return _is_real_number(x) and x >= 0.0


def _valid_latency_ms(x) -> bool:
    """A VALID measured latency (ms): see _nonneg_real. A bool / NaN / Inf /
    negative value is UNKNOWN and is neither averaged nor treated as a
    'populated' segment for id linkage."""
    return _nonneg_real(x)


# ---------------------------------------------------------------------------
# Intent definitions (Section II-A, Section V-B)
# ---------------------------------------------------------------------------

@dataclass
class IntentConfig:
    """The dual-intent definition used to evaluate satisfaction.

    I1 (power constraint):  |power offset on `power_bs`| <= power_bound_db
        -> g1(u) = |dP_bs2| - 3   (paper: "BS2 power offset within +/-3 dB")
    I2 (throughput goal):   throughput(ue) >= throughput_target_mbps  for
        every UE in `throughput_ue_ids` (empty = all UEs present in the step).
        -> g2(y) = throughput_target_mbps - throughput(y)

    The 8.0 Mbps default is the OFFLINE (synthetic / emulated) paper model's
    target, calibrated against config.REFERENCE_CEILING_MBPS = 10.5 and shared
    with experiments.synthetic's profiles - do NOT change it. A LIVE run builds
    its config through `live_intent_config()` instead, which substitutes the
    MEASURED hardware target config.LIVE_I2_TARGET_MBPS (8.0 is unreachable on
    the real 24-PRB cell; docs/ue_stability_and_capacity.md).
    """
    power_bs: str = "bs2"                 # which BS the +/-3 dB constraint guards
    power_bound_db: float = 3.0
    # OFFLINE default only - see the class docstring and live_intent_config().
    throughput_target_mbps: float = 8.0
    throughput_ue_ids: Tuple[str, ...] = ()  # () => all UEs present at the step

    def i1_satisfied(self, action_offsets: Dict[str, float]) -> bool:
        """I1: coordination action's power offset on `power_bs` within bound.

        Fail-closed: a MISSING or non-finite offset for the constrained BS is
        unverifiable and counts as NOT satisfied (it used to default to
        0.0 dB, so a record-generation gap silently passed I1). A recorded
        0.0 is a valid measurement and still satisfies.
        """
        off = action_offsets.get(self.power_bs)
        # EXPLICIT reject at the evaluator entrance: a bool is NOT a measurement
        # (True would coerce to 1.0 dB and silently PASS I1), and only a real
        # finite number is verifiable. Everything else is fail-closed NOT
        # satisfied. A recorded 0.0 is a valid measurement and still satisfies.
        if not _is_real_number(off):
            return False
        return abs(float(off)) <= self.power_bound_db + 1e-9

    def i2_satisfied_ue(self, throughput_mbps: Optional[float]) -> bool:
        """I2 for a single UE. Fail-closed: None / non-finite / bool throughput
        is NOT a verifiable measurement and counts as NOT satisfied (a bool True
        must never be read as 1.0 Mbps and pass/fail by accident)."""
        if not _is_real_number(throughput_mbps):
            return False
        return float(throughput_mbps) >= self.throughput_target_mbps - 1e-9

    def i2_ue_scope(self, present_ue_ids: Iterable[str]) -> List[str]:
        """UEs that I2 must hold for.

        A CONFIGURED scope is returned unfiltered: a configured UE that is
        absent from the current record stays in scope and evaluates as NOT
        satisfied (fail-closed), instead of silently dropping out - a
        telemetry loss must never look like satisfaction.
        """
        if self.throughput_ue_ids:
            return list(self.throughput_ue_ids)
        return list(present_ue_ids)


def validated_i2_target(target_mbps) -> float:
    """A finite, POSITIVE I2 throughput target in Mbps, or ValueError.

    Fail-closed at the entrance so a bool / None / NaN / inf / <= 0 operator
    override (e.g. `--i2-target-mbps`) can never silently become an intent that
    is trivially satisfied (0.0) or never decidable (NaN). Zero is rejected as
    well: an "at least 0 Mbps" throughput goal is not an intent."""
    if not (_is_real_number(target_mbps) and float(target_mbps) > 0.0):
        raise ValueError(
            "I2 throughput target must be a finite positive number of Mbps, "
            f"got {target_mbps!r}")
    return float(target_mbps)


def live_intent_config(target_mbps: Optional[float] = None,
                       throughput_ue_ids: Iterable[str] = ()) -> IntentConfig:
    """The IntentConfig for a LIVE (real-hardware / OTA) run.

    The ONLY difference from the offline default is the I2 target: live runs use
    config.LIVE_I2_TARGET_MBPS (a MEASURED, hardware-dependent value - see
    docs/ue_stability_and_capacity.md) because the 8.0 Mbps offline default is
    ABOVE the real 24-PRB cell's single-UE ceiling (4.82 Mbps) and can therefore
    never be satisfied by any UE at any time.

    This factory is the live path's ONLY entry point for that value. Synthetic /
    emulated runs must NOT call it - they keep `IntentConfig()` with its 8.0
    default, which is calibrated against config.REFERENCE_CEILING_MBPS and is
    shared with experiments.synthetic's profiles.

    `target_mbps=None` takes the measured constant; an explicit value (the
    `--i2-target-mbps` override, for after a hardware swap) is validated and
    used instead. Everything else keeps the IntentConfig defaults."""
    target = LIVE_I2_TARGET_MBPS if target_mbps is None else target_mbps
    return IntentConfig(throughput_target_mbps=validated_i2_target(target),
                        throughput_ue_ids=tuple(throughput_ue_ids))


# ---------------------------------------------------------------------------
# Record schemas (produced by the driver, consumed by the metrics)
# ---------------------------------------------------------------------------

@dataclass
class StepRecord:
    """One 15 s KPI timestep (the paper's per-interval KPI collection).

    `ue_kpis` maps ue_id -> {"rsrp":.., "sinr":.., "throughput_mbps":..}.
    `action_offsets` maps bs_id -> coordination-action power offset (dB). This
    is the *action* the coordinator applied on top of the emulated phase
    channel, i.e. exactly what I1 constrains. (The emulated per-phase channel
    gains are the environment, not the action - see docs/experiment_plan.md.)
    """
    trial_id: int
    method: str
    phase: str
    step_idx: int                                   # global 0..(num_steps-1)
    t_s: float = 0.0                                 # elapsed seconds
    ue_kpis: Dict[str, Dict[str, Optional[float]]] = field(default_factory=dict)
    action_offsets: Dict[str, float] = field(default_factory=dict)
    # P1-5: repeated phase NAMES (the first vs the last Nominal) must keep a
    # DISTINCT identity through aggregation/plotting. phase_idx is the phase's
    # ORDINAL within the scenario; phase_label is an explicit unique label.
    # Backward-compatible defaults (None) - phase_key() falls back to the bare
    # phase name only when neither is set (legacy records).
    phase_idx: Optional[int] = None
    phase_label: Optional[str] = None

    def phase_key(self) -> str:
        """A STABLE unique phase identity for grouping/plotting, using the
        repo-CANONICAL form ``p{idx}:{name}`` (same as environment.py /
        paired_runner.py). Never blindly trusts an arbitrary phase_label:

          * phase_idx set  -> require a non-bool int >= 0; expected = p{idx}:{name}.
            If phase_label is also set it MUST equal expected (a disagreeing label
            is malformed provenance and could collapse two phases) - otherwise a
            distinct, NON-colliding ``__phase_mismatch__`` identity is returned.
          * malformed phase_idx -> a distinct ``__invalid_phase_idx__`` identity.
          * neither idx nor label (LEGACY only) -> the bare phase name.

        A repeated phase name (first vs last 'Nominal') is thus NEVER merged when
        the ordinal distinguishes them, and a malformed/mismatched identity is
        surfaced rather than silently trusted."""
        idx = self.phase_idx
        if idx is not None:
            if not (isinstance(idx, int) and not isinstance(idx, bool)
                    and idx >= 0):
                return f"__invalid_phase_idx__:{self.phase}:{idx!r}"
            expected = f"p{idx}:{self.phase}"
            if self.phase_label is not None and self.phase_label != expected:
                return f"__phase_mismatch__:{expected}:{self.phase_label!r}"
            return expected
        if self.phase_label is not None:
            # a label with NO ordinal cannot be validated against the canonical
            # p{idx}:{name} form -> INVALID, never blindly trusted.
            return f"__phase_label_without_idx__:{self.phase_label!r}"
        return self.phase                      # legacy bare fallback (both absent)

    def evaluate(self, cfg: IntentConfig) -> Dict[str, object]:
        """Return per-UE and aggregate intent satisfaction for this timestep.

        Fail-closed: a configured UE missing from this step's KPIs stays in
        scope and evaluates NOT satisfied (marked "missing"), so partial
        telemetry can never produce dual_all=True.
        """
        i1 = cfg.i1_satisfied(self.action_offsets)
        scope = cfg.i2_ue_scope(self.ue_kpis.keys())
        per_ue = {}
        for ue in scope:
            if ue not in self.ue_kpis:
                per_ue[ue] = {"i1": i1, "i2": False, "dual": False,
                              "missing": True}
                continue
            tput = self.ue_kpis.get(ue, {}).get("throughput_mbps")
            i2 = cfg.i2_satisfied_ue(tput)
            per_ue[ue] = {"i1": i1, "i2": i2, "dual": bool(i1 and i2)}
        # strict aggregate: every in-scope UE must meet I2 (and I1 global)
        i2_all = all(v["i2"] for v in per_ue.values()) if per_ue else False
        return {
            "i1": i1,
            "i2_all": i2_all,
            "dual_all": bool(i1 and i2_all),
            "per_ue": per_ue,
        }

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ClipEvent:
    axis: str            # "bs2_power" | "bs1_power" | "prb" | "sched" | "mcs" ...
    proposed: float
    applied: float

    @property
    def magnitude(self) -> float:
        return abs(self.proposed - self.applied)


@dataclass
class EpisodeRecord:
    """One S0-S6 coordination decision (an 'episode' in the state machine).

    Captures everything needed for the coordination + calibration metrics.
    Cost-trajectory fields are in Mbps (throughput) / seconds (durations); the
    cost estimator integrates them into Mbps*s.
    """
    trial_id: int
    method: str
    phase: str
    # Batch G (P1-6): the phase INDEX (so repeated phase names - e.g. the first
    # and last Nominal - are distinguishable) and the ACTUAL monotonic episode
    # timestamp. Keyword-defaulted so existing positional EpisodeRecord(trial,
    # method, phase, ...) calls are unaffected.
    phase_idx: Optional[int] = None
    episode_monotonic_s: Optional[float] = None
    # C1: index of the autonomous coordination cycle within its episode
    # (0 = the original intent; >0 = re-execution of an accepted relaxed
    # alternative). One EpisodeRecord per cycle - each cycle has its own
    # confidence and trial outcome, the statistical unit for ECE/costs.
    cycle_idx: int = 0

    # S2 analysis. ``confidence`` is the RAW-confidence legacy adapter (== the
    # raw proposer score); the Batch D schema below carries raw vs calibrated
    # explicitly, and routing is on calibrated_probability (blocker 3).
    confidence: float = 0.0                # p_t from the LLM (RAW legacy alias)
    theta_star: float = 0.0                # theta* in effect at decision time
    predicted_feasible: bool = False
    # LLM JSON parsed & had required fields: the REAL verdict (True pass / False
    # reject / None when no proposal was generated). Default True preserves the
    # synthetic/legacy shape; the live path threads the coordinator's real value.
    schema_valid: Optional[bool] = True
    # explicit source fact: whether a proposal was actually generated this cycle
    # (the model returned output). None on synthetic/pre-Batch-G records.
    proposal_generated: Optional[bool] = None
    # --- Batch D raw/calibrated schema (blocker 3) ---
    raw_confidence: Optional[float] = None
    calibrated_probability: Optional[float] = None
    threshold: Optional[float] = None
    threshold_applied_to: str = "calibrated_probability"
    model_id: str = "unknown"
    operating_regime_id: str = "default"
    proposal_eligible: bool = False
    proposal_executed: bool = False

    # routing: S3 (trial) vs S5 (negotiation)
    routed_to: str = "trial"               # "trial" | "negotiation"
    trial_executed: bool = False
    rolled_back: bool = False              # S4 rollback due to intent violation
    entered_negotiation: bool = False
    negotiation_rounds: int = 0
    # `success` is the episode's FINAL RESOLUTION, negotiation included
    # (accept / accept_modified). It is NOT the trial outcome: an episode whose
    # trial failed but whose negotiation was accepted still has success=True.
    success: bool = False
    # `trial_success` is the S4 outcome of the TRIED configuration only: did the
    # applied config pass the validation window? Independent of any later
    # negotiation. None = not recorded (pre-P2 records, or no trial executed);
    # consumers fall back to the legacy classification via trial_outcome().
    trial_success: Optional[bool] = None
    # P7 verified actuation: did the post-rollback device read-back audit
    # match the snapshot on every axis? None = no rollback / not verified.
    restore_verified: Optional[bool] = None
    hard_failure: bool = False             # RRC drop / paging fail / UE disconnect
    # Telemetry failure during the S4 window (UE attached but no usable
    # throughput): the trial fails closed and rolls back, but the window's
    # KPI trajectory is untrusted - excluded from every cost sample and
    # never a C_hard contribution (C5).
    measurement_invalid: bool = False

    # enforcement-layer clipping (action-space bound U)
    clips: List[ClipEvent] = field(default_factory=list)

    # latency breakdown (ms) - paper Table I / P1-3. Each component is
    # Optional[float] and None when NOT measured for this record: an unmeasured
    # segment is honest None, NEVER a fabricated 0.0 or a total-episode latency
    # divided across cycles. The synthetic harness sets its own measured values.
    kpi_ms: Optional[float] = None
    prompt_ms: Optional[float] = None
    inference_ms: Optional[float] = None
    action_ms: Optional[float] = None
    # P1-3 real per-segment latency (ms), each linked to THIS cycle's id chain
    # (episode_id / cycle_id / proposal_id / actuation_trial_id). None when the
    # segment did not run or was not measured (never a fabricated value).
    parse_ms: Optional[float] = None
    schema_admission_ms: Optional[float] = None
    executor_write_ms: Optional[float] = None
    readback_ms: Optional[float] = None
    validation_ms: Optional[float] = None
    negotiation_ms: Optional[float] = None
    rollback_ms: Optional[float] = None
    recovery_ms: Optional[float] = None
    # the ACTUAL measured total episode latency (ms) - the real wall-clock total,
    # NOT a sum of (possibly unknown) components. None when not measured.
    total_latency_ms: Optional[float] = None

    # cost trajectory (for Eq. 8 / Eq. 9 derivation). Optional (None) when the
    # trial window did NOT measure the value - never coerced to 0.0 (which would
    # fabricate a measured 0 Mbps). throughput_unknown flags such an episode and
    # cost/calibration exclude it; the honest UNKNOWN lives in the samples.
    throughput_before: Optional[float] = None   # Mbps, pre-trial
    throughput_after: Optional[float] = None    # Mbps, end of trial window
    throughput_trial_min: Optional[float] = None  # Mbps, worst in trial window
    tau_trial: float = 15.0                # s
    throughput_during_nego: Optional[float] = None  # Mbps, mean during nego
    # Batch F (P0-13): TRUE when the trial's throughput was UNKNOWN (a failed /
    # missing live probe). The numeric fields above are then NOT trustworthy
    # measurements (the honest UNKNOWN lives in the evidence MeasurementSamples);
    # such an episode is EXCLUDED from calibration/cost so a raw/paper value can
    # never silently claim 0 Mbps or a stale cached value.
    throughput_unknown: bool = False
    nego_throughput_unknown: bool = False
    nego_duration: float = 0.0             # s
    # s, only if hard_failure. Optional (None) when the reconnection was NOT
    # measured - never coerced to 0.0. p_hard still counts the hard event; the
    # C_hard sample only includes finite measured reconnection values.
    reconnection_time: Optional[float] = None

    # P0-11: OPTIONAL timestamped KPI trajectories for honest numerical
    # integration of the trial / negotiation cost. Each is a list of
    # (timestamp_s, throughput_mbps). When present the estimator integrates them
    # (trapezoid) instead of the tput_min*tau / last-value shortcut; when absent
    # it falls back to the legacy scalar estimate, flagged separately.
    trial_trajectory: List = field(default_factory=list)
    nego_trajectory: List = field(default_factory=list)

    # Batch F (P0-13, review #726e): the exact measurement PROVENANCE preserved
    # into the raw *_episodes.json - the PRE_ACTION baseline sample, the probe
    # configuration, and the IN_WINDOW/NEGOTIATION MeasurementSamples (incl.
    # UNKNOWN + error). Optional so pre-Batch-F records/tests are unaffected.
    pre_action_baseline: Optional[Dict] = None
    probe_config: Optional[Dict] = None
    measurement_samples: List = field(default_factory=list)
    # Batch G (P0-20 / P1-6): the action ACTUALLY applied on an S3 write, as
    # {target: {axis: value}} (e.g. {"bs2": {"power_offset": 2.4}}). Populated
    # only when a real write happened; consumed to build the action-event stream
    # in the raw evidence export (so an actuation record is never claimed with an
    # empty action stream). None when no write occurred.
    applied_action: Optional[Dict] = None
    # Batch G (P0-20/P1-6, review): the coordinator's REAL evidence identifiers
    # and applied timestamps for this cycle, threaded from result["cycles"] so the
    # raw export uses the real evidence chain instead of fabricated ids/times.
    # None on the offline synthetic harness (no coordinator evidence exists there;
    # the synthetic path synthesises deterministic ids and labels them non-OTA).
    evidence_episode_id: Optional[str] = None
    evidence_cycle_id: Optional[str] = None
    evidence_proposal_id: Optional[str] = None
    evidence_actuation_trial_id: Optional[str] = None   # only for a real S3 write
    action_apply_time: Optional[float] = None           # real applied timestamp (EPOCH)
    # Batch G (P1-6/P0-19): the DISTINCT monotonic apply timestamp captured at the
    # first apply ATTEMPT boundary (epoch action_apply_time stays epoch). The raw
    # action stream uses this domain; None when no write attempt happened.
    action_apply_monotonic_s: Optional[float] = None
    final_readback_time: Optional[float] = None
    readback_action: Optional[Dict] = None
    # Batch G (P1-6 review blocker): the REST of the coordinator's real evidence
    # provenance, threaded from result["evidence"] / result["cycles"] / the
    # reserve ledger so the raw export uses the REAL provenance instead of a
    # fabricated/derived value. None on the offline synthetic harness (no
    # coordinator evidence exists there). On a real (emulated/live) run a field
    # the coordinator did not produce is PRESERVED as None (honest unknown) -
    # never fabricated (handoff: missing measurement/provenance stays None).
    evidence_experiment_run_id: Optional[str] = None
    evidence_fsm_step_id: Optional[str] = None
    # EXPLICIT source fact: whether result["pending_intent"] was a parsed Intent
    # (a dict) vs raw text / None (a pre-parse terminal). The raw exporter uses
    # THIS - not a terminal-reason guess - to decide whether the real Intent.id is
    # required (parsed) or must be None (unparsed). None only on the synthetic
    # harness / pre-Batch-G records.
    evidence_was_parsed_intent: Optional[bool] = None
    # the parsed Intent's own id (result["pending_intent"]["id"]); None ONLY for a
    # pre-parse terminal (no Intent existed). Distinct from pending_intent_hash
    # (content provenance), which is NOT an identity.
    evidence_pending_intent_id: Optional[str] = None
    evidence_pending_intent_hash: Optional[str] = None
    evidence_proposer_id: Optional[str] = None
    evidence_model_version: Optional[str] = None
    evidence_prompt_hash: Optional[str] = None          # real hash; None if no prompt generated
    # the exact action provenance chain (P1-6): raw proposed (requested), the
    # clipped/enforced canonical, all None when no proposal/write happened.
    evidence_requested_action: Optional[Dict] = None
    evidence_clipped_action: Optional[Dict] = None
    evidence_canonical_action: Optional[Dict] = None
    evidence_canonical_action_hash: Optional[str] = None
    # the ACTUAL pending-Intent type / target / scope (from the parsed Intent's
    # to_dict()), threaded so the raw export records what the coordinator actually
    # evaluated - never the runner's cfg constants. type is the exact string;
    # target and scope are the FULL exact dicts ({kpi_name,constraint_type,
    # target_value,unit} and {ue_ids,bs_ids}), not flattened. None on a pre-parse
    # terminal / synthetic.
    evidence_intent_type: Optional[str] = None
    evidence_intent_target: Optional[Dict] = None
    evidence_intent_scope: Optional[Dict] = None
    # per-episode terminal decision (the coordinator's real TerminalOutcome /
    # TerminalReason vocabulary), threaded so the raw export records the REAL
    # terminal instead of one re-derived from cycle flags.
    terminal_outcome: Optional[str] = None
    terminal_reason: Optional[str] = None
    # reserve-ledger budget snapshot (Mbps*s): the episode cap (before), the
    # cumulative worst-case reservation, the separately-measured actual debit,
    # and the remaining budget. None when no ledger was produced.
    budget_before: Optional[float] = None
    budget_reserved: Optional[float] = None
    # the MEASURED actual debit; None (UNKNOWN) when any accepted reservation is
    # unsettled or settled UNKNOWN/PARTIAL - never a fabricated 0. The lower bound
    # + status + reservation ids live in reserve_ledger.
    budget_debited: Optional[float] = None
    budget_debited_lower_bound: Optional[float] = None
    budget_debit_status: Optional[str] = None
    budget_after: Optional[float] = None
    # the full reserve-ledger provenance (entries + settlements + reservation ids)
    # so an unknown/partial debit is auditable, not silently a scalar.
    reserve_ledger: Optional[Dict] = None

    @property
    def total_ms(self) -> Optional[float]:
        """The episode's total latency (ms). Prefers the ACTUAL measured total
        (total_latency_ms); NEVER sums unknown components. Falls back to a sum of
        the four Table-I components ONLY when they are all measured (the synthetic
        harness sets them) - if any is None the sum would be a fabricated total,
        so it returns the measured total or None instead."""
        if self.total_latency_ms is not None:
            return self.total_latency_ms
        parts = [self.kpi_ms, self.prompt_ms, self.inference_ms, self.action_ms]
        if all(p is not None for p in parts):
            return sum(parts)
        return None

    def trial_outcome(self) -> bool:
        """Trial-level success/failure classification (ECE + cost estimation).

        Uses the explicit S4 flag when recorded; falls back to the legacy
        `success and not rolled_back` for records that predate trial_success.
        """
        if self.trial_success is not None:
            return bool(self.trial_success)
        return bool(self.success and not self.rolled_back)

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["total_ms"] = self.total_ms
        return d


# ---------------------------------------------------------------------------
# Small statistics helpers (stdlib only, so tests need no numpy/scipy)
# ---------------------------------------------------------------------------

# Student-t two-sided 95% critical values, df = n-1 (n independent trials).
# For df >= 30 we fall back to the normal approx 1.960.
_T95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056,
    27: 2.052, 28: 2.048, 29: 2.045,
}


def _t95(df: int) -> float:
    if df <= 0:
        return float("nan")
    if df in _T95:
        return _T95[df]
    return 1.960


def mean_std(values: List[float]) -> Tuple[float, float]:
    """(mean, sample-std). std is 0.0 for n<2 (population fallback)."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return (float("nan"), float("nan"))
    m = statistics.fmean(vals)
    s = statistics.stdev(vals) if len(vals) >= 2 else 0.0
    return (m, s)


def confidence_interval(values: List[float], confidence: float = 0.95,
                        clip01: bool = False) -> Dict[str, float]:
    """Student-t two-sided CI of the mean over independent samples (trials).

    Returns {mean, std, n, half_width, low, high}. Only the 95% level uses the
    embedded t-table; other levels fall back to the normal 1.960 approx.

    clip01=True clips the interval to [0, 1] - use for PROPORTIONS
    (satisfaction rates etc.), where a t-interval on few trials can exceed
    the feasible range (e.g. [0.632, 1.048]).
    """
    vals = [float(v) for v in values if v is not None and not _isnan(v)]
    n = len(vals)
    if n == 0:
        return {"mean": float("nan"), "std": float("nan"), "n": 0,
                "half_width": float("nan"), "low": float("nan"), "high": float("nan")}
    m = statistics.fmean(vals)
    if n == 1:
        return {"mean": m, "std": 0.0, "n": 1, "half_width": 0.0, "low": m, "high": m}
    s = statistics.stdev(vals)
    tcrit = _t95(n - 1) if abs(confidence - 0.95) < 1e-9 else 1.960
    hw = tcrit * s / math.sqrt(n)
    low, high = m - hw, m + hw
    if clip01:
        low, high = max(0.0, low), min(1.0, high)
    return {"mean": m, "std": s, "n": n, "half_width": hw,
            "low": low, "high": high}


def _isnan(x) -> bool:
    try:
        return math.isnan(x)
    except (TypeError, ValueError):
        return False


def _frac(numer: int, denom: int) -> float:
    return (numer / denom) if denom else 0.0


# ---------------------------------------------------------------------------
# Metric 1: dual-intent satisfaction rate
# ---------------------------------------------------------------------------

def dual_intent_satisfaction(steps: List[StepRecord], cfg: IntentConfig) -> Dict:
    """DIAGNOSTIC SECONDARY (P1-2). Fraction of (UE, timestep) pairs with I1 AND
    I2 satisfied, averaged across UEs and timesteps.

    NOT the headline: a per-UE/per-step AVERAGE can hide an all-UE or single-step
    failure, so the PRIMARY metric is strict_joint_satisfaction (a window passes
    only if EVERY scoped intent holds at EVERY step). This averaged rate and its
    by_ue/by_phase breakdown are kept for diagnosis only. Also reports the strict
    all-UE variant and a 95% CI over the independent trials.
    """
    # per (UE, timestep) satisfaction, grouped for the primary "averaged across
    # UEs" number, plus strict all-UE-per-timestep for reference.
    def rate_over(records: List[StepRecord]) -> Dict:
        pair_hits = pair_total = 0
        strict_hits = strict_total = 0
        i1_hits = i1_total = 0
        for st in records:
            ev = st.evaluate(cfg)
            i1_total += 1
            i1_hits += int(ev["i1"])
            # strict counts EVERY step: a telemetry-empty step evaluates
            # dual_all=False and must stay in the denominator (fail-closed) -
            # skipping it would report strict 1.0 over [satisfied, empty]
            strict_total += 1
            strict_hits += int(ev["dual_all"])
            for _ue, v in ev["per_ue"].items():
                pair_total += 1
                pair_hits += int(v["dual"])
        return {
            "dual_satisfaction_rate": _frac(pair_hits, pair_total),   # primary
            "dual_satisfaction_strict": _frac(strict_hits, strict_total),
            "i1_satisfaction_rate": _frac(i1_hits, i1_total),
            "n_pairs": pair_total,
            "n_steps": strict_total,
        }

    overall = rate_over(steps)

    by_phase = {}
    # P1-5: group by the UNIQUE phase_key so repeated phase names stay distinct.
    for phase in _ordered_unique(s.phase_key() for s in steps):
        recs = [s for s in steps if s.phase_key() == phase]
        by_phase[phase] = rate_over(recs)

    # per-UE rates over every step where the UE is IN SCOPE (configured UEs
    # missing from a step count as failures there - fail-closed, consistent
    # with the aggregate above). With a configured scope, ONLY configured
    # UEs are reported - an out-of-scope observed UE is excluded from every
    # aggregation, by_ue included.
    by_ue = {}
    if cfg.throughput_ue_ids:
        all_ues = list(cfg.throughput_ue_ids)
    else:
        all_ues = _ordered_unique(u for s in steps for u in s.ue_kpis.keys())
    for ue in all_ues:
        hits = total = 0
        for st in steps:
            ev = st.evaluate(cfg)
            if ue in ev["per_ue"]:
                total += 1
                hits += int(ev["per_ue"][ue]["dual"])
        by_ue[ue] = {"dual_satisfaction_rate": _frac(hits, total), "n": total}

    # 95% CI over trials: one satisfaction rate per trial, then t-CI
    per_trial_rates = []
    for tid in _ordered_unique(s.trial_id for s in steps):
        recs = [s for s in steps if s.trial_id == tid]
        per_trial_rates.append(rate_over(recs)["dual_satisfaction_rate"])
    # satisfaction rates are proportions: keep the interval in [0, 1]
    ci = confidence_interval(per_trial_rates, clip01=True)

    return {
        "overall": overall,
        "by_phase": by_phase,
        "by_ue": by_ue,
        "per_trial_rates": per_trial_rates,
        "ci95": ci,
    }


# ---------------------------------------------------------------------------
# Metric 2: per-UE KPI statistics
# ---------------------------------------------------------------------------

def per_ue_kpi_statistics(steps: List[StepRecord]) -> Dict:
    """Mean +/- std of throughput + radio KPIs for each UE, per phase and
    overall. Batch F: aggregates BOTH the legacy synthetic names (rsrp/sinr,
    explicitly synthetic) AND the honest live names (gnb_ul_avg_rsrp_dbm /
    gnb_ul_snr_db / ue_dl_*); a KPI absent from a given step is simply skipped."""
    kpis = ("rsrp", "sinr", "gnb_ul_avg_rsrp_dbm", "gnb_ul_snr_db",
            "ue_dl_ss_rsrp_dbm", "ue_dl_sinr_db", "throughput_mbps")
    ues = _ordered_unique(u for s in steps for u in s.ue_kpis.keys())
    # P1-5: group by the UNIQUE phase_key so a repeated phase name (the first vs
    # last Nominal) is NEVER merged in this real consumer.
    phases = _ordered_unique(s.phase_key() for s in steps)

    out: Dict[str, Dict] = {}
    for ue in ues:
        out[ue] = {"overall": {}, "by_phase": {}}
        for kpi in kpis:
            vals = [s.ue_kpis[ue].get(kpi) for s in steps if ue in s.ue_kpis]
            m, sd = mean_std([v for v in vals if v is not None])
            out[ue]["overall"][kpi] = {"mean": m, "std": sd,
                                       "n": len([v for v in vals if v is not None])}
        for phase in phases:
            out[ue]["by_phase"][phase] = {}
            for kpi in kpis:
                vals = [s.ue_kpis[ue].get(kpi) for s in steps
                        if s.phase_key() == phase and ue in s.ue_kpis]
                m, sd = mean_std([v for v in vals if v is not None])
                out[ue]["by_phase"][phase][kpi] = {
                    "mean": m, "std": sd,
                    "n": len([v for v in vals if v is not None])}
    return out


# ---------------------------------------------------------------------------
# Metrics 3 & 4: rollback rate, negotiation rate
# ---------------------------------------------------------------------------

def rollback_rate(episodes: List[EpisodeRecord]) -> Dict:
    """Fraction of executed trials (S3->S4) that ended in rollback.

    Also counts VERIFIED rollbacks (P7): those whose post-restore device
    read-back audit matched the snapshot on every axis.
    """
    trials = [e for e in episodes if e.trial_executed]
    rolled = [e for e in trials if e.rolled_back]
    verified = [e for e in rolled if e.restore_verified is True]
    return {"rate": _frac(len(rolled), len(trials)),
            "n_rollbacks": len(rolled), "n_trials": len(trials),
            "n_verified_rollbacks": len(verified)}


def negotiation_rate(episodes: List[EpisodeRecord]) -> Dict:
    """Fraction of S2 decisions ROUTED to negotiation (S5) instead of trial (S3).

    This measures the execution/negotiation *partition* conservatism (paper
    metric 4), so it counts only the S2 routing decision - NOT trials that were
    executed (routed to S3) and subsequently fell into negotiation after a
    rollback. Those are captured by the rollback metric instead.
    """
    n = len(episodes)
    nego = [e for e in episodes if e.routed_to == "negotiation"]
    return {"rate": _frac(len(nego), n), "n_negotiations": len(nego), "n_episodes": n}


# ---------------------------------------------------------------------------
# Metric 5: consecutive rollback distribution
# ---------------------------------------------------------------------------

def consecutive_rollback_distribution(episodes: List[EpisodeRecord]) -> Dict:
    """Run-length histogram of consecutive rollbacks (Section IV-B safety check).

    Within each (method, trial) run, walk the ordered coordination decisions and
    record the length of every maximal run of consecutive rollbacks. A healthy
    system shows almost all mass at run-length 0/1 (no sustained oscillation).
    """
    hist: Dict[int, int] = {}
    max_run = 0
    for (_method, _tid), run in _group_runs(episodes):
        cur = 0
        for e in run:
            # only executed trials can roll back
            if e.trial_executed and e.rolled_back:
                cur += 1
            else:
                if cur > 0:
                    hist[cur] = hist.get(cur, 0) + 1
                    max_run = max(max_run, cur)
                cur = 0
        if cur > 0:
            hist[cur] = hist.get(cur, 0) + 1
            max_run = max(max_run, cur)
    return {"histogram": dict(sorted(hist.items())), "max_consecutive": max_run,
            "n_runs": len(list(_group_runs(episodes)))}


# ---------------------------------------------------------------------------
# Metric 6: Expected Calibration Error (ECE)
# ---------------------------------------------------------------------------

def expected_calibration_error(episodes: List[EpisodeRecord],
                               n_bins: int = 10) -> Dict:
    """ECE over executed trials: bin the probability, compare mean probability
    to accuracy.

        ECE = sum_b (|B_b| / n) * | acc(B_b) - prob(B_b) |

    Item (a): the PRIMARY probability is the CALIBRATED probability p_cal (the
    quantity routing is applied to); a record missing p_cal falls back to the
    RAW confidence, and ``confidence_source`` reports whether the run was on
    calibrated ('calibrated'), raw ('raw_legacy'), or mixed ('mixed') data - so
    an ECE computed on raw scores is never silently presented as a calibrated
    metric. The observed outcome is trial_outcome() on the executed trial.
    """
    def _p(e):
        if e.calibrated_probability is not None:
            return float(e.calibrated_probability), "calibrated"
        rc = e.raw_confidence if e.raw_confidence is not None else e.confidence
        return float(rc), "raw_legacy"
    executed = [e for e in episodes if e.trial_executed]
    obs, sources, n_invalid = [], set(), 0
    for e in executed:
        p, src = _p(e)
        # honest invalid handling: a non-finite / out-of-[0,1] probability is
        # counted separately (n_invalid) and EXCLUDED - never silently binned.
        if not math.isfinite(p) or p < 0.0 or p > 1.0:
            n_invalid += 1
            continue
        obs.append((p, e.trial_outcome()))
        sources.add(src)
    confidence_source = ("mixed" if len(sources) > 1
                         else (sources.pop() if sources else "none"))
    n = len(obs)
    bins = []
    ece = 0.0
    for b in range(n_bins):
        lo = b / n_bins
        hi = (b + 1) / n_bins
        # last bin is inclusive of 1.0
        in_bin = [(c, y) for (c, y) in obs
                  if (c >= lo and c < hi) or (b == n_bins - 1 and c == 1.0)]
        if in_bin:
            conf = statistics.fmean([c for c, _ in in_bin])
            acc = statistics.fmean([1.0 if y else 0.0 for _, y in in_bin])
            weight = len(in_bin) / n if n else 0.0
            gap = abs(acc - conf)
            ece += weight * gap
        else:
            conf = acc = weight = gap = 0.0
        bins.append({"bin": b, "low": lo, "high": hi, "count": len(in_bin),
                     "confidence": conf, "accuracy": acc, "gap": gap})
    # HONEST probability_field, derived from the ACTUAL valid included samples:
    # calibrated -> calibrated_probability; raw_legacy -> raw_confidence;
    # mixed -> mixed; none -> none (never a blanket 'calibrated_probability').
    probability_field = {"calibrated": "calibrated_probability",
                         "raw_legacy": "raw_confidence",
                         "mixed": "mixed", "none": "none"}[confidence_source]
    return {"ece": ece, "n": n, "n_bins": n_bins, "bins": bins,
            "confidence_source": confidence_source,
            "probability_field": probability_field,
            "n_invalid": n_invalid, "n_executed": len(executed)}


# ---------------------------------------------------------------------------
# Metric 7: executor enforcement statistics
# ---------------------------------------------------------------------------

# Native (upper-lower) span per canonical action axis, from the documented
# action-space ranges (config.ActionSpaceConfig defaults). Used ONLY to
# NORMALISE clip magnitudes; per-axis native magnitudes are reported separately
# and NEVER summed across axes (P1-1: no unit mixing).
_AXIS_SPAN = {"power_offset": 20.0, "prb": 100.0,
              "sched_priority": 3.75, "mcs_offset": 20.0}
_AXIS_UNIT = {"power_offset": "dB", "prb": "PRB",
              "sched_priority": "pf_weight", "mcs_offset": "mcs_index"}


def _finite_real(x) -> bool:
    """A REAL, FINITE magnitude component (P1-1 point 4). A bool is NOT a valid
    magnitude - True/False must never be silently coerced to 1.0/0.0 - and a
    None/NaN/inf value is not either. Such a clip's magnitude is EXPLICIT
    UNKNOWN: excluded from every aggregate and counted, NEVER faked to zero."""
    return _is_real_number(x)


def _clip_axis_kind(label: str) -> str:
    a = str(label)
    if "power" in a or a == "rfatt":
        return "power_offset"
    if "prb" in a:
        return "prb"
    if "sched" in a:
        return "sched_priority"
    if "mcs" in a:
        return "mcs_offset"
    # UNKNOWN axis: keep its EXACT label in its OWN bucket ("other:<label>").
    # Merging every unrecognised axis into a single "other" average would
    # re-introduce unit mixing (P1-1) between distinct unknown native units.
    return "other:" + a


def executor_enforcement_statistics(episodes: List[EpisodeRecord],
                                    axis_span: Optional[Dict] = None) -> Dict:
    """Enforcement stats WITHOUT unit-mixing (P1-1).

    (a) schema-reject fraction (over all episodes);
    (b) clip fraction whose DENOMINATOR is ACTUAL APPLIED PROPOSALS only
        (episodes that actually proposed/applied an action - a trial, a recorded
        proposal, or a clip event), NOT every schema-valid cycle;
    (c) PER-AXIS NATIVE clip magnitude (each in its own unit, never summed) and a
        unit-free NORMALISED clip = abs(raw-clipped)/(upper-lower).
    The old unit-mixed mean/max_clip_magnitude_db fields are REMOVED."""
    span = axis_span or _AXIS_SPAN
    n = len(episodes)
    schema_reject = [e for e in episodes if not e.schema_valid]
    # DENOMINATOR = actual applied proposals only (P1-1): a schema-valid episode
    # with NO action proposal (no trial, no executed proposal, no clip) is NOT in
    # the denominator.
    applied = [e for e in episodes if e.schema_valid and (
        e.trial_executed or getattr(e, "proposal_executed", False) or e.clips)]
    clipped_eps = [e for e in applied if e.clips]
    per_axis: Dict[str, List[float]] = {}
    normalized: List[float] = []
    n_unknown_magnitude = 0
    n_unnormalized_clip = 0
    for e in applied:
        for c in e.clips:
            # P1-1 point 4: a non-finite / bool clip magnitude is EXPLICIT
            # UNKNOWN - excluded from the native mean/max AND the normalized
            # aggregate (never averaged in, never faked to zero) and counted.
            if not (_finite_real(c.proposed) and _finite_real(c.applied)):
                n_unknown_magnitude += 1
                continue
            kind = _clip_axis_kind(c.axis)
            per_axis.setdefault(kind, []).append(c.magnitude)
            sp = span.get(kind)
            # Normalise ONLY with a FINITE REAL span > 0. A bool/NaN/inf/<=0 span
            # (or an unknown axis with no span) would FAKE a value - e.g.
            # magnitude/inf == 0.0 - so it stays out of the normalized aggregate
            # and is counted as EXPLICIT unnormalized (never a fake zero).
            if _finite_real(sp) and sp > 0:
                normalized.append(c.magnitude / sp)
            else:
                n_unnormalized_clip += 1
    per_axis_native = {
        k: {"mean": statistics.fmean(v), "max": max(v), "n": len(v),
            "unit": _AXIS_UNIT.get(k, "native")}
        for k, v in per_axis.items()}
    return {
        "schema_reject_fraction": _frac(len(schema_reject), n),
        "n_schema_rejects": len(schema_reject),
        # denominator is applied proposals (explicit)
        "clip_fraction": _frac(len(clipped_eps), len(applied)),
        "n_clipped_episodes": len(clipped_eps),
        "n_applied_proposals": len(applied),
        # per-axis native magnitudes (each in its OWN unit; never summed)
        "per_axis_native_clip": per_axis_native,
        # unit-free normalised violation abs(raw-clipped)/(upper-lower)
        "normalized_clip": {
            "mean": (statistics.fmean(normalized) if normalized else None),
            "max": (max(normalized) if normalized else None),
            "n": len(normalized),
            "definition": "abs(raw-clipped)/(upper-lower) per axis",
        },
        # n_clip_events is the TOTAL number of clip events (valid-magnitude ones
        # that entered the per-axis buckets PLUS the quarantined unknown ones), so
        # it reconciles with clipped_eps / n_unknown_magnitude. The finite-only
        # count is reported separately as n_valid_clip_events.
        "n_valid_clip_events": sum(len(v) for v in per_axis.values()),
        "n_clip_events": (sum(len(v) for v in per_axis.values())
                          + n_unknown_magnitude),
        # clips whose magnitude was non-finite/bool: EXPLICIT unknown, quarantined
        # from every aggregate above (never a fake zero) - P1-1 point 4.
        "n_unknown_magnitude": n_unknown_magnitude,
        # valid-magnitude clips that could NOT be normalized (missing/invalid axis
        # span): EXPLICIT unknown, kept out of normalized_clip (never a fake zero).
        "n_unnormalized_clip": n_unnormalized_clip,
        "n_episodes": n,
        "unit_mixing": "none",
    }


# ---------------------------------------------------------------------------
# Metric 8: hard failure count
# ---------------------------------------------------------------------------

def hard_failure_count(episodes: List[EpisodeRecord]) -> Dict:
    hard = [e for e in episodes if e.hard_failure]
    trials = [e for e in episodes if e.trial_executed]
    return {"count": len(hard), "n_trials": len(trials),
            "p_hard": _frac(len(hard), len(trials))}


# ---------------------------------------------------------------------------
# Metric 9: cost parameter estimates -> theta* (Eq.8) / N_max (Eq.9)
# ---------------------------------------------------------------------------

@dataclass
class CostPriors:
    """Fallback cost values used ONLY when a component has zero measured samples.

    These come from config.CalibrationConfig and let the derivation stay
    well-defined before enough live episodes exist. Any component that falls
    back is flagged `derived=False` in the output so the report is honest.
    """
    c_worst: float = 125.0
    r_success: float = 64.5
    c_nego: float = 50.0
    c_hard: float = 30.0
    p_hard: float = 0.02
    # DETERMINISTIC safety caps (P0-11): distinct from the expected means above;
    # feed the reserve-ledger upper bounds ONLY, never θ* updates.
    kpi_loss_cap_mbps: float = 10.0
    c_hard_cap: float = 30.0
    c_nego_cap: float = 50.0
    tau_trial: float = 15.0

    def c_trial_ub(self, tau_trial: Optional[float] = None) -> float:
        """Deterministic per-trial upper bound (continuous KPI-loss cap over the
        window + hard-failure cap), from caps only - never sample means."""
        tau = self.tau_trial if tau_trial is None else float(tau_trial)
        return self.kpi_loss_cap_mbps * max(0.0, tau) + self.c_hard_cap

    def c_nego_ub(self) -> float:
        """Deterministic per-negotiation upper bound."""
        return float(self.c_nego_cap)


def derive_theta_star(c_worst: float, c_nego: float, r_success: float) -> float:
    """Eq. 8:  theta* = (C_worst - C_nego) / (R_success + C_worst)."""
    denom = r_success + c_worst
    if denom <= 0:
        return float("nan")
    return (c_worst - c_nego) / denom


def derive_n_max(c_episode: float, c_trial_ub: float, c_nego_ub: float) -> int:
    """Eq. 9 (corrected, P0-10):

        N_max = max(0, floor((C_episode - C_trial_ub) / (C_trial_ub + C_nego_ub)))

    The runtime loop runs one INITIAL trial plus, for each accepted negotiation,
    one revised trial - so N negotiations cost (N+1) trials + N negotiations.
    The old ``floor(C_episode/(C_worst+C_nego))`` was an off-by-one over-count.
    If ``C_episode < C_trial_ub`` not even the initial trial is affordable -> 0
    (N_max=0 never means a free initial trial). The positional args are the
    deterministic trial / negotiation upper bounds (legacy callers pass the
    empirical C_worst / C_nego as a soft guard)."""
    if c_episode < c_trial_ub:
        return 0
    denom = c_trial_ub + c_nego_ub
    if denom <= 0:
        return 0
    return max(0, int(math.floor((c_episode - c_trial_ub) / denom)))


def trajectory_cost(samples, deficit_ref: Optional[float] = 0.0,
                    gain: bool = False) -> Optional[float]:
    """Timestamped numerical integration of a KPI trajectory (P0-11).

    ``samples`` is an iterable of (timestamp_s, throughput_mbps). Integrates the
    throughput deficit ``max(0, deficit_ref - tput)`` (or, when gain=True, the
    surplus ``max(0, tput - deficit_ref)``) over time by the trapezoid rule,
    giving an HONEST Mbps·s area instead of a ``tput_min*tau`` or last-value
    shortcut. Returns None when fewer than two timestamped samples exist (the
    caller then falls back to the legacy estimate, flagged separately)."""
    # None=unknown: an UNMEASURED reference baseline (deficit_ref is None) has no
    # honest zero-point to integrate the deficit/surplus against. Fail closed with
    # NO sample (the caller falls back to the prior) rather than fabricating a 0.0
    # reference - and never let a None reach the v-deficit_ref arithmetic below.
    if deficit_ref is None:
        return None
    pts = [(float(t), float(v)) for t, v in (samples or [])
           if t is not None and v is not None
           and math.isfinite(float(t)) and math.isfinite(float(v))]
    pts.sort(key=lambda p: p[0])
    if len(pts) < 2:
        return None

    def _d(v):
        return max(0.0, (v - deficit_ref) if gain else (deficit_ref - v))
    area = 0.0
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        dt = t1 - t0
        if dt <= 0:
            continue
        area += 0.5 * (_d(v0) + _d(v1)) * dt
    return area


def estimate_costs(episodes: List[EpisodeRecord],
                   cfg: IntentConfig,
                   c_episode: float = 500.0,
                   priors: Optional[CostPriors] = None) -> Dict:
    """Empirically estimate R_success, C_cont, C_hard, P_hard, C_worst, C_nego
    from measured episode trajectories, then derive theta* (Eq.8) & N_max (Eq.9).

    All costs are Mbps*s (throughput deficit/gain integrated over a duration):
      R_success = mean over SUCCESSFUL trials of max(0, tput_after-tput_before)*tau
      C_cont    = mean over FAILED  trials of max(0, tput_before-tput_min)*tau
                  (Prop.1: worst-case continuous degradation of a failed trial)
      P_hard    = (#hard-failure trials) / (#trials)
      C_hard    = mean over hard-failure trials of tput_before * reconnection_time
      C_worst   = C_cont + P_hard * C_hard                              (Eq. 6)
      C_nego    = mean over negotiation episodes of
                  max(0, I2_target - tput_during_nego) * nego_duration
    """
    priors = priors or CostPriors()
    trials = [e for e in episodes if e.trial_executed]
    # A measurement-invalid trial carries an UNTRUSTED KPI trajectory: it is
    # excluded from every trajectory-derived cost sample (fail-closed) and
    # counted separately (samples.n_invalid). Hard failures stay in: the
    # detach + reconnection_time observation is real regardless of what the
    # throughput probe reported.
    valid_trials = [e for e in trials if not e.measurement_invalid]
    # Classify by the TRIAL outcome (S4), not the episode resolution: a failed
    # trial resolved via negotiation must contribute a C_cont sample, not
    # pollute R_success (trial_outcome() falls back for legacy records).
    succ = [e for e in valid_trials if e.trial_outcome()]
    failed = [e for e in valid_trials if not e.trial_outcome()]
    hard = [e for e in trials if e.hard_failure]
    nego = [e for e in episodes if e.entered_negotiation]

    # P0-11: prefer TIMESTAMPED numerical integration of the trajectory; fall
    # back to the legacy scalar shortcut only when no trajectory exists, and
    # count how often each path was taken (honest separate flag).
    traj_used = {"r_success": 0, "c_cont": 0, "c_nego": 0}
    legacy_used = {"r_success": 0, "c_cont": 0, "c_nego": 0}

    def _r_success_sample(e):
        area = trajectory_cost(e.trial_trajectory, deficit_ref=e.throughput_before,
                               gain=True)
        if area is not None:
            traj_used["r_success"] += 1
            return area
        # None=unknown: throughput_before/after are now Optional (default None).
        # An unmeasured scalar baseline yields NO legacy sample (never a
        # fabricated 0 Mbps, never a None-minus-None TypeError).
        if e.throughput_after is None or e.throughput_before is None:
            return None
        legacy_used["r_success"] += 1
        return max(0.0, e.throughput_after - e.throughput_before) * e.tau_trial

    def _c_cont_sample(e):
        area = trajectory_cost(e.trial_trajectory, deficit_ref=e.throughput_before)
        if area is not None:
            traj_used["c_cont"] += 1
            return area
        # None=unknown: an unmeasured baseline/trial-min yields NO legacy sample.
        if e.throughput_before is None or e.throughput_trial_min is None:
            return None
        legacy_used["c_cont"] += 1
        return max(0.0, e.throughput_before - e.throughput_trial_min) * e.tau_trial

    def _c_nego_sample(e):
        area = trajectory_cost(e.nego_trajectory,
                               deficit_ref=cfg.throughput_target_mbps)
        if area is not None:
            traj_used["c_nego"] += 1
            return area
        legacy_used["c_nego"] += 1
        return max(0.0, cfg.throughput_target_mbps - e.throughput_during_nego) \
            * e.nego_duration

    # Batch F (P0-13): EXCLUDE episodes whose throughput was UNKNOWN from every
    # throughput-derived cost SAMPLE - an unmeasured trial never contributes a
    # (silent) 0 Mbps or a stale value. Event COUNTS (p_hard, trial count) still
    # include them (the hard-failure event happened); only the throughput cost
    # sample is dropped.
    def _tput_known(e):
        return not getattr(e, "throughput_unknown", False)
    # None=unknown: the per-sample helpers return None when the required scalar
    # throughput was never measured, so those UNKNOWN episodes drop out here
    # (never a fabricated 0 Mbps). Event COUNTS (p_hard, trial count) still
    # include them; only the throughput cost sample is dropped.
    r_success_samples = [s for s in (_r_success_sample(e) for e in succ
                                     if _tput_known(e)) if s is not None]
    c_cont_samples = [s for s in (_c_cont_sample(e) for e in failed
                                  if _tput_known(e)) if s is not None]
    # C_hard: ONLY hard trials with a FINITE MEASURED reconnection time
    # contribute a sample (an unmeasured None reconnection is not a zero-cost
    # sample); p_hard below still counts ALL hard events. n_hard_unmeasured is
    # reported so the fallback-to-prior is honest.
    hard_measured = [e for e in hard if e.reconnection_time is not None
                     and math.isfinite(float(e.reconnection_time))
                     and _tput_known(e)                  # Batch F: known baseline
                     and e.throughput_before is not None]  # None=unknown baseline
    n_hard_unmeasured = len(hard) - len(hard_measured)
    c_hard_samples = [e.throughput_before * float(e.reconnection_time)
                      for e in hard_measured]
    c_nego_samples = [_c_nego_sample(e) for e in nego
                      if not getattr(e, "nego_throughput_unknown", False)]

    r_success, r_derived = _mean_or_prior(r_success_samples, priors.r_success)
    c_cont, cont_derived = _mean_or_prior(c_cont_samples, priors.c_worst)
    c_hard, hard_derived = _mean_or_prior(c_hard_samples, priors.c_hard)
    p_hard = _frac(len(hard), len(trials)) if trials else priors.p_hard
    c_nego, nego_derived = _mean_or_prior(c_nego_samples, priors.c_nego)

    c_worst = c_cont + p_hard * c_hard

    theta = derive_theta_star(c_worst, c_nego, r_success)
    n_max = derive_n_max(c_episode, c_worst, c_nego)

    return {
        "r_success": r_success,
        "c_cont": c_cont,
        "c_hard": c_hard,
        "p_hard": p_hard,
        "c_worst": c_worst,
        "c_nego": c_nego,
        "c_episode": c_episode,
        "theta_star": theta,          # Eq. 8, DERIVED from measured costs
        "n_max": n_max,               # Eq. 9, DERIVED from measured costs
        # Prop. 2 requires C_worst > C_nego for theta* in (0,1). When too few
        # failed-trial / negotiation samples exist this can be violated (=>
        # theta* <= 0); the live calibrator additionally clamps to [0.1, 0.9].
        "assumption_ok": c_worst > c_nego,
        "samples": {
            "n_success": len(succ), "n_failed": len(failed),
            "n_hard": len(hard), "n_nego": len(nego), "n_trials": len(trials),
            "n_invalid": len(trials) - len(valid_trials),
            # hard events whose reconnection cost was UNMEASURED (excluded from
            # the C_hard mean; c_hard falls back to the prior when all missing).
            "n_hard_measured": len(hard_measured),
            "n_hard_unmeasured": n_hard_unmeasured,
        },
        "derived": {                  # False => fell back to a prior (be honest)
            "r_success": r_derived, "c_cont": cont_derived,
            "c_hard": hard_derived, "c_nego": nego_derived,
        },
        # samples exist but are ALL zero => the pipeline is likely not
        # filling the trajectory fields (silently-wrong detector, P4)
        "degenerate": {
            "r_success": _all_zero(r_success_samples),
            "c_cont": _all_zero(c_cont_samples),
            "c_hard": _all_zero(c_hard_samples),
            "c_nego": _all_zero(c_nego_samples),
        },
        # P0-11: the EXPECTED (empirical mean) block feeds θ* only - it is
        # explicitly NOT an upper bound. P_hard is per-trial unconditional.
        "expected": {
            "c_worst": c_worst, "c_nego": c_nego, "r_success": r_success,
            "p_hard": p_hard, "p_hard_definition": "per_trial_unconditional",
        },
        # blocker 6: SEPARATE confidence intervals + sample metadata for the
        # EXPECTED estimates (never placed in any *_ub field).
        "expected_ci": {
            "r_success": confidence_interval(r_success_samples),
            "c_cont": confidence_interval(c_cont_samples),
            "c_hard": confidence_interval(c_hard_samples),
            "c_nego": confidence_interval(c_nego_samples),
            "c_worst": confidence_interval(
                [c + p_hard * c_hard for c in c_cont_samples]
                if c_cont_samples else []),
            # p_hard is a PROPORTION over all trials (per-trial 0/1 indicators)
            "p_hard": confidence_interval(
                [1.0 if e.hard_failure else 0.0 for e in trials],
                clip01=True),
        },
        # P0-11: DETERMINISTIC upper bounds from declared caps (never means).
        # The reserve ledger uses THESE; n_max_deterministic is the corrected
        # Eq. 9 over the caps. A sample mean can NEVER lower these.
        "upper_bounds": {
            "c_trial_ub": priors.c_trial_ub(),
            "c_nego_ub": priors.c_nego_ub(),
            "n_max_deterministic": derive_n_max(
                c_episode, priors.c_trial_ub(), priors.c_nego_ub()),
            "source": "deterministic_caps",
        },
        # which cost samples came from timestamped integration vs the legacy
        # scalar shortcut (honest, separate).
        "trajectory": {"integrated": dict(traj_used), "legacy": dict(legacy_used)},
    }


def estimate_costs_by_phase(episodes: List[EpisodeRecord], cfg: IntentConfig,
                            c_episode: float = 500.0,
                            priors: Optional[CostPriors] = None) -> Dict:
    """Per-phase cost estimates + theta*/N_max (paper reports cost params per phase)."""
    out = {}
    for phase in _ordered_unique(e.phase for e in episodes):
        recs = [e for e in episodes if e.phase == phase]
        out[phase] = estimate_costs(recs, cfg, c_episode, priors)
    return out


def _mean_or_prior(samples: List[float], prior: float) -> Tuple[float, bool]:
    if samples:
        return (statistics.fmean(samples), True)
    return (prior, False)


def _all_zero(samples: List[float]) -> bool:
    """True when samples exist but every one is (numerically) zero."""
    return bool(samples) and all(abs(s) < 1e-12 for s in samples)


# ---------------------------------------------------------------------------
# Latency breakdown (paper Table I)
# ---------------------------------------------------------------------------

def latency_breakdown(episodes: List[EpisodeRecord]) -> Dict:
    """Mean per-component loop latency (ms): KPI, prompt, inference, action, total.

    Only MEASURED samples are averaged; a component with no measured sample
    reports 'not_instrumented' rather than a fabricated 0.0 (P1-3). A measured
    0.0 counts; a bool / NaN / Inf / negative value is rejected as unknown, so
    this scalar summary is never non-finite (Table I compatibility shape)."""
    def comp(getter):
        vals = [float(v) for v in (getter(e) for e in episodes)
                if _is_real_number(v) and v >= 0.0]
        return statistics.fmean(vals) if vals else "not_instrumented"
    return {
        "kpi_ms": comp(lambda e: e.kpi_ms),
        "prompt_ms": comp(lambda e: e.prompt_ms),
        "inference_ms": comp(lambda e: e.inference_ms),
        "action_ms": comp(lambda e: e.action_ms),
        "total_ms": comp(lambda e: e.total_ms),
        "n": len(episodes),
    }


# ---------------------------------------------------------------------------
# Top-level aggregation
# ---------------------------------------------------------------------------

def strict_joint_satisfaction(steps: List[StepRecord],
                              episodes: List[EpisodeRecord],
                              cfg: IntentConfig) -> Dict:
    """PRIMARY metric (P1-2): intent-level STRICT joint satisfaction.

    A per-(method,trial) window counts as satisfied ONLY if EVERY scoped
    monitored intent holds at EVERY observed step of the window (never a per-UE
    average that hides an all-UE or single-step failure). All no-sample outcomes
    are EXPLICIT None + status/counts (never NaN/Infinity or a fake zero) so
    json.dumps(..., allow_nan=False) succeeds.

    - Malformed windows (bool / non-int / negative / duplicate / gapped
      step_idx) are UNVERIFIABLE: included as a FAIL-CLOSED 0 and counted, so a
      malformed "all satisfied" window can never inflate the rate (blocker 6).
    - Violation AREA is reported per PHYSICAL unit, never summed: throughput
      deficit (Mbps*step) and I1 power deficit (dB*step). Missing /
      non-finite / bool scoped values are counted as unknown, NEVER zero, so the
      reported area is a measured LOWER BOUND when unknowns exist (blocker 3).
    - Time-to-recovery uses the ACTUAL elapsed t_s (seconds); a violation still
      open at the window end is CENSORED (unrecovered) and steps with unusable
      t_s are counted as unknown-timing - no fabricated finite recovery (blocker
      4). A later recovery never retroactively flips the full-window success.
    - Rollback-verified: verified == restore_verified IS True only; denominator
      is EVERY rollback attempt; false / unknown / no-sample exposed (blocker 5).

    Per-UE / averaged dual satisfaction is a DIAGNOSTIC SECONDARY only
    (dual_intent_satisfaction), never the headline."""
    keys = _ordered_unique((s.method, s.trial_id) for s in steps)
    window_ok: List[float] = []
    n_unverifiable = 0
    tput_area = 0.0
    tput_cells = 0
    tput_unknown = 0
    power_area = 0.0
    power_steps = 0
    power_unknown = 0
    recovered_elapsed: List[float] = []       # seconds, from valid t_s evidence
    n_recovered_unknown_timing = 0
    n_censored = 0                            # violation open at window end
    for key in keys:
        recs = [s for s in steps if (s.method, s.trial_id) == key]
        # BLOCKER 6: a window is VERIFIABLE only when its step indexes are unique,
        # non-negative PLAIN ints forming a CONTIGUOUS run that STARTS AT 0 (a
        # legitimate window always begins at step 0, so [3,4] means leading steps
        # are absent). No bool, non-int, negative, duplicate, gap, or non-zero
        # start. A malformed set cannot be trusted to be a genuine success, so it
        # is counted AND included as a FAIL-CLOSED 0 - NEVER excluded, which would
        # let a malformed "all satisfied" window inflate the rate to 1.0.
        # (Sorting normalises merely-reordered input.)
        idxs = [s.step_idx for s in recs]
        if (not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0
                    for v in idxs)
                or len(idxs) != len(set(idxs))
                or min(idxs) != 0
                or (max(idxs) - min(idxs) + 1) != len(idxs)):
            n_unverifiable += 1
            window_ok.append(0.0)
            continue
        srecs = sorted(recs, key=lambda s: s.step_idx)
        all_ok = True
        viol_open_t: Optional[float] = None   # t_s at the open violation start
        viol_open = False                     # a violation is currently open
        viol_open_time_known = False
        for s in srecs:
            ev = s.evaluate(cfg)
            # --- violation AREA (units kept SEPARATE; never summed) ---
            for ue in cfg.i2_ue_scope(s.ue_kpis.keys()):
                tput_cells += 1
                t = (s.ue_kpis.get(ue, {}).get("throughput_mbps")
                     if ue in s.ue_kpis else None)
                if _finite_real(t):
                    tput_area += max(0.0, cfg.throughput_target_mbps - t)
                else:
                    tput_unknown += 1          # NEVER counted as zero deficit
            power_steps += 1
            off = s.action_offsets.get(cfg.power_bs)
            if _finite_real(off):
                power_area += max(0.0, abs(off) - cfg.power_bound_db)
            else:
                power_unknown += 1
            # --- strict window verdict + recovery timing ---
            if not ev["dual_all"]:
                all_ok = False               # never reset to True (blocker 4)
                if not viol_open:
                    viol_open = True
                    if _finite_real(s.t_s):
                        viol_open_t = s.t_s
                        viol_open_time_known = True
                    else:
                        viol_open_time_known = False
            else:
                if viol_open:
                    if (viol_open_time_known and _finite_real(s.t_s)
                            and s.t_s >= viol_open_t):
                        recovered_elapsed.append(s.t_s - viol_open_t)
                    else:
                        n_recovered_unknown_timing += 1
                    viol_open = False
                    viol_open_t = None
                    viol_open_time_known = False
        if viol_open:
            n_censored += 1                   # unrecovered within the window
        window_ok.append(1.0 if all_ok else 0.0)

    # n_windows is TOTAL windows scored: verifiable ones (1.0/0.0) PLUS malformed
    # ones included as a fail-closed 0. n_verifiable_windows is the trustworthy
    # subset; windows_status flags all-invalid / partial so the headline rate is
    # never read as clean when malformed windows exist.
    n_windows = len(window_ok)
    n_verifiable = n_windows - n_unverifiable
    if n_windows == 0:
        windows_status = "no_windows"
    elif n_unverifiable == 0:
        windows_status = "all_verifiable"
    elif n_verifiable == 0:
        windows_status = "all_invalid"
    else:
        windows_status = "partial_invalid"
    if n_windows:
        # rate is over ALL windows with malformed ones fail-closed to 0.
        strict_rate: Optional[float] = statistics.fmean(window_ok)
        ci: Optional[Dict] = confidence_interval(window_ok, clip01=True)
        rate_status = "measured"
    else:
        strict_rate = None
        ci = None
        rate_status = "no_windows"

    if recovered_elapsed:
        mean_recovery = statistics.fmean(recovered_elapsed)
        median_recovery = statistics.median(recovered_elapsed)
        recovery_status = "measured"
    else:
        mean_recovery = None
        median_recovery = None
        recovery_status = "no_recovered_violations"

    # BLOCKER 4/5: the denominator is EVERY actual rollback attempt (rolled_back
    # IS True), NOT filtered by trial_executed - a partial-write rollback that
    # never completed a trial is still an attempt and must not be dropped.
    rolled = [e for e in episodes if e.rolled_back is True]
    rb_true = [e for e in rolled if e.restore_verified is True]
    rb_false = [e for e in rolled if e.restore_verified is False]
    rb_unknown = [e for e in rolled if e.restore_verified is None]
    if rolled:
        rb_rate: Optional[float] = len(rb_true) / len(rolled)
        rb_status = "measured"
    else:
        rb_rate = None
        rb_status = "no_rollbacks"

    return {
        "strict_joint_rate": strict_rate,          # None when no windows
        "rate_status": rate_status,
        "n_windows": n_windows,                    # total scored (incl. invalid)
        "n_verifiable_windows": n_verifiable,      # trustworthy subset
        "n_unverifiable_windows": n_unverifiable,
        "windows_status": windows_status,
        "ci95": ci,                                # None when no windows
        # violation area per physical unit - NEVER summed across units. Malformed
        # windows are EXCLUDED from the area sums, so their count also drives the
        # lower-bound flag/completeness (not just per-cell unknowns).
        "violation_area": {
            "throughput": {
                "value": tput_area, "unit": "Mbps*step",
                "n_cells": tput_cells, "n_unknown_cells": tput_unknown,
                "n_unverifiable_windows_excluded": n_unverifiable,
                "is_lower_bound": tput_unknown > 0 or n_unverifiable > 0},
            "power_i1": {
                "value": power_area, "unit": "dB*step",
                "n_steps": power_steps, "n_unknown_steps": power_unknown,
                "n_unverifiable_windows_excluded": n_unverifiable,
                "is_lower_bound": power_unknown > 0 or n_unverifiable > 0},
            "note": ("throughput (Mbps) and power (dB) deficits are kept "
                     "SEPARATE and never summed; each is a deficit summed over "
                     "steps (unit X*step, a per-step-weighted area, NOT integrated "
                     "over t_s); unknown cells AND all steps of unverifiable "
                     "windows are excluded from the sum and counted, so a value "
                     "with n_unknown>0 OR n_unverifiable_windows_excluded>0 is a "
                     "measured LOWER BOUND"),
        },
        "recovery": {
            "mean_time_to_recovery_s": mean_recovery,   # None when no recoveries
            "median_time_to_recovery_s": median_recovery,
            "unit": "s",
            "n_recovered": len(recovered_elapsed),
            "n_recovered_unknown_timing": n_recovered_unknown_timing,
            "n_censored_unrecovered": n_censored,
            "status": recovery_status,
            "definition": ("elapsed t_s from violation onset to the next fully "
                           "satisfied step; unrecovered violations are censored, "
                           "not a fabricated finite recovery"),
        },
        "rollback_verified": {
            "rate": rb_rate,                        # None when no rollbacks
            "status": rb_status,
            "n_rollbacks": len(rolled),
            "n_verified_true": len(rb_true),
            "n_verified_false": len(rb_false),
            "n_verified_unknown": len(rb_unknown),
            "definition": ("verified == restore_verified IS True; denominator is "
                           "EVERY rollback attempt; false/unknown counted"),
        },
        "definition": ("PRIMARY: ALL scoped monitored intents satisfied at EVERY "
                       "observed step of a VERIFIABLE (method,trial) window; the "
                       "per-UE average is diagnostic secondary only"),
    }


# Documented FINITE offline reference set (P1-4). The per-phase best ACHIEVABLE
# throughput level (Mbps) is sourced from the SINGLE canonical constant
# config.REFERENCE_CEILING_MBPS (the same value synthetic uses as
# RECOVER_CEILING_MBPS) - one source, no drift, no separate literal here. It is a
# declared finite ceiling, NOT an unknown global optimum. All phases share it.
REFERENCE_UTILITY_MBPS = {
    "Nominal": REFERENCE_CEILING_MBPS, "Degradation": REFERENCE_CEILING_MBPS,
    "Impairment": REFERENCE_CEILING_MBPS, "Recovery": REFERENCE_CEILING_MBPS,
}
# NO default/fallback: a phase absent from the set has NO declared reference
# (reference_set.no_fallback=True), so its decision sample is excluded, never
# silently mapped to a default. (There is deliberately no REFERENCE_UTILITY_DEFAULT.)
REFERENCE_UTILITY_PROVENANCE = (
    "declared per-phase best ACHIEVABLE throughput ceiling under the "
    "emulation/paper model, from the single source config.REFERENCE_CEILING_MBPS "
    "(also synthetic.RECOVER_CEILING_MBPS); a documented FINITE reference, NOT an "
    "unknown global optimum")


def _regret_reference(cfg: IntentConfig) -> Dict:
    """The exposed reference-set descriptor: values, units, provenance."""
    return {
        "kind": "finite_offline_declared",
        "utility_mbps_by_phase": dict(REFERENCE_UTILITY_MBPS),
        "no_fallback": True,      # an unknown phase has NO reference (excluded)
        "phases": sorted(REFERENCE_UTILITY_MBPS),
        "unit": ("Mbps (achievable throughput LEVEL); a regret/cost value is that "
                 "level integrated over its window -> Mbps*s"),
        "provenance": REFERENCE_UTILITY_PROVENANCE,
        "is_global_optimum": False,
    }


def regret_decomposition(episodes: List[EpisodeRecord], cfg: IntentConfig
                         ) -> Dict:
    """Regret/cost vs a DOCUMENTED FINITE reference set (P1-4), decomposed into
    FOUR SEPARATE components (each Mbps*s): proposal-DECISION regret, EXECUTION
    cost, NEGOTIATION cost, HARD-FAILURE cost. total_regret is EXACTLY their sum.

    Semantics (all measured against the declared finite reference, never a global
    optimum):
      * decision_regret - over EVERY verifiable proposal decision (an executed
        trial, success OR failure, AND a no-trial negotiation decision): the
        reference-vs-achieved shortfall (ref_mbps - achieved_mbps) over the
        DECISION HORIZON (tau). Achieved = throughput_after for a trial, and the
        BASELINE throughput_before (the config left in place by a no-write / no-
        trial decision) for a no-trial decision - a DISTINCT operand+window from
        the negotiation transient, so one shortfall is never double-counted.
      * execution_cost - a FAILED executed trial's TRANSIENT in-window dip
        (before - trial_min) x tau.
      * negotiation_cost - shortfall vs the INTENT TARGET over the ACTUAL
        negotiation window (target - during_nego) x nego_duration.
      * hard_failure_cost - throughput lost over the reconnection (before x
        reconnection_time).

    Unknown / non-finite / bool / NEGATIVE operands (or *_unknown flags) EXCLUDE
    that sample as n_unknown; a MALFORMED non-bool event/outcome flag
    (trial_executed / entered_negotiation / trial_success / ...) is NEVER
    bool()-coerced - it is excluded as n_invalid. Neither is a fabricated zero;
    a measured 0.0 is valid. An episode whose PHASE is not in the declared finite
    reference set has NO fallback reference - its decision sample is excluded
    (n_unknown), preserving the membership boundary. Output is finite JSON
    (json.dumps(allow_nan=False) safe). Reserve/budget accounting does NOT bound
    physical loss - these are measured physical shortfalls only."""
    def ref_mbps(e):
        # NO silent fallback: an unknown phase has NO declared reference (None),
        # so its decision sample is excluded, not computed against a default.
        return REFERENCE_UTILITY_MBPS.get(e.phase)

    def _exact_bool(x):
        """x if it is a GENUINE bool, else None (malformed / not a bool)."""
        return x if isinstance(x, bool) else None

    def _trial_failed(e):
        """Trial FAILED verdict by EXACT bool (never trial_outcome()'s bool()
        coercion): True/False, or None when the outcome flags are malformed."""
        ts = e.trial_success
        if isinstance(ts, bool):
            return not ts
        if ts is None:                          # legacy success/rolled_back path
            s, rb = e.success, e.rolled_back
            if isinstance(s, bool) and isinstance(rb, bool):
                return not (s and not rb)
            return None
        return None                             # malformed non-bool trial_success

    def _unknown_flag(flag):
        """Classify a *_unknown provenance flag by EXACT bool: 'measure' (genuine
        False -> operands may be measured), 'unknown' (genuine True -> honest
        exclusion), or 'invalid' (a MALFORMED non-bool, e.g. the string 'false' -
        NEVER coerced; excluded as n_invalid / lower bound)."""
        if flag is False:
            return "measure"
        if flag is True:
            return "unknown"
        return "invalid"

    dec = exe = neg = hard = 0.0
    dec_app = dec_n = dec_unk = dec_inv = 0
    exe_app = exe_n = exe_unk = exe_inv = 0
    neg_app = neg_n = neg_unk = neg_inv = 0
    hard_app = hard_n = hard_unk = hard_inv = 0
    for e in episodes:
        r = ref_mbps(e)                          # None if unknown phase
        te = _exact_bool(e.trial_executed)       # None if malformed non-bool
        en = _exact_bool(e.entered_negotiation)
        hfl = _exact_bool(e.hard_failure)
        routed_nego = (e.routed_to == "negotiation")
        # --- proposal-DECISION regret: DECISION RESULT vs the finite reference
        #     over the DECISION HORIZON (tau). Trial result = throughput_after; a
        #     no-trial decision leaves the BASELINE throughput_before (no write).
        #     Distinct operand+window from the negotiation transient (no double
        #     count). Malformed trial/nego flags -> n_invalid (never coerced).
        if te is None or en is None:
            dec_inv += 1
        else:
            is_trial = te
            is_nego_decision = en or routed_nego
            if is_trial or is_nego_decision:
                dec_app += 1
                achieved = e.throughput_after if is_trial else e.throughput_before
                fl = _unknown_flag(e.throughput_unknown)
                if fl == "invalid":
                    dec_inv += 1               # malformed provenance flag
                elif fl == "unknown":
                    dec_unk += 1               # honestly unknown
                elif (r is not None and _nonneg_real(achieved)
                        and _nonneg_real(e.tau_trial)):
                    dec += max(0.0, r - achieved) * e.tau_trial
                    dec_n += 1
                else:
                    dec_unk += 1
        # --- EXECUTION cost: a FAILED executed trial's TRANSIENT in-window dip.
        #     Outcome judged by EXACT bool (never trial_outcome()'s coercion). ---
        if te is None:
            exe_inv += 1
        elif te is True:
            failed = _trial_failed(e)
            mi = _exact_bool(e.measurement_invalid)
            if failed is None or mi is None:
                exe_inv += 1
            elif failed and not mi:
                exe_app += 1
                fl = _unknown_flag(e.throughput_unknown)
                if fl == "invalid":
                    exe_inv += 1
                elif fl == "unknown":
                    exe_unk += 1
                elif (_nonneg_real(e.throughput_before)
                        and _nonneg_real(e.throughput_trial_min)
                        and _nonneg_real(e.tau_trial)):
                    exe += max(0.0, e.throughput_before
                               - e.throughput_trial_min) * e.tau_trial
                    exe_n += 1
                else:
                    exe_unk += 1
        # --- NEGOTIATION cost: target shortfall over the ACTUAL negotiation
        #     window (distinct window/reference from decision regret) ---
        if en is None:
            neg_inv += 1
        elif en is True:
            neg_app += 1
            fl = _unknown_flag(e.nego_throughput_unknown)
            if fl == "invalid":
                neg_inv += 1
            elif fl == "unknown":
                neg_unk += 1
            elif (_nonneg_real(e.throughput_during_nego)
                    and _nonneg_real(e.nego_duration)
                    and _nonneg_real(cfg.throughput_target_mbps)):
                neg += max(0.0, cfg.throughput_target_mbps
                           - e.throughput_during_nego) * e.nego_duration
                neg_n += 1
            else:
                neg_unk += 1
        # --- HARD-FAILURE cost: throughput lost over reconnection ---
        if hfl is None:
            hard_inv += 1
        elif hfl is True:
            hard_app += 1
            if (_nonneg_real(e.throughput_before)
                    and _nonneg_real(e.reconnection_time)):
                hard += e.throughput_before * float(e.reconnection_time)
                hard_n += 1
            else:
                hard_unk += 1
    total = dec + exe + neg + hard

    def _comp(value, n_app, n_cnt, n_unk, n_inv):
        # status distinguishes a TRUE additive zero (no applicable events, no
        # malformed flags) from a LOWER-BOUND zero (applicable/malformed samples
        # whose operands/flags were excluded).
        excluded = n_unk + n_inv
        if n_app == 0 and n_inv == 0:
            status = "not_applicable"      # value 0 is a genuine 0
        elif n_cnt == 0:
            status = "unknown_only"        # value is a LOWER BOUND, not a real 0
        elif excluded == 0:
            status = "measured"
        else:
            status = "partial"
        return {"value": value, "unit": "Mbps*s", "n_applicable": n_app,
                "n_counted": n_cnt, "n_unknown": n_unk, "n_invalid": n_inv,
                "is_lower_bound": excluded > 0, "status": status}

    comps = {
        "decision_regret": _comp(dec, dec_app, dec_n, dec_unk, dec_inv),
        "execution_cost": _comp(exe, exe_app, exe_n, exe_unk, exe_inv),
        "negotiation_cost": _comp(neg, neg_app, neg_n, neg_unk, neg_inv),
        "hard_failure_cost": _comp(hard, hard_app, hard_n, hard_unk, hard_inv),
    }
    any_lb = any(c["is_lower_bound"] for c in comps.values())
    return {
        "reference_set": _regret_reference(cfg),
        **comps,
        "total_regret": {
            "value": total, "unit": "Mbps*s", "is_lower_bound": any_lb,
            "completeness": {
                "n_unknown_total": dec_unk + exe_unk + neg_unk + hard_unk,
                "n_invalid_total": dec_inv + exe_inv + neg_inv + hard_inv,
                "all_components_complete": not any_lb},
            "definition": ("EXACT sum of decision_regret + execution_cost + "
                           "negotiation_cost + hard_failure_cost")},
        "no_global_optimum": True,
        "reserve_accounting_note": ("reserve/budget accounting does NOT imply a "
                                    "physical-loss upper bound; these components "
                                    "are measured physical shortfalls only"),
    }


def component_latency_breakdown(episodes: List[EpisodeRecord]) -> Dict:
    """Per-segment latency (ms) with EXPLICIT coverage/status (P1-3, blocker 6).

    For each segment: mean over MEASURED samples only, plus n_measured /
    n_unknown / n_missing and a status. A measured 0.0 IS a valid sample; a
    bool / NaN / Inf / NEGATIVE value is UNKNOWN - never averaged in and never
    relabelled 0 - and None is missing (segment did not run). `mean_ms` is None
    (never NaN) when there is no valid sample, so the output is finite JSON. A
    total is never split and relabelled as inference."""
    segments = ("parse_ms", "prompt_ms", "inference_ms", "schema_admission_ms",
                "executor_write_ms", "readback_ms", "validation_ms",
                "negotiation_ms", "rollback_ms", "recovery_ms", "kpi_ms",
                "action_ms", "total_ms")
    out: Dict[str, object] = {}
    for seg in segments:
        present = [getattr(e, seg, None) for e in episodes
                   if getattr(e, seg, None) is not None]
        # accept measured 0.0; reject bool / NaN / inf / negative as UNKNOWN.
        valid = [float(v) for v in present if _valid_latency_ms(v)]
        n_unknown = len(present) - len(valid)
        out[seg] = {
            "mean_ms": (statistics.fmean(valid) if valid else None),
            "unit": "ms",
            "n_measured": len(valid),
            "n_unknown": n_unknown,        # non-None but bool/non-finite/negative
            "n_missing": len(episodes) - len(present),   # segment did not run
            "status": ("measured" if valid
                       else ("unknown_only" if present else "not_instrumented")),
        }
    # P1-3 (blocker 5): REAL linkage coverage over the same episodes (not a
    # test-only helper) - how many populated-latency records carry the required
    # evidence ids, with example violations for audit.
    n_viol_records = 0
    n_violations = 0
    examples: List[str] = []
    for e in episodes:
        vv = latency_linkage_violations(e)
        if vv:
            n_viol_records += 1
            n_violations += len(vv)
            for x in vv:
                if len(examples) < 5:
                    examples.append(x)
    out["linkage"] = {
        "n_episodes": len(episodes),
        "n_records_with_violations": n_viol_records,
        "n_violations": n_violations,
        "examples": examples,
        "status": ("ok" if n_violations == 0 else "violations"),
    }
    out["n"] = len(episodes)
    return out


# P1-3 (blocker 5): a POPULATED segment latency must carry the real evidence ids
# that necessarily existed when that stage ran. Rules (conditional, honest):
#  (a) EVERY populated cycle segment -> real evidence_episode_id + evidence_cycle_id.
#  (b) proposal/schema-stage latency -> evidence_proposal_id ONLY when a proposal
#      was actually generated (a timeout/exception with proposal_generated=False
#      honestly has inference timing but NO proposal id).
#  (c) post-actuation stages (readback/validation) -> evidence_actuation_trial_id.
#  (d) executor-write -> evidence_actuation_trial_id ONLY when a real write
#      COMPLETED (applied_action present); a pre-snapshot failure timer may
#      honestly have actuation id None.
_CYCLE_LATENCY_SEGMENTS = ("inference_ms", "schema_admission_ms",
                           "executor_write_ms", "readback_ms", "validation_ms",
                           "rollback_ms", "recovery_ms", "negotiation_ms")
_PROPOSAL_STAGE_LATENCY = ("inference_ms", "schema_admission_ms")
_POST_ACTUATION_LATENCY = ("readback_ms", "validation_ms")


def latency_linkage_violations(ep) -> List[str]:
    """Return the latency<->evidence-id linkage violations for one EpisodeRecord
    (empty == correctly linked). See the rules above; a segment whose latency is
    None imposes no id requirement, and an honestly id-less stage (pre-parse, a
    proposal that never generated, a pre-snapshot write failure) is NOT a
    violation."""
    v: List[str] = []
    # only a VALID (finite, >=0, non-bool) latency counts as a populated segment;
    # an unknown value (bool/NaN/Inf/negative) imposes no id requirement.
    populated = [s for s in _CYCLE_LATENCY_SEGMENTS
                 if _valid_latency_ms(getattr(ep, s, None))]
    if populated:                                       # rule (a)
        if not getattr(ep, "evidence_episode_id", None):
            v.append("cycle-segment latency without evidence_episode_id")
        if not getattr(ep, "evidence_cycle_id", None):
            v.append("cycle-segment latency without evidence_cycle_id")
    if getattr(ep, "proposal_generated", None) is True:  # rule (b)
        for seg in _PROPOSAL_STAGE_LATENCY:
            if _valid_latency_ms(getattr(ep, seg, None)) \
                    and not getattr(ep, "evidence_proposal_id", None):
                v.append(f"{seg} populated without evidence_proposal_id")
    for seg in _POST_ACTUATION_LATENCY:                 # rule (c)
        if _valid_latency_ms(getattr(ep, seg, None)) \
                and not getattr(ep, "evidence_actuation_trial_id", None):
            v.append(f"{seg} populated without evidence_actuation_trial_id")
    if _valid_latency_ms(getattr(ep, "executor_write_ms", None)) \
            and getattr(ep, "applied_action", None) \
            and not getattr(ep, "evidence_actuation_trial_id", None):   # rule (d)
        v.append("executor_write_ms (applied write) without "
                 "evidence_actuation_trial_id")
    return v


def compute_all_metrics(steps: List[StepRecord], episodes: List[EpisodeRecord],
                        cfg: Optional[IntentConfig] = None,
                        c_episode: float = 500.0,
                        priors: Optional[CostPriors] = None,
                        ece_bins: int = 5) -> Dict:
    """Compute the full paper metric set for ONE method (all 10 metrics).

    Records are assumed to belong to a single method; the runner calls this once
    per method. Pure & offline: no hardware, no LLM, no network.
    ece_bins defaults to 5 here (few executed trials per method make 10 bins
    mostly empty); the report prints the sample count alongside.
    """
    cfg = cfg or IntentConfig()
    return {
        # PRIMARY (P1-2): intent-level strict joint satisfaction over the window
        "strict_joint_satisfaction": strict_joint_satisfaction(steps, episodes,
                                                               cfg),
        # regret vs a declared finite reference set (P1-4)
        "regret": regret_decomposition(episodes, cfg),
        # per-segment latency, honest about what is instrumented (P1-3)
        "component_latency": component_latency_breakdown(episodes),
        "dual_intent_satisfaction": dual_intent_satisfaction(steps, cfg),   # 1 (diagnostic)
        "per_ue_kpi": per_ue_kpi_statistics(steps),                         # 2
        "rollback": rollback_rate(episodes),                               # 3
        "negotiation": negotiation_rate(episodes),                         # 4
        "consecutive_rollback": consecutive_rollback_distribution(episodes),  # 5
        "ece": expected_calibration_error(episodes, n_bins=ece_bins),      # 6
        "enforcement": executor_enforcement_statistics(episodes),          # 7
        "hard_failures": hard_failure_count(episodes),                     # 8
        "cost_estimate": estimate_costs(episodes, cfg, c_episode, priors),  # 9
        "cost_estimate_by_phase": estimate_costs_by_phase(episodes, cfg, c_episode, priors),
        "latency": latency_breakdown(episodes),                            # Table I
        "counts": {"n_steps": len(steps), "n_episodes": len(episodes)},
    }


def compute_multi_method(steps: List[StepRecord], episodes: List[EpisodeRecord],
                         cfg: Optional[IntentConfig] = None,
                         c_episode: float = 500.0,
                         priors: Optional[CostPriors] = None,
                         ece_bins: int = 5) -> Dict[str, Dict]:
    """Split records by method and compute the full metric set for each."""
    cfg = cfg or IntentConfig()
    methods = _ordered_unique([s.method for s in steps] + [e.method for e in episodes])
    out = {}
    for method in methods:
        m_steps = [s for s in steps if s.method == method]
        m_eps = [e for e in episodes if e.method == method]
        out[method] = compute_all_metrics(m_steps, m_eps, cfg, c_episode,
                                          priors, ece_bins=ece_bins)
    return out


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _ordered_unique(it: Iterable) -> List:
    seen = set()
    out = []
    for x in it:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _group_runs(episodes: List[EpisodeRecord]):
    """Yield ((method, trial_id), [episodes in original order]) preserving order."""
    keys = _ordered_unique((e.method, e.trial_id) for e in episodes)
    for key in keys:
        yield key, [e for e in episodes if (e.method, e.trial_id) == key]


if __name__ == "__main__":
    # tiny smoke check (no hardware): derive theta*/N_max from the paper priors
    p = CostPriors()
    print("theta* (Eq.8) from priors:", round(derive_theta_star(p.c_worst, p.c_nego, p.r_success), 4))
    print("N_max  (Eq.9) from priors:", derive_n_max(500.0, p.c_worst, p.c_nego))
