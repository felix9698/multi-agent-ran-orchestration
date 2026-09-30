"""The Write Gateway (design section 4.4).

Owner lane: **KGW** for :mod:`assurance.gateway.write_gateway`;
:mod:`assurance.gateway.token` is complete and has no owner.

The only component that performs dynamic equipment changes, and only through
the official O-RAN path of design section 9.  It acts on a
:class:`~assurance.gateway.token.KernelToken` and on nothing else -- a token
being a deterministic safety permit, never a signature or an approval.

The adapters are deliberately *not* re-exported.  A component that can import
an adapter can construct one and call it; registration through the gateway's
private registry is the only path onto the equipment, and the KGW boundary test
asserts that nothing outside ``assurance/gateway/`` imports one.
"""

from __future__ import annotations

from assurance.gateway.token import FORBIDDEN_TOKEN_FIELDS, KernelToken, TokenKind
from assurance.gateway.write_gateway import (
    AdapterRegistry,
    GatewayOutcome,
    GatewayRefusal,
    GatewayResult,
    WriteGateway,
    WriteGatewayAdapter,
)
from assurance.gateway.commands import (
    SIDE_EFFECT_FREE_OPERATIONS,
    GatewayOperation,
    build_command,
)
from assurance.gateway.journal import (
    UNCERTAIN_PHASES,
    InMemoryTransactionJournal,
    JsonFileTransactionJournal,
    NextSafeAction,
    TransactionJournal,
    TransactionPhase,
    TransactionRecord,
)
from assurance.gateway.plan import ActuationPlan, PlanError, PlanStep, config_hash
from assurance.gateway.registry import GatewayAdapterRegistry
from assurance.gateway.gateway import TokenBoundWriteGateway

__all__ = [
    "FORBIDDEN_TOKEN_FIELDS",
    "SIDE_EFFECT_FREE_OPERATIONS",
    "UNCERTAIN_PHASES",
    "ActuationPlan",
    "AdapterRegistry",
    "GatewayAdapterRegistry",
    "GatewayOperation",
    "GatewayOutcome",
    "GatewayRefusal",
    "GatewayResult",
    "InMemoryTransactionJournal",
    "JsonFileTransactionJournal",
    "KernelToken",
    "NextSafeAction",
    "PlanError",
    "PlanStep",
    "TokenBoundWriteGateway",
    "TokenKind",
    "TransactionJournal",
    "TransactionPhase",
    "TransactionRecord",
    "WriteGateway",
    "WriteGatewayAdapter",
    "build_command",
    "config_hash",
]
