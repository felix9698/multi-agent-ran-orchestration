"""2026-09-27: between boards a cell left off its C0 attenuation for two cycles is restored."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "keeper_att", Path(__file__).resolve().parents[1] / "experiment_results/ota-20260911/ops/keeper.py")
keeper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keeper)


class RestoreAttenuation(unittest.TestCase):
    def test_two_cycles_then_restore(self):
        state = {'gnb1': 'current TX attenuation 14.0 dB', 'gnb2': 'current TX attenuation 6.0 dB'}
        sent = []

        def telnet(cell, cmd):
            if cmd == 'ci rfatt':
                return state[cell]
            sent.append((cell, cmd))
            return 'TX attenuation set'
        keeper._cell_telnet, keeper.log = telnet, (lambda *a, **k: None)
        keeper._cell_attenuation_scope_owned = lambda cell: False
        keeper._attenuation_off_seen.clear()
        keeper.restore_cell_attenuation_between_boards()
        self.assertEqual([], sent)
        keeper.restore_cell_attenuation_between_boards()
        self.assertEqual([('gnb1', 'ci rfatt 8')], sent)

    def test_a_live_policy_is_left_to_the_producer(self):
        sent = []
        keeper._cell_telnet = lambda cell, cmd: ('current TX attenuation 14.0 dB' if cmd == 'ci rfatt'
                                                 else sent.append(cmd) or '')
        keeper.log = lambda *a, **k: None
        keeper._cell_attenuation_scope_owned = lambda cell: True
        keeper._attenuation_off_seen.clear()
        for _ in range(3):
            keeper.restore_cell_attenuation_between_boards()
        self.assertEqual([], sent)


if __name__ == "__main__":
    unittest.main()
