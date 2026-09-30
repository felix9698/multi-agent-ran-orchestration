"""Mechanical O-RAN and credential boundary gate for the Operator Console."""

import ast
import importlib
import os
import re
import socket
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI_ROOT = ROOT / "gui" / "operator"
#: The run-directory store the console writes through.  It lives outside
#: ``gui/operator/`` so ``assurance/batch/`` can share the schema without
#: importing a console (Gate 2), and it is scanned here so relocating it did not
#: quietly take it out from under this gate.
RUNSTORE_ROOT = ROOT / "runstore"
PROJECTION = ROOT / "oran" / "rapp" / "status_projection.py"
FORBIDDEN_IMPORTS = {
    "subprocess", "socket", "http.client", "urllib.request", "requests",
    "paramiko", "telnetlib", "pexpect", "fabric", "executor.system_controller", "executor.oai_executor",
    "collectors.multi_ue_collector", "gui.legacy_tools", "gui.dashboard",
}
FORBIDDEN_CALLS = {
    ("os", "system"), ("os", "popen"), ("os", "execv"),
    ("os", "execve"), ("os", "spawnv"), ("os", "spawnve"),
    ("asyncio", "create_subprocess_exec"), ("asyncio", "create_subprocess_shell"),
}
FORBIDDEN_TEXT = re.compile(
    r"\bpgrep\b|\bpkill\b|\bps\s+(?:-ef|aux)\b|\bkill\s+-9\b|\btmux\b|"
    r"\b(?:ssh|scp|sftp)\b(?=\s)|\btelnet\b|\bnc\b(?=\s)|"
    r"\bdocker\b(?=\s)|\bsystemctl\b|\bip\s+link\s+set\b|\bifconfig\b|"
    r"\bnr-softmodem\b|\bnr-uesoftmodem\b|\buhd_|\biperf3\b|/harness/",
    re.IGNORECASE,
)
SECRET = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\b(?:sk|ghp|AIza)-[A-Za-z0-9_]{6,}|"
    r"\b(?:api_)?(?:key|token|secret|password)\s*[:=]\s*['\"][^'\"]+['\"]|"
    r"['\"](?:api_)?(?:key|token|secret|password)['\"]\s*:\s*['\"][^'\"]+['\"]",
)
MUTATORS = {
    "create_policy", "update_policy", "delete_policy", "create_status_subscription",
    "update_status_subscription", "delete_status_subscription", "create_continuous_job",
    "update_data_job", "delete_data_job", "accept_evidence", "recover_one_time_pull",
    "harness_reset", "harness_precreate_binding", "harness_commit_binding",
}


def _python_sources():
    return (sorted(GUI_ROOT.rglob("*.py")) + sorted(RUNSTORE_ROOT.rglob("*.py"))
            + [PROJECTION])


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            yield node.module or ""


def _executable_strings(tree):
    """Yield strings that can form a request, excluding explanatory docstrings."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                yield node.value


RAN_SETTING_COMMAND = re.compile(
    r"^(?:set|write|apply|push)_(?:prb|mcs|power|gain|atten|sched|priority)\w*$",
    re.IGNORECASE,
)


def _command_names(expression):
    if isinstance(expression, ast.Name):
        yield expression.id
    elif isinstance(expression, ast.Attribute):
        yield expression.attr
    elif isinstance(expression, ast.Lambda):
        for call in ast.walk(expression.body):
            if isinstance(call, ast.Call):
                yield from _command_names(call.func)


def _boundary_violations(paths):
    """Return concrete violations for the complete gate scope and test injections."""
    violations = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in _imports(tree):
            if any(name == banned or name.startswith(banned + ".")
                   for banned in FORBIDDEN_IMPORTS):
                violations.append(f"{path}: forbidden import {name}")
        if any("/harness/" in value for value in _executable_strings(tree)):
            violations.append(f"{path}: forbidden /harness/ literal")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                owner = node.func.value.id if isinstance(node.func.value, ast.Name) else None
                if (owner, node.func.attr) in FORBIDDEN_CALLS:
                    violations.append(f"{path}:{node.lineno}: forbidden {owner}.{node.func.attr}")
            for keyword in node.keywords:
                if keyword.arg == "command":
                    for name in _command_names(keyword.value):
                        if RAN_SETTING_COMMAND.fullmatch(name):
                            violations.append(f"{path}:{node.lineno}: forbidden RAN command {name}")
    return violations


def _raw_text_matches(paths):
    """Files whose RAW TEXT contains a forbidden control token.

    Deliberately not AST-based, and deliberately applied to the projection as
    well as to the console.  The AST scan cannot see a comment at all - comments
    are not nodes - so a ``# route = "/harness/state"`` left behind by a
    half-reverted experiment is invisible to it while being exactly the thing
    the next person copies back into code.  Reading the bytes has no such blind
    spot.  It costs a little prose discipline in the docstrings, which is the
    right trade: the modules under this scan already avoid naming the route.
    """
    return [str(path) for path in paths
            if FORBIDDEN_TEXT.search(path.read_text(encoding="utf-8"))]


def _secret_matches(paths):
    return [str(path) for path in paths if path.is_file()
            and SECRET.search(path.read_text(encoding="utf-8"))]


class GuiBoundaryScanTests(unittest.TestCase):
    def test_gui_source_has_no_transport_or_legacy_import(self):
        self.assertEqual(_boundary_violations(_python_sources()), [])

    def test_gui_source_has_no_direct_control_text(self):
        """Raw text, over the console AND the projection - comments included."""
        self.assertEqual(_raw_text_matches(_python_sources()), [])

    def test_raw_text_scan_catches_a_comment_only_harness_route(self):
        """The AST scan's structural blind spot, closed.

        A ``/harness/`` literal inside a comment is not a node, so
        ``_boundary_violations`` cannot see it.  The raw-text scan must.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "status_projection.py"
            path.write_text(
                '"""A projection."""\n'
                '# route = "/harness/state"  # left over from an experiment\n'
                'VALUE = 1\n', encoding="utf-8")
            self.assertEqual(_boundary_violations((path,)), [],
                             "precondition: the AST scan is blind to comments")
            self.assertEqual(_raw_text_matches((path,)), [str(path)])

    def test_raw_text_scan_catches_a_commented_out_transport_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "widget.py"
            path.write_text("# telnet 127.0.0.1 9091\nVALUE = 2\n",
                            encoding="utf-8")
            self.assertEqual(_raw_text_matches((path,)), [str(path)])

    def test_process_token_detection_does_not_match_identifier_substrings(self):
        self.assertIsNotNone(FORBIDDEN_TEXT.search("nc localhost 39000"))
        for ordinary_identifier in ("async_handler", "mnc_code", "func_name"):
            with self.subTest(ordinary_identifier=ordinary_identifier):
                self.assertIsNone(FORBIDDEN_TEXT.search(ordinary_identifier))

    def test_projection_scope_rejects_harness_literal_and_process_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "status_projection.py"
            path.write_text('import os\nroute = "/harness/state"\nos.system("true")\n', encoding="utf-8")
            violations = _boundary_violations((path,))
        self.assertTrue(any("/harness/" in item for item in violations))
        self.assertTrue(any("os.system" in item for item in violations))

    def test_transport_import_injection_rejects_pexpect_and_fabric(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "injected.py"
            path.write_text("import pexpect\nimport fabric\n", encoding="utf-8")
            violations = _boundary_violations((path,))
        self.assertTrue(any("pexpect" in item for item in violations))
        self.assertTrue(any("fabric" in item for item in violations))

    def test_ran_setting_button_command_injection_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "injected_widget.py"
            path.write_text("tk.Button(root, command=set_prb)\n", encoding="utf-8")
            violations = _boundary_violations((path,))
        self.assertTrue(any("set_prb" in item for item in violations))

    def test_secret_scan_includes_json_documents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "integration.json"
            path.write_text('{"token": "example-value"}\n', encoding="utf-8")
            matches = _secret_matches((path,))
        self.assertEqual(matches, [str(path)])

    def test_projection_has_no_mutating_r1_call(self):
        tree = ast.parse(PROJECTION.read_text(encoding="utf-8"), filename=str(PROJECTION))
        calls = [node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in MUTATORS]
        self.assertEqual(calls, [])

    def test_gui_source_has_no_process_or_async_subprocess_call(self):
        self.assertEqual(_boundary_violations(_python_sources()), [])

    def test_workspace_imports_do_not_use_blocked_runtime_operations(self):
        def blocked(*args, **kwargs):
            raise AssertionError("GUI import attempted a forbidden runtime operation")

        with mock.patch.object(socket, "socket", blocked), \
             mock.patch.object(subprocess, "run", blocked), \
             mock.patch.object(os, "system", blocked):
            for name in ("gui.operator.workspaces.demo", "gui.operator.workspaces.settings",
                         "gui.operator.widgets.constellation", "gui.operator.widgets.kpi_hero"):
                importlib.reload(importlib.import_module(name))

    def test_gui_credentials_and_docs_contain_no_secret_material(self):
        docs = ROOT / "docs" / "phase-b-gui"
        paths = (list(GUI_ROOT.rglob("*.py")) + list(RUNSTORE_ROOT.rglob("*.py"))
                 + list(docs.rglob("*.md")) + list(docs.rglob("*.json")))
        self.assertEqual(_secret_matches(paths), [])


if __name__ == "__main__":
    unittest.main()
