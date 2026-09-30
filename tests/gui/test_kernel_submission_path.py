"""The Cockpit's Kernel submission path, end to end and hardware-free.

Gate 3's acceptance requires PIN_TO_CELL to enter through *the* Cockpit
submission path rather than through a test harness that happens to reach the
same Kernel.  This module drives that path: a sentence an operator types, the
Intent Agent's reading of it, the frozen contract instance the epoch already
holds, one confirmation over that instance's content hash, and then the
Kernel's own trial loop polled to a terminal state.

Nothing here contacts anything.  The deployment behind the Write Gateway is the
mock actuation adapter, the measurement series is scripted, and the clock is
the deterministic stepping clock -- so every assertion below is about what the
Kernel decided, never about how long anything took.

The widget half of the same path -- that an operator can reach all of this by
pressing buttons -- is ``test_kernel_submission_console.py``.  Both halves are
needed: a source that works and a console nobody can drive it from is not a
submission path.
"""

from __future__ import annotations

import ast
import dataclasses
import unittest
from pathlib import Path

from assurance.core.axes import (
    CaseTermination,
    EvidenceCellStatus,
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
    TrialOutcome,
)
from assurance.core.confirmation import (
    FORBIDDEN_CONFIRMATION_FIELDS,
    ConfirmationAction,
    ConfirmationRecord,
)
from assurance.core.states import StopReason, TrialState
from assurance.gateway.mock_adapter import FaultInjection

from gui.operator import status as st
from gui.operator.sources import kernel_live as kl
from tests.assurance.pin_to_cell_support import BASELINE_CONFIG, HOME_NCI, TARGET_NCI
from tests.gui.kernel_submission_support import (
    HOME_UTTERANCE,
    PIN_TO_CELL_GRAMMAR,
    PIN_UTTERANCE,
    UNSERVABLE_UTTERANCE,
    KernelSubmissionFixture,
)

SOURCE = Path(kl.__file__)


class NaturalLanguageToTerminal(KernelSubmissionFixture, unittest.TestCase):
    """One sentence, one confirmation, one Kernel terminal."""

    def test_a_pinned_ue_runs_from_a_sentence_to_a_finalized_success(self) -> None:
        session = self.session()

        session.draft(PIN_UTTERANCE)
        session.confirm()
        view = session.start()

        self.assertEqual(view.stage, kl.STAGE_TERMINAL)
        self.assertEqual(view.settlement.trial_state,
                         TrialState.SETTLED_SUCCESS.value)
        self.assertEqual(view.settlement.outcome, TrialOutcome.SUCCESS.value)
        self.assertEqual(view.settlement.evidence_status,
                         EvidenceCellStatus.CLOSED_PASS.value)
        # The change is live on the deployment, and the case closes on it.
        self.assertEqual(self.adapter.snapshot(),
                         {"servingCell": str(TARGET_NCI)})
        self.assertEqual(session.abort(), CaseTermination.SUCCESS)

    def test_success_is_a_readback_and_a_finalize_not_an_acknowledgement(self) -> None:
        """Gate 3 acceptance: ``ACK만으로 success 처리하지 않음``."""
        session = self.session()
        session.draft(PIN_UTTERANCE)
        session.confirm()

        view = session.start()

        operations = [name for name, _outcome in
                      view.settlement.gateway_operations]
        self.assertEqual(operations,
                         ["PREPARE", "READY", "COMMIT",
                          "CONFIGURATION_REREAD", "FINALIZE_LIVE"])
        self.assertTrue(view.axes.hold_complete)

    def test_the_published_stages_only_ever_advance(self) -> None:
        """What the operator watches never runs backwards."""
        stages = []
        session = self.session(publish=lambda _c, view: stages.append(view.stage))

        session.draft(PIN_UTTERANCE)
        session.confirm()
        session.start()

        positions = [kl.STAGE_ORDER.index(stage) for stage in stages]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(stages[0], kl.STAGE_DRAFTED)
        self.assertEqual(stages[-1], kl.STAGE_TERMINAL)

    def test_the_preview_names_the_candidate_the_epoch_already_froze(self) -> None:
        session = self.session()

        preview = session.draft(PIN_UTTERANCE)

        frozen = self.kernel.current_catalog().candidates[0]
        self.assertEqual(preview.candidate_id, frozen.candidate_id)
        self.assertEqual(dict(preview.parameters), dict(frozen.parameters))
        self.assertEqual(preview.semantic_hash, frozen.semantic_hash)
        self.assertEqual(preview.epoch_hash, self.path.epoch_hash())
        self.assertTrue(preview.is_servable)

    def test_the_agent_reading_is_draft_and_only_a_confirmation_makes_it_normative(
            self) -> None:
        session = self.session()

        preview = session.draft(PIN_UTTERANCE)
        self.assertEqual(preview.document_status, kl.DRAFT)
        self.assertEqual(preview.origin, kl.ORIGIN_AGENT)
        self.assertTrue(all("[DRAFT]" in text
                            for text in preview.draft_constraints))

        instance = session.confirm()
        self.assertEqual(instance.document_status, kl.NORMATIVE)
        self.assertEqual(instance.origin, kl.ORIGIN_OPERATOR)
        self.assertEqual(instance.content_hash, preview.content_hash())

    def test_an_objective_the_registry_does_not_recognise_is_refused_by_name(
            self) -> None:
        session = self.session()

        with self.assertRaises(kl.SubmissionRefused) as caught:
            session.draft(UNSERVABLE_UTTERANCE)

        self.assertEqual(caught.exception.reason, "INTENT_NOT_RECOGNISED")
        self.assertIsNone(session.preview)

    def test_a_drifting_ue_rolls_back_and_settles_non_success(self) -> None:
        """The excursion the old end-of-episode readback could not see."""
        session = self.session(
            observed=(TARGET_NCI, TARGET_NCI, HOME_NCI, TARGET_NCI, TARGET_NCI))
        session.draft(PIN_UTTERANCE)
        session.confirm()

        view = session.start()

        self.assertEqual(view.settlement.outcome, TrialOutcome.FAIL.value)
        self.assertEqual(view.settlement.stop_reason,
                         StopReason.SEMANTIC_NON_SUCCESS.value)
        self.assertEqual(view.settlement.evidence_status,
                         EvidenceCellStatus.CLOSED_FAIL.value)
        self.assertEqual(dict(self.adapter.snapshot()), dict(BASELINE_CONFIG))
        operations = [name for name, _outcome in
                      view.settlement.gateway_operations]
        self.assertIn("STOP", operations)
        self.assertIn("REVERSE_ROLLBACK", operations)
        self.assertIn("RECOVERY_CONFIRM", operations)

    def test_the_operator_emergency_stop_reaches_rollback_and_settlement(
            self) -> None:
        """Task section 9.10's chain, raised from the console while it runs."""
        pressed = {}

        def press_at_second_poll(_channel, view):
            if view.stage == kl.STAGE_OBSERVING and view.poll_count == 2:
                pressed["at"] = view.poll_count
                session.request_emergency_stop()

        session = self.session(publish=press_at_second_poll)
        session.draft(PIN_UTTERANCE)
        session.confirm()

        view = session.start()

        self.assertEqual(pressed, {"at": 2})
        self.assertEqual(view.settlement.outcome,
                         TrialOutcome.OPERATOR_ABORTED.value)
        self.assertEqual(view.settlement.trial_state,
                         TrialState.SETTLED_NON_SUCCESS.value)
        # Kernel OPERATOR_ABORT -> gateway stop -> rollback -> recovery, in the
        # trial's own recorded transitions.
        kinds = [envelope.event_kind for envelope in self.store.iterate()]
        states = [envelope.payload.get("to")
                  for envelope in self.store.iterate()
                  if envelope.event_kind == "TrialStateChanged"]
        self.assertIn(TrialState.STOPPING.value, states)
        self.assertIn(TrialState.REVERSE_ROLLBACK.value, states)
        self.assertIn(TrialState.RECOVERY_VERIFYING.value, states)
        self.assertIn("TransactionResolved", kinds)
        self.assertIn("TrialSettled", kinds)
        # And the deployment is back where it started.
        self.assertEqual(dict(self.adapter.snapshot()), dict(BASELINE_CONFIG))
        self.assertEqual(session.abort(), CaseTermination.OPERATOR_ABORT)

    def test_the_stop_reason_recorded_is_the_operator_abort(self) -> None:
        session = self.session(
            publish=lambda _c, view: (session.request_emergency_stop()
                                      if view.poll_count == 1 else None))
        session.draft(PIN_UTTERANCE)
        session.confirm()
        session.start()

        reasons = [envelope.payload.get("reason")
                   for envelope in self.store.iterate()
                   if envelope.event_kind == "TrialStateChanged"]
        self.assertIn(StopReason.OPERATOR_ABORT.value, reasons)

    def test_an_emergency_stop_before_anything_is_applied_starts_nothing(
            self) -> None:
        session = self.session()
        session.draft(PIN_UTTERANCE)
        session.confirm()

        session.request_emergency_stop()
        with self.assertRaises(kl.SubmissionRefused) as caught:
            session.start()

        self.assertEqual(caught.exception.reason, "EMERGENCY_STOP_REQUESTED")
        self.assertIsNone(session.trial_id)
        self.assertEqual(dict(self.adapter.snapshot()), dict(BASELINE_CONFIG))

    def test_emergency_stop_with_no_trial_in_flight_is_a_view_not_a_write(
            self) -> None:
        session = self.session()
        session.draft(PIN_UTTERANCE)

        view = session.emergency_stop()

        self.assertTrue(session.emergency_stop_requested)
        self.assertIsNone(view.settlement)
        self.assertEqual(self.adapter.commands, [])

    def test_abort_refuses_while_a_trial_is_in_flight_and_names_the_other_control(
            self) -> None:
        refusals = []

        def abort_at_second_poll(_channel, view):
            if view.stage == kl.STAGE_OBSERVING and view.poll_count == 2:
                try:
                    session.abort()
                except kl.SubmissionRefused as exc:
                    refusals.append(exc.reason)

        session = self.session(publish=abort_at_second_poll)
        session.draft(PIN_UTTERANCE)
        session.confirm()
        view = session.start()

        self.assertEqual(refusals, ["TRIAL_IN_FLIGHT"])
        # And the trial the operator did not stop still finished normally.
        self.assertEqual(view.settlement.outcome, TrialOutcome.SUCCESS.value)

    def test_a_second_start_on_the_same_session_is_refused(self) -> None:
        session = self.session()
        session.draft(PIN_UTTERANCE)
        session.confirm()
        session.start()

        with self.assertRaises(kl.SubmissionRefused) as caught:
            session.start()

        self.assertEqual(caught.exception.reason, "TRIAL_ALREADY_RUN")


class ConfirmationModel(KernelSubmissionFixture, unittest.TestCase):
    """Design section 5: a content hash, and a new click when it changes."""

    def test_the_record_carries_no_identity_of_any_kind(self) -> None:
        session = self.session()
        session.draft(PIN_UTTERANCE)

        record = session.confirm().confirmation

        fields = {field.name for field in dataclasses.fields(record)}
        self.assertEqual(fields & FORBIDDEN_CONFIRMATION_FIELDS, set())
        self.assertEqual(record.action,
                         ConfirmationAction.REVIEW_AND_CONFIRM)
        self.assertTrue(record.timestamp.endswith("Z"))
        self.assertTrue(record.event_id)

    def test_changing_the_content_invalidates_the_confirmation(self) -> None:
        session = self.session()
        first = session.draft(PIN_UTTERANCE)
        session.confirm()

        second = session.draft(HOME_UTTERANCE)

        self.assertNotEqual(first.content_hash(), second.content_hash())
        self.assertIsNone(session.confirmed)
        invalidated = session.view().invalidated_confirmations
        self.assertEqual(len(invalidated), 1)
        self.assertTrue(invalidated[0].changed_after_confirmation)
        self.assertFalse(session.view().confirmation_valid)

    def test_an_invalidated_confirmation_cannot_start_a_trial(self) -> None:
        session = self.session()
        session.draft(PIN_UTTERANCE)
        stale = session.confirm()
        session.draft(HOME_UTTERANCE)

        with self.assertRaises(kl.ConfirmationRequired):
            session.start()
        with self.assertRaises(kl.ConfirmationRequired):
            session.start(stale)
        self.assertIsNone(session.trial_id)

    def test_re_confirming_the_new_content_starts_it(self) -> None:
        session = self.session()
        session.draft(PIN_UTTERANCE)
        session.confirm()
        session.draft(HOME_UTTERANCE)
        session.draft(PIN_UTTERANCE)

        session.confirm()
        view = session.start()

        self.assertEqual(view.settlement.outcome, TrialOutcome.SUCCESS.value)
        self.assertTrue(view.confirmation_valid)

    def test_starting_without_any_confirmation_is_refused(self) -> None:
        session = self.session()
        session.draft(PIN_UTTERANCE)

        with self.assertRaises(kl.ConfirmationRequired):
            session.start()

    def test_a_stop_control_cannot_stand_in_for_a_confirmation(self) -> None:
        session = self.session()
        preview = session.draft(PIN_UTTERANCE)
        record = ConfirmationRecord(
            confirmed_object_type="ContractPreview",
            confirmed_content_hash=preview.content_hash(),
            event_id="confirm/abort", timestamp=self.clock(),
            action=ConfirmationAction.ABORT)

        with self.assertRaises(kl.ConfirmationRequired):
            kl.promote_to_normative(preview, confirmation=record)

    def test_a_confirmation_of_other_content_cannot_be_reused(self) -> None:
        session = self.session()
        pinned = session.draft(PIN_UTTERANCE)
        instance = session.confirm()
        home = session.draft(HOME_UTTERANCE)

        with self.assertRaises(kl.ConfirmationRequired):
            kl.promote_to_normative(home, confirmation=instance.confirmation)
        self.assertNotEqual(home.content_hash(), pinned.content_hash())

    def test_promoting_without_a_confirmation_is_refused(self) -> None:
        session = self.session()
        preview = session.draft(PIN_UTTERANCE)

        with self.assertRaises(kl.ConfirmationRequired):
            kl.promote_to_normative(preview, confirmation=None)

    def test_promote_to_normative_is_the_only_promotion(self) -> None:
        """Blocking item 6: one entry point, checked mechanically.

        A second construction site would be a second way for DRAFT numbers to
        become NORMATIVE, and the whole rule is that there is only one.
        """
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
        sites = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name)
                 and node.func.id == "NormativeContractInstance"]
        self.assertEqual(len(sites), 1)
        enclosing = [node.name for node in ast.walk(tree)
                     if isinstance(node, ast.FunctionDef)
                     and any(site in ast.walk(node) for site in sites)]
        self.assertEqual(enclosing, ["promote_to_normative"])


class AxisSeparation(KernelSubmissionFixture, unittest.TestCase):
    """Design section 8: four axes, never collapsed into one badge."""

    def test_the_four_axes_have_four_fields_and_four_tables(self) -> None:
        fields = {field.name for field in dataclasses.fields(kl.AxisView)}

        for axis in ("execution_validity", "measurement_sufficiency",
                     "predicate_verdicts", "trial_outcome"):
            self.assertIn(axis, fields)
        self.assertEqual(set(kl.AXIS_TABLES),
                         {"execution_validity", "measurement_sufficiency",
                          "predicate_verdict", "trial_outcome"})
        # Distinct tables, not four names for one.
        self.assertEqual(
            len({id(table) for table in kl.AXIS_TABLES.values()}), 4)

    def test_an_execution_error_is_not_reported_as_a_failed_predicate(self) -> None:
        session = self.session(faults=FaultInjection(fail_prepare=True))
        session.draft(PIN_UTTERANCE)
        session.confirm()

        view = session.start()

        self.assertEqual(view.settlement.outcome,
                         TrialOutcome.EXEC_ERROR.value)
        # Nothing about the candidate was tested, so no predicate has a verdict.
        self.assertEqual(view.axes.predicate_verdicts, ())
        self.assertEqual(view.axes.predicate_verdict,
                         PredicateVerdict.NOT_EVALUATED.value)
        self.assertEqual(view.axes.measurement_sufficiency,
                         MeasurementSufficiency.NOT_EVALUATED.value)
        resolved = view.axes.resolved()
        self.assertEqual(resolved["trial_outcome"].status, st.ERROR)
        self.assertEqual(resolved["predicate_verdict"].status, st.UNKNOWN)

    def test_a_valid_sufficient_trace_with_a_failed_predicate_shows_all_three(
            self) -> None:
        session = self.session(
            observed=(TARGET_NCI, TARGET_NCI, HOME_NCI, TARGET_NCI, TARGET_NCI))
        session.draft(PIN_UTTERANCE)
        session.confirm()

        view = session.start()

        self.assertEqual(view.axes.execution_validity,
                         ExecutionValidity.VALID.value)
        self.assertEqual(view.axes.measurement_sufficiency,
                         MeasurementSufficiency.SUFFICIENT.value)
        self.assertEqual(dict(view.axes.predicate_verdicts), {
            "serving-cell-is-pinned-throughout": PredicateVerdict.FAIL.value,
            "serving-cell-is-pinned-only": PredicateVerdict.PASS.value,
        })
        resolved = view.axes.resolved()
        self.assertEqual(resolved["execution_validity"].status, st.OK)
        self.assertEqual(resolved["predicate_verdict"].status, st.ERROR)

    def test_a_disagreeing_mandatory_pair_stays_a_pair(self) -> None:
        session = self.session(observed=(HOME_NCI,) * 5)
        session.draft(PIN_UTTERANCE)
        session.confirm()

        view = session.start()

        self.assertEqual(len(view.axes.predicate_verdicts), 2)
        self.assertEqual(view.axes.predicate_verdict,
                         PredicateVerdict.FAIL.value)

    def test_no_axis_table_maps_an_unrecognised_value_onto_ok(self) -> None:
        for axis in kl.AXIS_TABLES:
            with self.subTest(axis=axis):
                resolved = kl.axis_status(axis, "SOMETHING_NEW")
                self.assertEqual(resolved.status, st.UNKNOWN)
                self.assertEqual(resolved.reason, st.UNMAPPED_REASON)
        self.assertEqual(kl.axis_status("not_an_axis", "VALID").status,
                         st.UNKNOWN)

    def test_indeterminate_is_promoted_to_neither_pass_nor_fail(self) -> None:
        self.assertEqual(
            kl.PREDICATE_VERDICT_STATUS[PredicateVerdict.INDETERMINATE.value],
            st.UNKNOWN)
        self.assertEqual(
            kl.TRIAL_OUTCOME_STATUS[TrialOutcome.INDETERMINATE.value],
            st.UNKNOWN)


class ModeMarking(KernelSubmissionFixture, unittest.TestCase):
    """Task section 9.7: LIVE and everything else are visibly different."""

    def test_only_live_resolves_to_ok(self) -> None:
        self.assertEqual(kl.mode_badge(kl.MODE_LIVE).status, st.OK)
        for mode in (kl.MODE_REPLAY, kl.MODE_MOCK):
            with self.subTest(mode=mode):
                self.assertNotEqual(kl.mode_badge(mode).status, st.OK)

    def test_every_mode_carries_a_distinct_glyph_and_a_stated_reason(self) -> None:
        glyphs = {kl.mode_badge(mode).glyph for mode in kl.KERNEL_SESSION_MODES}

        self.assertEqual(len(glyphs), len(kl.KERNEL_SESSION_MODES))
        for mode in kl.KERNEL_SESSION_MODES:
            with self.subTest(mode=mode):
                self.assertTrue(kl.mode_badge(mode).reason)

    def test_an_unrecognised_mode_fails_closed(self) -> None:
        badge = kl.mode_badge("PROBABLY_LIVE")

        self.assertEqual(badge.status, st.UNKNOWN)
        self.assertEqual(badge.reason, st.UNMAPPED_REASON)

    def test_a_session_cannot_be_built_in_an_unrecognised_mode(self) -> None:
        with self.assertRaises(kl.SubmissionRefused) as caught:
            kl.KernelSubmissionSession(
                path=self.build(), objective_registry=PIN_TO_CELL_GRAMMAR,
                mode="PROBABLY_LIVE")

        self.assertEqual(caught.exception.reason, "UNKNOWN_SESSION_MODE")

    def test_the_mock_session_never_claims_to_be_live(self) -> None:
        session = self.session()

        view = session.view()

        self.assertFalse(view.is_live)
        self.assertEqual(view.mode, kl.MODE_MOCK)
        self.assertIn("no O-RAN endpoint", view.mode_status.reason)


class PollingRegime(KernelSubmissionFixture, unittest.TestCase):
    """Blocking item 7: finite polling derived from the frozen contracts."""

    def test_the_cadence_and_deadline_come_from_the_measurement_contracts(
            self) -> None:
        self.build()

        plan = kl.polling_plan(self.kernel)

        # counter cadence 1000 ms, hold 3000 ms, window 3000 ms, freshness
        # 2000 ms -- every number here is in pin_to_cell_support's contracts.
        self.assertEqual(plan.cadence_ms, 1000)
        self.assertEqual(plan.hold_ms, 3000)
        self.assertEqual(plan.deadline_ms, 8000)
        self.assertEqual(plan.observation_polls, 4)
        self.assertEqual(plan.max_polls, 9)

    def test_a_completed_run_stops_well_inside_the_deadline(self) -> None:
        session = self.session()
        session.draft(PIN_UTTERANCE)
        session.confirm()

        view = session.start()

        self.assertEqual(view.poll_count, 5)
        self.assertLess(view.poll_count, view.max_polls)
        self.assertEqual(view.cadence_ms, 1000)

    def test_polling_is_finite_when_no_terminal_state_ever_appears(self) -> None:
        """A path that never settles must still stop the console polling."""
        session = self.session()
        session.draft(PIN_UTTERANCE)
        session.confirm()
        plan = kl.polling_plan(self.kernel)
        session.path = _NeverSettles(self.path)

        view = session.start()

        self.assertEqual(view.poll_count, plan.max_polls)
        self.assertEqual(view.refusal, "POLL_DEADLINE_EXCEEDED")
        self.assertIsNone(view.settlement)

    def test_an_epoch_with_no_measurement_contract_has_no_cadence(self) -> None:
        with self.assertRaises(kl.SubmissionRefused) as caught:
            kl.polling_plan(_EmptyKernel())

        self.assertEqual(caught.exception.reason,
                         "NO_MEASUREMENT_CONTRACT_IN_EPOCH")

    def test_a_plan_with_no_cadence_is_refused_rather_than_defaulted(self) -> None:
        with self.assertRaises(kl.SubmissionRefused):
            kl.PollingPlan(cadence_ms=0, hold_ms=1000, deadline_ms=2000)
        with self.assertRaises(kl.SubmissionRefused):
            kl.PollingPlan(cadence_ms=1000, hold_ms=5000, deadline_ms=1000)

    def test_the_source_reads_the_kernel_only_through_its_reduced_state(
            self) -> None:
        """No subscription, no callback, no second entry into the Kernel."""
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
        reached = {
            node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "kernel"}

        self.assertEqual(reached, {"reduced_state"})


class NoOverrideFromTheConsole(KernelSubmissionFixture, unittest.TestCase):
    """Task section 9.9: the GUI cannot edit what the Kernel decided."""

    #: Every Kernel method that changes a verdict, a closure, a charge, a
    #: release, a rollback or a terminal state.  The console may not call one.
    FORBIDDEN_CALLS = frozenset({
        "admit_contract", "advance_trial", "charge_harm", "close_evidence",
        "decide_trial", "evaluate_trial", "freeze_epoch", "ingest_raw_sample",
        "issue_token", "open_case", "open_trial", "record_commit_readiness",
        "record_gateway_result", "release_target", "reserve",
        "resolve_recovery", "settle_trial", "stage_actuation_plan",
        "terminate_case",
    })

    GUI_MODULES = (
        Path(kl.__file__),
        Path(__file__).resolve().parents[2] / "gui" / "operator" /
        "workspaces" / "contract_studio.py",
    )

    def test_no_console_module_calls_a_kernel_mutator(self) -> None:
        for path in self.GUI_MODULES:
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"),
                                 filename=str(path))
                called = {node.func.attr for node in ast.walk(tree)
                          if isinstance(node, ast.Call)
                          and isinstance(node.func, ast.Attribute)}
                self.assertEqual(called & self.FORBIDDEN_CALLS, set())

    def test_every_published_view_model_is_frozen(self) -> None:
        for view_type in (kl.ContractPreview, kl.NormativeContractInstance,
                          kl.AxisView, kl.SettlementView, kl.KernelSessionView,
                          kl.PollingPlan):
            with self.subTest(view=view_type.__name__):
                self.assertTrue(
                    view_type.__dataclass_params__.frozen)

    def test_a_settled_view_cannot_be_edited_into_a_success(self) -> None:
        session = self.session(observed=(HOME_NCI,) * 5)
        session.draft(PIN_UTTERANCE)
        session.confirm()
        view = session.start()

        self.assertEqual(view.settlement.outcome, TrialOutcome.FAIL.value)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            view.settlement.outcome = TrialOutcome.SUCCESS.value
        with self.assertRaises(dataclasses.FrozenInstanceError):
            view.axes.execution_validity = ExecutionValidity.VALID.value
        # And re-reading the session still reports what the Kernel decided.
        self.assertEqual(session.view().settlement.outcome,
                         TrialOutcome.FAIL.value)


class BoundaryScope(unittest.TestCase):
    """The new console files are inside the gate that guards the old ones."""

    def _sources(self):
        from tests.test_gui_boundary_scan import _python_sources

        return _python_sources()

    def test_the_new_modules_are_walked_by_the_boundary_scan(self) -> None:
        scanned = {str(path) for path in self._sources()}

        for module in NoOverrideFromTheConsole.GUI_MODULES:
            with self.subTest(module=module.name):
                self.assertIn(str(module), scanned)

    def test_the_new_modules_pass_the_boundary_scan(self) -> None:
        from tests.test_gui_boundary_scan import (_boundary_violations,
                                                  _raw_text_matches,
                                                  _secret_matches)

        modules = list(NoOverrideFromTheConsole.GUI_MODULES)
        self.assertEqual(_boundary_violations(modules), [])
        self.assertEqual(_raw_text_matches(modules), [])
        self.assertEqual(_secret_matches(modules), [])

    def test_the_kernel_source_reaches_the_kernel_only_through_its_facade(
            self) -> None:
        """No transport, no gateway, no R1 client, no legacy coordinator.

        The session is *handed* a vertical path; it never builds one, which is
        why importing an adapter or a client here would be a defect rather than
        a convenience.
        """
        forbidden = ("assurance.gateway", "assurance.kernel", "oran.",
                     "coordinator.", "executor.", "collectors.", "decision.")
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                imported.append(node.module or "")

        for name in imported:
            with self.subTest(imported=name):
                self.assertFalse(
                    any(name == item.rstrip(".") or name.startswith(item)
                        for item in forbidden),
                    f"{name} is not the Kernel facade")


class _NeverSettles:
    """A vertical path whose trial never reaches a terminal state.

    Delegates everything real to the wrapped path and lies about exactly one
    thing: :meth:`conclude_trial` returns no report.  That is the shape of a
    deployment that stopped answering after the change was applied, and the
    console must stop polling it rather than wait forever.
    """

    def __init__(self, path) -> None:
        self._path = path

    def __getattr__(self, name):
        return getattr(self._path, name)

    def begin_trial(self, candidate_id):
        return "trial/never-settles", None

    def observe(self, **_kwargs):
        return None

    def conclude_trial(self, *_args, **_kwargs):
        return None


class _EmptyKernel:
    """A Kernel whose epoch froze no measurement contract."""

    def reduced_state(self):
        return {"contracts": {}}


if __name__ == "__main__":
    unittest.main()
