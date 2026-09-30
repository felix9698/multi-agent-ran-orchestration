"""Re-export of :mod:`runstore.records`.

The record builders never depended on a toolkit or on anything under ``gui``;
only their *location* did.  They now live in the neutral top-level
``runstore`` package so ``assurance/batch/`` can write the same run-directory
schema without importing a console (Gate 2, proven by
``tests/gui/test_obj3_registry_projection.py::test_assurance_never_imports_the_gui``).

This module stays so every console import keeps resolving unchanged.  A star
re-export rather than a hand-kept list: a name added to ``runstore.records``
must appear here too, and a transcribed list would silently fall behind.
``tests/test_runstore_neutrality.py`` asserts the two surfaces stay equal.
"""

from runstore.records import *  # noqa: F401,F403
