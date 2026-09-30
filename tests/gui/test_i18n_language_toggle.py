"""The corner language control: Korean display, exact English round trip.

The property that matters is the same one the reachability suite guards: the
control must be the real widget in the real window, not a method only a test
can call.  So the display tests drive the actual combobox.  The boundary
properties are just as load-bearing:

* English is the default and nothing changes until the operator chooses.
* Contract vocabulary (mode tokens, Eq.12 outcomes, S0-S6) is never in the
  catalog - what an operator quotes to the lower researcher stays canonical.
* Switching back restores every widget byte-for-byte.
"""

import os
import tempfile
import unittest

from gui.operator.app import OperatorConsole
from gui.operator.i18n import CATALOG, ENGLISH, KOREAN, LanguageSwitch

HAS_DISPLAY = bool(os.environ.get("DISPLAY"))


class CatalogStaysOutOfTheContract(unittest.TestCase):
    """Headless: the catalog must not rename semantic tokens."""

    def test_contract_vocabulary_is_not_translated(self):
        for token in ("DISCONNECTED", "LIVE", "REPLAY", "Admitted",
                      "NotAdmitted", "TechnicalFailsafe", "RUNNING",
                      "COMPLETED", "OK", "S0", "S6"):
            self.assertNotIn(token, CATALOG)

    def test_every_entry_actually_changes_the_text(self):
        for english, korean in CATALOG.items():
            self.assertTrue(korean.strip(), english)
            self.assertNotEqual(english, korean)

    def test_headless_switch_is_a_safe_no_op(self):
        switch = LanguageSwitch()
        switch.set_language(KOREAN)
        self.assertEqual(switch.language, KOREAN)
        switch.set_language(ENGLISH)
        with self.assertRaises(ValueError):
            switch.set_language("fr")


@unittest.skipUnless(HAS_DISPLAY, "requires an X display")
class TheCornerControlSwitchesTheDisplay(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.console = OperatorConsole(runs_root=self._tmp.name)
        self.root = self.console.create_window()
        self.addCleanup(self.root.destroy)
        self.pump()

    def pump(self, times: int = 6) -> None:
        for _ in range(times):
            self.root.update_idletasks()
            self.root.update()

    def choose(self, display_name: str) -> None:
        """Drive the real combobox, not the switch API."""
        control = self.console.language.control
        self.assertIsNotNone(control, "the corner control was not attached")
        self.console.language._var.set(display_name)
        control.event_generate("<<ComboboxSelected>>")
        self.pump()

    def dump(self) -> dict:
        notebook = self.console.window.notebook
        live_ops = self.console.workspace("live_ops")
        table = self.console.workspace("intent_decision").table.tree
        return {
            "tabs": tuple(notebook.tab(t, "text") for t in notebook.tabs()),
            "submit": str(self.console.footer._buttons["submit"].cget("text")),
            "bind": str(live_ops.buttons["bind_live"].cget("text")),
            "heading": str(table.heading("id", "text")),
        }

    def test_english_is_the_default_and_the_control_exists(self):
        self.assertEqual(self.console.language.language, ENGLISH)
        before = self.dump()
        self.assertIn("Live Operations", before["tabs"])
        self.assertEqual(before["submit"], "Submit Intent")
        self.assertEqual(before["bind"], "Bind Live deployment")

    def test_choosing_korean_translates_the_static_display(self):
        self.choose("한국어")
        after = self.dump()
        self.assertIn("라이브 운영", after["tabs"])
        self.assertEqual(after["submit"], "인텐트 제출")
        self.assertEqual(after["bind"], "라이브 배치 바인딩")
        self.assertEqual(after["heading"], "인텐트 ID")

    def test_round_trip_restores_english_exactly(self):
        before = self.dump()
        self.choose("한국어")
        self.assertNotEqual(before, self.dump())
        self.choose("English")
        self.assertEqual(before, self.dump())

    def test_a_repaint_in_english_is_retranslated_on_the_next_tick(self):
        self.choose("한국어")
        button = self.console.footer._buttons["submit"]
        button.configure(text="Submit Intent")   # what a repaint would do
        self.console.language.refresh()          # what the tick hook does
        self.assertEqual(str(button.cget("text")), "인텐트 제출")

    def test_korean_leaves_mode_tokens_canonical(self):
        self.choose("한국어")
        state = self.console.controller.state()
        self.assertEqual(state.mode, "DISCONNECTED",
                         "the session state itself must never be localised")


if __name__ == "__main__":
    unittest.main()
