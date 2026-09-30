"""Dependency-ordered, ownership-aware lab lifecycle orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from .inventory import LabInventory
from .models import ComponentSpec
from .state import RunRecord, RunStore, mutation_lock


CONFIRMATION = "YES"


@dataclass(frozen=True)
class ComponentReport:
    component_id: str
    action: str
    outcome: str
    exit_code: Optional[int]
    preexisting: bool = False


@dataclass(frozen=True)
class ActionReport:
    action: str
    profile_id: str
    disposition: str
    components: Tuple[ComponentReport, ...]
    run_directory: Path


class LabOrchestrator:
    def __init__(self, inventory: LabInventory, executor, store: RunStore):
        self.inventory = inventory
        self.executor = executor
        self.store = store

    def _run_command(self, run, component, command, label, dry_run):
        return self.executor.run(
            self.inventory.hosts[component.host],
            command,
            run_dir=run.run_dir,
            label=label,
            dry_run=dry_run,
        )

    def _report(self, action, disposition, reports, run):
        self.store.finish(run, disposition)
        return ActionReport(
            action=action,
            profile_id=self.inventory.profile_id,
            disposition=disposition,
            components=tuple(reports),
            run_directory=run.run_dir,
        )

    def _observe(self, action: str) -> ActionReport:
        run = self.store.begin(self.inventory.profile_id, action)
        reports = []
        all_ready = True
        for component in self.inventory.ordered_components():
            result = self._run_command(
                run, component, component.status, f"{component.id}-status", False
            )
            ready = result.exit_code == 0
            all_ready = all_ready and ready
            reports.append(
                ComponentReport(
                    component.id,
                    "status",
                    "READY" if ready else "NOT_READY",
                    result.exit_code,
                    preexisting=ready,
                )
            )
        return self._report(action, "READY" if all_ready else "NOT_READY", reports, run)

    def status(self) -> ActionReport:
        return self._observe("status")

    def preflight(self) -> ActionReport:
        return self._observe("preflight")

    def _rollback(
        self, run: RunRecord, owned: List[ComponentSpec], reports: List[ComponentReport]
    ) -> None:
        for component in reversed(owned):
            if component.stop_policy == "never":
                reports.append(
                    ComponentReport(component.id, "rollback", "PRESERVED_BY_POLICY", None)
                )
                continue
            if component.stop is None:
                reports.append(ComponentReport(component.id, "rollback", "NO_STOP_COMMAND", None))
                continue
            result = self._run_command(
                run, component, component.stop, f"{component.id}-rollback", False
            )
            if result.exit_code == 0:
                self.store.mark_stopped(run, component.id)
            reports.append(
                ComponentReport(
                    component.id,
                    "rollback",
                    "STOPPED" if result.exit_code == 0 else "STOP_FAILED",
                    result.exit_code,
                )
            )

    def apply_config(self, *, execute: bool, confirmation: Optional[str]) -> ActionReport:
        """Stage only declared static configuration; it never deploys to a radio."""
        with mutation_lock(self.inventory.state_root):
            run = self.store.begin(self.inventory.profile_id, "apply-config")
            reports: List[ComponentReport] = []
            if execute and confirmation != CONFIRMATION:
                return self._report("apply-config", "REFUSED_CONFIRMATION", reports, run)
            for component in self.inventory.ordered_components():
                if component.configure is None:
                    continue
                result = self._run_command(
                    run, component, component.configure, f"{component.id}-configure", not execute
                )
                reports.append(ComponentReport(
                    component.id, "configure",
                    "CONFIG_STAGED" if execute and result.exit_code == 0 else
                    "WOULD_STAGE_CONFIG" if not execute else "CONFIG_FAILED",
                    result.exit_code,
                ))
                if execute and result.exit_code != 0:
                    return self._report("apply-config", "FAILED", reports, run)
            return self._report("apply-config", "COMPLETED" if execute else "DRY_RUN", reports, run)

    def prepare(self, *, execute: bool, rf_approval: Optional[str] = None,
                confirmation: Optional[str] = None) -> ActionReport:
        """Start the declared dependency order after the explicit operator confirmation."""
        with mutation_lock(self.inventory.state_root):
            run = self.store.begin(self.inventory.profile_id, "prepare")
            reports: List[ComponentReport] = []
            owned: List[ComponentSpec] = []
            if execute and (confirmation or rf_approval) != CONFIRMATION:
                return self._report("prepare", "REFUSED_CONFIRMATION", reports, run)
            for component in self.inventory.ordered_components():
                status = self._run_command(
                    run, component, component.status, f"{component.id}-initial-status", False
                )
                if status.exit_code == 0:
                    self.store.mark_started(run, component.id, preexisting=True)
                    reports.append(
                        ComponentReport(component.id, "prepare", "PREEXISTING", 0, True)
                    )
                    continue
                if not component.required:
                    reports.append(ComponentReport(
                        component.id, "prepare", "OPTIONAL_NOT_READY", status.exit_code
                    ))
                    continue
                if component.start is None and not execute:
                    reports.append(
                        ComponentReport(component.id, "start", "NO_START_COMMAND", None)
                    )
                    continue
                if component.start is None:
                    self._rollback(run, owned, reports)
                    return self._report("prepare", "ROLLED_BACK", reports, run)
                started = self._run_command(
                    run,
                    component,
                    component.start,
                    f"{component.id}-start",
                    not execute,
                )
                reports.append(
                    ComponentReport(
                        component.id,
                        "start",
                        "WOULD_START" if not execute else "START_REQUESTED",
                        started.exit_code,
                    )
                )
                if not execute:
                    continue
                if started.exit_code != 0:
                    self._rollback(run, owned, reports)
                    return self._report("prepare", "ROLLED_BACK", reports, run)
                self.store.mark_started(run, component.id, preexisting=False)
                owned.append(component)
                ready = False
                for attempt in range(1, 4):
                    observed = self._run_command(
                        run,
                        component,
                        component.status,
                        f"{component.id}-readiness-{attempt}",
                        False,
                    )
                    if observed.exit_code == 0:
                        ready = True
                        break
                if not ready:
                    self._rollback(run, owned, reports)
                    return self._report("prepare", "ROLLED_BACK", reports, run)
            return self._report(
                "prepare", "COMPLETED" if execute else "DRY_RUN", reports, run
            )

    def cleanup(self, *, execute: bool, include_core: bool,
                confirmation: Optional[str]) -> ActionReport:
        """Return components owned by a prior run to the stopped safe state."""
        if execute and confirmation != CONFIRMATION:
            run = self.store.begin(self.inventory.profile_id, "cleanup")
            return self._report("cleanup", "REFUSED_CONFIRMATION", [], run)
        report = self.stop(execute=execute, include_core=include_core)
        return ActionReport("cleanup", report.profile_id, report.disposition,
                            report.components, report.run_directory)

    def recover(self, *, execute: bool, include_core: bool,
                confirmation: Optional[str]) -> ActionReport:
        """Recovery is cleanup only: it cannot silently restart RF equipment."""
        report = self.cleanup(execute=execute, include_core=include_core,
                              confirmation=confirmation)
        return ActionReport("recover", report.profile_id, report.disposition,
                            report.components, report.run_directory)

    def stop(self, *, execute: bool, include_core: bool) -> ActionReport:
        with mutation_lock(self.inventory.state_root):
            owned_run = self.store.latest_owning_run()
            run = self.store.begin(self.inventory.profile_id, "stop")
            reports: List[ComponentReport] = []
            ownership = (owned_run.body.get("components", {}) if owned_run else {})
            for component in reversed(self.inventory.ordered_components()):
                entry = ownership.get(component.id, {})
                owned = (
                    entry.get("ownership") == "STARTED_BY_THIS_RUN"
                    and "stoppedAt" not in entry
                )
                if component.id == "core" and not include_core:
                    reports.append(ComponentReport(component.id, "stop", "PRESERVED_CORE", None))
                    continue
                if not owned:
                    reports.append(ComponentReport(component.id, "stop", "NOT_OWNED", None))
                    continue
                if component.stop is None:
                    reports.append(ComponentReport(component.id, "stop", "NO_STOP_COMMAND", None))
                    continue
                result = self._run_command(
                    run,
                    component,
                    component.stop,
                    f"{component.id}-stop",
                    not execute,
                )
                if execute and result.exit_code == 0 and owned_run is not None:
                    self.store.mark_stopped(owned_run, component.id)
                reports.append(
                    ComponentReport(
                        component.id,
                        "stop",
                        "STOPPED" if execute and result.exit_code == 0 else "WOULD_STOP",
                        result.exit_code,
                    )
                )
            return self._report(
                "stop", "COMPLETED" if execute else "DRY_RUN", reports, run
            )
