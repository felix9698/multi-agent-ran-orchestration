"""Gate 2 acceptance: zero hardware calls, and the authority boundary.

Two of task section 13's Gate 2 criteria:

* "hardware/live target call 0" -- design section 15 requires hardware-free
  work to *report* zero live E2/RAN/OTA/USRP calls, so this file makes the run
  fail if one is attempted rather than counting after the fact;
* "illegal transition ... fail-closed", together with task section 3.3's list
  of things an agent may not do: set a target, a threshold, catalog
  membership, an authorization token, a verdict, an evidence closure, a harm
  charge, a target release or a terminal state.
"""

from __future__ import annotations

import os
import pathlib
import re
import socket
import subprocess
import unittest
from typing import Any

from assurance.core.envelopes import EnvelopeRejection
from assurance.core.states import TRIAL_TRANSITIONS, StopReason, TrialState
from assurance.kernel.kernel import KernelRefusal

from tests.assurance.vertical_support import CELL_ID, VerticalFixture

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
ASSURANCE_PACKAGE = REPOSITORY_ROOT / "assurance"

#: Modules that would put a live endpoint, a process or a model behind the
#: assurance package.  The list is the design's exclusion, not a style rule:
#: Gate 2 is hardware-free and every component in it is a pure function of its
#: injected collaborators.
FORBIDDEN_IMPORTS = (
    "anthropic",
    "google.generativeai",
    "http.client",
    "httpx",
    "openai",
    "paramiko",
    "requests",
    "socket",
    "subprocess",
    "telnetlib",
    "urllib.request",
    "websockets",
)

_IMPORT_PATTERN = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", re.MULTILINE)


class NoLiveCall(AssertionError):
    """Raised the moment a hardware-free run reaches for the outside world."""


class HardwareFreeTests(VerticalFixture, unittest.TestCase):
    def test_the_assurance_package_imports_nothing_that_could_reach_equipment(
        self,
    ) -> None:
        offenders = {}
        for module in sorted(ASSURANCE_PACKAGE.rglob("*.py")):
            imported = set(_IMPORT_PATTERN.findall(module.read_text()))
            hits = sorted(
                name
                for name in imported
                for forbidden in FORBIDDEN_IMPORTS
                if name == forbidden or name.startswith(f"{forbidden}.")
            )
            if hits:
                offenders[str(module.relative_to(REPOSITORY_ROOT))] = hits

        self.assertEqual(offenders, {})

    def test_a_complete_run_opens_no_socket_and_starts_no_process(self) -> None:
        def refuse(*args: Any, **kwargs: Any):
            raise NoLiveCall("a hardware-free run attempted a live call")

        patches = [
            (socket, "socket"),
            (socket, "create_connection"),
            (subprocess, "Popen"),
            (subprocess, "run"),
            (os, "system"),
            (os, "popen"),
        ]
        originals = [(module, name, getattr(module, name)) for module, name in patches]
        for module, name in patches:
            setattr(module, name, refuse)
        try:
            path = self.build()
            report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)
            path.terminate()
        finally:
            for module, name, original in originals:
                setattr(module, name, original)

        self.assertEqual(report.terminal_state, TrialState.SETTLED_SUCCESS)

    def test_every_effect_is_recorded_against_a_kernel_issued_permit(self) -> None:
        path = self.build()
        path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)

        issued = {
            envelope.payload["idempotencyKey"]
            for envelope in self.store.iterate()
            if envelope.event_kind == "TokenIssued"
        }
        # Every command the adapter saw derives its key from one of those
        # permits; there is no second door into the equipment.
        self.assertTrue(self.adapter.idempotency_keys)
        for key in self.adapter.idempotency_keys:
            self.assertIn(key.split("#")[0], issued)
        self.assertEqual(self.gateway.refusals(), ())

    def test_the_collector_delivers_to_the_kernel_and_to_nothing_else(self) -> None:
        path = self.build()

        with self.assertRaises(RuntimeError):
            self.collector.bind_sink(lambda sample: None)

        path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)
        ingested = [
            envelope.payload["sampleId"]
            for envelope in self.store.iterate()
            if envelope.event_kind == "RawSampleIngested"
        ]
        self.assertEqual(len(ingested), 5)
        self.assertEqual(len(set(ingested)), 5)


class IllegalTransitionTests(VerticalFixture, unittest.TestCase):
    def test_every_illegal_transition_out_of_a_live_state_is_refused(self) -> None:
        path = self.build()
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)

        source = TrialState.PREPARING
        self.assertEqual(
            self.kernel.reduced_state()["trials"][trial_id]["state"], source.value
        )
        for target in TrialState:
            if target in TRIAL_TRANSITIONS[source]:
                continue
            with self.subTest(target=target.value):
                with self.assertRaises(KernelRefusal) as refusal:
                    self.kernel.advance_trial(
                        trial_id,
                        target,
                        now=self.clock(),
                        reason=StopReason.OPERATOR_ABORT
                        if target is TrialState.STOPPING
                        else None,
                    )
                self.assertEqual(refusal.exception.reason, "ILLEGAL_TRANSITION")

    def test_apply_is_refused_without_a_durable_acknowledged_commit(self) -> None:
        path = self.build()
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)
        path.ready(trial_id)
        self.kernel.advance_trial(trial_id, TrialState.READY, now=self.clock())

        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.advance_trial(
                trial_id, TrialState.COMMIT_DECIDED, now=self.clock()
            )
        self.assertEqual(refusal.exception.reason, "COMMIT_NOT_READY")
        self.assertEqual(self.adapter.writes, [])


class AdvisoryAuthorityTests(VerticalFixture, unittest.TestCase):
    """Task section 3.3: an advisory names things; it never sets them."""

    def _proposal(self, path, candidate_id: str):
        from assurance.advisors.proposal_support import build_next_candidate_proposal

        return build_next_candidate_proposal(
            message_id=f"probe/{candidate_id}",
            candidate_id=candidate_id,
            rationale="probe",
            correlation_id=path.case_id,
            epoch_hash=path.epoch_hash(),
            now=self.clock(),
        )

    def test_a_candidate_outside_the_frozen_catalog_is_refused(self) -> None:
        path = self.build()

        rejection = path.submit(self._proposal(path, "candidate/hallucinated"))

        self.assertIs(rejection, EnvelopeRejection.EPOCH_MISMATCH)
        reasons = [
            envelope.payload["reason"]
            for envelope in self.store.iterate()
            if envelope.event_kind == "AdvisoryRejected"
        ]
        self.assertEqual(reasons, ["CANDIDATE_NOT_FROZEN"])

    def _tampered(self, message: Any, mutate) -> Any:
        """A sender that seals a body the typed message type would not build."""
        canonical = message.to_canonical_dict()
        mutate(canonical)

        class Tampered:
            kind = message.kind
            issued_by = message.issued_by
            correlation_id = message.correlation_id
            epoch_hash = message.epoch_hash
            created_at = message.created_at

            def to_canonical_dict(self) -> dict:
                return canonical

        return Tampered()

    def test_an_advisory_body_field_outside_the_schema_is_refused(self) -> None:
        path = self.build()
        message = self._proposal(path, self.first_candidate_id())

        rejection = path.submit(
            self._tampered(
                message, lambda payload: payload["body"].update({"verdict": "PASS"})
            )
        )

        self.assertIs(rejection, EnvelopeRejection.SOURCE_NOT_PERMITTED)
        reasons = [
            envelope.payload["reason"]
            for envelope in self.store.iterate()
            if envelope.event_kind == "AdvisoryRejected"
        ]
        self.assertEqual(reasons, ["MALFORMED_TYPED_ADVISORY"])

    def test_a_kernel_decision_key_nested_in_a_schema_field_is_refused(self) -> None:
        """The schema check alone is not enough.

        ``scope_selector`` is a free string-to-string mapping, so a body that
        satisfies the schema can still carry ``verdict`` one level down.  The
        Kernel walks the whole payload for its own vocabulary rather than
        checking only the top level.
        """
        from assurance.advisors.messages import (
            AdvisoryKind,
            AdvisoryMessage,
            IntentDraft,
        )
        from assurance.core.components import ComponentId

        path = self.build()
        draft = AdvisoryMessage(
            message_id="probe/intent-1",
            kind=AdvisoryKind.INTENT_DRAFT,
            issued_by=ComponentId.INTENT_AGENT,
            correlation_id=path.case_id,
            epoch_hash=path.epoch_hash(),
            created_at=self.clock(),
            body=IntentDraft(
                objective_family="TrafficSteeringPreference",
                scope_selector={"cellId": "cell-1", "verdict": "PASS"},
            ),
        )

        rejection = path.submit(draft)

        self.assertIs(rejection, EnvelopeRejection.SOURCE_NOT_PERMITTED)
        reasons = [
            envelope.payload["reason"]
            for envelope in self.store.iterate()
            if envelope.event_kind == "AdvisoryRejected"
        ]
        self.assertEqual(reasons, ["ADVISORY_ATTEMPTED_KERNEL_MUTATION"])

    def test_an_accepted_proposal_confers_nothing_by_itself(self) -> None:
        path = self.build()
        candidate_id = self.first_candidate_id()

        self.assertIsNone(path.submit(self._proposal(path, candidate_id)))

        state = self.kernel.reduced_state()
        self.assertEqual(state["trials"], {})
        self.assertEqual(state["harmLedger"], [])
        self.assertEqual(state["resourceLocks"], {})
        self.assertEqual(
            state["evidenceCells"][CELL_ID]["status"], "OPEN"
        )
        self.assertEqual(
            [envelope.event_kind for envelope in self.store.iterate()][-1],
            "AdvisoryAccepted",
        )


if __name__ == "__main__":
    unittest.main()
