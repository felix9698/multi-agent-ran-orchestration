"""A chain that lost a node is rebuilt whole, not repaired a piece at a time.

2026-09-21: a gNB re-registering into a running RIC aborts the RIC, both cells
drop, and only gnb1 returns by itself.  The keeper used to restart the stalled
gNB into that wreck and log a stale gate forever; recovery took three hours of
hand work, twice in two days.  The rebuild is one operation, it never runs
while somebody holds the radio, and it does not thrash.
"""
import importlib.util
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911' / 'ops'


def _load():
    spec = importlib.util.spec_from_file_location('keeper_chain', OPS / 'keeper.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Result:
    def __init__(self, stdout='', returncode=0):
        self.stdout, self.returncode = stdout, returncode


class RebuildsTheWholeChain(unittest.TestCase):
    def setUp(self):
        self.k = _load()
        self.ran = []
        self.logged = []
        self.k.run = lambda cmd, **kw: (self.ran.append(cmd[-1]), Result('READY\n'))[1]
        self.k.log = lambda event, **kw: self.logged.append(event)
        self.k.episode_running = lambda: False
        self.k.episode_in_sitting = lambda: False
        self.k._live_epochs = lambda: {}
        self.k._last_chain_rebuild = 0.0

    def test_bring_up_then_repin_in_that_order(self):
        self.assertTrue(self.k.rebuild_chain())
        self.assertEqual(2, len(self.ran))
        self.assertTrue(self.ran[0].endswith('bring_up_oran.sh'))
        self.assertTrue(self.ran[1].endswith('repin_a1p.sh'))
        self.assertIn('CHAIN_REBUILD_DONE', self.logged)

    def test_a_held_radio_is_never_touched(self):
        self.k.episode_running = lambda: True
        self.k.episode_in_sitting = lambda: True
        self.assertFalse(self.k.rebuild_chain())
        self.assertEqual([], self.ran)

    def test_a_bed_that_will_not_come_up_does_not_thrash(self):
        self.assertTrue(self.k.rebuild_chain())
        self.ran.clear()
        self.assertFalse(self.k.rebuild_chain())
        self.assertEqual([], self.ran)

    def test_a_bring_up_that_never_says_ready_stops_before_the_repin(self):
        self.k.run = lambda cmd, **kw: (self.ran.append(cmd[-1]),
                                        Result('두 노드가 다 붙지 않았다\n'))[1]
        self.assertFalse(self.k.rebuild_chain())
        self.assertEqual(1, len(self.ran))
        self.assertIn('CHAIN_REBUILD_FAILED', self.logged)


if __name__ == '__main__':
    unittest.main()
