"""Thinking budget on the wire, and transport failures kept apart from refusals.

2026-09-23 owner: "LLM 생각 예산이 한 번도 전송되지 않는 것 -- 이것도 고쳐".
The request body is intercepted with a mock; nothing touches a network.
"""
from types import SimpleNamespace as NS
import os
import unittest
from unittest.mock import Mock, patch

from decision.llm_backend import ClaudeBackend, LLMResponse, MODEL_IDS, LLMBackendType
from assurance.coordination.agents import DecisionUnavailable, RoleAgents, RoleModels


def _claude(model):
    obj = ClaudeBackend.__new__(ClaudeBackend)
    obj.model, obj.api_key, obj.client = model, 'fake', Mock()
    obj.backend_type = LLMBackendType.CLAUDE_SONNET
    return obj


class ThinkingOnTheWire(unittest.TestCase):
    def test_by_default_the_budget_is_declared_unsent_and_the_answer_limit_holds(self):
        """2026-09-23 owner review: the 8317 proxy strips ``thinking`` (probe 09-14),
        so sending it would only widen max_tokens and break the equal output limit."""
        obj = _claude('gpt-5.6-luna')
        obj.client.messages.create.return_value = NS(
            content=[NS(type='text', text='{}')], usage=NS(input_tokens=1, output_tokens=1))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('AIC_LLM_SEND_THINKING', None)
            response = obj.generate('p', options={'maxTokens': 2000, 'thinkingBudgetTokens': 4000})
        body = obj.client.messages.create.call_args.kwargs
        self.assertNotIn('thinking', body)
        self.assertEqual(2000, body['max_tokens'])
        self.assertIn('strips', response.options['notSent']['thinkingBudgetTokens'])

    @patch.dict(os.environ, {'AIC_LLM_SEND_THINKING': '1'})
    def test_budget_is_sent_for_the_id_this_lab_actually_serves(self):
        # The proxy route id is neither a claude-* name nor stable; the backend
        # kind (Anthropic Messages API) is what decides, not the name.
        for model in ('gpt-5.6-luna', 'claude-fable-5-dd-anul-6.5-tpg',
                      MODEL_IDS[LLMBackendType.CLAUDE_SONNET]):
            with self.subTest(model=model):
                obj = _claude(model)
                obj.client.messages.create.return_value = NS(
                    content=[NS(type='text', text='{}')],
                    usage=NS(input_tokens=1, output_tokens=1))
                response = obj.generate('p', options={'maxTokens': 4000,
                                                      'thinkingBudgetTokens': 8000})
                self.assertTrue(response.success, response.error)
                body = obj.client.messages.create.call_args.kwargs
                self.assertEqual({'type': 'enabled', 'budget_tokens': 8000}, body['thinking'])
                # The answer allowance survives the thinking budget.
                self.assertEqual(12000, body['max_tokens'])
                self.assertEqual(8000, response.options['thinkingBudgetTokens'])
                self.assertNotIn('thinkingBudgetTokens', response.options.get('notSent', {}))

    @patch.dict(os.environ, {'AIC_LLM_SEND_THINKING': '1'})
    def test_endpoint_rejection_is_recorded_not_silent(self):
        obj = _claude('gpt-5.6-luna')
        rejection = Exception('thinking: not supported for this model')
        rejection.status_code = 400
        answer = NS(content=[NS(type='text', text='{}')], usage=NS(input_tokens=1, output_tokens=1))
        obj.client.messages.create.side_effect = [rejection, answer]
        response = obj.generate('p', options={'maxTokens': 3000, 'thinkingBudgetTokens': 8000})
        self.assertTrue(response.success, response.error)
        first, second = obj.client.messages.create.call_args_list
        self.assertIn('thinking', first.kwargs)
        self.assertNotIn('thinking', second.kwargs)
        self.assertEqual(3000, second.kwargs['max_tokens'])
        self.assertNotIn('thinkingBudgetTokens', response.options)
        self.assertIn('rejected', response.options['notSent']['thinkingBudgetTokens'])

    def test_unrelated_400_is_not_blamed_on_thinking(self):
        obj = _claude('gpt-5.6-luna')
        rejection = Exception('prompt is too long')
        rejection.status_code = 400
        obj.client.messages.create.side_effect = rejection
        with self.assertLogs('LLMBackend', level='ERROR'):
            response = obj.generate('p', options={'thinkingBudgetTokens': 8000})
        self.assertFalse(response.success)
        self.assertFalse(response.transport_failure)
        self.assertEqual(1, obj.client.messages.create.call_count)

    def test_timeout_and_429_are_transport_failures(self):
        for exc in (TimeoutError('read timed out'),
                    type('APITimeoutError', (Exception,), {})('Request timed out.'),
                    type('RateLimitError', (Exception,), {'status_code': 429})('slow down'),
                    type('APIStatusError', (Exception,), {'status_code': 503})('unavailable')):
            with self.subTest(exc=exc):
                obj = _claude('gpt-5.6-luna')
                obj.client.messages.create.side_effect = exc
                with self.assertLogs('LLMBackend', level='ERROR'):
                    response = obj.generate('p')
                self.assertFalse(response.success)
                self.assertTrue(response.transport_failure)


class _Flaky:
    """Two transport failures, then a real answer."""
    model = 'flaky-route'

    def __init__(self, failures=2, error='503 Service Unavailable'):
        self.prompts, self.failures, self.error = [], failures, error

    def generate(self, prompt, system_prompt='', options=None):
        self.prompts.append(prompt)
        if len(self.prompts) <= self.failures:
            return LLMResponse(success=False, content='', error=self.error,
                               transport_failure=True, options={'maxTokens': 1})
        return LLMResponse(success=True, content='{"ok": 1}', parsed_json={'ok': 1},
                           input_tokens=1, output_tokens=1, options={'maxTokens': 1})


class TransportIsNotARefusal(unittest.TestCase):
    def decide(self, backend, reask=None):
        agents = RoleAgents(models=RoleModels(target='m'), resolver=lambda name: backend,
                            may_reask=reask)
        with patch('assurance.coordination.agents.time.sleep') as sleep:
            try:
                value, record = agents._decide('target', 'formation', 'sys', {'x': 1}, {},
                                               lambda parsed, record: parsed, lambda: None)
            except DecisionUnavailable as exc:
                value, record = None, exc.record
        return value, record, sleep

    def test_retried_with_backoff_without_repair_or_refusal_text(self):
        backend = _Flaky(failures=2)
        reask = Mock(return_value=True)
        value, record, sleep = self.decide(backend, reask)
        self.assertEqual({'ok': 1}, value)
        self.assertEqual(0, record.repair_retries)
        self.assertEqual(2, record.transport_retries)
        reask.assert_not_called()
        self.assertEqual(len(set(backend.prompts)), 1)
        self.assertNotIn('refused', backend.prompts[-1])
        self.assertEqual(3, len(record.generations))
        self.assertIn('503', record.generations[0]['transportFailure'])
        self.assertIsNone(record.generations[0]['refusedBecause'])
        self.assertIsNone(record.generations[2]['transportFailure'])
        delays = [c.args[0] for c in sleep.call_args_list]
        self.assertEqual(sorted(delays), delays)
        self.assertLess(delays[0], delays[1])
        self.assertEqual(2, record.to_record()['transportRetries'])

    def test_exhausted_transport_ends_the_decision_as_transport(self):
        backend = _Flaky(failures=99)
        value, record, _ = self.decide(backend)
        self.assertIsNone(value)
        self.assertFalse(record.accepted)
        self.assertEqual(0, record.repair_retries)
        self.assertTrue(all(g['transportFailure'] for g in record.generations))
        self.assertTrue(all('refused' not in p for p in backend.prompts))

    def test_a_timeout_is_not_resent(self):
        """2026-09-23 오너: 너무 오래 기다리지 마라 -- a timeout already spent the call timeout."""
        backend = _Flaky(failures=99, error='Request timed out.')
        value, record, sleep = self.decide(backend)
        self.assertIsNone(value)
        self.assertEqual(1, len(backend.prompts))
        self.assertEqual(0, record.transport_retries)
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
