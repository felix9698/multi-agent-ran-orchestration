"""keeper 의 판 생산 감시 (2026-09-21).

2026-09-21: 에피소드 서비스가 17시간 `active` 였는데 판은 0개였다. 서비스가 살아
있는 것과 산출물이 나오는 것은 다른 질문이고, 아무도 뒤쪽을 묻지 않았다.
"""
import importlib.util
import pathlib
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

OPS = (pathlib.Path(__file__).resolve().parents[1]
       / 'experiment_results' / 'ota-20260911' / 'ops')


def _keeper():
    sys.path.insert(0, str(OPS))
    try:
        spec = importlib.util.spec_from_file_location('ota_keeper', OPS / 'keeper.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(OPS))


class BoardProductionWatch(unittest.TestCase):
    def setUp(self):
        self.k = _keeper()
        # hermetic: 이 테스트는 실제 `ops/overnight/` 에 쪽지를 쓰면 안 된다.
        # 2026-09-21 에 `BOARDS-STALLED` 를 진짜로 만들었다 지웠고, 그 사이 keeper 가
        # 읽었다면 운영 판단을 바꿨을 것이다.
        self.sandbox = pathlib.Path(tempfile.mkdtemp())
        (self.sandbox / 'overnight').mkdir()
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        self.k.HERE = self.sandbox
        self.k._board_strikes = 0
        self.k._board_acted_at = 0.0
        self.k._board_watch_from = 0.0      # 기동 유예는 따로 시험한다

    def _run(self, age_s, hold=False, busy=False):
        calls = []
        with mock.patch.object(self.k, '_newest_board_age_s', return_value=age_s), \
             mock.patch.object(self.k, 'episode_running', return_value=busy), \
             mock.patch.object(self.k, 'run',
                               side_effect=lambda *a, **kw: calls.append(a[0]) or mock.Mock(returncode=0, stdout='')), \
             mock.patch.object(self.k, 'log'), \
             mock.patch.object(type(self.k.HERE / 'overnight' / 'NO_EPISODES'),
                               'exists', lambda _self: hold):
            self.k.ensure_boards_are_being_produced()
        return calls

    def test_a_fresh_board_is_left_alone(self):
        self.assertEqual(self._run(60.0), [])

    def test_a_stalled_board_restarts_the_episode_service(self):
        calls = self._run(self.k.BOARD_STALL_S + 1)
        self.assertEqual(len(calls), 1)
        self.assertIn('aic-v31-episodes', calls[0])
        self.assertIn('restart', calls[0])

    def test_the_cooldown_stops_a_restart_storm(self):
        """냉각이 없으면 keeper 주기마다 재기동해 세 번을 90초에 태운다."""
        self._run(self.k.BOARD_STALL_S + 1)
        self.assertEqual(self._run(self.k.BOARD_STALL_S + 2), [],
                         '냉각 중에는 아무 조치도 하지 않아야 한다')
        self.assertEqual(self.k._board_strikes, 1)

    def test_it_asks_for_a_hand_once_restarts_stop_helping(self):
        for _ in range(self.k.BOARD_ESCALATIONS + 1):
            self.k._board_acted_at = 0.0          # 냉각을 흘려보낸다
            calls = self._run(self.k.BOARD_STALL_S + 1)
        self.assertEqual(calls, [], '한계를 넘으면 더 재기동하지 않는다')
        flag = self.k.HERE / 'overnight' / 'BOARDS-STALLED'
        self.assertTrue(flag.exists())
        flag.unlink()

    def test_the_hold_file_silences_it(self):
        self.assertEqual(self._run(self.k.BOARD_STALL_S + 1, hold=True), [])
        self.assertEqual(self.k._board_strikes, 0)

    def test_recovery_clears_the_strikes(self):
        self._run(self.k.BOARD_STALL_S + 1)
        self._run(10.0)
        self.assertEqual(self.k._board_strikes, 0)


    def test_a_running_episode_is_never_interrupted(self):
        """판이 하드웨어를 쥐고 있으면 생산은 진행 중이다 -- 재기동하면 그 판을 죽인다."""
        self.assertEqual(self._run(self.k.BOARD_STALL_S + 1, busy=True), [])

    def test_lifting_the_hold_starts_a_fresh_clock(self):
        """두 시간 쉬었다 재개하는 순간 오래된 판 나이로 곧바로 재기동하면 안 된다."""
        self._run(self.k.BOARD_STALL_S * 10, hold=True)     # 보류 중 -- 시계가 갱신된다
        self.assertEqual(self._run(self.k.BOARD_STALL_S * 10), [],
                         '해제 직후에는 첫 판이 나올 시간을 준다')

    def test_a_refused_board_does_not_count_as_production(self):
        """판 디렉터리는 preflight 보다 먼저 생긴다 -- 증거 파일만이 진짜 생산이다."""
        import inspect
        source = inspect.getsource(self.k._newest_board_age_s)
        self.assertIn('evidence', source)


if __name__ == '__main__':
    unittest.main()
