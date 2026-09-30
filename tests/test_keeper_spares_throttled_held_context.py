"""2026-09-27 03:26: during a board a held RNTI marked out-of-sync on its own cell is a throttled UE
(a trial capped and deprioritised it), not the 591-593 stale twin -- it must not be released."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "keeper_desync", Path(__file__).resolve().parents[1] / "experiment_results/ota-20260911/ops/keeper.py")
keeper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keeper)

OUT = "".join(f"Frame.Slot\nUE RNTI 4d1a CU-UE-ID 1 out-of-sync\nUE 4d1a: ulsch_rounds 1/0\n" for _ in range(5))


class SpareThrottled(unittest.TestCase):
    def setUp(self):
        self.sent = []
        keeper.os.environ.pop('AIC_RELEASE_ZOMBIES', None)
        keeper._cell_runs_checked_release = lambda cell: True
        keeper._ue_held_rntis = lambda: {'ue3': '4d1a'}
        keeper._retired_rntis = lambda: set()
        keeper.log = lambda *a, **k: None
        keeper.time.sleep = lambda s: None
        keeper._cell_telnet = lambda cell, cmd: (self.sent.append((cell, cmd)) or 'softmodem ok')
        keeper._gnb1_tail = lambda n=0: OUT
        keeper._gnb2_tail = lambda n=0: "Frame.Slot\nUE aaaa: ulsch_rounds 1/0\n" * 5

    def test_throttled_held_context_is_spared_in_a_board(self):
        keeper.episode_in_sitting = lambda: True
        self.assertEqual([], keeper.release_zombie_contexts('gnb1', {'4d1a'}))

    def test_a_stale_twin_on_the_other_cell_is_still_released(self):
        keeper.episode_in_sitting = lambda: True
        keeper._gnb2_tail = lambda n=0: "Frame.Slot\nUE 4d1a: ulsch_rounds 1/0\n" * 5
        self.assertEqual(['4d1a'], keeper.release_zombie_contexts('gnb1', {'4d1a'}))


if __name__ == "__main__":
    unittest.main()
