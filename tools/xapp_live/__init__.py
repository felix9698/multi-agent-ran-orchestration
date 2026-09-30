"""The composition root for live xApp actuation over the lab telnet knobs.

:mod:`assurance.xapps.live_actuation` holds the *shape* of a live specialist
xApp write -- the grammar of each ``ci`` knob, the permit scope, the
fail-closed parsing -- and cannot open a transport: nothing under
``assurance/`` may import :mod:`socket`.  This package is where the socket
lives, the same division ``tools/g3ota`` has with :mod:`assurance.live`.

Two modules:

``transport``
    a per-command telnet client for the OAI shell.
``run_xapp``
    one assignment, executed live under a Kernel-issued permit, with the
    deterministic rollback on the way out.
"""
