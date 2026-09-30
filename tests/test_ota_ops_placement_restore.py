"""A board that ends with a UE on the wrong cell is followed by a re-pin.

2026-09-20: two boards ran with all three UEs on gnb1 and gnb2 empty.  A
steering trial had carried ue1 off, the gateway's hand-back was never observed
so the trial locked down, and nothing moved the UE afterwards -- the placement
was only ever checked before the *next* board, which then inherited the broken
bed.  The repair belongs right after the board, where no verdict can be
polluted by a UE restart.
"""
import importlib.util
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911' / 'ops'


def _load_runner():
    spec = importlib.util.spec_from_file_location('run_episode_placement', OPS / 'run_episode.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


import sys
import types
from unittest import mock


def _fake_keeper(headers):
    keeper = types.ModuleType('keeper')
    keeper.CELL_OF = {'ue1': 'gnb2', 'ue2': 'gnb1', 'ue3': 'gnb2'}
    keeper.CELL_NB = {'gnb1': 3584, 'gnb2': 2816}
    keeper._header_amf = headers.get
    return keeper


class FindsAUeOnTheWrongCellEvenWhenNoCellIsEmpty(unittest.TestCase):
    """2026-09-23 board 153808: ue3 sat on gnb1 beside ue2 while gnb2 still
    carried ue1 -- no cell empty, so the old empty-cell test re-pinned nothing
    and the board took the wrong cell as its baseline."""

    def setUp(self):
        self.runner = _load_runner()
        self.saved = sys.modules.get('keeper')
        sys.modules['keeper'] = _fake_keeper({'ue1': 25, 'ue2': 26, 'ue3': 24})
        self.addCleanup(self._restore)

    def _restore(self):
        if self.saved is None:
            sys.modules.pop('keeper', None)
        else:
            sys.modules['keeper'] = self.saved

    def _attributed(self, mapping):
        self.runner.kpm_attribution = lambda *a, **k: {nb: set(ids) for nb, ids in mapping.items()}
        self.runner.kpm_identities = lambda *a, **k: {nb: frozenset(ids) for nb, ids in mapping.items()}

    def test_the_153808_bed_names_ue3(self):
        self._attributed({2816: {25}, 3584: {26, 24}})
        self.assertEqual(['ue3'], self.runner._hosts_off_their_cell())

    def test_the_placed_bed_names_nobody(self):
        self._attributed({2816: {25, 24}, 3584: {26}})
        self.assertEqual([], self.runner._hosts_off_their_cell())

    def test_a_ue_seen_on_both_cells_mid_handover_is_left_alone(self):
        self._attributed({2816: {25, 24}, 3584: {26, 24}})
        self.assertEqual([], self.runner._hosts_off_their_cell())

    def test_an_identity_no_cell_attributes_decides_nothing(self):
        # ue3's header names an id that re-registered away: not evidence of a cell.
        self._attributed({2816: {25, 31}, 3584: {26}})
        self.assertEqual([], self.runner._hosts_off_their_cell())

    def test_an_empty_placed_cell_still_names_its_hosts(self):
        self._attributed({3584: {26, 25, 24}})
        self.assertEqual(['ue1', 'ue3'], self.runner._hosts_off_their_cell())

    def test_the_gate_repins_an_off_cell_ue_once_before_the_board(self):
        runner = self.runner
        repinned = []
        runner.repin_ue = repinned.append
        runner.ue_carries_downlink = lambda host: '12.1.1.9'
        runner.kpm_ready = lambda *a: ''
        runner.identities_stable = lambda *a, **k: ''
        runner.kpm_identities = lambda *a, **k: {}
        runner._hosts_off_their_cell = lambda: ['ue3']   # a re-pin that does not take
        with mock.patch.object(runner.time, 'sleep', lambda s: None):
            self.assertTrue(runner.wait_for_ues(limit_s=5))
        self.assertEqual(['ue3'], repinned)


class RestoresThePlacementAfterTheBoard(unittest.TestCase):
    def setUp(self):
        self.runner = _load_runner()
        self.repinned = []
        self.runner.repin_ue = self.repinned.append

    def test_a_host_off_its_placed_cell_is_repinned(self):
        self.runner._hosts_off_their_cell = lambda: ['ue1']
        self.runner.restore_placement()
        self.assertEqual(['ue1'], self.repinned)

    def test_a_bed_that_is_where_it_belongs_is_left_alone(self):
        self.runner._hosts_off_their_cell = lambda: []
        self.runner.restore_placement()
        self.assertEqual([], self.repinned)

    def test_a_failing_check_never_masks_the_boards_own_exit(self):
        def boom():
            raise OSError('KPM unreadable')
        self.runner._hosts_off_their_cell = boom
        self.runner.restore_placement()   # must not raise
        self.assertEqual([], self.repinned)


if __name__ == '__main__':
    unittest.main()
