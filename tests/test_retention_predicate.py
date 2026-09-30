"""유지 술어 — 결정문의 표를 그대로 못 박는다.

2026-09-23 결정(시나리오 작성자 §3).  이전 런타임은 **시행 후 초기화**였다: 목표를
충족한 제어 시행 24건 전부가 C0 로 되돌려졌고, 탐색이 끝난 뒤 별도 재적용만 남길 수
있었다.  그건 다른 실험이다.
"""
import unittest

from assurance.coordination.retention import (
    NO_ATTAINMENT, decide_retention, next_baseline, transition_scope,
)

OK = dict(trial_is_valid=True, configuration_confirmed=True, recovery_required=False)


class TestTheDecisionTable(unittest.TestCase):
    """결정문의 표 여섯 줄."""

    def test_first_finite_attainment_retains(self):
        d = decide_retention(current_best=12, previous_best=NO_ATTAINMENT, **OK)
        self.assertTrue(d.retain)
        self.assertEqual(d.reason, 'first-attainment')

    def test_a_strictly_better_p1_retains(self):
        d = decide_retention(current_best=6, previous_best=12, **OK)
        self.assertTrue(d.retain)
        self.assertEqual(d.reason, 'improved')

    def test_an_equal_p1_retains_even_with_a_different_requirement_vector(self):
        """소유자는 두 목표 사이에 무차별하고, 뒤에 적용된 것이 이미 걸려 있다."""
        d = decide_retention(current_best=12, previous_best=12, **OK)
        self.assertTrue(d.retain)
        self.assertEqual(d.reason, 'equal-preference')

    def test_a_worse_p1_restores(self):
        d = decide_retention(current_best=20, previous_best=12, **OK)
        self.assertFalse(d.retain)
        self.assertEqual(d.reason, 'worse-than-previous-best')

    def test_a_valid_observation_attaining_nothing_restores(self):
        d = decide_retention(current_best=NO_ATTAINMENT, previous_best=12, **OK)
        self.assertFalse(d.retain)
        self.assertEqual(d.reason, 'no-target-attained')

    def test_an_invalid_observation_never_retains(self):
        d = decide_retention(current_best=0, previous_best=NO_ATTAINMENT,
                             trial_is_valid=False, configuration_confirmed=True,
                             recovery_required=False)
        self.assertFalse(d.retain)
        self.assertEqual(d.reason, 'observation-invalid')
        self.assertFalse(d.qualifies, '무효 관측은 성능 판정 자체를 하지 않는다')


class TestTheFiniteCondition(unittest.TestCase):
    def test_infinity_against_infinity_must_not_retain(self):
        """`inf <= inf` 이 참이므로 유한 조건 없이는 실패한 설정이 유지된다."""
        d = decide_retention(current_best=NO_ATTAINMENT, previous_best=NO_ATTAINMENT, **OK)
        self.assertFalse(d.retain)

    def test_rank_zero_is_an_attainment_not_a_falsy_miss(self):
        """T0 의 순위는 0 이다 -- 0 을 '없음' 으로 읽으면 최고 결과가 버려진다."""
        d = decide_retention(current_best=0, previous_best=NO_ATTAINMENT, **OK)
        self.assertTrue(d.retain)


class TestExecutionState(unittest.TestCase):
    def test_performance_can_qualify_while_retention_does_not(self):
        """역사적 달성과 성공한 유지는 따로 보고된다."""
        d = decide_retention(current_best=6, previous_best=12, trial_is_valid=True,
                             configuration_confirmed=False, recovery_required=False)
        self.assertFalse(d.retain)
        self.assertTrue(d.qualifies, '성능은 자격을 얻었고 적용 상태만 미확인이다')
        self.assertEqual(d.reason, 'applied-state-unconfirmed')

    def test_an_outstanding_recovery_blocks_retention(self):
        d = decide_retention(current_best=6, previous_best=12, trial_is_valid=True,
                             configuration_confirmed=True, recovery_required=True)
        self.assertFalse(d.retain)
        self.assertTrue(d.qualifies)
        self.assertEqual(d.reason, 'recovery-outstanding')

    def test_the_record_separates_the_two(self):
        d = decide_retention(current_best=6, previous_best=12, trial_is_valid=True,
                             configuration_confirmed=True, recovery_required=True)
        record = d.to_record()
        self.assertEqual((record['retain'], record['performanceQualifies']), (False, True))
        self.assertEqual((record['currentBestPRank'], record['previousBestPRank']), (6, 12))

    def test_no_attainment_is_written_as_null_not_infinity(self):
        record = decide_retention(current_best=NO_ATTAINMENT,
                                  previous_best=NO_ATTAINMENT, **OK).to_record()
        self.assertIsNone(record['currentBestPRank'])


class TestBaselineTransition(unittest.TestCase):
    def test_a_retained_configuration_becomes_the_next_baseline(self):
        d = decide_retention(current_best=6, previous_best=12, **OK)
        applied = {'pfWeight@ue1': '4.0', 'dlPrbCap@ue2': '0'}
        self.assertEqual(next_baseline(d, applied, {'pfWeight@ue1': '1.0'}), applied)

    def test_a_restored_trial_carries_the_old_baseline_forward(self):
        d = decide_retention(current_best=20, previous_best=12, **OK)
        previous = {'pfWeight@ue1': '1.0'}
        self.assertEqual(next_baseline(d, {'pfWeight@ue1': '4.0'}, previous), previous)

    def test_the_transition_scope_counts_resets_as_changes(self):
        """유지된 축을 되돌리는 것도 변경이다 -- 4개 한도는 전환에 대해 재야 한다."""
        applied = {'pfWeight@ue1': '4.0', 'pfWeight@ue2': '4.0', 'dlPrbCap@ue3': '6'}
        target = {'pfWeight@ue1': '1.0', 'pfWeight@ue2': '1.0', 'dlPrbCap@ue3': '0',
                  'servingCell@ue1': '12345678'}
        self.assertEqual(transition_scope(applied, target),
                         ('dlPrbCap@ue3', 'pfWeight@ue1', 'pfWeight@ue2', 'servingCell@ue1'))

    def test_an_unchanged_axis_is_not_in_the_transition(self):
        same = {'pfWeight@ue1': '4.0'}
        self.assertEqual(transition_scope(same, same), ())


if __name__ == '__main__':
    unittest.main()
