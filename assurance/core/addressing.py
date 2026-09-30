"""Content addressing for assurance records.

Complete module: a thin, deliberate wrapper, no owner.

Design section 6.1 puts a content hash on every runtime-relevant object, and
section 6.3 freezes an epoch by hashing contracts, manifests, bindings and the
candidate universe.  Section 17.5 then requires raw evidence to reproduce every
verdict and terminal state, which is only true if two processes that hold the
same object compute the same digest.

The canonicalisation is **not reimplemented here**.  ``oran/contract/jcs.py``
already carries the project's RFC 8785 implementation, it is the digest that
the frozen ``oran-aic/1.0.0`` contract bundle and the R1/A1 policy path already
publish, and a second implementation would eventually disagree with the first
on a float boundary or a surrogate pair.  This module exists to give the
assurance package one import site, one hash-shape predicate and one mismatch
error, so a caller never has to know which canonicaliser is in use.

Digest shape is the bare lowercase SHA-256 hex string produced by
``jcs_sha256`` -- the same shape already stored in ``policySha256`` /
``jobDefinitionSha256`` fields on the R1 path -- so an assurance record and an
O-RAN artefact can be compared without a prefix-stripping step.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Final

from oran.contract.jcs import CanonicalizationError, canonicalize_bytes, jcs_sha256

__all__ = [
    "CONTENT_HASH_ALGORITHM",
    "CONTENT_HASH_PATTERN",
    "TRACE_UUID_NAMESPACE",
    "ContentHashMismatch",
    "CanonicalizationError",
    "canonical_bytes",
    "content_hash",
    "is_content_hash",
    "deterministic_uuid",
    "require_content_hash",
    "verify_content_hash",
]

#: Named so an evidence bundle records which algorithm produced its digests.
CONTENT_HASH_ALGORITHM: Final[str] = "sha-256"

#: A digest is 64 lowercase hex characters and nothing else.
CONTENT_HASH_PATTERN: Final[re.Pattern] = re.compile(r"^[0-9a-f]{64}$")

#: Namespace for identifiers that must be UUIDs on an O-RAN interface but
#: originate as Kernel identifiers.  Itself derived rather than a magic
#: literal, so the constant can be recomputed from the string that names it.
TRACE_UUID_NAMESPACE: Final[uuid.UUID] = uuid.uuid5(
    uuid.NAMESPACE_URL, "urn:assurance:trace"
)


class ContentHashMismatch(ValueError):
    """A record's stored content hash does not match its content.

    Raised rather than returned in the sealing paths: a mismatch means the
    object changed after it was hashed, which invalidates any Operator
    confirmation over it (design section 5) and any epoch that froze it.
    """


def canonical_bytes(value: Any) -> bytes:
    """RFC 8785 canonical JSON bytes for *value*."""
    return canonicalize_bytes(value)


def content_hash(value: Any) -> str:
    """The canonical SHA-256 digest of *value*.

    *value* must be JSON-representable: mappings with string keys, sequences,
    strings, finite numbers, booleans and ``None``.  Non-finite floats and lone
    surrogates raise :class:`CanonicalizationError` -- a number that cannot be
    canonicalised cannot be evidence.
    """
    return jcs_sha256(value)


def is_content_hash(value: object) -> bool:
    """True when *value* has the shape of a canonical digest."""
    return isinstance(value, str) and bool(CONTENT_HASH_PATTERN.match(value))


def require_content_hash(value: object, field: str) -> str:
    """Return *value* when it is a digest, else raise ``ValueError``.

    Used by the complete envelope types so a malformed digest is refused at
    construction instead of surfacing later as a silent comparison failure.
    """
    if not is_content_hash(value):
        raise ValueError(f"{field} is not a {CONTENT_HASH_ALGORITHM} digest: {value!r}")
    return value  # type: ignore[return-value]


def verify_content_hash(value: Any, expected: str) -> bool:
    """True when *value* hashes to *expected*."""
    return content_hash(value) == expected


def deterministic_uuid(name: str, *, namespace: uuid.UUID = TRACE_UUID_NAMESPACE) -> str:
    """A UUID derived from a Kernel identifier, never drawn.

    Several O-RAN interfaces type an identifier as a UUID where the Kernel's
    own identifier is a readable path -- ``AIC_UECellSteering_1.0.0``'s
    ``trace.intentId`` and ``trace.correlationId`` against a trial id like
    ``case/pin-to-cell:trial:1``.  Minting a fresh UUID there would put a
    random value in the event stream and in the policy body, and design
    section 4.3 requires the same stream to replay to the same terminal state
    hash.  A drawn identifier makes that false on the second run.

    UUIDv5 over :data:`TRACE_UUID_NAMESPACE`: a pure function of *name*, so
    the value is reproducible from the identifier it stands for and two
    different Kernel identifiers cannot collide onto one UUID by accident.
    Returned as a string because that is what the wire schemas type.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"a deterministic uuid needs a non-empty name, got {name!r}")
    return str(uuid.uuid5(namespace, name))
