"""Batch A: terminal outcome unification + finalization funnel.

Covers sections 3.1 / 3.2 and P0-4: every episode terminates in exactly one of
four TerminalOutcome values; success is derived from the outcome (True only for
commits); no path leaves resolution=None; the (outcome, reason) pairing is
validated so a mismatch cannot be emitted; the finalizer produces an explicit
pending_intent (preserved on every path) and committed_revision (only for a
CommitRevised); and final-cleanup failures still return a terminal outcome.
"""

import unittest

from coordinator.intent_coordinator import (
    IntentCoordinator, IntentManager, IntentParseError,
)
from coordinator.episode_types import (
    ALLOWED_REASONS, EvidenceRecord, MonitorVerdict, TerminalOutcome,
    TerminalReason, TerminalRecord, is_valid_outcome_reason,
    legacy_resolution, legacy_success, validate_outcome_reason,
)


def _ev(outcome=None, reason=None):
    """A minimal valid EvidenceRecord for TerminalRecord construction tests."""
    return EvidenceRecord(
        experiment_run_id="r", episode_id="e", fsm_step_id="f",
        evidence_record_id="evid", proposer_id="p", model_version="m",
        intent_set_version="v", pending_intent_hash="h",
        terminal_outcome=(outcome.value if outcome else None),
        terminal_reason=(reason.value if reason else None))
from decision.intent_model import (
    Alternative, ConstraintType, FeasibilityPrediction, Intent, IntentTarget,
    IntentType, NetworkState,
)


# --------------------------------------------------------------------------
# Skeleton coordinator (mirrors tests/test_trial_semantics._make_coordinator):
# __new__ skips __init__ so no hardware/LLM is built, but the REAL S0-S6
# control flow, _negotiate, result assembly AND the finalizer run.
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


class _FakeCollector:
    def __init__(self, metrics=None):
        self.metrics = metrics or {}

    def collect_all(self):
        return dict(self.metrics)


def _make_intent(value=8.0) -> Intent:
    return Intent(type=IntentType.THROUGHPUT_GOAL,
                  target=IntentTarget(kpi_name="throughput",
                                      constraint_type=ConstraintType.MIN,
                                      target_value=value, unit="Mbps"))


def _make_coordinator(states=None):
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

    intent = _make_intent()
    alt = Alternative(id="a1", description="relax I2 to 6 Mbps")
    c._parse_intent = lambda text: {"intent": intent, "raw": {}}
    c._check_conflicts = lambda new, active: True   # skip S1 shortcut
    c._get_network_state = lambda: NetworkState(ue_states={})
    c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
        feasible=True, confidence=0.9, reasoning="", alternatives=[alt])
    return c, states


# --------------------------------------------------------------------------
# Pure enum / adapter contract
# --------------------------------------------------------------------------

class TerminalOutcomeContractTest(unittest.TestCase):

    def test_terminal_outcome_is_exhaustive(self):
        # exactly four terminal outcomes (section 3.1)
        self.assertEqual(
            {o.value for o in TerminalOutcome},
            {"commit_original", "commit_revised",
             "pending_not_admitted", "technical_failsafe"})
        for outcome in TerminalOutcome:
            self.assertIsInstance(legacy_success(outcome), bool)
            self.assertIn(
                legacy_resolution(outcome, TerminalReason.INTERNAL_ERROR),
                {"accept", "accept_modified", "reject"})

    def test_success_only_for_commit_outcomes(self):
        # P0-4: success is True iff the outcome is a commit
        commits = {TerminalOutcome.COMMIT_ORIGINAL,
                   TerminalOutcome.COMMIT_REVISED}
        for outcome in TerminalOutcome:
            self.assertEqual(outcome.is_commit, outcome in commits)
            self.assertEqual(legacy_success(outcome), outcome in commits)

    def test_legacy_resolution_mapping(self):
        self.assertEqual(
            legacy_resolution(TerminalOutcome.COMMIT_ORIGINAL,
                              TerminalReason.COMMIT_VERIFIED), "accept")
        self.assertEqual(
            legacy_resolution(TerminalOutcome.COMMIT_REVISED,
                              TerminalReason.COMMIT_VERIFIED),
            "accept_modified")
        self.assertEqual(
            legacy_resolution(TerminalOutcome.PENDING_NOT_ADMITTED,
                              TerminalReason.NO_ACCEPTABLE_ALTERNATIVE),
            "reject")
        self.assertEqual(
            legacy_resolution(TerminalOutcome.TECHNICAL_FAILSAFE,
                              TerminalReason.INTERNAL_ERROR), "reject")


class OutcomeReasonContractTest(unittest.TestCase):
    """P0-4: a total allowed-reasons-by-outcome contract (COMMIT_VERIFIED is
    valid for BOTH commit outcomes), and mismatches cannot be emitted."""

    def test_contract_is_total_over_outcomes_and_reasons(self):
        self.assertEqual(set(ALLOWED_REASONS), set(TerminalOutcome))
        for outcome, reasons in ALLOWED_REASONS.items():
            self.assertTrue(reasons, f"{outcome} has no allowed reasons")
        union = set().union(*ALLOWED_REASONS.values())
        self.assertEqual(union, set(TerminalReason))  # every reason mapped

    def test_commit_verified_valid_for_both_commits(self):
        self.assertTrue(is_valid_outcome_reason(
            TerminalOutcome.COMMIT_ORIGINAL, TerminalReason.COMMIT_VERIFIED))
        self.assertTrue(is_valid_outcome_reason(
            TerminalOutcome.COMMIT_REVISED, TerminalReason.COMMIT_VERIFIED))

    def test_mismatched_pair_is_invalid(self):
        self.assertFalse(is_valid_outcome_reason(
            TerminalOutcome.COMMIT_ORIGINAL,
            TerminalReason.NO_ACCEPTABLE_ALTERNATIVE))
        self.assertFalse(is_valid_outcome_reason(
            TerminalOutcome.PENDING_NOT_ADMITTED,
            TerminalReason.COMMIT_VERIFIED))
        with self.assertRaises(ValueError):
            validate_outcome_reason(TerminalOutcome.TECHNICAL_FAILSAFE,
                                    TerminalReason.COMMIT_VERIFIED)

    def test_terminal_record_rejects_mismatch(self):
        with self.assertRaises(ValueError):
            TerminalRecord(TerminalOutcome.COMMIT_ORIGINAL,
                           TerminalReason.PARSE_FAILED)

    def test_committed_revision_only_for_commit(self):
        with self.assertRaises(ValueError):
            TerminalRecord(TerminalOutcome.PENDING_NOT_ADMITTED,
                           TerminalReason.PARSE_FAILED,
                           evidence=_ev(TerminalOutcome.PENDING_NOT_ADMITTED,
                                        TerminalReason.PARSE_FAILED),
                           committed_revision="x")

    def test_both_commits_require_committed_revision(self):
        # item B counterexample: a commit with committed_revision=None is
        # invalid for BOTH commit outcomes.
        for oc in (TerminalOutcome.COMMIT_ORIGINAL,
                   TerminalOutcome.COMMIT_REVISED):
            with self.assertRaises(ValueError):
                TerminalRecord(oc, TerminalReason.COMMIT_VERIFIED,
                               evidence=_ev(oc, TerminalReason.COMMIT_VERIFIED),
                               committed_revision=None)
            rec = TerminalRecord(
                oc, TerminalReason.COMMIT_VERIFIED,
                evidence=_ev(oc, TerminalReason.COMMIT_VERIFIED),
                committed_revision="rev")
            self.assertEqual(rec.to_dict()["committed_revision"], "rev")

    def test_evidence_is_mandatory(self):
        # item D: a TerminalRecord cannot be emitted without an evidence bundle
        with self.assertRaises(ValueError):
            TerminalRecord(TerminalOutcome.PENDING_NOT_ADMITTED,
                           TerminalReason.PARSE_FAILED)

    def test_evidence_terminal_fields_must_match(self):
        # item D: evidence whose terminal fields contradict the pair is rejected
        with self.assertRaises(ValueError):
            TerminalRecord(
                TerminalOutcome.PENDING_NOT_ADMITTED,
                TerminalReason.PARSE_FAILED,
                evidence=_ev(TerminalOutcome.COMMIT_ORIGINAL,
                             TerminalReason.COMMIT_VERIFIED))
        # matching evidence is accepted
        rec = TerminalRecord(
            TerminalOutcome.PENDING_NOT_ADMITTED, TerminalReason.PARSE_FAILED,
            evidence=_ev(TerminalOutcome.PENDING_NOT_ADMITTED,
                         TerminalReason.PARSE_FAILED))
        self.assertEqual(rec.to_dict()["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)


class MonitorVerdictTest(unittest.TestCase):

    def test_monitor_verdict_from_optional_bool(self):
        self.assertIs(MonitorVerdict.from_optional_bool(None),
                      MonitorVerdict.UNKNOWN)
        self.assertIs(MonitorVerdict.from_optional_bool(True),
                      MonitorVerdict.SATISFIED)
        self.assertIs(MonitorVerdict.from_optional_bool(False),
                      MonitorVerdict.VIOLATED)

    def test_unknown_is_not_satisfied(self):
        self.assertFalse(MonitorVerdict.UNKNOWN.is_satisfied)
        self.assertFalse(MonitorVerdict.VIOLATED.is_satisfied)
        self.assertTrue(MonitorVerdict.SATISFIED.is_satisfied)


# --------------------------------------------------------------------------
# Typed evaluator is authoritative (item 4)
# --------------------------------------------------------------------------

class TypedEvaluatorTest(unittest.TestCase):

    class _M:
        def __init__(self, tput, attached=True):
            self.throughput_mbps = tput
            self.latency_ms = None
            self.attached = attached

    def _tput_intent(self):
        return _make_intent(8.0)

    def test_verdict_is_the_authority(self):
        c, _ = _make_coordinator()
        sat = {"ue1": self._M(9.0)}
        vio = {"ue1": self._M(3.0)}
        gap = {"ue1": self._M(None)}
        self.assertIs(c._evaluate_intent_verdict(self._tput_intent(), sat),
                      MonitorVerdict.SATISFIED)
        self.assertIs(c._evaluate_intent_verdict(self._tput_intent(), vio),
                      MonitorVerdict.VIOLATED)
        self.assertIs(c._evaluate_intent_verdict(self._tput_intent(), gap),
                      MonitorVerdict.UNKNOWN)

    def test_bool_and_string_adapters_agree(self):
        c, _ = _make_coordinator()
        gap = {"ue1": self._M(None)}
        # UNKNOWN must never read as satisfied through the legacy adapters
        self.assertFalse(c._evaluate_intent(self._tput_intent(), gap))
        self.assertEqual(c._evaluate_intent_tristate(self._tput_intent(), gap),
                         "unknown")
        sat = {"ue1": self._M(9.0)}
        self.assertTrue(c._evaluate_intent(self._tput_intent(), sat))
        self.assertEqual(c._evaluate_intent_tristate(self._tput_intent(), sat),
                         "satisfied")


# --------------------------------------------------------------------------
# Finalization funnel over the real process_intent flow
# --------------------------------------------------------------------------

class FinalizeEpisodeTest(unittest.TestCase):

    def _assert_terminal(self, result):
        self.assertIn(result["terminal_outcome"],
                      {o.value for o in TerminalOutcome})
        self.assertIsNotNone(result["resolution"])
        self.assertIsNotNone(result.get("terminal_reason"))
        self.assertIn("pending_intent", result)
        self.assertIn("committed_revision", result)
        is_commit = result["terminal_outcome"] in (
            TerminalOutcome.COMMIT_ORIGINAL.value,
            TerminalOutcome.COMMIT_REVISED.value)
        self.assertEqual(result["success"], is_commit)
        # both commits name what they committed; non-commits carry None
        if is_commit:
            self.assertIsNotNone(result["committed_revision"])
        else:
            self.assertIsNone(result["committed_revision"])

    def test_parse_failure_finalizes_pending_not_admitted(self):
        c, _ = _make_coordinator()

        def _raise(text):
            raise IntentParseError("unsupported type 'coverage_goal'")

        c._parse_intent = _raise
        result = c.process_intent("ensure coverage everywhere")
        self._assert_terminal(result)
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.INPUT_SCHEMA_REJECTED.value)
        self.assertEqual(result["resolution"], "reject")
        self.assertFalse(result["success"])
        # pending intent preserved as the RAW text (parse failed)
        self.assertEqual(result["pending_intent"], "ensure coverage everywhere")

    def test_generic_illegal_event_finalizes_failsafe(self):
        c, _ = _make_coordinator()

        def _boom(new, active):
            raise RuntimeError("unknown system event")

        c._check_conflicts = _boom
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_terminal(result)
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.INTERNAL_ERROR.value)
        self.assertFalse(result["success"])
        self.assertIn("unknown system event", result["error"])

    def test_commit_original_commits_original_as_revision0(self):
        c, _ = _make_coordinator()
        c._execute_trial = lambda feas: {"success": True, "snapshot": {},
                                         "clipped": [],
                                         "applied": [{"axis": "power"}]}
        c._validate_trial = lambda ni, ai: {
            "all_satisfied": True, "metrics": {},
            "monitor_verdicts": {getattr(i, "id", None): "satisfied"
                                 for i in list(ai) + [ni]}}
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_terminal(result)
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_ORIGINAL.value)
        self.assertEqual(result["resolution"], "accept")
        self.assertTrue(result["success"])
        # original structured intent preserved as pending AND committed as the
        # executed intent (revision 0) - CommitOriginal is NOT None (item B)
        self.assertEqual(result["pending_intent"]["target"]["target_value"],
                         8.0)
        self.assertEqual(
            result["committed_revision"]["target"]["target_value"], 8.0)

    def test_commit_revised_stores_executed_revision(self):
        c, _ = _make_coordinator()
        relaxed = _make_intent(6.0)
        alt = Alternative(id="a6", description="relax to 6",
                          modified_intent=relaxed)
        c._analyze_feasibility = lambda i, a, s: FeasibilityPrediction(
            feasible=True, confidence=0.9, reasoning="", alternatives=[alt])
        c._execute_trial = lambda feas: {"success": True, "snapshot": {},
                                         "clipped": [],
                                         "applied": [{"axis": "power"}]}
        validations = []

        def _validate(ni, ai):
            validations.append(ni.target.target_value)
            ok = len(validations) > 1
            return {"all_satisfied": ok, "metrics": {},
                    "monitor_verdicts": (
                        {getattr(i, "id", None): "satisfied"
                         for i in list(ai) + [ni]} if ok else {})}

        c._validate_trial = _validate
        c._rollback = lambda snapshot: None
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_terminal(result)
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.COMMIT_REVISED.value)
        self.assertEqual(result["resolution"], "accept_modified")
        # pending intent is the ORIGINAL (8.0); committed revision is the
        # EXACT executed relaxed intent (6.0)
        self.assertEqual(result["pending_intent"]["target"]["target_value"],
                         8.0)
        self.assertEqual(
            result["committed_revision"]["target"]["target_value"], 6.0)

    def test_revision_agreement_without_execution_is_not_commit(self):
        c, _ = _make_coordinator()
        c._execute_trial = lambda feas: {"success": False, "snapshot": {},
                                         "clipped": [], "applied": []}
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_terminal(result)
        self.assertTrue(result["agreement"]["accepted"])
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.UNMATERIALIZABLE_AGREEMENT.value)
        self.assertFalse(result["success"])
        self.assertIsNone(result["committed_revision"])

    def test_already_satisfied_is_pending_not_admitted(self):
        # Item 3: an already-satisfied shortcut is NOT a commit until fresh
        # joint satisfaction + commit/no-op evidence exist. Batch A fails
        # closed: nothing admitted, no success claimed.
        c, _ = _make_coordinator()
        c._check_conflicts = lambda new, active: False
        c._evaluate_intent_verdict = (
            lambda intent, metrics: MonitorVerdict.SATISFIED)
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_terminal(result)
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.ALREADY_SATISFIED_UNVERIFIED.value)
        self.assertFalse(result["success"])
        self.assertEqual(result["resolution"], "reject")
        # the intent was NOT admitted into the monitored set
        self.assertEqual(c.intent_manager.added, [])

    def test_reentrant_call_is_pending_not_admitted(self):
        c, _ = _make_coordinator()
        c._episode_in_flight = True
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_terminal(result)
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.PENDING_NOT_ADMITTED.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.SINGLE_FLIGHT_REJECTED.value)
        self.assertFalse(result["success"])
        self.assertEqual(result["pending_intent"], "throughput >= 8 Mbps")


# --------------------------------------------------------------------------
# Exhaustive terminal behavior on cleanup failure (item 5)
# --------------------------------------------------------------------------

class CleanupExceptionTest(unittest.TestCase):

    def test_s0_callback_exception_downgrades_to_failsafe(self):
        # a committed episode whose FINAL S0 state callback throws must still
        # return a terminal outcome - downgraded to TechnicalFailsafe.
        states = []

        def _cb(old, new):
            if old == "S6" and new == "S0":
                raise RuntimeError("final S0 callback boom")
            states.append(new)

        c, _ = _make_coordinator(states)
        c.on_state_change = _cb
        c._execute_trial = lambda feas: {"success": True, "snapshot": {},
                                         "clipped": [],
                                         "applied": [{"axis": "power"}]}
        c._validate_trial = lambda ni, ai: {
            "all_satisfied": True, "metrics": {},
            "monitor_verdicts": {getattr(i, "id", None): "satisfied"
                                 for i in list(ai) + [ni]}}
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.INTERNAL_ERROR.value)
        self.assertFalse(result["success"])
        self.assertIn("cleanup failed", result["error"])

    def test_history_finalization_exception_downgrades_to_failsafe(self):
        c, _ = _make_coordinator()
        c._parse_intent = lambda text: None   # clean PENDING episode

        def _boom(result):
            raise RuntimeError("history append boom")

        # Batch E two-phase history: the PRE-ADMISSION history-finalization seam
        # is _prepare_history (validates the finalizable record before admission,
        # so its failure still downgrades before intent_manager.add).
        c._prepare_history = _boom
        result = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertFalse(result["success"])
        self.assertIn("cleanup failed", result["error"])


if __name__ == "__main__":
    unittest.main()
