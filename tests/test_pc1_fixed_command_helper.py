"""The PC1 helper is exercised with fakes only: no root, no socket, no radio, no command."""
import ast
import importlib.util
import os
from pathlib import Path
import socket
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

ENTRY = Path(__file__).resolve().parents[1] / 'experiment_results/ota-20260911/pc1_fixed_command_helper.py'


def load():
    spec = importlib.util.spec_from_file_location('_pc1_helper_under_test', ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Pc1FixedCommandHelperTests(unittest.TestCase):
    def setUp(self):
        self.helper = load()

    def test_only_the_four_fixed_operations_are_offered(self):
        self.assertEqual({'gnb1-status', 'gnb1-stop', 'gnb1-start', 'n3-capture'},
                         set(self.helper.ALLOWED))

    def test_no_shell_string_or_arbitrary_execution_path_exists(self):
        source = ENTRY.read_text()
        tree = ast.parse(source)
        called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        self.assertFalse(called.intersection({'os.system', 'eval', 'exec', 'subprocess.Popen'}))
        self.assertNotIn('shell=True', source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) == 'subprocess.run':
                self.assertIsInstance(node.args[0], ast.List, 'argv must be a fixed list')

    def test_nonroot_start_is_refused_before_binding_anything(self):
        with patch.object(self.helper.os, 'geteuid', return_value=1000), \
                patch.object(socket, 'socket', side_effect=AssertionError('no socket')):
            with self.assertRaisesRegex(SystemExit, 'RUN_WITH_SUDO'):
                self.helper.main()

    def test_sudo_without_invoking_user_is_refused(self):
        with patch.object(self.helper.os, 'geteuid', return_value=0), \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(socket, 'socket', side_effect=AssertionError('no socket')):
            with self.assertRaisesRegex(SystemExit, 'RUN_WITH_SUDO'):
                self.helper.main()

    def test_socket_path_fits_the_af_unix_limit(self):
        self.assertLess(len(str(self.helper.SOCKET).encode()), 100)

    def test_capture_argv_is_fixed_and_bounded(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen['argv'] = argv
            return NS(returncode=0, stdout='', stderr='')

        with patch.object(self.helper.subprocess, 'run', fake_run):
            self.helper.op_n3_capture()
        self.assertEqual(seen['argv'][:3], ['timeout', '12', 'tcpdump'])
        self.assertIn('udp port 2152 and host ' + self.helper.GNB1_N3, seen['argv'])

    def test_leader_is_only_taken_when_exactly_one_session_leader_exists(self):
        for rows, expected in (('100 100\n101 100\n', 100), ('', None), ('100 100\n200 200\n', None)):
            with self.subTest(rows=rows), patch.object(
                    self.helper.subprocess, 'run', return_value=NS(returncode=0, stdout=rows, stderr='')):
                self.assertEqual(expected, self.helper.gnb1_leader())

    def test_stop_refuses_when_no_single_leader_and_signals_nothing(self):
        with patch.object(self.helper, 'gnb1_leader', return_value=None), \
                patch.object(self.helper.os, 'killpg', side_effect=AssertionError('no signal')):
            self.assertFalse(self.helper.op_stop()['stopped'])

    def test_start_refuses_while_a_softmodem_is_running(self):
        with patch.object(self.helper, 'gnb1_leader', return_value=4242), \
                patch.object(self.helper.subprocess, 'run', side_effect=AssertionError('no start')):
            self.assertFalse(self.helper.op_start()['started'])


if __name__ == '__main__':
    unittest.main()
