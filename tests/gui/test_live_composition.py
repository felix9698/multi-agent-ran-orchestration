"""The Live composition flow, driven end to end against the local mock stack.

This is the Live counterpart of ``test_ten_step_replay_scenario``.  It answers
the question that a green Replay run cannot: *does an intent typed into the
default Operator Console reach the preserved three-stage coordinator and leave
the console as an R1 outbound the contract accepts?*

Hermetic, and deliberately so:

* the deployment is ``oran.profiles.local_mock``, the contract-faithful stack
  this repository already ships and the conformance catalog already runs.  No
  new mock is invented here, nothing is stubbed inside the console, and the
  episode enters ``oran.rapp.gui_entry.run_gui_once`` - the same entry the
  headless rApp uses.
* the proposer is ``DeterministicMockBackend``.  No provider is contacted.
* nothing here touches a Near-RT RIC, an xApp, E2, a radio or a USRP.

What the flow does **not** claim
--------------------------------
The local deployment's Near-RT side is not driven into an enforcing state and
delivers no O1 evidence, because manufacturing either would be fabricating the
lower half of the system this campaign explicitly does not own.  The episode
therefore ends in a real, typed Eq.12 terminal state that is not ``Admitted``,
and that is the point: the upper composition is complete and honest, and what
remains is lower integration.  The assertions below pin the composition, never
a particular verdict.
"""

import json
import socket
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from decision.llm_backend import (DeterministicMockBackend, LLMBackendManager,
                                  RuleBasedBackend)
from gui.operator.app import OperatorConsole
from gui.operator.session.composition import (LiveComposition,
                                              SubmissionRefused,
                                              WITHDRAWAL_UNSUPPORTED)
from gui.operator.session.scenario import LIVE_STEPS, TenStepLiveScenario
from oran.conformance.contracts import ContractBundle, canonicalize
from oran.rapp.contract_support import _bundle_dir
from oran.rapp.gui_entry import (IntegrationError, LiveIntegration,
                                 RUNTIME_ENDPOINT_KEYS)
from tools.legacy.episode_support import legacy_episode_support

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT = REPO_ROOT / "docs" / "phase-a" / "raw" / "mock-local"
INTEGRATION_VALUES = DEPLOYMENT / "integration-values.json"
INTENT_TEXT = "keep UE downlink throughput above 1 Mbps"



def _legacy_console(**kwargs):
    """An Operator Console handed the *preserved* episode runtime.

    Every console in this module is one: this file's whole subject is the
    pre-Kernel Live composition, and since the B-01 cutover a console can only
    reach that composition if something outside it hands the runtime over.
    ``main.py`` hands over nothing, which is what
    ``tests/gui/test_default_entry_reachability.py`` asserts; here the test is
    the composition root, and it says so by name.
    """
    return OperatorConsole(legacy_episode=legacy_episode_support(), **kwargs)


def _golden():
    return json.loads((_bundle_dir() / "golden/golden-vectors.1.0.0.json")
                      .read_text(encoding="utf-8"))["canonicalObjects"]


def _policy_context():
    """The deployment-owned policy values, with a validity window that is now.

    Every value is read from the contract's own golden objects; the only thing
    computed here is the validity window, which has to be the present for a
    policy submitted in the present.
    """
    policy = _golden()["policy"]
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "ueId": policy["scope"]["ueId"],
        "allowedCells": policy["steeringObjective"]["actionEnvelope"]["allowedCells"],
        "forbiddenCells": [],
        "objectiveKind": "BALANCE_PRB_LOAD",
        "improvementThresholdPrb": 10,
        "minSecondsBetweenActuations": 30,
        "requiredKpiFreshnessMs": 3000,
        "actionDeadlineMs": 10000,
        "notBefore": now.isoformat().replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "rollbackOn": policy["rollbackPolicy"]["on"],
        "rollbackTimeoutMs": 10000,
        "intentRevision": 1,
        "policyRevision": 1,
        "correlationId": str(uuid.uuid4()),
        "producerId": policy["trace"]["producerId"],
    }


def _register_evidence_producer(profile):
    """Make the local deployment's evidence surface real.

    The DME type ``aic:policy-evidence:1.0.0`` is registered by the *producer*
    side over R1, exactly as the conformance catalog's SC-089 does and from the
    same contract fixtures.  Without it the deployment has no data-access
    surface to bind a job to, which is a fact about the deployment rather than
    about the console - so the test states it instead of leaving the console to
    trip over a 404 it did not cause.
    """
    bundle = ContractBundle.discover(None)
    registration = {
        "dmeTypeDefinition": {
            "dmeTypeId": {"namespace": "aic", "name": "policy-evidence",
                          "version": "1.0.0"},
            "metadata": {"dataCategory": ["PERFORMANCE"]},
            "dataProductionSchema": bundle.fixture(
                "fixture://policyEvidenceFilterSchema"),
            "dataDeliverySchemas": [{
                "type": "JSON_SCHEMA",
                "deliverySchemaId": "aic.policy-evidence.record.schema.1.0.0",
                "schema": canonicalize(bundle.schema(
                    "aic.policy-evidence.1.0.0.schema.json")).decode("utf-8"),
            }],
            "dataDeliveryMechanisms": [{"dataDeliveryMethod": "PUSH_HTTP"}],
        },
        "dataAccessEndpoint": profile.vector["r1"]["dme"]["dataAccessEndpoint"],
        "dataDeliveryModes": ["CONTINUOUS"],
    }
    response = profile.nonrt.handle(
        "POST", "/data-registration/v2/production-capabilities",
        body=registration, headers={"Version": "2.0.0-alpha.2"})
    if response.status != 201:
        raise AssertionError(
            f"the local deployment refused the evidence producer registration: "
            f"{response.status}")


def _write_profile(path: Path, *, runs_root: Path) -> Path:
    document = {
        "schema": "oran-aic-phase-b-gui-profile/1.0.0",
        "profileId": "live-mock",
        "label": "Local mock deployment",
        "description": "Loopback O-RAN mock stack; no radio is addressed.",
        "runsRoot": str(runs_root),
        "recording": True,
        "metricSelection": ["RRU.PrbDl"],
        "refreshIntervalMs": 100,
        "capabilityManifestPath": str(DEPLOYMENT / "capability.json"),
        "integrationValuesPath": str(INTEGRATION_VALUES),
        "policyContext": _policy_context(),
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def _manager():
    return LLMBackendManager.with_backend(DeterministicMockBackend(seed=7))


def _endpoint_overlay(profile):
    """The deployment's own resolved endpoints, and nothing else.

    ``LocalMockProfile`` computes these from the deployment vector whose byte
    digest it has already verified.  Only endpoint keys are handed over: the
    integration entry refuses anything else, so an overlay can never redirect
    an identity, a digest or a credential reference.
    """
    return {key: value for key, value in profile.runtime_values.items()
            if key in RUNTIME_ENDPOINT_KEYS}


class LiveDeploymentCase(unittest.TestCase):
    """Shared local deployment: booted once, torn down once.

    The local profile is imported inside ``setUpClass`` and unloaded in
    ``tearDownClass``, deliberately.  ``test_ubm_runtime_gates`` asserts,
    process-wide, that the bilateral-mock runtime never has the local profile
    loaded beside it; that gate is about the *runtime's* import graph, and a
    test-only deployment must not weaken it by leaving the module resident for
    the rest of the run.  Importing at module scope would pollute
    ``sys.modules`` at collection time, before any test has run at all.
    """

    @classmethod
    def setUpClass(cls):
        from oran.profiles.local_mock import LocalMockProfile

        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.state_dir = cls.root / "deployment-state"
        cls.profile = LocalMockProfile(INTEGRATION_VALUES, cls.state_dir)
        cls.profile.start()
        _register_evidence_producer(cls.profile)

    @classmethod
    def tearDownClass(cls):
        cls.profile.close()
        cls._tmp.cleanup()
        sys.modules.pop("oran.profiles.local_mock", None)

    def integration(self, *, state_dir=None):
        return LiveIntegration.load(
            INTEGRATION_VALUES,
            state_dir=state_dir or (self.root / "console-state"),
            runtime_values=_endpoint_overlay(self.profile),
            insecure_dev=True)


class TenStepLiveComposition(LiveDeploymentCase):
    """One run of the ten Live-composition steps, asserted from every angle."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.runs_root = cls.root / "runs"
        cls.profile_path = _write_profile(cls.root / "profile.json",
                                          runs_root=cls.runs_root)
        cls.console = _legacy_console(runs_root=str(cls.runs_root))
        cls.scenario = TenStepLiveScenario(
            cls.console, profile_path=cls.profile_path,
            intent_text=INTENT_TEXT,
            runtime_values=_endpoint_overlay(cls.profile),
            insecure_dev=True, state_dir=cls.root / "console-state",
            llm_manager=_manager(), export_dir=cls.root / "export")
        cls.result = cls.scenario.run()

    @classmethod
    def tearDownClass(cls):
        cls.console.shutdown()
        super().tearDownClass()

    def evidence(self, step_id):
        step = self.result.step(step_id)
        self.assertIsNotNone(step, f"{step_id} was never attempted")
        return step.evidence

    # -- the flow ----------------------------------------------------------- #

    def test_all_ten_steps_completed_in_order(self):
        self.assertTrue(self.scenario.ok, self.result.to_dict())
        self.assertEqual([s.step_id for s in self.result.steps],
                         [step_id for step_id, _title in LIVE_STEPS])

    def test_step_01_the_console_opened_disconnected(self):
        evidence = self.evidence("L10-01")
        self.assertEqual(evidence["mode"], "DISCONNECTED")
        self.assertFalse(evidence["isLive"])
        self.assertFalse(evidence["hasIntegration"])
        self.assertFalse(evidence["hasRecordedSource"])
        self.assertEqual(evidence["preflightChecks"], 0)

    def test_step_02_binding_a_deployment_is_not_connecting_to_it(self):
        evidence = self.evidence("L10-02")
        self.assertEqual(evidence["modeAfterBinding"], "DISCONNECTED")
        identity = evidence["integration"]
        self.assertTrue(identity["capabilityManifestId"])
        self.assertTrue(identity["bundleManifestJcsSha256"])
        self.assertTrue(identity["capabilityManifestSha256"])
        self.assertEqual(identity["endpointSource"],
                         "DEPLOYMENT_VECTOR_RUNTIME_OVERLAY")

    def test_step_03_live_started_only_after_a_transport_answered(self):
        evidence = self.evidence("L10-03")
        checks = {c["id"]: c for c in evidence["checks"]}
        self.assertEqual(checks["PF-R1-BOOTSTRAP"]["status"], "OK")
        self.assertEqual(checks["PF-CAPABILITY"]["status"], "OK")
        self.assertEqual(checks["PF-POLICY-TYPE"]["status"], "OK")
        self.assertEqual(checks["PF-DME-TYPE"]["status"], "OK")
        self.assertEqual(evidence["mode"], "LIVE")
        self.assertEqual(evidence["modeEvidence"]["basis"], "LIVE_R1_TRANSPORT")
        self.assertIn("no network element was started",
                      evidence["startMeaning"])

    def test_step_04_preview_named_the_deployment_and_submit_was_confirmed(self):
        evidence = self.evidence("L10-04")
        self.assertTrue(evidence["previewShown"])
        self.assertTrue(evidence["previewSubmittable"])
        self.assertTrue(evidence["previewIntegration"]["capabilityManifestId"])
        self.assertTrue(evidence["confirmationShown"])
        self.assertTrue(evidence["confirmationTargets"])
        self.assertTrue(evidence["contractIntentId"])

    def test_step_05_one_submission_entered_the_coordinator_exactly_once(self):
        evidence = self.evidence("L10-05")
        self.assertEqual(evidence["coordinatorEntries"], 1)
        self.assertEqual(evidence["submissions"], 1)
        self.assertEqual(evidence["storedEpisodes"], 1)
        self.assertIsNone(evidence["inFlight"])
        self.assertEqual(evidence["entryModule"],
                         "oran.rapp.gui_entry.run_gui_once")

    def test_step_06_the_r1_outbound_satisfies_the_frozen_contract(self):
        evidence = self.evidence("L10-06")
        outbound = evidence["r1Outbound"]
        self.assertTrue(outbound["policyId"])
        self.assertTrue(outbound["contractValid"])
        self.assertEqual(outbound["policyTypeId"], "AIC_UECellSteering_1.0.0")
        # The intent identity that went out is the contract one the console
        # minted, not the coordinator's internal short id.
        uuid.UUID(str(outbound["intentId"]))
        self.assertTrue(evidence["policyStatus"])

    def test_step_06_the_deployment_really_holds_that_policy(self):
        """Read it back from the deployment, not from the episode record."""
        outbound = self.evidence("L10-06")["r1Outbound"]
        response = self.profile.nonrt.handle(
            "GET",
            f"/a1-policy-management/v1/policies/{outbound['policyId']}",
            headers={"Version": "1.0.0"})
        self.assertEqual(response.status, 200)

    def test_step_07_status_and_the_active_intent_row_are_real(self):
        evidence = self.evidence("L10-07")
        self.assertGreater(evidence["components"], 0)
        rows = evidence["activeIntents"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["intentId"])
        projection = evidence["statusProjection"]
        self.assertEqual(projection["mode"], "LIVE")
        self.assertEqual(projection["source"], "LIVE_INTEGRATION")
        self.assertTrue(projection["observedAt"])
        self.assertTrue(projection["integration"]["capabilityManifestId"])

    def test_step_07_an_element_the_manifest_omits_stays_unsupported(self):
        evidence = self.evidence("L10-07")
        self.assertTrue(evidence["unsupportedElements"],
                        "a deployment that declares nothing about an element "
                        "must render it Unsupported, not omit it")

    def test_step_08_the_eq12_terminal_state_is_typed_and_displayed(self):
        evidence = self.evidence("L10-08")
        self.assertIn(evidence["eq12State"],
                      ("Admitted", "NotAdmitted", "TechnicalFailsafe"))
        self.assertTrue(evidence["terminalOutcome"])
        self.assertTrue(evidence["fsmStages"])
        self.assertTrue(evidence["llmStages"])

    def test_step_08_a_real_episode_carries_its_confidence_and_threshold(self):
        """The half a capture cannot have: this one ran, so it has numbers."""
        evidence = self.evidence("L10-08")
        self.assertIsNotNone(evidence["rawConfidence"])
        self.assertIsNotNone(evidence["calibratedProbability"])
        self.assertIsNotNone(evidence["thetaStar"])

    def test_step_09_the_export_carries_mode_identity_and_provenance(self):
        evidence = self.evidence("L10-09")
        self.assertEqual(evidence["exportMode"], "LIVE")
        for name in ("decision.json", "summary.json", "events.json"):
            self.assertIn(name, evidence["exportedFiles"])
        identity = evidence["integration"]
        self.assertTrue(identity["capabilityManifestId"])
        self.assertTrue(identity["bundleManifestJcsSha256"])
        self.assertTrue(identity["r1ApiRoot"])
        self.assertTrue(evidence["exportedAt"])

    def test_step_09_the_exported_decision_names_the_r1_outbound(self):
        export_dir = Path(self.evidence("L10-09")["exportDir"])
        decision = json.loads((export_dir / "decision.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(decision["mode"], "LIVE")
        episodes = decision["data"]["episodes"]
        self.assertEqual(len(episodes), 1)
        self.assertTrue(episodes[0]["r1Outbound"]["policyId"])
        self.assertTrue(episodes[0]["integration"]["capabilityManifestId"])
        self.assertTrue(episodes[0]["contractIntentId"])

    def test_step_09_no_export_file_carries_model_text_or_a_secret(self):
        export_dir = Path(self.evidence("L10-09")["exportDir"])
        forbidden = ("BEGIN PRIVATE KEY", "api_key", "apiKey")
        for path in sorted(export_dir.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for token in forbidden:
                self.assertNotIn(token, text, f"{path.name} carries {token}")

    def test_step_10_the_session_finalized_and_left_nothing_attached(self):
        evidence = self.evidence("L10-10")
        self.assertEqual(evidence["disposition"], "COMPLETED")
        self.assertTrue(evidence["manifestWritten"])
        self.assertTrue(evidence["integrationDetached"])
        self.assertTrue(evidence["recordedSourceDetached"])
        self.assertIsNone(evidence["requestedMode"])
        self.assertTrue(evidence["submitterCleared"])
        self.assertEqual(evidence["modeAfterClose"], "DISCONNECTED",
                         "a console holding nothing must not keep claiming "
                         "the mode of the run it just finished")

    def test_step_08_the_failure_cause_is_recorded_beside_the_terminal(self):
        """A failsafe must be distinguishable from a refusal, and say why."""
        observed = [e for e in self.console.controller.timeline
                    if e.kind == "DECISION_OBSERVED"]
        # The timeline was cleared by the Replay-free teardown path only after
        # the flow; the run's own events are the durable record.
        run_dir = Path(self.evidence("L10-10")["runDir"])
        events = [json.loads(line) for line in
                  (run_dir / "events" / "timeline.jsonl")
                  .read_text(encoding="utf-8").splitlines() if line.strip()]
        decisions = [e for e in events if e["kind"] == "DECISION_OBSERVED"]
        self.assertEqual(len(decisions), 1, observed)
        detail = decisions[0]["detail"]
        self.assertTrue(detail["terminalOutcome"])
        self.assertIn("assuranceDecision", detail)
        statuses = [e for e in events
                    if e["kind"] == "A1_POLICY_STATUS_OBSERVED"]
        self.assertEqual(len(statuses), 1)
        self.assertTrue(statuses[0]["detail"]["enforceStatus"])

    def test_the_run_manifest_is_live_and_says_why(self):
        manifest = json.loads(
            (Path(self.evidence("L10-10")["runDir"]) / "manifest.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(manifest["mode"], "LIVE")
        self.assertEqual(manifest["modeEvidence"]["basis"], "LIVE_R1_TRANSPORT")


class LiveCompositionRefusals(LiveDeploymentCase):
    """The refusals that keep one operator action to one episode."""

    def setUp(self):
        self.runs = Path(tempfile.mkdtemp(dir=self.root))
        self.console = _legacy_console(runs_root=str(self.runs))
        _write_profile(self.runs / "profile.json", runs_root=self.runs)
        self.console.handle_action("profile_load",
                                   str(self.runs / "profile.json"))
        self.console.attach_live_integration(
            self.integration(state_dir=self.runs / "state"),
            llm_manager=_manager())

    def tearDown(self):
        self.console.shutdown()

    def _start(self):
        self.console.controller.preflight(**self.console.preflight_inputs())
        self.console.select_mode("LIVE")
        mode, evidence = self.console.session_mode_evidence()
        self.console.controller.start(mode=mode, mode_evidence=evidence)

    def test_live_cannot_be_selected_without_a_runtime_behind_it(self):
        """A bare console - the deployed build - cannot ask for Live.

        The wording changed with the B-01 cutover and so did the thing it
        checks: Live is backed by an attached Kernel submission session, not by
        a bound legacy deployment, and a console with neither is refused with
        the name of the one that ships.
        """
        console = OperatorConsole(runs_root=str(self.runs / "bare"))
        try:
            with self.assertRaises(Exception) as caught:
                console.select_mode("LIVE")
            self.assertIn("Kernel submission session", str(caught.exception))
        finally:
            console.shutdown()

    def test_live_cannot_start_before_a_transport_answered(self):
        console = _legacy_console(runs_root=str(self.runs / "no-transport"))
        try:
            console.attach_live_integration(
                self.integration(state_dir=self.runs / "state-2"),
                llm_manager=_manager())
            console.requested_mode = "LIVE"
            with self.assertRaises(Exception) as caught:
                console.session_mode_evidence()
            self.assertIn("Preflight has not run", str(caught.exception))
        finally:
            console.shutdown()

    def test_a_second_submission_while_one_is_in_flight_is_refused(self):
        composition = self.console.live
        started = __import__("threading").Event()
        release = __import__("threading").Event()

        def slow_runner(**kwargs):
            started.set()
            release.wait(30)
            from oran.rapp.gui_entry import run_gui_once

            return run_gui_once(**kwargs)

        composition.episode_runner = slow_runner
        self._start()
        self.console.confirm = lambda spec: type(
            "Outcome", (), {"confirmed": True})()
        self.console.handle_action("submit", INTENT_TEXT)
        self.assertTrue(started.wait(30), "the first episode never started")
        self.console.handle_action("submit", INTENT_TEXT)
        release.set()
        self.console.controller.join_workers(timeout=120)
        refusals = [e for e in self.console.controller.timeline
                    if e.kind == "ACTION_REFUSED"
                    and "still running" in e.title]
        self.assertEqual(len(refusals), 1)
        self.assertEqual(composition.submissions, 1)
        self.console.controller.stop("COMPLETED")

    def test_a_direct_second_call_is_refused_by_the_composition_itself(self):
        """The guard does not depend on the console reaching it first."""
        composition = self.console.live
        composition._in_flight = "episode-in-flight"
        try:
            with self.assertRaises(SubmissionRefused) as caught:
                composition.submit(INTENT_TEXT, policy_context=_policy_context())
            self.assertIn("second coordinator run", str(caught.exception))
        finally:
            composition._in_flight = None

    def test_a_submission_during_finalize_is_refused(self):
        self._start()
        self.console.controller._stop_requested.set()
        self.console.intent_submitter = lambda _text: self.fail(
            "submitted while finalizing")
        self.console.handle_action("submit", INTENT_TEXT)
        self.assertIn("finalizing", self.console.controller.timeline[-1].title)
        self.console.controller._stop_requested.clear()
        self.console.controller.stop("COMPLETED")

    def test_withdrawal_is_disabled_with_the_contract_reason(self):
        self.console.handle_action("withdraw", "intent-1")
        last = self.console.controller.timeline[-1]
        self.assertEqual(last.kind, "ACTION_UNSUPPORTED")
        self.assertEqual(last.title, WITHDRAWAL_UNSUPPORTED)
        self.assertIn("no intent-withdrawal operation", last.title)

    def test_an_incomplete_policy_context_is_refused_by_name(self):
        context = _policy_context()
        del context["actionDeadlineMs"]
        with self.assertRaises(IntegrationError) as caught:
            self.console.live.integration.episode_request(
                intent_text=INTENT_TEXT, policy_context=context)
        self.assertIn("actionDeadlineMs", str(caught.exception))
        self.assertIn("no default may be invented", str(caught.exception))


class LiveModeIsolation(LiveDeploymentCase):
    """A new session inherits nothing from the session before it."""

    def test_a_replay_session_after_a_live_one_carries_nothing_across(self):
        runs = Path(tempfile.mkdtemp(dir=self.root))
        console = _legacy_console(runs_root=str(runs))
        _write_profile(runs / "profile.json", runs_root=runs)
        console.handle_action("profile_load", str(runs / "profile.json"))
        console.attach_live_integration(
            self.integration(state_dir=runs / "state"), llm_manager=_manager())
        console.controller.preflight(**console.preflight_inputs())
        console.select_mode("LIVE")
        mode, evidence = console.session_mode_evidence()
        console.controller.start(mode=mode, mode_evidence=evidence)
        console.confirm = lambda spec: type("Outcome", (), {"confirmed": True})()
        console.handle_action("submit", INTENT_TEXT)
        console.controller.join_workers(timeout=120)
        live_run = console.controller.run_id
        live_state = console.controller.state()
        self.assertTrue(live_state.intents)
        self.assertIsNotNone(live_state.decision)
        console.controller.stop("COMPLETED")

        # Now the same console reads a recording.  Nothing from the Live
        # session may appear in it.
        capture = REPO_ROOT / "tests" / "gui" / "fixtures" / "lo1-capture-min"
        console.attach_replay_source(capture)
        self.assertIsNone(console.live,
                          "attaching a recording must drop the deployment")
        console.select_mode("REPLAY")
        console.controller.preflight(runs_root=str(runs))
        mode, evidence = console.session_mode_evidence()
        self.assertEqual(mode, "REPLAY")
        console.controller.start(mode=mode, mode_evidence=evidence)
        state = console.controller.state()
        try:
            self.assertEqual(state.mode, "REPLAY")
            self.assertFalse(state.is_live)
            self.assertEqual(state.intents, ())
            self.assertIsNone(state.decision)
            self.assertEqual(state.components, ())
            self.assertNotEqual(console.controller.run_id, live_run)
            for event in state.timeline:
                self.assertNotEqual(event.kind, "R1_POLICY_OUTBOUND")
                self.assertNotEqual(event.kind, "DECISION_OBSERVED")
        finally:
            console.controller.stop("COMPLETED")
            console.shutdown()

    def test_a_replay_selection_is_not_upgraded_to_live_by_a_transport(self):
        """Both kinds of evidence present; the operator's choice decides."""
        runs = Path(tempfile.mkdtemp(dir=self.root))
        console = _legacy_console(runs_root=str(runs))
        try:
            console.attach_live_integration(
                self.integration(state_dir=runs / "state"),
                llm_manager=_manager())
            console.controller.preflight(**console.preflight_inputs())
            capture = REPO_ROOT / "tests" / "gui" / "fixtures" / "lo1-capture-min"
            console.attach_replay_source(capture)
            console.select_mode("REPLAY")
            mode, evidence = console.session_mode_evidence()
            self.assertEqual(mode, "REPLAY")
            self.assertEqual(evidence["basis"], "REPLAY_OF_RECORDED_SOURCE")
        finally:
            console.shutdown()


class LiveProposerControl(LiveDeploymentCase):
    """Model selection reaches the next episode and never rewrites a past one."""

    def test_a_switch_during_an_episode_applies_to_the_next_one(self):
        manager = LLMBackendManager.with_backend(DeterministicMockBackend(seed=7))
        other = RuleBasedBackend(seed=11)
        manager.dynamic_backends[other.name] = other
        composition = LiveComposition(
            integration=self.integration(
                state_dir=Path(tempfile.mkdtemp(dir=self.root))),
            llm_manager=manager,
            episode_support=legacy_episode_support())
        first = manager.active_backend_name()
        self.assertIn(first, manager.get_available_names())

        composition._in_flight = "episode-1"
        applied, message = composition.select_backend(other.name)
        composition._in_flight = None
        self.assertFalse(applied)
        self.assertIn("keeps the provenance it started with", message)
        self.assertEqual(manager.active_backend_name(), first,
                         "a running episode's proposer must not change")

        episode = composition.submit(INTENT_TEXT,
                                     policy_context=_policy_context())
        self.assertEqual(manager.active_backend_name(), other.name)
        self.assertEqual(episode.proposer_at_submit, other.name)
        record = episode.store_records()["episode"]
        self.assertEqual(record["proposerAtSubmit"], other.name)

    def test_the_registry_never_publishes_a_credential_value(self):
        composition = LiveComposition(
            integration=self.integration(
                state_dir=Path(tempfile.mkdtemp(dir=self.root))),
            llm_manager=_manager(),
            episode_support=legacy_episode_support())
        for view in composition.backend_views():
            for ref in view.credential_refs:
                self.assertRegex(ref, r"^(env|file|keyring|vault):")


class RuntimeOverlayIsEndpointOnly(unittest.TestCase):
    """The overlay allowlist, tested where it has to hold: ``run_once`` itself.

    A cross-check found the hole this class exists to close.  The GUI seam
    refused a non-endpoint overlay, but the episode authority applied whatever
    it was handed - so ``run_once(runtime_values={"r1.rAppId": "forged-rapp"})``
    reached ``R1Client(r_app_id="forged-rapp")``, and any caller that did not go
    through the console silently opted out of the rule the commit claimed.
    Checking a rule only at one caller is not checking it.

    These tests call the authority directly and never touch the deployment:
    every collaborator below the check is a mock, so a refusal that is supposed
    to happen *before* anything is read is proved by nothing being read.
    """

    VALUES = {
        "backend.capabilityManifestPath": "capability.json",
        "backend.capabilityManifestSha256": "0" * 64,
        "r1.apiRoot": "https://deployment.example/r1",
        "r1.rAppId": "the-real-rapp",
        "r1.dme.policyEvidencePushBaseUri": "https://deployment.example/push",
    }

    def _request(self, tmp):
        return {
            "statePath": str(Path(tmp) / "state.json"),
            "evidencePath": str(Path(tmp) / "evidence.jsonl"),
            "intentText": INTENT_TEXT,
            "policyContext": _policy_context(),
            "identifiers": {"run_id": "run-overlay"},
        }

    def _run(self, tmp, runtime_values):
        """Drive ``run_once`` with every collaborator replaced.

        ``build_r1_security`` is one of those collaborators: the episode entry
        now supplies the secure client's TLS context and authorization hook from
        the document's credential references, and this fixture's ``VALUES`` stub
        declares endpoints only because endpoints are its whole subject.  What
        the builder does with real references, and what it refuses, lives in
        ``tests/gui/test_live_r1_security.py``.
        """
        from oran.rapp import headless

        with mock.patch.object(headless, "load_integration_values",
                               return_value=dict(self.VALUES)) as values, \
             mock.patch.object(headless, "_load_pinned_json",
                               return_value={"nearRtRicId": "ric-1"}) as pinned, \
             mock.patch.object(headless, "validate") as validated, \
             mock.patch.object(headless, "R1Client") as client, \
             mock.patch.object(headless, "build_r1_security"), \
             mock.patch.object(headless, "CombinedAssurance"), \
             mock.patch.object(headless, "EvidenceLedger"), \
             mock.patch("oran.rapp.coordinator_adapter.RAppCoordinatorAdapter") as adapter:
            adapter.return_value.process_intent.return_value = {
                "terminal_outcome": "pending_not_admitted"}
            try:
                result = headless.run_once(
                    integration_path=str(Path(tmp) / "values.json"),
                    request=self._request(tmp), runtime_values=runtime_values)
                error = None
            except ValueError as exc:
                result, error = None, exc
            return {"result": result, "error": error, "client": client,
                    "values": values, "pinned": pinned,
                    "validated": validated, "adapter": adapter}

    def test_a_forged_identity_overlay_is_refused_by_name(self):
        """The reviewer's probe, verbatim."""
        with tempfile.TemporaryDirectory() as tmp:
            probe = self._run(tmp, {"r1.rAppId": "forged-rapp"})
        self.assertIsNotNone(probe["error"], "the forged identity was applied")
        message = str(probe["error"])
        self.assertIn("r1.rAppId", message)
        self.assertIn("endpoints only", message)
        # Refused before anything was read, built or dispatched.
        probe["client"].assert_not_called()
        probe["values"].assert_not_called()
        probe["pinned"].assert_not_called()
        probe["adapter"].assert_not_called()

    def test_the_pinned_capability_artifact_cannot_be_swapped(self):
        for key, value in (("backend.capabilityManifestPath", "other.json"),
                           ("backend.capabilityManifestSha256", "f" * 64),
                           ("backend.releaseManifestSha256", "e" * 64),
                           ("deployment.testVectorSha256", "d" * 64),
                           ("r1.https.oauthCredentialRef", "env://forged")):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                probe = self._run(tmp, {key: value})
                self.assertIsNotNone(probe["error"],
                                     f"{key} was accepted as an overlay")
                self.assertIn(key, str(probe["error"]))
                probe["client"].assert_not_called()

    def test_one_bad_key_refuses_the_whole_overlay(self):
        """No partial application: a mixed overlay is refused entirely."""
        with tempfile.TemporaryDirectory() as tmp:
            probe = self._run(tmp, {"r1.apiRoot": "http://127.0.0.1:21000/r1",
                                    "r1.rAppId": "forged-rapp"})
        self.assertIsNotNone(probe["error"])
        self.assertIn("r1.rAppId", str(probe["error"]))
        self.assertNotIn("r1.apiRoot", str(probe["error"]).split("Permitted")[0])
        probe["client"].assert_not_called()

    def test_a_legitimate_endpoint_overlay_still_reaches_the_client(self):
        """The permitted half of the contract, unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            probe = self._run(tmp, {
                "r1.apiRoot": "http://127.0.0.1:21000/r1",
                "r1.dme.policyEvidencePushBaseUri": "http://127.0.0.1:21002/push"})
        self.assertIsNone(probe["error"])
        kwargs = probe["client"].call_args.kwargs
        self.assertEqual(kwargs["api_root"], "http://127.0.0.1:21000/r1")
        self.assertEqual(kwargs["policy_evidence_push_base_uri"],
                         "http://127.0.0.1:21002/push")
        # And the identity the document pinned is the one that was used.
        self.assertEqual(kwargs["r_app_id"], "the-real-rapp")

    def test_no_overlay_at_all_behaves_exactly_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            probe = self._run(tmp, None)
        self.assertIsNone(probe["error"])
        kwargs = probe["client"].call_args.kwargs
        self.assertEqual(kwargs["api_root"], self.VALUES["r1.apiRoot"])
        self.assertEqual(kwargs["r_app_id"], self.VALUES["r1.rAppId"])

    def test_the_console_and_the_authority_share_one_allowlist(self):
        """Not two lists that agree today - one object."""
        from oran.rapp import gui_entry, headless

        self.assertIs(gui_entry.RUNTIME_ENDPOINT_KEYS,
                      headless.RUNTIME_ENDPOINT_KEYS)
        for key in headless.RUNTIME_ENDPOINT_KEYS:
            self.assertTrue(key.endswith(("apiRoot", "Uri", "Reference",
                                          "Root", "Destination")),
                            f"{key} is not an endpoint-shaped key")


class DisconnectedStartupIsOffline(unittest.TestCase):
    """The default console opens without touching the network at all."""

    def test_building_the_console_opens_no_socket(self):
        opened = []

        def refuse(*args, **kwargs):
            opened.append(args)
            raise AssertionError("the console opened a socket at startup")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(socket, "socket", refuse), \
                 mock.patch.object(socket, "create_connection", refuse):
                console = _legacy_console(runs_root=tmp)
                try:
                    state = console.controller.state()
                    self.assertEqual(state.mode, "DISCONNECTED")
                    self.assertFalse(state.is_live)
                    self.assertIsNone(console.live)
                    self.assertIsNone(console.requested_mode)
                    self.assertIsNone(console.intent_submitter)
                finally:
                    console.shutdown()
        self.assertEqual(opened, [])

    def test_an_unreadable_integration_document_is_refused_not_defaulted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(IntegrationError):
                LiveIntegration.load(Path(tmp) / "missing.json",
                                     state_dir=Path(tmp) / "state")

    def test_a_runtime_overlay_may_not_carry_a_non_endpoint_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(IntegrationError) as caught:
                LiveIntegration.load(
                    INTEGRATION_VALUES, state_dir=Path(tmp) / "state",
                    runtime_values={"backend.capabilityManifestSha256": "0" * 64})
            self.assertIn("backend.capabilityManifestSha256",
                          str(caught.exception))


if __name__ == "__main__":
    unittest.main()
