"""Compatibility guarantees for the deployment-binding terminology migration."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from oran.integration.deployment_binding import (
    DeploymentBindingContracts,
    DeploymentBindingError,
    resolve_deployment_binding,
)
from oran.integration.lower_release import LowerReleaseContracts


class DeploymentBindingCompatibilityTests(unittest.TestCase):
    def test_legacy_module_aliases_the_modern_contract_reader(self):
        self.assertIs(LowerReleaseContracts, DeploymentBindingContracts)

    def test_legacy_binding_filename_remains_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = root / "integration-values.json"
            values.write_text("{}", encoding="utf-8")
            (root / "lower-release-binding.json").write_text(
                json.dumps({"lowerReleaseRoot": str(root / "missing")}),
                encoding="utf-8")

            with self.assertRaises(DeploymentBindingError):
                resolve_deployment_binding(values)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
