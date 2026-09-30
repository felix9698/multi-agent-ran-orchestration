"""Batch C (remaining P0-6 / section 3.3): canonical-action binding and the
atomic commit-invariant re-check.

Before the write the authorized action is canonicalised + hash-bound with an
authorization artifact; immediately before settlement the whole bundle is
re-checked atomically.  Any mismatch BLOCKS the commit and takes the safe
transaction path (roll back; PendingNotAdmitted on a verified restore).

Covers every named counterexample: pre-action observation, stale observation,
action-hash mismatch, final-readback mismatch, expired authorization, a
complete identifier chain, plus the intent-set-version mismatch.

Offline deterministic fakes only; no network, no hardware, no real sleeps.
"""

import copy
import dataclasses
import enum
import hashlib
import time
import unittest

from coordinator import commit_binding as cb
from coordinator.commit_binding import _verdict_is_satisfied
from coordinator.episode_types import MonitorVerdict
from coordinator.commit_binding import (
    CommitAuthorization, CommitContext, verify_commit_invariant,
    canonicalize_action, stable_action_hash, actions_equal, bind_authorization,
    COMMIT_OK, COMMIT_HASH_MISMATCH, COMMIT_READBACK_MISMATCH,
    COMMIT_AUTH_EXPIRED, COMMIT_INTENT_SET_CHANGED, COMMIT_PRE_ACTION_OBSERVATION,
    COMMIT_STALE_OBSERVATION, COMMIT_SAFETY_LATCHED, COMMIT_ROLLBACK_OBLIGATION,
    COMMIT_NOT_JOINTLY_SATISFIED, COMMIT_MISSING_OBSERVATION,
    COMMIT_INCOMPLETE_OBSERVATION, COMMIT_INTENT_CONTENT_CHANGED,
    COMMIT_FUTURE_OBSERVATION, COMMIT_REVISION_ID_MISMATCH,
    COMMIT_MISSING_BINDING, COMMIT_IDENTIFIER_CHAIN,
    COMMIT_MISSING_VERDICT, COMMIT_VERDICT_NOT_SATISFIED,
    COMMIT_VERDICT_INCONSISTENT, stable_action_hash,
)
from coordinator.intent_coordinator import IntentCoordinator
from coordinator.episode_types import SafetyState, TerminalOutcome, TerminalReason
from decision.intent_model import (
    ConstraintType, FeasibilityPrediction, Intent, IntentTarget, IntentType,
    NetworkState,
)
from config import ActionSpaceConfig


# --------------------------------------------------------------------------
# Pure commit-binding unit tests
# --------------------------------------------------------------------------

class CanonicalActionTest(unittest.TestCase):

    def test_canonicalize_is_order_independent(self):
        a = canonicalize_action([("gnb1", "power_offset", 2.0, None),
                                 ("gnb2", "prb", 24.0, None)])
        b = canonicalize_action([("gnb2", "prb", 24.0, None),
                                 ("gnb1", "power_offset", 2.0, None)])
        self.assertEqual(a, b)
        self.assertEqual(stable_action_hash(a), stable_action_hash(b))

    def test_hash_changes_with_value(self):
        a = canonicalize_action([("gnb1", "power_offset", 2.0, None)])
        b = canonicalize_action([("gnb1", "power_offset", 3.0, None)])
        self.assertNotEqual(stable_action_hash(a), stable_action_hash(b))

    def test_actions_equal_tolerance(self):
        self.assertTrue(actions_equal({"k": 2.0}, {"k": 2.0 + 1e-9}))
        self.assertFalse(actions_equal({"k": 2.0}, {"k": 2.5}))
        self.assertFalse(actions_equal({"k": 2.0}, {"k": 2.0, "j": 1.0}))


def _auth(now=1000.0, ttl=100.0, canonical=None, isv="iset-1"):
    canonical = canonical if canonical is not None else {"gnb1.cell.power_offset": 2.0}
    return bind_authorization(canonical, authorized_revision_id="rev-1",
                              intent_set_version=isv, now=now, ttl_s=ttl,
                              authorized_intent_hash="ic-rev1",
                              episode_id="ep-1", cycle_id="cy-1",
                              proposal_id="pr-1", actuation_trial_id="tr-1")


def _obs(source="trial_window", sample_time=999.5, collection_start=999.2,
         collection_end=999.8, freshness_verdict="fresh"):
    return {"source": source, "sample_time": sample_time,
            "collection_start": collection_start,
            "collection_end": collection_end,
            "freshness_verdict": freshness_verdict}


def _ctx(**over):
    auth = over.pop("authorization", _auth())
    base = dict(
        authorization=auth,
        applied_action_hash=auth.canonical_action_hash,
        now=1000.0,
        intent_set_version_now=auth.intent_set_version,
        committed_intent_hash_now=auth.authorized_intent_hash,
        committed_revision_id_now=auth.authorized_revision_id,
        current_identifier_chain={"episode_id": "ep-1", "cycle_id": "cy-1",
                                  "proposal_id": "pr-1",
                                  "actuation_trial_id": "tr-1"},
        required_intent_ids=("i1",),
        monitor_verdicts={"i1": "satisfied"},
        joint_satisfied=True,
        safety_latched=False,
        rollback_obligation_open=False,
        readback_required=True,
        final_readback_action=dict(auth.canonical_action),
        observations_required=True,
        action_apply_time=999.0,
        freshness_max_age_s=30.0,
        observations=(_obs(),),
    )
    base.update(over)
    return CommitContext(**base)


class VerifyCommitInvariantTest(unittest.TestCase):

    def test_all_satisfied_is_ok(self):
        self.assertTrue(verify_commit_invariant(_ctx()).ok)

    def test_safety_latched_blocks(self):
        v = verify_commit_invariant(_ctx(safety_latched=True))
        self.assertFalse(v.ok)
        self.assertEqual(v.code, COMMIT_SAFETY_LATCHED)

    def test_open_rollback_obligation_blocks(self):
        v = verify_commit_invariant(_ctx(rollback_obligation_open=True))
        self.assertEqual(v.code, COMMIT_ROLLBACK_OBLIGATION)

    def test_hash_mismatch_blocks(self):
        v = verify_commit_invariant(_ctx(applied_action_hash="act-bogus"))
        self.assertEqual(v.code, COMMIT_HASH_MISMATCH)

    def test_expired_authorization_blocks(self):
        v = verify_commit_invariant(_ctx(now=2000.0))    # past expiry 1100
        self.assertEqual(v.code, COMMIT_AUTH_EXPIRED)

    def test_intent_set_change_blocks(self):
        v = verify_commit_invariant(_ctx(intent_set_version_now="iset-9"))
        self.assertEqual(v.code, COMMIT_INTENT_SET_CHANGED)

    def test_violated_verdict_blocks_despite_all_satisfied_boolean(self):
        # the exact repro: all_satisfied cache True but a typed 'violated'
        # verdict -> BLOCK (the boolean cannot override the typed verdicts).
        v = verify_commit_invariant(_ctx(
            joint_satisfied=True,
            monitor_verdicts={"i1": "violated"}))
        self.assertEqual(v.code, COMMIT_VERDICT_NOT_SATISFIED)

    def test_missing_verdict_for_required_intent_blocks(self):
        v = verify_commit_invariant(_ctx(monitor_verdicts={}))
        self.assertEqual(v.code, COMMIT_MISSING_VERDICT)

    def test_unknown_verdict_blocks(self):
        v = verify_commit_invariant(_ctx(monitor_verdicts={"i1": "unknown"}))
        self.assertEqual(v.code, COMMIT_VERDICT_NOT_SATISFIED)

    def test_wrong_typed_verdict_blocks(self):
        for bad in (7, True, None, [], {"x": 1}):
            v = verify_commit_invariant(_ctx(monitor_verdicts={"i1": bad}))
            self.assertFalse(v.ok)
            self.assertEqual(v.code, COMMIT_VERDICT_NOT_SATISFIED)

    def test_empty_required_set_blocks(self):
        v = verify_commit_invariant(_ctx(required_intent_ids=()))
        self.assertEqual(v.code, COMMIT_MISSING_VERDICT)

    def test_missing_required_id_blocks(self):
        # a verdict set that omits a required intent id
        v = verify_commit_invariant(_ctx(
            required_intent_ids=("i1", "i2"),
            monitor_verdicts={"i1": "satisfied"}))
        self.assertEqual(v.code, COMMIT_MISSING_VERDICT)

    def test_boolean_cache_inconsistent_blocks(self):
        # verdicts all satisfied but the cache says False -> inconsistency
        v = verify_commit_invariant(_ctx(joint_satisfied=False))
        self.assertEqual(v.code, COMMIT_VERDICT_INCONSISTENT)

    def test_extra_violated_verdict_blocks(self):
        # an EXTRA (non-required) verdict that is violated still blocks
        v = verify_commit_invariant(_ctx(
            monitor_verdicts={"i1": "satisfied", "iX": "violated"}))
        self.assertEqual(v.code, COMMIT_VERDICT_NOT_SATISFIED)

    def test_fake_verdict_object_blocks(self):
        # item 4 (end-to-end via the gate): a FAKE object with .value=='satisfied'
        # must NOT be accepted (it would fail-open a getattr-based check).
        class _Fake:
            value = "satisfied"
        v = verify_commit_invariant(_ctx(monitor_verdicts={"i1": _Fake()}))
        self.assertEqual(v.code, COMMIT_VERDICT_NOT_SATISFIED)


class StrictVerdictTypeTest(unittest.TestCase):
    """Item 4 (pure): _verdict_is_satisfied accepts ONLY the exact string
    'satisfied' or the real MonitorVerdict.SATISFIED enum instance."""

    def test_accepts_exact_forms(self):
        self.assertTrue(_verdict_is_satisfied("satisfied"))
        self.assertTrue(_verdict_is_satisfied(MonitorVerdict.SATISFIED))

    def test_rejects_fake_object_with_value_attr(self):
        class _Fake:
            value = "satisfied"
        self.assertFalse(_verdict_is_satisfied(_Fake()))

    def test_rejects_other_enum_with_same_value(self):
        class _Other(enum.Enum):
            SAT = "satisfied"
        self.assertFalse(_verdict_is_satisfied(_Other.SAT))

    def test_rejects_bool_number_container(self):
        for bad in (True, False, 1, 0, 1.0, ["satisfied"], {"v": "satisfied"},
                    ("satisfied",), None, MonitorVerdict.VIOLATED,
                    MonitorVerdict.UNKNOWN, "SATISFIED", "Satisfied", " satisfied"):
            self.assertFalse(_verdict_is_satisfied(bad), repr(bad))

    def test_readback_mismatch_blocks(self):
        v = verify_commit_invariant(
            _ctx(final_readback_action={"gnb1.cell.power_offset": 9.0}))
        self.assertEqual(v.code, COMMIT_READBACK_MISMATCH)

    def test_pre_action_observation_blocks(self):
        v = verify_commit_invariant(_ctx(observations=(
            _obs(collection_start=998.0, sample_time=998.5,
                 collection_end=998.9),)))     # window starts < apply 999
        self.assertEqual(v.code, COMMIT_PRE_ACTION_OBSERVATION)

    def test_stale_observation_blocks(self):
        v = verify_commit_invariant(
            _ctx(observations=(_obs(freshness_verdict="stale"),)))
        self.assertEqual(v.code, COMMIT_STALE_OBSERVATION)

    def test_incomplete_observation_blocks(self):
        # missing collection_start -> invalid provenance (never fresh)
        bad = _obs()
        bad.pop("collection_start")
        v = verify_commit_invariant(_ctx(observations=(bad,)))
        self.assertEqual(v.code, COMMIT_INCOMPLETE_OBSERVATION)

    def test_sample_outside_window_blocks(self):
        v = verify_commit_invariant(_ctx(observations=(
            _obs(collection_start=999.2, collection_end=999.4,
                 sample_time=999.9),)))         # sample after collection_end
        self.assertEqual(v.code, COMMIT_INCOMPLETE_OBSERVATION)

    def test_intent_content_change_blocks(self):
        v = verify_commit_invariant(_ctx(committed_intent_hash_now="ic-other"))
        self.assertEqual(v.code, COMMIT_INTENT_CONTENT_CHANGED)

    def test_future_observation_blocks(self):
        # sample/window in the FUTURE relative to commit now (reproduced #2).
        obs = {"source": "w", "sample_time": 2000.0, "collection_start": 1999.0,
               "collection_end": 2001.0, "freshness_verdict": "fresh"}
        v = verify_commit_invariant(_ctx(observations=(obs,)))
        self.assertEqual(v.code, COMMIT_FUTURE_OBSERVATION)

    def test_nonstring_source_blocks(self):
        # source=7 must not be accepted (reproduced #2).
        obs = _obs()
        obs["source"] = 7
        v = verify_commit_invariant(_ctx(observations=(obs,)))
        self.assertEqual(v.code, COMMIT_INCOMPLETE_OBSERVATION)

    def test_nonfinite_sample_time_blocks_not_throws(self):
        for bad in (float("nan"), float("inf"), "x", None, True):
            obs = _obs()
            obs["sample_time"] = bad
            v = verify_commit_invariant(_ctx(observations=(obs,)))
            self.assertFalse(v.ok)              # returns non-OK, never throws
            self.assertEqual(v.code, COMMIT_INCOMPLETE_OBSERVATION)

    def test_empty_authorized_intent_hash_blocks(self):
        # a default-empty authorized_intent_hash must NOT be able to commit even
        # when the committed content differs (reproduced #3).
        auth = _auth()
        auth = dataclasses.replace(auth, authorized_intent_hash="")
        v = verify_commit_invariant(_ctx(authorization=auth,
                                         committed_intent_hash_now="different"))
        self.assertFalse(v.ok)
        self.assertEqual(v.code, COMMIT_MISSING_BINDING)

    def test_empty_authorized_revision_id_blocks(self):
        auth = dataclasses.replace(_auth(), authorized_revision_id="")
        v = verify_commit_invariant(_ctx(authorization=auth))
        self.assertEqual(v.code, COMMIT_MISSING_BINDING)

    def test_revision_id_swap_blocks(self):
        # a SWAPPED object: identical CONTENT hash but a different revision id.
        v = verify_commit_invariant(_ctx(committed_revision_id_now="rev-OTHER"))
        self.assertEqual(v.code, COMMIT_REVISION_ID_MISMATCH)

    def test_identifier_chain_incomplete_blocks(self):
        # a real write whose authorization chain is incomplete (trial id None).
        auth = dataclasses.replace(_auth(), actuation_trial_id=None)
        v = verify_commit_invariant(_ctx(authorization=auth))
        self.assertEqual(v.code, COMMIT_IDENTIFIER_CHAIN)

    def test_identifier_chain_mismatch_blocks(self):
        v = verify_commit_invariant(_ctx(current_identifier_chain={
            "episode_id": "ep-1", "cycle_id": "cy-DIFF",
            "proposal_id": "pr-1", "actuation_trial_id": "tr-1"}))
        self.assertEqual(v.code, COMMIT_IDENTIFIER_CHAIN)

    def test_readback_skipped_when_no_real_write(self):
        # a simulation / no-write commit does not require a device read-back
        v = verify_commit_invariant(_ctx(readback_required=False,
                                         final_readback_action=None,
                                         observations_required=False,
                                         observations=()))
        self.assertTrue(v.ok)


class AuthorizationTest(unittest.TestCase):

    def test_expiry(self):
        a = _auth(now=1000.0, ttl=50.0)
        self.assertTrue(a.is_valid(1049.0))
        self.assertFalse(a.is_valid(1051.0))


# --------------------------------------------------------------------------
# End-to-end: the commit gate through process_intent (real-write path)
# --------------------------------------------------------------------------

class _FakeExecutor:
    class _S:
        pass

    def __init__(self, readback=2.0):
        self.states = {"gnb1": self._S()}
        self.readback = readback
        self.last_restore_report = {}

    def get_axis(self, gnb_id, axis, rnti=None):
        return self.readback


class _IM:
    def __init__(self):
        self.added = []
        self._monitored = []

    def get_active(self):
        return []

    def get_monitored(self):
        return list(self._monitored)

    def add(self, intent):
        self.added.append(intent)
        return True


class _Cal:
    def get_theta_star(self, phase=None):
        return 0.5

    def get_n_max(self):
        return 1

    n_max = 1

    def record_episode(self, m):
        pass

    def get_stats(self):
        return {}


class _Collector:
    simulation_mode = False

    def collect_all(self):
        return {}


class _LLM:
    def active_backend_name(self):
        return "fake-model"


def _intent(v=8.0):
    return Intent(type=IntentType.THROUGHPUT_GOAL,
                  target=IntentTarget(kpi_name="throughput",
                                      constraint_type=ConstraintType.MIN,
                                      target_value=v, unit="Mbps"))


SNAP = {"gnb1": {"power_offset_db": 0.0}}


def _commit_coord(readback=2.0):
    """A coordinator wired for a REAL-write commit whose executor read-back and
    validation observations are controllable."""
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.gui = None
    c.on_state_change = None
    c.on_negotiation_needed = None
    c.current_state = "S0"
    c.history_reservoir = []
    c.max_history = 100
    c.tau_trial_s = 0.0
    c.intent_manager = _IM()
    c.calibrator = _Cal()
    c.negotiation_policy = lambda alt: "reject"
    c.generate_alternatives_fn = lambda i, r: []
    c.ue_collector = _Collector()
    c.executor = _FakeExecutor(readback)
    c.llm_manager = _LLM()
    c.action_space = ActionSpaceConfig()
    c.ue_serving_gnb = {}
    c.safety_state = SafetyState.READY
    c._safety_latch = None
    c.episode_budget_s = 30.0
    c.model_call_timeout_s = 30.0
    c.policy_call_timeout_s = 15.0
    c.max_fsm_steps = 256
    c.commit_freshness_s = 30.0
    c._deadline = None
    c._fsm_steps = 0
    c._cur_schema_valid = True
    c._cur_schema_reason = None
    c._parse_intent = lambda text: {"intent": _intent(), "raw": {}}
    c._check_conflicts = lambda new, active: True
    c._get_network_state = lambda: NetworkState(ue_states={})
    def _af(i, a, s):
        # honest double: a generated proposal always stamps its REAL prompt
        # hash + proposal-generated state (as the production _analyze_feasibility
        # does), so the pre-write S3 invariant (P1-6) sees a bound prompt hash
        # and the commit identifier chain binds a real proposal id.
        c._cur_proposal_generated = True
        c._cur_schema_valid = True   # generated-proposal schema verdict (real bool)
        c._cur_prompt_hash = hashlib.sha256(b"commit-test-prompt").hexdigest()
        return FeasibilityPrediction(
            feasible=True, confidence=0.9, reasoning="",
            proposed_config={"bs1_power_offset": 2.0})
    c._analyze_feasibility = _af
    c._rollback = lambda snap: True          # verified restore on any block
    return c


def _real_write_trial(c):
    """A monkeypatch _execute_trial that performs a REAL write on the shared
    transaction (marks the write, mints the trial id + rebinds authorization at
    the pre-write point, records the applied canonical axis)."""
    def _trial(feas):
        tx = c._active_txn
        c._bind_trial_id_prewrite(tx)          # pre-write id + auth rebind
        tx.note_real_write(time.time())
        tx.snapshot = SNAP
        tx.applied = ({"gnb_id": "gnb1", "axis": "power_offset",
                       "ue_id": None, "value": 2.0, "ok": True},)
        return {"success": True, "snapshot": SNAP, "clipped": [],
                "applied": list(tx.applied)}
    return _trial


def _full_obs(apply_t, freshness="fresh", offset=0.0):
    """A FULL-provenance post-action observation AT the apply time (post-action
    since collection_start >= apply, and never in the FUTURE relative to the
    microsecond-later settlement now)."""
    st = apply_t + offset
    return {"source": "trial_window", "sample_time": st,
            "collection_start": apply_t, "collection_end": st,
            "freshness_verdict": freshness}


def _ok_verdicts(ni, ai):
    """The typed SATISFIED verdicts for the system-owned required intent set
    (current + active), as the real _build_validation would produce."""
    return {getattr(i, "id", None): "satisfied" for i in list(ai) + [ni]}


def _validation(observations):
    def _v(ni, ai):
        return {"all_satisfied": True, "metrics": {},
                "monitor_verdicts": _ok_verdicts(ni, ai),
                "observations": observations}
    return _v


class CommitGateEndToEndTest(unittest.TestCase):

    def _run(self, c, observations="post_fresh"):
        c._execute_trial = _real_write_trial(c)
        if observations == "post_fresh":
            # a fresh, full-provenance observation guaranteed to be AFTER the
            # actual apply time (validation runs after the write).
            def _v(ni, ai):
                apply_t = getattr(c._active_txn, "action_apply_time",
                                  None) or time.time()
                return {"all_satisfied": True, "metrics": {},
                "monitor_verdicts": _ok_verdicts(ni, ai),
                        "observations": [_full_obs(apply_t)]}
            c._validate_trial = _v
        else:
            c._validate_trial = _validation(observations)
        return c.process_intent("throughput >= 8 Mbps")

    def _assert_blocked_pending(self, result, rollbacks):
        self.assertFalse(result["success"])
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.COMMIT_REVERIFICATION_FAILED.value)
        self.assertTrue(result["rolled_back"])
        self.assertEqual(len(rollbacks), 1)     # rolled back exactly once

    def test_commit_succeeds_when_all_invariants_hold(self):
        c = _commit_coord(readback=2.0)
        result = self._run(c)
        self.assertTrue(result["success"])
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        self.assertEqual(len(c.intent_manager.added), 1)   # actually admitted
        self.assertTrue(result["commit_check"]["ok"])
        # item 5: the committed evidence carries the same typed verdicts, all
        # SATISFIED, for the required intent set.
        ev = result["evidence"]
        self.assertTrue(ev["monitor_verdicts"])
        self.assertTrue(all(v == "satisfied"
                            for v in ev["monitor_verdicts"].values()))

    def test_real_build_validation_produces_typed_verdicts(self):
        # item 5: the ACTUAL _build_validation records the typed per-intent
        # MonitorVerdict values (not just the all_satisfied boolean).
        from coordinator.episode_types import MonitorVerdict
        c = IntentCoordinator.__new__(IntentCoordinator)
        c._evaluate_intent_verdict = lambda intent, metrics: \
            MonitorVerdict.SATISFIED
        c.tau_trial_s = 0.0
        ni = _intent(8.0)
        out = c._build_validation({}, ni, [], samples=[5.0], trajectory=[{}],
                                  hard_failure=False, reconnection_time=0.0)
        self.assertTrue(out["all_satisfied"])
        self.assertEqual(out["monitor_verdicts"], {ni.id: "satisfied"})

        c._evaluate_intent_verdict = lambda intent, metrics: \
            MonitorVerdict.VIOLATED
        out2 = c._build_validation({}, ni, [], samples=[5.0], trajectory=[{}],
                                   hard_failure=False, reconnection_time=0.0)
        self.assertFalse(out2["all_satisfied"])
        self.assertEqual(out2["monitor_verdicts"], {ni.id: "violated"})

    def test_commit_rejects_readback_mismatch(self):
        c = _commit_coord(readback=9.0)          # device != canonical 2.0
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        result = self._run(c)
        self._assert_blocked_pending(result, rollbacks)
        self.assertEqual(result["commit_check"]["code"],
                         COMMIT_READBACK_MISMATCH)
        self.assertEqual(c.intent_manager.added, [])       # nothing admitted

    def test_commit_rejects_action_hash_mismatch(self):
        # Canonical-content mutation: the executor applied a DIFFERENT canonical
        # action than authorized.  The commit gate recomputes the hash from what
        # the executor actually handled (tx.applied_canonical) and compares it
        # to the authorization hash -> mismatch (not a cached-string compare).
        c = _commit_coord(readback=2.0)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True

        def _trial(feas):
            tx = c._active_txn
            c._bind_trial_id_prewrite(tx)
            tx.note_real_write(time.time())
            tx.snapshot = SNAP
            tx.applied = ({"gnb_id": "gnb1", "axis": "power_offset",
                           "ue_id": None, "value": 9.0, "ok": True},)
            # executor applied 9.0 while authorized canonical was 2.0
            tx.applied_canonical = {"gnb1.cell.power_offset": 9.0}
            return {"success": True, "snapshot": SNAP, "clipped": [],
                    "applied": list(tx.applied)}

        c._execute_trial = _trial
        c._validate_trial = _validation(
            [{"source": "w", "sample_time": time.time() + 0.001,
              "freshness_verdict": "fresh"}])
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_blocked_pending(result, rollbacks)
        self.assertEqual(result["commit_check"]["code"], COMMIT_HASH_MISMATCH)

    def test_commit_rejects_pre_action_observation(self):
        c = _commit_coord(readback=2.0)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        # collection window BEFORE the action apply time (a pre-action cache)
        result = self._run(c, observations=[
            {"source": "cache", "sample_time": 1.0, "collection_start": 1.0,
             "collection_end": 1.0, "freshness_verdict": "fresh"}])
        self._assert_blocked_pending(result, rollbacks)
        self.assertEqual(result["commit_check"]["code"],
                         COMMIT_PRE_ACTION_OBSERVATION)

    def test_commit_rejects_stale_observation(self):
        c = _commit_coord(readback=2.0)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        c.commit_freshness_s = 5.0
        old = time.time() - 1000.0
        # far-older window (also pre-action since apply is "now"); either a
        # pre-action or stale block is valid - assert it did NOT commit
        result = self._run(c, observations=[
            {"source": "old", "sample_time": old, "collection_start": old,
             "collection_end": old, "freshness_verdict": "fresh"}])
        self.assertFalse(result["success"])
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)
        self.assertIn(result["commit_check"]["code"],
                      (COMMIT_STALE_OBSERVATION, COMMIT_PRE_ACTION_OBSERVATION))

    def test_commit_rejects_explicit_stale_verdict(self):
        c = _commit_coord(readback=2.0)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        c._execute_trial = _real_write_trial(c)

        # post-action sample (after the real apply time) but the collector
        # flagged it stale -> stale block (not pre-action).
        def _v(ni, ai):
            apply_t = getattr(c._active_txn, "action_apply_time",
                              None) or time.time()
            return {"all_satisfied": True, "metrics": {},
            "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(apply_t, freshness="stale")]}

        c._validate_trial = _v
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_blocked_pending(result, rollbacks)
        self.assertEqual(result["commit_check"]["code"],
                         COMMIT_STALE_OBSERVATION)

    def test_commit_rejects_expired_authorization(self):
        c = _commit_coord(readback=2.0)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        orig = _real_write_trial(c)

        def _trial(feas):
            r = orig(feas)
            tx = c._active_txn
            # expire the authorization AFTER the write, before settlement
            tx.authorization = dataclasses.replace(
                tx.authorization, expiry_time=time.time() - 1.0)
            return r

        c._execute_trial = _trial
        c._validate_trial = _validation(
            [{"source": "w", "sample_time": time.time() + 0.001,
              "freshness_verdict": "fresh"}])
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_blocked_pending(result, rollbacks)
        self.assertEqual(result["commit_check"]["code"], COMMIT_AUTH_EXPIRED)

    def test_commit_rejects_intent_set_version_change(self):
        c = _commit_coord(readback=2.0)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        orig = _real_write_trial(c)

        def _trial(feas):
            r = orig(feas)
            tx = c._active_txn
            tx.authorization = dataclasses.replace(
                tx.authorization, intent_set_version="iset-DIFFERENT")
            return r

        c._execute_trial = _trial
        c._validate_trial = _validation(
            [{"source": "w", "sample_time": time.time() + 0.001,
              "freshness_verdict": "fresh"}])
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_blocked_pending(result, rollbacks)
        self.assertEqual(result["commit_check"]["code"],
                         COMMIT_INTENT_SET_CHANGED)

    def test_commit_rejects_committed_intent_content_mutation(self):
        # Mutating the authorized intent CONTENT (not just its id) after
        # authorization must block: the content hash is re-checked at settlement.
        c = _commit_coord(readback=2.0)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        the_intent = _intent(8.0)
        c._parse_intent = lambda text: {"intent": the_intent, "raw": {}}
        orig = _real_write_trial(c)

        def _trial(feas):
            r = orig(feas)
            the_intent.target.target_value = 999.0     # in-place content mutation
            return r

        c._execute_trial = _trial

        def _v(ni, ai):
            apply_t = getattr(c._active_txn, "action_apply_time",
                              None) or time.time()
            return {"all_satisfied": True, "metrics": {},
            "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(apply_t)]}

        c._validate_trial = _v
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_blocked_pending(result, rollbacks)
        self.assertEqual(result["commit_check"]["code"],
                         COMMIT_INTENT_CONTENT_CHANGED)

    def test_commit_rejects_monitored_intent_inplace_mutation(self):
        # An IN-PLACE target mutation of a MONITORED intent between authorization
        # and commit changes the intent-set version and must block.
        c = _commit_coord(readback=2.0)
        monitored = _intent(5.0)
        c.intent_manager._monitored = [monitored]
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        orig = _real_write_trial(c)

        def _trial(feas):
            r = orig(feas)
            monitored.target.target_value = 123.0       # in-place mutation
            return r

        c._execute_trial = _trial

        def _v(ni, ai):
            apply_t = getattr(c._active_txn, "action_apply_time",
                              None) or time.time()
            return {"all_satisfied": True, "metrics": {},
            "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(apply_t)]}

        c._validate_trial = _v
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_blocked_pending(result, rollbacks)
        self.assertEqual(result["commit_check"]["code"],
                         COMMIT_INTENT_SET_CHANGED)

    def test_commit_rejects_device_change_between_capture_and_settlement(self):
        # A state/finalizer callback that changes the device AFTER the S4
        # capture but BEFORE settlement is caught by the FRESH pre-settlement
        # read-back (not the reused S4 capture).
        c = _commit_coord(readback=2.0)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True

        def _cb(old, new):
            if old == "S6" and new == "S0":
                c.executor.readback = 9.0        # device changed post-capture
        c.on_state_change = _cb
        result = self._run(c)
        self._assert_blocked_pending(result, rollbacks)
        self.assertEqual(result["commit_check"]["code"],
                         COMMIT_READBACK_MISMATCH)

    def test_post_gate_evidence_downgrade_leaves_no_commit(self):
        # The commit passes the gate but the post-gate evidence RE-STAMP returns
        # an error downgrade: nothing may be admitted, commit_verified stays
        # False, the real write is rolled back exactly once, and the result is
        # TechnicalFailsafe (coordinator review defect).
        c = _commit_coord(readback=2.0)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        calls = {"n": 0}
        real = c._build_evidence

        def fake(result, outcome, reason):
            calls["n"] += 1
            if calls["n"] >= 2:                 # fail the post-gate re-stamp
                return None, "post-gate evidence boom"
            return real(result, outcome, reason)

        c._build_evidence = fake
        result = self._run(c)
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(c.intent_manager.added, [])       # manager pre-state
        self.assertFalse(c._active_txn.commit_verified)    # not verified
        self.assertEqual(len(rollbacks), 1)                # rolled back once
        self.assertTrue(result["rolled_back"])
        self.assertFalse(result["success"])

    def test_post_gate_evidence_raise_leaves_no_commit(self):
        # Same, but the post-gate re-stamp RAISES rather than returning an error.
        c = _commit_coord(readback=2.0)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        calls = {"n": 0}
        real = c._finalize_episode

        def fake(result, outcome, reason, *, pending_intent=None,
                 committed_revision=None):
            calls["n"] += 1
            if calls["n"] == 2:                 # the post-gate re-stamp
                raise RuntimeError("finalize boom")
            return real(result, outcome, reason, pending_intent=pending_intent,
                        committed_revision=committed_revision)

        c._finalize_episode = fake
        result = self._run(c)
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(c.intent_manager.added, [])
        self.assertFalse(c._active_txn.commit_verified)
        self.assertEqual(len(rollbacks), 1)
        self.assertTrue(result["rolled_back"])
        self.assertFalse(result["success"])

    def test_evidence_identifier_chain_is_complete(self):
        c = _commit_coord(readback=2.0)
        result = self._run(c)
        self.assertTrue(result["success"])
        ev = result["evidence"]
        for link in ("experiment_run_id", "episode_id", "fsm_step_id",
                     "proposer_id", "model_version", "intent_set_version",
                     "pending_intent_hash"):
            self.assertTrue(ev.get(link), f"missing {link}")
        # the canonical action + hash + authorization are bound into evidence
        self.assertTrue(ev["canonical_action_hash"])
        self.assertEqual(ev["canonical_action"],
                         {"gnb1.cell.power_offset": 2.0})
        self.assertIsNotNone(ev["authorization"])
        self.assertTrue(ev["schema_valid"])
        self.assertTrue(ev["commit_check"]["ok"])

    def test_commit_rejects_missing_observation(self):
        # A real write whose validator returns all_satisfied=True but ZERO
        # observations must NOT commit: joint satisfaction is not a substitute
        # for post-action evidence existence (coordinator review).
        c = _commit_coord(readback=2.0)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        c._execute_trial = _real_write_trial(c)
        c._validate_trial = lambda ni, ai: {
            "all_satisfied": True, "metrics": {},
            "monitor_verdicts": _ok_verdicts(ni, ai), "observations": []}
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_blocked_pending(result, rollbacks)
        self.assertEqual(result["commit_check"]["code"],
                         COMMIT_MISSING_OBSERVATION)
        self.assertEqual(c.intent_manager.added, [])       # no admission

    def test_commit_rejects_unknown_freshness_verdict(self):
        # a post-action sample whose freshness verdict is "unknown" (not the
        # explicit "fresh") fails closed - not only "stale" is rejected.
        c = _commit_coord(readback=2.0)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        c._execute_trial = _real_write_trial(c)

        def _v(ni, ai):
            apply_t = getattr(c._active_txn, "action_apply_time",
                              None) or time.time()
            return {"all_satisfied": True, "metrics": {},
            "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(apply_t, freshness="unknown")]}

        c._validate_trial = _v
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_blocked_pending(result, rollbacks)
        self.assertEqual(result["commit_check"]["code"],
                         COMMIT_STALE_OBSERVATION)


class FinalGateAtomicityTest(unittest.TestCase):
    """Reproduced #5 + coordinator follow-ups: a device/deadline mutation on ANY
    re-stamp OR a custom snapshot_state must be caught by the FINAL pre-admission
    gate - block + rollback once + admit nothing, with the emitted evidence
    reflecting the final gate (no stale read-back)."""

    def _commit(self):
        c = _commit_coord(readback=2.0)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        c._execute_trial = _real_write_trial(c)

        def _v(ni, ai):
            apply_t = getattr(c._active_txn, "action_apply_time",
                              None) or time.time()
            return {"all_satisfied": True, "metrics": {},
            "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(apply_t)]}
        c._validate_trial = _v
        return c, rollbacks

    def _assert_blocked(self, result, rollbacks, c):
        self.assertFalse(result["success"])
        self.assertNotEqual(result["terminal_outcome"],
                            TerminalOutcome.COMMIT_ORIGINAL.value)
        self.assertEqual(len(rollbacks), 1)            # rolled back once
        self.assertEqual(c.intent_manager.added, [])   # admitted nothing

    def test_readback_mutation_during_restamp_is_blocked(self):
        c, rollbacks = self._commit()
        real_final = c._finalize_episode
        calls = {"n": 0}

        def _fake(result, outcome, reason, *, pending_intent=None,
                  committed_revision=None):
            calls["n"] += 1
            if calls["n"] >= 2:                # during the re-stamp
                c.executor.readback = 9.0      # device changes post-gate
            return real_final(result, outcome, reason,
                              pending_intent=pending_intent,
                              committed_revision=committed_revision)
        c._finalize_episode = _fake
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_blocked(result, rollbacks, c)
        self.assertEqual(result["commit_check"]["code"],
                         COMMIT_READBACK_MISMATCH)

    def test_deadline_expire_during_restamp_is_blocked(self):
        c, rollbacks = self._commit()
        real_final = c._finalize_episode
        calls = {"n": 0}

        def _fake(result, outcome, reason, *, pending_intent=None,
                  committed_revision=None):
            calls["n"] += 1
            if calls["n"] >= 2:
                c._deadline.deadline = c._deadline.start - 1.0    # expire
            return real_final(result, outcome, reason,
                              pending_intent=pending_intent,
                              committed_revision=committed_revision)
        c._finalize_episode = _fake
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_blocked(result, rollbacks, c)

    def test_custom_snapshot_state_mutation_is_blocked(self):
        # a CUSTOM intent-manager snapshot_state (external code) that mutates the
        # device is captured BEFORE the final gate, so the final gate catches it.
        c, rollbacks = self._commit()

        def _snap():
            c.executor.readback = 9.0          # external mutation during snapshot
            return {}
        c.intent_manager.snapshot_state = _snap
        c.intent_manager.restore_state = lambda snap: None
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_blocked(result, rollbacks, c)
        self.assertEqual(result["commit_check"]["code"],
                         COMMIT_READBACK_MISMATCH)


class ActionsEqualHardeningTest(unittest.TestCase):
    """Item 1 (pure): actions_equal never coerces a non-finite-number value."""

    def test_string_never_matches_number(self):
        self.assertFalse(actions_equal({"k": 2.0}, {"k": "2.0"}))

    def test_bool_never_collides_with_one(self):
        self.assertFalse(actions_equal({"k": 1.0}, {"k": True}))
        self.assertFalse(actions_equal({"k": 0.0}, {"k": False}))

    def test_nonfinite_never_matches(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertFalse(actions_equal({"k": bad}, {"k": bad}))
            self.assertFalse(actions_equal({"k": 2.0}, {"k": bad}))

    def test_finite_numbers_match(self):
        self.assertTrue(actions_equal({"k": 2.0}, {"k": 2.0 + 1e-9}))


class ReadbackTypeStrictTest(unittest.TestCase):
    """Item 1 (end-to-end): a device read-back that is not a finite JSON number
    blocks the commit (rollback once, admit nothing)."""

    def _run_with_readback(self, readback):
        c = _commit_coord(readback=readback)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        c._execute_trial = _real_write_trial(c)

        def _v(ni, ai):
            at = getattr(c._active_txn, "action_apply_time",
                         None) or time.time()
            return {"all_satisfied": True, "metrics": {},
                    "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(at)]}
        c._validate_trial = _v
        result = c.process_intent("throughput >= 8 Mbps")
        return c, result, rollbacks

    def _assert_blocked(self, c, result, rollbacks):
        self.assertFalse(result["success"])
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)
        self.assertEqual(result["commit_check"]["code"],
                         COMMIT_READBACK_MISMATCH)
        self.assertEqual(len(rollbacks), 1)
        self.assertEqual(c.intent_manager.added, [])

    def test_string_readback_blocks(self):
        # the exact repro: readback='2.0' vs canonical numeric 2.0
        self._assert_blocked(*self._run_with_readback("2.0"))

    def test_bool_readback_blocks(self):
        self._assert_blocked(*self._run_with_readback(True))

    def test_nan_readback_blocks(self):
        self._assert_blocked(*self._run_with_readback(float("nan")))

    def test_infinity_readback_blocks(self):
        self._assert_blocked(*self._run_with_readback(float("inf")))


class EvidenceForgeResistanceTest(unittest.TestCase):
    """Item 4: mutating the mutable cycle/result after the provisional finalize
    must NOT forge the committed evidence - the emitted record is rebuilt from
    the immutable transaction/authorization."""

    def test_forged_cycle_fields_do_not_survive(self):
        c = _commit_coord(readback=2.0)
        c._rollback = lambda snap: True
        c._execute_trial = _real_write_trial(c)

        def _v(ni, ai):
            at = getattr(c._active_txn, "action_apply_time",
                         None) or time.time()
            return {"all_satisfied": True, "metrics": {},
                    "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(at)]}
        c._validate_trial = _v

        real_final = c._finalize_episode
        calls = {"n": 0}

        def _fake(result, outcome, reason, *, pending_intent=None,
                  committed_revision=None):
            r = real_final(result, outcome, reason,
                           pending_intent=pending_intent,
                           committed_revision=committed_revision)
            # forge the mutable cycle + result evidence AFTER the finalize
            cyc = (result.get("cycles") or [])
            if cyc:
                cyc[-1]["canonical_action"] = {"gnb1.cell.power_offset": 99.0}
                cyc[-1]["canonical_action_hash"] = "act-forged"
                if isinstance(cyc[-1].get("authorization"), dict):
                    cyc[-1]["authorization"]["canonical_action"] = \
                        {"gnb1.cell.power_offset": 99.0}
            ev = result.get("evidence")
            if isinstance(ev, dict):
                ev["canonical_action"] = {"gnb1.cell.power_offset": 99.0}
                ev["canonical_action_hash"] = "act-forged"
            return r
        c._finalize_episode = _fake

        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        ev = result["evidence"]
        # NO forged value survives: canonical == 2.0, hash consistent, ==auth
        self.assertEqual(ev["canonical_action"],
                         {"gnb1.cell.power_offset": 2.0})
        self.assertNotEqual(ev["canonical_action_hash"], "act-forged")
        self.assertEqual(ev["canonical_action_hash"],
                         stable_action_hash(ev["canonical_action"]))
        self.assertEqual(ev["canonical_action_hash"],
                         ev["authorization"]["canonical_action_hash"])
        self.assertEqual(ev["authorization"]["canonical_action"],
                         {"gnb1.cell.power_offset": 2.0})
        # canonical == final readback
        self.assertEqual(ev["final_readback"], ev["canonical_action"])
        # identifier chain agrees between authorization and evidence
        for f in ("episode_id", "cycle_id", "proposal_id", "actuation_trial_id"):
            self.assertEqual(ev[f], ev["authorization"][f])


class PostGatePurityTest(unittest.TestCase):
    """Coordinator review #5 items 1/3: the post-final-gate publication calls NO
    external code and uses ONLY trusted pre-bound provenance - no forged mutable
    result/cycle value and no post-gate backend call can reopen the gap."""

    def _committing(self, c):
        c._rollback = lambda snap: True
        c._execute_trial = _real_write_trial(c)

        def _v(ni, ai):
            at = getattr(c._active_txn, "action_apply_time",
                         None) or time.time()
            return {"all_satisfied": True, "metrics": {},
                    "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(at)]}
        c._validate_trial = _v

    def test_no_external_backend_call_after_final_gate(self):
        # item 1 exact repro: a backend that MUTATES the device when called must
        # NOT be invoked after the final gate - so it cannot reopen the readback
        # gap; the commit succeeds with device == evidence readback.
        c = _commit_coord(readback=2.0)
        self._committing(c)
        state = {"gate_done": False, "post_gate_calls": 0}

        class _Backend:
            def active_backend_name(self):
                if state["gate_done"]:
                    state["post_gate_calls"] += 1
                    c.executor.readback = 9.0     # would reopen the gap IF called
                return "fake-model"
        c.llm_manager = _Backend()
        real_verify = c._verify_commit
        vc = {"n": 0}

        def _wrap_verify(tx):
            v = real_verify(tx)
            vc["n"] += 1
            if vc["n"] >= 2:                       # after the FINAL gate (verdict2)
                state["gate_done"] = True
            return v
        c._verify_commit = _wrap_verify

        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        self.assertEqual(state["post_gate_calls"], 0)   # NO backend call post-gate
        ev = result["evidence"]
        self.assertEqual(ev["final_readback"], ev["canonical_action"])
        self.assertEqual(ev["final_readback"], {"gnb1.cell.power_offset": 2.0})

    def test_raising_backend_post_gate_cannot_reopen_gap(self):
        # a backend that RAISES if called: with a pure post-gate publication it
        # is never called, so the commit still succeeds cleanly.
        c = _commit_coord(readback=2.0)
        self._committing(c)
        state = {"gate_done": False}

        class _Backend:
            def active_backend_name(self):
                if state["gate_done"]:
                    raise RuntimeError("backend called post-gate!")
                return "fake-model"
        c.llm_manager = _Backend()
        real_verify = c._verify_commit
        vc = {"n": 0}

        def _wrap_verify(tx):
            v = real_verify(tx)
            vc["n"] += 1
            if vc["n"] >= 2:
                state["gate_done"] = True
            return v
        c._verify_commit = _wrap_verify

        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)

    def test_forged_run_and_fsm_ids_do_not_survive(self):
        # item 3 exact repro: mutate result experiment_run_id/fsm_step_id (which
        # are NOT gate-chain inputs) during the re-stamp -> the commit stands but
        # the AUTHORITATIVE evidence uses the ORIGINAL bound ids, not the forged
        # ones.
        c = _commit_coord(readback=2.0)
        self._committing(c)
        real_final = c._finalize_episode
        calls = {"n": 0}

        def _fake(result, outcome, reason, *, pending_intent=None,
                  committed_revision=None):
            r = real_final(result, outcome, reason,
                           pending_intent=pending_intent,
                           committed_revision=committed_revision)
            calls["n"] += 1
            if calls["n"] >= 2:                   # during the re-stamp
                result["experiment_run_id"] = "run-forged"
                result["fsm_step_id"] = "fsm-forged"
            return r
        c._finalize_episode = _fake

        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        ev = result["evidence"]
        self.assertNotEqual(ev["experiment_run_id"], "run-forged")
        self.assertNotEqual(ev["fsm_step_id"], "fsm-forged")
        # authorization <-> evidence identifier chain agrees
        for f in ("episode_id", "cycle_id", "proposal_id", "actuation_trial_id"):
            self.assertEqual(ev[f], ev["authorization"][f])
        # correction #6 item 2: the TOP-LEVEL result identifiers must equal the
        # authoritative evidence identifiers - no forged top-level value survives.
        self.assertNotEqual(result["experiment_run_id"], "run-forged")
        self.assertNotEqual(result["fsm_step_id"], "fsm-forged")
        self.assertEqual(result["experiment_run_id"], ev["experiment_run_id"])
        self.assertEqual(result["fsm_step_id"], ev["fsm_step_id"])
        self.assertEqual(result["episode_id"], ev["episode_id"])
        # and the last cycle's identifier chain agrees with the evidence too.
        last = result["cycles"][-1]
        for f in ("episode_id", "fsm_step_id", "cycle_id", "proposal_id",
                  "actuation_trial_id"):
            self.assertEqual(last[f], ev[f])

    def test_forged_gate_chain_id_blocks_safely(self):
        # a forged GATE-CHAIN id (episode) is caught by the final gate -> safe
        # block, never admits a mismatched/forged id.
        c = _commit_coord(readback=2.0)
        self._committing(c)
        real_final = c._finalize_episode
        calls = {"n": 0}

        def _fake(result, outcome, reason, *, pending_intent=None,
                  committed_revision=None):
            r = real_final(result, outcome, reason,
                           pending_intent=pending_intent,
                           committed_revision=committed_revision)
            calls["n"] += 1
            if calls["n"] >= 2:
                cyc = (result.get("cycles") or [])
                if cyc:
                    cyc[-1]["episode_id"] = "ep-forged"
            return r
        c._finalize_episode = _fake

        result = c.process_intent("throughput >= 8 Mbps")
        self.assertNotEqual(result["terminal_outcome"],
                            TerminalOutcome.COMMIT_ORIGINAL.value)   # safe block
        self.assertEqual(c.intent_manager.added, [])


class SnapshotAliasIsolationTest(unittest.TestCase):
    """Coordinator review #5 item 2: snapshot + tx provenance are alias-isolated
    - a public mutation of a cycle/result copy never corrupts the trusted tx,
    and the rollback receives the ORIGINAL snapshot exactly once."""

    def test_stash_cycle_audit_deep_copies(self):
        from coordinator.episode_types import ActuationTransaction
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.executor = None
        tx = ActuationTransaction()
        tx.snapshot = {"gnb1": {"power_offset_db": 0.0}}
        tx.requested_action = {"bs1_power_offset": 2.0}
        tx.applied = ({"gnb_id": "gnb1", "axis": "power_offset", "value": 2.0},)
        tx.canonical_action = {"gnb1.cell.power_offset": 2.0}
        cycle = {}
        c._stash_cycle_audit(cycle, tx)
        # not the same object
        self.assertIsNot(cycle["snapshot"], tx.snapshot)
        self.assertIsNot(cycle["snapshot"]["gnb1"], tx.snapshot["gnb1"])
        # a public nested mutation does NOT touch tx
        cycle["snapshot"]["gnb1"]["power_offset_db"] = 999.0
        cycle["requested_action"]["bs1_power_offset"] = 999.0
        cycle["canonical_action"]["gnb1.cell.power_offset"] = 999.0
        self.assertEqual(tx.snapshot["gnb1"]["power_offset_db"], 0.0)
        self.assertEqual(tx.requested_action["bs1_power_offset"], 2.0)
        self.assertEqual(tx.canonical_action["gnb1.cell.power_offset"], 2.0)

    def test_public_snapshot_mutation_does_not_corrupt_rollback(self):
        # end-to-end: mutate the public cycle snapshot 0->999 during the re-stamp
        # and force a gate error - the rollback still receives the ORIGINAL 0.
        c = ExecutorConsumesCanonicalTest._coord(
            ExecutorConsumesCanonicalTest(), {"bs1_power_offset": 2.0})
        c.executor.snap = {"gnb1": {"power_offset_db": 0.0,
                                    "snapshot_source": {"power_offset": "device"}}}
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(copy.deepcopy(snap)) or True

        def _v(ni, ai):
            at = getattr(c._active_txn, "action_apply_time",
                         None) or time.time()
            return {"all_satisfied": True, "metrics": {},
                    "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(at)]}
        c._validate_trial = _v
        real_final = c._finalize_episode
        calls = {"n": 0}

        def _fake(result, outcome, reason, *, pending_intent=None,
                  committed_revision=None):
            r = real_final(result, outcome, reason,
                           pending_intent=pending_intent,
                           committed_revision=committed_revision)
            calls["n"] += 1
            if calls["n"] >= 2:               # during the re-stamp
                cyc = (result.get("cycles") or [])
                if cyc and isinstance(cyc[-1].get("snapshot"), dict):
                    cyc[-1]["snapshot"]["gnb1"]["power_offset_db"] = 999.0
                # force the final gate to fail (readback mismatch)
                c.executor.get_axis = lambda *a, **k: 9.0
            return r
        c._finalize_episode = _fake

        result = c.process_intent("throughput >= 8 Mbps")
        self.assertNotEqual(result["terminal_outcome"],
                            TerminalOutcome.COMMIT_ORIGINAL.value)
        self.assertEqual(len(rollbacks), 1)               # rolled back once
        self.assertEqual(rollbacks[0]["gnb1"]["power_offset_db"], 0.0)  # ORIGINAL
        self.assertEqual(c.intent_manager.added, [])      # admitted nothing


class MonitorVerdictAliasIsolationTest(unittest.TestCase):
    """Coordinator correction #6 item 1: the trusted tx monitor verdicts (the
    typed commit-gate input) are an INDEPENDENT deep copy of the public cycle
    copy - a callback flipping the public cycle verdict violated->satisfied can
    NOT reach through a shared alias and change what the gate reads from tx."""

    def test_capture_isolates_tx_from_cycle_verdicts(self):
        # direct identity + no-alias: mutating the public cycle copy leaves the
        # trusted tx verdicts untouched.
        from coordinator.episode_types import ActuationTransaction
        c = IntentCoordinator.__new__(IntentCoordinator)
        tx = ActuationTransaction()
        cycle = {}
        validation = {"observations": [],
                      "monitor_verdicts": {"i1": "violated"}}
        c._capture_commit_evidence(tx, cycle, validation)
        self.assertIsNot(tx.monitor_verdicts, cycle["monitor_verdicts"])
        self.assertIsNot(tx.monitor_verdicts,
                         validation["monitor_verdicts"])
        self.assertEqual(tx.monitor_verdicts, {"i1": "violated"})
        # a public mutation of the cycle copy does NOT change tx
        cycle["monitor_verdicts"]["i1"] = "satisfied"
        self.assertEqual(tx.monitor_verdicts["i1"], "violated")

    def test_public_verdict_flip_cannot_admit(self):
        # end-to-end exact repro: S4 payload is all_satisfied=True but the typed
        # verdict is VIOLATED; a callback flips the PUBLIC cycle verdict to
        # satisfied before the gate.  Because tx keeps its own copy, the gate
        # still sees violated -> block + rollback once + admit nothing.
        c = _commit_coord(readback=2.0)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        c._execute_trial = _real_write_trial(c)

        def _v(ni, ai):
            at = getattr(c._active_txn, "action_apply_time",
                         None) or time.time()
            # all_satisfied cache True, but the per-intent verdict is VIOLATED.
            return {"all_satisfied": True, "metrics": {},
                    "monitor_verdicts": {getattr(i, "id", None): "violated"
                                         for i in list(ai) + [ni]},
                    "observations": [_full_obs(at)]}
        c._validate_trial = _v

        real_verify = c._verify_commit

        def _wrap_verify(tx):
            # flip the PUBLIC cycle verdict to satisfied BEFORE the gate reads tx
            result = getattr(c, "_active_result", None)
            cyc = (result.get("cycles") if isinstance(result, dict) else None)
            if cyc:
                mv = cyc[-1].get("monitor_verdicts")
                if isinstance(mv, dict):
                    for k in list(mv):
                        mv[k] = "satisfied"
            return real_verify(tx)
        c._verify_commit = _wrap_verify

        result = c.process_intent("throughput >= 8 Mbps")
        self.assertNotEqual(result["terminal_outcome"],
                            TerminalOutcome.COMMIT_ORIGINAL.value)  # NOT admitted
        self.assertEqual(result["commit_check"]["code"],
                         COMMIT_VERDICT_NOT_SATISFIED)
        self.assertEqual(c.intent_manager.added, [])               # nothing added
        self.assertEqual(len(rollbacks), 1)                        # rolled back once


class SettlementExceptionBackstopTest(unittest.TestCase):
    """Coordinator review #3 item 1: an UNEXPECTED exception from a commit gate
    or any pre-admission settlement op must NOT escape - it fails closed to a
    typed non-commit, rolls a real write back once, admits nothing, keeps
    commit_verified False, and preserves the root error."""

    def _committing(self):
        c = _commit_coord(readback=2.0)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        c._execute_trial = _real_write_trial(c)

        def _v(ni, ai):
            at = getattr(c._active_txn, "action_apply_time",
                         None) or time.time()
            return {"all_satisfied": True, "metrics": {},
            "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(at)]}
        c._validate_trial = _v
        return c, rollbacks

    def test_gate_exception_from_corrupt_intent_is_caught(self):
        # Exact counterexample: corrupt committed_revision.target.target_value
        # to a non-number -> _intent_content_hash raises ValueError during the
        # gate.  process_intent must NOT raise.
        c, rollbacks = self._committing()
        the_intent = _intent(8.0)
        c._parse_intent = lambda text: {"intent": the_intent, "raw": {}}
        base = _real_write_trial(c)

        def _trial(feas):
            r = base(feas)
            the_intent.target.target_value = "not-a-number"    # corrupt
            return r
        c._execute_trial = _trial

        result = c.process_intent("throughput >= 8 Mbps")     # must NOT raise
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.INTERNAL_ERROR.value)
        self.assertFalse(result["success"])
        self.assertEqual(len(rollbacks), 1)                   # rolled back once
        self.assertEqual(c.intent_manager.added, [])          # admitted nothing
        self.assertFalse(c._active_txn.commit_verified)       # not verified
        self.assertIn("not-a-number", result["error"])        # root error kept

    def test_final_gate_exception_during_restamp_is_caught(self):
        # Structurally distinct: gate #1 passes, then the corruption happens
        # DURING the re-stamp so the FINAL gate raises.  Still caught.
        c, rollbacks = self._committing()
        the_intent = _intent(8.0)
        c._parse_intent = lambda text: {"intent": the_intent, "raw": {}}
        real_final = c._finalize_episode
        calls = {"n": 0}

        def _fake(result, outcome, reason, *, pending_intent=None,
                  committed_revision=None):
            calls["n"] += 1
            if calls["n"] >= 2:                 # during the re-stamp, post gate#1
                the_intent.target.target_value = "boom"
            return real_final(result, outcome, reason,
                              pending_intent=pending_intent,
                              committed_revision=committed_revision)
        c._finalize_episode = _fake

        result = c.process_intent("throughput >= 8 Mbps")     # must NOT raise
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(len(rollbacks), 1)
        self.assertEqual(c.intent_manager.added, [])
        self.assertFalse(c._active_txn.commit_verified)


class IntentSetVersionFailClosedTest(unittest.TestCase):
    """Coordinator review #3 item 2: a present intent-manager whose
    get_monitored raises must FAIL CLOSED - never be hashed as an empty set."""

    def test_intent_set_version_propagates_manager_failure(self):
        c = _commit_coord(readback=2.0)

        class _Raise:
            def get_monitored(self):
                raise RuntimeError("monitored boom")
        c.intent_manager = _Raise()
        with self.assertRaises(RuntimeError):
            c._intent_set_version()

    def test_missing_manager_is_still_tolerated(self):
        # a DELIBERATELY-missing manager (test skeleton) stays compatible.
        c = IntentCoordinator.__new__(IntentCoordinator)
        self.assertTrue(c._intent_set_version().startswith("iset-"))

    def test_get_monitored_failure_pre_write_zero_writes(self):
        # a raising get_monitored fails the episode BEFORE any write.
        c = _commit_coord(readback=2.0)
        applied = []
        c.executor.get_axis = lambda *a, **k: 2.0
        base_apply = getattr(c.executor, "apply_axis", None)

        class _Raise:
            def get_monitored(self):
                raise RuntimeError("monitored boom")
            def add(self, i):
                applied.append(("add", i))
                return True
        c.intent_manager = _Raise()
        # count real executor writes via a tracking snapshot/apply executor
        writes = []
        c.executor.apply_axis = lambda *a, **k: writes.append(a) or True
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(writes, [])            # zero writes
        self.assertEqual(applied, [])           # nothing admitted

    def test_get_monitored_failure_at_settlement_rolls_back_once(self):
        # get_monitored works until AFTER the write, then fails -> the
        # settlement path fails closed and rolls back exactly once, admitting
        # nothing (never an unchanged-set commit).
        c = _commit_coord(readback=2.0)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True
        state = {"fail": False, "added": []}

        class _Toggle:
            def get_monitored(self):
                if state["fail"]:
                    raise RuntimeError("monitored boom (post-write)")
                return []
            def add(self, i):
                state["added"].append(i)
                return True
        c.intent_manager = _Toggle()
        base = _real_write_trial(c)

        def _trial(feas):
            r = base(feas)
            state["fail"] = True               # break get_monitored post-write
            return r
        c._execute_trial = _trial

        def _v(ni, ai):
            at = getattr(c._active_txn, "action_apply_time",
                         None) or time.time()
            return {"all_satisfied": True, "metrics": {},
            "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(at)]}
        c._validate_trial = _v

        result = c.process_intent("throughput >= 8 Mbps")     # must NOT raise
        self.assertFalse(result["success"])
        self.assertNotEqual(result["terminal_outcome"],
                            TerminalOutcome.COMMIT_ORIGINAL.value)
        self.assertEqual(len(rollbacks), 1)                   # rolled back once
        self.assertEqual(state["added"], [])                  # admitted nothing
        self.assertFalse(c._active_txn.commit_verified)


class ExecutorConsumesCanonicalTest(unittest.TestCase):
    """P0-6 (coordinator review 2): the executor receives ONLY the authorized
    canonical action; the raw proposal cannot be re-read after authorization."""

    class _RecordingExec:
        class _S:
            pass

        def __init__(self):
            self.states = {"gnb1": self._S()}
            self.applied = []
            self.snap = {"gnb1": {"power_offset_db": 0.0,
                                  "snapshot_source": {"power_offset": "device"}}}

        def snapshot(self, from_device=True):
            return self.snap

        def apply_axis(self, gnb_id, axis, value, rnti=None, verify=True):
            self.applied.append((gnb_id, axis, float(value), rnti))
            return True

        def get_axis(self, gnb_id, axis, rnti=None):
            for g, a, v, r in reversed(self.applied):
                if g == gnb_id and a == axis:
                    return v
            return 0.0

        def restore(self, snap):
            return True

    def _coord(self, proposed):
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.gui = None
        c.on_state_change = None
        c.on_negotiation_needed = None
        c.current_state = "S0"
        c.history_reservoir = []
        c.max_history = 100
        c.tau_trial_s = 0.0
        c.intent_manager = _IM()
        c.calibrator = _Cal()
        c.negotiation_policy = lambda alt: "reject"
        c.generate_alternatives_fn = lambda i, r: []
        c.ue_collector = _Collector()
        c.executor = self._RecordingExec()
        c.llm_manager = _LLM()
        c.action_space = ActionSpaceConfig()
        c.ue_serving_gnb = {}
        c.ue_rnti = {}
        c.safety_state = SafetyState.READY
        c._safety_latch = None
        c.episode_budget_s = 30.0
        c.model_call_timeout_s = 30.0
        c.policy_call_timeout_s = 15.0
        c.max_fsm_steps = 256
        c.commit_freshness_s = 30.0
        c._deadline = None
        c._fsm_steps = 0
        c._cur_schema_valid = True
        c._cur_schema_reason = None
        self._proposed = dict(proposed)
        c._parse_intent = lambda text: {"intent": _intent(), "raw": {}}
        c._check_conflicts = lambda new, active: True
        c._get_network_state = lambda: NetworkState(ue_states={})

        def _af(i, a, s):
            # honest double: stamp the REAL prompt hash + proposal-generated
            # state so the pre-write S3 invariant (P1-6) and the commit
            # identifier chain see a bound proposal.
            c._cur_proposal_generated = True
            c._cur_schema_valid = True   # generated-proposal schema verdict (real bool)
            c._cur_prompt_hash = hashlib.sha256(b"exec-canonical-test").hexdigest()
            return FeasibilityPrediction(
                feasible=True, confidence=0.9, reasoning="",
                proposed_config=self._proposed)
        c._analyze_feasibility = _af
        return c

    def test_raw_proposal_mutation_after_authorization_has_no_effect(self):
        # Mutating the raw proposal AFTER authorization (before the write) must
        # NOT change what the executor applies: it consumes only the authorized
        # canonical vector.
        c = self._coord({"bs1_power_offset": 2.0})
        real_auth = c._authorize_action

        def _auth(feas, cycle, tx, cur):
            real_auth(feas, cycle, tx, cur)
            feas.proposed_config["bs1_power_offset"] = 99.0   # attacker mutates
            feas.proposed_config["bs1_prb"] = 6

        c._authorize_action = _auth
        c._validate_trial = lambda ni, ai: {"all_satisfied": False, "metrics": {}}
        c._rollback = lambda snap: True
        c.process_intent("throughput >= 8 Mbps")
        # exactly the AUTHORIZED action reached the executor; the mutation did not
        self.assertEqual(c.executor.applied,
                         [("gnb1", "power_offset", 2.0, None)])

    def test_executor_applies_only_authorized_canonical(self):
        c = self._coord({"bs1_power_offset": 3.0})
        c._validate_trial = lambda ni, ai: {"all_satisfied": False, "metrics": {}}
        c._rollback = lambda snap: True
        c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(c.executor.applied,
                         [("gnb1", "power_offset", 3.0, None)])

    def test_snapshot_verify_failure_has_no_trial_id_and_zero_writes(self):
        # If the pre-trial snapshot cannot be device-verified (unqualified axis)
        # the trial fails BEFORE any write: NO actuation_trial_id is minted and
        # zero executor writes occur (coordinator review).
        c = self._coord({"bs1_power_offset": 2.0})
        c.executor.snap = {"gnb1": {"power_offset_db": 0.0,
                                    "snapshot_source": {"power_offset": "mirror"}}}
        c._validate_trial = lambda ni, ai: {"all_satisfied": False, "metrics": {}}
        c._rollback = lambda snap: True
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(c.executor.applied, [])           # zero writes
        cyc = result["cycles"][-1]
        self.assertIsNone(cyc.get("actuation_trial_id"))   # no dishonest id
        self.assertIsNone(cyc["authorization"]["actuation_trial_id"])

    def test_real_write_authorization_and_evidence_share_trial_id(self):
        # The real verified-snapshot write path mints ONE trial id and the
        # authorization AND the evidence bind the SAME id.
        c = self._coord({"bs1_power_offset": 2.0})

        def _v(ni, ai):
            apply_t = getattr(c._active_txn, "action_apply_time",
                              None) or time.time()
            return {"all_satisfied": True, "metrics": {},
            "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(apply_t)]}

        c._validate_trial = _v
        c._rollback = lambda snap: True
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        cyc = result["cycles"][-1]
        tid = cyc["actuation_trial_id"]
        self.assertTrue(tid)
        self.assertEqual(cyc["authorization"]["actuation_trial_id"], tid)
        self.assertEqual(result["evidence"]["actuation_trial_id"], tid)

    def test_no_authorization_performs_zero_writes(self):
        # Reproduced #4: _execute_trial with NO authorization must write NOTHING
        # and fail closed (canonical-only execution).
        c = self._coord({"bs1_power_offset": 2.0})
        from decision.intent_model import FeasibilityPrediction
        feas = FeasibilityPrediction(feasible=True, confidence=0.9,
                                     reasoning="",
                                     proposed_config={"bs1_power_offset": 2.0})
        # call directly WITHOUT _authorize_action (tx has no authorization)
        from coordinator.episode_types import ActuationTransaction
        c._active_txn = ActuationTransaction()
        res = c._execute_trial(feas)
        self.assertFalse(res["success"])
        self.assertEqual(c.executor.applied, [])       # zero writes
        self.assertIsNone(c._active_txn.authorization)

    def test_unresolved_rnti_has_no_trial_id_zero_writes(self):
        # Reproduced #7: a per-UE axis whose RNTI cannot be resolved makes ZERO
        # executor attempts and mints NO trial id (the id is minted only right
        # before the first actual apply, after RNTI resolution).
        c = self._coord({"ue1_prb": 20})
        c.ue_serving_gnb = {"ue1": "gnb1"}
        c.ue_rnti = {}
        # executor cannot resolve the RNTI
        c.executor.get_connected_rnti = lambda gnb: None
        # snapshot must be device-qualified for the ue_prb axis
        c.executor.snap = {"gnb1": {"power_offset_db": 0.0,
                                    "snapshot_source": {"ue_prb": "device"}}}
        c._validate_trial = lambda ni, ai: {"all_satisfied": False, "metrics": {}}
        c._rollback = lambda snap: True
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(c.executor.applied, [])       # zero executor attempts
        cyc = result["cycles"][-1]
        self.assertIsNone(cyc.get("actuation_trial_id"))
        self.assertIsNone(cyc["authorization"]["actuation_trial_id"])
        self.assertIsNone(c._active_txn.actuation_trial_id)

    def test_first_axis_raise_rolls_back_once(self):
        # Reproduced C5: an apply that RAISES has unknown side-effect status ->
        # treat as a write attempt so the rollback runs exactly once (not zero).
        c = self._coord({"bs1_power_offset": 2.0})
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True

        def _raise(gnb, axis, value, rnti=None, verify=True):
            raise RuntimeError("apply boom")
        c.executor.apply_axis = _raise
        c._validate_trial = lambda ni, ai: {"all_satisfied": False, "metrics": {}}
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(len(rollbacks), 1)            # rolled back exactly once
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        # a trial id WAS minted (a real write was attempted) and survives
        self.assertTrue(c._active_txn.actuation_trial_id)
        self.assertTrue(result["cycles"][-1].get("actuation_trial_id"))

    def test_simulation_successful_path_has_no_trial_id(self):
        # A simulated (no real executor write) successful S3 route carries NO
        # actuation_trial_id, on the cycle OR in the authorization.
        c = self._coord({"bs1_power_offset": 2.0})
        c.ue_collector = type("SimC", (), {
            "simulation_mode": True,
            "collect_all": lambda self: {}})()
        # fail S4 so it does not attempt a commit (a no-op simulation route)
        c._validate_trial = lambda ni, ai: {"all_satisfied": False, "metrics": {}}
        c._rollback = lambda snap: True
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(c.executor.applied, [])           # no REAL write
        cyc = result["cycles"][-1]
        self.assertIsNone(cyc.get("actuation_trial_id"))
        self.assertIsNone(cyc["authorization"]["actuation_trial_id"])


class ReadbackLatencyTimerP13Test(unittest.TestCase):
    """P1-3 (blocker 1/4): the AUTHORITATIVE commit-gate read-back timer is
    measured even when the read-back RAISES, and survives onto the cycle."""

    def test_commit_readback_timer_survives_readback_exception(self):
        import math
        from coordinator.episode_types import ActuationTransaction
        c = IntentCoordinator.__new__(IntentCoordinator)
        tx = ActuationTransaction()
        tx.actual_real_write_performed = True
        tx.canonical_action = {}

        def _boom(_t):
            raise RuntimeError("readback boom")
        c._readback_canonical = _boom
        with self.assertRaises(RuntimeError):
            c._verify_commit(tx)
        # the finally recorded the REAL elapsed read-back latency, not lost
        self.assertIsInstance(tx.commit_readback_ms, float)
        self.assertTrue(math.isfinite(tx.commit_readback_ms))
        self.assertGreaterEqual(tx.commit_readback_ms, 0.0)

    def test_commit_settle_preserves_readback_ms_when_verify_raises(self):
        from coordinator.episode_types import ActuationTransaction
        c = IntentCoordinator.__new__(IntentCoordinator)
        tx = ActuationTransaction()
        tx.commit_readback_ms = 4.2               # timer already measured
        c._deadline_expired = lambda: False

        def _raise(_t):
            raise RuntimeError("verify boom")
        c._verify_commit = _raise
        result = {"cycles": [{"cycle": 0}]}
        with self.assertRaises(RuntimeError):
            c._commit_settle(result, tx)
        # the try/finally preserved the authoritative readback latency onto the
        # raw cycle even though _verify_commit raised (timer never lost).
        self.assertEqual(result["cycles"][0]["readback_ms"], 4.2)


if __name__ == "__main__":
    unittest.main()
