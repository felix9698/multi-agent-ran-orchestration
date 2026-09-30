"""Live readiness probes for the receipt command.

Each inventory component already declares a ``status`` command that returns
zero when the component is up on its host (local or over ssh).  This module
turns those declarations into :class:`~tools.labctl.receipt.Probe` objects so
``labctl receipt`` reports the *actual* testbed state instead of an empty
readiness set.

A probe is preparation evidence only: running a component's ``status`` command
is a read, never a candidate, an objective effect, or OTA success evidence, and
the receipt's ``boundary`` block continues to say so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .executor import CommandExecutor
from .models import LabInventory
from .receipt import Probe


def live_probes(inventory: LabInventory, *, run_dir: Path,
                executor: CommandExecutor | None = None) -> Sequence[Probe]:
    """One probe per component, checking its declared ``status`` command.

    The check runs on the component's own host (``executor`` handles the
    local/ssh transport) and maps exit code 0 to READY.  The detail string is
    the trimmed status output, so a NOT_READY component names why.
    """
    runner = executor or CommandExecutor()
    probes: list[Probe] = []
    for component in inventory.ordered_components():
        if not component.required:
            continue
        host = inventory.hosts[component.host]
        status = component.status

        def _check(host=host, status=status, cid=component.id) -> tuple[bool, str]:
            result = runner.run(host, status, run_dir=run_dir,
                                label=f"probe-{cid}", dry_run=False)
            text = ""
            for path in (result.stdout_path, result.stderr_path):
                try:
                    body = path.read_text(errors="replace").strip()
                except OSError:
                    body = ""
                if body:
                    text = body
                    break
            lines = text.splitlines()
            summary = lines[-1] if lines else f"exit {result.exit_code}"
            return result.exit_code == 0, summary[:200]

        probes.append(Probe(component_id=component.id, check=_check))
    return probes
