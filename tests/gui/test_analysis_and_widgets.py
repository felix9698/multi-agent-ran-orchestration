"""Analysis workspace, chart, table and metric tile.

Owner: track **T3**.  This file is additive to the four test files named in
``file-ownership.1.0.0.json`` for T3; it covers T3-owned modules
(``workspaces/analysis.py``, ``widgets/charts.py``, ``widgets/tables.py``,
``widgets/metric_tile.py``) that would otherwise ship without one.

Almost everything here is hermetic, because almost everything these modules do
is a *decision* and the decisions live in pure models.  The handful of tests
that need a real toolkit are skipped without a display, matching how the rest of
the suite treats Tk.
"""

import os
import tempfile
import unittest
from pathlib import Path

from gui.operator import status as st
from gui.operator import tokens
from gui.operator.sources import replay
from gui.operator.widgets import charts, metric_tile, tables
from gui.operator.workspaces import analysis

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "gui" / "fixtures"
LIVE = FIXTURES / "experiment-run-live-min"
CAPTURE = FIXTURES / "lo1-capture-min"

HAS_DISPLAY = bool(os.environ.get("DISPLAY"))




class MetricTileHonesty(unittest.TestCase):
    def test_an_unsupported_metric_shows_the_status_not_a_number(self):
        model = metric_tile.MetricTileModel.from_index(
            "mcs", {"status": "UNSUPPORTED", "reason": "no source", "unit": "index",
                    "source": "NONE", "scopeLevel": "UE", "gapId": "GAP-07",
                    "sampleCount": 0},
            display="MCS")
        self.assertFalse(model.shows_a_value)
        self.assertIn("Unsupported", model.value_text())
        self.assertIn("GAP-07", model.reason_text())

    def test_a_never_measured_value_shows_the_placeholder_not_zero(self):
        model = metric_tile.MetricTileModel(metric="throughput_mbps",
                                            display="throughput", status="OK")
        self.assertEqual(model.value_text(), st.PRE_MEASUREMENT)

    def test_zero_is_a_measurement(self):
        model = metric_tile.MetricTileModel(metric="RRU.PrbDl", display="PRB",
                                            status="OK", value=0.0,
                                            unit="percent", decimals=0)
        self.assertEqual(model.value_text(), "0 percent")

    def test_the_provenance_block_states_everything_the_registry_requires(self):
        model = metric_tile.MetricTileModel.from_index(
            "RRU.PrbDl",
            {"status": "OK", "unit": "percent", "source": "O1_DME",
             "scopeLevel": "CELL", "scopeIds": ["NRCellDU=1"],
             "samplingIntervalMs": 60000, "sampleCount": 2,
             "lastSampleAt": "2026-08-13T14:20:00Z"},
            latest={"value": 30, "tUtc": "2026-08-13T14:20:00Z", "ageMs": 585,
                    "quality": "OK", "scope": {"id": "NRCellDU=1"}},
            display="DL PRB utilization")
        lines = "\n".join(model.provenance_lines())
        for required in ("source:", "scope:", "interval:", "observed:", "age:",
                         "quality:", "n:"):
            self.assertIn(required, lines)
        self.assertEqual(model.freshness(), st.FRESH)

    def test_a_non_ok_status_always_has_a_reason_line(self):
        model = metric_tile.MetricTileModel(metric="x", display="x",
                                            status="UNAVAILABLE")
        self.assertTrue(model.reason_text())


class TableSortsOnValuesNotStrings(unittest.TestCase):
    COLUMNS = (
        tables.ColumnSpec("id", "id"),
        tables.ColumnSpec("status", "Status", kind="status"),
        tables.ColumnSpec("n", "n", kind="number", decimals=0),
    )

    def setUp(self):
        self.model = tables.TableModel(columns=self.COLUMNS)
        self.model.set_rows([
            {"id": "a", "status": "OK", "n": 9},
            {"id": "b", "status": "ERROR", "n": 10},
            {"id": "c", "status": "DEGRADED", "n": None},
        ])

    def test_status_sorts_by_severity_so_the_worst_reaches_the_top(self):
        self.model.set_sort("status")
        self.assertEqual([r["id"] for r in self.model.visible()],
                         ["b", "c", "a"])

    def test_numbers_sort_numerically(self):
        self.model.set_sort("n")
        order = [r["id"] for r in self.model.visible()]
        self.assertEqual(order[:2], ["a", "b"])
        self.assertEqual(order[-1], "c", "an unknown sorts last, not smallest")

    def test_an_unmeasured_cell_renders_the_placeholder(self):
        rendered = dict(zip([r["id"] for r in self.model.visible()],
                            self.model.rendered()))
        self.assertEqual(rendered["c"][2], st.PRE_MEASUREMENT)

    def test_zero_renders_as_a_number(self):
        self.model.set_rows([{"id": "z", "status": "OK", "n": 0}])
        self.assertEqual(self.model.rendered()[0][2], "0")

    def test_filtering_is_case_insensitive_and_field_scoped(self):
        self.model.set_filter("error")
        self.assertEqual([r["id"] for r in self.model.visible()], ["b"])
        self.model.set_filter("error", fields=["id"])
        self.assertEqual(self.model.visible(), [])

    def test_the_row_cap_reports_what_it_hid(self):
        self.model.max_rows = 2
        self.assertEqual(len(self.model.visible()), 2)
        self.assertEqual(self.model.overflow(), 1)


class ChartModelHonesty(unittest.TestCase):
    def setUp(self):
        self.model = charts.ChartModel(max_points=5, decimation_threshold=4)
        self.model.set_series([charts.SeriesSpec.styled(0, "s1", "series one")])

    def test_a_null_is_kept_as_a_gap_not_a_zero(self):
        self.model.extend("s1", [(0.0, 1.0), (1.0, None), (2.0, 3.0)])
        values = [v for _x, v in self.model.points["s1"]]
        self.assertEqual(values, [1.0, None, 3.0])
        self.assertNotIn(0.0, [v for v in values if v is not None])

    def test_the_ring_buffer_is_bounded_and_reports_what_it_shed(self):
        self.model.extend("s1", [(float(i), float(i)) for i in range(8)])
        self.assertEqual(len(self.model.points["s1"]), 5)
        self.assertEqual(self.model.dropped, 3)

    def test_a_two_point_series_is_sparse(self):
        self.model.extend("s1", [(0.0, 30.0), (60.0, 40.0)])
        self.assertEqual(self.model.sparse_series(), ["s1"])

    def test_decimation_is_display_only_and_says_so(self):
        model = charts.ChartModel(max_points=100, decimation_threshold=4)
        model.set_series([charts.SeriesSpec.styled(0, "s1", "series one")])
        model.extend("s1", [(float(i), float(i)) for i in range(20)])
        thinned, note = model.decimate(list(model.points["s1"]))
        self.assertLess(len(thinned), 20)
        self.assertIn("exports draw the full series", note)
        self.assertEqual(len(model.series_data()[0].points), 20,
                         "the export path must see the full series")

    def test_decimation_never_drops_a_gap(self):
        model = charts.ChartModel(max_points=100, decimation_threshold=3)
        model.set_series([charts.SeriesSpec.styled(0, "s1", "series one")])
        points = [(float(i), None if i == 7 else float(i)) for i in range(20)]
        model.extend("s1", points)
        thinned, _note = model.decimate(list(model.points["s1"]))
        self.assertIn((7.0, None), thinned)

    def test_an_unavailable_series_keeps_its_status_and_drops_its_points(self):
        self.model.extend("s1", [(0.0, 1.0)])
        self.model.set_unavailable("s1", "UNAVAILABLE", "never delivered")
        self.assertEqual(self.model.specs["s1"].status, "UNAVAILABLE")
        self.assertEqual(len(self.model.points["s1"]), 0)
        self.assertEqual(self.model.series_data()[0].status, "UNAVAILABLE")

    def test_the_window_selects_a_tail_without_altering_the_data(self):
        self.model.extend("s1", [(float(i), float(i)) for i in range(5)])
        self.model.set_window(2.0)
        self.assertEqual([x for x, _v in self.model.windowed("s1")],
                         [2.0, 3.0, 4.0])
        self.assertEqual(len(self.model.points["s1"]), 5)

    def test_only_placeable_annotations_are_drawn(self):
        self.model.set_annotations([
            {"annotatable": True, "kind": "INTENT_SUBMITTED", "tRelS": 15.0},
            {"annotatable": True, "kind": "ROLLBACK", "tRelS": None},
            {"annotatable": False, "kind": "RPC_SENT", "tRelS": 1.0},
        ])
        self.assertEqual([a["kind"] for a in self.model.annotations],
                         ["INTENT_SUBMITTED"])

    def test_series_styles_differ_by_more_than_colour(self):
        first = charts.SeriesSpec.styled(0, "a", "a")
        second = charts.SeriesSpec.styled(1, "b", "b")
        self.assertNotEqual(first.color, second.color)
        self.assertNotEqual(first.linestyle, second.linestyle)
        self.assertNotEqual(first.marker, second.marker)


class FrameBudgetDegradesRateNeverData(unittest.TestCase):
    def test_redraws_are_capped_and_skips_are_counted(self):
        now = [0.0]
        budget = charts.FrameBudget(max_hz=4.0, clock=lambda: now[0])
        self.assertTrue(budget.allow())
        for _ in range(10):
            now[0] += 0.01
            self.assertFalse(budget.allow())
        self.assertEqual(budget.skipped, 10)
        now[0] += 1.0
        self.assertTrue(budget.allow())


class AnalysisModelBehaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        live = replay.load(LIVE, root / "runs")
        capture = replay.load(CAPTURE, root / "runs")
        cls.live_view = analysis.load_run_view(live)
        cls.capture_view = analysis.load_run_view(capture)
        live.close()
        capture.close()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.model = analysis.AnalysisModel()
        self.model.add_run(self.live_view, primary=True)
        self.model.add_run(self.capture_view)

    def test_every_metric_is_listed_including_the_unsupported_ones(self):
        rows = self.model.metric_rows()
        ids = {r["id"] for r in rows}
        for metric in ("mcs", "constellation_iq", "DRB.UEThpDl", "latency_ran_ms"):
            self.assertIn(metric, ids, f"{metric} vanished from the panel")
        for row in rows:
            if row["status"] != "OK":
                self.assertTrue(row["reason"], f"{row['id']} has no reason")

    def test_the_goal_line_is_the_runs_own_target(self):
        goal, label = self.model.goal_line("throughput_mbps")
        self.assertEqual(goal, 3.5)
        self.assertEqual(label, "I2 target")
        self.assertEqual(self.model.goal_line("RRU.PrbDl"), (None, ""))

    def test_a_relative_time_run_labels_its_axis_elapsed(self):
        self.assertEqual(self.model.axis_label(), ("elapsed", "s"))

    def test_two_ues_are_two_series_never_one_line(self):
        series = self.model.chart_series("throughput_mbps")
        self.assertEqual(len(series), 2)
        self.assertEqual({s["scope"] for s in series}, {"ue1", "ue2"})

    def test_two_runs_are_two_series_never_a_concatenation(self):
        series = self.model.chart_series(
            "throughput_mbps",
            run_ids=[self.live_view.run_id, self.capture_view.run_id])
        self.assertEqual({s["runId"] for s in series},
                         {self.live_view.run_id, self.capture_view.run_id})
        # The capture has no throughput_mbps, and it still appears - with its
        # UNSUPPORTED status and no points.  A chart never silently drops an
        # unavailable series: the absence has to be visible.
        absent = [s for s in series if s["runId"] == self.capture_view.run_id]
        self.assertEqual(len(absent), 1)
        self.assertEqual(absent[0]["status"], "UNSUPPORTED")
        self.assertEqual(absent[0]["points"], ())
        self.assertTrue(absent[0]["statusReason"])

    def test_the_source_class_travels_with_every_series(self):
        for series in self.model.chart_series("throughput_mbps"):
            self.assertEqual(series["sourceClass"], "EXPERIMENT_RECORD")

    def test_the_results_bundle_has_all_eight_groups_plus_the_issue_list(self):
        bundle = self.model.results_bundle()
        for group in analysis.RESULT_GROUPS:
            self.assertIn(group, bundle, group)
        self.assertIn("dataIssues", bundle)
        self.assertEqual(bundle["manifest"]["mode"], "REPLAY")
        self.assertIs(bundle["manifest"]["isSuccess"], True)

    def test_repeat_run_comparison_keeps_n_and_the_conditions(self):
        comparison = self.model.compare_runs("throughput_mbps")
        entry = comparison["runs"][self.live_view.run_id]
        self.assertEqual(entry["n"], 17)
        self.assertEqual(entry["excludedNullSamples"], 1)
        self.assertEqual(entry["conditions"]["goalMbps"], 3.5)
        self.assertEqual(entry["conditions"]["sourceClass"], "EXPERIMENT_RECORD")
        self.assertIn("experiments.metrics", comparison["statistic"])

    def test_comparison_statistics_equal_the_paper_pipeline(self):
        from experiments.metrics import confidence_interval

        values = [s["value"] for s in self.live_view.samples
                  if s["metric"] == "throughput_mbps" and s["value"] is not None]
        expected = confidence_interval(values)
        entry = self.model.compare_runs("throughput_mbps")["runs"][
            self.live_view.run_id]
        self.assertAlmostEqual(entry["mean"], expected["mean"], places=12)
        self.assertAlmostEqual(entry["ci95"]["low"], expected["low"], places=12)

    def test_before_during_after_reports_an_empty_window_as_n_zero(self):
        split = self.model.before_during_after("throughput_mbps",
                                               boundaries=[-5.0, -1.0])
        self.assertEqual(split["windows"]["before"]["n"], 0)
        self.assertIsNone(split["windows"]["before"]["mean"])

    def test_phase_bands_come_from_the_runs_own_events(self):
        bands = self.model.phase_bands()
        self.assertEqual([b["name"] for b in bands],
                         ["Nominal", "Congestion", "Recovery"])

    def test_selection_axes_are_enumerated_from_the_run(self):
        axes = self.model.selection_axes()
        self.assertEqual(axes["ue"], ["ue1", "ue2"])
        self.assertIn(self.live_view.run_id, axes["run"])
        # the live experiment record carries no cell-scoped metric and no policy
        self.assertEqual(axes["cell"], [])

    def test_a_capture_offers_cell_and_policy_axes(self):
        model = analysis.AnalysisModel()
        model.add_run(self.capture_view, primary=True)
        axes = model.selection_axes()
        self.assertEqual(axes["cell"], ["NRCellDU=1", "NRCellDU=2"])
        self.assertTrue(axes["policy"])

    def test_selecting_a_policy_restricts_the_span_and_says_so(self):
        """A sample carries no policy field; the association is a time span."""
        model = analysis.AnalysisModel()
        model.add_run(self.capture_view, primary=True)
        policy_id = model.selection_axes()["policy"][0]
        window = model.correlation_window(policy_id=policy_id)
        self.assertIsNotNone(window)

        restricted = model.chart_series("RRU.PrbDl", policy_id=policy_id)
        self.assertTrue(restricted[0]["restrictedTo"])
        self.assertEqual(restricted[0]["restrictedTo"]["policyId"], policy_id)
        self.assertIn("samples carry no intent or policy field",
                      restricted[0]["restrictedTo"]["basis"])

        full = model.chart_series("RRU.PrbDl")
        self.assertIsNone(full[0]["restrictedTo"])
        self.assertGreaterEqual(sum(len(s["points"]) for s in full),
                                sum(len(s["points"]) for s in restricted))

    def test_an_unplaceable_selection_restricts_nothing(self):
        model = analysis.AnalysisModel()
        model.add_run(self.capture_view, primary=True)
        self.assertIsNone(model.correlation_window(policy_id="no-such-policy"))
        series = model.chart_series("RRU.PrbDl", policy_id="no-such-policy")
        self.assertIsNone(series[0]["restrictedTo"])

    def test_gui_state_round_trips(self):
        self.model.selection.metric = "throughput_mbps"
        self.model.selection.window_s = 60.0
        state = self.model.gui_state()
        other = analysis.AnalysisModel()
        other.restore_gui_state(state)
        self.assertEqual(other.selection.metric, "throughput_mbps")
        self.assertEqual(other.selection.window_s, 60.0)
        self.model.selection.policy_id = "p-1"
        self.assertEqual(self.model.gui_state()["policyId"], "p-1")

    def test_the_capture_run_reports_its_sparse_prb_series(self):
        model = analysis.AnalysisModel()
        model.add_run(self.capture_view, primary=True)
        series = model.chart_series("RRU.PrbDl")
        self.assertEqual(len(series), 2)
        for entry in series:
            self.assertEqual(len(entry["points"]), 1)


class WorkspaceContract(unittest.TestCase):
    """The workspace protocol, and the no-I/O-on-the-Tk-thread rule."""

    def test_it_implements_the_workspace_protocol(self):
        workspace = analysis.AnalysisWorkspace()
        for name in ("build", "on_state", "on_activate", "on_deactivate",
                     "gui_state", "restore_gui_state"):
            self.assertTrue(callable(getattr(workspace, name)), name)
        self.assertEqual(workspace.id, "analysis")
        self.assertTrue(workspace.title)

    def test_on_state_before_build_is_a_no_op_rather_than_a_crash(self):
        analysis.AnalysisWorkspace().on_state(None)

    def test_no_workspace_method_reads_a_store_or_exports_inline(self):
        """By AST: the Tk thread performs no I/O.

        ``load_run_view`` is the one store reader in the module and it is a
        module-level worker function, not a workspace method.
        """
        import ast

        source = (REPO_ROOT / "gui" / "operator" / "workspaces"
                  / "analysis.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        workspace = next(n for n in ast.walk(tree)
                         if isinstance(n, ast.ClassDef)
                         and n.name == "AnalysisWorkspace")
        forbidden = {"read_telemetry", "read_events", "read_episodes",
                     "read_manifest", "read_summary", "read_statistics",
                     "read_metric_index", "read_config_snapshot",
                     "export_figure", "export_run", "load_run_view", "load"}
        called = [node.func.attr for node in ast.walk(workspace)
                  if isinstance(node, ast.Call)
                  and getattr(node.func, "attr", None) in forbidden]
        called += [node.func.id for node in ast.walk(workspace)
                   if isinstance(node, ast.Call)
                   and getattr(node.func, "id", None) in forbidden]
        self.assertEqual(called, [])

    def test_an_export_request_is_delegated_never_run_inline(self):
        seen = []
        workspace = analysis.AnalysisWorkspace(on_export=seen.append)
        workspace.request_export("i2-throughput")
        self.assertEqual(seen, ["i2-throughput"])


@unittest.skipUnless(HAS_DISPLAY, "no DISPLAY")
class WidgetSmoke(unittest.TestCase):
    """Real widgets, skipped where there is no display.

    The toolkit is probed in ``setUp`` rather than in the decorator: a decorator
    predicate runs at import time, and opening a Tk root during test *discovery*
    would put a window on the display of every other test module in the suite.
    """

    def setUp(self):
        import tkinter as tk

        try:
            self.root = tk.Tk()
        except Exception as exc:                 # a DISPLAY that is not usable
            self.skipTest(f"no usable display: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_a_dense_table_paints_its_rows(self):
        table = tables.DenseTable(
            self.root, columns=(tables.ColumnSpec("id", "id"),
                                tables.ColumnSpec("status", "Status",
                                                  kind="status")),
            id_key="id")
        table.set_rows([{"id": "a", "status": "OK"},
                        {"id": "b", "status": "ERROR"}])
        self.assertEqual(len(table.tree.get_children()), 2)
        table.set_filter("error")
        self.assertEqual(len(table.tree.get_children()), 1)

    def test_a_metric_tile_paints_a_model(self):
        tile = metric_tile.MetricTile(self.root)
        tile.set_model(metric_tile.MetricTileModel(
            metric="RRU.PrbDl", display="DL PRB utilization", status="OK",
            value=30.0, unit="percent", decimals=0))
        self.assertEqual(tile.model.value_text(), "30 percent")

    def test_a_chart_draws_and_honours_the_frame_budget(self):
        chart = charts.TimeSeriesChart(self.root, title="t", y_label="y",
                                       y_unit="Mbps")
        chart.set_series([charts.SeriesSpec.styled(0, "s1", "series one")])
        chart.append("s1", 0.0, 1.0)
        chart.append("s1", 1.0, None)
        chart.append("s1", 2.0, 3.0)
        self.assertTrue(chart.refresh(force=True))
        self.assertFalse(chart.refresh(), "a second immediate redraw is skipped")


class TestPackageDoesNotShadowTheRealGuiPackage(unittest.TestCase):
    """``tests/gui/`` is importable as bare ``gui`` and must not hide ``gui/``.

    Lives here rather than beside ``tests/gui/__init__.py`` because that file is
    a package marker owned by another track; the property it guarantees is what
    is worth pinning, and it is a property of this whole directory.

    ``python3 -m unittest discover -s tests`` puts ``tests/`` on ``sys.path[0]``
    on Python 3.10, so a bare ``import gui`` finds this package.  Without the
    ``__path__`` extension in ``tests/gui/__init__.py`` that took out six
    unrelated test modules with ``No module named 'gui.operator'``.  Reproduced
    here directly - a subprocess with the same ``sys.path`` condition - rather
    than by running discovery, which would import the entire suite to prove one
    import.

    The repository's ``gui/__init__.py`` is deliberately not re-executed by the
    extension, because it imports the legacy Tk dashboard eagerly and that would
    pull tkinter into every headless discovery run.  So the assertion below is
    on ``gui.dashboard``, which is how the tests that need the legacy console
    reach it.
    """

    def test_the_shadow_still_resolves_the_real_package(self):
        import subprocess
        import sys

        probe = (
            "import sys; sys.path.insert(0, 'tests');"
            "import gui, gui.operator.status, gui.dashboard;"
            "assert gui.operator.status.OK == 'OK';"
            "assert hasattr(gui.dashboard, 'IntentCoordinatorGUI');"
            "assert 'gui.test_analysis_and_widgets' or True;"
            "print('ok')"
        )
        result = subprocess.run([sys.executable, "-c", probe],
                                cwd=str(REPO_ROOT), capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 0,
                         f"the shadow hides the real gui package:\n{result.stderr}")
        self.assertIn("ok", result.stdout)

    def test_the_extension_does_not_pull_tkinter_into_discovery(self):
        """A headless discovery run must not import the legacy Tk console."""
        import subprocess
        import sys

        probe = (
            "import sys; sys.path.insert(0, 'tests');"
            "import gui, gui.operator.status;"
            "assert 'tkinter' not in sys.modules, sorted(sys.modules);"
            "print('ok')"
        )
        result = subprocess.run([sys.executable, "-c", probe],
                                cwd=str(REPO_ROOT), capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_test_modules_stay_importable_through_the_same_package(self):
        import subprocess
        import sys

        probe = ("import sys; sys.path.insert(0, 'tests');"
                 "import gui.test_analysis_and_widgets; print('ok')")
        result = subprocess.run([sys.executable, "-c", probe],
                                cwd=str(REPO_ROOT), capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
