"""The console window: theme, workspace notebook, and the drain loop.

This is the only place in the console that owns a Tk main loop, and the only
place that schedules the periodic bus drain.  Two properties matter more than
the widget code:

* **The Tk thread does no I/O.**  It drains the bus, repaints, and returns.
  Everything else - coordinator episodes, R1 reads, replay loading, export -
  belongs to a worker thread that publishes into the bus.
* **The tick is measured, not assumed.**  :class:`TickMonitor` records the
  interval actually achieved and the drain cost, which is what the
  responsiveness gate and the soak harness read.  A console that claims to stay
  responsive without measuring it is claiming, not proving.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import tokens
from ..viewmodel.bus import StateBus
from ..workspaces import Workspace


@dataclass
class TickStats:
    """What one soak or one responsiveness assertion reads."""

    ticks: int = 0
    dispatched: int = 0
    max_interval_ms: float = 0.0
    mean_interval_ms: float = 0.0
    max_drain_ms: float = 0.0
    mean_drain_ms: float = 0.0
    queue_high_water: int = 0

    def as_dict(self) -> Dict[str, float]:
        return {"ticks": self.ticks, "dispatched": self.dispatched,
                "maxIntervalMs": round(self.max_interval_ms, 3),
                "meanIntervalMs": round(self.mean_interval_ms, 3),
                "maxDrainMs": round(self.max_drain_ms, 3),
                "meanDrainMs": round(self.mean_drain_ms, 3),
                "queueHighWater": self.queue_high_water}


class TickMonitor:
    """Bounded ring buffer of drain-loop timings.

    Bounded on purpose: a thirty-minute soak at 10 Hz is eighteen thousand
    ticks, and a monitor that grew without bound would be measuring its own leak
    as much as the console's.
    """

    def __init__(self, *, capacity: int = 4096) -> None:
        self._capacity = max(16, int(capacity))
        self._intervals: List[float] = []
        self._drains: List[float] = []
        self._last_start: Optional[float] = None
        self.ticks = 0
        self.dispatched = 0
        self.max_interval_ms = 0.0
        self.max_drain_ms = 0.0
        self.queue_high_water = 0

    def record(self, *, start: float, end: float, dispatched: int,
               depth: int = 0) -> None:
        """Record one tick.  ``start``/``end`` are monotonic seconds."""
        if self._last_start is not None:
            interval_ms = (start - self._last_start) * 1000.0
            self._intervals.append(interval_ms)
            if len(self._intervals) > self._capacity:
                del self._intervals[:len(self._intervals) - self._capacity]
            self.max_interval_ms = max(self.max_interval_ms, interval_ms)
        self._last_start = start
        drain_ms = (end - start) * 1000.0
        self._drains.append(drain_ms)
        if len(self._drains) > self._capacity:
            del self._drains[:len(self._drains) - self._capacity]
        self.max_drain_ms = max(self.max_drain_ms, drain_ms)
        self.ticks += 1
        self.dispatched += int(dispatched)
        self.queue_high_water = max(self.queue_high_water, int(depth))

    def stats(self) -> TickStats:
        mean = (sum(self._intervals) / len(self._intervals)
                if self._intervals else 0.0)
        drain_mean = (sum(self._drains) / len(self._drains)
                      if self._drains else 0.0)
        return TickStats(ticks=self.ticks, dispatched=self.dispatched,
                         max_interval_ms=self.max_interval_ms,
                         mean_interval_ms=mean, max_drain_ms=self.max_drain_ms,
                         mean_drain_ms=drain_mean,
                         queue_high_water=self.queue_high_water)


class ConsoleWindow:
    """Root window, workspace notebook and the periodic drain.

    Constructed without a toolkit; ``create()`` is the first call that touches
    Tk.  That split is what lets the console be assembled and inspected in a
    hermetic test.
    """

    def __init__(self, bus: StateBus, *, title: str = "Operator Console",
                 theme: str = tokens.DEFAULT_THEME,
                 drain_interval_ms: int = tokens.BUS_DRAIN_INTERVAL_MS,
                 drain_budget: int = 256) -> None:
        self.bus = bus
        self.title = title
        self.theme = theme
        self.drain_interval_ms = max(10, int(drain_interval_ms))
        self.drain_budget = max(1, int(drain_budget))
        self.root = None
        self.notebook = None
        self.header = None
        self.cockpit_header = None
        self.footer = None
        self.monitor = TickMonitor()
        self._palette = tokens.theme(theme)
        self._workspaces: List[Workspace] = []
        self._frames: Dict[str, Any] = {}
        self._active: Optional[str] = None
        self._tick_hooks: List[Callable[[], None]] = []
        self._closing = False
        self._fullscreen = False
        self._restore_workspace = None

    # -- assembly ----------------------------------------------------------- #

    def add_workspace(self, workspace: Workspace) -> None:
        self._workspaces.append(workspace)

    def add_tick_hook(self, hook: Callable[[], None]) -> None:
        """Run ``hook`` after every drain.  Tk thread; must return quickly."""
        self._tick_hooks.append(hook)

    @property
    def workspaces(self) -> Tuple[Workspace, ...]:
        return tuple(self._workspaces)

    def create(self, *, header=None, footer=None, cockpit_header=None):
        """Build the real window.  The first call that imports the toolkit.

        ``cockpit_header`` is task section 9's always-visible row.  It is a
        second bar rather than a replacement for ``header``: the two answer
        different questions -- what this console is doing, and what the Kernel
        is doing -- and a build that had only one of them would drop the other.
        """
        import tkinter as tk
        from tkinter import ttk

        self.root = tk.Tk()
        self.root.title(self.title)
        width, height = tokens.TARGET_WINDOW
        min_w, min_h = tokens.MIN_WINDOW
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min_w, min_h)
        self.root.configure(bg=self._palette["bg"])
        self._apply_style(ttk)

        self.header = header
        if header is not None:
            header.build(self.root)
            header.frame.pack(fill="x", side="top")

        self.cockpit_header = cockpit_header
        if cockpit_header is not None:
            cockpit_header.build(self.root)
            cockpit_header.frame.pack(fill="x", side="top")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True,
                           padx=tokens.SPACING["tight"],
                           pady=tokens.SPACING["tight"])
        for workspace in self._workspaces:
            frame = ttk.Frame(self.notebook)
            self.notebook.add(frame, text=workspace.title)
            workspace.build(frame)
            self._frames[workspace.id] = frame
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.footer = footer
        if footer is not None:
            footer.build(self.root)
            footer.frame.pack(fill="x", side="bottom")

        self.root.bind("<F11>", lambda _event: self.toggle_fullscreen())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if self._workspaces:
            self._active = self._workspaces[0].id
            self._workspaces[0].on_activate()
        return self.root

    def _apply_style(self, ttk) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        palette = self._palette
        style.configure(".", background=palette["bg"], foreground=palette["fg"],
                        fieldbackground=palette["panel_alt_bg"],
                        bordercolor=palette["border"],
                        font=tokens.font("body"))
        style.configure("TNotebook", background=palette["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=palette["panel_bg"],
                        foreground=palette["fg"],
                        padding=(tokens.SPACING["loose"], tokens.SPACING["tight"]),
                        font=tokens.font("label", bold=True))
        style.map("TNotebook.Tab",
                  background=[("selected", palette["accent"])],
                  foreground=[("selected", palette["accent_fg"])])
        style.configure("TFrame", background=palette["bg"])
        style.configure("TLabel", background=palette["bg"],
                        foreground=palette["fg"])
        style.configure("Treeview", background=palette["panel_bg"],
                        fieldbackground=palette["panel_bg"],
                        foreground=palette["fg"], rowheight=20,
                        font=tokens.font("small"))
        style.configure("Treeview.Heading", background=palette["panel_alt_bg"],
                        foreground=palette["fg"],
                        font=tokens.font("small", bold=True))

    # -- workspace switching ------------------------------------------------ #

    def _on_tab_changed(self, _event=None) -> None:
        if self.notebook is None:
            return
        try:
            index = self.notebook.index(self.notebook.select())
        except Exception:
            return
        if index >= len(self._workspaces):
            return
        target = self._workspaces[index]
        if self._active == target.id:
            return
        for workspace in self._workspaces:
            if workspace.id == self._active:
                _quietly(workspace.on_deactivate)
        self._active = target.id
        _quietly(target.on_activate)

    def select(self, workspace_id: str) -> bool:
        """Switch to ``workspace_id``.  Returns False if it is not registered."""
        for index, workspace in enumerate(self._workspaces):
            if workspace.id == workspace_id:
                if self.notebook is not None:
                    self.notebook.select(index)
                else:
                    self._active = workspace_id
                return True
        return False

    @property
    def active_workspace(self) -> Optional[str]:
        return self._active

    def toggle_fullscreen(self, *, present: bool = True) -> bool:
        """F11: full screen, and on the way in, the Demo View.

        Wired at integration.  ``docs/phase-b-gui/OPERATOR-GUIDE.md`` and
        ``REPLAY-AND-DEMO.md`` both describe F11 as the presentation key - the
        thing a speaker presses when the projector is live - and a full-screen
        Settings pane is not that.  Entering therefore selects the Demo View and
        remembers where the operator was; leaving puts them back, which is what
        makes "step away to a detail screen and come back" cost nothing.

        ``present=False`` toggles the window only, for a caller that wants the
        geometry without the workspace change.
        """
        self._fullscreen = not self._fullscreen
        if present and self._workspaces:
            if self._fullscreen:
                self._restore_workspace = self._active
                if any(w.id == "demo" for w in self._workspaces):
                    self.select("demo")
            elif self._restore_workspace:
                self.select(self._restore_workspace)
                self._restore_workspace = None
        if self.root is not None:
            try:
                self.root.attributes("-fullscreen", self._fullscreen)
            except Exception:
                self._fullscreen = False
        return self._fullscreen

    # -- the drain loop ----------------------------------------------------- #

    def tick(self) -> int:
        """One drain cycle.  Returns the number of records dispatched.

        Separated from the scheduling so a test - and the soak harness - can run
        the exact production cycle without a main loop.
        """
        import time

        start = time.monotonic()
        dispatched = 0
        try:
            dispatched = self.bus.drain(budget=self.drain_budget)
        except Exception:
            # The drain loop is the console's heartbeat.  It degrades; it never
            # stops, because a stopped heartbeat freezes the whole window.
            dispatched = 0
        for hook in tuple(self._tick_hooks):
            _quietly(hook)
        end = time.monotonic()
        self.monitor.record(start=start, end=end, dispatched=dispatched,
                            depth=self.bus.depth())
        return dispatched

    def _schedule(self) -> None:
        if self.root is None or self._closing:
            return
        self.tick()
        self.root.after(self.drain_interval_ms, self._schedule)

    def run(self) -> None:
        if self.root is None:
            raise RuntimeError("create() must run before run()")
        self._schedule()
        self.root.mainloop()

    def close(self) -> None:
        self._closing = True
        if self.root is not None:
            try:
                self.root.destroy()
            except Exception:
                pass

    # -- persisted cosmetic state ------------------------------------------- #

    def gui_state(self) -> Dict[str, Any]:
        state: Dict[str, Any] = {"activeWorkspace": self._active,
                                 "theme": self.theme,
                                 "fullscreen": self._fullscreen,
                                 "workspaces": {}}
        for workspace in self._workspaces:
            try:
                state["workspaces"][workspace.id] = dict(workspace.gui_state())
            except Exception:
                state["workspaces"][workspace.id] = {}
        return state

    def restore_gui_state(self, state) -> None:
        data = dict(state or {})
        per_workspace = dict(data.get("workspaces") or {})
        for workspace in self._workspaces:
            saved = per_workspace.get(workspace.id)
            if saved:
                _quietly(workspace.restore_gui_state, saved)
        active = data.get("activeWorkspace")
        if active:
            self.select(active)


def _quietly(call: Callable, *args: Any) -> None:
    """Run ``call`` and swallow anything it raises.

    Presentation is downstream and powerless - the same rule ``run_gui_once``
    applies to a broken presenter.  A workspace that fails to repaint must not
    be able to stop the drain loop for every other workspace.
    """
    try:
        call(*args)
    except Exception:
        import logging

        logging.getLogger("gui.operator.window").exception(
            "console callback failed: %r", getattr(call, "__name__", call))


__all__ = ["ConsoleWindow", "TickMonitor", "TickStats", "Workspace"]
