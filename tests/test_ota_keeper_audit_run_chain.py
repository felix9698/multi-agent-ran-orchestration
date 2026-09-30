"""판 실행 사슬 감사(2026-09-23) 결함 5~7 -- 옛 코드에서 실패하는 시험.

5. SIGTERM(systemd stop/restart) 에 finally 가 돌지 않아 exit.json·원장 행·원격 정리가 빠졌다.
6. force_reattach 의 러너 보호 가드가 주석 처리된 채 방치, 스크립트는 늘 exit 0.
7. run_blocks_campaign.sh 가 깨진 상태 파일에서 방식을 추측해(three-agent 기본값) 돌았다.

hermetic: ssh·subprocess 는 가짜, 러너 락·상태 파일은 임시 디렉터리.  신호는 실제로
보내지 않고 설치된 처리기를 직접 부른다.
"""
import importlib.util
import json
import os
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

EXP = Path(os.environ.get('AIC_OTA_EXP_UNDER_TEST') or
           Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911')
OPS = EXP / 'ops'


def _load(path, name):
    if not path.exists():
        raise unittest.SkipTest(f'운영 소스가 없다: {path}')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


class _KeepsSigterm(unittest.TestCase):
    def setUp(self):
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)


class _FakeChild:
    def __init__(self, on_wait, code=130):
        self.on_wait, self.code, self.signals, self.done = on_wait, code, [], False

    def poll(self):
        return self.code if self.done else None

    def send_signal(self, signum):
        self.signals.append(signum)

    def wait(self, timeout=None):
        self.on_wait()
        self.done = True
        return self.code


class TheProxyWrapperHandsSigtermToTheRunner(_KeepsSigterm):
    def test_sigterm_is_forwarded_and_the_child_is_waited_for(self):
        mod = _load(OPS / 'run_with_proxy.py', 'rwp_audit')
        child = _FakeChild(lambda: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None))
        fake_call = lambda *a, **kw: child.wait()        # 옛 코드의 subprocess.call 도 같은 틀로
        with patch.object(mod.subprocess, 'Popen', lambda *a, **kw: child), \
                patch.object(mod.subprocess, 'call', fake_call), \
                patch.object(sys, 'argv', ['run_with_proxy.py', str(EXP / 'atomic_formal_run_guarded.py')]):
            self.assertEqual(130, mod.main())
        self.assertEqual([signal.SIGTERM], child.signals)


class TheEpisodeRunnerCleansUpOnSigterm(_KeepsSigterm):
    def test_sigterm_mid_board_forwards_skips_the_radio_and_releases_the_lock(self):
        mod = _load(OPS / 'run_episode.py', 'run_episode_audit')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        busy = Path(tmp.name) / 'busy.lock'
        restored = []
        child = _FakeChild(lambda: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None))
        with patch.object(mod, 'BUSY', busy), \
                patch.object(mod, 'held_by_someone_else', lambda: False), \
                patch.object(mod, 'wait_for_bed', lambda *a, **kw: True), \
                patch.object(mod, 'bed_not_ready', lambda: []), \
                patch.object(mod, 'wait_for_ues', lambda *a, **kw: True), \
                patch.object(mod, 'wait_for_baseline', lambda *a, **kw: True), \
                patch.object(mod, 'restore_placement', lambda: restored.append(1)), \
                patch.object(mod.time, 'sleep', lambda _s: None), \
                patch.object(mod.subprocess, 'Popen', lambda *a, **kw: child), \
                patch.object(mod.subprocess, 'call', lambda *a, **kw: child.wait()), \
                patch('builtins.print'), \
                patch.object(sys, 'argv', ['run_episode.py']):
            code = mod.main()
        self.assertEqual(130, code)
        self.assertEqual([signal.SIGTERM], child.signals)
        self.assertEqual([], restored, '멈추라는 신호에 UE 재부착(배치 복원)을 하지 않는다')
        self.assertFalse(busy.exists(), 'finally 가 락을 풀어야 한다')


class TheAttemptRunnerTurnsSigtermIntoItsInterruptPath(_KeepsSigterm):
    def test_main_installs_a_handler_that_forwards_then_interrupts(self):
        mod = _load(EXP / 'atomic_formal_run_guarded.py', 'atomic_audit')
        killed, seen = [], []

        def fake_attempt(*a, **kw):
            handler = signal.getsignal(signal.SIGTERM)
            with patch.object(mod, '_direct_children', lambda: []), \
                    patch.object(mod.os, 'kill', lambda pid, s: killed.append((pid, s))):
                try:
                    handler(signal.SIGTERM, None)
                except KeyboardInterrupt:
                    seen.append('interrupt')
            return 0
        with patch.object(mod, 'run_attempt', fake_attempt):
            mod.main([])
        self.assertEqual(['interrupt'], seen)
        self.assertEqual(signal.SIG_IGN, signal.getsignal(signal.SIGTERM),
                         '정리 도중 두 번째 SIGTERM 은 무시된다')

    def test_children_get_sigterm_before_the_interrupt(self):
        mod = _load(EXP / 'atomic_formal_run_guarded.py', 'atomic_audit2')
        if not hasattr(mod, '_sigterm_as_interrupt'):
            self.fail('SIGTERM 처리기가 없다')
        killed = []
        answers = iter([[4242], []])
        with patch.object(mod, '_direct_children', lambda: next(answers)), \
                patch.object(mod.os, 'kill', lambda pid, s: killed.append((pid, s))):
            mod._sigterm_as_interrupt()
            with self.assertRaises(KeyboardInterrupt):
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        self.assertEqual([(4242, signal.SIGTERM)], killed)


def _ops_copy(test):
    """force_reattach 가 옆의 keeper·run_episode 와 overnight/ 락을 보도록 임시 사본을 만든다."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    for name in ('force_reattach.py', 'keeper.py', 'run_episode.py'):
        shutil.copy(OPS / name, root / name)
    (root / 'overnight').mkdir()
    pw = root / 'pw'
    pw.write_text('x\n')
    return root, pw


class ForceReattachSaysWhenItFailed(unittest.TestCase):
    def _run(self, root, pw, argv, carrier_ok=True, start_rc=0, env_extra=None):
        calls = []
        carriers = {'gnb1': '3349920000', 'gnb2': '3319680000'}

        def fake_run(args, **kw):
            text = ' '.join(map(str, args))
            host = args[5] if len(args) > 5 else ''
            if '/proc/$p/cmdline' in text:
                calls.append('readback')
                cell = argv[0].partition('=')[2]
                return SimpleNamespace(returncode=0, stderr='',
                                       stdout=carriers[cell] if carrier_ok else '1')
            if 'AIC_UE_NO_SCAN=1' in text:
                calls.append('start')
                return SimpleNamespace(returncode=start_rc, stdout='', stderr='')
            calls.append('other')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        env = {'AIC_UE_PASSWORD_FILE': str(pw), **(env_extra or {})}
        env_clean = {k: v for k, v in os.environ.items()
                     if k not in ('AIC_REATTACH_USB_RESET', 'AIC_REATTACH_USB_AUTHORIZED', 'AIC_TAKE_BED')}
        code = 0
        sys.modules.pop('keeper', None)
        sys.modules.pop('run_episode', None)
        with patch.dict(os.environ, {**env_clean, **env}, clear=True), \
                patch('subprocess.run', side_effect=fake_run), patch('time.sleep'), \
                patch.object(sys, 'argv', ['force_reattach.py', *argv]), patch('builtins.print'):
            try:
                runpy.run_path(str(root / 'force_reattach.py'), run_name='__main__')
            except SystemExit as exc:
                code = exc.code
            finally:
                sys.modules.pop('keeper', None)
                sys.modules.pop('run_episode', None)
                while str(root) in sys.path:
                    sys.path.remove(str(root))
        return code, calls

    def test_a_carrier_mismatch_is_a_nonzero_exit(self):
        root, pw = _ops_copy(self)
        code, calls = self._run(root, pw, ['ue1=gnb1'], carrier_ok=False)
        self.assertTrue(code, '되읽은 반송파가 다르면 실패다')

    def test_a_failed_start_is_a_nonzero_exit(self):
        root, pw = _ops_copy(self)
        code, _calls = self._run(root, pw, ['ue1=gnb1'], start_rc=1)
        self.assertTrue(code)

    def test_a_clean_reattach_is_zero(self):
        root, pw = _ops_copy(self)
        code, calls = self._run(root, pw, ['ue1=gnb1'])
        self.assertFalse(code)
        self.assertEqual(['other', 'start', 'readback'], calls)

    def test_the_runner_guard_refuses_while_a_live_board_holds_the_bed(self):
        root, pw = _ops_copy(self)
        sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        (root / 'overnight' / 'episode-busy.lock').write_text(f'{sleeper.pid} cli\n')
        code, calls = self._run(root, pw, ['ue1=gnb1'])
        self.assertTrue(code, '러너가 베드를 쥐고 있으면 거절한다')
        self.assertEqual([], calls, '아무 UE 도 건드리지 않는다')

    def test_take_bed_overrides_the_guard(self):
        root, pw = _ops_copy(self)
        sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        (root / 'overnight' / 'episode-busy.lock').write_text(f'{sleeper.pid} cli\n')
        code, calls = self._run(root, pw, ['ue1=gnb1'], env_extra={'AIC_TAKE_BED': '1'})
        self.assertFalse(code)


class TheBlockCampaignStopsOnABrokenState(unittest.TestCase):
    """상태 파일이 깨졌으면 방식을 추측하지 말고 멈춘다."""

    def _setup(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        shutil.copy(OPS / 'run_blocks_campaign.sh', root / 'run_blocks_campaign.sh')
        marker = root / 'ran-case'
        (root / 'run_case.sh').write_text(f'echo "$AIC_METHOD" >> {marker}\nexit 0\n')
        (root / 'overnight').mkdir()
        (root / 'overnight' / 'next-attempt.txt').write_text('7\n')
        return root, marker

    def _go(self, root, timeout=20):
        env = {k: v for k, v in os.environ.items() if not k.startswith('AIC_')}
        env['AIC_CAMPAIGN'] = 'unit'
        try:
            r = subprocess.run(['bash', str(root / 'run_blocks_campaign.sh')], env=env,
                               capture_output=True, text=True, timeout=timeout)
            return r.returncode, r.stderr
        except subprocess.TimeoutExpired:
            return None, 'still running'

    def test_a_truncated_state_file_stops_the_campaign(self):
        root, marker = self._setup()
        (root / 'overnight' / 'unit.progress.json').write_text('{"block": 3, "sl')
        code, err = self._go(root)
        self.assertIsNotNone(code, '깨진 상태로 계속 돌면 안 된다')
        self.assertNotEqual(0, code)
        self.assertFalse(marker.exists(), '어떤 방식으로도 판을 돌리지 않는다')

    def test_a_state_without_a_slot_stops_the_campaign(self):
        root, marker = self._setup()
        (root / 'overnight' / 'unit.progress.json').write_text(
            json.dumps({'block': 0, 'completedBlocks': 0, 'incompleteBlocks': []}))
        code, _err = self._go(root)
        self.assertIsNotNone(code)
        self.assertNotEqual(0, code)
        self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
