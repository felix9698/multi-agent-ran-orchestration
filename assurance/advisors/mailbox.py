"""The typed mailbox sending contract: seal an advisory, and manufacture the
violations task section 6.12 requires a receiver to fail closed on.

Owner lane: **KAGT**.  New file, added under
``docs/architecture/SEAMS-GATE2.md`` section 3.

Design section 4.2: every advisory an agent produces "passes through a typed
Kernel mailbox."  :func:`seal_advisory_message` is the whole of that
requirement from the sending side -- it wraps an
:class:`~assurance.advisors.messages.AdvisoryMessage` in the
:class:`~assurance.core.envelopes.MailboxEnvelope` that carries schema
version, sequence, expiry and idempotency key, with a *computed* content
hash so ``verify_content_hash()`` holds by construction.

Kernel-side admission -- the receiver's decision, per envelope, to admit or
reject -- is KERN's, built on
:func:`assurance.core.envelopes.classify_envelope`.  What KAGT owes every
lane that will write a boundary test against that receiver is a cheap,
correct way to produce a message that violates exactly one rule, so a test
does not have to hand-assemble seven envelope fields to prove one rejection
path fires.  :func:`build_violation_sample` is that factory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from assurance.advisors.messages import AdvisoryMessage
from assurance.core.components import ADVISORY_COMPONENTS
from assurance.core.envelopes import (
    ASSURANCE_SCHEMA_VERSION,
    EnvelopeAdmissionState,
    EnvelopeRejection,
    MailboxEnvelope,
    classify_envelope,
)

__all__ = [
    "MailboxViolationSample",
    "SENDER_VIOLATION_KINDS",
    "build_violation_sample",
    "seal_advisory_message",
]


def seal_advisory_message(
    message: AdvisoryMessage,
    *,
    object_id: str,
    event_id: str,
    sequence: int,
    idempotency_key: str,
    timestamp: Optional[str] = None,
    expiry: Optional[str] = None,
    schema_version: str = ASSURANCE_SCHEMA_VERSION,
) -> MailboxEnvelope:
    """Seal one advisory into the typed Kernel mailbox envelope.

    This is the entire KAGT sending contract: every advisory an agent
    produces leaves this package through here.  ``content_hash`` is computed
    from the message's own canonical form -- never supplied by the caller --
    so a later mismatch on the receiving side means genuine tampering, not a
    sender bug.  Refuses to seal a message from a non-advisory component,
    which would otherwise be the first step toward a component other than
    the three advisory agents reaching the mailbox.
    """
    if message.issued_by not in ADVISORY_COMPONENTS:
        raise ValueError(
            f"{message.issued_by.value} is not an advisory component; "
            "only the Intent Agent, xApp Agent and Evidence Coordinator "
            "send mailbox envelopes (design section 4.2)"
        )
    return MailboxEnvelope.seal(
        schema_version=schema_version,
        object_id=object_id,
        event_id=event_id,
        timestamp=timestamp or message.created_at,
        sequence=sequence,
        idempotency_key=idempotency_key,
        source_component=message.issued_by,
        message_kind=message.kind.value,
        correlation_id=message.correlation_id,
        epoch_hash=message.epoch_hash,
        payload=message.to_canonical_dict(),
        expiry=expiry,
    )


#: The mailbox violations a well-formed sender can reproduce from message
#: content and sequencing alone (task section 6.12).
#: ``SCHEMA_VERSION_UNSUPPORTED``, ``SOURCE_NOT_PERMITTED``,
#: ``CONTENT_HASH_MISMATCH`` and ``EPOCH_MISMATCH`` are receiver-configuration
#: or tamper conditions rather than scenarios a sender manufactures on
#: purpose, so they are out of scope for this factory.
SENDER_VIOLATION_KINDS = frozenset(
    {
        EnvelopeRejection.STALE_SEQUENCE,
        EnvelopeRejection.REORDERED,
        EnvelopeRejection.DUPLICATE_EVENT_ID,
        EnvelopeRejection.REPLAYED_DUPLICATE,
        EnvelopeRejection.IDEMPOTENCY_COLLISION,
        EnvelopeRejection.EXPIRED,
    }
)


@dataclass(frozen=True)
class MailboxViolationSample:
    """A minimal, reproducible mailbox scenario for one violation kind.

    ``admission_state`` already has ``accepted`` recorded, so a consumer can
    call ``classify_envelope(sample.violating, sample.admission_state,
    now=sample.now)`` immediately and get exactly :attr:`kind` back.
    """

    kind: EnvelopeRejection
    admission_state: EnvelopeAdmissionState
    now: str
    accepted: MailboxEnvelope
    violating: MailboxEnvelope


def build_violation_sample(
    kind: EnvelopeRejection,
    *,
    primary_message: AdvisoryMessage,
    now: str,
    object_id: str = "mailbox-violation-sample",
    colliding_message: Optional[AdvisoryMessage] = None,
) -> MailboxViolationSample:
    """Build a self-contained scenario that classifies as exactly *kind*.

    *primary_message* seeds an accepted baseline every scenario is built
    against.  *colliding_message* is required only for
    ``IDEMPOTENCY_COLLISION``: the violation is precisely that a second,
    *different* piece of content reuses the first message's idempotency key,
    so a single message cannot demonstrate it alone.

    Self-verifying: raises if the envelope it just built does not in fact
    classify as *kind*, so a bug here surfaces at the call site rather than
    as a silently-wrong fixture in someone else's test.
    """
    if kind not in SENDER_VIOLATION_KINDS:
        raise ValueError(f"{kind.value} is not a sender-reproducible violation")

    state = EnvelopeAdmissionState(
        supported_schema_versions=frozenset({ASSURANCE_SCHEMA_VERSION}),
        allowed_sources=frozenset(ADVISORY_COMPONENTS),
        expected_epoch_hash=primary_message.epoch_hash,
    )
    accepted = seal_advisory_message(
        primary_message,
        object_id=object_id,
        event_id="evt-accepted",
        sequence=0,
        idempotency_key="idem-accepted",
        timestamp=now,
    )
    if classify_envelope(accepted, state, now=now) is not None:
        raise AssertionError("internal: baseline envelope must itself be admissible")
    state.record(accepted)

    if kind is EnvelopeRejection.STALE_SEQUENCE:
        violating = seal_advisory_message(
            primary_message, object_id=object_id, event_id="evt-stale",
            sequence=0, idempotency_key="idem-stale", timestamp=now,
        )
    elif kind is EnvelopeRejection.REORDERED:
        violating = seal_advisory_message(
            primary_message, object_id=object_id, event_id="evt-reordered",
            sequence=5, idempotency_key="idem-reordered", timestamp=now,
        )
    elif kind is EnvelopeRejection.DUPLICATE_EVENT_ID:
        violating = seal_advisory_message(
            primary_message, object_id=object_id, event_id=accepted.event_id,
            sequence=1, idempotency_key="idem-duplicate-event", timestamp=now,
        )
    elif kind is EnvelopeRejection.REPLAYED_DUPLICATE:
        violating = seal_advisory_message(
            primary_message, object_id=object_id, event_id="evt-replay",
            sequence=1, idempotency_key=accepted.idempotency_key, timestamp=now,
        )
    elif kind is EnvelopeRejection.IDEMPOTENCY_COLLISION:
        if colliding_message is None:
            raise ValueError("IDEMPOTENCY_COLLISION requires colliding_message")
        violating = seal_advisory_message(
            colliding_message, object_id=object_id, event_id="evt-collision",
            sequence=1, idempotency_key=accepted.idempotency_key, timestamp=now,
        )
    elif kind is EnvelopeRejection.EXPIRED:
        violating = seal_advisory_message(
            primary_message, object_id=object_id, event_id="evt-expired",
            sequence=1, idempotency_key="idem-expired", timestamp=now,
            expiry=now,
        )
    else:  # pragma: no cover - guarded by SENDER_VIOLATION_KINDS above
        raise AssertionError(kind)

    actual = classify_envelope(violating, state, now=now)
    if actual is not kind:
        raise AssertionError(f"internal: expected {kind.value}, got {actual}")

    return MailboxViolationSample(
        kind=kind, admission_state=state, now=now, accepted=accepted, violating=violating,
    )
