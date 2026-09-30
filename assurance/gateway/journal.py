"""The gateway's durable transaction record.

Owner lane: **KGW**.

Design section 8: "The Kernel and Write Gateway retain durable transaction
phase, fencing token, configuration snapshots, reservations, participant state,
and next safe action. Restart recovery blocks new trials until every uncertain
transaction is queried and safely aborted, finalized, rolled back, or placed in
incident lockdown."

Every field below exists because a restart has to answer a question without
asking the equipment first:

``phase`` / :data:`UNCERTAIN_PHASES`
    Whether a change may exist in the field.  A process that died between
    "about to write" and "wrote" leaves :attr:`TransactionPhase.APPLYING`, and
    that is an uncertain transaction -- not an absent one, and not a failed
    one.
``baseline_config_hash`` / ``applied_config_hash``
    The two configuration snapshots a reread is compared against.  Without both
    stored, an observed digest after a restart means nothing.
``applied_axes``
    Which axis writes are believed live, *in apply order*, so a reverse
    rollback after a restart still runs backwards.
``participants``
    Which registered adapters this transaction reached, in first-write order.
    A composition can write a PRIMARY steering policy over one client and a
    SUPPLEMENTARY UE cap over another; a restart that knew only the first would
    call the transaction resolved while the second still held a policy.
``next_safe_action``
    What the Kernel should do next.  Stored rather than recomputed, because the
    component that knew why is the one that wrote it.
``effects``
    The idempotency ledger: derived key to the result that key already
    produced.  This is what makes a retransmitted command a recorded answer
    instead of a second effect.

The journal is a protocol with two implementations.  The in-memory one is for
tests that do not need durability; the JSON file one writes atomically
(temporary file, ``fsync``, ``os.replace``) so a record survives the process
that wrote it -- which is the whole meaning of "durable ready".
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, Mapping, Optional, Protocol, Tuple

__all__ = [
    "UNCERTAIN_PHASES",
    "InMemoryTransactionJournal",
    "JsonFileTransactionJournal",
    "NextSafeAction",
    "TransactionJournal",
    "TransactionPhase",
    "TransactionRecord",
]


class TransactionPhase(Enum):
    """Where one gateway transaction stands, from the gateway's own record."""

    #: Validated and staged; nothing has been written (task section 6.2).
    PREPARED = "PREPARED"
    #: Durably ready.  Survives a gateway restart, which is what lets the
    #: Kernel record ``COMMIT_DECIDED`` on the strength of it.
    READY = "READY"
    #: Written before the first write leaves the gateway.  A record found in
    #: this phase after a restart is uncertain by construction.
    APPLYING = "APPLYING"
    #: Every step applied and confirmed by a configuration reread.
    APPLIED = "APPLIED"
    #: Verified inconsistency: the reread matched neither the baseline nor the
    #: fully applied configuration, or matched a proper prefix of the plan.
    PARTIALLY_APPLIED = "PARTIALLY_APPLIED"
    #: An acknowledgement was lost and the reread could not resolve it.
    UNCERTAIN = "UNCERTAIN"
    #: Downstream refused with no effect, and a reread confirmed the baseline.
    REFUSED = "REFUSED"
    #: Halted.  Reached from any post-commit phase, repeatable.
    STOPPED = "STOPPED"
    #: A reverse rollback is in flight.
    ROLLING_BACK = "ROLLING_BACK"
    #: Reversed and confirmed back at the baseline configuration.
    ROLLED_BACK = "ROLLED_BACK"
    #: Reversal ran but the configuration did not come back to the baseline.
    ROLLBACK_INCOMPLETE = "ROLLBACK_INCOMPLETE"
    #: Driven to the deployment's contracted safe state.
    SAFE_STATE = "SAFE_STATE"
    #: Success finalized: durable decision, reread, finalize acknowledgement.
    FINALIZED = "FINALIZED"


#: Phases in which a change may exist in the field without the gateway being
#: able to say so.  Design section 8 blocks new trials until each of these is
#: queried and resolved; :meth:`WriteGateway.query_transaction` answers
#: ``UNKNOWN`` for them rather than guessing.
UNCERTAIN_PHASES: FrozenSet[TransactionPhase] = frozenset(
    {
        TransactionPhase.APPLYING,
        TransactionPhase.PARTIALLY_APPLIED,
        TransactionPhase.UNCERTAIN,
        TransactionPhase.ROLLING_BACK,
        TransactionPhase.ROLLBACK_INCOMPLETE,
    }
)


class NextSafeAction(Enum):
    """What the Kernel may safely do next with this transaction.

    Recorded by the gateway rather than inferred by the reader: the operation
    that observed the equipment is the one that knows whether the next step is
    a reread or a rollback.
    """

    NONE = "NONE"
    READY = "READY"
    COMMIT = "COMMIT"
    CONFIGURATION_REREAD = "CONFIGURATION_REREAD"
    RECOVERY_CONFIRM = "RECOVERY_CONFIRM"
    REVERSE_ROLLBACK = "REVERSE_ROLLBACK"
    FINALIZE_LIVE = "FINALIZE_LIVE"
    EMERGENCY_SAFE_STATE = "EMERGENCY_SAFE_STATE"
    #: Nothing is outstanding at the equipment; the Kernel may settle.
    SETTLE = "SETTLE"


@dataclass(frozen=True)
class TransactionRecord:
    """One durable gateway transaction.

    ``last_token`` keeps the canonical form of the newest accepted permit so
    fencing is decided by :meth:`~assurance.gateway.token.KernelToken.fences_out`
    itself rather than by a second copy of that rule living here.
    """

    transaction_id: str
    trial_id: str
    phase: TransactionPhase
    adapter: str
    baseline_config_hash: str
    applied_config_hash: str
    plan: Mapping[str, Any]
    last_token: Mapping[str, Any]
    next_safe_action: NextSafeAction = NextSafeAction.NONE
    applied_axes: Tuple[str, ...] = ()
    #: Every registered adapter this transaction writes through, in first-write
    #: order.  Empty for the single-participant case the field did not exist
    #: for; a restart reads it to know which clients must be queried before the
    #: transaction can be called resolved, and reverse rollback unwinds them in
    #: the opposite order.
    participants: Tuple[str, ...] = ()
    observed_config_hash: Optional[str] = None
    watchdog_deadline: Optional[str] = None
    reservations: Mapping[str, str] = field(default_factory=dict)
    effects: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    evidence_refs: Tuple[str, ...] = ()
    updated_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan", MappingProxyType(dict(self.plan)))
        object.__setattr__(self, "last_token", MappingProxyType(dict(self.last_token)))
        object.__setattr__(self, "reservations", MappingProxyType(dict(self.reservations)))
        object.__setattr__(
            self,
            "effects",
            MappingProxyType(
                {key: MappingProxyType(dict(value)) for key, value in dict(self.effects).items()}
            ),
        )
        object.__setattr__(self, "applied_axes", tuple(self.applied_axes))
        object.__setattr__(self, "participants", tuple(self.participants))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))

    @property
    def is_uncertain(self) -> bool:
        """True while a change may exist in the field unconfirmed."""
        return self.phase in UNCERTAIN_PHASES

    def evolve(self, **changes: Any) -> "TransactionRecord":
        """A new record with *changes* applied; records are never mutated."""
        return replace(self, **changes)

    def with_effect(
        self, key: str, effect: Mapping[str, Any]
    ) -> "TransactionRecord":
        """Record what one derived idempotency key produced."""
        ledger = {name: dict(value) for name, value in self.effects.items()}
        ledger[key] = dict(effect)
        return self.evolve(effects=ledger)

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "transactionId": self.transaction_id,
            "trialId": self.trial_id,
            "phase": self.phase.value,
            "adapter": self.adapter,
            "baselineConfigHash": self.baseline_config_hash,
            "appliedConfigHash": self.applied_config_hash,
            "plan": dict(self.plan),
            "lastToken": dict(self.last_token),
            "nextSafeAction": self.next_safe_action.value,
            "appliedAxes": list(self.applied_axes),
            "participants": list(self.participants),
            "observedConfigHash": self.observed_config_hash,
            "watchdogDeadline": self.watchdog_deadline,
            "reservations": dict(self.reservations),
            "effects": {key: dict(value) for key, value in self.effects.items()},
            "evidenceRefs": list(self.evidence_refs),
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_canonical_dict(cls, record: Mapping[str, Any]) -> "TransactionRecord":
        return cls(
            transaction_id=record["transactionId"],
            trial_id=record["trialId"],
            phase=TransactionPhase(record["phase"]),
            adapter=record["adapter"],
            baseline_config_hash=record["baselineConfigHash"],
            applied_config_hash=record["appliedConfigHash"],
            plan=record["plan"],
            last_token=record["lastToken"],
            next_safe_action=NextSafeAction(record["nextSafeAction"]),
            applied_axes=tuple(record["appliedAxes"]),
            participants=tuple(record.get("participants") or ()),
            observed_config_hash=record["observedConfigHash"],
            watchdog_deadline=record["watchdogDeadline"],
            reservations=record["reservations"],
            effects=record["effects"],
            evidence_refs=tuple(record["evidenceRefs"]),
            updated_at=record["updatedAt"],
        )


class TransactionJournal(Protocol):
    """Where the gateway keeps what it must still know after a restart."""

    def read(self, transaction_id: str) -> Optional[TransactionRecord]:
        """The stored record, or ``None`` when the gateway never saw it."""
        ...

    def write(self, record: TransactionRecord) -> None:
        """Store *record* durably before the next command leaves the gateway."""
        ...

    def transaction_ids(self) -> Tuple[str, ...]:
        """Every stored transaction id, sorted."""
        ...


class InMemoryTransactionJournal:
    """A journal that lives as long as the process.

    Honest about what it is: durable enough for a single-process test, and
    :meth:`snapshot` / :meth:`restore` let a test model a restart explicitly
    instead of pretending memory survives one.
    """

    def __init__(self, records: Optional[Mapping[str, Mapping[str, Any]]] = None) -> None:
        self._records: Dict[str, TransactionRecord] = {
            key: TransactionRecord.from_canonical_dict(value)
            for key, value in dict(records or {}).items()
        }

    def read(self, transaction_id: str) -> Optional[TransactionRecord]:
        return self._records.get(transaction_id)

    def write(self, record: TransactionRecord) -> None:
        self._records[record.transaction_id] = record

    def transaction_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._records))

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        return {key: value.to_canonical_dict() for key, value in self._records.items()}


class JsonFileTransactionJournal:
    """A journal on disk, written atomically.

    Temporary file, ``fsync``, ``os.replace``: a crash leaves either the old
    record or the new one, never half of one.  This is the implementation that
    makes ``READY`` mean what design section 7 step 6 needs it to mean -- a
    ready state that only existed in memory would make the Kernel's commit
    decision a promise the gateway cannot keep.
    """

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self._records: Dict[str, TransactionRecord] = {}
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("a gateway journal file holds a JSON object")
            self._records = {
                key: TransactionRecord.from_canonical_dict(value)
                for key, value in loaded.items()
            }

    def read(self, transaction_id: str) -> Optional[TransactionRecord]:
        return self._records.get(transaction_id)

    def write(self, record: TransactionRecord) -> None:
        self._records[record.transaction_id] = record
        self._flush()

    def transaction_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._records))

    def _flush(self) -> None:
        payload = {
            key: value.to_canonical_dict() for key, value in sorted(self._records.items())
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
