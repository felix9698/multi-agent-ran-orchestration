#!/usr/bin/env python3
"""Batch E (P0-16): model hot-swap PROPOSAL BOUNDARY.

The proposer/model is PINNED (backend object identity) for a whole cycle; a
switch requested mid-cycle is QUEUED and applied atomically only at the NEXT
proposal boundary. In-flight action + terminal evidence keep the cycle's
ORIGINAL proposer/model; calibration attribution stays partitioned by the
pinned model. Concurrent requests never block; failed switches fail closed and
restore; the public audit is JSON-safe.
"""

import hashlib
import json
import threading
import unittest

from calibration.adaptive_calibrator import AdaptiveCalibrator
from coordinator.history import HistoryMode
from coordinator.intent_coordinator import IntentCoordinator
from coordinator.proposer import (
    ProposerContext, SWITCH_PENDING, SWITCH_APPLIED, SWITCH_FAILED,
    SWITCH_COALESCED,
)
from decision.llm_backend import LLMResponse


class _FakeBackend:
    def __init__(self, name, model):
        self.name = name
        self.model = model
        self.prompts = []
        self.gate = None            # optional threading.Event to block generate
        self.entered = threading.Event()   # set the instant generate is entered
        self.content = "{}"

    def generate(self, prompt, system_prompt=""):
        self.prompts.append(prompt)
        if self.gate is not None:
            self.entered.set()      # signal the call is IN and about to block
            self.gate.wait(timeout=5.0)
        return LLMResponse(success=True, content=self.content, model=self.model)


class _FakeManager:
    """Mirrors the LLMBackendManager surface the coordinator pins against,
    INCLUDING the transactional selection snapshot/restore API."""
    def __init__(self):
        self.backends = {"A": _FakeBackend("A", "modelA"),
                         "B": _FakeBackend("B", "modelB")}
        self.active = "A"
        self.mutate_then_raise = None      # name -> raise AFTER mutating active

    def _name(self, target):
        return str(getattr(target, "value", target))

    def active_selection_snapshot(self):
        return (self.active,)

    def restore_active_selection(self, snap):
        (self.active,) = snap

    def set_backend(self, target):
        name = self._name(target)
        if self.mutate_then_raise == name:
            self.active = name          # PARTIAL mutation ...
            raise RuntimeError("warming boom")   # ... then raise
        if name not in self.backends:
            return False
        self.active = name
        return True

    def active_backend_object(self):
        return self.backends[self.active]

    def active_backend_name(self):
        return self.active

    def active_model_version(self):
        return self.backends[self.active].model

    def generate_with(self, obj, prompt, system_prompt=""):
        return obj.generate(prompt, system_prompt)

    def build_feasibility_prompt_hash(self, a, n, s, h=None):
        # the REAL prompt hash the coordinator stamps BEFORE the bounded model
        # call (P1-6); a deterministic 64-hex digest without a backend call.
        return hashlib.sha256(repr((a, n, s)).encode()).hexdigest()

    def analyze_feasibility_with(self, obj, a, n, s, h=None):
        return obj.generate("feas")

    def generate_alternatives_with(self, obj, a, f, s):
        return obj.generate("alts")

    def generate(self, prompt, system_prompt="", backend=None):
        return self.active_backend_object().generate(prompt, system_prompt)


class _FakeCal:
    def __init__(self):
        self.contexts = []

    def set_operating_context(self, model, regime):
        self.contexts.append((model, regime))


def _coord(cal=None):
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.llm_manager = _FakeManager()
    c._proposer_ctx = None
    c._switch_lock = threading.Lock()
    c._switch_queue = []
    c._switch_audit = []
    c._switch_raw_targets = {}
    c._episode_in_flight = False
    c._episode_context = None
    c._calibration_context_ok = True
    c.current_phase = "regimeX"
    c.calibrator = cal if cal is not None else _FakeCal()
    return c


class PinningTest(unittest.TestCase):

    def test_model_is_pinned_for_cycle(self):
        c = _coord()
        ctx = c._open_proposal_boundary(0, "cyc-0", "ep-1")
        self.assertEqual(ctx.model_version, "modelA")
        self.assertIs(ctx.backend_object, c.llm_manager.backends["A"])
        self.assertEqual(c._proposer_context(), ("A", "modelA"))
        self.assertEqual(c._active_model_id(), "modelA")

    def test_external_active_mutation_does_not_move_the_pin(self):
        c = _coord()
        c._open_proposal_boundary(0, "cyc-0", "ep-1")
        c.llm_manager.active = "B"                    # external mutation
        self.assertEqual(c._proposer_context(), ("A", "modelA"))
        c._pinned_generate("parse")
        self.assertTrue(c.llm_manager.backends["A"].prompts)
        self.assertFalse(c.llm_manager.backends["B"].prompts)

    def test_proposal_id_binds_onto_pin(self):
        c = _coord()
        c._open_proposal_boundary(0, None, "ep-1")
        c._refine_pin_cycle(0, "cyc-0")
        self.assertIsNone(c._proposer_ctx.proposal_id)
        c._bind_proposal_id("prop-7")
        self.assertEqual(c._proposer_ctx.proposal_id, "prop-7")
        self.assertEqual(c._proposer_ctx.model_version, "modelA")   # unchanged


class SwitchQueueTest(unittest.TestCase):

    def test_idle_switch_applies_immediately(self):
        c = _coord()
        self.assertTrue(c.set_llm_backend("B"))
        self.assertEqual(c.llm_manager.active, "B")
        self.assertEqual(c._switch_audit[-1]["status"], SWITCH_APPLIED)

    def test_switch_during_cycle_is_queued(self):
        c = _coord()
        c._open_proposal_boundary(0, "cyc-0", "ep-1")
        c._episode_in_flight = True
        self.assertTrue(c.set_llm_backend("B"))
        self.assertEqual(c.llm_manager.active, "A")   # not switched yet
        self.assertEqual(c._proposer_context(), ("A", "modelA"))
        self.assertEqual(c._switch_queue[0]["status"], SWITCH_PENDING)

    def test_switch_applies_at_next_proposal_boundary(self):
        c = _coord()
        c._open_proposal_boundary(0, "cyc-0", "ep-1")
        c._episode_in_flight = True
        c.set_llm_backend("B")
        ctx = c._open_proposal_boundary(1, "cyc-1", "ep-1")
        self.assertEqual(c.llm_manager.active, "B")
        self.assertEqual(ctx.model_version, "modelB")
        applied = [a for a in c._switch_audit if a["status"] == SWITCH_APPLIED]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["first_effective_cycle"], 1)
        self.assertEqual(applied[0]["cycle_id"], "cyc-1")
        # after S2 mints the proposal id, the applied audit gains it
        c._bind_proposal_id("prop-9")
        applied = [a for a in c.get_switch_audit()
                   if a["status"] == SWITCH_APPLIED]
        self.assertEqual(applied[0]["proposal_id"], "prop-9")

    def test_multiple_coalesced_json_safe_null_first_effective(self):
        c = _coord()
        c._open_proposal_boundary(0, "cyc-0", "ep-1")
        c._episode_in_flight = True
        c.set_llm_backend("A")        # superseded
        c.set_llm_backend("A")        # superseded
        c.set_llm_backend("B")        # last wins
        c._open_proposal_boundary(1, "cyc-1", "ep-1")
        audit = c.get_switch_audit()
        json.dumps(audit)             # JSON-safe (no enum/object)
        coalesced = [a for a in audit if a["status"] == SWITCH_COALESCED]
        self.assertEqual(len(coalesced), 2)
        for rec in coalesced:
            self.assertIsNone(rec["first_effective_cycle"])
            self.assertIsNotNone(rec["superseded_by"])
        applied = [a for a in audit if a["status"] == SWITCH_APPLIED]
        self.assertEqual(len(applied), 1)

    def test_failed_switch_restores_manager_and_retains_pin(self):
        # a fake manager that mutates active THEN raises must be rolled back and
        # the PREVIOUS pin retained (no re-read of a partial mutation).
        c = _coord()
        c._open_proposal_boundary(0, "cyc-0", "ep-1")     # pinned A
        pin0 = c._proposer_ctx.backend_object
        c._episode_in_flight = True
        c.llm_manager.mutate_then_raise = "B"
        c.set_llm_backend("B")
        ctx = c._open_proposal_boundary(1, "cyc-1", "ep-1")
        self.assertEqual(c.llm_manager.active, "A")       # restored
        self.assertEqual(ctx.model_version, "modelA")     # pin retained
        self.assertIs(ctx.backend_object, pin0)
        failed = [a for a in c._switch_audit if a["status"] == SWITCH_FAILED]
        self.assertEqual(len(failed), 1)
        self.assertIsNone(failed[0]["first_effective_cycle"])

    def test_concurrent_switch_does_not_block(self):
        c = _coord()
        c._open_proposal_boundary(0, "cyc-0", "ep-1")
        c._episode_in_flight = True
        done = []
        t = threading.Thread(target=lambda: (c.set_llm_backend("B"),
                                             done.append(True)))
        t.start()
        t.join(timeout=2.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(done, [True])
        self.assertEqual(len(c._switch_queue), 1)


class BlockedCallConcurrencyTest(unittest.TestCase):
    """The ACTUAL pinned S2/S5 backend call is blocked in one thread while a
    switch is requested from another; the OLD backend must serve the whole call
    and the NEXT boundary must use the new backend."""

    def _run(self, pinned_call):
        c = _coord()
        c._open_proposal_boundary(0, "cyc-0", "ep-1")     # pinned A
        gate = threading.Event()
        backend_a = c.llm_manager.backends["A"]
        backend_a.gate = gate
        c._episode_in_flight = True
        result = {}

        def _call():
            result["resp"] = pinned_call(c)
        worker = threading.Thread(target=_call)
        worker.start()
        # WAIT until the pinned call has actually ENTERED backend A and is
        # blocked (no race: prove the old call is in flight before switching).
        self.assertTrue(backend_a.entered.wait(timeout=5.0))
        self.assertEqual(len(backend_a.prompts), 1)       # call is in
        # request a switch WHILE the old call is blocked - must return promptly
        t0 = c.set_llm_backend("B")
        self.assertTrue(t0)
        self.assertEqual(c.llm_manager.active, "A")       # not applied mid-call
        gate.set()                                        # release the call
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive())
        # the OLD backend A served the entire call; B never touched
        self.assertFalse(c.llm_manager.backends["B"].prompts)
        self.assertEqual(result["resp"].model, "modelA")
        # the NEXT proposal boundary applies the switch
        ctx = c._open_proposal_boundary(1, "cyc-1", "ep-1")
        self.assertEqual(ctx.model_version, "modelB")
        json.dumps(c.get_switch_audit())                  # JSON-safe

    def test_blocked_s2_feasibility_call(self):
        self._run(lambda c: c._pinned_analyze_feasibility([], {}, {}, None))

    def test_blocked_s5_alternatives_call(self):
        self._run(lambda c: c._pinned_generate_alternatives([], {}, {}))


class RealWriteSettlementTest(unittest.TestCase):
    """A FAITHFUL real-write settlement-failure regression using the Batch B
    real-write fixtures (not the _trial_ok bypass): a real fake write occurs
    (tx.actual_real_write_performed=True), then intent_manager.add partially
    mutates + switches the manager A->B + raises. Exactly ONE rollback, nothing
    admitted, commit_verified False, final technical_failsafe/internal_error, all
    attribution A/modelA, one final history record, pin cleared afterwards."""

    def _real_write_coord(self):
        import time
        from tests.test_commit_invariant import (
            _commit_coord, _real_write_trial, _full_obs, _ok_verdicts)
        from decision.intent_model import FeasibilityPrediction
        c = _commit_coord(readback=2.0)
        c.llm_manager = _FakeManager()                 # A(modelA) / B(modelB)
        c.history_mode = HistoryMode.ONLINE_RESERVOIR

        def _feas(i, a, s):
            c._last_intent_signature = c._intent_content_hash(i)
            # honest double: stamp the REAL prompt hash + proposal-generated
            # state so the pre-write S3 invariant (P1-6) sees a bound prompt hash.
            c._cur_proposal_generated = True
            c._cur_schema_valid = True   # generated-proposal schema verdict (real bool)
            c._cur_prompt_hash = hashlib.sha256(b"hotswap-real-write").hexdigest()
            return FeasibilityPrediction(
                feasible=True, confidence=0.9, reasoning="",
                proposed_config={"bs1_power_offset": 2.0})
        c._analyze_feasibility = _feas
        c._execute_trial = _real_write_trial(c)

        def _v(ni, ai):
            apply_t = getattr(c._active_txn, "action_apply_time",
                              None) or time.time()
            return {"all_satisfied": True, "metrics": {},
                    "monitor_verdicts": _ok_verdicts(ni, ai),
                    "observations": [_full_obs(apply_t)]}
        c._validate_trial = _v
        return c

    def test_real_write_settle_boom_exactly_one_rollback(self):
        c = self._real_write_coord()
        c.history_reservoir = []
        rb = {"n": 0}
        orig = c._run_rollback_transaction

        def _counted(*a, **k):
            rb["n"] += 1
            return orig(*a, **k)
        c._run_rollback_transaction = _counted

        def _boom_add(intent):
            c.llm_manager.active = "B"                 # partial + external switch
            raise RuntimeError("SETTLE BOOM")
        c.intent_manager.add = _boom_add

        result = c.process_intent("throughput >= 8 Mbps")
        tx = c._active_txn
        ev = result["evidence"]
        self.assertTrue(tx.actual_real_write_performed)    # a REAL write happened
        self.assertFalse(tx.commit_verified)               # never admitted
        self.assertEqual(rb["n"], 1)                        # EXACTLY one rollback
        self.assertEqual(c.intent_manager.added, [])       # nothing admitted
        self.assertEqual(result["terminal_outcome"], "technical_failsafe")
        self.assertEqual(result["terminal_reason"], "internal_error")
        # attribution A/modelA on result/tx/evidence
        self.assertEqual((tx.proposer_id, tx.model_version), ("A", "modelA"))
        self.assertEqual((ev["proposer_id"], ev["model_version"]),
                         ("A", "modelA"))
        # exactly one FINAL history record matching the final evidence/outcome
        self.assertEqual(len(c.history_reservoir), 1)
        r = c.history_reservoir[0]
        self.assertEqual(r.terminal_outcome, "technical_failsafe")
        self.assertEqual(r.terminal_reason, "internal_error")
        self.assertEqual((r.proposer_id, r.model_version), ("A", "modelA"))
        self.assertEqual(r.evidence_id, ev["evidence_record_id"])
        self.assertIsNone(c._proposer_ctx)                 # pin cleared AFTER

    def test_real_write_clean_commit_no_rollback(self):
        from coordinator.episode_types import TerminalOutcome
        c = self._real_write_coord()
        c.history_reservoir = []
        rb = {"n": 0}
        orig = c._run_rollback_transaction
        c._run_rollback_transaction = lambda *a, **k: (rb.__setitem__(
            "n", rb["n"] + 1), orig(*a, **k))[1]
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        self.assertEqual(rb["n"], 0)                       # no rollback on commit
        self.assertEqual(len(c.intent_manager.added), 1)   # admitted
        self.assertEqual(len(c.history_reservoir), 1)
        self.assertEqual(c.history_reservoir[0].terminal_outcome,
                         "commit_original")


class FullEpisodeBlockedS2Test(unittest.TestCase):
    """A REAL process_intent whose S2 feasibility backend call blocks; a switch
    requested concurrently is queued, the old backend serves the whole cycle,
    and the new backend first becomes effective at the NEXT episode's boundary."""

    def _real_feasibility_coord(self):
        import types
        from tests.test_safety_transaction import _make_coordinator
        c, _ = _make_coordinator()
        c.llm_manager = _FakeManager()
        c.history_mode = HistoryMode.DISABLED           # skip retrieval detail
        # parse WITHOUT a backend call so the block lands on S2 feasibility only
        from decision.intent_model import (
            ConstraintType, Intent, IntentTarget, IntentType)
        intent = Intent(type=IntentType.THROUGHPUT_GOAL,
                        target=IntentTarget(kpi_name="throughput",
                                            constraint_type=ConstraintType.MIN,
                                            target_value=8.0, unit="Mbps"))
        c._parse_intent = lambda text: {"intent": intent, "raw": {}}
        # RESTORE the real _analyze_feasibility (the harness stubbed it out)
        c._analyze_feasibility = types.MethodType(
            IntentCoordinator._analyze_feasibility, c)
        return c

    def test_blocked_s2_episode_old_backend_serves_switch_next_episode(self):
        c = self._real_feasibility_coord()
        gate = threading.Event()
        a = c.llm_manager.backends["A"]
        a.gate = gate
        out = {}

        def _episode():
            out["r"] = c.process_intent("throughput >= 8 Mbps")
        worker = threading.Thread(target=_episode)
        worker.start()
        # S2 feasibility has entered backend A and is blocked
        self.assertTrue(a.entered.wait(timeout=5.0))
        # a concurrent switch returns promptly and is queued (episode in flight)
        self.assertTrue(c.set_llm_backend("B"))
        self.assertEqual(c.llm_manager.active, "A")
        gate.set()
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive())
        # backend A served the in-flight S2 call; B never touched this episode
        self.assertTrue(a.prompts)
        self.assertFalse(c.llm_manager.backends["B"].prompts)
        # B is still queued (this episode had no re-entry boundary)
        self.assertEqual(len(c._switch_queue), 1)
        # the NEXT episode's first proposal boundary applies B
        c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(c.llm_manager.active, "B")
        applied = [x for x in c.get_switch_audit()
                   if x["status"] == SWITCH_APPLIED]
        self.assertEqual(len(applied), 1)


class FullEpisodeBlockedS5Test(unittest.TestCase):
    """A REAL process_intent that routes to S5 and blocks inside the BUILT-IN
    alternative generation backend call; a concurrent switch is queued, the old
    backend serves the whole S5/cycle, and the new backend is effective only at
    the next episode's boundary."""

    def _s5_coord(self):
        from tests.test_safety_transaction import _make_coordinator
        from decision.intent_model import (
            ConstraintType, FeasibilityPrediction, Intent, IntentTarget,
            IntentType)
        c, _ = _make_coordinator()
        c.llm_manager = _FakeManager()
        c.history_mode = HistoryMode.DISABLED
        c.generate_alternatives_fn = None          # use the BUILT-IN (backend)
        intent = Intent(type=IntentType.THROUGHPUT_GOAL,
                        target=IntentTarget(kpi_name="throughput",
                                            constraint_type=ConstraintType.MIN,
                                            target_value=8.0, unit="Mbps"))
        c._parse_intent = lambda text: {"intent": intent, "raw": {}}
        # route to S5: infeasible with NO carried alternatives -> negotiation
        # must call the built-in _generate_alternatives (backend) to make some.
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=False, confidence=0.0, reasoning="", alternatives=[])
        return c

    def test_blocked_s5_alternative_generation(self):
        c = self._s5_coord()
        gate = threading.Event()
        a = c.llm_manager.backends["A"]
        a.gate = gate
        out = {}

        def _episode():
            out["r"] = c.process_intent("throughput >= 8 Mbps")
        worker = threading.Thread(target=_episode)
        worker.start()
        # the BUILT-IN S5 alternative generation has entered backend A
        self.assertTrue(a.entered.wait(timeout=5.0))
        self.assertIn("alts", a.prompts[0] if a.prompts else "")
        # a concurrent switch returns promptly and is queued
        self.assertTrue(c.set_llm_backend("B"))
        self.assertEqual(c.llm_manager.active, "A")
        gate.set()
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive())
        # backend A served the in-flight S5 call; B never touched this episode
        self.assertFalse(c.llm_manager.backends["B"].prompts)
        self.assertEqual(len(c._switch_queue), 1)          # B still queued
        # next episode's first proposal boundary applies B (same infeasible
        # feasibility stub still routes to S5, now served by backend B)
        c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(c.llm_manager.active, "B")
        applied = [x for x in c.get_switch_audit()
                   if x["status"] == SWITCH_APPLIED]
        self.assertEqual(len(applied), 1)
        json.dumps(c.get_switch_audit())                   # JSON-safe


class CalibrationPartitionTest(unittest.TestCase):

    def test_model_calibration_isolation_A_B_A(self):
        # REAL per-model calibration state: adapt A, switch to B (cold isolated),
        # switch back to A (exact restoration) - driven by the hot-swap boundary.
        cal = AdaptiveCalibrator(initial_theta=0.4, initial_n_max=3)
        c = _coord(cal=cal)
        c._open_proposal_boundary(0, "cyc-0", "ep-1")     # pin A
        c._episode_context = c._freeze_episode_context()
        self.assertEqual(c._episode_context, ("modelA", "regimeX"))
        # train A's reliability map so calibrated(0.5) moves off identity
        for _ in range(80):
            cal.record_calibration_outcome(0.5, True)
        cal_a = cal.calibrated_probability(0.5)
        self.assertGreater(cal_a, 0.5)                    # A actually adapted
        # switch to B at the next boundary -> cold isolated partition
        c._episode_in_flight = True
        c.set_llm_backend("B")
        c._open_proposal_boundary(1, "cyc-1", "ep-1")
        self.assertEqual(c._episode_context, ("modelB", "regimeX"))
        cal_b = cal.calibrated_probability(0.5)
        self.assertEqual(cal_b, 0.5)                      # cold identity, isolated
        self.assertNotEqual(cal_b, cal_a)
        # switch back to A -> exact restoration of A's adapted state
        c.set_llm_backend("A")
        c._open_proposal_boundary(2, "cyc-2", "ep-1")
        self.assertEqual(c._episode_context, ("modelA", "regimeX"))
        self.assertEqual(cal.calibrated_probability(0.5), cal_a)


class EndToEndPinTest(unittest.TestCase):
    """Full process_intent: the pin binds onto the transaction so terminal
    EVIDENCE carries the cycle's original proposer/model + a stable
    evidence_record_id, even under a queued/external switch mid-cycle."""

    def _committing_coord(self, validate_ok=True):
        from tests.test_safety_transaction import (
            _make_coordinator, _trial_ok)
        from decision.intent_model import (
            Alternative, FeasibilityPrediction)
        c, _ = _make_coordinator()
        c.llm_manager = _FakeManager()
        c.history_mode = HistoryMode.ONLINE_RESERVOIR

        # a faithful feasibility stub that ALSO captures the intent signature,
        # exactly as the real _analyze_feasibility does (so the finalized
        # history append can key on it).
        def _feas(i, a, s):
            c._last_intent_signature = c._intent_content_hash(i)
            return FeasibilityPrediction(
                feasible=True, confidence=0.9, reasoning="",
                alternatives=[Alternative(id="a1", description="x")])
        c._analyze_feasibility = _feas
        c._execute_trial = _trial_ok
        c._validate_trial = lambda ni, ai: {
            "all_satisfied": validate_ok, "metrics": {},
            "monitor_verdicts": {getattr(i, "id", None): "satisfied"
                                 for i in list(ai) + [ni]}}
        c._rollback = lambda snap: None
        return c

    def test_commit_evidence_pinned_with_record_id(self):
        from coordinator.episode_types import TerminalOutcome
        c = self._committing_coord()
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        ev = result["evidence"]
        self.assertEqual(ev["proposer_id"], "A")
        self.assertEqual(ev["model_version"], "modelA")
        self.assertTrue(ev["evidence_record_id"])
        self.assertNotEqual(ev["evidence_record_id"], ev["episode_id"])
        self.assertNotEqual(ev["evidence_record_id"],
                            ev.get("actuation_trial_id"))

    def test_external_switch_during_trial_keeps_original(self):
        from coordinator.episode_types import TerminalOutcome
        c = self._committing_coord()
        orig = c._execute_trial

        def _exec(feas):
            c.set_llm_backend("B")          # queued mid-cycle
            c.llm_manager.active = "B"       # external mutation too
            return orig(feas)
        c._execute_trial = _exec
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        self.assertEqual(result["evidence"]["proposer_id"], "A")
        self.assertEqual(result["evidence"]["model_version"], "modelA")

    def test_commit_history_append_references_evidence_record_id(self):
        # after a real commit, the ONLINE history append references EXACTLY the
        # finalized evidence_record_id (no trial/episode alias).
        c = self._committing_coord()
        c.history_reservoir = []
        result = c.process_intent("throughput >= 8 Mbps")
        erid = result["evidence"]["evidence_record_id"]
        self.assertEqual(len(c.history_reservoir), 1)
        self.assertEqual(c.history_reservoir[0].evidence_id, erid)

    def test_noncommit_evidence_pinned_with_record_id(self):
        # a validation-fail rolls back to a non-commit terminal; the evidence
        # still carries a stable evidence_record_id and the pinned proposer.
        c = self._committing_coord(validate_ok=False)
        result = c.process_intent("throughput >= 8 Mbps")
        ev = result["evidence"]
        self.assertNotEqual(result["terminal_outcome"], "commit_original")
        self.assertTrue(ev["evidence_record_id"])

    def _rollback_counter(self, c):
        n = {"count": 0}
        orig = c._run_rollback_transaction

        def _counted(*a, **k):
            n["count"] += 1
            return orig(*a, **k)
        c._run_rollback_transaction = _counted
        return n

    def test_settle_boom_keeps_original_pin_no_stale_commit(self):
        # the coordinator's exact counterexample: intent_manager.add mutates
        # llm_manager.active='B' then raises. The FINAL result/evidence/history
        # must stay A/modelA (original pinned cycle), history must be the final
        # technical_failsafe record (NOT a stale commit) with the final
        # evidence_record_id, nothing admitted, rollback exactly once, pin
        # cleared only afterward.
        c = self._committing_coord()
        c.history_reservoir = []
        rb = self._rollback_counter(c)

        def _boom_add(intent):
            c.llm_manager.active = "B"          # external + partial mutation
            raise RuntimeError("SETTLE BOOM")
        c.intent_manager.add = _boom_add

        result = c.process_intent("throughput >= 8 Mbps")
        ev = result["evidence"]
        self.assertEqual(result["terminal_outcome"], "technical_failsafe")
        self.assertEqual(result["terminal_reason"], "internal_error")
        self.assertEqual(ev["proposer_id"], "A")           # ORIGINAL pin
        self.assertEqual(ev["model_version"], "modelA")
        self.assertEqual(c.intent_manager.added, [])       # nothing admitted
        self.assertLessEqual(rb["count"], 1)               # never double rollback
        # exactly one FINAL history record, matching the final evidence/outcome
        self.assertEqual(len(c.history_reservoir), 1)
        r = c.history_reservoir[0]
        self.assertEqual(r.terminal_outcome, "technical_failsafe")
        self.assertEqual(r.proposer_id, "A")
        self.assertEqual(r.model_version, "modelA")
        self.assertEqual(r.evidence_id, ev["evidence_record_id"])
        self.assertIsNone(c._proposer_ctx)                 # pin cleared AFTER

    def test_clean_commit_full_attribution(self):
        from coordinator.episode_types import TerminalOutcome
        c = self._committing_coord()
        c.history_reservoir = []
        result = c.process_intent("throughput >= 8 Mbps")
        ev = result["evidence"]
        tx = c._active_txn
        cyc = result["cycles"][-1]
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        # result / evidence / tx / history all A/modelA
        self.assertEqual((ev["proposer_id"], ev["model_version"]),
                         ("A", "modelA"))
        self.assertEqual((tx.proposer_id, tx.model_version), ("A", "modelA"))
        self.assertEqual(len(c.history_reservoir), 1)
        self.assertEqual(c.history_reservoir[0].terminal_outcome,
                         "commit_original")
        self.assertEqual(c.history_reservoir[0].evidence_id,
                         ev["evidence_record_id"])
        self.assertEqual(len(c.intent_manager.added), 1)   # admitted
        self.assertIsNone(c._proposer_ctx)

    def test_ordinary_noncommit_attribution(self):
        c = self._committing_coord(validate_ok=False)
        c.history_reservoir = []
        result = c.process_intent("throughput >= 8 Mbps")
        ev = result["evidence"]
        self.assertNotEqual(result["terminal_outcome"], "commit_original")
        self.assertEqual((ev["proposer_id"], ev["model_version"]),
                         ("A", "modelA"))
        self.assertEqual(c.intent_manager.added, [])
        # EXACTLY ONE published record whose EVERY trusted field equals the
        # final evidence/result (not a vacuous zero-record pass).
        self.assertEqual(len(c.history_reservoir), 1)
        r = c.history_reservoir[0]
        self.assertEqual(r.terminal_outcome, result["terminal_outcome"])
        self.assertEqual(r.terminal_reason, result["terminal_reason"])
        self.assertEqual(r.proposer_id, ev["proposer_id"])
        self.assertEqual(r.model_version, ev["model_version"])
        self.assertEqual(r.evidence_id, ev["evidence_record_id"])

    def test_publish_matches_final_reason_not_provisional(self):
        # same terminal_outcome but a DIFFERENT terminal_reason between the
        # provisional prepare and the final result: the published record must
        # carry the FINAL reason (rebuilt from the final trusted result).
        c = self._committing_coord()
        c.history_reservoir = []
        real_prepare = c._prepare_history

        def _prepare_with_wrong_reason(result):
            # force a provisional candidate whose reason will NOT match the final
            result = dict(result)
            result["terminal_reason"] = "some_other_reason"
            return real_prepare(result)
        c._prepare_history = _prepare_with_wrong_reason
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(len(c.history_reservoir), 1)
        # published reason equals the FINAL result's reason, not the provisional
        self.assertEqual(c.history_reservoir[0].terminal_reason,
                         result["terminal_reason"])
        self.assertNotEqual(c.history_reservoir[0].terminal_reason,
                            "some_other_reason")

    def test_pin_cleared_after_episode_queue_survives_to_next_boundary(self):
        c = self._committing_coord()
        # a switch queued while a PRIOR cycle was in flight must survive the
        # enqueue->boundary gap and be applied at the NEXT episode's first
        # proposal boundary (not lost when the stale pin is cleared).
        c._episode_in_flight = True
        c.set_llm_backend("B")
        c._episode_in_flight = False
        self.assertEqual(len(c._switch_queue), 1)         # queued, survives
        c.process_intent("throughput >= 8 Mbps")
        # applied at this episode's first boundary; pin then cleared
        self.assertEqual(c.llm_manager.active, "B")
        self.assertEqual(c._switch_queue, [])
        self.assertIsNone(c._proposer_ctx)
        applied = [a for a in c.get_switch_audit()
                   if a["status"] == SWITCH_APPLIED]
        self.assertEqual(len(applied), 1)


if __name__ == "__main__":
    unittest.main()
