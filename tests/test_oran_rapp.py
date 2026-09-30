import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from decision.intent_model import (
    ConstraintType, Intent, IntentPriority, IntentScope, IntentTarget, IntentType,
)
from decision.llm_backend import DeterministicMockBackend, LLMBackendManager
from oran.rapp.assurance import CombinedAssurance
from oran.rapp.contract_support import _bundle_dir
from oran.rapp.coordinator_adapter import R1PolicyExecutor, RAppCoordinatorAdapter
from coordinator.episode_types import MonitorVerdict
from oran.rapp.evidence import EvidenceLedger
from oran.rapp.headless import run_once
from oran.rapp.policy_translator import (
    AdmissionRejected, POLICY_TYPE_ID, PolicyTranslationContext,
    local_policy_schema_digest, translate_intent,
)
from oran.rapp.ports import AssuranceDecision


class ContractFixture(unittest.TestCase):
    NOW = datetime.fromisoformat("2026-08-04T00:02:08+00:00")

    @classmethod
    def setUpClass(cls):
        cls.bundle = _bundle_dir()
        cls.golden = json.loads((cls.bundle / "golden/golden-vectors.1.0.0.json")
                                .read_text(encoding="utf-8"))["canonicalObjects"]

    def intent(self, intent_type=IntentType.THROUGHPUT_GOAL):
        policy = self.golden["policy"]
        return Intent(
            id=policy["trace"]["intentId"], type=intent_type,
            target=IntentTarget("throughput", ConstraintType.MIN, 1.0, unit="Mbps"),
            scope=IntentScope(ue_ids=["ue-fixture"]),
            priority=IntentPriority.HIGH)

    def context(self, objective="BALANCE_PRB_LOAD"):
        policy = self.golden["policy"]
        cells = policy["steeringObjective"]["actionEnvelope"]["allowedCells"]
        return PolicyTranslationContext(
            ue_id=policy["scope"]["ueId"],
            allowed_cells=(cells if objective == "BALANCE_PRB_LOAD" else cells[:1]),
            forbidden_cells=[], objective_kind=objective,
            improvement_threshold_prb=(10 if objective == "BALANCE_PRB_LOAD"
                                       else None),
            min_seconds_between_actuations=30,
            required_kpi_freshness_ms=3000, action_deadline_ms=10000,
            not_before=policy["validity"]["notBefore"],
            expires_at=policy["validity"]["expiresAt"],
            rollback_on=policy["rollbackPolicy"]["on"],
            rollback_timeout_ms=10000, intent_revision=1, policy_revision=1,
            correlation_id=policy["trace"]["correlationId"],
            producer_id=policy["trace"]["producerId"])

    def translate(self, intent_type=IntentType.THROUGHPUT_GOAL,
                  objective="BALANCE_PRB_LOAD", capability=None, discovery=None):
        if discovery is None:
            discovery = {
                "policyTypeIds": [POLICY_TYPE_ID],
                "policyTypeObject": {
                    "policyTypeId": POLICY_TYPE_ID,
                    "policySchema": json.loads((self.bundle /
                        "AIC_UECellSteering_1.0.0.policy.schema.json").read_text()),
                    "statusSchema": json.loads((self.bundle /
                        "AIC_UECellSteering_1.0.0.status.schema.json").read_text()),
                },
            }
        return translate_intent(
            self.intent(intent_type),
            policy_type_discovery=discovery,
            capability_manifest=(capability if capability is not None
                                 else self.golden["capabilityManifest"]),
            context=self.context(objective))


class PolicyTranslatorTests(ContractFixture):
    def test_golden_policy_is_reproduced_exactly(self):
        self.assertEqual(self.translate(), self.golden["policy"])
        self.assertEqual(local_policy_schema_digest(),
                         self.golden["capabilityManifest"]["schemaDigests"]["policy"])

    def test_four_supported_admission_paths(self):
        for intent_type in (IntentType.THROUGHPUT_GOAL,
                            IntentType.THROUGHPUT_FAIRNESS,
                            IntentType.LATENCY_GOAL):
            with self.subTest(intent_type=intent_type):
                self.assertEqual(self.translate(intent_type)["steeringObjective"]["kind"],
                                 "BALANCE_PRB_LOAD")
        pin = self.translate(IntentType.THROUGHPUT_GOAL, "PIN_TO_CELL")
        self.assertEqual(pin["steeringObjective"]["kind"], "PIN_TO_CELL")
        self.assertEqual(len(pin["steeringObjective"]["actionEnvelope"]
                             ["allowedCells"]), 1)
        self.assertNotIn("improvementThresholdPrb", pin["steeringObjective"])

    def test_unsupported_intents_fail_closed(self):
        unsupported = (IntentType.POWER_CONSTRAINT, IntentType.RSRP_CONSTRAINT,
                       IntentType.ENERGY_EFFICIENCY, IntentType.COVERAGE_GOAL)
        for intent_type in unsupported:
            with self.subTest(intent_type=intent_type):
                with self.assertRaises(AdmissionRejected):
                    self.translate(intent_type)

    def test_discovery_and_manifest_mismatch_fail_closed(self):
        with self.assertRaises(AdmissionRejected):
            self.translate(discovery=[])
        capability = copy.deepcopy(self.golden["capabilityManifest"])
        capability["objectives"] = ["PIN_TO_CELL"]
        with self.assertRaises(AdmissionRejected):
            self.translate(capability=capability)


class AssuranceTests(ContractFixture):
    def assess(self, records, status=None, now=None):
        return CombinedAssurance(self.golden["capabilityManifest"]).assess(
            self.golden["policy"],
            self.golden["appliedVerifiedStatus"]["aicStatus"]["policyId"],
            status or self.golden["appliedVerifiedStatus"], records,
            now=now or self.NOW)

    def test_golden_after_evidence_is_commit_eligible(self):
        self.assertEqual(self.assess([self.golden["afterEvidence"]]),
                         AssuranceDecision.SATISFIED)
        self.assertEqual(self.assess([self.golden["afterEvidence"],
                                      self.golden["afterEvidenceCell2"]]),
                         AssuranceDecision.SATISFIED)

    def test_suspect_null_overlap_and_uncorrelated_are_unknown(self):
        original = self.golden["afterEvidence"]
        suspect = copy.deepcopy(original)
        suspect["quality"] = "SUSPECT"
        suspect["samples"][0]["quality"] = "SUSPECT"
        suspect["samples"][0]["suspect"] = True

        null = copy.deepcopy(original)
        null["quality"] = "NOT_AVAILABLE"
        null["samples"][0]["quality"] = "NOT_AVAILABLE"
        null["samples"][0]["value"] = None

        overlap = copy.deepcopy(original)
        overlap["quality"] = "AMBIGUOUS"
        overlap["phase"] = "OVERLAPS_ACTION"
        overlap["ambiguityReason"] = "ACTION_WINDOW_OVERLAP"

        uncorrelated = copy.deepcopy(original)
        uncorrelated["correlation"]["policyId"] = (
            "a57b75ca-63e0-44d8-acf6-335431f0a317")

        wrong_dn = copy.deepcopy(original)
        wrong_dn["measurementScope"]["managedObjectDn"] = (
            self.golden["afterEvidenceCell2"]["measurementScope"]
            ["managedObjectDn"])

        for record in (suspect, null, overlap, uncorrelated, wrong_dn):
            with self.subTest(quality=record["quality"]):
                self.assertEqual(self.assess([record]), AssuranceDecision.UNKNOWN)

    def test_stale_and_enforced_without_verified_action_are_unknown(self):
        late = datetime.fromisoformat("2026-08-04T00:04:01+00:00")
        self.assertEqual(self.assess([self.golden["afterEvidence"]], now=late),
                         AssuranceDecision.UNKNOWN)
        status = copy.deepcopy(self.golden["appliedVerifiedStatus"])
        status["aicStatus"]["episodeState"] = "APPLIED_UNVERIFIED"
        status["aicStatus"]["episodeTerminal"] = False
        status["aicStatus"]["readback"]["result"] = "UNKNOWN"
        self.assertEqual(self.assess([self.golden["afterEvidence"]], status=status),
                         AssuranceDecision.UNKNOWN)


class _Dispatch:
    def __init__(self, fixture):
        self.fixture = fixture
        self.create_policy_calls = 0

    def bootstrap_info(self):
        return {"bootstrap": "ready"}

    def discover_services(self, *, api_name=None):
        return [{"apiName": api_name}]

    def discover_policy_types(self):
        return [POLICY_TYPE_ID]

    def get_policy_type(self, policy_type_id):
        bundle = _bundle_dir()
        return {"policyTypeId": policy_type_id,
                "policySchema": json.loads((bundle /
                    "AIC_UECellSteering_1.0.0.policy.schema.json").read_text()),
                "statusSchema": json.loads((bundle /
                    "AIC_UECellSteering_1.0.0.status.schema.json").read_text())}

    def create_policy(self, near_rt_ric_id, policy_type_id, policy_object):
        self.create_policy_calls += 1
        return {"policyId": self.fixture["appliedVerifiedStatus"]["aicStatus"]
                ["policyId"], "location": "stub://policy/42"}

    def get_policy_status(self, policy_id):
        return self.fixture["appliedVerifiedStatus"]

    def create_continuous_job(self, *, policy_id, policy_revision,
                              near_rt_ric_id):
        return {"dataJobId": "job-fixture",
                "deliveryBindingId": "0" * 32}


class CoordinatorAdapterTests(ContractFixture):
    @staticmethod
    def manager(seed=7):
        return LLMBackendManager.with_backend(DeterministicMockBackend(seed=seed))

    class ScriptedBackend(DeterministicMockBackend):
        def __init__(self, confidences):
            super().__init__(seed=19)
            self.confidences = list(confidences)
            self.generate_calls = 0

        def generate(self, prompt, system_prompt=""):
            self.generate_calls += 1
            return super().generate(prompt, system_prompt)

        def _feasibility(self, prompt):
            data = super()._feasibility(prompt)
            if self.confidences:
                data["confidence"] = self.confidences.pop(0)
            return data

    def adapter(self, tmp, backend, dispatch=None):
        dispatch = dispatch or _Dispatch(self.golden)
        return RAppCoordinatorAdapter(
            dispatch=dispatch,
            assurance=CombinedAssurance(self.golden["capabilityManifest"]),
            ledger=EvidenceLedger(Path(tmp) / "evidence.jsonl"),
            capability_manifest=self.golden["capabilityManifest"],
            llm_manager=LLMBackendManager.with_backend(backend))

    @staticmethod
    def set_cold_routing(adapter, *, theta, n_max):
        calibrator = adapter.coordinator.calibrator
        calibrator.theta_star = theta
        calibrator.theta_star_raw = theta
        calibrator.n_max = n_max
        calibrator._cold_theta = theta
        calibrator._cold_n_max = n_max

    def test_s0_s6_and_four_plane_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = EvidenceLedger(Path(tmp) / "evidence.jsonl")
            adapter = RAppCoordinatorAdapter(
                dispatch=_Dispatch(self.golden),
                assurance=CombinedAssurance(self.golden["capabilityManifest"]),
                ledger=ledger,
                capability_manifest=self.golden["capabilityManifest"],
                llm_manager=self.manager())
            result = adapter.process_admitted_intent(
                self.intent(), context=self.context(),
                evidence_records=[self.golden["afterEvidence"]], now=self.NOW,
                identifiers={"run_id": "run-1", "episode_id": "episode-1",
                             "cycle_id": "cycle-1", "proposal_id": "proposal-1",
                             "trial_id": "trial-1", "r1_request_id": "request-1"})
            self.assertTrue(result["success"])
            self.assertEqual(result["terminal_outcome"], "commit_original")
            self.assertEqual([r["stage"] for r in ledger.read_all()], [
                "COORDINATOR_JUDGEMENT", "A1_POLICY_LIFECYCLE",
                "NEAR_RT_ACTION_EVIDENCE", "INTENT_SATISFACTION"])
            transitions = adapter.transition_snapshot()
            self.assertTrue(transitions)
            self.assertEqual({"REAL"}, {item["origin"] for item in transitions})
            terminal = [t for t in transitions if t["to"] == "S6"][-1]
            self.assertEqual(terminal["outcome"], "commit_original")
            self.assertEqual(transitions[-1]["to"], "S0")
            observations = adapter.harness_observations()
            self.assertEqual(observations["processIntentCalls"], 1)
            self.assertEqual(observations["coordinatorExecutionMode"],
                             "REAL_PROCESS_INTENT")
            self.assertEqual(observations["coordinatorTerminalOutcome"],
                             "commit_original")
            self.assertTrue(observations["coordinatorTerminalEvidenceRef"])
            self.assertEqual(len(observations["coordinatorLedgerReferences"]), 4)

    def test_synthetic_harness_transition_is_origin_labeled(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self.adapter(tmp, DeterministicMockBackend(seed=7))

            adapter.harness_transition("S0", "S1")

            self.assertEqual("SYNTHETIC",
                             adapter.transition_snapshot()[0]["origin"])

    def test_headless_typed_entry_reaches_terminal_outcome(self):
        policy = self.golden["policy"]
        with tempfile.TemporaryDirectory() as tmp, \
                patch("oran.rapp.headless.load_integration_values") as values, \
                patch("oran.rapp.headless._load_pinned_json",
                      return_value=self.golden["capabilityManifest"]), \
                patch("oran.rapp.headless.R1Client",
                      return_value=_Dispatch(self.golden)):
            values.return_value = {
                "backend.capabilityManifestPath": "capability.json",
                "backend.capabilityManifestSha256": "0" * 64,
                "r1.apiRoot": "http://127.0.0.1:1",
                "r1.rAppId": "rapp-fixture",
                "r1.dme.policyEvidencePushBaseUri": "http://127.0.0.1:2/dme",
            }
            request = {
                "statePath": str(Path(tmp) / "state.json"),
                "evidencePath": str(Path(tmp) / "evidence.jsonl"),
                "intent": {
                    "id": policy["trace"]["intentId"],
                    "type": "throughput_goal",
                    "target": {"kpiName": "throughput",
                               "constraintType": "min", "targetValue": 1.0,
                               "unit": "Mbps"},
                    "scope": {"ueIds": ["ue-fixture"], "bsIds": [],
                              "cellIds": []},
                    "priority": "HIGH",
                },
                "policyContext": {
                    "ueId": policy["scope"]["ueId"],
                    "allowedCells": policy["steeringObjective"]["actionEnvelope"]
                    ["allowedCells"], "forbiddenCells": [],
                    "objectiveKind": "BALANCE_PRB_LOAD",
                    "improvementThresholdPrb": 10,
                    "minSecondsBetweenActuations": 30,
                    "requiredKpiFreshnessMs": 3000,
                    "actionDeadlineMs": 10000,
                    "notBefore": policy["validity"]["notBefore"],
                    "expiresAt": policy["validity"]["expiresAt"],
                    "rollbackOn": policy["rollbackPolicy"]["on"],
                    "rollbackTimeoutMs": 10000, "intentRevision": 1,
                    "policyRevision": 1,
                    "correlationId": policy["trace"]["correlationId"],
                    "producerId": policy["trace"]["producerId"],
                },
                "evidenceRecords": [self.golden["afterEvidence"]],
                "identifiers": {"run_id": "run-1", "episode_id": "episode-1",
                                "cycle_id": "cycle-1",
                                "proposal_id": "proposal-1", "trial_id": "trial-1",
                                "r1_request_id": "request-1"},
            }
            result = run_once(integration_path=str(Path(tmp) / "values.json"),
                              request=request, insecure_dev=True, now=self.NOW,
                              llm_manager=self.manager())
            self.assertTrue(result["success"])
            self.assertEqual(result["terminal_outcome"], "commit_original")

    def test_real_backend_call_and_theta_boundary_route_trial_vs_negotiation(self):
        identifiers = {"run_id": "run-theta", "episode_id": "episode-theta",
                       "cycle_id": "cycle-theta", "proposal_id": "proposal-theta",
                       "trial_id": "trial-theta", "r1_request_id": "request-theta"}
        with tempfile.TemporaryDirectory() as tmp:
            high = self.ScriptedBackend([0.51])
            dispatch = _Dispatch(self.golden)
            trial_adapter = self.adapter(tmp, high, dispatch)
            self.set_cold_routing(trial_adapter, theta=0.50, n_max=1)
            trial = trial_adapter.process_intent(
                self.intent(), context=self.context(),
                evidence_records=[self.golden["afterEvidence"]], now=self.NOW,
                identifiers=identifiers)
            self.assertEqual(trial["cycles"][0]["routed_to"], "trial")
            self.assertEqual(dispatch.create_policy_calls, 1)
            self.assertGreaterEqual(high.generate_calls, 1)

        with tempfile.TemporaryDirectory() as tmp:
            low = self.ScriptedBackend([0.49])
            dispatch = _Dispatch(self.golden)
            negotiation_adapter = self.adapter(tmp, low, dispatch)
            self.set_cold_routing(negotiation_adapter, theta=0.50, n_max=0)
            negotiation = negotiation_adapter.process_intent(
                self.intent(), context=self.context(),
                evidence_records=[self.golden["afterEvidence"]], now=self.NOW,
                identifiers=identifiers)
            self.assertEqual(negotiation["cycles"][0]["routed_to"],
                             "negotiation")
            self.assertEqual(dispatch.create_policy_calls, 0)
            self.assertGreaterEqual(low.generate_calls, 1)

    def test_s5_acceptance_reenters_s2_and_executes_revised_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.ScriptedBackend([0.40, 0.90])
            dispatch = _Dispatch(self.golden)
            adapter = self.adapter(tmp, backend, dispatch)
            self.set_cold_routing(adapter, theta=0.50, n_max=2)
            result = adapter.process_intent(
                self.intent(), context=self.context(),
                evidence_records=[self.golden["afterEvidence"]], now=self.NOW,
                identifiers={"run_id": "run-reenter", "episode_id": "episode-reenter",
                             "cycle_id": "cycle-reenter",
                             "proposal_id": "proposal-reenter",
                             "trial_id": "trial-reenter",
                             "r1_request_id": "request-reenter"})
            self.assertTrue(result["agreement"]["accepted"])
            self.assertEqual(len(result["cycles"]), 2)
            self.assertEqual(result["cycles"][0]["routed_to"], "negotiation")
            self.assertEqual(result["cycles"][1]["routed_to"], "trial")
            self.assertEqual(result["terminal_outcome"], "commit_revised")
            self.assertEqual(dispatch.create_policy_calls, 1)
            transitions = [(t["from"], t["to"])
                           for t in adapter.transition_snapshot()]
            self.assertIn(("S5", "S2"), transitions)

    def test_natural_language_entry_parses_at_s0_before_feasibility(self):
        backend = self.ScriptedBackend([0.90])
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self.adapter(tmp, backend)
            self.set_cold_routing(adapter, theta=0.50, n_max=1)
            result = adapter.process_intent(
                "keep UE downlink throughput above 1 Mbps",
                context=self.context(),
                evidence_records=[self.golden["afterEvidence"]], now=self.NOW,
                identifiers={"run_id": "run-natural",
                             "episode_id": "episode-natural",
                             "cycle_id": "cycle-natural",
                             "proposal_id": "proposal-natural",
                             "trial_id": "trial-natural",
                             "r1_request_id": "request-natural"})
            # One backend call parses the raw text and a second, distinct call
            # evaluates feasibility. A terminal result without these calls is a
            # regression back to constant S0/S1 assertions.
            self.assertGreaterEqual(backend.generate_calls, 2)
            self.assertIn("parse_ms", result)
            self.assertTrue(result["cycles"])
            transitions = [(t["from"], t["to"])
                           for t in adapter.transition_snapshot()]
            self.assertIn(("S0", "S1"), transitions)
            self.assertIn(("S1", "S2"), transitions)


class R1PolicyExecutorMonitorInterfaceTests(ContractFixture):
    """The base coordinator's continuous-assurance monitor
    (`_check_intent_satisfaction`) re-evaluates every MONITORED intent, and its
    POWER_CONSTRAINT branch queries ``executor.get_all_offsets()``. On the
    O-RAN profile that executor is an ``R1PolicyExecutor``; it must answer the
    query (the profile never actuates power over telnet, so all offsets read
    0.0) instead of raising ``AttributeError`` and sinking the whole admitted
    episode into an empty-trace TechnicalFailsafe.
    """

    def _executor(self):
        return R1PolicyExecutor(
            dispatch=_Dispatch(self.golden),
            assurance=CombinedAssurance(self.golden["capabilityManifest"]),
            capability_manifest=self.golden["capabilityManifest"])

    def test_get_all_offsets_is_empty_before_configure(self):
        self.assertEqual(self._executor().get_all_offsets(), {})

    def test_get_all_offsets_reports_default_zero_per_gnb(self):
        executor = self._executor()
        executor.configure_gnbs(["gnb1", "gnb2"])
        self.assertEqual(executor.get_all_offsets(), {"gnb1": 0.0, "gnb2": 0.0})

    def test_power_constraint_verdict_does_not_crash_on_r1_executor(self):
        # Directly reproduce the live failure: evaluate a POWER_CONSTRAINT
        # monitor while ``coordinator.executor`` is the R1 executor.
        with tempfile.TemporaryDirectory() as tmp:
            adapter = RAppCoordinatorAdapter(
                dispatch=_Dispatch(self.golden),
                assurance=CombinedAssurance(self.golden["capabilityManifest"]),
                ledger=EvidenceLedger(Path(tmp) / "evidence.jsonl"),
                capability_manifest=self.golden["capabilityManifest"],
                llm_manager=LLMBackendManager.with_backend(
                    DeterministicMockBackend(seed=7)))
            coordinator = adapter.coordinator
            self.assertIsInstance(coordinator.executor, R1PolicyExecutor)
            coordinator.executor.configure_gnbs(["gnb1", "gnb2"])
            power_intent = Intent(
                id="pwr-guard", type=IntentType.POWER_CONSTRAINT,
                target=IntentTarget("power", ConstraintType.MAX, 3.0, unit="dB"),
                scope=IntentScope(bs_ids=[]), priority=IntentPriority.HIGH)
            # No AttributeError; the guard reads within-bound (offsets all 0.0).
            verdict = coordinator._evaluate_intent_verdict(power_intent, {})
            self.assertIsInstance(verdict, MonitorVerdict)
            self.assertNotEqual(verdict, MonitorVerdict.VIOLATED)


if __name__ == "__main__":
    unittest.main()
