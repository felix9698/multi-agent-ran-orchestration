"""Batch B: safety transaction, rollback containment, and the safety latch.

Covers section 3.4 and P0-1 (rollback failure -> persistent TechnicalFailsafe
latch), P0-2 (one first-write-through-commit transaction; exactly-once rollback;
verified commit skips rollback), and the callback-isolation requirement (optional
GUI/log callbacks are isolated; the S4 state notification fails CLOSED via the
transaction backstop).

Skeleton coordinator: __new__ skips __init__ so no hardware/LLM is built, but
the REAL process_intent S0-S6 flow, the _coordination_cycle transaction, the
rollback transaction, and the finalizer all run.  P0-3 (hard-failure ordering /
physical recovery) lives in test_hard_failure_recovery.py.
"""

import hashlib
import unittest

from coordinator.intent_coordinator import IntentCoordinator
from coordinator.episode_types import (
    ConfigRestoreVerdict, SafetyState, TerminalOutcome, TerminalReason,
)
from decision.intent_model import (
    Alternative, ConstraintType, FeasibilityPrediction, Intent, IntentTarget,
    IntentType, NetworkState,
)


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------

class _FakeIntentManager:
    def __init__(self):
        self.added = []

    def get_active(self):
        return []

    def get_monitored(self):
        return []

    def add(self, intent):
        self.added.append(intent)


class _FakeCalibrator:
    def __init__(self, theta=0.5, n_max=2):
        self.theta = theta
        self.n_max = n_max
        self.recorded = []

    def get_theta_star(self, phase=None):
        return self.theta

    def can_negotiate_more(self, rounds, phase=None):
        return rounds < self.n_max

    def record_episode(self, metrics):
        self.recorded.append(metrics)

    def get_stats(self):
        return {}


class _FakeCollector:
    simulation_mode = False

    def __init__(self, metrics=None):
        self.metrics = metrics or {}

    def collect_all(self):
        return dict(self.metrics)


class _FakeLLM:
    def active_backend_name(self):
        return "fake-model"


class _FakeExecutor:
    """Only what the coordinator safety path touches: the executor latch +
    the verified recover-and-clear operation (item 5)."""

    def __init__(self):
        self._latched = False
        self.latch_calls = 0
        self.recover_calls = 0
        self.recover_verified = True      # controls recover_and_clear result
        self.raise_on_recover = False

    def latch_failsafe(self, reason=""):
        self._latched = True
        self.latch_calls += 1

    def recover_and_clear(self, target):
        # the ONLY clear path: clears the latch IFF full verification passes
        self.recover_calls += 1
        if self.raise_on_recover:
            raise RuntimeError("executor clearance boom")
        if self.recover_verified:
            self._latched = False
            return True
        return False

    @property
    def is_latched(self):
        return self._latched


def _make_intent(value=8.0) -> Intent:
    return Intent(type=IntentType.THROUGHPUT_GOAL,
                  target=IntentTarget(kpi_name="throughput",
                                      constraint_type=ConstraintType.MIN,
                                      target_value=value, unit="Mbps"))


def _make_coordinator(states=None, alt=None):
    states = states if states is not None else []
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.gui = None
    c.on_negotiation_needed = None
    c.on_state_change = lambda old, new: states.append(new)
    c.current_state = "S0"
    c.history_reservoir = []
    c.max_history = 100
    c.tau_trial_s = 0.0
    c.intent_manager = _FakeIntentManager()
    c.calibrator = _FakeCalibrator()
    c.negotiation_policy = None            # default auto-accept resolution
    c.generate_alternatives_fn = lambda intent, rejected: []
    c.ue_collector = _FakeCollector({})
    c.executor = _FakeExecutor()
    c.llm_manager = _FakeLLM()
    c.safety_state = SafetyState.READY
    c._safety_latch = None

    intent = _make_intent()
    if alt is None:
        alt = Alternative(id="a1", description="relax I2 to 6 Mbps")
    c._parse_intent = lambda text: {"intent": intent, "raw": {}}
    c._check_conflicts = lambda new, active: True   # skip S1 shortcut
    c._get_network_state = lambda: NetworkState(ue_states={})
    def _af(i, a, s):
        # honest double: a generated proposal stamps its REAL prompt hash +
        # proposal-generated state (as production _analyze_feasibility does), so
        # the pre-write S3 invariant (P1-6) sees a bound prompt hash.
        c._cur_proposal_generated = True
        c._cur_schema_valid = True   # generated-proposal schema verdict (real bool)
        c._cur_prompt_hash = hashlib.sha256(b"safety-transaction-test").hexdigest()
        return FeasibilityPrediction(
            feasible=True, confidence=0.9, reasoning="", alternatives=[alt])
    c._analyze_feasibility = _af
    return c, states


def _trial_ok(feas):
    return {"success": True, "snapshot": {"gnb1": {"power_offset_db": 0.0}},
            "clipped": [], "applied": [{"axis": "power"}],
            "first_write_time": 100.0}


# --------------------------------------------------------------------------
# P0-1: rollback failure -> persistent TechnicalFailsafe latch
# --------------------------------------------------------------------------

class RollbackLatchTest(unittest.TestCase):

    def test_rollback_failure_latches_failsafe(self):
        # Counterexample (P0-1): a validation-fail whose restore returns False
        # must LATCH TechnicalFailsafe - not resume negotiation.
        c, _ = _make_coordinator()
        c._execute_trial = _trial_ok
        c._validate_trial = lambda ni, ai: {"all_satisfied": False, "metrics": {}}
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(1) or False  # restore FAILS
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.RESTORE_UNVERIFIED.value)
        self.assertFalse(result["success"])
        self.assertTrue(c._is_latched())
        self.assertIs(c.safety_state, SafetyState.LATCHED_FAILSAFE)
        self.assertTrue(c.executor.is_latched)          # executor latch engaged
        self.assertEqual(len(rollbacks), 1)             # exactly one rollback
        # audit record carries the failed restore
        self.assertIsNotNone(c._safety_latch)
        self.assertIn("baseline_snapshot", c._safety_latch)

    def test_latched_failsafe_blocks_new_episode(self):
        # A latched coordinator refuses the NEXT process_intent without any
        # parse / LLM / write.
        c, _ = _make_coordinator()
        c.safety_state = SafetyState.LATCHED_FAILSAFE
        c._safety_latch = {"baseline_snapshot": {"gnb1": {}}}
        parsed = []
        c._parse_intent = lambda text: parsed.append(1) or {
            "intent": _make_intent(), "raw": {}}
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.SAFETY_LATCHED.value)
        self.assertFalse(result["success"])
        self.assertEqual(parsed, [])                    # no parse ran
        self.assertEqual(result["pending_intent"], "throughput >= 8 Mbps")

    def test_rollback_failure_blocks_negotiation(self):
        # After a restore failure, NO negotiation policy is consulted and NO
        # further trial runs (no LLM/alternate/cycle/write).
        relaxed = _make_intent(6.0)
        alt = Alternative(id="a6", description="relax to 6",
                          modified_intent=relaxed)
        c, _ = _make_coordinator(alt=alt)
        executions = []
        c._execute_trial = lambda feas: executions.append(1) or _trial_ok(feas)
        c._validate_trial = lambda ni, ai: {"all_satisfied": False, "metrics": {}}
        c._rollback = lambda snap: False               # restore FAILS
        consulted = []
        c.negotiation_policy = lambda a: consulted.append(1) or "accept"
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(consulted, [])                 # negotiation blocked
        self.assertEqual(len(executions), 1)            # no re-execution
        self.assertEqual(len(result["cycles"]), 1)

    def test_partial_apply_restore_failure_is_failsafe(self):
        # A PARTIAL apply whose restore fails goes through the SAME rollback
        # transaction and latches (P0-1 step 7).  Here _execute_trial routes
        # the partial-apply restore through the real _run_rollback_transaction
        # (whose _rollback returns False), so it latches before returning.
        c, _ = _make_coordinator()

        def _partial(feas):
            outcome = c._run_rollback_transaction(
                {"gnb1": {"power_offset_db": 0.0}}, None,
                first_write_time=100.0,
                audit_extra={"failed_axis": "partial_apply"})
            return {"success": False,
                    "snapshot": {"gnb1": {"power_offset_db": 0.0}},
                    "clipped": [], "applied": [{"ok": False}],
                    "rollback_result": outcome.to_dict(),
                    "restore_verified": outcome.restore_verified,
                    "config_restore_verdict":
                        outcome.config_restore_verdict.value}

        c._execute_trial = _partial
        c._rollback = lambda snap: False               # restore FAILS
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.RESTORE_UNVERIFIED.value)
        self.assertTrue(c._is_latched())
        self.assertTrue(result["rolled_back"])
        # the cycle absorbed the partial-apply verdict
        cyc = result["cycles"][-1]
        self.assertEqual(cyc["config_restore_verdict"],
                         ConfigRestoreVerdict.FAILED.value)

    def test_operator_clear_requires_verified_recovery(self):
        # Only an operator recovery API that FULLY re-reads and VERIFIES the
        # target may release the latch; an unverified recovery keeps BOTH
        # latches engaged (item 5). READY/cleared is set only AFTER the executor
        # latch is actually cleared.
        c, _ = _make_coordinator()
        c._execute_trial = _trial_ok
        c._validate_trial = lambda ni, ai: {"all_satisfied": False, "metrics": {}}
        c._rollback = lambda snap: False               # config restore FAILS
        c.process_intent("throughput >= 8 Mbps")
        self.assertTrue(c._is_latched())
        self.assertTrue(c.executor.is_latched)

        # recovery still failing (full read-back does NOT verify) -> both stay
        c.executor.recover_verified = False
        res1 = c.clear_safety_latch()
        self.assertFalse(res1.cleared)
        self.assertIs(res1.verdict, ConfigRestoreVerdict.FAILED)
        self.assertTrue(c._is_latched())
        self.assertTrue(c.executor.is_latched)

        # operator fixed the box: full read-back verifies -> latch released
        c.executor.recover_verified = True
        res2 = c.clear_safety_latch()
        self.assertTrue(res2.cleared)
        self.assertIs(res2.verdict, ConfigRestoreVerdict.VERIFIED)
        self.assertFalse(c._is_latched())
        self.assertIs(c.safety_state, SafetyState.READY)
        self.assertFalse(c.executor.is_latched)
        self.assertEqual(c.current_state, "S0")

        # a fresh episode now runs (no longer blocked)
        c._validate_trial = lambda ni, ai: {"all_satisfied": True, "metrics": {}, "monitor_verdicts": {getattr(i, "id", None): "satisfied" for i in list(ai) + [ni]}}
        c._rollback = lambda snap: True
        res3 = c.process_intent("throughput >= 8 Mbps")
        self.assertNotEqual(res3["terminal_reason"],
                            TerminalReason.SAFETY_LATCHED.value)

    def test_executor_clear_exception_leaves_both_latched(self):
        # item 5: an executor clearance EXCEPTION must leave BOTH latches
        # engaged and return cleared=False (never a half-cleared state).
        c, _ = _make_coordinator()
        c._execute_trial = _trial_ok
        c._validate_trial = lambda ni, ai: {"all_satisfied": False, "metrics": {}}
        c._rollback = lambda snap: False
        c.process_intent("throughput >= 8 Mbps")
        self.assertTrue(c._is_latched())
        c.executor.raise_on_recover = True
        res = c.clear_safety_latch()
        self.assertFalse(res.cleared)
        self.assertIs(res.verdict, ConfigRestoreVerdict.FAILED)
        self.assertTrue(c._is_latched())          # coordinator latch stays
        self.assertTrue(c.executor.is_latched)    # executor latch stays

    def test_clear_latch_noop_when_not_latched(self):
        c, _ = _make_coordinator()
        res = c.clear_safety_latch()
        self.assertFalse(res.cleared)
        self.assertIs(res.verdict, ConfigRestoreVerdict.UNKNOWN)


# --------------------------------------------------------------------------
# P0-2: one first-write-through-commit transaction; exactly-once rollback
# --------------------------------------------------------------------------

class _RaisingGUI:
    """A GUI whose update_state raises ONLY on a chosen state; log/analysis
    are pure-display (isolated).  Used to prove a post-write state-notification
    exception fails closed (rollback)."""

    def __init__(self, raise_on="S4"):
        self.raise_on = raise_on
        self.logs = []

    def update_state(self, state):
        if state == self.raise_on:
            raise RuntimeError(f"GUI update_state boom at {state}")

    def log(self, msg):
        self.logs.append(msg)

    def update_llm_analysis(self, *_):
        pass

    def update_calibration(self, *_):
        pass


class TransactionRollbackTest(unittest.TestCase):

    def _wire_committing(self, c, rollbacks):
        c._execute_trial = _trial_ok
        c._validate_trial = lambda ni, ai: {"all_satisfied": True, "metrics": {}, "monitor_verdicts": {getattr(i, "id", None): "satisfied" for i in list(ai) + [ni]}}
        c._rollback = lambda snap: rollbacks.append(1) or None

    def test_s4_transition_callback_exception_rolls_back(self):
        # Counterexample (P0-2): an S4 state callback exception used to leave
        # the action applied with rollback count 0.  Now it rolls back once.
        states = []

        def _cb(old, new):
            states.append(new)
            if new == "S4":
                raise RuntimeError("S4 state callback boom")

        c, _ = _make_coordinator()
        c.on_state_change = _cb
        rollbacks = []
        self._wire_committing(c, rollbacks)
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(len(rollbacks), 1)             # exactly one rollback
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertFalse(result["success"])
        self.assertTrue(result.get("rolled_back") or
                        result["cycles"][-1].get("rolled_back"))

    def test_post_write_gui_exception_rolls_back(self):
        # A POST-write GUI state notification that raises fails closed through
        # the transaction backstop (exactly-once rollback), never orphaning the
        # applied action.  (Pure-display log calls stay isolated - see
        # CallbackIsolationTest.)
        c, _ = _make_coordinator()
        c.gui = _RaisingGUI(raise_on="S4")
        rollbacks = []
        self._wire_committing(c, rollbacks)
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(len(rollbacks), 1)
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)

    def test_validation_consumption_exception_rolls_back(self):
        c, _ = _make_coordinator()
        rollbacks = []
        c._execute_trial = _trial_ok

        def _boom(ni, ai):
            raise RuntimeError("validation consumption boom")

        c._validate_trial = _boom
        c._rollback = lambda snap: rollbacks.append(1) or None
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(len(rollbacks), 1)
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)

    def test_transaction_rolls_back_exactly_once(self):
        # Even with an exception AND a subsequent non-commit exit, the finally
        # rolls back exactly once (no double rollback).
        c, _ = _make_coordinator()
        rollbacks = []
        c._execute_trial = _trial_ok
        c._validate_trial = lambda ni, ai: (_ for _ in ()).throw(
            RuntimeError("boom"))
        c._rollback = lambda snap: rollbacks.append(1) or None
        c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(len(rollbacks), 1)

    def test_verified_commit_skips_rollback(self):
        # The verified commit is the ONLY path that skips rollback.
        c, _ = _make_coordinator()
        rollbacks = []
        self._wire_committing(c, rollbacks)
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        self.assertTrue(result["success"])
        self.assertEqual(len(rollbacks), 0)             # no rollback on commit
        self.assertIs(c.safety_state, SafetyState.READY)
        self.assertFalse(c._is_latched())


# --------------------------------------------------------------------------
# Callback isolation: optional GUI/log callbacks must not break control
# --------------------------------------------------------------------------

class CallbackIsolationTest(unittest.TestCase):

    def test_gui_log_exception_does_not_break_commit(self):
        # A flaky GUI *log* (pure display) must NOT abort the control
        # transaction: the commit still succeeds and no rollback happens.
        class _LogBoomGUI:
            def __init__(self):
                self.n = 0

            def update_state(self, state):
                pass

            def log(self, msg):
                self.n += 1
                raise RuntimeError("gui.log boom")

            def update_llm_analysis(self, *_):
                raise RuntimeError("gui analysis boom")

            def update_calibration(self, *_):
                raise RuntimeError("gui calibration boom")

        c, _ = _make_coordinator()
        c.gui = _LogBoomGUI()
        rollbacks = []
        c._execute_trial = _trial_ok
        c._validate_trial = lambda ni, ai: {"all_satisfied": True, "metrics": {}, "monitor_verdicts": {getattr(i, "id", None): "satisfied" for i in list(ai) + [ni]}}
        c._rollback = lambda snap: rollbacks.append(1) or None
        result = c.process_intent("throughput >= 8 Mbps")
        # log/analysis/calibration exceptions were ISOLATED -> commit still ran
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        self.assertTrue(result["success"])
        self.assertEqual(len(rollbacks), 0)
        self.assertGreater(c.gui.n, 0)                  # log WAS attempted

    def test_unknown_gui_method_is_ignored(self):
        # _safe_gui tolerates a GUI missing an optional display method.
        class _PartialGUI:
            def update_state(self, state):
                pass
        c, _ = _make_coordinator()
        c.gui = _PartialGUI()
        c._execute_trial = _trial_ok
        c._validate_trial = lambda ni, ai: {"all_satisfied": True, "metrics": {}, "monitor_verdicts": {getattr(i, "id", None): "satisfied" for i in list(ai) + [ni]}}
        c._rollback = lambda snap: None
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertTrue(result["success"])


# --------------------------------------------------------------------------
# P0-1 step 9: the executor's OWN latch blocks DIRECT write APIs
# --------------------------------------------------------------------------

class ExecutorDirectWriteLatchTest(unittest.TestCase):

    def _make_executor(self):
        from experiments.emulation import SimExecutor, SimTelnetGNB
        sims = {"gnb1": SimTelnetGNB(pci=0), "gnb2": SimTelnetGNB(pci=1)}
        sims["gnb1"].add_ue(0x4601)
        sims["gnb2"].add_ue(0x4602)
        return SimExecutor(sims)

    def test_executor_direct_write_latch(self):
        ex = self._make_executor()
        # a healthy write works before latching
        self.assertTrue(ex.apply_axis("gnb1", "power_offset", 3.0))
        snap = ex.snapshot()

        ex.latch_failsafe("rollback unverified")
        self.assertTrue(ex.is_latched)
        # EVERY ordinary write API is refused - even reaching the executor
        # directly, outside the coordinator object (P0-1 step 9)
        self.assertFalse(ex.apply_axis("gnb1", "power_offset", 5.0))
        self.assertFalse(ex.set_power_offset("gnb1", 5.0))
        self.assertFalse(ex.set_tx_att("gnb1", 20.0))
        self.assertFalse(ex.set_prb_allocation("gnb1", 40))
        self.assertFalse(ex.set_sched_priority("gnb1", 2.0, rnti=0x4601))
        self.assertFalse(ex.set_mcs_cap("gnb1", 20))
        self.assertFalse(ex.set_mcs_offset("gnb1", -4))
        self.assertFalse(ex.apply_axis("gnb1", "prb", 24, rnti=0x4601))
        # the mirror did NOT move (nothing was written)
        self.assertAlmostEqual(ex.states["gnb1"].power_offset_db, 3.0)

    def test_restore_bypasses_latch_for_recovery(self):
        # restore() IS the recovery path: it must succeed even while latched.
        ex = self._make_executor()
        snap = ex.snapshot()
        ex.apply_axis("gnb1", "power_offset", 5.0)
        ex.latch_failsafe("test")
        self.assertFalse(ex.apply_axis("gnb1", "power_offset", 7.0))  # blocked
        self.assertTrue(ex.restore(snap))                # recovery permitted
        self.assertAlmostEqual(ex.states["gnb1"].power_offset_db, 0.0)

    def test_recover_and_clear_restores_writability(self):
        # item 5: the ONLY clear path is recover_and_clear, which re-applies the
        # target AND full-verifies it before clearing.
        ex = self._make_executor()
        snap = ex.snapshot()
        ex.apply_axis("gnb1", "power_offset", 3.0)
        ex.latch_failsafe("test")
        self.assertFalse(ex.apply_axis("gnb1", "power_offset", 5.0))   # blocked
        self.assertTrue(ex.recover_and_clear(snap))    # full verify -> cleared
        self.assertFalse(ex.is_latched)
        self.assertTrue(ex.apply_axis("gnb1", "power_offset", 4.0))    # writable

    def test_no_public_unconditional_clear_latch(self):
        # item 5: there is deliberately NO public unconditional clear bypass.
        ex = self._make_executor()
        self.assertFalse(hasattr(ex, "clear_latch"))


if __name__ == "__main__":
    unittest.main()
