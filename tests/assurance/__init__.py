"""Gate 2 assurance-seam tests.

This package needs an ``__init__.py`` - ``python3 -m unittest discover -s tests``
on Python 3.10 silently skips a directory without one, so the whole Gate 2 seam
suite would vanish from the canonical command.

Having one creates a name collision that has to be handled here, and only here.
``discover -s tests`` puts ``tests/`` on ``sys.path``, so this directory is
imported under the top-level name ``assurance`` - which is also the name of the
repository's real assurance package.  Without the line below, every module that
does ``from assurance.core import ...`` fails to import under discovery.

Extending ``__path__`` makes the two directories one package for import
purposes, so ``assurance.core``, ``assurance.kernel`` and
``assurance.test_seams`` all resolve whichever way the suite is invoked:

* ``python3 -m unittest discover -s tests``           (this file is ``assurance``)
* ``python3 -m unittest discover -s tests/assurance`` (this file is the top level)
* ``python3 -m unittest discover -s tests -t .``      (this file is ``tests.assurance``)
* ``python3 -m unittest tests.assurance.test_seams``

This is the same treatment ``tests/gui/__init__.py`` already applies to the
``gui`` collision, for the same reason and with the same limit: the
repository's own ``assurance/__init__.py`` is deliberately *not* re-executed
here, because re-running a package body under a second name is how you end up
with two of something.  Nothing in it is needed - it only re-exports
``ASSURANCE_SCHEMA_VERSION``, which the tests import from
``assurance.core.envelopes`` where it is defined.

Test file naming is fixed by ``docs/architecture/SEAMS-GATE2.md`` section 4:
``test_<lane>_*.py`` for a lane's own tests, ``test_seams.py`` for the shared
seam contract every lane depends on.
"""

import os

_REPO_ASSURANCE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "assurance")

if os.path.isdir(_REPO_ASSURANCE) and _REPO_ASSURANCE not in __path__:
    __path__.append(_REPO_ASSURANCE)
