"""Trial, settlement, budget, fencing, and recovery tests for lane KERN."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
import unittest

from assurance.contracts.catalog import Candidate, CandidateCatalog, CoordinationCasePolicy
from assurance.contracts.epoch import EpochRecord
from assurance.contracts.harm import (
    HarmContract,
    HarmKind,
    WatchdogAction,
    WatchdogContract,
)
from assurance.contracts.target import (
    ComparisonOperator,
    TypedConstraint,
)
from assurance.core.addressing import content_hash
from assurance.core.axes import (
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
    TrialOutcome,
)
from assurance.core.components import ComponentId
from assurance.core.confirmation import ConfirmationAction, ConfirmationRecord
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION, EventEnvelope
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.core.states import outranks_semantic_verdict, StopReason, TrialState
from assurance.gateway.token import KernelToken, TokenKind
from assurance.gateway.write_gateway import GatewayOutcome, GatewayResult
from assurance.kernel.event_store import MemoryEventStore
from assurance.kernel.kernel import AssuranceKernel, KernelRefusal
from assurance.kernel.reducer import KernelReducer, replay


NOW = "2026-08-21T00:00:00.000000Z"
T1 = "2026-08-21T00:00:01.000000Z"
T2 = "2026-08-21T00:00:02.000000Z"
T3 = "2026-08-21T00:00:03.000000Z"


def q(
    value: float, *, source: str = "contract-harm-1", unit: str = "ms"
) -> TypedQuantity:
    return TypedQuantity(
        value=value,
        unit=unit,
        provenance=Provenance.EXPERIMENT_CONFIG,
        source_record=source,
    )


def plain(value):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: plain(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def harm_contract(
    contract_id: str = "harm-1", *, reserve_value: float = 10, unit: str = "ms"
) -> HarmContract:
    watchdog = WatchdogContract(
        contract_id=f"watchdog-contract-{contract_id}",
        version="1.0.0",
        schema_version="assurance/1.0.0",
        document_status="NORMATIVE",
        standard_mapping={},
        watchdog_id=f"watchdog-{contract_id}",
        trigger=TypedConstraint(
            measurement_ref="harm-measurement-1",
            operator=ComparisonOperator.LESS_OR_EQUAL,
            bound=q(1, source=f"watchdog-bound-{contract_id}", unit=unit),
        ),
        action=WatchdogAction.STOP_AND_ROLLBACK,
    )
    return HarmContract(
        contract_id=contract_id,
        version="1.0.0",
        schema_version="assurance/1.0.0",
        document_status="NORMATIVE",
        standard_mapping={},
        harm_kind=HarmKind.TRIAL_INDUCED,
        scope_selector={"cellId": "cell-1"},
        reserve=q(
            reserve_value, source=f"reserve-{contract_id}", unit=unit
        ),
        bounds=(),
        watchdogs=(watchdog,),
        missing_interval_charge=q(
            1, source=f"missing-charge-{contract_id}", unit=unit
        ),
    )


def epoch(
    epoch_id: str = "epoch-1",
    *,
    catalog_digest: str | None = None,
    target_contract_hashes: dict[str, str] | None = None,
    measurement_contract_hashes: dict[str, str] | None = None,
    harm_contract_hashes: dict[str, str] | None = None,
) -> EpochRecord:
    digest = catalog_digest or content_hash({"catalog": epoch_id})
    return EpochRecord(
        contract_id=f"epoch/{epoch_id}",
        version="1.0.0",
        schema_version="assurance/1.0.0",
        document_status="NORMATIVE",
        standard_mapping={},
        epoch_id=epoch_id,
        frozen_at=NOW,
        target_contract_hashes=target_contract_hashes or {},
        harm_contract_hashes=harm_contract_hashes
        or {"harm-1": content_hash(plain(harm_contract()))},
        measurement_contract_hashes=measurement_contract_hashes or {},
        capability_manifest_hashes={},
        composition_manifest_hash=content_hash({"composition": 1}),
        target_vector_hash=content_hash({"vectors": ["vector-1"]}),
        target_vector_order=("vector-1",),
        case_policy_hash=content_hash({"case-policy": 1}),
        deployment_binding_hashes={},
        counter_binding_hashes={},
        actuator_binding_hashes={},
        candidate_generator_version="test-generator/1",
        candidate_universe_cardinality=2,
        candidate_semantic_hashes=("a" * 64, "b" * 64),
        catalog_hash=digest,
        evaluator_version="test-evaluator/1",
        reducer_version=KernelReducer.reducer_version,
    )


def catalog(
    epoch_id: str = "epoch-1", *, shared_resource: bool = False
) -> CandidateCatalog:
    candidates = (
        Candidate(
            candidate_id="candidate-1",
            target_ref="target-1",
            option_ref="option-1",
            parameters={"baseline": "1"},
            semantic_hash="a" * 64,
            capability_ref="resource-1",
        ),
        Candidate(
            candidate_id="candidate-2",
            target_ref="target-1",
            option_ref="option-1",
            parameters={"baseline": "2"},
            semantic_hash="b" * 64,
            capability_ref="resource-1" if shared_resource else "resource-2",
        ),
    )
    return CandidateCatalog(
        contract_id=f"catalog/{epoch_id}",
        version="1.0.0",
        schema_version="assurance/1.0.0",
        document_status="NORMATIVE",
        standard_mapping={},
        generator_version="test-generator/1",
        cardinality=2,
        candidates=candidates,
        catalog_hash=content_hash({"catalog": epoch_id}),
        epoch_ref=epoch_id,
    )


def policy(
    harm_contract_refs: tuple[str, ...] = ("harm-1",),
) -> CoordinationCasePolicy:
    return CoordinationCasePolicy(
        contract_id="case-policy-1",
        version="1.0.0",
        schema_version="assurance/1.0.0",
        document_status="NORMATIVE",
        standard_mapping={},
        deadline_ms=60_000,
        max_trials=4,
        max_proposals=8,
        target_release_policy_ref="release-policy-1",
        harm_contract_refs=harm_contract_refs,
    )


def make_kernel(
    *, gateway=None, frozen_epoch: EpochRecord | None = None
) -> tuple[AssuranceKernel, MemoryEventStore]:
    store = MemoryEventStore()
    kernel = AssuranceKernel(
        event_store=store,
        reducer=KernelReducer(),
        write_gateway=gateway,
        measurement_collector=None,
    )
    frozen_catalog = catalog()
    epoch_record = frozen_epoch or epoch(catalog_digest=frozen_catalog.catalog_hash)
    kernel.activate_frozen_epoch(epoch_record, frozen_catalog, now=NOW)
    frozen_harm = harm_contract()
    kernel.record_frozen_contract(
        frozen_harm,
        contract_hash=content_hash(plain(frozen_harm)),
        now=NOW,
    )
    kernel.open_case(
        case_id="case-1",
        policy=policy(),
        active_vector="vector-1",
        usable_reserve={"harm-1": q(10).to_canonical_dict()},
        reserve_per_trial={"harm-1": q(4).to_canonical_dict()},
        evidence_cells=(),
        now=NOW,
    )
    return kernel, store


def make_multi_harm_kernel() -> tuple[AssuranceKernel, MemoryEventStore]:
    store = MemoryEventStore()
    kernel = AssuranceKernel(
        event_store=store,
        reducer=KernelReducer(),
        write_gateway=None,
        measurement_collector=None,
    )
    frozen_catalog = catalog()
    first_harm = harm_contract()
    second_harm = harm_contract(
        "harm-2", reserve_value=100, unit="count"
    )
    kernel.activate_frozen_epoch(
        epoch(
            catalog_digest=frozen_catalog.catalog_hash,
            harm_contract_hashes={
                "harm-1": content_hash(plain(first_harm)),
                "harm-2": content_hash(plain(second_harm)),
            },
        ),
        frozen_catalog,
        now=NOW,
    )
    for frozen_harm in (first_harm, second_harm):
        kernel.record_frozen_contract(
            frozen_harm,
            contract_hash=content_hash(plain(frozen_harm)),
            now=NOW,
        )
    kernel.open_case(
        case_id="case-1",
        policy=policy(("harm-1", "harm-2")),
        active_vector="vector-1",
        usable_reserve={
            "harm-1": q(10).to_canonical_dict(),
            "harm-2": q(100, unit="count").to_canonical_dict(),
        },
        reserve_per_trial={
            "harm-1": q(4).to_canonical_dict(),
            "harm-2": q(20, unit="count").to_canonical_dict(),
        },
        evidence_cells=(),
        now=NOW,
    )
    return kernel, store


class RecoveryGateway:
    def __init__(self, query_outcome: GatewayOutcome) -> None:
        self.query_outcome = query_outcome
        self.calls: list[str] = []

    def query_transaction(self, transaction_id: str) -> GatewayResult:
        self.calls.append("query")
        return GatewayResult(self.query_outcome, observed_config_hash="a" * 64)

    def reverse_rollback(self, *, token: KernelToken) -> GatewayResult:
        self.calls.append("rollback")
        return GatewayResult(GatewayOutcome.ACKED)

    def reread_configuration(self, *, token: KernelToken) -> GatewayResult:
        self.calls.append("reread")
        return GatewayResult(
            GatewayOutcome.ACKED, observed_config_hash=token.expected_config_hash
        )

    def confirm_recovery(self, *, token: KernelToken) -> GatewayResult:
        self.calls.append("confirm")
        return GatewayResult(GatewayOutcome.ACKED)


def watchdog_arming_evidence(
    kernel: AssuranceKernel, trial_id: str
) -> tuple[str, ...]:
    state = kernel.reduced_state()
    trial = state["trials"][trial_id]
    case = state["cases"][trial["caseId"]]
    epoch_record = state["epochs"][trial["epochId"]]
    watchdog_ids = []
    for harm_ref in case["harmContractRefs"]:
        contract_hash = epoch_record["harmContractHashes"][harm_ref]
        for watchdog in state["contracts"][contract_hash]["body"]["watchdogs"]:
            watchdog_ids.append(watchdog["watchdog_id"])
    return tuple(f"watchdog:{identifier}:armed" for identifier in watchdog_ids)


#: The fixture deployment's configuration surface.  Its digest is
#: ``content_hash({"baseline": 1})`` -- the value this file already used as
#: the baseline everywhere -- because ``config_hash`` is ``content_hash`` over
#: the axis mapping.
BASELINE_CONFIG = {"baseline": 1}


def staged_plan(kernel: AssuranceKernel, trial_id: str) -> dict:
    """The actuation plan realising this trial's frozen candidate."""
    state = kernel.reduced_state()
    trial = state["trials"][trial_id]
    candidate = next(
        item
        for item in state["catalog"]["candidates"]
        if item["candidateId"] == trial["candidateId"]
    )
    return {
        "adapter": "mock",
        "scope": {"ueId": "ue-1"},
        "baselineConfig": dict(BASELINE_CONFIG),
        "steps": [
            {"axis": axis, "value": value}
            for axis, value in sorted(candidate["parameters"].items())
        ],
        "watchdogs": [
            watchdog.split(":")[1]
            for watchdog in watchdog_arming_evidence(kernel, trial_id)
        ],
    }


def stage_plan(kernel: AssuranceKernel, trial_id: str) -> None:
    kernel.stage_actuation_plan(trial_id, plan=staged_plan(kernel, trial_id), now=NOW)


def advance_to_ready(kernel: AssuranceKernel, trial_id: str) -> None:
    kernel.advance_trial(trial_id, TrialState.VALIDATING, now=NOW)
    kernel.reserve(trial_id, now=NOW)
    kernel.advance_trial(trial_id, TrialState.RESERVED, now=NOW)
    stage_plan(kernel, trial_id)
    kernel.advance_trial(trial_id, TrialState.PREPARING, now=NOW)
    ready = kernel.issue_token(trial_id, token_kind=TokenKind.READY, now=NOW)
    resource_id = kernel.reduced_state()["trials"][trial_id]["resourceId"]
    kernel.record_gateway_result(
        ready,
        GatewayResult(
            GatewayOutcome.ACKED,
            observed_config_hash=content_hash({"baseline": 1}),
            evidence_refs=watchdog_arming_evidence(kernel, trial_id),
        ),
        resource_id=resource_id,
        now=NOW,
    )
    kernel.advance_trial(trial_id, TrialState.READY, now=NOW)


def acknowledge_commit(
    kernel: AssuranceKernel, trial_id: str, *, now: str = T1
) -> None:
    token = kernel.issue_token(trial_id, token_kind=TokenKind.COMMIT, now=now)
    resource_id = kernel.reduced_state()["trials"][trial_id]["resourceId"]
    kernel.record_gateway_result(
        token,
        GatewayResult(
            GatewayOutcome.ACKED,
            observed_config_hash=kernel.reduced_state()["trials"][trial_id][
                "planAppliedHash"
            ],
        ),
        resource_id=resource_id,
        now=now,
    )


def advance_to_decision_hold(kernel: AssuranceKernel, trial_id: str) -> None:
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


def record_evaluation(
    kernel: AssuranceKernel,
    trial_id: str,
    *,
    execution_validity: ExecutionValidity = ExecutionValidity.VALID,
    measurement_sufficiency: MeasurementSufficiency = MeasurementSufficiency.SUFFICIENT,
    predicate_verdicts: dict[str, PredicateVerdict] | None = None,
    hold_complete: bool = True,
    same_candidate: bool = True,
    validity_region_stable: bool = True,
) -> None:
    """Seed the Kernel-owned evaluator result used by lifecycle tests."""
    verdicts = predicate_verdicts or {"mandatory-1": PredicateVerdict.PASS}
    kernel._append(
        "TrialEvaluated",
        object_id=trial_id,
        now=T3,
        payload={
            "executionValidity": execution_validity.value,
            "holdComplete": hold_complete,
            "mandatoryPredicateIds": sorted(verdicts),
            "measurementSufficiency": measurement_sufficiency.value,
            "predicateVerdicts": {
                key: value.value for key, value in sorted(verdicts.items())
            },
            "sameCandidate": same_candidate,
            "stopReason": None,
            "traceRefs": [],
            "trialId": trial_id,
            "validityRegionStable": validity_region_stable,
        },
    )


class TrialLifecycleTests(unittest.TestCase):
    def test_reserved_requires_durable_harm_reservation(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        kernel.advance_trial(trial_id, TrialState.VALIDATING, now=NOW)

        with self.assertRaises(KernelRefusal) as refusal:
            kernel.advance_trial(trial_id, TrialState.RESERVED, now=NOW)

        self.assertEqual(refusal.exception.reason, "HARM_NOT_RESERVED")

    def test_ready_requires_durable_gateway_ack(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        kernel.advance_trial(trial_id, TrialState.VALIDATING, now=NOW)
        kernel.reserve(trial_id, now=NOW)
        kernel.advance_trial(trial_id, TrialState.RESERVED, now=NOW)
        kernel.advance_trial(trial_id, TrialState.PREPARING, now=NOW)

        with self.assertRaises(KernelRefusal) as refusal:
            kernel.advance_trial(trial_id, TrialState.READY, now=NOW)

        self.assertEqual(refusal.exception.reason, "COMPONENTS_NOT_READY")

    def test_commit_token_is_refused_before_durable_commit_decision(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )

        with self.assertRaises(KernelRefusal) as refusal:
            kernel.issue_token(trial_id, token_kind=TokenKind.COMMIT, now=NOW)

        self.assertEqual(refusal.exception.reason, "TOKEN_KIND_NOT_ALLOWED_IN_STATE")

    def test_apply_requires_durable_commit_readiness_and_counts_once(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(candidate_id="candidate-1", case_id="case-1", now=NOW)
        advance_to_ready(kernel, trial_id)

        with self.assertRaises(KernelRefusal) as missing_readiness:
            kernel.advance_trial(trial_id, TrialState.COMMIT_DECIDED, now=NOW)
        self.assertEqual(missing_readiness.exception.reason, "COMMIT_NOT_READY")

        kernel.record_commit_readiness(
            trial_id,
            watchdogs_armed=True,
            baseline_hash=content_hash({"baseline": 1}),
            now=NOW,
        )
        kernel.advance_trial(trial_id, TrialState.COMMIT_DECIDED, now=NOW)
        with self.assertRaises(KernelRefusal) as missing_commit_result:
            kernel.advance_trial(trial_id, TrialState.APPLYING, now=T1)
        self.assertEqual(
            missing_commit_result.exception.reason,
            "COMMIT_RESULT_NOT_ACKNOWLEDGED",
        )
        acknowledge_commit(kernel, trial_id, now=T1)
        kernel.advance_trial(trial_id, TrialState.APPLYING, now=T1)
        with self.assertRaises(KernelRefusal):
            kernel.advance_trial(trial_id, TrialState.APPLYING, now=T2)

        state = kernel.reduced_state()
        self.assertEqual(len(state["trialLedger"]), 1)
        self.assertEqual(state["trials"][trial_id]["harmClockStartedAt"], T1)

    def test_watchdog_boolean_cannot_replace_gateway_arming_evidence(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        kernel.advance_trial(trial_id, TrialState.VALIDATING, now=NOW)
        kernel.reserve(trial_id, now=NOW)
        kernel.advance_trial(trial_id, TrialState.RESERVED, now=NOW)
        stage_plan(kernel, trial_id)
        kernel.advance_trial(trial_id, TrialState.PREPARING, now=NOW)
        ready = kernel.issue_token(
            trial_id, token_kind=TokenKind.READY, now=NOW
        )
        kernel.record_gateway_result(
            ready,
            GatewayResult(
                GatewayOutcome.ACKED,
                observed_config_hash=content_hash({"baseline": 1}),
            ),
            resource_id="resource-1",
            now=NOW,
        )
        kernel.advance_trial(trial_id, TrialState.READY, now=NOW)

        with self.assertRaises(KernelRefusal) as refusal:
            kernel.record_commit_readiness(
                trial_id,
                watchdogs_armed=True,
                baseline_hash=content_hash({"baseline": 1}),
                now=NOW,
            )

        self.assertEqual(
            refusal.exception.reason, "WATCHDOG_ARMING_NOT_OBSERVED"
        )

    def test_pause_blocks_only_new_scheduling_at_safe_boundary(self) -> None:
        kernel, _ = make_kernel()
        active = kernel.open_trial(candidate_id="candidate-1", case_id="case-1", now=NOW)
        kernel.pause_case("case-1", paused=True, now=NOW)

        kernel.advance_trial(active, TrialState.VALIDATING, now=NOW)
        with self.assertRaises(KernelRefusal) as blocked:
            kernel.open_trial(candidate_id="candidate-2", case_id="case-1", now=T1)
        self.assertEqual(blocked.exception.reason, "CASE_PAUSED")

    def test_safety_reason_wins_when_kpi_success_arrives_simultaneously(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(candidate_id="candidate-1", case_id="case-1", now=NOW)
        advance_to_decision_hold(kernel, trial_id)
        record_evaluation(kernel, trial_id)

        decided = kernel.decide_trial(
            trial_id,
            stop_reasons=(StopReason.HARM_LIMIT_BREACH,),
            now=T3,
        )

        self.assertEqual(decided, TrialState.STOPPING)
        trial = kernel.reduced_state()["trials"][trial_id]
        self.assertEqual(trial["evaluation"]["predicateVerdicts"], {"mandatory-1": "PASS"})
        self.assertEqual(trial["stopReason"], StopReason.HARM_LIMIT_BREACH.value)

    def test_a_baseline_reset_rolls_a_passing_trial_back(self) -> None:
        """Owner decision 2026-09-20: judged on C0, returned to C0."""
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(candidate_id="candidate-1", case_id="case-1", now=NOW)
        advance_to_decision_hold(kernel, trial_id)
        record_evaluation(kernel, trial_id)
        decided = kernel.decide_trial(
            trial_id, stop_reasons=(StopReason.BASELINE_RESET,), now=T3)
        self.assertEqual(TrialState.STOPPING, decided)
        trial = kernel.reduced_state()["trials"][trial_id]
        self.assertEqual(StopReason.BASELINE_RESET.value, trial["stopReason"])
        self.assertEqual({"mandatory-1": "PASS"}, trial["evaluation"]["predicateVerdicts"])
        self.assertFalse(trial.get("successDecisionDurable"))

    def test_a_baseline_reset_never_outranks_a_safety_reason_or_hides_a_failure(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(candidate_id="candidate-1", case_id="case-1", now=NOW)
        advance_to_decision_hold(kernel, trial_id)
        record_evaluation(kernel, trial_id)
        kernel.decide_trial(trial_id, stop_reasons=(StopReason.BASELINE_RESET,
                                                    StopReason.HARM_LIMIT_BREACH), now=T3)
        self.assertEqual(StopReason.HARM_LIMIT_BREACH.value,
                         kernel.reduced_state()["trials"][trial_id]["stopReason"])
        self.assertFalse(outranks_semantic_verdict(StopReason.BASELINE_RESET))

    def test_execution_error_axis_cannot_fall_through_as_semantic_failure(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        advance_to_decision_hold(kernel, trial_id)
        record_evaluation(
            kernel, trial_id, execution_validity=ExecutionValidity.EXEC_ERROR
        )

        kernel.decide_trial(
            trial_id,
            stop_reasons=(),
            now=T3,
        )

        self.assertEqual(
            kernel.reduced_state()["trials"][trial_id]["stopReason"],
            StopReason.EXECUTION_ERROR.value,
        )

    def test_success_settlement_requires_reread_and_finalize_ack(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(candidate_id="candidate-1", case_id="case-1", now=NOW)
        advance_to_decision_hold(kernel, trial_id)
        record_evaluation(kernel, trial_id)
        self.assertEqual(
            kernel.decide_trial(
                trial_id,
                stop_reasons=(),
                now=T3,
            ),
            TrialState.FINALIZING_LIVE,
        )

        with self.assertRaises(KernelRefusal) as not_finalized:
            kernel.advance_trial(trial_id, TrialState.SETTLEMENT, now=T3)
        self.assertEqual(not_finalized.exception.reason, "LIVE_FINALIZE_INCOMPLETE")

        live_hash = kernel.reduced_state()["trials"][trial_id]["planAppliedHash"]
        reread = kernel.issue_token(
            trial_id, token_kind=TokenKind.CONFIGURATION_REREAD, now=T3
        )
        kernel.record_gateway_result(
            reread,
            GatewayResult(GatewayOutcome.ACKED, observed_config_hash=live_hash),
            resource_id="resource-1",
            now=T3,
        )
        finalize = kernel.issue_token(
            trial_id, token_kind=TokenKind.FINALIZE_LIVE, now=T3
        )
        kernel.record_gateway_result(
            finalize,
            GatewayResult(GatewayOutcome.ACKED, observed_config_hash=live_hash),
            resource_id="resource-1",
            now=T3,
        )
        kernel.advance_trial(trial_id, TrialState.SETTLEMENT, now=T3)
        settled = kernel.settle_trial(trial_id, outcome=TrialOutcome.SUCCESS, now=T3)
        self.assertEqual(settled.event_kind, "TrialSettled")
        self.assertEqual(
            kernel.reduced_state()["trials"][trial_id]["state"],
            TrialState.SETTLED_SUCCESS.value,
        )
        second_catalog = catalog("epoch-2")
        with self.assertRaises(KernelRefusal) as drain:
            kernel.activate_frozen_epoch(
                epoch("epoch-2", catalog_digest=second_catalog.catalog_hash),
                second_catalog,
                now=T3,
            )
        self.assertEqual(drain.exception.reason, "EPOCH_DRAIN_REQUIRED")
        confirmation = ConfirmationRecord(
            confirmed_object_type="EpochRecord",
            confirmed_content_hash=content_hash({"epoch": 2}),
            event_id="confirm-epoch-2",
            timestamp=T3,
            action=ConfirmationAction.REVIEW_AND_CONFIRM,
        )
        with self.assertRaises(KernelRefusal) as normal_freeze_drain:
            kernel.freeze_epoch(confirmation=confirmation, now=T3)
        self.assertEqual(
            normal_freeze_drain.exception.reason, "EPOCH_DRAIN_REQUIRED"
        )

    def test_mismatched_live_reread_cannot_enable_success_settlement(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        advance_to_decision_hold(kernel, trial_id)
        record_evaluation(kernel, trial_id)
        kernel.decide_trial(
            trial_id,
            stop_reasons=(),
            now=T3,
        )
        reread = kernel.issue_token(
            trial_id, token_kind=TokenKind.CONFIGURATION_REREAD, now=T3
        )

        kernel.record_gateway_result(
            reread,
            GatewayResult(GatewayOutcome.ACKED, observed_config_hash="b" * 64),
            resource_id="resource-1",
            now=T3,
        )

        self.assertFalse(
            kernel.reduced_state()["trials"][trial_id]["configurationReread"]
        )


class SettlementBudgetRecoveryTests(unittest.TestCase):
    def test_resource_lock_conflict_is_refused_before_durable_append(self) -> None:
        store = MemoryEventStore()
        kernel = AssuranceKernel(
            event_store=store,
            reducer=KernelReducer(),
            write_gateway=None,
            measurement_collector=None,
        )
        frozen_catalog = catalog(shared_resource=True)
        kernel.activate_frozen_epoch(
            epoch(catalog_digest=frozen_catalog.catalog_hash),
            frozen_catalog,
            now=NOW,
        )
        frozen_harm = harm_contract()
        kernel.record_frozen_contract(
            frozen_harm,
            contract_hash=content_hash(plain(frozen_harm)),
            now=NOW,
        )
        for case_id in ("case-1", "case-2"):
            kernel.open_case(
                case_id=case_id,
                policy=policy(),
                active_vector="vector-1",
                usable_reserve={"harm-1": q(10).to_canonical_dict()},
                reserve_per_trial={"harm-1": q(4).to_canonical_dict()},
                evidence_cells=(),
                now=NOW,
            )
        first = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        kernel.advance_trial(first, TrialState.VALIDATING, now=NOW)
        kernel.reserve(first, now=NOW)
        second = kernel.open_trial(
            candidate_id="candidate-2", case_id="case-2", now=NOW
        )
        kernel.advance_trial(second, TrialState.VALIDATING, now=NOW)
        position = store.last_position()

        with self.assertRaises(KernelRefusal) as refusal:
            kernel.reserve(second, now=NOW)

        self.assertEqual(refusal.exception.reason, "RESOURCE_LOCKED")
        self.assertEqual(store.last_position(), position)
        kernel.reduced_state()

    def test_multi_contract_reserve_and_charge_preserve_units_and_buckets(self) -> None:
        kernel, _ = make_multi_harm_kernel()
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        kernel.advance_trial(trial_id, TrialState.VALIDATING, now=NOW)
        kernel.reserve(trial_id, now=NOW)
        reservations = [
            entry
            for entry in kernel.reduced_state()["harmLedger"]
            if entry["movementKind"] == "RESERVE"
        ]

        self.assertEqual(
            {(entry["harmContractRef"], entry["amount"]["unit"]) for entry in reservations},
            {("harm-1", "ms"), ("harm-2", "count")},
        )

        kernel.advance_trial(trial_id, TrialState.RESERVED, now=NOW)
        stage_plan(kernel, trial_id)
        kernel.advance_trial(trial_id, TrialState.PREPARING, now=NOW)
        ready = kernel.issue_token(trial_id, token_kind=TokenKind.READY, now=NOW)
        kernel.record_gateway_result(
            ready,
            GatewayResult(
                GatewayOutcome.ACKED,
                observed_config_hash=content_hash({"baseline": 1}),
                evidence_refs=watchdog_arming_evidence(kernel, trial_id),
            ),
            resource_id="resource-1",
            now=NOW,
        )
        kernel.advance_trial(trial_id, TrialState.READY, now=NOW)
        kernel.record_commit_readiness(
            trial_id,
            watchdogs_armed=True,
            baseline_hash=content_hash({"baseline": 1}),
            now=NOW,
        )
        kernel.advance_trial(trial_id, TrialState.COMMIT_DECIDED, now=NOW)
        acknowledge_commit(kernel, trial_id, now=T1)
        kernel.advance_trial(trial_id, TrialState.APPLYING, now=T1)
        first = kernel.charge_harm(
            trial_id,
            amount=q(2, source="observed-ms"),
            harm_kind=HarmKind.TRIAL_INDUCED,
            for_missing_interval=False,
            now=T2,
        )
        second = kernel.charge_harm(
            trial_id,
            amount=q(5, source="observed-count", unit="count"),
            harm_kind=HarmKind.TRIAL_INDUCED,
            for_missing_interval=False,
            now=T2,
        )
        self.assertEqual(first.payload["harmContractRef"], "harm-1")
        self.assertEqual(second.payload["harmContractRef"], "harm-2")

        with self.assertRaises(KernelRefusal) as refusal:
            kernel.charge_harm(
                trial_id,
                amount=q(1, source="target-debt"),
                harm_kind=HarmKind.TARGET_DEBT,
                for_missing_interval=False,
                now=T2,
            )
        self.assertEqual(refusal.exception.reason, "HARM_BUCKET_NOT_RESERVED")

    def test_settlement_is_one_idempotent_event(self) -> None:
        kernel, store = make_kernel()
        trial_id = kernel.open_trial(candidate_id="candidate-1", case_id="case-1", now=NOW)
        kernel.advance_trial(trial_id, TrialState.PRE_COMMIT_ABORT, now=NOW)
        kernel.advance_trial(trial_id, TrialState.SETTLEMENT, now=NOW)

        first = kernel.settle_trial(
            trial_id, outcome=TrialOutcome.OPERATOR_ABORTED, now=NOW
        )
        position = store.last_position()
        first_hash = kernel.terminal_state_hash()
        second = kernel.settle_trial(
            trial_id, outcome=TrialOutcome.OPERATOR_ABORTED, now=T1
        )

        self.assertEqual(second, first)
        self.assertEqual(store.last_position(), position)
        self.assertEqual(kernel.terminal_state_hash(), first_hash)

    def test_epoch_transition_does_not_reset_trial_count_or_harm_charge(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(candidate_id="candidate-1", case_id="case-1", now=NOW)
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
        kernel.charge_harm(
            trial_id,
            amount=q(2, source="measured-harm-1"),
            harm_kind=HarmKind.TRIAL_INDUCED,
            for_missing_interval=False,
            now=T2,
        )
        before = kernel.reduced_state()

        second_catalog = catalog("epoch-2")
        kernel.activate_frozen_epoch(
            epoch("epoch-2", catalog_digest=second_catalog.catalog_hash),
            second_catalog,
            now=T3,
            safety_relevant=False,
        )
        after = kernel.reduced_state()

        self.assertEqual(after["trialLedger"], before["trialLedger"])
        self.assertEqual(after["harmLedger"], before["harmLedger"])
        self.assertEqual(
            after["pendingHarmCharges"], before["pendingHarmCharges"]
        )

    def test_staged_catalog_does_not_replace_active_epoch_catalog(self) -> None:
        kernel, _ = make_kernel()
        second = catalog("epoch-2")

        kernel._append(
            "CatalogFrozen",
            object_id=second.contract_id,
            now=T3,
            payload={
                "catalogHash": second.catalog_hash,
                "cardinality": second.cardinality,
                "candidates": [
                    {
                        "candidateId": candidate.candidate_id,
                        "targetRef": candidate.target_ref,
                        "optionRef": candidate.option_ref,
                        "parameters": dict(candidate.parameters),
                        "semanticHash": candidate.semantic_hash,
                        "capabilityRef": candidate.capability_ref,
                    }
                    for candidate in second.candidates
                ],
                "contractId": second.contract_id,
                "documentStatus": second.document_status,
                "epochRef": second.epoch_ref,
                "generatorVersion": second.generator_version,
                "schemaVersion": second.schema_version,
                "standardMapping": dict(second.standard_mapping),
                "version": second.version,
            },
        )

        self.assertEqual(kernel.reduced_state()["activeEpoch"], "epoch-1")
        self.assertEqual(kernel.current_catalog().epoch_ref, "epoch-1")

    def test_candidate_availability_is_scoped_to_epoch(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        kernel.advance_trial(trial_id, TrialState.PRE_COMMIT_ABORT, now=NOW)
        kernel.advance_trial(trial_id, TrialState.SETTLEMENT, now=NOW)
        kernel.settle_trial(
            trial_id, outcome=TrialOutcome.OPERATOR_ABORTED, now=NOW
        )
        self.assertEqual(
            kernel.reduced_state()["candidateAvailability"]["candidate-1"],
            "CONSUMED",
        )
        second_catalog = catalog("epoch-2")
        kernel.activate_frozen_epoch(
            epoch("epoch-2", catalog_digest=second_catalog.catalog_hash),
            second_catalog,
            now=T3,
        )

        self.assertEqual(
            kernel.reduced_state()["candidateAvailability"]["candidate-1"],
            "AVAILABLE",
        )

    def test_missing_interval_charge_cannot_be_zero(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(candidate_id="candidate-1", case_id="case-1", now=NOW)
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

        with self.assertRaises(KernelRefusal) as refusal:
            kernel.charge_harm(
                trial_id,
                amount=q(0),
                harm_kind=HarmKind.TRIAL_INDUCED,
                for_missing_interval=True,
                now=T2,
            )
        self.assertEqual(refusal.exception.reason, "NON_CONSERVATIVE_MISSING_CHARGE")

    def test_uncertain_transaction_blocks_new_trial_until_resolved(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(candidate_id="candidate-1", case_id="case-1", now=NOW)
        advance_to_ready(kernel, trial_id)
        kernel.record_commit_readiness(
            trial_id,
            watchdogs_armed=True,
            baseline_hash=content_hash({"baseline": 1}),
            now=NOW,
        )
        kernel.advance_trial(trial_id, TrialState.COMMIT_DECIDED, now=NOW)

        with self.assertRaises(KernelRefusal) as blocked:
            kernel.open_trial(candidate_id="candidate-2", case_id="case-1", now=T1)
        self.assertEqual(blocked.exception.reason, "RECOVERY_BLOCKED")

        kernel.advance_trial(trial_id, TrialState.RECOVERY_VERIFYING, now=T2)
        reread = kernel.issue_token(
            trial_id, token_kind=TokenKind.CONFIGURATION_REREAD, now=T2
        )
        kernel.record_gateway_result(
            reread,
            GatewayResult(
                GatewayOutcome.ACKED,
                observed_config_hash=content_hash({"baseline": 1}),
            ),
            resource_id="resource-1",
            now=T2,
        )
        confirmation = kernel.issue_token(
            trial_id, token_kind=TokenKind.RECOVERY_CONFIRM, now=T2
        )
        kernel.record_gateway_result(
            confirmation,
            GatewayResult(GatewayOutcome.ACKED, observed_config_hash="a" * 64),
            resource_id="resource-1",
            now=T2,
        )
        kernel.resolve_recovery("tx:case-1:trial:1", resolution="ROLLED_BACK", now=T2)
        kernel.advance_trial(trial_id, TrialState.SETTLEMENT, now=T2)
        kernel.settle_trial(
            trial_id, outcome=TrialOutcome.OPERATOR_ABORTED, now=T2
        )
        reopened = kernel.open_trial(candidate_id="candidate-2", case_id="case-1", now=T3)
        self.assertEqual(reopened, "case-1:trial:2")

    def test_recovery_resolution_requires_verified_known_state(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        advance_to_ready(kernel, trial_id)
        kernel.record_commit_readiness(
            trial_id,
            watchdogs_armed=True,
            baseline_hash=content_hash({"baseline": 1}),
            now=NOW,
        )
        kernel.advance_trial(trial_id, TrialState.COMMIT_DECIDED, now=NOW)

        with self.assertRaises(KernelRefusal) as refusal:
            kernel.resolve_recovery(
                f"tx:{trial_id}", resolution="ROLLED_BACK", now=T1
            )
        self.assertEqual(refusal.exception.reason, "RECOVERY_NOT_VERIFIED")
        with self.assertRaises(KernelRefusal) as blocked:
            kernel.open_trial(candidate_id="candidate-2", case_id="case-1", now=T1)
        self.assertEqual(blocked.exception.reason, "RECOVERY_BLOCKED")

    def test_restart_recovery_drives_reread_and_confirmation_before_resolution(self) -> None:
        gateway = RecoveryGateway(GatewayOutcome.REJECTED)
        kernel, _ = make_kernel(gateway=gateway)
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        advance_to_ready(kernel, trial_id)
        kernel.record_commit_readiness(
            trial_id,
            watchdogs_armed=True,
            baseline_hash=content_hash({"baseline": 1}),
            now=NOW,
        )
        kernel.advance_trial(trial_id, TrialState.COMMIT_DECIDED, now=NOW)

        self.assertEqual(kernel.recover(now=T2), ())

        self.assertEqual(gateway.calls, ["query", "reread", "confirm"])
        transaction = kernel.reduced_state()["transactions"][f"tx:{trial_id}"]
        self.assertTrue(transaction["resolved"])
        self.assertEqual(transaction["resolution"], "ABORTED")

    def test_restart_recovery_rolls_back_observed_applied_transaction(self) -> None:
        gateway = RecoveryGateway(GatewayOutcome.ACKED)
        kernel, _ = make_kernel(gateway=gateway)
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        advance_to_ready(kernel, trial_id)
        kernel.record_commit_readiness(
            trial_id,
            watchdogs_armed=True,
            baseline_hash=content_hash({"baseline": 1}),
            now=NOW,
        )
        kernel.advance_trial(trial_id, TrialState.COMMIT_DECIDED, now=NOW)

        self.assertEqual(kernel.recover(now=T2), ())

        # The reread before the rollback is the permit's expected
        # configuration being *observed* rather than assumed: a reverse
        # rollback names the live configuration, and after a restart the
        # Kernel has not read it since the transaction became uncertain.
        self.assertEqual(
            gateway.calls, ["query", "reread", "rollback", "reread", "confirm"]
        )
        transaction = kernel.reduced_state()["transactions"][f"tx:{trial_id}"]
        self.assertEqual(transaction["resolution"], "ROLLED_BACK")

    def test_restart_recovery_rereads_unknown_transaction_before_resolution(self) -> None:
        gateway = RecoveryGateway(GatewayOutcome.UNKNOWN)
        kernel, _ = make_kernel(gateway=gateway)
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        advance_to_ready(kernel, trial_id)
        kernel.record_commit_readiness(
            trial_id,
            watchdogs_armed=True,
            baseline_hash=content_hash({"baseline": 1}),
            now=NOW,
        )
        kernel.advance_trial(trial_id, TrialState.COMMIT_DECIDED, now=NOW)

        self.assertEqual(kernel.recover(now=T2), ())

        self.assertEqual(gateway.calls, ["query", "reread", "confirm"])
        self.assertEqual(
            kernel.reduced_state()["transactions"][f"tx:{trial_id}"]["resolution"],
            "ABORTED",
        )

    def test_restart_recovery_continues_from_stopping_phase(self) -> None:
        gateway = RecoveryGateway(GatewayOutcome.REJECTED)
        kernel, _ = make_kernel(gateway=gateway)
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        advance_to_ready(kernel, trial_id)
        kernel.record_commit_readiness(
            trial_id,
            watchdogs_armed=True,
            baseline_hash=content_hash({"baseline": 1}),
            now=NOW,
        )
        kernel.advance_trial(trial_id, TrialState.COMMIT_DECIDED, now=NOW)
        kernel.advance_trial(
            trial_id,
            TrialState.STOPPING,
            reason=StopReason.PARTIAL_APPLY,
            now=T1,
        )

        self.assertEqual(kernel.recover(now=T2), ())

        self.assertEqual(
            gateway.calls, ["query", "reread", "rollback", "reread", "confirm"]
        )
        self.assertEqual(
            kernel.reduced_state()["transactions"][f"tx:{trial_id}"]["resolution"],
            "ROLLED_BACK",
        )

    def test_incident_lockdown_resolution_keeps_new_trials_blocked(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        advance_to_decision_hold(kernel, trial_id)
        record_evaluation(
            kernel,
            trial_id,
            execution_validity=ExecutionValidity.INVALID,
            predicate_verdicts={"mandatory-1": PredicateVerdict.INDETERMINATE},
            hold_complete=False,
            validity_region_stable=False,
        )
        kernel.decide_trial(
            trial_id,
            stop_reasons=(StopReason.HARD_SAFETY_GUARD,),
            now=T3,
        )

        kernel.resolve_recovery(
            f"tx:{trial_id}", resolution="INCIDENT_LOCKDOWN", now=T3
        )

        self.assertEqual(
            kernel.reduced_state()["trials"][trial_id]["state"],
            TrialState.INCIDENT_LOCKDOWN.value,
        )
        with self.assertRaises(KernelRefusal) as refusal:
            kernel.open_trial(candidate_id="candidate-2", case_id="case-1", now=T3)
        self.assertEqual(refusal.exception.reason, "INCIDENT_LOCKDOWN")

    def test_old_fence_result_is_recorded_and_rejected(self) -> None:
        kernel, store = make_kernel()
        trial_id = kernel.open_trial(candidate_id="candidate-1", case_id="case-1", now=NOW)
        advance_to_decision_hold(kernel, trial_id)
        first = kernel.issue_token(trial_id, token_kind=TokenKind.STOP, now=T3)
        kernel.issue_token(trial_id, token_kind=TokenKind.STOP, now=T3)

        with self.assertRaises(KernelRefusal) as refusal:
            kernel.record_gateway_result(
                first,
                GatewayResult(GatewayOutcome.ACKED),
                resource_id="resource-1",
                now=T3,
            )
        self.assertEqual(refusal.exception.reason, "OLD_FENCE")
        self.assertEqual(list(store.iterate())[-1].event_kind, "GatewayResultRejected")

    def test_unissued_higher_fence_result_fails_closed(self) -> None:
        kernel, store = make_kernel()
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        advance_to_decision_hold(kernel, trial_id)
        forged = KernelToken(
            token_kind=TokenKind.STOP,
            transaction_id=f"tx:{trial_id}",
            trial_id=trial_id,
            fencing_token=99,
            command_sequence=0,
            lease_expiry="2026-08-21T00:01:00.000000Z",
            expected_config_hash="a" * 64,
            idempotency_key="forged-token",
            issued_at=T3,
        )

        with self.assertRaises(KernelRefusal) as refusal:
            kernel.record_gateway_result(
                forged,
                GatewayResult(GatewayOutcome.ACKED),
                resource_id="resource-1",
                now=T3,
            )

        self.assertEqual(refusal.exception.reason, "TOKEN_NOT_ISSUED")
        self.assertEqual(list(store.iterate())[-1].event_kind, "GatewayResultRejected")

    def test_expired_post_commit_result_durably_stops_trial(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        advance_to_ready(kernel, trial_id)
        kernel.record_commit_readiness(
            trial_id,
            watchdogs_armed=True,
            baseline_hash=content_hash({"baseline": 1}),
            now=NOW,
        )
        kernel.advance_trial(trial_id, TrialState.COMMIT_DECIDED, now=NOW)
        token = kernel.issue_token(trial_id, token_kind=TokenKind.COMMIT, now=NOW)

        with self.assertRaises(KernelRefusal) as refusal:
            kernel.record_gateway_result(
                token,
                GatewayResult(GatewayOutcome.ACKED),
                resource_id="resource-1",
                now="2026-08-21T00:00:31.000000Z",
            )

        self.assertEqual(refusal.exception.reason, "LEASE_EXPIRED")
        trial = kernel.reduced_state()["trials"][trial_id]
        self.assertEqual(trial["state"], TrialState.STOPPING.value)
        self.assertEqual(trial["stopReason"], StopReason.LEASE_EXPIRY.value)

    def test_unknown_post_commit_result_takes_partial_apply_stop_path(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        advance_to_ready(kernel, trial_id)
        kernel.record_commit_readiness(
            trial_id,
            watchdogs_armed=True,
            baseline_hash=content_hash({"baseline": 1}),
            now=NOW,
        )
        kernel.advance_trial(trial_id, TrialState.COMMIT_DECIDED, now=NOW)
        token = kernel.issue_token(trial_id, token_kind=TokenKind.COMMIT, now=NOW)

        kernel.record_gateway_result(
            token,
            GatewayResult(GatewayOutcome.UNKNOWN),
            resource_id="resource-1",
            now=T1,
        )

        trial = kernel.reduced_state()["trials"][trial_id]
        self.assertEqual(trial["state"], TrialState.STOPPING.value)
        self.assertEqual(trial["stopReason"], StopReason.PARTIAL_APPLY.value)

    def test_reserve_exceeding_harm_observation_stops_before_refusal(self) -> None:
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
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

        with self.assertRaises(KernelRefusal) as refusal:
            kernel.charge_harm(
                trial_id,
                amount=q(5, source="measured-harm-1"),
                harm_kind=HarmKind.TRIAL_INDUCED,
                for_missing_interval=False,
                now=T2,
            )

        self.assertEqual(refusal.exception.reason, "TRIAL_RESERVE_EXCEEDED")
        trial = kernel.reduced_state()["trials"][trial_id]
        self.assertEqual(trial["state"], TrialState.STOPPING.value)
        self.assertEqual(trial["stopReason"], StopReason.HARM_LIMIT_BREACH.value)


if __name__ == "__main__":
    unittest.main()


class NothingLearnedKeepsTheCandidate(unittest.TestCase):
    """핸드오프 2026-09-18 §5.1: 아무것도 알려 주지 않은 시행은 후보를 소진하지 않는다."""

    def settle(self, outcome):
        kernel, _ = make_kernel()
        trial_id = kernel.open_trial(candidate_id="candidate-1", case_id="case-1", now=NOW)
        kernel.advance_trial(trial_id, TrialState.PRE_COMMIT_ABORT, now=NOW)
        kernel.advance_trial(trial_id, TrialState.SETTLEMENT, now=NOW)
        kernel.settle_trial(trial_id, outcome=outcome, now=NOW)
        return kernel

    def test_a_refused_before_apply_trial_leaves_the_candidate_available(self):
        kernel = self.settle(TrialOutcome.EXEC_ERROR)
        self.assertEqual("AVAILABLE",
                         kernel.reduced_state()["candidateAvailability"]["candidate-1"])
        # 같은 후보를 다시 열 수 있다 -- 재측정이 막히지 않는다.
        kernel.open_trial(candidate_id="candidate-1", case_id="case-1", now=NOW)

    def test_an_operator_abort_still_consumes_it(self):
        kernel = self.settle(TrialOutcome.OPERATOR_ABORTED)
        self.assertEqual("CONSUMED",
                         kernel.reduced_state()["candidateAvailability"]["candidate-1"])
