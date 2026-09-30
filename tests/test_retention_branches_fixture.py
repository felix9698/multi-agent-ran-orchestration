"""§6 구현 반환 패키지 — 유지 상태기계의 일곱 분기를 결정론적으로 덮는다.

2026-09-23 검토 §6 가 요구한 대표 실행 기록의 분기들이다.  "Deterministic fixtures can
cover branches that are inconvenient to reproduce on air" 에 따라 여기서 덮고,
`.orca/drops/V46_IMPLEMENTATION_RETURN/retention_branches.json` 으로 내보낸다.

한 판을 시행 순서대로 흉내낸다 -- 상태(이전 최고, 기준선)가 시행 사이에 이어지는 것이
이 상태기계의 요점이므로, 분기를 하나씩 독립 시험하면 그 이어짐을 못 본다.
"""
import json
import unittest
from pathlib import Path

from assurance.coordination.retention import (
    NO_ATTAINMENT, decide_retention, next_baseline, transition_scope,
)

OUT = (Path(__file__).resolve().parents[1]
       / '.orca/drops/V46_IMPLEMENTATION_RETURN/retention_branches.json')
C0 = {'pfWeight@ue1': '1.0', 'pfWeight@ue2': '1.0', 'dlPrbCap@ue3': '0',
      'servingCell@ue1': '87654321'}


class Episode:
    """이전 최고와 기준선을 시행 사이에 이어 가는 최소 모형."""

    def __init__(self):
        self.previous_best = NO_ATTAINMENT
        self.baseline = dict(C0)
        self.rows = []

    def trial(self, name, applied, *, best, valid=True,
              confirmed=True, recovery=False):
        before = dict(self.baseline)
        decision = decide_retention(
            trial_is_valid=valid, current_best=best, previous_best=self.previous_best,
            configuration_confirmed=confirmed, recovery_required=recovery)
        row = dict(decision.to_record())
        row.update(branch=name, applied=dict(applied), baselineBefore=before,
                   transitionScope=list(transition_scope(before, applied)))
        self.baseline = next_baseline(decision, applied, before)
        row['baselineAfter'] = dict(self.baseline)
        # 역사 최고는 **유효 관측**에서 갱신한다 -- 유지 여부와 무관하다.
        if valid and best < self.previous_best:
            self.previous_best = best
        row['previousBestAfter'] = (None if self.previous_best == NO_ATTAINMENT
                                    else self.previous_best)
        self.rows.append(row)
        return row


class TestTheSevenBranches(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        e = Episode()
        pf1 = dict(C0, **{'pfWeight@ue1': '4.0'})
        pf12 = dict(C0, **{'pfWeight@ue1': '4.0', 'pfWeight@ue2': '4.0'})
        cap = dict(C0, **{'dlPrbCap@ue3': '6'})
        steer = dict(C0, **{'servingCell@ue1': '12345678'})

        # 1) T 밖의 목표를 달성 -- 순위 12 는 이 방식이 T 에 적지 않은 열이다.
        cls.outside_t = e.trial('target-attained-outside-T', pf1, best=12)
        # 2) 첫 유한 달성 = 유지
        cls.first = cls.outside_t
        # 3) 더 나은 P1 = 유지, 기준선이 옮겨간다
        cls.better = e.trial('strictly-better-p1', pf12, best=6)
        # 4) 같은 P1, 다른 요구 벡터 = 유지
        cls.equal = e.trial('equal-p1-different-vector', cap, best=6)
        # 5) 더 나쁜 P1 = 복원
        cls.worse = e.trial('worse-p1-restores', steer, best=20)
        # 6) 유효하지만 아무 목표도 못 맞춤 = 복원
        cls.nothing = e.trial('valid-but-no-target', pf1, best=NO_ATTAINMENT)
        # 7) 유효 판정 뒤 운영 상태 미확정 = 역사는 남고 유지는 안 된다
        cls.unresolved = e.trial('attained-then-unresolved', pf12, best=4,
                                 confirmed=True, recovery=True)
        # 8) 무효 관측 = 성능 판정 자체를 하지 않는다
        cls.invalid = e.trial('invalid-observation', steer, best=0, valid=False)
        cls.episode = e
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(
            {"note": "deterministic fixture for the branches the 2026-09-23 review "
                     "asked to see; state is carried across trials in order",
             "referenceConfiguration": C0, "trials": e.rows}, ensure_ascii=False, indent=1))

    def test_a_target_outside_t_is_credited(self):
        self.assertTrue(self.outside_t['retain'])
        self.assertEqual(self.outside_t['reason'], 'first-attainment')

    def test_a_better_result_moves_the_baseline(self):
        self.assertTrue(self.better['retain'])
        self.assertEqual(self.better['reason'], 'improved')
        self.assertEqual(self.better['baselineAfter'], self.better['applied'])

    def test_an_equal_result_retains_and_the_transition_is_measured(self):
        self.assertTrue(self.equal['retain'])
        self.assertEqual(self.equal['reason'], 'equal-preference')
        # 유지된 pfWeight 둘을 되돌리고 cap 을 켠다 = 3개 축의 전환
        self.assertEqual(self.equal['transitionScope'],
                         ['dlPrbCap@ue3', 'pfWeight@ue1', 'pfWeight@ue2'])

    def test_a_worse_result_recovers_to_the_retained_configuration(self):
        """C0 가 아니라 **직전에 유지된 설정**으로 복원한다 -- 결정 §3.3."""
        self.assertFalse(self.worse['retain'])
        self.assertEqual(self.worse['baselineAfter'], self.worse['baselineBefore'])
        self.assertEqual(self.worse['baselineAfter']['dlPrbCap@ue3'], '6',
                         'C0 로 되돌아가면 유지 규칙이 없는 것과 같다')
        self.assertNotEqual(self.worse['baselineAfter'], C0)

    def test_a_valid_window_attaining_nothing_restores(self):
        self.assertFalse(self.nothing['retain'])
        self.assertEqual(self.nothing['reason'], 'no-target-attained')

    def test_history_survives_an_unresolved_operating_state(self):
        self.assertFalse(self.unresolved['retain'])
        self.assertTrue(self.unresolved['performanceQualifies'])
        self.assertEqual(self.unresolved['reason'], 'recovery-outstanding')
        self.assertEqual(self.unresolved['previousBestAfter'], 4,
                         '달성은 역사에 남는다 -- 유지가 안 된 것과 별개다')

    def test_an_invalid_observation_adds_no_attainment(self):
        self.assertFalse(self.invalid['retain'])
        self.assertFalse(self.invalid['performanceQualifies'])
        self.assertEqual(self.episode.previous_best, 4,
                         '무효 관측의 순위 0 이 역사를 오염시키면 안 된다')

    def test_the_fixture_is_exported_for_the_return_package(self):
        body = json.loads(OUT.read_text())
        self.assertEqual(len(body['trials']), 7)
        self.assertEqual([row['branch'] for row in body['trials']][:2],
                         ['target-attained-outside-T', 'strictly-better-p1'])


if __name__ == '__main__':
    unittest.main()
