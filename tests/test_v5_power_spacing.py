"""v5 cell-power execution rules (2026-09-26): the gNB refuses an Action-104 write within
15 s of the previous one, and a board's finalized power policy owns the scope past the
board.  One rule for every method: the worker spaces and skips, the adapter waits before
its HTTP call and takes over a retained power policy it is refused for."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from assurance.gateway import power_spacing
from assurance.gateway.commands import GatewayOperation
from assurance.gateway.write_gateway import GatewayOutcome
from oran.campaign5 import live_worker
from tests.assurance import test_r1_undo_refusals as undo

OWNER = "f9da18cf-de74-5208-8113-b6b0d79e3ba6"


class _Stamp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"AIC_POWER_WRITE_STAMP": str(Path(tmp.name) / "s")})
        env.start()
        self.addCleanup(env.stop)


class TheWorkerSpacesAndSkipsCellPowerWrites(_Stamp):
    def _worker(self, current):
        worker = live_worker.Campaign5LiveWorker.__new__(live_worker.Campaign5LiveWorker)
        worker._read_values = lambda action, identity, min_line: current
        worker.sent, worker.slept = [], []
        worker._run_control = lambda action, ident, values: (
            worker.sent.append(dict(values)) or (True, "ACK"))
        worker._sleep = worker.slept.append
        return worker

    def test_a_value_the_cell_holds_is_not_written(self):
        worker = self._worker({"txAttenuationDb": 6.0})
        power = live_worker._ACTIONS["AIC_CellDlTxPower_1.0.0"]
        ack, _detail = worker._spaced_control(power, None, None, {"txAttenuationDb": 6})
        self.assertTrue(ack)
        self.assertEqual([], worker.sent)

    def test_a_write_inside_the_interval_waits_the_rest_and_stamps(self):
        power_spacing.mark_power_write(time.time() - 4.0)
        worker = self._worker({"txAttenuationDb": 6.0})
        power = live_worker._ACTIONS["AIC_CellDlTxPower_1.0.0"]
        worker._spaced_control(power, None, None, {"txAttenuationDb": 9})
        self.assertEqual([{"txAttenuationDb": 9}], worker.sent)
        self.assertAlmostEqual(power_spacing.POWER_WRITE_INTERVAL_S - 4.0, worker.slept[0],
                               delta=0.5)
        self.assertGreater(power_spacing.power_write_wait_s(),
                           power_spacing.POWER_WRITE_INTERVAL_S - 1)

    def test_other_actions_are_untouched(self):
        power_spacing.mark_power_write()
        worker = self._worker({"maxDlPrbs": 6})
        cap = live_worker._ACTIONS["AIC_UeDlPrbCap_1.0.0"]
        worker._spaced_control(cap, None, None, {"maxDlPrbs": 6})
        self.assertEqual([{"maxDlPrbs": 6}], worker.sent)
        self.assertEqual([], worker.slept)


class TheAdapterWaitsAndTakesOverARetainedPowerPolicy(_Stamp):
    def _adapter(self, port):
        target = undo.adapter(port, refusal_errors=(undo.Refusal,))
        target._policy_type_id = power_spacing.POWER_POLICY_TYPE
        target.slept = []
        target._gate_sleep = target.slept.append
        return target

    def test_a_create_refused_by_a_retained_owner_withdraws_it_and_creates(self):
        port = undo.Port()
        port.create_error = undo.Refusal(f"target scope already owned by policy {OWNER}")
        original = port.create_policy

        def create(ric, policy_type_id, body):
            if OWNER in port.deletes:
                port.create_error = None
            return original(ric, policy_type_id, body)
        port.create_policy = create
        target = self._adapter(port)
        undo.send(target, GatewayOperation.VALIDATE)
        result = undo.send(target, GatewayOperation.APPLY, "B", 1)
        self.assertIs(result.outcome, GatewayOutcome.ACKED, result.detail)
        self.assertEqual([OWNER], port.deletes)

    def test_a_steering_create_refused_by_a_retained_owner_is_taken_over_too(self):
        """2026-09-29 board 917: a retained ue3 steering policy survived a rebind; the new case's
        CREATE got A1-P's 'UE scope is already owned by policy <id>.' and the board died."""
        port = undo.Port()
        port.create_error = undo.Refusal(
            f"AIC_POLICY_CONFLICT: A1-P returned HTTP 409: UE scope is already owned by policy {OWNER}.")
        original = port.create_policy

        def create(ric, policy_type_id, body):
            if OWNER in port.deletes:
                port.create_error = None
            return original(ric, policy_type_id, body)
        port.create_policy = create
        target = self._adapter(port)
        target._policy_type_id = "AIC_UECellSteering_1.0.0"
        undo.send(target, GatewayOperation.VALIDATE)
        result = undo.send(target, GatewayOperation.APPLY, "B", 1)
        self.assertIs(result.outcome, GatewayOutcome.ACKED, result.detail)
        self.assertEqual([OWNER], port.deletes)

    def test_another_refusal_is_not_a_takeover(self):
        port = undo.Port()
        port.create_error = undo.Refusal("policy body invalid", status=400)
        target = self._adapter(port)
        undo.send(target, GatewayOperation.VALIDATE)
        undo.send(target, GatewayOperation.APPLY, "B", 1)
        self.assertEqual([], port.deletes)

    def test_a_power_write_waits_out_the_interval_before_its_call(self):
        power_spacing.mark_power_write(time.time() - 10.0)
        port = undo.Port()
        target = self._adapter(port)
        undo.send(target, GatewayOperation.VALIDATE)
        undo.send(target, GatewayOperation.APPLY, "B", 1)
        self.assertTrue(target.slept)
        self.assertAlmostEqual(power_spacing.POWER_WRITE_INTERVAL_S - 10.0, target.slept[0],
                               delta=0.5)


if __name__ == "__main__":
    unittest.main()
