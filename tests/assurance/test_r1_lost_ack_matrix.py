"""Q-3: a write that landed but whose answer was lost, at every R1 write site.

The producer performs the write and *then* the answer is lost -- a read
timeout, a reset connection, or a 5xx relayed after the write.  Pinned per site:

  unknown   the GatewayResult is ``UNKNOWN`` (never ``REJECTED``: something may
            have been written);
  journal   the settling journal record is ``UNKNOWN`` with
            ``write_may_have_occurred`` true;
  converge  a following UNDO (asked up to three times) ends ``ACKED`` with the
            producer back on the baseline and no policy left behind.

Plus the refusal rows: 400/404/409 before anything was written are
``REJECTED``/``REFUSED`` with no possible write, and a 404 on the DELETE inside
``_replace_policy`` is "already gone", not a refusal.

Cells the adapter does not meet would be marked ``expectedFailure`` via the
module's ``KNOWN_GAPS`` (empty since 2026-09-24).
Hermetic: in-memory ports and journals, no network.
"""
from __future__ import annotations

import unittest

from assurance.gateway.commands import GatewayOperation
from assurance.gateway.r1_adapter import R1Adapter
from assurance.gateway.r1_binding_journal import InMemoryR1BindingJournal
from assurance.gateway.r1_operation_journal import R1OperationOutcome, R1PolicyOperation
from assurance.gateway.write_gateway import GatewayOutcome
from tests.assurance.action102_support import CAP
from tests.assurance.test_r1_scope_takeover import AWithdrawalCannotRestoreANonSentinelBaseline
from tests.assurance.test_r1_undo_refusals import Refusal, send


class Status(Exception):
    """A relayed HTTP status that is not a producer decision (5xx)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status} after the write")
        self.status = status


#: The answer is lost after the producer wrote.
FAULTS = {
    "timeout": lambda: TimeoutError("read timed out"),
    "reset": lambda: ConnectionResetError("connection reset by peer"),
    "503": lambda: Status(503),
    "500": lambda: Status(500),
}


class LosingPort:
    """A scope-aware producer (A1-P rules) that can lose one answer per call kind."""

    def __init__(self) -> None:
        self.cell = "A"
        self.policies = {}
        self.next_id = 1
        self.lose = {}                      # "CREATE"/"UPDATE"/"DELETE" -> fault factory

    def _answer(self, kind):
        fault = self.lose.pop(kind, None)
        if fault is not None:
            raise fault()

    def get_policy_type(self, policy_type_id):
        return {}

    def create_policy(self, ric, policy_type_id, body):
        policy_id = f"p-{self.next_id}"
        self.next_id += 1
        self.policies[policy_id] = dict(body)
        self.cell = body["config"]["value"]
        self._answer("CREATE")
        return {"policyId": policy_id}

    def update_policy(self, policy_id, body):
        if self.policies[policy_id]["scope"] != body["scope"]:
            raise Refusal("A policy update cannot change its UE scope.")
        self.policies[policy_id] = dict(body)
        self.cell = body["config"]["value"]
        self._answer("UPDATE")

    def delete_policy(self, policy_id):
        if policy_id not in self.policies:
            raise Refusal(f"unknown policy {policy_id}", status=404)
        self.policies.pop(policy_id)
        if not self.policies:
            self.cell = "A"
        self._answer("DELETE")

    def get_policy_status(self, policy_id):
        return {}

    # The two reads the real ``R1Client`` has; a lost CREATE is found through them.
    def list_policies(self, *, near_rt_ric_id=None, policy_type_id=None):
        return sorted(self.policies)

    def get_policy(self, policy_id):
        if policy_id not in self.policies:
            raise Refusal(f"unknown policy {policy_id}", status=404)
        return dict(self.policies[policy_id])


def _adapter(port, ue, *, handover):
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
        retain_binding_until_restore=True)
    target._gate_sleep = lambda seconds: None
    target.restore_by_handover = handover
    return target


# Each site: (handover, the call kind whose answer is lost, steps up to the lost write).
def _apply_create(target, port, ue):
    send(target, GatewayOperation.VALIDATE)
    port.lose["CREATE"] = port.fault
    return send(target, GatewayOperation.APPLY, "B", 1)


def _apply_update(target, port, ue):
    send(target, GatewayOperation.VALIDATE)
    assert send(target, GatewayOperation.APPLY, "B", 1).outcome is GatewayOutcome.ACKED
    port.lose["UPDATE"] = port.fault
    return send(target, GatewayOperation.APPLY, "C", 2)


def _undo_delete(target, port, ue):
    send(target, GatewayOperation.VALIDATE)
    assert send(target, GatewayOperation.APPLY, "B", 1).outcome is GatewayOutcome.ACKED
    port.lose["DELETE"] = port.fault
    return send(target, GatewayOperation.UNDO, "A", 2)


def _handback_update(target, port, ue):
    send(target, GatewayOperation.VALIDATE)
    assert send(target, GatewayOperation.APPLY, "B", 1).outcome is GatewayOutcome.ACKED
    port.lose["UPDATE"] = port.fault
    return send(target, GatewayOperation.UNDO, "A", 2)


def _replace_delete(target, port, ue):
    send(target, GatewayOperation.VALIDATE)
    assert send(target, GatewayOperation.APPLY, "B", 1).outcome is GatewayOutcome.ACKED
    ue["id"] = 18                           # re-registered while steered away
    port.lose["DELETE"] = port.fault
    return send(target, GatewayOperation.UNDO, "A", 2)


def _replace_create(target, port, ue):
    send(target, GatewayOperation.VALIDATE)
    assert send(target, GatewayOperation.APPLY, "B", 1).outcome is GatewayOutcome.ACKED
    ue["id"] = 18
    port.lose["CREATE"] = port.fault
    return send(target, GatewayOperation.UNDO, "A", 2)


SITES = {
    "apply_create": (False, R1PolicyOperation.CREATE, _apply_create),
    "apply_update": (False, R1PolicyOperation.UPDATE, _apply_update),
    "undo_delete": (False, R1PolicyOperation.DELETE, _undo_delete),
    "handback_update": (True, R1PolicyOperation.UPDATE, _handback_update),
    "replace_delete": (True, R1PolicyOperation.DELETE, _replace_delete),
    "replace_create": (True, R1PolicyOperation.CREATE, _replace_create),
}

#: (site, check) cells the adapter does not meet.  Empty since 2026-09-24: the four
#: measured gaps (apply_create, undo_delete, replace_create converge; the baseline
#: rewrite converge) are fixed in ``r1_adapter`` -- a lost CREATE is found at the
#: producer, a lost DELETE is asked again (404 = it landed), and a rewrite's
#: revision is journalled before it is sent.
KNOWN_GAPS = set()


def _run(site, fault):
    handover, lost, steps = SITES[site]
    port, ue = LosingPort(), {"id": 15}
    port.fault = FAULTS[fault]
    target = _adapter(port, ue, handover=handover)
    return target, port, lost, steps(target, port, ue)


def _check_unknown(self, site):
    for fault in FAULTS:
        with self.subTest(fault=fault):
            _, _, _, result = _run(site, fault)
            self.assertIs(result.outcome, GatewayOutcome.UNKNOWN, result.detail)


def _check_journal(self, site):
    for fault in FAULTS:
        with self.subTest(fault=fault):
            target, _, lost, _ = _run(site, fault)
            settled = [op for op in target.operations("tx-1")
                       if op.operation is lost and op.outcome is not R1OperationOutcome.IN_FLIGHT]
            self.assertTrue(settled, "the lost write left no settling record")
            self.assertIs(settled[-1].outcome, R1OperationOutcome.UNKNOWN)
            self.assertTrue(settled[-1].write_may_have_occurred)


def _check_converge(self, site):
    for fault in FAULTS:
        with self.subTest(fault=fault):
            target, port, _, _ = _run(site, fault)
            outcomes = []
            for sequence in (3, 4, 5):
                result = send(target, GatewayOperation.UNDO, "A", sequence)
                outcomes.append(result.outcome)
                if result.outcome is GatewayOutcome.ACKED:
                    break
            self.assertIs(outcomes[-1], GatewayOutcome.ACKED, outcomes)
            self.assertEqual("A", port.cell)
            self.assertEqual({}, port.policies, "a policy was left at the producer")


class LostAckMatrix(unittest.TestCase):
    """site x {unknown, journal, converge}, each over the four lost-answer faults."""


for _site in SITES:
    for _check, _body in (("unknown", _check_unknown), ("journal", _check_journal),
                          ("converge", _check_converge)):
        def _test(self, _site=_site, _body=_body):
            _body(self, _site)
        if (_site, _check) in KNOWN_GAPS:
            _test = unittest.expectedFailure(_test)
        setattr(LostAckMatrix, f"test_{_site}_{_check}", _test)


class DefinitiveRefusals(unittest.TestCase):
    """400/404/409 before anything was written: rejected, no possible write."""

    def test_a_refused_create_is_rejected_and_journals_no_write(self):
        for status in (400, 404, 409):
            with self.subTest(status=status):
                port, ue = LosingPort(), {"id": 15}
                target = _adapter(port, ue, handover=False)
                send(target, GatewayOperation.VALIDATE)

                def refuse(*args, status=status):
                    raise Refusal("refused", status=status)
                port.create_policy = refuse
                result = send(target, GatewayOperation.APPLY, "B", 1)
                self.assertIs(result.outcome, GatewayOutcome.REJECTED, result.detail)
                last = [op for op in target.operations("tx-1")
                        if op.operation is R1PolicyOperation.CREATE][-1]
                self.assertIs(last.outcome, R1OperationOutcome.REFUSED)
                self.assertFalse(last.write_may_have_occurred)

    def test_a_404_on_the_replace_delete_is_already_gone(self):
        port, ue = LosingPort(), {"id": 15}
        target = _adapter(port, ue, handover=True)
        send(target, GatewayOperation.VALIDATE)
        send(target, GatewayOperation.APPLY, "B", 1)
        ue["id"] = 18
        port.policies.clear()               # withdrawn out of band
        result = send(target, GatewayOperation.UNDO, "A", 2)
        self.assertIs(result.outcome, GatewayOutcome.ACKED, result.detail)
        self.assertEqual({}, port.policies)


class _LosingOnePolicyPort:
    """Wraps the campaign-5 double so one UPDATE lands and then loses its answer."""

    def __init__(self, inner, fault):
        self._inner, self._fault, self.armed = inner, fault, False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def update_policy(self, policy_id, body):
        self._inner.update_policy(policy_id, body)
        if self.armed:
            self.armed = False
            raise self._fault()


class LostAckOnTheBaselineRewrite(AWithdrawalCannotRestoreANonSentinelBaseline):
    """The PF/cap baseline rewrite (``_baseline_needing_rewrite``) is an UPDATE too."""

    def _lost_rewrite(self, fault):
        self.adapter._port = _LosingOnePolicyPort(self.port, FAULTS[fault])
        self._dispatch("PREPARE", GatewayOperation.READ, transaction="tx-create")
        self.assertIs(GatewayOutcome.ACKED, self._settle("tx-create", "18").outcome)
        self._dispatch("PREPARE", GatewayOperation.READ, transaction="tx-return")
        self._dispatch("PREPARE", GatewayOperation.VALIDATE, transaction="tx-return",
                       value=CAP.baseline)
        self._dispatch("COMMIT", GatewayOperation.APPLY, transaction="tx-return",
                       value=CAP.baseline, sequence=1)
        self.adapter._port.armed = True
        return self._dispatch("STOP", GatewayOperation.UNDO, transaction="tx-return",
                              value=CAP.baseline, sequence=2)

    def test_baseline_rewrite_unknown(self):
        for fault in FAULTS:
            with self.subTest(fault=fault):
                self.setUp()
                result = self._lost_rewrite(fault)
                self.assertIs(GatewayOutcome.UNKNOWN, result.outcome, result.detail)

    def test_baseline_rewrite_journal(self):
        for fault in FAULTS:
            with self.subTest(fault=fault):
                self.setUp()
                self._lost_rewrite(fault)
                settled = [op for op in self.adapter.operations("tx-return")
                           if op.operation is R1PolicyOperation.UPDATE
                           and op.outcome is not R1OperationOutcome.IN_FLIGHT]
                self.assertIs(settled[-1].outcome, R1OperationOutcome.UNKNOWN)
                self.assertTrue(settled[-1].write_may_have_occurred)

    def test_baseline_rewrite_converge(self):
        for fault in FAULTS:
            with self.subTest(fault=fault):
                self.setUp()
                self._lost_rewrite(fault)
                result = self._dispatch("STOP", GatewayOperation.UNDO, transaction="tx-return",
                                        value=CAP.baseline, sequence=3)
                self.assertIs(GatewayOutcome.ACKED, result.outcome, result.detail)
                self.assertEqual("18", self._live())


if __name__ == "__main__":
    unittest.main()
