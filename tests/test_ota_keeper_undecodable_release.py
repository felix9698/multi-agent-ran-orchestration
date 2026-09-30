"""들리는데 복호만 안 되는 UE 컨텍스트는 OAI 가 스스로 끝내지 못한다 (2026-09-22).

gNB 쪽에는 RLF 콜백이 등록돼 있지 않고(`nr_rlc_set_rlf_handler` 는
`RRC/NR_UE/rrc_UE.c` 에서만 불린다 -- gNB 로그엔 "RLF detected, but no callable
RLF handler registered" 가 gnb1 1,952 · gnb2 20,817 회), UL 실패 경로는
`pusch_consecutive_dtx_cnt >= pusch_FailureThres` 즉 **DTX** 를 센다.  그런데 이
UE 는 들리기는 하므로 DTX 가 쌓이지 않는다 -- 실측 b1d1 은 `ulsch_errors
25292/25342 · BLER 1.00000` 인데 `ulsch_DTX` 가 140 에서 멈춰 있었다.  그래서
컨텍스트가 영원히 남고, 판은 goodput 0 인 그 UE 를 계속 측정 대상으로 붙든다.
"""
import importlib.util
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911' / 'ops'


def _load():
    if not (OPS / 'keeper.py').exists():
        raise unittest.SkipTest(f'운영 소스가 없다: {OPS / "keeper.py"}')
    spec = importlib.util.spec_from_file_location('keeper_undec', OPS / 'keeper.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def line(rnti, rounds, errors, dtx, bler):
    return (f"UE {rnti}: ulsch_rounds {rounds}/{rounds}/{rounds}/{rounds}, "
            f"ulsch_errors {errors}, ulsch_DTX {dtx}, BLER {bler:.5f} MCS (0) 0 "
            f"(Qm 2 deltaMCS 0 dB) NPRB 21 SNR 16.5 (+1.5) dB CCE fail 7\n")


class OnlyAStuckContextIsReleased(unittest.TestCase):
    def setUp(self):
        self.k = _load()

    def text(self, rows):
        return ''.join(rows)

    def test_a_sustained_undecodable_context_is_named(self):
        rows = [line('b1d1', 25000 + i, 24900 + i, 140, 1.0) for i in range(14)]
        self.assertEqual([('b1d1', 24913, 140)], self.k.undecodable_contexts(self.text(rows)))

    def test_a_healthy_ue_is_left_alone(self):
        rows = [line('b700', 700 + i, 3, 20, 0.0004) for i in range(14)]
        self.assertEqual([], self.k.undecodable_contexts(self.text(rows)))

    def test_a_brief_spike_is_not_enough(self):
        rows = [line('aaaa', 100 + i, 5, 3, 0.02) for i in range(10)]
        rows += [line('aaaa', 120 + i, 6, 3, 1.0) for i in range(4)]
        self.assertEqual([], self.k.undecodable_contexts(self.text(rows)),
                         '짧은 첨두로 UE 를 끊으면 안 된다')

    def test_a_silent_ue_is_not_this_case(self):
        """오류가 더 늘지 않으면 이미 끝난 컨텍스트다 -- 끊을 것이 없다."""
        rows = [line('dead', 9000, 8999, 900, 1.0) for _ in range(14)]
        self.assertEqual([], self.k.undecodable_contexts(self.text(rows)))

    def test_one_stuck_among_healthy_ones(self):
        rows = []
        for i in range(14):
            rows.append(line('b700', 700 + i, 3, 20, 0.0004))
            rows.append(line('b1d1', 25000 + i, 24900 + i, 140, 1.0))
            rows.append(line('e0f0', 500 + i, 1, 7, 0.011))
        self.assertEqual(['b1d1'], [r for r, _e, _d in self.k.undecodable_contexts(self.text(rows))])


if __name__ == '__main__':
    unittest.main()
