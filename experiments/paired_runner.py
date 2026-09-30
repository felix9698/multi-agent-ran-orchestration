#!/usr/bin/env python3
"""
Paired-block runner + raw evidence export (Batch G, P0-20 & P1-6).

Contract:
  * Every configured method runs through the SAME adapter -> schema/admission ->
    safety executor -> evidence path. No baseline is silently skipped and no
    coordinator-default behaviour is mislabeled as a named baseline (a method
    that is not genuinely wired is recorded with an explicit `method_wiring` tag,
    never presented as the named baseline).
  * Method order is randomised REPRODUCIBLY per environment block from the master
    seed; every method in a block replays the block's ONE canonical environment
    trace (P0-19) from the SAME paired start state.
  * The validated default supports >= 20 independent blocks (tests use fewer).
  * Each raw record carries the full identifier chain, provenance, both typed
    event streams, the environment-trace hash and an explicit `data_origin`.
    `actuation_trial_id` exists ONLY for a cycle that attempted a real S3 write.
  * `data_origin` distinguishes synthetic_harness / emulated_pipeline / live_ota.
    Paper export accepts ONLY live_ota records that pass the paper-run gate;
    everything else is `integration_only` and excluded from performance tables.
  * All artifacts are serialised with json.dumps(..., allow_nan=False) over a
    NaN/inf -> null sanitised structure, so no artifact ever emits NaN/Infinity.

stdlib-only (hashlib/json/random), consistent with experiments/metrics.py.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Tuple

from experiments.metrics import EpisodeRecord, IntentConfig, live_intent_config
from experiments.topology import ExperimentTopology, three_ue_shared_topology
from experiments.environment import (
    EnvironmentTrace, generate_environment_trace, EnvironmentActionLog,
    EnvironmentEvent, ActionEvent,
)

DEFAULT_PAPER_BLOCKS = 20            # P0-20: validated default block count

# P0-20: the CANONICAL required comparison method set for the paper. A paper
# paired run must include EXACTLY these six (no unknown method, no duplicate, and
# NEVER a silent fallback to llm_with_history). fixed_threshold and adaptive are
# the theta*-ablation pair (the SAME LLM proposer, calibrator mode fixed vs
# online). llm_with_history is a KNOWN method but NOT one of the required six.
REQUIRED_METHODS = ("rule_based", "score_heuristic", "rl_controller",
                    "llm_no_history", "fixed_threshold", "adaptive")
KNOWN_METHODS = frozenset(REQUIRED_METHODS) | {"llm_with_history"}


def validate_method_set(methods) -> List[str]:
    """Fail-closed comparison-method-set validation (P0-20): reject an UNKNOWN
    method and reject DUPLICATES - never silently substitute / fall back to
    llm_with_history. Returns the validated ordered list."""
    seen: List[str] = []
    for m in methods:
        if m not in KNOWN_METHODS:
            raise ValueError(
                f"unknown comparison method {m!r} (known: "
                f"{sorted(KNOWN_METHODS)}) - refusing to silently substitute")
        if m in seen:
            raise ValueError(f"duplicate comparison method {m!r}")
        seen.append(m)
    return seen


# --------------------------------------------------------------------------- #
# Data origin + paper-export gating                                           #
# --------------------------------------------------------------------------- #

class DataOrigin(Enum):
    SYNTHETIC_HARNESS = "synthetic_harness"   # fabricated profiles, NON-OTA
    EMULATED_PIPELINE = "emulated_pipeline"   # real pipeline, simulated radio
    LIVE_OTA = "live_ota"                     # real testbed over the air

    @property
    def is_paper_ready_candidate(self) -> bool:
        """ONLY a live_ota origin can ever be paper-ready (P1-6)."""
        return self is DataOrigin.LIVE_OTA

    @staticmethod
    def coerce(v) -> "DataOrigin":
        if isinstance(v, DataOrigin):
            return v
        for o in DataOrigin:
            if o.value == v:
                return o
        raise ValueError(f"unknown data_origin {v!r}")


class PaperExportError(RuntimeError):
    """Raised when a non-live (or non-paper-ready) record reaches paper export."""


class PairedResetError(RuntimeError):
    """Raised (FAIL CLOSED) when the exact block-start state cannot be captured
    or restored/read-back-verified. It is raised BEFORE any method runs (capture)
    or before the affected method runs (restore), so a failed paired reset causes
    ZERO method execution rather than an un-paired comparison."""


@dataclass(frozen=True)
class PaperGateEvidence:
    """Independently-collected paper-run gate evidence (Gate E/F).

    Paper export requires EXPLICIT, separately-collected proof for EVERY gate -
    a `data_origin` enum alone can NEVER opt a run into the performance table.
    Each flag must be set True only when its real evidence exists (raw logs /
    version manifest / readback captures referenced in `evidence_refs`). Any
    unmet or absent gate makes the run non-paper-ready (fail closed)."""
    hardware_provisioned: bool = False
    version_manifest_locked: bool = False
    topology_verified: bool = False
    readback_verified: bool = False
    scheduler_efficacy_qualified: bool = False
    environment_action_disjoint: bool = False
    paired_trace_verified: bool = False
    p0_invariants_held: bool = False
    evidence_refs: Dict = field(default_factory=dict)
    # P0-20 (F): the gate is BOUND to ONE specific attested run - the exact
    # experiment_run_id and immutable run digest it was collected against. Export
    # requires these to match the runner's current attestation, so a gate can
    # never be reused to admit a DIFFERENT run.
    attested_experiment_run_id: Optional[str] = None
    attested_run_digest: Optional[str] = None

    REQUIRED = ("hardware_provisioned", "version_manifest_locked",
                "topology_verified", "readback_verified",
                "scheduler_efficacy_qualified", "environment_action_disjoint",
                "paired_trace_verified", "p0_invariants_held")

    def unmet_gates(self) -> List[str]:
        """A gate is UNMET unless it is EXACTLY bool True AND carries a nonempty
        evidence_ref (P0-20-F): a truthy non-bool (1, 'yes') or a True flag with
        no concrete evidence reference is NOT a proof."""
        refs = self.evidence_refs or {}
        bad: List[str] = []
        for g in self.REQUIRED:
            if getattr(self, g, None) is not True:
                bad.append(g)
            elif not str(refs.get(g, "") or "").strip():
                bad.append(g)
        return bad

    def is_paper_ready(self) -> bool:
        return not self.unmet_gates()

    def to_dict(self) -> Dict:
        # report the RAW flag exactly (so a non-bool is visible), plus the
        # consistent paper_ready verdict that also requires nonempty refs, plus
        # the run this gate is BOUND to (auditable in the export payload).
        d = {g: (getattr(self, g, None) is True) for g in self.REQUIRED}
        d["evidence_refs"] = dict(self.evidence_refs)
        d["attested_experiment_run_id"] = self.attested_experiment_run_id
        d["attested_run_digest"] = self.attested_run_digest
        d["paper_ready"] = self.is_paper_ready()
        return d


# --------------------------------------------------------------------------- #
# Finite-JSON helpers (no NaN / Infinity in any artifact)                     #
# --------------------------------------------------------------------------- #

def sanitize_finite(obj):
    """Recursively replace NaN/Infinity floats with None (explicit unknown),
    NEVER a fake 0.0 (handoff: 'emit null/explicit unknown, never fake zero')."""
    import math
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: sanitize_finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_finite(v) for v in obj]
    return obj


def dumps_finite(obj, **kw) -> str:
    """json.dumps with allow_nan=False over a NaN/inf -> null sanitised copy, so
    the strict output can never contain a non-standard JSON constant."""
    return json.dumps(sanitize_finite(obj), allow_nan=False, **kw)


def assert_finite_json(obj) -> None:
    """Fail closed if `obj` cannot be serialised as strict finite JSON."""
    json.dumps(obj, allow_nan=False)


# --------------------------------------------------------------------------- #
# Deterministic block seeding + method ordering                              #
# --------------------------------------------------------------------------- #

def block_seed(master_seed: int, block_idx: int) -> int:
    """A per-block seed derived PURELY from (master_seed, block_idx) via SHA-1
    (never Python hash(), which is salted). Independent of method order."""
    digest = hashlib.sha1(f"{int(master_seed)}:{int(block_idx)}"
                          .encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def deterministic_method_order(methods: List[str], master_seed: int,
                               block_idx: int) -> List[str]:
    """Reproducible per-block method order. Derived from (master_seed, block_idx)
    only, so re-running yields the same order and it does NOT depend on how much
    RNG any method consumes."""
    rng = random.Random(f"order:{int(master_seed)}:{int(block_idx)}")
    ordered = list(methods)
    rng.shuffle(ordered)
    return ordered


# --------------------------------------------------------------------------- #
# Raw evidence record (P1-6 schema)                                          #
# --------------------------------------------------------------------------- #

@dataclass
class RawEvidenceRecord:
    """One raw evidence row with the full provenance chain (P1-6).

    Distinct identifiers: experiment_run_id, method_block_id, fsm_step_id,
    episode_id, pending_intent_id, cycle_id, proposal_id and (ONLY for a real S3
    write) actuation_trial_id.
    """
    # --- identifier chain ---
    # experiment_run_id / episode_id / fsm_step_id are ALWAYS present (every
    # terminal reaches them). The stage-dependent ids are Optional and honestly
    # None when their stage did not run (P1-6 stage honesty): pending_intent_id is
    # None for a pre-parse terminal; cycle_id / proposal_id are None before the
    # proposal stage; actuation_trial_id ONLY for a real S3 write. A None stage id
    # is never fabricated - the raw builder validates presence only when that
    # stage was actually reached.
    experiment_run_id: str
    method_block_id: str
    fsm_step_id: str
    episode_id: str
    pending_intent_id: Optional[str]      # None ONLY for a pre-parse terminal
    cycle_id: Optional[str]               # None before a cycle ran
    proposal_id: Optional[str]            # None before a proposal was generated
    actuation_trial_id: Optional[str]     # ONLY for an actual S3 write

    # --- block / ordering provenance ---
    method: str
    block_id: str
    block_index: int
    master_seed: int
    block_seed: int
    method_order: List[str]
    method_order_index: int
    monotonic_seq: int                    # deterministic monotone offline clock

    # --- model / proposal provenance ---
    model_id: str
    model_version: str
    prompt_hash: Optional[str]
    intent_type: Optional[str]
    # the FULL exact target / scope dicts from the parsed Intent (not flattened);
    # None on a pre-parse terminal.
    intent_target: Optional[Dict]
    intent_scope: Optional[Dict]
    raw_confidence: Optional[float]
    calibrated_probability: Optional[float]
    # the strict-schema verdict: None when no proposal stage ran (no verdict to
    # record) - never coerced from None to a fabricated False.
    schema_verdict: Optional[bool]

    # --- action provenance (proposed/clipped/enforced/readback) ---
    proposed_action: Optional[Dict]
    clipped_action: Optional[Dict]
    enforced_action: Optional[Dict]
    readback_action: Optional[Dict]

    # --- trajectories ---
    trial_trajectory: List
    nego_trajectory: List

    # --- budget ledger ---
    budget_before: Optional[float]
    budget_reserved: Optional[float]
    budget_debited: Optional[float]
    budget_after: Optional[float]

    # --- terminal outcome ---
    terminal_outcome: Optional[str]
    terminal_reason: Optional[str]

    # --- separate typed streams + environment trace + origin ---
    environment_stream: List = field(default_factory=list)
    action_stream: List = field(default_factory=list)
    environment_trace_hash: Optional[str] = None
    data_origin: str = DataOrigin.SYNTHETIC_HARNESS.value
    hardware_state: Optional[Dict] = None
    executor_provenance: Optional[str] = None
    topology_label: Optional[str] = None
    # explicit wiring honesty tag (P0-20): a method that is not genuinely wired
    # to the safety path is recorded as such, never mislabeled as the baseline.
    method_wiring: str = "wired"
    # Batch G (P1-6): the phase INDEX + a UNIQUE phase label (so the first and
    # last Nominal phase are distinct in aggregates), and the ACTUAL monotonic
    # episode timestamp (a real time anchor, distinct from the sequence number).
    phase_idx: Optional[int] = None
    phase_label: Optional[str] = None
    episode_monotonic_s: Optional[float] = None
    # Batch G: an UNKNOWN/partial debit keeps budget_debited=None with a measured
    # lower bound + status + the full reserve ledger (reservation ids/settlements).
    budget_debited_lower_bound: Optional[float] = None
    budget_debit_status: Optional[str] = None
    reserve_ledger: Optional[Dict] = None
    # Batch G (P1-6): EXPLICIT source facts (real bools, validated below), never
    # inferred from another field. proposal_generated: the model returned output
    # this cycle. was_parsed_intent: a real Intent was parsed for this row.
    # phase_name: the phase's name (phase_label is exactly p{phase_idx}:{name}).
    proposal_generated: Optional[bool] = None
    was_parsed_intent: Optional[bool] = None
    phase_name: Optional[str] = None

    def __post_init__(self):
        # ---- phase provenance (P1-6) --------------------------------------- #
        # phase_idx is a REAL non-negative int (a bool is NOT an int here).
        if isinstance(self.phase_idx, bool) \
                or not isinstance(self.phase_idx, int) or self.phase_idx < 0:
            raise ValueError(
                f"phase_idx must be a non-negative int, got {self.phase_idx!r}")
        if not isinstance(self.phase_name, str) or not self.phase_name:
            raise ValueError(
                f"phase_name must be a non-empty string, got {self.phase_name!r}")
        _expect_label = f"p{self.phase_idx}:{self.phase_name}"
        if self.phase_label != _expect_label:
            raise ValueError(
                f"phase_label must be exactly {_expect_label!r}, got "
                f"{self.phase_label!r}")
        # episode_monotonic_s: a finite number > 0 (the pipeline's real monotonic
        # time OR the synthetic harness's deterministic offline clock) - never a
        # missing/zero/negative/NaN anchor.
        _ems = self.episode_monotonic_s
        if isinstance(_ems, bool) or not isinstance(_ems, (int, float)) \
                or not math.isfinite(float(_ems)) or float(_ems) <= 0.0:
            raise ValueError(
                f"episode_monotonic_s must be a finite number > 0, got {_ems!r}")
        # ---- proposal_generated (explicit source fact) --------------------- #
        if not isinstance(self.proposal_generated, bool):
            raise ValueError(
                f"proposal_generated must be a real bool, got "
                f"{self.proposal_generated!r}")
        if self.proposal_generated:
            # a generated proposal MUST carry the full proposal-stage chain.
            if not self.prompt_hash:
                raise ValueError("proposal_generated=True requires a non-empty "
                                 "prompt_hash")
            if not self.cycle_id:
                raise ValueError("proposal_generated=True requires a cycle_id")
            if not self.proposal_id:
                raise ValueError("proposal_generated=True requires a proposal_id")
            if not isinstance(self.schema_verdict, bool):
                raise ValueError("proposal_generated=True requires a real bool "
                                 "schema_verdict")
        else:
            # no proposal: no proposal_id and no schema verdict (a cycle_id MAY
            # exist - a cycle can run without generating a proposal).
            if self.proposal_id is not None:
                raise ValueError("proposal_generated=False requires "
                                 "proposal_id=None")
            if self.schema_verdict is not None:
                raise ValueError("proposal_generated=False requires "
                                 "schema_verdict=None")
        # ---- was_parsed_intent (explicit source fact) ---------------------- #
        if not isinstance(self.was_parsed_intent, bool):
            raise ValueError(
                f"was_parsed_intent must be a real bool, got "
                f"{self.was_parsed_intent!r}")
        if self.was_parsed_intent:
            if not self.pending_intent_id:
                raise ValueError("was_parsed_intent=True requires "
                                 "pending_intent_id")
            if not self.intent_type:
                raise ValueError("was_parsed_intent=True requires intent_type")
            if not isinstance(self.intent_target, dict):
                raise ValueError("was_parsed_intent=True requires a dict "
                                 "intent_target")
            if not isinstance(self.intent_scope, dict):
                raise ValueError("was_parsed_intent=True requires a dict "
                                 "intent_scope")
        else:
            for _nm, _v in (("pending_intent_id", self.pending_intent_id),
                            ("intent_type", self.intent_type),
                            ("intent_target", self.intent_target),
                            ("intent_scope", self.intent_scope)):
                if _v is not None:
                    raise ValueError(
                        f"was_parsed_intent=False requires {_nm}=None, got "
                        f"{_v!r}")
        # strict schema-verdict type (P1-6): the verdict is either None (no
        # proposal stage / no verdict) or a REAL bool - never a coerced/fabricated
        # value from a missing/None/string source.
        if self.schema_verdict is not None \
                and not isinstance(self.schema_verdict, bool):
            raise ValueError(
                f"schema_verdict must be None or a real bool, got "
                f"{self.schema_verdict!r}")
        # invariant (P1-6): actuation_trial_id exists IFF a real S3 write was
        # attempted. A negotiation-only cycle must not carry one.
        if self.actuation_trial_id is not None and \
                self.terminal_outcome == "negotiation_only":
            raise ValueError(
                f"actuation_trial_id set on a negotiation-only cycle "
                f"({self.cycle_id}) - only an actual S3 write gets one")
        # action-stream contract (review blocker): an actuation record MUST carry
        # a non-empty action stream built from ACTUAL applied writes, and every
        # action event MUST have an actual applied (monotonic) timestamp - else
        # fail validation. A non-actuation record must carry NO action events.
        if self.actuation_trial_id is not None:
            if not self.action_stream:
                raise ValueError(
                    f"actuation record {self.cycle_id} has an empty action "
                    f"stream (no applied-write evidence); an S3 write must "
                    f"record its applied action events")
            for a in self.action_stream:
                _am = a.get("applied_monotonic_s")
                # P1-6 (item 5): a REAL applied monotonic timestamp is a non-bool
                # real FINITE value > 0. None / bool / string / NaN / inf / 0 /
                # negative (incl. anything that sanitises to null) FAILS CLOSED.
                if not (isinstance(_am, (int, float))
                        and not isinstance(_am, bool)
                        and math.isfinite(_am) and _am > 0):
                    raise ValueError(
                        f"action event {a.get('event_id')} applied monotonic "
                        f"timestamp must be a real finite value > 0 (got {_am!r}) "
                        f"- actual applied time required")
        elif self.action_stream:
            raise ValueError(
                f"record {self.cycle_id} has action events without an "
                f"actuation_trial_id (no S3 write happened)")

    def to_dict(self) -> Dict:
        return sanitize_finite(asdict(self))


# --------------------------------------------------------------------------- #
# Paired-block runner                                                        #
# --------------------------------------------------------------------------- #

class PairedBlockRunner:
    """Run every method across N paired environment blocks and emit raw evidence.

    The default offline path is the SYNTHETIC HARNESS: it is deterministic, needs
    no hardware, and is TAGGED non-OTA (data_origin=synthetic_harness) so its
    numbers can never flow into a paper performance table. An emulated/live
    coordinator can be supplied to run the real pipeline (data_origin overridden
    accordingly); every method then shares that one coordinator's executor.
    """

    def __init__(self, methods: List[str],
                 topology: Optional[ExperimentTopology] = None,
                 phases: Optional[List[Dict]] = None,
                 n_blocks: int = DEFAULT_PAPER_BLOCKS,
                 master_seed: int = 12345,
                 data_origin: DataOrigin = DataOrigin.SYNTHETIC_HARNESS,
                 cfg: Optional[IntentConfig] = None,
                 trials_per_block: int = 1,
                 steps_per_phase: int = 4,
                 kpi_interval_s: float = 15.0,
                 offered_load_mbps: float = 12.0,
                 experiment_run_id: str = "run",
                 coordinator=None,
                 env_driver=None):
        if not methods:
            raise ValueError("PairedBlockRunner needs at least one method")
        # P0-20: validate the method set (reject unknown / duplicate; no silent
        # llm_with_history fallback).
        self.methods = validate_method_set(methods)
        self.topology = topology or three_ue_shared_topology()
        self.phases = phases or [
            {"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0},
            {"name": "Degradation", "serving_gain": -2, "neighbor_gain": 1},
            {"name": "Impairment", "serving_gain": -5, "neighbor_gain": 2},
            {"name": "Recovery", "serving_gain": 0, "neighbor_gain": 0},
            {"name": "Nominal", "serving_gain": 3, "neighbor_gain": 0},
        ]
        self.n_blocks = int(n_blocks)
        self.master_seed = int(master_seed)
        self.data_origin = DataOrigin.coerce(data_origin)
        # I2 target: an explicit cfg always wins. Otherwise a LIVE_OTA run gets
        # the MEASURED hardware target (config.LIVE_I2_TARGET_MBPS via
        # live_intent_config) - the 8.0 Mbps offline default is above the real
        # 24-PRB cell's single-UE ceiling (4.82 Mbps) and could never be
        # satisfied, collapsing strict satisfaction to 0. synthetic_harness /
        # emulated_pipeline keep IntentConfig's 8.0 paper default EXACTLY as
        # before (it is calibrated against config.REFERENCE_CEILING_MBPS and
        # shared with experiments.synthetic's profiles). See
        # docs/ue_stability_and_capacity.md §5.
        scoped_ues = self.topology.contending_ues() or None
        if cfg is not None:
            self.cfg = cfg
        elif self.data_origin is DataOrigin.LIVE_OTA:
            self.cfg = live_intent_config(throughput_ue_ids=scoped_ues or ())
        else:
            self.cfg = IntentConfig(throughput_ue_ids=scoped_ues)
        self.trials_per_block = int(trials_per_block)
        self.steps_per_phase = int(steps_per_phase)
        self.kpi_interval_s = float(kpi_interval_s)
        self.offered_load_mbps = float(offered_load_mbps)
        self.experiment_run_id = str(experiment_run_id)
        self.coordinator = coordinator
        # P0-19: the LIVE exogenous environment driver injected into the shared
        # pipeline ExperimentRunner (a live pipeline has no channel model, so
        # without this every live paired run would fail closed at startup with
        # driver=None). Emulated pipelines leave it None and use the ChannelModel.
        self.env_driver = env_driver
        self._seq = 0                     # deterministic monotone offline clock
        self._ep_counter = 0              # global episode counter (distinct ids)

    # -- id helpers ---------------------------------------------------------- #

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # -- public API ---------------------------------------------------------- #

    def run(self) -> Dict:
        """Run all blocks; return the structured result (blocks + flat records +
        provenance). Deterministic for the synthetic origin."""
        blocks: List[Dict] = []
        records: List[RawEvidenceRecord] = []
        executor_ids: set = set()
        # P0-20 (E): capture the EXPERIMENT-START state ONCE (pipeline only) so it
        # can be restored before EVERY block. A block therefore begins from the
        # identical controlled experiment-start - NOT the previous block's end
        # state - which is what makes the >=20 blocks INDEPENDENT. None (only) for
        # the synthetic harness (no mutable coordinator state). Fail-closed
        # capture (missing/no-op primitives raise) runs BEFORE any block.
        exp_start = self._capture_block_state()
        # P0-20 (Codex-3): on SUCCESS AND FAILURE, finally restore + verify the
        # experiment-start state so a run never leaves the last method's/block's
        # mutable state behind. If the body raised AND the cleanup restore also
        # fails, the cleanup failure is chained (observable), never log-only.
        body_exc = None
        try:
            for b in range(self.n_blocks):
                block = self._run_block(b, records, executor_ids, exp_start)
                blocks.append(block)
        except BaseException as e:                # noqa: BLE001 - re-raised below
            body_exc = e
            raise
        finally:
            if exp_start is not None:
                try:
                    self._restore_block_state_verified(exp_start)
                except Exception as ce:
                    if body_exc is not None:
                        raise PairedResetError(
                            f"experiment-start restore FAILED during cleanup "
                            f"after a run error: {ce}") from body_exc
                    raise
        synthetic = self.data_origin is DataOrigin.SYNTHETIC_HARNESS
        # P0-20 (G): a synthetic run has NO executor object (empty ids -> shared
        # vacuously, acceptable ONLY for the explicit synthetic origin). A
        # pipeline run MUST have EXACTLY ONE non-null shared executor identity
        # across every method.
        executor_shared = (len(executor_ids) == 0 if synthetic
                           else len(executor_ids) == 1)
        result = {
            "experiment_run_id": self.experiment_run_id,
            "data_origin": self.data_origin.value,
            "master_seed": self.master_seed,
            "n_blocks": self.n_blocks,
            "methods": list(self.methods),
            "required_methods_present": set(REQUIRED_METHODS) <= set(self.methods),
            "topology": self.topology.to_dict(),
            "blocks": blocks,
            "records": [r.to_dict() for r in records],
            "n_records": len(records),
            # P0-20: a single shared safety executor across every method (empty
            # for the pure-synthetic path, which has no executor object).
            "shared_executor_ids": sorted(executor_ids),
            "executor_shared": executor_shared,
            # P0-20 (E): every block restored the experiment-start state before it
            # ran (block independence). None for the synthetic origin.
            "block_independence_verified": (None if synthetic
                                            else all(b.get("block_independent")
                                                     for b in blocks)),
            # P1-6: EXPLICIT export-eligibility classification IN the artifact
            # (not just a comment/error). EVERY raw run() result - INCLUDING
            # LIVE_OTA - is integration_only and EXCLUDED from the paper table:
            # data_origin=live_ota already expresses "candidate", but performance
            # eligibility is earned ONLY by a successful paper_export() (all gates +
            # attestation). Deriving excluded=False from the origin label alone
            # would open a bypass, so these are UNCONDITIONAL here.
            "paper_eligibility": "integration_only",
            "excluded_from_paper": True,
            "paper_ready": False,
        }
        # P0-20 (F): stamp an IMMUTABLE in-memory attestation binding THIS genuine
        # result to THIS runner instance (a digest over the exact result content +
        # run id + origin + runner identity). paper_export admits ONLY a result
        # that STILL matches this attestation - a mutated / relabeled / foreign /
        # deep-copy-then-mutated result is rejected, and export NEVER starts a new
        # run (so the exported run is exactly the one the gate refs attest).
        digest = self._attestation_digest(result)
        self._run_attestation = {
            "digest": digest,
            "experiment_run_id": self.experiment_run_id,
            "data_origin": self.data_origin.value,
            "runner_id": id(self),
        }
        result["run_attestation"] = dict(self._run_attestation)
        return result

    def _attestation_digest(self, result: Dict) -> str:
        """SHA-256 over the result's EXACT content EXCLUDING the attestation field
        itself, so the digest is stable and detects ANY content mutation.

        TAMPER-EVIDENT: the body is serialised with NO NaN/inf sanitisation
        (allow_nan=False) - a NON-FINITE value FAILS CLOSED (raises ValueError)
        rather than being normalised to null. Otherwise a None -> NaN (or -> inf)
        mutation would sanitise back to the SAME digest and defeat the exact
        tamper-evidence claim. sort_keys gives a canonical byte form."""
        body = {k: v for k, v in result.items() if k != "run_attestation"}
        blob = json.dumps(body, allow_nan=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @staticmethod
    def _paired_reset_verified(methods, start_digests: Dict[str, str],
                               readback_flags: List) -> Optional[bool]:
        """The STRICT per-block paired-reset verdict.

        None  -> no snapshot basis (pure-synthetic harness; verified by SEED).
        True  -> EVERY configured method produced a start-observation digest,
                 ALL those digests are IDENTICAL, the readback-flag count matches
                 the method count, AND every readback flag is EXACTLY True.
        False -> otherwise. A None/absent readback flag is NOT a pass (the old
                 `is not False` let None through); a missing method digest, a
                 divergent digest, or a length mismatch also fail closed."""
        if not start_digests:
            return None
        method_set = set(methods)
        all_methods_have_digest = set(start_digests) == method_set
        obs_same = len(set(start_digests.values())) == 1
        rb_ok = (len(readback_flags) == len(method_set)
                 and all(f is True for f in readback_flags))
        return bool(all_methods_have_digest and obs_same and rb_ok)

    def _run_block(self, block_idx: int, records: List[RawEvidenceRecord],
                   executor_ids: set, exp_start=None) -> Dict:
        bseed = block_seed(self.master_seed, block_idx)
        block_id = f"{self.experiment_run_id}:blk{block_idx}"
        # P0-20 (E): restore the EXPERIMENT-START state (verified) so this block is
        # INDEPENDENT of the previous block's end state, THEN apply block-specific
        # deterministic RNG (reseed the channel by the block seed) so the block
        # has its own reproducible noise realization. None for the synthetic
        # origin (independence is by block SEED there).
        block_independent = None
        if exp_start is not None:
            self._restore_block_state_verified(exp_start)
            # block-specific deterministic RNG via the ORIGIN-appropriate state
            # driver (emulated ChannelModel OR the live env_driver).
            drv = self._state_driver()
            pre_reseed = drv.capture_state()          # == exp-start env state
            drv.reseed(bseed)
            post_reseed = drv.capture_state()
            # A no-op reseed would leave EVERY block at the SAME env realization,
            # so the blocks would NOT be independent. Fail closed BEFORE running a
            # single method rather than silently trusting that reseed() worked.
            if post_reseed == pre_reseed:
                raise PairedResetError(
                    f"block {block_idx}: reseed({bseed}) was a NO-OP (env state "
                    f"unchanged) - blocks would not be independent")
            # Determinism: restoring the experiment-start state and reseeding the
            # SAME block seed must reproduce the SAME readback (a reproducible,
            # deterministic per-block RNG); the block then proceeds from it.
            self._restore_block_state_verified(exp_start)
            drv.reseed(bseed)
            if drv.capture_state() != post_reseed:
                raise PairedResetError(
                    f"block {block_idx}: reseed({bseed}) is NON-DETERMINISTIC "
                    f"(same seed produced a different env readback)")
            block_independent = True
        # ONE canonical environment trace for this block (P0-19), replayed
        # identically to every method regardless of order / RNG consumption.
        trace = generate_environment_trace(
            self.phases, block_id=block_id, seed=bseed,
            offered_load_mbps=self.offered_load_mbps,
            steps_per_phase=self.steps_per_phase,
            kpi_interval_s=self.kpi_interval_s)
        trace_hash = trace.canonical_hash()
        order = deterministic_method_order(self.methods, self.master_seed,
                                           block_idx)
        # VERIFIED same-block start state (P0-20): capture the block-start state
        # (AFTER the experiment-start restore + block reseed) ONCE and restore it
        # before EVERY method so all methods in the block begin from an identical
        # state; the first-observation digest is recorded per method and must be
        # IDENTICAL (else the reset is not verified).
        block_snap = self._capture_block_state()
        method_records: Dict[str, int] = {}
        start_digests: Dict[str, str] = {}
        readback_flags: List = []
        for order_idx, method in enumerate(order):
            v = self._restore_block_state_verified(block_snap)
            if v is not None:
                if v.get("obs_digest") is not None:
                    start_digests[method] = v["obs_digest"]
                readback_flags.append(v.get("readback_verified"))
            recs = self._run_method_in_block(
                method, block_idx, block_id, bseed, order, order_idx,
                trace, trace_hash, executor_ids)
            records.extend(recs)
            method_records[method] = len(recs)
        # for the pure-synthetic harness the paired start state is the identical
        # block SEED (deterministic; same trace hash) - there is no mutable
        # coordinator state to snapshot, so verification is by seed/trace.
        # STRICT paired-reset verdict (see _paired_reset_verified): True ONLY when
        # every method observed an IDENTICAL start state AND every executor device
        # read-back was EXACTLY True; a None/absent flag is NOT a pass. None for
        # the pure-synthetic harness (verification is by block SEED / trace hash).
        reset_verified = self._paired_reset_verified(
            self.methods, start_digests, readback_flags)
        return {
            "block_id": block_id,
            "block_index": block_idx,
            "block_seed": bseed,
            "method_order": order,
            "environment_trace": trace.to_dict(),
            "environment_trace_hash": trace_hash,
            "n_records_by_method": method_records,
            # P0-20 honesty: EVERY configured method appears in EVERY block WITH a
            # real int > 0 record count - a baseline can never be silently skipped
            # NOR counted as "executed" with ZERO records.
            "all_methods_present": (
                set(method_records) == set(self.methods)
                and all(isinstance(n, int) and not isinstance(n, bool) and n > 0
                        for n in method_records.values())),
            # P0-20: verified identical paired start state across methods.
            "paired_reset_verified": reset_verified,
            "start_observation_digests": start_digests,
            "paired_start_basis": ("block_seed" if not start_digests
                                   else "snapshot_restore"),
            # P0-20 (E): this block restored the experiment-start state before it
            # ran (independent of the previous block). None for synthetic.
            "block_independent": block_independent,
        }

    def _state_driver(self):
        """The ORIGIN-APPROPRIATE environment STATE driver for a paired reset
        (P0-20). EMULATED_PIPELINE -> the ChannelModel (capture/restore/reseed of
        the SIMULATED radio). LIVE_OTA -> the APPROVED exogenous env_driver
        (capture_state/restore_state/reseed of the REAL attenuator/load/position),
        which REQUIRES channel_model to be None (an emulated channel can NEVER
        stand in for a live run). None for the synthetic harness. FAIL CLOSED if
        the required driver / capability is absent."""
        if self.data_origin is DataOrigin.SYNTHETIC_HARNESS:
            return None
        if self.data_origin is DataOrigin.LIVE_OTA:
            if getattr(self, "channel_model", None) is not None:
                raise PairedResetError(
                    "LIVE_OTA must NOT use a ChannelModel (an emulated radio "
                    "cannot be relabeled live) - the state driver is the approved "
                    "live env_driver (P0-20)")
            drv = getattr(self, "env_driver", None)
            from experiments.environment import validate_exogenous_driver
            validate_exogenous_driver(drv)   # exists + approved + env axes only
            for cap in ("capture_state", "restore_state", "reseed"):
                if not callable(getattr(drv, cap, None)):
                    raise PairedResetError(
                        f"live env_driver lacks callable {cap}() - cannot "
                        f"capture/restore the live environment state for a paired "
                        f"reset (P0-20)")
            return drv
        # EMULATED_PIPELINE
        ch = getattr(self, "channel_model", None)
        for cap in ("capture_state", "restore_state", "reseed"):
            if ch is None or not callable(getattr(ch, cap, None)):
                raise PairedResetError(
                    f"emulated ChannelModel lacks callable {cap}() (P0-20)")
        return ch

    def _proposer_scope(self, mgr) -> Dict[str, object]:
        """The proposer backends THIS run can ACTUALLY exercise, keyed by name.

        P0-20 (B): a backend that is merely DISCOVERED but never selected by any
        configured method cannot leak state across methods or blocks - no method
        ever calls it. Snapshotting every discovered backend made the paired
        guard depend on what happened to be installed on a local model server (a
        LiteLLM proxy advertises every model it hosts), which blocked genuine
        runs for a reason unrelated to pairing. The in-scope set is derived from
        the run's OWN wiring, mirroring ExperimentRunner._configure_method
        exactly:

          * ``baseline:<method>`` for every configured method wired to a baseline
            proposer backend (those methods SELECT it),
          * ``mock:deterministic`` - the emulated pipeline's proposer, and the
            object the runner reseeds for paired trials,
          * the ACTIVE proposer object - the LLM that every llm_* method resets
            to - whether it is a dynamic ``local:*`` backend or a FIXED enum
            backend (the old guard never checked fixed backends at all).

        Narrowing the SET does not weaken the guard: every member must still
        expose capture/restore, is still captured, restored and read-back
        verified, and _restore_block_state_verified RE-DERIVES this set and FAILS
        CLOSED if it no longer matches the snapshot (i.e. if a backend outside
        the paired snapshot became the active proposer during a block).
        """
        dyn = getattr(mgr, "dynamic_backends", {}) or {}
        scope: Dict[str, object] = {}
        for m in self.methods:
            key = f"baseline:{m}"
            if key in dyn:
                scope[key] = dyn[key]
        if "mock:deterministic" in dyn:
            scope["mock:deterministic"] = dyn["mock:deterministic"]
        getter = getattr(mgr, "active_backend_object", None)
        namer = getattr(mgr, "active_backend_name", None)
        active = getter() if callable(getter) else None
        if active is not None:
            name = namer() if callable(namer) else None
            if not name:
                name = getattr(active, "name", None)
            if name:
                scope[str(name)] = active
        return scope

    @staticmethod
    def _backend_by_name(mgr, name):
        """Resolve a SNAPSHOTTED proposer name back to its live object (dynamic
        or fixed enum). Returns None when the backend is gone."""
        getter = getattr(mgr, "backend_by_name", None)
        if callable(getter):
            try:
                return getter(name)
            except Exception:                     # pragma: no cover - defensive
                return None
        return (getattr(mgr, "dynamic_backends", {}) or {}).get(name)

    def _capture_block_state(self):
        """Capture the EXACT block-start state for a verified paired reset
        (pipeline path only). FAIL CLOSED: every required primitive MUST exist and
        succeed; any missing/erroring primitive raises PairedResetError BEFORE any
        method runs (so a run that cannot be paired executes ZERO methods rather
        than an un-paired comparison). Captures the ORIGIN-appropriate environment
        state (emulated ChannelModel OR the live env_driver); the active backend
        SELECTION and every IN-SCOPE proposer backend's exact state
        (see _proposer_scope); the executor's DEVICE
        snapshot (read back from the gNB); the calibrator's EXACT state; and the
        history reservoir, frozen-history and history-mode. None for synthetic."""
        drv = self._state_driver()
        if drv is None:
            return None
        c = self.coordinator
        mgr = getattr(c, "llm_manager", None)
        ex = getattr(c, "executor", None)
        cal = getattr(c, "calibrator", None)
        if mgr is None or not hasattr(mgr, "active_selection_snapshot") \
                or not hasattr(mgr, "restore_active_selection"):
            raise PairedResetError("backend manager lacks selection snapshot")
        if ex is None or not hasattr(ex, "snapshot") or not hasattr(ex, "restore"):
            raise PairedResetError("executor lacks snapshot/restore")
        if cal is None:
            raise PairedResetError("no calibrator to snapshot")
        if not (hasattr(c, "history_snapshot")
                and hasattr(c, "restore_history_snapshot")):
            raise PairedResetError("coordinator lacks history snapshot/restore")
        scope = self._proposer_scope(mgr)
        # P0-20 (E/G): every IN-SCOPE proposer backend MUST support exact
        # capture/restore - a backend without it is NOT silently skipped (its
        # mutable proposer state would leak across methods/blocks), it FAILS
        # CLOSED before any execution. Remote LLM backends satisfy this via an
        # EXPLICIT stateless contract (StatelessProposerStateMixin) whose restore
        # re-verifies the backend did not mutate - never a no-op snapshot.
        for n, b in scope.items():
            if not (callable(getattr(b, "capture_state", None))
                    and callable(getattr(b, "restore_state", None))):
                raise PairedResetError(
                    f"proposer backend {n!r} lacks capture/restore_state - "
                    f"refusing to silently skip its state (P0-20)")
        # NOTE: no try/except - any failure propagates as fail-closed.
        return {
            "env_state": drv.capture_state(),                 # origin state driver
            "selection": mgr.active_selection_snapshot(),
            "backends": {n: b.capture_state() for n, b in scope.items()},
            "executor": ex.snapshot(from_device=True),       # device read-back
            "calibrator": copy.deepcopy(cal.__dict__),        # EXACT, not reset
            "history": c.history_snapshot(),
            "frozen_history": getattr(c, "_frozen_history", tuple()),
            "history_mode": getattr(c, "history_mode", None),
        }

    def _restore_block_state_verified(self, snap) -> Optional[Dict]:
        """Restore the coordinator to the EXACT block-start state and VERIFY it.
        FAIL CLOSED: no swallowing, no reset fallback; the executor is RESTORED
        from its device snapshot with read-back verification and a False verdict
        RAISES; a failed observation RAISES. Returns {obs_digest,
        readback_verified=True} on success. None for the synthetic harness."""
        if snap is None:
            return None
        c = self.coordinator
        drv = self._state_driver()            # origin-appropriate state driver
        mgr = c.llm_manager
        ex = c.executor
        cal = c.calibrator
        # P0-20 (B): RE-DERIVE the reachable proposer set BEFORE restoring the
        # selection and require it to be CONTAINED in the paired snapshot. A
        # proposer that became reachable/active AFTER the capture holds state
        # that was never snapshotted and therefore cannot be paired - FAIL CLOSED
        # instead of running an un-paired comparison. This containment check is
        # what keeps the narrowed snapshot set exactly as strong as snapshotting
        # every discovered backend. (Subset, not equality: the randomised method
        # order legitimately leaves a DIFFERENT snapshotted proposer active
        # between methods, which shrinks the derived set without adding anything.)
        outside = sorted(set(self._proposer_scope(mgr)) - set(snap["backends"]))
        if outside:
            raise PairedResetError(
                f"proposer backend(s) {outside} became reachable after the "
                f"paired capture and hold un-snapshotted state - refusing an "
                f"un-paired restore (P0-20)")
        # every SNAPSHOTTED proposer must still exist (by name) and restore.
        live: Dict[str, object] = {}
        for n in snap["backends"]:
            b = self._backend_by_name(mgr, n)
            if b is None or not callable(getattr(b, "restore_state", None)):
                raise PairedResetError(f"backend {n!r} vanished; cannot restore")
            live[n] = b
        drv.restore_state(snap["env_state"])
        mgr.restore_active_selection(snap["selection"])          # backend selection
        for n, st in snap["backends"].items():
            try:
                live[n].restore_state(st)
            except PairedResetError:
                raise
            except Exception as e:                # never swallowed - fail closed
                raise PairedResetError(
                    f"backend {n!r} state restore FAILED: {e}") from e
        # EXACT calibrator restore (theta*/N_max/mode/reserve priors), not reset.
        cal.__dict__.clear()
        cal.__dict__.update(copy.deepcopy(snap["calibrator"]))
        # EXACT executor restore WITH read-back verification - fail closed on False
        if not ex.restore(snap["executor"]):
            raise PairedResetError(
                "executor device restore FAILED read-back verification")
        c.restore_history_snapshot(snap["history"])
        if hasattr(c, "set_frozen_history"):
            c.set_frozen_history(snap["frozen_history"])
        if snap["history_mode"] is not None and hasattr(c, "set_history_mode"):
            c.set_history_mode(snap["history_mode"])
        # P0-20 (Codex-1): a 'verified' reset must be REAL, not name-only. RE-
        # CAPTURE every restored primitive and require EXACT (deep) equality with
        # the snapshot; a mismatch or no-op restore FAILS CLOSED. (The env-state
        # restore is proven by capture_state read-back, NOT by a return value; the
        # executor keeps its device read-back bool above.)
        if drv.capture_state() != snap["env_state"]:
            raise PairedResetError(
                "environment state restore did not read back EXACTLY (P0-20)")
        if mgr.active_selection_snapshot() != snap["selection"]:
            raise PairedResetError("backend selection restore mismatch (P0-20)")
        re_backends = {n: live[n].capture_state() for n in snap["backends"]}
        if re_backends != snap["backends"]:
            raise PairedResetError("proposer backend state restore mismatch (P0-20)")
        if copy.deepcopy(cal.__dict__) != snap["calibrator"]:
            raise PairedResetError("calibrator state restore mismatch (P0-20)")
        if c.history_snapshot() != snap["history"]:
            raise PairedResetError("history restore mismatch (P0-20)")
        if getattr(c, "_frozen_history", tuple()) != snap["frozen_history"]:
            raise PairedResetError("frozen history restore mismatch (P0-20)")
        if getattr(c, "history_mode", None) != snap["history_mode"]:
            raise PairedResetError("history mode restore mismatch (P0-20)")
        # verification observation WITHOUT disturbing the state driver's state; a
        # failed observation is fatal (fail closed).
        pre = drv.capture_state()
        try:
            obs = c.ue_collector.collect_all()
        finally:
            drv.restore_state(pre)
            # the verification observation must leave NO state behind - prove the
            # driver read back to `pre` EXACTLY (P0-20 Codex-4).
            if drv.capture_state() != pre:
                raise PairedResetError(
                    "verification-observation restore did not read back exactly")
        body = {u: (round(m.throughput_mbps, 3)
                    if getattr(m, "throughput_mbps", None) is not None
                    else None) for u, m in sorted(obs.items())}
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        return {"obs_digest": digest, "readback_verified": True}

    def _run_method_in_block(self, method: str, block_idx: int, block_id: str,
                             bseed: int, order: List[str], order_idx: int,
                             trace: EnvironmentTrace, trace_hash: str,
                             executor_ids: set) -> List[RawEvidenceRecord]:
        """Generate this (method, block)'s records through the shared path."""
        if self.data_origin is DataOrigin.SYNTHETIC_HARNESS:
            episodes, model_id, wiring, exec_id = self._synthetic_episodes(
                method, bseed)
        else:
            episodes, model_id, wiring, exec_id = self._pipeline_episodes(
                method, trace)
        if exec_id is not None:
            executor_ids.add(exec_id)
        env_stream = self._stamp_env_stream(trace)
        out: List[RawEvidenceRecord] = []
        method_block_id = f"{block_id}:{method}"
        for ep in episodes:
            self._ep_counter += 1
            out.append(self._raw_from_episode(
                ep, method, method_block_id, block_id, block_idx, bseed,
                order, order_idx, trace_hash, env_stream, model_id, wiring,
                exec_id))
        return out

    def _stamp_env_stream(self, trace: EnvironmentTrace) -> List[Dict]:
        """Return the replayed environment events STAMPED with their ACTUAL
        applied time (P0-19: the trace must never be left un-stamped).

        On the PIPELINE path the applied time is the REAL monotonic time the
        phase's environment was set, matched by the UNIQUE phase identity
        (phase_label 'p{idx}:{name}') from THIS method's runner env-apply log.
        Every replayed phase MUST have exactly ONE recorded, valid timestamp: a
        MISSING, DUPLICATE, or INVALID (non-finite / <= 0 / bool) record RAISES
        - there is NO time.monotonic() fallback that would fabricate evidence.
        On the synthetic harness the applied time is the deterministic monotone
        offline clock. All events (gains, offered load, disturbance) are stamped.
        """
        real = self.data_origin is not DataOrigin.SYNTHETIC_HARNESS
        applied_by_label: Dict[str, float] = {}
        if real:
            # MULTI-TRIAL ATTRIBUTION (P0-19): the per-block canonical trace is
            # replayed once per method and stamped ONCE into the method's env
            # stream; with >1 pipeline trial the per-trial env-apply log would
            # leave only the FINAL trial's timestamps, mis-attributing earlier
            # trials' episodes to them. Fail closed rather than fabricate that
            # attribution (the paired evidence path uses trials_per_block=1).
            if int(self.trials_per_block) > 1:
                raise PaperExportError(
                    "pipeline environment evidence requires trials_per_block=1 "
                    f"(got {self.trials_per_block}); multiple trials would "
                    "attribute earlier episodes to the final trial's applied "
                    "timestamps - refusing (fail closed)")
            runner = getattr(self, "_pr", None)
            for rec in (getattr(runner, "_env_apply_log", []) or []):
                label = rec.get("phase_label")
                ts = rec.get("applied_monotonic_s")
                if not isinstance(label, str) or not label:
                    raise PaperExportError(
                        f"environment apply evidence has no phase_label: {rec!r}")
                if label in applied_by_label:
                    raise PaperExportError(
                        f"duplicate environment apply evidence for phase "
                        f"{label!r} (ambiguous - refusing to stamp)")
                if isinstance(ts, bool) or not isinstance(ts, (int, float)) \
                        or not math.isfinite(float(ts)) or float(ts) <= 0:
                    raise PaperExportError(
                        f"invalid applied timestamp for phase {label!r}: {ts!r}")
                applied_by_label[label] = float(ts)
        out: List[Dict] = []
        for ev in trace.replay():
            if real:
                applied = applied_by_label.get(ev.phase_label)
                if applied is None:
                    raise PaperExportError(
                        f"no environment apply evidence for replayed phase "
                        f"{ev.phase_label!r} (event {ev.event_id!r}) - refusing "
                        f"to fabricate an applied time")
            else:
                applied = float(self._next_seq())
            out.append(ev.with_applied(applied).to_dict())
        return out

    def _synthetic_episodes(self, method: str, bseed: int
                            ) -> Tuple[List[EpisodeRecord], str, str, Optional[str]]:
        """Records from the synthetic harness (paired start state = same block
        seed for every method). Deterministic, NON-OTA. Every configured method
        is honestly its own METHOD_PROFILE - not skipped, not mislabeled."""
        from experiments.synthetic import generate_run, profile_for_method
        profile = profile_for_method(method)     # reject unknown; no silent sub
        episodes: List[EpisodeRecord] = []
        for tid in range(1, self.trials_per_block + 1):
            rng = random.Random(f"{bseed}:{method}:{tid}")
            _s, e = generate_run(
                method, tid, profile, self.cfg, self.phases,
                self.steps_per_phase, rng, ue_ids=self.topology.ue_ids())
            episodes.extend(e)
        return episodes, method, "wired", None

    def _pipeline_episodes(self, method: str, trace: EnvironmentTrace
                           ) -> Tuple[List[EpisodeRecord], str, str, Optional[str]]:
        """Records from the REAL pipeline over a supplied coordinator (emulated /
        live), driven through the FULL method wiring: run_live() applies
        _configure_method (baseline fail-closed + proposer reset), replays the
        environment trace's phases step by step into the channel (NOT final-state
        only), and RESTORES the entry backend. It never calls _run_live_trial
        directly (which would bypass all of that)."""
        from experiments.runner import ExperimentRunner
        if self.coordinator is None:
            raise RuntimeError(
                "pipeline data_origin requires a coordinator (emulated/live)")
        c = self.coordinator
        # P0-20 (G): a pipeline run requires ONE genuine non-null executor shared
        # by every method - a None executor cannot be the shared safety path.
        ex = getattr(c, "executor", None)
        if ex is None:
            raise RuntimeError(
                "pipeline data_origin requires a non-null coordinator executor "
                "(the shared safety path) - refusing (P0-20)")
        exec_id = f"executor@{id(ex):#x}"
        runner = self._pipeline_runner(c)
        # the runner replays THESE phases (the same phases the canonical trace was
        # materialised from) step by step, so environment != final-state only.
        runner.phases = list(self._phases_from_trace(trace))
        _s, episodes = runner.run_live([method], trials=self.trials_per_block)
        # P0-20 (D): read the REAL proposer/model identity captured DURING this
        # method's execution, NOT runner._active_model_key(c) - which run_live's
        # finally has already RESTORED to the entry LLM, mislabeling a baseline.
        model_id = (getattr(runner, "_method_model_ids", {}) or {}).get(method)
        if not model_id or model_id == "unknown":
            raise RuntimeError(
                f"no verified proposer identity captured for method {method!r} "
                f"during execution - refusing to label its records (P0-20)")
        wiring = "wired"     # run_live fails closed if a method is not wired
        return episodes, model_id, wiring, exec_id

    def _pipeline_runner(self, c):
        """One shared ExperimentRunner over the coordinator (reused per method so
        the SAME executor / safety path is used for every method, P0-20)."""
        from experiments.runner import ExperimentRunner
        r = getattr(self, "_pr", None)
        if r is None:
            r = ExperimentRunner(coordinator=c, cfg=self.cfg,
                                 kpi_interval_s=0.0,
                                 steps_per_phase=self.steps_per_phase)
            r.channel_model = getattr(self, "channel_model", None)
            r.probe_config = getattr(self, "probe_config", r.probe_config)
            self._pr = r
        # P0-19: propagate the SAME injected exogenous environment driver to the
        # pipeline runner on EVERY call (new OR reused), so a live paired run has
        # the approved driver and does not fail closed with driver=None.
        r.env_driver = self.env_driver
        return r

    @staticmethod
    def _phases_from_trace(trace: EnvironmentTrace) -> List[Dict]:
        """Reconstruct the per-phase environment (serving/neighbor gains, offered
        load, disturbance) from the canonical trace so the runner replays the
        SAME environment step by step.

        The phase NAME is preserved SEPARATELY from the unique phase_label
        (P0-19: 'p0:Nominal' must NOT become the phase name), each phase carries
        its real phase_idx + phase_label, and a MISSING / INVALID / INCONSISTENT
        phase identity is REJECTED - never a None / duplicate / collapsed phase.
        """
        by_idx: Dict[int, Dict] = {}
        for ev in trace.replay():
            pidx = ev.phase_idx
            if isinstance(pidx, bool) or not isinstance(pidx, int) or pidx < 0:
                raise PaperExportError(
                    f"trace {trace.block_id!r} event {ev.event_id!r} has an "
                    f"invalid phase_idx {pidx!r}")
            pname = ev.phase_name
            if not isinstance(pname, str) or not pname.strip():
                raise PaperExportError(
                    f"trace {trace.block_id!r} event {ev.event_id!r} has an "
                    f"invalid phase_name {pname!r}")
            expect = f"p{pidx}:{pname}"
            if ev.phase_label != expect:
                raise PaperExportError(
                    f"trace {trace.block_id!r} event {ev.event_id!r} phase_label "
                    f"{ev.phase_label!r} != expected {expect!r}")
            p = by_idx.setdefault(pidx, {"name": pname, "phase_idx": pidx,
                                         "phase_label": ev.phase_label})
            # the phase identity must be CONSISTENT across all of its events (no
            # two different phases collapsed under one index).
            if p["name"] != pname or p["phase_label"] != ev.phase_label:
                raise PaperExportError(
                    f"trace {trace.block_id!r} phase index {pidx} has "
                    f"inconsistent identity across events")
            if ev.axis == "channel_serving_gain_db":
                p["serving_gain"] = ev.value
            elif ev.axis == "channel_neighbor_gain_db":
                p["neighbor_gain"] = ev.value
            elif ev.axis == "external_disturbance_db":
                p["external_disturbance_db"] = ev.value
            elif ev.axis == "offered_load_mbps":
                p["offered_load_mbps"] = ev.value
        if not by_idx:
            raise PaperExportError(f"trace {trace.block_id!r} has no phases")
        return [by_idx[k] for k in sorted(by_idx)]

    def _raw_from_episode(self, ep: EpisodeRecord, method: str,
                          method_block_id: str, block_id: str, block_idx: int,
                          bseed: int, order: List[str], order_idx: int,
                          trace_hash: str, env_stream: List, model_id: str,
                          wiring: str, exec_id: Optional[str]
                          ) -> RawEvidenceRecord:
        real = self.data_origin is not DataOrigin.SYNTHETIC_HARNESS
        cyc = int(getattr(ep, "cycle_idx", 0))
        if real:
            # PIPELINE (emulated / live_ota): EVERY coordinator-sourced field is
            # REQUIRED - fail closed on any missing REQUIRED provenance, never
            # fabricate or substitute a runner-side value. Fields that are
            # legitimately absent for a given terminal state (pending_intent_id on
            # a pre-parse terminal, prompt_hash on a pre-proposal terminal, the
            # action vectors on a no-write terminal, budget on a no-ledger episode)
            # stay honest None - never a fabricated stand-in.
            def _req(val, name):
                if val is None or (isinstance(val, str) and not val):
                    raise ValueError(
                        f"pipeline record ({method}): missing REAL {name} from "
                        f"the coordinator - refusing to fabricate/substitute it")
                return val
            # ALWAYS-present provenance (every terminal reaches these): fail closed.
            eid = str(_req(getattr(ep, "evidence_episode_id", None), "episode_id"))
            fsm_step_id = str(_req(getattr(ep, "evidence_fsm_step_id", None),
                                   "fsm_step_id"))
            # experiment_run_id MUST be the coordinator's OWN run id; the runner's
            # label is not the coordinator run id (fail closed, no substitution).
            run_id = str(_req(getattr(ep, "evidence_experiment_run_id", None),
                              "experiment_run_id"))
            # model IDENTITY on the pipeline path is the coordinator's finalized
            # EvidenceRecord provenance: proposer_id + model_version, BOTH required
            # (fail closed). Never the runner label or the current manager backend
            # id (those are not the finalized evidence). model_version's own honest
            # "unknown" is a REAL coordinator value, not a fabrication.
            model_id_val = str(_req(getattr(ep, "evidence_proposer_id", None),
                                    "proposer_id"))
            model_version = str(_req(getattr(ep, "evidence_model_version", None),
                                     "model_version"))
            # terminal outcome/reason MUST be the coordinator's FINALIZED values -
            # never re-derived from cycle flags (no `or self._terminal_outcome`).
            terminal_outcome = str(_req(getattr(ep, "terminal_outcome", None),
                                        "terminal_outcome"))
            terminal_reason = str(_req(getattr(ep, "terminal_reason", None),
                                       "terminal_reason"))
            # prompt hash: the REAL hash stamped at generation (retained even on a
            # model timeout); None ONLY when no proposal prompt was generated.
            prompt_hash = getattr(ep, "evidence_prompt_hash", None)
            # proposal-stage decision uses the EXPLICIT coordinator source fact
            # (a proposal was actually generated), NOT prompt_hash presence. It
            # MUST be an explicit bool on the pipeline path (None fails closed -
            # never silently treated as "no proposal").
            _pg = getattr(ep, "proposal_generated", None)
            if not isinstance(_pg, bool):
                raise ValueError(
                    f"pipeline record ({method}): proposal_generated is not an "
                    f"explicit bool ({_pg!r}) - refusing to guess the proposal "
                    f"stage")
            proposal_stage_reached = _pg
            # STAGE-HONEST ids: present iff their stage actually ran, else honest
            # None - NEVER fabricated. When a proposal WAS generated the cycle_id +
            # proposal_id MUST exist (fail closed); otherwise they are the
            # coordinator's real value or None.
            _cid = getattr(ep, "evidence_cycle_id", None)
            _prid = getattr(ep, "evidence_proposal_id", None)
            if proposal_stage_reached:
                cycle_id = str(_req(_cid, "cycle_id (proposal generated)"))
                proposal_id = str(_req(_prid, "proposal_id (proposal generated)"))
            else:
                cycle_id = str(_cid) if _cid else None
                proposal_id = str(_prid) if _prid else None
            # actuation id: the coordinator's own, present ONLY for a real S3 write
            # attempt (honest None otherwise) - never fabricated.
            actuation_trial_id = ep.evidence_actuation_trial_id
            # P1-6 (item 5): actuation_trial_id <-> real applied-write evidence is
            # BIDIRECTIONAL. RawEvidenceRecord catches an id WITH an empty stream;
            # here we catch the REVERSE - real applied-write evidence (a write
            # attempt with a monotonic timestamp) present but NO actuation id -
            # which the stream builder would otherwise silently DROP. Fail closed.
            if actuation_trial_id is None:
                _applied = getattr(ep, "applied_action", None)
                if isinstance(_applied, (list, tuple)) and any(
                        isinstance(a, dict)
                        and a.get("attempt_monotonic_s") is not None
                        for a in _applied):
                    raise ValueError(
                        f"pipeline record ({method}): applied-write evidence "
                        f"present but NO actuation_trial_id - inconsistent write "
                        f"provenance (refusing to drop a real S3 write)")
            # pending_intent_id keys on the EXPLICIT parse source fact (was the
            # pending intent a parsed Intent?), which MUST be an explicit bool -
            # None FAILS CLOSED (never silently treated as unparsed). A parsed
            # Intent MUST carry its real id (fail closed); an unparsed terminal
            # MUST have None; a parsed-but-idless record fails closed.
            _parsed = getattr(ep, "evidence_was_parsed_intent", None)
            if not isinstance(_parsed, bool):
                raise ValueError(
                    f"pipeline record ({method}): was_parsed_intent is not an "
                    f"explicit bool ({_parsed!r}) - refusing to guess parse state")
            if _parsed:
                pending_intent_id = str(_req(
                    getattr(ep, "evidence_pending_intent_id", None),
                    "pending_intent_id (parsed Intent)"))
            else:
                if getattr(ep, "evidence_pending_intent_id", None) is not None:
                    raise ValueError(
                        f"pipeline record ({method}): an UNPARSED pending intent "
                        f"carries an id - inconsistent parse provenance")
                pending_intent_id = None
            # EXACT action provenance from the evidence/cycle (NOT clip-event
            # derived): raw proposed (requested), the clipped canonical, the
            # enforced canonical, and the device readback. Each is honest None
            # when its stage did not run (no proposal / no write / no readback).
            # preserve EXACT {} vs None (empty recorded action vs no action) - no
            # `or None` collapse.
            proposed = getattr(ep, "evidence_requested_action", None)
            clipped = getattr(ep, "evidence_clipped_action", None)
            enforced = getattr(ep, "evidence_canonical_action", None)
            readback = getattr(ep, "readback_action", None)
            # the ACTUAL pending-Intent type / target / scope (never cfg
            # constants). None on a pre-parse terminal (no Intent).
            intent_type = getattr(ep, "evidence_intent_type", None)
            intent_target = getattr(ep, "evidence_intent_target", None)
            intent_scope = getattr(ep, "evidence_intent_scope", None)
            # strict-schema verdict ONLY when a proposal stage ran (a prompt was
            # generated). It MUST then be an ACTUAL bool from the coordinator - a
            # missing/None/string verdict is a real provenance gap and fails
            # closed, never bool()-coerced into a fabricated False. Before the
            # proposal stage there is no verdict, so it is None.
            if proposal_stage_reached:
                _sv = getattr(ep, "schema_valid", None)
                if not isinstance(_sv, bool):
                    raise ValueError(
                        f"pipeline record ({method}): proposal stage reached but "
                        f"schema_valid is not a real bool ({_sv!r}) - refusing to "
                        f"coerce a schema verdict")
                schema_verdict = _sv
            else:
                # no proposal was generated -> there is NO schema verdict. A
                # non-null schema_valid on such an EpisodeRecord is INCONSISTENT
                # (a verdict without a proposal); REJECT it, never silently discard
                # it by normalizing to None.
                _sv = getattr(ep, "schema_valid", None)
                if _sv is not None:
                    raise ValueError(
                        f"pipeline record ({method}): proposal_generated is False "
                        f"but schema_valid is {_sv!r} (non-null) - a schema verdict "
                        f"without a proposal is inconsistent; refusing to discard "
                        f"it")
                schema_verdict = None
            # explicit source facts for the record invariants (real bools).
            proposal_generated = proposal_stage_reached
            was_parsed_intent = _parsed
        else:
            # SYNTHETIC HARNESS: no coordinator evidence exists; synthesise
            # deterministic ids (the record is tagged non-OTA, excluded from any
            # paper table). A real S3 write happened IFF the trial executed.
            eid = f"{method_block_id}:ep{self._ep_counter}"
            cycle_id = f"{eid}:c{cyc}"
            proposal_id = f"{eid}:c{cyc}:prop"
            actuation_trial_id = (f"{eid}:c{cyc}:act"
                                  if bool(getattr(ep, "trial_executed", False))
                                  else None)
            fsm_step_id = f"{eid}:fsm"
            pending_intent_id = f"{method_block_id}:pi{self._ep_counter}"
            run_id = self.experiment_run_id
            model_id_val = str(getattr(ep, "model_id", model_id) or model_id)
            model_version = model_id_val
            terminal_outcome = self._terminal_outcome(ep)
            terminal_reason = None
            prompt_hash = "prompt-" + hashlib.sha1(
                f"{method}:{ep.phase}:{ep.trial_id}".encode("utf-8")
            ).hexdigest()[:12]
            # synthetic has no real action vectors: the clip events are the only
            # applied-value evidence (non-OTA).
            proposed = {c.axis: c.proposed for c in ep.clips} or None
            clipped = {c.axis: c.applied for c in ep.clips} or None
            enforced = clipped
            readback = getattr(ep, "readback_action", None)
            # synthetic intent is the harness's configured I2 goal, in the SAME
            # dict shape as the real Intent.to_dict() target/scope (non-OTA).
            intent_type = "throughput_goal"
            intent_target = {
                "kpi_name": "throughput", "constraint_type": "min",
                "target_value": self.cfg.throughput_target_mbps, "unit": "Mbps"}
            intent_scope = {
                "ue_ids": list(self.cfg.i2_ue_scope(self.topology.ue_ids())),
                "bs_ids": []}
            schema_verdict = bool(getattr(ep, "schema_valid", False))
            # synthetic always generates a proposal for a parsed intent: a cycle
            # id / proposal id / prompt hash / schema verdict AND a full intent
            # snapshot (type + dict target + dict scope + pending id) are all
            # present above - explicit CONSISTENT facts, not a fabricated stub.
            proposal_generated = True
            was_parsed_intent = True
        # action-event stream from the ACTUAL applied writes (P0-20). Empty for a
        # negotiation-only cycle; non-empty with applied timestamps for an S3
        # write (RawEvidenceRecord validates this invariant).
        action_stream = self._build_action_stream(
            ep, eid, cyc, actuation_trial_id, proposal_id)
        rec = RawEvidenceRecord(
            experiment_run_id=run_id,
            method_block_id=method_block_id,
            fsm_step_id=fsm_step_id,
            episode_id=eid,
            pending_intent_id=pending_intent_id,
            cycle_id=cycle_id,
            proposal_id=proposal_id,
            actuation_trial_id=actuation_trial_id,
            method=method,
            block_id=block_id,
            block_index=block_idx,
            master_seed=self.master_seed,
            block_seed=bseed,
            method_order=list(order),
            method_order_index=order_idx,
            monotonic_seq=self._next_seq(),
            # REAL: the coordinator's finalized EvidenceRecord.proposer_id /
            # model_version (both required, fail-closed above). SYNTHETIC: the
            # method-profile id.
            model_id=model_id_val,
            model_version=model_version,
            prompt_hash=prompt_hash,
            intent_type=intent_type,
            intent_target=intent_target,
            intent_scope=intent_scope,
            raw_confidence=getattr(ep, "raw_confidence", ep.confidence),
            calibrated_probability=getattr(ep, "calibrated_probability", None),
            schema_verdict=schema_verdict,
            proposed_action=proposed,
            clipped_action=clipped,
            enforced_action=enforced,
            readback_action=readback,
            trial_trajectory=list(getattr(ep, "trial_trajectory", []) or []),
            nego_trajectory=list(getattr(ep, "nego_trajectory", []) or []),
            budget_before=getattr(ep, "budget_before", None),
            budget_reserved=getattr(ep, "budget_reserved", None),
            budget_debited=getattr(ep, "budget_debited", None),
            budget_debited_lower_bound=getattr(
                ep, "budget_debited_lower_bound", None),
            budget_debit_status=getattr(ep, "budget_debit_status", None),
            reserve_ledger=getattr(ep, "reserve_ledger", None),
            budget_after=getattr(ep, "budget_after", None),
            terminal_outcome=terminal_outcome,
            terminal_reason=terminal_reason,
            environment_stream=env_stream,
            action_stream=action_stream,
            environment_trace_hash=trace_hash,
            data_origin=self.data_origin.value,
            # hardware provisioning is NEVER inferred from the data_origin enum
            # (that was spoofable). It is proven only by independent
            # PaperGateEvidence supplied to paper_export().
            hardware_state={"origin": self.data_origin.value,
                            "provisioning_proven_by_origin": False,
                            "note": "paper readiness requires independent "
                                    "PaperGateEvidence, not the origin enum"},
            executor_provenance=exec_id,
            topology_label=self.topology.label,
            method_wiring=wiring,
            # explicit source facts (validated in __post_init__): a proposal was
            # generated iff the model returned output; an intent was parsed iff a
            # full snapshot exists. Neither is inferred from another field.
            proposal_generated=proposal_generated,
            was_parsed_intent=was_parsed_intent,
            # phase_idx + phase_name + UNIQUE phase label p{idx}:{name} (repeated
            # names disambiguated by index) + the ACTUAL monotonic episode
            # timestamp (real on the pipeline; the deterministic offline clock on
            # the synthetic harness). phase_idx MUST be a real non-negative int.
            phase_idx=getattr(ep, "phase_idx", None),
            phase_name=str(getattr(ep, "phase", None)),
            phase_label="p{}:{}".format(getattr(ep, "phase_idx", None),
                                        getattr(ep, "phase", None)),
            episode_monotonic_s=(getattr(ep, "episode_monotonic_s", None)
                                 if real else float(self._next_seq())),
        )
        return rec

    # -- action-event stream from actual applied writes ---------------------- #

    @staticmethod
    def _canonical_axis(axis: str) -> str:
        a = str(axis)
        if a == "rfatt" or "power" in a:
            return "power_offset"
        if "prb" in a:
            return "prb"
        if "sched" in a:
            return "sched_priority"
        if "mcs" in a:
            return "mcs_offset"
        return a

    @classmethod
    def _split_clip_axis(cls, label: str) -> Tuple[str, str]:
        parts = str(label).split("_", 1)
        if len(parts) == 2:
            return parts[0], cls._canonical_axis(parts[1])
        return str(label), cls._canonical_axis(label)

    def _build_action_stream(self, ep: EpisodeRecord, eid: str, cyc: int,
                             actuation_trial_id: Optional[str],
                             proposal_id: str) -> List[Dict]:
        """Build the action-event stream from the ACTUAL applied writes of an S3
        trial. Empty for a non-actuation cycle. Each event carries an applied
        (monotonic) timestamp and, when available, the device readback - so an
        actuation record can never claim an empty/timestamp-less action stream.
        A trial that executed but exposes NO applied-write evidence yields an
        empty stream, which RawEvidenceRecord then REJECTS (fail validation)."""
        if actuation_trial_id is None:
            return []
        real = self.data_origin is not DataOrigin.SYNTHETIC_HARNESS
        readback = getattr(ep, "readback_action", None) or {}
        events: List[Dict] = []
        if real:
            # PIPELINE: build STRICTLY from the coordinator's FINALIZED applied-
            # write list. Each entry is one real executor apply ATTEMPT carrying
            # its OWN per-axis attempt monotonic timestamp; the readback is the
            # finalized device readback. NEVER reconstructed from clip events or
            # the executor's current offsets. A pre-attempt failure entry (e.g. an
            # unresolved RNTI, appended before any executor call) has no attempt
            # timestamp and is SKIPPED (it was not a write attempt).
            applied = getattr(ep, "applied_action", None)
            if not isinstance(applied, (list, tuple)):
                return []
            for i, entry in enumerate(applied):
                if not isinstance(entry, dict):
                    continue
                attempt_t = entry.get("attempt_monotonic_s")
                if attempt_t is None:
                    continue                      # not an executor attempt
                # P1-6 (item 5): the apply-attempt monotonic timestamp is REAL
                # monotonic evidence - a non-bool real FINITE value > 0. A bool /
                # string / NaN / inf / 0 / negative (anything that would sanitise
                # to null or is not a real elapsed time) FAILS CLOSED.
                if not (isinstance(attempt_t, (int, float))
                        and not isinstance(attempt_t, bool)
                        and math.isfinite(attempt_t) and attempt_t > 0):
                    raise ValueError(
                        "pipeline S3 applied-write entry's apply-attempt monotonic "
                        f"timestamp must be a real finite value > 0 (got "
                        f"{attempt_t!r}) - refusing fabricated/unusable evidence")
                target = entry.get("ue_id") or entry.get("gnb_id")
                raw_axis = entry.get("axis")
                axis = self._canonical_axis(raw_axis)
                value = entry.get("value")
                # finalized device readback for this exact target+axis (None when
                # not read back) - never fabricated.
                rb = None
                if isinstance(readback, dict):
                    _t = (readback.get(str(target)) or readback.get(target)
                          or readback.get(entry.get("gnb_id")) or {})
                    if isinstance(_t, dict):
                        rb = _t.get(raw_axis, _t.get(axis))
                    elif not isinstance(readback.get(str(target)), dict):
                        rb = readback.get(str(target))
                ev = ActionEvent(
                    event_id=f"{eid}:c{cyc}:w{i}", t_s=0.0, axis=axis,
                    value=float(value), target=str(target),
                    proposal_id=proposal_id,
                    actuation_trial_id=actuation_trial_id,
                    applied_monotonic_s=float(attempt_t))
                d = ev.to_dict()
                d["readback"] = rb
                events.append(d)
            return events
        # SYNTHETIC HARNESS: build the action stream from the ACTUAL applied
        # action (the simulated S3 write) that every trial-executed synthetic
        # episode records - NOT the clip events (an in-bound, non-clipped action
        # has NO clip yet is still a real applied write). Each axis gets a
        # deterministic positive monotone offline clock (non-OTA).
        applied = getattr(ep, "applied_action", None)
        if isinstance(applied, dict) and applied:
            i = 0
            for tgt, axes in applied.items():
                if not isinstance(axes, dict):
                    continue
                for raw_axis, value in axes.items():
                    ev = ActionEvent(
                        event_id=f"{eid}:c{cyc}:w{i}", t_s=0.0,
                        axis=self._canonical_axis(raw_axis),
                        value=float(value), target=str(tgt),
                        proposal_id=proposal_id,
                        actuation_trial_id=actuation_trial_id,
                        applied_monotonic_s=float(self._next_seq()))
                    d = ev.to_dict()
                    d["readback"] = None
                    events.append(d)
                    i += 1
            return events
        # fallback: clip events (a synthetic episode with no recorded applied
        # action). An actuation with NO applied-write evidence still fails the
        # RawEvidenceRecord action-stream contract (fail-closed), as it must.
        for i, c in enumerate(ep.clips):
            tgt, ax = self._split_clip_axis(c.axis)
            ev = ActionEvent(
                event_id=f"{eid}:c{cyc}:w{i}", t_s=0.0, axis=ax,
                value=float(c.applied), target=tgt,
                proposal_id=proposal_id, actuation_trial_id=actuation_trial_id,
                applied_monotonic_s=float(self._next_seq()))
            d = ev.to_dict()
            d["readback"] = None
            events.append(d)
        return events

    @staticmethod
    def _terminal_outcome(ep: EpisodeRecord) -> str:
        if not getattr(ep, "trial_executed", False):
            return "negotiation_only"
        if getattr(ep, "hard_failure", False):
            return "technical_failsafe"
        if getattr(ep, "trial_success", None) is False or \
                getattr(ep, "rolled_back", False):
            return "pending_not_admitted"
        return "commit_original" if getattr(ep, "success", False) \
            else "pending_not_admitted"

    # -- export -------------------------------------------------------------- #

    def paper_export(self, gate: PaperGateEvidence,
                     result: Optional[Dict] = None) -> Dict:
        """Build the paper performance-table input - ONLY when EVERY independent
        paper-run gate is proven AND the origin is live_ota.

        The `data_origin` enum ALONE can never opt a run into the paper table:
        `gate` is REQUIRED and must be a PaperGateEvidence whose every gate
        (hardware / version manifest / topology / readback / scheduler efficacy /
        environment-action disjointness / paired trace / P0 invariants) is
        independently proven. An absent or unverified gate is rejected (fail
        closed); a non-live origin is rejected even with a passing gate."""
        if not isinstance(gate, PaperGateEvidence):
            raise PaperExportError(
                "paper export refused: explicit PaperGateEvidence is required "
                "(the data_origin enum alone cannot prove paper readiness)")
        # (1) gates: EXACT bool True + nonempty per-gate evidence refs.
        unmet = gate.unmet_gates()
        if unmet:
            raise PaperExportError(
                f"paper export refused: unproven/unreferenced gates {unmet} "
                f"(each requires exactly True + a concrete evidence ref)")
        # (2) the EXACT attested run() result MUST be supplied - paper_export
        # NEVER starts a new run (that would export a DIFFERENT run than the gate
        # refs attest, and would implicitly resume live hardware).
        if result is None:
            raise PaperExportError(
                "paper export refused: the exact attested run() result must be "
                "supplied (paper_export never starts a new run)")
        att = getattr(self, "_run_attestation", None)
        if att is None:
            raise PaperExportError(
                "paper export refused: no attested run() on this runner - call "
                "run() and pass its result")
        # (3) verify the supplied result is THIS runner's GENUINE, UNMODIFIED run
        # (mutation / relabel / foreign / deep-copy-then-mutate is rejected).
        claimed = result.get("run_attestation") or {}
        # recompute the tamper-evident digest; a NON-FINITE (NaN/inf) mutation
        # makes this FAIL CLOSED (allow_nan=False raises) - it is a rejection,
        # not an uncaught error, so a None -> NaN tamper cannot slip through.
        try:
            recomputed = self._attestation_digest(result)
        except ValueError as e:
            raise PaperExportError(
                "paper export refused: result carries non-finite content that "
                f"cannot be attested (tamper-evident digest fail-closed): {e}")
        if claimed.get("digest") != att["digest"] \
                or claimed.get("experiment_run_id") != att["experiment_run_id"] \
                or claimed.get("data_origin") != att["data_origin"] \
                or claimed.get("runner_id") != att["runner_id"] \
                or recomputed != att["digest"]:
            raise PaperExportError(
                "paper export refused: result does not match this runner's "
                "attested genuine run (mutated / relabeled / foreign)")
        # (3b) the GATE itself must be BOUND to THIS attested run (its
        # experiment_run_id + run digest), so a gate collected for one run can
        # never admit a different run.
        if gate.attested_experiment_run_id != att["experiment_run_id"] \
                or gate.attested_run_digest != att["digest"]:
            raise PaperExportError(
                "paper export refused: the gate is not bound to this attested "
                "run (attested_experiment_run_id / attested_run_digest mismatch)")
        # (4) origin consistency - the attested run must itself be live_ota.
        origin = DataOrigin.coerce(result["data_origin"])
        if origin is not DataOrigin.LIVE_OTA:
            raise PaperExportError(
                f"paper export refused: attested origin {origin.value} is not "
                f"live_ota (synthetic/emulated are integration_only)")
        # (5) >= 20 blocks AND len(blocks)==n_blocks, with UNIQUE id/index/seed.
        n_blocks = int(result.get("n_blocks", 0))
        blocks = result.get("blocks", [])
        if n_blocks < DEFAULT_PAPER_BLOCKS or len(blocks) != n_blocks:
            raise PaperExportError(
                f"paper export refused: requires len(blocks)==n_blocks>="
                f"{DEFAULT_PAPER_BLOCKS} (n_blocks={n_blocks}, "
                f"len(blocks)={len(blocks)})")
        ids = [b.get("block_id") for b in blocks]
        idxs = [b.get("block_index") for b in blocks]
        seeds = [b.get("block_seed") for b in blocks]
        if len(set(ids)) != n_blocks or len(set(idxs)) != n_blocks \
                or len(set(seeds)) != n_blocks:
            raise PaperExportError(
                "paper export refused: blocks are not unique (block_id / index / "
                "seed) - not independent")
        # (6) methods EXACTLY the required set; independence verified.
        if set(result.get("methods", [])) != set(REQUIRED_METHODS):
            raise PaperExportError(
                f"paper export refused: methods must be EXACTLY "
                f"{sorted(REQUIRED_METHODS)}, got {result.get('methods')}")
        if result.get("required_methods_present") is not True \
                or result.get("block_independence_verified") is not True:
            raise PaperExportError(
                "paper export refused: required methods / block independence "
                "not verified")
        # (7) per-block: paired reset + all methods + independence + exact method
        # keys + trace hash.
        for b in blocks:
            if b.get("paired_reset_verified") is not True:
                raise PaperExportError(
                    f"paper export refused: block {b.get('block_id')!r} paired "
                    f"reset unverified")
            if b.get("all_methods_present") is not True \
                    or b.get("block_independent") is not True:
                raise PaperExportError(
                    f"paper export refused: block {b.get('block_id')!r} missing "
                    f"a method or not independent")
            nrec = b.get("n_records_by_method") or {}
            if set(nrec.keys()) != set(REQUIRED_METHODS):
                raise PaperExportError(
                    f"paper export refused: block {b.get('block_id')!r} method "
                    f"keys are not EXACTLY the required set")
            if not all(isinstance(nrec.get(m), int)
                       and not isinstance(nrec.get(m), bool)
                       and nrec.get(m) > 0
                       for m in REQUIRED_METHODS):
                raise PaperExportError(
                    f"paper export refused: block {b.get('block_id')!r} has a "
                    f"method with zero/invalid record count (not executed)")
            if not str(b.get("environment_trace_hash", "") or "").strip():
                raise PaperExportError(
                    f"paper export refused: block {b.get('block_id')!r} has no "
                    f"environment trace hash (paired trace)")
        # (8) exactly ONE shared NON-NULL executor.
        if result.get("executor_shared") is not True \
                or len(result.get("shared_executor_ids", [])) != 1:
            raise PaperExportError(
                "paper export refused: requires exactly one shared non-null "
                f"executor (got {result.get('shared_executor_ids')})")
        admitted = [r for r in result["records"]
                    if r.get("data_origin") == DataOrigin.LIVE_OTA.value]
        if not admitted:
            raise PaperExportError(
                "paper export refused: no live_ota records to admit")
        payload = {"data_origin": origin.value, "gate": gate.to_dict(),
                   "n_blocks": n_blocks, "n_admitted": len(admitted),
                   "records": admitted,
                   "shared_executor_ids": result["shared_executor_ids"],
                   # P1-6: ONLY this successful-export payload (all gates +
                   # attestation passed) is a paper PERFORMANCE artifact. The raw
                   # run() result stays integration_only/excluded regardless of
                   # origin, so nothing bypasses the gates into the paper table.
                   "paper_eligibility": "paper_performance",
                   "excluded_from_paper": False,
                   "paper_ready": True}
        assert_finite_json(payload)
        return payload

    def save(self, result: Dict, path: str) -> str:
        """Persist a run result as STRICT finite JSON (no NaN/Infinity)."""
        with open(path, "w") as f:
            f.write(dumps_finite(result, indent=2))
        return path
