"""Typed advisory mailbox rejection matrix for lane KERN."""

from __future__ import annotations

import unittest

from assurance.core.addressing import content_hash
from assurance.core.components import ComponentId
from assurance.core.envelopes import (
    ASSURANCE_SCHEMA_VERSION,
    EnvelopeRejection,
    EventEnvelope,
    MailboxEnvelope,
)
from assurance.kernel.event_store import MemoryEventStore
from assurance.kernel.kernel import AssuranceKernel
from assurance.kernel.reducer import KernelReducer


NOW = "2026-08-21T00:00:00.000000Z"
LATER = "2026-08-21T00:00:01.000000Z"
PAST = "2026-08-20T23:59:59.000000Z"
EPOCH_HASH = content_hash({"epoch": "epoch-1"})


def kernel_event(kind: str, payload: dict, *, object_id: str, sequence: int = 0) -> EventEnvelope:
    return EventEnvelope.seal(
        schema_version=ASSURANCE_SCHEMA_VERSION,
        object_id=object_id,
        event_id=f"{object_id}:{sequence}:{kind}",
        timestamp=NOW,
        sequence=sequence,
        idempotency_key=f"idem:{object_id}:{sequence}:{kind}",
        source_component=ComponentId.ASSURANCE_KERNEL,
        event_kind=kind,
        payload=payload,
    )


def proposal_payload(candidate_id: str = "candidate-1") -> dict:
    return {
        "messageId": "message-1",
        "kind": "NextCandidateProposal",
        "issuedBy": ComponentId.EVIDENCE_COORDINATOR.value,
        "correlationId": "case-1",
        "epochHash": EPOCH_HASH,
        "createdAt": NOW,
        "body": {"candidate_id": candidate_id, "evidence_cell_refs": []},
    }


def mailbox(
    *,
    payload: dict | None = None,
    sequence: int = 0,
    event_id: str = "mail-0",
    idempotency_key: str = "proposal-key",
    expiry: str | None = None,
    epoch_hash: str = EPOCH_HASH,
    source: ComponentId = ComponentId.EVIDENCE_COORDINATOR,
    message_kind: str = "NextCandidateProposal",
) -> MailboxEnvelope:
    return MailboxEnvelope.seal(
        schema_version=ASSURANCE_SCHEMA_VERSION,
        object_id="mailbox-case-1",
        event_id=event_id,
        timestamp=NOW,
        sequence=sequence,
        idempotency_key=idempotency_key,
        source_component=source,
        message_kind=message_kind,
        correlation_id="case-1",
        epoch_hash=epoch_hash,
        payload=payload or proposal_payload(),
        expiry=expiry,
    )


def make_kernel() -> tuple[AssuranceKernel, MemoryEventStore]:
    store = MemoryEventStore()
    store.append(
        kernel_event(
            "CatalogFrozen",
            {
                "catalogHash": content_hash({"catalog": 1}),
                "cardinality": 1,
                "generatorVersion": "test/1",
                "epochRef": "epoch-1",
                "candidates": [
                    {
                        "candidateId": "candidate-1",
                        "targetRef": "target-1",
                        "optionRef": "option-1",
                        "parameters": {},
                        "semanticHash": "a" * 64,
                        "capabilityRef": "capability-1",
                    }
                ],
            },
            object_id="catalog-1",
        )
    )
    store.append(
        kernel_event(
            "EpochFrozen",
            {
                "epochId": "epoch-1",
                "epochHash": EPOCH_HASH,
                "catalogHash": content_hash({"catalog": 1}),
                "targetVectorOrder": ["vector-1"],
                "reducerVersion": KernelReducer.reducer_version,
            },
            object_id="epoch-1",
        )
    )
    return (
        AssuranceKernel(
            event_store=store,
            reducer=KernelReducer(),
            write_gateway=None,
            measurement_collector=None,
        ),
        store,
    )


class MailboxRejectionTests(unittest.TestCase):
    def test_stale_reordered_duplicate_expired_collision_and_wrong_epoch_fail_closed(self) -> None:
        cases = []

        accepted = mailbox()
        cases.append((mailbox(sequence=0, event_id="mail-stale", idempotency_key="other"), EnvelopeRejection.STALE_SEQUENCE))
        cases.append((mailbox(sequence=2, event_id="mail-reordered", idempotency_key="other"), EnvelopeRejection.REORDERED))
        cases.append((mailbox(sequence=1, event_id=accepted.event_id, idempotency_key="other"), EnvelopeRejection.DUPLICATE_EVENT_ID))
        cases.append((mailbox(sequence=1, event_id="mail-expired", idempotency_key="other", expiry=PAST), EnvelopeRejection.EXPIRED))
        cases.append((mailbox(sequence=1, event_id="mail-replayed"), EnvelopeRejection.REPLAYED_DUPLICATE))
        cases.append((mailbox(payload=proposal_payload("candidate-2"), sequence=1, event_id="mail-collision"), EnvelopeRejection.IDEMPOTENCY_COLLISION))
        cases.append((mailbox(sequence=1, event_id="mail-old-epoch", idempotency_key="other", epoch_hash="b" * 64), EnvelopeRejection.EPOCH_MISMATCH))

        for candidate, expected in cases:
            with self.subTest(expected=expected.value):
                kernel, store = make_kernel()
                self.assertIsNone(kernel.submit_advisory(accepted, now=NOW))
                self.assertEqual(kernel.submit_advisory(candidate, now=LATER), expected)
                recorded = list(store.iterate())[-1]
                self.assertEqual(recorded.event_kind, "AdvisoryRejected")
                self.assertEqual(recorded.payload["reason"], expected.value)

    def test_candidate_outside_frozen_catalog_is_rejected_and_recorded(self) -> None:
        kernel, store = make_kernel()

        rejection = kernel.submit_advisory(
            mailbox(payload=proposal_payload("hallucinated-candidate")), now=NOW
        )

        self.assertEqual(rejection, EnvelopeRejection.EPOCH_MISMATCH)
        self.assertEqual(list(store.iterate())[-1].event_kind, "AdvisoryRejected")
        self.assertEqual(
            list(store.iterate())[-1].payload["reason"], "CANDIDATE_NOT_FROZEN"
        )

    def test_rejected_proposal_keeps_case_correlation_for_proposal_cap(self) -> None:
        kernel, store = make_kernel()

        kernel.submit_advisory(
            mailbox(payload=proposal_payload("hallucinated-candidate")), now=NOW
        )

        recorded = list(store.iterate())[-1]
        self.assertEqual(recorded.payload["correlationId"], "case-1")

    def test_unknown_kind_and_untyped_extra_fields_are_rejected(self) -> None:
        malformed = proposal_payload()
        malformed["body"]["arbitraryInstruction"] = {"doAnything": True}
        cases = (
            mailbox(
                payload={**proposal_payload(), "kind": "UNRECOGNISED"},
                message_kind="UNRECOGNISED",
            ),
            mailbox(payload=malformed),
        )

        for envelope in cases:
            with self.subTest(kind=envelope.message_kind):
                kernel, store = make_kernel()
                self.assertEqual(
                    kernel.submit_advisory(envelope, now=NOW),
                    EnvelopeRejection.SOURCE_NOT_PERMITTED,
                )
                self.assertEqual(
                    list(store.iterate())[-1].payload["reason"],
                    "MALFORMED_TYPED_ADVISORY",
                )

    def test_typed_body_values_and_frozen_evidence_refs_are_validated(self) -> None:
        bad_ref = proposal_payload()
        bad_ref["body"]["evidence_cell_refs"] = ["unknown-cell"]
        bad_type = {
            **proposal_payload(),
            "kind": "CandidateAssessment",
            "issuedBy": ComponentId.XAPP_AGENT.value,
            "body": {"candidate_id": "candidate-1", "applicable": "yes"},
        }
        cases = (
            mailbox(payload=bad_ref),
            mailbox(
                payload=bad_type,
                source=ComponentId.XAPP_AGENT,
                message_kind="CandidateAssessment",
            ),
        )

        for envelope in cases:
            with self.subTest(kind=envelope.message_kind):
                kernel, _ = make_kernel()
                self.assertEqual(
                    kernel.submit_advisory(envelope, now=NOW),
                    EnvelopeRejection.SOURCE_NOT_PERMITTED,
                )


if __name__ == "__main__":
    unittest.main()
