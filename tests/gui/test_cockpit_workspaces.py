"""The Cockpit's eight workspaces, its seven-item header, and its data classes.

Task section 9 fixes three lists, and a console can drift off any of them
silently:

* **eight workspaces**, named -- a build that quietly ships seven still starts,
  still looks finished, and is missing a surface nobody notices until the run
  that needed it;
* **seven always-visible header items** -- six of them readouts and the seventh
  the Emergency Stop, which is the one control that must be reachable from
  every workspace;
* **five data classes** -- ``LIVE``, ``REPLAY``, ``DERIVED``, ``UNKNOWN`` and
  ``UNSUPPORTED`` -- which must be distinguishable without colour.

Every assertion here is against the real console, the real registry and the
real projections.  Hermetic: nothing touches a radio, a model or a network, and
the two classes that build actual Tk widgets are skipped without a display.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from gui.operator import data_class as dc
from gui.operator import status as st
from gui.operator.app import OperatorConsole
from gui.operator.shell.cockpit_header import (
    EMERGENCY_STOP_ACTION,
    CockpitHeaderBar,
    header_line,
)
from gui.operator.sources.cockpit import (
    COCKPIT_HEADER_KEYS,
    CockpitHeaderView,
    header_items,
)
from gui.operator.viewmodel.bus import CHANNELS
from gui.operator.workspaces import (
    COCKPIT_WORKSPACES,
    WORKSPACE_MODULES,
    PlaceholderWorkspace,
)

HAS_DISPLAY = bool(os.environ.get("DISPLAY"))

#: The eight names task section 9 lists, verbatim.  Written out here rather
#: than imported so a rename in the registry has to be a deliberate edit in two
#: places instead of a silent one in one.
REQUIRED_WORKSPACES = (
    "Live Operations",
    "Contract Studio",
    "Trial & Safety",
    "Evidence Ledger",
    "Batch Experiments",
    "Analysis & Results",
    "Demo View",
    "Settings & Integration",
)

#: The seven header items, in the order the task lists them.
REQUIRED_HEADER_ITEMS = (
    "case_trial", "kernel_state", "target_vector", "harm", "measurement",
    "mode", "emergency_stop",
)


class TheCockpitHasEightWorkspaces(unittest.TestCase):
    """Named, ordered, importable and actually constructed by the console."""

    def test_the_registry_declares_the_eight_by_name(self) -> None:
        self.assertEqual(tuple(title for _id, title in COCKPIT_WORKSPACES),
                         REQUIRED_WORKSPACES)

    def test_every_declared_cockpit_workspace_has_a_module(self) -> None:
        modules = {entry[0]: entry for entry in WORKSPACE_MODULES}
        for workspace_id, title in COCKPIT_WORKSPACES:
            with self.subTest(workspace=workspace_id):
                self.assertIn(workspace_id, modules,
                              f"{workspace_id} is not in WORKSPACE_MODULES")
                self.assertEqual(modules[workspace_id][3], title)

    def test_an_assembled_console_builds_all_eight_for_real(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            try:
                built = {workspace.id: workspace
                         for workspace in console.workspaces}
                for workspace_id, title in COCKPIT_WORKSPACES:
                    with self.subTest(workspace=workspace_id):
                        pane = built.get(workspace_id)
                        self.assertIsNotNone(pane)
                        self.assertNotIsInstance(
                            pane, PlaceholderWorkspace,
                            f"{workspace_id} fell back to a placeholder")
                        self.assertEqual(pane.title, title)
            finally:
                console.shutdown()

    def test_the_three_new_panes_are_in_cockpit_order(self) -> None:
        """Trial & Safety and the Evidence Ledger follow the Contract Studio.

        Order is not decoration here: the reading order of the tabs is the
        order of the work -- what was submitted, what is being tried, what was
        recorded -- and a Cockpit that put the ledger first would be asking the
        operator to check a result before seeing the run.
        """
        order = [entry[0] for entry in WORKSPACE_MODULES]
        self.assertLess(order.index("contract_studio"),
                        order.index("trial_safety"))
        self.assertLess(order.index("trial_safety"),
                        order.index("evidence_ledger"))
        self.assertLess(order.index("evidence_ledger"),
                        order.index("batch_experiments"))

    def test_the_preserved_legacy_panes_are_still_there(self) -> None:
        """Section 9.5: the existing valid functions are relocated, not dropped.

        Intent history, the commercial/local model selector and the
        multi-stage reasoning view live in ``intent_decision``; the honest
        per-objective capability projection lives in ``objective_registry``.
        Neither is one of the eight, and neither may be deleted to make the
        list of eight look tidy.
        """
        declared = {entry[0] for entry in WORKSPACE_MODULES}
        self.assertIn("intent_decision", declared)
        self.assertIn("objective_registry", declared)

    def test_the_cockpit_channels_are_subscribable(self) -> None:
        self.assertIn("cockpit", CHANNELS)
        self.assertIn("batch", CHANNELS)


class TheHeaderShowsSevenThingsAlways(unittest.TestCase):
    """Six readouts and one control, on every state including the empty one."""

    def test_the_seven_items_are_declared_in_order(self) -> None:
        self.assertEqual(COCKPIT_HEADER_KEYS, REQUIRED_HEADER_ITEMS)

    def test_an_empty_console_still_renders_all_seven(self) -> None:
        items = header_items(CockpitHeaderView())
        self.assertEqual(tuple(item.key for item in items),
                         REQUIRED_HEADER_ITEMS)

    def test_an_empty_console_claims_nothing(self) -> None:
        """No case, no Kernel state, no vector, no harm, no measurement.

        The failure this guards is a header that defaults to plausible values
        and therefore reads as a healthy idle console when nothing has been
        contacted at all.
        """
        items = {item.key: item for item in header_items(CockpitHeaderView())}
        for key in ("case_trial", "kernel_state", "target_vector", "harm",
                    "measurement"):
            with self.subTest(item=key):
                self.assertEqual(items[key].value, st.PRE_MEASUREMENT)
                self.assertEqual(items[key].status, st.UNKNOWN)
                self.assertEqual(items[key].badge.data_class, dc.UNKNOWN)
                self.assertTrue(items[key].badge.states_a_reason,
                                f"{key} is Unknown without saying why")

    def test_a_disconnected_console_never_reads_live(self) -> None:
        items = {item.key: item for item in header_items(CockpitHeaderView())}
        self.assertEqual(items["mode"].value, "DISCONNECTED")
        self.assertFalse(items["mode"].badge.is_live)
        for item in header_items(CockpitHeaderView()):
            with self.subTest(item=item.key):
                self.assertFalse(item.badge.is_live)

    def test_a_mock_session_is_not_badged_live(self) -> None:
        """A hardware-free adapter is a complete Kernel path, not a radio."""
        view = CockpitHeaderView(mode="MOCK", case_id="case/x",
                                 trial_id="case/x:trial:1",
                                 kernel_state="OBSERVING")
        items = {item.key: item for item in header_items(view)}
        self.assertEqual(items["mode"].value, "MOCK")
        self.assertFalse(items["mode"].badge.is_live)
        self.assertIn("no O-RAN endpoint", items["mode"].detail or "")

    def test_the_harm_cell_is_marked_derived_not_measured(self) -> None:
        from gui.operator.sources.cockpit import HarmBalanceView

        view = CockpitHeaderView(mode="MOCK", case_id="case/x",
                                 harm=(HarmBalanceView("harm/x", "ms", 100.0,
                                                       charged=5.0,
                                                       status=st.OK),))
        item = {i.key: i for i in header_items(view)}["harm"]
        self.assertEqual(item.badge.data_class, dc.DERIVED)
        self.assertTrue(item.badge.states_a_reason)
        self.assertIn("charged 5", item.value)
        self.assertIn("reserve 100", item.value)

    def test_every_readout_is_glyph_led_not_colour_only(self) -> None:
        view = CockpitHeaderView(mode="LIVE", case_id="case/x",
                                 trial_id="case/x:trial:1",
                                 kernel_state="OBSERVING",
                                 active_vector="vector/x")
        for item in header_items(view):
            with self.subTest(item=item.key):
                text = item.as_text()
                self.assertIn(st.resolve(item.status).glyph, text)
                self.assertIn(item.badge.glyph, text)

    def test_the_line_rendering_carries_all_seven_labels(self) -> None:
        line = header_line(CockpitHeaderView())
        for item in header_items(CockpitHeaderView()):
            with self.subTest(item=item.key):
                self.assertIn(item.label, line)

    def test_emergency_stop_states_the_chain_it_runs(self) -> None:
        """Section 9.10 is a chain, and the header says which one."""
        view = CockpitHeaderView(mode="MOCK", case_id="case/x",
                                 trial_id="case/x:trial:1",
                                 kernel_state="OBSERVING")
        item = {i.key: i for i in header_items(view)}["emergency_stop"]
        self.assertEqual(item.value, "ARMED")
        self.assertIn("OPERATOR_ABORT", item.detail or "")
        self.assertIn("Write Gateway stop", item.detail or "")
        self.assertIn("rollback", item.detail or "")
        self.assertIn("recovery", item.detail or "")

    def test_a_raised_stop_is_visible_before_it_is_honoured(self) -> None:
        view = CockpitHeaderView(mode="MOCK", case_id="case/x",
                                 trial_id="case/x:trial:1",
                                 kernel_state="OBSERVING",
                                 emergency_stop_requested=True)
        item = {i.key: i for i in header_items(view)}["emergency_stop"]
        self.assertEqual(item.value, "REQUESTED")
        self.assertEqual(item.status, st.BLOCKED)

    def test_a_settled_terminal_trial_offers_no_stop(self) -> None:
        view = CockpitHeaderView(mode="MOCK", case_id="case/x",
                                 trial_id="case/x:trial:1",
                                 kernel_state="SETTLED_SUCCESS")
        item = {i.key: i for i in header_items(view)}["emergency_stop"]
        self.assertEqual(item.status, st.NOT_APPLICABLE)
        self.assertIn("nothing to stop", item.detail or "")


class TheEmergencyStopIsReachableFromEverywhere(unittest.TestCase):
    """A control only one workspace can reach is not an emergency control."""

    def test_the_console_routes_the_action_the_header_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            try:
                self.assertIn(EMERGENCY_STOP_ACTION, console.action_keys())
                console.handle_action(EMERGENCY_STOP_ACTION, "")
                last = console.controller.timeline[-1]
                self.assertNotEqual(
                    last.title,
                    f"unknown action {EMERGENCY_STOP_ACTION!r}")
            finally:
                console.shutdown()

    def test_the_console_holds_a_cockpit_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            try:
                self.assertIsInstance(console.cockpit_header, CockpitHeaderBar)
                self.assertEqual(console.cockpit_header.on_action,
                                 console.handle_action)
            finally:
                console.shutdown()

    @unittest.skipUnless(HAS_DISPLAY, "needs a display")
    def test_the_button_exists_on_screen_and_raises_the_action(self) -> None:
        raised = []
        bar = CockpitHeaderBar(on_action=lambda key, payload:
                               raised.append((key, payload)))
        import tkinter as tk

        root = tk.Tk()
        try:
            bar.build(root)
            self.assertIsNotNone(bar.stop_button)
            bar.stop_button.invoke()
            self.assertEqual(raised, [(EMERGENCY_STOP_ACTION, "")])
            # And it repaints without a Kernel rather than blanking.
            bar.update(CockpitHeaderView())
            self.assertEqual(len(bar.items()), 7)
        finally:
            root.destroy()

    @unittest.skipUnless(HAS_DISPLAY, "needs a display")
    def test_the_window_packs_both_header_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            try:
                console.create_window()
                self.assertIsNotNone(console.window.header)
                self.assertIsNotNone(console.window.cockpit_header)
                self.assertIsNotNone(console.window.cockpit_header.frame)
            finally:
                if console.window.root is not None:
                    console.window.root.destroy()
                console.shutdown()


class TheFiveDataClassesAreDistinguishable(unittest.TestCase):
    """Task section 9.7, and it must survive a greyscale screenshot."""

    def test_all_five_are_declared(self) -> None:
        self.assertEqual(dc.DATA_CLASS_ORDER,
                         ("LIVE", "REPLAY", "DERIVED", "UNKNOWN",
                          "UNSUPPORTED"))
        self.assertEqual(set(dc.DATA_CLASS_SPECS), set(dc.DATA_CLASS_ORDER))

    def test_no_two_classes_share_a_glyph_shape_or_label(self) -> None:
        for attribute in ("glyph", "shape", "label"):
            values = [getattr(dc.DATA_CLASS_SPECS[name], attribute)
                      for name in dc.DATA_CLASS_ORDER]
            with self.subTest(attribute=attribute):
                self.assertEqual(len(set(values)), len(values),
                                 f"two data classes share a {attribute}")

    def test_the_encoding_is_never_colour_only(self) -> None:
        """No colour appears in the vocabulary at all, by construction."""
        import dataclasses

        fields = {f.name for f in dataclasses.fields(dc.DataClassSpec)}
        self.assertNotIn("color", fields)
        self.assertNotIn("colour", fields)
        for name in dc.DATA_CLASS_ORDER:
            spec = dc.DATA_CLASS_SPECS[name]
            with self.subTest(data_class=name):
                self.assertTrue(spec.glyph.strip())
                self.assertTrue(spec.shape.strip())

    def test_the_three_reason_requiring_classes_never_render_bare(self) -> None:
        for name in (dc.DERIVED, dc.UNKNOWN, dc.UNSUPPORTED):
            with self.subTest(data_class=name):
                badge = dc.resolve(name)
                self.assertEqual(badge.reason, dc.REASON_NOT_STATED)
                self.assertIn(dc.REASON_NOT_STATED, badge.as_text())

    def test_live_and_replay_do_not_demand_a_reason(self) -> None:
        self.assertIsNone(dc.resolve(dc.LIVE).reason)
        self.assertIsNone(dc.resolve(dc.REPLAY).reason)

    def test_an_unmapped_class_falls_to_unknown_naming_itself(self) -> None:
        badge = dc.resolve("OTA_VERIFIED")
        self.assertEqual(badge.data_class, dc.UNKNOWN)
        self.assertIn("OTA_VERIFIED", badge.reason or "")

    def test_unsupported_outranks_everything_including_live(self) -> None:
        badge = dc.classify(mode="LIVE", value=1.0, supported=False,
                            reason="no counter is bound")
        self.assertEqual(badge.data_class, dc.UNSUPPORTED)

    def test_derived_outranks_the_session_mode(self) -> None:
        badge = dc.classify(mode="LIVE", value=1.0, derived=True,
                            reason="mean over the window")
        self.assertEqual(badge.data_class, dc.DERIVED)

    def test_an_absent_value_is_unknown_even_in_a_live_session(self) -> None:
        badge = dc.classify(mode="LIVE", value=None, reason="not reported")
        self.assertEqual(badge.data_class, dc.UNKNOWN)

    def test_only_an_observed_value_in_a_live_session_is_live(self) -> None:
        self.assertTrue(dc.classify(mode="LIVE", value=1.0).is_live)
        self.assertFalse(dc.classify(mode="LIVE", value=1.0,
                                     observed=False).is_live)
        for mode in ("REPLAY", "MOCK", "SYNTHETIC", "EMULATED",
                     "DISCONNECTED", "SOMETHING_ELSE"):
            with self.subTest(mode=mode):
                self.assertFalse(dc.classify(mode=mode, value=1.0).is_live)

    def test_every_non_live_mode_states_why_it_is_not_live(self) -> None:
        for mode in ("MOCK", "SYNTHETIC", "EMULATED", "DISCONNECTED"):
            with self.subTest(mode=mode):
                badge = dc.classify(mode=mode, value=mode)
                self.assertTrue(badge.states_a_reason)

    def test_the_legend_names_all_five(self) -> None:
        legend = dc.legend()
        self.assertEqual(len(legend), 5)
        for name in dc.DATA_CLASS_ORDER:
            with self.subTest(data_class=name):
                self.assertTrue(any(dc.DATA_CLASS_SPECS[name].label in line
                                    for line in legend))


if __name__ == "__main__":                                 # pragma: no cover
    unittest.main()
