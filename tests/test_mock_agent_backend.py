"""Hermetic contract-v2 mock responses, independent of concurrent executor work."""
import copy
import json
from pathlib import Path
import unittest

from decision.llm_backend import MockAgentBackend
from tools.campaign5.calibrate_agent_latency import representative_inputs, role_prompts, accepted_response


class MockAgentV2Tests(unittest.TestCase):
    def setUp(self):
        fixture = next(Path('tests/fixtures/agent_episodes').glob('*.json'))
        self.inputs = representative_inputs(json.loads(fixture.read_text()))
        self.backend = MockAgentBackend()
        self.prompts = role_prompts()

    def call(self, role, inputs=None, options=None):
        response = self.backend.generate('INPUTS:\n' + json.dumps(inputs or self.inputs[role]) + '\nOUTPUT SCHEMA:\n{}',
                                         self.prompts[role], options=options)
        self.assertTrue(response.success, response.error)
        # A stand-in reports no counts at all.  It used to return len//4, which
        # arrived downstream as a provider-reported measurement; see
        # ``TheStandInReportsNoTokenCounts`` below.
        self.assertIsNone(response.input_tokens)
        self.assertIsNone(response.output_tokens)
        return response

    def test_every_revised_role_first_try(self):
        for role in self.prompts:
            with self.subTest(role=role):
                self.assertTrue(accepted_response(self.call(role).parsed_json, role))

    def test_it_reads_the_flat_input_dot_name_keys_the_executor_sends(self):
        """``SINGLE_CALL.md`` names each position ``input.<name>``; the
        executor sends exactly that as the key, and the two older shapes still
        mean the same thing to a stand-in that only has to find its inputs."""
        for role in self.prompts:
            bare = self.inputs[role]
            flat = {f'input.{key}': value for key, value in bare.items()}
            nested = {'input': dict(bare)}
            with self.subTest(role=role):
                expected = self.call(role, bare).parsed_json
                self.assertEqual(expected, self.call(role, flat).parsed_json)
                self.assertEqual(expected, self.call(role, nested).parsed_json)

    def test_the_flat_shape_is_not_an_empty_input(self):
        """The failure this guards against: an unrecognised key shape reads as
        no authorization at all, and the mock answers an empty T that every
        executor rightly refuses."""
        flat = {f'input.{key}': value for key, value in self.inputs['target'].items()}
        data = self.call('target', flat).parsed_json
        self.assertTrue(data['t0']['requirements'])
        signed = {key for key, entry in self.inputs['target']['authorization'].items()
                  if isinstance(entry, dict) and 'original' in entry}
        self.assertEqual(signed, set(data['t0']['requirements']))

    def test_target_copies_authorization(self):
        data = self.call('target').parsed_json
        for key, entry in self.inputs['target']['authorization'].items():
            if isinstance(entry, dict) and 'original' in entry:
                self.assertEqual(data['t0']['requirements'][key], entry['original'])
                self.assertEqual(data['levels'][key], {'steps': entry['steps'], 'bound': entry['bound']})

    def test_missing_bound_steps_and_unit_ask_owner_without_target(self):
        for field in ('bound', 'steps', 'unit'):
            inputs = copy.deepcopy(self.inputs['target'])
            intent = inputs['intents'][0]
            intent['requirement'].pop(field)
            inputs['authorization'][intent['requirement']['reqId']].pop(field)
            data = self.call('target', inputs).parsed_json
            self.assertNotIn('t0', data)
            self.assertNotIn('levels', data)
            self.assertEqual(data['missingInformation'][0]['field'], field)

    def test_explicit_nonrelaxable_needs_no_bound(self):
        inputs = copy.deepcopy(self.inputs['target'])
        for intent in inputs['intents']:
            req = intent['requirement']
            req['steps'] = 0
            req.pop('bound')
            entry = inputs['authorization'][req['reqId']]
            entry['steps'] = 0
            entry.pop('bound')
        data = self.call('target', inputs).parsed_json
        self.assertEqual(data['missingInformation'], [])

    def test_functions_baseline_singles_pairs_and_prediction(self):
        inputs = copy.deepcopy(self.inputs['control'])
        inputs['construction_policy']['retain'] = 20
        inputs['function_catalog'] = [
            {'functionId': 'cap', 'scopes': ['ue@1'], 'axis': 'dlPrbCap@<ue>',
             'policyFields': {'maxDlPrbs': {'values': [24, 12], 'baseline': 24}}},
            {'functionId': 'steer', 'scopes': ['ue@2'], 'axis': 'servingCell@<ue>',
             'policyFields': {'servingCell': {'values': ['a', 'b'], 'baseline': 'a'}}}]
        inputs['effect_evidence']['predictions'] = [{'functions': [], 'predicted': {'r1': 3}, 'uncertainty': {'r1': .2}}]
        candidates = self.call('control', inputs).parsed_json['candidates']
        self.assertEqual(candidates[0]['functions'], [])
        self.assertEqual(candidates[0]['predicted'], {'r1': 3})
        self.assertEqual(candidates[1]['predicted'], 'unknown')
        self.assertEqual([len(row['functions']) for row in candidates], [0, 1, 1, 1, 1, 2, 2, 2, 2])

    def test_trajectory_cost_then_input_order_ignores_stale_history(self):
        inputs = copy.deepcopy(self.inputs['trajectory'])
        inputs['target_contract'] = {'t0': {'targetId': 'T0', 'cost': 0}, 'alternatives': [{'targetId': 'T1', 'cost': 2}]}
        inputs['control_candidates'] = [{'controlId': 'C1', 'predictedTarget': 'T1'},
                                       {'controlId': 'C2', 'predictedTarget': 'T0'},
                                       {'controlId': 'C3', 'predictedTarget': 'T0'}]
        inputs['observations'] = [{'controlId': 'C2', 'valid': True}, {'controlId': 'C3', 'valid': False}]
        data = self.call('trajectory', inputs).parsed_json
        self.assertEqual((data['controlId'], data['targetId']), ('C3', 'T0'))

    def test_thinking_budget_changes_real_latency(self):
        low = self.call('target', options={'thinkingBudgetTokens': 100})
        high = self.call('target', options={'thinkingBudgetTokens': 4000})
        self.assertGreater(high.latency_ms, low.latency_ms + 40)
        self.assertEqual(high.options['thinkingBudgetTokens'], 4000)

    def test_unrecognized_prompt_fails(self):
        self.assertFalse(self.backend.generate('{}', 'unknown').success)


class TheStandInReportsNoTokenCounts(MockAgentV2Tests):
    """A mock-produced count must not read as a provider measurement.

    The old ``len(text) // 4`` estimate was a plain int, so every reader
    downstream -- which only ever asks "did the provider report a number?" --
    took it for reported usage and marked the cost row complete.  The only tell
    was that ``usage`` stayed ``None``, which no call row shows.
    """

    def test_no_role_reports_a_count_or_a_usage_envelope(self):
        for role in self.prompts:
            with self.subTest(role=role):
                response = self.call(role)          # asserts the two are None
                # No envelope either: there is no provider here to have one.
                self.assertIsNone(response.usage)

    def test_the_estimate_is_not_smuggled_back_as_a_zero(self):
        """``None`` is unknown; 0 would be a claim that the call was free."""
        response = self.call('target')
        self.assertIsNot(response.input_tokens, 0)
        self.assertIsNot(response.output_tokens, 0)
