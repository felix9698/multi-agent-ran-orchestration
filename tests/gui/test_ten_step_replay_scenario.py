"""phaseB_task.md section 10: the ten operator steps, driven end to end.

The skip is gone.  T4 left this test visible and honestly skipped because the
frozen ownership contract defined no controller-level Replay driver and T4 would
have had to invent one in the verification layer.  Integration owns that seam
now, so the driver exists (``gui/operator/session/scenario.py``) and this is its
test.

Hermetic: it drives the committed ``lo1-capture-min`` fixture, which is the
``run-012`` reference capture with three lab identifiers redacted (see
``tests/gui/fixtures/PROVENANCE.md``).  No display, no network, no hardware, no
LLM call.  The same driver runs against the unredacted run-012 capture under
Xvfb for the screenshot evidence; nothing about the flow differs.

What is asserted here is not "ten methods returned" - it is the properties that
would make a green ten-step run a lie:

* the session is REPLAY and says so everywhere a reader could look;
* the recorded decision is never attributed to the submitted intent text;
* an unsupported metric stays visible with its reason instead of vanishing, and
  no card shows a number the run did not measure;
* the finalized run re-opens with its content, and the export carries the mode,
  the availability and the watermark.
"""

import json
import tempfile
import unittest
from pathlib import Path

from gui.operator.app import OperatorConsole
from gui.operator.session.scenario import (STEPS, ScenarioError,
                                           TenStepReplayScenario,
                                           run_ten_step_replay)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "gui" / "fixtures"
CAPTURE = FIXTURES / "lo1-capture-min"
PROFILE = FIXTURES / "profile-min.json"
MANIFEST = FIXTURES / "capability-manifest-min.json"


def _manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


class TenStepReplayScenarioTests(unittest.TestCase):
    """One run of the flow, asserted from every angle that matters."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.console, cls.result = run_ten_step_replay(
            CAPTURE, runs_root=root / "runs", profile_path=PROFILE,
            capability_manifest=_manifest(),
            export_dir=root / "export")

    @classmethod
    def tearDownClass(cls):
        cls.console.shutdown()
        cls._tmp.cleanup()

    def evidence(self, step_id):
        step = self.result.step(step_id)
        self.assertIsNotNone(step, f"{step_id} was never attempted")
        return step.evidence

    # -- the flow ----------------------------------------------------------- #

    def test_all_ten_steps_completed_in_order(self):
        self.assertTrue(self.result.ok, self.result.to_dict())
        self.assertEqual([s.step_id for s in self.result.steps],
                         [step_id for step_id, _title in STEPS])
        self.assertEqual([s.number for s in self.result.steps],
                         list(range(1, 11)))

    def test_step_01_the_profile_reached_the_controller(self):
        evidence = self.evidence("S10-01")
        self.assertEqual(evidence["profileId"], evidence["controllerProfileId"])
        self.assertTrue(evidence["profileId"])

    def test_step_01_a_profile_that_names_a_manifest_brings_its_inventory(self):
        """Loading a profile adopts the capability manifest it names.

        The field has been on the profile since the design step and the console
        read a manifest only from its constructor, so an operator who started
        from main.py and pressed Load got a topology that could never show the
        deployment - whatever the profile said.
        """
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            try:
                self.assertEqual(console.capability_manifest, {})
                console.handle_action("profile_load", str(PROFILE))
                self.assertEqual(console.capability_manifest.get("manifestId"),
                                 _manifest()["manifestId"])
                console.controller.preflight(
                    capability_manifest=console.capability_manifest,
                    runs_root=tmp)
                console.refresh_status()
                kinds = {c.kind for c in console.controller.state().components}
                self.assertIn("GNB", kinds)
                self.assertIn("O_DU", kinds)
                # And what the manifest does not declare is Unsupported, with a
                # reason - not absent, which would read as "there is none here".
                undeclared = [c for c in console.controller.state().components
                              if c.status == "UNSUPPORTED"]
                self.assertTrue(undeclared)
                for component in undeclared:
                    self.assertTrue(component.status_reason)
            finally:
                console.shutdown()

    def test_step_01_an_unreadable_manifest_warns_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "profile.json"
            broken.write_text(json.dumps({
                "schema": "oran-aic-phase-b-gui-profile/1.0.0",
                "profileId": "broken", "runsRoot": tmp,
                "capabilityManifestPath": str(Path(tmp) / "missing.json"),
            }), encoding="utf-8")
            console = OperatorConsole(runs_root=tmp)
            try:
                console.handle_action("profile_load", str(broken))
                kinds = [e.kind for e in console.controller.timeline]
                self.assertIn("CAPABILITY_UNREADABLE", kinds)
                self.assertEqual(console.capability_manifest, {})
            finally:
                console.shutdown()

    def test_step_02_preflight_ran_and_named_its_boundaries(self):
        evidence = self.evidence("S10-02")
        self.assertEqual(evidence["blocking"], [])
        ids = {check["id"] for check in evidence["checks"]}
        self.assertIn("PF-PROFILE", ids)
        self.assertIn("PF-RUNS-ROOT", ids)
        self.assertIn("PF-CAPABILITY", ids)
        # No live transport in this environment, and the check says so rather
        # than passing quietly.
        r1 = next(c for c in evidence["checks"] if c["id"] == "PF-R1-BOOTSTRAP")
        self.assertNotEqual(r1["status"], "OK")
        self.assertTrue(r1["reason"])

    def test_step_03_the_session_is_replay_and_never_claims_live(self):
        evidence = self.evidence("S10-03")
        self.assertEqual(evidence["mode"], "REPLAY")
        self.assertFalse(evidence["isLive"])
        self.assertEqual(evidence["modeEvidence"]["basis"],
                         "REPLAY_OF_RECORDED_SOURCE")
        self.assertTrue(evidence["sourceRunId"])
        self.assertIn("no network element was started",
                      evidence["startMeaning"])

    def test_step_03_start_is_refused_when_nothing_is_attached(self):
        """The other half of the rule: no source, no session, stated reason."""
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp,
                                      capability_manifest=_manifest())
            try:
                console.controller.preflight(runs_root=tmp)
                with self.assertRaises(Exception) as caught:
                    console.session_mode_evidence()
                self.assertIn("no session source is attached",
                              str(caught.exception))
            finally:
                console.shutdown()

    def test_step_04_the_confirmation_was_shown_before_the_submission(self):
        evidence = self.evidence("S10-04")
        self.assertTrue(evidence["confirmationShown"])
        self.assertTrue(evidence["confirmationTargets"])
        self.assertTrue(evidence["confirmationEffects"])
        self.assertEqual(evidence["intentText"],
                         next(s for s in self.result.steps
                              if s.step_id == "S10-04").evidence["intentText"])

    def test_step_04_the_replayed_decision_is_not_attributed_to_the_text(self):
        """The dishonesty this flow could most easily commit, asserted against.

        The operator's submission is real.  The decision is the recording's.
        Both facts have to survive into the stored record, or a reader of the
        run would conclude the typed sentence produced that outcome.
        """
        evidence = self.evidence("S10-04")
        self.assertIn("were not produced by this text", evidence["attribution"])

        episodes = list(self.reopened().read_episodes())
        self.assertTrue(episodes)
        replay_of = episodes[0].get("replayOf")
        self.assertIsInstance(replay_of, dict)
        self.assertTrue(replay_of.get("sourceRunId"))
        self.assertEqual(replay_of.get("episodeId"), episodes[0]["episodeId"])

        derived = [e for e in self.reopened().read_events()
                   if e.get("kind") == "DECISION_REPLAYED"]
        self.assertEqual(len(derived), 1)
        self.assertEqual(derived[0]["origin"], "DERIVED")
        self.assertIn("did not produce it", derived[0]["derivation"])

    def test_step_05_the_s0_s6_path_and_the_eq12_terminal_are_read_back(self):
        evidence = self.evidence("S10-05")
        self.assertIn(evidence["eq12State"],
                      ("Admitted", "NotAdmitted", "TechnicalFailsafe"))
        visited = evidence["fsmVisited"]
        self.assertIn("S0", visited)
        self.assertIn("S6", visited)
        self.assertTrue(evidence["llmStages"])

    def test_step_05_a_capture_carries_no_confidence_and_says_so(self):
        """Section 4: never synthesize a KPI the source does not have.

        The same rule applies to a decision field.  A capture has no confidence,
        no theta* and no per-stage LLM latency; those must come back None, not
        zero and not a plausible-looking number.
        """
        evidence = self.evidence("S10-05")
        self.assertIsNone(evidence["rawConfidence"])
        self.assertIsNone(evidence["calibratedProbability"])
        self.assertIsNone(evidence["thetaStar"])
        self.assertIn("Unavailable", evidence["unavailableNote"])

    def test_step_06_the_policy_and_evidence_lanes_were_replayed(self):
        evidence = self.evidence("S10-06")
        self.assertGreater(evidence["eventCount"], 0)
        lanes = evidence["lanes"]
        self.assertTrue({"R1", "A1", "O1", "DME", "COORDINATOR"} & set(lanes))
        self.assertTrue(evidence["policyIds"])

    def test_step_07_measured_and_unmeasured_metrics_are_both_visible(self):
        evidence = self.evidence("S10-07")
        self.assertGreater(evidence["sampleCount"], 0)
        measured = {m["metric"] for m in evidence["metricsMeasured"]}
        self.assertIn("RRU.PrbDl", measured)
        for metric in evidence["metricsMeasured"]:
            self.assertIsNotNone(metric["value"])
            self.assertTrue(metric["unit"])
            self.assertTrue(metric["quality"],
                            "a value without its quality is not a measurement")
        unmeasured = {m["metric"] for m in evidence["metricsNotMeasured"]}
        self.assertIn("DRB.UEThpDl", unmeasured,
                      "subscribed but never delivered - it must stay on screen")
        for metric in evidence["metricsNotMeasured"]:
            self.assertTrue(metric["reason"])

    def test_step_07_annotations_are_placed_only_where_the_axis_has_a_time(self):
        evidence = self.evidence("S10-07")
        self.assertLessEqual(len(evidence["placedAnnotations"]),
                             len(evidence["annotatableEvents"]))
        self.assertIn("invented position", evidence["annotationNote"])

    def test_step_08_the_run_finalized_as_completed(self):
        evidence = self.evidence("S10-08")
        self.assertEqual(evidence["disposition"], "COMPLETED")
        self.assertTrue(evidence["manifestWritten"])

    def test_step_09_the_finalized_run_reopens_with_its_content(self):
        evidence = self.evidence("S10-09")
        self.assertEqual(evidence["mode"], "REPLAY")
        self.assertEqual(evidence["disposition"], "COMPLETED")
        self.assertGreater(evidence["sampleCount"], 0)
        self.assertGreater(evidence["eventCount"], 0)
        self.assertGreater(evidence["episodeCount"], 0)
        self.assertIn("RRU.PrbDl", evidence["metricsIndexed"])

    def test_step_10_the_export_carries_mode_availability_and_a_watermark(self):
        evidence = self.evidence("S10-10")
        self.assertEqual(evidence["exportMode"], "REPLAY")
        self.assertTrue(evidence["figureWatermarked"],
                        "a non-LIVE figure must carry its mode watermark")
        for name in ("telemetry.csv", "telemetry.json", "events.csv",
                     "decision.json", "summary.json"):
            self.assertIn(name, evidence["exportedFiles"])
        availability = evidence["metricAvailability"]
        self.assertIn("RRU.PrbDl", availability)

    def test_step_10_the_figure_ships_with_its_source_data(self):
        evidence = self.evidence("S10-10")
        base = Path(self.result.session_run_dir)
        self.assertTrue((base / evidence["figureSourceCsv"]).is_file())
        suffixes = {Path(p).suffix for p in evidence["figurePaths"]}
        self.assertIn(".pdf", suffixes, "a paper figure needs a vector format")
        self.assertIn(".png", suffixes, "and a high-resolution raster")
        for rel in evidence["figurePaths"]:
            self.assertTrue((base / rel).is_file(), rel)

    def test_the_export_directory_is_self_describing_after_a_move(self):
        manifest = json.loads(
            (Path(self.result.export_dir) / "EXPORT-MANIFEST.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(manifest["mode"], "REPLAY")
        self.assertIn("A run read back from a recording is REPLAY",
                      manifest["modeNote"])
        self.assertEqual(manifest["runId"], self.result.session_run_id)

    def test_every_exported_csv_leads_with_its_mode_and_run(self):
        for name in ("telemetry.csv", "events.csv"):
            first = (Path(self.result.export_dir) / name).read_text(
                encoding="utf-8").splitlines()[0]
            with self.subTest(file=name):
                self.assertIn("REPLAY", first)
                self.assertIn(self.result.session_run_id, first)

    # -- helpers ------------------------------------------------------------ #

    def reopened(self):
        from gui.operator.store.session_store import SessionStore

        return SessionStore.open(self.result.session_run_dir)


class ScenarioRefusesRatherThanPretends(unittest.TestCase):
    """A source that is not there fails the step it fails on, and says which."""

    def test_a_missing_source_fails_step_three_with_its_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp,
                                      capability_manifest=_manifest())
            scenario = TenStepReplayScenario(
                console, source_path=Path(tmp) / "not-a-source",
                profile_path=PROFILE)
            try:
                with self.assertRaises(ScenarioError) as caught:
                    scenario.run()
            finally:
                console.shutdown()
            self.assertEqual(caught.exception.step_id, "S10-03")
            partial = scenario.result()
            self.assertFalse(partial.ok)
            self.assertEqual([s.step_id for s in partial.steps],
                             ["S10-01", "S10-02"])


if __name__ == "__main__":
    unittest.main()
