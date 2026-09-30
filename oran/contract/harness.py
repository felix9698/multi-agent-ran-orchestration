"""Development-only common harness control-plane types.

These dataclasses describe the loopback-only, insecure-dev-flag-gated management
surface.  They are not O-RAN interfaces and production profiles must not enable
them; component servers own routing and durable restart behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

HARNESS_PATHS = {
    "op": "/harness/op", "fault": "/harness/fault", "restart": "/harness/restart", "state": "/harness/state",
}
HARNESS_OPERATION_OWNERS = {
    "rApp": frozenset({"COORDINATOR_TRANSITION"}),
    "Non-RT RIC Framework": frozenset({"PROCESS_RESTART:NON_RT_RIC_FRAMEWORK"}),
    "Near-RT mock": frozenset({
        "A1_SEED_RESOURCE", "A1_EMIT_STATUS", "KPM_SNAPSHOT", "E2_CONTROL_RESULT", "READBACK",
        "SET_DEPENDENCY", "DECISION_RESULT", "E2_STUB_LOG", "PROCESS_RESTART:NEAR_RT_RIC_A1P_PRODUCER",
        "PROCESS_RESTART:NEAR_RT_RIC_XAPP",
    }),
    "O1 stack": frozenset({"PROCESS_RESTART:O1_CONSUMER", "PROCESS_RESTART:O1_PROVIDER"}),
    # The contract assigns no harness operation to the conformance runner.
    "conformance runner": frozenset(),
}
HARNESS_FAULT_OWNERS = {
    "Non-RT RIC Framework": frozenset({"CRASH_PROCESS", "DROP_HTTP_RESPONSE", "DROP_CALLBACK_DELIVERY"}),
    "Near-RT mock": frozenset({"DROP_CALLBACK_DELIVERY"}),
    "O1 stack": frozenset({"DROP_O1_NOTIFICATION", "FLIP_RETRIEVED_BYTE", "TLS_HANDSHAKE_REJECT"}),
}


@dataclass(frozen=True)
class HarnessOperationRequest:
    op: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"op": self.op, **dict(self.arguments)}


@dataclass(frozen=True)
class HarnessOperationResponse:
    outputs: Mapping[str, Any]

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> "HarnessOperationResponse":
        return cls(outputs=body["outputs"])


@dataclass(frozen=True)
class HarnessFaultRequest:
    fault: str
    boundary: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"fault": self.fault, "boundary": dict(self.boundary)}


@dataclass(frozen=True)
class HarnessFaultResponse:
    outputs: Mapping[str, Any]


@dataclass(frozen=True)
class HarnessRestartRequest:
    component: str

    def to_json(self) -> dict[str, str]:
        return {"component": self.component}


@dataclass(frozen=True)
class HarnessRestartResponse:
    outputs: Mapping[str, Any]


@dataclass(frozen=True)
class HarnessStateResponse:
    state: Mapping[str, Any]


def harness_enabled(*, host: str, insecure_dev_flag: bool, production: bool) -> bool:
    """Return true only for an explicitly enabled loopback development server."""
    return insecure_dev_flag and not production and host in {"127.0.0.1", "::1", "localhost"}


INTEGRATION_CONTROL_SURFACE_APPROVAL_ENV = "UBM_INTEGRATION_CONTROL_APPROVED"


def integration_control_surface_enabled(
        *, host: str, approved_flag: bool, approved_env: bool, production: bool) -> bool:
    """Two-key, loopback-only approval for the contract-declared harness operations.

    TLS is mandatory on the bilateral path, so ``insecure_dev`` is necessarily
    false there and cannot be the predicate.  The two keys are independent: a
    CLI flag without the environment variable, or the environment variable
    without the flag, leaves the surface absent.  Code never self-approves.
    """
    return harness_enabled(host=host,
                           insecure_dev_flag=(approved_flag and approved_env),
                           production=production)
