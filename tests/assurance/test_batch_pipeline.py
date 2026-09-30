"""Batch experiment contract and hardware-free export regression tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class BatchPlanContractTests(unittest.TestCase):
    """A changed execution scope must never retain a previous confirmation."""

    def test_plan_confirmation_is_invalidated_by_scope_change(self) -> None:
        from assurance.batch.plan import BatchPlan, IntentProfile

        plan = BatchPlan(
            objectives=("TrafficSteeringPreference",),
            intent_profiles=(IntentProfile("gold", {"slice": "gold"}),),
            strategy="deterministic",
            repeats=2,
            seed=7,
            ordering="counterbalanced",
        )
        confirmation = plan.confirm(event_id="confirm/batch", timestamp="2026-01-01T00:00:00.000000Z")
        self.assertTrue(plan.is_confirmed_by(confirmation))

        changed = plan.with_scope({"cell": "cell-2"})
        self.assertFalse(changed.is_confirmed_by(confirmation))
        self.assertEqual(len(plan.cases()), 2)
        self.assertNotEqual([case.seed for case in plan.cases()], [])


class BatchPipelineTests(unittest.TestCase):
    """A regression that would drop provenance axes or raw trace links fails."""

    def test_hardware_free_run_exports_traceable_axis_preserving_records(self) -> None:
        from assurance.batch.plan import BatchPlan, IntentProfile
        from assurance.batch.runner import BatchRunner, DeterministicMockKernelExecutor

        plan = BatchPlan(
            objectives=("TrafficSteeringPreference",),
            intent_profiles=(IntentProfile("gold", {"target": "throughput"}, scope={"slice": "gold", "ue": "ue-1"}),),
            strategy="deterministic",
            repeats=2,
            seed=11,
            ordering="randomized",
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = BatchRunner(
                executor=DeterministicMockKernelExecutor(),
                runs_root=Path(tmp),
            ).run(plan, confirmation=plan.confirm(
                event_id="confirm/batch", timestamp="2026-01-01T00:00:00.000000Z"
            ))

            self.assertEqual(result.manifest["mode"], "REPLAY")
            self.assertEqual(result.manifest["disposition"], "COMPLETED")
            self.assertEqual(result.summary["trialCounts"]["valid"], 2)
            self.assertTrue((result.run_dir / "raw" / "batch" / "events.jsonl").is_file())
            self.assertTrue((result.run_dir / "normalized" / "trials.csv").is_file())
            self.assertTrue((result.run_dir / "summary" / "provenance-manifest.json").is_file())
            self.assertTrue((result.run_dir / "summary" / "batch-summary.traceability.json").is_file())
            self.assertTrue((result.run_dir / "figures" / "cdf.png").is_file())

            row = json.loads((result.run_dir / "normalized" / "trials.json").read_text())[0]
            self.assertEqual(row["measurementKind"], "MEASURED")
            self.assertEqual(row["interface"], "E2")
            self.assertEqual(row["scope"]["kind"], "UE")
            self.assertEqual(row["mode"], "REPLAY")
            sidecar = json.loads((result.run_dir / "figures" / "cdf.traceability.json").read_text())
            self.assertEqual(sidecar["sourceRecordIds"], ["trial-0001", "trial-0002"])
            self.assertEqual(sidecar["derivation"], "empirical_cdf(wallClockS)")
            table_sidecar = json.loads((result.run_dir / "summary" / "batch-summary.traceability.json").read_text())
            self.assertEqual(table_sidecar["sourceRecordIds"], ["trial-0001", "trial-0002"])

    def test_demo_plan_covers_two_objective_profile_pairs(self) -> None:
        from assurance.batch.demo import build_demo_plan

        plan = build_demo_plan()
        self.assertEqual(plan.strategy, "deterministic")
        self.assertEqual(len(plan.cases()), 4)
        self.assertEqual(plan.topology["kind"], "mock")

    def test_exclusion_rule_keeps_error_raw_record_but_excludes_it_from_statistics(self) -> None:
        from assurance.batch.plan import BatchPlan, IntentProfile, RetryPolicy
        from assurance.batch.runner import BatchRunner, CaseExecution

        class ErrorThenSuccessExecutor:
            mode = "REPLAY"

            def execute(self, case, plan):
                is_error = case.order_index == 0
                return CaseExecution(
                    outcome="ERROR" if is_error else "SUCCESS",
                    validity="ERROR" if is_error else "VALID",
                    raw_events=({"eventKind": "KernelTerminal"},), measurements={},
                    wall_clock_s=9.0 if is_error else 1.0,
                    target_satisfied=not is_error, hold_satisfied=not is_error,
                )

        plan = BatchPlan(
            objectives=("TrafficSteeringPreference",),
            intent_profiles=(IntentProfile("gold", {"target": "x"}),), strategy="deterministic",
            repeats=2, seed=4, retry=RetryPolicy(inclusion="exclude_errors"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = BatchRunner(executor=ErrorThenSuccessExecutor(), runs_root=Path(tmp)).run(
                plan, confirmation=plan.confirm(event_id="confirm/exclude", timestamp="2026-01-01T00:00:00.000000Z")
            )
        self.assertEqual(result.summary["trialCounts"], {"valid": 1, "invalid": 0, "error": 1, "total": 2})
        self.assertEqual(result.summary["inclusion"], {"rule": "exclude_errors", "included": 1, "excluded": 1})
        self.assertEqual(result.summary["wallClock"]["mean"], 1.0)

    def test_raw_bundle_alone_rederives_paper_inputs_in_an_empty_directory(self) -> None:
        from assurance.batch.plan import BatchPlan, IntentProfile
        from assurance.batch.runner import BatchRunner, DeterministicMockKernelExecutor

        plan = BatchPlan(
            objectives=("TrafficSteeringPreference",),
            intent_profiles=(IntentProfile("gold", {"target": "throughput"}, scope={"ue": "ue-1"}),),
            strategy="deterministic", repeats=2, seed=11,
        )
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as isolated:
            result = BatchRunner(executor=DeterministicMockKernelExecutor(), runs_root=Path(tmp)).run(
                plan, confirmation=plan.confirm(
                    event_id="confirm/rederive", timestamp="2026-01-01T00:00:00.000000Z"
                )
            )
            raw_copy = Path(isolated) / "raw-only.jsonl"
            raw_copy.write_bytes((result.run_dir / "raw" / "batch" / "events.jsonl").read_bytes())
            rederived = Path(isolated) / "rederived"

            completed = subprocess.run(
                [sys.executable, "-m", "assurance.batch.rederive", "--raw-bundle", str(raw_copy),
                 "--output-dir", str(rederived)],
                check=False, capture_output=True, text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            observations = [json.loads(line) for line in raw_copy.read_text().splitlines()]
            canonical = [record for record in observations if record["eventKind"] == "CanonicalTrialObservation"]
            self.assertEqual(len(canonical), 2)
            self.assertTrue(all(record["measurementKind"] == "MEASURED" for record in canonical))
            self.assertTrue(all(record["interface"] == "E2" for record in canonical))
            self.assertTrue(all(record["scope"]["kind"] == "UE" for record in canonical))
            self.assertTrue(all(record["mode"] == "REPLAY" for record in canonical))
            self.assertTrue(all(record["derivedAxes"]["measurementKind"] == "DERIVED" for record in canonical))
            self.assertTrue(all(record["derivedAxes"]["interface"] == "O1" for record in canonical))
            for relative in (
                "normalized/trials.json", "normalized/trials.csv", "summary/statistics.json",
                "summary/batch-summary.tex", "summary/batch-summary.traceability.json",
            ):
                self.assertEqual((rederived / relative).read_bytes(), (result.run_dir / relative).read_bytes(), relative)
            for figure_id in ("cdf", "box", "violin", "convergence", "harm_efficiency", "stacked_outcome", "timeline"):
                for suffix in ("source.csv", "traceability.json"):
                    relative = f"figures/{figure_id}.{suffix}"
                    self.assertEqual((rederived / relative).read_bytes(), (result.run_dir / relative).read_bytes(), relative)
