"""Append-only event and typed-mailbox envelopes.

Complete module: pure data and pure functions, no owner.

Design section 6.1 puts a schema version, object id, record/event id,
timestamp and content hash on every runtime-relevant object.  Section 6.12 of
the task then requires the Kernel to fail closed on "stale, reordered,
duplicate, expired message and idempotency collision, old-fence commit/finalize",
and design section 15 lists the same set as a verification requirement for the
typed mailbox.

Those two requirements together decide the field list here: an envelope must
carry enough for a receiver to make every one of those judgements *from the
record alone*, without asking the sender anything.  Hence ``sequence`` (stale
and reordered), ``event_id`` (duplicate), ``expiry`` (expired),
``idempotency_key`` plus ``content_hash`` (duplicate versus collision -- the
same key with different content is a collision, the same key with the same
content is a replay), and ``source_component`` (routing and correlation only,
never authority -- see :mod:`assurance.core.components`).

``content_hash`` is deliberately *not* verified in ``__post_init__``.  A
tampered or truncated envelope has to be constructible in order to be
classified and rejected; enforcing the digest at construction would make
:attr:`EnvelopeRejection.CONTENT_HASH_MISMATCH` unreachable and move the
failure into a stack trace instead of the event stream.  Use :meth:`seal` to
build a correct envelope and :func:`classify_envelope` to judge a received one.

Envelopes compare by value and are intentionally unhashable (they carry a
payload mapping).  Use ``event_id`` or ``content_hash`` as a set/dict key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from collections import abc as _abc
from typing import Any, Dict, Final, FrozenSet, Mapping, Optional, Set

from assurance.core.addressing import (
    canonical_bytes,
    content_hash as digest_of,
    is_content_hash,
)
from assurance.core.components import ComponentId
from assurance.core.timebase import is_utc_timestamp, parse_utc

__all__ = [
    "ASSURANCE_SCHEMA_VERSION",
    "EnvelopeAdmissionState",
    "EnvelopeRejection",
    "EventEnvelope",
    "MailboxEnvelope",
    "classify_envelope",
]

#: Schema version carried by every runtime envelope this package emits.
#: Bumping it is an epoch-affecting change and requires a new evidence epoch
#: (design section 6.3), not a local edit.
#:
#: Defined here rather than in ``assurance/__init__.py`` so it stays reachable
#: from a submodule import.  ``tests/assurance/__init__.py`` extends its
#: ``__path__`` onto this package instead of re-executing the package body, and
#: a constant that lived only in that body would vanish under
#: ``python3 -m unittest discover -s tests``.
ASSURANCE_SCHEMA_VERSION: Final[str] = "assurance/1.0.0"


class EnvelopeRejection(Enum):
    """Why a received envelope is refused.

    Fail-closed vocabulary: the classifier returns ``None`` for admissible and
    exactly one of these otherwise.  Every member is a distinguishable
    condition in task section 6.12 / design section 15, kept separate because
    the paper reports "proposal rejection/staleness" counts per reason
    (design section 13).
    """

    #: The envelope's schema version is not one this receiver admits.  A new
    #: schema version waits for a new evidence epoch (design section 6.3).
    SCHEMA_VERSION_UNSUPPORTED = "SCHEMA_VERSION_UNSUPPORTED"
    #: ``content_hash`` does not match the payload it claims to describe.
    CONTENT_HASH_MISMATCH = "CONTENT_HASH_MISMATCH"
    #: ``expiry`` is in the past.  Late is not "slightly early".
    EXPIRED = "EXPIRED"
    #: This ``event_id`` has already been accepted.
    DUPLICATE_EVENT_ID = "DUPLICATE_EVENT_ID"
    #: Same idempotency key, same content: a benign retransmission.  Refused
    #: as an append, because appending it twice would double-count evidence.
    REPLAYED_DUPLICATE = "REPLAYED_DUPLICATE"
    #: Same idempotency key, *different* content.  This is the dangerous one:
    #: something reused a key for new intent.  Never resolved by preferring
    #: either version.
    IDEMPOTENCY_COLLISION = "IDEMPOTENCY_COLLISION"
    #: ``sequence`` is at or behind the last accepted one for this object.
    STALE_SEQUENCE = "STALE_SEQUENCE"
    #: ``sequence`` skips ahead: an earlier envelope has not arrived yet.
    REORDERED = "REORDERED"
    #: The sender is not one this receiver accepts on this channel.
    SOURCE_NOT_PERMITTED = "SOURCE_NOT_PERMITTED"
    #: The message was formed against a different evidence epoch than the one
    #: currently frozen -- the mailbox form of a stale proposal.
    EPOCH_MISMATCH = "EPOCH_MISMATCH"


#: `_jsonable` 의 빠른 경로.  JSON 스칼라는 그대로 통과시킨다 -- 여기서 걸러야
#: 값마다 ABC `isinstance` 를 두 번 타지 않는다.
_SCALARS = frozenset((str, int, float, bool, type(None)))
#: 의미는 `typing.Mapping` 과 같고 `isinstance` 가 훨씬 싸다.
_ABCMapping = _abc.Mapping


def _jsonable(value: Any) -> Any:
    """Convert the two Python spellings of a JSON structure into one.

    ``tuple`` becomes ``list`` and any ``Mapping`` becomes ``dict``, because a
    frozen dataclass naturally holds tuples while a decoded wire message holds
    lists, and the two must not produce different digests for the same
    content.  Everything else is passed straight to the canonicaliser, which
    refuses what JSON cannot represent -- sets, ``NaN``, ``Decimal`` -- rather
    than guessing an encoding for it.
    """
    # 2026-09-22 성능: 이 함수는 커널에 들어가는 **모든 표본의 payload 를 값마다**
    # 재귀로 훑는다.  `Mapping` 은 `typing` 에서 왔고 `typing.Mapping` 에 대한
    # `isinstance` 는 `__subclasscheck__` 를 타서 스칼라 하나에 `type() is str` 의
    # **8배**가 든다(실측).  라이브 판이 CPU 를 100% 태우며 여기 머물러 있었다 --
    # 스택 최상단이 `typing.py:1158 __subclasscheck__` 였다.
    # 대부분의 값은 스칼라이므로 그것을 **먼저 정확 타입으로** 걸러내고, 컨테이너
    # 검사에는 `collections.abc` 를 쓴다(같은 의미, 2.3배 빠르다).
    if type(value) in _SCALARS:
        return value
    if isinstance(value, _ABCMapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _normalised_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Round-trip *payload* through canonical JSON.

    This both validates (non-canonicalisable content raises here, at the
    boundary) and normalises, so two envelopes built from equivalent Python
    structures -- ``tuple`` versus ``list``, key insertion order -- compare
    equal and hash identically.
    """
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    return json.loads(canonical_bytes(_jsonable(payload)).decode("utf-8"))


@dataclass(frozen=True)
class _EnvelopeBase:
    """Fields shared by every envelope in the system (design section 6.1)."""

    #: Version of the envelope schema, e.g. ``assurance/1.0.0``.
    schema_version: str
    #: The object this record belongs to: a case, trial, contract or epoch id.
    #: Sequence numbers are per ``object_id``.
    object_id: str
    #: Unique id of this record.  Duplicate ids are refused, not merged.
    event_id: str
    #: Canonical UTC creation instant (:mod:`assurance.core.timebase`).
    timestamp: str
    #: Canonical digest of :attr:`payload`.  Not verified at construction --
    #: see the module docstring.
    content_hash: str
    #: Per-object monotonic counter, starting at 0.  Gaps mean reordering,
    #: repeats mean staleness.
    sequence: int
    #: Canonical UTC instant after which this record must not be acted on, or
    #: ``None`` for a record that does not expire (durable ledger events).
    expiry: Optional[str]
    #: Deduplication key for the *effect* this record requests.  Distinct from
    #: :attr:`event_id`, which identifies the record.
    idempotency_key: str
    #: Emitting component.  Routing and correlation only (section 4.8).
    source_component: ComponentId
    #: Canonical JSON payload.
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in ("schema_version", "object_id", "event_id", "idempotency_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string, got {value!r}")
        if not is_utc_timestamp(self.timestamp):
            raise ValueError(
                f"timestamp must be canonical UTC (…Z, microseconds), got {self.timestamp!r}"
            )
        if self.expiry is not None and not is_utc_timestamp(self.expiry):
            raise ValueError(f"expiry must be canonical UTC or None, got {self.expiry!r}")
        if not is_content_hash(self.content_hash):
            raise ValueError(f"content_hash is not a digest: {self.content_hash!r}")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("sequence must be an int")
        if self.sequence < 0:
            raise ValueError("sequence must be >= 0")
        if not isinstance(self.source_component, ComponentId):
            raise TypeError("source_component must be a ComponentId member")
        object.__setattr__(self, "payload", _normalised_payload(self.payload))
        object.__setattr__(self, "_payload_digest", None)

    # -- content addressing ------------------------------------------------

    def payload_digest(self) -> str:
        """The digest the payload actually has.

        2026-09-22 성능: `event_store._validate_append` 가 **append 마다**
        `verify_content_hash()` 를 부르고, 그때마다 payload 전체를 JCS 로 다시 훑었다.
        라이브 판이 CPU 를 98% 태우며 `jcs.canonicalize` 재귀에 머물러 있었다 --
        스택 최상단이 그것이었다.  이 봉투는 frozen 이고 payload 는 `__post_init__`
        에서 이미 정규화돼 더 바뀌지 않으므로, digest 는 **한 번만** 계산하면 된다.
        """
        cached = getattr(self, "_payload_digest", None)
        if cached is None:
            cached = digest_of(self.payload)
            object.__setattr__(self, "_payload_digest", cached)
        return cached

    def verify_content_hash(self) -> bool:
        """True when :attr:`content_hash` describes :attr:`payload`."""
        return self.payload_digest() == self.content_hash

    def to_canonical_dict(self) -> Dict[str, Any]:
        """Canonical wire form, in the design's camelCase spelling.

        ``expiry`` is omitted when absent rather than emitted as ``null``, so
        a non-expiring record has exactly one canonical form.
        """
        record: Dict[str, Any] = {
            "schemaVersion": self.schema_version,
            "objectId": self.object_id,
            "eventId": self.event_id,
            "timestamp": self.timestamp,
            "contentHash": self.content_hash,
            "sequence": self.sequence,
            "idempotencyKey": self.idempotency_key,
            "sourceComponent": self.source_component.value,
            "payload": self.payload,
        }
        if self.expiry is not None:
            record["expiry"] = self.expiry
        return record

    def envelope_hash(self) -> str:
        """Digest over the whole envelope, not just the payload.

        This is what an event store chains and what a terminal-state hash is
        ultimately built from: two runs that accepted the same envelopes in
        the same order produce the same sequence of envelope hashes.
        """
        return digest_of(self.to_canonical_dict())

    def is_expired(self, now: str) -> bool:
        """True when *now* is at or past :attr:`expiry`."""
        if self.expiry is None:
            return False
        return parse_utc(now) >= parse_utc(self.expiry)


@dataclass(frozen=True)
class EventEnvelope(_EnvelopeBase):
    """One record in the Kernel's append-only event stream.

    Design section 4.3: "The same accepted event stream and reducer version
    must reproduce the same ledger and terminal state hash without any LLM."
    Everything a replay needs is therefore in the envelope; nothing is carried
    in ambient process state.
    """

    #: What happened, e.g. ``ContractAdmitted``, ``TrialStateChanged``,
    #: ``HarmCharged``, ``TrialSettled``.  The vocabulary is owned by the
    #: Kernel lane; the envelope only requires it to be a non-empty token.
    event_kind: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.event_kind, str) or not self.event_kind.strip():
            raise ValueError("event_kind must be a non-empty string")

    def to_canonical_dict(self) -> Dict[str, Any]:
        record = super().to_canonical_dict()
        record["eventKind"] = self.event_kind
        return record

    @classmethod
    def seal(
        cls,
        *,
        schema_version: str,
        object_id: str,
        event_id: str,
        timestamp: str,
        sequence: int,
        idempotency_key: str,
        source_component: ComponentId,
        event_kind: str,
        payload: Mapping[str, Any],
        expiry: Optional[str] = None,
    ) -> "EventEnvelope":
        """Build an envelope whose ``content_hash`` is computed, not supplied.

        The only correct way to create an event: the digest is derived from
        the normalised payload, so ``verify_content_hash()`` is true by
        construction and a later mismatch means genuine tampering.
        """
        normalised = _normalised_payload(payload)
        return cls(
            schema_version=schema_version,
            object_id=object_id,
            event_id=event_id,
            timestamp=timestamp,
            content_hash=digest_of(normalised),
            sequence=sequence,
            expiry=expiry,
            idempotency_key=idempotency_key,
            source_component=source_component,
            payload=normalised,
            event_kind=event_kind,
        )


@dataclass(frozen=True)
class MailboxEnvelope(_EnvelopeBase):
    """One typed advisory message on its way into the Kernel mailbox.

    Design section 4.2: the three advisory agents "cannot add candidates,
    change targets, issue actuator commands, assign verdicts, modify ledgers,
    release a target vector, or terminate a case.  Their messages pass through
    a typed Kernel mailbox."  This envelope is that mailbox's outer layer; the
    typed proposal itself is :class:`assurance.advisors.messages.AdvisoryMessage`,
    carried in :attr:`payload`.

    :attr:`epoch_hash` is the field that makes a stale proposal detectable.  An
    advisory formed while looking at one frozen catalog is meaningless against
    a different one, and section 6.3 forbids the catalog changing underneath
    it -- so a mismatch is a rejection, never a re-interpretation.
    """

    #: The advisory kind, e.g. ``IntentDraft``, ``CandidateAssessment``,
    #: ``NextCandidateProposal``.  See ``assurance.advisors.messages``.
    message_kind: str
    #: Correlates a proposal with the case/trial it answers and with the
    #: Kernel event it eventually produces.
    correlation_id: str
    #: Content hash of the evidence epoch the sender formed this against.
    epoch_hash: str

    def __post_init__(self) -> None:
        super().__post_init__()
        for name in ("message_kind", "correlation_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not is_content_hash(self.epoch_hash):
            raise ValueError(f"epoch_hash is not a digest: {self.epoch_hash!r}")

    def to_canonical_dict(self) -> Dict[str, Any]:
        record = super().to_canonical_dict()
        record["messageKind"] = self.message_kind
        record["correlationId"] = self.correlation_id
        record["epochHash"] = self.epoch_hash
        return record

    @classmethod
    def seal(
        cls,
        *,
        schema_version: str,
        object_id: str,
        event_id: str,
        timestamp: str,
        sequence: int,
        idempotency_key: str,
        source_component: ComponentId,
        message_kind: str,
        correlation_id: str,
        epoch_hash: str,
        payload: Mapping[str, Any],
        expiry: Optional[str] = None,
    ) -> "MailboxEnvelope":
        """Build a mailbox envelope with a computed ``content_hash``."""
        normalised = _normalised_payload(payload)
        return cls(
            schema_version=schema_version,
            object_id=object_id,
            event_id=event_id,
            timestamp=timestamp,
            content_hash=digest_of(normalised),
            sequence=sequence,
            expiry=expiry,
            idempotency_key=idempotency_key,
            source_component=source_component,
            payload=normalised,
            message_kind=message_kind,
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
        )


@dataclass
class EnvelopeAdmissionState:
    """Everything a receiver needs to judge the next envelope.

    Mutable on purpose and deliberately small: it is a per-object receiver
    cursor, not a store.  The Kernel lane owns the durable version; this type
    exists so the rule set is written once, in pure code, and so the seam
    tests can exercise every rejection without a Kernel.

    ``last_sequence`` starts at ``-1`` so the first admissible envelope is
    sequence ``0``.
    """

    supported_schema_versions: FrozenSet[str]
    allowed_sources: FrozenSet[ComponentId]
    expected_epoch_hash: Optional[str] = None
    last_sequence: int = -1
    seen_event_ids: Set[str] = field(default_factory=set)
    #: idempotency key -> content hash of the envelope that first used it.
    seen_idempotency: Dict[str, str] = field(default_factory=dict)

    def record(self, envelope: _EnvelopeBase) -> None:
        """Advance the cursor after an envelope has been accepted.

        Call only for envelopes :func:`classify_envelope` admitted; recording
        a rejected envelope would let a bad sequence number poison the cursor.
        """
        self.last_sequence = envelope.sequence
        self.seen_event_ids.add(envelope.event_id)
        self.seen_idempotency[envelope.idempotency_key] = envelope.content_hash


def classify_envelope(
    envelope: _EnvelopeBase,
    state: EnvelopeAdmissionState,
    *,
    now: str,
) -> Optional[EnvelopeRejection]:
    """Return the rejection reason for *envelope*, or ``None`` if admissible.

    The checks run most-fundamental-first, so the reported reason is the one
    that made the envelope unusable rather than whichever check happened to
    run first.  Schema and sender before content, content before freshness,
    identity before ordering: an envelope whose payload does not match its digest is
    reported as a hash mismatch even if it is also stale, because the digest
    failure means its sequence number cannot be trusted either.

    Pure: it reads *state* and never mutates it.  The caller records an
    accepted envelope with :meth:`EnvelopeAdmissionState.record`, so an
    envelope that is admitted but then fails a Kernel-level rule does not
    silently advance the cursor.
    """
    if envelope.schema_version not in state.supported_schema_versions:
        return EnvelopeRejection.SCHEMA_VERSION_UNSUPPORTED
    if envelope.source_component not in state.allowed_sources:
        return EnvelopeRejection.SOURCE_NOT_PERMITTED
    if not envelope.verify_content_hash():
        return EnvelopeRejection.CONTENT_HASH_MISMATCH
    if (
        state.expected_epoch_hash is not None
        and isinstance(envelope, MailboxEnvelope)
        and envelope.epoch_hash != state.expected_epoch_hash
    ):
        return EnvelopeRejection.EPOCH_MISMATCH
    if envelope.is_expired(now):
        return EnvelopeRejection.EXPIRED
    if envelope.event_id in state.seen_event_ids:
        return EnvelopeRejection.DUPLICATE_EVENT_ID
    previous = state.seen_idempotency.get(envelope.idempotency_key)
    if previous is not None:
        if previous == envelope.content_hash:
            return EnvelopeRejection.REPLAYED_DUPLICATE
        return EnvelopeRejection.IDEMPOTENCY_COLLISION
    if envelope.sequence <= state.last_sequence:
        return EnvelopeRejection.STALE_SEQUENCE
    if envelope.sequence > state.last_sequence + 1:
        return EnvelopeRejection.REORDERED
    return None
