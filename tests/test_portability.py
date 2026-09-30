"""Fail closed when tests or ORAN sources depend on a host-specific checkout."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
FORBIDDEN_HOST_REFERENCES = ("/" + "home/", "Oran" + "C/")


def find_forbidden_host_references(roots: tuple[Path, ...]) -> list[str]:
    findings: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            try:
                display = path.relative_to(REPO)
            except ValueError:
                display = path
            for reference in FORBIDDEN_HOST_REFERENCES:
                if reference in text:
                    findings.append(f"{display}: {reference}")
    return findings


class PortabilityGuardTests(unittest.TestCase):
    def test_test_and_oran_sources_do_not_depend_on_host_checkout_paths(self) -> None:
        """B2: portable tests must use tracked inputs rather than a local checkout."""
        self.assertEqual([], find_forbidden_host_references((REPO / "tests", REPO / "oran")))

    def test_guard_rejects_injected_host_checkout_reference(self) -> None:
        """B2: an injected host-specific checkout reference is detected before execution."""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "injected.py"
            source.write_text("bundle = '/" + "home/example/Oran" + "C/input'\n", encoding="utf-8")
            findings = find_forbidden_host_references((Path(directory),))
        self.assertEqual(2, len(findings))
        self.assertTrue(any(finding.endswith(": /" + "home/") for finding in findings))
        self.assertTrue(any(finding.endswith(": Oran" + "C/") for finding in findings))
