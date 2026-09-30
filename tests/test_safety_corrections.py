"""Batch B Gate-review corrections: counterexamples the coordinator reproduced.

Covers items 1-8 of the correction dispatch:
  1. mid-write exception (real two/three-axis _execute_trial) rolls back once,
     no later write;
  2. finalization is INSIDE the transaction (deferred to post-cleanup): a
     proposer/_build_evidence/admission failure rolls back once and admits
     nothing; a post-write final-S0 / history-finalization failure likewise;
  3. an UNKNOWN restore AFTER a real write latches; a simulation/no-write
     UNKNOWN does not;
  4. hard-failure honesty: no recovery sampling when config restore is
     unverified; typed physical-recovery-failed vs unknown terminals, no
     negotiation;
  5. operator clearance is full-readback verified (stale-mirror false-clear
     refused) and there is no public unconditional clear;
  6. executor restore bypass is thread-scoped;
  7. trigger_handover obeys the latch;
  8. the emitted terminal evidence (not only the private latch dict) carries
     the action/snapshot/rollback provenance.
"""

import hashlib
import threading
import time
import unittest

from config import ActionSpaceConfig
from coordinator.intent_coordinator import IntentCoordinator, IntentManager
from coordinator.episode_types import (
    ConfigRestoreVerdict, PhysicalRecoveryVerdict, SafetyState, TerminalOutcome,
    TerminalReason,
)
from decision.intent_model import (
    ConstraintType, FeasibilityPrediction, Intent, IntentStatus, IntentTarget,
    IntentType, NetworkState,
)
from tests.test_safety_transaction import (
    _make_coordinator, _FakeIntentManager,
)
from tests.test_hard_failure_recovery import _txn_coordinator


def _intent(target=4.0):
    return Intent(type=IntentType.THROUGHPUT_GOAL,
                  target=IntentTarget(kpi_name="throughput",
                                      constraint_type=ConstraintType.MIN,
                                      target_value=target, unit="Mbps"))


# ==========================================================================
# Real-executor harness (drives the REAL _execute_trial write loop)
# ==========================================================================

class _ScriptedExec:
    """A fake executor whose apply_axis outcome is scripted per call, and whose
    restore verdict is controllable. NOT the real telnet path - it exercises the
    coordinator's real _execute_trial write loop / rollback transaction."""

    def __init__(self, apply_script=None, restore_result=True):
        self._latched = False
        self.apply_script = list(apply_script or [])
        self.apply_calls = []
        self.restore_result = restore_result       # True/False/"raise"
        self.restore_calls = 0
        self.last_restore_report = {}
        # _parse_action_vector checks `gnb_id in executor.states`
        self.states = {"gnb1": object(), "gnb2": object()}

    def snapshot(self, from_device=True):
        src = {a: "device" for a in ("power_offset", "prb", "sched_priority",
                                     "mcs_offset", "ue_prb", "ue_sched")}
        return {"gnb1": {"power_offset_db": 0.0, "prb_cap": 0,
                         "sched_priority": 1.0, "mcs_offset": 0.0,
                         "ue_prb_cap": {}, "ue_sched_priority": {},
                         "snapshot_source": src, "reestab_count": 0}}

    def apply_axis(self, gnb_id, axis, value, rnti=None, verify=True):
        i = len(self.apply_calls)
        self.apply_calls.append((gnb_id, axis, value))
        act = self.apply_script[i] if i < len(self.apply_script) else "ok"
        if act == "raise":
            raise RuntimeError(f"apply {axis} boom")
        return act == "ok"

    def restore(self, snapshot):
        self.restore_calls += 1
        if self.restore_result == "raise":
            raise RuntimeError("restore boom")
        ok = bool(self.restore_result)
        self.last_restore_report = {
            "gnb1": {"power_offset": (0.0, 0.0 if ok else 9.9, ok)}}
        return ok

    def get_connected_rnti(self, gnb_id):
        return None

    def latch_failsafe(self, reason=""):
        self._latched = True

    @property
    def is_latched(self):
        return self._latched

    def recover_and_clear(self, target):
        self._latched = False
        return True


class _OffCollector:
    simulation_mode = False

    def collect_all(self):
        return {}

    def get_throughput_all(self, duration=2.0):
        return {}


class _Cal:
    n_max = 0

    def get_theta_star(self, phase=None):
        return 0.5

    def can_negotiate_more(self, rounds, phase=None):
        return False

    def record_episode(self, m):
        pass


class _LLM:
    def active_backend_name(self):
        return "fake"


def _real_coord(executor, proposed, validate=None):
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.gui = None
    c.on_negotiation_needed = None
    c.on_state_change = None
    c.current_state = "S0"
    c.history_reservoir = []
    c.max_history = 100
    c.tau_trial_s = 0.0
    c.hard_failure_cap_s = 0.1
    c._pre_trial_attached = set()
    c._last_throughput = {}
    c._last_tp_time = 0.0
    c.intent_manager = _FakeIntentManager()
    c.calibrator = _Cal()
    c.negotiation_policy = None
    c.generate_alternatives_fn = lambda i, r: []
    c.ue_collector = _OffCollector()
    c.executor = executor
    c.llm_manager = _LLM()
    c.action_space = ActionSpaceConfig()
    c.action_min_db = c.action_space.power_offset_min_db
    c.action_max_db = c.action_space.power_offset_max_db
    c.ue_serving_gnb = {"ue1": "gnb1", "ue2": "gnb1", "ue3": "gnb2"}
    c.ue_rnti = {}
    c.safety_state = SafetyState.READY
    c._safety_latch = None
    intent = _intent()
    c._parse_intent = lambda t: {"intent": intent, "raw": {}}
    c._check_conflicts = lambda n, a: True
    c._get_network_state = lambda: NetworkState(ue_states={})
    def _af(i, a, s):
        # honest double: a generated proposal stamps its REAL prompt hash +
        # proposal-generated state (as production _analyze_feasibility does), so
        # the pre-write S3 invariant (P1-6) sees a bound prompt hash.
        c._cur_proposal_generated = True
        c._cur_schema_valid = True   # generated-proposal schema verdict (real bool)
        c._cur_prompt_hash = hashlib.sha256(b"safety-corrections-test").hexdigest()
        return FeasibilityPrediction(
            feasible=True, confidence=0.9, reasoning="",
            proposed_config=dict(proposed), alternatives=[])
    c._analyze_feasibility = _af
    if validate is not None:
        c._validate_trial = validate
    return c


# ==========================================================================
# Item 1: mid-write exception rolls back once, no later write
# ==========================================================================

class MidWriteExceptionTest(unittest.TestCase):

    def test_second_axis_raise_rolls_back_once_no_later_write(self):
        # first axis writes OK (real write recorded on the shared tx), the
        # SECOND axis's executor.apply_axis RAISES (do NOT rely on the executor
        # catching OSError). Expect: exactly one rollback, no third write,
        # transaction not left READY-without-rollback.
        ex = _ScriptedExec(apply_script=["ok", "raise"], restore_result=True)
        c = _real_coord(ex, {"bs1_power_offset": 3.0, "bs1_mcs_offset": -4,
                             "bs1_prb": 24})
        result = c.process_intent("throughput >= 4 Mbps")
        self.assertEqual(len(ex.apply_calls), 2)     # third axis NEVER attempted
        self.assertEqual(ex.restore_calls, 1)        # EXACTLY one rollback
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertFalse(c._is_latched())            # restore verified -> no latch
        self.assertIs(c.safety_state, SafetyState.READY)

    def test_mid_write_raise_with_failed_restore_latches(self):
        ex = _ScriptedExec(apply_script=["ok", "raise"], restore_result=False)
        c = _real_coord(ex, {"bs1_power_offset": 3.0, "bs1_mcs_offset": -4})
        result = c.process_intent("throughput >= 4 Mbps")
        self.assertEqual(ex.restore_calls, 1)
        self.assertTrue(c._is_latched())
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)


# ==========================================================================
# Item 2 + cleanup: finalization deferred; failures roll back, admit nothing
# ==========================================================================

def _committing_validation(c):
    """A validated-commit _validate_trial that ALSO returns a valid post-action
    fresh Observation tied to the transaction apply time (Batch C P0-6: a real
    write must carry post-action evidence for the commit gate to pass)."""
    def _v(ni, ai):
        tx = getattr(c, "_active_txn", None)
        apply_t = getattr(tx, "action_apply_time", None) or 0.0
        # a current, post-action (>= apply), fresh sample with FULL provenance
        sample_t = max(float(apply_t), time.time())
        return {"all_satisfied": True, "metrics": {},
                "monitor_verdicts": {getattr(i, "id", None): "satisfied"
                                     for i in list(ai) + [ni]},
                "observations": [{"source": "trial_window",
                                  "sample_time": sample_t,
                                  "collection_start": float(apply_t),
                                  "collection_end": sample_t,
                                  "freshness_verdict": "fresh"}]}
    return _v


def _exec_realwrite(c):
    """A monkeypatched _execute_trial that records a REAL write on the shared
    transaction (so the finally/settle rollback path fires)."""
    def _f(feas):
        tx = c._get_txn()
        c._bind_trial_id_prewrite(tx)          # pre-write id + auth rebind
        tx.note_real_write(100.0, "power_offset")
        tx.snapshot = {"gnb1": {"power_offset_db": 0.0}}
        tx.requested_action = {"bs1_power_offset": 3.0}
        return {"success": True, "snapshot": {"gnb1": {"power_offset_db": 0.0}},
                "clipped": [], "first_write_time": 100.0,
                "applied": [{"gnb_id": "gnb1", "ue_id": None,
                             "axis": "power_offset", "value": 3.0, "ok": True}]}
    return _f


class FinalizationInsideTransactionTest(unittest.TestCase):

    def _committing(self, c, rollbacks):
        c._execute_trial = _exec_realwrite(c)
        c._validate_trial = _committing_validation(c)
        c._rollback = lambda snap: rollbacks.append(1) or True

    def test_proposer_context_failure_rolls_back_admits_nothing(self):
        c, _ = _make_coordinator()
        rollbacks = []
        self._committing(c, rollbacks)
        c._proposer_context = lambda: (_ for _ in ()).throw(
            RuntimeError("proposer boom"))
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertFalse(result["success"])
        self.assertEqual(len(rollbacks), 1)          # rolled back exactly once
        self.assertEqual(c.intent_manager.added, [])  # admitted NOTHING
        self.assertIsNone(result["committed_revision"])

    def test_whole_build_evidence_raise_fails_closed(self):
        c, _ = _make_coordinator()
        rollbacks = []
        self._committing(c, rollbacks)
        c._build_evidence = lambda result, o, r: (_ for _ in ()).throw(
            RuntimeError("build boom"))
        result = c.process_intent("throughput >= 8 Mbps")   # must NOT escape
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(len(rollbacks), 1)
        self.assertEqual(c.intent_manager.added, [])

    def test_intent_admission_rejected_rolls_back(self):
        class _RejectIM(_FakeIntentManager):
            def add(self, intent):
                return False                          # explicit rejection
        c, _ = _make_coordinator()
        c.intent_manager = _RejectIM()
        rollbacks = []
        self._committing(c, rollbacks)
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(len(rollbacks), 1)
        self.assertIsNone(result["committed_revision"])

    def test_intent_admission_raise_rolls_back(self):
        class _RaiseIM(_FakeIntentManager):
            def add(self, intent):
                raise RuntimeError("admission boom")
        c, _ = _make_coordinator()
        c.intent_manager = _RaiseIM()
        rollbacks = []
        self._committing(c, rollbacks)
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(len(rollbacks), 1)

    def test_post_write_final_s0_callback_failure_rolls_back(self):
        # a validated (pending) commit whose FINAL S0 cleanup callback throws
        # must downgrade to failsafe, roll back exactly once, and admit nothing.
        c, states = _make_coordinator()
        rollbacks = []
        self._committing(c, rollbacks)

        def _cb(old, new):
            states.append(new)
            if old == "S6" and new == "S0":
                raise RuntimeError("final S0 callback boom")
        c.on_state_change = _cb
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertIn("cleanup failed", result["error"])
        self.assertEqual(len(rollbacks), 1)          # rolled back once
        self.assertEqual(c.intent_manager.added, [])  # admitted nothing

    def test_history_finalization_failure_rolls_back(self):
        c, _ = _make_coordinator()
        rollbacks = []
        self._committing(c, rollbacks)
        # Batch E two-phase history: _prepare_history is the pre-admission
        # finalization seam whose failure must still roll back + admit nothing.
        c._prepare_history = lambda result: (_ for _ in ()).throw(
            RuntimeError("history boom"))
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(len(rollbacks), 1)
        self.assertEqual(c.intent_manager.added, [])

    def test_clean_commit_still_admits(self):
        # the deferred settlement still produces a real commit on the happy path
        c, _ = _make_coordinator()
        rollbacks = []
        self._committing(c, rollbacks)
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        self.assertTrue(result["success"])
        self.assertEqual(len(rollbacks), 0)          # commit skips rollback
        self.assertEqual(len(c.intent_manager.added), 1)


# ==========================================================================
# Item 3: UNKNOWN restore after a REAL write latches; sim/no-write does not
# ==========================================================================

class UnknownRestoreLatchTest(unittest.TestCase):

    def test_real_write_unknown_restore_latches(self):
        c, _ = _make_coordinator()
        c._execute_trial = _exec_realwrite(c)          # REAL write recorded
        c._validate_trial = lambda ni, ai: {"all_satisfied": False, "metrics": {}}
        c._rollback = lambda snap: None                # UNKNOWN restore
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertTrue(c._is_latched())               # UNKNOWN + real write
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.RESTORE_UNVERIFIED.value)

    def test_simulation_unknown_restore_does_not_latch(self):
        c, _ = _make_coordinator()
        # a no-real-write trial (simulation-style): tx.actual_real_write stays
        # False, so an UNKNOWN restore must NOT latch.
        c._execute_trial = lambda feas: {
            "success": True, "snapshot": {}, "clipped": [],
            "first_write_time": 100.0, "applied": [{"axis": "power"}]}
        c._validate_trial = lambda ni, ai: {"all_satisfied": False, "metrics": {}}
        c._rollback = lambda snap: None                # UNKNOWN restore
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertFalse(c._is_latched())              # no real write -> no latch
        self.assertNotEqual(result["terminal_outcome"],
                            TerminalOutcome.TECHNICAL_FAILSAFE.value)


# ==========================================================================
# Item 4: hard-failure phase / terminal honesty
# ==========================================================================

class HardFailureHonestyTest(unittest.TestCase):

    def test_no_recovery_sampling_when_restore_unverified(self):
        # config restore FAILS -> do NOT sample physical recovery; physical
        # UNKNOWN; terminate failsafe (RESTORE_UNVERIFIED); latched.
        order = []
        c = _txn_coordinator("recovered", False, order)   # restore False
        result = c.process_intent("throughput >= 4 Mbps")
        cyc = result["cycles"][-1]
        self.assertEqual(cyc["physical_recovery_verdict"],
                         PhysicalRecoveryVerdict.UNKNOWN.value)
        # NO recovery collect happened after the rollback
        rb = order.index("rollback")
        self.assertNotIn("collect", order[rb + 1:])
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.RESTORE_UNVERIFIED.value)
        self.assertTrue(c._is_latched())

    def test_verified_restore_not_recovered_is_typed_failsafe(self):
        order = []
        c = _txn_coordinator("stuck", True, order)         # restore ok, stuck
        result = c.process_intent("throughput >= 4 Mbps")
        cyc = result["cycles"][-1]
        self.assertEqual(cyc["config_restore_verdict"],
                         ConfigRestoreVerdict.VERIFIED.value)
        self.assertEqual(cyc["physical_recovery_verdict"],
                         PhysicalRecoveryVerdict.NOT_RECOVERED.value)
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.PHYSICAL_RECOVERY_FAILED.value)
        self.assertFalse(c._is_latched())              # config restored -> safe
        self.assertNotIn("nego_stats", cyc)            # NO negotiation

    def test_verified_restore_unknown_recovery_is_typed_failsafe(self):
        order = []
        c = _txn_coordinator("blind", True, order)         # recovery unobservable
        result = c.process_intent("throughput >= 4 Mbps")
        cyc = result["cycles"][-1]
        self.assertEqual(cyc["physical_recovery_verdict"],
                         PhysicalRecoveryVerdict.UNKNOWN.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.PHYSICAL_RECOVERY_UNKNOWN.value)

    def test_recovered_proceeds_not_failsafe(self):
        order = []
        c = _txn_coordinator("recovered", True, order)
        result = c.process_intent("throughput >= 4 Mbps")
        # RECOVERED -> proceeds per safe policy (n_max=0 -> pending-not-admitted)
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)


# ==========================================================================
# Items 5/6/7: executor-level clearance, thread-scoping, handover guard
# ==========================================================================

class ExecutorCorrectionsTest(unittest.TestCase):

    def _ex(self):
        from experiments.emulation import SimExecutor, SimTelnetGNB
        sims = {"gnb1": SimTelnetGNB(pci=0), "gnb2": SimTelnetGNB(pci=1)}
        sims["gnb1"].add_ue(0x4601)
        sims["gnb2"].add_ue(0x4602)
        return SimExecutor(sims), sims

    def test_full_readback_refuses_stale_mirror_false_clear(self):
        # item 5: the device silently rejects the recovery write (device stays
        # wrong) but the mirror is updated to target. A diff-based restore then
        # touches/audits nothing; the FULL read-back must still catch the device
        # mismatch and refuse to clear.
        ex, sims = self._ex()
        snap = ex.snapshot()                    # baseline: power 0 dB
        ex.apply_axis("gnb1", "power_offset", 5.0)
        ex.latch_failsafe("rollback unverified")
        sims["gnb1"].silent_fail_axes.add("rfatt")   # device rejects silently
        # diff-based restore updates the mirror toward target but the device is
        # stuck; a second diff-based restore would touch nothing and vacuously
        # "pass" - the full read-back does NOT:
        self.assertFalse(ex._verify_full_snapshot(snap))
        self.assertFalse(ex.recover_and_clear(snap))   # refuses to clear
        self.assertTrue(ex.is_latched)

    def test_no_public_unconditional_clear(self):
        ex, _ = self._ex()
        self.assertFalse(hasattr(ex, "clear_latch"))

    def test_verified_full_readback_clears(self):
        ex, _ = self._ex()
        snap = ex.snapshot()
        ex.apply_axis("gnb1", "power_offset", 5.0)
        ex.latch_failsafe("test")
        self.assertTrue(ex.recover_and_clear(snap))    # device really restored
        self.assertFalse(ex.is_latched)

    def test_restore_bypass_is_thread_scoped(self):
        # item 6: another thread must NOT be able to write while latched just
        # because THIS thread authorized a restore (thread-local, not global).
        ex, _ = self._ex()
        ex.latch_failsafe("test")
        ex._restore_local.active = True                # only THIS thread
        results = {}

        def worker():
            results["b"] = ex.apply_axis("gnb1", "power_offset", 3.0)
        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertIs(results["b"], False)             # other thread blocked
        # this thread is authorized
        self.assertTrue(ex.apply_axis("gnb1", "power_offset", 3.0))

    def test_trigger_handover_obeys_latch(self):
        # item 7: a handover is an ordinary write and must be refused while
        # latched.
        ex, _ = self._ex()
        ex.latch_failsafe("test")
        self.assertFalse(ex.trigger_handover("gnb1", "gnb2", 1))


# ==========================================================================
# Item 8: emitted terminal EVIDENCE carries the provenance
# ==========================================================================

class AuditEvidenceTest(unittest.TestCase):

    def test_rollback_failure_evidence_is_populated(self):
        # on a rollback-failure latch, the EMITTED terminal evidence (not just
        # the private latch dict) must carry the requested action, the verified
        # snapshot, the rollback_result, and the executor restore read-back.
        ex = _ScriptedExec(apply_script=["ok"], restore_result=False)
        c = _real_coord(
            ex, {"bs1_power_offset": 3.0},
            validate=lambda ni, ai, **kw: {"all_satisfied": False,
                                           "metrics": {}})
        result = c.process_intent("throughput >= 4 Mbps")
        self.assertTrue(c._is_latched())
        ev = result["evidence"]
        self.assertTrue(ev["snapshot"])                # verified snapshot bound
        self.assertTrue(ev["requested_action"])        # requested action bound
        self.assertIsNotNone(ev["rollback_result"])    # rollback result bound
        self.assertEqual(ev["rollback_result"]["config_restore_verdict"],
                         ConfigRestoreVerdict.FAILED.value)
        self.assertIsNotNone(ev["final_readback"])     # restore read-back bound
        # private latch audit is also populated (no longer all-None)
        latch = c._safety_latch
        self.assertTrue(latch["requested_action"])
        self.assertIsNotNone(latch["observed_readback"])
        self.assertIsNotNone(latch["restore_error"])


# ==========================================================================
# Item 1 (final): single-flight MUST cover settlement/admission
# ==========================================================================

class SingleFlightCoversSettlementTest(unittest.TestCase):

    def test_admission_reentry_is_single_flight_rejected(self):
        # A custom IntentManager.add re-enters process_intent on the SAME RLock
        # thread while the outer validated config is still pending. The nested
        # call MUST be rejected (single-flight) - no nested parse/write/admit -
        # and the outer settles exactly once.
        box = {}
        parse_calls = []
        exec_calls = []

        class _ReentrantIM(_FakeIntentManager):
            def __init__(self):
                super().__init__()
                self.nested = None

            def add(self, intent):
                if self.nested is None:                # re-enter exactly once
                    self.nested = box["c"].process_intent("nested >= 8 Mbps")
                self.added.append(intent)
                return True

        c, _ = _make_coordinator()
        box["c"] = c
        c.intent_manager = _ReentrantIM()
        orig_parse = c._parse_intent
        c._parse_intent = lambda t: parse_calls.append(t) or orig_parse(t)
        base_exec = _exec_realwrite(c)
        c._execute_trial = lambda feas: exec_calls.append(1) or base_exec(feas)
        c._validate_trial = _committing_validation(c)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(1) or True

        result = c.process_intent("outer >= 8 Mbps")
        # outer settled once as a real commit
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        self.assertEqual(len(c.intent_manager.added), 1)     # ONE admission
        self.assertEqual(len(rollbacks), 0)
        # nested was rejected BEFORE any parse / write / admission
        nested = c.intent_manager.nested
        self.assertEqual(nested["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)
        self.assertEqual(nested["terminal_reason"],
                         TerminalReason.SINGLE_FLIGHT_REJECTED.value)
        self.assertEqual(parse_calls, ["outer >= 8 Mbps"])   # no nested parse
        self.assertEqual(len(exec_calls), 1)                 # no nested write

    def test_flag_cleared_after_settlement_exception(self):
        # even if settlement raises, _episode_in_flight must be cleared so the
        # NEXT episode is not permanently locked out.
        c, _ = _make_coordinator()
        c._execute_trial = _exec_realwrite(c)
        c._validate_trial = _committing_validation(c)
        c._rollback = lambda snap: True

        def _boom(res):
            raise RuntimeError("settlement boom")
        c._settle_after_cleanup = _boom
        with self.assertRaises(RuntimeError):
            c.process_intent("throughput >= 8 Mbps")
        self.assertFalse(getattr(c, "_episode_in_flight", False))


# ==========================================================================
# Item 2 (final): admission restores the ENTIRE lifecycle on partial failure
# ==========================================================================

class AdmissionLifecycleRestoreTest(unittest.TestCase):

    def _committing(self, c):
        c._execute_trial = _exec_realwrite(c)
        c._validate_trial = _committing_validation(c)

    def test_production_manager_replacement_then_raise_is_restored(self):
        # A REAL IntentManager subclass whose add performs the true same-scope
        # REPLACEMENT (old -> history, new -> intents) and THEN raises. The
        # partial mutation must be fully undone: old restored, new absent,
        # history unchanged, and the committed intent's prior status restored.
        class _ReplaceThenRaiseIM(IntentManager):
            def add(self, intent):
                super().add(intent)                    # real replacement
                raise RuntimeError("post-replacement boom")

        c, _ = _make_coordinator()
        im = _ReplaceThenRaiseIM()
        # an existing MONITORED intent with the SAME (type, scope) as the parsed
        # commit target (throughput_goal, empty scope).
        old = _intent(8.0)
        old.status = IntentStatus.ACTIVE
        im.intents[old.id] = old
        c.intent_manager = im
        self._committing(c)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(1) or True
        # capture the committed intent + its prior status
        committed = c._parse_intent("x")["intent"]
        prior = committed.status

        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(len(rollbacks), 1)             # rolled back exactly once
        # old intent restored, new absent, history UNCHANGED
        self.assertIn(old.id, im.intents)
        self.assertIs(im.intents[old.id], old)
        self.assertNotIn(committed.id, im.intents)
        self.assertEqual(im.history, [])
        # committed intent's status restored (NOT left ACTIVE)
        self.assertEqual(committed.status, prior)
        self.assertNotEqual(committed.status, IntentStatus.ACTIVE)

    def test_fake_append_then_raise_is_restored(self):
        class _AppendThenRaiseIM(_FakeIntentManager):
            def add(self, intent):
                self.added.append(intent)              # partial mutation
                raise RuntimeError("append then boom")

        c, _ = _make_coordinator()
        c.intent_manager = _AppendThenRaiseIM()
        self._committing(c)
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(1) or True
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(len(rollbacks), 1)
        self.assertEqual(c.intent_manager.added, [])    # append undone

    def test_clean_commit_promotes_to_active(self):
        # the deferred ACTIVE promotion still happens on a successful commit
        c, _ = _make_coordinator()
        im = IntentManager()
        c.intent_manager = im
        self._committing(c)
        c._rollback = lambda snap: True
        committed = c._parse_intent("x")["intent"]
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        self.assertIn(committed.id, im.intents)
        self.assertEqual(committed.status, IntentStatus.ACTIVE)


if __name__ == "__main__":
    unittest.main()
