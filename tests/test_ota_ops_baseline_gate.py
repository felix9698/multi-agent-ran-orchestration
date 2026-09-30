"""The gate holds a board until the previous board's controls are off the radio.

2026-09-19 18:28: a retained 6.0 dB attenuation outlived its board, the next board
took it as its baseline, the policy expired, and both power trials were refused at
PREPARE with REJECTED_CONFIG_MISMATCH.
"""
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911' / 'ops'


def _load_runner():
    spec = importlib.util.spec_from_file_location('run_episode_baseline', OPS / 'run_episode.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(nb, atten=None, ues=()):
    row = {'recv_unix_us': int(time.time() * 1e6), 'nb_id': nb}
    if atten is not None:
        row['measurements'] = [{'name': 'RAN.Cell.TxAttenuationDb', 'type': 'real', 'value': atten}]
    row['ues'] = [{'amf_ue_ngap_id': ue, 'measurements': [
        {'name': 'RAN.UE.DlPrbCap', 'type': 'int', 'value': cap},
        {'name': 'RAN.UE.PfWeight', 'type': 'real', 'value': pf}]} for ue, cap, pf in ues]
    return json.dumps(row)


@unittest.skipUnless((OPS / 'run_episode.py').is_file(), 'OTA ops scripts not present')
class TheGateWaitsForC0(unittest.TestCase):
    def _leftover(self, *rows):
        runner = _load_runner()
        with tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False) as handle:
            handle.write('\n'.join(rows) + '\n')
        runner.KPM_JSONL = handle.name
        return runner.leftover_controls()

    # C0 는 무선이 실제로 사는 동작점이라 **움직인다** -- 2026-09-22 07:10 에 gnb1(3584)
    # 이 0.0 에서 8.0 으로 옮겨졌다(`gnb1-lives-in-a-two-decibel-window`).  값을 여기
    # 박아 두면 다음 이동 때 또 빨개지고, 그때 의심받는 건 게이트지 이 파일이 아니다.
    # 그래서 기준선은 **게이트가 쓰는 상수에서 읽는다**; 이 시험들이 묻는 것은
    # "기준선에서 벗어난 값이 남은 제어로 보이는가" 이지 그 값이 얼마냐가 아니다.
    #: skip 이 걸리기 전에 클래스 본문이 돌면 ops 파일이 없는 checkout 에서 import 가
    #: 터진다(codex 감사 #10).  그래서 setUp 에서 읽는다 -- `skipUnless` 가 먼저다.
    BASELINE: dict = {}

    def setUp(self):
        self.BASELINE = _load_runner().BASELINE_ATTENUATION_DB

    def test_a_retained_attenuation_is_leftover(self):
        off_baseline = self.BASELINE[3584] - 2.0
        left = self._leftover(_row(3584, off_baseline), _row(2816, self.BASELINE[2816]))
        self.assertEqual(left, ['txAttenuationDb@nb3584=%g (C0 %g)'
                                % (off_baseline, self.BASELINE[3584])])

    def test_the_newest_value_decides(self):
        """기준선에서 벗어난 값 뒤에 기준선 값이 오면 남은 제어가 아니다."""
        self.assertEqual(self._leftover(_row(3584, self.BASELINE[3584] - 2.0),
                                        _row(3584, self.BASELINE[3584]),
                                        _row(2816, self.BASELINE[2816])), [])

    def test_ue_axes_are_checked(self):
        left = self._leftover(_row(3584, self.BASELINE[3584],
                                   ues=[(301, 18, 4.0), (300, 0, 1.0)]))
        self.assertEqual(left, ['RAN.UE.DlPrbCap@301=18 (C0 0)', 'RAN.UE.PfWeight@301=4 (C0 1)'])


if __name__ == '__main__':
    unittest.main()
