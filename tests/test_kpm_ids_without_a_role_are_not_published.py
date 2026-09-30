"""역할에 안 붙은 KPM id 는 소속으로 내지 않는다 (2026-09-23 라이브 판).

`formal38guarded-20260923T064157` 의 기준 관측: `servingCell@ue1/ue2/ue3` 적용범위는 전부
1.0 인데 같은 창에 `servingCell@530/533/534`(원번호, 0.6/0.4/0.07)가 함께 실렸다.
재등록한 UE 의 옛 번호와, 신원 해석기가 아직 역할에 안 붙인 새 번호다.

그 원번호들이:
1. `cell_goodput_totals` 의 소속 키 집합 == goodput 키 집합 조건을 깨 그 표본의 셀 합을 지웠고
2. 창 집계에서 UNKNOWN 이 되어 소속 불명 청소가 **모든** 셀 KPI 를 거뒀다 →
   사업자 요구 I0c 빠짐 → 기준 관측 무효 → 초기상태 `unknown`.
"""
import unittest

from tools.liveconsole.agent import role_serving_cells
from tools.liveconsole.kpi_observer import cell_goodput_totals


ROLES = {"ue1": 515, "ue2": 506, "ue3": 516}


class OnlyRolesArePublished(unittest.TestCase):
    def test_a_stale_id_beside_the_role_ids_is_dropped(self):
        raw = {"515": "87654321", "506": "12345678", "516": "87654321",
               "530": "87654321"}                       # 재등록한 UE 의 옛 번호
        self.assertEqual({"ue1": "87654321", "ue2": "12345678", "ue3": "87654321"},
                         role_serving_cells(raw, list(ROLES), ROLES.get))

    def test_a_role_whose_id_is_in_transition_is_simply_absent(self):
        """ue1 의 새 번호가 아직 역할에 안 붙었다 -- ue1 소속은 **모른다**, 0 이 아니다."""
        raw = {"540": "87654321", "506": "12345678", "516": "87654321"}
        self.assertEqual({"ue2": "12345678", "ue3": "87654321"},
                         role_serving_cells(raw, list(ROLES), ROLES.get))

    def test_numbered_labels_resolve_to_themselves(self):
        """번호 지정 판(hardware-free)은 라벨이 곧 번호다 -- 무변화."""
        amf_of = lambda ue: int(ue)
        self.assertEqual({"131": "1", "132": "2"},
                         role_serving_cells({"131": "1", "132": "2"}, ["131", "132"], amf_of))

    def test_the_cell_total_survives_once_the_stale_id_is_gone(self):
        """원번호가 섞이면 합이 통째로 사라진다 -- 그것이 적용범위 0.53~0.73 의 한 원인."""
        goodput = {"dlGoodputMbps@ue1": 5.0, "dlGoodputMbps@ue2": 4.0, "dlGoodputMbps@ue3": 6.0}
        leaked = {**goodput, "servingCell@ue1": "87654321", "servingCell@ue2": "12345678",
                  "servingCell@ue3": "87654321", "servingCell@530": "87654321"}
        self.assertEqual({}, cell_goodput_totals(leaked), "예전 동작: 원번호 하나가 합을 지웠다")
        cells = role_serving_cells({"515": "87654321", "506": "12345678", "516": "87654321",
                                    "530": "87654321"}, list(ROLES), ROLES.get)
        clean = {**goodput, **{f"servingCell@{ue}": cell for ue, cell in cells.items()}}
        self.assertEqual({"cellGoodputMbps@87654321": 11.0, "cellGoodputMbps@12345678": 4.0},
                         cell_goodput_totals(clean))


if __name__ == "__main__":
    unittest.main()
