"""``main.py --replay`` and ``--export``: the console's Replay lane, headless.

Acceptance matrix ``GAP-5``.  A run an operator can open in the window but not
from a script is a run that will be re-derived by hand for the paper, so these
two entry points print the *same* fields the Cockpit shows and hand the same
export files the window's Export button writes.

Two properties carry the safety of the lane and are asserted rather than
described:

* the printed block always names the session mode and the recording's mode
  **separately**, so a Replay of a live sitting can never read as LIVE;
* a source that cannot be reproduced is refused with a non-zero exit and no
  output that looks like a run.

Hermetic: the six committed ``LIVECONSOLE-*`` runs, a temp run root, and a temp
export directory.  Nothing opens a window and nothing touches a radio.
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main as main_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = REPO_ROOT / "docs" / "integration" / "evidence"
SUCCESS = "LIVECONSOLE-UeCellSteeringPinToCell-20260904T102054Z"
LOCKDOWN = "LIVECONSOLE-UELevelTarget-20260904T102318Z"


class _Headless(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.runs_root = self.root / "runs"
        self.runs_root.mkdir()

    def run_main(self, argv):
        """``main.main()`` with *argv*, capturing stdout."""
        buffer = io.StringIO()
        with mock.patch.object(sys, "argv", ["main.py", *argv]), \
             contextlib.redirect_stdout(buffer):
            code = main_mod.main()
        return code, buffer.getvalue()

    def evidence_copy(self, run_id):
        target = self.root / "evidence"
        target.mkdir(exist_ok=True)
        for path in EVIDENCE.glob(f"{run_id}-*"):
            shutil.copy2(path, target / path.name)
        return target


class TheArgumentNamesAFileOrADirectory(_Headless):

    def test_a_run_json_resolves_to_its_own_run(self):
        directory, run_id = main_mod._replay_target(
            EVIDENCE / f"{SUCCESS}-run.json")
        self.assertEqual(directory, EVIDENCE)
        self.assertEqual(run_id, SUCCESS)

    def test_a_directory_names_no_run_and_leaves_the_choice_open(self):
        directory, run_id = main_mod._replay_target(EVIDENCE)
        self.assertEqual(directory, EVIDENCE)
        self.assertIsNone(run_id)

    def test_a_path_that_is_neither_is_refused_before_anything_loads(self):
        with self.assertRaises(FileNotFoundError):
            main_mod._replay_target(self.root / "nothing-here")


class ReplayPrintsWhatTheConsoleShows(_Headless):

    def test_a_settled_success_prints_its_axes_and_exits_zero(self):
        code, out = self.run_main(
            ["--replay", str(EVIDENCE / f"{SUCCESS}-run.json"),
             "--runs-root", str(self.runs_root)])
        self.assertEqual(code, 0)
        self.assertIn("[REPLAY]", out)
        self.assertIn(SUCCESS, out)
        self.assertIn("SETTLED_SUCCESS", out)
        self.assertIn("execution validity     : VALID", out)
        self.assertIn("measurement sufficiency: SUFFICIENT", out)
        self.assertIn("serving-cell-is-pinned-throughout: PASS", out)
        self.assertIn("PREPARE ACKED -> READY ACKED", out)

    def test_the_two_modes_are_always_printed_apart(self):
        _code, out = self.run_main(
            ["--replay", str(EVIDENCE / f"{SUCCESS}-run.json"),
             "--runs-root", str(self.runs_root)])
        # The recording was LIVE; the session reading it back is not, and a
        # reader is told so on the same line.
        self.assertIn("recorded as   : LIVE", out)
        self.assertIn("this session is REPLAY", out)
        self.assertIn("nothing is being observed", out)

    def test_the_terminal_hash_and_reducer_are_printed(self):
        _code, out = self.run_main(
            ["--replay", str(EVIDENCE / f"{SUCCESS}-run.json"),
             "--runs-root", str(self.runs_root)])
        recorded = json.loads(
            (EVIDENCE / f"{SUCCESS}-run.json").read_text(encoding="utf-8"))
        self.assertIn(recorded["contract"]["terminalStateHash"], out)
        self.assertIn("assurance-kernel-reducer/", out)

    def test_a_lockdown_prints_its_incident_and_exits_non_zero(self):
        code, out = self.run_main(
            ["--replay", str(EVIDENCE / f"{LOCKDOWN}-run.json"),
             "--runs-root", str(self.runs_root)])
        self.assertEqual(code, 1)
        self.assertIn("INCIDENT_LOCKDOWN", out)
        self.assertIn("PARTIAL_APPLY", out)
        self.assertIn("NOT_EVALUATED", out)
        self.assertIn("[PARTIAL_DATA]", out)

    def test_a_directory_of_several_runs_can_be_pointed_at_one(self):
        code, out = self.run_main(
            ["--replay", str(EVIDENCE), "--run-id", LOCKDOWN,
             "--runs-root", str(self.runs_root)])
        self.assertEqual(code, 1)
        self.assertIn(LOCKDOWN, out)

    def test_a_source_that_cannot_be_reproduced_is_refused(self):
        root = self.evidence_copy(SUCCESS)
        path = root / f"{SUCCESS}-run.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["contract"]["terminalStateHash"] = "0" * 64
        path.write_text(json.dumps(document), encoding="utf-8")

        code, out = self.run_main(
            ["--replay", str(path), "--runs-root", str(self.runs_root)])
        self.assertEqual(code, 3)
        self.assertIn("refused", out)
        self.assertNotIn("[REPLAY]", out)

    def test_a_directory_holding_no_source_is_refused(self):
        code, out = self.run_main(
            ["--replay", str(self.root), "--runs-root", str(self.runs_root)])
        self.assertEqual(code, 2)
        self.assertIn("no source this console can read", out)


class ExportWritesTheSameFilesTheWindowDoes(_Headless):

    def test_it_prints_the_run_and_writes_csv_and_json(self):
        out_dir = self.root / "export"
        code, out = self.run_main(
            ["--export", str(EVIDENCE / f"{SUCCESS}-run.json"),
             "--out", str(out_dir), "--runs-root", str(self.runs_root)])
        self.assertEqual(code, 0)
        self.assertIn("SETTLED_SUCCESS", out)
        self.assertIn("exported to", out)
        for name in ("telemetry.csv", "events.csv", "episodes.csv",
                     "cycles.csv", "telemetry.json", "events.json",
                     "decision.json", "summary.json", "EXPORT-MANIFEST.json"):
            with self.subTest(file=name):
                self.assertTrue((out_dir / name).is_file())

    def test_every_exported_file_carries_the_session_mode(self):
        out_dir = self.root / "export"
        self.run_main(["--export", str(EVIDENCE / f"{SUCCESS}-run.json"),
                       "--out", str(out_dir), "--runs-root", str(self.runs_root)])
        for name in ("telemetry.csv", "events.csv", "episodes.csv"):
            with self.subTest(file=name):
                head = (out_dir / name).read_text(encoding="utf-8").splitlines()[0]
                self.assertIn("mode=REPLAY", head)

    def test_the_raw_evidence_travels_with_the_export(self):
        out_dir = self.root / "export"
        self.run_main(["--export", str(EVIDENCE / f"{SUCCESS}-run.json"),
                       "--out", str(out_dir), "--runs-root", str(self.runs_root)])
        raw = out_dir / "raw" / "liveconsole-run" / f"{SUCCESS}-events.jsonl"
        self.assertTrue(raw.is_file())
        self.assertEqual(raw.read_bytes(),
                         (EVIDENCE / f"{SUCCESS}-events.jsonl").read_bytes())

    def test_a_lockdown_export_still_writes_and_still_exits_non_zero(self):
        out_dir = self.root / "export"
        code, _out = self.run_main(
            ["--export", str(EVIDENCE / f"{LOCKDOWN}-run.json"),
             "--out", str(out_dir), "--runs-root", str(self.runs_root)])
        self.assertEqual(code, 1)
        head = (out_dir / "episodes.csv").read_text(
            encoding="utf-8").splitlines()[0]
        self.assertIn("disposition=FAILED", head)


class TheSupplementaryHalfReachesTheShell(_Headless):
    """``liveconsole-run/1.1.0`` headless: the cap is printed, not swallowed.

    The 1.1.0 document is constructed over a committed 1.0.0 stream -- there is
    no OTA 1.1.0 evidence yet and none is claimed -- so the replay still has to
    re-derive and match the recorded terminal state hash to get this far.
    """

    def one_one_zero(self):
        root = self.evidence_copy(SUCCESS)
        path = root / f"{SUCCESS}-run.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["schemaVersion"] = "liveconsole-run/1.1.0"
        document["supplementary"] = [{
            "actionId": "ue-dl-prb-cap",
            "adapterKey": "r1-cap",
            "policyTypeId": "AIC_UeDlPrbCap_1.0.0",
            "axis": "dlPrbCap",
            "controlledUe": {"cellId": "12345678", "ueId": "17"},
            "candidateCaps": [5, 12, 24],
            "liveBindings": [],
            "expectedAttribution": {"amfUeNgapId": 17, "e2Node": "gnb-2816",
                                    "connectionEpoch": 273},
            "bindingState": "RESTORED",
            "bindingPolicyId": "pol-cap-1",
            "readbackState": "VERIFIED",
            "rollbackDetail": "baseline read back through the independent counter",
            "readbackLog": [{"maxDlPrbs": 12}, {"maxDlPrbs": 0}],
        }]
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_the_cap_its_ue_and_its_restoration_are_printed(self):
        path = self.one_one_zero()
        code, out = self.run_main(
            ["--replay", str(path), "--runs-root", str(self.runs_root)])
        self.assertEqual(code, 0)
        self.assertIn("supplementary:", out)
        self.assertIn("ue-dl-prb-cap via r1-cap", out)
        self.assertIn("AIC_UeDlPrbCap_1.0.0", out)
        self.assertIn("controlled UE : 17", out)
        self.assertIn("pol-cap-1", out)
        self.assertIn("RESTORED", out)
        self.assertIn("readbacks     : 2", out)

    def test_a_one_point_zero_run_says_not_recorded_rather_than_none(self):
        code, out = self.run_main(
            ["--replay", str(EVIDENCE / f"{SUCCESS}-run.json"),
             "--runs-root", str(self.runs_root)])
        self.assertEqual(code, 0)
        self.assertIn("supplementary : not recorded", out)
        self.assertNotIn("ue-dl-prb-cap", out)

    def test_the_export_carries_it_into_summary_json(self):
        path = self.one_one_zero()
        out_dir = self.root / "export"
        self.run_main(["--export", str(path), "--out", str(out_dir),
                       "--runs-root", str(self.runs_root)])
        exported = json.loads(
            (out_dir / "summary.json").read_text(encoding="utf-8"))
        # The export wraps every document in its provenance envelope; the run
        # summary is the ``summary`` key of its ``data``.
        entries = exported["data"]["summary"]["supplementary"]
        self.assertEqual([entry["bindingPolicyId"] for entry in entries],
                         ["pol-cap-1"])


class TheLaneRefusesToBeMixedWithARuntime(_Headless):

    def _error(self, argv):
        buffer = io.StringIO()
        with mock.patch.object(sys, "argv", ["main.py", *argv]), \
             contextlib.redirect_stderr(buffer), \
             self.assertRaises(SystemExit) as caught:
            main_mod.main()
        return caught.exception.code, buffer.getvalue()

    def test_a_recorded_source_and_a_runtime_are_not_one_session(self):
        for runtime in ("--hardware-free", "--live"):
            with self.subTest(runtime=runtime):
                code, err = self._error(
                    ["--replay", str(EVIDENCE), runtime])
                self.assertEqual(code, 2)
                self.assertIn("two different", err)

    def test_export_needs_a_destination(self):
        code, err = self._error(["--export", str(EVIDENCE)])
        self.assertEqual(code, 2)
        self.assertIn("--out", err)

    def test_out_without_export_is_refused(self):
        code, err = self._error(["--out", str(self.root)])
        self.assertEqual(code, 2)
        self.assertIn("--out is the destination", err)

    def test_replay_and_export_are_not_both_given(self):
        code, err = self._error(
            ["--replay", str(EVIDENCE), "--export", str(EVIDENCE),
             "--out", str(self.root)])
        self.assertEqual(code, 2)
        self.assertIn("already does both", err)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
