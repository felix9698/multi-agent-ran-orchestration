"""P1 은 목표 id 도 가중 스칼라도 아니다.

2026-09-23 결정(시나리오 작성자): 비교 키는 **기록된 P1 사전식 소유자 순서**다.
같은 `target.cost` 가 같은 P1 선호를 뜻하지 않는다 -- 실제 144목표 도메인에서
같은 가중 cost 가 서로 다른 P1 순위를 갖는 경우가 **20건** 있다(cost=4.0 이 순위
6·13·36).  그리고 보호된 차원(steps 0)은 소유자 평균에 0 을 더하지 않는다.
"""
import json
import unittest
from fractions import Fraction
from pathlib import Path

from assurance.coordination.preference import (
    P1_RANK_CEILING, owner_concessions, p1_vector, p_rank, preference_table, rank_scales,
)

ORDER = ["operator-gnb2", "ue1-video", "ue2-map", "ue3-incumbent"]
AUTH = {
    "I0c.r1": {"owner": "operator-gnb2", "steps": 1},
    "I1g.r1": {"owner": "ue1-video", "steps": 2},
    "I1d.r1": {"owner": "ue1-video", "steps": 0, "deadlineSteps": 0},
    "I2g.r1": {"owner": "ue2-map", "steps": 0},
    "I2d.r1": {"owner": "ue2-map", "steps": 2, "deadlineSteps": 0},
    "I3g.r1": {"owner": "ue3-incumbent", "steps": 1},
    "I3d.r1": {"owner": "ue3-incumbent", "steps": 1, "deadlineSteps": 1},
}


def _target(**concession):
    base = {k: 0.0 for k in ("I0c.r1", "I1g.r1", "I1d.r1", "I1d.r1#deadline",
                             "I2g.r1", "I2d.r1", "I2d.r1#deadline",
                             "I3g.r1", "I3d.r1", "I3d.r1#deadline")}
    base.update(concession)
    return {"targetId": "X", "concession": base, "cost": 0.0}


class TestOwnerMeans(unittest.TestCase):
    def test_a_protected_dimension_does_not_dilute_the_mean(self):
        """ue1 의 마감은 steps 0 이므로 평균에 0 을 더하지 않는다."""
        means = owner_concessions(_target(**{"I1g.r1": 1.0}), AUTH)
        self.assertEqual(means["ue1-video"], Fraction(1),
                         '보호 차원을 세면 1/3 이 되어 순서가 바뀐다')

    def test_ue3_averages_its_three_adjustable_dimensions(self):
        means = owner_concessions(_target(**{"I3g.r1": 1.0}), AUTH)
        self.assertEqual(means["ue3-incumbent"], Fraction(1, 3))

    def test_the_mean_is_exact_not_floating_point(self):
        means = owner_concessions(
            _target(**{"I3g.r1": 1.0, "I3d.r1": 1.0, "I3d.r1#deadline": 1.0}), AUTH)
        self.assertEqual(means["ue3-incumbent"], Fraction(1), '세 삼분의 일은 정확히 1')


class TestRank(unittest.TestCase):
    def test_the_original_target_is_rank_zero(self):
        self.assertEqual(p_rank(_target(), AUTH, ORDER), 0)

    def test_the_fully_relaxed_target_is_the_ceiling(self):
        full = _target(**{"I0c.r1": 1.0, "I1g.r1": 1.0, "I2d.r1": 1.0,
                          "I3g.r1": 1.0, "I3d.r1": 1.0, "I3d.r1#deadline": 1.0})
        self.assertEqual(p_rank(full, AUTH, ORDER), P1_RANK_CEILING)

    def test_the_operator_outranks_every_tenant(self):
        """사전식이므로 사업자가 1 칸 양보하는 것이 세 테넌트 전부보다 나쁘다."""
        operator = p_rank(_target(**{"I0c.r1": 1.0}), AUTH, ORDER)
        tenants = p_rank(_target(**{"I1g.r1": 1.0, "I2d.r1": 1.0, "I3g.r1": 1.0,
                                    "I3d.r1": 1.0, "I3d.r1#deadline": 1.0}), AUTH, ORDER)
        self.assertGreater(operator, tenants)

    # 2026-09-25 v4.7: a workload the frozen table does not match no longer gets *no*
    # rank.  It gets the generic mixed-radix encoding (``generic_scales``), whose order
    # agreement with the lexicographic tuple is checked pair-by-pair in
    # tests/test_concession_evaluator.py::GenericRankScales -- the multipliers are
    # derived, not invented.  The frozen v4.5 table is still what its own grid gets.
    def test_an_unmatched_workload_gets_the_generic_encoding(self):
        from assurance.coordination.preference import generic_scales
        thin = {"I0c.r1": {"owner": "operator-gnb2", "steps": 1}}
        self.assertEqual(rank_scales(thin, ORDER), generic_scales(thin, ORDER))
        self.assertEqual(0, p_rank(_target(), thin, ORDER))

    def test_a_reordered_owner_priority_gets_the_generic_encoding(self):
        from assurance.coordination.preference import generic_scales
        reordered = list(reversed(ORDER))
        self.assertEqual(rank_scales(AUTH, reordered), generic_scales(AUTH, reordered))


class TestAgainstTheRecordedDomain(unittest.TestCase):
    """기록된 실제 144목표로 검증한다 -- 내가 상상한 모양이 아니라."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.episode = None
        for path in sorted((root / '.orca/drops/V44_SECTION5_DATA/raw/episodes').glob('*.json')):
            try:
                body = json.load(open(path))
            except (OSError, ValueError):
                continue
            contract = body.get('T') or {}
            if 1 + len(contract.get('alternatives') or []) == 144:
                cls.episode = body
                break

    def setUp(self):
        if self.episode is None:
            self.skipTest('no recorded 144-target episode in this checkout')

    def _table(self):
        contract = self.episode['T']
        return preference_table([contract['t0']] + list(contract['alternatives']),
                                contract['authorization'],
                                contract['preference']['ownerPriority'])

    def test_the_rank_agrees_with_the_tuple_order_on_every_pair(self):
        self.assertTrue(self._table()['rankAgreesWithTupleOrder'])

    def test_the_domain_has_144_targets_in_72_preference_classes(self):
        table = self._table()
        self.assertEqual((table['targets'], table['classes']), (144, 72))

    def test_equal_weighted_cost_can_differ_in_p1(self):
        """검토가 경고한 것: cost 를 P1 대신 쓰면 안 된다."""
        import collections
        by_cost = collections.defaultdict(set)
        for row in self._table()['rows']:
            by_cost[row['weightedCost']].add(row['pRank'])
        clashes = {cost: sorted(ranks) for cost, ranks in by_cost.items() if len(ranks) > 1}
        self.assertGreater(len(clashes), 0,
                           'cost 와 P1 이 일치하면 이 결정의 근거가 없어진다')

    def test_every_rank_from_0_to_71_occurs(self):
        ranks = {row['pRank'] for row in self._table()['rows']}
        self.assertEqual(ranks, set(range(72)))


if __name__ == '__main__':
    unittest.main()
