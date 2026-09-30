"""성공한 조종은 좀비가 아니다.

2026-09-23: `CELL_OF` 는 고정 지도인데 조종 축은 UE 를 옮긴다.  옮겨간 셀은
`excess: 1` 로 보였고 3연속이면 gNB 가 재기동됐다.  재기동 → E2 epoch 상승 →
A1-P 인벤토리 어긋남 → 다음 판 조종이 `AIC_E2_NOT_READY` → `RECOVERY_FAILURE`.
가드가 실험을 죽이는 닫힌 고리였다.  UE 수는 보존되므로 판정은 **두 셀 합**이다.
"""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

_OPS = Path(__file__).resolve().parents[1] / 'experiment_results/ota-20260911/ops'
_spec = importlib.util.spec_from_file_location('keeper_under_test', _OPS / 'keeper.py')
keeper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(keeper)
keeper.release_zombie_contexts = lambda *_a, **_k: []   # hermetic: no ssh/telnet to the bed


def _tail(*rntis):
    return ''.join(f'UE {r}: ulsch_rounds 1/2/3\n' for r in rntis)


class TestHandoverIsNotAZombie(unittest.TestCase):
    def setUp(self):
        keeper._stale_seen.clear()
        # 인내는 test_ota_keeper_stale_contexts 가 본다; 여기는 핸드오버/좀비 분류만.
        self._patience = keeper.STALE_PATIENCE
        keeper.STALE_PATIENCE = 1
        self.addCleanup(setattr, keeper, 'STALE_PATIENCE', self._patience)
        self.restarts = []
        self.logs = []
        self.p = [
            patch.object(keeper, 'restart_gnb1', lambda *a, **k: self.restarts.append('gnb1')),
            patch.object(keeper, 'restart_gnb2', lambda *a, **k: self.restarts.append('gnb2')),
            patch.object(keeper, 'broken_harq_ues', lambda _t: []),
            patch.object(keeper, 'budget', lambda _c: True),
            patch.object(keeper, 'episode_running', lambda: False),
            patch.object(keeper, 'episode_in_sitting', lambda: False),
            patch.object(keeper, 'log', lambda ev, **kw: self.logs.append((ev, kw))),
        ]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def _run(self, gnb1, gnb2):
        with patch.object(keeper, '_gnb1_tail', lambda *_a, **_k: _tail(*gnb1)), \
             patch.object(keeper, '_gnb2_tail', lambda *_a, **_k: _tail(*gnb2)):
            keeper.ensure_no_stale_contexts()

    def test_a_ue_steered_onto_the_other_cell_is_not_a_zombie(self):
        # 배치는 gnb1=1(ue2), gnb2=2.  조종이 ue1 을 gnb1 으로 옮기면 2 대 1 이 되지만
        # 합은 여전히 3 이다 -- 아무도 죽지 않았다.
        self._run(gnb1=('aaaa', 'bbbb'), gnb2=('cccc',))
        self.assertEqual(self.restarts, [])

    def test_a_real_zombie_still_restarts_its_cell(self):
        # 합이 4 -- 하나는 어느 쪽에도 살아 있을 수 없다.
        self._run(gnb1=('aaaa', 'bbbb'), gnb2=('cccc', 'dddd'))
        # The cell holding more live contexts than UEs placed on it (placement from CELL_OF:
        # with ue2 and ue3 on gnb1 since placement B, that is gnb2).
        placed = {c: sum(1 for v in keeper.CELL_OF.values() if v == c) for c in ('gnb1', 'gnb2')}
        self.assertEqual(self.restarts, [c for c in ('gnb1', 'gnb2') if 2 > placed[c]])

    def test_the_baseline_placement_is_quiet(self):
        self._run(gnb1=('aaaa',), gnb2=('bbbb', 'cccc'))
        self.assertEqual(self.restarts, [])

    def test_an_unreadable_cell_decides_nothing(self):
        with patch.object(keeper, '_gnb1_tail', lambda *_a, **_k: ''), \
             patch.object(keeper, '_gnb2_tail', lambda *_a, **_k: _tail('a', 'b', 'c', 'd')):
            keeper.ensure_no_stale_contexts()
        self.assertEqual(self.restarts, [])

    def test_broken_harq_still_restarts_even_when_the_count_is_right(self):
        # 2026-09-23: broken_harq_ues 는 (rnti, 비율) 을 준다; 판 밖에서 UE 를 못 찾으면 셀 재기동.
        with patch.object(keeper, 'broken_harq_ues', lambda t: [('aaaa', 0.02)] if 'aaaa' in t else []), \
             patch.object(keeper, '_host_of_rnti', lambda rnti: None):
            self._run(gnb1=('aaaa',), gnb2=('bbbb', 'cccc'))
        self.assertEqual(self.restarts, ['gnb1'])

    def test_active_rntis_ignores_a_released_context(self):
        text = 'UE dead: released\nUE aaaa: ulsch_rounds 1/2\n'
        self.assertEqual(keeper.active_rntis(text), {'aaaa'})


if __name__ == '__main__':
    unittest.main()
