"""The LIVE console evidence, read back as a Replay source.

Acceptance matrix ``GAP-4``: until this adapter existed the only OTA evidence
the project had was citable from a shell and not openable in the console that
produced it.  What makes it evidence rather than a rendering is the property
this file asserts over and over: **every value shown is re-derived by folding
the recorded event stream through the same Kernel reducer that produced it**,
and a stream that does not reproduce its recorded terminal state hash is
refused rather than displayed.

Hermetic: the six ``LIVECONSOLE-*`` runs of 2026-09-04 are committed in the
repository, and every mutation test copies them into a temporary directory
first -- the evidence directory is an input and nothing here writes to it.
"""

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from gui.operator.sources import replay
from gui.operator.sources.adapters import liveconsole_run as lc

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = REPO_ROOT / "docs" / "integration" / "evidence"

#: The six committed runs, with the trial state each ended in and the run
#: disposition that state maps to.  Named rather than globbed so a file that
#: disappears is a failure and not a silently shorter sweep.
RUNS = (
    ("LIVECONSOLE-UeCellSteeringPinToCell-20260904T101748Z",
     "INCIDENT_LOCKDOWN", "FAILED"),
    ("LIVECONSOLE-UeCellSteeringPinToCell-20260904T102054Z",
     "SETTLED_SUCCESS", "COMPLETED"),
    ("LIVECONSOLE-UeCellSteeringPinToCell-20260904T102214Z",
     "SETTLED_SUCCESS", "COMPLETED"),
    ("LIVECONSOLE-UELevelTarget-20260904T102318Z",
     "INCIDENT_LOCKDOWN", "FAILED"),
    ("LIVECONSOLE-UELevelTarget-20260904T102711Z",
     "SETTLED_NON_SUCCESS", "ABORTED"),
    ("LIVECONSOLE-UELevelTarget-20260904T102917Z",
     "INCIDENT_LOCKDOWN", "FAILED"),
    # 2026-09-06 live OTA (commit 72d661811): one steering SETTLED_SUCCESS and
    # three QoSTarget+cap compositions that fail-closed with zero writes.
    ("LIVECONSOLE-TrafficSteeringPreference-20260906T091043Z-2",
     "SETTLED_SUCCESS", "COMPLETED"),
    ("LIVECONSOLE-QoSTarget-20260906T092318Z",
     "SETTLED_NON_SUCCESS", "ABORTED"),
    ("LIVECONSOLE-QoSTarget-20260906T092723Z",
     "SETTLED_NON_SUCCESS", "ABORTED"),
    ("LIVECONSOLE-QoSTarget-20260906T094007Z",
     "INCIDENT_LOCKDOWN", "FAILED"),
)

SUCCESS = RUNS[1][0]
LOCKDOWN = RUNS[3][0]

#: What ``tools/liveconsole/build.py::_supplementary_record`` writes for one
#: composed cap.  Constructed here, field for field from that writer: there is
#: no committed 1.1.0 evidence and none is invented -- this is a fixture for the
#: reader, and the docstring above says so.
SUPPLEMENTARY_RECORD = [{
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
    "readbackLog": [
        {"at": "2026-09-04T10:21:09.100000Z", "maxDlPrbs": 12,
         "result": "VERIFIED"},
        {"at": "2026-09-04T10:21:44.700000Z", "maxDlPrbs": 0,
         "result": "VERIFIED"},
    ],
}]


def recorded(run_id):
    return json.loads(
        (EVIDENCE / f"{run_id}-run.json").read_text(encoding="utf-8"))


class _Temp(unittest.TestCase):
    """A temp run root, and a writable copy of the evidence when needed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runs_root = Path(self._tmp.name) / "runs"
        self.runs_root.mkdir()

    def copy_evidence(self, *run_ids):
        """A writable copy of the named runs.  The repository is read-only."""
        target = Path(self._tmp.name) / "evidence"
        target.mkdir(exist_ok=True)
        for run_id in run_ids:
            for suffix in (lc.RUN_SUFFIX, lc.EVENTS_SUFFIX,
                           lc.SCOPE_ARCHIVE_SUFFIX):
                source = EVIDENCE / f"{run_id}{suffix}"
                if source.is_file():
                    shutil.copy2(source, target / source.name)
        return target

    def load(self, run_id, root=None):
        store = replay.load(root or EVIDENCE, self.runs_root,
                            session_id=run_id)
        self.addCleanup(store.close)
        return store


class TheSourceIsDiscovered(_Temp):

    def test_the_evidence_directory_is_recognized_as_this_source(self):
        self.assertEqual(replay.detect_adapter(EVIDENCE), lc.SOURCE_ID)

    def test_every_committed_run_is_listed(self):
        self.assertEqual(sorted(replay.list_sessions(EVIDENCE)),
                         sorted(run_id for run_id, _s, _d in RUNS))

    def test_a_run_record_without_its_stream_is_not_listed(self):
        # The stream is what the axes are re-derived from.  A run record on its
        # own is a claim, and offering it would be offering a rendering.
        root = self.copy_evidence(SUCCESS)
        (root / f"{SUCCESS}{lc.EVENTS_SUFFIX}").unlink()
        self.assertEqual(lc.list_sessions(root), [])

    def test_a_directory_of_neither_is_no_source_at_all(self):
        self.assertIsNone(replay.detect_adapter(Path(self._tmp.name)))

    def test_the_source_declares_what_it_cannot_show(self):
        description = replay.SOURCE_DESCRIPTIONS[lc.SOURCE_ID]
        self.assertEqual(description["mode"], "REPLAY")
        self.assertTrue(description["carries"])
        self.assertTrue(description["cannotShow"])


class TheSessionIsAlwaysReplay(_Temp):

    def test_a_recording_of_a_live_run_is_a_replay_session(self):
        store = self.load(SUCCESS)
        manifest = store.read_manifest()
        self.assertEqual(manifest["mode"], "REPLAY")
        self.assertEqual(manifest["modeEvidence"]["basis"],
                         "REPLAY_OF_RECORDED_SOURCE")
        # The recording's own mode travels separately and never becomes the
        # session's: the badge, the banner and the watermark follow the session.
        self.assertEqual(recorded(SUCCESS)["sessionMode"], "LIVE")
        self.assertEqual(store.read_summary()["sourceMode"], "LIVE")

    def test_there_is_no_argument_that_makes_it_live(self):
        import inspect

        self.assertNotIn(
            "mode", inspect.signature(lc.load_liveconsole_run).parameters)
        self.assertNotIn("mode", inspect.signature(replay.load).parameters)


class TheStateIsReDerived(_Temp):
    """The point of the source: the stream reproduces what the operator saw."""

    def test_the_axes_equal_the_recorded_ones_for_every_run(self):
        for run_id, _state, _disposition in RUNS:
            with self.subTest(run=run_id):
                summary = self.load(run_id).read_summary()
                self.assertEqual(summary["axes"], recorded(run_id)["axes"])

    def test_the_terminal_hash_is_re_derived_and_matches_the_record(self):
        for run_id, _state, _disposition in RUNS:
            with self.subTest(run=run_id):
                summary = self.load(run_id).read_summary()
                self.assertEqual(summary["terminalStateHash"],
                                 recorded(run_id)["contract"]["terminalStateHash"])
                self.assertEqual(summary["terminalStateHash"],
                                 summary["recordedTerminalStateHash"])

    def test_the_settlement_is_read_off_the_reduced_state(self):
        for run_id, expected_state, _disposition in RUNS:
            with self.subTest(run=run_id):
                settlement = self.load(run_id).read_summary()["settlement"]
                record = recorded(run_id)["settlement"]
                self.assertEqual(settlement["trialState"], expected_state)
                self.assertEqual(settlement["trialState"], record["trialState"])
                self.assertEqual(settlement["outcome"], record["outcome"])
                self.assertEqual(settlement["stopReason"], record["stopReason"])
                self.assertEqual(settlement["harmCharges"], record["harmCharges"])

    def test_the_recovery_leg_the_run_record_stopped_short_of_is_shown(self):
        """The stream carries more than the console's own report did.

        On the three lockdowns and the safety stop the runtime report ends at
        the COMMIT, and the Kernel's stream carries the ``CONFIGURATION_REREAD``
        the recovery went on to issue.  Showing the stream's version is the
        whole point of replaying it, and the difference is *recorded* rather
        than resolved silently.
        """
        store = self.load(LOCKDOWN)
        operations = store.read_summary()["settlement"]["gatewayOperations"]
        self.assertEqual(
            [list(item) for item in operations],
            [["PREPARE", "ACKED"], ["READY", "ACKED"], ["COMMIT", "UNKNOWN"],
             ["CONFIGURATION_REREAD", "UNKNOWN"]])
        self.assertEqual(
            recorded(LOCKDOWN)["settlement"]["gatewayOperations"],
            [["PREPARE", "ACKED"], ["READY", "ACKED"], ["COMMIT", "UNKNOWN"]])
        mismatches = [issue for issue in store.read_manifest()["dataIssues"]
                      if issue["kind"] == "SCHEMA_MISMATCH"]
        self.assertTrue(any("gatewayOperations" in issue["detail"]
                            for issue in mismatches))

    def test_a_settled_success_records_no_disagreement_at_all(self):
        store = self.load(SUCCESS)
        self.assertEqual(
            [issue for issue in store.read_manifest()["dataIssues"]
             if issue["kind"] == "SCHEMA_MISMATCH"], [])

    def test_the_stream_wins_when_the_run_record_disagrees_about_the_axes(self):
        root = self.copy_evidence(SUCCESS)
        path = root / f"{SUCCESS}{lc.RUN_SUFFIX}"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["axes"]["executionValidity"] = "INVALID"
        path.write_text(json.dumps(document), encoding="utf-8")

        store = self.load(SUCCESS, root=root)
        self.assertEqual(store.read_summary()["axes"]["executionValidity"],
                         "VALID")
        self.assertTrue(any(
            issue["kind"] == "SCHEMA_MISMATCH" and "axes" in issue["detail"]
            for issue in store.read_manifest()["dataIssues"]))


class AStreamThatCannotBeReproducedIsRefused(_Temp):
    """A console that renders a run it cannot reproduce will one day render a
    run that never happened."""

    def _refusal(self, root, run_id=SUCCESS):
        with self.assertRaises(replay.ReplayError) as caught:
            replay.load(root, self.runs_root, session_id=run_id)
        return caught.exception

    def test_a_recorded_hash_that_the_stream_does_not_reach_is_refused(self):
        root = self.copy_evidence(SUCCESS)
        path = root / f"{SUCCESS}{lc.RUN_SUFFIX}"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["contract"]["terminalStateHash"] = "0" * 64
        path.write_text(json.dumps(document), encoding="utf-8")

        error = self._refusal(root)
        self.assertIn("0" * 64, error.detail)
        self.assertIn("cannot be reproduced", error.detail)
        # And nothing was left behind to be interpreted later.
        self.assertEqual(list(self.runs_root.iterdir()), [])

    def test_a_mutated_payload_is_caught_at_the_envelope(self):
        root = self.copy_evidence(SUCCESS)
        path = root / f"{SUCCESS}{lc.EVENTS_SUFFIX}"
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            record = json.loads(line)
            if record["eventKind"] == "TrialEvaluated":
                record["payload"]["predicateVerdicts"] = {
                    name: "FAIL" for name in record["payload"]["predicateVerdicts"]}
                lines[index] = json.dumps(record)
                break
        else:  # pragma: no cover - the fixture always has one
            self.fail("no TrialEvaluated event to mutate")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        error = self._refusal(root)
        self.assertIn("digest of their own payload", error.detail)

    def test_a_truncated_line_is_refused_rather_than_partly_replayed(self):
        root = self.copy_evidence(SUCCESS)
        path = root / f"{SUCCESS}{lc.EVENTS_SUFFIX}"
        text = path.read_text(encoding="utf-8")
        path.write_text(text[:len(text) // 2], encoding="utf-8")

        error = self._refusal(root)
        self.assertEqual(error.kind, "DATA_TRUNCATED")

    def test_a_reordered_stream_is_refused_by_the_reducer_or_the_hash(self):
        root = self.copy_evidence(SUCCESS)
        path = root / f"{SUCCESS}{lc.EVENTS_SUFFIX}"
        lines = [line for line in
                 path.read_text(encoding="utf-8").splitlines() if line.strip()]
        lines[3], lines[-3] = lines[-3], lines[3]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._refusal(root)

    def test_an_unknown_schema_version_is_refused(self):
        root = self.copy_evidence(SUCCESS)
        path = root / f"{SUCCESS}{lc.RUN_SUFFIX}"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["schemaVersion"] = "liveconsole-run/9.9.9"
        path.write_text(json.dumps(document), encoding="utf-8")

        error = self._refusal(root)
        self.assertIn("9.9.9", error.detail)


class TheDispositionFollowsTheRecordedOutcome(_Temp):

    def test_only_a_settled_success_is_a_completed_run(self):
        for run_id, _state, expected in RUNS:
            with self.subTest(run=run_id):
                store = self.load(run_id)
                self.assertEqual(store.read_manifest()["disposition"], expected)
                self.assertEqual(store.is_success, expected == "COMPLETED")

    def test_a_non_success_says_why_in_the_data_issues(self):
        store = self.load(LOCKDOWN)
        self.assertTrue(any(
            issue["kind"] == "PARTIAL_DATA"
            and "INCIDENT_LOCKDOWN" in issue["detail"]
            for issue in store.read_manifest()["dataIssues"]))


class TheNormalizedRecords(_Temp):

    def setUp(self):
        super().setUp()
        self.store = self.load(SUCCESS)
        self.samples = list(self.store.read_telemetry())
        self.events = list(self.store.read_events())
        self.episodes = list(self.store.read_episodes())

    def test_the_counter_samples_keep_the_time_they_declared(self):
        self.assertTrue(self.samples)
        observed = {
            sample["observedAt"]
            for sample in (json.loads(line) for line in
                           (EVIDENCE / f"{SUCCESS}{lc.EVENTS_SUFFIX}"
                            ).read_text(encoding="utf-8").splitlines())
            if sample.get("eventKind") == "RawSampleIngested"
            for sample in [sample["payload"]]}
        self.assertEqual({sample["tUtc"] for sample in self.samples}, observed)

    def test_a_sample_quality_comes_from_the_sample_and_not_a_default(self):
        for sample in self.samples:
            self.assertEqual(sample["quality"], "OK")  # clockHealth SYNCHRONISED
            self.assertEqual(sample["source"]["boundary"], "R1_DME")
            self.assertIsNotNone(sample["value"])

    def test_no_kpi_registry_metric_is_claimed_by_this_source(self):
        index = self.store.read_metric_index()
        self.assertTrue(index)
        for metric, entry in index.items():
            if metric.startswith("counter/"):
                continue  # what the stream actually carries
            self.assertEqual(
                entry["status"], "UNSUPPORTED",
                f"{metric} must not read as available on an event stream")

    def test_every_recorded_envelope_appears_once_and_in_order(self):
        recorded_lines = [
            json.loads(line) for line in
            (EVIDENCE / f"{SUCCESS}{lc.EVENTS_SUFFIX}"
             ).read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(self.events), len(recorded_lines))
        self.assertEqual([event["kind"] for event in self.events],
                         [item["eventKind"] for item in recorded_lines])
        self.assertEqual([event["tUtc"] for event in self.events],
                         [item["timestamp"] for item in recorded_lines])
        self.assertEqual({event["origin"] for event in self.events},
                         {"OBSERVED"})

    def test_a_lockdown_transition_is_an_error_on_the_timeline(self):
        store = self.load(LOCKDOWN)
        errors = [event for event in store.read_events()
                  if event["severity"] == "ERROR"]
        self.assertTrue(errors)
        self.assertTrue(any("INCIDENT_LOCKDOWN" in event["title"]
                            for event in errors))
        # An error must be correlatable or an operator cannot act on it.
        for event in errors:
            self.assertTrue(any(value for value in event["ids"].values()))

    def test_the_episode_states_the_kernel_vocabulary_and_invents_no_eq12(self):
        episode = self.episodes[0]
        self.assertIsNone(episode["eq12State"])
        self.assertIsNone(episode["terminalOutcome"])
        self.assertEqual(episode["trialState"], "SETTLED_SUCCESS")
        self.assertEqual(episode["intentText"], recorded(SUCCESS)["utterance"])
        self.assertIn("Eq.12", episode["eq12Note"])

    def test_one_cycle_per_trial_the_case_opened(self):
        cycles = list(self.store.read_cycles())
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["trialState"], "SETTLED_SUCCESS")
        self.assertEqual(cycles[0]["planAxes"], ["servingCell"])

    def test_the_raw_evidence_is_copied_byte_for_byte(self):
        for suffix in (lc.EVENTS_SUFFIX, lc.RUN_SUFFIX):
            with self.subTest(suffix=suffix):
                copied = (self.store.run_dir / "raw" / lc.SOURCE_ID
                          / f"{SUCCESS}{suffix}")
                self.assertEqual(copied.read_bytes(),
                                 (EVIDENCE / f"{SUCCESS}{suffix}").read_bytes())


class TheSupplementaryHalfOfAOnePointOneRun(_Temp):
    """``liveconsole-run/1.1.0``: the cap's own adapter is read back too.

    The live writer bumped to 1.1.0 to record what only a SUPPLEMENTARY
    participant knows -- the policy it bound, the UE/cell/epoch its readback was
    entitled to match, every answer that readback gave and whether the durable
    binding reached ``RESTORED``.  A reader pinned to 1.0.0 would refuse the
    next OTA sitting outright, and one that merely *accepted* 1.1.0 without
    reading that half would answer "was the cap applied, to which UE, and was
    it restored" with silence.

    The 1.1.0 document here is **constructed**, not recorded: the six committed
    runs are 1.0.0 and steering-only, so there is no OTA 1.1.0 evidence to read
    and none is claimed.  Its event stream is a real committed one, which is
    what keeps the replay honest -- the terminal state hash still has to be
    re-derived and matched.
    """

    def one_one_zero(self, *, supplementary=None):
        """A 1.1.0 run over a real committed stream, in a temp directory."""
        root = self.copy_evidence(SUCCESS)
        path = root / f"{SUCCESS}{lc.RUN_SUFFIX}"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["schemaVersion"] = "liveconsole-run/1.1.0"
        document["supplementary"] = (
            SUPPLEMENTARY_RECORD if supplementary is None else supplementary)
        path.write_text(json.dumps(document, indent=2, sort_keys=True),
                        encoding="utf-8")
        return root

    def test_a_one_one_zero_run_is_accepted_and_still_re_derived(self):
        root = self.one_one_zero()
        store = self.load(SUCCESS, root=root)
        summary = store.read_summary()
        self.assertEqual(store.read_manifest()["mode"], "REPLAY")
        self.assertEqual(
            store.read_manifest()["sources"][0]["schemaVersion"],
            "liveconsole-run/1.1.0")
        # The bump changes nothing about the reduction: the hash is still
        # re-derived from the stream and still has to match.
        self.assertEqual(summary["terminalStateHash"],
                         recorded(SUCCESS)["contract"]["terminalStateHash"])
        self.assertEqual(summary["axes"], recorded(SUCCESS)["axes"])

    def test_the_supplementary_half_is_read_back_field_for_field(self):
        store = self.load(SUCCESS, root=self.one_one_zero())
        entries = store.read_summary()["supplementary"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["actionId"], "ue-dl-prb-cap")
        self.assertEqual(entry["adapterKey"], "r1-cap")
        self.assertEqual(entry["policyTypeId"], "AIC_UeDlPrbCap_1.0.0")
        self.assertEqual(entry["axis"], "dlPrbCap")
        # The three facts the R2 rows exist to ask for.
        self.assertEqual(entry["controlledUe"], {"cellId": "12345678",
                                                 "ueId": "17"})
        self.assertEqual(entry["bindingPolicyId"], "pol-cap-1")
        self.assertEqual(entry["bindingState"], "RESTORED")
        self.assertEqual(entry["readbackState"], "VERIFIED")
        self.assertIn("baseline", entry["rollbackDetail"])

    def test_the_readback_carries_the_ue_cell_and_epoch_it_was_entitled_to(self):
        store = self.load(SUCCESS, root=self.one_one_zero())
        entry = store.read_summary()["supplementary"][0]
        self.assertEqual(entry["expectedAttribution"],
                         {"amfUeNgapId": 17, "e2Node": "gnb-2816",
                          "connectionEpoch": 273})
        # Every answer the reader gave, not just the last one: a cap that read
        # 24 then 12 is a different run from one that only ever read 12.
        self.assertEqual([item["maxDlPrbs"] for item in entry["readbackLog"]],
                         [12, 0])

    def test_it_lives_in_the_summary_and_not_in_the_config_snapshot(self):
        """One whole copy beats two, one of them mangled.

        The config snapshot is credential-scrubbed, and ``adapterKey`` is
        key-shaped: a second copy there would come back
        ``REDACTED_NEVER_WRITTEN`` and read as a redacted secret rather than as
        the adapter it names.  The half is a result, so it belongs to the
        summary.
        """
        store = self.load(SUCCESS, root=self.one_one_zero())
        self.assertNotIn("supplementary", store.read_config_snapshot())
        self.assertTrue(store.read_summary()["supplementary"])

    def test_a_one_one_zero_run_that_composed_none_records_no_absence(self):
        # An empty list on a version that *would* have recorded one is an
        # answer: this case composed no supplementary control.
        store = self.load(SUCCESS, root=self.one_one_zero(supplementary=[]))
        self.assertEqual(store.read_summary()["supplementary"], [])
        self.assertTrue(store.read_summary()["supplementaryRecorded"])
        self.assertFalse([issue for issue in store.read_manifest()["dataIssues"]
                          if "predates" in issue["detail"]])

    def test_a_one_point_zero_run_records_the_absence_as_an_absence(self):
        # The same empty list on 1.0.0 is *not* an answer, and the run says so.
        store = self.load(SUCCESS)
        self.assertEqual(store.read_summary()["supplementary"], [])
        self.assertFalse(store.read_summary()["supplementaryRecorded"])
        self.assertTrue([issue for issue in store.read_manifest()["dataIssues"]
                         if "absence of record, not an absence of cap"
                         in issue["detail"]])

    def test_a_version_this_adapter_does_not_read_is_still_refused(self):
        root = self.copy_evidence(SUCCESS)
        path = root / f"{SUCCESS}{lc.RUN_SUFFIX}"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["schemaVersion"] = "liveconsole-run/2.0.0"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(replay.ReplayError) as caught:
            replay.load(root, self.runs_root, session_id=SUCCESS)
        self.assertIn("2.0.0", caught.exception.detail)

    def test_every_version_that_records_it_is_a_version_that_is_read(self):
        # Enumerated, not ordered: "1.2.0" sorts above "1.10.0", and a reader
        # comparing version strings would one day decide a run that carried the
        # supplementary half had not recorded it.
        for version in lc.VERSIONS_WITH_SUPPLEMENTARY:
            with self.subTest(version=version):
                self.assertIn(version, lc.SUPPORTED_SCHEMA_VERSIONS)
        self.assertIn(lc.SUPPLEMENTARY_SINCE, lc.VERSIONS_WITH_SUPPLEMENTARY)

    def test_the_writer_and_the_reader_name_the_same_version(self):
        """The defect this closes: two files, one constant, no drift."""
        writer = (REPO_ROOT / "tools" / "liveconsole" / "build.py").read_text(
            encoding="utf-8")
        emitted = re.findall(r'"schemaVersion": "(liveconsole-run/[\d.]+)"',
                             writer)
        self.assertTrue(emitted, "the live writer names no run schema version")
        for version in emitted:
            with self.subTest(version=version):
                self.assertIn(version, lc.SUPPORTED_SCHEMA_VERSIONS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
