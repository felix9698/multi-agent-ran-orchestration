"""Model provenance stays distinct from routing labels; all backends are mocks."""
from types import SimpleNamespace as NS
import json
import unittest

from assurance.coordination import DecisionUnavailable
from unittest.mock import Mock

from assurance.coordination.agents import (
    AnswerRefused, CallRecord, DETERMINISTIC, RoleAgents, RoleModels,
)
from decision.llm_backend import LLMResponse


SELECTED = 'claude-sonnet'
ROUTE = 'claude-looking-proxy-route'


def response(*, accepted=True, model='gpt-served-test', success=True, error=None):
    return LLMResponse(success=success, content='{}', parsed_json={'ok': accepted},
                       model=ROUTE, requested_model=ROUTE, response_model=model,
                       latency_ms=5, input_tokens=2, output_tokens=3, error=error)


class CoordinationModelProvenanceTests(unittest.TestCase):
    def agents(self, replies):
        backend = NS(model=ROUTE, api_key='test-private-credential',
                     generate=Mock(side_effect=replies))
        agents = RoleAgents(models=RoleModels(target=SELECTED), resolver=lambda _: backend)
        return agents, backend

    def decide(self, agents):
        def accept(payload, record):
            if not payload.get('ok'):
                raise AnswerRefused('test schema refused')
            return 'accepted'

        try:
            return agents._decide('target', 'formation', 'system', {}, {},
                                  accept, lambda: 'deterministic-result')
        except DecisionUnavailable as exc:        # 2026-09-19: no stand-in answer
            return None, exc.record

    def test_accepted_generation_preserves_selected_route_and_response_model(self):
        agents, backend = self.agents([response()])
        value, record = self.decide(agents)
        self.assertEqual(value, 'accepted')
        self.assertEqual(record.model, SELECTED)
        self.assertTrue(record.used_llm)
        generation = record.to_record()['generations'][0]
        self.assertEqual(generation['selectedModel'], SELECTED)
        self.assertEqual(generation['requestedRoute'], ROUTE)
        self.assertEqual(generation['responseModel'], 'gpt-served-test')
        self.assertTrue(generation['responseSuccess'])
        self.assertEqual(generation['inputTokens'], 2)
        self.assertEqual(generation['outputTokens'], 3)
        backend.generate.assert_called_once()

    def test_repair_records_both_actual_responses_without_overwriting(self):
        agents, backend = self.agents([
            response(accepted=False, model='first-served-model'),
            response(model='repair-served-model'),
        ])
        _, record = self.decide(agents)
        self.assertEqual(record.repair_retries, 1)
        self.assertTrue(record.accepted)
        self.assertEqual(backend.generate.call_count, 2)
        self.assertEqual([g['attempt'] for g in record.generations], [1, 2])
        self.assertEqual([g['responseModel'] for g in record.generations],
                         ['first-served-model', 'repair-served-model'])
        self.assertEqual(record.input_tokens, 4)
        self.assertEqual(record.output_tokens, 6)
        self.assertEqual(record.latency_ms, 10)

    def test_fallback_remains_deterministic_and_preserves_attempt_routes(self):
        agents, _ = self.agents([
            response(success=False, model=None, error='invalid test-private-credential'),
            response(success=False, model=None, error='invalid test-private-credential'),
        ])
        value, record = self.decide(agents)
        self.assertIsNone(value)                    # no deterministic stand-in
        self.assertFalse(record.accepted)
        self.assertFalse(record.used_llm)
        self.assertIsNone(record.fallback_reason)
        self.assertNotIn('test-private-credential', json.dumps(record.to_record()))
        self.assertEqual(len(record.generations), 2)
        self.assertTrue(all(g['requestedRoute'] == ROUTE for g in record.generations))
        self.assertTrue(all(g['selectedModel'] == SELECTED for g in record.generations))
        self.assertTrue(all(g['responseModel'] is None for g in record.generations))
        self.assertFalse(any(g['responseSuccess'] for g in record.generations))

    def test_legacy_model_label_does_not_become_an_actual_response_model(self):
        legacy = LLMResponse(success=True, content='{}', parsed_json={'ok': True}, model=SELECTED)
        agents, _ = self.agents([legacy])
        _, record = self.decide(agents)
        self.assertEqual(record.generations[0]['requestedRoute'], ROUTE)
        self.assertIsNone(record.generations[0]['responseModel'])

    def test_non_string_response_metadata_is_unknown(self):
        item = response()
        item.requested_model, item.response_model = Mock(), Mock()
        agents, _ = self.agents([item])
        _, record = self.decide(agents)
        self.assertEqual(record.generations[0]['requestedRoute'], ROUTE)
        self.assertIsNone(record.generations[0]['responseModel'])

    def test_raised_backend_errors_are_recorded_without_credentials(self):
        agents, _ = self.agents([
            RuntimeError('rejected test-private-credential'),
            RuntimeError('rejected test-private-credential'),
        ])
        _, record = self.decide(agents)
        self.assertFalse(record.accepted)
        self.assertNotIn('test-private-credential', json.dumps(record.to_record()))
        self.assertEqual([g['errorType'] for g in record.generations],
                         ['RuntimeError', 'RuntimeError'])
        self.assertTrue(all(g['responseModel'] is None for g in record.generations))

    def test_resolver_failure_does_not_invent_a_request_route(self):
        resolver = Mock(side_effect=RuntimeError('unavailable resolver'))
        agents = RoleAgents(models=RoleModels(target=SELECTED), resolver=resolver)
        _, record = self.decide(agents)
        self.assertFalse(record.accepted)
        self.assertEqual(len(record.generations), 2)
        self.assertTrue(all(g['requestedRoute'] is None for g in record.generations))
        self.assertTrue(all(g['responseModel'] is None for g in record.generations))

    def test_legacy_record_shape_is_unchanged_without_generation_metadata(self):
        record = CallRecord('target', 'legacy-label', 'formation')
        self.assertNotIn('generations', record.to_record())

    def test_record_serialization_does_not_alias_generation_rows(self):
        agents, _ = self.agents([response()])
        _, record = self.decide(agents)
        serialized = record.to_record()
        serialized['generations'][0]['responseModel'] = 'modified-copy'
        self.assertEqual(record.generations[0]['responseModel'], 'gpt-served-test')


if __name__ == '__main__':
    unittest.main()


class TheCallRecordCarriesTheAmendmentsAccounting(unittest.TestCase):
    """v3.1 amendment section 6, "LLM call": independent start and end times,
    exposed input/cache/output/reasoning usage with its scope, and each attempt's
    actual request and response.  Nothing is inferred; unknown stays None."""

    def decide(self, replies):
        backend = NS(model=ROUTE, api_key='test-private-credential',
                     generate=Mock(side_effect=replies))
        agents = RoleAgents(models=RoleModels(target=SELECTED), resolver=lambda _: backend)

        def accept(payload, record):
            if not payload.get('ok'):
                raise AnswerRefused('test schema refused')
            return 'accepted'

        try:
            _, record = agents._decide('target', 'formation', 'the system prompt',
                                       {'input.intents': []}, {}, accept, lambda: 'fallback')
        except DecisionUnavailable as exc:
            record = exc.record
        return record.to_record()['generations']

    def reply(self, *, accepted=True, usage=None, reasoning=None, content='{"ok": true}'):
        return LLMResponse(success=True, content=content, parsed_json={'ok': accepted},
                           model=ROUTE, requested_model=ROUTE, response_model='served',
                           latency_ms=5, input_tokens=9114, output_tokens=263,
                           usage=usage, reasoning_tokens=reasoning)

    def test_start_and_end_are_both_stamped_on_success(self):
        generation = self.decide([self.reply()])[0]
        self.assertIsNotNone(generation['startedAt'])
        self.assertIsNotNone(generation['endedAt'])
        self.assertLessEqual(generation['startedAt'], generation['endedAt'])

    def test_a_raised_attempt_still_gets_an_end_time_and_unknown_usage(self):
        generations = self.decide([RuntimeError('socket died'), RuntimeError('again')])
        for generation in generations:
            self.assertIsNotNone(generation['endedAt'])
            self.assertIsNone(generation['inputTokens'])
            self.assertIsNone(generation['reasoningTokens'])
            self.assertIsNone(generation['usageScope'])

    def test_anthropic_shaped_usage_excludes_cache_from_input(self):
        usage = {'input_tokens': 9114, 'output_tokens': 263,
                 'cache_read_input_tokens': 4000, 'cache_creation_input_tokens': None,
                 'output_tokens_details': {'thinking_tokens': 222}}
        generation = self.decide([self.reply(usage=usage, reasoning=222)])[0]
        self.assertEqual(4000, generation['cacheReadInputTokens'])
        self.assertIsNone(generation['cacheCreationInputTokens'], 'reported null stays null')
        self.assertEqual(222, generation['reasoningTokens'])
        self.assertIn('excludes', generation['usageScope']['inputTokens'])

    def test_openai_shaped_usage_includes_cache_in_input(self):
        usage = {'prompt_tokens': 100, 'completion_tokens': 50,
                 'prompt_tokens_details': {'cached_tokens': 40}}
        generation = self.decide([self.reply(usage=usage)])[0]
        self.assertEqual(40, generation['cacheReadInputTokens'])
        self.assertIn('includes', generation['usageScope']['inputTokens'])

    def test_each_attempt_keeps_its_own_request_and_response(self):
        first = self.reply(accepted=False, content='{"first": 1}')
        second = self.reply(content='{"second": 2}')
        generations = self.decide([first, second])
        self.assertEqual(2, len(generations))
        self.assertEqual(['{"first": 1}', '{"second": 2}'],
                         [g['responseText'] for g in generations])
        self.assertNotEqual(generations[0]['request']['prompt'],
                            generations[1]['request']['prompt'],
                            'the repair request is not the original request')
        self.assertEqual(1, len({g['request']['systemPromptSha256'] for g in generations}))
