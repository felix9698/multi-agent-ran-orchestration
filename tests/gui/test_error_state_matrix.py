"""The required error modes remain visibly distinct in presentation output.

Two layers.  T4's original test pins the *presentation* rule: the ten states
section 10 names must not collapse onto one another in the Demo View's label,
and must be distinguishable without colour.

The second class, added at integration, drives the **real console** into each
state and asserts what an operator would actually be able to tell apart -
because two distinct labels are worth nothing if nothing ever puts the console
into the state that produces them.  Each case names the state, the path that
reaches it, and the observable that separates it from the case above.
"""

import json
import tempfile
import unittest
from pathlib import Path

from gui.operator import status as st
from gui.operator.app import OperatorConsole
from gui.operator.session.controller import SessionError
from gui.operator.session.scenario import _episode_as_decision_input
from gui.operator.shell.confirm import evaluate_confirmation
from gui.operator.sources.live import project_decision
from gui.operator.store.session_store import SessionStore
from gui.operator.workspaces.demo import demo_state_label

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "gui" / "fixtures"
CAPTURE = FIXTURES / "lo1-capture-min"
PROFILE = FIXTURES / "profile-min.json"
MANIFEST = FIXTURES / "capability-manifest-min.json"


def _manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


class ErrorStateMatrixTests(unittest.TestCase):
    def test_required_outcomes_have_distinct_non_success_labels(self):
        cases = {
            "NORMAL": ("OK", "COMPLETED"),
            "STALE_DATA": ("STALE", "COMPLETED"),
            "PARTIAL_DATA": ("UNAVAILABLE", "COMPLETED"),
            "CONNECTION_LOST": ("UNAVAILABLE", "RUNNING"),
            "SCHEMA_MISMATCH": ("ERROR", "FAILED"),
            "REJECTED_INTENT": ("BLOCKED", "COMPLETED"),
            "NEGOTIATION": ("DEGRADED", "RUNNING"),
            "TECHNICAL_FAILSAFE": ("ERROR", "FAILED"),
            "USER_ABORT": ("UNKNOWN", "ABORTED"),
            "CALLBACK_FAILURE": ("UNKNOWN", "RUNNING"),
        }
        labels = {name: demo_state_label(*values, case=name) for name, values in cases.items()}
        self.assertEqual(labels["NORMAL"], "● OK — COMPLETED")
        self.assertIn("Stale", labels["STALE_DATA"])
        self.assertIn("ABORTED", labels["USER_ABORT"])
        self.assertEqual(len(set(labels.values())), len(labels))


class ConsoleReachesEachStateAndSaysWhich(unittest.TestCase):
    """The states of section 10, produced through the console's own paths."""

    def _console(self, tmp):
        return OperatorConsole(runs_root=tmp, capability_manifest=_manifest())

    def _started_replay_session(self, tmp):
        """A console with a Replay source attached and a session recording."""
        console = self._console(tmp)
        console.controller.load_profile(PROFILE)
        console.controller.preflight(capability_manifest=_manifest(),
                                     runs_root=tmp)
        console.attach_replay_source(CAPTURE)
        mode, evidence = console.session_mode_evidence()
        console.controller.start(mode=mode, mode_evidence=evidence,
                                 runs_root=tmp)
        return console

    # -- normal ------------------------------------------------------------- #

    def test_normal_completes_and_is_the_only_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = self._started_replay_session(tmp)
            try:
                console.controller.stop("COMPLETED")
                store = SessionStore.open(
                    Path(tmp) / console.controller.run_id)
                self.assertEqual(store.disposition, "COMPLETED")
                self.assertTrue(store.is_success)
            finally:
                console.shutdown()

    # -- stale and partial data --------------------------------------------- #

    def test_stale_and_partial_data_are_two_different_statements(self):
        """Both keep the run; neither is allowed to read as a clean result."""
        with tempfile.TemporaryDirectory() as tmp:
            console = self._started_replay_session(tmp)
            try:
                store = console.controller._store
                store.record_issue("STALE_DATA",
                                   "the last O1 observation is 4 minutes old")
                store.record_issue("PARTIAL_DATA",
                                   "DRB.UEThpDl was subscribed and never "
                                   "delivered")
                console.controller.stop("COMPLETED")
                reopened = SessionStore.open(
                    Path(tmp) / console.controller.run_id)
            finally:
                console.shutdown()
            kinds = [issue["kind"] for issue in reopened.data_issues()]
            self.assertIn("STALE_DATA", kinds)
            self.assertIn("PARTIAL_DATA", kinds)
            self.assertNotEqual(kinds[0], kinds[1])
            for issue in reopened.data_issues():
                self.assertTrue(issue["detail"],
                                "an issue without a reason teaches nothing")
            # The run still completed.  The issues travel *with* the result
            # rather than replacing it, and the export carries them.
            self.assertTrue(reopened.is_success)
            self.assertTrue(reopened.read_manifest()["dataIssues"])

    def test_a_stale_status_never_resolves_to_ok(self):
        self.assertEqual(st.resolve("STALE").status, "STALE")
        self.assertNotEqual(st.resolve("STALE").status, st.OK)

    # -- connection failure -------------------------------------------------- #

    def test_a_connection_failure_is_unavailable_with_its_cause(self):
        """A transport that raises is UNAVAILABLE and names the exception.

        Distinct from UNSUPPORTED, which is what an absent client produces:
        "the deployment does not offer this" and "it is offered and did not
        answer" are different facts and the operator has to be able to tell.
        """
        class Unreachable:
            def bootstrap_info(self):
                raise ConnectionRefusedError("nonrt-ric refused the connection")

        with tempfile.TemporaryDirectory() as tmp:
            console = self._console(tmp)
            console.r1_client = Unreachable()
            try:
                results = console.controller.preflight(
                    r1_client=console.r1_client, runs_root=tmp)
            finally:
                console.shutdown()
            check = next(v for v in results if v.check_id == "PF-R1-BOOTSTRAP")
            self.assertEqual(check.status, st.UNAVAILABLE)
            self.assertIn("refused the connection", check.reason)

        with tempfile.TemporaryDirectory() as tmp:
            console = self._console(tmp)
            try:
                results = console.controller.preflight(runs_root=tmp)
            finally:
                console.shutdown()
            check = next(v for v in results if v.check_id == "PF-R1-BOOTSTRAP")
            self.assertEqual(check.status, st.UNSUPPORTED)

    def test_a_connection_failure_cannot_be_promoted_into_a_live_session(self):
        """The failure mode that would matter most: LIVE on a dead transport."""
        class Unreachable:
            def bootstrap_info(self):
                raise ConnectionRefusedError("no route")

        with tempfile.TemporaryDirectory() as tmp:
            console = self._console(tmp)
            console.r1_client = Unreachable()
            try:
                console.controller.preflight(r1_client=console.r1_client,
                                             runs_root=tmp)
                with self.assertRaises(SessionError):
                    console.session_mode_evidence()
            finally:
                console.shutdown()

    # -- rejected intent, negotiation, technical failsafe -------------------- #

    def _decision_for(self, **episode):
        document = _episode_as_decision_input(episode, intent_text="an intent")
        return project_decision(document, intent_text="an intent")

    def test_the_three_eq12_terminals_are_three_different_screens(self):
        admitted = self._decision_for(
            episodeId="e-1", fsmPath=["S0", "S1", "S2", "S3", "S4", "S6"],
            terminalOutcome="commit_original", success=True)
        rejected = self._decision_for(
            episodeId="e-2", fsmPath=["S0", "S1", "S2", "S6"],
            terminalOutcome="pending_not_admitted",
            terminalReason="no_acceptable_alternative", success=False)
        failsafe = self._decision_for(
            episodeId="e-3", fsmPath=["S0", "S1", "S2", "S3",
                                      "S_TECHNICAL_FAILSAFE"],
            terminalOutcome="technical_failsafe",
            terminalReason="rollback_failed", success=False)

        self.assertEqual(admitted.eq12_state, "Admitted")
        self.assertEqual(rejected.eq12_state, "NotAdmitted")
        self.assertEqual(failsafe.eq12_state, "TechnicalFailsafe")
        self.assertEqual(len({admitted.eq12_state, rejected.eq12_state,
                              failsafe.eq12_state}), 3)

        # A rejection carries its reason; a failsafe carries a different one.
        self.assertEqual(rejected.terminal_reason,
                         "no_acceptable_alternative")
        self.assertEqual(failsafe.terminal_reason, "rollback_failed")

        # And the failsafe path is visible as a stage, not only as a word.
        states = {stage.stage_id: stage.state for stage in failsafe.fsm_stages}
        self.assertEqual(states.get("S_TECHNICAL_FAILSAFE"), "FAILED")
        self.assertNotIn("S_TECHNICAL_FAILSAFE",
                         {s.stage_id for s in admitted.fsm_stages})

    def test_a_negotiated_episode_shows_s5_and_its_round_count(self):
        negotiated = self._decision_for(
            episodeId="e-4",
            fsmPath=["S0", "S1", "S2", "S3", "S4", "S5", "S3", "S4", "S6"],
            terminalOutcome="commit_alternative", negotiationRounds=2,
            rolledBack=True, success=True)
        states = {stage.stage_id: stage.state
                  for stage in negotiated.fsm_stages}
        self.assertEqual(states["S5"], "DONE")
        self.assertEqual(negotiated.negotiation_rounds, 2)
        self.assertTrue(negotiated.rolled_back)

        straight = self._decision_for(
            episodeId="e-5", fsmPath=["S0", "S1", "S2", "S3", "S4", "S6"],
            terminalOutcome="commit_original", success=True)
        self.assertEqual(
            {s.stage_id: s.state for s in straight.fsm_stages}["S5"],
            "PENDING",
            "an episode that never negotiated must not show S5 as done")

    # -- user abort ---------------------------------------------------------- #

    def test_a_user_abort_keeps_the_data_and_refuses_to_be_a_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = self._started_replay_session(tmp)
            run_id = console.controller.run_id
            try:
                console.controller.record_event(lane="SESSION", kind="PROBE")
                spec = console.controller.abort_confirmation()
                # A typed phrase, not a click: this is an irreversible action.
                self.assertEqual(spec.acknowledgement, "TYPED_CONFIRM")
                self.assertEqual(spec.typed_phrase, "ABORT")
                self.assertFalse(
                    evaluate_confirmation(spec, acknowledged=True,
                                          typed="abort").confirmed,
                    "a near miss must be refused, or the typed form is theatre")
                self.assertTrue(
                    evaluate_confirmation(spec, acknowledged=True,
                                          typed="ABORT").confirmed)
                self.assertIn("the run is never reported as a success",
                              spec.effects)
                console.controller.abort()
            finally:
                console.shutdown()

            reopened = SessionStore.open(Path(tmp) / run_id)
            self.assertEqual(reopened.disposition, "ABORTED")
            self.assertFalse(reopened.is_success)
            kinds = [event.get("kind") for event in reopened.read_events()]
            self.assertIn("PROBE", kinds,
                          "an abort preserves what was captured before it")

    def test_an_unconfirmed_abort_does_not_happen(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = self._started_replay_session(tmp)
            try:
                console.confirm = lambda spec: evaluate_confirmation(
                    spec, acknowledged=False)
                console.handle_action("abort")
                console.controller.join_workers(timeout=5)
                self.assertEqual(console.controller.disposition, "RUNNING")
                kinds = [e.kind for e in console.controller.timeline]
                self.assertIn("ACTION_CANCELLED", kinds)
            finally:
                console.shutdown()


if __name__ == "__main__":
    unittest.main()
