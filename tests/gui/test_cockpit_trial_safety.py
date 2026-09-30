"""Trial & Safety and the Evidence Ledger, against a real Assurance Kernel.

Both panes exist to let an operator check the Kernel rather than take its word,
so both are tested against the Kernel's own reduced state and its own
append-only stream -- the hardware-free PIN_TO_CELL vertical path, which is a
complete Kernel run with a mock actuation adapter behind the Write Gateway.
Nothing here contacts a radio, an xApp, a model or a network.

Three scenarios cover the three shapes an operator actually sees:

* a trial that succeeds -- candidate consumed, guards armed, evidence closed,
  reserve returned;
* a trial whose UE drifts away and back inside the hold -- rollback, recovery
  reread, a ``CLOSED_FAIL`` cell;
* an Emergency Stop raised mid-poll -- ``OPERATOR_ABORTED``, the gateway's stop
  and reverse rollback, and the deployment back at its baseline.

The fail-closed half of the file is task section 9.9: there is no widget in
either pane that could change a verdict, an evidence closure, a harm charge or
a rollback result.  That is asserted over the panes' syntax trees, because "I
looked and did not find a control" and "there is no control" are different
claims.
"""

from __future__ import annotations

import ast
import dataclasses
import os
import unittest
from pathlib import Path
from typing import Set

from assurance.core.axes import EvidenceCellStatus, TrialOutcome
from assurance.core.states import TrialState

from gui.operator import data_class as dc
from gui.operator import status as st
from gui.operator.sources import cockpit
from gui.operator.sources import kernel_live as kl
from gui.operator.viewmodel.types import SessionState
from gui.operator.workspaces import evidence_ledger as el
from gui.operator.workspaces import trial_safety as ts
from tests.assurance.pin_to_cell_support import BASELINE_CONFIG, HOME_NCI, TARGET_NCI
from tests.gui.kernel_submission_support import (
    KernelSubmissionFixture,
    PIN_UTTERANCE,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PANE_SOURCES = (
    REPOSITORY_ROOT / "gui" / "operator" / "workspaces" / "trial_safety.py",
    REPOSITORY_ROOT / "gui" / "operator" / "workspaces" / "evidence_ledger.py",
)
HAS_DISPLAY = bool(os.environ.get("DISPLAY"))

#: Widgets that accept operator input.  Neither pane may build one.
CONTROL_WIDGETS = {"Button", "Entry", "Checkbutton", "Radiobutton", "Scale",
                   "Spinbox", "Menubutton", "OptionMenu", "Listbox",
                   "Combobox", "Treeview"}

#: Keyword arguments that give a widget behaviour.
CONTROL_KWARGS = {"command", "textvariable", "variable", "validatecommand"}


class CockpitCase(unittest.TestCase):
    """A hardware-free Kernel run with a Cockpit projection over it."""

    def setUp(self) -> None:
        self.fixture = KernelSubmissionFixture()

    def run_session(self, **kwargs):
        session = self.fixture.session(**kwargs)
        session.draft(PIN_UTTERANCE)
        session.confirm()
        session.start()
        return session

    def project(self, session):
        return cockpit.project_session(session)


class ASuccessfulTrialReadsAsOneEverywhere(CockpitCase):

    def setUp(self) -> None:
        super().setUp()
        self.session = self.run_session()
        self.snapshot = self.project(self.session)

    def test_the_header_names_the_case_and_the_trial(self) -> None:
        header = self.snapshot.header
        self.assertEqual(header.case_id, self.session.case_id)
        self.assertEqual(header.trial_id, self.session.trial_id)
        self.assertEqual(header.kernel_state,
                         TrialState.SETTLED_SUCCESS.value)
        self.assertEqual(header.trial_outcome, TrialOutcome.SUCCESS.value)

    def test_the_header_names_the_active_target_vector(self) -> None:
        self.assertTrue(self.snapshot.header.active_vector)
        item = {i.key: i for i in cockpit.header_items(self.snapshot.header)
                }["target_vector"]
        self.assertEqual(item.value, self.snapshot.header.active_vector)
        self.assertEqual(item.status, st.OK)

    def test_the_harm_balance_is_the_ledger_not_a_running_total(self) -> None:
        """Reserve, charge and return are all read; the balance is arithmetic."""
        balances = self.snapshot.header.harm
        self.assertEqual(len(balances), 1)
        balance = balances[0]
        self.assertEqual(balance.harm_contract_ref, "harm/pin-to-cell")
        self.assertEqual(balance.usable, 100.0)
        self.assertGreater(balance.reserved, 0.0)
        self.assertEqual(balance.charged, 0.0)
        self.assertEqual(balance.returned, balance.reserved)
        self.assertEqual(balance.remaining, 100.0)
        self.assertTrue(balance.limit_respected)

    def test_the_measurement_cell_reports_the_collector_clock(self) -> None:
        measurement = self.snapshot.header.measurement
        self.assertGreater(measurement.sample_count, 0)
        self.assertEqual(measurement.clock_health, "SYNCHRONISED")
        self.assertEqual(measurement.status, st.OK)
        self.assertIsNotNone(measurement.last_observed_at)
        self.assertIsNotNone(measurement.freshness_bound_ms)

    def test_the_consumed_candidate_stays_visible_with_its_reason(self) -> None:
        """A catalog that hid its spent entries would look like a smaller one."""
        rows = self.snapshot.trial_safety.candidates
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].availability, "CONSUMED")
        self.assertIn("already trialled", rows[0].reason or "")
        text = "\n".join(ts.candidate_lines(self.snapshot.trial_safety))
        self.assertIn(rows[0].candidate_id, text)
        self.assertIn("CONSUMED", text)

    def test_the_watchdog_block_shows_what_was_armed_before_apply(self) -> None:
        trial = self.snapshot.trial_safety.current_trial
        self.assertTrue(trial.guards_armed)
        self.assertTrue(trial.apply_counted)
        self.assertIn("wd/serving-cell", trial.watchdog_ids)
        self.assertIn("harm/pin-to-cell", trial.armed_harm_contract_refs)
        text = "\n".join(ts.watchdog_lines(self.snapshot.trial_safety))
        self.assertIn("armed", text)
        self.assertIn("wd/serving-cell", text)

    def test_the_harm_clock_starts_at_the_first_apply(self) -> None:
        trial = self.snapshot.trial_safety.current_trial
        self.assertTrue(trial.apply_counted)
        self.assertIsNotNone(trial.harm_clock_started_at)

    def test_a_success_shows_no_rollback(self) -> None:
        trial = self.snapshot.trial_safety.current_trial
        self.assertFalse(trial.rolled_back)
        self.assertTrue(trial.commit_acknowledged)
        self.assertTrue(trial.finalize_acknowledged)
        self.assertTrue(trial.configuration_reread)

    def test_the_append_only_stream_is_shown_with_its_positions(self) -> None:
        events = self.snapshot.evidence.events
        self.assertGreater(len(events), 10)
        self.assertEqual([entry.position for entry in events],
                         list(range(1, len(events) + 1)))
        self.assertTrue(all(entry.content_hash for entry in events))
        kinds = {entry.event_kind for entry in events}
        for kind in ("EpochFrozen", "CaseOpened", "TrialOpened",
                     "TrialSettled"):
            with self.subTest(kind=kind):
                self.assertIn(kind, kinds)

    def test_the_stream_states_its_true_length_even_when_windowed(self) -> None:
        text = "\n".join(el.stream_lines(self.snapshot.evidence))
        self.assertIn(f"{self.snapshot.evidence.event_count} event(s)", text)
        self.assertIn(self.snapshot.evidence.reducer_version, text)
        self.assertIn(self.snapshot.evidence.terminal_state_hash, text)

    def test_the_evidence_cell_closes_pass_and_says_so(self) -> None:
        cells = self.snapshot.evidence.cells
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].status, EvidenceCellStatus.CLOSED_PASS.value)
        self.assertTrue(cells[0].is_closed)
        self.assertFalse(cells[0].blocks_exhaustion)
        self.assertEqual(cells[0].post_closure_witnesses, 0)
        self.assertEqual(self.snapshot.evidence.closure_progress, 1.0)

    def test_the_confirmation_row_carries_a_hash_and_no_identity(self) -> None:
        """Task section 4.2's ceiling, on the row the operator reads."""
        rows = self.snapshot.evidence.confirmations
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action, "REVIEW_AND_CONFIRM")
        self.assertEqual(rows[0].content_hash,
                         self.session.preview.content_hash())
        self.assertFalse(rows[0].invalidated)
        fields = {f.name.lower()
                  for f in dataclasses.fields(cockpit.ConfirmationRow)}
        for banned in ("signer", "signature", "approver", "operator_name",
                       "role", "user", "identity", "authority"):
            with self.subTest(field=banned):
                self.assertNotIn(banned, fields)

    def test_the_harm_ledger_block_shows_the_movements_it_derives_from(
            self) -> None:
        text = "\n".join(el.harm_lines(self.snapshot.evidence))
        self.assertIn("RESERVE", text)
        self.assertIn("RETURN", text)
        self.assertIn("harm/pin-to-cell", text)
        self.assertIn(dc.DATA_CLASS_SPECS[dc.DERIVED].glyph, text)

    def test_every_block_of_both_panes_renders(self) -> None:
        trial_blocks = ts.block_text(self.snapshot.trial_safety)
        self.assertEqual(set(trial_blocks),
                         {key for key, _label in ts.BLOCKS})
        evidence_blocks = el.block_text(self.snapshot.evidence)
        self.assertEqual(set(evidence_blocks),
                         {key for key, _label in el.BLOCKS})
        for blocks in (trial_blocks, evidence_blocks):
            for key, lines in blocks.items():
                with self.subTest(block=key):
                    self.assertTrue(lines, f"{key} rendered nothing at all")


class ADriftingUeRollsBackAndTheCockpitSaysSo(CockpitCase):

    def setUp(self) -> None:
        super().setUp()
        self.session = self.run_session(
            observed=(TARGET_NCI, TARGET_NCI, HOME_NCI, TARGET_NCI, TARGET_NCI))
        self.snapshot = self.project(self.session)

    def test_the_trial_row_records_the_rollback_and_the_recovery(self) -> None:
        trial = self.snapshot.trial_safety.current_trial
        self.assertEqual(trial.outcome, TrialOutcome.FAIL.value)
        self.assertTrue(trial.rolled_back)
        self.assertTrue(trial.recovery_reread)
        self.assertTrue(trial.recovery_verified)
        text = "\n".join(ts.rollback_lines(self.snapshot.trial_safety))
        self.assertIn("rolled back yes", text)
        self.assertIn("recovery verified yes", text)

    def test_the_deployment_is_back_at_its_baseline(self) -> None:
        self.assertEqual(dict(self.fixture.adapter.snapshot()),
                         dict(BASELINE_CONFIG))

    def test_a_closed_fail_is_not_drawn_as_a_success(self) -> None:
        cell = self.snapshot.evidence.cells[0]
        self.assertEqual(cell.status, EvidenceCellStatus.CLOSED_FAIL.value)
        self.assertEqual(cell.gui_status, st.ERROR)
        self.assertNotEqual(cell.gui_status, st.OK)

    def test_the_header_colours_the_kernel_state_by_the_outcome(self) -> None:
        item = {i.key: i for i in cockpit.header_items(self.snapshot.header)
                }["kernel_state"]
        self.assertEqual(item.status, st.DEGRADED)
        self.assertIn("FAIL", item.detail or "")


class AnEmergencyStopIsVisibleEndToEnd(CockpitCase):

    def setUp(self) -> None:
        super().setUp()
        session = self.fixture.session(
            publish=lambda _channel, view:
                (session.request_emergency_stop()
                 if view.stage == kl.STAGE_OBSERVING and view.poll_count == 2
                 else None))
        session.draft(PIN_UTTERANCE)
        session.confirm()
        session.start()
        self.session = session
        self.snapshot = self.project(session)

    def test_the_kernel_settles_the_trial_operator_aborted(self) -> None:
        trial = self.snapshot.trial_safety.current_trial
        self.assertEqual(trial.outcome, TrialOutcome.OPERATOR_ABORTED.value)
        self.assertEqual(trial.state, TrialState.SETTLED_NON_SUCCESS.value)

    def test_the_stop_and_the_rollback_are_in_the_append_only_stream(
            self) -> None:
        states = [entry for entry in self.snapshot.evidence.events
                  if entry.event_kind == "TrialStateChanged"]
        self.assertTrue(states)
        kinds = {entry.event_kind for entry in self.snapshot.evidence.events}
        self.assertIn("TrialSettled", kinds)
        self.assertIn("TransactionResolved", kinds)

    def test_the_trial_safety_pane_names_the_full_chain(self) -> None:
        text = "\n".join(ts.rollback_lines(self.snapshot.trial_safety))
        self.assertIn("Emergency Stop raised", text)
        for step in ("OPERATOR_ABORT", "Write Gateway stop", "rollback",
                     "recovery"):
            with self.subTest(step=step):
                self.assertIn(step, text)

    def test_the_deployment_is_back_at_its_baseline(self) -> None:
        self.assertEqual(dict(self.fixture.adapter.snapshot()),
                         dict(BASELINE_CONFIG))


class AnEmptyCockpitClaimsNothing(unittest.TestCase):
    """Task sections 9.11 and 9.12: absent is stated, never rendered as fine."""

    def test_a_projection_with_no_kernel_states_its_reason(self) -> None:
        snapshot = cockpit.project({}, case_id=None, mode="DISCONNECTED")
        self.assertIsNotNone(snapshot.header.unavailable_reason)
        self.assertIsNotNone(snapshot.trial_safety.unavailable_reason)
        self.assertIsNotNone(snapshot.evidence.unavailable_reason)

    def test_an_empty_ledger_is_not_a_successful_read(self) -> None:
        view = cockpit.evidence_ledger_view({}, (), mode="REPLAY")
        self.assertEqual(view.event_count, 0)
        self.assertEqual(view.unavailable_reason, cockpit.NO_EVENT_STREAM)
        text = "\n".join(el.stream_lines(view))
        self.assertIn("Unknown", text)
        self.assertIn(cockpit.NO_EVENT_STREAM, text)

    def test_a_case_with_no_evidence_cell_has_no_closure_progress(self) -> None:
        """``None``, not ``100%``: nothing to close is not everything closed."""
        view = cockpit.EvidenceLedgerView(mode="MOCK", case_id="case/x")
        self.assertIsNone(view.closure_progress)
        text = "\n".join(el.closure_lines(view))
        self.assertIn("Unknown", text)
        self.assertNotIn("100%", text)

    def test_every_empty_block_states_a_reason_rather_than_going_blank(
            self) -> None:
        empty_trial = cockpit.TrialSafetyView(
            mode="DISCONNECTED", unavailable_reason="no Kernel session")
        for key, lines in ts.block_text(empty_trial).items():
            with self.subTest(block=key):
                self.assertTrue(lines)
                self.assertIn("Unknown", "\n".join(lines))
        empty_evidence = cockpit.EvidenceLedgerView(
            mode="DISCONNECTED", unavailable_reason="no Kernel session")
        for key, lines in el.block_text(empty_evidence).items():
            with self.subTest(block=key):
                self.assertTrue(lines)
                self.assertIn("Unknown", "\n".join(lines))

    def test_a_kernel_with_no_readable_stream_yields_nothing_not_a_guess(
            self) -> None:
        class Opaque:
            pass

        self.assertEqual(cockpit.kernel_events(Opaque()), ())


class NeitherPaneCanChangeAnything(unittest.TestCase):
    """Task section 9.9, asserted over the syntax trees rather than by clicking."""

    def test_no_control_widget_is_built(self) -> None:
        for path in PANE_SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"),
                             filename=str(path))
            built: Set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = (getattr(node.func, "attr", None)
                            or getattr(node.func, "id", None))
                    if name in CONTROL_WIDGETS:
                        built.add(name)
            with self.subTest(pane=path.name):
                self.assertEqual(built, set(),
                                 f"{path.name} builds control widgets: {built}")

    def test_no_widget_is_given_behaviour(self) -> None:
        for path in PANE_SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"),
                             filename=str(path))
            attached: Set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        if keyword.arg in CONTROL_KWARGS:
                            attached.add(keyword.arg)
                    if getattr(node.func, "attr", None) == "bind":
                        attached.add("bind")
            with self.subTest(pane=path.name):
                self.assertEqual(attached, set(),
                                 f"{path.name} attaches behaviour: {attached}")

    def test_every_text_widget_is_disabled_again_after_writing(self) -> None:
        for path in PANE_SOURCES:
            source = path.read_text(encoding="utf-8")
            with self.subTest(pane=path.name):
                self.assertEqual(
                    source.count('state="normal"'),
                    source.count('state="disabled"') - 1,
                    "every enable must be paired with a disable, plus the "
                    "disable that follows building")

    def test_the_panes_offer_no_action_callback(self) -> None:
        for workspace in (ts.TrialSafetyWorkspace(),
                          el.EvidenceLedgerWorkspace()):
            with self.subTest(pane=workspace.id):
                self.assertFalse(hasattr(workspace, "buttons"))
                self.assertFalse(hasattr(workspace, "on_action"))
                self.assertEqual(dict(workspace.gui_state()), {})

    def test_restoring_gui_state_cannot_inject_a_verdict(self) -> None:
        for workspace in (ts.TrialSafetyWorkspace(),
                          el.EvidenceLedgerWorkspace()):
            with self.subTest(pane=workspace.id):
                before = workspace.lines()
                workspace.restore_gui_state({"outcome": "SUCCESS",
                                             "status": "CLOSED_PASS",
                                             "harmCharge": 0})
                self.assertEqual(workspace.lines(), before)

    def test_the_session_state_does_not_change_what_either_pane_says(
            self) -> None:
        for workspace in (ts.TrialSafetyWorkspace(),
                          el.EvidenceLedgerWorkspace()):
            with self.subTest(pane=workspace.id):
                before = workspace.lines()
                for mode in ("LIVE", "REPLAY", "DISCONNECTED"):
                    self.assertIsNone(
                        workspace.on_state(SessionState(mode=mode)))
                    self.assertEqual(workspace.lines(), before)

    def test_every_cockpit_view_model_is_frozen(self) -> None:
        for factory in (lambda: cockpit.TrialRow(trial_id="t"),
                        lambda: cockpit.CandidateRow(candidate_id="c"),
                        lambda: cockpit.EvidenceCellRow(cell_id="cell"),
                        lambda: cockpit.HarmBalanceView("harm/x"),
                        lambda: cockpit.LedgerEntry(1, "e", "k", "o", "t", 0),
                        lambda: cockpit.CockpitHeaderView(),
                        lambda: cockpit.TrialSafetyView(),
                        lambda: cockpit.EvidenceLedgerView()):
            instance = factory()
            with self.subTest(view=type(instance).__name__):
                field = dataclasses.fields(instance)[0].name
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(instance, field, "tampered")


@unittest.skipUnless(HAS_DISPLAY, "needs a display")
class ThePanesRenderOnScreen(CockpitCase):
    """The Tk half: both panes build, paint a real snapshot and tear down."""

    def test_both_panes_build_and_paint_a_settled_trial(self) -> None:
        import tkinter as tk

        session = self.run_session()
        snapshot = self.project(session)
        root = tk.Tk()
        try:
            trial_pane = ts.TrialSafetyWorkspace()
            evidence_pane = el.EvidenceLedgerWorkspace()
            trial_pane.build(root)
            evidence_pane.build(root)
            trial_pane.on_trial_safety(snapshot.trial_safety)
            evidence_pane.on_evidence(snapshot.evidence)
            self.assertIn("SETTLED_SUCCESS",
                          "\n".join(trial_pane.lines()["candidates"]))
            self.assertIn("CLOSED_PASS",
                          "\n".join(evidence_pane.lines()["closure"]))
            for pane in (trial_pane, evidence_pane):
                for widget in pane._texts.values():
                    self.assertEqual(str(widget.cget("state")), "disabled")
            trial_pane.destroy()
            evidence_pane.destroy()
        finally:
            root.destroy()


if __name__ == "__main__":                                 # pragma: no cover
    unittest.main()
