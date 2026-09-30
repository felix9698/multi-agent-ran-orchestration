"""The experiment-run replay source, and the Replay façade over both adapters.

Hermetic: committed fixtures plus a temp run root.

``experiment_results/`` is gitignored and empty on every machine in this
project, so there is no found data to validate against.  Both fixtures are
committed for exactly that reason - see ``tests/gui/fixtures/PROVENANCE.md``.
"""

import json
import tempfile
import unittest
from pathlib import Path

from gui.operator.sources import replay
from gui.operator.sources.adapters import experiments_run as er
from gui.operator.store.session_store import SessionStore

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "gui" / "fixtures"
SYNTHETIC = FIXTURES / "experiment-run-min"
LIVE = FIXTURES / "experiment-run-live-min"
CAPTURE = FIXTURES / "lo1-capture-min"


class _Loaded(unittest.TestCase):
    """Loads a fixture once per test into a temp run root."""

    fixture = SYNTHETIC

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runs_root = Path(self._tmp.name)
        self.store = replay.load(self.fixture, self.runs_root)
        self.addCleanup(self.store.close)
        self.manifest = self.store.read_manifest()
        self.samples = list(self.store.read_telemetry())
        self.index = self.store.read_metric_index()
        self.summary = self.store.read_summary()


class SyntheticRun(_Loaded):
    fixture = SYNTHETIC

    def test_the_session_is_replay_and_the_recording_mode_is_separate(self):
        """Reading a recording back is a Replay session, always."""
        self.assertEqual(self.manifest["mode"], "REPLAY")
        self.assertEqual(self.summary["sourceMode"], "SYNTHETIC")
        meta = self.store.read_config_snapshot()["meta"]
        self.assertEqual(meta["mode"], "synthetic")
        self.assertEqual(
            self.store.read_config_snapshot()["source"]["sourceMode"],
            "SYNTHETIC")

    def test_relative_time_is_preserved_and_no_clock_is_invented(self):
        self.assertTrue(self.samples)
        for sample in self.samples:
            self.assertIsNone(sample["tUtc"],
                              "StepRecord carries no wall clock; none may be made up")
            self.assertIsNotNone(sample["tRelS"])
        self.assertEqual(min(s["tRelS"] for s in self.samples), 0.0)

    def test_the_goal_line_comes_from_the_run_not_a_constant(self):
        """8.0 offline vs 3.5 live: a constant would mislabel half the runs."""
        self.assertEqual(self.summary["goal"]["targetMbps"], 8.0)
        self.assertIn("config-snapshot", self.summary["goal"]["source"])

    def test_legacy_ambiguous_radio_keys_are_not_mapped_onto_a_direction(self):
        """UL and DL are separate series; 'rsrp' does not say which it is."""
        for metric in ("ue_dl_ss_rsrp_dbm", "gnb_ul_avg_rsrp_dbm",
                       "ue_dl_sinr_db", "gnb_ul_snr_db"):
            with self.subTest(metric=metric):
                self.assertEqual(self.index[metric]["status"], "UNAVAILABLE")
                self.assertEqual(
                    [s for s in self.samples if s["metric"] == metric], [])
        issues = [i["detail"] for i in self.manifest["dataIssues"]]
        self.assertTrue(any("'rsrp'" in d for d in issues))
        self.assertTrue(any("'sinr'" in d for d in issues))

    def test_source_class_is_experiment_record_not_an_oran_boundary(self):
        for sample in self.samples:
            self.assertEqual(sample["source"]["boundary"], "EXPERIMENT_RECORD")
        self.assertEqual(self.summary["sourceClass"], "EXPERIMENT_RECORD")

    def test_capture_only_metrics_are_unsupported_here(self):
        self.assertEqual(self.index["RRU.PrbDl"]["status"], "UNSUPPORTED")
        self.assertTrue(self.index["RRU.PrbDl"]["reason"])

    def test_decision_records_carry_the_raw_and_calibrated_split(self):
        cycles = list(self.store.read_cycles())
        self.assertEqual(len(cycles), 4)
        for cycle in cycles:
            self.assertEqual(cycle["thresholdAppliedTo"], "calibrated_probability")
            self.assertIsNotNone(cycle["rawConfidence"])
            self.assertIsNotNone(cycle["calibratedProbability"])
            self.assertIsNotNone(cycle["threshold"])

    def test_llm_calls_are_recorded_without_any_model_text(self):
        calls = list(self.store.read_llm_calls())
        self.assertTrue(calls)
        forbidden = {"prompt", "response", "reasoning", "reasoning_content",
                     "thinking", "content", "messages"}
        for call in calls:
            self.assertEqual(set(call) & forbidden, set())
            self.assertIn(call["stage"], ("PARSE", "FEASIBILITY", "ALTERNATIVES"))

    def test_paper_readiness_is_shown_rather_than_hidden(self):
        self.assertIs(self.summary["paperReady"], False)
        self.assertIn("paired", self.summary["paperExportNote"])

    def test_statistics_come_from_the_paper_pipeline(self):
        from experiments.metrics import confidence_interval

        statistics = self.store.read_statistics()
        entry = statistics["throughput_mbps"]["ue1"]
        values = [s["value"] for s in self.samples
                  if s["metric"] == "throughput_mbps"
                  and s["scope"]["id"] == "ue1" and s["value"] is not None]
        expected = confidence_interval(values)
        self.assertEqual(entry["n"], expected["n"])
        self.assertAlmostEqual(entry["mean"], expected["mean"], places=12)
        self.assertAlmostEqual(entry["ci95"]["low"], expected["low"], places=12)

    def test_phase_boundaries_become_derived_events(self):
        phases = [e for e in self.store.read_events()
                  if e["kind"] == "PHASE_CHANGE"]
        # two trials over the five legacy phases: a repeated phase NAME in a
        # second trial is a distinct boundary, not a duplicate to collapse
        self.assertEqual(len(phases), 10)
        for event in phases:
            self.assertEqual(event["origin"], "DERIVED")
            self.assertIsNone(event["tUtc"])
            self.assertTrue(event["derivation"])


class LiveRadioRun(_Loaded):
    """`_meta.mode = live` - the run is LIVE and the numbers still are not O-RAN."""

    fixture = LIVE

    def test_a_recorded_live_run_is_a_replay_session(self):
        """The defect this replaces: _meta.mode=live gave manifest.mode LIVE.

        The badge, the export banner and the figure watermark all follow
        manifest.mode, so a recording that carried LIVE there read as live.
        """
        self.assertEqual(self.manifest["mode"], "REPLAY")
        self.assertEqual(self.summary["sourceMode"], "LIVE")
        self.assertIn("REPLAY", self.summary["sourceModeNote"])

    def test_the_source_class_is_not_an_oran_boundary_either(self):
        self.assertEqual(self.summary["sourceClass"], "EXPERIMENT_RECORD")
        for sample in self.samples:
            self.assertEqual(sample["source"]["boundary"], "EXPERIMENT_RECORD")

    def test_the_run_names_the_recording_it_replays(self):
        evidence = self.manifest["modeEvidence"]
        self.assertEqual(evidence["basis"], "REPLAY_OF_RECORDED_SOURCE")
        self.assertEqual(evidence["sourceRunIds"], ["20260102_000000"])

    def test_illustrative_fixture_values_are_declared_as_such(self):
        """The producer's own note about the data travels with the run."""
        note = self.summary["sourceProvenanceNote"]
        self.assertIn("illustrative, not measured", note)
        self.assertIn("illustrative", self.manifest["sources"][0]["notes"])

    def test_the_honest_per_direction_keys_are_mapped(self):
        for metric in ("gnb_ul_avg_rsrp_dbm", "ue_dl_ss_rsrp_dbm",
                       "gnb_ul_snr_db", "ue_dl_sinr_db"):
            with self.subTest(metric=metric):
                self.assertEqual(self.index[metric]["status"], "OK")
                self.assertTrue(
                    [s for s in self.samples if s["metric"] == metric])

    def test_ul_and_dl_never_share_a_series(self):
        ul = {s["value"] for s in self.samples
              if s["metric"] == "gnb_ul_avg_rsrp_dbm"}
        dl = {s["value"] for s in self.samples
              if s["metric"] == "ue_dl_ss_rsrp_dbm"}
        self.assertTrue(ul and dl)
        self.assertNotEqual(ul, dl)

    def test_a_null_throughput_stays_null_and_degrades_the_metric(self):
        nulls = [s for s in self.samples
                 if s["metric"] == "throughput_mbps" and s["value"] is None]
        self.assertEqual(len(nulls), 1)
        self.assertEqual(nulls[0]["quality"], "MISSING")
        self.assertEqual(self.index["throughput_mbps"]["status"], "DEGRADED")
        self.assertEqual(
            self.index["throughput_mbps"]["qualityCounts"]["MISSING"], 1)

    def test_the_live_goal_is_the_live_target(self):
        self.assertEqual(self.summary["goal"]["targetMbps"], 3.5)

    def test_the_terminal_outcome_maps_to_its_eq12_state(self):
        episodes = list(self.store.read_episodes())
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["terminalOutcome"], "commit_original")
        self.assertEqual(episodes[0]["eq12State"], "Admitted")
        self.assertEqual(self.summary["outcomes"], {"Admitted": 1})

    def test_an_epoch_stamped_event_keeps_its_wall_clock(self):
        applied = [e for e in self.store.read_events()
                   if e["kind"] == "ACTION_APPLIED"]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["origin"], "OBSERVED")
        self.assertTrue(applied[0]["tUtc"].endswith("Z"))

    def test_statistics_record_how_many_nulls_were_excluded(self):
        entry = self.store.read_statistics()["throughput_mbps"]["ue2"]
        self.assertEqual(entry["excludedNullSamples"], 1)
        self.assertEqual(entry["n"], 8)


class SourceDiscovery(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_layout_detection(self):
        self.assertEqual(replay.detect_adapter(CAPTURE), replay.LO1_CAPTURE)
        self.assertEqual(replay.detect_adapter(SYNTHETIC), replay.EXPERIMENT_RUN)
        self.assertIsNone(replay.detect_adapter(self.tmp))
        self.assertIsNone(replay.detect_adapter(self.tmp / "nope"))

    def test_session_listing(self):
        self.assertEqual(er.list_sessions(SYNTHETIC), ["20260101_000000"])
        self.assertEqual(replay.list_sessions(CAPTURE),
                         ["sc084-pc1-upper-109-20260813T141759Z"])

    def test_an_unrecognised_directory_is_refused_with_its_issue_kind(self):
        with self.assertRaises(replay.ReplayError) as raised:
            replay.load(self.tmp, self.tmp / "runs")
        self.assertEqual(raised.exception.kind, "CONNECTION_LOST")

    def test_an_unknown_meta_mode_is_refused_rather_than_defaulted(self):
        results = self.tmp / "results"
        results.mkdir()
        metrics = json.loads(
            (SYNTHETIC / "experiment_20260101_000000_metrics.json")
            .read_text(encoding="utf-8"))
        metrics["_meta"]["mode"] = "probably_live"
        (results / "experiment_x_metrics.json").write_text(
            json.dumps(metrics), encoding="utf-8")
        with self.assertRaises(replay.ReplayError):
            replay.load(results, self.tmp / "runs", session_id="x")

    def test_an_absent_meta_mode_loads_as_synthetic_with_an_issue(self):
        results = self.tmp / "results"
        results.mkdir()
        metrics = json.loads(
            (SYNTHETIC / "experiment_20260101_000000_metrics.json")
            .read_text(encoding="utf-8"))
        del metrics["_meta"]["mode"]
        (results / "experiment_x_metrics.json").write_text(
            json.dumps(metrics), encoding="utf-8")
        store = replay.load(results, self.tmp / "runs", session_id="x")
        self.addCleanup(store.close)
        self.assertEqual(store.mode, "REPLAY")
        self.assertEqual(store.read_summary()["sourceMode"], "SYNTHETIC")
        self.assertTrue(any("_meta.mode" in i["detail"]
                            for i in store.read_manifest()["dataIssues"]))

    def test_the_facade_offers_no_way_to_set_the_mode(self):
        """There is no UI control and no argument that changes manifest.mode."""
        import inspect

        signature = inspect.signature(replay.load)
        self.assertNotIn("mode", signature.parameters)

    def test_each_source_declares_what_it_cannot_show(self):
        for source_id, description in replay.SOURCE_DESCRIPTIONS.items():
            with self.subTest(source=source_id):
                self.assertTrue(description["carries"])
                self.assertTrue(description["cannotShow"])


class ReopenAfterRestart(unittest.TestCase):
    """Section 9: a completed run re-opens from the directory alone."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runs_root = Path(self._tmp.name)

    def test_a_finalized_run_reopens_with_identical_content(self):
        store = replay.load(LIVE, self.runs_root)
        store.close()
        original = {
            "manifest": store.read_manifest(),
            "telemetry": list(store.read_telemetry()),
            "events": list(store.read_events()),
            "episodes": list(store.read_episodes()),
            "summary": store.read_summary(),
            "statistics": store.read_statistics(),
        }

        reopened = SessionStore.open(store.run_dir)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.disposition, "COMPLETED")
        self.assertEqual(reopened.mode, "REPLAY")
        self.assertEqual(list(reopened.read_telemetry()), original["telemetry"])
        self.assertEqual(list(reopened.read_events()), original["events"])
        self.assertEqual(list(reopened.read_episodes()), original["episodes"])
        self.assertEqual(reopened.read_summary(), original["summary"])
        self.assertEqual(reopened.read_statistics(), original["statistics"])

    def test_loading_the_same_source_twice_yields_the_same_content(self):
        """Reproducibility: the same stored run regenerates the same numbers."""
        first = replay.load(SYNTHETIC, self.runs_root)
        second = replay.load(SYNTHETIC, self.runs_root)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(list(first.read_telemetry()), list(second.read_telemetry()))
        self.assertEqual(list(first.read_episodes()), list(second.read_episodes()))
        self.assertEqual(first.read_statistics(), second.read_statistics())
        self.assertEqual(first.read_metric_index(), second.read_metric_index())

    def test_the_source_directory_is_never_written_to(self):
        before = {p: p.stat().st_mtime_ns for p in sorted(SYNTHETIC.rglob("*"))
                  if p.is_file()}
        store = replay.load(SYNTHETIC, self.runs_root)
        self.addCleanup(store.close)
        after = {p: p.stat().st_mtime_ns for p in sorted(SYNTHETIC.rglob("*"))
                 if p.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
