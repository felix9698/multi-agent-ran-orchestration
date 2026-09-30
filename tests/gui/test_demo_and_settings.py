"""Behavioural checks for the T4 presentation workspaces.

These checks name the production mistakes they prevent: presenting replay as
live, inventing a constellation without IQ input, and exposing a credential.
They intentionally exercise the small pure projections behind the widgets so
they run without a display server.
"""

import os
import unittest

from gui.operator.viewmodel.types import (
    ComponentStatusView,
    LlmBackendView,
    MetricAvailabilityView,
    SessionState,
)
from gui.operator.workspaces.demo import DemoWorkspace, demo_snapshot
from gui.operator.workspaces.settings import SettingsWorkspace, integration_snapshot
from gui.operator.widgets.constellation import constellation_state


class DemoAndSettingsTests(unittest.TestCase):
    def test_replay_demo_keeps_an_unhideable_mode_badge(self):
        snapshot = demo_snapshot(SessionState(mode="REPLAY", run_id="run-7"))
        self.assertEqual(snapshot["mode_badge"], "REPLAY")
        self.assertFalse(snapshot["mode_badge_can_hide"])
        self.assertEqual(snapshot["run_id"], "run-7")

    def test_constellation_without_iq_is_unsupported_not_a_fabricated_cloud(self):
        state = constellation_state((), ())
        self.assertEqual(state.status, "UNSUPPORTED")
        self.assertEqual(state.points, ())
        self.assertIn("GAP-10", state.reason)

    def test_sinr_allows_only_explicitly_badged_derived_radio_visualization(self):
        availability = (MetricAvailabilityView(
            metric="NRCellDU.SINR", display="SINR", status="OK", sample_count=3,
        ),)
        state = constellation_state(availability, (1.0, 2.0, 3.0))
        self.assertEqual(state.status, "OK")
        self.assertTrue(state.derived)
        self.assertEqual(state.label, "Derived visualization")

    def test_settings_exposes_credential_reference_and_configuration_not_value(self):
        backend = LlmBackendView(
            name="remote", credential_refs=("env:MODEL_API_KEY",),
            credential_configured=True,
        )
        snapshot = integration_snapshot(SessionState(llm_backends=(backend,)))
        credential = snapshot["llm_backends"][0]["credentials"][0]
        self.assertEqual(credential, {"reference": "env:MODEL_API_KEY", "configured": True})
        self.assertNotIn("value", credential)


@unittest.skipUnless(os.environ.get("DISPLAY"), "requires an Xvfb display")
class T4WorkspaceWidgetSmokeTests(unittest.TestCase):
    """Every T4 workspace must build and accept a state on real Tk widgets."""

    def _root(self):
        import tkinter as tk

        root = tk.Tk()
        root.geometry("1280x720")
        return root

    def test_demo_workspace_builds_and_renders_its_initial_state(self):
        root = self._root()
        try:
            workspace = DemoWorkspace()
            workspace.build(root)
            workspace.on_state(SessionState(mode="REPLAY", run_id="smoke-demo"))
            root.update_idletasks()
            self.assertEqual(workspace._labels["flow"].cget("text"), "No readiness evidence")
            self.assertEqual(workspace._labels["mode"].cget("text"), "MODE: REPLAY")
        finally:
            root.destroy()

    def test_settings_workspace_renders_editable_preferences_and_provenance(self):
        root = self._root()
        try:
            workspace = SettingsWorkspace()
            workspace.build(root)
            state = SessionState(components=(ComponentStatusView(
                element_id="o1", label="O1 Provider", kind="O1_PROVIDER", status="OK",
                source="O1_ASSURANCE", detail={"capture": "run-012", "gate": "PASS"},
            ),))
            workspace.on_state(state)
            workspace._graph_window_var.set("300")
            workspace._refresh_var.set("250")
            workspace._recording_var.set(False)
            root.update_idletasks()
            self.assertEqual(workspace.gui_state()["graph_window_s"], 300.0)
            self.assertEqual(workspace.gui_state()["refresh_ms"], 250)
            self.assertFalse(workspace.gui_state()["recording"])
            self.assertIn("capture: run-012", workspace._provenance.get("1.0", "end"))
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
