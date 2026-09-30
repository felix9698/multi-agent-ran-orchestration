import os
"""Provider SDK calls are mocked: no API keys or network required."""
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from decision.llm_backend import (ClaudeBackend, OpenAIBackend, GeminiBackend,
                                  LiteLLMBackend, OllamaBackend, DeterministicMockBackend)


class LocalReasoningIsOffUnlessAskedFor(unittest.TestCase):
    """2026-09-23: with reasoning on, every local model on this lab's server spent the
    output limit reasoning and returned truncated JSON; role options never carry
    ``think``, so the default must be off."""

    def backend(self):
        obj = LiteLLMBackend.__new__(LiteLLMBackend)
        obj.model, obj.api_key, obj.client = 'qwen3', 'fake', Mock()
        obj.base_url, obj.max_tokens, obj.temperature, obj.timeout = 'http://unused', 4096, .3, 300.0
        obj._available = True
        obj.client.chat.completions.create.return_value = NS(
            choices=[NS(message=NS(content='{}'))], usage=NS(prompt_tokens=10, completion_tokens=2))
        return obj

    def test_role_options_without_think_turn_reasoning_off(self):
        obj = self.backend()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('LITELLM_THINK', None)
            response = obj.generate('p', options={'maxTokens': 2000, 'jsonMode': True})
        body = obj.client.chat.completions.create.call_args.kwargs['extra_body']
        self.assertIs(False, body['think'])
        self.assertEqual({'enable_thinking': False}, body['chat_template_kwargs'])
        self.assertIs(False, response.options['think'])

    def test_reasoning_can_be_asked_for(self):
        obj = self.backend()
        obj.generate('p', options={'think': True})
        self.assertNotIn('extra_body', obj.client.chat.completions.create.call_args.kwargs)
        with patch.dict(os.environ, {'LITELLM_THINK': '1'}):
            obj.generate('p', options={})
        self.assertNotIn('extra_body', obj.client.chat.completions.create.call_args.kwargs)


class BackendOptionsTests(unittest.TestCase):
    def backend(self, cls, model='model'):
        obj = cls.__new__(cls)
        obj.model, obj.api_key, obj.client = model, 'fake', Mock()
        obj.base_url, obj.max_tokens, obj.temperature, obj.timeout = 'http://unused', 4096, .3, 300.0
        obj._available = True
        return obj

    def chat(self, obj):
        obj.client.chat.completions.create.return_value = NS(
            choices=[NS(message=NS(content='{}'))], usage=NS(prompt_tokens=10, completion_tokens=2))

    @patch.dict(os.environ, {'AIC_LLM_SEND_THINKING': '1'})
    def test_claude_thinking_text_and_total_token_limit(self):
        obj = self.backend(ClaudeBackend, 'claude-sonnet-4-5')
        obj.client.messages.create.return_value = NS(content=[NS(type='thinking'), NS(type='text', text='{}')],
                                                     usage=NS(input_tokens=10, output_tokens=2))
        response = obj.generate('p', 's', options={'maxTokens': 1500, 'thinkingBudgetTokens': 4000, 'bogus': True})
        self.assertTrue(response.success, response.error)
        kwargs = obj.client.messages.create.call_args.kwargs
        self.assertEqual(kwargs['thinking'], {'type': 'enabled', 'budget_tokens': 4000})
        self.assertGreater(kwargs['max_tokens'], 4000)
        self.assertEqual(kwargs['timeout'], 300)
        self.assertNotIn('bogus', kwargs)
        self.assertEqual(response.options['maxTokens'], kwargs['max_tokens'])
        # Nothing was dropped, so there is nothing to declare.
        self.assertNotIn('notSent', response.options)

    def test_claude_distinguishes_proxy_route_and_response_model(self):
        obj = self.backend(ClaudeBackend, 'claude-looking-proxy-route')
        obj.client.messages.create.return_value = NS(
            content=[NS(type='text', text='{}')],
            usage=NS(input_tokens=10, output_tokens=2), model='gpt-served-test')
        response = obj.generate('p')
        self.assertTrue(response.success, response.error)
        self.assertEqual(response.model, 'claude-looking-proxy-route')
        self.assertEqual(response.requested_model, 'claude-looking-proxy-route')
        self.assertEqual(response.response_model, 'gpt-served-test')
        self.assertEqual(obj.client.messages.create.call_args.kwargs['model'],
                         'claude-looking-proxy-route')

    def test_claude_missing_response_model_is_unknown_not_the_alias(self):
        for reported in (None, '', '   ', Mock()):
            with self.subTest(reported=reported):
                obj = self.backend(ClaudeBackend, 'claude-looking-proxy-route')
                obj.client.messages.create.return_value = NS(
                    content=[NS(type='text', text='{}')],
                    usage=NS(input_tokens=1, output_tokens=1), model=reported)
                response = obj.generate('p')
                self.assertTrue(response.success, response.error)
                self.assertEqual(response.requested_model, 'claude-looking-proxy-route')
                self.assertIsNone(response.response_model)

    def test_claude_failure_preserves_route_without_logging_credential(self):
        obj = self.backend(ClaudeBackend, 'claude-looking-proxy-route')
        obj.api_key = 'test-private-credential'
        obj.client.messages.create.side_effect = RuntimeError('invalid test-private-credential')
        with self.assertLogs('LLMBackend', level='ERROR') as logs:
            response = obj.generate('p')
        self.assertFalse(response.success)
        self.assertEqual(response.requested_model, 'claude-looking-proxy-route')
        self.assertIsNone(response.response_model)
        self.assertNotIn('test-private-credential', response.error)
        self.assertNotIn('test-private-credential', '\n'.join(logs.output))

    @patch.dict(os.environ, {'AIC_LLM_SEND_THINKING': '1'})
    def test_claude_sends_thinking_whatever_the_model_id(self):
        """2026-09-23: the budget is sent by backend kind, not model-id regex.

        The old gate dropped it for ``claude-3-haiku`` and for this lab's
        obfuscated ``claude-fable-5-...`` route alike, so 40 sittings sent none.
        An endpoint that refuses it is recorded in ``notSent`` instead (see
        tests/test_llm_backend_wire_and_transport.py).
        """
        served = 'claude-fable-5-dd-weiver-otua-xedoc'
        obj = self.backend(ClaudeBackend, served)
        obj.client.messages.create.return_value = NS(
            content=[NS(type='text', text='{}')], model='gpt-5.6-luna',
            usage=NS(input_tokens=3148, output_tokens=3728,
                     output_tokens_details={'thinking_tokens': 2407}))
        response = obj.generate('p', options={'maxTokens': 3000, 'thinkingBudgetTokens': 8000,
                                              'reasoningEffort': 'high', 'jsonMode': True})
        self.assertTrue(response.success, response.error)
        kwargs = obj.client.messages.create.call_args.kwargs
        self.assertEqual({'type': 'enabled', 'budget_tokens': 8000}, kwargs['thinking'])
        self.assertEqual(11000, kwargs['max_tokens'])
        self.assertEqual(8000, response.options['thinkingBudgetTokens'])
        unsent = response.options['notSent']
        self.assertNotIn('thinkingBudgetTokens', unsent)
        # Anthropic's Messages API has neither of these parameters.
        self.assertIn('reasoningEffort', unsent)
        self.assertIn('jsonMode', unsent)
        # The reasoning bill is reported, not inferred from the sent 8000.
        self.assertEqual(2407, response.reasoning_tokens)
        self.assertEqual(3728, response.output_tokens)

    def test_openai_reasoning_and_regular_models(self):
        for model, reasoning in [('gpt-4o', False), ('o3-mini', True), ('gpt-5', True)]:
            obj = self.backend(OpenAIBackend, model)
            self.chat(obj)
            response = obj.generate('p', options={'maxTokens': 1200, 'reasoningEffort': 'medium', 'think': False})
            self.assertTrue(response.success, response.error)
            kwargs = obj.client.chat.completions.create.call_args.kwargs
            self.assertEqual(kwargs['timeout'], 300)
            self.assertEqual(kwargs['max_completion_tokens' if reasoning else 'max_tokens'], 1200)
            self.assertEqual('reasoning_effort' in kwargs, reasoning)
            self.assertNotIn('think', kwargs)

    def test_litellm_options_and_json_rejection_retry(self):
        obj = self.backend(LiteLLMBackend)
        self.chat(obj)
        answer = obj.client.chat.completions.create.return_value
        rejection = Exception('unsupported response_format')
        rejection.status_code = 400
        obj.client.chat.completions.create.side_effect = [rejection, answer]
        response = obj.generate('p', options={'maxTokens': 123, 'think': False, 'jsonMode': True, 'reasoningEffort': 'high'})
        self.assertTrue(response.success, response.error)
        calls = obj.client.chat.completions.create.call_args_list
        self.assertEqual(calls[0].kwargs['response_format'], {'type': 'json_object'})
        self.assertEqual(calls[0].kwargs['extra_body']['chat_template_kwargs'], {'enable_thinking': False})
        self.assertEqual(calls[0].kwargs['max_tokens'], 123)
        self.assertNotIn('response_format', calls[1].kwargs)
        self.assertNotIn('jsonMode', response.options)

    def test_transport_timeout_is_failure_without_json_retry(self):
        obj = self.backend(LiteLLMBackend)
        obj.client.chat.completions.create.side_effect = TimeoutError('hang guard')
        response = obj.generate('p', options={'jsonMode': True})
        self.assertFalse(response.success)
        self.assertEqual(obj.client.chat.completions.create.call_count, 1)
        self.assertIn('hang guard', response.error)
        self.assertGreater(response.latency_ms, 0)

    def test_gemini_timeout(self):
        obj = self.backend(GeminiBackend)
        obj.client.generate_content.return_value = NS(text='{}')
        self.assertTrue(obj.generate('p', options={'maxTokens': 120}).success)
        kwargs = obj.client.generate_content.call_args.kwargs
        self.assertEqual(kwargs['request_options']['timeout'], 300)
        self.assertEqual(kwargs['generation_config']['max_output_tokens'], 120)

    @patch('requests.post')
    @patch.object(OllamaBackend, 'is_available', return_value=True)
    def test_ollama_native_options_and_timeout(self, available, post):
        obj = self.backend(OllamaBackend)
        post.return_value = NS(status_code=200, json=lambda: {'response': '{}'})
        response = obj.generate('p', options={'maxTokens': 90, 'think': False, 'jsonMode': True})
        self.assertTrue(response.success, response.error)
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs['timeout'], 300)
        self.assertEqual(kwargs['json']['options']['num_predict'], 90)
        self.assertEqual(kwargs['json']['format'], 'json')
        self.assertFalse(kwargs['json']['think'])

    def test_legacy_mock_ignores_options(self):
        self.assertTrue(DeterministicMockBackend().generate('p', options={'unsupported': 1}).success)
