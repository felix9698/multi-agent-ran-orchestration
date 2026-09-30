"""Can an operator actually *reach* the Kernel submission path?

The same two-part question ``test_gui_reachability.py`` asks of the Live path,
asked of the new one.  A source that runs a trial and a console nobody can
drive it from is not a submission path, and Gate 3 requires the intent to enter
through the path the finished product uses.

**Wiring** (headless): every control the Contract Studio declares has a button,
the console routes every action those buttons raise, and pressing one with no
Kernel session attached is refused *by name* rather than silently dropped.

**Flow** (real display): the whole submission driven by pressing the actual Tk
buttons -- type the sentence, Draft contract, Review & Confirm, Confirm and
Start -- and then read back what the widgets show.  Nothing in that class calls
a console method a button does not call.  The confirmation *rule* still runs;
only the click on the dialog is supplied, exactly as the Live reachability
suite does it.

The Emergency Stop case is driven the way it actually happens: the button is
pressed while the trial is running, from the thread that owns the widgets,
while the poll loop waits on its next boundary.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path

from assurance.core.axes import TrialOutcome
from assurance.core.states import TrialState

from gui.operator.app import OperatorConsole
from gui.operator.shell.confirm import ConfirmationOutcome
from gui.operator.sources import kernel_live as kl
from gui.operator.workspaces.contract_studio import (
    AXIS_ROWS,
    KERNEL_ACTIONS,
    preview_lines,
    settlement_lines,
)
from tests.assurance.pin_to_cell_support import BASELINE_CONFIG, TARGET_NCI
from tests.gui.kernel_submission_support import (
    HOME_UTTERANCE,
    PIN_UTTERANCE,
    KernelSubmissionFixture,
)

HAS_DISPLAY = bool(os.environ.get("DISPLAY"))


class ContractStudioWiring(unittest.TestCase):
    """Every declared control routes, and every route has a control."""

    def _console(self, tmp) -> OperatorConsole:
        return OperatorConsole(runs_root=tmp)

    def test_the_console_routes_every_action_the_studio_declares(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            console = self._console(tmp)
            try:
                routed = set(console.action_keys())
                for key, label in KERNEL_ACTIONS:
                    with self.subTest(action=key):
                        self.assertIn(key, routed,
                                      f"{key} has a button but no handler")
                        self.assertTrue(label)
            finally:
                console.shutdown()

    def test_every_kernel_route_has_a_declared_control(self) -> None:
        """The other half: a handler no button raises is unreachable."""
        with tempfile.TemporaryDirectory() as tmp:
            console = self._console(tmp)
            try:
                declared = {key for key, _label in KERNEL_ACTIONS}
                routed = {key for key in console.action_keys()
                          if key.startswith("kernel_")}
                self.assertEqual(routed, declared)
            finally:
                console.shutdown()

    def test_the_studio_is_built_with_its_bus_and_its_action_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            console = self._console(tmp)
            try:
                kwargs = console._workspace_kwargs("contract_studio")
                self.assertIs(kwargs["bus"], console.bus)
                self.assertTrue(callable(kwargs["on_action"]))
                workspace = console.workspace("contract_studio")
                self.assertEqual(workspace.title, "Contract Studio")
            finally:
                console.shutdown()

    def test_pressing_a_control_with_no_session_is_refused_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            console = self._console(tmp)
            try:
                for key, _label in KERNEL_ACTIONS:
                    with self.subTest(action=key):
                        console.handle_action(key, PIN_UTTERANCE)
                        console.controller.join_workers(timeout=10)
                        last = console.controller.timeline[-1]
                        self.assertNotEqual(last.title,
                                            f"unknown action {key!r}")
                        self.assertEqual(last.kind, "ACTION_UNSUPPORTED")
                        self.assertIn("no Kernel session", last.title)
            finally:
                console.shutdown()

    def test_attaching_a_session_is_recorded_with_its_mode(self) -> None:
        fixture = KernelSubmissionFixture()
        session = fixture.session()
        with tempfile.TemporaryDirectory() as tmp:
            console = self._console(tmp)
            try:
                console.attach_kernel_session(session)
                last = console.controller.timeline[-1]
                self.assertEqual(last.kind, "KERNEL_SESSION_ATTACHED")
                self.assertIn(kl.MODE_MOCK, last.title)
            finally:
                console.shutdown()

    def test_a_refused_draft_reaches_the_timeline_rather_than_a_traceback(
            self) -> None:
        fixture = KernelSubmissionFixture()
        session = fixture.session()
        with tempfile.TemporaryDirectory() as tmp:
            console = self._console(tmp)
            try:
                console.attach_kernel_session(session)
                console.handle_action("kernel_draft",
                                      "raise throughput to 40 Mbps")
                console.controller.join_workers(timeout=30)
                titles = [event.title for event in console.controller.timeline]
                self.assertTrue(
                    any("INTENT_NOT_RECOGNISED" in title for title in titles),
                    titles)
            finally:
                console.shutdown()

    def test_the_preview_text_names_the_hash_the_confirmation_covers(self) -> None:
        """The dialog and the pane must describe the same content."""
        fixture = KernelSubmissionFixture()
        session = fixture.session()
        preview = session.draft(PIN_UTTERANCE)
        session.confirm()

        lines = preview_lines(session.view())

        joined = "\n".join(lines)
        self.assertIn(preview.content_hash(), joined)
        self.assertIn(kl.ORIGIN_LABELS[kl.ORIGIN_AGENT], joined)
        self.assertIn(kl.ORIGIN_LABELS[kl.ORIGIN_OPERATOR], joined)
        self.assertIn(kl.NORMATIVE, joined)

    def test_the_preview_text_says_when_a_confirmation_was_invalidated(
            self) -> None:
        fixture = KernelSubmissionFixture()
        session = fixture.session()
        session.draft(PIN_UTTERANCE)
        session.confirm()
        session.draft(HOME_UTTERANCE)

        joined = "\n".join(preview_lines(session.view()))

        self.assertIn("confirm again", joined)
        self.assertNotIn(kl.ORIGIN_LABELS[kl.ORIGIN_OPERATOR], joined)

    def test_the_settlement_text_is_labelled_as_the_kernel_speaking(self) -> None:
        fixture = KernelSubmissionFixture()
        session = fixture.session()
        session.draft(PIN_UTTERANCE)
        session.confirm()
        view = session.start()

        joined = "\n".join(settlement_lines(view))

        self.assertIn(kl.ORIGIN_LABELS[kl.ORIGIN_KERNEL], joined)
        self.assertIn(TrialState.SETTLED_SUCCESS.value, joined)
        self.assertNotIn(kl.ORIGIN_LABELS[kl.ORIGIN_AGENT], joined)


@unittest.skipUnless(HAS_DISPLAY, "requires an X display")
class ButtonsDriveTheKernelPath(unittest.TestCase):
    """The whole submission, pressed rather than called."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = KernelSubmissionFixture()
        self.console = OperatorConsole(runs_root=self.tmp.name)
        self.window = self.console.create_window()
        self.window.geometry("1600x1000+0+0")
        # The confirmation rule still runs; only the click is supplied.
        self.console.confirm = lambda _spec: ConfirmationOutcome(True)
        self.pump()

    def tearDown(self) -> None:
        try:
            self.console.shutdown()
        finally:
            try:
                self.window.destroy()
            except Exception:
                pass
            self.tmp.cleanup()

    # -- driving ------------------------------------------------------------

    def pump(self, times: int = 6) -> None:
        for _ in range(times):
            self.window.update_idletasks()
            self.window.update()

    def settle(self) -> None:
        self.console.controller.join_workers(timeout=120)
        self.console.bus.drain(budget=100000)
        self.pump()

    @property
    def studio(self):
        return self.console.workspace("contract_studio")

    def press(self, key: str) -> None:
        button = self.studio.buttons.get(key)
        self.assertIsNotNone(button, f"the studio has no {key} button")
        self.assertEqual(str(button.cget("state")), "normal", key)
        button.invoke()
        self.settle()

    def type_intent(self, text: str) -> None:
        entry = self.studio.intent_entry
        self.assertIsNotNone(entry, "the studio has no intent entry")
        entry.delete(0, "end")
        entry.insert(0, text)
        self.pump()

    def pane_text(self, attribute: str) -> str:
        return getattr(self.studio, attribute).get("1.0", "end")

    # -- the flow -----------------------------------------------------------

    def test_the_operator_can_drive_a_submission_from_the_window(self) -> None:
        session = self.fixture.session(publish=self.console.bus.publish)
        self.console.attach_kernel_session(session)

        self.type_intent(PIN_UTTERANCE)
        self.press("kernel_draft")

        preview = session.preview
        self.assertIsNotNone(preview, "Draft contract produced no preview")
        shown = self.pane_text("_preview_text")
        self.assertIn(preview.candidate_id, shown)
        self.assertIn(preview.content_hash(), shown)
        self.assertIn(kl.DRAFT, shown)
        # The mode badge is on screen and does not claim to be live.
        self.assertIn(kl.MODE_MOCK, self.studio._mode_label.cget("text"))

        self.press("kernel_confirm")
        self.assertIsNotNone(session.confirmed)
        self.assertIn(kl.NORMATIVE, self.pane_text("_preview_text"))

        self.press("kernel_start")

        self.assertEqual(session.view().settlement.outcome,
                         TrialOutcome.SUCCESS.value)
        settlement = self.pane_text("_settlement_text")
        self.assertIn(TrialState.SETTLED_SUCCESS.value, settlement)
        self.assertIn("CLOSED_PASS", settlement)
        self.assertEqual(self.fixture.adapter.snapshot(),
                         {"servingCell": str(TARGET_NCI)})
        # Four axes, four rows, each with its own value on screen.
        for field, _label in AXIS_ROWS:
            with self.subTest(axis=field):
                text = self.studio._axis_labels[field].cget("text")
                self.assertIn(getattr(session.view().axes, field), text)

    def test_starting_before_confirming_is_refused_at_the_button(self) -> None:
        session = self.fixture.session(publish=self.console.bus.publish)
        self.console.attach_kernel_session(session)

        self.type_intent(PIN_UTTERANCE)
        self.press("kernel_draft")
        self.press("kernel_start")

        self.assertIsNone(session.trial_id)
        titles = [event.title for event in self.console.controller.timeline]
        self.assertTrue(any("CONFIRMATION_REQUIRED" in title
                            for title in titles), titles)

    def test_a_changed_contract_needs_a_new_click_before_it_will_start(
            self) -> None:
        session = self.fixture.session(publish=self.console.bus.publish)
        self.console.attach_kernel_session(session)

        self.type_intent(PIN_UTTERANCE)
        self.press("kernel_draft")
        self.press("kernel_confirm")
        self.type_intent(HOME_UTTERANCE)
        self.press("kernel_draft")
        self.press("kernel_start")

        self.assertIsNone(session.trial_id)
        self.assertIn("confirm again", self.pane_text("_preview_text"))

        self.press("kernel_confirm")
        self.press("kernel_start")
        self.assertIsNotNone(session.trial_id)

    def test_the_emergency_stop_button_stops_a_running_trial(self) -> None:
        """Pressed on the Tk thread while the poll loop waits at a boundary."""
        reached = threading.Event()
        released = threading.Event()

        def publish(channel, payload):
            self.console.bus.publish(channel, payload)
            if (getattr(payload, "stage", None) == kl.STAGE_OBSERVING
                    and getattr(payload, "poll_count", 0) == 2):
                reached.set()
                released.wait(60)

        session = self.fixture.session(publish=publish)
        self.console.attach_kernel_session(session)
        self.type_intent(PIN_UTTERANCE)
        self.press("kernel_draft")
        self.press("kernel_confirm")

        # Start without settling: the loop must still be running when the stop
        # is pressed, which is the only situation this control exists for.
        self.studio.buttons["kernel_start"].invoke()
        self.assertTrue(reached.wait(60), "the poll loop never reached poll 2")
        self.studio.buttons["kernel_estop"].invoke()
        released.set()
        self.settle()

        view = session.view()
        self.assertEqual(view.settlement.outcome,
                         TrialOutcome.OPERATOR_ABORTED.value)
        self.assertEqual(dict(self.fixture.adapter.snapshot()),
                         dict(BASELINE_CONFIG))
        kinds = [event.kind for event in self.console.controller.timeline]
        self.assertIn("OPERATOR_EMERGENCY_STOP", kinds)
        # The stop is shown as the Kernel's, with the recovery that made it a
        # terminal rather than a request: safe state, reread, confirm.  For a
        # steering objective the contracted safe state *is* the baseline, so
        # there is no separate reversal to drive (SEAMS-GATE2 section 8.3).
        shown = "\n".join(settlement_lines(view))
        self.assertIn("OPERATOR_ABORT", shown)
        for operation in ("EMERGENCY_SAFE_STATE", "CONFIGURATION_REREAD",
                          "RECOVERY_CONFIRM"):
            with self.subTest(operation=operation):
                self.assertIn(operation, shown)

    def test_no_pane_control_can_edit_what_the_kernel_settled(self) -> None:
        """Task section 9.9, at the widget: both panes are read-only."""
        session = self.fixture.session(publish=self.console.bus.publish)
        self.console.attach_kernel_session(session)
        self.type_intent(PIN_UTTERANCE)
        self.press("kernel_draft")
        self.press("kernel_confirm")
        self.press("kernel_start")

        for attribute in ("_preview_text", "_settlement_text"):
            with self.subTest(pane=attribute):
                widget = getattr(self.studio, attribute)
                self.assertEqual(str(widget.cget("state")), "disabled")
        entries = [child for child in self.studio.frame.winfo_children()
                   if child.winfo_class() == "Entry"]
        self.assertEqual(entries, [],
                         "the only Entry in this pane is the intent field")


if __name__ == "__main__":
    unittest.main()
