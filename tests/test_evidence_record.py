"""Batch A: immutable evidence record + identifier honesty (P0-6 / P1-6).

Covers the EvidenceRecord dataclass (deep immutability incl. dict fields,
JSON-safe serialization, complete-core identifier chain), that the coordinator
mints & reuses stage identifiers honestly (cycle/proposal/trial ids minted
when their stage begins, actuation_trial_id only for a real S3 write), and that
evidence is never silently omitted (construction failure -> minimal bundle).

Scope note: this is the DEFINITIONAL P0-6 evidence record only. Commit-binding
enforcement (readback/hash/freshness commit checks) is Batch C.
"""

import hashlib
import unittest

from coordinator.episode_types import (
    EVIDENCE_IDENTIFIER_FIELDS, EvidenceRecord, MonitorVerdict, Observation,
    TerminalOutcome, TerminalReason, new_id,
)
from coordinator.intent_coordinator import IntentCoordinator
from decision.intent_model import (
    Alternative, ConstraintType, FeasibilityPrediction, Intent, IntentTarget,
    IntentType, NetworkState,
)


# --------------------------------------------------------------------------
# Skeletons
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
    def __init__(self):
        self.theta = 0.5
        self.n_max = 2
        self.recorded = []

    def get_theta_star(self, phase=None):
        return self.theta

    def can_negotiate_more(self, rounds, phase=None):
        return rounds < self.n_max

    def record_episode(self, metrics):
        self.recorded.append(metrics)


class _FakeCollector:
    def collect_all(self):
        return {}


def _base_coordinator():
    c = IntentCoordinator.__new__(IntentCoordinator)
    c.gui = None
    c.on_negotiation_needed = None
    c.on_state_change = None
    c.current_state = "S0"
    c.history_reservoir = []
    c.max_history = 100
    c.tau_trial_s = 0.0
    c.intent_manager = _FakeIntentManager()
    c.calibrator = _FakeCalibrator()
    c.negotiation_policy = None
    c.generate_alternatives_fn = lambda i, r: []
    c.ue_collector = _FakeCollector()
    return c


def _pending_skeleton():
    """A coordinator that ends every episode PENDING (parse returns None), so
    no cycle/proposal/trial stage runs."""
    c = _base_coordinator()
    c._parse_intent = lambda text: None
    return c


def _intent(value=8.0):
    return Intent(type=IntentType.THROUGHPUT_GOAL,
                  target=IntentTarget(kpi_name="throughput",
                                      constraint_type=ConstraintType.MIN,
                                      target_value=value, unit="Mbps"))


def _trial_skeleton(applied, validate_ok):
    """A coordinator whose single cycle routes to S3 and returns the given
    `applied` list; `validate_ok` controls whether S4 passes."""
    c = _base_coordinator()
    intent = _intent()
    alt = Alternative(id="a1", description="bare")
    c._parse_intent = lambda text: {"intent": intent, "raw": {}}
    c._check_conflicts = lambda new, active: True
    c._get_network_state = lambda: NetworkState(ue_states={})
    def _af(i, a, s):
        # honest double: a generated proposal stamps its REAL prompt hash +
        # proposal-generated state, so the pre-write S3 invariant (P1-6) sees a
        # bound prompt hash.
        c._cur_proposal_generated = True
        c._cur_schema_valid = True   # generated-proposal schema verdict (real bool)
        c._cur_prompt_hash = hashlib.sha256(b"evidence-record").hexdigest()
        return FeasibilityPrediction(
            feasible=True, confidence=0.9, reasoning="", alternatives=[alt])
    c._analyze_feasibility = _af
    def _exec(feas):
        # a real S3 write mints the actuation trial id at the pre-write point
        # (Batch C moved minting into _execute_trial); model that faithfully.
        if applied:
            c._bind_trial_id_prewrite(c._get_txn())
        return {"success": bool(applied), "snapshot": {},
                "clipped": [], "applied": list(applied)}
    c._execute_trial = _exec
    c._validate_trial = lambda ni, ai: {"all_satisfied": validate_ok,
                                        "metrics": {}}
    c._rollback = lambda snapshot: None
    return c


def _evidence(**overrides):
    base = dict(
        experiment_run_id=new_id("run"),
        episode_id=new_id("ep"),
        fsm_step_id=new_id("fsm"),
        evidence_record_id=new_id("evid"),
        proposer_id="mock-llm",
        model_version="mock-llm",
        intent_set_version="iset-abc",
        pending_intent_hash="pi-123",
        cycle_id=new_id("cycle"),
        proposal_id=new_id("prop"),
    )
    base.update(overrides)
    return EvidenceRecord(**base)


# --------------------------------------------------------------------------
# Observation dataclass
# --------------------------------------------------------------------------

class ObservationTest(unittest.TestCase):

    def test_observation_serializes(self):
        obs = Observation(source="iperf", value=9.0, sample_time=101.0,
                          collection_start=100.5, collection_end=101.0,
                          freshness_verdict="fresh")
        d = obs.to_dict()
        self.assertEqual(d["source"], "iperf")
        self.assertEqual(d["sample_time"], 101.0)
        self.assertEqual(d["freshness_verdict"], "fresh")


# --------------------------------------------------------------------------
# Evidence record immutability + identifier chain
# --------------------------------------------------------------------------

class EvidenceRecordTest(unittest.TestCase):

    def test_identifier_chain_complete_over_core(self):
        ev = _evidence()
        self.assertTrue(ev.has_complete_identifier_chain())
        for f in EVIDENCE_IDENTIFIER_FIELDS:
            self.assertIn(f, ev.identifier_chain())

    def test_missing_core_link_is_incomplete(self):
        self.assertFalse(_evidence(proposer_id="").has_complete_identifier_chain())

    def test_stage_ids_optional_and_honest(self):
        # a pre-cycle rejection has no cycle/proposal/trial ids but is still
        # chain-complete over the required core (P1-6 honesty)
        ev = _evidence(cycle_id=None, proposal_id=None, actuation_trial_id=None)
        self.assertTrue(ev.has_complete_identifier_chain())

    def test_record_attribute_is_immutable(self):
        ev = _evidence()
        with self.assertRaises(Exception):
            ev.proposal_id = "mutated"

    def test_action_maps_are_immutable(self):
        # frozen=True is shallow; the top-level dict fields must be frozen too.
        ev = _evidence(canonical_action={"bs1_power_offset": 2.0})
        with self.assertRaises(TypeError):
            ev.canonical_action["bs1_power_offset"] = 9.0
        with self.assertRaises(TypeError):
            ev.snapshot["x"] = 1
        with self.assertRaises(TypeError):
            ev.monitor_verdicts["i1"] = MonitorVerdict.VIOLATED
        # but to_dict is still a plain, mutable, JSON-safe dict
        d = ev.to_dict()
        self.assertEqual(d["canonical_action"]["bs1_power_offset"], 2.0)
        d["canonical_action"]["bs1_power_offset"] = 9.0  # no raise

    def test_nested_containers_are_deeply_immutable(self):
        # item A reproduced counterexamples: nested mutation must RAISE.
        ev = _evidence(canonical_action={"outer": {"x": 1}},
                       observations=({"nested": [1]},),
                       snapshot={"g": {"axes": [1, 2]}})
        with self.assertRaises((TypeError, AttributeError)):
            ev.canonical_action["outer"]["x"] = 2
        with self.assertRaises((TypeError, AttributeError)):
            ev.observations[0]["nested"].append(2)
        with self.assertRaises((TypeError, AttributeError)):
            ev.snapshot["g"]["axes"].append(3)

    def test_construction_copies_caller_input(self):
        # mutating the caller's ORIGINAL input after construction must not
        # affect the record (no aliasing) - item A.
        original = {"outer": {"x": 1}}
        ev = _evidence(canonical_action=original)
        original["outer"]["x"] = 99
        self.assertEqual(ev.canonical_action["outer"]["x"], 1)
        self.assertEqual(ev.to_dict()["canonical_action"]["outer"]["x"], 1)

    def test_to_dict_recursively_thaws_to_mutable_json(self):
        import json
        ev = _evidence(canonical_action={"outer": {"x": 1}},
                       snapshot={"g": {"a": [1, 2]}},
                       observations=(Observation(
                           source="iperf", value=9.0, sample_time=1.0,
                           collection_start=0.0, collection_end=1.0),))
        d = ev.to_dict()
        d["canonical_action"]["outer"]["x"] = 5      # nested mutable
        d["snapshot"]["g"]["a"].append(3)            # nested list mutable
        self.assertEqual(d["observations"][0]["source"], "iperf")
        json.dumps(d)                                # JSON-safe

    def test_to_dict_serializes_observations_and_verdicts(self):
        ev = _evidence(
            observations=(Observation(source="iperf", value=9.0,
                                      sample_time=101.0, collection_start=100.0,
                                      collection_end=101.0,
                                      freshness_verdict="fresh"),),
            monitor_verdicts={"i1": MonitorVerdict.SATISFIED})
        d = ev.to_dict()
        self.assertEqual(d["observations"][0]["source"], "iperf")
        self.assertEqual(d["monitor_verdicts"]["i1"], "satisfied")


# --------------------------------------------------------------------------
# Coordinator identifier honesty (P0-6 / P1-6, item 7)
# --------------------------------------------------------------------------

class IdentifierHonestyTest(unittest.TestCase):

    def test_pending_episode_has_no_stage_ids(self):
        c = _pending_skeleton()
        result = c.process_intent("throughput >= 8 Mbps")
        ev = result["evidence"]
        # core chain present ...
        for f in ("experiment_run_id", "episode_id", "fsm_step_id",
                  "proposer_id", "model_version", "intent_set_version",
                  "pending_intent_hash"):
            self.assertTrue(ev[f], f"empty core link: {f}")
        self.assertEqual(ev["experiment_run_id"],
                         result["experiment_run_id"])
        self.assertEqual(ev["episode_id"], result["episode_id"])
        self.assertEqual(ev["fsm_step_id"], result["fsm_step_id"])
        # ... but no cycle/proposal/trial stage ran -> honest None
        self.assertIsNone(ev["cycle_id"])
        self.assertIsNone(ev["proposal_id"])
        self.assertIsNone(ev["actuation_trial_id"])

    def test_cycle_and_proposal_ids_persisted_and_reused(self):
        c = _trial_skeleton(applied=[{"axis": "power"}], validate_ok=True)
        result = c.process_intent("throughput >= 8 Mbps")
        ev = result["evidence"]
        cyc = result["cycles"][-1]
        # ids minted at stage start are REUSED in evidence, not re-invented
        self.assertTrue(ev["cycle_id"])
        self.assertEqual(ev["cycle_id"], cyc["cycle_id"])
        self.assertEqual(ev["proposal_id"], cyc["proposal_id"])

    def test_actuation_trial_id_only_for_real_write(self):
        # a real S3 write (non-empty applied) -> trial id minted & reused
        c = _trial_skeleton(applied=[{"axis": "power"}], validate_ok=True)
        result = c.process_intent("throughput >= 8 Mbps")
        cyc = result["cycles"][-1]
        self.assertTrue(cyc["actuation_trial_id"])
        self.assertEqual(result["evidence"]["actuation_trial_id"],
                         cyc["actuation_trial_id"])

    def test_no_write_route_has_no_actuation_trial_id(self):
        # routed to S3 but no applicable action (applied=[]) is NOT a write
        c = _trial_skeleton(applied=[], validate_ok=False)
        result = c.process_intent("throughput >= 8 Mbps")
        cyc = result["cycles"][-1]
        self.assertEqual(cyc.get("routed_to"), "trial")   # it DID route to S3
        self.assertNotIn("actuation_trial_id", cyc)        # but no write
        self.assertIsNone(result["evidence"]["actuation_trial_id"])

    def test_repeated_episodes_have_distinct_ids(self):
        c = _pending_skeleton()
        r1 = c.process_intent("throughput >= 8 Mbps")
        r2 = c.process_intent("throughput >= 8 Mbps")
        self.assertEqual(r1["experiment_run_id"], r2["experiment_run_id"])
        self.assertNotEqual(r1["episode_id"], r2["episode_id"])


# --------------------------------------------------------------------------
# Evidence is never silently omitted (item 6)
# --------------------------------------------------------------------------

class EvidenceTotalityTest(unittest.TestCase):
    """Item C: evidence construction failure is FAIL-CLOSED - the episode
    atomically downgrades to TechnicalFailsafe/InternalError with a minimal,
    auditable, self-consistent evidence bundle. It is never presented as a
    commit/admission with an evidence error."""

    ROOT = "ROOT evidence boom"

    def _break_evidence(self, c):
        def _boom():
            raise RuntimeError(self.ROOT)
        c._proposer_context = _boom

    def _assert_downgraded(self, result):
        self.assertEqual(result["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(result["terminal_reason"],
                         TerminalReason.INTERNAL_ERROR.value)
        self.assertFalse(result["success"])
        self.assertIsNone(result["committed_revision"])
        ev = result["evidence"]
        self.assertIsNotNone(ev)
        self.assertIn("evidence_construction_failed", ev["error"])
        # the minimal bundle's OWN terminal fields match the downgrade
        self.assertEqual(ev["terminal_outcome"],
                         TerminalOutcome.TECHNICAL_FAILSAFE.value)
        self.assertEqual(ev["terminal_reason"],
                         TerminalReason.INTERNAL_ERROR.value)
        # ids still carried so the record stays auditable
        self.assertEqual(ev["episode_id"], result["episode_id"])
        # the ROOT exception text is preserved in BOTH the result error and
        # the audit evidence error (not swallowed by a generic message)
        self.assertIn(self.ROOT, result["error"])
        self.assertIn(self.ROOT, ev["error"])

    def test_pending_episode_downgrades_on_evidence_failure(self):
        c = _pending_skeleton()
        self._break_evidence(c)
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_downgraded(result)

    def test_commit_path_downgrades_on_evidence_failure(self):
        # a would-be CommitOriginal whose required evidence cannot be built
        # must NOT surface as commit_original/success=True (fail-open).
        c = _trial_skeleton(applied=[{"axis": "power"}], validate_ok=True)
        self._break_evidence(c)
        result = c.process_intent("throughput >= 8 Mbps")
        self._assert_downgraded(result)


class ObservationDetailsImmutabilityTest(unittest.TestCase):
    """Review bug 1: Observation.details must be DEEP-frozen (a frozen dataclass
    only blocks attribute rebinding), and to_dict must return an independent
    mutable copy that cannot feed back."""

    def _obs(self):
        return Observation(source="joint_shortcut", value=9.0, sample_time=1.0,
                           collection_start=0.0, collection_end=1.0,
                           details={"ue1": {"samples": [9], "attached": True}})

    def test_source_mutation_does_not_affect_observation(self):
        src = {"ue1": {"samples": [9], "attached": True}}
        obs = Observation(source="joint_shortcut", value=9.0, sample_time=1.0,
                          collection_start=0.0, collection_end=1.0, details=src)
        src["ue1"]["samples"].append(999)          # mutate the ORIGINAL
        src["ue1"]["attached"] = False
        self.assertEqual(obs.details["ue1"]["samples"], (9,))
        self.assertTrue(obs.details["ue1"]["attached"])

    def test_source_mutation_does_not_affect_evidence(self):
        src = {"ue1": {"samples": [9]}}
        obs = Observation(source="joint_shortcut", value=9.0, sample_time=1.0,
                          collection_start=0.0, collection_end=1.0, details=src)
        ev = EvidenceRecord(
            experiment_run_id="r", episode_id="e", fsm_step_id="f",
            evidence_record_id="evid-1",
            proposer_id="p", model_version="m", intent_set_version="iset",
            pending_intent_hash="pi", observations=(obs,))
        src["ue1"]["samples"].append(999)
        self.assertEqual(
            ev.to_dict()["observations"][0]["details"]["ue1"]["samples"], [9])

    def test_nested_write_raises(self):
        obs = self._obs()
        with self.assertRaises((TypeError, AttributeError)):
            obs.details["ue1"]["samples"] = [0]
        with self.assertRaises((TypeError, AttributeError)):
            obs.details["ue1"]["samples"].append(1)   # tuple after freeze

    def test_to_dict_returns_independent_mutable_copy(self):
        obs = self._obs()
        d1 = obs.to_dict()
        # mutable (plain dict/list), and mutation cannot feed back
        d1["details"]["ue1"]["samples"].append(777)
        d1["details"]["ue1"]["new"] = "x"
        d2 = obs.to_dict()
        self.assertEqual(d2["details"]["ue1"]["samples"], [9])
        self.assertNotIn("new", d2["details"]["ue1"])
        # and the frozen observation itself is unchanged
        self.assertEqual(obs.details["ue1"]["samples"], (9,))

    def test_none_details_unchanged(self):
        obs = Observation(source="trial_window", value=5.0, sample_time=1.0,
                          collection_start=0.0, collection_end=1.0)
        self.assertIsNone(obs.details)
        self.assertIsNone(obs.to_dict()["details"])


if __name__ == "__main__":
    unittest.main()
