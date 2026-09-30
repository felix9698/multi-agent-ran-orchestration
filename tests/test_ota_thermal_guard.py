"""Thermal guard decisions (hermetic: no host is read, nothing is stopped)."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'experiment_results/ota-20260911/ops/thermal_guard.py'


def _load():
    spec = importlib.util.spec_from_file_location('thermal_guard_under_test', SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ThermalGuardDecides(unittest.TestCase):
    def setUp(self):
        self.g = _load()
        tmp = Path(tempfile.mkdtemp())
        self.g.PAUSE, self.g.LOG = tmp / 'PAUSE', tmp / 'log.jsonl'
        self.g.HOSTS = ('ue1',)
        self.stopped = []
        self.g.stop_host = lambda host: self.stopped.append(host) or 0

    def test_normal_readings_do_nothing(self):
        self.g.read = lambda host: [('k10temp', 63.0), ('nvme', 48.0), ('acpitz', 20.0)]
        self.g.cycle()
        self.assertFalse(self.g.PAUSE.exists())
        self.assertEqual([], self.stopped)

    def test_warn_pauses_without_stopping(self):
        self.g.read = lambda host: [('k10temp', 91.0)]
        self.g.cycle()
        self.assertTrue(self.g.PAUSE.exists())
        self.assertEqual([], self.stopped)

    def test_critical_pauses_and_stops_that_host(self):
        self.g.read = lambda host: [('nvme', 86.0), ('k10temp', 60.0)]
        self.g.cycle()
        self.assertTrue(self.g.PAUSE.exists())
        self.assertEqual(['ue1'], self.stopped)

    def test_unlisted_sensor_is_ignored(self):
        self.g.read = lambda host: [('iwlwifi_1', 120.0)]
        self.g.cycle()
        self.assertFalse(self.g.PAUSE.exists())

    def test_an_unreadable_host_decides_nothing(self):
        self.g.read = lambda host: None
        self.g.cycle()
        self.assertFalse(self.g.PAUSE.exists())
        self.assertIn('THERMAL_UNREADABLE', self.g.LOG.read_text())


if __name__ == '__main__':
    unittest.main()
