"""`cellGoodputMbps@<nci>` — 셀이 실어 나른 하향 총량.

새 계측이 아니라 이미 UE 안에서 재고 있는 goodput 을 `servingCell` 이 말해 주는 소속으로
묶어 더한 값이다.  그래서 **합계가 참이려면 기여할 UE 가 전부 읽혀야 한다** — 한 UE 가
빠진 채 더하면 셀 총량이 조용히 작아지고, 그 판은 "사업자 요구가 안 지켜졌다" 로 읽힌다.
측정 공백을 위반으로 세탁하는 것이므로 그때는 아무 셀도 내지 않는다(= UNKNOWN).
"""
import unittest
from pathlib import Path

from tools.liveconsole.kpi_observer import CELL_GOODPUT_KPI, cell_goodput_totals

GNB1, GNB2 = "12345678", "87654321"
BASE = {
    "dlGoodputMbps@ue1": 7.2, "servingCell@ue1": GNB2,
    "dlGoodputMbps@ue2": 9.7, "servingCell@ue2": GNB1,
    "dlGoodputMbps@ue3": 1.5, "servingCell@ue3": GNB2,
}


class TestCellGoodputTotals(unittest.TestCase):
    def test_sums_the_ues_of_each_cell(self):
        self.assertEqual(cell_goodput_totals(BASE), {
            f"{CELL_GOODPUT_KPI}@{GNB2}": 8.7,     # ue1 + ue3
            f"{CELL_GOODPUT_KPI}@{GNB1}": 9.7,     # ue2
        })

    def test_follows_a_steering_move(self):
        """조종이 셀을 옮기면 총량도 따라가야 사업자 요구가 조종과 맞물린다."""
        moved = dict(BASE, **{"servingCell@ue3": GNB1,
                              "dlGoodputMbps@ue3": 5.5, "dlGoodputMbps@ue2": 6.0})
        self.assertEqual(cell_goodput_totals(moved), {
            f"{CELL_GOODPUT_KPI}@{GNB2}": 7.2,     # ue1 만 남는다
            f"{CELL_GOODPUT_KPI}@{GNB1}": 11.5,    # ue2 + ue3
        })

    def test_a_missing_goodput_yields_no_total_at_all(self):
        thin = {k: v for k, v in BASE.items() if k != "dlGoodputMbps@ue3"}
        self.assertEqual(cell_goodput_totals(thin), {})

    def test_a_missing_serving_cell_yields_no_total_at_all(self):
        thin = {k: v for k, v in BASE.items() if k != "servingCell@ue3"}
        self.assertEqual(cell_goodput_totals(thin), {})

    def test_an_unknown_serving_cell_is_not_a_cell(self):
        self.assertEqual(cell_goodput_totals(dict(BASE, **{"servingCell@ue3": "UNKNOWN"})), {})

    def test_nothing_observed_is_not_zero(self):
        self.assertEqual(cell_goodput_totals({}), {})

    def test_a_cell_with_no_ue_gets_no_key_rather_than_zero(self):
        """UE 가 한 대도 없는 셀에 0 을 발행하면 관측하지 않은 값을 발행하는 것이다."""
        only_gnb2 = {"dlGoodputMbps@ue1": 7.2, "servingCell@ue1": GNB2,
                     "dlGoodputMbps@ue3": 1.5, "servingCell@ue3": GNB2}
        self.assertEqual(cell_goodput_totals(only_gnb2), {f"{CELL_GOODPUT_KPI}@{GNB2}": 8.7})


class TestCellScopedRequirement(unittest.TestCase):
    def test_a_cell_scope_makes_the_observed_key(self):
        from assurance.coordination.tc import KPI_CELL_GOODPUT, SUPPORTED_KPIS, kpi_key
        self.assertIn(KPI_CELL_GOODPUT, SUPPORTED_KPIS)
        self.assertEqual(kpi_key(KPI_CELL_GOODPUT, "cell@87654321"),
                         f"{CELL_GOODPUT_KPI}@87654321")

    def test_the_kpi_has_an_observation_rule(self):
        from assurance.coordination.intake import DEFAULT_OBSERVATION_RULES
        from assurance.coordination.tc import KPI_CELL_GOODPUT
        rule = DEFAULT_OBSERVATION_RULES[KPI_CELL_GOODPUT]
        self.assertEqual(rule.statistic, "mean")

    def test_an_intent_without_a_ue_needs_a_scope(self):
        """사업자 요구는 ueId 가 없다.  scope 가 있으면 통과하고, 둘 다 없으면 거절이다."""
        import main
        row = {"intentId": "I0c", "owner": "operator-gnb2",
               "requirement": {"reqId": "I0c.r1", "kpi": "cellGoodputMbps",
                               "scope": "cell@87654321", "op": ">=",
                               "value": 12.0, "unit": "Mbps"}}
        import json, tempfile, os
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as handle:
            json.dump({"intents": [row]}, handle)
            path = handle.name
        try:
            self.assertEqual(len(main._parse_intents_json(path, {})), 1)
            bare = dict(row, requirement=dict(row["requirement"]))
            bare["requirement"].pop("scope")
            with open(path, 'w') as handle:
                json.dump({"intents": [bare]}, handle)
            with self.assertRaises(ValueError):
                main._parse_intents_json(path, {})
        finally:
            os.unlink(path)


class TestCellScopedIntentReachesTheJointSitting(unittest.TestCase):
    """세 번째 층 — joint sitting 은 `ueId` 없는 인텐트를 거부해 왔다.

    2026-09-23 시도 360 이 `intent I0c names no ueId; a joint sitting addresses each
    intent to one UE` 로 거절됐다.  파서 두 곳(main._parse_intents_json,
    atomic_formal_run_guarded.pilot_intents)만 고쳐 놓고 여기를 빠뜨린 것이다 --
    **한 축은 여러 층에서 막힌다**.  셀 scope 요구는 UE 목록에서는 빠지고 계약에는 남아야 한다.
    """

    def _row(self, **over):
        from tools.liveconsole.agent import _row_from_record
        record = {"intentId": "I0c", "owner": "operator-gnb2",
                  "requirement": {"reqId": "I0c.r1", "kpi": "cellGoodputMbps",
                                  "scope": "cell@87654321", "op": ">=",
                                  "value": 12.0, "unit": "Mbps"}}
        record.update(over)
        return _row_from_record(record, 1, ("traffic-steering",))

    def test_a_cell_scoped_intent_is_accepted_without_a_ue(self):
        row = self._row()
        self.assertEqual(row.ue_id, "")
        self.assertEqual(row.cell_scope, "87654321")

    def test_an_intent_with_neither_ue_nor_cell_scope_is_still_refused(self):
        from tools.liveconsole.agent import LiveConsoleError
        with self.assertRaises(LiveConsoleError):
            self._row(requirement={"reqId": "Ix.r1", "kpi": "dlGoodputMbps",
                                   "op": ">=", "value": 5.0, "unit": "Mbps"})

    def test_a_ue_scoped_intent_reports_no_cell_scope(self):
        row = self._row(ueId="ue1",
                        requirement={"reqId": "I1g.r1", "kpi": "dlGoodputMbps",
                                     "scope": "ue@ue1", "op": ">=",
                                     "value": 7.5, "unit": "Mbps"})
        self.assertEqual(row.ue_id, "ue1")
        self.assertIsNone(row.cell_scope)

    def test_the_cell_row_is_left_out_of_the_per_ue_objective_specs(self):
        """UE 를 향한 O-RAN objective 가 아니므로 IntentSpec 에 들어가면 안 된다."""
        from tools.liveconsole.agent import _intent_specs
        rows = (self._row(),
                self._row(intentId="I1g", ueId="ue1",
                          requirement={"reqId": "I1g.r1", "kpi": "dlGoodputMbps",
                                       "scope": "ue@ue1", "op": ">=",
                                       "value": 7.5, "unit": "Mbps"}))
        specs = _intent_specs(rows, lambda ue: 87654321, (12345678, 87654321))
        self.assertEqual([s.intent_id for s in specs], ["I1g"])


class TestIntakeAcceptsACellScopedIntent(unittest.TestCase):
    """네 번째·다섯 번째 층 — intake 검사표와 러너의 --observe.

    2026-09-23 시도 361: 앞의 세 층을 통과한 뒤 검사표가 `[I0c] ueId -- Which UE does I0c
    apply to?` 를 물었고, **답 없는 질문 하나가 판 전체를 제출 전에 거절시켰다.**
    그 뒤 `observation.cellGoodputMbps` 가 같은 식으로 남았다.  한 축은 여러 층에서 막힌다.
    """

    def _intents(self):
        from assurance.coordination.tc import Intent
        return (Intent.from_record(
                    {"intentId": "I0c", "owner": "operator-gnb2",
                     "requirement": {"reqId": "I0c.r1", "kpi": "cellGoodputMbps",
                                     "scope": "cell@87654321", "op": ">=",
                                     "value": 12.0, "unit": "Mbps",
                                     "steps": 1, "bound": 10.0}}),
                Intent.from_record(
                    {"intentId": "I1g", "owner": "ue1-video", "ueId": "ue1",
                     "requirement": {"reqId": "I1g.r1", "kpi": "dlGoodputMbps",
                                     "op": ">=", "value": 7.5, "unit": "Mbps",
                                     "steps": 2, "bound": 6.0}}))

    def _settings(self, *, with_cell_rule=True):
        import main
        flags = ["dlGoodputMbps=1000:5000:60000", "servingCell=1500:1500:120000"]
        if with_cell_rule:
            flags.append("cellGoodputMbps=1000:5000:60000")
        return {"observation": main._parse_observe_flags(flags), "trialsK": 8,
                "deadlineMs": None, "horizonMs": None,
                "stopAfterRelaxedSuccess": False, "retention": 1}

    def test_no_question_is_asked_about_a_cell_scoped_intents_ue(self):
        from assurance.coordination.intake import IntakeChecklist
        asked = IntakeChecklist().check(self._intents(), settings=self._settings())
        self.assertEqual([q["field"] for q in asked], [])

    def test_a_ue_scoped_intent_without_a_ue_is_still_asked(self):
        from assurance.coordination.intake import IntakeChecklist
        from assurance.coordination.tc import Intent
        bare = Intent.from_record({"intentId": "Ix", "owner": "o",
                                   "requirement": {"reqId": "Ix.r1", "kpi": "dlGoodputMbps",
                                                   "op": ">=", "value": 5.0, "unit": "Mbps",
                                                   "steps": 0}})
        asked = IntakeChecklist().check((bare,), settings=self._settings())
        self.assertIn("ueId", [q["field"] for q in asked])

    def test_the_runner_supplies_the_cell_rule(self):
        """러너가 --observe 를 안 주면 검사표가 그 KPI 를 묻고 판이 죽는다."""
        from assurance.coordination.intake import IntakeChecklist
        asked = IntakeChecklist().check(self._intents(),
                                        settings=self._settings(with_cell_rule=False))
        self.assertIn("observation.cellGoodputMbps", [q["field"] for q in asked])
        runner = Path("experiment_results/ota-20260911/atomic_formal_run_guarded.py").read_text()
        # dlGoodputMbps 와 **같은** 창·유효기간이어야 한다 (번들 유효기간은 최솟값이 지배)
        self.assertIn("cellGoodputMbps=1000:15000:120000", runner)


class TestCompositionDoesNotTrustRowOrder(unittest.TestCase):
    """여섯 번째 층 — `rows[0]` 이 UE 를 향한다는 가정.

    2026-09-23 시도 363 이 제출까지 간 뒤 `identities[rows[0].ue_id]` 에서 `KeyError: ''`
    로 죽었다.  코퍼스의 첫 인텐트가 셀 사업자 요구였기 때문이다.  **행의 순서는 오너가
    코퍼스를 어떻게 적었는지일 뿐**이므로 거기에 신원을 걸면 안 된다.
    """

    def _rows(self, cell_first: bool):
        from tools.liveconsole.agent import _row_from_record
        cell = {"intentId": "I0c", "owner": "operator-gnb2",
                "requirement": {"reqId": "I0c.r1", "kpi": "cellGoodputMbps",
                                "scope": "cell@87654321", "op": ">=",
                                "value": 12.0, "unit": "Mbps", "steps": 1, "bound": 10.0}}
        ue = {"intentId": "I1g", "owner": "ue1-video", "ueId": "ue1",
              "requirement": {"reqId": "I1g.r1", "kpi": "dlGoodputMbps",
                              "op": ">=", "value": 7.5, "unit": "Mbps",
                              "steps": 2, "bound": 6.0}}
        order = (cell, ue) if cell_first else (ue, cell)
        return tuple(_row_from_record(r, i, ("traffic-steering",))
                     for i, r in enumerate(order, 1))

    def test_the_ue_row_is_found_even_when_the_cell_row_is_first(self):
        from tools.liveconsole.agent import _first_ue_row
        self.assertEqual(_first_ue_row(self._rows(cell_first=True)).ue_id, "ue1")
        self.assertEqual(_first_ue_row(self._rows(cell_first=False)).ue_id, "ue1")

    def test_a_sitting_with_no_ue_intent_is_refused_by_name(self):
        from tools.liveconsole.agent import _first_ue_row, LiveConsoleError, _row_from_record
        only_cell = (_row_from_record(
            {"intentId": "I0c", "owner": "operator-gnb2",
             "requirement": {"reqId": "I0c.r1", "kpi": "cellGoodputMbps",
                             "scope": "cell@87654321", "op": ">=",
                             "value": 12.0, "unit": "Mbps", "steps": 1, "bound": 10.0}},
            1, ("traffic-steering",)),)
        with self.assertRaises(LiveConsoleError):
            _first_ue_row(only_cell)


if __name__ == "__main__":
    unittest.main()
