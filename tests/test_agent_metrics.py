"""Hand-computed tests over contract 2.5 records; no external services."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from experiments import agent_metrics as m

FIXTURES = Path(__file__).parent / 'fixtures' / 'agent_episodes'


def episode():
    return copy.deepcopy(m.load_episodes(FIXTURES / 'contention-boundary-three-agent.json')[0])


class AgentMetricsTests(unittest.TestCase):
    def test_counted_false_events_do_not_consume_recovery_indices(self):
        e = episode()
        e['schemaVersion'] = 'agent-episode/1.3.0'
        e['trials'][0]['counted'] = False
        e['trials'][1]['counted'] = True
        e['budget']['trialsK'] = 1
        self.assertEqual(1, m.first_success_cost([e])['records'][0]['trialIndex'])
        self.assertEqual(1, m.trial_count(e))
        e['trials'][1]['counted'] = False
        self.assertFalse(m.valid_successes(e))
        e['trials'][1]['trialIndex'] = 0
        self.assertEqual(0, m.first_success_cost([e])['records'][0]['trialIndex'])
        self.assertEqual(0, m.first_success_cost([e])['records'][0]['elapsedMs'])
        self.assertEqual(0, m.trial_count(e))

    def test_boundary_invalidates_prior_success_without_resetting_counts(self):
        e = episode()
        e['trials'][1]['beforeBoundary'] = e['timing']['end']
        self.assertFalse(m.valid_successes(e))
        self.assertEqual(2, m.trial_count(e))
        del e['trials'][1]['beforeBoundary']
        e['boundaries'] = [{'at': e['timing']['end'], 'kind': 'policy', 'detail': 'new policy'}]
        self.assertFalse(m.valid_successes(e))
        e['boundaries'][0]['kind'] = 'fading'
        self.assertTrue(m.valid_successes(e))

    def test_unstamped_declared_boundary_does_not_crash_aggregation(self):
        e = episode()
        e['boundaries'] = [{'at': '', 'kind': 'exogenous', 'detail': 'live trigger'}]
        self.assertTrue(m.valid_successes(e))
        self.assertEqual(1, m.summarize([e])['groups'][0]['resolution']['n'])

    def test_exclusions_reported_beside_rates_and_all_excluded_is_na(self):
        good, excluded = episode(), episode()
        excluded['excluded'] = {'rule': 'logging-failure', 'reason': 'external logger', 'at': 1}
        rows = [good, excluded]
        for rate in (m.resolution_rate(rows), m.original_target_attainment(rows)):
            self.assertEqual(1, rate['N'])
            self.assertEqual(1, rate['excludedCount'])
        curve = m.quality_qualified_success(rows, [1])[0]
        self.assertEqual(1, curve['N'])
        self.assertEqual(1, curve['trialIndex'][-1]['excludedCount'])
        self.assertEqual(1, m.concession_summary(rows)['bestAttained']['N'])
        self.assertEqual(1, len(m.first_success_cost(rows)['records']))
        self.assertEqual(2, len(m.resource_cost(rows)['records']))
        self.assertEqual(1, m.resource_cost(rows)['excludedCount'])
        summary = m.summarize([excluded])
        self.assertEqual(0, summary['groups'][0]['N'])
        self.assertIsNone(summary['overall'][0]['resolutionRate'])
        self.assertIsNone(summary['overall'][0]['qualityQualified'][0]['unresolvedFraction'])
        self.assertEqual(1, summary['excludedCount'])

    def test_ablation_grouping_and_bootstrap_attempts(self):
        full, trajectory = episode(), episode()
        full['condition']['ablation'] = 'full-construction'
        trajectory['condition']['ablation'] = 'trajectory-only'
        self.assertEqual(2, len(m.summarize([full, trajectory])['overall']))
        with self.assertRaises(ValueError):
            m.paired_block_bootstrap([full, trajectory], 'resolution', 'a', 'b')
        rows = []
        for attempt in (0, 1):
            for method in ('a', 'b'):
                e = episode()
                e.update(method=method, attempt=attempt)
                if attempt == 0 and method == 'a':
                    e['excluded'] = {'rule': 'logging-failure'}
                rows.append(e)
        result = m.paired_block_bootstrap(rows, 'resolution', 'a', 'b', n=10)
        self.assertEqual(1, result['matchedPairs'])
        self.assertEqual(1, result['unmatchedPairs'])
        self.assertEqual(1, result['excludedCount'])

    def test_role_figure_requires_explicit_labels_and_accepts_all_excluded(self):
        from unittest.mock import patch
        from experiments import agent_figures as figures
        ordinary, full, trajectory = episode(), episode(), episode()
        full['condition']['ablation'] = 'full-construction'
        trajectory['condition']['ablation'] = 'trajectory-only'
        trajectory['excluded'] = {'rule': 'logging-failure'}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(figures, 'figure_role_ablation', wraps=figures.figure_role_ablation) as ablation:
            figures.render_all([ordinary, full, trajectory], directory)
        full_summary, trajectory_summary = ablation.call_args.args[:2]
        self.assertEqual(1, sum(g['N'] for g in full_summary['groups']))
        self.assertEqual(0, sum(g['N'] for g in trajectory_summary['groups']))
        self.assertEqual(1, trajectory_summary['excludedCount'])

    def test_load_v13_with_every_earlier_version(self):
        """The writer's version and this reader's allow-list must move together.

        ``load_episodes`` is a CLOSED allow-list: a version it does not name is
        dropped with no error and no warning, so a writer-side bump that lands
        without this list reports N = 0 instead of failing. This is the
        regression guard for that pair. It also pins that 1.2.0 stays readable,
        which every record currently on disk declares.
        """
        with tempfile.TemporaryDirectory() as directory:
            for version in ('1.0.0', '1.1.0', '1.2.0', '1.3.0'):
                e = episode()
                e['schemaVersion'] = 'agent-episode/' + version
                (Path(directory) / (version + '.json')).write_text(json.dumps(e))
            self.assertEqual(4, len(m.load_episodes(directory)))
            # How the allow-list fails: an unlisted version is not an error, it
            # is an absence. N shrinks and nothing says why.
            unlisted = episode()
            unlisted['schemaVersion'] = 'agent-episode/9.9.9'
            (Path(directory) / 'unlisted.json').write_text(json.dumps(unlisted))
            self.assertEqual(4, len(m.load_episodes(directory)))

    def test_concession_owner_means_and_unequal_requirement_counts(self):
        e = episode()
        extra = copy.deepcopy(e['intents'][0])
        extra['requirement']['reqId'] = 'r3'
        e['intents'].append(extra)
        target = {'requirements': {'r1': 2, 'r2': 1, 'r3': 3}}
        q = m.concession_of(e, target)
        self.assertEqual(q['perRequirement'], {'r1': .5, 'r2': 1, 'r3': 0})
        self.assertEqual(q['perOwner'], {'video': .25, 'maps': 1})
        self.assertEqual(q['mean'], .625)
        self.assertEqual(q['max'], 1)
        self.assertEqual(q['thresholds']['r1']['value'], 2)

    def test_floor_ceiling_strengthening_and_invalid(self):
        self.assertEqual(m.q_r(10, 20, 15, '<='), .5)
        self.assertEqual(m.q_r(10, 20, 5, '<='), 0)
        self.assertEqual(m.q_r(3, 1, 4), 0)
        self.assertEqual(m.q_r('cell1', None, 'cell1', '=='), 0)
        for args in ((3, 1, .5), (3, None, 2), (3, 3, 2), ('a', None, 'b', '==')):
            with self.assertRaises(ValueError):
                m.q_r(*args)

    def test_wilson_known_half_of_ten(self):
        rate = m._rate(5, 10)
        self.assertAlmostEqual(rate['wilson95'][0], .2365930905)
        self.assertAlmostEqual(rate['wilson95'][1], .7634069095)
        self.assertIsNone(m.resolution_rate([])['rate'])

    def test_invalid_and_late_windows_and_trial_budget(self):
        for change in ('valid', 'deadline', 'budget'):
            e = episode()
            if change == 'valid':
                e['trials'][1]['window']['valid'] = False
            elif change == 'deadline':
                e['budget']['deadlineBMs'] = 9999
            else:
                e['budget']['trialsK'] = 1
            self.assertEqual(m.resolution_rate([e])['n'], 0)

    def test_no_combining_trials_or_unapproved_success(self):
        e = episode()
        e['trials'][0]['verdicts']['T1'] = {'r1': 'PASS', 'r2': 'FAIL'}
        e['trials'][1]['verdicts']['T1'] = {'r1': 'FAIL', 'r2': 'PASS'}
        e['trials'][1]['success']['unapproved'] = True
        self.assertEqual(m.resolution_rate([e])['n'], 0)

    def test_curves_keep_failure_denominator(self):
        good, bad = episode(), episode()
        bad['trials'] = []
        curves = m.quality_qualified_success([good, bad], [0, .5, 1])
        self.assertEqual(curves[0]['trialIndex'][-1]['rate'], 0)
        self.assertEqual(curves[1]['trialIndex'][-1]['rate'], .5)
        self.assertEqual(curves[1]['elapsedMs'][-1]['rate'], .5)
        self.assertEqual(curves[1]['unresolvedFraction'], .5)
        self.assertEqual(curves[1]['trialIndex'][0]['rate'], 0)

    def test_initial_success_is_trial_and_time_zero(self):
        e = episode()
        e['trials'][1]['trialIndex'] = 0
        e['trials'][1]['window']['end'] = e['timing']['t0']
        self.assertEqual(m.first_success_cost([e])['records'][0]['elapsedMs'], 0)
        self.assertEqual(m.quality_qualified_success([e], [1])[0]['trialIndex'][0]['rate'], 1)

    def test_best_recomputed_retained_unqualified_and_unresolved_na(self):
        e = episode()
        e['bestAttained']['concession']['max'] = 0
        e['retained']['qualified'] = False
        bad = episode()
        bad['trials'] = []
        result = m.concession_summary([e, bad])
        self.assertEqual(result['bestAttained']['n'], 1)
        self.assertEqual(result['bestAttained']['D_max']['mean'], .5)
        self.assertTrue(result['bestAttained']['records'][0]['storedMismatch'])
        self.assertIsNone(result['bestAttained']['records'][1]['concession'])
        self.assertIsNone(result['retained']['D_max']['mean'])

    def test_original_attainment_by_case_type(self):
        result = m.original_target_attainment(m.load_episodes(FIXTURES))
        self.assertEqual(result['n'], 2)
        self.assertEqual(result['byCaseType']['control-only']['rate'], 1)
        self.assertEqual(result['byCaseType']['joint-concession']['rate'], 0)

    def test_deficit_missing_bin_and_original_reference(self):
        e = episode()
        e['budget']['horizonHMs'] = 15000
        e['serviceTrace'] = e['serviceTrace'][:5] + e['serviceTrace'][10:15]
        for point in e['serviceTrace']:
            point['kpis']['dlGoodputMbps@131'] = 2
        d = m.service_deficit(e, 5000)['r1']
        # v3.1 section 6: this test used to expect cumulativeDeficit == 10 beside
        # incomplete=True. That is the zero-filled gap the amendment forbids: a
        # horizon with a missing 5 s is unavailable, and 10 is only its lower bound.
        self.assertIsNone(d['cumulativeDeficit'])
        self.assertIsNone(d['fullHorizonShortage'])
        self.assertEqual(d['shortageLowerBound'], 10)  # (3-2) Mbps * 10 seconds
        self.assertEqual(d['shortageUpperBound'], 25)  # + original 3 Mbps * missing 5 s
        self.assertEqual(d['missingIntervals'], [{'startMs': 5000, 'endMs': 10000}])
        self.assertEqual(d['violationDurationSeconds'], 10)
        self.assertAlmostEqual(d['coverage'], 2/3)
        self.assertTrue(d['incomplete'])
        self.assertIsNone(d['bins'][1]['shortage'])
        self.assertEqual(d['deficitUnit'], 'Mbit')  # Mbps over seconds, not 'Mbps*s'
        e['serviceTrace'] = episode()['serviceTrace'][:15]
        for point in e['serviceTrace']:
            point['kpis']['dlGoodputMbps@131'] = 2
        d = m.service_deficit(e, 5000)['r1']
        self.assertEqual(d['cumulativeDeficit'], 15)
        self.assertEqual(d['shortageUpperBound'], 15)
        self.assertEqual(d['missingIntervals'], [])
        self.assertFalse(d['incomplete'])

    def test_deficit_ceiling_and_partial_coverage_configuration(self):
        e = episode()
        req = e['intents'][0]['requirement']
        req.update(op='<=', value=1)
        e['budget']['horizonHMs'] = 5000
        e['serviceTrace'] = e['serviceTrace'][:2]
        for p in e['serviceTrace']:
            p['kpis']['dlGoodputMbps@131'] = 3
        e['measurementRules'] = {'sampleIntervalMs': 1000, 'minValidCoverage': .4, 'statistic': 'mean'}
        d = m.service_deficit(e)['r1']
        # v3.1 section 6: minValidCoverage still lets the bin's mean decide its
        # verdict, but it used to also make 2 s of samples stand for 5 s of shortage
        # (10) and call the horizon complete. The 3 missing seconds are now marked,
        # the full horizon is unavailable, and the 2 measured seconds give 2*2 = 4.
        self.assertTrue(d['bins'][0]['valid'])
        self.assertIsNone(d['cumulativeDeficit'])
        self.assertEqual(d['shortageLowerBound'], 4)
        self.assertIsNone(d['shortageUpperBound'])  # a ceiling has no finite worst case
        self.assertEqual(d['missingIntervals'], [{'startMs': 2000, 'endMs': 5000}])
        self.assertEqual(d['coverage'], .4)
        self.assertTrue(d['incomplete'])

    def test_shortage_integrates_the_one_second_stream_not_the_bin_mean(self):
        e = episode()
        e['budget']['horizonHMs'] = 5000
        e['serviceTrace'] = e['serviceTrace'][:5]
        for point, value in zip(e['serviceTrace'], (0, 6, 0, 6, 3)):
            point['kpis']['dlGoodputMbps@131'] = value
        d = m.service_deficit(e, 5000)['r1']
        self.assertEqual(d['bins'][0]['shortage'], 0)  # the 5 s mean meets 3 Mbps
        self.assertEqual(d['cumulativeDeficit'], 6)  # but two 1 s intervals were 3 short
        self.assertEqual(d['shortageResolutionMs'], 1000)

    def test_horizon_without_budget_is_unavailable_not_zero(self):
        e = episode()
        del e['budget']['horizonHMs']
        d = m.service_deficit(e)['r1']
        for field in ('cumulativeDeficit', 'shortageLowerBound', 'shortageUpperBound', 'horizonMs'):
            self.assertIsNone(d[field])
        self.assertTrue(d['incomplete'])

    def test_failure_cost_and_resource_provenance(self):
        e = episode()
        e['trials'] = []
        row = m.first_success_cost([e])['records'][0]
        self.assertIsNone(row['elapsedMs'])
        self.assertEqual(row['followUpMs'], 15000)
        self.assertEqual(row['termination']['reason'], 'BUDGET_EXHAUSTED')
        del e['resourceCost']
        result = m.resource_cost([e])
        self.assertEqual(result['prepMs']['mean'], 1000)
        self.assertEqual(result['decisionLatenciesMs']['values'], [100])
        self.assertEqual(result['records'][0]['calls'], e['calls'])

    def test_bootstrap_seed_pairing_and_equal_condition_weight(self):
        episodes = m.load_episodes(FIXTURES)
        a = m.paired_block_bootstrap(episodes, 'resolution', 'three-agent', 'basic-monolith', n=100, seed=7)
        self.assertEqual(a, m.paired_block_bootstrap(episodes, 'resolution', 'three-agent', 'basic-monolith', n=100, seed=7))
        self.assertAlmostEqual(a['difference'], 1/3)
        self.assertEqual(a['matchedPairs'], 3)
        self.assertEqual(a['ci95'], [1/3, 1/3])

    def test_summary_equal_conditions_and_separate_timing(self):
        e, bad = episode(), episode()
        bad['condition']['name'] = 'difficult'
        bad['trials'] = []
        summary = m.summarize([e, e, e, bad])
        self.assertEqual(summary['overall'][0]['resolutionRate'], .5)
        self.assertEqual(summary['overall'][0]['qualityQualified'][-1]['trialIndex'][-1]['rate'], .5)
        self.assertEqual(summary['overall'][0]['concession']['bestAttained']['D_max']['conditionsWithData'], 1)
        cold = copy.deepcopy(e)
        cold['timing']['timingMode'] = 'cold-start'
        self.assertEqual(len(m.summarize([e, cold])['overall']), 2)
        with self.assertRaises(ValueError):
            m.paired_block_bootstrap([e, cold], 'resolution', 'a', 'b')

    def test_bootstrap_resamples_blocks_with_nonzero_uncertainty(self):
        rows = []
        for block, scores in enumerate(((1, 0), (0, 1), (1, 0))):
            for method, score in zip(('a', 'b'), scores):
                e = episode()
                e.update(method=method, block=block, score=score)
                rows.append(e)
        result = m.paired_block_bootstrap(rows, lambda e: e['score'], 'a', 'b', n=500, seed=17)
        self.assertAlmostEqual(result['difference'], 1/3)
        self.assertLess(result['ci95'][0], result['difference'])
        self.assertGreater(result['ci95'][1], result['difference'])
        self.assertEqual(result, m.paired_block_bootstrap(rows, lambda e: e['score'], 'a', 'b', n=500, seed=17))

    def test_categorical_service_has_duration_without_scalar_deficit(self):
        e = episode()
        e['intents'][0]['requirement'].update(kpi='servingCell', op='==', value='cell1', unit='NCI')
        for p in e['serviceTrace']:
            p['kpis']['servingCell@131'] = 'cell2'
        result = m.service_deficit(e)['r1']
        self.assertIsNone(result['cumulativeDeficit'])
        self.assertEqual(result['violationDurationSeconds'], 20)
        self.assertFalse(result['incomplete'])

    def test_retained_control_needs_its_own_successful_observation(self):
        e = episode()
        e['retained']['controlId'] = 'C0'
        self.assertEqual(m.concession_summary([e])['retained']['n'], 0)

    def test_load_and_cli(self):
        self.assertEqual(len(m.load_episodes(FIXTURES)), 6)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'metrics.json'
            m.main([str(FIXTURES), '--out', str(output)])
            data = json.loads(output.read_text())
            self.assertEqual(len(data['groups']), 6)
            self.assertNotIn('NaN', output.read_text())


#: Two owners, one relaxation step each: Omega is the four authorized vectors
#: T0(1.0,0.4) < T1(1.0,0.2) < T2(0.5,0.4) < T3(0.5,0.2) under P1 = (D_video, D_maps).
OMEGA_AUTHORIZATION = {
    'r1': {'original': 1.0, 'bound': 0.5, 'limit': 0.5, 'op': '>=', 'owner': 'video',
           'kpi': 'dlGoodputMbps', 'scope': 'ue@666', 'steps': 1, 'unit': 'Mbps', 'weight': 1.0},
    'r2': {'original': 0.4, 'bound': 0.2, 'limit': 0.2, 'op': '>=', 'owner': 'maps',
           'kpi': 'dlGoodputMbps', 'scope': 'ue@668', 'steps': 1, 'unit': 'Mbps', 'weight': 1.0}}


def omega_episode(alternatives=({'targetId': 'TA', 'requirements': {'r1': 0.5, 'r2': 0.2}},),
                  kpis={'dlGoodputMbps@666': 0.7, 'dlGoodputMbps@668': 0.45}):
    """One measured window that meets T2 and T3 but neither T0 nor T1."""
    trial = {'trialIndex': 1, 'controlId': 'C1',
             'window': {'start': '2026-09-14T00:00:01Z', 'end': '2026-09-14T00:00:05Z', 'valid': True}}
    if kpis is not None:
        trial['kpis'] = kpis
    return {'schemaVersion': 'agent-episode/1.3.0', 'episodeId': 'E1', 'method': 'three-agent',
            'timing': {'t0': '2026-09-14T00:00:00Z'},
            'budget': {'trialsK': 4, 'deadlineBMs': 60000},
            'T': {'authorization': copy.deepcopy(OMEGA_AUTHORIZATION),
                  'preference': {'rule': 'lexicographic(D1,D2)', 'ownerPriority': ['video', 'maps']},
                  't0': {'targetId': 'T0', 'requirements': {'r1': 1.0, 'r2': 0.4}},
                  'alternatives': [copy.deepcopy(a) for a in alternatives]},
            'trials': [trial]}


class OmegaCoverageTests(unittest.TestCase):
    def test_omega_is_the_pinned_authorization_not_the_selected_targets(self):
        contract = m.omega_targets(omega_episode())
        self.assertEqual([t.requirements for t in contract.ranked()],
                         [{'r1': 1.0, 'r2': 0.4}, {'r1': 1.0, 'r2': 0.2},
                          {'r1': 0.5, 'r2': 0.4}, {'r1': 0.5, 'r2': 0.2}])
        self.assertEqual(4, len(contract.targets))  # the episode carried only 2

    def test_best_attained_target_may_sit_outside_the_prepared_T(self):
        result = m.omega_attainment(omega_episode())
        self.assertEqual(4, result['omegaSize'])
        self.assertEqual(2, result['selectedCount'])
        self.assertEqual({'r1': 0.5, 'r2': 0.4}, result['best']['requirements'])
        self.assertEqual([1.0, 0.0], result['best']['preferenceKey'])
        self.assertEqual({'video': 1.0, 'maps': 0.0}, result['best']['perOwner'])
        self.assertFalse(result['bestInSelectedT'])
        self.assertIsNone(result['best']['selectedAs'])
        # the same vector scored against the arm that did carry it
        carried = m.omega_attainment(omega_episode(
            alternatives=({'targetId': 'TA', 'requirements': {'r1': 0.5, 'r2': 0.4}},)))
        self.assertEqual(result['best']['requirements'], carried['best']['requirements'])
        self.assertTrue(carried['bestInSelectedT'])
        self.assertEqual('TA', carried['best']['selectedAs'])

    def test_lost_coverage_names_the_missed_vectors_and_the_reference_subset(self):
        result = m.lost_target_coverage(omega_episode(), reference_size=3)
        self.assertEqual([1, 2], [t['rank'] for t in result['missed']])
        self.assertEqual(1, result['bestMissedRank'])
        self.assertEqual(1, result['lostRecords'])
        record = result['records'][0]
        self.assertTrue(record['lost'])
        self.assertEqual(1, record['rankGap'])
        self.assertEqual({'r1': 0.5, 'r2': 0.2}, record['bestInT']['requirements'])
        self.assertEqual({'r1': 0.5, 'r2': 0.4}, record['bestInOmega']['requirements'])
        self.assertEqual(record['bestInOmega']['rank'], record['bestInReference']['rank'])
        complete = m.lost_target_coverage(omega_episode(alternatives=(
            {'targetId': 'TA', 'requirements': {'r1': 0.5, 'r2': 0.4}},
            {'targetId': 'TB', 'requirements': {'r1': 0.5, 'r2': 0.2}},
            {'targetId': 'TC', 'requirements': {'r1': 1.0, 'r2': 0.2}})))
        self.assertEqual(0, complete['missedCount'])
        self.assertEqual(0, complete['lostRecords'])
        self.assertIsNone(complete['bestMissedRank'])
        self.assertEqual(0, complete['records'][0]['rankGap'])

    def test_missing_authorization_fails_closed(self):
        for damage in ({}, None, 'T'):
            e = omega_episode()
            if damage == 'T':
                del e['T']
            else:
                e['T']['authorization'] = damage
            for call in (m.omega_targets, m.omega_attainment, m.lost_target_coverage):
                with self.assertRaises(ValueError):
                    call(e)

    def test_unmeasured_and_invalid_windows_are_not_attainments(self):
        unmeasured = m.omega_attainment(omega_episode(kpis=None))
        self.assertEqual(0, unmeasured['judgedRecords'])
        self.assertIsNone(unmeasured['best'])
        self.assertFalse(unmeasured['records'][0]['judged'])
        self.assertFalse(m.lost_target_coverage(omega_episode(kpis=None))['lostRecords'])
        invalid = omega_episode()
        invalid['trials'][0]['window']['valid'] = False
        self.assertEqual([], m.omega_attainment(invalid)['records'])
        self.assertEqual(4, m.omega_attainment(invalid)['omegaSize'])

    def test_selected_target_outside_omega_is_reported_not_dropped(self):
        result = m.lost_target_coverage(omega_episode(alternatives=(
            {'targetId': 'TB', 'requirements': {'r1': 0.3, 'r2': 0.2}},)))
        self.assertEqual(['TB'], result['unmatchedSelected'])
        self.assertEqual(3, result['missedCount'])


class OmegaSummaryWiringTests(unittest.TestCase):
    """Omega attainment and lost coverage reach the reported per-method figures."""

    #: sha256 of summarize(FIXTURES) with the 'omega', 'serviceDeficit' and
    #: 'trialExposure' keys removed. The first digest ('ce34e6e2...') also kept
    #: serviceDeficit; v3.1 section 6 deliberately redefined that (Mbit, bounds,
    #: unavailable horizon), so it is excluded here and the digest was retaken
    #: from the pre-v3.1 module with only omega and serviceDeficit removed. If any
    #: other pre-existing statistic moves, this fails.
    #: 2026-09-23 재촬영 (4df2916e -> 7827f69e).  **이 감시는 침묵시키지 않았다** --
    #: 움직인 것을 먼저 찾았다: `concession_of` 가 소유자 평균에서 **보호된 차원을
    #: 빼도록** 바뀌었고(`if extendable:` -- 인가된 축만 센다), 그 값이 요약의
    #: `concession.bestAttained.D_max` / `D_mean` / `D_o` 로 흘러든다.
    #: 그것이 규칙인 이유: 고정 차원을 0 으로 세면 고정이 많은 소유자일수록 양보가
    #: 작아 보여 P1 순서가 뒤집힌다 (2026-09-23 시나리오 결정 §3.1, 같은 규칙이
    #: `assurance/coordination/preference.py` 에도 있다).  이 값 말고 다른 통계가
    #: 움직이면 이 해시는 여전히 빨개진다.
    PRE_WIRING_DIGEST = '7827f69e8ae943aeb5c8e23cd4500982bcf8695f3bf463a717b25eaf7651473c'

    def test_wiring_is_additive_to_every_pre_existing_statistic(self):
        summary = m.summarize(m.load_episodes(FIXTURES))
        for row in summary['groups'] + summary['overall']:
            self.assertIn('omega', row)  # it reaches every method, not just one
            del row['omega']
            row.pop('serviceDeficit', None)
            row.pop('trialExposure', None)
            row.pop('hardwareDisconnects', None)  # additive: the identity-continuity footnote
            # additive (2026-09-21): 판 기록의 `t0Success` 를 이 파일이 한 번도 읽지
            # 않아, 초기 측정이 최선이었던 판(실측 50%)이 어디에도 드러나지 않았다.
            # 세어서 내보내기만 하고 기존 통계는 건드리지 않는다 -- 아래 해시가 그것을 증명한다.
            row.pop('walkover', None)
            # additive (2026-09-23, codex 감사 #8): 두 셈 정책이 코호트 키에 들어갔다.
            # 같은 condition·method 라도 기준 시행을 `N_max` 에 세는 판과 안 세는 판은
            # 다른 실험이라 합산할 수 없다.  줄을 **가르기만** 하고 그 안의 통계는
            # 건드리지 않는다 -- 아래 해시가 그것을 증명한다.
            row.pop('formalReferenceTrial', None)
            row.pop('retainOnImprovement', None)
            row.pop('campaign', None)   # additive: 같은 코퍼스의 두 캠페인을 가른다
            row.pop('quality', None)    # additive: 결정 §5.3 지표 (experiments/agent_quality.py)
            # **재정의** (2026-09-23 결정 §4), serviceDeficit 과 같은 종류의 변경이다:
            # 표를 요청값(`options`)이 아니라 **전선에 실린 것**(`sentOptions`)으로 묶고
            # 안 실린 설정(`notSent`)을 따로 낸다.  침묵시키는 것이 아니라 옛 모양으로
            # 되돌려서 비교한다 -- 이 정규화로 해시가 예전 값과 **정확히 일치**하므로,
            # 움직인 것이 이 재정의뿐이고 다른 통계는 한 바이트도 안 움직였다는 증명이 된다.
            if 'generationOptions' in row:
                row['generationOptions'] = [
                    {'model': g['model'], 'role': g['role'], 'options': g['requestedOptions'],
                     'calls': g['calls'], 'latencyMs': g['latencyMs'],
                     'staleDecisions': g['staleDecisions']}
                    for g in row['generationOptions']]
        blob = json.dumps(summary, sort_keys=True, separators=(',', ':')).encode()
        self.assertEqual(self.PRE_WIRING_DIGEST, hashlib.sha256(blob).hexdigest())

    def test_every_method_reports_best_in_omega_and_what_T_discarded(self):
        e = omega_episode()
        block = next(iter(m.summarize([e])['overall'][0]['omega']['byDigest'].values()))
        self.assertEqual(4, block['omegaSize'])
        self.assertEqual({'r1': 0.5, 'r2': 0.4}, block['bestAttained']['requirements'])
        # the attained vector was never carried into this method's prepared T
        self.assertFalse(block['bestAttainedInSelectedT'])
        self.assertEqual(1, block['attainedOutsideSelectedT'])
        # what selection discarded, and how the no-LLM reference did on the same records
        self.assertEqual(1, block['lostRecords'])
        self.assertEqual(2, block['missedCount']['mean'])
        self.assertEqual(1, block['bestMissedRank']['mean'])
        self.assertEqual(1, block['rankGapVsOmega']['mean'])
        self.assertEqual(0, block['referenceGapVsOmega']['mean'])

    def test_an_unusable_authorization_is_named_and_counted_never_scored(self):
        good, broken = omega_episode(), omega_episode()
        broken['episodeId'] = 'NOAUTH'
        del broken['T']['authorization']
        group = m.summarize([good, broken])['groups'][0]
        self.assertEqual(2, group['N'])  # the pre-existing denominator is untouched
        self.assertEqual(1, group['omega']['omegaExcludedCount'])
        excluded = group['omega']['omegaExclusions'][0]
        self.assertEqual('NOAUTH', excluded['episodeId'])
        self.assertIn('no pinned authorization', excluded['reason'])
        block = next(iter(group['omega']['byDigest'].values()))
        self.assertEqual(1, block['N'])  # not a zero and not a pass: simply absent
        self.assertEqual(1, block['omegaExcludedCount'])  # repeated beside the figures
        self.assertNotIn('NOAUTH', [r['episodeId'] for r in block['episodes']])

    def test_digests_are_reported_separately_and_never_pooled(self):
        a, b = omega_episode(), omega_episode()
        b['episodeId'] = 'B'
        b['T']['preference']['ownerPriority'] = ['maps', 'video']
        omega = m.summarize([a, b])['overall'][0]['omega']
        self.assertEqual(2, omega['digestCount'])
        self.assertEqual([1, 1], [block['N'] for block in omega['byDigest'].values()])

    def test_one_omega_under_two_schemas_is_two_populations_never_one_row(self):
        """Every figure pooled in a block is a function of the selection, so a
        schema bump -- which is what marks a selection rule change -- splits the
        block even though the pinned authorization is identical."""
        old, new = omega_episode(), omega_episode(alternatives=(
            {'targetId': 'TA', 'requirements': {'r1': 0.5, 'r2': 0.4}},))
        old['episodeId'], old['schemaVersion'] = 'OLD', 'agent-episode/1.2.0'
        new['episodeId'] = 'NEW'
        coverage = m.omega_coverage([old, new])
        self.assertEqual(2, coverage['digestCount'])
        blocks = coverage['byDigest']
        self.assertEqual([1, 1], [b['N'] for b in blocks.values()])
        self.assertEqual({'agent-episode/1.2.0', 'agent-episode/1.3.0'},
                         {b['schemaVersion'] for b in blocks.values()})
        # one Omega: the split is the schema, never a different authorization
        self.assertEqual(1, len({b['omegaDigest'] for b in blocks.values()}))
        self.assertEqual(m.require_shared_omega([old]),
                         next(iter(blocks.values()))['omegaDigest'])
        # and it is the schema doing it: both narrowed Omega to the same size
        self.assertEqual({2}, {b['selectedCount'] for b in blocks.values()})
        # the selection-dependent figure no longer speaks for both
        self.assertEqual({False, True},
                         {b['bestAttainedInSelectedT'] for b in blocks.values()})
        # the key stays a string, so metrics.json still round-trips
        self.assertEqual(coverage, json.loads(json.dumps(coverage)))

    def test_one_omega_under_two_selections_is_two_populations(self):
        """A schema bump cannot separate arms that narrowed the same Omega
        differently under the same schema; the selection size does. Pooled,
        they average two different questions into a figure true of neither."""
        narrow, wide = omega_episode(), omega_episode(alternatives=(
            {'targetId': 'TA', 'requirements': {'r1': 0.5, 'r2': 0.4}},
            {'targetId': 'TB', 'requirements': {'r1': 0.5, 'r2': 0.2}},
            {'targetId': 'TC', 'requirements': {'r1': 1.0, 'r2': 0.2}}))
        narrow['episodeId'], wide['episodeId'] = 'NARROW', 'WIDE'
        coverage = m.omega_coverage([narrow, wide])
        blocks = coverage['byDigest']
        self.assertEqual(2, coverage['digestCount'])
        self.assertEqual([1, 1], [b['N'] for b in blocks.values()])
        self.assertEqual({2, 4}, {b['selectedCount'] for b in blocks.values()})
        # one Omega and one schema: the selection is the only difference
        self.assertEqual(1, len({b['omegaDigest'] for b in blocks.values()}))
        self.assertEqual(1, len({b['schemaVersion'] for b in blocks.values()}))
        # the figure pooling would have flattened stays separated
        self.assertEqual({0, 2}, {b['missedCount']['mean'] for b in blocks.values()})
        self.assertEqual(coverage, json.loads(json.dumps(coverage)))

    def test_reported_size_is_read_from_the_record_never_assumed(self):
        """Joint conditions will carve Omega; nothing here hardcodes a cardinality,
        so the reported figures follow whatever the record actually holds."""
        e = omega_episode()
        for joint in ([], [{'kpi': 'dlGoodputMbps@668', 'op': '>=', 'value': 0.4}]):
            e['T']['jointConditions'] = joint
            block = next(iter(m.omega_coverage([e])['byDigest'].values()))
            self.assertEqual(len(m.omega_targets(e).ranked()), block['omegaSize'])
        self.assertEqual(1, m.omega_coverage([e], reference_size=1)['referenceSize'])


class OmegaBlockTests(unittest.TestCase):
    """A block of episodes may only be compared when one authorization pins them all."""

    def block(self, mutate=None):
        a, b = omega_episode(), omega_episode()
        a['episodeId'], b['episodeId'] = 'A', 'B'
        b['method'] = 'basic-monolith'
        if mutate:
            mutate(b)
        return a, b

    def test_identical_block_shares_one_digest_whatever_the_key_order(self):
        def reorder(episode):
            rows = episode['T']['authorization']
            episode['T']['authorization'] = {key: dict(reversed(list(rows[key].items())))
                                             for key in reversed(list(rows))}
        a, b = self.block(reorder)
        digest = m.require_shared_omega([a, b])
        self.assertEqual(64, len(digest))
        self.assertEqual(digest, m.require_shared_omega([b, a]))
        self.assertEqual(digest, m.require_shared_omega([a]))

    def refusal(self, mutate):
        a, b = self.block(mutate)
        with self.assertRaises(ValueError) as raised:
            m.require_shared_omega([a, b])
        self.assertIn("'B'", str(raised.exception))
        return str(raised.exception)

    def test_differing_joint_conditions_are_refused(self):
        def carve(episode):
            episode['T']['jointConditions'] = [
                {'kpi': 'dlGoodputMbps@668', 'op': '>=', 'value': 0.3}]
        self.assertIn('jointConditions', self.refusal(carve))

    def test_differing_levels_are_refused(self):
        def widen(episode):
            episode['T']['authorization']['r2'].update(bound=0.3, limit=0.3)
        message = self.refusal(widen)
        self.assertIn('requirements.r2.levels', message)

    def test_differing_owner_priority_is_refused_because_it_reorders_omega(self):
        def reprioritize(episode):
            episode['T']['preference']['ownerPriority'] = ['maps', 'video']
        self.assertIn('preference.ownerPriority', self.refusal(reprioritize))
        a, b = self.block(reprioritize)
        self.assertNotEqual([t.requirements for t in m.omega_targets(a).ranked()],
                            [t.requirements for t in m.omega_targets(b).ranked()])

    def test_measurement_binding_and_prose_labels_do_not_split_a_block(self):
        """The RNTI moves on every reattachment and the cost label is rendered
        differently run to run; neither changes which vectors Omega holds."""
        def rebind(episode):
            for entry in episode['T']['authorization'].values():
                entry['scope'] = 'ue@999'
            episode['T']['preference'].update(
                costRule='sum_w_q2 = 1.0*q1^2 + 1.0*q2^2',
                tieBreak='lexicographic(D_max, D_mean) then intent order, owner priority video, maps')
        a, b = self.block(rebind)
        self.assertEqual(m.require_shared_omega([a]), m.require_shared_omega([a, b]))
        self.assertEqual([t.requirements for t in m.omega_targets(a).ranked()],
                         [t.requirements for t in m.omega_targets(b).ranked()])

    def test_mixed_omega_block_refuses_instead_of_reporting_a_number(self):
        rows = []
        for method in ('three-agent', 'basic-monolith'):
            e = episode()
            e.update(method=method, episodeId=method)
            rows.append(e)
        self.assertIsNotNone(m.paired_block_bootstrap(
            rows, 'resolution', 'three-agent', 'basic-monolith', n=10)['difference'])
        rows[1]['T']['preference']['ownerPriority'] = ['maps', 'video']
        with self.assertRaises(ValueError) as raised:
            m.paired_block_bootstrap(rows, 'resolution', 'three-agent', 'basic-monolith', n=10)
        self.assertIn("'basic-monolith'", str(raised.exception))
        self.assertIn('preference.ownerPriority', str(raised.exception))

    def test_shared_omega_wiring_leaves_a_valid_comparison_unchanged(self):
        """The pre-wiring statistic, pinned: a consistent block still reports it."""
        episodes = m.load_episodes(FIXTURES)
        self.assertEqual(1, len({m.require_shared_omega([e]) for e in episodes}))
        result = m.paired_block_bootstrap(episodes, 'resolution', 'three-agent',
                                          'basic-monolith', n=100, seed=7)
        self.assertAlmostEqual(result['difference'], 1/3)
        self.assertEqual(result['matchedPairs'], 3)
        self.assertEqual(result['ci95'], [1/3, 1/3])

    def test_empty_block_and_unusable_authorization_fail_closed(self):
        with self.assertRaises(ValueError):
            m.require_shared_omega([])
        a, b = self.block(lambda e: e['T'].pop('authorization'))
        for rows in ([a, b], [b, a]):
            with self.assertRaises(ValueError):
                m.require_shared_omega(rows)


#: Omega's four vectors are (r1, r2) at levels (0,0), (0,1), (1,0), (1,1), which
#: rank T0(1.0,0.4) < T1(1.0,0.2) < T2(0.5,0.4) < T3(0.5,0.2). The constraints
#: below each carve a different one out, so the sizes are derived, not assumed:
#: the quota forbids both axes relaxing at once, dropping (1,1) -> 4-1 = 3; the
#: mode set permits (0,0),(1,0),(1,1), dropping (0,1) -> 3; together they leave
#: (0,0) and (1,0) -> 2.
BOTH_RELAX_QUOTA = {'axes': ['r1', 'r2'], 'atMost': 1, 'level': 1}
MODE_SET = {'axes': ['r1', 'r2'], 'allow': [[0, 0], [1, 0], [1, 1]]}
UNCONSTRAINED = [{'r1': 1.0, 'r2': 0.4}, {'r1': 1.0, 'r2': 0.2},
                 {'r1': 0.5, 'r2': 0.4}, {'r1': 0.5, 'r2': 0.2}]


class OmegaModeConstraintTests(unittest.TestCase):
    """A pinned mode constraint shapes the rebuilt Omega, or the episode is refused.

    Target ids are positional, assigned after the ranking sort, so an Omega
    rebuilt one member too large does not merely report a bigger number: every
    id past the dropped vector names a different vector than the episode ran.
    """

    def pinned(self, *constraints):
        e = omega_episode()
        e['T']['modeConstraints'] = [copy.deepcopy(c) for c in constraints]
        return e

    def vectors(self, episode):
        return [t.requirements for t in m.omega_targets(episode).ranked()]

    def test_each_kind_of_constraint_carves_the_rebuilt_domain(self):
        self.assertEqual(UNCONSTRAINED, self.vectors(omega_episode()))
        self.assertEqual([v for v in UNCONSTRAINED if v != {'r1': 0.5, 'r2': 0.2}],
                         self.vectors(self.pinned(BOTH_RELAX_QUOTA)))
        self.assertEqual([v for v in UNCONSTRAINED if v != {'r1': 1.0, 'r2': 0.2}],
                         self.vectors(self.pinned(MODE_SET)))
        together = self.pinned(BOTH_RELAX_QUOTA, MODE_SET)
        self.assertEqual([{'r1': 1.0, 'r2': 0.4}, {'r1': 0.5, 'r2': 0.4}],
                         self.vectors(together))
        self.assertEqual(len(UNCONSTRAINED) - 2, m.omega_attainment(together)['omegaSize'])

    def test_positional_target_ids_follow_the_constrained_domain(self):
        """The whole point: T1 is a different vector once (0,1) is not authorized."""
        ranked = m.omega_targets(self.pinned(MODE_SET)).ranked()
        self.assertEqual(['T0', 'T1', 'T2'], [t.target_id for t in ranked])
        self.assertEqual({'r1': 0.5, 'r2': 0.4}, ranked[1].requirements)
        self.assertEqual(UNCONSTRAINED[2], ranked[1].requirements)  # was T2 unpinned

    def test_a_constrained_episode_is_scored_against_its_own_smaller_omega(self):
        block = next(iter(m.omega_coverage([self.pinned(MODE_SET)])['byDigest'].values()))
        self.assertEqual(3, block['omegaSize'])
        # the measured window meets (0.5,0.4), which is rank 1 here and rank 2 unpinned
        self.assertEqual({'r1': 0.5, 'r2': 0.4}, block['bestAttained']['requirements'])
        self.assertEqual(1, block['bestAttained']['rank'])
        self.assertEqual(2, next(iter(m.omega_coverage([omega_episode()])['byDigest']
                                      .values()))['bestAttained']['rank'])

    def test_a_malformed_constraint_is_refused_never_silently_dropped(self):
        for broken in ('ue2 may not relax both', {'axes': []}, {'axes': ['r1']},
                       {'axes': ['r1'], 'allow': [[0, 0]]}, {'axes': ['nope'], 'atMost': 1}):
            e = self.pinned(broken)
            with self.assertRaises(ValueError):
                m.omega_targets(e)
            # and through the aggregation it is named, never scored on a wider Omega
            coverage = m.omega_coverage([e])
            self.assertEqual(1, coverage['omegaExcludedCount'])
            self.assertEqual({}, coverage['byDigest'])

    def test_an_unreadable_joint_condition_is_refused_never_silently_dropped(self):
        """The identical failure reached through the other parameter, so the two
        posture the same way: a dropped joint condition widens Omega too."""
        carved = omega_episode()
        carved['T']['jointConditions'] = [{'kpi': 'dlGoodputMbps@668', 'op': '>=',
                                           'value': 0.4}]
        # the protected floor refuses r2's relaxed level, leaving 4 - 2 = 2 vectors
        self.assertEqual([{'r1': 1.0, 'r2': 0.4}, {'r1': 0.5, 'r2': 0.4}],
                         self.vectors(carved))
        for broken in ('ue 668 keeps 0.4 Mbps whatever else happens',
                       {'op': '>=', 'value': 0.4},
                       {'kpi': 'dlGoodputMbps@668', 'op': '>='},
                       {'kpi': 'dlGoodputMbps@668', 'op': '!!', 'value': 0.4}):
            e = omega_episode()
            e['T']['jointConditions'] = [copy.deepcopy(broken)]
            with self.assertRaises(ValueError):
                m.omega_targets(e)
            coverage = m.omega_coverage([e])
            self.assertEqual(1, coverage['omegaExcludedCount'])
            self.assertEqual({}, coverage['byDigest'])
            self.assertEqual('E1', coverage['omegaExclusions'][0]['episodeId'])

    def test_mode_constraints_are_part_of_the_shared_omega_digest(self):
        plain, carved = omega_episode(), self.pinned(BOTH_RELAX_QUOTA)
        carved['episodeId'] = 'B'
        with self.assertRaises(ValueError) as raised:
            m.require_shared_omega([plain, carved])
        self.assertIn('modeConstraints', str(raised.exception))
        # a conjunction is unordered, so restating it the other way is one block
        reordered = self.pinned(MODE_SET, BOTH_RELAX_QUOTA)
        reordered['episodeId'] = 'C'
        self.assertEqual(m.require_shared_omega([self.pinned(BOTH_RELAX_QUOTA, MODE_SET)]),
                         m.require_shared_omega([reordered]))

    def test_an_episode_without_mode_constraints_is_unchanged(self):
        absent, empty = omega_episode(), self.pinned()
        self.assertEqual(UNCONSTRAINED, self.vectors(absent))
        self.assertEqual(m.require_shared_omega([absent]), m.require_shared_omega([empty]))
        self.assertEqual(json.dumps(m.omega_coverage([absent]), sort_keys=True),
                         json.dumps(m.omega_coverage([empty]), sort_keys=True))


class OmegaQualifiedSuccessTests(unittest.TestCase):
    """v3.1 section 5: original and Dmax-qualified success against all of Omega."""

    def test_qualified_success_uses_every_attained_omega_target_not_only_T(self):
        e = omega_episode(kpis={'dlGoodputMbps@666': 0.8, 'dlGoodputMbps@668': 0.35})
        for rid in ('r1', 'r2'):
            e['T']['authorization'][rid]['steps'] = 2  # r1 1.0/.75/.5, r2 .4/.3/.2
        result = m.omega_attainment(e)
        # T holds only T0 and TA (Dmax 1); Omega's (.75, .3) has Dmax .5.
        self.assertEqual([(0, False), (0.5, True), (1, True)],
                         [(q['a'], q['success']) for q in result['qualifiedSuccess']])
        self.assertFalse(result['originalSuccess'])
        self.assertEqual({'r1': 0.75, 'r2': 0.3}, result['best']['requirements'])
        block = next(iter(m.omega_coverage([e])['byDigest'].values()))
        self.assertEqual(0, block['originalSuccess']['n'])
        self.assertEqual([0, 1, 1], [q['n'] for q in block['qualifiedSuccess']])

    def test_original_success_is_the_all_zero_vector_of_omega(self):
        met = m.omega_attainment(omega_episode(kpis={'dlGoodputMbps@666': 1.0,
                                                     'dlGoodputMbps@668': 0.4}))
        self.assertTrue(met['originalSuccess'])
        self.assertTrue(all(q['success'] for q in met['qualifiedSuccess']))
        unmeasured = m.omega_attainment(omega_episode(kpis=None))
        self.assertFalse(unmeasured['originalSuccess'])
        self.assertFalse(any(q['success'] for q in unmeasured['qualifiedSuccess']))


class TrialExposureTests(unittest.TestCase):
    """v3.1 section 6: application start through acceptance or confirmed restoration."""

    @staticmethod
    def exposure_episode(*trials):
        return {'episodeId': 'X', 'timing': {'t0': 0},
                'trials': [{'trialIndex': 0, 'counted': False, 'appliedAt': 0}] + list(trials)}

    def test_legacy_kernel_states_and_lockdown_is_a_lower_bound(self):
        e = self.exposure_episode(
            {'trialIndex': 1, 'appliedAt': 1000, 'elapsedMs': 9000,
             'kernel': {'terminalState': 'SETTLED_SUCCESS'}},
            {'trialIndex': 2, 'appliedAt': 10000, 'elapsedMs': 16000,
             'kernel': {'terminalState': 'SETTLED_NON_SUCCESS'}},
            {'trialIndex': 3, 'appliedAt': 20000, 'elapsedMs': 50000,
             'window': {'end': 30000}, 'kernel': {'terminalState': 'INCIDENT_LOCKDOWN',
                                                  'stopReason': 'PARTIAL_APPLY'}})
        result = m.trial_exposure(e)
        rows = result['trials']
        self.assertEqual([1, 2, 3], [r['trialIndex'] for r in rows])  # trial 0 is not applied
        self.assertEqual((8000, True, False), (rows[0]['exposureMs'], rows[0]['accepted'],
                                               rows[0]['restorationConfirmed']))
        self.assertEqual((6000, True), (rows[1]['exposureMs'], rows[1]['restorationConfirmed']))
        self.assertIsNone(rows[2]['exposureMs'])
        self.assertFalse(rows[2]['restorationConfirmed'])
        self.assertFalse(rows[2]['complete'])
        self.assertEqual(30000, rows[2]['exposureLowerBoundMs'])
        self.assertIsNone(result['exposureMs'])  # never complete with one unresolved trial
        self.assertEqual(8000 + 6000 + 30000, result['exposureLowerBoundMs'])
        self.assertEqual([3], result['recoveryFailures'])
        self.assertFalse(result['completionConfirmed'])
        self.assertIn('not established', result['trialCountShortageBound'])
        self.assertIn('no cumulative-shortage budget gate', result['trialCountShortageBound'])

    def test_new_fields_win_and_a_recovery_timeout_is_not_restoration(self):
        e = self.exposure_episode(
            {'trialIndex': 1, 'appliedAt': 5000, 'application': {'startedAt': 2000},
             'recovery': {'requestedAt': 7000, 'restoredAt': 12000},
             'kernel': {'terminalState': 'SETTLED_NON_SUCCESS'}, 'elapsedMs': 13000},
            {'trialIndex': 2, 'application': {'startedAt': 20000},
             'recovery': {'status': 'TIMEOUT', 'requestedAt': 25000, 'timedOutAt': 40000},
             'kernel': {'terminalState': 'SETTLED_NON_SUCCESS'}},
            {'trialIndex': 3, 'appliedAt': 50000, 'window': {'end': 55000}})
        rows = m.trial_exposure(e)['trials']
        self.assertEqual((10000, 'application.startedAt', 'recovery.restoredAt'),
                         (rows[0]['exposureMs'], rows[0]['startField'], rows[0]['endField']))
        self.assertFalse(rows[1]['restorationConfirmed'])
        self.assertTrue(rows[1]['recoveryFailed'])
        self.assertEqual(20000, rows[1]['exposureLowerBoundMs'])
        self.assertIsNone(rows[2]['exposureMs'])  # no terminal state: unknown completion
        self.assertEqual([3], m.trial_exposure(e)['unknownCompletion'])

    def test_complete_episode_sums_and_reaches_the_summary(self):
        e = self.exposure_episode({'trialIndex': 1, 'appliedAt': 1000,
                                   'completion': {'acceptedAt': 4000}})
        result = m.trial_exposure(e)
        self.assertEqual(3000, result['exposureMs'])
        self.assertTrue(result['completionConfirmed'])
        self.assertIn('holds', result['trialCountShortageBound'])
        group = m.summarize(m.load_episodes(FIXTURES))['groups'][0]
        self.assertIn('trialExposure', group)


if __name__ == '__main__':
    unittest.main()
