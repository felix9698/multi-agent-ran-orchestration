#!/usr/bin/env python3
"""
Canonical-action binding and the atomic commit-invariant re-check
(CLI handoff Batch C, remaining P0-6 / section 3.3).

Before the first write the authorized action is CANONICALISED and bound to a
stable hash plus an authorization artifact (revision id, intent-set version,
identifiers, an expiry).  The executor receives ONLY that canonical action.
Immediately before settlement the whole bundle is re-checked ATOMICALLY
(section 3.3): a single mid-S4 read-back is NOT sufficient - the commit is
allowed only if, at commit time,

    safety not latched
    and canonical_action_hash == authorized_action_hash
    and final device read-back == canonical applied action
    and every validation observation was produced AFTER action-apply and is fresh
    and authorization is still valid (not expired)
    and intent_set_version is unchanged
    and every scoped monitored + pending intent is SATISFIED
    and no rollback / recovery obligation remains open

Any mismatch BLOCKS the commit and hands control to the safe transaction path
(roll back; PendingNotAdmitted on a verified restore, TechnicalFailsafe
otherwise).  This module is stdlib-only and pure (no I/O); the coordinator
supplies the observed values.  Nothing here reaches the RAN.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Iterable, Optional, Tuple


def content_hash(obj: Any, prefix: str = "ic-") -> str:
    """Deterministic content hash of a JSON-serializable object (stable full
    semantic content), used to bind/re-check the exact authorized intent
    revision CONTENT and the monitored intent-set content (P0-6)."""
    payload = json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Canonical action + stable hash
# ---------------------------------------------------------------------------

def canonical_action_key(gnb_id: Optional[str], axis: str,
                         ue_id: Optional[str]) -> str:
    """Stable, addressable key for one applied axis: ``gnb.<ue|cell>.axis``.

    The per-UE/per-cell distinction is preserved so a per-UE override and a
    cell-wide value on the same axis never collide."""
    scope = ue_id if ue_id else "cell"
    return f"{gnb_id}.{scope}.{axis}"


def canonicalize_action(applied_actions: Iterable[Tuple]) -> Dict[str, float]:
    """Canonical ``{key: float}`` mapping from a list of applied axes.

    ``applied_actions`` items are ``(gnb_id, axis, value, ue_id)`` (the clipped,
    enforcement-approved action the executor will actually receive).  The
    mapping is order-independent (a dict keyed by :func:`canonical_action_key`)
    so the hash does not depend on proposal iteration order."""
    out: Dict[str, float] = {}
    for gnb_id, axis, value, ue_id in applied_actions:
        out[canonical_action_key(gnb_id, axis, ue_id)] = float(value)
    return out


def stable_action_hash(canonical: Dict[str, float]) -> str:
    """Deterministic content hash of a canonical action.  Values are rounded to
    a fixed precision before hashing so float formatting jitter cannot change
    the hash for an unchanged action."""
    norm = {k: round(float(v), 9) for k, v in sorted(canonical.items())}
    payload = json.dumps(norm, sort_keys=True, separators=(",", ":"))
    return "act-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def actions_equal(a: Dict[str, float], b: Dict[str, float],
                  *, abs_tol: float = 1e-6) -> bool:
    """True iff two canonical actions have the same keys and numerically-equal
    FINITE-NUMBER values (used for final read-back == canonical applied action).

    Hardened (coordinator review): a value that is NOT a finite JSON number
    (str / bool / NaN / +-Infinity / wrong type) is NEVER coerced - it makes the
    two actions UNEQUAL, so a string '2.0' can never match a numeric 2.0 and a
    bool True can never collide with 1.0."""
    if set(a) != set(b):
        return False
    for k in a:
        av = _finite_num(a[k])
        bv = _finite_num(b[k])
        if av is None or bv is None:
            return False
        if not math.isclose(av, bv, rel_tol=1e-9, abs_tol=abs_tol):
            return False
    return True


# ---------------------------------------------------------------------------
# Authorization artifact (bound BEFORE the write)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommitAuthorization:
    """Immutable authorization bound to a canonical action before the first
    write (P0-6 / 3.3).

    Carries the stable action hash, the authorized revision id, the intent-set
    version AT authorization time, the identifier chain, and an expiry.  The
    executor is given only ``canonical_action``; settlement re-checks every
    field of this artifact atomically."""
    canonical_action: Dict[str, float]
    canonical_action_hash: str
    authorized_revision_id: Optional[str]
    intent_set_version: str
    issued_time: float
    expiry_time: float
    # content hash of the EXACT authorized intent revision (not just its id): a
    # swapped or in-place-mutated committed intent is caught at settlement.
    authorized_intent_hash: str = ""
    episode_id: Optional[str] = None
    cycle_id: Optional[str] = None
    proposal_id: Optional[str] = None
    actuation_trial_id: Optional[str] = None

    def __post_init__(self):
        # deeply immutable / safely copied canonical payload (P0-6): a caller
        # cannot mutate the authorization's action after binding.
        object.__setattr__(
            self, "canonical_action",
            MappingProxyType({k: float(v)
                              for k, v in dict(self.canonical_action).items()}))

    def is_valid(self, now: float) -> bool:
        """True iff the authorization has not expired at ``now`` (same clock as
        ``issued_time``/``expiry_time``)."""
        return now <= self.expiry_time

    def matches_hash(self, other_hash: str) -> bool:
        return bool(other_hash) and other_hash == self.canonical_action_hash

    def to_dict(self) -> Dict[str, Any]:
        return {
            "canonical_action": dict(self.canonical_action),
            "canonical_action_hash": self.canonical_action_hash,
            "authorized_revision_id": self.authorized_revision_id,
            "authorized_intent_hash": self.authorized_intent_hash,
            "intent_set_version": self.intent_set_version,
            "issued_time": self.issued_time,
            "expiry_time": self.expiry_time,
            "episode_id": self.episode_id,
            "cycle_id": self.cycle_id,
            "proposal_id": self.proposal_id,
            "actuation_trial_id": self.actuation_trial_id,
        }


def bind_authorization(canonical: Dict[str, float], *,
                       authorized_revision_id: Optional[str],
                       intent_set_version: str,
                       now: float, ttl_s: float,
                       authorized_intent_hash: str = "",
                       episode_id: Optional[str] = None,
                       cycle_id: Optional[str] = None,
                       proposal_id: Optional[str] = None,
                       actuation_trial_id: Optional[str] = None
                       ) -> CommitAuthorization:
    """Bind a canonical action to a fresh :class:`CommitAuthorization`.

    ``ttl_s`` is how long the authorization stays valid (bounded by the caller
    to the remaining episode time, so it can never outlive the episode).  The
    canonical payload is deep-copied so a later mutation of the caller's dict
    cannot change the bound authorization."""
    return CommitAuthorization(
        canonical_action=copy.deepcopy(dict(canonical)),
        canonical_action_hash=stable_action_hash(canonical),
        authorized_revision_id=authorized_revision_id,
        intent_set_version=intent_set_version,
        issued_time=float(now),
        expiry_time=float(now) + float(ttl_s),
        authorized_intent_hash=authorized_intent_hash,
        episode_id=episode_id, cycle_id=cycle_id,
        proposal_id=proposal_id, actuation_trial_id=actuation_trial_id)


# ---------------------------------------------------------------------------
# Observation freshness (post-action + within the freshness bound)
# ---------------------------------------------------------------------------

def observation_is_post_action(sample_time: Optional[float],
                               action_apply_time: Optional[float]) -> bool:
    """True iff a KPI sample was produced AT OR AFTER the action apply time
    (P0-6: a pre-action sample can never be commit evidence)."""
    if sample_time is None or action_apply_time is None:
        return False
    return float(sample_time) >= float(action_apply_time)


def observation_is_fresh(sample_time: Optional[float], now: float,
                         max_age_s: float) -> bool:
    """True iff a KPI sample is within the freshness bound at commit time."""
    if sample_time is None:
        return False
    return (float(now) - float(sample_time)) <= float(max_age_s)


# ---------------------------------------------------------------------------
# Atomic commit-invariant verdict
# ---------------------------------------------------------------------------

# Machine-readable failure codes (stored in the evidence schema/commit verdict).
COMMIT_OK = "ok"
COMMIT_SAFETY_LATCHED = "safety_latched"
COMMIT_ROLLBACK_OBLIGATION = "rollback_obligation_open"
COMMIT_HASH_MISMATCH = "action_hash_mismatch"
COMMIT_AUTH_EXPIRED = "authorization_expired"
COMMIT_INTENT_SET_CHANGED = "intent_set_version_changed"
COMMIT_INTENT_CONTENT_CHANGED = "authorized_intent_content_changed"
COMMIT_REVISION_ID_MISMATCH = "authorized_revision_id_mismatch"
COMMIT_MISSING_BINDING = "missing_authorization_binding"
COMMIT_IDENTIFIER_CHAIN = "authorization_identifier_chain_mismatch"
COMMIT_NOT_JOINTLY_SATISFIED = "not_jointly_satisfied"
COMMIT_MISSING_VERDICT = "missing_monitor_verdict"
COMMIT_VERDICT_NOT_SATISFIED = "monitor_verdict_not_satisfied"
COMMIT_VERDICT_INCONSISTENT = "joint_verdict_boolean_inconsistent"
COMMIT_READBACK_MISMATCH = "readback_mismatch"


def _verdict_is_satisfied(v: Any) -> bool:
    """True iff a per-intent monitor verdict is EXACTLY SATISFIED - ONLY the
    exact string ``'satisfied'`` OR the actual ``MonitorVerdict.SATISFIED`` enum
    instance (coordinator review #5 item 4).

    A ``getattr(v, 'value', v)`` check would fail-OPEN: any FAKE object /
    other Enum / namedtuple with ``.value == 'satisfied'`` would slip through.
    So this rejects arbitrary objects, other Enums, bools, numbers, and
    containers - only the two exact forms count."""
    if isinstance(v, bool):          # bool is an int subclass - reject explicitly
        return False
    if isinstance(v, str):
        return v == "satisfied"
    # accept ONLY the real MonitorVerdict.SATISFIED enum member (by identity).
    try:
        from coordinator.episode_types import MonitorVerdict
    except Exception:
        return False
    return v is MonitorVerdict.SATISFIED
COMMIT_PRE_ACTION_OBSERVATION = "pre_action_observation"
COMMIT_STALE_OBSERVATION = "stale_observation"
COMMIT_FUTURE_OBSERVATION = "future_observation"
COMMIT_MISSING_OBSERVATION = "missing_post_action_observation"
COMMIT_INCOMPLETE_OBSERVATION = "incomplete_observation_provenance"


def _finite_num(x: Any) -> Optional[float]:
    """Return ``float(x)`` iff ``x`` is a FINITE JSON number (not bool/str/
    NaN/Infinity); otherwise None.  Used so the commit gate can REJECT (not
    throw) on a wrong-typed / non-finite provenance field."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    fx = float(x)
    return fx if math.isfinite(fx) else None


@dataclass(frozen=True)
class CommitVerdict:
    """Result of the atomic commit-invariant re-check.  ``ok`` gates the
    commit; ``code``/``detail`` name the FIRST failing invariant for the audit
    record (P0-6)."""
    ok: bool
    code: str = COMMIT_OK
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "code": self.code, "detail": self.detail}


@dataclass
class CommitContext:
    """Everything the atomic re-check observes at commit time.  The coordinator
    fills this from the transaction, a FRESH device read-back, and the S4
    observations; :func:`verify_commit_invariant` is pure over it."""
    authorization: Optional[CommitAuthorization]
    applied_action_hash: str = ""
    now: float = 0.0
    intent_set_version_now: str = ""
    # content hash AND id of the intent actually being committed NOW (compared
    # to the authorization: a swap - same content, different id - OR an in-place
    # mutation - same id, different content - is caught).
    committed_intent_hash_now: str = ""
    committed_revision_id_now: Optional[str] = None
    # the CURRENT identifier chain of the write attempt; for a real write it
    # must exactly match the authorization's ids (episode/cycle/proposal/trial).
    current_identifier_chain: Optional[Dict[str, Any]] = None
    # the EXACT system-owned set of monitored+pending intent IDs this cycle must
    # satisfy, and the TYPED per-intent verdicts from S4.  joint_satisfied is
    # only a derived/cache boolean and CANNOT override the typed verdicts.
    required_intent_ids: Tuple[Any, ...] = ()
    monitor_verdicts: Dict[str, Any] = field(default_factory=dict)
    joint_satisfied: bool = False
    safety_latched: bool = False
    rollback_obligation_open: bool = False
    # device read-back == canonical applied action (only checked for a real write)
    readback_required: bool = False
    final_readback_action: Optional[Dict[str, float]] = None
    # every validation observation must carry full provenance + be post-action
    # + fresh (real writes).  Each item is a dict with keys: source, sample_time,
    # collection_start, collection_end, freshness_verdict.
    observations_required: bool = False
    action_apply_time: Optional[float] = None
    freshness_max_age_s: float = 30.0
    observations: Tuple[Dict[str, Any], ...] = ()


def verify_commit_invariant(ctx: CommitContext) -> CommitVerdict:
    """The atomic S6 commit gate (section 3.3 / P0-6).

    Re-checks the whole bundle in a fixed order and returns the FIRST failure,
    so an audit names exactly which invariant blocked the commit.  Any non-OK
    verdict must be routed to the safe transaction path by the caller."""
    auth = ctx.authorization
    if auth is None:
        return CommitVerdict(False, COMMIT_MISSING_BINDING,
                             "no authorization bound before the write")
    # 0. the authorization MUST carry its required binding fields (a missing
    # revision id / intent-content hash / action hash cannot be bypassed).
    if not auth.authorized_revision_id or not auth.authorized_intent_hash \
            or not auth.canonical_action_hash:
        return CommitVerdict(
            False, COMMIT_MISSING_BINDING,
            "authorization missing a required binding field "
            f"(revision_id={auth.authorized_revision_id!r} "
            f"intent_hash={auth.authorized_intent_hash!r} "
            f"action_hash={auth.canonical_action_hash!r})")
    # 1. safety not latched
    if ctx.safety_latched:
        return CommitVerdict(False, COMMIT_SAFETY_LATCHED,
                             "safety latch engaged at commit")
    # 2. no open rollback / recovery obligation
    if ctx.rollback_obligation_open:
        return CommitVerdict(False, COMMIT_ROLLBACK_OBLIGATION,
                             "an open rollback/recovery obligation remains")
    # 3. canonical action hash == authorized action hash
    if not auth.matches_hash(ctx.applied_action_hash):
        return CommitVerdict(
            False, COMMIT_HASH_MISMATCH,
            f"applied hash {ctx.applied_action_hash!r} != authorized "
            f"{auth.canonical_action_hash!r}")
    # 4. authorization still valid (not expired)
    if not auth.is_valid(ctx.now):
        return CommitVerdict(
            False, COMMIT_AUTH_EXPIRED,
            f"authorization expired (now {ctx.now} > {auth.expiry_time})")
    # 5. intent-set version unchanged
    if ctx.intent_set_version_now != auth.intent_set_version:
        return CommitVerdict(
            False, COMMIT_INTENT_SET_CHANGED,
            f"intent-set version changed {auth.intent_set_version!r} -> "
            f"{ctx.intent_set_version_now!r}")
    # 5b. the EXACT authorized intent revision id is unchanged (a SWAPPED
    # object - identical content but a different id - is caught).
    if ctx.committed_revision_id_now != auth.authorized_revision_id:
        return CommitVerdict(
            False, COMMIT_REVISION_ID_MISMATCH,
            f"authorized revision id changed {auth.authorized_revision_id!r} "
            f"-> {ctx.committed_revision_id_now!r}")
    # 5c. the EXACT authorized intent revision CONTENT is unchanged (an
    # in-place-mutated committed intent is caught, not just its id).
    if ctx.committed_intent_hash_now != auth.authorized_intent_hash:
        return CommitVerdict(
            False, COMMIT_INTENT_CONTENT_CHANGED,
            f"authorized intent content changed "
            f"{auth.authorized_intent_hash!r} -> {ctx.committed_intent_hash_now!r}")
    # 5d. for a REAL write the authorization identifier chain must be complete
    # AND exactly match the current write attempt's chain (episode/cycle/
    # proposal/trial) so authorization and evidence name the SAME attempt.
    if ctx.readback_required:
        chain = ctx.current_identifier_chain or {}
        for field in ("episode_id", "cycle_id", "proposal_id",
                      "actuation_trial_id"):
            av = getattr(auth, field, None)
            cv = chain.get(field)
            if not av or not cv or av != cv:
                return CommitVerdict(
                    False, COMMIT_IDENTIFIER_CHAIN,
                    f"authorization {field}={av!r} != current {cv!r}")
    # 6. joint monitored + pending satisfaction, from the TYPED per-intent
    # verdicts over the EXACT system-owned required intent-ID set (coordinator
    # review).  The cached all_satisfied boolean CANNOT override the typed
    # verdicts: an empty/missing/violated/unknown/wrong-typed verdict blocks
    # regardless of joint_satisfied=True.
    required = set(ctx.required_intent_ids or ())
    verdicts = ctx.monitor_verdicts if isinstance(ctx.monitor_verdicts, dict) \
        else None
    if verdicts is None:
        return CommitVerdict(False, COMMIT_MISSING_VERDICT,
                             "monitor_verdicts is not a mapping")
    if not required:
        # there is ALWAYS at least the pending intent - an empty required set is
        # itself an inconsistency (no system-owned intent set was bound).
        return CommitVerdict(False, COMMIT_MISSING_VERDICT,
                             "no system-owned required intent-ID set bound")
    # full coverage: every required intent MUST have a verdict (no omissions)
    missing = [i for i in required if i not in verdicts]
    if missing:
        return CommitVerdict(
            False, COMMIT_MISSING_VERDICT,
            f"required intents missing a monitor verdict: {missing}")
    # every verdict present (required or extra) must be EXACTLY satisfied - a
    # single violated/unknown/wrong-typed verdict blocks the commit
    for iid, v in verdicts.items():
        if not _verdict_is_satisfied(v):
            return CommitVerdict(
                False, COMMIT_VERDICT_NOT_SATISFIED,
                f"intent {iid!r} verdict is not SATISFIED: {v!r}")
    # converse consistency: the cached boolean must AGREE with the typed
    # verdicts (all satisfied) - a disagreement is itself an inconsistency.
    if not ctx.joint_satisfied:
        return CommitVerdict(
            False, COMMIT_VERDICT_INCONSISTENT,
            "joint_satisfied cache is False while typed verdicts are all "
            "SATISFIED (inconsistent)")
    # 7. final device read-back == canonical applied action (real writes only)
    if ctx.readback_required:
        rb = ctx.final_readback_action
        if rb is None:
            return CommitVerdict(False, COMMIT_READBACK_MISMATCH,
                                 "no final device read-back captured")
        if not actions_equal(rb, auth.canonical_action):
            return CommitVerdict(
                False, COMMIT_READBACK_MISMATCH,
                f"final read-back {rb} != canonical {auth.canonical_action}")
    # 8. a real write MUST carry at least one validation observation, and EVERY
    # observation must be post-action AND fresh (P0-6).  joint satisfaction is
    # NOT a substitute for evidence existence: a validator/callback that returns
    # all_satisfied with zero observations cannot commit.  A freshness verdict
    # that is anything other than the explicit "fresh" (stale / unknown /
    # invalid / missing) fails closed.
    if ctx.observations_required:
        if not ctx.observations:
            return CommitVerdict(False, COMMIT_MISSING_OBSERVATION,
                                 "a real write has zero post-action "
                                 "observations to validate the commit")
        apply_t = _finite_num(ctx.action_apply_time)
        if apply_t is None:
            return CommitVerdict(False, COMMIT_INCOMPLETE_OBSERVATION,
                                 "action_apply_time is not a finite number")
        for obs in ctx.observations:
            if not isinstance(obs, dict):
                return CommitVerdict(False, COMMIT_INCOMPLETE_OBSERVATION,
                                     f"observation is not an object: {obs!r}")
            source = obs.get("source")
            st = _finite_num(obs.get("sample_time"))
            cs = _finite_num(obs.get("collection_start"))
            ce = _finite_num(obs.get("collection_end"))
            fresh = obs.get("freshness_verdict")
            # EXACT types: nonempty STRING source; finite numeric sample/start/
            # end.  A wrong type / NaN / Infinity is a NON-OK verdict (the gate
            # never throws, coordinator review).
            if not (isinstance(source, str) and source) \
                    or st is None or cs is None or ce is None:
                return CommitVerdict(
                    False, COMMIT_INCOMPLETE_OBSERVATION,
                    f"observation provenance incomplete/typed-wrong: {obs!r}")
            # window must start AT/AFTER action-apply, be ordered, and contain
            # the sample.
            if cs < apply_t:
                return CommitVerdict(
                    False, COMMIT_PRE_ACTION_OBSERVATION,
                    f"observation {source!r} collection_start {cs} precedes "
                    f"action-apply {apply_t}")
            if ce < cs:
                return CommitVerdict(
                    False, COMMIT_INCOMPLETE_OBSERVATION,
                    f"observation {source!r} collection_end {ce} < start {cs}")
            if not (cs <= st <= ce):
                return CommitVerdict(
                    False, COMMIT_INCOMPLETE_OBSERVATION,
                    f"observation {source!r} sample_time {st} outside "
                    f"[{cs}, {ce}]")
            # NO FUTURE timestamps: sample_time and collection_end must be at or
            # before commit-now (a future observation is rejected).
            if st > ctx.now or ce > ctx.now:
                return CommitVerdict(
                    False, COMMIT_FUTURE_OBSERVATION,
                    f"observation {source!r} is in the future "
                    f"(sample {st} / end {ce} > now {ctx.now})")
            # age in [0, max_age] and the verdict is EXACTLY 'fresh'.
            age = ctx.now - st
            if fresh != "fresh" or age < 0 or age > ctx.freshness_max_age_s:
                return CommitVerdict(
                    False, COMMIT_STALE_OBSERVATION,
                    f"observation {source!r} at {st} is not fresh "
                    f"(verdict={fresh!r}, age={age}) at commit {ctx.now}")
    return CommitVerdict(True, COMMIT_OK, "all commit invariants satisfied")
