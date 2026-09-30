"""The R1 adapter's append-only record of the writes it actually issued.

Owner lane: **KGW**.

The binding journal (``r1_binding_journal.py``) answers *where a transaction
stands*: one record per transaction, replaced as the binding moves through
``RESERVED -> BOUND -> RESTORE_PENDING -> RESTORED``.  It deliberately cannot
answer *how many times the adapter wrote*, because a replaced record keeps no
history -- and "how many writes reached the equipment" is the number every
safety claim in ``docs/traceability/FAULT-MATRIX.md`` rests on.

So this is the other half: **append-only**, and **two records per call**.

``IN_FLIGHT`` is written and ``fsync``-ed *before* the port is touched.  It says
"a call is about to leave this process"; a crash between it and the response
therefore leaves a record that a write may have happened, which is the only
reading a recovery may safely take.  Writing the outcome first and the record
after would under-count a real E2 write for exactly the crash the journal
exists to survive.

The terminal record is appended when the call resolves and names the in-flight
record it settles (:attr:`R1Operation.resolves`):

``ISSUED``
    the port returned; the request was accepted downstream.
``REFUSED``
    the producer answered *no* before accepting anything.  Nothing was sent.
``UNKNOWN``
    the call raised something that is not a refusal, so the request may have
    reached the Non-RT RIC.

An ``IN_FLIGHT`` record with no resolution counts as exactly one possible
write -- never zero, and never two once its resolution arrives.

``operation`` is ``CREATE`` / ``UPDATE`` / ``DELETE``, the three calls that make
the released worker send an E2 CONTROL, or ``VALIDATE`` / ``STATUS``, which are
recorded for the sequence and never counted as writes.

What this journal does **not** claim is that a write reached the RAN.  The
adapter cannot know that; the producer's status object says it
(``aicStatus.control.writeMayHaveOccurred``, ``episodeState``) and an
independent readback confirms the effect.  An entry here is "the gateway caused
this call", which is exactly the number a refusal claim needs and no more.

Both implementations keep a bounded ring of entries and count **exactly**: the
tallies are kept per transaction as records are appended, so a session whose
oldest detail has rolled out of the ring still reports the right number of
applies.  A count derived by scanning the ring would silently drop a CREATE
behind four thousand status reads.
"""

from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Protocol, Tuple

__all__ = [
    "TERMINAL_OUTCOMES",
    "InMemoryR1OperationJournal",
    "JsonlR1OperationJournal",
    "R1Operation",
    "R1OperationJournal",
    "R1OperationOutcome",
    "R1PolicyOperation",
    "WRITE_OPERATIONS",
    "write_counts",
]

#: How many entries the in-memory ring keeps.  Counts stay exact beyond it.
OPERATION_RING_LIMIT: int = 4096


class R1PolicyOperation(Enum):
    """The policy-port calls an adapter can issue."""

    #: Discovery and body construction; no policy exists afterwards.
    VALIDATE = "VALIDATE"
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    #: A status read.  Recorded because "the adapter looked" is part of the
    #: sequence, and never counted as a write.
    STATUS = "STATUS"


#: The three calls that cause the released worker to send an E2 CONTROL.
WRITE_OPERATIONS: Tuple[R1PolicyOperation, ...] = (
    R1PolicyOperation.CREATE,
    R1PolicyOperation.UPDATE,
    R1PolicyOperation.DELETE,
)


class R1OperationOutcome(Enum):
    """Where one policy-port call stands."""

    #: Written and flushed *before* the port is touched.  Unresolved, it means
    #: a call left this process and nothing came back to say how it ended.
    IN_FLIGHT = "IN_FLIGHT"
    #: The call returned.  The request was accepted downstream.
    ISSUED = "ISSUED"
    #: The producer answered *no* before accepting anything.
    REFUSED = "REFUSED"
    #: The call raised something that is not a refusal: it may have arrived.
    UNKNOWN = "UNKNOWN"


#: The outcomes that settle an in-flight record.
TERMINAL_OUTCOMES: Tuple[R1OperationOutcome, ...] = (
    R1OperationOutcome.ISSUED,
    R1OperationOutcome.REFUSED,
    R1OperationOutcome.UNKNOWN,
)


@dataclass(frozen=True)
class R1Operation:
    """One policy-port call, as issued."""

    sequence: int
    transaction_id: str
    operation: R1PolicyOperation
    outcome: R1OperationOutcome
    adapter: str = ""
    policy_type_id: str = ""
    policy_id: Optional[str] = None
    fencing_token: Optional[int] = None
    reference: str = ""
    at: str = ""
    detail: str = ""
    #: For a terminal record, the sequence of the ``IN_FLIGHT`` record it
    #: settles.  ``None`` on an in-flight record and on a call that never had
    #: one.
    resolves: Optional[int] = None

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("sequence must be an int")
        if self.sequence < 1:
            raise ValueError("sequence starts at 1")
        if not isinstance(self.transaction_id, str) or not self.transaction_id.strip():
            raise ValueError("transaction_id must be a non-empty string")
        if not isinstance(self.operation, R1PolicyOperation):
            raise TypeError("operation must be an R1PolicyOperation member")
        if not isinstance(self.outcome, R1OperationOutcome):
            raise TypeError("outcome must be an R1OperationOutcome member")
        if self.resolves is not None:
            if isinstance(self.resolves, bool) or not isinstance(self.resolves, int):
                raise TypeError("resolves must be an int")
            if self.outcome is R1OperationOutcome.IN_FLIGHT:
                raise ValueError("an in-flight record resolves nothing")

    @property
    def is_write(self) -> bool:
        """True when this call is one the worker turns into an E2 CONTROL."""
        return self.operation in WRITE_OPERATIONS

    @property
    def is_terminal(self) -> bool:
        """True once the port has answered, one way or another."""
        return self.outcome in TERMINAL_OUTCOMES

    @property
    def write_may_have_occurred(self) -> bool:
        """The conservative reading: only a refusal proves nothing was sent."""
        return self.is_write and self.outcome is not R1OperationOutcome.REFUSED

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "sequence": self.sequence,
            "transactionId": self.transaction_id,
            "operation": self.operation.value,
            "outcome": self.outcome.value,
            "adapter": self.adapter,
            "policyTypeId": self.policy_type_id,
            "policyId": self.policy_id,
            "fencingToken": self.fencing_token,
            "reference": self.reference,
            "at": self.at,
            "detail": self.detail,
            "resolves": self.resolves,
            "writeMayHaveOccurred": self.write_may_have_occurred,
        }

    @classmethod
    def from_canonical_dict(cls, record: Any) -> "R1Operation":
        return cls(
            sequence=record["sequence"],
            transaction_id=record["transactionId"],
            operation=R1PolicyOperation(record["operation"]),
            outcome=R1OperationOutcome(record["outcome"]),
            adapter=record.get("adapter", ""),
            policy_type_id=record.get("policyTypeId", ""),
            policy_id=record.get("policyId"),
            fencing_token=record.get("fencingToken"),
            reference=record.get("reference", ""),
            at=record.get("at", ""),
            detail=record.get("detail", ""),
            resolves=record.get("resolves"),
        )


class R1OperationJournal(Protocol):
    """Append-only: an entry is added, never revised."""

    def append(self, operation: R1Operation) -> None: ...

    def operations(self, transaction_id: Optional[str] = None
                   ) -> Tuple[R1Operation, ...]: ...

    def next_sequence(self) -> int: ...


#: The empty tally, and the shape every count in this module has.
_EMPTY_COUNTS: Dict[str, int] = {
    "applies": 0, "withdrawals": 0, "refused": 0, "unknown": 0,
}


class _OperationCounts:
    """Counting shared by both implementations, exact beyond the ring.

    The tallies are kept **as records are appended**, per transaction and in
    total, and are never recomputed by scanning the retained entries.  Scanning
    would be wrong rather than merely slow: the ring keeps the most *recent*
    entries, so a CREATE followed by four thousand status reads would report
    zero applies while the durable file still held the write.

    An unresolved ``IN_FLIGHT`` record counts as exactly one possible write.
    Its resolution *replaces* that reading rather than adding to it, so a call
    that completed is never counted twice.
    """

    _total: int
    _tallies: Dict[str, Dict[str, int]]
    _in_flight: Dict[int, R1Operation]

    def _init_counts(self) -> None:
        self._total = 0
        self._tallies = {}
        self._in_flight = {}

    @staticmethod
    def _bucket(operation: R1Operation) -> str:
        return ("withdrawals" if operation.operation is R1PolicyOperation.DELETE
                else "applies")

    def _count(self, operation: R1Operation) -> None:
        self._total += 1
        if not operation.is_write:
            return
        tally = self._tallies.setdefault(operation.transaction_id,
                                         dict(_EMPTY_COUNTS))
        if operation.outcome is R1OperationOutcome.IN_FLIGHT:
            # Conservative from the moment it is written: a call that has left
            # this process may have reached the equipment.
            self._in_flight[operation.sequence] = operation
            tally[self._bucket(operation)] += 1
            tally["unknown"] += 1
            return
        settled = (self._in_flight.pop(operation.resolves, None)
                   if operation.resolves is not None else None)
        if settled is not None:
            tally[self._bucket(settled)] -= 1
            tally["unknown"] -= 1
        if operation.outcome is R1OperationOutcome.REFUSED:
            tally["refused"] += 1
            return
        tally[self._bucket(operation)] += 1
        if operation.outcome is R1OperationOutcome.UNKNOWN:
            tally["unknown"] += 1

    def next_sequence(self) -> int:
        return self._total + 1

    def counts(self, transaction_id: Optional[str] = None) -> Dict[str, int]:
        """``applies`` / ``withdrawals`` / ``refused`` / ``unknown``, exact.

        For one transaction, or summed over every transaction this journal has
        recorded.  ``applies`` and ``withdrawals`` count the calls that **may
        have reached the equipment** -- accepted, lost, or still in flight --
        and ``refused`` the ones the producer answered *no* to, which reached
        nothing.  ``unknown`` marks how many of the first two are the unsettled
        kind, and is a subset rather than a fourth bucket.
        """
        if transaction_id is not None:
            return dict(self._tallies.get(transaction_id, _EMPTY_COUNTS))
        summed = dict(_EMPTY_COUNTS)
        for tally in self._tallies.values():
            for key, value in tally.items():
                summed[key] += value
        return summed

    def unresolved(self) -> Tuple[R1Operation, ...]:
        """In-flight records nothing came back to settle.

        Non-empty after a crash mid-call, and exactly the set a restart must
        treat as writes that may have landed.
        """
        return tuple(self._in_flight[key] for key in sorted(self._in_flight))

    @property
    def total(self) -> int:
        """Every call recorded, writes and reads alike."""
        return self._total

    @property
    def writes_issued(self) -> int:
        """Write calls the port accepted and answered."""
        counts = self.counts()
        return counts["applies"] + counts["withdrawals"] - counts["unknown"]

    @property
    def writes_refused(self) -> int:
        """Write calls refused before acceptance: nothing was sent."""
        return self.counts()["refused"]

    @property
    def writes_unknown(self) -> int:
        """Write calls whose fate the adapter cannot state, in flight included."""
        return self.counts()["unknown"]

    @property
    def writes_that_may_have_occurred(self) -> int:
        """The number a recovery must act on."""
        counts = self.counts()
        return counts["applies"] + counts["withdrawals"]


class InMemoryR1OperationJournal(_OperationCounts):
    """A journal that lives as long as the process, bounded in memory."""

    def __init__(self, limit: int = OPERATION_RING_LIMIT) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive int")
        self._entries: Deque[R1Operation] = deque(maxlen=limit)
        self._init_counts()

    def append(self, operation: R1Operation) -> None:
        self._entries.append(operation)
        self._count(operation)

    def operations(self, transaction_id: Optional[str] = None
                   ) -> Tuple[R1Operation, ...]:
        if transaction_id is None:
            return tuple(self._entries)
        return tuple(entry for entry in self._entries
                     if entry.transaction_id == transaction_id)

    def snapshot(self) -> List[Dict[str, Any]]:
        return [entry.to_canonical_dict() for entry in self._entries]


class JsonlR1OperationJournal(_OperationCounts):
    """A journal on disk, one JSON object per line, flushed before returning.

    Append-only suits the file as well as the record: an entry is written and
    ``fsync``-ed *before* the adapter acts on the call it describes, so a crash
    between the two leaves a line that says a write may have happened.  A file
    that grew under a previous process is read back on construction, which is
    what makes the counts survive a restart.
    """

    def __init__(self, path: str | os.PathLike, *,
                 limit: int = OPERATION_RING_LIMIT) -> None:
        self.path = Path(path)
        self._entries: Deque[R1Operation] = deque(maxlen=limit)
        self._init_counts()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                entry = R1Operation.from_canonical_dict(json.loads(stripped))
                self._entries.append(entry)
                self._count(entry)

    def append(self, operation: R1Operation) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(operation.to_canonical_dict(), sort_keys=True,
                             separators=(",", ":"))
        # 2026-09-22: 프로세스가 `}` 까지 쓰고 개행 전에 죽으면, 로딩은
        # `splitlines()` 라 그 줄을 정상으로 받아들이는데 다음 append 가 바로 이어
        # 붙어 `{...}{...}` 가 된다.  그 다음 기동에서는 journal 을 **통째로** 못
        # 읽어 쓰기 이력과 미확정 작업 복원이 막힌다.  경계를 여기서 복구한다.
        if self.path.exists() and self.path.stat().st_size:
            with self.path.open("rb") as probe:
                probe.seek(-1, os.SEEK_END)
                if probe.read(1) != b"\n":
                    payload = "\n" + payload
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._entries.append(operation)
        self._count(operation)

    def operations(self, transaction_id: Optional[str] = None
                   ) -> Tuple[R1Operation, ...]:
        if transaction_id is None:
            return tuple(self._entries)
        return tuple(entry for entry in self._entries
                     if entry.transaction_id == transaction_id)


def write_counts(operations: Iterable[R1Operation]) -> Dict[str, int]:
    """Apply/withdraw accounting over a **complete** sequence of records.

    ``applies`` and ``withdrawals`` count the calls that **may have reached the
    equipment** -- accepted, lost, or still in flight, because none of the three
    is a proof of absence.  ``refused`` counts the ones the producer answered
    *no* to, which reached nothing, and ``unknown`` marks how many of the first
    two are the unsettled kind.  So ``applies + withdrawals + refused`` is every
    write call made, and ``unknown`` is a subset of the first two rather than a
    fourth bucket.

    Use it on a durable file read back in full, or on a journal's retained
    entries when the ring is known not to have rolled.  For a live journal ask
    :meth:`_OperationCounts.counts` (``R1Adapter.write_counts``) instead: those
    tallies are kept as records are appended and stay exact past the ring.
    """
    entries = list(operations)
    resolved = {entry.resolves for entry in entries
                if entry.resolves is not None}
    counts = dict(_EMPTY_COUNTS)
    for entry in entries:
        if not entry.is_write:
            continue
        bucket = ("withdrawals"
                  if entry.operation is R1PolicyOperation.DELETE else "applies")
        if entry.outcome is R1OperationOutcome.IN_FLIGHT:
            if entry.sequence in resolved:
                continue  # its resolution carries the reading
            counts[bucket] += 1
            counts["unknown"] += 1
            continue
        if entry.outcome is R1OperationOutcome.REFUSED:
            counts["refused"] += 1
            continue
        counts[bucket] += 1
        if entry.outcome is R1OperationOutcome.UNKNOWN:
            counts["unknown"] += 1
    return counts
