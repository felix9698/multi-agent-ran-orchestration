"""Re-export of :mod:`runstore.session_store`.

The store moved to the neutral top-level ``runstore`` package so the
hardware-free batch/export boundary can write the one run-directory schema
without importing a console (Gate 2).  Nothing about the seam changed: the
frozen surface ``docs/phase-b-gui/file-ownership.1.0.0.json`` describes for
this path is the very class re-exported below, and
``tests/gui/test_session_store.py`` still checks it through this import.

This module must never import ``tkinter``, ``subprocess`` or ``socket`` - and
neither must the module it re-exports; ``tests/test_gui_operator_seams.py``
scans both.
"""

from runstore.session_store import *  # noqa: F401,F403
