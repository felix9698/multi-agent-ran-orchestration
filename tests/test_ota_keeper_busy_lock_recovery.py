"""Under the busy lock the Kernel owns the UE: a handover must not look like a fault.

2026-09-20 lost five boards in a row.  Each steering trial handed ue1 to the other
cell; for a few seconds after a handover the tun is up, the receive counter is
frozen and a UE-originated ping fails -- which is exactly the ``bearer-frozen``
evidence added on 2026-09-18.  The keeper restarted a healthy UE mid-trial, the
rollback's readback then found nothing, and the Kernel locked the trial down.

Evidence that cannot be a handover (absent softmodem, idle after release, an RA
loop, a released RNTI) still justifies a restart under the lock.
"""
import importlib.util
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911' / 'ops'


def _load_keeper():
    spec = importlib.util.spec_from_file_location('keeper_busy_lock', OPS / 'keeper.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DuringAnEpisode(unittest.TestCase):
    def setUp(self):
        self.keeper = _load_keeper()
        self.restarted = []
        self.logged = []
        self.keeper.restart_ue = lambda host, evidence=None: self.restarted.append((host, evidence))
        self.keeper.log = lambda event, **fields: self.logged.append((event, fields))
        self.keeper.episode_in_sitting = lambda: True
        self.keeper._busy_streak.clear()
        self.keeper._busy_streak.update({'ue1': 0})

    def _call(self, evidence):
        self.keeper.zombie_release_evidence = lambda host: evidence
        acted = False
        for _ in range(self.keeper.FAIL_STREAK):
            # any call may be the restart: a UE under the grace is re-judged every cycle
            acted = self.keeper.recover_during_episode('ue1', False) or acted
        return acted

    def test_a_frozen_bearer_is_left_alone_inside_the_handover_window(self):
        self.keeper._frozen_since.clear()
        self.assertFalse(self._call('bearer-frozen'))
        self.assertEqual([], self.restarted)
        self.assertIn('UE_BEARER_FROZEN_DURING_EPISODE', [event for event, _ in self.logged])

    def test_a_freeze_that_outlives_the_window_is_restarted(self):
        """2026-09-21: ue1 froze for 25 minutes and three boards died on
        LEASE_EXPIRED while composition waited for a UE nobody could repair."""
        self.keeper._frozen_since.clear()
        self._call('bearer-frozen')                      # first sighting
        self.keeper._frozen_since['ue1'] -= self.keeper.BEARER_FROZEN_GRACE_S + 1
        self.assertTrue(self._call('bearer-frozen'))
        self.assertEqual([('ue1', 'bearer-frozen')], self.restarted)
        self.assertIn('UE_BEARER_FROZEN_TOO_LONG', [event for event, _ in self.logged])

    def test_an_absent_softmodem_is_still_restarted(self):
        self.assertTrue(self._call('softmodem-absent'))
        self.assertEqual([('ue1', 'softmodem-absent')], self.restarted)

    def test_a_released_rnti_is_still_restarted(self):
        self.assertTrue(self._call('af5f'))
        self.assertEqual([('ue1', 'af5f')], self.restarted)


if __name__ == '__main__':
    unittest.main()
