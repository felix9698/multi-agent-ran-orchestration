"""Seeded physics and the real joint Kernel over local emulator ports only."""
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from assurance.gateway.mock_adapter import FaultInjection
from assurance.objectives.joint import (
    CapAxisSpec, IntentSpec, PriorityAxisSpec, SteeringAxisSpec,
    McsBoundsAxisSpec, TxAttenuationAxisSpec, SlicePrbQuotaAxisSpec,
)
from tools.hfconsole.agent_env import (
    CONDITIONS, EmulatedActuationAdapter, EmulatedClock, EmulatedKpiObserver,
    EmulatedKpmStream, EmulatedRan, build_hardware_free_agent_runtime, three_ue_topology,
    _counter_loaders,
)


class EmulatorPhysics(unittest.TestCase):
    def setUp(self):
        self.ran = three_ue_topology(noise_sigma=0)

    def test_capacity_split_and_pf_weights(self):
        self.assertEqual({'131': 2.5, '132': 2.5, '133': 4.0}, self.ran.dl_goodput())
        self.ran.apply('pfWeight', '131', 24)
        self.assertEqual(3.75, self.ran.dl_goodput()['131'])
        self.assertEqual(1.25, self.ran.dl_goodput()['132'])

    def test_cap_clips_and_redistributes(self):
        self.ran.apply('dlPrbCap', '132', 6)
        self.assertEqual(1.25, self.ran.dl_goodput()['132'])
        self.assertEqual(3.75, self.ran.dl_goodput()['131'])
        self.ran.offered_load_mbps['131'] = 1
        self.assertEqual(1, self.ran.dl_goodput()['131'])
        self.assertLessEqual(sum(self.ran.dl_goodput()[u] for u in ('131', '132')), 5)

    def test_handover_membership_and_transient(self):
        self.ran.apply('servingCell', '131', '87654321')
        self.assertEqual('87654321', self.ran.serving_cell('131'))
        self.assertEqual(0, self.ran.dl_goodput()['131'])
        self.assertEqual(4, self.ran.dl_goodput()['132'])
        self.ran.advance(1999)
        self.assertEqual(0, self.ran.dl_goodput()['131'])
        self.ran.advance(1)
        self.assertEqual(2.5, self.ran.dl_goodput()['131'])
        self.assertEqual(2.5, self.ran.dl_goodput()['133'])
        self.ran.apply('servingCell', '131', '87654321')
        self.assertEqual(2.5, self.ran.dl_goodput()['131'])

    def test_seed_reproducibility_and_poll_count_independence(self):
        a, b = three_ue_topology(seed=17), three_ue_topology(seed=17)
        first = a.dl_goodput()
        self.assertEqual(first, a.dl_goodput())
        self.assertEqual(first, b.dl_goodput())
        a.advance(1000)
        b.advance(1000)
        self.assertEqual(a.dl_goodput(), b.dl_goodput())
        self.assertNotEqual(first, a.dl_goodput())
        self.assertNotEqual(first, three_ue_topology(seed=18).dl_goodput())

    def test_generic_ue_ids_and_link_efficiency(self):
        ran = EmulatedRan(ues={'21': '7', '22': '7', '23': '7', '24': '8'},
                          cells={'7': 6, '8': 9}, noise_sigma=0,
                          offered_load_mbps={u: 20 for u in ('21', '22', '23', '24')},
                          link_efficiency={'21': {'7': 2}})
        self.assertEqual({'21': 3, '22': 1.5, '23': 1.5, '24': 9}, ran.dl_goodput())

    def test_cell_rate_ceilings_are_local_and_monotone(self):
        baseline = self.ran.dl_goodput()
        self.ran.apply('dlMcsBounds', '12345678', '0..16')
        capped = self.ran.dl_goodput()
        self.assertLess(capped['131'], baseline['131'])
        self.assertEqual(baseline['133'], capped['133'])
        self.ran.apply('dlMcsBounds', '12345678', '10..16')
        self.assertLess(self.ran.dl_goodput()['131'], capped['131'])
        self.ran.apply('dlMcsBounds', '12345678', '0..28')
        self.assertEqual(baseline, self.ran.dl_goodput())
        self.ran.apply('txAttenuationDb', '12345678', '6.0')
        attenuated = self.ran.dl_goodput()['131']
        self.assertLess(attenuated, baseline['131'])
        self.ran.apply('txAttenuationDb', '12345678', '12.0')
        self.assertLess(self.ran.dl_goodput()['131'], attenuated)
        self.ran.apply('txAttenuationDb', '12345678', '0.0')
        self.assertEqual(baseline, self.ran.dl_goodput())

    def test_slice_quota_is_aggregate_per_cell_and_frees_other_slice(self):
        ran = EmulatedRan(ues={'1': '7', '2': '7', '3': '7', '4': '8'},
                          cells={'7': 10, '8': 10}, noise_sigma=0,
                          offered_load_mbps={u: 20 for u in ('1', '2', '3', '4')},
                          ue_slices={'1': '1', '2': '1', '3': '2', '4': '1'})
        ran.apply('slicePrbQuota', '1', '0:1:30')
        rates = ran.dl_goodput()
        self.assertAlmostEqual(3, rates['1'] + rates['2'])
        self.assertAlmostEqual(7, rates['3'])
        self.assertAlmostEqual(3, rates['4'])
        ran.apply('dlPrbCap', '3', 6)
        self.assertLessEqual(ran.dl_goodput()['3'], 2.5)
        ran.apply('slicePrbQuota', '1', '0:0:0')
        self.assertEqual(0, ran.dl_goodput()['1'])
        self.assertEqual(0, ran.dl_goodput()['4'])

    def test_new_adapters_validate_before_write_and_restore_lost_ack(self):
        for axis, applied, invalid in (
                ('dlMcsBounds@12345678', '10..16', '20..10'),
                ('txAttenuationDb@12345678', '6.0', '60.1'),
                ('slicePrbQuota@1', '0:1:30', '20:1:30')):
            with self.subTest(axis=axis):
                baseline = self.ran.axis_value(axis)
                adapter = EmulatedActuationAdapter(self.ran, axis)
                command = {'operation': 'APPLY', 'idempotencyKey': axis,
                           'axis': axis, 'value': applied}
                with self.assertRaises(ValueError):
                    adapter.dispatch(token=None, command={**command, 'value': invalid})
                self.assertEqual([], adapter.writes)
                self.assertEqual(baseline, self.ran.axis_value(axis))
                adapter.set_faults(FaultInjection(fail_axes={axis}))
                adapter.dispatch(token=None, command=command)
                self.assertEqual(baseline, self.ran.axis_value(axis))
                adapter.set_faults(FaultInjection(drop_ack_axes={axis}))
                result = adapter.dispatch(token=None, command=command)
                self.assertEqual('UNKNOWN', result.outcome.value)
                self.assertEqual(applied, self.ran.axis_value(axis))
                self.assertEqual(applied, adapter.snapshot()[axis])
                adapter.dispatch(token=None, command={**command, 'operation': 'UNDO', 'value': baseline})
                self.assertEqual(baseline, self.ran.axis_value(axis))

    def test_cell_and_slice_counter_units_scopes_and_composites(self):
        self.ran.apply('dlMcsBounds', '12345678', '10..16')
        self.ran.apply('txAttenuationDb', '12345678', '6.0')
        self.ran.apply('slicePrbQuota', '1', '0:1:30')
        cases = (
            ('RAN.Cell.DlMcsBounds', {'cellId': '12345678'}, 16, 'MCS-index', '10..16'),
            ('RAN.Cell.TxAttenuationDb', {'cellId': '12345678'}, 6, 'dB', '6.0'),
            ('RAN.SlicePrbQuotaMin', {'sst': '1'}, 1, 'percent', '0:1:30'),
            ('L1M.SS-RSRP', {'cellId': '12345678'}, -86, 'dBm', None))
        geometry = SimpleNamespace(counters=[SimpleNamespace(
            deployment_counter_name=name, counter_id=name, scope=scope, cadence_ms=1000)
            for name, scope, *_ in cases])
        with patch('assurance.live.objective_runtime.bundle_geometry', return_value=geometry):
            loaders = _counter_loaders(SimpleNamespace(bundle=None), self.ran,
                                       EmulatedClock(self.ran), {})
        for name, scope, value, unit, encoded in cases:
            sample = loaders[name]()[0]
            self.assertEqual(value, sample.value.value)
            self.assertEqual(unit, sample.value.unit)
            self.assertEqual(encoded, sample.scope_snapshot.get('appliedValue'))
            self.assertNotIn('amf_ue_ngap_id', sample.scope_snapshot)
            self.assertEqual(1, loaders[name]()[0].sequence)
        self.ran.apply('dlMcsBounds', '12345678', '0..28')
        self.assertEqual(28, loaders['RAN.Cell.DlMcsBounds']()[0].value.value)

    def test_presets(self):
        for condition, load in CONDITIONS.items():
            ran = three_ue_topology(condition=condition)
            self.assertEqual({load}, set(ran.offered_load_mbps.values()))

    def test_kpm_attribution_and_observer_follow_handover(self):
        clock = EmulatedClock(self.ran)
        stream = EmulatedKpmStream(clock, self.ran, 42)
        before = json.loads(stream()[0])
        self.ran.apply('servingCell', '131', '87654321')
        after = json.loads(stream()[0])
        self.assertNotEqual(before['e2_node'], after['e2_node'])
        self.assertNotEqual(before['nb_id'], after['nb_id'])
        self.assertEqual(42, after['connection_epoch'])
        sample = EmulatedKpiObserver(self.ran).sample()
        self.assertEqual(6, len(sample))
        self.assertEqual(0, sample['dlGoodputMbps@131'])
        self.assertEqual('87654321', sample['servingCell@131'])
        clock.sleep_ms(2000)
        self.assertEqual(2.5, EmulatedKpiObserver(self.ran).sample()['dlGoodputMbps@131'])
        stream.publishing = False
        self.assertEqual((), stream())

    def test_adapter_write_lost_ack_refusal_and_undo(self):
        axis = 'dlPrbCap@132'
        adapter = EmulatedActuationAdapter(self.ran, axis, baseline='0')
        command = {'operation': 'APPLY', 'idempotencyKey': 'apply', 'axis': axis, 'value': '6'}
        adapter.set_faults(FaultInjection(fail_axes={axis}))
        adapter.dispatch(token=None, command=command)
        self.assertEqual(24, self.ran.caps['132'])
        adapter.set_faults(FaultInjection(drop_ack_axes={axis}))
        result = adapter.dispatch(token=None, command=command)
        self.assertEqual('UNKNOWN', result.outcome.value)
        self.assertEqual(6, self.ran.caps['132'])
        adapter.dispatch(token=None, command={**command, 'operation': 'UNDO', 'value': '0'})
        self.assertEqual(24, self.ran.caps['132'])

    def test_adapter_physical_and_ratio_pf_units(self):
        physical = EmulatedActuationAdapter(self.ran, 'pfWeight@131')
        physical.dispatch(token=None, command={'operation': 'APPLY', 'idempotencyKey': 'pf',
                                              'axis': 'pfWeight@131', 'value': '16'})
        self.assertEqual(16, self.ran.pf_weights['131'])
        ratio = EmulatedActuationAdapter(self.ran, 'pfWeight@131', baseline='1.0', pf_ratio=True)
        ratio.dispatch(token=None, command={'operation': 'UNDO', 'idempotencyKey': 'undo',
                                           'axis': 'pfWeight@131', 'value': '1.0'})
        self.assertEqual(8, self.ran.pf_weights['131'])


class JointRuntimeIntegration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = temporary.name
        self.ran = three_ue_topology(noise_sigma=0)
        self.intents = [IntentSpec(f'I{i}', 'UELevelTarget', ue, 12345678, 87654321)
                        for i, ue in enumerate(('131', '132'), 1)]
        self.axes = [SteeringAxisSpec(ue, 12345678, (12345678, 87654321)) for ue in ('131', '132')]

    def build(self, extra):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            return build_hardware_free_agent_runtime(
                intents_spec=self.intents, axes_spec=self.axes + extra, ran=self.ran,
                budget_trials=4, tmp_dir=self.tmp)

    def test_existing_agent_sitting_smoke_with_cap(self):
        from tools.liveconsole.agent import AgentRequest, build_agent_sitting
        # Cap and PF are exposed on EVERY intent UE by default (contract v3),
        # so a composition root that wires only some axes has to say which
        # kinds it wired: an axis with no emulated adapter falls through to
        # the live producer and refuses on R1 TLS material, which is the
        # deployment's rule working, not this smoke test's subject.
        env = self.build([CapAxisSpec(ue, 12345678, (6, 12), 100, 'emulator')
                          for ue in ('131', '132')])
        request = AgentRequest(sentences=tuple(
            f'Hold the UE-level serving cell at 87654321 nci for ueId={ue}' for ue in ('131', '132')),
            budget_trials=1, caps={ue: (6, 12) for ue in ('131', '132')},
            axes=('servingCell', 'dlPrbCap'), controlled_reserve_kbps=100,
            cap_calibration_ref='emulator')
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            sitting = build_agent_sitting(env.profile_path, request, **env.sitting_kwargs())
            self.assertEqual('MOCK', sitting.mode)
            sitting.confirm()
            sitting.run()
        self.assertEqual(1, len(sitting.runtime.trials))
        # Both intents ask for the same serving cell, so the deterministic
        # Trajectory offers the joint move (steer 131 and 132 together) first --
        # the predictor says it satisfies T0 and it does.
        # Owner decision 2026-09-20 (``reset_each_trial``): every judged trial is
        # rolled back to the frozen C0, so a trial whose mandatory predicates all
        # passed settles as ``PASS_RESET`` -- the evidence is CLOSED_PASS and the
        # stop reason names the rollback.  ``SETTLED_SUCCESS`` is what the same
        # trial reaches with the reset off, which is asserted right below.
        trial = sitting.runtime.trials[0]
        self.assertEqual('PASS_RESET', trial['outcome'])
        self.assertEqual('SETTLED_NON_SUCCESS', trial['terminalState'])
        self.assertEqual('BASELINE_RESET', trial['stopReason'])
        self.assertEqual('CLOSED_PASS', trial['evidenceStatus'])

    def test_a_passing_trial_settles_live_when_the_reset_is_off(self):
        """The same smoke sitting, without the per-trial rollback."""
        from dataclasses import replace as _replace
        from tools.liveconsole.agent import AgentRequest, build_agent_sitting
        env = self.build([CapAxisSpec(ue, 12345678, (6, 12), 100, 'emulator')
                          for ue in ('131', '132')])
        request = _replace(AgentRequest(sentences=tuple(
            f'Hold the UE-level serving cell at 87654321 nci for ueId={ue}' for ue in ('131', '132')),
            budget_trials=1, caps={ue: (6, 12) for ue in ('131', '132')},
            axes=('servingCell', 'dlPrbCap'), controlled_reserve_kbps=100,
            cap_calibration_ref='emulator'), reset_each_trial=False)
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            sitting = build_agent_sitting(env.profile_path, request, **env.sitting_kwargs())
            sitting.confirm()
            sitting.run()
        self.assertEqual('SUCCESS', sitting.runtime.trials[0]['outcome'])
        self.assertEqual('SETTLED_SUCCESS', sitting.runtime.trials[0]['terminalState'])

    def test_steering_success_is_observed_from_target_node(self):
        env = self.build([])
        entry = next(e for e in env.runtime.catalog_entries()
                     if all(value == '87654321' for value in e.candidate.parameters.values()))
        geometry = env.runtime.geometry
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            _, report = env.runtime.run_candidate(entry.candidate.candidate_id,
                observation_polls=geometry.hold_ms // geometry.cadence_ms + 1,
                cadence_ms=geometry.cadence_ms, settle_ms=250)
        self.assertEqual('SUCCESS', report.outcome.value)
        self.assertEqual('SETTLED_SUCCESS', report.terminal_state.value)
        self.assertEqual('87654321', self.ran.serving_cell('131'))
        self.assertEqual('87654321', self.ran.serving_cell('132'))
        self.assertGreater(env.kpi_observer.sample()['dlGoodputMbps@131'], 0)

    def test_applied_cap_feeds_raw_counters_then_rolls_back(self):
        env = self.build([CapAxisSpec('132', 12345678, (6,), 100, 'emulator')])
        entry = next(e for e in env.runtime.catalog_entries()
                     if e.candidate.parameters == {'dlPrbCap@132': '6',
                         'servingCell@131': '12345678', 'servingCell@132': '12345678'})
        observed = []
        geometry = env.runtime.geometry
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            _, report = env.runtime.run_candidate(entry.candidate.candidate_id,
                observation_polls=geometry.hold_ms // geometry.cadence_ms + 1,
                cadence_ms=geometry.cadence_ms, settle_ms=250,
                on_poll=lambda _: observed.append(env.kpi_observer.sample()))
        self.assertEqual(1.25, observed[0]['dlGoodputMbps@132'])
        self.assertEqual(3.75, observed[0]['dlGoodputMbps@131'])
        self.assertEqual(24, self.ran.caps['132'])
        self.assertEqual('FAIL', report.outcome.value)
        samples = env.counter_sample_loaders['counter/joint@132/ue-throughput-controlled']()
        self.assertEqual(2500, samples[0].value.value)
        self.assertEqual('132', samples[0].scope_snapshot['amf_ue_ngap_id'])

    def test_cell_and_slice_axes_run_through_kernel_and_roll_back(self):
        env = self.build([McsBoundsAxisSpec(cell_nci=12345678, bounds=('0..16',)),
                          TxAttenuationAxisSpec(cell_nci=12345678, attenuations=('6.0',)),
                          SlicePrbQuotaAxisSpec(sst=1, quotas=('0:1:30',))])
        configuration = {'servingCell@131': '12345678', 'servingCell@132': '12345678',
                         'dlMcsBounds@12345678': '0..16', 'txAttenuationDb@12345678': '6.0',
                         'slicePrbQuota@1': '0:1:30'}
        entry = next(e for e in env.runtime.catalog_entries()
                     if e.candidate.parameters == configuration)
        observed = []
        geometry = env.runtime.geometry
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            _, report = env.runtime.run_candidate(entry.candidate.candidate_id,
                observation_polls=geometry.hold_ms // geometry.cadence_ms + 1,
                cadence_ms=geometry.cadence_ms, settle_ms=250,
                on_poll=lambda _: observed.append({axis: self.ran.axis_value(axis)
                    for axis in configuration}))
        self.assertIn(configuration, observed)
        self.assertEqual('FAIL', report.outcome.value)
        self.assertEqual('SETTLED_NON_SUCCESS', report.terminal_state.value)
        self.assertEqual('0..28', self.ran.axis_value('dlMcsBounds@12345678'))
        self.assertEqual('0.0', self.ran.axis_value('txAttenuationDb@12345678'))
        self.assertEqual('0:1:100', self.ran.axis_value('slicePrbQuota@1'))
        for loader in env.counter_sample_loaders.values():
            self.assertTrue(loader())

    def test_pf_candidate_runs_through_kernel_and_restores_emulator(self):
        env = self.build([PriorityAxisSpec('131', 12345678, (2,))])
        entry = next(e for e in env.runtime.catalog_entries()
                     if e.candidate.parameters['pfWeight@131'] == '2.0'
                     and all(e.candidate.parameters[f'servingCell@{u}'] == '12345678'
                             for u in ('131', '132')))
        observed = []
        geometry = env.runtime.geometry
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            _, report = env.runtime.run_candidate(entry.candidate.candidate_id,
                observation_polls=geometry.hold_ms // geometry.cadence_ms + 1,
                cadence_ms=geometry.cadence_ms, settle_ms=250,
                on_poll=lambda _: observed.append(self.ran.pf_weights['131']))
        self.assertIn(16, observed)
        self.assertEqual(8, self.ran.pf_weights['131'])
        self.assertEqual('SETTLED_NON_SUCCESS', report.terminal_state.value)
        self.assertEqual('FAIL', report.outcome.value)


class TheTaggedEchoFlow(unittest.TestCase):
    """Scenario I4 in the emulator: issued, eligible, completed.

    The flow is off until a sitting declares a deadline, so a run that never
    mentions one observes exactly the vector it always did.  Once on, every
    request is served at the share the goodput model reports at the moment it
    was issued -- which is what makes the counters a property of the emulated
    radio rather than of how often the executor polled it.
    """

    def test_a_sitting_with_no_deadline_sees_the_vector_it_always_saw(self):
        sample = EmulatedKpiObserver(three_ue_topology()).sample()
        self.assertEqual(6, len(sample))
        self.assertFalse([key for key in sample if key.startswith('deadlineSuccessRatio')])

    def test_the_observer_reports_counters_not_a_ratio(self):
        ran = three_ue_topology(echo_deadline_ms={'131': 50.0})
        ran.advance(10000)
        sample = EmulatedKpiObserver(ran).sample()
        self.assertEqual(7, len(sample))
        counters = sample['deadlineSuccessRatio@131']
        self.assertEqual({'issued', 'eligible', 'completed'}, set(counters))
        self.assertEqual(100, counters['issued'])   # 10 Hz for ten seconds

    def test_a_request_still_in_flight_is_neither_a_success_nor_a_miss(self):
        ran = three_ue_topology(echo_deadline_ms={'131': 500.0})
        ran.advance(1000)
        counters = ran.echo_counters['131']
        self.assertEqual(10, counters['issued'])
        self.assertEqual(6, counters['eligible'])   # only those whose 500 ms passed

    def test_the_counters_do_not_depend_on_the_poll_cadence(self):
        one_step = three_ue_topology(echo_deadline_ms=50.0)
        many_steps = three_ue_topology(echo_deadline_ms=50.0)
        one_step.advance(30000)
        for _ in range(12):
            many_steps.advance(2500)
        self.assertEqual(one_step.echo_counters, many_steps.echo_counters)

    def test_the_same_seed_gives_the_same_counters(self):
        first = three_ue_topology(echo_deadline_ms=50.0, seed=7)
        second = three_ue_topology(echo_deadline_ms=50.0, seed=7)
        other = three_ue_topology(echo_deadline_ms=50.0, seed=8)
        for ran in (first, second, other):
            ran.advance(30000)
        self.assertEqual(first.echo_counters, second.echo_counters)
        self.assertNotEqual(first.echo_counters, other.echo_counters)

    def test_more_share_completes_more_requests(self):
        shared = three_ue_topology(echo_deadline_ms={'131': 20.0})
        alone = three_ue_topology(echo_deadline_ms={'131': 20.0})
        alone.apply('servingCell', '132', '87654321')
        shared.advance(30000)
        alone.advance(30000)
        self.assertGreater(alone.echo_ratio('131'), shared.echo_ratio('131'))

    def test_a_request_issued_in_the_handover_transient_never_answers(self):
        ran = three_ue_topology(echo_deadline_ms={'131': 50.0})
        ran.advance(5000)
        before = dict(ran.echo_counters['131'])
        ran.apply('servingCell', '131', '87654321')
        ran.advance(5000)                       # 2000 ms of it is the transient
        after = ran.echo_counters['131']
        issued_in_transient = 20                # 10 Hz for the 2000 ms transient
        self.assertEqual(before['eligible'] + 50, after['eligible'])
        self.assertLessEqual(after['completed'] - before['completed'],
                             50 - issued_in_transient)

    def test_an_unknown_ue_or_a_zero_deadline_is_refused(self):
        ran = three_ue_topology()
        with self.assertRaises(ValueError):
            ran.enable_tagged_echo({'999': 50.0})
        with self.assertRaises(ValueError):
            ran.enable_tagged_echo(0.0)

    def test_the_cumulative_ratio_is_none_until_something_is_eligible(self):
        ran = three_ue_topology(echo_deadline_ms={'131': 50.0})
        self.assertIsNone(ran.echo_ratio('131'))
        self.assertIsNone(ran.echo_ratio('132'))
        ran.advance(5000)
        self.assertIsNotNone(ran.echo_ratio('131'))


if __name__ == '__main__':
    unittest.main()
