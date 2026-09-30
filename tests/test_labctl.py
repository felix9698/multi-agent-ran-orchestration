"""Hermetic acceptance coverage for the separate Lab Setup utility."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.labctl.executor import CommandResult
from tools.labctl.inventory import load_inventory
from tools.labctl.orchestrator import LabOrchestrator
from tools.labctl.receipt import Probe, create_receipt
from tools.labctl.state import RunStore


class MockExecutor:
    """Port-only executor: records requests and never starts a process."""

    def __init__(self, outcomes=()):
        self.outcomes = iter(outcomes)
        self.calls = []

    def run(self, host, command, *, run_dir, label, dry_run):
        self.calls.append((host.id, command.argv, label, dry_run))
        try:
            exit_code = next(self.outcomes)
        except StopIteration:
            exit_code = 0
        stdout = run_dir / f"{label}.stdout"
        stderr = run_dir / f"{label}.stderr"
        stdout.write_text("mock\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        return CommandResult(command.argv, exit_code, stdout, stderr,
                             "2026-08-26T00:00:00Z", "2026-08-26T00:00:00Z", dry_run)


def _inventory(root: Path) -> Path:
    path = root / "inventory.json"
    path.write_text(json.dumps({
        "schemaVersion": "oran-aic-labctl-inventory/1.0.0",
        "profileId": "mock-lab", "objective": "PIN_TO_CELL",
        "stateRoot": str(root / "runs"),
        "hosts": {"pc1": {"transport": "local"}},
        "components": [
            {"id": "core", "host": "pc1", "stage": 1,
             "status": {"argv": ["mock", "core-status"]},
             "start": {"argv": ["mock", "core-start"]},
             "stop": {"argv": ["mock", "core-stop"]},
             "configure": {"argv": ["mock", "core-config"]}},
            {"id": "gnb1", "host": "pc1", "stage": 2,
             "dependencies": ["core"], "rf": True,
             "status": {"argv": ["mock", "gnb-status"]},
             "start": {"argv": ["mock", "gnb-start"]},
             "stop": {"argv": ["mock", "gnb-stop"]},
             "configure": {"argv": ["mock", "gnb-config"]}},
        ],
    }), encoding="utf-8")
    return path


class LabctlAcceptanceTests(unittest.TestCase):
    def test_default_inventory_declares_all_required_hosts_and_readiness_categories(self):
        inventory = load_inventory("tools/labctl/profiles/pc1-dual-cell-prb24.json")

        self.assertTrue({"core", "gnb1", "gnb2", "ue1", "ue2", "ue3"}
                        .issubset({component.id for component in inventory.components}))
        categories = {component.readiness_category for component in inventory.components}
        self.assertTrue({"CORE", "GNB", "UE", "USRP", "NETWORK", "PREREQUISITE"}
                        .issubset(categories))

    def test_static_config_requires_yes_and_never_calls_executor_when_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = MockExecutor()
            controller = LabOrchestrator(load_inventory(_inventory(root)), executor, RunStore(root / "runs"))

            refused = controller.apply_config(execute=True, confirmation=None)
            accepted = controller.apply_config(execute=True, confirmation="YES")

        self.assertEqual(refused.disposition, "REFUSED_CONFIRMATION")
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(accepted.disposition, "COMPLETED")
        self.assertEqual({item.outcome for item in accepted.components}, {"CONFIG_STAGED"})
        self.assertEqual([call[1][1] for call in executor.calls], ["core-config", "gnb-config"])

    def test_prepare_cleanup_and_recover_require_yes_without_executor_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = MockExecutor()
            controller = LabOrchestrator(load_inventory(_inventory(root)), executor, RunStore(root / "runs"))

            reports = (
                controller.prepare(execute=True),
                controller.cleanup(execute=True, include_core=False, confirmation=None),
                controller.recover(execute=True, include_core=False, confirmation=None),
            )

        self.assertEqual([report.disposition for report in reports],
                         ["REFUSED_CONFIRMATION"] * 3)
        self.assertEqual(executor.calls, [])

    def test_hardware_free_usrp_and_network_scripts_report_unknown_not_ready(self):
        root = Path(__file__).resolve().parents[1]
        for name, category in (("check_usrp.sh", "USRP"),
                               ("check_network.sh", "NETWORK")):
            script = (root / "tools" / "labctl" / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn(f"{category}_UNKNOWN", script)
            self.assertNotIn(f"{category}_READY", script)

    def test_staged_config_is_explicitly_not_radio_applied_in_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            body = create_receipt(
                inventory={"profileId": "mock-lab", "components": ["core"]},
                probes=(Probe("core", lambda: (True, "mock ready")),),
                configuration_identity={"inventoryProfileId": "mock-lab"},
                started_at="2026-08-26T00:00:00Z", now="2026-08-26T00:00:01Z",
                output_path=Path(directory) / "receipt.json",
            )

        self.assertEqual(body["configurationState"]["staticConfig"], "STAGED_NOT_RADIO_APPLIED")

    def test_start_failure_reports_partial_start_and_reverse_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # core status not ready, core start succeeds, core readiness succeeds;
            # gNB status not ready, gNB start fails, then core is rolled back.
            executor = MockExecutor((1, 0, 0, 1, 1, 0))
            controller = LabOrchestrator(load_inventory(_inventory(root)), executor, RunStore(root / "runs"))

            report = controller.prepare(execute=True, confirmation="YES")

        self.assertEqual(report.disposition, "ROLLED_BACK")
        self.assertEqual(
            [(item.component_id, item.action, item.outcome) for item in report.components],
            [("core", "start", "START_REQUESTED"),
             ("gnb1", "start", "START_REQUESTED"),
             ("core", "rollback", "STOPPED")],
        )

    def test_receipt_is_non_secret_and_cockpit_reader_cannot_start_labctl_or_use_verdict_evidence(self):
        from gui.operator.sources.lab_readiness import read_lab_setup_receipt

        with tempfile.TemporaryDirectory() as directory:
            receipt_path = Path(directory) / "readiness.json"
            create_receipt(
                inventory={"profileId": "mock-lab", "components": ["core", "gnb1"]},
                probes=(Probe("core", lambda: (True, "mock ready")),
                        Probe("gnb1", lambda: (False, "mock not ready"))),
                configuration_identity={"inventoryProfileId": "mock-lab"},
                started_at="2026-08-26T00:00:00Z", now="2026-08-26T00:00:01Z",
                output_path=receipt_path,
            )
            view = read_lab_setup_receipt(
                receipt_path, now="2026-08-26T00:00:02Z", maximum_age_ms=60_000)

        self.assertEqual(view.state, "NOT_READY")
        self.assertFalse(view.may_start_lab_setup)
        self.assertFalse(view.counts_as_verdict_evidence)
        self.assertTrue(view.secret_free)

    def test_cockpit_import_scan_bans_labctl_driving_modules_and_rejects_injection(self):
        from tests.gui.test_cockpit_acceptance import (
            BANNED_TRANSPORT_IMPORTS, COCKPIT_SOURCES, transport_import_offenders,
        )

        self.assertTrue({"tools.labctl.cli", "tools.labctl.executor", "tools.labctl.orchestrator"}
                        .issubset(BANNED_TRANSPORT_IMPORTS))
        self.assertEqual(transport_import_offenders(COCKPIT_SOURCES), [])
        with tempfile.TemporaryDirectory() as directory:
            injected = Path(directory) / "cockpit_import.py"
            injected.write_text("from tools.labctl.executor import CommandExecutor\n", encoding="utf-8")
            self.assertTrue(transport_import_offenders((injected,)))

    def test_profile_is_self_contained_and_does_not_name_an_external_checkout(self):
        root = Path(__file__).resolve().parents[1]
        sources = tuple((root / "tools" / "labctl").rglob("*")) + (
            root / "gui" / "operator" / "sources" / "lab_readiness.py", Path(__file__).resolve(),
        )
        source_suffixes = {".py", ".json", ".sh", ".md"}
        scanned = [path for path in sources if path.suffix in source_suffixes]
        self.assertTrue(scanned)
        text = "\n".join(path.read_text(encoding="utf-8") for path in scanned)

        self.assertNotIn("/" + "home" + "/", text)
        self.assertNotIn("ai-ran-" + "wt", text)
        self.assertNotIn("Oran" + "C/", text)


if __name__ == "__main__":
    unittest.main()
