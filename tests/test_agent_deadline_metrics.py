"""Hand-computed I4 metrics on serialized, source-tagged counter traces only."""
import json
from pathlib import Path
import tempfile
import unittest

from assurance.coordination.tc import Authorization, Intent
from experiments import agent_metrics as m


RID = 'I4.r1'
KEY = 'deadlineSuccessRatio@131'


def episode():
    intent = Intent.from_record({
        'intentId': 'I4', 'owner': 'interactive', 'ueId': '131',
        'requirement': {'reqId': RID, 'kpi': 'deadlineSuccessRatio',
                        'scope': 'ue@131', 'op': '>=', 'value': .95, 'unit': 'ratio',
                        'steps': 2, 'bound': .85, 'deadlineMs': 50,
                        'deadlineSteps': 2, 'deadlineBound': 80}})
    return {
        'schemaVersion': 'agent-episode/1.3.0', 'episodeId': 'deadline-metrics',
        'method': 'deterministic', 'condition': {'name': 'counter-fixture'},
        'intents': [intent.to_record()],
        'timing': {'t0': 0, 'end': 4000, 'timingMode': 'prepared'},
        'budget': {'horizonHMs': 4000, 'binDeltaMs': 2000,
                   'trialsK': 2, 'deadlineBMs': 4000},
        'measurementRules': {'deadlineSuccessRatio': {
            'statistic': 'ratio', 'sampleIntervalMs': 1000, 'minCoverage': 1.0}},
        'T': {'authorization': Authorization.from_intents([intent]).to_record(),
              't0': {'targetId': 'T0', 'requirements': {RID: .95},
                     'deadlines': {RID: 50}},
              'alternatives': [{'targetId': 'T1', 'requirements': {RID: .95},
                                'deadlines': {RID: 80}}]},
        'trials': [], 'serviceTrace': []}


def counter_trace():
    # Busy/thin intervals have unequal denominators. The baseline's historical
    # losses must not become this horizon's losses; D80 is perfect throughout.
    totals = [(100, 50), (110, 60), (200, 105), (250, 145), (300, 195)]
    return [{'t': index * 1000, 'kpis': {KEY: {
        'byDeadlineMs': {
            '50': {'issued': eligible + 1, 'eligible': eligible, 'completed': completed},
            '80': {'issued': eligible + 1, 'eligible': eligible, 'completed': eligible}},
        'source': {'sessionId': 'session-1', 'flowId': 'ue131-echo', 'clockId': 'mono-1'},
        'observedAtMs': 10000 + index * 1000}}}
        for index, (eligible, completed) in enumerate(totals)]


def successful_trial(kpis):
    return {'trialIndex': 1, 'controlId': 'C0',
            'window': {'start': 0, 'end': 2000, 'valid': True},
            'verdicts': {'T0': {RID: 'PASS'}, 'T1': {RID: 'PASS'}}, 'kpis': kpis}


class DeadlineConcessionTests(unittest.TestCase):
    def test_serialized_authorization_and_independent_concessions(self):
        e = episode()
        authorization = e['T']['authorization']
        self.assertIn(RID, authorization)
        self.assertNotIn('requirements', authorization)
        self.assertEqual(authorization[RID]['deadlineBound'], 80)
        for reliability, deadline, q_r, q_d in (
                (.95, 50, 0, 0), (.90, 50, .5, 0),
                (.95, 80, 0, 1), (.90, 65, .5, .5)):
            with self.subTest(reliability=reliability, deadline=deadline):
                target = {'requirements': {RID: reliability}, 'deadlines': {RID: deadline}}
                result = m.concession_of(e, target)
                self.assertAlmostEqual(result['perRequirement'][RID], q_r)
                self.assertAlmostEqual(result['perRequirement'][RID + '#deadline'], q_d)
                self.assertAlmostEqual(result['perOwner']['interactive'], (q_r + q_d)/2)
                self.assertAlmostEqual(result['mean'], (q_r + q_d)/2)
                self.assertEqual(result['thresholds'][RID + '#deadline'], {
                    'original': 50, 'limit': 80, 'value': deadline, 'unit': 'ms'})

    def test_fixed_reliability_does_not_forbid_authorized_deadline_extension(self):
        e = episode()
        e['intents'][0]['requirement']['relaxable'] = False
        e['T']['authorization'][RID].update(steps=0, bound=.95, limit=.95)
        result = m.concession_of(e, e['T']['alternatives'][0])
        self.assertEqual(result['perRequirement'], {RID: 0, RID + '#deadline': 1})
        # 2026-09-23 결정 §3.1: **보호된 차원은 소유자 평균에 0 을 더하지 않는다.**
        # 여기서 신뢰도는 steps=0 으로 고정돼 있으므로 소유자 평균은 조절 가능한
        # 마감 하나만 본다 -- (0+1)/2 = 0.5 가 아니라 1.0 이다.  0 을 세면 고정 차원이
        # 많은 소유자일수록 양보가 작아 보여 P1 순서가 뒤집힌다.
        self.assertEqual(result['perOwner'], {'interactive': 1.0})
        self.assertEqual(result['max'], 1.0)

    def test_deadline_is_part_of_fixed_owner_requirement_mean(self):
        e = episode()
        target = {'requirements': {RID: .90, 'other': 1, 'second-owner': 3}}
        for rid, owner in (('other', 'interactive'), ('second-owner', 'video')):
            e['intents'].append({'owner': owner, 'requirement': {
                'reqId': rid, 'kpi': 'dlGoodputMbps', 'scope': 'ue@132',
                'op': '>=', 'value': 3, 'bound': 1, 'unit': 'Mbps'}})
        result = m.concession_of(e, target)  # omitted deadlines mean D0, not no charge axis
        self.assertEqual(result['perRequirement'][RID + '#deadline'], 0)
        self.assertAlmostEqual(result['perOwner']['interactive'], (.5 + 0 + 1)/3)
        self.assertEqual(result['perOwner']['video'], 0)
        self.assertAlmostEqual(result['mean'], .25)

    def test_unauthorized_deadline_must_remain_unchanged(self):
        for steps, bound in ((0, 80), (0, None), (None, None), (2, None), (0, 50)):
            e = episode()
            e['T']['authorization'][RID].update(deadlineSteps=steps, deadlineBound=bound)
            self.assertEqual(m.concession_of(e, e['T']['t0'])['max'], 0)
            for changed in (49, 80):
                with self.subTest(steps=steps, bound=bound, changed=changed):
                    target = {'requirements': {RID: .95}, 'deadlines': {RID: changed}}
                    with self.assertRaises(ValueError):
                        m.concession_of(e, target)

    def test_bare_legacy_bound_and_permitted_strengthening(self):
        e = episode()
        e['T']['authorization'][RID]['deadlineSteps'] = None
        self.assertEqual(m.concession_of(e, e['T']['alternatives'][0])['max'], .5)
        target = {'requirements': {RID: .97}, 'deadlines': {RID: 45}}
        self.assertEqual(m.concession_of(e, target)['max'], 0)

    def test_invalid_deadlines_are_not_clipped_or_nan(self):
        e = episode()
        for deadline in (81, None, -1, 0, True, float('nan'), float('inf')):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                m.concession_of(e, {'requirements': {RID: .95}, 'deadlines': {RID: deadline}})


class DeadlineServiceDeficitTests(unittest.TestCase):
    def test_original_deadline_counter_delta_not_sample_ratios_or_relaxed_deadline(self):
        e = episode()
        e['serviceTrace'] = counter_trace()
        result = m.service_deficit(e)[RID]
        self.assertEqual([b['value'] for b in result['bins']], [.55, .9])
        # D0: (0.95 - 55/100)*2 + (0.95 - 90/100)*2 = 0.9 ratio*s.
        # Every eligible request (including all 55 lost/late requests) is counted.
        self.assertAlmostEqual(result['cumulativeDeficit'], .9)
        self.assertEqual(result['violationDurationSeconds'], 4)
        self.assertEqual(result['deficitUnit'], 'ratio*s')
        self.assertEqual(result['coverage'], 1)
        self.assertEqual(result['validBins'], 2)
        self.assertFalse(result['incomplete'])
        self.assertEqual(result['bins'][-1]['endMs'], 4000)
        e['T']['alternatives'][0].update(requirements={RID: .1}, deadlines={RID: 80})
        self.assertEqual(m.service_deficit(e)[RID], result)

    def test_unknown_missing_deadline_counter_and_source_are_not_zero(self):
        for failure in ('missing-kpi', 'missing-d0', 'missing-source', 'missing-clock',
                        'missing-counter', 'scalar-ratio', 'invalid-point'):
            e = episode()
            e['budget']['horizonHMs'] = 2000
            e['serviceTrace'] = counter_trace()[:3]
            point = e['serviceTrace'][1]
            value = point['kpis'][KEY]
            if failure == 'missing-kpi':
                point['kpis'].clear()
            elif failure == 'missing-d0':
                del value['byDeadlineMs']['50']
            elif failure == 'missing-source':
                del value['source']
            elif failure == 'missing-clock':
                del value['source']['clockId']
            elif failure == 'missing-counter':
                del value['byDeadlineMs']['50']['eligible']
            elif failure == 'scalar-ratio':
                point['kpis'][KEY] = 1.0
            else:
                point['valid'] = False
            with self.subTest(failure=failure):
                result = m.service_deficit(e)[RID]
                self.assertEqual(result['validBins'], 0)
                self.assertTrue(result['incomplete'])
                self.assertIsNone(result['cumulativeDeficit'])
                self.assertIsNone(result['violationDurationSeconds'])
                self.assertIsNone(result['bins'][0]['shortage'])
                self.assertIsNone(result['bins'][0]['violation'])

    def test_mixed_source_and_repeated_source_timestamp_are_unknown(self):
        for field in ('sessionId', 'flowId', 'clockId', 'observedAtMs'):
            e = episode()
            e['budget']['horizonHMs'] = 2000
            e['serviceTrace'] = counter_trace()[:3]
            value = e['serviceTrace'][1]['kpis'][KEY]
            if field == 'observedAtMs':
                value[field] = e['serviceTrace'][0]['kpis'][KEY][field]
            else:
                value['source'][field] = 'restarted'
            with self.subTest(field=field):
                result = m.service_deficit(e)[RID]
                self.assertEqual(result['coverage'], 1)
                self.assertIsNone(result['bins'][0]['value'])
                self.assertIsNone(result['cumulativeDeficit'])

    def test_intermediate_reset_remains_unknown_even_after_counter_recovers(self):
        for legacy in (False, True):
            for counter in ('issued', 'eligible', 'completed'):
                e = episode()
                e['budget']['horizonHMs'] = 2000
                e['serviceTrace'] = counter_trace()[:3]
                # Start with extra outstanding requests so each individual
                # counter can decrease while every row remains well-formed.
                e['serviceTrace'][0]['kpis'][KEY]['byDeadlineMs']['50']['issued'] = 150
                middle = e['serviceTrace'][1]['kpis'][KEY]['byDeadlineMs']['50']
                middle.update(issued=160, eligible=110, completed=60)
                middle[counter] = {'issued': 149, 'eligible': 99, 'completed': 49}[counter]
                if legacy:
                    for point in e['serviceTrace']:
                        point['kpis'][KEY] = point['kpis'][KEY]['byDeadlineMs']['50']
                with self.subTest(legacy=legacy, counter=counter):
                    result = m.service_deficit(e)[RID]
                    self.assertEqual(result['coverage'], 1)
                    self.assertIsNone(result['bins'][0]['value'])
                    self.assertTrue(result['incomplete'])

    def test_legacy_flat_counters_use_original_deadline(self):
        e = episode()
        e['serviceTrace'] = counter_trace()
        for point in e['serviceTrace']:
            point['kpis'][KEY] = point['kpis'][KEY]['byDeadlineMs']['50']
        result = m.service_deficit(e)[RID]
        self.assertEqual([b['value'] for b in result['bins']], [.55, .9])
        self.assertAlmostEqual(result['cumulativeDeficit'], .9)

    def test_endpoint_closes_delta_without_filling_missing_coverage_slot(self):
        e = episode()
        e['budget']['horizonHMs'] = 2000
        e['serviceTrace'] = [counter_trace()[0], counter_trace()[2]]
        result = m.service_deficit(e)[RID]
        self.assertEqual(result['coverage'], .5)
        self.assertIsNone(result['cumulativeDeficit'])
        e['measurementRules']['deadlineSuccessRatio']['minCoverage'] = .5
        result = m.service_deficit(e)[RID]
        self.assertAlmostEqual(result['bins'][0]['value'], .55)
        self.assertAlmostEqual(result['cumulativeDeficit'], .8)

    def test_jittered_boundary_counters_disclose_actual_interval(self):
        e = episode()
        e['serviceTrace'] = counter_trace()
        for point in e['serviceTrace']:
            point['t'] -= 100
        result = m.service_deficit(e)[RID]
        self.assertEqual([b['value'] for b in result['bins']], [.55, .9])
        self.assertEqual([b['counterStartMs'] for b in result['bins']], [-100, 1900])
        self.assertEqual([b['counterEndMs'] for b in result['bins']], [1900, 3900])
        self.assertEqual(result['coverage'], 1)
        self.assertFalse(result['incomplete'])

    def test_stale_boundary_beyond_declared_cadence_is_unknown(self):
        for boundary in ('start', 'end'):
            e = episode()
            e['budget'].update(horizonHMs=4000, binDeltaMs=4000)
            e['measurementRules']['deadlineSuccessRatio']['minCoverage'] = .5
            e['serviceTrace'] = counter_trace()
            if boundary == 'start':
                e['serviceTrace'][0]['t'] = -1001
            else:
                e['serviceTrace'] = e['serviceTrace'][:3]
            with self.subTest(boundary=boundary):
                result = m.service_deficit(e)[RID]
                self.assertIsNone(result['bins'][0]['value'])
                self.assertIsNone(result['cumulativeDeficit'])
                self.assertTrue(result['incomplete'])

    def test_missing_baseline_and_no_eligible_requests_are_unknown(self):
        for failure in ('no-baseline', 'no-eligible'):
            e = episode()
            e['budget']['horizonHMs'] = 2000
            e['serviceTrace'] = counter_trace()[:3]
            e['measurementRules']['deadlineSuccessRatio']['minCoverage'] = .5
            if failure == 'no-baseline':
                e['serviceTrace'] = e['serviceTrace'][1:]
            else:
                for point in e['serviceTrace']:
                    point['kpis'][KEY]['byDeadlineMs']['50'] = {
                        'issued': 100, 'eligible': 100, 'completed': 50}
            with self.subTest(failure=failure):
                self.assertIsNone(m.service_deficit(e)[RID]['cumulativeDeficit'])

    def test_unknown_bin_is_unavailable_for_the_horizon_but_bounded(self):
        e = episode()
        e['serviceTrace'] = counter_trace()
        del e['serviceTrace'][3]['kpis'][KEY]['source']
        result = m.service_deficit(e)[RID]
        self.assertEqual(result['validBins'], 1)
        # v3.1 section 6: this used to report cumulativeDeficit .8 beside
        # incomplete=True, i.e. the unknown bin filled with zero shortage. The
        # horizon total is now unavailable; .8 survives as its lower bound and the
        # unknown 2 s are charged the original .95 for the conservative upper bound.
        self.assertIsNone(result['cumulativeDeficit'])
        self.assertAlmostEqual(result['shortageLowerBound'], .8)
        self.assertAlmostEqual(result['shortageUpperBound'], .8 + .95*2)
        self.assertEqual(result['missingIntervals'], [{'startMs': 2000, 'endMs': 4000}])
        self.assertEqual(result['violationDurationSeconds'], 2)
        self.assertTrue(result['incomplete'])
        self.assertIsNone(result['bins'][1]['shortage'])

    def test_existing_nonratio_reducers_and_categorical_duration_unchanged(self):
        e = episode()
        e['intents'] = [{'owner': 'video', 'requirement': {
            'reqId': 'rate', 'kpi': 'dlGoodputMbps', 'scope': 'ue@131',
            'op': '>=', 'value': 3, 'unit': 'Mbps'}}]
        e['serviceTrace'] = [{'t': i * 1000, 'kpis': {'dlGoodputMbps@131': v}}
                             for i, v in enumerate((1, 3, 5, 7, 100))]
        # The statistic still reduces each bin's value; the shortage total now
        # integrates the 1 s stream (v3.1 section 6), so it is (3-1)*1 s = 2 for
        # every statistic. min/max/last used to give 4/0/0 from one bin reading.
        for statistic, values, total in (
                ('mean', [2, 6], 2), ('min', [1, 5], 2), ('max', [3, 7], 2),
                ('median', [2, 6], 2), ('last', [3, 7], 2)):
            e['measurementRules'] = {'statistic': statistic, 'sampleIntervalMs': 1000}
            with self.subTest(statistic=statistic):
                result = m.service_deficit(e)['rate']
                self.assertEqual([b['value'] for b in result['bins']], values)
                self.assertEqual(result['cumulativeDeficit'], total)
                self.assertFalse(result['incomplete'])
        e['intents'][0]['requirement'].update(kpi='servingCell', op='==', value='cell1', unit='nci')
        e['measurementRules']['statistic'] = 'last'
        e['serviceTrace'] = [{'t': i * 1000, 'kpis': {'servingCell@131': 'cell2'}}
                             for i in range(4)]
        result = m.service_deficit(e)['rate']
        self.assertIsNone(result['cumulativeDeficit'])
        self.assertEqual(result['violationDurationSeconds'], 4)


class DeadlineSuccessEvidenceTests(unittest.TestCase):
    def test_wrong_or_absent_deadline_cannot_reuse_stored_pass(self):
        for kpis, expected in (
                ({}, []), ({KEY: None}, []), (['not-a-kpi-map'], []),
                ({KEY: {'byDeadlineMs': {'80': 1.0}}}, ['T1']),
                ({KEY: {'byDeadlineMs': {'50.0': .90, '80': 1.0}}}, ['T1']),
                ({KEY: {'byDeadlineMs': {'50.0': .99, '80': .99}}}, ['T0', 'T1']),
                ({KEY: 1.0}, ['T0'])):
            e = episode()
            e['trials'] = [successful_trial(kpis)]
            with self.subTest(kpis=kpis):
                self.assertEqual([s['targetId'] for s in m.valid_successes(e)], expected)

    def test_verdict_only_legacy_and_unauthorized_deadline(self):
        e = episode()
        trial = successful_trial({})
        del trial['kpis']
        e['trials'] = [trial]
        self.assertEqual(len(m.valid_successes(e)), 2)
        e['T']['authorization'][RID]['deadlineSteps'] = 0
        self.assertEqual([s['targetId'] for s in m.valid_successes(e)], ['T0'])

    def test_cli_consumes_deadline_episode_without_changing_service_reference(self):
        e = episode()
        e['serviceTrace'] = counter_trace()
        e['trials'] = [successful_trial({KEY: {'byDeadlineMs': {'50': .55, '80': 1}}})]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'episode.json'
            output = Path(directory) / 'metrics.json'
            source.write_text(json.dumps(e), encoding='utf-8')
            m.main([str(source), '--out', str(output)])
            result = json.loads(output.read_text(encoding='utf-8'))['groups'][0]
        self.assertEqual(result['resolution']['n'], 1)
        self.assertEqual(result['originalTarget']['n'], 0)
        self.assertEqual(result['concession']['bestAttained']['D_max']['mean'], .5)
        self.assertAlmostEqual(result['serviceDeficit'][0]['requirements'][RID]['cumulativeDeficit'], .9)


if __name__ == '__main__':
    unittest.main()
