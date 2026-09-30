"""The condition, block and repetition identifiers exp_metrics.md section 5 asks for.

Every episode already recorded block and repetition; the condition was an empty
object, so the aggregator pooled every run under one unnamed condition. These
cover the flag that fills it and the request field it lands in. Hermetic: no
model, no radio, no runner subprocess.
"""
import importlib.util
from pathlib import Path
import sys
import unittest

import main
from tools.liveconsole.agent import AgentRequest

RUNNER = (Path(__file__).resolve().parents[1]
          / 'experiment_results/ota-20260911/atomic_formal_run_guarded.py')


class TheConditionFlag(unittest.TestCase):
    def test_one_pair_becomes_the_condition(self):
        self.assertEqual({'name': 'contention-boundary'},
                         main._parse_condition(['name=contention-boundary']))

    def test_repeating_the_flag_accumulates(self):
        self.assertEqual({'name': 'light-load', 'load': 'nominal'},
                         main._parse_condition(['name=light-load', 'load=nominal']))

    def test_a_value_may_contain_an_equals_sign(self):
        self.assertEqual({'note': 'a=b'}, main._parse_condition(['note=a=b']))

    def test_surrounding_space_is_trimmed(self):
        self.assertEqual({'name': 'x'}, main._parse_condition([' name = x ']))

    def test_no_flag_is_an_empty_condition_not_an_error(self):
        self.assertEqual({}, main._parse_condition(None))
        self.assertEqual({}, main._parse_condition([]))

    def test_a_pair_without_an_equals_sign_is_refused_by_name(self):
        with self.assertRaisesRegex(SystemExit, 'no-equals-sign'):
            main._parse_condition(['no-equals-sign'])

    def test_an_empty_key_is_refused(self):
        with self.assertRaisesRegex(SystemExit, 'expected --condition'):
            main._parse_condition(['=value'])


class WhatTheRequestCarries(unittest.TestCase):
    def test_the_identifiers_reach_the_episode_record(self):
        request = AgentRequest(sentences=('x',),
                               condition={'name': 'contention-boundary'},
                               block=2, repetition=3)
        record = request.to_record()
        self.assertEqual({'name': 'contention-boundary'}, record['condition'])
        self.assertEqual(2, record['block'])
        self.assertEqual(3, record['repetition'])

    def test_an_unnamed_condition_still_records_the_other_two(self):
        record = AgentRequest(sentences=('x',), block=1, repetition=4).to_record()
        self.assertEqual({}, record['condition'])
        self.assertEqual(1, record['block'])
        self.assertEqual(4, record['repetition'])

    def test_the_request_does_not_share_the_mapping_it_was_given(self):
        given = {'name': 'light-load'}
        request = AgentRequest(sentences=('x',), condition=given)
        given['name'] = 'something else'
        self.assertEqual({'name': 'light-load'}, dict(request.condition))


class WhatTheRunnerPasses(unittest.TestCase):
    def setUp(self):
        directory = str(RUNNER.parent)
        if directory not in sys.path:
            sys.path.insert(0, directory)
        spec = importlib.util.spec_from_file_location('_runner_condition', RUNNER)
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)

    def test_every_attempt_names_a_condition(self):
        import tempfile
        argv = self.runner.command_for(Path(tempfile.mkdtemp()),
                                       {'901': 'ue1', '902': 'ue2', '903': 'ue3'})
        self.assertIn('--condition', argv)
        named = argv[argv.index('--condition') + 1]
        self.assertTrue(named.startswith('name='), named)
        self.assertEqual('0', argv[argv.index('--block') + 1])
        self.assertEqual('0', argv[argv.index('--repetition') + 1])

    def test_the_environment_can_override_the_name(self):
        import os
        import tempfile
        from unittest.mock import patch
        with patch.dict(os.environ, {'AIC_CONDITION': 'light-load-control',
                                     'AIC_BLOCK': '2', 'AIC_REPETITION': '5'}):
            argv = self.runner.command_for(Path(tempfile.mkdtemp()),
                                           {'901': 'ue1', '902': 'ue2', '903': 'ue3'})
        self.assertEqual('name=light-load-control', argv[argv.index('--condition') + 1])
        self.assertEqual('2', argv[argv.index('--block') + 1])
        self.assertEqual('5', argv[argv.index('--repetition') + 1])


if __name__ == '__main__':
    unittest.main()
