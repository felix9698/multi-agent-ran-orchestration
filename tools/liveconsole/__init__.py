"""Live console composition root: the Cockpit over the real O-RAN control path.

``tools/hfconsole`` wires the **Research Operations Cockpit** to a hardware-free
Kernel submission session; this package is its live sibling.  The Kernel, the
Write Gateway, the epoch freeze, the trial state machine and the evidence rules
are the same objects in both — what differs is that here the Write Gateway's
actuation adapter is the real ``R1Adapter``, so an admitted candidate leaves the
process over
``R1 → Non-RT RIC → A1-P → xApp → FlexRIC → E2SM-RC → OAI gNB``
(``docs/architecture/FINAL-ARCHITECTURE.md`` §4.2), the only path an objective
effect may travel.

Why a third composition root rather than code in ``gui`` or ``assurance``
------------------------------------------------------------------------
``gui`` imports no transport (``tests/gui/test_cockpit_acceptance.py``) and
``assurance`` imports no ``gui`` (``tests/assurance/test_seams.py``).  Building
a live ``VerticalPath`` from ``assurance`` and wrapping it in the ``gui``-side
``KernelSubmissionSession`` therefore has to happen outside both — which is
where ``tools/g3ota/run_ota.py`` already does it for a one-shot OTA episode.
This package does the same wiring for an *interactive* Cockpit session and is
imported lazily, inside ``main.py``'s ``--live`` branch only, so the default
entry point still composes a Disconnected console that has loaded no transport
at all (``tests/gui/test_default_entry_reachability.py``).

One authority, one objective
----------------------------
The deployment is resolved from a single live profile JSON
(:mod:`tools.liveconsole.profile`) rather than from a set of CLI defaults, and
one session freezes one objective's epoch — a second family would need a second
epoch, which is a session factory and a different work package.
"""

from tools.liveconsole.build import (
    GATE3_REGRESSION_CASE,
    LiveSession,
    attach_live_session,
    build_live_session,
    live_capable_families,
    session_mode,
    write_run_evidence,
)
from tools.liveconsole.profile import (
    LiveConsoleError,
    LiveDeployment,
    load_live_deployment,
)

__all__ = [
    "GATE3_REGRESSION_CASE",
    "LiveConsoleError",
    "LiveDeployment",
    "LiveSession",
    "attach_live_session",
    "build_live_session",
    "live_capable_families",
    "load_live_deployment",
    "session_mode",
    "write_run_evidence",
]
