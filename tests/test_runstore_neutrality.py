"""The run-directory store is neutral, and its old path still resolves.

Why this file exists.  ``assurance/batch/`` writes the same run directory the
Operator Console does - phaseB_task.md section 12 requires one schema, not two -
and it did so by importing ``gui.operator.store``.  That crossed the Gate 2
boundary in the one direction that matters: the Kernel's own package must never
depend on a console.  The fix was a dependency inversion, not an allowlist: the
store never had a ``gui`` dependency, only a ``gui`` *address*, so it moved to
the neutral top-level :mod:`runstore` and ``gui/operator/store/`` became a
re-export.

Two things have to keep holding after that move, and neither is covered by the
guards that already exist:

* the neutral package must stay neutral - a ``gui`` import added to it would
  restore the violation one level down, where
  ``test_assurance_never_imports_the_gui`` cannot see it;
* the re-export must stay complete - it is a star import, so a name added to
  the neutral module reaches the console automatically, but a ``__all__``
  edited on one side only would break console imports silently.
"""

import ast
import unittest
from pathlib import Path

import runstore.metric_index
import runstore.records
import runstore.session_store
from gui.operator.store import metric_index as gui_metric_index
from gui.operator.store import records as gui_records
from gui.operator.store import session_store as gui_session_store

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNSTORE_ROOT = REPO_ROOT / "runstore"

#: The re-export path, and the module each one stands for.
PAIRS = (
    (gui_records, runstore.records),
    (gui_metric_index, runstore.metric_index),
    (gui_session_store, runstore.session_store),
)


class TheNeutralPackageIsNeutral(unittest.TestCase):
    """Nothing under ``runstore/`` may reach back into a console."""

    def test_no_module_imports_the_gui(self):
        offenders = []
        for path in sorted(RUNSTORE_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [node.module or ""]
                for name in names:
                    if name == "gui" or name.startswith("gui."):
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{name}")
        self.assertEqual(offenders, [])

    def test_it_imports_only_the_standard_library_and_itself(self):
        """A new third-party or project dependency here would travel to both
        consumers at once, including the hardware-free batch boundary."""
        allowed_prefixes = ("runstore", "__future__")
        stdlib = {"hashlib", "json", "os", "secrets", "time", "datetime",
                  "pathlib", "typing"}
        outside = []
        for path in sorted(RUNSTORE_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [node.module or ""]
                for name in names:
                    root = name.split(".", 1)[0]
                    if root in stdlib or name.startswith(allowed_prefixes):
                        continue
                    outside.append(f"{path.relative_to(REPO_ROOT)}:{name}")
        self.assertEqual(outside, [])

    def test_the_repository_root_is_still_found_from_the_new_depth(self):
        """Both moved modules locate the repository by ``__file__`` depth.

        ``metric_index`` reads the metric registry that way and
        ``session_store`` reads ``repoCommit`` for every manifest; an off-by-one
        after the move would turn the first into a crash and the second into a
        silent ``None`` in provenance.
        """
        self.assertTrue(runstore.metric_index.REGISTRY_PATH.is_file(),
                        runstore.metric_index.REGISTRY_PATH)
        self.assertEqual(
            runstore.metric_index.REGISTRY_PATH,
            REPO_ROOT / "docs" / "phase-b-gui" / "metric-registry.1.0.0.json")
        self.assertIsNotNone(runstore.session_store._repo_commit())  # noqa: SLF001


class TheOldPathStillResolves(unittest.TestCase):
    """Every console import of the store keeps working, unchanged."""

    def test_the_re_export_carries_the_whole_declared_surface(self):
        for shim, neutral in PAIRS:
            with self.subTest(module=neutral.__name__):
                missing = [name for name in neutral.__all__
                           if not hasattr(shim, name)]
                self.assertEqual(missing, [])

    def test_the_re_exported_objects_are_the_same_objects(self):
        """Re-export, not re-implementation: an ``isinstance`` check written
        against either path has to accept an object made through the other."""
        for shim, neutral in PAIRS:
            for name in neutral.__all__:
                with self.subTest(module=neutral.__name__, name=name):
                    self.assertIs(getattr(shim, name), getattr(neutral, name))

    def test_the_console_reaches_the_store_through_the_old_path(self):
        from gui.operator.store.session_store import SessionStore
        from gui.operator.store.records import telemetry_sample
        from gui.operator.store.metric_index import MetricIndexBuilder

        self.assertIs(SessionStore, runstore.session_store.SessionStore)
        self.assertIs(telemetry_sample, runstore.records.telemetry_sample)
        self.assertIs(MetricIndexBuilder,
                      runstore.metric_index.MetricIndexBuilder)


class TheBatchRunnerUsesTheNeutralPath(unittest.TestCase):
    """The violation this file was written for, asserted at its own site."""

    def test_the_runner_imports_the_store_from_runstore(self):
        path = REPO_ROOT / "assurance" / "batch" / "runner.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules = {node.module for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) and not node.level}
        self.assertIn("runstore.records", modules)
        self.assertIn("runstore.session_store", modules)
        self.assertFalse({m for m in modules if m and m.startswith("gui")})


if __name__ == "__main__":
    unittest.main()
