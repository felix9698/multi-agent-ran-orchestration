#!/usr/bin/env python3
"""
Paper-corresponding figures for the 5-phase intent-coordination experiment.

Uses matplotlib with the non-interactive Agg backend (no display / headless
safe). Every figure is written to `output_dir` (default experiment_results/) as
both PNG and PDF. Figures are generated from the SAME StepRecord / EpisodeRecord
streams and the metrics dicts, so they work identically on synthetic (Phase 1)
and live (Phase 2) data.

Figures produced (paper correspondence):
  * throughput_timeline  -> Fig. (Section V-G): DL throughput vs. timestep with
    per-phase shading, the I2 threshold, and RED DIAMOND markers at timesteps
    where I2 is not fulfilled. One line per method.
  * metrics_table        -> Table III: per-method/-model dual-sat, rollback,
    negotiation, ECE, latency.
  * reliability_diagram  -> ECE reliability curves per model with theta* overlay
    (ICC Fig. 5 style threshold/calibration curves).
  * consecutive_rollback -> Section IV-B safety histogram.
  * per_ue_kpi           -> metric 2: per-UE RSRP/SINR/throughput per phase.

Every I2 reference line is drawn from the run's `cfg.throughput_target_mbps` -
never a hardcoded number. That is 8.0 Mbps for the offline paper model and
config.LIVE_I2_TARGET_MBPS for a live/OTA run, so a live figure can never show a
threshold the experiment did not actually use.
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib
matplotlib.use("Agg")  # headless
# IEEE/camera-ready: embed TrueType (Type 42) fonts in pdf/ps outputs
matplotlib.rcParams["pdf.fonttype"] = 42
matplotlib.rcParams["ps.fonttype"] = 42
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

from experiments.metrics import (
    StepRecord, EpisodeRecord, IntentConfig, confidence_interval,
    _is_real_number, _nonneg_real,
)

logger = logging.getLogger(__name__)

# color-blind-safe qualitative palette (Okabe-Ito subset)
_PALETTE = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00",
            "#56B4E9", "#F0E442", "#000000"]
_PHASE_FILL = {
    "Nominal": "#E8F5E9",
    "Degradation": "#FFF8E1",
    "Impairment": "#FFEBEE",
    "Recovery": "#E3F2FD",
}


SAVE_DPI = 300                # camera-ready PNG resolution
SINGLE_COLUMN_WIDTH_IN = 3.5  # IEEE single-column figure width
_COLUMN = "double"            # module-wide width preset (see set_column)


def set_column(column: str):
    """Figure width preset: 'double' keeps each plot's native size;
    'single' rescales every saved figure to the IEEE single-column width
    (3.5 in, height scaled proportionally)."""
    global _COLUMN
    _COLUMN = column if column in ("single", "double") else "double"


def _ensure(out_dir: str) -> Path:
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _save(fig, out_dir: Path, name: str) -> List[str]:
    if _COLUMN == "single":
        w, h = fig.get_size_inches()
        fig.set_size_inches(SINGLE_COLUMN_WIDTH_IN,
                            h * SINGLE_COLUMN_WIDTH_IN / max(w, 1e-6))
    paths = []
    for ext in ("png", "pdf"):
        fp = out_dir / f"{name}.{ext}"
        fig.savefig(fp, dpi=SAVE_DPI, bbox_inches="tight")
        paths.append(str(fp))
    plt.close(fig)
    return paths


def _ordered_unique(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# --------------------------------------------------------------------------- #
# P1-5 PURE aggregation helpers (inspectable metadata; NO plotting side effects)
# --------------------------------------------------------------------------- #

def _valid_prob(x) -> bool:
    """A genuine finite probability in [0, 1] (bool / NaN / out-of-range = no)."""
    return _is_real_number(x) and 0.0 <= x <= 1.0


def scoped_throughput_metadata(steps: List[StepRecord],
                               cfg: IntentConfig) -> Dict:
    """Contract 1: per-scoped-UE / per-trial throughput trajectories that NEVER
    average an I2 violation away.

    Scope = cfg.throughput_ue_ids if configured, else every OBSERVED UE. Each
    (method, phase_key, trial_id, ue) maps to a list of (step_idx, value) where
    value is the finite Mbps or None (missing/invalid/negative -> UNKNOWN, fail-
    closed, NEVER a fabricated 0). A global step is a STRICT violation when ANY
    scoped UE in ANY trial is below target (finite) OR unknown - the decision is
    per-UE/per-trial, never on a mean. Returns inspectable metadata only."""
    target = cfg.throughput_target_mbps
    # the I2 target itself must be a genuine finite non-negative number; a
    # bool/NaN/inf/negative target is INVALID -> fail closed (every scoped step
    # is a violation, since satisfaction cannot be decided).
    target_valid = _nonneg_real(target)
    methods = _ordered_unique(s.method for s in steps)
    observed = _ordered_unique(u for s in steps for u in s.ue_kpis.keys())
    scope = list(cfg.throughput_ue_ids) if cfg.throughput_ue_ids else list(observed)
    series: Dict = {}
    violation_steps: Dict = {}            # STRICT verdict = finite-below OR unknown
    finite_violation_steps: Dict = {}     # a scoped UE MEASURED below target
    unknown_steps: Dict = {}              # a scoped UE missing/invalid (fail-closed)
    coverage_by_method: Dict = {}
    n_unknown_cells = 0
    for m in methods:
        fin_v = set()
        unk = set()
        m_unknown = 0
        for s in (x for x in steps if x.method == m):
            pk = s.phase_key()
            for ue in scope:
                cell = s.ue_kpis.get(ue)
                t = cell.get("throughput_mbps") if isinstance(cell, dict) else None
                val = t if (_is_real_number(t) and t >= 0.0) else None
                series.setdefault((m, pk, s.trial_id, ue), []).append(
                    (s.step_idx, val))
                if val is None or not target_valid:
                    # unknown value OR an invalid target -> fail-closed UNKNOWN
                    # (NOT a measured value; never plotted as a data point).
                    m_unknown += 1 if val is None else 0
                    n_unknown_cells += 1 if val is None else 0
                    unk.add(s.step_idx)
                elif val < target - 1e-9:
                    fin_v.add(s.step_idx)          # a MEASURED (finite) violation
        strict = fin_v | unk
        violation_steps[m] = sorted(strict)
        finite_violation_steps[m] = sorted(fin_v)
        unknown_steps[m] = sorted(unk)
        coverage_by_method[m] = {
            "n_unknown_cells": m_unknown,
            "n_finite_violation_steps": len(fin_v),
            "n_unknown_steps": len(unk),
            "n_violation_steps": len(strict)}
    return {
        "target_mbps": target,
        "target_valid": target_valid,
        "scope": scope,
        "series": series,
        "violation_steps": violation_steps,
        "finite_violation_steps": finite_violation_steps,
        "unknown_steps": unknown_steps,
        "coverage_by_method": coverage_by_method,
        "n_unknown_cells": n_unknown_cells,
        "any_violation": any(bool(v) for v in violation_steps.values()),
        "definition": ("STRICT per-scoped-UE/per-trial: a step violates when ANY "
                       "scoped UE in ANY trial is below target OR unknown; a mean "
                       "NEVER decides a violation; missing/invalid KPI is UNKNOWN "
                       "(fail-closed), never a fabricated 0 Mbps. An INVALID target "
                       "(bool/NaN/inf/negative) fails closed: every scoped step is "
                       "a violation. Coverage is reported per method."),
    }


def reliability_metadata(episodes: List[EpisodeRecord]) -> Dict:
    """Contract 2: PER-METHOD, CYCLE-LEVEL calibration + ordered per-cycle
    threshold history.

    - Partitioned BY METHOD (methods are never mixed).
    - Ordered by a STABLE ordinal trial_id -> phase_idx -> episode_monotonic_s ->
      cycle_idx (malformed sort fields counted) - never input order.
    - A calibration sample requires a valid calibrated_probability in [0,1] AND a
      GENUINE executed trial with an EXACT bool outcome (never bool(trial_outcome)
      coercion, never raw_confidence substitution). A valid p with
      trial_executed=False is NOT invalid - it is not_executed/not-applicable;
      only a non-bool executed flag or executed=True-without-an-exact-outcome is
      invalid.
    - A threshold is used ONLY when it is a valid [0,1] value AND
      threshold_applied_to == 'calibrated_probability'; otherwise it is unknown
      (a missing/misapplied threshold is counted, never fabricated).
    Everything invalid is counted, never coerced."""
    def _eb(x):
        return x if isinstance(x, bool) else None

    def _int0(x):
        return isinstance(x, int) and not isinstance(x, bool) and x >= 0

    def _exact_outcome(e):
        """The trial-success outcome by EXACT bool, with the LEGACY fallback
        (trial_success None -> success/rolled_back must BOTH be genuine bools):
        True/False, or None when malformed."""
        ts = e.trial_success
        if isinstance(ts, bool):
            return ts
        if ts is None:
            s, rb = e.success, e.rolled_back
            if isinstance(s, bool) and isinstance(rb, bool):
                return bool(s and not rb)
        return None

    def _ord_key(e):
        # method-local STABLE ordinal: trial_id -> phase_idx -> monotonic
        # timestamp -> cycle_idx (matches the tuple returned below); a malformed
        # field sorts last (inf) so input order never decides a tie.
        pi = e.phase_idx if _int0(e.phase_idx) else math.inf
        ms = (float(e.episode_monotonic_s)
              if _is_real_number(e.episode_monotonic_s) else math.inf)
        ci = e.cycle_idx if _int0(e.cycle_idx) else math.inf
        tid = e.trial_id if _int0(e.trial_id) else math.inf
        return (tid, pi, ms, ci)

    def _malformed_ordinal(e):
        return ((e.phase_idx is not None and not _int0(e.phase_idx))
                or (e.episode_monotonic_s is not None
                    and not _is_real_number(e.episode_monotonic_s))
                or not _int0(e.cycle_idx))

    by_method: Dict = {}
    for m in _ordered_unique(e.method for e in episodes):
        meps = [e for e in episodes if e.method == m]
        ordered = sorted(meps, key=_ord_key)
        calibration = []
        cycle_history = []
        n_unknown_prob = n_unknown_threshold = 0
        n_invalid_outcome = n_not_executed = 0
        n_malformed_ordinal = sum(1 for e in meps if _malformed_ordinal(e))
        for ordinal_i, e in enumerate(ordered):
            p = e.calibrated_probability
            pv = float(p) if _valid_prob(p) else None
            executed = _eb(e.trial_executed)
            outcome = _exact_outcome(e)
            if pv is None:
                n_unknown_prob += 1
            elif executed is True and outcome is not None:
                calibration.append((pv, outcome))   # calibrated-only, exact outcome
            elif executed is False:
                n_not_executed += 1     # valid p but the trial was NOT executed (N/A)
            else:
                # executed non-bool, OR executed=True with no exact outcome -> invalid
                n_invalid_outcome += 1
            thr = e.threshold
            applies = getattr(e, "threshold_applied_to",
                              None) == "calibrated_probability"
            thrv = float(thr) if (_valid_prob(thr) and applies) else None
            if thrv is None:
                n_unknown_threshold += 1            # missing / out-of-range / misapplied
            cycle_history.append({
                "ordinal": ordinal_i,               # method-local stable position
                "trial_id": e.trial_id, "phase_idx": e.phase_idx,
                "cycle_idx": e.cycle_idx,
                "calibrated_probability": pv, "threshold": thrv})
        by_method[m] = {
            "calibration": calibration,
            "cycle_history": cycle_history,
            "distinct_thresholds": sorted({c["threshold"] for c in cycle_history
                                           if c["threshold"] is not None}),
            "n_unknown_calibrated": n_unknown_prob,
            "n_unknown_threshold": n_unknown_threshold,
            "n_invalid_outcome": n_invalid_outcome,
            "n_not_executed": n_not_executed,
            "n_malformed_ordinal": n_malformed_ordinal,
        }
    return {
        "by_method": by_method,
        "source": ("PER-METHOD cycle-level calibrated_probability + that cycle's "
                   "threshold (applied_to=calibrated_probability); stable ordinal "
                   "trial_id -> phase_idx -> episode_monotonic_s -> cycle_idx"),
        "note": ("never raw_confidence, never a single final theta, never "
                 "bool()-coerced outcome (exact bool + legacy success/rolled_back "
                 "fallback); methods never mixed"),
    }


def phase_group_metadata(steps: List[StepRecord]) -> Dict:
    """Contract 3: ordered UNIQUE phase identities (repeated names kept distinct
    via StepRecord.phase_key). Surfaces malformed / mismatched identities."""
    keys = _ordered_unique(s.phase_key() for s in steps)
    invalid = [k for k in keys if k.startswith("__invalid_phase_idx__")
               or k.startswith("__phase_mismatch__")
               or k.startswith("__phase_label_without_idx__")]
    by_name: Dict = {}
    for s in steps:
        by_name.setdefault(s.phase, set()).add(s.phase_key())
    repeated = {n: sorted(v) for n, v in by_name.items() if len(v) > 1}
    return {
        "phase_keys": keys,
        "n_phases": len(keys),
        "invalid_phase_keys": invalid,
        "repeated_names_kept_distinct": repeated,
    }


def trial_satisfaction_ci(steps: List[StepRecord], cfg: IntentConfig) -> Dict:
    """Contract 4: PER-METHOD independent-TRIAL sample size + 95% CI computed
    ACROSS TRIALS (one strict dual-satisfaction rate per trial), NEVER over UE x
    step pseudo-replicates and NEVER mixing methods (a trial_id reused across two
    methods stays two SEPARATE per-method trials)."""
    by_method: Dict = {}
    for m in _ordered_unique(s.method for s in steps):
        msteps = [s for s in steps if s.method == m]
        per_trial = []
        for tid in _ordered_unique(s.trial_id for s in msteps):
            recs = [s for s in msteps if s.trial_id == tid]
            if recs:
                ok = sum(1 for s in recs if s.evaluate(cfg)["dual_all"])
                per_trial.append(ok / len(recs))
        ci = confidence_interval(per_trial, clip01=True) if per_trial else None
        by_method[m] = {"n_trials": len(per_trial),
                        "per_trial_rates": per_trial, "ci95": ci}
    return {
        "by_method": by_method,
        "definition": ("PER-METHOD: one strict dual-satisfaction rate per "
                       "INDEPENDENT trial, then a t-CI across trials; methods are "
                       "never mixed and it is NOT UE x step pseudo-replicates"),
    }


def _mean_ue_throughput_by_step(steps: List[StepRecord]) -> Dict[int, float]:
    """CONTEXT mean throughput across UEs/trials per step. Only genuine finite
    non-negative values are averaged (bool / NaN / Inf / negative excluded), so
    the context line is never polluted by a bad value."""
    acc: Dict[int, List[float]] = {}
    for s in steps:
        vals = [k.get("throughput_mbps") for k in s.ue_kpis.values()
                if _nonneg_real(k.get("throughput_mbps"))]
        if not vals:
            continue
        acc.setdefault(s.step_idx, []).extend(vals)
    return {i: sum(v) / len(v) for i, v in sorted(acc.items())}


def _phase_spans(steps: List[StepRecord]):
    """Return [(phase_label, start_idx, end_idx_inclusive)] in step order, keyed
    by the UNIQUE phase_key so a repeated phase name (p0:Nominal vs p4:Nominal)
    yields DISTINCT spans/labels and is never visually merged."""
    ordered = sorted({(s.step_idx, s.phase_key()) for s in steps})
    spans = []
    for idx, pk in ordered:
        if spans and spans[-1][0] == pk and spans[-1][2] == idx - 1:
            spans[-1] = (pk, spans[-1][1], idx)
        else:
            spans.append((pk, idx, idx))
    return spans


def plot_throughput_timeline(steps: List[StepRecord],
                             cfg: Optional[IntentConfig] = None,
                             methods: Optional[List[str]] = None,
                             output_dir: str = "experiment_results",
                             name: str = "throughput_timeline") -> List[str]:
    """DL throughput time-series with phase shading + I2-violation red markers."""
    cfg = cfg or IntentConfig()
    out = _ensure(output_dir)
    methods = methods or _ordered_unique(s.method for s in steps)

    fig, ax = plt.subplots(figsize=(10, 4.5))

    # phase shading (use the first method's spans - identical across methods)
    ref_steps = [s for s in steps if s.method == methods[0]]
    spans = _phase_spans(ref_steps)
    for pk, a, b in spans:
        # colour by the bare phase NAME (last segment of p{idx}:{name}); the
        # LABEL shown keeps the DISTINCT phase_key (p0:Nominal vs p4:Nominal).
        bare = pk.split(":", 1)[-1] if pk.startswith("p") and ":" in pk else pk
        ax.axvspan(a - 0.5, b + 0.5, color=_PHASE_FILL.get(bare, "#F5F5F5"),
                   alpha=0.7, zorder=0)
        ax.annotate(pk, xy=((a + b) / 2, 0.96), xycoords=("data", "axes fraction"),
                    ha="center", va="top", fontsize=7, color="#555")

    # P1-5: the strict violation decision comes from PER-SCOPED-UE / PER-TRIAL
    # metadata (never a mean), and the CI is computed ACROSS TRIALS.
    target = cfg.throughput_target_mbps
    meta = scoped_throughput_metadata(steps, cfg)
    ci_meta = trial_satisfaction_ci(steps, cfg)
    # I2 threshold: draw the numeric baseline ONLY when the target is VALID; an
    # invalid (bool/NaN/inf/negative) target is NEVER drawn as a real reference
    # line (it would look like a legitimate threshold) - annotate fail-closed.
    if meta["target_valid"]:
        ax.axhline(target, color="#B00020", ls="--", lw=1.2, zorder=2,
                   label=f"I2 target ({target:g} Mbps)")
    else:
        ax.annotate("I2 target INVALID (fail-closed; no numeric baseline)",
                    xy=(0.01, 0.02), xycoords="axes fraction", fontsize=8,
                    color="#B00020")
    y0 = 0.0                       # throughput floor for unknown fail-closed marks
    for mi, method in enumerate(methods):
        m_steps = [s for s in steps if s.method == method]
        color = _PALETTE[mi % len(_PALETTE)]
        # context mean trace (clearly a mean, excludes bad values, NOT the basis)
        series = _mean_ue_throughput_by_step(m_steps)
        xs = sorted(series.keys())
        ax.plot(xs, [series[i] for i in xs], "-", lw=1.0, color=color, alpha=0.35,
                zorder=3, label=f"{method} (mean, context)")
        # RAW per-scoped-UE / per-trial trajectories (faint) - the actual scoped
        # trajectories, so an individual UE/trial violation is directly visible.
        worst: Dict[int, float] = {}
        labelled = False
        for (mm, _pk, _tid, _ue), pts in meta["series"].items():
            if mm != method:
                continue
            fx = [i for i, v in pts if v is not None]
            fy = [v for _i, v in pts if v is not None]
            if fx:
                ax.plot(fx, fy, "-", lw=0.5, color=color, alpha=0.28, zorder=2,
                        label=(f"{method} (scoped UE×trial)" if not labelled
                               else None))
                labelled = True
                for i, v in zip(fx, fy):
                    worst[i] = min(worst.get(i, v), v)
        # FINITE (measured) violations -> red diamond at the ACTUAL low value.
        fvx = meta["finite_violation_steps"].get(method, [])
        if fvx:
            ax.plot(fvx, [worst.get(i, target) for i in fvx], "D", ms=6,
                    color="#B00020", markeredgecolor="k", markeredgewidth=0.4,
                    zorder=5,
                    label="I2 violated (measured, scoped UE)" if mi == 0 else None)
        # UNKNOWN / fail-closed steps -> a DISTINCT marker at the axis BOTTOM
        # (never a diamond at target, which would look like a measured value).
        ukx = meta["unknown_steps"].get(method, [])
        if ukx:
            ax.plot(ukx, [y0] * len(ukx), "x", ms=6, color="#6A1B9A", zorder=5,
                    label="unknown KPI (fail-closed)" if mi == 0 else None)

    ax.set_xlabel("KPI timestep (15 s each)")
    ax.set_ylabel("DL throughput (Mbps)")
    ax.set_title("Downlink throughput across the 5-phase scenario")
    ax.grid(True, axis="y", ls=":", alpha=0.5)
    # PER-METHOD n + 95% CI (across INDEPENDENT trials, never UE×step) in caption
    parts = []
    for method in methods:
        bm = ci_meta["by_method"].get(method, {})
        ci = bm.get("ci95")
        if ci is not None:
            parts.append(f"{method}: n={bm['n_trials']} strict-sat CI"
                         f"[{ci['low']:.2f},{ci['high']:.2f}]")
        else:
            parts.append(f"{method}: n={bm.get('n_trials', 0)} CI n/a")
    cap = "; ".join(parts) + " (95% CI across trials, not UE×step)"
    if meta["n_unknown_cells"]:
        cap += f"; {meta['n_unknown_cells']} unknown KPI cell(s) fail-closed"
    ax.annotate(cap, xy=(0.0, -0.17), xycoords="axes fraction", fontsize=6.5,
                color="#333", annotation_clip=False)
    # legend outside (right) so it never occludes the throughput trace
    ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize=7,
              framealpha=0.95)
    fig.tight_layout()
    return _save(fig, out, name)


def plot_metrics_table(metrics_by_key: Dict[str, Dict],
                       output_dir: str = "experiment_results",
                       name: str = "metrics_table",
                       title: str = "Coordination performance summary") -> List[str]:
    """Render the per-method/-model summary table (paper Table III style)."""
    out = _ensure(output_dir)
    # PRIMARY headline (P1-2) is Strict-sat; Dual-sat kept as diagnostic column.
    cols = ["Strict-sat (%)", "Dual-sat (%)", "Rollback (%)", "Negotiation (%)",
            "ECE", "Inference (ms)", "theta*", "N_max"]
    rows, cell = [], []
    for key, res in metrics_by_key.items():
        sj = res["strict_joint_satisfaction"]["strict_joint_rate"]
        sj_str = "n/a" if sj is None else f"{sj*100:.1f}"    # never a fake 0
        ds = res["dual_intent_satisfaction"]["overall"]["dual_satisfaction_rate"]
        rb = res["rollback"]["rate"]
        ng = res["negotiation"]["rate"]
        ece = res["ece"]["ece"]
        inf = res["latency"]["inference_ms"]
        # P1-3: 'not_instrumented'/None/NaN/Inf/negative inference latency renders
        # n/a, not 0 ms (a bad measurement is never shown as a real number).
        inf_str = (f"{inf:.0f}" if isinstance(inf, (int, float))
                   and not isinstance(inf, bool) and math.isfinite(inf)
                   and inf >= 0 else "n/a")
        cost = res["cost_estimate"]
        rows.append(key)
        cell.append([sj_str, f"{ds*100:.1f}", f"{rb*100:.1f}", f"{ng*100:.1f}",
                     f"{ece:.3f}", inf_str,
                     f"{cost['theta_star']:.3f}", f"{cost['n_max']}"])

    fig, ax = plt.subplots(figsize=(max(8, 1.4 * len(cols)), 0.6 + 0.5 * len(rows)))
    ax.axis("off")
    tbl = ax.table(cellText=cell, rowLabels=rows, colLabels=cols,
                   loc="center", cellLoc="center", rowLoc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    tbl.scale(1, 1.4)
    # header styling
    for j in range(len(cols)):
        tbl[0, j].set_facecolor("#37474F")
        tbl[0, j].set_text_props(color="w", fontweight="bold")
    ax.set_title(title, pad=12, fontsize=11)
    fig.tight_layout()
    return _save(fig, out, name)


def plot_reliability_diagram(episodes: List[EpisodeRecord],
                             metrics_by_key: Dict[str, Dict],
                             theta_star: Optional[float] = None,
                             output_dir: str = "experiment_results",
                             name: str = "reliability_diagram") -> List[str]:
    """P1-5 contract 2: LEFT = per-model calibration curve; RIGHT = the ordered
    CYCLE-LEVEL history of calibrated_probability with THAT cycle's threshold. No
    single final-theta line is the evidence; ``theta_star`` is ignored (kept for
    signature compatibility)."""
    out = _ensure(output_dir)
    keys = list(metrics_by_key.keys())
    meta = reliability_metadata(episodes)

    by_method = meta["by_method"]
    fig, (axc, axh) = plt.subplots(1, 2, figsize=(11, 5))
    # -- LEFT: calibration curve from CALIBRATED-ONLY samples (never the raw/mixed
    #    ECE bins) - per method, binned into deciles. --
    axc.plot([0, 1], [0, 1], color="#888", ls="--", lw=1,
             label="perfect calibration")
    for ki, key in enumerate(by_method):
        samples = by_method[key]["calibration"]        # (calibrated_p, outcome)
        # decile bins on the calibrated probability
        buckets: Dict[int, list] = {}
        for p, ok in samples:
            buckets.setdefault(min(9, int(p * 10)), []).append((p, ok))
        xs, ys = [], []
        for b in sorted(buckets):
            ps = [p for p, _ in buckets[b]]
            os = [1.0 if ok else 0.0 for _, ok in buckets[b]]
            xs.append(sum(ps) / len(ps))
            ys.append(sum(os) / len(os))
        if xs:
            axc.plot(xs, ys, "-o", ms=4, lw=1.4,
                     color=_PALETTE[ki % len(_PALETTE)],
                     label=f"{key} (calibrated, n={len(samples)})")
    axc.set_xlim(0, 1)
    axc.set_ylim(0, 1)
    axc.set_xlabel("Calibrated probability  $p_t$")
    axc.set_ylabel("Observed success rate")
    axc.set_title("Calibration (calibrated-only)")
    axc.grid(True, ls=":", alpha=0.5)
    axc.legend(loc="upper left", fontsize=7)

    # -- RIGHT: per-method ordered cycle history (stable ordinal trial_id ->
    #    phase_idx -> episode_monotonic_s -> cycle_idx) of
    #    calibrated p + THAT cycle's threshold --
    all_thr = set()
    for ki, key in enumerate(by_method):
        hist = by_method[key]["cycle_history"]
        order = list(range(len(hist)))
        pv = [h["calibrated_probability"] for h in hist]
        thr = [h["threshold"] for h in hist]
        px = [i for i, v in zip(order, pv) if v is not None]
        py = [v for v in pv if v is not None]
        col = _PALETTE[ki % len(_PALETTE)]
        if px:
            axh.plot(px, py, "-o", ms=3, lw=1.2, color=col,
                     label=f"{key} calibrated $p_t$")
        tx = [i for i, v in zip(order, thr) if v is not None]
        ty = [v for v in thr if v is not None]
        if tx:
            axh.step(tx, ty, where="mid", lw=1.1, ls="--", color=col)
        all_thr.update(by_method[key]["distinct_thresholds"])
    for thv in sorted(all_thr):
        axh.axhline(thv, color="#B00020", ls=":", lw=0.8, alpha=0.4)
    axh.set_ylim(0, 1)
    axh.set_xlabel("Coordination cycle (ordered per method)")
    axh.set_ylabel("Probability / threshold")
    axh.set_title("Cycle-level calibrated $p_t$ vs its threshold")
    axh.grid(True, ls=":", alpha=0.5)
    axh.annotate(f"distinct thresholds: {sorted(all_thr)}", xy=(0.0, -0.16),
                 xycoords="axes fraction", fontsize=7, color="#333",
                 annotation_clip=False)
    axh.legend(loc="upper right", fontsize=7)
    fig.tight_layout()
    return _save(fig, out, name)


def plot_consecutive_rollback(metrics_by_key: Dict[str, Dict],
                              output_dir: str = "experiment_results",
                              name: str = "consecutive_rollback") -> List[str]:
    """Grouped bar chart of consecutive-rollback run-length histograms."""
    out = _ensure(output_dir)
    keys = list(metrics_by_key.keys())
    max_len = 1
    for res in metrics_by_key.values():
        h = res["consecutive_rollback"]["histogram"]
        if h:
            max_len = max(max_len, max(int(k) for k in h.keys()))
    lengths = list(range(1, max_len + 1))

    fig, ax = plt.subplots(figsize=(8, 4.5))
    w = 0.8 / max(1, len(keys))
    for ki, key in enumerate(keys):
        h = metrics_by_key[key]["consecutive_rollback"]["histogram"]
        counts = [h.get(L, h.get(str(L), 0)) for L in lengths]
        xs = [L + (ki - len(keys) / 2) * w + w / 2 for L in lengths]
        ax.bar(xs, counts, width=w, color=_PALETTE[ki % len(_PALETTE)], label=key)

    ax.set_xlabel("Consecutive-rollback run length")
    ax.set_ylabel("Occurrences")
    ax.set_title("Consecutive-rollback distribution (sustained-oscillation check)")
    ax.set_xticks(lengths)
    ax.grid(True, axis="y", ls=":", alpha=0.5)
    ax.legend(fontsize=7)
    fig.tight_layout()
    return _save(fig, out, name)


def plot_per_ue_kpi(metrics_by_key: Dict[str, Dict],
                    method_key: str,
                    cfg: Optional[IntentConfig] = None,
                    output_dir: str = "experiment_results",
                    name: str = "per_ue_kpi") -> List[str]:
    """Per-UE mean throughput (+/- std) per phase for one method/model.

    The I2 reference line comes from `cfg.throughput_target_mbps` - NEVER a
    hardcoded 8.0. A live run's target is the measured hardware value
    (config.LIVE_I2_TARGET_MBPS), and drawing 8 Mbps on a live figure would put
    a threshold in the paper that the experiment never used."""
    out = _ensure(output_dir)
    cfg = cfg or IntentConfig()
    # Fail CLEARLY (not with a bare KeyError deep in the indexing) when asked to
    # feature a method that is absent from the metrics. compute_multi_method keys
    # metrics_by_key by the methods that ACTUALLY appear in the records, so a
    # method that never ran (e.g. only a subset ran in a live single-method run)
    # is simply not a key. generate_all_figures resolves the headline against the
    # present keys before calling this, so this guard only fires on direct misuse.
    if method_key not in metrics_by_key:
        raise KeyError(
            f"plot_per_ue_kpi: method {method_key!r} is not present in metrics "
            f"(available methods: {list(metrics_by_key.keys())}); nothing to plot")
    per_ue = metrics_by_key[method_key]["per_ue_kpi"]
    ues = list(per_ue.keys())
    if not ues:
        return []
    phases = list(per_ue[ues[0]]["by_phase"].keys())

    fig, ax = plt.subplots(figsize=(9, 4.5))
    w = 0.8 / max(1, len(ues))
    x = list(range(len(phases)))
    for ui, ue in enumerate(ues):
        means = [per_ue[ue]["by_phase"][p]["throughput_mbps"]["mean"] for p in phases]
        stds = [per_ue[ue]["by_phase"][p]["throughput_mbps"]["std"] for p in phases]
        xs = [xi + (ui - len(ues) / 2) * w + w / 2 for xi in x]
        ax.bar(xs, means, width=w, yerr=stds, capsize=3,
               color=_PALETTE[ui % len(_PALETTE)], label=ue)

    # I2 threshold from the RUN's config. An invalid (bool/NaN/inf/negative)
    # target is NEVER drawn as a real reference line - annotate fail-closed,
    # exactly as plot_throughput_timeline does.
    target = cfg.throughput_target_mbps
    if _nonneg_real(target):
        ax.axhline(target, color="#B00020", ls="--", lw=1,
                   label=f"I2 target ({target:g} Mbps)")
    else:
        ax.annotate("I2 target INVALID (fail-closed; no numeric baseline)",
                    xy=(0.01, 0.02), xycoords="axes fraction", fontsize=8,
                    color="#B00020")
    ax.set_xticks(x)
    ax.set_xticklabels(phases)
    ax.set_ylabel("DL throughput (Mbps)")
    ax.set_title(f"Per-UE throughput by phase - {method_key}")
    ax.grid(True, axis="y", ls=":", alpha=0.5)
    ax.legend(fontsize=8)
    fig.tight_layout()
    return _save(fig, out, name)


def _resolve_headline_method(metrics_by_key: Dict[str, Dict],
                             requested: Optional[str]) -> Optional[str]:
    """Resolve the requested headline method against the methods ACTUALLY present
    in ``metrics_by_key``.

    ``compute_multi_method`` keys the metrics dict by the methods that appear in
    the records, so a requested headline may be ABSENT: the runner passes its
    fixed default (``DEFAULT_HEADLINE_METHOD == 'adaptive'``), but a live run in
    which only a subset of methods actually produced records (e.g. a single
    ``llm_with_history`` run) yields a dict without an ``'adaptive'`` key. Indexing
    it blindly raised ``KeyError: 'adaptive'`` and crashed figure generation after
    the experiment loop had already completed and saved its records/metrics.

    Contract: the headline is a *preference*. This returns a key that is genuinely
    present (or ``None`` when there is nothing to feature). It NEVER raises:

      * requested method present            -> use it,
      * requested method absent, keys exist -> fall back to the first present key
        with a clear WARNING naming both (the headline figures label themselves
        with the method they use, so this is an honest substitution, never a
        silently-mislabelled figure),
      * no methods present                  -> ``None`` (headline figures skipped).
    """
    keys = list(metrics_by_key.keys())
    if not keys:
        if requested is not None:
            logger.warning(
                "generate_all_figures: metrics is empty; skipping headline "
                "figures (requested headline_method=%r)", requested)
        return None
    if requested is None:
        return keys[0]
    if requested in metrics_by_key:
        return requested
    fallback = keys[0]
    logger.warning(
        "generate_all_figures: requested headline_method=%r is not present in "
        "metrics (available methods: %s); featuring %r instead", requested, keys,
        fallback)
    return fallback


def generate_all_figures(steps: List[StepRecord], episodes: List[EpisodeRecord],
                         metrics_by_key: Dict[str, Dict],
                         cfg: Optional[IntentConfig] = None,
                         theta_star: Optional[float] = None,
                         output_dir: str = "experiment_results",
                         headline_method: Optional[str] = None,
                         column: str = "double") -> Dict[str, List[str]]:
    """Generate the full figure set; returns {figure_name: [paths]}.

    column="single" saves IEEE single-column (3.5 in wide) variants."""
    set_column(column)
    cfg = cfg or IntentConfig()
    # Resolve the requested headline to a method that is ACTUALLY present so a
    # missing key (e.g. the runner default 'adaptive' when only 'llm_with_history'
    # ran) degrades to an honest fallback + warning instead of a KeyError crash.
    headline_method = _resolve_headline_method(metrics_by_key, headline_method)
    if theta_star is None and headline_method is not None:
        # headline_method is guaranteed present here; still guard the nested lookup
        # so a method whose cost estimate lacks theta* leaves it None (the
        # reliability diagram ignores theta_star) rather than raising.
        theta_star = (metrics_by_key.get(headline_method, {})
                      .get("cost_estimate", {})
                      .get("theta_star"))

    figs = {}
    figs["throughput_timeline"] = plot_throughput_timeline(steps, cfg, output_dir=output_dir)
    figs["metrics_table"] = plot_metrics_table(metrics_by_key, output_dir=output_dir)
    figs["reliability_diagram"] = plot_reliability_diagram(
        episodes, metrics_by_key, theta_star, output_dir=output_dir)
    figs["consecutive_rollback"] = plot_consecutive_rollback(metrics_by_key, output_dir=output_dir)
    if headline_method is not None:
        figs["per_ue_kpi"] = plot_per_ue_kpi(metrics_by_key, headline_method,
                                             cfg, output_dir=output_dir)
    return figs


if __name__ == "__main__":
    from experiments.synthetic import generate_experiment, METHOD_PROFILES
    from experiments.metrics import compute_multi_method
    steps, eps = generate_experiment(list(METHOD_PROFILES.keys()))
    m = compute_multi_method(steps, eps)
    out = os.environ.get("FIG_OUT", "/tmp/fig_demo")
    figs = generate_all_figures(steps, eps, m, output_dir=out,
                                headline_method="llm_with_history")
    for k, paths in figs.items():
        print(k, "->", paths)
