"""Hermetic tests for the frozen Phase B Operator Console seams.

The design step freezes five shared seams so the four implementation tracks can
start in parallel without waiting on each other.  These tests pin the properties
the tracks are entitled to rely on:

* the seam modules import with **no toolkit and no transport** (so headless
  paths, CI and the export pipeline all keep working);
* the status vocabulary is complete, fail-closed and not colour-only;
* the Eq.12 terminal mapping agrees with the existing research console;
* the value formatters never turn "not measured" into a number;
* the frozen signature modules expose the documented API surface.

Everything here runs without a display and without network access.
"""

import ast
import json
import unittest
from pathlib import Path

from gui.operator import status as st
from gui.operator import tokens as tk_tokens
from gui.operator.viewmodel import bus, types

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = REPO_ROOT / "docs" / "phase-b-gui"

#: Every seam module, and the boundary they all share.
SEAM_MODULES = (
    REPO_ROOT / "gui" / "operator" / "tokens.py",
    REPO_ROOT / "gui" / "operator" / "status.py",
    REPO_ROOT / "gui" / "operator" / "viewmodel" / "types.py",
    REPO_ROOT / "gui" / "operator" / "viewmodel" / "bus.py",
    REPO_ROOT / "gui" / "operator" / "store" / "session_store.py",
    # The store's body moved to the neutral ``runstore`` package so
    # ``assurance/batch/`` can share the run-directory schema without importing
    # a console (Gate 2).  Scanned here as well as at the re-export path: the
    # headless guarantee is a property of the code, not of where it sits, and a
    # move must not carry it out from under the gate.
    REPO_ROOT / "runstore" / "session_store.py",
    REPO_ROOT / "runstore" / "records.py",
    REPO_ROOT / "runstore" / "metric_index.py",
    REPO_ROOT / "oran" / "rapp" / "status_projection.py",
)

#: Imports that would break the headless guarantee or cross the O-RAN boundary.
FORBIDDEN_SEAM_IMPORTS = {
    "tkinter", "matplotlib", "numpy",
    "subprocess", "socket", "telnetlib", "paramiko", "requests",
    "http.client", "urllib.request",
    "executor.system_controller", "executor.oai_executor",
    "collectors.multi_ue_collector", "gui.legacy_tools",
    "oran.nonrt.a1_client",
}


def _imported_names(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names.append(node.module or "")
    return names


class SeamHygiene(unittest.TestCase):
    """The seams stay importable from a headless process."""

    def test_seam_modules_import_no_toolkit_and_no_transport(self):
        violations = []
        for path in SEAM_MODULES:
            self.assertTrue(path.is_file(), f"missing seam file: {path}")
            for name in _imported_names(path):
                for banned in FORBIDDEN_SEAM_IMPORTS:
                    if name == banned or name.startswith(banned + "."):
                        violations.append(f"{path.name}:{name}")
        self.assertEqual(violations, [], "forbidden seam imports: %s" % violations)

    def test_status_projection_declares_read_only(self):
        from oran.rapp import status_projection

        self.assertTrue(status_projection.READ_ONLY)

    def test_status_projection_calls_no_mutating_r1_method(self):
        """The read-only property is proven by AST, not promised in a docstring."""
        path = REPO_ROOT / "oran" / "rapp" / "status_projection.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        mutators = {
            "create_policy", "update_policy", "delete_policy",
            "create_status_subscription", "update_status_subscription",
            "delete_status_subscription", "create_continuous_job",
            "update_data_job", "delete_data_job", "accept_evidence",
            "recover_one_time_pull", "harness_reset",
            "harness_precreate_binding", "harness_commit_binding",
        }
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                attr = getattr(node.func, "attr", None)
                if attr in mutators:
                    found.append(f"{attr}@{node.lineno}")
        self.assertEqual(found, [], "mutating R1 call in a read-only projection")

    def test_no_seam_reaches_the_harness_control_plane(self):
        """The /harness/** endpoints are a development control plane, not R1.

        Prose explaining *why* they are forbidden is expected in a docstring, so
        the scan looks at executable code only: every string constant and every
        attribute or function name outside a docstring.  That is what a real
        request would have to be built from.
        """
        for path in SEAM_MODULES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    doc = ast.get_docstring(node, clean=False)
                    if doc is not None:
                        docstrings.add(doc)
            offenders = [
                f"{path.name}:{node.lineno}"
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value not in docstrings
                and "harness" in node.value.lower()
            ]
            offenders += [
                f"{path.name}:{node.lineno}:{node.attr}"
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute) and node.attr.startswith("harness")
            ]
            self.assertEqual(offenders, [],
                             f"{path.name} reaches the harness control plane")


class StatusVocabulary(unittest.TestCase):
    """The vocabulary is complete, fail-closed and not colour-only."""

    def test_every_status_has_a_colour(self):
        for status_id in st.STATUS_SPECS:
            self.assertIn(status_id, tk_tokens.STATUS_COLORS)
            self.assertIn(status_id, tk_tokens.STATUS_COLORS_LIGHT)

    def test_encoding_is_not_colour_only(self):
        """Colour + glyph + shape.  Glyphs and shapes must all be distinct."""
        glyphs = [s.glyph for s in st.STATUS_SPECS.values()]
        shapes = [s.shape for s in st.STATUS_SPECS.values()]
        self.assertEqual(len(glyphs), len(set(glyphs)), "duplicate status glyph")
        self.assertEqual(len(shapes), len(set(shapes)), "duplicate status shape")

    def test_unmapped_upstream_value_never_resolves_ok(self):
        for table in st.CONTRACT_MAPS:
            resolved = st.map_contract_value(table, "NO_SUCH_VALUE")
            self.assertEqual(resolved.status, st.UNKNOWN)
            self.assertIn(st.UNMAPPED_REASON, resolved.reason or "")

    def test_unknown_table_and_absent_value_fail_closed(self):
        self.assertEqual(
            st.map_contract_value("no_such_table", "ENFORCED").status, st.UNKNOWN)
        self.assertEqual(
            st.map_contract_value("enforceStatus", None).status, st.UNKNOWN)

    def test_known_contract_values_map_as_specified(self):
        self.assertEqual(st.map_contract_value("enforceStatus", "ENFORCED").status, st.OK)
        self.assertEqual(st.map_contract_value("policyState", "ERROR").status, st.ERROR)
        self.assertEqual(
            st.map_contract_value("episodeState", "READBACK_MISMATCH").status, st.ERROR)
        self.assertEqual(
            st.map_contract_value("readbackResult", "NOT_AVAILABLE").status,
            st.UNSUPPORTED)
        self.assertEqual(
            st.map_contract_value("evidenceQuality", "STALE").status, st.STALE)
        self.assertEqual(
            st.map_contract_value("assuranceDecision", "VIOLATED").status, st.ERROR)

    def test_health_rollup_reports_the_worst_not_an_average(self):
        summary = st.health_summary({"a": st.OK, "b": st.OK, "c": st.ERROR})
        self.assertEqual(summary["worst"], st.ERROR)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["counts"][st.OK], 2)

    def test_empty_health_rollup_is_unknown_not_ok(self):
        self.assertEqual(st.worst([]), st.UNKNOWN)


class TerminalMapping(unittest.TestCase):
    """The operator console and the research console must never disagree."""

    def test_four_outcomes_collapse_onto_three_eq12_states(self):
        self.assertEqual(st.terminal_state_label("commit_original"), "Admitted")
        self.assertEqual(st.terminal_state_label("commit_revised"), "Admitted")
        self.assertEqual(
            st.terminal_state_label("pending_not_admitted"), "NotAdmitted")
        self.assertEqual(
            st.terminal_state_label("technical_failsafe"), "TechnicalFailsafe")

    def test_unknown_outcome_is_none_never_a_guess(self):
        self.assertIsNone(st.terminal_state_label(None))
        self.assertIsNone(st.terminal_state_label(""))
        self.assertIsNone(st.terminal_state_label("something_else"))

    def test_mapping_matches_the_existing_dashboard(self):
        """Read the legacy table out of source, so the two cannot drift apart."""
        path = REPO_ROOT / "gui" / "dashboard.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        legacy = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and any(getattr(t, "id", None) == "_TERMINAL_OUTCOME_TO_EQ12"
                            for t in node.targets)):
                legacy = {
                    k.value: v.id if isinstance(v, ast.Name) else getattr(v, "value", None)
                    for k, v in zip(node.value.keys, node.value.values)
                }
        self.assertIsNotNone(legacy, "legacy Eq.12 table not found")
        self.assertEqual(set(legacy), set(st.TERMINAL_OUTCOME_TO_EQ12))


class Freshness(unittest.TestCase):
    def test_absent_inputs_are_unknown_not_fresh(self):
        self.assertEqual(st.freshness(None, 1000), st.FRESHNESS_UNKNOWN)
        self.assertEqual(st.freshness(100, None), st.FRESHNESS_UNKNOWN)
        self.assertEqual(st.freshness(100, 0), st.FRESHNESS_UNKNOWN)

    def test_non_finite_age_is_unknown(self):
        self.assertEqual(st.freshness(float("nan"), 1000), st.FRESHNESS_UNKNOWN)
        self.assertEqual(st.freshness(float("inf"), 1000), st.FRESHNESS_UNKNOWN)
        self.assertEqual(st.freshness(True, 1000), st.FRESHNESS_UNKNOWN)

    def test_thresholds(self):
        self.assertEqual(st.freshness(100, 1000), st.FRESH)
        self.assertEqual(st.freshness(600, 1000), st.AGING)
        self.assertEqual(st.freshness(1500, 1000), st.FRESHNESS_STALE)


class ValueFormatting(unittest.TestCase):
    """"Not measured" must never become a number."""

    def test_unmeasured_renders_the_placeholder(self):
        for value in (None, float("nan"), float("inf"), float("-inf")):
            self.assertEqual(
                st.format_value(value, "throughput"), st.PRE_MEASUREMENT)

    def test_bool_is_not_a_number(self):
        self.assertEqual(st.format_value(True, "confidence"), st.PRE_MEASUREMENT)

    def test_zero_is_a_real_measurement(self):
        self.assertEqual(st.format_value(0.0, "throughput"), "0.00 Mbps")

    def test_units_and_precision(self):
        self.assertEqual(st.format_value(-95.24, "rsrp"), "-95.2 dBm")
        self.assertEqual(st.format_value(12.5, "sinr"), "12.5 dB")
        self.assertEqual(st.format_value(0.7123, "theta_star"), "0.712")

    def test_age_and_time_formatting(self):
        self.assertEqual(st.format_age(None), st.PRE_MEASUREMENT)
        self.assertEqual(st.format_age(-1), st.PRE_MEASUREMENT)
        self.assertEqual(st.format_age(500), "<1s ago")
        self.assertEqual(st.format_age(12_000), "12s ago")
        self.assertEqual(st.format_utc(None), st.PRE_MEASUREMENT)
        self.assertEqual(
            st.format_utc("2026-08-13T14:19:00.585Z"), "2026-08-13T14:19:00Z")

    def test_describe_leads_with_the_glyph(self):
        text = st.describe(st.resolve(st.UNSUPPORTED, reason="no source",
                                      gap_id="GAP-07"))
        self.assertTrue(text.startswith(st.STATUS_SPECS[st.UNSUPPORTED].glyph))
        self.assertIn("no source", text)
        self.assertIn("GAP-07", text)


class Tokens(unittest.TestCase):
    def test_chart_palette_matches_the_paper_figure_palette(self):
        """One palette on screen and on paper, so a figure cannot drift."""
        figures = (REPO_ROOT / "experiments" / "figures.py").read_text(
            encoding="utf-8")
        for color in tk_tokens.CHART_PALETTE:
            self.assertIn(color, figures,
                          f"{color} is not in the experiments figure palette")

    def test_fsm_and_terminal_colours_match_the_legacy_console(self):
        dashboard = (REPO_ROOT / "gui" / "dashboard.py").read_text(encoding="utf-8")
        for color in tk_tokens.FSM_STATE_COLORS.values():
            self.assertIn(color, dashboard)
        for color in tk_tokens.TERMINAL_STATE_COLORS.values():
            self.assertIn(color, dashboard)

    def test_series_style_varies_more_than_colour(self):
        first, second = tk_tokens.series_style(0), tk_tokens.series_style(1)
        self.assertNotEqual(first["color"], second["color"])
        self.assertNotEqual(first["linestyle"], second["linestyle"])
        self.assertNotEqual(first["marker"], second["marker"])

    def test_unknown_theme_falls_back_rather_than_raising(self):
        self.assertEqual(tk_tokens.theme("no-such-theme"), tk_tokens.theme("dark"))

    def test_demo_scale_keeps_body_text_projector_legible(self):
        body = tk_tokens.font("body", scale=tk_tokens.SCALE_DEMO)[1]
        self.assertGreaterEqual(body, 16)


class ViewModelTypes(unittest.TestCase):
    def test_session_state_defaults_are_not_live(self):
        """A default-constructed state must never claim to be a live run."""
        self.assertFalse(types.SessionState().is_live)
        self.assertTrue(types.SessionState(mode="LIVE").is_live)

    def test_view_models_are_frozen(self):
        row = types.IntentRowView(intent_id="i-1", revision="1", text="t")
        with self.assertRaises(Exception):
            row.intent_id = "i-2"        # type: ignore[misc]

    def test_three_status_axes_are_separate_fields(self):
        row = types.IntentRowView(intent_id="i-1", revision="1", text="t")
        for field_name in ("intent_state", "policy_status", "evidence_status"):
            self.assertTrue(hasattr(row, field_name))

    def test_conflict_defaults_to_unknown_not_false(self):
        """GAP-03: the coordinator does not persist the S1 verdict; do not infer it."""
        self.assertIsNone(types.DecisionView(episode_id="e", intent_text="t")
                          .has_conflict)


class FrozenSignatures(unittest.TestCase):
    """Consumers may code against these before the bodies land."""

    def test_state_bus_surface(self):
        for name in ("publish", "publish_many", "subscribe", "drain",
                     "snapshot", "dropped", "depth"):
            self.assertTrue(callable(getattr(bus.StateBus, name)))
        self.assertIn("timeline", bus.CHANNELS)
        self.assertIn("warning", bus.CHANNELS)

    def test_session_store_surface(self):
        from gui.operator.store.session_store import SessionStore

        for name in ("create", "open", "list_runs", "finalize",
                     "append_telemetry", "append_event", "append_episode",
                     "append_cycle", "append_llm_call", "record_issue",
                     "copy_raw", "write_metric_index", "write_summary",
                     "write_figure", "read_telemetry", "read_events",
                     "read_episodes"):
            self.assertTrue(hasattr(SessionStore, name), f"missing {name}")

    def test_only_completed_is_a_success_disposition(self):
        from gui.operator.store import session_store as ss

        self.assertEqual(ss.SUCCESS_DISPOSITIONS, ("COMPLETED",))
        for bad in ("ABORTED", "FAILED", "INTERRUPTED"):
            self.assertIn(bad, ss.DISPOSITIONS)
            self.assertNotIn(bad, ss.SUCCESS_DISPOSITIONS)


class DesignSpecs(unittest.TestCase):
    """The machine-readable specs stay parseable and agree with the code."""

    SPECS = (
        "boundary-map.1.0.0.json",
        "status-vocabulary.1.0.0.json",
        "session-store.1.0.0.schema.json",
        "metric-registry.1.0.0.json",
        "replay-sources.1.0.0.json",
        "file-ownership.1.0.0.json",
        "verification-plan.1.0.0.json",
    )

    def test_specs_parse(self):
        for name in self.SPECS:
            path = SPEC_DIR / name
            self.assertTrue(path.is_file(), f"missing spec: {name}")
            json.loads(path.read_text(encoding="utf-8"))

    def test_vocabulary_spec_matches_the_code(self):
        spec = json.loads(
            (SPEC_DIR / "status-vocabulary.1.0.0.json").read_text(encoding="utf-8"))
        spec_ids = {entry["id"] for entry in spec["statuses"]}
        self.assertEqual(spec_ids, set(st.STATUS_SPECS))
        for entry in spec["statuses"]:
            self.assertEqual(st.STATUS_SPECS[entry["id"]].glyph, entry["glyph"])
            self.assertEqual(st.STATUS_SPECS[entry["id"]].severity, entry["severity"])

    def test_contract_mappings_match_the_code(self):
        spec = json.loads(
            (SPEC_DIR / "status-vocabulary.1.0.0.json").read_text(encoding="utf-8"))
        for table, mapping in spec["contractMappings"].items():
            code_map = st.CONTRACT_MAPS.get(table)
            if code_map is None:
                continue
            for key, value in mapping.items():
                if key.startswith("_"):
                    continue
                self.assertEqual(code_map.get(key), value,
                                 f"{table}.{key} disagrees with the spec")

    def test_track_ownership_sets_do_not_intersect(self):
        spec = json.loads(
            (SPEC_DIR / "file-ownership.1.0.0.json").read_text(encoding="utf-8"))
        seen = {}
        for track in spec["tracks"]:
            for path in track["owns"]:
                self.assertNotIn(
                    path, seen,
                    f"{path} owned by both {seen.get(path)} and {track['id']}")
                seen[path] = track["id"]

    def test_frozen_paths_are_not_owned_by_any_track(self):
        """Exact frozen paths and frozen glob prefixes are both off limits."""
        spec = json.loads(
            (SPEC_DIR / "file-ownership.1.0.0.json").read_text(encoding="utf-8"))
        frozen_exact = {p for p in spec["frozen"]["paths"] if "*" not in p}
        frozen_prefixes = tuple(
            p.split("*", 1)[0] for p in spec["frozen"]["paths"] if "*" in p)
        for track in spec["tracks"]:
            for path in track["owns"]:
                self.assertNotIn(path, frozen_exact,
                                 f"{path} is frozen but owned by {track['id']}")
                for prefix in frozen_prefixes:
                    self.assertFalse(
                        path.startswith(prefix),
                        f"{path} is under frozen {prefix}* but owned by {track['id']}")

    def test_every_seam_declared_in_the_spec_exists_on_disk(self):
        spec = json.loads(
            (SPEC_DIR / "file-ownership.1.0.0.json").read_text(encoding="utf-8"))
        for seam in spec["seams"]["files"]:
            path = REPO_ROOT / seam["path"]
            self.assertTrue(path.is_file(), f"seam not created: {seam['path']}")
            if seam["state"] == "SIGNATURES_FROZEN":
                self.assertIsNotNone(seam["bodyOwner"],
                                     f"{seam['path']} has no body owner")

    def test_boundary_map_prohibitions_are_present(self):
        """The prohibition list is what the scan gate is built from."""
        spec = json.loads(
            (SPEC_DIR / "boundary-map.1.0.0.json").read_text(encoding="utf-8"))
        prohibitions = spec["prohibitions"]
        for name in ("forbiddenImports", "forbiddenProcessPatterns",
                     "forbiddenNetworkTargets", "forbiddenControls",
                     "prbMcsRule", "legacyIsolationPolicy"):
            self.assertIn(name, prohibitions)
        self.assertIn("gui.legacy_tools", prohibitions["forbiddenImports"])
        self.assertIn("executor.system_controller", prohibitions["forbiddenImports"])
        harness = [t["pattern"] for t in prohibitions["forbiddenNetworkTargets"]]
        self.assertIn("/harness/", harness)

    def test_metric_registry_gaps_are_documented(self):
        registry = json.loads(
            (SPEC_DIR / "metric-registry.1.0.0.json").read_text(encoding="utf-8"))
        gaps_doc = (SPEC_DIR / "integration-field-gaps.md").read_text(
            encoding="utf-8")
        for metric in registry["metrics"]:
            gap_id = metric.get("gapId")
            if gap_id:
                self.assertIn(gap_id, gaps_doc,
                              f"{metric['id']} cites undocumented {gap_id}")


if __name__ == "__main__":
    unittest.main()
