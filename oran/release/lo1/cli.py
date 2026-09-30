"""``lo1`` command line: gate, plan, readiness, run.

Every path is fail-closed.  A gate refusal exits 78 **and** writes a capture
whose ``run.disposition`` is ``ABORTED_GATE`` and whose
``authority.gateResult`` is ``REFUSED``, because a silent refusal is itself a
failure (G-GATE-2).  When the refusal happened so early that the capture
skeleton itself cannot be measured -- a corrupt contract bundle, an unreadable
release manifest -- the command says exactly that instead of writing a document
full of values it never observed.

No subcommand can turn a refusal into a pass: there is no ``--force``, no
per-check override and no environment escape.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from oran.conformance.contracts import ContractBundle

from .capture import CaptureError, CaptureRecorder, ObservedClock, RawStore, default_schema_path
from .config import Lo1StartupConfig, StartupConfigurationError, load_startup_config
from .core_service import ServiceStartupError, UpperLiveO1HarnessService, run_gate_under_guard
from .egress import EgressGuard
from .gate import GateRefused, GateResult
from .plan import LIVE_O1_SCENARIO, build_plan, oracle_reference, roots_from_vector
from .preflight import load_vector

#: The capture schema requires at least one allowlist entry.  A refusal
#: before G-ID-06 has no admitted authority yet, so the document names the
#: absence explicitly instead of inventing an address.
UNRESOLVED_AUTHORITY = "unresolved-authority:0"

EXIT_OK = 0
EXIT_CONFIG = 78
EXIT_FAILURE = 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lo1", description=__doc__)
    parser.add_argument("command",
                        choices=("gate", "plan", "readiness", "run"))
    parser.add_argument("--startup", required=True,
                        help="path to the startup document")
    parser.add_argument("--out-dir", default=None,
                        help="where the capture and the ledger are written")
    arguments = parser.parse_args(list(argv) if argv is not None else None)

    try:
        config = load_startup_config(Path(arguments.startup))
    except StartupConfigurationError as exc:
        _emit({"outcome": "STARTUP_REFUSED", "detail": str(exc)})
        return EXIT_CONFIG

    out_dir = Path(arguments.out_dir) if arguments.out_dir else Path(config.capture_root)

    if arguments.command == "plan":
        return _plan(config)
    try:
        gate, observed = run_gate_under_guard(config)
    except GateRefused as refusal:
        return _refused(config, refusal, out_dir)
    if arguments.command == "gate":
        _emit({
            "outcome": "ADMITTED",
            "connectionAttemptsDuringGate": int(observed["connectionAttempts"]),
            "checks": [check.as_capture() for check in gate.checks],
            "unresolved": gate.unresolved_ledger(),
            "providerKind": gate.provider_kind,
            "deterministicStubRouting": gate.deterministic_stub_routing,
            "netconfAdapterActions": sorted(gate.netconf_adapter_actions),
        })
        return EXIT_OK

    try:
        service = UpperLiveO1HarnessService.from_startup(config)
    except (ServiceStartupError, GateRefused) as exc:
        _emit({"outcome": "START_REFUSED", "detail": str(exc)})
        return EXIT_CONFIG
    if arguments.command == "readiness":
        started = False
        try:
            service.start()
            started = True
            _emit(service.readiness())
        except Exception as exc:  # noqa: BLE001 - structured nonzero refusal
            _emit({"outcome": "READINESS_FAILED", "detail": str(exc)})
            return EXIT_FAILURE
        finally:
            if started:
                try:
                    service.stop(failure=False)
                except Exception as exc:  # noqa: BLE001 - teardown is reported
                    _emit({"outcome": "CLEANUP_FAILED", "detail": str(exc)})
        return EXIT_OK

    failure = False
    termination_reason = "STARTUP_FAILURE"
    started = False
    reset = False
    exported: dict[str, str] = {}
    run_started_monotonic = time.monotonic()
    with _termination_signal_guard() as termination_event:
        try:
            service.start()
            started = True
            service.reset(scenario_id=LIVE_O1_SCENARIO)
            reset = True
            service.seed(scenario_id=LIVE_O1_SCENARIO,
                         initial_state=list(service.plan.initial_state))
            readiness = service.readiness()
            readiness["runtimeControl"] = {
                "owner": "LOWER_SIDE_CONFORMANCE_RUNNER",
                "terminationSignals": ["SIGINT", "SIGTERM"],
                "httpStopEndpoint": None,
                "maximumRunDurationMs": int(
                    service.gate.observations["maximumRunDurationMs"]),
            }
            _emit(readiness)
            termination_reason = _wait_for_termination(
                service.gate, event=termination_event,
                run_started_monotonic=run_started_monotonic)
            if termination_reason != "EXTERNAL_TERMINATION_SIGNAL":
                failure = True
                service.note_runtime_timeout()
        except Exception as exc:  # noqa: BLE001 - a failure still runs cleanup
            failure = True
            _emit({"outcome": "RUN_FAILED", "detail": str(exc)})
        finally:
            if reset:
                try:
                    exported = service.export(out_dir)
                except (ServiceStartupError, CaptureError) as exc:
                    exported = {"captureError": str(exc)}
                    failure = True
            if started:
                try:
                    service.stop(failure=failure)
                except Exception as exc:  # noqa: BLE001 - cleanup failure is reported
                    failure = True
                    _emit({"outcome": "CLEANUP_FAILED", "detail": str(exc)})
    # The exit code follows the RECORDED disposition, not the absence of an
    # exception.  A run that raised nothing but never established the §10.2
    # readiness lifecycle is dispositioned ABORTED_ERROR, and exiting zero on it
    # would report the withdrawn 1.0.1 behaviour with a different label.
    disposition = "UNKNOWN"
    unmet: list[str] = []
    try:
        recorded = service.recorder.snapshot()
        disposition = str(recorded["run"]["disposition"])
        readiness = (recorded.get("netconf") or {}).get("readiness") or {}
        unmet = [str(item) for item in readiness.get("unmet", [])]
    except Exception:  # noqa: BLE001 - an unreadable capture is itself a failure
        failure = True
    if disposition != "COMPLETED":
        failure = True
    _emit({"outcome": "RUN_COMPLETE", "export": exported,
           "terminationReason": termination_reason,
           "disposition": disposition, "o1ReadinessUnmet": unmet})
    return EXIT_FAILURE if failure else EXIT_OK


def _plan(config: Lo1StartupConfig) -> int:
    bundle = ContractBundle(Path(config.contract_authority))
    vector, _digest, _raw = load_vector(Path(config.vector_path))
    plan = build_plan(bundle.catalog, LIVE_O1_SCENARIO, roots_from_vector(vector))
    _emit({
        "scenario": plan.scenario_shape(),
        "oracle": oracle_reference(bundle.catalog, LIVE_O1_SCENARIO),
        "stepIds": list(plan.step_ids),
        "plannedExchanges": [
            {"stepId": item.step_id, "stepIndex": item.step_index,
             "method": item.method, "endpointRef": item.endpoint_ref,
             "declaredExpectedHttpStatus": item.expected_status}
            for item in plan.exchanges],
        "initialState": list(plan.initial_state),
        "rules": list(plan.rules),
    })
    return EXIT_OK


def _refused(config: Lo1StartupConfig, refusal: GateRefused, out_dir: Path) -> int:
    payload: dict[str, Any] = {
        "outcome": "GATE_REFUSED",
        "detail": str(refusal),
        "connectionAttemptsDuringGate": int(
            getattr(refusal, "connection_attempts", 0)),
    }
    result = refusal.result
    if result is not None:
        payload["checks"] = [check.as_capture() for check in result.checks]
        payload["unresolved"] = result.unresolved_ledger()
        try:
            payload["capture"] = write_gate_refusal_capture(config, result, out_dir)
        except (CaptureError, OSError, KeyError, ValueError, TypeError) as exc:
            payload["captureError"] = (
                "the refusal capture could not be measured: %s" % exc)
    _emit(payload)
    return EXIT_CONFIG


def write_gate_refusal_capture(config: Lo1StartupConfig, result: GateResult,
                               out_dir: Path) -> str:
    """G-GATE-2: a refusal is evidence, so it is written as a capture."""
    from .gate import _contract_digests  # noqa: PLC0415 - same-package seam

    bundle = ContractBundle(Path(config.contract_authority))
    vector, vector_sha256, _raw = load_vector(Path(config.vector_path))
    manifest = json.loads(
        Path(config.release_manifest_path).read_text(encoding="utf-8"))
    plan = build_plan(bundle.catalog, LIVE_O1_SCENARIO, roots_from_vector(vector))
    oracle = oracle_reference(bundle.catalog, LIVE_O1_SCENARIO)
    observed = dict(result.observations or {})
    contract = dict(_contract_digests(bundle))
    contract.update({
        "contractProfile": "oran-aic/1.0.1",
        "correctedHandoff": "1.0.1",
        "frozenBundleDiffFileCount": int(
            observed.get("frozenBundleDiffFileCount", 0)),
    })
    envelope = {
        "schemaPath": str(default_schema_path()),
        "revisions": observed.get("revisions") or _revisions_from(manifest, config),
        "contract": contract,
        "provider": observed.get("provider") or _unresolved_provider(),
        "deployment": {
            "vectorVersion": str(vector["vectorVersion"]),
            "vectorSha256": vector_sha256,
            "bindingDocSha256": oracle["expectedJcsSha256"],
            "placeholderFree": True,
            "resolvedRoots": observed.get("resolvedRoots")
            or _roots_for_capture(vector),
            "originOwnership": observed.get("originOwnership", {}),
            "secretRefsResolved": list(observed.get("secretRefsResolved", [])),
            "unresolvedSecretRefs": list(observed.get("unresolvedSecretRefs", [])),
        },
        "scenario": plan.scenario_shape(),
        "authorityAllowlist": list(result.allowlist) or [UNRESOLVED_AUTHORITY],
        "declaredRuleIds": list(plan.rules),
        "requiresRealCoordinatorExecution": True,
    }
    clock = ObservedClock()
    capture_root = Path(config.capture_root)
    capture_root.mkdir(parents=True, exist_ok=True)
    recorder = CaptureRecorder(
        run_id=config.run_id, scenario_id=LIVE_O1_SCENARIO, envelope=envelope,
        clock=clock, raw_store=RawStore(capture_root=capture_root))
    guard = EgressGuard(allowlist=result.allowlist or (UNRESOLVED_AUTHORITY,),
                        hardware_source="refusal capture; the guard was armed "
                                        "with an empty allowlist during the gate")
    guard.install()
    guard.uninstall()
    recorder.bind_egress_guard(guard)
    recorder.set_gate_result(result)
    recorder.set_disposition("ABORTED_GATE")
    recorder.set_cleanup_path(path="FAILURE", complete=True, residual=[])
    target = Path(out_dir) / ("capture-%s-%s-gate-refused.json" % (
        config.run_id, LIVE_O1_SCENARIO))
    recorder.export(target)
    return str(target)


def _revisions_from(manifest: Mapping[str, Any],
                    config: Lo1StartupConfig) -> dict[str, Any]:
    """Project the manifest's identity into the capture's ``/revisions``.

    Used only when the gate refused before ``G-ID-02`` observed the block, so
    every value here is read from bytes rather than defaulted.  The delivery
    archive digest is not a packaged member (A9): it is resolved from the
    out-of-band record the manifest names, and a refusal that never got that
    far reports the absence rather than a fabricated digest.
    """
    import hashlib

    from .gate import ARCHIVE_DIGEST_PUBLICATION_MEMBER, _digest_record

    revisions = dict(manifest.get("revisions") or {})
    manifest_sha256 = hashlib.sha256(
        Path(config.release_manifest_path).read_bytes()).hexdigest()
    publication = str(revisions.get(ARCHIVE_DIGEST_PUBLICATION_MEMBER, ""))
    name, _separator, record = publication.partition("#")
    archive = ""
    if name and record and not Path(name).is_absolute():
        archive, _target = _digest_record(
            Path(config.release_root) / name, record)
    if not archive:
        raise ValueError(
            "the out-of-band delivery-archive digest record named by "
            "%s was not supplied, so /revisions cannot be measured"
            % (publication or ARCHIVE_DIGEST_PUBLICATION_MEMBER))
    oci = (manifest.get("provenance") or {}).get("oci") or {}
    return {
        "upperReleaseId": str(manifest.get("releaseId", "upper-live-o1-harness")),
        "upperReleaseVersion": str(manifest.get("releaseVersion", "UNRESOLVED")),
        "upperReleaseContentSha256": str(revisions["upperReleaseContentSha256"]),
        "upperReleaseManifestSha256": manifest_sha256,
        "upperDeliveryArchiveSha256": archive,
        "upperSourceCommit": str(revisions["upperSourceCommit"]),
        "upperSourceTree": str(revisions["upperSourceTree"]),
        "testedCodeCommit": str(revisions["testedCodeCommit"]),
        "upperOciImageManifestDigest": str(
            revisions.get("upperOciImageManifestDigest")
            or oci.get("imageManifestDigest", "")),
    }


def _unresolved_provider() -> dict[str, Any]:
    return {
        "releaseId": "UNRESOLVED", "releaseVersion": "UNRESOLVED",
        "sourceCommit": "UNRESOLVED", "sourceTree": "UNRESOLVED",
        "releaseManifestSha256": "UNRESOLVED",
        "ociImageManifestDigest": "UNRESOLVED",
        "yangClosureDigest": "UNRESOLVED", "yangCapabilityDigest": "UNRESOLVED",
        "acceptanceRecordDigest": "UNRESOLVED",
        "runtimeOwner": "UNRESOLVED", "teardownOwner": "UNRESOLVED",
    }


def _roots_for_capture(vector: Mapping[str, Any]) -> dict[str, Any]:
    file_data = vector["o1"]["fileDataReporting"]
    return {
        "r1ApiRoot": vector["r1"]["apiRoot"],
        "rAppCallbackRoot": vector["r1"]["callbackApi"]["rootUri"],
        "a1ApiRoot": vector["a1"]["apiRoot"],
        "a1StatusCallbackRoot": vector["a1"]["statusCallbackRoot"],
        "policyEvidencePushBaseUri": vector["r1"]["dme"]["policyEvidencePushBaseUri"],
        "mnsRoot": file_data["mnsRoot"],
        "o1ConsumerRoot": file_data["consumerReference"],
        "netconfEndpoint": vector["o1"]["netconf"]["endpoint"],
        "sftpAuthorities": list(vector["o1"]["sftp"]["allowedAuthorities"]),
    }


def _emit(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    # Readiness is a handshake to a supervising lower runner.  The process is
    # intentionally long-lived, so relying on exit-time buffering deadlocks
    # that supervisor before it can drive SC-084.
    sys.stdout.flush()


def _wait_for_termination(
        gate: GateResult, *, event: threading.Event,
        run_started_monotonic: float | None = None) -> str:
    """Wait for the lower runner's signal, bounded by both authorities.

    No HTTP stop route is invented: the frozen catalog contains no such
    operation.  SIGINT/SIGTERM merely request teardown; they are never treated
    as proof that the measured SC-084 body is complete.
    """
    observed = dict(gate.observations or {})
    maximum_ms = observed.get("maximumRunDurationMs")
    if isinstance(maximum_ms, bool) or not isinstance(maximum_ms, int) \
            or maximum_ms <= 0:
        raise ServiceStartupError(
            "the admitted gate did not retain maximumRunDurationMs")
    approved_after = _parse_utc(gate.execution_window[1])
    wall_remaining = max(
        0.0, (approved_after - datetime.now(timezone.utc)).total_seconds())
    elapsed = max(0.0, time.monotonic() - (
        time.monotonic() if run_started_monotonic is None
        else float(run_started_monotonic)))
    duration_remaining = max(0.0, float(maximum_ms) / 1000.0 - elapsed)
    timeout = min(duration_remaining, wall_remaining)
    return ("EXTERNAL_TERMINATION_SIGNAL"
            if event.wait(timeout=timeout) else "WATCHDOG_TIMEOUT")


@contextmanager
def _termination_signal_guard():
    """Install handlers before startup and retain them through teardown."""
    stopped = threading.Event()
    previous: dict[int, Any] = {}

    def request_stop(_signum: int, _frame: Any) -> None:
        stopped.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
    except ValueError as exc:
        raise ServiceStartupError(
            "the long-lived run must execute on the process main thread") from exc
    try:
        yield stopped
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _parse_utc(value: str) -> datetime:
    text = str(value)
    if not text.endswith("Z"):
        raise ServiceStartupError("approvedNotAfter is not a UTC instant")
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ServiceStartupError("approvedNotAfter is malformed") from exc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
