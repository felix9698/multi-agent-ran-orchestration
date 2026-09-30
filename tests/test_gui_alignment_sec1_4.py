#!/usr/bin/env python3
"""
GUI <-> latest paper (Sec I-IV) alignment tests (docs/gui_alignment_sec1_4.md).

These assert the GUI shows exactly what the latest paper defines and NOTHING it
dropped:

  * the closed-form N_max is GONE from the decision-calibration panel (it was
    removed from the paper; the retained soft negotiation cap lives under the
    budget guard, Eq.28-30);
  * the FSM's TechnicalFailsafe sink is a DISTINCT, highlightable node (before
    this the coordinator emitted S_TECHNICAL_FAILSAFE but the GUI could not show
    it - all circles just went dark);
  * the three Eq.12 terminal states (Admitted / NotAdmitted / TechnicalFailsafe)
    are visibly distinct;
  * pre-measurement cells show a placeholder, never a stale Sec V prior dressed
    up as a measurement;
  * the calibration panel reads the SAME keys the coordinator's
    ``calibrator.get_stats()`` emits (the RSRP/SINR-style key-drift bug class);
  * the coordinator emits the truly-final Eq.12 terminal outcome to the GUI.

Every GUI test is HEADLESS: ``tk.Tk()`` then immediate ``withdraw()``, NO
mainloop, and the update queue is drained manually.  Window creation is guarded
so a display-less CI skips (never fails) - matching tests/test_batch_g.py.
"""

import unittest


def _try_make_gui(title="test"):
    """Create a withdrawn dashboard window or return (None, reason) if tk / a
    display is unavailable (headless CI)."""
    try:
        import gui.dashboard as dash
    except Exception as e:                      # pragma: no cover - import guard
        return None, None, f"dashboard import unavailable: {e}"
    try:
        g = dash.IntentCoordinatorGUI(title=title)
        g.create_window()
        g.root.withdraw()                       # headless: never map the window
    except Exception as e:                       # pragma: no cover - no display
        return None, dash, f"tk window unavailable: {e}"
    return g, dash, None


class _GuiTestBase(unittest.TestCase):
    def setUp(self):
        g, dash, reason = _try_make_gui(self.__class__.__name__)
        if g is None:
            self.skipTest(reason)
        self.g = g
        self.dash = dash

    def tearDown(self):
        g = getattr(self, "g", None)
        if g is not None and getattr(g, "root", None) is not None:
            try:
                g.root.destroy()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Module-level mapping (no window needed)
# --------------------------------------------------------------------------- #
class TerminalStateMappingTest(unittest.TestCase):
    """TerminalOutcome (4 members) -> Eq.12 terminal state (3 members)."""

    def setUp(self):
        try:
            import gui.dashboard as dash
        except Exception as e:                  # pragma: no cover
            self.skipTest(f"dashboard import unavailable: {e}")
        self.dash = dash

    def test_commit_outcomes_are_admitted(self):
        self.assertEqual(self.dash.terminal_state_label("commit_original"),
                         self.dash.TERMINAL_STATE_ADMITTED)
        self.assertEqual(self.dash.terminal_state_label("commit_revised"),
                         self.dash.TERMINAL_STATE_ADMITTED)

    def test_pending_is_not_admitted(self):
        self.assertEqual(self.dash.terminal_state_label("pending_not_admitted"),
                         self.dash.TERMINAL_STATE_NOT_ADMITTED)

    def test_failsafe_is_technical_failsafe(self):
        self.assertEqual(self.dash.terminal_state_label("technical_failsafe"),
                         self.dash.TERMINAL_STATE_TECHNICAL_FAILSAFE)

    def test_enum_value_is_accepted(self):
        # A real coordinator TerminalOutcome enum (has .value) maps identically.
        from coordinator.episode_types import TerminalOutcome
        self.assertEqual(
            self.dash.terminal_state_label(TerminalOutcome.COMMIT_ORIGINAL),
            self.dash.TERMINAL_STATE_ADMITTED)
        self.assertEqual(
            self.dash.terminal_state_label(TerminalOutcome.PENDING_NOT_ADMITTED),
            self.dash.TERMINAL_STATE_NOT_ADMITTED)

    def test_unknown_and_none_are_none(self):
        # An unknown/empty value is never fabricated into an outcome.
        self.assertIsNone(self.dash.terminal_state_label(None))
        self.assertIsNone(self.dash.terminal_state_label(""))
        self.assertIsNone(self.dash.terminal_state_label("banana"))

    def test_every_terminal_outcome_maps(self):
        # No coordinator terminal outcome may fall through the mapping.
        from coordinator.episode_types import TerminalOutcome
        for oc in TerminalOutcome:
            self.assertIn(self.dash.terminal_state_label(oc),
                          set(self.dash.TERMINAL_STATE_ORDER))


# --------------------------------------------------------------------------- #
# N_max removal (task item 1)
# --------------------------------------------------------------------------- #
class NmaxRemovalTest(_GuiTestBase):
    def test_no_nmax_headline_calibration_cell(self):
        # The dropped closed-form N_max is no longer a headline calibration
        # parameter (neither "N_max" nor the old §V "C_worst/R_success/C_nego").
        for gone in ("N_max", "C_worst", "R_success", "C_nego"):
            self.assertNotIn(gone, self.g.calib_labels)

    def test_theta_star_is_present(self):
        self.assertIn("θ*", self.g.calib_labels)

    def test_retained_soft_cap_under_budget_guard(self):
        # The retained soft negotiation-round cap (N_a^max) is still shown, but
        # under the budget guard (Eq.28-30), fed by get_stats()["n_max"].
        self.assertIn("Nego cap", self.g.calib_labels)
        self.g.update_calibration({"n_max": 4})
        self.g._process_updates()
        self.assertEqual(self.g.calib_labels["Nego cap"].cget("text"), "4")


# --------------------------------------------------------------------------- #
# TechnicalFailsafe sink + Eq.12 terminal states (task item 2)
# --------------------------------------------------------------------------- #
class TerminalStateDisplayTest(_GuiTestBase):
    def test_failsafe_sink_is_a_distinct_node(self):
        self.assertIn("S_TECHNICAL_FAILSAFE", self.g.state_colors)
        self.assertIn("S_TECHNICAL_FAILSAFE", self.g.state_labels)
        # distinct hue from S5 (Nego) so the sink is not confused with it.
        self.assertNotEqual(self.g.state_colors["S_TECHNICAL_FAILSAFE"],
                            self.g.state_colors["S5"])

    def test_failsafe_state_highlights(self):
        # The coordinator emits update_state("S_TECHNICAL_FAILSAFE"); the node
        # must light up (before this it was silently invisible).
        self.g.update_state("S_TECHNICAL_FAILSAFE")
        self.g._process_updates()
        node = self.g.state_labels["S_TECHNICAL_FAILSAFE"]
        fill = node["canvas"].itemcget(node["circle"], "fill")
        self.assertEqual(fill, self.g.state_colors["S_TECHNICAL_FAILSAFE"])

    def test_all_three_terminal_states_have_indicators(self):
        self.assertEqual(set(self.g.terminal_labels),
                         set(self.dash.TERMINAL_STATE_ORDER))

    def test_terminal_indicator_highlights_each_outcome(self):
        cases = {
            "commit_original": self.dash.TERMINAL_STATE_ADMITTED,
            "commit_revised": self.dash.TERMINAL_STATE_ADMITTED,
            "pending_not_admitted": self.dash.TERMINAL_STATE_NOT_ADMITTED,
            "technical_failsafe": self.dash.TERMINAL_STATE_TECHNICAL_FAILSAFE,
        }
        for outcome, expect in cases.items():
            self.g.update_terminal_outcome(outcome)
            self.g._process_updates()
            self.assertEqual(self.g.terminal_state, expect)
            active = self.g.terminal_labels[expect]
            self.assertEqual(active.cget("bg"),
                             self.dash.TERMINAL_STATE_COLORS[expect])
            # every OTHER indicator is muted (exactly one is active)
            for name, lbl in self.g.terminal_labels.items():
                if name != expect:
                    self.assertNotEqual(lbl.cget("bg"),
                                        self.dash.TERMINAL_STATE_COLORS[name])

    def test_unknown_terminal_outcome_clears_row(self):
        self.g.update_terminal_outcome("commit_original")
        self.g._process_updates()
        self.g.update_terminal_outcome("nonsense")
        self.g._process_updates()
        self.assertIsNone(self.g.terminal_state)
        for lbl in self.g.terminal_labels.values():
            self.assertEqual(lbl.cget("bg"), self.g.colors["border"])


# --------------------------------------------------------------------------- #
# Pre-measurement placeholders (task item 3)
# --------------------------------------------------------------------------- #
class PreMeasurementPlaceholderTest(_GuiTestBase):
    def test_defaults_are_placeholder_not_priors(self):
        # No Sec V prior (0.70 / 125.0 / 64.5 / 50.0 / 3) may be shown as a
        # measurement before calibration data arrives.
        for key in ("θ*", "C_F", "R", "C_N", "θ* valid",
                    "Nego cap", "Rollbacks", "Negotiations"):
            self.assertEqual(self.g.calib_labels[key].cget("text"),
                             self.dash.PRE_MEASUREMENT)

    def test_real_update_replaces_placeholder(self):
        self.g.update_calibration({"theta_star": 0.42})
        self.g._process_updates()
        self.assertEqual(self.g.calib_labels["θ*"].cget("text"), "0.420")
        self.assertNotEqual(self.g.calib_labels["θ*"].cget("text"),
                            self.dash.PRE_MEASUREMENT)

    def test_gauge_threshold_safe_when_theta_is_placeholder(self):
        # float("-") must NOT raise: an LLM update before any calibration keeps
        # the gauge's own threshold rather than crashing.
        self.g.update_llm_analysis({"model": "m", "confidence": 0.9})
        self.g._process_updates()   # must not raise
        self.assertIsInstance(self.g.confidence_gauge.threshold, float)


# --------------------------------------------------------------------------- #
# Key-mapping fidelity vs calibrator.get_stats() (RSRP/SINR-style drift class)
# --------------------------------------------------------------------------- #
class CalibrationKeyMappingTest(_GuiTestBase):
    def _stats(self):
        """A representative calibrator.get_stats() payload (the real emit)."""
        try:
            from calibration.adaptive_calibrator import AdaptiveCalibrator
            cal = AdaptiveCalibrator(initial_theta=0.55, initial_n_max=2,
                                     c_episode=500.0)
            return cal.get_stats()
        except Exception:
            # fall back to a hand-built stats-shaped dict if construction needs
            # more config than is available here
            return {"theta_star": 0.55, "assumption_ok": True, "n_max": 2,
                    "c_worst_estimate": 125.0, "r_success_estimate": 64.5,
                    "c_nego_estimate": 50.0, "total_rollbacks": 0,
                    "total_negotiations": 0}

    def test_all_panels_populate_from_get_stats(self):
        stats = self._stats()
        # sanity: the keys the GUI reads really are in get_stats()
        for k in ("theta_star", "assumption_ok", "n_max", "c_worst_estimate",
                  "r_success_estimate", "c_nego_estimate", "total_rollbacks",
                  "total_negotiations"):
            self.assertIn(k, stats, f"get_stats() missing {k} (GUI would go blank)")

        self.g.update_calibration(stats)
        self.g._process_updates()

        # every cell left its placeholder (no key drift left a panel blank)
        for key in ("θ*", "C_F", "R", "C_N", "θ* valid",
                    "Nego cap", "Rollbacks", "Negotiations"):
            self.assertNotEqual(self.g.calib_labels[key].cget("text"),
                                self.dash.PRE_MEASUREMENT,
                                f"{key} stayed blank - key drift")

        self.assertEqual(self.g.calib_labels["θ*"].cget("text"),
                         f"{stats['theta_star']:.3f}")
        self.assertEqual(self.g.calib_labels["Nego cap"].cget("text"),
                         str(stats["n_max"]))
        self.assertEqual(self.g.calib_labels["θ* valid"].cget("text"),
                         "yes" if stats["assumption_ok"] else "no")

    def test_theta_valid_shows_no_when_assumption_violated(self):
        self.g.update_calibration({"assumption_ok": False})
        self.g._process_updates()
        self.assertEqual(self.g.calib_labels["θ* valid"].cget("text"), "no")


# --------------------------------------------------------------------------- #
# Coordinator -> GUI terminal-outcome emit (the GUI delivery path)
# --------------------------------------------------------------------------- #
class _RecordingGUI:
    def __init__(self):
        self.terminal = []

    def update_terminal_outcome(self, outcome):
        self.terminal.append(outcome)


class CoordinatorEmitsTerminalOutcomeTest(unittest.TestCase):
    """The single-flight harness reports the TRULY-FINAL Eq.12 outcome (after
    settlement) to the GUI, isolated via _safe_gui."""

    def _skeleton(self, gui, settled=None):
        from coordinator.intent_coordinator import IntentCoordinator
        c = IntentCoordinator.__new__(IntentCoordinator)
        c.gui = gui
        c._episode_in_flight = False
        c._publish_history = lambda published: None
        # settlement may rewrite the terminal outcome; default: pass-through
        c._settle_after_cleanup = lambda result: (settled
                                                   if settled is not None
                                                   else result)
        return c

    def test_emits_final_outcome(self):
        gui = _RecordingGUI()
        c = self._skeleton(gui)
        c._run_episode_single_flight(
            pending_ref="x",
            body=lambda: {"terminal_outcome": "commit_original"})
        self.assertEqual(gui.terminal, ["commit_original"])

    def test_emits_settlement_downgraded_outcome(self):
        # If settlement downgrades a commit to failsafe, the GUI must see the
        # DOWNGRADED outcome, never the pre-settlement commit.
        gui = _RecordingGUI()
        downgraded = {"terminal_outcome": "technical_failsafe"}
        c = self._skeleton(gui, settled=downgraded)
        c._run_episode_single_flight(
            pending_ref="x",
            body=lambda: {"terminal_outcome": "commit_original"})
        self.assertEqual(gui.terminal, ["technical_failsafe"])

    def test_no_gui_is_safe(self):
        # A coordinator with gui=None must not raise (import-compat / headless).
        c = self._skeleton(None)
        out = c._run_episode_single_flight(
            pending_ref="x",
            body=lambda: {"terminal_outcome": "pending_not_admitted"})
        self.assertEqual(out["terminal_outcome"], "pending_not_admitted")

    def test_gui_without_method_is_ignored(self):
        # A GUI lacking update_terminal_outcome (older build) is tolerated by
        # _safe_gui (getattr miss -> no-op).
        class _Bare:
            pass
        c = self._skeleton(_Bare())
        c._run_episode_single_flight(
            pending_ref="x",
            body=lambda: {"terminal_outcome": "commit_revised"})   # no raise


if __name__ == "__main__":
    unittest.main()
