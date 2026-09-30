"""2026-09-24 board 151803: a hand-back after the UE re-registered.

The builder follows the role to the UE's new id, and both the R1 service and
A1-P refuse an UPDATE that changes a policy's UE scope.  The hand-back must
withdraw the dead-id policy and pin the live id with a fresh one.  Hermetic.
"""
from __future__ import annotations

import unittest

from assurance.gateway.commands import GatewayOperation
from assurance.gateway.r1_adapter import R1Adapter
from assurance.gateway.r1_binding_journal import InMemoryR1BindingJournal
from assurance.gateway.write_gateway import GatewayOutcome
from tests.assurance.action102_support import CAP
from tests.assurance.test_r1_scope_takeover import (
    AWithdrawalCannotRestoreANonSentinelBaseline, OnePolicyPort)
from tests.assurance.test_r1_undo_refusals import Refusal, send


class ScopedPort:
    """A producer that refuses a scope change on update, like A1-P."""

    def __init__(self) -> None:
        self.cell = "A"
        self.policies = {}
        self.calls = []

    def get_policy_type(self, policy_type_id):
        return {}

    def create_policy(self, ric, policy_type_id, body):
        policy_id = f"p-{len(self.policies) + len(self.calls) + 1}"
        self.calls.append(("CREATE", policy_id, body["scope"]["ueId"]))
        self.policies[policy_id] = dict(body)
        self.cell = body["config"]["value"]
        return {"policyId": policy_id}

    def update_policy(self, policy_id, body):
        self.calls.append(("UPDATE", policy_id, body["scope"]["ueId"]))
        if self.policies[policy_id]["scope"] != body["scope"]:
            raise Refusal("A policy update cannot change its UE scope.")
        self.policies[policy_id] = dict(body)
        self.cell = body["config"]["value"]

    def delete_policy(self, policy_id):
        self.calls.append(("DELETE", policy_id, None))
        if policy_id not in self.policies:
            raise Refusal(f"unknown policy {policy_id}", status=404)
        self.policies.pop(policy_id)

    def get_policy_status(self, policy_id):
        return {}


class AHandBackAfterReRegistration(unittest.TestCase):

    def test_the_dead_id_policy_is_replaced_not_updated(self):
        port = ScopedPort()
        ue = {"id": 15}
        target = R1Adapter(
            policy_port=port,
            policy_builder=lambda command: {
                "scope": {"ueId": ue["id"]},
                "config": {"value": command.get("value")},
                "trace": {"revision": 1}},
            near_rt_ric_id="ric", policy_type_id="T",
            readback_port=lambda **_: {"cell": port.cell},
            binding_journal=InMemoryR1BindingJournal(),
            scope_key=lambda body: f"ueId={body['scope']['ueId']}",
            refusal_errors=(Refusal,), retain_binding_until_restore=True)
        target._gate_sleep = lambda seconds: None
        target.restore_by_handover = True
        send(target, GatewayOperation.VALIDATE)
        self.assertIs(send(target, GatewayOperation.APPLY, "B", 1).outcome,
                      GatewayOutcome.ACKED)
        ue["id"] = 18                       # re-registered while steered away
        result = send(target, GatewayOperation.UNDO, "A", 2)
        self.assertIs(result.outcome, GatewayOutcome.ACKED, result.detail)
        self.assertEqual([("CREATE", "p-1", 15), ("DELETE", "p-1", None),
                          ("CREATE", "p-3", 18), ("DELETE", "p-3", None)], port.calls)
        self.assertEqual("A", port.cell)
        self.assertEqual({}, port.policies)

    def test_an_unchanged_id_still_updates_in_place(self):
        port = ScopedPort()
        target = R1Adapter(
            policy_port=port,
            policy_builder=lambda command: {
                "scope": {"ueId": 15}, "config": {"value": command.get("value")},
                "trace": {"revision": 1}},
            near_rt_ric_id="ric", policy_type_id="T",
            readback_port=lambda **_: {"cell": port.cell},
            refusal_errors=(Refusal,))
        target.restore_by_handover = True
        send(target, GatewayOperation.VALIDATE)
        send(target, GatewayOperation.APPLY, "B", 1)
        self.assertIs(send(target, GatewayOperation.UNDO, "A", 2).outcome,
                      GatewayOutcome.ACKED)
        self.assertEqual(["CREATE", "UPDATE", "DELETE"], [c[0] for c in port.calls])


def _steering(port, ue):
    target = R1Adapter(
        policy_port=port,
        policy_builder=lambda command: {
            "scope": {"ueId": ue["id"]},
            "config": {"value": command.get("value")},
            "trace": {"revision": 1}},
        near_rt_ric_id="ric", policy_type_id="T",
        readback_port=lambda **_: {"cell": port.cell},
        binding_journal=InMemoryR1BindingJournal(),
        scope_key=lambda body: f"ueId={body['scope']['ueId']}",
        refusal_errors=(Refusal,), retain_binding_until_restore=True)
    target._gate_sleep = lambda seconds: None
    target.restore_by_handover = True
    return target


class SiblingUpdateSites(unittest.TestCase):
    """Every UPDATE site, not only the hand-back, meets the moved scope."""

    def test_an_apply_on_a_bound_policy_after_reregistration_recreates_it(self):
        port, ue = ScopedPort(), {"id": 15}
        target = _steering(port, ue)
        send(target, GatewayOperation.VALIDATE)
        send(target, GatewayOperation.APPLY, "B", 1)
        ue["id"] = 18
        result = send(target, GatewayOperation.APPLY, "B", 2)
        self.assertIs(result.outcome, GatewayOutcome.ACKED, result.detail)
        self.assertEqual([("CREATE", "p-1", 15), ("DELETE", "p-1", None),
                          ("CREATE", "p-3", 18)], port.calls)
        self.assertIn("re-registered", result.detail)

    def test_a_create_that_failed_after_the_delete_is_asked_again(self):
        port, ue = ScopedPort(), {"id": 15}
        target = _steering(port, ue)
        send(target, GatewayOperation.VALIDATE)
        send(target, GatewayOperation.APPLY, "B", 1)
        ue["id"] = 18
        create = port.create_policy

        def lost(*args):
            port.calls.append(("CREATE-LOST", None, None))
            raise RuntimeError("producer unreachable")
        port.create_policy = lost
        first = send(target, GatewayOperation.UNDO, "A", 2)
        self.assertIsNot(first.outcome, GatewayOutcome.ACKED)
        port.create_policy = create
        second = send(target, GatewayOperation.UNDO, "A", 3)
        self.assertIs(second.outcome, GatewayOutcome.ACKED, second.detail)
        # The old policy is already gone: its 404 does not refuse the undo.
        self.assertEqual("A", port.cell)
        self.assertEqual({}, port.policies)
        self.assertEqual(2, sum(1 for c in port.calls if c[:2] == ("DELETE", "p-1")))


class _ScopeRefusingPort(OnePolicyPort):
    def update_policy(self, policy_id, body):
        if self.policies[policy_id]["config"]["ueId"] != body["config"]["ueId"]:
            raise AssertionError("policy id cannot move to a different target scope")
        super().update_policy(policy_id, body)


class ABaselineRewriteAfterReregistration(AWithdrawalCannotRestoreANonSentinelBaseline):
    """The PF/cap baseline rewrite is an UPDATE too (``_baseline_needing_rewrite``)."""

    def setUp(self):
        super().setUp()
        self.port = _ScopeRefusingPort()
        self.adapter._port = self.port
        self.ue = "132"
        self.adapter._build = lambda command, last_revision=0: {
            "config": {"ueId": self.ue, "cap": str(command["value"])},
            "trace": {"revision": int(last_revision) + 1}}

    def test_the_baseline_is_written_on_the_new_id(self):
        self._dispatch("PREPARE", GatewayOperation.READ, transaction="tx-create")
        self.assertIs(GatewayOutcome.ACKED, self._settle("tx-create", "18").outcome)
        self._dispatch("PREPARE", GatewayOperation.READ, transaction="tx-return")
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-return",
                       value=CAP.baseline)
        self._dispatch("COMMIT", GatewayOperation.APPLY, transaction="tx-return",
                       value=CAP.baseline, sequence=1)
        self.ue = "140"                     # re-registered before the reversal
        result = self._dispatch("STOP", GatewayOperation.UNDO, transaction="tx-return",
                                value=CAP.baseline, sequence=2)
        self.assertIs(GatewayOutcome.ACKED, result.outcome, result.detail)
        self.assertEqual(["policy-1"], self.port.deleted)
        self.assertEqual(("policy-2", "18"), self.port.creates[-1])
        self.assertEqual("140", self.port.policies["policy-2"]["config"]["ueId"])

    # The inherited cases run on the unchanged id and must still pass.


if __name__ == "__main__":
    unittest.main()
