"""Phase B Operator Console - status vocabulary and contract-enum mapping.

FROZEN SEAM.  Design authority: ``docs/phase-b-gui/status-vocabulary.1.0.0.json``.

Pure functions over plain data.  This module must never import ``tkinter``: it is
the layer both the renderer and the headless exporter share, and it is exercised
by hermetic tests without a display.

Two rules govern everything here:

1. **Fail closed.**  An upstream value that is not in a mapping table resolves to
   ``UNKNOWN`` with reason ``UNMAPPED_UPSTREAM_VALUE``.  It never resolves to
   ``OK``.  A GUI that greens an unrecognised state is worse than one that admits
   it does not know.
2. **Never colour alone.**  Every status carries a distinct glyph and a distinct
   shape as well as a colour, so the indicator survives greyscale, projection and
   colour vision deficiency (phaseB_task.md section 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Final, Mapping, Optional, Tuple

# --------------------------------------------------------------------------- #
# The vocabulary
# --------------------------------------------------------------------------- #

OK: Final[str] = "OK"
DEGRADED: Final[str] = "DEGRADED"
STALE: Final[str] = "STALE"
UNKNOWN: Final[str] = "UNKNOWN"
UNAVAILABLE: Final[str] = "UNAVAILABLE"
BLOCKED: Final[str] = "BLOCKED"
ERROR: Final[str] = "ERROR"
UNSUPPORTED: Final[str] = "UNSUPPORTED"
EXTERNAL: Final[str] = "EXTERNAL"
LAB_HARDWARE: Final[str] = "LAB_HARDWARE"
NOT_APPLICABLE: Final[str] = "NOT_APPLICABLE"


@dataclass(frozen=True)
class StatusSpec:
    """Presentation contract for one status id."""

    id: str
    label: str
    severity: int
    glyph: str
    shape: str
    hatch: str


#: The complete vocabulary.  ``severity`` drives sorting and the health roll-up
#: (which reports the WORST, never an average - an average hides one dead element
#: among healthy ones).
STATUS_SPECS: Final[Dict[str, StatusSpec]] = {
    OK: StatusSpec(OK, "OK", 0, "●", "FILLED_CIRCLE", ""),
    UNSUPPORTED: StatusSpec(UNSUPPORTED, "Unsupported", 1, "⊘", "SLASHED_CIRCLE", "--"),
    EXTERNAL: StatusSpec(EXTERNAL, "External dependency", 1, "▣", "BOXED_SQUARE", ""),
    LAB_HARDWARE: StatusSpec(LAB_HARDWARE, "Lab hardware", 1, "▤", "LINED_SQUARE", ""),
    NOT_APPLICABLE: StatusSpec(NOT_APPLICABLE, "N/A", 1, "–", "DASH", ""),
    DEGRADED: StatusSpec(DEGRADED, "Degraded", 2, "◐", "HALF_CIRCLE", "//"),
    STALE: StatusSpec(STALE, "Stale", 3, "◔", "QUARTER_CIRCLE", ".."),
    UNKNOWN: StatusSpec(UNKNOWN, "Unknown", 4, "?", "QUESTION", "xx"),
    UNAVAILABLE: StatusSpec(UNAVAILABLE, "Unavailable", 5, "○", "HOLLOW_CIRCLE", ""),
    BLOCKED: StatusSpec(BLOCKED, "Blocked", 6, "▲", "TRIANGLE", "\\\\"),
    ERROR: StatusSpec(ERROR, "Error", 7, "✕", "CROSS", "++"),
}

#: Reason emitted whenever an upstream value is absent from a mapping table.
UNMAPPED_REASON: Final[str] = "UNMAPPED_UPSTREAM_VALUE"

#: Shown wherever a real measurement has not arrived.  Inherited verbatim from
#: gui/dashboard.py:91 - a default, a prior or a zero must never take this slot.
PRE_MEASUREMENT: Final[str] = "—"


# --------------------------------------------------------------------------- #
# Contract enum mappings
# --------------------------------------------------------------------------- #
# Authority: AIC_UECellSteering_1.0.0.status.schema.json,
# aic.policy-evidence.1.0.0.schema.json, oran/rapp/ports.py,
# coordinator/episode_types.py.

ENFORCE_STATUS: Final[Dict[str, str]] = {
    "ENFORCED": OK,
    "NOT_ENFORCED": DEGRADED,
}

ENFORCE_REASON: Final[Dict[str, str]] = {
    "SCOPE_NOT_APPLICABLE": NOT_APPLICABLE,
    "STATEMENT_NOT_APPLICABLE": NOT_APPLICABLE,
    "OTHER_REASON": UNKNOWN,
}

POLICY_STATE: Final[Dict[str, str]] = {
    "ACTIVE": OK,
    "NOT_ENFORCED": DEGRADED,
    "EXPIRED": NOT_APPLICABLE,
    "CANCELLED": NOT_APPLICABLE,
    "SUPERSEDED": NOT_APPLICABLE,
    "RECOVERY_PENDING": BLOCKED,
    "ERROR": ERROR,
}

EPISODE_STATE: Final[Dict[str, str]] = {
    "SCHEDULED": UNKNOWN,
    "COMPUTING": UNKNOWN,
    "NO_ACTION": OK,
    "ABORTED_NO_WRITE": DEGRADED,
    "APPLYING": UNKNOWN,
    "APPLIED_UNVERIFIED": DEGRADED,
    "APPLIED_VERIFIED": OK,
    "READBACK_MISMATCH": ERROR,
    "APPLY_FAILED": ERROR,
    "ROLLING_BACK": BLOCKED,
    "ROLLED_BACK_VERIFIED": DEGRADED,
    "ROLLBACK_FAILED": ERROR,
    "ROLLBACK_UNKNOWN": UNKNOWN,
    "RECOVERY_PENDING": BLOCKED,
    "QUARANTINED": ERROR,
}

CONTROL_RESULT: Final[Dict[str, str]] = {
    "PENDING": UNKNOWN,
    "ACK": OK,
    "NACK": ERROR,
    "TIMEOUT": UNAVAILABLE,
    "UNKNOWN": UNKNOWN,
}

READBACK_RESULT: Final[Dict[str, str]] = {
    "VERIFIED": OK,
    "MISMATCH": ERROR,
    "MISSING": UNAVAILABLE,
    "STALE": STALE,
    "NOT_AVAILABLE": UNSUPPORTED,
}

ROLLBACK_STATE: Final[Dict[str, str]] = {
    "NOT_REQUESTED": NOT_APPLICABLE,
    "REQUESTED": UNKNOWN,
    "SENT": UNKNOWN,
    "VERIFIED": OK,
    "FAILED": ERROR,
    "UNKNOWN": UNKNOWN,
}

EVIDENCE_QUALITY: Final[Dict[str, str]] = {
    "OK": OK,
    "SUSPECT": DEGRADED,
    "STALE": STALE,
    "MISSING": UNAVAILABLE,
    "NOT_AVAILABLE": UNSUPPORTED,
    "AMBIGUOUS": DEGRADED,
}

ASSURANCE_DECISION: Final[Dict[str, str]] = {
    "SATISFIED": OK,
    "VIOLATED": ERROR,
    "UNKNOWN": UNKNOWN,
}

MONITOR_VERDICT: Final[Dict[str, str]] = {
    "satisfied": OK,
    "violated": ERROR,
    "unknown": UNKNOWN,
}

GATE_RESULT: Final[Dict[str, str]] = {
    "ADMITTED": OK,
    "REFUSED": BLOCKED,
}

SUBSCRIPTION_RESOURCE: Final[Dict[str, str]] = {
    "PRESENT": OK,
    "DELETED": NOT_APPLICABLE,
    "UNKNOWN": UNKNOWN,
}

#: Every mapping table, addressable by name.  ``map_contract_value`` uses this so
#: a caller cannot accidentally invent a table.
CONTRACT_MAPS: Final[Dict[str, Dict[str, str]]] = {
    "enforceStatus": ENFORCE_STATUS,
    "enforceReason": ENFORCE_REASON,
    "policyState": POLICY_STATE,
    "episodeState": EPISODE_STATE,
    "controlResult": CONTROL_RESULT,
    "readbackResult": READBACK_RESULT,
    "rollbackState": ROLLBACK_STATE,
    "evidenceQuality": EVIDENCE_QUALITY,
    "assuranceDecision": ASSURANCE_DECISION,
    "monitorVerdict": MONITOR_VERDICT,
    "gateResult": GATE_RESULT,
    "subscriptionResource": SUBSCRIPTION_RESOURCE,
}


# --------------------------------------------------------------------------- #
# Readiness ladder
# --------------------------------------------------------------------------- #
# oran/release/lo1/o1_readiness.py READINESS_ORDER.  These are ladder SEGMENTS,
# not statuses: a segment is OK when confirmed, BLOCKED when something earlier is
# unmet, UNKNOWN when the ladder is absent, UNSUPPORTED when the deployment
# declares it offers none.

READINESS_ORDER: Final[Tuple[str, ...]] = (
    "PRECHECK", "TRUST_READY", "SUBSCRIBED", "JOB_ACTIVE", "ASSURANCE_READY",
)


# --------------------------------------------------------------------------- #
# Eq.12 terminal states
# --------------------------------------------------------------------------- #
# The coordinator's TerminalOutcome has FOUR members collapsing onto the paper's
# THREE terminal states.  Copied from gui/dashboard.py:79-84 so the two consoles
# cannot disagree.

TERMINAL_ADMITTED: Final[str] = "Admitted"
TERMINAL_NOT_ADMITTED: Final[str] = "NotAdmitted"
TERMINAL_FAILSAFE: Final[str] = "TechnicalFailsafe"

TERMINAL_STATE_ORDER: Final[Tuple[str, ...]] = (
    TERMINAL_ADMITTED, TERMINAL_NOT_ADMITTED, TERMINAL_FAILSAFE,
)

TERMINAL_OUTCOME_TO_EQ12: Final[Dict[str, str]] = {
    "commit_original": TERMINAL_ADMITTED,
    "commit_revised": TERMINAL_ADMITTED,
    "pending_not_admitted": TERMINAL_NOT_ADMITTED,
    "technical_failsafe": TERMINAL_FAILSAFE,
}


# --------------------------------------------------------------------------- #
# Freshness
# --------------------------------------------------------------------------- #

FRESH: Final[str] = "FRESH"
AGING: Final[str] = "AGING"
FRESHNESS_STALE: Final[str] = "STALE"
FRESHNESS_UNKNOWN: Final[str] = "UNKNOWN"

#: Default budgets in milliseconds.  ``requiredKpiFreshnessMs`` from the policy
#: context overrides O1_PM_SAMPLE when the deployment supplies one.
FRESHNESS_BUDGET_MS: Final[Dict[str, int]] = {
    "A1_POLICY_STATUS": 15_000,
    "DME_EVIDENCE": 120_000,
    "O1_PM_SAMPLE": 120_000,
    "COORDINATOR_FSM": 5_000,
    "LLM_BACKEND_AVAILABILITY": 300_000,
}


# --------------------------------------------------------------------------- #
# Resolution results
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Resolved:
    """A resolved status with everything a renderer needs and nothing more."""

    status: str
    label: str
    glyph: str
    shape: str
    hatch: str
    severity: int
    reason: Optional[str] = None
    gap_id: Optional[str] = None

    @property
    def is_ok(self) -> bool:
        return self.status == OK


def resolve(status: str, *, reason: Optional[str] = None,
            gap_id: Optional[str] = None) -> Resolved:
    """Build a :class:`Resolved` for a GUI status id.

    An unknown id resolves to ``UNKNOWN`` rather than raising: a status lookup
    must never be able to take down a running console.
    """
    spec = STATUS_SPECS.get(status) or STATUS_SPECS[UNKNOWN]
    if status not in STATUS_SPECS and reason is None:
        reason = UNMAPPED_REASON
    return Resolved(
        status=spec.id, label=spec.label, glyph=spec.glyph, shape=spec.shape,
        hatch=spec.hatch, severity=spec.severity, reason=reason, gap_id=gap_id,
    )


def map_contract_value(table: str, value: Any, *,
                       gap_id: Optional[str] = None) -> Resolved:
    """Project an upstream contract enum value onto the GUI vocabulary.

    ``table`` names one of :data:`CONTRACT_MAPS`.  A value absent from the table -
    or an unknown table name - yields ``UNKNOWN`` with reason
    :data:`UNMAPPED_REASON`.  It never yields ``OK``.
    """
    mapping = CONTRACT_MAPS.get(table)
    if mapping is None:
        return resolve(UNKNOWN, reason=f"{UNMAPPED_REASON}: no table {table!r}")
    if value is None:
        return resolve(UNKNOWN, reason="value absent", gap_id=gap_id)
    key = value if isinstance(value, str) else str(value)
    mapped = mapping.get(key)
    if mapped is None:
        return resolve(UNKNOWN, reason=f"{UNMAPPED_REASON}: {table}={key!r}",
                       gap_id=gap_id)
    return resolve(mapped, gap_id=gap_id)


def terminal_state_label(outcome: Any) -> Optional[str]:
    """Map a coordinator terminal outcome onto its Eq.12 state.

    Accepts an enum, its ``.value`` string, or a raw result's
    ``terminal_outcome`` field.  Returns ``None`` for anything unrecognised, so
    the caller renders :data:`PRE_MEASUREMENT` rather than a guessed outcome.
    Behaviourally identical to ``gui.dashboard.terminal_state_label``.
    """
    if outcome is None:
        return None
    key = str(getattr(outcome, "value", outcome)).strip().lower()
    return TERMINAL_OUTCOME_TO_EQ12.get(key)


def freshness(age_ms: Optional[float], budget_ms: Optional[float]) -> str:
    """Classify an observation's age against its budget.

    ``None`` for either argument yields :data:`FRESHNESS_UNKNOWN`.  Freshness is
    never derived from arrival time at the GUI, because arrival time is not
    observation time.
    """
    if age_ms is None or budget_ms is None or budget_ms <= 0:
        return FRESHNESS_UNKNOWN
    if not isinstance(age_ms, (int, float)) or isinstance(age_ms, bool):
        return FRESHNESS_UNKNOWN
    if age_ms != age_ms or age_ms in (float("inf"), float("-inf")):  # NaN / Inf
        return FRESHNESS_UNKNOWN
    if age_ms > budget_ms:
        return FRESHNESS_STALE
    if age_ms > budget_ms * 0.5:
        return AGING
    return FRESH


def worst(statuses) -> str:
    """Roll several statuses up to the worst of them.

    Used by the header health summary.  Deliberately a maximum and not an
    average: an average would hide one failed element among healthy ones.
    """
    worst_id, worst_sev = OK, -1
    seen = False
    for s in statuses:
        seen = True
        spec = STATUS_SPECS.get(s)
        if spec is not None and spec.severity > worst_sev:
            worst_id, worst_sev = spec.id, spec.severity
    return worst_id if seen else UNKNOWN


# --------------------------------------------------------------------------- #
# Unit formatting
# --------------------------------------------------------------------------- #
# gui/dashboard.py formats units with inline f-strings scattered across the file.
# Centralised here so every workspace and every export agree.

UNIT_SPECS: Final[Dict[str, Tuple[str, int]]] = {
    "rsrp": ("dBm", 1),
    "sinr": ("dB", 1),
    "snr": ("dB", 1),
    "throughput": ("Mbps", 2),
    "prb_utilization": ("%", 0),
    "latency_ms": ("ms", 0),
    "confidence": ("", 3),
    "theta_star": ("", 3),
    "duration_s": ("s", 1),
    "count": ("", 0),
}


def _is_real_number(value: Any) -> bool:
    """Finite, numeric, and not a bool.

    Mirrors ``experiments/metrics.py:_is_real_number``.  A ``True`` must never be
    rendered as ``1``.
    """
    if value is None or isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def format_value(value: Any, quantity: str, *,
                 placeholder: str = PRE_MEASUREMENT) -> str:
    """Format a measured value with its unit.

    ``None``, ``NaN``, ``Inf`` and ``bool`` all render as the placeholder.  They
    are never rendered as ``0`` - the distinction between "zero" and "not
    measured" is load bearing in this system.
    """
    if not _is_real_number(value):
        return placeholder
    unit, decimals = UNIT_SPECS.get(quantity, ("", 2))
    text = f"{float(value):.{decimals}f}"
    return f"{text} {unit}".strip() if unit else text


def format_age(age_ms: Optional[float]) -> str:
    """Render an observation age as a compact relative string."""
    if not _is_real_number(age_ms) or age_ms < 0:
        return PRE_MEASUREMENT
    seconds = float(age_ms) / 1000.0
    if seconds < 1:
        return "<1s ago"
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


def format_utc(iso: Optional[str]) -> str:
    """Render a UTC timestamp for display.

    Times are always UTC.  Captures, evidence and PM windows are all UTC, and a
    mixed local/UTC display would silently misalign the timeline.
    """
    if not iso:
        return PRE_MEASUREMENT
    text = str(iso)
    if "." in text and text.endswith("Z"):
        text = text.split(".", 1)[0] + "Z"
    return text


def describe(resolved: Resolved, *, include_reason: bool = True) -> str:
    """One-line text rendering: glyph, label and reason.

    The glyph leads so a low-fidelity screenshot still carries the distinction
    that colour alone would lose.
    """
    text = f"{resolved.glyph} {resolved.label}"
    if include_reason and resolved.reason:
        text = f"{text} ({resolved.reason})"
    if resolved.gap_id:
        text = f"{text} [{resolved.gap_id}]"
    return text


def health_summary(statuses: Mapping[str, str]) -> Dict[str, Any]:
    """Header health roll-up: the worst status plus a count per status."""
    counts: Dict[str, int] = {}
    for value in statuses.values():
        counts[value] = counts.get(value, 0) + 1
    return {"worst": worst(statuses.values()), "counts": counts,
            "total": len(statuses)}
