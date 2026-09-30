"""Integration Control Surface (D-4).

The *operations* are contract-declared: ``scenario-runner-contract.1.0.1.json``
classifies them (``HARNESS_CONTROL``, ``HARNESS_INPUT``, ``HARNESS_OBSERVATION``,
``HARNESS_SETUP``) and names the adapter actions of every ``initialState``.  The
only upper invention is the *transport* (``oran/contract/harness.py`` paths).

Exposure is minimised: the allowlist is frozen in
``integration-control-surface.1.0.0.json`` and read from those bytes, every call
is audited into the capture document with **argument key names only**, and any
non-allowlisted operation returns 404 without reaching a component.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from oran.contract.harness import (
    INTEGRATION_CONTROL_SURFACE_APPROVAL_ENV,
    integration_control_surface_enabled,
)

SPEC_FILE_NAME = "integration-control-surface.1.0.0.json"
COMPONENTS = ("nonrt", "rapp", "o1_provider", "o1_consumer")
OP_PATH = "/harness/op"
STATE_PATH = "/harness/state"
NOT_FOUND_BODY = {"code": "AIC_RESOURCE_NOT_FOUND"}

__all__ = [
    "ALLOWED_OPERATIONS",
    "IntegrationControlSurface",
    "IntegrationControlSurfaceError",
    "INTEGRATION_CONTROL_SURFACE_APPROVAL_ENV",
    "load_allowed_operations",
    "spec_path",
]


class IntegrationControlSurfaceError(RuntimeError):
    """The control surface was constructed without a valid approval."""


def _spec_search_roots() -> tuple[Path, ...]:
    here = Path(__file__).resolve()
    return (
        here.parents[3] / "docs" / "upper-bilateral-mock",
        here.parents[4] / "spec",
        here.parent / "spec",
    )


def spec_path() -> Path:
    for root in _spec_search_roots():
        candidate = root / SPEC_FILE_NAME
        if candidate.is_file():
            return candidate
    raise IntegrationControlSurfaceError(
        "the frozen %s specification was not found" % SPEC_FILE_NAME)


def load_allowed_operations(path: Path | None = None
                            ) -> dict[str, frozenset[str]]:
    """Read the allowlist from the frozen spec bytes, never from code."""
    document = json.loads(
        Path(path or spec_path()).read_text(encoding="utf-8"))
    declared = document["allowedOperations"]
    allowed = {component: frozenset(str(op) for op in declared[component])
               for component in COMPONENTS}
    return allowed


ALLOWED_OPERATIONS: Mapping[str, frozenset[str]] = load_allowed_operations()

#: Operations the spec denies by name, kept so a denial is explicit in audit.
DENIED_OPERATIONS = frozenset({
    "CONFIGURE_NEXT_POLICY_ID", "COORDINATOR_TRANSITION",
    "INSTALL_NON_RT_DESIRED_POLICY", "INSTALL_PINNED_SERVICE_DESCRIPTIONS",
    "START_SERVICE_REGISTRATION_API", "PROCESS_RESTART",
})


class IntegrationControlSurface:
    """Allowlist, audit and dispatch for one component's harness transport."""

    def __init__(self, *, component: str,
                 delegate: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
                 recorder: Any,
                 approved: bool, host: str) -> None:
        if component not in COMPONENTS:
            raise IntegrationControlSurfaceError("unknown component: %s" % component)
        if not integration_control_surface_enabled(
                host=host, approved_flag=bool(approved), approved_env=bool(approved),
                production=False):
            raise IntegrationControlSurfaceError(
                "the integration control surface is loopback-only and needs the "
                "CLI flag together with %s=1"
                % INTEGRATION_CONTROL_SURFACE_APPROVAL_ENV)
        self.component = component
        self.delegate = delegate
        self.recorder = recorder
        self.host = host
        self.allowed = ALLOWED_OPERATIONS[component]

    # -- audit ------------------------------------------------------------
    def _audit(self, *, op: str, allowed: bool, argument_keys: list[str],
               status: int, event: str,
               output_keys: list[str] | None = None) -> None:
        record: dict[str, Any] = {
            "sequence": self.recorder.next_sequence(),
            "logicalTimeMs": self.recorder.clock.now_ms(),
            "component": self.component,
            "op": op,
            "allowed": bool(allowed),
            "argumentKeys": sorted(set(argument_keys)),
            "httpStatus": int(status),
            "event": event,
        }
        if output_keys is not None:
            record["outputKeys"] = sorted(set(output_keys))
        self.recorder.emit_harness_operation(record)

    # -- gate -------------------------------------------------------------
    def _authorize(self, method: str, path: str, body: Mapping[str, Any] | None
                   ) -> tuple[bool, int, dict[str, Any]]:
        """Allowlist check plus audit, evaluated before the delegate runs."""
        if method == "GET" and path == STATE_PATH:
            self._audit(op="STATE", allowed=True, argument_keys=[],
                        status=200, event="ICS_STATE_READ")
            return True, 200, {}
        if method != "POST" or path != OP_PATH:
            op = _op_token(path)
            self._audit(op=op, allowed=False, argument_keys=[], status=404,
                        event="ICS_OP_NOT_ALLOWED")
            return False, 404, dict(NOT_FOUND_BODY)
        arguments = dict(body or {})
        op = str(arguments.pop("op", "")) or "UNSPECIFIED"
        if op not in self.allowed:
            self._audit(op=op, allowed=False,
                        argument_keys=list(arguments), status=404,
                        event="ICS_OP_NOT_ALLOWED")
            return False, 404, dict(NOT_FOUND_BODY)
        self._audit(op=op, allowed=True, argument_keys=list(arguments),
                    status=200, event="ICS_OP")
        return True, 200, {}

    def handle(self, method: str, path: str,
               body: Mapping[str, Any] | None) -> tuple[int, dict[str, Any]]:
        permitted, status, payload = self._authorize(method, path, body)
        if not permitted:
            return status, payload
        # The envelope is declared in
        # integration-control-surface.1.0.0.json#/transport/responseEnvelope:
        # `outputs`/`state` are always present so a counterpart can tell an
        # empty result from a missing one instead of guessing.
        if method == "GET":
            return 200, {"component": self.component,
                         "state": dict(self.delegate("__STATE__", {}))}
        arguments = dict(body or {})
        op = str(arguments.pop("op", ""))
        outputs = self.delegate(op, arguments)
        return 200, {"op": op, "outputs": dict(outputs)}


def _op_token(path: str) -> str:
    token = "".join(
        character if character.isalnum() or character in {"_", ":"} else "_"
        for character in str(path).upper())
    return token or "UNSPECIFIED"
