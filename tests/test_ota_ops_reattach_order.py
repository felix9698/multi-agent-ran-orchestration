"""The runner's re-attach resets USB only after the softmodem is stopped.

2026-09-15: ``run_episode.restart_ue`` reset the USRP's USB endpoint before
``force_reattach`` stopped the UE, pulling the device from under a running
softmodem (80 of 199 UE logs ended ERROR_CODE_TIMEOUT -> NO_DEVICE -> SIGINT).
"""
import importlib.util
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

OPS = Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911' / 'ops'


def _load_runner():
    spec = importlib.util.spec_from_file_location('run_episode_under_test', OPS / 'run_episode.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless((OPS / 'run_episode.py').is_file() and (OPS / 'force_reattach.py').is_file(),
                     'OTA ops scripts not present')
class TheRunnerResetsUsbOnlyWhenTheUeIsDown(unittest.TestCase):
    def test_restart_ue_does_not_reset_before_stopping_and_asks_for_the_reset(self):
        runner = _load_runner()
        # This cell is about the ORDER -- no reset before the stop, and the
        # reset asked for through force_reattach -- so the restart gate is
        # pinned open rather than left to the bed.  Unpinned it reached the
        # live lab: with overnight/NO_UE_AUTO_RESTART in place restart_ue
        # returns early unless ue_zombie finds evidence over ssh, so the test
        # passed only while ue2 happened to look like a zombie and errored with
        # 'NoneType has no attribute kwargs' whenever that UE was healthy.
        with patch.object(runner, 'usb_reset') as reset, \
             patch.object(runner, 'ue_auto_restart_held', return_value=False), \
             patch.object(runner.subprocess, 'call', return_value=0) as call:
            runner.restart_ue('ue2')
        reset.assert_not_called()
        self.assertEqual(call.call_args.kwargs['env'].get('AIC_REATTACH_USB_RESET'), '1')
        self.assertTrue(str(call.call_args.args[0][1]).endswith('force_reattach.py'))

    def test_force_reattach_orders_stop_then_reset_then_start(self):
        order = []

        class Done:
            returncode = 0
            stderr = ''
            stdout = ''

        def fake_run(argv, **kwargs):
            joined = ' '.join(map(str, argv))
            if 'ioctl' in joined or '0x5514' in joined:
                order.append('usb_reset')
            elif 'AIC_UE_NO_SCAN=1' in joined:
                order.append('start')
            elif 'sudo' in joined:
                order.append('stop')
            return Done()

        with tempfile.TemporaryDirectory() as tmp:
            pw = Path(tmp) / 'pw'
            pw.write_text('x\n')
            # 2026-09-23: 러너 보호 가드가 되살아났다 -- 실제 락을 읽지 않도록 AIC_TAKE_BED=1.
            env = {'AIC_UE_PASSWORD_FILE': str(pw), 'AIC_REATTACH_USB_RESET': '1', 'AIC_TAKE_BED': '1'}
            sys.path.insert(0, str(OPS))
            try:
                with patch.dict(os.environ, env), patch('subprocess.run', side_effect=fake_run), \
                     patch('time.sleep'), patch.object(sys, 'argv', ['force_reattach.py', 'ue2']), \
                     patch('builtins.print'):
                    sys.modules.pop('run_episode', None)
                    # 2026-09-23: 가짜 되읽기는 반송파를 못 주므로 비영 종료가 옳다 -- 순서만 본다.
                    with self.assertRaises(SystemExit):
                        runpy.run_path(str(OPS / 'force_reattach.py'), run_name='__main__')
            finally:
                sys.path.remove(str(OPS))
                sys.modules.pop('run_episode', None)
        self.assertEqual(order, ['stop', 'usb_reset', 'start'])

    def test_without_the_flag_force_reattach_does_not_reset(self):
        order = []

        class Done:
            returncode = 0
            stderr = ''
            stdout = ''

        def fake_run(argv, **kwargs):
            if '0x5514' in ' '.join(map(str, argv)):
                order.append('usb_reset')
            return Done()

        with tempfile.TemporaryDirectory() as tmp:
            pw = Path(tmp) / 'pw'
            pw.write_text('x\n')
            environ = {k: v for k, v in os.environ.items() if k != 'AIC_REATTACH_USB_RESET'}
            environ['AIC_UE_PASSWORD_FILE'] = str(pw)
            environ['AIC_TAKE_BED'] = '1'
            with patch.dict(os.environ, environ, clear=True), patch('subprocess.run', side_effect=fake_run), \
                 patch('time.sleep'), patch.object(sys, 'argv', ['force_reattach.py', 'ue1']), \
                 patch('builtins.print'), self.assertRaises(SystemExit):
                runpy.run_path(str(OPS / 'force_reattach.py'), run_name='__main__')
        self.assertEqual(order, [])


if __name__ == '__main__':
    unittest.main()
