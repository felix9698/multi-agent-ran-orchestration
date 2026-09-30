"""Phase B Operator Console.

The O-RAN-boundary-compliant operator GUI.  Design authority:
``docs/phase-b-gui/DESIGN.md``.

Nothing is imported eagerly here.  The headless entry points
(``python -m tools.legacy.coordinator_console --no-gui`` / ``--cmd``,
``python -m oran.rapp.headless``) must never pull in tkinter or matplotlib, and
``gui.operator.status``/``tokens``/``viewmodel``/``store`` are imported by
hermetic tests that run without a display.  Import the shell explicitly::

    from gui.operator.app import OperatorConsole
"""
