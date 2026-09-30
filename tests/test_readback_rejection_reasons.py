"""되읽기 거부는 사유별로 세야 한다 (2026-09-21).

여섯(이제 일곱) 가지 서로 다른 사유가 `_rejected` 한 카운터에 합산됐다. 그래서 그 수가
올라가도 **gNB 재기동으로 epoch 이 낡은 것인지**(재핀이 필요) **노드 이름이 안 맞는
것인지**(설정 문제) **입력이 깨진 것인지**(무해) 구분할 수 없었다 — 조치가 전부 다른데도.

2026-09-21 에 조종 실패 63판을 진단할 때 이 수가 전부 0 이라 가설 하나를 반증할 수는
있었지만, 0 이 아니었다면 무엇 때문인지 말할 수 없었을 것이다.
"""
import json
import unittest

from assurance.live.pin_to_cell_driver import KpmUeAttributionReader, LiveCellTopology


NODE = "ngran=02;plmn=208-095-2;nb=0000003584/00;cudu=none:00000000000000000000"
GUAMI = {"mcc": 208, "mnc": 95, "mnc_digit_len": 2,
         "amf_region_id": 1, "amf_set_id": 1, "amf_pointer": 0}


def topology():
    return LiveCellTopology(plmn={"mcc": "208", "mnc": "095"},
                            nb_id_to_nci={3584: 12345678},
                            expected_epochs={NODE: 100})


def reader(lines):
    r = KpmUeAttributionReader(read_new_lines=lambda: tuple(lines), topology=topology())
    r.refresh()
    return r


def indication(**over):
    record = {"event": "kpm_indication", "e2_node": NODE, "connection_epoch": 100,
              "nb_id": 3584, "recv_unix_us": 1_788_000_000_000_000,
              "ues": [{"amf_ue_ngap_id": 7, "guami": GUAMI}]}
    record.update(over)
    return json.dumps(record)


class EachReasonIsCountedSeparately(unittest.TestCase):
    def _reasons(self, line):
        return reader([line]).rejected_by_reason

    def test_a_stale_epoch_says_epoch(self):
        """gNB 재기동 뒤 재핀 전 -- 이 노드의 지시가 한 줄도 통과하지 못한다."""
        self.assertEqual(self._reasons(indication(connection_epoch=999)), {"epoch": 1})

    def test_a_node_name_that_disagrees_with_nb_id_says_nbId(self):
        self.assertEqual(self._reasons(indication(nb_id=2816)), {"nbId": 1})

    def test_an_nb_id_outside_the_topology_says_topology(self):
        line = indication(e2_node=NODE.replace("3584", "9999"), nb_id=9999)
        reasons = reader([line]).rejected_by_reason
        # 이 레코드는 epoch 지도에도 없으므로 epoch 에서 먼저 걸린다 -- 순서가 계약이다.
        self.assertEqual(reasons, {"epoch": 1})

    def test_a_broken_line_says_json(self):
        self.assertEqual(self._reasons("{not json"), {"json": 1})

    def test_a_malformed_scope_says_fields(self):
        self.assertEqual(self._reasons(indication(recv_unix_us="어제")), {"fields": 1})

    def test_a_malformed_ue_entry_says_ueEntry(self):
        self.assertEqual(self._reasons(indication(ues=["문자열"])), {"ueEntry": 1})

    def test_a_ue_without_an_identity_says_ueFields(self):
        self.assertEqual(self._reasons(indication(ues=[{"guami": {}}])), {"ueFields": 1})

    def test_a_good_record_is_not_rejected(self):
        r = reader([indication()])
        self.assertEqual(r.rejected_by_reason, {})
        self.assertEqual(r.rejected_records, 0)


class TheBreakdownAgreesWithTheTotal(unittest.TestCase):
    def test_the_reasons_sum_to_rejected_records(self):
        r = reader(["{not json", indication(connection_epoch=999), indication(nb_id=2816),
                    indication(ues=["문자열"]), indication()])
        self.assertEqual(sum(r.rejected_by_reason.values()), r.rejected_records)
        self.assertEqual(r.rejected_records, 4)


if __name__ == "__main__":
    unittest.main()


class TheKpmFileReportDoesNotCallAnEmptyStreamValid(unittest.TestCase):
    """모든 줄이 epoch 불일치면 쓸 수 있는 샘플이 0 이다 — VALID 일 수 없다.

    2026-09-21: `missing_records` 를 `invalid_records` 에서 분리하면서 상태 판정이
    `invalid_records` 만 보고 있어, epoch 불일치뿐인 파일이 **샘플 0 인데 VALID** 로
    나왔다. 분리가 만든 회귀다.
    """

    def _report(self, epoch):
        import json as _json, tempfile, pathlib
        from assurance.collector.live import validate_kpm_jsonl
        record = {"event": "kpm_indication", "e2_node": "gNB1",
                  "connection_epoch": epoch, "recv_unix_us": 1_788_000_000_000_000,
                  "measurements": [{"name": "RRC.ConnMean", "type": "int", "value": 1}]}
        path = pathlib.Path(tempfile.mkdtemp()) / "k.jsonl"
        path.write_text("\n".join([_json.dumps(record)] * 3))
        return validate_kpm_jsonl(str(path), expected_epochs={"gNB1": 161})

    def test_an_all_stale_file_is_partial_not_valid(self):
        r = self._report(999)
        self.assertEqual(r.state, "PARTIAL")
        self.assertEqual(r.sample_count, 0)
        self.assertEqual(r.missing_records, 3)
        self.assertEqual(r.invalid_records, 0)

    def test_a_matching_file_is_valid(self):
        r = self._report(161)
        self.assertEqual(r.state, "VALID")
        self.assertEqual(r.missing_records, 0)
        self.assertGreater(r.sample_count, 0)


class EveryPlaceThatRecordsTheTotalAlsoRecordsTheReasons(unittest.TestCase):
    """사유는 reader 안에만 있으면 판이 끝날 때 사라진다 (2026-09-21).

    총계(`readerRejectedRecords`)만으로는 **무엇을 해야 하는지** 알 수 없다: `epoch` 이면
    gNB 가 재기동해 재핀이 필요하고, `nbId`/`topology` 는 설정 문제이며, 나머지는 입력이
    깨진 것이라 무해하다. 오늘 사유를 갈라 놓고도 기록으로 내보내지 않으면 아무 소용이 없다.

    형제 자리를 하나만 고치는 실수를 막으려고 **세 곳 전부**를 검사한다.
    """

    PLACES = (
        "tools/liveconsole/agent.py",
        "tools/liveconsole/build.py",
        "assurance/live/pin_to_cell_driver.py",
    )

    def test_the_total_and_the_reasons_travel_together(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        for place in self.PLACES:
            text = (root / place).read_text(encoding="utf-8")
            with self.subTest(place=place):
                self.assertIn("readerRejectedRecords", text)
                self.assertIn("readerRejectedByReason", text,
                              f"{place} 가 총계만 싣는다 -- 사유가 판 기록에서 사라진다")
