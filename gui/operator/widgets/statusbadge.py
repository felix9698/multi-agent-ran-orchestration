"""The status indicator, and the one place the dual-encoding rule is applied.

``status-vocabulary.1.0.0.json`` forbids colour as the discriminator.  Every
indicator in this console therefore renders **glyph + label + colour**, with the
glyph first so that a projector, a greyscale print and a downscaled screenshot
all keep the distinction that colour alone would lose.

:func:`badge_text` is the whole rule as a pure function.  The widget below is a
thin rendering of it, and every other widget in the console calls the same
function rather than formatting a status itself.
"""

from __future__ import annotations

from typing import Optional

from .. import status as st
from .. import tokens


def badge_text(status: str, *, label: Optional[str] = None,
               reason: Optional[str] = None,
               gap_id: Optional[str] = None) -> str:
    """Glyph-led one-line rendering of a status.

    ``label`` overrides the vocabulary's display name for cases where the
    element already names itself ("A1 policy: Enforced"), but the glyph is never
    optional.
    """
    resolved = st.resolve(status, reason=reason, gap_id=gap_id)
    text = f"{resolved.glyph} {label if label is not None else resolved.label}"
    if resolved.reason:
        text = f"{text} ({resolved.reason})"
    if resolved.gap_id:
        text = f"{text} [{resolved.gap_id}]"
    return text


def badge_tooltip(status: str, *, source: Optional[str] = None,
                  observed_at: Optional[str] = None,
                  age_ms: Optional[float] = None,
                  freshness: Optional[str] = None,
                  reason: Optional[str] = None) -> str:
    """The detail an operator needs to trust or distrust a status.

    Section 2 of the task asks for the *source* of a status and its freshness,
    not just the status.  A green dot whose provenance is unknown is not
    evidence, so the two travel together everywhere in this console.
    """
    lines = [badge_text(status, reason=reason)]
    lines.append(f"source: {source or 'unknown'}")
    lines.append(f"observed: {st.format_utc(observed_at)}")
    lines.append(f"age: {st.format_age(age_ms)}")
    lines.append(f"freshness: {freshness or st.FRESHNESS_UNKNOWN}")
    return "\n".join(lines)


class StatusBadge:
    """A single label rendering :func:`badge_text` in the status colour."""

    def __init__(self, *, theme: str = tokens.DEFAULT_THEME,
                 scale: float = tokens.SCALE_NORMAL, bold: bool = False,
                 size_key: str = "small") -> None:
        self.theme = theme
        self.scale = scale
        self.bold = bold
        self.size_key = size_key
        self.widget = None
        self._palette = tokens.theme(theme)

    def build(self, parent, *, status: str = st.UNKNOWN,
              label: Optional[str] = None) -> None:
        import tkinter as tk

        self.widget = tk.Label(
            parent, text=badge_text(status, label=label), anchor="w",
            bg=self._palette["panel_bg"],
            fg=tokens.status_color(status, self.theme),
            font=tokens.font(self.size_key, scale=self.scale, bold=self.bold))

    def update(self, status: str, *, label: Optional[str] = None,
               reason: Optional[str] = None,
               gap_id: Optional[str] = None) -> None:
        if self.widget is None:
            return
        self.widget.configure(
            text=badge_text(status, label=label, reason=reason, gap_id=gap_id),
            fg=tokens.status_color(status, self.theme))


__all__ = ["StatusBadge", "badge_text", "badge_tooltip"]
