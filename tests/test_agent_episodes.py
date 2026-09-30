"""Runner integration stays on the real composition with emulated radio ports."""
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.agent_episodes import SCENARIOS, parse_models, run_block, run_matrix
from tools.campaign5.run_agent_experiments import main


class AgentEpisodeExperiments(unittest.TestCase):
    def test_trajectory_ablation_reuses_exact_pair_with_real_composition(self):
        scenario = replace(SCENARIOS['two-ue'], intents=SCENARIOS['two-ue'].intents[:1],
                           ues={'131': '12345678'}, caps={},
                           axis_exposure={'axes': ['servingCell']})
        with tempfile.TemporaryDirectory() as directory, \
                patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \
                patch('subprocess.Popen', side_effect=AssertionError('subprocess forbidden')):
            rows = run_block(scenario, 'contention-boundary',
                ['three-agent', 'internal-monolith', 'basic-monolith'], 0,
                'mock:agent', 1, 0, directory, ablation='trajectory')
            # The basic monolith is not in this comparison: it has no T and no
            # C to hold identical, and SINGLE_CALL.md forbids showing it ours.
            self.assertEqual(2, len(list((Path(directory) / 'episodes').glob('*.json'))))
        self.assertEqual([1, 2], [r['resourceCost']['reuseCount'] for r in rows])
        for row in rows:
            self.assertEqual(['basic-monolith'], row['condition']['methodsWithoutGrid'])
        for row in rows:
            self.assertEqual('trajectory-only', row['condition']['ablation'])
            self.assertEqual(rows[0]['T'], row['T'])
            self.assertEqual(rows[0]['C'], row['C'])
            self.assertEqual(rows[0]['tcHashes'], row['tcHashes'])
            self.assertEqual(rows[0]['resourceCost']['reuseGroup'], row['resourceCost']['reuseGroup'])
            self.assertGreaterEqual(row['resourceCost']['priorPrepMs'], 0)
            self.assertFalse(any(c['phase'] == 'formation' for c in row['calls']))

    def test_trajectory_refuses_changed_pair_before_evidence_publication(self):
        from unittest.mock import Mock
        source = Mock()
        source.contract.to_record.return_value = {'t0': 'original'}
        source.controls.to_record.return_value = {'candidates': []}
        source.timing = {'prepMs': 7}
        source.resource_cost.return_value = {'prepMs': 7}
        source.agents.calls = []
        changed = Mock()
        changed.episode.return_value.to_record.return_value = {'T': {'t0': 'changed'}, 'C': {'candidates': []}}
        with tempfile.TemporaryDirectory() as directory, \
                patch('experiments.agent_episodes.build_agent_sitting', side_effect=[source, changed]), \
                patch('experiments.agent_episodes.write_agent_evidence') as write:
            with self.assertRaisesRegex(ValueError, 'changed the injected T/C'):
                run_block('two-ue', 'contention-boundary', ['three-agent'], 0,
                          'mock:agent', 1, 0, directory, live_profile='unused', ablation='trajectory')
            write.assert_not_called()
            self.assertFalse((Path(directory) / 'episodes').exists())

    def test_exclusion_is_explicit_external_and_method_independent(self):
        from experiments.agent_episodes import EXCLUSION_RULES
        for name, predicate in EXCLUSION_RULES.items():
            for method in ('three-agent', 'internal-monolith', 'basic-monolith'):
                self.assertIsNone(predicate({'method': method, 'termination': {'reason': 'ERROR'}}))
                failure = {'kind': name, 'cause': 'external', 'reason': 'instrument offline'}
                self.assertEqual('instrument offline', predicate({'method': method, 'failures': [failure]}))
                failure['methodCaused'] = True
                self.assertIsNone(predicate({'failures': [failure]}))
                failure.update(cause='method', methodCaused=False)
                self.assertIsNone(predicate({'failures': [failure]}))

    def test_excluded_episode_written_and_affected_block_repeated_once(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.json'
            source.write_text(json.dumps({'method': 'deterministic', 'timing': {},
                'failures': [{'kind': 'logging-failure', 'cause': 'external', 'reason': 'disk offline'}]}))
            def evidence(sitting):
                record = json.loads(source.read_text())
                request = sitting.request
                record.update(episodeId=request.episode_id, block=request.block,
                              repetition=request.repetition, condition=request.condition)
                source.write_text(json.dumps(record))
                return {'episode': str(source)}
            def build(profile, request, **kwargs):
                from unittest.mock import Mock
                return Mock(request=request)
            with patch('experiments.agent_episodes.build_agent_sitting', side_effect=build), \
                    patch('experiments.agent_episodes.write_agent_evidence', side_effect=evidence), \
                    patch('experiments.agent_episodes.agent_figures.render_all', return_value=[]):
                rows = run_matrix(scenarios=['two-ue'], methods=['deterministic'],
                    repetitions=1, budget=1, out_dir=directory, live_profile='unused',
                    exclusion_rule='logging-failure')
            self.assertEqual(2, len(rows))
            self.assertEqual([0, 1], [r['attempt'] for r in rows])
            self.assertEqual([0, 0], [r['block'] for r in rows])
            self.assertTrue(all(r['excluded']['rule'] == 'logging-failure' for r in rows))
            self.assertEqual(2, len(list((Path(directory) / 'episodes').glob('*.json'))))
            summary = json.loads((Path(directory) / 'metrics.json').read_text())
            self.assertEqual(0, summary['groups'][0]['N'])
            self.assertEqual(2, summary['groups'][0]['resolution']['excludedCount'])
            manifest = json.loads((Path(directory) / 'manifest.json').read_text())
            self.assertEqual('logging-failure', manifest['exclusionRule'])
            self.assertEqual(2, len(manifest['blocks']))
        with self.assertRaises(ValueError):
            run_matrix(exclusion_rule='method-error')

    def test_ablation_and_exclusion_cli(self):
        with patch('tools.campaign5.run_agent_experiments.run_matrix', return_value=[]) as matrix:
            main(['--ablation', 'trajectory', '--exclusion-rule', 'logging-failure', '--out', '/unused'])
        self.assertEqual('trajectory', matrix.call_args.kwargs['ablation'])
        self.assertEqual('logging-failure', matrix.call_args.kwargs['exclusion_rule'])

    def test_wide_axes_cli_passes_composite_ladders_without_splitting(self):
        with patch('tools.campaign5.run_agent_experiments.run_matrix', return_value=[]) as matrix:
            main(['--scenario', 'wide-axes', '--axes', 'all', '--max-catalog', '2048',
                  '--cap-axis', '131:0,6', '--pf-axis', '131:0.5,1.0',
                  '--mcs-axis', '12345678:0..28,10..28',
                  '--atten-axis', '12345678:0.0,12.0',
                  '--slice-axis', '1:0:1:100,0:1:30', '--out', '/unused'])
            exposure = matrix.call_args.kwargs['axis_exposure']
            self.assertEqual(exposure['axes'], ('all',))
            self.assertEqual(exposure['max_catalog_cardinality'], 2048)
            self.assertEqual(exposure['slice_quotas']['1'], ('0:1:100', '0:1:30'))
            self.assertEqual(exposure['mcs_bounds']['12345678'], ('0..28', '10..28'))

    def test_wide_axes_scenario_real_composition_and_override_record(self):
        # Sixteen combinations exercise all six ports without benchmarking the catalog.
        scenario = replace(SCENARIOS['wide-axes'],
            intents=SCENARIOS['wide-axes'].intents[:1], ues={'131': '12345678'},
            cells={'12345678': 5.0, '87654321': 5.0}, caps={'131': (0, 12)},
            axis_exposure={'axes': ['all'], 'pf_weights': {'131': (1.0, 2.0)},
                'mcs_bounds': {'12345678': ('0..28', '0..16')},
                'tx_attenuations': {'12345678': ('0.0', '6.0')},
                'slice_quotas': {'1': ('0:1:100', '0:1:60')}})
        with tempfile.TemporaryDirectory() as directory, \
                patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \
                patch('subprocess.Popen', side_effect=AssertionError('subprocess forbidden')):
            record = run_block(scenario, 'contention-boundary', ['deterministic'],
                0, 'mock:agent', 1, 0, directory,
                axis_exposure={'max_catalog_cardinality': 1024})[0]
        preflight = record['execution']['preflight']
        self.assertEqual(6, len(preflight['exposedAxisKinds']))
        self.assertEqual(1024, preflight['catalogCeiling'])
        self.assertIn('dlMcsBounds@12345678', preflight['axisScopes'])
        self.assertIn('slicePrbQuota@1', preflight['axisScopes'])
        self.assertEqual(record['axisExposure']['mcs_bounds'], scenario.axis_exposure['mcs_bounds'])
        self.assertTrue(record['trials'])

    def test_matched_matrix_writes_episodes_metrics_and_figures_without_network(self):
        from tools.liveconsole.agent import build_hardware_free_agent_sitting
        initial_states = []

        def build(request, **kwargs):
            ran = kwargs['ran']
            initial_states.append((id(ran), dict(ran.ues), dict(ran.caps),
                                   dict(ran.pf_weights), ran.seed, dict(ran.offered_load_mbps)))
            return build_hardware_free_agent_sitting(request, **kwargs)

        with tempfile.TemporaryDirectory() as directory, \
                patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \
                patch('subprocess.Popen', side_effect=AssertionError('subprocess forbidden')), \
                patch('experiments.agent_episodes.build_hardware_free_agent_sitting', side_effect=build):
            records = run_matrix(scenarios=(replace(SCENARIOS['three-ue'], caps={},
                                           axis_exposure={'axes': ['servingCell']},
                                           intents=SCENARIOS['three-ue'].intents[:1],
                                           ues={'131': '12345678'}),),
                                 # 2026-09-19: no deterministic stand-in for a model, and
                                 # the offline mock model forms no T, so the formation
                                 # arm is the model-free method here.
                                 methods=('deterministic', 'basic-monolith'), repetitions=2,
                                 budget=2, seed=0, out_dir=directory, load_scale=1.25)
            out = Path(directory)
            self.assertEqual(4, len(records))
            self.assertEqual(4, len(list((out / 'episodes').glob('*.json'))))
            required = {'schemaVersion', 'episodeId', 'method', 'sessionMode', 'condition',
                        'block', 'repetition', 'models', 'intents', 'T', 'C', 'budget',
                        'timing', 'calls', 'trials', 'serviceTrace', 'bestAttained',
                        'retained', 'firstSuccess', 't0Success', 'termination', 'resourceCost'}
            for record in records:
                self.assertTrue(required <= record.keys())
                self.assertEqual('MOCK', record['sessionMode'])
                self.assertEqual('full-construction', record['condition']['ablation'])
                self.assertNotIn('preparation', record)
                if record['method'] == 'deterministic':
                    self.assertTrue(record['trials'])
                else:
                    # The mock model's answer is unusable and nothing stands in
                    # for it (2026-09-19): no trial is taken on its behalf.
                    self.assertNotEqual('T0_SUCCESS', record['termination']['reason'])
                self.assertEqual('I4', record['unsupportedRequirements'][0]['intentId'])
                self.assertTrue(Path(record['evidence']['events']).is_file())
            self.assertNotEqual(records[0]['methodOrder'], records[2]['methodOrder'])
            for left, right in ((initial_states[0], initial_states[1]),
                                (initial_states[2], initial_states[3])):
                self.assertNotEqual(left[0], right[0])
                self.assertEqual(left[1:], right[1:])
                self.assertEqual({5.0}, set(left[-1].values()))
            summary = json.loads((out / 'metrics.json').read_text())
            self.assertEqual(4, sum(group['N'] for group in summary['groups']))
            manifest = json.loads((out / 'manifest.json').read_text())
            self.assertEqual([0, 1], [b['seed'] for b in manifest['blocks']])
            self.assertTrue(manifest['promptVersions'])
            self.assertEqual(4, manifest['episodeCount'])
            for name in ('figure1-trajectory.png', 'figure3-search-efficiency.png'):
                self.assertGreater((out / 'figures' / name).stat().st_size, 1000)

    def test_two_ue_scenario_preserves_the_handoff_conflicting_floor_and_ceiling(self):
        intents = SCENARIOS['two-ue'].intents
        self.assertEqual(('>=', 1.5), (intents[1]['requirement']['op'], intents[1]['requirement']['value']))
        self.assertEqual(('<=', 1.0), (intents[2]['requirement']['op'], intents[2]['requirement']['value']))
        with tempfile.TemporaryDirectory() as directory:
            record = run_block(replace(SCENARIOS['two-ue'], caps={}),
                               'contention-boundary', ['deterministic'],
                               0, 'mock:deterministic', 1, 0, directory)[0]
            self.assertEqual(3, len(record['intents']))
            self.assertFalse(record['t0Success'])

    def test_v2_cli_matrix_answers_observation_and_calibration(self):
        from experiments.agent_metrics import load_episodes
        from decision.llm_backend import MockAgentBackend
        with tempfile.TemporaryDirectory() as directory, \
                patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \
                patch('subprocess.Popen', side_effect=AssertionError('subprocess forbidden')), \
                patch('decision.llm_backend.LLMBackendManager') as manager:
            manager.return_value.resolve_object.return_value = MockAgentBackend()
            root = Path(directory)
            answers = root / 'answers.json'
            answers.write_text(json.dumps({'I2': {'steps': 2, 'bound': 1.0}}))
            rules = {'dlGoodputMbps': {'settleMs': 1000, 'windowMs': 2000,
                     'statistic': 'mean', 'minCoverage': 0.5, 'validityMs': 60000}}
            observe = root / 'observe.json'
            observe.write_text(json.dumps(rules))
            calibration = root / 'calibration.json'
            calibration.write_text(json.dumps({'mock:agent': {role: {
                '1234': {'p50': 1, 'p95': 2}, '9999': {'p50': 90000, 'p95': 99000}}
                for role in ('target', 'control', 'trajectory', 'monolith-form', 'monolith-select')}}))
            out = root / 'out'
            self.assertEqual(0, main(['--scenario', 'intake-incomplete', '--methods',
                'three-agent,internal-monolith', '--model', 'mock:agent', '--repetitions', '1',
                '--budget', '2', '--answers', str(answers), '--observe', str(observe),
                '--calibration', str(calibration), '--retain', '4', '--out', str(out)]))
            episodes = load_episodes(out / 'episodes')
            self.assertEqual(2, len(episodes))
            for episode in episodes:
                self.assertEqual('agent-episode/1.3.0', episode['schemaVersion'])
                self.assertEqual(rules['dlGoodputMbps'], episode['measurementRules']['dlGoodputMbps'])
                self.assertEqual(2, episode['intents'][1]['requirement']['steps'])
                self.assertEqual(1.0, episode['intents'][1]['requirement']['bound'])
                calls = [c for c in episode['calls'] if c['phase'] != 'intake']
                self.assertTrue(calls)
                self.assertTrue(all(c['options']['thinkingBudgetTokens'] == 1234 for c in calls))
                self.assertTrue(any(c['model'] == 'mock:agent' and c['accepted'] for c in calls), calls)
                # C0 is the baseline and is always first, carrying a
                # configuration rather than a function selection; what the
                # agent contributed is the candidates after it.
                self.assertEqual('C0', episode['C']['candidates'][0]['controlId'])
                self.assertTrue(any('functions' in candidate
                                    for candidate in episode['C']['candidates'][1:]),
                                episode['C']['candidates'])
                self.assertIn('cost', episode['T']['alternatives'][0])
            manifest = json.loads((out / 'manifest.json').read_text())
            self.assertEqual(rules['dlGoodputMbps'], manifest['settings']['observation']['dlGoodputMbps'])
            self.assertEqual(str(calibration), manifest['settings']['latencyCalibrationPath'])
            self.assertEqual(manifest['settings'], manifest['blocks'][0]['settings'])
            self.assertNotIn('steps', SCENARIOS['intake-incomplete'].intents[1]['requirement'])

    def test_calibrate_runs_before_matrix_and_forwards_generated_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'agent-latency-calibration.json'
            with patch('tools.campaign5.run_agent_experiments.calibrate', return_value=path) as calibration, \
                    patch('tools.campaign5.run_agent_experiments.run_matrix', return_value=[]) as matrix:
                self.assertEqual(0, main(['--models', 'target=mock:agent,control=mock:agent',
                                         '--calibrate', '--out', directory]))
                self.assertEqual(['mock:agent'], calibration.call_args.args[0])
                self.assertEqual(path, matrix.call_args.kwargs['calibration'])

    def test_v2_metrics_staleness_bound_and_per_kpi_rules(self):
        from experiments.agent_metrics import (concession_of, decision_staleness,
                                              generation_options_summary, service_deficit)
        episode = {'intents': [SCENARIOS['two-ue'].intents[0]],
            'timing': {'t0': 0}, 'budget': {'horizonHMs': 5000},
            'measurementRules': {'dlGoodputMbps': {'minCoverage': 0.2, 'statistic': 'last'}},
            'serviceTrace': [{'t': 1000, 'kpis': {'dlGoodputMbps@131': 2.0}}],
            'calls': [{'phase': 'formation', 'staleAtArrival': True},
                      {'phase': 'selection', 'role': 'trajectory', 'model': 'mock:agent',
                       'options': {'maxTokens': 100}, 'staleAtArrival': True},
                      {'phase': 'selection', 'role': 'trajectory', 'model': 'mock:agent'}],
            'execution': {'reobservations': [{'observedAt': 2000, 'windowEnd': 7000}]}}
        self.assertEqual(1.0, concession_of(episode, {'requirements': {'I1.r1': 2.0}})['max'])
        stale = decision_staleness([episode])
        self.assertEqual(0.5, stale['fraction'])
        self.assertEqual(5000, stale['reobservationMs'])
        self.assertEqual(1, service_deficit(episode)['I1.r1']['validBins'])
        self.assertEqual(3, sum(row['calls'] for row in generation_options_summary([episode])))
        self.assertIsNone(decision_staleness([{}])['fraction'])

    def test_incomplete_intake_refuses_without_answers(self):
        from tools.liveconsole.agent import ClarificationNeeded
        with tempfile.TemporaryDirectory() as directory, \
                patch('decision.llm_backend.LLMBackendManager') as manager:
            with self.assertRaises(ClarificationNeeded):
                run_block('intake-incomplete', 'contention-boundary', ['three-agent'],
                          0, 'mock:agent', 1, 0, directory)
            manager.assert_not_called()

    def test_v2_best_target_uses_cost_and_allows_finishing_after_deadline(self):
        from experiments.agent_metrics import concession_summary, quality_qualified_success
        episode = {'schemaVersion': 'agent-episode/1.1.0',
            'intents': list(SCENARIOS['two-ue'].intents[:2]),
            'T': {'t0': {'targetId': 'T0', 'requirements': {'I1.r1': 3, 'I2.r1': 1.5}},
                  'alternatives': [
                      {'targetId': 'T1', 'requirements': {'I1.r1': 2, 'I2.r1': 1.5}, 'cost': 12},
                      {'targetId': 'T2', 'requirements': {'I1.r1': 3, 'I2.r1': 1}, 'cost': 8}]},
            'timing': {'t0': 0}, 'budget': {'deadlineBMs': 1000},
            'trials': [{'trialIndex': 1, 'window': {'valid': True, 'end': 2000},
                        'success': {'T1': True, 'T2': True}}]}
        best = concession_summary([episode])['bestAttained']['records'][0]
        self.assertEqual('T2', best['targetId'])
        self.assertEqual(8, best['cost'])
        episode['budget']['deadlineBMs'] = None
        self.assertTrue(quality_qualified_success([episode], [1]))

    def test_models_accept_a_shared_name_or_per_role_assignments(self):
        self.assertEqual({'mock:deterministic'}, set(parse_models().values()))
        assigned = parse_models('target=mock:a,control=mock:b,trajectory=mock:c,monolith=mock:d')
        self.assertEqual('mock:d', assigned['monolith'])
        with self.assertRaises(ValueError):
            parse_models('typo=mock:a')

    def test_live_requires_a_profile_before_composing(self):
        with patch('tools.campaign5.run_agent_experiments.run_matrix') as run:
            with self.assertRaises(SystemExit) as raised:
                main(['--live', '--out', '/unused'])
            self.assertEqual(2, raised.exception.code)
            run.assert_not_called()

    def test_live_profile_passes_to_existing_sitting_without_emulator(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / 'source.json'
            evidence.write_text(json.dumps({'episodeId': 'live', 'method': 'deterministic'}))
            with patch('experiments.agent_episodes.build_agent_sitting') as build, \
                    patch('experiments.agent_episodes.build_hardware_free_agent_sitting') as hf, \
                    patch('experiments.agent_episodes.write_agent_evidence',
                          return_value={'episode': str(evidence), 'events': 'events.jsonl'}):
                run_block('three-ue', 'contention-boundary', ['deterministic'], 0,
                          'mock:deterministic', 2, 0, directory, live_profile='live.json')
                self.assertEqual('live.json', build.call_args.args[0])
                hf.assert_not_called()
                build.return_value.confirm.assert_called_once()
                build.return_value.run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
