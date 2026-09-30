"""파생값은 자기 입력의 유효성을 물려받는다.

2026-09-23 (시나리오 검토 §1): `cellGoodputMbps@<nci>` 는 그 창의 `servingCell@<ue>` 로
UE 를 셀에 묶어 만든 합계인데, 두 KPI 의 `minCoverage` 가 달라(소속 1.0, 합계 0.8)
표본 하나만 빠져도 소속은 UNKNOWN 이 되고 합계는 통과했다.  v4.4 전수 144 시행 중
**21건(15%)** 이 "셀 총량 발행 · 소속 UNKNOWN" 이었고, 그 판의 사업자 요구 판정은
기록이 모른다고 말하는 소속에 기대고 있었다.
"""
import unittest

from assurance.coordination.tc import UNKNOWN
from tools.liveconsole.kpi_observer import CELL_GOODPUT_KPI, SERVING_CELL_KPI


def _sweep(aggregated, audit=None):
    """`_aggregate_window` 의 해당 구간과 같은 규칙 (그 메서드는 sitting 상태를 요구한다)."""
    audit = {} if audit is None else audit
    stale = sorted(k for k, v in aggregated.items()
                   if v == UNKNOWN and k.startswith(f"{SERVING_CELL_KPI}@"))
    if stale:
        for key in [k for k in aggregated if k.startswith(f"{CELL_GOODPUT_KPI}@")]:
            if aggregated[key] == UNKNOWN:
                continue
            aggregated[key] = UNKNOWN
            audit.setdefault(key, {}).update(
                valid=False, invalidReason="serving-cell-membership-unknown",
                membershipUnknown=stale)
    return aggregated, audit


class TestCellTotalInheritsMembershipValidity(unittest.TestCase):
    def test_an_unknown_membership_withdraws_the_cell_total(self):
        agg, audit = _sweep({f'{SERVING_CELL_KPI}@ue1': UNKNOWN,
                             f'{SERVING_CELL_KPI}@ue3': '87654321',
                             f'{CELL_GOODPUT_KPI}@87654321': 16.2,
                             'dlGoodputMbps@ue1': 8.7})
        self.assertEqual(agg[f'{CELL_GOODPUT_KPI}@87654321'], UNKNOWN)
        self.assertEqual(audit[f'{CELL_GOODPUT_KPI}@87654321']['invalidReason'],
                         'serving-cell-membership-unknown')
        self.assertEqual(audit[f'{CELL_GOODPUT_KPI}@87654321']['membershipUnknown'],
                         [f'{SERVING_CELL_KPI}@ue1'])

    def test_every_cell_total_is_withdrawn_not_only_the_named_one(self):
        """소속 하나가 불명이면 어느 셀 합계가 오염됐는지 알 수 없다."""
        agg, _ = _sweep({f'{SERVING_CELL_KPI}@ue2': UNKNOWN,
                         f'{CELL_GOODPUT_KPI}@12345678': 9.5,
                         f'{CELL_GOODPUT_KPI}@87654321': 16.2})
        self.assertEqual(agg[f'{CELL_GOODPUT_KPI}@12345678'], UNKNOWN)
        self.assertEqual(agg[f'{CELL_GOODPUT_KPI}@87654321'], UNKNOWN)

    def test_a_complete_membership_leaves_the_total_alone(self):
        agg, audit = _sweep({f'{SERVING_CELL_KPI}@ue1': '87654321',
                             f'{SERVING_CELL_KPI}@ue3': '87654321',
                             f'{CELL_GOODPUT_KPI}@87654321': 16.2})
        self.assertEqual(agg[f'{CELL_GOODPUT_KPI}@87654321'], 16.2)
        self.assertEqual(audit, {})

    def test_the_per_ue_goodput_is_not_withdrawn(self):
        """소속을 모르면 셀 합계는 말할 수 없지만 그 UE 의 처리량은 여전히 참이다."""
        agg, _ = _sweep({f'{SERVING_CELL_KPI}@ue1': UNKNOWN,
                         f'{CELL_GOODPUT_KPI}@87654321': 16.2,
                         'dlGoodputMbps@ue1': 8.7, 'dlGoodputMbps@ue3': 7.5})
        self.assertEqual(agg['dlGoodputMbps@ue1'], 8.7)
        self.assertEqual(agg['dlGoodputMbps@ue3'], 7.5)

    def test_an_already_unknown_total_is_not_re_audited(self):
        agg, audit = _sweep({f'{SERVING_CELL_KPI}@ue1': UNKNOWN,
                             f'{CELL_GOODPUT_KPI}@87654321': UNKNOWN})
        self.assertEqual(agg[f'{CELL_GOODPUT_KPI}@87654321'], UNKNOWN)
        self.assertEqual(audit, {})

    def test_the_sitting_carries_the_same_sweep(self):
        """시험용 복제가 아니라 실제 코드에 들어 있는지 확인한다."""
        import inspect
        from tools.liveconsole.agent import AgentSitting
        source = inspect.getsource(AgentSitting._aggregate_window)
        self.assertIn('serving-cell-membership-unknown', source)
        self.assertIn('stale_membership', source)


if __name__ == '__main__':
    unittest.main()
