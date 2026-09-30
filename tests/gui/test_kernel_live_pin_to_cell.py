"""The Cockpit's LIVE submission path, driven over a clock-driven deployment.

``tests/gui/test_kernel_submission_path.py`` proves the console's half against
the hardware-free mock adapter.  This file proves the *other* half: that the
same :class:`~gui.operator.sources.kernel_live.KernelSubmissionSession`, in
``LIVE`` mode over the live driver's ports, reaches a settled terminal when an
acknowledgement and its effect are separated in time -- and that the evidence
the operational runner writes out of it is the Kernel's record rather than a
second opinion.

The runner's own projections (``tools/g3ota/run_ota``) are exercised here too.
They are the thing an OTA run is judged from, so a defect in them is a defect
in the evidence, and the previous live attempt lost a completed episode to
exactly that.
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List

from assurance.live.pin_to_cell_driver import (
    PIN_TO_CELL_GRAMMAR,
    LiveDriverError,
    pin_utterance,
)
from gui.operator.sources.kernel_live import (
    MODE_LIVE,
    STAGE_TERMINAL,
    KernelSubmissionSession,
)

from tests.assurance.live_support import TARGET_NCI
from tests.assurance.test_live_pin_to_cell_driver import LiveRunFixture
from tools.g3ota.run_ota import _evidence, _view_record, operator_utterance


class _Args:
    """The runner's argument surface, reduced to what the evidence reads."""

    producer_db = "/definitely/not/a/producer.sqlite3"
    evidence_dir = "/tmp"


class LiveCockpitSubmission(LiveRunFixture):

    def submit(self, **testbed: Any) -> Dict[str, Any]:
        runtime = self.build(**testbed)
        published: List[Dict[str, Any]] = []
        self.session = KernelSubmissionSession(
            path=runtime.path,
            cell_id=runtime.cell_id,
            objective_registry=PIN_TO_CELL_GRAMMAR,
            mode=MODE_LIVE,
            publish=lambda _topic, view: published.append(_view_record(view)),
            settle_ms=runtime.timing.cadence_ms,
        )
        self.utterance = runtime.utterance()
        self.preview = self.session.draft(self.utterance)
        self.instance = self.session.confirm(self.preview)
        self.view = self.session.start(self.instance)
        self.published = published
        return _evidence(
            stamp="20260824T000000Z",
            args=_Args(),
            binding=self.binding,
            runtime=runtime,
            session=self.session,
            view=self.view,
            preview=self.preview,
            instance=self.instance,
            utterance=self.utterance,
            preflight={},
            cleared=[],
            builders=[],
            published=published,
        )

    def test_a_sentence_becomes_a_settled_live_success(self) -> None:
        evidence = self.submit()

        self.assertEqual(self.view.mode, MODE_LIVE)
        self.assertEqual(self.view.stage, STAGE_TERMINAL)
        self.assertIsNone(self.view.refusal)
        self.assertEqual(evidence["settlement"]["trialState"], "SETTLED_SUCCESS")
        self.assertEqual(evidence["settlement"]["outcome"], "SUCCESS")
        self.assertEqual(evidence["settlement"]["evidenceStatus"], "CLOSED_PASS")

    def test_the_four_axes_are_reported_separately(self) -> None:
        evidence = self.submit()
        axes = evidence["axes"]

        self.assertEqual(axes["executionValidity"], "VALID")
        self.assertEqual(axes["measurementSufficiency"], "SUFFICIENT")
        self.assertEqual(
            axes["predicateVerdicts"],
            {
                "serving-cell-is-pinned-throughout": "PASS",
                "serving-cell-is-pinned-only": "PASS",
            },
        )
        self.assertEqual(axes["trialOutcome"], "SUCCESS")
        self.assertTrue(axes["holdComplete"])

    def test_the_evidence_carries_what_makes_it_evidence(self) -> None:
        """Every field an OTA claim rests on, written before it is claimed."""
        evidence = self.submit()

        self.assertEqual(evidence["utterance"], self.utterance)
        self.assertEqual(evidence["contract"]["confirmedContentHash"],
                         self.preview.content_hash())
        self.assertEqual(evidence["contract"]["parameters"],
                         {"servingCell": str(TARGET_NCI)})
        self.assertTrue(evidence["terminalStateHash"])
        self.assertEqual(
            [entry[0] for entry in evidence["gatewayLog"]],
            ["PREPARE", "READY", "COMMIT", "CONFIGURATION_REREAD", "FINALIZE_LIVE"],
        )
        self.assertEqual(
            [read["detail"] for read in evidence["readbacks"]][-1],
            "status-and-stream",
        )
        self.assertTrue(evidence["policyIds"])
        self.assertEqual(
            {sample["value"] for sample in evidence["samples"]}, {float(TARGET_NCI)}
        )

    def test_an_effect_nobody_else_saw_is_not_reported_as_a_success(self) -> None:
        evidence = self.submit(stream_follows_policy=False)

        self.assertNotEqual(evidence["settlement"]["outcome"], "SUCCESS")
        self.assertIn(
            "status verified but the stream did not corroborate",
            [read["detail"] for read in evidence["readbacks"]],
        )

    def test_the_console_never_writes_the_axes_it_publishes(self) -> None:
        """Design section 9.9, on the live path: every axis is read back."""
        evidence = self.submit()
        trial = self.session.path.kernel.reduced_state()["trials"][
            self.view.trial_id
        ]["evaluation"]

        self.assertEqual(evidence["axes"]["executionValidity"],
                         trial["executionValidity"])
        self.assertEqual(evidence["axes"]["measurementSufficiency"],
                         trial["measurementSufficiency"])
        self.assertEqual(evidence["axes"]["predicateVerdicts"],
                         dict(trial["predicateVerdicts"]))


class OperatorUtteranceTests(unittest.TestCase):
    """``--utterance`` puts the Operator's words in, not a different contract.

    The flag exists so the sentence in the evidence is one a person typed.  It
    is only honest while the typed sentence and the sentence this runner would
    have composed read to the *same* contract, so both halves are proved here:
    a differently-worded sentence with the same reading is accepted, and one
    that would move the UE somewhere else is refused before anything is
    submitted.
    """

    GENERATED = pin_utterance(TARGET_NCI, "171")

    def accept(self, typed: str) -> str:
        return operator_utterance(typed, self.GENERATED,
                                  grammar=PIN_TO_CELL_GRAMMAR,
                                  case_id="case/test")

    def test_the_operators_own_wording_is_kept_verbatim(self) -> None:
        typed = f"please pin the serving cell to {TARGET_NCI} nci for ueId=171"

        self.assertEqual(self.accept(typed), typed)

    def test_a_sentence_naming_another_cell_is_refused(self) -> None:
        with self.assertRaises(LiveDriverError) as caught:
            self.accept("pin the serving cell to 11111111 nci for ueId=171")

        self.assertIn("does not read as the contract", str(caught.exception))

    def test_a_sentence_naming_another_ue_is_refused(self) -> None:
        with self.assertRaises(LiveDriverError):
            self.accept(f"pin the serving cell to {TARGET_NCI} nci for ueId=999")

    def test_an_unrecognised_sentence_is_refused_by_name(self) -> None:
        with self.assertRaises(LiveDriverError) as caught:
            self.accept("make the network better please")

        self.assertIn("not recognised", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
