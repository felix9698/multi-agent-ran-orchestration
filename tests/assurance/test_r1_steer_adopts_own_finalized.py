"""2026-09-27 board 807: a steering adapter (no binding journal) adopts the policy its own finalized
transaction left live, instead of a CREATE the producer refuses 409.  Hermetic."""
from __future__ import annotations

import unittest

from assurance.gateway.commands import GatewayOperation, build_command
from assurance.gateway.r1_adapter import R1Adapter
from assurance.gateway.token import TokenKind
from assurance.gateway.write_gateway import GatewayOutcome
from tests.assurance.kgw_support import token
from tests.assurance.test_r1_undo_refusals import Refusal

SCOPE = {"ueId": "ue-2"}


class OneScopePort:
    """Like A1-P: one live policy per UE scope; a second CREATE is refused 409."""

    def __init__(self):
        self.cell, self.policies, self.calls = "A", {}, []

    def get_policy_type(self, policy_type_id):
        return {}

    def create_policy(self, ric, policy_type_id, body):
        if self.policies:
            self.calls.append(("CREATE-409",))
            raise Refusal("AIC_POLICY_CONFLICT: A1-P returned HTTP 409")
        pid = f"p-{len(self.calls) + 1}"
        self.calls.append(("CREATE", pid)); self.policies[pid] = body; self.cell = body["config"]["value"]
        return {"policyId": pid}

    def update_policy(self, pid, body):
        self.calls.append(("UPDATE", pid)); self.policies[pid] = body; self.cell = body["config"]["value"]

    def delete_policy(self, pid):
        self.calls.append(("DELETE", pid)); self.policies.pop(pid)   # never steers back

    def get_policy_status(self, pid):
        return {}


def send(target, operation, value=None, seq=0, tx="tx-1"):
    permit = token(TokenKind.COMMIT, sequence=seq, transaction_id=tx)
    axis = "cell" if operation in (GatewayOperation.APPLY, GatewayOperation.UNDO) else None
    return target.dispatch(token=permit, command=build_command(
        permit, operation, scope=SCOPE, axis=axis, value=value if axis else None, index=seq))


class ASteeringAdapterAdoptsItsOwnFinalizedPolicy(unittest.TestCase):
    def setUp(self):
        self.port = port = OneScopePort()
        self.target = R1Adapter(
            policy_port=port,
            policy_builder=lambda command: {"scope": dict(SCOPE), "config": {"value": command.get("value")}},
            near_rt_ric_id="ric", policy_type_id="T",
            readback_port=lambda **_: {"cell": port.cell}, refusal_errors=(Refusal,))
        self.target.restore_by_handover = True

    def test_a_later_trial_updates_the_retained_policy_and_its_rollback_hands_back(self):
        send(self.target, GatewayOperation.VALIDATE, tx="tx-1")
        self.assertIs(send(self.target, GatewayOperation.APPLY, "B", 1, "tx-1").outcome, GatewayOutcome.ACKED)
        self.target._mark_finalized("tx-1")          # trial 1 settled SUCCESS: retention keeps it live
        send(self.target, GatewayOperation.VALIDATE, tx="tx-2")
        result = send(self.target, GatewayOperation.APPLY, "A", 2, "tx-2")
        self.assertIs(result.outcome, GatewayOutcome.ACKED, result.detail)
        self.assertEqual([("CREATE", "p-1"), ("UPDATE", "p-1")], self.port.calls)
        self.assertEqual("p-1", self.target.bound_policy("tx-2"))
        self.assertIsNone(self.target.bound_policy("tx-1"))
        undo = send(self.target, GatewayOperation.UNDO, "B", 3, "tx-2")   # back to the held cell
        self.assertIs(undo.outcome, GatewayOutcome.ACKED, undo.detail)
        self.assertEqual("B", self.port.cell)

    def test_a_lost_update_answer_still_hands_back_on_rollback(self):
        send(self.target, GatewayOperation.VALIDATE, tx="tx-1")
        send(self.target, GatewayOperation.APPLY, "B", 1, "tx-1")
        self.target._mark_finalized("tx-1")
        real = self.port.update_policy
        def lost(pid, body):
            real(pid, body); raise TimeoutError("answer lost")
        self.port.update_policy = lost
        send(self.target, GatewayOperation.VALIDATE, tx="tx-2")
        send(self.target, GatewayOperation.APPLY, "A", 2, "tx-2")
        self.port.update_policy = real
        self.assertEqual("A", self.port.cell)                   # the write landed
        send(self.target, GatewayOperation.UNDO, "B", 3, "tx-2")
        self.assertEqual("B", self.port.cell)

    def test_an_unfinalized_earlier_transaction_is_not_adopted(self):
        send(self.target, GatewayOperation.VALIDATE, tx="tx-1")
        send(self.target, GatewayOperation.APPLY, "B", 1, "tx-1")
        send(self.target, GatewayOperation.VALIDATE, tx="tx-2")
        send(self.target, GatewayOperation.APPLY, "A", 2, "tx-2")
        self.assertIn(("CREATE-409",), self.port.calls)


if __name__ == "__main__":
    unittest.main()
