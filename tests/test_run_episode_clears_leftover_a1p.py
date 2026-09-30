"""2026-09-29 boards 885/895: a steering policy outlived its board and held a live UE's scope, so
the next board's steer was refused 409 AIC_POLICY_CONFLICT.  The runner withdraws every A1-P
policy left at the producer once it holds the busy lock.  Hermetic: a fake scope module."""
import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

OPS = Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911' / 'ops'


def _load_runner():
    spec = importlib.util.spec_from_file_location('run_episode_leftover_t', OPS / 'run_episode.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless((OPS / 'run_episode.py').is_file(), 'OTA ops scripts not present')
class LeftoverPoliciesAreWithdrawn(unittest.TestCase):
    def _fake(self, ids, status=204):
        db = Path(tempfile.mkdtemp()) / 'a1p.sqlite3'
        con = sqlite3.connect(db)
        con.execute('create table policies (policy_id text)')
        con.executemany('insert into policies values (?)', [(i,) for i in ids])
        con.commit(); con.close()
        deleted = []
        fake = types.ModuleType('clear_ue_scope')
        fake.DB = str(db)
        fake.delete = lambda pid, seq: (deleted.append(pid), (status, ''))[1]
        return fake, deleted

    def test_every_leftover_is_deleted(self):
        runner = _load_runner()
        fake, deleted = self._fake(['aaaaaaaa-1', 'bbbbbbbb-2'])
        with patch.dict(sys.modules, {'clear_ue_scope': fake}):
            gone = runner.clear_leftover_a1p_policies()
        self.assertEqual(['aaaaaaaa-1', 'bbbbbbbb-2'], deleted)
        self.assertEqual(deleted, gone)

    def test_an_unreadable_producer_does_not_stop_the_board(self):
        runner = _load_runner()
        fake = types.ModuleType('clear_ue_scope')
        fake.DB = '/nonexistent/dir/a1p.sqlite3'
        fake.delete = lambda pid, seq: (500, '')
        with patch.dict(sys.modules, {'clear_ue_scope': fake}):
            self.assertEqual([], runner.clear_leftover_a1p_policies())


if __name__ == '__main__':
    unittest.main()
