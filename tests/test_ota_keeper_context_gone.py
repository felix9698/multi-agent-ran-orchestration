"""A frozen UE whose RNTI no gNB holds skips the handover grace (2026-09-24 21:14-21:20, ue1)."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911' / 'ops'


def _load():
    sys.path.insert(0, str(OPS))
    try:
        spec = importlib.util.spec_from_file_location('keeper_ctx', OPS / 'keeper.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(OPS))


class ContextGone(unittest.TestCase):
    def setUp(self):
        self.k = _load()
        self.k.log = lambda *a, **kw: None
        self.restarts = []
        self.k.restart_ue = lambda host, evidence=None: self.restarts.append((host, evidence))
        self.k.episode_in_sitting = lambda: True
        self.k.zombie_release_evidence = lambda host: 'bearer-frozen'
        self.k.gnb1_log = lambda: Path('/gnb1.log')
        self.gnb2 = 'UE f45d: dlsch_rounds 1/0/0/0\n'
        # keeper judges gnb1 on its last stats blocks only (09-24 22:33), so the fake log has some.
        self.gnb1 = 'Frame.Slot 0.0\nUE c5df: dlsch_rounds 1/0/0/0\n' * 6
        self.ue_tail = 'RNTI adb7 stats\n'

        def ssh(host, cmd, timeout=20):
            if host == 'enb2':
                return types.SimpleNamespace(returncode=0, stdout=self.gnb2)
            return types.SimpleNamespace(returncode=0, stdout=self.ue_tail)
        self.k.ssh = ssh
        self.k.run = lambda cmd, timeout=60: types.SimpleNamespace(returncode=0, stdout=self.gnb1)

    def _cycles(self):
        for _ in range(self.k.FAIL_STREAK):
            self.k.recover_during_episode('ue1', healthy=False)

    def test_rnti_on_no_gnb_restarts_without_the_grace(self):
        self._cycles()
        self.assertEqual(self.restarts, [('ue1', 'context-gone')])

    def test_rnti_on_the_other_cell_is_a_handover_and_waits(self):
        self.gnb1 += 'UE adb7: dlsch_rounds 5/0/0/0\n'
        self._cycles()
        self.assertEqual(self.restarts, [])

    def test_a_handover_after_the_last_stats_line_keeps_the_grace(self):
        self.ue_tail = 'Processing reconfigurationWithSync\n'
        self._cycles()
        self.assertEqual(self.restarts, [])

    def test_unreadable_gnb2_keeps_the_grace(self):
        self.gnb2 = ''
        self._cycles()
        self.assertEqual(self.restarts, [])


if __name__ == '__main__':
    unittest.main()
