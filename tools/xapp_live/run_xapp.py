"""Execute ONE xApp assignment live on the gNB, then roll it back.

This is what a Phase-3 campaign calls per specialist xApp while it samples
throughput: apply one Action to the real radio, hold it while the sampler runs,
put the radio back exactly as it was.  It is deliberately one assignment and
one rollback -- no loop, no retry, no verdict.  Whether the KPI moved is the
Kernel's question and the evaluator's; this entry answers "was the change
applied, was it read back, and is the deployment where it started".

The order is the whole of it:

1. resolve the specialist xApp's manifest from the registry, and refuse an xApp
   outside the coordinated live set unless the caller says so out loud;
2. take the common KPI snapshot from the injected provider.  A snapshot is
   assembled from Measurement Collector ``RawSample`` objects and nothing else
   (``assurance/xapps/snapshot.py``), so this entry cannot invent the
   attribution a UE-scoped action is checked against;
3. obtain a ``COMMIT`` permit from the injected permit source.  Only the
   Assurance Kernel issues one; a source that hands back anything but a
   :class:`~assurance.gateway.token.KernelToken` is refused and nothing is
   sent;
4. build the specialist executor over the live telnet transport and execute
   the assignment inside the permit scope -- snapshot, apply, read back;
5. run the caller's dwell (the throughput sampler) while the change is live;
6. **always** roll back: any acknowledged write, and any write whose outcome is
   unknown, is followed by a ``REVERSE_ROLLBACK`` permit and the executor's own
   deterministic restore, in a ``finally``.

What this path is not: it is not the official
``R1 -> A1-P -> xApp -> FlexRIC -> E2`` chain.  ``ci rfatt`` / ``ci mcs`` /
``ci prbcap`` / ``ci sched_prio`` are direct gNB controls -- ``LAB_SETUP``
by design section 9 -- so an effect measured here is a research result and
never an objective effect or OTA evidence for an objective family.  Steering
keeps its production path (``tools.g3ota.run_ota``); this entry refuses it.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from assurance.actions import action_catalog
from assurance.contracts.capability import DeploymentBinding, TransportSecurity
from assurance.core.timebase import format_utc, parse_utc
from assurance.gateway.token import KernelToken, TokenKind
from assurance.xapps import (
    CommonKpiSnapshot, XAppExecutionAssignment, XAppExecutionReport,
    XAppExecutionStatus, build_default_xapp_coordination,
)
from assurance.xapps.executors import PermitRequiredError
from assurance.xapps.registry import XAppRegistryError
from assurance.xapps.live_actuation import (
    LiveActuationError, TelnetActuationTransport, build_specialist_executor,
)

from tools.xapp_live.transport import build_transport

__all__ = [
    "KernelPermitSource",
    "LiveRunError",
    "LiveXAppOutcome",
    "LiveXAppRequest",
    "main",
    "run_one_assignment",
]


class LiveRunError(RuntimeError):
    """The live run refuses to start, or cannot be completed honestly."""


# --------------------------------------------------------------------------- #
# permits: Kernel-issued, or none
# --------------------------------------------------------------------------- #

class KernelPermitSource:
    """Permits for one open trial, taken from the Assurance Kernel.

    The only permit source this entry ships.  It holds a Kernel and a trial id
    and calls :meth:`~assurance.kernel.kernel.AssuranceKernel.issue_token`,
    which is where a permit's fence, lease, expected configuration hash and
    idempotency key come from and where the issue is recorded as an event.
    There is no fallback that builds a token locally: a token this process
    made up would carry a fence nothing in the Kernel's ledger ever saw.
    """

    def __init__(self, *, kernel: Any, trial_id: str) -> None:
        if not hasattr(kernel, "issue_token"):
            raise LiveRunError(
                "a permit source needs the Assurance Kernel; nothing else "
                "issues a Write Gateway permit")
        self._kernel = kernel
        self._trial_id = trial_id

    def acquire(self, kind: TokenKind, *, now: str) -> KernelToken:
        token = self._kernel.issue_token(self._trial_id, token_kind=kind,
                                         now=now)
        if not isinstance(token, KernelToken):
            raise LiveRunError(
                f"the permit source returned {type(token).__name__}, not a "
                "Kernel token; no equipment write is authorised")
        return token


# --------------------------------------------------------------------------- #
# request and outcome
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LiveXAppRequest:
    """One live assignment: which xApp, which Action, where, with what."""

    xapp_id: str
    action_id: str
    parameters: Mapping[str, Any]
    target_selector: Mapping[str, Any]
    cell_id: str
    endpoint: str = ""
    deadline_ms: int = 60_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", dict(self.parameters))
        object.__setattr__(self, "target_selector", dict(self.target_selector))
        for name in ("xapp_id", "action_id", "cell_id"):
            if not str(getattr(self, name)).strip():
                raise LiveRunError(f"{name} must be a non-empty string")
        if self.deadline_ms <= 0:
            raise LiveRunError("deadline_ms must be positive")

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "xappId": self.xapp_id,
            "actionId": self.action_id,
            "parameters": dict(self.parameters),
            "targetSelector": dict(self.target_selector),
            "cellId": self.cell_id,
            "endpoint": self.endpoint,
            "deadlineMs": self.deadline_ms,
        }


@dataclass(frozen=True)
class LiveXAppOutcome:
    """What one live assignment did, including what it could not do."""

    request: LiveXAppRequest
    assignment_id: str
    execution: Optional[XAppExecutionReport] = None
    rollback: Optional[XAppExecutionReport] = None
    refusal: str = ""
    rollback_refusal: str = ""
    rollback_skipped_reason: str = ""
    baseline: Mapping[str, Any] = field(default_factory=dict)
    applied_axes: Tuple[str, ...] = ()
    unknown_writes: Tuple[str, ...] = ()
    exchanges: Tuple[Mapping[str, Any], ...] = ()
    dwell: Any = None
    live_selection: str = ""
    coordinated_live_set: bool = True

    @property
    def applied(self) -> bool:
        return self.execution is not None \
            and self.execution.status is XAppExecutionStatus.SUCCEEDED

    @property
    def restored(self) -> bool:
        """True when the deployment is back where the run found it."""
        if self.unknown_writes:
            return self.rollback is not None \
                and self.rollback.status is XAppExecutionStatus.ROLLBACK_SUCCEEDED
        if not self.applied_axes:
            return True
        return self.rollback is not None \
            and self.rollback.status is XAppExecutionStatus.ROLLBACK_SUCCEEDED

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "request": self.request.to_canonical_dict(),
            "assignmentId": self.assignment_id,
            "execution": None if self.execution is None
            else self.execution.to_canonical_dict(),
            "rollback": None if self.rollback is None
            else self.rollback.to_canonical_dict(),
            "refusal": self.refusal,
            "rollbackRefusal": self.rollback_refusal,
            "rollbackSkippedReason": self.rollback_skipped_reason,
            "baseline": dict(self.baseline),
            "appliedAxes": list(self.applied_axes),
            "unknownWrites": list(self.unknown_writes),
            "exchanges": [dict(exchange) for exchange in self.exchanges],
            "dwell": self.dwell,
            "liveSelection": self.live_selection,
            "coordinatedLiveSet": self.coordinated_live_set,
            "applied": self.applied,
            "restored": self.restored,
            "actuatorPath": TelnetActuationTransport.actuator_path.value,
            "evidenceGrade": "LAB_TELNET_RESEARCH_MEASUREMENT",
        }


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #

def run_one_assignment(
    request: LiveXAppRequest,
    *,
    send: Callable[[str], str],
    permit_source: Any,
    snapshot_provider: Callable[[], CommonKpiSnapshot],
    deployment: DeploymentBinding,
    clock: Callable[[], str],
    while_applied: Optional[Callable[[XAppExecutionReport], Any]] = None,
    outside_coordinated_live_set: bool = False,
) -> LiveXAppOutcome:
    """Apply one Action live under a Kernel permit, then always roll it back."""
    if permit_source is None or not hasattr(permit_source, "acquire"):
        raise LiveRunError(
            "no permit source: an equipment write happens only under a "
            "Kernel-issued Write Gateway permit, so this entry refuses to run")
    runtime = build_default_xapp_coordination(deployment)
    try:
        manifest = runtime.registry.manifest_for(request.xapp_id)
    except XAppRegistryError as exc:
        raise LiveRunError(str(exc)) from exc
    if not manifest.owns_action(request.action_id):
        raise LiveRunError(
            f"{request.xapp_id} does not own {request.action_id!r}; its closed "
            f"ownership set is {sorted(manifest.owned_action_ids)}")

    in_live_set = runtime.registry.in_coordinated_live_set(request.xapp_id)
    if not in_live_set and not outside_coordinated_live_set:
        raise LiveRunError(
            f"{request.xapp_id} ({manifest.kind.value}) is outside the "
            "coordinated live set recorded in assurance/xapps/registry.py; "
            "its manifest states why. Pass outside_coordinated_live_set=True "
            "to run it anyway, so the deviation is in the call and not in a "
            "default")

    now = clock()
    snapshot = snapshot_provider()
    if not isinstance(snapshot, CommonKpiSnapshot):
        raise LiveRunError(
            "the snapshot provider must return a CommonKpiSnapshot assembled "
            "from Measurement Collector samples; this entry does not build one")
    selection = runtime.registry.live_selection(request.xapp_id, now=now)

    commit_permit = permit_source.acquire(TokenKind.COMMIT, now=now)
    if not isinstance(commit_permit, KernelToken):
        raise LiveRunError(
            "the permit source did not return a Kernel token; nothing is sent")

    assignment = XAppExecutionAssignment(
        assignment_id=f"assignment/live/{request.xapp_id.split('/')[-1]}/"
                      f"{request.action_id}",
        plan_id=f"xapp-plan/live/{request.action_id}",
        step_id=f"step/live/{request.action_id}",
        xapp_id=request.xapp_id,
        action_id=request.action_id,
        parameters=request.parameters,
        target_selector=request.target_selector,
        snapshot_id=snapshot.snapshot_id,
        snapshot_hash=snapshot.content_hash(),
        preconditions=(),
        deadline=format_utc(parse_utc(now)
                            + timedelta(milliseconds=request.deadline_ms)),
        permit_ref=commit_permit.content_hash(),
    )
    transport = TelnetActuationTransport(
        send=send, deployment=deployment, cell_id=request.cell_id,
        endpoint=request.endpoint, clock=clock)
    executor = build_specialist_executor(manifest=manifest, backend=transport)

    report: Optional[XAppExecutionReport] = None
    rollback_report: Optional[XAppExecutionReport] = None
    refusal = ""
    rollback_refusal = ""
    skipped = ""
    dwell: Any = None
    try:
        with transport.permit_scope(assignment, permit=commit_permit,
                                    kind=TokenKind.COMMIT, now=now):
            report = executor.execute(assignment, permit=commit_permit,
                                      snapshot=snapshot, now=now)
        if report.status is XAppExecutionStatus.SUCCEEDED \
                and while_applied is not None:
            dwell = while_applied(report)
    except (LiveActuationError, PermitRequiredError) as exc:
        refusal = f"{type(exc).__name__}: {exc}"
    finally:
        if transport.applied_axes or transport.unknown_writes:
            try:
                rollback_now = clock()
                rollback_permit = permit_source.acquire(
                    TokenKind.REVERSE_ROLLBACK, now=rollback_now)
                with transport.permit_scope(
                        assignment, permit=rollback_permit,
                        kind=TokenKind.REVERSE_ROLLBACK, now=rollback_now):
                    rollback_report = executor.rollback(
                        assignment, permit=rollback_permit, now=rollback_now)
            except Exception as exc:  # the rollback's own failure is evidence
                rollback_refusal = f"{type(exc).__name__}: {exc}"
        else:
            skipped = ("no write was acknowledged and none is unknown; there "
                       "is nothing to restore")

    return LiveXAppOutcome(
        request=request,
        assignment_id=assignment.assignment_id,
        execution=report,
        rollback=rollback_report,
        refusal=refusal,
        rollback_refusal=rollback_refusal,
        rollback_skipped_reason=skipped,
        baseline=transport.observed_baseline,
        applied_axes=transport.applied_axes,
        unknown_writes=transport.unknown_writes,
        exchanges=tuple(
            {"axis": exchange.axis, "operation": exchange.operation,
             "command": exchange.command, "response": exchange.response,
             "outcome": exchange.outcome, "at": exchange.at}
            for exchange in transport.exchanges),
        dwell=dwell,
        live_selection=selection.state.value,
        coordinated_live_set=in_live_set,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def wall_clock() -> str:
    """The canonical UTC instant, read from the wall clock (a tool may)."""
    return format_utc(datetime.now(timezone.utc))


def lab_deployment(base_url: str) -> DeploymentBinding:
    """The deployment description a lab telnet run acts under.

    ``TransportSecurity.NONE`` is the truth about a loopback telnet shell and
    is recorded rather than dressed up; the catalog reads parameter ranges out
    of this binding and nothing else about it reaches the wire.
    """
    return DeploymentBinding(
        contract_id="deployment/xapp-live-telnet", version="1.0.0",
        schema_version="1.0.0", document_status="NORMATIVE",
        standard_mapping={"labSetup": "direct-gnb-telnet"},
        endpoint_id="oai-gnb-telnet", base_url=base_url,
        transport_security=TransportSecurity.NONE,
        secret_refs={})


def _load(reference: str) -> Any:
    """``package.module:attribute`` -> the attribute, called if callable."""
    module_name, _, attribute = reference.partition(":")
    if not module_name or not attribute:
        raise LiveRunError(
            f"{reference!r} is not 'package.module:attribute'")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise LiveRunError(f"cannot import {module_name!r}: {exc}") from exc
    try:
        found = getattr(module, attribute)
    except AttributeError as exc:
        raise LiveRunError(
            f"{module_name!r} has no {attribute!r}") from exc
    return found() if callable(found) else found


def _typed_parameters(action_id: str, pairs: Sequence[str],
                      deployment: DeploymentBinding) -> Dict[str, Any]:
    """``name=value`` strings typed by the frozen catalog contract."""
    contract = next((item for item in action_catalog(deployment)
                     if item.action_id == action_id), None)
    if contract is None:
        raise LiveRunError(f"{action_id!r} is not in the action catalog")
    types = {p.name: p.value_type for p in contract.binding.parameters}
    values: Dict[str, Any] = {}
    for pair in pairs:
        name, separator, text = pair.partition("=")
        if not separator:
            raise LiveRunError(f"--param {pair!r} is not name=value")
        value_type = types.get(name)
        if value_type is None:
            raise LiveRunError(
                f"{action_id}: no parameter {name!r}; the contract declares "
                f"{sorted(types)}")
        try:
            if value_type in {"integer", "hex-integer"}:
                values[name] = int(text, 0)
            elif value_type == "number":
                values[name] = float(text)
            else:
                values[name] = text
        except ValueError as exc:
            raise LiveRunError(
                f"{action_id}.{name}: {text!r} is not a {value_type}") from exc
    return values


def _pairs(items: Sequence[str]) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for item in items:
        name, separator, text = item.partition("=")
        if not separator:
            raise LiveRunError(f"--selector {item!r} is not name=value")
        parsed[name] = text
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--xapp-id", required=True,
                        help="e.g. xapp/ue-scheduler")
    parser.add_argument("--action-id", required=True,
                        help="e.g. scheduler-priority")
    parser.add_argument("--param", action="append", default=[],
                        metavar="NAME=VALUE",
                        help="action parameter, typed from the catalog")
    parser.add_argument("--selector", action="append", default=[],
                        metavar="NAME=VALUE",
                        help="assignment target selector entry")
    parser.add_argument("--cell-id", required=True,
                        help="the cell this telnet endpoint serves")
    parser.add_argument("--endpoint", default="127.0.0.1:9091",
                        help="gNB telnet host[:port] (default 127.0.0.1:9091)")
    parser.add_argument("--permit-source", required=True,
                        metavar="package.module:attribute",
                        help="an object with .acquire(kind, now=...) returning "
                             "a Kernel-issued permit; there is no default, "
                             "because there is no permit without the Kernel")
    parser.add_argument("--snapshot-source", required=True,
                        metavar="package.module:attribute",
                        help="a callable returning the CommonKpiSnapshot this "
                             "assignment is checked against")
    parser.add_argument("--hold-seconds", type=float, default=0.0,
                        help="dwell with the change live (the sampling window)")
    parser.add_argument("--deadline-ms", type=int, default=60_000)
    parser.add_argument("--timeout", type=float, default=3.0,
                        help="telnet connect/read timeout in seconds")
    parser.add_argument("--allow-outside-coordinated-live-set",
                        action="store_true",
                        help="run an xApp the registry keeps out of the "
                             "coordinated live set (e.g. link adaptation)")
    parser.add_argument("--output", default=None,
                        help="write the outcome JSON here as well as stdout")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        transport = build_transport(args.endpoint, timeout=args.timeout)
        deployment = lab_deployment(f"telnet://{transport.endpoint}")
        request = LiveXAppRequest(
            xapp_id=args.xapp_id,
            action_id=args.action_id,
            parameters=_typed_parameters(args.action_id, args.param,
                                         deployment),
            target_selector=_pairs(args.selector),
            cell_id=args.cell_id,
            endpoint=transport.endpoint,
            deadline_ms=args.deadline_ms,
        )
        dwell = None
        if args.hold_seconds > 0:
            def dwell(report):  # noqa: F811 - the sampling window
                del report
                time.sleep(args.hold_seconds)
                return {"heldSeconds": args.hold_seconds}
        outcome = run_one_assignment(
            request,
            send=transport,
            permit_source=_load(args.permit_source),
            snapshot_provider=_load(args.snapshot_source),
            deployment=deployment,
            clock=wall_clock,
            while_applied=dwell,
            outside_coordinated_live_set=args.allow_outside_coordinated_live_set,
        )
    except LiveRunError as exc:
        print(json.dumps({"refused": str(exc)}, indent=2))
        return 2
    record = outcome.to_canonical_dict()
    text = json.dumps(record, indent=2, sort_keys=True)
    print(text)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    return 0 if outcome.applied and outcome.restored else 1


if __name__ == "__main__":
    sys.exit(main())
