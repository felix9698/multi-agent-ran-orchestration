"""Phase B Operator Console - style tokens.

FROZEN SEAM.  Design authority: ``docs/phase-b-gui/DESIGN.md`` section 2 and
``docs/phase-b-gui/status-vocabulary.1.0.0.json``.

Pure data only.  This module must never import ``tkinter``, ``matplotlib`` or any
other rendering library - it is imported by hermetic tests that run without a
display, and by the export pipeline which runs headless.

Two deliberate inheritances keep the console consistent with what already exists:

* the chart palette is the Okabe-Ito set already used by ``experiments/figures.py``,
  so an on-screen series and the camera-ready figure drawn from the same data are
  the same colour;
* the FSM state colours and the Eq.12 terminal colours are copied from
  ``gui/dashboard.py`` so the research console and the operator console can never
  disagree about what S5 or TechnicalFailsafe looks like.
"""

from __future__ import annotations

from typing import Dict, Final, Tuple

# --------------------------------------------------------------------------- #
# Themes
# --------------------------------------------------------------------------- #
# "dark" is the lab/desk theme and inherits gui/dashboard.py's VS Code palette.
# "light" is the presentation theme: Demo View defaults to it because a dark
# surface washes out on a projector and loses the contrast phaseB_task.md section 5
# requires for screen sharing and screenshots.

_DARK: Final[Dict[str, str]] = {
    "bg": "#1e1e1e",
    "panel_bg": "#252526",
    "panel_alt_bg": "#2d2d30",
    "fg": "#ffffff",
    "fg_muted": "#8a8a8a",
    "fg_inverse": "#1e1e1e",
    "accent": "#007acc",
    "accent_fg": "#ffffff",
    "border": "#3c3c3c",
    "grid": "#555555",
    "selection": "#094771",
}

_LIGHT: Final[Dict[str, str]] = {
    "bg": "#ffffff",
    "panel_bg": "#f4f4f5",
    "panel_alt_bg": "#e8e8ea",
    "fg": "#111111",
    "fg_muted": "#5c5c5c",
    "fg_inverse": "#ffffff",
    "accent": "#005a9e",
    "accent_fg": "#ffffff",
    "border": "#c8c8cc",
    "grid": "#d0d0d4",
    "selection": "#cce4f7",
}

THEMES: Final[Dict[str, Dict[str, str]]] = {"dark": _DARK, "light": _LIGHT}

DEFAULT_THEME: Final[str] = "dark"
#: Demo View defaults to the high-contrast theme (projector legibility, section 5).
DEMO_THEME: Final[str] = "light"


# --------------------------------------------------------------------------- #
# Status colours
# --------------------------------------------------------------------------- #
# Keyed by the status ids in status-vocabulary.1.0.0.json.  Colour is NEVER the
# only discriminator - gui/operator/status.py pairs each of these with a distinct
# glyph and a distinct shape.  These hues are chosen to remain distinguishable
# under the common forms of colour vision deficiency.

STATUS_COLORS: Final[Dict[str, str]] = {
    "OK": "#2e7d32",
    "DEGRADED": "#ef6c00",
    "STALE": "#8d6e63",
    "UNKNOWN": "#616161",
    "UNAVAILABLE": "#455a64",
    "BLOCKED": "#6a1b9a",
    "ERROR": "#c62828",
    "UNSUPPORTED": "#37474f",
    "EXTERNAL": "#1565c0",
    "LAB_HARDWARE": "#00695c",
    "NOT_APPLICABLE": "#78909c",
}

#: Light-theme variants, lightened for contrast against a white surface.
STATUS_COLORS_LIGHT: Final[Dict[str, str]] = {
    "OK": "#1b5e20",
    "DEGRADED": "#e65100",
    "STALE": "#5d4037",
    "UNKNOWN": "#424242",
    "UNAVAILABLE": "#37474f",
    "BLOCKED": "#4a148c",
    "ERROR": "#b71c1c",
    "UNSUPPORTED": "#263238",
    "EXTERNAL": "#0d47a1",
    "LAB_HARDWARE": "#004d40",
    "NOT_APPLICABLE": "#546e7a",
}


# --------------------------------------------------------------------------- #
# FSM and terminal-state colours - inherited from gui/dashboard.py
# --------------------------------------------------------------------------- #
# Copied verbatim from gui/dashboard.py:495-504 and :72-76.  If these ever
# diverge, an operator moving between the two consoles would read the same
# episode two different ways.

FSM_STATE_COLORS: Final[Dict[str, str]] = {
    "S0": "#4caf50",
    "S1": "#2196f3",
    "S2": "#9c27b0",
    "S3": "#ff9800",
    "S4": "#00bcd4",
    "S5": "#f44336",
    "S6": "#8bc34a",
    "S_TECHNICAL_FAILSAFE": "#b71c1c",
}

TERMINAL_STATE_COLORS: Final[Dict[str, str]] = {
    "Admitted": "#4caf50",
    "NotAdmitted": "#ff9800",
    "TechnicalFailsafe": "#f44336",
}


# --------------------------------------------------------------------------- #
# Chart palette - Okabe-Ito, matching experiments/figures.py:52-53
# --------------------------------------------------------------------------- #

CHART_PALETTE: Final[Tuple[str, ...]] = (
    "#0072B2", "#D55E00", "#009E73", "#CC79A7",
    "#E69F00", "#56B4E9", "#F0E442", "#000000",
)

#: Cycled alongside CHART_PALETTE so a series is identifiable without colour.
CHART_LINESTYLES: Final[Tuple[str, ...]] = ("-", "--", "-.", ":")
CHART_MARKERS: Final[Tuple[str, ...]] = ("o", "s", "^", "D", "v", "P", "X", "*")
#: Applied to categorical fills so bar and area figures survive greyscale print.
CHART_HATCHES: Final[Tuple[str, ...]] = ("", "//", "..", "xx", "\\\\", "++", "--", "oo")

#: Phase shading, matching experiments/figures.py:54-59.
PHASE_FILL: Final[Dict[str, str]] = {
    "Nominal": "#E8F5E9",
    "Degradation": "#FFF8E1",
    "Impairment": "#FFEBEE",
    "Recovery": "#E3F2FD",
    "Congestion": "#FFF8E1",
}

#: Annotation colours by timeline event kind, for chart vertical markers.
ANNOTATION_COLORS: Final[Dict[str, str]] = {
    "INTENT_SUBMITTED": "#0072B2",
    "DECISION_COMPLETE": "#009E73",
    "POLICY_APPLIED": "#CC79A7",
    "EVIDENCE_RECEIVED": "#56B4E9",
    "NEGOTIATION_ROUND": "#E69F00",
    "ROLLBACK": "#D55E00",
    "SESSION_ENDED": "#000000",
}


# --------------------------------------------------------------------------- #
# Typography and spacing
# --------------------------------------------------------------------------- #
# gui/dashboard.py uses Helvetica for chrome and Consolas for logs; both are kept
# so the two consoles look like one product.  DejaVu is the reliable fallback on
# the target Ubuntu machine.

FONT_FAMILY: Final[str] = "Helvetica"
FONT_FAMILY_MONO: Final[str] = "Consolas"
FONT_FAMILY_FALLBACK: Final[str] = "DejaVu Sans"
FONT_FAMILY_MONO_FALLBACK: Final[str] = "DejaVu Sans Mono"

#: Base point sizes at 1920x1080.  Multiply by the active scale.
FONT_SIZES: Final[Dict[str, int]] = {
    "micro": 8,
    "small": 9,
    "body": 10,
    "label": 11,
    "subhead": 13,
    "head": 16,
    "hero": 28,
}

SCALE_NORMAL: Final[float] = 1.0
#: Demo View scale.  Chosen so 'body' reaches 18pt, which stays legible from the
#: back of a conference room and in a downscaled screenshot.
SCALE_DEMO: Final[float] = 1.8

SPACING: Final[Dict[str, int]] = {
    "hair": 2,
    "tight": 4,
    "base": 8,
    "loose": 12,
    "section": 16,
}

#: Minimum window the layout is designed against (phaseB_task.md section 1).
MIN_WINDOW: Final[Tuple[int, int]] = (1600, 900)
TARGET_WINDOW: Final[Tuple[int, int]] = (1920, 1080)


# --------------------------------------------------------------------------- #
# Refresh budgets
# --------------------------------------------------------------------------- #
# The mitigation for the matplotlib real-time redraw risk recorded in DESIGN.md
# section 12.  Under pressure the refresh RATE degrades; data never does.

BUS_DRAIN_INTERVAL_MS: Final[int] = 100
CHART_REFRESH_MAX_HZ: Final[float] = 4.0
CHART_MAX_POINTS: Final[int] = 1800
#: Above this many points a series is decimated for DISPLAY only.  The decimation
#: is recorded in the figure metadata 'processing' block; an export always draws
#: from the full data.
CHART_DECIMATION_THRESHOLD: Final[int] = 2000
TABLE_MAX_ROWS: Final[int] = 5000
TIMELINE_MAX_EVENTS: Final[int] = 20000


# --------------------------------------------------------------------------- #
# Figure export
# --------------------------------------------------------------------------- #
# Aligned with experiments/figures.py:62-63 so a GUI export and a paper-pipeline
# export are interchangeable.  SVG is added to the existing png+pdf pair.

FIGURE_DPI: Final[int] = 300
FIGURE_FORMATS: Final[Tuple[str, ...]] = ("pdf", "svg", "png")
FIGURE_SINGLE_COLUMN_IN: Final[float] = 3.5
FIGURE_DOUBLE_COLUMN_IN: Final[float] = 7.16
#: Type 42 (TrueType) embedding, matching experiments/figures.py:39-40.
FIGURE_FONTTYPE: Final[int] = 42
#: Diagonal watermark applied in-figure (not as an overlay) for every non-LIVE
#: mode, so a screenshot cannot lose it.
FIGURE_WATERMARK_MODES: Final[Tuple[str, ...]] = ("REPLAY", "SYNTHETIC", "EMULATED")
FIGURE_WATERMARK_ALPHA: Final[float] = 0.10


def theme(name: str = DEFAULT_THEME) -> Dict[str, str]:
    """Return the colour map for ``name``, falling back to the default theme.

    Fail-soft on purpose: an unknown theme name yields a usable console rather
    than a crash, because a cosmetic setting must never take the operator's
    window away mid-experiment.
    """
    return dict(THEMES.get(name, THEMES[DEFAULT_THEME]))


def status_color(status: str, theme_name: str = DEFAULT_THEME) -> str:
    """Colour for a GUI status id.  Unknown ids fall back to the UNKNOWN hue."""
    table = STATUS_COLORS_LIGHT if theme_name == "light" else STATUS_COLORS
    return table.get(status, table["UNKNOWN"])


def font(size_key: str = "body", *, scale: float = SCALE_NORMAL,
         mono: bool = False, bold: bool = False):
    """Return a Tk-compatible font tuple ``(family, size, style)``.

    Returned as a plain tuple rather than a font object so this module stays
    free of any toolkit import.
    """
    family = FONT_FAMILY_MONO if mono else FONT_FAMILY
    size = max(6, int(round(FONT_SIZES.get(size_key, FONT_SIZES["body"]) * scale)))
    return (family, size, "bold") if bold else (family, size)


def series_style(index: int) -> Dict[str, str]:
    """Colour, line style, marker and hatch for series ``index``.

    Every series differs from its neighbours in more than colour, which is what
    keeps an exported figure readable in greyscale.
    """
    return {
        "color": CHART_PALETTE[index % len(CHART_PALETTE)],
        "linestyle": CHART_LINESTYLES[index % len(CHART_LINESTYLES)],
        "marker": CHART_MARKERS[index % len(CHART_MARKERS)],
        "hatch": CHART_HATCHES[index % len(CHART_HATCHES)],
    }
