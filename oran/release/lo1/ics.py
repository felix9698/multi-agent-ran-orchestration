"""Integration Control Surface for the live-O1 profile.

The *operations* are contract-declared: ``role-table.1.0.0.json`` resolves every
SC-084 initial state to a party and names its adapter action, so the allowlist is
READ from those bytes rather than written here.  Three consequences follow
directly and none of them is a policy choice this module makes:

* the six ``REQUIRED_SEED_OP`` rows are the operations the upper exposes;
* the three ``REQUIRED_READINESS_ASSIGNED`` rows -- the NETCONF / PerfMetricJob
  module SC-084 declares as mandatory initial states -- answer
  ``ICS_OP_NOT_ASSIGNED`` and abort non-zero unless the authority record
  assigned them to the upper **by name**.  The gate refuses an empty or partial
  assignment outright, so this refusal now guards a composition fault rather
  than a supported configuration;
* the one ``NOT_UPPER`` row is not exposed at all: the live PM source belongs to
  the Provider and the upper must never synthesise it.

``COORDINATOR_TRANSITION`` is denied by name.  A synthetic transition is never
accepted as evidence that the Coordinator executed (LO1-ST-N01); the only way to
move the FSM is the real ``COORDINATOR_PROCESS_INTENT`` path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .o1_readiness import ReadinessRefused

ROLE_TABLE_FILE_NAME = "role-table.1.0.0.json"

COMPONENTS = ("nonrt", "rapp", "o1_consumer", "e2_stub")
OP_PATH = "/harness/op"
STATE_PATH = "/harness/state"
NOT_FOUND_BODY = {"code": "AIC_RESOURCE_NOT_FOUND"}
NOT_ASSIGNED_BODY = {"code": "AIC_OPERATION_NOT_ASSIGNED"}

#: Operations denied by name, kept so a denial is explicit in the audit record.
DENIED_OPERATIONS = frozenset({
    "COORDINATOR_TRANSITION", "CONFIGURE_LIVE_PM_PROFILE",
    "CONFIGURE_NEXT_POLICY_ID", "PROCESS_RESTART",
})

#: Operations the upper adds beyond the role table's seed rows, all of which are
#: harness control rather than scenario material.
CONTROL_OPERATIONS = frozenset({
    "RESET_SCENARIO_STATE", "R1_DME_BINDING_PRECREATE", "R1_DME_BINDING_COMMIT",
    "COORDINATOR_PROCESS_INTENT",
    # These are the five non-HTTP SC-084 steps which the frozen runner contract
    # declares as HARNESS_INPUT / HARNESS_OBSERVATION / PURE_COMPUTATION.  The
    # control surface is only their invocation transport; the production
    # boundaries below still perform and record the real work.
    "KPM_SNAPSHOT", "E2_CONTROL_RESULT", "READBACK",
    "O1_RETRIEVE", "O1_NORMALIZE",
})


class IntegrationControlSurfaceError(RuntimeError):
    """The control surface was constructed or driven outside its declaration."""


class NonMeasuredRefusalBody(dict):
    """Wire-compatible problem body marked as outside measured capture."""

    suppress_capture = True


class OperationNotAssigned(RuntimeError):
    """A mandatory readiness adapter action was used without an assignment."""

    exit_code = 78


def _spec_search_roots() -> tuple[Path, ...]:
    here = Path(__file__).resolve()
    return (
        here.parents[3] / "docs" / "upper-live-o1-harness",
        here.parents[4] / "spec",
        here.parent / "spec",
    )


def role_table_path(explicit: Path | None = None) -> Path:
    if explicit is not None and Path(explicit).is_file():
        return Path(explicit)
    for root in _spec_search_roots():
        candidate = root / ROLE_TABLE_FILE_NAME
        if candidate.is_file():
            return candidate
    raise IntegrationControlSurfaceError(
        "the frozen %s was not found" % ROLE_TABLE_FILE_NAME)


def load_role_table(path: Path | None = None) -> dict[str, Any]:
    return json.loads(role_table_path(path).read_text(encoding="utf-8"))


def seed_operations(role_table: Mapping[str, Any]) -> tuple[str, ...]:
    """The adapter actions the role table marks REQUIRED_SEED_OP."""
    return tuple(sorted({
        str(entry["adapterAction"]) for entry in role_table.get("initialStates", [])
        if str(entry.get("upperObligation")) == "REQUIRED_SEED_OP"}))


def readiness_operations(role_table: Mapping[str, Any]) -> tuple[str, ...]:
    """The adapter actions the upper must be assigned to establish readiness."""
    return tuple(sorted({
        str(entry["adapterAction"]) for entry in role_table.get("initialStates", [])
        if str(entry.get("upperObligation")) == "REQUIRED_READINESS_ASSIGNED"}))


def not_upper_operations(role_table: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(sorted({
        str(entry["adapterAction"]) for entry in role_table.get("initialStates", [])
        if str(entry.get("upperObligation")) == "NOT_UPPER"}))


class IntegrationControlSurface:
    """Allowlist, audit and dispatch for one component's harness transport."""

    def __init__(self, *, component: str,
                 delegate: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
                 recorder: Any, assigned_adapter_actions: frozenset[str],
                 role_table: Mapping[str, Any] | None = None) -> None:
        if component not in COMPONENTS:
            raise IntegrationControlSurfaceError("unknown component: %s" % component)
        table = dict(role_table or load_role_table())
        self.component = component
        self.delegate = delegate
        self.recorder = recorder
        self.assigned = frozenset(str(item) for item in assigned_adapter_actions)
        self.seed_ops = frozenset(seed_operations(table))
        self.assignable_ops = frozenset(readiness_operations(table))
        self.not_upper_ops = frozenset(not_upper_operations(table))
        unknown = self.assigned - self.assignable_ops
        if unknown:
            raise IntegrationControlSurfaceError(
                "the authority assigned %s, which the role table does not mark "
                "assignable" % sorted(unknown)[0])
        self.allowed = (self.seed_ops | CONTROL_OPERATIONS | self.assigned) \
            - DENIED_OPERATIONS - self.not_upper_ops

    # -- audit ------------------------------------------------------------
    def _audit(self, *, op: str, allowed: bool, argument_keys: list[str],
               status: int, event: str,
               output_keys: list[str] | None = None) -> None:
        record: dict[str, Any] = {
            "sequence": self.recorder.next_sequence(),
            "observedAt": self.recorder.clock.now(),
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
    def handle(self, method: str, path: str,
               body: Mapping[str, Any] | None) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == STATE_PATH:
            self._audit(op="STATE", allowed=True, argument_keys=[], status=200,
                        event="ICS_STATE_READ")
            return 200, {"component": self.component,
                         "state": dict(self.delegate("__STATE__", {}))}
        if method != "POST" or path != OP_PATH:
            self._audit(op=_op_token(path), allowed=False, argument_keys=[],
                        status=404, event="ICS_OP_NOT_ALLOWED")
            return 404, dict(NOT_FOUND_BODY)
        arguments = dict(body or {})
        op = str(arguments.pop("op", "")) or "UNSPECIFIED"
        if op in self.assignable_ops and op not in self.assigned:
            # Fail closed, and say exactly why: SC-084 declares this row as a
            # mandatory initial state and only a named authority assignment
            # binds it to the upper.  G-ID-07 refuses an unassigned or partly
            # assigned run, so an unassigned action reaching here is a
            # composition fault and is still refused rather than performed.
            self._audit(op=op, allowed=False, argument_keys=list(arguments),
                        status=409, event="ICS_OP_NOT_ASSIGNED")
            return 409, dict(NOT_ASSIGNED_BODY)
        if op not in self.allowed:
            self._audit(op=op, allowed=False, argument_keys=list(arguments),
                        status=404, event="ICS_OP_NOT_ALLOWED")
            return 404, dict(NOT_FOUND_BODY)
        try:
            outputs = dict(self.delegate(op, arguments))
        except ReadinessRefused as exc:
            # The delegate was refused before the measured body.  Appending an
            # ICS row here would consume a measured sequence for an operation
            # whose defining invariant is zero side effects and zero capture.
            return 409, NonMeasuredRefusalBody({
                "code": "AIC_%s" % exc.reason_code,
                "reasonCode": exc.reason_code})
        self._audit(op=op, allowed=True, argument_keys=list(arguments),
                    status=200, event="ICS_OP", output_keys=list(outputs))
        return 200, {"op": op, "outputs": outputs}


def _op_token(path: str) -> str:
    token = "".join(
        character if character.isalnum() or character in {"_", ":"} else "_"
        for character in str(path).upper())
    return token or "UNSPECIFIED"
