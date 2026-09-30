"""gnb2 is restarted once when a start came up with a high receive noise floor (2026-09-24).

Per-start I0 medians that night: 231-241 normally, 275 (03:19) and 263 (05:20) twice -- in
those starts UE PRACH was buried (`RAR reception failed`), and a restart brought I0 back.
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911' / 'ops'


def _load():
    sys.path.insert(0, str(OPS))
    try:
        spec = importlib.util.spec_from_file_location('keeper_noise_floor', OPS / 'keeper.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(OPS))


class Gnb2NoiseFloor(unittest.TestCase):
    def setUp(self):
        self.k = _load()
        self.k._gnb2_noise_checked = None
        self.k.log = lambda *a, **kw: None
        self.k.episode_in_sitting = lambda: False
        self.restarts = []
        self.k.restart_gnb2 = lambda *a, **kw: self.restarts.append(kw.get('reason'))

    def _feed(self, name, values):
        out = name + '\n' + ''.join(f'I0 {v}\n' for v in values)
        self.k.ssh = lambda *a, **kw: types.SimpleNamespace(stdout=out)

    def test_a_high_start_is_restarted_once(self):
        self._feed('/tmp/gnb2-probe-A.log', [262, 263, 264, 271, 272])
        self.k.ensure_gnb2_noise_floor()
        self.k.ensure_gnb2_noise_floor()
        self.assertEqual(['noise-floor'], self.restarts)

    def test_a_normal_start_is_left_alone(self):
        self._feed('/tmp/gnb2-probe-B.log', [227, 230, 231, 232, 241])
        self.k.ensure_gnb2_noise_floor()
        self.assertEqual([], self.restarts)

    def test_too_few_samples_wait(self):
        self._feed('/tmp/gnb2-probe-C.log', [275, 275])
        self.k.ensure_gnb2_noise_floor()
        self.assertEqual([], self.restarts)

    def test_never_inside_a_board(self):
        self.k.episode_in_sitting = lambda: True
        self._feed('/tmp/gnb2-probe-D.log', [270] * 6)
        self.k.ensure_gnb2_noise_floor()
        self.assertEqual([], self.restarts)


if __name__ == '__main__':
    unittest.main()
