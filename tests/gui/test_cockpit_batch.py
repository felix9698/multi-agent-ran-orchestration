"""Batch Experiments: one bounded plan, one Kernel, one evidence store.

Task sections 9.3, 9.4 and 12.  The three claims this pane makes are the three
that would be easiest to fake, so each is checked against the thing itself
rather than against a label:

**"the same Kernel"** -- the executor is handed the console's own session
factory and the test asserts the batch reached the *same object*: the same
:class:`~gui.operator.sources.kernel_live.KernelSubmissionSession`, the same
:class:`~assurance.kernel.kernel.AssuranceKernel`, and the same append-only
event store, whose length grows because of the batch.

**"the same evidence store"** -- the class the batch runner writes run
directories with is asserted to *be* the class the Operator Console's session
controller writes with, by identity.

**"a bounded plan confirmed once"** -- the cases are enumerated before anything
runs, the confirmation covers the plan's content hash, editing a field
invalidates it, and starting locks the scope so the repetition cannot change
what was confirmed.

Hardware-free throughout: the vertical path runs over the mock actuation
adapter, and the batch runner refuses any executor that is not REPLAY or
EMULATED.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from assurance.core.axes import TrialOutcome
from assurance.core.confirmation import ConfirmationAction

from gui.operator import status as st
from gui.operator.app import OperatorConsole
from gui.operator.sources import batch as batch_source
from gui.operator.sources import cockpit
from gui.operator.workspaces import batch_experiments as bx
from tests.gui.kernel_submission_support import (
    KernelSubmissionFixture,
    PIN_UTTERANCE,
    UNSERVABLE_UTTERANCE,
)

HAS_DISPLAY = bool(os.environ.get("DISPLAY"))

OBJECTIVE = "UeCellSteeringPinToCell"
CLOCK = "2026-08-21T09:00:00.000000Z"


def draft_for(utterance: str = PIN_UTTERANCE, *, repeats: str = "1"
              ) -> batch_source.PlanDraft:
    """A minimal admissible plan over the PIN_TO_CELL objective."""
    return batch_source.PlanDraft(
        objectives=OBJECTIVE,
        profiles=f"pin-target={utterance}",
        strategy="deterministic", repeats=repeats, seed="7",
        budget_cases="8")


class ThePlanFormCarriesSectionTwelvesFields(unittest.TestCase):

    def test_every_field_task_section_twelve_names_is_settable(self) -> None:
        keys = set(batch_source.PLAN_FIELD_KEYS)
        for required in ("objectives", "profiles", "strategy", "repeats",
                         "seed", "ordering", "warmup_s", "recovery_s",
                         "measurement_s", "observation_s", "hold_s",
                         "budget_cases", "budget_trials", "budget_harm",
                         "retries", "abort_on_error", "inclusion"):
            with self.subTest(field=required):
                self.assertIn(required, keys)

    def test_every_field_has_a_label_and_a_stated_meaning(self) -> None:
        for key, label, help_text in batch_source.PLAN_FIELDS:
            with self.subTest(field=key):
                self.assertTrue(label.strip())
                self.assertTrue(help_text.strip())

    def test_the_form_never_coerces_a_malformed_value_silently(self) -> None:
        draft = draft_for().with_field("repeats", "three")
        with self.assertRaises(batch_source.BatchRefused) as caught:
            draft.build()
        self.assertEqual(caught.exception.code, "PLAN_FIELD_NOT_AN_INTEGER")

    def test_an_unknown_field_is_refused_by_name(self) -> None:
        with self.assertRaises(batch_source.BatchRefused) as caught:
            draft_for().with_field("theta", "0.5")
        self.assertEqual(caught.exception.code, "UNKNOWN_PLAN_FIELD")

    def test_a_plan_with_no_objective_or_no_profile_is_refused(self) -> None:
        for field, code in (("objectives", "NO_OBJECTIVE"),
                            ("profiles", "NO_INTENT_PROFILE")):
            with self.subTest(field=field):
                with self.assertRaises(batch_source.BatchRefused) as caught:
                    draft_for().with_field(field, "").build()
                self.assertEqual(caught.exception.code, code)

    def test_a_matrix_larger_than_the_case_budget_is_refused(self) -> None:
        draft = draft_for(repeats="9").with_field("budget_cases", "4")
        with self.assertRaises(batch_source.BatchRefused) as caught:
            draft.build()
        self.assertEqual(caught.exception.code, "PLAN_NOT_ADMISSIBLE")


class TheBoundedPlanIsConfirmedExactlyOnce(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.session = batch_source.BatchSession(
            runs_root=self._tmp.name, session_factory=lambda case: None,
            mode="MOCK", draft=draft_for(repeats="2"))

    def test_the_cases_are_enumerated_before_anything_runs(self) -> None:
        """Bounded means countable, and the pane counts rather than claims."""
        view = self.session.view()
        self.assertEqual(view.case_count, 2)
        self.assertEqual(len(view.cases), 2)
        text = "\n".join(bx.case_lines(view))
        self.assertIn("2 case(s)", text)
        for case in view.cases:
            with self.subTest(case=case):
                self.assertIn(case, text)

    def test_the_confirmation_covers_the_plan_content_hash(self) -> None:
        view = self.session.view()
        record = self.session.confirm(now=CLOCK)
        self.assertEqual(record.action, ConfirmationAction.CONFIRM_BATCH_PLAN)
        self.assertEqual(record.confirmed_content_hash,
                         view.plan_content_hash)
        self.assertTrue(self.session.view().confirmation_valid)

    def test_editing_a_scope_field_invalidates_the_confirmation(self) -> None:
        self.session.confirm(now=CLOCK)
        self.session.edit("seed", "8")
        view = self.session.view()
        self.assertFalse(view.confirmation_valid)
        self.assertTrue(view.confirmation.changed_after_confirmation)
        self.assertEqual(view.stage, batch_source.STAGE_DRAFT)
        self.assertIn("confirm the plan again",
                      "\n".join(bx.confirmation_lines(view)))

    def test_start_is_refused_without_a_standing_confirmation(self) -> None:
        with self.assertRaises(batch_source.BatchRefused) as caught:
            self.session.start()
        self.assertEqual(caught.exception.code, "CONFIRM_BATCH_PLAN_REQUIRED")

    def test_start_is_refused_after_the_plan_moved(self) -> None:
        self.session.confirm(now=CLOCK)
        self.session.edit("repeats", "1")
        with self.assertRaises(batch_source.BatchRefused) as caught:
            self.session.start()
        self.assertEqual(caught.exception.code, "CONFIRM_BATCH_PLAN_REQUIRED")

    def test_the_controls_are_disabled_with_their_reason(self) -> None:
        view = self.session.view()
        controls = bx.control_enabled(view)
        self.assertTrue(controls["batch_confirm"][0])
        self.assertFalse(controls["batch_start"][0])
        self.assertIn("confirm the bounded plan", controls["batch_start"][1])
        self.session.confirm(now=CLOCK)
        controls = bx.control_enabled(self.session.view())
        self.assertTrue(controls["batch_start"][0])

    def test_an_inadmissible_draft_disables_both_controls_with_a_reason(
            self) -> None:
        self.session.edit("objectives", "")
        controls = bx.control_enabled(self.session.view())
        for key in ("batch_confirm", "batch_start"):
            with self.subTest(control=key):
                self.assertFalse(controls[key][0])
                self.assertTrue(controls[key][1])

    def test_a_live_session_cannot_be_batched_from_this_pane(self) -> None:
        session = batch_source.BatchSession(
            runs_root=self._tmp.name, session_factory=lambda case: None,
            mode="LIVE", draft=draft_for())
        session.confirm(now=CLOCK)
        with self.assertRaises(batch_source.BatchRefused) as caught:
            session.start()
        self.assertEqual(caught.exception.code,
                         "BATCH_MODE_NOT_HARDWARE_FREE")

    def test_a_console_with_no_kernel_session_refuses_by_name(self) -> None:
        session = batch_source.BatchSession(
            runs_root=self._tmp.name, session_factory=None, mode="MOCK",
            draft=draft_for())
        session.confirm(now=CLOCK)
        with self.assertRaises(batch_source.BatchRefused) as caught:
            session.start()
        self.assertEqual(caught.exception.code, "NO_KERNEL_SESSION")


class TheBatchDrivesTheSameKernelAsAnInteractiveRun(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fixture = KernelSubmissionFixture()
        #: One session, handed to the batch the way the console hands it its
        #: own: the point of the test is that this is *the* session, not a
        #: second one built for batching.
        self.kernel_session = self.fixture.session()
        self.batch = batch_source.BatchSession(
            runs_root=self._tmp.name,
            session_factory=lambda case: self.kernel_session,
            mode="MOCK", draft=draft_for())

    def run_batch(self):
        self.batch.confirm(now=CLOCK)
        return self.batch.start()

    def test_the_batch_case_ran_on_the_console_s_own_session(self) -> None:
        before = len(tuple(self.fixture.store.iterate()))
        self.run_batch()
        self.assertEqual(self.batch.executor.sessions, [self.kernel_session])
        self.assertIs(self.kernel_session.path.kernel, self.fixture.kernel)
        self.assertGreater(len(tuple(self.fixture.store.iterate())), before)

    def test_the_trial_the_batch_opened_is_in_the_kernel_s_own_ledger(
            self) -> None:
        self.run_batch()
        snapshot = cockpit.project_session(self.kernel_session)
        trial = snapshot.trial_safety.current_trial
        self.assertIsNotNone(trial)
        self.assertEqual(trial.outcome, TrialOutcome.SUCCESS.value)
        self.assertEqual(trial.trial_id, self.kernel_session.trial_id)

    def test_the_batch_record_repeats_the_kernel_s_verdict_and_nothing_else(
            self) -> None:
        """No second decision path: the record is the Kernel's own outcome."""
        view = self.run_batch()
        rows = json.loads(
            (Path(view.run_dir) / "normalized" / "trials.json").read_text(
                encoding="utf-8"))
        self.assertEqual(len(rows), 1)
        snapshot = cockpit.project_session(self.kernel_session)
        self.assertEqual(rows[0]["outcome"], "SUCCESS")
        self.assertEqual(rows[0]["validity"], "VALID")
        self.assertEqual(
            rows[0]["harmCharge"],
            sum(item.charged for item in snapshot.evidence.harm))
        self.assertEqual(rows[0]["confirmations"],
                         len(snapshot.evidence.confirmations))
        self.assertEqual(rows[0]["closureProgress"],
                         snapshot.evidence.closure_progress)

    def test_the_batch_writes_with_the_console_s_own_store_class(self) -> None:
        """"The same evidence store" is asserted by identity, not by shape."""
        from gui.operator.store import session_store as console_store
        from assurance.batch import runner as batch_runner

        self.assertIs(console_store.SessionStore, batch_runner.SessionStore)

    def test_the_run_directory_is_a_readable_session_store_run(self) -> None:
        view = self.run_batch()
        run_dir = Path(view.run_dir)
        manifest = json.loads((run_dir / "manifest.json").read_text(
            encoding="utf-8"))
        self.assertEqual(manifest["disposition"], "COMPLETED")
        self.assertEqual(manifest["mode"], "EMULATED")

    def test_all_eight_paper_grade_outputs_are_present_or_stated(self) -> None:
        view = self.run_batch()
        self.assertEqual(len(view.artifacts), len(batch_source.ARTIFACTS))
        for row in view.artifacts:
            with self.subTest(artifact=row.key):
                self.assertIn(row.status, (st.OK, st.UNSUPPORTED),
                              f"{row.key}: {row.reason}")
                if row.status == st.UNSUPPORTED:
                    self.assertTrue(row.reason,
                                    "an unsupported output must say why")

    def test_the_artifact_block_names_what_was_written(self) -> None:
        view = self.run_batch()
        text = "\n".join(bx.artifact_lines(view))
        self.assertIn("raw/batch/events.jsonl", text)
        self.assertIn("summary/batch-summary.tex", text)
        self.assertIn("figures/cdf.traceability.json", text)

    def test_the_raw_bundle_carries_the_kernel_s_own_events(self) -> None:
        view = self.run_batch()
        lines = (Path(view.run_dir) / "raw" / "batch" / "events.jsonl"
                 ).read_text(encoding="utf-8").strip().splitlines()
        kinds = {json.loads(line)["eventKind"] for line in lines}
        self.assertIn("TrialOpened", kinds)
        self.assertIn("TrialSettled", kinds)
        self.assertIn("EpochFrozen", kinds)

    def test_the_summary_claims_no_kpi_the_batch_did_not_measure(self) -> None:
        """Task section 9.13: a Kernel event count is not a KPM KPI."""
        view = self.run_batch()
        kpis = (view.summary or {}).get("kpis") or {}
        for name in ("KPM", "O1", "Core", "RAN", "UEApplication"):
            with self.subTest(kpi=name):
                self.assertIsNone(kpis.get(name),
                                  f"{name} was reported without being measured")

    def test_the_scope_is_locked_once_the_repetition_has_started(self) -> None:
        self.run_batch()
        self.assertTrue(self.batch.scope_locked)
        for method, args in ((self.batch.edit, ("seed", "9")),
                             (self.batch.confirm, ()),
                             (self.batch.start, ())):
            with self.subTest(method=method.__name__):
                with self.assertRaises(batch_source.BatchRefused) as caught:
                    method(*args)
                self.assertIn(caught.exception.code,
                              {"BATCH_SCOPE_LOCKED", "BATCH_ALREADY_RUN"})

    def test_a_locked_plan_disables_both_controls_with_the_reason(self) -> None:
        view = self.run_batch()
        controls = bx.control_enabled(view)
        for key in ("batch_confirm", "batch_start"):
            with self.subTest(control=key):
                self.assertFalse(controls[key][0])
                self.assertIn("scope cannot change", controls[key][1])
        self.assertIn("LOCKED", "\n".join(bx.plan_lines(view)))


class ARefusedCaseIsRecordedNotHidden(unittest.TestCase):

    def test_an_unrecognised_intent_lands_as_an_error_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = KernelSubmissionFixture()
            session = fixture.session()
            batch = batch_source.BatchSession(
                runs_root=tmp, session_factory=lambda case: session,
                mode="MOCK", draft=draft_for(UNSERVABLE_UTTERANCE))
            batch.confirm(now=CLOCK)
            view = batch.start()
            rows = json.loads(
                (Path(view.run_dir) / "normalized" / "trials.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["outcome"], "ERROR")
            self.assertEqual(rows[0]["validity"], "ERROR")
            counts = view.summary["trialCounts"]
            self.assertEqual(counts["error"], 1)
            self.assertEqual(counts["valid"], 0)
            raw = (Path(view.run_dir) / "raw" / "batch" / "events.jsonl"
                   ).read_text(encoding="utf-8")
            self.assertIn("CaseRefused", raw)
            self.assertIn("INTENT_NOT_RECOGNISED", raw)


class TheConsoleReachesTheBatchPane(unittest.TestCase):

    def test_the_console_routes_every_action_the_pane_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            try:
                routed = set(console.action_keys())
                for key, _label in bx.BATCH_ACTIONS:
                    with self.subTest(action=key):
                        self.assertIn(key, routed)
                self.assertIn("batch_edit", routed)
            finally:
                console.shutdown()

    def test_editing_through_the_console_reaches_the_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            try:
                console.handle_action("batch_edit", f"objectives={OBJECTIVE}")
                self.assertEqual(console.batch.draft.objectives, OBJECTIVE)
            finally:
                console.shutdown()

    def test_an_unknown_plan_field_is_refused_with_a_stated_reason(
            self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            try:
                console.handle_action("batch_edit", "theta=0.5")
                titles = [event.title for event in console.controller.timeline]
                self.assertTrue(any("UNKNOWN_PLAN_FIELD" in title
                                    for title in titles), titles)
            finally:
                console.shutdown()

    def test_binding_sessions_is_a_composition_act_not_a_console_choice(
            self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            console = OperatorConsole(runs_root=tmp)
            try:
                self.assertIsNone(console.batch_session_factory)
                factory = lambda case: None                # noqa: E731
                console.attach_batch_sessions(factory, mode="MOCK")
                self.assertIs(console.batch_session_factory, factory)
                self.assertEqual(console.batch.mode, "MOCK")
            finally:
                console.shutdown()

    @unittest.skipUnless(HAS_DISPLAY, "needs a display")
    def test_the_pane_builds_its_form_and_its_two_controls(self) -> None:
        import tkinter as tk

        root = tk.Tk()
        try:
            pane = bx.BatchExperimentsWorkspace()
            pane.build(root)
            for key, _label, _help in batch_source.PLAN_FIELDS:
                with self.subTest(field=key):
                    self.assertIn(key, pane.entries)
            for key, _label in bx.BATCH_ACTIONS:
                with self.subTest(control=key):
                    self.assertIn(key, pane.buttons)
            pane.destroy()
        finally:
            root.destroy()

    @unittest.skipUnless(HAS_DISPLAY, "needs a display")
    def test_a_locked_plan_disables_the_form_on_screen(self) -> None:
        import tkinter as tk

        root = tk.Tk()
        try:
            pane = bx.BatchExperimentsWorkspace()
            pane.build(root)
            pane.on_batch_view(batch_source.BatchConsoleView(
                mode="MOCK", stage=batch_source.STAGE_RUNNING,
                draft=draft_for(), scope_locked=True))
            for key, entry in pane.entries.items():
                with self.subTest(field=key):
                    self.assertEqual(str(entry.cget("state")), "disabled")
            pane.destroy()
        finally:
            root.destroy()


if __name__ == "__main__":                                 # pragma: no cover
    unittest.main()
