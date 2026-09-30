"""Gateway defects 6 and 8 of the 2026-09-23 audit.  Hermetic (mock adapter)."""
from __future__ import annotations

import unittest

from assurance.gateway.commands import GatewayOperation
from assurance.gateway.write_gateway import GatewayOutcome, GatewayResult
from tests.assurance.kgw_support import BASELINE, SCOPE, GatewayFixture

#: A no-op first step, then a move whose answer is lost.
PLAN = {
    "adapter": "mock",
    "scope": dict(SCOPE),
    "baselineConfig": dict(BASELINE),
    "steps": [
        {"axis": "prbCap", "value": BASELINE["prbCap"]},
        {"axis": "queuePriority", "value": 7},
    ],
}


class TheStepWhoseAnswerWasLostIsOwedAReversal(GatewayFixture, unittest.TestCase):
    """Defect 6: ``applied_axes`` stopped *before* the UNKNOWN step."""

    def test_the_lost_step_is_in_the_applied_axes(self):
        self.build()
        real = self.adapter.dispatch

        def lose_the_move(*, token, command):
            if (command["operation"] == GatewayOperation.APPLY.value
                    and command.get("axis") == "queuePriority"):
                self.adapter.commands.append(dict(command))
                return GatewayResult(outcome=GatewayOutcome.UNKNOWN, detail="timeout")
            return real(token=token, command=command)

        self.adapter.dispatch = lose_the_move
        self.do_prepare(plan=PLAN)
        self.do_ready()
        result = self.do_commit()
        self.assertIsNot(result.outcome, GatewayOutcome.ACKED)
        record = self.journal.read("tx-1")
        self.assertIn("queuePriority", record.applied_axes)


class AnAdapterExceptionKeepsItsMessage(GatewayFixture, unittest.TestCase):
    """Defect 8: only the class name survived."""

    def test_the_message_is_in_the_detail(self):
        self.build()

        def explode(*, token, command):
            if command["operation"] == GatewayOperation.APPLY.value:
                raise RuntimeError("producer said: policy type not advertised")
            return real(token=token, command=command)

        real = self.adapter.dispatch
        self.adapter.dispatch = explode
        self.do_prepare()
        self.do_ready()
        result = self.do_commit()
        self.assertIn("policy type not advertised", result.detail)


if __name__ == "__main__":
    unittest.main()
