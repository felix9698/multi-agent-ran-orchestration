"""Batch C (P0-7): typed FSM transition table, fail-closed callbacks, and the
absolute-deadline / bounded-call wall-clock guarantee.

  * an illegal FSM transition / an unknown target state -> TechnicalFailsafe
    (latched), never a silently-created state;
  * a strict PolicyResponse enum: an unknown negotiation-policy response is
    fail-closed (never converted to accept), so no relaxation is actuated and
    the episode ends in a safe non-commit;
  * an absolute monotonic episode deadline fixed at episode start bounds the
    wall clock even when a dependency NEVER returns: a pre-actuation timeout
    ends PendingNotAdmitted, a post-actuation timeout rolls back first.

Offline deterministic fakes; the only waits are tiny bounded call timeouts.
"""

import threading
import time
import unittest

from coordinator.intent_coordinator import IntentCoordinator
from coordinator.episode_types import SafetyState, TerminalOutcome, TerminalReason
from coordinator.fsm import (
    FSMState, FSMEvent, legal_next, event_for_target, IllegalTransition,
    UnknownSystemEvent, EpisodeDeadline, OperationTimeout, call_with_timeout,
    PolicyResponse,
)
from decision.intent_model import (
    Alternative, ConstraintType, FeasibilityPrediction, Intent, IntentTarget,
    IntentType, NetworkState,
)


# --------------------------------------------------------------------------
# Pure FSM / deadline / policy unit tests
# --------------------------------------------------------------------------

class FSMTableTest(unittest.TestCase):

    def test_legal_edges(self):
        self.assertIs(legal_next(FSMState.S0, FSMEvent.BEGIN), FSMState.S1)
        self.assertIs(legal_next(FSMState.S2, FSMEvent.TRIAL), FSMState.S3)
        self.assertIs(legal_next(FSMState.S3, FSMEvent.VALIDATE), FSMState.S4)
        self.assertIs(legal_next(FSMState.S4, FSMEvent.RESOLVE), FSMState.S6)
        self.assertIs(legal_next(FSMState.S5, FSMEvent.REENTER), FSMState.S2)

    def test_failsafe_is_universal_entry(self):
        for st in FSMState:
            self.assertIs(legal_next(st, FSMEvent.FAILSAFE),
                          FSMState.TECHNICAL_FAILSAFE)

    def test_reset_returns_to_idle_except_from_sink(self):
        for st in FSMState:
            if st is FSMState.TECHNICAL_FAILSAFE:
                continue
            self.assertIs(legal_next(st, FSMEvent.RESET), FSMState.S0)

    def test_technical_failsafe_is_a_persistent_sink(self):
        # NO event leaves the sink - not even RESET (only an out-of-band
        # verified recovery does, outside the transition table).
        for ev in FSMEvent:
            if ev is FSMEvent.FAILSAFE:
                self.assertIs(legal_next(FSMState.TECHNICAL_FAILSAFE, ev),
                              FSMState.TECHNICAL_FAILSAFE)
                continue
            with self.assertRaises(IllegalTransition):
                legal_next(FSMState.TECHNICAL_FAILSAFE, ev)

    def test_illegal_edge_raises(self):
        with self.assertRaises(IllegalTransition):
            legal_next(FSMState.S0, FSMEvent.VALIDATE)   # no S0 -> validate
        with self.assertRaises(IllegalTransition):
            event_for_target(FSMState.S0, FSMState.S4)    # no legal event

    def test_unknown_state_name(self):
        self.assertIsNone(FSMState.from_name("S99"))

    def test_legal_next_typed_errors_not_attributeerror(self):
        # coordinator review D2: wrong-typed inputs return TYPED failures, never
        # an AttributeError.
        with self.assertRaises(IllegalTransition):
            legal_next("S0", FSMEvent.BEGIN)          # str state
        with self.assertRaises(UnknownSystemEvent):
            legal_next(FSMState.S0, "begin")          # str event
        with self.assertRaises(IllegalTransition):
            event_for_target("S0", FSMState.S1)       # str state
        with self.assertRaises(IllegalTransition):
            event_for_target(FSMState.S0, None)       # None target


class PolicyResponseTest(unittest.TestCase):

    def test_only_exact_accept_accepts(self):
        self.assertIs(PolicyResponse.parse("accept"), PolicyResponse.ACCEPT)
        for bad in ("reject", "ok", "ACCEPT", "yes", None, 1, object()):
            self.assertIs(PolicyResponse.parse(bad), PolicyResponse.REJECT)

    def test_is_recognized(self):
        self.assertTrue(PolicyResponse.is_recognized("accept"))
        self.assertTrue(PolicyResponse.is_recognized("reject"))
        self.assertFalse(PolicyResponse.is_recognized("weird"))


class DeadlinePrimitiveTest(unittest.TestCase):

    def test_op_timeout_never_exceeds_remaining(self):
        d = EpisodeDeadline(0.2)
        self.assertLessEqual(d.op_timeout(30.0), 0.2 + 1e-6)
        self.assertGreaterEqual(d.op_timeout(30.0), 0.0)

    def test_call_with_timeout_returns_value(self):
        self.assertEqual(call_with_timeout(lambda: 7, 1.0), 7)

    def test_call_with_timeout_abandons_non_returning(self):
        ev = threading.Event()
        t0 = time.monotonic()
        with self.assertRaises(OperationTimeout):
            call_with_timeout(lambda: ev.wait(3.0), 0.05)
        self.assertLess(time.monotonic() - t0, 1.0)   # bounded, not 3s
        ev.set()

    def test_call_with_timeout_propagates_callee_error(self):
        def boom():
            raise ValueError("callee")
        with self.assertRaises(ValueError):
            call_with_timeout(boom, 1.0)


# --------------------------------------------------------------------------
# Coordinator skeleton (real process_intent, no hardware)
# --------------------------------------------------------------------------

class _IM:
    def get_active(self):
        return []

    def get_monitored(self):
        return []

    def add(self, intent):
        return True


class _Cal:
    def get_theta_star(self, phase=None):
        return 0.5

    def get_n_max(self):
        return 2

    n_max = 2

    def record_episode(self, m):
        pass

    def get_stats(self):
        return {}


class _Collector:
    simulation_mode = False

    def collect_all(self):
        return {}


class _Exec:
    def __init__(self):
        self._latched = False

    def latch_failsafe(self, reason=""):
        self._latched = True

    @property
    def is_latched(self):
        return self._latched


class _LLM:
    def active_backend_name(self):
        return "fake"


def _intent(v=8.0):
    return Intent(type=IntentType.THROUGHPUT_GOAL,
                  target=IntentTarget(kpi_name="throughput",
                                      constraint_type=ConstraintType.MIN,
                                      target_value=v, unit="Mbps"))


def _make_coord():
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
    c.negotiation_policy = None
    c.generate_alternatives_fn = lambda i, r: []
    c.ue_collector = _Collector()
    c.executor = _Exec()
    c.llm_manager = _LLM()
    c.safety_state = SafetyState.READY
    c._safety_latch = None
    c.episode_budget_s = 5.0
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
    c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
        feasible=True, confidence=0.9, reasoning="")
    return c


# --------------------------------------------------------------------------
# FSM fail-closed (through the coordinator _transition)
# --------------------------------------------------------------------------

class FSMFailClosedTest(unittest.TestCase):

    def test_illegal_transition_is_failsafe(self):
        c = _make_coord()
        c.current_state = "S0"
        with self.assertRaises(IllegalTransition):
            c._transition("S4")               # S0 -> S4 is illegal
        self.assertTrue(c._is_latched())
        self.assertEqual(c.current_state, "S_TECHNICAL_FAILSAFE")
        self.assertTrue(c.executor.is_latched)

    def test_unknown_state_is_failsafe(self):
        c = _make_coord()
        with self.assertRaises(IllegalTransition):
            c._transition("S99_bogus")        # never silently create a state
        self.assertTrue(c._is_latched())
        self.assertEqual(c.current_state, "S_TECHNICAL_FAILSAFE")

    def test_corrupt_current_state_is_failsafe(self):
        # A corrupt CURRENT state must latch + raise, never be assumed to be S0.
        c = _make_coord()
        c.current_state = "ALIEN"
        with self.assertRaises(IllegalTransition):
            c._transition("S1")               # would be "legal" if cur==S0
        self.assertTrue(c._is_latched())
        self.assertEqual(c.current_state, "S_TECHNICAL_FAILSAFE")

    def test_latched_failsafe_refuses_ordinary_reset(self):
        # TechnicalFailsafe is a persistent sink: an ordinary _transition('S0')
        # is REFUSED while latched (only verified recovery may leave it).
        c = _make_coord()
        c._latch_failsafe({"restore_error": "test"})
        self.assertEqual(c.current_state, "S_TECHNICAL_FAILSAFE")
        with self.assertRaises(IllegalTransition):
            c._transition("S0")
        self.assertEqual(c.current_state, "S_TECHNICAL_FAILSAFE")
        with self.assertRaises(IllegalTransition):
            c._transition("S2")
        self.assertEqual(c.current_state, "S_TECHNICAL_FAILSAFE")

    def test_max_fsm_step_count_fails_closed(self):
        c = _make_coord()
        c.max_fsm_steps = 3
        c._fsm_steps = 0
        # legal transitions until the step budget is exceeded
        c.current_state = "S0"
        c._transition("S1")
        c._transition("S2")
        c._transition("S3")           # step 3 == budget, still ok
        with self.assertRaises(IllegalTransition):
            c._transition("S4")       # step 4 exceeds the budget
        self.assertTrue(c._is_latched())


# --------------------------------------------------------------------------
# Unknown policy response is fail-closed (no actuation, safe non-commit)
# --------------------------------------------------------------------------

class UnknownPolicyTest(unittest.TestCase):

    def test_unknown_policy_response_is_failsafe(self):
        # An UNKNOWN / corrupt negotiation-policy response ('maybe-ok') is a
        # broken system contract: it must raise UnknownSystemEvent and finalize
        # TechnicalFailsafe with NO actuation - NOT be silently downgraded to a
        # plain reject / PendingNotAdmitted (coordinator review).
        c = _make_coord()
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=False, confidence=0.1, reasoning="")   # route to S5
        c.generate_alternatives_fn = lambda i, r: [
            Alternative(id="a1", description="relax",
                        modified_intent=_intent(6.0))]
        consulted = []
        c.negotiation_policy = lambda alt: consulted.append(1) or "maybe-ok"
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertTrue(consulted)                     # the policy WAS consulted
        self.assertFalse(result["success"])            # nothing committed
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.UNKNOWN_SYSTEM_EVENT.value)
        # coordinator review D1: the FSM state, the terminal claim AND the
        # latches must AGREE - it must ENTER/PIN the failsafe sink, not clean up
        # to S0 with the latch false.
        self.assertTrue(c._is_latched())
        self.assertEqual(c.current_state, "S_TECHNICAL_FAILSAFE")
        self.assertTrue(c.executor.is_latched)
        # and the NEXT process_intent is BLOCKED until explicit operator recovery
        nxt = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(nxt["terminal_reason"],
                         TerminalReason.SAFETY_LATCHED.value)

    def test_recognized_reject_is_not_failsafe(self):
        # a RECOGNIZED 'reject' remains a normal, safe negotiation termination
        # (PendingNotAdmitted), distinct from an unknown response.
        c = _make_coord()
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=False, confidence=0.1, reasoning="")
        c.generate_alternatives_fn = lambda i, r: [
            Alternative(id="a1", description="relax",
                        modified_intent=_intent(6.0))]
        c.negotiation_policy = lambda alt: "reject"
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)

    def test_unknown_policy_response_direct_raises(self):
        c = _make_coord()
        alt = Alternative(id="a", description="x")
        c.negotiation_policy = lambda a: "totally-bogus"
        with self.assertRaises(UnknownSystemEvent):
            c._negotiation_decision(alt)
        # a recognized response does not raise
        c.negotiation_policy = lambda a: "reject"
        self.assertEqual(c._negotiation_decision(alt), "reject")


# --------------------------------------------------------------------------
# Bounded execution: model timeout, post-actuation timeout, absolute deadline
# --------------------------------------------------------------------------

class _BlockingLLM:
    """A non-returning backend: generate blocks on an event (bounded so the
    test can never truly hang)."""
    def __init__(self, ev):
        self.ev = ev

    def generate(self, prompt, system_prompt=""):
        self.ev.wait(3.0)
        return type("R", (), {"success": True, "content": "{}",
                              "parsed_json": {}})()

    def active_backend_name(self):
        return "blk"


class DeadlineIntegrationTest(unittest.TestCase):

    def test_model_timeout_before_actuation_is_pending_not_admitted(self):
        c = _make_coord()
        ev = threading.Event()
        c.llm_manager = _BlockingLLM(ev)
        del c._parse_intent                 # use the REAL bounded _parse_intent
        c.model_call_timeout_s = 0.05
        c.episode_budget_s = 5.0
        try:
            result = c.process_intent("throughput >= 8 Mbps")
        finally:
            ev.set()
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.DEADLINE_EXHAUSTED.value)
        self.assertFalse(result["success"])

    def test_timeout_after_actuation_rolls_back(self):
        c = _make_coord()
        c._check_conflicts = lambda n, a: True
        snapshot = {"gnb1": {"power_offset_db": 0.0}}
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True   # verified

        def _trial(feas):
            tx = c._active_txn
            tx.note_real_write(time.time())
            tx.snapshot = snapshot
            tx.applied = ({"gnb_id": "gnb1", "axis": "power_offset",
                           "ue_id": None, "value": 1.0, "ok": True},)
            # force the absolute deadline to have passed AFTER the write
            c._deadline.deadline = c._deadline.start - 1.0
            return {"success": True, "snapshot": snapshot, "clipped": [],
                    "applied": list(tx.applied)}

        c._execute_trial = _trial
        result = c.process_intent("throughput >= 8 Mbps")
        # post-actuation timeout rolled back exactly once; restore VERIFIED ->
        # PendingNotAdmitted (nothing unverified committed)
        self.assertEqual(len(rollbacks), 1)
        self.assertTrue(result["rolled_back"])
        self.assertFalse(result["success"])
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.DEADLINE_EXHAUSTED.value)

    def test_timeout_after_actuation_unverified_restore_is_failsafe(self):
        c = _make_coord()
        c._check_conflicts = lambda n, a: True
        snapshot = {"gnb1": {"power_offset_db": 0.0}}
        c._rollback = lambda snap: False           # restore FAILS -> latch

        def _trial(feas):
            tx = c._active_txn
            tx.note_real_write(time.time())
            tx.snapshot = snapshot
            tx.applied = ({"gnb_id": "gnb1", "axis": "power_offset",
                           "ue_id": None, "value": 1.0, "ok": True},)
            c._deadline.deadline = c._deadline.start - 1.0
            return {"success": True, "snapshot": snapshot, "clipped": [],
                    "applied": list(tx.applied)}

        c._execute_trial = _trial
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.TIMEOUT_UNRECOVERED.value)
        self.assertTrue(c._is_latched())

    def test_hanging_post_write_validation_is_bounded_and_rolls_back(self):
        # A REAL non-returning post-write validation (blocks on an event) must
        # return within the episode budget, roll back EXACTLY once, admit
        # nothing, and (verified restore) end PendingNotAdmitted - NOT hang and
        # NOT commit.  (Not a manually-expired deadline: the validation itself
        # blocks.)
        c = _make_coord()
        c._check_conflicts = lambda n, a: True
        c.episode_budget_s = 0.15
        snapshot = {"gnb1": {"power_offset_db": 0.0}}
        rollbacks = []
        c._rollback = lambda snap: rollbacks.append(snap) or True   # verified

        def _trial(feas):
            tx = c._active_txn
            tx.note_real_write(time.time())
            tx.snapshot = snapshot
            tx.applied = ({"gnb_id": "gnb1", "axis": "power_offset",
                           "ue_id": None, "value": 1.0, "ok": True},)
            return {"success": True, "snapshot": snapshot, "clipped": [],
                    "applied": list(tx.applied)}

        c._execute_trial = _trial
        ev = threading.Event()

        def _hang(ni, ai):
            ev.wait(3.0)                    # non-returning until the timeout
            return {"all_satisfied": True, "metrics": {}}
        c._validate_trial = _hang

        t0 = time.monotonic()
        try:
            result = c.process_intent("throughput >= 8 Mbps")
        finally:
            ev.set()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.5)                       # bounded, not 3s
        self.assertEqual(len(rollbacks), 1)                 # rolled back once
        self.assertFalse(result["success"])                 # nothing committed
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.DEADLINE_EXHAUSTED.value)

    def test_hanging_custom_alternative_generator_is_bounded(self):
        # Item 3 (coordinator review): a NON-RETURNING custom
        # generate_alternatives_fn (invoked inline by _negotiate) must NOT defeat
        # the absolute deadline - it is bounded, OperationTimeout propagates to a
        # typed PendingNotAdmitted/DeadlineExhausted, zero writes/admission.
        c = _make_coord()
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=False, confidence=0.1, reasoning="", alternatives=[])
        c.episode_budget_s = 0.12
        c.model_call_timeout_s = 30.0        # the deadline bounds it, not this
        ev = threading.Event()
        consulted = []

        def _hang(new_intent, rejected):
            consulted.append(1)
            ev.wait(3.0)                     # non-returning until the timeout
            return []
        c.generate_alternatives_fn = _hang

        t0 = time.monotonic()
        try:
            result = c.process_intent("throughput >= 8 Mbps")
        finally:
            ev.set()
        elapsed = time.monotonic() - t0
        self.assertTrue(consulted)                     # the generator WAS called
        self.assertLess(elapsed, 1.5)                  # bounded, not the 3s wait
        self.assertFalse(result["success"])
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.DEADLINE_EXHAUSTED.value)

    def test_episode_absolute_deadline_bounds_wall_clock(self):
        # A NON-RETURNING dependency (blocks 3s) cannot defeat the wall-clock
        # bound: with a 0.1s episode budget the episode returns well under it.
        c = _make_coord()
        ev = threading.Event()
        c.llm_manager = _BlockingLLM(ev)
        del c._parse_intent
        c.model_call_timeout_s = 30.0     # per-call larger; the deadline bounds
        c.episode_budget_s = 0.1
        t0 = time.monotonic()
        try:
            result = c.process_intent("throughput >= 8 Mbps")
        finally:
            ev.set()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.5)     # bounded, not the 3s block
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.DEADLINE_EXHAUSTED.value)


if __name__ == "__main__":
    unittest.main()
