"""중단된 블록도 시도로 남아야 한다 (2026-09-21).

`run_block` 은 **완전한 매칭 블록만** 공개한다 — 중간에 예외가 나면 이미 완주한 판의
episode 파일도 쓰이지 않는다. 그 설계 자체는 옳다(반쪽 블록은 비교가 안 된다). 문제는
그렇게 사라진 블록이 **어디에도 남지 않아 분모를 알 수 없었다**는 것이다.

실측: 보드 593개 중 episode 기록은 322개(54%). 리포트는 N=322 를 모집단이라고 적었다.
불안정해서 죽은 판이 체계적으로 빠지므로 모든 비율이 낙관 쪽으로 치우친다.
"""
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from experiments import agent_episodes


class AnAbortedBlockIsRecorded(unittest.TestCase):
    def setUp(self):
        self.out = pathlib.Path(tempfile.mkdtemp())

    def _run(self):
        return agent_episodes.run_matrix(
            repetitions=1, out_dir=str(self.out), methods=('three-agent',))

    def test_the_manifest_names_the_attempt_that_died(self):
        with mock.patch.object(agent_episodes, 'run_block',
                               side_effect=ValueError('the radio went away')):
            with self.assertRaises(ValueError):
                self._run()
        manifest = json.loads((self.out / 'manifest.json').read_text())
        blocks = manifest['blocks']
        self.assertEqual(len(blocks), 1, '시도가 통째로 사라지면 분모를 알 수 없다')
        aborted = blocks[0]['aborted']
        self.assertIn('the radio went away', aborted['error'])
        self.assertIn('ValueError', aborted['error'])
        self.assertTrue(aborted['at'])

    def test_it_publishes_no_episodes_for_that_block(self):
        """반쪽 블록은 공개하지 않는다 -- 비교가 성립하지 않는다."""
        with mock.patch.object(agent_episodes, 'run_block',
                               side_effect=ValueError('boom')):
            with self.assertRaises(ValueError):
                self._run()
        block = json.loads((self.out / 'manifest.json').read_text())['blocks'][0]
        self.assertEqual(block['episodeIds'], [])
        self.assertFalse(block['repeatRequired'])

    def test_a_healthy_block_carries_no_aborted_key(self):
        row = {'episodeId': 'e0', 'method': 'three-agent'}
        with mock.patch.object(agent_episodes, 'run_block', return_value=[row]), \
             mock.patch.object(agent_episodes.agent_metrics, 'summarize', return_value={}), \
             mock.patch.object(agent_episodes.agent_figures, 'render_all', return_value={}):
            self._run()
        for block in json.loads((self.out / 'manifest.json').read_text())['blocks']:
            self.assertNotIn('aborted', block)


if __name__ == '__main__':
    unittest.main()
