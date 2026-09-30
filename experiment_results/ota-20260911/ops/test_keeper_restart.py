#!/usr/bin/env python3
"""증거로 시작한 재기동은 USB 엔드포인트도 반드시 지운다 (2026-09-18).

`restart_ue` 는 증거를 **찾아** `UE_ZOMBIE_RESTART` 까지 찍고도 그 값을 지역변수
`evidence` 에 넣지 않아, 바로 아래 `if evidence is not None or usb_wedged(host)`
가 언제나 거짓이었다.  즉 **호출자가 명시로 넘긴 경우에만** 리셋이 돌았다.
그 결과 ue3 가 18:26·18:40 에 리셋 없이 두 번 재기동되고, 무용 가드가
18:49 에 ue3 를 포기했으며, 19:03 에 러너의 게이트가 STOP→USB RESET→START 로
같은 주소에서 되살렸다.

hermetic: ssh·budget·파일 읽기를 전부 가짜로 세우고 호출 순서만 본다.
"""
import sys, types, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import keeper  # noqa: E402


class EvidenceRestartResetsUsb(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._saved = {}
        def fake(name, fn):
            self._saved[name] = getattr(keeper, name)
            setattr(keeper, name, fn)
        fake('log', lambda ev, **kw: self.calls.append(('log', ev)))
        fake('budget', lambda target: True)
        fake('usb_reset', lambda host: self.calls.append(('usb_reset', host)))
        fake('usb_wedged', lambda host: False)          # 웨지 신호는 없다
        fake('ue_auto_restart_held', lambda: True)
        fake('zombie_release_evidence', lambda host: 'session-released')
        fake('_ue_password', lambda: 'x')
        fake('ssh', lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout='', stderr=''))
        self._read = Path.read_text
        Path.read_text = lambda self, *a, **k: (   # noqa: ARG005
            'STOP_CODE = """pass"""' if self.name == 'execute_once.py' else 'pass')
        import time as _t
        self._sleep, _t.sleep = _t.sleep, lambda s: None

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(keeper, name, fn)
        Path.read_text = self._read
        import time as _t
        _t.sleep = self._sleep

    def test_a_restart_that_found_its_own_evidence_still_resets_the_usb(self):
        keeper.restart_ue('ue3')
        names = [c[0] for c in self.calls]
        self.assertIn('usb_reset', names,
                      '증거로 시작한 재기동인데 USB 를 안 지웠다')
        self.assertLess(names.index('usb_reset'),
                        len(names) - 1 - names[::-1].index('log'),
                        'USB 리셋은 START 로그보다 앞서야 한다')
        self.assertIn('UE_ZOMBIE_RESTART', [c[1] for c in self.calls if c[0] == 'log'])



class GnB2StartupDetection(unittest.TestCase):
    """2026-09-20: 오류 패턴을 세면 목록에 없는 실패를 성공으로 읽는다.

    X310 관리 채널이 죽었을 때 사인은 'Failure to create rfnoc_graph' 였고,
    keeper 가 세던 key_error / No USRP Device Found 는 0 건이라 startupErrors=0 —
    즉 성공으로 읽고 재시도조차 하지 않았다.
    """

    def test_startup_check_asks_whether_it_is_running_not_which_errors_appeared(self):
        import re, pathlib
        src = pathlib.Path(__file__).with_name('keeper.py').read_text()
        body = src[src.index('def restart_gnb2'):]
        body = body[:body.index('\ndef ')]
        self.assertIn('pgrep -x nr-softmodem', body,
                      '기동 판정은 프로세스 생존으로 해야 한다')
        self.assertNotIn('grep -c "key_error', body,
                         '오류 패턴 세기로 돌아가면 목록에 없는 실패를 놓친다')


if __name__ == '__main__':
    unittest.main(verbosity=2)
