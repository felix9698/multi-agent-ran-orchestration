"""Can the default entry point reach the pre-Kernel Coordinator?  It must not.

Every other guard in this repository is a *static* one: the acceptance scan
reads the Cockpit's source and refuses an import of the old decision runtime.
A static scan cannot see a lazy import three calls down, and that is exactly
where the old runtime lived - ``main.py`` constructed ``IntentCoordinator``
eagerly, and the console reached ``oran.rapp.gui_entry`` from inside four
methods.

So this module asserts the property dynamically, in a **clean process**:

* build exactly what ``python3 main.py`` builds (``main.build_console``), and
* dump ``sys.modules``, and
* require that the pre-Kernel decision runtime is not in it.

A subprocess rather than an import here, because ``sys.modules`` is
process-global: the rest of the suite legitimately imports the preserved
runtime to test it, and a same-process assertion would pass or fail on test
ordering rather than on the composition.

The mutation tests are the other half.  A guard that cannot fail is not a
guard, so each one puts a legacy import back into ``main.py`` - eagerly, lazily
inside the composition function, and through the console - and requires this
check to catch it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The pre-Kernel decision runtime and everything that can only be reached
#: through it.  Prefix-matched: ``coordinator.anything`` counts, and so does
#: the package itself, because importing ``coordinator`` runs its ``__init__``.
FORBIDDEN_PREFIXES = (
    "coordinator",
    "executor",
    "collectors",
    "gui.dashboard",
    "oran.rapp.gui_entry",
    "oran.rapp.coordinator_adapter",
    "oran.rapp.headless",
    "tools.legacy",
)

#: The probe.  It imports the *entry point module itself* and calls the
#: composition function ``main()`` calls, so nothing here can drift from what
#: an operator actually starts.  ``create_window`` is deliberately not called:
#: the toolkit is not the subject, the composition is.
PROBE = """
import json, sys
sys.path.insert(0, {root!r})
import main
console = main.build_console()
print("@@" + json.dumps({{
    "modules": sorted(sys.modules),
    "hasLegacyEpisodePort": console.legacy_episode is not None,
    "hasKernelSession": getattr(console, "kernel_session", None) is not None,
    "hasLiveComposition": console.live is not None,
    "mode": console.controller.state().mode,
}}))
"""

#: A second probe: not only "was it imported", but "can the operator get
#: there".  It drives the console the way the Bind button does and requires a
#: stated refusal rather than a legacy episode.
BIND_PROBE = """
import json, sys
sys.path.insert(0, {root!r})
import main
console = main.build_console()
outcome = {{}}
try:
    console.attach_live_from_profile()
except Exception as exc:
    outcome = {{"refused": True, "type": type(exc).__name__, "reason": str(exc)}}
else:
    outcome = {{"refused": False}}
try:
    console.select_mode("LIVE")
except Exception as exc:
    outcome["liveRefused"] = True
    outcome["liveReason"] = str(exc)
else:
    outcome["liveRefused"] = False
outcome["modules"] = sorted(sys.modules)
print("@@" + json.dumps(outcome))
"""


def _run(source: str, *, root: Path = REPO_ROOT) -> dict:
    """Run *source* in a clean interpreter and return its JSON payload."""
    result = subprocess.run(
        [sys.executable, "-c", source.format(root=str(root))],
        cwd=str(root), capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise AssertionError(
            f"probe failed ({result.returncode})\n{result.stdout}\n{result.stderr}")
    for line in result.stdout.splitlines():
        if line.startswith("@@"):
            return json.loads(line[2:])
    raise AssertionError(f"probe printed no payload\n{result.stdout}")


def _offenders(modules) -> list:
    return sorted(name for name in modules
                  if any(name == item or name.startswith(item + ".")
                         for item in FORBIDDEN_PREFIXES))


class DefaultEntryLoadsNoLegacyRuntime(unittest.TestCase):
    """``python3 main.py``'s composition, in a process of its own."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = _run(PROBE)

    def test_the_entry_module_does_not_import_the_coordinator(self) -> None:
        self.assertEqual([], _offenders(self.payload["modules"]))

    def test_the_console_is_handed_no_legacy_episode_runtime(self) -> None:
        self.assertFalse(self.payload["hasLegacyEpisodePort"])

    def test_it_opens_disconnected_with_nothing_attached(self) -> None:
        """No deployment, no recording, no Kernel session: no claim."""
        self.assertEqual("DISCONNECTED", self.payload["mode"])
        self.assertFalse(self.payload["hasLiveComposition"])
        self.assertFalse(self.payload["hasKernelSession"])

    def test_the_probe_would_notice_a_legacy_module(self) -> None:
        """The offender scan itself, on a list that does contain one."""
        self.assertEqual(
            ["coordinator.intent_coordinator"],
            _offenders(["json", "coordinator.intent_coordinator", "assurance"]))


class BindingRefusesInsteadOfReachingTheLegacyRuntime(unittest.TestCase):
    """The operator's own route: press Bind, ask for Live."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = _run(BIND_PROBE)

    def test_bind_is_refused_by_name(self) -> None:
        self.assertTrue(self.payload["refused"], self.payload)
        self.assertIn("Kernel", self.payload["reason"])

    def test_live_is_refused_without_a_kernel_session(self) -> None:
        self.assertTrue(self.payload["liveRefused"], self.payload)
        self.assertIn("Kernel", self.payload["liveReason"])

    def test_the_refusal_loaded_no_legacy_module_either(self) -> None:
        """A refusal that first imports what it refuses is not a refusal."""
        self.assertEqual([], _offenders(self.payload["modules"]))


class MutationRestoresTheLegacyImport(unittest.TestCase):
    """Put the old reachability back; this check must fail.

    Each mutation is copied into a throwaway checkout rather than applied to
    the repository, so a failure here can never leave a legacy import behind.
    """

    #: A copy of the tree is expensive; one per class, reset per mutation.
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.mkdtemp(prefix="b01-mutation-")
        cls.root = Path(cls._tmp) / "repo"
        cls.root.mkdir()
        # A symlink farm rather than a copy: every package stays the real one,
        # so the mutation is judged against the tree as it actually ships, and
        # only ``main.py`` is a real file this class may rewrite.  Copying the
        # tree instead would take a minute and risk proving something about an
        # incomplete checkout.
        for source in sorted(REPO_ROOT.iterdir()):
            if source.name in (".git", "main.py", "__pycache__"):
                continue
            (cls.root / source.name).symlink_to(source)
        cls._original = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        (cls.root / "main.py").write_text(cls._original, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def tearDown(self) -> None:
        (self.root / "main.py").write_text(self._original, encoding="utf-8")

    def _mutate(self, snippet: str) -> None:
        (self.root / "main.py").write_text(
            self._original + textwrap.dedent(snippet), encoding="utf-8")

    def test_an_eager_module_level_import_is_caught(self) -> None:
        self._mutate("""
            from coordinator.intent_coordinator import IntentCoordinator  # noqa: E402
            """)
        payload = _run(PROBE, root=self.root)
        self.assertTrue(_offenders(payload["modules"]))

    def test_a_lazy_import_inside_the_composition_is_caught(self) -> None:
        """The case a static scan of ``main.py`` alone would still miss."""
        self._mutate("""
            _build_console = build_console


            def build_console(**kwargs):
                from oran.rapp.gui_entry import LiveIntegration  # noqa: F401
                return _build_console(**kwargs)
            """)
        payload = _run(PROBE, root=self.root)
        self.assertIn("oran.rapp.gui_entry", payload["modules"])
        self.assertTrue(_offenders(payload["modules"]))

    def test_handing_the_console_the_legacy_port_is_caught(self) -> None:
        """Reachability, not just import: the port is what re-opens the path."""
        self._mutate("""
            _build_console = build_console


            def build_console(**kwargs):
                from tools.legacy.episode_support import legacy_episode_support

                console = _build_console(**kwargs)
                console.legacy_episode = legacy_episode_support()
                return console
            """)
        payload = _run(PROBE, root=self.root)
        self.assertTrue(payload["hasLegacyEpisodePort"])
        self.assertTrue(_offenders(payload["modules"]))

    def test_the_unmutated_copy_still_passes(self) -> None:
        """Otherwise the mutations above would prove nothing."""
        payload = _run(PROBE, root=self.root)
        self.assertEqual([], _offenders(payload["modules"]))
        self.assertFalse(payload["hasLegacyEpisodePort"])


class TheNeutralProjectionIsTransitivelyPure(unittest.TestCase):
    """The one ``oran.rapp`` module the Cockpit still imports, on its own.

    ``tests/gui/test_cockpit_acceptance.py`` allows the console to import
    ``oran.rapp.status_projection`` - Preflight's boundary check has to import
    it to assert its ``READ_ONLY`` declaration.  An allowance is only as good
    as what sits behind the module it allows, so this imports it into an
    otherwise empty interpreter and requires the pre-Kernel runtime not to
    arrive with it.
    """

    PROBE = """
import json, sys
sys.path.insert(0, {root!r})
import oran.rapp.status_projection as projection
print("@@" + json.dumps({{"modules": sorted(sys.modules),
                          "readOnly": bool(projection.READ_ONLY)}}))
"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = _run(cls.PROBE)

    def test_it_pulls_in_no_decision_runtime(self) -> None:
        self.assertEqual([], _offenders(self.payload["modules"]))

    def test_it_still_declares_itself_read_only(self) -> None:
        """The declaration Preflight checks; the allowance rests on it."""
        self.assertTrue(self.payload["readOnly"])


class TheLegacyRuntimeIsPreservedAndOptIn(unittest.TestCase):
    """Unreachable is not deleted, and the gate is still the operator's."""

    def test_the_preserved_console_still_exists(self) -> None:
        path = REPO_ROOT / "tools" / "legacy" / "coordinator_console.py"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("AIC_LEGACY_CONSOLE_APPROVED", text)
        self.assertIn("history-only", text.lower())

    def test_the_preserved_runtime_modules_still_exist(self) -> None:
        for name in ("coordinator/intent_coordinator.py",
                     "coordinator/fsm.py",
                     "gui/dashboard.py",
                     "oran/rapp/gui_entry.py",
                     "oran/rapp/coordinator_adapter.py"):
            with self.subTest(module=name):
                self.assertTrue((REPO_ROOT / name).is_file())

    def test_the_preserved_console_refuses_without_the_gate(self) -> None:
        """No gate, no coordinator: the refusal happens before construction."""
        result = subprocess.run(
            [sys.executable, "-m", "tools.legacy.coordinator_console",
             "--no-gui"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
            env={"PATH": "/usr/bin:/bin", "HOME": str(REPO_ROOT)})
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("AIC_LEGACY_CONSOLE_APPROVED", result.stdout)
        self.assertIn("python3 main.py", result.stdout)


if __name__ == "__main__":
    unittest.main()
