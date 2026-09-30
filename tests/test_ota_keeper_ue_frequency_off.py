"""A UE whose LO locked far from the carrier is restarted once (2026-09-24 18:4x, ue3 at +13.8 kHz)."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911' / 'ops'


def _load():
    sys.path.insert(0, str(OPS))
    try:
        spec = importlib.util.spec_from_file_location('keeper_fo', OPS / 'keeper.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(OPS))


class UeFrequencyOff(unittest.TestCase):
    def setUp(self):
        self.k = _load()
        self.k.log = lambda *a, **kw: None
        self.restarts = []
        self.k.restart_ue = lambda host, evidence=None: self.restarts.append((host, evidence))
        self.tx = {'ue1': 3319680012, 'ue2': 3349920050, 'ue3': 3319693832}
        self.k.ssh = lambda host, cmd, timeout=20: types.SimpleNamespace(
            stdout=f'/home/x/ota-fixed38-{host}-A.log\nsetting tx_freq {self.tx[host]} Hz\n')

    def test_only_the_far_off_ue_is_restarted_once(self):
        self.k.ensure_ue_frequency_sane()
        self.k.ensure_ue_frequency_sane()
        self.assertEqual([('ue3', 'frequency-off')], self.restarts)

    def test_normal_offsets_are_left_alone(self):
        self.tx['ue3'] = 3319681399
        self.k.ensure_ue_frequency_sane()
        self.assertEqual([], self.restarts)


if __name__ == '__main__':
    unittest.main()
