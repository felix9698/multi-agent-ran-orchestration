"""KAGT lane: typed mailbox sending contract and violation sample factory.

Authority: docs/architecture/SEAMS-GATE2.md (KAGT owns
assurance/advisors/mailbox.py); task section 6.12 (stale, reordered,
duplicate, expired, idempotency-collision).
"""

from __future__ import annotations

import unittest

from assurance.advisors import mailbox
from assurance.advisors.messages import AdvisoryKind, AdvisoryMessage, NextCandidateProposal
from assurance.core.addressing import content_hash
from assurance.core.components import ComponentId
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION, EnvelopeRejection, classify_envelope

STAMP = "2026-08-21T09:00:00.000000Z"


def _message(candidate_id: str = "cand-1", correlation_id: str = "corr-1") -> AdvisoryMessage:
    return AdvisoryMessage(
        message_id=f"m-{candidate_id}",
        kind=AdvisoryKind.NEXT_CANDIDATE_PROPOSAL,
        issued_by=ComponentId.EVIDENCE_COORDINATOR,
        correlation_id=correlation_id,
        epoch_hash=content_hash({"epoch": 1}),
        created_at=STAMP,
        body=NextCandidateProposal(candidate_id=candidate_id),
    )


class SealAdvisoryMessage(unittest.TestCase):
    def test_seals_a_verifiable_envelope_with_the_right_fields(self):
        message = _message()
        envelope = mailbox.seal_advisory_message(
            message, object_id="case-1", event_id="evt-1", sequence=0,
            idempotency_key="idem-1", timestamp=STAMP,
        )
        self.assertTrue(envelope.verify_content_hash())
        self.assertEqual(envelope.message_kind, AdvisoryKind.NEXT_CANDIDATE_PROPOSAL.value)
        self.assertEqual(envelope.correlation_id, message.correlation_id)
        self.assertEqual(envelope.epoch_hash, message.epoch_hash)
        self.assertEqual(envelope.source_component, ComponentId.EVIDENCE_COORDINATOR)
        self.assertEqual(envelope.schema_version, ASSURANCE_SCHEMA_VERSION)
        self.assertEqual(envelope.payload["body"]["candidate_id"], "cand-1")

    def test_refuses_a_non_advisory_source(self):
        # AdvisoryMessage's own constructor already refuses a non-advisory
        # issuer, so the only way to exercise seal_advisory_message's guard
        # is a minimal stand-in exposing just the attribute it reads first.
        class _FakeAdvisory:
            issued_by = ComponentId.ASSURANCE_KERNEL

        with self.assertRaises(ValueError):
            mailbox.seal_advisory_message(
                _FakeAdvisory(), object_id="case-1", event_id="evt-1", sequence=0,
                idempotency_key="idem-1", timestamp=STAMP,
            )


class ViolationSampleFactory(unittest.TestCase):
    """Every factory-produced envelope classifies as exactly what it claims."""

    def test_every_sender_violation_kind_is_covered(self):
        self.assertEqual(
            {k.value for k in mailbox.SENDER_VIOLATION_KINDS},
            {
                "STALE_SEQUENCE", "REORDERED", "DUPLICATE_EVENT_ID",
                "REPLAYED_DUPLICATE", "IDEMPOTENCY_COLLISION", "EXPIRED",
            },
        )

    def test_each_kind_classifies_as_itself(self):
        primary = _message("cand-1")
        colliding = _message("cand-2")
        for kind in mailbox.SENDER_VIOLATION_KINDS:
            with self.subTest(kind=kind.value):
                extra = {"colliding_message": colliding} if kind is EnvelopeRejection.IDEMPOTENCY_COLLISION else {}
                sample = mailbox.build_violation_sample(
                    kind, primary_message=primary, now=STAMP, **extra
                )
                self.assertIs(sample.kind, kind)
                verdict = classify_envelope(sample.violating, sample.admission_state, now=sample.now)
                self.assertIs(verdict, kind)

    def test_the_baseline_in_every_sample_is_itself_admissible(self):
        primary = _message("cand-1")
        colliding = _message("cand-2")
        for kind in mailbox.SENDER_VIOLATION_KINDS:
            with self.subTest(kind=kind.value):
                extra = {"colliding_message": colliding} if kind is EnvelopeRejection.IDEMPOTENCY_COLLISION else {}
                sample = mailbox.build_violation_sample(
                    kind, primary_message=primary, now=STAMP, **extra
                )
                self.assertTrue(sample.accepted.verify_content_hash())

    def test_idempotency_collision_requires_a_colliding_message(self):
        with self.assertRaises(ValueError):
            mailbox.build_violation_sample(
                EnvelopeRejection.IDEMPOTENCY_COLLISION, primary_message=_message(), now=STAMP,
            )

    def test_rejects_a_non_sender_reproducible_kind(self):
        with self.assertRaises(ValueError):
            mailbox.build_violation_sample(
                EnvelopeRejection.SCHEMA_VERSION_UNSUPPORTED, primary_message=_message(), now=STAMP,
            )


if __name__ == "__main__":
    unittest.main()
