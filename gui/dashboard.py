#!/usr/bin/env python3
"""
Intent Coordinator GUI Dashboard

Real-time visualization for journal demo:
- System Control Panel (Start/Stop all components)
- Per-UE KPI monitoring with real-time graphs
- State machine visualization (S0-S6 + TechnicalFailsafe sink)
- Terminal-outcome indicator (Eq.12: Admitted / NotAdmitted / TechnicalFailsafe)
- LLM decision display with confidence gauge
- Intent status tracking
- Decision calibration (θ*, Eq.17) + finite-termination budget guard (Eq.28-30)

Sec I-IV alignment (docs/gui_alignment_sec1_4.md): the closed-form N_max was
DROPPED from the latest paper - finite termination is now the deterministic
budget guard (Eq.28-30); the retained ``n_max`` is only a SOFT negotiation-round
cap (N_a^max), never a headline metric.  The primary decision metric is θ*
(Eq.17).  The FSM's TechnicalFailsafe sink and the three Eq.12 terminal states
are shown distinctly rather than collapsed into S0-S6.
"""

import tkinter as tk
from tkinter import ttk, scrolledtext
import math
import threading
import queue
import time
import logging
import importlib
from datetime import datetime
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field
from collections import deque

# Matplotlib for graphs
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import matplotlib.patches as mpatches
import numpy as np

logger = logging.getLogger("GUI")


@dataclass
class GUIUpdate:
    """Update message for GUI thread"""
    update_type: str  # "ue_metrics", "state", "intent", "llm", "log",
                      # "calibration", "confidence", "terminal_outcome", "call"
    data: Dict


# --------------------------------------------------------------------------- #
# Eq.12 terminal states (Sec IV): S^term = {Admitted, NotAdmitted,
# TechnicalFailsafe}.  The coordinator's TerminalOutcome enum has FOUR members
# that collapse onto the paper's THREE terminal states - the two commit
# outcomes are both "Admitted".  The GUI shows the Eq.12 state so an operator
# sees exactly what the paper defines (not the internal 4-way enum).
# --------------------------------------------------------------------------- #
TERMINAL_STATE_ADMITTED = "Admitted"
TERMINAL_STATE_NOT_ADMITTED = "NotAdmitted"
TERMINAL_STATE_TECHNICAL_FAILSAFE = "TechnicalFailsafe"

# Ordered so the indicator row is always drawn Admitted -> NotAdmitted ->
# TechnicalFailsafe, and each has a distinct, colour-blind-safe hue.
TERMINAL_STATE_ORDER = (
    TERMINAL_STATE_ADMITTED,
    TERMINAL_STATE_NOT_ADMITTED,
    TERMINAL_STATE_TECHNICAL_FAILSAFE,
)
TERMINAL_STATE_COLORS = {
    TERMINAL_STATE_ADMITTED: "#4caf50",            # green  - a verified commit
    TERMINAL_STATE_NOT_ADMITTED: "#ff9800",        # amber  - refused admission
    TERMINAL_STATE_TECHNICAL_FAILSAFE: "#f44336",  # red    - fail-closed sink
}

# coordinator TerminalOutcome.value -> Eq.12 terminal state.
_TERMINAL_OUTCOME_TO_EQ12 = {
    "commit_original": TERMINAL_STATE_ADMITTED,
    "commit_revised": TERMINAL_STATE_ADMITTED,
    "pending_not_admitted": TERMINAL_STATE_NOT_ADMITTED,
    "technical_failsafe": TERMINAL_STATE_TECHNICAL_FAILSAFE,
}


# Shown wherever a live/measured value has not arrived yet, so a stale
# hard-coded prior can never be mistaken for a real measurement (Sec V priors
# 125.0/64.5/50.0/0.70 are NOT ground truth on this testbed).
PRE_MEASUREMENT = "—"   # em dash "-"


def terminal_state_label(outcome) -> Optional[str]:
    """Map a coordinator ``TerminalOutcome`` (enum, its ``.value`` string, or a
    raw result's ``terminal_outcome`` field) onto the paper's Eq.12 terminal
    state.  Returns None for an unknown/empty value (the GUI shows '-' - never a
    fabricated outcome)."""
    if outcome is None:
        return None
    key = str(getattr(outcome, "value", outcome)).strip().lower()
    return _TERMINAL_OUTCOME_TO_EQ12.get(key)


class RealTimeGraph:
    """Real-time scrolling graph for KPI visualization"""

    def __init__(self, parent, title: str, ylabel: str, max_points: int = 60,
                 figsize=(4, 2), colors=None):
        self.max_points = max_points
        self.colors = colors or {"bg": "#1e1e1e", "fg": "#ffffff"}

        # Data storage for multiple lines
        self.data: Dict[str, deque] = {}
        self.lines: Dict[str, any] = {}

        # Create figure with dark theme
        self.fig = Figure(figsize=figsize, dpi=80, facecolor=self.colors["bg"])
        self.ax = self.fig.add_subplot(111)

        # Style the axes
        self.ax.set_facecolor(self.colors["bg"])
        self.ax.set_title(title, color=self.colors["fg"], fontsize=10, pad=5)
        self.ax.set_ylabel(ylabel, color=self.colors["fg"], fontsize=8)
        self.ax.tick_params(colors=self.colors["fg"], labelsize=7)
        for spine in self.ax.spines.values():
            spine.set_color("#3c3c3c")
        self.ax.grid(True, alpha=0.2, color="#555")

        # Create canvas
        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Line colors
        self.line_colors = ["#4caf50", "#2196f3", "#ff9800", "#e91e63", "#9c27b0"]
        self.color_idx = 0

    def add_line(self, name: str, color: str = None):
        """Add a new data line"""
        if name not in self.data:
            self.data[name] = deque(maxlen=self.max_points)
            line_color = color or self.line_colors[self.color_idx % len(self.line_colors)]
            self.color_idx += 1
            line, = self.ax.plot([], [], label=name, color=line_color, linewidth=1.5)
            self.lines[name] = line
            self.ax.legend(loc='upper left', fontsize=7, facecolor=self.colors["bg"],
                          labelcolor=self.colors["fg"], framealpha=0.8)

    def update(self, name: str, value: float):
        """Update data for a line"""
        if name not in self.data:
            self.add_line(name)

        self.data[name].append(value)

        # Update line data
        x = list(range(len(self.data[name])))
        y = list(self.data[name])
        self.lines[name].set_data(x, y)

        # Adjust axes
        if x:
            self.ax.set_xlim(0, max(self.max_points, len(x)))
            all_values = [v for d in self.data.values() for v in d if v is not None]
            if all_values:
                ymin, ymax = min(all_values), max(all_values)
                margin = (ymax - ymin) * 0.1 if ymax != ymin else 1
                self.ax.set_ylim(ymin - margin, ymax + margin)

        self.canvas.draw_idle()

    def clear(self):
        """Clear all data"""
        for name in self.data:
            self.data[name].clear()
            self.lines[name].set_data([], [])
        self.canvas.draw_idle()


class ConfidenceGauge:
    """Confidence gauge showing current confidence vs threshold"""

    def __init__(self, parent, colors=None):
        self.colors = colors or {"bg": "#1e1e1e", "fg": "#ffffff"}
        self.confidence = 0.0
        self.threshold = 0.7

        # Create figure
        self.fig = Figure(figsize=(3.5, 1.5), dpi=80, facecolor=self.colors["bg"])
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor(self.colors["bg"])

        # Create canvas
        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        self._draw()

    def _draw(self):
        """Draw the gauge"""
        self.ax.clear()
        self.ax.set_facecolor(self.colors["bg"])
        self.ax.set_xlim(0, 1)
        self.ax.set_ylim(0, 1)
        self.ax.axis('off')

        # Background bar
        bar_height = 0.3
        bar_y = 0.4

        # Draw background
        self.ax.add_patch(mpatches.Rectangle(
            (0, bar_y), 1, bar_height,
            facecolor="#333", edgecolor="#555", linewidth=1
        ))

        # Draw confidence fill
        conf_color = "#4caf50" if self.confidence >= self.threshold else "#f44336"
        self.ax.add_patch(mpatches.Rectangle(
            (0, bar_y), self.confidence, bar_height,
            facecolor=conf_color, edgecolor="none"
        ))

        # Draw threshold line
        self.ax.axvline(x=self.threshold, ymin=bar_y - 0.1, ymax=bar_y + bar_height + 0.1,
                       color="#ff9800", linewidth=2, linestyle="--")

        # Labels
        self.ax.text(0.5, 0.9, "Confidence vs θ*", ha='center', va='center',
                    color=self.colors["fg"], fontsize=9, fontweight='bold')

        self.ax.text(0.02, bar_y + bar_height + 0.15,
                    f"Conf: {self.confidence:.2f}", ha='left', va='center',
                    color=conf_color, fontsize=8, fontweight='bold')

        self.ax.text(self.threshold, bar_y - 0.15,
                    f"θ*={self.threshold:.2f}", ha='center', va='center',
                    color="#ff9800", fontsize=8)

        # Status text
        status = "ACCEPT" if self.confidence >= self.threshold else "NEGOTIATE"
        status_color = "#4caf50" if self.confidence >= self.threshold else "#f44336"
        self.ax.text(0.98, bar_y + bar_height + 0.15,
                    status, ha='right', va='center',
                    color=status_color, fontsize=8, fontweight='bold')

        self.canvas.draw_idle()

    def update(self, confidence: float, threshold: float = None):
        """Update gauge values"""
        self.confidence = max(0, min(1, confidence))
        if threshold is not None:
            self.threshold = max(0, min(1, threshold))
        self._draw()


_UE_COLOR_PALETTE = ("#4caf50", "#2196f3", "#ff9800", "#e91e63",
                     "#9c27b0", "#00bcd4", "#8bc34a", "#ff5722")


def configured_ue_ids(config=None):
    """The configured UE membership for display (P0-18), DERIVED FROM
    CONFIGURATION (ue1, ue2, ue3, ...) - never a hardcoded ("UE1", "UE2") pair.
    Returns upper-cased ids; falls back to the legacy 2-UE labels only if the
    config is unavailable (keeps import-compat for headless use)."""
    try:
        if config is None:
            from config import get_config
            config = get_config()
        ues = [str(u).upper() for u in config.network.ues.keys()]
        return tuple(ues) if ues else ("UE1", "UE2")
    except Exception:
        return ("UE1", "UE2")


def configured_ue_colors(config=None):
    """{UE_ID: hex color} for EVERY configured UE (P0-18), cycling a palette so a
    3rd/4th UE always has a distinct, deterministic color."""
    ids = configured_ue_ids(config)
    return {ue: _UE_COLOR_PALETTE[i % len(_UE_COLOR_PALETTE)]
            for i, ue in enumerate(ids)}


def configured_bs_ids(config=None):
    """Configured gNB/BS ids (P0-18) in config order (native lower-case ids);
    falls back to the legacy pair if config is unavailable."""
    try:
        if config is None:
            from config import get_config
            config = get_config()
        bss = [str(g) for g in config.network.gnbs.keys()]
        return tuple(bss) if bss else ("gnb1", "gnb2")
    except Exception:
        return ("gnb1", "gnb2")


class ConstellationPlot:
    """Live per-UE constellation view.

    Renders a QAM constellation whose modulation order follows the UE's
    live MAC MCS and whose scatter (EVM) follows the measured SINR:
    high SINR -> tight clusters, low SINR -> smeared. This is a faithful
    visualization of the real link quality (not captured IQ) and gives an
    at-a-glance "the link is alive" indicator - the points jitter every
    tick and the layout changes (QPSK/16/64/256-QAM) as the link adapts.
    """

    N_POINTS = 256

    def __init__(self, parent, colors=None, ue_ids=None):
        self.colors = colors or {"bg": "#1e1e1e", "fg": "#ffffff"}
        # P0-18: UE membership is config-driven (variable UE count) unless an
        # explicit set is passed. Per-UE colors CYCLE the shared palette so any
        # number of UEs (ue4, ue5, ...) get a distinct, deterministic color -
        # never a fixed ue1/ue2/ue3 map. ue_colors.get(...) below still has a
        # default so an unexpected id never KeyErrors.
        self.ue_ids = list(ue_ids if ue_ids is not None else configured_ue_ids())
        self.ue_colors = {ue: _UE_COLOR_PALETTE[i % len(_UE_COLOR_PALETTE)]
                          for i, ue in enumerate(self.ue_ids)}
        # cached live link state per UE: (mcs, sinr, attached)
        self.state: Dict[str, tuple] = {u: (None, None, False) for u in self.ue_ids}

        self.fig = Figure(figsize=(5, 2.5), dpi=80, facecolor=self.colors["bg"])
        self.axes = {}
        self.scatters = {}
        n = len(self.ue_ids)
        for i, ue in enumerate(self.ue_ids):
            ax = self.fig.add_subplot(1, n, i + 1)
            ax.set_facecolor(self.colors["bg"])
            ax.set_xlim(-1.4, 1.4)
            ax.set_ylim(-1.4, 1.4)
            ax.set_aspect('equal')
            ax.set_xticks([])
            ax.set_yticks([])
            for sp in ax.spines.values():
                sp.set_color("#3c3c3c")
            ax.set_title(f"{ue}: --", color="#888", fontsize=9)
            self.axes[ue] = ax
        self.fig.tight_layout(pad=0.5)
        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    @staticmethod
    def _mod_order(mcs):
        """MCS -> QAM order (approx NR MCS table; for visualization)."""
        if mcs is None:
            return 0
        if mcs < 10:
            return 4      # QPSK
        if mcs < 17:
            return 16     # 16-QAM
        if mcs < 23:
            return 64     # 64-QAM
        return 256        # 256-QAM

    @staticmethod
    def _mod_name(m):
        return {4: "QPSK", 16: "16-QAM", 64: "64-QAM", 256: "256-QAM"}.get(m, "--")

    def set_state(self, ue_id: str, mcs, sinr, attached: bool):
        """Update the cached live link state for a UE (call on new metrics)."""
        ue_id = ue_id.upper()
        if ue_id in self.state:
            self.state[ue_id] = (mcs, sinr, attached)

    def tick(self):
        """Re-render every UE constellation with fresh noise (call on a timer
        for continuous motion). Cheap: a few hundred points per UE."""
        for ue in self.ue_ids:
            ax = self.axes[ue]
            mcs, sinr, attached = self.state[ue]
            if ue in self.scatters and self.scatters[ue] is not None:
                try:
                    self.scatters[ue].remove()
                except Exception:
                    pass
                self.scatters[ue] = None

            M = self._mod_order(mcs)
            if not attached or M == 0:
                ax.set_title(f"{ue}: no signal", color="#888", fontsize=9)
                continue

            k = int(round(M ** 0.5))                 # levels per axis
            levels = np.linspace(-1.0, 1.0, k)
            grid = np.array([(x, y) for x in levels for y in levels])
            idx = np.random.randint(0, len(grid), self.N_POINTS)
            base = grid[idx]

            spacing = 2.0 / (k - 1) if k > 1 else 2.0
            s = sinr if sinr is not None else 0.0
            # EVM ~ 10^(-SINR/20); clamp so it stays readable
            evm = min(10 ** (-s / 20.0), 0.9)
            noise_std = spacing * evm * 0.5
            pts = base + np.random.normal(0.0, noise_std, base.shape)

            color = self.ue_colors.get(ue, "#4caf50")
            self.scatters[ue] = ax.scatter(
                pts[:, 0], pts[:, 1], s=6, c=color, alpha=0.55, edgecolors='none')
            snr_txt = f"{s:.0f}dB" if sinr is not None else "--"
            ax.set_title(f"{ue}: {self._mod_name(M)}  SINR {snr_txt}",
                         color=self.colors["fg"], fontsize=9)
        self.canvas.draw_idle()

    def clear(self):
        for ue in self.ue_ids:
            self.state[ue] = (None, None, False)
        self.tick()


class IntentCoordinatorGUI:
    """
    Main GUI Dashboard for Agentic Intent Coordinator.

    Features:
    - System Control Panel (Start/Stop Core, gNBs, UEs)
    - Real-time per-UE KPI display with graphs (RSRP, throughput)
    - State machine visualization with transition animation (S0-S6 +
      TechnicalFailsafe sink)
    - Terminal-outcome indicator (Eq.12: Admitted / NotAdmitted /
      TechnicalFailsafe)
    - Confidence gauge (current vs θ*, Eq.17)
    - Active intents panel
    - LLM reasoning display
    - Decision calibration (θ*, Eq.17) + budget-guard readout (Eq.28-30)
    - Event log
    """

    def __init__(self, title: str = "Agentic Intent Coordinator", *,
                 profile: str = "oran"):
        if profile not in {"oran", "legacy"}:
            raise ValueError("GUI profile must be 'oran' or 'legacy'")
        self.title = title
        self.profile = profile
        self.root = None
        self.running = False

        # Update queue for thread-safe GUI updates
        self.update_queue = queue.Queue()

        # System controller for component management
        self.system_controller = None
        self._component_status_type = None
        if profile == "legacy":
            try:
                legacy = importlib.import_module("gui.legacy_tools")
                self._component_status_type = legacy.ComponentStatus
                self.system_controller = legacy.SystemController()
                self.system_controller.on_status_change = self._on_component_status_change
                self.system_controller.on_log = self._on_component_log
            except ImportError:
                logger.warning("legacy system controller is unavailable")

        # Callbacks
        self.on_intent_submit: Optional[Callable] = None
        self.on_command: Optional[Callable] = None
        self.on_llm_change: Optional[Callable] = None
        self.on_llm_refresh: Optional[Callable] = None

        # State
        self.current_state = "S0"
        self.ue_data: Dict[str, Dict] = {}
        self.active_intents: List[Dict] = []

        # Graph components
        self.rsrp_graph: Optional[RealTimeGraph] = None
        self.throughput_graph: Optional[RealTimeGraph] = None
        self.confidence_gauge: Optional[ConfidenceGauge] = None
        self.constellation: Optional[ConstellationPlot] = None

        # Single-flight guard for background component status polling
        self._status_poll_active = False

        # Component status widgets
        self.component_status_labels: Dict[str, tk.Label] = {}
        self.component_buttons: Dict[str, Dict[str, tk.Button]] = {}

        # Colors
        self.colors = {
            "bg": "#1e1e1e",
            "fg": "#ffffff",
            "accent": "#007acc",
            "success": "#4caf50",
            "warning": "#ff9800",
            "error": "#f44336",
            "panel_bg": "#252526",
            "border": "#3c3c3c"
        }

        # State colors.  S0-S6 are the coordination FSM states; the
        # TechnicalFailsafe sink (S_TECHNICAL_FAILSAFE, coordinator/fsm.py) is a
        # DISTINCT node so an operator can SEE a fail-closed episode - before
        # this it was emitted by the coordinator but never highlighted (all
        # circles just went dark).  Its dark-red hue is distinct from S5's red.
        self.state_colors = {
            "S0": "#4caf50",
            "S1": "#2196f3",
            "S2": "#9c27b0",
            "S3": "#ff9800",
            "S4": "#00bcd4",
            "S5": "#f44336",
            "S6": "#8bc34a",
            "S_TECHNICAL_FAILSAFE": "#b71c1c",
        }
        # Last observed Eq.12 terminal state (None until an episode finalizes).
        self.terminal_state: Optional[str] = None

        # Component status colors
        self.status_colors = {
            "stopped": "#888888",
            "starting": "#ff9800",
            "running": "#4caf50",
            "stopping": "#ff9800",
            "error": "#f44336",
            "unknown": "#555555"
        }

    def create_window(self):
        """Create main window and widgets"""
        self.root = tk.Tk()
        self.root.title(self.title)
        self.root.geometry("1600x950")
        self.root.configure(bg=self.colors["bg"])

        # Configure grid - 3 columns now
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_columnconfigure(2, weight=1)
        self.root.grid_rowconfigure(0, weight=0)
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_rowconfigure(2, weight=0)

        # Create panels
        self._create_header()
        self._create_left_panel()
        self._create_center_panel()
        self._create_right_panel()
        self._create_footer()

        # Start update loops
        self._schedule_update()          # drain the thread-safe update queue
        self._schedule_constellation()   # continuous constellation animation
        self._schedule_status_poll()     # auto-refresh component status

    def _create_header(self):
        """Create header with state machine visualization + Eq.12 terminal row"""
        header = tk.Frame(self.root, bg=self.colors["panel_bg"], height=175)
        header.grid(row=0, column=0, columnspan=3, sticky="ew", padx=5, pady=5)
        header.grid_propagate(False)

        # Title
        title_label = tk.Label(
            header, text="Agentic Intent Coordinator",
            font=("Helvetica", 18, "bold"),
            bg=self.colors["panel_bg"], fg=self.colors["fg"]
        )
        title_label.pack(pady=(5, 0))

        # State machine visualization
        state_frame = tk.Frame(header, bg=self.colors["panel_bg"])
        state_frame.pack(pady=(6, 2))

        self.state_labels = {}
        # S0-S6 are the linear coordination path; the TechnicalFailsafe sink is
        # NOT part of that chain (it is reachable from ANY state), so it is drawn
        # AFTER a sink separator with no forward arrow - see below.
        states = ["S0", "S1", "S2", "S3", "S4", "S5", "S6"]
        state_names = ["Normal", "Screen", "LLM", "Trial", "Valid", "Nego", "Done"]

        for i, (state, name) in enumerate(zip(states, state_names)):
            frame = tk.Frame(state_frame, bg=self.colors["panel_bg"])
            frame.pack(side=tk.LEFT, padx=8)

            # State circle
            canvas = tk.Canvas(frame, width=46, height=46,
                             bg=self.colors["panel_bg"], highlightthickness=0)
            canvas.pack()

            color = self.state_colors[state] if state == "S0" else self.colors["border"]
            circle = canvas.create_oval(5, 5, 41, 41, fill=color, outline=color)
            text = canvas.create_text(23, 23, text=state, fill="white", font=("Helvetica", 11, "bold"))

            self.state_labels[state] = {"canvas": canvas, "circle": circle, "text": text}

            # State name
            label = tk.Label(frame, text=name, font=("Helvetica", 9),
                           bg=self.colors["panel_bg"], fg=self.colors["fg"])
            label.pack()

            # Arrow (except last)
            if i < len(states) - 1:
                arrow = tk.Label(state_frame, text="→", font=("Helvetica", 15),
                               bg=self.colors["panel_bg"], fg=self.colors["border"])
                arrow.pack(side=tk.LEFT)

        # Sink separator + TechnicalFailsafe node (a persistent sink, reachable
        # from ANY state, coordinator/fsm.py) - a dashed "⇢" makes clear it is NOT
        # the next sequential step after S6.
        sink_arrow = tk.Label(state_frame, text="⇢", font=("Helvetica", 15),
                              bg=self.colors["panel_bg"], fg="#b71c1c")
        sink_arrow.pack(side=tk.LEFT, padx=(10, 0))

        fs_state = "S_TECHNICAL_FAILSAFE"
        fs_frame = tk.Frame(state_frame, bg=self.colors["panel_bg"])
        fs_frame.pack(side=tk.LEFT, padx=8)
        fs_canvas = tk.Canvas(fs_frame, width=46, height=46,
                              bg=self.colors["panel_bg"], highlightthickness=0)
        fs_canvas.pack()
        fs_circle = fs_canvas.create_oval(5, 5, 41, 41,
                                          fill=self.colors["border"],
                                          outline=self.colors["border"])
        fs_text = fs_canvas.create_text(23, 23, text="FS", fill="white",
                                        font=("Helvetica", 11, "bold"))
        self.state_labels[fs_state] = {"canvas": fs_canvas, "circle": fs_circle,
                                       "text": fs_text}
        tk.Label(fs_frame, text="Failsafe", font=("Helvetica", 9),
                 bg=self.colors["panel_bg"], fg=self.colors["fg"]).pack()

        # --- Eq.12 terminal-outcome indicator ------------------------------- #
        # S6 (Done) resolves into one of the paper's THREE terminal states; the
        # sink is the third.  This row lights the LAST episode's terminal state
        # so Admitted / NotAdmitted / TechnicalFailsafe are visibly DISTINCT
        # (before this the GUI collapsed them all into "S6 Done").
        term_frame = tk.Frame(header, bg=self.colors["panel_bg"])
        term_frame.pack(pady=(0, 4))
        tk.Label(term_frame, text="Terminal (Eq.12):", font=("Helvetica", 9, "bold"),
                 bg=self.colors["panel_bg"], fg="#888").pack(side=tk.LEFT, padx=(0, 6))

        self.terminal_labels: Dict[str, tk.Label] = {}
        for name in TERMINAL_STATE_ORDER:
            lbl = tk.Label(term_frame, text=name, font=("Helvetica", 9, "bold"),
                           bg=self.colors["border"], fg="#bbbbbb",
                           padx=8, pady=2)
            lbl.pack(side=tk.LEFT, padx=3)
            self.terminal_labels[name] = lbl

    def _create_left_panel(self):
        """Create left panel with system control, UE metrics, and intents"""
        left_frame = tk.Frame(self.root, bg=self.colors["bg"])
        left_frame.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)

        # System Control Panel
        self._create_control_panel(left_frame)

        # UE Metrics Panel
        ue_panel = tk.LabelFrame(
            left_frame, text="UE Metrics",
            font=("Helvetica", 12, "bold"),
            bg=self.colors["panel_bg"], fg=self.colors["fg"]
        )
        ue_panel.pack(fill=tk.X, pady=(0, 5))

        # UE metrics display
        self.ue_frames = {}
        self.ue_metric_labels = {}

        for ue_id in configured_ue_ids():          # P0-18: config-driven UEs
            frame = tk.Frame(ue_panel, bg=self.colors["panel_bg"])
            frame.pack(fill=tk.X, padx=10, pady=5)

            # UE header
            header = tk.Label(frame, text=ue_id, font=("Helvetica", 11, "bold"),
                            bg=self.colors["panel_bg"], fg=self.colors["accent"])
            header.pack(anchor="w")

            # Metrics grid
            metrics_frame = tk.Frame(frame, bg=self.colors["panel_bg"])
            metrics_frame.pack(fill=tk.X, padx=10)

            self.ue_metric_labels[ue_id] = {}

            metrics = [
                ("Status", "Disconnected"),
                ("PCI", "-"),
                ("RSRP", "- dBm"),
                ("SINR", "- dB"),
                ("Throughput", "- Mbps"),
                ("Latency", "- ms")
            ]

            for i, (name, default) in enumerate(metrics):
                row = i // 3
                col = i % 3

                label = tk.Label(metrics_frame, text=f"{name}:",
                               font=("Helvetica", 9),
                               bg=self.colors["panel_bg"], fg="#888")
                label.grid(row=row, column=col*2, sticky="e", padx=(5, 2))

                value = tk.Label(metrics_frame, text=default,
                               font=("Helvetica", 9, "bold"),
                               bg=self.colors["panel_bg"], fg=self.colors["fg"])
                value.grid(row=row, column=col*2+1, sticky="w", padx=(0, 15))

                self.ue_metric_labels[ue_id][name] = value

            self.ue_frames[ue_id] = frame

        # Active Intents Panel
        intent_panel = tk.LabelFrame(
            left_frame, text="Active Intents",
            font=("Helvetica", 12, "bold"),
            bg=self.colors["panel_bg"], fg=self.colors["fg"]
        )
        intent_panel.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        self.intent_listbox = tk.Listbox(
            intent_panel, font=("Consolas", 10),
            bg=self.colors["bg"], fg=self.colors["fg"],
            selectbackground=self.colors["accent"],
            height=5
        )
        self.intent_listbox.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Decision-calibration panel (θ*, Eq.17).  N_max is GONE from here: the
        # closed-form N_max was dropped from the latest paper, so it is no longer
        # a headline calibration parameter (the retained soft cap moved to the
        # budget-guard panel below).  The θ* inputs use Eq.15-17 notation
        # (C_F = failure cost, R = success reward, C_N = non-trial cost) and
        # start at the PRE_MEASUREMENT placeholder so the Sec V priors are never
        # shown as if they were measured on this testbed.
        self.calib_labels = {}

        calib_panel = tk.LabelFrame(
            left_frame, text="Decision Calibration (θ*, Eq.17)",
            font=("Helvetica", 12, "bold"),
            bg=self.colors["panel_bg"], fg=self.colors["fg"]
        )
        calib_panel.pack(fill=tk.X, pady=(0, 5))

        calib_frame = tk.Frame(calib_panel, bg=self.colors["panel_bg"])
        calib_frame.pack(fill=tk.X, padx=10, pady=5)

        # (display label, data key from calibrator.get_stats()).
        calib_items = [
            ("θ*", None),          # theta_star (handled specially: also gauge)
            ("C_F", None),         # c_worst_estimate
            ("R", None),           # r_success_estimate
            ("C_N", None),         # c_nego_estimate
            ("θ* valid", None),    # assumption_ok (C_F > C_N; Eq.17 assumption)
        ]
        self._add_stat_grid(calib_frame, [n for n, _ in calib_items], per_row=3)

        # Finite-termination BUDGET GUARD (Eq.28-30) - the mechanism that
        # REPLACED the closed-form N_max.  "Nego cap" is the retained SOFT
        # negotiation-round guard (N_a^max), NOT a safety finite-termination
        # bound; rollbacks/negotiations are the coordinator's running S4/S5
        # counts (already produced by get_stats, previously never shown).
        guard_panel = tk.LabelFrame(
            left_frame, text="Budget Guard (Eq.28-30)",
            font=("Helvetica", 12, "bold"),
            bg=self.colors["panel_bg"], fg=self.colors["fg"]
        )
        guard_panel.pack(fill=tk.X, pady=(0, 5))
        guard_frame = tk.Frame(guard_panel, bg=self.colors["panel_bg"])
        guard_frame.pack(fill=tk.X, padx=10, pady=5)
        self._add_stat_grid(guard_frame,
                            ["Nego cap", "Rollbacks", "Negotiations"], per_row=3)

    def _add_stat_grid(self, parent, names, per_row=3):
        """Lay out a label:value grid of read-only stat cells, each value seeded
        with the PRE_MEASUREMENT placeholder (muted) and registered in
        ``self.calib_labels`` under its display name."""
        for i, name in enumerate(names):
            row = i // per_row
            col = i % per_row
            label = tk.Label(parent, text=f"{name}:",
                           font=("Helvetica", 9),
                           bg=self.colors["panel_bg"], fg="#888")
            label.grid(row=row, column=col*2, sticky="e", padx=(5, 2))

            value = tk.Label(parent, text=PRE_MEASUREMENT,
                           font=("Helvetica", 9, "bold"),
                           bg=self.colors["panel_bg"], fg="#888")
            value.grid(row=row, column=col*2+1, sticky="w", padx=(0, 15))

            self.calib_labels[name] = value

    def _create_control_panel(self, parent):
        """Create system control panel with start/stop buttons"""
        if self.profile == "oran":
            self._create_oran_observability_panel(parent)
            return
        control_panel = tk.LabelFrame(
            parent, text="System Control",
            font=("Helvetica", 12, "bold"),
            bg=self.colors["panel_bg"], fg=self.colors["fg"]
        )
        control_panel.pack(fill=tk.X, pady=(0, 5))

        # Master control buttons
        master_frame = tk.Frame(control_panel, bg=self.colors["panel_bg"])
        master_frame.pack(fill=tk.X, padx=5, pady=5)

        start_all_btn = tk.Button(
            master_frame, text="▶ Start All",
            font=("Helvetica", 10, "bold"),
            bg=self.colors["success"], fg="white",
            width=12, height=1,
            command=self._start_all_components
        )
        start_all_btn.pack(side=tk.LEFT, padx=5)

        stop_all_btn = tk.Button(
            master_frame, text="■ Stop All",
            font=("Helvetica", 10, "bold"),
            bg=self.colors["error"], fg="white",
            width=12, height=1,
            command=self._stop_all_components
        )
        stop_all_btn.pack(side=tk.LEFT, padx=5)

        refresh_btn = tk.Button(
            master_frame, text="↻ Refresh",
            font=("Helvetica", 10),
            bg=self.colors["border"], fg="white",
            width=8,
            command=self._refresh_component_status
        )
        refresh_btn.pack(side=tk.LEFT, padx=5)

        logs_btn = tk.Button(
            master_frame, text="📄 Logs",
            font=("Helvetica", 10),
            bg=self.colors["border"], fg="white",
            width=7,
            command=self._open_log_viewer
        )
        logs_btn.pack(side=tk.LEFT, padx=5)

        # Toggle the OAI soft-scope (real Rx constellation window on each gNB PC)
        self.scope_btn = tk.Button(
            master_frame, text="🔬 Scope: OFF",
            font=("Helvetica", 10),
            bg=self.colors["border"], fg="white",
            width=12,
            command=self._toggle_scope
        )
        self.scope_btn.pack(side=tk.LEFT, padx=5)

        # One-shot 10G NIC + host real-time tuning + USRP detection (run once
        # after a reboot; the NIC/C-state settings are runtime-only). Do this
        # BEFORE Start All so the X310s are reachable and tuned.
        self.teng_btn = tk.Button(
            master_frame, text="🔧 10G Prep",
            font=("Helvetica", 10),
            bg=self.colors["border"], fg="white",
            width=10,
            command=self._run_10g_setup
        )
        self.teng_btn.pack(side=tk.LEFT, padx=5)

        # PRB Selection
        prb_frame = tk.Frame(control_panel, bg=self.colors["panel_bg"])
        prb_frame.pack(fill=tk.X, padx=5, pady=5)

        tk.Label(
            prb_frame, text="PRB Config:",
            font=("Helvetica", 10),
            bg=self.colors["panel_bg"], fg=self.colors["fg"]
        ).pack(side=tk.LEFT, padx=(0, 5))

        self.prb_var = tk.StringVar(value="24")
        prb_options = [
            ("24", "24 PRB (10 MHz, 15.36 MSps) - 1GbE Compatible"),
            ("51", "51 PRB (20 MHz, 30.72 MSps) - 10GbE, RPi marginal"),
            ("106", "106 PRB (40 MHz, 61.44 MSps) - 10GbE Required")
        ]
        self.prb_dropdown = ttk.Combobox(
            prb_frame,
            textvariable=self.prb_var,
            values=[f"{prb} - {desc}" for prb, desc in prb_options],
            state="readonly",
            width=45
        )
        self.prb_dropdown.set("24 - 24 PRB (10 MHz, 15.36 MSps) - 1GbE Compatible")
        self.prb_dropdown.pack(side=tk.LEFT, padx=5)
        self.prb_dropdown.bind("<<ComboboxSelected>>", self._on_prb_change)

        # Component list (P0-18: derived from ONE configured UE/BS list, not a
        # hardcoded ue1/ue2 pair).
        components = [("5g-core", "5G Core")]
        for bs in configured_bs_ids():
            components.append((bs, bs.upper()))
        for ue in configured_ue_ids():
            components.append((ue.lower(), ue))

        for comp_id, comp_name in components:
            self._create_component_row(control_panel, comp_id, comp_name)

    def _create_oran_observability_panel(self, parent):
        """Read-only policy/assurance summary for the O-RAN profile.

        It contains no radio, process, shell, or transport control.  Values are
        presentation-only copies and therefore cannot mutate policy or safety
        decisions.
        """
        panel = tk.LabelFrame(
            parent, text="O-RAN Policy & Assurance",
            font=("Helvetica", 12, "bold"),
            bg=self.colors["panel_bg"], fg=self.colors["fg"])
        panel.pack(fill=tk.X, pady=(0, 5))
        self.oran_labels = {}
        for row, (key, label) in enumerate((
                ("policyLifecycle", "A1 policy lifecycle"),
                ("negotiation", "Negotiation"),
                ("assurance", "KPI / assurance"),
                ("evidence", "Outcome evidence"))):
            tk.Label(panel, text=label + ":", bg=self.colors["panel_bg"],
                     fg="#888").grid(row=row, column=0, sticky="e", padx=5, pady=2)
            value = tk.Label(panel, text=PRE_MEASUREMENT,
                             bg=self.colors["panel_bg"], fg=self.colors["fg"])
            value.grid(row=row, column=1, sticky="w", padx=5, pady=2)
            self.oran_labels[key] = value

    def _create_component_row(self, parent, comp_id: str, comp_name: str):
        """Create a row for a single component with status and controls"""
        row_frame = tk.Frame(parent, bg=self.colors["panel_bg"])
        row_frame.pack(fill=tk.X, padx=5, pady=2)

        # Status indicator (circle)
        status_canvas = tk.Canvas(
            row_frame, width=16, height=16,
            bg=self.colors["panel_bg"], highlightthickness=0
        )
        status_canvas.pack(side=tk.LEFT, padx=(5, 5))
        status_circle = status_canvas.create_oval(2, 2, 14, 14, fill="#555", outline="#555")

        # Component name
        name_label = tk.Label(
            row_frame, text=comp_name,
            font=("Helvetica", 9),
            bg=self.colors["panel_bg"], fg=self.colors["fg"],
            width=12, anchor="w"
        )
        name_label.pack(side=tk.LEFT)

        # Status text
        status_label = tk.Label(
            row_frame, text="Unknown",
            font=("Helvetica", 8),
            bg=self.colors["panel_bg"], fg="#888",
            width=8
        )
        status_label.pack(side=tk.LEFT, padx=5)

        # Store references
        self.component_status_labels[comp_id] = {
            "canvas": status_canvas,
            "circle": status_circle,
            "label": status_label
        }

        # Buttons
        start_btn = tk.Button(
            row_frame, text="▶",
            font=("Helvetica", 8),
            bg=self.colors["success"], fg="white",
            width=3, height=1,
            command=lambda c=comp_id: self._start_component(c)
        )
        start_btn.pack(side=tk.LEFT, padx=2)

        stop_btn = tk.Button(
            row_frame, text="■",
            font=("Helvetica", 8),
            bg=self.colors["error"], fg="white",
            width=3, height=1,
            command=lambda c=comp_id: self._stop_component(c)
        )
        stop_btn.pack(side=tk.LEFT, padx=2)

        self.component_buttons[comp_id] = {
            "start": start_btn,
            "stop": stop_btn
        }

    def _start_component(self, comp_id: str):
        """Start a single component"""
        if self.system_controller:
            self.log(f"Starting {comp_id}...")
            self.system_controller.start_component(comp_id)

    def _stop_component(self, comp_id: str):
        """Stop a single component"""
        if self.system_controller:
            self.log(f"Stopping {comp_id}...")
            self.system_controller.stop_component(comp_id)

    def _start_all_components(self):
        """Start all system components"""
        if self.system_controller:
            self.log("Starting full system (Core → gNBs → UEs)...")
            self.system_controller.start_all()

    def _stop_all_components(self):
        """Stop all system components"""
        if self.system_controller:
            self.log("Stopping full system (UEs → gNBs → Core)...")
            self.system_controller.stop_all()

    def _refresh_component_status(self):
        """Refresh status of all components"""
        if self.system_controller:
            self.log("Refreshing component status...")

            def do_refresh():
                status = self.system_controller.check_all_status()
                for comp_id, comp_status in status.items():
                    self._update_component_status(comp_id, comp_status)

            thread = threading.Thread(target=do_refresh, daemon=True)
            thread.start()

    def _open_log_viewer(self):
        """Open a window that tails a component's live log (gNB/UE/core),
        so the operator never needs a terminal to see what's happening."""
        if not self.system_controller:
            self.log("No system controller - logs unavailable")
            return

        win = tk.Toplevel(self.root)
        win.title("Component Logs")
        win.geometry("900x600")
        win.configure(bg=self.colors["bg"])

        top = tk.Frame(win, bg=self.colors["panel_bg"])
        top.pack(fill=tk.X, padx=5, pady=5)

        tk.Label(top, text="Component:", font=("Helvetica", 10),
                 bg=self.colors["panel_bg"], fg=self.colors["fg"]).pack(side=tk.LEFT, padx=(5, 5))

        comp_var = tk.StringVar(value="gnb1")
        comp_names = (["5g-core"] + list(configured_bs_ids())
                      + [u.lower() for u in configured_ue_ids()])
        combo = ttk.Combobox(top, textvariable=comp_var, values=comp_names,
                             state="readonly", width=12)
        combo.pack(side=tk.LEFT, padx=5)

        auto_var = tk.BooleanVar(value=True)
        tk.Checkbutton(top, text="Auto-refresh (2s)", variable=auto_var,
                       font=("Helvetica", 9), bg=self.colors["panel_bg"],
                       fg=self.colors["fg"], selectcolor=self.colors["bg"],
                       activebackground=self.colors["panel_bg"]).pack(side=tk.LEFT, padx=10)

        text = scrolledtext.ScrolledText(win, font=("Consolas", 9),
                                         bg=self.colors["bg"], fg=self.colors["fg"],
                                         wrap=tk.NONE)
        text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        def fetch():
            name = comp_var.get()

            def worker():
                try:
                    content = self.system_controller.get_component_log(name, lines=200)
                except Exception as e:
                    content = f"(failed to read {name} log: {e})"
                # update the Toplevel from the GUI thread
                def apply():
                    if not win.winfo_exists():
                        return
                    text.delete("1.0", tk.END)
                    text.insert(tk.END, content or f"(no log yet for {name} - is it running?)")
                    text.see(tk.END)
                self.update_queue.put(GUIUpdate("call", {"fn": apply}))

            threading.Thread(target=worker, daemon=True).start()

        def auto_loop():
            if not win.winfo_exists():
                return
            if auto_var.get():
                fetch()
            win.after(2000, auto_loop)

        tk.Button(top, text="Refresh now", font=("Helvetica", 9),
                  bg=self.colors["accent"], fg="white", command=fetch).pack(side=tk.LEFT, padx=5)
        combo.bind("<<ComboboxSelected>>", lambda e: fetch())

        fetch()
        auto_loop()

    def _run_10g_setup(self):
        """Run the 10G NIC + host tuning + USRP detection on both PCs (once per
        boot). Runs off the GUI thread; results go to the event log."""
        if not self.system_controller:
            self.log("No system controller - 10G setup unavailable")
            return
        self.log("10G Prep: configuring NICs + USRP on PC1 and PC2 "
                 "(run this before Start All after a reboot)...")
        self.teng_btn.config(state=tk.DISABLED)

        def do_setup():
            try:
                ok = self.system_controller.run_10g_setup()
                self.log("10G Prep: done ✓" if ok else
                         "10G Prep: finished with warnings (see log / check SFP)")
            except Exception as e:
                self.log(f"10G Prep failed: {e}")
            finally:
                self.update_queue.put(GUIUpdate(
                    "call", {"fn": lambda: self.teng_btn.config(state=tk.NORMAL)}))

        threading.Thread(target=do_setup, daemon=True).start()

    def _toggle_scope(self):
        """Toggle the OAI soft-scope on the gNBs. Because OAI only opens the
        scope at gNB startup, this restarts any running gNB so the real Rx
        constellation window appears (or disappears) on each gNB's own PC."""
        if not self.system_controller:
            self.log("No system controller - scope unavailable")
            return

        enable = not self.system_controller.scope_enabled
        self.system_controller.set_scope(enable)
        self.scope_btn.config(
            text=f"🔬 Scope: {'ON' if enable else 'OFF'}",
            bg=self.colors["success"] if enable else self.colors["border"])

        if enable:
            self.log("RF Scope ENABLED - a real constellation window will open "
                     "on each gNB's own screen (restarting running gNBs)...")
        else:
            self.log("RF Scope disabled - restarting running gNBs without scope...")

        def do_restart():
            for gnb in ("gnb1", "gnb2"):
                try:
                    running = getattr(self._component_status_type, "RUNNING", object())
                    if self.system_controller.check_status(gnb) == running:
                        self.system_controller.stop_component(gnb, async_stop=False)
                        self.system_controller.start_component(gnb, async_start=False)
                    else:
                        self.log(f"{gnb} not running - scope applies on next start")
                except Exception as e:
                    logger.debug(f"scope restart {gnb} failed: {e}")

        threading.Thread(target=do_restart, daemon=True).start()

    def _update_component_status(self, comp_id: str, status):
        """Update status display for a component"""
        if comp_id not in self.component_status_labels:
            return

        widgets = self.component_status_labels[comp_id]

        # Get status name and color
        if self._component_status_type:
            status_name = status.value if hasattr(status, 'value') else str(status)
        else:
            status_name = str(status)

        color = self.status_colors.get(status_name, "#555")

        def update():
            widgets["canvas"].itemconfig(widgets["circle"], fill=color, outline=color)
            widgets["label"].config(text=status_name.capitalize(), fg=color)

        # Called from SystemController worker threads: Tk is not thread-safe,
        # so route through the update queue instead of root.after()
        self.update_queue.put(GUIUpdate("call", {"fn": update}))

    def _on_component_status_change(self, comp_id: str, status):
        """Callback when component status changes"""
        self._update_component_status(comp_id, status)

    def _on_component_log(self, component: str, message: str):
        """Callback for component log messages"""
        self.log(f"[{component}] {message}")

    def _on_prb_change(self, event=None):
        """Handle PRB selection change"""
        selected = self.prb_dropdown.get()
        prb = selected.split(" - ")[0] if " - " in selected else selected

        if self.system_controller:
            self.system_controller.set_prb_config(int(prb))
            self.log(f"PRB config changed to: {prb} PRB")

            if prb == "24":
                self.log("INFO: 24 PRB (10 MHz) - Compatible with 1GbE and RPi5 + B206mini")
            elif prb == "51":
                self.log("INFO: 51 PRB (20 MHz) requires 10GbE on the gNB side")
                self.log("INFO: UE runs at 23.04 MSps (-E) - RPi5 should sustain; monitor UE CPU")
            elif prb == "106":
                self.log("WARNING: 106 PRB requires 10GbE!")
                self.log("INFO: UE at 46 MSps (-E) is borderline on RPi5 ARM - verify (watch late/CPU)")

    def _create_center_panel(self):
        """Create center panel with real-time graphs"""
        center_frame = tk.Frame(self.root, bg=self.colors["bg"])
        center_frame.grid(row=1, column=1, sticky="nsew", padx=5, pady=5)

        # Live constellation (headline "link is alive" visual) - top, prominent
        const_panel = tk.LabelFrame(
            center_frame, text="Live Constellation (from MCS / SINR)",
            font=("Helvetica", 12, "bold"),
            bg=self.colors["panel_bg"], fg=self.colors["fg"]
        )
        const_panel.pack(fill=tk.BOTH, expand=True, pady=(0, 5))
        self.constellation = ConstellationPlot(const_panel, self.colors,
                                               ue_ids=configured_ue_ids())

        # RSRP Graph
        rsrp_panel = tk.LabelFrame(
            center_frame, text="RSRP (Real-time)",
            font=("Helvetica", 12, "bold"),
            bg=self.colors["panel_bg"], fg=self.colors["fg"]
        )
        rsrp_panel.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        self.rsrp_graph = RealTimeGraph(
            rsrp_panel, title="RSRP over Time", ylabel="dBm",
            max_points=60, figsize=(5, 1.8), colors=self.colors
        )
        for ue, color in configured_ue_colors().items():   # P0-18: config-driven
            self.rsrp_graph.add_line(ue, color)

        # Throughput Graph
        tp_panel = tk.LabelFrame(
            center_frame, text="Throughput (Real-time)",
            font=("Helvetica", 12, "bold"),
            bg=self.colors["panel_bg"], fg=self.colors["fg"]
        )
        tp_panel.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        self.throughput_graph = RealTimeGraph(
            tp_panel, title="Throughput over Time", ylabel="Mbps",
            max_points=60, figsize=(5, 1.8), colors=self.colors
        )
        for ue, color in configured_ue_colors().items():   # P0-18: config-driven
            self.throughput_graph.add_line(ue, color)

        # Confidence Gauge
        conf_panel = tk.LabelFrame(
            center_frame, text="LLM Confidence",
            font=("Helvetica", 12, "bold"),
            bg=self.colors["panel_bg"], fg=self.colors["fg"]
        )
        conf_panel.pack(fill=tk.X, pady=(0, 5))

        self.confidence_gauge = ConfidenceGauge(conf_panel, self.colors)

    def _create_right_panel(self):
        """Create right panel with LLM output and log"""
        right_frame = tk.Frame(self.root, bg=self.colors["bg"])
        right_frame.grid(row=1, column=2, sticky="nsew", padx=5, pady=5)

        # LLM Reasoning Panel
        llm_panel = tk.LabelFrame(
            right_frame, text="LLM Analysis",
            font=("Helvetica", 12, "bold"),
            bg=self.colors["panel_bg"], fg=self.colors["fg"]
        )
        llm_panel.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        self.llm_text = scrolledtext.ScrolledText(
            llm_panel, font=("Consolas", 10),
            bg=self.colors["bg"], fg=self.colors["fg"],
            wrap=tk.WORD, height=12
        )
        self.llm_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Event Log Panel
        log_panel = tk.LabelFrame(
            right_frame, text="Event Log",
            font=("Helvetica", 12, "bold"),
            bg=self.colors["panel_bg"], fg=self.colors["fg"]
        )
        log_panel.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_panel, font=("Consolas", 9),
            bg=self.colors["bg"], fg=self.colors["fg"],
            wrap=tk.WORD, height=12
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def _create_footer(self):
        """Create footer with input, LLM selector, and controls"""
        footer = tk.Frame(self.root, bg=self.colors["panel_bg"], height=80)
        footer.grid(row=2, column=0, columnspan=3, sticky="ew", padx=5, pady=5)
        footer.grid_propagate(False)

        # LLM Selector row
        llm_frame = tk.Frame(footer, bg=self.colors["panel_bg"])
        llm_frame.pack(fill=tk.X, padx=10, pady=(5, 0))

        llm_label = tk.Label(llm_frame, text="LLM Backend:",
                            font=("Helvetica", 10),
                            bg=self.colors["panel_bg"], fg=self.colors["fg"])
        llm_label.pack(side=tk.LEFT)

        # LLM dropdown values
        self.llm_options = [
            "claude-sonnet",
            "claude-opus",
            "gpt-4o",
            "gpt-4o-mini",
            "codex",
            "gemini-pro",
            "gemini-flash",
            "llama-3",
            "llama-3-70b",
            "phi-3",
            "mistral",
            "qwen"
        ]

        self.llm_var = tk.StringVar(value=self.llm_options[0])
        self.llm_dropdown = ttk.Combobox(
            llm_frame,
            textvariable=self.llm_var,
            values=self.llm_options,
            state="readonly",
            width=22,
            font=("Helvetica", 10)
        )
        self.llm_dropdown.pack(side=tk.LEFT, padx=10)
        self.llm_dropdown.bind("<<ComboboxSelected>>", self._on_llm_change)

        # Refresh local models (re-query the LiteLLM proxy on demand)
        self.llm_refresh_btn = tk.Button(
            llm_frame, text="↻",
            font=("Helvetica", 10, "bold"),
            bg=self.colors["border"], fg="white",
            command=self._on_llm_refresh
        )
        self.llm_refresh_btn.pack(side=tk.LEFT)

        # LLM status indicator
        self.llm_status_label = tk.Label(
            llm_frame, text="(checking...)",
            font=("Helvetica", 9),
            bg=self.colors["panel_bg"], fg="#888"
        )
        self.llm_status_label.pack(side=tk.LEFT, padx=5)

        # Intent input row
        input_frame = tk.Frame(footer, bg=self.colors["panel_bg"])
        input_frame.pack(fill=tk.X, padx=10, pady=5)

        label = tk.Label(input_frame, text="Intent:",
                        font=("Helvetica", 11),
                        bg=self.colors["panel_bg"], fg=self.colors["fg"])
        label.pack(side=tk.LEFT)

        self.intent_entry = tk.Entry(
            input_frame, font=("Helvetica", 11),
            bg=self.colors["bg"], fg=self.colors["fg"],
            insertbackground=self.colors["fg"],
            width=60
        )
        self.intent_entry.pack(side=tk.LEFT, padx=10)
        self.intent_entry.bind("<Return>", self._on_intent_submit)

        # Buttons
        submit_btn = tk.Button(
            input_frame, text="Submit",
            font=("Helvetica", 10),
            bg=self.colors["accent"], fg="white",
            command=self._on_intent_submit
        )
        submit_btn.pack(side=tk.LEFT, padx=5)

        reset_btn = tk.Button(
            input_frame, text="Reset",
            font=("Helvetica", 10),
            bg=self.colors["warning"], fg="white",
            command=lambda: self._send_command("reset")
        )
        reset_btn.pack(side=tk.LEFT, padx=5)

        status_btn = tk.Button(
            input_frame, text="Status",
            font=("Helvetica", 10),
            bg=self.colors["border"], fg="white",
            command=lambda: self._send_command("status")
        )
        status_btn.pack(side=tk.LEFT, padx=5)

        # Clear graphs button
        clear_btn = tk.Button(
            input_frame, text="Clear Graphs",
            font=("Helvetica", 10),
            bg="#555", fg="white",
            command=self._clear_graphs
        )
        clear_btn.pack(side=tk.LEFT, padx=5)

    def _clear_graphs(self):
        """Clear all graph data"""
        if self.rsrp_graph:
            self.rsrp_graph.clear()
        if self.throughput_graph:
            self.throughput_graph.clear()
        if self.constellation:
            self.constellation.clear()
        self.log("Graphs cleared")

    def _on_intent_submit(self, event=None):
        """Handle intent submission"""
        intent = self.intent_entry.get().strip()
        if intent and self.on_intent_submit:
            self.on_intent_submit(intent)
            self.intent_entry.delete(0, tk.END)

    def _on_llm_change(self, event=None):
        """Handle LLM backend selection change"""
        selected = self.llm_var.get()
        if self.on_llm_change:
            self.on_llm_change(selected)
        self.log(f"LLM backend changed to: {selected}")

    def _on_llm_refresh(self):
        """Re-scan the local LLM server for available models (runs off the UI thread)."""
        if not self.on_llm_refresh:
            return
        self.log("Refreshing local LLM models...")
        threading.Thread(target=self.on_llm_refresh, daemon=True).start()

    def _send_command(self, cmd: str):
        """Send command"""
        if self.on_command:
            self.on_command(cmd)

    def _schedule_update(self):
        """Schedule periodic update"""
        self._process_updates()
        if self.running:
            self.root.after(100, self._schedule_update)

    def _schedule_constellation(self):
        """Re-render the constellation with fresh noise for continuous motion"""
        if self.constellation:
            try:
                self.constellation.tick()
            except Exception as e:
                logger.debug(f"constellation tick failed: {e}")
        if self.running:
            self.root.after(500, self._schedule_constellation)

    def _schedule_status_poll(self):
        """Periodically refresh component status so the GUI reflects reality
        without a manual refresh (single-flight, runs off the GUI thread)."""
        if self.system_controller and not self._status_poll_active:
            self._status_poll_active = True

            def do_poll():
                try:
                    self.system_controller.check_all_status()
                except Exception as e:
                    logger.debug(f"status poll failed: {e}")
                finally:
                    self._status_poll_active = False

            threading.Thread(target=do_poll, daemon=True).start()
        if self.running:
            self.root.after(8000, self._schedule_status_poll)

    def _process_updates(self):
        """Process queued updates"""
        try:
            while True:
                update = self.update_queue.get_nowait()
                self._apply_update(update)
        except queue.Empty:
            pass

    def _apply_update(self, update: GUIUpdate):
        """Apply update to GUI"""
        if update.update_type == "ue_metrics":
            self._update_ue_metrics(update.data)
        elif update.update_type == "state":
            self._update_state(update.data.get("state", "S0"))
        elif update.update_type == "intent":
            self._update_intents(update.data.get("intents", []))
        elif update.update_type == "llm":
            self._update_llm(update.data)
        elif update.update_type == "log":
            self._add_log(update.data.get("message", ""))
        elif update.update_type == "calibration":
            self._update_calibration(update.data)
        elif update.update_type == "confidence":
            self._update_confidence(update.data)
        elif update.update_type == "terminal_outcome":
            self._update_terminal_outcome(update.data.get("outcome"))
        elif update.update_type == "oran_observability":
            self._update_oran_observability(update.data)
        elif update.update_type == "call":
            # Generic closure executed on the GUI thread (thread-safe
            # replacement for root.after() calls from worker threads)
            fn = update.data.get("fn")
            if fn:
                fn()

    def _update_ue_metrics(self, data: Dict):
        """Update UE metrics display and graphs"""
        ue_id = data.get("ue_id", "").upper()
        if ue_id not in self.ue_metric_labels:
            return

        labels = self.ue_metric_labels[ue_id]

        if "attached" in data:
            status = "Connected" if data["attached"] else "Disconnected"
            color = self.colors["success"] if data["attached"] else self.colors["error"]
            labels["Status"].config(text=status, fg=color)

        if data.get("serving_pci") is not None:  # PCI 0 (gNB1) is valid
            labels["PCI"].config(text=str(data["serving_pci"]))

        # Radio KPIs. The collector's schema names every measurement by WHERE it
        # was measured (UE-side DL vs gNB-side UL) - `ue_dl_ss_rsrp_dbm`,
        # `gnb_ul_avg_rsrp_dbm`, ... - so prefer the UE-side DL value (what the
        # paper reports per UE) and fall back to the gNB-side UL measurement,
        # then to the legacy flat keys still emitted by the GUI demo mode.
        # Without this the panels/graphs stay EMPTY even though collection works.
        def _pick(*keys):
            for k in keys:
                v = data.get(k)
                if v is not None:
                    return v
            return None

        rsrp = _pick("ue_dl_ss_rsrp_dbm", "gnb_ul_avg_rsrp_dbm", "rsrp")
        sinr = _pick("ue_dl_sinr_db", "gnb_ul_snr_db", "sinr")
        mcs = _pick("gnb_dl_mcs_index", "mcs")

        if rsrp is not None:
            color = self.colors["success"] if rsrp > -100 else (
                self.colors["warning"] if rsrp > -110 else self.colors["error"]
            )
            labels["RSRP"].config(text=f"{rsrp:.1f} dBm", fg=color)
            # Update graph
            if self.rsrp_graph:
                self.rsrp_graph.update(ue_id, rsrp)

        if sinr is not None:  # 0.0 dB is a valid SINR
            labels["SINR"].config(text=f"{sinr:.1f} dB")

        # Feed the live constellation with this UE's modulation (MCS) + SINR
        if self.constellation:
            self.constellation.set_state(
                ue_id,
                mcs,
                sinr,
                bool(data.get("attached", False)),
            )

        tp_key = "throughput_mbps" if "throughput_mbps" in data else "throughput"
        if data.get(tp_key) is not None:  # 0.0 Mbps is a valid measurement
            tp = data[tp_key]
            color = self.colors["success"] if tp > 8 else (
                self.colors["warning"] if tp > 4 else self.colors["error"]
            )
            labels["Throughput"].config(text=f"{tp:.1f} Mbps", fg=color)
            # Update graph
            if self.throughput_graph:
                self.throughput_graph.update(ue_id, tp)

        lat_key = "latency_ms" if "latency_ms" in data else "latency"
        if lat_key in data and data[lat_key]:
            labels["Latency"].config(text=f"{data[lat_key]:.1f} ms")

    def _update_state(self, state: str):
        """Update state machine visualization"""
        # Reset all states
        for s, widgets in self.state_labels.items():
            color = self.colors["border"]
            widgets["canvas"].itemconfig(widgets["circle"], fill=color, outline=color)

        # Highlight current state
        if state in self.state_labels:
            color = self.state_colors[state]
            widgets = self.state_labels[state]
            widgets["canvas"].itemconfig(widgets["circle"], fill=color, outline=color)

        self.current_state = state

    def _update_intents(self, intents: List[Dict]):
        """Update active intents list"""
        self.intent_listbox.delete(0, tk.END)

        for intent in intents:
            status_icon = {
                "ACTIVE": "●",
                "SATISFIED": "✓",
                "VIOLATED": "✗",
                "NEGOTIATING": "~"
            }.get(intent.get("status", ""), "○")

            text = f"{status_icon} {intent.get('type', 'unknown')}: {intent.get('target_value', '')} ({intent.get('scope', '')})"
            self.intent_listbox.insert(tk.END, text)

    def _update_llm(self, data: Dict):
        """Update LLM analysis display"""
        self.llm_text.delete(1.0, tk.END)

        lines = []
        lines.append(f"Model: {data.get('model', 'unknown')}")
        # P1-3 (blocker 7): show the ACTUAL measured proposal latency; an
        # unmeasured / NaN / Inf / negative latency is n/a, NEVER a fake 0 ms.
        _lat = data.get("latency_ms")
        _lat_ok = (isinstance(_lat, (int, float)) and not isinstance(_lat, bool)
                   and math.isfinite(_lat) and _lat >= 0)
        _lat_str = f"{_lat:.0f}ms" if _lat_ok else "n/a"
        lines.append(f"Latency: {_lat_str}")
        lines.append("")
        lines.append(f"Feasible: {data.get('feasible', False)}")
        lines.append(f"Confidence: {data.get('confidence', 0):.2f}")
        lines.append("")
        lines.append("Reasoning:")
        lines.append(data.get('reasoning', ''))

        if data.get('alternatives'):
            lines.append("")
            lines.append("Alternatives:")
            for alt in data['alternatives']:
                lines.append(f"  - {alt.get('description', '')}")

        self.llm_text.insert(tk.END, "\n".join(lines))

        # Update confidence gauge.  θ* comes from the calibration panel, but its
        # label may hold the PRE_MEASUREMENT placeholder ("-") before any
        # calibration update - float("-") would raise, so fall back to the
        # gauge's current threshold (never a hard-coded 0.7 that could mask a
        # real θ*).
        confidence = data.get('confidence', 0)
        if self.confidence_gauge:
            theta_lbl = self.calib_labels.get("θ*") if self.calib_labels else None
            theta_txt = theta_lbl.cget("text") if theta_lbl is not None else ""
            try:
                theta_star = float(theta_txt)
            except (TypeError, ValueError):
                theta_star = self.confidence_gauge.threshold
            self.confidence_gauge.update(confidence, theta_star)

    def _set_calib(self, key: str, text: str):
        """Set a calibration/guard cell to a REAL measured value (switches its
        colour from the muted placeholder tint to the live foreground)."""
        lbl = self.calib_labels.get(key)
        if lbl is not None:
            lbl.config(text=text, fg=self.colors["fg"])

    def _update_calibration(self, data: Dict):
        """Update the θ* (Eq.17) calibration + budget-guard (Eq.28-30) panels
        from ``calibrator.get_stats()``.  Every read is keyed to a get_stats
        field so a coordinator/GUI key drift leaves the cell at its
        PRE_MEASUREMENT placeholder rather than showing a stale prior."""
        if "theta_star" in data and "θ*" in self.calib_labels:
            self._set_calib("θ*", f"{data['theta_star']:.3f}")
            # Also update gauge threshold
            if self.confidence_gauge:
                self.confidence_gauge.threshold = data['theta_star']
        # Eq.15-17 cost/reward inputs (C_F / R / C_N).
        if "c_worst_estimate" in data:
            self._set_calib("C_F", f"{data['c_worst_estimate']:.1f}")
        if "r_success_estimate" in data:
            self._set_calib("R", f"{data['r_success_estimate']:.1f}")
        if "c_nego_estimate" in data:
            self._set_calib("C_N", f"{data['c_nego_estimate']:.1f}")
        # Eq.17 validity assumption (C_F > C_N): a degenerate θ* if violated.
        if "assumption_ok" in data and "θ* valid" in self.calib_labels:
            ok = bool(data["assumption_ok"])
            lbl = self.calib_labels["θ* valid"]
            lbl.config(text=("yes" if ok else "no"),
                       fg=self.colors["success"] if ok else self.colors["error"])
        # Budget guard (Eq.28-30): the retained SOFT negotiation-round cap
        # (N_a^max) + the running S4 rollback / S5 negotiation counts.
        if "n_max" in data:
            self._set_calib("Nego cap", str(data["n_max"]))
        if "total_rollbacks" in data:
            self._set_calib("Rollbacks", str(data["total_rollbacks"]))
        if "total_negotiations" in data:
            self._set_calib("Negotiations", str(data["total_negotiations"]))

    def _update_terminal_outcome(self, outcome):
        """Highlight the Eq.12 terminal state (Admitted / NotAdmitted /
        TechnicalFailsafe) of the LAST finalized episode.  ``outcome`` is a
        coordinator TerminalOutcome value/name (or a raw ``terminal_outcome``
        field); an unrecognized value clears the row (never a fabricated
        outcome)."""
        state = terminal_state_label(outcome)
        self.terminal_state = state
        labels = getattr(self, "terminal_labels", None)
        if not labels:
            return
        for name, lbl in labels.items():
            if name == state:
                lbl.config(bg=TERMINAL_STATE_COLORS[name], fg="white")
            else:
                lbl.config(bg=self.colors["border"], fg="#bbbbbb")

    def _update_oran_observability(self, data: Dict):
        labels = getattr(self, "oran_labels", {})
        for key in ("policyLifecycle", "negotiation", "assurance", "evidence"):
            if key in data and key in labels:
                labels[key].config(text=str(data[key]))

    def _update_confidence(self, data: Dict):
        """Update confidence gauge directly"""
        if self.confidence_gauge:
            self.confidence_gauge.update(
                data.get('confidence', 0),
                data.get('threshold')
            )

    def _add_log(self, message: str):
        """Add message to event log"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)

    # Public API for updates from other threads

    def update_ue_metrics(self, ue_id: str, data: Dict):
        """Thread-safe UE metrics update"""
        data["ue_id"] = ue_id
        self.update_queue.put(GUIUpdate("ue_metrics", data))

    def update_state(self, state: str):
        """Thread-safe state update"""
        self.update_queue.put(GUIUpdate("state", {"state": state}))

    def update_intents(self, intents: List[Dict]):
        """Thread-safe intents update"""
        self.update_queue.put(GUIUpdate("intent", {"intents": intents}))

    def update_llm_analysis(self, data: Dict):
        """Thread-safe LLM analysis update"""
        self.update_queue.put(GUIUpdate("llm", data))

    def update_calibration(self, data: Dict):
        """Thread-safe calibration update"""
        self.update_queue.put(GUIUpdate("calibration", data))

    def update_terminal_outcome(self, outcome):
        """Thread-safe Eq.12 terminal-outcome update.  ``outcome`` may be a
        coordinator ``TerminalOutcome`` enum, its ``.value`` string, or a raw
        result's ``terminal_outcome`` field."""
        self.update_queue.put(GUIUpdate("terminal_outcome", {"outcome": outcome}))

    def update_oran_observability(self, **values):
        """Queue display-only A1 lifecycle/negotiation/assurance evidence."""
        self.update_queue.put(GUIUpdate("oran_observability", dict(values)))

    def update_confidence(self, confidence: float, threshold: float = None):
        """Thread-safe confidence gauge update"""
        self.update_queue.put(GUIUpdate("confidence", {
            "confidence": confidence,
            "threshold": threshold
        }))

    def log(self, message: str):
        """Thread-safe log message"""
        self.update_queue.put(GUIUpdate("log", {"message": message}))

    def update_llm_status(self, available_backends: list, current_backend: str = None):
        """Update LLM backend status in GUI"""
        def update():
            if current_backend:
                self.llm_var.set(current_backend)

            if available_backends:
                status_text = f"({len(available_backends)} available)"
                self.llm_status_label.config(text=status_text, fg=self.colors["success"])
            else:
                self.llm_status_label.config(text="(none available)", fg=self.colors["error"])

        # Queued so updates sent before the window exists are not dropped
        self.update_queue.put(GUIUpdate("call", {"fn": update}))

    def set_llm_options(self, options: list, current: str = None):
        """Set available LLM options in dropdown"""
        def update():
            self.llm_dropdown['values'] = options
            if current and current in options:
                self.llm_var.set(current)
            elif options:
                self.llm_var.set(options[0])

        self.update_queue.put(GUIUpdate("call", {"fn": update}))

    def run(self):
        """Start GUI main loop"""
        self.running = True
        self.create_window()
        self.root.mainloop()
        self.running = False

    def stop(self):
        """Stop GUI"""
        self.running = False
        if self.root:
            self.root.quit()


def main():
    """Test GUI with simulated data"""
    gui = IntentCoordinatorGUI(title="Agentic Intent Coordinator - Demo")

    def test_updates():
        """Simulate real-time updates"""
        import random
        time.sleep(1)

        # Initial connection
        gui.update_ue_metrics("UE1", {
            "attached": True,
            "serving_pci": 1,
            "rsrp": -85.0,
            "sinr": 15.0,
            "throughput": 12.0,
            "latency": 25.0
        })
        gui.update_ue_metrics("UE2", {
            "attached": True,
            "serving_pci": 1,
            "rsrp": -92.0,
            "sinr": 10.0,
            "throughput": 8.0,
            "latency": 30.0
        })
        gui.log("UE1 and UE2 connected")

        # Populate the θ*/budget-guard panels with a representative
        # get_stats()-shaped payload so the placeholders ("-") resolve to live
        # values (demo only - real values arrive from calibrator.get_stats()).
        gui.update_calibration({
            "theta_star": 0.63, "assumption_ok": True,
            "c_worst_estimate": 118.0, "r_success_estimate": 61.0,
            "c_nego_estimate": 47.0, "n_max": 2,
            "total_rollbacks": 0, "total_negotiations": 0,
        })

        # Simulate state transitions with data updates.  The extra
        # S_TECHNICAL_FAILSAFE step exercises the failsafe sink node + the
        # TechnicalFailsafe terminal indicator, then RESET returns to S0.
        states = ["S0", "S1", "S2", "S3", "S4", "S6",
                  "S1", "S2", "S_TECHNICAL_FAILSAFE", "S0"]
        state_msgs = [
            "Normal operation",
            "Conflict screening started",
            "LLM analysis in progress",
            "Trial execution",
            "Validation in progress",
            "Resolution: ACCEPT (Admitted)",
            "New intent - conflict screening",
            "LLM analysis in progress",
            "Broken invariant - fail-closed to TechnicalFailsafe",
            "Recovered - back to normal",
        ]
        # Eq.12 terminal outcome emitted at each episode boundary.
        term_at = {"S6": "commit_original",
                   "S_TECHNICAL_FAILSAFE": "technical_failsafe"}

        for i, (state, msg) in enumerate(zip(states, state_msgs)):
            time.sleep(1.5)
            gui.update_state(state)
            gui.log(msg)
            if state in term_at:
                gui.update_terminal_outcome(term_at[state])

            # LLM analysis at S2
            if state == "S2":
                gui.update_llm_analysis({
                    "model": "claude-sonnet",
                    "latency_ms": 850,
                    "feasible": True,
                    "confidence": 0.85,
                    "reasoning": "Both intents can be satisfied by adjusting BS2 power offset. UE1 throughput will remain above 10Mbps.",
                    "alternatives": [
                        {"description": "Handover UE1 to BS2"},
                        {"description": "Reduce throughput target to 6 Mbps"}
                    ]
                })

            # Simulate KPI changes
            rsrp1 = -85 + random.uniform(-3, 3) - (i * 2 if i < 4 else (8 - i) * 2)
            rsrp2 = -92 + random.uniform(-2, 2)
            tp1 = 12 + random.uniform(-2, 2) - (i * 1.5 if i < 4 else (8 - i) * 1.5)
            tp2 = 8 + random.uniform(-1, 1)

            gui.update_ue_metrics("UE1", {
                "rsrp": rsrp1,
                "throughput": max(0, tp1)
            })
            gui.update_ue_metrics("UE2", {
                "rsrp": rsrp2,
                "throughput": max(0, tp2)
            })

        # Continue updating graphs
        for _ in range(30):
            time.sleep(0.5)
            rsrp1 = -85 + random.uniform(-3, 3)
            rsrp2 = -92 + random.uniform(-2, 2)
            tp1 = 12 + random.uniform(-2, 2)
            tp2 = 8 + random.uniform(-1, 1)

            gui.update_ue_metrics("UE1", {"rsrp": rsrp1, "throughput": tp1})
            gui.update_ue_metrics("UE2", {"rsrp": rsrp2, "throughput": tp2})

    thread = threading.Thread(target=test_updates, daemon=True)
    thread.start()

    gui.run()


if __name__ == "__main__":
    main()
