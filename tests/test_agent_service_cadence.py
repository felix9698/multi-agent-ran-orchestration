"""Service-only holds use the same drift compensation as Kernel observations."""
from datetime import datetime

from tests.test_agent_sitting import AgentSittingFixture, METHOD_DETERMINISTIC


class DelayedObserver:
    def __init__(self, inner, clock, delay_ms, *, missing=False):
        self.inner = inner
        self.clock = clock
        self.delay_ms = delay_ms
        self.missing = missing
        self.calls = 0

    def sample(self):
        self.calls += 1
        self.clock.sleep_ms(self.delay_ms)
        return {} if self.missing else self.inner.sample()


class ServiceCadenceTests(AgentSittingFixture):
    def sitting(self, *, delay_ms=900, missing=False, horizon_ms=None):
        sitting = self.build(
            method=METHOD_DETERMINISTIC, budget_trials=1,
            horizon_ms=horizon_ms,
            settings={"observation": {
                "dlGoodputMbps": {"settleMs": 1000, "windowMs": 5000,
                                  "minCoverage": 0.8, "validityMs": 60000},
                "servingCell": {"settleMs": 1000, "windowMs": 5000,
                                "minCoverage": 0.8, "validityMs": 60000}}})
        sitting.observer = DelayedObserver(
            sitting.observer, sitting.clock, delay_ms, missing=missing)
        sitting.started_ms = sitting.clock.monotonic_ms()
        sitting.t0_monotonic_ms = sitting.started_ms
        self.assertEqual(1000, sitting._cadence_ms)
        return sitting

    def assert_actual_cadence(self, sitting):
        stamps = [datetime.fromisoformat(row['t'].replace('Z', '+00:00')).timestamp()
                  for row in sitting.service_trace]
        self.assertEqual(6, len(stamps))
        for before, after in zip(stamps, stamps[1:]):
            self.assertAlmostEqual(1.0, after - before, places=6)
        self.assertEqual(sitting.clock.now(), sitting.service_trace[-1]['t'])
        self.assertAlmostEqual(6900, sitting._elapsed_ms())

    def test_initial_window_does_not_add_collection_latency_to_every_tick(self):
        sitting = self.sitting()
        trial = sitting._initial_measurement()
        self.assert_actual_cadence(sitting)
        self.assertEqual(0, trial.trial_index)
        self.assertFalse(sitting.trial_flags[0]['counted'])
        self.assertEqual(1.0, trial.window['coverage']['dlGoodputMbps@131'])
        self.assertIn('dlGoodputMbps@131', trial.kpis)

    def test_reobservation_does_not_add_collection_latency_to_every_tick(self):
        sitting = self.sitting()
        observation = sitting._reobserve()
        self.assert_actual_cadence(sitting)
        self.assertIn('dlGoodputMbps@131', observation.kpis)
        self.assertEqual([], sitting.grid.trials)
        self.assertEqual(1.0, sitting.preflight['reobservations'][-1]
                         ['coverage']['dlGoodputMbps@131'])

    def test_post_search_service_horizon_uses_compensated_cadence(self):
        sitting = self.sitting(horizon_ms=6000)
        sitting._observe_to_horizon(1000)
        self.assert_actual_cadence(sitting)
        self.assertEqual(6, sitting.preflight['horizonObservation']['samples'])
        self.assertEqual([], sitting.grid.trials)

    def test_source_slower_than_window_still_has_insufficient_coverage(self):
        sitting = self.sitting(delay_ms=6000)
        trial = sitting._initial_measurement()
        self.assertEqual(6, sitting.observer.calls)
        self.assertEqual(0.2, trial.window['coverage']['dlGoodputMbps@131'])
        self.assertNotIn('dlGoodputMbps@131', trial.kpis)
        self.assertEqual('UNKNOWN', trial.cell('T0'))
        self.assertEqual(sitting.clock.now(), sitting.service_trace[-1]['t'])

    def test_missing_source_does_not_gain_fabricated_samples(self):
        sitting = self.sitting(missing=True)
        trial = sitting._initial_measurement()
        self.assertEqual(6, sitting.observer.calls)
        self.assertTrue(all(not row['kpis'] for row in sitting.service_trace))
        self.assertEqual({}, trial.kpis)
        self.assertEqual('UNKNOWN', trial.cell('T0'))
