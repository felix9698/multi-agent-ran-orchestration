"""One reproducible, non-LIVE Batch demo plan and command-line runner."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from assurance.batch.plan import BatchPlan, Budget, IntentProfile, RetryPolicy, Windows
from assurance.batch.runner import BatchRunner, DeterministicMockKernelExecutor


def build_demo_plan() -> BatchPlan:
    """Return the checked-in mock topology demo used by hardware-free CI."""
    return BatchPlan(
        objectives=("TrafficSteeringPreference",),
        intent_profiles=(
            IntentProfile("gold-ue-1", {"throughputFloorMbps": 6.0},
                          scope={"slice": "gold", "ue": "ue-1"}),
            IntentProfile("silver-ue-2", {"throughputFloorMbps": 5.0},
                          scope={"slice": "silver", "ue": "ue-2"}),
        ),
        strategy="deterministic", repeats=2, seed=20260825,
        ordering="counterbalanced", budget=Budget(cases=4, trials_per_case=1, harm=10.0),
        windows=Windows(measurement_s=1.0, observation_s=1.0, hold_s=1.0,
                        warmup_s=0.0, recovery_s=0.0),
        retry=RetryPolicy(retries=0, abort_on_error=True, inclusion="all_terminal"),
        scope={"site": "mock-site", "cell": "cell-1"},
        model={"family": "deterministic", "version": "mock-1"},
        topology={"kind": "mock", "adapter": "MockActuationAdapter", "liveCalls": 0},
    )


def run_demo(runs_root: Path | str) -> Path:
    """Execute the demo solely against the deterministic mock executor."""
    plan = build_demo_plan()
    result = BatchRunner(executor=DeterministicMockKernelExecutor(), runs_root=runs_root).run(
        plan, confirmation=plan.confirm(event_id="demo/confirm-batch")
    )
    return result.run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the hardware-free Batch demo")
    parser.add_argument("--runs-root", default="batch_demo_runs")
    parser.add_argument("--print-plan", action="store_true")
    args = parser.parse_args()
    plan = build_demo_plan()
    if args.print_plan:
        print(json.dumps(plan.canonical_dict(), indent=2, sort_keys=True))
        return 0
    print(run_demo(args.runs_root))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by command line
    raise SystemExit(main())
