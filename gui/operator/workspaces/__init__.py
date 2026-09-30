"""Workspaces: the panes of the Research Operations Cockpit.

This package holds the one interface every workspace implements, and nothing
else.  Keeping the protocol here rather than in the shell means a workspace
module never has to import the window in order to describe itself - which is
what lets the four implementation tracks build their workspaces independently.

The contract in one line: **a workspace reads a**
:class:`~gui.operator.viewmodel.types.SessionState` **and nothing else.**  It
never calls a source, a store or an exporter, because the Tk thread it runs on
does no I/O.  A workspace that needs something not on the state asks for the
field to be added rather than reaching around the boundary.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable


@runtime_checkable
class Workspace(Protocol):
    """Every workspace implements exactly this.

    ``on_state`` runs on the Tk thread and must return quickly; a workspace that
    needs to compute reaches for a worker and publishes the result instead.
    """

    id: str
    title: str

    def build(self, parent) -> None: ...
    def on_state(self, state) -> None: ...
    def on_activate(self) -> None: ...
    def on_deactivate(self) -> None: ...
    def gui_state(self) -> Mapping[str, Any]: ...
    def restore_gui_state(self, state: Mapping[str, Any]) -> None: ...


class PlaceholderWorkspace:
    """Stands in for a workspace module that is not present in this build.

    The console is assembled by four parallel tracks, so a build can legitimately
    be missing a pane.  The honest rendering of that is a tab that says which
    module is absent - not a hidden tab, and certainly not an empty pane that
    reads as "nothing to report".  It follows the same rule as an unsupported
    metric: stay visible, state the reason.
    """

    def __init__(self, workspace_id: str, title: str, reason: str) -> None:
        self.id = workspace_id
        self.title = title
        self.reason = reason
        self.frame = None

    def build(self, parent) -> None:
        import tkinter as tk

        from .. import status as st
        from .. import tokens

        palette = tokens.theme()
        self.frame = tk.Frame(parent, bg=palette["bg"])
        self.frame.pack(fill="both", expand=True)
        glyph = st.resolve(st.UNAVAILABLE).glyph
        tk.Label(self.frame,
                 text=f"{glyph} {self.title} is not available in this build",
                 bg=palette["bg"], fg=palette["fg"],
                 font=tokens.font("subhead", bold=True)).pack(
                     anchor="w", padx=tokens.SPACING["section"],
                     pady=(tokens.SPACING["section"], tokens.SPACING["tight"]))
        tk.Label(self.frame, text=self.reason, justify="left", anchor="w",
                 bg=palette["bg"], fg=palette["fg_muted"],
                 font=tokens.font("body")).pack(
                     anchor="w", padx=tokens.SPACING["section"])

    def on_state(self, state) -> None:
        return None

    def on_activate(self) -> None:
        return None

    def on_deactivate(self) -> None:
        return None

    def gui_state(self) -> Mapping[str, Any]:
        return {}

    def restore_gui_state(self, state: Mapping[str, Any]) -> None:
        return None


#: Task section 9's eight Cockpit workspaces, in the order that section lists
#: them.  Declared here as data so "the Cockpit has these eight" is a test
#: rather than a claim, and so a build missing one still starts and says which.
COCKPIT_WORKSPACES: Sequence[tuple] = (
    ("live_ops", "Live Operations"),
    ("contract_studio", "Contract Studio"),
    ("trial_safety", "Trial & Safety"),
    ("evidence_ledger", "Evidence Ledger"),
    ("batch_experiments", "Batch Experiments"),
    ("analysis", "Analysis & Results"),
    ("demo", "Demo View"),
    ("settings", "Settings & Integration"),
)

#: The workspaces this build offers, in tab order, with the module that owns
#: each.  The console imports them by name so a build missing one still starts -
#: and says which one is missing.
#:
#: The eight of :data:`COCKPIT_WORKSPACES` are all present.  Two more sit
#: alongside them rather than inside them, and both are section 9.5's "preserve
#: and relocate the existing valid functions": ``intent_decision`` carries the
#: intent history, the commercial/local model selector and the multi-stage
#: reasoning visibility that the legacy console had, and
#: ``objective_registry`` is Gate 4's honest per-objective projection.  Folding
#: either into one of the eight would have meant deleting a working surface to
#: make a list shorter.
WORKSPACE_MODULES: Sequence[tuple] = (
    # First, and therefore the tab the shell opens on.  It is not a ninth
    # surface: it is the shallow one-screen view of five things the other
    # panes already own deeply, for an operator mid-sitting and for a figure
    # that has to show the console working in a single capture.
    ("main", "gui.operator.workspaces.main", "MainWorkspace", "Main"),
    ("live_ops", "gui.operator.workspaces.live_ops",
     "LiveOperationsWorkspace", "Live Operations"),
    ("contract_studio", "gui.operator.workspaces.contract_studio",
     "ContractStudioWorkspace", "Contract Studio"),
    ("trial_safety", "gui.operator.workspaces.trial_safety",
     "TrialSafetyWorkspace", "Trial & Safety"),
    ("evidence_ledger", "gui.operator.workspaces.evidence_ledger",
     "EvidenceLedgerWorkspace", "Evidence Ledger"),
    ("batch_experiments", "gui.operator.workspaces.batch_experiments",
     "BatchExperimentsWorkspace", "Batch Experiments"),
    # Gate 4's fourth acceptance item: the objective registry, projected
    # read-only so an operator can read what is actually true of each
    # objective -- support state, evidence level, and why a refused
    # submission is refused.
    ("objective_registry", "gui.operator.workspaces.objective_registry",
     "ObjectiveRegistryWorkspace", "Objective Registry"),
    ("intent_decision", "gui.operator.workspaces.intent_decision",
     "IntentDecisionWorkspace", "Intent & Decision"),
    ("analysis", "gui.operator.workspaces.analysis",
     "AnalysisWorkspace", "Analysis & Results"),
    ("demo", "gui.operator.workspaces.demo",
     "DemoWorkspace", "Demo View"),
    ("settings", "gui.operator.workspaces.settings",
     "SettingsWorkspace", "Settings & Integration"),
)


def load_workspace(workspace_id: str, module_name: str, class_name: str,
                   title: str, **kwargs: Any) -> Any:
    """Import and construct one workspace, or return a stated placeholder.

    Deliberately fail-soft *for the missing-module case only*: a console that
    refused to start because one pane is absent would be unusable during a
    parallel build, while a console that silently dropped the tab would hide the
    gap.  A module that exists but raises while constructing is a real defect, so
    that placeholder carries the exception text.
    """
    try:
        module = __import__(module_name, fromlist=[class_name])
    except ImportError as exc:
        return PlaceholderWorkspace(
            workspace_id, title,
            f"module {module_name} is not present in this build ({exc}).")
    factory: Optional[Any] = getattr(module, class_name, None)
    if factory is None:
        return PlaceholderWorkspace(
            workspace_id, title,
            f"{module_name} does not define {class_name}.")
    for arguments in ({k: v for k, v in kwargs.items()}, {}):
        try:
            return factory(**arguments)
        except TypeError:
            # A workspace owned by another track may not accept the console's
            # optional keywords.  Retry bare before giving up, rather than
            # forcing four tracks to agree on a constructor signature the design
            # never froze.
            continue
        except Exception as exc:                          # pragma: no cover
            return PlaceholderWorkspace(
                workspace_id, title,
                f"{class_name} could not be constructed: "
                f"{type(exc).__name__}: {exc}")
    return PlaceholderWorkspace(
        workspace_id, title,
        f"{class_name} accepts neither the console keywords "
        f"({', '.join(sorted(kwargs)) or 'none'}) nor an empty constructor.")


__all__ = ["COCKPIT_WORKSPACES", "PlaceholderWorkspace", "WORKSPACE_MODULES",
           "Workspace", "load_workspace"]
