"""A supplementary A1 policy must stay valid through the live trial it serves."""
import unittest

from tools.liveconsole.build import supplementary_policy_window_ms


class ThePolicyOutlivesItsLiveTrial(unittest.TestCase):
    def test_attempt_30_would_not_have_expired_before_its_reread(self):
        # 2026-09-15 attempt 30: policy written at +0 s, COMMIT at +5.7 s, the
        # FINALIZE reread at +56 s; the old window (one hold) closed at +50.5 s.
        old_window_ms = 50500
        hold_ms, freshness_ms = 16500, 1000
        enforced_plus_deadline_ms = old_window_ms - hold_ms - freshness_ms
        window = supplementary_policy_window_ms(
            enforced_timeout_ms=enforced_plus_deadline_ms, r1_deadline_ms=0,
            hold_ms=hold_ms, freshness_bound_ms=freshness_ms)
        self.assertGreater(window, 56000 + 30000)   # the observed reread, with room to spare

    def test_attempt_33_whole_span_from_write_to_delete_answer_fits(self):
        # 2026-09-15 attempt 33: write 22:17:07.2, DELETE answered 22:18:40 (93 s),
        # which a 3x window (93.5 s, notAfter 22:18:40.67) lost by 0.6 s.
        window = supplementary_policy_window_ms(
            enforced_timeout_ms=33000, r1_deadline_ms=0, hold_ms=16500, freshness_bound_ms=1000)
        self.assertGreater(window, 93000 * 1.5)

    def test_the_window_still_scales_with_the_hold_rather_than_being_open_ended(self):
        short = supplementary_policy_window_ms(enforced_timeout_ms=0, r1_deadline_ms=0,
                                               hold_ms=1000, freshness_bound_ms=0)
        self.assertLess(short, 60000)


if __name__ == "__main__":
    unittest.main()


class ASettledPolicyOutlivesItsTrialUntilTheSittingEnds(unittest.TestCase):
    """2026-09-15 attempt 51: pfWeight settled at 00:55:43 was restored by expiry at
    00:58:38 while the sitting (ending 01:00:45) still reported it applied."""

    def test_the_window_reaches_the_service_horizon_plus_the_reserve(self):
        from tools.liveconsole.agent import held_policy_window_ms
        from tools.liveconsole.build import CONCLUDE_RESERVE_MS
        # ... plus one trial window, for a trial admitted just before the horizon.
        self.assertEqual(300_000 + 176_000 + CONCLUDE_RESERVE_MS,
                         held_policy_window_ms(176_000, now_ms=100_000, sitting_end_ms=400_000))

    def test_a_late_trial_keeps_its_own_window(self):
        from tools.liveconsole.agent import held_policy_window_ms
        self.assertEqual(176_000, held_policy_window_ms(176_000, now_ms=600_000,
                                                        sitting_end_ms=400_000))

    def test_a_policy_outlives_a_trial_started_just_before_the_horizon(self):
        # 2026-09-19 board 093728: trial 1's pfWeight expired at horizon + 10 s under trial 3.
        from tools.liveconsole.agent import held_policy_window_ms
        written_at, horizon, trial = 100_000, 480_000, 176_000
        expires = written_at + held_policy_window_ms(trial, now_ms=written_at,
                                                     sitting_end_ms=horizon)
        late_trial_end = (horizon - 1) + trial
        self.assertGreater(expires, late_trial_end)

    def test_without_a_horizon_the_trial_window_stands(self):
        from tools.liveconsole.agent import held_policy_window_ms
        self.assertEqual(176_000, held_policy_window_ms(176_000, now_ms=0, sitting_end_ms=None))
