"""The power-on runbook (steps 4-6) is a gate before every board (2026-09-24, owner).

A board must not start while a gNB is down, an E2 node is missing, a re-pin is owed after
a gNB restart, a producer port is closed or the R1 token is stale -- whoever restarted what.
"""
import importlib.util
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

OPS = Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911' / 'ops'


def _load():
    spec = importlib.util.spec_from_file_location('run_episode_bed_ready', OPS / 'run_episode.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _keeper(**over):
    token = mock.Mock()
    token.stat.return_value = types.SimpleNamespace(st_mtime=1000.0)
    k = types.SimpleNamespace(
        gnb1_pids=lambda: [11], CELL_NB={'gnb1': 3584, 'gnb2': 2816},
        _live_epochs=lambda: {3584: 958, 2816: 965},
        _gate_epoch_drift=lambda: {}, _inventory_drift=lambda: {},
        _port_is_open=lambda host, port: True,
        R1_TOKEN=token, R1_TOKEN_MAX_AGE_S=420.0)
    for key, value in over.items():
        setattr(k, key, value)
    return k


class BedReadyGate(unittest.TestCase):
    def setUp(self):
        self.r = _load()
        self.ssh = mock.patch.object(subprocess, 'run',
                                     return_value=types.SimpleNamespace(stdout='1\n'))
        self.ssh.start()
        self.clock = mock.patch.object(self.r.time, 'time', return_value=1100.0)
        self.clock.start()

    def tearDown(self):
        self.ssh.stop()
        self.clock.stop()
        sys.modules.pop('keeper', None)

    def missing(self, **over):
        with mock.patch.dict(sys.modules, {'keeper': _keeper(**over)}):
            return self.r.bed_not_ready()

    def test_a_finished_sequence_is_ready(self):
        self.assertEqual(self.missing(), [])

    def test_a_re_pin_owed_after_a_gnb_restart_blocks_the_board(self):
        # Board 471's bed: gnb2 restarted, epoch 963 -> 965, inventory still on 963.
        out = self.missing(_inventory_drift=lambda: {2816: [963, 965]})
        self.assertTrue(any('re-pin owed' in m for m in out), out)

    def test_a_dead_gnb_or_missing_e2_node_blocks_the_board(self):
        out = self.missing(gnb1_pids=lambda: [], _live_epochs=lambda: {2816: 965})
        self.assertIn('gnb1 process', out)
        self.assertTrue(any('3584' in m for m in out), out)

    def test_a_closed_producer_port_or_stale_token_blocks_the_board(self):
        token = mock.Mock()
        token.stat.return_value = types.SimpleNamespace(st_mtime=0.0)
        out = self.missing(_port_is_open=lambda host, port: port != 18443, R1_TOKEN=token)
        self.assertIn('port 18443 closed', out)
        self.assertTrue(any('R1 token' in m for m in out), out)


if __name__ == '__main__':
    unittest.main()
