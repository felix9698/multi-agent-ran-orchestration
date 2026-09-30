"""The authenticated entry is exercised with fake modules/env, never an API or radio."""
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch


REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / 'experiment_results/ota-20260911/authenticated_live_entry.py'


def load_entry():
    spec = importlib.util.spec_from_file_location('_authenticated_live_entry_test', ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Never depend on or clear an operational incident hold in a unit test.
    module.AUTHENTICATION_HOLD = Mock()
    module.AUTHENTICATION_HOLD.exists.return_value = False
    return module


class AuthenticatedLiveEntryTests(unittest.TestCase):
    def modules(self, callback=None):
        backend = ModuleType('decision.llm_backend')
        backend.LLMBackendType = NS(CLAUDE_SONNET='sonnet-label')
        backend.MODEL_IDS = {'sonnet-label': 'original-route'}
        decision = ModuleType('decision')
        decision.llm_backend = backend
        entry = ModuleType('main')
        entry.main = Mock(side_effect=callback) if callback else Mock(return_value=0)
        return {'decision': decision, 'decision.llm_backend': backend, 'main': entry}

    def test_incident_hold_refuses_before_mapping_credentials_or_calling_main(self):
        entry = load_entry()
        entry.AUTHENTICATION_HOLD.exists.return_value = True
        modules = self.modules()
        env = {'ANTHROPIC_BASE_URL': 'https://llm.example.test',
               'ANTHROPIC_AUTH_TOKEN': 'test-token', 'ANTHROPIC_API_KEY': 'old-test-key'}
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(SystemExit, 'LIVE_MODEL_AUTHENTICATION_ON_HOLD'):
                entry.main([])
            self.assertEqual('old-test-key', os.environ['ANTHROPIC_API_KEY'])
            modules['main'].main.assert_not_called()
        entry.AUTHENTICATION_HOLD.unlink.assert_not_called()

    def test_import_has_no_credential_or_entrypoint_side_effect(self):
        modules = self.modules()
        env = {'ANTHROPIC_AUTH_TOKEN': 'test-token', 'ANTHROPIC_API_KEY': 'old-test-key'}
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, modules):
            load_entry()
            self.assertEqual(os.environ['ANTHROPIC_API_KEY'], 'old-test-key')
            modules['main'].main.assert_not_called()
            self.assertEqual(modules['decision.llm_backend'].MODEL_IDS['sonnet-label'],
                             'original-route')

    def test_configured_origin_uses_no_argument_main_without_rewriting_state(self):
        captured = {}

        def called():
            captured['argv'] = list(sys.argv)
            captured['repo_available'] = str(REPO) in sys.path
            captured['credential_preserved'] = os.environ['ANTHROPIC_API_KEY'] == 'old-test-key'
            captured['route'] = modules['decision.llm_backend'].MODEL_IDS['sonnet-label']
            return 2

        modules = self.modules(called)
        entry = load_entry()
        previous_argv = ['outer-command', '--unrelated']
        env = {'ANTHROPIC_BASE_URL': 'https://llm.example.test',
               'ANTHROPIC_AUTH_TOKEN': 'test-token', 'ANTHROPIC_API_KEY': 'old-test-key'}
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, modules), \
                patch.object(sys, 'argv', previous_argv), patch.object(sys, 'path', ['/unused']):
            self.assertEqual(entry.main(['--no-gui', '--help']), 2)
            self.assertIs(sys.argv, previous_argv)
            self.assertEqual(sys.path, ['/unused'])
            self.assertEqual(os.environ['ANTHROPIC_API_KEY'], 'old-test-key')
            self.assertEqual(modules['decision.llm_backend'].MODEL_IDS['sonnet-label'],
                             'original-route')
        modules['main'].main.assert_called_once_with()
        self.assertEqual(captured['argv'], [str(REPO / 'main.py'), '--no-gui', '--help'])
        self.assertTrue(captured['repo_available'])
        self.assertTrue(captured['credential_preserved'])
        self.assertEqual(captured['route'], 'original-route')

    def test_configured_origins_are_not_subject_to_a_private_allowlist(self):
        entry = load_entry()
        for origin in ('', 'https://api.anthropic.com', 'http://localhost:4000',
                       'https://llm.example.test'):
            with self.subTest(origin=origin):
                modules = self.modules()
                env = {'ANTHROPIC_BASE_URL': origin, 'ANTHROPIC_AUTH_TOKEN': 'test-token',
                       'ANTHROPIC_API_KEY': 'old-test-key'}
                with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, modules):
                    self.assertEqual(entry.main([]), 0)
                    self.assertEqual(os.environ['ANTHROPIC_BASE_URL'], origin)
                    self.assertEqual(os.environ['ANTHROPIC_API_KEY'], 'old-test-key')
                    modules['main'].main.assert_called_once_with()

    def test_another_provider_does_not_require_an_anthropic_token(self):
        entry = load_entry()
        modules = self.modules()
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-openai'}, clear=True), \
                patch.dict(sys.modules, modules):
            self.assertEqual(entry.main([]), 0)
            self.assertNotIn('ANTHROPIC_API_KEY', os.environ)
            modules['main'].main.assert_called_once_with()

    def test_trailing_slash_and_default_argv_are_supported(self):
        entry = load_entry()
        captured = []
        modules = self.modules(lambda: captured.extend(sys.argv))
        env = {'ANTHROPIC_BASE_URL': 'https://llm.example.test/', 'ANTHROPIC_AUTH_TOKEN': 'test-token'}
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, modules), \
                patch.object(sys, 'argv', ['wrapper.py', '--help']):
            self.assertEqual(entry.main(), 0)
            self.assertNotIn('ANTHROPIC_API_KEY', os.environ)
        self.assertEqual(captured, [str(REPO / 'main.py'), '--help'])

    def test_entry_exception_restores_credentials_route_and_argv(self):
        entry = load_entry()
        modules = self.modules()
        modules['main'].main.side_effect = RuntimeError('test failure')
        previous_argv = ['outer-command']
        env = {'ANTHROPIC_BASE_URL': 'https://llm.example.test', 'ANTHROPIC_AUTH_TOKEN': 'test-token'}
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, modules), \
                patch.object(sys, 'argv', previous_argv):
            with self.assertRaisesRegex(RuntimeError, 'test failure'):
                entry.main([])
            self.assertNotIn('ANTHROPIC_API_KEY', os.environ)
            self.assertEqual(modules['decision.llm_backend'].MODEL_IDS['sonnet-label'],
                             'original-route')
            self.assertIs(sys.argv, previous_argv)


if __name__ == '__main__':
    unittest.main()
