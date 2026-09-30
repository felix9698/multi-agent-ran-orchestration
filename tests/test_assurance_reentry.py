"""P0-8: typed-core split + bounded continuous-assurance re-entry scheduler.

Covers:
 * process_intent_text (NL wrapper) / resolve_pending_intent (typed core, NO
   reparse) / process_intent (compatible wrapper).
 * production drift violations enqueue the EXACT typed Intent with the
   originating intent-set version, re-entry reason and originating evidence id.
 * a stable semantic dedup key + bounded pending/in-flight/completed
   suppression so duplicate violation events cannot spawn unbounded episodes.
 * a deterministic/synchronous drain that re-enters via resolve_pending_intent
   (no reparse, no recursion, no background thread), traversing the full
   pipeline: single-flight lock, absolute deadline, safety latch, FSM, commit
   invariant, terminal + evidence contracts; a latched coordinator never
   actuates.
 * trigger provenance preserved in result / evidence.
"""

import threading
import time
import unittest

from coordinator.intent_coordinator import IntentCoordinator, IntentManager
from coordinator.episode_types import (
    SafetyState, TerminalOutcome, TerminalReason,
)
from decision.intent_model import (
    ConstraintType, FeasibilityPrediction, Intent, IntentStatus, IntentTarget,
    IntentType, NetworkState,
)


class _Metric:
    def __init__(self, tput, attached=True, latency=None):
        self.throughput_mbps = tput
        self.attached = attached
        self.latency_ms = latency

    def to_dict(self):
        return {"throughput_mbps": self.throughput_mbps,
                "attached": self.attached, "latency_ms": self.latency_ms}


def _tput_intent(target=8.0, status=IntentStatus.ACTIVE, ue_ids=None) -> Intent:
    i = Intent(type=IntentType.THROUGHPUT_GOAL,
               target=IntentTarget(kpi_name="throughput",
                                   constraint_type=ConstraintType.MIN,
                                   target_value=target, unit="Mbps"))
    i.status = status
    if ue_ids is not None:
        i.scope.ue_ids = list(ue_ids)
    return i


class _CountingExecutor:
    """A stand-in executor that records any actuation attempt."""
    def __init__(self):
        self.writes = 0

    def get_all_offsets(self):
        return {}

    def snapshot(self):
        self.writes += 0
        return {}


def _reentry_coord(metrics, monitored=None, latched=False):
    """A coordinator wired so resolve_pending_intent reaches the joint-evaluated
    already-satisfied shortcut terminal with ONE observation bundle (no LLM /
    feasibility needed)."""
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.gui = None
    c.on_state_change = None
    c.on_negotiation_needed = None
    c.on_intent_violated = None
    c.current_state = "S0"
    c.history_reservoir = []
    c.max_history = 100
    c.tau_trial_s = 0.0
    c.episode_budget_s = 30.0
    c.safety_state = (SafetyState.LATCHED_FAILSAFE if latched
                      else SafetyState.READY)
    c._safety_latch = None
    c.llm_manager = None
    c.executor = _CountingExecutor()
    c.intent_manager = IntentManager()
    for m in (monitored or []):
        c.intent_manager.add(m)

    class _Collector:
        simulation_mode = False

        def __init__(self):
            self.calls = 0

        def collect_all(self):
            self.calls += 1
            return {k: v for k, v in metrics.items()}

    c.ue_collector = _Collector()
    c._check_conflicts = lambda n, a: False        # no conflict -> shortcut
    return c


class TypedCoreSplitTest(unittest.TestCase):

    def test_process_intent_delegates_to_text_wrapper(self):
        seen = {}
        c = IntentCoordinator.__new__(IntentCoordinator)

        def _text(text):
            seen["text"] = text
            return {"ok": True}
        c.process_intent_text = _text
        out = c.process_intent("throughput >= 8")
        self.assertEqual(seen["text"], "throughput >= 8")
        self.assertEqual(out, {"ok": True})

    def test_resolve_pending_intent_does_not_reparse(self):
        # the typed core NEVER calls the LLM parser
        pend = _tput_intent(target=8.0, ue_ids=["ue1"])
        c = _reentry_coord({"ue1": _Metric(9.0)})
        parse_calls = {"n": 0}

        def _boom(text):
            parse_calls["n"] += 1
            raise AssertionError("resolve_pending_intent must not reparse")
        c._parse_intent = _boom
        result = c.resolve_pending_intent(pend)
        self.assertEqual(parse_calls["n"], 0)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.ALREADY_SATISFIED_UNVERIFIED.value)
        # the typed pending intent is carried through (not a re-parsed copy)
        self.assertEqual(result["parsed_intent"]["id"], pend.id)


class AssuranceEnqueueTest(unittest.TestCase):

    def _drift_coord(self):
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.gui = None
        c.on_state_change = None
        c.on_negotiation_needed = None
        c.on_intent_violated = None
        c.current_state = "S0"
        c.history_reservoir = []
        c.max_history = 100
        c.tau_trial_s = 0.0
        c.intent_manager = IntentManager()
        return c

    def test_assurance_violation_enqueues_typed_intent(self):
        c = self._drift_coord()
        intent = _tput_intent(target=8.0, status=IntentStatus.SATISFIED,
                              ue_ids=["ue1"])
        c.intent_manager.add(intent)
        c._check_intent_satisfaction({"ue1": _Metric(3.0)})   # drift below 8
        self.assertEqual(intent.status, IntentStatus.VIOLATED)
        self.assertEqual(c.assurance_queue_depth(), 1)
        entry = c._assurance_queue[0]
        self.assertIs(entry["intent"], intent)                # EXACT typed Intent
        self.assertEqual(entry["reason"], "assurance_drift")
        self.assertTrue(entry["origin_intent_set_version"].startswith("iset-"))
        self.assertIn("dedup_key", entry)

    def test_assurance_reentry_is_deduplicated(self):
        c = self._drift_coord()
        intent = _tput_intent(target=8.0, ue_ids=["ue1"])
        # same violation event firing repeatedly collapses to ONE entry
        self.assertTrue(c.enqueue_assurance_violation(intent))
        self.assertFalse(c.enqueue_assurance_violation(intent))
        self.assertFalse(c.enqueue_assurance_violation(intent))
        self.assertEqual(c.assurance_queue_depth(), 1)
        # a DISTINCT semantic alias (same content, different id) is the same key
        alias = _tput_intent(target=8.0, ue_ids=["ue1"])
        self.assertNotEqual(alias.id, intent.id)
        self.assertFalse(c.enqueue_assurance_violation(alias))
        self.assertEqual(c.assurance_queue_depth(), 1)

    def test_queue_overflow_is_bounded_fail_closed(self):
        c = self._drift_coord()
        c.assurance_queue_max = 2
        i1 = _tput_intent(target=8.0, ue_ids=["ue1"])
        i2 = _tput_intent(target=8.0, ue_ids=["ue2"])
        i3 = _tput_intent(target=8.0, ue_ids=["ue3"])
        self.assertTrue(c.enqueue_assurance_violation(i1))
        self.assertTrue(c.enqueue_assurance_violation(i2))
        self.assertFalse(c.enqueue_assurance_violation(i3))   # dropped, bounded
        self.assertEqual(c.assurance_queue_depth(), 2)


class AssuranceDrainTest(unittest.TestCase):

    def test_assurance_reentry_does_not_reparse_intent(self):
        pend = _tput_intent(target=8.0, ue_ids=["ue1"])
        c = _reentry_coord({"ue1": _Metric(9.0)})
        parse_calls = {"n": 0}

        def _boom(text):
            parse_calls["n"] += 1
            raise AssertionError("drain must not reparse")
        c._parse_intent = _boom
        c.enqueue_assurance_violation(pend, reason="assurance_drift",
                                      evidence_id="ev-origin-1")
        results = c.drain_assurance_queue()
        self.assertEqual(parse_calls["n"], 0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["terminal_reason"],
                         TerminalReason.ALREADY_SATISFIED_UNVERIFIED.value)
        self.assertEqual(c.assurance_queue_depth(), 0)

    def test_trigger_provenance_preserved_in_result_and_evidence(self):
        pend = _tput_intent(target=8.0, ue_ids=["ue1"])
        c = _reentry_coord({"ue1": _Metric(9.0)})
        c.enqueue_assurance_violation(pend, reason="assurance_drift",
                                      evidence_id="ev-origin-9",
                                      intent_set_version="iset-origin")
        result = c.drain_assurance_queue()[0]
        tc = result["trigger_context"]
        self.assertTrue(tc["reentry"])
        self.assertEqual(tc["reentry_reason"], "assurance_drift")
        self.assertEqual(tc["origin_evidence_id"], "ev-origin-9")
        self.assertEqual(tc["origin_intent_set_version"], "iset-origin")
        # evidence record carries it too
        self.assertEqual(result["evidence"]["trigger_context"]["origin_evidence_id"],
                         "ev-origin-9")

    def test_drain_max_items_budget_leaves_remaining_queued(self):
        # max_items bounds a single drain; the rest stay queued. (The absolute-
        # deadline bounding of a hung dependency is proved in
        # AssuranceBudgetDeadlineTest.)
        c = _reentry_coord({"ue1": _Metric(9.0)})
        # three DISTINCT semantic intents (different targets -> different dedup
        # keys), all scoped to ue1 and all jointly satisfied by ue1=9.0.
        intents = [_tput_intent(target=t, ue_ids=["ue1"])
                   for t in (8.0, 7.0, 6.0)]
        for it in intents:
            c.enqueue_assurance_violation(it)
        self.assertEqual(c.assurance_queue_depth(), 3)
        first = c.drain_assurance_queue(max_items=1)
        self.assertEqual(len(first), 1)
        self.assertEqual(c.assurance_queue_depth(), 2)         # budget honoured
        rest = c.drain_assurance_queue()
        self.assertEqual(len(rest), 2)
        self.assertEqual(c.assurance_queue_depth(), 0)

    def test_latched_coordinator_never_actuates_on_drain(self):
        # latch BEFORE drain: the re-entry episode fails closed to
        # TechnicalFailsafe/SafetyLatched and NEVER collects/actuates.
        pend = _tput_intent(target=8.0, ue_ids=["ue1"])
        c = _reentry_coord({"ue1": _Metric(9.0)}, latched=True)
        c.enqueue_assurance_violation(pend)
        result = c.drain_assurance_queue()[0]
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.SAFETY_LATCHED.value)
        self.assertEqual(c.ue_collector.calls, 0)   # no observation/actuation
        self.assertEqual(c.executor.writes, 0)

    def test_no_recursive_drain_during_in_flight_episode(self):
        c = _reentry_coord({"ue1": _Metric(9.0)})
        c._episode_in_flight = True                 # pretend mid-episode
        with self.assertRaises(RuntimeError):
            c.drain_assurance_queue()

    def test_no_recursive_resolve_during_in_flight_episode(self):
        # a re-entrant resolve_pending_intent on the same thread is rejected by
        # the single-flight guard (not silently interleaved).
        pend = _tput_intent(target=8.0, ue_ids=["ue1"])
        c = _reentry_coord({"ue1": _Metric(9.0)})
        c._episode_in_flight = True
        result = c.resolve_pending_intent(pend)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.SINGLE_FLIGHT_REJECTED.value)

    def test_completed_suppression_prevents_immediate_reenqueue(self):
        pend = _tput_intent(target=8.0, ue_ids=["ue1"])
        c = _reentry_coord({"ue1": _Metric(9.0)})
        c.enqueue_assurance_violation(pend)
        c.drain_assurance_queue()
        # an immediately re-fired identical violation is suppressed (bounded
        # completed set) so it cannot spawn a second episode.
        self.assertFalse(c.enqueue_assurance_violation(pend))
        self.assertEqual(c.assurance_queue_depth(), 0)


class AssuranceRecurrenceTest(unittest.TestCase):
    """Review #4: completed suppression must not block a GENUINE later
    satisfied->violated recurrence - a verified SATISFIED transition retires the
    completed key (violation epoch closes)."""

    def _drift_coord(self):
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.gui = None
        c.on_state_change = None
        c.on_negotiation_needed = None
        c.on_intent_violated = None
        c.current_state = "S0"
        c.history_reservoir = []
        c.max_history = 100
        c.tau_trial_s = 0.0
        c.intent_manager = IntentManager()
        return c

    def test_recovered_then_violated_reenqueues_but_immediate_dup_suppressed(self):
        c = self._drift_coord()
        intent = _tput_intent(target=8.0, status=IntentStatus.SATISFIED,
                              ue_ids=["ue1"])
        c.intent_manager.add(intent)
        # simulate a PRIOR drained violation: its key is in the completed set
        key = c._assurance_dedup_key(intent)
        c._mark_assurance_completed(key)
        # an IMMEDIATE re-violation (no recovery in between) is suppressed
        c._check_intent_satisfaction({"ue1": _Metric(3.0)})   # -> VIOLATED
        self.assertEqual(intent.status, IntentStatus.VIOLATED)
        self.assertEqual(c.assurance_queue_depth(), 0)        # suppressed
        # RECOVERY: a verified SATISFIED transition retires the completed key
        c._check_intent_satisfaction({"ue1": _Metric(9.0)})   # -> SATISFIED
        self.assertEqual(intent.status, IntentStatus.SATISFIED)
        self.assertNotIn(key, c._assurance_completed_set)      # retired
        # a NEW violation now enqueues again (genuine recurrence)
        c._check_intent_satisfaction({"ue1": _Metric(3.0)})   # -> VIOLATED
        self.assertEqual(c.assurance_queue_depth(), 1)


class AssuranceOverflowStateTest(unittest.TestCase):
    """Review #5: a full queue creates OBSERVABLE fail-closed state (monotonic
    count + last-overflow audit), leaves the intent VIOLATED, and actuates
    nothing."""

    def _drift_coord(self):
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.gui = None
        c.on_state_change = None
        c.on_negotiation_needed = None
        c.on_intent_violated = None
        c.current_state = "S0"
        c.history_reservoir = []
        c.max_history = 100
        c.tau_trial_s = 0.0
        c.intent_manager = IntentManager()
        return c

    def test_overflow_records_observable_audit(self):
        c = self._drift_coord()
        c.assurance_queue_max = 1
        i1 = _tput_intent(target=8.0, ue_ids=["ue1"])
        i2 = _tput_intent(target=8.0, ue_ids=["ue2"])
        self.assertTrue(c.enqueue_assurance_violation(i1))
        self.assertFalse(c.enqueue_assurance_violation(
            i2, reason="assurance_drift", evidence_id="ev-2",
            intent_set_version="iset-2"))
        state = c.assurance_overflow_state()
        self.assertEqual(state["overflow_count"], 1)
        audit = state["last_overflow"]
        self.assertEqual(audit["dedup_key"], c._assurance_dedup_key(i2))
        self.assertEqual(audit["reason"], "assurance_drift")
        self.assertEqual(audit["origin_intent_set_version"], "iset-2")
        self.assertEqual(audit["origin_evidence_id"], "ev-2")
        self.assertIsNotNone(audit["time"])
        self.assertEqual(c.assurance_queue_depth(), 1)   # bounded

    def test_overflow_leaves_intent_violated_no_actuation(self):
        c = self._drift_coord()
        c.executor = _CountingExecutor()
        c.assurance_queue_max = 1
        first = _tput_intent(target=8.0, status=IntentStatus.SATISFIED,
                             ue_ids=["ue0"])
        drifter = _tput_intent(target=8.0, status=IntentStatus.SATISFIED,
                               ue_ids=["ue1"])
        c.intent_manager.add(first)
        c.intent_manager.add(drifter)
        # fill the queue with `first`, then drift `drifter` -> overflow
        c.enqueue_assurance_violation(first)
        c._check_intent_satisfaction({"ue0": _Metric(9.0),
                                      "ue1": _Metric(3.0)})   # ue1 drifts
        self.assertEqual(drifter.status, IntentStatus.VIOLATED)  # still violated
        self.assertGreaterEqual(c.assurance_overflow_state()["overflow_count"], 1)
        self.assertEqual(c.executor.writes, 0)               # no actuation


class AssuranceConcurrencyTest(unittest.TestCase):
    """Review #3: the queue + pending/inflight/completed/overflow/seq state are
    lock-protected; concurrent enqueue/drain never double-processes an entry,
    loses bookkeeping, or exceeds the bound."""

    def test_concurrent_enqueue_and_drain_no_duplicate_episodes(self):
        c = _reentry_coord({"ue1": _Metric(9.0)})
        c.assurance_queue_max = 1000
        # 20 DISTINCT semantic intents (distinct targets), all scoped to ue1 and
        # jointly satisfied on re-entry so each resolve reaches a shortcut
        # terminal.
        intents = [_tput_intent(target=1.0 + 0.1 * i, ue_ids=["ue1"])
                   for i in range(20)]

        # phase 1: many threads each enqueue ALL intents (heavy dedup contention)
        def _worker():
            for it in intents:
                c.enqueue_assurance_violation(it)
        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # each unique key enqueued exactly once; bookkeeping consistent; bounded
        keys = [e["dedup_key"] for e in list(c._assurance_queue)]
        self.assertEqual(len(keys), 20)
        self.assertEqual(len(set(keys)), 20)                 # no duplicates
        self.assertEqual(c.assurance_queue_depth(), 20)
        self.assertEqual(len(c._assurance_pending_keys), 20)
        self.assertEqual(c.assurance_overflow_state()["overflow_count"], 0)

        # phase 2: drain everything; exactly one episode per unique entry
        results = c.drain_assurance_queue()
        self.assertEqual(len(results), 20)                   # no duplicate episodes
        self.assertEqual(c.assurance_queue_depth(), 0)
        self.assertEqual(len(c._assurance_pending_keys), 0)
        self.assertEqual(len(c._assurance_inflight_keys), 0)
        self.assertTrue(all(
            r["terminal_reason"]
            == TerminalReason.ALREADY_SATISFIED_UNVERIFIED.value
            for r in results))

    def test_overlapping_enqueue_and_drain_processes_each_key_once(self):
        # review item C: producers and a consumer OVERLAP (Barrier-coordinated),
        # so enqueue races drain. Assert each semantic key is processed at most
        # once, the bookkeeping sets are consistent at quiescence, and the
        # observed depth never exceeds the cap. No timing-only assertions.
        K = 24
        c = _reentry_coord({"ue1": _Metric(9.0)})
        c.assurance_queue_max = K                 # cap == distinct key count
        intents = [_tput_intent(target=1.0 + 0.1 * i, ue_ids=["ue1"])
                   for i in range(K)]
        n_producers = 4
        rounds = 6
        start = threading.Barrier(n_producers + 1)   # producers + 1 consumer
        stop = threading.Event()
        processed = []                             # (parsed intent id) per episode
        proc_lock = threading.Lock()
        max_depth = [0]
        depth_lock = threading.Lock()

        def _producer():
            start.wait()
            for _ in range(rounds):
                for it in intents:
                    c.enqueue_assurance_violation(it)
                    d = c.assurance_queue_depth()
                    with depth_lock:
                        if d > max_depth[0]:
                            max_depth[0] = d

        def _consumer():
            start.wait()
            while True:
                got = c.drain_assurance_queue(max_items=1)
                if got:
                    with proc_lock:
                        processed.append(
                            got[0]["parsed_intent"]["id"])
                elif stop.is_set() and c.assurance_queue_depth() == 0:
                    break

        producers = [threading.Thread(target=_producer)
                     for _ in range(n_producers)]
        consumer = threading.Thread(target=_consumer)
        consumer.start()
        for p in producers:
            p.start()
        for p in producers:
            p.join()
        stop.set()                                 # let the consumer finish
        consumer.join(timeout=30)
        self.assertFalse(consumer.is_alive())

        # each semantic key processed AT MOST once (no double-processing)
        self.assertEqual(len(processed), len(set(processed)))
        self.assertLessEqual(len(processed), K)
        self.assertTrue(set(processed) <= {it.id for it in intents})
        # every distinct violation was eventually processed exactly once
        self.assertEqual(set(processed), {it.id for it in intents})
        # bookkeeping consistent at quiescence
        self.assertEqual(c.assurance_queue_depth(), 0)
        self.assertEqual(len(c._assurance_pending_keys), 0)
        self.assertEqual(len(c._assurance_inflight_keys), 0)
        # observed depth never exceeded the cap
        self.assertLessEqual(max_depth[0], K)
        self.assertEqual(c.assurance_overflow_state()["overflow_count"], 0)


def _hang_coord(metrics):
    """A coordinator whose re-entry FALLS THROUGH the shortcut (intent violated)
    into a cycle whose negotiation alternative-generator HANGS - so the episode
    can only end by the absolute deadline bounding the hung dependency."""
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.gui = None
    c.on_state_change = None
    c.on_negotiation_needed = None
    c.on_intent_violated = None
    c.current_state = "S0"
    c.history_reservoir = []
    c.max_history = 100
    c.tau_trial_s = 0.0
    c.episode_budget_s = 0.5              # small ABSOLUTE deadline
    c.model_call_timeout_s = 30.0         # >> deadline, so the deadline bounds
    c.policy_call_timeout_s = 30.0
    c.max_fsm_steps = 256
    c.safety_state = SafetyState.READY
    c._safety_latch = None
    c.llm_manager = None
    c.executor = _CountingExecutor()
    c.intent_manager = IntentManager()

    class _Collector:
        simulation_mode = False

        def collect_all(self):
            return {k: v for k, v in metrics.items()}
    c.ue_collector = _Collector()

    class _Cal:
        def get_theta_star(self, phase=None):
            return 0.5

        def get_n_max(self):
            return 1

        def get_stats(self):
            return {}

        def record_episode(self, m):
            pass
    c.calibrator = _Cal()
    c.negotiation_policy = lambda alt: "reject"

    def _hang(intent, rejected):
        time.sleep(3.0)                   # abandoned by the deadline bound
        return []
    c.generate_alternatives_fn = _hang
    c._check_conflicts = lambda n, a: False
    c._get_network_state = lambda: NetworkState(ue_states={})
    c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
        feasible=False, confidence=0.0, reasoning="", alternatives=[])
    c._record_episode_result = lambda r, f: None
    return c


class AssuranceOriginEvidenceTest(unittest.TestCase):
    """Review #6: a REAL commit binds the authoritative EvidenceRecord
    .actuation_trial_id to the admitted intent; a LATER drift enqueues carrying
    that id (never a caller-forged result field)."""

    def test_real_commit_then_drift_carries_actuation_trial_id(self):
        from tests.test_commit_invariant import (
            _commit_coord, _real_write_trial, _full_obs, _ok_verdicts)
        c = _commit_coord(readback=2.0)
        c.intent_manager = IntentManager()        # real manager -> monitored
        c.on_intent_violated = None
        the_intent = _tput_intent(target=8.0)     # empty scope -> all UEs
        c._parse_intent = lambda text: {"intent": the_intent, "raw": {}}
        c._execute_trial = _real_write_trial(c)

        def _v(ni, ai):
            at = getattr(c._active_txn, "action_apply_time", None) or time.time()
            return {"all_satisfied": True, "metrics": {},
                    "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(at)]}
        c._validate_trial = _v

        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        trial_id = result["evidence"]["actuation_trial_id"]
        self.assertTrue(trial_id)
        # the authoritative id is bound to the admitted intent
        self.assertEqual(c._assurance_evidence_id_for(the_intent), trial_id)
        # a LATER drift on the committed (now ACTIVE) intent enqueues with it
        c._check_intent_satisfaction({"ue1": _Metric(3.0)})
        self.assertEqual(the_intent.status, IntentStatus.VIOLATED)
        entry = c._assurance_queue[0]
        self.assertEqual(entry["origin_evidence_id"], trial_id)

    def test_inplace_mutation_does_not_misattribute_old_evidence(self):
        # review item B: after commit binds (id, content_hash) -> trial_id, an
        # in-place target/scope mutation must NOT claim the old evidence.
        from tests.test_commit_invariant import (
            _commit_coord, _real_write_trial, _full_obs, _ok_verdicts)
        c = _commit_coord(readback=2.0)
        c.intent_manager = IntentManager()
        c.on_intent_violated = None
        the_intent = _tput_intent(target=8.0)
        c._parse_intent = lambda text: {"intent": the_intent, "raw": {}}
        c._execute_trial = _real_write_trial(c)

        def _v(ni, ai):
            at = getattr(c._active_txn, "action_apply_time", None) or time.time()
            return {"all_satisfied": True, "metrics": {},
                    "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(at)]}
        c._validate_trial = _v

        result = c.process_intent("throughput >= 8 Mbps")
        trial_id = result["evidence"]["actuation_trial_id"]
        self.assertEqual(c._assurance_evidence_id_for(the_intent), trial_id)
        # MUTATE the committed intent's target in place (semantics changed)
        the_intent.target.target_value = 42.0
        # the old evidence is NO LONGER attributed to the changed intent
        self.assertIsNone(c._assurance_evidence_id_for(the_intent))
        # a drift now enqueues with a None origin evidence id (not the stale one)
        c._check_intent_satisfaction({"ue1": _Metric(3.0)})
        self.assertEqual(the_intent.status, IntentStatus.VIOLATED)
        self.assertIsNone(c._assurance_queue[0]["origin_evidence_id"])


class CommittedReentryProvenanceTest(unittest.TestCase):
    """Review bug 2: a typed re-entry that reaches CommitOriginal must preserve
    the trigger_context in result AND the authoritative evidence AND the last
    cycle - rebuilt from the TRUSTED transaction copy, so a forged provisional
    result/cycle trigger cannot survive. Direct episodes stay ctx None/absent."""

    def _committing(self):
        from tests.test_commit_invariant import (
            _commit_coord, _real_write_trial, _full_obs, _ok_verdicts)
        c = _commit_coord(readback=2.0)
        c.on_intent_violated = None
        c._execute_trial = _real_write_trial(c)

        def _v(ni, ai):
            at = getattr(c._active_txn, "action_apply_time", None) or time.time()
            return {"all_satisfied": True, "metrics": {},
                    "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(at)]}
        c._validate_trial = _v
        return c

    def test_real_write_reentry_commit_preserves_trigger_context(self):
        c = self._committing()
        intent = _tput_intent(target=8.0)
        ctx = {"reentry": True, "reentry_reason": "assurance_drift",
               "origin_evidence_id": "ev-orig-7",
               "origin_intent_set_version": "iset-orig"}
        result = c.resolve_pending_intent(intent, trigger_context=ctx)
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        # result, authoritative evidence, and last cycle ALL carry the ctx
        self.assertEqual(result["trigger_context"]["reentry_reason"],
                         "assurance_drift")
        self.assertEqual(result["evidence"]["trigger_context"]["origin_evidence_id"],
                         "ev-orig-7")
        self.assertEqual(result["cycles"][-1]["trigger_context"]
                         ["origin_intent_set_version"], "iset-orig")

    def test_forged_provisional_trigger_cannot_survive_reflection(self):
        # an adversarial provisional finalizer forges result/cycle trigger data;
        # the authoritative reflection (from the trusted tx copy) overrides it.
        c = self._committing()
        real_final = c._finalize_episode
        calls = {"n": 0}

        def _fake(result, outcome, reason, *, pending_intent=None,
                  committed_revision=None):
            r = real_final(result, outcome, reason, pending_intent=pending_intent,
                           committed_revision=committed_revision)
            calls["n"] += 1
            if calls["n"] >= 2:                     # during the re-stamp
                result["trigger_context"] = {"reentry_reason": "FORGED",
                                             "origin_evidence_id": "FORGED"}
                cyc = result.get("cycles") or []
                if cyc:
                    cyc[-1]["trigger_context"] = {"reentry_reason": "FORGED"}
            return r
        c._finalize_episode = _fake

        intent = _tput_intent(target=8.0)
        ctx = {"reentry": True, "reentry_reason": "assurance_drift"}
        result = c.resolve_pending_intent(intent, trigger_context=ctx)
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        self.assertEqual(result["evidence"]["trigger_context"]["reentry_reason"],
                         "assurance_drift")           # NOT forged
        self.assertEqual(result["trigger_context"]["reentry_reason"],
                         "assurance_drift")
        self.assertEqual(result["cycles"][-1]["trigger_context"]["reentry_reason"],
                         "assurance_drift")
        self.assertNotIn("origin_evidence_id",
                         result["evidence"]["trigger_context"])   # trusted ctx only

    def test_direct_commit_has_no_trigger_context(self):
        c = self._committing()
        result = c.process_intent("throughput >= 8 Mbps")     # direct, no ctx
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        self.assertIsNone(result.get("trigger_context"))
        self.assertIsNone(result["evidence"]["trigger_context"])
        self.assertIsNone(result["cycles"][-1].get("trigger_context"))


class ReentryRollbackProvenanceTest(unittest.TestCase):
    """Review gaps 2/3: a real-write typed re-entry that ROLLS BACK must carry
    the trusted trigger_context on the terminal cycle (verified restore, no
    latch) and in the LATCH audit (failed/unknown restore -> TechnicalFailsafe),
    rebuilt from the transaction - a forged result/cycle field cannot alter it."""

    def _coord(self, restore_ok):
        from tests.test_safety_transaction import _make_coordinator, _trial_ok
        c, _ = _make_coordinator()
        c.on_intent_violated = None
        c._execute_trial = _trial_ok
        c._validate_trial = lambda ni, ai: {"all_satisfied": False, "metrics": {}}
        c._rollback = ((lambda snap: True) if restore_ok
                       else (lambda snap: False))
        return c

    def test_verified_rollback_reentry_cycle_and_evidence_carry_ctx(self):
        c = self._coord(restore_ok=True)
        intent = _tput_intent(target=8.0)
        ctx = {"reentry": True, "reentry_reason": "assurance_drift",
               "origin_evidence_id": "ev-r1"}
        result = c.resolve_pending_intent(intent, trigger_context=ctx)
        self.assertFalse(c._is_latched())              # verified restore, no latch
        self.assertFalse(result["success"])
        self.assertEqual(result["cycles"][-1]["trigger_context"]["reentry_reason"],
                         "assurance_drift")
        self.assertEqual(
            result["evidence"]["trigger_context"]["origin_evidence_id"], "ev-r1")

    def test_failed_restore_latch_audit_carries_ctx(self):
        c = self._coord(restore_ok=False)
        intent = _tput_intent(target=8.0)
        ctx = {"reentry": True, "reentry_reason": "assurance_drift",
               "origin_evidence_id": "ev-r2"}
        result = c.resolve_pending_intent(intent, trigger_context=ctx)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.RESTORE_UNVERIFIED.value)
        self.assertTrue(c._is_latched())
        # the latch audit preserves the re-entry provenance across the failsafe
        self.assertEqual(c._safety_latch["trigger_context"]["origin_evidence_id"],
                         "ev-r2")
        self.assertEqual(c._safety_latch["trigger_context"]["reentry_reason"],
                         "assurance_drift")

    def test_forged_result_cycle_cannot_alter_cycle_or_latch_audit(self):
        # a rollback-time hook forges result/cycle trigger data; the tx-based
        # cycle stash and latch audit ignore it.
        c = self._coord(restore_ok=False)

        def _rb(snap):
            r = getattr(c, "_active_result", None)
            if isinstance(r, dict):
                r["trigger_context"] = {"reentry_reason": "FORGED"}
                cyc = r.get("cycles") or []
                if cyc:
                    cyc[-1]["trigger_context"] = {"reentry_reason": "FORGED"}
            return False                               # fail restore -> latch
        c._rollback = _rb
        intent = _tput_intent(target=8.0)
        ctx = {"reentry": True, "reentry_reason": "assurance_drift",
               "origin_evidence_id": "ev-forge"}
        result = c.resolve_pending_intent(intent, trigger_context=ctx)
        self.assertTrue(c._is_latched())
        # EVERY terminal surface carries the TRUSTED ctx, not the forgery:
        # result, evidence, terminal cycle, AND latch audit.
        self.assertEqual(result["trigger_context"]["reentry_reason"],
                         "assurance_drift")
        self.assertEqual(result["evidence"]["trigger_context"]["origin_evidence_id"],
                         "ev-forge")
        self.assertEqual(result["cycles"][-1]["trigger_context"]["reentry_reason"],
                         "assurance_drift")
        self.assertEqual(c._safety_latch["trigger_context"]["reentry_reason"],
                         "assurance_drift")
        # NO forged value survives ANYWHERE in the returned result
        self.assertNotIn("FORGED", str(result))

    def test_pre_actuation_no_tx_reentry_preserves_coordinator_ctx(self):
        # a typed re-entry that fails BEFORE any transaction is created (an
        # unsupported intent type) has no tx, so the authoritative provenance
        # comes from the coordinator-owned _active_trigger_context.
        c = _reentry_coord({"ue1": _Metric(9.0)})
        bad = Intent(type=IntentType.RSRP_CONSTRAINT,
                     target=IntentTarget(kpi_name="rsrp",
                                         constraint_type=ConstraintType.MAX,
                                         target_value=-90.0))
        ctx = {"reentry": True, "reentry_reason": "assurance_drift",
               "origin_evidence_id": "ev-noTx"}
        result = c.resolve_pending_intent(bad, trigger_context=ctx)
        self.assertIsNone(getattr(c, "_active_txn", None))    # NO transaction
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.UNSUPPORTED_INTENT.value)
        self.assertEqual(result["trigger_context"]["origin_evidence_id"],
                         "ev-noTx")
        self.assertEqual(
            result["evidence"]["trigger_context"]["origin_evidence_id"], "ev-noTx")

    def test_direct_rollback_has_no_cycle_or_latch_ctx(self):
        c = self._coord(restore_ok=False)
        result = c.process_intent("throughput >= 8 Mbps")   # direct, no ctx
        self.assertTrue(c._is_latched())
        self.assertEqual(c._safety_latch.get("trigger_context"), {})
        self.assertNotIn("trigger_context", result["cycles"][-1])
        self.assertIsNone(result.get("trigger_context"))
        self.assertIsNone(result["evidence"]["trigger_context"])   # evidence too


class AssuranceBudgetDeadlineTest(unittest.TestCase):

    def test_assurance_reentry_respects_budget_and_deadline(self):
        # a hung dependency on a typed re-entry is BOUNDED by the episode
        # absolute deadline and reaches a typed terminal; drain max_items leaves
        # the remaining queue entries.
        c = _hang_coord({"ue1": _Metric(3.0)})    # violated -> falls through
        a = _tput_intent(target=8.0, ue_ids=["ue1"])
        b = _tput_intent(target=7.0, ue_ids=["ue1"])
        c.enqueue_assurance_violation(a)
        c.enqueue_assurance_violation(b)
        self.assertEqual(c.assurance_queue_depth(), 2)

        t0 = time.time()
        first = c.drain_assurance_queue(max_items=1)
        elapsed = time.time() - t0
        # bounded by the ~0.5s absolute deadline (NOT the 3s generator sleep)
        self.assertLess(elapsed, 2.5)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["terminal_reason"],
                         TerminalReason.DEADLINE_EXHAUSTED.value)  # typed terminal
        self.assertFalse(first[0]["success"])
        self.assertEqual(c.executor.writes, 0)                 # nothing actuated
        # max_items budget left the second entry queued
        self.assertEqual(c.assurance_queue_depth(), 1)


if __name__ == "__main__":
    unittest.main()
