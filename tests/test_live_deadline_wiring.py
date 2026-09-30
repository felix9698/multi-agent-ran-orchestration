"""Real-source composition and all-target deadline judgement, with no network."""
from copy import deepcopy
from dataclasses import replace
import json
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from assurance.coordination.intake import KpiObservationRule, ObservationRules
from assurance.coordination.tc import (
    PASS, FAIL, UNKNOWN, Trial, deterministic_targets, intent_from_sentence,
    judge, judge_target, kpi_gaps,
)
from tools.hfconsole.agent_env import HermeticDeployment
from tools.liveconsole.agent import (
    AgentRequest, AgentSitting, _echo_sources, build_agent_sitting,
    issued_cohort_ratios,
)
from tools.liveconsole.build import LiveConsoleError
from tools.liveconsole.kpi_observer import (
    DEADLINE_RATIO_KPI, CompositeKpiObserver, KpiObserverError,
    LiveTaggedEchoObserver, TunRateObserver, build_profile_observer,
)

KEY = 'deadlineSuccessRatio@131'
ECHO = ('I4: at least 95% of tagged echo requests within 50 ms for '
        'ueId=131 owner ue1-command, relaxable in 0 steps, '
        'deadline relaxable in 1 step to 80 ms')
SOURCE = {'sessionId': 'episode-a', 'flowId': 'ue1-command', 'clockId': 'boot-a'}
PROFILE = {'liveConsole': {'ueHosts': {'131': 'ue1'}, 'taggedEcho': {'131': {
    'sourcePath': '/tmp/source path/tagged_echo.py',
    'logPath': '/tmp/echo log.jsonl', 'sessionId': 'episode-a',
    'flowId': 'ue1-command', 'maxAgeMs': 1500}}}}


def snapshot(at=1000, **changes):
    row = dict(SOURCE, schemaVersion='tagged-echo-snapshot/1', status='running',
               observedAtMs=at, remoteNowMs=at + 100,
               countersByDeadlineMs={
                   '50': {'issued': 10, 'eligible': 10, 'completed': 8},
                   '80': {'issued': 10, 'eligible': 10, 'completed': 10}},
               interface={'name': 'oaitun_ue1', 'ip': '12.1.1.10', 'ifindex': 8, 'up': True})
    row.update(changes)
    return row


class Runner:
    def __init__(self, rows):
        self.rows = list(rows)
        self.commands = []

    def run(self, argv, **kwargs):
        self.commands.append(argv)
        row = self.rows.pop(0)
        if row is None:
            return SimpleNamespace(returncode=1, stdout='', stderr='missing source')
        return SimpleNamespace(returncode=0, stdout=json.dumps(row), stderr='')


def live_observer(rows):
    return LiveTaggedEchoObserver(
        hosts={'131': 'ue1'}, source_path='/tmp/source path/tagged_echo.py',
        log_path='/tmp/echo log.jsonl', session_id='episode-a', flow_id='ue1-command',
        deadlines_ms=(50.0, 80.0), runner=Runner(rows), monotonic_ms=lambda: 9e12)


class LiveSourceWiring(unittest.TestCase):
    def test_source_command_is_quoted_and_deadlines_are_explicit(self):
        observer = live_observer([snapshot()])
        sample = observer.sample()[KEY]
        argv = shlex.split(observer.runner.commands[0][-1])
        self.assertEqual(argv[:3], ['python3', '/tmp/source path/tagged_echo.py', 'snapshot'])
        self.assertEqual(argv[argv.index('--log') + 1], '/tmp/echo log.jsonl')
        self.assertEqual(argv[argv.index('--deadlines-ms') + 1], '50,80')
        self.assertEqual(sample['byDeadlineMs']['50']['completed'], 8)
        self.assertEqual(sample['byDeadlineMs']['80']['completed'], 10)
        # The coordinator's unrelated monotonic time never enters eligibility.
        self.assertEqual(sample['observedAtMs'], 1000)
        self.assertEqual(sample['source'], {**SOURCE, 'bindEpoch': 0})

    def test_stale_wrong_flow_terminal_and_bad_counters_are_missing(self):
        bad_counts = {'50': {'issued': 10, 'eligible': 5, 'completed': 6}}
        for changes in ({'remoteNowMs': 4000}, {'flowId': 'other'},
                        {'sessionId': 'old'}, {'status': 'ended'},
                        {'remoteNowMs': float('nan')},
                        {'countersByDeadlineMs': bad_counts}):
            with self.subTest(changes=changes):
                observer = live_observer([snapshot(**changes)])
                self.assertEqual(observer.sample(), {})
                self.assertTrue(observer.failures)

    def test_same_heartbeat_and_clock_restart_do_not_establish_new_samples(self):
        observer = live_observer([snapshot(), snapshot(),
                                  snapshot(2000, clockId='boot-b'), snapshot(3000)])
        self.assertIn(KEY, observer.sample())
        self.assertEqual(observer.sample(), {})
        self.assertEqual(observer.sample(), {})
        self.assertIn(KEY, observer.sample())

    def test_profile_builds_real_sources_without_observer_injection(self):
        req = intent_from_sentence(ECHO).requirement
        observer = build_profile_observer([req], profile_document=PROFILE,
                                           hosts={'131': 'ue1'})
        self.assertIsInstance(observer, CompositeKpiObserver)
        self.assertIsInstance(observer.observers[0], TunRateObserver)
        self.assertTrue(observer.observers[0].verify_interface)
        echo = observer.observers[1]
        self.assertIsInstance(echo, LiveTaggedEchoObserver)
        # v4.7: a moving deadline is also read at 1/20 of its span for the paper metric.
        self.assertEqual(echo.deadlines_ms, tuple(round(50.0 + 30.0 * q / 20, 9) for q in range(21)))
        contract = deterministic_targets([intent_from_sentence(ECHO)])
        contract = replace(contract, alternatives=(replace(
            contract.alternatives[0], deadlines={'I4.r1': 65.0}),))
        observer.include_contract_deadlines(contract)
        self.assertEqual(echo.deadlines_ms, tuple(round(50.0 + 30.0 * q / 20, 9) for q in range(21)))  # 65 is one of them

    def test_unconfigured_actual_live_root_refuses_before_any_policy_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = HermeticDeployment.write(
                tmp, ues={'131': '12345678'}, cells={'12345678': 5, '87654321': 5})
            with patch('tools.liveconsole.agent.build_r1_policy_port',
                       side_effect=AssertionError('must refuse before R1')):
                with self.assertRaisesRegex(LiveConsoleError, 'deadlineSuccessRatio@131'):
                    build_agent_sitting(profile, AgentRequest(sentences=(ECHO,)))

    def test_sudo_source_log_is_readable_by_the_invoking_ssh_user(self):
        from pathlib import Path
        from tools.liveconsole.tagged_echo import _Log
        with tempfile.TemporaryDirectory() as tmp, \
                patch('tools.liveconsole.tagged_echo.os.geteuid', return_value=0), \
                patch.dict('os.environ', {'SUDO_UID': '1000', 'SUDO_GID': '1000'}), \
                patch('tools.liveconsole.tagged_echo.os.fchown') as chown:
            path = Path(tmp) / 'raw.jsonl'
            log = _Log(path, 'episode-a', 'ue1-command', 'boot-a')
            log.close()
            self.assertEqual(chown.call_args.args[1:], (1000, 1000))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_profile_missing_host_or_source_descriptor_is_not_an_injected_fixture(self):
        req = intent_from_sentence(ECHO).requirement
        with self.assertRaises(KpiObserverError):
            build_profile_observer([req], profile_document=PROFILE, hosts={})
        document = deepcopy(PROFILE)
        document['liveConsole']['taggedEcho']['131'].pop('sessionId')
        with self.assertRaises(KpiObserverError):
            build_profile_observer([req], profile_document=document, hosts={'131': 'ue1'})


class LiveTunContinuity(unittest.TestCase):
    def row(self, at, rx, **changes):
        row = {'rx': rx, 'atMs': at, 'clockId': 'boot-a', 'ifindex': 8,
               'addresses': ['12.1.1.10']}
        row.update(changes)
        return row

    def test_rate_uses_remote_counter_interval_and_identity_changes_rebase(self):
        observer = TunRateObserver(hosts={'131': 'ue1'}, verify_interface=True,
                                  runner=Runner([self.row(0, 0), self.row(1000, 750000),
                                                 self.row(2000, 1500000, ifindex=9),
                                                 self.row(3000, 1750000, ifindex=9)]),
                                  monotonic_ms=lambda: 999999)
        self.assertEqual(observer.sample(), {})
        self.assertEqual(observer.sample()['dlGoodputMbps@131'], 6.0)
        self.assertEqual(observer.sample(), {})
        self.assertEqual(observer.sample()['dlGoodputMbps@131'], 2.0)
        command = shlex.split(observer.runner.commands[0][-1])
        self.assertEqual(command[:2], ['python3', '-c'])
        self.assertIn("'show','up','dev'", command[2])

    def test_missing_or_down_sample_never_bridges_old_bytes_into_next_hold(self):
        observer = TunRateObserver(hosts={'131': 'ue1'}, verify_interface=True,
                                  runner=Runner([self.row(0, 0), None,
                                                 self.row(2000, 1500000),
                                                 self.row(3000, 2000000, addresses=[])]))
        for _ in range(4):
            self.assertEqual(observer.sample(), {})


class EveryDeadlineInOneWindow(unittest.TestCase):
    def setUp(self):
        self.rule = KpiObservationRule('deadlineSuccessRatio', window_ms=2000,
                                       statistic='ratio', min_coverage=1)
        self.rows = [(t, {'byDeadlineMs': {
            '50': {'issued': n, 'eligible': n, 'completed': n * 4 // 5},
            '80': {'issued': n, 'eligible': n, 'completed': n}},
            'source': dict(SOURCE), 'observedAtMs': t})
            for t, n in ((0, 0), (1000, 10), (2000, 20))]
        self.intents = [intent_from_sentence(ECHO)] + [intent_from_sentence(
            f'I{i}: UE ueId={ue} needs at least 1 Mbps downlink, relaxable in 0 steps')
            for i, ue in ((1, 131), (2, 132), (3, 133))]
        self.contract = deterministic_targets(self.intents)

    def test_one_four_intent_vector_fails_d0_but_passes_authorized_d1(self):
        result, coverage = self.rule.aggregate(self.rows, 2000, 1000)
        self.assertEqual(result, {'byDeadlineMs': {'50': 0.8, '80': 1.0}})
        self.assertEqual(coverage, 1)
        kpis = {KEY: result, **{f'dlGoodputMbps@{ue}': 2.0 for ue in (131, 132, 133)}}
        trial = Trial(1, kpis=kpis, window={'valid': True})
        trial.judge_against(self.contract, self.intents)
        self.assertFalse(trial.success[self.contract.t0.target_id])
        relaxed = self.contract.alternatives[0]
        self.assertTrue(trial.success[relaxed.target_id])
        self.assertEqual(judge_target(kpis, relaxed, self.contract.authorization),
                         trial.verdicts[relaxed.target_id])
        gaps = kpi_gaps(kpis, self.contract, relaxed)['perRequirement']['I4.r1']
        self.assertEqual(gaps['againstT0']['verdict'], FAIL)
        self.assertEqual(gaps['againstNext']['verdict'], PASS)

    def test_scalar_ratio_is_not_reused_across_deadlines(self):
        original = judge({KEY: 1.0}, self.contract.t0, self.intents)
        relaxed = judge({KEY: 1.0}, self.contract.alternatives[0], self.intents)
        self.assertEqual(original['I4.r1'], PASS)
        self.assertEqual(relaxed['I4.r1'], UNKNOWN)

    def test_missing_deadline_is_unknown_not_a_string_comparison_failure(self):
        result = judge({KEY: {'byDeadlineMs': {'80': 1.0}}},
                       self.contract.t0, self.intents)
        self.assertEqual(result['I4.r1'], UNKNOWN)

    def test_source_or_clock_boundary_invalidates_whole_ratio_window(self):
        for field in ('sessionId', 'flowId', 'clockId'):
            rows = deepcopy(self.rows)
            rows[1][1]['source'][field] = 'different'
            self.assertEqual(self.rule.aggregate(rows, 2000, 1000)[0], UNKNOWN)

    def test_intermediate_reset_and_repeated_heartbeat_cannot_qualify(self):
        rows = deepcopy(self.rows)
        rows[1][1]['byDeadlineMs']['50']['eligible'] = 30
        result = self.rule.aggregate(rows, 2000, 1000)[0]
        self.assertNotIn('50', result['byDeadlineMs'])
        rows = deepcopy(self.rows)
        rows[1][1]['observedAtMs'] = 0
        self.assertEqual(self.rule.aggregate(rows, 2000, 1000)[0], UNKNOWN)

    def test_zero_eligible_requests_is_unknown(self):
        for _, value in self.rows:
            for counters in value['byDeadlineMs'].values():
                counters.update(issued=0, eligible=0, completed=0)
        self.assertEqual(self.rule.aggregate(self.rows, 2000, 1000)[0], UNKNOWN)


class TheIssuedRequestCohort(unittest.TestCase):
    """The judged cohort is the requests ISSUED in ``[start, end)``.

    Differencing the source's cumulative counters answers a different
    question -- which requests *matured* in the window -- and is plausible
    enough to survive unnoticed, which is why it did.  These fix the cohort,
    the two clocks it is bounded by, and what an unusable one is recorded as.
    """

    #: The live shape: ``5000`` settling excluded, the judged window is the
    #: 15 s tail of the 20 s hold.
    RULES = ObservationRules({DEADLINE_RATIO_KPI: KpiObservationRule(
        kpi=DEADLINE_RATIO_KPI, settle_ms=5000, window_ms=15000,
        statistic='ratio', min_coverage=0.0, validity_ms=120000)})

    #: Wall-clock sample stamps, and the source's own monotonic clock beside
    #: them.  The two share no epoch: that is the point of the anchoring.
    HOLD_END = 20000.0
    SETTLE_OBSERVED_AT = 105000.0        # t=5000, the settle boundary
    LAST_OBSERVED_AT = 120000.0          # t=20000, the end of the hold

    def rows(self, matured=((100, 100, 90), (140, 140, 92))):
        """A hold whose cumulative counters difference to a low ratio."""
        base, last = matured
        counts = []
        for issued, eligible, completed in (base, base, last):
            counts.append({name: {'issued': issued, 'eligible': eligible,
                                  'completed': completed} for name in ('50', '80')})
        stamps = ((0.0, 100000.0), (5000.0, self.SETTLE_OBSERVED_AT),
                  (self.HOLD_END, self.LAST_OBSERVED_AT))
        return [{'t': stamp, 'kpis': {KEY: {
            'byDeadlineMs': row, 'source': dict(SOURCE), 'observedAtMs': at}}}
            for (stamp, at), row in zip(stamps, counts)]

    def cohort(self, counters, **kwargs):
        """``issued_cohort_ratios`` over one windowed snapshot from the source."""
        observer = live_observer([snapshot(int(self.LAST_OBSERVED_AT),
                                           countersByDeadlineMs=counters)])
        rows = kwargs.pop('rows', None)
        result = issued_cohort_ratios(
            self.rows() if rows is None else rows, self.HOLD_END, self.RULES,
            (observer,), **kwargs)
        return result, observer

    def test_the_cohort_is_the_issued_set_not_the_matured_set(self):
        # Differencing says 2 of 40 matured requests answered in time. The
        # requests *issued* in the same window are a different set entirely.
        old = self.RULES.rule_for(KEY).aggregate(
            [(row['t'], row['kpis'][KEY]) for row in self.rows()], self.HOLD_END, 1000)[0]
        self.assertEqual(old['byDeadlineMs']['50'], 0.05)
        result, observer = self.cohort({
            '50': {'issued': 20, 'eligible': 20, 'completed': 18, 'valid': True},
            '80': {'issued': 20, 'eligible': 20, 'completed': 20, 'valid': True}})
        self.assertEqual(result[KEY], {'byDeadlineMs': {'50': 0.9, '80': 1.0}})
        # ... and the cohort was bounded by the SOURCE's clock, taken from the
        # settle-boundary and final samples, not by the wall-clock window.
        argv = shlex.split(observer.runner.commands[0][-1])
        self.assertEqual(argv[argv.index('--window-start-ms') + 1], '105000')
        self.assertEqual(argv[argv.index('--window-end-ms') + 1], '120000')

    def test_zero_issued_requests_is_unknown_and_never_a_zero_or_a_pass(self):
        result, _ = self.cohort({
            name: {'issued': 0, 'eligible': 0, 'completed': 0, 'valid': False,
                   'invalidReason': 'no-issued-requests'} for name in ('50', '80')})
        self.assertEqual(result[KEY], UNKNOWN)
        self.assertNotEqual(result[KEY], 0.0)

    def test_a_collection_that_ended_early_is_unknown_with_its_reason(self):
        result, _ = self.cohort({
            name: {'issued': 20, 'eligible': 4, 'completed': 4, 'valid': False,
                   'invalidReason': 'collection-ended-early'} for name in ('50', '80')})
        self.assertEqual(result[KEY], UNKNOWN)

    def test_one_invalid_level_leaves_the_other_level_judged(self):
        result, _ = self.cohort({
            '50': {'issued': 20, 'eligible': 20, 'completed': 18, 'valid': True},
            '80': {'issued': 0, 'eligible': 0, 'completed': 0, 'valid': False,
                   'invalidReason': 'no-issued-requests'}})
        self.assertEqual(result[KEY], {'byDeadlineMs': {'50': 0.9}})

    def test_a_normal_window_still_reads_the_ratio_it_reads_today(self):
        # When the issued cohort IS the matured set, the corrected path returns
        # the same number the differencing does -- no silent re-scaling.
        rows = self.rows(matured=((100, 100, 90), (120, 120, 108)))
        old = self.RULES.rule_for(KEY).aggregate(
            [(row['t'], row['kpis'][KEY]) for row in rows], self.HOLD_END, 1000)[0]
        self.assertEqual(old['byDeadlineMs']['50'], 0.9)
        result, _ = self.cohort({
            '50': {'issued': 20, 'eligible': 20, 'completed': 18, 'valid': True},
            '80': {'issued': 20, 'eligible': 20, 'completed': 18, 'valid': True}},
            rows=rows)
        self.assertEqual(result[KEY]['byDeadlineMs']['50'],
                         old['byDeadlineMs']['50'])

    def test_an_unreadable_source_is_unknown_rather_than_the_matured_set(self):
        observer = live_observer([None])
        result = issued_cohort_ratios(self.rows(), self.HOLD_END, self.RULES,
                                      (observer,))
        self.assertEqual(result[KEY], UNKNOWN)

    def test_the_trailing_collection_is_waited_out_before_the_source_is_read(self):
        slept = []
        self.cohort({name: {'issued': 20, 'eligible': 20, 'completed': 18,
                            'valid': True} for name in ('50', '80')},
                    sleep_ms=slept.append)
        # Every request issued in the window is owed the longest deadline.
        self.assertEqual(slept, [80])

    def test_synthetic_cohort_audit_keeps_counts_bounds_identity_and_reason(self):
        """Synthetic cohort: the audit record says what was issued, matched and why invalid."""
        identity = {'startMs': 105000.0, 'endMs': 120000.0, 'valid': False, 'issued': 20,
                    'firstSeq': 7, 'lastSeq': 26, 'firstIssuedAtMs': 105010.0,
                    'lastIssuedAtMs': 119960.0, 'seqSha256': 'ab'}
        audit = {}
        result, _ = self.cohort({
            '50': {'issued': 20, 'eligible': 20, 'completed': 18, 'valid': True},
            '80': {'issued': 20, 'eligible': 4, 'completed': 4, 'valid': False,
                   'invalidReason': 'collection-ended-early'}},
            rows=None, audit=audit)
        self.assertEqual(result[KEY], {'byDeadlineMs': {'50': 0.9}})
        record = audit[KEY]
        # One invalid level makes the observation invalid, with its reason --
        # never a zero and never a success.
        self.assertFalse(record['valid'])
        self.assertEqual(record['invalidReason'], '80ms:collection-ended-early')
        self.assertEqual(record['issued'], 20)
        self.assertEqual(record['matchedByDeadlineMs'], {'50': 18, '80': 4})
        self.assertEqual((record['windowStartMs'], record['windowEndMs']),
                         (self.SETTLE_OBSERVED_AT, self.LAST_OBSERVED_AT))
        self.assertEqual(record['levels']['50'], {'valid': True, 'invalidReason': None})
        # The source's cohort identity rides through the observer unchanged.
        observer = live_observer([snapshot(int(self.LAST_OBSERVED_AT), window=identity)])
        self.assertEqual(observer.sample()[KEY]['window'], identity)

    def test_a_cohort_whose_configuration_was_not_held_is_not_read(self):
        audit, slept = {}, []
        observer = live_observer([])
        result = issued_cohort_ratios(self.rows(), self.HOLD_END, self.RULES, (observer,),
                                      sleep_ms=slept.append, audit=audit, held=False)
        self.assertEqual(result[KEY], UNKNOWN)
        self.assertEqual((observer.runner.commands, slept), ([], []))
        self.assertEqual(audit[KEY]['invalidReason'],
                         'configuration-not-held-through-trailing-collection')

    def test_an_unreadable_source_names_its_error_in_the_audit(self):
        audit = {}
        observer = live_observer([None])
        issued_cohort_ratios(self.rows(), self.HOLD_END, self.RULES, (observer,), audit=audit)
        self.assertEqual((audit[KEY]['valid'], audit[KEY]['invalidReason']),
                         (False, 'source-unreadable'))
        self.assertIn('ssh exit', audit[KEY]['error'])

    def test_with_no_live_source_nothing_is_corrected_at_all(self):
        self.assertEqual(
            issued_cohort_ratios(self.rows(), self.HOLD_END, self.RULES, ()), {})
        plain = TunRateObserver(hosts={'131': 'ue1'})
        self.assertEqual(_echo_sources(plain), ())

    def test_the_default_command_carries_no_window_and_is_unchanged(self):
        observer = live_observer([snapshot()])
        observer.sample()
        argv = shlex.split(observer.runner.commands[0][-1])
        self.assertNotIn('--window-start-ms', argv)
        self.assertNotIn('--window-end-ms', argv)
        self.assertEqual(argv[-2:], ['--max-age-ms', '1500.0'])


class TheUnusableCohortReachesTheWindowRecord(unittest.TestCase):
    """``UNKNOWN`` is dropped from the vector and named in ``unknownKpis``."""

    def sitting(self, observer):
        # The aggregation is exercised on the real method; nothing else of a
        # sitting is needed for it, and building one would freeze an epoch.
        sitting = object.__new__(AgentSitting)
        sitting.rules = TheIssuedRequestCohort.RULES
        sitting._cached_cadence_ms = 1000
        sitting.observer = observer
        sitting.clock = SimpleNamespace(sleep_ms=lambda ms: None,
                                        now=lambda: '2026-09-15T00:00:30.000000Z')
        return sitting

    def test_an_empty_cohort_is_recorded_unknown_not_as_a_measured_zero(self):
        observer = live_observer([snapshot(120000, countersByDeadlineMs={
            name: {'issued': 0, 'eligible': 0, 'completed': 0, 'valid': False,
                   'invalidReason': 'no-issued-requests'} for name in ('50', '80')})])
        rows = TheIssuedRequestCohort().rows()
        kpis, unknown, _coverage, cohorts = self.sitting(observer)._aggregate_window(
            rows, TheIssuedRequestCohort.HOLD_END)
        self.assertNotIn(KEY, kpis)
        self.assertIn(KEY, unknown)
        self.assertFalse(cohorts['byKpi'][KEY]['valid'])
        self.assertIn('no-issued-requests', cohorts['byKpi'][KEY]['invalidReason'])
        sitting = self.sitting(observer)
        sitting.rules = TheIssuedRequestCohort.RULES
        window = sitting._window_record('s', 'e', rows, kpis, unknown, {}, cohorts)
        # The window itself is invalid and says why, not just missing a KPI.
        self.assertFalse(window['valid'])
        self.assertIn(f'{KEY} cohort invalid', window['reason'])
        self.assertEqual(window['cohorts'], cohorts['byKpi'])

    def test_a_valid_cohort_reaches_the_vector(self):
        observer = live_observer([snapshot(120000, countersByDeadlineMs={
            name: {'issued': 20, 'eligible': 20, 'completed': 18, 'valid': True}
            for name in ('50', '80')})])
        rows = TheIssuedRequestCohort().rows()
        kpis, unknown, _coverage, cohorts = self.sitting(observer)._aggregate_window(
            rows, TheIssuedRequestCohort.HOLD_END)
        self.assertEqual(kpis[KEY], {'byDeadlineMs': {'50': 0.9, '80': 0.9}})
        self.assertNotIn(KEY, unknown)
        self.assertTrue(cohorts['byKpi'][KEY]['valid'])
        window = self.sitting(observer)._window_record(
            's', 'e', rows, kpis, unknown, {}, cohorts)
        self.assertTrue(window['valid'])
        # The trailing collection's completion is its own stamp, not the window end.
        self.assertEqual(cohorts['trailingCollectionEnd'], '2026-09-15T00:00:30.000000Z')


if __name__ == '__main__':
    unittest.main()
