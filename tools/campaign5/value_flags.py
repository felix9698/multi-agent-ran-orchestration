"""Which command-line flag carries each Campaign 5 family's value.

Its own module, and deliberately transport-free.  ``main.py`` needs this table
to build its parser, and ``tools.campaign5.live_run`` -- which imports the R1
consumer and the KPM tail -- must not be loaded to get it: the default console
entry point loads no transport at all, and
``tests/test_liveconsole.py::TheLazyImportKeepsTheDefaultEntryClean`` holds it
to that.

One flag per *value leaf*, so ``mcs`` has two: a family that moves two leaves
cannot be described by one number, and the flag list is where that stops being
a comment and becomes an argument the operator has to supply.
"""

from __future__ import annotations

from typing import Dict, Final, Tuple

__all__ = ["FAMILY_VALUE_FLAGS"]

FAMILY_VALUE_FLAGS: Final[Dict[str, Tuple[Tuple[str, str], ...]]] = {
    "cap": (("--cap", "maxDlPrbs"),),
    "priority": (("--pf-weight", "pfWeight"),),
    "mcs": (("--mcs-min", "minDlMcs"), ("--mcs-max", "maxDlMcs")),
    "power": (("--tx-atten-db", "txAttenuationDb"),),
}
