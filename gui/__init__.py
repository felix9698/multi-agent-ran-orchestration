"""The repository's GUI package.

``IntentCoordinatorGUI`` stays importable as ``gui.IntentCoordinatorGUI``, but
it is resolved on **first attribute access** rather than at package import.

Importing it eagerly meant that ``import gui.operator.status`` - a pure data
module with no widget in it - pulled in ``gui.dashboard``, and with it tkinter
and the legacy console's module-level work.  That defeats three things at once:
the Operator Console's headless-import property, the import-graph gate in
``tests/test_gui_boundary_scan.py`` (``gui.dashboard`` is a forbidden import for
console code, and an eager re-export put it in every console module's graph),
and any use of ``gui.operator`` from the export pipeline on a machine with no
display.

The legacy console's behaviour is unchanged.  It is imported by
``tools/legacy/coordinator_console.py`` - which is where the B-01 cutover moved
that entry, since ``main.py`` now composes the Cockpit over the Assurance
Kernel and reaches no part of the pre-Kernel runtime; the
``AIC_LEGACY_CONSOLE_APPROVED`` gate is untouched; and
``gui.IntentCoordinatorGUI`` still returns exactly the same class to anything
that asks for it - the import simply happens when it is asked for.
"""

from typing import Any

__all__ = ["IntentCoordinatorGUI"]


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute resolution for the legacy console class."""
    if name == "IntentCoordinatorGUI":
        from .dashboard import IntentCoordinatorGUI

        globals()[name] = IntentCoordinatorGUI
        return IntentCoordinatorGUI
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list:
    return sorted(set(globals()) | {"IntentCoordinatorGUI"})
