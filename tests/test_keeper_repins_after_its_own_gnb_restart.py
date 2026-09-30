"""epoch 을 움직인 게 우리면 판 사이를 기다릴 이유가 없다.

2026-09-23: keeper 가 gnb1 을 04:56 에 재기동해 E2 epoch 을 774 -> 776 으로 올렸다.
`ensure_a1p_inventory()` 는 `if not busy:` 안에만 있어 그 판이 끝날 때까지 돌지 않았고,
20:04 의 조종 정책은 생성 62 ms 뒤 `AIC_E2_NOT_READY` 로 거절됐다.  강제 재기동은 세 UE 를
이미 리셋하므로 그 판은 그 순간 희생된 것 -- 미루면 손해만 다음 판으로 넘어간다.
"""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

_OPS = Path(__file__).resolve().parents[1] / 'experiment_results/ota-20260911/ops'
_spec = importlib.util.spec_from_file_location('keeper_repin_test', _OPS / 'keeper.py')
keeper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(keeper)


class TestRepinAfterOurOwnRestart(unittest.TestCase):
    def setUp(self):
        keeper._gnb_restarted_at = 0.0
        self.calls = []
        self.logs = []
        self.drift = {3584: [774, 776]}
        self.p = [
            patch.object(keeper, 'ensure_a1p_inventory', lambda: self.calls.append('repin')),
            patch.object(keeper, '_inventory_drift', lambda: self.drift),
            patch.object(keeper, 'log', lambda ev, **kw: self.logs.append(ev)),
        ]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        keeper._gnb_restarted_at = 0.0

    def test_no_restart_of_ours_owes_nothing(self):
        keeper.repay_repin_owed_after_gnb_restart()
        self.assertEqual(self.calls, [])

    def test_a_repin_straight_after_the_restart_would_roll_back(self):
        # 22초 뒤에는 `KPM gate did not report both nodes` 로 실패했다.
        keeper._gnb_restarted_at = __import__('time').time() - 22
        keeper.repay_repin_owed_after_gnb_restart()
        self.assertEqual(self.calls, [])

    def test_past_the_settling_window_it_repins_even_mid_board(self):
        keeper._gnb_restarted_at = __import__('time').time() - keeper.A1P_REPIN_AFTER_GNB_S - 1
        keeper.repay_repin_owed_after_gnb_restart()
        self.assertEqual(self.calls, ['repin'])
        self.assertIn('A1P_REPIN_OWED_AFTER_GNB_RESTART', self.logs)

    def test_a_closed_drift_clears_the_debt_without_repinning(self):
        keeper._gnb_restarted_at = __import__('time').time() - keeper.A1P_REPIN_AFTER_GNB_S - 1
        self.drift = {}
        keeper.repay_repin_owed_after_gnb_restart()
        self.assertEqual(self.calls, [])
        self.assertEqual(keeper._gnb_restarted_at, 0.0)

    def test_the_debt_survives_a_repin_that_did_not_close_the_drift(self):
        keeper._gnb_restarted_at = __import__('time').time() - keeper.A1P_REPIN_AFTER_GNB_S - 1
        keeper.repay_repin_owed_after_gnb_restart()
        self.assertNotEqual(keeper._gnb_restarted_at, 0.0, '빚이 사라지면 다시 시도하지 않는다')

    def test_the_main_loop_repays_before_the_between_boards_block(self):
        # 2026-09-23: 주기 본문이 cycle() 로 옮겨져 들여쓰기가 4칸이 됐다.
        source = (_OPS / 'keeper.py').read_text()
        # 2026-09-27: start_exited_a1p_producer() now sits between the two lines; what matters is
        # that the repay is a top-level call of the cycle body that comes before the busy block.
        repay = source.index('\n    repay_repin_owed_after_gnb_restart()\n')
        busy = source.index('\n    if not busy:', repay)
        self.assertLess(repay, busy, 'busy 블록 바깥, 그 앞에서 불려야 한다')


if __name__ == '__main__':
    unittest.main()
