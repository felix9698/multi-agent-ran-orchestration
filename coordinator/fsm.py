#!/usr/bin/env python3
"""
Typed FSM (state + event enums with a total legal transition table),
wall-clock deadline/timeout primitives, and a strict policy-response enum
(CLI handoff Batch C, P0-7).

The pre-Batch-C ``_transition(state)`` accepted an arbitrary string, so an
illegal or unknown target silently CREATED a state; there was no episode-wide
wall-clock bound, so a non-returning dependency (a hung model / policy call)
could run past ``N_max`` forever; and a negotiation-policy callback response
was interpreted by ad-hoc string compares.  This module makes all three
explicit and model-agnostic:

  * :class:`FSMState` / :class:`FSMEvent` enums + :data:`TRANSITIONS`, a total
    ``(state, event) -> next_state`` map.  An unknown event or an illegal
    transition is NOT a new state - the coordinator finalises TechnicalFailsafe
    (and latches) instead (section 3.1 / P0-7).
  * :class:`EpisodeDeadline`, an ABSOLUTE monotonic deadline fixed at episode
    start, plus :func:`call_with_timeout`, which runs one external call in a
    worker thread and abandons it when it does not return within
    ``min(op_timeout, remaining)`` - so a dependency that never returns cannot
    defeat the episode wall-clock bound.
  * :class:`PolicyResponse`, the strict typed enum a decision-bearing callback
    must speak; anything else is fail-closed (P0-7: an unknown callback is
    never converted to accept).

Uses ``time.monotonic`` (never wall-clock ``time.time``) for the deadline, so
a clock step cannot extend or collapse an episode's budget.  Nothing here
reaches the RAN, opens a socket, or touches hardware.
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# FSM state + event enums (P0-7)
# ---------------------------------------------------------------------------

class FSMState(Enum):
    """The coordination FSM states.  ``value`` matches the legacy string names
    (``"S0"`` ... ``"S6"`` / ``"S_TECHNICAL_FAILSAFE"``) so the existing GUI /
    ``on_state_change`` consumers and tests keep seeing the same labels."""
    S0 = "S0"      # idle / episode boundary
    S1 = "S1"      # conflict screening
    S2 = "S2"      # feasibility analysis
    S3 = "S3"      # trial execution (first write)
    S4 = "S4"      # validation window
    S5 = "S5"      # negotiation
    S6 = "S6"      # resolution
    TECHNICAL_FAILSAFE = "S_TECHNICAL_FAILSAFE"

    @classmethod
    def from_name(cls, name: str) -> Optional["FSMState"]:
        """The state whose ``value`` equals ``name`` (or None - never invent
        one, P0-7)."""
        for st in cls:
            if st.value == name:
                return st
        return None


class FSMEvent(Enum):
    """The events that drive legal transitions.  Two universal events model
    the episode boundary and the fail-closed sink:

      * RESET  -> S0 from ANY state (return to idle at episode end);
      * FAILSAFE -> TECHNICAL_FAILSAFE from ANY state (a broken invariant).

    Every other event is legal only from the specific state(s) named in
    :data:`TRANSITIONS`."""
    BEGIN = "begin"          # S0 -> S1
    SCREEN = "screen"        # S1 -> S2
    TRIAL = "trial"          # S2 -> S3
    VALIDATE = "validate"    # S3 -> S4
    NEGOTIATE = "negotiate"  # S2 -> S5, S3 -> S5, S4 -> S5
    REENTER = "reenter"      # S5 -> S2
    RESOLVE = "resolve"      # S4 -> S6, S5 -> S6
    RESET = "reset"          # * -> S0
    FAILSAFE = "failsafe"    # * -> TECHNICAL_FAILSAFE


# Total legal (state, event) -> next_state table (P0-7).  The two universal
# events (RESET / FAILSAFE) are handled in legal_next below rather than
# enumerated for every state, so this table lists only the state-specific
# edges.  A (state, event) pair absent from BOTH is an ILLEGAL transition.
TRANSITIONS: Dict[Tuple[FSMState, FSMEvent], FSMState] = {
    (FSMState.S0, FSMEvent.BEGIN): FSMState.S1,
    (FSMState.S1, FSMEvent.SCREEN): FSMState.S2,
    (FSMState.S2, FSMEvent.TRIAL): FSMState.S3,
    (FSMState.S3, FSMEvent.VALIDATE): FSMState.S4,
    (FSMState.S2, FSMEvent.NEGOTIATE): FSMState.S5,
    (FSMState.S3, FSMEvent.NEGOTIATE): FSMState.S5,
    (FSMState.S4, FSMEvent.NEGOTIATE): FSMState.S5,
    (FSMState.S5, FSMEvent.REENTER): FSMState.S2,
    (FSMState.S4, FSMEvent.RESOLVE): FSMState.S6,
    (FSMState.S5, FSMEvent.RESOLVE): FSMState.S6,
}


class IllegalTransition(Exception):
    """An illegal (state, event) transition, or an unknown target state.

    The coordinator catches this and finalises TechnicalFailsafe (never
    silently creating a state, P0-7)."""


class UnknownSystemEvent(Exception):
    """An event token that is not a known :class:`FSMEvent` (an unknown system
    callback / event).  Fail-closed to TechnicalFailsafe (section 3.1)."""


def legal_next(state: FSMState, event: FSMEvent) -> FSMState:
    """The next state for ``(state, event)`` per the total table, honouring the
    universal FAILSAFE event.  Raises :class:`IllegalTransition` for any pair
    not in the table (so an illegal transition can never fabricate a state).

    TechnicalFailsafe is a PERSISTENT SINK (P0-7): NO event leaves it - not even
    RESET.  The only way back to S0 is an out-of-band verified operator recovery
    (``clear_safety_latch``), which is deliberately NOT expressible in this
    table.

    Wrong-typed inputs return a TYPED failure, never an AttributeError
    (coordinator review D2): a non-:class:`FSMState` ``state`` is an
    :class:`IllegalTransition`; a non-:class:`FSMEvent` ``event`` is an
    :class:`UnknownSystemEvent`."""
    if not isinstance(state, FSMState):
        raise IllegalTransition(f"not a valid FSM state: {state!r}")
    if not isinstance(event, FSMEvent):
        raise UnknownSystemEvent(f"not a valid FSM event: {event!r}")
    if state is FSMState.TECHNICAL_FAILSAFE:
        if event is FSMEvent.FAILSAFE:
            return FSMState.TECHNICAL_FAILSAFE      # idempotent re-entry
        raise IllegalTransition(
            f"TechnicalFailsafe is a sink; {event.value!r} cannot leave it")
    if event is FSMEvent.RESET:
        return FSMState.S0
    if event is FSMEvent.FAILSAFE:
        return FSMState.TECHNICAL_FAILSAFE
    nxt = TRANSITIONS.get((state, event))
    if nxt is None:
        raise IllegalTransition(
            f"illegal transition: no {event.value!r} edge from {state.value!r}")
    return nxt


def event_for_target(state: FSMState, target: FSMState) -> FSMEvent:
    """Resolve the single event that legally drives ``state -> target``.

    Backs the legacy ``_transition(target_state_name)`` call sites: the code
    still names the TARGET state, and this maps that onto the (unique) event so
    the transition is validated against the (state, event) table.  A
    self-loop to S0 and any move to the failsafe sink use the universal events.
    Raises :class:`IllegalTransition` if no legal event connects them, or if
    either argument is not a valid :class:`FSMState` (never AttributeError)."""
    if not isinstance(state, FSMState) or not isinstance(target, FSMState):
        raise IllegalTransition(
            f"invalid state(s) for transition: {state!r} -> {target!r}")
    if target is FSMState.S0:
        return FSMEvent.RESET
    if target is FSMState.TECHNICAL_FAILSAFE:
        return FSMEvent.FAILSAFE
    for (st, ev), nxt in TRANSITIONS.items():
        if st is state and nxt is target:
            return ev
    raise IllegalTransition(
        f"illegal transition: {state.value!r} -> {target.value!r} "
        f"(no legal event connects them)")


# ---------------------------------------------------------------------------
# Absolute episode deadline + bounded external calls (P0-7)
# ---------------------------------------------------------------------------

class EpisodeDeadline:
    """An ABSOLUTE monotonic deadline fixed once at episode start (P0-7 / 3.5).

    Every bounded external call is given ``min(op_timeout, remaining())`` so no
    single call - and no sequence of calls - can run past the episode budget.
    Monotonic clock only, so a wall-clock step cannot move the deadline."""

    def __init__(self, budget_s: float, *, now: Optional[float] = None):
        self.budget_s = float(budget_s)
        self.start = time.monotonic() if now is None else float(now)
        self.deadline = self.start + self.budget_s

    def remaining(self) -> float:
        """Seconds left before the absolute deadline (>= 0)."""
        return max(0.0, self.deadline - time.monotonic())

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def op_timeout(self, op_timeout_s: Optional[float]) -> float:
        """The timeout to hand ONE external call: never more than the remaining
        episode time (P0-7)."""
        rem = self.remaining()
        if op_timeout_s is None:
            return rem
        return max(0.0, min(float(op_timeout_s), rem))


class OperationTimeout(Exception):
    """One external call (model / policy / alternative generator) did not
    return within its bounded timeout.  A pre-actuation timeout ends the
    episode PendingNotAdmitted; a post-actuation timeout rolls back first
    (P0-7)."""


def call_with_timeout(fn: Callable[[], Any], timeout_s: float,
                      *, label: str = "external call") -> Any:
    """Run ``fn()`` in a daemon worker thread and return its result, or raise
    :class:`OperationTimeout` if it does not finish within ``timeout_s``.

    The key property (P0-7): a dependency that NEVER returns cannot defeat the
    wall-clock bound.  When ``fn`` hangs, the worker thread is abandoned (it is
    a daemon, so it cannot keep the process alive) and control returns to the
    caller at the timeout - the episode still terminates by its deadline.  A
    non-positive timeout raises immediately (the deadline is already spent)."""
    if timeout_s <= 0:
        raise OperationTimeout(f"{label}: no time budget remaining")
    box: Dict[str, Any] = {}
    done = threading.Event()

    def _runner():
        try:
            box["value"] = fn()
        except BaseException as e:     # propagate the callee's failure verbatim
            box["error"] = e
        finally:
            done.set()

    worker = threading.Thread(target=_runner, name="bounded-call", daemon=True)
    worker.start()
    if not done.wait(timeout_s):
        raise OperationTimeout(
            f"{label}: did not return within {timeout_s:.3f}s (abandoned)")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------
# Strict policy-response enum (P0-7)
# ---------------------------------------------------------------------------

class PolicyResponse(Enum):
    """The strict typed vocabulary a decision-bearing (negotiation) callback
    must speak: EXACTLY ``"accept"`` or ``"reject"`` (or a member).

    An UNKNOWN response is NOT silently downgraded to REJECT.  The coordinator
    gates on :meth:`is_recognized` FIRST: an unrecognised value (an unknown
    string, ``None``, a wrong type) is a broken system contract that raises
    ``UnknownSystemEvent`` and finalises TechnicalFailsafe (P0-7 / coordinator
    review).  :meth:`parse` is only ever applied to an ALREADY-RECOGNISED value
    (it maps ``accept`` -> ACCEPT and everything else -> REJECT), so a recognised
    ``"reject"`` remains a safe, ordinary PendingNotAdmitted negotiation
    termination."""
    ACCEPT = "accept"
    REJECT = "reject"

    @classmethod
    def parse(cls, raw: Any) -> "PolicyResponse":
        """Map an ALREADY-RECOGNISED response (see :meth:`is_recognized`) onto
        the enum: ``accept`` -> ACCEPT, ``reject`` -> REJECT.  NOTE: an unknown
        value is handled by the caller BEFORE calling this (it raises
        UnknownSystemEvent), so this must not be relied on to fail an unknown
        value closed - it is not a substitute for the is_recognized gate."""
        if isinstance(raw, cls):
            return raw
        if raw == "accept":
            return cls.ACCEPT
        return cls.REJECT

    @classmethod
    def is_recognized(cls, raw: Any) -> bool:
        """True iff ``raw`` is a value the strict enum actually recognises
        (``accept``/``reject`` or a member).  The coordinator treats a value
        that is NOT recognised as an UnknownSystemEvent (-> TechnicalFailsafe),
        NOT as a REJECT."""
        return isinstance(raw, cls) or raw in ("accept", "reject")
