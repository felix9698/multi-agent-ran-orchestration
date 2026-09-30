"""Phase B Operator Console - the state bus.

Signatures frozen by the design step; body owned and implemented by track **T1**
(see ``docs/phase-b-gui/file-ownership.1.0.0.json``).  Consumers code against the
signatures below and must not change them.

The single rule that makes the console non-blocking, headless-testable and
renderer-agnostic:

    the render layer reads only from the StateBus,
    and the data layer writes only to it.

Worker threads (coordinator episodes, R1 status projection, replay loading,
figure export, model discovery) call :meth:`StateBus.publish`.  The Tk thread
calls :meth:`StateBus.drain` on a fixed cadence and dispatches to subscribers.
No widget is ever touched off the Tk thread, and no I/O ever happens on it.

Implementation constraints the body must honour - these are the contract, not
suggestions:

* ``publish`` is callable from any thread, never blocks, and never raises.  A
  publisher is usually holding a lock somewhere upstream; blocking it would
  stall the coordinator.
* Under back-pressure the bus sheds the **oldest** records and increments
  :meth:`dropped`.  The footer displays that count, so a dropped update is
  visible rather than silent.  It must never grow without bound.
* A subscriber that raises is caught and logged.  It cannot affect the publisher
  or any other subscriber.  This mirrors ``_safe_gui`` in the coordinator, and
  it is the same reasoning as ``run_gui_once`` swallowing a failing presenter:
  presentation is downstream and powerless.
* ``drain`` is called from the Tk thread only, and honours ``budget`` so a burst
  cannot monopolise a frame.
* This module must never import ``tkinter``.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Callable, Dict, Final, List, Sequence, Tuple

logger = logging.getLogger("gui.operator.bus")

#: The fixed channel set.  A publisher naming an unknown channel is a programming
#: error and must be rejected loudly at publish time rather than silently dropped -
#: a typo'd channel that vanishes is exactly the class of bug this console cannot
#: afford during a live experiment.
CHANNELS: Final[Tuple[str, ...]] = (
    "session",    # SessionState replacement or partial update
    "fsm",        # FSM transitions, timestamped by the hook multiplexer
    "intent",     # intent table rows
    "decision",   # DecisionView
    "policy",     # A1 policy status projections
    "evidence",   # DME/O1 evidence state
    "metric",     # MetricSampleView batches
    "timeline",   # TimelineEventView
    "health",     # component status / readiness
    "llm",        # backend availability and switch audit
    "warning",    # operator-visible warnings and errors
    "export",     # export progress and completion
    "cockpit",    # CockpitSnapshot: header, Trial & Safety, Evidence Ledger
    "batch",      # BatchConsoleView: the bounded plan and its confirmed run
)


#: Reported on the ``warning`` channel when a publisher names a channel that is
#: not in :data:`CHANNELS`.  The record is never silently discarded.
UNKNOWN_CHANNEL_KIND: Final[str] = "BUS_UNKNOWN_CHANNEL"

#: Hard cap on the misrouted-record description.  A payload may be anything, and
#: an unbounded repr on a hot path would be its own denial of service.
MISROUTE_DESCRIPTION_CHARS: Final[int] = 200


def _describe(payload: Any) -> str:
    """A short, always-safe description of a misrouted record.

    ``repr`` on a foreign object can itself raise, and this runs on a path whose
    entire purpose is that it cannot fail, so the failure is described rather
    than propagated.
    """
    try:
        text = repr(payload)
    except Exception:
        return f"<unrepresentable {type(payload).__name__}>"
    if len(text) > MISROUTE_DESCRIPTION_CHARS:
        return text[:MISROUTE_DESCRIPTION_CHARS] + "... (truncated)"
    return text


class StateBus:
    """Thread-safe, bounded, shed-oldest state distribution.

    The queue is a single ordered log rather than one queue per channel, because
    the operator reads the timeline and the status panes as one story: a metric
    that arrived before a transition must dispatch before it, whichever channel
    carried it.
    """

    def __init__(self, *, max_queue: int = 4096) -> None:
        self._max_queue = max(1, int(max_queue))
        self._lock = threading.Lock()
        self._queue: deque = deque()
        self._subscribers: Dict[str, List[Callable[[Any], None]]] = {
            channel: [] for channel in CHANNELS}
        self._latest: Dict[str, Any] = {}
        self._dropped = 0
        self._rejected = 0
        self._published = 0
        self._high_water = 0

    # -- producer side (any thread) ---------------------------------------- #

    def publish(self, channel: str, payload: Any) -> None:
        """Publish one record.  Never blocks, never raises."""
        try:
            self._offer(((channel, payload),))
        except Exception:                                 # pragma: no cover
            # A publisher is usually holding a lock upstream.  Nothing that
            # happens in here may ever propagate back into it.
            logger.exception("StateBus.publish failed for channel %r", channel)

    def publish_many(self, items: Sequence[Tuple[str, Any]]) -> None:
        """Publish a batch atomically with respect to ordering.

        The whole batch enters the queue under one lock acquisition, so another
        thread cannot interleave a record into the middle of it.
        """
        try:
            self._offer(tuple(items))
        except Exception:                                 # pragma: no cover
            logger.exception("StateBus.publish_many failed")

    def _offer(self, items: Sequence[Tuple[str, Any]]) -> None:
        accepted: List[Tuple[str, Any]] = []
        rejected: List[Tuple[str, Any]] = []
        for channel, payload in items:
            if channel in self._subscribers:
                accepted.append((channel, payload))
            else:
                rejected.append((str(channel), payload))
        for name, payload in rejected:
            # Loud, but never fatal: a typo'd channel that vanished silently is
            # exactly the class of bug this console cannot afford mid-experiment.
            # The record travels with the warning - truncated, because it may be
            # anything - so the misroute can be identified, not merely counted.
            logger.error("StateBus: unknown channel %r; record rerouted to "
                         "the warning channel", name)
            accepted.append(("warning", {
                "kind": UNKNOWN_CHANNEL_KIND, "channel": name,
                "record": _describe(payload)}))
        if not accepted:
            return
        with self._lock:
            self._rejected += len(rejected)
            for record in accepted:
                self._queue.append(record)
                self._latest[record[0]] = record[1]
                self._published += 1
            while len(self._queue) > self._max_queue:
                self._queue.popleft()
                self._dropped += 1
            if len(self._queue) > self._high_water:
                self._high_water = len(self._queue)

    # -- consumer side (Tk thread) ----------------------------------------- #

    def subscribe(self, channel: str,
                  handler: Callable[[Any], None]) -> Callable[[], None]:
        """Register ``handler`` for ``channel``; returns an unsubscribe callable."""
        if channel not in self._subscribers:
            raise ValueError(f"unknown channel: {channel!r}")
        with self._lock:
            self._subscribers[channel].append(handler)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers[channel].remove(handler)
                except ValueError:
                    pass

        return unsubscribe

    def drain(self, *, budget: int = 256) -> int:
        """Dispatch up to ``budget`` queued records.  Returns the number sent.

        Tk thread only.  Dispatch order is publish order.
        """
        sent = 0
        limit = max(0, int(budget))
        while sent < limit:
            with self._lock:
                if not self._queue:
                    break
                channel, payload = self._queue.popleft()
                # Copy under the lock: a handler may unsubscribe itself while it
                # is running, and mutating the live list mid-dispatch would skip
                # its neighbour.
                handlers = tuple(self._subscribers.get(channel, ()))
            for handler in handlers:
                try:
                    handler(payload)
                except Exception:
                    # A failing subscriber is a presentation failure.  It cannot
                    # affect the publisher or another subscriber - the same
                    # reasoning as run_gui_once swallowing a broken presenter.
                    logger.exception(
                        "StateBus subscriber on channel %r raised", channel)
            sent += 1
        return sent

    def snapshot(self, channel: str) -> Any:
        """Last value published on ``channel``, or ``None``.

        Lets a workspace that was inactive during an update repaint correctly
        when it is activated - which is how Demo View can be left and re-entered
        without losing experiment context.
        """
        with self._lock:
            return self._latest.get(channel)

    # -- diagnostics -------------------------------------------------------- #

    def dropped(self) -> int:
        """Records shed under back-pressure since construction."""
        with self._lock:
            return self._dropped

    def depth(self) -> int:
        """Current queue depth."""
        with self._lock:
            return len(self._queue)

    def rejected(self) -> int:
        """Records addressed to a channel that does not exist."""
        with self._lock:
            return self._rejected

    def stats(self) -> Dict[str, int]:
        """Counters for the footer and for the soak harness."""
        with self._lock:
            return {"published": self._published, "dropped": self._dropped,
                    "rejected": self._rejected, "depth": len(self._queue),
                    "highWater": self._high_water,
                    "maxQueue": self._max_queue}


__all__ = ["CHANNELS", "MISROUTE_DESCRIPTION_CHARS", "UNKNOWN_CHANNEL_KIND",
           "StateBus"]
