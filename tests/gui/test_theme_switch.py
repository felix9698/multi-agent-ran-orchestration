"""The display-theme toggle: the maps, and the real console it repaints.

Two halves, matching the house pattern in ``test_cockpit_workspaces``: the
colour arithmetic is hermetic and always runs, and the half that builds real
Tk widgets is skipped without a display.

The assertions are deliberately about *failure modes a screenshot would not
catch*.  A theme switch is easy to make look right in one capture and be
broken in use: text the colour of its own background, a value quietly changed
along with a colour, a tick hook that undoes the switch a second later, or a
console that stops accepting input after the repaint.  Each has its own test.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from gui.operator import status as st
from gui.operator import theming, tokens

HAS_DISPLAY = bool(os.environ.get("DISPLAY"))


class ColourMaps(unittest.TestCase):
    """The reverse lookup, without a toolkit."""

    def test_a_background_and_a_foreground_never_share_one_map(self) -> None:
        """#ffffff is dark ``fg`` and light ``bg``; one map would confuse them."""
        backgrounds, foregrounds = theming.colour_maps("dark", "light")
        self.assertEqual(backgrounds["#1e1e1e"], tokens.theme("light")["bg"])
        self.assertEqual(foregrounds["#ffffff"], tokens.theme("light")["fg"])

    def test_white_text_does_not_stay_white(self) -> None:
        """The one failure that makes the whole switch pointless."""
        _backgrounds, foregrounds = theming.colour_maps("dark", "light")
        self.assertNotEqual(foregrounds.get("#ffffff"), "#ffffff")

    def test_a_status_keeps_its_meaning_across_the_two_palettes(self) -> None:
        _backgrounds, foregrounds = theming.colour_maps("dark", "light")
        for name in (st.OK, st.DEGRADED, st.UNKNOWN):
            with self.subTest(status=name):
                self.assertEqual(foregrounds.get(tokens.status_color(name, "dark")),
                                 tokens.status_color(name, "light"))

    def test_a_status_colour_never_displaces_a_palette_entry(self) -> None:
        """Body text must win the key it shares with any status colour."""
        _backgrounds, foregrounds = theming.colour_maps("dark", "light")
        self.assertEqual(foregrounds[tokens.theme("dark")["fg"]],
                         tokens.theme("light")["fg"])

    def test_the_round_trip_is_the_inverse(self) -> None:
        there_bg, there_fg = theming.colour_maps("dark", "light")
        back_bg, back_fg = theming.colour_maps("light", "dark")
        for source, target in there_bg.items():
            with self.subTest(colour=source):
                self.assertEqual(back_bg.get(target), source)
        for source, target in there_fg.items():
            if list(there_fg.values()).count(target) == 1:
                with self.subTest(colour=source):
                    self.assertEqual(back_fg.get(target), source)

    def test_an_unknown_theme_is_refused_rather_than_guessed(self) -> None:
        switch = theming.ThemeSwitch()
        with self.assertRaises(ValueError):
            switch.set_theme("solarized")

    def test_a_headless_switch_toggles_without_a_widget(self) -> None:
        switch = theming.ThemeSwitch(theme=theming.DARK)
        self.assertIsNone(switch.control)
        switch.toggle()
        self.assertEqual(switch.theme, theming.LIGHT)
        switch.toggle()
        self.assertEqual(switch.theme, theming.DARK)


@unittest.skipUnless(HAS_DISPLAY, "needs a display")
class TheRealConsoleRepaints(unittest.TestCase):
    """Build the console ``main.py`` builds and press the control it shows."""

    def setUp(self) -> None:
        from gui.operator.app import OperatorConsole

        self.console = OperatorConsole(runs_root=tempfile.mkdtemp())
        self.root = self.console.create_window()
        self.root.geometry("1400x900")
        self.settle()
        self.addCleanup(self.root.destroy)

    def settle(self, rounds: int = 8) -> None:
        for _ in range(rounds):
            self.root.update()
            self.root.update_idletasks()

    def painted(self):
        return (self.console.window.header.frame.cget("background"),
                self.console.workspace("main").frame.cget("background"))

    def walk(self):
        widgets = []

        def visit(widget):
            widgets.append(widget)
            try:
                children = widget.winfo_children()
            except Exception:
                return
            for child in children:
                visit(child)

        visit(self.root)
        return widgets

    def invisible(self):
        """Widgets whose own text is the colour of their own background."""
        offenders = []
        for widget in self.walk():
            try:
                keys = set(widget.keys())
                if "text" not in keys or not str(widget.cget("text")).strip():
                    continue
                if not {"foreground", "background"} <= keys:
                    continue
                if str(widget.cget("foreground")) == str(widget.cget("background")):
                    offenders.append((widget.winfo_class(),
                                      str(widget.cget("text"))[:40]))
            except Exception:
                continue
        return offenders

    def labels(self):
        found = []
        for widget in self.walk():
            try:
                if "text" in set(widget.keys()):
                    value = str(widget.cget("text")).strip()
                    if value:
                        found.append(value)
            except Exception:
                continue
        return found

    def test_the_control_is_attached_beside_the_language_box(self) -> None:
        self.assertIsNotNone(self.console.theme_switch.control)
        self.assertIsNotNone(self.console.language.control)
        self.assertEqual(self.console.theme_switch.control.master.master,
                         self.console.language.control.master.master)

    def test_pressing_it_repaints_the_console(self) -> None:
        before = self.painted()
        self.console.theme_switch.control.invoke()
        self.settle()
        after = self.painted()
        self.assertNotEqual(before, after, "the button changed nothing on screen")
        self.assertEqual(after[1], tokens.theme("light")["bg"])

    def test_nothing_becomes_invisible(self) -> None:
        """The failure a single screenshot of one tab would never show."""
        before = self.invisible()
        self.console.theme_switch.control.invoke()
        self.settle()
        self.assertEqual(self.invisible(), before)

    def test_no_displayed_text_changes_except_the_button_itself(self) -> None:
        """A display control may not edit a value, a status or a readout."""
        before = sorted(set(self.labels()) - {"Dark", "Light"})
        self.console.theme_switch.control.invoke()
        self.settle()
        self.assertEqual(sorted(set(self.labels()) - {"Dark", "Light"}), before)

    def test_the_tick_hook_does_not_undo_the_switch(self) -> None:
        self.console.theme_switch.control.invoke()
        self.settle()
        after = self.painted()
        for _ in range(3):
            self.console.theme_switch.refresh()
            self.settle(2)
        self.assertEqual(self.painted(), after)

    def test_the_round_trip_returns_the_exact_colours(self) -> None:
        before = self.painted()
        self.console.theme_switch.control.invoke()
        self.settle()
        self.console.theme_switch.control.invoke()
        self.settle()
        self.assertEqual(self.painted(), before)

    def test_the_console_still_works_after_the_switch(self) -> None:
        """The point of the whole exercise: it has to still run an experiment.

        A repaint and a tab change must still happen, and the Submit button
        must still route the typed text to the console's one submit action.
        What the console then *does* with it is unchanged policy and is not
        this test's business -- with no session recording it refuses, which is
        correct and is asserted next door.
        """
        self.console.theme_switch.control.invoke()
        self.settle()
        self.console.controller.publish_state()
        self.settle(4)
        notebook = self.console.window.notebook
        notebook.select(notebook.tabs()[1])
        self.settle(4)
        notebook.select(notebook.tabs()[0])
        self.settle(4)
        routed = []
        self.console.handle_action = lambda name, payload="": routed.append(
            (name, payload))
        pane = self.console.workspace("main")
        pane._intent_text.insert("1.0", "hold UE1 video at 9 Mbps")
        pane._submit()
        self.settle(4)
        self.assertEqual(routed, [("submit", "hold UE1 video at 9 Mbps")])

    def test_the_theme_does_not_change_what_a_submit_does(self) -> None:
        """Identical outcome either side of the switch, refusal included."""
        pane = self.console.workspace("main")
        warnings = []
        self.console._warn = lambda message, **kwargs: warnings.append(message)

        pane._intent_text.delete("1.0", "end")
        pane._intent_text.insert("1.0", "hold UE1 video at 9 Mbps")
        pane._submit()
        self.settle(4)
        dark_outcome = list(warnings)

        self.console.theme_switch.control.invoke()
        self.settle()
        warnings.clear()
        pane._intent_text.delete("1.0", "end")
        pane._intent_text.insert("1.0", "hold UE1 video at 9 Mbps")
        pane._submit()
        self.settle(4)

        self.assertEqual(warnings, dark_outcome)
        self.assertTrue(dark_outcome, "the console answered a submit with nothing")


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
