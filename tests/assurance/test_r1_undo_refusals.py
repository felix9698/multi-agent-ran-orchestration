"""R1 adapter rollback paths audited 2026-09-23 (defects 1, 3, 5, 7).

Hermetic: an in-memory policy port and binding journal, no network.
"""
from __future__ import annotations

import unittest

from assurance.gateway.commands import GatewayOperation, build_command
from assurance.gateway.r1_adapter import R1Adapter
from assurance.gateway.r1_binding_journal import BindingState, InMemoryR1BindingJournal
from assurance.gateway.token import TokenKind
from assurance.gateway.write_gateway import GatewayOutcome
from tests.assurance.kgw_support import token

SCOPE = {"ueId": "ue-1"}
GATE = "DELETE rollback gate failed: UE scope has no unique fresh KPM/header"
NO_HEADER = "bound cell has no fresh UE control header"


class Refusal(Exception):
    """A producer answer that carries its HTTP status, like ``R1Refusal``."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


class Port:
    def __init__(self) -> None:
        self.value = "A"
        self.policies = {}
        self.deletes = []
        self.updates = []
        self.delete_errors = []
        self.update_errors = []
        self.create_error = None

    def get_policy_type(self, policy_type_id):
        return {}

    def create_policy(self, ric, policy_type_id, body):
        if self.create_error is not None:
            raise self.create_error
        self.policies["p-1"] = dict(body)
        self.value = body["config"]["value"]
        return {"policyId": "p-1"}

    def update_policy(self, policy_id, body):
        self.updates.append(policy_id)
        if self.update_errors:
            raise self.update_errors.pop(0)
        self.policies[policy_id] = dict(body)
        self.value = body["config"]["value"]

    def delete_policy(self, policy_id):
        self.deletes.append(policy_id)
        if self.delete_errors:
            raise self.delete_errors.pop(0)
        self.policies.pop(policy_id, None)
        self.value = "A"

    def get_policy_status(self, policy_id):
        return {}

    def get_policy(self, policy_id):
        if policy_id not in self.policies:
            raise Refusal(f"unknown policy {policy_id}", status=404)
        return dict(self.policies[policy_id])


def adapter(port: Port, **kwargs) -> R1Adapter:
    built = R1Adapter(
        policy_port=port,
        policy_builder=lambda command: {
            "config": {"ueId": SCOPE["ueId"], "value": command.get("value")},
            "trace": {"revision": 1}},
        near_rt_ric_id="ric", policy_type_id="T",
        readback_port=lambda **_: {"cell": port.value},
        binding_journal=InMemoryR1BindingJournal(),
        scope_key=lambda body: "ueId=" + body["config"]["ueId"],
        retain_binding_until_restore=True, **kwargs)
    built._gate_sleep = lambda seconds: None
    return built


def send(target: R1Adapter, operation: GatewayOperation, value=None, sequence=0):
    permit = token(TokenKind.COMMIT, sequence=sequence)
    axis = "cell" if operation in (GatewayOperation.APPLY, GatewayOperation.UNDO) else None
    return target.dispatch(token=permit, command=build_command(
        permit, operation, scope=SCOPE, axis=axis,
        value=value if axis else None, index=sequence))


def applied(port: Port, **kwargs) -> R1Adapter:
    target = adapter(port, **kwargs)
    send(target, GatewayOperation.VALIDATE)
    result = send(target, GatewayOperation.APPLY, "B", 1)
    assert result.outcome is GatewayOutcome.ACKED, result
    return target


class ARefusedDeleteLeavesTheBindingWithdrawable(unittest.TestCase):
    """Defect 1: a refused DELETE wrote nothing, so the next UNDO must send it again.

    The binding stays RESTORE_PENDING (the policy is live; a BOUND read could be
    satisfied by another cell's equal scalar), marked refused rather than lost."""

    def test_the_next_undo_sends_the_delete_again(self):
        port = Port()
        target = applied(port, refusal_errors=(Refusal,))
        port.delete_errors = [Refusal("revision conflict")]
        first = send(target, GatewayOperation.UNDO, "A", 2)
        self.assertIs(first.outcome, GatewayOutcome.REJECTED)
        record = target.durable_binding("tx-1")
        # Still owed (the policy is live), but marked as asked-and-refused.
        self.assertIs(record.state, BindingState.RESTORE_PENDING)
        self.assertTrue(record.detail.startswith("A1 DELETE refused"))
        self.assertEqual("p-1", record.policy_id)
        second = send(target, GatewayOperation.UNDO, "A", 3)
        self.assertIs(second.outcome, GatewayOutcome.ACKED, second.detail)
        self.assertEqual(["p-1", "p-1"], port.deletes)

    def test_an_exhausted_gate_also_reverts(self):
        port = Port()
        target = applied(port, refusal_errors=(Refusal,))
        port.delete_errors = [Refusal(GATE)] * R1Adapter.GATE_RETRY_ATTEMPTS
        send(target, GatewayOperation.UNDO, "A", 2)
        self.assertTrue(target.durable_binding("tx-1").detail.startswith("A1 DELETE refused"))
        self.assertIs(send(target, GatewayOperation.UNDO, "A", 3).outcome,
                      GatewayOutcome.ACKED)

    def test_a_lost_delete_is_asked_again(self):
        # Q-3 (2026-09-24): this used to pin UNKNOWN forever.  The next UNDO reads
        # first: the policy is still there (the DELETE never landed), so it is sent
        # again and settles.  A port that cannot read keeps UNKNOWN (no blind retry,
        # test_r1_withdrawal_obligations).
        port = Port()
        target = applied(port, refusal_errors=(Refusal,))
        port.delete_errors = [OSError("timeout")]
        self.assertIs(send(target, GatewayOperation.UNDO, "A", 2).outcome,
                      GatewayOutcome.UNKNOWN)
        self.assertIs(target.durable_binding("tx-1").state, BindingState.RESTORE_PENDING)
        self.assertIs(send(target, GatewayOperation.UNDO, "A", 3).outcome,
                      GatewayOutcome.ACKED)
        self.assertEqual(2, len(port.deletes))
        self.assertTrue(target.durable_binding("tx-1").withdrawal_acknowledged)


class AProducerStatusIsARefusalWithoutItsClass(unittest.TestCase):
    """Defect 3: ``R1Refusal`` cannot be imported under ``assurance/**``."""

    def test_a_409_is_rejected_not_unknown_with_no_refusal_errors(self):
        port = Port()
        target = adapter(port)
        send(target, GatewayOperation.VALIDATE)
        port.create_error = Refusal("target scope already owned", status=409)
        self.assertIs(send(target, GatewayOperation.APPLY, "B", 1).outcome,
                      GatewayOutcome.REJECTED)

    def test_a_retryable_status_stays_unknown(self):
        port = Port()
        target = adapter(port)
        send(target, GatewayOperation.VALIDATE)
        port.create_error = Refusal("service unavailable", status=503)
        self.assertIs(send(target, GatewayOperation.APPLY, "B", 1).outcome,
                      GatewayOutcome.UNKNOWN)


class TheGateIsRetriedOnEveryRestoringWrite(unittest.TestCase):
    """Defect 5: the hand-back UPDATE and the second KPM-gap phrase."""

    def test_the_hand_back_update_is_retried(self):
        port = Port()
        target = applied(port, refusal_errors=(Refusal,))
        target.restore_by_handover = True
        port.update_errors = [Refusal(NO_HEADER), Refusal(GATE)]
        result = send(target, GatewayOperation.UNDO, "A", 2)
        self.assertIs(result.outcome, GatewayOutcome.ACKED, result.detail)
        self.assertEqual(3, len(port.updates))
        self.assertEqual(["p-1"], port.deletes)

    def test_the_no_header_phrase_is_retried_on_delete(self):
        port = Port()
        target = applied(port, refusal_errors=(Refusal,))
        port.delete_errors = [Refusal(NO_HEADER)]
        self.assertIs(send(target, GatewayOperation.UNDO, "A", 2).outcome,
                      GatewayOutcome.ACKED)
        self.assertEqual(2, len(port.deletes))


class ALostCreateIsNotNothingToWithdraw(unittest.TestCase):
    """Defect 7: a POST whose answer was lost may have created a policy."""

    def test_undo_after_a_lost_create_is_unknown_and_holds_the_scope(self):
        port = Port()
        target = adapter(port)
        send(target, GatewayOperation.VALIDATE)
        port.create_error = OSError("read timed out")
        self.assertIs(send(target, GatewayOperation.APPLY, "B", 1).outcome,
                      GatewayOutcome.UNKNOWN)
        result = send(target, GatewayOperation.UNDO, "A", 2)
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertTrue(target.durable_binding("tx-1").holds_scope)
        self.assertFalse(target.release_unapplied("tx-1"))

    def test_a_reservation_that_never_applied_is_still_released(self):
        port = Port()
        target = adapter(port)
        send(target, GatewayOperation.VALIDATE)
        result = send(target, GatewayOperation.UNDO, "A", 2)
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertFalse(target.durable_binding("tx-1").holds_scope)


if __name__ == "__main__":
    unittest.main()
