"""Operator trace safeguards only: no root, live process, network or tracefs."""
import ast
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ENTRY = Path(__file__).resolve().parents[1]/'experiment_results/ota-20260911/trace_msg4_gnb1.py'


def load():
    spec = importlib.util.spec_from_file_location('msg4_trace_under_test', ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Msg4TraceOperatorTests(unittest.TestCase):
    def setUp(self):
        self.module = load()
        env = patch.dict(os.environ, {'PATH': os.defpath, 'HOME': '/nonexistent/msg4-test'}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_check_does_not_open_tracefs_or_run_commands(self):
        m = self.module
        with patch.object(m, 'digest', return_value=m.EXPECTED_SHA), \
                patch.object(m, 'append_probe', side_effect=AssertionError('no probes')), \
                patch.object(m.subprocess, 'run', side_effect=AssertionError('no commands')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, m.main(['--check']))
        self.assertIn('binaryFingerprintMatches', output.getvalue())

    def test_last_error_only_reads_diagnostics(self):
        m = self.module
        with patch.object(m.os, 'geteuid', return_value=0), \
                patch.object(m, 'last_errors', return_value=[{'kernelError':'test-error'}]), \
                patch.object(m, 'digest', side_effect=AssertionError('no binary operation')), \
                patch.object(m, 'append_probe', side_effect=AssertionError('no probes')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, m.main(['--last-error']))
        self.assertIn('test-error', output.getvalue())

    def test_last_error_does_not_expose_other_probe_groups(self):
        m = self.module
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            out, tracing = root/'output', root/'tracing'
            out.mkdir(); tracing.mkdir()
            (out/'aic_m4_100_1234abcd').mkdir()
            (tracing/'error_log').write_text(
                'trace_uprobe: test error\n'
                'Command: p:aic_m4_100_1234abcd/uci safe-control-scalars\n'
                '                    ^\n'
                'Command: p:unrelated_group/other never-copy-this\n')
            with patch.object(m, 'OUTPUT', out), patch.object(m, 'TRACEFS', tracing):
                rows = m.last_errors()
            self.assertEqual(1, len(rows))
            self.assertNotIn('never-copy-this', str(rows))

    def test_changed_binary_refuses_before_probe_installation(self):
        m = self.module
        with patch.object(m, 'digest', return_value='different'), \
                patch.object(m, 'append_probe') as install:
            with self.assertRaisesRegex(SystemExit, 'GNB_BINARY_CHANGED'):
                m.main(['--check'])
            install.assert_not_called()

    def test_nonroot_cannot_start_capture(self):
        m = self.module
        with patch.object(m, 'digest', return_value=m.EXPECTED_SHA), \
                patch.object(m.os, 'geteuid', return_value=1000), \
                patch.object(m, 'append_probe') as install:
            with self.assertRaisesRegex(SystemExit, 'NORMAL_PC1_TERMINAL'):
                m.main([])
            install.assert_not_called()

    def test_duration_bounds_are_trace_limits_not_radio_timeouts(self):
        for seconds in ('0', '301'):
            with self.subTest(seconds=seconds), self.assertRaisesRegex(SystemExit, 'TRACE_DURATION'):
                self.module.main(['--seconds', seconds, '--check'])

    def test_only_the_unique_session_leader_is_selected(self):
        with patch.object(self.module.subprocess, 'run', return_value=SimpleNamespace(stdout='100 100\n101 100\n')):
            self.assertEqual(100, self.module.selected_pid())

    def test_no_or_multiple_leaders_refuse(self):
        for rows in ('', '100 100\n200 200\n'):
            with self.subTest(rows=rows), patch.object(self.module.subprocess, 'run', return_value=SimpleNamespace(stdout=rows)):
                with self.assertRaisesRegex(RuntimeError, 'EXACTLY_ONE'):
                    self.module.selected_pid()

    def test_existing_owned_private_output_is_not_chowned(self):
        m = self.module
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)/'output'
            out.mkdir(mode=0o700)
            with patch.object(m, 'OUTPUT', out), patch.object(m.os, 'chown') as chown:
                m.prepare_output(os.getuid(), os.getgid())
                chown.assert_not_called()

    def test_symlink_output_is_refused_without_chowning_target(self):
        m = self.module
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'target'
            target.mkdir()
            out = Path(folder)/'output'
            out.symlink_to(target, target_is_directory=True)
            with patch.object(m, 'OUTPUT', out), patch.object(m.os, 'chown') as chown:
                with self.assertRaisesRegex(RuntimeError, 'UNOWNED_OR_UNSAFE'):
                    m.prepare_output(os.getuid(), os.getgid())
                chown.assert_not_called()

    def test_nonprivate_output_is_refused(self):
        m = self.module
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)/'output'
            out.mkdir()
            out.chmod(0o755)
            with patch.object(m, 'OUTPUT', out), patch.object(m.os, 'chown'):
                with self.assertRaisesRegex(RuntimeError, 'UNOWNED_OR_UNSAFE'):
                    m.prepare_output(os.getuid(), os.getgid())

    def test_probes_only_fetch_declared_control_scalars(self):
        probes = self.module.PROBES
        self.assertEqual({'uci','check','pdu'}, set(probes))
        self.assertIn('frame=%si:u32 slot=%dx:u32', probes['uci'])
        self.assertIn('rnti=+8(%cx):u16', probes['uci'])
        self.assertIn('conf=+19(%cx):u8 v0=+20(%cx):u8', probes['uci'])
        self.assertNotIn('v1=', probes['uci'])
        self.assertIn('frame=%dx:s32 slot=%cx:s32', probes['pdu'])
        self.assertNotIn('%si', probes['pdu'])  # IQ buffer argument is never fetched.
        self.assertTrue(all('string' not in definition for definition in probes.values()))

    def test_script_never_signals_or_launches_a_radio_process(self):
        tree = ast.parse(ENTRY.read_text())
        called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        self.assertFalse(called.intersection({'os.kill','os.killpg','signal.pidfd_send_signal','subprocess.Popen'}))


if __name__ == '__main__':
    unittest.main()
