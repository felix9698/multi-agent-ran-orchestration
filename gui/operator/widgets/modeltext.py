"""Safe rendering policy for model-authored text.

This is deliberately a plain-text policy, not a reasoning or chain-of-thought
view.  Hidden thinking blocks are removed, remaining model output is bounded,
and the provenance marker is always present.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


MODEL_TEXT_LIMIT = 600
MODEL_TEXT_LABEL = "Model-authored summary"
TRUNCATED_LABEL = "model text, truncated"
SUPPRESSED_LABEL = "private reasoning suppressed"

_THINK_BLOCK = re.compile(
    r"<(?:think(?:ing)?|scratchpad)\b[^>]*>.*?"
    r"</(?:think(?:ing)?|scratchpad)\s*>", re.IGNORECASE | re.DOTALL)
_OPEN_THINK = re.compile(
    r"<(?:think(?:ing)?|scratchpad)\b[^>]*>.*$", re.IGNORECASE | re.DOTALL)
_ANALYSIS_CHANNEL = re.compile(
    r"<\|channel\|>\s*analysis\b.*?(?=<\|channel\|>\s*"
    r"(?:final|commentary)\b|\Z)", re.IGNORECASE | re.DOTALL)
_THOUGHT_LINE = re.compile(r"^\s*Thought\s*:\s*.*(?:\n|$)", re.IGNORECASE | re.MULTILINE)
_REASONING_FIELD = re.compile(
    r"(?is)[\"']?(?:reasoning|reasoning_content|chain[-_ ]of[-_ ]thought)"
    r"[\"']?\s*[:=]\s*(?:\"(?:\\.|[^\"\\])*\"|'.*?'|[^,}\n]+)")


@dataclass(frozen=True)
class SanitizedModelText:
    text: str
    label: str = MODEL_TEXT_LABEL
    truncated: bool = False
    suppressed: bool = False

    @property
    def marker(self) -> str:
        markers = [self.label]
        if self.suppressed:
            markers.append(SUPPRESSED_LABEL)
        if self.truncated:
            markers.append(TRUNCATED_LABEL)
        return " · ".join(markers)


def sanitize_model_text(value: Any, *, limit: int = MODEL_TEXT_LIMIT) -> SanitizedModelText:
    """Return bounded visible model text with private-reasoning fields removed."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 32:
        raise ValueError("limit must be an integer of at least 32")
    raw = "" if value is None else str(value)
    cleaned, count1 = _THINK_BLOCK.subn("[private reasoning suppressed]", raw)
    cleaned, count2 = _OPEN_THINK.subn("[private reasoning suppressed]", cleaned)
    cleaned, count3 = _ANALYSIS_CHANNEL.subn("[private reasoning suppressed]", cleaned)
    cleaned, count4 = _THOUGHT_LINE.subn("[private reasoning suppressed]\n", cleaned)
    cleaned, count5 = _REASONING_FIELD.subn("reasoning_content=[suppressed]", cleaned)
    cleaned = " ".join(cleaned.split())
    truncated = len(cleaned) > limit
    if truncated:
        cleaned = cleaned[: max(0, limit - 1)].rstrip() + "…"
    return SanitizedModelText(
        text=cleaned, truncated=truncated,
        suppressed=bool(count1 or count2 or count3 or count4 or count5))


# Short aliases make the policy convenient in projections and tests.
sanitize = sanitize_model_text
render_model_text = sanitize_model_text


__all__ = [
    "MODEL_TEXT_LABEL", "MODEL_TEXT_LIMIT", "SUPPRESSED_LABEL",
    "TRUNCATED_LABEL", "SanitizedModelText", "render_model_text", "sanitize",
    "sanitize_model_text",
]
