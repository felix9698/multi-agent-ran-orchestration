"""A block reference reads a UE's tun address more than once (2026-09-30 08:47: ue1's reads over
its wireless control link came back empty for ~2 min while the UE stayed attached, and a one-shot
read refused the block-4 reference)."""
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

PATH = Path(__file__).resolve().parents[1] / "experiment_results/ota-20260911/ops/reference_dl.py"


def _mod():
    spec = importlib.util.spec_from_file_location("reference_dl_tun_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TunIpRetry(unittest.TestCase):
    def test_empty_reads_then_address(self):
        mod = _mod()
        outs = iter(['', '', '12.1.1.130\n'])
        with mock.patch.object(mod, 'sh', side_effect=lambda *a, **k: next(outs)), \
                mock.patch.object(mod.time, 'sleep') as sleep:
            self.assertEqual(mod.tun_ip('ue1', tries=6), '12.1.1.130')
        self.assertEqual(sleep.call_count, 2)

    def test_default_reads_once(self):
        mod = _mod()
        with mock.patch.object(mod, 'sh', return_value='') as sh, mock.patch.object(mod.time, 'sleep') as sleep:
            self.assertIsNone(mod.tun_ip('ue1'))
        self.assertEqual((sh.call_count, sleep.call_count), (1, 0))

    def test_measure_resolves_every_address_before_starting_anything(self):
        mod = _mod()
        order = []
        with mock.patch.object(mod, 'tun_ip', side_effect=lambda h, tries=1: order.append(('ip', h)) or ''), \
                mock.patch.object(mod, '_start_echo', side_effect=lambda *a: order.append(('echo',))), \
                mock.patch.object(mod, 'sh', side_effect=lambda *a, **k: order.append(('sh',)) or ''):
            with self.assertRaises(SystemExit):
                mod.measure(next(iter(mod.CONFIGS)), 'x', echo=True)
        self.assertTrue(order and all(o[0] == 'ip' for o in order), order)
        self.assertEqual(len(order), len(mod.CONFIGS[next(iter(mod.CONFIGS))]))

    def test_absent_every_time_is_none(self):
        mod = _mod()
        with mock.patch.object(mod, 'sh', return_value='') as sh, mock.patch.object(mod.time, 'sleep'):
            self.assertIsNone(mod.tun_ip('ue1', tries=3))
        self.assertEqual(sh.call_count, 3)


if __name__ == '__main__':
    unittest.main()
