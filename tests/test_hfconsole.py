"""Hardware-free console composition — verification.

Proves that ``tools/hfconsole`` gives an operator a working hardware-free
intent path (draft -> confirm -> start -> terminal) through the *real* Kernel,
Write Gateway and epoch machinery over the mock actuation adapter, while:

* the session is ``MODE_MOCK`` and never ``LIVE`` (so a run is never OTA
  evidence);
* the default console composition (``main.build_console``) still attaches
  nothing and opens Disconnected — the hardware-free session is attached only
  when explicitly requested.

Hardware-free by construction: no socket, process, model or radio.
"""

from __future__ import annotations

import unittest

from assurance.objectives import FAMILY_MODULES, RegistryError, record_for
from gui.operator.sources.kernel_live import MODE_LIVE, MODE_MOCK

from tools.hfconsole import (
    build_hardware_free_runtime,
    build_hardware_free_session,
)
from tools.hfconsole.build import (
    HardwareFreeConsoleError,
    submittable_families,
)


class SubmittableFamiliesFollowTheRegistry(unittest.TestCase):

    def test_every_listed_family_is_submittable_in_the_registry(self) -> None:
        for family in submittable_families():
            self.assertTrue(record_for(family).deployment_capability.submittable,
                            f"{family} listed but not submittable")

    def test_a_non_submittable_family_is_excluded(self) -> None:
        # SliceSLATarget is a module but is not submittable on this deployment.
        listed = submittable_families()
        for family in FAMILY_MODULES:
            try:
                submittable = record_for(family).deployment_capability.submittable
            except RegistryError:
                continue
            if not submittable:
                self.assertNotIn(family, listed)


class TheSessionIsHardwareFreeNotLive(unittest.TestCase):

    def test_session_mode_is_mock(self) -> None:
        composed = build_hardware_free_session("UELevelTarget")
        self.assertEqual(composed.session.mode, MODE_MOCK)
        self.assertNotEqual(composed.session.mode, MODE_LIVE)

    def test_view_is_never_live(self) -> None:
        composed = build_hardware_free_session("UELevelTarget")
        view = composed.session.start(
            composed.session.confirm(composed.session.draft(composed.utterance)))
        self.assertFalse(view.is_live)
        self.assertEqual(view.mode, MODE_MOCK)


class TheRoundTripReachesATerminalOverTheRealKernel(unittest.TestCase):

    def test_draft_confirm_start_reaches_a_terminal_with_valid_execution(self) -> None:
        composed = build_hardware_free_session("UELevelTarget")
        session = composed.session
        preview = session.draft(composed.utterance)
        self.assertTrue(preview.content_hash())
        instance = session.confirm(preview)
        view = session.start(instance)
        self.assertEqual(view.stage, "TERMINAL")
        # The Kernel admitted and the Write Gateway applied over the mock
        # adapter: execution is VALID even though, with no injected samples, the
        # predicate is INDETERMINATE. That is the honest hardware-free plumbing
        # result, not a masked failure.
        self.assertEqual(view.axes.execution_validity, "VALID")
        self.assertIsNotNone(view.settlement)

    def test_several_submittable_objectives_compose_and_run(self) -> None:
        for family in ("UELevelTarget", "QoSTarget", "QoSandTSP"):
            with self.subTest(family=family):
                composed = build_hardware_free_session(family)
                view = composed.session.start(
                    composed.session.confirm(
                        composed.session.draft(composed.utterance)))
                self.assertEqual(view.mode, MODE_MOCK)
                self.assertEqual(view.stage, "TERMINAL")

    def test_a_custom_utterance_is_accepted(self) -> None:
        composed = build_hardware_free_session(
            "UELevelTarget",
            utterance="Hold the UE-level serving cell at 87654321 nci for ueId=131")
        self.assertIn("87654321", composed.utterance)
        view = composed.session.start(
            composed.session.confirm(composed.session.draft(composed.utterance)))
        self.assertEqual(view.stage, "TERMINAL")


class UnknownFamiliesAreRefused(unittest.TestCase):

    def test_unknown_family_raises(self) -> None:
        with self.assertRaises(HardwareFreeConsoleError):
            build_hardware_free_runtime("NoSuchObjective")


class TheDefaultConsoleStillAttachesNothing(unittest.TestCase):

    def test_build_console_opens_disconnected_with_no_session(self) -> None:
        import main

        console = main.build_console()
        # No hardware-free (or any) session is attached by the default path;
        # attaching one is an explicit, separate act (main --hardware-free).
        self.assertIsNone(console.kernel_session)

    def test_headless_hardware_free_returns_zero(self) -> None:
        import main

        code = main.run_headless_hardware_free(
            objective="UELevelTarget", utterance=None)
        self.assertEqual(code, 0)


class TheXAppCoordinationRunsHardwareFree(unittest.TestCase):
    """The verified xApp layer (composition + specialist actuation) over the
    in-memory store, gated by a permit — connected to the objective, no radio."""

class ControlledUeReachesTheHardwareFreeRun(unittest.TestCase):
    """WP-B3 addendum: the operator surface can name the UE a cap acts on.

    Before this, every ``--cmd``/GUI intent was steering-only: the controlled
    UE existed as a Python argument and nothing on the operator's surface could
    say it.  Naming it is what composes the supplementary control, so these
    tests are about the three places it can be named and the two it cannot.
    """

    def test_naming_no_controlled_ue_stays_steering_only(self) -> None:
        from tools.hfconsole import build_hardware_free_session

        composed = build_hardware_free_session("QoSTarget")
        self.assertIsNone(composed.cap)
        self.assertNotIn("cap controlledUeId=", composed.utterance)

    def test_the_flag_names_it(self) -> None:
        from tools.hfconsole import build_hardware_free_session

        composed = build_hardware_free_session(
            "QoSTarget", controlled_amf_ue_ngap_id=132)
        self.assertIsNotNone(composed.cap)
        self.assertEqual(composed.cap.request.controlled_ue_scope_id, "132")
        # The generated sentence says what the session would do.
        self.assertIn("cap controlledUeId=132 at 12 PRB", composed.utterance)

    def test_the_sentence_names_it_on_its_own_scope_key(self) -> None:
        from tools.hfconsole import build_hardware_free_session

        composed = build_hardware_free_session("QoSTarget", utterance=(
            "Meet the QoS target by cell selection at serving cell 87654321 "
            "nci for ueId=131; cap controlledUeId=132 at 12 PRB"))
        self.assertEqual(composed.cap.request.controlled_ue_scope_id, "132")

    def test_the_flag_and_the_sentence_must_agree(self) -> None:
        from tools.hfconsole import HardwareFreeConsoleError, build_hardware_free_session

        with self.assertRaisesRegex(
                HardwareFreeConsoleError, "more than one controlled UE"):
            build_hardware_free_session(
                "QoSTarget", controlled_amf_ue_ngap_id=133, utterance=(
                    "Meet the QoS target by cell selection at serving cell "
                    "87654321 nci for ueId=131; cap controlledUeId=132 at 12 PRB"))

    def test_capping_the_objective_ue_is_refused(self) -> None:
        from tools.hfconsole import HardwareFreeConsoleError, build_hardware_free_session

        with self.assertRaisesRegex(HardwareFreeConsoleError, "different, heavy"):
            build_hardware_free_session(
                "QoSTarget", controlled_amf_ue_ngap_id=131)

    def test_the_preview_and_the_view_both_show_it(self) -> None:
        from tools.hfconsole import build_hardware_free_session

        composed = build_hardware_free_session(
            "QoSTarget", controlled_amf_ue_ngap_id=132)
        preview = composed.session.draft(composed.utterance)
        declared, = preview.supplementary
        self.assertEqual(declared["controlledUeId"], "132")
        self.assertEqual(declared["maxDlPrbs"], "12")
        self.assertEqual(declared["adapter"], "r1-cap")
        # Covered by the hash the operator confirms.
        self.assertIn("supplementary", preview.to_canonical_dict())
        view = composed.session.start(composed.session.confirm(preview))
        observed, = view.supplementary
        self.assertEqual(observed.controlled_ue_id, "132")

    def test_the_headless_runner_forwards_the_flag(self) -> None:
        import contextlib
        import io

        import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main.run_headless_hardware_free(
                objective="QoSTarget", utterance=None,
                controlled_amf_ue_ngap_id=132)
        self.assertEqual(code, 0)
        printed = buffer.getvalue()
        self.assertIn("supplementary      : ue-dl-prb-cap 12 PRB on "
                      "controlledUeId=132 via r1-cap", printed)
        self.assertIn("supplementary state:", printed)

    def test_the_cli_refuses_the_flag_without_a_runtime(self) -> None:
        import sys
        from unittest import mock

        import main

        with mock.patch.object(
                sys, "argv",
                ["main", "--controlled-amf-ue-ngap-id", "132", "--no-gui"]):
            with self.assertRaises(SystemExit):
                main.main()


    def test_a_composition_is_produced_and_actuated_under_a_permit(self) -> None:
        from tools.hfconsole.xapp import run_xapp_round_trip

        result = run_xapp_round_trip("QoSTarget")
        self.assertTrue(result.accepted)
        # QoSTarget's richest allowed composition, resolved and role-separated.
        self.assertEqual(
            result.apply_order,
            ("cell-steering", "ue-dl-prb-cap", "scheduler-priority"))
        # rollback is the reverse of apply.
        self.assertEqual(result.rollback_order, tuple(reversed(result.apply_order)))
        actuated = {a.action_id: a for a in result.actuations}
        # steering and priority apply cleanly over the config store under a permit.
        self.assertIn("SUCCEEDED", actuated["cell-steering"].status)
        self.assertIn("SUCCEEDED", actuated["scheduler-priority"].status)
        # every actuation carries the permit it was gated by.
        for actuation in result.actuations:
            self.assertTrue(actuation.permit_ref)

    def test_the_cap_priority_conflict_on_one_ue_is_refused(self) -> None:
        from tools.hfconsole.xapp import run_xapp_round_trip

        result = run_xapp_round_trip("QoSTarget", same_ue_conflict=True)
        self.assertFalse(result.accepted)
        self.assertEqual(result.refusal_type, "CompositionConstraintViolationError")
        self.assertIn("distinct RNTIs", result.refusal)
        self.assertEqual(result.actuations, ())

    def test_each_submittable_objective_composes(self) -> None:
        from tools.hfconsole.build import submittable_families
        from tools.hfconsole.xapp import run_xapp_round_trip

        for family in submittable_families():
            with self.subTest(family=family):
                result = run_xapp_round_trip(family)
                self.assertTrue(result.accepted, f"{family}: {result.refusal}")
                self.assertTrue(result.apply_order)
                # the composition never applies a bare cell-wide power/mcs here;
                # every applied action has an owning specialist.
                for actuation in result.actuations:
                    self.assertTrue(actuation.xapp_id.startswith("xapp/"))


if __name__ == "__main__":
    unittest.main()
