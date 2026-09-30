"""Export: reproducibility, paper-grade form, and metric honesty.

Hermetic: committed fixtures, a temp run root, matplotlib on the Agg backend.
No display, no hardware, no network.

G-REPRODUCIBILITY and G-METRIC-HONESTY in one file, because they are the same
question asked twice: can a reader reconstruct exactly what was plotted, and can
the figure lie about it.
"""

import json
import tempfile
import unittest
from pathlib import Path

from gui.operator.export import data_export
from gui.operator.export import figure_export as fx
from gui.operator.sources import replay
from gui.operator.store.session_store import SessionStore, SessionStoreError

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "gui" / "fixtures"
LIVE = FIXTURES / "experiment-run-live-min"
CAPTURE = FIXTURES / "lo1-capture-min"

STAMP = "2026-01-01T00:00:00Z"


def _spec(**overrides):
    fields = dict(figure_id="i2-throughput", title="UE DL throughput",
                  x_label="elapsed", x_unit="s", y_label="throughput",
                  y_unit="Mbps", goal_value=3.5, goal_label="I2 target")
    fields.update(overrides)
    return fx.FigureSpec(**fields)


class _Run(unittest.TestCase):
    fixture = LIVE

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        store = replay.load(self.fixture, self.tmp / "runs")
        store.close()
        self.run_dir = store.run_dir
        self.mode = store.mode

    def analysis(self):
        store = SessionStore.reopen_for_analysis(self.run_dir)
        self.addCleanup(store.close)
        return store

    def throughput_series(self, store):
        samples = list(store.read_telemetry())
        return [fx.series_from_samples(samples, metric="throughput_mbps",
                                       scope_id=ue, run_id=store.run_id)
                for ue in ("ue1", "ue2")]


class Reproducibility(_Run):
    def test_re_exporting_yields_identical_source_bytes(self):
        store = self.analysis()
        series = self.throughput_series(store)
        fx.export_figure(store, _spec(), series, generated_at=STAMP)
        first_csv = (self.run_dir / "figures" / "i2-throughput.source.csv").read_bytes()
        first_meta = json.loads(
            (self.run_dir / "figures" / "i2-throughput.meta.json").read_text())

        fx.export_figure(store, _spec(), series, generated_at="2026-06-06T06:06:06Z")
        second_csv = (self.run_dir / "figures" / "i2-throughput.source.csv").read_bytes()
        second_meta = json.loads(
            (self.run_dir / "figures" / "i2-throughput.meta.json").read_text())

        self.assertEqual(first_csv, second_csv)
        first_meta.pop("generatedAt")
        second_meta.pop("generatedAt")
        self.assertEqual(first_meta, second_meta,
                         "only the generation timestamp may differ")

    def test_re_opening_and_regenerating_the_summary_yields_the_same_numbers(self):
        first = SessionStore.open(self.run_dir)
        self.addCleanup(first.close)
        second = SessionStore.open(self.run_dir)
        self.addCleanup(second.close)
        self.assertEqual(first.read_summary(), second.read_summary())
        self.assertEqual(first.read_statistics(), second.read_statistics())
        self.assertEqual(list(first.read_telemetry()), list(second.read_telemetry()))

    def test_the_statistics_equal_the_paper_pipeline(self):
        from experiments.metrics import confidence_interval

        store = SessionStore.open(self.run_dir)
        self.addCleanup(store.close)
        values = [s["value"] for s in store.read_telemetry()
                  if s["metric"] == "throughput_mbps"
                  and s["scope"]["id"] == "ue1" and s["value"] is not None]
        expected = confidence_interval(values)
        entry = store.read_statistics()["throughput_mbps"]["ue1"]
        self.assertEqual(entry["n"], expected["n"])
        self.assertAlmostEqual(entry["mean"], expected["mean"], places=12)

    def test_a_finalized_run_keeps_its_disposition_after_an_analysis_session(self):
        store = self.analysis()
        fx.export_figure(store, _spec(), self.throughput_series(store),
                         generated_at=STAMP)
        manifest = store.finalize_analysis()
        self.assertEqual(manifest["disposition"], "COMPLETED")
        self.assertIn("figures/i2-throughput.meta.json", manifest["artifacts"])

    def test_analysis_mode_refuses_to_rewrite_evidence(self):
        store = self.analysis()
        from gui.operator.store import records as rec

        with self.assertRaises(SessionStoreError):
            store.append_telemetry(rec.telemetry_sample(
                seq=0, metric="throughput_mbps", value=1.0, unit="Mbps",
                scope_level="UE", scope_id="ue1", boundary="EXPERIMENT_RECORD",
                t_rel_s=0.0, quality="OK"))
        with self.assertRaises(SessionStoreError):
            store.append_event(rec.timeline_event(seq=0, lane="SESSION",
                                                  kind="SESSION_STARTED"))

    def test_a_read_only_run_refuses_a_figure_until_reopened_for_analysis(self):
        store = SessionStore.open(self.run_dir)
        self.addCleanup(store.close)
        with self.assertRaises(SessionStoreError):
            fx.export_figure(store, _spec(), self.throughput_series(store),
                             generated_at=STAMP)


class FigureMetadataIsComplete(_Run):
    def setUp(self):
        super().setUp()
        self.store = self.analysis()
        fx.export_figure(self.store, _spec(), self.throughput_series(self.store),
                         generated_at=STAMP)
        self.meta = json.loads(
            (self.run_dir / "figures" / "i2-throughput.meta.json").read_text())

    def test_every_series_carries_its_sample_count(self):
        self.assertTrue(self.meta["series"])
        for entry in self.meta["series"]:
            self.assertIn("n", entry)
            self.assertGreater(entry["n"], 0)

    def test_axes_carry_labels_and_units(self):
        for axis in ("x", "y"):
            self.assertTrue(self.meta["axes"][axis]["label"])
            self.assertTrue(self.meta["axes"][axis]["unit"])

    def test_the_processing_block_is_complete(self):
        processing = self.meta["processing"]
        for field in fx.REQUIRED_PROCESSING:
            self.assertTrue(processing.get(field), field)

    def test_an_incomplete_processing_block_is_refused(self):
        with self.assertRaises(fx.FigureExportError):
            fx.export_figure(self.store,
                             _spec(figure_id="bad", processing={"smoothing": "none"}),
                             self.throughput_series(self.store))

    def test_the_store_refuses_an_incomplete_block_too(self):
        """Two gates: the store is what actually writes the sidecar."""
        with self.assertRaises(SessionStoreError):
            self.store.write_figure("bad", paths=[], source_csv="x.csv",
                                    metadata={"processing": {"smoothing": "none"}})

    def test_series_differ_by_more_than_colour(self):
        styles = {(e["color"], e["linestyle"], e["marker"])
                  for e in self.meta["series"]}
        self.assertEqual(len(styles), len(self.meta["series"]))
        markers = {e["marker"] for e in self.meta["series"]}
        self.assertEqual(len(markers), len(self.meta["series"]))
        self.assertTrue(self.meta["colorSafe"])

    def test_the_mode_and_run_identity_travel_with_the_figure(self):
        self.assertEqual(self.meta["mode"], "REPLAY")
        self.assertIn(self.store.run_id, self.meta["runIds"])

    def test_vector_and_high_resolution_raster_are_both_produced(self):
        figures = self.run_dir / "figures"
        for suffix in (".pdf", ".svg", ".png"):
            self.assertTrue((figures / f"i2-throughput{suffix}").is_file(), suffix)
        self.assertGreater((figures / "i2-throughput.png").stat().st_size, 10_000,
                           "a 300 dpi raster is not a thumbnail")


class MetricHonesty(_Run):
    def test_a_null_exports_as_an_empty_field_never_as_zero(self):
        store = self.analysis()
        fx.export_figure(store, _spec(), self.throughput_series(store),
                         generated_at=STAMP)
        rows = (self.run_dir / "figures" / "i2-throughput.source.csv").read_text(
            encoding="utf-8").splitlines()
        data = [r for r in rows if r.startswith("throughput_mbps:ue2")]
        empty = [r for r in data if r.split(",")[5] == ""]
        self.assertEqual(len(empty), 1, "the null sample must stay empty")
        self.assertFalse([r for r in data if r.split(",")[5] == "0"],
                         "a gap must never be exported as a zero")

    def test_an_unavailable_series_stays_in_the_legend_with_its_status(self):
        store = self.analysis()
        series = self.throughput_series(store) + [fx.SeriesData(
            series_id="DRB.UEThpDl:cell-1", label="DL UE throughput (O1)",
            points=(), unit="kbps", status="UNAVAILABLE",
            status_reason="subscribed in the PerfMetricJob, never delivered")]
        fx.export_figure(store, _spec(figure_id="with-unavailable"), series,
                         generated_at=STAMP)
        meta = json.loads(
            (self.run_dir / "figures" / "with-unavailable.meta.json").read_text())
        labels = [e["label"] for e in meta["series"]]
        self.assertIn("DL UE throughput (O1)", labels)
        entry = next(e for e in meta["series"]
                     if e["label"] == "DL UE throughput (O1)")
        self.assertEqual(entry["n"], 0)

    def test_a_derived_series_must_be_exported_as_a_derived_visualization(self):
        store = self.analysis()
        derived = fx.SeriesData(
            series_id="constellation_derived:ue1", label="Modulation quality",
            points=((0.0, 0.12), (15.0, 0.15), (30.0, 0.11)),
            unit="EVM-equivalent", derived=True)
        with self.assertRaises(fx.FigureExportError):
            fx.export_figure(store, _spec(figure_id="derived-unlabelled"),
                             [derived], generated_at=STAMP)

        fx.export_figure(store,
                         _spec(figure_id="derived-labelled",
                               derived_visualization=True, goal_value=None),
                         [derived], generated_at=STAMP)
        meta = json.loads(
            (self.run_dir / "figures" / "derived-labelled.meta.json").read_text())
        self.assertTrue(meta["derivedVisualization"])

    def test_claiming_a_derived_visualization_without_a_derived_series_is_refused(self):
        store = self.analysis()
        with self.assertRaises(fx.FigureExportError):
            fx.export_figure(store,
                             _spec(figure_id="not-really-derived",
                                   derived_visualization=True),
                             self.throughput_series(store), generated_at=STAMP)


class SparseCaptureData(unittest.TestCase):
    """Two RRU.PrbDl scalars are two points, not a trend."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        store = replay.load(CAPTURE, self.tmp / "runs")
        store.close()
        self.run_dir = store.run_dir
        self.store = SessionStore.reopen_for_analysis(self.run_dir)
        self.addCleanup(self.store.close)

    def test_a_two_point_series_is_drawn_as_points_with_a_sparse_note(self):
        samples = list(self.store.read_telemetry())
        series = [fx.series_from_samples(samples, metric="RRU.PrbDl",
                                         run_id=self.store.run_id)]
        self.assertTrue(series[0].is_sparse)
        self.assertEqual(series[0].n, 2)

        fx.export_figure(self.store,
                         _spec(figure_id="prb", title="DL PRB utilization",
                               x_label="sample", x_unit="", y_label="PRB",
                               y_unit="percent", goal_value=None),
                         series, generated_at=STAMP)
        meta = json.loads((self.run_dir / "figures" / "prb.meta.json").read_text())
        self.assertEqual(meta["series"][0]["linestyle"], "none",
                         "two points must not be joined into a line")
        self.assertIn("sparse", meta["processing"]["exclusions"])

    def test_a_replay_figure_carries_the_mode_watermark_inside_the_figure(self):
        samples = list(self.store.read_telemetry())
        series = [fx.series_from_samples(samples, metric="RRU.PrbDl",
                                         run_id=self.store.run_id)]
        fx.export_figure(self.store,
                         _spec(figure_id="prb-mode", x_unit="", y_unit="percent",
                               goal_value=None), series, generated_at=STAMP)
        svg = (self.run_dir / "figures" / "prb-mode.svg").read_text(
            encoding="utf-8")
        self.assertIn("REPLAY", svg,
                      "a screenshot must not be able to lose the mode")


class DataExport(_Run):
    def setUp(self):
        super().setUp()
        self.store = SessionStore.open(self.run_dir)
        self.addCleanup(self.store.close)
        self.dest = self.tmp / "export"
        self.manifest = data_export.export_run(self.store, self.dest)

    def test_every_exported_file_names_its_mode_and_run(self):
        for path in sorted(self.dest.rglob("*")):
            if not path.is_file() or path.suffix not in (".csv", ".json"):
                continue
            if "raw" in path.parts:
                continue                      # raw is byte-identical by contract
            payload = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertIn(self.store.mode, payload)
                self.assertIn(self.store.run_id, payload)

    def test_raw_artifacts_export_byte_identically(self):
        source = self.run_dir / "raw"
        exported = self.dest / "raw"
        pairs = 0
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(source)
            self.assertTrue((exported / rel).is_file(), str(rel))
            self.assertEqual((exported / rel).read_bytes(), path.read_bytes())
            pairs += 1
        self.assertGreater(pairs, 0)

    @staticmethod
    def _table(path):
        """Header and body of an exported CSV.

        The banner is variable-length - it grows a line when the run has a
        source mode or a provenance note - so the header is found by name, not
        by row index.
        """
        rows = [r for r in path.read_text(encoding="utf-8").splitlines() if r]
        index = next(i for i, r in enumerate(rows) if not r.startswith("#"))
        return rows[index].split(","), [r.split(",") for r in rows[index + 1:]]

    def test_a_null_telemetry_value_exports_as_an_empty_cell(self):
        header, body = self._table(self.dest / "telemetry.csv")
        value_index = header.index("value")
        empties = [r for r in body if r[value_index] == ""]
        self.assertTrue(empties, "the null sample must survive the export")
        for row in empties:
            self.assertEqual(row[header.index("quality")], "MISSING")

    def test_the_export_manifest_carries_the_data_issues(self):
        self.assertIn("dataIssues", self.manifest)
        self.assertIs(self.manifest["isSuccess"], True)
        self.assertIn("EXPORT-MANIFEST.json",
                      [p.name for p in self.dest.iterdir()])

    def test_re_exporting_is_byte_stable(self):
        second = self.tmp / "export-2"
        data_export.export_run(self.store, second)
        for name in ("telemetry.csv", "events.csv", "episodes.csv",
                     "cycles.csv", "telemetry.json", "summary.json"):
            with self.subTest(file=name):
                self.assertEqual((self.dest / name).read_bytes(),
                                 (second / name).read_bytes())

    def test_an_unsupported_format_is_refused(self):
        with self.assertRaises(data_export.ExportError):
            data_export.export_run(self.store, self.tmp / "x", formats=("xlsx",))


class AbortedRunExport(unittest.TestCase):
    """A partial run exports its data and is never summarized as success."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_an_aborted_run_exports_with_is_success_false(self):
        from gui.operator.store import records as rec

        store = SessionStore.create(
            self.tmp / "runs", mode="REPLAY",
            mode_evidence={"basis": "REPLAY_OF_RECORDED_SOURCE"})
        store.append_telemetry(rec.telemetry_sample(
            seq=0, metric="RRU.PrbDl", value=30.0, unit="percent",
            scope_level="CELL", scope_id="NRCellDU=1", boundary="O1_ASSURANCE",
            t_utc="2026-08-13T14:20:00Z", quality="OK"))
        store.record_issue("PARTIAL_DATA", "the operator aborted the session")
        store.finalize("ABORTED")

        manifest = data_export.export_run(store, self.tmp / "export")
        self.assertEqual(manifest["disposition"], "ABORTED")
        self.assertIs(manifest["isSuccess"], False)
        self.assertTrue(manifest["dataIssues"])
        self.assertTrue((self.tmp / "export" / "telemetry.csv").is_file(),
                        "an aborted run keeps its data")


class ARecordedLiveRunNeverExportsAsLive(unittest.TestCase):
    """Negative test for the Replay-reads-as-LIVE defect.

    ``experiment-run-live-min`` is a recording whose ``_meta.mode`` is ``live``.
    Opening it is a Replay session, and every artefact a reader could receive on
    its own - the CSV banner, the JSON wrapper, the export manifest, the figure
    metadata and the rendered SVG - must say REPLAY.  The recording's own mode
    is present, but as supplementary provenance that cannot be mistaken for the
    session.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        store = replay.load(LIVE, self.tmp / "runs")
        store.close()
        self.run_dir = store.run_dir

        analysis = SessionStore.reopen_for_analysis(self.run_dir)
        samples = list(analysis.read_telemetry())
        series = [fx.series_from_samples(samples, metric="throughput_mbps",
                                         scope_id="ue1",
                                         run_id=analysis.run_id)]
        fx.export_figure(analysis, _spec(), series, generated_at=STAMP)
        analysis.finalize_analysis()
        analysis.close()

        self.store = SessionStore.open(self.run_dir)
        self.addCleanup(self.store.close)
        self.dest = self.tmp / "export"
        self.manifest = data_export.export_run(self.store, self.dest)

    def test_the_session_mode_is_replay_not_live(self):
        self.assertEqual(self.store.mode, "REPLAY")
        self.assertEqual(self.store.read_summary()["sourceMode"], "LIVE")

    def test_every_exported_csv_banner_says_replay(self):
        for name in ("telemetry.csv", "events.csv", "episodes.csv",
                     "cycles.csv"):
            with self.subTest(file=name):
                banner = [line for line in (self.dest / name).read_text(
                    encoding="utf-8").splitlines() if line.startswith("#")]
                text = "\n".join(banner)
                self.assertIn("mode=REPLAY", text)
                self.assertNotIn("mode=LIVE", text)
                self.assertIn("sourceMode=LIVE", text)

    def test_the_export_manifest_leads_with_the_session_mode(self):
        self.assertEqual(self.manifest["mode"], "REPLAY")
        self.assertEqual(self.manifest["sourceMode"], "LIVE")
        self.assertIn("illustrative, not measured",
                      self.manifest["sourceProvenanceNote"])
        self.assertIs(self.manifest["paperReady"], False)

    def test_the_json_export_carries_the_session_mode(self):
        document = json.loads((self.dest / "telemetry.json").read_text())
        self.assertEqual(document["mode"], "REPLAY")

    def test_the_figure_metadata_and_watermark_say_replay(self):
        meta = json.loads(
            (self.run_dir / "figures" / "i2-throughput.meta.json").read_text())
        self.assertEqual(meta["mode"], "REPLAY")
        self.assertIn("REPLAY", meta["caption"])
        self.assertIn("recorded LIVE run", meta["caption"])
        self.assertIn("illustrative, not measured", meta["caption"])

        svg = (self.run_dir / "figures" / "i2-throughput.svg").read_text(
            encoding="utf-8")
        self.assertIn("REPLAY", svg,
                      "a recorded LIVE run must still be watermarked")

    def test_no_exported_artifact_claims_the_session_was_live(self):
        for path in sorted(self.dest.rglob("*")):
            if not path.is_file() or path.suffix not in (".csv", ".json"):
                continue
            if "raw" in path.parts:
                continue
            with self.subTest(file=path.name):
                self.assertNotIn('"mode": "LIVE"',
                                 path.read_text(encoding="utf-8"))


class AnUndeclaredMetricCannotReadAsAMeasurement(unittest.TestCase):
    """Negative test for the detached-export defect.

    A metric neither the registry nor the capability manifest declares is
    ``UNKNOWN`` in the index.  Its value may still be exported - dropping data is
    its own dishonesty - but the row has to carry the status and the reason, or a
    detached CSV shows a bare number that reads as a sound measurement.
    """

    METRIC = "made.up.kpi"

    def setUp(self):
        from gui.operator.store import records as rec
        from gui.operator.store.metric_index import MetricIndexBuilder

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        store = SessionStore.create(
            self.tmp / "runs", mode="REPLAY",
            mode_evidence={"basis": "REPLAY_OF_RECORDED_SOURCE"})
        store.append_telemetry(rec.telemetry_sample(
            seq=0, metric=self.METRIC, value=123.0, unit="widgets",
            scope_level="RUN", scope_id="run-1", boundary="RAPP_INTERNAL",
            t_rel_s=0.0, quality="OK"))
        builder = MetricIndexBuilder(source_id="lo1-capture")
        builder.observe_all(store.read_telemetry())
        store.write_metric_index(builder.build())
        store.write_summary({"runId": store.run_id})
        store.finalize("COMPLETED")
        store.close()

        self.store = SessionStore.open(store.run_dir)
        self.addCleanup(self.store.close)
        self.dest = self.tmp / "export"
        self.manifest = data_export.export_run(self.store, self.dest)

    def _row(self):
        rows = [r for r in (self.dest / "telemetry.csv").read_text(
            encoding="utf-8").splitlines() if r and not r.startswith("#")]
        header = rows[0].split(",")
        body = next(r.split(",") for r in rows[1:] if r.startswith("0,"))
        return dict(zip(header, body))

    def test_the_index_marks_it_unknown(self):
        self.assertEqual(
            self.store.read_metric_index()[self.METRIC]["status"], "UNKNOWN")

    def test_the_exported_row_carries_the_availability_and_the_reason(self):
        row = self._row()
        self.assertEqual(row["metric"], self.METRIC)
        self.assertEqual(row["value"], "123.0")
        self.assertEqual(row["availability"], "UNKNOWN")
        self.assertTrue(row["availabilityReason"],
                        "an UNKNOWN availability without a reason is no better "
                        "than no availability at all")

    def test_availability_precedes_the_value_in_the_row(self):
        header = list(data_export.TELEMETRY_COLUMNS)
        self.assertLess(header.index("availability"), header.index("value"))

    def test_a_metric_absent_from_the_index_fails_closed(self):
        """Absence of an availability record is not evidence of availability."""
        status = data_export.availability_of({}, "anything")
        self.assertEqual(status["availability"], "UNKNOWN")
        self.assertIn("cannot vouch", status["reason"])

    def test_the_json_export_keeps_samples_and_availability_together(self):
        document = json.loads((self.dest / "telemetry.json").read_text())
        payload = document["data"]
        self.assertIn("metricIndex", payload)
        sample = payload["samples"][0]
        self.assertEqual(sample["availability"]["availability"], "UNKNOWN")

    def test_the_export_manifest_reports_availability_per_metric(self):
        entry = self.manifest["metricAvailability"][self.METRIC]
        self.assertEqual(entry["availability"], "UNKNOWN")
        self.assertTrue(entry["reason"])

    def test_quality_can_no_longer_be_defaulted_to_ok(self):
        """The path that filled quality with OK for a caller who did not know."""
        from gui.operator.store import records as rec

        with self.assertRaises(TypeError):
            rec.telemetry_sample(                       # type: ignore[call-arg]
                seq=0, metric="m", value=1.0, unit="u", scope_level="RUN",
                scope_id="r", boundary="RAPP_INTERNAL", t_rel_s=0.0)

    def test_a_source_that_declares_no_quality_says_so(self):
        """AMBIGUOUS, not OK: no assertion is not an assertion of soundness."""
        from gui.operator.sources.adapters.lo1_capture import _declared_quality

        self.assertEqual(_declared_quality({"quality": "OK"}), "OK")
        self.assertEqual(_declared_quality({"quality": "STALE"}), "STALE")
        self.assertEqual(_declared_quality({}), "AMBIGUOUS")


if __name__ == "__main__":
    unittest.main()
