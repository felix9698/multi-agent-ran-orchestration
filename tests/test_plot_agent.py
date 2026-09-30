"""The Agent board-throughput plotter, hermetically.

A synthetic AGENT run document and KPM JSONL: assert the per-UE throughput
series, the per-trial board bands and the CSVs, and that a UE the run did not
name is never attributed.
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.campaign5.plot_agent import PlotError, load_run, render, trials_of

BASE = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
NODE = "ngran=02;plmn=208-095-2;nb=0000003584/00;cudu=none:00000000000000000000"


def _kpm_line(*, at: datetime, epoch: int, ue: int, thp_kbps: float, cap: int = 0) -> str:
    return json.dumps({
        "event": "kpm_indication", "kpm_msg_format": 3, "e2_node": NODE,
        "nb_id": 3584, "connection_epoch": epoch,
        "recv_unix_us": int(at.timestamp() * 1_000_000),
        "ues": [{"amf_ue_ngap_id": ue, "measurements": [
            {"name": "DRB.UEThpDl", "type": "int", "value": thp_kbps},
            {"name": "RAN.UE.DlPrbCap", "type": "int", "value": cap}]}],
    })


class TheAgentPlotter(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.run = self.tmp / "AGENT-case_a-run.json"
        self.events = self.tmp / "AGENT-case_a-events.jsonl"
        self.kpm = self.tmp / "kpm.jsonl"
        # Two trials: baseline (both deferred), then cap on UE 29 executed.
        document = {
            "schemaVersion": "liveconsole-agent-run/1.0.0",
            "caseId": "case/liveconsole-agent:case_a",
            "preflight": {
                "observedUes": {"28": {"amfUeNgapId": 28}, "29": {"amfUeNgapId": 29}},
                "deployment": {"expectedEpochs": {NODE: 286},
                               "kpmJsonlPath": str(self.kpm)},
            },
            "trials": [
                {"trialId": "case/liveconsole-agent:case_a:trial:1",
                 "candidateId": "candidate/000000", "terminalState": "SETTLED_NON_SUCCESS",
                 "outcome": "FAIL"},
                {"trialId": "case/liveconsole-agent:case_a:trial:2",
                 "candidateId": "candidate/000004", "terminalState": "SETTLED_SUCCESS",
                 "outcome": "SUCCESS"},
            ],
            "board": {
                "intentOrder": ["I1", "I2"],
                "actionOrder": ["steer@28", "steer@29", "cap@29"],
                "pairs": [
                    {"candidateId": "candidate/000000",
                     "intentStates": {"I1": "DEFERRED", "I2": "PRIORITIZED"},
                     "actionStates": {"steer@28": "DEFERRED", "steer@29": "DEFERRED",
                                      "cap@29": "DEFERRED"}},
                    {"candidateId": "candidate/000004",
                     "intentStates": {"I1": "PRIORITIZED", "I2": "PRIORITIZED"},
                     "actionStates": {"steer@28": "DEFERRED", "steer@29": "DEFERRED",
                                      "cap@29": "PRIORITIZED"}},
                ],
            },
            "summary": {"termination": "FOUND"},
        }
        self.run.write_text(json.dumps(document), encoding="utf-8")
        self.events.write_text("".join(
            json.dumps({"eventKind": "TrialOpened",
                        "timestamp": (BASE + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z"),
                        "payload": {"trialId": f"case/liveconsole-agent:case_a:trial:{index}"}}) + "\n"
            for index, offset in ((1, 0), (2, 12))), encoding="utf-8")
        lines = []
        for second in range(0, 24):
            at = BASE + timedelta(seconds=second)
            # UE 29 throughput rises after the cap trial opens (t=12).
            lines.append(_kpm_line(at=at, epoch=286, ue=28, thp_kbps=8000.0))
            lines.append(_kpm_line(at=at, epoch=286, ue=29,
                                   thp_kbps=2000.0 if second < 12 else 6000.0,
                                   cap=0 if second < 12 else 12))
            # A stray UE the run did not name, and a stale-epoch record: both dropped.
            lines.append(_kpm_line(at=at, epoch=286, ue=99, thp_kbps=9999.0))
            lines.append(_kpm_line(at=at, epoch=999, ue=28, thp_kbps=1.0))
        self.kpm.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    def test_trials_carry_their_board_cell_labels(self) -> None:
        trials = trials_of({**load_run(self.run),
                            "_events": {"case/liveconsole-agent:case_a:trial:1": BASE,
                                        "case/liveconsole-agent:case_a:trial:2": BASE + timedelta(seconds=12)}})
        self.assertEqual(2, len(trials))
        self.assertEqual("(I1', I2) <-> (steer@28', steer@29', cap@29')", trials[0].label)
        self.assertEqual("(I1, I2) <-> (steer@28', steer@29', cap@29)", trials[1].label)

    def test_render_writes_the_figure_and_two_csvs(self) -> None:
        out = self.tmp / "out"
        written = render(load_run(self.run), self.kpm, self.events, out)
        self.assertTrue(Path(written["figure"]).is_file())
        rows = list(csv.DictReader(open(written["boardCsv"], encoding="utf-8")))
        self.assertEqual(["1", "2"], [r["trial"] for r in rows])
        self.assertEqual("(I1, I2) <-> (steer@28', steer@29', cap@29)", rows[1]["board_cell"])
        self.assertEqual("FOUND", "".join("FOUND" for _ in [1]))

    def test_only_named_ues_are_attributed_and_stale_epochs_dropped(self) -> None:
        out = self.tmp / "out"
        written = render(load_run(self.run), self.kpm, self.events, out)
        rows = list(csv.DictReader(open(written["throughputCsv"], encoding="utf-8")))
        ues = {r["ue"] for r in rows}
        self.assertEqual({"28", "29"}, ues, "UE 99 was never named; it must not appear")
        # The stale-epoch UE 28 record (value 1.0 -> 0.001 Mbps) is dropped.
        self.assertNotIn("0.0010", {r["dl_throughput_mbps"] for r in rows})

    def test_the_throughput_moves_between_trials(self) -> None:
        out = self.tmp / "out"
        written = render(load_run(self.run), self.kpm, self.events, out)
        rows = list(csv.DictReader(open(written["throughputCsv"], encoding="utf-8")))
        ue29 = [(float(r["relative_seconds"]), float(r["dl_throughput_mbps"]))
                for r in rows if r["ue"] == "29"]
        before = [v for t, v in ue29 if 0 <= t < 12]
        after = [v for t, v in ue29 if t >= 12]
        self.assertTrue(before and after)
        self.assertLess(max(before), min(after),
                        "UE 29 throughput should rise after the cap trial opens")


    def test_render_reads_a_tun_rx_bytes_throughput_csv_as_the_y_source(self) -> None:
        out = self.tmp / "out2"
        thp = self.tmp / "thp.csv"
        base_ms = int(BASE.timestamp() * 1000)
        lines = ["unix_ms,amf_ue_ngap_id,dl_mbps,ul_mbps"]
        for second in range(0, 24):
            ms = base_ms + second * 1000
            lines.append(f"{ms},28,8.0,0.5")
            lines.append(f"{ms},29,{2.0 if second < 12 else 6.0},0.3")
            lines.append(f"{ms},99,9.0,0.0")  # never named -> dropped
        thp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        from tools.campaign5.plot_agent import render, load_run
        written = render(load_run(self.run), None, self.events, out, throughput_csv=thp)
        import csv as _csv
        rows = list(_csv.DictReader(open(written["throughputCsv"], encoding="utf-8")))
        self.assertEqual({"28", "29"}, {r["ue"] for r in rows})
        ue29 = [(float(r["relative_seconds"]), float(r["dl_throughput_mbps"]))
                for r in rows if r["ue"] == "29"]
        before = [v for t, v in ue29 if 0 <= t < 12]
        after = [v for t, v in ue29 if t >= 12]
        self.assertLess(max(before), min(after))
        self.assertTrue(Path(written["figure"]).is_file())

    def test_a_wrong_schema_is_refused(self) -> None:
        bad = self.tmp / "bad-run.json"
        bad.write_text(json.dumps({"schemaVersion": "liveconsole-run/1.1.0"}), encoding="utf-8")
        with self.assertRaises(PlotError):
            load_run(bad)


if __name__ == "__main__":
    unittest.main()
