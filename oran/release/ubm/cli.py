"""Launcher for the upper-bilateral-mock runtime.

The packaged ``bin/ubm`` is a thin shim: it computes
``ORAN_CONTRACT_AUTHORITY`` from its own ``__file__`` *before* the first
``import oran.*`` (``oran/contract/__init__.py`` verifies the authority at import
time) and then calls :func:`main`.  All argument parsing, the two-key
integration-control approval and the fail-closed exit codes live here.

Exit codes: ``0`` success, ``78`` (``EX_CONFIG``) for every configuration,
preflight, dependency-lock, TLS-reference or port-occupancy failure.  There is
no other outcome; the runtime never downgrades a failure into a warning.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Sequence

EXIT_CONFIG = 78
APPROVAL_VALUE = "approved"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ubm", description="Run the upper-bilateral-mock/1.0.0 runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="bind the four upper listeners")
    start.add_argument("--startup", required=True, type=Path)
    start.add_argument("--integration-control-surface", default=None,
                       choices=[APPROVAL_VALUE],
                       help="first of the two integration-control keys")
    start.add_argument("--run-id", default=None)
    start.add_argument("--state-dir", default=None)
    start.add_argument("--secret-map", default=None)
    start.add_argument("--ready-file", default=None, type=Path,
                       help="write the readiness document here once bound")

    bootstrap = subparsers.add_parser(
        "bootstrap-tls", help="mint loopback TLS material and a secret map")
    bootstrap.add_argument("--out", required=True, type=Path)

    runtime = subparsers.add_parser(
        "bootstrap", help="write TLS material, secret map, values and startup")
    runtime.add_argument("--out", required=True, type=Path)
    runtime.add_argument("--vector", required=True, type=Path)
    runtime.add_argument("--contract-authority", required=True, type=Path)
    runtime.add_argument("--run-id", required=True)
    runtime.add_argument("--release-manifest", default=None, type=Path)
    runtime.add_argument("--integration-control-surface", default=None,
                         choices=[APPROVAL_VALUE])

    check = subparsers.add_parser(
        "preflight", help="run every start-time assertion without binding")
    check.add_argument("--startup", required=True, type=Path)
    check.add_argument("--integration-control-surface", default=None,
                       choices=[APPROVAL_VALUE])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "bootstrap-tls":
        return _bootstrap(args)
    if args.command == "bootstrap":
        return _bootstrap_runtime(args)
    if args.command == "preflight":
        return _preflight(args)
    return _start(args)


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if getattr(args, "integration_control_surface", None):
        overrides["integrationControlSurface"] = args.integration_control_surface
    for flag, key in (("run_id", "runId"), ("state_dir", "stateDir"),
                      ("secret_map", "secretMapPath")):
        value = getattr(args, flag, None)
        if value:
            overrides[key] = value
    return overrides


def _bootstrap(args: argparse.Namespace) -> int:
    from .tls import TlsConfigurationError, bootstrap_loopback_tls

    try:
        mapping = bootstrap_loopback_tls(Path(args.out))
    except TlsConfigurationError as exc:
        print(json.dumps({"error": str(exc), "exitCode": EXIT_CONFIG}),
              file=sys.stderr)
        return EXIT_CONFIG
    print(json.dumps({"secretMap": str(Path(args.out) / "secret-map.json"),
                      "references": sorted(mapping)}, sort_keys=True))
    return 0


def _bootstrap_runtime(args: argparse.Namespace) -> int:
    from .bootstrap import bootstrap_runtime

    try:
        result = bootstrap_runtime(
            out_dir=Path(args.out), vector_path=Path(args.vector),
            contract_authority=Path(args.contract_authority),
            run_id=str(args.run_id),
            integration_control_surface=args.integration_control_surface,
            release_manifest_path=args.release_manifest)
    except Exception as exc:  # fail closed, never a partial bootstrap
        return _fail(exc)
    print(json.dumps(result, sort_keys=True))
    return 0


def _load(args: argparse.Namespace) -> Any:
    from .config import load_startup_config

    return load_startup_config(Path(args.startup), argv_overrides=_overrides(args))


def _preflight(args: argparse.Namespace) -> int:
    from .service import UpperBilateralMockService

    try:
        config = _load(args)
        service = UpperBilateralMockService.from_startup(config)
    except Exception as exc:  # every failure is EX_CONFIG unless it says otherwise
        return _fail(exc)
    document = service.readiness()
    service.stop()
    print(json.dumps(document, sort_keys=True))
    return 0


def _start(args: argparse.Namespace) -> int:
    from .service import UpperBilateralMockService

    try:
        config = _load(args)
        service = UpperBilateralMockService.from_startup(config)
        service.start()
    except Exception as exc:  # every failure is EX_CONFIG unless it says otherwise
        return _fail(exc)
    document = service.readiness()
    if getattr(args, "ready_file", None):
        Path(args.ready_file).write_text(
            json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(document, sort_keys=True), flush=True)
    stop = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_args: stop.set())
    # Blocking on an Event is not a hidden sleep: it waits for a signal or for
    # POST /ubm/v1/stop, never for a fixed amount of wall-clock time.
    while not stop.is_set() and not service._stopped:  # noqa: SLF001
        stop.wait(timeout=0.5)
    service.stop()
    return 0


def _fail(exc: BaseException) -> int:
    code = int(getattr(exc, "exit_code", EXIT_CONFIG))
    print(json.dumps({"error": type(exc).__name__, "detail": str(exc),
                      "exitCode": code}, sort_keys=True), file=sys.stderr)
    return code


def contract_authority_from_release(release_root: Path) -> str:
    """Value ``bin/ubm`` must export before the first ``import oran.*``."""
    return str(Path(release_root) / "contracts" / "oran-aic" / "1.0.1")


def apply_contract_authority(release_root: Path) -> None:
    os.environ.setdefault("ORAN_CONTRACT_AUTHORITY",
                          contract_authority_from_release(release_root))
