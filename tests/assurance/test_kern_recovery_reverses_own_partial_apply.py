"""커널 복구는 **우리 자신의** 부분 적용이면 되돌리고 나서 판정한다 (2026-09-23 라이브).

판 `formal38guarded-20260923T083611` 시행 3 (조종 ue1 + pfWeight ue2):

    COMMIT   PARTIAL_APPLY  관측 51e64321   <- 계획의 어느 '접두'와도 불일치
    REREAD   UNKNOWN        관측 없음         (25 ms 뒤)
    -> 되돌리기를 한 번도 내지 않고 INCIDENT_LOCKDOWN, 두 정책이 RIC 에 남음

두 결함: (1) A1 효과는 순서 없이 착지하므로 "두 번째 축만 들어간" 상태는 접두가 아니어도
우리 것이다; (2) 읽을 수 없는 재독은 모르는 설정이 아니라 불확정이다.  둘 다 되돌리기를
보내고 확인 읽기가 판정하게 한다.  **읽히는데 계획 밖 값**이면 여전히 잠근다.
"""
from __future__ import annotations

import unittest
from unittest import mock

from assurance.contracts.catalog import Candidate, CandidateCatalog
from assurance.core.addressing import content_hash
from assurance.core.states import StopReason, TrialState
from assurance.gateway.plan import ActuationPlan
from assurance.gateway.token import KernelToken, TokenKind
from assurance.gateway.write_gateway import GatewayOutcome, GatewayResult

from tests.assurance import test_kern_lifecycle as lifecycle

TWO_AXES = {"axisA": "1", "axisB": "2"}
BASELINE = {"axisA": "0", "axisB": "0"}


def _two_axis_catalog(epoch_id: str = "epoch-1", *, shared_resource: bool = False):
    return CandidateCatalog(
        contract_id=f"catalog/{epoch_id}", version="1.0.0", schema_version="assurance/1.0.0",
        document_status="NORMATIVE", standard_mapping={}, generator_version="test-generator/1",
        cardinality=2,
        candidates=(Candidate(candidate_id="candidate-1", target_ref="target-1",
                              option_ref="option-1", parameters=dict(TWO_AXES),
                              semantic_hash="a" * 64, capability_ref="resource-1"),
                    Candidate(candidate_id="candidate-2", target_ref="target-1",
                              option_ref="option-1", parameters={"axisA": "3", "axisB": "0"},
                              semantic_hash="b" * 64,
                              capability_ref="resource-1" if shared_resource else "resource-2")),
        catalog_hash=content_hash({"catalog": epoch_id}), epoch_ref=epoch_id)


class _Gateway:
    """재독이 차례로 `rereads` 를 답하고, 되돌린 뒤에는 기준선을 답한다."""

    def __init__(self, rereads):
        self.rereads = list(rereads)
        self.calls = []
        self.reversed = False

    def query_transaction(self, transaction_id):
        self.calls.append("query")
        return GatewayResult(GatewayOutcome.UNKNOWN)

    def reread_configuration(self, *, token: KernelToken):
        self.calls.append("reread")
        if self.reversed or not self.rereads:
            return GatewayResult(GatewayOutcome.ACKED,
                                 observed_config_hash=content_hash(BASELINE))
        answer = self.rereads.pop(0)
        if answer is None:
            return GatewayResult(GatewayOutcome.UNKNOWN)
        return GatewayResult(GatewayOutcome.ACKED, observed_config_hash=answer)

    def reverse_rollback(self, *, token: KernelToken):
        self.calls.append("rollback")
        self.reversed = True
        # 실제 게이트웨이처럼: 되돌린 뒤 확인 읽기가 본 기준선을 결과에 싣는다.
        return GatewayResult(GatewayOutcome.ACKED, observed_config_hash=content_hash(BASELINE))

    def confirm_recovery(self, *, token: KernelToken):
        self.calls.append("confirm")
        return GatewayResult(GatewayOutcome.ACKED)


class OwnPartialApplyIsReversedNotLockedDown(unittest.TestCase):
    def _recover(self, rereads):
        """커밋 뒤 부분 적용으로 STOPPING 에 선 시행을 복구시킨다 -- 라이브와 같은 자리."""
        gateway = _Gateway(rereads)
        with mock.patch.object(lifecycle, "catalog", _two_axis_catalog):
            kernel, _ = lifecycle.make_kernel(gateway=gateway)
        NOW = lifecycle.NOW
        trial_id = kernel.open_trial(candidate_id="candidate-1", case_id="case-1", now=NOW)
        kernel.advance_trial(trial_id, TrialState.VALIDATING, now=NOW)
        kernel.reserve(trial_id, now=NOW)
        kernel.advance_trial(trial_id, TrialState.RESERVED, now=NOW)
        plan = lifecycle.staged_plan(kernel, trial_id)
        plan["baselineConfig"] = dict(BASELINE)
        kernel.stage_actuation_plan(trial_id, plan=plan, now=NOW)
        kernel.advance_trial(trial_id, TrialState.PREPARING, now=NOW)
        ready = kernel.issue_token(trial_id, token_kind=TokenKind.READY, now=NOW)
        resource_id = kernel.reduced_state()["trials"][trial_id]["resourceId"]
        kernel.record_gateway_result(
            ready, GatewayResult(GatewayOutcome.ACKED,
                                 observed_config_hash=content_hash(BASELINE),
                                 evidence_refs=lifecycle.watchdog_arming_evidence(kernel, trial_id)),
            resource_id=resource_id, now=NOW)
        kernel.advance_trial(trial_id, TrialState.READY, now=NOW)
        kernel.record_commit_readiness(trial_id, watchdogs_armed=True,
                                       baseline_hash=content_hash(BASELINE), now=NOW)
        kernel.advance_trial(trial_id, TrialState.COMMIT_DECIDED, now=NOW)
        kernel.advance_trial(trial_id, TrialState.STOPPING,
                             reason=StopReason.PARTIAL_APPLY, now=lifecycle.T1)
        kernel.recover(now=lifecycle.T2)
        resolution = kernel.reduced_state()["transactions"][f"tx:{trial_id}"]["resolution"]
        return gateway, resolution, ActuationPlan.from_mapping(plan)

    def test_a_non_prefix_own_state_is_reversed(self):
        plan = ActuationPlan.from_mapping({
            "adapter": "mock", "scope": {"ueId": "ue-1"}, "baselineConfig": dict(BASELINE),
            "steps": [{"axis": a, "value": v} for a, v in sorted(TWO_AXES.items())]})
        second_only = content_hash({**BASELINE, "axisB": "2"})
        self.assertIn(second_only, plan.subset_hashes())
        self.assertNotIn(second_only, plan.prefix_hashes(), "접두가 아니어야 이 시험이 뜻을 가진다")
        gateway, resolution, _ = self._recover([second_only])
        self.assertIn("rollback", gateway.calls, "우리 부분 적용인데 되돌리지 않았다")
        self.assertEqual("ROLLED_BACK", resolution)

    def test_an_unreadable_reread_still_reverses(self):
        gateway, resolution, _ = self._recover([None])
        self.assertIn("rollback", gateway.calls, "읽을 수 없다고 되돌리기를 건너뛰었다")
        self.assertEqual("ROLLED_BACK", resolution)

    def test_a_readable_foreign_configuration_still_locks_down(self):
        foreign = content_hash({"axisA": "9", "axisB": "0"})
        gateway, resolution, _ = self._recover([foreign])
        # 2026-09-26: our own policies are withdrawn before the lockdown (board 670 left
        # three at the RIC); the resolution is still INCIDENT_LOCKDOWN.
        self.assertIn("rollback", gateway.calls)
        self.assertEqual("INCIDENT_LOCKDOWN", resolution)


if __name__ == "__main__":
    unittest.main()
