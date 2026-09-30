"""Deterministic replay and append-only store tests for lane KERN."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from assurance.core.axes import TrialOutcome
from assurance.core.components import ComponentId
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION, EventEnvelope
from assurance.core.states import TRIAL_TRANSITIONS, TrialState
from assurance.kernel.event_store import EventStoreError, FileEventStore, MemoryEventStore
from assurance.kernel.reducer import (
    KernelReducer,
    ReducerError,
    replay,
    terminal_state_hash,
)


NOW = "2026-08-21T00:00:00.000000Z"


def event(
    kind: str,
    payload: dict,
    *,
    object_id: str = "trial-1",
    sequence: int = 0,
    event_id: str | None = None,
    idempotency_key: str | None = None,
) -> EventEnvelope:
    return EventEnvelope.seal(
        schema_version=ASSURANCE_SCHEMA_VERSION,
        object_id=object_id,
        event_id=event_id or f"{object_id}:{sequence}:{kind}",
        timestamp=NOW,
        sequence=sequence,
        idempotency_key=idempotency_key or f"idem:{object_id}:{sequence}:{kind}",
        source_component=ComponentId.ASSURANCE_KERNEL,
        event_kind=kind,
        payload=payload,
    )


def opened_trial() -> EventEnvelope:
    return event(
        "TrialOpened",
        {
            "trialId": "trial-1",
            "caseId": "case-1",
            "epochId": "epoch-1",
            "candidateId": "candidate-1",
            "candidateSemanticHash": "a" * 64,
            "transactionId": "tx-trial-1",
        },
    )


class EventStoreTests(unittest.TestCase):
    def test_memory_store_rejects_sequence_gap_duplicate_and_collision(self) -> None:
        store = MemoryEventStore()
        first = event("TrialOpened", {"trialId": "trial-1"})
        self.assertEqual(store.append(first), 0)

        with self.assertRaises(EventStoreError):
            store.append(event("TrialOpened", {}, sequence=2))
        with self.assertRaises(EventStoreError):
            store.append(event("Other", {}, object_id="other", event_id=first.event_id))
        with self.assertRaises(EventStoreError):
            store.append(
                event(
                    "Other",
                    {"different": True},
                    object_id="other",
                    idempotency_key=first.idempotency_key,
                )
            )

    def test_file_store_fsync_stream_survives_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel-events.jsonl"
            store = FileEventStore(path)
            first = event("TrialOpened", {"trialId": "trial-1"})
            store.append(first)

            reopened = FileEventStore(path)
            self.assertEqual(list(reopened.iterate()), [first])
            self.assertEqual(reopened.last_position(), 0)
            self.assertEqual(reopened.last_sequence("trial-1"), 0)

    def test_file_store_instances_serialize_and_reload_before_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel-events.jsonl"
            first = FileEventStore(path)
            second = FileEventStore(path)
            first.append(event("TrialOpened", {"trialId": "trial-1"}))
            second.append(
                event(
                    "TrialStateChanged",
                    {"trialId": "trial-1", "from": "PROPOSED", "to": "VALIDATING"},
                    sequence=1,
                )
            )

            with self.assertRaises(EventStoreError):
                first.append(event("Other", {}, sequence=1))

            self.assertEqual(FileEventStore(path).last_sequence("trial-1"), 1)


class ReducerReplayTests(unittest.TestCase):
    def test_same_stream_and_version_reproduce_identical_terminal_hash(self) -> None:
        reducer = KernelReducer()
        events = [
            opened_trial(),
            event(
                "TrialStateChanged",
                {"trialId": "trial-1", "from": "PROPOSED", "to": "VALIDATING"},
                sequence=1,
            ),
        ]

        first = replay(reducer, events)
        second = replay(KernelReducer(), events)

        self.assertEqual(first, second)
        self.assertEqual(
            terminal_state_hash(first, reducer_version=reducer.reducer_version),
            terminal_state_hash(second, reducer_version=reducer.reducer_version),
        )

    def test_removing_advisory_records_does_not_change_reduced_state_or_hash(self) -> None:
        reducer = KernelReducer()
        domain_events = [opened_trial()]
        with_advisory = [
            domain_events[0],
            event(
                "AdvisoryAccepted",
                {"messageKind": "NextCandidateProposal", "candidateId": "candidate-1"},
                object_id="mailbox-1",
            ),
        ]

        domain_state = replay(reducer, domain_events)
        advisory_state = replay(reducer, with_advisory)

        self.assertEqual(advisory_state, domain_state)
        self.assertEqual(
            terminal_state_hash(advisory_state, reducer_version=reducer.reducer_version),
            terminal_state_hash(domain_state, reducer_version=reducer.reducer_version),
        )

    def test_every_illegal_transition_fails_closed(self) -> None:
        reducer = KernelReducer()
        baseline = replay(reducer, [opened_trial()])

        for source in TrialState:
            for target in TrialState:
                if target in TRIAL_TRANSITIONS[source]:
                    continue
                state = dict(baseline)
                state["trials"] = {key: dict(value) for key, value in baseline["trials"].items()}
                state["trials"]["trial-1"]["state"] = source.value
                transition = event(
                    "TrialStateChanged",
                    {"trialId": "trial-1", "from": source.value, "to": target.value},
                )
                with self.subTest(source=source.value, target=target.value):
                    with self.assertRaises(ReducerError):
                        reducer.apply(state, transition)

    def test_unknown_event_kind_fails_closed(self) -> None:
        with self.assertRaises(ReducerError):
            replay(KernelReducer(), [event("UnknownKernelEvent", {})])


if __name__ == "__main__":
    unittest.main()
