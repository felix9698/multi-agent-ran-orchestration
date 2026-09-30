"""The gateway's durable transaction-to-policy binding record.

Owner lane: **KGW**.

The R1 adapter's transaction bindings were in-memory: a policy id lived in a
dictionary that a restart erased, so a process that died between "create
returned" and "the Kernel heard about it" left an orphan policy nobody could
address.  This journal is that dictionary made durable, and it carries the four
further facts a restart needs before it can decide anything:

``policy_type_id`` and ``scope_key``
    Which contract owns the scope.  One non-terminal policy per
    ``(policyTypeId, semanticScopeKey)``; a steering policy and a cap policy may
    coexist for the same UE because their *types* differ, and two cap policies
    for one UE conflict.
``policy_revision``
    The durable monotonic revision.  An idempotent replay reuses it rather than
    advancing it, which is what makes the replay a replay.
``baseline_config``
    The pre-policy configuration, uncapped ``0`` included.  Reverse rollback
    restores *this*, not a default, and it must survive the process that
    observed it or the restore has nothing to aim at.
``state``
    :class:`BindingState`.  The one that matters is
    :attr:`BindingState.RESTORE_PENDING`: a DELETE response is not recovery, so
    the binding and the scope lock are held until an independent readback shows
    the baseline back.  Releasing on the DELETE acknowledgement would free the
    scope while the cap may still be live.

Written before the side effect it describes, never after: a record that
appears only once the write succeeded cannot tell a restart that a write may
have happened.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, Mapping, Optional, Protocol, Tuple

__all__ = [
    "BindingState",
    "InMemoryR1BindingJournal",
    "JsonFileR1BindingJournal",
    "R1BindingJournal",
    "R1BindingRecord",
    "UNRELEASED_BINDING_STATES",
]


class BindingState(Enum):
    """Where one transaction-to-policy binding stands."""

    #: The scope is reserved and a revision allocated; no policy exists yet.
    #: A restart finding this may query the producer and find nothing, which
    #: is a complete answer rather than an orphan.
    RESERVED = "RESERVED"
    #: A policy id is bound.  A change may exist in the field.
    BOUND = "BOUND"
    #: DELETE was sent.  The policy row, the scope lock and this binding are
    #: all held until a restore readback proves the baseline is back.
    RESTORE_PENDING = "RESTORE_PENDING"
    #: The baseline was independently observed; the scope is released.
    RESTORED = "RESTORED"


#: States in which the scope is still owned.  New writes on the same scope are
#: refused while any of these stands.
UNRELEASED_BINDING_STATES: FrozenSet[BindingState] = frozenset(
    {BindingState.RESERVED, BindingState.BOUND, BindingState.RESTORE_PENDING}
)


@dataclass(frozen=True)
class R1BindingRecord:
    """One durable binding.  Records are replaced, never mutated."""

    transaction_id: str
    policy_type_id: str
    scope_key: str
    state: BindingState
    policy_revision: int
    policy_id: Optional[str] = None
    baseline_config: Mapping[str, Any] = MappingProxyType({})
    updated_at: str = ""
    detail: str = ""
    #: True only after the bound policy's DELETE returned successfully. A
    #: baseline scalar alone cannot resolve an unknown withdrawal outcome.
    withdrawal_acknowledged: bool = False
    #: True once the trial that created this policy settled and the Kernel
    #: finalized it live.  The policy stays in the field -- that is what
    #: retention means -- but its transaction is finished and will never write
    #: again (``kernel.issue_token`` grants no STOP after a SETTLED_* state).
    #: Holding the scope against every later transaction therefore froze the
    #: axis for the rest of the episode; a finalized owner may be *taken over*
    #: instead, which is an in-place update of the same policy, not a second
    #: policy on one scope.
    finalized: bool = False

    def __post_init__(self) -> None:
        for name in ("transaction_id", "policy_type_id", "scope_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string, got {value!r}")
        if not isinstance(self.state, BindingState):
            raise TypeError("state must be a BindingState member")
        if isinstance(self.policy_revision, bool) or not isinstance(self.policy_revision, int):
            raise TypeError("policy_revision must be an int")
        if self.policy_revision < 1:
            raise ValueError("policy_revision starts at 1")
        if not isinstance(self.withdrawal_acknowledged, bool):
            raise TypeError("withdrawal_acknowledged must be a bool")
        if not isinstance(self.finalized, bool):
            raise TypeError("finalized must be a bool")
        object.__setattr__(self, "baseline_config", MappingProxyType(dict(self.baseline_config)))

    @property
    def holds_scope(self) -> bool:
        """True while this binding still owns its ``(type, scope)`` pair."""
        return self.state in UNRELEASED_BINDING_STATES

    @property
    def takeable(self) -> bool:
        """True when a later transaction may adopt this binding's policy.

        Only a finalized binding with a policy id and no outstanding
        withdrawal: the creating transaction is done, the policy is live, and
        adopting it keeps the one-policy-per-scope invariant intact because
        the adopter *updates* that policy rather than creating a second one.
        """
        return (self.finalized and self.policy_id is not None
                and self.state is BindingState.BOUND
                and not self.withdrawal_acknowledged)

    def evolve(self, **changes: Any) -> "R1BindingRecord":
        return replace(self, **changes)

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "transactionId": self.transaction_id,
            "policyTypeId": self.policy_type_id,
            "scopeKey": self.scope_key,
            "state": self.state.value,
            "policyRevision": self.policy_revision,
            "policyId": self.policy_id,
            "baselineConfig": dict(self.baseline_config),
            "updatedAt": self.updated_at,
            "detail": self.detail,
            "withdrawalAcknowledged": self.withdrawal_acknowledged,
            "finalized": self.finalized,
        }

    @classmethod
    def from_canonical_dict(cls, record: Mapping[str, Any]) -> "R1BindingRecord":
        return cls(
            transaction_id=record["transactionId"],
            policy_type_id=record["policyTypeId"],
            scope_key=record["scopeKey"],
            state=BindingState(record["state"]),
            policy_revision=record["policyRevision"],
            policy_id=record.get("policyId"),
            baseline_config=record.get("baselineConfig") or {},
            updated_at=record.get("updatedAt", ""),
            detail=record.get("detail", ""),
            withdrawal_acknowledged=record.get("withdrawalAcknowledged", False),
            finalized=record.get("finalized", False),
        )


class R1BindingJournal(Protocol):
    """Where the adapter keeps what it must still know after a restart."""

    def binding_for(self, transaction_id: str) -> Optional[R1BindingRecord]: ...

    def write(self, record: R1BindingRecord) -> None: ...

    def transaction_ids(self) -> Tuple[str, ...]: ...


class _BindingStore:
    """Shared lookup behaviour for both journal implementations."""

    _records: Dict[str, R1BindingRecord]

    def binding_for(self, transaction_id: str) -> Optional[R1BindingRecord]:
        return self._records.get(transaction_id)

    def transaction_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._records))

    def scope_owner(
        self, policy_type_id: str, scope_key: str
    ) -> Optional[R1BindingRecord]:
        """The transaction still holding ``(policy type, scope)``, if any.

        One owner at a time, and *per type*: a cap binding does not block a
        steering binding for the same UE, because their policy types differ
        (contract section 2.3).
        """
        for transaction_id in sorted(self._records):
            record = self._records[transaction_id]
            if (record.policy_type_id == policy_type_id
                    and record.scope_key == scope_key
                    and record.holds_scope):
                return record
        return None

    def next_revision(self, policy_type_id: str, scope_key: str) -> int:
        """The revision a fresh policy on this scope must carry.

        Monotonic across the durable history of the scope, so a restart cannot
        reissue a revision the producer has already seen and a lower fence stays
        refused after the process that raised it is gone.
        """
        highest = 0
        for record in self._records.values():
            if record.policy_type_id == policy_type_id and record.scope_key == scope_key:
                highest = max(highest, record.policy_revision)
        return highest + 1

    def next_revision_for_type(self, policy_type_id: str) -> int:
        """The revision a fresh policy of this type must carry.

        Monotonic across every scope this journal has seen for the type, which
        is strictly stronger than per-scope monotonicity and needs no scope key
        -- the key is derived from a body that does not exist until the
        revision is chosen, so asking for it here would be circular.
        """
        highest = 0
        for record in self._records.values():
            if record.policy_type_id == policy_type_id:
                highest = max(highest, record.policy_revision)
        return highest + 1

    def unreleased(self) -> Tuple[R1BindingRecord, ...]:
        """Every binding that still owns a scope, for the recovery sweep."""
        return tuple(
            self._records[key] for key in sorted(self._records)
            if self._records[key].holds_scope
        )


class InMemoryR1BindingJournal(_BindingStore):
    """A binding journal that lives as long as the process.

    :meth:`snapshot` and the constructor let a test model a restart explicitly
    rather than pretending memory survives one.
    """

    def __init__(self, records: Optional[Mapping[str, Mapping[str, Any]]] = None) -> None:
        self._records = {
            key: R1BindingRecord.from_canonical_dict(value)
            for key, value in dict(records or {}).items()
        }

    def write(self, record: R1BindingRecord) -> None:
        self._records[record.transaction_id] = record

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        return {key: value.to_canonical_dict() for key, value in self._records.items()}


class JsonFileR1BindingJournal(_BindingStore):
    """A binding journal on disk, written atomically.

    Temporary file, ``fsync``, ``os.replace``: a crash leaves either the old
    record or the new one.  This is what makes "the policy id was persisted
    before the acknowledgement" a property of the deployment rather than a
    promise in a docstring.
    """

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self._records = {}
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("an R1 binding journal file holds a JSON object")
            self._records = {
                key: R1BindingRecord.from_canonical_dict(value)
                for key, value in loaded.items()
            }

    def write(self, record: R1BindingRecord) -> None:
        records = {**self._records, record.transaction_id: record}
        payload = {
            key: value.to_canonical_dict() for key, value in sorted(records.items())
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            # Failed persistence must not publish a withdrawal ACK or release
            # in memory that a restarted adapter would not recover from disk.
            self._records = records
        finally:
            if os.path.exists(name):
                os.unlink(name)
