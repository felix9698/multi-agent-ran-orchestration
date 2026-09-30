"""The Kernel token.

Complete module: pure data with constructor-enforced invariants, no owner.

Design section 4.4 fixes both the field list and what the object is *not*:

    It accepts only Kernel-issued prepare, commit, stop, rollback, and finalize
    tokens tied to a transaction, trial, fencing token, command sequence,
    lease, expected configuration hash, and idempotency key. ... A token is an
    internal deterministic safety permit, not a human signature.

Each field is a specific failure it prevents, which is why none of them is
optional:

``transaction_id`` / ``trial_id``
    Correlation.  A command that cannot be tied to a transaction cannot be
    recovered after a crash.
``fencing_token``
    Strictly increasing per resource.  A delayed command from a superseded
    attempt arrives with a lower fence and is refused instead of applied late
    (task section 6.12's old-fence rule).
``command_sequence``
    Ordering within one transaction, so a reordered stop and rollback cannot
    execute in the wrong order.
``lease_expiry``
    The permit stops being valid on its own.  A gateway that loses contact
    with the Kernel must not keep acting on an old instruction.
``expected_config_hash``
    What the gateway must observe *before* acting.  This is what makes a
    partial apply detectable and a blind overwrite impossible.
``idempotency_key``
    A retried command with the same key is the same command, not a second one.

There is no signer, no signature, no role and no approval field, and there is
no place to add one: design section 5 removes human cryptographic signatures,
signer identities, trusted-signer policies, role thresholds, joint approval
rules and governance authorities from the system entirely.
:data:`FORBIDDEN_TOKEN_FIELDS` makes that testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, FrozenSet, Mapping

from assurance.core.addressing import content_hash, is_content_hash
from assurance.core.components import ComponentId
from assurance.core.timebase import is_utc_timestamp, parse_utc

__all__ = ["FORBIDDEN_TOKEN_FIELDS", "KernelToken", "TokenKind"]


class TokenKind(Enum):
    """Which gateway operation this permit authorises.

    One kind per :class:`~assurance.gateway.write_gateway.WriteGateway`
    operation, and a token is valid for exactly one of them.  A single
    general-purpose token would mean a permit obtained for a side-effect-free
    prepare could be replayed against a commit.
    """

    #: Side-effect-free validation and staging (design section 7 step 6).
    PREPARE = "PREPARE"
    #: Durable ready confirmation across all components.
    READY = "READY"
    #: Perform the change.  Only after durable ``COMMIT_DECIDED``.
    COMMIT = "COMMIT"
    #: Halt the change immediately; a safety action, not a verdict.
    STOP = "STOP"
    #: Reverse the change in the opposite order it was applied.
    REVERSE_ROLLBACK = "REVERSE_ROLLBACK"
    #: Read the live configuration back without changing it.
    CONFIGURATION_REREAD = "CONFIGURATION_REREAD"
    #: Confirm that recovery reached a known safe state.
    RECOVERY_CONFIRM = "RECOVERY_CONFIRM"
    #: Drive the deployment to its contracted safe state.
    EMERGENCY_SAFE_STATE = "EMERGENCY_SAFE_STATE"
    #: Make a successful change the live configuration (design section 7.9).
    FINALIZE_LIVE = "FINALIZE_LIVE"


#: Field names a Kernel token must never carry (design section 5).  The seam
#: test asserts the intersection with the real field set is empty.
FORBIDDEN_TOKEN_FIELDS: FrozenSet[str] = frozenset(
    {
        "approval",
        "approved_by",
        "authority",
        "certificate",
        "joint_approval",
        "private_key",
        "role",
        "signature",
        "signature_bytes",
        "signed_by",
        "signer",
        "signer_id",
        "threshold",
        "trusted_signer",
    }
)


@dataclass(frozen=True)
class KernelToken:
    """A deterministic safety permit for one Write Gateway operation.

    Attributes
    ----------
    token_kind:
        The single operation this permit authorises.
    transaction_id:
        The durable transaction this belongs to.
    trial_id:
        The trial the transaction serves.
    fencing_token:
        Monotonic, non-negative.  Higher fences out lower.
    command_sequence:
        Position within the transaction, starting at 0.
    lease_expiry:
        Canonical UTC instant after which the permit is void.
    expected_config_hash:
        Configuration digest the gateway must observe before acting.
    idempotency_key:
        Deduplication key for the effect.
    issued_at:
        Canonical UTC issue instant.
    issuer:
        Always
        :attr:`~assurance.core.components.ComponentId.ASSURANCE_KERNEL`;
        enforced, because only the Kernel issues tokens (design section 4.3).
    """

    token_kind: TokenKind
    transaction_id: str
    trial_id: str
    fencing_token: int
    command_sequence: int
    lease_expiry: str
    expected_config_hash: str
    idempotency_key: str
    issued_at: str
    issuer: ComponentId = ComponentId.ASSURANCE_KERNEL

    def __post_init__(self) -> None:
        if not isinstance(self.token_kind, TokenKind):
            raise TypeError("token_kind must be a TokenKind member")
        for name in ("transaction_id", "trial_id", "idempotency_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string, got {value!r}")
        for name in ("fencing_token", "command_sequence"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int")
            if value < 0:
                raise ValueError(f"{name} must be >= 0")
        if not is_utc_timestamp(self.lease_expiry):
            raise ValueError(f"lease_expiry must be canonical UTC, got {self.lease_expiry!r}")
        if not is_utc_timestamp(self.issued_at):
            raise ValueError(f"issued_at must be canonical UTC, got {self.issued_at!r}")
        if parse_utc(self.lease_expiry) <= parse_utc(self.issued_at):
            raise ValueError("lease_expiry must be after issued_at; a lease with no life is not a permit")
        if not is_content_hash(self.expected_config_hash):
            raise ValueError(
                f"expected_config_hash is not a digest: {self.expected_config_hash!r}"
            )
        if self.issuer is not ComponentId.ASSURANCE_KERNEL:
            raise ValueError("only the Assurance Kernel issues tokens (design section 4.3)")

    # -- validity ----------------------------------------------------------

    def is_expired(self, now: str) -> bool:
        """True when the lease has run out at *now*."""
        return parse_utc(now) >= parse_utc(self.lease_expiry)

    def fences_out(self, other: "KernelToken") -> bool:
        """True when this token supersedes *other* on the same transaction.

        Compares the fencing token first and the command sequence only within
        the same fence, because a new fence restarts the command sequence.
        Tokens for different transactions never fence each other -- returning
        ``True`` there would let one trial's retry invalidate another's
        in-flight command.
        """
        if self.transaction_id != other.transaction_id:
            return False
        if self.fencing_token != other.fencing_token:
            return self.fencing_token > other.fencing_token
        return self.command_sequence > other.command_sequence

    def authorises(self, kind: TokenKind) -> bool:
        """True only for the one operation this permit was issued for."""
        return self.token_kind is kind

    # -- content addressing ------------------------------------------------

    def to_canonical_dict(self) -> Dict[str, Any]:
        """Canonical wire form, in the design's camelCase spelling."""
        return {
            "tokenKind": self.token_kind.value,
            "transactionId": self.transaction_id,
            "trialId": self.trial_id,
            "fencingToken": self.fencing_token,
            "commandSequence": self.command_sequence,
            "leaseExpiry": self.lease_expiry,
            "expectedConfigHash": self.expected_config_hash,
            "idempotencyKey": self.idempotency_key,
            "issuedAt": self.issued_at,
            "issuer": self.issuer.value,
        }

    def content_hash(self) -> str:
        """Digest of the token, recorded in the transaction event."""
        return content_hash(self.to_canonical_dict())

    @classmethod
    def from_canonical_dict(cls, record: Mapping[str, Any]) -> "KernelToken":
        """Rebuild a token from :meth:`to_canonical_dict` output."""
        return cls(
            token_kind=TokenKind(record["tokenKind"]),
            transaction_id=record["transactionId"],
            trial_id=record["trialId"],
            fencing_token=record["fencingToken"],
            command_sequence=record["commandSequence"],
            lease_expiry=record["leaseExpiry"],
            expected_config_hash=record["expectedConfigHash"],
            idempotency_key=record["idempotencyKey"],
            issued_at=record["issuedAt"],
            issuer=ComponentId(record["issuer"]),
        )
