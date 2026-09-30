"""Shared typed vocabulary for the assurance system.

Every module under ``assurance.core`` is **complete** -- pure data and pure
functions with no behaviour left to fill in -- and therefore has no owning
lane.  They are the specification: the trial state machine *is* the transition
table, the separation of the seven decision axes *is* seven enum types, and
the confirmation model *is* the field list of ``ConfirmationRecord``.  Changing
anything here is an amendment to ``docs/architecture/SEAMS-GATE2.md``, because
all four Gate 2 lanes code against these names simultaneously.

Nothing here imports a toolkit, a transport, a model client or hardware.  The
one outward dependency is ``oran.contract.jcs``, reused through
:mod:`assurance.core.addressing` so the assurance records and the frozen
``oran-aic/1.0.0`` contract artefacts share one canonical digest.
"""

from __future__ import annotations

from assurance.core.addressing import (
    CONTENT_HASH_ALGORITHM,
    CONTENT_HASH_PATTERN,
    CanonicalizationError,
    ContentHashMismatch,
    canonical_bytes,
    content_hash,
    is_content_hash,
    require_content_hash,
    verify_content_hash,
)
from assurance.core.axes import (
    AXIS_ENUMS,
    OPEN_EVIDENCE_CELL_STATUSES,
    AggregateState,
    CandidateAvailability,
    CaseTermination,
    EvidenceCellStatus,
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
    TrialOutcome,
    aggregate_from_cells,
    counts_toward_closure,
    is_non_success,
)
from assurance.core.components import (
    ADVISORY_COMPONENTS,
    AUTHORITATIVE_COMPONENTS,
    ComponentId,
)
from assurance.core.confirmation import (
    FORBIDDEN_CONFIRMATION_FIELDS,
    ConfirmationAction,
    ConfirmationRecord,
)
from assurance.core.envelopes import (
    ASSURANCE_SCHEMA_VERSION,
    EnvelopeAdmissionState,
    EnvelopeRejection,
    EventEnvelope,
    MailboxEnvelope,
    classify_envelope,
)
from assurance.core.provenance import (
    UNIT_DIMENSIONLESS,
    UNIT_PATTERN,
    DocumentStatus,
    InadmissibleQuantityError,
    Provenance,
    TypedQuantity,
)
from assurance.core.states import (
    POST_COMMIT_STATES,
    PRE_COMMIT_STATES,
    SAFETY_PRECEDENCE,
    TERMINAL_TRIAL_STATES,
    TRIAL_TRANSITIONS,
    IllegalTransitionError,
    StopReason,
    TrialState,
    assert_transition,
    is_legal_transition,
    is_terminal,
    outranks_semantic_verdict,
    reachable_from,
    strongest_reason,
    successors,
)
from assurance.core.timebase import (
    UTC_TIMESTAMP_PATTERN,
    TimestampError,
    format_utc,
    is_utc_timestamp,
    parse_utc,
    utc_now,
    utc_now_text,
)

__all__ = [
    # addressing
    "CONTENT_HASH_ALGORITHM", "CONTENT_HASH_PATTERN", "CanonicalizationError",
    "ContentHashMismatch", "canonical_bytes", "content_hash", "is_content_hash",
    "require_content_hash", "verify_content_hash",
    # axes
    "AXIS_ENUMS", "OPEN_EVIDENCE_CELL_STATUSES", "AggregateState",
    "CandidateAvailability", "CaseTermination", "EvidenceCellStatus",
    "ExecutionValidity", "MeasurementSufficiency", "PredicateVerdict",
    "TrialOutcome", "aggregate_from_cells", "counts_toward_closure",
    "is_non_success",
    # components
    "ADVISORY_COMPONENTS", "AUTHORITATIVE_COMPONENTS", "ComponentId",
    # confirmation
    "FORBIDDEN_CONFIRMATION_FIELDS", "ConfirmationAction", "ConfirmationRecord",
    # envelopes
    "ASSURANCE_SCHEMA_VERSION", "EnvelopeAdmissionState", "EnvelopeRejection",
    "EventEnvelope",
    "MailboxEnvelope",
    "classify_envelope",
    # provenance
    "UNIT_DIMENSIONLESS", "UNIT_PATTERN", "DocumentStatus",
    "InadmissibleQuantityError", "Provenance", "TypedQuantity",
    # states
    "POST_COMMIT_STATES", "PRE_COMMIT_STATES", "SAFETY_PRECEDENCE",
    "TERMINAL_TRIAL_STATES", "TRIAL_TRANSITIONS", "IllegalTransitionError",
    "StopReason", "TrialState", "assert_transition", "is_legal_transition",
    "is_terminal", "outranks_semantic_verdict", "reachable_from",
    "strongest_reason", "successors",
    # timebase
    "UTC_TIMESTAMP_PATTERN", "TimestampError", "format_utc", "is_utc_timestamp",
    "parse_utc", "utc_now", "utc_now_text",
]
