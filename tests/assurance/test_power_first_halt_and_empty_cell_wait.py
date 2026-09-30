"""Codex review of 507e23efc (2026-09-26): power-first joint plans must also HALT in reverse
first-write order, and a cell-power restore right after a hand-back must wait for the UE to
re-attach instead of refusing an empty cell."""
import unittest
from types import SimpleNamespace

from assurance.gateway.gateway import TokenBoundWriteGateway
from oran.campaign5.live_worker import Campaign5LiveWorker, IdentityRefusal


class ParticipantsFollowTheSteps(unittest.TestCase):
    def test_power_first_plan_lists_power_first_and_halts_it_last(self):
        steps = [SimpleNamespace(axis="txAttenuationDb@87654321"), SimpleNamespace(axis="servingCell@ue1"),
                 SimpleNamespace(axis="dlPrbCap@ue2")]
        names = {"txAttenuationDb@87654321": "r1-power@87654321", "servingCell@ue1": "r1@ue1",
                 "dlPrbCap@ue2": "r1-cap@ue2"}
        stub = SimpleNamespace(_step_adapter_name=lambda staged, axis: names[axis])
        staged = SimpleNamespace(adapter="r1@ue1", steps=steps)
        order = TokenBoundWriteGateway._plan_participants(stub, staged)
        self.assertEqual(("r1-power@87654321", "r1@ue1", "r1-cap@ue2"), order)
        self.assertEqual("r1-power@87654321", tuple(reversed(order))[-1])

    def test_the_plan_adapter_is_kept_when_no_step_names_it(self):
        stub = SimpleNamespace(_step_adapter_name=lambda staged, axis: "r1-cap@ue2")
        staged = SimpleNamespace(adapter="r1@ue1", steps=[SimpleNamespace(axis="dlPrbCap@ue2")])
        self.assertEqual(("r1-cap@ue2", "r1@ue1"),
                         TokenBoundWriteGateway._plan_participants(stub, staged))


class EmptyCellWait(unittest.TestCase):
    def _stub(self, fails, wait_s):
        calls = {"n": 0, "slept": 0}

        def resolve(requested, cell):
            calls["n"] += 1
            if calls["n"] <= fails:
                raise IdentityRefusal("bound cell has no fresh UE control header")
            return SimpleNamespace(epoch=7)

        def sleep(s):
            calls["slept"] += 1
        return SimpleNamespace(_resolve_identity=resolve, _sleep=sleep, _cell_ue_wait_s=wait_s), calls

    def test_waits_for_a_ue_to_reattach(self):
        stub, calls = self._stub(fails=3, wait_s=25.0)
        got = Campaign5LiveWorker._resolve_control_identity(stub, SimpleNamespace(cell_id="c", epoch=7))
        self.assertEqual(7, got.epoch)
        self.assertEqual(3, calls["slept"])

    def test_refuses_after_the_bound(self):
        stub, calls = self._stub(fails=100, wait_s=2.0)
        with self.assertRaises(IdentityRefusal):
            Campaign5LiveWorker._resolve_control_identity(stub, SimpleNamespace(cell_id="c", epoch=7))
        self.assertEqual(2, calls["slept"])

    def test_default_is_no_wait(self):
        stub, calls = self._stub(fails=1, wait_s=0.0)
        with self.assertRaises(IdentityRefusal):
            Campaign5LiveWorker._resolve_control_identity(stub, SimpleNamespace(cell_id="c", epoch=7))
        self.assertEqual(0, calls["slept"])


if __name__ == "__main__":
    unittest.main()
