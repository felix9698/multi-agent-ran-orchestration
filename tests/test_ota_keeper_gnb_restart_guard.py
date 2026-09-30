"""판이 도는 중의 gNB 재기동은 미루되, 무한히 미루지는 않는다 (2026-09-21).

gNB 재기동 한 번이 두 셀을 내리고 세 UE 의 부착 시계를 0 으로 돌린다. 러너는 세 UE 가
동시에 버텨야 판을 시작하므로 판 도중의 재기동은 그 판을 확실히 죽인다.

그런데 2026-09-21 에 나는 "이 유예가 교착을 만든다" 고 의심해 가드를 통째로 껐다.
그날 교착의 진짜 원인은 `att_tx` 기준선이었고, 가드는 무죄였다 — 끄는 바람에 keeper 가
판 도중 gNB 를 재기동할 수 있는 상태로 한동안 돌았다.

되살리되 한도를 둔다: `gnb1_stalled()` 이 참이면 RF 가 이미 죽어 그 판은 어차피 성공할
수 없으므로, 짧게만 미루고 그 뒤에는 고친다.
"""
import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

OPS = (pathlib.Path(__file__).resolve().parents[1]
       / 'experiment_results' / 'ota-20260911' / 'ops')


def _keeper():
    sys.path.insert(0, str(OPS))
    try:
        spec = importlib.util.spec_from_file_location('ota_keeper_guard', OPS / 'keeper.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(OPS))


class TheRestartGuard(unittest.TestCase):
    def setUp(self):
        self.k = _keeper()
        self.k._gnb_restart_deferred.clear()

    def _defer(self, busy, target='gnb1'):
        with mock.patch.object(self.k, 'episode_in_sitting', return_value=busy), \
             mock.patch.object(self.k, 'log'):
            return self.k._defer_gnb_restart(target)

    def test_an_idle_bed_is_never_deferred(self):
        self.assertFalse(self._defer(busy=False))

    def test_a_running_episode_defers_the_restart(self):
        self.assertTrue(self._defer(busy=True))

    def test_a_pre_board_lock_does_not_defer_the_restart(self):
        """2026-09-24 02:52~03:16: the runner held the lock while awaiting UEs that could
        not attach to a gnb1 full of dead contexts, and the keeper waited for that
        "episode" to end -- each waited for the other for 24 minutes."""
        with mock.patch.object(self.k, 'episode_running', return_value=True), \
             mock.patch.object(self.k, 'episode_in_sitting', return_value=False), \
             mock.patch.object(self.k, 'log'):
            self.assertFalse(self.k._defer_gnb_restart('gnb1'))

    def test_it_stops_deferring_once_the_limit_is_reached(self):
        """무한 유예는 진짜 RF 고장을 영영 못 고치게 만든다."""
        for _ in range(self.k.GNB_RESTART_DEFER_CYCLES):
            self.assertTrue(self._defer(busy=True))
        self.assertFalse(self._defer(busy=True), '한도를 넘으면 고쳐야 한다')

    def test_the_counter_resets_when_the_bed_frees_up(self):
        self._defer(busy=True)
        self._defer(busy=False)
        self.assertNotIn('gnb1', self.k._gnb_restart_deferred)
        self.assertTrue(self._defer(busy=True), '다음 판은 다시 온전한 유예를 받는다')

    def test_the_two_cells_are_counted_apart(self):
        for _ in range(self.k.GNB_RESTART_DEFER_CYCLES):
            self._defer(busy=True, target='gnb1')
        self.assertTrue(self._defer(busy=True, target='gnb2'),
                        'gnb1 을 미룬 횟수가 gnb2 의 유예를 깎으면 안 된다')

    def test_it_uses_the_same_occupancy_check_as_the_chain_rebuild(self):
        """판정이 둘이면 언젠가 갈린다 -- rebuild_chain 과 같은 함수를 쓴다."""
        import inspect
        self.assertIn('episode_in_sitting', inspect.getsource(self.k._defer_gnb_restart))
        self.assertIn('episode_in_sitting', inspect.getsource(self.k.rebuild_chain))


if __name__ == '__main__':
    unittest.main()
