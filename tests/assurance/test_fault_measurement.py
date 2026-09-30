"""Task section 14: telemetry gap, stale sample, clock drift, measurement
insufficiency, validity exit, hard safety guard and harm-limit stop.

Design section 8's rule for all of them: "Invalid, unprovable, or insufficient
traces do not fill closure quotas", and "missing intervals receive
conservative contract-defined charge, never zero or last-value substitution".
So every test here checks two things -- the axis the Kernel reported, and what
the evidence ledger and the harm ledger did about it.  A trace that cannot
decide anything must cost the case its budget and buy it no closure.
"""

from __future__ import annotations

import unittest

from assurance.contracts.harm import HarmKind
from assurance.core.axes import (
    CaseTermination,
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
    TrialOutcome,
)
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.core.states import StopReason, TrialState
from assurance.kernel.kernel import KernelRefusal

from tests.assurance.fault_preservation import (
    PreservationSnapshot,
    assert_finite_terminal,
    snapshot,
)
from tests.assurance.vertical_support import (
    CELL_ID,
    START,
    VerticalFixture,
    failing_collector,
    timeseries,
)


class MeasurementFaultFixture(VerticalFixture):
    def evaluation(self, trial_id: str):
        return self.kernel.reduced_state()["trials"][trial_id]["evaluation"]

    def charges(self):
        return [
            (
                entry["movementKind"],
                float(entry["amount"]["value"]),
                bool(entry.get("chargedForMissingInterval")),
            )
            for entry in self.kernel.reduced_state()["harmLedger"]
        ]

    def cell_status(self) -> str:
        return self.kernel.reduced_state()["evidenceCells"][CELL_ID]["status"]

    def run_case(self, **kwargs):
        path = self.build(**kwargs)
        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)
        return path, report


class TelemetryGapTests(MeasurementFaultFixture, unittest.TestCase):
    def test_a_gap_is_indeterminate_and_takes_the_contract_charge(self) -> None:
        path, report = self.run_case(
            collector=failing_collector("gap", start=START), max_trials=1
        )

        evaluation = self.evaluation(report.trial_id)
        self.assertEqual(
            evaluation["measurementSufficiency"],
            MeasurementSufficiency.MISSING_INTERVAL.value,
        )
        self.assertEqual(
            evaluation["predicateVerdicts"],
            {"dl-throughput-floor": PredicateVerdict.INDETERMINATE.value},
        )
        self.assertEqual(report.outcome, TrialOutcome.INDETERMINATE)
        # The contract's charge, marked as assumed rather than measured.  Not
        # zero, and not the last observed value.
        self.assertIn(("CHARGE", 5.0, True), self.charges())
        # 20 reserved, 5 charged, 15 returned.
        self.assertIn(("RETURN", 15.0, False), self.charges())
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=report.trial_id),
            PreservationSnapshot(
                actual_outcome="INDETERMINATE",
                harm_settlement=(
                    ("RESERVE", 20.0, False),
                    ("CHARGE", 5.0, True),
                    ("RETURN", 15.0, False),
                ),
                reserve_outstanding=0.0,
                evidence=(
                    (
                        CELL_ID,
                        "OPEN",
                        (("VALID", "MISSING_INTERVAL", "INDETERMINATE", False),),
                    ),
                ),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=("ROLLED_BACK",),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )

    def test_a_gapped_trace_fills_no_closure_quota(self) -> None:
        path, report = self.run_case(collector=failing_collector("gap", start=START))

        self.assertEqual(self.cell_status(), "OPEN")
        self.assertEqual(report.evidence_status, "OPEN")
        # The contribution is *recorded* -- the ledger is append-only and an
        # undecidable trial is a fact about the campaign -- and it counts for
        # nothing: the cell is still OPEN and the quota is still unmet.
        contributions = self.kernel.reduced_state()["evidenceCells"][CELL_ID][
            "contributions"
        ]
        self.assertEqual(len(contributions), 1)
        self.assertEqual(contributions[0]["predicateVerdict"], "INDETERMINATE")
        self.assertEqual(
            contributions[0]["measurementSufficiency"], "MISSING_INTERVAL"
        )
        from assurance.core.axes import counts_toward_closure

        self.assertFalse(
            counts_toward_closure(
                ExecutionValidity(contributions[0]["executionValidity"]),
                MeasurementSufficiency(contributions[0]["measurementSufficiency"]),
                PredicateVerdict(contributions[0]["predicateVerdict"]),
            )
        )

    def test_no_observation_at_all_is_insufficient_coverage(self) -> None:
        path, report = self.run_case(
            collector=failing_collector("silent", start=START), max_trials=1
        )

        evaluation = self.evaluation(report.trial_id)
        self.assertEqual(
            evaluation["measurementSufficiency"],
            MeasurementSufficiency.INSUFFICIENT_COVERAGE.value,
        )
        self.assertFalse(evaluation["holdComplete"])
        self.assertEqual(report.outcome, TrialOutcome.INDETERMINATE)
        self.assertEqual(self.cell_status(), "OPEN")
        # Nothing was measured, so nothing is charged beyond the returned
        # reserve -- a gap the collector never even reported is not a licence
        # to charge, and not a licence to close either.
        self.assertEqual(
            [movement for movement, _, _ in self.charges()], ["RESERVE", "RETURN"]
        )
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=report.trial_id),
            PreservationSnapshot(
                actual_outcome="INDETERMINATE",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=("ROLLED_BACK",),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )


class ClockAndStalenessTests(MeasurementFaultFixture, unittest.TestCase):
    def test_an_unhealthy_clock_makes_the_trace_undecidable(self) -> None:
        path, report = self.run_case(
            collector=failing_collector("clock-drift", start=START), max_trials=1
        )

        evaluation = self.evaluation(report.trial_id)
        self.assertEqual(
            evaluation["measurementSufficiency"],
            MeasurementSufficiency.CLOCK_UNHEALTHY.value,
        )
        self.assertEqual(report.outcome, TrialOutcome.INDETERMINATE)
        self.assertEqual(self.cell_status(), "OPEN")
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=report.trial_id),
            PreservationSnapshot(
                actual_outcome="INDETERMINATE",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=(
                    (
                        CELL_ID,
                        "OPEN",
                        (("VALID", "CLOCK_UNHEALTHY", "INDETERMINATE", False),),
                    ),
                ),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=("ROLLED_BACK",),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )

    def test_a_stale_sample_is_reported_not_dropped(self) -> None:
        """The collector never decides staleness on the Kernel's behalf."""
        stale_at = "2026-08-21T08:00:00.000000Z"
        path = self.build(collector=failing_collector("stale", start=stale_at))

        released = self.collector.poll(now=self.clock())

        self.assertEqual(len(released), 1)
        self.assertFalse(released[0].is_fresh(self.clock(), freshness_bound_ms=2000))

    def test_a_sample_older_than_the_observation_window_decides_nothing(self) -> None:
        stale_at = "2026-08-21T08:00:00.000000Z"
        path, report = self.run_case(
            collector=failing_collector("stale", start=stale_at), max_trials=1
        )

        evaluation = self.evaluation(report.trial_id)
        self.assertEqual(
            evaluation["measurementSufficiency"],
            MeasurementSufficiency.INSUFFICIENT_COVERAGE.value,
        )
        self.assertEqual(report.outcome, TrialOutcome.INDETERMINATE)
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=report.trial_id),
            PreservationSnapshot(
                actual_outcome="INDETERMINATE",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=("ROLLED_BACK",),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )


class ValidityAndGuardTests(MeasurementFaultFixture, unittest.TestCase):
    def test_a_validity_region_exit_is_invalid_not_a_kpi_failure(self) -> None:
        # Predicates pass at 4.0 Mbps; the validity region demands 10.
        path, report = self.run_case(
            validity_bound=10.0,
            collector=timeseries(start=START, value=4.0),
            max_trials=1,
        )

        evaluation = self.evaluation(report.trial_id)
        self.assertEqual(
            evaluation["executionValidity"], ExecutionValidity.INVALID.value
        )
        self.assertFalse(evaluation["validityRegionStable"])
        self.assertEqual(report.stop_reason, StopReason.VALIDITY_EXIT)
        self.assertEqual(report.outcome, TrialOutcome.INVALID)
        self.assertEqual(report.terminal_state, TrialState.SETTLED_NON_SUCCESS)
        # An invalid execution is not a tested candidate: no closure.
        self.assertEqual(self.cell_status(), "OPEN")
        assert_finite_terminal(self, path, CaseTermination.EVIDENCE_INCOMPLETE)
        self.assertEqual(
            snapshot(self, path, trial_id=report.trial_id),
            PreservationSnapshot(
                actual_outcome="INVALID",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", (("INVALID", "SUFFICIENT", "PASS", False),)),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=("ROLLED_BACK",),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="EVIDENCE_INCOMPLETE",
            ),
        )

    def test_a_hard_safety_guard_outranks_passing_predicates(self) -> None:
        path = self.build()
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)
        path.ready(trial_id)
        path.commit_decision(trial_id)
        path.commit(trial_id)
        path.enter_observation(trial_id)
        for _ in range(4):
            self.clock.advance(1000)
            path.collect()
        self.clock.advance(1000)

        decided = path.decide(
            trial_id, stop_reasons=(StopReason.HARD_SAFETY_GUARD,)
        )

        self.assertIs(decided, TrialState.STOPPING)
        evaluation = self.evaluation(trial_id)
        # The predicates did pass; the guard outranks them (design section 7).
        self.assertEqual(
            evaluation["predicateVerdicts"],
            {"dl-throughput-floor": PredicateVerdict.PASS.value},
        )
        self.assertEqual(
            self.kernel.reduced_state()["trials"][trial_id]["stopReason"],
            StopReason.HARD_SAFETY_GUARD.value,
        )
        self.assertTrue(path.stop_and_rollback(trial_id))
        path.settle(trial_id, outcome=TrialOutcome.SAFETY_STOPPED, cell_id=CELL_ID)

        # The observation that *was* made is recorded honestly -- the trace is
        # valid, sufficient and passing, and design section 8 keeps the
        # predicate verdict and the trial outcome on separate axes.  What the
        # guard denies is the deployed success: the change is reversed, the
        # trial settles SAFETY_STOPPED, and the case terminates as a safety
        # incident rather than a success.
        self.assertEqual(self.cell_status(), "CLOSED_PASS")
        self.assertEqual(
            self.kernel.reduced_state()["trials"][trial_id]["outcome"],
            TrialOutcome.SAFETY_STOPPED.value,
        )
        self.assertFalse(
            self.kernel.reduced_state()["cases"][path.case_id].get("deployedSuccess")
        )
        self.assertEqual(self.adapter.snapshot()["servingCell"], "cell-1")
        assert_finite_terminal(self, path, CaseTermination.SAFETY_INCIDENT)
        self.assertEqual(
            snapshot(self, path, trial_id=trial_id),
            PreservationSnapshot(
                actual_outcome="SAFETY_STOPPED",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "CLOSED_PASS", (("VALID", "SUFFICIENT", "PASS", False),)),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=("ROLLED_BACK",),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="SAFETY_INCIDENT",
            ),
        )

    def test_a_charge_beyond_the_trial_reserve_stops_the_trial(self) -> None:
        path = self.build(max_trials=1)
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)
        path.ready(trial_id)
        path.commit_decision(trial_id)
        path.commit(trial_id)
        self.kernel.advance_trial(trial_id, TrialState.APPLYING, now=self.clock())

        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.charge_harm(
                trial_id,
                amount=TypedQuantity(
                    25.0, "ms", Provenance.MEASURED, "observed/overrun"
                ),
                harm_kind=HarmKind.TRIAL_INDUCED,
                for_missing_interval=False,
                now=self.clock(),
            )

        self.assertEqual(refusal.exception.reason, "TRIAL_RESERVE_EXCEEDED")
        trial = self.kernel.reduced_state()["trials"][trial_id]
        self.assertEqual(trial["state"], TrialState.STOPPING.value)
        self.assertEqual(trial["stopReason"], StopReason.HARM_LIMIT_BREACH.value)
        # The refused charge is not in the ledger: an over-limit charge is a
        # stop, not a debt.
        self.assertEqual(
            [movement for movement, _, _ in self.charges()], ["RESERVE"]
        )
        self.assertTrue(path.stop_and_rollback(trial_id))
        path.settle(trial_id, outcome=TrialOutcome.SAFETY_STOPPED)
        assert_finite_terminal(self, path, CaseTermination.SAFETY_INCIDENT)
        self.assertEqual(
            snapshot(self, path, trial_id=trial_id),
            PreservationSnapshot(
                actual_outcome="SAFETY_STOPPED",
                harm_settlement=(("RESERVE", 20.0, False), ("RETURN", 20.0, False)),
                reserve_outstanding=0.0,
                evidence=((CELL_ID, "OPEN", ()),),
                configuration=(("queuePriority", 1), ("servingCell", "cell-1")),
                locks=(),
                recovery=("ROLLED_BACK",),
                trial_terminal="SETTLED_NON_SUCCESS",
                case_terminal="SAFETY_INCIDENT",
            ),
        )

    def test_a_conservative_missing_charge_may_never_be_zero(self) -> None:
        path = self.build()
        trial_id = path.open_trial(path.request_proposal()[0])
        path.reserve_and_stage(trial_id)
        path.prepare(trial_id)
        path.ready(trial_id)
        path.commit_decision(trial_id)
        path.commit(trial_id)
        self.kernel.advance_trial(trial_id, TrialState.APPLYING, now=self.clock())

        with self.assertRaises(KernelRefusal) as refusal:
            self.kernel.charge_harm(
                trial_id,
                amount=TypedQuantity(0.0, "ms", Provenance.MEASURED, "gap/assumed"),
                harm_kind=HarmKind.TRIAL_INDUCED,
                for_missing_interval=True,
                now=self.clock(),
            )

        self.assertEqual(refusal.exception.reason, "NON_CONSERVATIVE_MISSING_CHARGE")


class SampleIngestTests(MeasurementFaultFixture, unittest.TestCase):
    def test_a_duplicate_or_reordered_sample_is_refused(self) -> None:
        path = self.build()
        samples = list(self.collector.poll(now="2026-08-21T09:00:10.000000Z"))
        self.assertEqual(len(samples), 5)

        with self.assertRaises(KernelRefusal) as duplicate:
            self.kernel.ingest_raw_sample(samples[0], now=self.clock())
        self.assertEqual(duplicate.exception.reason, "DUPLICATE_RAW_SAMPLE")

        from dataclasses import replace

        reordered = replace(samples[-1], sample_id="out-of-order", sequence=99)
        with self.assertRaises(KernelRefusal) as gap:
            self.kernel.ingest_raw_sample(reordered, now=self.clock())
        self.assertEqual(gap.exception.reason, "REORDERED_RAW_SAMPLE")


if __name__ == "__main__":
    unittest.main()
