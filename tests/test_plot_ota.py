"""Hermetic coverage for the Campaign 5 OTA plot reader."""
from __future__ import annotations

import csv
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.campaign5.plot_ota import (
    Marker, PlotError, _case_table, load_run, markers, named_ues, plot_case,
)


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/integration/evidence"


class OtaPlotTests(unittest.TestCase):
    def test_all_six_committed_cockpit_records_are_accepted(self) -> None:
        records = sorted(EVIDENCE.glob("LIVECONSOLE-*-run.json"))
        # Six records were committed by 2026-09-05; live sessions keep adding
        # records of both schema versions, and every one must still load.
        self.assertGreaterEqual(len(records), 6)
        for record in records:
            self.assertIn(load_run(record)["schemaVersion"],
                          {"liveconsole-run/1.0.0", "liveconsole-run/1.1.0"})

    def test_only_exact_named_tuple_is_attributed_and_csv_is_beside_figure(self) -> None:
        source = EVIDENCE / "LIVECONSOLE-UeCellSteeringPinToCell-20260904T102214Z-run.json"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); run = root / source.name
            events = root / source.name.replace("-run.json", "-events.jsonl")
            shutil.copyfile(source, run); shutil.copyfile(source.with_name(events.name), events)
            detail = load_run(run); ue, node, epoch = next(iter(named_ues(detail)))
            anchor = datetime.fromisoformat("2026-09-04T10:22:15.292084+00:00")
            def row(seconds: int, identifier: str, value: float, *, no_value: bool = False) -> dict:
                return {"amf_ue_ngap_id": int(identifier), "nb_id": int(node),
                        "connection_epoch": int(epoch),
                        "recv_unix_us": int((anchor.timestamp() + seconds) * 1_000_000),
                        "measurements": [{"name": "DRB.UEThpDl", "value": value, "no_value": no_value},
                                         {"name": "DRB.PdcpSduVolumeDL", "value": seconds * 1_000_000},
                                         {"name": "RAN.UE.DlPrbCap", "value": 24}]}
            kpm = root / "synthetic-kpm.jsonl"
            lines = [row(1, ue, 8), row(2, ue, 10), row(3, "999", 999), row(4, ue, 0, no_value=True),
                     {"nb_id": int(node), "connection_epoch": int(epoch),
                      "recv_unix_us": int((anchor.timestamp()+2)*1_000_000),
                      "measurements": [{"name": "RRU.PrbTotDl", "value": 31}]}]
            kpm.write_text("\n".join(json.dumps(item) for item in lines) + "\n", encoding="utf-8")
            outputs = plot_case(run, kpm, root / "out", "steer", tables=True)
            png, sidecar, table = outputs
            self.assertTrue(png.exists()); self.assertTrue(sidecar.exists()); self.assertTrue(table.exists())
            contents = sidecar.read_text(encoding="utf-8")
            self.assertNotIn("999.0", contents)
            self.assertIn("ContractAdmitted", contents)
            with table.open(newline="", encoding="utf-8") as handle:
                self.assertEqual(1, len(list(csv.DictReader(handle))))
            with self.assertRaisesRegex(PlotError, "not named"):
                plot_case(run, kpm, root / "refused", "steer", requested_ue="999")

    def test_readback_latency_starts_at_commit_issue_not_later_commit_ack(self) -> None:
        instant = datetime(2026, 9, 6, 9, 10, 43, tzinfo=timezone.utc)
        events = [{"eventKind": "TokenIssued", "timestamp": instant.isoformat(),
                   "payload": {"tokenKind": "COMMIT", "issuedAt": instant.isoformat()}}]
        markers_for_case = [Marker(instant, "ContractAdmitted"),
                            Marker(instant.replace(second=44), "A1 policy APPLIED_VERIFIED"),
                            Marker(instant.replace(second=45), "COMMIT ACKED")]
        table = _case_table({}, events, markers_for_case, instant)
        self.assertEqual(1.0, table["commit_issue_or_prepare_ready_acked_to_applied_verified_s"])
        self.assertEqual("COMMIT issue", table["latency_start_anchor"])
        self.assertEqual("", table["latency_unavailable_reason"])

    def test_latency_refuses_negative_or_missing_anchors(self) -> None:
        instant = datetime(2026, 9, 6, 9, 10, 43, tzinfo=timezone.utc)
        events = [{"eventKind": "TokenIssued", "timestamp": instant.isoformat(),
                   "payload": {"tokenKind": "COMMIT", "issuedAt": instant.isoformat()}}]
        table = _case_table({}, events, [Marker(instant, "ContractAdmitted"),
                                          Marker(instant.replace(second=42), "A1 policy APPLIED_VERIFIED")], instant)
        self.assertEqual("", table["commit_issue_or_prepare_ready_acked_to_applied_verified_s"])
        self.assertIn("refusing a negative", table["latency_unavailable_reason"])
        refusal = markers({"supplementary": [{"readbackState": "COUNTER_ABSENT", "occurredAt": instant.isoformat()}]}, [])
        self.assertEqual("refusal: COUNTER_ABSENT", refusal[0].label)

    def test_conflict_plot_is_document_only(self) -> None:
        source = EVIDENCE / "LIVECONSOLE-UeCellSteeringPinToCell-20260904T102214Z-run.json"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); run = root / source.name
            events = root / source.name.replace("-run.json", "-events.jsonl")
            shutil.copyfile(source, run); shutil.copyfile(source.with_name(events.name), events)
            png, sidecar = plot_case(run, None, root / "out", "conflict")
            self.assertTrue(png.exists()); self.assertTrue(sidecar.exists())


if __name__ == "__main__":
    unittest.main()
