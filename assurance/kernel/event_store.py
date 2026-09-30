"""The append-only event store.

Owner lane: **KERN**.  Signatures frozen by this design step.

Design section 4.3: "The same accepted event stream and reducer version must
reproduce the same ledger and terminal state hash without any LLM."  Section 15
verifies it as "append-only event replay to identical final state hash".

The store is therefore not a database convenience -- it is the system of
record.  Three properties the implementation owes every caller:

* **Append-only.**  There is no update, no delete and no compaction that loses
  a record.  A correction is another event.
* **Durable before acknowledged.**  ``append`` returns only once the record
  survives a crash.  Design section 7 requires a durable ``COMMIT_DECIDED``
  before apply and a durable success decision before finalize; both are
  ``append`` calls, and a buffered write would make the durability claim
  false at exactly the moment it matters.
* **Deterministically ordered.**  Iteration yields records in accepted order,
  and the per-object sequence is dense: reordering or a gap is a rejected
  append (:func:`assurance.core.envelopes.classify_envelope`), not something
  the reader repairs.

The store does not decide admissibility.  It is handed envelopes the Kernel
has already classified, and it enforces only what it can see: sequence
density, id uniqueness, durability.
"""

from __future__ import annotations

import json
import os
import fcntl
from pathlib import Path
from threading import RLock
from typing import Dict, Iterator, List, Mapping, Optional, Protocol, Sequence, Set

from assurance.core.components import ComponentId
from assurance.core.envelopes import EventEnvelope

__all__ = [
    "EventStore",
    "EventStoreError",
    "FileEventStore",
    "MemoryEventStore",
]


class EventStoreError(RuntimeError):
    """An append or read violated the store's invariants.

    Raised rather than returned.  A failed append must never look like a
    successful one: the Kernel decides whether it is safe to apply based on
    whether the commit decision is durable, and a swallowed write error there
    would put an unrecorded change on the equipment.
    """


class EventStore(Protocol):
    """The append-only event stream the Kernel owns."""

    def append(self, envelope: EventEnvelope) -> int:
        """Durably append *envelope*; return its global position.

        Signature frozen; body owned by lane **KERN**.

        Must not return until the record is durable.  Must reject an envelope
        whose ``sequence`` is not exactly one past the last accepted sequence
        for its ``object_id``, and one whose ``event_id`` has been seen --
        those are the store-visible halves of the staleness and duplicate
        rules in task section 6.12.

        The returned position is global and monotonic across all objects: it
        is the replay order, which is what makes the terminal state hash
        reproducible.  It is deliberately not the per-object ``sequence``,
        which only orders one object's events relative to each other.
        """
        ...

    def iterate(self, *, since_position: int = 0) -> Iterator[EventEnvelope]:
        """Yield envelopes in accepted order from *since_position*.

        Signature frozen; body owned by lane **KERN**.

        Streaming rather than returning a list: a completed campaign's stream
        is the paper's raw data (design section 13) and does not have to fit
        in memory to be replayed.
        """
        ...

    def last_position(self) -> int:
        """The global position of the most recent append, or ``-1`` if empty."""
        ...

    def last_sequence(self, object_id: str) -> int:
        """The last accepted per-object sequence, or ``-1`` if none.

        Signature frozen; body owned by lane **KERN**.

        This is what a restarting Kernel reads to rebuild its
        :class:`~assurance.core.envelopes.EnvelopeAdmissionState` cursor, so
        recovery does not re-accept an event it already durably recorded.
        """
        ...

    def has_event_id(self, event_id: str) -> bool:
        """True when *event_id* has already been appended."""
        ...

    def idempotency_hash(self, idempotency_key: str) -> Optional[str]:
        """Content hash first recorded under *idempotency_key*, or ``None``.

        Signature frozen; body owned by lane **KERN**.

        The return type is what separates a benign retransmission from a
        collision: equal hash means
        :attr:`~assurance.core.envelopes.EnvelopeRejection.REPLAYED_DUPLICATE`,
        a different hash means
        :attr:`~assurance.core.envelopes.EnvelopeRejection.IDEMPOTENCY_COLLISION`,
        and the store is the only place that knows which.
        """
        ...

    def uncertain_transactions(self) -> Sequence[str]:
        """Transaction ids whose outcome is not durably resolved.

        Signature frozen; body owned by lane **KERN**.

        Design section 8: "Restart recovery blocks new trials until every
        uncertain transaction is queried and safely aborted, finalized,
        rolled back, or placed in incident lockdown."  This is the list that
        gate is evaluated over; an empty return is the only thing that lets a
        restarted Kernel admit a new trial.

        A transaction is uncertain when the stream contains a durable commit
        decision, apply or finalize request for it with no corresponding
        settlement -- which is exactly the lost-ack case, and is why the
        answer must come from the stream rather than from process memory.
        """
        ...


class MemoryEventStore:
    """Append-only deterministic store used by hardware-free runs and tests.

    This implementation has the same acceptance and replay semantics as the
    durable file store below.  It deliberately makes no durability claim;
    production processes that cross a crash boundary use
    :class:`FileEventStore`.
    """

    def __init__(self, events: Sequence[EventEnvelope] = ()) -> None:
        self._events: List[EventEnvelope] = []
        self._last_sequences: Dict[str, int] = {}
        self._event_ids: Set[str] = set()
        self._idempotency: Dict[str, str] = {}
        self._lock = RLock()
        for envelope in events:
            self._record_validated(envelope)

    def _validate_append(self, envelope: EventEnvelope) -> None:
        if not isinstance(envelope, EventEnvelope):
            raise EventStoreError("only EventEnvelope records may be appended")
        if not envelope.verify_content_hash():
            raise EventStoreError(f"content hash mismatch for {envelope.event_id}")
        expected = self.last_sequence(envelope.object_id) + 1
        if envelope.sequence != expected:
            raise EventStoreError(
                f"sequence for {envelope.object_id!r} must be {expected}, "
                f"got {envelope.sequence}"
            )
        if envelope.event_id in self._event_ids:
            raise EventStoreError(f"duplicate event id: {envelope.event_id}")
        previous = self._idempotency.get(envelope.idempotency_key)
        if previous is not None:
            if previous == envelope.content_hash:
                raise EventStoreError(
                    f"replayed idempotency key: {envelope.idempotency_key}"
                )
            raise EventStoreError(
                f"idempotency collision: {envelope.idempotency_key}"
            )

    def _record_validated(self, envelope: EventEnvelope) -> int:
        self._validate_append(envelope)
        position = len(self._events)
        self._events.append(envelope)
        self._last_sequences[envelope.object_id] = envelope.sequence
        self._event_ids.add(envelope.event_id)
        self._idempotency[envelope.idempotency_key] = envelope.content_hash
        return position

    def append(self, envelope: EventEnvelope) -> int:
        with self._lock:
            return self._record_validated(envelope)

    def iterate(self, *, since_position: int = 0) -> Iterator[EventEnvelope]:
        if isinstance(since_position, bool) or not isinstance(since_position, int):
            raise EventStoreError("since_position must be an int")
        if since_position < 0:
            raise EventStoreError("since_position must be >= 0")
        with self._lock:
            snapshot = tuple(self._events[since_position:])
        return iter(snapshot)

    def last_position(self) -> int:
        with self._lock:
            return len(self._events) - 1

    def last_sequence(self, object_id: str) -> int:
        with self._lock:
            return self._last_sequences.get(object_id, -1)

    def has_event_id(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._event_ids

    def idempotency_hash(self, idempotency_key: str) -> Optional[str]:
        with self._lock:
            return self._idempotency.get(idempotency_key)

    def event_for_idempotency_key(self, idempotency_key: str) -> Optional[EventEnvelope]:
        """Return the original record for an idempotent retry, if present."""
        with self._lock:
            for envelope in self._events:
                if envelope.idempotency_key == idempotency_key:
                    return envelope
        return None

    def uncertain_transactions(self) -> Sequence[str]:
        """Derive unresolved transactions from durable semantic events."""
        uncertain: Dict[str, None] = {}
        for envelope in self.iterate():
            payload = envelope.payload
            transaction_id = payload.get("transactionId")
            if not isinstance(transaction_id, str) or not transaction_id:
                continue
            if envelope.event_kind == "TrialStateChanged" and payload.get("to") in {
                "COMMIT_DECIDED",
                "APPLYING",
                "FINALIZING_LIVE",
            }:
                uncertain.setdefault(transaction_id, None)
            elif envelope.event_kind == "TokenIssued" and payload.get("tokenKind") in {
                "COMMIT",
                "FINALIZE_LIVE",
            }:
                uncertain.setdefault(transaction_id, None)
            elif envelope.event_kind in {"TrialSettled", "TransactionResolved"}:
                uncertain.pop(transaction_id, None)
        return tuple(uncertain)


def _event_from_record(record: Mapping[str, object]) -> EventEnvelope:
    """Rebuild an event from its canonical JSON record."""
    try:
        return EventEnvelope(
            schema_version=str(record["schemaVersion"]),
            object_id=str(record["objectId"]),
            event_id=str(record["eventId"]),
            timestamp=str(record["timestamp"]),
            content_hash=str(record["contentHash"]),
            sequence=int(record["sequence"]),
            expiry=(str(record["expiry"]) if "expiry" in record else None),
            idempotency_key=str(record["idempotencyKey"]),
            source_component=ComponentId(str(record["sourceComponent"])),
            payload=record["payload"],  # type: ignore[arg-type]
            event_kind=str(record["eventKind"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EventStoreError("invalid event record in durable stream") from exc


class FileEventStore(MemoryEventStore):
    """JSONL event store that fsyncs every append before acknowledging it."""

    def __init__(self, path: os.PathLike[str] | str) -> None:
        self.path = Path(path)
        super().__init__()
        #: 이 저장소가 이미 접어 넣은 파일 바이트.  `append` 는 이 지점 **뒤**만 읽는다.
        self._consumed_bytes = 0
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.endswith("\n"):
                        raise EventStoreError(
                            f"incomplete event record at line {line_number}"
                        )
                    record = json.loads(line)
                    if not isinstance(record, Mapping):
                        raise EventStoreError(
                            f"event record at line {line_number} is not an object"
                        )
                    self._record_validated(_event_from_record(record))
        except json.JSONDecodeError as exc:
            raise EventStoreError(
                f"invalid JSON event record at line {exc.lineno}"
            ) from exc
        self._consumed_bytes = self.path.stat().st_size

    def _fold_appended_records(self, descriptor: int) -> None:
        """다른 쓰기 주체가 **새로 붙인 것만** 접어 넣는다.

        `append` 는 예전에 매번 파일 **전체**를 다시 읽어 모든 이벤트를 재구성하고 각각의
        content hash 를 다시 검증했다.  N 번째 append 가 N 건을 정규화하므로 총 O(N**2) 이고,
        그 일은 전부 JCS 정규화라 CPU 를 태운다.  2026-09-23 04:05 에 판 하나가 이벤트
        5,826건(3건/초, 건당 679바이트)에서 **20분간 CPU 78%로 멎었다** -- 스택은 두 번 다
        `append -> _event_from_record -> payload_digest -> jcs.canonicalize` 였다.
        v4.4 가 owner 4명·요구 7개·후보 1536개로 커지며 이벤트가 빨리 쌓이자 터졌다.

        보장은 그대로다.  flock 을 쥔 채 파일 크기를 보고, 우리가 아직 안 읽은 꼬리만
        읽어 접는다 -- 다른 주체가 덧붙였으면 그것도 여기서 들어오므로 순번·중복·
        멱등키 검사는 여전히 **전체 상태**에 대해 이뤄진다.  줄마다 content hash 를
        확인하는 것도 그대로이고, 다만 **한 줄을 한 번만** 확인한다.
        """
        size = os.fstat(descriptor).st_size
        if size == self._consumed_bytes:
            return
        if size < self._consumed_bytes:
            raise EventStoreError(
                "the event log shrank beneath this store; it is append-only"
            )
        chunk = os.pread(descriptor, size - self._consumed_bytes, self._consumed_bytes)
        if not chunk.endswith(b"\n"):
            raise EventStoreError("incomplete final event record")
        for line_number, line in enumerate(
            chunk.decode("utf-8").splitlines(), start=1
        ):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EventStoreError(
                    f"invalid JSON event record at line {line_number}"
                ) from exc
            if not isinstance(record, Mapping):
                raise EventStoreError(
                    f"event record at line {line_number} is not an object"
                )
            self._record_validated(_event_from_record(record))
        self._consumed_bytes = size

    def append(self, envelope: EventEnvelope) -> int:
        data = (
            json.dumps(
                envelope.to_canonical_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            + b"\n"
        )
        with self._lock:
            created = not self.path.exists()
            try:
                descriptor = os.open(
                    self.path,
                    os.O_RDWR | os.O_CREAT | os.O_APPEND,
                    0o600,
                )
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    self._fold_appended_records(descriptor)
                    self._validate_append(envelope)
                    written = 0
                    while written < len(data):
                        written += os.write(descriptor, data[written:])
                    os.fsync(descriptor)
                    if created:
                        directory_descriptor = os.open(
                            self.path.parent,
                            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                        )
                        try:
                            os.fsync(directory_descriptor)
                        finally:
                            os.close(directory_descriptor)
                    position = self._record_validated(envelope)
                    self._consumed_bytes += len(data)
                    return position
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
            except OSError as exc:
                raise EventStoreError(f"durable append failed: {exc}") from exc
