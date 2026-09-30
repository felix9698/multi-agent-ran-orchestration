#!/usr/bin/env python3
"""Batch G provenance + latency focused tests (P1-3 / P1-6 review checkpoint).

Offline only: no hardware / network / OTA. These pin the anti-fabrication
contracts the coordinator review required:
  * prompt hash is a REAL full-SHA-256 of the exact prompt, computed WITHOUT a
    backend call (so a model timeout retains it);
  * the raw exporter uses EXPLICIT source facts (was_parsed_intent /
    proposal_generated) and fails closed on None or a missing real id - it never
    fabricates a pipeline id, schema verdict, model id, or run id;
  * the raw action stream is built from the finalized applied writes with their
    per-axis attempt monotonic timestamps (never clip events / executor state);
  * unmeasured latency / reconnection / budget-debit stay None (never a fake 0);
  * repeated phases carry a unique phase label + a real episode monotonic time.
"""

import math
import unittest

from decision.llm_backend import (
    LLMBackendManager, LLMResponse, prompt_content_hash,
)
from coordinator.intent_coordinator import (
    IntentCoordinator, MalformedResponseError,
)
from coordinator.fsm import OperationTimeout
from coordinator.episode_types import (
    EvidenceRecord, ActuationTransaction, TerminalOutcome, TerminalReason,
)
from decision.intent_model import (
    NetworkState, Intent, IntentTarget, IntentType, ConstraintType,
)
from experiments.metrics import EpisodeRecord, IntentConfig, regret_decomposition
from experiments.runner import ExperimentRunner
from experiments.paired_runner import PairedBlockRunner, DataOrigin


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #

def _emulated_raw_runner():
    """A PairedBlockRunner tagged EMULATED_PIPELINE (coordinator not needed for a
    direct _raw_from_episode call - the real branch reads only the EpisodeRecord's
    threaded evidence fields)."""
    return PairedBlockRunner(["llm_with_history"], n_blocks=1, master_seed=1,
                             trials_per_block=1, steps_per_phase=1,
                             data_origin=DataOrigin.EMULATED_PIPELINE)


def _real_ep(**over):
    """An EpisodeRecord carrying a COMPLETE, consistent real (emulated) evidence
    chain for a committed S3 write. Overridable per test."""
    base = dict(
        trial_id=1, method="llm_with_history", phase="Nominal", phase_idx=0,
        episode_monotonic_s=1234.5, cycle_idx=0,
        evidence_experiment_run_id="run-abc", evidence_episode_id="ep-abc",
        evidence_fsm_step_id="fsm-abc", evidence_cycle_id="cycle-abc",
        evidence_proposal_id="prop-abc",
        evidence_actuation_trial_id="trial-abc",
        evidence_proposer_id="mock:deterministic",
        evidence_model_version="mock:deterministic",
        evidence_was_parsed_intent=True, evidence_pending_intent_id="int-abc",
        proposal_generated=True, schema_valid=True,
        evidence_prompt_hash="prompt-sha256-" + "a" * 64,
        evidence_intent_type="throughput_goal",
        evidence_intent_target={"kpi_name": "throughput",
                                "constraint_type": "min",
                                "target_value": 8.0, "unit": "Mbps"},
        evidence_intent_scope={"ue_ids": ["ue1"], "bs_ids": []},
        evidence_requested_action={"bs2": {"power_offset": 2.0}},
        evidence_clipped_action={"bs2": {"power_offset": 2.0}},
        evidence_canonical_action={"bs2": {"power_offset": 2.0}},
        readback_action={"bs2": {"power_offset": 2.0}},
        action_apply_monotonic_s=555.0,
        applied_action=[{"gnb_id": "gnb2", "ue_id": None, "axis": "rfatt",
                         "value": 2.0, "ok": True,
                         "attempt_monotonic_s": 555.0}],
        terminal_outcome="commit_original", terminal_reason="commit_verified",
        trial_executed=True, success=True, trial_success=True,
    )
    base.update(over)
    return EpisodeRecord(**base)


def _raw(runner, ep):
    return runner._raw_from_episode(
        ep, "llm_with_history", "run:blk0:llm_with_history", "run:blk0", 0,
        123, ["llm_with_history"], 0, "envtrace-x", [], "mock:deterministic",
        "wired", "executor@0x1")


# --------------------------------------------------------------------------- #
# prompt hash: real full SHA-256, no backend call, timeout-retained            #
# --------------------------------------------------------------------------- #

class PromptHashTest(unittest.TestCase):
    def test_prompt_hash_is_full_sha256(self):
        h = prompt_content_hash("the exact prompt", "system")
        self.assertTrue(h.startswith("prompt-sha256-"))
        self.assertEqual(len(h[len("prompt-sha256-"):]), 64)   # full digest
        # deterministic + sensitive to both prompt and system prompt
        self.assertEqual(h, prompt_content_hash("the exact prompt", "system"))
        self.assertNotEqual(h, prompt_content_hash("the exact prompt", "other"))
        self.assertNotEqual(h, prompt_content_hash("other prompt", "system"))

    def test_prompt_build_hash_makes_zero_backend_calls(self):
        mgr = LLMBackendManager()
        called = {"n": 0}

        class _Spy:
            def generate(self_inner, prompt, system_prompt=""):
                called["n"] += 1
                return LLMResponse(success=False, content="")
        # even with a backend present, building the hash must NOT call it.
        mgr.backends = {"spy": _Spy()}
        h = mgr.build_feasibility_prompt_hash([], {"id": "i", "type": "t"}, {})
        self.assertEqual(called["n"], 0)
        self.assertTrue(h.startswith("prompt-sha256-"))

    def test_feasibility_stamps_prompt_hash_on_response(self):
        # analyze_feasibility stamps the response's prompt_hash at generation, so
        # a later timeout/failure path still has the hash of what it sent.
        mgr = LLMBackendManager()
        h = mgr.build_feasibility_prompt_hash([], {"id": "i", "type": "t"}, {})
        self.assertEqual(len(h[len("prompt-sha256-"):]), 64)


# --------------------------------------------------------------------------- #
# raw exporter: explicit source facts, fail-closed, no fabrication            #
# --------------------------------------------------------------------------- #

class RawStageHonestyTest(unittest.TestCase):
    def setUp(self):
        self.r = _emulated_raw_runner()

    def test_happy_path_uses_real_ids(self):
        rec = _raw(self.r, _real_ep())
        self.assertEqual(rec.experiment_run_id, "run-abc")   # coordinator's, not label
        self.assertEqual(rec.episode_id, "ep-abc")
        self.assertEqual(rec.fsm_step_id, "fsm-abc")
        self.assertEqual(rec.pending_intent_id, "int-abc")   # Intent.id, not hash
        self.assertEqual(rec.model_id, "mock:deterministic")  # proposer_id
        self.assertEqual(rec.terminal_outcome, "commit_original")
        self.assertEqual(rec.terminal_reason, "commit_verified")

    def test_actuation_id_iff_applied_write_evidence(self):
        # P1-6 (item 5): actuation_trial_id <-> real applied-write evidence, BOTH
        # directions. Forward: an actuation id WITHOUT applied-write evidence ->
        # empty stream, RawEvidenceRecord invariant FAILS CLOSED.
        with self.assertRaises(ValueError):
            _raw(self.r, _real_ep(applied_action=None))         # id set, no write
        # Reverse: real applied-write evidence WITHOUT an actuation id -> would
        # silently drop a real S3 write -> FAILS CLOSED.
        with self.assertRaises(ValueError):
            _raw(self.r, _real_ep(evidence_actuation_trial_id=None))
        # the consistent committed write (id + applied write) is accepted.
        rec = _raw(self.r, _real_ep())
        self.assertEqual(rec.actuation_trial_id, "trial-abc")
        self.assertTrue(rec.action_stream)

    def test_applied_write_bad_monotonic_timestamp_fails_closed(self):
        # P1-6 (item 5): the builder validates each S3 apply-attempt monotonic
        # timestamp as a non-bool real finite > 0 (bool/string/NaN/inf/0/neg fail).
        for bad in (True, "5.0", float("nan"), float("inf"), 0.0, -1.0):
            with self.assertRaises(ValueError):
                _raw(self.r, _real_ep(applied_action=[{
                    "gnb_id": "gnb2", "ue_id": None, "axis": "rfatt",
                    "value": 2.0, "ok": True, "attempt_monotonic_s": bad}]))

    def test_missing_run_id_fails_closed(self):
        with self.assertRaises(ValueError):
            _raw(self.r, _real_ep(evidence_experiment_run_id=None))

    def test_missing_proposer_or_model_version_fails_closed(self):
        with self.assertRaises(ValueError):
            _raw(self.r, _real_ep(evidence_proposer_id=None))
        with self.assertRaises(ValueError):
            _raw(self.r, _real_ep(evidence_model_version=None))

    def test_terminal_outcome_required_not_derived(self):
        with self.assertRaises(ValueError):
            _raw(self.r, _real_ep(terminal_outcome=None))
        with self.assertRaises(ValueError):
            _raw(self.r, _real_ep(terminal_reason=None))

    def test_was_parsed_none_fails_closed(self):
        with self.assertRaises(ValueError):
            _raw(self.r, _real_ep(evidence_was_parsed_intent=None))

    def test_parsed_requires_intent_id(self):
        with self.assertRaises(ValueError):
            _raw(self.r, _real_ep(evidence_was_parsed_intent=True,
                                  evidence_pending_intent_id=None))

    def test_unparsed_must_have_none_id(self):
        # unparsed pre-parse terminal with no proposal - id must be None
        with self.assertRaises(ValueError):
            _raw(self.r, _real_ep(evidence_was_parsed_intent=False,
                                  evidence_pending_intent_id="int-abc",
                                  proposal_generated=False,
                                  evidence_proposal_id=None,
                                  evidence_cycle_id=None,
                                  evidence_actuation_trial_id=None,
                                  applied_action=None, trial_executed=False,
                                  terminal_outcome="pending_not_admitted",
                                  terminal_reason="parse_failed",
                                  schema_valid=None))

    def test_proposal_generated_none_fails_closed(self):
        with self.assertRaises(ValueError):
            _raw(self.r, _real_ep(proposal_generated=None))

    def test_proposal_generated_requires_proposal_id(self):
        with self.assertRaises(ValueError):
            _raw(self.r, _real_ep(proposal_generated=True,
                                  evidence_proposal_id=None))

    def test_schema_verdict_requires_real_bool_when_generated(self):
        with self.assertRaises(ValueError):
            _raw(self.r, _real_ep(proposal_generated=True, schema_valid=None))

    def test_pre_proposal_terminal_has_none_stage_ids(self):
        rec = _raw(self.r, _real_ep(
            proposal_generated=False, schema_valid=None,
            evidence_proposal_id=None, evidence_cycle_id=None,
            evidence_actuation_trial_id=None, applied_action=None,
            trial_executed=False, terminal_outcome="pending_not_admitted",
            terminal_reason="low_confidence"))
        self.assertIsNone(rec.proposal_id)
        self.assertIsNone(rec.cycle_id)
        self.assertIsNone(rec.actuation_trial_id)
        self.assertIsNone(rec.schema_verdict)     # no proposal -> no verdict
        self.assertEqual(rec.action_stream, [])

    def test_no_proposal_with_non_null_schema_valid_is_rejected(self):
        # proposal_generated False but a non-null schema_valid is INCONSISTENT (a
        # verdict without a proposal): the builder REJECTS it, never silently
        # normalizes it to None.
        with self.assertRaises(ValueError):
            _raw(self.r, _real_ep(
                proposal_generated=False, schema_valid=True,
                evidence_proposal_id=None, evidence_cycle_id=None,
                evidence_actuation_trial_id=None, applied_action=None,
                trial_executed=False, terminal_outcome="pending_not_admitted",
                terminal_reason="low_confidence"))

    def test_intent_fields_are_real_dicts(self):
        rec = _raw(self.r, _real_ep())
        self.assertEqual(rec.intent_type, "throughput_goal")
        self.assertEqual(rec.intent_scope, {"ue_ids": ["ue1"], "bs_ids": []})
        self.assertEqual(rec.intent_target["target_value"], 8.0)

    def test_phase_and_episode_monotonic_present(self):
        rec = _raw(self.r, _real_ep(phase_idx=4, phase="Nominal"))
        self.assertEqual(rec.phase_idx, 4)
        self.assertEqual(rec.phase_label, "p4:Nominal")     # unique across repeats
        self.assertEqual(rec.episode_monotonic_s, 1234.5)   # actual, not a seq


# --------------------------------------------------------------------------- #
# action stream from finalized applied writes + per-axis attempt time         #
# --------------------------------------------------------------------------- #

class ActionStreamRealTest(unittest.TestCase):
    def setUp(self):
        self.r = _emulated_raw_runner()

    def test_uses_per_axis_attempt_timestamps(self):
        ep = _real_ep(applied_action=[
            {"gnb_id": "gnb2", "ue_id": None, "axis": "rfatt", "value": 2.0,
             "ok": True, "attempt_monotonic_s": 100.0},
            {"gnb_id": "gnb1", "ue_id": "ue1", "axis": "sched_prio",
             "value": 1.5, "ok": True, "attempt_monotonic_s": 100.5},
        ])
        rec = _raw(self.r, ep)
        stamps = [a["applied_monotonic_s"] for a in rec.action_stream]
        self.assertEqual(stamps, [100.0, 100.5])            # per-axis, not repeated
        self.assertEqual(rec.action_stream[0]["axis"], "power_offset")
        self.assertEqual(rec.action_stream[1]["axis"], "sched_priority")

    def test_pre_attempt_failure_entry_is_skipped(self):
        # an RNTI-unresolved entry (no attempt timestamp) is NOT an executor
        # attempt and must not appear in the stream (but at least one real
        # attempt keeps the actuation stream non-empty).
        ep = _real_ep(applied_action=[
            {"gnb_id": "gnb1", "ue_id": "ue9", "axis": "sched_prio",
             "value": 1.0, "ok": False},                    # no attempt time
            {"gnb_id": "gnb2", "ue_id": None, "axis": "rfatt", "value": 2.0,
             "ok": True, "attempt_monotonic_s": 100.0},
        ])
        rec = _raw(self.r, ep)
        self.assertEqual(len(rec.action_stream), 1)
        self.assertEqual(rec.action_stream[0]["applied_monotonic_s"], 100.0)


# --------------------------------------------------------------------------- #
# budget: PER-CYCLE ledger slice (no row repeats the episode aggregate)        #
# --------------------------------------------------------------------------- #

class PerCycleBudgetLedgerTest(unittest.TestCase):
    def _two_cycle_ledger(self):
        # cap 100; cycle 0 reserves 40 (settled actual 12), cycle 1 reserves 30
        # (settled actual 8). Monotonic reserved_before/cumulative_reserved.
        return {
            "c_episode": 100.0, "reserved": 70.0, "actual": 20.0,
            "remaining": 30.0,
            "entries": [
                {"reservation_id": "r0", "cycle_id": "c0", "cycle_index": 0,
                 "accepted": True, "amount": 40.0, "reserved_before": 0.0,
                 "cumulative_reserved": 40.0, "settled": True},
                {"reservation_id": "r1", "cycle_id": "c1", "cycle_index": 1,
                 "accepted": True, "amount": 30.0, "reserved_before": 40.0,
                 "cumulative_reserved": 70.0, "settled": True},
            ],
            "settlements": {"r0": {"status": "settled", "actual": 12.0},
                            "r1": {"status": "settled", "actual": 8.0}},
        }

    def test_two_cycles_each_get_their_own_slice(self):
        led = self._two_cycle_ledger()
        b0 = ExperimentRunner._budget_from_ledger(led, cycle_id="c0",
                                                  cycle_index=0)
        b1 = ExperimentRunner._budget_from_ledger(led, cycle_id="c1",
                                                  cycle_index=1)
        # cycle 0: before = 100 - 0, reserved 40, after = 100 - 40, debit 12
        self.assertEqual(b0["before"], 100.0)
        self.assertEqual(b0["reserved"], 40.0)
        self.assertEqual(b0["after"], 60.0)
        self.assertEqual(b0["debited"], 12.0)          # ITS OWN actual, not 20
        self.assertEqual(b0["status"], "settled")
        # cycle 1: before = 100 - 40, reserved 30, after = 100 - 70, debit 8
        self.assertEqual(b1["before"], 60.0)
        self.assertEqual(b1["reserved"], 30.0)
        self.assertEqual(b1["after"], 30.0)
        self.assertEqual(b1["debited"], 8.0)           # ITS OWN actual, not 20
        # neither row repeats the episode aggregate debit (20)
        self.assertNotEqual(b0["debited"], 20.0)
        self.assertNotEqual(b1["debited"], 20.0)
        # ledger slice carries ONLY the matching entry + its settlement
        self.assertEqual([e["reservation_id"] for e in b0["ledger"]["entries"]],
                         ["r0"])
        self.assertEqual(set(b0["ledger"]["settlements"]), {"r0"})

    def test_unknown_partial_debit_stays_none_with_lower_bound(self):
        led = {
            "c_episode": 100.0,
            "entries": [
                {"reservation_id": "r0", "cycle_id": "c0", "cycle_index": 0,
                 "accepted": True, "amount": 40.0, "reserved_before": 0.0,
                 "cumulative_reserved": 40.0, "settled": True},
                {"reservation_id": "r0b", "cycle_id": "c0", "cycle_index": 0,
                 "accepted": True, "amount": 10.0, "reserved_before": 40.0,
                 "cumulative_reserved": 50.0, "settled": True},
            ],
            "settlements": {"r0": {"status": "settled", "actual": 7.0},
                            "r0b": {"status": "partial_unknown",
                                    "actual_lower_bound": 3.0}},
        }
        b = ExperimentRunner._budget_from_ledger(led, cycle_id="c0",
                                                 cycle_index=0)
        self.assertIsNone(b["debited"])                 # never a fabricated total
        self.assertEqual(b["lower_bound"], 10.0)        # 7 settled + 3 partial LB
        self.assertEqual(b["status"], "unknown")
        self.assertEqual(b["reserved"], 50.0)
        self.assertEqual(set(b["ledger"]["settlements"]), {"r0", "r0b"})

    def test_rejected_only_cycle_reserves_and_debits_zero(self):
        led = {
            "c_episode": 100.0,
            "entries": [
                {"reservation_id": "r2", "cycle_id": "c2", "cycle_index": 2,
                 "accepted": False, "amount": 50.0, "reserved_before": 70.0,
                 "cumulative_reserved": 70.0, "settled": False}],
            "settlements": {},
        }
        b = ExperimentRunner._budget_from_ledger(led, cycle_id="c2",
                                                 cycle_index=2)
        self.assertEqual(b["reserved"], 0.0)            # nothing reserved
        self.assertEqual(b["debited"], 0.0)             # nothing spent
        self.assertEqual(b["before"], 30.0)             # 100 - 70 (unchanged)
        self.assertEqual(b["after"], 30.0)              # 100 - 70 (unchanged)
        self.assertEqual([e["reservation_id"] for e in b["ledger"]["entries"]],
                         ["r2"])

    def test_settled_status_with_missing_actual_is_unknown(self):
        # a SETTLED status whose actual is missing (None) is NOT a real total:
        # debit stays null and the status is unknown (never a fabricated 0).
        led = {
            "c_episode": 100.0,
            "entries": [
                {"reservation_id": "r0", "cycle_id": "c0", "cycle_index": 0,
                 "accepted": True, "amount": 40.0, "reserved_before": 0.0,
                 "cumulative_reserved": 40.0, "settled": True}],
            "settlements": {"r0": {"status": "settled", "actual": None}},
        }
        b = ExperimentRunner._budget_from_ledger(led, cycle_id="c0",
                                                 cycle_index=0)
        self.assertIsNone(b["debited"])                 # null, not 0
        self.assertEqual(b["status"], "unknown")
        self.assertEqual(b["lower_bound"], 0.0)         # nothing measured
        self.assertEqual(b["reserved"], 40.0)

    def test_no_matching_entries_is_all_none(self):
        led = self._two_cycle_ledger()
        b = ExperimentRunner._budget_from_ledger(led, cycle_id="nope",
                                                 cycle_index=99)
        self.assertIsNone(b["before"])
        self.assertIsNone(b["reserved"])
        self.assertIsNone(b["debited"])
        self.assertIsNone(b["ledger"])

    def test_no_ledger_is_all_none(self):
        b = ExperimentRunner._budget_from_ledger(None, cycle_id="c0",
                                                 cycle_index=0)
        self.assertIsNone(b["debited"])
        self.assertIsNone(b["before"])
        self.assertIsNone(b["ledger"])


# --------------------------------------------------------------------------- #
# missing values stay None (never a fabricated 0)                             #
# --------------------------------------------------------------------------- #

class MissingValuesNoneTest(unittest.TestCase):
    def test_reconnection_time_missing_stays_none(self):
        r = ExperimentRunner(coordinator=None)
        cyc = {"cycle": 0, "trial_success": True,
               "trial_stats": {"tput_before": 8.0, "tput_after": 9.0,
                               "tput_min": 7.0, "tau": 15.0},
               "evidence_episode_id": "e", "proposal_generated": True}
        result = {"success": True, "latency_ms": 10.0,
                  "pending_intent": {"id": "i"}, "experiment_run_id": "run",
                  "fsm_step_id": "fsm", "terminal_outcome": "commit_original",
                  "terminal_reason": "commit_verified"}
        ep = r._episode_from_cycle("m", 1, "P", result, cyc, 8.0,
                                   phase_idx=0)
        self.assertIsNone(ep.reconnection_time)   # NOT 0.0

    def test_latency_components_default_none(self):
        ep = EpisodeRecord(1, "m", "P")
        self.assertIsNone(ep.inference_ms)
        self.assertIsNone(ep.parse_ms)
        self.assertIsNone(ep.total_latency_ms)
        # total_ms never sums unknown components -> None here
        self.assertIsNone(ep.total_ms)

    def test_total_ms_uses_actual_measured_total(self):
        ep = EpisodeRecord(1, "m", "P", total_latency_ms=42.0, inference_ms=None)
        self.assertEqual(ep.total_ms, 42.0)   # measured total, not a sum

    def _cyc_result(self, tstats):
        cyc = {"cycle": 0, "trial_success": True, "trial_stats": tstats,
               "evidence_episode_id": "e", "proposal_generated": True}
        result = {"success": True, "latency_ms": 10.0,
                  "pending_intent": {"id": "i"}, "experiment_run_id": "run",
                  "fsm_step_id": "fsm", "terminal_outcome": "commit_original",
                  "terminal_reason": "commit_verified"}
        return cyc, result

    def test_missing_throughput_measurement_stays_none(self):
        # an UNKNOWN (None) trial-window measurement stays None - NEVER a
        # fabricated 0.0 Mbps; the honest UNKNOWN is throughput_unknown.
        r = ExperimentRunner(coordinator=None)
        cyc, result = self._cyc_result(
            {"tput_before": None, "tput_after": None, "tput_min": None,
             "tau": 15.0})
        ep = r._episode_from_cycle("m", 1, "P", result, cyc,
                                   None, phase_idx=0)   # tp_before None too
        self.assertIsNone(ep.throughput_before)   # NOT 0.0
        self.assertIsNone(ep.throughput_after)    # NOT 0.0
        self.assertIsNone(ep.throughput_trial_min)  # NOT 0.0
        self.assertTrue(ep.throughput_unknown)    # honest flag set

    def test_measured_zero_throughput_is_preserved(self):
        # a MEASURED 0.0 is a real value and must NOT be confused with UNKNOWN.
        r = ExperimentRunner(coordinator=None)
        cyc, result = self._cyc_result(
            {"tput_before": 0.0, "tput_after": 0.0, "tput_min": 0.0,
             "tau": 15.0})
        ep = r._episode_from_cycle("m", 1, "P", result, cyc,
                                   5.5, phase_idx=0)
        self.assertEqual(ep.throughput_before, 0.0)   # measured zero preserved
        self.assertEqual(ep.throughput_after, 0.0)
        self.assertFalse(ep.throughput_unknown)

    def test_legacy_result_missing_throughput_stays_none(self):
        # the no-cycles (legacy) terminal path is equally honest: missing stays
        # None. tp_before None => throughput_before None (no fabricated 0).
        r = ExperimentRunner(coordinator=None)
        result = {"success": True, "feasibility": {}}   # no trial_stats
        trace = {"routed_to": "negotiation", "trial_executed": False,
                 "rolled_back": False, "entered_negotiation": True}
        [ep] = r._episode_from_result("m", 0, "P", result, trace, None)
        self.assertIsNone(ep.throughput_after)     # NOT 0.0
        self.assertIsNone(ep.throughput_trial_min)  # NOT 0.0
        self.assertIsNone(ep.throughput_during_nego)  # NOT 0.0
        self.assertIsNone(ep.throughput_before)    # tp_before None -> None

    def test_regret_decomposition_excludes_unknown_throughput(self):
        # regret_decomposition must NOT crash on a None throughput and an UNKNOWN
        # episode contributes ZERO regret (not a fabricated 0-Mbps delta).
        cfg = IntentConfig(throughput_target_mbps=8.0)
        known = EpisodeRecord(
            1, "m", "Nominal", trial_executed=True, trial_success=True,
            throughput_before=8.0, throughput_after=9.0,
            throughput_trial_min=7.0, tau_trial=15.0)
        unknown = EpisodeRecord(
            2, "m", "Nominal", trial_executed=True, trial_success=True,
            throughput_before=None, throughput_after=None,
            throughput_trial_min=None, tau_trial=15.0,
            throughput_unknown=True)
        r_known = regret_decomposition([known], cfg)
        r_both = regret_decomposition([known, unknown], cfg)   # no raise
        self.assertEqual(r_both["total_regret"]["value"],
                         r_known["total_regret"]["value"])
        # the unknown episode is EXPLICITLY excluded (coverage count), not a fake 0
        self.assertEqual(r_both["decision_regret"]["n_unknown"], 1)


# --------------------------------------------------------------------------- #
# evidence + transaction carry the monotonic apply timestamp                  #
# --------------------------------------------------------------------------- #

class EvidenceMonotonicTest(unittest.TestCase):
    def test_evidence_record_has_action_apply_monotonic_s(self):
        rec = EvidenceRecord(
            experiment_run_id="run", episode_id="ep", fsm_step_id="fsm",
            proposer_id="p", model_version="m", intent_set_version="v",
            pending_intent_hash="h", evidence_record_id="er",
            action_apply_time=1.0, action_apply_monotonic_s=2.0,
            prompt_hash="prompt-sha256-" + "b" * 64)
        d = rec.to_dict()
        self.assertEqual(d["action_apply_monotonic_s"], 2.0)
        self.assertEqual(d["action_apply_time"], 1.0)         # epoch stays epoch
        self.assertEqual(d["prompt_hash"], "prompt-sha256-" + "b" * 64)

    def test_note_real_write_preserves_attempt_monotonic(self):
        tx = ActuationTransaction()
        tx.action_apply_monotonic_s = 999.0        # set at the apply attempt
        tx.note_real_write(123.0, "rfatt")         # post-return call
        self.assertEqual(tx.action_apply_monotonic_s, 999.0)  # NOT overwritten
        self.assertEqual(tx.action_apply_time, 123.0)         # epoch recorded


# --------------------------------------------------------------------------- #
# feasibility boundary: prompt-hash propagation, timeout stamping, strict bool #
# --------------------------------------------------------------------------- #

def _intent():
    return Intent(type=IntentType.THROUGHPUT_GOAL,
                  target=IntentTarget(kpi_name="throughput",
                                      constraint_type=ConstraintType.MIN,
                                      target_value=8.0, unit="Mbps"))


class FeasibilityBoundaryHardeningTest(unittest.TestCase):
    def _min_coord(self):
        """A hand-built IntentCoordinator carrying ONLY the state
        _analyze_feasibility / _stamp_proposal_stage read. history + intent-hash
        are stubbed so the test isolates the prompt-hash / timeout / success
        boundaries."""
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.gui = None
        c._cur_prompt_hash = None
        c._cur_inference_ms = None
        c._cur_schema_valid = None
        c._cur_schema_reason = None
        c._cur_proposal_generated = False
        c.history_reservoir = []
        c._history_for_prompt = lambda intent: []
        c._intent_content_hash = lambda intent: "sig"
        return c

    # -- Task 1: prompt-hash failure propagates BEFORE any bounded model call -- #
    def test_prompt_hash_failure_propagates_with_zero_backend_calls(self):
        calls = {"backend": 0, "bounded": 0}
        c = self._min_coord()

        class _Mgr:
            def build_feasibility_prompt_hash(self, *a, **k):
                raise RuntimeError("prompt build/hash boom")

            def generate(self, *a, **k):
                calls["backend"] += 1
                return LLMResponse(success=True, content="{}")
        c.llm_manager = _Mgr()

        def _bounded(fn, timeout_s, label=""):
            calls["bounded"] += 1
            return fn()
        c._bounded_call = _bounded

        with self.assertRaises(RuntimeError):
            c._analyze_feasibility(_intent(), [], NetworkState(ue_states={}))
        self.assertEqual(calls["bounded"], 0)   # no bounded model call issued
        self.assertEqual(calls["backend"], 0)   # backend never called
        self.assertIsNone(c._cur_prompt_hash)   # no fabricated hash

    # -- Task 2: a model timeout retains the real hash + measures inference --- #
    def test_timeout_retains_prompt_hash_and_measures_inference(self):
        c = self._min_coord()
        real_hash = "prompt-sha256-" + "a" * 64

        class _Mgr:
            def build_feasibility_prompt_hash(self, *a, **k):
                return real_hash
        c.llm_manager = _Mgr()

        def _bounded(fn, timeout_s, label=""):
            raise OperationTimeout("model call timed out")
        c._bounded_call = _bounded

        with self.assertRaises(OperationTimeout):
            c._analyze_feasibility(_intent(), [], NetworkState(ue_states={}))
        self.assertEqual(c._cur_prompt_hash, real_hash)   # retained on timeout
        self.assertIsNotNone(c._cur_inference_ms)         # measured, not lost
        self.assertGreaterEqual(c._cur_inference_ms, 0.0)  # finite/nonnegative
        self.assertTrue(math.isfinite(c._cur_inference_ms))
        self.assertFalse(c._cur_proposal_generated)       # no proposal on timeout

    def test_stamp_proposal_stage_is_honest_on_timeout(self):
        # the finally-stamped fields for a timed-out cycle: real hash retained,
        # measured inference, proposal_generated False, proposal_id None,
        # schema_valid None - never a fabricated stage id or verdict.
        c = self._min_coord()
        c._cur_proposal_generated = False
        c._cur_prompt_hash = "prompt-sha256-" + "a" * 64
        c._cur_inference_ms = 12.5
        cycle = {}
        c._stamp_proposal_stage(cycle)
        self.assertFalse(cycle["proposal_generated"])
        self.assertIsNone(cycle["proposal_id"])            # NOT minted
        self.assertIsNone(cycle["schema_valid"])           # no verdict
        self.assertEqual(cycle["prompt_hash"], "prompt-sha256-" + "a" * 64)
        self.assertEqual(cycle["inference_ms"], 12.5)

    def test_stamp_proposal_stage_mints_id_only_when_generated(self):
        c = self._min_coord()
        bound = {}
        c._bind_proposal_id = lambda pid: bound.__setitem__("id", pid)
        c._cur_proposal_generated = True
        c._cur_schema_valid = True
        c._cur_prompt_hash = "prompt-sha256-" + "b" * 64
        c._cur_inference_ms = 5.0
        cycle = {}
        c._stamp_proposal_stage(cycle)
        self.assertTrue(cycle["proposal_generated"])
        self.assertTrue(cycle["proposal_id"])              # minted
        self.assertEqual(bound["id"], cycle["proposal_id"])  # bound to same id
        self.assertTrue(cycle["schema_valid"])

    # -- Task 3: response.success must be EXACTLY bool (no bool() coercion) ---- #
    def test_non_bool_success_fails_closed(self):
        real_hash = "prompt-sha256-" + "c" * 64
        for bad in ("true", "false", 1, 0, None, 1.0):
            c = self._min_coord()

            class _Mgr:
                def build_feasibility_prompt_hash(self, *a, **k):
                    return real_hash
            c.llm_manager = _Mgr()

            class _BadResp:
                success = bad
                content = "{}"
                parsed_json = None
                prompt_hash = None
            c._bounded_call = lambda fn, timeout_s, label="": _BadResp()

            with self.assertRaises(MalformedResponseError):
                c._analyze_feasibility(_intent(), [],
                                       NetworkState(ue_states={}))
            # never coerced into a truthy proposal-generated verdict
            self.assertFalse(getattr(c, "_cur_proposal_generated", False))

    def test_exact_bool_success_is_accepted(self):
        # the well-formed contract still works: a real bool success routes as
        # before (False -> infeasible, proposal_generated False; no raise).
        c = self._min_coord()

        class _Mgr:
            def build_feasibility_prompt_hash(self, *a, **k):
                return "prompt-sha256-" + "d" * 64
        c.llm_manager = _Mgr()

        class _Resp:
            success = False
            content = ""
            parsed_json = None
            prompt_hash = None
        c._bounded_call = lambda fn, timeout_s, label="": _Resp()
        pred = c._analyze_feasibility(_intent(), [], NetworkState(ue_states={}))
        self.assertFalse(pred.feasible)                    # routed infeasible
        self.assertFalse(c._cur_proposal_generated)        # no output -> False


# --------------------------------------------------------------------------- #
# cycle-local provenance: two-cycle mapping retains each cycle's own facts     #
# --------------------------------------------------------------------------- #

class CycleLocalProvenanceMappingTest(unittest.TestCase):
    def _cycle(self, idx, proposer, model, intent_id, target_value, mono):
        snap = {
            "id": intent_id, "type": "throughput_goal",
            "target": {"kpi_name": "throughput", "constraint_type": "min",
                       "target_value": target_value, "unit": "Mbps"},
            "scope": {"ue_ids": ["ue1"], "bs_ids": []},
        }
        return {
            "cycle": idx, "cycle_id": f"cyc-{idx}", "episode_id": "ep-1",
            "fsm_step_id": f"fsm-{idx}", "proposal_id": f"prop-{idx}",
            "actuation_trial_id": f"trial-{idx}",
            "proposal_generated": True, "schema_valid": True,
            "trial_success": True,
            "cycle_monotonic_s": mono,
            "proposer_id": proposer, "model_version": model,
            "prompt_hash": "prompt-sha256-" + ("%064d" % idx),
            "current_intent": snap,
            "current_intent_id": intent_id,
            "current_intent_type": "throughput_goal",
            "current_intent_target": snap["target"],
            "current_intent_scope": snap["scope"],
            "intent_content_hash": f"ic-hash-{idx}",
            "pending_intent_hash": f"pi-hash-{idx}",
            "requested_action": {"bs2": {"power_offset": float(idx)}},
            "canonical_action": {"bs2": {"power_offset": float(idx)}},
            "final_readback": {"bs2": {"power_offset": float(idx)}},
            "applied_action": [{"gnb_id": "gnb2", "ue_id": None,
                                "axis": "rfatt", "value": float(idx),
                                "ok": True, "attempt_monotonic_s": mono}],
            "action_apply_time": 100.0 + idx,
            "action_apply_monotonic_s": mono,
        }

    def _two_cycle_result(self):
        c0 = self._cycle(0, "proposerA", "modelA", "int-A", 8.0, 111.0)
        c1 = self._cycle(1, "proposerB", "modelB", "int-B", 5.0, 222.0)
        result = {
            "success": True, "experiment_run_id": "run-1",
            "fsm_step_id": "fsm-top", "terminal_outcome": "commit_revised",
            "terminal_reason": "commit_verified",
            "cycles": [c0, c1],
            # the FINAL episode evidence + top-level pending_intent are the LAST
            # cycle's values ONLY - they must NEVER be copied into cycle 0's row.
            # The finalized evidence AGREES with the last cycle (c1): the pi- hash,
            # prompt hash, and the FLATTENED applied-map (gnb.ue_or_cell.axis).
            "evidence": {"proposer_id": "proposerB", "model_version": "modelB",
                         "pending_intent_hash": "pi-hash-1",
                         "prompt_hash": "prompt-sha256-" + ("%064d" % 1),
                         "clipped_action": {"gnb2.cell.rfatt": 1.0}},
            "pending_intent": {"id": "int-B", "type": "throughput_goal",
                               "target": {"target_value": 5.0}, "scope": {}},
        }
        return result, c0, c1

    def test_each_episode_row_retains_its_own_cycle_facts(self):
        result, c0, c1 = self._two_cycle_result()
        r = ExperimentRunner(coordinator=None)
        ep0 = r._episode_from_cycle("m", 1, "Nominal", result, c0, 8.0,
                                    phase_idx=0)
        ep1 = r._episode_from_cycle("m", 1, "Nominal", result, c1, 8.0,
                                    phase_idx=1)
        # cycle 0 keeps ITS proposer/model/intent/hash - NOT the final evidence's
        # (proposerB / modelB / int-B), which is the LAST cycle's only.
        self.assertEqual(ep0.evidence_proposer_id, "proposerA")
        self.assertEqual(ep0.evidence_model_version, "modelA")
        self.assertEqual(ep0.evidence_pending_intent_id, "int-A")
        self.assertEqual(ep0.evidence_intent_target["target_value"], 8.0)
        self.assertEqual(ep0.evidence_pending_intent_hash, "ic-hash-0")
        self.assertEqual(ep0.evidence_prompt_hash,
                         "prompt-sha256-" + ("%064d" % 0))
        # cycle 1 keeps its OWN (revised) facts
        self.assertEqual(ep1.evidence_proposer_id, "proposerB")
        self.assertEqual(ep1.evidence_model_version, "modelB")
        self.assertEqual(ep1.evidence_pending_intent_id, "int-B")
        self.assertEqual(ep1.evidence_intent_target["target_value"], 5.0)
        self.assertEqual(ep1.evidence_pending_intent_hash, "ic-hash-1")
        # the two rows differ (a revision genuinely changed proposer + target)
        self.assertNotEqual(ep0.evidence_proposer_id, ep1.evidence_proposer_id)
        self.assertNotEqual(ep0.evidence_intent_target["target_value"],
                            ep1.evidence_intent_target["target_value"])

    def test_cycle_timestamps_differ_and_are_positive(self):
        result, c0, c1 = self._two_cycle_result()
        r = ExperimentRunner(coordinator=None)
        ep0 = r._episode_from_cycle("m", 1, "Nominal", result, c0, 8.0,
                                    phase_idx=0)
        ep1 = r._episode_from_cycle("m", 1, "Nominal", result, c1, 8.0,
                                    phase_idx=1)
        self.assertEqual(ep0.episode_monotonic_s, 111.0)   # this cycle's stamp
        self.assertEqual(ep1.episode_monotonic_s, 222.0)
        self.assertNotEqual(ep0.episode_monotonic_s, ep1.episode_monotonic_s)
        self.assertGreater(ep0.episode_monotonic_s, 0.0)
        self.assertGreater(ep1.episode_monotonic_s, 0.0)

    def test_raw_rows_retain_their_own_cycle_facts(self):
        result, c0, c1 = self._two_cycle_result()
        r = ExperimentRunner(coordinator=None)
        ep0 = r._episode_from_cycle("m", 1, "Nominal", result, c0, 8.0,
                                    phase_idx=0)
        ep1 = r._episode_from_cycle("m", 1, "Nominal", result, c1, 8.0,
                                    phase_idx=1)
        runner = _emulated_raw_runner()
        raw0 = _raw(runner, ep0)
        raw1 = _raw(runner, ep1)
        # model_id on the pipeline raw row is the cycle's OWN pinned proposer.
        self.assertEqual(raw0.model_id, "proposerA")
        self.assertEqual(raw1.model_id, "proposerB")
        self.assertEqual(raw0.model_version, "modelA")
        self.assertEqual(raw1.model_version, "modelB")
        self.assertEqual(raw0.pending_intent_id, "int-A")
        self.assertEqual(raw1.pending_intent_id, "int-B")
        self.assertEqual(raw0.intent_target["target_value"], 8.0)
        self.assertEqual(raw1.intent_target["target_value"], 5.0)
        self.assertEqual(raw0.episode_monotonic_s, 111.0)
        self.assertEqual(raw1.episode_monotonic_s, 222.0)


# --------------------------------------------------------------------------- #
# final-cycle Evidence consistency (raise) + flat clipped-map + terminal mono  #
# --------------------------------------------------------------------------- #

class FinalCycleConsistencyTest(unittest.TestCase):
    def _cyc_and_result(self, evidence_over=None, cyc_over=None):
        applied = [{"gnb_id": "gnb2", "ue_id": None, "axis": "rfatt",
                    "value": 2.0, "ok": True, "attempt_monotonic_s": 5.0}]
        clipped_map = {"gnb2.cell.rfatt": 2.0}
        cyc = {
            "cycle": 0, "cycle_id": "c0", "episode_id": "e", "fsm_step_id": "f",
            "proposal_id": "p", "actuation_trial_id": "t",
            "proposal_generated": True, "schema_valid": True,
            "trial_success": True, "cycle_monotonic_s": 10.0,
            "proposer_id": "prA", "model_version": "mA",
            "pending_intent_hash": "pi-abc",
            "prompt_hash": "prompt-sha256-" + "a" * 64,
            "intent_content_hash": "ic-abc",
            "current_intent": {"id": "int-A", "type": "throughput_goal",
                               "target": {"target_value": 8.0},
                               "scope": {"ue_ids": []}},
            "current_intent_id": "int-A",
            "current_intent_type": "throughput_goal",
            "current_intent_target": {"target_value": 8.0},
            "current_intent_scope": {"ue_ids": []},
            "requested_action": {"bs2": {"power_offset": 2.0}},
            "canonical_action": {"bs2": {"power_offset": 2.0}},
            "canonical_action_hash": "cah-1",
            "final_readback": {"bs2": {"power_offset": 2.0}},
            "applied_action": applied,
            "action_apply_time": 100.0, "action_apply_monotonic_s": 5.0,
        }
        if cyc_over:
            cyc.update(cyc_over)
        evidence = {"proposer_id": "prA", "model_version": "mA",
                    "pending_intent_hash": "pi-abc",
                    "prompt_hash": "prompt-sha256-" + "a" * 64,
                    "requested_action": {"bs2": {"power_offset": 2.0}},
                    "clipped_action": clipped_map,
                    "canonical_action": {"bs2": {"power_offset": 2.0}},
                    "canonical_action_hash": "cah-1"}
        if evidence_over:
            evidence.update(evidence_over)
        result = {"success": True, "experiment_run_id": "run",
                  "terminal_outcome": "commit_original",
                  "terminal_reason": "commit_verified",
                  "cycles": [cyc], "evidence": evidence}
        return cyc, result

    def test_matching_final_cycle_does_not_raise(self):
        cyc, result = self._cyc_and_result()
        r = ExperimentRunner(coordinator=None)
        ep = r._episode_from_cycle("m", 1, "P", result, cyc, 8.0,
                                   phase_idx=0)
        self.assertEqual(ep.evidence_proposer_id, "prA")

    def test_clipped_action_is_flat_applied_map_not_canonical(self):
        # item 2: clipped_action is the flattened applied-map, NEVER the canonical.
        cyc, result = self._cyc_and_result()
        r = ExperimentRunner(coordinator=None)
        ep = r._episode_from_cycle("m", 1, "P", result, cyc, 8.0,
                                   phase_idx=0)
        self.assertEqual(ep.evidence_clipped_action, {"gnb2.cell.rfatt": 2.0})
        self.assertEqual(ep.evidence_canonical_action,
                         {"bs2": {"power_offset": 2.0}})
        self.assertNotEqual(ep.evidence_clipped_action,
                            ep.evidence_canonical_action)

    def test_each_evidence_field_mismatch_raises(self):
        r = ExperimentRunner(coordinator=None)
        for fld, bad in (("proposer_id", "OTHER"),
                         ("model_version", "OTHER"),
                         ("pending_intent_hash", "pi-XXX"),
                         ("prompt_hash", "prompt-sha256-" + "b" * 64),
                         ("requested_action", {"bsX": {"p": 9.0}}),
                         ("clipped_action", {"gnbX.cell.rfatt": 9.0}),
                         ("canonical_action", {"bsX": {"p": 9.0}}),
                         ("canonical_action_hash", "cah-OTHER")):
            cyc, result = self._cyc_and_result({fld: bad})
            with self.assertRaises(ValueError):
                r._episode_from_cycle("m", 1, "P", result, cyc, 8.0,
                                      phase_idx=0)

    def test_identifier_chain_run_terminal_mismatch_fail_closed(self):
        # P1-6 (item 4): the run/fsm/terminal AND the identifier chain
        # (episode_id/cycle_id/proposal_id/actuation_trial_id) the raw exporter
        # threads from top-level/cycle MUST match the FINALIZED EvidenceRecord;
        # any inconsistency FAILS CLOSED (checked only when Evidence provides the
        # field, so stage-honest None is preserved).
        r = ExperimentRunner(coordinator=None)
        for fld, bad in (("experiment_run_id", "run-X"),
                         ("fsm_step_id", "f-X"),
                         ("terminal_outcome", "reject-X"),
                         ("terminal_reason", "why-X"),
                         ("episode_id", "e-X"),
                         ("cycle_id", "c-X"),
                         ("proposal_id", "p-X"),
                         ("actuation_trial_id", "t-X")):
            cyc, result = self._cyc_and_result(evidence_over={fld: bad})
            with self.assertRaises(ValueError):
                r._episode_from_cycle("m", 1, "P", result, cyc, 8.0,
                                      phase_idx=0)

    def test_evidence_none_or_empty_field_is_not_checked(self):
        # a field the Evidence does not meaningfully provide (None / {} / "") is
        # never a mismatch - the cycle's own value is retained.
        for over in ({"proposer_id": None}, {"requested_action": {}},
                     {"canonical_action_hash": ""}):
            cyc, result = self._cyc_and_result(over)
            r = ExperimentRunner(coordinator=None)
            ep = r._episode_from_cycle("m", 1, "P", result, cyc, 8.0,
                                       phase_idx=0)
            self.assertEqual(ep.evidence_proposer_id, "prA")

    def test_missing_cycle_value_with_meaningful_evidence_raises(self):
        # a MEANINGFUL Evidence value with a MISSING cycle value fails closed
        # (no cycle-present skip): the cycle stamps proposer/model/hash/prompt at
        # its boundary, so a None there for a provided Evidence field is a fault.
        r = ExperimentRunner(coordinator=None)
        for missing in ("proposer_id", "model_version", "pending_intent_hash",
                        "prompt_hash"):
            cyc, result = self._cyc_and_result()
            del cyc[missing]
            with self.assertRaises(ValueError):
                r._episode_from_cycle("m", 1, "P", result, cyc, 8.0,
                                      phase_idx=0)

    def test_earlier_cycle_is_not_checked_against_final_evidence(self):
        # only the LAST cycle is checked; an earlier (revised) cycle may diverge
        # from the finalized evidence without raising.
        cyc0, result = self._cyc_and_result(cyc_over={"proposer_id": "prDIFF"})
        cyc1, _ = self._cyc_and_result()
        cyc1["cycle"] = 1
        cyc1["cycle_id"] = "c1"
        result["cycles"] = [cyc0, cyc1]     # cyc0 is NOT last
        r = ExperimentRunner(coordinator=None)
        ep0 = r._episode_from_cycle("m", 1, "P", result, cyc0, 8.0,
                                    phase_idx=0)
        self.assertEqual(ep0.evidence_proposer_id, "prDIFF")   # no raise


class TerminalEvidenceMonotonicTest(unittest.TestCase):
    def test_terminal_evidence_preserves_action_apply_monotonic_s(self):
        # item 3: a non-commit (rollback / technical) terminal EvidenceRecord must
        # carry the last cycle's action_apply_monotonic_s (previously omitted).
        import types
        from coordinator.intent_coordinator import IntentCoordinator
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.experiment_run_id = "run"
        c._reserve_ledger = None
        c._proposer_context = lambda: ("p", "m")
        c._resolve_trigger_context = lambda: None
        c._intent_set_version = lambda: "iset-x"
        c._pending_intent_hash = lambda result: "pi-x"
        c._resolve_calibration = lambda: {}
        c._probe_cfg = lambda: types.SimpleNamespace(to_dict=lambda: {})
        last = {"action_apply_time": 123.0, "action_apply_monotonic_s": 999.0,
                "applied_action": []}
        result = {"experiment_run_id": "run", "episode_id": "e",
                  "fsm_step_id": "f", "evidence_record_id": "er",
                  "cycles": [last]}
        evidence, err = c._build_evidence(
            result, TerminalOutcome.PENDING_NOT_ADMITTED,
            TerminalReason.DEADLINE_EXHAUSTED)
        self.assertIsNone(err)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.action_apply_monotonic_s, 999.0)
        self.assertEqual(evidence.to_dict()["action_apply_monotonic_s"], 999.0)
        self.assertEqual(evidence.to_dict()["action_apply_time"], 123.0)


class CyclePendingIntentHashTest(unittest.TestCase):
    def _coord(self):
        from coordinator.intent_coordinator import IntentCoordinator
        c = IntentCoordinator.__new__(IntentCoordinator)
        c._proposer_context = lambda: ("p", "m")
        return c

    def test_cycle_hash_is_pi_digest_of_frozen_snapshot(self):
        from coordinator.intent_coordinator import IntentCoordinator
        c = self._coord()
        intent = _intent()
        cyc = {}
        c._stamp_cycle_provenance(cyc, intent)
        expect = IntentCoordinator._hash_intent_basis(intent.to_dict())
        self.assertEqual(cyc["pending_intent_hash"], expect)
        self.assertTrue(cyc["pending_intent_hash"].startswith("pi-"))
        # the semantic content hash is a DIFFERENT digest (ic-), not reused here.
        self.assertNotEqual(cyc["pending_intent_hash"],
                            cyc["intent_content_hash"])

    def test_revised_intent_changes_cycle_hash(self):
        c = self._coord()
        i1 = _intent()
        i2 = Intent(type=IntentType.THROUGHPUT_GOAL,
                    target=IntentTarget(kpi_name="throughput",
                                        constraint_type=ConstraintType.MIN,
                                        target_value=5.0, unit="Mbps"))
        cyc1, cyc2 = {}, {}
        c._stamp_cycle_provenance(cyc1, i1)
        c._stamp_cycle_provenance(cyc2, i2)
        self.assertNotEqual(cyc1["pending_intent_hash"],
                            cyc2["pending_intent_hash"])

    def test_no_intent_cycle_hash_is_none(self):
        c = self._coord()
        cyc = {}
        c._stamp_cycle_provenance(cyc, None)
        self.assertIsNone(cyc["pending_intent_hash"])


if __name__ == "__main__":
    unittest.main()
