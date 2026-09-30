"""How a value got onto the screen, as its own visible axis.

Task section 9.7 names five classes of data the Cockpit must let an operator
tell apart at a glance: ``LIVE``, ``REPLAY``, ``DERIVED``, ``UNKNOWN`` and
``UNSUPPORTED``.  They are *not* health states, which is why they are a
separate vocabulary from :mod:`gui.operator.status` rather than five more
entries in it: a replayed reading can be perfectly healthy and a live one can
be an error, and a console that spent one badge on both would make the two
questions -- "is this fine?" and "where did this come from?" -- unanswerable
from the same screen.

Three rules the module enforces rather than documents:

* **Never colour-only.**  Every class carries a glyph, a shape name and a
  hatch, so the distinction survives a greyscale screenshot, a projector and a
  colour-vision difference.  :func:`badge_text` is the one-line rendering the
  tests assert on.
* **Three of the five must state a reason.**  ``DERIVED`` without its
  derivation, ``UNKNOWN`` without why it is unknown and ``UNSUPPORTED``
  without what is missing are the three ways a console quietly overstates what
  it knows (task sections 9.11 and 9.13).  :func:`classify` refuses to produce
  them bare: it substitutes :data:`REASON_NOT_STATED` so the omission is
  visible instead of invisible.
* **LIVE is never a default.**  :func:`classify` returns ``LIVE`` only when it
  is told the session is live *and* the value was observed rather than
  derived.  Everything unmapped falls to ``UNKNOWN``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Final, Mapping, Optional, Tuple

# --------------------------------------------------------------------------- #
# The five classes
# --------------------------------------------------------------------------- #

LIVE: Final[str] = "LIVE"
REPLAY: Final[str] = "REPLAY"
DERIVED: Final[str] = "DERIVED"
UNKNOWN: Final[str] = "UNKNOWN"
UNSUPPORTED: Final[str] = "UNSUPPORTED"

#: In the order an operator reads them: what was seen, what was replayed, what
#: was computed, what is missing, what this deployment cannot do at all.
DATA_CLASS_ORDER: Final[Tuple[str, ...]] = (
    LIVE, REPLAY, DERIVED, UNKNOWN, UNSUPPORTED,
)

#: The three that are meaningless without a reason.
REASON_REQUIRED: Final[frozenset] = frozenset({DERIVED, UNKNOWN, UNSUPPORTED})

#: Substituted when a reason-requiring class arrives bare.  Deliberately ugly:
#: it is a defect in the caller, and it should read as one on screen.
REASON_NOT_STATED: Final[str] = "REASON_NOT_STATED"


@dataclass(frozen=True)
class DataClassSpec:
    """Presentation contract for one data class.  No colour in here."""

    id: str
    label: str
    glyph: str
    shape: str
    hatch: str
    #: What the class means in one clause, for a legend and for a tooltip.
    meaning: str


DATA_CLASS_SPECS: Final[Dict[str, DataClassSpec]] = {
    LIVE: DataClassSpec(
        LIVE, "Live", "◆", "FILLED_DIAMOND", "",
        "observed now, from the bound deployment"),
    REPLAY: DataClassSpec(
        REPLAY, "Replay", "▷", "OUTLINE_TRIANGLE", "||",
        "re-read from a recorded source; nothing was actuated"),
    DERIVED: DataClassSpec(
        DERIVED, "Derived", "ƒ", "FUNCTION", "//",
        "computed from other values, not observed"),
    UNKNOWN: DataClassSpec(
        UNKNOWN, "Unknown", "?", "QUESTION", "xx",
        "not observed and not derivable here"),
    UNSUPPORTED: DataClassSpec(
        UNSUPPORTED, "Unsupported", "⊘", "SLASHED_CIRCLE", "--",
        "this deployment has no path that could produce it"),
}


@dataclass(frozen=True)
class DataClassBadge:
    """One resolved class, ready to render and ready to assert on."""

    data_class: str
    label: str
    glyph: str
    shape: str
    hatch: str
    meaning: str
    reason: Optional[str] = None

    @property
    def is_live(self) -> bool:
        return self.data_class == LIVE

    @property
    def states_a_reason(self) -> bool:
        return bool(self.reason) and self.reason != REASON_NOT_STATED

    def as_text(self) -> str:
        """``glyph LABEL - reason``.  The reason is never dropped."""
        text = f"{self.glyph} {self.label}"
        return f"{text} - {self.reason}" if self.reason else text


def resolve(data_class: str, *, reason: Optional[str] = None) -> DataClassBadge:
    """Resolve one class id, filling in a stated reason where one is required.

    An unrecognised id becomes ``UNKNOWN`` naming the id it could not read --
    a console that silently mapped an unknown class onto ``LIVE`` would be the
    exact overstatement task section 9.13 forbids.
    """
    spec = DATA_CLASS_SPECS.get(data_class)
    if spec is None:
        spec = DATA_CLASS_SPECS[UNKNOWN]
        reason = reason or f"UNMAPPED_DATA_CLASS:{data_class}"
    if spec.id in REASON_REQUIRED and not reason:
        reason = REASON_NOT_STATED
    return DataClassBadge(
        data_class=spec.id, label=spec.label, glyph=spec.glyph,
        shape=spec.shape, hatch=spec.hatch, meaning=spec.meaning,
        reason=reason)


def badge_text(data_class: str, *, reason: Optional[str] = None) -> str:
    """One-line rendering, for logs, tests and low-fidelity screenshots."""
    return resolve(data_class, reason=reason).as_text()


#: Session mode -> the class an *observed* value in that mode belongs to.
#:
#: ``MOCK``, ``SYNTHETIC`` and ``EMULATED`` all land on ``REPLAY``, and that
#: needs saying out loud.  Task section 9.7 fixes the vocabulary at five
#: classes, and none of the five is "modelled deployment"; of the five,
#: ``REPLAY`` is the only one that carries the property that actually matters
#: here -- *nothing was actuated on a radio in this run*.  Every one of them
#: therefore also carries its own reason below, and the header prints the mode
#: verbatim beside the badge, so a screenshot says ``MOCK`` and not merely
#: "Replay".  What none of them may do is read as ``LIVE``: the Kernel
#: decisions in a mock run are real, but the readings are not from a radio, and
#: calling them Live is the overstatement task section 9.13 exists to prevent.
MODE_DATA_CLASS: Final[Mapping[str, str]] = {
    "LIVE": LIVE,
    "REPLAY": REPLAY,
    "MOCK": REPLAY,
    "SYNTHETIC": REPLAY,
    "EMULATED": REPLAY,
    "DISCONNECTED": UNKNOWN,
}

MODE_DATA_CLASS_REASON: Final[Mapping[str, str]] = {
    "MOCK": "hardware-free mock adapter; no O-RAN endpoint was contacted",
    "SYNTHETIC": "offline model, not a recorded or observed deployment",
    "EMULATED": "offline model, not a recorded or observed deployment",
    "DISCONNECTED": "no source is attached to this console",
}


def classify(*, mode: str, value: Any = None, supported: bool = True,
             derived: bool = False, reason: Optional[str] = None,
             observed: bool = True) -> DataClassBadge:
    """Classify one displayed value.  The order of the tests is the rule.

    ``UNSUPPORTED`` outranks everything: a deployment that cannot produce a
    value at all must say so rather than report it missing, because "we did
    not see it" and "there is no way to see it" call for different operator
    actions.  ``DERIVED`` outranks the mode, because a number computed from
    live inputs is still not an observation.  Only then does the session mode
    decide, and only an observed value in a live session is ``LIVE``.
    """
    if not supported:
        return resolve(UNSUPPORTED, reason=reason)
    if derived:
        return resolve(DERIVED, reason=reason)
    if value is None or not observed:
        return resolve(UNKNOWN, reason=reason)
    mapped = MODE_DATA_CLASS.get(str(mode))
    if mapped is None:
        return resolve(UNKNOWN,
                       reason=reason or f"UNMAPPED_SESSION_MODE:{mode}")
    return resolve(mapped,
                   reason=reason or MODE_DATA_CLASS_REASON.get(str(mode)))


def legend() -> Tuple[str, ...]:
    """The five classes as an on-screen legend, in reading order."""
    return tuple(
        f"{spec.glyph} {spec.label} - {spec.meaning}"
        for spec in (DATA_CLASS_SPECS[name] for name in DATA_CLASS_ORDER))


__all__ = [
    "DATA_CLASS_ORDER", "DATA_CLASS_SPECS", "DERIVED", "DataClassBadge",
    "DataClassSpec", "LIVE", "MODE_DATA_CLASS", "MODE_DATA_CLASS_REASON",
    "REASON_NOT_STATED", "REASON_REQUIRED", "REPLAY", "UNKNOWN",
    "UNSUPPORTED", "badge_text", "classify", "legend", "resolve",
]
