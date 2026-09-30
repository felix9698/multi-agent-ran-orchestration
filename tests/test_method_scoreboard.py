"""부전승을 빼지 않으면 방식이 아니라 그날 베드를 비교하게 된다.

2026-09-23: v4.4 판 31개 중 17개가 부전승이었다 -- 기준선 C0 가 이미 어떤 T 를 충족해
에이전트가 움직이기 전에 이긴 판이다.  "성공은 아무 T 나 하나" 규칙이므로 완화된 열
하나만 통과해도 그 판은 방식을 구별하지 못한다.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_OPS = Path(__file__).resolve().parents[1] / 'experiment_results/ota-20260911/ops'
_spec = importlib.util.spec_from_file_location('method_scoreboard', _OPS / 'method_scoreboard.py')
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)


def _board(root, *, method, trials, intents=7):
    path = Path(root) / 'formal38guarded-20260923T000000-deadbeef'
    (path / 'evidence').mkdir(parents=True)
    (path / 'evidence' / 'AGENT-x-episode.json').write_text(
        json.dumps({'method': method, 'trials': trials, 'termination': {'reason': 'X'}}))
    if intents is not None:
        (path / 'intents.json').write_text(json.dumps([{}] * intents))
    return str(path)


class TestBoardSummary(unittest.TestCase):
    def test_a_baseline_that_passes_any_column_is_a_walkover(self):
        with tempfile.TemporaryDirectory() as root:
            p = _board(root, method='three-agent',
                       trials=[{'success': {'T0': False, 'T7': True}}])
            self.assertTrue(ms.board_summary(p)['walkover'])

    def test_a_baseline_that_passes_nothing_is_a_contest(self):
        with tempfile.TemporaryDirectory() as root:
            p = _board(root, method='three-agent',
                       trials=[{'success': {'T0': False, 'T7': False}}])
            self.assertFalse(ms.board_summary(p)['walkover'])

    def test_only_columns_the_agent_added_are_counted(self):
        """기준선이 이미 가진 열을 에이전트 공으로 세면 안 된다."""
        with tempfile.TemporaryDirectory() as root:
            p = _board(root, method='three-agent', trials=[
                {'success': {'T0': False, 'T7': True}},
                {'success': {'T0': True, 'T7': True}}])
            self.assertEqual(ms.board_summary(p)['gained'], 1, 'T0 하나만 에이전트의 것')

    def test_a_board_with_no_trials_is_not_summarised(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(ms.board_summary(_board(root, method='x', trials=[])))

    def test_the_corpus_size_is_carried_so_versions_do_not_mix(self):
        with tempfile.TemporaryDirectory() as root:
            p = _board(root, method='x', trials=[{'success': {}}], intents=3)
            self.assertEqual(ms.board_summary(p)['intents'], 3)

    def test_steering_appetite_is_measured_per_board(self):
        """방식 차이를 주장하려면 그 차이를 만드는 행동이 보여야 한다."""
        with tempfile.TemporaryDirectory() as root:
            p = _board(root, method='x', trials=[
                {'success': {}, 'configuration': {'servingCell@ue1': 'A', 'pfWeight@ue1': '1'}},
                {'success': {}, 'configuration': {'servingCell@ue1': 'A', 'pfWeight@ue1': '4'}},
                {'success': {}, 'configuration': {'servingCell@ue1': 'B', 'pfWeight@ue1': '1'}}])
            s = ms.board_summary(p)
            self.assertEqual((s['attempts'], s['steering']), (2, 1))
            self.assertFalse(s['first_is_steering'], '첫 시행은 PF 였다')

    def test_a_board_that_steers_first_is_flagged(self):
        with tempfile.TemporaryDirectory() as root:
            p = _board(root, method='x', trials=[
                {'success': {}, 'configuration': {'servingCell@ue1': 'A'}},
                {'success': {}, 'configuration': {'servingCell@ue1': 'B'}}])
            self.assertTrue(ms.board_summary(p)['first_is_steering'])

    def test_a_board_with_only_a_baseline_has_no_attempts(self):
        with tempfile.TemporaryDirectory() as root:
            p = _board(root, method='x', trials=[{'success': {}, 'configuration': {'servingCell@ue1': 'A'}}])
            s = ms.board_summary(p)
            self.assertEqual((s['attempts'], s['steering'], s['first_is_steering']), (0, 0, False))


if __name__ == '__main__':
    unittest.main()
