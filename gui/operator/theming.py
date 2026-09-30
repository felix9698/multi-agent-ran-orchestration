"""Display-theme toggle: dark or light, one corner control beside Language.

Same shape and the same rules as :mod:`gui.operator.i18n`, for the same
reason: this is a **display** control and nothing else.  It changes widget
colours and the ttk styles behind them; it never changes a value, a status, a
record, an export or what any workspace decides.  Status colours move to the
same status's colour in the other palette, so a green stays green and a
degraded amber stays amber -- a theme that changed which status a colour meant
would be a lie the palette told.

How it works, and why this way:

* The console's widgets are painted from :func:`gui.operator.tokens.theme`,
  so every colour on screen is one of eleven palette values.  Switching is
  therefore a **reverse lookup**: walk the tree, and wherever an option holds
  a value from the old palette, write the same key's value from the new one.
  Nothing else is touched, and an unrecognised colour is left exactly as it
  is -- the same rule i18n uses for text it does not know.
* Background options and foreground options are looked up in **separate**
  maps.  They have to be: ``#ffffff`` is the dark palette's ``fg`` and the
  light palette's ``bg``, so one shared map would turn white text into a
  white background.  The role of the option decides which map applies.
* ttk widgets ignore ``configure(bg=...)`` entirely -- they are drawn by
  styles -- so the same palette is written into the handful of ttk styles the
  console actually uses.  Before this module existed, no ttk style was
  configured at all, which is why comboboxes and tables stayed platform-pale
  inside a dark console.
* The matplotlib charts are deliberately **not** re-themed.  They never read
  the palette (``TimeSeriesChart.refresh`` draws on the matplotlib default),
  and a figure that prints on white is what a paper wants.

``refresh`` is registered as a tick hook: a workspace that builds a widget
after the switch paints it in the theme it was constructed with, and the next
tick converts it.  That makes the walk idempotent by construction -- a second
pass finds no old-palette values left to replace.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

from . import status as st
from . import tokens

DARK = "dark"
LIGHT = "light"
THEMES: Tuple[str, ...] = (DARK, LIGHT)

#: Palette keys that name a background.  Kept apart from the foreground keys
#: because the two palettes share hex values across roles.
BACKGROUND_KEYS: Tuple[str, ...] = (
    "bg", "panel_bg", "panel_alt_bg", "selection", "accent", "border", "grid")

#: Palette keys that name a foreground.  ``accent_fg`` is deliberately absent:
#: in the dark palette it is ``#ffffff``, the same hex as ``fg``, so including
#: it would overwrite the entry that turns white body text dark -- white text
#: would stay white on a white background, which is the one failure that makes
#: a theme switch useless.  Text that really sits on the accent is handled by
#: the on-accent rule in :meth:`ThemeSwitch._repaint` instead.
FOREGROUND_KEYS: Tuple[str, ...] = ("fg", "fg_muted", "fg_inverse")

#: Widget options that hold a background colour, and those that hold a
#: foreground.  ``insertbackground`` is the text caret, which is a foreground
#: despite its name.
BACKGROUND_OPTIONS: Tuple[str, ...] = (
    "background", "highlightbackground", "selectbackground",
    "activebackground", "disabledbackground", "troughcolor",
    "highlightcolor", "readonlybackground")
FOREGROUND_OPTIONS: Tuple[str, ...] = (
    "foreground", "selectforeground", "activeforeground",
    "disabledforeground", "insertbackground")

#: The ttk styles the console puts on screen.  Each entry is
#: ``style name -> (option, palette key)`` pairs.
TTK_STYLES: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...] = (
    ("TFrame", (("background", "bg"),)),
    ("TLabel", (("background", "bg"), ("foreground", "fg"))),
    ("TLabelframe", (("background", "bg"), ("foreground", "fg"))),
    ("TLabelframe.Label", (("background", "bg"), ("foreground", "fg"))),
    ("TPanedwindow", (("background", "bg"),)),
    ("TNotebook", (("background", "bg"),)),
    ("TNotebook.Tab", (("background", "panel_bg"), ("foreground", "fg"))),
    ("TCombobox", (("fieldbackground", "panel_bg"), ("background", "bg"),
                   ("foreground", "fg"), ("arrowcolor", "fg"))),
    ("TEntry", (("fieldbackground", "panel_bg"), ("foreground", "fg"))),
    ("Treeview", (("background", "panel_bg"), ("fieldbackground", "panel_bg"),
                  ("foreground", "fg"))),
    ("Treeview.Heading", (("background", "bg"), ("foreground", "fg_muted"))),
    ("Main.TCombobox", (("fieldbackground", "panel_bg"), ("background", "bg"),
                        ("foreground", "fg"), ("arrowcolor", "fg"))),
    ("Main.Treeview", (("background", "panel_bg"),
                       ("fieldbackground", "panel_bg"), ("foreground", "fg"))),
    ("Main.Treeview.Heading", (("background", "bg"),
                               ("foreground", "fg_muted"))),
)


def colour_maps(old: str, new: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """``(backgrounds, foregrounds)`` mapping *old* palette values to *new*.

    Status colours join the foreground map: they are drawn as text and as
    badge glyphs, and they must keep meaning the same thing.
    """
    before, after = tokens.theme(old), tokens.theme(new)
    backgrounds = {before[key]: after[key] for key in BACKGROUND_KEYS
                   if key in before and key in after}
    foregrounds = {before[key]: after[key] for key in FOREGROUND_KEYS
                   if key in before and key in after}
    for name in sorted(getattr(st, "STATUS_SPECS", {}) or {}):
        try:
            before_colour = tokens.status_color(name, old)
            after_colour = tokens.status_color(name, new)
        except Exception:
            continue
        # Never let a status colour displace a palette entry: if a status
        # happens to be drawn in the same hex as body text, the body-text
        # mapping is the one that must survive.
        foregrounds.setdefault(before_colour, after_colour)
    return backgrounds, foregrounds


class ThemeSwitch:
    """The corner control and the walker that repaints the tree."""

    def __init__(self, *, theme: str = tokens.DEFAULT_THEME,
                 on_change: Optional[Any] = None) -> None:
        #: The theme widgets are *authored* in.  Anything a workspace builds
        #: later is painted in this one, whatever is currently selected, which
        #: is exactly what the tick hook exists to fix up.
        self.authored = theme if theme in THEMES else DARK
        self.theme = self.authored
        self.control = None
        self._root = None
        self._var = None
        self._on_change = on_change

    # -- assembly ------------------------------------------------------------ #

    def attach(self, window) -> Optional[object]:
        """Add the toggle to ``window``'s header, beside the Language box.

        Returns ``None`` when the window has no header -- a console assembled
        without one still switches themes programmatically, there is simply no
        widget, which is how ``LanguageSwitch`` behaves too.
        """
        import tkinter as tk

        self._root = window.root
        header = getattr(window, "header", None)
        host = getattr(header, "frame", None)
        if host is None:
            return None
        palette = tokens.theme(self.theme)
        cell = tk.Frame(host, bg=palette["panel_bg"])
        column = host.grid_size()[0]
        cell.grid(row=0, column=column, sticky="ne",
                  padx=tokens.SPACING["tight"], pady=tokens.SPACING["hair"])
        tk.Label(cell, text="Theme", anchor="e", bg=palette["panel_bg"],
                 fg=palette["fg_muted"], font=tokens.font("micro")).pack(fill="x")
        self._var = tk.StringVar(value=self._label())
        button = tk.Button(cell, textvariable=self._var, width=7,
                           command=self.toggle, font=tokens.font("micro"))
        button.pack(anchor="e")
        self.control = button
        return button

    def _label(self) -> str:
        # The button says what is on screen now, not what pressing it does:
        # a control labelled with its own consequence reads as a state readout
        # to half of everyone who sees it, and they are never sure which.
        return "Dark" if self.theme == DARK else "Light"

    # -- switching ------------------------------------------------------------ #

    def toggle(self) -> None:
        self.set_theme(LIGHT if self.theme == DARK else DARK)

    def set_theme(self, theme: str) -> None:
        if theme not in THEMES:
            raise ValueError(f"unknown theme: {theme!r}")
        previous, self.theme = self.theme, theme
        if self._var is not None:
            self._var.set(self._label())
        if previous != theme and self._root is not None:
            self._apply_ttk()
            self._walk(self._root, *colour_maps(previous, theme))
        if self._on_change is not None and previous != theme:
            try:
                self._on_change(theme)
            except Exception:
                pass

    def refresh(self) -> None:
        """Tick hook.  Free while the selected theme is the authored one."""
        if self.theme == self.authored or self._root is None:
            return
        self._walk(self._root, *colour_maps(self.authored, self.theme))

    # -- the walker ----------------------------------------------------------- #

    def _apply_ttk(self) -> None:
        try:
            from tkinter import ttk
        except Exception:                                    # pragma: no cover
            return
        palette = tokens.theme(self.theme)
        try:
            style = ttk.Style()
        except Exception:
            return
        for name, pairs in TTK_STYLES:
            settings = {option: palette[key] for option, key in pairs
                        if key in palette}
            try:
                style.configure(name, **settings)
            except Exception:
                continue
        # A selected row has to stay legible in either palette, and the map()
        # entries are not reached by configure().
        for name in ("Treeview", "Main.Treeview"):
            try:
                style.map(name,
                          background=[("selected", palette["selection"])],
                          foreground=[("selected", palette["fg"])])
            except Exception:
                continue

    def _walk(self, widget, backgrounds: Mapping[str, str],
              foregrounds: Mapping[str, str]) -> None:
        self._repaint(widget, backgrounds, foregrounds)
        try:
            children = widget.winfo_children()
        except Exception:
            return
        for child in children:
            self._walk(child, backgrounds, foregrounds)

    @staticmethod
    def _repaint(widget, backgrounds: Mapping[str, str],
                 foregrounds: Mapping[str, str]) -> None:
        try:
            options = set(widget.keys())
        except Exception:
            return
        # One case the value maps cannot resolve on their own: in the dark
        # palette ``fg`` and ``accent_fg`` are both #ffffff, so white text on
        # an accent background would be rewritten to the light palette's dark
        # ``fg`` and vanish into the blue.  Text sitting on the accent keeps
        # its own colour.
        on_accent = False
        if "background" in options:
            try:
                on_accent = str(widget.cget("background")) in (
                    tokens.theme(DARK)["accent"], tokens.theme(LIGHT)["accent"])
            except Exception:
                on_accent = False
        for option in BACKGROUND_OPTIONS:
            if option not in options:
                continue
            _swap(widget, option, backgrounds)
        if on_accent:
            return
        # Read the background back AFTER the swaps: what matters to legibility
        # is what is behind the text now, not what was there a moment ago.
        behind = None
        if "background" in options:
            try:
                behind = str(widget.cget("background"))
            except Exception:
                behind = None
        for option in FOREGROUND_OPTIONS:
            if option not in options:
                continue
            _swap(widget, option, foregrounds, avoid=behind)


def _swap(widget, option: str, mapping: Mapping[str, str],
          *, avoid: Optional[str] = None) -> None:
    """Rewrite one colour option, unless doing so would hide the text.

    ``avoid`` is the colour now behind this widget.  A pane that paints itself
    from its own palette rather than from the tokens -- the Demo View is one,
    it is deliberately a fixed presentation surface -- has a background this
    module does not recognise and therefore does not move.  Rewriting its
    foreground anyway is how white-on-#111111 became #111111-on-#111111, which
    is a label that has silently vanished.  A colour that would collide with
    what is behind it is left exactly as it was: worse-matched, still legible.
    """
    try:
        current = str(widget.cget(option))
    except Exception:
        return
    replacement = mapping.get(current)
    if replacement is None or replacement == current:
        return
    if avoid is not None and replacement == avoid:
        return
    try:
        widget.configure(**{option: replacement})
    except Exception:
        # A widget that refuses one option is not a reason to stop repainting
        # the rest of the tree, and a half-painted console is still readable.
        pass


__all__ = ["BACKGROUND_KEYS", "BACKGROUND_OPTIONS", "DARK", "FOREGROUND_KEYS",
           "FOREGROUND_OPTIONS", "LIGHT", "THEMES", "TTK_STYLES", "ThemeSwitch",
           "colour_maps"]
