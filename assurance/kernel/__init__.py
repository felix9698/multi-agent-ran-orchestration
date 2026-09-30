"""The deterministic Assurance Kernel (design section 4.3).

Owner lane: **KERN**.  Every module in this package belongs to that lane; see
``docs/architecture/SEAMS-GATE2.md``.

The Kernel is the only component with authority.  It admits contracts, freezes
epochs, generates and hashes the finite catalog, drives trial transitions,
fences transactions, accounts harm, evaluates raw traces, closes evidence,
releases target vectors, terminates cases and recovers from crashes.  It does
none of the acting: dynamic equipment changes go through
:mod:`assurance.gateway`, and raw observations arrive from
:mod:`assurance.collector`.

It also does none of the proposing.  The three advisory agents in
:mod:`assurance.advisors` submit typed messages through
:meth:`~assurance.kernel.kernel.AssuranceKernel.submit_advisory` and receive
either admission into the mailbox or a rejection -- never authority.
"""

from __future__ import annotations

from assurance.kernel.event_store import EventStore, EventStoreError
from assurance.kernel.kernel import AssuranceKernel, KernelRefusal
from assurance.kernel.reducer import Reducer, ReducerError, replay, terminal_state_hash

__all__ = [
    "AssuranceKernel",
    "EventStore",
    "EventStoreError",
    "KernelRefusal",
    "Reducer",
    "ReducerError",
    "replay",
    "terminal_state_hash",
]
