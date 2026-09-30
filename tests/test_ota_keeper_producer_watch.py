"""keeper 의 프로듀서 감시 (2026-09-21).

2026-09-21: 18443(R1 조종)·9445(캠페인5 액션)가 재부팅 뒤 죽은 채 방치돼, 부착 게이트를
통과한 판이 전부 1.2초 만에 `DEPENDENCY_PREFLIGHT_REFUSED:R1Error` 로 즉사했다. 그날 아침
만든 `ops/preflight.sh` 는 컨테이너·서비스·UE 는 보는데 **포트는 안 봤다**.

그리고 감시를 넣자마자 두 번째 결함이 생겼다: 주 루프는 20초마다 도는데 프로듀서 기동은
그보다 오래 걸릴 수 있어(R1 실측 약 12초, 부하 시 더) **같은 프로듀서를 두 번 띄워 포트를
두고 경쟁**시킨다.
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
        spec = importlib.util.spec_from_file_location('ota_keeper_prod', OPS / 'keeper.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(OPS))


class ProducerWatch(unittest.TestCase):
    def setUp(self):
        self.k = _keeper()
        self.k._producer_started_at.clear()
        self.spawned = []
        # hermetic: 기동 로그를 실제 `ops/overnight/` 에 만들지 않는다.
        self.sandbox = pathlib.Path(tempfile.mkdtemp())
        (self.sandbox / 'overnight').mkdir()
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        self._real_here = self.k.HERE
        self.k.HERE = self.sandbox

    def _run(self, open_ports):
        self.envs = []

        def spawn(cmd, **kw):
            self.spawned.append(cmd[-1])
            self.envs.append(kw.get('env') or {})

        with mock.patch.object(self.k, '_port_is_open',
                               side_effect=lambda h, p: p in open_ports), \
             mock.patch.object(self.k, 'log'), \
             mock.patch.object(self.k.subprocess, 'Popen', side_effect=spawn):
            self.k.ensure_producers()
        return list(self.spawned)

    def test_an_open_port_is_left_alone(self):
        ports = {p for _n, _h, p, _c, _m in self.k.PRODUCERS}
        self.assertEqual(self._run(ports), [])

    def test_a_closed_port_is_started(self):
        self.assertEqual(len(self._run(set())), len(self.k.PRODUCERS))

    def test_it_does_not_start_the_same_producer_twice_while_it_binds(self):
        """주 루프가 20초마다 도는데 기동은 더 걸린다 -- 두 번 띄우면 포트를 두고 경쟁한다."""
        first = self._run(set())
        self.spawned.clear()
        self.assertEqual(self._run(set()), [], '유예 중에는 다시 띄우지 않는다')
        self.assertTrue(first)

    def test_it_tries_again_once_the_grace_has_passed(self):
        self._run(set())
        for port in list(self.k._producer_started_at):
            self.k._producer_started_at[port] = time.time() - self.k.PRODUCER_START_GRACE_S - 1
        self.spawned.clear()
        self.assertEqual(len(self._run(set())), len(self.k.PRODUCERS))

    def test_a_producer_that_came_up_clears_its_timer(self):
        self._run(set())
        ports = {p for _n, _h, p, _c, _m in self.k.PRODUCERS}
        self._run(ports)
        self.assertEqual(self.k._producer_started_at, {})


if __name__ == '__main__':
    unittest.main()


class TheCampaign5ProducerGetsBothCells(unittest.TestCase):
    """맵이 없으면 gnb1 한 셀만 등록되고, gnb2 의 UE 들은 보조 축을 못 쓴다.

    `run_campaign5_producer.sh` 는 `HW_CAMPAIGN5_CELL_BINDINGS` 가 없으면
    `--cell-id $HW_CAMPAIGN5_CELL_ID --nb-id $HW_GNB1_NB_ID` 로 떨어진다. 2026-09-21 에
    내가 맨 스크립트로 띄워 정확히 그 상태였고, 배치상 ue2·ue3 가 gnb2 에 있었다.
    """

    def setUp(self):
        self.k = _keeper()
        self.k._producer_started_at.clear()
        self.sandbox = pathlib.Path(tempfile.mkdtemp())
        (self.sandbox / 'overnight').mkdir()
        self.addCleanup(shutil.rmtree, self.sandbox, True)
        self._real_here = self.k.HERE     # 맵 경로는 진짜를 봐야 한다

    def test_the_map_lists_both_cells_and_matches_the_deployment(self):
        import json, pathlib
        path = pathlib.Path(self.k.PRODUCER_ENV[9445]['HW_CAMPAIGN5_CELL_BINDINGS'])
        self.assertTrue(path.is_file(), '맵이 없으면 keeper 가 한 셀만 띄운다')
        entries = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual({int(e['cellId']) for e in entries}, {12345678, 87654321})
        self.assertEqual({e['nbId'] for e in entries}, {3584, 2816})
        for entry in entries:
            self.assertEqual(set(entry), {'cellId', 'nbId', 'ledgerPath'},
                             '프로듀서는 정확히 이 세 키만 받는다')

    def test_the_binding_map_reaches_the_spawned_process(self):
        envs = []

        def spawn(cmd, **kw):
            envs.append(kw.get('env') or {})

        self.k.HERE = self.sandbox
        with mock.patch.object(self.k, '_port_is_open', return_value=False), \
             mock.patch.object(self.k, 'log'), \
             mock.patch.object(self.k.subprocess, 'Popen', side_effect=spawn):
            self.k.ensure_producers()
        campaign5 = [e for e in envs if 'HW_CAMPAIGN5_CELL_BINDINGS' in e]
        self.assertEqual(len(campaign5), 1, '9445 하나에만 맵이 붙는다')
