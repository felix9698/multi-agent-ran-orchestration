"""CLI for the black-box Section 18 conformance runner."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .contracts import ContractBundle, ContractError
from .report import suite_manifest, traceability_report
from .runner import LoopbackDevHttpBoundary, ScenarioRunner
from .vectors import load_deployment_vector
from .harness_api import RoutedHarnessAdapter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", required=True)
    parser.add_argument("--vector", required=True)
    parser.add_argument("--scenario")
    parser.add_argument("--report", choices=["traceability"])
    authority = parser.add_mutually_exclusive_group()
    authority.add_argument(
        "--contract-authority",
        help="authority tree containing shared-contract-bundle (defaults to 1.0.1)")
    authority.add_argument(
        "--contract-bundle",
        help="historical compatibility alias accepting a bundle directory")
    parser.add_argument("--artifacts", default="conformance-results")
    parser.add_argument("--harness-profile", choices=["local-mock"],
                        help="bind suite personas to real component /harness surfaces")
    args = parser.parse_args(argv)
    try:
        bundle = ContractBundle.discover(
            args.contract_authority or args.contract_bundle)
        if args.harness_profile == "local-mock":
            from oran.profiles.local_mock import _load_local_vector, local_schema_validation_shadow
            from .vectors import local_development_vector
            vector = local_development_vector(
                _load_local_vector(Path(args.vector), bundle),
                insecure_dev_loopback=True)
            validation_vector = local_schema_validation_shadow(vector)
        else:
            vector = load_deployment_vector(args.vector, bundle)
            validation_vector = None
        harness = (RoutedHarnessAdapter.from_vector(args.suite, vector)
                   if args.harness_profile == "local-mock" else None)
        sftp = netconf = None
        if args.harness_profile == "local-mock":
            from oran.profiles.local_mock import local_mock_runner_boundaries
            sftp, netconf = local_mock_runner_boundaries(vector)
        runner = ScenarioRunner(bundle, vector, harness=harness,
                                http=LoopbackDevHttpBoundary(vector) if args.harness_profile else None,
                                sftp=sftp, netconf=netconf,
                                artifacts_root=args.artifacts,
                                validation_vector=validation_vector)
        selected = [item for item in bundle.catalog["scenarios"] if item["suite"] == args.suite]
        if args.suite not in bundle.runner["suiteCatalog"]:
            raise ContractError("unknown suite: %s" % args.suite)
        if args.scenario:
            selected = [item for item in selected if item["id"] == args.scenario]
            if not selected:
                raise ContractError("scenario does not belong to requested suite")
        runner.preflight()
        results = [runner.run(scenario) for scenario in selected]
        root = Path(args.artifacts)
        suite_manifest(results, root / "suite-manifest.json")
        if args.report == "traceability":
            traceability_report(bundle, results, root / "traceability.json")
    except ContractError as exc:
        print("FAIL_SCENARIO_WITHOUT_TARGET_CALL: %s" % exc, file=sys.stderr)
        return 2
    return 1 if any(item.disposition == "FAIL" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
