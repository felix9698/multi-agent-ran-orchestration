"""A core container that exits must not stay down, and episodes must not stay stopped.

2026-09-21: `mysql` exited at ~01:30 and nothing restarted it.  The UDR could
not read subscription data, so the AMF rejected every UE with
`Registration Reject: Illegal_UE`.  UEs synced, did RA and completed RRC setup,
then never got an address -- the bed looked like a radio fault for hours, and
the whole core ran with `restart: no`.  Separately, maintenance had stopped the
episode service and nobody started it again.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

OPS = Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911' / 'ops'


def _load():
    # 이 운영 트리는 `.gitignore` 로 제외돼 있어 깨끗한 checkout 에는 없다.
    # 없을 때 import 오류로 죽으면 "시험이 깨졌다" 로 보이므로 정직하게 건너뛴다.
    if not (OPS / 'keeper.py').exists():
        raise unittest.SkipTest(f'운영 소스가 없다: {OPS / "keeper.py"}')
    spec = importlib.util.spec_from_file_location('keeper_core', OPS / 'keeper.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Result:
    def __init__(self, stdout='', returncode=0, stderr=''):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class BringsTheCoreBack(unittest.TestCase):
    def setUp(self):
        self.k = _load()
        self.calls = []
        self.logged = []
        self.k.log = lambda event, **kw: self.logged.append((event, kw))
        # `self.k.time` 은 stdlib `time` **모듈 자체**다.  여기에 대입하면 이 판의
        # 인터프리터 전체에서 `time.sleep` 이 무력화되고 복구되지 않는다 -- 뒤따르는
        # 모든 시험이 오염된다 (2026-09-22).  patch 로 바꾸고 반드시 되돌린다.
        patcher = mock.patch.object(self.k.time, 'sleep', lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.running = {name: True for name in self.k.CORE_CONTAINERS}

        def fake_run(cmd, **kw):
            self.calls.append(cmd)
            if cmd[:3] == ['docker', 'inspect', '-f']:
                return Result('true\n' if self.running.get(cmd[-1]) else 'false\n')
            if cmd[:2] == ['docker', 'start']:
                self.running[cmd[-1]] = True
                return Result()
            return Result()
        self.k.run = fake_run

    def test_a_healthy_core_is_left_alone(self):
        self.k.ensure_core()
        self.assertEqual([], [c for c in self.calls if c[:2] == ['docker', 'start']])

    def test_the_database_is_started_when_it_exited(self):
        self.running['mysql'] = False
        self.k.ensure_core()
        self.assertIn(['docker', 'start', 'mysql'], self.calls)
        self.assertIn('CORE_DOWN', [e for e, _ in self.logged])

    def test_every_down_container_is_started(self):
        for name in ('mysql', 'oai-amf'):
            self.running[name] = False
        self.k.ensure_core()
        started = [c[-1] for c in self.calls if c[:2] == ['docker', 'start']]
        self.assertEqual(['mysql', 'oai-amf'], started)


class KeepsEpisodesRunning(unittest.TestCase):
    def setUp(self):
        self.k = _load()
        self.calls = []
        self.k.log = lambda event, **kw: None
        self.state = 'inactive'

        def fake_run(cmd, **kw):
            self.calls.append(cmd)
            if cmd[:3] == ['systemctl', '--user', 'is-active']:
                return Result(self.state + '\n')
            return Result()
        self.k.run = fake_run
        # `ensure_episodes` 는 모듈 수준 `HERE` 밑의 hold 파일을 본다.  예전엔 **실제**
        # 운영 디렉터리를 보고 hold 가 있으면 skip 했다 -- 베드 상태가 시험 색을 정했고,
        # 정작 "hold 가 기동을 막는가" 는 한 번도 검증되지 않았다 (2026-09-22).
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'overnight').mkdir()
        patcher = mock.patch.object(self.k, 'HERE', tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hold = tmp / 'overnight' / 'NO_EPISODES'

    def test_a_stopped_service_is_started(self):
        self.k.ensure_episodes()
        self.assertIn(['systemctl', '--user', 'start', 'aic-v31-episodes'], self.calls)

    def test_a_running_service_is_left_alone(self):
        self.state = 'active'
        self.k.ensure_episodes()
        self.assertNotIn(['systemctl', '--user', 'start', 'aic-v31-episodes'], self.calls)

    def test_an_operator_hold_keeps_the_service_down(self):
        self.hold.write_text('operator hold\n')
        self.k.ensure_episodes()
        self.assertEqual([], self.calls, 'hold 가 있으면 systemctl 을 아예 부르지 않는다')


class RestoresTheN3Address(unittest.TestCase):
    """gNB1 binds its N3 socket to 192.168.70.140; a reboot drops that address.

    Without it the gNB starts, fails `bind: Cannot assign requested address`,
    and dies on the first PDU session with `Unable to create GTP-U tunnel for
    N3` -- which looks like a radio fault (2026-09-21).
    """

    def setUp(self):
        self.k = _load()
        self.calls = []
        self.logged = []
        self.k.log = lambda event, **kw: self.logged.append(event)
        self.present = True

        def fake_run(cmd, **kw):
            self.calls.append(cmd)
            if cmd[:2] == ['ip', '-br']:
                return Result('demo-oai UP 192.168.70.129/26 192.168.70.140/26\n'
                              if self.present else 'demo-oai UP 192.168.70.129/26\n')
            return Result()
        self.k.run = fake_run

    def test_a_present_address_is_left_alone(self):
        self.k.ensure_gnb1_n3()
        self.assertEqual([], [c for c in self.calls if c[:2] == ['docker', 'run']])

    def test_a_missing_address_is_added(self):
        self.present = False
        self.k.ensure_gnb1_n3()
        added = [c for c in self.calls if c[:2] == ['docker', 'run']]
        self.assertEqual(1, len(added))
        self.assertIn('192.168.70.140/26', added[0])
        self.assertIn('demo-oai', added[0])
        self.assertIn('GNB1_N3_MISSING', self.logged)


class RestoresTheGnb2UpfPath(unittest.TestCase):
    """PC1's raw table drops UPF-bound traffic from anything but demo-oai.

    2026-09-21: gNB2 lives on another host, so its GTP-U never reached the UPF
    and the user plane was dead on that cell only -- while gNB1, sharing the
    host, looked fine.  raw runs before filter and nat, so the filter rules,
    the routes and the MASQUERADE exception all looked correct.
    """

    def setUp(self):
        self.k = _load()
        self.calls = []
        self.logged = []
        self.k.log = lambda event, **kw: self.logged.append(event)
        self.present = True

        def fake_run(cmd, **kw):
            self.calls.append(cmd)
            return Result('present\n' if self.present else 'missing\n')
        self.k.run = fake_run

    def test_an_existing_exception_is_left_alone(self):
        self.k.ensure_gnb2_upf_path()
        self.assertEqual(1, len(self.calls))          # 확인만 하고 끝
        self.assertEqual([], self.logged)

    def test_a_missing_exception_is_restored(self):
        self.present = False
        self.k.ensure_gnb2_upf_path()
        self.assertEqual(2, len(self.calls))
        self.assertIn('-I PREROUTING 1 -s 192.168.50.2/32 -d 192.168.70.134/32 -j ACCEPT',
                      ' '.join(self.calls[1]))
        self.assertIn('GNB2_UPF_PATH_MISSING', self.logged)


if __name__ == '__main__':
    unittest.main()
