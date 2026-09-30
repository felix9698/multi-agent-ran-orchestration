"""Raw-trace evaluation tests for lane KERN."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from enum import Enum
import unittest
from unittest.mock import patch

from assurance.collector.samples import ClockHealth, MissingInterval, RawSample
from assurance.contracts.measurement import (
    Aggregation,
    ClockRequirement,
    Estimator,
    GapPolicy,
    MeasurementContract,
    OverlapPolicy,
    UncertaintyRule,
)
from assurance.contracts.target import (
    ComparisonOperator,
    TargetContract,
    TargetPredicate,
    TypedConstraint,
)
from assurance.core.addressing import content_hash
from assurance.core.axes import (
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
)
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.core.states import TrialState
from assurance.kernel.kernel import KernelRefusal
from tests.assurance.test_kern_lifecycle import (
    NOW,
    T1,
    T2,
    T3,
    acknowledge_commit,
    advance_to_ready,
    catalog,
    epoch,
    make_kernel,
    policy,
    q,
)


T4 = "2026-08-21T00:00:04.000000Z"


def plain(value):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def contracts() -> tuple[TargetContract, MeasurementContract]:
    threshold = TypedQuantity(
        value=5,
        unit="Mbps",
        provenance=Provenance.EXPERIMENT_CONFIG,
        source_record="target-threshold",
    )
    measurement = MeasurementContract(
        contract_id="measurement-1",
        version="1.0.0",
        schema_version="assurance/1.0.0",
        document_status="NORMATIVE",
        standard_mapping={},
        counter_id="counter-1",
        scope_selector={"cellId": "cell-1"},
        membership_snapshot=("ue-1",),
        cadence_ms=1000,
        window_width_ms=1000,
        window_stride_ms=1000,
        overlap=OverlapPolicy.DISJOINT,
        aggregation=Aggregation.MEAN,
        estimator=Estimator.SAMPLE_MEAN,
        minimum_entity_count=1,
        hold_ms=0,
        gap_policy=GapPolicy.CONSERVATIVE_CHARGE,
        missing_interval_charge=q(1, source="measurement-gap-charge"),
        freshness_bound_ms=5000,
        clock_requirement=ClockRequirement.SYNCHRONISED_REQUIRED,
        uncertainty_rule=UncertaintyRule(
            model="bounded_absolute",
            parameter=q(0.1, source="uncertainty", unit="Mbps"),
        ),
    )
    target = TargetContract(
        contract_id="target-1",
        version="1.0.0",
        schema_version="assurance/1.0.0",
        document_status="NORMATIVE",
        standard_mapping={},
        objective_family="test-objective",
        scope_selector={"cellId": "cell-1"},
        predicates=(
            TargetPredicate(
                predicate_id="mandatory-1",
                constraint=TypedConstraint(
                    measurement_ref="measurement-1",
                    operator=ComparisonOperator.GREATER_OR_EQUAL,
                    bound=threshold,
                ),
            ),
        ),
        options=(),
        hold_ms=0,
    )
    return target, measurement


def bind_contracts(kernel, frozen_contracts=None) -> None:
    for contract in frozen_contracts or contracts():
        kernel.record_frozen_contract(
            contract,
            contract_hash=content_hash(plain(contract)),
            now=NOW,
        )


def make_evaluator_kernel(frozen_contracts=None):
    frozen_contracts = frozen_contracts or contracts()
    target, measurement = frozen_contracts
    record = epoch(
        target_contract_hashes={target.contract_id: content_hash(plain(target))},
        measurement_contract_hashes={
            measurement.contract_id: content_hash(plain(measurement))
        },
    )
    kernel, store = make_kernel(frozen_epoch=record)
    bind_contracts(kernel, frozen_contracts)
    return kernel, store


def sample(
    sample_id: str,
    observed_at: str,
    sequence: int,
    *,
    gaps: tuple[MissingInterval, ...] = (),
    value: float = 10,
    unit: str = "Mbps",
    cadence_ms: int = 1000,
    ue_id: str = "ue-1",
) -> RawSample:
    return RawSample(
        sample_id=sample_id,
        counter_id="counter-1",
        value=TypedQuantity(
            value=value,
            unit=unit,
            provenance=Provenance.MEASURED,
            source_record=f"raw-{sample_id}",
        ),
        scope_snapshot={"cellId": "cell-1", "ueId": ue_id},
        observed_at=observed_at,
        cadence_ms=cadence_ms,
        clock_health=ClockHealth.SYNCHRONISED,
        trace_hash=content_hash({"trace": sample_id}),
        sequence=sequence,
        missing_intervals=gaps,
    )


def applied_trial(kernel, *, case_id: str = "case-1") -> str:
    trial_id = kernel.open_trial(
        candidate_id="candidate-1", case_id=case_id, now=NOW
    )
    advance_to_ready(kernel, trial_id)
    kernel.record_commit_readiness(
        trial_id,
        watchdogs_armed=True,
        baseline_hash=content_hash({"baseline": 1}),
        now=NOW,
    )
    kernel.advance_trial(trial_id, TrialState.COMMIT_DECIDED, now=NOW)
    acknowledge_commit(kernel, trial_id, now=T1)
    kernel.advance_trial(trial_id, TrialState.APPLYING, now=T1)
    kernel.advance_trial(trial_id, TrialState.APPLIED_PENDING_RESULT, now=T1)
    kernel.advance_trial(trial_id, TrialState.SETTLING, now=T1)
    kernel.advance_trial(trial_id, TrialState.OBSERVING, now=T2)
    kernel.advance_trial(trial_id, TrialState.DECISION_HOLD, now=T3)
    return trial_id


class RawEvaluatorTests(unittest.TestCase):
    def test_contract_not_named_by_active_epoch_is_refused(self) -> None:
        kernel, _ = make_kernel()
        target, _ = contracts()

        with self.assertRaises(KernelRefusal) as refusal:
            kernel.record_frozen_contract(
                target, contract_hash=content_hash(plain(target)), now=NOW
            )

        self.assertEqual(refusal.exception.reason, "CONTRACT_NOT_FROZEN_IN_EPOCH")

    def test_ingesting_a_sample_does_not_copy_the_whole_state(self) -> None:
        # 2026-09-15 attempt 40: a per-sample deepcopy grew with the stream and
        # left a live 12-counter trial 20-33 s behind its own telemetry.
        kernel, _ = make_evaluator_kernel()
        applied_trial(kernel)
        kernel.ingest_raw_sample(sample("sample-1", T2, 0), now=T2)
        with patch("assurance.kernel.kernel.deepcopy",
                   side_effect=AssertionError("state copied per sample")):
            kernel.ingest_raw_sample(sample("sample-2", T3, 1), now=T3)
        with self.assertRaises(KernelRefusal) as stale:
            kernel.ingest_raw_sample(sample("sample-3", T3, 1), now=T3)
        self.assertEqual(stale.exception.reason, "STALE_RAW_SAMPLE")

    def test_raw_samples_produce_separate_valid_sufficient_pass_axes(self) -> None:
        kernel, _ = make_evaluator_kernel()
        trial_id = applied_trial(kernel)
        kernel.ingest_raw_sample(sample("sample-1", T2, 0), now=T2)
        kernel.ingest_raw_sample(sample("sample-2", T3, 1), now=T3)

        validity, sufficiency, verdicts = kernel.evaluate_trial(trial_id, now=T3)

        self.assertEqual(validity, ExecutionValidity.VALID)
        self.assertEqual(sufficiency, MeasurementSufficiency.SUFFICIENT)
        self.assertEqual(verdicts, {"mandatory-1": PredicateVerdict.PASS})

    def test_normally_admitted_contracts_are_resolved_by_frozen_epoch_hash(self) -> None:
        kernel, _ = make_kernel()
        target, measurement = contracts()
        with patch(
            "assurance.kernel.kernel.validate_contract", return_value=None
        ), patch(
            "assurance.kernel.kernel.contract_content_hash",
            side_effect=lambda item: content_hash(plain(item)),
        ), patch(
            "assurance.kernel.kernel.canonical_form", side_effect=plain
        ):
            target_event = kernel.admit_contract(
                target, confirmation=None, now=NOW
            )
            measurement_event = kernel.admit_contract(
                measurement, confirmation=None, now=NOW
            )
        second_catalog = catalog("epoch-2")
        kernel.activate_frozen_epoch(
            epoch(
                "epoch-2",
                catalog_digest=second_catalog.catalog_hash,
                target_contract_hashes={
                    target.contract_id: target_event.payload["contractHash"]
                },
                measurement_contract_hashes={
                    measurement.contract_id: measurement_event.payload[
                        "contractHash"
                    ]
                },
            ),
            second_catalog,
            now=NOW,
        )
        kernel.open_case(
            case_id="case-2",
            policy=policy(),
            active_vector="vector-1",
            usable_reserve={"harm-1": q(10).to_canonical_dict()},
            reserve_per_trial={"harm-1": q(4).to_canonical_dict()},
            evidence_cells=(),
            now=NOW,
        )
        trial_id = applied_trial(kernel, case_id="case-2")
        kernel.ingest_raw_sample(sample("sample-1", T2, 0), now=T2)
        kernel.ingest_raw_sample(sample("sample-2", T3, 1), now=T3)

        validity, sufficiency, verdicts = kernel.evaluate_trial(
            trial_id, now=T3
        )

        self.assertEqual(validity, ExecutionValidity.VALID)
        self.assertEqual(sufficiency, MeasurementSufficiency.SUFFICIENT)
        self.assertEqual(verdicts["mandatory-1"], PredicateVerdict.PASS)

    def test_every_completed_window_in_hold_must_pass(self) -> None:
        target, measurement = contracts()
        target = replace(target, hold_ms=2000)
        measurement = replace(measurement, hold_ms=2000)
        kernel, _ = make_evaluator_kernel((target, measurement))
        trial_id = applied_trial(kernel)
        kernel.ingest_raw_sample(
            sample("sample-1", T2, 0, value=0), now=T2
        )
        kernel.ingest_raw_sample(
            sample("sample-2", T3, 1, value=10), now=T3
        )
        kernel.ingest_raw_sample(
            sample("sample-3", T4, 2, value=10), now=T4
        )

        _, sufficiency, verdicts = kernel.evaluate_trial(trial_id, now=T4)

        self.assertEqual(sufficiency, MeasurementSufficiency.SUFFICIENT)
        self.assertEqual(verdicts["mandatory-1"], PredicateVerdict.FAIL)

    def test_a_closed_window_is_judged_fresh_against_its_own_end(self) -> None:
        """A hold longer than the freshness bound is not by itself stale.

        Live OTA 2026-09-12: the joint contracts hold for 6000 ms with a 2000 ms
        freshness bound, and every completed window came back ``STALE`` even
        though the collector delivered an exact 1 Hz stream with no gaps.  The
        bound was compared against the *evaluation* clock, so a window that had
        already closed was stale by construction as soon as the hold outran the
        bound -- no radio fault could be told apart from a healthy one.
        Freshness asks whether telemetry is current; for a window that is
        already closed the answerable question is whether the window itself was
        covered, which the cadence and coverage checks below already decide.
        The test above keeps the other direction honest: it passes today only
        because its bound (5000 ms) outlasts its hold (2000 ms).
        """
        target, measurement = contracts()
        target = replace(target, hold_ms=2000)
        measurement = replace(measurement, hold_ms=2000, freshness_bound_ms=500)
        kernel, _ = make_evaluator_kernel((target, measurement))
        trial_id = applied_trial(kernel)
        for index, observed_at in enumerate((T2, T3, T4)):
            kernel.ingest_raw_sample(
                sample(f"sample-{index + 1}", observed_at, index), now=observed_at
            )

        validity, sufficiency, verdicts = kernel.evaluate_trial(trial_id, now=T4)

        self.assertEqual(validity, ExecutionValidity.VALID)
        self.assertEqual(sufficiency, MeasurementSufficiency.SUFFICIENT)
        self.assertEqual(verdicts["mandatory-1"], PredicateVerdict.PASS)

    def test_hold_clock_starts_with_observation_not_apply(self) -> None:
        target, measurement = contracts()
        target = replace(target, hold_ms=2000)
        measurement = replace(measurement, hold_ms=2000)
        kernel, _ = make_evaluator_kernel((target, measurement))
        trial_id = applied_trial(kernel)
        kernel.ingest_raw_sample(sample("sample-1", T2, 0), now=T2)
        kernel.ingest_raw_sample(sample("sample-2", T3, 1), now=T3)

        kernel.evaluate_trial(trial_id, now=T3)

        self.assertFalse(
            kernel.reduced_state()["trials"][trial_id]["evaluation"][
                "holdComplete"
            ]
        )

    def test_missing_interval_is_indeterminate_and_takes_contract_charge(self) -> None:
        kernel, _ = make_evaluator_kernel()
        trial_id = applied_trial(kernel)
        gap = MissingInterval(start=T2, end=T3, reason="collector restart")
        kernel.ingest_raw_sample(sample("sample-gap", T3, 0, gaps=(gap,)), now=T3)

        validity, sufficiency, verdicts = kernel.evaluate_trial(trial_id, now=T3)

        self.assertEqual(validity, ExecutionValidity.VALID)
        self.assertEqual(sufficiency, MeasurementSufficiency.MISSING_INTERVAL)
        self.assertEqual(verdicts, {"mandatory-1": PredicateVerdict.INDETERMINATE})
        missing_charges = [
            entry
            for entries in kernel.reduced_state()["pendingHarmCharges"].values()
            for entry in entries
            if entry.get("chargedForMissingInterval")
        ]
        self.assertEqual(len(missing_charges), 1)
        self.assertGreater(missing_charges[0]["amount"]["value"], 0)

    def test_cadence_and_frozen_membership_are_enforced(self) -> None:
        for bad_sample in (
            sample("bad-cadence", T2, 0, cadence_ms=500),
            sample("outsider", T2, 0, ue_id="ue-outside-epoch"),
        ):
            with self.subTest(sample_id=bad_sample.sample_id):
                kernel, _ = make_evaluator_kernel()
                trial_id = applied_trial(kernel)
                kernel.ingest_raw_sample(bad_sample, now=T2)
                kernel.ingest_raw_sample(
                    sample(
                        f"{bad_sample.sample_id}-2",
                        T3,
                        1,
                        cadence_ms=bad_sample.cadence_ms,
                        ue_id=bad_sample.scope_snapshot["ueId"],
                    ),
                    now=T3,
                )

                _, sufficiency, verdicts = kernel.evaluate_trial(trial_id, now=T3)

                self.assertIn(
                    sufficiency,
                    {
                        MeasurementSufficiency.INSUFFICIENT_COVERAGE,
                        MeasurementSufficiency.SCOPE_MISMATCH,
                    },
                )
                self.assertEqual(
                    verdicts["mandatory-1"], PredicateVerdict.INDETERMINATE
                )

    def test_uncertainty_margin_is_applied_in_conservative_direction(self) -> None:
        kernel, _ = make_evaluator_kernel()
        trial_id = applied_trial(kernel)
        kernel.ingest_raw_sample(sample("sample-1", T2, 0, value=5.05), now=T2)
        kernel.ingest_raw_sample(sample("sample-2", T3, 1, value=5.05), now=T3)

        _, sufficiency, verdicts = kernel.evaluate_trial(trial_id, now=T3)

        self.assertEqual(sufficiency, MeasurementSufficiency.SUFFICIENT)
        self.assertEqual(verdicts["mandatory-1"], PredicateVerdict.FAIL)

    def test_measurement_and_bound_units_must_match(self) -> None:
        kernel, _ = make_evaluator_kernel()
        trial_id = applied_trial(kernel)
        kernel.ingest_raw_sample(sample("sample-1", T2, 0, unit="ms"), now=T2)
        kernel.ingest_raw_sample(sample("sample-2", T3, 1, unit="ms"), now=T3)

        with self.assertRaisesRegex(KernelRefusal, "MEASUREMENT_UNIT_MISMATCH"):
            kernel.evaluate_trial(trial_id, now=T3)


if __name__ == "__main__":
    unittest.main()
