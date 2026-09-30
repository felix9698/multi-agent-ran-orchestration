"""Operator Console tests.

This package needs an ``__init__.py`` - ``python3 -m unittest discover -s tests``
on Python 3.10 silently skips a directory without one, so the whole Phase B GUI
suite would vanish from the canonical command.

Having one creates a name collision that has to be handled here, and only here.
``discover -s tests`` puts ``tests/`` on ``sys.path``, so this directory is
imported under the top-level name ``gui`` - which is also the name of the
repository's real GUI package.  Without the line below, every module that does
``from gui.operator import ...`` or ``from gui.dashboard import ...`` fails to
import under discovery, including three pre-existing test modules that have
nothing to do with this campaign.

Extending ``__path__`` makes the two directories one package for import
purposes, so ``gui.operator``, ``gui.dashboard`` and ``gui.test_shell_and_bus``
all resolve whichever way the suite is invoked:

* ``python3 -m unittest discover -s tests``      (this file is ``gui``)
* ``python3 -m unittest discover -s tests -t .`` (this file is ``tests.gui``)
* ``python3 -m unittest tests.gui.test_shell_and_bus``

The repository's own ``gui/__init__.py`` is deliberately *not* re-executed here.
It now resolves ``IntentCoordinatorGUI`` lazily, so importing it would no longer
pull tkinter into a headless discovery run - but re-running a package body under
a second name is still the kind of thing that produces two of something, and
there is nothing in it this package needs.  The tests that want the legacy
console import ``gui.dashboard`` explicitly, which works either way.
"""

import os

_REPO_GUI = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "gui")

if os.path.isdir(_REPO_GUI) and _REPO_GUI not in __path__:
    __path__.append(_REPO_GUI)
