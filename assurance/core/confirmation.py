"""The Operator confirmation record.

Complete module: pure data, no owner.

Design section 5 is short and this module is its whole implementation:

    A ``ConfirmationRecord`` contains only: confirmed object type and content
    hash; event ID; timestamp; whether the object changed after confirmation.
    ... The record does not store who clicked or a human role.

    There are no human cryptographic signatures, signer identities,
    trusted-signer policies, role thresholds, joint approval rules, governance
    authorities, or third-party approval records.

The field list is therefore a *ceiling*, not a starting point, and
:data:`FORBIDDEN_CONFIRMATION_FIELDS` exists so a seam test can prove the
ceiling holds instead of trusting this docstring.  Adding ``confirmed_by`` here
would not be a small convenience: it would reintroduce the signer model the
design explicitly overrides, and section 2.2 lists that as excluded scope.

The one behavioural rule is section 5's invalidation: "If confirmed content
changes, the previous confirmation is invalid and the GUI requires a new
click."  :meth:`ConfirmationRecord.is_valid_for` decides that from the content
hash alone, so a confirmation cannot be carried across an edited plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, FrozenSet, Mapping

from assurance.core.addressing import content_hash, is_content_hash
from assurance.core.timebase import is_utc_timestamp

__all__ = [
    "ConfirmationAction",
    "ConfirmationRecord",
    "FORBIDDEN_CONFIRMATION_FIELDS",
]


class ConfirmationAction(Enum):
    """The five Operator controls (design section 5, task section 4.1).

    A closed set.  Every Operator-initiated state change in the Cockpit is one
    of these; anything else the GUI offers is navigation or display.
    ``EMERGENCY_STOP`` is listed here but is not an approval: it flows through
    Kernel ``OPERATOR_ABORT``, Write Gateway stop, rollback and recovery
    (design section 11).
    """

    REVIEW_AND_CONFIRM = "REVIEW_AND_CONFIRM"
    CONFIRM_AND_START = "CONFIRM_AND_START"
    CONFIRM_BATCH_PLAN = "CONFIRM_BATCH_PLAN"
    ABORT = "ABORT"
    EMERGENCY_STOP = "EMERGENCY_STOP"


#: Field names that must never appear on :class:`ConfirmationRecord`.  The
#: seam test asserts the intersection with the dataclass's real fields is
#: empty, which turns design section 5's prohibition into a failing test
#: rather than a review comment.
FORBIDDEN_CONFIRMATION_FIELDS: FrozenSet[str] = frozenset(
    {
        "approver",
        "approval",
        "authority",
        "confirmed_by",
        "identity",
        "joint_approval",
        "key",
        "key_id",
        "operator_id",
        "operator_name",
        "public_key",
        "role",
        "signature",
        "signature_bytes",
        "signed_by",
        "signer",
        "signer_id",
        "threshold",
        "trusted_signer",
        "user",
        "username",
    }
)


@dataclass(frozen=True)
class ConfirmationRecord:
    """One Operator confirmation of one content-addressed object.

    Attributes
    ----------
    confirmed_object_type:
        What was confirmed -- ``"TargetVector"``, ``"BatchExperimentPlan"``,
        ``"CoordinationCasePolicy"``.  A type name, not a description.
    confirmed_content_hash:
        Digest of the exact object shown to the Operator.  This is what makes
        the confirmation checkable: the Kernel re-hashes the object it is
        about to act on and compares.
    event_id:
        Id of the confirmation event in the append-only stream.
    timestamp:
        Canonical UTC instant of the click.
    action:
        Which of the five controls was used.
    changed_after_confirmation:
        Set when the confirmed object was subsequently observed to differ.
        Kept as a recorded fact rather than deleting the record, so the event
        stream shows that a confirmation existed and was invalidated.
    """

    confirmed_object_type: str
    confirmed_content_hash: str
    event_id: str
    timestamp: str
    action: ConfirmationAction
    changed_after_confirmation: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.confirmed_object_type, str) or not self.confirmed_object_type.strip():
            raise ValueError("confirmed_object_type must be a non-empty type name")
        if not is_content_hash(self.confirmed_content_hash):
            raise ValueError(
                f"confirmed_content_hash is not a digest: {self.confirmed_content_hash!r}"
            )
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ValueError("event_id must be a non-empty string")
        if not is_utc_timestamp(self.timestamp):
            raise ValueError(f"timestamp must be canonical UTC, got {self.timestamp!r}")
        if not isinstance(self.action, ConfirmationAction):
            raise TypeError("action must be a ConfirmationAction member")
        if not isinstance(self.changed_after_confirmation, bool):
            raise TypeError("changed_after_confirmation must be a bool")

    # -- section 5 invalidation -------------------------------------------

    def is_valid_for(self, current_content_hash: str) -> bool:
        """True when this confirmation still covers the current object.

        False as soon as the content differs or the record has already been
        marked changed.  There is no tolerance and no "materially unchanged"
        judgement: the design's rule is content equality, and any softer test
        would be a place for a scope change to slip through unconfirmed.
        """
        if self.changed_after_confirmation:
            return False
        return current_content_hash == self.confirmed_content_hash

    def invalidated(self) -> "ConfirmationRecord":
        """A copy marked as changed-after-confirmation.

        Returns a new record rather than mutating: the confirmation is an
        append-only event, and the invalidation is another fact about it.
        """
        if self.changed_after_confirmation:
            return self
        return ConfirmationRecord(
            confirmed_object_type=self.confirmed_object_type,
            confirmed_content_hash=self.confirmed_content_hash,
            event_id=self.event_id,
            timestamp=self.timestamp,
            action=self.action,
            changed_after_confirmation=True,
        )

    # -- content addressing ------------------------------------------------

    def to_canonical_dict(self) -> Dict[str, Any]:
        """Canonical wire form, in the design's camelCase spelling."""
        return {
            "confirmedObjectType": self.confirmed_object_type,
            "confirmedContentHash": self.confirmed_content_hash,
            "eventId": self.event_id,
            "timestamp": self.timestamp,
            "action": self.action.value,
            "changedAfterConfirmation": self.changed_after_confirmation,
        }

    def content_hash(self) -> str:
        """Digest of the confirmation record itself."""
        return content_hash(self.to_canonical_dict())

    @classmethod
    def from_canonical_dict(cls, record: Mapping[str, Any]) -> "ConfirmationRecord":
        """Rebuild from :meth:`to_canonical_dict` output."""
        return cls(
            confirmed_object_type=record["confirmedObjectType"],
            confirmed_content_hash=record["confirmedContentHash"],
            event_id=record["eventId"],
            timestamp=record["timestamp"],
            action=ConfirmationAction(record["action"]),
            changed_after_confirmation=bool(record["changedAfterConfirmation"]),
        )
