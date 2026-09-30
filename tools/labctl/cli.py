"""Operator command-line interface for the separate lab lifecycle controller."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Optional

from .executor import CommandExecutor
from .inventory import InventoryError, load_inventory
from .orchestrator import ActionReport, LabOrchestrator
from .receipt import Probe, create_receipt
from .state import RunStore, StateError


DEFAULT_INVENTORY = Path(__file__).resolve().parent / "profiles/pc1-dual-cell-prb24.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="labctl",
        description="Guarded Core/RIC/gNB/UE laboratory preparation outside the O-RAN GUI",
    )
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--overlay", type=Path)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("status", help="observe every configured component")
    subcommands.add_parser("preflight", help="run read-only readiness checks")
    prepare = subcommands.add_parser("prepare", help="show or execute ordered preparation")
    prepare.add_argument("--execute", action="store_true")
    prepare.add_argument("--yes", action="store_true", help="confirm the displayed preparation")
    config = subcommands.add_parser("apply-config", help="apply declared static configuration")
    config.add_argument("--execute", action="store_true")
    config.add_argument("--yes", action="store_true", help="confirm the displayed configuration")
    stop = subcommands.add_parser("stop", help="show or execute owned-component teardown")
    stop.add_argument("--execute", action="store_true")
    stop.add_argument("--yes", action="store_true", help="confirm the displayed teardown")
    stop.add_argument("--include-core", action="store_true")
    cleanup = subcommands.add_parser("cleanup", help="return owned components to the stopped safe state")
    cleanup.add_argument("--execute", action="store_true")
    cleanup.add_argument("--yes", action="store_true", help="confirm the displayed cleanup")
    cleanup.add_argument("--include-core", action="store_true")
    recover = subcommands.add_parser("recover", help="perform safe cleanup after a failed start")
    recover.add_argument("--execute", action="store_true")
    recover.add_argument("--yes", action="store_true", help="confirm the displayed recovery")
    recover.add_argument("--include-core", action="store_true")
    receipt = subcommands.add_parser("receipt", help="write a Lab Setup readiness receipt")
    receipt.add_argument("--output", type=Path, required=True)
    receipt.add_argument("--started-at")
    receipt.add_argument("--live-probes", action="store_true",
                         help="explicitly opt in to declared status commands")
    subcommands.add_parser("logs", help="print the latest immutable run record location")
    return parser


def _component_json(component) -> dict:
    return {
        "componentId": component.component_id,
        "action": component.action,
        "outcome": component.outcome,
        "exitCode": component.exit_code,
        "preexisting": component.preexisting,
    }


def _report_json(report: ActionReport) -> dict:
    return {
        "schemaVersion": "oran-aic-labctl-report/1.0.0",
        "action": report.action,
        "profileId": report.profile_id,
        "disposition": report.disposition,
        "components": [_component_json(component) for component in report.components],
        "runDirectory": str(report.run_directory),
    }


def _exit_code(disposition: str) -> int:
    if disposition in {"READY", "COMPLETED", "DRY_RUN"}:
        return 0
    if disposition.startswith("REFUSED"):
        return 2
    return 1


def main(argv: Optional[Iterable[str]] = None, *, executor=None,
         probes: Iterable[Probe] = ()) -> int:
    arguments = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        inventory = load_inventory(arguments.inventory, overlay_path=arguments.overlay)
        store = RunStore(inventory.state_root)
        if arguments.command == "logs":
            latest = store.latest()
            body = {
                "schemaVersion": "oran-aic-labctl-report/1.0.0",
                "action": "logs",
                "profileId": inventory.profile_id,
                "disposition": "FOUND" if latest else "EMPTY",
                "components": [],
                "runDirectory": str(latest.run_dir) if latest else None,
            }
            print(json.dumps(body, sort_keys=True))
            return 0 if latest else 1
        if arguments.command == "receipt":
            started_at = arguments.started_at
            if started_at is None:
                from assurance.core.timebase import utc_now_text
                started_at = utc_now_text()
            active_probes = tuple(probes)
            if not active_probes and arguments.live_probes:
                # Tests inject the probe port.  Live probing is a separate,
                # explicit operator action and is never reached by default.
                from .live_probes import live_probes
                run = store.begin(inventory.profile_id, "receipt")
                active_probes = tuple(live_probes(inventory, run_dir=run.run_dir))
            receipt = create_receipt(
                inventory={
                    "schemaVersion": inventory.schema_version,
                    "profileId": inventory.profile_id,
                    "objective": inventory.objective,
                    "components": [component.id for component in inventory.ordered_components()],
                },
                probes=active_probes,
                configuration_identity={
                    "inventoryProfileId": inventory.profile_id,
                    "inventorySchemaVersion": inventory.schema_version,
                },
                started_at=started_at,
                output_path=arguments.output,
            )
            print(json.dumps({
                "schemaVersion": "oran-aic-labctl-report/1.0.0",
                "action": "receipt", "profileId": inventory.profile_id,
                "disposition": receipt["readiness"]["state"], "components": receipt["readiness"]["components"],
                "runDirectory": None, "receiptPath": str(arguments.output),
            }, sort_keys=True))
            return 0 if receipt["readiness"]["state"] == "READY" else 1
        orchestrator = LabOrchestrator(
            inventory, executor or CommandExecutor(), store
        )
        if arguments.command == "status":
            report = orchestrator.status()
        elif arguments.command == "preflight":
            report = orchestrator.preflight()
        elif arguments.command == "prepare":
            report = orchestrator.prepare(
                execute=arguments.execute,
                confirmation="YES" if arguments.yes else None,
            )
        elif arguments.command == "apply-config":
            report = orchestrator.apply_config(
                execute=arguments.execute,
                confirmation="YES" if arguments.yes else None,
            )
        elif arguments.command == "stop":
            report = orchestrator.cleanup(
                execute=arguments.execute, include_core=arguments.include_core,
                confirmation="YES" if arguments.yes else None,
            )
        elif arguments.command == "cleanup":
            report = orchestrator.cleanup(
                execute=arguments.execute, include_core=arguments.include_core,
                confirmation="YES" if arguments.yes else None,
            )
        elif arguments.command == "recover":
            report = orchestrator.recover(
                execute=arguments.execute, include_core=arguments.include_core,
                confirmation="YES" if arguments.yes else None,
            )
        else:  # argparse makes this unreachable.
            raise AssertionError(arguments.command)
        print(json.dumps(_report_json(report), sort_keys=True))
        return _exit_code(report.disposition)
    except (InventoryError, StateError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schemaVersion": "oran-aic-labctl-report/1.0.0",
                    "action": arguments.command,
                    "profileId": None,
                    "disposition": "REFUSED",
                    "components": [],
                    "runDirectory": None,
                    "error": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
