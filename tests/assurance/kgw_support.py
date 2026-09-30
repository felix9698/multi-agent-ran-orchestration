"""Shared fixtures for the KGW (Write Gateway) tests.

Not a test module: it holds the deployment the ``test_kgw_*`` files act on, so
each of them can be read as a list of claims about the gateway rather than as a
setup script.  Everything here is hermetic -- an in-memory configuration store,
an injected clock, no socket, no model and no testbed.

The deployment is deliberately multi-axis.  A single-axis change cannot be
*partially* applied, and partial apply is the case that shapes the whole
gateway interface (see ``assurance/gateway/write_gateway.py``).
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from assurance.gateway.gateway import TokenBoundWriteGateway
from assurance.gateway.journal import InMemoryTransactionJournal
from assurance.gateway.mock_adapter import FaultInjection, MockActuationAdapter
from assurance.gateway.plan import config_hash
from assurance.gateway.token import KernelToken, TokenKind

ISSUED = "2026-08-21T09:00:00.000000Z"
LEASE = "2026-08-21T10:00:00.000000Z"
AFTER_LEASE = "2026-08-21T11:00:00.000000Z"

#: The contracted configuration surface of the fixture deployment.
BASELINE: Mapping[str, Any] = {
    "servingCell": "cell-1",
    "prbCap": 24,
    "queuePriority": 1,
}

#: The deployment's contracted safe configuration.  Different from the
#: baseline on purpose: a safe state equal to the baseline would make an
#: emergency safe state indistinguishable from a rollback.
SAFE_STATE: Mapping[str, Any] = {
    "servingCell": "cell-1",
    "prbCap": 24,
    "queuePriority": 0,
}

SCOPE: Mapping[str, Any] = {"guAmfUeNgapId": "ue-1"}

#: Two axes, applied in this order and reversed in the opposite one.
PLAN: Mapping[str, Any] = {
    "adapter": "mock",
    "scope": dict(SCOPE),
    "baselineConfig": dict(BASELINE),
    "steps": [
        {"axis": "servingCell", "value": "cell-2"},
        {"axis": "queuePriority", "value": 7},
    ],
}

BASELINE_HASH = config_hash(BASELINE)
APPLIED = {"servingCell": "cell-2", "prbCap": 24, "queuePriority": 7}
APPLIED_HASH = config_hash(APPLIED)
PARTIAL = {"servingCell": "cell-2", "prbCap": 24, "queuePriority": 1}
PARTIAL_HASH = config_hash(PARTIAL)
SAFE_HASH = config_hash(SAFE_STATE)


class TestClock:
    """An injected clock.  Nothing in the gateway reads the wall clock."""

    def __init__(self, now: str = ISSUED) -> None:
        self.now = now

    def __call__(self) -> str:
        return self.now


def token(
    kind: TokenKind,
    *,
    transaction_id: str = "tx-1",
    trial_id: str = "trial-1",
    fence: int = 1,
    sequence: int = 0,
    expected: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    lease: str = LEASE,
    issued: str = ISSUED,
) -> KernelToken:
    """A Kernel permit for one operation.

    The idempotency key defaults to one derived from the transaction, the kind
    and the sequence, which is what a Kernel that never reuses a key for two
    effects would produce; the fencing tests override it to produce one that
    does.
    """
    return KernelToken(
        token_kind=kind,
        transaction_id=transaction_id,
        trial_id=trial_id,
        fencing_token=fence,
        command_sequence=sequence,
        lease_expiry=lease,
        expected_config_hash=expected if expected is not None else BASELINE_HASH,
        idempotency_key=idempotency_key
        or f"{transaction_id}:{kind.value}:{fence}:{sequence}",
        issued_at=issued,
    )


class GatewayFixture:
    """Mixin giving each KGW test one gateway over one mock deployment."""

    def build(
        self,
        *,
        faults: Optional[FaultInjection] = None,
        journal: Any = None,
        clock: Optional[TestClock] = None,
        config: Optional[Mapping[str, Any]] = None,
        adapters: Optional[Mapping[str, Any]] = None,
    ):
        self.clock = clock or TestClock()
        self.adapter = MockActuationAdapter(
            config=dict(config if config is not None else BASELINE), faults=faults
        )
        self.journal = journal if journal is not None else InMemoryTransactionJournal()
        self.gateway = TokenBoundWriteGateway(
            adapters=dict(adapters) if adapters is not None else {"mock": self.adapter},
            safe_state=SAFE_STATE,
            journal=self.journal,
            clock=self.clock,
        )
        return self.gateway, self.adapter

    # -- protocol steps ----------------------------------------------------

    def do_prepare(self, plan: Optional[Mapping[str, Any]] = None, **kwargs):
        return self.gateway.prepare(
            token=token(TokenKind.PREPARE, sequence=0, **kwargs),
            plan=dict(plan if plan is not None else PLAN),
        )

    def do_ready(self, **kwargs):
        return self.gateway.ready(token=token(TokenKind.READY, sequence=1, **kwargs))

    def do_commit(self, **kwargs):
        return self.gateway.commit(token=token(TokenKind.COMMIT, sequence=2, **kwargs))

    def do_finalize(self, *, expected: Optional[str] = None, sequence: int = 3, **kwargs):
        return self.gateway.finalize_live(
            token=token(
                TokenKind.FINALIZE_LIVE,
                sequence=sequence,
                expected=expected if expected is not None else APPLIED_HASH,
                **kwargs,
            )
        )

    def do_stop(self, *, sequence: int = 3, **kwargs):
        return self.gateway.stop(token=token(TokenKind.STOP, sequence=sequence, **kwargs))

    def do_rollback(self, *, expected: Optional[str] = None, sequence: int = 4, **kwargs):
        return self.gateway.reverse_rollback(
            token=token(
                TokenKind.REVERSE_ROLLBACK,
                sequence=sequence,
                expected=expected if expected is not None else APPLIED_HASH,
                **kwargs,
            )
        )

    def do_reread(self, *, sequence: int = 5, expected: Optional[str] = None, **kwargs):
        return self.gateway.reread_configuration(
            token=token(
                TokenKind.CONFIGURATION_REREAD, sequence=sequence, expected=expected, **kwargs
            )
        )

    def do_confirm(self, *, sequence: int = 6, expected: Optional[str] = None, **kwargs):
        return self.gateway.confirm_recovery(
            token=token(
                TokenKind.RECOVERY_CONFIRM, sequence=sequence, expected=expected, **kwargs
            )
        )

    def do_emergency(self, *, sequence: int = 7, **kwargs):
        return self.gateway.emergency_safe_state(
            token=token(TokenKind.EMERGENCY_SAFE_STATE, sequence=sequence, **kwargs)
        )

    def commit_applied(self, **kwargs) -> Dict[str, Any]:
        """Run prepare, ready and commit; return the three results."""
        return {
            "prepare": self.do_prepare(**kwargs),
            "ready": self.do_ready(**kwargs),
            "commit": self.do_commit(**kwargs),
        }
