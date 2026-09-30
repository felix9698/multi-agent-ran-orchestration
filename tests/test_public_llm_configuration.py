"""Public backend configuration never reads a private deployment or contacts a model."""
import importlib.util
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, mock_open, patch


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = ROOT / 'experiment_results' / 'ota-20260911'


def load(filename):
    spec = importlib.util.spec_from_file_location('_public_' + Path(filename).stem,
                                                  CAMPAIGN / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicEntryTests(unittest.TestCase):
    def test_compatibility_entry_preserves_provider_environment_and_model_route(self):
        entry = load('authenticated_live_entry.py')
        with tempfile.TemporaryDirectory() as temporary:
            entry.AUTHENTICATION_HOLD = Path(temporary) / 'hold.json'
            for environment in (
                {'OPENAI_API_KEY': 'test-openai'},
                {'ANTHROPIC_API_KEY': 'test-anthropic', 'AIC_CLAUDE_MODEL_ID': 'configured-sonnet'},
                {'LITELLM_BASE_URL': 'http://localhost:4000/v1'},
                {},
            ):
                with self.subTest(environment=tuple(environment)):
                    observed = {}
                    main_module = ModuleType('main')
                    def run():
                        observed.update(environment=dict(os.environ), argv=list(sys.argv))
                        return 7
                    main_module.main = run
                    old_argv = ['outer']
                    with patch.dict(os.environ, environment, clear=True), \
                         patch.dict(sys.modules, {'main': main_module}), \
                         patch.object(sys, 'argv', old_argv):
                        try:
                            result = entry.main(['--help'])
                        except SystemExit as exc:
                            self.fail(f'public environment was refused: {exc}')
                        self.assertEqual(result, 7)
                        self.assertEqual(observed['environment'], environment)
                        self.assertEqual(observed['argv'], [str(ROOT / 'main.py'), '--help'])
                        self.assertIs(sys.argv, old_argv)

    def test_incident_hold_still_refuses_before_entry(self):
        entry = load('authenticated_live_entry.py')
        with tempfile.TemporaryDirectory() as temporary:
            entry.AUTHENTICATION_HOLD = Path(temporary)
            with self.assertRaisesRegex(SystemExit, 'LIVE_MODEL_AUTHENTICATION_ON_HOLD'):
                entry.main([])

    def test_child_wrapper_inherits_configuration_without_private_file_read(self):
        wrapper = load('ops/run_with_proxy.py')
        child = SimpleNamespace(wait=lambda: 9, send_signal=Mock())
        captured = {}
        def launch(argv, **kwargs):
            captured.update(argv=argv, **kwargs)
            return child
        environment = {'OPENAI_API_KEY': 'public-test-key', 'AIC_ROLE_MODEL': 'gpt-4o'}
        with patch.dict(os.environ, environment, clear=True), \
             patch.object(sys, 'argv', ['wrapper.py', str(CAMPAIGN / 'atomic_formal_run_guarded.py'), '--help']), \
             patch.object(Path, 'read_text', side_effect=AssertionError('private config read')), \
             patch.object(wrapper.subprocess, 'Popen', side_effect=launch), \
             patch.object(wrapper.signal, 'signal'):
            self.assertEqual(wrapper.main(), 9)
        self.assertEqual(captured['env'], environment)
        self.assertEqual(captured['argv'][-1], '--help')

    def test_child_wrapper_still_forwards_sigterm_once(self):
        wrapper = load('ops/run_with_proxy.py')
        child = SimpleNamespace(send_signal=Mock())
        handlers = {}
        with patch.object(wrapper.signal, 'signal', side_effect=lambda sig, fn: handlers.update({sig: fn})):
            wrapper._forward_sigterm_to(child)
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        child.send_signal.assert_called_once_with(signal.SIGTERM)
        self.assertEqual(handlers[signal.SIGTERM], signal.SIG_IGN)

    def test_offline_formation_uses_the_selected_local_model_without_private_files(self):
        with patch.object(sys, 'path', [str(CAMPAIGN), *sys.path]):
            formation = load('form_board_offline.py')
            environment = {'AIC_ROLE_MODEL': 'local:research-model',
                           'LITELLM_BASE_URL': 'http://localhost:4000/v1'}
            with patch.dict(os.environ, environment, clear=True), \
                 patch.object(Path, 'read_text', side_effect=AssertionError('private config read')):
                self.assertEqual(formation.route_to_the_proxy(), 'local:research-model')
                self.assertEqual(dict(os.environ), environment)

    def test_conductor_inherits_provider_configuration_without_logging_credentials(self):
        manifest = {'cases': {'C4': {'policy': 'P1', 'level': 'L1', 'L': 10,
                                    'dir': 'fixture', 'intentsSha256': 'a' * 64,
                                    'manifestSha256': 'b' * 64}}}
        witness = {'connections': [
            {'active': True, 'globalE2NodeId': {'nbId': 3584}, 'connectionEpoch': 1},
            {'active': True, 'globalE2NodeId': {'nbId': 2816}, 'connectionEpoch': 2},
        ]}
        with patch.object(Path, 'read_text', return_value=json.dumps(manifest)), \
             patch.object(Path, 'read_bytes', return_value=b'{}'), \
             patch.object(Path, 'open', mock_open()), \
             patch.object(subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps(witness))), \
             patch('builtins.print'):
            conductor = load('ops/conductor.py')
        environment = {'AIC_ROLE_MODEL': 'gpt-4o', 'OPENAI_API_KEY': 'test-key'}
        with patch.dict(os.environ, environment, clear=True):
            entries = conductor.formal_block(2, random.Random(7))
            self.assertEqual(dict(os.environ), environment)
        self.assertEqual({entry['env']['AIC_METHOD'] for entry in entries},
                         {'three-agent', 'basic-monolith', 'internal-monolith'})
        self.assertNotIn('test-key', json.dumps(entries))
        with patch.dict(os.environ, {'AIC_METHOD': 'deterministic'}, clear=True):
            entries = conductor.formal_block(2, random.Random(7))
        self.assertEqual([entry['env']['AIC_METHOD'] for entry in entries], ['deterministic'])
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'LLM_'):
                conductor.formal_block(2, random.Random(7))


class CampaignConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.runner = load('atomic_formal_run_guarded.py')

    def preflight(self, environment):
        self.assertTrue(callable(getattr(self.runner, 'llm_configuration_preflight', None)),
                        'campaign needs provider-neutral offline configuration preflight')
        with patch.dict(os.environ, environment, clear=True):
            return self.runner.llm_configuration_preflight()

    def test_selected_cloud_and_local_providers_do_not_need_another_providers_token(self):
        for environment in (
            {'AIC_ROLE_MODEL': 'gpt-4o', 'OPENAI_API_KEY': 'test-key'},
            {'AIC_ROLE_MODEL': 'claude-sonnet', 'ANTHROPIC_API_KEY': 'test-key',
             'AIC_CLAUDE_MODEL_ID': 'configured-sonnet'},
            {'AIC_ROLE_MODEL': 'claude-sonnet', 'ANTHROPIC_AUTH_TOKEN': 'test-key',
             'ANTHROPIC_BASE_URL': 'https://llm.example.test', 'AIC_CLAUDE_MODEL_ID': 'configured-sonnet'},
            {'AIC_ROLE_MODEL': 'gemini-pro', 'GOOGLE_API_KEY': 'test-key'},
            {'AIC_ROLE_MODEL': 'local:test-model', 'LITELLM_BASE_URL': 'http://localhost:4000/v1'},
            {'AIC_METHOD': 'deterministic'},
            {'AIC_METHOD': 'basic-monolith', 'AIC_MONOLITH_MODEL': 'rule-greedy'},
        ):
            with self.subTest(environment=tuple(environment)):
                self.preflight(environment)

    def test_missing_configuration_and_unknown_backend_refuse_without_fallback(self):
        for environment in (
            {}, {'AIC_ROLE_MODEL': 'gpt-4o'}, {'AIC_ROLE_MODEL': 'local:test-model'},
            {'AIC_ROLE_MODEL': 'unconfigured-model', 'OPENAI_API_KEY': 'test-key'},
            {'AIC_ROLE_MODEL': 'claude-sonnet', 'ANTHROPIC_API_KEY': 'test-key'},
            {'AIC_METHOD': 'basic-monolith', 'AIC_MONOLITH_MODEL': 'gpt-4o'},
        ):
            with self.subTest(environment=tuple(environment)):
                with self.assertRaises(self.runner.Refused):
                    self.preflight(environment)

    def test_runner_configuration_preflight_happens_before_traffic(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True), \
             patch.object(self.runner, 'Remote', side_effect=AssertionError('traffic started')):
            self.runner.AUTHENTICATION_HOLD = Path(temporary) / 'no-hold'
            result = self.runner.run_attempt(Path(temporary) / 'profile.json', output_dir=Path(temporary))
            report = json.loads(next(Path(temporary).glob('*/exit.json')).read_text())
        self.assertEqual(result, 3)
        self.assertIn('LLM_', report['failure']['code'])


class NativeClaudeConfigurationTests(unittest.TestCase):
    def test_unconfigured_claude_model_cannot_send_a_request_using_a_private_alias(self):
        from decision import llm_backend
        client = SimpleNamespace(messages=SimpleNamespace(create=Mock()))
        constructor = Mock(return_value=client)
        sdk = SimpleNamespace(Anthropic=constructor)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key'}, clear=True), \
             patch.dict(sys.modules, {'anthropic': sdk}):
            backend = llm_backend.ClaudeBackend()
        self.assertFalse(backend.is_available(), 'Claude requires an explicitly configured provider model')
        constructor.assert_not_called()
        response = backend.generate('do not send')
        self.assertFalse(response.success)
        client.messages.create.assert_not_called()

    def test_model_override_is_used_without_module_reload(self):
        from decision import llm_backend
        sdk = SimpleNamespace(Anthropic=lambda **kwargs: SimpleNamespace())
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key',
                                    'AIC_CLAUDE_MODEL_ID': 'configured-sonnet'}, clear=True), \
             patch.dict(sys.modules, {'anthropic': sdk}):
            backend = llm_backend.ClaudeBackend()
        self.assertEqual(backend.model, 'configured-sonnet')
        self.assertTrue(backend.is_available())


if __name__ == '__main__':
    unittest.main()
