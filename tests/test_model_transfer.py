#!/usr/bin/env python3
"""Batch G item 5: model-transfer (hot-swap) pre/post comparison.

ONE coordinator / ONE topology; only proposer/model provenance changes. Reuses
IntentCoordinator.set_llm_backend + get_switch_audit; attributes pre/post by
EVIDENCE proposer/model (not the request point); honest not_applied/unknown on a
missing/unmatched/unbound switch; NO cross-model mixing; finite JSON; always
integration_only / excluded_from_paper / not paper_ready.
"""
import hashlib
import json
import threading
import unittest

from coordinator.intent_coordinator import IntentCoordinator
from coordinator.proposer import SWITCH_APPLIED
from experiments.metrics import EpisodeRecord, StepRecord, IntentConfig
from experiments import model_transfer as mt

CFG = IntentConfig(throughput_target_mbps=8.0, throughput_ue_ids=("ue1",))


def _ep(trial, model, terminal, debited=None, lb=None, dstatus=None, **kw):
    base = dict(trial_id=trial, method="m", phase="Nominal",
                evidence_proposer_id=model, evidence_model_version=model,
                evidence_episode_id=f"e{trial}", evidence_cycle_id=f"c{trial}",
                evidence_proposal_id=f"p{trial}",
                terminal_outcome=terminal,
                budget_debited=debited, budget_debited_lower_bound=lb,
                budget_debit_status=dstatus)
    base.update(kw)
    return EpisodeRecord(**base)


def _st(trial, idx, tput, bs2=0.0):
    return StepRecord(trial_id=trial, method="m", phase="Nominal", step_idx=idx,
                      phase_idx=0, phase_label="p0:Nominal",
                      ue_kpis={"ue1": {"throughput_mbps": tput}},
                      action_offsets={"bs2": bs2})


def _audit(target="B", **over):
    a = {"request_id": "r1", "requested_at": "2026-01-01T00:00:00",
         "target": target, "status": "applied",
         "applied_at": "2026-01-01T00:00:01", "episode_id": "e2",
         "cycle_index": 1, "cycle_id": "c2", "proposal_id": "p2",
         "first_effective_cycle": 1}
    a.update(over)
    return [a]


class ModelTransferComparisonTest(unittest.TestCase):

    def test_switch_status_literal_matches_coordinator(self):
        self.assertEqual(mt.SWITCH_APPLIED_STATUS, SWITCH_APPLIED)

    def _clean_run(self):
        eps = [_ep(1, "A", "commit_original", debited=10.0, dstatus="settled",
                   trial_executed=True, trial_success=True, rolled_back=False),
               _ep(2, "B", "reject", debited=5.0, dstatus="settled",
                   trial_executed=True, trial_success=False, rolled_back=True,
                   restore_verified=True)]
        steps = [_st(1, 0, 9.0), _st(2, 0, 5.0)]
        return eps, steps

    def test_boundary_and_six_metric_hand_calc(self):
        eps, steps = self._clean_run()
        r = mt.model_transfer_comparison(eps, steps, CFG, _audit(), "B",
                                         from_model="A",
                                         topology_fingerprint="topo#1")
        # boundary from EVIDENCE (trial 2 = first proposer_id==B), NOT the request
        self.assertTrue(r["applied"])
        self.assertEqual(r["status"], "applied")
        self.assertEqual(r["first_effective"]["trial_id"], 2)
        self.assertEqual(r["first_effective"]["evidence_proposer_id"], "B")
        self.assertEqual(r["from_model"], "A")
        # switch audit fields threaded exactly (timestamps/ids/first effective)
        self.assertEqual(r["switch"]["request_id"], "r1")
        self.assertEqual(r["switch"]["applied_at"], "2026-01-01T00:00:01")
        self.assertEqual(r["switch"]["first_effective_cycle"], 1)
        self.assertTrue(r["audit_evidence_bound"])
        self.assertFalse(r["cross_model_mixing"])
        # (1) strict joint: pre satisfied (9>=8) -> 1.0; post violated (5<8) -> 0.0
        self.assertEqual(r["pre"]["strict_joint_satisfaction"]["rate"], 1.0)
        self.assertEqual(r["post"]["strict_joint_satisfaction"]["rate"], 0.0)
        # (2) rollback verified: pre no rollback -> None; post verified -> 1.0
        self.assertIsNone(r["pre"]["rollback_verified"]["rate"])
        self.assertEqual(r["post"]["rollback_verified"]["rate"], 1.0)
        # (3) violation area keeps throughput/power units SEPARATE
        va = r["post"]["violation_area"]
        self.assertEqual(va["throughput"]["unit"], "Mbps*step")
        self.assertEqual(va["power_i1"]["unit"], "dB*step")
        # (4) recovery present with unit
        self.assertEqual(r["post"]["recovery"]["unit"], "s")
        # (5) budget: exact measured debit
        self.assertEqual(r["pre"]["budget"]["debited"], 10.0)
        self.assertEqual(r["post"]["budget"]["debited"], 5.0)
        self.assertEqual(r["pre"]["budget"]["status"], "measured")
        # (6) proposal acceptance from terminal evidence: commit=1.0, reject=0.0
        self.assertEqual(r["pre"]["acceptance"]["rate"], 1.0)
        self.assertEqual(r["post"]["acceptance"]["rate"], 0.0)
        # classification + finite JSON
        self.assertEqual(r["paper_eligibility"], "integration_only")
        self.assertIs(r["excluded_from_paper"], True)
        self.assertIs(r["paper_ready"], False)
        self.assertEqual(r["topology_fingerprint"], "topo#1")
        json.dumps(r, allow_nan=False)

    def test_not_applied_when_no_audit(self):
        eps, steps = self._clean_run()
        r = mt.model_transfer_comparison(eps, steps, CFG, [], "B")  # no audit
        self.assertFalse(r["applied"])
        self.assertEqual(r["status"], "not_applied")
        self.assertIsNone(r["pre"])
        self.assertIsNone(r["post"])
        json.dumps(r, allow_nan=False)

    def test_unknown_when_no_evidence_match(self):
        # applied audit for B, but NO episode carries proposer/model == B
        eps = [_ep(1, "A", "commit_original"), _ep(2, "A", "commit_original")]
        r = mt.model_transfer_comparison(eps, [], CFG, _audit(), "B")
        self.assertFalse(r["applied"])
        self.assertEqual(r["status"], "not_applied")
        self.assertIn("evidence", r["reason"])
        self.assertIsNone(r["pre"])

    def test_cross_model_mixing_emits_no_metrics(self):
        # a post-switch episode still attributed to model A -> mixing
        eps = [_ep(1, "A", "commit_original"),
               _ep(2, "B", "commit_original"),
               _ep(3, "A", "commit_original")]     # A after the switch -> mixed
        r = mt.model_transfer_comparison(eps, [], CFG, _audit(), "B")
        self.assertTrue(r["cross_model_mixing"])
        self.assertEqual(r["status"], "cross_model_mixing")
        self.assertIsNone(r["pre"])
        self.assertIsNone(r["post"])
        json.dumps(r, allow_nan=False)

    def test_audit_evidence_mismatch_is_unbound(self):
        eps, steps = self._clean_run()
        # audit episode_id disagrees with the first-effective evidence_episode_id
        bad = _audit(episode_id="WRONG")
        r = mt.model_transfer_comparison(eps, steps, CFG, bad, "B")
        self.assertFalse(r["audit_evidence_bound"])
        self.assertEqual(r["status"], "audit_evidence_unbound")
        self.assertIsNone(r["pre"])

    def test_budget_lower_bound_status_and_acceptance_unknown(self):
        # a debit present but status NOT settled -> lower bound (not measured);
        # a None-terminal episode -> acceptance unknown, excluded from the rate.
        eps = [_ep(1, "A", "commit_original", debited=4.0, dstatus="partial"),
               _ep(1, "A", None, lb=2.0, dstatus="unknown")]
        b = mt.budget_consumption(eps)
        self.assertEqual(b["n_measured"], 0)
        self.assertTrue(b["is_lower_bound"])
        self.assertEqual(b["status"], "unknown_only")
        self.assertEqual(b["lower_bound"], 6.0)          # 4.0 (downgraded) + 2.0
        self.assertIn("partial", b["debit_status_coverage"])
        acc = mt.proposal_acceptance(eps)
        self.assertEqual(acc["n_decisions"], 1)          # only the commit terminal
        self.assertEqual(acc["n_unknown"], 1)            # the None-terminal one
        self.assertEqual(acc["rate"], 1.0)
        # zero-acceptance + no-samples cases
        self.assertIsNone(mt.proposal_acceptance([])["rate"])
        z = mt.proposal_acceptance([_ep(1, "A", "reject")])
        self.assertEqual(z["rate"], 0.0)

    def test_rebase_preserves_identifiers_and_is_contiguous(self):
        # a post window whose step_idx starts at 5 must rebase to 0-based
        # contiguous WITHOUT corrupting the window identity.
        src = [_st(2, 7, 5.0), _st(2, 5, 6.0), _st(2, 6, 7.0)]
        out = mt.rebase_steps(src)
        self.assertEqual(sorted(s.step_idx for s in out), [0, 1, 2])   # 0-based
        for s in out:                                    # identity preserved
            self.assertEqual(s.trial_id, 2)
            self.assertEqual(s.method, "m")
            self.assertEqual(s.phase_label, "p0:Nominal")
        # sorted by original idx -> values map 5->0,6->1,7->2
        by_idx = {s.step_idx: s.ue_kpis["ue1"]["throughput_mbps"] for s in out}
        self.assertEqual(by_idx, {0: 6.0, 1: 7.0, 2: 5.0})

    def test_binding_requires_equality_missing_evidence_is_unbound(self):
        # the audit carries cycle_id but the first-effective evidence lacks one ->
        # UNBOUND (a missing evidence id is NOT success).
        eps = [_ep(1, "A", "commit_original"),
               _ep(2, "B", "commit_original", evidence_cycle_id=None)]
        r = mt.model_transfer_comparison(eps, [_st(1, 0, 9.0), _st(2, 0, 9.0)],
                                         CFG, _audit(), "B")
        self.assertFalse(r["audit_evidence_bound"])
        self.assertEqual(r["status"], "audit_evidence_unbound")
        self.assertIsNone(r["pre"])
        self.assertIsNone(r["post"])

    def test_ambiguous_when_multiple_applied_same_model(self):
        eps = [_ep(1, "A", "commit_original"), _ep(2, "B", "commit_original")]
        audit = _audit(request_id="rA") + _audit(request_id="rB")  # 2 applied to B
        r = mt.model_transfer_comparison(eps, [_st(1, 0, 9.0), _st(2, 0, 9.0)],
                                         CFG, audit, "B")
        self.assertEqual(r["status"], "ambiguous_switch")   # never the first one
        self.assertIsNone(r["pre"])
        self.assertIsNone(r["post"])
        # the EXACT request_id resolves the ambiguity
        r2 = mt.model_transfer_comparison(eps, [_st(1, 0, 9.0), _st(2, 0, 9.0)],
                                          CFG, audit, "B", request_id="rB")
        self.assertEqual(r2["status"], "applied")
        self.assertEqual(r2["switch"]["request_id"], "rB")

    def test_rebase_rejects_malformed_windows(self):
        for bad in ([_st(2, 0, 9.0), _st(2, 0, 8.0)],        # duplicate
                    [_st(2, 0, 9.0), _st(2, 2, 8.0)],        # gap
                    [_st(2, -1, 9.0)],                       # negative
                    [_st(2, True, 9.0)]):                    # bool step_idx
            with self.assertRaises(ValueError):
                mt.rebase_steps(bad)
        # a valid contiguous slice starting at 5 rebases cleanly to 0-based
        out = mt.rebase_steps([_st(2, 5, 9.0), _st(2, 6, 8.0), _st(2, 7, 7.0)])
        self.assertEqual(sorted(s.step_idx for s in out), [0, 1, 2])

    def test_budget_measured_plus_none_is_partial_lower_bound(self):
        # measured debit + a no-ledger (none) cycle -> partial + lower bound,
        # NEVER measured-complete.
        eps = [_ep(1, "A", "commit_original", debited=3.0, dstatus="settled"),
               _ep(1, "A", "commit_original")]            # no ledger -> n_none
        b = mt.budget_consumption(eps)
        self.assertEqual(b["n_measured"], 1)
        self.assertEqual(b["n_none"], 1)
        self.assertEqual(b["status"], "partial")
        self.assertTrue(b["is_lower_bound"])
        self.assertEqual(b["debited"], 3.0)


class _Mgr:
    """Minimal LLMBackendManager surface for the real switch/boundary path."""
    def __init__(self):
        self.active = "A"

    def active_selection_snapshot(self):
        return (self.active,)

    def restore_active_selection(self, snap):
        (self.active,) = snap

    def set_backend(self, target):
        self.active = str(getattr(target, "value", target))
        return True

    def active_backend_object(self):
        return object()

    def active_backend_name(self):
        return self.active

    def active_model_version(self):
        return "model" + self.active


class _Cal:
    def set_operating_context(self, model, regime):
        pass


def _mini_coord():
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.llm_manager = _Mgr()
    c._proposer_ctx = None
    c._switch_lock = threading.Lock()
    c._switch_queue = []
    c._switch_audit = []
    c._switch_raw_targets = {}
    c._episode_in_flight = False
    c.current_phase = "regimeX"
    c.calibrator = _Cal()
    return c


class SameCoordinatorSwitchTest(unittest.TestCase):
    """The switch request/audit come from the REAL coordinator (same instance,
    one topology); the comparison consumes that real audit."""

    def test_request_switch_fails_closed_without_active_run(self):
        c = _mini_coord()                               # NOT in flight
        req = mt.request_transfer_switch(c, "B")
        self.assertFalse(req["accepted"])
        self.assertEqual(req["status"], "no_active_run")
        self.assertEqual(c.get_switch_audit(), [])      # no switch was requested

    def test_active_run_switch_audit_and_first_effective_binding(self):
        c = _mini_coord()
        c._open_proposal_boundary(0, "cyc-0", "ep-1")   # boundary 0: pin A
        c._episode_in_flight = True                     # active -> switch QUEUES
        req = mt.request_transfer_switch(c, "B")        # reuse set_llm_backend
        self.assertTrue(req["accepted"])
        self.assertTrue(req["request_id"] and req["requested_at"])
        self.assertEqual(req["target"], "B")
        c._open_proposal_boundary(1, "cyc-1", "ep-1")   # boundary 1: apply B
        c._bind_proposal_id("prop-9")                   # S2 mints the proposal id
        applied = [a for a in c.get_switch_audit()
                   if a["status"] == SWITCH_APPLIED and a["target"] == "B"]
        self.assertEqual(len(applied), 1)               # exactly one switch
        a = applied[0]
        self.assertEqual(a["request_id"], req["request_id"])
        self.assertTrue(a["applied_at"])
        self.assertEqual(a["cycle_index"], 1)
        self.assertEqual(a["cycle_id"], "cyc-1")
        self.assertEqual(a["first_effective_cycle"], 1)
        self.assertEqual(a["proposal_id"], "prop-9")
        # feed the REAL audit into the comparison; evidence for the post trial
        # carries the same cycle/proposal ids -> bound + attributed to B.
        eps = [_ep(1, "A", "commit_original", debited=1.0, dstatus="settled"),
               _ep(2, "B", "commit_original", debited=1.0, dstatus="settled",
                   evidence_cycle_id="cyc-1", evidence_proposal_id="prop-9",
                   evidence_episode_id="ep-1")]
        steps = [_st(1, 0, 9.0), _st(2, 0, 9.0)]
        r = mt.model_transfer_comparison(eps, steps, CFG, c.get_switch_audit(),
                                         "B", from_model="A",
                                         request_id=req["request_id"])  # exact bind
        self.assertEqual(r["status"], "applied")
        self.assertTrue(r["audit_evidence_bound"])
        self.assertEqual(r["switch"]["request_id"], req["request_id"])
        self.assertEqual(r["first_effective"]["evidence_cycle_id"], "cyc-1")


if __name__ == "__main__":
    unittest.main()
