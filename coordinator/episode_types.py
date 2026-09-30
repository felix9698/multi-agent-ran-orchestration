#!/usr/bin/env python3
"""
Core terminal-outcome, monitor-verdict, and evidence types for a coordination
episode.

CLI handoff Batch A ("Core types and finalization", section 12): the smallest
coherent set of core types + a single finalization path + a legacy adapter,
covering the definitional parts of sections 3 / P0-4 / P0-6:

  * Section 3.1 - an episode terminates in EXACTLY ONE of four terminal
    outcomes.  Free-form labels ("accept", "reject", None) are NOT the
    external contract; they are produced only by the legacy adapter here.
  * Section 3.2 - a 3-valued monitor verdict (SATISFIED / VIOLATED / UNKNOWN)
    that is the AUTHORITATIVE evaluator result.
  * P0-4 - ``TerminalOutcome`` / ``TerminalReason`` enums with a total
    allowed-reasons-by-outcome contract; ``success`` derived from the
    outcome; a single finalization funnel that produces a ``TerminalRecord``
    carrying the pending intent and the committed revision.
  * P0-6 - an immutable ``EvidenceRecord`` binding the identifier/provenance
    chain to (best-effort in Batch A) the action, snapshot, observations and
    terminal decision.

Batch A is deliberately additive and does NOT strengthen process-flow
semantics: transaction/latch (Batch B) and schema/deadlines/commit-invariant
enforcement (Batch C) will consume these types later.  Nothing here reaches
the RAN, opens a socket, or touches hardware.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# Identifier chain (section P0-6 / P1-6)
# ---------------------------------------------------------------------------

def new_id(prefix: str) -> str:
    """A fresh, collision-resistant identifier for one link of the evidence
    chain (``run``, ``episode``, ``cycle``, ``proposal``, ``trial`` ...).

    uuid4 is used rather than a monotonic counter so identifiers are unique
    across processes/hosts and cannot be confused with array indices (P1-6:
    ``trial_id``, ``step_idx`` and ``cycle_id`` are distinct concepts).
    """
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# 3-valued monitor verdict (section 3.2)
# ---------------------------------------------------------------------------

class MonitorVerdict(Enum):
    """Exactly the three verdicts an intent evaluator may return.

    Missing / stale telemetry, a detached UE, a scope mismatch, or an
    unparseable KPI is UNKNOWN - never silently promoted to SATISFIED
    (section 3.2).  This is the authoritative evaluator result; the coordinator
    keeps ``bool``/``str`` forms only as thin, named legacy adapters.
    """
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNKNOWN = "unknown"

    @classmethod
    def from_optional_bool(cls, value: Optional[bool]) -> "MonitorVerdict":
        """None (no measurement) -> UNKNOWN; a real boolean -> SATISFIED /
        VIOLATED.  So an absent measurement can never read as satisfied."""
        if value is None:
            return cls.UNKNOWN
        return cls.SATISFIED if value else cls.VIOLATED

    @property
    def is_satisfied(self) -> bool:
        return self is MonitorVerdict.SATISFIED


# ---------------------------------------------------------------------------
# Safety-transaction state + typed restore / recovery verdicts (Batch B:
# P0-1 / P0-2 / P0-3)
# ---------------------------------------------------------------------------

class SafetyState(Enum):
    """The typed lifecycle of the post-actuation safety transaction (P0-1).

    READY               - no actuation in flight; ordinary writes permitted.
    ACTUATING           - inside a first-write-through-commit transaction.
    RESTORING           - a rollback is running (writes bypass the executor
                          latch only through the restore path).
    LATCHED_FAILSAFE    - a restore could NOT be verified.  A LATCH: the
                          coordinator AND the executor refuse every ordinary
                          write, new episode, cycle, negotiation and revision
                          until an explicit operator recovery API verifies
                          readback against the recovery/baseline target.
    """
    READY = "ready"
    ACTUATING = "actuating"
    RESTORING = "restoring"
    LATCHED_FAILSAFE = "latched_failsafe"


class ConfigRestoreVerdict(Enum):
    """Whether the CONFIGURATION rollback was verified against the snapshot
    (P0-3).  Kept SEPARATE from physical service recovery: a verified restore
    of the pre-trial config says nothing about whether the UE/service came
    back under it.  UNKNOWN is never success (section 3.2)."""
    VERIFIED = "verified"       # device read-back matched the snapshot
    FAILED = "failed"           # restore ran but read-back did NOT match
    UNKNOWN = "unknown"         # nothing to verify (simulation / no snapshot)

    @classmethod
    def from_restore_bool(cls, value: Optional[bool]) -> "ConfigRestoreVerdict":
        """True -> VERIFIED, False -> FAILED, None -> UNKNOWN.  ``_rollback``
        returns None only in simulation / with no device snapshot, where there
        was no real actuation to leave in an unsafe state; on the real device
        path it returns True/False, so "not VERIFIED" == "not True" there."""
        if value is None:
            return cls.UNKNOWN
        return cls.VERIFIED if value else cls.FAILED


class PhysicalRecoveryVerdict(Enum):
    """Whether physical service RECOVERED under the RESTORED configuration
    (P0-3), observed only AFTER the rollback.  UNKNOWN is never success."""
    RECOVERED = "recovered"
    NOT_RECOVERED = "not_recovered"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RollbackOutcome:
    """Typed result of one rollback transaction (P0-1 step 3 / P0-3).

    ``config_restore_verdict`` and ``physical_recovery_verdict`` are recorded
    SEPARATELY; the exposure timestamps bound how long a harmful action was
    applied (first applied write -> rollback start -> restore completion).
    Physical-recovery fields stay UNKNOWN/None until a post-restore recovery
    check runs.  ``restore_verified`` mirrors the legacy bool (True/False/None)
    the pre-Batch-B code carried on the cycle record."""
    config_restore_verdict: ConfigRestoreVerdict
    physical_recovery_verdict: PhysicalRecoveryVerdict = (
        PhysicalRecoveryVerdict.UNKNOWN)
    restore_verified: Optional[bool] = None
    latched: bool = False
    # exposure / timing (monotonic seconds; None when not measured)
    first_write_time: Optional[float] = None
    rollback_start_time: Optional[float] = None
    restore_complete_time: Optional[float] = None
    tau_exposure_to_rollback: Optional[float] = None
    tau_exposure_to_restore: Optional[float] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "config_restore_verdict": self.config_restore_verdict.value,
            "physical_recovery_verdict": self.physical_recovery_verdict.value,
            "restore_verified": self.restore_verified,
            "latched": self.latched,
            "first_write_time": self.first_write_time,
            "rollback_start_time": self.rollback_start_time,
            "restore_complete_time": self.restore_complete_time,
            "tau_exposure_to_rollback": self.tau_exposure_to_rollback,
            "tau_exposure_to_restore": self.tau_exposure_to_restore,
            "error": self.error,
        }


@dataclass
class ActuationTransaction:
    """Shared, MUTABLE context of one post-actuation transaction (Gate-review
    items 1/2/3).

    It is the single source of truth for "did a REAL device write happen" -
    updated by the actuator itself immediately AFTER the first successful
    executor write (not before, not on simulation, not only once _execute_trial
    returns), so a mid-write exception that never returns still records that a
    write occurred and the finally rolls back exactly once.

    ``actual_real_write_performed`` gates the latch policy: an UNKNOWN restore
    after a real write is UNVERIFIED and latches, while a simulation / no-write
    UNKNOWN does not.  ``commit_verified`` is only set AFTER mandatory evidence
    finalization + admission succeed, so an evidence/finalizer failure after a
    write rolls back and leaves nothing admitted.
    """
    actual_real_write_performed: bool = False
    first_write_time: Optional[float] = None
    rollback_done: bool = False
    commit_verified: bool = False
    pending_commit: bool = False
    simulation: bool = False
    snapshot: Dict = field(default_factory=dict)
    snapshot_readback_time: Optional[float] = None
    requested_action: Dict = field(default_factory=dict)
    applied: Tuple = ()
    failed_axis: Optional[str] = None
    observed_readback: Optional[Dict] = None
    restore_error: Optional[str] = None
    rollback_result: Optional[Dict] = None
    # Deferred-commit settlement (kept OPEN through the episode-cleanup finally):
    # the outer process_intent admits + marks verified ONLY after a still-commit
    # record survives the S0/history cleanup. These hold the actual objects the
    # settlement needs (the serialized result dict cannot re-admit an Intent).
    pending_intent: Any = None
    committed_intent: Any = None
    commit_outcome: Any = None
    commit_reason: Any = None
    # Batch C (P0-6): canonical action bound BEFORE the write + its stable hash,
    # the authorization artifact, the action-apply time, the final device
    # read-back, and the S4 observations - the atomic commit re-check reads
    # these at settlement.  monitor_verdicts/joint_satisfied carry the S4 joint
    # satisfaction so the commit gate does not re-run measurement.
    canonical_action: Dict = field(default_factory=dict)
    canonical_action_hash: str = ""
    # The authorized, enforcement-approved action VECTOR the executor consumes
    # (tuples of (gnb_id, axis, clipped_value, ue_id)); the write path applies
    # THIS, never re-reading the raw proposal after authorization (P0-6).
    authorized_vector: Tuple = ()
    clip_events: Tuple = ()
    # minted ONCE at the pre-write point (after verified snapshot capture, right
    # before the first actual nonempty executor write); the authorization is
    # rebound to carry it so authorization + evidence name the SAME write attempt.
    actuation_trial_id: Optional[str] = None
    # The canonical of what the executor ACTUALLY applied (recorded by the write
    # path); the commit gate recomputes the hash from THIS and compares it to
    # the authorization hash, rather than trusting a cached hash string.
    applied_canonical: Optional[Dict] = None
    authorization: Any = None
    action_apply_time: Optional[float] = None
    # Batch G (P1-6/P0-19): a DISTINCT monotonic timestamp captured at the SAME
    # write boundary as action_apply_time (which stays epoch). The raw action
    # stream uses THIS monotonic value; epoch and monotonic are kept separate so
    # neither domain is silently substituted for the other.
    action_apply_monotonic_s: Optional[float] = None
    final_readback: Optional[Dict] = None
    final_readback_time: Optional[float] = None
    observations: Tuple = ()
    monitor_verdicts: Dict = field(default_factory=dict)
    # TRUSTED provenance bound BEFORE the write / at authorization (coordinator
    # review #5): the post-final-gate publication reads ONLY these already-bound
    # primitives, never re-calling an external llm_manager / re-reading a mutable
    # result, so no post-gate external code can reopen the readback/deadline gap.
    proposer_id: str = ""
    model_version: str = ""
    experiment_run_id: str = ""
    episode_id: str = ""
    fsm_step_id: str = ""
    pending_intent_hash: str = ""
    # Batch G (P1-6): the REAL prompt hash of this cycle's proposal, bound before
    # the write so the finalized EvidenceRecord carries it as trusted provenance.
    # None until a proposal prompt was generated; a real S3 write requires it.
    prompt_hash: Optional[str] = None
    # the EXACT system-owned monitored+pending intent IDs the cycle must satisfy
    # (bound from active_intents + current_intent, independent of what a
    # validation self-reports).
    required_intent_ids: Tuple = ()
    joint_satisfied: bool = False
    commit_check: Optional[Dict] = None
    # P0-8 (review bug 2): the re-entry / assurance trigger context, captured
    # as a DEEP COPY when the transaction is created (before authorization/
    # write). It is the ONLY trusted provenance the authoritative commit
    # reflection uses - a forged result/cycle trigger field cannot survive.
    # None/{} for a direct (non-re-entry) operator episode.
    trigger_context: Optional[Dict] = None
    # Batch D (blocker 8): the trusted calibration provenance (raw/calibrated/
    # threshold/model/regime/reason/cold-start), deep-copied at tx creation.
    calibration: Optional[Dict] = None

    def note_real_write(self, ts: float, axis: Optional[str] = None) -> None:
        """Record the FIRST successful real executor write. ``ts`` is the epoch
        apply time; a DISTINCT monotonic timestamp is captured at the SAME
        boundary for the raw action stream (epoch stays epoch)."""
        if not self.actual_real_write_performed:
            self.actual_real_write_performed = True
            self.first_write_time = ts
            # the action-apply time the commit freshness check compares against
            self.action_apply_time = ts
            # Batch G: the monotonic apply timestamp for the raw action stream.
            # ONLY set if the write loop did not already stamp it from the FIRST
            # apply ATTEMPT (the attempt timestamp is authoritative; this is the
            # fallback for callers that do not stamp an attempt time).
            if self.action_apply_monotonic_s is None:
                self.action_apply_monotonic_s = time.monotonic()
        if axis is not None:
            self.failed_axis = None   # a success clears any prior failed axis


@dataclass(frozen=True)
class RecoveryClearanceResult:
    """Typed result of an operator recovery-clearance attempt (P0-1 step 6).

    A latch is only released when ``verdict`` is VERIFIED - i.e. the current
    device readback matched the recovery/baseline target.  UNKNOWN / FAILED
    leave the latch engaged (UNKNOWN is never success)."""
    cleared: bool
    verdict: ConfigRestoreVerdict
    detail: str = ""

    def to_dict(self) -> Dict:
        return {
            "cleared": self.cleared,
            "verdict": self.verdict.value,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Terminal outcome and reason (section 3.1 / P0-4)
# ---------------------------------------------------------------------------

class TerminalOutcome(Enum):
    """The four - and only four - ways an episode may terminate (3.1)."""
    COMMIT_ORIGINAL = "commit_original"
    COMMIT_REVISED = "commit_revised"
    PENDING_NOT_ADMITTED = "pending_not_admitted"
    TECHNICAL_FAILSAFE = "technical_failsafe"

    @property
    def is_commit(self) -> bool:
        """``success`` is derived from this (P0-4): only a verified commit of
        the original or a revised intent counts as success."""
        return self in (TerminalOutcome.COMMIT_ORIGINAL,
                        TerminalOutcome.COMMIT_REVISED)


class TerminalReason(Enum):
    """Why an episode reached its terminal outcome (the P0-4 decision-table
    vocabulary), grouped by the outcome each reason belongs to."""
    # --- commit outcomes -------------------------------------------------
    COMMIT_VERIFIED = "commit_verified"        # valid for BOTH commit outcomes

    # --- pending-not-admitted (admission refusals) -----------------------
    SINGLE_FLIGHT_REJECTED = "single_flight_rejected"
    INPUT_SCHEMA_REJECTED = "input_schema_rejected"
    PARSE_FAILED = "parse_failed"
    UNSUPPORTED_INTENT = "unsupported_intent"
    LOW_CONFIDENCE = "low_confidence"
    INFEASIBLE = "infeasible"
    NO_ACCEPTABLE_ALTERNATIVE = "no_acceptable_alternative"
    UNMATERIALIZABLE_AGREEMENT = "unmaterializable_agreement"
    TRIAL_NOT_VALIDATED = "trial_not_validated"
    BUDGET_EXHAUSTED = "budget_exhausted"
    # Batch D (P0-10): the runtime reserve ledger refused a transition because
    # its deterministic worst-case reservation would exceed C_episode. Split so
    # audit names WHERE the budget ran out: pre-actuation (no write happened) vs
    # before a negotiation callback.
    INSUFFICIENT_TRIAL_BUDGET = "insufficient_trial_budget"
    INSUFFICIENT_NEGOTIATION_BUDGET = "insufficient_negotiation_budget"
    NEGOTIATION_TERMINATED = "negotiation_terminated"
    # Batch C (P0-7): an ABSOLUTE episode deadline / a bounded external-call
    # timeout was hit BEFORE any actuation (or after a verified rollback), so
    # the pending intent is simply not admitted (never a commit).
    DEADLINE_EXHAUSTED = "deadline_exhausted"
    # Batch C (P0-6): the atomic S6 commit re-check failed (hash / read-back /
    # freshness / authorization / intent-set / joint satisfaction) but the trial
    # was rolled back and the restore VERIFIED - nothing unverified was
    # committed, so the pending intent is not admitted.
    COMMIT_REVERIFICATION_FAILED = "commit_reverification_failed"
    # already-satisfied is UNVERIFIED (no fresh joint-satisfaction + commit/
    # no-op evidence contract yet -> Batch A fails closed, admits nothing)
    ALREADY_SATISFIED_UNVERIFIED = "already_satisfied_unverified"

    # --- technical failsafe ----------------------------------------------
    ILLEGAL_TRANSITION = "illegal_transition"
    UNKNOWN_SYSTEM_EVENT = "unknown_system_event"
    ROLLBACK_FAILED = "rollback_failed"
    INTERNAL_ERROR = "internal_error"
    # Batch B (P0-1/P0-2/P0-3): the safety latch is engaged (a prior episode's
    # rollback could not be verified) - every new episode/cycle/write is
    # refused until an operator recovery API verifies readback against the
    # recovery/baseline target.
    SAFETY_LATCHED = "safety_latched"
    # Batch C (P0-7): a post-actuation bounded-call timeout whose rollback could
    # not be verified fails closed to TechnicalFailsafe (distinct from the
    # generic RESTORE_UNVERIFIED so an audit names a timeout specifically).
    TIMEOUT_UNRECOVERED = "timeout_unrecovered"
    # Batch B (P0-1/P0-3): a post-write configuration restore could NOT be
    # verified (config_restore_verdict FAILED/UNKNOWN after a real write), so
    # the episode fails closed and latches.  Distinct from ROLLBACK_FAILED
    # (used by earlier fail-closed paths) so audit records name WHICH invariant
    # broke.
    RESTORE_UNVERIFIED = "restore_unverified"
    # Batch B (P0-3, Gate-review item 4): the CONFIG was restored+verified but
    # physical service did NOT recover under it.  These distinguish a confirmed
    # non-recovery from an unobservable one - never collapsed into a benign
    # negotiation outcome.
    PHYSICAL_RECOVERY_FAILED = "physical_recovery_failed"
    PHYSICAL_RECOVERY_UNKNOWN = "physical_recovery_unknown"


# Total allowed-reasons-by-outcome contract (P0-4).  COMMIT_VERIFIED is valid
# for BOTH commit outcomes, so a one-to-one reason->outcome map is wrong; this
# is the authoritative pairing rule and every outcome has a non-empty set.
ALLOWED_REASONS: Dict[TerminalOutcome, "frozenset[TerminalReason]"] = {
    TerminalOutcome.COMMIT_ORIGINAL: frozenset({
        TerminalReason.COMMIT_VERIFIED,
    }),
    TerminalOutcome.COMMIT_REVISED: frozenset({
        TerminalReason.COMMIT_VERIFIED,
    }),
    TerminalOutcome.PENDING_NOT_ADMITTED: frozenset({
        TerminalReason.SINGLE_FLIGHT_REJECTED,
        TerminalReason.INPUT_SCHEMA_REJECTED,
        TerminalReason.PARSE_FAILED,
        TerminalReason.UNSUPPORTED_INTENT,
        TerminalReason.LOW_CONFIDENCE,
        TerminalReason.INFEASIBLE,
        TerminalReason.NO_ACCEPTABLE_ALTERNATIVE,
        TerminalReason.UNMATERIALIZABLE_AGREEMENT,
        TerminalReason.TRIAL_NOT_VALIDATED,
        TerminalReason.BUDGET_EXHAUSTED,
        TerminalReason.INSUFFICIENT_TRIAL_BUDGET,
        TerminalReason.INSUFFICIENT_NEGOTIATION_BUDGET,
        TerminalReason.NEGOTIATION_TERMINATED,
        TerminalReason.ALREADY_SATISFIED_UNVERIFIED,
        TerminalReason.DEADLINE_EXHAUSTED,
        TerminalReason.COMMIT_REVERIFICATION_FAILED,
    }),
    TerminalOutcome.TECHNICAL_FAILSAFE: frozenset({
        TerminalReason.ILLEGAL_TRANSITION,
        TerminalReason.UNKNOWN_SYSTEM_EVENT,
        TerminalReason.ROLLBACK_FAILED,
        TerminalReason.INTERNAL_ERROR,
        TerminalReason.SAFETY_LATCHED,
        TerminalReason.RESTORE_UNVERIFIED,
        TerminalReason.TIMEOUT_UNRECOVERED,
        TerminalReason.PHYSICAL_RECOVERY_FAILED,
        TerminalReason.PHYSICAL_RECOVERY_UNKNOWN,
    }),
}


def is_valid_outcome_reason(outcome: TerminalOutcome,
                            reason: TerminalReason) -> bool:
    """True iff ``reason`` is allowed for ``outcome`` (P0-4 pairing rule)."""
    return reason in ALLOWED_REASONS.get(outcome, frozenset())


def validate_outcome_reason(outcome: TerminalOutcome,
                            reason: TerminalReason) -> None:
    """Raise ValueError if the (outcome, reason) pairing is not allowed, so a
    mismatched terminal decision can never be emitted (P0-4)."""
    if not is_valid_outcome_reason(outcome, reason):
        raise ValueError(
            f"terminal reason {reason.value!r} is not allowed for outcome "
            f"{outcome.value!r} (P0-4 allowed-reasons contract)")


# ---------------------------------------------------------------------------
# Legacy adapter (P0-4 step 4 / 3.1)
# ---------------------------------------------------------------------------
#
# The four terminal outcomes are the contract; these two functions are the
# ONLY place the historical "success"/"resolution" labels are produced, so a
# legacy consumer (GUI, main.py, runner fallback, existing tests) keeps
# working unchanged.  The richer classification always lives alongside in
# ``terminal_outcome`` / ``terminal_reason``.

def legacy_success(outcome: TerminalOutcome) -> bool:
    """P0-4: ``success`` is True only for the two commit outcomes."""
    return outcome.is_commit


def legacy_resolution(outcome: TerminalOutcome,
                      reason: Optional[TerminalReason]) -> str:
    """Map a terminal outcome back onto the historical ``resolution`` string.

    Preserves every label existing tests pin:
      * COMMIT_ORIGINAL          -> "accept"
      * COMMIT_REVISED           -> "accept_modified"
      * PENDING_NOT_ADMITTED     -> "reject"
      * TECHNICAL_FAILSAFE       -> "reject"  (nuance in terminal_outcome)
    """
    if outcome is TerminalOutcome.COMMIT_ORIGINAL:
        return "accept"
    if outcome is TerminalOutcome.COMMIT_REVISED:
        return "accept_modified"
    return "reject"


# ---------------------------------------------------------------------------
# Observation provenance (section 3.2 / P0-6)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Observation:
    """One KPI observation with the provenance later batches need.

    ``sample_time`` is when the measurement was taken; ``collection_start``/
    ``end`` bound the collection window; ``freshness_verdict`` records the
    collector's own freshness judgment (``fresh`` / ``stale`` / ``unknown``).
    Batch A only defines the shape; the freshness/post-action commit checks
    that consume it are Batch C.
    """
    source: str
    value: Optional[float]
    sample_time: float
    collection_start: float
    collection_end: float
    freshness_verdict: str = "unknown"
    # P0-9 (review): an OPTIONAL structured per-source bundle carried alongside
    # the scalar ``value`` - e.g. the exact deep-copied per-UE metrics
    # (UEMetrics.to_dict()) an already-satisfied shortcut observed. None for the
    # legacy scalar observations, so existing callers are unaffected.
    details: Optional[Dict] = None

    def __post_init__(self):
        # DEEP-FREEZE details into a fresh, deeply-immutable structure (review
        # bug 1): a frozen dataclass only blocks attribute rebinding, so a
        # mutable ``details`` dict would remain writable (obs.details['x']=...)
        # and, worse, would ALIAS the caller's original (a later source mutation
        # would change the recorded observation / evidence). Rebuilding it as a
        # deep-frozen mapping severs the alias and makes nested writes raise.
        # ``None`` / scalar details are left untouched (legacy shape).
        if self.details is not None:
            object.__setattr__(self, "details", _deep_freeze(self.details))

    def to_dict(self) -> Dict:
        return {
            "source": self.source,
            "value": self.value,
            "sample_time": self.sample_time,
            "collection_start": self.collection_start,
            "collection_end": self.collection_end,
            "freshness_verdict": self.freshness_verdict,
            # deeply THAW to an INDEPENDENT mutable copy, so a caller mutating
            # the returned dict cannot feed back into the frozen observation.
            "details": (_thaw(self.details)
                        if self.details is not None else None),
        }


# ---------------------------------------------------------------------------
# Immutable evidence record (P0-6)
# ---------------------------------------------------------------------------

# The identifier + provenance links the record may carry (P0-6).
EVIDENCE_IDENTIFIER_FIELDS: Tuple[str, ...] = (
    "experiment_run_id", "episode_id", "fsm_step_id", "evidence_record_id",
    "cycle_id", "proposal_id", "actuation_trial_id",
)

# Links that must be present (non-empty) for the chain to be complete.  The
# stage-dependent ids (cycle_id / proposal_id / actuation_trial_id) are NOT
# required: per P1-6 they exist only once their stage actually began, and it
# would be dishonest to mint them for an episode that never ran a cycle.
# evidence_record_id IS required (Batch E): every authoritative record has its
# own stable identity, preallocated at episode start.
_EVIDENCE_REQUIRED_FIELDS: Tuple[str, ...] = (
    "experiment_run_id", "episode_id", "fsm_step_id", "evidence_record_id",
    "proposer_id", "model_version", "intent_set_version", "pending_intent_hash",
)


def _deep_freeze(value: Any) -> Any:
    """Recursively convert a value into a deeply-immutable representation.

    dict/MappingProxyType -> a MappingProxyType over a FRESH dict whose values
    are themselves deep-frozen (so no MappingProxyType is ever backed by an
    externally-reachable map); list/tuple -> a tuple of deep-frozen elements;
    set/frozenset -> a frozenset of deep-frozen elements; scalars (and frozen
    dataclasses like Observation) are returned unchanged. Every container is
    rebuilt, so mutating the caller's original input after construction cannot
    affect the record, and nested mutation (ev.a['b']['c']=..., ev.obs[0][...]
    .append(...)) raises."""
    if isinstance(value, (MappingProxyType, dict)):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    """Recursively convert a deep-frozen value back into ordinary, mutable,
    JSON-safe dict/list values (MonitorVerdict -> its string, Observation ->
    its dict, frozenset -> list)."""
    if isinstance(value, MonitorVerdict):
        return value.value
    if isinstance(value, (MappingProxyType, dict)):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_thaw(v) for v in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):          # e.g. Observation
        return _thaw(to_dict())
    return value


@dataclass(frozen=True)
class EvidenceRecord:
    """Immutable binding of the identifier/provenance chain to the action,
    snapshot, observations and terminal decision of one episode (P0-6).

    ``frozen=True`` blocks attribute rebinding; ``__post_init__`` additionally
    deep-freezes the dict/sequence fields (mapping proxies + tuples) so the
    record is immutable through its containers too.  Batch A populates the
    identifier chain, provenance, and terminal fields for every episode; the
    action / snapshot / observation fields are declared here (the P0-6 shape)
    and are fully populated by the later measurement and commit batches.
    """
    # --- identifier + provenance chain (required core) -------------------
    experiment_run_id: str
    episode_id: str
    fsm_step_id: str
    proposer_id: str
    model_version: str
    intent_set_version: str
    pending_intent_hash: str

    # Batch E (P0-15, coordinator review): the EXPLICIT, stable evidence-record
    # identity of THIS finalized evidence bundle. REQUIRED + non-empty on every
    # authoritative record (full / minimal / failure). Minted ONCE per episode
    # (in _begin_episode, preallocated) and reused, so it is distinct from
    # episode_id / actuation_trial_id (provenance links, not the record's own
    # identity). History records reference ONLY this id - never an alias.
    evidence_record_id: str

    # Batch G (P1-6): the REAL prompt hash of the proposal this evidence binds
    # (None when no proposal was generated for the terminal). Bound provenance;
    # a real actuation cannot be finalized without it (enforced at the write).
    # Placed AFTER the required core so no non-default field follows a default.
    prompt_hash: Optional[str] = None

    # --- stage-dependent identifiers (honest None until the stage runs) ---
    cycle_id: Optional[str] = None
    proposal_id: Optional[str] = None
    actuation_trial_id: Optional[str] = None
    authorized_revision_id: Optional[str] = None

    # --- action provenance (populated by later batches) ------------------
    requested_action: Dict = field(default_factory=dict)
    clipped_action: Dict = field(default_factory=dict)
    canonical_action: Dict = field(default_factory=dict)
    canonical_action_hash: str = ""

    # --- Batch D (P0-10/P0-17): risk budget + confidence calibration -----
    # The routing threshold is applied to calibrated_probability, NOT the raw
    # score (threshold_applied_to states which). reserve_ledger is the per-
    # episode worst-case reservation audit.
    raw_confidence: Optional[float] = None
    calibrated_probability: Optional[float] = None
    threshold: Optional[float] = None
    threshold_applied_to: Optional[str] = None
    calibration_context: Optional[Dict] = None
    cold_start_source: Optional[Dict] = None
    reserve_ledger: Optional[Dict] = None

    # --- Batch F (P0-13/P0-14): live-measurement provenance ---------------
    # The PRE_ACTION baseline MeasurementSample (value-or-UNKNOWN + provenance),
    # the probe configuration (offered load / protocol / direction / mode), and
    # the post-action IN_WINDOW measurement samples used for validation. Carried
    # so measurement source/provenance survives result -> cycle -> EvidenceRecord
    # -> raw export.
    pre_action_baseline: Optional[Dict] = None
    probe_config: Optional[Dict] = None
    measurement_samples: Tuple = ()

    # --- snapshot / readback (populated by later batches) ----------------
    snapshot: Dict = field(default_factory=dict)
    snapshot_readback_time: Optional[float] = None
    action_apply_time: Optional[float] = None           # EPOCH apply time
    # Batch G (P1-6/P0-19): the DISTINCT monotonic apply timestamp captured at the
    # first apply ATTEMPT boundary (epoch action_apply_time stays epoch). Bound so
    # the authoritative evidence carries the same domain the raw action stream uses.
    action_apply_monotonic_s: Optional[float] = None
    final_readback: Optional[Dict] = None
    final_readback_time: Optional[float] = None

    # --- observations + verdicts (populated by later batches) ------------
    observations: Tuple = ()
    monitor_verdicts: Dict = field(default_factory=dict)
    rollback_result: Optional[Dict] = None

    # --- Batch C provenance (P0-5 / P0-6): authorization artifact, strict
    # schema verdict, and the atomic commit-invariant verdict.  Optional +
    # deep-frozen like the other containers, so Batch A immutability is not
    # weakened.
    authorization: Optional[Dict] = None
    schema_valid: Optional[bool] = None
    schema_reject_reason: Optional[str] = None
    commit_check: Optional[Dict] = None

    # --- auditing --------------------------------------------------------
    error: Optional[str] = None            # set on minimal fail-closed bundles
    # P0-8: re-entry / assurance provenance (re-entry reason, originating
    # intent-set version, originating evidence id). None for a direct operator
    # episode; populated when the episode was driven by resolve_pending_intent
    # from the continuous-assurance scheduler.
    trigger_context: Optional[Dict] = None

    # --- terminal decision -----------------------------------------------
    terminal_outcome: Optional[str] = None
    terminal_reason: Optional[str] = None

    def __post_init__(self):
        # RECURSIVELY deep-freeze every nested container (object.__setattr__
        # because the dataclass is frozen), so no nested mapping/sequence/set
        # remains mutable and no proxy is backed by a caller-reachable map.
        for name in ("requested_action", "clipped_action", "canonical_action",
                     "snapshot", "monitor_verdicts", "observations",
                     "measurement_samples"):
            object.__setattr__(self, name, _deep_freeze(getattr(self, name)))
        for name in ("final_readback", "rollback_result", "authorization",
                     "commit_check", "trigger_context",
                     "calibration_context", "cold_start_source",
                     "reserve_ledger", "pre_action_baseline", "probe_config"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, _deep_freeze(getattr(self, name)))
        # Batch D (blocker 8): validate the calibration audit fields. When
        # present, raw/calibrated/threshold must be finite numbers in [0,1] and
        # threshold_applied_to must be exactly 'calibrated_probability'.
        for name in ("raw_confidence", "calibrated_probability", "threshold"):
            v = getattr(self, name)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not math.isfinite(float(v)) or float(v) < 0.0 \
                    or float(v) > 1.0:
                raise ValueError(f"EvidenceRecord.{name} must be a finite number "
                                 f"in [0,1], got {v!r}")
        if self.threshold_applied_to is not None \
                and self.threshold_applied_to != "calibrated_probability":
            raise ValueError("EvidenceRecord.threshold_applied_to must be "
                             "'calibrated_probability' when present, got "
                             f"{self.threshold_applied_to!r}")
        # Batch E: the evidence-record identity is REQUIRED and NON-EMPTY on
        # every authoritative record (JSON-safe, non-bool string). Production
        # never emits an empty/unknown id (it is preallocated in _begin_episode).
        if isinstance(self.evidence_record_id, bool) \
                or not isinstance(self.evidence_record_id, str) \
                or not self.evidence_record_id:
            raise ValueError("EvidenceRecord.evidence_record_id must be a "
                             f"non-empty string, got {self.evidence_record_id!r}")

    def identifier_chain(self) -> Dict[str, Optional[str]]:
        """The identifier links only (for provenance auditing)."""
        return {f: getattr(self, f) for f in EVIDENCE_IDENTIFIER_FIELDS}

    def has_complete_identifier_chain(self) -> bool:
        """True iff every required core identifier/provenance link is present
        and non-empty.  Stage-dependent ids are honestly None when their stage
        did not run and are not required here (P1-6)."""
        return all(getattr(self, f) for f in _EVIDENCE_REQUIRED_FIELDS)

    def to_dict(self) -> Dict:
        """Recursively THAW to ordinary, mutable, JSON-safe dict/list values
        (deep-frozen proxies -> dicts, tuples -> lists, Observation -> dict,
        MonitorVerdict -> its string)."""
        return {
            "experiment_run_id": self.experiment_run_id,
            "episode_id": self.episode_id,
            "fsm_step_id": self.fsm_step_id,
            "evidence_record_id": self.evidence_record_id,
            "proposer_id": self.proposer_id,
            "model_version": self.model_version,
            "intent_set_version": self.intent_set_version,
            "pending_intent_hash": self.pending_intent_hash,
            "prompt_hash": self.prompt_hash,
            "cycle_id": self.cycle_id,
            "proposal_id": self.proposal_id,
            "actuation_trial_id": self.actuation_trial_id,
            "authorized_revision_id": self.authorized_revision_id,
            "requested_action": _thaw(self.requested_action),
            "clipped_action": _thaw(self.clipped_action),
            "canonical_action": _thaw(self.canonical_action),
            "canonical_action_hash": self.canonical_action_hash,
            "snapshot": _thaw(self.snapshot),
            "snapshot_readback_time": self.snapshot_readback_time,
            "action_apply_time": self.action_apply_time,
            "action_apply_monotonic_s": self.action_apply_monotonic_s,
            "final_readback": (_thaw(self.final_readback)
                               if self.final_readback is not None else None),
            "final_readback_time": self.final_readback_time,
            "observations": _thaw(self.observations),
            "monitor_verdicts": _thaw(self.monitor_verdicts),
            "rollback_result": (_thaw(self.rollback_result)
                                if self.rollback_result is not None else None),
            "authorization": (_thaw(self.authorization)
                              if self.authorization is not None else None),
            "schema_valid": self.schema_valid,
            "schema_reject_reason": self.schema_reject_reason,
            "commit_check": (_thaw(self.commit_check)
                             if self.commit_check is not None else None),
            "error": self.error,
            "trigger_context": (_thaw(self.trigger_context)
                                if self.trigger_context is not None else None),
            # Batch D provenance
            "raw_confidence": self.raw_confidence,
            "calibrated_probability": self.calibrated_probability,
            "threshold": self.threshold,
            "threshold_applied_to": self.threshold_applied_to,
            "calibration_context": (_thaw(self.calibration_context)
                                    if self.calibration_context is not None
                                    else None),
            "cold_start_source": (_thaw(self.cold_start_source)
                                  if self.cold_start_source is not None
                                  else None),
            "reserve_ledger": (_thaw(self.reserve_ledger)
                               if self.reserve_ledger is not None else None),
            # Batch F: live-measurement provenance (survives to raw export).
            "pre_action_baseline": (_thaw(self.pre_action_baseline)
                                    if self.pre_action_baseline is not None
                                    else None),
            "probe_config": (_thaw(self.probe_config)
                             if self.probe_config is not None else None),
            "measurement_samples": _thaw(self.measurement_samples),
            "terminal_outcome": self.terminal_outcome,
            "terminal_reason": self.terminal_reason,
        }


# ---------------------------------------------------------------------------
# Terminal record bundle (P0-4 step 3)
# ---------------------------------------------------------------------------

def _intent_payload(value: Any) -> Any:
    """Serialize an Intent (``.to_dict()``) or a raw pending text/None."""
    if value is None:
        return None
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:
            return str(value)
    return value


@dataclass(frozen=True)
class TerminalRecord:
    """What ``finalize_episode`` produces (P0-4): exactly one terminal outcome,
    one reason (a validated allowed pair), a MANDATORY evidence bundle whose own
    terminal fields match the pair, the pending intent (preserved even on
    non-admission), and the committed intent revision.

    Both commit outcomes MUST carry a non-None committed_revision (CommitOriginal
    carries the original executed intent as revision 0; CommitRevised carries the
    exact executed revised intent); every non-commit outcome MUST carry None.
    """
    outcome: TerminalOutcome
    reason: TerminalReason
    evidence: Optional[EvidenceRecord] = None
    pending_intent: Any = None
    committed_revision: Any = None

    def __post_init__(self):
        # a mismatched (outcome, reason) can never be emitted (P0-4)
        validate_outcome_reason(self.outcome, self.reason)
        # committed_revision is required for BOTH commit outcomes and forbidden
        # for non-commit outcomes (a commit must name what it committed)
        if self.outcome.is_commit:
            if self.committed_revision is None:
                raise ValueError(
                    f"{self.outcome.value} requires a non-None "
                    f"committed_revision (the executed intent)")
        elif self.committed_revision is not None:
            raise ValueError(
                "committed_revision must be None for a non-commit outcome")
        # evidence is mandatory and must not contradict the terminal decision
        # (D): an inconsistent terminal/evidence record cannot be emitted
        if self.evidence is None:
            raise ValueError("TerminalRecord requires an evidence bundle")
        ev_out = self.evidence.terminal_outcome
        ev_rea = self.evidence.terminal_reason
        if ev_out is not None and ev_out != self.outcome.value:
            raise ValueError(
                f"evidence.terminal_outcome {ev_out!r} != {self.outcome.value!r}")
        if ev_rea is not None and ev_rea != self.reason.value:
            raise ValueError(
                f"evidence.terminal_reason {ev_rea!r} != {self.reason.value!r}")

    @property
    def success(self) -> bool:
        return legacy_success(self.outcome)

    @property
    def resolution(self) -> str:
        return legacy_resolution(self.outcome, self.reason)

    def to_dict(self) -> Dict:
        return {
            "terminal_outcome": self.outcome.value,
            "terminal_reason": self.reason.value,
            "success": self.success,
            "resolution": self.resolution,
            "evidence": (self.evidence.to_dict()
                         if self.evidence is not None else None),
            "pending_intent": _intent_payload(self.pending_intent),
            "committed_revision": _intent_payload(self.committed_revision),
        }


# ---------------------------------------------------------------------------
# Batch D (P0-10): runtime reserve ledger
# ---------------------------------------------------------------------------

class ReserveLedger:
    """Per-episode MONOTONIC worst-case reserve ledger (P0-10).

    Reserves a DETERMINISTIC upper bound before every state transition that can
    spend budget (the initial trial, each negotiation callback, each revised
    trial) and refuses the transition when the cumulative worst-case reservation
    would exceed ``C_episode``. It is the authoritative budget guard - N_max is
    only an extra guard on top.

    Every attempted reservation (accepted OR rejected) is recorded with its
    component breakdown, the cap, the cumulative reserved amount, and - as the
    trial/negotiation actually completes - the separately MEASURED actual cost,
    so the ledger stays auditable and consistent even on exception/timeout
    terminals (the reservation is recorded before the risky call).

    Cost caps are Mbps*s. A trial's upper bound is the deterministic continuous
    KPI-loss cap PLUS the hard-failure cap, combined ONCE (no double counting).
    """

    _EPS = 1e-9

    # settlement statuses (blocker 5). SETTLED means EVERY required cost
    # component was measured; PARTIAL_UNKNOWN means some (e.g. an unmeasured
    # hard-failure reconnection cost) is missing so the TOTAL is incomplete -
    # a measured lower bound is kept but never counted as the total.
    SETTLED = "settled"
    PARTIAL_UNKNOWN = "partial_unknown"
    UNKNOWN = "unknown"        # callback exception / timeout - actual unmeasured

    @staticmethod
    def _finite_nonneg(name, v):
        """Ledger follow-up: caps/amounts must be finite nonnegative numbers -
        a NaN/Inf/negative is REJECTED (raise), never silently clamped."""
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or not math.isfinite(float(v)) or float(v) < 0.0:
            raise ValueError(f"ReserveLedger {name} must be finite and >= 0, "
                             f"got {v!r}")
        return float(v)

    def __init__(self, c_episode, c_trial_cont_ub, c_hard_ub, c_nego_ub,
                 caps_audit=None):
        self.c_episode = self._finite_nonneg("c_episode", c_episode)
        self.c_trial_cont_ub = self._finite_nonneg("c_trial_cont_ub",
                                                   c_trial_cont_ub)
        self.c_hard_ub = self._finite_nonneg("c_hard_ub", c_hard_ub)
        self.c_nego_ub = self._finite_nonneg("c_nego_ub", c_nego_ub)
        # combined per-trial upper bound (continuous + hard, no double count)
        self.c_trial_ub = self.c_trial_cont_ub + self.c_hard_ub
        self.reserved = 0.0        # cumulative worst-case reserved
        self.actual = 0.0          # cumulative separately-measured actual cost
        self.entries = []          # every attempted reservation (accepted/rejected)
        self.settlements = {}      # reservation_id -> settlement record
        self.caps_audit = dict(caps_audit or {})   # deterministic cap provenance
        self._seq = 0
        self.last_reservation_id = None
        self.any_cap_breach = False

    def _now(self):
        return time.monotonic()

    def _reserve(self, kind, amount, components, cycle_index=None,
                 cycle_id=None):
        self._seq += 1
        rid = f"rsv-{self._seq}-{uuid.uuid4().hex[:8]}"
        amount = max(0.0, float(amount))
        would = self.reserved + amount
        accepted = would <= self.c_episode + self._EPS
        # a SINGLE reservation that alone exceeds the whole episode cap is a cap
        # breach (the transition can never be afforded); flag it explicitly.
        cap_breach = amount > self.c_episode + self._EPS
        if cap_breach:
            self.any_cap_breach = True
        rec = {
            "reservation_id": rid,
            "kind": kind,                       # transition kind
            "amount": amount,
            "components": dict(components),
            "cap": self.c_episode,
            "timestamp": self._now(),           # monotonic
            "cycle_index": cycle_index,
            "cycle_id": cycle_id,
            "reserved_before": self.reserved,
            "accepted": bool(accepted),
            # cumulative reserved AFTER this attempt (unchanged if rejected)
            "cumulative_reserved": (would if accepted else self.reserved),
            "cap_breach": bool(cap_breach),
            "settled": False,
        }
        if accepted:
            self.reserved = would
        self.entries.append(rec)
        self.last_reservation_id = rid
        return accepted

    def can_reserve_trial(self):
        """True iff a trial reservation would fit WITHOUT committing it."""
        return (self.reserved + self.c_trial_ub) <= self.c_episode + self._EPS

    def reserve_trial(self, kind="trial", cycle_index=None, cycle_id=None):
        """Reserve one trial's worst case (continuous + hard). Returns False and
        records a rejected entry when it would exceed the cap. ``last_reservation
        _id`` links the accepted reservation to its later settlement."""
        return self._reserve(kind, self.c_trial_ub, {
            "continuous": self.c_trial_cont_ub,
            "hard_failure": self.c_hard_ub,
        }, cycle_index, cycle_id)

    def reserve_negotiation(self, cycle_index=None, cycle_id=None):
        """Reserve one negotiation callback's worst case. Returns False and
        records a rejected entry when it would exceed the cap."""
        return self._reserve("negotiation", self.c_nego_ub, {
            "c_nego": self.c_nego_ub,
        }, cycle_index, cycle_id)

    def _entry_for(self, reservation_id):
        for e in self.entries:
            if e.get("reservation_id") == reservation_id:
                return e
        return None

    def settle(self, reservation_id, status, actual=None, method=None,
               components=None, error=None, elapsed=None,
               actual_lower_bound=None):
        """Record the TERMINAL settlement of a reservation, linked by id
        (blocker 5 / ledger follow-up). Accepts ONLY an existing ACCEPTED
        reservation, EXACTLY ONCE - a duplicate, unknown, or rejected-reservation
        settle raises ValueError (never double-counts ``actual``). Status/value
        invariants are enforced:
          * SETTLED         - a FINITE nonnegative ``actual`` (every component
                              measured).  actual > reserved UB is preserved but
                              flagged ``actual_exceeds_reservation``.
          * PARTIAL_UNKNOWN - ``actual`` is None + a FINITE ``actual_lower_bound``
                              (an incomplete total).
          * UNKNOWN         - ``actual`` is None (nothing measured).
        Malformed amounts (NaN/Inf/negative) are REJECTED, never clamped."""
        entry = self._entry_for(reservation_id)
        if entry is None:
            raise ValueError(f"settle: unknown reservation_id {reservation_id!r}")
        if not entry.get("accepted"):
            raise ValueError(f"settle: reservation {reservation_id!r} was "
                             f"REJECTED (cannot settle a non-reservation)")
        if entry.get("settled"):
            raise ValueError(f"settle: reservation {reservation_id!r} already "
                             f"settled (exactly-once)")
        if status not in (self.SETTLED, self.PARTIAL_UNKNOWN, self.UNKNOWN):
            raise ValueError(f"settle: invalid status {status!r}")
        # status / value invariants
        breach = False
        if status == self.SETTLED:
            a = self._finite_nonneg("settle actual", actual)
            lb = None
            if a > entry["amount"] + self._EPS:
                breach = True     # measured actual exceeded its reserved UB
        elif status == self.PARTIAL_UNKNOWN:
            if actual is not None:
                raise ValueError("PARTIAL_UNKNOWN requires actual=None")
            if actual_lower_bound is None:
                raise ValueError("PARTIAL_UNKNOWN requires a finite lower bound")
            a = None
            lb = self._finite_nonneg("settle actual_lower_bound",
                                     actual_lower_bound)
        else:  # UNKNOWN
            if actual is not None:
                raise ValueError("UNKNOWN requires actual=None")
            a, lb = None, None
        rec = {
            "reservation_id": reservation_id,
            "status": status,
            "actual": a,
            "actual_lower_bound": lb,
            "method": method,
            "components": dict(components or {}),
            "error": error,
            "elapsed": elapsed,
            "actual_exceeds_reservation": breach,
            "timestamp": self._now(),
        }
        if breach:
            self.any_cap_breach = True
        self.settlements[reservation_id] = rec
        entry["settled"] = True
        entry["settlement_status"] = status
        # ONLY a fully SETTLED actual counts toward the measured total.
        if a is not None:
            self.actual += a
        return rec

    def unsettled_accepted_ids(self):
        """Accepted reservations that were never settled (for a consistency
        check: every accepted reservation should end with a settlement)."""
        return [e["reservation_id"] for e in self.entries
                if e["accepted"] and not e.get("settled")]

    def to_dict(self):
        return {
            "c_episode": self.c_episode,
            "c_trial_ub": self.c_trial_ub,
            "c_trial_cont_ub": self.c_trial_cont_ub,
            "c_hard_ub": self.c_hard_ub,
            "c_nego_ub": self.c_nego_ub,
            "reserved": self.reserved,
            "actual": self.actual,
            "remaining": self.c_episode - self.reserved,
            "any_cap_breach": self.any_cap_breach,
            "caps_audit": dict(self.caps_audit),
            "entries": [dict(e) for e in self.entries],
            "settlements": {k: dict(v) for k, v in self.settlements.items()},
        }
