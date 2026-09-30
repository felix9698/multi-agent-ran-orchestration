"""An excess streak and its deferrals end when the excess does (2026-09-24 board 769).

09:53 gnb2 showed excess twice (streak 2), then five HARQ-only cycles, then at 09:59 one
transient excess while a steering trial swapped ue2 onto gnb2.  The streak carried over the
gap and hit 3, the deferral counter had kept every deferral since 09:50, and the keeper forced
a gnb2 restart mid-trial -- the trial locked down and the case ended RECOVERY_FAILURE.
"""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

OPS = Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911' / 'ops'


def _load(real_release=False):
    sys.path.insert(0, str(OPS))
    try:
        spec = importlib.util.spec_from_file_location('keeper_streak', OPS / 'keeper.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not real_release:
            module.release_zombie_contexts = lambda *_a, **_k: []   # hermetic: no ssh/telnet to the bed
        return module
    finally:
        sys.path.remove(str(OPS))


def _tail(*rntis):
    return ''.join(f'UE {r}: ulsch_rounds 1/2/3\n' for r in rntis)


class TheStreakBreaksOnACleanCycle(unittest.TestCase):
    def setUp(self):
        self.k = _load()
        self.restarts = []
        self.broken = []
        self.k.log = lambda *a, **kw: None
        self.k._save_state = lambda: None
        self.p = [
            patch.object(self.k, 'restart_gnb1', lambda *a, **kw: self.restarts.append('gnb1')),
            patch.object(self.k, 'restart_gnb2', lambda *a, **kw: self.restarts.append('gnb2')),
            patch.object(self.k, 'broken_harq_ues', lambda t: list(self.broken) if 'bbbb' in t else []),
            patch.object(self.k, 'budget', lambda _c: True),
            patch.object(self.k, 'episode_running', lambda: True),
            patch.object(self.k, 'episode_in_sitting', lambda: True),
        ]
        for p in self.p:
            p.start()
            self.addCleanup(p.stop)

    def _cycle(self, gnb2_rntis):
        with patch.object(self.k, '_gnb1_tail', lambda *_a, **_k: _tail('aaaa')), \
             patch.object(self.k, '_gnb2_tail', lambda *_a, **_k: _tail(*gnb2_rntis)):
            self.k.ensure_no_stale_contexts()

    def test_harq_only_cycles_between_two_excess_sightings_reset_the_streak(self):
        excess = ('bbbb', 'cccc', 'dddd')          # gnb2 placed 2, one extra
        clean = ('bbbb', 'cccc')
        self.broken = [('bbbb', 0.02)]            # HARQ-only in between, as on board 769
        self._cycle(excess)
        self._cycle(excess)
        for _ in range(5):
            self._cycle(clean)
        self._cycle(excess)                        # the steering transient
        self.assertEqual([], self.restarts)
        self.assertEqual(1, self.k._stale_seen.get('gnb2'))

    def test_a_context_deferral_is_forgotten_once_the_cell_is_clean(self):
        self.k._gnb_restart_deferred['gnb2'] = 3
        self.k._gnb_restart_deferred_cause['gnb2'] = 'excess'
        self._cycle(('bbbb', 'cccc'))
        self.assertNotIn('gnb2', self.k._gnb_restart_deferred)

    def test_a_radio_deferral_is_not_forgotten_by_a_clean_context_count(self):
        """A dead radio shows no contexts, so it always looks clean."""
        self.k._gnb_restart_deferred['gnb2'] = 3
        self.k._gnb_restart_deferred_cause['gnb2'] = 'radio'
        self._cycle(('bbbb', 'cccc'))
        self.assertEqual(3, self.k._gnb_restart_deferred.get('gnb2'))

    def test_a_persistent_excess_still_restarts(self):
        for _ in range(self.k.STALE_PATIENCE):
            self._cycle(('bbbb', 'cccc', 'dddd'))
        self.assertEqual(['gnb2'], self.restarts)


class ZombieContextsAreReleasedNotRestarted(unittest.TestCase):
    """2026-09-24 11:0x-11:36: a gNB restart per leftover context looped the bed."""

    def setUp(self):
        self.k = _load(real_release=True)             # the real one, with its I/O stubbed below
        self.k.log = lambda *a, **kw: None
        self.sent = []
        self.k._cell_telnet = lambda cell, cmd: self.sent.append((cell, cmd)) or 'softmodem_gnb> ok'
        self.k._cell_runs_checked_release = lambda cell: True
        self.k.time.sleep = lambda *_a: None
        self.k.episode_in_sitting = lambda: False

    def tearDown(self):
        import time as _t
        self.k.time.sleep = _t.sleep

    def test_only_rntis_no_ue_holds_are_released(self):
        self.k._ue_held_rntis = lambda: {'ue1': 'b864', 'ue2': 'bb96', 'ue3': '73e4'}
        out = self.k.release_zombie_contexts('gnb2', {'b864', '73e4', '0869', 'c510'})
        self.assertEqual(['0869', 'c510'], out)
        self.assertIn(('gnb2', 'ci force_ul_failure 0869'), self.sent)
        self.assertNotIn(('gnb2', 'ci force_ul_failure b864'), self.sent)

    def test_a_gnb_still_running_the_unchecked_telnet_is_left_alone(self):
        """12:11 gnb2 died on `ci force_ul_failure 0006`; only the patched library may be asked."""
        self.k._cell_runs_checked_release = lambda cell: False
        self.k._ue_held_rntis = lambda: {'ue1': 'b864', 'ue2': 'bb96', 'ue3': '73e4'}
        self.assertEqual([], self.k.release_zombie_contexts('gnb2', {'0869'}))
        self.assertEqual([], self.sent)

    def test_an_empty_answer_stops_the_release(self):
        """The ssh+nc path returns '' when gnb2 is gone; that is not an answer."""
        self.k._ue_held_rntis = lambda: {'ue1': 'b864', 'ue2': 'bb96', 'ue3': '73e4'}
        self.k._cell_telnet = lambda cell, cmd: self.sent.append((cell, cmd)) or ''
        self.assertEqual(['0006'], self.k.release_zombie_contexts('gnb2', {'0006', '024c'}))
        self.assertNotIn(('gnb2', 'ci force_ul_failure 024c'), self.sent)

    def test_an_unreadable_ue_releases_nothing(self):
        self.k._ue_held_rntis = lambda: None
        self.assertEqual([], self.k.release_zombie_contexts('gnb2', {'0869'}))
        self.assertEqual([], self.sent)

    def test_inside_a_board_only_a_finished_instances_rnti_is_released(self):
        """A handover's new context is not held by any UE log yet; only an RNTI a dead UE
        instance last held is certainly nobody's handover (11:47 ue2 died, 11:55 gnb1 forced)."""
        self.k.episode_in_sitting = lambda: True
        self.k._ue_held_rntis = lambda: {'ue1': 'b864', 'ue2': 'bb96', 'ue3': '73e4'}
        self.k._retired_rntis = lambda: {'0869'}
        self.assertEqual(['0869'], self.k.release_zombie_contexts('gnb2', {'b864', '0869', 'c510'}))
        self.assertNotIn(('gnb2', 'ci force_ul_failure c510'), self.sent)


if __name__ == '__main__':
    unittest.main()
