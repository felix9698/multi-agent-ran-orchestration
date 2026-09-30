"""Can an operator actually *reach* the Live path from the window?

The Live composition was assembled and tested through the console's methods -
``attach_live_from_profile``, ``attach_replay_source``, ``select_mode`` - and
every one of those tests passed while the real window had no control that
called any of them.  A capability only a test can invoke is not a capability the
operator has, and a test suite that can only reach it by calling the method is
the reason nobody noticed.

So this module asserts two different things, and needs both:

**Wiring** (headless): every action a workspace declares has a button, and the
console routes every action a button raises.  Either half alone is satisfiable
by a console that does nothing - a button wired to a missing handler, or a
handler no button calls.

**Flow** (real display): the whole session lifecycle driven by pressing the
actual Tk buttons - profile load, bind, preflight, select Live, start, submit,
export, stop, disconnect, load a Replay source, select Replay, start again.
Nothing here calls a console method that a button does not call, which is the
property that was missing.  File dialogs are stubbed the way a user filling one
in would answer them; the confirmation *rule* still runs, only the click is
supplied.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gui.operator.app import LOOPBACK_DEPLOYMENT_ENV, OperatorConsole
from gui.operator.workspaces.live_ops import SOURCE_ACTIONS, WORKFLOW_ACTIONS
from tests.gui.test_live_composition import (INTEGRATION_VALUES, INTENT_TEXT,
                                             LiveDeploymentCase,
                                             _legacy_console, _manager,
                                             _write_profile)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "gui" / "fixtures"
CAPTURE = FIXTURES / "lo1-capture-min"
HAS_DISPLAY = bool(os.environ.get("DISPLAY"))

#: The controls this campaign added, and what each is for.  Named here so the
#: wiring test fails if one is quietly dropped from the workspace again.
REQUIRED_SOURCE_ACTIONS = {
    "bind_live": "Bind Live deployment",
    "mode_live": "Select Live",
    "load_replay": "Load Replay capture/run",
    "mode_replay": "Select Replay",
    "disconnect": "Disconnect",
}


class ActionWiring(unittest.TestCase):
    """Every declared control routes, and every route has a control."""

    def _console(self, tmp):
        return OperatorConsole(runs_root=tmp)

    def test_the_source_row_declares_every_control_this_console_needs(self):
        declared = dict(SOURCE_ACTIONS)
        for key, label in REQUIRED_SOURCE_ACTIONS.items():
            self.assertIn(key, declared, f"{key} has no control")
            self.assertEqual(declared[key], label)

    def test_the_console_routes_every_action_a_button_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = self._console(tmp)
            try:
                routed = set(console.action_keys())
                for key, _label in WORKFLOW_ACTIONS + SOURCE_ACTIONS:
                    self.assertIn(key, routed,
                                  f"{key} has a button but no handler")
                    console.handle_action(key, "")
                    last = console.controller.timeline[-1]
                    self.assertNotEqual(last.title, f"unknown action {key!r}")
            finally:
                console.shutdown()

    def test_an_unrouted_action_is_still_refused_by_name(self):
        """The negative control for the assertion above."""
        with tempfile.TemporaryDirectory() as tmp:
            console = self._console(tmp)
            try:
                console.handle_action("bind_the_gnb", "")
                self.assertEqual(console.controller.timeline[-1].title,
                                 "unknown action 'bind_the_gnb'")
            finally:
                console.shutdown()

    def test_the_intent_and_analysis_panes_are_built_with_their_callbacks(self):
        """The bug that made every Intent & Decision button inert."""
        with tempfile.TemporaryDirectory() as tmp:
            console = self._console(tmp)
            try:
                intent = console._workspace_kwargs("intent_decision")
                for name in ("on_preview", "on_submit", "on_withdraw",
                             "on_model_switch", "on_model_refresh"):
                    self.assertTrue(callable(intent[name]), name)
                analysis = console._workspace_kwargs("analysis")
                for name in ("on_export", "on_load_run"):
                    self.assertTrue(callable(analysis[name]), name)
                # And the constructed objects really took them.
                self.assertIsNotNone(
                    console.workspace("analysis").on_load_run)
                self.assertIsNotNone(console.workspace("analysis").on_export)
            finally:
                console.shutdown()

    def test_binding_needs_a_profile_that_names_a_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = self._console(tmp)
            try:
                console.handle_action("bind_live", "")
                last = console.controller.timeline[-1]
                self.assertEqual(last.kind, "ACTION_UNSUPPORTED")
                self.assertIn("integrationValuesPath", last.title)
                self.assertIsNone(console.live)
            finally:
                console.shutdown()


@unittest.skipUnless(HAS_DISPLAY, "requires an X display")
class ButtonsReachTheLivePath(LiveDeploymentCase):
    """The whole lifecycle, pressed rather than called."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=self.root))
        self.runs = self.tmp / "runs"
        self.profile_path = _write_profile(self.tmp / "profile.json",
                                           runs_root=self.runs)
        # The preserved episode runtime, handed in the way a console can only
        # get one since the B-01 cutover.  This class drives the *legacy* Live
        # flow end to end; ``MainEntryBuildsTheSameConsole`` below drives what
        # ``main.py`` builds, which is handed nothing and refuses to bind.
        self.console = _legacy_console(runs_root=str(self.runs))
        self.window = self.console.create_window()
        self.window.geometry("1600x1000+0+0")
        # The confirmation rule still runs; only the click is supplied.
        self.console.confirm = lambda spec: type(
            "Outcome", (), {"confirmed": True})()
        self.steps: list = []
        self.pump()

    def tearDown(self):
        try:
            self.console.shutdown()
        finally:
            try:
                self.window.destroy()
            except Exception:
                pass

    # -- driving ------------------------------------------------------------ #

    def pump(self, times: int = 6) -> None:
        for _ in range(times):
            self.window.update_idletasks()
            self.window.update()

    def settle(self) -> None:
        self.console.controller.join_workers(timeout=120)
        self.console.bus.drain(budget=100000)
        self.pump()

    def press(self, workspace_id: str, key: str) -> None:
        """Press the real button, through the real widget."""
        workspace = self.console.workspace(workspace_id)
        button = workspace.buttons.get(key)
        self.assertIsNotNone(button, f"{workspace_id} has no {key} button")
        self.assertEqual(str(button.cget("state")), "normal", key)
        button.invoke()
        self.settle()
        self.steps.append(key)

    def press_footer(self, key: str, intent: str = "") -> None:
        button = self.console.footer._buttons.get(key)
        self.assertIsNotNone(button, f"the footer has no {key} button")
        if intent:
            self.console.footer.entry.delete(0, "end")
            self.console.footer.entry.insert(0, intent)
        button.invoke()
        self.settle()
        self.steps.append(f"footer:{key}")

    def type_into(self, workspace_id: str, attribute: str, text: str) -> None:
        entry = getattr(self.console.workspace(workspace_id), attribute)
        self.assertIsNotNone(entry, f"{workspace_id}.{attribute} is missing")
        entry.delete(0, "end")
        entry.insert(0, text)
        self.pump()

    # -- the flow ----------------------------------------------------------- #

    def test_the_operator_can_drive_the_whole_flow_from_the_window(self):
        console, controller = self.console, self.console.controller

        with mock.patch("tkinter.filedialog.askopenfilename",
                        return_value=str(self.profile_path)):
            self.press("live_ops", "profile_load")
        self.assertEqual(console.profile.profile_id, "live-mock")
        self.assertEqual(controller.state().mode, "DISCONNECTED",
                         "loading a profile must not connect to anything")
        self.assertIsNone(console.live)

        # Bind: names the deployment, contacts nothing.
        with mock.patch.dict(os.environ, {LOOPBACK_DEPLOYMENT_ENV: "1"}):
            self.press("live_ops", "bind_live")
        self.assertIsNotNone(console.live, "Bind did not bind the deployment")
        self.assertEqual(controller.state().mode, "DISCONNECTED",
                         "Bind must not open a session or a connection")
        self.assertEqual(controller.preflight_results, (),
                         "Bind must not contact the deployment; only Preflight "
                         "does")
        identity = console.live.identity()
        self.assertTrue(identity["capabilityManifestId"])
        console.live.llm_manager = _manager()

        # Selecting Live before Preflight is refused: no transport was seen.
        self.press("live_ops", "mode_live")
        self.press_footer("start")
        self.assertNotEqual(controller.disposition, "RUNNING")

        self.press_footer("preflight")
        checks = {v.check_id: v for v in controller.preflight_results}
        self.assertEqual(checks["PF-R1-BOOTSTRAP"].status, "OK")

        self.press("live_ops", "mode_live")
        self.assertEqual(console.requested_mode, "LIVE")
        self.press_footer("start")
        self.assertEqual(controller.state().mode, "LIVE")
        self.assertEqual(controller.disposition, "RUNNING")
        live_run = controller.run_id

        self.press_footer("submit", intent=INTENT_TEXT)
        self.assertEqual(console.live.submissions, 1)
        decision = controller.state().decision
        self.assertIsNotNone(decision)
        self.assertIn(decision.eq12_state,
                      ("Admitted", "NotAdmitted", "TechnicalFailsafe"))
        self.assertTrue(controller.state().intents)

        # Section 4.7: one correlation id makes eight items traceable, read
        # from the same published state a workspace renders from - not from
        # calling the projection directly.
        trace = controller.state().correlation_trace
        self.assertIsNotNone(trace, "no correlation trace was published")
        self.assertTrue(trace.correlation_id)
        self.assertEqual(trace.intent_status, "OK")               # 1
        self.assertEqual(len(trace.llm_stages), 3)                 # 2
        self.assertIn(trace.verdict_detail,
                      ("Admitted", "NotAdmitted", "TechnicalFailsafe"))
        self.assertTrue(trace.policy_id)                           # 3
        self.assertEqual(trace.policy_identity_status, "OK")
        self.assertIn(trace.policy_lifecycle_status,               # 4
                      ("OK", "DEGRADED", "STALE", "UNKNOWN", "ERROR"))
        self.assertTrue(trace.ue_id or trace.target_status == "UNKNOWN")  # 5
        self.assertIn(trace.e2_status, ("OK", "UNKNOWN"))          # 6
        if trace.e2_status == "UNKNOWN":
            self.assertTrue(trace.e2_reason)
        self.assertIn(trace.assurance_status, ("OK", "UNKNOWN"))   # 7
        self.assertEqual(trace.eq12_state, decision.eq12_state)    # 8

        # The same eight items are on screen, not only on the bus: the
        # Intent & Decision workspace's trace panel renders this state and
        # nothing else.
        panel = console.workspace("intent_decision").correlation
        self.assertIn(trace.correlation_id, panel._vars["correlation_id"].get())
        self.assertIn(str(trace.policy_id), panel._vars["policy_identity"].get())

        self.press("analysis", "export")
        exported = [e for e in controller.timeline
                    if e.kind == "EXPORT_WRITTEN"]
        self.assertTrue(exported, "the Export button produced no export")
        export_dir = Path(exported[-1].title)
        self.assertTrue((export_dir / "EXPORT-MANIFEST.json").is_file())
        self.assertEqual(
            json.loads((export_dir / "OPERATOR-CONTEXT.json")
                       .read_text(encoding="utf-8"))["mode"], "LIVE")

        self.press_footer("stop")
        self.assertEqual(controller.disposition, "COMPLETED")

        self.press("live_ops", "disconnect")
        self.assertIsNone(console.live)
        self.assertEqual(controller.state().mode, "DISCONNECTED")

        # Replay, on the same window, from the same row.
        self.type_into("live_ops", "source_path", str(CAPTURE))
        self.press("live_ops", "load_replay")
        self.assertTrue(console.replay_source, "the capture was not attached")
        self.press("live_ops", "mode_replay")
        self.press_footer("preflight")
        self.press_footer("start")
        state = controller.state()
        self.assertEqual(state.mode, "REPLAY")
        self.assertFalse(state.is_live)
        self.assertNotEqual(controller.run_id, live_run)
        # And the Live session's rows did not follow it here.
        self.assertEqual(state.intents, ())
        self.assertIsNone(state.decision)
        controller.stop("COMPLETED")

        self.assertEqual(self.steps[:3],
                         ["profile_load", "bind_live", "mode_live"])

    def test_opening_a_run_in_analysis_is_not_attaching_a_replay_source(self):
        """Requirement 3, asserted as the difference it actually makes."""
        console, controller = self.console, self.console.controller

        # A finalized Replay run to open.  It is produced through the same
        # buttons, so the fixture is not a shortcut either.
        self.type_into("live_ops", "source_path", str(CAPTURE))
        self.press("live_ops", "load_replay")
        self.press("live_ops", "mode_replay")
        self.press_footer("preflight")
        self.press_footer("start")
        run_dir = str(controller._store.run_dir)
        self.press_footer("stop")
        console.handle_action("disconnect", "")
        self.settle()
        self.assertIsNone(console.replay_source)

        # Opening it in Analysis charts it and changes no session state.
        self.type_into("analysis", "run_path", run_dir)
        self.press("analysis", "load_run")
        loaded = [e for e in controller.timeline if e.kind == "RUN_LOADED"]
        self.assertTrue(loaded, "the run was not opened in Analysis")
        self.assertIsNone(console.replay_source,
                          "opening a run for viewing must not make it the "
                          "session source")
        self.assertIsNone(console.requested_mode)
        self.assertEqual(controller.state().mode, "DISCONNECTED")
        charted = console.workspace("analysis").model.primary
        self.assertIsNotNone(charted, "the run was not charted")
        self.assertEqual(charted.run_id, Path(run_dir).name)

        # The same path handed to the *other* action is refused with its
        # reason: a session run directory is something Analysis can chart, not
        # a recorded source a session can be replayed from.  Two actions, two
        # answers, and neither pretends to be the other.
        self.type_into("live_ops", "source_path", run_dir)
        self.press("live_ops", "load_replay")
        self.assertIsNone(console.replay_source)
        refusal = console.controller.timeline[-1]
        self.assertEqual(refusal.severity, "ERROR")
        self.assertIn("neither a capture document nor an experiments-runner",
                      refusal.title)

        # A recorded source is what that action takes, and it does become the
        # session's source - which the Analysis one never does.
        self.type_into("live_ops", "source_path", str(CAPTURE))
        self.press("live_ops", "load_replay")
        self.assertTrue(console.replay_source)
        self.assertTrue(console.replay_source["sourceRunId"])
        self.press("live_ops", "mode_replay")
        self.assertEqual(console.requested_mode, "REPLAY")

    def test_every_declared_control_exists_as_a_button_on_screen(self):
        live_ops = self.console.workspace("live_ops")
        for key, label in WORKFLOW_ACTIONS + SOURCE_ACTIONS:
            with self.subTest(action=key):
                button = live_ops.buttons.get(key)
                self.assertIsNotNone(button, f"{key} has no button")
                self.assertEqual(str(button.cget("text")), label)
                self.assertTrue(button.winfo_exists())
        analysis = self.console.workspace("analysis")
        for key in ("load_run", "export"):
            self.assertIsNotNone(analysis.buttons.get(key), key)


class MainEntryBuildsTheSameConsole(unittest.TestCase):
    """``python3 main.py --profile ...`` lands on the console under test.

    Rewritten at the B-01 cutover, and narrowed by it.  The launcher used to be
    handed a coordinator and to start it; it now composes the console alone,
    and what this asserts is that the console an operator actually gets is the
    same object every test above drives - same profile, same handler table,
    same Disconnected start - with no episode runtime behind it and nothing
    contacted.  No display is needed for that, because ``build_console``
    touches no toolkit.
    """

    def test_the_launcher_builds_the_console_the_buttons_belong_to(self):
        import main

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            profile_path = _write_profile(Path(tmp) / "profile.json",
                                          runs_root=runs)
            console = main.build_console(profile_path=str(profile_path),
                                         runs_root=str(runs))
            try:
                self.assertEqual(console.profile.profile_id, "live-mock")
                self.assertEqual(console.profile.integration_values_path,
                                 str(INTEGRATION_VALUES))
                # The launcher's console reaches the same action the window's
                # Bind button raises - it is the same handler table.
                self.assertIn("bind_live", console.action_keys())
                self.assertIsNone(console.live)
                self.assertIsNone(console.legacy_episode)
                self.assertIsNone(console.kernel_session)
                self.assertEqual(console.controller.state().mode,
                                 "DISCONNECTED")
            finally:
                console.shutdown()

    def test_pressing_bind_on_it_refuses_instead_of_running_an_episode(self):
        """The whole point of the cutover, at the button the operator presses."""
        import main

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            profile_path = _write_profile(Path(tmp) / "profile.json",
                                          runs_root=runs)
            console = main.build_console(profile_path=str(profile_path),
                                         runs_root=str(runs))
            try:
                console.handle_action("bind_live", "")
                console.controller.join_workers(timeout=30)
                console.bus.drain(budget=10000)
                self.assertIsNone(console.live)
                warnings = [event for event in console.controller.timeline
                            if event.severity in ("WARNING", "ERROR")]
                self.assertTrue(warnings, "binding neither bound nor refused")
                self.assertTrue(
                    any("Kernel" in (event.title or "") for event in warnings),
                    [event.title for event in warnings])
            finally:
                console.shutdown()


if __name__ == "__main__":
    unittest.main()
