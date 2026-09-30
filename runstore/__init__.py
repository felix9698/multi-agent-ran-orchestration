"""Run-directory persistence, independent of any console.

One schema for Live, Replay, Synthetic and Emulated runs.  Schema authority:
``docs/phase-b-gui/session-store.1.0.0.schema.json``.

This package is stdlib-only and imports nothing from ``gui``.  It sits at the
top level rather than under ``gui/operator/`` because the run directory is not
a rendering concern: the Operator Console writes one, and so does the
hardware-free batch/export boundary in ``assurance/batch/``, which must never
import a console (Gate 2, ``tests/gui/test_obj3_registry_projection.py::
ThePaneHasNoPathBackIntoTheState::test_assurance_never_imports_the_gui``).

``gui/operator/store/`` remains as a re-export of this package so every
existing console import keeps resolving and the frozen ``session_store``
surface stays reachable at the path ``file-ownership.1.0.0.json`` names.
"""
