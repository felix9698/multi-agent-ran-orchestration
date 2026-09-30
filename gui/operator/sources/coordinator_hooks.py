"""Safe multiplexing for ``IntentCoordinator.on_state_change``.

The coordinator exposes one callback slot and intentionally does not isolate
callback errors.  Replacing that slot would break the experiment runner's
``_StateTracer``; allowing a UI exception through it could alter the episode.
This module therefore preserves the existing listener and makes every added
observer best-effort and timestamped.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class CoordinatorTransition:
    source: str
    target: str
    observed_at: str
    monotonic_s: float


TransitionListener = Callable[[CoordinatorTransition], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class CoordinatorHookMultiplexer:
    """Retain the safety listener and isolate only added view observers.

    The pre-installed callback remains part of the coordinator transaction: an
    exception from it must propagate so the S4 backstop can roll back.  Only
    listeners added for presentation are best-effort.
    """

    def __init__(self, previous: Optional[Callable[..., Any]] = None) -> None:
        self._previous = previous
        self._listeners: list[TransitionListener] = []
        self._lock = threading.RLock()

    def add(self, listener: TransitionListener) -> Callable[[], None]:
        if not callable(listener):
            raise TypeError("listener must be callable")
        with self._lock:
            self._listeners.append(listener)

        def remove() -> None:
            with self._lock:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass

        return remove

    def __call__(self, source: str, target: str) -> None:
        transition = CoordinatorTransition(
            source=str(source), target=str(target), observed_at=_utc_now(),
            monotonic_s=time.monotonic())
        # The pre-installed listener keeps its historical two-argument shape.
        if self._previous is not None:
            self._previous(source, target)
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(transition)
            except Exception:
                pass


def install_coordinator_hook(coordinator: Any,
                             listener: TransitionListener) -> Callable[[], None]:
    """Install ``listener`` without replacing an existing state tracer.

    Repeated installs share one multiplexer.  The returned callable removes only
    the newly added listener; it never restores or overwrites another owner's
    callback slot.
    """
    current = getattr(coordinator, "on_state_change", None)
    if isinstance(current, CoordinatorHookMultiplexer):
        mux = current
    else:
        mux = CoordinatorHookMultiplexer(current)
        coordinator.on_state_change = mux
    return mux.add(listener)


# Compatibility name used by callers that treat installation as subscription.
subscribe_state_changes = install_coordinator_hook


__all__ = [
    "CoordinatorHookMultiplexer", "CoordinatorTransition",
    "install_coordinator_hook", "subscribe_state_changes",
]
