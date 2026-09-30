"""The command vocabulary the gateway dispatches through an adapter.

Owner lane: **KGW**.

:meth:`~assurance.gateway.write_gateway.WriteGatewayAdapter.dispatch` takes
"one already-validated command".  This module is what *validated* means: a
closed set of operations, a canonical wire form, and a deterministic derivation
of every field from the :class:`~assurance.gateway.token.KernelToken` that
authorised it.

Two properties matter more than the shape.

**Read-only operations are named as such.**  :data:`SIDE_EFFECT_FREE_OPERATIONS`
is the set an adapter may perform during ``prepare``.  Task section 6.2 forbids
any real configuration change before ``READY``, and a set an adapter can be
tested against is stronger than a sentence asking it not to write.

**Per-step idempotency keys are derived, not invented.**  A permit carries one
``idempotency_key`` for the effect it authorises; a multi-axis change reaches
the equipment as several commands, and each needs its own key or a retry of the
second write would deduplicate against the first.  The derivation is a pure
function of the token and the step index, so the same permit replayed produces
the same keys and therefore the same deduplication -- no counter, no clock and
no random component anywhere in it.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, FrozenSet, Mapping, Optional

from assurance.core.addressing import content_hash
from assurance.gateway.token import KernelToken

__all__ = [
    "SIDE_EFFECT_FREE_OPERATIONS",
    "GatewayOperation",
    "build_command",
    "command_hash",
    "derive_idempotency_key",
]


class GatewayOperation(Enum):
    """What one dispatched command asks the adapter to do."""

    #: Validate that one staged axis write is admissible downstream.  Reads
    #: and local construction only -- no policy created, no E2 command sent.
    #: Carries the axis and value, because "admissible" is a question about the
    #: change, not about the scope: an adapter that could only be asked "is
    #: this scope reachable" would have to wait until commit to discover that
    #: the value violates a capability constraint.
    VALIDATE = "VALIDATE"
    #: Read the live configuration back.  The answer to ``UNKNOWN`` and the
    #: evidence design section 9 requires instead of an acceptance ack.
    READ = "READ"
    #: Apply one axis write.
    APPLY = "APPLY"
    #: Reverse one axis write, restoring the recorded baseline value.
    UNDO = "UNDO"
    #: Halt the change immediately.  A safety action, not a verdict.
    HALT = "HALT"
    #: Confirm the applied change is the live configuration.
    FINALIZE = "FINALIZE"
    #: Arm one contracted watchdog before the commit line.  Carries the
    #: watchdog id and no axis: it does not move the configuration surface,
    #: it installs the guard that will halt a change on that surface.  Task
    #: section 6.3 requires the Kernel to have *observed* the arming before
    #: the durable commit decision, and the gateway is the only component
    #: that reaches the deployment.
    ARM_WATCHDOG = "ARM_WATCHDOG"


#: The operations an adapter may perform without changing anything.  Asserted
#: by the KGW tests against the mock adapter's write log during ``prepare``.
SIDE_EFFECT_FREE_OPERATIONS: FrozenSet[GatewayOperation] = frozenset(
    {GatewayOperation.VALIDATE, GatewayOperation.READ}
)


def derive_idempotency_key(
    token: KernelToken, operation: GatewayOperation, index: int
) -> str:
    """The per-command deduplication key.

    Deterministic in the permit, the operation and the step position, and in
    nothing else.  ``UNDO`` deliberately derives a key distinct from the
    ``APPLY`` of the same axis: reversing a write is a different effect, and
    sharing a key would make the rollback look like a retransmission of the
    apply and be dropped.
    """
    if index < 0:
        raise ValueError("command index must be >= 0")
    return f"{token.idempotency_key}#{operation.value}:{index}"


def build_command(
    token: KernelToken,
    operation: GatewayOperation,
    *,
    scope: Mapping[str, Any],
    axis: Optional[str] = None,
    value: Any = None,
    index: int = 0,
    watchdog_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the canonical command for one dispatch.

    Every behaviour-bearing field comes from the token or the staged plan.
    There is no caller-supplied identity, no free-text field and no place for a
    downstream endpoint: an adapter is told *what* to do, and where to send it
    is the adapter's own registered configuration.
    """
    if not isinstance(operation, GatewayOperation):
        raise TypeError("operation must be a GatewayOperation member")
    if operation in (GatewayOperation.APPLY, GatewayOperation.UNDO) and not axis:
        raise ValueError(f"{operation.value} needs the axis it writes")
    if operation in (GatewayOperation.READ, GatewayOperation.HALT,
                     GatewayOperation.FINALIZE, GatewayOperation.ARM_WATCHDOG) and axis:
        raise ValueError(f"{operation.value} is not about one axis")
    if operation is GatewayOperation.ARM_WATCHDOG and not watchdog_id:
        raise ValueError("ARM_WATCHDOG needs the watchdog it arms")
    if operation is not GatewayOperation.ARM_WATCHDOG and watchdog_id:
        raise ValueError(f"{operation.value} does not arm a watchdog")
    command: Dict[str, Any] = {
        "operation": operation.value,
        "transactionId": token.transaction_id,
        "trialId": token.trial_id,
        "fencingToken": token.fencing_token,
        "commandSequence": token.command_sequence,
        "commandIndex": index,
        "idempotencyKey": derive_idempotency_key(token, operation, index),
        "scope": dict(scope),
    }
    if axis is not None:
        command["axis"] = axis
        command["value"] = value
    if watchdog_id is not None:
        command["watchdogId"] = watchdog_id
    return command


def command_hash(command: Mapping[str, Any]) -> str:
    """Digest of a command, used to tell a retransmission from a collision.

    Same key and same digest is the same command, and the recorded result is
    returned without a second effect.  Same key and a different digest is an
    idempotency collision and is refused -- silently applying it would be the
    one case where a deduplication key makes things worse than none.
    """
    return content_hash(dict(command))
