"""조종 전수 판정의 규칙을 못 박는다.

2026-09-23: 같은 질문에 세 번 다른 답을 냈고, 틀린 이유는 매번 **표본이 질문의 창을
덮지 않았다**는 것이었다.  특히 두 번째 오답은 KPM 파일 끝 근처의 명령을 전부
"사라짐" 으로 읽은 것이다 -- 관측 부재는 고장이 아니다.
"""
import importlib.util
import unittest
from pathlib import Path

_OPS = Path(__file__).resolve().parents[1] / 'experiment_results/ota-20260911/ops'
_spec = importlib.util.spec_from_file_location('steering_census', _OPS / 'steering_census.py')
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)

S = 1_000_000  # 1초


def _obs(amf, *pairs):
    return {amf: [(t * S, nb) for t, nb in pairs]}


class TestCensus(unittest.TestCase):
    def test_a_move_that_reaches_the_target_counts_as_reached(self):
        obs = _obs(400, (0, 2816), (10, 2816), (20, 3584), (300, 3584))
        r, v = sc.census([(10 * S, 400, 3584)], obs, window=(0, 300 * S))
        self.assertEqual(r['2816->3584 도달'], 1)
        self.assertEqual(v, [])

    def test_a_ue_that_stops_publishing_counts_as_vanished(self):
        obs = _obs(400, (0, 2816), (10, 2816), (53, 2816))
        r, v = sc.census([(10 * S, 400, 3584)], obs, window=(0, 400 * S))
        self.assertEqual(r['2816->3584 사라짐'], 1)
        self.assertEqual(v, [43])

    def test_the_end_of_the_observation_window_is_not_a_fault(self):
        """내 2번째 오답. 파일이 끝난 것이지 UE 가 사라진 것이 아니다."""
        obs = _obs(400, (0, 2816), (10, 2816), (53, 2816))
        r, v = sc.census([(10 * S, 400, 3584)], obs, window=(0, 53 * S))
        self.assertEqual(r['창 밖/여유 부족'], 1)
        self.assertEqual(v, [])
        self.assertNotIn('2816->3584 사라짐', r)

    def test_a_ue_that_survives_without_reaching_is_its_own_class(self):
        obs = _obs(400, *[(t, 2816) for t in range(0, 400, 10)])
        r, _ = sc.census([(10 * S, 400, 3584)], obs, window=(0, 500 * S))
        self.assertEqual(r['2816->3584 도달못함(생존)'], 1)

    def test_duplicate_status_events_for_one_command_are_one_command(self):
        obs = _obs(400, (0, 2816), (10, 2816), (20, 3584), (300, 3584))
        cmds = [(10 * S, 400, 3584), (10 * S + 40_000, 400, 3584), (10 * S + 90_000, 400, 3584)]
        r, _ = sc.census(cmds, obs, window=(0, 300 * S))
        self.assertEqual(r['2816->3584 도달'], 1, '한 이동이 세 번 세어지면 안 된다')

    def test_pinning_a_ue_to_the_cell_it_already_holds_is_not_a_move(self):
        obs = _obs(400, (0, 2816), (10, 2816), (300, 2816))
        r, _ = sc.census([(10 * S, 400, 2816)], obs, window=(0, 300 * S))
        self.assertEqual(r['이미 목표(기준선)'], 1)

    def test_a_ue_absent_from_the_stream_is_not_judged(self):
        r, _ = sc.census([(10 * S, 999, 3584)], _obs(400, (0, 2816)), window=(0, 300 * S))
        self.assertEqual(r['창 밖/여유 부족'], 1)

    def test_the_cell_map_matches_the_deployed_conf(self):
        self.assertEqual(sc.NB_OF_NCI, {12345678: 3584, 87654321: 2816})


class TestAgreement(unittest.TestCase):
    """커널의 잠금이 무선의 실제 결과를 재고 있는가."""

    def test_a_reached_move_that_locked_lands_in_its_own_cell(self):
        obs = _obs(400, (0, 2816), (10, 2816), (20, 3584), (300, 3584))
        r = sc.agreement([(10 * S, 400, 3584, True)], obs, window=(0, 300 * S))
        self.assertEqual(r[('도달', '잠금')], 1)

    def test_a_reached_move_that_settled_lands_in_another(self):
        obs = _obs(400, (0, 2816), (10, 2816), (20, 3584), (300, 3584))
        r = sc.agreement([(10 * S, 400, 3584, False)], obs, window=(0, 300 * S))
        self.assertEqual(r[('도달', '정상')], 1)

    def test_a_move_that_never_reached_is_separated(self):
        obs = _obs(400, *[(t, 2816) for t in range(0, 400, 10)])
        r = sc.agreement([(10 * S, 400, 3584, True)], obs, window=(0, 500 * S))
        self.assertEqual(r[('미도달', '잠금')], 1)

    def test_the_window_guard_applies_here_too(self):
        """같은 함정을 두 번째 함수에 다시 넣지 않는다."""
        obs = _obs(400, (0, 2816), (10, 2816), (53, 2816))
        r = sc.agreement([(10 * S, 400, 3584, True)], obs, window=(0, 53 * S))
        self.assertEqual(r['판정불가(창 밖)'], 1)
        self.assertEqual(sum(v for k, v in r.items() if isinstance(k, tuple)), 0)


if __name__ == '__main__':
    unittest.main()
