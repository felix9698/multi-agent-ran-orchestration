"""Command line for the self-test, the probes, the verifier and the falsifiers.

Every subcommand is a real process with a real exit code, because that is what
the falsifiers assert on.  A falsifier that only reasoned about intent would be
a comment; these exit codes are evidence.

Exit codes (``sysexits.h``)::

    0   the check passed
    65  EX_DATAERR      -- the adjudication found a defect in the evidence
    69  EX_UNAVAILABLE  -- the runtime under test is absent
    70  EX_SOFTWARE     -- an internal self-test failure
    78  EX_CONFIG       -- admission refused, fail-closed
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from . import (
    EXIT_FINDING,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_REFUSED,
    NOT_SC084_ACCEPTANCE,
    SELF_TEST_STATE_LABEL,
)

def _runtime_layout() -> tuple[Path, Path, Path, bool]:
    """Resolve release data explicitly, preserving checkout development mode."""
    release_value = os.environ.get("LO1_RELEASE_ROOT")
    if release_value:
        root = Path(release_value).resolve()
        provenance = root / "ARTIFACT-PROVENANCE.json"
        if not provenance.is_file() or not (root / "lib").is_dir():
            raise RuntimeError("LO1_RELEASE_ROOT is not a complete extracted release")
        return (root, root / "contracts" / "oran-aic" / "1.0.1" /
                "shared-contract-bundle", root / "spec", True)
    source = Path(__file__).resolve().parents[3]
    if not (source / ".git").exists() and not (source / "docs" / "upper-live-o1-harness").is_dir():
        raise RuntimeError("self-test cannot resolve a release or source-tree root")
    return (source, source / "contracts" / "oran-aic" / "1.0.1" /
            "shared-contract-bundle", source / "docs" / "upper-live-o1-harness", False)


REPO_ROOT, BUNDLE, DESIGN, RELEASE_MODE = _runtime_layout()


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def command_interface(args: argparse.Namespace) -> int:
    from .provider_emulator import publish_interface

    _emit(publish_interface(Path(args.bundle)))
    return EXIT_OK


def command_selftest_run(args: argparse.Namespace) -> int:
    from .driver import GateRefused, SelfTestConfig, SelfTestDriver

    config = SelfTestConfig(
        repo_root=REPO_ROOT,
        work_dir=Path(args.work),
        bundle_path=Path(args.bundle) if args.bundle else None,
        seed=args.seed,
        profile=args.profile,
        emulator_faults=tuple(args.faults.split(",")) if args.faults else (),
        require_runtime=args.require_runtime,
        provider_ocidigest=args.provider_digest,
        stale_state_probe=args.stale_state,
        endpoint_collision_probe=args.endpoint_collision,
        drop_secret_ref=args.drop_secret,
        recovery_files_probe=args.recovery_probe,
        release_archive_sha256=args.release_archive_sha256,
    )
    try:
        result = SelfTestDriver(config).run()
    except GateRefused as exc:
        _emit({"status": "REFUSED", "stateLabel": SELF_TEST_STATE_LABEL,
               "detail": str(exc), "notAcceptance": NOT_SC084_ACCEPTANCE})
        return EXIT_REFUSED
    _emit({
        "status": result.status,
        "stateLabel": SELF_TEST_STATE_LABEL,
        "runId": result.run_id,
        "captureRoot": str(result.capture_root),
        "findings": list(result.findings),
        "detail": result.detail,
        "checks": [check.as_capture() for check in result.checks],
        "notAcceptance": NOT_SC084_ACCEPTANCE,
    })
    return result.exit_code


def command_adjudicate(args: argparse.Namespace) -> int:
    from .verifier import IndependentVerifier

    verifier = IndependentVerifier(
        capture_path=Path(args.capture),
        bundle_path=Path(args.bundle),
        gates_path=Path(args.gates),
        capture_schema_path=Path(args.schema),
    )
    report = verifier.readjudicate()
    report["notAcceptance"] = NOT_SC084_ACCEPTANCE
    _emit(report)
    return EXIT_FINDING if report["findingCount"] else EXIT_OK


def command_probe(args: argparse.Namespace) -> int:
    from . import probes

    work = Path(args.work) if args.work else Path(
        tempfile.mkdtemp(prefix="lo1-probe-"))
    if args.name == "emulator-rpc":
        result = probes.probe_emulator_rpc(
            repo_root=REPO_ROOT, bundle_path=Path(args.bundle), work_dir=work)
    elif args.name == "emulator-capability":
        result = probes.probe_emulator_capability(
            repo_root=REPO_ROOT, bundle_path=Path(args.bundle), work_dir=work)
    elif args.name == "emulator-pm":
        result = probes.probe_emulator_pm(
            repo_root=REPO_ROOT, bundle_path=Path(args.bundle), work_dir=work,
            repeat=args.repeat, seed=args.seed)
    elif args.name == "egress":
        result = probes.probe_egress(armed=not args.disarmed)
    elif args.name == "oracle-literals":
        result = probes.probe_oracle_literals(
            repo_root=REPO_ROOT,
            roots=[Path(root) for root in args.roots] if args.roots else None)
    else:  # pragma: no cover - argparse constrains the choices
        return EXIT_INTERNAL
    _emit({"probe": result.probe, "ok": result.ok, "detail": result.detail,
           "observations": result.observations,
           "stateLabel": SELF_TEST_STATE_LABEL})
    return EXIT_OK if result.ok else EXIT_FINDING


def command_determinism(args: argparse.Namespace) -> int:
    from .determinism import SELF_TEST_VOLATILE, compare_runs, negative_control

    first = json.loads((Path(args.a) / "capture.json").read_text(encoding="utf-8"))
    second = json.loads((Path(args.b) / "capture.json").read_text(encoding="utf-8"))
    comparison = compare_runs(first, second, gates_path=Path(args.gates))
    control = negative_control(first, gates_path=Path(args.gates)) \
        if args.negative_control else None
    payload = {
        "stateLabel": SELF_TEST_STATE_LABEL,
        "comparedSlots": comparison.compared,
        "excusedByFrozenAllowlist": comparison.excused,
        "excusedBySelfTestVectorMinting": comparison.self_test_excused,
        "selfTestVolatileDeclaration": SELF_TEST_VOLATILE,
        "differing": comparison.differing,
        "agrees": comparison.agrees,
        "negativeControl": control,
    }
    _emit(payload)
    if not comparison.agrees:
        return EXIT_FINDING
    if control is not None and not control["allDetected"]:
        return EXIT_FINDING
    return EXIT_OK


def command_falsify(args: argparse.Namespace) -> int:
    from .falsifiers import run_falsifiers

    report = run_falsifiers(
        repo_root=REPO_ROOT,
        work_dir=Path(args.work) if args.work else None,
        only=tuple(args.id) if args.id else None,
    )
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _emit(report)
    return EXIT_OK if report["allFalsified"] else EXIT_FINDING


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m oran.release.lo1_selftest",
        description="upper-live-o1-harness self-test: emulator, driver, verifier, "
                    "falsifiers. Upper-side readiness evidence only.")
    sub = parser.add_subparsers(dest="command", required=True)

    interface = sub.add_parser("interface", help="publish the frozen emulator seam S4")
    interface.add_argument("--bundle", default=str(BUNDLE))
    interface.set_defaults(handler=command_interface)

    run = sub.add_parser("selftest-run", help="drive the O1 leg against the emulator")
    run.add_argument("--work", required=True)
    run.add_argument("--bundle", default=None)
    run.add_argument("--seed", type=int, default=20260811)
    run.add_argument("--profile", default="self-test")
    run.add_argument("--faults", default="")
    run.add_argument("--provider-digest", default="UNRESOLVED")
    run.add_argument(
        "--release-archive-sha256",
        default=os.environ.get("LO1_RELEASE_ARCHIVE_SHA256"),
        help=("out-of-band SHA-256 of the exact packaged release archive; "
              "mandatory when LO1_RELEASE_ROOT selects release mode"),
    )
    run.add_argument("--require-runtime", action="store_true")
    run.add_argument("--stale-state", action="store_true")
    run.add_argument("--endpoint-collision", action="store_true")
    run.add_argument("--drop-secret", default=None)
    run.add_argument("--recovery-probe", default=None,
                     choices=[None, "NOT_UNIQUE", "WRONG_CARDINALITY"])
    run.set_defaults(handler=command_selftest_run)

    adjudicate = sub.add_parser(
        "adjudicate", help="independent re-adjudication from raw evidence")
    adjudicate.add_argument("--capture", required=True)
    adjudicate.add_argument("--bundle", default=str(BUNDLE))
    adjudicate.add_argument("--gates", default=str(DESIGN / "release-gates.1.0.0.json"))
    adjudicate.add_argument("--schema", default=str(DESIGN / "capture-schema.2.0.0.json"))
    adjudicate.set_defaults(handler=command_adjudicate)

    probe = sub.add_parser("probe", help="one executable probe")
    probe.add_argument("--name", required=True, choices=[
        "emulator-rpc", "emulator-capability", "emulator-pm", "egress",
        "oracle-literals"])
    probe.add_argument("--bundle", default=str(BUNDLE))
    probe.add_argument("--work", default=None)
    probe.add_argument("--repeat", type=int, default=2)
    probe.add_argument("--seed", type=int, default=None)
    probe.add_argument("--disarmed", action="store_true")
    probe.add_argument("--roots", action="append", default=None)
    probe.set_defaults(handler=command_probe)

    determinism = sub.add_parser(
        "determinism", help="two-run comparison and its negative control")
    determinism.add_argument("--a", required=True)
    determinism.add_argument("--b", required=True)
    determinism.add_argument("--gates", default=str(DESIGN / "release-gates.1.0.0.json"))
    determinism.add_argument("--negative-control", action="store_true")
    determinism.set_defaults(handler=command_determinism)

    falsify = sub.add_parser("falsify", help="run the falsification suite")
    falsify.add_argument("--id", action="append", default=None)
    falsify.add_argument("--work", default=None)
    falsify.add_argument("--report", default=None)
    falsify.set_defaults(handler=command_falsify)

    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:  # pragma: no cover
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
