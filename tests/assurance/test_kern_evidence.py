"""Evidence closure and finite termination tests for lane KERN."""

from __future__ import annotations

import unittest

from assurance.contracts.ledgers import (
    CompatibilityCheck,
    CompatibilityRecord,
    EvidenceCell,
    EvidenceContribution,
)
from assurance.contracts.harm import HarmKind
from assurance.core.axes import (
    AggregateState,
    EvidenceCellStatus,
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
    TrialOutcome,
)
from assurance.core.addressing import content_hash
from assurance.core.states import TrialState
from assurance.gateway.token import TokenKind
from assurance.gateway.write_gateway import GatewayOutcome, GatewayResult
from assurance.kernel.kernel import KernelRefusal
from tests.assurance.test_kern_lifecycle import (
    NOW,
    T3,
    advance_to_decision_hold,
    make_kernel,
    policy,
    record_evaluation,
    q,
)


def contribution(
    contribution_id: str,
    *,
    verdict: PredicateVerdict = PredicateVerdict.PASS,
    dependency_group: str | None = None,
    trace: str | None = None,
    reused_from_epoch: str | None = None,
    trial_ref: str | None = None,
) -> EvidenceContribution:
    return EvidenceContribution(
        contribution_id=contribution_id,
        trial_ref=trial_ref or f"trial-{contribution_id}",
        candidate_semantic_hash="a" * 64,
        execution_validity=ExecutionValidity.VALID,
        measurement_sufficiency=MeasurementSufficiency.SUFFICIENT,
        predicate_verdict=verdict,
        trace_refs=(content_hash({"trace": trace or contribution_id}),),
        dependency_group=dependency_group,
        reused_from_epoch=reused_from_epoch,
    )


def close_supported(kernel, *, cell_id: str, item: EvidenceContribution):
    """Record the Kernel evidence that objectively backs one contribution."""
    if item.reused_from_epoch is None:
        state = kernel.reduced_state()
        if item.trial_ref not in state["trials"]:
            kernel._append(
                "TrialOpened",
                object_id=item.trial_ref,
                now=NOW,
                payload={
                    "candidateId": f"candidate-{item.contribution_id}",
                    "candidateSemanticHash": item.candidate_semantic_hash,
                    "caseId": "case-1",
                    "epochId": "epoch-1",
                    "resourceId": f"resource-{item.contribution_id}",
                    "targetRef": "target-1",
                    "transactionId": f"tx:{item.trial_ref}",
                    "trialId": item.trial_ref,
                },
            )
            path = (
                TrialState.VALIDATING,
                TrialState.RESERVED,
                TrialState.PREPARING,
                TrialState.READY,
                TrialState.COMMIT_DECIDED,
                TrialState.APPLYING,
                TrialState.APPLIED_PENDING_RESULT,
                TrialState.SETTLING,
                TrialState.OBSERVING,
                TrialState.DECISION_HOLD,
            )
            source = TrialState.PROPOSED
            for target in path:
                kernel._append(
                    "TrialStateChanged",
                    object_id=item.trial_ref,
                    now=NOW,
                    payload={
                        "from": source.value,
                        "reason": None,
                        "to": target.value,
                        "transactionId": f"tx:{item.trial_ref}",
                        "trialId": item.trial_ref,
                    },
                )
                source = target
        known_traces = {
            sample["traceHash"]
            for sample in kernel.reduced_state()["samples"].values()
        }
        for index, trace_hash in enumerate(item.trace_refs):
            if trace_hash in known_traces:
                continue
            sample_id = f"sample:{item.contribution_id}:{index}"
            kernel._append(
                "RawSampleIngested",
                object_id=sample_id,
                now=NOW,
                payload={
                    "counterId": "counter-1",
                    "sampleId": sample_id,
                    "sequence": index,
                    "traceHash": trace_hash,
                },
            )
        kernel._append(
            "TrialEvaluated",
            object_id=item.trial_ref,
            now=NOW,
            payload={
                "executionValidity": item.execution_validity.value,
                "holdComplete": True,
                "mandatoryPredicateIds": ["predicate-1"],
                "measurementSufficiency": item.measurement_sufficiency.value,
                "predicateVerdicts": {
                    "predicate-1": item.predicate_verdict.value
                },
                "sameCandidate": True,
                "stopReason": None,
                "traceRefs": list(item.trace_refs),
                "trialId": item.trial_ref,
                "validityRegionStable": True,
            },
        )
    return kernel.close_evidence(cell_id=cell_id, contribution=item, now=NOW)


def cell(
    cell_id: str,
    *,
    status: EvidenceCellStatus = EvidenceCellStatus.OPEN,
    quota: int = 1,
    sealed_until: str | None = None,
) -> EvidenceCell:
    return EvidenceCell(
        cell_id=cell_id,
        target_ref="target-1",
        candidate_semantic_hash="a" * 64,
        status=status,
        required_independent_contributions=quota,
        sealed_until_vector_ref=sealed_until,
    )


class EvidenceClosureTests(unittest.TestCase):
    def test_same_trace_or_dependency_group_counts_only_once(self) -> None:
        kernel, _ = make_kernel()
        kernel.register_evidence_cell(cell("cell-1", quota=2), case_id="case-1", now=NOW)

        self.assertEqual(
            close_supported(
                kernel,
                cell_id="cell-1",
                item=contribution("1", dependency_group="group-1"),
            ),
            EvidenceCellStatus.PARTIAL,
        )
        self.assertEqual(
            close_supported(
                kernel,
                cell_id="cell-1",
                item=contribution(
                    "2", dependency_group="group-1", trace="trace-1"
                ),
            ),
            EvidenceCellStatus.PARTIAL,
        )
        self.assertEqual(
            close_supported(
                kernel,
                cell_id="cell-1",
                item=contribution("3", dependency_group="group-2"),
            ),
            EvidenceCellStatus.CLOSED_PASS,
        )

    def test_dormant_and_historical_evidence_stay_sealed_or_provisional(self) -> None:
        kernel, _ = make_kernel()
        kernel.register_evidence_cell(
            cell(
                "dormant",
                status=EvidenceCellStatus.DORMANT_SEALED,
                sealed_until="vector-2",
            ),
            case_id="case-1",
            now=NOW,
        )
        self.assertEqual(
            close_supported(
                kernel, cell_id="dormant", item=contribution("dormant")
            ),
            EvidenceCellStatus.DORMANT_SEALED,
        )

        kernel.register_evidence_cell(cell("historical"), case_id="case-1", now=NOW)
        checks = {check.value: True for check in CompatibilityCheck}
        kernel.record_compatibility(
            CompatibilityRecord(
                record_id="compat-1",
                source_epoch_ref="old-epoch",
                target_epoch_ref="epoch-1",
                candidate_semantic_hash="a" * 64,
                results=checks,
                admitted=True,
            ),
            now=NOW,
        )
        self.assertEqual(
            close_supported(
                kernel,
                cell_id="historical",
                item=contribution("historical", reused_from_epoch="old-epoch"),
            ),
            EvidenceCellStatus.PROVISIONAL_HISTORICAL,
        )
        self.assertEqual(
            close_supported(
                kernel, cell_id="historical", item=contribution("confirm")
            ),
            EvidenceCellStatus.CLOSED_PASS,
        )

    def test_incompatible_cross_epoch_reuse_fails_closed(self) -> None:
        kernel, _ = make_kernel()
        kernel.register_evidence_cell(cell("cell-1"), case_id="case-1", now=NOW)
        with self.assertRaises(KernelRefusal) as refusal:
            kernel.close_evidence(
                cell_id="cell-1",
                contribution=contribution("reuse", reused_from_epoch="old-epoch"),
                now=NOW,
            )
        self.assertEqual(refusal.exception.reason, "INCOMPATIBLE_EVIDENCE_REUSE")

    def test_pass_after_closed_fail_is_witness_and_does_not_rewrite_history(self) -> None:
        kernel, _ = make_kernel()
        kernel.register_evidence_cell(cell("cell-1"), case_id="case-1", now=NOW)
        self.assertEqual(
            close_supported(
                kernel,
                cell_id="cell-1",
                item=contribution("fail", verdict=PredicateVerdict.FAIL),
            ),
            EvidenceCellStatus.CLOSED_FAIL,
        )
        self.assertEqual(
            close_supported(
                kernel, cell_id="cell-1", item=contribution("pass")
            ),
            EvidenceCellStatus.CLOSED_FAIL,
        )
        recorded = kernel.reduced_state()["evidenceCells"]["cell-1"][
            "pendingEvidenceUpdates"
        ][-1]["contribution"]
        self.assertTrue(recorded["isPostClosureWitness"])

    def test_unbacked_contribution_cannot_close_a_cell(self) -> None:
        kernel, _ = make_kernel()
        kernel.register_evidence_cell(cell("cell-1"), case_id="case-1", now=NOW)

        with self.assertRaises(KernelRefusal) as refusal:
            kernel.close_evidence(
                cell_id="cell-1", contribution=contribution("forged"), now=NOW
            )

        self.assertEqual(refusal.exception.reason, "EVIDENCE_TRIAL_NOT_FOUND")

    def test_settlement_atomically_carries_trial_evidence_and_reserve_return(self) -> None:
        kernel, _ = make_kernel()
        kernel.register_evidence_cell(cell("cell-1"), case_id="case-1", now=NOW)
        trial_id = kernel.open_trial(
            candidate_id="candidate-1", case_id="case-1", now=NOW
        )
        advance_to_decision_hold(kernel, trial_id)
        kernel.charge_harm(
            trial_id,
            amount=q(2, source="settlement-observation"),
            harm_kind=HarmKind.TRIAL_INDUCED,
            for_missing_interval=False,
            now=T3,
        )
        pending_state = kernel.reduced_state()
        self.assertEqual(len(pending_state["pendingHarmCharges"][trial_id]), 1)
        self.assertFalse(
            any(
                entry.get("movementKind") == "CHARGE"
                for entry in pending_state["harmLedger"]
            )
        )
        record_evaluation(kernel, trial_id)
        item = contribution("settled", trial_ref=trial_id)
        self.assertEqual(
            close_supported(kernel, cell_id="cell-1", item=item),
            EvidenceCellStatus.CLOSED_PASS,
        )
        pending_cell = kernel.reduced_state()["evidenceCells"]["cell-1"]
        self.assertEqual(pending_cell["status"], EvidenceCellStatus.OPEN.value)
        self.assertEqual(
            pending_cell["pendingStatus"], EvidenceCellStatus.CLOSED_PASS.value
        )
        kernel.decide_trial(trial_id, stop_reasons=(), now=T3)
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

        settled = kernel.settle_trial(
            trial_id, outcome=TrialOutcome.SUCCESS, now=T3
        )

        self.assertEqual(settled.payload["locksReleased"], ["resource-1"])
        self.assertEqual(len(settled.payload["harmMovements"]), 1)
        self.assertEqual(
            settled.payload["evidenceUpdates"][0]["contributions"][0][
                "contributionId"
            ],
            "settled",
        )
        self.assertEqual(
            kernel.reduced_state()["evidenceCells"]["cell-1"]["status"],
            EvidenceCellStatus.CLOSED_PASS.value,
        )
        settled_state = kernel.reduced_state()
        self.assertNotIn(trial_id, settled_state["pendingHarmCharges"])
        self.assertEqual(
            len(
                [
                    entry
                    for entry in settled_state["harmLedger"]
                    if entry.get("movementKind") == "CHARGE"
                ]
            ),
            1,
        )


class ExhaustionTerminationTests(unittest.TestCase):
    def test_exhaustion_refuses_ambiguous_case_instead_of_mixing_cells(self) -> None:
        kernel, _ = make_kernel()
        kernel.register_evidence_cell(cell("case-1-cell"), case_id="case-1", now=NOW)
        kernel.open_case(
            case_id="case-2",
            policy=policy(),
            active_vector="vector-1",
            usable_reserve={"harm-1": q(10).to_canonical_dict()},
            reserve_per_trial={"harm-1": q(4).to_canonical_dict()},
            evidence_cells=(),
            now=NOW,
        )
        kernel.register_evidence_cell(
            cell("case-2-cell", status=EvidenceCellStatus.CLOSED_PASS),
            case_id="case-2",
            now=NOW,
        )

        with self.assertRaises(KernelRefusal) as refusal:
            kernel.exhaustion_certificate(vector_ref="vector-1")

        self.assertEqual(refusal.exception.reason, "AMBIGUOUS_ACTIVE_CASE_FOR_VECTOR")

    def test_open_partial_or_blocked_obligation_is_evidence_incomplete(self) -> None:
        for status in (
            EvidenceCellStatus.OPEN,
            EvidenceCellStatus.PARTIAL,
            EvidenceCellStatus.TEMP_BLOCKED,
            EvidenceCellStatus.BUDGET_LOCKED,
            EvidenceCellStatus.PROVISIONAL_HISTORICAL,
        ):
            with self.subTest(status=status.value):
                kernel, _ = make_kernel()
                kernel.register_evidence_cell(
                    cell("cell-1", status=status), case_id="case-1", now=NOW
                )
                aggregate, certificate = kernel.exhaustion_certificate(
                    vector_ref="vector-1"
                )
                self.assertEqual(aggregate, AggregateState.EVIDENCE_INCOMPLETE)
                self.assertEqual(len(certificate), 64)


if __name__ == "__main__":
    unittest.main()
